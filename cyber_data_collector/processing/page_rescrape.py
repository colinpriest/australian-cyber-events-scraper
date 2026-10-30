"""Re-fetch Google-sourced pages whose stored text is too short to judge.

The source gate (:mod:`cyber_data_collector.dedup.source_gate`) judges a
Google record from its page's own text and leaves alone any record with under
``MIN_PAGE_CHARS`` of it: the scrape failed, or caught only a headline, a
cookie wall or a PDF it could not read. On 2026-10-27, 158 active Google
records were like that, so junk among them could never be removed.

:func:`rescrape_short_pages` fetches each again with the Playwright scraper
(and its PDF extractor and second-browser-context retry) and stores the text
only when it is real (not a block page, error page or cookie wall), at least
``MIN_PAGE_CHARS`` long and longer than what is stored. The next
``check-sources`` then judges the record on it.

The Perplexity fallback is never used here: it returns *another* page about
the story, and storing that as the record's page would make the gate judge
the wrong page. A failed fetch is recorded as a failure.

Every attempt is recorded in ``PageRescrapes`` (attempt count, last error),
and a page is given up after ``MAX_ATTEMPTS`` failures, so dead pages are not
fetched on every run. Social, profile and marketplace URLs, which the scraper
never fetches, are given up at once.

Records from other sources are re-fetched when something asks for them:
:func:`queue_pages` stores a ``queued`` row (evidence recovery does this for
incidents with no usable evidence, ``processing/evidence_recovery.py``), and
from then on the record is a candidate like a short Google page.

A page that is not the record's article (``RecordIdentityChecks``, kind
``page_other_article``, action ``rescrape_queued``; ``processing/record_identity.py``)
is a candidate however long its text: it counts as having no text of its own,
so any real text the record's URL now gives replaces it, and is checked again.

When real text is stored for a record whose Perplexity research is not
validated, that research is queued again (attempts and quarantine reset): it
failed or was never tried on the snippet the record held, and the page is new
evidence. Validated research is left alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from cyber_data_collector.dedup.source_gate import MIN_PAGE_CHARS, SOURCE_TYPE

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
FETCH_TIMEOUT_SECONDS = 45
#: Hard limit on one page, retries included. Playwright's own timeouts do not
#: cover every call (a page whose context was destroyed mid-navigation hung the
#: 2026-10-29 run for six hours), so a page past this is a failed attempt.
PAGE_DEADLINE_SECONDS = 4 * FETCH_TIMEOUT_SECONDS
#: Closing the browser after a hang can hang too; past this it is abandoned.
CLOSE_DEADLINE_SECONDS = 60

STATUS_STORED = "stored"
STATUS_RETRY = "retry"
STATUS_GAVE_UP = "gave_up"
#: Asked for by another step and not tried yet (any source).
STATUS_QUEUED = "queued"

SCHEMA = """
CREATE TABLE IF NOT EXISTS PageRescrapes (
    raw_event_id     TEXT PRIMARY KEY,
    url              TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,  -- failed attempts; reset by nothing
    status           TEXT NOT NULL,               -- queued / stored / retry / gave_up
    last_error       TEXT,
    last_chars       INTEGER,                     -- length of the text fetched last time
    last_attempt_at  TEXT NOT NULL
);
"""

#: ``fetch(url) -> (text, metadata)`` as ``PlaywrightScraper.get_page_text(..., return_metadata=True)``.
Fetch = Callable[[str], Awaitable[Tuple[Optional[str], Dict[str, Any]]]]


class ScraperUnavailable(RuntimeError):
    """The browser could not start (Playwright or Chromium missing); nothing was fetched."""


@dataclass
class RescrapeReport:
    candidates: int = 0
    attempted: int = 0
    stored: List[Dict[str, Any]] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)
    gave_up: int = 0           # pages reaching MAX_ATTEMPTS (or never fetchable) this run
    requeued_research: int = 0  # unvalidated enrichments queued for Perplexity again


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def queue_pages(conn: sqlite3.Connection, raw_event_ids: Sequence[str]) -> List[str]:
    """Ask for these records' pages to be fetched again, whatever their source. Idempotent.

    A record that already has a ``PageRescrapes`` row (queued, retrying, stored
    or given up) is left as it is. Returns the records newly queued; does not
    commit.
    """
    ensure_schema(conn)
    queued = []
    for raw_id in raw_event_ids:
        row = conn.execute("SELECT source_url FROM RawEvents WHERE raw_event_id = ?", (raw_id,)).fetchone()
        # Only a web address is a page that can be fetched; a record with another kind of identifier
        # ("perplexity://research/...") is left to the other evidence routes.
        if row is None or not _is_web_url(row[0]):
            continue
        if conn.execute("SELECT 1 FROM PageRescrapes WHERE raw_event_id = ?", (raw_id,)).fetchone():
            continue
        conn.execute("INSERT INTO PageRescrapes (raw_event_id, url, attempts, status, last_attempt_at) "
                     "VALUES (?, ?, 0, ?, ?)", (raw_id, row[0], STATUS_QUEUED, datetime.now().isoformat()))
        queued.append(raw_id)
    return queued


def _is_web_url(url: Optional[str]) -> bool:
    return (url or "").strip().lower().startswith(("http://", "https://"))


def short_pages(conn: sqlite3.Connection, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Active records with too little stored text, not yet given up on: every
    Google record, and records of other sources :func:`queue_pages` asked for."""
    from cyber_data_collector.processing import record_identity

    ensure_schema(conn)
    record_identity.ensure_schema(conn)
    sql = f"""
        SELECT r.raw_event_id, r.source_url,
               CASE WHEN i.raw_event_id IS NOT NULL THEN 0
                    ELSE LENGTH(TRIM(COALESCE(r.raw_content, ''))) END AS chars,
               COALESCE(p.attempts, 0) AS attempts
        FROM RawEvents r
        LEFT JOIN PageRescrapes p ON p.raw_event_id = r.raw_event_id
        LEFT JOIN (SELECT DISTINCT raw_event_id FROM RecordIdentityChecks
                   WHERE kind = '{record_identity.KIND_PAGE}' AND resolved_at IS NULL
                     AND action = '{record_identity.ACTION_RESCRAPE}') i ON i.raw_event_id = r.raw_event_id
        WHERE (r.source_type = ? OR p.raw_event_id IS NOT NULL OR i.raw_event_id IS NOT NULL)
          AND EXISTS (SELECT 1 FROM EnrichedEvents e
                      WHERE e.raw_event_id = r.raw_event_id AND e.status = 'Active')
          AND (i.raw_event_id IS NOT NULL OR LENGTH(TRIM(COALESCE(r.raw_content, ''))) < ?)
          AND (r.source_url LIKE 'http://%' OR r.source_url LIKE 'https://%')
          AND COALESCE(p.status, '') != '{STATUS_GAVE_UP}'
        ORDER BY COALESCE(p.attempts, 0), r.raw_event_id
    """
    params: List[Any] = [SOURCE_TYPE, MIN_PAGE_CHARS]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [{"raw_event_id": r[0], "url": r[1], "chars": r[2], "attempts": r[3]}
            for r in conn.execute(sql, params)]


def assess(text: Optional[str], meta: Dict[str, Any], stored_chars: int) -> Optional[str]:
    """None if ``text`` should be stored, else why not."""
    from cyber_data_collector.utils.entity_scraper import ORIGIN_PERPLEXITY_ALTERNATIVE, looks_like_block_page

    if not text or not text.strip():
        return str(meta.get("error") or "no text extracted")
    if meta.get("content_origin") == ORIGIN_PERPLEXITY_ALTERNATIVE:
        return "text came from another page (Perplexity alternative); not stored as this page"
    chars = len(text.strip())
    if looks_like_block_page(text):
        return f"block page, error page or cookie wall ({chars} chars)"
    if chars < MIN_PAGE_CHARS:
        return f"too short ({chars} chars < {MIN_PAGE_CHARS})"
    if chars <= stored_chars:
        return f"not longer than the stored text ({chars} <= {stored_chars} chars)"
    return None


def _record_attempt(conn: sqlite3.Connection, page: Dict[str, Any], status: str,
                    error: Optional[str], chars: Optional[int], failed: bool) -> None:
    conn.execute(
        """INSERT INTO PageRescrapes (raw_event_id, url, attempts, status, last_error, last_chars, last_attempt_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(raw_event_id) DO UPDATE SET
               url = excluded.url, attempts = excluded.attempts, status = excluded.status,
               last_error = excluded.last_error, last_chars = excluded.last_chars,
               last_attempt_at = excluded.last_attempt_at""",
        (page["raw_event_id"], page["url"], page["attempts"] + (1 if failed else 0), status, error, chars,
         datetime.now().isoformat()))


def _store_text(conn: sqlite3.Connection, raw_event_id: str, text: str, origin: str) -> int:
    """Store the page text, and record in source_metadata that it is the page's own.

    Queues the record's unvalidated research again; returns how many enrichments were queued."""
    row = conn.execute("SELECT source_metadata FROM RawEvents WHERE raw_event_id = ?", (raw_event_id,)).fetchone()
    try:
        meta = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta.update({"content_origin": origin, "content_source_url": None,
                 "content_rescraped_at": datetime.now().isoformat()})
    conn.execute("UPDATE RawEvents SET raw_content = ?, source_metadata = ? WHERE raw_event_id = ?",
                 (text.strip(), json.dumps(meta), raw_event_id))
    return conn.execute(
        "UPDATE EnrichedEvents SET perplexity_status = NULL, perplexity_attempt_count = 0, "
        "perplexity_last_error = NULL WHERE raw_event_id = ? AND status = 'Active' "
        "AND COALESCE(perplexity_validated, 0) != 1", (raw_event_id,)).rowcount


def apply_result(conn: sqlite3.Connection, page: Dict[str, Any], text: Optional[str],
                 meta: Dict[str, Any], report: RescrapeReport) -> str:
    """Store or record one fetch; commits. Returns what happened, for the log."""
    from cyber_data_collector.utils.entity_scraper import BLOCKED_DOMAIN_ERROR, ORIGIN_PAGE

    report.attempted += 1
    problem = assess(text, meta, page["chars"])
    chars = len(text.strip()) if text else 0
    if problem is None:
        report.requeued_research += _store_text(conn, page["raw_event_id"], text,
                                                meta.get("content_origin") or ORIGIN_PAGE)
        # New text may be the record's article now, or still another one (then a replacement search follows).
        from cyber_data_collector.processing import record_identity
        record_identity.recheck_page(conn, page["raw_event_id"])
        _record_attempt(conn, page, STATUS_STORED, None, chars, failed=False)
        report.stored.append({**page, "new_chars": chars})
        outcome = f"stored {chars} chars (was {page['chars']})"
    else:
        attempts = page["attempts"] + 1
        give_up = attempts >= MAX_ATTEMPTS or problem == BLOCKED_DOMAIN_ERROR
        _record_attempt(conn, page, STATUS_GAVE_UP if give_up else STATUS_RETRY, problem, chars, failed=True)
        report.failed.append({**page, "error": problem, "attempts": attempts})
        report.gave_up += int(give_up)
        outcome = f"{problem} (attempt {attempts}{', giving up' if give_up else ''})"
    conn.commit()
    return outcome


async def _scrape_all(conn: sqlite3.Connection, pages: List[Dict[str, Any]], fetch: Optional[Fetch],
                      concurrency: int, report: RescrapeReport,
                      deadline: float = PAGE_DEADLINE_SECONDS) -> None:
    async def run(fetcher: Fetch) -> None:
        sem = asyncio.Semaphore(max(1, concurrency))

        async def one(page: Dict[str, Any]) -> None:
            async with sem:
                try:
                    text, meta = await asyncio.wait_for(fetcher(page["url"]), timeout=deadline)
                except asyncio.TimeoutError:
                    text, meta = None, {"error": f"no answer within {deadline:g}s (fetch abandoned)"}
                except Exception as exc:  # noqa: BLE001 - one page's failure is recorded, not fatal
                    text, meta = None, {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            # Single-threaded event loop: the connection is used from one thread.
            logger.info("Re-scrape %s: %s", page["url"], apply_result(conn, page, text, meta, report))

        await asyncio.gather(*(one(p) for p in pages))

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
        try:
            await asyncio.wait_for(scraper.close(), timeout=CLOSE_DEADLINE_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("Browser did not close within %ss; abandoned", CLOSE_DEADLINE_SECONDS)


def rescrape_short_pages(conn: sqlite3.Connection, limit: Optional[int] = None, dry_run: bool = False,
                         concurrency: int = 3, fetch: Optional[Fetch] = None,
                         deadline: float = PAGE_DEADLINE_SECONDS) -> RescrapeReport:
    """Re-fetch every short Google page not yet given up on.

    Raises:
        ScraperUnavailable: the browser could not start - a failure of the
            command, not of a page, so no page's attempt count changes.
    """
    pages = short_pages(conn, limit=limit)
    report = RescrapeReport(candidates=len(pages))
    if dry_run or not pages:
        return report
    asyncio.run(_scrape_all(conn, pages, fetch, concurrency, report, deadline))
    return report
