"""Pages that were read are checked again, and only what a changed page affects is rejudged.

Reviewer finding: ``CitedDocumentStore`` retried failed fetches but treated every
successful one as permanently fresh, so a corrected notice or an updated victim
list at the same URL stayed invisible. Covered here (no test touches the
network; an autouse fixture sets bogus API keys so a call that reached a paid
service would fail):

* conditional requests carry the stored ``ETag`` / ``Last-Modified``; a 304
  changes nothing; a server that ignores them is compared by content fingerprint;
* a changed page keeps its old version (fingerprint, fetch time) and a failed
  check keeps the page that was read;
* tables from before the version columns are migrated;
* which pages are due, in what order, under a budget;
* a changed page rejudges the facts it supported - and only those records - against
  the cached pages, writes ``EnrichmentFactSupport`` and the enrichment, and
  refreshes events only when an enrichment changed.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from cyber_data_collector.processing import cited_documents as cd
from cyber_data_collector.processing import cited_refresh as cr
from cyber_data_collector.processing.cited_documents import (
    STATUS_FETCH_ERROR,
    STATUS_NOT_MODIFIED,
    STATUS_OK,
    CitedDocument,
    CitedDocumentStore,
    fetch_document,
    fingerprint,
)
from cyber_data_collector.storage.cyber_event_data_v2 import CyberEventDataV2
from cyber_data_collector.storage.enrichment_support import load_fact_support

URL = "https://news.example.com.au/acme-update"
OTHER_URL = "https://news.example.com.au/other"
TODAY = datetime(2026, 10, 29, 12, 0, 0)
ARTICLE = ("Acme Logistics Pty Ltd confirmed on 14 March 2025 that attackers encrypted its warehouse systems. "
           "The company said the LockBit group claimed responsibility for the ransomware attack. ") * 2
PAGE_V1 = ("Acme Logistics has confirmed that up to 150,000 customers in Australia were affected by the "
           "attack. Acme Logistics said it is writing to every customer. ") * 3
PAGE_V2 = ("Acme Logistics has corrected its notice: 90,000 customers in Australia were affected by the "
           "attack, not the larger figure first reported by Acme Logistics. ") * 3


@pytest.fixture(autouse=True)
def bogus_keys(monkeypatch):
    for name in ("OPENAI_API_KEY", "PERPLEXITY_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.setenv(name, "bogus-not-a-key")


def _page(url: str, text: str, etag: Optional[str] = None, last_modified: Optional[str] = None,
          fetched_at: Optional[str] = None) -> CitedDocument:
    return CitedDocument(url, STATUS_OK, fetched_at or datetime.now().isoformat(timespec="seconds"), text=text,
                         etag=etag, last_modified=last_modified, content_hash=fingerprint(text))


def _not_modified(url: str, etag: Optional[str] = None) -> CitedDocument:
    return CitedDocument(url, STATUS_NOT_MODIFIED, datetime.now().isoformat(timespec="seconds"),
                         http_status=304, etag=etag)


class FakeSite:
    """Serves pages; records the validators each request carried."""

    def __init__(self, pages: Dict[str, CitedDocument], honours_validators: bool = True):
        self.pages = pages
        self.honours_validators = honours_validators
        self.requests: List[tuple] = []

    def fetch(self, url: str, validators=None) -> CitedDocument:
        self.requests.append((url, dict(validators) if validators else None))
        page = self.pages.get(url)
        if page is None:
            return CitedDocument(url, STATUS_FETCH_ERROR, datetime.now().isoformat(timespec="seconds"),
                                 error="offline in tests")
        if self.honours_validators and validators and validators.get("etag") and validators["etag"] == page.etag:
            return _not_modified(url, page.etag)
        return page


def _store(conn: sqlite3.Connection, site: FakeSite) -> CitedDocumentStore:
    return CitedDocumentStore(conn, fetcher=lambda url: site.fetch(url), conditional_fetcher=site.fetch)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# fetch_document with validators
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, status=200, headers=None, body=b"", url=URL):
        self.status_code = status
        self.headers = {"Content-Type": "text/html", **(headers or {})}
        self.encoding = "utf-8"
        self._body = body
        self.url = url

    def iter_content(self, chunk_size=65536):
        yield self._body

    def close(self):
        pass


class _Session:
    def __init__(self, response):
        self.response, self.calls = response, []

    def get(self, url, **kwargs):
        self.calls.append(kwargs)
        return self.response


class TestConditionalFetch:
    def test_stored_validators_are_sent_and_a_304_is_not_modified(self):
        session = _Session(_Response(304, {"ETag": '"v1"'}))
        document = fetch_document(URL, session=session, validators={"etag": '"v1"', "last_modified": "Mon, 01 Jan 2026"})
        headers = session.calls[0]["headers"]
        assert headers["If-None-Match"] == '"v1"' and headers["If-Modified-Since"] == "Mon, 01 Jan 2026"
        assert document.status == STATUS_NOT_MODIFIED and not document.is_usable

    def test_no_validators_means_no_conditional_headers(self):
        session = _Session(_Response(200, body=b"<html><body><p>" + b"Acme said so. " * 20 + b"</p></body></html>"))
        fetch_document(URL, session=session)
        assert "If-None-Match" not in session.calls[0]["headers"]
        assert "If-Modified-Since" not in session.calls[0]["headers"]

    def test_a_page_records_its_validators_and_a_content_fingerprint(self):
        body = b"<html><body><p>" + b"Acme said so.  " * 20 + b"</p></body></html>"
        session = _Session(_Response(200, {"ETag": 'W/"x"', "Last-Modified": "Tue, 02 Jan 2026 00:00:00 GMT"}, body))
        document = fetch_document(URL, session=session)
        assert document.etag == 'W/"x"' and document.last_modified == "Tue, 02 Jan 2026 00:00:00 GMT"
        assert document.content_hash == fingerprint(document.text)

    def test_the_fingerprint_ignores_whitespace_only(self):
        assert fingerprint("a  b\n c") == fingerprint("a b c")
        assert fingerprint("a b c") != fingerprint("a b d")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestSchema:
    def test_a_table_from_before_the_version_columns_is_migrated_with_its_pages_kept(self, conn):
        conn.execute(cd.CITED_DOCUMENTS_DDL)
        conn.execute("INSERT INTO CitedDocuments (url, fetched_at, status, text) VALUES (?, ?, 'ok', ?)",
                     (URL, "2026-10-28T09:00:00", PAGE_V1))
        conn.execute("INSERT INTO CitedDocuments (url, fetched_at, status, error) VALUES (?, ?, 'fetch_error', 'x')",
                     (OTHER_URL, "2026-10-28T09:00:00"))
        cd.ensure_schema(conn)
        cd.ensure_schema(conn)                       # idempotent
        row = conn.execute("SELECT content_hash, version, first_fetched_at, last_checked_at, change_count, "
                           "unchanged_streak FROM CitedDocuments WHERE url = ?", (URL,)).fetchone()
        assert row == (fingerprint(PAGE_V1), 1, "2026-10-28T09:00:00", "2026-10-28T09:00:00", 0, 0)
        failed = conn.execute("SELECT content_hash FROM CitedDocuments WHERE url = ?", (OTHER_URL,)).fetchone()
        assert failed == (None,)
        assert conn.execute("SELECT COUNT(*) FROM CitedDocumentVersions").fetchone()[0] == 0

    def test_a_page_read_by_get_documents_is_stored_with_its_validators_and_fingerprint(self, conn):
        site = FakeSite({URL: _page(URL, PAGE_V1, etag='"v1"', last_modified="Mon, 01 Jan 2026")})
        _store(conn, site).get_documents([URL])
        row = conn.execute("SELECT etag, last_modified, content_hash, version FROM CitedDocuments").fetchone()
        assert row == ('"v1"', "Mon, 01 Jan 2026", fingerprint(PAGE_V1), 1)


# ---------------------------------------------------------------------------
# Refreshing a page
# ---------------------------------------------------------------------------

class TestRefresh:
    def _read(self, conn, site) -> CitedDocumentStore:
        store = _store(conn, site)
        store.get_documents([URL])
        return store

    def test_the_stored_etag_is_sent_and_a_304_changes_nothing(self, conn):
        site = FakeSite({URL: _page(URL, PAGE_V1, etag='"v1"')})
        store = self._read(conn, site)
        [outcome] = store.refresh([URL])
        assert outcome.outcome == cd.OUTCOME_NOT_MODIFIED and outcome.via == "validator" and not outcome.is_changed
        assert site.requests[-1] == (URL, {"etag": '"v1"', "last_modified": None})
        row = conn.execute("SELECT version, change_count, unchanged_streak, last_check_status FROM CitedDocuments"
                           ).fetchone()
        assert row == (1, 0, 1, "not_modified")
        assert store.versions(URL) == []

    def test_a_page_with_no_validators_is_fetched_plainly_and_compared_by_fingerprint(self, conn):
        site = FakeSite({URL: _page(URL, PAGE_V1)})
        store = self._read(conn, site)
        [outcome] = store.refresh([URL])
        assert site.requests[-1] == (URL, None)
        assert (outcome.outcome, outcome.via) == (cd.OUTCOME_UNCHANGED, "fingerprint")

    def test_a_server_that_ignores_validators_is_still_compared_by_fingerprint(self, conn):
        site = FakeSite({URL: _page(URL, PAGE_V1, etag='"v1"')}, honours_validators=False)
        store = self._read(conn, site)
        [same] = store.refresh([URL])
        assert (same.outcome, same.via) == (cd.OUTCOME_UNCHANGED, "fingerprint")
        site.pages[URL] = _page(URL, PAGE_V2, etag='"v1"')       # changed, though it still sends the old ETag
        [changed] = store.refresh([URL])
        assert changed.is_changed

    def test_a_changed_page_keeps_its_old_version_with_fingerprint_and_fetch_time(self, conn):
        site = FakeSite({URL: _page(URL, PAGE_V1, etag='"v1"', fetched_at="2026-10-01T10:00:00")})
        store = self._read(conn, site)
        conn.execute("UPDATE CitedDocuments SET fetched_at = '2026-10-01T10:00:00'")
        site.pages[URL] = _page(URL, PAGE_V2, etag='"v2"')
        [outcome] = store.refresh([URL])
        assert outcome.is_changed and outcome.old_hash == fingerprint(PAGE_V1) and outcome.new_hash == fingerprint(PAGE_V2)
        current = conn.execute("SELECT text, content_hash, etag, version, change_count, unchanged_streak, "
                               "last_changed_at IS NOT NULL FROM CitedDocuments").fetchone()
        assert current == (PAGE_V2, fingerprint(PAGE_V2), '"v2"', 2, 1, 0, 1)
        [old] = store.versions(URL)
        assert (old["version"], old["fetched_at"], old["content_hash"], old["etag"]) == (
            1, "2026-10-01T10:00:00", fingerprint(PAGE_V1), '"v1"')
        assert old["text_chars"] == len(PAGE_V1)
        # The old text is kept, not just its hash.
        assert conn.execute("SELECT text FROM CitedDocumentVersions WHERE version = 1").fetchone()[0] == PAGE_V1

    def test_a_second_change_adds_a_second_version(self, conn):
        site = FakeSite({URL: _page(URL, PAGE_V1)})
        store = self._read(conn, site)
        site.pages[URL] = _page(URL, PAGE_V2)
        store.refresh([URL])
        site.pages[URL] = _page(URL, PAGE_V2 + " A further correction was issued on Friday.")
        store.refresh([URL])
        assert [v["version"] for v in store.versions(URL)] == [1, 2]
        assert conn.execute("SELECT version, change_count FROM CitedDocuments").fetchone() == (3, 2)

    def test_a_failed_check_keeps_the_page_that_was_read(self, conn):
        site = FakeSite({URL: _page(URL, PAGE_V1)})
        store = self._read(conn, site)
        del site.pages[URL]
        [outcome] = store.refresh([URL])
        assert outcome.outcome == cd.OUTCOME_FAILED and "offline" in outcome.error
        row = conn.execute("SELECT status, text, last_check_status, unchanged_streak FROM CitedDocuments").fetchone()
        assert row == (STATUS_OK, PAGE_V1, "failed", 1)       # still readable, still the evidence
        assert store.versions(URL) == []

    def test_a_page_that_comes_back_with_no_text_is_a_failed_check_not_a_change(self, conn):
        site = FakeSite({URL: _page(URL, PAGE_V1)})
        store = self._read(conn, site)
        site.pages[URL] = CitedDocument(URL, cd.STATUS_EMPTY, "2026-10-29T10:00:00", text="x", error="only 1 characters")
        [outcome] = store.refresh([URL])
        assert outcome.outcome == cd.OUTCOME_FAILED
        assert conn.execute("SELECT text FROM CitedDocuments").fetchone()[0] == PAGE_V1

    def test_urls_never_read_are_not_refreshed(self, conn):
        site = FakeSite({URL: _page(URL, PAGE_V1)})
        store = _store(conn, site)
        assert store.refresh([URL]) == []
        assert site.requests == []

    def test_the_default_fetcher_is_called_with_the_stored_validators(self, conn, monkeypatch):
        seen = []

        def real(url, **kwargs):
            seen.append((url, kwargs.get("validators")))
            return _page(url, PAGE_V1, etag='"v1"') if not kwargs.get("validators") else _not_modified(url, '"v1"')

        monkeypatch.setattr(cd, "fetch_document", real)
        store = CitedDocumentStore(conn)
        store.get_documents([URL])
        [outcome] = store.refresh([URL])
        assert outcome.outcome == cd.OUTCOME_NOT_MODIFIED
        assert seen == [(URL, None), (URL, {"etag": '"v1"', "last_modified": None})]

    def test_get_documents_still_treats_a_page_that_was_read_as_fresh(self, conn):
        site = FakeSite({URL: _page(URL, PAGE_V1)})
        store = self._read(conn, site)
        store.get_documents([URL])
        assert len(site.requests) == 1


# ---------------------------------------------------------------------------
# What is due, and in what order
# ---------------------------------------------------------------------------

PLAN_DDL = """
CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, status TEXT DEFAULT 'Active',
    perplexity_enrichment_data TEXT);
CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, status TEXT DEFAULT 'Active',
    event_date DATE, evidence_status TEXT);
CREATE TABLE EventDeduplicationMap (deduplicated_event_id TEXT, enriched_event_id TEXT);
CREATE TABLE EnrichmentFactSupport (enriched_event_id TEXT, fact TEXT, status TEXT, value_json TEXT,
    confidence REAL, source_kind TEXT, source_url TEXT, reason TEXT, details_json TEXT, checked_at TEXT,
    PRIMARY KEY (enriched_event_id, fact));
CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
"""


def _plan_db(conn) -> None:
    conn.executescript(PLAN_DDL)
    cd.ensure_schema(conn)


def _cite(conn, record_id: str, event_id: str, urls: List[str], event_date: str = "2020-01-01",
          status: str = "Active", evidence_status: Optional[str] = "supported", record_status: str = "Active") -> None:
    conn.execute("INSERT OR IGNORE INTO DeduplicatedEvents VALUES (?, ?, ?, ?)",
                 (event_id, status, event_date, evidence_status))
    conn.execute("INSERT INTO EnrichedEvents VALUES (?, ?, ?)",
                 (record_id, record_status, json.dumps({"sources_consulted": urls})))
    conn.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?)", (event_id, record_id))


def _doc(conn, url: str, checked: str = "2026-01-01T00:00:00", published: Optional[str] = None,
         streak: int = 0, changes: int = 0, changed_at: Optional[str] = None, status: str = "ok") -> None:
    conn.execute("INSERT INTO CitedDocuments (url, fetched_at, status, text, published_date, first_fetched_at, "
                 "last_checked_at, unchanged_streak, change_count, last_changed_at, version) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                 (url, checked, status, PAGE_V1 if status == "ok" else None, published, checked, checked,
                  streak, changes, changed_at))


class TestPlan:
    def test_only_pages_an_active_event_rests_on_are_checked(self, conn):
        _plan_db(conn)
        _cite(conn, "r1", "e1", [URL])
        _cite(conn, "r2", "e2", ["https://a.example.com/merged"], status="Merged")
        _cite(conn, "r3", "e3", ["https://a.example.com/inactive-record"], record_status="Rejected")
        for url in (URL, "https://a.example.com/merged", "https://a.example.com/inactive-record",
                    "https://a.example.com/cited-by-nobody"):
            _doc(conn, url)
        plan = cr.plan_refresh(conn, TODAY, limit=10)
        assert (plan.cached_total, plan.readable_total, plan.cited_by_active) == (4, 4, 1)
        assert [d.url for d in plan.selected] == [URL]

    def test_a_support_row_citing_a_page_counts_even_if_the_enrichment_json_does_not_list_it(self, conn):
        _plan_db(conn)
        _cite(conn, "r1", "e1", [])
        conn.execute("INSERT INTO EnrichmentFactSupport VALUES ('r1', 'date', 'supported', NULL, 0.9, "
                     "'cited_document', ?, NULL, NULL, '2026-01-01')", (URL,))
        _doc(conn, URL)
        assert [d.url for d in cr.plan_refresh(conn, TODAY).selected] == [URL]

    def test_a_page_is_due_only_after_its_interval(self, conn):
        _plan_db(conn)
        for i, url in enumerate((URL, OTHER_URL)):
            _cite(conn, f"r{i}", f"e{i}", [url])
        _doc(conn, URL, checked=(TODAY - timedelta(days=44)).isoformat())
        _doc(conn, OTHER_URL, checked=(TODAY - timedelta(days=46)).isoformat())
        plan = cr.plan_refresh(conn, TODAY)
        assert [d.url for d in plan.due] == [OTHER_URL]

    def test_recently_published_or_recently_changed_pages_are_checked_sooner(self, conn):
        _plan_db(conn)
        urls = {"fresh": "https://a.example.com/fresh", "changed": "https://a.example.com/changed",
                "quiet": "https://a.example.com/quiet"}
        for i, url in enumerate(urls.values()):
            _cite(conn, f"r{i}", f"e{i}", [url])
        checked = (TODAY - timedelta(days=15)).isoformat()
        _doc(conn, urls["fresh"], checked=checked, published=(TODAY - timedelta(days=20)).date().isoformat())
        _doc(conn, urls["changed"], checked=checked, changes=1, changed_at=(TODAY - timedelta(days=40)).isoformat())
        _doc(conn, urls["quiet"], checked=checked, published="2024-01-01")
        assert {d.url for d in cr.plan_refresh(conn, TODAY).due} == {urls["fresh"], urls["changed"]}

    def test_each_check_that_finds_nothing_lengthens_the_interval(self, conn):
        state = lambda streak: cr.DocumentState(URL, "2026-01-01T00:00:00", None, None, None, 0, streak, False)  # noqa: E731
        assert [cr.interval_days(state(s), TODAY) for s in (0, 1, 2, 3, 9)] == [45, 90, 180, 180, 180]
        recent = cr.DocumentState(URL, "x", None, (TODAY - timedelta(days=5)).date().isoformat(), None, 0, 2, False)
        assert cr.interval_days(recent, TODAY) == 56

    def test_pages_are_ranked_by_what_depends_on_them_and_cut_at_the_budget(self, conn):
        _plan_db(conn)
        pages = {n: f"https://a.example.com/{n}" for n in ("plain", "withheld", "weak", "recent")}
        _cite(conn, "r1", "e1", [pages["plain"]], evidence_status="supported")
        _cite(conn, "r2", "e2", [pages["withheld"]], evidence_status="supported")
        conn.execute("INSERT INTO EnrichmentFactSupport VALUES ('r2', 'victim_count', 'withheld', '5', 0.9, "
                     "NULL, NULL, 'not stated', NULL, '2026-01-01')")
        _cite(conn, "r3", "e3", [pages["weak"]], evidence_status="unsupported")
        _cite(conn, "r4", "e4", [pages["recent"]], event_date="2026-09-01", evidence_status="supported")
        for url in pages.values():
            _doc(conn, url)
        plan = cr.plan_refresh(conn, TODAY, limit=3)
        assert [d.url for d in plan.due] == [pages["withheld"], pages["weak"], pages["recent"], pages["plain"]]
        assert [d.url for d in plan.selected] == [pages["withheld"], pages["weak"], pages["recent"]]
        assert plan.due[0].reasons == ["withheld fact"] and plan.due[3].reasons == ["overdue"]
        assert plan.limit == 3 and cr.plan_refresh(conn, TODAY, limit=0).selected == []

    def test_a_flagged_attribution_raises_a_pages_priority(self, conn):
        _plan_db(conn)
        conn.execute("CREATE TABLE VictimAttributionActions (action_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "deduplicated_event_id TEXT, entity_id INTEGER, action TEXT, reason TEXT, "
                     "p_recorded_victim REAL, created_at TEXT)")
        conn.execute("INSERT INTO VictimAttributionActions (deduplicated_event_id, entity_id, action, reason, "
                     "p_recorded_victim, created_at) VALUES ('e2', 7, 'flagged', 'low', 0.1, '2026-10-01')")
        conn.execute("INSERT INTO DeduplicatedEventEntities VALUES ('e2', 7, 'victim')")
        _cite(conn, "r1", "e1", [URL])
        _cite(conn, "r2", "e2", [OTHER_URL])
        _doc(conn, URL)
        _doc(conn, OTHER_URL)
        plan = cr.plan_refresh(conn, TODAY)
        assert plan.due[0].url == OTHER_URL and "flagged attribution" in plan.due[0].reasons


# ---------------------------------------------------------------------------
# Rerunning what a changed page affects
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE RawEvents (
    raw_event_id TEXT PRIMARY KEY, source_type VARCHAR(50), source_event_id TEXT,
    raw_title TEXT, raw_description TEXT, raw_content TEXT, event_date DATE,
    source_url VARCHAR(2048), source_metadata TEXT,
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, is_processed BOOLEAN DEFAULT FALSE,
    processing_attempted_at TIMESTAMP, processing_error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE EnrichedEvents (
    enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title VARCHAR(255),
    description TEXT, summary TEXT, event_type VARCHAR(50), severity VARCHAR(20),
    event_date DATE, records_affected BIGINT, is_australian_event BOOLEAN,
    is_specific_event BOOLEAN, confidence_score REAL, australian_relevance_score REAL,
    status VARCHAR(20) DEFAULT 'Active', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, perplexity_validated BOOLEAN DEFAULT FALSE,
    perplexity_validated_at TIMESTAMP, perplexity_enrichment_data TEXT, date_confidence REAL,
    entity_confidence REAL, data_source_reliability REAL, attacking_entity_id INTEGER,
    attacking_entity_name VARCHAR(255), attack_method VARCHAR(100));
CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY AUTOINCREMENT, entity_name TEXT UNIQUE,
    entity_type TEXT, is_australian BOOLEAN, confidence_score REAL, created_at TIMESTAMP);
CREATE TABLE EnrichedEventEntities (enriched_event_id TEXT, entity_id INTEGER, relationship_type TEXT,
    confidence_score REAL);
CREATE TABLE ProcessingLog (log_id TEXT PRIMARY KEY, raw_event_id TEXT, processing_stage TEXT, status TEXT,
    result_data TEXT, error_message TEXT, processing_time_ms INTEGER, created_at TIMESTAMP);
CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, status TEXT DEFAULT 'Active',
    event_date DATE, evidence_status TEXT);
CREATE TABLE EventDeduplicationMap (deduplicated_event_id TEXT, enriched_event_id TEXT);
CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
"""

ANSWER = dict(
    earliest_event_date=None, date_confidence=None, formal_entity_name="Acme Logistics Pty Ltd",
    entity_confidence=0.85, victim_industry="Transportation", threat_actor=None, threat_actor_confidence=None,
    attack_method=None, attack_method_confidence=None, victim_count=150_000, victim_count_confidence=0.9,
    sources_consulted=[URL], overall_confidence=0.8, parse_failed=False)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    setup = sqlite3.connect(path)
    setup.executescript(DDL)
    setup.close()
    database = CyberEventDataV2(path)
    yield database
    database.close()


def _record(db: CyberEventDataV2, event_id: str, urls: List[str], **answer: Any) -> str:
    raw_id = db.add_raw_event("GoogleSearch", {
        "title": "Acme Logistics breach", "description": "snippet", "content": ARTICLE,
        "event_date": "2025-03-10", "source_url": "https://news.example.com.au/acme",
        "metadata": {"publication_date": "2025-03-20"}})
    enriched_id = db.create_enriched_event(raw_id, {
        "title": "Acme Logistics breach", "description": "d", "event_date": "2025-03-14",
        "is_australian_event": True, "is_specific_event": True, "confidence_score": 0.7})
    blob = {**ANSWER, "sources_consulted": urls, **answer}
    with db._lock:
        db._conn.execute("UPDATE EnrichedEvents SET perplexity_validated = 1, data_source_reliability = 0.8, "
                         "perplexity_enrichment_data = ?, records_affected = ? WHERE enriched_event_id = ?",
                         (json.dumps(blob), blob.get("victim_count"), enriched_id))
        db._conn.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, status, event_date, evidence_status) "
                         "VALUES (?, 'Active', '2025-03-14', 'supported')", (event_id,))
        db._conn.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?)", (event_id, enriched_id))
        db._conn.commit()
    return enriched_id


def _judge_first(db, site: FakeSite) -> CitedDocumentStore:
    """Judge every stored enrichment once against the pages as first read (the --recompute-reliability pass)."""
    from cyber_data_collector.processing.reliability_recheck import recheck_stored_enrichments

    store = _store(db._conn, site)
    recheck_stored_enrichments(db._conn, store, dry_run=False)
    return store


def _age(db: CyberEventDataV2, days: int = 120) -> None:
    old = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    db._conn.execute("UPDATE CitedDocuments SET last_checked_at = ?, fetched_at = ?", (old, old))
    db._conn.commit()


def _victim_count(db, enriched_id: str) -> Any:
    blob = db._conn.execute("SELECT perplexity_enrichment_data FROM EnrichedEvents WHERE enriched_event_id = ?",
                            (enriched_id,)).fetchone()[0]
    return json.loads(blob)["victim_count"]


class TestRerun:
    def test_a_corrected_page_rejudges_the_fact_it_supported(self, db, monkeypatch):
        enriched_id = _record(db, "ev1", [URL])
        site = FakeSite({URL: _page(URL, PAGE_V1, etag='"v1"')})
        store = _judge_first(db, site)
        assert _victim_count(db, enriched_id) == 150_000
        supported = load_fact_support(db._conn, [enriched_id])[enriched_id]["victim_count"]
        assert (supported["status"], supported["source_kind"], supported["source_url"]) == (
            "supported", "cited_document", URL)

        refreshed = []
        monkeypatch.setattr("cyber_data_collector.dedup.record_fingerprints.refresh_changed_events",
                            lambda conn, dry_run=False: refreshed.append(1) or SimpleNamespace(refreshed_events=["ev1"]))
        _age(db)
        site.pages[URL] = _page(URL, PAGE_V2, etag='"v2"')              # the notice now says 90,000
        report = cr.refresh_cited_documents(db._conn, store, limit=5, now=datetime.now())

        assert report.changed_urls == [URL] and report.records_rejudged == 1
        assert _victim_count(db, enriched_id) is None                    # 150,000 is no longer stated anywhere
        support = load_fact_support(db._conn, [enriched_id])[enriched_id]["victim_count"]
        assert support["status"] == "withheld" and support["value"] == 150_000
        assert report.rows_changed == 1 and report.events_refreshed == 1 and refreshed == [1]
        assert [v["content_hash"] for v in store.versions(URL)] == [fingerprint(PAGE_V1)]

    def test_a_withheld_claim_is_supported_again_when_a_correction_states_it(self, db, monkeypatch):
        monkeypatch.setattr("cyber_data_collector.dedup.record_fingerprints.refresh_changed_events",
                            lambda conn, dry_run=False: SimpleNamespace(refreshed_events=[]))
        enriched_id = _record(db, "ev1", [URL], victim_count=90_000)
        site = FakeSite({URL: _page(URL, PAGE_V1)})                       # the first notice said 150,000
        store = _judge_first(db, site)
        assert _victim_count(db, enriched_id) is None
        _age(db)
        site.pages[URL] = _page(URL, PAGE_V2)
        cr.refresh_cited_documents(db._conn, store, limit=5, now=datetime.now())
        assert _victim_count(db, enriched_id) == 90_000
        assert load_fact_support(db._conn, [enriched_id])[enriched_id]["victim_count"]["status"] == "supported"

    def test_only_records_citing_a_changed_page_are_rejudged(self, db, monkeypatch):
        refreshed = []
        monkeypatch.setattr("cyber_data_collector.dedup.record_fingerprints.refresh_changed_events",
                            lambda conn, dry_run=False: refreshed.append(1) or SimpleNamespace(refreshed_events=[]))
        affected = _record(db, "ev1", [URL])
        bystander = _record(db, "ev2", [OTHER_URL])
        site = FakeSite({URL: _page(URL, PAGE_V1), OTHER_URL: _page(OTHER_URL, PAGE_V1)})
        store = _judge_first(db, site)
        stamp = db._conn.execute("SELECT checked_at FROM EnrichmentFactSupport WHERE enriched_event_id = ? "
                                 "AND fact = 'victim_count'", (bystander,)).fetchone()[0]
        _age(db)
        site.pages[URL] = _page(URL, PAGE_V2)
        report = cr.refresh_cited_documents(db._conn, store, limit=5, now=datetime.now())
        assert sorted(report.changed_urls) == [URL] and report.records_rejudged == 1
        assert _victim_count(db, bystander) == 150_000
        assert db._conn.execute("SELECT checked_at FROM EnrichmentFactSupport WHERE enriched_event_id = ? "
                                "AND fact = 'victim_count'", (bystander,)).fetchone()[0] == stamp
        assert affected != bystander

    def test_an_unchanged_page_rejudges_nothing_and_refreshes_no_event(self, db, monkeypatch):
        refreshed = []
        monkeypatch.setattr("cyber_data_collector.dedup.record_fingerprints.refresh_changed_events",
                            lambda conn, dry_run=False: refreshed.append(1))
        _record(db, "ev1", [URL])
        site = FakeSite({URL: _page(URL, PAGE_V1, etag='"v1"')})
        store = _judge_first(db, site)
        _age(db)
        report = cr.refresh_cited_documents(db._conn, store, limit=5, now=datetime.now())
        assert report.counts == {cd.OUTCOME_NOT_MODIFIED: 1}
        assert report.changed_urls == [] and report.records_rejudged == 0 and refreshed == []

    def test_a_change_that_alters_no_fact_changes_no_enrichment_and_refreshes_no_event(self, db, monkeypatch):
        refreshed = []
        monkeypatch.setattr("cyber_data_collector.dedup.record_fingerprints.refresh_changed_events",
                            lambda conn, dry_run=False: refreshed.append(1))
        enriched_id = _record(db, "ev1", [URL])
        site = FakeSite({URL: _page(URL, PAGE_V1)})
        store = _judge_first(db, site)
        blob_before = db._conn.execute("SELECT perplexity_enrichment_data FROM EnrichedEvents").fetchone()[0]
        _age(db)
        site.pages[URL] = _page(URL, PAGE_V1 + " Updated: customers can call the help line on weekdays.")
        report = cr.refresh_cited_documents(db._conn, store, limit=5, now=datetime.now())
        assert report.changed_urls == [URL] and report.records_rejudged == 1 and report.rows_changed == 0
        assert db._conn.execute("SELECT perplexity_enrichment_data FROM EnrichedEvents").fetchone()[0] == blob_before
        assert refreshed == [] and _victim_count(db, enriched_id) == 150_000

    def test_a_record_citing_a_page_that_was_never_cached_is_deferred_not_judged_on_a_missing_page(self, db, monkeypatch):
        monkeypatch.setattr("cyber_data_collector.dedup.record_fingerprints.refresh_changed_events",
                            lambda conn, dry_run=False: SimpleNamespace(refreshed_events=[]))
        enriched_id = _record(db, "ev1", [URL, OTHER_URL])
        site = FakeSite({URL: _page(URL, PAGE_V1), OTHER_URL: _page(OTHER_URL, PAGE_V1)})
        store = _judge_first(db, site)
        db._conn.execute("DELETE FROM CitedDocuments WHERE url = ?", (OTHER_URL,))
        db._conn.commit()
        _age(db)
        site.pages[URL] = _page(URL, PAGE_V2)
        site.requests.clear()
        report = cr.refresh_cited_documents(db._conn, store, limit=5, now=datetime.now())
        assert report.records_deferred == 1 and report.records_rejudged == 0
        assert [r[0] for r in site.requests] == [URL]                     # nothing fetched beyond the budget
        assert _victim_count(db, enriched_id) == 150_000

    def test_the_budget_bounds_the_pages_fetched(self, db):
        urls = [f"https://news.example.com.au/p{i}" for i in range(6)]
        for i, url in enumerate(urls):
            _record(db, f"ev{i}", [url])
        site = FakeSite({u: _page(u, PAGE_V1) for u in urls})
        store = _judge_first(db, site)
        _age(db)
        site.requests.clear()
        report = cr.refresh_cited_documents(db._conn, store, limit=4, now=datetime.now())
        assert len(site.requests) == 4 and len(report.outcomes) == 4
        assert len(report.plan.due) == 6 and len(report.plan.selected) == 4

    def test_a_dry_run_fetches_and_changes_nothing(self, db):
        _record(db, "ev1", [URL])
        site = FakeSite({URL: _page(URL, PAGE_V1)})
        store = _judge_first(db, site)
        _age(db)
        site.requests.clear()
        site.pages[URL] = _page(URL, PAGE_V2)
        report = cr.refresh_cited_documents(db._conn, store, limit=5, dry_run=True, now=datetime.now())
        assert site.requests == [] and report.outcomes == [] and len(report.plan.selected) == 1
        assert db._conn.execute("SELECT version FROM CitedDocuments").fetchone()[0] == 1

    def test_a_second_run_right_after_checks_nothing(self, db, monkeypatch):
        monkeypatch.setattr("cyber_data_collector.dedup.record_fingerprints.refresh_changed_events",
                            lambda conn, dry_run=False: SimpleNamespace(refreshed_events=[]))
        _record(db, "ev1", [URL])
        site = FakeSite({URL: _page(URL, PAGE_V1)})
        store = _judge_first(db, site)
        _age(db)
        site.pages[URL] = _page(URL, PAGE_V2)
        cr.refresh_cited_documents(db._conn, store, limit=5, now=datetime.now())
        site.requests.clear()
        again = cr.refresh_cited_documents(db._conn, store, limit=5, now=datetime.now())
        assert site.requests == [] and again.plan.selected == []


# ---------------------------------------------------------------------------
# The command and the refresh hygiene phase
# ---------------------------------------------------------------------------

def test_the_command_reports_what_is_due_and_a_dry_run_fetches_and_changes_nothing(tmp_path, capsys, monkeypatch):
    from scripts import dedup_admin

    path = tmp_path / "t.db"
    setup = sqlite3.connect(path)
    _plan_db(setup)
    _cite(setup, "r1", "e1", [URL])
    _doc(setup, URL, checked="2026-01-01T00:00:00")
    setup.commit()
    setup.close()
    monkeypatch.setattr(cd, "fetch_document", lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetched")))
    assert dedup_admin.main(["--db", str(path), "refresh-cited-documents", "--dry-run", "--verbose",
                             "--as-of", "2026-10-29"]) == 0
    out = capsys.readouterr().out
    assert "Due for a check: 1; budget 25 per run -> 1 selected" in out and URL in out and "DRY RUN" in out
    check = sqlite3.connect(path)
    assert check.execute("SELECT version, change_count FROM CitedDocuments").fetchone() == (1, 0)
    check.close()


def test_the_refresh_hygiene_phase_runs_it_with_a_small_bound_after_the_rescrape():
    source = (Path(__file__).resolve().parents[2] / "run_full_pipeline.py").read_text(encoding="utf-8")
    hygiene = source[source.index("def run_data_hygiene"):source.index("def run_recurrence_check")]
    assert 'getattr(args, \'cited_refresh_limit\', 25)' in hygiene
    assert '["rescrape-short-pages"], cited_refresh, ["publication-dates"]' in hygiene
    option = source[source.index("add_argument('--cited-refresh-limit'"):][:120]
    assert "type=int, default=25" in option
