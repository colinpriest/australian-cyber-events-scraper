"""Integrity checks fail when they cannot run; the dashboard keeps unknown
severity and malformed PII data out of averages and denominators.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.check_data_integrity as integrity

INTEGRITY_SCRIPT = Path(integrity.__file__)


# ---- 21: integrity checker -------------------------------------------------------

@pytest.mark.parametrize("check", [
    integrity.check_no_duplicate_h1_h2_oaic,
    integrity.check_oaic_top_sectors_in_range,
    integrity.check_oaic_period_publication_realistic,
])
def test_unreadable_oaic_file_fails_every_oaic_check(check, tmp_path, monkeypatch):
    (tmp_path / "oaic_cyber_statistics_20260101_000000.json").write_text("{broken", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    result = check(None)
    assert not result.ok and "could not be read" in result.summary


def test_published_dataset_contradiction_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "instance" / "oaic"
    path.mkdir(parents=True)
    (path / "oaic_statistics.json").write_text(json.dumps({"schema": "oaic-statistics-v1", "records": [
        {"year": 2025, "period": "H2", "total_notifications": 100, "malicious_attacks": 300}]}),
        encoding="utf-8")
    assert not integrity.check_oaic_published_dataset_consistent(None).ok
    (path / "oaic_statistics.json").write_text("not json", encoding="utf-8")
    assert not integrity.check_oaic_published_dataset_consistent(None).ok


def test_absent_published_dataset_is_a_skip_not_a_pass(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = integrity.check_oaic_published_dataset_consistent(None)
    assert result.ok and result.skipped


def test_in_memory_dashboard_check_is_reported_as_skip():
    result = integrity.check_dashboard_totals_reconcile(sqlite3.connect(":memory:"))
    assert result.skipped


def test_dashboard_check_database_error_is_a_failure(tmp_path, monkeypatch):
    db = tmp_path / "x.db"
    c = sqlite3.connect(db)
    for table in integrity.DASHBOARD_REQUIRED_TABLES:
        c.execute(f"CREATE TABLE {table} (id TEXT)")
    c.commit()
    import scripts.build_static_dashboard as sd

    def broken(**kwargs):
        raise sqlite3.OperationalError("no such column: status")
    monkeypatch.setattr(sd, "build_dashboard_file", broken)
    result = integrity.check_dashboard_totals_reconcile(c)
    c.close()
    assert not result.ok and not result.skipped


def test_missing_database_fails_without_creating_it(tmp_path):
    db = tmp_path / "missing.db"
    proc = subprocess.run([sys.executable, str(INTEGRITY_SCRIPT), "--db", str(db)],
                          capture_output=True, text=True, cwd=str(tmp_path))
    assert proc.returncode == 1 and "Database not found" in proc.stdout
    assert not db.exists()


# ---- 36: unknown severity is excluded from averages ---------------------------------

@pytest.fixture
def sev_conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT, event_date TEXT, status TEXT,
            severity TEXT, event_type TEXT, victim_organization_industry TEXT,
            personal_info_types_json TEXT);
        CREATE TABLE IndustryGroupings (group_name TEXT, keywords TEXT, display_order INTEGER);
        INSERT INTO IndustryGroupings VALUES ('Health', '["health"]', 1);
        INSERT INTO DeduplicatedEvents VALUES
            ('a', '2024-03-01', 'Active', 'High', 'Ransomware', 'Healthcare', NULL),
            ('b', '2024-03-02', 'Active', 'Unknown', 'Ransomware', 'Healthcare', NULL),
            ('c', '2024-03-03', 'Active', NULL, 'Ransomware', 'Healthcare', NULL),
            ('d', '2024-03-04', 'Active', 'Severe', 'Ransomware', 'Healthcare', NULL);
    """)
    return c


def test_severity_by_industry_excludes_unknown_from_average(sev_conn):
    import scripts.build_static_dashboard as sd
    out = sd.get_severity_by_industry(sev_conn, "2024-01-01", "2024-12-31")
    assert out["industries"] == ["Health"]
    assert out["avg_severities"] == [3.0]           # was (3+0+0)/3 = 1.0
    assert out["unknown_severity_total"] == 3        # Unknown, NULL, unrecognised


def test_severity_by_attack_type_excludes_unknown_from_average(sev_conn):
    import scripts.build_static_dashboard as sd
    out = sd.get_severity_by_attack_type(sev_conn, "2024-01-01", "2024-12-31")
    assert out["avg_severities"] == [3.0]
    assert out["unknown_severity_counts"] == [3]


def test_unrecognised_severity_is_none_not_zero():
    import scripts.build_static_dashboard as sd
    assert sd._severity_to_numeric("Severe") is None
    assert sd._severity_to_numeric("Unknown") is None
    assert sd._severity_to_numeric("EventSeverity.HIGH") == 3


def test_median_skips_unrecognised_severity(sev_conn):
    import scripts.build_static_dashboard as sd
    med = sd.get_median_severity_per_month(sev_conn, "2024-01-01", "2024-12-31")
    assert med["median_severities"] == [3]  # 'Severe' is not counted as 0


# ---- 37: malformed PII data leaves the denominator too -------------------------------

def test_pii_rates_exclude_malformed_and_unclassified(tmp_path):
    import scripts.build_static_dashboard as sd
    db = tmp_path / "pii.db"
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT, event_date TEXT, status TEXT,
            personal_info_types_json TEXT);
    """)
    rows = [("a", '{"contact_information": true}'), ("b", '{"contact_information": false}'),
            ("c", "{not json"), ("d", '{"contact_information": "yes"}'), ("e", None)]
    c.executemany("INSERT INTO DeduplicatedEvents VALUES (?, '2024-03-01', 'Active', ?)", rows)
    c.commit()
    c.close()
    oaic = [{"year": 2024, "period": "H1", "total_notifications": 200,
             "personal_info_types": {"contact_information": 100}}]
    out = sd.prepare_oaic_personal_info_series(oaic, db_path=str(db))
    assert out["db_classified"] == [2] and out["db_malformed"] == [2] and out["db_unclassified"] == [1]
    assert out["db_series"]["contact_information"] == [1]
    assert out["db_series_pct"]["contact_information"] == [50.0]   # 1 of 2, not 1 of 5
    assert out["series_pct"]["contact_information"] == [50.0]      # OAIC 100 of 200


def test_pii_database_error_raises_instead_of_zeros(tmp_path):
    import scripts.build_static_dashboard as sd
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    oaic = [{"year": 2024, "period": "H1", "total_notifications": 10,
             "personal_info_types": {"contact_information": 1}}]
    with pytest.raises(sqlite3.OperationalError):
        sd.prepare_oaic_personal_info_series(oaic, db_path=str(db))
