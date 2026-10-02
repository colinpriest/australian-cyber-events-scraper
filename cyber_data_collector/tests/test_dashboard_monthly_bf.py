"""Tests for the per-month Bornhuetter-Ferguson estimates on the monthly event-count chart."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Tuple

from scripts.build_static_dashboard import (
    bf_month_intervals,
    compute_monthly_bf_estimates,
    rate_months,
    reporting_lag_sample,
)

E = date(2026, 10, 2)
START = date(2024, 1, 1)


def _events(per_day: int, lags: List[int], start: date = START, end: date = E) -> List[Tuple[date, date]]:
    """per_day incidents a day in [start, end], lags cycling through ``lags``."""
    out = []
    d, k = start, 0
    while d <= end:
        for _ in range(per_day):
            out.append((d, d + timedelta(days=lags[k % len(lags)])))
            k += 1
        d += timedelta(days=1)
    return out


def _counts(events: List[Tuple[date, date]], extraction: date) -> Dict[Tuple[int, int], int]:
    """Monthly counts of incidents already known at extraction."""
    counts: Dict[Tuple[int, int], int] = {}
    for incident, known in events:
        if incident <= extraction and known <= extraction:
            key = (incident.year, incident.month)
            counts[key] = counts.get(key, 0) + 1
    return counts


def _estimate(events, **kwargs):
    return compute_monthly_bf_estimates(_counts(events, E), events, E, **kwargs)


def test_only_under_reported_months_get_estimates():
    # Lags spread 0-120 days: months older than ~4 months are fully reported.
    events = _events(1, list(range(0, 121, 5)))
    result = _estimate(events)
    assert result is not None
    months = [m['month'] for m in result['months']]
    assert months[-1] == '2026-10'          # the partial month at E is included
    assert months == sorted(months)
    assert all(m['reported_fraction'] < 0.95 for m in result['months'])
    # The month just before the earliest estimated one is at/above the threshold.
    first = date(int(months[0][:4]), int(months[0][5:]), 1)
    assert '2026-04' not in months and '2026-01' not in months
    assert first > date(2026, 4, 1)


def test_zero_lag_gives_only_the_partial_month():
    # Everything is known on the day it happens: every complete month is 100%
    # reported, only the month containing E is still under-reported.
    events = _events(2, [0])
    result = _estimate(events)
    assert [m['month'] for m in result['months']] == ['2026-10']


def test_partial_month_estimate_at_least_observed_and_interval_contains_it():
    events = _events(1, list(range(0, 121, 5)))
    result = _estimate(events)
    for m in result['months']:
        assert m['estimate'] >= m['observed']
        assert m['low'] <= m['estimate'] <= m['high']
        assert m['low'] >= m['observed']        # unreported draws are never negative
    partial = result['months'][-1]
    assert partial['month'] == '2026-10'
    assert partial['estimate'] > partial['observed']


def test_estimates_are_deterministic():
    events = _events(1, list(range(0, 121, 5)))
    assert _estimate(events) == _estimate(events)


def test_fallback_returns_none_when_too_few_developed_incidents():
    # Collection began recently, so no incident is both developed and collected.
    events = _events(1, [3], start=date(2026, 6, 1))
    assert _estimate(events) is None


def test_month_intervals_seeded_and_bracket_observed():
    events = _events(1, list(range(0, 121, 5)))
    counts = _counts(events, E)
    lags = reporting_lag_sample(events, E)
    months = [(2026, 8), (2026, 9), (2026, 10)]
    a = bf_month_intervals(counts, months, E, lags, rate_months(E), n_boot=200, seed=7)
    b = bf_month_intervals(counts, months, E, lags, rate_months(E), n_boot=200, seed=7)
    assert a == b
    assert len(a) == 3
    for (low, high), pm in zip(a, months):
        assert counts.get(pm, 0) <= low <= high
    assert bf_month_intervals(counts, [], E, lags, rate_months(E)) == []
