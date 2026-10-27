"""
CyberEventData V2 - Separated Raw and Enriched Event Management

This module provides thread-safe access to the Version 2 database schema
with separated raw and enriched events.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


#: Columns added to EnrichedEvents to track Perplexity enrichment attempts.
#: Timestamps are TEXT, not TIMESTAMP: the connection uses PARSE_DECLTYPES,
#: whose timestamp converter rejects the ISO 'T' separator we write.
ENRICHMENT_TRACKING_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("perplexity_attempt_count", "INTEGER NOT NULL DEFAULT 0"),
    ("perplexity_last_attempt_at", "TEXT"),
    ("perplexity_last_error", "TEXT"),
    # NULL (never attempted) | 'retrying' | 'validated' | 'quarantined'
    ("perplexity_status", "TEXT"),
)

STATUS_RETRYING = "retrying"
STATUS_VALIDATED = "validated"
STATUS_QUARANTINED = "quarantined"

# Columns an accepted enrichment may promote; guards the dynamic UPDATE.
_PROMOTABLE_COLUMNS = frozenset({
    "event_date", "date_confidence", "entity_confidence",
    "attacking_entity_name", "attack_method", "records_affected",
})


class CyberEventDataV2:
    """
    Thread-safe library for managing cyber event data with separated raw and enriched schemas.
    """

    def __init__(self, db_path: str | Path = "instance/cyber_events.db"):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        self._logger = logging.getLogger(self.__class__.__name__)
        self._connect()
        self._ensure_v2_schema()

    def _connect(self):
        try:
            self._conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                timeout=30,
                detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON;")
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA busy_timeout = 30000;")
        except sqlite3.Error as e:
            self._logger.error("Database connection error: %s", e)
            raise

    def _ensure_v2_schema(self):
        """Ensure V2 schema tables exist"""
        with self._lock:
            cursor = self._conn.cursor()
            # Check if V2 tables exist
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='RawEvents'")
            if not cursor.fetchone():
                self._logger.error("Database schema not found in %s. Run: python pipeline.py init --db-path %s",
                                   self._db_path, self._db_path)
                raise RuntimeError(f"Database schema not found in {self._db_path}. "
                                   f"Run: python pipeline.py init --db-path {self._db_path}")
            self._ensure_enrichment_tracking_columns(cursor)

    def _ensure_enrichment_tracking_columns(self, cursor: sqlite3.Cursor) -> None:
        """Add the Perplexity attempt-tracking columns if they are missing.

        Idempotent: checks ``PRAGMA table_info`` first, so it is safe on every
        connect and on a database that already has them.  A database without an
        EnrichedEvents table (a bare test schema) is left alone.
        """
        cursor.execute("PRAGMA table_info(EnrichedEvents)")
        existing = {row[1] for row in cursor.fetchall()}
        if not existing:
            return
        added = False
        for name, ddl in ENRICHMENT_TRACKING_COLUMNS:
            if name not in existing:
                cursor.execute(f"ALTER TABLE EnrichedEvents ADD COLUMN {name} {ddl}")
                added = True
        if added:
            self._conn.commit()
            self._logger.info("Added Perplexity enrichment tracking columns to EnrichedEvents")

    # =========================================================================
    # RAW EVENT OPERATIONS
    # =========================================================================

    def add_raw_event(self, source_type: str, raw_data: Dict[str, Any]) -> str:
        """
        Add a new raw event to the database.

        Args:
            source_type: Type of source ('GDELT', 'Perplexity', 'GoogleSearch', 'WebberInsurance')
            raw_data: Dictionary containing raw event data

        Returns:
            The raw_event_id of the created event
        """
        if not self._conn:
            raise ConnectionError("Database not connected")

        raw_event_id = str(uuid.uuid4())

        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute("""
                    INSERT INTO RawEvents (
                        raw_event_id, source_type, source_event_id, raw_title,
                        raw_description, raw_content, event_date, source_url,
                        source_metadata, discovered_at, is_processed
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    raw_event_id,
                    source_type,
                    raw_data.get('source_event_id'),
                    raw_data.get('title'),
                    raw_data.get('description'),
                    raw_data.get('content'),
                    raw_data.get('event_date'),
                    raw_data.get('source_url'),
                    json.dumps(raw_data.get('metadata', {}), default=self._json_default),
                    datetime.now().isoformat(),
                    False
                ))
                self._conn.commit()
                return raw_event_id
            except sqlite3.Error as e:
                self._logger.error("Error adding raw event: %s", e)
                self._conn.rollback()
                raise

    def find_existing_raw_event(self, source_type: str, source_url: str, title: str) -> Optional[str]:
        """
        Check if a raw event with the same source_url and title already exists.

        Args:
            source_type: Type of source
            source_url: URL of the source
            title: Title of the event

        Returns:
            The raw_event_id if found, None otherwise
        """
        if not self._conn:
            raise ConnectionError("Database not connected")

        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute("""
                    SELECT raw_event_id FROM RawEvents
                    WHERE source_type = ? AND source_url = ? AND raw_title = ?
                    LIMIT 1
                """, (source_type, source_url, title))

                result = cursor.fetchone()
                return result['raw_event_id'] if result else None
            except sqlite3.Error as e:
                self._logger.error("Error checking for existing raw event: %s", e)
                return None

    def get_known_source_urls(self, source_type: str) -> Set[str]:
        """Return all source_url values already stored for a given source type."""
        if not self._conn:
            raise ConnectionError("Database not connected")

        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute(
                    "SELECT source_url FROM RawEvents WHERE source_type = ? AND source_url IS NOT NULL",
                    (source_type,),
                )
                return {row["source_url"] for row in cursor.fetchall()}
            except sqlite3.Error as e:
                self._logger.error("Error fetching known source URLs: %s", e)
                return set()

    def get_unprocessed_raw_events(self, source_types: List[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Get raw events that haven't been processed yet.

        Args:
            source_types: List of source types to filter by, or None for all
            limit: Maximum number of events to return

        Returns:
            List of raw event dictionaries
        """
        if not self._conn:
            raise ConnectionError("Database not connected")

        if limit is not None and limit <= 0:
            limit = None

        with self._lock:
            cursor = self._conn.cursor()

            if source_types:
                placeholders = ','.join('?' * len(source_types))
                query = f"""
                    SELECT * FROM RawEvents
                    WHERE is_processed = FALSE AND source_type IN ({placeholders})
                    ORDER BY discovered_at ASC
                """
                params = list(source_types)
            else:
                query = """
                    SELECT * FROM RawEvents
                    WHERE is_processed = FALSE
                    ORDER BY discovered_at ASC
                """
                params = []

            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)

            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def get_raw_events_for_processing(self, australian_only: bool = True, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Get raw events that need processing for enrichment.
        Filters for events that are likely Australian and about actual cyber events.

        Args:
            australian_only: If True, only return events likely to be Australian
            limit: Maximum number of events to return

        Returns:
            List of raw event dictionaries suitable for enrichment
        """
        if not self._conn:
            raise ConnectionError("Database not connected")

        if limit is not None and limit <= 0:
            limit = None

        with self._lock:
            cursor = self._conn.cursor()

            # Build query to find events likely to be Australian cyber events
            query = """
                SELECT re.* FROM RawEvents re
                LEFT JOIN ProcessingLog pl ON re.raw_event_id = pl.raw_event_id
                    AND pl.processing_stage = 'llm_analysis' AND pl.status = 'success'
                WHERE re.is_processed = FALSE
                    AND pl.log_id IS NULL  -- Not already analyzed
                    AND re.source_url IS NOT NULL  -- Has URL for scraping
            """

            params = []

            if australian_only:
                query += """
                    AND (
                        LOWER(re.raw_title) LIKE '%australia%' OR
                        LOWER(re.raw_title) LIKE '%australian%' OR
                        LOWER(COALESCE(re.raw_description,'')) LIKE '%australia%' OR
                        LOWER(COALESCE(re.raw_description,'')) LIKE '%australian%' OR
                        re.source_url LIKE '%.com.au%' OR
                        re.source_url LIKE '%.gov.au%' OR
                        re.source_url LIKE '%.edu.au%' OR
                        re.source_url LIKE '%.org.au%' OR
                        re.source_url LIKE '%.net.au%' OR
                        re.source_url LIKE '%/au/%'
                    )
                """

            query += " ORDER BY re.discovered_at ASC"
            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)

            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def mark_raw_event_processed(self, raw_event_id: str, error_message: str = None):
        """Mark a raw event as processed"""
        if not self._conn:
            raise ConnectionError("Database not connected")

        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute("""
                    UPDATE RawEvents
                    SET is_processed = TRUE, processing_attempted_at = ?, processing_error = ?
                    WHERE raw_event_id = ?
                """, (datetime.now().isoformat(), error_message, raw_event_id))
                self._conn.commit()
            except sqlite3.Error as e:
                self._logger.error("Error marking raw event as processed: %s", e)
                self._conn.rollback()

    # =========================================================================
    # ENRICHED EVENT OPERATIONS
    # =========================================================================

    def create_enriched_event(self, raw_event_id: str, enriched_data: Dict[str, Any]) -> str:
        """
        Create an enriched event from a raw event.

        Args:
            raw_event_id: ID of the source raw event
            enriched_data: Dictionary containing enriched event data

        Returns:
            The enriched_event_id of the created event
        """
        if not self._conn:
            raise ConnectionError("Database not connected")

        with self._lock:
            cursor = self._conn.cursor()

            # Return existing enriched event if one already exists for this raw event
            try:
                cursor.execute(
                    "SELECT enriched_event_id FROM EnrichedEvents "
                    "WHERE raw_event_id = ? AND status = 'Active' LIMIT 1",
                    (raw_event_id,),
                )
                existing = cursor.fetchone()
                if existing:
                    self._logger.debug(
                        "Enriched event already exists for raw_event_id %s", raw_event_id
                    )
                    return existing["enriched_event_id"]
            except sqlite3.Error:
                pass  # Fall through to insert

        enriched_event_id = str(uuid.uuid4())

        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute("""
                    INSERT INTO EnrichedEvents (
                        enriched_event_id, raw_event_id, title, description, summary,
                        event_type, severity, event_date, records_affected,
                        is_australian_event, is_specific_event, confidence_score,
                        australian_relevance_score, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    enriched_event_id,
                    raw_event_id,
                    enriched_data.get('title'),
                    enriched_data.get('description'),
                    enriched_data.get('summary'),
                    enriched_data.get('event_type'),
                    enriched_data.get('severity'),
                    enriched_data.get('event_date'),  # Can now be None/NULL
                    enriched_data.get('records_affected'),
                    enriched_data.get('is_australian_event', False),
                    enriched_data.get('is_specific_event', False),
                    enriched_data.get('confidence_score', 0.0),
                    enriched_data.get('australian_relevance_score', 0.0),
                    enriched_data.get('status', 'Active'),
                    datetime.now().isoformat(),
                    datetime.now().isoformat()
                ))

                # Add entities if provided
                if enriched_data.get('entities'):
                    self._link_entities_to_enriched_event(enriched_event_id, enriched_data['entities'])

                self._conn.commit()
                return enriched_event_id
            except sqlite3.Error as e:
                self._logger.error("Error creating enriched event: %s", e)
                self._conn.rollback()
                raise

    # =========================================================================
    # PERPLEXITY ENRICHMENT ATTEMPTS
    # =========================================================================

    def get_enrichment_row(self, enriched_event_id: str) -> Optional[Dict[str, Any]]:
        """Return the enrichment-related columns of one EnrichedEvents row."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT enriched_event_id, CAST(event_date AS TEXT) AS event_date, date_confidence, entity_confidence, "
                "attacking_entity_name, attack_method, records_affected, perplexity_validated, "
                "perplexity_enrichment_data, data_source_reliability, perplexity_attempt_count, "
                "perplexity_last_attempt_at, perplexity_last_error, perplexity_status "
                "FROM EnrichedEvents WHERE enriched_event_id = ?",
                (enriched_event_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def record_enrichment_success(
        self,
        enriched_event_id: str,
        gate: Any,
        min_apply_confidence: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Store an enrichment that passed the acceptance gate.

        The stored row is read and updated inside one lock so the "never
        replace stronger information" decision is made against current state.

        Args:
            enriched_event_id: Row to update.
            gate: An accepted ``GateResult`` (see processing.enrichment_gate).
            min_apply_confidence: Confidence needed to promote a fact to a
                typed column.

        Returns:
            ``{'applied': [...], 'reliability': float | None}``.

        Raises:
            ValueError: If the gate result is not accepted or the row is missing.
        """
        from cyber_data_collector.processing.enrichment_gate import (
            MIN_APPLY_CONFIDENCE,
            plan_enrichment_update,
        )

        threshold = MIN_APPLY_CONFIDENCE if min_apply_confidence is None else min_apply_confidence
        now = datetime.now().isoformat()
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT date_confidence, entity_confidence, attacking_entity_name, "
                "attack_method, records_affected, perplexity_enrichment_data, "
                "data_source_reliability FROM EnrichedEvents WHERE enriched_event_id = ?",
                (enriched_event_id,),
            )
            current = cursor.fetchone()
            if current is None:
                raise ValueError(f"EnrichedEvents row {enriched_event_id} not found")

            plan = plan_enrichment_update(dict(current), gate, threshold)
            assignments = ["perplexity_enrichment_data = ?", "data_source_reliability = ?",
                           "perplexity_validated = ?", "perplexity_validated_at = ?",
                           "perplexity_attempt_count = COALESCE(perplexity_attempt_count, 0) + 1",
                           "perplexity_last_attempt_at = ?", "perplexity_last_error = NULL",
                           "perplexity_status = ?", "updated_at = ?"]
            values: List[Any] = [
                json.dumps(plan.merged, default=str), plan.reliability,
                True, now, now, STATUS_VALIDATED, now,
            ]
            for column, value in plan.columns.items():
                if column not in _PROMOTABLE_COLUMNS:
                    raise ValueError(f"refusing to write unexpected column {column!r}")
                assignments.append(f"{column} = ?")
                values.append(value)
            values.append(enriched_event_id)
            try:
                cursor.execute(
                    f"UPDATE EnrichedEvents SET {', '.join(assignments)} WHERE enriched_event_id = ?",
                    values,
                )
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise
        return {"applied": plan.applied, "reliability": plan.reliability}

    def record_enrichment_failure(
        self,
        enriched_event_id: str,
        error: str,
        max_attempts: Optional[int] = None,
    ) -> str:
        """Record a failed Perplexity attempt; quarantine after *max_attempts*.

        Touches only the attempt-tracking columns.  In particular it never sets
        ``perplexity_validated`` or ``data_source_reliability``, and never
        writes the failed payload over a stored enrichment.

        Returns:
            The new status: ``'retrying'`` or ``'quarantined'``.
        """
        from cyber_data_collector.processing.enrichment_gate import MAX_ENRICHMENT_ATTEMPTS

        limit = MAX_ENRICHMENT_ATTEMPTS if max_attempts is None else max_attempts
        now = datetime.now().isoformat()
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT COALESCE(perplexity_attempt_count, 0) FROM EnrichedEvents "
                "WHERE enriched_event_id = ?",
                (enriched_event_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValueError(f"EnrichedEvents row {enriched_event_id} not found")
            attempts = int(row[0]) + 1
            status = STATUS_QUARANTINED if attempts >= limit else STATUS_RETRYING
            try:
                cursor.execute(
                    "UPDATE EnrichedEvents SET perplexity_attempt_count = ?, "
                    "perplexity_last_attempt_at = ?, perplexity_last_error = ?, "
                    "perplexity_status = ?, updated_at = ? WHERE enriched_event_id = ?",
                    (attempts, now, (error or "unknown error")[:500], status, now, enriched_event_id),
                )
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise
        if status == STATUS_QUARANTINED:
            self._logger.warning(
                "Enrichment quarantined after %d failed attempts: %s (%s)",
                attempts, enriched_event_id, (error or "")[:120],
            )
        return status

    def get_quarantined_enrichments(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """List records that exhausted their enrichment attempts."""
        query = (
            "SELECT enriched_event_id, title, perplexity_attempt_count, "
            "perplexity_last_attempt_at, perplexity_last_error FROM EnrichedEvents "
            "WHERE perplexity_status = ? ORDER BY perplexity_last_attempt_at DESC"
        )
        params: List[Any] = [STATUS_QUARANTINED]
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def release_quarantined_enrichments(self) -> int:
        """Put quarantined records back in the retry queue (attempts reset)."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "UPDATE EnrichedEvents SET perplexity_status = NULL, perplexity_attempt_count = 0 "
                "WHERE perplexity_status = ?",
                (STATUS_QUARANTINED,),
            )
            self._conn.commit()
            return cursor.rowcount

    def requeue_empty_validations(self) -> int:
        """Un-validate records whose stored "enrichment" contains no facts.

        Before the acceptance gate, an empty or unparseable Perplexity answer
        was stored as validated with a constant reliability of 0.85, which
        removed the record from the retry queue for good.  This clears that
        state so the record is enriched again.  Idempotent: a second call finds
        nothing.

        Returns:
            Number of records re-queued.
        """
        fact_keys = ("earliest_event_date", "formal_entity_name", "threat_actor",
                     "attack_method", "victim_count", "victim_industry")
        requeued = 0
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT enriched_event_id, perplexity_enrichment_data FROM EnrichedEvents "
                "WHERE perplexity_validated = 1"
            )
            for row in cursor.fetchall():
                try:
                    data = json.loads(row["perplexity_enrichment_data"]) if row["perplexity_enrichment_data"] else {}
                except (TypeError, ValueError):
                    data = {}
                if isinstance(data, dict) and any(data.get(key) not in (None, "") for key in fact_keys):
                    continue
                cursor.execute(
                    "UPDATE EnrichedEvents SET perplexity_validated = 0, perplexity_validated_at = NULL, "
                    "perplexity_enrichment_data = NULL, data_source_reliability = NULL, "
                    "perplexity_status = NULL, perplexity_attempt_count = 0, perplexity_last_error = NULL "
                    "WHERE enriched_event_id = ?",
                    (row["enriched_event_id"],),
                )
                requeued += 1
            self._conn.commit()
        return requeued

    def _link_entities_to_enriched_event(self, enriched_event_id: str, entities: List[Dict[str, Any]]):
        """Link entities to an enriched event"""
        cursor = self._conn.cursor()

        for entity_data in entities:
            entity_name = entity_data.get('name')
            if not entity_name:
                continue

            # Insert or get entity
            cursor.execute("""
                INSERT OR IGNORE INTO EntitiesV2 (entity_name, entity_type, is_australian, confidence_score, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                entity_name,
                entity_data.get('type'),
                entity_data.get('is_australian', False),
                entity_data.get('confidence_score', 0.0),
                datetime.now().isoformat()
            ))

            # Get entity ID
            cursor.execute("SELECT entity_id FROM EntitiesV2 WHERE entity_name = ?", (entity_name,))
            entity_id = cursor.fetchone()['entity_id']

            # Link to enriched event
            cursor.execute("""
                INSERT OR IGNORE INTO EnrichedEventEntities
                (enriched_event_id, entity_id, relationship_type, confidence_score)
                VALUES (?, ?, ?, ?)
            """, (
                enriched_event_id,
                entity_id,
                entity_data.get('relationship_type', 'affected'),
                entity_data.get('confidence_score', 0.0)
            ))

    def get_enriched_events(self, australian_only: bool = True, specific_only: bool = True, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Get enriched events with optional filters.

        Args:
            australian_only: Only return Australian events
            specific_only: Only return specific (not general news) events
            limit: Maximum number of events to return

        Returns:
            List of enriched event dictionaries
        """
        if not self._conn:
            raise ConnectionError("Database not connected")

        with self._lock:
            cursor = self._conn.cursor()

            query = "SELECT * FROM EnrichedEvents WHERE status = 'Active'"
            params = []

            if australian_only:
                query += " AND is_australian_event = TRUE"

            if specific_only:
                query += " AND is_specific_event = TRUE"

            query += " ORDER BY event_date DESC, created_at DESC LIMIT ?"
            params.append(limit)

            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    # =========================================================================
    # PROCESSING LOG OPERATIONS
    # =========================================================================

    def log_processing_attempt(self, raw_event_id: str, stage: str, status: str,
                             result_data: Dict[str, Any] = None, error_message: str = None,
                             processing_time_ms: int = None) -> str:
        """
        Log a processing attempt.

        Args:
            raw_event_id: ID of the raw event being processed
            stage: Processing stage ('url_scraping', 'llm_analysis', 'entity_extraction')
            status: Status ('success', 'failed', 'skipped')
            result_data: Additional result data
            error_message: Error message if failed
            processing_time_ms: Time taken in milliseconds

        Returns:
            The log_id of the created log entry
        """
        if not self._conn:
            raise ConnectionError("Database not connected")

        log_id = str(uuid.uuid4())

        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute("""
                    INSERT INTO ProcessingLog (
                        log_id, raw_event_id, processing_stage, status,
                        result_data, error_message, processing_time_ms, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    log_id,
                    raw_event_id,
                    stage,
                    status,
                    json.dumps(result_data, default=self._json_default) if result_data else None,
                    error_message,
                    processing_time_ms,
                    datetime.now().isoformat()
                ))
                self._conn.commit()
                return log_id
            except sqlite3.Error as e:
                self._logger.error("Error logging processing attempt: %s", e)
                self._conn.rollback()
                raise

    @staticmethod
    def _json_default(obj: Any) -> Any:
        """JSON serializer for objects not serializable by default json code"""
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, set):
            return list(obj)
        return str(obj)

    # =========================================================================
    # STATISTICS AND REPORTING
    # =========================================================================

    def get_summary_statistics(self) -> Dict[str, Any]:
        """Get comprehensive database statistics"""
        if not self._conn:
            raise ConnectionError("Database not connected")

        with self._lock:
            cursor = self._conn.cursor()

            stats = {}

            # Raw events stats
            cursor.execute("SELECT COUNT(*) as total FROM RawEvents")
            stats['raw_events_total'] = cursor.fetchone()['total']

            cursor.execute("SELECT COUNT(*) as processed FROM RawEvents WHERE is_processed = TRUE")
            stats['raw_events_processed'] = cursor.fetchone()['processed']

            cursor.execute("SELECT source_type, COUNT(*) as count FROM RawEvents GROUP BY source_type")
            stats['raw_events_by_source'] = {row['source_type']: row['count'] for row in cursor.fetchall()}

            # Enriched events stats
            cursor.execute("SELECT COUNT(*) as total FROM EnrichedEvents WHERE status = 'Active'")
            stats['enriched_events_total'] = cursor.fetchone()['total']

            cursor.execute("SELECT COUNT(*) as australian FROM EnrichedEvents WHERE is_australian_event = TRUE AND status = 'Active'")
            stats['enriched_events_australian'] = cursor.fetchone()['australian']

            cursor.execute("SELECT COUNT(*) as specific FROM EnrichedEvents WHERE is_specific_event = TRUE AND status = 'Active'")
            stats['enriched_events_specific'] = cursor.fetchone()['specific']

            cursor.execute("""
                SELECT event_type, COUNT(*) as count
                FROM EnrichedEvents
                WHERE status = 'Active' AND event_type IS NOT NULL
                GROUP BY event_type
            """)
            stats['events_by_type'] = {row['event_type']: row['count'] for row in cursor.fetchall()}

            # Entity stats
            cursor.execute("SELECT COUNT(*) as total FROM EntitiesV2")
            stats['entities_total'] = cursor.fetchone()['total']

            cursor.execute("SELECT COUNT(*) as australian FROM EntitiesV2 WHERE is_australian = TRUE")
            stats['entities_australian'] = cursor.fetchone()['australian']

            # Processing stats
            cursor.execute("""
                SELECT processing_stage, status, COUNT(*) as count
                FROM ProcessingLog
                GROUP BY processing_stage, status
            """)
            processing_stats = {}
            for row in cursor.fetchall():
                stage = row['processing_stage']
                if stage not in processing_stats:
                    processing_stats[stage] = {}
                processing_stats[stage][row['status']] = row['count']
            stats['processing_stats'] = processing_stats

            return stats

    def get_processing_queue_status(self) -> Dict[str, int]:
        """Get current processing queue status"""
        if not self._conn:
            raise ConnectionError("Database not connected")

        with self._lock:
            cursor = self._conn.cursor()

            # Count events ready for different processing stages
            cursor.execute("SELECT COUNT(*) as count FROM RawEvents WHERE is_processed = FALSE")
            unprocessed = cursor.fetchone()['count']

            cursor.execute("""
                SELECT COUNT(*) as count FROM RawEvents re
                WHERE re.is_processed = FALSE
                    AND re.source_url IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM ProcessingLog pl
                        WHERE pl.raw_event_id = re.raw_event_id
                            AND pl.processing_stage = 'url_scraping'
                            AND pl.status = 'success'
                    )
            """)
            needs_scraping = cursor.fetchone()['count']

            cursor.execute("""
                SELECT COUNT(*) as count FROM RawEvents re
                WHERE re.is_processed = FALSE
                    AND EXISTS (
                        SELECT 1 FROM ProcessingLog pl
                        WHERE pl.raw_event_id = re.raw_event_id
                            AND pl.processing_stage = 'url_scraping'
                            AND pl.status = 'success'
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM ProcessingLog pl2
                        WHERE pl2.raw_event_id = re.raw_event_id
                            AND pl2.processing_stage = 'llm_analysis'
                            AND pl2.status = 'success'
                    )
            """)
            needs_analysis = cursor.fetchone()['count']

            return {
                'unprocessed_total': unprocessed,
                'needs_scraping': needs_scraping,
                'needs_analysis': needs_analysis,
                'ready_for_enrichment': needs_analysis
            }

    # =========================================================================
    # MONTH PROCESSING TRACKING METHODS
    # =========================================================================

    def is_month_processed(self, year: int, month: int) -> bool:
        """Check if a specific month has been processed"""
        if not self._conn:
            raise ConnectionError("Database not connected")

        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("""
                SELECT is_processed FROM MonthProcessed
                WHERE year = ? AND month = ?
            """, (year, month))
            result = cursor.fetchone()
            return result['is_processed'] if result else False

    def mark_month_as_processed(self, year: int, month: int,
                               total_raw_events: int = 0,
                               total_enriched_events: int = 0,
                               processing_notes: str = None) -> bool:
        """Mark a month as processed with statistics"""
        if not self._conn:
            raise ConnectionError("Database not connected")

        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute("""
                    INSERT OR REPLACE INTO MonthProcessed
                    (year, month, is_processed, processed_at, total_raw_events,
                     total_enriched_events, processing_notes)
                    VALUES (?, ?, TRUE, ?, ?, ?, ?)
                """, (year, month, datetime.now().isoformat(),
                     total_raw_events, total_enriched_events, processing_notes))
                self._conn.commit()
                return True
            except sqlite3.Error as e:
                self._logger.error("Error marking month as processed: %s", e)
                self._conn.rollback()
                return False

    def get_unprocessed_months(self, start_year: int = 2020, start_month: int = 1,
                              end_year: int = 2025, end_month: int = 8) -> List[Tuple[int, int]]:
        """Get list of unprocessed months in the specified range"""
        if not self._conn:
            raise ConnectionError("Database not connected")

        # Generate all months in range
        all_months = []
        current_year, current_month = start_year, start_month

        while (current_year < end_year) or (current_year == end_year and current_month <= end_month):
            all_months.append((current_year, current_month))
            current_month += 1
            if current_month > 12:
                current_month = 1
                current_year += 1

        # Check which ones are processed
        unprocessed = []
        with self._lock:
            cursor = self._conn.cursor()
            for year, month in all_months:
                cursor.execute("""
                    SELECT is_processed FROM MonthProcessed
                    WHERE year = ? AND month = ?
                """, (year, month))
                result = cursor.fetchone()
                if not result or not result['is_processed']:
                    unprocessed.append((year, month))

        return unprocessed

    def get_month_processing_stats(self) -> Dict[str, Any]:
        """Get statistics about month processing"""
        if not self._conn:
            raise ConnectionError("Database not connected")

        with self._lock:
            cursor = self._conn.cursor()

            # Count processed months
            cursor.execute("SELECT COUNT(*) as count FROM MonthProcessed WHERE is_processed = TRUE")
            processed_count = cursor.fetchone()['count']

            # Count total months in range (Jan 2020 - Aug 2025)
            total_months = ((2025 - 2020) * 12) + 8 - 1 + 1  # 68 months total

            # Get processing stats by year
            cursor.execute("""
                SELECT year, COUNT(*) as processed_months,
                       SUM(total_raw_events) as total_raw,
                       SUM(total_enriched_events) as total_enriched
                FROM MonthProcessed
                WHERE is_processed = TRUE
                GROUP BY year
                ORDER BY year
            """)
            by_year = [dict(row) for row in cursor.fetchall()]

            return {
                'processed_months': processed_count,
                'total_months_in_range': total_months,
                'remaining_months': total_months - processed_count,
                'completion_percentage': (processed_count / total_months * 100) if total_months > 0 else 0,
                'by_year': by_year
            }

    # =========================================================================
    # UTILITY METHODS
    # =========================================================================

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the underlying database connection."""
        return self._conn

    def close(self):
        """Close database connection"""
        if self._conn:
            with self._lock:
                self._conn.close()
                self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
