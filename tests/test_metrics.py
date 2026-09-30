"""Every metric and rule in docs/DESIGN.md, against the synthetic fixture (as_of 2026-10-04) and hand-built cases."""

import copy
import json
from datetime import date
from pathlib import Path

import pytest

from ivi_tracker import escalation, metrics, store
from ivi_tracker.analysis import analyze, console_summary
from ivi_tracker.config import load_config

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = json.loads((FIXTURES / "sample_issues.json").read_text())
AS_OF = date.fromisoformat(SAMPLE["as_of"])
ISSUES, VERSIONS = SAMPLE["issues"], SAMPLE["versions"]
BY_KEY = {i["key"]: i for i in ISSUES}


@pytest.fixture
def cfg():
    c = load_config()
    c["release_plan"] = {}
    c["rag"]["rate_undated"] = True
    return c


def keys(rows):
    return [r["key"] for r in rows]


# ---------- primitives ----------

def test_open_uses_status_category():
    assert metrics.is_open(BY_KEY["SPEC-1"])       # In Progress
    assert metrics.is_open(BY_KEY["SPEC-2"])       # To Do
    assert not metrics.is_open(BY_KEY["SPEC-4"])   # Done


def test_age_and_idle_days():
    assert metrics.age_days(BY_KEY["SPEC-1"], AS_OF) == 33
    assert metrics.idle_days(BY_KEY["SPEC-1"], AS_OF) == 10


@pytest.mark.parametrize("days,bucket", [
    (0, "0-7"), (7, "0-7"), (8, "8-30"), (30, "8-30"), (31, "31-90"), (90, "31-90"),
    (91, "91-365"), (365, "91-365"), (366, "365+"),
])
def test_age_buckets(days, bucket):
    assert metrics.age_bucket(days) == bucket


def test_to_date_handles_jira_offsets():
    assert metrics.to_date("2016-10-04T06:50:48.000-0700") == date(2016, 10, 4)
    assert metrics.to_date("2026-11-15") == date(2026, 11, 15)
    assert metrics.to_date(None) is None


# ---------- hygiene & stale ----------

def test_hygiene_checks(cfg):
    h = metrics.hygiene(ISSUES, AS_OF, cfg["stale_days"])
    assert set(h["no_fix_version"]) == {"SPEC-2", "SPEC-9", "SPEC-10"}
    assert h["no_component"] == ["SPEC-2"]
    assert h["no_assignee"] == ["SPEC-2"]


def test_stale_is_strictly_more_than_threshold(cfg):
    h = metrics.hygiene(ISSUES, AS_OF, cfg["stale_days"])
    assert h["stale"] == ["SPEC-2"]                            # idle 45
    assert metrics.idle_days(BY_KEY["SPEC-6"], AS_OF) == 30    # idle exactly 30 -> not stale
    assert "SPEC-6" not in h["stale"]


def test_hygiene_ignores_done_issues(cfg):
    h = metrics.hygiene(ISSUES, AS_OF, cfg["stale_days"])
    done = {i["key"] for i in ISSUES if not metrics.is_open(i)}
    assert not done & {k for ks in h.values() for k in ks}


# ---------- window: new / resolved / net ----------

def test_flow_window_30():
    assert metrics.flow(ISSUES, AS_OF, 30) == {"new": 4, "resolved": 1, "net": 3}


def test_flow_window_7():
    # created 2026-09-28 .. 2026-10-04: SPEC-3, SPEC-8, SPEC-9; nothing resolved
    assert metrics.flow(ISSUES, AS_OF, 7) == {"new": 3, "resolved": 0, "net": 3}


# ---------- blocker view ----------

def test_blocker_view_ranked_by_priority_then_idle():
    rows = metrics.blocker_view(ISSUES, AS_OF)
    assert keys(rows) == ["SPEC-1", "SPEC-10", "SPEC-9", "SPEC-2", "SPEC-8"]
    assert rows[0]["age_bucket"] == "31-90" and rows[0]["component"] == "audio"
    assert rows[3]["component"] == "none"


# ---------- release scope & done % ----------

def test_version_scope_and_done_pct():
    v3 = next(v for v in VERSIONS if v["id"] == "v3")
    s = metrics.version_scope(v3, ISSUES)
    assert (s["scope"], s["done"], s["open"], s["done_pct"]) == (3, 2, 1, 66.7)
    assert (s["open_blockers"], s["open_criticals"]) == (1, 0)


def test_empty_version_has_no_done_pct():
    v8 = next(v for v in VERSIONS if v["id"] == "v8")
    assert metrics.version_scope(v8, ISSUES)["done_pct"] is None


# ---------- RAG ----------

RAG_CFG = {"amber_done_pct": 80, "amber_days_to_release": 14, "rate_undated": False}


def scope(**kw):
    base = {"scope": 10, "done": 10, "open": 0, "done_pct": 100.0, "open_blockers": 0, "open_criticals": 0}
    return {**base, **kw}


def test_rag_red_on_open_blocker():
    assert metrics.rag(scope(open=1, open_blockers=1), date(2026, 12, 1), AS_OF, 0, RAG_CFG) == "Red"


def test_rag_red_when_date_passed_with_open_issues():
    assert metrics.rag(scope(open=1, done_pct=90.0), date(2026, 10, 1), AS_OF, 0, RAG_CFG) == "Red"


def test_rag_passed_date_with_nothing_open_is_not_red():
    assert metrics.rag(scope(), date(2026, 10, 1), AS_OF, 0, RAG_CFG) == "Green"


def test_rag_amber_on_open_critical():
    assert metrics.rag(scope(open=1, open_criticals=1), date(2026, 12, 1), AS_OF, 0, RAG_CFG) == "Amber"


def test_rag_amber_when_behind_close_to_release():
    near = date(2026, 10, 18)                                   # 14 days out
    assert metrics.rag(scope(open=3, done_pct=79.9), near, AS_OF, 0, RAG_CFG) == "Amber"
    assert metrics.rag(scope(open=2, done_pct=80.0), near, AS_OF, 0, RAG_CFG) == "Green"
    far = date(2026, 10, 19)                                    # 15 days out
    assert metrics.rag(scope(open=3, done_pct=50.0), far, AS_OF, 0, RAG_CFG) == "Green"


def test_rag_amber_on_slip_into_version():
    assert metrics.rag(scope(), date(2026, 12, 1), AS_OF, 1, RAG_CFG) == "Amber"


def test_rag_undated_strict_vs_rate_undated():
    blocked = scope(open=1, open_blockers=1)
    assert metrics.rag(blocked, None, AS_OF, 0, RAG_CFG) == "No date"
    assert metrics.rag(blocked, None, AS_OF, 0, {**RAG_CFG, "rate_undated": True}) == "Red"
    assert metrics.rag(scope(), None, AS_OF, 0, {**RAG_CFG, "rate_undated": True}) == "Green"


# ---------- readiness table ----------

def test_release_readiness_rows(cfg):
    r = metrics.release_readiness(VERSIONS, ISSUES, AS_OF, cfg)
    got = {row["name"]: row["rag"] for row in r["rows"]}
    assert got == {"Rel 0.9": "Red", "Rel 2.0": "Red", "Rel 3.0": "Amber", "Rel 1.1": "Released"}
    assert [row["name"] for row in r["rows"]] == ["Rel 0.9", "Rel 2.0", "Rel 3.0", "Rel 1.1"]
    assert r["complete_not_released"] == ["Rel 2.1"]
    assert r["empty_unreleased"] == ["Empty"]
    rel20 = next(row for row in r["rows"] if row["name"] == "Rel 2.0")
    assert rel20["days_to_release"] == 6


def test_readiness_only_latest_released_and_no_archived(cfg):
    names = [row["name"] for row in metrics.release_readiness(VERSIONS, ISSUES, AS_OF, cfg)["rows"]]
    assert "Rel 1.0" not in names and "Old" not in names


def test_readiness_strict_mode_leaves_undated_unrated(cfg):
    cfg["rag"]["rate_undated"] = False
    rows = metrics.release_readiness(VERSIONS, ISSUES, AS_OF, cfg)["rows"]
    assert next(r for r in rows if r["name"] == "Rel 3.0")["rag"] == "No date"


def test_release_plan_supplies_missing_date(cfg):
    cfg["release_plan"] = {"Rel 3.0": "2026-10-01"}            # passed, with an open issue
    rows = metrics.release_readiness(VERSIONS, ISSUES, AS_OF, cfg)["rows"]
    rel30 = next(r for r in rows if r["name"] == "Rel 3.0")
    assert (rel30["rag"], rel30["date_source"], rel30["days_to_release"]) == ("Red", "plan", -3)


def test_readiness_passes_slips_into_rag(cfg):
    v6 = copy.deepcopy(VERSIONS)
    rows = metrics.release_readiness(v6, ISSUES, AS_OF, cfg, slips_in={"v3": 2})["rows"]
    assert next(r for r in rows if r["name"] == "Rel 2.0")["slips_in"] == 2


# ---------- past-due ----------

def test_past_due_released_and_past_date():
    rows = metrics.past_due(VERSIONS, ISSUES, AS_OF)
    assert [(r["key"], r["reason"]) for r in rows] == [
        ("SPEC-3", "released"), ("SPEC-6", "past release date")]


# ---------- escalation rules ----------

def test_escalations(cfg):
    rows = escalation.evaluate(ISSUES, VERSIONS, AS_OF, cfg["escalation"])
    assert {r["key"]: r["rules"] for r in rows} == {
        "SPEC-1": ["E1"], "SPEC-2": ["E2", "E4"], "SPEC-3": ["E3"]}
    assert keys(rows) == ["SPEC-1", "SPEC-2", "SPEC-3"]
    assert rows[2]["released_versions"] == ["Rel 1.1"]


def test_e1_thresholds_are_strict(cfg):
    rows = {r["key"] for r in escalation.evaluate(ISSUES, VERSIONS, AS_OF, cfg["escalation"])}
    assert "SPEC-9" not in rows       # age 4, not > 7
    assert "SPEC-10" not in rows      # age 8 but idle 3, not > 3


def test_e2_needs_age_over_14(cfg):
    rows = {r["key"] for r in escalation.evaluate(ISSUES, VERSIONS, AS_OF, cfg["escalation"])}
    assert "SPEC-8" not in rows       # critical, age 6


def test_escalation_thresholds_come_from_config(cfg):
    cfg["escalation"]["E1"]["min_age_days"] = 1
    cfg["escalation"]["E1"]["min_idle_days"] = 1
    rows = {r["key"] for r in escalation.evaluate(ISSUES, VERSIONS, AS_OF, cfg["escalation"])}
    assert {"SPEC-9", "SPEC-10"} <= rows


def test_rules_can_be_disabled(cfg):
    cfg["escalation"]["E3"]["enabled"] = False
    rows = {r["key"] for r in escalation.evaluate(ISSUES, VERSIONS, AS_OF, cfg["escalation"])}
    assert "SPEC-3" not in rows


# ---------- parsing a real API page ----------

def test_real_api_page_parses():
    page = json.loads((FIXTURES / "real_search_page.json").read_text())
    rows = [store.flatten_issue(i) for i in page["issues"]]
    assert len(rows) == 15
    for r in rows:
        assert r["key"].startswith("SPEC-")
        assert r["status_category"] in {"To Do", "In Progress"}
        assert metrics.to_date(r["created"]) is not None


# ---------- end to end over the fixture ----------

def test_analyze_and_console_summary(cfg):
    a = analyze(ISSUES, VERSIONS, AS_OF, cfg, 30)
    assert a["open_total"] == 7
    text = console_summary(a)
    assert "Rel 2.0" in text and "SPEC-1" in text


# ---------- trend ----------

def test_open_trend_rebuilds_open_counts():
    rows = metrics.open_trend(ISSUES, AS_OF, weeks=3)
    assert [r["week_ending"] for r in rows] == ["2026-09-20", "2026-09-27", "2026-10-04"]
    # 2026-10-04: all 7 open issues; SPEC-1, 2, 8, 9, 10 are Blocker/Critical
    assert rows[-1] == {"week_ending": "2026-10-04", "open": 7, "open_blocker_critical": 5}
    # 2026-09-20: SPEC-4 resolved that day (not open); SPEC-3/8/9/10 not created yet
    assert rows[0]["open"] == 3 and rows[0]["open_blocker_critical"] == 2


# ---------- verdict ----------

def test_verdict_leads_with_worst_release_and_its_blockers(cfg):
    from ivi_tracker.analysis import verdict
    v = verdict(analyze(ISSUES, VERSIONS, AS_OF, cfg, 30))
    # Rel 0.9 and Rel 2.0 are both Red; readiness sorts Red by date, so Rel 0.9 comes first
    assert v["worst"]["name"] == "Rel 0.9" and v["worst"]["rag"] == "Red"
    assert [r["name"] for r in v["others"]] == ["Rel 2.0", "Rel 3.0"]
    assert v["culprits"] == []                      # Rel 0.9 is Red for its passed date, not a blocker
    assert (v["escalations"], v["open_blocker_critical"], v["unowned_blocker_critical"]) == (3, 5, 1)


def test_verdict_names_the_blocking_issue(cfg):
    from ivi_tracker.analysis import verdict
    versions = [v for v in VERSIONS if v["id"] != "v5"]          # drop Rel 0.9
    v = verdict(analyze(ISSUES, versions, AS_OF, cfg, 30))
    assert v["worst"]["name"] == "Rel 2.0"
    assert [c["key"] for c in v["culprits"]] == ["SPEC-1"]


# ---------- agenda order and escalation ranking ----------

def test_issues_carry_their_release_and_risk(cfg):
    a = analyze(ISSUES, VERSIONS, AS_OF, cfg, 30)
    rows = {b["key"]: b for b in a["blockers"]}
    assert rows["SPEC-1"]["releases"] == ["Rel 2.0"] and rows["SPEC-1"]["at_risk"]      # Rel 2.0 is Red
    assert rows["SPEC-8"]["releases"] == ["Rel 3.0"] and rows["SPEC-8"]["at_risk"]      # Rel 3.0 is Amber
    assert rows["SPEC-9"]["releases"] == [] and not rows["SPEC-9"]["at_risk"]
    assert a["open_unscoped"] == 3


def test_agenda_puts_at_risk_releases_first(cfg):
    a = analyze(ISSUES, VERSIONS, AS_OF, cfg, 30)
    # at-risk: SPEC-1 (Blocker), SPEC-8 (Critical); then the rest by priority, then idle
    assert [b["key"] for b in a["agenda"]] == ["SPEC-1", "SPEC-8", "SPEC-10", "SPEC-9", "SPEC-2"]


def test_chase_first_ranks_at_risk_then_unowned(cfg):
    from ivi_tracker.analysis import chase_first
    a = analyze(ISSUES, VERSIONS, AS_OF, cfg, 30)
    assert [e["key"] for e in chase_first(a)] == ["SPEC-1", "SPEC-2", "SPEC-3"]
    reasons = {e["key"]: e["reason"] for e in a["escalations"]}
    assert reasons == {"SPEC-1": "Blocker stalled", "SPEC-2": "Critical stalled, no owner",
                       "SPEC-3": "Open in a released version"}


def test_escalations_are_listed_in_chase_order_and_flag_saturation(cfg):
    from ivi_tracker.analysis import verdict
    a = analyze(ISSUES, VERSIONS, AS_OF, cfg, 30)
    assert [e["key"] for e in a["escalations"]] == ["SPEC-1", "SPEC-2", "SPEC-3"]
    assert verdict(a)["saturated"] is False            # 3 escalations vs 5 blockers/criticals: not over half
    cfg["escalation"]["E1"].update(min_age_days=0, min_idle_days=0)
    assert verdict(analyze(ISSUES, VERSIONS, AS_OF, cfg, 30))["saturated"] is True


def test_branches_are_labelled(cfg):
    cfg["readiness"]["branch_names"] = ["Rel 3.0"]
    rows = analyze(ISSUES, VERSIONS, AS_OF, cfg, 30)["readiness"]["rows"]
    assert {r["name"]: r["is_branch"] for r in rows}["Rel 3.0"] is True
