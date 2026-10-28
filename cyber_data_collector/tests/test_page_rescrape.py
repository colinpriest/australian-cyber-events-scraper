"""Re-scraping short pages, and the scraper's no-silent-fallback rules (fetches mocked)."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from cyber_data_collector.processing import page_rescrape as pr
from cyber_data_collector.processing.record_evidence import RecordEvidence
from cyber_data_collector.storage.schema_init import initialize_database
from cyber_data_collector.utils import entity_scraper as es

ARTICLE = ("Acme Health confirmed on 12 March 2024 that a ransomware attack encrypted its patient "
           "systems and that the details of 5,000 patients were taken. ") * 4


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = tmp_path / "instance" / "t.db"
    initialize_database(str(path))
    c = sqlite3.connect(path)
    rows = [  # raw id, source, stored content, url, enriched status
        ("short", "GoogleSearch", "Cyber incident | Menu", "https://news.example/acme", "Active"),
        ("empty", "GoogleSearch", None, "https://news.example/dead", "Active"),
        ("social", "GoogleSearch", "", "https://www.facebook.com/acme/posts/1", "Active"),
        ("long", "GoogleSearch", ARTICLE, "https://news.example/long", "Active"),
        ("inactive", "GoogleSearch", "", "https://news.example/inactive", "Inactive"),
        ("perplexity", "Perplexity", "", "https://news.example/p", "Active"),
    ]
    for raw_id, source, content, url, status in rows:
        c.execute("INSERT INTO RawEvents (raw_event_id, source_type, raw_content, source_url, source_metadata) "
                  "VALUES (?, ?, ?, ?, ?)", (raw_id, source, content, url, json.dumps({"severity": "Medium"})))
        c.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, status, is_australian_event, "
                  "is_specific_event) VALUES (?, ?, 't', ?, 1, 1)", (f"e-{raw_id}", raw_id, status))
    c.commit()
    yield c
    c.close()


def fetcher(responses):
    calls = []

    async def fetch(url):
        calls.append(url)
        result = responses[url]
        if isinstance(result, Exception):
            raise result
        return result
    fetch.calls = calls
    return fetch


def _page(conn, raw_id):
    return conn.execute("SELECT raw_content, source_metadata FROM RawEvents WHERE raw_event_id = ?",
                        (raw_id,)).fetchone()


def _attempt(conn, raw_id):
    return conn.execute("SELECT attempts, status, last_error FROM PageRescrapes WHERE raw_event_id = ?",
                        (raw_id,)).fetchone()


def test_candidates_are_active_google_records_with_short_text(conn):
    assert {p["raw_event_id"] for p in pr.short_pages(conn)} == {"short", "empty", "social"}


def test_stores_real_longer_text_and_marks_its_origin(conn):
    fetch = fetcher({"https://news.example/acme": (ARTICLE, {"content_origin": "page"}),
                     "https://news.example/dead": (None, {"error": "HTTP 404"}),
                     "https://www.facebook.com/acme/posts/1": (None, {"error": es.BLOCKED_DOMAIN_ERROR})})
    report = pr.rescrape_short_pages(conn, fetch=fetch)
    assert [s["raw_event_id"] for s in report.stored] == ["short"]
    content, meta = _page(conn, "short")
    assert content == ARTICLE.strip()
    meta = json.loads(meta)
    assert meta["content_origin"] == "page" and meta["severity"] == "Medium"   # other metadata kept
    assert _attempt(conn, "short")[:2] == (0, pr.STATUS_STORED)
    assert _attempt(conn, "empty") == (1, pr.STATUS_RETRY, "HTTP 404")
    assert _attempt(conn, "social")[:2] == (1, pr.STATUS_GAVE_UP)               # never fetchable
    assert report.gave_up == 1
    # Stored and given-up pages are not candidates any more.
    assert {p["raw_event_id"] for p in pr.short_pages(conn)} == {"empty"}


def test_gives_up_after_three_failed_attempts(conn):
    fetch = fetcher({"https://news.example/acme": (None, {"error": "timed out"}),
                     "https://news.example/dead": (None, {"error": "HTTP 404"}),
                     "https://www.facebook.com/acme/posts/1": (None, {"error": es.BLOCKED_DOMAIN_ERROR})})
    for _ in range(pr.MAX_ATTEMPTS):
        pr.rescrape_short_pages(conn, fetch=fetch)
    assert _attempt(conn, "empty") == (3, pr.STATUS_GAVE_UP, "HTTP 404")
    assert pr.short_pages(conn) == []
    calls = len(fetch.calls)
    report = pr.rescrape_short_pages(conn, fetch=fetch)                       # idempotent: nothing left
    assert report.candidates == 0 and len(fetch.calls) == calls


@pytest.mark.parametrize("text, meta, reason", [
    ("Just a moment...\nChecking your browser before accessing news.example." + " " * 300, {}, "block page"),
    ("We use cookies to give you the best experience.\nAccept all cookies\nCookie settings\n" * 3, {}, "block page"),
    ("A short real line about Acme.", {}, "too short"),
    (ARTICLE, {"content_origin": es.ORIGIN_PERPLEXITY_ALTERNATIVE}, "another page"),
    (None, {"error": "HTTP 403"}, "HTTP 403"),
])
def test_unusable_text_is_a_recorded_failure(conn, text, meta, reason):
    fetch = fetcher({"https://news.example/acme": (text, meta),
                     "https://news.example/dead": (None, {}),
                     "https://www.facebook.com/acme/posts/1": (None, {})})
    pr.rescrape_short_pages(conn, fetch=fetch)
    assert _page(conn, "short")[0] == "Cyber incident | Menu"                 # unchanged
    attempts, status, error = _attempt(conn, "short")
    assert attempts == 1 and reason in error


def test_text_not_longer_than_stored_is_not_stored():
    problem = pr.assess("x " * 200, {}, stored_chars=1000)
    assert problem and "not longer" in problem


def test_a_fetch_exception_is_recorded_per_page(conn):
    fetch = fetcher({"https://news.example/acme": RuntimeError("boom"),
                     "https://news.example/dead": (None, {}),
                     "https://www.facebook.com/acme/posts/1": (None, {})})
    pr.rescrape_short_pages(conn, fetch=fetch)
    assert "RuntimeError: boom" in _attempt(conn, "short")[2]


def test_dry_run_fetches_nothing(conn):
    fetch = fetcher({})
    report = pr.rescrape_short_pages(conn, dry_run=True, fetch=fetch)
    assert report.candidates == 3 and fetch.calls == []
    assert conn.execute("SELECT COUNT(*) FROM PageRescrapes").fetchone()[0] == 0


def test_browser_that_cannot_start_fails_the_command(conn, monkeypatch, tmp_path, capsys):
    async def broken(self):
        raise RuntimeError("Executable doesn't exist")

    async def noop(self):
        return None

    monkeypatch.setattr(es.PlaywrightScraper, "__aenter__", broken)
    monkeypatch.setattr(es.PlaywrightScraper, "close", noop)
    with pytest.raises(pr.ScraperUnavailable, match="playwright install chromium"):
        pr.rescrape_short_pages(conn)
    assert conn.execute("SELECT COUNT(*) FROM PageRescrapes").fetchone()[0] == 0   # no page blamed

    from scripts import dedup_admin
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    assert dedup_admin.main(["--db", db_path, "rescrape-short-pages"]) == 2
    assert "could not run" in capsys.readouterr().err


# --- the scraper itself ------------------------------------------------------

def test_block_page_detection():
    assert es.looks_like_block_page("Access Denied\nYou don't have permission to access this server.")
    assert es.looks_like_block_page("")
    assert not es.looks_like_block_page(ARTICLE)
    # A long article that mentions cookies once is still an article.
    assert not es.looks_like_block_page(ARTICLE * 3 + "\nThis site uses cookies.")


def test_perplexity_fallback_is_off_by_default(monkeypatch):
    scraper = es.PlaywrightScraper()
    assert scraper.allow_perplexity_fallback is False

    def explode(*a, **k):
        raise AssertionError("Perplexity must not be called")
    monkeypatch.setattr(es, "openai", type("O", (), {"OpenAI": explode}))
    assert asyncio.run(scraper._perplexity_fallback("https://news.example/x", None, ["Acme"])) is None


def test_enabling_the_fallback_requires_its_key(monkeypatch):
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="PERPLEXITY_API_KEY"):
        es.PlaywrightScraper(allow_perplexity_fallback=True)


def test_enabled_fallback_needs_expected_terms(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "test")
    scraper = es.PlaywrightScraper(allow_perplexity_fallback=True)

    def explode(*a, **k):
        raise AssertionError("no search without something to verify against")
    monkeypatch.setattr(es, "openai", type("O", (), {"OpenAI": explode}))
    assert asyncio.run(scraper._perplexity_fallback("https://news.example/x", None, None)) is None


def test_enabled_fallback_verifies_identity_and_marks_the_text(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "test")
    scraper = es.PlaywrightScraper(allow_perplexity_fallback=True)

    class Client:
        def __init__(self, **kwargs):
            self.chat = self

        @property
        def completions(self):
            return self

        def create(self, **kwargs):
            content = "https://www.abc.net.au/news/other-story\nhttps://www.abc.net.au/news/acme-story"
            return type("R", (), {"choices": [type("C", (), {"message": type("M", (), {"content": content})})]})

    pages = {"https://www.abc.net.au/news/other-story": "Borealis Bank outage hits customers. " * 30,
             "https://www.abc.net.au/news/acme-story": ARTICLE * 2}

    async def simple(url):
        return pages[url]

    monkeypatch.setattr(es, "openai", type("O", (), {"OpenAI": Client}))
    monkeypatch.setattr(scraper, "_simple_url_test", simple)
    text = asyncio.run(scraper._perplexity_fallback("https://news.example/x", None, ["Acme Health Pty Ltd"]))
    assert isinstance(text, es.AlternativeSourceText)
    assert text.source_url == "https://www.abc.net.au/news/acme-story"   # the other story was refused
    assert es.content_origin_of(text) == {"content_origin": es.ORIGIN_PERPLEXITY_ALTERNATIVE,
                                          "content_source_url": "https://www.abc.net.au/news/acme-story"}
    assert not es.mentions_expected(pages["https://www.abc.net.au/news/other-story"], ["Acme Health"])


def test_evidence_labels_alternative_source_text():
    row = {"raw_event_id": "r", "raw_title": "Acme breach", "raw_content": ARTICLE,
           "source_url": "https://news.example/x",
           "source_metadata": json.dumps({"content_origin": es.ORIGIN_PERPLEXITY_ALTERNATIVE,
                                          "content_source_url": "https://www.abc.net.au/news/acme-story"})}
    evidence = RecordEvidence.from_row(row)
    assert evidence.is_alternative_source
    assert evidence.prompt_block().startswith("ALTERNATIVE-SOURCE TEXT")
    assert "abc.net.au" in evidence.reason
    plain = RecordEvidence.from_row({**row, "source_metadata": json.dumps({"content_origin": "page"})})
    assert not plain.is_alternative_source and plain.prompt_block().startswith("ARTICLE TEXT")


def test_webber_detail_failure_is_a_failure_not_perplexity_prose(monkeypatch):
    from cyber_data_collector.datasources import webber_insurance as wi
    from cyber_data_collector.models.config import DataSourceConfig
    from cyber_data_collector.utils.rate_limiter import RateLimiter

    source = wi.WebberInsuranceDataSource(DataSourceConfig(name="WebberInsurance"), RateLimiter(),
                                          {"PERPLEXITY_API_KEY": "test"})

    def refuse(*a, **k):
        raise ConnectionError("HTTP 503")
    monkeypatch.setattr(wi.requests, "get", refuse)
    assert source._scrape_detail_page("https://www.webberinsurance.com.au/x") is None
    assert source.failed_detail_pages == [{"url": "https://www.webberinsurance.com.au/x",
                                           "error": "ConnectionError: HTTP 503"}]
    assert not hasattr(source, "_perplexity_fallback")


def test_runs_as_a_pipeline_step_inside_the_pipelines_event_loop(tmp_path):
    """The 2026-10-28 refresh: run_full_pipeline runs under asyncio.run(), and the
    step failed with 'asyncio.run() cannot be called from a running event loop'."""
    import run_full_pipeline

    def step(argv):  # a script main that, like rescrape-short-pages, runs its own loop
        async def work():
            return len(argv)
        return asyncio.run(work())

    async def pipeline():
        with pytest.raises(RuntimeError, match="running event loop"):
            step(["--db", "x"])                                                  # the old call
        return run_full_pipeline.run_step(step, ["--db", "x"])

    assert asyncio.run(pipeline()) == 2
    assert run_full_pipeline.run_step(step, ["a"]) == 1                          # no loop: called directly
