"""Operations aimed at one database never touch another.

``rebuild --force --db-path scratch.db`` used to wipe the default database
(the wiper hard-coded its path) and then rebuild scratch.db.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import pipeline
from scripts.wipe_database import DatabaseRecordWiper


def _make_db(path: Path, rows: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, raw_title TEXT)")
    conn.executemany("INSERT INTO RawEvents VALUES (?, ?)", [(f"r{i}", "t") for i in range(rows)])
    conn.commit()
    conn.close()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM RawEvents").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project root holding a default database, with no DATABASE_URL set."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    default = tmp_path / "instance" / "cyber_events.db"
    _make_db(default)
    return tmp_path, default


def test_rebuild_with_db_path_leaves_the_default_database_untouched(project, monkeypatch):
    root, default = project
    scratch = root / "scratch.db"
    _make_db(scratch)
    before = _digest(default)
    seen = {}
    monkeypatch.setattr(pipeline, "_run_pipeline", lambda args: seen.setdefault("db", args.db_path) and 0)

    parsed = SimpleNamespace(force=True, dry_run=False, db_path=str(scratch), source=None,
                             max_events=1, days=0, out_dir=str(root / "dash"), skip_classification=True)
    assert pipeline._run_rebuild(parsed) == 0

    assert _digest(default) == before                 # not even opened for writing
    assert _rows(scratch) == 0                         # the requested database was wiped
    assert seen["db"] == str(scratch)                  # and is the one rebuilt
    assert list(root.glob("scratch.db.backup.*_pre_rebuild"))  # with a verified backup first


def test_wiper_follows_database_url(project, monkeypatch):
    root, default = project
    other = root / "elsewhere" / "events.db"
    _make_db(other)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{other.as_posix()}")
    before = _digest(default)

    wiper = DatabaseRecordWiper(dry_run=False, force=True)
    assert Path(wiper.db_path) == other
    assert wiper.wipe_sqlite_records()

    assert _rows(other) == 0
    assert _digest(default) == before


def test_explicit_path_never_reaches_postgres(project):
    root, _ = project
    assert DatabaseRecordWiper(db_path=str(root / "x.db")).wipe_postgres is False


def test_collector_uses_the_callers_database(monkeypatch):
    from cyber_data_collector import cyber_collector

    opened = []
    monkeypatch.setattr(cyber_collector, "DatabaseManager", lambda url: opened.append(url))
    monkeypatch.setattr(cyber_collector.CyberDataCollector, "_initialize_data_sources", lambda self: None)
    monkeypatch.setattr(cyber_collector, "LLMClassifier", lambda key: None)
    monkeypatch.setattr(cyber_collector, "EntityExtractor", lambda classifier: None)
    config = SimpleNamespace(max_threads=1)
    cyber_collector.CyberDataCollector(config, ".env", db_path="scratch/run.db")
    assert opened == ["sqlite:///scratch/run.db"]


def test_unsupported_database_url_is_an_error_not_the_default(project, monkeypatch):
    """Reviewer finding 39: it used to select instance/cyber_events.db silently."""
    from cyber_data_collector.utils.config_manager import resolve_db_path
    monkeypatch.setenv("DATABASE_URL", "postgresql://user@host/events")
    with pytest.raises(ValueError, match="Unsupported DATABASE_URL"):
        resolve_db_path(None, env_path=project[0] / "no.env")
