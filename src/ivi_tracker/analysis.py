"""Runs every metric and rule over the cache and bundles the results (the basis of the facts JSON)."""

from __future__ import annotations

from datetime import date

from . import escalation, metrics


AT_RISK = ("Red", "Amber")


def analyze(issues: list[dict], versions: list[dict], as_of: date, cfg: dict, window_days: int) -> dict:
    open_issues = [i for i in issues if metrics.is_open(i)]
    readiness = metrics.release_readiness(versions, issues, as_of, cfg)
    branches = set(cfg.get("readiness", {}).get("branch_names", []))
    for row in readiness["rows"]:
        row["is_branch"] = row["name"] in branches
    at_risk = {r["name"] for r in readiness["rows"] if r["rag"] in AT_RISK}
    names = {v["id"]: v["name"] for v in versions}
    releases_of = {i["key"]: sorted(names[v] for v in i["fix_versions"] if v in names) for i in open_issues}

    def with_release(row: dict) -> dict:
        releases = releases_of.get(row["key"], [])
        return {**row, "releases": releases, "at_risk": bool(at_risk & set(releases))}

    blockers = [with_release(b) for b in metrics.blocker_view(issues, as_of)]
    escalations = sorted((with_release(e) for e in escalation.evaluate(issues, versions, as_of, cfg["escalation"])),
                         key=chase_order)
    return {
        "as_of": as_of.isoformat(),
        "window_days": window_days,
        "issues_total": len(issues),
        "open_total": len(open_issues),
        "open_unscoped": sum(1 for i in open_issues if not i["fix_versions"]),
        "flow": metrics.flow(issues, as_of, window_days),
        "hygiene": metrics.hygiene(issues, as_of, cfg["stale_days"]),
        "blockers": blockers,
        # Bug-review order: whatever threatens an at-risk release first, then priority, then idle time.
        "agenda": sorted(blockers, key=lambda b: (not b["at_risk"], -metrics.PRIORITY_RANK[b["priority"]],
                                                  -b["idle_days"], b["key"])),
        "readiness": readiness,
        "past_due": metrics.past_due(versions, issues, as_of, cfg.get("release_plan")),
        "escalations": escalations,
        "escalation_rules": escalation.rule_descriptions(cfg["escalation"]),
    }


def chase_order(e: dict) -> tuple:
    """Escalations worth chasing first: at-risk release, then no owner, then priority, then idle time."""
    return (not e["at_risk"], "E4" not in e["rules"], -metrics.PRIORITY_RANK.get(e["priority"], 0),
            -e["idle_days"], e["key"])


def chase_first(a: dict, n: int = 3) -> list[dict]:
    return a["escalations"][:n]   # already in chase order


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
    escalated_bc = {e["key"] for e in a["escalations"]} & {b["key"] for b in a["blockers"]}
    return {
        "worst": worst,
        "culprits": culprits,
        "others": [r for r in active if r is not worst],
        "escalations": len(a["escalations"]),
        "chase": chase_first(a),
        "escalated_blocker_critical": len(escalated_bc),
        # When most blockers/criticals trip a rule, the list is only useful ranked; say so.
        "saturated": bool(a["blockers"]) and len(escalated_bc) > len(a["blockers"]) / 2,
        "open_blocker_critical": len(a["blockers"]),
        "unowned_blocker_critical": len(unowned),
    }
