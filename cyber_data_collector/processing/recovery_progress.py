"""How far each evidence-recovery route has got: queued, attempted, succeeded, failing, exhausted, and how old.

Evidence recovery (:mod:`~cyber_data_collector.processing.evidence_recovery`)
works through five routes, each backed by a durable queue:

``cyberdaily_refetch``   stored Cyber Daily stories with cut-off or short text (``CyberDailyRefetches``)
``page_rescrape``        other pages queued for a second fetch (``PageRescrapes``)
``research``             Perplexity research of active specific records (``EnrichedEvents.perplexity_*``)
``label_report``         the report a label cites, queued as its own record (``label_evidence``)
``replacement_search``   one stored search whose best citation becomes a new record (``SourceReplacements``)

The 2026-10-29 quality report said 122 incidents awaited recovery and 2,723
stories awaited their re-fetch, but not whether either number was falling, how
the attempts had gone or how long the oldest item had waited. :func:`measure`
answers per route, with one definition throughout:

* ``queued``     waiting for a first attempt
* ``attempted``  at least one attempt made (``succeeded + failing + exhausted``)
* ``succeeded``  the attempt produced what the route is for
* ``failing``    attempted, failed, and still scheduled for another try; split by failure type
* ``exhausted``  given up (a missing page, an attempt cap, a quarantine)
* ``oldest_queued_days``  the longest any queued item has waited

Reads only; the quality report records each number on every run
(``recovery.route.<route>.<field>``) so a trend is one query away.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from cyber_data_collector.processing import fetch_errors

CYBERDAILY_REFETCH = "cyberdaily_refetch"
PAGE_RESCRAPE = "page_rescrape"
RESEARCH = "research"
LABEL_REPORT = "label_report"
REPLACEMENT_SEARCH = "replacement_search"
ROUTES = (CYBERDAILY_REFETCH, PAGE_RESCRAPE, RESEARCH, LABEL_REPORT, REPLACEMENT_SEARCH)

DESCRIPTIONS = {
    CYBERDAILY_REFETCH: "stored Cyber Daily stories with cut-off or short text",
    PAGE_RESCRAPE: "pages queued for a second fetch (non-Cyber Daily)",
    RESEARCH: "Perplexity research of active specific records",
    LABEL_REPORT: "reports cited by labels, queued as records",
    REPLACEMENT_SEARCH: "replacement searches for incidents with no evidence",
}


@dataclass
class RouteProgress:
    route: str
    queued: int = 0
    succeeded: int = 0
    exhausted: int = 0
    failing_by_type: Dict[str, int] = field(default_factory=dict)
    oldest_queued_days: Optional[float] = None
    oldest_failing_days: Optional[float] = None       # days since the longest-failing item was last tried

    @property
    def failing(self) -> int:
        return sum(self.failing_by_type.values())

    @property
    def attempted(self) -> int:
        return self.succeeded + self.failing + self.exhausted

    @property
    def total(self) -> int:
        return self.queued + self.attempted

    def to_dict(self) -> Dict[str, Any]:
        return {"route": self.route, "queued": self.queued, "attempted": self.attempted,
                "succeeded": self.succeeded, "failing": self.failing, "failing_by_type": dict(self.failing_by_type),
                "exhausted": self.exhausted, "oldest_queued_days": self.oldest_queued_days,
                "oldest_failing_days": self.oldest_failing_days}

    def describe(self) -> str:
        failing = ", ".join(f"{n} {t}" for t, n in sorted(self.failing_by_type.items())) or "0"
        age = f", oldest queued {self.oldest_queued_days:.0f} days" if self.oldest_queued_days is not None else ""
        return (f"{self.queued} queued, {self.attempted} attempted ({self.succeeded} succeeded, "
                f"{self.failing} failing [{failing}], {self.exhausted} exhausted){age}")


def _days_since(stamp: Optional[str], now: datetime) -> Optional[float]:
    if not stamp:
        return None
    try:
        then = datetime.fromisoformat(str(stamp).replace(" ", "T")[:26])
    except ValueError:
        return None
    return max(0.0, (now - then).total_seconds() / 86400)


def _oldest(stamps: List[Optional[str]], now: datetime) -> Optional[float]:
    ages = [a for a in (_days_since(s, now) for s in stamps) if a is not None]
    return round(max(ages), 1) if ages else None


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)).fetchone())


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _failure_type(error: Optional[str], stored_type: Optional[str] = None) -> str:
    if stored_type in fetch_errors.ERROR_TYPES:
        return stored_type
    return fetch_errors.error_type_from_text(error)


def cyberdaily_refetch(conn: sqlite3.Connection, now: datetime) -> RouteProgress:
    from cyber_data_collector.processing import cyberdaily_refetch as cr

    progress = RouteProgress(CYBERDAILY_REFETCH)
    queued, retrying = [], []
    state = cr.queue_state(conn)
    for row in state:
        if not row["attempted"]:
            progress.queued += 1
            queued.append(row["discovered_at"])
        elif row["gave_up_at"]:
            progress.exhausted += 1
        elif row["error_type"]:
            kind = _failure_type(None, row["error_type"])
            progress.failing_by_type[kind] = progress.failing_by_type.get(kind, 0) + 1
            retrying.append(row["fetched_at"])
        elif row["incomplete"]:
            # Fetched, text unchanged and still short, but not (yet) typed a failure: queued again.
            progress.queued += 1
            queued.append(row["fetched_at"])
        else:
            progress.succeeded += 1
    # Stories repaired or confirmed complete are no longer in the queue: count them from the log.
    in_queue = {row["raw_event_id"] for row in state}
    progress.succeeded += sum(1 for (raw_id,) in conn.execute(
        "SELECT raw_event_id FROM CyberDailyRefetches WHERE error_type IS NULL AND gave_up_at IS NULL")
        if raw_id not in in_queue)
    progress.oldest_queued_days = _oldest(queued, now)
    progress.oldest_failing_days = _oldest(retrying, now)
    return progress


def page_rescrape(conn: sqlite3.Connection, now: datetime) -> RouteProgress:
    from cyber_data_collector.processing import page_rescrape as pr

    progress = RouteProgress(PAGE_RESCRAPE)
    queued, retrying = [], []
    for status, error, stamp in conn.execute("SELECT status, last_error, last_attempt_at FROM PageRescrapes"):
        if status == pr.STATUS_QUEUED:
            progress.queued += 1
            queued.append(stamp)
        elif status == pr.STATUS_STORED:
            progress.succeeded += 1
        elif status == pr.STATUS_GAVE_UP:
            progress.exhausted += 1
        else:
            kind = _failure_type(error)
            progress.failing_by_type[kind] = progress.failing_by_type.get(kind, 0) + 1
            retrying.append(stamp)
    progress.oldest_queued_days = _oldest(queued, now)
    progress.oldest_failing_days = _oldest(retrying, now)
    return progress


def research(conn: sqlite3.Connection, now: datetime) -> RouteProgress:
    from cyber_data_collector.processing.enrichment_gate import MAX_ENRICHMENT_ATTEMPTS

    progress = RouteProgress(RESEARCH)
    queued, retrying = [], []
    for validated, status, attempts, error, created, last in conn.execute(
            """SELECT e.perplexity_validated, e.perplexity_status, COALESCE(e.perplexity_attempt_count, 0),
                      e.perplexity_last_error, CAST(e.created_at AS TEXT), CAST(e.perplexity_last_attempt_at AS TEXT)
               FROM EnrichedEvents e
               WHERE e.status = 'Active' AND e.is_specific_event IN (1, '1', 'true', 'True')"""):
        if validated in (1, "1", True):
            progress.succeeded += 1
        elif status == "quarantined" or attempts >= MAX_ENRICHMENT_ATTEMPTS:
            progress.exhausted += 1
        elif attempts == 0:
            progress.queued += 1
            queued.append(created)
        else:
            kind = _failure_type(error)
            progress.failing_by_type[kind] = progress.failing_by_type.get(kind, 0) + 1
            retrying.append(last)
    progress.oldest_queued_days = _oldest(queued, now)
    progress.oldest_failing_days = _oldest(retrying, now)
    return progress


def label_report(conn: sqlite3.Connection, now: datetime) -> RouteProgress:
    from cyber_data_collector.dedup import label_evidence

    progress = RouteProgress(LABEL_REPORT)
    for queued in label_evidence.queued_reports(conn):
        if queued.get("processed"):
            progress.succeeded += 1
        else:
            progress.queued += 1
    return progress


def replacement_search(conn: sqlite3.Connection, now: datetime) -> RouteProgress:
    from cyber_data_collector.dedup import source_gate
    from cyber_data_collector.processing import evidence_recovery as er

    progress = RouteProgress(REPLACEMENT_SEARCH)
    queued, retrying = [], []
    for status, error, raw_id, source_url, processed, rejected, created in conn.execute(
            """SELECT s.status, s.last_error, s.raw_event_id, r.source_url, COALESCE(r.is_processed, 0),
                      r.processing_error, COALESCE(s.updated_at, s.created_at)
               FROM SourceReplacements s LEFT JOIN RawEvents r ON r.raw_event_id = s.raw_event_id
               WHERE s.purpose = ?""", (er.PURPOSE,)):
        if status == source_gate.STATUS_PENDING:
            kind = _failure_type(error)
            progress.failing_by_type[kind] = progress.failing_by_type.get(kind, 0) + 1
            retrying.append(created)
        elif not raw_id or source_url is None or rejected:
            progress.exhausted += 1            # nothing found, record removed, or the report was rejected
        elif not processed:
            progress.queued += 1               # the replacement record waits to be scraped and enriched
            queued.append(created)
        else:
            progress.succeeded += 1
    progress.oldest_queued_days = _oldest(queued, now)
    progress.oldest_failing_days = _oldest(retrying, now)
    return progress


_MEASURES = (
    (CYBERDAILY_REFETCH, cyberdaily_refetch, ("RawEvents", "CyberDailyRefetches", "EnrichedEvents")),
    (PAGE_RESCRAPE, page_rescrape, ("PageRescrapes",)),
    (RESEARCH, research, ("EnrichedEvents",)),
    (LABEL_REPORT, label_report, ("RawEvents",)),
    (REPLACEMENT_SEARCH, replacement_search, ("SourceReplacements", "RawEvents")),
)


def measure(conn: sqlite3.Connection, now: Optional[datetime] = None) -> Dict[str, Any]:
    """``{route: RouteProgress}`` for every route whose tables exist, ``{route: reason}`` for the rest.

    A route that cannot be read is reported with its reason (the quality
    report records it as not measured), never left out or counted as empty.
    """
    now = now or datetime.now()
    out: Dict[str, Any] = {}
    for route, function, tables in _MEASURES:
        missing = [t for t in tables if not _has_table(conn, t)]
        if missing:
            out[route] = f"{', '.join(missing)} missing (run dedup_admin.py recover-evidence)"
            continue
        needed = {"perplexity_last_attempt_at", "created_at", "perplexity_attempt_count"}
        if route == RESEARCH and not needed <= _columns(conn, "EnrichedEvents"):
            out[route] = "EnrichedEvents.perplexity_* columns missing"
            continue
        if route == REPLACEMENT_SEARCH and "purpose" not in _columns(conn, "SourceReplacements"):
            out[route] = "SourceReplacements.purpose missing (run dedup_admin.py recover-evidence)"
            continue
        try:
            out[route] = function(conn, now)
        except sqlite3.Error as exc:         # a schema this route cannot read: reported, never a zero
            out[route] = f"{type(exc).__name__}: {exc}"
    return out


def failure_types(progress: Dict[str, Any]) -> List[str]:
    """Every failure type seen on any route, in :data:`fetch_errors.ERROR_TYPES` order."""
    seen = Counter()
    for item in progress.values():
        if isinstance(item, RouteProgress):
            seen.update(item.failing_by_type)
    return [t for t in fetch_errors.ERROR_TYPES if t in seen]
