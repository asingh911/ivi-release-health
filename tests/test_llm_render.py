"""LLM and rendering tests: facts JSON, number guard, retry-then-fail, and markdown rendering (no network)."""

import json
from datetime import date
from pathlib import Path

import pytest

from ivi_tracker import llm, render
from ivi_tracker.analysis import analyze
from ivi_tracker.config import load_config

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "sample_issues.json").read_text())
AS_OF = date.fromisoformat(SAMPLE["as_of"])

GOOD_STATUS = """## Summary
Rel 0.9 and Rel 2.0 are Red; SPEC-1 is the biggest risk, idle 10 days.
## Risks & slips
- SPEC-3 is open in released version Rel 1.1.
## Asks / decisions needed
- Confirm an owner for SPEC-2.
## Next steps
- Review the 5 open blockers and criticals at triage."""


@pytest.fixture
def cfg():
    c = load_config()
    c["release_plan"], c["rag"]["rate_undated"] = {}, True
    return c


@pytest.fixture
def analysis(cfg):
    return analyze(SAMPLE["issues"], SAMPLE["versions"], AS_OF, cfg, 30)


@pytest.fixture
def facts(analysis, cfg):
    return llm.build_facts(analysis, cfg)


def fake_llm(responses):
    """Returns a CompleteFn that replays canned responses and records the prompts it saw."""
    calls = []

    def complete(messages, json_mode):
        calls.append(messages)
        return responses.pop(0), 100

    complete.calls = calls
    return complete


def mapping(keys, text="Confirm severity and assign an owner."):
    return json.dumps({k: text for k in keys})


# ---------- number guard ----------

def test_number_guard_rejects_invented_number():
    facts = {"open_total": 156, "hygiene": {"stale": 88}}
    assert llm.number_guard("There are 157 open issues.", facts) == ["157"]


def test_number_guard_passes_numbers_from_facts():
    facts = {"open_total": 156, "releases": [{"name": "Unagi 21.0.2", "done_pct": 72.4}]}
    assert llm.number_guard("156 open; Unagi 21.0.2 is 72.4% done.", facts) == []


def test_number_guard_ignores_issue_keys_and_iso_dates():
    assert llm.number_guard("SPEC-5719 was updated 2026-09-29.", {"x": 1}) == []


# ---------- facts ----------

def test_facts_are_compact_and_name_free(facts, cfg):
    assert facts["open_total"] == 7
    assert facts["hygiene"] == {"no_fix_version": 3, "no_component": 1, "no_assignee": 1, "stale": 1}
    assert len(facts["agenda_items"]) == min(cfg["agenda_size"], 5)
    assert all(len(b["summary"]) <= cfg["llm"]["max_summary_chars"] for b in facts["blockers"])
    assert "assignee_id" not in json.dumps(facts) and "\"a1\"" not in json.dumps(facts)
    assert facts["escalations"][0]["rule_text"].startswith("Blocker open > 7d")


def test_truncate_summary():
    assert llm._truncate("a" * 200, 120) == "a" * 119 + "…"
    assert llm._truncate("  short\n text ", 120) == "short text"


# ---------- drafting ----------

def test_draft_all_happy_path(facts):
    complete = fake_llm([GOOD_STATUS,
                         mapping([i["key"] for i in facts["agenda_items"]]),
                         mapping([e["key"] for e in facts["escalations"]], "Rule E1 tripped; name an owner.")])
    d = llm.draft_all(facts, complete)
    assert list(d.status_sections) == llm.STATUS_SECTIONS
    assert "SPEC-1" in d.status_sections["Summary"]
    assert set(d.agenda_questions) == {i["key"] for i in facts["agenda_items"]}
    assert d.tokens == 300 and d.retries == []
    assert len(complete.calls) == 3


def test_invented_number_triggers_one_retry(facts):
    bad = GOOD_STATUS.replace("idle 10 days", "idle 999 days")
    complete = fake_llm([bad, GOOD_STATUS,
                         mapping([i["key"] for i in facts["agenda_items"]]),
                         mapping([e["key"] for e in facts["escalations"]])])
    d = llm.draft_all(facts, complete)
    assert len(d.retries) == 1 and "999" in d.retries[0]
    retry_prompt = complete.calls[1][-1]["content"]
    assert "999" in retry_prompt and "failed validation" in retry_prompt


def test_guard_fails_loudly_after_second_bad_answer(facts):
    bad = GOOD_STATUS.replace("idle 10 days", "idle 999 days")
    with pytest.raises(llm.LLMGuardError, match="999"):
        llm.draft_all(facts, fake_llm([bad, bad]))


def test_missing_section_is_rejected(facts):
    no_asks = GOOD_STATUS.replace("## Asks / decisions needed\n- Confirm an owner for SPEC-2.\n", "")
    with pytest.raises(llm.LLMGuardError, match="Asks"):
        llm.draft_all(facts, fake_llm([no_asks, no_asks]))


def test_agenda_must_cover_every_key(facts):
    keys = [i["key"] for i in facts["agenda_items"]]
    partial = mapping(keys[:-1])
    with pytest.raises(llm.LLMGuardError, match="missing key"):
        llm.draft_all(facts, fake_llm([GOOD_STATUS, partial, partial]))


def test_agenda_invented_number_rejected(facts):
    keys = [i["key"] for i in facts["agenda_items"]]
    bad = mapping(keys, "Reproduce on release 42?")
    with pytest.raises(llm.LLMGuardError, match="42"):
        llm.draft_all(facts, fake_llm([GOOD_STATUS, bad, bad]))


# ---------- rendering ----------

def test_render_writes_three_artifacts(analysis, facts, cfg, tmp_path):
    drafts = llm.draft_all(facts, fake_llm([
        GOOD_STATUS,
        mapping([i["key"] for i in facts["agenda_items"]], "Is this | still reproducible?"),
        mapping([e["key"] for e in facts["escalations"]], "Name an owner this week."),
    ]))
    out = render.render_all(analysis, drafts, facts, cfg, out_root=tmp_path)
    assert out == tmp_path / "2026-10-04"
    status = (out / "weekly_status.md").read_text()
    assert status.startswith("# AGL Release Health: Weekly Status (as of 2026-10-04, window 30 days)")
    headings = [line for line in status.splitlines() if line.startswith("## ")]
    assert headings == ["## Summary", "## Release readiness", "## Blockers & criticals", "## Risks & slips",
                        "## Backlog hygiene", "## Asks / decisions needed", "## This period", "## Next steps"]
    assert "| Rel 2.0 | 2026-10-10 | 3 | 66.7% | 1/0 | 🔴 Red |" in status
    assert "[SPEC-1](https://lf-automotivelinux.atlassian.net/browse/SPEC-1)" in status
    assert "Draft generated by ivi-release-health; reviewed by" in status

    agenda = (out / "bug_review.md").read_text()
    assert "Is this \\| still reproducible?" in agenda          # pipes escaped inside table cells
    esc = (out / "escalations.md").read_text()
    assert "| E2, E4 |" in esc and "Name an owner this week." in esc
    assert json.loads((out / "facts.json").read_text()) == facts


def test_render_with_placeholders(analysis, facts, cfg, tmp_path):
    out = render.render_all(analysis, llm.placeholder_drafts(facts), facts, cfg, out_root=tmp_path)
    assert "_Not drafted" in (out / "weekly_status.md").read_text()


def test_tables_have_no_blank_lines_inside(analysis, facts, cfg, tmp_path):
    out = render.render_all(analysis, llm.placeholder_drafts(facts), facts, cfg, out_root=tmp_path)
    for name in ("weekly_status.md", "bug_review.md", "escalations.md"):
        lines = (out / name).read_text().splitlines()
        for prev, line, nxt in zip(lines, lines[1:], lines[2:]):
            if prev.startswith("|") and nxt.startswith("|"):
                assert line.startswith("|"), f"table broken in {name}"
