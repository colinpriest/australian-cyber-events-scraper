#!/usr/bin/env python3
"""
Database Migration Script - Version 2 Schema
Migrates from mixed raw/enriched schema to separated schema

This script creates new tables for separated raw and enriched events:
- RawEvents: Contains raw event data from all sources
- EnrichedEvents: Contains processed and validated events
- ProcessingLog: Tracks which raw events have been processed
"""

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List


class DatabaseMigrationV2:
    def __init__(self, db_path: str = "instance/cyber_events.db"):
        self.db_path = Path(db_path)
        self.conn = None

    def connect(self):
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")

    def close(self):
        if self.conn:
            self.conn.close()

    def create_v2_schema(self):
        """Create the new separated schema"""
        print("Creating Version 2 schema with separated raw and enriched events...")

        schema_script = """
        -- Raw Events Table: Store all discovered events before processing
        CREATE TABLE IF NOT EXISTS RawEvents (
            raw_event_id TEXT PRIMARY KEY,
            source_type VARCHAR(50) NOT NULL,  -- 'GDELT', 'Perplexity', 'GoogleSearch', 'WebberInsurance'
            source_event_id TEXT,              -- External ID from source system
            raw_title TEXT,
            raw_description TEXT,
            raw_content TEXT,                  -- Full scraped content if applicable
            event_date DATE,
            source_url VARCHAR(2048),
            source_metadata TEXT,              -- JSON for additional source-specific data
            discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_processed BOOLEAN DEFAULT FALSE,
            processing_attempted_at TIMESTAMP NULL,
            processing_error TEXT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Enriched Events Table: Store processed and validated events
        CREATE TABLE IF NOT EXISTS EnrichedEvents (
            enriched_event_id TEXT PRIMARY KEY,
            raw_event_id TEXT NOT NULL,        -- Link back to raw event
            title VARCHAR(255) NOT NULL,
            description TEXT,
            summary TEXT,                      -- LLM-generated summary
            event_type VARCHAR(50),
            severity VARCHAR(20),
            event_date DATE,
            records_affected BIGINT,
            is_australian_event BOOLEAN NOT NULL,
            is_specific_event BOOLEAN NOT NULL,
            confidence_score REAL,             -- Overall confidence in event data
            australian_relevance_score REAL,  -- How relevant to Australia (0-1)
            status VARCHAR(20) DEFAULT 'Active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (raw_event_id) REFERENCES RawEvents(raw_event_id) ON DELETE CASCADE
        );

        -- Processing Log: Track processing attempts and results
        CREATE TABLE IF NOT EXISTS ProcessingLog (
            log_id TEXT PRIMARY KEY,
            raw_event_id TEXT NOT NULL,
            processing_stage VARCHAR(50) NOT NULL,  -- 'url_scraping', 'llm_analysis', 'entity_extraction'
            status VARCHAR(20) NOT NULL,            -- 'success', 'failed', 'skipped'
            result_data TEXT,                       -- JSON result data
            error_message TEXT,
            processing_time_ms INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (raw_event_id) REFERENCES RawEvents(raw_event_id) ON DELETE CASCADE
        );

        -- Enhanced Entities Table (keeping existing but adding fields)
        CREATE TABLE IF NOT EXISTS EntitiesV2 (
            entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_name VARCHAR(255) UNIQUE NOT NULL,
            entity_type VARCHAR(50),
            industry VARCHAR(100),
            turnover VARCHAR(50),
            employee_count INTEGER,
            is_australian BOOLEAN,
            headquarters_location TEXT,
            website_url TEXT,
            confidence_score REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Link enriched events to entities
        CREATE TABLE IF NOT EXISTS EnrichedEventEntities (
            enriched_event_id TEXT,
            entity_id INTEGER,
            relationship_type VARCHAR(50) DEFAULT 'affected',  -- 'primary_target', 'affected', 'mentioned'
            confidence_score REAL,
            PRIMARY KEY (enriched_event_id, entity_id),
            FOREIGN KEY (enriched_event_id) REFERENCES EnrichedEvents(enriched_event_id) ON DELETE CASCADE,
            FOREIGN KEY (entity_id) REFERENCES EntitiesV2(entity_id) ON DELETE CASCADE
        );

        -- Data Sources (enhanced)
        CREATE TABLE IF NOT EXISTS DataSourcesV2 (
            source_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name VARCHAR(100) UNIQUE NOT NULL,
            source_type VARCHAR(50),
            base_url TEXT,
            api_endpoint TEXT,
            credibility_score REAL,
            last_accessed TIMESTAMP,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Audit Log for changes
        CREATE TABLE IF NOT EXISTS AuditLog (
            audit_id TEXT PRIMARY KEY,
            table_name VARCHAR(50) NOT NULL,
            record_id TEXT NOT NULL,
            action VARCHAR(20) NOT NULL,  -- 'INSERT', 'UPDATE', 'DELETE'
            old_values TEXT,              -- JSON
            new_values TEXT,              -- JSON
            changed_by TEXT,              -- System/User identifier
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Indexes for performance
        CREATE INDEX IF NOT EXISTS idx_raw_events_source_type ON RawEvents(source_type);
        CREATE INDEX IF NOT EXISTS idx_raw_events_is_processed ON RawEvents(is_processed);
        CREATE INDEX IF NOT EXISTS idx_raw_events_discovered_at ON RawEvents(discovered_at);
        CREATE INDEX IF NOT EXISTS idx_enriched_events_australian ON EnrichedEvents(is_australian_event);
        CREATE INDEX IF NOT EXISTS idx_enriched_events_specific ON EnrichedEvents(is_specific_event);
        CREATE INDEX IF NOT EXISTS idx_enriched_events_date ON EnrichedEvents(event_date);
        CREATE INDEX IF NOT EXISTS idx_processing_log_status ON ProcessingLog(status);
        """

        cursor = self.conn.cursor()
        cursor.executescript(schema_script)
        self.conn.commit()
        print("✅ Version 2 schema created successfully")

    def migrate_existing_data(self):
        """Migrate existing data to new schema"""
        print("Migrating existing data to Version 2 schema...")

        cursor = self.conn.cursor()

        # Get existing events
        cursor.execute("""
            SELECT ue.*, es.url, es.raw_title, es.raw_content, es.is_enriched
            FROM UniqueEvents ue
            LEFT JOIN EventSources es ON ue.event_id = es.event_id
        """)

        events = cursor.fetchall()
        migrated_count = 0

        for event in events:
            try:
                raw_event_id = str(uuid.uuid4())

                # Create raw event
                cursor.execute("""
                    INSERT INTO RawEvents (
                        raw_event_id, source_type, raw_title, raw_description,
                        event_date, source_url, is_processed, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    raw_event_id,
                    'Unknown',  # We don't have source type in old schema
                    event.get('title'),
                    event.get('description'),
                    event.get('event_date'),
                    event.get('url'),
                    bool(event.get('is_enriched')),
                    event.get('created_at') or datetime.now().isoformat()
                ))

                # If enriched, create enriched event
                if (event.get('is_australian_event') is not None or
                    event.get('is_specific_event') is not None):

                    enriched_event_id = str(uuid.uuid4())
                    cursor.execute("""
                        INSERT INTO EnrichedEvents (
                            enriched_event_id, raw_event_id, title, description,
                            event_type, severity, event_date, records_affected,
                            is_australian_event, is_specific_event, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        enriched_event_id,
                        raw_event_id,
                        event.get('title'),
                        event.get('description'),
                        event.get('event_type'),
                        event.get('severity'),
                        event.get('event_date'),
                        event.get('records_affected'),
                        bool(event.get('is_australian_event')) if event.get('is_australian_event') is not None else False,
                        bool(event.get('is_specific_event')) if event.get('is_specific_event') is not None else False,
                        event.get('status', 'Active'),
                        event.get('created_at') or datetime.now().isoformat(),
                        event.get('updated_at') or datetime.now().isoformat()
                    ))

                migrated_count += 1

            except Exception as e:
                print(f"Error migrating event {event.get('event_id')}: {e}")

        self.conn.commit()
        print(f"✅ Migrated {migrated_count} events to Version 2 schema")

    def backup_old_schema(self):
        """Rename old tables with _v1_backup suffix"""
        print("Backing up old schema...")

        backup_commands = [
            "ALTER TABLE UniqueEvents RENAME TO UniqueEvents_v1_backup",
            "ALTER TABLE Entities RENAME TO Entities_v1_backup",
            "ALTER TABLE EventEntities RENAME TO EventEntities_v1_backup",
            "ALTER TABLE DataSources RENAME TO DataSources_v1_backup",
            "ALTER TABLE EventSources RENAME TO EventSources_v1_backup",
            "ALTER TABLE EventAttributeHistory RENAME TO EventAttributeHistory_v1_backup"
        ]

        cursor = self.conn.cursor()
        for command in backup_commands:
            try:
                cursor.execute(command)
            except sqlite3.Error as e:
                print(f"Warning: {e}")

        self.conn.commit()
        print("✅ Old schema backed up")

    def insert_default_data_sources(self):
        """Insert default data sources"""
        print("Inserting default data sources...")

        sources = [
            ('GDELT Project', 'GDELT', 'https://gdeltproject.org', None, 0.8),
            ('Perplexity AI', 'Perplexity', 'https://api.perplexity.ai', '/chat/completions', 0.7),
            ('Google Custom Search', 'GoogleSearch', 'https://www.googleapis.com', '/customsearch/v1', 0.6),
            ('Webber Insurance', 'WebberInsurance', 'https://www.webberinsurance.com.au', None, 0.9)
        ]

        cursor = self.conn.cursor()
        for name, source_type, base_url, api_endpoint, credibility in sources:
            cursor.execute("""
                INSERT OR IGNORE INTO DataSourcesV2
                (source_name, source_type, base_url, api_endpoint, credibility_score, last_accessed, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (name, source_type, base_url, api_endpoint, credibility,
                  datetime.now().isoformat(), datetime.now().isoformat()))

        self.conn.commit()
        print("✅ Default data sources inserted")

    def run_migration(self):
        """Run the complete migration"""
        print("🚀 Starting database migration to Version 2 schema...")

        try:
            self.connect()

            # Create backup
            print("\n1. Creating backup of current database...")
            backup_path = self.db_path.parent / f"cyber_events_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            import shutil
            shutil.copy2(self.db_path, backup_path)
            print(f"✅ Backup created: {backup_path}")

            # Create new schema
            print("\n2. Creating Version 2 schema...")
            self.create_v2_schema()

            # Migrate existing data
            print("\n3. Migrating existing data...")
            self.migrate_existing_data()

            # Insert default data
            print("\n4. Inserting default data...")
            self.insert_default_data_sources()

            # Backup old schema
            print("\n5. Backing up old schema...")
            self.backup_old_schema()

            print("\n🎉 Migration completed successfully!")
            print(f"📁 Original data backed up to: {backup_path}")
            print("📋 Old tables renamed with '_v1_backup' suffix")
            print("🔄 Database is ready for the new discover_enrich_events.py script")

        except Exception as e:
            print(f"❌ Migration failed: {e}")
            raise
        finally:
            self.close()


if __name__ == "__main__":
    migration = DatabaseMigrationV2()
    migration.run_migration()