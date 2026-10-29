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

A day the article states is the *incident's* only when its wording ties it to
the breach (:func:`incident_date_from_text`): "the breach occurred on 2 May
2025", "discovered on", "attack on", "between 1 and 3 May 2025". The first date
anywhere in an article is usually its byline or publication date ("Published 1
June 2025"), which the parser used to store as an exact incident date.
Publication, byline, "updated" and announcement dates never count; without a
tied date the record keeps the list heading's month.
:func:`rederive_webber_dates` applies the rule to the stored records.
"""

from __future__ import annotations

import calendar
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

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


# ---------------------------------------------------------------------------
# A day the text ties to the incident
# ---------------------------------------------------------------------------

_MONTH = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?"
          r"|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
_MONTH_NUMBER = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_DATE_PATTERNS = (
    re.compile(rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m>{_MONTH})\.?,?\s+(?P<y>\d{{4}})\b", re.I),
    re.compile(rf"\b(?P<m>{_MONTH})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y>\d{{4}})\b", re.I),
    re.compile(r"\b(?P<y>\d{4})-(?P<n>\d{1,2})-(?P<d>\d{1,2})\b"),
    re.compile(r"\b(?P<d>\d{1,2})[/.](?P<n>\d{1,2})[/.](?P<y>\d{4})\b"),     # day first, as written in Australia
)
#: "between 1 and 3 May 2025", "between 28 April and 3 May 2025", "between November 1 and December 21, 2023":
#: the start has no year (and perhaps no month) of its own.
_RANGES = (
    re.compile(rf"\bbetween\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?(?:\s+(?P<m>{_MONTH}))?\s+(?:and|to)\s+"
               rf"\d{{1,2}}(?:st|nd|rd|th)?\s+(?P<m2>{_MONTH}),?\s+(?P<y>\d{{4}})\b", re.I),
    re.compile(rf"\bbetween\s+(?P<m>{_MONTH})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:and|to)\s+"
               rf"(?P<m2>{_MONTH})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+(?P<y>\d{{4}})\b", re.I),
)

#: ``(kind, is_weak, pattern)``: wording that says when the incident happened.
#: ``onset`` is when it began or happened, ``awareness`` when someone found it.
#: A weak cue ("attack on") also fits a report of an attack, so a publication
#: cue earlier in the sentence overrules it.
_CUES = (
    ("onset", False, re.compile(r"\b(?:occurred|occurring|took\s+place|taking\s+place|happened|began|started|"
                                r"commenced|commencing|first\s+(?:occurred|began|appeared))\b", re.I)),
    ("awareness", False, re.compile(r"\b(?:discovered|detected|identified|noticed|became\s+aware|learn(?:t|ed)|"
                                    r"first\s+(?:discovered|detected|noticed|identified))\b", re.I)),
    ("onset", True, re.compile(r"\b(?:attack(?:ed)?|breach(?:ed)?|incident|hack(?:ed)?|intrusion|compromised?|"
                               r"leak(?:ed)?|ransomware(?:\s+attack)?|hit|struck|targeted)\s+(?:on|from)\b", re.I)),
    ("onset", True, re.compile(r"\bbetween\b", re.I)),
)
#: Wording that dates a report, a byline or a statement, never the incident.
_PUBLICATION = re.compile(
    r"\b(?:published|posted|updated|modified|reported|reports?|announced|disclosed|confirmed|notified|"
    r"notification|statement|released|issued|said|stated|revealed|warned|advised|written|filed|copyright|"
    r"subscribe|comments?|shared)\b|©|\bby\s+[A-Z][a-z]+", re.I)
_BREACH_WORDS = re.compile(r"attack|breach|incident|hack|intrusion|compromis|ransomware|leak|exposed|stolen|"
                           r"unauthori[sz]ed|cyber", re.I)
#: A date that opens its sentence: "On 2 May 2025, the company detected ...".
_OPENS_WITH_ON = re.compile(r"^\s*(?:on|around|about)\s+(?:or\s+about\s+)?$", re.I)
_LEFT_CHARS = 120
_RIGHT_CHARS = 80
#: A tied date outside this window around the list heading's month is background ("a similar
#: attack in 2016"), not the incident. The list is by month of listing, so an incident can
#: precede its heading by months but cannot follow it.
HEADING_WINDOW_BEFORE_DAYS = 183
HEADING_WINDOW_AFTER_DAYS = 31


@dataclass(frozen=True)
class IncidentDate:
    day: date
    kind: str       # onset / awareness
    cue: str        # the wording that tied it
    sentence: str


def _calendar_dates(text: str) -> List[Tuple[int, int, date]]:
    """``(start, end, day)`` for every full calendar date in ``text``, in order of appearance."""
    found: Dict[int, Tuple[int, int, date]] = {}
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            groups = match.groupdict()
            try:
                month = int(groups["n"]) if groups.get("n") else _MONTH_NUMBER[groups["m"][:3].lower()]
                day = date(int(groups["y"]), month, int(groups["d"]))
            except (KeyError, ValueError):
                continue
            found.setdefault(match.start(), (match.start(), match.end(), day))
    for pattern in _RANGES:
        for match in pattern.finditer(text):
            groups = match.groupdict()
            try:
                month = _MONTH_NUMBER[(groups["m"] or groups["m2"])[:3].lower()]
                year = int(groups["y"]) - (1 if month > _MONTH_NUMBER[groups["m2"][:3].lower()] else 0)
                day = date(year, month, int(groups["d"]))
            except (KeyError, ValueError):
                continue
            # The start of the range, borrowing the month and year the end gives.
            found[match.start("d")] = (match.start("d"), match.end(), day)
    return [found[k] for k in sorted(found)]


def _sentence_start(text: str, position: int, floor: int) -> int:
    cut = max(text.rfind(". ", floor, position), text.rfind("\n", floor, position),
              text.rfind("! ", floor, position), text.rfind("? ", floor, position))
    return floor if cut < 0 else cut + 1


def _tying_cue(text: str, start: int, end: int, previous_end: int) -> Optional[Tuple[str, str]]:
    """``(kind, cue)`` when the wording around the date at ``start:end`` ties it to the incident, else None."""
    left_from = _sentence_start(text, start, max(previous_end, start - _LEFT_CHARS))
    left = text[left_from:start]
    cues = [(m.end(), kind, is_weak, m.group(0)) for kind, is_weak, pattern in _CUES for m in pattern.finditer(left)]
    publication = [m.end() for m in _PUBLICATION.finditer(left)]
    if cues:
        end_of_cue, kind, is_weak, wording = max(cues)
        if publication and (is_weak or max(publication) > end_of_cue):
            return None                       # "reported a breach on ...": a report's date, not the incident's
        if wording.lower() == "between" and not _BREACH_WORDS.search(text[left_from:end + _RIGHT_CHARS]):
            return None
        return kind, wording
    if publication or not _OPENS_WITH_ON.search(left):
        return None
    right = text[end:end + _RIGHT_CHARS]
    right = right[:right.find(". ")] if ". " in right else right
    first_publication = _PUBLICATION.search(right)
    for kind, _weak, pattern in _CUES[:2]:    # the wording follows the date: "..., the company detected"
        match = pattern.search(right)
        if match and (not first_publication or match.start() < first_publication.start()):
            return kind, match.group(0)
    return None


def incident_date_from_text(text: Optional[str], near: Optional[date] = None) -> Optional[IncidentDate]:
    """The day ``text`` says the incident happened, or None when no date is tied to it.

    A date counts only when the wording around it ties it to the breach:
    "occurred on", "took place", "discovered", "detected", "attack on",
    "between 1 May 2025 and 3 May 2025", or "On 2 May 2025 the company
    detected ...". A publication, byline, "updated" or announcement date
    never counts ("Published 1 June 2025", "By A. Writer January 31, 2020",
    "the company said on 5 May 2025"). Of several tied dates the first for
    when the incident began wins, else the first for when it was found.

    Args:
        near: A day of the month the source's list heading gives. A tied
            date outside the window around that month
            (``HEADING_WINDOW_BEFORE_DAYS`` / ``_AFTER_DAYS``) is background,
            not the incident.
    """
    body = text or ""
    low = high = None
    if near is not None:
        low = date(near.year, near.month, 1) - timedelta(days=HEADING_WINDOW_BEFORE_DAYS)
        high = (date(near.year, near.month, calendar.monthrange(near.year, near.month)[1])
                + timedelta(days=HEADING_WINDOW_AFTER_DAYS))
    tied: List[IncidentDate] = []
    previous_end = 0
    for start, end, day in _calendar_dates(body):
        cue = _tying_cue(body, start, end, previous_end)
        previous_end = end
        if cue is None or (low is not None and not low <= day <= high):
            continue
        sentence_from = _sentence_start(body, start, max(0, start - _LEFT_CHARS))
        tied.append(IncidentDate(day, cue[0], cue[1], body[sentence_from:end + _RIGHT_CHARS].strip()))
    for kind in ("onset", "awareness"):
        for found in tied:
            if found.kind == kind:
                return found
    return None


@dataclass
class WebberRedate:
    records: int = 0          # records read
    tied: int = 0             # heading-month records whose text ties a day to the incident -> day precision
    reverted: int = 0         # records the old rule dated from a day nothing ties -> heading month
    changed: int = 0          # records whose stored day moved to the day the text ties
    unresolved: int = 0       # old-rule records whose heading month is unknown (not changed)
    enriched: int = 0

    def summary(self) -> str:
        return (f"{self.records} Webber record(s) read: {self.tied} month(s) -> the day the text ties to the incident, "
                f"{self.changed} day(s) moved to the tied day, {self.reverted} untied day(s) -> the heading month, "
                f"{self.unresolved} untied day(s) whose heading month is unknown (left); "
                f"{self.enriched} enriched record(s) re-dated")


def rederive_webber_dates(conn: sqlite3.Connection, dry_run: bool = False,
                          headings: Optional[Dict[str, date]] = None) -> WebberRedate:
    """Apply :func:`incident_date_from_text` to the stored Webber records (idempotent). Commits unless ``dry_run``.

    * A record on its heading month whose stored page text ties a day to the
      incident gets that day (precision ``day``).
    * A record the old parser gave a day ("stated in the source page": the
      first date in the article, usually its byline) keeps it only if the text
      ties it; otherwise it moves to the tied day, or back to its heading month
      (``headings``: list URL -> any day of the heading's month, from the live
      list; without it such a record is counted as unresolved and left).

    The records' enrichments are re-dated where they held the old date; an
    event's own date follows by member consensus at the next deduplication
    (record fingerprints), not from one member.
    """
    ensure_schema(conn)
    out = WebberRedate()
    headings = headings or {}
    rows = conn.execute("""SELECT raw_event_id, event_date, event_date_basis, raw_content, source_url
                           FROM RawEvents WHERE source_type = 'WebberInsurance' AND raw_content IS NOT NULL
                             AND event_date_basis IN (?, ?)""", (BASIS_WEBBER_HEADING, BASIS_STATED_IN_SOURCE)).fetchall()
    for raw_id, stored, basis, content, url in rows:
        out.records += 1
        stored_day = _day(stored)
        if stored_day is None:
            continue
        heading = (date(stored_day.year, stored_day.month, 1) if basis == BASIS_WEBBER_HEADING else headings.get(url))
        found = incident_date_from_text(content, near=heading)
        if basis == BASIS_WEBBER_HEADING:
            if found is None:
                continue
            target, precision, new_basis = found.day, "day", BASIS_STATED_IN_SOURCE
            out.tied += 1
        elif found is not None and found.day == stored_day:
            continue
        elif found is not None:
            target, precision, new_basis = found.day, "day", BASIS_STATED_IN_SOURCE
            out.changed += 1
        elif heading is not None:
            target, precision, new_basis = date(heading.year, heading.month, 1), "month", BASIS_WEBBER_HEADING
            out.reverted += 1
        else:
            out.unresolved += 1
            continue
        if dry_run:
            continue
        conn.execute("UPDATE RawEvents SET event_date = ?, event_date_precision = ?, event_date_basis = ? "
                     "WHERE raw_event_id = ?", (target.isoformat(), precision, new_basis, raw_id))
        out.enriched += conn.execute(
            "UPDATE EnrichedEvents SET event_date = ? WHERE raw_event_id = ? AND substr(event_date, 1, 10) = ?",
            (target.isoformat(), raw_id, stored_day.isoformat())).rowcount
    if not dry_run:
        conn.commit()
    return out
