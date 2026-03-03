#!/usr/bin/env python3
"""
Database Records Wiper for Australian Cyber Events Scraper

This script safely clears all RECORDS from the project's database tables
while preserving the database structure and schema.

Usage:
    python wipe_database.py [--force] [--dry-run] [--tables TABLES]

Arguments:
    --force         Skip confirmation prompts (dangerous!)
    --dry-run       Show what would be deleted without actually deleting
    --tables        Comma-separated list of specific tables to wipe (default: all)
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import List, Optional, Dict

try:
    import psycopg2
    POSTGRES_AVAILABLE = True
except ImportError:
    POSTGRES_AVAILABLE = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class DatabaseRecordWiper:
    """Handles wiping records from the project's database tables."""

    # Project-specific database configuration
    SQLITE_DB_PATH = "instance/cyber_events.db"

    # Allowlist of permitted table names to prevent SQL injection
    ALLOWED_TABLES = {
        'RawEvents', 'EnrichedEvents', 'DeduplicatedEvents', 'ProcessingLog',
        'ASDRiskClassifications', 'EntitiesV2', 'EventEntitiesV2',
        'DeduplicationClusters', 'EventDeduplicationMap', 'EnrichmentAuditTrail',
        'IndustryGroupings', 'AuditLog', 'DeduplicatedEventEntities',
        'DeduplicatedEventSources', 'EnrichedEventEntities', 'DataSourcesV2',
        'MonthProcessed', 'EventAttributeHistory', 'EventSources', 'EventEntities',
        'UniqueEvents', 'Entities', 'DataSources',
    }

    # V1 project tables (legacy - kept for backward compatibility)
    V1_PROJECT_TABLES = [
        "EventAttributeHistory",  # No dependencies
        "EventSources",          # References UniqueEvents and DataSources
        "EventEntities",         # References UniqueEvents and Entities
        "UniqueEvents",          # Main events table
        "Entities",              # Entity definitions
        "DataSources"            # Data source definitions
    ]

    # V2 project tables in dependency order (for proper deletion)
    V2_PROJECT_TABLES = [
        "AuditLog",                    # Audit trail - no dependencies
        "ProcessingLog",               # References RawEvents
        "DeduplicatedEventEntities",   # References DeduplicatedEvents and EntitiesV2
        "DeduplicatedEventSources",    # References DeduplicatedEvents
        "EventDeduplicationMap",       # References RawEvents, EnrichedEvents, DeduplicatedEvents
        "DeduplicationClusters",       # References DeduplicatedEvents
        "DeduplicatedEvents",          # Deduplicated events table
        "EnrichedEventEntities",       # References EnrichedEvents and EntitiesV2
        "EnrichedEvents",              # References RawEvents
        "RawEvents",                   # Main raw events table
        "EntitiesV2",                  # Enhanced entity definitions
        "DataSourcesV2",               # Enhanced data source definitions
        "MonthProcessed"               # Month processing tracking table
    ]

    # Combined tables - will detect which schema is in use
    PROJECT_TABLES = V1_PROJECT_TABLES + V2_PROJECT_TABLES

    def __init__(self, dry_run: bool = False, force: bool = False):
        self.dry_run = dry_run
        self.force = force
        self.deleted_count = 0
        self.schema_version = None

    def log(self, message: str, level: str = "INFO"):
        """Log a message with timestamp."""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        prefix = "[DRY-RUN] " if self.dry_run else ""
        print(f"[{timestamp}] {prefix}{level}: {message}")

    def confirm_action(self, message: str) -> bool:
        """Get user confirmation for dangerous operations."""
        if self.force:
            return True

        if self.dry_run:
            self.log(f"Would ask: {message}")
            return True

        response = input(f"\nWARNING: {message} (type 'YES' to confirm): ").strip()
        return response == "YES"

    def detect_schema_version(self, db_path: str) -> str:
        """Detect which database schema version is in use."""
        if not os.path.exists(db_path):
            return "none"

        try:
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()

                # Check for V2 tables
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='RawEvents'")
                if cursor.fetchone():
                    self.schema_version = "v2"
                    return "v2"

                # Check for V1 tables
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='UniqueEvents'")
                if cursor.fetchone():
                    self.schema_version = "v1"
                    return "v1"

            return "unknown"

        except Exception as e:
            self.log(f"Error detecting schema version: {e}", "ERROR")
            return "error"

    def get_relevant_tables(self) -> List[str]:
        """Get the relevant tables based on detected schema version."""
        if self.schema_version == "v2":
            return self.V2_PROJECT_TABLES
        elif self.schema_version == "v1":
            return self.V1_PROJECT_TABLES
        else:
            # Return all tables for detection
            return self.PROJECT_TABLES

    def get_table_info(self, db_path: str) -> Dict[str, int]:
        """Get record counts for all project tables."""
        if not os.path.exists(db_path):
            return {}

        # Detect schema version if not already done
        if not self.schema_version:
            self.detect_schema_version(db_path)

        table_counts = {}
        try:
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()

                # Get relevant tables for the detected schema
                tables_to_check = self.get_relevant_tables()

                for table in tables_to_check:
                    if table not in self.ALLOWED_TABLES:
                        continue
                    try:
                        cursor.execute(f"SELECT COUNT(*) FROM {table}")
                        count = cursor.fetchone()[0]
                        table_counts[table] = count
                    except sqlite3.OperationalError:
                        # Table doesn't exist
                        pass
        except Exception as e:
            self.log(f"Error getting table info: {e}", "ERROR")

        return table_counts

    def wipe_sqlite_records(self, tables_to_wipe: Optional[List[str]] = None) -> bool:
        """Wipe records from SQLite database tables."""
        if not os.path.exists(self.SQLITE_DB_PATH):
            self.log(f"SQLite database not found at: {self.SQLITE_DB_PATH}")
            return True

        # Detect and report schema version
        schema_version = self.detect_schema_version(self.SQLITE_DB_PATH)
        self.log(f"Found project database: {self.SQLITE_DB_PATH} (Schema: {schema_version.upper()})")

        if schema_version in ["none", "unknown", "error"]:
            self.log(f"Cannot proceed with {schema_version} schema", "ERROR")
            return False

        # Get current record counts
        table_counts = self.get_table_info(self.SQLITE_DB_PATH)
        if not table_counts:
            self.log("No project tables found or database is empty")
            return True

        # Determine which tables to wipe
        relevant_tables = self.get_relevant_tables()
        if tables_to_wipe:
            tables_to_process = [t for t in tables_to_wipe if t in table_counts]
            if not tables_to_process:
                self.log("None of the specified tables exist in the database")
                return True
        else:
            tables_to_process = [t for t in relevant_tables if t in table_counts and table_counts[t] > 0]

        if not tables_to_process:
            self.log("No tables with records found")
            return True

        # Validate each table against the allowlist to prevent SQL injection
        validated_tables = []
        for table in tables_to_process:
            if table not in self.ALLOWED_TABLES:
                self.log(f"Skipping unknown table: {table}", "WARNING")
            else:
                validated_tables.append(table)
        tables_to_process = validated_tables

        if not tables_to_process:
            self.log("No valid tables to process after allowlist check")
            return True

        # Show what will be deleted
        total_records = sum(table_counts[t] for t in tables_to_process)
        self.log(f"Tables to wipe:")
        for table in tables_to_process:
            count = table_counts[table]
            self.log(f"  - {table}: {count:,} records")

        self.log(f"Total records to delete: {total_records:,}")

        if not self.confirm_action(f"Delete {total_records:,} records from {len(tables_to_process)} tables?"):
            self.log("Record deletion cancelled by user")
            return False

        if self.dry_run:
            self.log("DRY RUN: Would delete records from tables")
            return True

        # Perform the deletion
        try:
            with sqlite3.connect(self.SQLITE_DB_PATH) as conn:
                cursor = conn.cursor()

                # Disable foreign key constraints temporarily
                cursor.execute("PRAGMA foreign_keys = OFF")

                # Delete records from each table
                for table in tables_to_process:
                    cursor.execute(f"DELETE FROM {table}")
                    deleted_rows = cursor.rowcount
                    self.log(f"Deleted {deleted_rows:,} records from {table}")
                    self.deleted_count += deleted_rows

                # Reset auto-increment counters
                cursor.execute("DELETE FROM sqlite_sequence WHERE name IN ({})".format(
                    ','.join('?' * len(tables_to_process))
                ), tables_to_process)

                # Re-enable foreign key constraints
                cursor.execute("PRAGMA foreign_keys = ON")

            self.log(f"Successfully deleted {self.deleted_count:,} total records")
            return True

        except Exception as e:
            self.log(f"Error wiping database records: {e}", "ERROR")
            return False

    def wipe_postgresql_records(self, tables_to_wipe: Optional[List[str]] = None) -> bool:
        """Wipe records from PostgreSQL database tables."""
        if not POSTGRES_AVAILABLE:
            self.log("psycopg2 not available, skipping PostgreSQL")
            return True

        # Get PostgreSQL connection details from environment
        db_config = {
            'host': os.getenv('POSTGRES_HOST', 'localhost'),
            'port': os.getenv('POSTGRES_PORT', '5432'),
            'database': os.getenv('POSTGRES_DB', 'cyber_events'),
            'user': os.getenv('POSTGRES_USER'),
            'password': os.getenv('POSTGRES_PASSWORD')
        }

        if not db_config['user']:
            self.log("No PostgreSQL credentials found, skipping")
            return True

        self.log(f"Connecting to PostgreSQL database: {db_config['database']}")

        try:
            conn = psycopg2.connect(**db_config)
            cursor = conn.cursor()

            # Get existing tables that match our project tables
            cursor.execute("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                AND table_type = 'BASE TABLE'
                AND table_name = ANY(%s);
            """, (self.PROJECT_TABLES,))

            existing_tables = [row[0] for row in cursor.fetchall()]

            if tables_to_wipe:
                tables_to_process = [t for t in tables_to_wipe if t in existing_tables]
            else:
                tables_to_process = existing_tables

            if not tables_to_process:
                self.log("No matching project tables found in PostgreSQL")
                conn.close()
                return True

            # Get record counts
            table_counts = {}
            for table in tables_to_process:
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                table_counts[table] = cursor.fetchone()[0]

            total_records = sum(table_counts.values())
            if total_records == 0:
                self.log("No records found in PostgreSQL tables")
                conn.close()
                return True

            self.log(f"PostgreSQL tables to wipe:")
            for table in tables_to_process:
                count = table_counts[table]
                self.log(f"  - {table}: {count:,} records")

            if not self.confirm_action(f"Truncate {total_records:,} records from {len(tables_to_process)} PostgreSQL tables?"):
                self.log("PostgreSQL record deletion cancelled by user")
                conn.close()
                return False

            if not self.dry_run:
                # Disable foreign key constraints temporarily
                cursor.execute("SET session_replication_role = replica;")

                # Truncate all tables
                for table in tables_to_process:
                    cursor.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE;")
                    self.log(f"Truncated table: {table}")

                # Re-enable foreign key constraints
                cursor.execute("SET session_replication_role = DEFAULT;")

                conn.commit()
                self.log(f"Successfully truncated {len(tables_to_process)} PostgreSQL tables")
            else:
                for table in tables_to_process:
                    self.log(f"Would truncate table: {table}")

            conn.close()
            return True

        except Exception as e:
            self.log(f"Error wiping PostgreSQL records: {e}", "ERROR")
            return False

    def verify_wipe(self) -> bool:
        """Verify that the wipe operation was successful."""
        if self.dry_run:
            return True

        self.log("Verifying wipe operation...")

        # Check SQLite
        if os.path.exists(self.SQLITE_DB_PATH):
            table_counts = self.get_table_info(self.SQLITE_DB_PATH)
            total_remaining = sum(table_counts.values())

            if total_remaining > 0:
                self.log(f"WARNING: {total_remaining} records still remain in SQLite database", "WARNING")
                for table, count in table_counts.items():
                    if count > 0:
                        self.log(f"  - {table}: {count} records remaining")
                return False
            else:
                self.log("SQLite database successfully wiped - all project tables empty")

        return True

    def wipe_all_records(self, tables_to_wipe: Optional[List[str]] = None) -> bool:
        """Wipe records from all configured databases."""
        self.log("Starting database record wipe operation...")

        if tables_to_wipe:
            self.log(f"Target tables: {', '.join(tables_to_wipe)}")
        else:
            self.log("Target: All project tables")

        if self.dry_run:
            self.log("DRY RUN MODE - No actual deletion will occur")

        if not self.force and not self.dry_run:
            if not self.confirm_action(
                "This will permanently delete all records from the project database tables. "
                "The database structure will be preserved, but all data will be lost. "
                "This action cannot be undone!"
            ):
                self.log("Operation cancelled by user")
                return False

        success = True

        # Wipe SQLite records
        success &= self.wipe_sqlite_records(tables_to_wipe)

        # Wipe PostgreSQL records if configured
        success &= self.wipe_postgresql_records(tables_to_wipe)

        # Verify the operation
        if success and not self.dry_run:
            success &= self.verify_wipe()

        if success:
            self.log(f"Database record wipe completed successfully! Processed {self.deleted_count:,} records.")
        else:
            self.log("Database record wipe completed with some errors", "WARNING")

        return success


def main():
    parser = argparse.ArgumentParser(
        description="Wipe all records from Australian Cyber Events Scraper database tables"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompts (dangerous!)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without actually deleting"
    )
    parser.add_argument(
        "--tables",
        type=str,
        help="Comma-separated list of specific tables to wipe (default: all project tables)"
    )

    args = parser.parse_args()

    # Parse table list
    tables_to_wipe = None
    if args.tables:
        tables_to_wipe = [t.strip() for t in args.tables.split(',')]

    print("=" * 70)
    print("Australian Cyber Events Scraper - Database Record Wiper")
    print("=" * 70)

    if args.dry_run:
        print("DRY RUN MODE - No actual deletion will occur")

    if args.force and not args.dry_run:
        print("FORCE MODE - Skipping all confirmations!")
        time.sleep(2)  # Give user a moment to abort

    wiper = DatabaseRecordWiper(dry_run=args.dry_run, force=args.force)

    try:
        success = wiper.wipe_all_records(tables_to_wipe)
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"\nUnexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()