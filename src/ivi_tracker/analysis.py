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
