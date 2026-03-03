#!/usr/bin/env python3
"""
Project status reporter for the Australian Cyber Events database.

Reports:
- Last data ingest timestamp (latest RawEvents.discovered_at)
- Latest event in the database (latest EnrichedEvents by event_date/created_at)
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Optional, Tuple

from cyber_data_collector.utils import ConfigManager


def _resolve_db_path(db_path: Optional[str]) -> str:
    env_config = ConfigManager(".env").load()
    return db_path or env_config.get("DATABASE_PATH") or "instance/cyber_events.db"


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def _fetch_last_ingest(conn: sqlite3.Connection) -> Optional[str]:
    if not _table_exists(conn, "RawEvents"):
        return None
    cursor = conn.execute("SELECT MAX(discovered_at) AS last_discovered FROM RawEvents")
    row = cursor.fetchone()
    return row["last_discovered"] if row else None


def _fetch_latest_event(conn: sqlite3.Connection) -> Optional[Tuple[str, Optional[str], Optional[str]]]:
    if not _table_exists(conn, "EnrichedEvents"):
        return None
    try:
        cursor = conn.execute(
            """
            SELECT title, event_date, created_at
            FROM EnrichedEvents
            WHERE status = 'Active'
            ORDER BY event_date IS NULL, event_date DESC, created_at DESC
            LIMIT 1
            """
        )
    except sqlite3.OperationalError:
        # Fallback query without the status column if the column does not exist
        cursor = conn.execute(
            """
            SELECT title, event_date, created_at
            FROM EnrichedEvents
            ORDER BY event_date IS NULL, event_date DESC, created_at DESC
            LIMIT 1
            """
        )
    row = cursor.fetchone()
    if not row:
        return None
    return row["title"], row["event_date"], row["created_at"]


def report_status(db_path: Optional[str]) -> int:
    resolved_path = Path(_resolve_db_path(db_path))
    if not resolved_path.exists():
        print(f"Database not found: {resolved_path}")
        return 1

    conn = sqlite3.connect(resolved_path)
    conn.row_factory = sqlite3.Row
    try:
        last_ingest = _fetch_last_ingest(conn)
        latest_event = _fetch_latest_event(conn)
    finally:
        conn.close()

    print("📌 Project Status")
    print(f"Database: {resolved_path}")

    if last_ingest:
        print(f"Last ingest: {last_ingest}")
    else:
        print("Last ingest: No RawEvents records found")

    if latest_event:
        title, event_date, created_at = latest_event
        print("Latest event:")
        print(f"  Title: {title}")
        print(f"  Event date: {event_date or 'Unknown'}")
        print(f"  Added: {created_at}")
    else:
        print("Latest event: No EnrichedEvents records found")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Report recent ingestion status for the project database.")
    parser.add_argument("--db-path", default=None, help="Path to SQLite database file.")
    args = parser.parse_args()
    return report_status(args.db_path)


if __name__ == "__main__":
    raise SystemExit(main())
