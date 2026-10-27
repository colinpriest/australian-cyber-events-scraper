#!/usr/bin/env python3
"""
Simplified command-line interface for the Australian Cyber Events pipeline.

Commands:
  refresh  - Run a 3-month rolling refresh (default 90 days)
  rebuild  - Wipe and reconstruct the entire database (requires --force)
  status   - Report last ingest and latest event
  init     - Create or upgrade a database to the current schema
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional

from cyber_data_collector.utils.config_manager import resolve_db_path
from cyber_data_collector.utils.db_backup import backup_database
from cyber_data_collector.utils.run_summary import (
    install_run_summary,
    print_run_summary,
)
from scripts.project_status import report_status
from run_full_pipeline import UnifiedPipeline
from scripts.wipe_database import DatabaseRecordWiper

# Install end-of-run summary handler + demote noisy third-party loggers.
# Idempotent - run_full_pipeline.py also installs but the second call is
# a no-op. Calling here ensures the collector is active even when this
# wrapper short-circuits before delegating (e.g. status / dry-run paths).
install_run_summary()


DEFAULT_SOURCES = ["Perplexity", "OAIC", "GoogleSearch", "WebberInsurance", "CyberDaily"]


def _resolve_db_path(db_path: Optional[str]) -> str:
    return resolve_db_path(db_path)


def _build_pipeline_args(
    db_path: str,
    sources: Optional[List[str]],
    max_events: int,
    days: int,
    out_dir: str,
    skip_classification: bool,
) -> SimpleNamespace:
    return SimpleNamespace(
        discover_only=False,
        classify_only=False,
        dashboard_only=False,
        re_enrich=False,
        re_enrich_limit=None,
        skip_classification=skip_classification,
        classify_limit=None,
        # Read unconditionally by UnifiedPipeline's post-dedup steps; leaving
        # them off made the recurrence check and entity sizing fail silently.
        force_dedup=False,
        skip_data_hygiene=False,
        report_event_rejections=False,
        skip_recurrence_check=False,
        recurrence_window=90,
        recurrence_min_certainty=0.85,
        skip_entity_sizing=False,
        entity_size_limit=None,
        skip_missed_merge_check=False,
        missed_merge_apply=False,
        missed_merge_days=120,
        missed_merge_min_certainty=0.9,
        continue_on_error=False,
        enrich_limit=500,
        source=sources,
        max_events=max_events,
        days=days,
        out_dir=out_dir,
        db_path=db_path,
    )


def _run_pipeline(args: SimpleNamespace) -> int:
    """Run the unified pipeline. Stashes ``pipeline.results`` on the args
    namespace so the outermost ``__main__`` summary can include phase
    results. The summary itself is printed by ``__main__``, not here, to
    avoid double-printing.
    """
    from cyber_data_collector.utils.api_budget import configure as configure_api_budget
    configure_api_budget()   # API_BUDGET_<PROVIDER> limits from the environment
    pipeline = UnifiedPipeline(args.db_path)
    args._pipeline_results = pipeline.results  # picked up by main()
    result = asyncio.run(pipeline.run_pipeline(args))

    # Print API token usage and cost report
    from cyber_data_collector.utils.token_tracker import tracker
    tracker.log_report()

    # 0 ok, 1 a required phase failed, 2 completed with failed steps (as run_full_pipeline).
    return {"ok": 0, "degraded": 2}.get(result, 1)


def _run_refresh(parsed: argparse.Namespace) -> int:
    db_path = _resolve_db_path(parsed.db_path)
    sources = _normalize_sources(parsed.source) or DEFAULT_SOURCES
    args = _build_pipeline_args(
        db_path=db_path,
        sources=sources,
        max_events=parsed.max_events,
        days=parsed.days,
        out_dir=parsed.out_dir,
        skip_classification=parsed.skip_classification,
    )
    return _run_pipeline(args)


def _run_rebuild(parsed: argparse.Namespace) -> int:
    if not parsed.force:
        print("Rebuild requires --force to wipe existing data.")
        return 2

    # Resolved once, before anything is touched: the wipe, the backup and the
    # rebuild all act on this one database.
    db_path = _resolve_db_path(parsed.db_path)
    if not parsed.dry_run and Path(db_path).exists():
        backup = f"{db_path}.backup.{datetime.now():%Y%m%d_%H%M%S}_pre_rebuild"
        backup_database(db_path, backup)
        print(f"Verified backup before wipe: {backup}")

    wiper = DatabaseRecordWiper(db_path=db_path, dry_run=parsed.dry_run, force=parsed.force)
    wipe_success = wiper.wipe_sqlite_records()
    if not wipe_success:
        print("Database wipe failed. Aborting rebuild.")
        return 1

    if parsed.dry_run:
        print("Dry run complete. Re-run with --force to rebuild.")
        return 0

    sources = _normalize_sources(parsed.source) or DEFAULT_SOURCES
    args = _build_pipeline_args(
        db_path=db_path,
        sources=sources,
        max_events=parsed.max_events,
        days=parsed.days,
        out_dir=parsed.out_dir,
        skip_classification=parsed.skip_classification,
    )
    return _run_pipeline(args)


def _run_init(parsed: argparse.Namespace) -> int:
    from cyber_data_collector.storage.schema_init import initialize_database
    db_path = _resolve_db_path(parsed.db_path)
    report = initialize_database(db_path)
    print(f"{db_path}: {report.summary()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Simplified CLI for the Australian Cyber Events pipeline."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    refresh = subparsers.add_parser("refresh", help="Run a 3-month rolling refresh (default 90 days).")
    refresh.add_argument("--days", type=int, default=90, help="Days to look back (default: 90).")
    refresh.add_argument("--source", action="append", nargs="+", choices=DEFAULT_SOURCES,
                         help="Data sources to use (default: recommended sources).")
    refresh.add_argument("--max-events", type=int, default=500,
                         help="Maximum events per source per month (default: 500).")
    refresh.add_argument("--out-dir", default="dashboard",
                         help="Output directory for static dashboard (default: dashboard).")
    refresh.add_argument("--skip-classification", action="store_true",
                         help="Skip ASD classification phase (faster refresh).")
    refresh.add_argument("--db-path", default=None, help="Path to SQLite database file.")
    refresh.set_defaults(func=_run_refresh)

    rebuild = subparsers.add_parser("rebuild", help="Wipe and reconstruct the entire database.")
    rebuild.add_argument("--days", type=int, default=0, help="Days to look back (default: full history).")
    rebuild.add_argument("--source", action="append", nargs="+", choices=DEFAULT_SOURCES,
                         help="Data sources to use (default: recommended sources).")
    rebuild.add_argument("--max-events", type=int, default=1000,
                         help="Maximum events per source per month (default: 1000).")
    rebuild.add_argument("--out-dir", default="dashboard",
                         help="Output directory for static dashboard (default: dashboard).")
    rebuild.add_argument("--skip-classification", action="store_true",
                         help="Skip ASD classification phase (faster rebuild).")
    rebuild.add_argument("--db-path", default=None, help="Path to SQLite database file.")
    rebuild.add_argument("--dry-run", action="store_true", help="Preview the wipe without deleting data.")
    rebuild.add_argument("--force", action="store_true", help="Confirm destructive wipe.")
    rebuild.set_defaults(func=_run_rebuild)

    init = subparsers.add_parser("init", help="Create or upgrade a database to the current schema.")
    init.add_argument("--db-path", default=None, help="Path to SQLite database file.")
    init.set_defaults(func=_run_init)

    status = subparsers.add_parser("status", help="Show last ingest and latest event.")
    status.add_argument("--db-path", default=None, help="Path to SQLite database file.")
    status.set_defaults(func=lambda parsed: report_status(parsed.db_path))

    return parser


def _normalize_sources(sources: Optional[List[List[str]]]) -> Optional[List[str]]:
    if not sources:
        return None
    return [source for group in sources for source in group]


def main() -> int:
    parser = build_parser()
    parsed = parser.parse_args()
    try:
        return parsed.func(parsed)
    except KeyboardInterrupt:
        import logging
        logging.getLogger(__name__).warning(
            "pipeline.py interrupted by user (Ctrl+C)"
        )
        return 130


if __name__ == "__main__":
    rc = 1
    parsed_args = None
    try:
        parser = build_parser()
        parsed_args = parser.parse_args()
        try:
            rc = parsed_args.func(parsed_args)
        except KeyboardInterrupt:
            import logging
            logging.getLogger(__name__).warning(
                "pipeline.py interrupted by user (Ctrl+C)"
            )
            rc = 130
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error(
                f"pipeline.py unhandled exception: {exc}"
            )
            rc = 1
    finally:
        # Always print the end-of-run summary - including the
        # status / rebuild / refresh paths. Phase results are stashed on
        # the args namespace by _run_pipeline; status / rebuild --dry-run
        # paths leave it unset, in which case we just print the
        # warning/error log without a phases section.
        phase_results = getattr(parsed_args, "_pipeline_results", None) if parsed_args else None
        try:
            print_run_summary(phase_results=phase_results)
        except Exception:
            pass
    raise SystemExit(rc)
