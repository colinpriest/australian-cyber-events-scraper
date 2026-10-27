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
lag the incident), ``stated in the record`` only when the record's own text
states that date (to its precision), and ``unknown`` otherwise - a date that
matches nothing used to be called "stated" without any check.
``precision`` is ``day``, ``month only`` (stored as the 1st) or ``year only``
(stored as 1 January) - a genuine 1st of the month is indistinguishable from a
placeholder, so it is reported as possibly month-only.
"""

from __future__ import annotations

import json
import re
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


MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]


def text_states_date(text: Optional[str], day: Optional[str]) -> bool:
    """Does ``text`` state ``day`` to the precision it is stored at?

    A day-precision date needs the day ("12 March 2024", "March 12, 2024",
    "2024-03-12", "12/03/2024"); a month-only date (stored as the 1st) needs
    the month and year; a year-only date needs the year. A month mention is
    never taken as support for an exact day.
    """
    if not text or not day:
        return False
    year, month, dom = int(day[:4]), int(day[5:7]), int(day[8:10])
    lowered = text.lower()
    name = MONTHS[month - 1]
    names = rf"(?:{name}|{name[:3]}\.?)"
    level = precision(day)
    if level.startswith("year only"):
        return re.search(rf"\b{year}\b", lowered) is not None
    month_year = re.search(rf"\b{names}\s*,?\s*{year}\b", lowered) is not None
    if level.startswith("month only"):
        return month_year or _states_day(lowered, year, month, dom, names)
    return _states_day(lowered, year, month, dom, names)


def _states_day(lowered: str, year: int, month: int, dom: int, names: str) -> bool:
    patterns = [
        rf"\b0?{dom}(?:st|nd|rd|th)?\s+(?:of\s+)?{names}\s*,?\s*{year}\b",
        rf"\b{names}\s+0?{dom}(?:st|nd|rd|th)?\s*,?\s*{year}\b",
        rf"\b{year}-{month:02d}-{dom:02d}\b",
        rf"\b0?{dom}/0?{month}/{year}\b",
    ]
    return any(re.search(pattern, lowered) for pattern in patterns)


def record_dates(incident: Any, source_date: Any, source_metadata: Optional[str],
                 discovered_at: Any, perplexity_data: Optional[str],
                 text: Optional[str] = None) -> Dict[str, Any]:
    """Provenance for one record's date; ``text`` is the record's own page text."""
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
    elif text_states_date(text, incident_day):
        basis = "stated in the record"
    else:
        basis = "unknown (not a source date and not found in the text)"
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
    per_record: List[Dict[str, Any]] = [record_dates(*r[2:], text=r[1]) for r in rows]
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


def event_date_provenance(conn: sqlite3.Connection, dedup_id: str) -> Dict[str, str]:
    """What the event's own date is: its basis and precision.

    The basis is the most common basis among the records carrying the event
    date; an event date no record carries was set by a repair pass
    (consensus, text extraction or research).
    """
    summary = event_dates(conn, dedup_id, max_records=10 ** 6)
    stored = conn.execute("SELECT event_date FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                          (dedup_id,)).fetchone()
    event_day = _day(stored[0] if stored else None)
    carrying = [r["basis"] for r in summary["records"] if event_day and r["date"] == event_day]
    if event_day is None:
        basis = "no date"
    elif carrying:
        basis = Counter(carrying).most_common(1)[0][0]
    else:
        basis = "set by a repair pass (no record carries it)"
    return {"date_basis": basis, "date_precision": precision(event_day)}


def refresh_event_date_provenance(conn: sqlite3.Connection) -> int:
    """Store ``date_basis`` / ``date_precision`` on every active event; returns how many changed."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(DeduplicatedEvents)")}
    for column in ("date_basis", "date_precision"):
        if column not in columns:
            conn.execute(f"ALTER TABLE DeduplicatedEvents ADD COLUMN {column} TEXT")
    changed = 0
    rows = conn.execute("SELECT deduplicated_event_id, date_basis, date_precision FROM DeduplicatedEvents "
                        "WHERE status = 'Active'").fetchall()
    for dedup_id, old_basis, old_precision in rows:
        new = event_date_provenance(conn, dedup_id)
        if (old_basis, old_precision) != (new["date_basis"], new["date_precision"]):
            conn.execute("UPDATE DeduplicatedEvents SET date_basis = ?, date_precision = ? "
                         "WHERE deduplicated_event_id = ?", (new["date_basis"], new["date_precision"], dedup_id))
            changed += 1
    return changed
