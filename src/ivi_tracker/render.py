"""Jinja2 rendering of the three artifacts into reports/YYYY-MM-DD/."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .jira_client import DEFAULT_BASE_URL
from .llm import Drafts

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = ROOT / "templates"
REPORTS_DIR = ROOT / "reports"

RAG_ICON = {"Red": "🔴 Red", "Amber": "🟠 Amber", "Green": "🟢 Green", "No date": "No date", "Released": "Released"}
HYGIENE_LABEL = {
    "no_fix_version": "No fix version", "no_component": "No component",
    "no_assignee": "No assignee", "stale": "Stale (idle > 30 days)",
}


def cell(text: str) -> str:
    """Make free text safe inside a markdown table cell."""
    return " ".join(str(text).split()).replace("|", "\\|")


def link(key: str) -> str:
    base = (os.getenv("JIRA_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    return f"[{key}]({base}/browse/{key})"


ISSUE_KEY = re.compile(r"(?<![\w\[/-])([A-Z][A-Z0-9]+-\d+)(?![\w\]])")


def linkify(text: str) -> str:
    """Turn bare issue keys in drafted narrative into Jira links (keys already inside links are left alone)."""
    return ISSUE_KEY.sub(lambda m: link(m.group(1)), text)


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def examples(keys: list[str], n: int = 5) -> str:
    shown = ", ".join(link(k) for k in keys[:n])
    return shown + (f" (+{len(keys) - n} more)" if len(keys) > n else "")


def environment(stale_days: int) -> Environment:
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), undefined=StrictUndefined,
                      trim_blocks=False, lstrip_blocks=True, keep_trailing_newline=True)
    env.globals.update(cell=cell, link=link, linkify=linkify, narrative=narrative, pct=pct, examples=examples, rag=RAG_ICON.get,
                       hygiene_label=lambda c: HYGIENE_LABEL.get(c, c).replace("30", str(stale_days)))
    return env


ARTIFACTS = ("weekly_status", "bug_review", "escalations")


def render_strings(a: dict, drafts: Drafts, cfg: dict) -> dict[str, str]:
    """Render the three artifacts to markdown strings, keyed by artifact name."""
    env = environment(cfg["stale_days"])
    ctx = {"a": a, "d": drafts, "reviewer": cfg.get("reviewer", "<reviewer>"),
           "items": a["agenda"][: cfg["agenda_size"]]}
    return {name: _tidy(env.get_template(f"{name}.md.j2").render(**ctx)) for name in ARTIFACTS}


def render_all(a: dict, drafts: Drafts, facts: dict, cfg: dict, out_root: Path = REPORTS_DIR) -> Path:
    """Write the three artifacts plus facts.json and drafts.json. Returns the output dir."""
    out = out_root / a["as_of"]
    out.mkdir(parents=True, exist_ok=True)
    for name, text in render_strings(a, drafts, cfg).items():
        (out / f"{name}.md").write_text(text)
    # The exact facts the LLM saw: lets a reader audit every number in the narrative.
    (out / "facts.json").write_text(json.dumps(facts, indent=1) + "\n")
    # The narrative itself, so the web app can reuse it when its settings match this run.
    (out / "drafts.json").write_text(json.dumps(drafts_to_dict(drafts), indent=1) + "\n")
    return out


def drafts_to_dict(d: Drafts) -> dict:
    return {"status_sections": d.status_sections, "agenda_questions": d.agenda_questions,
            "escalation_notes": d.escalation_notes, "tokens": d.tokens}


def drafts_from_dict(data: dict) -> Drafts:
    return Drafts(data["status_sections"], data["agenda_questions"], data["escalation_notes"],
                  data.get("tokens", 0))


NOT_DRAFTED = "_Narrative not drafted for these settings._"


def narrative(text: str) -> str:
    """A drafted section with issue keys linked, or a plain note when it wasn't drafted."""
    return linkify(text) if text else NOT_DRAFTED


def _tidy(text: str) -> str:
    """Collapse runs of blank lines left by template control blocks."""
    lines, blank = [], 0
    for line in text.rstrip().splitlines():
        blank = blank + 1 if not line.strip() else 0
        if blank <= 1:
            lines.append(line.rstrip())
    return "\n".join(lines) + "\n"
