#!/usr/bin/env python3
"""Clear confidence values that were defaults, not scores - only where provenance proves it.

    python scripts/repair_confidence_defaults.py --dry-run     # what would be cleared, and what is only suspect
    python scripts/repair_confidence_defaults.py               # set the proven defaults to NULL

Discovery used to store 0.7 for an event with no confidence and constant 0.8 /
0.6 for the entities of the single-record path; those read as model output.
A value is cleared only when the code path that wrote the default is proved from
the database (see ``cyber_data_collector/processing/confidence_defaults.py``);
every other 0.7 / 0.8 is reported as ambiguous and left alone. Each cleared value
is recorded in ``ConfidenceRepairs``. Idempotent: a second run finds nothing.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cyber_data_collector.processing import confidence_defaults  # noqa: E402
from cyber_data_collector.utils.config_manager import resolve_db_path  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", default=None, help="Database (default: DATABASE_URL / instance/cyber_events.db)")
    parser.add_argument("--dry-run", action="store_true", help="Report only; change nothing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    db_path = resolve_db_path(args.db_path)
    if not Path(db_path).exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(db_path)
    try:
        report = confidence_defaults.repair(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    print(("DRY RUN - nothing changed.\n" if args.dry_run else "") + "\n".join(report.lines()))
    if report.proven and not args.dry_run:
        print(f"\nCleared {len(report.proven)} value(s); each is recorded in ConfidenceRepairs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
