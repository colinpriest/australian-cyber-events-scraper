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
- ``throttled`` (HTTP 429/503): the *site* asked for a slower pace, which says
  nothing about the story. It is retried after ``THROTTLE_PAUSE`` and never
  counts towards giving the story up; ``STOP_AFTER_THROTTLED`` throttled
  answers in a row end the run (the rest stay queued);
- ``network``, ``incomplete`` (a fetch that still leaves the stored text cut
  off or short) and ``other``: retried on later runs after a back-off (1, 2,
  4 ... days, capped at ``REFETCH_BACKOFF_CAP``), and given up after
  ``MAX_REFETCH_FAILURES`` consecutive failures (reported too).

:func:`drain_async` / :func:`drain` repeat the run in batches until nothing is
left to fetch, a time budget is spent, or the site throttles (then it stops
rather than hammering it). Everything a run decides is stored in
``CyberDailyRefetches``, so a stopped drain resumes where it stopped.

A stored URL may carry a newsletter's tracking query (``utm_*`` and a personal
``token``). It is never sent or logged: stories are fetched, logged and
recorded by :func:`fetch_url` (scheme, host and path only).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from cyber_data_collector.datasources.cyber_daily import article_id
from cyber_data_collector.dedup.source_gate import MIN_PAGE_CHARS
from cyber_data_collector.processing.fetch_errors import (
    INCOMPLETE, MISSING, THROTTLED, error_type_from_text, is_permanent,
)
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
#: A throttled story is fetched again no sooner than this (the site, not the story, failed).
THROTTLE_PAUSE = timedelta(hours=2)
#: Throttled answers in a row that end a run; the remaining stories stay queued.
STOP_AFTER_THROTTLED = 3
#: Pages fetched at once by a drain (the scraper also limits requests per host).
DRAIN_CONCURRENCY = 2
#: Time a drain may run before it stops (stories left stay queued).
DEFAULT_DRAIN_MINUTES = 360
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
    throttled: int = 0                     # fetches the site answered 429/503
    stopped_on_throttle: bool = False      # the run ended early because the site kept throttling
    stopped_on_time: bool = False          # the run ended early at its time budget
    not_fetched: int = 0                   # candidates left unfetched by an early stop
    throttled_tail: int = 0                # throttled answers in a row at the end of the run (carried into the next)


def fetch_url(url: str) -> str:
    """``url`` without its query and fragment: what is fetched, logged and recorded.

    Newsletter links carry ``utm_*`` tracking and a personal ``token`` (a signed
    value naming the subscriber); neither is needed to read an article, and
    neither is sent to the site or written to a log.
    """
    parts = urlsplit((url or "").strip())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")) if parts.scheme else (url or "").strip()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    present = {row[1] for row in conn.execute("PRAGMA table_info(CyberDailyRefetches)")}
    for column, decl in _ADDED_COLUMNS:
        if column not in present:
            conn.execute(f"ALTER TABLE CyberDailyRefetches ADD COLUMN {column} {decl}")
    # Earlier runs recorded the URL with its tracking query (and a subscriber's token).
    for raw_event_id, url in conn.execute(
            "SELECT raw_event_id, url FROM CyberDailyRefetches WHERE url LIKE '%?%' OR url LIKE '%#%'").fetchall():
        conn.execute("UPDATE CyberDailyRefetches SET url = ? WHERE raw_event_id = ?", (fetch_url(url), raw_event_id))
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
                   report: Optional[RefetchReport] = None,
                   first: Optional[Iterable[str]] = None,
                   recent: bool = True, only: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    """Stored Cyber Daily stories to re-fetch (see the module docstring).

    Recent stories an active enrichment uses come first, then incomplete ones
    (``first`` - raw ids someone is waiting for, e.g. records of incidents with
    no evidence - then those an active enrichment uses, then newest first), at
    most ``repair_limit`` of them. Missing, given-up and backed-off stories are
    left out. ``recent=False`` leaves out the recent complete stories (a drain
    wants only the incomplete ones). ``only`` restricts the run to these raw ids.
    """
    ensure_schema(conn)
    active, anything = enrichment_ids(conn)
    has_publication = "publication_date" in {row[1] for row in conn.execute("PRAGMA table_info(RawEvents)")}
    # The window is by publication: a story the backfill stored today from 2021 is not recent.
    published_expr = ("COALESCE(NULLIF(r.publication_date, ''), r.event_date)" if has_publication
                      else "r.event_date")
    priority = set(first or ())
    restricted = set(only) if only is not None else None
    rows = conn.execute(
        f"""SELECT r.raw_event_id, r.source_url, r.raw_content, CAST({published_expr} AS TEXT),
                  CAST(r.discovered_at AS TEXT), f.fetched_at, f.next_attempt_at, f.gave_up_at, r.is_processed,
                  r.processing_error, f.error_type
           FROM RawEvents r
           LEFT JOIN CyberDailyRefetches f ON f.raw_event_id = r.raw_event_id
           WHERE r.source_url LIKE '%cyberdaily.au/%'
           ORDER BY r.raw_event_id""").fetchall()
    now = now or datetime.now()
    cutoff = (now.date() - timedelta(days=days)).isoformat() if days is not None else None
    fresh = (now - timedelta(hours=MIN_AGE_HOURS)).isoformat(sep=" ")
    stamp = now.isoformat()
    recent_stories, repairs = [], []
    for raw_id, url, content, published, discovered, fetched, next_at, gave_up, processed, error, last_type in rows:
        if restricted is not None and raw_id not in restricted:
            continue
        if not is_eligible(raw_id, processed, error, active, anything):
            continue
        enriched = raw_id in active
        if not article_id(url) or gave_up:
            continue                                   # an index page; or gone / given up
        if next_at and next_at > stamp:
            continue                                   # backed off after a failure
        # Just scraped or re-fetched. A throttled fetch told us about the site, not the
        # story: it waits for its own pause (next_attempt_at), not for 24 hours.
        last = str(fetched or "").replace("T", " ") if last_type != THROTTLED else ""
        if max(str(discovered or "").replace("T", " "), last) >= fresh:
            continue
        story = {"raw_event_id": raw_id, "url": fetch_url(url), "content": content or "", "enriched": bool(enriched),
                 "published": str(published or discovered or "")}
        is_recent = cutoff is None or str(published or discovered or "")[:10] >= cutoff
        if is_incomplete(content):
            repairs.append(story)
        elif recent and is_recent and enriched:
            recent_stories.append(story)
    repairs.sort(key=lambda st: (st["raw_event_id"] not in priority, not st["enriched"], _desc(st["published"])))
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
    if error_type == THROTTLED:
        # The site is slowing us down; the story is not at fault, so its failure count stands.
        failures = (row[0] or 0) if row else 0
        next_at = (now + THROTTLE_PAUSE).isoformat()
    elif error_type is not None:
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
        (story["raw_event_id"], fetch_url(story["url"]), status, content_hash(text) if text else None,
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
        report.throttled += error_type == THROTTLED
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


@dataclass
class _Pace:
    """Shared by the workers of one run: the delay between fetches and the throttle brake."""

    delay: float
    stop_after_throttled: int
    deadline: Optional[float] = None            # time.monotonic() value
    throttled_in_a_row: int = 0
    stopped: bool = False

    def current_delay(self) -> float:
        # Every throttled answer in a row doubles the pause (the site is asking for a slower pace).
        return min(120.0, self.delay * (2 ** self.throttled_in_a_row)) if self.delay else 0.0

    def after(self, outcome: str, error_type: Optional[str], report: RefetchReport) -> None:
        if outcome == FAILED and error_type == THROTTLED:
            self.throttled_in_a_row += 1
            if self.stop_after_throttled and self.throttled_in_a_row >= self.stop_after_throttled:
                self.stopped = True
                report.stopped_on_throttle = True
        else:
            self.throttled_in_a_row = 0

    def out_of_time(self, report: RefetchReport) -> bool:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.stopped = True
            report.stopped_on_time = True
        return self.stopped


async def _refetch_all(conn: sqlite3.Connection, stories: List[Dict[str, Any]], fetch: Optional[Fetch],
                       delay_seconds: float, report: RefetchReport, concurrency: int = 1,
                       stop_after_throttled: int = STOP_AFTER_THROTTLED,
                       deadline: Optional[float] = None, throttled_before: int = 0) -> None:
    pace = _Pace(delay_seconds, stop_after_throttled, deadline, throttled_in_a_row=throttled_before)
    waiting = list(stories)

    async def worker(fetcher: Fetch) -> None:
        fetched_any = False
        while waiting and not pace.out_of_time(report):
            story = waiting.pop(0)
            if fetched_any and pace.current_delay():
                await asyncio.sleep(pace.current_delay())
            fetched_any = True
            try:
                text, meta = await fetcher(fetch_url(story["url"]))
            except Exception as exc:  # noqa: BLE001 - one page's failure is recorded, not fatal
                text, meta = None, {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            before = len(report.failed)
            outcome = apply_result(conn, story, text, meta, report)
            error_type = report.failed[-1]["error_type"] if len(report.failed) > before else None
            pace.after(outcome, error_type, report)
            logger.info("Cyber Daily re-fetch %s: %s", fetch_url(story["url"]), outcome)

    async def run(fetcher: Fetch) -> None:
        await asyncio.gather(*(worker(fetcher) for _ in range(max(1, concurrency))))
        report.not_fetched = len(waiting)
        report.throttled_tail = pace.throttled_in_a_row

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
                                repair_limit: Optional[int] = DEFAULT_REPAIR_LIMIT,
                                concurrency: int = 1,
                                stop_after_throttled: int = STOP_AFTER_THROTTLED,
                                deadline: Optional[float] = None,
                                first: Optional[Iterable[str]] = None,
                                recent: bool = True,
                                only: Optional[Iterable[str]] = None,
                                throttled_before: int = 0) -> RefetchReport:
    """Re-fetch stored Cyber Daily stories (from inside a running event loop).

    Ends early (``stopped_on_throttle`` / ``stopped_on_time``; the unfetched
    stories stay queued) when the site throttles ``stop_after_throttled`` times
    in a row or ``deadline`` (a ``time.monotonic()`` value) passes.

    Raises:
        ScraperUnavailable: the browser could not start; nothing was fetched.
    """
    report = RefetchReport()
    stories = stored_stories(conn, days=days, limit=limit, repair_limit=repair_limit, report=report,
                             first=first, recent=recent, only=only)
    report.candidates = len(stories)
    if not dry_run and stories:
        await _refetch_all(conn, stories, fetch,
                           DEFAULT_DELAY_SECONDS if delay_seconds is None else delay_seconds, report,
                           concurrency=concurrency, stop_after_throttled=stop_after_throttled,
                           deadline=deadline, throttled_before=throttled_before)
    return report


def refetch_stories(conn: sqlite3.Connection, **kwargs: Any) -> RefetchReport:
    """Synchronous :func:`refetch_stories_async`."""
    return asyncio.run(refetch_stories_async(conn, **kwargs))


# --- drain ----------------------------------------------------------------------

#: Why a drain ended.
DRAINED = "drained"                  # nothing left to fetch
THROTTLED_STOP = "throttled"         # the site throttled; resume later
TIME_STOP = "time budget"            # --max-minutes spent; resume later
NO_PROGRESS = "no progress"          # a batch fetched nothing


@dataclass
class DrainReport:
    batches: int = 0
    fetched: int = 0
    outcomes: Dict[str, int] = field(default_factory=lambda: {UNCHANGED: 0, UPDATED: 0, KEPT_STORED: 0, FAILED: 0})
    throttled: int = 0
    reopened: int = 0
    requeued_enrichments: int = 0
    missing: int = 0
    gave_up: int = 0
    remaining: int = 0                   # incomplete stories still queued when it ended
    ended: str = DRAINED
    minutes: float = 0.0

    @property
    def complete(self) -> bool:
        return self.ended == DRAINED

    def add(self, report: RefetchReport) -> None:
        self.batches += 1
        self.fetched += sum(report.outcomes.values())
        for key, n in report.outcomes.items():
            self.outcomes[key] = self.outcomes.get(key, 0) + n
        self.throttled += report.throttled
        self.reopened += report.reopened
        self.requeued_enrichments += report.requeued_enrichments
        self.missing += len(report.missing)
        self.gave_up += len(report.gave_up)

    def summary(self) -> str:
        return (f"{self.batches} batch(es), {self.fetched} fetched in {self.minutes:.0f} min: "
                + ", ".join(f"{k} {v}" for k, v in self.outcomes.items())
                + f"; {self.throttled} throttled, {self.missing} missing, {self.gave_up} given up; "
                f"{self.requeued_enrichments} enrichment(s) queued again, {self.reopened} record(s) reopened; "
                f"ended: {self.ended}; {self.remaining} incomplete stor(ies) still queued")


def queue_state(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Every story a re-fetch may repair (eligible, with an article id, text incomplete) and where it stands.

    Each row: ``raw_event_id``, ``discovered_at``, ``attempted`` (has a
    ``CyberDailyRefetches`` row), ``error_type`` (of the last failure; None
    once it fetched), ``failures``, ``fetched_at``, ``gave_up_at``, ``status``.
    Back-off and the 24-hour rule are not applied: this is the queue, not what
    the next run will take.
    """
    ensure_schema(conn)
    active, anything = enrichment_ids(conn)
    out = []
    for (raw_id, url, content, discovered, processed, error, status, error_type, failures, fetched,
         gave_up) in conn.execute(
            """SELECT r.raw_event_id, r.source_url, r.raw_content, CAST(r.discovered_at AS TEXT), r.is_processed,
                      r.processing_error, f.status, f.error_type, f.failures, f.fetched_at, f.gave_up_at
               FROM RawEvents r LEFT JOIN CyberDailyRefetches f ON f.raw_event_id = r.raw_event_id
               WHERE r.source_url LIKE '%cyberdaily.au/%'""").fetchall():
        if not article_id(url) or not is_eligible(raw_id, processed, error, active, anything):
            continue
        if not is_incomplete(content) and status is None:
            continue
        out.append({"raw_event_id": raw_id, "discovered_at": discovered, "attempted": status is not None,
                    "status": status, "error_type": error_type, "failures": failures or 0,
                    "fetched_at": fetched, "gave_up_at": gave_up, "incomplete": is_incomplete(content)})
    return out


def waiting_count(conn: sqlite3.Connection) -> int:
    """Stories with incomplete text still in the queue (not given up, not missing), back-off ignored."""
    return sum(1 for row in queue_state(conn) if row["incomplete"] and not row["gave_up_at"])


async def drain_async(conn: sqlite3.Connection, days: Optional[int] = DEFAULT_WINDOW_DAYS,
                      batch_size: Optional[int] = DEFAULT_REPAIR_LIMIT,
                      max_minutes: Optional[float] = DEFAULT_DRAIN_MINUTES,
                      concurrency: int = DRAIN_CONCURRENCY, fetch: Optional[Fetch] = None,
                      delay_seconds: Optional[float] = None,
                      stop_after_throttled: int = STOP_AFTER_THROTTLED,
                      first: Optional[Iterable[str]] = None,
                      only: Optional[Iterable[str]] = None) -> DrainReport:
    """Fetch stories in batches of ``batch_size`` until none is left, ``max_minutes`` pass or the site throttles.

    Every fetch is stored as it happens (``CyberDailyRefetches``), so a drain
    that stops - throttled, out of time, interrupted - is resumed by running
    it again; nothing is fetched twice within 24 hours. A site that throttles
    ends the drain (``ended == 'throttled'``) instead of being asked again.
    Only stories whose text is incomplete are drained: re-checking recent
    complete stories for edits is the refresh's job.
    """
    report = DrainReport()
    started = time.monotonic()
    deadline = started + max_minutes * 60 if max_minutes else None
    priority = list(first or ())
    only = None if only is None else list(only)
    carried = 0                      # throttled answers in a row, across batches
    while True:
        batch = await refetch_stories_async(
            conn, days=days, repair_limit=batch_size, fetch=fetch, delay_seconds=delay_seconds,
            concurrency=concurrency, stop_after_throttled=stop_after_throttled, deadline=deadline,
            first=priority, recent=False, only=only, throttled_before=carried)
        carried = batch.throttled_tail
        if batch.candidates == 0:
            report.ended = DRAINED
            break
        report.add(batch)
        if batch.stopped_on_throttle:
            report.ended = THROTTLED_STOP
            break
        if batch.stopped_on_time:
            report.ended = TIME_STOP
            break
        if sum(batch.outcomes.values()) == 0:
            report.ended = NO_PROGRESS
            break
    report.minutes = (time.monotonic() - started) / 60
    report.remaining = waiting_count(conn)
    return report


def drain(conn: sqlite3.Connection, **kwargs: Any) -> DrainReport:
    """Synchronous :func:`drain_async`."""
    return asyncio.run(drain_async(conn, **kwargs))
