"""Tests for dropping an incomplete latest month from the dashboard's monthly
time-series charts (scripts.build_static_dashboard)."""
from __future__ import annotations

import sqlite3

import pytest

from scripts.build_static_dashboard import (
    compute_incomplete_month_cutoff,
    get_data_collection_timestamp,
    get_latest_incident_month,
)


def test_incomplete_month_is_excluded():
    cutoff = compute_incomplete_month_cutoff('2026-10', '2026-10-02T09:52:48.864901')
    assert cutoff is not None
    assert cutoff['excluded_months'] == ['2026-10']
    assert cutoff['series_end_date'] == '2026-09-30'
    assert cutoff['collected_on'] == '2026-10-02'
    assert cutoff['note'] == 'Excludes Oct 2026 (incomplete at data collection, 2 Oct 2026)'


def test_complete_month_is_kept():
    # Latest incident month is September; data collected in October.
    assert compute_incomplete_month_cutoff('2026-09', '2026-10-02T09:52:48') is None


def test_collection_on_last_day_of_month_counts_as_complete():
    assert compute_incomplete_month_cutoff('2026-09', '2026-09-30T08:00:00') is None
    # December boundary
    assert compute_incomplete_month_cutoff('2025-12', '2025-12-31 23:59:59') is None


def test_collection_on_second_last_day_is_incomplete():
    cutoff = compute_incomplete_month_cutoff('2026-02', '2026-02-27T12:00:00')
    assert cutoff is not None
    assert cutoff['excluded_months'] == ['2026-02']
    assert cutoff['series_end_date'] == '2026-01-31'


def test_future_dated_months_are_also_excluded():
    # A mis-dated November event must not leave the in-progress October behind.
    cutoff = compute_incomplete_month_cutoff('2026-11', '2026-10-02T09:00:00')
    assert cutoff['excluded_months'] == ['2026-10', '2026-11']
    assert cutoff['series_end_date'] == '2026-09-30'
    assert cutoff['note'].startswith('Excludes Oct 2026 to Nov 2026')


def test_future_month_after_collection_on_last_day():
    cutoff = compute_incomplete_month_cutoff('2026-11', '2026-10-31T09:00:00')
    assert cutoff['excluded_months'] == ['2026-11']
    assert cutoff['series_end_date'] == '2026-10-31'


@pytest.mark.parametrize('latest_month, collected_at', [
    (None, '2026-10-02T09:00:00'),
    ('', '2026-10-02T09:00:00'),
    ('2026-10', None),
    ('2026-10', ''),
    ('2026-10', 'not a date'),
])
def test_empty_or_unknown_inputs_exclude_nothing(latest_month, collected_at):
    assert compute_incomplete_month_cutoff(latest_month, collected_at) is None


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE RawEvents (raw_event_id TEXT, discovered_at TEXT)')
    conn.execute(
        'CREATE TABLE DeduplicatedEvents '
        '(deduplicated_event_id TEXT, status TEXT, event_date TEXT)'
    )
    return conn


def test_db_helpers_on_empty_tables():
    conn = _make_db()
    assert get_data_collection_timestamp(conn) is None
    assert get_latest_incident_month(conn, '2020-01-01', '2026-12-31') is None


def test_db_helpers_return_latest_values():
    conn = _make_db()
    conn.executemany('INSERT INTO RawEvents VALUES (?, ?)', [
        ('a', '2026-09-15T10:00:00'),
        ('b', '2026-10-02T09:52:48.864901'),
    ])
    conn.executemany('INSERT INTO DeduplicatedEvents VALUES (?, ?, ?)', [
        ('d1', 'Active', '2026-09-20'),
        ('d2', 'Active', '2026-10-02'),
        ('d3', 'Superseded', '2026-12-01'),
    ])
    assert get_data_collection_timestamp(conn) == '2026-10-02T09:52:48.864901'
    assert get_latest_incident_month(conn, '2020-01-01', '2026-12-31') == '2026-10'


def test_missing_rawevents_table_returns_none():
    conn = sqlite3.connect(':memory:')
    assert get_data_collection_timestamp(conn) is None
