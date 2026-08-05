"""Detect when an event's entity roles no longer match its membership.

Roles are assigned per event: which organisation was the victim, which was the
vendor, which names are bystanders. That judgement depends on the set of
records the event contains, so folding two events together or splitting one
apart invalidates it - the surviving event keeps roles that were decided for a
different set of records.

**Staleness is derived, not flagged.** The obvious approach is for every
mutation to set a "needs reclassification" flag, but that only works while
every current *and future* code path remembers to do it, and this pipeline has
several: ``merge_events``, ``split_member``, ``consolidate``, the legacy
review, the backfill. Missing one fails silently, which is exactly the class of
bug that left ``DeduplicatedEventSources`` empty.

Instead each event stores a signature of the members it was classified against.
Any change to membership - by any route, including ones not yet written -
changes the signature and the event shows up as stale. Nothing has to remember
anything.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


def signature_for_members(enriched_event_ids: Sequence[str]) -> str:
    """Stable signature for a set of member records.

    Order-independent, so re-storing the same members in a different order does
    not look like a change.
    """
    joined = "|".join(sorted(str(i) for i in enriched_event_ids if i))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def member_signature(conn: sqlite3.Connection, deduplicated_event_id: str) -> str:
    """Current signature of an event's membership."""
    rows = conn.execute(
        "SELECT enriched_event_id FROM EventDeduplicationMap "
        "WHERE deduplicated_event_id = ?",
        (deduplicated_event_id,),
    ).fetchall()
    return signature_for_members([r[0] for r in rows])


def mark_classified(
    conn: sqlite3.Connection,
    deduplicated_event_id: str,
    signature: Optional[str] = None,
) -> str:
    """Record that roles are current for this event's membership."""
    signature = signature or member_signature(conn, deduplicated_event_id)
    conn.execute(
        "UPDATE DeduplicatedEvents SET roles_member_signature = ?, "
        "roles_classified_at = ? WHERE deduplicated_event_id = ?",
        (signature, datetime.now().isoformat(), deduplicated_event_id),
    )
    return signature


def stale_event_ids(
    conn: sqlite3.Connection,
    include_unclassified: bool = True,
) -> List[str]:
    """Active events whose roles no longer match their membership.

    Args:
        include_unclassified: Also return events that have never been
            classified. They are stale in the sense that matters - their roles
            are absent rather than merely outdated.
    """
    rows = conn.execute(
        """
        SELECT d.deduplicated_event_id AS id, d.roles_member_signature AS stored,
               GROUP_CONCAT(m.enriched_event_id) AS members
        FROM DeduplicatedEvents d
        LEFT JOIN EventDeduplicationMap m
               ON m.deduplicated_event_id = d.deduplicated_event_id
        WHERE COALESCE(d.status, 'Active') = 'Active'
        GROUP BY d.deduplicated_event_id
        """
    ).fetchall()

    stale: List[str] = []
    for row in rows:
        members = (row["members"] or "").split(",") if row["members"] else []
        current = signature_for_members(members)
        stored = row["stored"]
        if stored is None or not str(stored).strip():
            if include_unclassified:
                stale.append(row["id"])
            continue
        if stored != current:
            stale.append(row["id"])
    return stale


def staleness_report(conn: sqlite3.Connection) -> Dict[str, int]:
    """Counts of current, outdated and never-classified active events."""
    rows = conn.execute(
        """
        SELECT d.deduplicated_event_id AS id, d.roles_member_signature AS stored,
               GROUP_CONCAT(m.enriched_event_id) AS members
        FROM DeduplicatedEvents d
        LEFT JOIN EventDeduplicationMap m
               ON m.deduplicated_event_id = d.deduplicated_event_id
        WHERE COALESCE(d.status, 'Active') = 'Active'
        GROUP BY d.deduplicated_event_id
        """
    ).fetchall()

    report = {"active": len(rows), "current": 0, "outdated": 0, "never_classified": 0}
    for row in rows:
        members = (row["members"] or "").split(",") if row["members"] else []
        current = signature_for_members(members)
        stored = row["stored"]
        if stored is None or not str(stored).strip():
            report["never_classified"] += 1
        elif stored != current:
            report["outdated"] += 1
        else:
            report["current"] += 1
    return report


def invalidate(conn: sqlite3.Connection, deduplicated_event_id: str) -> None:
    """Force an event to be treated as stale.

    Rarely needed - membership changes are detected automatically - but useful
    when the *classifier* changes and existing labels should be redone.
    """
    conn.execute(
        "UPDATE DeduplicatedEvents SET roles_member_signature = NULL "
        "WHERE deduplicated_event_id = ?",
        (deduplicated_event_id,),
    )


def invalidate_all(conn: sqlite3.Connection) -> int:
    """Mark every active event stale, e.g. after changing the classifier."""
    cursor = conn.execute(
        "UPDATE DeduplicatedEvents SET roles_member_signature = NULL "
        "WHERE COALESCE(status, 'Active') = 'Active'"
    )
    return cursor.rowcount


# ----------------------------------------------------------------------
# Automatic refresh
# ----------------------------------------------------------------------

class DeferredRoleRefresher:
    """Collects events whose roles a mutation invalidated, then refreshes them.

    Wired into :class:`~cyber_data_collector.dedup.ledger.DedupLedger`, so a
    merge or split schedules its own refresh - the caller does not have to
    remember a follow-up command.

    Refreshing is *deferred to the end of the operation* rather than done
    inline, for one reason: merges chain. Consolidating a cluster folds B into
    A, then C into A, then D into A; refreshing on each would classify A four
    times and discard three of the answers. Collecting ids and flushing once
    classifies each event exactly once, and skips any that a later mutation
    merged away. The deferral is invisible to the caller as long as ``flush``
    runs before the command returns.
    """

    def __init__(self, classifier=None, max_entities: int = 25):
        self._classifier = classifier
        self._max_entities = max_entities
        self._pending: "OrderedDict[str, None]" = OrderedDict()
        self.stats: Dict[str, int] = {
            "scheduled": 0, "refreshed": 0, "skipped": 0, "failed": 0,
        }

    def __call__(self, deduplicated_event_id: str) -> None:
        """Schedule an event for refresh. Called by the ledger."""
        if deduplicated_event_id:
            self._pending[deduplicated_event_id] = None
            self.stats["scheduled"] += 1

    @property
    def pending(self) -> List[str]:
        return list(self._pending)

    def flush(self, conn: sqlite3.Connection) -> Dict[str, int]:
        """Reclassify every scheduled event that is still active and stale."""
        if not self._pending:
            return self.stats

        from cyber_data_collector.dedup.entity_classification import (
            EntityRoleClassifier,
            classify_event,
        )

        classifier = self._classifier or EntityRoleClassifier()
        stale = set(stale_event_ids(conn))
        scheduled, self._pending = list(self._pending), OrderedDict()

        for dedup_id in scheduled:
            row = conn.execute(
                "SELECT 1 FROM DeduplicatedEvents WHERE deduplicated_event_id = ? "
                "AND COALESCE(status,'Active')='Active'", (dedup_id,)).fetchone()
            if row is None or dedup_id not in stale:
                # Merged away by a later mutation, or already current.
                self.stats["skipped"] += 1
                continue
            try:
                classify_event(conn, dedup_id, classifier=classifier,
                               max_entities=self._max_entities)
                self.stats["refreshed"] += 1
            except Exception as exc:  # noqa: BLE001 - never fail the merge itself
                self.stats["failed"] += 1
                logger.warning("Automatic role refresh failed for %s: %s",
                               dedup_id, exc)
        logger.info("Automatic role refresh: %s", self.stats)
        return self.stats


@contextmanager
def with_auto_refresh(conn: sqlite3.Connection, enabled: bool = True,
                      classifier=None):
    """Provide a refresher and guarantee it flushes, including on error.

    Yields ``None`` when disabled, which the ledger treats as "no refresh".
    """
    if not enabled:
        yield None
        return

    refresher = DeferredRoleRefresher(classifier=classifier)
    try:
        yield refresher
    finally:
        try:
            refresher.flush(conn)
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Role refresh flush failed: %s", exc)
