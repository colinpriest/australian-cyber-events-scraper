"""Copy a labelling-time database backup for recalibration, carrying stored research over.

Labels refer to the database as it was when they were made: once events are
rejected or merged, the labelled items are no longer active and cannot be
re-judged in the live database. Recalibrating a new question version
therefore runs on a copy of the backup taken at labelling time. Stored
organisation timelines and corroboration searches are copied in from the live
database (a live search cannot be replayed, and re-searching would make the
evaluation differ from what the pipeline uses).

Usage (from the project root):
    python scripts/prepare_calibration_snapshot.py <backup.db> <copy.db> [--live instance/cyber_events.db]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.dedup import corroboration, org_timeline  # noqa: E402
from cyber_data_collector.utils import jev  # noqa: E402
from cyber_data_collector.utils.db_backup import backup_database  # noqa: E402

CARRIED = ("OrganisationTimelines", "EventCorroborations")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("backup")
    parser.add_argument("copy")
    parser.add_argument("--live", default="instance/cyber_events.db")
    args = parser.parse_args(argv)

    # The backup API, not a file copy: a backup taken by copying a WAL-mode
    # database's main file may be missing committed changes (db_backup).
    backup_database(args.backup, args.copy)
    conn = sqlite3.connect(args.copy)
    org_timeline.ensure_schema(conn)
    corroboration.ensure_schema(conn)
    jev.ensure_schema(conn)
    conn.execute("ATTACH DATABASE ? AS live", (str(Path(args.live).resolve()),))
    for table in CARRIED:
        added = conn.execute(f"INSERT OR IGNORE INTO main.{table} SELECT * FROM live.{table}").rowcount
        print(f"{table}: {added} row(s) carried over")
    conn.commit()
    conn.execute("DETACH DATABASE live")
    conn.close()
    print(f"Calibration snapshot ready: {args.copy}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
