"""Descriptions remembered in DescriptiveNames are never a victim and never an organisation."""
from __future__ import annotations

import sqlite3

import pytest

from cyber_data_collector.dedup import descriptive_names as dn
from cyber_data_collector.dedup import name_judgement as nj


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(nj, "CALIBRATION_PATH", tmp_path / "none.json")
    monkeypatch.setattr(nj, "LABELS_PATH", tmp_path / "none.csv")
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, status TEXT,
                                         victim_organization_name TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER,
                                                relationship_type TEXT);
        INSERT INTO EntitiesV2 VALUES (1, 'Australian university'), (2, 'Mackay Sugar');
        INSERT INTO DeduplicatedEvents VALUES ('e1', 'Active', 'Australian university'), ('e2', 'Active', 'Mackay Sugar');
        INSERT INTO DeduplicatedEventEntities VALUES ('e1', 1, 'victim'), ('e2', 2, 'victim');
    """)
    yield c
    nj.deactivate()


def test_a_persons_entry_needs_a_reason_and_is_matched_loosely(conn):
    with pytest.raises(dn.DescriptiveNameError):
        dn.add(conn, "Australian university", "")
    assert dn.add(conn, "Australian  University", "a description of any university")
    nj.activate(conn)
    assert nj.is_descriptive_name("australian university") and not nj.is_descriptive_name("Mackay Sugar")


def test_a_model_cannot_replace_a_persons_entry(conn):
    dn.add(conn, "Australian university", "owner")
    assert not dn.add(conn, "Australian university", "model", source=dn.SOURCE_LLM, probability=0.1)
    assert dn.entries(conn)[0]["source"] == "human"


def test_a_described_victim_is_demoted_and_audited(conn):
    dn.add(conn, "Australian university", "owner")
    nj.activate(conn)
    planned = dn.demote_victims(conn, apply=False).demoted
    assert [(d.deduplicated_event_id, d.scalar_cleared) for d in planned] == [("e1", True)]
    dn.demote_victims(conn, apply=True)
    assert conn.execute("SELECT relationship_type FROM DeduplicatedEventEntities WHERE deduplicated_event_id = 'e1'"
                        ).fetchone()[0] == "affected"
    assert conn.execute("SELECT victim_organization_name FROM DeduplicatedEvents WHERE deduplicated_event_id = 'e1'"
                        ).fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM DescriptiveVictimDemotions").fetchone()[0] == 1
    assert dn.demote_victims(conn, apply=True).demoted == []          # idempotent
    assert conn.execute("SELECT relationship_type FROM DeduplicatedEventEntities WHERE deduplicated_event_id = 'e2'"
                        ).fetchone()[0] == "victim"


def test_a_database_without_the_table_has_no_entries():
    c = sqlite3.connect(":memory:")
    assert dn.keys(c) == set() and dn.entries(c) == []
