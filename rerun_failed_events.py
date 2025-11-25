"""
Rerun Enrichment on Failed Events (0.00 confidence)

This script re-runs the enrichment pipeline on events that previously failed
content extraction (0.00 confidence) using the improved PDF and Playwright extraction.
"""

import sqlite3
import logging
import sys
import os
from datetime import datetime

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('phase2_rerun_failed_urls.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)


def get_failed_events():
    """Get list of events with 0.00 confidence from audit trail"""
    conn = sqlite3.connect('instance/cyber_events.db')
    cursor = conn.cursor()

    # Query events that failed (0.00 confidence) from most recent enrichment
    # Join with RawEvents to get the source URL
    query = """
    SELECT DISTINCT
        e.enriched_event_id,
        r.raw_event_id,
        r.raw_title,
        r.source_url,
        a.error_message,
        a.created_at
    FROM EnrichmentAuditTrail a
    JOIN EnrichedEvents e ON a.enriched_event_id = e.enriched_event_id
    JOIN RawEvents r ON e.raw_event_id = r.raw_event_id
    WHERE a.final_confidence = 0.0
        AND a.error_message LIKE '%Failed to extract sufficient content%'
        AND e.status = 'Active'
        AND r.source_url IS NOT NULL
        AND r.source_url != ''
    ORDER BY a.created_at DESC
    """

    cursor.execute(query)
    results = cursor.fetchall()

    conn.close()

    # Convert to list of dicts
    events = []
    for row in results:
        events.append({
            'enriched_event_id': row[0],
            'raw_event_id': row[1],
            'title': row[2],
            'url': row[3],
            'error_message': row[4],
            'failed_at': row[5]
        })

    return events


def main():
    logger.info("=" * 100)
    logger.info("PHASE 2 RERUN: Failed URLs with Improved Extraction")
    logger.info("=" * 100)

    # Get failed events
    logger.info("Querying failed events from database...")
    failed_events = get_failed_events()

    logger.info(f"Found {len(failed_events)} events with failed content extraction (0.00 confidence)")

    if len(failed_events) == 0:
        logger.info("No failed events to rerun!")
        return

    # Save list to file for batch_enrichment.py
    logger.info(f"Saving failed event IDs to file for batch processing...")

    with open('failed_events_to_rerun.txt', 'w') as f:
        for event in failed_events:
            f.write(f"{event['enriched_event_id']}\n")

    logger.info(f"Saved {len(failed_events)} event IDs to failed_events_to_rerun.txt")

    # Print sample of failed events
    logger.info("\nSample of failed events (first 10):")
    for i, event in enumerate(failed_events[:10], 1):
        logger.info(f"  {i}. {event['title'][:70]}")
        logger.info(f"     URL: {event['url'][:80]}")
        logger.info(f"     Error: {event['error_message'][:80]}")
        logger.info("")

    # Now we need to use batch_enrichment.py with these specific event IDs
    # But batch_enrichment.py loads events differently - it queries based on missing victims
    # We need to modify the approach

    logger.info("=" * 100)
    logger.info("RERUN APPROACH")
    logger.info("=" * 100)
    logger.info("")
    logger.info("The batch_enrichment.py script will be modified to:")
    logger.info("  1. Load events from failed_events_to_rerun.txt")
    logger.info("  2. Re-fetch content using improved PDF + Playwright extraction")
    logger.info("  3. Re-run enrichment pipeline on successfully extracted content")
    logger.info("  4. Update audit trail with new results")
    logger.info("")
    logger.info(f"Estimated time: {len(failed_events) * 45 / 3600:.1f} hours")
    logger.info(f"Estimated cost: ${len(failed_events) * 0.15:.2f} (if content successfully extracted)")
    logger.info("")

    return failed_events


if __name__ == '__main__':
    failed_events = main()

    if failed_events:
        print("\n" + "=" * 100)
        print("NEXT STEP")
        print("=" * 100)
        print(f"\nReady to rerun enrichment on {len(failed_events)} failed events")
        print("Use the modified batch_enrichment.py to process these events")
