"""Tests for the non-incident gate and the manual merge command."""
from __future__ import annotations

import json
import sqlite3

import pytest

from cyber_data_collector.dedup import schema
from cyber_data_collector.dedup.non_incident import (
    deactivate_unmapped_non_incidents,
    find_non_incident_events,
    is_non_incident,
    reject_non_incident_events,
)


def _pe(confidence, entity):
    return json.dumps({"overall_confidence": confidence, "formal_entity_name": entity})


@pytest.mark.parametrize("is_au, pe, expected", [
    # The Insight marketing page: not Australian, Perplexity found nothing.
    (0, _pe("0.0", "None"), True),
    (0, _pe(0, ""), True),
    # Real incidents that fail only one half of the gate must be kept.
    (0, _pe("0.9", "NSW Health"), False),          # missed Australian flag
    (1, _pe("0.0", "None"), False),                 # thin reporting (FIIG)
    (0, _pe("0.0", "Canva Pty Ltd"), False),        # named victim
    # No evidence is not evidence of junk.
    (None, _pe("0.0", "None"), False),
    (0, None, False),
    (0, "not json", False),
])
def test_is_non_incident_requires_both_signals(is_au, pe, expected):
    assert is_non_incident(is_au, pe) is expected


_JUNK = _pe("0.0", "None")
_REAL = _pe("0.9", "Acme Pty Ltd")


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, title TEXT,
            is_australian_event INTEGER, perplexity_enrichment_data TEXT,
            status TEXT, updated_at TEXT);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY,
            master_enriched_event_id TEXT, title TEXT, summary TEXT, event_date TEXT,
            status TEXT, updated_at TEXT, records_affected INTEGER,
            victim_organization_name TEXT, has_human_override INTEGER DEFAULT 0,
            total_data_sources INTEGER);
        CREATE TABLE EventDeduplicationMap (map_id TEXT PRIMARY KEY, raw_event_id TEXT,
            enriched_event_id TEXT, deduplicated_event_id TEXT, contribution_type TEXT);
    """)
    rows = [
        # junk-only event
        ("j1", 0, _JUNK, "d-junk"),
        # real event that absorbed one junk record - must survive
        ("r1", 1, _REAL, "d-mixed"), ("j2", 0, _JUNK, "d-mixed"),
        # plain real event
        ("r2", 1, _REAL, "d-real"),
    ]
    for eid, au, pe, did in rows:
        c.execute("INSERT INTO EnrichedEvents VALUES (?, ?, ?, ?, 'Active', NULL)", (eid, eid, au, pe))
        c.execute("INSERT OR IGNORE INTO DeduplicatedEvents (deduplicated_event_id, master_enriched_event_id, title, status) "
                  "VALUES (?, ?, ?, 'Active')", (did, eid, did))
        c.execute("INSERT INTO EventDeduplicationMap VALUES (?, NULL, ?, ?, 'merged')", (f"m-{eid}", eid, did))
    # unmapped records awaiting incremental dedup
    c.execute("INSERT INTO EnrichedEvents VALUES ('new-junk', 't', 0, ?, 'Active', NULL)", (_JUNK,))
    c.execute("INSERT INTO EnrichedEvents VALUES ('new-real', 't', 1, ?, 'Active', NULL)", (_REAL,))
    c.commit()
    yield c
    c.close()


def _status(conn, table, key, value):
    return conn.execute(f"SELECT status FROM {table} WHERE {key} = ?", (value,)).fetchone()[0]


def test_only_events_made_entirely_of_junk_are_found(conn):
    found = find_non_incident_events(conn)
    assert [e["deduplicated_event_id"] for e in found] == ["d-junk"]


def test_dry_run_rejects_nothing(conn):
    assert len(reject_non_incident_events(conn, dry_run=True)) == 1
    assert _status(conn, "DeduplicatedEvents", "deduplicated_event_id", "d-junk") == "Active"


def test_reject_sets_statuses_and_is_idempotent(conn, monkeypatch):
    from cyber_data_collector.dedup import ledger as ledger_mod
    snapshots = []
    monkeypatch.setattr(ledger_mod.DedupLedger, "snapshot_event",
                        lambda self, batch, did: snapshots.append(did))

    rejected = reject_non_incident_events(conn, dry_run=False)

    assert [e["deduplicated_event_id"] for e in rejected] == ["d-junk"]
    assert snapshots == ["d-junk"]
    assert _status(conn, "DeduplicatedEvents", "deduplicated_event_id", "d-junk") == "Rejected"
    assert _status(conn, "EnrichedEvents", "enriched_event_id", "j1") == "Inactive"
    assert _status(conn, "DeduplicatedEvents", "deduplicated_event_id", "d-mixed") == "Active"
    assert _status(conn, "EnrichedEvents", "enriched_event_id", "j2") == "Active"
    assert reject_non_incident_events(conn, dry_run=False) == []


def test_unmapped_junk_is_kept_out_of_dedup_without_touching_events(conn):
    assert deactivate_unmapped_non_incidents(conn, dry_run=True) == ["new-junk"]
    assert _status(conn, "EnrichedEvents", "enriched_event_id", "new-junk") == "Active"

    assert deactivate_unmapped_non_incidents(conn) == ["new-junk"]
    assert _status(conn, "EnrichedEvents", "enriched_event_id", "new-junk") == "Inactive"
    assert _status(conn, "EnrichedEvents", "enriched_event_id", "new-real") == "Active"
    # mapped junk inside an existing event is left alone
    assert _status(conn, "EnrichedEvents", "enriched_event_id", "j2") == "Active"
    assert deactivate_unmapped_non_incidents(conn) == []


def test_merge_command_refuses_inactive_event(tmp_path, capsys):
    from scripts import dedup_admin

    db = tmp_path / "m.db"
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY,
            master_enriched_event_id TEXT, status TEXT, title TEXT);
        INSERT INTO DeduplicatedEvents VALUES ('a', 'ea', 'Active', 'MediSecure breach');
        INSERT INTO DeduplicatedEvents VALUES ('b', 'eb', 'Merged', 'MediSecure incident');
    """)
    c.commit()
    c.close()

    rc = dedup_admin.main(["--db", str(db), "--no-refresh-roles", "merge", "a", "b",
                           "--reason", "same incident"])
    assert rc == 1
    assert "not Active" in capsys.readouterr().out
