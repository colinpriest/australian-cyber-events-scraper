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

Which stories: by default those published or discovered in the last
``DEFAULT_WINDOW_DAYS`` days, plus every stored story whose text is cut off
or too short to judge, whatever its age. ``days=None`` re-fetches all of them.
Only records an active enrichment uses are re-fetched; a rejected record's
text decides nothing. A story stored or re-fetched in the last
``MIN_AGE_HOURS`` hours is not fetched again: a refresh has just scraped its
new stories, and a second run the same day has nothing to gain.

Every fetch is logged in ``CyberDailyRefetches`` (hash, length, outcome).
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
from cyber_data_collector.processing.page_rescrape import ScraperUnavailable

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_DAYS = 90
#: Seconds between fetches: one page at a time, politely.
DEFAULT_DELAY_SECONDS = 2.0
#: A story stored or re-fetched more recently than this is left alone.
MIN_AGE_HOURS = 24
FETCH_TIMEOUT_SECONDS = 45

UNCHANGED = "unchanged"
UPDATED = "updated"
KEPT_STORED = "kept_stored"
FAILED = "failed"

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
    failures       INTEGER NOT NULL DEFAULT 0,
    fetched_at     TEXT NOT NULL
);
"""

#: ``fetch(url) -> (text, metadata)`` as ``PlaywrightScraper.get_page_text(..., return_metadata=True)``.
Fetch = Callable[[str], Awaitable[Tuple[Optional[str], Dict[str, Any]]]]


@dataclass
class RefetchReport:
    candidates: int = 0
    outcomes: Dict[str, int] = field(default_factory=lambda: {UNCHANGED: 0, UPDATED: 0, KEPT_STORED: 0, FAILED: 0})
    updated: List[Dict[str, Any]] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)
    requeued_enrichments: int = 0


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def normalise(text: Optional[str]) -> str:
    """The text that decides whether a page changed: banner removed, whitespace collapsed."""
    return " ".join(_BANNER.sub("", text or "").split())


def content_hash(text: Optional[str]) -> str:
    return hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()


def is_cut_off(text: Optional[str]) -> bool:
    """True when the free-article meter stopped the page part-way through the article."""
    return bool(_EXHAUSTED_METER.search(text or ""))


def stored_stories(conn: sqlite3.Connection, days: Optional[int] = DEFAULT_WINDOW_DAYS,
                   now: Optional[datetime] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Stored Cyber Daily stories to re-fetch (see the module docstring)."""
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT r.raw_event_id, r.source_url, r.raw_content, r.event_date, CAST(r.discovered_at AS TEXT),
                  f.fetched_at
           FROM RawEvents r
           LEFT JOIN CyberDailyRefetches f ON f.raw_event_id = r.raw_event_id
           WHERE r.source_url LIKE '%cyberdaily.au/%'
             AND EXISTS (SELECT 1 FROM EnrichedEvents e
                         WHERE e.raw_event_id = r.raw_event_id AND e.status = 'Active')
           ORDER BY r.raw_event_id""").fetchall()
    now = now or datetime.now()
    cutoff = (now.date() - timedelta(days=days)).isoformat() if days is not None else None
    fresh = (now - timedelta(hours=MIN_AGE_HOURS)).isoformat(sep=" ")
    stories = []
    for raw_id, url, content, event_date, discovered, fetched in rows:
        if not article_id(url):
            continue                                   # a section or index page, not a story
        if max(str(discovered or "").replace("T", " "), str(fetched or "").replace("T", " ")) >= fresh:
            continue                                   # just scraped or re-fetched
        needs_repair = is_cut_off(content) or len((content or "").strip()) < MIN_PAGE_CHARS
        recent = cutoff is None or max(str(event_date or ""), str(discovered or ""))[:10] >= cutoff
        if needs_repair or recent:
            stories.append({"raw_event_id": raw_id, "url": url, "content": content or ""})
    return stories[:limit] if limit else stories


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
        return FAILED, f"too short ({len(text.strip())} chars < {MIN_PAGE_CHARS})"
    if normalise(text) == normalise(stored):
        return UNCHANGED, None
    if is_cut_off(text) and not is_cut_off(stored) and len(stored.strip()) >= MIN_PAGE_CHARS:
        return KEPT_STORED, "fetched page is cut off by the free-article meter; stored article kept"
    return UPDATED, None


def _record(conn: sqlite3.Connection, story: Dict[str, Any], outcome: str, text: Optional[str],
            error: Optional[str]) -> None:
    conn.execute(
        """INSERT INTO CyberDailyRefetches (raw_event_id, url, status, content_hash, chars, last_error, failures,
                                            fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(raw_event_id) DO UPDATE SET
               url = excluded.url, status = excluded.status, content_hash = excluded.content_hash,
               chars = excluded.chars, last_error = excluded.last_error,
               failures = CASE WHEN excluded.status = 'failed' THEN CyberDailyRefetches.failures + 1 ELSE 0 END,
               fetched_at = excluded.fetched_at""",
        (story["raw_event_id"], story["url"], outcome, content_hash(text) if text else None,
         len(text.strip()) if text else None, error, 1 if outcome == FAILED else 0, datetime.now().isoformat()))


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
                 report: RefetchReport) -> str:
    """Decide, store and log one fetch; commits. Returns the outcome."""
    outcome, reason = decide(story["content"], text, meta)
    if outcome == UPDATED:
        report.requeued_enrichments += _store_update(conn, story["raw_event_id"], text,
                                                     meta.get("content_origin"))
        report.updated.append({"raw_event_id": story["raw_event_id"], "url": story["url"],
                               "old_chars": len(story["content"].strip()), "new_chars": len(text.strip())})
    elif outcome == FAILED:
        report.failed.append({"raw_event_id": story["raw_event_id"], "url": story["url"], "error": reason})
    _record(conn, story, outcome, text if outcome != FAILED else None, reason)
    conn.commit()
    report.outcomes[outcome] += 1
    return outcome


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
                                delay_seconds: Optional[float] = None) -> RefetchReport:
    """Re-fetch stored Cyber Daily stories (from inside a running event loop).

    Raises:
        ScraperUnavailable: the browser could not start; nothing was fetched.
    """
    stories = stored_stories(conn, days=days, limit=limit)
    report = RefetchReport(candidates=len(stories))
    if not dry_run and stories:
        await _refetch_all(conn, stories, fetch,
                           DEFAULT_DELAY_SECONDS if delay_seconds is None else delay_seconds, report)
    return report


def refetch_stories(conn: sqlite3.Connection, **kwargs: Any) -> RefetchReport:
    """Synchronous :func:`refetch_stories_async`."""
    return asyncio.run(refetch_stories_async(conn, **kwargs))
