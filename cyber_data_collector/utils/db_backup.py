"""Back up and restore the SQLite database correctly, and prove it worked.

The database runs in WAL mode: committed transactions live in
``cyber_events.db-wal`` until a checkpoint copies them into the main file.
Copying ``cyber_events.db`` alone - which every backup did until 2026-10-27
(``shutil.copy2`` in ``run_global_deduplication.py``, and the manual
``.backup.*`` copies) - silently drops whatever the WAL holds, so a "backup"
could predate its own label by any number of committed changes. A reviewer
reproduced committed data missing from such a copy.

:func:`backup_database` uses SQLite's online backup API, which copies a
consistent snapshot including committed WAL content while other connections
read and write. Every backup is then verified: ``PRAGMA integrity_check`` on
the copy, and per-table row counts equal to the source's at the moment of the
copy. :func:`restore_database` restores the same way and verifies the result.
A backup that cannot be verified raises - an unverified backup is not one.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Dict, Union

logger = logging.getLogger(__name__)

PathLike = Union[str, Path]


class BackupError(RuntimeError):
    """A backup or restore could not be verified."""


def _table_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}


def _copy_and_verify(source: PathLike, destination: PathLike, what: str) -> Dict[str, int]:
    source, destination = Path(source), Path(destination)
    if not source.exists():
        raise BackupError(f"{what}: source {source} does not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(source))
    try:
        # One read transaction for both the copy and the counts, so they
        # describe the same instant even while a writer is active.
        src.execute("BEGIN")
        expected = _table_counts(src)
        dst = sqlite3.connect(str(destination))
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode = DELETE")   # a standalone file, no -wal beside it
            check = dst.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise BackupError(f"{what}: integrity_check on {destination} returned {check!r}")
            actual = _table_counts(dst)
        finally:
            dst.close()
        src.execute("COMMIT")
    finally:
        src.close()
    if actual != expected:
        differing = {t: (expected.get(t), actual.get(t)) for t in set(expected) | set(actual)
                     if expected.get(t) != actual.get(t)}
        raise BackupError(f"{what}: row counts differ between {source} and {destination}: {differing}")
    return expected


def backup_database(source: PathLike, destination: PathLike) -> Dict[str, int]:
    """Copy ``source`` to ``destination`` via the backup API and verify it.

    Returns the per-table row counts the backup was verified against.
    """
    counts = _copy_and_verify(source, destination, "backup")
    logger.info("Verified backup %s (%d tables, %d rows)", destination, len(counts), sum(counts.values()))
    return counts


def restore_database(backup: PathLike, destination: PathLike) -> Dict[str, int]:
    """Restore ``destination`` from ``backup`` via the backup API and verify the result.

    Writes through SQLite, so a live database's WAL is handled correctly;
    callers must make sure nothing else is writing to ``destination``.
    """
    counts = _copy_and_verify(backup, destination, "restore")
    logger.info("Verified restore of %s from %s", destination, backup)
    return counts
