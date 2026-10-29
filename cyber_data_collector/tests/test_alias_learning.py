"""Alias learning from evidence: variants of folded-away aliases, co-referring names, verified before they fold.

All offline: the Perplexity search is a fake.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from cyber_data_collector.dedup import alias_learning as al
from cyber_data_collector.dedup import entity_alias_verification as eav


@pytest.fixture(autouse=True)
def _bogus_keys(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "bogus-not-a-real-key")


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT, entity_kind TEXT);
        CREATE TABLE EntityAliases (alias_id INTEGER PRIMARY KEY AUTOINCREMENT, raw_name TEXT UNIQUE,
            canonical_name TEXT, canonical_key TEXT, source TEXT DEFAULT 'rule', confidence REAL, created_at TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, title TEXT);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, status TEXT);
        CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
        CREATE TABLE EnrichedEventEntities (enriched_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
    """)
    return c


def _entity(conn, name, kind="organisation"):
    return conn.execute("INSERT INTO EntitiesV2 (entity_name, entity_kind) VALUES (?, ?)", (name, kind)).lastrowid


def _alias(conn, raw, canonical, source="human"):
    conn.execute("INSERT INTO EntityAliases (raw_name, canonical_name, canonical_key, source) VALUES (?, ?, ?, ?)",
                 (raw, canonical, canonical.lower(), source))


def test_a_name_that_is_a_known_variant_plus_a_qualifier_is_proposed_against_the_variant(conn):
    _entity(conn, "Services Australia", "government_body")
    _entity(conn, "Australian Medicare", "government_body")
    _alias(conn, "Medicare", "Services Australia")          # "Medicare" is no longer an entity
    assert al.variant_proposals(conn) == [("Australian Medicare", "Medicare", "Services Australia")]


def test_the_proposal_is_verified_and_only_same_folds(conn):
    _entity(conn, "Services Australia", "government_body")
    _entity(conn, "Australian Medicare", "government_body")
    _alias(conn, "Medicare", "Services Australia")
    asked = []

    def search(prompt):
        asked.append(prompt)
        return {"text": json.dumps({"verdict": "same", "canonical_name": "Medicare", "reason": "a qualifier"}),
                "citations": ["https://example.org"]}

    report = eav.VerificationReport()
    groups = al.learn(conn, search=search, report=report)
    assert groups == [("Services Australia", ["Australian Medicare"])] and len(asked) == 1
    again = al.learn(conn, search=lambda p: pytest.fail("a stored verdict must be reused"), report=report)
    assert again == [("Services Australia", ["Australian Medicare"])]
    # A verdict other than same folds nothing.
    other = sqlite3.connect(":memory:")
    for stmt in conn.iterdump():
        other.execute(stmt) if not stmt.startswith(("BEGIN", "COMMIT")) else None
    other.execute("DELETE FROM EntityAliasDecisions")
    assert al.learn(other, search=lambda p: {"text": json.dumps({"verdict": "related", "reason": "a programme"}),
                                             "citations": []}) == []


def test_names_two_records_of_one_event_give_for_the_victim_are_proposed(conn):
    a, b = _entity(conn, "Medibank"), _entity(conn, "Medibank Private Limited")
    conn.execute("INSERT INTO DeduplicatedEvents VALUES ('e1', 'Active')")
    for record, entity in (("r1", a), ("r2", b)):
        conn.execute("INSERT INTO EnrichedEvents VALUES (?, 't')", (record,))
        conn.execute("INSERT INTO EventDeduplicationMap VALUES (?, 'e1')", (record,))
        conn.execute("INSERT INTO EnrichedEventEntities VALUES (?, ?, 'victim')", (record, entity))
    assert al.coreference_proposals(conn) == [("Medibank", "Medibank Private Limited")]


def test_an_event_with_many_distinct_victims_is_not_a_naming_question(conn):
    conn.execute("INSERT INTO DeduplicatedEvents VALUES ('e1', 'Active')")
    for i in range(al.MAX_COREFERENT_NAMES + 1):
        entity = _entity(conn, f"University {chr(65 + i)}")
        conn.execute("INSERT INTO EnrichedEvents VALUES (?, 't')", (f"r{i}",))
        conn.execute("INSERT INTO EventDeduplicationMap VALUES (?, 'e1')", (f"r{i}",))
        conn.execute("INSERT INTO EnrichedEventEntities VALUES (?, ?, 'victim')", (f"r{i}", entity))
    assert al.coreference_proposals(conn) == []


def test_a_system_named_after_an_organisation_maps_to_it_without_folding(conn):
    org = _entity(conn, "Services Australia", "government_body")
    product = _entity(conn, "Medicare statistics portal", "product")
    _alias(conn, "Medicare", "Services Australia")
    conn.execute("INSERT INTO DeduplicatedEvents VALUES ('e1', 'Active')")
    conn.execute("INSERT INTO EnrichedEvents VALUES ('r1', 't')")
    conn.execute("INSERT INTO EventDeduplicationMap VALUES ('r1', 'e1')")
    conn.execute("INSERT INTO EnrichedEventEntities VALUES ('r1', ?, 'product')", (product,))
    assert al.product_hosts(conn, "e1") == [("Medicare statistics portal", "Services Australia")]
    assert conn.execute("SELECT COUNT(*) FROM EntitiesV2").fetchone()[0] == 2 and org != product
