#!/usr/bin/env python3
"""
Global deduplication script for cyber events.

This script runs the deduplication engine to merge duplicate events from
EnrichedEvents into a deduplicated set in DeduplicatedEvents.

This script:
1. Backs up the current database
2. Clears existing DeduplicatedEvents and ASDRiskClassifications
3. Loads all EnrichedEvents with Perplexity enrichment data
4. Runs global deduplication (merges events about same incident)
5. Stores deduplicated events with preserved victim organization info
6. Validates the results and generates a report

Usage:
    python scripts/run_global_deduplication.py [--db-path PATH] [--backup-path PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from tqdm import tqdm

# Add the project root to the Python path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.processing.deduplication_v2 import DeduplicationEngine, LLMArbiter, DeduplicationValidator
from cyber_data_collector.storage.deduplication_storage import DeduplicationStorage
from cyber_data_collector.processing.deduplication_v2 import CyberEvent

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def _parse_event_date(date_str: Optional[str]) -> Optional[object]:
    """Parse an event date string into a date object, handling partial formats."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str).date()
    except ValueError:
        try:
            if len(date_str) == 7 and date_str.count('-') == 1:
                return datetime.fromisoformat(date_str + '-01').date()
            elif len(date_str) == 4:
                return datetime.fromisoformat(date_str + '-01-01').date()
            else:
                logger.warning(f"Could not parse date '{date_str}'")
        except ValueError:
            logger.warning(f"Could not parse date '{date_str}'")
    return None


class DeduplicationMigration:
    """Handles migration from old to new deduplication system"""
    
    def __init__(self, db_path: str, backup_path: str = None, dry_run: bool = False, force: bool = False):
        self.db_path = db_path
        self.backup_path = backup_path or f"{db_path}.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.dry_run = dry_run
        self.force = force
        self.migration_report = {
            'start_time': datetime.now().isoformat(),
            'db_path': db_path,
            'backup_path': self.backup_path,
            'dry_run': dry_run,
            'steps_completed': [],
            'errors': [],
            'statistics': {}
        }
    
    def run_migration(self) -> bool:
        """Run deduplication: incremental if possible, full rebuild if required."""
        logger.info("🚀 Starting deduplication run...")

        try:
            # Step 1: Backup current data
            if not self._backup_current_data():
                return False

            # Step 2: Detect new events and decide mode
            new_event_ids, existing_dedup_exists = self._detect_new_events()

            if self.force or not existing_dedup_exists:
                # --- PATH A: Full rebuild ---
                mode = "forced full rebuild" if self.force else "initial full build"
                logger.info(f"🔄 Running {mode}...")

                if not self._apply_database_constraints():
                    return False

                enriched_events = self._load_enriched_events()
                if enriched_events is None:
                    return False

                if not self._run_global_deduplication(enriched_events):
                    return False

            elif len(new_event_ids) == 0:
                # --- PATH B: Nothing to do ---
                logger.info("✅ No new events since last deduplication run — skipping.")
                self.migration_report['steps_completed'].append('skipped_no_new_events')
                self.migration_report['statistics']['new_events_processed'] = 0

            else:
                # --- PATH C: Incremental ---
                logger.info(f"🔄 Incremental mode: {len(new_event_ids)} new events to process "
                            f"against existing deduplicated data.")

                new_events = self._load_enriched_events(event_ids=new_event_ids)
                if new_events is None:
                    return False

                existing_dedup_events, dedup_to_master = self._load_existing_deduplicated_events()
                if not self._run_incremental_deduplication(new_events, existing_dedup_events, dedup_to_master):
                    return False

            # Validate results (skip in dry-run or skip mode)
            if not self.dry_run and self.migration_report['steps_completed'] \
                    and 'skipped_no_new_events' not in self.migration_report['steps_completed']:
                if not self._validate_migration_results():
                    return False
            elif self.dry_run:
                logger.info("🔍 DRY RUN: Would validate migration results")

            # Generate report
            self._generate_migration_report()

            logger.info("✅ Deduplication completed successfully!")
            return True

        except Exception as e:
            logger.error(f"❌ Deduplication failed: {e}")
            self.migration_report['errors'].append(str(e))
            return False
    
    def _backup_current_data(self) -> bool:
        """Backup the current DeduplicatedEvents table"""
        logger.info("📦 Backing up current deduplication data...")
        
        try:
            # Create backup directory if it doesn't exist
            backup_dir = Path(self.backup_path).parent
            backup_dir.mkdir(parents=True, exist_ok=True)
            
            # Copy database file
            shutil.copy2(self.db_path, self.backup_path)
            logger.info(f"✅ Database backed up to: {self.backup_path}")
            
            # Also create SQL backup
            sql_backup_path = f"{self.backup_path}.sql"
            self._create_sql_backup(sql_backup_path)
            logger.info(f"✅ SQL backup created: {sql_backup_path}")
            
            self.migration_report['steps_completed'].append('backup_created')
            return True
            
        except Exception as e:
            logger.error(f"❌ Backup failed: {e}")
            self.migration_report['errors'].append(f"Backup failed: {e}")
            return False
    
    def _format_sql_value(self, val: object) -> str:
        """Format a Python value as a safe SQL literal."""
        if val is None:
            return "NULL"
        elif isinstance(val, (int, float)):
            return str(val)
        else:
            return "'" + str(val).replace("'", "''") + "'"

    def _write_table_backup(self, f, cursor: sqlite3.Cursor, table_name: str) -> None:
        """Write INSERT statements for a single table to the backup file."""
        try:
            cursor.execute(f"SELECT * FROM {table_name}")
            rows = cursor.fetchall()
            for row in rows:
                values = ", ".join(self._format_sql_value(v) for v in row)
                f.write(f"INSERT INTO {table_name} VALUES ({values});\n")
        except sqlite3.OperationalError as e:
            f.write(f"-- Skipped {table_name}: {e}\n")

    def _create_sql_backup(self, sql_backup_path: str) -> None:
        """Create SQL backup of deduplication tables"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            with open(sql_backup_path, 'w', encoding='utf-8') as f:
                f.write("-- Deduplication Migration Backup\n")
                f.write(f"-- Created: {datetime.now()}\n\n")

                f.write("-- DeduplicatedEvents\n")
                self._write_table_backup(f, cursor, "DeduplicatedEvents")

                f.write("\n-- EventDeduplicationMap\n")
                self._write_table_backup(f, cursor, "EventDeduplicationMap")

                f.write("\n-- DeduplicationClusters\n")
                self._write_table_backup(f, cursor, "DeduplicationClusters")
    
    def _apply_database_constraints(self) -> bool:
        """Clear existing dedup data and prepare for full rebuild."""
        logger.info("🧹 Clearing existing deduplicated events for full rebuild...")

        if self.dry_run:
            logger.info("🔍 DRY RUN: Would clear dedup tables")
            self.migration_report['steps_completed'].append('constraints_applied_dry_run')
            return True

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                # IMPORTANT: ASDRiskClassifications has FK to DeduplicatedEvents — delete first
                cursor.execute("DELETE FROM ASDRiskClassifications")
                cursor.execute("DELETE FROM DeduplicatedEventEntities")
                cursor.execute("DELETE FROM DeduplicatedEventSources")
                cursor.execute("DELETE FROM EventDeduplicationMap")
                cursor.execute("DELETE FROM DeduplicationClusters")
                cursor.execute("DELETE FROM DeduplicatedEvents")
                conn.commit()
                logger.info("✅ Cleared existing deduplicated events and ASD classifications")

            self.migration_report['steps_completed'].append('constraints_applied')
            return True

        except Exception as e:
            logger.error(f"❌ Failed to clear dedup tables: {e}")
            self.migration_report['errors'].append(f"Constraints failed: {e}")
            return False
    
    def _load_enriched_events(self, event_ids: Optional[List[str]] = None) -> Optional[List[CyberEvent]]:
        """Load enriched events from the database.

        Args:
            event_ids: If provided, load only these enriched event IDs.
                       If None, load all active enriched events.
        """
        if event_ids:
            logger.info(f"📥 Loading {len(event_ids)} enriched events from database...")
        else:
            logger.info("📥 Loading all enriched events from database...")

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                base_query = """
                    SELECT e.enriched_event_id, e.title, e.summary, e.event_date, e.event_type, e.severity,
                           e.records_affected, e.confidence_score, e.perplexity_enrichment_data,
                           r.raw_description
                    FROM EnrichedEvents e
                    LEFT JOIN RawEvents r ON e.raw_event_id = r.raw_event_id
                    WHERE e.status = 'Active'
                """

                if event_ids:
                    placeholders = ','.join('?' * len(event_ids))
                    query = f"{base_query} AND e.enriched_event_id IN ({placeholders}) ORDER BY e.event_date DESC"
                    cursor.execute(query, event_ids)
                else:
                    cursor.execute(f"{base_query} ORDER BY e.event_date DESC")

                raw_rows = cursor.fetchall()

            enriched_events = []
            for row in raw_rows:
                event_date = _parse_event_date(row[3])

                # Extract victim organization name and industry from Perplexity enrichment JSON
                victim_org_name = None
                victim_org_industry = None
                if row[8]:  # perplexity_enrichment_data
                    try:
                        enrichment_data = json.loads(row[8])
                        victim_org_name = enrichment_data.get('formal_entity_name')
                        victim_org_industry = enrichment_data.get('victim_industry')
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.debug(f"Could not parse enrichment data for event {row[0]}: {e}")

                event = CyberEvent(
                    event_id=row[0],
                    title=row[1],
                    summary=row[2],
                    description=row[9] if len(row) > 9 else None,
                    event_date=event_date,
                    event_type=row[4],
                    severity=row[5],
                    records_affected=row[6],
                    victim_organization_name=victim_org_name,
                    victim_organization_industry=victim_org_industry,
                    data_sources=[],
                    urls=[],
                    confidence=row[7] if row[7] else 0.5
                )
                enriched_events.append(event)

            logger.info(f"✅ Loaded {len(enriched_events)} enriched events")
            self.migration_report['statistics']['enriched_events_loaded'] = len(enriched_events)
            return enriched_events

        except Exception as e:
            logger.error(f"❌ Failed to load enriched events: {e}")
            self.migration_report['errors'].append(f"Load events failed: {e}")
            return None
    
    def _detect_new_events(self) -> Tuple[List[str], bool]:
        """Detect enriched events not yet present in deduplication output.

        Returns:
            (new_event_ids, existing_dedup_data_exists)
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Check if any deduplicated data exists
                cursor.execute("SELECT COUNT(*) FROM DeduplicatedEvents WHERE status = 'Active'")
                existing_count = cursor.fetchone()[0]

                # Find enriched events that are neither a master nor a merged duplicate
                cursor.execute("""
                    SELECT e.enriched_event_id
                    FROM EnrichedEvents e
                    WHERE e.status = 'Active'
                      AND NOT EXISTS (
                          SELECT 1 FROM EventDeduplicationMap edm
                          WHERE edm.enriched_event_id = e.enriched_event_id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM DeduplicatedEvents de
                          WHERE de.master_enriched_event_id = e.enriched_event_id
                            AND de.status = 'Active'
                      )
                """)
                new_event_ids = [row[0] for row in cursor.fetchall()]

            logger.info(f"📊 Detected {len(new_event_ids)} new events, "
                        f"{existing_count} existing deduplicated events")
            return new_event_ids, existing_count > 0

        except Exception as e:
            logger.error(f"❌ Failed to detect new events: {e}")
            # Fall back to full rebuild
            return [], False

    def _load_existing_deduplicated_events(self) -> Tuple[List[CyberEvent], Dict[str, str]]:
        """Load existing DeduplicatedEvents as CyberEvent objects for incremental matching.

        Returns:
            Tuple of (events list, dict mapping deduplicated_event_id -> master_enriched_event_id)
        """
        logger.info("📥 Loading existing deduplicated events for comparison...")

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT deduplicated_event_id, master_enriched_event_id,
                           title, summary, event_date, event_type, severity,
                           records_affected, confidence_score, victim_organization_name,
                           victim_organization_industry
                    FROM DeduplicatedEvents
                    WHERE status = 'Active'
                """)
                rows = cursor.fetchall()

            events = []
            dedup_to_master = {}
            for row in rows:
                dedup_id = row[0]
                master_id = row[1]
                dedup_to_master[dedup_id] = master_id
                event = CyberEvent(
                    event_id=dedup_id,
                    title=row[2],
                    summary=row[3],
                    event_date=_parse_event_date(row[4]),
                    event_type=row[5],
                    severity=row[6],
                    records_affected=row[7],
                    victim_organization_name=row[9],
                    victim_organization_industry=row[10],
                    data_sources=[],
                    urls=[],
                    confidence=row[8] if row[8] else 0.5
                )
                events.append(event)

            logger.info(f"✅ Loaded {len(events)} existing deduplicated events")
            return events, dedup_to_master

        except Exception as e:
            logger.error(f"❌ Failed to load existing dedup events: {e}")
            return [], {}

    def _run_incremental_deduplication(self, new_events: List[CyberEvent],
                                       existing_dedup_events: List[CyberEvent],
                                       dedup_to_master: Dict[str, str] = None) -> bool:
        """Run incremental deduplication: compare new events against existing + each other."""
        if self.dry_run:
            logger.info(f"🔍 DRY RUN: Would run incremental dedup on {len(new_events)} new events "
                        f"against {len(existing_dedup_events)} existing events")
            self.migration_report['steps_completed'].append('incremental_dedup_dry_run')
            return True

        try:
            with sqlite3.connect(self.db_path) as conn:
                storage = DeduplicationStorage(conn)
                engine = DeduplicationEngine(
                    similarity_threshold=0.75,
                    llm_arbiter=LLMArbiter(api_key=os.getenv('OPENAI_API_KEY')),
                    validators=[DeduplicationValidator()]
                )

                # Step 1: Deduplicate new events among themselves
                logger.info(f"🔄 Step 1/2: Deduplicating {len(new_events)} new events among themselves...")
                new_result = engine.deduplicate(new_events)

                # Step 2: Match each new unique event against existing deduplicated events
                logger.info(f"🔄 Step 2/2: Matching {len(new_result.unique_events)} unique new events "
                            f"against {len(existing_dedup_events)} existing events...")

                matched_ids = set()
                cursor = conn.cursor()

                for new_event in tqdm(new_result.unique_events,
                                      desc="Incremental matching", unit="event", smoothing=0):
                    best_match = None

                    for existing in existing_dedup_events:
                        # Reuse same matching rules as _group_similar_events
                        same_entity = engine._same_entity(new_event, existing)
                        same_date = (new_event.event_date and existing.event_date
                                     and new_event.event_date == existing.event_date)

                        if same_entity and same_date:
                            best_match = existing
                            break

                        if (new_event.title.lower().strip() == existing.title.lower().strip()
                                and new_event.event_date == existing.event_date):
                            best_match = existing
                            break

                        if same_entity:
                            title_sim = engine._quick_title_similarity(new_event.title, existing.title)
                            if title_sim >= 0.15:
                                best_match = existing
                                break
                            if new_event.description and existing.summary:
                                desc_sim = engine._quick_title_similarity(
                                    new_event.description, existing.summary)
                                if desc_sim >= 0.20:
                                    best_match = existing
                                    break

                    if best_match:
                        # Merge into existing deduplicated event
                        self._merge_into_existing(cursor, new_event, best_match,
                                                  dedup_to_master or {})
                        matched_ids.add(new_event.event_id)

                conn.commit()

                # Step 3: Store genuinely new events (no match found)
                truly_new = [e for e in new_result.unique_events if e.event_id not in matched_ids]
                if truly_new:
                    logger.info(f"💾 Storing {len(truly_new)} new deduplicated events...")
                    from cyber_data_collector.processing.deduplication_v2 import (
                        DeduplicationResult, DeduplicationStats
                    )
                    # Build a minimal result for storage
                    minimal_result = DeduplicationResult(
                        unique_events=truly_new,
                        merge_groups=[g for g in new_result.merge_groups
                                      if g.master_event.event_id not in matched_ids],
                        statistics=DeduplicationStats(
                            input_events=len(new_events),
                            output_events=len(truly_new),
                            merge_groups=len(new_result.merge_groups),
                            total_merges=len(new_events) - len(new_result.unique_events),
                            avg_confidence=new_result.statistics.avg_confidence,
                            processing_time_seconds=new_result.statistics.processing_time_seconds
                        ),
                        validation_errors=[]
                    )
                    storage_result = storage.store_deduplication_result(minimal_result)
                    if not storage_result.success:
                        logger.error(f"❌ Storage failed: {len(storage_result.validation_errors)} errors")
                        return False

                logger.info(f"✅ Incremental dedup complete: {len(matched_ids)} merged into existing, "
                            f"{len(truly_new)} new events added")
                self.migration_report['steps_completed'].append('incremental_dedup_completed')
                self.migration_report['statistics'].update({
                    'mode': 'incremental',
                    'new_events_processed': len(new_events),
                    'merged_into_existing': len(matched_ids),
                    'new_events_added': len(truly_new),
                    'input_events': len(new_events),
                    'output_events': len(truly_new),
                })
                return True

        except Exception as e:
            logger.error(f"❌ Incremental deduplication failed: {e}")
            self.migration_report['errors'].append(f"Incremental dedup failed: {e}")
            return False

    def _merge_into_existing(self, cursor: sqlite3.Cursor,
                             new_event: CyberEvent, existing_dedup: CyberEvent,
                             dedup_to_master: Dict[str, str]) -> None:
        """Record that new_event maps to an existing deduplicated event."""
        map_id = str(uuid.uuid4())
        # EventDeduplicationMap.deduplicated_event_id stores the master_enriched_event_id
        # (not the deduplicated_event_id UUID) to match the convention used by full rebuild
        master_id = dedup_to_master.get(existing_dedup.event_id, existing_dedup.event_id)
        cursor.execute("""
            INSERT OR IGNORE INTO EventDeduplicationMap (
                map_id, raw_event_id, enriched_event_id, deduplicated_event_id,
                contribution_type, similarity_score, data_source_weight
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            map_id,
            new_event.event_id,
            new_event.event_id,
            master_id,
            'incremental_merge',
            1.0,
            1.0
        ))

        # Update date to earliest if the new event is earlier
        if (new_event.event_date and existing_dedup.event_date
                and new_event.event_date < existing_dedup.event_date):
            cursor.execute("""
                UPDATE DeduplicatedEvents
                SET event_date = ?, updated_at = ?
                WHERE deduplicated_event_id = ?
            """, (new_event.event_date, datetime.now().isoformat(), existing_dedup.event_id))

    def _clear_existing_deduplications(self) -> bool:
        """Clear existing deduplicated events (already done in _apply_database_constraints)"""
        logger.info("🧹 Deduplicated events already cleared in previous step, skipping...")
        self.migration_report['steps_completed'].append('cleared_deduplications_skipped')
        return True
    
    def _run_global_deduplication(self, enriched_events: list) -> bool:
        """Run the new global deduplication system"""
        logger.info("🔄 Running global deduplication...")

        if self.dry_run:
            logger.info("🔍 DRY RUN: Would run global deduplication")
            self.migration_report['steps_completed'].append('deduplication_run_dry_run')
            return True

        try:
            with sqlite3.connect(self.db_path) as conn:
                storage = DeduplicationStorage(conn)

                # Create deduplication engine
                logger.info("🔧 Initializing deduplication engine...")
                engine = DeduplicationEngine(
                    similarity_threshold=0.75,
                    llm_arbiter=LLMArbiter(api_key=os.getenv('OPENAI_API_KEY')),
                    validators=[DeduplicationValidator()]
                )

                # Run deduplication
                logger.info(f"🔄 Processing {len(enriched_events)} events...")
                logger.info(f"⏱️ This may take several minutes for large datasets...")
                logger.info(f"📊 Estimated comparisons: ~{len(enriched_events) * (len(enriched_events) - 1) // 2:,}")
                result = engine.deduplicate(enriched_events)

                # Log validation warnings (non-fatal - still proceed with storage)
                if result.validation_errors:
                    logger.warning(f"⚠️ Deduplication validation found {len(result.validation_errors)} issues (non-fatal)")
                    for error in result.validation_errors:
                        logger.warning(f"  - {error.error_type}: {error.message}")

                # Store result
                logger.info("💾 Storing deduplication results...")
                storage_result = storage.store_deduplication_result(result)

                if not storage_result.success:
                    logger.error(f"❌ Storage failed: {len(storage_result.validation_errors)} errors")
                    return False

                # Log statistics
                stats = result.statistics
                logger.info(f"✅ Deduplication complete: {stats.input_events} -> {stats.output_events} events")
                logger.info(f"📊 Merge groups: {stats.merge_groups}, Total merges: {stats.total_merges}")
                logger.info(f"📊 Average confidence: {stats.avg_confidence:.2f}")
                logger.info(f"⏱️ Processing time: {stats.processing_time_seconds:.1f}s")

                # Store statistics
                self.migration_report['statistics'].update({
                    'input_events': stats.input_events,
                    'output_events': stats.output_events,
                    'merge_groups': stats.merge_groups,
                    'total_merges': stats.total_merges,
                    'avg_confidence': stats.avg_confidence,
                    'processing_time_seconds': stats.processing_time_seconds
                })

            self.migration_report['steps_completed'].append('deduplication_completed')
            return True

        except Exception as e:
            logger.error(f"❌ Global deduplication failed: {e}")
            self.migration_report['errors'].append(f"Deduplication failed: {e}")
            return False
    
    def _validate_migration_results(self) -> bool:
        """Validate the migration results"""
        logger.info("🔍 Validating migration results...")
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                storage = DeduplicationStorage(conn)

                # Check for integrity issues
                integrity_errors = storage.validate_storage_integrity()
                if integrity_errors:
                    logger.error(f"❌ Storage integrity check failed: {len(integrity_errors)} issues")
                    for error in integrity_errors:
                        logger.error(f"  - {error.error_type}: {error.message}")
                    return False

                # Get final statistics
                stats = storage.get_deduplication_statistics()
                logger.info(f"📊 Final statistics:")
                logger.info(f"  - Active events: {stats['active_events']}")
                logger.info(f"  - Merge groups: {stats['merge_groups']}")
                logger.info(f"  - Total merges: {stats['total_merges']}")

                # Store final statistics
                self.migration_report['statistics'].update({
                    'final_active_events': stats['active_events'],
                    'final_merge_groups': stats['merge_groups'],
                    'final_total_merges': stats['total_merges']
                })

            self.migration_report['steps_completed'].append('validation_completed')
            return True

        except Exception as e:
            logger.error(f"❌ Validation failed: {e}")
            self.migration_report['errors'].append(f"Validation failed: {e}")
            return False
    
    def _generate_migration_report(self):
        """Generate a comprehensive migration report"""
        logger.info("📋 Generating migration report...")
        
        self.migration_report['end_time'] = datetime.now().isoformat()
        self.migration_report['success'] = len(self.migration_report['errors']) == 0
        
        # Save report to file
        report_path = f"migration_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump(self.migration_report, f, indent=2)
        
        logger.info(f"📋 Migration report saved to: {report_path}")
        
        # Print summary
        print("\n" + "="*60)
        print("MIGRATION SUMMARY")
        print("="*60)
        print(f"Database: {self.db_path}")
        print(f"Backup: {self.backup_path}")
        print(f"Success: {'YES' if self.migration_report['success'] else 'NO'}")
        print(f"Steps completed: {len(self.migration_report['steps_completed'])}")
        print(f"Errors: {len(self.migration_report['errors'])}")
        
        if self.migration_report['statistics']:
            stats = self.migration_report['statistics']
            print(f"\nStatistics:")
            print(f"  Input events: {stats.get('input_events', 'N/A')}")
            print(f"  Output events: {stats.get('output_events', 'N/A')}")
            print(f"  Merge groups: {stats.get('merge_groups', 'N/A')}")
            print(f"  Total merges: {stats.get('total_merges', 'N/A')}")
            avg_conf = stats.get('avg_confidence', 'N/A')
            proc_time = stats.get('processing_time_seconds', 'N/A')
            if isinstance(avg_conf, (int, float)):
                print(f"  Avg confidence: {avg_conf:.2f}")
            else:
                print(f"  Avg confidence: {avg_conf}")
            if isinstance(proc_time, (int, float)):
                print(f"  Processing time: {proc_time:.1f}s")
            else:
                print(f"  Processing time: {proc_time}")
        
        if self.migration_report['errors']:
            print(f"\nErrors:")
            for error in self.migration_report['errors']:
                print(f"  - {error}")
        
        print("="*60)


def main():
    """Main migration function"""
    parser = argparse.ArgumentParser(
        description='Migrate from old to new global deduplication system'
    )
    parser.add_argument(
        '--db-path', 
        default='instance/cyber_events.db',
        help='Path to SQLite database file (default: instance/cyber_events.db)'
    )
    parser.add_argument(
        '--backup-path',
        help='Path for backup file (default: auto-generated)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Run in dry-run mode (no actual changes)'
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Force full rebuild (ignore incremental mode)'
    )
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Enable verbose logging'
    )
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Check if database exists
    if not os.path.exists(args.db_path):
        logger.error(f"❌ Database file not found: {args.db_path}")
        return 1
    
    # Run migration
    migration = DeduplicationMigration(
        db_path=args.db_path,
        backup_path=args.backup_path,
        dry_run=args.dry_run,
        force=args.force
    )
    
    success = migration.run_migration()
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
