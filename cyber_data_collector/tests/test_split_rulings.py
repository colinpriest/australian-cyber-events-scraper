"""Rulings cached between two strays that left one event in separate splits are void."""
from __future__ import annotations

import sqlite3

from cyber_data_collector.dedup import schema, split_rulings
from cyber_data_collector.dedup.backfill import run_backfill
from cyber_data_collector.dedup.ledger import DedupLedger
from cyber_data_collector.tests.test_dedup_v3 import BASE_DDL, _add_event


def _world():
    """Event ``big`` of four records; ``w1`` and ``w2`` (one other incident) leave it in separate splits."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(BASE_DDL)
    schema.migrate(conn)
    for key in ("big", "b2", "w1", "w2"):
        _add_event(conn, key, f"Optus {key}", f"https://x/{key}", victim="Optus", date="2022-09-20")
    run_backfill(conn)
    ledger = DedupLedger(conn)
    for key in ("b2", "w1", "w2"):
        ledger.merge_events("ded-big", f"ded-{key}", reason="t", respect_overrides=False)
    conn.commit()
    return conn, ledger


def _active(conn, a, b):
    return conn.execute("""SELECT COUNT(*) FROM DedupOverrides WHERE active = 1 AND verdict = 'different'
                           AND pair_key IN (?, ?)""", (f"{a}|{b}", f"{b}|{a}")).fetchone()[0]


def test_strays_split_one_at_a_time_are_not_barred_from_each_other():
    conn, ledger = _world()
    ledger.split_members("ded-big", ["enr-w1"], reason="[coherence] stray", actor="rule")
    ledger.split_members("ded-big", ["enr-w2"], reason="[coherence] stray", actor="rule")
    conn.commit()
    assert _active(conn, "enr-w1", "enr-w2") == 1                   # the artefact
    found = split_rulings.void_orphaned(conn)
    assert [(o.left, o.right) for o in found] in ([("enr-w2", "enr-w1")], [("enr-w1", "enr-w2")])
    assert _active(conn, "enr-w1", "enr-w2") == 0
    assert _active(conn, "enr-w1", "enr-big") == 1 and _active(conn, "enr-w2", "enr-b2") == 1   # against what stays
    assert split_rulings.void_orphaned(conn) == []                   # idempotent
    DedupLedger(conn).merge_events(
        conn.execute("SELECT deduplicated_event_id FROM EventDeduplicationMap WHERE enriched_event_id = 'enr-w1' "
                     "AND deduplicated_event_id != 'ded-big' ORDER BY created_at DESC").fetchone()[0],
        conn.execute("SELECT deduplicated_event_id FROM EventDeduplicationMap WHERE enriched_event_id = 'enr-w2' "
                     "AND deduplicated_event_id != 'ded-big' ORDER BY created_at DESC").fetchone()[0],
        reason="duplicate check 0.98", actor="rule")                # no longer refused


def test_a_ruling_against_a_record_that_stayed_is_kept():
    conn, ledger = _world()
    ledger.split_members("ded-big", ["enr-w1"], reason="[coherence] stray", actor="rule")
    conn.commit()
    assert split_rulings.orphaned(conn) == []


def test_a_persons_ruling_is_never_voided():
    conn, ledger = _world()
    ledger.split_members("ded-big", ["enr-w1"], reason="checked", actor="human")
    ledger.split_members("ded-big", ["enr-w2"], reason="checked", actor="human")
    conn.commit()
    assert split_rulings.orphaned(conn) == [] and _active(conn, "enr-w1", "enr-w2") == 1
