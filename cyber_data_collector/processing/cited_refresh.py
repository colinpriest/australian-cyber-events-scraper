"""Check pages the pipeline has already read, and rerun what a changed page affects.

A cited page was read once and then treated as permanently fresh
(``CitedDocumentStore.get_documents`` retries failures, never successes). But a
corrected notice, an updated victim list or a revised record count appears at
the *same URL*, so the facts that page supported, and the events built on them,
never saw it.

What this does, per run and within a budget:

1. **Choose** which read pages to check (:func:`plan_refresh`). Only pages cited
   by a record of an *active* event are eligible - a page nothing live rests on
   is not worth a request. A page is *due* when it was last checked longer ago
   than its interval: 14 days for a page that changed before or was itself
   published in the last 60 days, 45 days for a quiet one, doubling with each
   consecutive check that found nothing (to 180 days). Due pages are ranked by
   how much depends on them and at most ``limit`` are checked:

   ========================================================  ======
   a citing record has a *withheld* fact (it may now be       +4
   supported by the corrected page)
   an event it supports has an unresolved victim attribution  +3
   an event it supports has partial or unsupported evidence   +3
   an event it supports happened in the last 180 days         +2
   the page changed before                                    +2
   how overdue it is (0 to 2) and how many active events     +0..2.5
   cite it (0.5 each, up to 3)
   ========================================================  ======

2. **Check** them (:meth:`CitedDocumentStore.refresh`): conditional requests with
   the stored ``ETag`` / ``Last-Modified`` where the server provides them, a
   content fingerprint of the extracted text otherwise. The old version of a
   changed page stays in ``CitedDocumentVersions``.

3. **Rerun only what a changed page affects**: the records that cite it are
   judged again by the current acceptance gate against the cached pages (no
   fetching and no model: the judgement is the gate's rule), writing
   ``EnrichmentFactSupport``, the typed columns and the enrichment JSON through
   the same path as ``--recompute-reliability``. Records whose pages are not all
   cached are deferred, not judged on a missing page. A record whose
   enrichment content changed then changes its fingerprint
   (``RecordFingerprints``), which refreshes the events built on it (title,
   date, master-copied fields, facts, roles marked outdated), and the Jev
   judgements of those events are keyed on a hash of their evidence, so they
   are asked again only where the evidence changed. Pages that changed in a way
   that alters no fact cost nothing downstream.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from cyber_data_collector.processing import cited_documents
from cyber_data_collector.processing.active_evidence import member_condition
from cyber_data_collector.processing.cited_documents import (
    OUTCOME_CHANGED,
    CitedDocument,
    CitedDocumentStore,
    RefreshOutcome,
)
from cyber_data_collector.processing.enrichment_gate import valid_source_urls

logger = logging.getLogger(__name__)

#: Pages checked per run unless told otherwise: small and bounded.
DEFAULT_LIMIT = 25
#: Days between checks of a page that changed before, or was published recently.
ACTIVE_INTERVAL_DAYS = 14
#: Days between checks of a quiet page.
QUIET_INTERVAL_DAYS = 45
#: Each check that finds nothing doubles the interval, up to 2 ** BACKOFF_STEPS times ...
BACKOFF_STEPS = 3
#: ... and never beyond this.
MAX_INTERVAL_DAYS = 180
#: A page counts as changing recently when it changed within this many days.
RECENT_CHANGE_DAYS = 180
#: A page published within this many days of today is likely still being corrected.
RECENT_PAGE_DAYS = 60
#: An incident this recent is a consequential one.
RECENT_INCIDENT_DAYS = 180

W_WITHHELD, W_FLAGGED, W_WEAK_EVIDENCE, W_RECENT_INCIDENT, W_CHANGED_BEFORE = 4.0, 3.0, 3.0, 2.0, 2.0
W_PER_ACTIVE_EVENT, MAX_EVENT_WEIGHT_COUNT, MAX_OVERDUE_WEIGHT = 0.5, 3, 2.0


@dataclass
class Citation:
    """What rests on one cited page."""

    url: str
    record_ids: Set[str] = field(default_factory=set)
    event_ids: Set[str] = field(default_factory=set)
    has_withheld: bool = False
    is_flagged: bool = False
    has_weak_evidence: bool = False
    has_recent_incident: bool = False


@dataclass
class DocumentState:
    """The checking history of one stored page."""

    url: str
    fetched_at: str
    last_checked_at: Optional[str]
    published_date: Optional[str]
    last_changed_at: Optional[str]
    change_count: int
    unchanged_streak: int
    has_validators: bool


@dataclass
class DueDocument:
    url: str
    priority: float
    interval_days: float
    age_days: float
    reasons: List[str]


@dataclass
class RefreshPlan:
    """What a run would check, and the counts that explain the choice."""

    cached_total: int = 0
    readable_total: int = 0
    with_validators: int = 0
    cited_by_active: int = 0
    due: List[DueDocument] = field(default_factory=list)
    selected: List[DueDocument] = field(default_factory=list)
    limit: int = DEFAULT_LIMIT
    reasons_due: Counter = field(default_factory=Counter)

    def lines(self) -> List[str]:
        return [
            f"Cached cited pages: {self.cached_total} ({self.readable_total} read; "
            f"{self.with_validators} with an ETag or Last-Modified stored)",
            f"Read pages cited by an active event's record: {self.cited_by_active}",
            f"Due for a check: {len(self.due)}; budget {self.limit} per run -> {len(self.selected)} selected",
            "Selected because: " + (", ".join(f"{k} {v}" for k, v in self.reasons_due.most_common()) or "-"),
        ]


@dataclass
class RefreshReport:
    plan: RefreshPlan
    outcomes: List[RefreshOutcome] = field(default_factory=list)
    records_rejudged: int = 0
    records_deferred: int = 0
    rows_changed: int = 0
    rows_unvalidated: int = 0
    facts_withheld: Counter = field(default_factory=Counter)
    events_refreshed: int = 0
    changed_urls: List[str] = field(default_factory=list)
    is_dry_run: bool = False

    @property
    def counts(self) -> Counter:
        return Counter(o.outcome for o in self.outcomes)

    def lines(self) -> List[str]:
        out = list(self.plan.lines())
        if self.is_dry_run:
            out.append("DRY RUN: nothing fetched and no page or fact changed.")
            return out
        counts = self.counts
        via = Counter(o.via for o in self.outcomes)
        out.append("Checked: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none")
                   + (f" (by {', '.join(f'{k} {v}' for k, v in sorted(via.items()))})" if via else ""))
        failed = [o for o in self.outcomes if o.outcome == "failed"]
        for o in failed[:5]:
            out.append(f"  check failed, previous page kept: {o.url} ({o.error})")
        out.append(f"Changed pages: {len(self.changed_urls)}; records judged again: {self.records_rejudged} "
                   f"(deferred, a cited page not cached: {self.records_deferred}); "
                   f"records whose enrichment changed: {self.rows_changed} "
                   f"(re-queued for enrichment: {self.rows_unvalidated}); events refreshed: {self.events_refreshed}")
        return out


# ---------------------------------------------------------------------------
# What rests on each page
# ---------------------------------------------------------------------------

def _column(conn: sqlite3.Connection, table: str, name: str, alias: str) -> str:
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    return f"{alias}.{name}" if name in have else "NULL"


def load_citations(conn: sqlite3.Connection, today: datetime) -> Dict[str, Citation]:
    """Cited URL -> what rests on it, over the active records of active events.

    A record cites a URL when it is in the enrichment's ``sources_consulted`` or
    is the stored source of one of its supported facts.
    """
    evidence_status = _column(conn, "DeduplicatedEvents", "evidence_status", "d")
    rows = conn.execute(
        f"""SELECT e.enriched_event_id, e.perplexity_enrichment_data, d.deduplicated_event_id,
                   CAST(d.event_date AS TEXT), {evidence_status}
            FROM EventDeduplicationMap m
            JOIN DeduplicatedEvents d ON d.deduplicated_event_id = m.deduplicated_event_id
            JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
            WHERE COALESCE(d.status, 'Active') = 'Active' {member_condition(conn)}""").fetchall()
    events_of: Dict[str, List[Tuple[str, Optional[str], Optional[str]]]] = {}
    urls_of: Dict[str, Set[str]] = {}
    for record_id, blob, event_id, event_date, status in rows:
        events_of.setdefault(record_id, []).append((event_id, event_date, status))
        if not blob:
            continue
        try:
            parsed = json.loads(blob)
        except (TypeError, ValueError):
            continue            # an unreadable blob cites nothing the gate could read either
        if isinstance(parsed, dict):
            urls_of.setdefault(record_id, set()).update(valid_source_urls(parsed.get("sources_consulted")))

    withheld: Set[str] = set()
    for record_id, status, kind, url in conn.execute(
            "SELECT enriched_event_id, status, source_kind, source_url FROM EnrichmentFactSupport"):
        if record_id not in events_of:
            continue
        if status == "withheld":
            withheld.add(record_id)
        if kind == "cited_document" and url:
            urls_of.setdefault(record_id, set()).add(url)

    from cyber_data_collector.dedup import victim_judgement
    flagged_events = {event for event, _entity in victim_judgement.unresolved_attributions(conn)}
    recent_after = (today - timedelta(days=RECENT_INCIDENT_DAYS)).date().isoformat()

    citations: Dict[str, Citation] = {}
    for record_id, urls in urls_of.items():
        for url in urls:
            citation = citations.setdefault(url, Citation(url))
            citation.record_ids.add(record_id)
            citation.has_withheld = citation.has_withheld or record_id in withheld
            for event_id, event_date, status in events_of.get(record_id, []):
                citation.event_ids.add(event_id)
                citation.is_flagged = citation.is_flagged or event_id in flagged_events
                citation.has_weak_evidence = citation.has_weak_evidence or status in ("unsupported", "partial")
                citation.has_recent_incident = (citation.has_recent_incident
                                                or bool(event_date and str(event_date)[:10] >= recent_after))
    return citations


def load_states(conn: sqlite3.Connection) -> Tuple[int, Dict[str, DocumentState]]:
    """``(cached pages in total, {url: state})`` for the pages that were read."""
    cited_documents.ensure_schema(conn)
    total = conn.execute("SELECT COUNT(*) FROM CitedDocuments").fetchone()[0]
    states: Dict[str, DocumentState] = {}
    for (url, fetched_at, checked_at, published, changed_at, changes, streak, etag, modified) in conn.execute(
            "SELECT url, fetched_at, last_checked_at, published_date, last_changed_at, change_count, "
            "unchanged_streak, etag, last_modified FROM CitedDocuments WHERE status = ? AND text IS NOT NULL",
            (cited_documents.STATUS_OK,)):
        states[url] = DocumentState(url, fetched_at, checked_at, published, changed_at, int(changes or 0),
                                    int(streak or 0), bool(etag or modified))
    return total, states


# ---------------------------------------------------------------------------
# Which pages are due, and which first
# ---------------------------------------------------------------------------

def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:19])
    except ValueError:
        return None


def interval_days(state: DocumentState, now: datetime) -> float:
    """Days that must pass after the last check before this page is due again."""
    changed_at = _parse(state.last_changed_at)
    published = _parse(state.published_date)
    is_active = bool(
        (state.change_count > 0 and changed_at is not None and (now - changed_at).days <= RECENT_CHANGE_DAYS)
        or (published is not None and 0 <= (now - published).days <= RECENT_PAGE_DAYS))
    base = ACTIVE_INTERVAL_DAYS if is_active else QUIET_INTERVAL_DAYS
    return float(min(base * 2 ** min(state.unchanged_streak, BACKOFF_STEPS), MAX_INTERVAL_DAYS))


def plan_refresh(conn: sqlite3.Connection, now: Optional[datetime] = None,
                 limit: int = DEFAULT_LIMIT) -> RefreshPlan:
    """Which read pages to check this run (see the module docstring for the rule)."""
    now = now or datetime.now()
    total, states = load_states(conn)
    citations = load_citations(conn, now)
    plan = RefreshPlan(cached_total=total, readable_total=len(states),
                       with_validators=sum(1 for s in states.values() if s.has_validators), limit=limit)
    for url, state in states.items():
        citation = citations.get(url)
        if citation is None:
            continue                       # nothing live rests on this page
        plan.cited_by_active += 1
        checked = _parse(state.last_checked_at) or _parse(state.fetched_at)
        age = (now - checked).total_seconds() / 86400 if checked else float("inf")
        interval = interval_days(state, now)
        if age < interval:
            continue
        score, reasons = 0.0, []
        for is_true, weight, why in (
                (citation.has_withheld, W_WITHHELD, "withheld fact"),
                (citation.is_flagged, W_FLAGGED, "flagged attribution"),
                (citation.has_weak_evidence, W_WEAK_EVIDENCE, "weak evidence"),
                (citation.has_recent_incident, W_RECENT_INCIDENT, "recent incident"),
                (state.change_count > 0, W_CHANGED_BEFORE, "changed before")):
            if is_true:
                score += weight
                reasons.append(why)
        score += W_PER_ACTIVE_EVENT * min(len(citation.event_ids), MAX_EVENT_WEIGHT_COUNT)
        score += min(max(age / interval - 1.0, 0.0), MAX_OVERDUE_WEIGHT)
        plan.due.append(DueDocument(url, round(score, 4), interval, round(age, 1), reasons or ["overdue"]))
    plan.due.sort(key=lambda d: (-d.priority, d.url))
    plan.selected = plan.due[:max(0, int(limit))]
    for d in plan.selected:
        plan.reasons_due.update(d.reasons)
    return plan


# ---------------------------------------------------------------------------
# Rerun what a changed page affects
# ---------------------------------------------------------------------------

class _CacheOnlyStore(CitedDocumentStore):
    """The store as the acceptance gate's recheck sees it after a refresh: cached pages only, no fetching.

    The recheck must judge against what was read, not read more (a refresh's
    budget is its only network use).  Callers pass only records whose cited pages
    are all cached; a missing page is a logic error, never judged as a failure.
    """

    def get_documents(self, urls: Iterable[str],
                      progress: Any = None) -> Dict[str, CitedDocument]:
        wanted = list(dict.fromkeys(u for u in urls if u))
        documents = self.cached(wanted)
        missing = [u for u in wanted if u not in documents]
        if missing:
            raise RuntimeError(f"{len(missing)} cited page(s) not cached, e.g. {missing[0]}")
        return documents


def _rejudgeable(conn: sqlite3.Connection, store: CitedDocumentStore,
                 record_ids: Sequence[str]) -> Tuple[List[str], int]:
    """``(records whose cited pages are all cached, records deferred)``."""
    ready: List[str] = []
    deferred = 0
    for start in range(0, len(record_ids), 500):
        chunk = list(record_ids[start:start + 500])
        rows = conn.execute(
            "SELECT enriched_event_id, perplexity_enrichment_data FROM EnrichedEvents "
            f"WHERE enriched_event_id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
        urls_of: Dict[str, List[str]] = {}
        for record_id, blob in rows:
            try:
                parsed = json.loads(blob) if blob else {}
            except (TypeError, ValueError):
                parsed = {}
            urls_of[record_id] = valid_source_urls(parsed.get("sources_consulted")) if isinstance(parsed, dict) else []
        cached = store.cached(u for urls in urls_of.values() for u in urls)
        for record_id, urls in urls_of.items():
            if all(u in cached for u in urls):
                ready.append(record_id)
            else:
                deferred += 1
    return sorted(ready), deferred


def refresh_cited_documents(
    conn: sqlite3.Connection,
    store: CitedDocumentStore,
    *,
    limit: int = DEFAULT_LIMIT,
    dry_run: bool = False,
    now: Optional[datetime] = None,
    refresh_events: bool = True,
) -> RefreshReport:
    """Check the due pages (within *limit*), then rerun what the changed ones affect.

    Args:
        conn: Database connection (the store's).
        store: Fetches and stores pages; a fake fetcher keeps a test off the network.
        limit: Most pages checked this run (the budget; 0 checks none).
        dry_run: Choose and report; fetch and write nothing.
        refresh_events: Refresh the events built on records whose enrichment
            changed (``RecordFingerprints``); False leaves that to the next
            deduplication.

    Returns:
        A :class:`RefreshReport`.
    """
    plan = plan_refresh(conn, now, limit)
    report = RefreshReport(plan=plan, is_dry_run=dry_run)
    if dry_run or not plan.selected:
        return report

    report.outcomes = store.refresh([d.url for d in plan.selected])
    report.changed_urls = [o.url for o in report.outcomes if o.outcome == OUTCOME_CHANGED]
    logger.info("Cited pages checked: %s", dict(report.counts))
    if not report.changed_urls:
        return report

    citations = load_citations(conn, now or datetime.now())
    affected = sorted({r for url in report.changed_urls for r in citations.get(url, Citation(url)).record_ids})
    ready, report.records_deferred = _rejudgeable(conn, store, affected)
    report.records_rejudged = len(ready)
    if ready:
        from cyber_data_collector.processing.reliability_recheck import recheck_stored_enrichments

        cache_only = _CacheOnlyStore(conn, store._lock)
        recheck = recheck_stored_enrichments(conn, cache_only, dry_run=False, enriched_event_ids=ready)
        report.rows_changed = recheck.rows_changed
        report.rows_unvalidated = recheck.rows_unvalidated
        report.facts_withheld = recheck.facts_withheld
        if recheck.rows_changed and refresh_events:
            from cyber_data_collector.dedup.record_fingerprints import refresh_changed_events

            refreshed = refresh_changed_events(conn)
            conn.commit()
            report.events_refreshed = len(refreshed.refreshed_events)
    return report
