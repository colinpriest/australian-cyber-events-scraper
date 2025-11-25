#!/usr/bin/env python3
"""
Fix unrealistic records_affected values in the database.

This script finds and corrects records_affected values that exceed
the realistic maximum of 1 billion (1,000,000,000) records.
"""

import sqlite3
import sys
from datetime import datetime
from cyber_data_collector.utils.validation import validate_records_affected


def fix_records_affected(db_path: str = 'instance/cyber_events.db', dry_run: bool = False):
    """
    Find and fix unrealistic records_affected values.

    Args:
        db_path: Path to SQLite database
        dry_run: If True, only report issues without updating
    """

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Find events with potentially unrealistic values
    query = """
        SELECT
            deduplicated_event_id,
            title,
            event_date,
            victim_organization_name,
            records_affected
        FROM DeduplicatedEvents
        WHERE status = 'Active'
            AND records_affected IS NOT NULL
        ORDER BY records_affected DESC
    """

    cursor.execute(query)
    rows = cursor.fetchall()

    print(f"\n{'='*80}")
    print(f"RECORDS AFFECTED VALIDATION REPORT")
    print(f"{'='*80}")
    print(f"Database: {db_path}")
    print(f"Mode: {'DRY RUN (no changes)' if dry_run else 'LIVE UPDATE'}")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*80}\n")

    total_events = len(rows)
    rejected_count = 0
    valid_count = 0

    adjustments = []

    for row in rows:
        event_id = row['deduplicated_event_id']
        title = row['title']
        original_value = row['records_affected']

        # Apply validation
        validated_value = validate_records_affected(original_value, title)

        if validated_value != original_value:
            adjustments.append({
                'event_id': event_id,
                'title': title,
                'original': original_value,
                'validated': validated_value,
                'organization': row['victim_organization_name'],
                'date': row['event_date']
            })
            rejected_count += 1
        else:
            valid_count += 1

    # Print summary
    print(f"SUMMARY:")
    print(f"  Total events with records_affected: {total_events:,}")
    print(f"  Valid (no change needed): {valid_count:,}")
    print(f"  Rejected (set to NULL): {rejected_count:,}")
    print(f"\n{'='*80}\n")

    if adjustments:
        print(f"ADJUSTMENTS NEEDED:\n")

        for adj in adjustments:
            print(f"Event: {adj['title']}")
            print(f"  Organization: {adj['organization']}")
            print(f"  Date: {adj['date']}")
            print(f"  Original Value: {adj['original']:,}" if adj['original'] else "  Original Value: None")
            print(f"  Action: REJECTED (set to NULL)")
            print()

        if not dry_run:
            print(f"{'='*80}")
            print(f"APPLYING UPDATES...")
            print(f"{'='*80}\n")

            update_query = """
                UPDATE DeduplicatedEvents
                SET records_affected = ?,
                    updated_at = ?
                WHERE deduplicated_event_id = ?
            """

            updated = 0
            for adj in adjustments:
                try:
                    cursor.execute(
                        update_query,
                        (adj['validated'], datetime.now().isoformat(), adj['event_id'])
                    )
                    updated += 1
                except Exception as e:
                    print(f"ERROR updating {adj['event_id']}: {e}")

            conn.commit()
            print(f"✓ Updated {updated} events successfully\n")
        else:
            print(f"{'='*80}")
            print(f"DRY RUN - No changes made to database")
            print(f"Run without --dry-run to apply these fixes")
            print(f"{'='*80}\n")
    else:
        print("No adjustments needed - all values are within realistic limits!\n")

    conn.close()

    return {
        'total': total_events,
        'valid': valid_count,
        'rejected': rejected_count
    }


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description='Fix unrealistic records_affected values in the database',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        '--db-path',
        default='instance/cyber_events.db',
        help='Path to SQLite database (default: instance/cyber_events.db)'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be changed without updating database'
    )

    args = parser.parse_args()

    try:
        result = fix_records_affected(args.db_path, args.dry_run)
        sys.exit(0)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
