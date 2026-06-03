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
