"""Ingestion tests: /search/jql pagination, 429 retry, and issue flattening."""

import json

import pytest

from ivi_tracker import jira_client, store
from ivi_tracker.jira_client import JiraClient


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code, self._payload, self.headers = status, payload or {}, headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls, self.headers, self.auth = list(responses), [], {}, None

    def get(self, url, params=None, timeout=None):
        self.calls.append(params or {})
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(jira_client.time, "sleep", lambda s: None)


def issue(key):
    return {"id": key.split("-")[1], "key": key, "fields": {}}


def test_search_all_follows_next_page_token():
    session = FakeSession([
        FakeResponse(200, {"issues": [issue("SPEC-1"), issue("SPEC-2")], "nextPageToken": "t1", "isLast": False}),
        FakeResponse(200, {"issues": [issue("SPEC-3")], "isLast": True}),
    ])
    client = JiraClient(session=session)
    got = client.search_all("project = SPEC")
    assert [i["key"] for i in got] == ["SPEC-1", "SPEC-2", "SPEC-3"]
    assert "nextPageToken" not in session.calls[0]
    assert session.calls[1]["nextPageToken"] == "t1"
    assert client.api_calls == 2


def test_search_all_retries_429_with_retry_after(monkeypatch):
    slept = []
    monkeypatch.setattr(jira_client.time, "sleep", slept.append)
    session = FakeSession([
        FakeResponse(429, headers={"Retry-After": "7"}),
        FakeResponse(200, {"issues": [issue("SPEC-1")], "isLast": True}),
    ])
    assert len(JiraClient(session=session).search_all("project = SPEC")) == 1
    assert slept == [7]


def test_flatten_issue_keeps_ids_not_names():
    raw = {"id": "1", "key": "SPEC-1", "fields": {
        "summary": "no sound output", "issuetype": {"name": "Bug"},
        "status": {"name": "Open", "statusCategory": {"name": "To Do"}},
        "priority": {"name": "Blocker"}, "components": [{"name": "audio"}],
        "fixVersions": [{"id": "10"}], "labels": [],
        "assignee": {"accountId": "abc", "displayName": "Some Person"},
        "created": "2026-01-01T00:00:00.000+0000", "updated": "2026-01-02T00:00:00.000+0000",
        "resolutiondate": None,
    }}
    row = store.flatten_issue(raw)
    assert row["status_category"] == "To Do"
    assert json.loads(row["components"]) == ["audio"]
    assert json.loads(row["fix_versions"]) == ["10"]
    assert row["assignee_id"] == "abc"
    assert "Some Person" not in json.dumps(row)


def test_upsert_is_idempotent(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    for _ in range(2):
        store.upsert_issues(conn, [issue("SPEC-1"), issue("SPEC-2")])
    assert conn.execute("SELECT count(*) FROM issues").fetchone()[0] == 2


def test_snapshot_has_open_issues_only_and_no_people(tmp_path):
    from datetime import date
    conn = store.connect(tmp_path / "t.db")
    store.replace_versions(conn, [{"id": "10", "name": "Vimba 22.0.0", "released": False, "archived": False}])
    open_issue = {"id": "1", "key": "SPEC-1", "fields": {
        "status": {"statusCategory": {"name": "To Do"}}, "priority": {"name": "Blocker"},
        "fixVersions": [{"id": "10"}], "components": [{"name": "audio"}],
        "assignee": {"accountId": "abc", "displayName": "Some Person"}}}
    done_issue = {"id": "2", "key": "SPEC-2", "fields": {"status": {"statusCategory": {"name": "Done"}}}}
    store.upsert_issues(conn, [open_issue, done_issue])
    path = store.save_snapshot(conn, date(2026, 10, 5), snapshot_dir=tmp_path)
    snap = json.loads(path.read_text())
    assert path.name == "2026-10-05.json"
    assert snap["open_issues"] == {"SPEC-1": {"status_category": "To Do", "priority": "Blocker",
                                              "components": ["audio"], "fix_versions": ["Vimba 22.0.0"]}}
    assert "abc" not in path.read_text() and "Some Person" not in path.read_text()


def test_app_export_round_trips_without_account_ids(tmp_path):
    from datetime import date
    conn = store.connect(tmp_path / "t.db")
    store.replace_versions(conn, [{"id": "10", "name": "Vimba 22.0.0", "released": False, "archived": False}])
    store.upsert_issues(conn, [{"id": "1", "key": "SPEC-1", "fields": {
        "status": {"statusCategory": {"name": "To Do"}}, "assignee": {"accountId": "abc-123"}}}])
    path = store.export_app_data(conn, date(2026, 10, 5), out_dir=tmp_path)
    data = store.load_app_data(path)
    assert data["as_of"] == "2026-10-05"
    assert data["issues"][0]["assignee_id"] == "assigned"
    assert data["versions"][0]["name"] == "Vimba 22.0.0"
    assert b"abc-123" not in gzip_bytes(path)


def gzip_bytes(path):
    import gzip
    return gzip.decompress(path.read_bytes())


def test_email_addresses_are_removed_from_summaries():
    row = store.flatten_issue({"id": "1", "key": "SPEC-1", "fields": {
        "summary": "Jane (jane.doe@example.io) cannot edit pages"}})
    assert row["summary"] == "Jane ([email removed]) cannot edit pages"
