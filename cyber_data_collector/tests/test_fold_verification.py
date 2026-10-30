"""Model folds reached by chaining are asked about directly; folds of a case caption are undone by structure."""
from __future__ import annotations

import json
import sqlite3

import pytest

from cyber_data_collector.dedup import entity_alias_verification as eav
from cyber_data_collector.dedup import entity_merge_ledger as ledger
from cyber_data_collector.dedup.entity_merge import is_organisation_list

CAPTION = "Australian Securities and Investments Commission v Fortnum Private Wealth Ltd [2026] NSWSC 1252"


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "not-a-real-key")


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT);
        CREATE TABLE EntityAliases (raw_name TEXT UNIQUE, canonical_name TEXT, canonical_key TEXT, source TEXT,
                                    confidence REAL, created_at TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, title TEXT);
        CREATE TABLE EnrichedEventEntities (enriched_event_id TEXT, entity_id INTEGER);
    """)
    eav.ensure_schema(c)
    ledger.ensure_schema(c)
    for i, (kept, dropped) in enumerate([("Fortnum Private Wealth", "Australian Securities and Investments Commission"),
                                         ("Fortnum Private Wealth", CAPTION)]):
        c.execute("INSERT INTO EntityMerges (merge_id, kept_entity_id, kept_name, dropped_entity_id, dropped_name, "
                  "source, entity_row_json, kept_before_json, links_json, merged_at) "
                  "VALUES (?, 1, ?, ?, ?, 'llm', '{}', '{}', '[]', '2026-10-30')", (f"em-{i}", kept, i + 2, dropped))
    return c


def test_a_case_caption_is_a_list_of_parties():
    assert is_organisation_list(CAPTION) and is_organisation_list("ASIC v. AGM Markets")
    assert not is_organisation_list("Fortnum Private Wealth")


def test_a_fold_of_a_caption_is_overturned_without_asking(conn):
    overturned = {e["merge_id"]: e["verdict"] for e in ledger.overturned_merges(conn)}
    assert overturned == {"em-1": "organisation list"}


def test_a_chained_fold_is_asked_and_undone_when_not_the_same(conn):
    asked = []

    def search(prompt):
        asked.append(prompt)
        return {"text": json.dumps({"verdict": "different", "reason": "ASIC is the regulator suing Fortnum"}),
                "citations": []}

    result = eav.verify_unverified_folds(conn, search=search)
    assert result == [("Fortnum Private Wealth", "Australian Securities and Investments Commission", "different")]
    assert len(asked) == 1                                         # the caption fold is not asked about
    assert {e["merge_id"] for e in ledger.overturned_merges(conn)} == {"em-0", "em-1"}
    assert eav.verify_unverified_folds(conn, search=search) == [] and len(asked) == 1   # asked once
