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

A second, explicit signal is the vendor blocklist
(``vendor_source_blocklist.txt``): service, product and marketing pages that
describe what a firm sells rather than an incident. AusCi's incident-response
services page was Australian, so the conjunction above could not catch it. A
record from a listed page counts as a non-incident.

An event is rejected only when *every* member record is a non-incident, so a
real incident that absorbed one junk record, or that also cites a vendor page,
is never removed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from cyber_data_collector.dedup.ledger import DedupLedger
from cyber_data_collector.dedup.page_classifier import is_non_incident_verdict, load_verdicts

logger = logging.getLogger(__name__)

_EMPTY_VALUES = {"", "none", "null", "unknown", "n/a"}

DEFAULT_BLOCKLIST_PATH = Path(__file__).with_name("vendor_source_blocklist.txt")


def _split_host_path(value: str) -> Tuple[str, str]:
    """Lower-cased (host, path) with scheme and a leading ``www.`` removed."""
    value = value.strip().lower()
    if "://" not in value:
        value = "http://" + value
    parsed = urlparse(value)
    host = parsed.netloc.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host, parsed.path or "/"


def load_blocklist(path: Path = DEFAULT_BLOCKLIST_PATH) -> List[Tuple[str, str]]:
    """Parse the vendor blocklist into ``(host, path_prefix)`` entries."""
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            entries.append(_split_host_path(line))
    return entries


def is_blocked_source(url: Optional[str], blocklist: Sequence[Tuple[str, str]]) -> bool:
    """True if ``url`` is on a listed domain (or subdomain) under a listed path."""
    if not url or not blocklist:
        return False
    host, path = _split_host_path(url)
    for entry_host, entry_path in blocklist:
        if host == entry_host or host.endswith("." + entry_host):
            if path.startswith(entry_path):
                return True
    return False


def _is_non_incident_record(is_au: Any, pe: Optional[str], url: Optional[str],
                            blocklist: Sequence[Tuple[str, str]],
                            page_verdict: Optional[Tuple[str, float]] = None) -> bool:
    """Any one signal suffices: the zero-confidence conjunction, the vendor
    blocklist, or a confident page-classifier verdict (roundup, guidance,
    profile or index page - see :mod:`page_classifier`)."""
    return (is_non_incident(is_au, pe) or is_blocked_source(url, blocklist)
            or is_non_incident_verdict(page_verdict))


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


def deactivate_unmapped_non_incidents(
    conn: sqlite3.Connection,
    dry_run: bool = False,
    blocklist: Optional[Sequence[Tuple[str, str]]] = None,
) -> List[str]:
    """Mark Active enriched records meeting the gate Inactive before dedup.

    Only records not yet in ``EventDeduplicationMap`` are considered, so this
    never alters an existing event's membership. Incremental deduplication
    picks up Active records only, so these never become events.

    Returns:
        The enriched_event_ids deactivated (or that would be, in a dry run).
    """
    rows = conn.execute(
        """
        SELECT e.enriched_event_id, e.is_australian_event, e.perplexity_enrichment_data,
               r.source_url
        FROM EnrichedEvents e
        LEFT JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
        WHERE e.status = 'Active'
          AND NOT EXISTS (SELECT 1 FROM EventDeduplicationMap m
                          WHERE m.enriched_event_id = e.enriched_event_id)
        """
    ).fetchall()
    blocklist = load_blocklist() if blocklist is None else blocklist
    verdicts = load_verdicts(conn)
    ids = [r[0] for r in rows if _is_non_incident_record(r[1], r[2], r[3], blocklist, verdicts.get(r[0]))]
    if ids and not dry_run:
        conn.executemany(
            "UPDATE EnrichedEvents SET status = 'Inactive', updated_at = ? WHERE enriched_event_id = ?",
            [(datetime.now(), i) for i in ids],
        )
        conn.commit()
        logger.info("Deactivated %d non-incident enriched record(s) before dedup", len(ids))
    return ids


def find_non_incident_events(
    conn: sqlite3.Connection,
    blocklist: Optional[Sequence[Tuple[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """Active deduplicated events whose every member record is a non-incident."""
    blocklist = load_blocklist() if blocklist is None else blocklist
    verdicts = load_verdicts(conn)
    events = conn.execute(
        "SELECT deduplicated_event_id, title, event_date FROM DeduplicatedEvents WHERE status = 'Active'"
    ).fetchall()
    found: List[Dict[str, Any]] = []
    for dedup_id, title, event_date in events:
        members = conn.execute(
            """
            SELECT e.enriched_event_id, e.is_australian_event, e.perplexity_enrichment_data,
                   r.source_url
            FROM EventDeduplicationMap m
            JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
            LEFT JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
            WHERE m.deduplicated_event_id = ?
            """,
            (dedup_id,),
        ).fetchall()
        if members and all(_is_non_incident_record(m[1], m[2], m[3], blocklist, verdicts.get(m[0]))
                           for m in members):
            found.append({
                "deduplicated_event_id": dedup_id,
                "title": title,
                "event_date": event_date,
                "member_ids": [m[0] for m in members],
            })
    return found


def reject_non_incident_events(
    conn: sqlite3.Connection,
    dry_run: bool = True,
    blocklist: Optional[Sequence[Tuple[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """Reject events that were never incidents; reversible via the ledger snapshot.

    Sets the event to ``'Rejected'`` (the status the integrity check reserves
    for rows that were never an incident) and its member records to
    ``'Inactive'`` so incremental dedup cannot recreate it.

    Returns:
        The events rejected (or that would be, in a dry run).
    """
    found = find_non_incident_events(conn, blocklist)
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
