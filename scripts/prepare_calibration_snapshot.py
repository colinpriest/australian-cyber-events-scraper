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


def carry_over(copy: str, live: str) -> dict:
    """Insert the live database's stored timelines and corroborations into ``copy`` (idempotent).

    Returns ``{table: rows added}``. Safe to repeat on a copy that already holds
    judgements: nothing existing is touched, so a recalibration can be resumed.
    """
    conn = sqlite3.connect(copy)
    try:
        org_timeline.ensure_schema(conn)
        corroboration.ensure_schema(conn)
        jev.ensure_schema(conn)
        conn.execute("ATTACH DATABASE ? AS live", (str(Path(live).resolve()),))
        added = {table: conn.execute(f"INSERT OR IGNORE INTO main.{table} SELECT * FROM live.{table}").rowcount
                 for table in CARRIED}
        conn.commit()
        conn.execute("DETACH DATABASE live")
        return added
    finally:
        conn.close()


def prepare(backup: str, copy: str, live: str) -> dict:
    """Copy ``backup`` to ``copy`` and carry the live research over; returns the rows carried."""
    # The backup API, not a file copy: a backup taken by copying a WAL-mode
    # database's main file may be missing committed changes (db_backup).
    backup_database(backup, copy)
    return carry_over(copy, live)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("backup")
    parser.add_argument("copy")
    parser.add_argument("--live", default="instance/cyber_events.db")
    args = parser.parse_args(argv)

    for table, added in prepare(args.backup, args.copy, args.live).items():
        print(f"{table}: {added} row(s) carried over")
    print(f"Calibration snapshot ready: {args.copy}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
