"""Offline regression tests for recurrence sequence and observation analyses."""
from __future__ import annotations

import sqlite3
from datetime import date

import pandas as pd
import pytest

from scripts.recurrence_extended import sequence_bias as seq


def events():
    return pd.DataFrame([
        {"entity_id": "a", "deduplicated_event_id": "a1", "event_date": "2020-01-01", "event_type": "Ransomware", "org_size": "SMALL"},
        {"entity_id": "a", "deduplicated_event_id": "a2", "event_date": "2020-01-11", "event_type": "Phishing", "org_size": "SMALL"},
        {"entity_id": "a", "deduplicated_event_id": "a3", "event_date": "2020-02-01", "event_type": None, "org_size": "SMALL"},
        {"entity_id": "b", "deduplicated_event_id": "b1", "event_date": "2020-01-01", "event_type": "Data Breach", "org_size": "LARGE"},
        {"entity_id": "b", "deduplicated_event_id": "b2", "event_date": "2020-01-01", "event_type": "Ransomware", "org_size": "LARGE"},
        {"entity_id": "b", "deduplicated_event_id": "b3", "event_date": "2020-03-01", "event_type": None, "org_size": "LARGE"},
        {"entity_id": "b", "deduplicated_event_id": "future", "event_date": "2021-01-01", "event_type": "Ransomware", "org_size": "LARGE"},
    ])


def test_spell_order_and_right_censoring():
    cutoff = date(2020, 4, 1)
    days = seq.prepare_events(events(), cutoff)
    spells = seq.make_spells(days, cutoff)
    rates = seq.order_rates(spells).set_index("event_order")
    first = rates.loc["After first observed incident"]
    later = rates.loc["After second or later observed incident"]
    assert (first["organisations"], first["repeats"], first["censored"], first["exposure_days"]) == (2, 2, 0, 70)
    assert (later["organisations"], later["repeats"], later["censored"], later["exposure_days"]) == (2, 1, 2, 112)
    assert first["poisson_ci_low"] < first["rate_per_100_entity_years"] < first["poisson_ci_high"]
    assert first["exposure_days"] + later["exposure_days"] == 182


def test_same_day_conflict_is_ambiguous_and_missing_attack_stays_unknown():
    cutoff = date(2020, 4, 1)
    days = seq.prepare_events(events(), cutoff)
    b_first = days[(days.entity_id == "b") & (days.event_date == date(2020, 1, 1))].iloc[0]
    assert b_first.event_type == seq.AMBIGUOUS
    transition = seq.transition_table(seq.make_spells(days, cutoff))
    pairs = {(r.prior_attack_type, r.next_attack_type): r.transitions for r in transition.itertuples()}
    assert pairs[("Ransomware", "Phishing")] == 1
    assert pairs[("Phishing", seq.UNKNOWN)] == 1
    assert pairs[(seq.AMBIGUOUS, seq.UNKNOWN)] == 1
    assert len(transition) == 3


def test_source_history_excludes_known_future_sources_without_guessing_undated(tmp_path):
    db = tmp_path / "source.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE RawEvents (raw_event_id TEXT, source_type TEXT, discovered_at TEXT);
        CREATE TABLE EventDeduplicationMap (deduplicated_event_id TEXT, raw_event_id TEXT);
        INSERT INTO RawEvents VALUES ('r1', 'OAIC', '2020-01-02T00:00:00');
        INSERT INTO RawEvents VALUES ('r2', 'Google', '2020-06-01T00:00:00');
        INSERT INTO RawEvents VALUES ('r3', 'Webber', NULL);
        INSERT INTO EventDeduplicationMap VALUES ('a1', 'r1');
        INSERT INTO EventDeduplicationMap VALUES ('a2', 'r2');
        INSERT INTO EventDeduplicationMap VALUES ('a3', 'r3');
    """)
    conn.commit()
    conn.close()
    history, status = seq._source_history(db, date(2020, 4, 1))
    labels, future_only, missing = seq._source_labels(history, date(2020, 4, 1))
    assert status == "available"
    assert labels["a1"] == "OAIC"
    assert labels["a3"] == seq.UNKNOWN
    assert future_only == {"a2"}
    assert missing == 1
    days = seq.prepare_events(events(), date(2020, 4, 1), labels, future_only)
    assert "a2" not in "|".join(days.event_ids)


def test_reporting_scenarios_are_seeded_and_never_thin_first_events():
    days = seq.prepare_events(events(), date(2020, 4, 1))
    thinned = seq._observed_rows(days, __import__("numpy").random.default_rng(1), "lower observation for small organisations")
    assert set(days.groupby("entity_id").first().event_ids).issubset(set(thinned.event_ids))
    first = seq.reporting_sensitivity(days, date(2020, 4, 1), 123, 20)
    second = seq.reporting_sensitivity(days, date(2020, 4, 1), 123, 20)
    pd.testing.assert_frame_equal(first, second)
    assert all(first.p95_recorded_repeats <= seq.make_spells(days, date(2020, 4, 1)).event.sum())


def test_prior_history_scenario_relabels_without_inventing_exposure():
    cutoff = date(2020, 4, 1)
    days = seq.prepare_events(events(), cutoff)
    baseline = seq.make_spells(days, cutoff)
    shifted = seq.make_spells(days, cutoff, {"a": 1, "b": 1})
    assert shifted.exposure_days.sum() == baseline.exposure_days.sum()
    assert shifted.event.sum() == baseline.event.sum()
    assert seq.order_rates(shifted).iloc[0].spells == 0
    assert seq.order_rates(shifted).iloc[1].spells == len(baseline)
    sensitivity = seq.prior_history_sensitivity(days, cutoff, 7, 10)
    assert len(sensitivity) == 6
    assert "unobserved earlier incident" in sensitivity.scenario.iloc[0]


def test_run_writes_complete_outputs_without_database_and_handles_empty_events(tmp_path):
    frame = events().iloc[:0]
    result = seq.run(frame, tmp_path / "missing.db", date(2020, 4, 1), tmp_path / "out", 2, bias_replicates=3)
    assert result["source_history_status"] == "database unavailable"
    assert len(result["outputs"]) == 5
    assert all(__import__("pathlib").Path(p).exists() for p in result["outputs"])
    assert "do not estimate or reconstruct unreported attacks" in result["section"]
    assert not (tmp_path / "missing.db").exists()


def test_invalid_replication_count_is_rejected():
    days = seq.prepare_events(events(), date(2020, 4, 1))
    with pytest.raises(ValueError, match="at least 1"):
        seq.reporting_sensitivity(days, date(2020, 4, 1), 1, 0)
