#!/usr/bin/env python3
"""
Add threat actor/attacking entity tracking to EnrichedEvents and DeduplicatedEvents tables.

This will allow us to track:
1. Which entity/group performed the cyber attack
2. Link to EntitiesV2 for threat actor details
3. Distinguish between victims and attackers in our data
"""

import sqlite3
import sys
from pathlib import Path


def add_threat_actor_columns(db_path: str = "instance/cyber_events.db"):
    """Add threat actor tracking columns to event tables."""

    print("Adding threat actor tracking columns...")

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON;")
        cursor = conn.cursor()

        # Add columns to EnrichedEvents
        print("Adding columns to EnrichedEvents table...")
        try:
            cursor.execute("""
                ALTER TABLE EnrichedEvents
                ADD COLUMN attacking_entity_id INTEGER;
            """)
            cursor.execute("""
                ALTER TABLE EnrichedEvents
                ADD COLUMN attacking_entity_name VARCHAR(255);
            """)
            cursor.execute("""
                ALTER TABLE EnrichedEvents
                ADD COLUMN attack_method VARCHAR(100);
            """)
            print("  Added attacking_entity_id, attacking_entity_name, attack_method")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e):
                print("  Columns already exist in EnrichedEvents")
            else:
                raise

        # Add columns to DeduplicatedEvents
        print("Adding columns to DeduplicatedEvents table...")
        try:
            cursor.execute("""
                ALTER TABLE DeduplicatedEvents
                ADD COLUMN attacking_entity_id INTEGER;
            """)
            cursor.execute("""
                ALTER TABLE DeduplicatedEvents
                ADD COLUMN attacking_entity_name VARCHAR(255);
            """)
            cursor.execute("""
                ALTER TABLE DeduplicatedEvents
                ADD COLUMN attack_method VARCHAR(100);
            """)
            print("  Added attacking_entity_id, attacking_entity_name, attack_method")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e):
                print("  Columns already exist in DeduplicatedEvents")
            else:
                raise

        # Add foreign key constraint (note: SQLite doesn't enforce this in ALTER TABLE)
        # But we can create an index to help with performance
        print("Creating indexes for threat actor relationships...")
        try:
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_enriched_attacking_entity
                ON EnrichedEvents(attacking_entity_id);
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_dedup_attacking_entity
                ON DeduplicatedEvents(attacking_entity_id);
            """)
            print("  Created performance indexes")
        except sqlite3.OperationalError as e:
            print(f"  Index creation warning: {e}")

        conn.commit()
        conn.close()

        print("Threat actor columns added successfully!")
        return True

    except Exception as e:
        print(f"Error adding threat actor columns: {e}")
        return False


def populate_known_threat_actors(db_path: str = "instance/cyber_events.db"):
    """Populate known threat actors from existing entity relationships."""

    print("Populating known threat actors from existing data...")

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Find threat actors from EntitiesV2
        cursor.execute("""
            SELECT entity_id, entity_name
            FROM EntitiesV2
            WHERE entity_type = 'threat-actor'
        """)
        threat_actors = cursor.fetchall()

        print(f"Found {len(threat_actors)} threat actors:")
        for actor in threat_actors:
            print(f"  - {actor['entity_name']} (ID: {actor['entity_id']})")

        # Update enriched events with known threat actors
        updated_count = 0
        for actor in threat_actors:
            # Look for events where this actor is mentioned in the title or summary
            cursor.execute("""
                UPDATE EnrichedEvents
                SET attacking_entity_id = ?, attacking_entity_name = ?, attack_method = ?
                WHERE (LOWER(title) LIKE ? OR LOWER(summary) LIKE ?)
                  AND attacking_entity_id IS NULL
            """, (
                actor['entity_id'],
                actor['entity_name'],
                'ransomware' if 'ransomware' in actor['entity_name'].lower() else 'cyber attack',
                f"%{actor['entity_name'].lower()}%",
                f"%{actor['entity_name'].lower()}%"
            ))

            if cursor.rowcount > 0:
                updated_count += cursor.rowcount
                print(f"  Updated {cursor.rowcount} events for {actor['entity_name']}")

        # Update deduplicated events based on their master enriched events
        cursor.execute("""
            UPDATE DeduplicatedEvents
            SET
                attacking_entity_id = ee.attacking_entity_id,
                attacking_entity_name = ee.attacking_entity_name,
                attack_method = ee.attack_method
            FROM EnrichedEvents ee
            WHERE DeduplicatedEvents.master_enriched_event_id = ee.enriched_event_id
              AND ee.attacking_entity_id IS NOT NULL
        """)

        dedup_updated = cursor.rowcount

        conn.commit()
        conn.close()

        print(f"Population complete!")
        print(f"  Updated {updated_count} enriched events")
        print(f"  Updated {dedup_updated} deduplicated events")

        return True

    except Exception as e:
        print(f"Error populating threat actors: {e}")
        return False


if __name__ == "__main__":
    print("Setting up threat actor tracking...")

    # Add columns
    if not add_threat_actor_columns():
        print("Failed to add columns")
        sys.exit(1)

    # Populate known data
    if not populate_known_threat_actors():
        print("Failed to populate threat actors")
        sys.exit(1)

    print("Threat actor setup complete!")