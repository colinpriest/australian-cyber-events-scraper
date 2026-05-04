"""Tests for the shared end-of-run summary infrastructure used by
pipeline.py, run_full_pipeline.py, and the OAIC dashboard scraper.
"""
from __future__ import annotations

import io
import logging
from contextlib import redirect_stdout

import pytest

from cyber_data_collector.utils.run_summary import (
    RUN_LOG_COLLECTOR,
    RunLogCollector,
    install_run_summary,
    print_run_summary,
)


@pytest.fixture(autouse=True)
def _reset_collector():
    """Reset the global collector before and after each test so they
    don't leak state into each other.
    """
    RUN_LOG_COLLECTOR.reset()
    yield
    RUN_LOG_COLLECTOR.reset()


def test_install_idempotent():
    """install_run_summary can be called multiple times without
    duplicating handlers."""
    install_run_summary()
    install_run_summary()
    install_run_summary()
    handlers = [h for h in logging.getLogger().handlers
                if isinstance(h, RunLogCollector)]
    assert len(handlers) == 1


def test_collector_captures_warnings_and_errors():
    install_run_summary()
    log = logging.getLogger("test.collector")
    log.info("info should NOT be captured")
    log.warning("warning A")
    log.error("error A")
    log.critical("critical A")

    warns, errs = RUN_LOG_COLLECTOR.by_level()
    assert [r.getMessage() for r in warns] == ["warning A"]
    # CRITICAL rolls into errors.
    assert [r.getMessage() for r in errs] == ["error A", "critical A"]


def test_collector_ignores_info_and_debug():
    install_run_summary()
    log = logging.getLogger("test.quiet")
    log.debug("debug")
    log.info("info")
    assert RUN_LOG_COLLECTOR.records == []


def test_summary_prints_clean_when_no_issues():
    install_run_summary()
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_run_summary()
    out = buf.getvalue()
    assert "RUN SUMMARY" in out
    assert "0 error(s)" in out
    assert "0 warning(s)" in out
    assert "No warnings or errors captured." in out


def test_summary_replays_warnings_and_errors():
    install_run_summary()
    log = logging.getLogger("test.replay")
    log.warning("connection slow")
    log.error("connection refused")

    buf = io.StringIO()
    with redirect_stdout(buf):
        print_run_summary()
    out = buf.getvalue()
    assert "1 error(s)" in out
    assert "1 warning(s)" in out
    assert "connection slow" in out
    assert "connection refused" in out
    # Errors section appears before warnings.
    assert out.index("connection refused") < out.index("connection slow")


def test_summary_includes_phase_results_when_provided():
    install_run_summary()
    phase_results = {
        "discovery":      {"success": True, "events_found": 100, "errors": []},
        "reenrichment":   {"success": True, "events_enriched": 80,
                           "events_failed": 5, "errors": []},
        "deduplication":  {"success": True, "events_deduplicated": 75, "errors": []},
        "classification": {"success": False, "events_classified": 0,
                           "errors": ["openai 429"]},
        "dashboard":      {"success": True, "files_created": ["dashboard/index.html"],
                           "errors": []},
    }
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_run_summary(phase_results=phase_results)
    out = buf.getvalue()
    assert "PHASES" in out
    assert "discovery" in out
    assert "found=100" in out
    assert "enriched=80" in out
    assert "deduplicated=75" in out
    # FAILed classification surfaces with its error.
    assert "FAIL" in out and "classification" in out
    assert "openai 429" in out
    assert "OK" in out and "dashboard" in out


def test_summary_never_raises_even_on_garbage():
    """print_run_summary must be safe to call from finally blocks."""
    install_run_summary()
    # Phase result with an unexpected shape - must not raise.
    bad_phase = {"weird": "not a dict"}
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_run_summary(phase_results=bad_phase)  # type: ignore[arg-type]
    # No exception means pass; the bad entry just gets skipped.
    assert "RUN SUMMARY" in buf.getvalue()


def test_extra_sections_render():
    install_run_summary()
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_run_summary(
            extra_sections=[
                ("TOKEN USAGE", ["openai: 12,345 tokens", "cost: $0.42"]),
            ]
        )
    out = buf.getvalue()
    assert "TOKEN USAGE" in out
    assert "openai: 12,345 tokens" in out
    assert "cost: $0.42" in out


def test_noisy_loggers_demoted_to_warning():
    install_run_summary()
    for name in ("httpx", "httpcore", "openai"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


def test_collector_reset_clears_records():
    install_run_summary()
    log = logging.getLogger("test.reset")
    log.warning("first")
    log.error("second")
    assert len(RUN_LOG_COLLECTOR.records) == 2
    RUN_LOG_COLLECTOR.reset()
    assert RUN_LOG_COLLECTOR.records == []
