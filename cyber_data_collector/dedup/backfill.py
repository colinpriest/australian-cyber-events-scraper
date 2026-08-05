"""Repair provenance and lineage for already-stored deduplicated events.

Two defects in ``storage/deduplication_storage.py`` left the current database
without usable provenance:

* ``_store_unique_events`` inserts a DeduplicatedEvents row without
  ``total_data_sources`` and never writes ``DeduplicatedEventSources``, so the
  sources table is empty (0 rows) and the count is 0 on all 1,034 events.
* ``_store_merge_groups`` returns early for single-event groups
  (``if len(group.merged_events) == 0: continue``) and never writes a row for
  the group's *master* event, so only merged members of multi-event groups
  appear in ``EventDeduplicationMap`` - 384 of 1,034 events have lineage.

This module reconstructs both from data that is still intact: the
``master_enriched_event_id`` on every dedup row, and the RawEvents behind each
enriched event. It is idempotent - safe to run repeatedly.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class BackfillReport(dict):
    """Plain dict with a readable summary."""

    def summary(self) -> str:
        return (
            f"master lineage rows added: {self.get('master_rows_added', 0)}; "
            f"source rows added: {self.get('source_rows_added', 0)}; "
            f"entity links added: {self.get('entity_links_added', 0)}; "
            f"counts updated: {self.get('counts_updated', 0)}; "
            f"events still without lineage: {self.get('events_without_lineage', 0)}"
        )


def backfill_master_lineage(conn: sqlite3.Connection) -> int:
    """Insert the missing ``master`` row in EventDeduplicationMap.

    Every DeduplicatedEvents row names its ``master_enriched_event_id`` but the
    storage layer never recorded that as membership, so singleton events had no
    lineage at all and merged events omitted their own master.
    """
    rows = conn.execute(
        """
        SELECT d.deduplicated_event_id, d.master_enriched_event_id, e.raw_event_id
        FROM DeduplicatedEvents d
        JOIN EnrichedEvents e
          ON e.enriched_event_id = d.master_enriched_event_id
        WHERE NOT EXISTS (
            SELECT 1 FROM EventDeduplicationMap m
            WHERE m.deduplicated_event_id = d.deduplicated_event_id
              AND m.enriched_event_id = d.master_enriched_event_id
        )
        """
    ).fetchall()

    added = 0
    for dedup_id, enriched_id, raw_id in rows:
        if raw_id is None:
            logger.debug("No raw event for %s; skipping lineage row", enriched_id)
            continue
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO EventDeduplicationMap (
                    map_id, raw_event_id, enriched_event_id, deduplicated_event_id,
                    contribution_type, similarity_score, data_source_weight
                ) VALUES (?, ?, ?, ?, 'master', 1.0, 1.0)
                """,
                (str(uuid.uuid4()), raw_id, enriched_id, dedup_id),
            )
            added += conn.total_changes and 1 or 0
        except sqlite3.IntegrityError as exc:
            logger.debug("Lineage insert skipped for %s: %s", dedup_id, exc)
    logger.info("Backfilled %d master lineage row(s)", added)
    return added


def backfill_sources(conn: sqlite3.Connection) -> int:
    """Rebuild ``DeduplicatedEventSources`` from each event's raw ancestry.

    One row per distinct source URL contributing to the deduplicated event,
    which is what ``total_data_sources`` is meant to count.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT m.deduplicated_event_id,
               r.source_url, r.source_type, r.discovered_at,
               substr(COALESCE(r.raw_description, r.raw_content, ''), 1, 400) AS snippet
        FROM EventDeduplicationMap m
        JOIN RawEvents r ON r.raw_event_id = m.raw_event_id
        WHERE r.source_url IS NOT NULL AND r.source_url != ''
          AND NOT EXISTS (
              SELECT 1 FROM DeduplicatedEventSources s
              WHERE s.deduplicated_event_id = m.deduplicated_event_id
                AND s.source_url = r.source_url
          )
        """
    ).fetchall()

    added = 0
    for dedup_id, url, source_type, discovered_at, snippet in rows:
        try:
            conn.execute(
                """
                INSERT INTO DeduplicatedEventSources (
                    deduplicated_event_id, source_url, source_type,
                    credibility_score, content_snippet, discovered_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (dedup_id, url, source_type, None, snippet,
                 discovered_at or datetime.now()),
            )
            added += 1
        except sqlite3.IntegrityError as exc:
            logger.debug("Source insert skipped for %s: %s", dedup_id, exc)
    logger.info("Backfilled %d source row(s)", added)
    return added



def backfill_event_entities(conn: sqlite3.Connection) -> int:
    """Rebuild ``DeduplicatedEventEntities`` from the members' entity links.

    The table is written during discovery but **deleted and never repopulated**
    by a deduplication rebuild, so it sat at 0 rows. Anything reading it saw
    nothing: the main dashboard's severity-by-month chart joins it for the
    organisation name and printed "Unknown Entity" for all 79 months, and the
    full export reports zero event-entity relationships.

    Same defect class as the empty ``DeduplicatedEventSources``: a rebuild
    regenerates ``deduplicated_event_id``, so everything keyed on it is lost
    unless something rewrites it.

    Roles come from ``EnrichedEventEntities.relationship_type``, which entity
    classification now populates, so the rebuilt rows carry victim/vendor/
    regulator rather than a uniform 'affected'.
    """
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "DeduplicatedEventEntities" not in tables or "EnrichedEventEntities" not in tables:
        logger.warning(
            "Entity link tables absent; event-entity provenance not rebuilt.")
        return 0

    rows = conn.execute(
        """
        SELECT m.deduplicated_event_id AS did, ee.entity_id AS eid,
               ee.relationship_type AS role,
               MAX(COALESCE(ee.confidence_score, 0.5)) AS conf,
               COUNT(*) AS n
        FROM EventDeduplicationMap m
        JOIN EnrichedEventEntities ee
             ON ee.enriched_event_id = m.enriched_event_id
        WHERE NOT EXISTS (
            SELECT 1 FROM DeduplicatedEventEntities d
            WHERE d.deduplicated_event_id = m.deduplicated_event_id
              AND d.entity_id = ee.entity_id
        )
        GROUP BY m.deduplicated_event_id, ee.entity_id, ee.relationship_type
        """
    ).fetchall()

    added = 0
    for did, eid, role, conf, count in rows:
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO DeduplicatedEventEntities (
                    deduplicated_event_id, entity_id, relationship_type,
                    confidence_score, source_count
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (did, eid, role or "affected", conf, count),
            )
            added += 1
        except sqlite3.IntegrityError as exc:
            logger.debug("Event-entity insert skipped for %s: %s", did, exc)
    logger.info("Backfilled %d event-entity link(s)", added)
    return added


def refresh_source_counts(conn: sqlite3.Connection) -> int:
    """Recompute ``total_data_sources`` from actual membership.

    Uses distinct source URLs where available, falling back to the member count
    so an event is never left claiming zero sources when it demonstrably has
    contributors.
    """
    updated = conn.execute(
        """
        UPDATE DeduplicatedEvents
        SET total_data_sources = MAX(
            (SELECT COUNT(*) FROM DeduplicatedEventSources s
             WHERE s.deduplicated_event_id = DeduplicatedEvents.deduplicated_event_id),
            (SELECT COUNT(DISTINCT m.enriched_event_id) FROM EventDeduplicationMap m
             WHERE m.deduplicated_event_id = DeduplicatedEvents.deduplicated_event_id),
            1
        )
        """
    ).rowcount
    logger.info("Refreshed total_data_sources on %d event(s)", updated)
    return updated


def count_events_without_lineage(conn: sqlite3.Connection) -> int:
    return conn.execute(
        """
        SELECT COUNT(*) FROM DeduplicatedEvents d
        WHERE NOT EXISTS (
            SELECT 1 FROM EventDeduplicationMap m
            WHERE m.deduplicated_event_id = d.deduplicated_event_id
        )
        """
    ).fetchone()[0]


def run_backfill(conn: sqlite3.Connection, dry_run: bool = False) -> BackfillReport:
    """Repair lineage, sources and counts. Idempotent."""
    report = BackfillReport()
    report["events_without_lineage_before"] = count_events_without_lineage(conn)
    report["sources_before"] = conn.execute(
        "SELECT COUNT(*) FROM DeduplicatedEventSources"
    ).fetchone()[0]

    report["master_rows_added"] = backfill_master_lineage(conn)
    report["source_rows_added"] = backfill_sources(conn)
    report["entity_links_added"] = backfill_event_entities(conn)
    report["counts_updated"] = refresh_source_counts(conn)
    report["events_without_lineage"] = count_events_without_lineage(conn)

    if dry_run:
        conn.rollback()
        logger.info("Dry run - rolled back")
    else:
        conn.commit()
    return report
