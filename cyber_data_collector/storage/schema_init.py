"""Create or upgrade a database to the schema the pipeline expects.

``python pipeline.py init [--db-path PATH]`` runs this. It applies the numbered
SQL files in ``storage/migrations/`` in order, recording each in
``SchemaMigrations`` (version, checksum, time), then the idempotent schema
steps the modules own (dedup v3 tables and columns, Jev judgements, timelines,
corroborations, page classifications, record fingerprints, the discovery
retry queue, enrichment tracking, the ASD severity constraint). Safe to run on
an existing database: every statement is ``IF NOT EXISTS`` or guarded, and an applied migration is never re-run - but a
migration file edited after it was applied is reported, because the database
then no longer matches the file that describes it.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
CORE_TABLES = ("RawEvents", "EnrichedEvents", "DeduplicatedEvents", "EventDeduplicationMap",
               "ASDRiskClassifications", "EntitiesV2")


@dataclass
class InitReport:
    applied: List[str] = field(default_factory=list)
    already_applied: List[str] = field(default_factory=list)
    changed_since_applied: List[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{len(self.applied)} migration(s) applied", f"{len(self.already_applied)} already present"]
        if self.changed_since_applied:
            parts.append(f"edited since applied: {', '.join(self.changed_since_applied)}")
        return "; ".join(parts)


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def initialize_database(db_path: str) -> InitReport:
    """Bring ``db_path`` (created if missing) up to the current schema."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    report = InitReport()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS SchemaMigrations (
                version    TEXT PRIMARY KEY,
                checksum   TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )""")
        applied = dict(conn.execute("SELECT version, checksum FROM SchemaMigrations"))
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = path.stem
            checksum = _checksum(path)
            if version in applied:
                report.already_applied.append(version)
                if applied[version] != checksum:
                    report.changed_since_applied.append(version)
                continue
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO SchemaMigrations VALUES (?, ?, ?)",
                         (version, checksum, datetime.now().isoformat(timespec="seconds")))
            conn.commit()
            report.applied.append(version)
        _module_schema_steps(conn)
        conn.commit()
        missing = [t for t in CORE_TABLES
                   if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()]
        if missing:
            raise RuntimeError(f"schema incomplete after init: missing {', '.join(missing)}")
    finally:
        conn.close()
    # Enrichment-tracking columns are added by the storage layer on connect.
    from cyber_data_collector.storage.cyber_event_data_v2 import CyberEventDataV2
    CyberEventDataV2(db_path).close()
    logger.info("Database %s initialised: %s", db_path, report.summary())
    return report


def _module_schema_steps(conn: sqlite3.Connection) -> None:
    """The schema steps each module runs for itself, run once here up front."""
    from cyber_data_collector.dedup import (
        contextual_aliases, corroboration, entity_alias_verification, event_facts, input_history, org_timeline,
        page_classifier, record_fingerprints,
    )
    from cyber_data_collector.dedup.schema import migrate as migrate_dedup
    from cyber_data_collector.processing import (
        ai_involvement, cyberdaily_refetch, evidence_recovery, incident_dates, publication_dates,
        record_identity, retry_queue,
    )
    from cyber_data_collector.storage import asd_schema
    from cyber_data_collector.utils import jev

    migrate_dedup(conn)
    asd_schema.ensure_schema(conn)
    for module in (jev, org_timeline, corroboration, entity_alias_verification, contextual_aliases, page_classifier,
                   record_fingerprints, event_facts, retry_queue, ai_involvement, publication_dates,
                   evidence_recovery, cyberdaily_refetch, incident_dates, record_identity, input_history):
        module.ensure_schema(conn)
