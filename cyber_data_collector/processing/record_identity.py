"""Does a record's page, and the enrichment attached to it, describe the record's own article?

On 2026-10-29, 1,179 of 3,402 stored records behind active events failed
:func:`processing.active_evidence.page_identity` (Webber 457, Perplexity 409,
Google 220, OAIC 80, Cyber Daily 13). The evidence readers already withhold
such pages, but nothing said *why* they were wrong, and the pairing itself was
never repaired. Measured on a copy of the live database, the cause is not the
record's URL:

* **The enrichment belongs to another record** (about 840 of the 1,179). The
  raw record is whole - its title, URL and stored page are one article - but
  the ``EnrichedEvents`` row attached to it (title, description, date,
  Perplexity research, entities) describes another article, and in 827 of 840
  the enriched title is *exactly* the raw title of a different record. 1,088 of
  the 1,283 enrichments made in September 2025 (mostly 2025-09-28) are
  unrelated to the title of their record; the rate since 2026-03 is 1-2%. Raw
  records and enrichments were paired by position in an old run, and the
  enrichments came back in another order. Records are paired by id now; this
  module repairs what was stored and checks new pairings.
* **The page is not the record's article** (about 190): a Perplexity citation
  that is a site root or a breach-list page (cybertrackerau.com, borderlesscs),
  a Webber link whose page has a site name for a heading ("Gizmodo"), a Google
  match on a snippet.

:func:`assess` classifies every active record that way
(``enrichment_other_article`` / ``page_other_article``) and :func:`repair`
acts on it:

* an enrichment whose title is exactly one other record's title, and which that
  record's page confirms, is **re-attached** to that record
  (``EnrichedEvents.raw_event_id`` and ``EventDeduplicationMap.raw_event_id``;
  the enrichment itself is untouched, so no event is rebuilt). The previous
  record is remembered and the move can be reversed. A move that would leave
  one record with two enrichments, or send two enrichments to one record, is
  not made;
* a record left with no enrichment by a move, whose own page is its article,
  is reopened (``is_processed = 0``) so the discovery retry queue enriches it;
* a page that is not the record's article is queued for the **re-scrape**
  route (``PageRescrapes``) when its URL is an article, and for the
  **replacement search** (``source_gate.replace_evidence``: its citation
  becomes a new record with its own URL) when the URL is a site root or a hub
  page, when the re-fetch still gives another article, and for an enrichment
  that could not be re-attached.

Another page's text is never stored as a record's own. New records are
checked as they arrive: after scraping, a page that is not the record's
article is withheld from classification (``record_evidence``) and the
retrieval is queued; after enrichment, an enrichment that does not describe
its record is recorded here (``storage.cyber_event_data_v2``).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlparse

from cyber_data_collector.processing.active_evidence import (
    member_condition,
    page_identity,
    research_names,
    shares_distinctive_words,
)
from cyber_data_collector.utils.url_clean import scrub_stored_urls

logger = logging.getLogger(__name__)

KIND_PAGE = "page_other_article"
KIND_ENRICHMENT = "enrichment_other_article"

STAGE_INGEST = "ingest"
STAGE_AUDIT = "audit"

ACTION_REPOINTED = "repointed"
ACTION_REQUEUED = "requeued_for_enrichment"
ACTION_RESCRAPE = "rescrape_queued"
ACTION_RESCRAPED = "rescraped_still_mismatched"
ACTION_REPLACEMENT = "replacement_searched"
ACTION_COVERED = "covered_by_another_event"
ACTION_REVIEW = "needs_review"
ACTION_REVERSED = "reversed_by_hand"      # a person undid a re-attachment: never made again

#: A URL cited by more records than this is a list, roundup or hub, not an article (the duplicate checks' rule).
HUB_RECORDS = 3
#: New retrieval work (re-fetches queued, searches made, records reopened) per route per run.
DEFAULT_LIMIT = 100
#: Replacement calls that find the incident already covered (no search made) allowed per search.
CALLS_PER_SEARCH = 5
#: Rounds of re-attachment (a move can unblock the next); the live data settles in 3.
MAX_PASSES = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS RecordIdentityChecks (
    raw_event_id          TEXT NOT NULL,
    enriched_event_id     TEXT NOT NULL DEFAULT '',   -- '' = a check of the page alone (before enrichment)
    stage                 TEXT NOT NULL,              -- ingest / audit
    verdict               TEXT NOT NULL,              -- match / mismatch
    kind                  TEXT,                       -- page_other_article / enrichment_other_article
    reason                TEXT,
    owner_raw_event_id    TEXT,                       -- the record the enrichment belongs to, when found
    previous_raw_event_id TEXT,                       -- where a re-attached enrichment came from (to reverse it)
    action                TEXT,
    action_detail         TEXT,
    checked_at            TEXT NOT NULL,
    resolved_at           TEXT,
    PRIMARY KEY (raw_event_id, enriched_event_id)
);
CREATE INDEX IF NOT EXISTS idx_identity_open ON RecordIdentityChecks (verdict, resolved_at);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now().isoformat()


def _normal_title(title: Optional[str]) -> str:
    return " ".join((title or "").lower().split())


# ---------------------------------------------------------------------------
# The two questions
# ---------------------------------------------------------------------------

def page_matches_record(title: Optional[str], page_text: Optional[str],
                        names: Iterable[Optional[str]] = ()) -> Tuple[Optional[bool], str]:
    """Is the stored page the record's article? ``(True|False|None, reason)``; None = cannot tell.

    The rule is :func:`active_evidence.page_identity`, so ingest withholds
    what the evidence readers would withhold.
    """
    return page_identity(title, list(names), page_text)


def enrichment_matches_record(enriched_title: Optional[str], names: Iterable[Optional[str]],
                              raw_title: Optional[str], page_text: Optional[str],
                              carried_by_another: bool = False) -> Tuple[Optional[bool], str]:
    """Does the enrichment describe the record's own article? ``(True|False|None, reason)``.

    It does when its title shares a distinctive word with the record's title,
    or the record's page names the enrichment's organisation. It does not
    when neither holds *and* the page says so, or when another record carries
    exactly the enrichment's title (``carried_by_another``): the page rule
    alone is too generous - a Toyota article "names" Western Sydney University
    by its second word, "Sydney", in a menu. With no page to read and no
    shared word the answer is None.
    """
    if _normal_title(enriched_title) and _normal_title(enriched_title) == _normal_title(raw_title):
        return True, "the titles are the same"
    if shares_distinctive_words(enriched_title, raw_title):
        return True, "titles share a distinctive word"
    if carried_by_another:
        return False, ("titles share no distinctive word and another record's title is exactly this "
                       "enrichment's title")
    belongs, reason = page_identity(enriched_title, list(names), page_text)
    if belongs is True:
        return True, f"record's page: {reason}"
    if belongs is False:
        return False, f"titles share no distinctive word and the record's page does not belong to the enrichment ({reason})"
    return None, "titles share no distinctive word and there is no page to read"


def url_kind(conn: sqlite3.Connection, url: Optional[str]) -> str:
    """``site_root`` / ``hub`` (cited by more than HUB_RECORDS records) / ``article``."""
    parsed = urlparse(url or "")
    if not [segment for segment in parsed.path.split("/") if segment]:
        return "site_root"
    if conn.execute("SELECT COUNT(*) FROM RawEvents WHERE source_url = ?", (url,)).fetchone()[0] > HUB_RECORDS:
        return "hub"
    return "article"


# ---------------------------------------------------------------------------
# Stored verdicts
# ---------------------------------------------------------------------------

def _store(conn: sqlite3.Connection, raw_id: str, enriched_id: str, stage: str, verdict: str,
           kind: Optional[str], reason: str, *, owner: Optional[str] = None, previous: Optional[str] = None,
           action: Optional[str] = None, detail: Optional[str] = None, resolved: bool = False) -> None:
    """Write one verdict, keeping an earlier open action when the finding has not changed. Does not commit."""
    now = _now()
    old = conn.execute("SELECT kind, action, action_detail, previous_raw_event_id FROM RecordIdentityChecks "
                       "WHERE raw_event_id = ? AND enriched_event_id = ?", (raw_id, enriched_id)).fetchone()
    if old and old[0] == kind and verdict == "mismatch":
        action = action or old[1]
        detail = detail or old[2]
    previous = previous or (old[3] if old else None)
    conn.execute(
        """INSERT INTO RecordIdentityChecks (raw_event_id, enriched_event_id, stage, verdict, kind, reason,
                                              owner_raw_event_id, previous_raw_event_id, action, action_detail,
                                              checked_at, resolved_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (raw_event_id, enriched_event_id) DO UPDATE SET
               stage = excluded.stage, verdict = excluded.verdict, kind = excluded.kind, reason = excluded.reason,
               owner_raw_event_id = COALESCE(excluded.owner_raw_event_id, RecordIdentityChecks.owner_raw_event_id),
               previous_raw_event_id = excluded.previous_raw_event_id, action = excluded.action,
               action_detail = excluded.action_detail, checked_at = excluded.checked_at,
               resolved_at = excluded.resolved_at""",
        (raw_id, enriched_id, stage, verdict, kind, reason, owner, previous, action, detail, now,
         now if resolved else None))


def open_mismatches(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Findings not yet resolved (the table must exist: ``executescript`` in ``ensure_schema`` would commit)."""
    cursor = conn.execute("SELECT * FROM RecordIdentityChecks WHERE verdict = 'mismatch' AND resolved_at IS NULL")
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# At ingest
# ---------------------------------------------------------------------------

def check_ingest_page(db: Any, raw_event_id: str, title: Optional[str], page_text: Optional[str],
                      url: Optional[str]) -> Optional[str]:
    """Check a freshly scraped page against its record; returns the reason when it is another article, else None.

    A mismatch is recorded on the record and retrieval of the right page is
    queued: the re-scrape route for an article URL, and (left open for
    ``check-record-identity``, which holds the replacement search) for a site
    root or hub page. The caller withholds the page from classification.

    Args:
        db: A ``CyberEventDataV2`` (its connection and lock are used).
    """
    belongs, reason = page_matches_record(title, page_text)
    with db._lock:
        conn = db._conn
        ensure_schema(conn)
        if belongs is not False:
            if belongs is True:
                _resolve_page(conn, raw_event_id)
                conn.commit()
            return None
        action = detail = None
        if url_kind(conn, url) == "article":
            from cyber_data_collector.processing import page_rescrape
            page_rescrape.queue_pages(conn, [raw_event_id])
            action, detail = ACTION_RESCRAPE, "page queued for a re-fetch of the record's own URL"
        else:
            detail = "the URL is a site root or a page cited by several records; a replacement search is needed"
        _store(conn, raw_event_id, "", STAGE_INGEST, "mismatch", KIND_PAGE, reason, action=action, detail=detail)
        conn.commit()
    logger.warning("Identity: the page stored for %r (%s) is not its article (%s); withheld, retrieval queued",
                   (title or "")[:60], url, reason)
    return reason


def _resolve_page(conn: sqlite3.Connection, raw_event_id: str) -> None:
    conn.execute("UPDATE RecordIdentityChecks SET verdict = 'match', resolved_at = ?, checked_at = ? "
                 "WHERE raw_event_id = ? AND kind = ? AND resolved_at IS NULL",
                 (_now(), _now(), raw_event_id, KIND_PAGE))


def check_new_enrichment(conn: sqlite3.Connection, raw_event_id: str, enriched_event_id: str,
                         enriched_title: Optional[str]) -> Optional[str]:
    """Record an enrichment that does not describe its record. Returns the reason, or None. Does not commit.

    Called as the enrichment is stored. Records are paired by id, so this
    should never fire; it is the guard against the positional pairing that
    shifted 840 enrichments in 2025.
    """
    row = conn.execute("SELECT raw_title, raw_content FROM RawEvents WHERE raw_event_id = ?",
                       (raw_event_id,)).fetchone()
    if row is None:
        return None
    matches, reason = enrichment_matches_record(enriched_title, (), row[0], row[1])
    if matches is not False:
        return None
    ensure_schema(conn)
    _store(conn, raw_event_id, enriched_event_id, STAGE_INGEST, "mismatch", KIND_ENRICHMENT, reason,
           action=ACTION_REVIEW, detail="stored, not re-attached: run check-record-identity")
    logger.error("Identity: the enrichment %r stored for record %s does not describe it (%s)",
                 (enriched_title or "")[:60], raw_event_id, reason)
    return reason


# ---------------------------------------------------------------------------
# Auditing what is stored
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    raw_event_id: str
    enriched_event_id: str
    event_id: str
    source_type: str
    url: Optional[str]
    raw_title: str
    enriched_title: str
    kind: Optional[str]            # None = the pairing is sound
    reason: str
    owner_raw_event_id: Optional[str] = None   # the one record the enrichment belongs to, when found
    owners: int = 0                # how many records carry exactly the enrichment's title
    chars: int = 0
    created_at: Optional[str] = None           # when the enrichment was made


def _members(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    cond = member_condition(conn)
    cursor = conn.execute(
        f"""SELECT DISTINCT e.enriched_event_id, r.raw_event_id, r.source_type, r.source_url, e.title AS enriched_title,
                   r.raw_title, r.raw_content, e.perplexity_enrichment_data, d.victim_organization_name,
                   d.deduplicated_event_id AS event_id, e.created_at
            FROM DeduplicatedEvents d
            JOIN EventDeduplicationMap m ON m.deduplicated_event_id = d.deduplicated_event_id
            JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
            JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
            WHERE d.status = 'Active' {cond}""")
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _title_index(conn: sqlite3.Connection) -> Dict[str, List[Tuple[str, Optional[str]]]]:
    index: Dict[str, List[Tuple[str, Optional[str]]]] = defaultdict(list)
    for raw_id, title, content in conn.execute("SELECT raw_event_id, raw_title, raw_content FROM RawEvents"):
        key = _normal_title(title)
        if key:
            index[key].append((raw_id, content))
    return index


def assess(conn: sqlite3.Connection) -> List[Finding]:
    """Check every active record of every active event; one finding each (``kind`` None = sound)."""
    titles = _title_index(conn)
    findings: List[Finding] = []
    for r in _members(conn):
        names = [*research_names(r["perplexity_enrichment_data"]), r["victim_organization_name"]]
        page = r["raw_content"]
        finding = Finding(r["raw_event_id"], r["enriched_event_id"], r["event_id"], r["source_type"],
                          r["source_url"], r["raw_title"] or "", r["enriched_title"] or "", None, "sound",
                          chars=len((page or "").strip()), created_at=r["created_at"])
        owners = [(rid, content) for rid, content in titles.get(_normal_title(r["enriched_title"]), [])
                  if rid != r["raw_event_id"]]
        # An exact title is strong evidence; the owner's page can only veto it. Formal names ("Gold
        # Corporation") are often not in the article ("Perth Mint"), so the title's own words count too.
        owners = [(rid, content) for rid, content in owners
                  if page_identity(r["enriched_title"], [], content)[0] is not False
                  or page_identity(r["enriched_title"], names, content)[0] is not False]
        finding.owners = len(owners)
        enrichment_ok, why = enrichment_matches_record(r["enriched_title"], names, r["raw_title"], page,
                                                       carried_by_another=bool(owners))
        if enrichment_ok is False:
            finding.kind = KIND_ENRICHMENT
            finding.reason = why
            if len(owners) == 1:
                finding.owner_raw_event_id = owners[0][0]
        else:
            page_ok, page_reason = page_matches_record(r["raw_title"], page)
            if page_ok is False:
                finding.kind, finding.reason = KIND_PAGE, page_reason
        findings.append(finding)
    return findings


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------

@dataclass
class RepairReport:
    checked: int = 0
    sound: int = 0
    by_kind: Dict[str, int] = field(default_factory=dict)
    by_source: Dict[str, int] = field(default_factory=dict)
    repointed: int = 0
    not_repointed: Dict[str, int] = field(default_factory=dict)
    requeued: int = 0
    rescrape_queued: int = 0
    searches: int = 0
    review: int = 0
    waiting: int = 0          # retrieval held back by --limit until the next run
    urls_cleaned: int = 0     # stored values that carried tracking or identity parameters
    left_with_two: int = 0    # records now holding two active enrichments (one known not to describe it)
    resolved: int = 0
    errors: List[str] = field(default_factory=list)

    def summary(self, dry_run: bool = False) -> str:
        would = "would be " if dry_run else ""
        kinds = ", ".join(f"{n} {k}" for k, n in sorted(self.by_kind.items())) or "none"
        sources = ", ".join(f"{s} {n}" for s, n in sorted(self.by_source.items(), key=lambda kv: -kv[1])) or "none"
        blocked = ", ".join(f"{n} {k}" for k, n in sorted(self.not_repointed.items())) or "none"
        return (f"{self.checked} active record(s) checked: {self.sound} sound; mismatched: {kinds} (by source: "
                f"{sources}). {self.repointed} enrichment(s) {would}re-attached to their own record; not re-attached: "
                f"{blocked}. {self.requeued} record(s) {would}reopened for enrichment, {self.rescrape_queued} "
                f"page(s) {would}queued for re-fetch, {self.searches} replacement search(es) {would}made, "
                f"{self.review} left for review, {self.waiting} waiting for the next run (limit); "
                f"{self.resolved} earlier finding(s) resolved. {self.left_with_two} record(s) hold two active "
                f"enrichments, one of them known not to describe the record. {self.urls_cleaned} stored URL value(s) "
                f"{would}cleaned of tracking and identity parameters.")


def _plan_repoints(findings: List[Finding], active_by_raw: Dict[str, Set[str]],
                   held: Iterable[str] = (),
                   collides: Callable[[str, str], bool] = lambda eid, owner: False
                   ) -> Tuple[Dict[str, str], Dict[str, str]]:
    """``(moves, blocked)``: enrichment id -> record it moves to; enrichment id -> why it does not move.

    A move needs exactly one record carrying the enrichment's title and
    nobody else heading there (of several copies of one enrichment the
    earliest goes). The record may hold enrichments that are themselves
    known not to describe it - they stay, as wrong as before, and are
    reported - but never one that does: that would be a second, genuine
    enrichment.
    """
    candidates = {f.enriched_event_id: f for f in findings if f.kind == KIND_ENRICHMENT}
    held = set(held)
    moves: Dict[str, str] = {}
    blocked: Dict[str, str] = {}
    for eid, f in candidates.items():
        if eid in held:
            blocked[eid] = "re-attachment was undone by hand"
        elif f.owners == 0:
            blocked[eid] = "no stored record has this title"
        elif f.owners > 1:
            blocked[eid] = "several records have this title"
        elif collides(eid, f.owner_raw_event_id or ""):
            blocked[eid] = "its event already holds that record through another enrichment"
        else:
            moves[eid] = f.owner_raw_event_id or ""
    changed = True
    while changed:
        changed = False
        heading: Dict[str, List[str]] = defaultdict(list)
        for eid, owner in moves.items():
            heading[owner].append(eid)
        for owner, eids in heading.items():
            if len(eids) > 1:
                # Copies of one enrichment: the earliest made goes home, the rest stay and are reported.
                winner = min(eids, key=lambda e: (candidates[e].created_at or "", e))
                for eid in eids:
                    if eid != winner:
                        blocked[eid] = "a copy of an enrichment that is re-attached to its record"
                        del moves[eid]
                        changed = True
        for eid, owner in list(moves.items()):
            if any(other not in moves and other not in candidates
                   for other in active_by_raw.get(owner, set()) if other != eid):
                blocked[eid] = "that record already has an enrichment of its own"
                del moves[eid]
                changed = True
    return moves, blocked
    return moves, blocked


def _collides(conn: sqlite3.Connection, enriched_id: str, owner_raw_id: str) -> bool:
    """Would the move put the record into an event twice (``EventDeduplicationMap`` holds each record once per event)?"""
    return conn.execute(
        """SELECT 1 FROM EventDeduplicationMap o JOIN EventDeduplicationMap e
                  ON e.deduplicated_event_id = o.deduplicated_event_id
           WHERE o.raw_event_id = ? AND e.enriched_event_id = ? AND o.enriched_event_id != e.enriched_event_id
           LIMIT 1""", (owner_raw_id, enriched_id)).fetchone() is not None


def _active_by_raw(conn: sqlite3.Connection) -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = defaultdict(set)
    for eid, rid in conn.execute("SELECT enriched_event_id, raw_event_id FROM EnrichedEvents WHERE status = 'Active'"):
        out[rid].add(eid)
    return out


def repoint(conn: sqlite3.Connection, enriched_id: str, owner_raw_id: str) -> str:
    """Attach an enrichment to its own record; returns the record it left. Does not commit."""
    old = conn.execute("SELECT raw_event_id FROM EnrichedEvents WHERE enriched_event_id = ?",
                       (enriched_id,)).fetchone()[0]
    conn.execute("UPDATE EnrichedEvents SET raw_event_id = ?, updated_at = ? WHERE enriched_event_id = ?",
                 (owner_raw_id, _now(), enriched_id))
    conn.execute("UPDATE EventDeduplicationMap SET raw_event_id = ? WHERE enriched_event_id = ? AND raw_event_id = ?",
                 (owner_raw_id, enriched_id, old))
    return old


def reverse(conn: sqlite3.Connection, enriched_id: str) -> Optional[str]:
    """Undo a re-attachment; returns the record it went back to. Does not commit."""
    ensure_schema(conn)
    row = conn.execute("SELECT raw_event_id, previous_raw_event_id FROM RecordIdentityChecks "
                       "WHERE enriched_event_id = ? AND action = ? AND previous_raw_event_id IS NOT NULL",
                       (enriched_id, ACTION_REPOINTED)).fetchone()
    if row is None:
        return None
    repoint(conn, enriched_id, row[1])
    conn.execute("DELETE FROM RecordIdentityChecks WHERE raw_event_id = ? AND enriched_event_id = ?",
                 (row[0], enriched_id))
    _store(conn, row[1], enriched_id, STAGE_AUDIT, "mismatch", KIND_ENRICHMENT,
           "re-attachment undone by hand", action=ACTION_REVERSED, detail=f"was re-attached to {row[0]}")
    return row[1]


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def repair(conn: sqlite3.Connection, dry_run: bool = False, limit: int = DEFAULT_LIMIT,
           search: Optional[Callable[[str, str], Dict[str, Any]]] = None) -> RepairReport:
    """Audit every active record, re-attach enrichments to their own records, and queue retrieval for the rest.

    Re-attachment runs to a fixed point (a move can unblock the next), then
    the records and pages still wrong are sent to the retrieval queues.
    Idempotent: a second run finds the repaired pairings sound and queues
    nothing already queued. Commits unless ``dry_run``, which does all the
    work in a transaction (so its counts are what a run would do) and rolls it
    back, asking for no search.

    Args:
        limit: New retrieval work per route per run (page re-fetches queued,
            replacement searches made, records reopened for enrichment).
            Re-attachments are free and are not limited.
        search: Replacement search function, for tests; defaults to Perplexity.

    A replacement search needed without ``PERPLEXITY_API_KEY`` is reported in
    ``errors`` (the command exits 2); the other routes still run.
    """
    from cyber_data_collector.dedup import source_gate
    from cyber_data_collector.processing import page_rescrape

    ensure_schema(conn)
    page_rescrape.ensure_schema(conn)
    source_gate.ensure_replacement_schema(conn)
    conn.commit()
    report = RepairReport()
    # Newsletter links carry tracking tags and a subscriber token: cleaned wherever stored (counts only).
    report.urls_cleaned = scrub_stored_urls(conn, dry_run=dry_run).total
    # Each route has its own allowance, so reopening records cannot starve the searches.
    budget = {route: max(0, int(limit)) for route in ('requeue', 'rescrape', 'search')}
    held = {e for (_, e), action in _open_actions(conn).items() if action == ACTION_REVERSED}

    # 1. Re-attach enrichments to their own records, until nothing more can move.
    findings: List[Finding] = []
    blocked: Dict[str, str] = {}
    for pass_number in range(MAX_PASSES):
        findings = assess(conn)
        if pass_number == 0:
            report.checked = len(findings)
            report.sound = sum(1 for f in findings if f.kind is None)
            for f in findings:
                if f.kind:
                    report.by_kind[f.kind] = report.by_kind.get(f.kind, 0) + 1
                    report.by_source[f.source_type] = report.by_source.get(f.source_type, 0) + 1
        moves, blocked = _plan_repoints(findings, _active_by_raw(conn), held=held,
                                        collides=lambda eid, owner: _collides(conn, eid, owner))
        if not moves:
            break
        by_enriched = {f.enriched_event_id: f for f in findings}
        for eid, owner in moves.items():
            f = by_enriched[eid]
            repoint(conn, eid, owner)
            _store(conn, f.raw_event_id, eid, STAGE_AUDIT, "mismatch", KIND_ENRICHMENT, f.reason, owner=owner,
                   action=ACTION_REPOINTED, detail=f"re-attached to {owner}", resolved=True)
            _store(conn, owner, eid, STAGE_AUDIT, "match", None, "re-attached to the record carrying its title",
                   previous=f.raw_event_id, action=ACTION_REPOINTED, resolved=True)
        report.repointed += len(moves)
    for reason in blocked.values():
        report.not_repointed[reason] = report.not_repointed.get(reason, 0) + 1
    report.left_with_two = conn.execute(
        "SELECT COUNT(*) FROM (SELECT raw_event_id FROM EnrichedEvents WHERE status = 'Active' "
        "GROUP BY raw_event_id HAVING COUNT(*) > 1)").fetchone()[0]

    # 2. A record a move left without any enrichment (in this run or an earlier one that was held back by
    #    the limit), whose own page is its article, is enriched again.
    for raw_id in _orphans(conn):
        row = conn.execute("SELECT raw_title, raw_content FROM RawEvents WHERE raw_event_id = ?",
                           (raw_id,)).fetchone()
        if row is None or page_matches_record(row[0], row[1])[0] is not True:
            continue
        if budget['requeue'] <= 0:
            report.waiting += 1
            continue
        budget['requeue'] -= 1
        report.requeued += 1
        conn.execute("UPDATE RawEvents SET is_processed = 0, processing_error = NULL WHERE raw_event_id = ?",
                     (raw_id,))
        if _has_table(conn, "DiscoveryRecordState"):
            conn.execute("DELETE FROM DiscoveryRecordState WHERE raw_event_id = ?", (raw_id,))
        _store(conn, raw_id, "", STAGE_AUDIT, "mismatch", KIND_ENRICHMENT,
               "its enrichment belonged to another record and was re-attached to it",
               action=ACTION_REQUEUED, detail="reopened for the discovery retry queue", resolved=True)

    # 3. Everything still mismatched: retrieval through the existing queues, each asked for once.
    prior = _open_actions(conn)
    events_with_own_evidence = {f.event_id for f in findings if f.kind is None}
    rescrapes = dict(conn.execute("SELECT raw_event_id, status FROM PageRescrapes").fetchall())
    waiting_states = (page_rescrape.STATUS_QUEUED, page_rescrape.STATUS_RETRY)
    for f in (f for f in findings if f.kind):
        key = (f.raw_event_id, f.enriched_event_id if f.kind == KIND_ENRICHMENT else "")
        before = prior.get(key)
        action, detail = ACTION_REVIEW, blocked.get(f.enriched_event_id, "")
        if before in (ACTION_REPLACEMENT, ACTION_COVERED, ACTION_REVERSED):
            continue                                     # searched already (never repeated), or decided by hand
        if before == ACTION_RESCRAPE and rescrapes.get(f.raw_event_id) in waiting_states:
            continue                                     # waiting for rescrape-short-pages
        needs_search = (f.kind == KIND_ENRICHMENT or url_kind(conn, f.url) != "article"
                        or before == ACTION_RESCRAPED
                        or rescrapes.get(f.raw_event_id) in (page_rescrape.STATUS_STORED, page_rescrape.STATUS_GAVE_UP))
        route = 'search' if needs_search else 'rescrape'
        if needs_search and f.event_id in events_with_own_evidence:
            action = ACTION_COVERED                      # free: no allowance used
            detail = "no search: the event has another record whose page is its article"
        elif budget[route] <= 0:
            detail = "waiting for the next run (limit reached)"
            report.waiting += 1
        elif not needs_search:
            budget['rescrape'] -= 1
            report.rescrape_queued += 1
            action, detail = ACTION_RESCRAPE, "page queued for a re-fetch of the record's own URL"
            if not dry_run:         # (queue_pages creates its table with executescript, which commits)
                page_rescrape.queue_pages(conn, [f.raw_event_id])
        elif dry_run:
            budget['search'] -= 1
            report.searches += 1
            action, detail = ACTION_REPLACEMENT, "a replacement search would be made unless the incident is covered"
        else:
            searches_before = conn.execute("SELECT COUNT(*) FROM SourceReplacements").fetchone()[0]
            try:
                # The record's own event does not count as covering the incident: its evidence is what is missing.
                queued = source_gate.replace_evidence(conn, f.enriched_event_id, f.event_id, search=search,
                                                      exclude_events=[f.event_id])
            except source_gate.ReplacementSearchUnavailable as exc:
                report.errors.append(str(exc))
                detail = "replacement search unavailable (PERPLEXITY_API_KEY not set)"
            else:
                # A call that finds the incident covered costs nothing; only searches made use the allowance
                # (calls are capped too, so a run over many covered copies still ends).
                made = conn.execute("SELECT COUNT(*) FROM SourceReplacements").fetchone()[0] - searches_before
                report.searches += made
                budget['search'] -= made if made else 1 / CALLS_PER_SEARCH
                action = ACTION_REPLACEMENT if queued else ACTION_COVERED
                detail = (f"replacement record {queued} queued" if queued else
                          "no replacement queued: the incident is covered, was searched before, or nothing was found")
        report.review += action == ACTION_REVIEW
        _store(conn, key[0], key[1], STAGE_AUDIT, "mismatch", f.kind, f.reason, owner=f.owner_raw_event_id,
               action=action, detail=detail)

    report.resolved = _resolve_sound(conn, findings)
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return report


def _orphans(conn: sqlite3.Connection) -> List[str]:
    """Records an earlier re-attachment left with no active enrichment, not yet reopened."""
    return [r[0] for r in conn.execute(
        """SELECT DISTINCT c.raw_event_id FROM RecordIdentityChecks c
           WHERE c.action = ? AND c.kind = ? AND c.enriched_event_id != '' AND c.previous_raw_event_id IS NULL
             AND NOT EXISTS (SELECT 1 FROM EnrichedEvents e
                             WHERE e.raw_event_id = c.raw_event_id AND e.status = 'Active')
             AND NOT EXISTS (SELECT 1 FROM RecordIdentityChecks q
                             WHERE q.raw_event_id = c.raw_event_id AND q.enriched_event_id = '' AND q.action = ?)
           ORDER BY c.raw_event_id""", (ACTION_REPOINTED, KIND_ENRICHMENT, ACTION_REQUEUED))]


def _open_actions(conn: sqlite3.Connection) -> Dict[Tuple[str, str], Optional[str]]:
    return {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT raw_event_id, enriched_event_id, action FROM RecordIdentityChecks WHERE resolved_at IS NULL")}


def _resolve_sound(conn: sqlite3.Connection, findings: List[Finding]) -> int:
    """Close earlier open findings whose pairing is now sound. Returns how many."""
    sound = {(f.raw_event_id, f.enriched_event_id) for f in findings if f.kind is None}
    sound_raw = {raw for raw, _ in sound}
    closed = 0
    for row in open_mismatches(conn):
        is_sound = ((row["raw_event_id"], row["enriched_event_id"]) in sound if row["kind"] == KIND_ENRICHMENT
                    else row["raw_event_id"] in sound_raw)
        if is_sound:
            conn.execute("UPDATE RecordIdentityChecks SET verdict = 'match', resolved_at = ?, checked_at = ? "
                         "WHERE raw_event_id = ? AND enriched_event_id = ?",
                         (_now(), _now(), row["raw_event_id"], row["enriched_event_id"]))
            closed += 1
    return closed


def recheck_page(conn: sqlite3.Connection, raw_event_id: str) -> Optional[bool]:
    """After a re-fetch stored new text: is it the record's article now? Does not commit.

    A page that now belongs closes the finding; one that is still another
    article keeps it open as ``rescraped_still_mismatched``, which sends the
    record to the replacement search on the next ``check-record-identity``.
    """
    ensure_schema(conn)
    row = conn.execute("SELECT raw_title, raw_content FROM RawEvents WHERE raw_event_id = ?",
                       (raw_event_id,)).fetchone()
    if row is None or not conn.execute("SELECT 1 FROM RecordIdentityChecks WHERE raw_event_id = ? AND kind = ? "
                                       "AND resolved_at IS NULL", (raw_event_id, KIND_PAGE)).fetchone():
        return None
    belongs, reason = page_matches_record(row[0], row[1])
    if belongs is True or belongs is None:
        _resolve_page(conn, raw_event_id)
    else:
        conn.execute("UPDATE RecordIdentityChecks SET action = ?, action_detail = ?, reason = ?, checked_at = ? "
                     "WHERE raw_event_id = ? AND kind = ? AND resolved_at IS NULL",
                     (ACTION_RESCRAPED, "the record's own URL still gives another article", reason, _now(),
                      raw_event_id, KIND_PAGE))
    return belongs
