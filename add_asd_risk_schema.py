#!/usr/bin/env python3
"""
Add ASD Risk Classification Schema

This script creates the ASDRiskClassifications table for storing
ASD severity and stakeholder classifications for cyber events.

Usage:
    python add_asd_risk_schema.py [--db-path instance/cyber_events.db]
"""

import argparse
import sqlite3
import sys
from pathlib import Path


def add_asd_risk_schema(db_path: str = "instance/cyber_events.db"):
    """Add ASD risk classification table to the database."""
    
    print("Adding ASD Risk Classification schema...")
    
    try:
        db_path_obj = Path(db_path)
        if not db_path_obj.exists():
            print(f"Error: Database not found at {db_path}")
            return False
        
        conn = sqlite3.connect(str(db_path_obj))
        conn.execute("PRAGMA foreign_keys = ON;")
        cursor = conn.cursor()
        
        # Create ASDRiskClassifications table
        schema_script = """
        -- ASD Risk Classifications Table
        CREATE TABLE IF NOT EXISTS ASDRiskClassifications (
            classification_id TEXT PRIMARY KEY,
            deduplicated_event_id TEXT UNIQUE NOT NULL,
            severity_category VARCHAR(2) NOT NULL CHECK(severity_category IN ('C1', 'C2', 'C3', 'C4', 'C5', 'C6')),
            primary_stakeholder_category VARCHAR(255) NOT NULL,
            impact_type VARCHAR(100) NOT NULL,
            reasoning_json TEXT NOT NULL,
            confidence_score REAL NOT NULL CHECK(confidence_score >= 0.0 AND confidence_score <= 1.0),
            model_used VARCHAR(50) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (deduplicated_event_id) REFERENCES DeduplicatedEvents(deduplicated_event_id) ON DELETE CASCADE
        );
        
        -- Create index for fast lookups
        CREATE INDEX IF NOT EXISTS idx_asd_risk_dedup_event_id 
        ON ASDRiskClassifications(deduplicated_event_id);
        
        -- Create index for severity category queries
        CREATE INDEX IF NOT EXISTS idx_asd_risk_severity 
        ON ASDRiskClassifications(severity_category);
        
        -- Create index for stakeholder category queries
        CREATE INDEX IF NOT EXISTS idx_asd_risk_stakeholder 
        ON ASDRiskClassifications(primary_stakeholder_category);
        """
        
        cursor.executescript(schema_script)
        conn.commit()
        
        # Verify table was created
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='ASDRiskClassifications'
        """)
        
        if cursor.fetchone():
            print("✅ ASD Risk Classification schema added successfully")
            
            # Check if table has expected columns
            cursor.execute("PRAGMA table_info(ASDRiskClassifications)")
            columns = [row[1] for row in cursor.fetchall()]
            print(f"   Table columns: {', '.join(columns)}")
            
            conn.close()
            return True
        else:
            print("❌ Failed to create ASDRiskClassifications table")
            conn.close()
            return False
            
    except sqlite3.Error as e:
        print(f"❌ Database error: {e}")
        if conn:
            conn.close()
        return False
    except Exception as e:
        print(f"❌ Error: {e}")
        if conn:
            conn.close()
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='Add ASD Risk Classification schema to database',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument(
        '--db-path',
        type=str,
        default='instance/cyber_events.db',
        help='Path to the SQLite database (default: instance/cyber_events.db)'
    )
    
    args = parser.parse_args()
    
    success = add_asd_risk_schema(args.db_path)
    
    if success:
        print("\n✅ Schema setup complete!")
        sys.exit(0)
    else:
        print("\n❌ Schema setup failed!")
        sys.exit(1)

