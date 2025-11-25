"""
Retrospective cleanup script for unrealistic high record counts.

Applies the new validation rule that rejects >20M records for non-international organizations.
"""

import sqlite3
import logging
from cyber_data_collector.utils.validation import validate_records_affected

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def cleanup_high_record_counts(db_path: str = 'instance/cyber_events.db', dry_run: bool = False):
    """Clean up unrealistic high record counts in the database."""

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Get all events with records_affected > 20 million
    query = """
    SELECT deduplicated_event_id, title, records_affected
    FROM DeduplicatedEvents
    WHERE records_affected > 20000000
    AND status = 'Active'
    ORDER BY records_affected DESC
    """

    cursor.execute(query)
    high_count_events = cursor.fetchall()

    logger.info(f"Found {len(high_count_events)} events with records_affected > 20 million")

    corrected_count = 0
    kept_count = 0

    for event in high_count_events:
        event_id = event['deduplicated_event_id']
        title = event['title']
        original_count = event['records_affected']

        # Validate using the new rule
        validated_count = validate_records_affected(original_count, title)

        if validated_count is None:
            # Record failed validation - set to NULL
            logger.info(
                f"CORRECTING: '{title}' - "
                f"Record count {original_count:,} rejected (non-international org)"
            )

            if not dry_run:
                cursor.execute(
                    "UPDATE DeduplicatedEvents SET records_affected = NULL WHERE deduplicated_event_id = ?",
                    (event_id,)
                )
            corrected_count += 1
        else:
            # Record passed validation (international organization)
            logger.info(
                f"KEEPING: '{title}' - "
                f"Record count {original_count:,} accepted (international org)"
            )
            kept_count += 1

    if not dry_run:
        conn.commit()
        logger.info(f"Database updated: {corrected_count} events corrected, {kept_count} events kept")
    else:
        logger.info(f"DRY RUN: Would correct {corrected_count} events, keep {kept_count} events")

    conn.close()

    return corrected_count, kept_count


if __name__ == '__main__':
    import sys

    dry_run = '--dry-run' in sys.argv

    if dry_run:
        logger.info("Running in DRY RUN mode - no database changes will be made")
    else:
        logger.info("Running in LIVE mode - database will be updated")

    corrected, kept = cleanup_high_record_counts(dry_run=dry_run)

    logger.info(f"\nSummary:")
    logger.info(f"  Corrected (set to NULL): {corrected}")
    logger.info(f"  Kept (international orgs): {kept}")
    logger.info(f"  Total processed: {corrected + kept}")
