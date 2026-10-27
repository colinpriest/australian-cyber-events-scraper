from datetime import date
import json
import sqlite3

import numpy as np
import pandas as pd

from scripts.recurrence_extended import timing_precision as timing


def events(rows):
    return pd.DataFrame(rows, columns=["entity_id", "deduplicated_event_id", "event_date", "date_precision"])


def test_censoring_band_exposure_and_same_day_deduplication():
    frame = events([
        ("a", "1", "2025-01-01", None), ("a", "2", "2025-04-11", None),
        ("a", "3", "2025-04-11", None), ("a", "4", "2026-03-01", None),
    ])
    spells = timing.make_spells(frame, date(2025, 6, 1))
    assert spells["duration_days"].tolist() == [100, 51]
    assert spells["event"].tolist() == [1, 0]
    bands = timing.band_rates(spells, timing.PEAK_BOUNDS)
    assert bands["events"].tolist() == [0, 1, 0]
    assert bands["exposure_days"].tolist() == [141.0, 10.0, 0.0]


def test_precision_and_censor_resampling_preserves_shared_incident():
    frame = events([
        ("a", "1", "2026-10-01", None), ("b", "1", "2026-10-01", None),
        ("c", "2", "2026-01-01", "year"), ("d", "3", "2026-01-15", None),
        ("e", "4", "2026-02-01", "day"),
    ])
    annotated = timing.annotate_precision(frame, {})
    assert annotated["date_precision_basis"].tolist() == ["ambiguous_first_of_month"] * 2 + ["explicit_year", "unknown", "explicit_day"]
    for seed in range(15):
        sampled = timing.impute_dates(annotated, np.random.default_rng(seed), "month", date(2026, 10, 5))
        assert sampled.iloc[0]["event_date"] == sampled.iloc[1]["event_date"]
        assert date(2026, 10, 1) <= sampled.iloc[0]["event_date"] <= date(2026, 10, 5)
        assert sampled.iloc[2]["event_date"] <= date(2026, 10, 5)
        assert sampled.iloc[4]["event_date"] == date(2026, 2, 1)


def test_precise_subset_requires_complete_entity_histories():
    frame = timing.annotate_precision(events([
        ("a", "1", "2025-01-01", "day"), ("a", "2", "2025-02-01", None),
        ("a", "3", "2025-04-01", "day"), ("b", "4", "2025-01-01", "day"),
        ("b", "5", "2025-04-11", "day"),
    ]), {})
    result = timing.date_sensitivity(frame, date(2025, 6, 1), 2, draws=5)
    exact = result[result.scenario == "explicit_day_only_complete_histories"].set_index("band")
    # Entity a is entirely excluded: retaining just its exact dates creates a false gap.
    assert exact.loc["91-180", "median_rate_per_100_entity_years"] == 3652.5


def test_smooth_curve_reproducible_with_cluster_bootstrap():
    frame = events([("a", "1", "2025-01-01", None), ("a", "2", "2025-04-11", None),
                    ("b", "3", "2025-01-01", None), ("c", "4", "2025-02-01", None)])
    spells = timing.make_spells(frame, date(2026, 1, 1))
    first = timing.smooth_hazard(spells, seed=19, bootstrap=20)
    pd.testing.assert_frame_equal(first, timing.smooth_hazard(spells, seed=19, bootstrap=20))
    assert first.rate_per_100_entity_years.gt(0).any()
    assert first.loc[first.day <= 365, "bootstrap_ci_low"].notna().all()
    assert first.loc[first.day > 700, "bootstrap_ci_low"].isna().all()


def test_evidence_must_match_date_and_exist_by_as_of(tmp_path):
    db = tmp_path / "test.db"
    with sqlite3.connect(db) as conn:
        conn.executescript('''
            CREATE TABLE EventDeduplicationMap (deduplicated_event_id TEXT, enriched_event_id TEXT);
            CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT, event_date TEXT);
            CREATE TABLE EnrichedEvents (enriched_event_id TEXT, raw_event_id TEXT, event_date TEXT, perplexity_enrichment_data TEXT);
            CREATE TABLE RawEvents (raw_event_id TEXT, event_date TEXT, discovered_at TEXT, source_metadata TEXT);
            INSERT INTO DeduplicatedEvents VALUES ('d','2025-01-01');
            INSERT INTO EventDeduplicationMap VALUES ('d','e1'), ('d','e2');
            INSERT INTO EnrichedEvents VALUES ('e1','r1','2025-01-01',NULL), ('e2','r2','2025-01-01',NULL);
            INSERT INTO RawEvents VALUES ('r1','2025-01-01','2025-03-01','{"event_date_precision":"month"}'),
              ('r2','2025-01-01','2026-03-01','{"event_date_precision":"day"}');
        ''')
    before = db.read_bytes()
    evidence, seen = timing._read_evidence(db, date(2025, 12, 31))
    assert evidence == {"d": {"month"}}
    assert seen == {"d": date(2025, 3, 1)}
    assert db.read_bytes() == before


def test_late_first_run_cannot_claim_prospective_validation(tmp_path, monkeypatch):
    class LaterDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 28)
    monkeypatch.setattr(timing, "date", LaterDate)
    frame = timing.annotate_precision(events([("a", "1", "2026-10-01", None)]), {})
    result = timing.prospective_validation(frame, {"1": date(2026, 10, 2)}, date(2026, 10, 28), tmp_path)
    assert result["status"] == "unavailable"
    assert not (tmp_path / "timing_peak_registration.json").exists()


def test_registered_cohort_only_uses_post_registration_discoveries(tmp_path, monkeypatch):
    class RegisteredDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 27)
    monkeypatch.setattr(timing, "date", RegisteredDate)
    frame = timing.annotate_precision(events([("a", "1", "2026-09-01", None)]), {})
    result = timing.prospective_validation(frame, {"1": date(2026, 9, 2)}, date(2026, 10, 2), tmp_path)
    assert result["status"] == "pending"
    assert result["snapshot_cutoff"] == "2026-10-02"
    frame = timing.annotate_precision(events([("a", "1", "2026-09-01", None),
        ("a", "2", "2026-11-15", None), ("a", "3", "2026-12-15", None)]), {})
    result = timing.prospective_validation(frame, {"1": date(2026, 9, 2),
        "2": date(2026, 10, 20), "3": date(2026, 12, 16)}, date(2027, 1, 1), tmp_path)
    assert result["events_by_band"] == [0, 1, 0]
    assert result["one_sided_p_value"] is None


def test_run_handles_no_precision_metadata(tmp_path, monkeypatch):
    class BeforeRegistration(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 1)
    monkeypatch.setattr(timing, "date", BeforeRegistration)
    frame = events([("a", "1", "2025-01-01", None), ("a", "2", "2025-04-11", None),
                    ("b", "3", "2025-01-01", None)])
    result = timing.run(frame, tmp_path / "missing.db", date(2025, 6, 1), tmp_path / "output", 42,
                        date_imputations=3, smooth_bootstrap=5)
    json.dumps(result, allow_nan=False)
    assert result["precision_counts"] == {"ambiguous_first_of_month": 2, "unknown": 1}
    assert result["validation"]["status"] == "pending_registration"
    assert all((tmp_path / "output" / filename).exists() for filename in result["outputs"])


def test_size_standardization_and_missing_stratum_are_explicit():
    spells = pd.DataFrame([
        {"entity_id": "s1", "size": "SMALL", "duration_days": 90, "event": 1},
        {"entity_id": "s2", "size": "SMALL", "duration_days": 180, "event": 0},
        {"entity_id": "h1", "size": "HUGE", "duration_days": 90, "event": 0},
        {"entity_id": "h2", "size": "HUGE", "duration_days": 180, "event": 1},
    ])
    result, strata = timing.size_adjusted_bands(spells, (0, 90, 180), {"SMALL": .5, "HUGE": .5})
    assert np.isclose(result.iloc[0].size_adjusted_rate_per_100_entity_years, .5 * 36525 / 180)
    assert np.isclose(result.iloc[1].size_adjusted_rate_per_100_entity_years, .5 * 36525 / 90)
    assert strata.loc[(strata['size'] == 'HUGE') & (strata['band'] == '91-180'), 'events'].iloc[0] == 1
    unavailable, _ = timing.size_adjusted_bands(spells[spells['size'] == 'SMALL'], (0, 90), {"SMALL": .5, "HUGE": .5})
    assert pd.isna(unavailable.iloc[0].size_adjusted_rate_per_100_entity_years)
    assert unavailable.iloc[0].unsupported_size_strata == 'HUGE'


def test_conflicting_sizes_are_unknown_and_weights_are_entity_based():
    frame = pd.DataFrame({'entity_id': ['a', 'a', 'b', 'c'], 'org_size': ['SMALL', 'UNKNOWN', 'HUGE', 'HUGE']})
    assert timing.size_labels(frame).tolist() == ['UNKNOWN', 'UNKNOWN', 'HUGE', 'HUGE']
    weights = timing.standard_size_weights(frame)
    assert np.isclose(weights['UNKNOWN'], 1 / 3)
    assert np.isclose(weights['HUGE'], 2 / 3)


def test_prospective_size_cohort_is_frozen_separately(tmp_path, monkeypatch):
    class RegisteredDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 27)
    monkeypatch.setattr(timing, 'date', RegisteredDate)
    frame = timing.annotate_precision(events([('a', '1', '2026-09-01', None)]), {})
    frame['org_size'] = 'LARGE'
    first = timing.prospective_size_validation(frame, {'1': date(2026, 9, 2)}, date(2026, 10, 27), tmp_path)
    assert first['status'] == 'pending'
    assert (tmp_path / 'timing_size_adjusted_registration.json').exists()
    assert not (tmp_path / 'timing_peak_registration.json').exists()
    frame = timing.annotate_precision(events([('a', '1', '2026-09-01', None), ('a', '2', '2026-12-15', None)]), {})
    frame['org_size'] = 'SMALL'  # Later changes must not change the frozen size.
    second = timing.prospective_size_validation(frame, {'1': date(2026, 9, 2), '2': date(2026, 12, 16)}, date(2027, 1, 1), tmp_path)
    large = next(row for row in second['size_strata'] if row['size'] == 'LARGE')
    assert large['entities'] == 1 and large['events'] == [0, 1, 0]
    assert second['one_sided_p_value'] is None
