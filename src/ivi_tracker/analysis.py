"""Runs every metric and rule over the cache and bundles the results (the basis of the facts JSON)."""

from __future__ import annotations

from datetime import date

from . import escalation, metrics


def analyze(issues: list[dict], versions: list[dict], as_of: date, cfg: dict, window_days: int) -> dict:
    open_issues = [i for i in issues if metrics.is_open(i)]
    return {
        "as_of": as_of.isoformat(),
        "window_days": window_days,
        "issues_total": len(issues),
        "open_total": len(open_issues),
        "flow": metrics.flow(issues, as_of, window_days),
        "hygiene": metrics.hygiene(issues, as_of, cfg["stale_days"]),
        "blockers": metrics.blocker_view(issues, as_of),
        "readiness": metrics.release_readiness(versions, issues, as_of, cfg),
        "past_due": metrics.past_due(versions, issues, as_of, cfg.get("release_plan")),
        "escalations": escalation.evaluate(issues, versions, as_of, cfg["escalation"]),
        "escalation_rules": escalation.rule_descriptions(cfg["escalation"]),
    }


def _keys(keys: list[str], n: int = 5) -> str:
    more = f" (+{len(keys) - n} more)" if len(keys) > n else ""
    return ", ".join(keys[:n]) + more


def console_summary(a: dict) -> str:
    """Plain-text summary printed to the terminal by `report`."""
    f, h, r = a["flow"], a["hygiene"], a["readiness"]
    lines = [
        f"AGL release health as of {a['as_of']} (window {a['window_days']}d)",
        f"  Issues: {a['issues_total']} total, {a['open_total']} open | "
        f"new {f['new']}, resolved {f['resolved']}, net {f['net']:+d}",
        "",
        "Release readiness:",
    ]
    for row in r["rows"]:
        date_txt = row["release_date"] or "no date"
        pct = f"{row['done_pct']:.1f}%" if row["done_pct"] is not None else "n/a"
        lines.append(f"  [{row['rag']:<8}] {row['name']:<18} {date_txt:<10} scope {row['scope']:>3}, "
                     f"done {pct:>6}, open B/C {row['open_blockers']}/{row['open_criticals']}")
    lines.append(f"  Complete but not marked released: {len(r['complete_not_released'])} versions")
    lines += ["", f"Open blockers & criticals: {len(a['blockers'])}"]
    for b in a["blockers"][:10]:
        lines.append(f"  {b['key']:<10} {b['priority']:<8} {b['component'][:18]:<18} "
                     f"age {b['age_days']:>4}d ({b['age_bucket']}), idle {b['idle_days']:>4}d  {b['summary'][:50]}")
    lines += ["", "Backlog hygiene (open issues):"]
    for check, keys in h.items():
        lines.append(f"  {check:<15} {len(keys):>4}  {_keys(keys)}")
    lines += ["", f"Past-due / open in released versions: {len(a['past_due'])}"]
    for p in a["past_due"]:
        lines.append(f"  {p['key']:<10} {p['version']} ({p['reason']})")
    lines += ["", f"Escalations: {len(a['escalations'])}"]
    for e in a["escalations"]:
        lines.append(f"  {e['key']:<10} {e['rule']:<10} {e['priority']:<8} "
                     f"open {e['age_days']:>4}d, idle {e['idle_days']:>4}d")
    return "\n".join(lines)


RAG_SEVERITY = {"Red": 0, "Amber": 1, "Green": 2, "No date": 3, "Released": 4}


def verdict(a: dict) -> dict:
    """The at-a-glance answer: the worst unreleased version, what blocks it, and what needs escalating.

    Everything here is counted from the analysis; nothing is drafted.
    """
    active = [r for r in a["readiness"]["rows"] if r["rag"] != "Released"]
    worst = min(active, key=lambda r: RAG_SEVERITY[r["rag"]], default=None)
    blockers_by_key = {b["key"]: b for b in a["blockers"]}
    culprits = [blockers_by_key[k] for k in (worst["open_keys"] if worst else []) if k in blockers_by_key]
    unowned = [b for b in a["blockers"] if not b["assigned"]]
    return {
        "worst": worst,
        "culprits": culprits,
        "others": [r for r in active if r is not worst],
        "escalations": len(a["escalations"]),
        "open_blocker_critical": len(a["blockers"]),
        "unowned_blocker_critical": len(unowned),
    }
