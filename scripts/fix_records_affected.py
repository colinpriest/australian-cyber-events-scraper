"""Re-validate stored records_affected values against the recorded victim.

Global vendor breaches get their worldwide total attributed to the Australian
customer caught up in them. The University of Melbourne (roughly 50,000
students) was recorded as losing 275,000,000 records - the global
Canvas/Instructure figure - because the original validation inferred the
organisation from the event TITLE, which named the vendor, rather than from the
victim field.

This re-runs ``validate_records_affected`` over stored rows with the victim
supplied, and clears values that cannot be true. A cleared value means
"unknown", which is honest; substituting a guess would not be.

Usage:
    python scripts/fix_records_affected.py --dry-run
    python scripts/fix_records_affected.py
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.utils.validation import validate_records_affected

logger = logging.getLogger(__name__)

# Values below this are left alone; see find_implausible().
LOW_VALUE_FLOOR = 50

DEFAULT_DB = "instance/cyber_events.db"


def find_implausible(conn: sqlite3.Connection) -> List[Tuple]:
    """Return rows whose stored records_affected fails victim-aware validation."""
    rows = conn.execute(
        """
        SELECT deduplicated_event_id, title, records_affected,
               victim_organization_name, status
        FROM DeduplicatedEvents
        WHERE records_affected IS NOT NULL
        """
    ).fetchall()

    bad = []
    for dedup_id, title, value, victim, status in rows:
        validated = validate_records_affected(value, title or "", victim or "")
        if validated == value:
            continue
        # Only correct OVER-statements. The same validator also rejects values
        # below 50 as suspected unit-parsing errors, but small counts are often
        # exactly right ("6 Australian senators confirm being targeted", and an
        # OMARA notification that Perplexity separately confirmed at 6). Nulling
        # those would destroy correct data to fix a different problem.
        if value is not None and value < LOW_VALUE_FLOOR:
            continue
        bad.append((dedup_id, title, value, validated, victim, status))
    return bad


def repair(conn: sqlite3.Connection, dry_run: bool = False) -> int:
    bad = find_implausible(conn)
    print(f"{len(bad)} event(s) hold an implausible records_affected value")
    for _, title, old, new, victim, status in sorted(bad, key=lambda r: -(r[2] or 0))[:25]:
        shown = f"{new:,}" if new is not None else "unknown"
        print(f"  {old:>14,} -> {shown:>10}  {str(victim)[:34]:36} "
              f"{str(title)[:44]!r} [{status}]")

    if dry_run:
        print("\nDry run - nothing written.")
        return 0

    for dedup_id, _, _, new, _, _ in bad:
        conn.execute(
            "UPDATE DeduplicatedEvents SET records_affected = ?, updated_at = "
            "CURRENT_TIMESTAMP WHERE deduplicated_event_id = ?",
            (new, dedup_id),
        )
    conn.commit()
    print(f"\nCorrected {len(bad)} event(s).")
    return len(bad)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-validate records_affected against the recorded victim")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    conn = sqlite3.connect(args.db)
    try:
        repair(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
