"""Retrieve and cache the documents a Perplexity answer cites.

A cited URL is not evidence until its page has been read: the acceptance gate
used to treat any syntactically valid URL as support, so an answer citing an
unrelated page carried invented dates, organisation names and record counts
into the typed columns at high reliability.  The gate now checks each claim
against the *text* of the cited pages, and this module supplies that text.

Every fetch - successful or not - is cached in ``CitedDocuments`` so a page is
read once, a later re-check of the same answer (``--recompute-reliability``)
sees the same text, and nothing is fetched twice in one run.  A failed fetch
is cached with its status and retried only after ``retry_failed_after`` has
passed; a page that was read successfully is never re-fetched.

Fetching a public page is not a paid API call (no budgeted host is involved),
but it is bounded all the same: a connect/read timeout, a byte cap on the
download and a character cap on the stored text.
"""
from __future__ import annotations

import io
import logging
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

#: Seconds to connect / to wait between bytes.
FETCH_TIMEOUT: Tuple[float, float] = (5.0, 15.0)
#: Download cap per page; a longer body is truncated, not rejected.
MAX_FETCH_BYTES = 5_000_000
#: Stored text cap per page.
MAX_TEXT_CHARS = 200_000
#: Extracted text shorter than this is not a usable document.
MIN_DOCUMENT_CHARS = 100
#: PDF pages read at most.
MAX_PDF_PAGES = 40

STATUS_OK = "ok"
STATUS_HTTP_ERROR = "http_error"
STATUS_FETCH_ERROR = "fetch_error"
STATUS_UNSUPPORTED = "unsupported_type"
STATUS_EMPTY = "empty"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 (australian-cyber-events research; citation check)"
)

CITED_DOCUMENTS_DDL = """
CREATE TABLE IF NOT EXISTS CitedDocuments (
    url          TEXT PRIMARY KEY,
    fetched_at   TEXT NOT NULL,
    status       TEXT NOT NULL,
    http_status  INTEGER,
    content_type TEXT,
    final_url    TEXT,
    text         TEXT,
    error        TEXT,
    published_date TEXT   -- the page's own publication date, from its metadata
)
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create ``CitedDocuments`` if it is missing (idempotent)."""
    conn.execute(CITED_DOCUMENTS_DDL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(CitedDocuments)")}
    if "published_date" not in columns:
        conn.execute("ALTER TABLE CitedDocuments ADD COLUMN published_date TEXT")


@dataclass(frozen=True)
class CitedDocument:
    """One cited page as retrieved (or as it failed to be retrieved)."""

    url: str
    status: str
    fetched_at: str
    text: Optional[str] = None
    http_status: Optional[int] = None
    content_type: Optional[str] = None
    final_url: Optional[str] = None
    error: Optional[str] = None
    published_date: Optional[str] = None

    @property
    def is_usable(self) -> bool:
        return self.status == STATUS_OK and bool(self.text)


Fetcher = Callable[[str], CitedDocument]


# ---------------------------------------------------------------------------
# Fetching and text extraction
# ---------------------------------------------------------------------------

_DROP_TAGS = ("script", "style", "noscript", "template", "svg", "iframe", "form",
              "nav", "header", "footer", "aside")


_PUBLISHED_META = (
    ("property", "article:published_time"), ("name", "article:published_time"),
    ("itemprop", "datePublished"), ("name", "pubdate"), ("name", "publishdate"),
    ("name", "publish-date"), ("name", "date"), ("name", "dc.date"), ("name", "DC.date.issued"),
    ("property", "og:published_time"),
)


def _iso_day(value: Optional[str]) -> Optional[str]:
    match = re.match(r"\s*(\d{4}-\d{2}-\d{2})", value or "")
    return match.group(1) if match else None


def published_date_of(soup: BeautifulSoup) -> Optional[str]:
    """The page's own publication date (``YYYY-MM-DD``) from its metadata, if stated."""
    for attr, name in _PUBLISHED_META:
        tag = soup.find("meta", attrs={attr: name})
        day = _iso_day(tag.get("content") if tag else None)
        if day:
            return day
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', script.string or "")
        day = _iso_day(match.group(1) if match else None)
        if day:
            return day
    time_tag = soup.find("time", attrs={"datetime": True})
    return _iso_day(time_tag.get("datetime") if time_tag else None)


def html_to_text(html: str) -> str:
    """Visible article text of an HTML page (menus, scripts and footers removed)."""
    return html_to_text_and_date(html)[0]


def html_to_text_and_date(html: str) -> Tuple[str, Optional[str]]:
    """Visible article text of an HTML page, and its own publication date."""
    soup = BeautifulSoup(html, "html.parser")
    published = published_date_of(soup)
    for tag in soup(_DROP_TAGS):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip(), published


def pdf_to_text(data: bytes) -> str:
    """Text of a PDF (first :data:`MAX_PDF_PAGES` pages)."""
    import pdfplumber

    parts: List[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages[:MAX_PDF_PAGES]:
            parts.append(page.extract_text() or "")
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def fetch_document(
    url: str,
    *,
    session: Optional[requests.Session] = None,
    timeout: Tuple[float, float] = FETCH_TIMEOUT,
    max_bytes: int = MAX_FETCH_BYTES,
) -> CitedDocument:
    """Fetch one cited page and extract its text.

    Never raises for a page that cannot be read: the failure is returned as a
    :class:`CitedDocument` with a non-``ok`` status so it can be cached.
    """
    http = session or requests
    try:
        response = http.get(url, headers={"User-Agent": USER_AGENT,
                                          "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.5"},
                            timeout=timeout, stream=True, allow_redirects=True)
    except Exception as exc:  # network errors of every kind are a cacheable outcome
        return CitedDocument(url, STATUS_FETCH_ERROR, _now(), error=f"{type(exc).__name__}: {str(exc)[:300]}")

    try:
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        final_url = str(getattr(response, "url", url) or url)
        if response.status_code >= 400:
            return CitedDocument(url, STATUS_HTTP_ERROR, _now(), http_status=response.status_code,
                                 content_type=content_type, final_url=final_url,
                                 error=f"HTTP {response.status_code}")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) >= max_bytes:
                break
        data = bytes(body[:max_bytes])

        published: Optional[str] = None
        is_pdf = content_type == "application/pdf" or data[:5] == b"%PDF-"
        if is_pdf:
            text = pdf_to_text(data)
        elif "html" in content_type or "xml" in content_type or not content_type:
            encoding = response.encoding or "utf-8"
            text, published = html_to_text_and_date(data.decode(encoding, errors="replace"))
        elif content_type.startswith("text/"):
            encoding = response.encoding or "utf-8"
            text = re.sub(r"\s+", " ", data.decode(encoding, errors="replace")).strip()
        else:
            return CitedDocument(url, STATUS_UNSUPPORTED, _now(), http_status=response.status_code,
                                 content_type=content_type, final_url=final_url,
                                 error=f"unsupported content type {content_type!r}")
    except Exception as exc:
        return CitedDocument(url, STATUS_FETCH_ERROR, _now(), error=f"{type(exc).__name__}: {str(exc)[:300]}")
    finally:
        try:
            response.close()
        except Exception:
            pass

    text = text[:MAX_TEXT_CHARS]
    if len(text) < MIN_DOCUMENT_CHARS:
        return CitedDocument(url, STATUS_EMPTY, _now(), text=text or None, http_status=response.status_code,
                             content_type=content_type, final_url=final_url,
                             error=f"only {len(text)} characters of text", published_date=published)
    return CitedDocument(url, STATUS_OK, _now(), text=text, http_status=response.status_code,
                         content_type=content_type, final_url=final_url, published_date=published)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class CitedDocumentStore:
    """Cached access to cited pages.

    Args:
        conn: SQLite connection holding ``CitedDocuments`` (created if missing).
        lock: Lock guarding *conn* when it is shared between threads (the
            storage layer's lock); a private one is used otherwise.
        fetcher: Retrieves one URL; defaults to :func:`fetch_document`.
            Tests pass a fake - no test touches the network.
        max_workers: Concurrent fetches per :meth:`get_documents` call.
        retry_failed_after: A cached failure older than this is fetched again.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        lock: Optional[threading.Lock] = None,
        fetcher: Optional[Fetcher] = None,
        max_workers: int = 4,
        retry_failed_after: timedelta = timedelta(days=7),
    ) -> None:
        self._conn = conn
        self._lock = lock or threading.Lock()
        self._fetcher = fetcher or fetch_document
        self.max_workers = max(1, int(max_workers))
        self.retry_failed_after = retry_failed_after
        self.stats: Dict[str, int] = {"cache_hits": 0, "fetched": 0}
        with self._lock:
            ensure_schema(self._conn)
            self._conn.commit()

    def cached(self, urls: Iterable[str]) -> Dict[str, CitedDocument]:
        """Cached documents for *urls* (missing ones are absent)."""
        wanted = list(dict.fromkeys(u for u in urls if u))
        found: Dict[str, CitedDocument] = {}
        with self._lock:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start:start + 500]
                rows = self._conn.execute(
                    "SELECT url, status, fetched_at, text, http_status, content_type, final_url, error, "
                    "published_date "
                    f"FROM CitedDocuments WHERE url IN ({','.join('?' * len(chunk))})", chunk,
                ).fetchall()
                for row in rows:
                    found[row[0]] = CitedDocument(url=row[0], status=row[1], fetched_at=row[2], text=row[3],
                                                  http_status=row[4], content_type=row[5],
                                                  final_url=row[6], error=row[7], published_date=row[8])
        return found

    def _is_stale(self, document: CitedDocument) -> bool:
        if document.status == STATUS_OK:
            return False
        try:
            fetched = datetime.fromisoformat(document.fetched_at)
        except (TypeError, ValueError):
            return True
        return datetime.now() - fetched >= self.retry_failed_after

    def _store(self, documents: Iterable[CitedDocument]) -> None:
        rows = [(d.url, d.fetched_at, d.status, d.http_status, d.content_type, d.final_url, d.text, d.error,
                 d.published_date) for d in documents]
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO CitedDocuments "
                "(url, fetched_at, status, http_status, content_type, final_url, text, error, published_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            self._conn.commit()

    def get_documents(self, urls: Iterable[str],
                      progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, CitedDocument]:
        """Documents for *urls*, fetching (and caching) any not yet read.

        Every requested URL is present in the result: a page that could not
        be read is returned with its failure status, never left out.
        """
        wanted = list(dict.fromkeys(u for u in urls if u))
        documents = self.cached(wanted)
        to_fetch = [u for u in wanted if u not in documents or self._is_stale(documents[u])]
        self.stats["cache_hits"] += len(wanted) - len(to_fetch)
        if not to_fetch:
            return documents

        done = 0
        batch: List[CitedDocument] = []
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(to_fetch))) as pool:
            for document in pool.map(self._fetch_one, to_fetch):
                documents[document.url] = document
                batch.append(document)
                done += 1
                if len(batch) >= 50:
                    self._store(batch)
                    batch = []
                if progress is not None:
                    progress(done, len(to_fetch))
        self._store(batch)
        self.stats["fetched"] += len(to_fetch)
        return documents

    def _fetch_one(self, url: str) -> CitedDocument:
        document = self._fetcher(url)
        if document.url != url:  # the cache is keyed on the URL as cited
            document = CitedDocument(url=url, status=document.status, fetched_at=document.fetched_at,
                                     text=document.text, http_status=document.http_status,
                                     content_type=document.content_type,
                                     final_url=document.final_url or document.url, error=document.error,
                                     published_date=document.published_date)
        return document
