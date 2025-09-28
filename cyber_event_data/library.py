from __future__ import annotations

import hashlib
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

class CyberEventData:
    """
    A thread-safe library for storing and managing cyber event data in a SQLite database.
    """

    def __init__(self, db_path: str | Path = "instance/cyber_events.db"):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        self._connect()
        self._create_tables()
        self._update_schema()

    def _connect(self):
        try:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON;")
        except sqlite3.Error as e:
            print(f"Database connection error: {e}")
            raise

    def _execute_script(self, script: str):
        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.executescript(script)
                self._conn.commit()
            except sqlite3.Error as e:
                print(f"SQL script execution error: {e}")
                self._conn.rollback()
                raise

    def _create_tables(self):
        create_script = """
            CREATE TABLE IF NOT EXISTS UniqueEvents (
                event_id TEXT PRIMARY KEY,
                unique_hash VARCHAR(64) UNIQUE NOT NULL,
                title VARCHAR(255) NOT NULL,
                description TEXT,
                event_date DATE NOT NULL,
                event_type VARCHAR(50),
                severity VARCHAR(20),
                records_affected BIGINT,
                status VARCHAR(20) DEFAULT 'Active',
                is_australian_event BOOLEAN DEFAULT NULL,
                is_specific_event BOOLEAN DEFAULT NULL,
                is_reviewed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS Entities (
                entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_name VARCHAR(255) UNIQUE NOT NULL,
                entity_type VARCHAR(50),
                industry VARCHAR(100),
                turnover VARCHAR(50),
                employee_count INT
            );
            CREATE TABLE IF NOT EXISTS EventEntities (
                event_id TEXT,
                entity_id INTEGER,
                PRIMARY KEY (event_id, entity_id),
                FOREIGN KEY (event_id) REFERENCES UniqueEvents(event_id) ON DELETE CASCADE,
                FOREIGN KEY (entity_id) REFERENCES Entities(entity_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS DataSources (
                source_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_name VARCHAR(100) UNIQUE NOT NULL,
                source_type VARCHAR(50)
            );
            CREATE TABLE IF NOT EXISTS EventSources (
                event_source_id TEXT PRIMARY KEY,
                event_id TEXT,
                source_id INTEGER,
                url VARCHAR(2048) UNIQUE,
                published_date TIMESTAMP,
                retrieved_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                raw_title TEXT,
                raw_content TEXT,
                is_enriched BOOLEAN DEFAULT FALSE,
                FOREIGN KEY (event_id) REFERENCES UniqueEvents(event_id) ON DELETE CASCADE,
                FOREIGN KEY (source_id) REFERENCES DataSources(source_id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS EventAttributeHistory (
                history_id TEXT PRIMARY KEY,
                event_id TEXT,
                event_source_id TEXT,
                attribute_name VARCHAR(50) NOT NULL,
                old_value TEXT,
                new_value TEXT,
                change_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (event_id) REFERENCES UniqueEvents(event_id) ON DELETE CASCADE,
                FOREIGN KEY (event_source_id) REFERENCES EventSources(event_source_id) ON DELETE SET NULL
            );
        """
        self._execute_script(create_script)

    def _update_schema(self):
        with self._lock:
            cursor = self._conn.cursor()
            try:
                table_info = cursor.execute("PRAGMA table_info(UniqueEvents)").fetchall()
                columns = [col['name'] for col in table_info]
                if 'is_australian_event' not in columns:
                    cursor.execute("ALTER TABLE UniqueEvents ADD COLUMN is_australian_event BOOLEAN DEFAULT NULL")
                if 'is_specific_event' not in columns:
                    cursor.execute("ALTER TABLE UniqueEvents ADD COLUMN is_specific_event BOOLEAN DEFAULT NULL")
                if 'is_reviewed' not in columns:
                    cursor.execute("ALTER TABLE UniqueEvents ADD COLUMN is_reviewed BOOLEAN DEFAULT FALSE")

                table_info = cursor.execute("PRAGMA table_info(EventSources)").fetchall()
                columns = [col['name'] for col in table_info]
                if 'is_enriched' not in columns:
                    cursor.execute("ALTER TABLE EventSources ADD COLUMN is_enriched BOOLEAN DEFAULT FALSE")
                
                self._conn.commit()
            except sqlite3.Error as e:
                print(f"Schema update error: {e}")
                self._conn.rollback()

    def add_event(self, event_data: Dict[str, Any]):
        # Simplified logic for initial data insertion
        if not self._conn: raise ConnectionError("DB not connected.")
        if not all(k in event_data for k in ["title", "event_date", "data_sources"]): return

        normalized_title = "".join(filter(str.isalnum, event_data["title"])).lower()
        event_date_str = str(event_data.get("event_date", ""))[:10]
        unique_hash = hashlib.sha256(f"{normalized_title}{event_date_str}".encode()).hexdigest()

        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute("SELECT event_id FROM UniqueEvents WHERE unique_hash = ?", (unique_hash,))
                existing_event = cursor.fetchone()
                event_id = existing_event['event_id'] if existing_event else str(uuid.uuid4())

                if not existing_event:
                    cursor.execute("INSERT INTO UniqueEvents (event_id, unique_hash, title, description, event_date) VALUES (?, ?, ?, ?, ?)",
                                 (event_id, unique_hash, event_data.get("title"), event_data.get("description"), event_date_str))

                for source in event_data.get("data_sources", []):
                    if source.get("url"):
                        cursor.execute("INSERT OR IGNORE INTO EventSources (event_source_id, event_id, url) VALUES (?, ?, ?)", 
                                     (str(uuid.uuid4()), event_id, source.get("url")))
                self._conn.commit()
            except sqlite3.Error as e:
                print(f"DB error in add_event: {e}")
                if self._conn: self._conn.rollback()

    def get_unenriched_urls(self, limit: int = 50) -> List[Dict[str, str]]:
        if not self._conn: raise ConnectionError("DB not connected.")
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("SELECT url, event_id FROM EventSources WHERE is_enriched = FALSE AND url IS NOT NULL LIMIT ?", (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def get_events_to_review(self, limit: int = 100) -> List[Dict[str, Any]]:
        if not self._conn: raise ConnectionError("DB not connected.")
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("SELECT * FROM UniqueEvents WHERE is_reviewed = FALSE AND is_specific_event IS NOT NULL LIMIT ?", (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def get_events_missing_details(self, limit: int = 50) -> List[Dict[str, str]]:
        if not self._conn: raise ConnectionError("DB not connected.")
        with self._lock:
            cursor = self._conn.cursor()
            # Find events with short/generic descriptions that have an unscraped source URL
            cursor.execute("""
                SELECT ue.event_id, es.url FROM UniqueEvents ue
                JOIN EventSources es ON ue.event_id = es.event_id
                WHERE (ue.description IS NULL OR LENGTH(ue.description) < 100) AND es.is_enriched = FALSE
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def get_entities_for_event(self, event_id: str) -> List[str]:
        """Retrieves all entity names for a given event."""
        if not self._conn: raise ConnectionError("DB not connected.")
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("""
                SELECT e.entity_name FROM Entities e
                JOIN EventEntities ee ON e.entity_id = ee.entity_id
                WHERE ee.event_id = ?
            """, (event_id,))
            return [row['entity_name'] for row in cursor.fetchall()]

    def update_event_details(self, event_id: str, details: Dict[str, Any]):
        if not self._conn: raise ConnectionError("DB not connected.")
        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute("UPDATE UniqueEvents SET description = ?, is_specific_event = ?, is_australian_event = ? WHERE event_id = ?",
                             (details.get('summary'), details.get('is_specific_event'), details.get('is_australian_event'), event_id))
                self._conn.commit()
            except sqlite3.Error as e:
                print(f"DB error in update_event_details: {e}")
                self._conn.rollback()

    def mark_url_as_processed(self, url: str):
        if not self._conn: raise ConnectionError("DB not connected.")
        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute("UPDATE EventSources SET is_enriched = TRUE WHERE url = ?", (url,))
                self._conn.commit()
            except sqlite3.Error as e:
                print(f"DB error in mark_url_as_processed: {e}")
                self._conn.rollback()

    def update_event_with_enriched_data(self, url: str, enriched_data: Dict[str, Any]):
        if not self._conn: raise ConnectionError("DB not connected.")
        if not enriched_data:
            return self.mark_url_as_processed(url)
        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute("SELECT event_id FROM EventSources WHERE url = ?", (url,))
                event_row = cursor.fetchone()
                if not event_row: return
                event_id = event_row['event_id']

                cursor.execute("UPDATE UniqueEvents SET is_australian_event = ?, is_specific_event = ? WHERE event_id = ?",
                             (enriched_data.get('is_australian_event'), enriched_data.get('is_specific_event'), event_id))

                all_entities = [enriched_data.get('primary_entity')] + enriched_data.get('affected_entities', [])
                for name in set(filter(None, all_entities)):
                    cursor.execute("INSERT OR IGNORE INTO Entities (entity_name) VALUES (?)", (name,))
                    cursor.execute("SELECT entity_id FROM Entities WHERE entity_name = ?", (name,))
                    entity_id_db = cursor.fetchone()['entity_id']
                    cursor.execute("INSERT OR IGNORE INTO EventEntities (event_id, entity_id) VALUES (?, ?)", (event_id, entity_id_db))

                cursor.execute("UPDATE EventSources SET is_enriched = TRUE WHERE url = ?", (url,))
                self._conn.commit()
            except sqlite3.Error as e:
                print(f"DB error in update_event_with_enriched_data: {e}")
                self._conn.rollback()

    def delete_event(self, event_id: str):
        if not self._conn: raise ConnectionError("DB not connected.")
        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute("DELETE FROM UniqueEvents WHERE event_id = ?", (event_id,))
                self._conn.commit()
                print(f"Deleted event {event_id} due to quality filters.")
            except sqlite3.Error as e:
                print(f"DB error in delete_event: {e}")
                self._conn.rollback()

    def reset_review_status(self):
        """Resets the review status for all events, allowing them to be re-processed."""
        if not self._conn: raise ConnectionError("DB not connected.")
        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute("UPDATE UniqueEvents SET is_reviewed = FALSE")
                self._conn.commit()
                print(f"Reset review status for {cursor.rowcount} events.")
            except sqlite3.Error as e:
                print(f"DB error in reset_review_status: {e}")
                self._conn.rollback()

    def mark_event_as_reviewed(self, event_id: str):
        """Marks an event as reviewed to prevent re-processing."""
        if not self._conn: raise ConnectionError("DB not connected.")
        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute("UPDATE UniqueEvents SET is_reviewed = TRUE WHERE event_id = ?", (event_id,))
                self._conn.commit()
            except sqlite3.Error as e:
                print(f"DB error in mark_event_as_reviewed: {e}")
                self._conn.rollback()

    def get_summary_statistics(self) -> Dict[str, Any]:
        """Get summary statistics for the database (V1 schema compatibility)"""
        if not self._conn:
            raise ConnectionError("Database not connected")

        with self._lock:
            cursor = self._conn.cursor()
            stats = {}

            try:
                # Unique events count
                cursor.execute("SELECT COUNT(*) as count FROM UniqueEvents")
                stats['unique_event_count'] = cursor.fetchone()['count']

                # Unique entities count
                cursor.execute("SELECT COUNT(*) as count FROM Entities")
                stats['unique_entity_count'] = cursor.fetchone()['count']

                # Events by category (event_type)
                cursor.execute("SELECT event_type, COUNT(*) as count FROM UniqueEvents WHERE event_type IS NOT NULL GROUP BY event_type")
                stats['events_by_category'] = {row['event_type']: row['count'] for row in cursor.fetchall()}

                # Events by source
                cursor.execute("""
                    SELECT ds.source_name, COUNT(DISTINCT ue.event_id) as count
                    FROM UniqueEvents ue
                    JOIN EventSources es ON ue.event_id = es.event_id
                    JOIN DataSources ds ON es.source_id = ds.source_id
                    GROUP BY ds.source_name
                """)
                stats['events_by_source'] = {row['source_name']: row['count'] for row in cursor.fetchall()}

                # Events by entity industry (if available)
                cursor.execute("""
                    SELECT e.industry, COUNT(DISTINCT ue.event_id) as count
                    FROM UniqueEvents ue
                    JOIN EventEntities ee ON ue.event_id = ee.event_id
                    JOIN Entities e ON ee.entity_id = e.entity_id
                    WHERE e.industry IS NOT NULL
                    GROUP BY e.industry
                """)
                stats['events_by_industry'] = {row['industry']: row['count'] for row in cursor.fetchall()}

            except sqlite3.Error as e:
                print(f"Error generating statistics: {e}")

            return stats

    def close(self):
        if self._conn:
            with self._lock:
                self._conn.close()
                self._conn = None