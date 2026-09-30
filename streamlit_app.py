"""Web view of the IVI release-health report.

Reads the data the weekly job commits (data/app/data.json.gz), recomputes every metric live from the
settings in the sidebar, and reuses the weekly AI narrative when the settings match that run. Changed
settings can be re-drafted with AI, up to a daily cap. The whole app sits behind a share link
(?key=SHARE_KEY) and/or APP_PASSWORD.

Run locally:  streamlit run streamlit_app.py
"""

from __future__ import annotations

import copy
import hmac
import html
import re
import json
import os
import sys
import threading
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from ivi_tracker import analysis, llm, metrics, render, store  # noqa: E402
from ivi_tracker.config import load_config  # noqa: E402

load_dotenv(ROOT / ".env")
APP_NAME = "AGL Release Health"
INTRO = ("Weekly release health for Automotive Grade Linux, an open-source in-vehicle infotainment platform, "
         "from its public Jira.")
st.set_page_config(page_title=APP_NAME, page_icon=":material/directions_car:", layout="wide",
                   initial_sidebar_state="collapsed")

SERIES_BLUE = "#2a78d6"   # categorical slot 1 (validated reference palette)
# Reserved status colors (reference palette): never reused for series, always paired with a text label.
STATUS = {"Red": "#d03b3b", "Amber": "#fab219", "Green": "#0ca30c", "No date": "#8a8984", "Released": "#8a8984"}

STYLES = """
<style>
.verdict { border: 1px solid rgba(128,128,128,.28); border-radius: 10px; padding: 1.25rem 1.5rem 1rem;
           margin: .25rem 0 .75rem; display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr);
           gap: .5rem 2.5rem; }
.verdict .foot { grid-column: 1 / -1; border-top: 1px solid rgba(128,128,128,.2); padding-top: .7rem;
                 margin-top: .4rem; font-size: .85rem; opacity: .8; max-width: none; }
.verdict .headline { font-size: 1.9rem; line-height: 1.2; font-weight: 700; margin: 0 0 .5rem; }
.verdict .subhead { font-size: 1rem; font-weight: 700; margin: .3rem 0 .4rem; }
.verdict .why { display: block; font-size: .88rem; opacity: .75; }
.verdict p { margin: .2rem 0; }
@media (max-width: 820px) { .verdict { grid-template-columns: 1fr; } .verdict .headline { font-size: 1.5rem; } }
.verdict .quiet { opacity: .72; font-size: .92rem; }
.rag { display: inline-flex; align-items: center; gap: .4em; padding: .1em .6em .12em; border-radius: 999px;
       font-size: .8em; font-weight: 600; vertical-align: .12em; background: var(--rag-bg); white-space: nowrap; }
.rag::before { content: ""; width: .6em; height: .6em; border-radius: 50%; background: var(--rag); }
.stMarkdown table, .verdict { font-variant-numeric: tabular-nums; }
::selection { background: rgba(37,106,191,.3); }
h1 { font-size: 1.25rem !important; }
h3 { font-size: 1.2rem !important; }
.stMarkdown p, .stMarkdown li, [data-testid="stCaptionContainer"] p { max-width: 75ch; }
.stMarkdown table p { max-width: none; }
a[href*="/browse/"] { white-space: nowrap; }
.chase { margin: 0 0 .5rem; padding: 0 0 0 1.2rem; }
.chase li { margin: 0 0 .45rem; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
</style>
"""
RULE_GRAY = "#9a9893"
WINDOWS = [7, 14, 30, 60, 90]


def secret(name: str) -> str | None:
    """Streamlit secrets on the hosted app, environment / .env locally."""
    try:
        value = st.secrets.get(name)
    except Exception:          # no secrets file at all
        value = None
    return value or os.getenv(name)


# ---------- access ----------

MIN_SHARE_KEY_LEN = 16   # a short share key would be guessable; ignore it rather than accept it


def _matches(given: str | None, expected: str | None) -> bool:
    return bool(given and expected) and hmac.compare_digest(given.encode(), expected.encode())


def require_password() -> None:
    """Gate the app. Viewers get in with the share link (?key=SHARE_KEY) or by typing APP_PASSWORD."""
    expected = secret("APP_PASSWORD")
    share_key = secret("SHARE_KEY")
    if share_key and len(share_key) < MIN_SHARE_KEY_LEN:
        share_key = None
    if not expected and not share_key:
        st.error("This app is locked. Set `APP_PASSWORD` and/or `SHARE_KEY` in the app's secrets "
                 "(or `.env` locally).")
        st.stop()
    if st.session_state.get("authed"):
        return

    # Share link: log in, then drop the key from the address bar so it isn't copied or bookmarked.
    if "key" in st.query_params:
        given = st.query_params["key"]
        st.query_params.clear()
        if _matches(given, share_key):
            st.session_state.authed = True
            st.rerun()
        st.session_state.bad_link = True

    st.title(APP_NAME)
    st.markdown(INTRO + " Open it with the link or password you were sent.")
    if st.session_state.pop("bad_link", False):
        st.error("That link isn't valid anymore. Ask the person who shared it for a new one.")
    if not expected:
        st.info("Open this app with the link you were sent.")
        st.stop()
    with st.form("login"):
        attempt = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Enter")
    if submitted and _matches(attempt, expected):
        st.session_state.authed = True
        st.rerun()
    if submitted:
        st.error("Wrong password.")
    st.stop()


# ---------- data ----------

@st.cache_data(ttl=3600, show_spinner=False)
def load_data() -> dict:
    return store.load_app_data()


@st.cache_data(ttl=3600, show_spinner=False)
def load_weekly_run(as_of: str) -> tuple[dict, dict] | None:
    """Facts and AI drafts from the weekly run that produced this data, if present."""
    run_dir = ROOT / "reports" / as_of
    try:
        return (json.loads((run_dir / "facts.json").read_text()),
                json.loads((run_dir / "drafts.json").read_text()))
    except FileNotFoundError:
        return None


@st.cache_resource
def ai_budget() -> dict:
    """Process-wide daily counter of AI drafts (shared by every viewer)."""
    return {"lock": threading.Lock(), "day": None, "used": 0}


def ai_remaining(cap: int) -> int:
    b = ai_budget()
    with b["lock"]:
        if b["day"] != date.today():
            b["day"], b["used"] = date.today(), 0
        return max(cap - b["used"], 0)


def spend_ai_draft(cap: int) -> bool:
    b = ai_budget()
    with b["lock"]:
        if b["day"] != date.today():
            b["day"], b["used"] = date.today(), 0
        if b["used"] >= cap:
            return False
        b["used"] += 1
        return True


# ---------- settings ----------

def version_sort_key(name: str) -> list:
    """Natural order: 'Quillback 17.1.8' before 'Quillback 17.1.10'."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def sidebar_settings(defaults: dict, versions: list[dict], issues: list[dict]) -> tuple[dict, int]:
    cfg = copy.deepcopy(defaults)
    sb = st.sidebar
    sb.header("Settings")
    # Every control's key carries a generation number; Reset bumps it, so each control is rebuilt from its
    # default. (Deleting session keys left some controls showing stale values while the page used defaults.)
    gen = st.session_state.setdefault("settings_gen", 0)

    def k(name: str) -> str:
        return f"s_{name}_{gen}"

    if sb.button("Reset to defaults", use_container_width=True):
        st.session_state.settings_gen = gen + 1
        st.rerun()

    window = sb.select_slider("Window (days)", WINDOWS, value=defaults["window_days"], key=k("window"),
                              help="Period for new / resolved counts. AGL is quiet, so 30 is the default.")
    cfg["agenda_size"] = sb.slider("Bug-review agenda size", 5, 20, defaults["agenda_size"], key=k("agenda"))
    cfg["stale_days"] = sb.number_input("Stale after (idle days)", 7, 365, defaults["stale_days"], key=k("stale"))

    with sb.expander("Release readiness (RAG)"):
        rag = cfg["rag"]
        rag["amber_done_pct"] = st.slider("Amber if done % is below", 50, 100, rag["amber_done_pct"], key=k("pct"))
        rag["amber_days_to_release"] = st.number_input("…within this many days of release", 1, 90,
                                                       rag["amber_days_to_release"], key=k("days"))
        rag["rate_undated"] = st.toggle("Rate versions with no release date", rag["rate_undated"], key=k("undated"),
                                        help="On: an open blocker makes it Red; a critical or slip makes it "
                                             "Amber. Off: shown as 'No date'.")

    with sb.expander("Target release dates"):
        st.caption("AGL doesn't set release dates in Jira. Add your own targets to turn on the date-based rules.")
        open_ids = {vid for i in issues if i["status_category"] != "Done" for vid in i["fix_versions"]}
        pending = [v for v in versions if not v["released"] and not v["archived"]]
        active = sorted((v["name"] for v in pending if v["id"] in open_ids), key=version_sort_key)
        rest = sorted((v["name"] for v in pending if v["id"] not in open_ids), key=version_sort_key)
        show_rest = st.toggle(f"Also show {len(rest)} versions with no open work", False, key=k("plan_all"))
        unreleased = active + (rest if show_rest else [])
        plan_df = pd.DataFrame({"Version": unreleased, "Target date": [None] * len(unreleased)})
        plan_df["Target date"] = pd.to_datetime(plan_df["Target date"])
        edited = st.data_editor(
            plan_df, key=k("plan"), hide_index=True, use_container_width=True, disabled=["Version"],
            column_config={"Target date": st.column_config.DateColumn(format="YYYY-MM-DD")})
        cfg["release_plan"] = {row["Version"]: row["Target date"].date().isoformat()
                               for _, row in edited.iterrows() if pd.notna(row["Target date"])}

    with sb.expander("Escalation rules"):
        esc = cfg["escalation"]
        for rid in ("E1", "E2"):
            rule = esc[rid]
            st.markdown(f"**{rid}:** {rule['priority']} stalled")
            c1, c2 = st.columns(2)
            rule["min_age_days"] = c1.number_input("Open more than (days)", 0, 365, rule["min_age_days"],
                                                   key=k(f"{rid}_age"))
            rule["min_idle_days"] = c2.number_input("Idle more than (days)", 0, 365, rule["min_idle_days"],
                                                    key=k(f"{rid}_idle"))
        esc["E3"]["enabled"] = st.toggle("E3: open issue in a released version", True, key=k("E3"))
        esc["E4"]["enabled"] = st.toggle("E4: Blocker/Critical with no owner", True, key=k("E4"))
    return cfg, window


# ---------- views ----------

def rag_pill(rag: str) -> str:
    color = STATUS[rag]
    return (f'<span class="rag" style="--rag:{color};--rag-bg:{color}29">{html.escape(rag)}</span>')


def rag_cell_style(value: str) -> str:
    color = STATUS.get(value)
    return f"background-color: {color}33; font-weight: 600" if color else ""


def version_label(row: dict) -> str:
    return f"{row['name']} (branch)" if row.get("is_branch") else row["name"]


def show_verdict(a: dict, window: int, source_line: str) -> None:
    """Lead with the answer: the worst release and what blocks it, beside what to chase first."""
    v = analysis.verdict(a)
    worst, left, right = v["worst"], [], []
    if worst is None:
        left.append('<div class="headline" role="heading" aria-level="2">No unreleased versions have open work</div>')
    elif worst["rag"] == "Green":
        left.append(f'<div class="headline" role="heading" aria-level="2">{rag_pill("Green")} '
                    'All active releases are on track</div>')
    else:
        state = "is at risk" if worst["rag"] == "Red" else "needs attention"
        left.append(f'<div class="headline" role="heading" aria-level="2">{rag_pill(worst["rag"])} '
                    f'{html.escape(version_label(worst))} {state}</div>')
        for c in v["culprits"][:2]:
            left.append(f'<p>Open {c["priority"]} on this release: {jira_link(c["key"])} '
                        f'{html.escape(c["summary"])} · idle {c["idle_days"]} days</p>')
        if worst.get("rag_basis", "full") != "full":
            left.append('<p class="quiet">Rated on blockers, criticals, and slips: Jira has no release date '
                        'for this version.</p>')
    if v["others"]:
        others = " · ".join(f"{html.escape(version_label(r))} {rag_pill(r['rag'])} {r['open']} open"
                            for r in v["others"])
        left.append(f'<p class="quiet">Other active versions: {others}</p>')
    f = a["flow"]
    left.append(f'<p class="quiet">{a["open_total"]} open issues, {a["open_total"] - a["open_unscoped"]} of them '
                f'assigned to a release · {f["new"]} new and {f["resolved"]} resolved in the last {window} days '
                f'(net {f["net"]:+d})</p>')

    if v["chase"]:
        items = "".join(f'<li>{jira_link(e["key"])} {html.escape(e["summary"])}'
                        f'<span class="why">{html.escape(e["reason"])}'
                        f'{" · on an at-risk release" if e["at_risk"] else ""} · idle {e["idle_days"]} days</span></li>'
                        for e in v["chase"])
        note = (f'{v["escalated_blocker_critical"]} of {v["open_blocker_critical"]} open Blocker/Critical issues trip a rule at '
                "these thresholds, so they're ranked: at-risk release first, then no owner, then priority, then "
                "idle time." if v["saturated"] else
                f'{v["escalations"]} issues trip an escalation rule.')
        right.append(f'<div class="subhead">Chase these first</div><ol class="chase">{items}</ol>'
                     f'<p class="quiet">{note}</p>')
    else:
        right.append('<div class="subhead">Nothing to escalate</div>'
                     '<p class="quiet">No open issue trips an escalation rule at these thresholds.</p>')

    foot = ("Releases use fish codenames (Unagi 21, Vimba 22); Red, Amber, and Green rate how ready each one is. "
            "Every number here is computed from the Jira data in code; AI only writes the narrative, and each "
            f"number it writes is checked against those computations. {source_line}")
    st.markdown(f'<div class="verdict"><div class="col">{"".join(left)}</div>'
                f'<div class="col">{"".join(right)}</div><p class="foot">{foot}</p></div>',
                unsafe_allow_html=True)


def jira_link(key: str) -> str:
    return (f'<a href="{browse(key)}" target="_blank" rel="noopener">{key}'
            f'<span class="sr-only"> (opens Jira in a new tab)</span></a>')


def link_column(label: str = "Key"):
    return st.column_config.LinkColumn(label, display_text=r"https?://.*/browse/(.*)")


def browse(key: str) -> str:
    return render.link(key).split("(", 1)[1].rstrip(")")


def trend_chart(df: pd.DataFrame, field: str, title: str) -> alt.LayerChart:
    """Single-series line with a crosshair + tooltip on hover."""
    hover = alt.selection_point(fields=["week_ending"], nearest=True, on="pointerover", empty=False)
    base = alt.Chart(df).encode(x=alt.X("week_ending:T", title=None, axis=alt.Axis(format="%b %d", grid=False)))
    y = alt.Y(f"{field}:Q", title=None, scale=alt.Scale(zero=True), axis=alt.Axis(tickCount=5))
    line = base.mark_line(color=SERIES_BLUE, strokeWidth=2).encode(y=y)
    dots = base.mark_point(color=SERIES_BLUE, filled=True, size=70).encode(
        y=y, opacity=alt.condition(hover, alt.value(1), alt.value(0)))
    rule = base.mark_rule(color=RULE_GRAY).encode(
        opacity=alt.condition(hover, alt.value(0.8), alt.value(0)),
        tooltip=[alt.Tooltip("week_ending:T", title="Week ending", format="%b %d, %Y"),
                 alt.Tooltip(f"{field}:Q", title=title)],
    ).add_params(hover)
    return (line + dots + rule).properties(title=title, height=260)


def show_narrative(heading: str, text: str) -> None:
    """Keep the section in place either way, so the page doesn't silently change shape."""
    st.subheader(heading)
    st.markdown(render.narrative(text))


def show_status(a: dict, drafts: llm.Drafts, stale_days: int) -> None:
    s = drafts.status_sections
    st.subheader("Release readiness")
    rows = a["readiness"]["rows"]
    st.dataframe(pd.DataFrame([{
        "Version": version_label(r), "RAG": r["rag"],
        "Release date": r["release_date"] or "no date", "Scope": r["scope"],
        "Done %": r["done_pct"], "Open": r["open"],
        "Open blockers": r["open_blockers"], "Open criticals": r["open_criticals"],
    } for r in rows]).style.map(rag_cell_style, subset=["RAG"]), hide_index=True, use_container_width=True,
        column_config={"Done %": st.column_config.ProgressColumn(format="%.1f%%", min_value=0, max_value=100)})
    if any(r.get("rag_basis", "full") not in ("full",) and r["rag"] != "Released" for r in rows):
        st.caption("Undated versions are rated on blockers, criticals, and slips only.")
    done = a["readiness"]["complete_not_released"]
    if done:
        with st.expander(f"{len(done)} versions are fully done but not marked released in Jira"):
            st.write(", ".join(done))

    st.subheader("Blockers & criticals")
    if a["blockers"]:
        st.dataframe(pd.DataFrame([{
            "Key": browse(b["key"]), "Summary": b["summary"], "Release": ", ".join(b["releases"]) or "none",
            "Priority": b["priority"], "Age (days)": b["age_days"], "Idle (days)": b["idle_days"],
            "Owner": "yes" if b["assigned"] else "no",
        } for b in a["blockers"]]), hide_index=True, use_container_width=True,
            height=38 + 35 * len(a["blockers"]), column_config={"Key": link_column()})
    else:
        st.write("No open Blocker or Critical issues.")

    show_narrative("Risks & slips", s["Risks & slips"])
    if a["past_due"]:
        st.dataframe(pd.DataFrame([{"Key": browse(p["key"]), "Version": p["version"], "Priority": p["priority"],
                                    "Why": f"open in a {p['reason']} version"} for p in a["past_due"]]),
                     hide_index=True, use_container_width=True, column_config={"Key": link_column()})

    st.subheader("Backlog hygiene")
    for check, keys in a["hygiene"].items():
        label = render.HYGIENE_LABEL.get(check, check).replace("30", str(stale_days))
        with st.expander(f"{label}: **{len(keys)}**"):
            st.markdown(", ".join(render.link(k) for k in keys) or "None")

    show_narrative("Asks / decisions needed", s["Asks / decisions needed"])
    show_narrative("Next steps", s["Next steps"])


def artifact_body(markdown: str) -> str:
    """Drop the artifact's H1 and 'Draft generated by' line; the page already has a header."""
    lines = markdown.splitlines()
    return "\n".join(l for l in lines if not l.startswith("# ") and not l.startswith("*Draft generated"))


def show_artifact(markdown: str) -> None:
    """Render a markdown artifact as-is: its tables wrap long sentences, unlike data grids."""
    st.markdown(artifact_body(markdown))


def show_trends(issues: list[dict], as_of: date) -> None:
    df = pd.DataFrame(metrics.open_trend(issues, as_of, weeks=26))
    c1, c2 = st.columns(2)
    c1.altair_chart(trend_chart(df, "open", "Open issues"), use_container_width=True)
    c2.altair_chart(trend_chart(df, "open_blocker_critical", "Open Blocker/Critical issues"),
                    use_container_width=True)
    st.caption("Last 26 weeks, rebuilt from created and resolved dates using each issue's current priority "
               "(reopens and priority changes aren't captured).")
    with st.expander("Table view"):
        st.dataframe(df.rename(columns={"week_ending": "Week ending", "open": "Open issues",
                                        "open_blocker_critical": "Open Blocker/Critical"}),
                     hide_index=True, use_container_width=True)


# ---------- page ----------

def main() -> None:
    require_password()
    defaults = load_config()
    try:
        data = load_data()
    except (FileNotFoundError, OSError, ValueError):
        st.error("No report data yet. The weekly job writes `data/app/data.json.gz`; run "
                 "`python -m ivi_tracker run` or trigger the workflow, then reload.")
        st.stop()
    issues, versions = data["issues"], data["versions"]
    as_of = date.fromisoformat(data["as_of"])
    cfg, window = sidebar_settings(defaults, versions, issues)

    a = analysis.analyze(issues, versions, as_of, cfg, window)
    facts = llm.build_facts(a, cfg)
    facts_key = json.dumps(facts, sort_keys=True)

    # Narrative: weekly run's AI draft if the facts match exactly, else one drafted this session, else none.
    weekly = load_weekly_run(data["as_of"])
    session_drafts = st.session_state.setdefault("drafts", {})
    if weekly and weekly[0] == facts:
        drafts, source = render.drafts_from_dict(weekly[1]), "weekly"
    elif facts_key in session_drafts:
        drafts, source = session_drafts[facts_key], "session"
    elif weekly and (reused := llm.reuse_drafts(render.drafts_from_dict(weekly[1]), facts)).complete:
        drafts, source = reused, "weekly"
    elif weekly and (any(reused.status_sections.values()) or reused.agenda_questions or reused.escalation_notes):
        drafts, source = reused, "partial"
    else:
        drafts, source = llm.placeholder_drafts(facts), None

    st.markdown(STYLES, unsafe_allow_html=True)
    st.title(APP_NAME)
    st.markdown(INTRO + f" Project SPEC · {len(issues):,} issues · data as of {data['as_of']} · "
                f"window {window} days. Thresholds are adjustable in the sidebar.")
    source_line = {"weekly": "The narrative below was drafted by AI in the weekly run, and every number in it "
                             "matches these settings.",
                   "session": "The narrative below was drafted by AI for these settings."}.get(source, "")
    show_verdict(a, window, source_line)

    cap = int(defaults.get("app", {}).get("max_ai_drafts_per_day", 20))
    if source not in ("weekly", "session"):
        left, has_key = ai_remaining(cap), bool(secret("OPENAI_API_KEY"))
        c1, c2 = st.columns([3, 1], vertical_alignment="center")
        c1.caption(":material/edit_note: " + (
            "Parts of the weekly narrative no longer match these settings, so they're hidden. Every table is "
            "current." if source == "partial" else
            "No narrative is drafted for these settings. Every table is current."))
        blocked = ("AI drafting isn't set up on this app." if not has_key else
                   "Today's AI drafts are used up. Try again tomorrow." if not left else None)
        if c2.button(f"Draft with AI · {left} left today", disabled=blocked is not None,
                     help=blocked or "The daily drafts are shared by everyone using this app.",
                     use_container_width=True):
            if spend_ai_draft(cap):
                os.environ.setdefault("OPENAI_API_KEY", secret("OPENAI_API_KEY") or "")
                with st.spinner("Drafting… about 10 seconds"):
                    try:
                        session_drafts[facts_key] = llm.draft_all(facts, llm.openai_complete(cfg))
                    except llm.LLMGuardError as exc:
                        st.error("The AI draft cited numbers that aren't in the data, twice, so it wasn't "
                                 "used. The tables are unaffected; try again or keep the tables as they are.")
                    except Exception:  # network / API errors: keep the tables usable
                        st.error("Couldn't reach the AI service. The tables are unaffected; try again in a "
                                 "minute.")
                    else:
                        st.rerun()

    tabs = st.tabs(["Weekly status", "Bug review", "Escalations", "Trends", "Download"])
    with tabs[0]:
        show_status(a, drafts, cfg["stale_days"])
    artifacts = render.render_strings(a, drafts, cfg)
    with tabs[1]:
        show_artifact(artifacts["bug_review"])
    with tabs[2]:
        show_artifact(artifacts["escalations"])
    with tabs[3]:
        show_trends(issues, as_of)
    with tabs[4]:
        st.caption("Confluence-ready markdown for the current settings.")
        for name, text in artifacts.items():
            st.download_button(f"{name}.md", text, file_name=f"{name}_{data['as_of']}.md",
                               mime="text/markdown", use_container_width=True)

    st.caption("Uses public AGL Jira data. Not affiliated with Automotive Grade Linux or the Linux Foundation.")


main()
