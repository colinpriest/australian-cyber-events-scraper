"""Pairs labelled different must not sit in one event: merges refuse them, and ones already merged are
split apart again through the ledger (reversible, with 'different' rulings cached)."""
from __future__ import annotations

import sqlite3

import pytest

from cyber_data_collector.dedup import label_contradictions as lc
from cyber_data_collector.dedup import schema
from cyber_data_collector.dedup.ledger import DedupLedger
from cyber_data_collector.quality import regression
from cyber_data_collector.tests.test_dedup_v3 import BASE_DDL, _add_event


def _db(path=None):
    conn = sqlite3.connect(path or ":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(BASE_DDL)
    schema.migrate(conn)
    return conn


def _labels(tmp_path, pairs, label="different"):
    path = tmp_path / "duplicate_labels.csv"
    path.write_text("pair_id,label,rationale,corroboration,labeller\n"
                    + "".join(f"{a}|{b},{label},why,,t\n" for a, b in pairs), encoding="utf-8")
    return path


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Two labelled-different events at one organisation; ``a`` holds two records, ``b`` one."""
    conn = _db()
    for key, title, date in (("a", "Acme breach (2024)", "2024-08-14"), ("a2", "Acme breach again", "2024-08-14"),
                             ("b", "Acme breach (2025)", "2025-01-28")):
        _add_event(conn, key, title, f"https://x/{key}", victim="Acme Ltd", date=date)
    from cyber_data_collector.dedup.backfill import run_backfill
    run_backfill(conn)
    ledger = DedupLedger(conn)
    ledger.merge_events("ded-a", "ded-a2", reason="same incident", respect_overrides=False)
    conn.commit()
    snapshot = tmp_path / "labelling.db"
    target = sqlite3.connect(snapshot)
    conn.backup(target)
    target.close()
    labels = _labels(tmp_path, [("ded-a", "ded-b")])
    monkeypatch.setattr(lc, "LABELS_PATH", labels)
    return conn, snapshot, labels


def _merge_anyway(conn):
    DedupLedger(conn).merge_events("ded-a", "ded-b", reason="recurrence 0.98", respect_overrides=False)
    conn.commit()


def test_a_merge_of_a_labelled_different_pair_is_refused(world):
    conn, _, _ = world
    with pytest.raises(ValueError, match="blocked by a label"):
        DedupLedger(conn).merge_events("ded-a", "ded-b", reason="recurrence 0.98")
    assert conn.execute("SELECT status FROM DeduplicatedEvents WHERE deduplicated_event_id = 'ded-b'").fetchone()[0] == "Active"


def test_a_human_can_overrule_a_label_with_force(world):
    conn, _, _ = world
    DedupLedger(conn).merge_events("ded-a", "ded-b", reason="I checked", respect_overrides=False)
    assert conn.execute("SELECT status FROM DeduplicatedEvents WHERE deduplicated_event_id = 'ded-b'").fetchone()[0] == "Merged"


def test_the_refusal_follows_merges_already_made(world):
    """b is merged into c elsewhere; joining c and a is still joining the labelled pair."""
    conn, _, _ = world
    _add_event(conn, "c", "Acme third report", "https://x/c", victim="Acme Ltd", date="2025-02-01")
    from cyber_data_collector.dedup.backfill import run_backfill
    run_backfill(conn)
    DedupLedger(conn).merge_events("ded-c", "ded-b", reason="same", respect_overrides=False)
    with pytest.raises(ValueError, match="blocked by a label"):
        DedupLedger(conn).merge_events("ded-a", "ded-c", reason="recurrence")


def test_unrelated_merges_are_untouched(world):
    conn, _, _ = world
    _add_event(conn, "z", "Zeta breach", "https://x/z", victim="Zeta Ltd")
    _add_event(conn, "y", "Zeta breach again", "https://x/y", victim="Zeta Ltd")
    DedupLedger(conn).merge_events("ded-z", "ded-y", reason="same")


def test_unreadable_labels_stop_the_merge(world, tmp_path, monkeypatch):
    conn, _, _ = world
    monkeypatch.setattr(lc, "LABELS_PATH", tmp_path / "gone.csv")
    with pytest.raises(lc.LabelsUnreadable):
        DedupLedger(conn).merge_events("ded-a", "ded-b", reason="x")


def test_a_merged_contradiction_is_found_the_way_the_quality_gate_finds_it(world, tmp_path):
    conn, _, labels = world
    _merge_anyway(conn)
    assert [c.pair_id for c in lc.find(conn)] == ["ded-a|ded-b"]
    measured = regression.check_duplicates(conn, regression.EventResolver(conn), labels)
    assert measured["labels.duplicates.contradictions"].value == 1


def test_the_smaller_labelled_side_is_split_out_through_the_ledger(world):
    conn, snapshot, labels = world
    _merge_anyway(conn)
    plan = lc.resolve(conn, snapshot, apply=False)
    assert plan[0].leaving == ["enr-b"] and plan[0].side_a == ["enr-a", "enr-a2"] and plan[0].new_event is None
    assert len(lc.find(conn)) == 1                   # a plan changes nothing

    done = lc.resolve(conn, snapshot, apply=True)
    assert lc.find(conn) == []
    assert done[0].new_event and done[0].new_event != "ded-a"
    members = {r[0]: r[1] for r in conn.execute("SELECT enriched_event_id, deduplicated_event_id FROM EventDeduplicationMap")}
    assert members["enr-b"] == done[0].new_event and members["enr-a"] == members["enr-a2"] == "ded-a"
    assert regression.check_duplicates(conn, regression.EventResolver(conn), labels)[
        "labels.duplicates.contradictions"].value == 0


def test_the_split_is_reversible_and_cached_against_a_remerge(world):
    conn, snapshot, _ = world
    _merge_anyway(conn)
    new_event = lc.resolve(conn, snapshot, apply=True)[0].new_event
    assert conn.execute("SELECT COUNT(*) FROM DedupSnapshots WHERE batch_id LIKE 'revert-%'").fetchone()[0] >= 1
    assert conn.execute("SELECT COUNT(*) FROM DedupOverrides WHERE verdict = 'different' AND active = 1").fetchone()[0] >= 2
    with pytest.raises(ValueError, match="blocked by a 'different' override"):
        DedupLedger(conn).merge_events("ded-a", new_event, reason="again")


def test_resolving_twice_is_a_no_op(world):
    conn, snapshot, _ = world
    _merge_anyway(conn)
    lc.resolve(conn, snapshot, apply=True)
    assert lc.resolve(conn, snapshot, apply=True) == []


def test_without_the_labelling_snapshot_the_contradiction_is_listed_not_guessed(world):
    conn, _, _ = world
    _merge_anyway(conn)
    result = lc.resolve(conn, None, apply=True)
    assert result[0].unresolved and "snapshot" in result[0].unresolved and len(lc.find(conn)) == 1


def test_a_side_with_no_active_record_left_is_unresolved(world):
    conn, snapshot, _ = world
    _merge_anyway(conn)
    conn.execute("ALTER TABLE EnrichedEvents ADD COLUMN status TEXT")
    conn.execute("UPDATE EnrichedEvents SET status = 'Inactive' WHERE enriched_event_id = 'enr-b'")
    result = lc.resolve(conn, snapshot, apply=True)
    assert result[0].unresolved and "no active record" in result[0].unresolved
    assert len(lc.find(conn)) == 1


def test_labels_other_than_different_are_ignored(world, tmp_path, monkeypatch):
    conn, snapshot, _ = world
    monkeypatch.setattr(lc, "LABELS_PATH", _labels(tmp_path, [("ded-a", "ded-b")], label="same"))
    _merge_anyway(conn)
    assert lc.find(conn) == []
