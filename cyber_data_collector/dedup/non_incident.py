"""Keep pages that were never cyber incidents out of the event set.

Search sources return vendor marketing and service pages that mention breaches
in passing ("Cybersecurity Consulting Services | IT Security Services |
Insight"). The RF filter and GPT-4o-mini pass can keep them, and incremental
deduplication then stores every Active enriched record as an event.

Neither signal alone is safe to act on. ``is_australian_event = 0`` covers
genuine Australian incidents whose first extraction missed the country (Canva,
NSW Health, Clutch Industries), and a zero-confidence Perplexity result with no
named victim covers real but thinly reported incidents (FIIG Securities, the
Victorian Department of Education). The gate is the conjunction: the record is
not Australian *and* Perplexity, asked to identify the incident, found no victim
and no confidence at all. On the 2026-09-15 database that matched exactly one
of 806 events - the Insight page.

An event is rejected only when *every* member record meets the gate, so a real
incident that absorbed one junk record is never removed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from cyber_data_collector.dedup.ledger import DedupLedger

logger = logging.getLogger(__name__)

_EMPTY_VALUES = {"", "none", "null", "unknown", "n/a"}


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_non_incident(is_australian_event: Any, perplexity_enrichment_data: Optional[str]) -> bool:
    """True when a record is positively not an Australian cyber incident.

    Args:
        is_australian_event: ``EnrichedEvents.is_australian_event`` (0/1/None).
        perplexity_enrichment_data: The stored Perplexity enrichment JSON.

    Returns:
        True only if the record is flagged non-Australian and Perplexity
        returned zero overall confidence with no formal victim name. Missing
        or unparseable enrichment returns False: absence of evidence is not
        grounds for removal.
    """
    if is_australian_event is None or bool(is_australian_event):
        return False
    if not perplexity_enrichment_data:
        return False
    try:
        data = json.loads(perplexity_enrichment_data)
    except (TypeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    confidence = _as_float(data.get("overall_confidence"))
    entity = str(data.get("formal_entity_name") or "").strip().lower()
    return confidence == 0.0 and entity in _EMPTY_VALUES


def deactivate_unmapped_non_incidents(conn: sqlite3.Connection, dry_run: bool = False) -> List[str]:
    """Mark Active enriched records meeting the gate Inactive before dedup.

    Only records not yet in ``EventDeduplicationMap`` are considered, so this
    never alters an existing event's membership. Incremental deduplication
    picks up Active records only, so these never become events.

    Returns:
        The enriched_event_ids deactivated (or that would be, in a dry run).
    """
    rows = conn.execute(
        """
        SELECT e.enriched_event_id, e.is_australian_event, e.perplexity_enrichment_data
        FROM EnrichedEvents e
        WHERE e.status = 'Active'
          AND NOT EXISTS (SELECT 1 FROM EventDeduplicationMap m
                          WHERE m.enriched_event_id = e.enriched_event_id)
        """
    ).fetchall()
    ids = [r[0] for r in rows if is_non_incident(r[1], r[2])]
    if ids and not dry_run:
        conn.executemany(
            "UPDATE EnrichedEvents SET status = 'Inactive', updated_at = ? WHERE enriched_event_id = ?",
            [(datetime.now(), i) for i in ids],
        )
        conn.commit()
        logger.info("Deactivated %d non-incident enriched record(s) before dedup", len(ids))
    return ids


def find_non_incident_events(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Active deduplicated events whose every member record meets the gate."""
    events = conn.execute(
        "SELECT deduplicated_event_id, title, event_date FROM DeduplicatedEvents WHERE status = 'Active'"
    ).fetchall()
    found: List[Dict[str, Any]] = []
    for dedup_id, title, event_date in events:
        members = conn.execute(
            """
            SELECT e.enriched_event_id, e.is_australian_event, e.perplexity_enrichment_data
            FROM EventDeduplicationMap m
            JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
            WHERE m.deduplicated_event_id = ?
            """,
            (dedup_id,),
        ).fetchall()
        if members and all(is_non_incident(m[1], m[2]) for m in members):
            found.append({
                "deduplicated_event_id": dedup_id,
                "title": title,
                "event_date": event_date,
                "member_ids": [m[0] for m in members],
            })
    return found


def reject_non_incident_events(conn: sqlite3.Connection, dry_run: bool = True) -> List[Dict[str, Any]]:
    """Reject events that were never incidents; reversible via the ledger snapshot.

    Sets the event to ``'Rejected'`` (the status the integrity check reserves
    for rows that were never an incident) and its member records to
    ``'Inactive'`` so incremental dedup cannot recreate it.

    Returns:
        The events rejected (or that would be, in a dry run).
    """
    found = find_non_incident_events(conn)
    if dry_run or not found:
        return found

    ledger = DedupLedger(conn)
    batch_id = f"reject-non-incident-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    now = datetime.now()
    try:
        for event in found:
            ledger.snapshot_event(batch_id, event["deduplicated_event_id"])
            conn.execute(
                "UPDATE DeduplicatedEvents SET status = 'Rejected', updated_at = ? "
                "WHERE deduplicated_event_id = ?",
                (now, event["deduplicated_event_id"]),
            )
            conn.executemany(
                "UPDATE EnrichedEvents SET status = 'Inactive', updated_at = ? WHERE enriched_event_id = ?",
                [(now, i) for i in event["member_ids"]],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    logger.info("Rejected %d non-incident event(s) (snapshot batch %s)", len(found), batch_id)
    return found
