#!/usr/bin/env python3
"""
Add Perplexity validation tracking fields to the database.

This script adds columns to track:
- Whether an event has been validated by Perplexity
- When the validation occurred
- The Perplexity enrichment data (JSON)
- Field-level confidence scores
"""

import sqlite3
import sys
from pathlib import Path


def add_perplexity_validation_fields(db_path: str = "instance/cyber_events.db"):
    """Add Perplexity validation tracking fields to EnrichedEvents and DeduplicatedEvents."""

    print("Adding Perplexity validation fields to database...")

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")

        cursor = conn.cursor()

        # Add fields to EnrichedEvents
        print("Adding fields to EnrichedEvents table...")
        try:
            cursor.execute("""
                ALTER TABLE EnrichedEvents
                ADD COLUMN perplexity_validated BOOLEAN DEFAULT FALSE;
            """)
            cursor.execute("""
                ALTER TABLE EnrichedEvents
                ADD COLUMN perplexity_validated_at TIMESTAMP;
            """)
            cursor.execute("""
                ALTER TABLE EnrichedEvents
                ADD COLUMN perplexity_enrichment_data TEXT;
            """)
            cursor.execute("""
                ALTER TABLE EnrichedEvents
                ADD COLUMN date_confidence REAL;
            """)
            cursor.execute("""
                ALTER TABLE EnrichedEvents
                ADD COLUMN entity_confidence REAL;
            """)
            cursor.execute("""
                ALTER TABLE EnrichedEvents
                ADD COLUMN data_source_reliability REAL;
            """)
            print("  Added perplexity_validated, perplexity_validated_at, perplexity_enrichment_data")
            print("  Added date_confidence, entity_confidence, data_source_reliability")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e):
                print("  Columns already exist in EnrichedEvents")
            else:
                raise

        # Add fields to DeduplicatedEvents
        print("\nAdding fields to DeduplicatedEvents table...")
        try:
            cursor.execute("""
                ALTER TABLE DeduplicatedEvents
                ADD COLUMN perplexity_validated BOOLEAN DEFAULT FALSE;
            """)
            cursor.execute("""
                ALTER TABLE DeduplicatedEvents
                ADD COLUMN perplexity_validated_at TIMESTAMP;
            """)
            cursor.execute("""
                ALTER TABLE DeduplicatedEvents
                ADD COLUMN perplexity_enrichment_data TEXT;
            """)
            cursor.execute("""
                ALTER TABLE DeduplicatedEvents
                ADD COLUMN date_confidence REAL;
            """)
            cursor.execute("""
                ALTER TABLE DeduplicatedEvents
                ADD COLUMN entity_confidence REAL;
            """)
            cursor.execute("""
                ALTER TABLE DeduplicatedEvents
                ADD COLUMN data_source_reliability REAL;
            """)
            print("  Added perplexity_validated, perplexity_validated_at, perplexity_enrichment_data")
            print("  Added date_confidence, entity_confidence, data_source_reliability")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e):
                print("  Columns already exist in DeduplicatedEvents")
            else:
                raise

        # Create indexes for performance
        print("\nCreating indexes for Perplexity validation...")
        try:
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_enriched_perplexity_validated
                ON EnrichedEvents(perplexity_validated);
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_dedup_perplexity_validated
                ON DeduplicatedEvents(perplexity_validated);
            """)
            print("  Created performance indexes")
        except sqlite3.OperationalError as e:
            print(f"  Index creation warning: {e}")

        conn.commit()
        print("\n[SUCCESS] Perplexity validation fields added successfully!")

        # Show statistics
        print("\nDatabase statistics:")
        cursor.execute("SELECT COUNT(*) as total FROM EnrichedEvents")
        enriched_count = cursor.fetchone()[0]
        print(f"  EnrichedEvents: {enriched_count} records")

        cursor.execute("SELECT COUNT(*) as total FROM DeduplicatedEvents")
        dedup_count = cursor.fetchone()[0]
        print(f"  DeduplicatedEvents: {dedup_count} records")

        cursor.execute("""
            SELECT COUNT(*) as count
            FROM EnrichedEvents
            WHERE perplexity_validated = TRUE
        """)
        validated_count = cursor.fetchone()[0]
        print(f"  Perplexity-validated events: {validated_count}")

        conn.close()
        return True

    except Exception as e:
        print(f"[ERROR] Error adding Perplexity validation fields: {e}")
        return False


if __name__ == "__main__":
    success = add_perplexity_validation_fields()
    if success:
        print("\n✅ Database schema update complete!")
    else:
        print("\n❌ Schema update failed!")
        sys.exit(1)
