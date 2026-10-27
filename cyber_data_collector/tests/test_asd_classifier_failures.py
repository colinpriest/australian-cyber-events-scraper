"""ASD classification: failures are counted, uncertainty is not C6, and a failed
risk-matrix export never replaces the previous workbook.
"""
from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

import scripts.asd_risk_classifier as asd
from scripts.asd_risk_classifier import (
    INSUFFICIENT_INFORMATION,
    ASDRiskClassification,
    ASDRiskClassifier,
    RiskMatrixExportError,
)

EVENT_COLUMNS = ("deduplicated_event_id TEXT PRIMARY KEY, title TEXT, description TEXT, summary TEXT, "
                 "event_type TEXT, severity TEXT, event_date TEXT, records_affected INTEGER, "
                 "victim_organization_name TEXT, victim_organization_industry TEXT, "
                 "attacking_entity_name TEXT, attack_method TEXT, is_australian_event BOOLEAN, "
                 "australian_relevance_score REAL, status TEXT, created_at TEXT")


@pytest.fixture
def classifier(tmp_path):
    db = tmp_path / "asd.db"
    c = sqlite3.connect(db)
    c.executescript(f"""
        CREATE TABLE DeduplicatedEvents ({EVENT_COLUMNS});
        CREATE TABLE ASDRiskClassifications (classification_id TEXT PRIMARY KEY,
            deduplicated_event_id TEXT UNIQUE NOT NULL, severity_category TEXT,
            primary_stakeholder_category TEXT, impact_type TEXT, reasoning_json TEXT,
            confidence_score REAL, model_used TEXT, created_at TEXT, updated_at TEXT);
        INSERT INTO DeduplicatedEvents (deduplicated_event_id, title, event_date, status)
            VALUES ('d1', 'Acme breach', '2024-03-12', 'Active'),
                   ('d2', 'Beta breach', '2024-04-12', 'Active');
    """)
    c.commit()
    c.close()
    clf = ASDRiskClassifier(str(db), model="test-model", api_key="test")
    yield clf
    clf.close()


def _classification(severity="C4", impact="Isolated compromise",
                    stakeholder="Small organisation(s)"):
    return {"severity_category": severity, "primary_stakeholder_category": stakeholder,
            "impact_type": impact, "confidence": 0.8,
            "reasoning": {"severity_reasoning": "x", "stakeholder_reasoning": "x",
                          "impact_reasoning": "x", "information_quality": "x"}}


# ---- 35: insufficient information is not C6 ---------------------------------

def test_prompt_no_longer_defaults_to_c6(classifier):
    prompt = classifier.build_prompt({"title": "t", "records_affected": None})
    assert "Default to C6 (least severe)" not in prompt
    assert "default to C6 (least severe)" not in prompt
    assert "Never default to C6" in prompt
    assert INSUFFICIENT_INFORMATION in prompt
    assert asd.PROMPT_VERSION == "asd-v2"


def test_schema_and_validation_accept_insufficient_information(classifier):
    parsed = ASDRiskClassification(
        severity_category=INSUFFICIENT_INFORMATION, primary_stakeholder_category=INSUFFICIENT_INFORMATION,
        impact_type=INSUFFICIENT_INFORMATION, confidence=0.2,
        reasoning={"severity_reasoning": "no impact detail", "stakeholder_reasoning": "unknown victim",
                   "impact_reasoning": "nothing reported", "information_quality": "one vague line"})
    assert parsed.severity_category == INSUFFICIENT_INFORMATION
    assert classifier.validate_classification(_classification(
        INSUFFICIENT_INFORMATION, INSUFFICIENT_INFORMATION, INSUFFICIENT_INFORMATION))


def test_prompt_version_change_makes_classifications_stale(classifier, monkeypatch):
    event = classifier.get_events(limit=10)[0]
    classifier.save_classification(event["deduplicated_event_id"], _classification(), event["_fingerprint"])
    before = classifier.stale_count()
    monkeypatch.setattr(asd, "PROMPT_VERSION", "asd-test-next")
    assert classifier.stale_count() == before + 1


# ---- 18: failures are exposed, stale stays stale -----------------------------

def test_process_events_counts_failures_and_leaves_them_stale(classifier, monkeypatch):
    def classify(event, force_reclassify=False):
        return None if event["deduplicated_event_id"] == "d2" else _classification()
    monkeypatch.setattr(classifier, "classify_event", classify)
    results = classifier.process_events(limit=10)
    assert len(results) == 1
    assert classifier.failed_count == 1 and classifier.failed_event_ids == ["d2"]
    assert classifier.stale_count() == 1


def test_failed_count_resets_each_run(classifier, monkeypatch):
    monkeypatch.setattr(classifier, "classify_event", lambda e, f=False: None)
    classifier.process_events(limit=10)
    assert classifier.failed_count == 2
    monkeypatch.setattr(classifier, "classify_event", lambda e, f=False: _classification())
    classifier.process_events(limit=10)
    assert classifier.failed_count == 0 and classifier.stale_count() == 0


def test_worker_exception_counts_as_failure(classifier, monkeypatch):
    def boom(event, force_reclassify=False):
        raise RuntimeError("API down")
    monkeypatch.setattr(classifier, "classify_event", boom)
    classifier.process_events(limit=10)
    assert classifier.failed_count == 2


# ---- 20: risk-matrix export -----------------------------------------------------

def test_failed_matrix_never_overwrites_previous_file(classifier, tmp_path, monkeypatch):
    out = tmp_path / "matrices"
    out.mkdir()
    previous = out / "risk_matrix.xlsx"
    previous.write_bytes(b"previous good workbook")

    def broken(year):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(classifier, "_matrix_rows", broken)
    with pytest.raises(RiskMatrixExportError):
        classifier.compile_risk_matrix(out)
    assert previous.read_bytes() == b"previous good workbook"
    assert not list(out.glob(".*.tmp*"))


def test_empty_population_writes_empty_matrix(classifier, tmp_path):
    files = classifier.compile_risk_matrix(tmp_path)
    df = pd.read_excel(files[0], sheet_name="Risk Matrix")
    assert df.drop(columns=["Impact Type"]).isna().all().all()


def test_insufficient_information_goes_to_not_placed_sheet(classifier, tmp_path):
    for eid, cls in (("d1", _classification()),
                     ("d2", _classification(INSUFFICIENT_INFORMATION, INSUFFICIENT_INFORMATION))):
        classifier.save_classification(eid, cls, "fp")
    files = classifier.compile_risk_matrix(tmp_path)
    overall = [f for f in files if f.endswith("risk_matrix.xlsx")][0]
    matrix = pd.read_excel(overall, sheet_name="Risk Matrix")
    placed = pd.to_numeric(matrix.drop(columns=["Impact Type"]).stack(), errors="coerce").sum()
    assert placed == 1
    not_placed = pd.read_excel(overall, sheet_name="Not placed")
    assert not_placed["Impact Type"].tolist() == [INSUFFICIENT_INFORMATION]
    assert not_placed["Classifications"].sum() == 1
