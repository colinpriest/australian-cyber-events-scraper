"""Cyber Daily section feeds: parsing, month paging, failures, and the discovery path.

Every network call is a fake: the feed is generated in memory with the shape
the live site serves (13 items a page, newest first, a few items up to a week
out of order).
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
import requests

from cyber_data_collector.datasources.cyber_daily import (
    CyberDailyDataSource,
    CyberDailyFeedError,
    article_id,
    parse_feed,
)
from cyber_data_collector.datasources import cyber_daily
from cyber_data_collector.dedup import source_gate
from cyber_data_collector.filtering.page_gate import DiscoveryPageGate
from cyber_data_collector.models.config import DataSourceConfig, DateRange
from cyber_data_collector.models.events import EventSource
from cyber_data_collector.pipelines import discovery
from cyber_data_collector.storage.cyber_event_data_v2 import CyberEventDataV2
from cyber_data_collector.storage.schema_init import initialize_database
from cyber_data_collector.utils import RateLimiter

AEDT = timezone(timedelta(hours=11))
PAGE = 13


def _rss(items: List[Dict[str, Any]]) -> bytes:
    body = "".join(
        f"<item><title>{i['title']}</title><link>{i['url']}</link><guid>{i['url']}</guid>"
        f"<description><![CDATA[<p><img src=\"x.jpg\"></p><p>{i['summary']}</p>]]></description>"
        f"<category>Security</category><pubDate>{i['published'].strftime('%a, %d %b %Y %H:%M:%S %z')}</pubDate>"
        f"</item>" for i in items)
    return (f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>Security</title>'
            f"<link>https://www.cyberdaily.au/security</link>{body}</channel></rss>").encode()


def _item(n: int, published: datetime, title: str = "") -> Dict[str, Any]:
    return {"url": f"https://www.cyberdaily.au/security/{n}-article-{n}", "title": title or f"Article {n}",
            "summary": f"Standfirst of article {n}.", "published": published}


class FakeFeed:
    """A section feed of ``items`` (newest first), paged like the live site."""

    def __init__(self, items: List[Dict[str, Any]], fail_at: int = -1):
        self.items, self.fail_at, self.calls = items, fail_at, []

    def __call__(self, url: str, params: Dict[str, Any]) -> bytes:
        offset = int(params["limitstart"])
        self.calls.append(offset)
        if offset == self.fail_at:
            raise ConnectionError("connection reset")
        return _rss(self.items[offset:offset + PAGE])


def _two_years(newest: datetime) -> List[Dict[str, Any]]:
    """Four articles a day for two years, newest first, every 37th one a week late in the order."""
    items = [_item(100000 - k, newest - timedelta(hours=6 * k)) for k in range(4 * 730)]
    for k in range(36, len(items), 37):
        items[k] = {**items[k], "published": items[k]["published"] + timedelta(days=7)}
    return items


def _source(feed, **custom) -> CyberDailyDataSource:
    limiter = RateLimiter()
    limiter.set_limit("cyberdaily", per_minute=100000, per_second=100000)
    return CyberDailyDataSource(DataSourceConfig(name="CyberDaily", custom_config=custom), limiter, {}, fetch=feed)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parse_feed_reads_title_summary_date_and_url():
    when = datetime(2026, 10, 27, 15, 45, 53, tzinfo=AEDT)
    [item] = parse_feed(_rss([_item(14358, when, "Exclusive: Qilin claims quartet of Aussie victims")]), "security")
    assert item.url == "https://www.cyberdaily.au/security/14358-article-14358"
    assert item.title.startswith("Exclusive: Qilin")
    assert item.summary == "Standfirst of article 14358."          # HTML and image stripped
    assert item.published == when and item.published_on == date(2026, 10, 27)


@pytest.mark.parametrize("content", [
    b"<html><body>Not found</body></html>",
    b"<rss><channel><item><title>x</title></item></channel></rss>",                 # no link / pubDate
    b"<rss><channel><item><title>x</title><link>u</link><pubDate>someday</pubDate></item></channel></rss>",
    b"not xml at all <",
])
def test_malformed_feed_is_an_error_not_an_empty_month(content):
    with pytest.raises(CyberDailyFeedError):
        parse_feed(content, "security")


def test_article_id_ignores_host_scheme_and_slug():
    assert article_id("https://www.cyberdaily.au/security/11281-home-affairs-zircodata") == "11281"
    assert article_id("http://cyberdaily.au/security/11281-renamed-slug?utm=x") == "11281"
    assert article_id("https://www.cyberdaily.au/security") is None
    assert article_id(None) is None


# ---------------------------------------------------------------------------
# Month paging
# ---------------------------------------------------------------------------

def test_an_old_month_is_found_by_bisection_and_read_completely():
    items = _two_years(datetime(2026, 10, 27, 18, 0, tzinfo=AEDT))
    feed = FakeFeed(items)
    got = asyncio.run(_source(feed).items_between("security", date(2025, 3, 1), date(2025, 3, 31)))
    expected = {i["url"] for i in items if date(2025, 3, 1) <= i["published"].date() <= date(2025, 3, 31)}
    assert {i.url for i in got} == expected and len(expected) > 100
    # Out-of-order items a week late are still caught at both ends of the month.
    assert any(i["published"].date() == date(2025, 3, 31) for i in items if i["url"] in expected)
    # ~19 months back through 225 pages: bisection, not a walk from today.
    assert len(set(feed.calls)) < 40


def test_current_month_starts_at_the_first_page():
    items = _two_years(datetime(2026, 10, 27, 18, 0, tzinfo=AEDT))
    feed = FakeFeed(items)
    got = asyncio.run(_source(feed).items_between("security", date(2026, 10, 1), date(2026, 10, 31)))
    assert feed.calls == [PAGE * k for k in range(len(feed.calls))]      # a straight walk, no bisection
    assert {i.url for i in got} == {i["url"] for i in items if i["published"].date() >= date(2026, 10, 1)}


def test_a_failed_page_fails_the_source_month():
    items = _two_years(datetime(2026, 10, 27, 18, 0, tzinfo=AEDT))
    source = _source(FakeFeed(items, fail_at=PAGE * 2))
    events = asyncio.run(source.collect_events(DateRange(start_date=datetime(2026, 10, 1),
                                                         end_date=datetime(2026, 10, 31))))
    assert events == []
    assert source.collection_failures and "security feed" in source.collection_failures[0]


def test_an_empty_feed_is_a_failure():
    source = _source(FakeFeed([]))
    asyncio.run(source.collect_events(DateRange(start_date=datetime(2026, 10, 1), end_date=datetime(2026, 10, 31))))
    assert source.collection_failures


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def test_events_carry_publication_date_not_an_incident_date_and_skip_known_articles():
    newest = datetime(2026, 10, 27, 18, 0, tzinfo=AEDT)
    items = [_item(14358, newest, "Exclusive: Qilin claims hack of Mount Barker Co-operative"),
             _item(14357, newest - timedelta(hours=3), "Analysis: Why the F5 breach is something to worry about"),
             _item(11281, newest - timedelta(hours=5), "Home Affairs personal data compromised")]
    source = _source(FakeFeed(items))
    source.known_urls = {"https://cyberdaily.au/security/11281-old-slug"}   # stored by Google, other slug
    events = asyncio.run(source.collect_events(DateRange(start_date=datetime(2026, 10, 1),
                                                         end_date=datetime(2026, 10, 31))))
    assert [e.title[:20] for e in events] == ["Analysis: Why the F5", "Exclusive: Qilin cla"]
    assert source.skipped_known == 1 and not source.collection_failures
    qilin = events[1]
    assert qilin.event_date is None
    [src] = qilin.data_sources
    assert src.source_type == "Cyber Daily" and src.publication_date == datetime(2026, 10, 27, 18, 0)
    assert src.source_id == "cyberdaily_14358" and qilin.description == "Standfirst of article 14358."
    assert not events[0].australian_relevance and events[1].australian_relevance is False
    aussie = CyberDailyDataSource._to_event(parse_feed(_rss([_item(1, newest, "Aussie retailer hacked")]),
                                                       "security")[0])
    assert aussie.australian_relevance


# ---------------------------------------------------------------------------
# Discovery path: storage, retry queue, page gate, backfill
# ---------------------------------------------------------------------------

class FakeScraper:
    """The Playwright scraper; ``pages`` overrides the text per URL, and every fetch is counted."""

    pages: Dict[str, str] = {}
    fetched: List[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def close(self):
        return None

    async def get_page_text(self, url, event_date=None, return_metadata=False, timeout=None):
        FakeScraper.fetched.append(url)
        text = FakeScraper.pages.get(
            url, "Qilin listed Mount Barker Co-operative on its leak site and claims to have stolen data. " * 6)
        meta = {"perplexity_attempted": False, "perplexity_succeeded": False, "content_origin": "page"}
        return (text, meta) if return_metadata else text


class FakeJudge:
    def ask(self, subject_type, subject_id, question_version, state, questions):
        return {qid: SimpleNamespace(value=0.9) for qid in questions}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    now = datetime.now(AEDT).replace(hour=12, minute=0, second=0, microsecond=0)
    feed = FakeFeed([_item(20000 - k, now - timedelta(hours=6 * k), f"Exclusive: Victim {k} listed by Qilin")
                     for k in range(800)])

    class Collector:
        def __init__(self, config, env_path=".env", db_path=None):
            self.data_sources = {"cyberdaily": _source(feed)} if config.cyberdaily_config.enabled else {}

        async def _process_events(self, events):
            return [e.model_copy(update={"classification_status": "classified"}) for e in events]

    monkeypatch.setattr(discovery, "CyberDataCollector", Collector)
    monkeypatch.setattr(discovery, "PlaywrightScraper", FakeScraper)
    from cyber_data_collector.utils import entity_scraper
    monkeypatch.setattr(entity_scraper, "PlaywrightScraper", FakeScraper)       # the re-fetch's scraper
    FakeScraper.pages, FakeScraper.fetched = {}, []
    db_path = tmp_path / "instance" / "t.db"
    initialize_database(str(db_path))
    cal = tmp_path / "cal.json"
    cal.write_text(__import__("json").dumps({**source_gate.calibration_identity(), "cutoff": 0.58}))

    def make():
        gate = DiscoveryPageGate(None, judge=FakeJudge(), mode="enforce", calibration_path=cal,
                                 evaluation_path=tmp_path / "none.json", sleep=lambda s: None)
        return discovery.EventDiscoveryEnrichmentPipeline(str(db_path), page_gate=gate)

    return SimpleNamespace(path=db_path, make=make, feed=feed, now=now)


def _rows(db_path, sql, *args):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def test_discovery_stores_cyber_daily_records_through_the_normal_path(env):
    # One article is already stored by Google: it is not stored again.
    known = env.feed.items[0]["url"]
    db = CyberEventDataV2(str(env.path))
    db.add_raw_event("GoogleSearch", {"title": "Victim 0 - Cyber Daily", "source_url": known,
                                      "event_date": env.now.date().isoformat()})
    db.close()

    pipeline = env.make()
    asyncio.run(pipeline.discover_events(source_types=["CyberDaily"], date_range_days=1,
                                         cyberdaily_refetch=False))
    pipeline.close()

    stored = _rows(env.path, "SELECT source_type, source_url, raw_description, raw_content, is_processed "
                             "FROM RawEvents WHERE source_type = 'CyberDaily'")
    assert stored and all(r[2].startswith("Standfirst of article") for r in stored)
    assert all(r[3] and "Mount Barker" in r[3] and r[4] for r in stored)          # scraped, processed
    assert known not in {r[1] for r in stored}
    statuses = _rows(env.path, "SELECT DISTINCT status FROM SourceMonthDiscovery WHERE source_type = 'CyberDaily'")
    assert statuses == [("complete",)]
    # The page gate scored the pages and, outside Google, keeps them.
    assert _rows(env.path, "SELECT COUNT(*) FROM EnrichedEvents e JOIN RawEvents r USING (raw_event_id) "
                           "WHERE r.source_type = 'CyberDaily'")[0][0] == len(stored)


def test_backfill_searches_processed_months_once_and_leaves_month_totals_alone(env):
    first = (env.now.replace(day=1) - timedelta(days=100))
    month = (first.year, first.month)
    db = CyberEventDataV2(str(env.path))
    db.mark_month_as_processed(*month, total_raw_events=42, processing_notes="Processed with sources: Perplexity")
    db.close()
    unprocessed = ((first - timedelta(days=40)).year, (first - timedelta(days=40)).month)

    pipeline = env.make()
    result = asyncio.run(pipeline.backfill_source("CyberDaily", [unprocessed, month]))
    assert result["searched"] == [month] and result["not_processed"] == [unprocessed] and not result["failed"]
    assert _rows(env.path, "SELECT total_raw_events, processing_notes FROM MonthProcessed WHERE year = ? AND "
                           "month = ?", *month) == [(42, "Processed with sources: Perplexity")]
    assert not _rows(env.path, "SELECT 1 FROM MonthProcessed WHERE year = ? AND month = ?", *unprocessed)
    stored = _rows(env.path, "SELECT COUNT(*) FROM RawEvents WHERE source_type = 'CyberDaily'")[0][0]
    assert stored > 0

    calls = len(env.feed.calls)
    again = asyncio.run(pipeline.backfill_source("CyberDaily", [month]))
    pipeline.close()
    assert again["already_done"] == [month] and len(env.feed.calls) == calls


def test_cyber_daily_records_get_their_own_source_type():
    pipeline = discovery.EventDiscoveryEnrichmentPipeline.__new__(discovery.EventDiscoveryEnrichmentPipeline)
    cd = EventSource(source_id="cyberdaily_1", source_type="Cyber Daily",
                     url="https://www.cyberdaily.au/security/1-x", credibility_score=0.7, relevance_score=0.6)
    google = cd.model_copy(update={"source_type": "Google Search"})
    assert pipeline._determine_source_type([cd]) == "CyberDaily"
    # Another source's record of a Cyber Daily article stays that source's.
    assert pipeline._determine_source_type([google]) == "GoogleSearch"


# ---------------------------------------------------------------------------
# Deep backfill and re-fetching stored stories
# ---------------------------------------------------------------------------

def _months_back(now: datetime, n: int):
    year, month = now.year, now.month
    for _ in range(n):
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return year, month


def test_first_run_backfills_processed_history_once_and_later_runs_do_not(env):
    old = _months_back(env.now, 5)            # processed before Cyber Daily existed
    older = _months_back(env.now, 6)          # never processed: left to normal discovery
    db = CyberEventDataV2(str(env.path))
    db.mark_month_as_processed(*old, total_raw_events=7, processing_notes="Processed with sources: Perplexity")
    db.close()

    pipeline = env.make()
    asyncio.run(pipeline.discover_events(source_types=["CyberDaily"], date_range_days=1,
                                         cyberdaily_backfill_since=older, cyberdaily_refetch=False))
    pipeline.close()
    done = set(_rows(env.path, "SELECT year, month FROM SourceMonthDiscovery WHERE source_type = 'CyberDaily' "
                               "AND status = 'complete'"))
    assert old in done and older not in done
    assert pipeline.stats["cyberdaily_backfill_months"] == 1
    assert _rows(env.path, "SELECT total_raw_events FROM MonthProcessed WHERE year = ? AND month = ?", *old) == [(7,)]

    calls = len(env.feed.calls)
    again = env.make()
    asyncio.run(again.discover_events(source_types=["CyberDaily"], date_range_days=1,
                                      cyberdaily_backfill_since=older, cyberdaily_refetch=False))
    again.close()
    assert again.stats.get("cyberdaily_backfill_months") == 0
    # Only the rolling window was read again, not the backfilled month.
    assert len(env.feed.calls) - calls < calls


def test_backfill_force_searches_a_completed_month_again(env):
    month = _months_back(env.now, 4)
    db = CyberEventDataV2(str(env.path))
    db.mark_month_as_processed(*month)
    db.close()
    pipeline = env.make()
    assert asyncio.run(pipeline.backfill_source("CyberDaily", [month]))["searched"] == [month]
    assert asyncio.run(pipeline.backfill_source("CyberDaily", [month]))["already_done"] == [month]
    assert asyncio.run(pipeline.backfill_source("CyberDaily", [month], force=True))["searched"] == [month]
    pipeline.close()
    # Re-searching found the same articles: still one record each.
    urls = [r[0] for r in _rows(env.path, "SELECT source_url FROM RawEvents WHERE source_type = 'CyberDaily'")]
    assert urls and len(urls) == len(set(urls))


ARTICLE = ("Exclusive: Sydney-based visualisation firm Virtual Ideas hit by Lamashtu ransomware attack\n"
           "Ransomware group Lamashtu claims Australian victim; hackers publish 35 gigabytes of data.\n"
           + "A Surry Hills-based company is responding to a cyber attack by the Lamashtu ransomware operation. " * 5)
CUT_OFF = (ARTICLE[:400] + "\nYou’re out of free articles for this month\nLog in\n"
           "To continue reading the rest of this article, please log in.")


def _stored_story(db_path, url, content, published, validated=1, source="GoogleSearch"):
    db = CyberEventDataV2(str(db_path))
    raw_id = db.add_raw_event(source, {"title": "Virtual Ideas", "source_url": url, "event_date": published})
    db.close()
    conn = sqlite3.connect(db_path)
    stored_at = (datetime.now() - timedelta(days=2)).isoformat()         # stored by an earlier run
    conn.execute("UPDATE RawEvents SET raw_content = ?, discovered_at = ? WHERE raw_event_id = ?",
                 (content, stored_at, raw_id))
    conn.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, status, is_australian_event, "
                 "is_specific_event, perplexity_validated, perplexity_attempt_count, perplexity_enrichment_data) "
                 "VALUES (?, ?, 'Virtual Ideas', 'Active', 1, 1, ?, 2, ?)",
                 (f"enr-{raw_id}", raw_id, validated, '{"threat_actor": "Lamashtu"}'))
    conn.commit()
    conn.close()
    return raw_id


def test_refetch_stores_changed_text_requeues_enrichment_and_is_idempotent(env):
    from cyber_data_collector.processing import cyberdaily_refetch as cr

    url = "https://www.cyberdaily.au/security/14337-virtual-ideas"
    raw_id = _stored_story(env.path, url, CUT_OFF, env.now.date().isoformat())
    FakeScraper.pages = {url: "You have 4 free articles left this month.\n" + ARTICLE}

    conn = sqlite3.connect(env.path)
    report = cr.refetch_stories(conn, days=90, delay_seconds=0)
    assert report.outcomes[cr.UPDATED] == 1 and report.requeued_enrichments == 1
    content, = conn.execute("SELECT raw_content FROM RawEvents WHERE raw_event_id = ?", (raw_id,)).fetchone()
    assert "Surry Hills" in content and not cr.is_cut_off(content)
    validated, attempts, kept = conn.execute(
        "SELECT perplexity_validated, perplexity_attempt_count, perplexity_enrichment_data FROM EnrichedEvents "
        "WHERE raw_event_id = ?", (raw_id,)).fetchone()
    assert validated == 0 and attempts == 0 and "Lamashtu" in kept      # queued again, stored facts kept

    # Fetched again within a day: left alone.
    assert cr.refetch_stories(conn, days=90, delay_seconds=0).candidates == 0

    # A day later, the same article with a different banner count: unchanged, nothing written or queued.
    conn.execute("UPDATE EnrichedEvents SET perplexity_validated = 1")
    conn.execute("UPDATE CyberDailyRefetches SET fetched_at = ?", ((datetime.now() - timedelta(days=1, hours=1)).isoformat(),))
    conn.commit()
    FakeScraper.pages = {url: "You have 2 free articles left this month.\n" + ARTICLE}
    again = cr.refetch_stories(conn, days=90, delay_seconds=0)
    assert again.outcomes[cr.UNCHANGED] == 1 and again.requeued_enrichments == 0
    assert conn.execute("SELECT perplexity_validated FROM EnrichedEvents").fetchone()[0] == 1
    assert conn.execute("SELECT raw_content FROM RawEvents WHERE raw_event_id = ?", (raw_id,)).fetchone()[0] == content
    conn.close()


def test_refetch_never_replaces_a_full_article_with_a_cut_off_page(env):
    from cyber_data_collector.processing import cyberdaily_refetch as cr

    url = "https://www.cyberdaily.au/security/14337-virtual-ideas"
    raw_id = _stored_story(env.path, url, ARTICLE, env.now.date().isoformat())
    FakeScraper.pages = {url: CUT_OFF}
    conn = sqlite3.connect(env.path)
    report = cr.refetch_stories(conn, days=90, delay_seconds=0)
    assert report.outcomes[cr.KEPT_STORED] == 1 and report.requeued_enrichments == 0
    assert conn.execute("SELECT raw_content FROM RawEvents WHERE raw_event_id = ?", (raw_id,)).fetchone()[0] == ARTICLE
    conn.close()


def test_refetch_window_takes_recent_and_cut_off_stories_and_all_takes_everything(env):
    from cyber_data_collector.processing import cyberdaily_refetch as cr

    old = (env.now - timedelta(days=400)).date().isoformat()
    new = env.now.date().isoformat()
    _stored_story(env.path, "https://www.cyberdaily.au/security/1-old-full", ARTICLE, old)
    _stored_story(env.path, "https://www.cyberdaily.au/security/2-old-cut-off", CUT_OFF, old)
    _stored_story(env.path, "https://www.cyberdaily.au/security/3-new", ARTICLE, new)
    _stored_story(env.path, "https://www.cyberdaily.au/security", ARTICLE, new)      # an index page
    conn = sqlite3.connect(env.path)
    conn.execute("UPDATE RawEvents SET discovered_at = ?", (old,))
    conn.commit()
    windowed = {s["url"].rsplit("/", 1)[1] for s in cr.stored_stories(conn, days=90)}
    everything = {s["url"].rsplit("/", 1)[1] for s in cr.stored_stories(conn, days=None)}
    conn.close()
    assert windowed == {"2-old-cut-off", "3-new"}
    assert everything == {"1-old-full", "2-old-cut-off", "3-new"}


@pytest.mark.parametrize("text,origin,outcome", [
    (None, None, "failed"),
    ("short", None, "failed"),
    (ARTICLE, "alternative", "failed"),
    (ARTICLE + " Updated: the company has notified the OAIC.", None, "updated"),
])
def test_refetch_decisions(text, origin, outcome):
    from cyber_data_collector.processing import cyberdaily_refetch as cr
    from cyber_data_collector.utils.entity_scraper import ORIGIN_PERPLEXITY_ALTERNATIVE

    meta = {"content_origin": ORIGIN_PERPLEXITY_ALTERNATIVE} if origin else {"error": "timeout"}
    assert cr.decide(ARTICLE, text, meta)[0] == outcome


def test_refetch_runs_inside_discovery_for_cyber_daily(env):
    url = "https://www.cyberdaily.au/security/14337-virtual-ideas"
    _stored_story(env.path, url, CUT_OFF, env.now.date().isoformat(), source="WebberInsurance")
    FakeScraper.pages = {url: ARTICLE}
    pipeline = env.make()
    asyncio.run(pipeline.discover_events(source_types=["CyberDaily"], date_range_days=1,
                                         cyberdaily_backfill_since=None))
    pipeline.close()
    assert url in FakeScraper.fetched
    assert _rows(env.path, "SELECT status FROM CyberDailyRefetches WHERE url = ?", url) == [("updated",)]


class _Response:
    def __init__(self, status, headers=None, content=b"<rss/>"):
        self.status_code, self.headers, self.content = status, headers or {}, content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error")


def test_throttled_feed_pages_are_waited_out_and_asked_again(monkeypatch):
    """2026-10-28: single 429s deep in the feed lost 2021-03 and 2021-05."""
    answers = [_Response(429, {"Retry-After": "40"}), _Response(503), _Response(200, content=b"ok")]
    monkeypatch.setattr(cyber_daily.requests, "get", lambda *a, **k: answers.pop(0))
    waits = []
    source = _source(FakeFeed([]))
    assert source._http_get("https://www.cyberdaily.au/security", {"limitstart": 4836}, sleep=waits.append) == b"ok"
    assert waits == [40, cyber_daily.THROTTLE_WAITS[1]]          # Retry-After wins over a shorter wait


def test_a_feed_page_still_throttled_after_every_wait_fails(monkeypatch):
    monkeypatch.setattr(cyber_daily.requests, "get", lambda *a, **k: _Response(429))
    waits = []
    with pytest.raises(requests.HTTPError):
        _source(FakeFeed([]))._http_get("https://www.cyberdaily.au/security", {"limitstart": 0}, sleep=waits.append)
    assert waits == list(cyber_daily.THROTTLE_WAITS)


def test_other_http_errors_are_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(cyber_daily.requests, "get", lambda *a, **k: calls.append(1) or _Response(404))
    with pytest.raises(requests.HTTPError):
        _source(FakeFeed([]))._http_get("https://www.cyberdaily.au/security", {}, sleep=lambda s: None)
    assert len(calls) == 1
