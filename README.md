# ivi-release-health

Automated weekly release-health reporting for [Automotive Grade Linux](https://www.automotivelinux.org/) (AGL),
an open-source in-vehicle infotainment (IVI) platform.

It pulls AGL's full public Jira backlog (5,709 issues) with JQL and computes release-health metrics in code:
blockers, aging, release readiness (RAG), slips, backlog hygiene, and escalation rules. An LLM then drafts
three recurring program artifacts in Confluence-ready markdown:

| Artifact | What it's for | Latest |
|---|---|---|
| Weekly status | Engineering leads, customer PM, steering | [weekly_status.md](reports/2026-09-30/weekly_status.md) |
| Bug-review agenda | Recurring triage meeting: top 10 Blocker/Critical items, one question each | [bug_review.md](reports/2026-09-30/bug_review.md) |
| Escalation tracker | Items that trip a rule (E1–E4), each with a specific ask | [escalations.md](reports/2026-09-30/escalations.md) |

**Code computes every number; the LLM only writes the narrative.** Every draft goes through a number guard
that rejects any number not present in the computed facts, retries once, then fails the run.
A GitHub Actions job runs the whole pipeline every Monday and commits the reports.

## Web app

**Live (invite-only):** https://ivi-release-health-wdfjjdcehdu8nuwwhnu9kw.streamlit.app

`streamlit_app.py` is an interactive view of the same report, open only to people you share it with: send them
a share link (`https://<app>/?key=<SHARE_KEY>`, one click, and the key is removed from the address bar once used)
or a password (`APP_PASSWORD`). Rotate either secret to revoke access. Change the window, RAG and escalation thresholds, agenda size, or target release dates in the
sidebar, and every table recomputes instantly. It also shows 26-week trends of open issues and open
blockers/criticals, and downloads the three markdown artifacts for the current settings.

With default settings it shows the weekly run's AI narrative at no cost. When settings change, a **Draft with AI**
button re-drafts the narrative for them (number-guarded), capped at `app.max_ai_drafts_per_day` in
`config.yaml` across all viewers.

```bash
echo "APP_PASSWORD=choose-a-password" >> .env
.venv/bin/streamlit run streamlit_app.py
```

The app reads `data/app/data.json.gz`, which the weekly job refreshes, so it never calls Jira itself.
To host it, deploy the repo on [Streamlit Community Cloud](https://share.streamlit.io) with `streamlit_app.py` as
the main file and `SHARE_KEY` (16+ characters), `APP_PASSWORD`, and `OPENAI_API_KEY` as app secrets.

## Architecture

```text
fetch (Jira /search/jql) → store (SQLite + weekly snapshot) → metrics & rules → facts JSON
    → LLM drafts (OpenAI) → number guard → Jinja2 render → reports/YYYY-MM-DD/ + run_log.csv
```

| Module | Role |
|---|---|
| `jira_client.py` | `GET /rest/api/3/search/jql` with `nextPageToken` pagination, 429/5xx retry honoring `Retry-After`, polite pacing |
| `store.py` | SQLite cache, weekly snapshots of open issues, run log |
| `metrics.py` | Pure functions: hygiene, aging, flow, release scope, RAG, past-due |
| `escalation.py` | Rule engine (E1–E4), thresholds from `config.yaml` |
| `llm.py` | Facts JSON, prompts, number guard, retry-then-fail |
| `render.py` + `templates/` | Markdown artifacts, issue keys linked to Jira |

## Results

From [`reports/run_log.csv`](reports/run_log.csv), three end-to-end `run`s on 2026-09-30 (full pull + metrics + LLM + render):

| Run | Issues pulled | API calls | Fetch | LLM | Total | LLM tokens |
|---|---|---|---|---|---|---|
| 1 | 5,709 | 60 | 53.3 s | 12.0 s | 65.5 s | 6,896 |
| 2 | 5,709 | 60 | 44.6 s | 9.6 s | 54.4 s | 6,903 |
| 3 | 5,709 | 60 | 44.2 s | 9.9 s | 54.4 s | 6,842 |
| **Median** | **5,709** | | | | **54.4 s** | |

**Manual baseline (estimated, not measured):** building the same status report by hand from the Jira UI is
estimated at roughly **75–120 minutes**:

| Step | Estimate |
|---|---|
| Run ~8 JQL views (open, blockers, new, resolved, stale, missing fix version/component/assignee) and note counts | ~12 min |
| Release readiness for the active versions: scope, done %, open B/C, RAG call | ~15 min |
| Blocker table for 18 items, computing age and idle days from dates | ~18 min |
| Check escalation rules across those items and write 17 notes | ~20 min |
| Draft 10 bug-review questions | ~10 min |
| Summary, risks, asks, next steps | ~15 min |
| Format tables for Confluence | ~10 min |
| **Total** | **~100 min** |

With the tool, prep is the ~1-minute run plus a human review and edit of the drafts before sending.

## What the data shows (and design decisions it forced)

AGL is a community project, so its Jira is a realistic but imperfect stand-in for an OEM IVI program:

- **No unreleased version has a release date**, and versions stopped being marked released after April 2025.
  Undated versions are RAG-rated on the rules that don't need a date (open blocker → Red; open critical or slip → Amber),
  and TPM-maintained target dates can be supplied in `config.yaml` under `release_plan`.
- **24 versions are 100% done but never marked released**, reported as a hygiene finding.
- **150 of 156 open issues have no fix version**, so backlog hygiene is the strongest signal.
- Activity is sparse week to week, so the default window is 30 days (`--window 7` for a busy program).

All thresholds (RAG, escalation, stale days, agenda size) live in [`config.yaml`](config.yaml).

| Rule | Trigger |
|---|---|
| E1 | Blocker open > 7 days and idle > 3 days |
| E2 | Critical open > 14 days and idle > 7 days |
| E3 | Open issue in a released version |
| E4 | Open Blocker/Critical with no assignee |

## How to run

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e .
cp .env.example .env        # add OPENAI_API_KEY; AGL's Jira needs no auth

.venv/bin/python -m ivi_tracker fetch --full          # all SPEC issues -> data/cache.db
.venv/bin/python -m ivi_tracker fetch --incremental   # issues updated in the last 8 days
.venv/bin/python -m ivi_tracker report --window 30    # metrics + LLM + render (--no-llm: tables only)
.venv/bin/python -m ivi_tracker run --window 30       # what CI runs: full fetch + report + snapshot + log
.venv/bin/pytest -q                                   # every metric and rule has a test
```

JQL used for the full pull: `project = SPEC ORDER BY created ASC`. More views are in the [JQL cookbook](docs/DESIGN.md#jql-cookbook).

## Privacy

Committed reports and snapshots contain issue keys, summaries, and components only. Assignee names are never
stored: the local, gitignored cache keeps only Jira's opaque account ID, and committed files record only whether
an issue is assigned.

## Design

[docs/DESIGN.md](docs/DESIGN.md) covers the Jira API details, every metric definition, the LLM design and number
guard, the output format, and how to point the tool at a different Jira project.

---

*Uses public AGL Jira data. Not affiliated with Automotive Grade Linux or the Linux Foundation.*
