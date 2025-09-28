#!/usr/bin/env python3
"""
Add deduplication schema to the V2 database.

This script creates the necessary tables for proper event deduplication:
1. DeduplicatedEvents - The final, unique cyber events
2. EventDeduplicationMap - Links raw events to deduplicated events
3. DeduplicationClusters - Groups of similar events that were merged
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def add_deduplication_schema(db_path: str = "instance/cyber_events.db"):
    """Add deduplication tables to the database."""

    print("Adding deduplication schema to V2 database...")

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")

        cursor = conn.cursor()

        # Schema for deduplicated events
        deduplication_schema = """
        -- Deduplicated Events: The final, unique cyber events after deduplication
        CREATE TABLE IF NOT EXISTS DeduplicatedEvents (
            deduplicated_event_id TEXT PRIMARY KEY,
            master_enriched_event_id TEXT NOT NULL,  -- References the "best" enriched event
            title VARCHAR(255) NOT NULL,
            description TEXT,
            summary TEXT,
            event_type VARCHAR(50),
            severity VARCHAR(20),
            event_date DATE,
            records_affected BIGINT,
            is_australian_event BOOLEAN NOT NULL,
            is_specific_event BOOLEAN NOT NULL,
            confidence_score REAL,
            australian_relevance_score REAL,

            -- Aggregated data from all contributing events
            total_data_sources INTEGER DEFAULT 0,      -- Number of contributing data sources
            contributing_raw_events INTEGER DEFAULT 0,  -- Number of raw events that contributed
            contributing_enriched_events INTEGER DEFAULT 0, -- Number of enriched events merged

            -- Deduplication metadata
            similarity_score REAL,                     -- Average similarity of merged events
            deduplication_method VARCHAR(50),          -- 'title_similarity', 'entity_match', 'manual'

            status VARCHAR(20) DEFAULT 'Active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (master_enriched_event_id) REFERENCES EnrichedEvents(enriched_event_id) ON DELETE CASCADE
        );

        -- Maps raw events to their deduplicated counterparts
        CREATE TABLE IF NOT EXISTS EventDeduplicationMap (
            map_id TEXT PRIMARY KEY,
            raw_event_id TEXT NOT NULL,
            enriched_event_id TEXT,                    -- May be NULL if raw event wasn't enriched
            deduplicated_event_id TEXT NOT NULL,
            contribution_type VARCHAR(50),             -- 'primary', 'supporting', 'duplicate'
            similarity_score REAL,                     -- How similar this event was to the master
            data_source_weight REAL DEFAULT 1.0,      -- Importance weighting of this source
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            UNIQUE(raw_event_id, deduplicated_event_id),
            FOREIGN KEY (raw_event_id) REFERENCES RawEvents(raw_event_id) ON DELETE CASCADE,
            FOREIGN KEY (enriched_event_id) REFERENCES EnrichedEvents(enriched_event_id) ON DELETE CASCADE,
            FOREIGN KEY (deduplicated_event_id) REFERENCES DeduplicatedEvents(deduplicated_event_id) ON DELETE CASCADE
        );

        -- Tracks deduplication clusters (groups of events that were merged)
        CREATE TABLE IF NOT EXISTS DeduplicationClusters (
            cluster_id TEXT PRIMARY KEY,
            deduplicated_event_id TEXT NOT NULL,
            cluster_size INTEGER NOT NULL,             -- Number of events in this cluster
            average_similarity REAL,                   -- Average pairwise similarity
            deduplication_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            algorithm_version VARCHAR(20),             -- Version of deduplication algorithm used

            FOREIGN KEY (deduplicated_event_id) REFERENCES DeduplicatedEvents(deduplicated_event_id) ON DELETE CASCADE
        );

        -- Link deduplicated events to entities
        CREATE TABLE IF NOT EXISTS DeduplicatedEventEntities (
            deduplicated_event_id TEXT,
            entity_id INTEGER,
            relationship_type VARCHAR(50) DEFAULT 'affected',
            confidence_score REAL,
            source_count INTEGER DEFAULT 1,            -- How many sources mentioned this entity

            PRIMARY KEY (deduplicated_event_id, entity_id),
            FOREIGN KEY (deduplicated_event_id) REFERENCES DeduplicatedEvents(deduplicated_event_id) ON DELETE CASCADE,
            FOREIGN KEY (entity_id) REFERENCES EntitiesV2(entity_id) ON DELETE CASCADE
        );

        -- Data sources consolidated at deduplicated event level
        CREATE TABLE IF NOT EXISTS DeduplicatedEventSources (
            deduplicated_event_id TEXT,
            source_url VARCHAR(2048),
            source_type VARCHAR(50),
            credibility_score REAL,
            content_snippet TEXT,
            discovered_at TIMESTAMP,

            PRIMARY KEY (deduplicated_event_id, source_url),
            FOREIGN KEY (deduplicated_event_id) REFERENCES DeduplicatedEvents(deduplicated_event_id) ON DELETE CASCADE
        );

        -- Indexes for performance
        CREATE INDEX IF NOT EXISTS idx_dedup_events_australian ON DeduplicatedEvents(is_australian_event);
        CREATE INDEX IF NOT EXISTS idx_dedup_events_date ON DeduplicatedEvents(event_date);
        CREATE INDEX IF NOT EXISTS idx_dedup_events_type ON DeduplicatedEvents(event_type);
        CREATE INDEX IF NOT EXISTS idx_dedup_map_raw_event ON EventDeduplicationMap(raw_event_id);
        CREATE INDEX IF NOT EXISTS idx_dedup_map_dedup_event ON EventDeduplicationMap(deduplicated_event_id);
        CREATE INDEX IF NOT EXISTS idx_dedup_sources_type ON DeduplicatedEventSources(source_type);
        """

        cursor.executescript(deduplication_schema)
        conn.commit()

        print("Deduplication schema added successfully")

        # Check tables were created
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%Dedup%'")
        tables = [row[0] for row in cursor.fetchall()]
        print(f"Created tables: {', '.join(tables)}")

        conn.close()
        return True

    except Exception as e:
        print(f"Error adding deduplication schema: {e}")
        return False


if __name__ == "__main__":
    success = add_deduplication_schema()
    if success:
        print("Deduplication schema setup complete!")
    else:
        print("Schema setup failed!")
        sys.exit(1)