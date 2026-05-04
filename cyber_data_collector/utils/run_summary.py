"""End-of-run summary infrastructure shared across pipeline entry points.

Long-running scripts in this repo (OAIC dashboard scraper, full pipeline,
status reporter) emit hundreds of INFO-level log lines per run, which
causes legitimate WARNING/ERROR records to scroll off the console before
the user can react to them. This module provides a single place to:

  1. Demote noisy third-party loggers (httpx, httpcore, openai) so their
     per-request chatter doesn't drown out warnings/errors.
  2. Capture every WARNING/ERROR/CRITICAL emitted during the run.
  3. Print a clean end-of-run summary that replays the captured records
     plus optional phase-level results.

Usage::

    from cyber_data_collector.utils.run_summary import (
        install_run_summary,
        print_run_summary,
    )

    install_run_summary()           # call once at startup

    try:
        do_work()
    finally:
        print_run_summary(            # always call, even on Ctrl+C
            phase_results=optional_dict_per_phase,
        )

The collector is process-global and idempotent - call ``install_run_summary``
multiple times safely; the handler is only attached once.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


__all__ = [
    "RunLogCollector",
    "install_run_summary",
    "print_run_summary",
    "RUN_LOG_COLLECTOR",
]


_NOISY_LOGGERS = ("httpx", "httpcore", "openai")


class RunLogCollector(logging.Handler):
    """Logging handler that captures every WARNING/ERROR/CRITICAL record.

    Stored in insertion order so ``print_run_summary`` can replay them
    in chronological order at end of run.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.WARNING:
            self.records.append(record)

    def reset(self) -> None:
        """Clear all captured records. Useful in tests so each test gets
        a fresh slate.
        """
        self.records.clear()

    def by_level(self) -> Tuple[List[logging.LogRecord], List[logging.LogRecord]]:
        """Return (warnings, errors) in insertion order. CRITICAL is
        rolled into errors.
        """
        warnings_ = [r for r in self.records if r.levelno == logging.WARNING]
        errors = [r for r in self.records if r.levelno >= logging.ERROR]
        return warnings_, errors


# Module-global singleton; installed lazily by install_run_summary.
RUN_LOG_COLLECTOR: RunLogCollector = RunLogCollector()
_INSTALLED = False


def install_run_summary() -> RunLogCollector:
    """Attach the global collector to the root logger and demote noisy
    third-party loggers. Idempotent - safe to call multiple times.

    Returns the global collector for callers that want to reset it
    between runs.
    """
    global _INSTALLED
    if _INSTALLED:
        return RUN_LOG_COLLECTOR

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger().addHandler(RUN_LOG_COLLECTOR)
    _INSTALLED = True
    return RUN_LOG_COLLECTOR


def _format_record(rec: logging.LogRecord) -> str:
    ts = datetime.fromtimestamp(rec.created).strftime("%H:%M:%S")
    return f"[{ts}] {rec.levelname:7s} {rec.name}: {rec.getMessage()}"


def _format_phase_results(phase_results: Dict[str, Dict[str, Any]]) -> List[str]:
    """Render the per-phase results dict (used by UnifiedPipeline) into
    one-line summaries with status, counts, and any per-phase errors.
    """
    out: List[str] = []
    for phase, info in phase_results.items():
        if not isinstance(info, dict):
            continue
        success = info.get("success")
        status = (
            "OK     " if success is True
            else "FAIL   " if success is False
            else "SKIP   "
        )
        # Common count fields used across phases. Show whichever is
        # populated; ignore fields that are 0 / None / missing.
        counts: List[str] = []
        for field in (
            "events_found",
            "events_enriched",
            "events_deduplicated",
            "events_classified",
            "events_skipped",
            "events_failed",
        ):
            val = info.get(field)
            if isinstance(val, int) and val:
                counts.append(f"{field.replace('events_', '')}={val}")
        counts_str = ", ".join(counts) if counts else "-"
        line = f"  {status} {phase:<16s} {counts_str}"
        out.append(line)
        for err in info.get("errors") or []:
            out.append(f"           ERROR: {err}")
    return out


def print_run_summary(
    phase_results: Optional[Dict[str, Dict[str, Any]]] = None,
    extra_sections: Optional[List[Tuple[str, List[str]]]] = None,
) -> None:
    """Print the end-of-run summary to stdout.

    Args:
        phase_results: Optional dict mapping phase-name -> per-phase
            info (success/counts/errors). Used by run_full_pipeline.py
            to surface phase-level outcomes alongside log records.
        extra_sections: Optional list of (heading, lines) tuples for
            arbitrary additional sections (e.g. token usage, cost).

    Always safe to call - never raises. Intended for ``finally``
    blocks so the summary fires on success, failure, and Ctrl+C alike.
    """
    try:
        warnings_, errors = RUN_LOG_COLLECTOR.by_level()
        sep = "=" * 78
        print()
        print(sep)
        print(f"  RUN SUMMARY  -  {len(errors)} error(s), {len(warnings_)} warning(s)")
        print(sep)

        if phase_results:
            phase_lines = _format_phase_results(phase_results)
            if phase_lines:
                print()
                print("PHASES")
                print("-" * 78)
                for line in phase_lines:
                    print(line)

        if errors:
            print()
            print(f"ERRORS ({len(errors)})")
            print("-" * 78)
            for r in errors:
                print(_format_record(r))

        if warnings_:
            print()
            print(f"WARNINGS ({len(warnings_)})")
            print("-" * 78)
            for r in warnings_:
                print(_format_record(r))

        if extra_sections:
            for heading, lines in extra_sections:
                if not lines:
                    continue
                print()
                print(heading)
                print("-" * 78)
                for line in lines:
                    print(line)

        if not warnings_ and not errors and not phase_results:
            print()
            print("No warnings or errors captured.")
        print(sep)
    except Exception as exc:  # never let the summary itself crash
        try:
            print(f"\n[run_summary] failed to print summary: {exc}")
        except Exception:
            pass
