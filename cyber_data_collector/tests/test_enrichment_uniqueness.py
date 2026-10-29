"""A stored record has at most one active enrichment, however many workers create one.

Regression for the reviewer finding: ``create_enriched_event`` looked for an
active enrichment, released its lock, and took it again to insert, so two
workers (threads, or processes with their own connection) could both pass the
check and both insert.

Everything runs on a temporary SQLite file; no service is called (the autouse
fixture sets bogus keys so a call that reached one would fail).
"""
from __future__ import annotations

import importlib.util
import logging
import sqlite3
import sys
import threading
from pathlib import Path
from typing import List

import pytest

from cyber_data_collector.processing import record_identity
from cyber_data_collector.storage import enrichment_uniqueness as uniq
from cyber_data_collector.storage.cyber_event_data_v2 import CyberEventDataV2
from cyber_data_collector.storage.schema_init import initialize_database

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def bogus_keys(monkeypatch):
    for name in ("OPENAI_API_KEY", "PERPLEXITY_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.setenv(name, "bogus-not-a-key")


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "instance" / "t.db"
    initialize_database(str(path))
    return path


def _raw(db: CyberEventDataV2, n: int = 1) -> List[str]:
    return [db.add_raw_event("Perplexity", {"title": f"Acme breach {i}", "description": "d",
                                            "source_url": f"https://example.com/a{i}"}) for i in range(n)]


def _data(title="Acme Logistics data breach", **extra):
    return {"title": title, "is_australian_event": True, "is_specific_event": True, "status": "Active", **extra}


def _active(path, raw_id):
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute(
            "SELECT enriched_event_id FROM EnrichedEvents WHERE raw_event_id = ? AND status = 'Active'", (raw_id,))]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Creation is atomic
# ---------------------------------------------------------------------------

def test_second_creation_returns_the_first_enrichment(db_path):
    db = CyberEventDataV2(db_path)
    [raw] = _raw(db)
    first = db.create_enriched_event(raw, _data())
    assert db.create_enriched_event(raw, _data("A different title")) == first
    assert _active(db_path, raw) == [first]
    db.close()


@pytest.mark.parametrize("workers", [2, 8])
def test_concurrent_workers_create_one_active_enrichment(db_path, workers):
    """Several threads, each with its own connection, create for the same records at once."""
    setup = CyberEventDataV2(db_path)
    raws = _raw(setup, 15)
    setup.close()
    dbs = [CyberEventDataV2(db_path) for _ in range(workers)]
    results = {raw: [] for raw in raws}
    errors: List[BaseException] = []
    lock = threading.Lock()

    def work(db, index):
        try:
            for raw in raws:
                barrier[raw].wait(timeout=30)
                eid = db.create_enriched_event(raw, _data(f"Acme breach by worker {index}"))
                with lock:
                    results[raw].append(eid)
        except BaseException as exc:  # reported below, never swallowed
            with lock:
                errors.append(exc)

    barrier = {raw: threading.Barrier(workers) for raw in raws}
    threads = [threading.Thread(target=work, args=(db, i)) for i, db in enumerate(dbs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    for db in dbs:
        db.close()

    assert not errors, errors
    for raw in raws:
        active = _active(db_path, raw)
        assert len(active) == 1, f"{raw} holds {len(active)} active enrichments"
        assert set(results[raw]) == set(active), "every worker is told which enrichment won"
        assert len(results[raw]) == workers


def test_threads_sharing_one_connection_create_one(db_path):
    db = CyberEventDataV2(db_path)
    [raw] = _raw(db)
    barrier = threading.Barrier(6)
    ids: List[str] = []

    def work():
        barrier.wait(timeout=30)
        ids.append(db.create_enriched_event(raw, _data()))

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(set(ids)) == 1 and _active(db_path, raw) == [ids[0]]
    db.close()


def test_inactive_history_does_not_block_a_new_active_enrichment(db_path):
    db = CyberEventDataV2(db_path)
    [raw] = _raw(db)
    old = db.create_enriched_event(raw, _data())
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE EnrichedEvents SET status = 'Inactive' WHERE enriched_event_id = ?", (old,))
    conn.commit()
    conn.close()
    new = db.create_enriched_event(raw, _data("Re-enriched"))
    assert new != old and _active(db_path, raw) == [new]
    db.close()


# ---------------------------------------------------------------------------
# The database refuses what the code no longer does
# ---------------------------------------------------------------------------

def _insert(conn, raw_id, eid, status="Active"):
    conn.execute(
        "INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, is_australian_event, is_specific_event, "
        "status) VALUES (?, ?, 'T', 1, 1, ?)", (eid, raw_id, status))


def test_database_refuses_a_second_active_insert(db_path):
    db = CyberEventDataV2(db_path)
    [raw] = _raw(db)
    db.close()
    conn = sqlite3.connect(db_path)
    _insert(conn, raw, "e1")
    with pytest.raises(sqlite3.IntegrityError, match=uniq.ABORT_MESSAGE):
        _insert(conn, raw, "e2")
    _insert(conn, raw, "e3", status="Inactive")      # history is not an active enrichment
    conn.commit()
    conn.close()


def test_record_identity_reattachment_may_still_leave_two(db_path):
    """The repair moves an enrichment with an UPDATE and reports a record that keeps a wrong one.

    This is why the guard is on INSERT and not a unique index on (raw_event_id) WHERE status = 'Active':
    such an index would make this repair impossible.
    """
    db = CyberEventDataV2(db_path)
    raw_a, raw_b = _raw(db, 2)
    db.close()
    conn = sqlite3.connect(db_path)
    _insert(conn, raw_a, "wrong-on-a")
    _insert(conn, raw_b, "belongs-to-a")
    conn.execute("PRAGMA foreign_keys = ON")
    record_identity.ensure_schema(conn)
    conn.commit()
    record_identity.repoint(conn, "belongs-to-a", raw_a)          # no IntegrityError
    conn.commit()
    holders = conn.execute("SELECT COUNT(*) FROM EnrichedEvents WHERE raw_event_id = ? AND status = 'Active'",
                           (raw_a,)).fetchone()[0]
    assert holders == 2
    conn.close()


# ---------------------------------------------------------------------------
# Existing duplicates: reported, never silently removed
# ---------------------------------------------------------------------------

def _legacy_duplicate(db_path):
    """A database written before the guard existed, holding two active enrichments for one record."""
    db = CyberEventDataV2(db_path)
    [raw] = _raw(db)
    db.close()
    conn = sqlite3.connect(db_path)
    conn.execute(f"DROP TRIGGER {uniq.TRIGGER_NAME}")
    _insert(conn, raw, "dup-1")
    _insert(conn, raw, "dup-2")
    conn.commit()
    conn.close()
    return raw


def test_existing_duplicates_are_reported_and_left_alone(db_path, caplog):
    raw = _legacy_duplicate(db_path)
    conn = sqlite3.connect(db_path)
    with caplog.at_level(logging.ERROR):
        uniq.ensure_schema(conn)
    conn.commit()
    assert any("more than one active enrichment" in r.message for r in caplog.records)
    assert sorted(_active(db_path, raw)) == ["dup-1", "dup-2"], "nothing is deleted or deactivated"
    [found] = uniq.duplicate_active_enrichments(conn)
    assert found["raw_event_id"] == raw and sorted(found["enriched_event_ids"]) == ["dup-1", "dup-2"]
    assert found["explained"] is False
    # ...and the guard is installed, so no third can be created.
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, raw, "dup-3")
    conn.close()


def test_ensure_schema_is_idempotent(db_path):
    conn = sqlite3.connect(db_path)
    uniq.ensure_schema(conn)
    uniq.ensure_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                        (uniq.TRIGGER_NAME,)).fetchone()[0] == 1
    conn.close()


def test_init_reports_but_does_not_fail_on_existing_duplicates(db_path):
    _legacy_duplicate(db_path)
    initialize_database(str(db_path))          # must not raise or delete
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM EnrichedEvents WHERE status = 'Active'").fetchone()[0] == 2
    conn.close()


def _integrity_module():
    spec = importlib.util.spec_from_file_location("_check_data_integrity_under_test",
                                                  ROOT / "scripts" / "check_data_integrity.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_integrity_check_fails_on_unexplained_duplicates_and_passes_when_the_repair_explains_them(db_path):
    raw = _legacy_duplicate(db_path)
    check = _integrity_module().check_active_enrichment_unique_per_record
    conn = sqlite3.connect(db_path)
    result = check(conn)
    assert not result.ok and "1 record(s)" in result.summary and raw in result.details[0]

    # A record-identity finding that names one of the two explains it (the repair left it on purpose).
    record_identity.ensure_schema(conn)
    conn.execute("INSERT INTO RecordIdentityChecks (raw_event_id, enriched_event_id, stage, verdict, kind, "
                 "checked_at) VALUES (?, 'dup-1', 'audit', 'mismatch', ?, '2026-10-29T00:00:00')",
                 (raw, record_identity.KIND_ENRICHMENT))
    conn.commit()
    result = check(conn)
    assert result.ok and "left on purpose" in result.summary
    conn.close()


def test_integrity_check_passes_on_a_clean_database(db_path):
    db = CyberEventDataV2(db_path)
    [raw] = _raw(db)
    db.create_enriched_event(raw, _data())
    db.close()
    conn = sqlite3.connect(db_path)
    assert _integrity_module().check_active_enrichment_unique_per_record(conn).ok
    conn.close()
