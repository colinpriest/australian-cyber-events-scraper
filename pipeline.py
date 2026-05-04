#!/usr/bin/env python3
"""
Simplified command-line interface for the Australian Cyber Events pipeline.

Commands:
  refresh  - Run a 3-month rolling refresh (default 90 days)
  rebuild  - Wipe and reconstruct the entire database (requires --force)
  status   - Report last ingest and latest event
"""

from __future__ import annotations

import argparse
import asyncio
from types import SimpleNamespace
from typing import List, Optional

from cyber_data_collector.utils import ConfigManager
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


DEFAULT_SOURCES = ["Perplexity", "OAIC", "GoogleSearch", "WebberInsurance"]


def _resolve_db_path(db_path: Optional[str]) -> str:
    env_config = ConfigManager(".env").load()
    return db_path or env_config.get("DATABASE_PATH") or "instance/cyber_events.db"


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
        continue_on_error=False,
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
    pipeline = UnifiedPipeline(args.db_path)
    args._pipeline_results = pipeline.results  # picked up by main()
    success = asyncio.run(pipeline.run_pipeline(args))

    # Print API token usage and cost report
    from cyber_data_collector.utils.token_tracker import tracker
    tracker.log_report()

    return 0 if success else 1


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

    wiper = DatabaseRecordWiper(dry_run=parsed.dry_run, force=parsed.force)
    wipe_success = wiper.wipe_sqlite_records()
    if not wipe_success:
        print("Database wipe failed. Aborting rebuild.")
        return 1

    if parsed.dry_run:
        print("Dry run complete. Re-run with --force to rebuild.")
        return 0

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
