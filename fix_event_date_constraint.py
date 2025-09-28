#!/usr/bin/env python3
"""
Fix Event Date NULL Constraint Issue

This script fixes the NOT NULL constraint on event_date in existing V2 databases
to allow NULL values when event dates are unknown.

Usage:
    python fix_event_date_constraint.py
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def fix_event_date_constraint(db_path: str = "instance/cyber_events.db"):
    """Fix the NOT NULL constraint on event_date in EnrichedEvents table."""

    if not Path(db_path).exists():
        print(f"Database not found: {db_path}")
        return False

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Check if V2 schema exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='EnrichedEvents'")
        if not cursor.fetchone():
            print("EnrichedEvents table not found - no V2 schema detected")
            conn.close()
            return False

        print("Found V2 schema with EnrichedEvents table")

        # Get current table schema
        cursor.execute("PRAGMA table_info(EnrichedEvents)")
        columns = cursor.fetchall()

        # Check if event_date has NOT NULL constraint
        event_date_col = None
        for col in columns:
            if col[1] == 'event_date':  # column name
                event_date_col = col
                break

        if not event_date_col:
            print("event_date column not found in EnrichedEvents")
            conn.close()
            return False

        if event_date_col[3] == 0:  # not null = 0 means nullable
            print("event_date column is already nullable - no fix needed")
            conn.close()
            return True

        print("event_date column has NOT NULL constraint - fixing...")

        # SQLite doesn't support ALTER COLUMN, so we need to recreate the table
        backup_table_name = f"EnrichedEvents_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        # Step 1: Create backup of existing data
        cursor.execute(f"CREATE TABLE {backup_table_name} AS SELECT * FROM EnrichedEvents")
        backup_count = cursor.rowcount
        print(f"Created backup table {backup_table_name} with {backup_count} records")

        # Step 2: Drop the original table
        cursor.execute("DROP TABLE EnrichedEvents")

        # Step 3: Recreate table with nullable event_date
        create_table_sql = """
        CREATE TABLE EnrichedEvents (
            enriched_event_id TEXT PRIMARY KEY,
            raw_event_id TEXT NOT NULL,
            title VARCHAR(255) NOT NULL,
            description TEXT,
            summary TEXT,
            event_type VARCHAR(50),
            severity VARCHAR(20),
            event_date DATE,                    -- Now nullable!
            records_affected BIGINT,
            is_australian_event BOOLEAN NOT NULL,
            is_specific_event BOOLEAN NOT NULL,
            confidence_score REAL,
            australian_relevance_score REAL,
            status VARCHAR(20) DEFAULT 'Active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (raw_event_id) REFERENCES RawEvents(raw_event_id) ON DELETE CASCADE
        )
        """
        cursor.execute(create_table_sql)
        print("Recreated EnrichedEvents table with nullable event_date")

        # Step 4: Restore data from backup
        cursor.execute(f"""
            INSERT INTO EnrichedEvents SELECT * FROM {backup_table_name}
        """)
        restored_count = cursor.rowcount
        print(f"Restored {restored_count} records to new table")

        # Step 5: Clean up backup table
        cursor.execute(f"DROP TABLE {backup_table_name}")
        print(f"Cleaned up backup table {backup_table_name}")

        conn.commit()
        conn.close()

        print("✅ Successfully fixed event_date constraint - NULL values now allowed")
        return True

    except Exception as e:
        print(f"❌ Error fixing event_date constraint: {e}")
        if 'conn' in locals():
            conn.rollback()
            conn.close()
        return False


if __name__ == "__main__":
    print("Fixing event_date NOT NULL constraint in EnrichedEvents table...")

    success = fix_event_date_constraint()

    if success:
        print("\nFix completed successfully!")
        print("You can now run the enrichment pipeline without event_date errors.")
    else:
        print("\nFix failed!")
        print("Please check the error messages above.")
        sys.exit(1)