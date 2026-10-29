"""Continuous quality checks and the publication gate (cyber_data_collector/quality)."""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from cyber_data_collector.quality import gate as quality_gate
from cyber_data_collector.quality import integrity, metrics, regression
from cyber_data_collector.quality.model import (
    FAIL, INFO, NOT_MEASURED, PASS, WARN, Measurement, Rule, Thresholds, evaluate, gate, load_thresholds,
    unproduced_rules,
)
from cyber_data_collector.storage.schema_init import initialize_database

TODAY = date(2026, 10, 27)
HOSTILE = "<script>alert('x')</script> Acme Health"


def _thresholds(*rules: Rule, settings=None) -> Thresholds:
    return Thresholds(version="test", settings=settings or {}, rules=list(rules), path="test", sha256="0" * 64)


# ---------------------------------------------------------------------------
# Fixture database
# ---------------------------------------------------------------------------

def _add_event(conn, event_id, *, title, date_, status="Active", victim=None, source="Perplexity",
               page="", researched=True, discovered="2026-09-15T10:00:00", entity_id=None):
    raw_id, enriched_id = f"raw-{event_id}", f"enr-{event_id}"
    conn.execute("INSERT INTO RawEvents (raw_event_id, source_type, raw_title, raw_content, discovered_at, "
                 "is_processed) VALUES (?, ?, ?, ?, ?, 1)", (raw_id, source, title, page, discovered))
    conn.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, event_date, "
                 "is_australian_event, is_specific_event, status, perplexity_validated, perplexity_enrichment_data) "
                 "VALUES (?, ?, ?, ?, 1, 1, 'Active', ?, ?)",
                 (enriched_id, raw_id, title, date_, int(researched),
                  json.dumps({"formal_entity_name": victim}) if researched else None))
    conn.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, master_enriched_event_id, title, "
                 "event_date, is_australian_event, is_specific_event, status, victim_organization_name, severity, "
                 "event_type) VALUES (?, ?, ?, ?, 1, 1, ?, ?, 'High', 'Ransomware')",
                 (event_id, enriched_id, title, date_, status, victim))
    conn.execute("INSERT INTO EventDeduplicationMap (raw_event_id, enriched_event_id, deduplicated_event_id) "
                 "VALUES (?, ?, ?)", (raw_id, enriched_id, event_id))
    if entity_id is not None:
        conn.execute("INSERT INTO DeduplicatedEventEntities (deduplicated_event_id, entity_id, relationship_type) "
                     "VALUES (?, ?, 'victim')", (event_id, entity_id))


@pytest.fixture
def db(tmp_path) -> Path:
    path = tmp_path / "quality.db"
    initialize_database(str(path))
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO EntitiesV2 (entity_id, entity_name) VALUES (1, 'Acme Health'), (2, 'Borealis Bank')")
    long_page = "Acme Health confirmed a ransomware attack. " * 20
    _add_event(conn, "E1", title="Acme Health ransomware attack", date_="2026-03-12", victim="Acme Health", page=long_page, entity_id=1)
    _add_event(conn, "E2", title="Acme Health patient data published", date_="2026-04-20", victim="Acme Health",
               page=long_page, entity_id=1)                                   # 39 days after E1
    _add_event(conn, "E3", title=HOSTILE, date_="2026-06-01", victim="Borealis Bank",
               researched=False, entity_id=2)                                  # no evidence, 1st of month
    _add_event(conn, "E4", title="Future thing", date_="2027-01-05")           # future
    _add_event(conn, "E5", title="Undated thing", date_=None)
    _add_event(conn, "E6", title="Old junk", date_="2024-05-05", status="Rejected")
    _add_event(conn, "E7", title="Folded report", date_="2026-03-13", status="Merged")
    # E7's record now lives in E1 (the ledger moves map rows on merge).
    conn.execute("UPDATE EventDeduplicationMap SET deduplicated_event_id = 'E1' WHERE deduplicated_event_id = 'E7'")
    conn.commit()
    conn.close()
    return path


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


# ---------------------------------------------------------------------------
# Judging and the gate
# ---------------------------------------------------------------------------

def test_unmeasured_blocking_metric_closes_the_gate():
    t = _thresholds(Rule("a.x", blocking=True, max=0))
    metric = evaluate("a.x", "a", Measurement.unavailable("column missing"), t)
    assert metric.status == NOT_MEASURED and metric.value is None
    allowed, reasons = gate([metric])
    assert not allowed and "column missing" in reasons[0]


def test_breach_is_fail_when_blocking_and_warn_otherwise():
    t = _thresholds(Rule("a.block", blocking=True, max=0), Rule("a.soft", max=0))
    assert evaluate("a.block", "a", Measurement(value=1), t).status == FAIL
    assert evaluate("a.soft", "a", Measurement(value=1), t).status == WARN
    assert evaluate("a.soft", "a", Measurement(value=0), t).status == PASS
    allowed, _ = gate([evaluate("a.soft", "a", Measurement(value=5), t)])
    assert allowed


def test_metric_without_rule_is_info_never_pass():
    assert evaluate("x.y", "x", Measurement(value=3), _thresholds()).status == INFO


def test_threshold_rule_with_no_value_is_not_measured():
    t = _thresholds(Rule("a.x", blocking=True, max=0))
    assert evaluate("a.x", "a", Measurement(), t).status == NOT_MEASURED


def test_require_pass_rule_needs_a_verdict():
    t = _thresholds(Rule("chk.*", blocking=True, require="pass"))
    assert evaluate("chk.one", "chk", Measurement(ok=True), t).status == PASS
    assert evaluate("chk.one", "chk", Measurement(ok=False), t).status == FAIL
    assert evaluate("chk.one", "chk", Measurement(value=0), t).status == NOT_MEASURED


def test_configured_rule_that_nothing_produced_is_reported():
    t = _thresholds(Rule("dates.future_active", blocking=True, max=0))
    missing = unproduced_rules([], t)
    assert [m.name for m in missing] == ["dates.future_active"]
    assert missing[0].status == NOT_MEASURED and not gate(missing)[0]


def test_committed_thresholds_load_and_name_their_reasons():
    t = load_thresholds()
    blocking = {r.match for r in t.rules if r.blocking}
    assert {"integrity.*", "labels.contradictions_total", "synthetic.*", "dates.future_active",
            "staleness.asd_stale_share"} <= blocking
    assert all(r.why for r in t.rules)


def test_unknown_rule_keys_are_rejected(tmp_path):
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"version": "x", "rules": [{"match": "a", "maximum": 1}]}))
    with pytest.raises(ValueError, match="unknown keys"):
        load_thresholds(path)


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------

def test_dates(db):
    conn = _ro(db)
    found = metrics.measure_dates(conn, load_thresholds(), TODAY)
    assert found["dates.future_active"].value == 1
    assert found["dates.undated_active"].value == 1
    assert found["dates.malformed_active"].value == 0
    assert found["dates.first_of_month_share"].value == pytest.approx(1 / 4)   # E3 of E1-E4


def test_missing_evidence(db):
    found = metrics.measure_evidence(_ro(db), load_thresholds(), TODAY)
    events = found["evidence.events_without_evidence"]
    assert events.value == 1          # E3: no page and no research; E4/E5 have research, E1/E2 pages
    assert events.examples[0].startswith("E3:")


def test_short_gap_same_victim_pairs_and_titles(db):
    found = metrics.measure_duplicates(_ro(db), load_thresholds(), TODAY)
    assert found["duplicates.same_victim_pairs_short_gap"].value == 1        # E1/E2, 39 days
    assert found["duplicates.identical_title_pairs"].value == 0


def test_undated_same_victim_pairs_are_counted_not_compared(db, monkeypatch):
    from cyber_data_collector.dedup import duplicate_judgement as dj

    pairs = [{"organisation": "Acme", "a": "E1", "b": "E2", "gap_days": 12},
             {"organisation": "Acme", "a": "E1", "b": "E3", "gap_days": dj.GAP_UNKNOWN}]
    monkeypatch.setattr(dj, "candidate_pairs", lambda conn: pairs)
    found = metrics.measure_duplicates(_ro(db), load_thresholds(), TODAY)
    assert found["duplicates.same_victim_pairs"].value == 2
    assert found["duplicates.same_victim_pairs_short_gap"].value == 1
    assert "1 undated pairs not judged" in found["duplicates.same_victim_pairs_short_gap"].detail


def test_absent_retry_columns_are_not_measured_never_zero(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE RawEvents (raw_event_id TEXT, source_type TEXT, discovered_at TEXT,
                                is_processed INTEGER, processing_error TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT, status TEXT);
    """)
    found = metrics.measure_backlogs(conn, load_thresholds(), TODAY)
    for name in ("backlog.enrichment_quarantined", "backlog.enrichment_retrying"):
        assert found[name].not_measured and found[name].value is None
    assert found["backlog.raw_unprocessed"].value == 0


def test_quarantine_is_counted_when_tracked(db):
    conn = sqlite3.connect(db)
    conn.execute("UPDATE EnrichedEvents SET perplexity_status = 'quarantined', perplexity_attempt_count = 3 "
                 "WHERE enriched_event_id = 'enr-E3'")
    conn.commit()
    found = metrics.measure_backlogs(conn, load_thresholds(), TODAY)
    assert found["backlog.enrichment_quarantined"].value == 1


def test_asd_staleness_needs_fingerprints(db):
    conn = sqlite3.connect(db)
    # init adds input_fingerprint; an older database without it cannot be measured
    conn.execute("ALTER TABLE ASDRiskClassifications DROP COLUMN input_fingerprint")
    found = metrics.measure_staleness(conn, load_thresholds(), TODAY)
    assert found["staleness.asd_stale_share"].not_measured
    assert found["staleness.asd_unclassified"].value == 5

    conn.execute("ALTER TABLE ASDRiskClassifications ADD COLUMN input_fingerprint TEXT")
    with metrics.row_factory(conn, sqlite3.Row):
        classifier = metrics._asd_classifier(conn)
        events = {e["deduplicated_event_id"]: e for e in classifier.get_events(limit=100)}
    for event_id, fingerprint in (("E1", events["E1"]["_fingerprint"]), ("E2", "outdated")):
        conn.execute("INSERT INTO ASDRiskClassifications (classification_id, deduplicated_event_id, "
                     "severity_category, primary_stakeholder_category, impact_type, reasoning_json, "
                     "confidence_score, model_used, input_fingerprint) VALUES (?, ?, 'C4', 'Large organisation(s)', "
                     "'Isolated compromise', '{}', 0.8, 'm', ?)", (f"c-{event_id}", event_id, fingerprint))
    found = metrics.measure_staleness(conn, load_thresholds(), TODAY)
    assert found["staleness.asd_stale"].value == 4               # E2 outdated + E3, E4, E5 unclassified
    assert found["staleness.asd_stale_share"].value == pytest.approx(0.8)


def test_measurement_does_not_write(db):
    before = db.read_bytes()
    conn = _ro(db)
    for _, measure in metrics.MEASURES:
        measure(conn, load_thresholds(), TODAY)
    conn.close()
    assert db.read_bytes() == before


def test_coverage_flags_zero_and_sharp_falls():
    months = [f"2026-{m:02d}" for m in range(1, 10)]
    series = {m: 20 for m in months} | {"2026-07": 0, "2026-08": 5}
    flags = metrics.flag_months(series, months, months[6:], trailing=6, drop_ratio=0.5, min_median=5)
    assert [f[:7] for f in flags] == ["2026-07", "2026-08"]
    quiet = {m: 2 for m in months} | {"2026-08": 1}
    assert metrics.flag_months(quiet, months, ["2026-08"], 6, 0.5, 5) == []    # below min median: noise


# ---------------------------------------------------------------------------
# Labels and synthetic cases
# ---------------------------------------------------------------------------

def _write(path: Path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def test_label_contradictions(db, tmp_path):
    conn = sqlite3.connect(db)
    _add_event(conn, "E8", title="Merged into rejected", date_="2025-01-02", status="Merged")
    conn.execute("UPDATE EventDeduplicationMap SET deduplicated_event_id = 'E6' WHERE deduplicated_event_id = 'E8'")
    conn.commit()
    _write(tmp_path / "event_reality_labels.csv", ["deduplicated_event_id", "label", "rationale"], [
        ("E1", "real", ""), ("E7", "real", "merged into active E1"), ("E8", "real", "merged into rejected E6"),
        ("E4", "not_incident", "still active"), ("E6", "not_incident", ""), ("gone", "real", ""),
        ("E2", "unsure", "")])
    _write(tmp_path / "duplicate_labels.csv", ["pair_id", "label"], [
        ("E1|E2", "same"), ("E1|E7", "different"), ("E1|E3", "different"), ("E1|nope", "same")])
    _write(tmp_path / "event_scope_labels.csv", ["deduplicated_event_id", "label", "rationale"], [
        ("E1", "in_scope", ""), ("E4", "out_of_scope", "still active")])
    _write(tmp_path / "source_gate_labels.csv", ["enriched_event_id", "label"], [
        ("enr-E3", "not_incident"), ("enr-E6", "not_incident"), ("enr-E1", "reports_incident")])

    found = regression.measure_labels(conn, evaluation_dir=tmp_path)
    reality = found["labels.event_reality.contradictions"]
    assert reality.value == 2 and {e.split(":")[0] for e in reality.examples} == {"E8", "E4"}
    assert found["labels.event_reality.unchecked"].value == 2                  # 'gone', 'unsure'
    assert found["labels.duplicates.contradictions"].value == 2                # E1/E2 apart, E1/E7 together
    assert found["labels.duplicates.unchecked"].value == 1
    assert found["labels.source_gate.contradictions"].value == 1               # enr-E3 evidence of active E3
    assert found["labels.event_scope.contradictions"].value == 1               # E4 labelled out of scope, still Active
    assert found["labels.contradictions_total"].value == 6


def test_missing_label_file_is_not_measured(db, tmp_path):
    found = regression.measure_labels(sqlite3.connect(db), evaluation_dir=tmp_path)
    assert found["labels.contradictions_total"].not_measured


def test_committed_labels_are_readable(db):
    found = regression.measure_labels(sqlite3.connect(db))
    assert not found["labels.contradictions_total"].not_measured


def test_synthetic_cases_pass_on_this_code():
    found = regression.measure_synthetic()
    assert len(found) >= 20
    failing = {k: v.detail or v.not_measured for k, v in found.items() if v.ok is not True}
    assert not failing


# ---------------------------------------------------------------------------
# Integrity wrapper
# ---------------------------------------------------------------------------

class _Result:
    def __init__(self, name, ok, summary="", details=()):
        self.name, self.ok, self.summary, self.details = name, ok, summary, list(details)


def test_integrity_skips_and_crashes_are_not_passes(monkeypatch, tmp_path):
    def skipped(conn):
        return _Result("dashboard-totals-reconcile", True, "Skipped: database schema is incomplete")

    def raises(conn):
        raise RuntimeError("boom")

    def fine(conn):
        return _Result("fine", True, "ok")

    def oaic(_unused):
        return _Result("oaic", True, "ok")

    class Module:
        DB_CHECKS = [skipped, raises, fine]
        JSON_CHECKS = [oaic]

        @staticmethod
        def _oaic_files_newest_first():
            return []

    monkeypatch.setattr(integrity, "_load_module", lambda: Module)
    found = integrity.measure_integrity(sqlite3.connect(":memory:"))
    assert found["integrity.skipped"].not_measured
    assert "boom" in found["integrity.raises"].not_measured
    assert found["integrity.fine"].ok is True
    assert found["integrity.oaic"].not_measured and found["integrity.oaic-files-readable"].not_measured

    (tmp_path / "oaic_cyber_statistics_20260101_000000.json").write_text("{broken")
    monkeypatch.setattr(Module, "_oaic_files_newest_first",
                        staticmethod(lambda: [str(tmp_path / "oaic_cyber_statistics_20260101_000000.json")]))
    found = integrity.measure_integrity(sqlite3.connect(":memory:"))
    assert found["integrity.oaic-files-readable"].ok is False


def test_integrity_import_failure_is_not_measured(monkeypatch):
    def boom():
        raise ImportError("no module")

    monkeypatch.setattr(integrity, "_load_module", boom)
    assert integrity.measure_integrity(None)["integrity.module"].not_measured


# ---------------------------------------------------------------------------
# Whole runs, recording and output
# ---------------------------------------------------------------------------

@pytest.fixture
def fast(monkeypatch):
    """Skip the integrity group (it builds the whole dashboard); covered above."""
    stub = ("integrity", lambda conn, t, d: {"integrity.stub": Measurement(ok=True)})
    monkeypatch.setattr(quality_gate, "ALL_MEASURES",
                        tuple(m for m in quality_gate.ALL_MEASURES if m[0] != "integrity") + (stub,))


def test_full_run_blocks_on_future_date_and_records_history(db, fast):
    conn = sqlite3.connect(db)
    allowed, reasons = quality_gate.publication_allowed(conn)
    assert not allowed
    assert any(r.startswith("dates.future_active") for r in reasons)
    assert any(r.startswith("staleness.asd_stale_share") for r in reasons)    # unmeasurable here
    runs = conn.execute("SELECT run_id, publication_allowed FROM QualityRuns").fetchall()
    assert len(runs) == 1 and runs[0][1] == 0
    stored = dict(conn.execute("SELECT metric, value FROM QualityMetrics WHERE metric = 'dates.future_active'"))
    assert stored["dates.future_active"] == 1

    report = quality_gate.run_quality_checks(conn, today=TODAY)
    assert report.previous["dates.future_active"]["value"] == 1


def test_a_crashing_measure_closes_the_gate(db, monkeypatch):
    def crash(conn, t, d):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(quality_gate, "ALL_MEASURES", (("dates", crash),))
    report = quality_gate.run_quality_checks(sqlite3.connect(db), today=TODAY)
    names = {m.name: m for m in report.metrics}
    assert names["dates.error"].status == NOT_MEASURED
    assert names["dates.future_active"].status == NOT_MEASURED       # configured, not produced
    assert not report.allowed


def test_reports_escape_database_text(db, fast, tmp_path):
    report = quality_gate.run_quality_checks(_ro(db), today=TODAY)
    paths = quality_gate.write_outputs(report, tmp_path / "out")
    page = paths["html"].read_text(encoding="utf-8")
    assert "<script>alert" not in page and "&lt;script&gt;" in page
    data = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert data["publication_allowed"] is False and data["reasons"]
    assert "publication BLOCKED" in paths["markdown"].read_text(encoding="utf-8")


def test_script_exit_codes(db, fast, tmp_path):
    from scripts import quality_report

    out = tmp_path / "out"
    assert quality_report.main(["--db", str(db), "--out-dir", str(out), "--no-record", "--quiet"]) == 1
    assert (out / "quality_report.json").exists()
    conn = sqlite3.connect(db)
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'QualityRuns'").fetchone()

    conn.execute("DELETE FROM DeduplicatedEvents WHERE deduplicated_event_id IN ('E4', 'E5')")
    conn.commit()
    assert quality_report.main(["--db", str(db), "--out-dir", str(out), "--quiet",
                                "--thresholds", str(_permissive(tmp_path))]) == 0
    assert conn.execute("SELECT COUNT(*) FROM QualityRuns").fetchone()[0] == 1


def _permissive(tmp_path) -> Path:
    """The committed thresholds minus the label/ASD rules this toy database cannot satisfy."""
    data = json.loads(Path(quality_gate.load_thresholds().path).read_text(encoding="utf-8"))
    data["rules"] = [r for r in data["rules"]
                     if r["match"] not in ("labels.contradictions_total", "staleness.asd_stale_share")]
    path = tmp_path / "permissive.json"
    path.write_text(json.dumps(data))
    return path


def test_missing_database_blocks(tmp_path):
    from scripts import quality_report

    assert quality_report.main(["--db", str(tmp_path / "nope.db"), "--out-dir", str(tmp_path), "--no-record"]) == 1


def test_real_integrity_checks_are_wrapped(db, monkeypatch, tmp_path):
    """The actual check_data_integrity checks, keyed by function name; no OAIC files here."""
    monkeypatch.chdir(tmp_path)
    found = integrity.measure_integrity(_ro(db))
    assert found["integrity.reporting-population"].ok is True
    assert found["integrity.no-future-dated-incidents"].ok is False          # E4
    assert found["integrity.oaic-files-readable"].not_measured
    assert found["integrity.no-duplicate-h1-h2-oaic"].not_measured
