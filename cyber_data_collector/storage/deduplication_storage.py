"""
Database storage layer for deduplication with comprehensive lineage tracking.

This module handles all database operations for the new deduplication system:
- Transaction-safe storage of deduplication results
- Merge lineage tracking for transparency
- Validation of database integrity
- Idempotent operations
"""

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple, Any
from datetime import datetime
import json

from ..processing.deduplication_v2 import DeduplicationResult, MergeGroup, ValidationError
# Import the simplified CyberEvent from deduplication_v2
from ..processing.deduplication_v2 import CyberEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StorageResult:
    """Result of storage operation with validation status"""
    success: bool
    stored_events: int
    merge_groups_created: int
    validation_errors: List[ValidationError]
    processing_time_seconds: float
    storage_timestamp: datetime


class DeduplicationStorage:
    """Handles all database operations for deduplication with lineage tracking"""
    
    def __init__(self, db_connection: sqlite3.Connection):
        self.conn = db_connection
        self.logger = logging.getLogger(f"{__name__}.DeduplicationStorage")
        self._validate_schema()
    
    def _validate_schema(self) -> None:
        """Validate that required tables exist and have correct schema"""
        cursor = self.conn.cursor()
        
        # Check for required tables
        required_tables = [
            'DeduplicatedEvents',
            'EventDeduplicationMap', 
            'DeduplicationClusters'
        ]
        
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name IN ({})
        """.format(','.join('?' * len(required_tables))), required_tables)
        
        existing_tables = [row[0] for row in cursor.fetchall()]
        missing_tables = [t for t in required_tables if t not in existing_tables]
        
        if missing_tables:
            self.logger.warning(f"Missing tables: {missing_tables}")
            # Create missing tables if needed
            self._create_missing_tables(missing_tables)
    
    def _create_missing_tables(self, missing_tables: List[str]) -> None:
        """Create missing tables with proper schema"""
        cursor = self.conn.cursor()
        
        if 'DeduplicationClusters' in missing_tables:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS DeduplicationClusters (
                    cluster_id TEXT PRIMARY KEY,
                    master_event_id TEXT NOT NULL,
                    merge_timestamp DATETIME NOT NULL,
                    merge_reason TEXT,
                    confidence REAL,
                    similarity_scores TEXT,  -- JSON
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
        
        if 'EventDeduplicationMap' in missing_tables:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS EventDeduplicationMap (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    deduplicated_event_id TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    similarity_score REAL,
                    merge_reason TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (deduplicated_event_id) REFERENCES DeduplicatedEvents(deduplicated_event_id)
                )
            """)
        
        self.conn.commit()
        self.logger.info(f"Created missing tables: {missing_tables}")
    
    def clear_existing_deduplications(self) -> None:
        """Remove all existing deduplicated events (idempotent)"""
        cursor = self.conn.cursor()
        
        try:
            # Delete in correct order to respect foreign keys
            cursor.execute("DELETE FROM EventDeduplicationMap")
            cursor.execute("DELETE FROM DeduplicationClusters")
            cursor.execute("DELETE FROM DeduplicatedEvents")
            
            self.conn.commit()
            self.logger.info("Cleared existing deduplications")
            
        except Exception as e:
            self.conn.rollback()
            self.logger.error(f"Failed to clear existing deduplications: {e}")
            raise
    
    def store_deduplication_result(self, result: DeduplicationResult) -> StorageResult:
        """Store complete deduplication result with lineage tracking"""
        start_time = datetime.now()
        validation_errors = []
        
        try:
            # Start transaction
            cursor = self.conn.cursor()
            
            # Validate no duplicates before storing
            pre_validation = self._validate_no_duplicates_before_store(result.unique_events)
            if pre_validation:
                validation_errors.extend(pre_validation)
                return StorageResult(
                    success=False,
                    stored_events=0,
                    merge_groups_created=0,
                    validation_errors=validation_errors,
                    processing_time_seconds=0.0,
                    storage_timestamp=datetime.now()
                )
            
            # Store unique events
            stored_events = self._store_unique_events(cursor, result.unique_events)
            
            # Store merge groups and lineage
            merge_groups_created = self._store_merge_groups(cursor, result.merge_groups)
            
            # Commit transaction
            self.conn.commit()
            
            # Post-storage validation
            post_validation = self.validate_storage_integrity()
            if post_validation:
                validation_errors.extend(post_validation)
                self.logger.warning(f"Post-storage validation found issues: {len(post_validation)}")
            
            processing_time = (datetime.now() - start_time).total_seconds()
            
            self.logger.info(f"Stored {stored_events} events and {merge_groups_created} merge groups")
            
            return StorageResult(
                success=True,
                stored_events=stored_events,
                merge_groups_created=merge_groups_created,
                validation_errors=validation_errors,
                processing_time_seconds=processing_time,
                storage_timestamp=datetime.now()
            )
            
        except Exception as e:
            self.conn.rollback()
            self.logger.error(f"Failed to store deduplication result: {e}")
            raise
    
    def _validate_no_duplicates_before_store(self, events: List[CyberEvent]) -> List[ValidationError]:
        """Validate that no duplicate title+date combinations exist before storing"""
        errors = []
        
        # Check for duplicates in the input
        seen_combinations = set()
        for event in events:
            key = (event.title.lower().strip(), event.event_date)
            if key in seen_combinations:
                errors.append(ValidationError(
                    error_type="DUPLICATE_INPUT",
                    message=f"Duplicate event in input: {event.title} on {event.event_date}",
                    context={"title": event.title, "date": event.event_date}
                ))
            seen_combinations.add(key)
        
        return errors
    
    def _store_unique_events(self, cursor: sqlite3.Cursor, events: List[CyberEvent]) -> int:
        """Store unique events in DeduplicatedEvents table"""
        stored_count = 0
        
        for event in events:
            # Generate unique deduplicated event ID
            deduplicated_event_id = str(uuid.uuid4())
            
            # Prepare event data (match DeduplicatedEvents schema)
            event_data = {
                'deduplicated_event_id': deduplicated_event_id,
                'master_enriched_event_id': event.event_id,  # Link back to source event
                'title': event.title,
                'summary': event.summary,
                'event_date': event.event_date,
                'event_type': event.event_type,
                'severity': event.severity,
                'records_affected': event.records_affected,
                'is_australian_event': True,  # All events are Australian
                'is_specific_event': True,  # Deduplicated events are specific
                'confidence_score': event.confidence if hasattr(event, 'confidence') else 0.5,
                'status': 'Active',
                'created_at': datetime.now(),
                'updated_at': datetime.now()
            }

            # Insert event
            cursor.execute("""
                INSERT INTO DeduplicatedEvents (
                    deduplicated_event_id, master_enriched_event_id, title, summary,
                    event_date, event_type, severity, records_affected,
                    is_australian_event, is_specific_event, confidence_score,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event_data['deduplicated_event_id'],
                event_data['master_enriched_event_id'],
                event_data['title'],
                event_data['summary'],
                event_data['event_date'],
                event_data['event_type'],
                event_data['severity'],
                event_data['records_affected'],
                event_data['is_australian_event'],
                event_data['is_specific_event'],
                event_data['confidence_score'],
                event_data['status'],
                event_data['created_at'],
                event_data['updated_at']
            ))
            
            stored_count += 1
        
        return stored_count
    
    def _store_merge_groups(self, cursor: sqlite3.Cursor, merge_groups: List[MergeGroup]) -> int:
        """Store merge groups and create lineage tracking"""
        stored_count = 0

        for group in merge_groups:
            # Skip groups with no merges (single events)
            if len(group.merged_events) == 0:
                continue

            # Create cluster record (match actual schema)
            cluster_id = str(uuid.uuid4())
            cluster_size = len(group.merged_events) + 1  # Include master event
            avg_similarity = sum(group.similarity_scores.values()) / len(group.similarity_scores) if group.similarity_scores else 1.0

            cursor.execute("""
                INSERT INTO DeduplicationClusters (
                    cluster_id, deduplicated_event_id, cluster_size,
                    average_similarity, deduplication_timestamp, algorithm_version
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                cluster_id,
                group.master_event.event_id,
                cluster_size,
                avg_similarity,
                group.merge_timestamp,
                'v2.0'
            ))

            # Create mapping records for each merged event
            for merged_event in group.merged_events:
                similarity_score = group.similarity_scores.get(merged_event.event_id, 0.0)
                map_id = str(uuid.uuid4())

                cursor.execute("""
                    INSERT INTO EventDeduplicationMap (
                        map_id, raw_event_id, enriched_event_id, deduplicated_event_id,
                        contribution_type, similarity_score, data_source_weight
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    map_id,
                    merged_event.event_id,  # Source enriched event
                    merged_event.event_id,  # Same as enriched
                    group.master_event.event_id,  # Master deduplicated event
                    'merged',  # This is a merged duplicate
                    similarity_score,
                    1.0  # Default weight
                ))

            stored_count += 1

        return stored_count
    
    def validate_storage_integrity(self) -> List[ValidationError]:
        """Check database for duplicate deduplicated_event_ids and other integrity issues"""
        errors = []
        cursor = self.conn.cursor()
        
        # Check if tables exist first
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name IN ('DeduplicatedEvents', 'EventDeduplicationMap', 'DeduplicationClusters')
        """)
        existing_tables = [row[0] for row in cursor.fetchall()]
        
        if 'DeduplicatedEvents' not in existing_tables:
            self.logger.info("DeduplicatedEvents table does not exist yet - skipping integrity checks")
            return errors
        
        # Check for duplicate deduplicated_event_ids
        cursor.execute("""
            SELECT deduplicated_event_id, COUNT(*) as count
            FROM DeduplicatedEvents
            GROUP BY deduplicated_event_id
            HAVING COUNT(*) > 1
        """)
        duplicate_ids = cursor.fetchall()
        
        for dedup_id, count in duplicate_ids:
            errors.append(ValidationError(
                error_type="DUPLICATE_DEDUP_ID",
                message=f"Duplicate deduplicated_event_id found: {dedup_id} ({count} times",
                context={"dedup_id": dedup_id, "count": count}
            ))
        
        # Check for duplicate title+date combinations
        cursor.execute("""
            SELECT title, event_date, COUNT(*) as count
            FROM DeduplicatedEvents
            WHERE status = 'Active'
            GROUP BY title, event_date
            HAVING COUNT(*) > 1
        """)
        duplicate_events = cursor.fetchall()
        
        for title, date, count in duplicate_events:
            errors.append(ValidationError(
                error_type="DUPLICATE_EVENT",
                message=f"Duplicate event found: '{title}' on {date} ({count} times)",
                context={"title": title, "date": date, "count": count}
            ))
        
        # Check for orphaned mapping records (only if table exists)
        if 'EventDeduplicationMap' in existing_tables:
            cursor.execute("""
                SELECT COUNT(*) FROM EventDeduplicationMap edm
                LEFT JOIN DeduplicatedEvents de ON edm.deduplicated_event_id = de.master_enriched_event_id
                WHERE de.master_enriched_event_id IS NULL
            """)
            orphaned_mappings = cursor.fetchone()[0]

            if orphaned_mappings > 0:
                errors.append(ValidationError(
                    error_type="ORPHANED_MAPPINGS",
                    message=f"Found {orphaned_mappings} orphaned mapping records",
                    context={"orphaned_count": orphaned_mappings}
                ))
        
        # Check for orphaned cluster records (only if table exists)
        if 'DeduplicationClusters' in existing_tables:
            cursor.execute("""
                SELECT COUNT(*) FROM DeduplicationClusters dc
                LEFT JOIN DeduplicatedEvents de ON dc.deduplicated_event_id = de.master_enriched_event_id
                WHERE de.master_enriched_event_id IS NULL
            """)
            orphaned_clusters = cursor.fetchone()[0]

            if orphaned_clusters > 0:
                errors.append(ValidationError(
                    error_type="ORPHANED_CLUSTERS",
                    message=f"Found {orphaned_clusters} orphaned cluster records",
                    context={"orphaned_count": orphaned_clusters}
                ))
        
        self.logger.info(f"Storage integrity check: {len(errors)} issues found")
        return errors
    
    def get_deduplication_statistics(self) -> Dict[str, Any]:
        """Get comprehensive statistics about the deduplication system"""
        cursor = self.conn.cursor()
        
        stats = {}
        
        # Check if tables exist first
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name IN ('DeduplicatedEvents', 'EventDeduplicationMap', 'DeduplicationClusters')
        """)
        existing_tables = [row[0] for row in cursor.fetchall()]
        
        # Basic counts (only if tables exist)
        if 'DeduplicatedEvents' in existing_tables:
            cursor.execute("SELECT COUNT(*) FROM DeduplicatedEvents WHERE status = 'Active'")
            stats['active_events'] = cursor.fetchone()[0]
        else:
            stats['active_events'] = 0
        
        if 'DeduplicationClusters' in existing_tables:
            cursor.execute("SELECT COUNT(*) FROM DeduplicationClusters")
            stats['merge_groups'] = cursor.fetchone()[0]
        else:
            stats['merge_groups'] = 0
        
        if 'EventDeduplicationMap' in existing_tables:
            cursor.execute("SELECT COUNT(*) FROM EventDeduplicationMap")
            stats['total_merges'] = cursor.fetchone()[0]
        else:
            stats['total_merges'] = 0
        
        # Date range (only if DeduplicatedEvents exists)
        if 'DeduplicatedEvents' in existing_tables:
            cursor.execute("""
                SELECT MIN(event_date), MAX(event_date)
                FROM DeduplicatedEvents
                WHERE status = 'Active'
            """)
            date_range = cursor.fetchone()
            stats['date_range'] = {
                'earliest': date_range[0],
                'latest': date_range[1]
            }
        else:
            stats['date_range'] = {'earliest': None, 'latest': None}
        
        # Event types (only if DeduplicatedEvents exists)
        if 'DeduplicatedEvents' in existing_tables:
            cursor.execute("""
                SELECT event_type, COUNT(*) as count
                FROM DeduplicatedEvents
                WHERE status = 'Active' AND event_type IS NOT NULL
                GROUP BY event_type
                ORDER BY count DESC
            """)
            stats['event_types'] = dict(cursor.fetchall())
        else:
            stats['event_types'] = {}
        
        # Severity distribution (only if DeduplicatedEvents exists)
        if 'DeduplicatedEvents' in existing_tables:
            cursor.execute("""
                SELECT severity, COUNT(*) as count
                FROM DeduplicatedEvents
                WHERE status = 'Active' AND severity IS NOT NULL
                GROUP BY severity
                ORDER BY count DESC
            """)
            stats['severity_distribution'] = dict(cursor.fetchall())
        else:
            stats['severity_distribution'] = {}
        
        return stats
    
    def get_merge_lineage(self, deduplicated_event_id: str) -> Optional[Dict[str, Any]]:
        """Get complete merge lineage for a deduplicated event"""
        cursor = self.conn.cursor()
        
        # Check if tables exist first
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name IN ('DeduplicationClusters', 'EventDeduplicationMap')
        """)
        existing_tables = [row[0] for row in cursor.fetchall()]
        
        if 'DeduplicationClusters' not in existing_tables:
            return None
        
        # Get the cluster information
        cursor.execute("""
            SELECT dc.cluster_id, dc.deduplicated_event_id, dc.deduplication_timestamp,
                   dc.cluster_size, dc.average_similarity, dc.algorithm_version
            FROM DeduplicationClusters dc
            WHERE dc.deduplicated_event_id = ?
        """, (deduplicated_event_id,))
        
        cluster_data = cursor.fetchone()
        if not cluster_data:
            return None
        
        # Get all source events that were merged (only if table exists)
        source_events = []
        if 'EventDeduplicationMap' in existing_tables:
            cursor.execute("""
                SELECT edm.enriched_event_id, edm.similarity_score, edm.contribution_type
                FROM EventDeduplicationMap edm
                WHERE edm.deduplicated_event_id = ?
            """, (deduplicated_event_id,))

            source_events = cursor.fetchall()

        return {
            'cluster_id': cluster_data[0],
            'deduplicated_event_id': cluster_data[1],
            'deduplication_timestamp': cluster_data[2],
            'cluster_size': cluster_data[3],
            'average_similarity': cluster_data[4],
            'algorithm_version': cluster_data[5],
            'source_events': [
                {
                    'enriched_event_id': row[0],
                    'similarity_score': row[1],
                    'contribution_type': row[2]
                }
                for row in source_events
            ]
        }
    
    def backup_deduplication_data(self, backup_path: str) -> bool:
        """Create a backup of all deduplication data"""
        try:
            cursor = self.conn.cursor()
            
            # Export to SQL file
            with open(backup_path, 'w') as f:
                f.write("-- Deduplication Data Backup\n")
                f.write(f"-- Created: {datetime.now()}\n\n")
                
                # Export DeduplicatedEvents
                f.write("-- DeduplicatedEvents\n")
                cursor.execute("SELECT * FROM DeduplicatedEvents")
                rows = cursor.fetchall()
                for row in rows:
                    f.write(f"INSERT INTO DeduplicatedEvents VALUES {row};\n")
                
                # Export DeduplicationClusters
                f.write("\n-- DeduplicationClusters\n")
                cursor.execute("SELECT * FROM DeduplicationClusters")
                rows = cursor.fetchall()
                for row in rows:
                    f.write(f"INSERT INTO DeduplicationClusters VALUES {row};\n")
                
                # Export EventDeduplicationMap
                f.write("\n-- EventDeduplicationMap\n")
                cursor.execute("SELECT * FROM EventDeduplicationMap")
                rows = cursor.fetchall()
                for row in rows:
                    f.write(f"INSERT INTO EventDeduplicationMap VALUES {row};\n")
            
            self.logger.info(f"Deduplication data backed up to {backup_path}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to backup deduplication data: {e}")
            return False
