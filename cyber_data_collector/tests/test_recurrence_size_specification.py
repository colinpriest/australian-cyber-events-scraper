"""Organisation size in the recurrence models: reconciled across name variants, and how it is specified."""
from __future__ import annotations

import math
import sqlite3
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from scripts import analyze_recurrent_timing as art
from scripts.recurrence_conclusions import describe_size_specification

CENSOR = date(2026, 6, 30)


# --- size across name variants ----------------------------------------------------------------------------

def variant(entity_id, band, confidence=None, method="perplexity", name=None):
    return {"entity_id": entity_id, "entity_name": name or f"Org {entity_id}", "size_estimate": band,
            "size_confidence": confidence, "size_method": method}


def test_a_known_band_outranks_unknown_on_the_representative():
    out = art.reconcile_variant_sizes([variant(1, "UNKNOWN", 0.9), variant(2, "LARGE", 0.6)], representative=1)
    assert (out["chosen"]["entity_id"], out["rule"], out["conflict"]) == (2, "known band over UNKNOWN", False)
    assert out["representative_band"] == "UNKNOWN"


def test_a_human_size_outranks_model_research_whatever_its_confidence():
    out = art.reconcile_variant_sizes([variant(1, "HUGE", 0.95), variant(2, "MEDIUM", None, "human")], 1)
    assert (out["chosen"]["entity_id"], out["rule"], out["conflict"]) == (2, "human size method", True)


def test_among_researched_bands_the_higher_confidence_wins_and_the_conflict_is_recorded():
    out = art.reconcile_variant_sizes([variant(1, "LARGE", 0.6), variant(2, "HUGE", 0.8), variant(3, "UNKNOWN")], 1)
    assert (out["chosen"]["entity_id"], out["rule"], out["conflict"]) == (2, "higher size_confidence", True)


def test_equal_evidence_falls_back_to_the_representative_then_the_lowest_id():
    assert art.reconcile_variant_sizes([variant(5, "LARGE", 0.7), variant(9, "LARGE", 0.7)], 9)["chosen"][
        "entity_id"] == 9
    tie = art.reconcile_variant_sizes([variant(5, "LARGE", 0.7), variant(3, "LARGE", 0.7), variant(9, "SMALL")], 9)
    assert (tie["chosen"]["entity_id"], tie["rule"]) == (3, "lowest entity id")
    assert art.reconcile_variant_sizes([variant(4, None)], 4)["rule"] == "only variant"


def _variant_db(path):
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT, employee_count INTEGER,
            turnover TEXT, entity_kind TEXT, size_estimate TEXT, size_confidence REAL, size_employees INTEGER,
            size_revenue_aud REAL, size_method TEXT, industry TEXT);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, title TEXT, event_date TEXT,
            event_type TEXT, records_affected INTEGER, vendor_organization_name TEXT, date_confidence REAL,
            confidence_score REAL, total_data_sources INTEGER, victim_organization_industry TEXT,
            is_australian_event INTEGER, is_specific_event INTEGER, status TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        CREATE TABLE EventDeduplicationMap (deduplicated_event_id TEXT, enriched_event_id TEXT);
    """)
    c.executemany("INSERT INTO EntitiesV2 (entity_id, entity_name, entity_kind, size_estimate, size_confidence, "
                  "size_method) VALUES (?,?,?,?,?,?)",
                  [(3, "Medibank", "organisation", "UNKNOWN", 0.4, "perplexity"),
                   (4, "Medibank Private Limited", "organisation", "LARGE", 0.8, "perplexity"),
                   (7, "Acme Pty Ltd", "organisation", "SMALL", 0.9, "perplexity")])
    for did, title, day in [("m1", "Medibank breach", "2022-10-13"), ("m2", "Medibank again", "2023-06-01"),
                            ("a1", "Acme breach", "2024-02-02")]:
        c.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, title, event_date, status) "
                  "VALUES (?, ?, ?, 'Active')", (did, title, day))
    c.executemany("INSERT INTO DeduplicatedEventEntities VALUES (?, ?, 'victim')",
                  [("m1", 3), ("m1", 4), ("m2", 3), ("a1", 7)])
    c.commit()
    c.close()


def test_the_analysis_takes_a_researched_size_from_a_variant_when_the_representative_is_unknown(tmp_path):
    db = tmp_path / "variants.db"
    _variant_db(db)
    events = art.load_victim_events(db)
    medibank = events[events["entity_name"] == "Medibank"]
    assert medibank["entity_id"].nunique() == 1 and set(medibank["org_size"]) == {"LARGE"}
    assert set(medibank["size_source_entity_id"]) == {4} and set(medibank["size_rule"]) == {"known band over UNKNOWN"}
    table = art.size_reconciliation_table(events)
    assert table[["entity_name", "representative_size_estimate", "org_size", "changed_from_representative"]
                 ].values.tolist() == [["Medibank", "UNKNOWN", "LARGE", True]]
    summary = art.size_reconciliation_summary(events)
    assert summary["organisations stored under several name variants"] == 1
    assert summary["organisations whose size changed against the representative-only rule"] == 1
    assert summary["organisations whose variants give conflicting known size bands"] == 0


# --- size specification ---------------------------------------------------------------------------------------

def synthetic_events(seed=7, organisations=240):
    rng = np.random.default_rng(seed)
    bands = ["SMALL", "MEDIUM", "LARGE", "HUGE", "UNKNOWN"]
    rows = []
    for org in range(organisations):
        band = bands[org % len(bands)]
        day = date(2018, 1, 1) + timedelta(days=int(rng.integers(0, 2500)))
        for n in range(int(rng.integers(1, 5))):
            if day > CENSOR:
                break
            rows.append({"entity_id": org, "entity_name": f"Org {org}", "deduplicated_event_id": f"{org}-{n}",
                         "event_date": day, "title": f"Incident {org}-{n}", "industry": "", "entity_kind": "organisation",
                         "size_estimate": band, "size_confidence": 0.8, "employee_count": None, "turnover": None,
                         "size_revenue_aud": None, "records_affected": None})
            day = day + timedelta(days=int(rng.integers(5, 600)))
    return art.add_entity_covariates(pd.DataFrame(rows))


def test_size_specification_comparison_reports_both_specifications_and_the_interaction_rule():
    spells, _ = art.build_spells(synthetic_events(), CENSOR)
    table, interaction = art.size_specification_comparison(spells, art.DEFAULT_ELAPSED_BOUNDS, CENSOR)
    assert set(table["banding"]) == {"run", "0-90 / 91-180"}
    run = table[table["banding"] == "run"].set_index("size_spec")
    assert run.loc["score", "parameters"] < run.loc["categorical", "parameters"]
    _, _, primary = art.fit_adjusted_elapsed_model(spells, art.DEFAULT_ELAPSED_BOUNDS, CENSOR)
    assert run.loc["score", "elapsed_constant_hazard_p"] == pytest.approx(primary["p_value"])
    assert run.loc["categorical", "nested"] in (True, False)
    if run.loc["categorical", "nested"]:
        assert math.isfinite(run.loc["categorical", "p_value"])
        assert run.loc["categorical", "likelihood_ratio"] >= -1e-6
    peak = table[(table["banding"] == "0-90 / 91-180") & (table["size_spec"] == "score_x_phase")].iloc[0]
    if interaction["supported"]:
        assert math.isfinite(peak["p_value"]) and peak["df"] == 2
    else:
        assert peak["note"].startswith("skipped") and not math.isfinite(peak["p_value"])
    assert describe_size_specification(table.to_dict(orient="records"))


def test_the_categorical_model_nests_the_score_unless_two_sparse_bands_are_pooled():
    split = pd.DataFrame({"org_size": ["SMALL", "MEDIUM", "LARGE", "HUGE"], "events": [5, 4, 9, 2],
                          "exposure_days": [100.0] * 4})
    assert art.categorical_nests_score(split)[0]
    pooled = split.assign(events=[1, 4, 9, 2])
    nested, reason = art.categorical_nests_score(pooled)
    assert not nested and "HUGE, SMALL" in reason


def test_the_interaction_is_skipped_with_a_reason_when_a_phase_is_thin_or_size_does_not_vary():
    split = pd.DataFrame({
        "delayed_peak_phase": ["immediate_0_90", "immediate_0_90", "peak_91_180", "post_180", "post_180"],
        "org_size": ["SMALL", "HUGE", "LARGE", "SMALL", "UNKNOWN"],
        "events": [6, 6, 12, 20, 9]})
    support = art.size_interaction_support(split)
    assert not support["supported"]
    assert "peak_91_180: repeats at 1 known size band(s)" in support["skipped_reason"]
    assert "immediate_0_90" not in support["skipped_reason"]
    thin = art.size_interaction_support(split.assign(events=[2, 2, 12, 20, 9]))
    assert "immediate_0_90: 4 repeats" in thin["skipped_reason"]


def _row(banding, spec, **values):
    base = {"banding": banding, "elapsed_bands": "0-90, >90", "size_spec": spec, "aic": 100.0, "nested": None,
            "p_value": math.nan, "df": math.nan, "elapsed_constant_hazard_p": math.nan,
            "delayed_peak_directional_p": math.nan, "note": ""}
    base.update(values)
    return base


def test_size_specification_sentences_say_when_a_headline_verdict_changes():
    rows = [_row("run", "score", aic=200.0, elapsed_constant_hazard_p=0.04),
            _row("run", "categorical", aic=199.0, nested=True, p_value=0.2, df=2, elapsed_constant_hazard_p=0.07),
            _row("run", "score_x_phase", note="skipped (rule): peak_91_180: 3 repeats (fewer than 10)")]
    text = " ".join(describe_size_specification(rows))
    assert "do not reject equal log-rate steps" in text
    assert "p = 0.040 with the size score and p = 0.070 with categorical size: the verdict at the 5% level changes" in text
    assert "Size by elapsed-time phase was not fitted: skipped" in text
    unnested = [_row("run", "score"), _row("run", "categorical", nested=False, note="bands pooled")]
    assert "not nested (bands pooled), so only AIC compares them" in " ".join(describe_size_specification(unnested))
