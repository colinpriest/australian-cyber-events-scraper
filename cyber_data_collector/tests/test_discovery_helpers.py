"""Regression tests for module-level helpers in discovery.py.

Pulled out of the main pipeline test so they can run without spinning
up the full discovery stack.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from cyber_data_collector.pipelines.discovery import _parse_event_date


# ----------------------------------------------------------------------
# _parse_event_date - regression for the
#   "fromisoformat: argument must be str"
# bug observed during global deduplication when SQLite returned a
# datetime.date object directly instead of a string.
# ----------------------------------------------------------------------

def test_parse_event_date_handles_str():
    assert _parse_event_date("2025-06-15") == date(2025, 6, 15)


def test_parse_event_date_handles_str_with_z_suffix():
    """Some pipelines stamp event_date with a Zulu suffix."""
    assert _parse_event_date("2025-06-15T00:00:00Z") == date(2025, 6, 15)


def test_parse_event_date_handles_already_a_date():
    """The bug case: SQLite can return a `date` object directly when
    detect_types is configured for DATE columns."""
    assert _parse_event_date(date(2025, 6, 15)) == date(2025, 6, 15)


def test_parse_event_date_handles_datetime():
    """And a `datetime` object - reduce to its date part."""
    assert _parse_event_date(datetime(2025, 6, 15, 14, 30, 0)) == date(2025, 6, 15)


def test_parse_event_date_handles_none():
    assert _parse_event_date(None) is None


def test_parse_event_date_returns_none_on_garbage_string():
    """Pre-fix this would raise ValueError; now it returns None so
    callers don't have to wrap each call in try/except."""
    assert _parse_event_date("not a date") is None


def test_parse_event_date_returns_none_on_unknown_type():
    """Defensive: int/list/etc. shouldn't crash the loader."""
    assert _parse_event_date(20250615) is None
    assert _parse_event_date([2025, 6, 15]) is None


def test_parse_event_date_does_not_raise_on_any_input():
    """The whole point of the helper - it must never raise so the
    enclosing query loop can't crash on a single bad row."""
    bad_inputs = [None, "", "not-a-date", 12345, [], {}, object()]
    for bad in bad_inputs:
        # Should not raise - return None or a valid date.
        result = _parse_event_date(bad)
        assert result is None or isinstance(result, date)
