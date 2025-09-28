#!/usr/bin/env python3
"""
Quick script to reprocess events with incorrect dates using the improved LLM extraction.
This will update existing enriched events that have today's date when they should have
actual incident dates extracted from the scraped content.
"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path

# Add parent directory to path for imports
sys.path.append(str(Path(__file__).parent))

from cyber_event_data_v2 import CyberEventDataV2
from llm_extractor import extract_event_details_with_llm


def get_events_with_wrong_dates(db):
    """Get enriched events that have today's date (likely wrong)."""

    with db._lock:
        cursor = db._conn.cursor()

        # Get events with today's date - these are likely wrong
        today = datetime.now().date().isoformat()

        cursor.execute("""
            SELECT ee.enriched_event_id, ee.title, ee.event_date, ee.raw_event_id,
                   re.raw_content, re.raw_title, re.source_url
            FROM EnrichedEvents ee
            JOIN RawEvents re ON ee.raw_event_id = re.raw_event_id
            WHERE ee.event_date = ? AND re.raw_content IS NOT NULL
        """, (today,))

        return [dict(row) for row in cursor.fetchall()]


def update_event_date(db, enriched_event_id, new_date):
    """Update the event_date for an enriched event."""

    with db._lock:
        cursor = db._conn.cursor()
        cursor.execute("""
            UPDATE EnrichedEvents
            SET event_date = ?, updated_at = CURRENT_TIMESTAMP
            WHERE enriched_event_id = ?
        """, (new_date.isoformat() if new_date else None, enriched_event_id))
        db._conn.commit()


async def reprocess_event_dates():
    """Main function to reprocess event dates."""

    db = CyberEventDataV2()

    print("Finding events with potentially incorrect dates...")
    events_to_fix = get_events_with_wrong_dates(db)

    if not events_to_fix:
        print("No events found with today's date - all dates appear correct.")
        return

    print(f"Found {len(events_to_fix)} events with today's date to reprocess...")

    fixed_count = 0

    for event in events_to_fix:
        print(f"\nReprocessing: {event['title'][:60]}...")

        if not event['raw_content']:
            print("No scraped content available, skipping...")
            continue

        try:
            # Extract details including the correct event date
            llm_data = extract_event_details_with_llm(event['raw_content'])

            if llm_data and llm_data.event_date:
                old_date = event['event_date']
                new_date = llm_data.event_date

                if str(new_date) != old_date:
                    print(f"Updating date: {old_date} -> {new_date}")
                    update_event_date(db, event['enriched_event_id'], new_date)
                    fixed_count += 1
                else:
                    print("Date unchanged (LLM extracted same date)")
            else:
                print("LLM couldn't extract a date from content")

        except Exception as e:
            print(f"Error processing {event['title'][:30]}: {e}")

    print(f"\nReprocessing complete! Fixed {fixed_count} event dates.")

    db.close()


if __name__ == "__main__":
    asyncio.run(reprocess_event_dates())