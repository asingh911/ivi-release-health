"""SQLite cache (data/cache.db), weekly snapshots, and the run log (reports/run_log.csv)."""

from __future__ import annotations

import csv
import gzip
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "cache.db"
SNAPSHOT_DIR = ROOT / "data" / "snapshots"
RUN_LOG_PATH = ROOT / "reports" / "run_log.csv"

RUN_LOG_COLUMNS = [
    "timestamp", "mode", "window_days", "issues_pulled", "api_calls",
    "fetch_s", "metrics_s", "llm_s", "render_s", "total_s", "llm_tokens",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    key              TEXT PRIMARY KEY,
    id               TEXT NOT NULL,
    summary          TEXT,
    issuetype        TEXT,
    status           TEXT,
    status_category  TEXT,          -- To Do / In Progress / Done
    priority         TEXT,
    components       TEXT NOT NULL, -- JSON list of names
    fix_versions     TEXT NOT NULL, -- JSON list of version ids
    labels           TEXT NOT NULL, -- JSON list
    assignee_id      TEXT,          -- accountId only; names are never stored
    created          TEXT,
    updated          TEXT,
    resolutiondate   TEXT,
    fetched_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    release_date TEXT,
    released     INTEGER NOT NULL,
    archived     INTEGER NOT NULL,
    fetched_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS components (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_log (
    timestamp    TEXT NOT NULL,
    mode         TEXT NOT NULL,
    window_days  INTEGER,
    issues_pulled INTEGER,
    api_calls    INTEGER,
    fetch_s      REAL,
    metrics_s    REAL,
    llm_s        REAL,
    render_s     REAL,
    total_s      REAL,
    llm_tokens   INTEGER
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def redact(text: str | None) -> str | None:
    """Strip email addresses: some issue titles quote a person's address, and nothing published should."""
    return EMAIL.sub("[email removed]", text) if text else text


def _name(obj: dict | None) -> str | None:
    return obj.get("name") if obj else None


def flatten_issue(raw: dict) -> dict:
    """Map one /search/jql issue to an `issues` row."""
    f = raw.get("fields", {})
    status = f.get("status") or {}
    return {
        "key": raw["key"],
        "id": raw["id"],
        "summary": redact(f.get("summary")),
        "issuetype": _name(f.get("issuetype")),
        "status": status.get("name"),
        "status_category": _name(status.get("statusCategory")),
        "priority": _name(f.get("priority")),
        "components": json.dumps(sorted(c["name"] for c in f.get("components") or [])),
        "fix_versions": json.dumps(sorted(v["id"] for v in f.get("fixVersions") or [])),
        "labels": json.dumps(sorted(f.get("labels") or [])),
        "assignee_id": (f.get("assignee") or {}).get("accountId"),
        "created": f.get("created"),
        "updated": f.get("updated"),
        "resolutiondate": f.get("resolutiondate"),
    }


def upsert_issues(conn: sqlite3.Connection, raw_issues: list[dict]) -> int:
    fetched_at = now_iso()
    rows = [{**flatten_issue(r), "fetched_at": fetched_at} for r in raw_issues]
    if rows:
        cols = list(rows[0])
        conn.executemany(
            f"INSERT OR REPLACE INTO issues ({','.join(cols)}) VALUES ({','.join(':' + c for c in cols)})",
            rows,
        )
    conn.commit()
    return len(rows)


def prune_missing_issues(conn: sqlite3.Connection, keep_keys: set[str]) -> int:
    """After a full pull, drop cached issues that no longer exist (deleted or moved)."""
    cached = {r["key"] for r in conn.execute("SELECT key FROM issues")}
    gone = cached - keep_keys
    conn.executemany("DELETE FROM issues WHERE key = ?", [(k,) for k in gone])
    conn.commit()
    return len(gone)


def replace_versions(conn: sqlite3.Connection, versions: list[dict]) -> None:
    fetched_at = now_iso()
    conn.execute("DELETE FROM versions")
    conn.executemany(
        "INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?)",
        [(v["id"], v["name"], v.get("releaseDate"), int(v.get("released", False)),
          int(v.get("archived", False)), fetched_at) for v in versions],
    )
    conn.commit()


def replace_components(conn: sqlite3.Connection, components: list[dict]) -> None:
    fetched_at = now_iso()
    conn.execute("DELETE FROM components")
    conn.executemany("INSERT INTO components VALUES (?, ?, ?)",
                     [(c["id"], c["name"], fetched_at) for c in components])
    conn.commit()


def append_run_log(conn: sqlite3.Connection, row: dict, csv_path: Path = RUN_LOG_PATH) -> dict:
    """Write one run row to both SQLite and the committed CSV (timing and volume per run)."""
    full = {c: row.get(c) for c in RUN_LOG_COLUMNS}
    full["timestamp"] = full["timestamp"] or now_iso()
    conn.execute(f"INSERT INTO run_log VALUES ({','.join('?' * len(RUN_LOG_COLUMNS))})",
                 [full[c] for c in RUN_LOG_COLUMNS])
    conn.commit()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not csv_path.exists()
    with csv_path.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=RUN_LOG_COLUMNS)
        if new_file:
            writer.writeheader()
        writer.writerow(full)
    return full


def load_issues(conn: sqlite3.Connection) -> list[dict]:
    """Cached issues in the shape metrics.py expects (JSON columns decoded to lists)."""
    out = []
    for r in conn.execute("SELECT * FROM issues"):
        d = dict(r)
        for col in ("components", "fix_versions", "labels"):
            d[col] = json.loads(d[col])
        out.append(d)
    return out


def load_versions(conn: sqlite3.Connection) -> list[dict]:
    return [{**dict(r), "released": bool(r["released"]), "archived": bool(r["archived"])}
            for r in conn.execute("SELECT id, name, release_date, released, archived FROM versions")]


def save_snapshot(conn: sqlite3.Connection, as_of, snapshot_dir: Path = SNAPSHOT_DIR) -> Path:
    """Write open issues' planning fields to data/snapshots/YYYY-MM-DD.json (committed; used for slip diffs).

    Keys, priorities, components, and fix-version names only: no assignees, no summaries.
    """
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM versions")}
    rows = conn.execute(
        "SELECT key, status_category, priority, components, fix_versions FROM issues "
        "WHERE status_category != 'Done' ORDER BY id + 0")
    issues = {
        r["key"]: {
            "status_category": r["status_category"], "priority": r["priority"],
            "components": json.loads(r["components"]),
            "fix_versions": sorted(names.get(v, v) for v in json.loads(r["fix_versions"])),
        }
        for r in rows
    }
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = snapshot_dir / f"{as_of.isoformat()}.json"
    path.write_text(json.dumps({"as_of": as_of.isoformat(), "open_issues": issues}, indent=1) + "\n")
    return path


APP_DATA_DIR = ROOT / "data" / "app"


def export_app_data(conn: sqlite3.Connection, as_of, out_dir: Path = APP_DATA_DIR) -> Path:
    """Write the issue/version data the web app reads (committed weekly, so the app never hits Jira).

    Assignee account IDs are replaced by a plain "assigned" marker: the app only needs to know whether
    an issue has an owner.
    """
    issues = load_issues(conn)
    for i in issues:
        i["assignee_id"] = "assigned" if i["assignee_id"] else None
        i.pop("fetched_at", None)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"as_of": as_of.isoformat(), "issues": issues, "versions": load_versions(conn)}
    path = out_dir / "data.json.gz"
    # mtime=0 keeps the file byte-identical when the data hasn't changed (no noise commits).
    with gzip.GzipFile(path, "wb", mtime=0) as fh:
        fh.write(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return path


def load_app_data(path: Path = APP_DATA_DIR / "data.json.gz") -> dict:
    with gzip.open(path, "rt") as fh:
        return json.load(fh)
