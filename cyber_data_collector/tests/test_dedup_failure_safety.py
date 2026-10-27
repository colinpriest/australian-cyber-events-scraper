"""Failures in deduplication stop the run; they never read as "nothing there".

Reviewer findings 1-5: a failed query used to start a destructive full
rebuild, a failed load created duplicate incidents, an unreadable ruling
disabled split protection, a schema mismatch dropped lineage tables, and
invalid storage results were committed and reported as success.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest

from cyber_data_collector.processing.deduplication_v2 import (
    CyberEvent, DeduplicationResult, DeduplicationStats, ValidationError,
)
from cyber_data_collector.storage.deduplication_storage import DeduplicationStorage
from scripts.run_global_deduplication import DeduplicationMigration


def _migration(db_path, force=False):
    m = DeduplicationMigration.__new__(DeduplicationMigration)
    m.db_path = str(db_path)
    m.dry_run = False
    m.force = force
    m.migration_report = {"steps_completed": [], "errors": [], "statistics": {}}
    return m


def test_detection_failure_is_an_error_not_an_empty_database(tmp_path):
    db = tmp_path / "broken.db"
    sqlite3.connect(db).close()                        # no tables at all: every query fails
    with pytest.raises(RuntimeError, match="could not read existing deduplication state"):
        _migration(db)._detect_new_events()


def test_load_failure_is_an_error_not_an_empty_list(tmp_path):
    db = tmp_path / "broken.db"
    sqlite3.connect(db).close()
    with pytest.raises(RuntimeError, match="could not load existing incidents"):
        _migration(db)._load_existing_deduplicated_events()


def test_unreadable_rulings_stop_the_run(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE DedupOverrides (wrong_column TEXT)")   # exists but cannot be read as rulings
    from cyber_data_collector.processing.deduplication_v2 import DeduplicationEngine
    with pytest.raises(sqlite3.OperationalError):
        DeduplicationMigration._engine_with_rulings(conn, DeduplicationEngine())


def test_no_rulings_table_means_no_rulings():
    conn = sqlite3.connect(":memory:")
    from cyber_data_collector.processing.deduplication_v2 import DeduplicationEngine
    engine = DeduplicationMigration._engine_with_rulings(conn, DeduplicationEngine())
    assert engine.different_pairs == set()


def test_ledger_refuses_to_merge_when_rulings_are_unreadable():
    from cyber_data_collector.dedup.ledger import DedupLedger
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with pytest.raises(RuntimeError, match="split rulings"):
        DedupLedger.__new__(DedupLedger).__class__.override_conflict(SimpleNamespace(conn=conn), "a", "b")


def _storage_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, master_enriched_event_id TEXT,
            title TEXT, summary TEXT, event_date DATE, event_type TEXT, severity TEXT, records_affected INTEGER,
            victim_organization_name TEXT, victim_organization_industry TEXT, is_australian_event BOOLEAN,
            is_specific_event BOOLEAN, confidence_score REAL, status TEXT, created_at TEXT, updated_at TEXT,
            total_data_sources INTEGER);
        CREATE TABLE EventDeduplicationMap (map_id TEXT PRIMARY KEY, enriched_event_id TEXT,
            deduplicated_event_id TEXT NOT NULL, contribution_type TEXT, similarity_score REAL);
        INSERT INTO DeduplicatedEvents (deduplicated_event_id, master_enriched_event_id, title, status)
            VALUES ('d1', 'e1', 'Existing incident', 'Active');
        INSERT INTO EventDeduplicationMap VALUES ('m1', 'e1', 'd1', 'master', 1.0);
    """)
    return conn


def test_schema_mismatch_adds_columns_and_keeps_lineage():
    conn = _storage_db()
    DeduplicationStorage(conn)
    assert conn.execute("SELECT COUNT(*) FROM EventDeduplicationMap").fetchone()[0] == 1   # not dropped
    columns = {r[1] for r in conn.execute("PRAGMA table_info(EventDeduplicationMap)")}
    assert {"raw_event_id", "data_source_weight", "created_at"} <= columns


def test_invalid_results_are_rolled_back_and_reported_as_failure(monkeypatch):
    conn = _storage_db()
    storage = DeduplicationStorage(conn)
    monkeypatch.setattr(storage, "_validate_storage_integrity_locked",
                        lambda: [ValidationError(error_type="DUPLICATE_EVENT", message="boom", context={})])
    event = CyberEvent(event_id="e2", title="Acme breach", event_date=date(2024, 3, 12))
    result = DeduplicationResult(unique_events=[event], merge_groups=[],
                                 statistics=DeduplicationStats(1, 1, 0, 0, 1.0, 0.0), validation_errors=[])
    outcome = storage.store_deduplication_result(result)
    assert outcome.success is False
    assert conn.execute("SELECT COUNT(*) FROM DeduplicatedEvents").fetchone()[0] == 1   # only the fixture's


def test_events_sharing_a_title_and_date_are_all_stored():
    conn = _storage_db()
    storage = DeduplicationStorage(conn)
    events = [CyberEvent(event_id=f"e{i}", title="Data breach", event_date=date(2024, 5, 14),
                         victim_organization_name=name)
              for i, name in ((5, "University of Sydney"), (6, "University of Melbourne"))]
    result = DeduplicationResult(unique_events=events, merge_groups=[],
                                 statistics=DeduplicationStats(2, 2, 0, 0, 1.0, 0.0), validation_errors=[])
    assert storage.store_deduplication_result(result).success
    assert conn.execute("SELECT COUNT(*) FROM DeduplicatedEvents WHERE title = 'Data breach'").fetchone()[0] == 2
