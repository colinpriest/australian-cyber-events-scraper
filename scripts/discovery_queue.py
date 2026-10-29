#!/usr/bin/env python3
"""Inspect and work the discovery retry queue.

Every discovery run drains the queue itself; this script reports on it and
handles the cases a run does not:

    python scripts/discovery_queue.py status            # per source and month: stored/processed/pending/...
    python scripts/discovery_queue.py status --all      # include complete months
    python scripts/discovery_queue.py drain             # process due records now, without discovery
    python scripts/discovery_queue.py release           # put quarantined records, given-up source-months
                                                        # and given-up Cyber Daily stories back in the queue
    python scripts/discovery_queue.py backfill --source CyberDaily --months 12
                                                        # run a source added later over processed months
    python scripts/discovery_queue.py backfill --source CyberDaily --since 2020-01 [--force]
    python scripts/discovery_queue.py refetch-cyberdaily [--days 90 | --all] [--repair-limit N] [--dry-run]
                                                        # fetch stored Cyber Daily stories again
    python scripts/discovery_queue.py refetch-cyberdaily --until-empty [--max-minutes N] [--concurrency N]
                                                        # keep going in batches until none is left, the
                                                        # time budget is spent or the site throttles

See ``cyber_data_collector/processing/retry_queue.py``.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cyber_data_collector.processing.retry_queue import RetryQueue  # noqa: E402
from cyber_data_collector.utils.config_manager import resolve_db_path  # noqa: E402

logger = logging.getLogger(__name__)


def _status(db_path: str, show_all: bool) -> int:
    from cyber_data_collector.processing import cyberdaily_refetch

    conn = sqlite3.connect(db_path)
    try:
        queue = RetryQueue(conn)
        rows = queue.completion() if show_all else queue.incomplete_months()
        due = len(queue.due())
        waiting = queue.waiting()
        quarantined = queue.quarantined()
        searches = queue.failed_discoveries()
        stories = cyberdaily_refetch.unrecoverable(conn)
    finally:
        conn.close()
    print(f"Due now: {due}   waiting for back-off: {waiting}   quarantined: {len(quarantined)}")
    if rows:
        print(f"\n{'source':<18}{'month':<9}{'stored':>7}{'processed':>10}{'rejected':>9}"
              f"{'pending':>8}{'failing':>8}{'quarantined':>12}")
        for r in rows:
            print(f"{r['source_type'] or '?':<18}{r['discovery_year']}-{r['discovery_month']:02d}  "
                  f"{r['stored']:>7}{r['processed']:>10}{r['rejected']:>9}{r['pending']:>8}{r['failing']:>8}"
                  f"{r['quarantined']:>12}")
    else:
        print("\nEvery tracked source-month is complete." if not show_all else "\nNothing tracked yet.")
    for q in quarantined:
        print(f"\nQUARANTINED {q['raw_event_id']} [{q['source_type']}] {q['raw_title'] or ''}\n"
              f"  {q['processing_error']}")
    if searches:
        print(f"\nSource-month searches that lost results ({len(searches)}):")
        for r in searches:
            state = ("GIVEN UP" if r["gave_up_at"] else
                     f"retry after {r['next_attempt_at']}" if r["next_attempt_at"] else "retry next run")
            print(f"  {r['source_type']:<16}{r['year']}-{r['month']:02d}  {r['error_type'] or '?':<11}"
                  f"attempts {r['failed_attempts']}  {state}")
    if stories:
        print(f"\nCyber Daily stories no longer fetched ({len(stories)}):")
        for r in stories:
            print(f"  {r['error_type'] or '?':<11}{r['url']}  ({r['last_error']})")
    return 0


def _release(db_path: str) -> int:
    from cyber_data_collector.processing import cyberdaily_refetch

    conn = sqlite3.connect(db_path)
    try:
        queue = RetryQueue(conn)
        released = queue.release_quarantine()
        months = queue.release_source_months()
        stories = cyberdaily_refetch.release(conn)
    finally:
        conn.close()
    print(f"Released {released} quarantined record(s); the next discovery run (or 'drain') retries them.")
    print(f"Released {months} given-up source-month search(es); the next backfill searches them again.")
    print(f"Released {stories} given-up Cyber Daily stor(ies); the next re-fetch tries them again "
          "(stories gone from the site, 404/410, stay out).")
    return 0


def _drain(db_path: str, limit: int | None) -> int:
    from cyber_data_collector.pipelines.discovery import EventDiscoveryEnrichmentPipeline

    pipeline = EventDiscoveryEnrichmentPipeline(db_path)
    try:
        pipeline.require_prerequisites()
        result = asyncio.run(pipeline.drain_retry_queue(limit=limit))
        pipeline.report_incomplete_months()
    finally:
        pipeline.close()
    print(f"Due {result['due']}, finished {result['completed']}, waiting for back-off {result['waiting']}")
    return 0 if result["completed"] == result["due"] else 2


def backfill_months(months: int, today: date | None = None) -> list[tuple[int, int]]:
    """The ``months`` calendar months before the current one, oldest first.

    The current month is left out: every refresh searches it (and the two
    before it) with all sources anyway.
    """
    today = today or date.today()
    year, month = today.year, today.month
    out = []
    for _ in range(months):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        out.append((year, month))
    return sorted(out)


def months_since(since: str, today: date | None = None) -> list[tuple[int, int]]:
    """Every calendar month from ``since`` (``YYYY-MM``) to the current one, oldest first."""
    today = today or date.today()
    year, month = (int(part) for part in since.split("-"))
    out = []
    while (year, month) <= (today.year, today.month):
        out.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def _backfill(db_path: str, source: str, months: list[tuple[int, int]], max_events: int, force: bool) -> int:
    from cyber_data_collector.pipelines.discovery import EventDiscoveryEnrichmentPipeline

    pipeline = EventDiscoveryEnrichmentPipeline(db_path)
    try:
        result = asyncio.run(pipeline.backfill_source(source, months, max_events, force=force))
    finally:
        pipeline.close()
    for key in ("searched", "already_done", "not_processed", "failed", "waiting", "gave_up"):
        listed = ", ".join(f"{y}-{m:02d}" for y, m in result[key]) or "-"
        print(f"{key:<14}{len(result[key]):>3}  {listed}")
    if result["searched"]:
        print("\nNew records are enriched, deduplicated and published by the next "
              "'python pipeline.py refresh'.")
    if result["gave_up"]:
        print("\nGiven-up months are not searched again until "
              "'python scripts/discovery_queue.py release' (or --force).")
    return 2 if result["failed"] or result["gave_up"] else 0


def _drain_refetch(db_path: str, days: int | None, batch_size: int | None, max_minutes: float | None,
                   concurrency: int) -> int:
    """Re-fetch in batches until the queue is empty. Exit 0 drained or out of time (resume later),
    2 when the site throttled (wait, then resume) or a story left the queue."""
    from cyber_data_collector.processing import cyberdaily_refetch as cr

    conn = sqlite3.connect(db_path)
    try:
        report = cr.drain(conn, days=days, batch_size=batch_size, max_minutes=max_minutes, concurrency=concurrency)
    finally:
        conn.close()
    print("Cyber Daily drain: " + report.summary())
    if report.ended == cr.THROTTLED_STOP:
        print("The site is throttling: stopped rather than ask again. Run the same command later; it resumes "
              "where it stopped.")
        return 2
    if report.ended == cr.TIME_STOP:
        print("Time budget spent; run the same command again to continue.")
    if report.ended == cr.NO_PROGRESS:
        print("A batch fetched nothing; stopped.")
        return 2
    return 2 if report.missing or report.gave_up else 0


def _refetch(db_path: str, days: int | None, limit: int | None, repair_limit: int | None, dry_run: bool) -> int:
    from cyber_data_collector.processing import cyberdaily_refetch

    conn = sqlite3.connect(db_path)
    try:
        report = cyberdaily_refetch.refetch_stories(conn, days=days, limit=limit, repair_limit=repair_limit,
                                                    dry_run=dry_run)
    finally:
        conn.close()
    scope = "all stored stories" if days is None else f"stories from the last {days} days, plus cut-off/short ones"
    print(f"Cyber Daily re-fetch ({scope}): {report.candidates} candidate(s), {report.repair_candidates} of them "
          f"incomplete; {report.repair_deferred} incomplete left for later runs"
          + (" - dry run" if dry_run else ""))
    if not dry_run:
        print("  " + ", ".join(f"{k} {v}" for k, v in report.outcomes.items())
              + f"; {report.requeued_enrichments} enrichment(s) queued again; {report.reopened} record(s) "
              f"reopened for classification; {report.still_incomplete} still incomplete")
        for row in report.failed[:20]:
            print(f"  FAILED [{row['error_type']}] {row['url']}: {row['error']}")
        for row in report.missing:
            print(f"  MISSING (not fetched again) {row['url']}: {row['error']}")
        for row in report.gave_up:
            print(f"  GAVE UP after {cyberdaily_refetch.MAX_REFETCH_FAILURES} failures [{row['error_type']}] "
                  f"{row['url']}: {row['error']}")
    # Temporary failures are retried by later runs; only a story that left the queue needs a person.
    return 2 if report.missing or report.gave_up else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", default=None, help="Database (default: DATABASE_URL / instance/cyber_events.db)")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="Completion per source and discovery month")
    status.add_argument("--all", action="store_true", help="Include complete source-months")
    drain = sub.add_parser("drain", help="Process due records now (needs OpenAI and TypeSafe keys)")
    drain.add_argument("--limit", type=int, default=None)
    sub.add_parser("release", help="Return quarantined records to the queue")
    backfill = sub.add_parser("backfill", help="Run one source over months processed before it was added "
                                               "(idempotent; needs OpenAI and TypeSafe keys)")
    backfill.add_argument("--source", required=True, choices=["CyberDaily", "WebberInsurance", "OAIC"],
                          help="Free sources only: a paid source over many months is a budget decision")
    span = backfill.add_mutually_exclusive_group()
    span.add_argument("--months", type=int, default=12, help="Calendar months before the current one")
    span.add_argument("--since", metavar="YYYY-MM", help="Every month from this one to the current one")
    backfill.add_argument("--max-events", type=int, default=1000)
    backfill.add_argument("--force", action="store_true", help="Search months the source already completed again")
    refetch = sub.add_parser("refetch-cyberdaily", help="Fetch stored Cyber Daily stories again; store changed text")
    window = refetch.add_mutually_exclusive_group()
    window.add_argument("--days", type=int, default=90,
                        help="Stories published or discovered in the last N days, plus cut-off/short ones")
    window.add_argument("--all", action="store_true", help="Every stored Cyber Daily story")
    refetch.add_argument("--limit", type=int, default=None)
    refetch.add_argument("--repair-limit", type=int, default=None,
                         help="Incomplete (cut-off or short) stories to fetch this run; 0 = no limit "
                              "(default: cyberdaily_refetch.DEFAULT_REPAIR_LIMIT)")
    refetch.add_argument("--dry-run", action="store_true", help="Count the candidates; fetch nothing")
    refetch.add_argument("--until-empty", action="store_true",
                         help="Drain: fetch batches until no incomplete story is left, --max-minutes pass or the "
                              "site throttles (resumable; stories fetched within 24 hours are not fetched again)")
    refetch.add_argument("--max-minutes", type=float, default=None,
                         help="Drain time budget in minutes (default: cyberdaily_refetch.DEFAULT_DRAIN_MINUTES)")
    refetch.add_argument("--concurrency", type=int, default=None,
                         help="Pages fetched at once by a drain (default: cyberdaily_refetch.DRAIN_CONCURRENCY)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    db_path = resolve_db_path(args.db_path)
    if args.command == "status":
        return _status(db_path, args.all)
    if args.command == "release":
        return _release(db_path)
    if args.command == "backfill":
        months = months_since(args.since) if args.since else backfill_months(args.months)
        return _backfill(db_path, args.source, months, args.max_events, args.force)
    if args.command == "refetch-cyberdaily":
        from cyber_data_collector.processing.cyberdaily_refetch import DEFAULT_REPAIR_LIMIT

        repair_limit = DEFAULT_REPAIR_LIMIT if args.repair_limit is None else (args.repair_limit or None)
        if args.until_empty:
            from cyber_data_collector.processing.cyberdaily_refetch import DEFAULT_DRAIN_MINUTES, DRAIN_CONCURRENCY

            if args.dry_run:
                parser.error("--until-empty cannot be combined with --dry-run")
            return _drain_refetch(db_path, None if args.all else args.days, repair_limit,
                                  DEFAULT_DRAIN_MINUTES if args.max_minutes is None else (args.max_minutes or None),
                                  args.concurrency or DRAIN_CONCURRENCY)
        return _refetch(db_path, None if args.all else args.days, args.limit, repair_limit, args.dry_run)
    return _drain(db_path, args.limit)


if __name__ == "__main__":
    sys.exit(main())
