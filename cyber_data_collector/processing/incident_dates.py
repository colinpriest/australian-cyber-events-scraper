"""Incident dates as intervals with verified precision, kept apart from publication dates.

A date stored as ``2025-06-01`` may mean "1 June 2025" or "some day in June
2025", and nothing in the date says which. :mod:`dedup.date_provenance`
guesses (a 1st is "month only, or the 1st"); the recurrence analysis then
treats every 1st as ambiguous. Precision is recorded where it is known instead:

* ``RawEvents.event_date_precision`` / ``event_date_basis`` - what the source
  itself established: a Webber list heading ("Vertel - June 2025") is a
  month; a date stated in the source's page is a day.
* Perplexity's ``date_precision`` counts only for a date the citation check
  accepted (``EnrichmentFactSupport`` fact ``date`` = ``supported``).
* ``DeduplicatedEvents.event_date_precision`` is set only when the records
  carrying the event's date agree on a verified precision, else NULL
  (unverified); ``event_date_earliest`` / ``event_date_latest`` give the
  interval (a month runs from its 1st to its last day; a year from 1 January
  to 31 December; a day is itself; unverified gives no interval).

Webber records were stored on the 15th of the heading month - a day nobody
reported - and the heading month outranked any date the article stated. All
832 Webber records on 2026-10-28 were on the 15th. :func:`repair_webber_dates`
moves them to the month interval (stored as the 1st, precision ``month``)
and clears the heading month as their publication date (it is the list's
heading, not the page's publication), so the page's own date is fetched.
"""

from __future__ import annotations

import calendar
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PRECISIONS = ("day", "month", "year")
BASIS_WEBBER_HEADING = "Webber list heading (month)"
BASIS_STATED_IN_SOURCE = "stated in the source page"


def ensure_schema(conn: sqlite3.Connection) -> None:
    raw = {r[1] for r in conn.execute("PRAGMA table_info(RawEvents)")}
    for column in ("event_date_precision", "event_date_basis"):
        if raw and column not in raw:
            conn.execute(f"ALTER TABLE RawEvents ADD COLUMN {column} TEXT")
    dedup = {r[1] for r in conn.execute("PRAGMA table_info(DeduplicatedEvents)")}
    for column in ("event_date_precision", "event_date_earliest", "event_date_latest"):
        if dedup and column not in dedup:
            conn.execute(f"ALTER TABLE DeduplicatedEvents ADD COLUMN {column} TEXT")


def _day(value: Any) -> Optional[date]:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def interval(value: Any, precision: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """``(earliest, latest)`` of a date at a precision; ``(None, None)`` when either is unknown."""
    day = _day(value)
    if day is None or precision not in PRECISIONS:
        return None, None
    if precision == "day":
        return day.isoformat(), day.isoformat()
    if precision == "month":
        last = calendar.monthrange(day.year, day.month)[1]
        return date(day.year, day.month, 1).isoformat(), date(day.year, day.month, last).isoformat()
    return date(day.year, 1, 1).isoformat(), date(day.year, 12, 31).isoformat()


def _record_precision(event_day: date, raw_date: Any, raw_precision: Optional[str], enriched_date: Any,
                      enrichment_json: Optional[str], date_supported: bool) -> Optional[str]:
    """The verified precision a record gives the event's date, or None."""
    if raw_precision in PRECISIONS and _day(raw_date) is not None:
        start, end = interval(raw_date, raw_precision)
        if start <= event_day.isoformat() <= end:
            return raw_precision
    if date_supported and _day(enriched_date) == event_day:
        try:
            data = json.loads(enrichment_json or "{}")
        except (TypeError, ValueError):
            data = {}
        precision = data.get("date_precision") if isinstance(data, dict) else None
        if precision in PRECISIONS:
            return precision
    return None


@dataclass
class PrecisionReport:
    events: int = 0
    verified: Dict[str, int] = field(default_factory=dict)
    conflicting: List[str] = field(default_factory=list)
    unverified: int = 0

    def summary(self) -> str:
        parts = ", ".join(f"{n} {p}" for p, n in sorted(self.verified.items()))
        return (f"incident-date precision verified for {sum(self.verified.values())} of {self.events} active "
                f"event(s) ({parts or 'none'}); {len(self.conflicting)} conflicting; {self.unverified} unverified")


def refresh_event_precision(conn: sqlite3.Connection) -> PrecisionReport:
    """Store each active event's verified precision and interval (NULL when unverified). The caller commits."""
    ensure_schema(conn)
    report = PrecisionReport()
    supported = {r[0] for r in conn.execute(
        "SELECT enriched_event_id FROM EnrichmentFactSupport WHERE fact = 'date' AND status = 'supported'")} \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'EnrichmentFactSupport'").fetchone() else set()
    events = conn.execute("SELECT deduplicated_event_id, event_date, event_date_precision, event_date_earliest, "
                          "event_date_latest FROM DeduplicatedEvents WHERE status = 'Active'").fetchall()
    for dedup_id, event_date, old_precision, old_start, old_end in events:
        report.events += 1
        event_day = _day(event_date)
        found = set()
        if event_day is not None:
            for enriched_id, raw_date, raw_precision, enriched_date, blob in conn.execute(
                    """SELECT e.enriched_event_id, r.event_date, r.event_date_precision, e.event_date,
                              e.perplexity_enrichment_data
                       FROM EventDeduplicationMap m
                       JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
                       JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
                       WHERE m.deduplicated_event_id = ? AND COALESCE(e.status, 'Active') = 'Active'""",
                    (dedup_id,)):
                precision = _record_precision(event_day, raw_date, raw_precision, enriched_date, blob,
                                              enriched_id in supported)
                if precision:
                    found.add(precision)
        if len(found) == 1:
            precision = found.pop()
            report.verified[precision] = report.verified.get(precision, 0) + 1
        else:
            precision = None
            if found:
                report.conflicting.append(dedup_id)
            else:
                report.unverified += 1
        start, end = interval(event_date, precision)
        if (old_precision, old_start, old_end) != (precision, start, end):
            conn.execute("UPDATE DeduplicatedEvents SET event_date_precision = ?, event_date_earliest = ?, "
                         "event_date_latest = ? WHERE deduplicated_event_id = ?", (precision, start, end, dedup_id))
    return report


@dataclass
class WebberRepair:
    raw: int = 0
    enriched: int = 0
    events: int = 0
    publication_cleared: int = 0


def repair_webber_dates(conn: sqlite3.Connection, dry_run: bool = False) -> WebberRepair:
    """Move Webber records off the invented 15th onto their heading month (idempotent). Commits."""
    ensure_schema(conn)
    out = WebberRepair()
    rows = conn.execute("""SELECT raw_event_id, event_date, publication_date, publication_date_source
                           FROM RawEvents WHERE source_type = 'WebberInsurance'
                             AND substr(event_date, 9, 2) = '15'
                             AND COALESCE(event_date_basis, '') != ?""", (BASIS_STATED_IN_SOURCE,)).fetchall()
    for raw_id, old, published, published_from in rows:
        old_day = _day(old)
        if old_day is None:
            continue
        month = date(old_day.year, old_day.month, 1).isoformat()
        out.raw += 1
        if dry_run:
            continue
        conn.execute("UPDATE RawEvents SET event_date = ?, event_date_precision = 'month', event_date_basis = ? "
                     "WHERE raw_event_id = ?", (month, BASIS_WEBBER_HEADING, raw_id))
        if published_from == "source" and _day(published) == old_day:
            conn.execute("UPDATE RawEvents SET publication_date = NULL, publication_date_source = NULL "
                         "WHERE raw_event_id = ?", (raw_id,))
            out.publication_cleared += 1
        for (enriched_id,) in conn.execute("SELECT enriched_event_id FROM EnrichedEvents WHERE raw_event_id = ? "
                                           "AND substr(event_date, 1, 10) = ?", (raw_id, old_day.isoformat())).fetchall():
            conn.execute("UPDATE EnrichedEvents SET event_date = ? WHERE enriched_event_id = ?", (month, enriched_id))
            out.enriched += 1
            for (dedup_id,) in conn.execute(
                    "SELECT m.deduplicated_event_id FROM EventDeduplicationMap m JOIN DeduplicatedEvents d "
                    "ON d.deduplicated_event_id = m.deduplicated_event_id WHERE m.enriched_event_id = ? "
                    "AND substr(d.event_date, 1, 10) = ?", (enriched_id, old_day.isoformat())).fetchall():
                conn.execute("UPDATE DeduplicatedEvents SET event_date = ? WHERE deduplicated_event_id = ?",
                             (month, dedup_id))
                out.events += 1
    if not dry_run:
        conn.commit()
    return out
