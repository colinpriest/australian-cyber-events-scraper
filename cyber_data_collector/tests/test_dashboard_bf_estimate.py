"""Tests for the Bornhuetter-Ferguson partial half-year estimate on the dashboard."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import numpy as np
import pytest

from typing import Dict, List, Tuple

from scripts.build_static_dashboard import (
    bf_interval,
    bf_month_estimates,
    compute_partial_period_estimate,
    expected_monthly_rate,
    get_partial_period_estimate,
    half_year_bounds,
    lag_completeness,
    month_reported_fraction,
    prepare_oaic_comparison_data,
    rate_months,
    reporting_lag_sample,
)

E = date(2026, 10, 2)


def _events(start: date, end: date, per_day: int, lag_days: int) -> List[Tuple[date, date]]:
    """per_day incidents on every day in [start, end], each known lag_days later."""
    out = []
    d = start
    while d <= end:
        out.extend([(d, d + timedelta(days=lag_days))] * per_day)
        d += timedelta(days=1)
    return out


def _counts(events: List[Tuple[date, date]], extraction: date) -> Dict[Tuple[int, int], int]:
    """Monthly counts of incidents already known at extraction."""
    counts: Dict[Tuple[int, int], int] = {}
    for incident, known in events:
        if incident <= extraction and known <= extraction:
            counts[(incident.year, incident.month)] = counts.get((incident.year, incident.month), 0) + 1
    return counts


def test_half_year_bounds():
    assert half_year_bounds(E) == ('2026 H2', date(2026, 7, 1), date(2026, 12, 31))
    assert half_year_bounds(date(2026, 6, 30))[0] == '2026 H1'


def test_lag_sample_filters_by_age_and_collection_start():
    events = [
        (E - timedelta(days=200), E - timedelta(days=190)),   # developed: lag 10
        (E - timedelta(days=100), E - timedelta(days=90)),    # too young
        (E - timedelta(days=800), E - timedelta(days=700)),   # too old
        (E - timedelta(days=300), E - timedelta(days=305)),   # negative lag -> 0
        (date(2025, 1, 1), date(2025, 9, 28)),                # pre-collection backfill
    ]
    lags = reporting_lag_sample(events, E, collection_start=date(2025, 9, 28))
    assert list(lags) == [0.0, 10.0]


def test_lag_completeness_is_ecdf():
    lags = np.array([0, 10, 20, 30], dtype=float)
    assert list(lag_completeness(lags, [-1, 0, 15, 30, 100])) == [0.0, 0.25, 0.5, 1.0, 1.0]


def test_zero_lag_reproduces_observed_past_months():
    lags = np.zeros(50)
    counts = {(2026, 7): 14, (2026, 8): 18, (2026, 9): 11}
    months = bf_month_estimates(counts, [(2026, 7), (2026, 8), (2026, 9)], E, lags, rate=20.0)
    assert [m['estimate'] for m in months] == pytest.approx([14, 18, 11])


def test_future_months_get_rate():
    lags = np.array([5.0, 40.0, 90.0])
    months = bf_month_estimates({}, [(2026, 11), (2026, 12)], E, lags, rate=17.5)
    assert [m['estimate'] for m in months] == pytest.approx([17.5, 17.5])
    assert all(m['reported_fraction'] == 0.0 for m in months)


def test_old_fully_reported_month_unchanged():
    lags = np.array([0.0, 30.0, 60.0, 150.0])
    months = bf_month_estimates({(2025, 1): 12}, [(2025, 1)], E, lags, rate=50.0)
    assert months[0]['estimate'] == pytest.approx(12)


def test_partial_current_month_with_zero_lag_adds_unelapsed_share():
    # Oct 2026: 2 of 31 days elapsed at E; zero lag -> 2/31 already reported.
    frac = month_reported_fraction(2026, 10, E, np.zeros(10))
    assert frac == pytest.approx(2 / 31)


def test_rate_months_are_complete_and_developed():
    assert rate_months(E) == [(2026, m) for m in range(1, 7)]
    for y, m in rate_months(E):
        assert (E - date(y, m, 28)).days >= 90


def test_expected_rate_grosses_up_underdeveloped_months():
    lags = np.zeros(10)
    counts = {(2026, m): 10 for m in range(1, 7)}
    assert expected_monthly_rate(counts, E, lags) == pytest.approx(10)
    # Half the incidents take 200 days to surface: recent rate months are only
    # partly reported, so R must exceed the raw mean.
    slow = np.array([0.0] * 5 + [200.0] * 5)
    assert expected_monthly_rate(counts, E, slow) > 10


def test_constant_rate_recovers_full_period():
    """Steady 1/day with a 30-day lag: the BF total should be close to 184."""
    events = _events(date(2025, 6, 1), date(2026, 12, 31), per_day=1, lag_days=30)
    known = [(i, k) for i, k in events if k <= E]
    result = compute_partial_period_estimate(_counts(events, E), known, E)
    assert result['method'] == 'bornhuetter_ferguson'
    assert result['period'] == '2026 H2'
    assert result['observed'] == sum(1 for i, k in known if i >= date(2026, 7, 1))
    assert result['estimate'] == pytest.approx(184, abs=4)
    # Straight pro-rata would be biased low here.
    assert result['observed'] * 184 / 94 < result['estimate']


def test_fallback_to_pro_rata_when_too_few_developed_events():
    events = _events(date(2026, 3, 1), date(2026, 3, 20), per_day=1, lag_days=5)
    events += _events(date(2026, 7, 1), date(2026, 9, 30), per_day=1, lag_days=0)
    result = compute_partial_period_estimate(_counts(events, E), events, E)
    assert result['method'] == 'pro_rata'
    assert result['low'] is None and result['high'] is None
    elapsed = (E - date(2026, 7, 1)).days + 1
    assert result['estimate'] == round(result['observed'] * 184 / elapsed)
    assert 'pro-rata' in result['note']


def test_interval_contains_estimate_and_is_deterministic():
    events = _events(date(2025, 6, 1), date(2026, 12, 31), per_day=1, lag_days=45)
    known = [(i, k) for i, k in events if k <= E]
    r1 = compute_partial_period_estimate(_counts(events, E), known, E)
    r2 = compute_partial_period_estimate(_counts(events, E), known, E)
    assert r1['low'] <= r1['estimate'] <= r1['high']
    assert r1['low'] < r1['high']
    assert (r1['low'], r1['high'], r1['estimate']) == (r2['low'], r2['high'], r2['estimate'])


def test_bf_interval_seed_reproducible():
    lags = np.array([0.0, 10, 20, 40, 80, 120] * 10)
    counts = {(2026, m): 15 for m in range(1, 10)}
    period = [(2026, m) for m in range(7, 13)]
    a = bf_interval(counts, period, E, lags, rate_months(E), n_boot=200, seed=1)
    b = bf_interval(counts, period, E, lags, rate_months(E), n_boot=200, seed=1)
    assert a == b


def test_comparison_data_attaches_estimate_to_latest_period():
    db = {'periods': ['2026 H1', '2026 H2'], 'database_counts': [96, 44]}
    est = {'period': '2026 H2', 'estimate': 121, 'low': 108, 'high': 138}
    out = prepare_oaic_comparison_data(db, [], est)
    assert out['partial_estimate'] == est
    # An estimate for a period that is not the latest one is not attached.
    out = prepare_oaic_comparison_data(db, [], {'period': '2026 H1', 'estimate': 1})
    assert out['partial_estimate'] is None


def test_get_partial_period_estimate_uses_extraction_date_not_wall_clock():
    conn = sqlite3.connect(':memory:')
    conn.executescript("""
        CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, discovered_at TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY,
                                         event_date TEXT, status TEXT);
        CREATE TABLE EventDeduplicationMap (deduplicated_event_id TEXT, enriched_event_id TEXT);
    """)
    extraction = date(2024, 9, 15)
    events = _events(date(2023, 6, 1), date(2024, 9, 15), per_day=1, lag_days=0)
    for n, (incident, known) in enumerate(events):
        conn.execute("INSERT INTO RawEvents VALUES (?, ?)", (f"r{n}", f"{known.isoformat()}T09:00:00"))
        conn.execute("INSERT INTO EnrichedEvents VALUES (?, ?)", (f"e{n}", f"r{n}"))
        conn.execute("INSERT INTO DeduplicatedEvents VALUES (?, ?, 'Active')", (f"d{n}", incident.isoformat()))
        conn.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?)", (f"d{n}", f"e{n}"))
    result = get_partial_period_estimate(conn)
    assert result['extraction_date'] == extraction.isoformat()
    assert result['period'] == '2024 H2'
    assert result['method'] == 'bornhuetter_ferguson'
    # Zero lag and 1/day: observed Jul 1 - Sep 15 plus R for the rest.
    assert result['observed'] == 77
    assert result['estimate'] == pytest.approx(184, abs=3)
