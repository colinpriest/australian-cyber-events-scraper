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

from cyber_data_collector.datasources.cyber_daily import (
    CyberDailyDataSource,
    CyberDailyFeedError,
    article_id,
    parse_feed,
)
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
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_page_text(self, url, event_date=None, return_metadata=False):
        text = "Qilin listed Mount Barker Co-operative on its leak site and claims to have stolen data. " * 6
        meta = {"perplexity_attempted": False, "perplexity_succeeded": False}
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
    asyncio.run(pipeline.discover_events(source_types=["CyberDaily"], date_range_days=1))
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
