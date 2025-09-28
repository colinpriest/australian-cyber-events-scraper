#!/usr/bin/env python3
"""
Add MonthProcessed table to track which months have been processed
"""

import sqlite3
from pathlib import Path
from datetime import datetime


def add_month_processed_table(db_path: str = "instance/cyber_events.db"):
    """Add MonthProcessed table to existing database"""
    print("Adding MonthProcessed table...")

    db_path = Path(db_path)
    if not db_path.exists():
        print(f"Database {db_path} does not exist!")
        return False

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # Create MonthProcessed table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS MonthProcessed (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                is_processed BOOLEAN DEFAULT FALSE,
                processed_at TIMESTAMP NULL,
                total_raw_events INTEGER DEFAULT 0,
                total_enriched_events INTEGER DEFAULT 0,
                processing_notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(year, month)
            )
        """)

        # Create index for faster lookups
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_month_processed_year_month
            ON MonthProcessed(year, month)
        """)

        conn.commit()
        print("MonthProcessed table added successfully")

        # Show the table structure
        cursor.execute("PRAGMA table_info(MonthProcessed)")
        columns = cursor.fetchall()
        print("\nMonthProcessed table structure:")
        for col in columns:
            print(f"  {col[1]} ({col[2]})")

        return True

    except sqlite3.Error as e:
        print(f"Error adding MonthProcessed table: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


if __name__ == "__main__":
    add_month_processed_table()