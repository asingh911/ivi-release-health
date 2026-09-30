"""Escalation rule engine, driven by config.yaml `escalation`.

E1/E2: priority open longer than min_age_days AND idle longer than min_idle_days.
E3:    any open issue in a released version.
E4:    open issue at an escalated priority with no assignee.
One row per issue; an issue that trips several rules lists them all.
"""

from __future__ import annotations

from datetime import date

from .metrics import PRIORITY_RANK, age_days, idle_days, is_open

RULE_TEXT = {
    "E1": "{priority} open > {min_age_days}d and idle > {min_idle_days}d",
    "E2": "{priority} open > {min_age_days}d and idle > {min_idle_days}d",
    "E3": "Open issue in a released version",
    "E4": "Open {priorities} with no assignee",
}


# Plain-language names for the rules, shown in place of the E1-E4 codes.
RULE_SHORT = {
    "E1": "{priority} stalled",
    "E2": "{priority} stalled",
    "E3": "Open in a released version",
    "E4": "No owner",
}


def rule_labels(rule_ids: list[str], esc_cfg: dict) -> str:
    """'Blocker stalled, no owner': sentence case, so joined labels read as one phrase."""
    labels = [RULE_SHORT[r].format(priority=esc_cfg.get(r, {}).get("priority", "")) for r in rule_ids]
    return ", ".join([labels[0]] + [l[0].lower() + l[1:] for l in labels[1:]]) if labels else ""


def _aging_rule(rule: dict, issue: dict, as_of: date) -> bool:
    return (issue["priority"] == rule["priority"]
            and age_days(issue, as_of) > rule["min_age_days"]
            and idle_days(issue, as_of) > rule["min_idle_days"])


def rule_descriptions(esc_cfg: dict) -> dict[str, str]:
    out = {}
    for rule_id, template in RULE_TEXT.items():
        rule = esc_cfg.get(rule_id)
        if rule and rule.get("enabled", True):
            params = {**rule, "priorities": "/".join(rule.get("priorities", []))}
            out[rule_id] = template.format(**params)
    return out


def evaluate(issues: list[dict], versions: list[dict], as_of: date, esc_cfg: dict) -> list[dict]:
    released_ids = {v["id"] for v in versions if v["released"]}
    names = {v["id"]: v["name"] for v in versions}
    active = rule_descriptions(esc_cfg)
    rows = []

    for i in issues:
        if not is_open(i):
            continue
        hits = []
        for rule_id in ("E1", "E2"):
            if rule_id in active and _aging_rule(esc_cfg[rule_id], i, as_of):
                hits.append(rule_id)
        released_hit = [names[v] for v in i["fix_versions"] if v in released_ids]
        if "E3" in active and released_hit:
            hits.append("E3")
        if "E4" in active and not i["assignee_id"] and i["priority"] in esc_cfg["E4"]["priorities"]:
            hits.append("E4")
        if hits:
            rows.append({
                "key": i["key"], "summary": i["summary"], "priority": i["priority"],
                "component": ", ".join(i["components"]) or "none",
                "rules": hits, "rule": ", ".join(hits), "reason": rule_labels(hits, esc_cfg),
                "age_days": age_days(i, as_of), "idle_days": idle_days(i, as_of),
                "released_versions": released_hit, "assigned": bool(i["assignee_id"]),
            })

    return sorted(rows, key=lambda r: (-PRIORITY_RANK.get(r["priority"], 0), -r["idle_days"], r["key"]))
