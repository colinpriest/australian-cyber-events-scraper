"""Tests for the detailed by-entity event table and the dedup workbook."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import build_entity_dashboard as entities  # noqa: E402
from scripts import build_entity_events_detail as detail  # noqa: E402
from scripts.build_dedup_dashboard import LINEAGE_COLUMNS, EVENT_COLUMNS, build_xlsx as dedup_xlsx  # noqa: E402

HOSTILE = '</script><script>alert("x")</script><img src=x onerror=alert(1)>'


@pytest.fixture
def conn():
    """Two organisations (one with two events), a vendor, a rejected event, ASD classifications and a profile."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT, entity_type TEXT, industry TEXT,
            entity_kind TEXT, size_estimate TEXT, size_confidence REAL, size_basis TEXT, size_employees INTEGER,
            size_revenue_aud REAL, is_australian BOOLEAN, headquarters_location TEXT, website_url TEXT);
        CREATE TABLE DeduplicatedEvents (
            deduplicated_event_id TEXT PRIMARY KEY, master_enriched_event_id TEXT,
            title TEXT, summary TEXT, description TEXT, event_date TEXT, event_type TEXT, severity TEXT,
            records_affected INTEGER, vendor_organization_name TEXT, status TEXT, ai_involvement TEXT,
            no_ai_probability REAL, attacking_entity_name TEXT, attack_method TEXT, event_date_precision TEXT,
            date_basis TEXT, first_reported_date TEXT, breach_source_category TEXT, personal_info_types_json TEXT,
            evidence_status TEXT, evidence_status_reason TEXT, evidence_uncertain_fields TEXT,
            total_data_sources INTEGER, has_human_override INTEGER);
        CREATE TABLE DeduplicatedEventEntities (
            deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        CREATE TABLE EnrichedEvents (
            enriched_event_id TEXT PRIMARY KEY, summary TEXT, description TEXT);
        CREATE TABLE EventDeduplicationMap (
            deduplicated_event_id TEXT, enriched_event_id TEXT);
        CREATE TABLE ASDRiskClassifications (classification_id TEXT PRIMARY KEY, deduplicated_event_id TEXT,
            severity_category TEXT, primary_stakeholder_category TEXT, impact_type TEXT, updated_at TEXT);
        CREATE TABLE EntityProfiles (profile_id INTEGER PRIMARY KEY, entity_id INTEGER, kind TEXT, value TEXT,
            source TEXT);
        """
    )
    c.executemany("INSERT INTO EntitiesV2 (entity_id, entity_name, entity_type, entity_kind, size_estimate, "
                  "size_employees, is_australian) VALUES (?,?,?,?,?,?,?)",
                  [(1, "Acme Ltd", "Retail", "organisation", "LARGE", 900, 1),
                   (2, HOSTILE, "Other", "organisation", None, None, None),
                   (3, "VendorCo", "Technology", "organisation", "HUGE", 9000, 0)])
    events = [
        ("e2", "m2", "Acme second breach", "2024-05-01", "Ransomware", "High", 10, None, "Active", "No-AI", 0.99,
         "LockBit", "ransomware", "day", '{"contact_information": true, "tax_file_numbers": false}'),
        ("e1", "m1", "Acme first breach", "2023-01-01", "Data Breach", "Medium", None, "VendorCo", "Active",
         "AI-attacker", 0.1, None, None, None, '{"contact_information": false}'),
        ("e3", "m3", HOSTILE, "2023-06-01", "Other", "Low", 7, None, "Active", None, None, None, None, None,
         "not json"),
        ("e4", "m4", "Retired page", "2023-07-01", "Other", "Low", 0, None, "Rejected", None, None, None, None,
         None, None),
    ]
    c.executemany("INSERT INTO DeduplicatedEvents (deduplicated_event_id, master_enriched_event_id, title, event_date, "
                  "event_type, severity, records_affected, vendor_organization_name, status, ai_involvement, "
                  "no_ai_probability, attacking_entity_name, attack_method, event_date_precision, "
                  "personal_info_types_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", events)
    c.executemany("INSERT INTO DeduplicatedEventEntities VALUES (?,?,?)",
                  [("e1", 1, "victim"), ("e2", 1, "victim"), ("e3", 2, "victim"),
                   ("e1", 3, "vendor"), ("e4", 2, "victim")])
    c.executemany("INSERT INTO EnrichedEvents VALUES (?,?,?)",
                  [("m1", None, "First Acme incident."), ("m2", None, "Second Acme incident."),
                   ("m3", None, HOSTILE), ("m4", None, "junk")])
    c.executemany("INSERT INTO EventDeduplicationMap VALUES (?,?)",
                  [("e1", "m1"), ("e2", "m2"), ("e3", "m3")])
    c.executemany("INSERT INTO ASDRiskClassifications VALUES (?,?,?,?,?,?)",
                  [("a1", "e1", "C4", "Large organisation(s)", "Isolated compromise", "2026-01-01"),
                   ("a2", "e2", "C3", "Large organisation(s)", "Extensive compromise", "2026-01-01"),
                   ("a4", "e4", "C1", "Federal government", "Extensive compromise", "2026-01-01")])
    c.executemany("INSERT INTO EntityProfiles (entity_id, kind, value, source) VALUES (?,?,?,?)",
                  [(1, "jurisdiction", "Australia, Victoria", "llm"), (1, "sector", "retail", "llm")])
    return c


def _html_rows(page: str):
    """The column labels and rows embedded in the page, parsed back."""
    cols = json.loads(re.search(r"const COLS = (.*);\n", page).group(1))
    rows = json.loads(re.search(r"const ROWS = (.*);\n", page).group(1))
    return cols, rows


def test_every_requested_column_is_present_once():
    keys = detail.COLUMN_KEYS
    assert len(keys) == len(set(keys))
    assert len({c.label for c in detail.COLUMNS}) == len(detail.COLUMNS)
    for key in ("entity", "entity_type", "severity", "records_affected", "event_type", "ai_involvement",
                "asd_impact", "asd_stakeholder", "asd_severity", "entity_kind", "size", "sector", "jurisdiction",
                "role", "event_date", "event_date_precision", "date_basis", "first_reported_date", "vendor",
                "service_provider", "attacker", "attack_method", "evidence_status", "breach_source_category",
                "personal_info_types", "source_records", "supplier_incident", "attribution_unresolved"):
        assert key in keys, key
    assert {c.group for c in detail.COLUMNS} == set(detail.GROUPS)
    assert {c.kind for c in detail.COLUMNS} <= set(detail.WIDTHS)


def test_rows_are_exactly_those_of_the_entity_dashboard(conn):
    base = entities.load_rows(conn)
    rows = detail.load_rows(conn)
    assert [(r["entity"], r["event_id"]) for r in rows] == [(r["entity"], r["event_id"]) for r in base]
    assert len(rows) == 3                                    # the rejected event e4 is in neither
    assert all(set(r) == set(detail.COLUMN_KEYS) for r in rows)


def test_attributes_come_from_the_event_the_entity_and_active_asd(conn):
    rows = {r["event_id"]: r for r in detail.load_rows(conn)}
    first, second = rows["e1"], rows["e2"]
    assert (second["severity"], second["records_affected"], second["event_type"]) == ("High", 10, "Ransomware")
    assert (second["asd_severity"], second["asd_stakeholder"], second["asd_impact"]) == (
        "C3", "Large organisation(s)", "Extensive compromise")
    assert first["ai_involvement"] == "AI-attacker" and first["p_ai"] == pytest.approx(0.9)
    assert first["vendor"] == "VendorCo" and first["records_affected"] is None
    assert second["entity_type"] == "Retail" and second["size"] == "LARGE" and second["size_employees"] == 900
    assert second["jurisdiction"] == "Australia, Victoria" and second["sector"] == "retail"
    assert second["is_australian"] == "yes" and second["role"] == "victim"
    assert second["personal_info_types"] == "contact information"
    assert first["personal_info_types"] == "(none)"
    assert rows["e3"]["personal_info_types"] == "(malformed)"
    assert rows["e3"]["asd_severity"] is None                # no classification: empty, never guessed
    assert second["attacker"] == "LockBit" and second["source_records"] == 1


def test_hostile_text_is_escaped_in_the_page(conn):
    rows = detail.load_rows(conn)
    page = detail.build_html(rows, note=HOSTILE + " ")
    # One script element; no database value closes it or opens markup.
    assert page.count("<script>") == 1 and page.count("</script>") == 1
    assert "<img src=x" not in page
    cols, data = _html_rows(page)
    assert HOSTILE in {row[0] for row in data}               # the text survives, as data
    # The page renders cells with textContent, never innerHTML.
    assert "innerHTML" not in page


def test_page_and_workbook_have_the_same_rows_and_columns(conn, tmp_path):
    rows = detail.load_rows(conn)
    cols, data = _html_rows(detail.build_html(rows))
    path = tmp_path / "detail.xlsx"
    detail.build_xlsx(rows, path)
    ws = load_workbook(path)[detail.SHEET]
    assert ws.max_row - 1 == len(data) == len(entities.load_rows(conn))
    assert ws.max_column == len(cols) == len(detail.COLUMNS)
    assert [c.value for c in ws[1]] == [c["label"] for c in cols]
    assert ws.freeze_panes == "A2" and ws.auto_filter.ref == f"A1:{ws.cell(1, ws.max_column).column_letter}4"
    for r, row in enumerate(data, start=2):
        assert [ws.cell(r, i + 1).value for i in range(len(cols))] == row


def test_long_text_is_cut_at_excels_limit_with_a_marker(conn, tmp_path):
    conn.execute("UPDATE EnrichedEvents SET description = ? WHERE enriched_event_id = 'm1'", ("x" * 40_000,))
    conn.execute("UPDATE DeduplicatedEvents SET attack_method = '=HYPERLINK(\"http://evil\")' "
                 "WHERE deduplicated_event_id = 'e1'")
    rows = detail.load_rows(conn)
    path = tmp_path / "detail.xlsx"
    report = detail.build_xlsx(rows, path)
    assert report.truncated_cells == 1
    ws = load_workbook(path)[detail.SHEET]
    column = detail.COLUMN_KEYS.index("description") + 1
    method = detail.COLUMN_KEYS.index("attack_method") + 1
    row = next(r for r in range(2, ws.max_row + 1) if ws.cell(r, 1 + detail.COLUMN_KEYS.index("event_id")).value == "e1")
    cut = ws.cell(row, column).value
    assert len(cut) <= 32_767 and "[TRUNCATED:" in cut and "of 40,000 characters shown]" in cut
    assert ws.cell(row, method).data_type == "s"             # text, never a formula


def test_mostly_empty_reports_sparse_columns(conn):
    sparse = dict(detail.mostly_empty(detail.load_rows(conn)))
    assert sparse["Headquarters"] == 1.0
    assert "Organisation" not in sparse


def test_dedup_workbook_flattens_records_and_decisions(tmp_path):
    data = {"events": [
        {"id": "d1", "title": HOSTILE, "date": "2024-01-02", "entity": "Acme", "stored_entity": "Acme",
         "vendor": None, "roles_stale": set(), "records_affected": 5, "severity": "High", "event_type": "Ransomware",
         "sources": 2, "member_count": 2, "overridden": False, "method": "deterministic", "min_certainty": 0.9,
         "members": [{"enriched_event_id": "r1", "contribution": "master", "similarity": 1.0, "title": "a",
                      "date": "2024-01-02 00:00:00", "entity": "Acme", "url": "http://x", "source_type": "Google"},
                     {"enriched_event_id": "r2", "contribution": "merged", "similarity": None, "title": "b",
                      "date": None, "entity": None, "url": None, "source_type": "OAIC"}],
         "decisions": [{"enriched_event_id": "r2", "action": "merge", "decided_by": "rule", "certainty": 0.9,
                        "method": "same-day", "reasoning": "=1+1", "superseded": False,
                        "created_at": "2026-01-01", "evidence": {"k": 1}}]},
        {"id": "d2", "title": "B", "date": None, "entity": None, "stored_entity": None, "vendor": "V",
         "roles_stale": True, "records_affected": None, "severity": None, "event_type": None, "sources": 1,
         "member_count": 0, "overridden": True, "method": None, "min_certainty": None, "members": [],
         "decisions": []},
    ]}
    path = tmp_path / "dedup.xlsx"
    dedup_xlsx(data, path)
    wb = load_workbook(path)
    events, lineage = wb["Events"], wb["Records and decisions"]
    assert (events.max_row - 1, events.max_column) == (2, len(EVENT_COLUMNS))
    assert (lineage.max_row - 1, lineage.max_column) == (3, len(LINEAGE_COLUMNS))
    assert events.freeze_panes == "A2" and lineage.auto_filter.ref == "A1:S4"
    stale = [c[0] for c in EVENT_COLUMNS].index("Roles stale") + 1
    assert [events.cell(r, stale).value for r in (2, 3)] == ["no", "yes"]
    kinds = [lineage.cell(r, 3).value for r in range(2, 5)]
    assert kinds == ["record", "record", "decision"]
    assert lineage.cell(4, 16).value == "=1+1" and lineage.cell(4, 16).data_type == "s"
    assert json.loads(lineage.cell(4, 19).value) == {"k": 1}
    assert events.cell(2, 2).value == HOSTILE
