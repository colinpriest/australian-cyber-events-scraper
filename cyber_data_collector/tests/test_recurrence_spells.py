"""One shared spell builder, and how it treats several incidents of one organisation on one date."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from scripts.analyze_recurrent_timing import build_spells as main_build_spells
from scripts.recurrence_extended import linkage_supplier, sequence_bias, spells, timing_precision


def events(rows):
    """``(entity, incident id, date[, precision])`` rows with the columns every consumer needs."""
    frame = pd.DataFrame([{**dict(zip(("entity_id", "deduplicated_event_id", "event_date", "date_precision"), row)),
                           "entity_name": f"Org {row[0]}", "title": f"Title {row[1]}", "industry": "x",
                           "entity_kind": "organisation", "org_size": "LARGE", "org_size_score": 3.0,
                           "org_size_unknown": 0, "size_confidence": 0.9, "employee_size": "LARGE",
                           "turnover_size": "LARGE", "entity_kind_group": "organisation",
                           "sector_proxy": "other", "records_affected": None, "records_bucket": "unknown",
                           "event_type": "Ransomware"}
                          for row in [r + (None,) * (4 - len(r)) for r in rows]])
    return frame


TIED = [("a", "1", "2024-01-10"), ("a", "2", "2024-01-10"), ("a", "3", "2024-01-10"),
        ("a", "4", "2024-04-10"), ("b", "5", "2024-02-01")]
CENSOR = date(2024, 12, 31)


def test_collapse_makes_a_tie_one_time_point_and_keeps_every_incident():
    points = spells.occurrences(events(TIED), "collapse")
    first = points[(points.entity_id == "a")].iloc[0]
    assert (first.event_ids, first.event_count, first.tie_size) == ("1|2|3", 3, 3)
    assert len(points[points.entity_id == "a"]) == 2
    out = spells.make_spells(points, CENSOR, "collapse")
    a = out[out.entity_id == "a"]
    assert a.event.tolist() == [1, 0]                     # one repeat: the tie itself is not one
    assert a.duration_days.tolist() == [91, 265]
    assert a.next_event_ids.tolist() == ["4", ""]
    assert out.tie_gap.sum() == 0


def test_zero_gap_makes_each_tied_incident_a_repeat_one_day_after_the_previous():
    out = spells.make_spells(spells.occurrences(events(TIED), "zero_gap"), CENSOR, "zero_gap")
    a = out[out.entity_id == "a"]
    assert a.event.tolist() == [1, 1, 1, 0]
    assert a.duration_days.tolist() == [1, 1, 91, 265]
    assert a.tie_gap.tolist() == [1, 1, 0, 0]
    assert a.prior_event_number.tolist() == [1, 2, 3, 4]          # the incidents are counted separately
    assert a.event_ids.tolist() == ["1", "2", "3", "4"]           # ordered by id: an arbitrary order


def test_the_two_policies_differ_by_exactly_the_repeats_in_ties():
    collapse = spells.make_spells(spells.occurrences(events(TIED), "collapse"), CENSOR, "collapse")
    zero = spells.make_spells(spells.occurrences(events(TIED), "zero_gap"), CENSOR, "zero_gap")
    tie_table = spells.tie_table(events(TIED))
    assert zero.event.sum() - collapse.event.sum() == (tie_table.incident_count - 1).sum() == 2


def test_an_unknown_date_is_dropped_never_given_a_day():
    frame = events(TIED + [("a", "6", None)])
    assert "6" not in "|".join(spells.occurrences(frame, "collapse").event_ids)
    assert "6" not in "|".join(spells.occurrences(frame, "zero_gap").event_ids)


def test_an_incident_after_the_censoring_date_never_ends_a_spell():
    frame = events([("a", "1", "2024-01-10"), ("a", "2", "2024-05-10")])
    for policy in spells.TIE_POLICIES:
        out = spells.make_spells(spells.occurrences(frame, policy), date(2024, 3, 1), policy)
        assert (out.event.tolist(), out.duration_days.tolist(), out.next_event_ids.tolist()) == ([0], [51], [""])


def test_a_tie_on_the_censoring_date_is_one_point_under_collapse_and_an_observed_repeat_otherwise():
    frame = events([("a", "1", "2024-03-01"), ("a", "2", "2024-03-01")])
    censor = date(2024, 3, 1)
    assert spells.make_spells(spells.occurrences(frame, "collapse"), censor, "collapse").empty
    for policy in ("zero_gap", "interval_spread"):
        out = spells.make_spells(spells.occurrences(frame, policy), censor, policy)
        # both incidents are on or before the censoring date, so the repeat between them is observed
        assert (out.event.tolist(), out.duration_days.tolist(), out.tie_repeat.tolist()) == ([1], [1], [1])


def test_a_repeat_into_a_tie_on_the_censoring_date_and_the_one_inside_it_are_both_kept():
    frame = events([("a", "0", "2024-01-01"), ("a", "1", "2024-03-01"), ("a", "2", "2024-03-01")])
    out = spells.make_spells(spells.occurrences(frame, "zero_gap"), date(2024, 3, 1), "zero_gap")
    assert out.event.tolist() == [1, 1]
    assert out.duration_days.tolist() == [60, 1]
    assert out.tie_repeat.tolist() == [0, 1]


def month_tie(rows, earliest="2024-06-01", latest="2024-06-30"):
    frame = events(rows)
    frame["event_date_earliest"] = [earliest if p == "month" else (d if p == "day" else None)
                                    for _, _, d, p in rows]
    frame["event_date_latest"] = [latest if p == "month" else (d if p == "day" else None) for _, _, d, p in rows]
    return frame


def test_interval_spread_places_a_verified_month_tie_evenly_across_the_month():
    frame = month_tie([("a", "1", "2024-06-01", "month"), ("a", "2", "2024-06-01", "month"),
                       ("a", "3", "2024-06-01", "month"), ("a", "4", "2024-09-10", "day")])
    out = spells.make_spells(spells.occurrences(frame, "interval_spread"), CENSOR, "interval_spread")
    assert [d.isoformat() for d in out.placed_date] == ["2024-06-01", "2024-06-11", "2024-06-21", "2024-09-10"]
    assert out.duration_days.tolist() == [10, 10, 81, 112]      # 30 days / 3 incidents; the last spell from 21 June
    assert out.tie_repeat.tolist() == [1, 1, 0, 0]
    assert out.tie_gap.sum() == 0
    zero = spells.make_spells(spells.occurrences(frame, "zero_gap"), CENSOR, "zero_gap")
    assert zero.event.sum() == out.event.sum()                  # the same repeats, at different elapsed times
    ties = spells.tie_table(frame)
    assert ties.shared_interval_days.tolist() == [30]
    assert spells.tie_summary(ties)[
        "ties with a shared verified interval longer than a day (spread by interval_spread)"] == 1


def test_interval_spread_is_zero_gap_for_a_tie_with_no_shared_verified_interval():
    rows = [("a", "1", "2024-06-01", None), ("a", "2", "2024-06-01", "month"), ("b", "3", "2024-06-05", "day"),
            ("b", "4", "2024-06-05", "day"), ("b", "5", "2024-08-01", None)]
    frame = month_tie(rows)
    spread = spells.make_spells(spells.occurrences(frame, "interval_spread"), CENSOR, "interval_spread")
    zero = spells.make_spells(spells.occurrences(frame, "zero_gap"), CENSOR, "zero_gap")
    columns = ["entity_id", "event_ids", "duration_days", "event", "placed_date"]
    pd.testing.assert_frame_equal(spread[columns], zero[columns])
    shared = spells.tie_table(frame).set_index("entity_id").shared_interval_days
    assert pd.isna(shared["a"]) and shared["b"] == 1          # unverified: no interval; a verified day: 1 day


def test_interval_spread_never_places_an_incident_after_the_censoring_date():
    frame = month_tie([("a", "1", "2024-06-01", "month"), ("a", "2", "2024-06-01", "month")])
    out = spells.make_spells(spells.occurrences(frame, "interval_spread"), date(2024, 6, 10), "interval_spread")
    assert [d.isoformat() for d in out.placed_date] == ["2024-06-01", "2024-06-06"]   # 10 days to the censor / 2
    assert out.event.tolist() == [1, 0] and out.duration_days.tolist() == [5, 4]


def test_interval_draw_needs_an_rng_and_stays_inside_the_shared_interval():
    frame = month_tie([("a", "1", "2024-06-01", "month"), ("a", "2", "2024-06-01", "month")])
    points = spells.occurrences(frame, "interval_draw")
    with pytest.raises(ValueError):
        spells.make_spells(points, CENSOR, "interval_draw")
    rng = np.random.default_rng(4)
    for _ in range(20):
        out = spells.make_spells(points, CENSOR, "interval_draw", rng=rng)
        assert all(date(2024, 6, 1) <= d <= date(2024, 6, 30) for d in out.placed_date)
        assert out.event.sum() == 1
    assert "interval_draw" not in spells.TIE_POLICIES and "interval_draw" in spells.ALL_TIE_POLICIES


def test_every_policy_states_its_assumption_and_none_claims_to_bound_the_truth():
    assert set(spells.TIE_POLICY_ASSUMPTIONS) == set(spells.ALL_TIE_POLICIES)
    assert "between the two" not in (spells.__doc__ or "")
    assert "None of these bounds the truth" in (spells.__doc__ or "")


def test_unknown_policy_is_an_error():
    with pytest.raises(ValueError):
        spells.occurrences(events(TIED), "guess")


def test_ties_are_reported_by_kind_and_month_placeholders_are_separate():
    rows = [("a", "1", "2024-06-01", "month"), ("a", "2", "2024-06-01", "month"),       # month placeholders
            ("b", "3", "2024-06-01", "month"), ("b", "4", "2024-06-01", None),          # a 1st, not all verified
            ("c", "5", "2024-06-17", "day"), ("c", "6", "2024-06-17", "day"),           # a real shared day
            ("d", "7", "2024-06-01", "day")]                                            # no tie
    ties = spells.tie_table(events(rows)).set_index("entity_id")
    assert ties.loc["a", "kind"] == "month_precision_first_of_month"
    assert ties.loc["b", "kind"] == "first_of_month_not_month_verified"
    assert ties.loc["c", "kind"] == "other_day"
    assert "d" not in ties.index
    summary = spells.tie_summary(ties.reset_index())
    assert summary["ties (organisation-dates with several incidents)"] == 3
    assert summary["ties: month_precision_first_of_month"] == 1
    assert summary["incidents in ties"] == 6


def test_a_tie_table_with_no_ties_is_empty_and_summarised_as_zero():
    ties = spells.tie_table(events([("a", "1", "2024-06-01"), ("a", "2", "2024-07-01")]))
    assert ties.empty
    assert spells.tie_summary(ties)["ties (organisation-dates with several incidents)"] == 0


def test_incidents_whose_intervals_overlap_on_different_stored_dates_are_counted_separately():
    frame = events([("a", "1", "2024-06-01", "month"), ("a", "2", "2024-06-20", "day"), ("a", "3", "2024-09-01", "month"),
                    ("b", "4", "2024-06-01", None), ("b", "5", "2024-06-05", None)])
    frame["event_date_earliest"] = ["2024-06-01", "2024-06-20", "2024-09-01", None, None]
    frame["event_date_latest"] = ["2024-06-30", "2024-06-20", "2024-09-30", None, None]
    counts = spells.overlapping_interval_pairs(frame)
    assert counts["consecutive pairs with overlapping verified intervals"] == 1       # a1/a2; b has no intervals
    assert counts["... of which stored dates differ by under 90 days"] == 1


# --- the same spells wherever they are built ------------------------------------------------------------

@pytest.mark.parametrize("policy", spells.TIE_POLICIES)
def test_every_consumer_counts_the_same_repeats(policy):
    frame = events(TIED)
    frame["_analysis_size"] = "LARGE"
    expected = int(spells.make_spells(spells.occurrences(frame, policy), CENSOR, policy).event.sum())
    main, _ = main_build_spells(frame, CENSOR, policy)
    timing = timing_precision.make_spells(frame, CENSOR, policy)
    sequence = sequence_bias.make_spells(sequence_bias.prepare_events(frame, CENSOR, tie_policy=policy), CENSOR,
                                         tie_policy=policy)
    assert int(main.event.sum()) == int(timing.event.sum()) == int(sequence.event.sum()) == expected
    assert main.duration_days.tolist() == timing.duration_days.tolist()
    # linkage: repeats inside 90 days (0-90 events) plus 91-180 days
    linkage_frame = linkage_supplier._prepare(frame, CENSOR)
    totals = linkage_supplier._entity_totals(linkage_frame, CENSOR, policy)
    in_180 = int(main[(main.event == 1) & (main.duration_days <= 180)].shape[0])
    assert int(totals.early_events.sum() + totals.late_events.sum()) == in_180


def test_the_main_analysis_keeps_the_incident_ids_and_counts_of_a_tie():
    main, ties = main_build_spells(events(TIED), CENSOR, "collapse")
    first = main[main.entity_id == "a"].iloc[0]
    assert (first.prior_event_ids, first.prior_event_count) == ("1|2|3", 3)
    assert (first.next_event_ids, first.next_event_count) == ("4", 1)
    assert ties.incident_count.tolist() == [3]
    zero, _ = main_build_spells(events(TIED), CENSOR, "zero_gap")
    assert zero[zero.entity_id == "a"].tie_gap.sum() == 2


def test_sequence_ties_stay_ambiguous_under_collapse_and_become_ordered_transitions_under_zero_gap():
    frame = events(TIED)
    frame.loc[frame.deduplicated_event_id == "2", "event_type"] = "Phishing"
    collapsed = sequence_bias.prepare_events(frame, CENSOR)
    assert collapsed[(collapsed.entity_id == "a")].iloc[0].event_type == sequence_bias.AMBIGUOUS
    separate = sequence_bias.prepare_events(frame, CENSOR, tie_policy="zero_gap")
    assert len(separate[separate.entity_id == "a"]) == 4


# --- dates: intervals, unverified dates stay unknown -------------------------------------------------------

def annotated(rows, **extra):
    frame = events(rows)
    for key, values in extra.items():
        frame[key] = values
    return timing_precision.annotate_precision(frame, {})


def test_annotate_uses_the_stored_interval_and_gives_unverified_dates_none():
    frame = annotated([("a", "1", "2024-06-01", "month"), ("a", "2", "2024-06-01", None), ("a", "3", "2024-06-17", None)],
                      event_date_earliest=["2024-06-01", None, None], event_date_latest=["2024-06-30", None, None])
    assert frame.date_precision_basis.tolist() == ["explicit_month", "ambiguous_first_of_month", "unknown"]
    assert frame.date_earliest.tolist()[0] == date(2024, 6, 1) and frame.date_latest.tolist()[0] == date(2024, 6, 30)
    assert frame.date_earliest.tolist()[1:] == [None, None]          # unknown, not a one-day interval


def test_imputation_draws_within_the_stored_interval_not_one_rebuilt_from_the_date():
    frame = annotated([("a", "1", "2024-06-01", "month")],
                      event_date_earliest=["2024-06-10"], event_date_latest=["2024-06-14"])
    for seed in range(20):
        drawn = timing_precision.impute_dates(frame, np.random.default_rng(seed), "keep", date(2026, 1, 1))
        assert date(2024, 6, 10) <= drawn.iloc[0].event_date <= date(2024, 6, 14)


def test_unverified_dates_are_not_imputed_unless_the_scenario_is_hypothetical():
    frame = annotated([("a", "1", "2024-06-01", None), ("a", "2", "2024-09-17", None)])
    kept = timing_precision.impute_dates(frame, np.random.default_rng(1), "keep", date(2026, 1, 1))
    assert kept.event_date.tolist() == [date(2024, 6, 1), date(2024, 9, 17)]
    hypothetical = timing_precision.impute_dates(frame, np.random.default_rng(1), "month", date(2026, 1, 1))
    assert hypothetical.iloc[1].event_date == date(2024, 9, 17)      # a non-1st unverified date is never moved


def test_complete_histories_leave_out_organisations_with_an_unverified_date():
    frame = annotated([("a", "1", "2024-06-01", "month"), ("a", "2", "2024-09-05", "day"),
                       ("b", "3", "2024-06-01", "month"), ("b", "4", "2024-09-17", None)])
    kept = timing_precision.complete_histories(frame, "interval")
    assert set(kept.entity_id) == {"a"}                                # b's unverified date is not read as a day
    assert set(timing_precision.complete_histories(frame, "day").entity_id) == set()


def test_date_scenarios_include_the_verified_interval_scenario():
    frame = annotated([("a", "1", "2024-06-01", "month"), ("a", "2", "2024-12-05", "day"),
                       ("b", "3", "2024-06-01", "month"), ("b", "4", "2024-12-17", None)])
    table = timing_precision.date_sensitivity(frame, CENSOR, seed=3, draws=4)
    assert "verified_intervals_complete_histories" in set(table.scenario)
    assert {"stored_dates", "known_intervals", "explicit_day_only_complete_histories"} <= set(table.scenario)


def test_tie_policy_sensitivity_reports_both_policies():
    frame = annotated(TIED)
    frame["_analysis_size"] = "LARGE"
    table = timing_precision.tie_policy_sensitivity(frame, CENSOR, {"LARGE": 1.0}, seed=1, draws=3)
    assert set(table.tie_policy) == set(spells.ALL_TIE_POLICIES)
    assert table.assumption.notna().all()
    assert set(table.loc[table.tie_policy == "interval_draw", "draws"]) == {3}
    first_band = table[(table.scheme == "pre_specified") & (table.band == "1-90")].set_index("tie_policy")
    assert first_band.loc["zero_gap", "events"] - first_band.loc["collapse", "events"] == 2
