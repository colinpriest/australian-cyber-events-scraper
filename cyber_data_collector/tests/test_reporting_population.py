"""The reporting population: dashboards and exports count the same incidents.

Regression for the ASD risk matrix, which LEFT JOINed every classification to
DeduplicatedEvents with no status filter. The live database held 1,201
classifications but only 643 belonged to active incidents; merged and rejected
events (history) were counted as if they were incidents.

Fixtures are small in-memory (or tmp-file) SQLite databases so each expectation
can be checked by hand.
"""

from __future__ import annotations

import ast
import logging
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cyber_data_collector.reporting.population import (  # noqa: E402
    ASD_POPULATION_FROM,
    VICTIM_ENTITY_JOINS,
    ReconciliationCheck,
    ReconciliationError,
    ReportingPeriod,
    calendar_year,
    count_classifications,
    count_classified_incidents,
    count_incidents,
    dashboard_period,
    half_year,
    half_year_containing,
    half_year_from_label,
    half_years_between,
    incident_filter,
    population_breakdown,
    reconcile,
    span_of,
)

SCHEMA = """
CREATE TABLE DeduplicatedEvents (
    deduplicated_event_id TEXT PRIMARY KEY, master_enriched_event_id TEXT,
    title TEXT, description TEXT, summary TEXT, event_type TEXT, severity TEXT,
    event_date TEXT, records_affected INTEGER, victim_organization_industry TEXT,
    victim_organization_name TEXT, attacking_entity_name TEXT, attack_method TEXT,
    vendor_organization_name TEXT, personal_info_types_json TEXT,
    breach_source_category TEXT, confidence_score REAL, australian_relevance_score REAL,
    total_data_sources INTEGER, contributing_raw_events INTEGER,
    contributing_enriched_events INTEGER, similarity_score REAL,
    deduplication_method TEXT, roles_member_signature TEXT, has_human_override INTEGER,
    dedup_method TEXT, dedup_certainty REAL, created_at TEXT, updated_at TEXT,
    is_australian_event INTEGER DEFAULT 1, is_specific_event INTEGER DEFAULT 1,
    status VARCHAR(20) DEFAULT 'Active');
CREATE TABLE ASDRiskClassifications (
    classification_id TEXT PRIMARY KEY, deduplicated_event_id TEXT UNIQUE NOT NULL,
    severity_category TEXT, primary_stakeholder_category TEXT, impact_type TEXT);
CREATE TABLE EntitiesV2 (
    entity_id INTEGER PRIMARY KEY, entity_name TEXT, entity_type TEXT, industry TEXT,
    turnover TEXT, employee_count INTEGER, is_australian INTEGER,
    headquarters_location TEXT, entity_kind TEXT);
CREATE TABLE DeduplicatedEventEntities (
    deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT,
    confidence_score REAL);
CREATE TABLE EnrichedEventEntities (
    enriched_event_id TEXT, entity_id INTEGER, relationship_type TEXT,
    confidence_score REAL);
CREATE TABLE EnrichedEvents (
    enriched_event_id TEXT PRIMARY KEY, perplexity_enrichment_data TEXT,
    raw_event_id TEXT, title TEXT, description TEXT, summary TEXT, event_date TEXT);
CREATE TABLE EventDeduplicationMap (
    deduplicated_event_id TEXT, enriched_event_id TEXT, raw_event_id TEXT,
    contribution_type TEXT, similarity_score REAL);
CREATE TABLE RawEvents (raw_event_id TEXT, discovered_at TEXT, raw_title TEXT,
    raw_description TEXT, raw_content TEXT, source_url TEXT, source_type TEXT);
CREATE TABLE DeduplicatedEventSources (
    deduplicated_event_id TEXT, source_url TEXT, source_type TEXT,
    credibility_score REAL, content_snippet TEXT, discovered_at TEXT);
"""

ISOLATED = ("Isolated compromise", "Large organisation(s)")
LOW = ("Low-level malicious attack", "Small organisation(s)")

# (id, status, event_date, severity, event_type, classification or None)
EVENTS = [
    # --- the population in the dashboard period (2020-01-01 .. today) -----------
    ("A1", "Active", "2024-03-10", "High", "Ransomware", ISOLATED),
    ("A2", "Active", "2024-03-20", "Medium", "Data Breach", ISOLATED),
    ("A3", "Active", "2025-07-01", "Low", "Phishing", LOW),
    ("A6", "Active", "2025-01-15", "Medium", "Other", None),          # unclassified
    # Same month and meaning as A1's 'High' but a legacy spelling (21 such rows
    # exist in the live database): must be added to A1's bar, not overwrite it.
    ("A7", "Active", "2024-03-12", "EventSeverity.HIGH", "CyberEventType.RANSOMWARE", None),
    # --- active, but outside the dashboard period --------------------------------
    ("A4", "Active", "2019-12-31", "Critical", "Ransomware", ISOLATED),
    ("A5", "Active", None, "Low", "Other", LOW),                       # undated
    # --- history: classified, but not incidents ----------------------------------
    ("M1", "Merged", "2024-03-10", "High", "Ransomware", ISOLATED),
    ("M2", "Merged", "2025-01-15", "Medium", "Other", LOW),
    ("R1", "Rejected", "2024-05-05", "Low", "Other", LOW),
]


def _build(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.executescript(SCHEMA)
    for eid, status, when, sev, etype, cls in EVENTS:
        conn.execute(
            "INSERT INTO DeduplicatedEvents (deduplicated_event_id, master_enriched_event_id, "
            "title, event_type, severity, event_date, records_affected, status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (eid, f"e-{eid}", f"title {eid}", etype, sev, when, 100, status))
        conn.execute("INSERT INTO EnrichedEvents (enriched_event_id, title) VALUES (?,?)",
                     (f"e-{eid}", f"title {eid}"))
        if cls:
            conn.execute("INSERT INTO ASDRiskClassifications VALUES (?,?,?,?,?)",
                         (f"c-{eid}", eid, "C3", cls[1], cls[0]))
    conn.executemany("INSERT INTO EntitiesV2 (entity_id, entity_name) VALUES (?,?)",
                     [(1, "Acme"), (2, "Regulator"), (3, "Gone Corp")])
    conn.executemany("INSERT INTO DeduplicatedEventEntities (deduplicated_event_id, "
                     "entity_id, relationship_type) VALUES (?,?,?)", [
        ("A1", 1, "victim"), ("A1", 2, "regulator"),   # only the victim may attach
        ("M1", 3, "victim"),                           # a merged event's entity
    ])
    conn.commit()
    return conn


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _build(c)
    yield c
    c.close()


@pytest.fixture
def db_file(tmp_path):
    path = tmp_path / "events.db"
    c = sqlite3.connect(path)
    _build(c)
    c.close()
    return path


# --------------------------------------------------------------------------
# Reporting periods
# --------------------------------------------------------------------------

class TestReportingPeriod:
    def test_both_ends_are_inclusive(self):
        p = ReportingPeriod(date(2025, 1, 1), date(2025, 6, 30))
        assert p.contains("2025-01-01") and p.contains("2025-06-30")
        assert not p.contains("2024-12-31") and not p.contains("2025-07-01")

    def test_sql_is_half_open_on_the_following_day(self):
        sql, params = ReportingPeriod(date(2025, 1, 1), date(2025, 6, 30)).where("e.d")
        assert sql == "e.d >= ? AND e.d < ?"
        assert params == ["2025-01-01", "2025-07-01"]

    def test_a_timestamp_on_the_last_day_is_inside_the_period(self, conn):
        # BETWEEN '2025-01-01' AND '2025-06-30' (and <= '2025-06-30') would lose it.
        conn.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, event_date) "
                     "VALUES ('T1', '2025-06-30 14:00:00')")
        h1 = half_year(2025, "H1")
        assert count_incidents(conn, h1) == 2          # A6 (15 Jan) and T1 (30 Jun 14:00)
        ids = {r[0] for r in conn.execute(
            "SELECT deduplicated_event_id FROM DeduplicatedEvents WHERE "
            + incident_filter(h1)[0], incident_filter(h1)[1])}
        assert "T1" in ids
        naive = conn.execute(
            "SELECT COUNT(*) FROM DeduplicatedEvents WHERE status='Active' "
            "AND event_date BETWEEN '2025-01-01' AND '2025-06-30' "
            "AND deduplicated_event_id = 'T1'").fetchone()[0]
        assert naive == 0, "documents the bug the half-open form avoids"

    def test_undated_and_garbage_values_are_outside_every_period(self):
        p = calendar_year(2025)
        assert not p.contains(None) and not p.contains("") and not p.contains("n/a")

    def test_end_before_start_is_rejected(self):
        with pytest.raises(ValueError):
            ReportingPeriod(date(2025, 2, 1), date(2025, 1, 1))

    def test_half_year_helpers(self):
        h1, h2 = half_year(2024, "H1"), half_year(2024, "h2")
        assert (h1.start, h1.end) == (date(2024, 1, 1), date(2024, 6, 30))
        assert (h2.start, h2.end) == (date(2024, 7, 1), date(2024, 12, 31))
        assert half_year_from_label("2025 H2") == half_year(2025, "H2")
        assert half_year_containing("2025-06-30").label == "2025 H1"
        assert half_year_containing(datetime(2025, 7, 1, 9)).label == "2025 H2"
        with pytest.raises(ValueError):
            half_year(2024, "H3")

    def test_calendar_year_and_half_years_partition_it(self):
        year = calendar_year(2024)
        assert (year.start, year.end) == (date(2024, 1, 1), date(2024, 12, 31))
        halves = half_years_between(2024, "2024-12-31")
        assert [h.label for h in halves] == ["2024 H1", "2024 H2"]
        assert halves[0].end_exclusive == halves[1].start

    def test_half_years_between_runs_through_the_half_containing_the_end(self):
        labels = [h.label for h in half_years_between(2023, "2025-02-01")]
        assert labels == ["2023 H1", "2023 H2", "2024 H1", "2024 H2", "2025 H1"]

    def test_span_of_labels_whole_years_and_odd_halves(self):
        assert span_of([half_year(2020, "H1"), half_year(2025, "H2")]).label == "2020-2025"
        assert span_of([half_year(2022, "H1"), half_year(2022, "H2")]).label == "2022"
        odd = span_of([half_year(2020, "H2"), half_year(2025, "H1")])
        assert odd.label == "2020 H2 to 2025 H1"
        assert span_of([]) is None

    def test_intersect_and_clip(self):
        window = ReportingPeriod(date(2020, 1, 1), date(2026, 10, 27))
        this_year = calendar_year(2026).intersect(window)
        assert (this_year.start, this_year.end) == (date(2026, 1, 1), date(2026, 10, 27))
        assert calendar_year(2010).intersect(window) is None
        assert window.clipped_to("2024-12-31").end == date(2024, 12, 31)
        assert window.clipped_to("2030-01-01").end == window.end

    def test_months_iterates_every_month_touched(self):
        p = ReportingPeriod(date(2024, 11, 15), date(2025, 2, 3))
        assert list(p.months()) == [(2024, 11), (2024, 12), (2025, 1), (2025, 2)]

    def test_dashboard_period_starts_in_2020(self):
        p = dashboard_period("2026-10-27")
        assert (p.start, p.end) == (date(2020, 1, 1), date(2026, 10, 27))


# --------------------------------------------------------------------------
# The population
# --------------------------------------------------------------------------

PERIOD = ReportingPeriod(date(2020, 1, 1), date(2026, 10, 27))


class TestPopulation:
    def test_only_active_rows_are_incidents(self, conn):
        # A1 A2 A3 A6 A7 A4 A5 are active; M1 M2 are merged and R1 rejected.
        assert count_incidents(conn) == 7

    def test_period_excludes_pre_2020_and_undated(self, conn):
        assert count_incidents(conn, PERIOD) == 5     # A1 A2 A3 A6 A7

    def test_first_and_last_day_are_inside(self, conn):
        conn.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, event_date) "
                     "VALUES ('F1', '2020-01-01'), ('L1', '2026-10-27'), ('X1', '2026-10-28')")
        assert count_incidents(conn, PERIOD) == 7     # + F1 and L1, not X1
        assert count_incidents(conn, ReportingPeriod(date(2020, 1, 2), date(2026, 10, 26))) == 5

    def test_breakdown_accounts_for_every_row(self, conn):
        b = population_breakdown(conn, PERIOD)
        assert (b["total_rows"], b["active"], b["merged"], b["rejected"]) == (10, 7, 2, 1)
        assert b["active_undated"] == 1
        assert b["active_in_period"] == 5
        assert b["active_dated_outside_period"] == 1
        assert (b["active_in_period"] + b["active_dated_outside_period"]
                + b["active_undated"]) == b["active"]

    def test_merged_and_rejected_classifications_are_excluded(self, conn):
        total_rows = conn.execute("SELECT COUNT(*) FROM ASDRiskClassifications").fetchone()[0]
        assert total_rows == 8        # what the old matrix reported
        # A1 A2 A3 (in period), A4 (2019) and A5 (undated) are active and classified.
        assert count_classifications(conn) == 5
        assert count_classifications(conn, PERIOD) == 3
        assert count_classified_incidents(conn, PERIOD) == 3

    def test_orphan_classification_is_not_counted(self, conn):
        conn.execute("INSERT INTO ASDRiskClassifications VALUES ('c-orphan','NOPE','C3','x','y')")
        assert count_classifications(conn) == 5

    def test_victim_entities_attach_only_to_active_incidents_and_the_victim_role(self, conn):
        where, params = incident_filter(None, "de")
        rows = conn.execute(
            f"SELECT de.deduplicated_event_id, e.entity_name FROM DeduplicatedEvents de "
            f"{VICTIM_ENTITY_JOINS} WHERE {where} AND e.entity_name IS NOT NULL", params
        ).fetchall()
        assert [tuple(r) for r in rows] == [("A1", "Acme")]   # not Regulator, not Gone Corp

    def test_asd_population_join_is_inner(self):
        assert " JOIN " in ASD_POPULATION_FROM and "LEFT" not in ASD_POPULATION_FROM


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------

class TestReconcile:
    def test_matching_totals_pass(self):
        checks = [ReconciliationCheck("a", 5, 5), ReconciliationCheck("b", 9, 4, relation="<=")]
        assert reconcile(checks, "t") == checks

    def test_mismatch_raises_and_logs_loudly(self, caplog):
        with caplog.at_level(logging.ERROR):
            with pytest.raises(ReconciliationError) as err:
                reconcile([ReconciliationCheck("ASD matrix", 3, 7)], "dashboard")
        assert "ASD matrix" in str(err.value) and "7" in str(err.value)
        assert any("RECONCILIATION FAILED" in r.message for r in caplog.records)

    def test_upper_bound_relation_fails_when_exceeded(self):
        with pytest.raises(ReconciliationError):
            reconcile([ReconciliationCheck("subset", 4, 5, relation="<=")])

    def test_bad_relation_is_rejected(self):
        with pytest.raises(ValueError):
            ReconciliationCheck("x", 1, 1, relation=">")


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

pytest.importorskip("scripts.build_static_dashboard")


@pytest.fixture
def sd():
    """The dashboard module as currently registered in sys.modules.

    Resolved per test, not at import: test_dashboard_oaic_merge pops and
    re-imports the module, so a module-level reference can go stale and
    monkeypatching it would miss the copy the code under test imports.
    """
    import importlib
    return importlib.import_module("scripts.build_static_dashboard")


def _legacy_asd_matrix(conn, year=None, period=None):
    """The pre-fix query: LEFT JOIN, no status filter. Reproduced to show it is caught."""
    total = conn.execute(
        "SELECT COUNT(*) FROM ASDRiskClassifications arc LEFT JOIN DeduplicatedEvents de "
        "ON arc.deduplicated_event_id = de.deduplicated_event_id").fetchone()[0]
    return {"total_classifications": total, "unmapped_classifications": 0,
            "matrix": [], "year": year}


class TestDashboardQueries:
    def test_asd_matrix_counts_only_active_incidents_in_period(self, conn, sd):
        matrix = sd.get_asd_risk_matrix(conn, period=PERIOD)
        assert matrix["total_classifications"] == 3          # not 7
        assert matrix["population_classifications"] == 3
        assert matrix["unmapped_classifications"] == 0
        assert matrix["period_start"] == "2020-01-01"
        cells = {r["impact_type"]: r["counts"] for r in matrix["matrix"]}
        assert sum(cells["Isolated compromise"].values()) == 2    # A1 A2, not M1
        assert sum(cells["Low-level malicious attack"].values()) == 1  # A3, not M2 / R1

    def test_asd_year_view_is_clipped_to_the_period(self, conn, sd):
        assert sd.get_asd_risk_matrix(conn, 2024, period=PERIOD)["total_classifications"] == 2
        assert sd.get_asd_risk_matrix(conn, 2025, period=PERIOD)["total_classifications"] == 1
        # 2019 lies before the dashboard window: A4 is active and classified but not reported.
        assert sd.get_asd_risk_matrix(conn, 2019, period=PERIOD)["total_classifications"] == 0

    def test_unplaceable_classification_is_counted_as_unmapped_not_dropped(self, conn, sd):
        conn.execute("UPDATE ASDRiskClassifications SET impact_type='Made-up impact' "
                     "WHERE deduplicated_event_id='A1'")
        matrix = sd.get_asd_risk_matrix(conn, period=PERIOD)
        assert matrix["total_classifications"] == 2
        assert matrix["unmapped_classifications"] == 1
        assert (matrix["total_classifications"] + matrix["unmapped_classifications"]
                == count_classifications(conn, PERIOD))

    def test_monthly_counts_exclude_history_and_out_of_period(self, conn, sd):
        counts = sd.get_monthly_event_counts(conn, "2020-01-01", "2026-10-27")
        assert counts["total_events"] == 5
        assert dict(zip(counts["months"], counts["counts"]))["2024-03"] == 3  # A1 A2 A7, not M1

    def test_period_end_is_inclusive_in_dashboard_queries(self, conn, sd):
        conn.execute("UPDATE DeduplicatedEvents SET event_date='2024-03-31 23:00:00' "
                     "WHERE deduplicated_event_id='A2'")
        counts = sd.get_monthly_event_counts(conn, "2024-03-01", "2024-03-31")
        assert counts["total_events"] == 3

    def test_two_spellings_of_one_severity_add_up(self, conn, sd):
        trends = sd.get_monthly_severity_trends(conn, "2020-01-01", "2026-10-27")
        march = trends["months"].index("2024-03")
        assert trends["data"]["Medium"][march] == 1
        assert trends["data"]["High"][march] == 2     # A1 'High' + A7 'EventSeverity.HIGH'
        total = sum(sum(v) for v in trends["data"].values())
        assert total == 5

    def test_half_yearly_counts_come_from_the_period(self, conn, sd):
        counts = sd.get_half_yearly_database_counts(conn, "2020-01-01", "2026-10-27")
        assert sum(counts["database_counts"]) == 5
        assert "2019 H2" not in counts["periods"]

    def test_individuals_distribution_buckets_cover_the_open_ends(self, conn, db_file, sd):
        conn.close()
        c = sqlite3.connect(db_file)
        c.execute("UPDATE DeduplicatedEvents SET records_affected=750000 WHERE deduplicated_event_id='A1'")
        c.execute("UPDATE DeduplicatedEvents SET records_affected=25000000 WHERE deduplicated_event_id='A2'")
        c.commit()
        c.close()
        oaic = [{"year": 2024, "period": "H1", "individuals_affected_distribution": [
            {"range": "500,001-1,000,000", "count": 2}, {"range": "10,000,001+", "count": 1}]}]
        out = sd.prepare_individuals_affected_distribution_comparison(oaic, str(db_file))
        by_bucket = dict(zip(out["buckets"], out["db_counts"]))
        assert by_bucket["500,001-1,000,000"] == 1 and by_bucket["10,000,001+"] == 1
        # Only the semesters OAIC reported (2024 H1) are compared: A1 A2 A7. A3 and A6
        # are dated 2025 and used to be counted against a 2024 H1 distribution.
        assert sum(out["db_counts"]) == 3
        assert (out["period_start"], out["period_end"]) == ("2024-01-01", "2024-06-30")

    def test_oaic_sectors_use_one_window_for_both_sides(self, db_file, sd):
        oaic = [
            {"year": 2019, "period": "H2", "top_sectors": [{"sector": "Health", "notifications": 99}]},
            {"year": 2024, "period": "H1", "top_sectors": [{"sector": "Health", "notifications": 40}]},
            {"year": 2024, "period": "H2", "top_sectors": [{"sector": "Health", "notifications": 50}]},
        ]
        out = sd.prepare_oaic_sectors_data(oaic, str(db_file))
        assert out["period_label"] == "2024"
        assert (out["period_start"], out["period_end"]) == ("2024-01-01", "2024-12-31")
        assert out["oaic_counts"] == [90]       # 2019 H2 is before the reporting start


class TestDashboardReconciliation:
    @pytest.fixture(autouse=True)
    def _no_oaic_files(self, monkeypatch, sd):
        monkeypatch.setattr(sd, "load_oaic_data", lambda: [])

    def test_consistent_database_builds_and_states_its_population(self, db_file, tmp_path, sd):
        out = sd.build_dashboard_file(str(db_file), str(tmp_path / "out"))
        page = Path(out).read_text(encoding="utf-8")
        assert "Population: 5 active incidents dated 2020-01-01 to" in page
        assert "3 with an ASD classification" in page
        assert "Excludes 2 merged and 1 rejected records" in page

    def test_old_asd_query_would_fail_the_build(self, db_file, tmp_path, monkeypatch, caplog, sd):
        monkeypatch.setattr(sd, "get_asd_risk_matrix", _legacy_asd_matrix)
        out_dir = tmp_path / "out"
        with caplog.at_level(logging.ERROR):
            with pytest.raises(ReconciliationError) as err:
                sd.build_dashboard_file(str(db_file), str(out_dir))
        assert "ASD matrix" in str(err.value)
        assert any("RECONCILIATION FAILED" in r.message for r in caplog.records)
        assert not (out_dir / "index.html").exists(), "a wrong page must not be published"

    def test_a_failed_build_leaves_the_previous_page_untouched(self, db_file, tmp_path, monkeypatch, sd):
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "index.html").write_text("previous good page", encoding="utf-8")
        monkeypatch.setattr(sd, "get_asd_risk_matrix", _legacy_asd_matrix)
        with pytest.raises(ReconciliationError):
            sd.build_dashboard_file(str(db_file), str(out_dir))
        assert (out_dir / "index.html").read_text(encoding="utf-8") == "previous good page"

    def test_inconsistent_chart_payload_is_caught_by_the_checks(self, conn, sd):
        data = {
            "monthly_counts": {"counts": [3, 1]},                    # 4, population has 5
            "oaic_comparison": {"database_counts": [5]},
            "overall_event_type_mix": {"counts": [5]},
            "entity_types": {"types": [{"events": 5}]},
            "severity_trends": {"data": {"High": [5]}},
            "event_type_mix": {"types": {"x": [5]}},
            "records_histogram": {"frequencies": [5]},
            "asd_risk_all": {"total_classifications": 3, "unmapped_classifications": 0},
            "asd_risk_current": {"total_classifications": 0},
            "asd_risk_previous": {"total_classifications": 1},   # 2025: A3
        }
        checks = sd.build_reconciliation_checks(
            conn, data, PERIOD, PERIOD, {"counts": [5]})
        failed = [c for c in checks if not c.is_ok]
        assert [c.name for c in failed] == ["Monthly unique events (trend series)"]
        with pytest.raises(ReconciliationError):
            reconcile(checks, "test")

    def test_cli_exits_non_zero_when_totals_do_not_reconcile(self, db_file, tmp_path, monkeypatch, sd):
        monkeypatch.setattr(sd, "get_asd_risk_matrix", _legacy_asd_matrix)
        monkeypatch.setattr(sys, "argv", ["build_static_dashboard.py", "--db-path", str(db_file),
                                          "--out-dir", str(tmp_path / "out")])
        with pytest.raises(SystemExit) as exit_info:
            sd.main()
        assert exit_info.value.code == 1

    def test_check_only_writes_nothing(self, db_file, tmp_path, sd):
        out_dir = tmp_path / "never"
        assert sd.build_dashboard_file(str(db_file), str(out_dir), check_only=True) == ""
        assert not out_dir.exists()


# --------------------------------------------------------------------------
# Integrity checker
# --------------------------------------------------------------------------

integrity = pytest.importorskip("scripts.check_data_integrity")


class TestIntegrityChecks:
    def test_population_summary_reports_what_is_excluded(self, conn):
        result = integrity.check_reporting_population(conn)
        assert result.ok
        text = "\n".join(result.details)
        assert "2 merged, 1 rejected" in text
        assert "8 ASD classification rows in total, of which 5 belong to active incidents" in text

    def test_two_classifications_for_one_incident_fail(self):
        c = sqlite3.connect(":memory:")
        c.executescript(SCHEMA.replace("deduplicated_event_id TEXT UNIQUE NOT NULL",
                                       "deduplicated_event_id TEXT NOT NULL"))
        c.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, event_date) "
                  "VALUES ('A', '2024-01-01')")
        c.executemany("INSERT INTO ASDRiskClassifications VALUES (?,?,?,?,?)",
                      [("c1", "A", "C3", "x", "y"), ("c2", "A", "C3", "x", "y")])
        result = integrity.check_reporting_population(c)
        assert not result.ok and "more than one classification" in result.summary

    def test_flags_check_fails_for_active_non_australian_incident(self, conn):
        assert integrity.check_population_flags_consistent(conn).ok
        conn.execute("UPDATE DeduplicatedEvents SET is_australian_event=0 WHERE deduplicated_event_id='A1'")
        assert not integrity.check_population_flags_consistent(conn).ok

    def test_merged_row_with_zero_flag_is_not_a_population_problem(self, conn):
        conn.execute("UPDATE DeduplicatedEvents SET is_specific_event=0 WHERE deduplicated_event_id='M1'")
        assert integrity.check_population_flags_consistent(conn).ok

    def test_future_dated_incident_fails(self, conn):
        assert integrity.check_no_future_dated_incidents(conn).ok
        conn.execute("UPDATE DeduplicatedEvents SET event_date='2999-01-01' WHERE deduplicated_event_id='A1'")
        assert not integrity.check_no_future_dated_incidents(conn).ok

    def test_dashboard_reconcile_check_passes_and_fails_with_the_data(self, db_file, monkeypatch, sd):
        monkeypatch.setattr(sd, "load_oaic_data", lambda: [])
        c = sqlite3.connect(db_file)
        assert integrity.check_dashboard_totals_reconcile(c).ok
        monkeypatch.setattr(sd, "get_asd_risk_matrix", _legacy_asd_matrix)
        result = integrity.check_dashboard_totals_reconcile(c)
        c.close()
        assert not result.ok and any("ASD matrix" in d for d in result.details)


# --------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------

class TestExports:
    def test_detailed_export_contains_only_the_population(self, db_file, tmp_path):
        from scripts.export.export_cyber_events import CyberEventsExporter
        out = tmp_path / "events.csv"
        with CyberEventsExporter(str(db_file)) as exporter:
            assert exporter.export_deduplicated_events_with_details(str(out), "csv")
        import pandas as pd
        df = pd.read_csv(out)
        assert set(df["status"]) == {"Active"}
        assert sorted(df["deduplicated_event_id"]) == ["A1", "A2", "A3", "A6", "A7"]

    def test_all_dates_adds_out_of_period_incidents_but_never_history(self, db_file, tmp_path):
        from scripts.export.export_cyber_events import CyberEventsExporter
        out = tmp_path / "events.csv"
        with CyberEventsExporter(str(db_file)) as exporter:
            assert exporter.export_deduplicated_events_with_details(str(out), "csv", all_dates=True)
        import pandas as pd
        df = pd.read_csv(out)
        assert sorted(df["deduplicated_event_id"]) == ["A1", "A2", "A3", "A4", "A5", "A6", "A7"]

    def test_history_is_exported_only_when_asked_for_and_is_labelled(self, db_file, tmp_path, capsys):
        from scripts.export.export_cyber_events import CyberEventsExporter
        out = tmp_path / "events.csv"
        with CyberEventsExporter(str(db_file)) as exporter:
            assert exporter.export_table("DeduplicatedEvents", str(out), "csv", include_history=True)
        import pandas as pd
        assert {"Active", "Merged", "Rejected"} <= set(pd.read_csv(out)["status"])
        assert "deduplication history, not a count of incidents" in capsys.readouterr().out

    def test_plain_table_export_is_also_the_population(self, db_file, tmp_path):
        from scripts.export.export_cyber_events import CyberEventsExporter
        out = tmp_path / "table.csv"
        with CyberEventsExporter(str(db_file)) as exporter:
            assert exporter.export_table("DeduplicatedEvents", str(out), "csv")
        import pandas as pd
        assert set(pd.read_csv(out)["status"]) == {"Active"}
        assert len(pd.read_csv(out)) == 5

    def test_explicit_date_range_is_inclusive_and_still_active_only(self, db_file, tmp_path):
        from scripts.export.export_cyber_events import CyberEventsExporter
        out = tmp_path / "range.csv"
        with CyberEventsExporter(str(db_file)) as exporter:
            assert exporter.export_deduplicated_events_with_details(
                str(out), "csv", date_range=("2024-03-10", "2024-03-20"))
        import pandas as pd
        assert sorted(pd.read_csv(out)["deduplicated_event_id"]) == ["A1", "A2", "A7"]

    def test_excel_export_has_exactly_the_population(self, db_file, tmp_path):
        pytest.importorskip("openpyxl")
        pytest.importorskip("openai")
        pytest.importorskip("tqdm")
        from scripts.export.export_events_excel import export_events_to_excel
        import pandas as pd
        out = tmp_path / "events.xlsx"
        export_events_to_excel(db_path=str(db_file), output_path=str(out), use_llm=False)
        assert len(pd.read_excel(out)) == 5
        export_events_to_excel(db_path=str(db_file), output_path=str(out), use_llm=False,
                               all_dates=True)
        assert len(pd.read_excel(out)) == 7


# --------------------------------------------------------------------------
# Entity and dedup dashboards
# --------------------------------------------------------------------------

class TestReviewDashboards:
    def test_entity_dashboard_note_states_the_population(self, conn):
        from scripts.build_entity_dashboard import load_rows, population_note, summarise
        rows = load_rows(conn)
        note = population_note(conn, summarise(rows))
        assert "7 in the database" in note
        assert [r["entity"] for r in rows] == ["Acme"]      # the merged event's entity is absent

    def test_entity_dashboard_refuses_to_list_more_than_the_population(self, conn):
        from scripts.build_entity_dashboard import population_note
        with pytest.raises(ReconciliationError):
            population_note(conn, {"events": 8})

    def test_dedup_dashboard_lists_active_incidents_and_labels_history(self, conn):
        from scripts.build_dedup_dashboard import collect
        data = collect(conn)
        assert len(data["events"]) == 7
        stats = data["stats"]
        assert (stats["active_population"], stats["history_merged"], stats["history_rejected"]) == (7, 2, 1)


# --------------------------------------------------------------------------
# Guard: no query writes its own copy of the population or a date range
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]
REPORTING_SCRIPTS = [
    "scripts/build_static_dashboard.py",
    "scripts/build_entity_dashboard.py",
    "scripts/build_dedup_dashboard.py",
    "scripts/export/export_events_excel.py",
    "scripts/export/export_cyber_events.py",
]


def _sql_strings(path: Path):
    """Every string literal in a module except docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value


@pytest.mark.parametrize("relative", REPORTING_SCRIPTS)
def test_queries_use_the_shared_population_and_periods(relative):
    """population.py is the one place allowed to spell out a status or a date range."""
    offenders = []
    for lineno, text in _sql_strings(ROOT / relative):
        if (re.search(r"status\s*(=|IS|IN)\s*\(?\s*'(Active|Merged|Rejected)'", text, re.I)
                or re.search(r"COALESCE\(\s*\w*\.?status", text, re.I)):
            offenders.append((lineno, "own status filter", text.strip()[:70]))
        if (re.search(r"event_date\s*(>=|<=|<|>|BETWEEN)\s*'", text, re.I)
                or re.search(r"'20\d\d-\d\d-\d\d'", text)):
            offenders.append((lineno, "hard-coded date", text.strip()[:70]))
    assert not offenders, f"{relative}: {offenders}"
