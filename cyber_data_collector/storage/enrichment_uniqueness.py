"""At most one active enrichment per stored record, enforced by the database.

``CyberEventDataV2.create_enriched_event`` used to look for an active
enrichment, release its lock, and take it again to insert. Two threads (or two
processes) could both find none and both insert, leaving a record with two
active enrichments - two events from one article, and a master record the
deduplication could not choose between.

Creation is now one statement (``INSERT ... WHERE NOT EXISTS``), and this
module adds the backstop in the database itself: a trigger that aborts an
``INSERT`` of an *active* enrichment for a record that already has one, so no
writer - a script, a test, a future code path - can recreate the race.

Why a trigger on ``INSERT`` and not a partial unique index on
``EnrichedEvents(raw_event_id) WHERE status = 'Active'``: the record-identity
repair (:mod:`~cyber_data_collector.processing.record_identity`) *moves*
enrichments between records with an ``UPDATE`` of ``raw_event_id``. A record
that holds an enrichment known to be about another article keeps it (it is
reported, not removed) while the enrichment that does belong to it is attached,
so two active enrichments on one record are a reported state of that repair,
not a bug; and a chain of moves would trip a unique index part-way through. The
invariant the pipeline owns is "ingest never creates a second one", and that is
what is enforced. Anything else that leaves a record with two is listed by
:func:`duplicate_active_enrichments` and fails ``check_data_integrity.py``
(``active-enrichment-unique-per-record``) unless a record-identity finding
explains it; nothing is deleted or deactivated by this module.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

TRIGGER_NAME = "trg_enriched_one_active_per_record"
#: The message of the abort; callers match on it.
ABORT_MESSAGE = "record already has an active enrichment"

_TRIGGER = f"""
CREATE TRIGGER IF NOT EXISTS {TRIGGER_NAME}
BEFORE INSERT ON EnrichedEvents
WHEN NEW.status = 'Active'
 AND EXISTS (SELECT 1 FROM EnrichedEvents
             WHERE raw_event_id = NEW.raw_event_id AND status = 'Active')
BEGIN
    SELECT RAISE(ABORT, '{ABORT_MESSAGE}');
END;
"""


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def duplicate_active_enrichments(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Records holding more than one active enrichment.

    Each entry: ``raw_event_id``, ``enriched_event_ids`` and ``explained`` (a
    record-identity finding names one of the enrichments, so the repair left the
    two on purpose and reported them).
    """
    if not _has_table(conn, "EnrichedEvents"):
        return []
    rows = conn.execute(
        "SELECT raw_event_id, GROUP_CONCAT(enriched_event_id, ',') FROM EnrichedEvents "
        "WHERE status = 'Active' GROUP BY raw_event_id HAVING COUNT(*) > 1 ORDER BY raw_event_id").fetchall()
    explained_ids: set = set()
    if _has_table(conn, "RecordIdentityChecks"):
        explained_ids = {r[0] for r in conn.execute(
            "SELECT enriched_event_id FROM RecordIdentityChecks WHERE enriched_event_id != '' "
            "AND (kind = 'enrichment_other_article' OR action = 'repointed')")}
    out = []
    for raw_id, joined in rows:
        ids = (joined or "").split(",")
        out.append({"raw_event_id": raw_id, "enriched_event_ids": ids,
                    "explained": any(i in explained_ids for i in ids)})
    return out


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Install the insert guard (idempotent) and say loudly if records already hold two.

    Existing duplicates are not touched: which of two enrichments is right is a
    decision, not a migration. They are logged at ERROR and listed by
    :func:`duplicate_active_enrichments`.
    """
    if not _has_table(conn, "EnrichedEvents"):
        return
    conn.execute(_TRIGGER)
    conn.commit()
    unexplained = [d for d in duplicate_active_enrichments(conn) if not d["explained"]]
    if unexplained:
        logger.error("[ENRICHMENT] %d record(s) already hold more than one active enrichment that no record-identity "
                     "finding explains (first: %s); the insert guard stops new ones, these need a decision: "
                     "python scripts/check_data_integrity.py", len(unexplained), unexplained[0]["raw_event_id"])
