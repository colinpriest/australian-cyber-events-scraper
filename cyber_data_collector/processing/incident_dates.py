"""Incident dates as intervals with verified precision, kept apart from publication dates.

A date stored as ``2025-06-01`` may mean "1 June 2025" or "some day in June
2025", and nothing in the date says which. :mod:`dedup.date_provenance`
guesses (a 1st is "month only, or the 1st"); the recurrence analysis then
treats every 1st as ambiguous. Precision is recorded where it is known instead:

* ``RawEvents.event_date_precision`` / ``event_date_basis`` - what the source
  itself established: a date the source's page ties to the incident is a day.
  A Webber list heading ("Vertel - June 2025") is the month **Webber published**
  the item, not the incident's (owner, 2026-11-02): it is the record's
  publication month (the 15th of it, imputed) and an upper bound on the incident,
  never an incident date (:func:`webber_month_is_publication`).
* Perplexity's ``date_precision`` counts only for a date the citation check
  accepted (``EnrichmentFactSupport`` fact ``date`` = ``supported``).
* ``DeduplicatedEvents.event_date_precision`` is set only from verified
  evidence, reconciled by :func:`reconcile_precision`: sources that give a
  month and a day inside it are compatible and the event takes the narrower;
  verified evidence for a different date is a conflict (NULL), as is no
  verified evidence (unverified). ``event_date_earliest`` /
  ``event_date_latest`` give the interval (a month runs from its 1st to its
  last day; a year from 1 January to 31 December; a day is itself; unverified
  gives no interval - the date is unknown, not a day).

Webber records were stored on the 15th of the heading month - a day nobody
reported - and the heading month outranked any date the article stated. All
832 Webber records on 2026-10-28 were on the 15th. :func:`repair_webber_dates`
moved them to the month interval (stored as the 1st, precision ``month``);
:func:`webber_month_is_publication` then takes that month off as an incident
date, because it is the month Webber published the item.

A day the article states is the *incident's* only when its wording ties it to
the breach (:func:`incident_date_from_text`): "the breach occurred on 2 May
2025", "discovered on", "attack on", "between 1 and 3 May 2025". The first date
anywhere in an article is usually its byline or publication date ("Published 1
June 2025"), which the parser used to store as an exact incident date.
Publication, byline, "updated" and announcement dates never count; without a
tied date the record has no incident date of its own (until 2026-11-02 it kept
the list heading's month, which is Webber's publication month).
:func:`rederive_webber_dates` applies the rule to the stored records.
"""

from __future__ import annotations

import calendar
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

PRECISIONS = ("day", "month", "year")
BASIS_WEBBER_HEADING = "Webber list heading (month)"
BASIS_STATED_IN_SOURCE = "stated in the source page"
BASIS_WEBBER_PUBLICATION = "none: the Webber list month is when Webber published the item, not the incident date"


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


def _record_evidence(raw_date: Any, raw_precision: Optional[str], enriched_date: Any,
                     enrichment_json: Optional[str], date_supported: bool) -> List[Tuple[str, str, str]]:
    """``(precision, earliest, latest)`` for each verified date a record gives, wherever the date falls.

    A record's own source-stated precision, and the precision Perplexity reported for a date the
    citation check accepted. Evidence that disagrees with the event's date is returned too:
    :func:`reconcile_precision` decides whether it is compatible, rather than dropping it here.
    """
    found: List[Tuple[str, str, str]] = []
    if raw_precision in PRECISIONS and _day(raw_date) is not None:
        start, end = interval(raw_date, raw_precision)
        found.append((raw_precision, start, end))
    if date_supported and _day(enriched_date) is not None:
        try:
            data = json.loads(enrichment_json or "{}")
        except (TypeError, ValueError):
            data = {}
        precision = data.get("date_precision") if isinstance(data, dict) else None
        if precision in PRECISIONS:
            start, end = interval(enriched_date, precision)
            found.append((precision, start, end))
    return found


def reconcile_precision(event_day: date, evidence: Iterable[Tuple[str, str, str]]) -> Tuple[Optional[str], str]:
    """The precision the evidence establishes for an event's date, and the status of that finding.

    Calendar intervals are nested or disjoint: a day lies inside its month and a month inside its
    year. Evidence whose interval contains the event's date is about that date, and the sources that
    give a year, a month and a day are *compatible*: the narrowest of them is the event's precision
    (a month and a day inside it reconcile to the day, not to a conflict). Evidence that does not
    contain the event's date is compatible only if it lies inside that narrowest interval (a more
    exact day elsewhere in the month; the interval stays the one holding the event's stored date);
    evidence disjoint from it is *incompatible* - verified sources disagree about when the incident
    happened - and the event is ``conflicting`` with no precision. Returns ``(precision, status)``;
    status is ``verified``, ``conflicting`` or ``unverified`` (no verified evidence at all).
    """
    evidence = list(evidence)
    if not evidence:
        return None, "unverified"
    day = event_day.isoformat()
    inside = [item for item in evidence if item[1] <= day <= item[2]]
    if not inside:
        return None, "conflicting"
    narrowest = min(inside, key=lambda item: (date.fromisoformat(item[2]) - date.fromisoformat(item[1])).days)
    for _, start, end in evidence:
        if not (start <= day <= end) and (end < narrowest[1] or start > narrowest[2]):
            return None, "conflicting"
    return narrowest[0], "verified"


@dataclass
class PrecisionReport:
    events: int = 0
    verified: Dict[str, int] = field(default_factory=dict)
    conflicting: List[str] = field(default_factory=list)
    unverified: int = 0
    #: Events whose sources gave different but compatible precisions, reconciled to the narrower.
    reconciled: int = 0

    def summary(self) -> str:
        parts = ", ".join(f"{n} {p}" for p, n in sorted(self.verified.items()))
        return (f"incident-date precision verified for {sum(self.verified.values())} of {self.events} active "
                f"event(s) ({parts or 'none'}; {self.reconciled} reconciled from compatible month/day evidence); "
                f"{len(self.conflicting)} conflicting; {self.unverified} unverified")


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
        evidence: List[Tuple[str, str, str]] = []
        if event_day is not None:
            for enriched_id, raw_date, raw_precision, enriched_date, blob in conn.execute(
                    """SELECT e.enriched_event_id, r.event_date, r.event_date_precision, e.event_date,
                              e.perplexity_enrichment_data
                       FROM EventDeduplicationMap m
                       JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
                       JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
                       WHERE m.deduplicated_event_id = ? AND COALESCE(e.status, 'Active') = 'Active'""",
                    (dedup_id,)):
                evidence.extend(_record_evidence(raw_date, raw_precision, enriched_date, blob,
                                                 enriched_id in supported))
            precision, status = reconcile_precision(event_day, evidence)
        else:
            precision, status = None, "unverified"
        if status == "verified":
            report.verified[precision] = report.verified.get(precision, 0) + 1
            if len({p for p, start, end in evidence if start <= event_day.isoformat() <= end}) > 1:
                report.reconciled += 1
        elif status == "conflicting":
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
    reverted: int = 0         # records the old rule dated from a day nothing ties -> no incident date
    changed: int = 0          # records whose stored day moved to the day the text ties
    unresolved: int = 0       # old-rule records whose heading month is unknown (not changed)
    enriched: int = 0

    def summary(self) -> str:
        return (f"{self.records} Webber record(s) read: {self.tied} month(s) -> the day the text ties to the incident, "
                f"{self.changed} day(s) moved to the tied day, {self.reverted} untied day(s) -> no incident date, "
                f"{self.unresolved} untied day(s) whose heading month is unknown (left); "
                f"{self.enriched} enriched record(s) re-dated")


@dataclass
class WebberPublication:
    records: int = 0
    enriched: int = 0
    publication_set: int = 0

    def summary(self) -> str:
        return (f"{self.records} Webber record(s) no longer take the list month as their incident date (it is Webber's "
                f"publication month); {self.enriched} research date(s) copied from it cleared; "
                f"{self.publication_set} publication month(s) recorded")


def webber_month_is_publication(conn: sqlite3.Connection, dry_run: bool = False) -> WebberPublication:
    """The Webber list month is the month Webber published the item, not the incident's (owner, 2026-11-02).

    Records still dated by the heading (basis :data:`BASIS_WEBBER_HEADING`) lose the incident date: the heading
    month becomes their publication month where they have none (the 15th, imputed, source ``source``) and an
    enrichment date copied from the heading is cleared, so the event is dated by what the reporting states, or by
    its first report. A day the article ties to the incident is kept (:func:`rederive_webber_dates`). Idempotent;
    commits unless ``dry_run``.
    """
    ensure_schema(conn)
    out = WebberPublication()
    rows = conn.execute("""SELECT raw_event_id, event_date, publication_date FROM RawEvents
                           WHERE source_type = 'WebberInsurance' AND event_date_basis = ?""",
                        (BASIS_WEBBER_HEADING,)).fetchall()
    for raw_id, stored, published in rows:
        day = _day(stored)
        out.records += 1
        if dry_run:
            continue
        conn.execute("UPDATE RawEvents SET event_date = NULL, event_date_precision = NULL, event_date_basis = ? "
                     "WHERE raw_event_id = ?", (BASIS_WEBBER_PUBLICATION, raw_id))
        if day is not None and not published:
            conn.execute("UPDATE RawEvents SET publication_date = ?, publication_date_source = 'source' "
                         "WHERE raw_event_id = ?", (date(day.year, day.month, 15).isoformat(), raw_id))
            out.publication_set += 1
        if day is not None:
            out.enriched += conn.execute(
                "UPDATE EnrichedEvents SET event_date = NULL WHERE raw_event_id = ? AND substr(event_date, 1, 10) = ?",
                (raw_id, day.isoformat())).rowcount
    if not dry_run:
        conn.commit()
    return out


def rederive_webber_dates(conn: sqlite3.Connection, dry_run: bool = False,
                          headings: Optional[Dict[str, date]] = None) -> WebberRedate:
    """Apply :func:`incident_date_from_text` to the stored Webber records (idempotent). Commits unless ``dry_run``.

    * A record with no incident date of its own (or still on its heading month) whose stored page text ties a
      day to the incident gets that day (precision ``day``); the publication month bounds the search.
    * A record the old parser gave a day ("stated in the source page": the
      first date in the article, usually its byline) keeps it only if the text
      ties it; otherwise it moves to the tied day, or has no incident date (the
      month bounding the search is its publication month, else ``headings``:
      list URL -> any day of the heading's month, from the live list; with
      neither such a record is counted as unresolved and left).

    The records' enrichments are re-dated where they held the old date; an
    event's own date follows by member consensus at the next deduplication
    (record fingerprints), not from one member.
    """
    ensure_schema(conn)
    out = WebberRedate()
    headings = headings or {}
    rows = conn.execute("""SELECT raw_event_id, event_date, event_date_basis, raw_content, source_url,
                                  publication_date
                           FROM RawEvents WHERE source_type = 'WebberInsurance' AND raw_content IS NOT NULL
                             AND event_date_basis IN (?, ?, ?)""",
                        (BASIS_WEBBER_HEADING, BASIS_STATED_IN_SOURCE, BASIS_WEBBER_PUBLICATION)).fetchall()
    for raw_id, stored, basis, content, url, published in rows:
        out.records += 1
        stored_day, published_day = _day(stored), _day(published)
        if basis == BASIS_WEBBER_HEADING and stored_day is not None:
            month = date(stored_day.year, stored_day.month, 1)
        elif published_day is not None:
            month = date(published_day.year, published_day.month, 1)
        else:
            month = headings.get(url)
        found = incident_date_from_text(content, near=month)
        if basis in (BASIS_WEBBER_HEADING, BASIS_WEBBER_PUBLICATION):
            if found is None or found.day == stored_day:
                continue
            target, precision, new_basis = found.day, "day", BASIS_STATED_IN_SOURCE
            out.tied += 1
        elif stored_day is None:
            continue
        elif found is not None and found.day == stored_day:
            continue
        elif found is not None:
            target, precision, new_basis = found.day, "day", BASIS_STATED_IN_SOURCE
            out.changed += 1
        elif month is not None:
            target, precision, new_basis = None, None, BASIS_WEBBER_PUBLICATION
            out.reverted += 1
        else:
            out.unresolved += 1
            continue
        if dry_run:
            continue
        conn.execute("UPDATE RawEvents SET event_date = ?, event_date_precision = ?, event_date_basis = ? "
                     "WHERE raw_event_id = ?", (target.isoformat() if target else None, precision, new_basis, raw_id))
        if stored_day is not None:
            out.enriched += conn.execute(
                "UPDATE EnrichedEvents SET event_date = ? WHERE raw_event_id = ? AND substr(event_date, 1, 10) = ?",
                (target.isoformat() if target else None, raw_id, stored_day.isoformat())).rowcount
    if not dry_run:
        conn.commit()
    return out


#: Days an incident date may run past its record's publication date (time zones, a date-only stamp) before it is
#: impossible.
PUBLICATION_SLACK_DAYS = 7
BASIS_AFTER_PUBLICATION = "removed: dated after the record's own publication date"

IMPOSSIBLE_DATES_SCHEMA = """
CREATE TABLE IF NOT EXISTS ImpossibleIncidentDates (
    raw_event_id      TEXT NOT NULL,
    enriched_event_id TEXT,
    field             TEXT NOT NULL,     -- raw incident date / research date (with the research's count)
    removed_value     TEXT,
    publication_date  TEXT NOT NULL,
    removed_at        TEXT NOT NULL
);
"""


@dataclass
class ImpossibleDates:
    raw_dates: int = 0
    research: int = 0
    examples: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.raw_dates} record incident date(s) and {self.research} research answer(s) dated after the "
                f"record's own publication date removed; the research is queued again")


def remove_dates_after_publication(conn: sqlite3.Connection, dry_run: bool = False) -> ImpossibleDates:
    """An incident cannot happen after the report of it was published (owner, 2026-11-02).

    Webber's May 2024 entry for Cyber Daily's report of 1 May 2024 on the Qantas app fault was researched as
    "Qantas": Perplexity answered with the July 2025 attack, its date and its 6,000,000 records, and the record became
    a second "July 2025" Qantas event. For every active record with a publication date the source gave for the item
    itself (``publication_date_source = 'source'``: a feed's or listing's date; Webber's is the month Webber
    published the item with an imputed 15th, so its bound is the end of that month). OAIC's is not used: every OAIC
    record carries 2023-03-10, whatever its incident. Nor is a page's own metadata date: an evergreen page keeps its
    first publication date while its content is updated (an ACSC warning of 2026 carries 2013):

    * a record incident date more than :data:`PUBLICATION_SLACK_DAYS` after it is removed (the record is then dated
      by what else it states, or by its publication, as any undated record);
    * research that dates the incident after it described another, later incident: its date and records count are
      withheld from the record and the research is queued again.

    Each removal is kept in ``ImpossibleIncidentDates``. Idempotent. Commits unless ``dry_run``.
    """
    conn.executescript(IMPOSSIBLE_DATES_SCHEMA)
    out = ImpossibleDates()
    now = datetime.now().isoformat()
    rows = conn.execute(
        """SELECT r.raw_event_id, r.event_date, r.publication_date, e.enriched_event_id, e.event_date,
                  e.records_affected, e.title, r.source_type
           FROM RawEvents r JOIN EnrichedEvents e ON e.raw_event_id = r.raw_event_id AND e.status = 'Active'
           WHERE r.publication_date IS NOT NULL AND r.publication_date_source = 'source'
             AND r.source_type != 'OAIC'""").fetchall()
    for raw_id, raw_date, published, enriched_id, enriched_date, count, title, source_type in rows:
        limit = _day(published)
        if limit is None:
            continue
        if source_type == "WebberInsurance":
            limit = date(limit.year, limit.month, calendar.monthrange(limit.year, limit.month)[1])
        limit = limit + timedelta(days=PUBLICATION_SLACK_DAYS)
        raw_day, research_day = _day(raw_date), _day(enriched_date)
        if raw_day is not None and raw_day > limit:
            out.raw_dates += 1
            out.examples.append(f"{title}: incident {raw_day} after publication {published}")
            if not dry_run:
                conn.execute("UPDATE RawEvents SET event_date = NULL, event_date_precision = NULL, "
                             "event_date_basis = ? WHERE raw_event_id = ?", (BASIS_AFTER_PUBLICATION, raw_id))
                conn.execute("INSERT INTO ImpossibleIncidentDates VALUES (?, ?, 'raw incident date', ?, ?, ?)",
                             (raw_id, enriched_id, str(raw_date), published, now))
        if research_day is not None and research_day > limit:
            out.research += 1
            if not dry_run:
                conn.execute(
                    "UPDATE EnrichedEvents SET event_date = NULL, records_affected = NULL, perplexity_validated = 0, "
                    "perplexity_validated_at = NULL, perplexity_status = NULL, perplexity_attempt_count = 0, "
                    "perplexity_last_error = NULL WHERE enriched_event_id = ?", (enriched_id,))
                conn.execute("INSERT INTO ImpossibleIncidentDates VALUES (?, ?, 'research date and count', ?, ?, ?)",
                             (raw_id, enriched_id, f"{enriched_date} / {count}", published, now))
    if not dry_run:
        conn.commit()
    return out
