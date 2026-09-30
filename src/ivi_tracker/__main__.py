"""CLI entry point: python -m ivi_tracker fetch|report|run."""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date

from dotenv import load_dotenv

from . import analysis, llm, render, store
from .config import load_config
from .jira_client import JiraClient

INCREMENTAL_DAYS = 8


def fetch(mode: str, cfg: dict, log: bool = True) -> dict:
    """Pull issues, versions, and components into the SQLite cache (and log the run unless `log` is False)."""
    project = cfg["project_key"]
    client = JiraClient()
    conn = store.connect()
    t0 = time.perf_counter()

    if mode == "full":
        jql = f"project = {project} ORDER BY created ASC"
    else:
        jql = f"project = {project} AND updated >= -{INCREMENTAL_DAYS}d ORDER BY updated ASC"

    raw = client.search_all(jql)
    n = store.upsert_issues(conn, raw)
    if mode == "full":
        pruned = store.prune_missing_issues(conn, {i["key"] for i in raw})
        if pruned:
            print(f"Pruned {pruned} issues no longer in {project}")
    store.replace_versions(conn, client.get_versions(project))
    store.replace_components(conn, client.get_components(project))
    fetch_s = round(time.perf_counter() - t0, 2)

    stats = {"issues_pulled": n, "api_calls": client.api_calls, "fetch_s": fetch_s}
    print(f"Fetched {n} issues ({mode}) in {fetch_s}s using {client.api_calls} API calls")
    if log:
        store.append_run_log(conn, {"mode": f"fetch-{mode}", "total_s": fetch_s, **stats})
    return stats


def report(window_days: int, cfg: dict, no_llm: bool = False, quiet: bool = False) -> dict:
    """Metrics -> facts JSON -> LLM drafts (number-guarded) -> markdown in reports/YYYY-MM-DD/."""
    conn = store.connect()
    t0 = time.perf_counter()
    result = analysis.analyze(store.load_issues(conn), store.load_versions(conn),
                              date.today(), cfg, window_days)
    facts = llm.build_facts(result, cfg)
    t1 = time.perf_counter()
    drafts = llm.placeholder_drafts(facts) if no_llm else llm.draft_all(facts, llm.openai_complete(cfg))
    t2 = time.perf_counter()
    out = render.render_all(result, drafts, facts, cfg)
    t3 = time.perf_counter()

    if not quiet:
        print(analysis.console_summary(result))
        print()
    for note in drafts.retries:
        print(f"Number guard retry: {note}")
    timings = {"metrics_s": round(t1 - t0, 2), "llm_s": round(t2 - t1, 2), "render_s": round(t3 - t2, 2)}
    print(f"Wrote {out.relative_to(store.ROOT)}/ ({'no LLM' if no_llm else f'{drafts.tokens} LLM tokens'}; "
          + ", ".join(f"{k} {v}" for k, v in timings.items()) + ")")
    return {"window_days": window_days, "llm_tokens": drafts.tokens, **timings, "out": out}


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    cfg = load_config()
    parser = argparse.ArgumentParser(prog="ivi_tracker")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="pull Jira data into data/cache.db")
    mode = p_fetch.add_mutually_exclusive_group(required=True)
    mode.add_argument("--full", action="store_const", const="full", dest="mode")
    mode.add_argument("--incremental", action="store_const", const="incremental", dest="mode")

    for name in ("report", "run"):
        p = sub.add_parser(name)
        p.add_argument("--window", type=int, default=cfg["window_days"])
        p.add_argument("--no-llm", action="store_true", help="skip OpenAI calls; narrative left as placeholders")

    args = parser.parse_args(argv)
    if args.command == "fetch":
        fetch(args.mode, cfg)
        return 0
    if args.command in ("report", "run") and not args.no_llm and not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set. Add it to .env, or pass --no-llm to render tables only.",
              file=sys.stderr)
        return 2
    if args.command == "report":
        stats = report(args.window, cfg, no_llm=args.no_llm)
        stats.pop("out")
        total = round(sum(v for k, v in stats.items() if k.endswith("_s")), 2)
        store.append_run_log(store.connect(), {"mode": "report-no-llm" if args.no_llm else "report",
                                               "total_s": total, **stats})
        return 0
    # run: what CI calls. Full fetch + report + snapshot, logged as one end-to-end row.
    t0 = time.perf_counter()
    fetch_stats = fetch("full", cfg, log=False)
    stats = report(args.window, cfg, no_llm=args.no_llm, quiet=True)
    snap = store.save_snapshot(store.connect(), date.today())
    store.export_app_data(store.connect(), date.today())
    total = round(time.perf_counter() - t0, 2)
    stats.pop("out")
    store.append_run_log(store.connect(), {"mode": "run-no-llm" if args.no_llm else "run",
                                           "total_s": total, **fetch_stats, **stats})
    print(f"Snapshot {snap.relative_to(store.ROOT)}; end-to-end {total}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
