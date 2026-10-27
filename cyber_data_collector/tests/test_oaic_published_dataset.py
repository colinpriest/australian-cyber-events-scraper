"""OAIC statistics: contradictions are withheld, never published; a failed
extract never replaces the last valid record; refreshes are dated; corrupt
files fail validation.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.oaic import cleanup_oaic_data, refresh_oaic_statistics
from scripts.oaic.oaic_consistency import (
    METHOD_FIELDS,
    find_contradictions,
    screen_record,
)
from scripts.oaic.oaic_dataset import (
    OAICDatasetError,
    load_dataset,
    load_for_dashboard,
    load_state,
    merge_legacy_files,
    promote_records,
    refresh_status_line,
)


def good(year=2025, period="H2", total=600, **extra):
    record = {
        "year": year, "period": period, "start_month": 7 if period == "H2" else 1,
        "end_month": 12 if period == "H2" else 6, "total_notifications": total,
        "malicious_attacks": 360, "human_error": 200, "system_faults": 30,
        "other_sources": 6, "currently_unknown_sources": 4,
        "cyber_incidents_total": 360, "cyber_incident_only": 220,
        "monthly_notifications": [{"month": m, "count": total // 6} for m in range(6)],
        "top_sectors": [{"sector": "Health", "notifications": 120}],
        "individuals_affected_distribution": [{"range": "1", "count": 300}],
        "source": "data.gov.au",
    }
    record.update(extra)
    return record


# ---- consistency rules ----------------------------------------------------------

def test_consistent_record_has_no_contradictions():
    assert find_contradictions(good()) == []


@pytest.mark.parametrize("change, whole", [
    ({"malicious_attacks": 900}, True),                       # part exceeds the total
    ({"other_sources": 50}, True),                            # complete split != total
    ({"monthly_notifications": [{"count": 99}] * 6}, True),   # 6 months != semester total
    ({"cyber_incidents_total": 400, "cyber_incident_only": None}, False),  # cyber > malicious
    ({"phishing": 200, "ransomware": 100}, False),            # methods exceed cyber incidents
    ({"top_sectors": [{"sector": "x", "notifications": 700}]}, False),
    ({"human_error_pct": 140}, False),
    ({"period": "Q3"}, True),                                 # malformed period
])
def test_contradictions_are_found(change, whole):
    issues = find_contradictions(good(**change))
    assert issues
    assert any(c.whole_period for c in issues) == whole


def test_screen_withholds_involved_figures_with_reasons():
    record, issues = screen_record(good(phishing=300, ransomware=100))
    assert record is not None and issues
    assert all(record[f] is None for f in METHOD_FIELDS)
    assert record["withheld"] and "attack-method" in record["withheld"][0]["reason"]
    assert record["total_notifications"] == 600  # uninvolved figures stay


def test_screen_withholds_whole_period_when_total_is_contradicted():
    record, issues = screen_record(good(malicious_attacks=900))
    assert record is None and issues


def test_power_bi_method_counts_on_the_wrong_base_are_withheld():
    record, issues = screen_record(good(source="dashboard", phishing=50))
    assert record["phishing"] is None
    assert any(c.check == "attack-method-base" for c in issues)


# ---- legacy files ------------------------------------------------------------------

def _write(dir_: Path, stamp: str, records) -> Path:
    path = dir_ / f"oaic_cyber_statistics_{stamp}_000000.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


def test_corrupt_legacy_file_raises_not_skipped(tmp_path):
    _write(tmp_path, "20260101", [good()])
    (tmp_path / "oaic_cyber_statistics_20260202_000000.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(OAICDatasetError):
        merge_legacy_files(tmp_path)


def test_legacy_merge_does_not_mix_records_with_different_totals(tmp_path):
    older = good()  # total 600, with a monthly series summing to it
    newer = good(total=250, malicious_attacks=150, human_error=80, system_faults=10,
                 other_sources=6, currently_unknown_sources=4, cyber_incidents_total=150,
                 cyber_incident_only=100, monthly_notifications=None, top_sectors=None,
                 individuals_affected_distribution=None)
    _write(tmp_path, "20260101", [older])
    _write(tmp_path, "20260202", [newer])
    records, notes = merge_legacy_files(tmp_path)
    assert records[0]["total_notifications"] == 250
    assert records[0]["monthly_notifications"] is None  # not taken from the 600 record
    assert any("disagree on total_notifications" in n.reason for n in notes)


# ---- promotion ------------------------------------------------------------------------

@pytest.fixture
def paths(tmp_path):
    return {"dataset_path": tmp_path / "pub" / "oaic.json",
            "state_path": tmp_path / "pub" / "state.json",
            "legacy_dir": tmp_path}


def test_bootstrap_then_promote_and_date_the_refresh(paths):
    _write(paths["legacy_dir"], "20260101", [good(2025, "H1")])
    result = promote_records([good()], "data.gov.au", now="2026-10-27T10:00:00", **paths)
    assert result.ok and result.bootstrapped and result.accepted == ["2025 H2"]
    dataset = load_dataset(paths["dataset_path"])
    assert [f"{r['year']} {r['period']}" for r in dataset["records"]] == ["2025 H1", "2025 H2"]
    assert dataset["records"][1]["refresh"]["refreshed_at"] == "2026-10-27T10:00:00"
    state = load_state(paths["state_path"])
    assert state["last_success_at"] == "2026-10-27T10:00:00"
    assert state["period_refreshed_at"]["2025 H2"]["source"] == "data.gov.au"
    line = refresh_status_line(state, dataset["records"], "published dataset")
    assert line == "OAIC statistics last refreshed 2026-10-27 (latest period 2025 H2)."


def test_failing_extract_keeps_last_valid_record_and_file(paths):
    promote_records([good()], "data.gov.au", now="2026-10-01T00:00:00", **paths)
    before = paths["dataset_path"].read_bytes()
    bad = good(malicious_attacks=900)
    result = promote_records([bad], "data.gov.au", now="2026-10-27T00:00:00", **paths)
    assert not result.ok and "2025 H2" in result.rejected
    assert paths["dataset_path"].read_bytes() == before        # never overwritten
    state = load_state(paths["state_path"])
    assert state["last_success_at"] == "2026-10-01T00:00:00"    # success not moved
    assert state["last_attempt"]["status"] == "failed"
    line = refresh_status_line(state, load_dataset(paths["dataset_path"])["records"],
                               "published dataset")
    assert "last refreshed 2026-10-01" in line and "FAILED" in line


def test_run_errors_make_the_attempt_a_failure(paths):
    result = promote_records([good()], "data.gov.au", errors=["download failed"], **paths)
    assert not result.ok and result.accepted == ["2025 H2"]
    assert "last_success_at" not in load_state(paths["state_path"])


def test_corrupt_published_dataset_is_never_overwritten(paths):
    paths["dataset_path"].parent.mkdir(parents=True)
    paths["dataset_path"].write_text("garbage", encoding="utf-8")
    with pytest.raises(OAICDatasetError):
        promote_records([good()], "data.gov.au", **paths)
    assert paths["dataset_path"].read_text(encoding="utf-8") == "garbage"


def test_refresh_command_fails_on_ingest_errors(paths):
    def ingest(cache_dir):
        return [good()], ["2026 H1: failed to ingest: timeout"], {}
    result = refresh_oaic_statistics.refresh(paths["dataset_path"], paths["state_path"],
                                             Path("unused"), paths["legacy_dir"], ingest=ingest)
    assert not result.ok and result.errors


def test_refresh_command_uses_datagov_exact_checks(paths):
    record = good()
    record["monthly_notifications"] = record["monthly_notifications"][:5]  # partial: not a contradiction
    result = refresh_oaic_statistics.refresh(
        paths["dataset_path"], paths["state_path"], Path("unused"), paths["legacy_dir"],
        ingest=lambda d: ([record], [], {}))
    assert "2025 H2" in result.rejected  # verify_record: monthly counts must sum to the total


def test_dashboard_loader_prefers_published_dataset(paths):
    _write(paths["legacy_dir"], "20260101", [good(2024, "H1")])
    promote_records([good()], "data.gov.au", now="2026-10-27T00:00:00", **paths)
    load = load_for_dashboard(paths["dataset_path"], paths["state_path"], paths["legacy_dir"])
    assert load.origin == "published dataset"
    assert "last refreshed 2026-10-27" in load.status_line


def test_dashboard_loader_says_when_data_is_not_from_a_refresh(tmp_path):
    _write(tmp_path, "20260101", [good()])
    load = load_for_dashboard(tmp_path / "none.json", tmp_path / "none-state.json", tmp_path)
    assert load.origin == "legacy files"
    assert "not from a validated refresh" in load.status_line


def test_dashboard_load_oaic_data_raises_on_corrupt_file(tmp_path, monkeypatch):
    import importlib
    import sys
    (tmp_path / "oaic_cyber_statistics_20260101_000000.json").write_text("[{", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    sys.modules.pop("scripts.build_static_dashboard", None)
    sd = importlib.import_module("scripts.build_static_dashboard")
    with pytest.raises(OAICDatasetError):
        sd.load_oaic_data()


def test_dashboard_status_html_escapes_and_lists_withheld():
    import scripts.build_static_dashboard as sd
    html = sd.oaic_status_html({"status_line": "OAIC <b>", "notes": [
        {"period": "2022 H1", "fields": ["phishing"], "reason": "x > y"}]})
    assert "&lt;b&gt;" in html and "x &gt; y" in html and "1 OAIC figure(s)" in html


# ---- validation command ------------------------------------------------------------

def test_cleanup_validation_fails_on_corrupt_file(tmp_path):
    (tmp_path / "oaic_cyber_statistics_20260101_000000.json").write_text("{", encoding="utf-8")
    failures, _ = cleanup_oaic_data.validate(tmp_path, tmp_path / "pub.json", tmp_path / "st.json")
    assert any("unreadable legacy file" in f for f in failures)


def test_cleanup_validation_fails_on_published_contradiction(tmp_path):
    pub = tmp_path / "pub.json"
    pub.write_text(json.dumps({"schema": "oaic-statistics-v1",
                               "records": [good(malicious_attacks=900)]}), encoding="utf-8")
    failures, _ = cleanup_oaic_data.validate(tmp_path, pub, tmp_path / "st.json")
    assert any(f.startswith("published 2025 H2") for f in failures)


def test_cleanup_validation_passes_clean_data(tmp_path):
    _write(tmp_path, "20260101", [good()])
    failures, _ = cleanup_oaic_data.validate(tmp_path, tmp_path / "pub.json", tmp_path / "st.json")
    assert failures == []


# ---- scrapers -----------------------------------------------------------------------

def test_power_bi_malformed_semester_raises_instead_of_current_h1():
    from scripts.oaic.OAIC_dashboard_scraper import DashboardVisionExtractor
    extractor = DashboardVisionExtractor.__new__(DashboardVisionExtractor)
    with pytest.raises(ValueError):
        extractor.consolidate_period_data({}, "Semester 2025")


def test_power_bi_does_not_estimate_method_counts():
    from scripts.oaic.OAIC_dashboard_scraper import DashboardVisionExtractor
    extractor = DashboardVisionExtractor.__new__(DashboardVisionExtractor)
    snapshot = {"total_notifications": 532, "malicious_attacks": 308, "human_error": 193,
                "system_faults": 17, "cyber_incidents": {"phishing_pct": 28, "ransomware_pct": 21}}
    record = extractor.consolidate_period_data({2: snapshot}, "Jan-Jun 2025")
    assert all(record[f] is None for f in METHOD_FIELDS)
    assert record["cyber_incident_method_pct"] == {"phishing": 28, "ransomware": 21}


def test_power_bi_semesters_are_derived_and_stop_at_last_period():
    from datetime import datetime
    from scripts.oaic.OAIC_dashboard_scraper import POWER_BI_LAST_SEMESTER, generate_known_semesters
    semesters = generate_known_semesters(datetime(2027, 6, 1))
    assert semesters[0] == POWER_BI_LAST_SEMESTER and semesters[-1] == "Jan-Jun 2020"
    assert len(semesters) == 11


def test_pdf_scraper_sets_aside_contradicting_records():
    from scripts.oaic.oaic_data_scraper import split_contradicting_records
    ok, rejected = split_contradicting_records([good(), good(malicious_attacks=900)])
    assert len(ok) == 1 and rejected[0]["_contradictions"]


def test_pdf_scraper_does_not_estimate_cyber_count(monkeypatch):
    from scripts.oaic.oaic_data_scraper import OAICDataScraper
    scraper = OAICDataScraper.__new__(OAICDataScraper)

    class Response:
        content = (b"<html><body>There were 400 notifications. 40% of all breaches resulted "
                   b"from cyber incidents.</body></html>")

        def raise_for_status(self):
            pass

    scraper.session = type("S", (), {"get": lambda self, url: Response()})()
    stats = scraper.scrape_report_statistics(
        {"title": "t", "url": "u", "year": 2024, "period": "H1", "quarter": "H1",
         "start_month": 1, "end_month": 6})
    assert stats["cyber_incidents_percentage"] == 40
    assert stats["cyber_incidents_total"] is None
