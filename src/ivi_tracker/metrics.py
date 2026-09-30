"""Pure metric functions (definitions in docs/DESIGN.md). No I/O: callers pass issue/version dicts and an as-of date.

Issue dicts use the flattened cache shape (see store.load_issues): components and fix_versions are
lists, dates are Jira ISO strings. Version dicts: id, name, release_date (YYYY-MM-DD or None),
released (bool), archived (bool).
"""

from __future__ import annotations

from datetime import date, datetime

PRIORITY_RANK = {"Blocker": 5, "Critical": 4, "Major": 3, "Minor": 2, "Trivial": 1}
AGE_BUCKETS = [(7, "0-7"), (30, "8-30"), (90, "31-90"), (365, "91-365")]
OVERFLOW_BUCKET = "365+"


# ---------- primitives ----------

def to_date(value: str | None) -> date | None:
    """Parse a Jira timestamp ('2026-01-02T03:04:05.000-0700') or plain date to a calendar date."""
    if not value:
        return None
    if len(value) == 10:
        return date.fromisoformat(value)
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z").date()


def is_open(issue: dict) -> bool:
    return issue["status_category"] != "Done"


def age_days(issue: dict, as_of: date) -> int:
    return (as_of - to_date(issue["created"])).days


def idle_days(issue: dict, as_of: date) -> int:
    return (as_of - to_date(issue["updated"])).days


def age_bucket(days: int) -> str:
    for upper, label in AGE_BUCKETS:
        if days <= upper:
            return label
    return OVERFLOW_BUCKET


def in_window(value: str | None, as_of: date, window_days: int) -> bool:
    d = to_date(value)
    return d is not None and 0 <= (as_of - d).days < window_days


# ---------- FR-2: backlog hygiene ----------

def hygiene(issues: list[dict], as_of: date, stale_days: int) -> dict[str, list[str]]:
    """Open issues failing each grooming check, as key lists sorted most-idle first."""
    open_issues = sorted((i for i in issues if is_open(i)), key=lambda i: -idle_days(i, as_of))
    return {
        "no_fix_version": [i["key"] for i in open_issues if not i["fix_versions"]],
        "no_component": [i["key"] for i in open_issues if not i["components"]],
        "no_assignee": [i["key"] for i in open_issues if not i["assignee_id"]],
        "stale": [i["key"] for i in open_issues if idle_days(i, as_of) > stale_days],
    }


# ---------- FR-3: defects & blockers ----------

def blocker_view(issues: list[dict], as_of: date) -> list[dict]:
    """Open Blocker/Critical issues, ranked by priority desc, then idle days desc."""
    rows = [
        {
            "key": i["key"], "summary": i["summary"], "issuetype": i["issuetype"],
            "component": ", ".join(i["components"]) or "none", "priority": i["priority"],
            "age_days": age_days(i, as_of), "age_bucket": age_bucket(age_days(i, as_of)),
            "idle_days": idle_days(i, as_of), "assigned": bool(i["assignee_id"]),
        }
        for i in issues if is_open(i) and i["priority"] in ("Blocker", "Critical")
    ]
    return sorted(rows, key=lambda r: (-PRIORITY_RANK[r["priority"]], -r["idle_days"], r["key"]))


def flow(issues: list[dict], as_of: date, window_days: int) -> dict[str, int]:
    """New / resolved / net in the window."""
    new = sum(in_window(i["created"], as_of, window_days) for i in issues)
    resolved = sum(in_window(i["resolutiondate"], as_of, window_days) for i in issues)
    return {"new": new, "resolved": resolved, "net": new - resolved}


# ---------- FR-4: release readiness ----------

def release_date(version: dict, release_plan: dict[str, str] | None = None) -> date | None:
    """Jira's releaseDate, or a TPM-maintained target from config when Jira has none."""
    planned = (release_plan or {}).get(version["name"])
    return to_date(version.get("release_date") or planned)


def version_scope(version: dict, issues: list[dict]) -> dict:
    scoped = [i for i in issues if version["id"] in i["fix_versions"]]
    open_ = [i for i in scoped if is_open(i)]
    done = len(scoped) - len(open_)
    return {
        "scope": len(scoped),
        "done": done,
        "open": len(open_),
        "done_pct": round(100 * done / len(scoped), 1) if scoped else None,
        "open_blockers": sum(i["priority"] == "Blocker" for i in open_),
        "open_criticals": sum(i["priority"] == "Critical" for i in open_),
        "open_keys": sorted(i["key"] for i in open_),
    }


def rag(scope: dict, rel_date: date | None, as_of: date, slips_in: int, rag_cfg: dict) -> str:
    """RAG for an unreleased version.

    Undated versions return "No date" unless rag_cfg["rate_undated"] is set, in which case the
    date-independent rules still apply (blockers -> Red; criticals or slips -> Amber).
    """
    if rel_date is None and not rag_cfg.get("rate_undated", False):
        return "No date"
    days_to = (rel_date - as_of).days if rel_date else None

    if scope["open_blockers"] >= 1 or (days_to is not None and days_to < 0 and scope["open"] > 0):
        return "Red"
    behind = (days_to is not None and 0 <= days_to <= rag_cfg["amber_days_to_release"]
              and scope["done_pct"] is not None and scope["done_pct"] < rag_cfg["amber_done_pct"])
    if scope["open_criticals"] >= 1 or behind or slips_in > 0:
        return "Amber"
    return "Green"


def release_readiness(versions: list[dict], issues: list[dict], as_of: date, cfg: dict,
                      slips_in: dict[str, int] | None = None) -> dict:
    """Readiness rows for unreleased versions plus the most recently released one.

    Unreleased versions with scope but no open work are split out as `complete_not_released`
    (a hygiene finding: someone should mark them released in Jira) unless config asks to show them.
    """
    rag_cfg, plan = cfg["rag"], cfg.get("release_plan") or {}
    show_complete = cfg.get("readiness", {}).get("include_complete_unreleased", False)
    slips_in = slips_in or {}
    rows, complete_not_released, empty = [], [], []

    released = [v for v in versions if v["released"] and v.get("release_date")]
    latest = max(released, key=lambda v: v["release_date"], default=None)

    for v in versions:
        if v["archived"] or (v["released"] and v is not latest):
            continue
        scope = version_scope(v, issues)
        rel = release_date(v, plan)
        row = {
            "name": v["name"], "id": v["id"], "released": v["released"],
            "release_date": rel.isoformat() if rel else None,
            "date_source": "jira" if v.get("release_date") else ("plan" if rel else None),
            "days_to_release": (rel - as_of).days if rel else None,
            "slips_in": slips_in.get(v["id"], 0),
            **scope,
        }
        if v["released"]:
            row["rag"] = "Released"
        elif scope["scope"] == 0:
            empty.append(v["name"])
            continue
        elif scope["open"] == 0 and not show_complete:
            complete_not_released.append(v["name"])
            continue
        else:
            row["rag"] = rag(scope, rel, as_of, row["slips_in"], rag_cfg)
            row["rag_basis"] = "full" if rel else "undated: blockers/criticals/slips only"
        rows.append(row)

    rag_order = {"Red": 0, "Amber": 1, "Green": 2, "No date": 3, "Released": 4}
    rows.sort(key=lambda r: (rag_order[r["rag"]], r["release_date"] or "9999", r["name"]))
    return {"rows": rows, "complete_not_released": sorted(complete_not_released),
            "empty_unreleased": sorted(empty)}


def past_due(versions: list[dict], issues: list[dict], as_of: date,
             release_plan: dict[str, str] | None = None) -> list[dict]:
    """Slip type (a): open issues whose fix version is released or whose release date has passed."""
    by_id = {v["id"]: v for v in versions}
    out = []
    for i in issues:
        if not is_open(i):
            continue
        for vid in i["fix_versions"]:
            v = by_id.get(vid)
            if v is None:
                continue
            rel = release_date(v, release_plan)
            if v["released"] or (rel is not None and rel < as_of):
                out.append({"key": i["key"], "version": v["name"], "priority": i["priority"],
                            "reason": "released" if v["released"] else "past release date"})
    return sorted(out, key=lambda r: (-PRIORITY_RANK.get(r["priority"], 0), r["key"]))


# ---------- trend ----------

def open_trend(issues: list[dict], as_of: date, weeks: int = 26) -> list[dict]:
    """Open issues at the end of each of the last `weeks` weeks, rebuilt from created/resolved dates.

    An approximation: reopened issues and priority changes aren't in these fields, so each issue's
    current priority is used for the Blocker/Critical line.
    """
    spans = [(to_date(i["created"]), to_date(i["resolutiondate"]), i["priority"] in ("Blocker", "Critical"))
             for i in issues]
    rows = []
    for w in range(weeks - 1, -1, -1):
        day = date.fromordinal(as_of.toordinal() - 7 * w)
        open_now = [(c, r, bc) for c, r, bc in spans if c <= day and (r is None or r > day)]
        rows.append({"week_ending": day.isoformat(), "open": len(open_now),
                     "open_blocker_critical": sum(bc for _, _, bc in open_now)})
    return rows
