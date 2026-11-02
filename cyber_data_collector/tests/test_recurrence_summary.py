"""The recurrence page's executive summary (owner, 2026-11-02): five questions answered from parsimonious models,
size as a monotone effect, and no direction stated for a result the data cannot distinguish from none."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from scripts import recurrence_summary as rs
from scripts.analyze_recurrent_timing import fit_parametric_models

CENSOR = date(2026, 11, 1)
RATE_PER_DAY = {"SMALL": 0.0002, "MEDIUM": 0.0002, "LARGE": 0.0006, "HUGE": 0.0015}


def _spells(rates, n_per_size=600, follow_up=2000.0, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for size, rate in rates.items():
        times = rng.exponential(1 / rate, n_per_size)
        censor = rng.uniform(100, follow_up, n_per_size)
        for t, c in zip(times, censor):
            start = CENSOR - timedelta(days=int(c))
            rows.append({"org_size": size, "event_number_cat": "1", "duration_days": max(1.0, min(t, c)),
                         "event": int(t <= c), "prior_event_date": start.isoformat()})
    return pd.DataFrame(rows)


def _summary(spells, replicates=99):
    return rs.executive_summary(spells, fit_parametric_models(spells), pd.DataFrame(),
                                {"day": len(spells)}, replicates=replicates, seed=3)


def _answer(summary, key):
    return next(q["answer"] for q in summary["questions"] if q["key"] == key)


def test_a_monotone_size_effect_is_found_with_its_direction():
    summary = _summary(_spells(RATE_PER_DAY))
    model = summary["size_model"]
    assert model["direction"] == "increasing"
    assert model["bootstrap_p"] < 0.05
    ratios = list(model["rate_ratio_vs_small"].values())
    assert ratios == sorted(ratios)                       # monotone by construction
    assert _answer(summary, "size").startswith("Yes: the recurrence rate rises with organisation size")


def test_the_constraint_pools_a_band_that_breaks_the_order():
    """MEDIUM below SMALL: the monotone fit gives them one rate instead of a dip no ordering allows."""
    rates = dict(RATE_PER_DAY, MEDIUM=0.00008)
    fit = rs.monotone_size_fit(rs.monthly_table(_spells(rates, seed=4), rs.CANDIDATE_CUTS))
    assert fit["unconstrained_rate_ratio_vs_small"]["MEDIUM"] < 1
    assert fit["rate_ratio_vs_small"]["MEDIUM"] == pytest.approx(1.0, abs=1e-3)
    assert fit["constraint_binds"]


def test_no_size_effect_and_a_constant_rate_are_reported_without_a_direction():
    summary = _summary(_spells({s: 0.0005 for s in rs.SIZE_ORDER}, seed=7))
    size = _answer(summary, "size")
    assert size.startswith("No evidence that organisation size predicts recurrence")
    splits = _answer(summary, "splits")
    assert splits.startswith("No split is supported")
    for key in ("size", "splits"):
        text = _answer(summary, key)
        assert "higher" not in text and "lower" not in text and "rises" not in text


def test_a_window_with_few_repeats_is_not_tested():
    summary = _summary(_spells({s: 0.00005 for s in rs.SIZE_ORDER}, n_per_size=150, seed=2), replicates=19)
    main, narrow = summary["delayed_peak"]
    assert (main["window"], narrow["window"]) == ("91-180", "91-120")
    assert main["repeats_in_window"] < rs.MIN_REPEATS_PER_BAND
    assert _answer(summary, "delayed_peak").startswith("Cannot be tested reliably")


def test_a_real_first_year_rise_is_found_by_the_search():
    rng = np.random.default_rng(11)
    rows = []
    for size in rs.SIZE_ORDER:
        for _ in range(700):
            # 0.002 per day in the first year, 0.0003 after
            t = rng.exponential(1 / 0.002)
            if t > 365:
                t = 365 + rng.exponential(1 / 0.0003)
            c = rng.uniform(200, 2500)
            rows.append({"org_size": size, "event_number_cat": "1", "duration_days": max(1.0, min(t, c)),
                         "event": int(t <= c), "prior_event_date": (CENSOR - timedelta(days=int(c))).isoformat()})
    summary = _summary(pd.DataFrame(rows))
    assert 365 in summary["band_search"]["selected_cuts"]
    assert summary["band_search"]["search_adjusted_p"] < 0.05
    assert _answer(summary, "splits").startswith("Best bands:")


def test_the_full_models_unidentified_terms_make_it_unusable_for_conclusions():
    coefficients = pd.DataFrame({"term": ["intercept", "org_size_unknown", "sector:x"],
                                 "std_error": [161.0, 9434.0, 0.3]})
    full = rs.adjusted_model_parameters(coefficients, repeats=20)
    assert full["not_identified"] == ["intercept", "org_size_unknown"]
    summary = rs.executive_summary(_spells(RATE_PER_DAY), [], coefficients, {"day": 10, "unverified": 30},
                                   replicates=0)
    adequacy = _answer(summary, "adequacy")
    assert "full adjusted model is over-parameterised (3 parameters" in adequacy
    assert "75% of incident dates are unverified" in adequacy


def test_too_few_repeats_is_not_adequate():
    summary = _summary(_spells({s: 0.00003 for s in rs.SIZE_ORDER}, n_per_size=100, seed=5), replicates=0)
    assert _answer(summary, "adequacy").startswith("Not adequate for a reliable recurrence model")


def test_the_page_opens_with_the_five_questions():
    from scripts.build_recurrence_dashboard import executive_summary_html

    summary = {"executive_summary": {"questions": [
        {"key": "adequacy", "question": "Is the data adequate?", "answer": "Adequate <b>", "adequate": True},
        {"key": "size", "question": "Size?", "answer": "No evidence."}]}}
    page = executive_summary_html(summary)
    assert page.count('class="q') == 2 and "Adequate &lt;b&gt;" in page and 'class="q ok"' in page
    assert "rerun" in executive_summary_html({})


def test_growth_in_calendar_time_is_not_read_as_an_elapsed_time_pattern():
    """Owner, 2026-11-02: the long-term increase in recorded incidents must not pass for a time-since-event effect.
    The per-organisation rate here rises 40% a year with calendar time and does not depend on elapsed time; old
    events contribute long gaps that end in the high-rate recent years, so without a calendar term a banding that
    puts long gaps apart wins."""
    rng = np.random.default_rng(21)
    rows = []
    origin = date(2010, 1, 1)
    horizon = (CENSOR - origin).days
    for _ in range(4000):
        start = int(rng.uniform(0, horizon - 30))
        day, event = start, 0
        while day < horizon:                                    # thinning, a year at a time
            rate = 0.00008 * np.exp(0.40 * (day / 365.25))
            gap = rng.exponential(1 / rate)
            if gap < 365 and day + gap < horizon:
                day, event = day + gap, 1
                break
            day += 365
        end = min(day, horizon)
        rows.append({"org_size": rng.choice(rs.SIZE_ORDER), "event_number_cat": "1",
                     "duration_days": max(1.0, end - start), "event": event,
                     "prior_event_date": (origin + timedelta(days=start)).isoformat()})
    spells = pd.DataFrame(rows)
    fine = rs.monthly_table(spells, rs.CANDIDATE_CUTS)
    robustness = rs.calendar_robustness(fine, [365, 1095]).set_index("calendar")
    assert robustness.loc["none", "bands_p"] < 0.05             # the confounded reading
    assert robustness.loc["linear_trend", "bands_p"] > 0.05     # gone once calendar time is held
    assert robustness.loc["linear_trend", "calendar_p"] < 0.001
