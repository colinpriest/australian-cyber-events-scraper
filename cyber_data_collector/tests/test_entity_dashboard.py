"""Tests for the by-entity event dashboard."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.build_entity_dashboard import build_html, load_rows, summarise


@pytest.fixture
def conn():
    """Two entities: one with two events, one with a single event."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT);
        CREATE TABLE DeduplicatedEvents (
            deduplicated_event_id TEXT PRIMARY KEY, master_enriched_event_id TEXT,
            title TEXT, summary TEXT, description TEXT, event_date TEXT,
            records_affected INTEGER, vendor_organization_name TEXT, status TEXT);
        CREATE TABLE DeduplicatedEventEntities (
            deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        CREATE TABLE EnrichedEvents (
            enriched_event_id TEXT PRIMARY KEY, summary TEXT, description TEXT);
        CREATE TABLE EventDeduplicationMap (
            deduplicated_event_id TEXT, enriched_event_id TEXT);
        """
    )
    c.executemany("INSERT INTO EntitiesV2 VALUES (?,?)",
                  [(1, "Acme Ltd"), (2, "Beta Corp"), (3, "VendorCo")])
    c.executemany(
        "INSERT INTO DeduplicatedEvents VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("e2", "m2", "Acme second breach", "", "", "2024-05-01", 10, None, "Active"),
            ("e1", "m1", "Acme first breach", "", "", "2023-01-01", 5, "VendorCo", "Active"),
            ("e3", "m3", "Beta breach", "", "", "2023-06-01", 7, None, "Active"),
            ("e4", "m4", "Retired page", "", "", "2023-07-01", 0, None, "Rejected"),
        ])
    c.executemany("INSERT INTO DeduplicatedEventEntities VALUES (?,?,?)",
                  [("e1", 1, "victim"), ("e2", 1, "victim"), ("e3", 2, "victim"),
                   ("e1", 3, "vendor"), ("e4", 2, "victim")])
    c.executemany("INSERT INTO EnrichedEvents VALUES (?,?,?)",
                  [("m1", None, "First Acme incident."), ("m2", None, "Second Acme incident."),
                   ("m3", None, "Beta incident."), ("m4", None, "%PDF-1.7 binary junk")])
    c.executemany("INSERT INTO EventDeduplicationMap VALUES (?,?)",
                  [("e1", "m1"), ("e2", "m2"), ("e3", "m3")])
    return c


def test_sorted_by_entity_then_date(conn):
    rows = load_rows(conn)
    assert [(r["entity"], r["date"]) for r in rows] == [
        ("Acme Ltd", "2023-01-01"),
        ("Acme Ltd", "2024-05-01"),
        ("Beta Corp", "2023-06-01"),
    ]


def test_only_victim_role_is_listed(conn):
    """A vendor is not the attacked entity, so it heads no group."""
    assert "VendorCo" not in {r["entity"] for r in load_rows(conn)}


def test_rejected_events_excluded(conn):
    assert "Retired page" not in {r["title"] for r in load_rows(conn)}


def test_description_falls_back_to_the_enriched_record(conn):
    """DeduplicatedEvents.description is empty on every real row."""
    rows = load_rows(conn)
    assert rows[0]["description"] == "First Acme incident."


def test_multi_event_entities_counted(conn):
    stats = summarise(load_rows(conn))
    assert stats["entities"] == 2
    assert stats["multi_entities"] == 1     # Acme only
    assert stats["multi_rows"] == 2


def test_vendor_and_records_carried_through(conn):
    first = load_rows(conn)[0]
    assert first["vendor"] == "VendorCo"
    assert first["records"] == 5


def test_page_embeds_valid_json(conn):
    rows = load_rows(conn)
    page = build_html(rows, summarise(rows))
    embedded = json.loads(re.search(r"const ROWS = (\[.*?\]);\n", page, re.S).group(1))
    assert len(embedded) == len(rows)
    assert "Only entities with more than one event" in page


# ----------------------------------------------------------------------
# Ordinal size band
# ----------------------------------------------------------------------

def test_unmigrated_database_still_renders(conn):
    """The size columns arrive with a migration; the page must not need them."""
    rows = load_rows(conn)
    assert {r["size"] for r in rows} == {"UNKNOWN"}
    assert all(r["estimated"] is False for r in rows)
    build_html(rows, summarise(rows))   # must not raise


@pytest.fixture
def sized_conn(conn):
    """The same fixture, migrated and with bands recorded."""
    from cyber_data_collector.dedup import schema

    schema.migrate(conn)
    conn.execute("UPDATE EntitiesV2 SET size_estimate='HUGE', "
                 "size_basis='45,000 staff.', size_confidence=0.95 "
                 "WHERE entity_name='Acme Ltd'")
    conn.execute("UPDATE EntitiesV2 SET size_estimate='SMALL', "
                 "size_basis='12 staff.', size_confidence=0.7 "
                 "WHERE entity_name='Beta Corp'")
    return conn


def test_bands_are_attached_to_every_row(sized_conn):
    bands = {r["entity"]: r["size"] for r in load_rows(sized_conn)}
    assert bands == {"Acme Ltd": "HUGE", "Beta Corp": "SMALL"}


def test_evidence_travels_with_the_band(sized_conn):
    row = load_rows(sized_conn)[0]
    assert row["size_basis"] == "45,000 staff."
    assert row["size_confidence"] == 0.95
    assert row["estimated"] is True


def test_distribution_counts_entities_not_rows(sized_conn):
    """Acme has two events; a per-row count would report it twice as HUGE."""
    stats = summarise(load_rows(sized_conn))
    assert stats["by_size"] == {"SMALL": 1, "HUGE": 1}
    assert stats["estimated"] == 2
    assert stats["unestimated"] == 0


def test_unresearched_entities_are_reported_as_pending(sized_conn):
    sized_conn.execute("UPDATE EntitiesV2 SET size_estimate=NULL "
                       "WHERE entity_name='Beta Corp'")
    stats = summarise(load_rows(sized_conn))
    # Rendered as UNKNOWN, but counted as work still to do - unlike a
    # researched UNKNOWN, which is a finished answer.
    assert stats["by_size"] == {"UNKNOWN": 1, "HUGE": 1}
    assert stats["unestimated"] == 1


def test_page_renders_the_column_filter_and_badges(sized_conn):
    rows = load_rows(sized_conn)
    page = build_html(rows, summarise(rows))
    assert "<th>Size</th>" in page
    assert 'id="size"' in page          # the filter control
    assert ".size-HUGE" in page         # badge styling, light and dark
    assert "By size:" in page           # summary line
