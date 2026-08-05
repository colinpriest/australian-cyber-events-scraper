"""Additive schema migration for deduplication v3.

Every statement is CREATE ... IF NOT EXISTS, so running this against a live
database is safe and idempotent. Nothing here drops or rewrites existing
tables - the v3 tables sit alongside DeduplicatedEvents and can be populated
without disturbing the current pipeline.

Design notes:

* ``DedupDecisions`` is append-only. A decision is never updated in place;
  it is superseded by a newer row pointing back at it. That is what makes
  history reversible without re-running the pipeline.
* ``DedupOverrides`` is keyed on a *pair key* derived from the two enriched
  event ids, not on ``deduplicated_event_id``. Dedup ids are regenerated on
  every rebuild (they are fresh UUIDs), so anything keyed on them is lost the
  moment a rebuild happens - which is exactly how the previous provenance was
  lost. Enriched ids are stable, so overrides survive.
* ``EntityAliases`` is the fix for variant under-merging ("Optus Pty Limited"
  vs "Singtel Optus Pty Limited"): all variants resolve to one canonical key.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import List, Union

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3

DDL_STATEMENTS: List[str] = [
    # ------------------------------------------------------------------
    # Append-only decision ledger
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS DedupDecisions (
        decision_id            TEXT PRIMARY KEY,
        batch_id               TEXT NOT NULL,
        enriched_event_id      TEXT NOT NULL,
        deduplicated_event_id  TEXT,
        cluster_key            TEXT,
        action                 TEXT NOT NULL CHECK (action IN ('merge','keep_separate','revert')),
        decided_by             TEXT NOT NULL CHECK (decided_by IN ('rule','embedding','llm','human')),
        certainty              REAL CHECK (certainty IS NULL OR (certainty >= 0.0 AND certainty <= 1.0)),
        method                 TEXT,
        reasoning              TEXT,
        evidence_json          TEXT,
        superseded_by          TEXT,
        created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dd_batch ON DedupDecisions(batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_dd_enriched ON DedupDecisions(enriched_event_id)",
    "CREATE INDEX IF NOT EXISTS idx_dd_dedup ON DedupDecisions(deduplicated_event_id)",
    "CREATE INDEX IF NOT EXISTS idx_dd_live ON DedupDecisions(enriched_event_id, superseded_by)",

    # ------------------------------------------------------------------
    # Human overrides - keyed on stable enriched ids so they survive rebuilds
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS DedupOverrides (
        override_id              TEXT PRIMARY KEY,
        pair_key                 TEXT NOT NULL UNIQUE,
        left_enriched_event_id   TEXT NOT NULL,
        right_enriched_event_id  TEXT NOT NULL,
        verdict                  TEXT NOT NULL CHECK (verdict IN ('same','different')),
        reason                   TEXT,
        created_by               TEXT DEFAULT 'human',
        created_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        active                   INTEGER NOT NULL DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_do_left ON DedupOverrides(left_enriched_event_id)",
    "CREATE INDEX IF NOT EXISTS idx_do_right ON DedupOverrides(right_enriched_event_id)",
    "CREATE INDEX IF NOT EXISTS idx_do_active ON DedupOverrides(active)",

    # ------------------------------------------------------------------
    # Canonical entity naming
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS EntityAliases (
        alias_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        raw_name       TEXT NOT NULL UNIQUE,
        canonical_name TEXT NOT NULL,
        canonical_key  TEXT NOT NULL,
        source         TEXT NOT NULL DEFAULT 'rule' CHECK (source IN ('rule','llm','human','learned')),
        confidence     REAL,
        created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ea_key ON EntityAliases(canonical_key)",

    # ------------------------------------------------------------------
    # Row-level snapshots so a merge can be undone without a full rebuild
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS DedupSnapshots (
        snapshot_id            TEXT PRIMARY KEY,
        batch_id               TEXT NOT NULL,
        deduplicated_event_id  TEXT NOT NULL,
        payload_json           TEXT NOT NULL,
        created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ds_batch ON DedupSnapshots(batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_ds_dedup ON DedupSnapshots(deduplicated_event_id)",

    # ------------------------------------------------------------------
    # Calibration learned from human overrides
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS DedupCalibration (
        calibration_id  TEXT PRIMARY KEY,
        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        sample_size     INTEGER NOT NULL,
        false_merges    INTEGER NOT NULL,
        missed_merges   INTEGER NOT NULL,
        merge_threshold REAL NOT NULL,
        notes           TEXT
    )
    """,
]

# ----------------------------------------------------------------------
# Identity must key on something immutable
# ----------------------------------------------------------------------
#
# The original schema enforced uniqueness with
#     CREATE UNIQUE INDEX idx_dedup_unique_event
#         ON DeduplicatedEvents(title, event_date) WHERE status = 'Active'
#
# That makes a *mutable display field* part of the row's identity, with two
# concrete consequences seen in this database:
#
#  1. Titles could not be corrected. A merged event inherits its master's
#     headline, so the 130-record Qantas breach was called "Scattered Spider
#     Ransomware Attacks" and the 43-record HWL Ebsworth breach "Untitled
#     Event" - and rewriting either risked colliding with another row.
#  2. Splits failed. Undoing a bad merge creates a row whose title and date
#     match the group it just left, so seven corrections aborted outright and
#     had to be worked around by recomputing the group first.
#
# It is also wrong on its own terms: two genuinely distinct incidents can share
# a title and a date, which is common when titles are placeholders.
#
# Identity moves to ``master_enriched_event_id`` - assigned once, never
# rewritten, and unique per active event. Duplicate *content* is the
# deduplication pipeline's job to detect, not a database index's.
IDENTITY_MIGRATION = [
    "DROP INDEX IF EXISTS idx_dedup_unique_event",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_dedup_unique_master
        ON DeduplicatedEvents(master_enriched_event_id)
        WHERE status = 'Active'
    """,
    # Titles are looked up often for display; keep them indexed, not unique.
    "CREATE INDEX IF NOT EXISTS idx_dedup_title ON DeduplicatedEvents(title)",
]


# Columns we add to the existing DeduplicatedEvents table, if absent.
DEDUP_EVENT_COLUMNS = {
    "dedup_certainty": "REAL",
    "dedup_method": "TEXT",
    "dedup_batch_id": "TEXT",
    # A supply-chain breach is only intelligible when both parties are known:
    # the victim whose data was exposed, and the vendor through whom it
    # happened. Recording only one produced events attributed to "Canvas
    # Learning Management System" - a product - because that is what coverage
    # names most.
    "vendor_organization_name": "TEXT",
    "vendor_organization_industry": "TEXT",
    # Signature of the members the roles were classified against. Roles are
    # per-event, so any change of membership invalidates them; comparing this
    # to the current membership detects that without every mutation path
    # having to remember to raise a flag.
    "roles_member_signature": "TEXT",
    "roles_classified_at": "TIMESTAMP",
    "has_human_override": "INTEGER NOT NULL DEFAULT 0",
}


# Columns we add to the existing EntitiesV2 table, if absent.
#
# ``turnover`` and ``employee_count`` already existed but are NULL on all 3,133
# rows - nothing ever populated them, so every analysis that asked for
# organisation size (the recurrent-timing model asks for two such covariates)
# silently ran with them unknown. The ordinal band is what those analyses
# actually need, and unlike an exact headcount it can be established for an
# organisation that does not publish one.
#
# The estimate is stored with its evidence for the same reason merges are:
# a size that cannot be traced back to what it was based on cannot be reviewed.
ENTITY_SIZE_COLUMNS = {
    "size_estimate": "TEXT",        # SMALL | MEDIUM | LARGE | HUGE | UNKNOWN
    "size_confidence": "REAL",
    "size_employees": "INTEGER",    # best point estimate behind the band
    "size_revenue_aud": "REAL",     # approximate annual revenue/budget, AUD
    "size_basis": "TEXT",           # one or two sentences of justification
    "size_method": "TEXT",          # perplexity | llm | rule | human | unavailable
    "size_sources": "TEXT",         # JSON list of URLs the research cited
    "size_estimated_at": "TIMESTAMP",
}


def _existing_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _migrate_identity_index(conn: sqlite3.Connection) -> None:
    """Move uniqueness from (title, event_date) to master_enriched_event_id.

    Skipped, with a warning, if the immutable key is not actually unique in
    this database - dropping the old index without a replacement would leave
    the table unprotected.
    """
    dupes = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT master_enriched_event_id FROM DeduplicatedEvents
            WHERE status = 'Active'
            GROUP BY master_enriched_event_id HAVING COUNT(*) > 1)
        """
    ).fetchone()[0]
    if dupes:
        logger.warning(
            "Not migrating the identity index: %d active row(s) share a "
            "master_enriched_event_id. Resolve those first.", dupes
        )
        return

    for statement in IDENTITY_MIGRATION:
        conn.execute(statement)
    logger.info("Identity index now keyed on master_enriched_event_id")


def migrate(db: Union[str, Path, sqlite3.Connection]) -> None:
    """Apply the v3 schema. Safe to run repeatedly.

    Args:
        db: Database path or an open connection. When a path is given the
            connection is opened and closed here.
    """
    owns_connection = not isinstance(db, sqlite3.Connection)
    conn = sqlite3.connect(str(db)) if owns_connection else db

    try:
        for statement in DDL_STATEMENTS:
            conn.execute(statement)

        # DeduplicatedEvents may not exist yet in a fresh test database.
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "EntitiesV2" in tables:
            present = set(_existing_columns(conn, "EntitiesV2"))
            if "entity_kind" not in present:
                # What an entity IS, independent of any event: organisation,
                # government_body, product, person, threat_actor. Distinct from
                # the per-event role, which lives on EnrichedEventEntities.
                conn.execute("ALTER TABLE EntitiesV2 ADD COLUMN entity_kind TEXT")
                logger.info("Added EntitiesV2.entity_kind")
            for column, decl in ENTITY_SIZE_COLUMNS.items():
                if column not in present:
                    conn.execute(
                        f"ALTER TABLE EntitiesV2 ADD COLUMN {column} {decl}")
                    logger.info("Added EntitiesV2.%s", column)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_entities_size "
                "ON EntitiesV2(size_estimate)")

        if "EnrichedEventEntities" in tables:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_eee_role "
                "ON EnrichedEventEntities(relationship_type)")

        if "DeduplicatedEvents" in tables:
            _migrate_identity_index(conn)
            present = set(_existing_columns(conn, "DeduplicatedEvents"))
            for column, decl in DEDUP_EVENT_COLUMNS.items():
                if column not in present:
                    conn.execute(
                        f"ALTER TABLE DeduplicatedEvents ADD COLUMN {column} {decl}"
                    )
                    logger.info("Added DeduplicatedEvents.%s", column)

        conn.commit()
        logger.info("Deduplication v%d schema applied", SCHEMA_VERSION)
    finally:
        if owns_connection:
            conn.close()
