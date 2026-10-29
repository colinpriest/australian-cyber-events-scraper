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
passed.  A page that was read successfully is fresh until :meth:`CitedDocumentStore.refresh`
checks it again - a corrected notice or an updated victim list at the same URL
is otherwise invisible.  The check is conditional where the server allows it
(``If-None-Match`` / ``If-Modified-Since`` from the stored ``ETag`` /
``Last-Modified``; a 304 changes nothing) and otherwise compares a content
fingerprint of the extracted text.  A changed page keeps its old version in
``CitedDocumentVersions`` (with its fingerprint and fetch time) and the current
one in ``CitedDocuments``; a failed check never replaces a page that was read.
Which pages to check, in what order and how many is decided by
:mod:`cyber_data_collector.processing.cited_refresh`.

Fetching a public page is not a paid API call (no budgeted host is involved),
but it is bounded all the same: a connect/read timeout, a byte cap on the
download and a character cap on the stored text.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

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
#: Returned by a conditional fetch only (HTTP 304); never stored as a document's status.
STATUS_NOT_MODIFIED = "not_modified"

#: Outcomes of checking an already-read page again (:meth:`CitedDocumentStore.refresh`).
OUTCOME_CHANGED = "changed"
OUTCOME_UNCHANGED = "unchanged"          # fetched again, same content fingerprint
OUTCOME_NOT_MODIFIED = "not_modified"    # the server answered 304 to the stored validators
OUTCOME_FAILED = "failed"                # the check failed; the page already read is kept

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

#: Columns added after the first version of the table: ``(name, declaration)``.
_ADDED_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("published_date", "TEXT"),
    ("etag", "TEXT"),                       # the server's validator, sent back as If-None-Match
    ("last_modified", "TEXT"),              # sent back as If-Modified-Since
    ("content_hash", "TEXT"),               # fingerprint of the stored text (see fingerprint())
    ("version", "INTEGER DEFAULT 1"),       # which content version this row holds
    ("first_fetched_at", "TEXT"),
    ("last_checked_at", "TEXT"),            # last time the page was fetched or validated, by any route
    ("last_changed_at", "TEXT"),            # when the content last differed from the version before
    ("change_count", "INTEGER DEFAULT 0"),
    ("unchanged_streak", "INTEGER DEFAULT 0"),   # consecutive checks that found nothing new
    ("last_check_status", "TEXT"),          # outcome of the latest check (OUTCOME_*)
    ("last_check_error", "TEXT"),
)

CITED_DOCUMENT_VERSIONS_DDL = """
CREATE TABLE IF NOT EXISTS CitedDocumentVersions (
    url           TEXT NOT NULL,
    version       INTEGER NOT NULL,
    fetched_at    TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    etag          TEXT,
    last_modified TEXT,
    http_status   INTEGER,
    content_type  TEXT,
    final_url     TEXT,
    text          TEXT,
    published_date TEXT,
    superseded_at TEXT NOT NULL,
    PRIMARY KEY (url, version)
)
"""


def fingerprint(text: Optional[str]) -> str:
    """Content fingerprint of a page's extracted text (whitespace-insensitive SHA-256)."""
    normalised = re.sub(r"\s+", " ", text or "").strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create ``CitedDocuments`` and ``CitedDocumentVersions`` or add their later columns (idempotent).

    Rows read before the version columns existed are given their first version:
    fetched once, last checked when fetched, content fingerprinted.
    """
    conn.execute(CITED_DOCUMENTS_DDL)
    conn.execute(CITED_DOCUMENT_VERSIONS_DDL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(CitedDocuments)")}
    for name, declaration in _ADDED_COLUMNS:
        if name not in columns:
            conn.execute(f"ALTER TABLE CitedDocuments ADD COLUMN {name} {declaration}")
    conn.execute("UPDATE CitedDocuments SET first_fetched_at = fetched_at WHERE first_fetched_at IS NULL")
    conn.execute("UPDATE CitedDocuments SET last_checked_at = fetched_at WHERE last_checked_at IS NULL")
    conn.execute("UPDATE CitedDocuments SET version = 1 WHERE version IS NULL")
    conn.execute("UPDATE CitedDocuments SET change_count = 0 WHERE change_count IS NULL")
    conn.execute("UPDATE CitedDocuments SET unchanged_streak = 0 WHERE unchanged_streak IS NULL")
    unhashed = conn.execute("SELECT url, text FROM CitedDocuments "
                            "WHERE content_hash IS NULL AND status = ? AND text IS NOT NULL",
                            (STATUS_OK,)).fetchall()
    if unhashed:
        conn.executemany("UPDATE CitedDocuments SET content_hash = ? WHERE url = ?",
                         [(fingerprint(text), url) for url, text in unhashed])


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
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    content_hash: Optional[str] = None

    @property
    def is_usable(self) -> bool:
        return self.status == STATUS_OK and bool(self.text)


Fetcher = Callable[[str], CitedDocument]
#: Fetches a page again with the stored validators (``{"etag": ..., "last_modified": ...}``, either may be None).
ConditionalFetcher = Callable[[str, Optional[Mapping[str, Optional[str]]]], CitedDocument]


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
    validators: Optional[Mapping[str, Optional[str]]] = None,
) -> CitedDocument:
    """Fetch one cited page and extract its text.

    Never raises for a page that cannot be read: the failure is returned as a
    :class:`CitedDocument` with a non-``ok`` status so it can be cached.

    Args:
        validators: The ``etag`` / ``last_modified`` stored from an earlier
            read.  They are sent as ``If-None-Match`` / ``If-Modified-Since``;
            a server that answers 304 gets a document with status
            :data:`STATUS_NOT_MODIFIED` and no text.  A server that ignores
            them sends the page again and the caller compares fingerprints.
    """
    http = session or requests
    headers = {"User-Agent": USER_AGENT,
               "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.5"}
    if validators:
        if validators.get("etag"):
            headers["If-None-Match"] = str(validators["etag"])
        if validators.get("last_modified"):
            headers["If-Modified-Since"] = str(validators["last_modified"])
    try:
        response = http.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=True)
    except Exception as exc:  # network errors of every kind are a cacheable outcome
        return CitedDocument(url, STATUS_FETCH_ERROR, _now(), error=f"{type(exc).__name__}: {str(exc)[:300]}")

    try:
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        final_url = str(getattr(response, "url", url) or url)
        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")
        if response.status_code == 304 and validators:
            return CitedDocument(url, STATUS_NOT_MODIFIED, _now(), http_status=304, final_url=final_url,
                                 etag=etag or validators.get("etag"),
                                 last_modified=last_modified or validators.get("last_modified"))
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
                         content_type=content_type, final_url=final_url, published_date=published,
                         etag=etag, last_modified=last_modified, content_hash=fingerprint(text))


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _fetch_conditionally(url: str, validators: Optional[Mapping[str, Optional[str]]] = None) -> CitedDocument:
    """:func:`fetch_document` with the stored validators (looked up when called, so a test can replace it)."""
    return fetch_document(url, validators=validators)


@dataclass(frozen=True)
class RefreshOutcome:
    """What checking one already-read page again found."""

    url: str
    outcome: str                     # OUTCOME_*
    via: str                         # 'validator' (HTTP 304), 'fingerprint' (content compared), 'error'
    old_hash: Optional[str] = None
    new_hash: Optional[str] = None
    error: Optional[str] = None

    @property
    def is_changed(self) -> bool:
        return self.outcome == OUTCOME_CHANGED


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
        conditional_fetcher: Retrieves one URL with stored validators
            (:data:`ConditionalFetcher`) for :meth:`refresh`.  Defaults to
            :func:`fetch_document` - or, when only a *fetcher* is given, to that
            fetcher with the validators ignored (every refresh then compares
            content fingerprints).
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        lock: Optional[threading.Lock] = None,
        fetcher: Optional[Fetcher] = None,
        max_workers: int = 4,
        retry_failed_after: timedelta = timedelta(days=7),
        conditional_fetcher: Optional[ConditionalFetcher] = None,
    ) -> None:
        self._conn = conn
        self._lock = lock or threading.Lock()
        self._fetcher = fetcher or fetch_document
        if conditional_fetcher is not None:
            self._conditional_fetcher: ConditionalFetcher = conditional_fetcher
        elif fetcher is not None:
            self._conditional_fetcher = lambda url, validators=None: fetcher(url)
        else:
            self._conditional_fetcher = _fetch_conditionally
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
                    "published_date, etag, last_modified, content_hash "
                    f"FROM CitedDocuments WHERE url IN ({','.join('?' * len(chunk))})", chunk,
                ).fetchall()
                for row in rows:
                    found[row[0]] = CitedDocument(url=row[0], status=row[1], fetched_at=row[2], text=row[3],
                                                  http_status=row[4], content_type=row[5],
                                                  final_url=row[6], error=row[7], published_date=row[8],
                                                  etag=row[9], last_modified=row[10], content_hash=row[11])
        return found

    def _is_stale(self, document: CitedDocument) -> bool:
        """A failure old enough to retry.  A page that was read is never stale here - see :meth:`refresh`."""
        if document.status == STATUS_OK:
            return False
        try:
            fetched = datetime.fromisoformat(document.fetched_at)
        except (TypeError, ValueError):
            return True
        return datetime.now() - fetched >= self.retry_failed_after

    def _store(self, documents: Iterable[CitedDocument]) -> None:
        rows = [(d.url, d.fetched_at, d.status, d.http_status, d.content_type, d.final_url, d.text, d.error,
                 d.published_date, d.etag, d.last_modified,
                 d.content_hash or (fingerprint(d.text) if d.status == STATUS_OK and d.text else None),
                 d.fetched_at, d.fetched_at) for d in documents]
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO CitedDocuments "
                "(url, fetched_at, status, http_status, content_type, final_url, text, error, published_date, "
                "etag, last_modified, content_hash, version, change_count, unchanged_streak, "
                "first_fetched_at, last_checked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0, ?, ?)", rows)
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
            document = _rekeyed(document, url)
        return document

    # ------------------------------------------------------------------
    # Refreshing pages that were read
    # ------------------------------------------------------------------

    def _check_one(self, url: str, previous: CitedDocument) -> Tuple[str, CitedDocument]:
        validators = {"etag": previous.etag, "last_modified": previous.last_modified}
        document = self._conditional_fetcher(url, validators if any(validators.values()) else None)
        if document.url != url:
            document = _rekeyed(document, url)
        return url, document

    def refresh(self, urls: Iterable[str],
                progress: Optional[Callable[[int, int], None]] = None) -> List[RefreshOutcome]:
        """Check pages that were read before and store what changed.

        For each URL with a stored page, the page is fetched with its stored
        ``ETag`` / ``Last-Modified`` (a 304 means nothing changed) and, when the
        server sends the page, its content fingerprint is compared with the
        stored one.  A changed page is stored as a new version: the previous
        version moves to ``CitedDocumentVersions`` with its fingerprint and
        fetch time, and the current row carries the new text, fingerprint,
        validators and fetch time.  A check that fails (network error, an HTTP
        error, no usable text) records the failure and **keeps the page that was
        read**: a transient outage must not remove the support facts rely on.

        URLs with no stored page are ignored (:meth:`get_documents` reads them).
        Returns one :class:`RefreshOutcome` per URL checked.
        """
        wanted = list(dict.fromkeys(u for u in urls if u))
        previous = {u: d for u, d in self.cached(wanted).items() if d.status == STATUS_OK and d.text}
        todo = [u for u in wanted if u in previous]
        if not todo:
            return []
        fetched: Dict[str, CitedDocument] = {}
        done = 0
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(todo))) as pool:
            for url, document in pool.map(lambda u: self._check_one(u, previous[u]), todo):
                fetched[url] = document
                done += 1
                if progress is not None:
                    progress(done, len(todo))
        outcomes = [self._record_check(url, previous[url], fetched[url]) for url in todo]
        self.stats["checked"] = self.stats.get("checked", 0) + len(todo)
        return outcomes

    def _record_check(self, url: str, old: CitedDocument, new: CitedDocument) -> RefreshOutcome:
        now = _now()
        old_hash = old.content_hash or fingerprint(old.text)
        with self._lock:
            try:
                if new.status == STATUS_NOT_MODIFIED:
                    self._conn.execute(
                        "UPDATE CitedDocuments SET last_checked_at = ?, unchanged_streak = unchanged_streak + 1, "
                        "last_check_status = ?, last_check_error = NULL, etag = COALESCE(?, etag), "
                        "last_modified = COALESCE(?, last_modified) WHERE url = ?",
                        (now, OUTCOME_NOT_MODIFIED, new.etag, new.last_modified, url))
                    outcome = RefreshOutcome(url, OUTCOME_NOT_MODIFIED, "validator", old_hash, old_hash)
                elif new.status == STATUS_OK and new.text:
                    new_hash = new.content_hash or fingerprint(new.text)
                    if new_hash == old_hash:
                        self._conn.execute(
                            "UPDATE CitedDocuments SET last_checked_at = ?, unchanged_streak = unchanged_streak + 1, "
                            "last_check_status = ?, last_check_error = NULL, etag = COALESCE(?, etag), "
                            "last_modified = COALESCE(?, last_modified), content_hash = ? WHERE url = ?",
                            (now, OUTCOME_UNCHANGED, new.etag, new.last_modified, old_hash, url))
                        outcome = RefreshOutcome(url, OUTCOME_UNCHANGED, "fingerprint", old_hash, new_hash)
                    else:
                        self._archive_current(url, now)
                        self._conn.execute(
                            "UPDATE CitedDocuments SET fetched_at = ?, status = ?, http_status = ?, "
                            "content_type = ?, final_url = ?, text = ?, error = NULL, published_date = ?, "
                            "etag = ?, last_modified = ?, content_hash = ?, version = version + 1, "
                            "last_checked_at = ?, last_changed_at = ?, change_count = change_count + 1, "
                            "unchanged_streak = 0, last_check_status = ?, last_check_error = NULL WHERE url = ?",
                            (new.fetched_at, new.status, new.http_status, new.content_type,
                             new.final_url, new.text, new.published_date, new.etag, new.last_modified,
                             new_hash, now, now, OUTCOME_CHANGED, url))
                        outcome = RefreshOutcome(url, OUTCOME_CHANGED, "fingerprint", old_hash, new_hash)
                else:
                    reason = new.error or new.status
                    self._conn.execute(
                        "UPDATE CitedDocuments SET last_checked_at = ?, unchanged_streak = unchanged_streak + 1, "
                        "last_check_status = ?, last_check_error = ? WHERE url = ?",
                        (now, OUTCOME_FAILED, str(reason)[:300], url))
                    outcome = RefreshOutcome(url, OUTCOME_FAILED, "error", old_hash, None, str(reason)[:300])
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return outcome

    def _archive_current(self, url: str, superseded_at: str) -> None:
        """Copy the current row of *url* to ``CitedDocumentVersions`` (caller holds the lock)."""
        self._conn.execute(
            "INSERT OR IGNORE INTO CitedDocumentVersions (url, version, fetched_at, content_hash, etag, "
            "last_modified, http_status, content_type, final_url, text, published_date, superseded_at) "
            "SELECT url, COALESCE(version, 1), fetched_at, COALESCE(content_hash, ''), etag, last_modified, "
            "http_status, content_type, final_url, text, published_date, ? FROM CitedDocuments WHERE url = ?",
            (superseded_at, url))

    def versions(self, url: str) -> List[Dict[str, object]]:
        """Every superseded version of *url*, oldest first (the current one is in ``CitedDocuments``)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, fetched_at, content_hash, etag, last_modified, superseded_at, "
                "LENGTH(text) FROM CitedDocumentVersions WHERE url = ? ORDER BY version", (url,)).fetchall()
        return [{"version": r[0], "fetched_at": r[1], "content_hash": r[2], "etag": r[3],
                 "last_modified": r[4], "superseded_at": r[5], "text_chars": r[6]} for r in rows]


def _rekeyed(document: CitedDocument, url: str) -> CitedDocument:
    """*document* stored under the URL as cited, not the one it was fetched from."""
    return CitedDocument(url=url, status=document.status, fetched_at=document.fetched_at,
                         text=document.text, http_status=document.http_status,
                         content_type=document.content_type,
                         final_url=document.final_url or document.url, error=document.error,
                         published_date=document.published_date, etag=document.etag,
                         last_modified=document.last_modified, content_hash=document.content_hash)
