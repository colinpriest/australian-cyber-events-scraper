#!/usr/bin/env python3
"""Inspect and work the discovery retry queue.

Every discovery run drains the queue itself; this script reports on it and
handles the cases a run does not:

    python scripts/discovery_queue.py status            # per source and month: stored/processed/pending/...
    python scripts/discovery_queue.py status --all      # include complete months
    python scripts/discovery_queue.py drain             # process due records now, without discovery
    python scripts/discovery_queue.py release           # put quarantined records back in the queue

See ``cyber_data_collector/processing/retry_queue.py``.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cyber_data_collector.processing.retry_queue import RetryQueue  # noqa: E402
from cyber_data_collector.utils.config_manager import resolve_db_path  # noqa: E402

logger = logging.getLogger(__name__)


def _status(db_path: str, show_all: bool) -> int:
    conn = sqlite3.connect(db_path)
    try:
        queue = RetryQueue(conn)
        rows = queue.completion() if show_all else queue.incomplete_months()
        due = len(queue.due())
        waiting = queue.waiting()
        quarantined = queue.quarantined()
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
    return 0


def _release(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        released = RetryQueue(conn).release_quarantine()
    finally:
        conn.close()
    print(f"Released {released} quarantined record(s); the next discovery run (or 'drain') retries them.")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", default=None, help="Database (default: DATABASE_URL / instance/cyber_events.db)")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="Completion per source and discovery month")
    status.add_argument("--all", action="store_true", help="Include complete source-months")
    drain = sub.add_parser("drain", help="Process due records now (needs OpenAI and TypeSafe keys)")
    drain.add_argument("--limit", type=int, default=None)
    sub.add_parser("release", help="Return quarantined records to the queue")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    db_path = resolve_db_path(args.db_path)
    if args.command == "status":
        return _status(db_path, args.all)
    if args.command == "release":
        return _release(db_path)
    return _drain(db_path, args.limit)


if __name__ == "__main__":
    sys.exit(main())
