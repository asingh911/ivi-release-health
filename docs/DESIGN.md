# Design

How ivi-release-health gets its data, what every metric means, and how the LLM is kept grounded.

- [Data source](#data-source)
- [Metric and rule definitions](#metric-and-rule-definitions)
- [LLM drafting](#llm-drafting)
- [Output format](#output-format)
- [Repo layout](#repo-layout)
- [Adapting it to another Jira project](#adapting-it-to-another-jira-project)

## Data source

[Automotive Grade Linux](https://www.automotivelinux.org/) (AGL) is a Linux Foundation–hosted, open-source Linux
platform for connected cars. It started with in-vehicle infotainment (IVI) and uses Jira for both defect tracking
and project management. Releases use fish codenames in alphabetical order (Quillback, Ricefish, Salmon, Trout,
Unagi, Vimba, …).

| | |
|---|---|
| Jira | `https://lf-automotivelinux.atlassian.net` |
| Project | `SPEC` ("AGL Development") |
| Auth | None needed: anonymous reads work. Optional Basic auth via `JIRA_EMAIL` + `JIRA_API_TOKEN` |
| Size | ~5,700 issues, ~156 open |

### Endpoints

| Purpose | Endpoint |
|---|---|
| Search issues with JQL | `GET /rest/api/3/search/jql` |
| Versions (name, release date, released flag) | `GET /rest/api/3/project/SPEC/versions` |
| Components | `GET /rest/api/3/project/SPEC/components` |
| Changelog | `GET /rest/api/3/issue/{key}/changelog` |

> **Note:** the older `/rest/api/3/search` endpoint has been removed from Jira Cloud. `/search/jql` paginates
> with `nextPageToken`, not `startAt`, and pages can be shorter than `maxResults`, so the client loops until
> there is no token. Many tutorials and older Jira client libraries still call the removed endpoint, which is
> why this project uses `requests` directly.

Fields requested per issue: `summary, issuetype, status, priority, components, fixVersions, labels, created,
updated, resolutiondate, assignee`. Status is read from `status.statusCategory.name` (To Do / In Progress /
Done) rather than raw status names, since workflows vary.

### Being a good citizen

This is a community project's server, so the client sends requests one at a time, 100 issues per page, pauses
0.3 s between pages, honors `Retry-After` on HTTP 429, backs off on 5xx, caches everything in SQLite, and sends a
descriptive User-Agent. A full pull is about 60 requests.

### JQL cookbook

| View | JQL |
|---|---|
| Everything | `project = SPEC ORDER BY created ASC` |
| Open | `project = SPEC AND statusCategory != Done` |
| Open blockers/criticals | `project = SPEC AND statusCategory != Done AND priority in (Blocker, Critical) ORDER BY priority DESC, updated ASC` |
| New bugs in window | `project = SPEC AND issuetype = Bug AND created >= -7d` |
| Resolved in window | `project = SPEC AND resolved >= -7d` |
| Changed recently (incremental pull) | `project = SPEC AND updated >= -8d` |
| Release scope | `project = SPEC AND fixVersion = "<version name>"` |
| Open in released versions | `project = SPEC AND fixVersion in releasedVersions() AND statusCategory != Done` |
| Unscoped open work | `project = SPEC AND statusCategory != Done AND fixVersion is EMPTY` |
| Stale | `project = SPEC AND statusCategory != Done AND updated <= -30d` |

## Metric and rule definitions

All of these are computed in code (`metrics.py`, `escalation.py`), and every one has a unit test.

| Term | Definition |
|---|---|
| Open | `statusCategory != Done` |
| Age (days) | today − created |
| Idle (days) | today − updated |
| Age buckets | 0–7, 8–30, 31–90, 91–365, 365+ |
| Stale | Open and idle > `stale_days` (default 30) |
| Window | `--window N` days (default 30; use 7 for a busy project) |
| New / resolved / net | Created in window / resolved in window / new − resolved |
| Release scope (version V) | Issues whose fix versions include V; done % = done ÷ total |
| Past-due | Open issue whose fix version is released, or whose release date has passed |

### Backlog hygiene

Among open issues: no fix version, no component, no assignee, and stale. Unreleased versions whose scope is 100%
done are also flagged as "complete but not marked released".

### Release readiness (RAG)

Shown for each unreleased version with open work, plus the most recently released version.

- 🔴 **Red:** at least one open Blocker, or the release date has passed with open issues.
- 🟠 **Amber:** at least one open Critical, or done % below 80 with 14 days or fewer to the release date, or a slip
  into this version during the window.
- 🟢 **Green:** otherwise.

**Undated versions.** AGL no longer sets release dates in Jira, so by default (`rag.rate_undated: true`)
undated versions are rated on the rules that don't need a date (blockers → Red; criticals or slips → Amber).
Set it to `false` to show "No date" instead, or give target dates in `release_plan`:

```yaml
release_plan:
  "Vimba 22.0.0": 2026-12-15
```

### Escalation rules

| Rule | Trigger (defaults) |
|---|---|
| E1 | Blocker open > 7 days and idle > 3 days |
| E2 | Critical open > 14 days and idle > 7 days |
| E3 | Any open issue in a released version |
| E4 | Open Blocker/Critical with no assignee |

An issue that trips several rules lists them all (e.g. `E1, E4`). Thresholds, E4's priorities, and an on/off
switch for E3 are in `config.yaml`.

## LLM drafting

**Code computes every number; the model writes narrative only.**

1. `build_facts()` turns the metrics into a compact facts JSON. Summaries are truncated to 120 characters, and
   people are represented only by an `assigned: true/false` flag.
2. Three short calls: the status narrative (Summary, Risks & slips, Asks / decisions needed, Next steps), one
   triage question per bug-review item, and one note per escalation. The last two use JSON mode and must cover
   every issue key.
3. **Number guard.** Every number in the output must appear in the facts JSON (issue keys and ISO dates are
   ignored). A failing draft is sent back once with the offending numbers listed. If the retry also fails, the
   run fails rather than publishing an unverified number.
4. Prompts steer each item toward an ask that fits it: long-idle items → close or downgrade, crashes and build
   failures → reproduce on the current release, release tasks → target date, unassigned items → owner.

The model is set in `config.yaml` (`llm.model`, default `gpt-5.4-mini`), at temperature 0.2. If a model only
accepts its default temperature, the client drops the setting and retries. A report uses about 7,000 tokens.
`facts.json` is written next to each report so every number in the narrative can be traced.

## Output format

Each run writes `reports/YYYY-MM-DD/`:

| File | Contents |
|---|---|
| `weekly_status.md` | Summary, release readiness table, blockers & criticals, risks & slips, backlog hygiene, asks, new/resolved/net, next steps |
| `bug_review.md` | Top N (default 10) open Blocker/Critical items with a triage question each |
| `escalations.md` | Every item that trips a rule, with days open, idle days, and the ask |
| `facts.json` | The exact facts the LLM saw |
| `drafts.json` | The AI narrative, reused by the web app when its settings match |

Tables are GitHub-flavored markdown, and issue keys link to Jira. Each run also appends a row to
`reports/run_log.csv` (issues pulled, API calls, seconds per stage, total seconds, LLM tokens) and saves a
snapshot of open issues' planning fields to `data/snapshots/YYYY-MM-DD.json`.

## Repo layout

```text
├── config.yaml               # project key, window, RAG + escalation thresholds, agenda size, model
├── src/ivi_tracker/
│   ├── __main__.py           # CLI: fetch | report | run
│   ├── jira_client.py        # search_all(), get_versions(), get_components(), get_changelog()
│   ├── store.py              # SQLite cache, snapshots, run log
│   ├── metrics.py            # pure functions: hygiene, aging, flow, scope, RAG, past-due
│   ├── escalation.py         # rule engine driven by config.yaml
│   ├── analysis.py           # runs every metric and rule; console summary
│   ├── llm.py                # facts JSON, prompts, number guard
│   └── render.py             # Jinja2 templates → markdown
├── streamlit_app.py          # password-protected web view with live settings
├── templates/                # weekly_status / bug_review / escalations .md.j2
├── tests/                    # synthetic edge-case fixture + one real API page
├── data/snapshots/           # weekly snapshots of open issues (committed)
├── data/app/                 # issue data for the web app (committed, refreshed weekly)
├── reports/                  # generated reports + run_log.csv (committed)
└── .github/workflows/weekly.yml
```

`data/cache.db` and `.env` are gitignored.

## Adapting it to another Jira project

1. Set `JIRA_BASE_URL` in `.env` (and `JIRA_EMAIL` / `JIRA_API_TOKEN` if the instance requires auth).
2. Set `project_key` in `config.yaml`.
3. Review the thresholds: `window_days`, `stale_days`, `rag`, and `escalation`.
4. Run `python -m ivi_tracker run --window 7`.

Priorities are expected to include Blocker and Critical; if your instance uses different names (e.g. P0/P1),
update `PRIORITY_RANK` in `metrics.py` and the E1/E2/E4 priorities in `config.yaml`.
