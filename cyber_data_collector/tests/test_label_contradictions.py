"""Pairs labelled different must not sit in one event: merges refuse them, and ones already merged are
split apart again through the ledger (reversible, with 'different' rulings cached)."""
from __future__ import annotations

import sqlite3

import pytest

from cyber_data_collector.dedup import label_contradictions as lc
from cyber_data_collector.dedup import schema
from cyber_data_collector.dedup.ledger import DedupLedger
from cyber_data_collector.dedup.models import OverrideVerdict
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


# --- labelled-same pairs still apart, and labelled events that were split ----------------------------------------


@pytest.fixture
def apart(tmp_path, monkeypatch):
    """``a`` (two records) and ``b`` (one) labelled the same incident but stored as two events."""
    conn = _db()
    for key, title in (("a", "Acme ransomware"), ("a2", "Acme confirms ransomware"), ("b", "Mining cyber article")):
        _add_event(conn, key, title, f"https://x/{key}", victim="Acme Ltd", date="2024-03-01")
    from cyber_data_collector.dedup.backfill import run_backfill
    run_backfill(conn)
    DedupLedger(conn).merge_events("ded-a", "ded-a2", reason="same incident", respect_overrides=False)
    conn.commit()
    snapshot = tmp_path / "labelling.db"
    target = sqlite3.connect(snapshot)
    conn.backup(target)
    target.close()
    labels = _labels(tmp_path, [("ded-a", "ded-b")], label="same")
    monkeypatch.setattr(lc, "LABELS_PATH", labels)
    monkeypatch.setattr(lc, "MEMBERS_PATH", tmp_path / "duplicate_label_members.csv")
    return conn, snapshot, labels


def _event_of(conn, record):
    return conn.execute("SELECT deduplicated_event_id FROM EventDeduplicationMap WHERE enriched_event_id = ?",
                        (record,)).fetchone()[0]


def test_a_labelled_same_pair_still_apart_is_merged_into_the_larger_event(apart):
    conn, snapshot, labels = apart
    assert [c.pair_id for c in lc.find_same(conn)] == ["ded-a|ded-b"]
    assert regression.check_duplicates(conn, regression.EventResolver(conn), labels)[
        "labels.duplicates.contradictions"].value == 1
    plan = lc.resolve(conn, snapshot, apply=False)
    assert plan[0].kind == "same" and plan[0].event == "ded-a" and plan[0].source == "ded-b"
    assert len(lc.find_same(conn)) == 1                      # a plan changes nothing
    lc.resolve(conn, snapshot, apply=True)
    assert lc.find_same(conn) == [] and _event_of(conn, "enr-b") == "ded-a"
    assert conn.execute("SELECT status FROM DeduplicatedEvents WHERE deduplicated_event_id = 'ded-b'").fetchone()[0] == "Merged"
    assert lc.resolve(conn, snapshot, apply=True) == []      # idempotent


def test_an_event_holding_unlabelled_records_is_not_merged(apart):
    conn, snapshot, _ = apart
    _add_event(conn, "c", "Another mining article", "https://x/c", victim="Acme Ltd", date="2025-01-01")
    from cyber_data_collector.dedup.backfill import run_backfill
    run_backfill(conn)
    DedupLedger(conn).merge_events("ded-b", "ded-c", reason="x", respect_overrides=False)
    result = lc.resolve(conn, snapshot, apply=True)
    assert result[0].unresolved and "neither labelled event" in result[0].unresolved
    assert _event_of(conn, "enr-b") == "ded-b"


def test_a_ruling_against_the_merge_is_reported_not_forced(apart):
    conn, snapshot, _ = apart
    DedupLedger(conn).add_override("enr-a", "enr-b", OverrideVerdict.DIFFERENT, reason="checked")
    conn.commit()
    result = lc.resolve(conn, snapshot, apply=True)
    assert result[0].unresolved and "ruling blocks" in result[0].unresolved and _event_of(conn, "enr-b") == "ded-b"


def test_a_page_the_source_gate_ruled_out_leaves_and_the_event_merges(apart):
    """The source gate split ``b``'s page out of ``a`` (no incident report); the label says ``b`` is ``a``'s
    incident. The page goes, as the gate ruled, and the event joins: no split-merge loop."""
    conn, snapshot, labels = apart
    conn.execute("ALTER TABLE EnrichedEvents ADD COLUMN status TEXT DEFAULT 'Active'")
    DedupLedger(conn).add_override("enr-b", "enr-a", OverrideVerdict.DIFFERENT,
                                   reason="split: source-gate-v1: page does not report an incident",
                                   created_by="rule")
    conn.commit()
    result = lc.resolve(conn, snapshot, apply=True)
    assert result[0].unresolved is None and result[0].leaving == ["enr-b"]
    assert conn.execute("SELECT status FROM EnrichedEvents WHERE enriched_event_id = 'enr-b'").fetchone()[0] == "Inactive"
    assert conn.execute("SELECT status FROM DeduplicatedEvents WHERE deduplicated_event_id = 'ded-b'").fetchone()[0] == "Merged"
    assert regression.check_duplicates(conn, regression.EventResolver(conn), labels)[
        "labels.duplicates.contradictions"].value == 0
    assert lc.resolve(conn, snapshot, apply=True) == []


def test_a_split_labelled_event_resolves_to_where_most_of_its_records_went(tmp_path, monkeypatch):
    """Labelled event ``x`` (three records) lost its lead record to a split; the other two now sit with ``a``,
    so the label "x is a" holds, though following the lead record says the two are still apart."""
    conn = _db()
    for key, title in (("a", "Optus 2022 breach"), ("x", "White Pages finding"), ("x2", "Optus class action"),
                       ("x3", "Optus hacked")):
        _add_event(conn, key, title, f"https://x/{key}", victim="Optus", date="2022-09-20")
    from cyber_data_collector.dedup.backfill import run_backfill
    run_backfill(conn)
    ledger = DedupLedger(conn)
    for key in ("x2", "x3"):
        ledger.merge_events("ded-x", f"ded-{key}", reason="t", respect_overrides=False)
    conn.commit()
    snapshot = tmp_path / "labelling.db"
    target = sqlite3.connect(snapshot)
    conn.backup(target)
    target.close()
    labels = _labels(tmp_path, [("ded-a", "ded-x")], label="same")
    monkeypatch.setattr(lc, "LABELS_PATH", labels)
    monkeypatch.setattr(lc, "MEMBERS_PATH", tmp_path / "duplicate_label_members.csv")
    lc.record_members(snapshot)
    new = ledger.split_members("ded-x", ["enr-x2", "enr-x3"], reason="t", actor="rule")
    ledger.merge_events("ded-a", new, reason="t", respect_overrides=False)
    conn.commit()
    # Followed by its lead record, x is still its own Active event apart from a.
    assert regression.EventResolver(conn).resolve("ded-x") == ("ded-x", "Active")
    assert lc.SideResolver(conn).resolve("ded-x") == ("ded-a", "Active")
    assert regression.check_duplicates(conn, regression.EventResolver(conn), labels)[
        "labels.duplicates.contradictions"].value == 0
    assert lc.find_same(conn) == []


def test_without_a_majority_a_labelled_event_is_ambiguous(apart):
    conn, snapshot, _ = apart
    lc.record_members(snapshot)                              # a: {enr-a, enr-a2}
    DedupLedger(conn).split_members("ded-a", ["enr-a2"], reason="t", actor="rule")
    conn.commit()
    assert lc.SideResolver(conn).resolve("ded-a") == (None, "ambiguous")
    assert lc.find_same(conn) == []                          # not checked rather than guessed


def test_record_members_writes_every_labelled_event(apart):
    conn, snapshot, _ = apart
    assert lc.record_members(snapshot) == 3
    assert lc.labelled_members() == {"ded-a": {"enr-a", "enr-a2"}, "ded-b": {"enr-b"}}
