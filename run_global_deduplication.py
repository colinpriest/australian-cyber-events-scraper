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
    python run_global_deduplication.py [--db-path PATH] [--backup-path PATH] [--dry-run]
"""

import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Add the project root to the Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cyber_data_collector.processing.deduplication_v2 import DeduplicationEngine, LLMArbiter, DeduplicationValidator
from cyber_data_collector.storage.deduplication_storage import DeduplicationStorage
from cyber_data_collector.processing.deduplication_v2 import CyberEvent

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class DeduplicationMigration:
    """Handles migration from old to new deduplication system"""
    
    def __init__(self, db_path: str, backup_path: str = None, dry_run: bool = False):
        self.db_path = db_path
        self.backup_path = backup_path or f"{db_path}.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.dry_run = dry_run
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
        """Run the complete migration process"""
        logger.info("🚀 Starting deduplication system migration...")
        
        try:
            # Step 1: Backup current data
            if not self._backup_current_data():
                return False
            
            # Step 2: Apply database constraints
            if not self._apply_database_constraints():
                return False
            
            # Step 3: Load enriched events
            enriched_events = self._load_enriched_events()
            if enriched_events is None:
                return False
            
            # Step 4: Clear existing deduplications
            if not self._clear_existing_deduplications():
                return False
            
            # Step 5: Run new global deduplication
            if not self._run_global_deduplication(enriched_events):
                return False
            
            # Step 6: Validate results (skip in dry-run mode)
            if not self.dry_run:
                if not self._validate_migration_results():
                    return False
            else:
                logger.info("🔍 DRY RUN: Would validate migration results")
            
            # Step 7: Generate report
            self._generate_migration_report()
            
            logger.info("✅ Migration completed successfully!")
            return True
            
        except Exception as e:
            logger.error(f"❌ Migration failed: {e}")
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
    
    def _create_sql_backup(self, sql_backup_path: str):
        """Create SQL backup of deduplication tables"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        with open(sql_backup_path, 'w', encoding='utf-8') as f:
            f.write(f"-- Deduplication Migration Backup\n")
            f.write(f"-- Created: {datetime.now()}\n\n")
            
            # Export DeduplicatedEvents
            f.write("-- DeduplicatedEvents\n")
            cursor.execute("SELECT * FROM DeduplicatedEvents")
            rows = cursor.fetchall()
            for row in rows:
                f.write(f"INSERT INTO DeduplicatedEvents VALUES {row};\n")
            
            # Export EventDeduplicationMap
            f.write("\n-- EventDeduplicationMap\n")
            cursor.execute("SELECT * FROM EventDeduplicationMap")
            rows = cursor.fetchall()
            for row in rows:
                f.write(f"INSERT INTO EventDeduplicationMap VALUES {row};\n")
            
            # Export DeduplicationClusters
            f.write("\n-- DeduplicationClusters\n")
            cursor.execute("SELECT * FROM DeduplicationClusters")
            rows = cursor.fetchall()
            for row in rows:
                f.write(f"INSERT INTO DeduplicationClusters VALUES {row};\n")
        
        conn.close()
    
    def _apply_database_constraints(self) -> bool:
        """Apply database constraints to prevent duplicates"""
        logger.info("🔧 Applying database constraints...")
        
        if self.dry_run:
            logger.info("🔍 DRY RUN: Would apply database constraints")
            self.migration_report['steps_completed'].append('constraints_applied_dry_run')
            return True
        
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # First, clear existing deduplicated events to avoid constraint conflicts
            # IMPORTANT: Also clear ASDRiskClassifications since it has FK to DeduplicatedEvents
            # The classifications will be regenerated in the next pipeline run
            logger.info("🧹 Clearing existing deduplicated events and related tables...")
            cursor.execute("DELETE FROM ASDRiskClassifications")  # Must delete first (FK constraint)
            cursor.execute("DELETE FROM DeduplicatedEvents")
            cursor.execute("DELETE FROM EventDeduplicationMap")
            cursor.execute("DELETE FROM DeduplicationClusters")
            conn.commit()
            logger.info("✅ Cleared existing deduplicated events and ASD classifications")
            
            # Read and execute the migration SQL
            migration_sql_path = "database_migrations/add_deduplication_constraints.sql"
            if os.path.exists(migration_sql_path):
                with open(migration_sql_path, 'r') as f:
                    sql_content = f.read()
                
                # Execute the migration
                cursor.executescript(sql_content)
                conn.commit()
                logger.info("✅ Database constraints applied successfully")
            else:
                logger.warning("⚠️ Migration SQL file not found, skipping constraints")
            
            conn.close()
            self.migration_report['steps_completed'].append('constraints_applied')
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to apply constraints: {e}")
            self.migration_report['errors'].append(f"Constraints failed: {e}")
            return False
    
    def _load_enriched_events(self) -> list:
        """Load all enriched events from the database"""
        logger.info("📥 Loading enriched events from database...")

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # Query all enriched events including perplexity enrichment data and raw description
            cursor.execute("""
                SELECT e.enriched_event_id, e.title, e.summary, e.event_date, e.event_type, e.severity,
                       e.records_affected, e.confidence_score, e.perplexity_enrichment_data,
                       r.raw_description
                FROM EnrichedEvents e
                LEFT JOIN RawEvents r ON e.raw_event_id = r.raw_event_id
                WHERE e.status = 'Active'
                ORDER BY e.event_date DESC
            """)

            enriched_events = []
            for row in cursor.fetchall():
                # Parse event date safely
                event_date = None
                if row[3]:
                    try:
                        # Try to parse as ISO format
                        event_date = datetime.fromisoformat(row[3]).date()
                    except ValueError:
                        # If it fails, try to parse partial dates
                        try:
                            # Handle YYYY-MM format by adding -01
                            if len(row[3]) == 7 and row[3].count('-') == 1:
                                event_date = datetime.fromisoformat(row[3] + '-01').date()
                            # Handle YYYY format by adding -01-01
                            elif len(row[3]) == 4:
                                event_date = datetime.fromisoformat(row[3] + '-01-01').date()
                            else:
                                logger.warning(f"Could not parse date '{row[3]}' for event {row[0]}")
                        except ValueError:
                            logger.warning(f"Could not parse date '{row[3]}' for event {row[0]}")

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
                    description=row[9] if len(row) > 9 else None,  # raw_description from RawEvents
                    event_date=event_date,
                    event_type=row[4],
                    severity=row[5],
                    records_affected=row[6],
                    victim_organization_name=victim_org_name,
                    victim_organization_industry=victim_org_industry,
                    data_sources=[],  # Not available in EnrichedEvents
                    urls=[],  # Not available in EnrichedEvents
                    confidence=row[7] if row[7] else 0.5
                )
                enriched_events.append(event)

            conn.close()

            logger.info(f"✅ Loaded {len(enriched_events)} enriched events")
            self.migration_report['statistics']['enriched_events_loaded'] = len(enriched_events)
            return enriched_events

        except Exception as e:
            logger.error(f"❌ Failed to load enriched events: {e}")
            self.migration_report['errors'].append(f"Load events failed: {e}")
            return None
    
    def _clear_existing_deduplications(self) -> bool:
        """Clear existing deduplicated events (already done in _apply_database_constraints)"""
        # Skip this step - we already cleared in _apply_database_constraints
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
            conn = sqlite3.connect(self.db_path)
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
            
            # Validate result
            if result.validation_errors:
                logger.error(f"❌ Deduplication validation failed: {len(result.validation_errors)} errors")
                for error in result.validation_errors:
                    logger.error(f"  - {error.error_type}: {error.message}")
                return False
            
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
            
            conn.close()
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
            conn = sqlite3.connect(self.db_path)
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
            
            conn.close()
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
        dry_run=args.dry_run
    )
    
    success = migration.run_migration()
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
