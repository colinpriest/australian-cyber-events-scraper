"""When each source was published - kept apart from when the incident happened.

A report's publication date is not the incident's date, and the enrichment
gate rightly refuses to pass one off as the other. But it is a fact worth
storing in its own right: the gap between an incident and its first report
is the reporting lag, which the recurrence analysis needs to model, and an
event whose incident date nobody states is still known to have happened by
the day it was first reported.

Until 2026-10-28 no Google or Perplexity record stored a publication date:
Google records took the *search window's* first day as their date (3,407
records, every one the 1st of a month) and records found outside a monthly
search took nothing, which left 129 active events undated and outside every
dashboard. So:

* ``RawEvents.publication_date`` / ``publication_date_source`` - the day the
  source was published and where that came from: the source itself (feed,
  listing, Google's page metadata) or the page's own metadata
  (``article:published_time``, JSON-LD ``datePublished``, ...). A page's
  *modified* date is never read as its publication date.
* :func:`backfill` fetches the page of every record still without one and
  reads its metadata; failures are recorded in ``PublicationDateFetches`` and
  retried, up to :data:`MAX_ATTEMPTS`.
* :func:`refresh_events` stores each active event's ``first_reported_date``
  (its members' earliest publication date) and ``reporting_lag_days`` (first
  report minus incident date, only where the incident date is not itself a
  publication or discovery date). An event with no incident date is dated by
  its first report, with ``date_basis`` saying so - the duplicate checks are
  shown that basis and know it may lag the incident.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

#: ``date_basis`` of an event dated by its first report (same wording as
#: :func:`cyber_data_collector.dedup.date_provenance.record_dates`).
BASIS_PUBLICATION = "publication date (may lag the incident)"
#: Bases whose date is a report or discovery date, not the incident's: no
#: reporting lag can be measured against them.
NOT_INCIDENT_BASES = ("publication date", "date the record was found", "no date")

SOURCE_FROM_SOURCE = "source"
SOURCE_PAGE_META = "page metadata"

MAX_ATTEMPTS = 3
STATUS_STORED = "stored"
STATUS_RETRY = "retry"
STATUS_NO_DATE = "no date on page"
STATUS_GAVE_UP = "gave up"
#: The URL is a listing (breach list, roundup, tracker) cited by records of
#: more than ``HUB_MAX_EVENTS`` events: the page's own date is not when any one
#: of them was reported. Webber's continuously updated data-breaches list
#: carries ``datePublished`` 2018-07-09 and gave that date to 110 records.
STATUS_LISTING = "listing page"

EARLIEST = date(1990, 1, 1)
FETCH_TIMEOUT_SECONDS = 20
MAX_HTML_BYTES = 3_000_000
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")

#: Meta tags naming a publication date, most specific first. Modified /
#: updated dates are deliberately absent.
META_KEYS = (
    "article:published_time", "og:article:published_time", "og:published_time", "article.published",
    "datepublished", "publishdate", "publish-date", "publish_date", "pubdate", "publication_date",
    "parsely-pub-date", "sailthru.date", "citation_publication_date", "citation_date",
    "dc.date.issued", "dcterms.issued", "dc.date", "dcterms.date", "dcterms.created", "date",
)
ARTICLE_TYPES = ("newsarticle", "article", "blogposting", "reportagenewsarticle", "report",
                 "pressrelease", "webpage", "analysisnewsarticle")

SCHEMA = """
CREATE TABLE IF NOT EXISTS PublicationDateFetches (
    raw_event_id TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    last_error TEXT,
    last_attempt_at TEXT NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    raw = {r[1] for r in conn.execute("PRAGMA table_info(RawEvents)")}
    for column in ("publication_date", "publication_date_source"):
        if raw and column not in raw:
            conn.execute(f"ALTER TABLE RawEvents ADD COLUMN {column} TEXT")
    dedup = {r[1] for r in conn.execute("PRAGMA table_info(DeduplicatedEvents)")}
    for column, ddl in (("first_reported_date", "TEXT"), ("reporting_lag_days", "INTEGER"),
                        ("date_basis", "TEXT"), ("date_precision", "TEXT")):
        if dedup and column not in dedup:
            conn.execute(f"ALTER TABLE DeduplicatedEvents ADD COLUMN {column} {ddl}")
    conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Reading a date
# ---------------------------------------------------------------------------

_ISO = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")


def normalise(value: Any, not_after: Optional[date] = None) -> Optional[str]:
    """``YYYY-MM-DD`` from an ISO, slashed or RFC 2822 date; None if unreadable or implausible.

    A date before 1990, or after ``not_after`` (the day the record was found -
    a page cannot be published after it was read), is not a publication date.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        day = value.date()
    elif isinstance(value, date):
        day = value
    else:
        text = str(value).strip()
        if not text:
            return None
        match = _ISO.search(text)
        day = None
        if match:
            try:
                day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                day = None
        if day is None:
            try:
                day = parsedate_to_datetime(text).date()
            except (TypeError, ValueError, IndexError):
                return None
    if day < EARLIEST or (not_after is not None and day > not_after):
        return None
    return day.isoformat()


def _not_after(discovered_at: Any) -> Optional[date]:
    found = normalise(discovered_at)
    return date.fromisoformat(found) if found else None


def from_metatags(tags: Iterable[Dict[str, Any]], not_after: Optional[date] = None) -> Optional[Tuple[str, str]]:
    """``(day, key)`` from meta-tag dictionaries (keys lower-cased), most specific key first."""
    merged: Dict[str, Any] = {}
    for tag in tags:
        for key, value in (tag or {}).items():
            merged.setdefault(str(key).strip().lower(), value)
    for key in META_KEYS:
        day = normalise(merged.get(key), not_after)
        if day:
            return day, key
    return None


def from_google_item(item: Dict[str, Any], not_after: Optional[date] = None) -> Optional[str]:
    """The publication date in a Google Custom Search result's ``pagemap.metatags``."""
    tags = ((item or {}).get("pagemap") or {}).get("metatags") or []
    found = from_metatags(tags, not_after)
    return found[0] if found else None


def _json_ld_dates(node: Any) -> Iterable[Tuple[bool, Any]]:
    """``(is_article, datePublished)`` for every node of a JSON-LD document."""
    if isinstance(node, list):
        for child in node:
            yield from _json_ld_dates(child)
    elif isinstance(node, dict):
        kinds = node.get("@type")
        kinds = kinds if isinstance(kinds, list) else [kinds]
        is_article = any(str(k).lower() in ARTICLE_TYPES for k in kinds if k)
        if node.get("datePublished"):
            yield is_article, node["datePublished"]
        for key in ("@graph", "mainEntity", "mainEntityOfPage"):
            if key in node:
                yield from _json_ld_dates(node[key])


def from_html(html: Optional[str], not_after: Optional[date] = None) -> Optional[Tuple[str, str]]:
    """``(day, how)`` - the page's own publication date, or None.

    Read, in order: JSON-LD ``datePublished`` on an article node; the meta tags
    in :data:`META_KEYS`; any other JSON-LD ``datePublished``; an element
    marked ``itemprop="datePublished"``; the first ``<time datetime>`` inside
    ``<article>``. A page that states none of these has no publication date
    here - nothing is inferred from its text.
    """
    if not html:
        return None
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html[:MAX_HTML_BYTES], "html.parser")
    ld_article, ld_other = [], []
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (TypeError, ValueError):
            continue
        for is_article, value in _json_ld_dates(data):
            (ld_article if is_article else ld_other).append(value)
    for value in ld_article:
        day = normalise(value, not_after)
        if day:
            return day, "JSON-LD datePublished"
    tags = []
    for meta in soup.find_all("meta"):
        key = meta.get("property") or meta.get("name") or meta.get("itemprop")
        if key and meta.get("content"):
            tags.append({key: meta["content"]})
    found = from_metatags(tags, not_after)
    if found:
        return found[0], f"meta {found[1]}"
    for value in ld_other:
        day = normalise(value, not_after)
        if day:
            return day, "JSON-LD datePublished"
    for element in soup.find_all(attrs={"itemprop": re.compile("^datePublished$", re.I)}):
        day = normalise(element.get("datetime") or element.get("content") or element.get_text(), not_after)
        if day:
            return day, "itemprop datePublished"
    article = soup.find("article")
    stamp = article.find("time") if article else None
    if stamp is not None:
        day = normalise(stamp.get("datetime"), not_after)
        if day:
            return day, "article <time>"
    return None


_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december")
_URL_YMD = re.compile(r"/((?:19|20)\d{2})/(\d{1,2})/(\d{1,2})(?:/|$|[-_])")
_URL_MONTH_DAY_YEAR = re.compile(r"\b(" + "|".join(_MONTHS) + r")-(\d{1,2})-((?:19|20)\d{2})\b", re.I)


def from_url(url: Optional[str], not_after: Optional[date] = None) -> Optional[str]:
    """A full date in the URL's path (``/2025/10/12/``, ``october-23-2025``), or None.

    Only a complete year-month-day counts; ``/2025/10/`` alone is not a date.
    Used after the page's metadata, never before it.
    """
    path = (url or "").split("?", 1)[0].split("#", 1)[0]
    match = _URL_YMD.search(path)
    if match:
        return normalise(f"{match.group(1)}-{match.group(2)}-{match.group(3)}", not_after)
    match = _URL_MONTH_DAY_YEAR.search(path)
    if match:
        month = _MONTHS.index(match.group(1).lower()) + 1
        return normalise(f"{match.group(3)}-{month}-{match.group(2)}", not_after)
    return None


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def store(conn: sqlite3.Connection, raw_event_id: str, day: Optional[str], how: str) -> bool:
    """Store a record's publication date unless it already has one; True if stored."""
    if not day:
        return False
    cur = conn.execute("UPDATE RawEvents SET publication_date = ?, publication_date_source = ? "
                       "WHERE raw_event_id = ? AND (publication_date IS NULL OR publication_date = '')",
                       (day, how, raw_event_id))
    return cur.rowcount > 0


def fill_from_metadata(conn: sqlite3.Connection) -> int:
    """Copy the publication date the source itself gave (``source_metadata``) into the column."""
    rows = conn.execute("SELECT raw_event_id, source_metadata, discovered_at FROM RawEvents "
                        "WHERE (publication_date IS NULL OR publication_date = '') "
                        "AND source_metadata LIKE '%publication_date%'").fetchall()
    filled = 0
    for raw_id, metadata, found in rows:
        try:
            value = (json.loads(metadata or "{}") or {}).get("publication_date")
        except (TypeError, ValueError, AttributeError):
            continue
        filled += store(conn, raw_id, normalise(value, _not_after(found)), SOURCE_FROM_SOURCE)
    return filled


def listing_urls(conn: sqlite3.Connection) -> Set[str]:
    """Source URLs whose records belong to more than ``HUB_MAX_EVENTS`` active events.

    The rule the duplicate candidates use for hub URLs (``dedup/candidates.py``):
    a page cited for several incidents is a listing, so a date read from the
    page or its URL is the listing's, not a report of any one incident.
    """
    from cyber_data_collector.dedup.candidates import HUB_MAX_EVENTS

    rows = conn.execute("""
        SELECT r.source_url FROM RawEvents r
        JOIN EnrichedEvents e ON e.raw_event_id = r.raw_event_id
        JOIN EventDeduplicationMap m ON m.enriched_event_id = e.enriched_event_id
        JOIN DeduplicatedEvents d ON d.deduplicated_event_id = m.deduplicated_event_id
        WHERE d.status = 'Active' AND r.source_url LIKE 'http%'
        GROUP BY r.source_url HAVING COUNT(DISTINCT d.deduplicated_event_id) > ?""",
        (HUB_MAX_EVENTS,)).fetchall()
    return {r[0] for r in rows}


def clear_listing_dates(conn: sqlite3.Connection, dry_run: bool = False) -> int:
    """Remove page-level publication dates from records whose URL is a listing.

    Only dates read from the page (metadata) or its URL path are removed; a
    date the source itself gave for the item (feed, search result) is kept.
    Listing records are marked :data:`STATUS_LISTING` so they are not fetched
    again. Idempotent; returns the number of records cleared.
    """
    ensure_schema(conn)
    urls = sorted(listing_urls(conn))
    if not urls:
        return 0
    marks = ",".join("?" * len(urls))
    rows = [r[0] for r in conn.execute(
        f"""SELECT raw_event_id FROM RawEvents WHERE source_url IN ({marks})
              AND publication_date IS NOT NULL AND publication_date != ''
              AND (publication_date_source LIKE ? OR publication_date_source = 'URL path')""",
        (*urls, f"{SOURCE_PAGE_META}%")).fetchall()]
    if dry_run:
        return len(rows)
    for raw_id in rows:
        conn.execute("UPDATE RawEvents SET publication_date = NULL, publication_date_source = NULL "
                     "WHERE raw_event_id = ?", (raw_id,))
    undated = [r[0] for r in conn.execute(
        f"""SELECT r.raw_event_id FROM RawEvents r
              LEFT JOIN PublicationDateFetches f ON f.raw_event_id = r.raw_event_id
              WHERE r.source_url IN ({marks}) AND (r.publication_date IS NULL OR r.publication_date = '')
                AND COALESCE(f.status, '') != ?""", (*urls, STATUS_LISTING)).fetchall()]
    for raw_id in undated:
        _record_attempt(conn, raw_id, 0, STATUS_LISTING, None)
    return len(rows)


def records_without_dates(conn: sqlite3.Connection, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Records of active events with a URL, no publication date, and not given up on."""
    sql = """
        SELECT DISTINCT r.raw_event_id, r.source_url, r.discovered_at, COALESCE(f.attempts, 0)
        FROM RawEvents r
        JOIN EnrichedEvents e ON e.raw_event_id = r.raw_event_id
        JOIN EventDeduplicationMap m ON m.enriched_event_id = e.enriched_event_id
        JOIN DeduplicatedEvents d ON d.deduplicated_event_id = m.deduplicated_event_id
        LEFT JOIN PublicationDateFetches f ON f.raw_event_id = r.raw_event_id
        WHERE d.status = 'Active'
          AND (r.publication_date IS NULL OR r.publication_date = '')
          AND r.source_url LIKE 'http%'
          AND COALESCE(f.status, '') NOT IN (?, ?, ?)
        ORDER BY r.raw_event_id"""
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, (STATUS_GAVE_UP, STATUS_NO_DATE, STATUS_LISTING)).fetchall()
    listings = listing_urls(conn)
    return [{"raw_event_id": r[0], "url": r[1], "discovered_at": r[2], "attempts": r[3]}
            for r in rows if r[1] not in listings]


Fetch = Callable[[str], Tuple[Optional[str], Optional[str]]]


def http_fetch(url: str) -> Tuple[Optional[str], Optional[str]]:
    """``(html, error)`` for one page, by plain HTTP."""
    import requests

    try:
        response = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*",
                                              "Accept-Language": "en-AU,en;q=0.9"},
                                timeout=FETCH_TIMEOUT_SECONDS, stream=True)
        if response.status_code >= 400:
            return None, f"HTTP {response.status_code}"
        kind = response.headers.get("Content-Type", "")
        if "html" not in kind.lower() and "xml" not in kind.lower():
            return None, f"not an HTML page ({kind or 'no content type'})"
        body = response.raw.read(MAX_HTML_BYTES, decode_content=True)
        return body.decode(response.encoding or "utf-8", errors="replace"), None
    except Exception as exc:  # noqa: BLE001 - one page's failure is recorded, not fatal
        return None, f"{type(exc).__name__}: {str(exc)[:200]}"


@dataclass
class BackfillReport:
    candidates: int = 0
    from_source: int = 0
    stored: int = 0
    no_date: int = 0
    failed: int = 0
    gave_up: int = 0
    listing_cleared: int = 0
    how: Dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        return (f"publication dates: {self.listing_cleared} listing-page date(s) removed; "
                f"{self.from_source} from the source, {self.stored} from the page "
                f"(metadata, else URL) of {self.candidates} page(s) fetched; {self.no_date} page(s) state none, "
                f"{self.failed} fetch(es) failed ({self.gave_up} given up)")


def _record_attempt(conn: sqlite3.Connection, raw_id: str, attempts: int, status: str,
                    error: Optional[str]) -> None:
    conn.execute(
        "INSERT INTO PublicationDateFetches (raw_event_id, attempts, status, last_error, last_attempt_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(raw_event_id) DO UPDATE SET attempts = excluded.attempts, "
        "status = excluded.status, last_error = excluded.last_error, last_attempt_at = excluded.last_attempt_at",
        (raw_id, attempts, status, error, datetime.now().isoformat(timespec="seconds")))


#: Failures worth a second fetch by a real browser: the site refused or broke
#: off a plain HTTP client. A missing page (404/410) is not retried.
BROWSER_RETRY = re.compile(r"HTTP 40[13]|HTTP 429|SSLError|ConnectionError|Timeout|RemoteDisconnected", re.I)
GONE = re.compile(r"HTTP 40[4]\b|HTTP 410\b")

BrowserFetch = Callable[[List[str]], Dict[str, Tuple[Optional[str], Optional[str]]]]


def browser_fetch_many(urls: List[str], concurrency: int = 3) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    """``{url: (html, error)}`` fetched with Playwright - the same pages, by a browser."""
    import asyncio

    async def run() -> Dict[str, Tuple[Optional[str], Optional[str]]]:
        from playwright.async_api import async_playwright

        out: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=USER_AGENT, locale="en-AU")
            sem = asyncio.Semaphore(max(1, concurrency))

            async def one(url: str) -> None:
                async with sem:
                    page = await context.new_page()
                    try:
                        response = await page.goto(url, timeout=FETCH_TIMEOUT_SECONDS * 1000,
                                                   wait_until="domcontentloaded")
                        if response is not None and response.status >= 400:
                            out[url] = (None, f"HTTP {response.status} (browser)")
                        else:
                            out[url] = (await page.content(), None)
                    except Exception as exc:  # noqa: BLE001 - recorded per page
                        out[url] = (None, f"browser: {type(exc).__name__}: {str(exc)[:160]}")
                    finally:
                        await page.close()

            try:
                await asyncio.gather(*(one(u) for u in urls))
            finally:
                await browser.close()
        return out

    return asyncio.run(run())


def backfill(conn: sqlite3.Connection, limit: Optional[int] = None, fetch: Optional[Fetch] = None,
             concurrency: int = 8, dry_run: bool = False, browser_fetch: Optional[BrowserFetch] = None,
             use_browser: bool = True) -> BackfillReport:
    """Give every record of an active event its publication date where it can be found.

    The source's own date first; otherwise the page is fetched by plain HTTP
    and its metadata read (:func:`from_html`); a page the site refused to a
    plain client (401/403/429, TLS or connection errors) is fetched again by a
    browser; failing both, a full date in the URL's path (:func:`from_url`).
    A page that loads but states no date is not fetched again; a missing page
    (404/410) is given up; any other failure is retried on later runs, up to
    :data:`MAX_ATTEMPTS`. Commits as it goes.
    """
    ensure_schema(conn)
    report = BackfillReport()
    report.listing_cleared = clear_listing_dates(conn, dry_run=dry_run)
    report.from_source = 0 if dry_run else fill_from_metadata(conn)
    conn.commit()
    pages = records_without_dates(conn, limit)
    report.candidates = len(pages)
    if dry_run or not pages:
        return report
    fetch = fetch or http_fetch
    urls = sorted({p["url"] for p in pages})
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        fetched = dict(zip(urls, pool.map(fetch, urls)))
    refused = [url for url, (html, error) in fetched.items()
               if html is None and error and BROWSER_RETRY.search(error)]
    if refused and use_browser:
        logger.info("Fetching %d page(s) refused to a plain client again with a browser", len(refused))
        for url, (html, error) in (browser_fetch or browser_fetch_many)(refused).items():
            fetched[url] = (html, None) if html is not None else (None, f"{fetched[url][1]}; {error}")
    for page in pages:   # stored on this thread: one connection, one writer
        html, error = fetched[page["url"]]
        not_after = _not_after(page["discovered_at"])
        found = from_html(html, not_after) if html else None
        how = f"{SOURCE_PAGE_META}: {found[1]}" if found else None
        if found is None and from_url(page["url"], not_after):
            found, how = (from_url(page["url"], not_after), "URL path"), "URL path"
        attempts = page["attempts"] + 1
        if found:
            store(conn, page["raw_event_id"], found[0], how)
            _record_attempt(conn, page["raw_event_id"], attempts, STATUS_STORED, None)
            report.stored += 1
            report.how[how] = report.how.get(how, 0) + 1
        elif html:
            _record_attempt(conn, page["raw_event_id"], attempts, STATUS_NO_DATE, None)
            report.no_date += 1
        else:
            give_up = attempts >= MAX_ATTEMPTS or bool(GONE.search(error or ""))
            _record_attempt(conn, page["raw_event_id"], attempts,
                            STATUS_GAVE_UP if give_up else STATUS_RETRY, error)
            report.failed += 1
            report.gave_up += int(give_up)
        conn.commit()
    return report


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@dataclass
class EventReport:
    first_reported: int = 0
    dated_by_first_report: List[str] = field(default_factory=list)
    redated_by_first_report: List[str] = field(default_factory=list)
    lags: int = 0
    #: Publication dates ignored because they precede the event's verified
    #: incident interval - a report cannot come before what it reports.
    before_incident: int = 0
    precision: Any = None   # incident_dates.PrecisionReport, after refresh_event_dates

    def summary(self) -> str:
        text = (f"{self.first_reported} event(s) with a first-report date; "
                f"{len(self.dated_by_first_report)} undated event(s) dated by their first report, "
                f"{len(self.redated_by_first_report)} re-dated to a changed first report; "
                f"{self.lags} reporting lag(s) measurable; {self.before_incident} publication date(s) "
                "ignored as earlier than the verified incident")
        return f"{text}; {self.precision.summary()}" if self.precision is not None else text


def refresh_events(conn: sqlite3.Connection) -> EventReport:
    """Store ``first_reported_date`` and ``reporting_lag_days`` on every active event,
    and date an undated event by its first report (basis :data:`BASIS_PUBLICATION`).

    Idempotent; changes only what differs. The caller commits.
    """
    from cyber_data_collector.dedup.date_provenance import precision

    ensure_schema(conn)
    report = EventReport()
    columns = {r[1] for r in conn.execute("PRAGMA table_info(DeduplicatedEvents)")}
    earliest_sql = "d.event_date_earliest" if "event_date_earliest" in columns else "NULL"
    published: Dict[str, List[str]] = {}
    for dedup_id, day in conn.execute("""
            SELECT m.deduplicated_event_id, r.publication_date
            FROM EventDeduplicationMap m
            JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
            JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
            WHERE r.publication_date IS NOT NULL AND r.publication_date != ''"""):
        published.setdefault(dedup_id, []).append(day)
    rows = conn.execute(f"""
        SELECT d.deduplicated_event_id, d.event_date, d.date_basis, d.first_reported_date,
               d.reporting_lag_days, {earliest_sql}
        FROM DeduplicatedEvents d WHERE d.status = 'Active'""").fetchall()
    for dedup_id, event_date, basis, old_first, old_lag, earliest in rows:
        days = sorted(published.get(dedup_id, []))
        if earliest:
            report.before_incident += sum(1 for day in days if day < earliest)
            days = [day for day in days if day >= earliest]
        first = days[0] if days else None
        event_day = normalise(event_date)
        if first:
            report.first_reported += 1
        if event_day is None and first:
            conn.execute("UPDATE DeduplicatedEvents SET event_date = ?, date_basis = ?, date_precision = ? "
                         "WHERE deduplicated_event_id = ?", (first, BASIS_PUBLICATION, precision(first), dedup_id))
            logger.info("Event %s had no date: dated %s by its first report", dedup_id[:8], first)
            report.dated_by_first_report.append(dedup_id)
            event_day, basis = first, BASIS_PUBLICATION
        elif first and basis == BASIS_PUBLICATION and event_day != first:
            # Dated by a report date that is no longer its first report (a
            # listing page's date removed, or an earlier report found).
            conn.execute("UPDATE DeduplicatedEvents SET event_date = ?, date_precision = ? "
                         "WHERE deduplicated_event_id = ?", (first, precision(first), dedup_id))
            logger.info("Event %s was dated %s by a report; re-dated %s by its first report",
                        dedup_id[:8], event_day, first)
            report.redated_by_first_report.append(dedup_id)
            event_day = first
        lag = None
        if first and event_day and basis and not str(basis).startswith(NOT_INCIDENT_BASES):
            lag = (date.fromisoformat(first) - date.fromisoformat(event_day)).days
            report.lags += 1
        if (old_first, old_lag) != (first, lag):
            conn.execute("UPDATE DeduplicatedEvents SET first_reported_date = ?, reporting_lag_days = ? "
                         "WHERE deduplicated_event_id = ?", (first, lag, dedup_id))
    return report


def refresh_event_dates(conn: sqlite3.Connection) -> EventReport:
    """Date undated events by their first report, refresh every event's date basis
    and precision, then store first-report dates and lags against those bases.

    Runs after every deduplication (a merge or split changes an event's earliest
    report) and as a hygiene step. The caller commits.
    """
    from cyber_data_collector.dedup.date_provenance import refresh_event_date_provenance
    from cyber_data_collector.processing.incident_dates import refresh_event_precision

    first = refresh_events(conn)
    refresh_event_date_provenance(conn)
    report = refresh_events(conn)
    report.dated_by_first_report = first.dated_by_first_report
    report.redated_by_first_report = first.redated_by_first_report + report.redated_by_first_report
    report.precision = refresh_event_precision(conn)
    return report
