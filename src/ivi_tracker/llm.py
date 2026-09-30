"""LLM drafting: facts JSON in, narrative out, every number checked by the number guard.

Three short calls per run: weekly status narrative, bug-review questions, escalation notes.
Code computes every number; the model only writes prose around the facts it is given.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

# A completion function takes (messages, json_mode) and returns (text, total_tokens).
CompleteFn = Callable[[list[dict], bool], tuple[str, int]]

STATUS_SECTIONS = ["Summary", "Risks & slips", "Asks / decisions needed", "Next steps"]

STATUS_SYSTEM = """You draft a weekly program status for an in-vehicle infotainment (IVI) software program.
Audience: engineering leads and the customer's program manager. Be plain and direct.
Rules: use only the facts provided; every number you write must appear in the facts JSON;
refer to issues by key (e.g., SPEC-1234); do not invent causes, owners, or dates; say "unknown" when unsure.
Release names contain numbers (e.g., "Unagi 21.0.2"); write them exactly as given."""

STATUS_USER = """Facts JSON: {facts}
Write markdown with exactly these sections:
## Summary (2-3 sentences; lead with the worst release RAG and the single biggest risk)
## Risks & slips (bullets; cite issue keys)
## Asks / decisions needed (bullets; each is a specific request)
## Next steps (bullets)
Do not write tables; tables are inserted separately."""

AGENDA_SYSTEM = """You prepare the agenda for a recurring IVI bug-triage meeting.
Use only the facts provided; every number you write must appear in the facts JSON. Do not invent owners or causes."""

# Shared by the agenda and escalation prompts so each item gets an ask that fits it.
TRIAGE_GUIDE = """Pick the action that fits each item; the items must not all read the same:
- idle for many months: propose closing as stale or downgrading;
- release-preparation, documentation, or admin tasks: ask for a target date or whether it really blocks a release;
- crashes and build/CI failures: ask whether it reproduces on the current release;
- "assigned" is false: ask who will own it. Never ask for an owner when "assigned" is true.
  "assigned" is today's state only: never say how long an item has lacked an owner.
Name the subject in a few words (e.g. "the RCar GPU crash") but don't copy the whole summary."""

AGENDA_USER = """Facts JSON: {facts}
For each item in "agenda_items", write one question for the triage meeting that would move it forward.
""" + TRIAGE_GUIDE + """
At most 25 words each.
Return a JSON object mapping each issue key to its one-line question, e.g. {{"SPEC-1234": "..."}}."""

ESCALATION_SYSTEM = """You write escalation notes for an IVI program's escalation tracker.
Use only the facts provided; every number you write must appear in the facts JSON. Do not invent owners, causes, or dates."""

ESCALATION_USER = """Facts JSON: {facts}
For each item in "escalations", write one sentence stating which rule it triggered and the specific ask
(owner, decision, or date).
- Say the rule in plain English with this item's own numbers, e.g. "Blocker idle 231 days with no progress" or
  "Critical with no owner"; don't quote "rule_text" or use symbols like ">".
- Then state the ask. If "assigned" is true, the ask goes to the current owner: a fix date, or a decision
  to downgrade, defer, or close.
""" + TRIAGE_GUIDE + """
At most 30 words each. Don't start with the issue key; it is already in the table.
Return a JSON object mapping each issue key to its sentence, e.g. {{"SPEC-1234": "..."}}."""


class LLMGuardError(RuntimeError):
    """The model produced unverifiable output twice in a row."""


# ---------- number guard ----------

NUMBER = r"\d+(?:\.\d+)?"


def number_guard(text: str, facts: dict) -> list[str]:
    """Return numbers in `text` that don't appear in `facts`. Empty list = pass."""
    allowed = set(re.findall(NUMBER, json.dumps(facts)))
    cleaned = re.sub(r"SPEC-\d+|\d{4}-\d{2}-\d{2}", "", text)   # ignore issue keys and ISO dates
    return [n for n in re.findall(NUMBER, cleaned) if n not in allowed]


# ---------- facts payload ----------

def _truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def build_facts(a: dict, cfg: dict) -> dict:
    """Compact, name-free facts JSON: the only thing the LLM sees."""
    limit = cfg["llm"]["max_summary_chars"]
    agenda_size = cfg["agenda_size"]
    r = a["readiness"]

    def issue_facts(row: dict) -> dict:
        return {"key": row["key"], "summary": _truncate(row["summary"], limit), "component": row["component"],
                "priority": row["priority"], "age_days": row["age_days"], "idle_days": row["idle_days"],
                "assigned": row["assigned"]}

    return {
        "as_of": a["as_of"],
        "window_days": a["window_days"],
        "open_total": a["open_total"],
        "new_in_window": a["flow"]["new"],
        "resolved_in_window": a["flow"]["resolved"],
        "net_in_window": a["flow"]["net"],
        "hygiene": {check: len(keys) for check, keys in a["hygiene"].items()},
        "releases": [
            {"name": row["name"], "release_date": row["release_date"], "rag": row["rag"],
             "rag_basis": row.get("rag_basis", "released"), "scope": row["scope"], "open": row["open"],
             "done_pct": row["done_pct"], "open_blockers": row["open_blockers"],
             "open_criticals": row["open_criticals"], "days_to_release": row["days_to_release"],
             "slips_in": row["slips_in"], "open_keys": row["open_keys"]}
            for row in r["rows"]
        ],
        "versions_complete_not_released": len(r["complete_not_released"]),
        "open_blockers_criticals": len(a["blockers"]),
        "blockers": [issue_facts(b) for b in a["blockers"]],
        "past_due": a["past_due"],
        "escalation_count": len(a["escalations"]),
        "escalations": [
            {**issue_facts(e), "rule": e["rule"],
             "rule_text": "; ".join(a["escalation_rules"][rid] for rid in e["rules"])}
            for e in a["escalations"]
        ],
        "agenda_items": [issue_facts(b) for b in a["blockers"][:agenda_size]],
    }


# ---------- OpenAI client ----------

def openai_complete(cfg: dict) -> CompleteFn:
    from openai import BadRequestError, OpenAI

    client = OpenAI()
    llm_cfg = cfg["llm"]
    state = {"temperature": llm_cfg.get("temperature")}

    def complete(messages: list[dict], json_mode: bool) -> tuple[str, int]:
        kwargs = {"model": llm_cfg["model"], "messages": messages}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if state["temperature"] is not None:
            kwargs["temperature"] = state["temperature"]
        try:
            resp = client.chat.completions.create(**kwargs)
        except BadRequestError as exc:
            # Some reasoning models only accept the default temperature; drop it once and retry.
            if "temperature" not in str(exc) or "temperature" not in kwargs:
                raise
            state["temperature"] = None
            kwargs.pop("temperature")
            resp = client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or "", resp.usage.total_tokens if resp.usage else 0

    return complete


# ---------- drafting ----------

@dataclass
class Drafts:
    status_sections: dict[str, str]
    agenda_questions: dict[str, str]
    escalation_notes: dict[str, str]
    tokens: int = 0
    retries: list[str] = field(default_factory=list)


def split_sections(markdown: str) -> dict[str, str]:
    """Split '## Heading' markdown into {heading: body}."""
    sections, current = {}, None
    for line in markdown.splitlines():
        m = re.match(r"^#{1,3}\s+(.*?)\s*$", line)
        if m:
            current = m.group(1)
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def _problems_status(text: str, facts: dict) -> list[str]:
    sections = split_sections(text)
    missing = [s for s in STATUS_SECTIONS if not sections.get(s)]
    problems = [f"missing section '## {s}'" for s in missing]
    bad = number_guard(text, facts)
    if bad:
        problems.append(f"numbers not in facts: {sorted(set(bad))}")
    return problems


def _problems_mapping(text: str, facts: dict, expected: list[str]) -> list[str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return ["response was not valid JSON"]
    if not isinstance(data, dict):
        return ["response was not a JSON object"]
    problems = [f"missing key {k}" for k in expected if not str(data.get(k, "")).strip()]
    bad = number_guard(" ".join(str(data.get(k, "")) for k in expected), facts)
    if bad:
        problems.append(f"numbers not in facts: {sorted(set(bad))}")
    return problems


def _draft(complete: CompleteFn, name: str, system: str, user: str, validate, drafts: Drafts) -> str:
    """One call, validated; on failure retry once with the problems stated, then fail loudly."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    json_mode = name != "status"
    for attempt in (1, 2):
        text, tokens = complete(messages, json_mode)
        drafts.tokens += tokens
        problems = validate(text)
        if not problems:
            return text
        if attempt == 1:
            drafts.retries.append(f"{name}: {'; '.join(problems)}")
            messages += [
                {"role": "assistant", "content": text},
                {"role": "user", "content": "Your answer failed validation: " + "; ".join(problems)
                 + ". Rewrite it. Use only numbers that appear in the facts JSON."},
            ]
    raise LLMGuardError(f"{name} draft failed validation twice: {'; '.join(problems)}")


def draft_all(facts: dict, complete: CompleteFn) -> Drafts:
    drafts = Drafts({}, {}, {})
    facts_json = json.dumps(facts, separators=(",", ":"))

    status = _draft(complete, "status", STATUS_SYSTEM, STATUS_USER.format(facts=facts_json),
                    lambda t: _problems_status(t, facts), drafts)
    drafts.status_sections = {s: split_sections(status)[s] for s in STATUS_SECTIONS}

    agenda_keys = [i["key"] for i in facts["agenda_items"]]
    if agenda_keys:
        agenda_facts = {k: facts[k] for k in ("as_of", "window_days", "agenda_items")}
        text = _draft(complete, "agenda", AGENDA_SYSTEM,
                      AGENDA_USER.format(facts=json.dumps(agenda_facts, separators=(",", ":"))),
                      lambda t: _problems_mapping(t, agenda_facts, agenda_keys), drafts)
        drafts.agenda_questions = {k: json.loads(text)[k].strip() for k in agenda_keys}

    esc_keys = [e["key"] for e in facts["escalations"]]
    if esc_keys:
        esc_facts = {k: facts[k] for k in ("as_of", "window_days", "escalations")}
        text = _draft(complete, "escalations", ESCALATION_SYSTEM,
                      ESCALATION_USER.format(facts=json.dumps(esc_facts, separators=(",", ":"))),
                      lambda t: _problems_mapping(t, esc_facts, esc_keys), drafts)
        drafts.escalation_notes = {k: json.loads(text)[k].strip() for k in esc_keys}

    return drafts


def placeholder_drafts(facts: dict) -> Drafts:
    """Used with --no-llm: tables render, narrative is clearly marked as not drafted."""
    note = "_Not drafted: run without `--no-llm` to generate this section._"
    return Drafts(
        status_sections={s: note for s in STATUS_SECTIONS},
        agenda_questions={i["key"]: "_not drafted_" for i in facts["agenda_items"]},
        escalation_notes={e["key"]: "_not drafted_" for e in facts["escalations"]},
    )
