"""What each incident's evidence lacks, retrieval of what is missing, and an evidence status per incident.

The 2026-10-28 quality report found 169 of 1,202 active incidents none of
whose records held 300 characters of stored page text or validated Perplexity
research. That does not make them false, but nothing in the database can then
verify their fields. 121 of the 152 such incidents on the evening copy were
Cyber Daily stories ingested that day with no page text at all (classified
from the feed's standfirst) and never researched.

This module does four things, every one idempotent (the fourth, :func:`drain`,
repeats the second with the fetching and research in between):

1. **Diagnose** (:func:`assess_events`) every active incident's records: is the
   page stored (the record's own page, another page, a block page, too short,
   missing)? Is the research validated, waiting, retrying, quarantined or not
   wanted? Has a re-fetch been tried (``PageRescrapes``, ``CyberDailyRefetches``),
   a replacement searched (``SourceReplacements``) or a labelled report queued
   (``label-evidence``)?
2. **Queue retrieval** (:func:`recover`) for incidents with no usable evidence,
   through the existing mechanisms only, at most ``limit`` incidents a run:

   * the record's own page fetched again - Cyber Daily stories by the Cyber
     Daily re-fetch (discovery phase), any other URL by ``rescrape-short-pages``
     (:func:`page_rescrape.queue_pages`); new real text queues the record's
     unvalidated research again;
   * Perplexity research is already a durable queue (``perplexity_backfill_events``,
     run by the enrichment phase; records of unsupported incidents go first);
   * the report a label cites (``label_evidence.queue(include_events=...)``);
   * when the record's own page and research are both out of reach, one stored
     replacement search (``source_gate.search_and_queue``): its best citation is
     queued as a **new record with its own URL**, scraped and enriched like any
     other, and joined to the incident only by the calibrated duplicate check.

   Alternate text is never passed off as the record's page: text whose
   ``content_origin`` is another page counts as that page's evidence, says so in
   the reason, and leaves the incident ``partial`` at best.
3. **Mark uncertainty** (:func:`mark_status`): ``DeduplicatedEvents.evidence_status``
   is ``supported`` (its own page text and validated research), ``partial`` (one
   of them, or only another page's text) or ``unsupported`` (neither), with the
   reason, and ``evidence_uncertain_fields`` lists the fields of an unsupported
   incident that nothing supports. Exports, dashboards and the quality report
   show them; the status changes as evidence arrives.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from cyber_data_collector.dedup import label_evidence, source_gate
from cyber_data_collector.dedup.event_reality import LABELS_PATH as EVENT_LABELS_PATH
from cyber_data_collector.processing import page_rescrape
from cyber_data_collector.processing.enrichment_gate import MAX_ENRICHMENT_ATTEMPTS
from cyber_data_collector.processing.record_evidence import ORIGIN_PERPLEXITY_ALTERNATIVE, content_origin
from cyber_data_collector.reporting.population import incident_filter

logger = logging.getLogger(__name__)

MIN_PAGE_CHARS = source_gate.MIN_PAGE_CHARS
#: Incidents given new retrieval work per run.
DEFAULT_LIMIT = 100
PURPOSE = "evidence_recovery"
ORIGIN = "evidence recovery replacement search"

SUPPORTED = "supported"
PARTIAL = "partial"
UNSUPPORTED = "unsupported"
STATUSES = (SUPPORTED, PARTIAL, UNSUPPORTED)

# What a record's page holds.
PAGE_OWN = "own page"
PAGE_ALTERNATE = "another page"
PAGE_BLOCKED = "block page"
PAGE_SHORT = "short"
PAGE_MISSING = "missing"
#: The stored page is another article (an open ``page_other_article`` finding of
#: ``record_identity``): not the record's own page, so it is fetched again.
PAGE_OTHER_ARTICLE = "another article"

# Where a record's research stands.
RESEARCH_VALIDATED = "validated"
RESEARCH_WAITING = "not attempted"
RESEARCH_RETRYING = "retrying"
RESEARCH_QUARANTINED = "quarantined"
RESEARCH_EXHAUSTED = "attempts used up"
RESEARCH_NOT_WANTED = "not a specific event"

# Where retrieval of one kind of evidence stands.
ROUTE_DONE = "done"            # nothing missing
ROUTE_NEEDED = "needed"        # nothing asked for yet
ROUTE_PENDING = "pending"      # asked for; waiting for a later step or run
ROUTE_EXHAUSTED = "exhausted"  # every attempt made; out of reach

#: An unsupported incident's fields, all of which are then uncertain (when set).
UNCERTAIN_FIELDS = ("title", "event_date", "event_type", "severity", "victim_organization_name",
                    "attacking_entity_name", "attack_method", "records_affected")

EVENT_COLUMNS = (("evidence_status", "TEXT"), ("evidence_status_reason", "TEXT"),
                 ("evidence_uncertain_fields", "TEXT"), ("evidence_status_at", "TEXT"))

Search = Callable[[str, str], Dict[str, Any]]


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def has_schema(conn: sqlite3.Connection) -> bool:
    """True when ``DeduplicatedEvents`` carries the evidence-status columns."""
    return {name for name, _ in EVENT_COLUMNS} <= _columns(conn, "DeduplicatedEvents")


NOT_ASSESSED = "not assessed"
NOT_ASSESSED_REASON = "evidence status not assessed yet: run dedup_admin.py recover-evidence"


def select_columns(conn: sqlite3.Connection, alias: str = "d") -> str:
    """SELECT terms ``evidence_status, evidence_status_reason, evidence_uncertain_fields``
    for exports and dashboards. An event not yet assessed (or a database without
    the columns) says so - it is never shown as supported."""
    if has_schema(conn):
        return (f"COALESCE({alias}.evidence_status, '{NOT_ASSESSED}') AS evidence_status, "
                f"COALESCE({alias}.evidence_status_reason, '{NOT_ASSESSED_REASON}') AS evidence_status_reason, "
                f"{alias}.evidence_uncertain_fields AS evidence_uncertain_fields")
    return (f"'{NOT_ASSESSED}' AS evidence_status, '{NOT_ASSESSED_REASON}' AS evidence_status_reason, "
            "NULL AS evidence_uncertain_fields")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Add the evidence-status columns and the tables the diagnosis reads. Idempotent."""
    from cyber_data_collector.processing import cyberdaily_refetch

    existing = _columns(conn, "DeduplicatedEvents")
    for name, ddl in EVENT_COLUMNS:
        if existing and name not in existing:
            conn.execute(f"ALTER TABLE DeduplicatedEvents ADD COLUMN {name} {ddl}")
    page_rescrape.ensure_schema(conn)
    cyberdaily_refetch.ensure_schema(conn)
    source_gate.ensure_replacement_schema(conn)
    conn.commit()


# --- diagnosis ----------------------------------------------------------------

@dataclass
class Route:
    state: str
    note: str = ""


@dataclass
class Member:
    """One active record of an incident and what its evidence lacks."""

    enriched_event_id: str
    raw_event_id: str
    source_type: str
    url: str
    chars: int
    origin: Optional[str]
    origin_url: Optional[str]
    page: str
    research: str
    research_attempts: int
    research_error: str
    page_route: Route
    research_route: Route
    research_data: Dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        page = {PAGE_OWN: f"own page {self.chars} chars",
                PAGE_ALTERNATE: f"text from another page ({self.origin_url or 'URL not recorded'})",
                PAGE_BLOCKED: "stored text is a block page",
                PAGE_SHORT: f"page text {self.chars} chars",
                PAGE_MISSING: "no page text",
                PAGE_OTHER_ARTICLE: f"stored page ({self.chars} chars) is another article"}[self.page]
        parts = [f"{self.source_type or 'unknown source'}: {page}"]
        if self.page_route.note:
            parts.append(self.page_route.note)
        parts.append(f"research {self.research_route.note or self.research}")
        return ", ".join(parts)


@dataclass
class EventEvidence:
    """An active incident's evidence, status and what retrieval has reached."""

    event_id: str
    title: str
    event_date: Optional[str]
    victim: Optional[str]
    description: str
    fields: Dict[str, Any]
    members: List[Member]
    status: str = UNSUPPORTED
    reason: str = ""
    uncertain_fields: List[str] = field(default_factory=list)
    label_route: Route = field(default_factory=lambda: Route(ROUTE_DONE))
    replacement_route: Route = field(default_factory=lambda: Route(ROUTE_DONE))
    stored_status: Optional[str] = None
    stored_reason: Optional[str] = None
    stored_fields: Optional[str] = None

    @property
    def routes(self) -> List[Route]:
        return ([m.page_route for m in self.members] + [m.research_route for m in self.members]
                + [self.label_route, self.replacement_route])

    @property
    def recovery(self) -> str:
        """For an unsupported incident: ``needed`` (something can still be asked for),
        ``pending`` (asked for, waiting) or ``exhausted`` (out of reach); else ``done``."""
        if self.status != UNSUPPORTED:
            return ROUTE_DONE
        states = {r.state for r in self.routes}
        for state in (ROUTE_NEEDED, ROUTE_PENDING):
            if state in states:
                return state
        return ROUTE_EXHAUSTED


def _json_dict(text: Optional[str]) -> Dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _page_state(content: Optional[str], metadata: Optional[str]) -> Tuple[str, int, Optional[str], Optional[str]]:
    from cyber_data_collector.utils.entity_scraper import looks_like_block_page

    text = (content or "").strip()
    origin, origin_url = content_origin(metadata)
    if not text:
        return PAGE_MISSING, 0, origin, origin_url
    if looks_like_block_page(text):
        return PAGE_BLOCKED, len(text), origin, origin_url
    if len(text) < MIN_PAGE_CHARS:
        return PAGE_SHORT, len(text), origin, origin_url
    if origin == ORIGIN_PERPLEXITY_ALTERNATIVE:
        return PAGE_ALTERNATE, len(text), origin, origin_url
    return PAGE_OWN, len(text), origin, origin_url


def _page_route(page: str, url: str, rescrape: Optional[Dict[str, Any]], refetch: Optional[Dict[str, Any]]) -> Route:
    """How far fetching the record's own page again has got."""
    from cyber_data_collector.datasources.cyber_daily import article_id
    from cyber_data_collector.utils.entity_scraper import is_blocked_domain

    if page == PAGE_OWN:
        return Route(ROUTE_DONE)
    if not url.strip():
        return Route(ROUTE_EXHAUSTED, "no URL to fetch")
    if is_blocked_domain(url):
        return Route(ROUTE_EXHAUSTED, "social, profile or marketplace URL: never fetched")
    if article_id(url) and "cyberdaily.au/" in url.lower():
        failures = int(refetch["failures"] or 0) if refetch is not None else 0
        # Out of reach exactly when the re-fetch has stopped fetching the story: gone from the
        # site (404/410) or given up at its own cap. The next routes - the record's research,
        # then a replacement search - take over from there.
        if refetch is not None and refetch.get("gave_up_at"):
            why = ("the story is gone from the site (HTTP 404/410)" if refetch.get("error_type") == "missing"
                   else f"Cyber Daily re-fetch given up after {failures} failure(s) "
                        f"({(refetch['last_error'] or '')[:80]})")
            return Route(ROUTE_EXHAUSTED, why)
        if refetch is not None:
            return Route(ROUTE_PENDING, f"Cyber Daily re-fetch {refetch['status']} at {str(refetch['fetched_at'])[:10]}"
                                        + (f", {failures} failure(s)" if failures else "")
                                        + (f", {refetch['error_type']}" if refetch.get("error_type") else ""))
        return Route(ROUTE_PENDING, "Cyber Daily re-fetch due (discovery phase, or `discovery_queue.py "
                                    "refetch-cyberdaily --until-empty`)")
    if rescrape is None:
        return Route(ROUTE_NEEDED, "re-fetch not yet asked for")
    status, attempts = rescrape["status"], int(rescrape["attempts"] or 0)
    if status == page_rescrape.STATUS_GAVE_UP:
        return Route(ROUTE_EXHAUSTED, f"re-fetch given up after {attempts} attempt(s) "
                                      f"({(rescrape['last_error'] or '')[:80]})")
    if status == page_rescrape.STATUS_STORED:
        # The text stored by the re-fetch has been replaced by something shorter since.
        return Route(ROUTE_EXHAUSTED, "re-fetched text was later replaced; not fetched again")
    return Route(ROUTE_PENDING, f"re-fetch {status}" + (f" (attempt {attempts} of {page_rescrape.MAX_ATTEMPTS} "
                                                        f"failed: {(rescrape['last_error'] or '')[:60]})"
                                                        if attempts else ""))


def _research_state(validated: Any, data: Dict[str, Any], status: Optional[str], attempts: int,
                    specific: Any) -> str:
    if validated in (1, "1", True) and data:
        return RESEARCH_VALIDATED
    if status == "quarantined":
        return RESEARCH_QUARANTINED
    if specific in (0, "0", False):
        return RESEARCH_NOT_WANTED
    if attempts >= MAX_ENRICHMENT_ATTEMPTS:
        return RESEARCH_EXHAUSTED
    return RESEARCH_RETRYING if attempts else RESEARCH_WAITING


def _research_route(state: str, attempts: int, error: str) -> Route:
    if state == RESEARCH_VALIDATED:
        return Route(ROUTE_DONE)
    if state == RESEARCH_WAITING:
        return Route(ROUTE_PENDING, "not attempted yet (queued for Perplexity enrichment)")
    if state == RESEARCH_RETRYING:
        return Route(ROUTE_PENDING, f"retrying, attempt {attempts} of {MAX_ENRICHMENT_ATTEMPTS} failed: {error[:80]}")
    if state == RESEARCH_NOT_WANTED:
        return Route(ROUTE_EXHAUSTED, "not researched (not a specific event)")
    return Route(ROUTE_EXHAUSTED, f"{state} after {attempts} attempt(s): {error[:80]}")


def _other_article_pages(conn: sqlite3.Connection) -> set:
    """Records whose stored page ``record_identity`` found to be another article, still open."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'RecordIdentityChecks'"
                        ).fetchone():
        return set()
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT raw_event_id FROM RecordIdentityChecks WHERE verdict = 'mismatch' "
        "AND kind = 'page_other_article' AND resolved_at IS NULL")}


def _members(conn: sqlite3.Connection, event_ids: Sequence[str]) -> Dict[str, List[Member]]:
    out: Dict[str, List[Member]] = {e: [] for e in event_ids}
    other_article = _other_article_pages(conn)
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        for chunk in range(0, len(event_ids), 500):
            ids = list(event_ids[chunk:chunk + 500])
            marks = ",".join("?" * len(ids))
            rows = conn.execute(
                f"""SELECT m.deduplicated_event_id AS event_id, e.enriched_event_id, e.raw_event_id,
                           r.source_type, COALESCE(r.source_url, '') AS url, r.raw_content, r.source_metadata,
                           e.perplexity_validated, e.perplexity_enrichment_data, e.perplexity_status,
                           COALESCE(e.perplexity_attempt_count, 0) AS attempts,
                           COALESCE(e.perplexity_last_error, '') AS last_error, e.is_specific_event,
                           p.status AS p_status, p.attempts AS p_attempts, p.last_error AS p_last_error,
                           c.status AS c_status, c.failures AS c_failures, c.last_error AS c_last_error,
                           c.fetched_at AS c_fetched_at, c.gave_up_at AS c_gave_up_at,
                           c.error_type AS c_error_type
                    FROM EventDeduplicationMap m
                    JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
                    LEFT JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
                    LEFT JOIN PageRescrapes p ON p.raw_event_id = e.raw_event_id
                    LEFT JOIN CyberDailyRefetches c ON c.raw_event_id = e.raw_event_id
                    WHERE m.deduplicated_event_id IN ({marks}) AND e.status = 'Active'
                    ORDER BY m.deduplicated_event_id, e.enriched_event_id""", ids).fetchall()
            for row in rows:
                page, chars, origin, origin_url = _page_state(row["raw_content"], row["source_metadata"])
                if page == PAGE_OWN and row["raw_event_id"] in other_article:
                    page = PAGE_OTHER_ARTICLE
                rescrape = ({"status": row["p_status"], "attempts": row["p_attempts"],
                             "last_error": row["p_last_error"]} if row["p_status"] else None)
                refetch = ({"status": row["c_status"], "failures": row["c_failures"],
                            "last_error": row["c_last_error"], "fetched_at": row["c_fetched_at"],
                            "gave_up_at": row["c_gave_up_at"], "error_type": row["c_error_type"]}
                           if row["c_status"] else None)
                data = _json_dict(row["perplexity_enrichment_data"])
                research = _research_state(row["perplexity_validated"], data, row["perplexity_status"],
                                           int(row["attempts"]), row["is_specific_event"])
                out[row["event_id"]].append(Member(
                    enriched_event_id=row["enriched_event_id"], raw_event_id=row["raw_event_id"],
                    source_type=row["source_type"] or "", url=row["url"], chars=chars, origin=origin,
                    origin_url=origin_url, page=page, research=research, research_attempts=int(row["attempts"]),
                    research_error=row["last_error"], research_data=data,
                    page_route=_page_route(page, row["url"], rescrape, refetch),
                    research_route=_research_route(research, int(row["attempts"]), row["last_error"])))
    finally:
        conn.row_factory = previous
    return out


def _labelled(conn: sqlite3.Connection, labels_path: Optional[Path]) -> Tuple[Dict[str, str], Dict[str, Dict]]:
    """``({current event id: cited URL}, {current event id: queued label-evidence record})``."""
    if labels_path is None or not Path(labels_path).exists():
        return {}, {}
    from cyber_data_collector.quality.regression import EventResolver

    resolver = EventResolver(conn)
    cited = {resolver.resolve(e)[0]: c["url"] for e, c in label_evidence.cited_reports(labels_path).items()}
    queued = {}
    for q in label_evidence.queued_reports(conn):
        queued[resolver.resolve(q["event"])[0]] = q
    return cited, queued


def _replacements(conn: sqlite3.Connection) -> Dict[str, sqlite3.Row]:
    """This module's replacement searches, by event."""
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT s.deduplicated_event_id, s.enriched_event_id, s.status, s.attempts, s.last_error, s.query,
                      s.raw_event_id, r.source_url, COALESCE(r.is_processed, 0) AS processed,
                      r.processing_error
               FROM SourceReplacements s LEFT JOIN RawEvents r ON r.raw_event_id = s.raw_event_id
               WHERE s.purpose = ?""", (PURPOSE,)).fetchall()
    finally:
        conn.row_factory = previous
    return {r["deduplicated_event_id"]: r for r in rows}


def _replacement_route(row: Optional[sqlite3.Row], members: List[Member], label: Route) -> Route:
    """A search costs a Perplexity call, so it waits until the records' own page,
    their research and any labelled report are all out of reach."""
    if row is None:
        reachable = [r for m in members for r in (m.page_route, m.research_route)
                     if r.state in (ROUTE_NEEDED, ROUTE_PENDING)]
        if reachable or label.state in (ROUTE_NEEDED, ROUTE_PENDING):
            return Route(ROUTE_DONE)   # not wanted yet: other evidence may still arrive
        return Route(ROUTE_NEEDED, "replacement search not yet made")
    if row["status"] == source_gate.STATUS_PENDING:
        return Route(ROUTE_PENDING, f"replacement search failed {row['attempts']} time(s), retried next run "
                                    f"({(row['last_error'] or '')[:60]})")
    if not row["raw_event_id"]:
        return Route(ROUTE_EXHAUSTED, "replacement search found no reporting")
    if row["source_url"] is None:
        return Route(ROUTE_EXHAUSTED, "replacement record was removed")
    if not row["processed"]:
        return Route(ROUTE_PENDING, f"replacement report queued ({row['source_url']})")
    if row["processing_error"]:
        return Route(ROUTE_EXHAUSTED, f"replacement report {row['source_url']} rejected "
                                      f"({str(row['processing_error'])[:60]})")
    return Route(ROUTE_PENDING, f"replacement report {row['source_url']} processed; joined to this incident "
                                "only if check-duplicates finds them one incident")


def _label_route(conn: sqlite3.Connection, event_id: str, cited: Dict[str, str], queued: Dict[str, Dict]) -> Route:
    if event_id not in cited:
        return Route(ROUTE_DONE)
    q = queued.get(event_id)
    if q is None:
        copy = label_evidence.enriched_copy(conn, cited[event_id])
        if copy:
            return Route(ROUTE_EXHAUSTED, f"labelled report {cited[event_id]} is already record {copy} of another "
                                          "event; join them with dedup_admin.py merge")
        return Route(ROUTE_NEEDED, f"labelled report {cited[event_id]} not yet queued")
    if not q["processed"]:
        return Route(ROUTE_PENDING, f"labelled report {q['url']} queued")
    return Route(ROUTE_PENDING, f"labelled report {q['url']} processed; attached by label-evidence")


def _judge(ev: EventEvidence) -> None:
    """Set the status, reason and uncertain fields from the members."""
    own = [m for m in ev.members if m.page == PAGE_OWN]
    alternate = [m for m in ev.members if m.page == PAGE_ALTERNATE]
    researched = [m for m in ev.members if m.research == RESEARCH_VALIDATED]
    lacking_page = "; ".join(m.describe() for m in ev.members if m.page != PAGE_OWN)
    lacking_research = "; ".join(f"{m.source_type or 'unknown source'}: {m.research_route.note or m.research}"
                                 for m in ev.members if m.research != RESEARCH_VALIDATED)
    if not ev.members:
        ev.status, ev.reason = UNSUPPORTED, "no active record"
    elif own and researched:
        ev.status, ev.reason = SUPPORTED, (f"{len(own)} record(s) with the page's own text, "
                                           f"{len(researched)} with validated research")
    elif own or researched or alternate:
        parts = []
        if not own:
            parts.append("no record holds its own page text"
                         + (f" (another page's text only: {', '.join(m.origin_url or '?' for m in alternate)})"
                            if alternate else "")
                         + f" - {lacking_page}")
        if not researched:
            parts.append(f"no validated research - {lacking_research}")
        ev.status, ev.reason = PARTIAL, "; ".join(parts)
    else:
        ev.status = UNSUPPORTED
        ev.reason = f"no page text and no validated research - {'; '.join(m.describe() for m in ev.members)}"
    ev.uncertain_fields = ([f for f in UNCERTAIN_FIELDS if ev.fields.get(f) not in (None, "")]
                           if ev.status == UNSUPPORTED else [])


def assess_events(conn: sqlite3.Connection, event_ids: Optional[Iterable[str]] = None,
                  labels_path: Optional[Path] = EVENT_LABELS_PATH) -> List[EventEvidence]:
    """Diagnose every active incident (or those given). Reads only."""
    where, params = incident_filter(alias="d")
    cols = _columns(conn, "DeduplicatedEvents")
    stored = ("d.evidence_status, d.evidence_status_reason, d.evidence_uncertain_fields" if has_schema(conn)
              else "NULL, NULL, NULL")
    wanted = [f for f in UNCERTAIN_FIELDS if f in cols]
    rows = conn.execute(
        f"""SELECT d.deduplicated_event_id, COALESCE(d.description, ''), {stored}, {', '.join('d.' + f for f in wanted)}
            FROM DeduplicatedEvents d WHERE {where}
            ORDER BY d.event_date DESC, d.deduplicated_event_id""", params).fetchall()
    rows = [tuple(r) for r in rows]
    if event_ids is not None:
        keep = set(event_ids)
        rows = [r for r in rows if r[0] in keep]
    members = _members(conn, [r[0] for r in rows])
    cited, queued = _labelled(conn, labels_path)
    replacements = _replacements(conn) if "purpose" in _columns(conn, "SourceReplacements") else {}
    out = []
    for row in rows:
        fields = dict(zip(wanted, row[5:]))
        ev = EventEvidence(event_id=row[0], title=fields.get("title") or "", event_date=fields.get("event_date"),
                           victim=fields.get("victim_organization_name"), description=row[1], fields=fields,
                           members=members.get(row[0], []), stored_status=row[2], stored_reason=row[3],
                           stored_fields=row[4])
        _judge(ev)
        if ev.status == UNSUPPORTED:
            ev.label_route = _label_route(conn, ev.event_id, cited, queued)
            ev.replacement_route = _replacement_route(replacements.get(ev.event_id), ev.members, ev.label_route)
            extra = [r.note for r in (ev.label_route, ev.replacement_route) if r.note]
            if extra:
                ev.reason += "; " + "; ".join(extra)
        out.append(ev)
    return out


# --- status -------------------------------------------------------------------

def mark_status(conn: sqlite3.Connection, assessments: Optional[List[EventEvidence]] = None,
                labels_path: Optional[Path] = EVENT_LABELS_PATH) -> Dict[str, int]:
    """Write each active incident's evidence status; returns counts per status plus ``changed``.

    Only rows whose status, reason or uncertain fields changed are written, and
    ``evidence_status_at`` moves only when the status itself changes."""
    ensure_schema(conn)
    assessments = assessments if assessments is not None else assess_events(conn, labels_path=labels_path)
    counts = {s: 0 for s in STATUSES}
    counts["changed"] = 0
    now = datetime.now().isoformat(timespec="seconds")
    for ev in assessments:
        counts[ev.status] += 1
        uncertain = json.dumps(ev.uncertain_fields)
        if (ev.status, ev.reason, uncertain) == (ev.stored_status, ev.stored_reason, ev.stored_fields):
            continue
        if ev.status != ev.stored_status:
            conn.execute("UPDATE DeduplicatedEvents SET evidence_status = ?, evidence_status_reason = ?, "
                         "evidence_uncertain_fields = ?, evidence_status_at = ? WHERE deduplicated_event_id = ?",
                         (ev.status, ev.reason, uncertain, now, ev.event_id))
        else:
            conn.execute("UPDATE DeduplicatedEvents SET evidence_status_reason = ?, evidence_uncertain_fields = ? "
                         "WHERE deduplicated_event_id = ?", (ev.reason, uncertain, ev.event_id))
        counts["changed"] += 1
    conn.commit()
    return counts


# --- retrieval ----------------------------------------------------------------

@dataclass
class RecoveryReport:
    unsupported: int = 0
    acted_on: List[str] = field(default_factory=list)
    pages_queued: List[str] = field(default_factory=list)
    labels_queued: List[Dict[str, Any]] = field(default_factory=list)
    searches: List[Dict[str, Any]] = field(default_factory=list)
    deferred: int = 0            # incidents with work left for a later run (the cap)
    errors: List[str] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)
    by_recovery: Dict[str, int] = field(default_factory=dict)

    def summary(self, dry_run: bool = False) -> str:
        verb = "would queue" if dry_run else "queued"
        lines = [f"{self.unsupported} incident(s) with no usable evidence; retrieval {verb} for "
                 f"{len(self.acted_on)} ({len(self.pages_queued)} page re-fetch(es), "
                 f"{len(self.labels_queued)} labelled report(s), {len(self.searches)} replacement search(es))"
                 + (f"; {self.deferred} left for the next run (cap)" if self.deferred else ""),
                 "Unsupported incidents by retrieval state: "
                 + ", ".join(f"{k} {v}" for k, v in sorted(self.by_recovery.items()))]
        if self.counts:
            lines.append("Evidence status: " + ", ".join(f"{s} {self.counts.get(s, 0)}" for s in STATUSES)
                         + f" ({self.counts.get('changed', 0)} changed)")
        for s in self.searches:
            lines.append(f"  search {s['event'][:8]} ({s['org']}): {s['outcome']}")
        lines += [f"  ERROR {e}" for e in self.errors]
        return "\n".join(lines)


def _parse_day(value: Optional[str]) -> Optional[date]:
    try:
        return datetime.strptime(str(value or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _organisation(ev: EventEvidence) -> str:
    for m in ev.members:
        name = str(m.research_data.get("formal_entity_name") or "").strip()
        if name:
            return name
    return (ev.victim or "").strip()


def _search_for(conn: sqlite3.Connection, ev: EventEvidence, search: Optional[Search],
                report: RecoveryReport, dry_run: bool) -> bool:
    """One replacement search for an incident whose own evidence is out of reach. True if acted on."""
    org = _organisation(ev)
    entry = {"event": ev.event_id, "org": org or "(none)"}
    if not org:
        entry["outcome"] = "no organisation to search for"
        report.searches.append(entry)
        return False
    when = _parse_day(ev.event_date)
    if source_gate._covered(conn, org, when, exclude_events=[ev.event_id]):
        entry["outcome"] = ("another active event about this organisation near this date holds evidence; "
                            "check-duplicates decides whether they are one incident")
        report.searches.append(entry)
        return False
    if dry_run:
        entry["outcome"] = "would search"
        report.searches.append(entry)
        return True
    summary = " ".join(x for x in (ev.title, ev.description[:500]) if x)
    query = source_gate.REPLACEMENT_PROMPT.format(org=org, date=when.isoformat() if when else "unknown",
                                                  summary=summary[:600])
    key = min(m.enriched_event_id for m in ev.members) if ev.members else ev.event_id
    raw_id = source_gate.search_and_queue(conn, key, query, org, when, search,
                                          metadata={"origin": ORIGIN, "evidence_for": ev.event_id},
                                          purpose=PURPOSE, event_id=ev.event_id)
    entry["outcome"] = f"queued {raw_id}" if raw_id else "searched; nothing found or search failed (pending)"
    report.searches.append(entry)
    return True


def _retry_pending_searches(conn: sqlite3.Connection, search: Optional[Search], report: RecoveryReport) -> None:
    """Re-run this module's failed searches with the query stored for them."""
    rows = conn.execute("SELECT enriched_event_id, deduplicated_event_id, query FROM SourceReplacements "
                        "WHERE purpose = ? AND status = ?", (PURPOSE, source_gate.STATUS_PENDING)).fetchall()
    for key, event_id, query in rows:
        row = conn.execute("SELECT event_date, victim_organization_name FROM DeduplicatedEvents "
                           "WHERE deduplicated_event_id = ?", (event_id,)).fetchone()
        org = (row[1] if row else None) or "(unknown)"
        raw_id = source_gate.search_and_queue(conn, key, query, org, _parse_day(row[0] if row else None), search,
                                              metadata={"origin": ORIGIN, "evidence_for": event_id},
                                              purpose=PURPOSE, event_id=event_id)
        report.searches.append({"event": event_id, "org": org,
                                "outcome": f"retried: queued {raw_id}" if raw_id else "retried: nothing queued"})


def recover(conn: sqlite3.Connection, limit: int = DEFAULT_LIMIT, dry_run: bool = False,
            search: Optional[Search] = None, labels_path: Optional[Path] = EVENT_LABELS_PATH) -> RecoveryReport:
    """Queue retrieval for incidents with no usable evidence, then mark every incident's status.

    At most ``limit`` incidents are given new work per run (re-fetches,
    labelled reports, searches); the rest wait for the next run. Waiting work
    (a re-fetch already queued, research the enrichment phase will retry) is
    not counted. Failed searches from earlier runs are retried first.

    A replacement search that cannot run (no ``PERPLEXITY_API_KEY``) is
    recorded in ``errors`` - the caller fails - and nothing else is affected.
    """
    ensure_schema(conn)
    report = RecoveryReport()
    if not dry_run:
        try:
            _retry_pending_searches(conn, search, report)
        except source_gate.ReplacementSearchUnavailable as exc:
            report.errors.append(str(exc))
    assessments = assess_events(conn, labels_path=labels_path)
    unsupported = [ev for ev in assessments if ev.status == UNSUPPORTED]
    report.unsupported = len(unsupported)
    label_events: List[str] = []
    search_blocked = False
    for ev in unsupported:
        if ev.recovery != ROUTE_NEEDED:
            continue
        if len(report.acted_on) >= limit:
            report.deferred += 1
            continue
        acted = False
        pages = [m.raw_event_id for m in ev.members if m.page_route.state == ROUTE_NEEDED]
        if pages:
            report.pages_queued += pages if dry_run else page_rescrape.queue_pages(conn, pages)
            acted = True
        if ev.label_route.state == ROUTE_NEEDED:
            label_events.append(ev.event_id)
            acted = True
        if ev.replacement_route.state == ROUTE_NEEDED and not search_blocked:
            try:
                acted = _search_for(conn, ev, search, report, dry_run) or acted
            except source_gate.ReplacementSearchUnavailable as exc:
                report.errors.append(str(exc))
                search_blocked = True
        if acted:
            report.acted_on.append(ev.event_id)
    if label_events:
        report.labels_queued = label_evidence.queue(conn, labels_path, dry_run=dry_run, include_events=label_events)
    if not dry_run:
        conn.commit()
        assessments = assess_events(conn, labels_path=labels_path)
        report.counts = mark_status(conn, assessments)
    for ev in assessments:
        if ev.status == UNSUPPORTED:
            report.by_recovery[ev.recovery] = report.by_recovery.get(ev.recovery, 0) + 1
    return report


def stored_counts(conn: sqlite3.Connection, period: Any = None) -> Dict[str, int]:
    """Stored evidence status of the active incidents (in ``period``), with ``not assessed``."""
    where, params = incident_filter(period, "d")
    counts = {s: 0 for s in (*STATUSES, NOT_ASSESSED)}
    if not has_schema(conn):
        counts[NOT_ASSESSED] = int(conn.execute(f"SELECT COUNT(*) FROM DeduplicatedEvents d WHERE {where}",
                                                params).fetchone()[0])
        return counts
    for status, n in conn.execute(f"SELECT d.evidence_status, COUNT(*) FROM DeduplicatedEvents d WHERE {where} "
                                  "GROUP BY 1", params).fetchall():
        counts[status if status in STATUSES else NOT_ASSESSED] += int(n)
    return counts


def describe_counts(counts: Dict[str, int]) -> str:
    """One sentence for a dashboard: how far stored evidence supports the incidents shown."""
    text = (f"Evidence: {counts.get(SUPPORTED, 0):,} supported, {counts.get(PARTIAL, 0):,} partial, "
            f"{counts.get(UNSUPPORTED, 0):,} unsupported (no page text and no validated research: "
            "their fields are uncertain until evidence arrives)")
    if counts.get(NOT_ASSESSED):
        text += f"; {counts[NOT_ASSESSED]:,} not assessed yet"
    return text + "."


def status_counts(assessments: Sequence[EventEvidence]) -> Dict[str, int]:
    counts = {s: 0 for s in STATUSES}
    for ev in assessments:
        counts[ev.status] += 1
    return counts


# --- drain ----------------------------------------------------------------------

#: Rounds of (queue, fetch, research) a drain makes at most.
DEFAULT_ROUNDS = 20
DRAINED = "nothing left to retrieve"
STALLED = "no progress in the last round"
THROTTLED = "throttled"
BUDGET = "API budget spent"
ROUNDS = "round limit"
BLOCKED = "blocked"


@dataclass
class Steps:
    """The retrieval work a drain runs between recovery rounds. Each is injectable (tests use fakes).

    ``refetch(conn, raw_ids)``  fetch these Cyber Daily stories again -> :class:`cyberdaily_refetch.DrainReport`
    ``rescrape(conn)``          fetch the queued non-Cyber Daily pages again -> number of pages attempted
    ``research(conn, limit)``   Perplexity research for the first ``limit`` waiting records -> number attempted
    ``search``                  the replacement search function (None: Perplexity)
    """

    refetch: Callable[[sqlite3.Connection, List[str]], Any]
    rescrape: Callable[[sqlite3.Connection], int]
    research: Callable[[sqlite3.Connection, int], int]
    search: Optional[Search] = None


@dataclass
class DrainRound:
    number: int
    queued: int = 0                 # incidents given new retrieval work this round
    stories: int = 0                # Cyber Daily stories fetched
    pages: int = 0                  # other pages fetched
    researched: int = 0             # records sent to Perplexity research
    searches: int = 0               # replacement searches made
    unsupported: int = 0            # unsupported incidents at the end of the round
    by_recovery: Dict[str, int] = field(default_factory=dict)

    @property
    def worked(self) -> bool:
        return bool(self.queued or self.stories or self.pages or self.researched or self.searches)


@dataclass
class DrainReport:
    rounds: List[DrainRound] = field(default_factory=list)
    ended: str = DRAINED
    detail: str = ""
    counts: Dict[str, int] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.ended == DRAINED

    def summary(self) -> str:
        lines = [f"Evidence recovery drain: {len(self.rounds)} round(s); ended: {self.ended}"
                 + (f" ({self.detail})" if self.detail else "")]
        for r in self.rounds:
            lines.append(f"  round {r.number}: {r.queued} incident(s) queued, {r.stories} Cyber Daily stor(ies) and "
                         f"{r.pages} page(s) fetched, {r.researched} record(s) researched, {r.searches} "
                         f"replacement search(es); {r.unsupported} unsupported "
                         f"({', '.join(f'{k} {v}' for k, v in sorted(r.by_recovery.items())) or 'none'})")
        if self.counts:
            lines.append("Evidence status: " + ", ".join(f"{s} {self.counts.get(s, 0)}" for s in STATUSES))
        lines += [f"  ERROR {e}" for e in self.errors]
        return "\n".join(lines)


def _is_cyberdaily(member: Member) -> bool:
    return "cyberdaily.au/" in member.url.lower()


def _runnable(assessments: Sequence[EventEvidence]) -> Dict[str, Any]:
    """What can be fetched or researched now for the unsupported incidents: stories, pages, research records."""
    stories: List[str] = []
    pages = 0
    research = 0
    for ev in assessments:
        if ev.status != UNSUPPORTED:
            continue
        for m in ev.members:
            if m.page_route.state == ROUTE_PENDING:
                if _is_cyberdaily(m):
                    stories.append(m.raw_event_id)
                else:
                    pages += 1
            if m.research_route.state == ROUTE_PENDING:
                research += 1
    return {"stories": stories, "pages": pages, "research": research}


def drain(conn: sqlite3.Connection, steps: Steps, limit: int = DEFAULT_LIMIT, max_rounds: int = DEFAULT_ROUNDS,
          labels_path: Optional[Path] = EVENT_LABELS_PATH) -> DrainReport:
    """Keep recovering evidence, round after round, until nothing more can be retrieved.

    A round queues new work for up to ``limit`` unsupported incidents
    (:func:`recover`: page re-fetches, labelled reports, a replacement search
    for an incident whose own page and research are both out of reach), then
    runs what is waiting: the Cyber Daily re-fetch for those incidents'
    stories, the page re-fetch queue, and Perplexity research for their
    records. When a route is given up (a 404/410 story, a page that failed its
    last attempt, research that used up its attempts) the incident falls
    through to the next route on the following round - the record's research,
    then one replacement search whose best citation becomes a record with its
    own URL - never to another page's text.

    It stops when nothing is left to retrieve, when a round makes no progress,
    when the site throttles (the Cyber Daily re-fetch ends instead of asking
    again), when a paid API's budget (``--api-budget``) is spent, or after
    ``max_rounds``. Everything is stored as it happens, so running it again
    resumes; the statuses are written at the end of every round.
    """
    from cyber_data_collector.utils.api_budget import budget

    report = DrainReport()
    previous: Optional[Tuple] = None
    for number in range(1, max_rounds + 1):
        recovered = recover(conn, limit=limit, search=steps.search, labels_path=labels_path)
        rnd = DrainRound(number, queued=len(recovered.acted_on),
                         searches=sum(1 for s in recovered.searches if "queued" in str(s.get("outcome", ""))
                                      and "would" not in str(s.get("outcome", ""))))
        report.rounds.append(rnd)
        if recovered.errors:
            report.ended, report.detail, report.errors = BLOCKED, "; ".join(recovered.errors), recovered.errors
            report.counts = recovered.counts
            break
        todo = _runnable(assess_events(conn, labels_path=labels_path))
        throttled = False
        if todo["stories"]:
            fetched = steps.refetch(conn, todo["stories"])
            rnd.stories = int(getattr(fetched, "fetched", 0))
            throttled = getattr(fetched, "ended", "") == "throttled"
        if todo["pages"] and not throttled:
            rnd.pages = int(steps.rescrape(conn) or 0)
        if todo["research"] and not budget.exhausted().get("perplexity"):
            rnd.researched = int(steps.research(conn, todo["research"]) or 0)
        assessments = assess_events(conn, labels_path=labels_path)
        report.counts = mark_status(conn, assessments)
        unsupported = [e for e in assessments if e.status == UNSUPPORTED]
        rnd.unsupported = len(unsupported)
        for ev in unsupported:
            rnd.by_recovery[ev.recovery] = rnd.by_recovery.get(ev.recovery, 0) + 1
        if throttled:
            report.ended, report.detail = THROTTLED, "the Cyber Daily site is throttling; run the command again later"
            break
        spent = budget.exhausted()
        if spent:
            report.ended = BUDGET
            report.detail = ", ".join(f"{p} refused {n}" for p, n in spent.items())
            break
        again = _runnable(assessments)
        if not unsupported or not (rnd.by_recovery.get(ROUTE_NEEDED) or again["stories"] or again["pages"]
                                   or again["research"]):
            report.ended = DRAINED
            report.detail = (f"{rnd.by_recovery.get(ROUTE_EXHAUSTED, 0)} exhausted, "
                             f"{rnd.by_recovery.get(ROUTE_PENDING, 0)} waiting on another step" if unsupported else "")
            break
        signature = (tuple(sorted(report.counts.items())), tuple(sorted(rnd.by_recovery.items())),
                     len(again["stories"]), again["pages"], again["research"])
        if not rnd.worked or signature == previous:
            report.ended = STALLED
            report.detail = "what is left needs a later run or another step"
            break
        previous = signature
    else:
        report.ended = ROUNDS
    return report
