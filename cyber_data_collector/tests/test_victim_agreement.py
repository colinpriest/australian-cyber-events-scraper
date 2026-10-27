"""Victim agreement: an event's victim must agree with the victim its records name.

Each positive case is a wrong attribution that put a false second attack into
the recurrence analysis's short-gap band. Each negative case is a proposal an
earlier draft of the rule made on the live database and should not have.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from cyber_data_collector.dedup.victim_agreement import (
    apply_victim,
    proposed_victim,
    strip_legal_suffix,
)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT, entity_kind TEXT,
            entity_type TEXT, industry TEXT, is_australian INTEGER);
        CREATE TABLE EntityAliases (raw_name TEXT, canonical_name TEXT, canonical_key TEXT);
        CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_url TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title TEXT,
            event_date TEXT, summary TEXT, description TEXT, perplexity_enrichment_data TEXT);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, title TEXT, event_date TEXT,
            status TEXT, victim_organization_name TEXT, victim_organization_industry TEXT,
            vendor_organization_name TEXT, updated_at TEXT);
        CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
        CREATE TABLE EnrichedEventEntities (enriched_event_id TEXT, entity_id INTEGER,
            relationship_type TEXT, confidence_score REAL);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER,
            relationship_type TEXT, confidence_score REAL, source_count INTEGER);
    """)
    return c


def _entity(conn, name):
    row = conn.execute("SELECT entity_id FROM EntitiesV2 WHERE entity_name = ?", (name,)).fetchone()
    if row:
        return row[0]
    return conn.execute("INSERT INTO EntitiesV2 (entity_name, entity_kind) VALUES (?, 'organisation')",
                        (name,)).lastrowid


def _event(conn, did, victim, records, vendor=None, links=()):
    """records: [(title, body, perplexity victim)]; links: [(entity, role)] on every record."""
    conn.execute("INSERT INTO DeduplicatedEvents VALUES (?, ?, '2022-12-15', 'Active', ?, NULL, ?, NULL)",
                 (did, records[0][0], victim, vendor))
    links = [(victim, "victim"), *links]
    for i, (title, body, org) in enumerate(records):
        eid = f"{did}-{i}"
        conn.execute("INSERT INTO EnrichedEvents VALUES (?, ?, ?, '2022-12-15', ?, NULL, ?)",
                     (eid, eid, title, body, json.dumps({"formal_entity_name": org})))
        conn.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?)", (eid, did))
        for name, role in links:
            conn.execute("INSERT INTO EnrichedEventEntities VALUES (?, ?, ?, NULL)", (eid, _entity(conn, name), role))
    for name, role in links:
        conn.execute("INSERT INTO DeduplicatedEventEntities VALUES (?, ?, ?, NULL, ?)",
                     (did, _entity(conn, name), role, len(records)))


def _role(conn, did, name):
    row = conn.execute("""SELECT relationship_type FROM DeduplicatedEventEntities dee
                          JOIN EntitiesV2 v USING (entity_id)
                          WHERE deduplicated_event_id = ? AND entity_name = ?""", (did, name)).fetchone()
    return row[0] if row else None


def test_strip_legal_suffix():
    assert strip_legal_suffix("Sharp Office Systems Pty Ltd") == "Sharp Office Systems"
    assert strip_legal_suffix("TPG Telecom Limited") == "TPG Telecom"


def test_wrong_victim_from_a_mention_is_replaced(conn):
    # A Reuters piece on TPG's breach mentions Optus; the role pass chose Optus.
    _event(conn, "tpg", "Optus",
           [("TPG Telecom email breach (December 2022)",
             "TPG Telecom said hackers accessed an email hosting service.", "TPG Telecom Limited")],
           links=[("TPG Telecom", "affected_customer")])
    proposal = proposed_victim(conn, "tpg", "Optus", None)
    assert proposal["name"] == "TPG Telecom"

    assert apply_victim(conn, "tpg", "Optus", proposal["name"]) == "TPG Telecom"
    assert _role(conn, "tpg", "TPG Telecom") == "victim"
    assert _role(conn, "tpg", "Optus") == "bystander"
    stored = conn.execute("SELECT victim_organization_name FROM DeduplicatedEvents").fetchone()[0]
    assert stored == "TPG Telecom"


def test_shared_brand_word_in_a_different_business_is_replaced(conn):
    # Sharp Office Systems had been folded into Sharp Motor Group on "sharp".
    _event(conn, "sharp", "Sharp Motor Group",
           [("The Gentlemen ransomware attack on Sharp Office", "Sharp Office confirmed an incident.",
             "Sharp Office Systems Pty Ltd")] * 2)
    assert proposed_victim(conn, "sharp", "Sharp Motor Group", None)["name"] == "Sharp Office Systems"


def test_legal_name_of_the_titled_victim_is_not_a_different_victim(conn):
    _event(conn, "ibm", "IBM",
           [("IBM staff data allegedly leaked in cyber attack", "International Business Machines staff data",
             "International Business Machines Corporation")])
    assert proposed_victim(conn, "ibm", "IBM", None) is None


def test_spelling_variant_counts_as_agreement(conn):
    _event(conn, "ollie", "Ollie's Place",
           [("Aussie kidswear brand launches investigation", "Ollies Place Kidswear is investigating.",
             "Ollies Place Kidswear")])
    assert proposed_victim(conn, "ollie", "Ollie's Place", None) is None


def test_supplier_named_by_perplexity_is_not_the_victim(conn):
    _event(conn, "canvas", "University of Melbourne",
           [("Canvas breach hits students", "Instructure's Canvas platform was breached.", "Instructure Inc")],
           vendor="Instructure")
    assert proposed_victim(conn, "canvas", "University of Melbourne", "Instructure") is None


def test_party_labelled_product_or_regulator_is_not_the_victim(conn):
    _event(conn, "oracle", "62 Australian Government entities",
           [("Oracle E-Business Suite Zero-Day Exploitation Campaign", "Oracle customers were hit.",
             "Oracle Corporation")],
           links=[("Oracle E-Business Suite", "product")])
    assert proposed_victim(conn, "oracle", "62 Australian Government entities", None) is None


def test_one_record_naming_the_victim_keeps_it(conn):
    _event(conn, "mixed", "Toll Group",
           [("Toll Group hit by ransomware", "Toll Group confirmed.", "Toll Holdings Limited"),
            ("Logistics firm attacked", "Henning Harders was attacked.", "Henning Harders Pty Ltd")])
    assert proposed_victim(conn, "mixed", "Toll Group", None) is None
