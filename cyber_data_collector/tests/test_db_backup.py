"""Backups of a WAL-mode database must include committed WAL content, and be verified."""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from cyber_data_collector.utils.db_backup import BackupError, backup_database, restore_database


@pytest.fixture
def wal_db(tmp_path):
    """A WAL database with committed rows still in the -wal file (no checkpoint yet)."""
    path = tmp_path / "live.db"
    writer = sqlite3.connect(str(path))
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("PRAGMA wal_autocheckpoint = 0")   # keep everything in the WAL
    writer.execute("CREATE TABLE Events (id INTEGER PRIMARY KEY, title TEXT)")
    writer.executemany("INSERT INTO Events (title) VALUES (?)", [(f"event {i}",) for i in range(50)])
    writer.commit()
    yield path, writer
    writer.close()


def test_copying_the_main_file_loses_committed_wal_data(wal_db, tmp_path):
    path, _ = wal_db
    copied = tmp_path / "copied.db"
    shutil.copyfile(path, copied)
    conn = sqlite3.connect(str(copied))
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    rows = conn.execute("SELECT COUNT(*) FROM Events").fetchone()[0] if "Events" in tables else 0
    assert rows < 50   # the defect this module exists to prevent


def test_backup_includes_wal_content_and_is_verified(wal_db, tmp_path):
    path, writer = wal_db
    counts = backup_database(path, tmp_path / "backup.db")
    assert counts == {"Events": 50}
    conn = sqlite3.connect(str(tmp_path / "backup.db"))
    assert conn.execute("SELECT COUNT(*) FROM Events").fetchone()[0] == 50
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_restore_is_verified(wal_db, tmp_path):
    path, writer = wal_db
    backup_database(path, tmp_path / "backup.db")
    restored = tmp_path / "restored.db"
    assert restore_database(tmp_path / "backup.db", restored) == {"Events": 50}


def test_missing_source_raises(tmp_path):
    with pytest.raises(BackupError):
        backup_database(tmp_path / "nope.db", tmp_path / "backup.db")
