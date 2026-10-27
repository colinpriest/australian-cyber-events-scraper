"""Back up (or restore) the project database correctly, with verification.

Use this instead of copying ``cyber_events.db``: the database runs in WAL mode
and a file copy misses committed changes still in ``cyber_events.db-wal``.
See ``cyber_data_collector/utils/db_backup.py``.

Usage (from the project root):
    python scripts/backup_database.py                                  # -> instance/cyber_events.db.backup.<timestamp>
    python scripts/backup_database.py --to instance/before_refresh.db
    python scripts/backup_database.py --restore instance/before_refresh.db
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.utils.db_backup import BackupError, backup_database, restore_database  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="instance/cyber_events.db")
    parser.add_argument("--to", default=None, help="Backup file (default: <db>.backup.<timestamp>)")
    parser.add_argument("--restore", default=None, help="Restore --db from this backup (nothing else may be writing)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        if args.restore:
            counts = restore_database(args.restore, args.db)
            print(f"Restored {args.db} from {args.restore}: {sum(counts.values())} rows verified")
        else:
            target = args.to or f"{args.db}.backup.{datetime.now():%Y%m%d_%H%M%S}"
            counts = backup_database(args.db, target)
            print(f"Backed up {args.db} to {target}: {len(counts)} tables, {sum(counts.values())} rows verified")
    except BackupError as exc:
        print(f"FAILED: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
