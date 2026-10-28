"""Re-fetch Cyber Daily stories already stored, and keep the stored text current.

Cyber Daily articles reach the database through four sources (CyberDaily
itself, Webber Insurance, Google and Perplexity cite them). Each run fetches
the stored ones again with the Playwright scraper:

- the text is compared with the stored text after removing the site's
  free-article banner, whose count changes from visit to visit. Unchanged
  text writes nothing to ``RawEvents`` and queues nothing;
- changed text replaces the stored text, and the record's active enrichments
  are queued for Perplexity enrichment again (their stored facts are kept: a
  weaker new answer never overwrites a stronger one). Record fingerprints,
  ASD input fingerprints and Jev input hashes carry the change downstream;
- a page cut off by the exhausted free-article meter never replaces a
  complete stored article (it is recorded as ``kept_stored``);
- a fetch that fails, returns another page's text (the Perplexity fallback is
  off) or returns a block page is recorded as a failure and changes nothing.

Which stories: by default those published in the last ``DEFAULT_WINDOW_DAYS``
days (by publication date, else the record's date, else when it was
discovered), plus every stored story whose text is cut off
or too short to judge (*incomplete*), whatever its age. ``days=None``
re-fetches all of them. Records an active enrichment uses are re-fetched; so
is an incomplete record that discovery classified on the feed's standfirst
alone and filtered out (no enrichment, ``Filtered out during processing``):
when its full text arrives it is *reopened* (``is_processed = 0``) and the
next run's retry-queue drain classifies it again on that text. A record
rejected by a filter or a hygiene step is never re-fetched. A story stored or
re-fetched in the last ``MIN_AGE_HOURS`` hours is not fetched again: a
refresh has just scraped its new stories, and a second run the same day has
nothing to gain. At most ``DEFAULT_REPAIR_LIMIT`` incomplete stories are
fetched per run (stories an active enrichment uses first), so a large repair
backlog is worked off over several runs.

Every fetch is logged in ``CyberDailyRefetches`` (hash, length, outcome) with
the error type of a failure (:mod:`~cyber_data_collector.processing.fetch_errors`):

- ``missing`` (HTTP 404/410): the story is gone; it is never fetched again and
  is reported (:func:`unrecoverable`);
- ``throttled``, ``network``, ``incomplete`` (a fetch that still leaves the
  stored text cut off or short) and ``other``: retried on later runs after a
  back-off (1, 2, 4 ... days, capped at ``REFETCH_BACKOFF_CAP``), and given up
  after ``MAX_REFETCH_FAILURES`` consecutive failures (reported too).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from cyber_data_collector.datasources.cyber_daily import article_id
from cyber_data_collector.dedup.source_gate import MIN_PAGE_CHARS
from cyber_data_collector.processing.fetch_errors import INCOMPLETE, MISSING, error_type_from_text, is_permanent
from cyber_data_collector.processing.page_rescrape import ScraperUnavailable

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_DAYS = 90
#: Seconds between fetches: one page at a time, politely.
DEFAULT_DELAY_SECONDS = 2.0
#: A story stored or re-fetched more recently than this is left alone.
MIN_AGE_HOURS = 24
FETCH_TIMEOUT_SECONDS = 45
#: Incomplete stories fetched per run (None: no limit).
DEFAULT_REPAIR_LIMIT = 200
#: Consecutive failed (or still incomplete) fetches before a story is given up.
MAX_REFETCH_FAILURES = 5
REFETCH_BACKOFF_BASE = timedelta(days=1)
REFETCH_BACKOFF_CAP = timedelta(days=30)
#: How discovery marks a record its classifier did not keep.
FILTERED_OUT = "Filtered out during processing"

UNCHANGED = "unchanged"
UPDATED = "updated"
KEPT_STORED = "kept_stored"
FAILED = "failed"
MISSING_STATUS = "missing"

_BANNER = re.compile(r"^\s*You have\s*\d+\s*free articles? left this month\.?\s*$|"
                     r"^\s*Register for a free account to access unlimited free content\.?\s*$",
                     re.IGNORECASE | re.MULTILINE)
_EXHAUSTED_METER = re.compile(r"out of free articles|To continue reading the rest of this article", re.IGNORECASE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS CyberDailyRefetches (
    raw_event_id   TEXT PRIMARY KEY,
    url            TEXT NOT NULL,
    status         TEXT NOT NULL,     -- unchanged / updated / kept_stored / failed
    content_hash   TEXT,              -- of the fetched text, banner removed
    chars          INTEGER,
    last_error     TEXT,
    failures       INTEGER NOT NULL DEFAULT 0,  -- consecutive failed or still-incomplete fetches
    fetched_at     TEXT NOT NULL,
    error_type     TEXT,              -- fetch_errors type of the last failure; NULL once complete
    next_attempt_at TEXT,             -- not fetched again before this
    gave_up_at     TEXT               -- missing (404/410) or MAX_REFETCH_FAILURES reached
);
"""
_ADDED_COLUMNS = (("error_type", "TEXT"), ("next_attempt_at", "TEXT"), ("gave_up_at", "TEXT"))

#: ``fetch(url) -> (text, metadata)`` as ``PlaywrightScraper.get_page_text(..., return_metadata=True)``.
Fetch = Callable[[str], Awaitable[Tuple[Optional[str], Dict[str, Any]]]]


@dataclass
class RefetchReport:
    candidates: int = 0
    repair_candidates: int = 0             # of the candidates, stories whose stored text is incomplete
    repair_deferred: int = 0               # incomplete stories left for a later run by the repair limit
    outcomes: Dict[str, int] = field(default_factory=lambda: {UNCHANGED: 0, UPDATED: 0, KEPT_STORED: 0, FAILED: 0})
    updated: List[Dict[str, Any]] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)
    requeued_enrichments: int = 0
    reopened: int = 0                      # filtered-on-snippet records queued for classification again
    still_incomplete: int = 0              # fetched, but the stored text is still cut off or short
    missing: List[Dict[str, Any]] = field(default_factory=list)   # 404/410 this run: never fetched again
    gave_up: List[Dict[str, Any]] = field(default_factory=list)   # reached MAX_REFETCH_FAILURES this run


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    present = {row[1] for row in conn.execute("PRAGMA table_info(CyberDailyRefetches)")}
    for column, decl in _ADDED_COLUMNS:
        if column not in present:
            conn.execute(f"ALTER TABLE CyberDailyRefetches ADD COLUMN {column} {decl}")
    # Legacy failures (before error types): typed from their message, retried as usual.
    for raw_event_id, error in conn.execute(
            "SELECT raw_event_id, last_error FROM CyberDailyRefetches WHERE status = 'failed' "
            "AND error_type IS NULL").fetchall():
        error_type = error_type_from_text(error)
        conn.execute("UPDATE CyberDailyRefetches SET error_type = ?, status = ?, gave_up_at = ? "
                     "WHERE raw_event_id = ?",
                     (error_type, MISSING_STATUS if is_permanent(error_type) else FAILED,
                      datetime.now().isoformat() if is_permanent(error_type) else None, raw_event_id))
    conn.commit()


def refetch_backoff(failures: int) -> timedelta:
    """Delay before fetching a story again after ``failures`` consecutive failures."""
    return min(REFETCH_BACKOFF_CAP, REFETCH_BACKOFF_BASE * (2 ** max(0, failures - 1)))


def normalise(text: Optional[str]) -> str:
    """The text that decides whether a page changed: banner removed, whitespace collapsed."""
    return " ".join(_BANNER.sub("", text or "").split())


def content_hash(text: Optional[str]) -> str:
    return hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()


def is_cut_off(text: Optional[str]) -> bool:
    """True when the free-article meter stopped the page part-way through the article."""
    return bool(_EXHAUSTED_METER.search(text or ""))


def is_incomplete(text: Optional[str]) -> bool:
    """Cut off by the meter, or too short for the page gate to judge."""
    return is_cut_off(text) or len((text or "").strip()) < MIN_PAGE_CHARS


def enrichment_ids(conn: sqlite3.Connection) -> Tuple[set, set]:
    """``(raw ids with an active enrichment, raw ids with any enrichment)``, in one pass.

    (``EnrichedEvents.raw_event_id`` has no index; a correlated subquery per
    record scanned the table thousands of times.)
    """
    active, anything = set(), set()
    for raw_id, status in conn.execute("SELECT raw_event_id, status FROM EnrichedEvents"):
        anything.add(raw_id)
        if status == "Active":
            active.add(raw_id)
    return active, anything


def is_eligible(raw_id: str, is_processed: Any, processing_error: Optional[str], active: set,
                anything: set) -> bool:
    """A record a re-fetch may change: used by an active enrichment, or classified
    on the standfirst and filtered out at discovery (never a filter or hygiene rejection)."""
    if raw_id in active:
        return True
    return (bool(is_processed) and processing_error in (None, FILTERED_OUT) and raw_id not in anything)


def stored_stories(conn: sqlite3.Connection, days: Optional[int] = DEFAULT_WINDOW_DAYS,
                   now: Optional[datetime] = None, limit: Optional[int] = None,
                   repair_limit: Optional[int] = DEFAULT_REPAIR_LIMIT,
                   report: Optional[RefetchReport] = None) -> List[Dict[str, Any]]:
    """Stored Cyber Daily stories to re-fetch (see the module docstring).

    Recent stories an active enrichment uses come first, then incomplete ones
    (those an active enrichment uses first, newest first), at most
    ``repair_limit`` of them. Missing, given-up and backed-off stories are left out.
    """
    ensure_schema(conn)
    active, anything = enrichment_ids(conn)
    has_publication = "publication_date" in {row[1] for row in conn.execute("PRAGMA table_info(RawEvents)")}
    # The window is by publication: a story the backfill stored today from 2021 is not recent.
    published_expr = ("COALESCE(NULLIF(r.publication_date, ''), r.event_date)" if has_publication
                      else "r.event_date")
    rows = conn.execute(
        f"""SELECT r.raw_event_id, r.source_url, r.raw_content, CAST({published_expr} AS TEXT),
                  CAST(r.discovered_at AS TEXT), f.fetched_at, f.next_attempt_at, f.gave_up_at, r.is_processed,
                  r.processing_error
           FROM RawEvents r
           LEFT JOIN CyberDailyRefetches f ON f.raw_event_id = r.raw_event_id
           WHERE r.source_url LIKE '%cyberdaily.au/%'
           ORDER BY r.raw_event_id""").fetchall()
    now = now or datetime.now()
    cutoff = (now.date() - timedelta(days=days)).isoformat() if days is not None else None
    fresh = (now - timedelta(hours=MIN_AGE_HOURS)).isoformat(sep=" ")
    stamp = now.isoformat()
    recent_stories, repairs = [], []
    for raw_id, url, content, published, discovered, fetched, next_at, gave_up, processed, error in rows:
        if not is_eligible(raw_id, processed, error, active, anything):
            continue
        enriched = raw_id in active
        if not article_id(url) or gave_up:
            continue                                   # an index page; or gone / given up
        if next_at and next_at > stamp:
            continue                                   # backed off after a failure
        if max(str(discovered or "").replace("T", " "), str(fetched or "").replace("T", " ")) >= fresh:
            continue                                   # just scraped or re-fetched
        story = {"raw_event_id": raw_id, "url": url, "content": content or "", "enriched": bool(enriched),
                 "published": str(published or discovered or "")}
        recent = cutoff is None or str(published or discovered or "")[:10] >= cutoff
        if is_incomplete(content):
            repairs.append(story)
        elif recent and enriched:
            recent_stories.append(story)
    repairs.sort(key=lambda st: (not st["enriched"], _desc(st["published"])))
    chosen = repairs if repair_limit is None else repairs[:repair_limit]
    if report is not None:
        report.repair_candidates = len(chosen)
        report.repair_deferred = len(repairs) - len(chosen)
    stories = recent_stories + chosen
    return stories[:limit] if limit else stories


def _desc(value: str) -> str:
    """Sort key putting later ISO dates first."""
    return "".join(chr(0x10FFFF - ord(ch)) for ch in value)


def decide(stored: str, text: Optional[str], meta: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """``(outcome, reason)`` for a fetched page against the stored text."""
    from cyber_data_collector.utils.entity_scraper import ORIGIN_PERPLEXITY_ALTERNATIVE, looks_like_block_page

    if not text or not text.strip():
        return FAILED, str(meta.get("error") or "no text extracted")
    if meta.get("content_origin") == ORIGIN_PERPLEXITY_ALTERNATIVE:
        return FAILED, "text came from another page (Perplexity alternative); not stored as this page"
    if looks_like_block_page(text):
        return FAILED, f"block page, error page or cookie wall ({len(text.strip())} chars)"
    if len(text.strip()) < MIN_PAGE_CHARS:
        return FAILED, f"page text too short ({len(text.strip())} chars < {MIN_PAGE_CHARS})"
    if normalise(text) == normalise(stored):
        return UNCHANGED, None
    if is_cut_off(text) and not is_cut_off(stored) and len(stored.strip()) >= MIN_PAGE_CHARS:
        return KEPT_STORED, "fetched page is cut off by the free-article meter; stored article kept"
    return UPDATED, None


def _record(conn: sqlite3.Connection, story: Dict[str, Any], outcome: str, text: Optional[str],
            error: Optional[str], error_type: Optional[str], now: datetime) -> Optional[str]:
    """Log one fetch. Returns ``'missing'`` / ``'gave_up'`` when the story leaves the queue this time."""
    row = conn.execute("SELECT failures FROM CyberDailyRefetches WHERE raw_event_id = ?",
                       (story["raw_event_id"],)).fetchone()
    failures, next_at, gave_up, leaves = 0, None, None, None
    if error_type is not None:
        failures = (row[0] or 0) + 1 if row else 1
        if is_permanent(error_type):
            gave_up, leaves = now.isoformat(), "missing"
        elif failures >= MAX_REFETCH_FAILURES:
            gave_up, leaves = now.isoformat(), "gave_up"
        else:
            next_at = (now + refetch_backoff(failures)).isoformat()
    status = MISSING_STATUS if leaves == "missing" else outcome
    conn.execute(
        """INSERT INTO CyberDailyRefetches (raw_event_id, url, status, content_hash, chars, last_error, failures,
                                            fetched_at, error_type, next_attempt_at, gave_up_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(raw_event_id) DO UPDATE SET
               url = excluded.url, status = excluded.status, content_hash = excluded.content_hash,
               chars = excluded.chars, last_error = excluded.last_error, failures = excluded.failures,
               fetched_at = excluded.fetched_at, error_type = excluded.error_type,
               next_attempt_at = excluded.next_attempt_at, gave_up_at = excluded.gave_up_at""",
        (story["raw_event_id"], story["url"], status, content_hash(text) if text else None,
         len(text.strip()) if text else None, error, failures, now.isoformat(), error_type, next_at, gave_up))
    return leaves


def _reopen(conn: sqlite3.Connection, raw_event_id: str) -> bool:
    """Queue a filtered-on-snippet record for classification again (its full text has arrived)."""
    return conn.execute(
        f"""UPDATE RawEvents SET is_processed = 0, processing_error = NULL
            WHERE raw_event_id = ? AND COALESCE(is_processed, 0) != 0
              AND (processing_error IS NULL OR processing_error = '{FILTERED_OUT}')
              AND NOT EXISTS (SELECT 1 FROM EnrichedEvents e WHERE e.raw_event_id = RawEvents.raw_event_id)""",
        (raw_event_id,)).rowcount > 0


def _store_update(conn: sqlite3.Connection, raw_event_id: str, text: str, origin: Optional[str]) -> int:
    """Replace the stored text and queue the record's active enrichments again. Returns rows queued."""
    row = conn.execute("SELECT source_metadata FROM RawEvents WHERE raw_event_id = ?", (raw_event_id,)).fetchone()
    try:
        meta = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta.update({"content_origin": origin or "page", "content_source_url": None,
                 "content_refetched_at": datetime.now().isoformat()})
    conn.execute("UPDATE RawEvents SET raw_content = ?, source_metadata = ? WHERE raw_event_id = ?",
                 (text.strip(), json.dumps(meta), raw_event_id))
    return conn.execute(
        "UPDATE EnrichedEvents SET perplexity_validated = 0, perplexity_validated_at = NULL, "
        "perplexity_status = NULL, perplexity_attempt_count = 0, perplexity_last_error = NULL "
        "WHERE raw_event_id = ? AND status = 'Active'", (raw_event_id,)).rowcount


def apply_result(conn: sqlite3.Connection, story: Dict[str, Any], text: Optional[str], meta: Dict[str, Any],
                 report: RefetchReport, now: Optional[datetime] = None) -> str:
    """Decide, store and log one fetch; commits. Returns the outcome.

    A failed fetch is typed; one that succeeds but leaves the stored text
    incomplete is logged as an ``incomplete`` failure, so it is retried with
    back-off and given up at the cap rather than fetched on every run.
    """
    now = now or datetime.now()
    outcome, reason = decide(story["content"], text, meta)
    error_type: Optional[str] = None
    if outcome == UPDATED:
        report.requeued_enrichments += _store_update(conn, story["raw_event_id"], text,
                                                     meta.get("content_origin"))
        report.updated.append({"raw_event_id": story["raw_event_id"], "url": story["url"],
                               "old_chars": len(story["content"].strip()), "new_chars": len(text.strip())})
        if is_incomplete(story["content"]) and len(text.strip()) >= MIN_PAGE_CHARS and _reopen(
                conn, story["raw_event_id"]):
            report.reopened += 1
    elif outcome == FAILED:
        error_type = error_type_from_text(reason)
        report.failed.append({"raw_event_id": story["raw_event_id"], "url": story["url"], "error": reason,
                              "error_type": error_type})
    stored_now = text if outcome == UPDATED else story["content"]
    if outcome != FAILED and is_incomplete(stored_now):
        error_type, reason = INCOMPLETE, reason or (
            "stored text still cut off by the free-article meter" if is_cut_off(stored_now)
            else f"stored text still too short ({len(stored_now.strip())} chars < {MIN_PAGE_CHARS})")
        report.still_incomplete += 1
    elif outcome == FAILED and error_type == INCOMPLETE:
        report.still_incomplete += 1
    leaves = _record(conn, story, outcome, text if outcome != FAILED else None, reason, error_type, now)
    if leaves == "missing":
        report.missing.append({"raw_event_id": story["raw_event_id"], "url": story["url"], "error": reason})
    elif leaves == "gave_up":
        report.gave_up.append({"raw_event_id": story["raw_event_id"], "url": story["url"], "error": reason,
                               "error_type": error_type})
    conn.commit()
    report.outcomes[outcome] += 1
    return outcome


def unrecoverable(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Stories no longer fetched: gone from the site (404/410) or given up after repeated failures."""
    ensure_schema(conn)
    cur = conn.execute("""SELECT raw_event_id, url, status, error_type, last_error, failures, gave_up_at
                          FROM CyberDailyRefetches WHERE gave_up_at IS NOT NULL ORDER BY gave_up_at""")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def release(conn: sqlite3.Connection, include_missing: bool = False) -> int:
    """Put given-up stories back in the queue (and missing ones with ``include_missing``)."""
    ensure_schema(conn)
    sql = ("UPDATE CyberDailyRefetches SET gave_up_at = NULL, next_attempt_at = NULL, failures = 0, "
           "status = CASE WHEN status = 'missing' THEN 'failed' ELSE status END WHERE gave_up_at IS NOT NULL")
    if not include_missing:
        sql += " AND COALESCE(error_type, '') != 'missing'"
    count = conn.execute(sql).rowcount
    conn.commit()
    return count


async def _refetch_all(conn: sqlite3.Connection, stories: List[Dict[str, Any]], fetch: Optional[Fetch],
                       delay_seconds: float, report: RefetchReport) -> None:
    async def run(fetcher: Fetch) -> None:
        for index, story in enumerate(stories):
            if index and delay_seconds:
                await asyncio.sleep(delay_seconds)
            try:
                text, meta = await fetcher(story["url"])
            except Exception as exc:  # noqa: BLE001 - one page's failure is recorded, not fatal
                text, meta = None, {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            outcome = apply_result(conn, story, text, meta, report)
            logger.info("Cyber Daily re-fetch %s: %s", story["url"], outcome)

    if fetch is not None:
        await run(fetch)
        return
    from cyber_data_collector.utils.entity_scraper import PlaywrightScraper

    # No fallback: a page that cannot be fetched is a failure, not another page's text.
    scraper = PlaywrightScraper(allow_perplexity_fallback=False)
    try:
        await scraper.__aenter__()
    except Exception as exc:
        await scraper.close()
        raise ScraperUnavailable(
            f"the browser could not start ({type(exc).__name__}: {exc}); install it with "
            "`pip install -r requirements.txt` and `playwright install chromium`") from exc
    try:
        async def fetch_page(url: str):
            return await scraper.get_page_text(url, timeout=FETCH_TIMEOUT_SECONDS, return_metadata=True)

        await run(fetch_page)
    finally:
        await scraper.close()


async def refetch_stories_async(conn: sqlite3.Connection, days: Optional[int] = DEFAULT_WINDOW_DAYS,
                                limit: Optional[int] = None, dry_run: bool = False,
                                fetch: Optional[Fetch] = None,
                                delay_seconds: Optional[float] = None,
                                repair_limit: Optional[int] = DEFAULT_REPAIR_LIMIT) -> RefetchReport:
    """Re-fetch stored Cyber Daily stories (from inside a running event loop).

    Raises:
        ScraperUnavailable: the browser could not start; nothing was fetched.
    """
    report = RefetchReport()
    stories = stored_stories(conn, days=days, limit=limit, repair_limit=repair_limit, report=report)
    report.candidates = len(stories)
    if not dry_run and stories:
        await _refetch_all(conn, stories, fetch,
                           DEFAULT_DELAY_SECONDS if delay_seconds is None else delay_seconds, report)
    return report


def refetch_stories(conn: sqlite3.Connection, **kwargs: Any) -> RefetchReport:
    """Synchronous :func:`refetch_stories_async`."""
    return asyncio.run(refetch_stories_async(conn, **kwargs))
