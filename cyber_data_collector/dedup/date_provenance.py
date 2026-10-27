"""How each stored date was determined, and how precise it is.

Deduplication treats a date gap as evidence of separate incidents, but many
stored dates are not incident dates at all: the extraction often took the
article's publication date, or the date the record was found, and a date known
only to the month or year is stored as the 1st (35% of active events on
2026-10-27). Two reports of one breach then sit weeks apart and look like a
repeat attack. Nothing records the provenance directly, so it is derived per
record by comparing the stored incident date with the dates around it:

* ``published`` - the source's publication date (``source_metadata``), or the
  date the source itself carried;
* ``found``     - when the pipeline discovered the record (``discovered_at``);
* ``researched``- Perplexity's earliest event date for the record.

``basis`` names which of these the incident date equals (checked in that
order, because a date equal to the publication date is the one most likely to
lag the incident), or ``stated in the record`` when it equals none of them.
``precision`` is ``day``, ``month only`` (stored as the 1st) or ``year only``
(stored as 1 January) - a genuine 1st of the month is indistinguishable from a
placeholder, so it is reported as possibly month-only.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from typing import Any, Dict, List, Optional


def _day(value: Any) -> Optional[str]:
    text = str(value or "").strip()[:10]
    return text if len(text) == 10 and text[4] == "-" and text[7] == "-" else None


def precision(date_text: Optional[str]) -> str:
    day = _day(date_text)
    if day is None:
        return "unknown"
    if day.endswith("-01-01"):
        return "year only (stored as 1 January)"
    if day.endswith("-01"):
        return "month only, or the 1st (stored as the 1st)"
    return "day"


def record_dates(incident: Any, source_date: Any, source_metadata: Optional[str],
                 discovered_at: Any, perplexity_data: Optional[str]) -> Dict[str, Any]:
    """Provenance for one record's date."""
    try:
        meta = json.loads(source_metadata or "{}")
    except (TypeError, ValueError):
        meta = {}
    try:
        pplx = json.loads(perplexity_data or "{}")
    except (TypeError, ValueError):
        pplx = {}
    incident_day = _day(incident)
    published = _day(meta.get("publication_date") if isinstance(meta, dict) else None) or _day(source_date)
    found = _day(discovered_at)
    researched = _day(pplx.get("earliest_event_date") if isinstance(pplx, dict) else None)
    if incident_day is None:
        basis = "no date"
    elif incident_day == published:
        basis = "publication date (may lag the incident)"
    elif incident_day == found:
        basis = "date the record was found (may lag the incident)"
    elif incident_day == researched:
        basis = "Perplexity research"
    else:
        basis = "stated in the record"
    return {"date": incident_day, "basis": basis, "precision": precision(incident_day),
            "published": published, "found": found, "researched": researched}


def event_dates(conn: sqlite3.Connection, dedup_id: str, max_records: int) -> Dict[str, Any]:
    """Per-record provenance in the same order as ``event_reality._evidence_state``,
    plus a summary over all of the event's records."""
    rows = conn.execute(
        """SELECT e.enriched_event_id, COALESCE(NULLIF(r.raw_content, ''), r.raw_description, ''),
                  e.event_date, r.event_date, r.source_metadata, r.discovered_at,
                  e.perplexity_enrichment_data
           FROM EventDeduplicationMap m
           JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
           LEFT JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
           WHERE m.deduplicated_event_id = ?""", (dedup_id,)).fetchall()
    rows = sorted(rows, key=lambda r: (not r[1], r[0]))
    per_record: List[Dict[str, Any]] = [record_dates(*r[2:]) for r in rows]
    stored = conn.execute("SELECT event_date FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                          (dedup_id,)).fetchone()
    event_day = _day(stored[0] if stored else None)
    supporting = sum(1 for d in per_record if event_day and d["date"] == event_day)
    return {
        "event_date_precision": precision(event_day),
        # An event date no record carries was set by a repair pass (consensus,
        # text extraction, research), not read from the event's own evidence.
        "records_carrying_event_date": f"{supporting} of {len(per_record)}",
        "record_date_bases": dict(Counter(d["basis"] for d in per_record)),
        "records": per_record[:max_records],
    }
