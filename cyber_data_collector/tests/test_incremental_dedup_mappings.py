"""Regression tests for incremental-deduplication lineage mappings.

The incremental merge path (scripts/run_global_deduplication.py) used to store
``master_enriched_event_id`` in ``EventDeduplicationMap.deduplicated_event_id``
— a column whose FK references ``DeduplicatedEvents(deduplicated_event_id)``.
That produced ORPHANED_MAPPINGS integrity failures (and would violate the FK
under enforcement). These tests pin the corrected behaviour and the self-heal
that repairs rows written by the old convention.
"""

import os
import sqlite3
import tempfile

import pytest

from scripts.run_global_deduplication import DeduplicationMigration
from cyber_data_collector.processing.deduplication_v2 import CyberEvent
from cyber_data_collector.storage.deduplication_storage import DeduplicationStorage


@pytest.fixture
def real_schema_db():
    """A DB mirroring the production schema with all relevant foreign keys."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE RawEvents (
            raw_event_id TEXT PRIMARY KEY,
            source_type VARCHAR(50) NOT NULL,
            raw_title TEXT
        );
        CREATE TABLE EnrichedEvents (
            enriched_event_id TEXT PRIMARY KEY,
            raw_event_id TEXT NOT NULL,
            title VARCHAR(255) NOT NULL,
            status VARCHAR(20) DEFAULT 'Active',
            FOREIGN KEY (raw_event_id) REFERENCES RawEvents(raw_event_id) ON DELETE CASCADE
        );
        CREATE TABLE DeduplicatedEvents (
            deduplicated_event_id TEXT PRIMARY KEY,
            master_enriched_event_id TEXT NOT NULL,
            title VARCHAR(255) NOT NULL,
            event_date DATE,
            status TEXT DEFAULT 'Active',
            FOREIGN KEY (master_enriched_event_id)
                REFERENCES EnrichedEvents(enriched_event_id) ON DELETE CASCADE
        );
        CREATE TABLE EventDeduplicationMap (
            map_id TEXT PRIMARY KEY,
            raw_event_id TEXT NOT NULL,
            enriched_event_id TEXT,
            deduplicated_event_id TEXT NOT NULL,
            contribution_type VARCHAR(50),
            similarity_score REAL,
            data_source_weight REAL DEFAULT 1.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(raw_event_id, deduplicated_event_id),
            FOREIGN KEY (raw_event_id) REFERENCES RawEvents(raw_event_id) ON DELETE CASCADE,
            FOREIGN KEY (enriched_event_id) REFERENCES EnrichedEvents(enriched_event_id) ON DELETE CASCADE,
            FOREIGN KEY (deduplicated_event_id)
                REFERENCES DeduplicatedEvents(deduplicated_event_id) ON DELETE CASCADE
        );
        CREATE TABLE DeduplicationClusters (
            cluster_id TEXT PRIMARY KEY,
            deduplicated_event_id TEXT NOT NULL,
            cluster_size INTEGER NOT NULL,
            average_similarity REAL,
            deduplication_timestamp TIMESTAMP,
            algorithm_version VARCHAR(20),
            FOREIGN KEY (deduplicated_event_id)
                REFERENCES DeduplicatedEvents(deduplicated_event_id) ON DELETE CASCADE
        );
    """)
    # Seed: raw -> enriched -> a deduplicated event whose master is enr-existing,
    # plus a brand-new enriched event (enr-new) to merge in.
    cur.execute("INSERT INTO RawEvents VALUES ('raw-existing', 'Perplexity', 'Existing')")
    cur.execute("INSERT INTO RawEvents VALUES ('raw-new', 'Perplexity', 'New')")
    cur.execute("INSERT INTO EnrichedEvents VALUES ('enr-existing', 'raw-existing', 'Existing', 'Active')")
    cur.execute("INSERT INTO EnrichedEvents VALUES ('enr-new', 'raw-new', 'New', 'Active')")
    cur.execute(
        "INSERT INTO DeduplicatedEvents VALUES ('dedup-uuid-1', 'enr-existing', 'Existing', '2026-02-10', 'Active')"
    )
    conn.commit()
    yield conn, db_path
    conn.close()
    os.unlink(db_path)


def _migration():
    return DeduplicationMigration.__new__(DeduplicationMigration)


def test_merge_into_existing_stores_real_dedup_id(real_schema_db):
    conn, _ = real_schema_db
    conn.execute("PRAGMA foreign_keys = ON;")  # the FK must be satisfiable
    cur = conn.cursor()

    new_event = CyberEvent(event_id="enr-new", title="New")
    existing = CyberEvent(event_id="dedup-uuid-1", title="Existing")
    dedup_to_master = {"dedup-uuid-1": "enr-existing"}

    _migration()._merge_into_existing(cur, new_event, existing, dedup_to_master)
    conn.commit()

    row = cur.execute(
        "SELECT raw_event_id, enriched_event_id, deduplicated_event_id "
        "FROM EventDeduplicationMap"
    ).fetchone()
    # deduplicated_event_id must be the real dedup UUID, NOT the master enriched id.
    assert row == ("raw-new", "enr-new", "dedup-uuid-1")

    errors = DeduplicationStorage(conn).validate_storage_integrity()
    assert errors == []


def test_repair_heals_legacy_orphaned_mapping(real_schema_db):
    conn, _ = real_schema_db
    cur = conn.cursor()

    # Simulate a row written by the OLD buggy convention: deduplicated_event_id
    # holds the master_enriched_event_id instead of the real dedup UUID.
    cur.execute(
        "INSERT INTO EventDeduplicationMap "
        "(map_id, raw_event_id, enriched_event_id, deduplicated_event_id, contribution_type) "
        "VALUES ('m1', 'raw-new', 'enr-new', 'enr-existing', 'incremental_merge')"
    )
    conn.commit()

    storage = DeduplicationStorage(conn)
    before = storage.validate_storage_integrity()
    assert any(e.error_type == "ORPHANED_MAPPINGS" for e in before)

    repaired = _migration()._repair_orphaned_incremental_mappings(cur)
    conn.commit()
    assert repaired == 1

    row = cur.execute(
        "SELECT deduplicated_event_id FROM EventDeduplicationMap WHERE map_id='m1'"
    ).fetchone()
    assert row[0] == "dedup-uuid-1"  # now the real dedup UUID
    assert storage.validate_storage_integrity() == []


def test_repair_is_idempotent_and_safe_on_clean_db(real_schema_db):
    conn, _ = real_schema_db
    cur = conn.cursor()
    # No orphans present — repair must touch nothing.
    assert _migration()._repair_orphaned_incremental_mappings(cur) == 0


def _incremental_migration(db_path):
    migration = _migration()
    migration.db_path = db_path
    migration.dry_run = False
    migration.migration_report = {"steps_completed": [], "errors": [], "statistics": {}}
    return migration


def test_whole_new_group_joins_the_matched_event(real_schema_db, monkeypatch):
    """Every record of a new group is attached, not just its lead record."""
    conn, db_path = real_schema_db
    conn.execute("INSERT INTO RawEvents VALUES ('raw-new2', 'Perplexity', 'New 2')")
    conn.execute("INSERT INTO EnrichedEvents VALUES ('enr-new2', 'raw-new2', 'New 2', 'Active')")
    conn.commit()
    from types import SimpleNamespace
    from datetime import date
    import cyber_data_collector.dedup.backfill as backfill
    monkeypatch.setattr(backfill, "run_backfill", lambda c: SimpleNamespace(summary=lambda: "stub"))

    day = date(2026, 2, 10)
    count = 9_800_000      # the incident-specific fact the records agree on
    new = [CyberEvent(event_id="enr-new", title="Optus breach", event_date=day, victim_organization_name="Optus",
                      records_affected=count),
           CyberEvent(event_id="enr-new2", title="Optus hack", event_date=day, victim_organization_name="Optus",
                      records_affected=count)]
    existing = [CyberEvent(event_id="dedup-uuid-1", title="Existing", event_date=day,
                           victim_organization_name="Singtel Optus Pty Limited", record_counts=[count])]

    assert _incremental_migration(db_path)._run_incremental_deduplication(
        new, existing, {"dedup-uuid-1": "enr-existing"})

    mapped = {r[0] for r in conn.execute(
        "SELECT enriched_event_id FROM EventDeduplicationMap WHERE deduplicated_event_id = 'dedup-uuid-1'")}
    assert mapped == {"enr-new", "enr-new2"}
    import gc
    gc.collect()  # the run's connection sits in a reference cycle; free it so Windows can delete the file


def test_split_ruling_keeps_a_record_out_of_its_old_event(real_schema_db, monkeypatch):
    """The incremental matcher uses the shared decision, vetoed by any member's ruling."""
    conn, db_path = real_schema_db
    conn.executescript("""
        CREATE TABLE DedupOverrides (left_enriched_event_id TEXT, right_enriched_event_id TEXT,
                                     verdict TEXT, active INTEGER);
        INSERT INTO EventDeduplicationMap (map_id, raw_event_id, enriched_event_id, deduplicated_event_id)
            VALUES ('m0', 'raw-existing', 'enr-existing', 'dedup-uuid-1');
        INSERT INTO DedupOverrides VALUES ('enr-existing', 'enr-new', 'different', 1);
    """)
    conn.commit()
    from types import SimpleNamespace
    from datetime import date
    import cyber_data_collector.dedup.backfill as backfill
    from cyber_data_collector.storage.deduplication_storage import DeduplicationStorage as Storage
    monkeypatch.setattr(backfill, "run_backfill", lambda c: SimpleNamespace(summary=lambda: "stub"))
    stored = []
    monkeypatch.setattr(Storage, "store_deduplication_result",
                        lambda self, result: stored.append(result) or SimpleNamespace(success=True))
    day = date(2026, 2, 10)
    new = [CyberEvent(event_id="enr-new", title="Optus breach", event_date=day, victim_organization_name="Optus")]
    existing = [CyberEvent(event_id="dedup-uuid-1", title="Existing", event_date=day,
                           victim_organization_name="Optus")]
    assert _incremental_migration(db_path)._run_incremental_deduplication(new, existing, {})
    mapped = {r[0] for r in conn.execute(
        "SELECT enriched_event_id FROM EventDeduplicationMap WHERE deduplicated_event_id = 'dedup-uuid-1'")}
    assert mapped == {"enr-existing"}          # not re-attached
    assert stored and stored[0].unique_events[0].event_id == "enr-new"   # stored as its own event
    import gc
    gc.collect()


def test_membership_check_finds_records_in_no_event_and_in_two(real_schema_db):
    conn, _ = real_schema_db
    cur = conn.cursor()
    # enr-new is active and in no event.
    errors = _migration()._membership_errors(cur)
    assert len(errors) == 1 and "belong to no event" in errors[0]

    cur.execute("INSERT INTO DeduplicatedEvents VALUES ('dedup-uuid-2', 'enr-new', 'New', '2026-02-10', 'Active')")
    cur.execute("INSERT INTO EventDeduplicationMap (map_id, raw_event_id, enriched_event_id, deduplicated_event_id) "
                "VALUES ('a', 'raw-new', 'enr-new', 'dedup-uuid-1')")
    cur.execute("INSERT INTO EventDeduplicationMap (map_id, raw_event_id, enriched_event_id, deduplicated_event_id) "
                "VALUES ('b', 'raw-new', 'enr-new', 'dedup-uuid-2')")
    errors = _migration()._membership_errors(cur)
    assert len(errors) == 1 and "more than one active event" in errors[0]


def test_membership_check_ignores_records_removed_by_rejection(real_schema_db):
    conn, _ = real_schema_db
    cur = conn.cursor()
    cur.execute("INSERT INTO DeduplicatedEvents VALUES ('dedup-rej', 'enr-new', 'New', '2026-02-10', 'Rejected')")
    cur.execute("INSERT INTO EventDeduplicationMap (map_id, raw_event_id, enriched_event_id, deduplicated_event_id) "
                "VALUES ('r', 'raw-new', 'enr-new', 'dedup-rej')")
    assert _migration()._membership_errors(cur) == []
