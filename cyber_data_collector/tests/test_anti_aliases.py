"""Anti-aliases: similar names listed as different entities, honoured by every path that joins names."""

from __future__ import annotations

import json
import sqlite3

import pytest

from cyber_data_collector.dedup import anti_aliases as aa
from cyber_data_collector.dedup import entity_alias_verification as eav
from cyber_data_collector.dedup.entity_resolution import EntityResolver, stored_aliases
from cyber_data_collector.dedup.victim_counting import organisation_groups
from cyber_data_collector.utils.llm_models import perplexity_model


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    # A test that reaches for a model must fail, not make a paid call.
    monkeypatch.setenv("PERPLEXITY_API_KEY", "not-a-real-key")
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")


@pytest.fixture
def conn():
    """Two victims, 'Optus' and 'Optus Stadium', each of its own active event."""
    from cyber_data_collector.dedup.entity_profiles import ensure_schema as profiles_schema

    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY AUTOINCREMENT, entity_name TEXT UNIQUE);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, status TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, title TEXT);
        CREATE TABLE EnrichedEventEntities (enriched_event_id TEXT, entity_id INTEGER);
        CREATE TABLE EntityAliases (raw_name TEXT UNIQUE, canonical_name TEXT, canonical_key TEXT,
                                    source TEXT, confidence REAL, created_at TEXT);
    """)
    eav.ensure_schema(c)
    profiles_schema(c)
    for i, name in enumerate(["Optus", "Optus Stadium"], 1):
        c.execute("INSERT INTO EntitiesV2 (entity_name) VALUES (?)", (name,))
        c.execute("INSERT INTO DeduplicatedEvents VALUES (?, 'Active')", (f"e{i}",))
        c.execute("INSERT INTO DeduplicatedEventEntities VALUES (?, ?, 'victim')", (f"e{i}", i))
    c.commit()
    return c


def _joined(c) -> bool:
    groups = organisation_groups(c, [1, 2])
    return groups[1] == groups[2]


def _verdict(c, a, b, verdict, model=None):
    key = tuple(sorted((a, b)))
    c.execute("INSERT OR REPLACE INTO EntityAliasDecisions (name, other_name, verdict, reason, model, "
              "prompt_version, decided_at) VALUES (?, ?, ?, 'r', ?, ?, '2026-10-30T00:00:00+00:00')",
              (*key, verdict, model or perplexity_model(), eav.PROMPT_VERSION))


def test_a_listed_pair_is_never_one_victim(conn):
    assert _joined(conn)                                   # name shape alone joins them
    aa.add(conn, "Optus", "optus stadium", "a stadium named for its sponsor")
    assert not _joined(conn)                               # matched case- and punctuation-insensitively


def test_a_persons_entry_needs_a_reason_and_outranks_a_models(conn):
    with pytest.raises(aa.AntiAliasError):
        aa.add(conn, "Optus", "Optus Stadium", "")
    assert aa.add(conn, "Optus", "Optus Stadium", "venue", source=aa.SOURCE_HUMAN)
    assert not aa.add(conn, "Optus", "Optus Stadium", "model says related", relation="related", source=aa.SOURCE_LLM)
    assert aa.entries(conn)[0]["source"] == "human"


def test_a_reviewed_alias_refuses_the_entry(conn):
    conn.execute("INSERT INTO EntityAliases VALUES ('Optus Stadium', 'Optus', 'optus', 'human', 1, NULL)")
    with pytest.raises(aa.AntiAliasError, match="reviewed alias"):
        aa.add(conn, "Optus", "Optus Stadium", "venue")
    with pytest.raises(aa.AntiAliasError):
        aa.add(conn, "Optus", "OPTUS", "same name")


def test_verdicts_are_listed_and_a_later_same_verdict_removes_the_models_entry(conn):
    _verdict(conn, "Optus", "Optus Stadium", "related")
    assert aa.sync_from_verdicts(conn).added == 1 and aa.entries(conn)[0]["relation"] == "related"
    _verdict(conn, "Optus", "Optus Stadium", "same")
    assert aa.sync_from_verdicts(conn).removed == 1 and aa.entries(conn) == []


def test_learning_asks_about_the_pairs_counting_joins_and_lists_the_different_ones(conn):
    asked = []

    def search(prompt):
        asked.append(prompt)
        return {"text": json.dumps({"verdict": "related", "reason": "naming-rights sponsor"}), "citations": ["c"]}

    assert aa.counting_joins(conn) == [("Optus", "Optus Stadium")]
    report = aa.learn_from_counting(conn, search=search)
    assert len(asked) == 1 and report.listed == [("Optus", "Optus Stadium", "related")]
    assert not _joined(conn) and aa.counting_joins(conn) == []
    aa.learn_from_counting(conn, search=search)
    assert len(asked) == 1                                 # asked once per pair


def test_an_undecided_pair_is_reported_and_left_joined(conn):
    report = aa.learn_from_counting(conn, search=lambda prompt: None)
    assert report.failed == [("Optus", "Optus Stadium")] and _joined(conn)


def test_a_model_alias_for_a_listed_pair_no_longer_applies(conn):
    conn.execute("INSERT INTO EntityAliases VALUES ('Optus Stadium', 'Optus', 'optus', 'llm', 1, NULL)")
    assert stored_aliases(conn) == {"Optus Stadium": "Optus"}
    assert EntityResolver(conn).key_for("Optus Stadium") == "optus"
    aa.add(conn, "Optus", "Optus Stadium", "venue")
    assert stored_aliases(conn) == {}
    assert EntityResolver(conn).key_for("Optus Stadium") != "optus"


def test_a_listed_pair_is_never_folded_and_never_asked(conn):
    aa.add(conn, "Optus", "Optus Stadium", "venue")

    def search(prompt):
        raise AssertionError("an anti-aliased pair must not be asked about")

    report = eav.VerificationReport()
    assert eav.verified_groups(conn, [("Optus", ["Optus Stadium"])], search=search, report=report) == []
    assert report.rejected == [("Optus Stadium", "Optus", "anti-alias")]


def test_a_database_without_the_table_has_no_anti_aliases():
    c = sqlite3.connect(":memory:")
    assert aa.pair_set(c) == set() and aa.entries(c) == [] and not aa.remove(c, "a", "b")
