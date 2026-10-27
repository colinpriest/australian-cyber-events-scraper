"""Cut-off selection for the event-reality filter."""

from __future__ import annotations

import pytest

from scripts.calibrate_event_reality import choose_cutoff, wilson


def _rows(spec):
    """spec: [(p, not_incident, weight)]"""
    return [{"p": p, "not_incident": n, "weight": w} for p, n, w in spec]


def test_largest_cutoff_meeting_precision_is_chosen():
    rows = _rows([(0.01, True, 1), (0.02, True, 1), (0.03, True, 1), (0.10, True, 1),
                  (0.20, False, 1), (0.30, True, 1), (0.90, False, 20)])
    chosen, _ = choose_cutoff(rows, precision=0.95, min_rejections=1)
    # Rejecting below 0.2 is all correct; reaching 0.2 lets a real event in (4/5 = 0.8).
    assert 0.10 < chosen["cutoff"] <= 0.10 + 1e-5
    assert chosen["correct_labelled"] == 4 and chosen["weighted_precision"] == 1.0


def test_weights_count_in_precision():
    # One real event from a heavily weighted stratum outweighs several right rejections.
    rows = _rows([(0.01, True, 1), (0.02, True, 1), (0.03, False, 20)])
    chosen, table = choose_cutoff(rows, precision=0.95, min_rejections=1)
    assert chosen["cutoff"] < 0.03
    at_all = [t for t in table if t["rejected_labelled"] == 3][0]
    assert at_all["weighted_precision"] == pytest.approx(2 / 22)


def test_no_cutoff_when_precision_is_never_met():
    rows = _rows([(0.01, False, 1), (0.02, True, 1)])
    chosen, _ = choose_cutoff(rows, precision=0.95, min_rejections=1)
    assert chosen is None


def test_minimum_rejections_guards_against_a_lucky_few():
    rows = _rows([(0.01, True, 1), (0.02, True, 1)])
    chosen, _ = choose_cutoff(rows, precision=0.95, min_rejections=10)
    assert chosen is None


def test_wilson_interval():
    lo, hi = wilson(19, 20)
    assert lo == pytest.approx(0.7639, abs=1e-4) and hi == pytest.approx(0.9911, abs=1e-4)
