"""Cyber Daily monitoring and self-healing: error types, automatic retry, coverage metrics.

Every fetch is a fake. Each error type is taken through the place it is
stored (``SourceMonthDiscovery``, ``DiscoveryRecordState``,
``CyberDailyRefetches``) and the retry decision that follows from it.
"""
from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

import pytest
import requests

from cyber_data_collector.datasources.cyber_daily import CyberDailyFeedError
from cyber_data_collector.models.config import DateRange
from cyber_data_collector.processing import cyberdaily_refetch as cr
from cyber_data_collector.processing import fetch_errors as fe
from cyber_data_collector.processing.retry_queue import (
    MAX_SOURCE_MONTH_ATTEMPTS,
    MONTH_COMPLETE,
    MONTH_DUE,
    MONTH_GAVE_UP,
    MONTH_NEVER,
    MONTH_WAITING,
    RetryQueue,
    ensure_schema,
)
from cyber_data_collector.quality import metrics as qm
from cyber_data_collector.quality.gate import render_markdown, run_quality_checks
from cyber_data_collector.quality.model import FAIL, PASS, WARN, evaluate, load_thresholds
from cyber_data_collector.storage.cyber_event_data_v2 import CyberEventDataV2
from cyber_data_collector.storage.schema_init import initialize_database
from cyber_data_collector.tests.test_cyber_daily_source import (  # noqa: F401 - env is a fixture
    AEDT,
    ARTICLE,
    CUT_OFF,
    PAGE,
    FakeFeed,
    _item,
    _months_back,
    _rows,
    _rss,
    _source,
    _stored_story,
    env,
)


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} Error", response=response)


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exc,expected", [
    (_http_error(429), fe.THROTTLED),
    (_http_error(503), fe.THROTTLED),
    (_http_error(404), fe.MISSING),
    (_http_error(410), fe.MISSING),
    (_http_error(500), fe.NETWORK),
    (_http_error(403), fe.OTHER),
    (requests.HTTPError("429 Client Error: Too Many Requests for url: x"), fe.THROTTLED),   # no response object
    (requests.ConnectionError("reset"), fe.NETWORK),
    (requests.Timeout("read timed out"), fe.NETWORK),
    (TimeoutError(), fe.NETWORK),
    (ET.ParseError("junk"), fe.PARSE),
    (CyberDailyFeedError("not an RSS feed"), fe.PARSE),
    (ValueError("something odd"), fe.OTHER),
])
def test_exceptions_are_classified(exc, expected):
    assert fe.classify_error(exc) == expected


def test_classification_follows_the_cause_chain():
    try:
        try:
            raise _http_error(404)
        except requests.HTTPError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert fe.classify_error(outer) == fe.MISSING


@pytest.mark.parametrize("text,expected", [
    ("Cyber Daily security feed for 2021-03-01..2021-03-31 incomplete: security feed at offset 4836 could not "
     "be fetched: 429 Client Error: Too Many Requests for url: https://www.cyberdaily.au/security?limitstart=4836",
     fe.THROTTLED),                                     # the message stored on 2026-10-28
    ("HTTP 404", fe.MISSING),
    ("HTTP 410", fe.MISSING),
    ("timed out after 3 attempts", fe.NETWORK),
    ("page text too short (12 chars < 300)", fe.INCOMPLETE),
    ("Insufficient content retrieved", fe.INCOMPLETE),
    ("security feed returned no items", fe.PARSE),
    ("Google daily quota exceeded; remaining queries not run", fe.THROTTLED),
    ("something nobody anticipated", fe.OTHER),
    ("", fe.OTHER),
])
def test_stored_messages_are_classified(text, expected):
    assert fe.error_type_from_text(text) == expected


def test_a_permanent_failure_decides_a_source_month():
    assert fe.deciding_type([fe.THROTTLED, fe.MISSING, fe.THROTTLED]) == fe.MISSING
    assert fe.deciding_type([fe.NETWORK, fe.THROTTLED, fe.THROTTLED]) == fe.THROTTLED
    assert fe.deciding_type([]) is None
    assert fe.is_permanent(fe.MISSING) and not any(fe.is_permanent(t) for t in fe.TEMPORARY_TYPES)


# ---------------------------------------------------------------------------
# The source keeps the type of every failure
# ---------------------------------------------------------------------------

def _failing_feed(items, exc_or_content):
    """A feed whose first page answers and every later page fails (raises or returns ``content``)."""
    def fetch(url, params):
        offset = int(params["limitstart"])
        if offset == 0:
            return _rss(items[:PAGE])
        if isinstance(exc_or_content, BaseException):
            raise exc_or_content
        return exc_or_content
    return fetch


@pytest.mark.parametrize("failure,expected", [
    (_http_error(429), fe.THROTTLED),
    (_http_error(404), fe.MISSING),
    (requests.ConnectionError("connection reset"), fe.NETWORK),
    (b"<html><body>Just a moment...</body></html>", fe.PARSE),        # a challenge page, not the feed
])
def test_feed_failures_keep_their_type(failure, expected):
    newest = datetime(2026, 10, 27, 18, 0, tzinfo=AEDT)
    items = [_item(30000 - k, newest - timedelta(hours=6 * k)) for k in range(400)]
    source = _source(_failing_feed(items, failure))
    events = asyncio.run(source.collect_events(DateRange(start_date=datetime(2026, 6, 1),
                                                         end_date=datetime(2026, 6, 30))))
    assert events == [] and source.items_seen is None
    assert source.collection_error_types == [expected]
    assert len(source.collection_failures) == 1


def test_a_complete_collection_counts_the_articles_seen_including_known_ones():
    newest = datetime(2026, 10, 27, 18, 0, tzinfo=AEDT)
    items = [_item(14358, newest), _item(14357, newest - timedelta(hours=3)),
             _item(11281, newest - timedelta(hours=5))]
    source = _source(FakeFeed(items))
    source.known_urls = {"https://cyberdaily.au/security/11281-old-slug"}
    events = asyncio.run(source.collect_events(DateRange(start_date=datetime(2026, 10, 1),
                                                         end_date=datetime(2026, 10, 31))))
    assert len(events) == 2 and source.items_seen == 3 and source.collection_error_types == []


# ---------------------------------------------------------------------------
# Source-months: back-off, cap, permanent failures, release
# ---------------------------------------------------------------------------

def _queue(clock):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_type TEXT, event_date TEXT, "
                 "discovered_at TEXT, is_processed INTEGER, processing_error TEXT, processing_attempted_at TEXT, "
                 "raw_title TEXT, raw_description TEXT, raw_content TEXT, source_url TEXT, source_metadata TEXT)")
    return conn, RetryQueue(conn, clock=lambda: clock[0])


def test_a_temporary_failure_is_backed_off_then_searched_again_and_given_up_at_the_cap():
    now = [datetime(2026, 10, 28, 9, 0)]
    _, queue = _queue(now)
    assert queue.source_month_retry("CyberDaily", 2021, 3) == MONTH_NEVER
    throttled = ["feed at offset 4836 could not be fetched: 429 Client Error"]
    for attempt in range(1, MAX_SOURCE_MONTH_ATTEMPTS):
        assert queue.record_discovery("CyberDaily", 2021, 3, 0, throttled, error_types=[fe.THROTTLED]) == "failed"
        assert queue.source_month_retry("CyberDaily", 2021, 3) == MONTH_WAITING
        [row] = queue.failed_discoveries()
        assert row["error_type"] == fe.THROTTLED and row["failed_attempts"] == attempt and not row["gave_up_at"]
        now[0] = datetime.fromisoformat(row["next_attempt_at"]) + timedelta(seconds=1)
        assert queue.source_month_retry("CyberDaily", 2021, 3) == MONTH_DUE
    queue.record_discovery("CyberDaily", 2021, 3, 0, throttled)          # type classified from the message
    [row] = queue.failed_discoveries()
    assert row["gave_up_at"] and row["failed_attempts"] == MAX_SOURCE_MONTH_ATTEMPTS
    now[0] += timedelta(days=30)
    assert queue.source_month_retry("CyberDaily", 2021, 3) == MONTH_GAVE_UP

    assert queue.release_source_months("CyberDaily") == 1
    assert queue.source_month_retry("CyberDaily", 2021, 3) == MONTH_DUE
    queue.record_discovery("CyberDaily", 2021, 3, 40, [], items_seen=52)
    assert queue.source_month_retry("CyberDaily", 2021, 3) == MONTH_COMPLETE
    assert queue.failed_discoveries() == []


def test_back_off_doubles_up_to_a_day():
    now = [datetime(2026, 10, 28, 9, 0)]
    _, queue = _queue(now)
    gaps = []
    for _ in range(4):
        queue.record_discovery("CyberDaily", 2021, 5, 0, ["timed out"], error_types=[fe.NETWORK])
        gaps.append(datetime.fromisoformat(queue.failed_discoveries()[0]["next_attempt_at"]) - now[0])
    assert gaps == [timedelta(minutes=30), timedelta(hours=1), timedelta(hours=2), timedelta(hours=4)]


def test_a_missing_feed_is_given_up_at_once_and_never_searched_again():
    now = [datetime(2026, 10, 28, 9, 0)]
    _, queue = _queue(now)
    queue.record_discovery("CyberDaily", 2020, 2, 0, ["throttled once", "HTTP 404"],
                           error_types=[fe.THROTTLED, fe.MISSING])
    [row] = queue.failed_discoveries()
    assert row["error_type"] == fe.MISSING and row["gave_up_at"] and row["next_attempt_at"] is None
    assert json.loads(row["error_types_json"]) == [fe.THROTTLED, fe.MISSING]
    assert queue.source_month_retry("CyberDaily", 2020, 2) == MONTH_GAVE_UP


def test_a_complete_search_keeps_items_seen_and_a_failed_one_does_not_overwrite_it():
    now = [datetime(2026, 10, 28, 9, 0)]
    conn, queue = _queue(now)
    queue.record_discovery("CyberDaily", 2024, 7, 10, [], items_seen=83)
    queue.record_discovery("CyberDaily", 2024, 7, 0, ["timed out"], items_seen=4)
    assert conn.execute("SELECT items_seen FROM SourceMonthDiscovery").fetchone()[0] == 83


def test_legacy_failed_months_get_a_type_from_their_message():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_type TEXT, event_date TEXT, "
                 "discovered_at TEXT, is_processed INTEGER, processing_error TEXT)")
    conn.execute("""CREATE TABLE SourceMonthDiscovery (source_type TEXT NOT NULL, year INTEGER NOT NULL,
                    month INTEGER NOT NULL, status TEXT NOT NULL, events_found INTEGER NOT NULL DEFAULT 0,
                    failures_json TEXT, attempted_at TEXT NOT NULL, completed_at TEXT,
                    PRIMARY KEY (source_type, year, month))""")
    conn.execute("INSERT INTO SourceMonthDiscovery VALUES ('CyberDaily', 2021, 3, 'failed', 0, ?, '2026-10-28', NULL)",
                 (json.dumps(["... offset 4836 could not be fetched: 429 Client Error: Too Many Requests"]),))
    ensure_schema(conn)
    ensure_schema(conn)                                                   # idempotent
    row = conn.execute("SELECT error_type, failed_attempts, gave_up_at FROM SourceMonthDiscovery").fetchone()
    assert row == (fe.THROTTLED, 1, None)
    assert RetryQueue(conn).source_month_retry("CyberDaily", 2021, 3) == MONTH_DUE


def test_record_failures_keep_their_type():
    now = [datetime(2026, 10, 28, 9, 0)]
    conn, queue = _queue(now)
    conn.execute("INSERT INTO RawEvents (raw_event_id, source_type, is_processed) VALUES ('r1', 'CyberDaily', 0)")
    queue.record_failure("r1", "classification", "RateLimitError: 429 Too Many Requests", "temporary")
    queue.record_failure("r1", "classification", "anything", "temporary", error_type=fe.NETWORK)
    assert conn.execute("SELECT last_error_type FROM DiscoveryRecordState").fetchone()[0] == fe.NETWORK
    queue.record_failure("r1", "classification", "HTTP 429", "temporary")
    assert conn.execute("SELECT last_error_type FROM DiscoveryRecordState").fetchone()[0] == fe.THROTTLED


# ---------------------------------------------------------------------------
# The backfill follows the retry state
# ---------------------------------------------------------------------------

class SwitchFeed:
    """``env.feed`` with a switch: while ``failure`` is set, every page after the first fails."""

    def __init__(self, feed):
        self.feed, self.failure, self.calls = feed, None, 0

    def __call__(self, url, params):
        self.calls += 1
        if self.failure is not None and int(params["limitstart"]) > 0:
            raise self.failure
        return self.feed(url, params)


@pytest.fixture
def switch(env, monkeypatch):
    from cyber_data_collector.pipelines import discovery

    feed = SwitchFeed(env.feed)

    class Collector:
        def __init__(self, config, env_path=".env", db_path=None):
            self.data_sources = {"cyberdaily": _source(feed)} if config.cyberdaily_config.enabled else {}

        async def _process_events(self, events):
            return [e.model_copy(update={"classification_status": "classified"}) for e in events]

    monkeypatch.setattr(discovery, "CyberDataCollector", Collector)
    return feed


def _processed_month(env, back):
    month = _months_back(env.now, back)
    db = CyberEventDataV2(str(env.path))
    db.mark_month_as_processed(*month)
    db.close()
    return month


def test_backfill_retries_a_throttled_month_after_its_back_off(env, switch):
    month = _processed_month(env, 5)
    pipeline = env.make()
    switch.failure = _http_error(429)
    assert asyncio.run(pipeline.backfill_source("CyberDaily", [month]))["failed"] == [month]
    [row] = pipeline.retry_queue.failed_discoveries()
    assert row["error_type"] == fe.THROTTLED and row["failed_attempts"] == 1

    calls = switch.calls
    assert asyncio.run(pipeline.backfill_source("CyberDaily", [month]))["waiting"] == [month]
    assert switch.calls == calls                                          # not searched inside the back-off

    switch.failure = None
    later = datetime.fromisoformat(row["next_attempt_at"]) + timedelta(minutes=1)
    pipeline.retry_queue.clock = lambda: later
    assert asyncio.run(pipeline.backfill_source("CyberDaily", [month]))["searched"] == [month]
    pipeline.close()
    seen = _rows(env.path, "SELECT status, items_seen, failed_attempts FROM SourceMonthDiscovery WHERE year = ? "
                           "AND month = ?", *month)
    assert seen[0][0] == "complete" and seen[0][1] > 0 and seen[0][2] == 0


def test_backfill_gives_up_a_missing_month_and_release_brings_it_back(env, switch):
    month = _processed_month(env, 6)
    pipeline = env.make()
    switch.failure = _http_error(404)
    assert asyncio.run(pipeline.backfill_source("CyberDaily", [month]))["failed"] == [month]
    calls = switch.calls
    later = datetime.now() + timedelta(days=60)
    pipeline.retry_queue.clock = lambda: later
    assert asyncio.run(pipeline.backfill_source("CyberDaily", [month]))["gave_up"] == [month]
    assert switch.calls == calls

    switch.failure = None
    assert pipeline.retry_queue.release_source_months() == 1
    assert asyncio.run(pipeline.backfill_source("CyberDaily", [month]))["searched"] == [month]
    pipeline.close()


def test_backfill_force_searches_a_given_up_month(env, switch):
    month = _processed_month(env, 7)
    pipeline = env.make()
    switch.failure = _http_error(410)
    asyncio.run(pipeline.backfill_source("CyberDaily", [month]))
    switch.failure = None
    assert asyncio.run(pipeline.backfill_source("CyberDaily", [month], force=True))["searched"] == [month]
    pipeline.close()


# ---------------------------------------------------------------------------
# Re-fetching stored stories: missing, temporary, incomplete, reopened
# ---------------------------------------------------------------------------

URL = "https://www.cyberdaily.au/security/14337-virtual-ideas"


def _fetcher(result):
    async def fetch(url):
        if isinstance(result, BaseException):
            raise result
        return result
    return fetch


def _age_refetch(conn, days=40):
    """Move every refetch log entry back, as if the back-off and the 24-hour rule had passed."""
    past = (datetime.now() - timedelta(days=days)).isoformat()
    conn.execute("UPDATE CyberDailyRefetches SET fetched_at = ?, next_attempt_at = CASE WHEN next_attempt_at "
                 "IS NULL THEN NULL ELSE ? END", (past, past))
    conn.commit()


def test_a_missing_story_is_never_fetched_again_and_is_reported(env):
    raw_id = _stored_story(env.path, URL, ARTICLE, env.now.date().isoformat())
    conn = sqlite3.connect(env.path)
    report = cr.refetch_stories(conn, fetch=_fetcher((None, {"error": "HTTP 404"})), delay_seconds=0)
    assert report.outcomes[cr.FAILED] == 1 and report.failed[0]["error_type"] == fe.MISSING
    assert [m["raw_event_id"] for m in report.missing] == [raw_id]
    assert conn.execute("SELECT status, error_type FROM CyberDailyRefetches").fetchone() == ("missing", fe.MISSING)
    _age_refetch(conn, days=400)
    assert cr.stored_stories(conn, days=None) == []
    assert [r["raw_event_id"] for r in cr.unrecoverable(conn)] == [raw_id]
    assert cr.release(conn) == 0                                          # gone from the site: stays out
    assert cr.release(conn, include_missing=True) == 1
    conn.close()


@pytest.mark.parametrize("result,expected", [
    ((None, {"error": "timed out after 3 attempts"}), fe.NETWORK),
    (TimeoutError("page load"), fe.NETWORK),
    (("A page with only a headline.", {}), fe.INCOMPLETE),
])
def test_temporary_failures_are_retried_after_a_back_off_and_given_up_at_the_cap(env, result, expected):
    raw_id = _stored_story(env.path, URL, CUT_OFF, env.now.date().isoformat())
    conn = sqlite3.connect(env.path)
    for attempt in range(1, cr.MAX_REFETCH_FAILURES + 1):
        report = cr.refetch_stories(conn, fetch=_fetcher(result), delay_seconds=0)
        assert report.candidates == 1 and report.failed[0]["error_type"] == expected
        failures, next_at, gave_up = conn.execute(
            "SELECT failures, next_attempt_at, gave_up_at FROM CyberDailyRefetches").fetchone()
        assert failures == attempt
        if attempt < cr.MAX_REFETCH_FAILURES:
            assert next_at and not gave_up and not report.gave_up
            assert cr.stored_stories(conn, days=90) == []                 # inside the back-off
            _age_refetch(conn)
        else:
            assert gave_up and [g["raw_event_id"] for g in report.gave_up] == [raw_id]
    _age_refetch(conn, days=400)
    assert cr.stored_stories(conn, days=None) == []
    assert cr.release(conn) == 1 and len(cr.stored_stories(conn, days=90)) == 1
    conn.close()


def test_a_fetch_that_leaves_the_story_cut_off_is_an_incomplete_failure(env):
    _stored_story(env.path, URL, CUT_OFF, env.now.date().isoformat())
    conn = sqlite3.connect(env.path)
    other_banner = CUT_OFF.replace("Log in", "Sign in")                  # changed, still cut off
    report = cr.refetch_stories(conn, fetch=_fetcher((other_banner, {})), delay_seconds=0)
    assert report.outcomes[cr.UPDATED] == 1 and report.still_incomplete == 1
    assert conn.execute("SELECT status, error_type, failures FROM CyberDailyRefetches").fetchone() == (
        "updated", fe.INCOMPLETE, 1)
    _age_refetch(conn)
    report = cr.refetch_stories(conn, fetch=_fetcher((ARTICLE, {})), delay_seconds=0)
    assert report.outcomes[cr.UPDATED] == 1 and report.still_incomplete == 0
    assert conn.execute("SELECT error_type, failures, next_attempt_at FROM CyberDailyRefetches").fetchone() == (
        None, 0, None)
    conn.close()


def _snippet_only_record(db_path, url, processing_error="Filtered out during processing", published="2021-07-14"):
    """A record discovery could not scrape, classified on the standfirst and not kept."""
    db = CyberEventDataV2(str(db_path))
    raw_id = db.add_raw_event("CyberDaily", {"title": "Exclusive: Qilin claims Victorian firm", "source_url": url,
                                              "event_date": published})
    db.close()
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE RawEvents SET raw_content = NULL, is_processed = 1, processing_error = ?, discovered_at = ? "
                 "WHERE raw_event_id = ?", (processing_error, (datetime.now() - timedelta(days=2)).isoformat(), raw_id))
    conn.commit()
    conn.close()
    return raw_id


def test_full_text_reopens_a_record_filtered_on_its_standfirst(env):
    raw_id = _snippet_only_record(env.path, "https://www.cyberdaily.au/security/6001-qilin-victorian-firm")
    rejected = _snippet_only_record(env.path, "https://www.cyberdaily.au/security/6002-x",
                                    processing_error="Rejected at page_gate: not an incident report")
    hygiene = _snippet_only_record(env.path, "https://www.cyberdaily.au/security/6003-y")
    conn = sqlite3.connect(env.path)
    conn.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, status, is_australian_event, "
                 "is_specific_event) VALUES ('enr-h', ?, 'y', 'Rejected', 1, 1)", (hygiene,))
    conn.commit()
    candidates = {s["raw_event_id"] for s in cr.stored_stories(conn, days=90)}
    assert candidates == {raw_id}           # a filter or hygiene rejection is never re-fetched

    report = cr.refetch_stories(conn, fetch=_fetcher((ARTICLE, {"content_origin": "page"})), delay_seconds=0)
    assert report.outcomes[cr.UPDATED] == 1 and report.reopened == 1 and report.requeued_enrichments == 0
    row = conn.execute("SELECT is_processed, processing_error, raw_content FROM RawEvents WHERE raw_event_id = ?",
                       (raw_id,)).fetchone()
    assert row[0] == 0 and row[1] is None and "Surry Hills" in row[2]
    # The retry queue drains it on the next run, from the stored full text.
    assert raw_id in {r["raw_event_id"] for r in RetryQueue(conn).due()}
    conn.close()


def test_the_repair_limit_takes_stories_used_by_an_event_first(env):
    used = _stored_story(env.path, "https://www.cyberdaily.au/security/7001-used", CUT_OFF, "2021-02-01")
    unused = [_snippet_only_record(env.path, f"https://www.cyberdaily.au/security/{7100 + n}-x",
                                   published=f"2022-0{n + 1}-01") for n in range(3)]
    conn = sqlite3.connect(env.path)
    report = cr.RefetchReport()
    chosen = cr.stored_stories(conn, days=90, repair_limit=2, report=report)
    assert [s["raw_event_id"] for s in chosen] == [used, unused[2]]       # then newest first
    assert report.repair_candidates == 2 and report.repair_deferred == 2
    assert len(cr.stored_stories(conn, days=90, repair_limit=None)) == 4
    conn.close()


def test_the_window_is_by_publication_not_by_when_the_backfill_stored_it(env):
    _stored_story(env.path, "https://www.cyberdaily.au/security/8001-old", ARTICLE, "2021-03-04")
    conn = sqlite3.connect(env.path)
    conn.execute("UPDATE RawEvents SET discovered_at = ?", ((datetime.now() - timedelta(days=2)).isoformat(),))
    conn.commit()
    assert cr.stored_stories(conn, days=90) == []
    assert len(cr.stored_stories(conn, days=None)) == 1
    conn.close()


def test_legacy_refetch_failures_get_a_type(env):
    conn = sqlite3.connect(env.path)
    conn.execute("DROP TABLE IF EXISTS CyberDailyRefetches")
    conn.execute("""CREATE TABLE CyberDailyRefetches (raw_event_id TEXT PRIMARY KEY, url TEXT NOT NULL,
                    status TEXT NOT NULL, content_hash TEXT, chars INTEGER, last_error TEXT,
                    failures INTEGER NOT NULL DEFAULT 0, fetched_at TEXT NOT NULL)""")
    conn.execute("INSERT INTO CyberDailyRefetches VALUES ('a', 'u1', 'failed', NULL, NULL, 'HTTP 404', 1, '2026-10-28')")
    conn.execute("INSERT INTO CyberDailyRefetches VALUES ('b', 'u2', 'failed', NULL, NULL, 'timed out', 1, '2026-10-28')")
    conn.commit()
    cr.ensure_schema(conn)
    cr.ensure_schema(conn)
    rows = conn.execute("SELECT raw_event_id, status, error_type, gave_up_at IS NOT NULL FROM CyberDailyRefetches "
                        "ORDER BY 1").fetchall()
    assert rows == [("a", "missing", fe.MISSING, 1), ("b", "failed", fe.NETWORK, 0)]
    conn.close()


# ---------------------------------------------------------------------------
# Coverage metrics
# ---------------------------------------------------------------------------

TODAY = date(2026, 10, 15)


def _thresholds(**cyberdaily):
    thresholds = copy.deepcopy(load_thresholds())
    thresholds.settings["cyberdaily"].update({"backfill_since": "2026-01", **cyberdaily})
    return thresholds


@pytest.fixture
def cd_db(tmp_path):
    """Months 2026-01..2026-09 judged on 2026-10-15, one of each search state."""
    path = tmp_path / "cd.db"
    initialize_database(str(path))
    conn = sqlite3.connect(path)
    queue = RetryQueue(conn, clock=lambda: datetime(2026, 10, 1))
    counts = {1: 40, 2: 42, 3: 38, 4: 41, 5: 0, 6: 12, 7: 39}       # 2026-05 never stored (failed search)
    for month in (1, 2, 3, 4, 6, 7):
        queue.record_discovery("CyberDaily", 2026, month, counts[month], [], items_seen=counts[month])
    queue.record_discovery("CyberDaily", 2026, 2, 42, [], items_seen=50)                 # 8 listed, not stored
    queue.record_discovery("CyberDaily", 2026, 5, 0, ["429 Client Error"], error_types=[fe.THROTTLED])
    queue.record_discovery("CyberDaily", 2026, 8, 0, ["HTTP 404"], error_types=[fe.MISSING])
    # 2026-09: never searched
    n = 0
    for month, count in counts.items():
        for _ in range(count):
            n += 1
            content = ARTICLE if n % 10 else None                    # every tenth page could not be scraped
            conn.execute(
                "INSERT INTO RawEvents (raw_event_id, source_type, raw_title, source_url, raw_content, event_date, "
                "publication_date, discovered_at, is_processed, processing_error) VALUES (?, 'CyberDaily', 't', ?, ?, "
                "?, ?, '2026-10-02', ?, ?)",
                (f"r{n}", f"https://www.cyberdaily.au/security/{n}-x", content, f"2026-{month:02d}-10",
                 f"2026-{month:02d}-10", 0 if n == 3 else 1, None if n == 3 else "Filtered out during processing"))
    # A Google record of an article Cyber Daily also stored: one article, not two.
    conn.execute("INSERT INTO RawEvents (raw_event_id, source_type, source_url, raw_content, publication_date, "
                 "is_processed) VALUES ('g1', 'GoogleSearch', 'https://cyberdaily.au/security/1-other-slug', ?, "
                 "'2026-01-10', 1)", (ARTICLE,))
    cr.ensure_schema(conn)
    conn.execute("INSERT INTO CyberDailyRefetches (raw_event_id, url, status, last_error, failures, fetched_at, "
                 "error_type, gave_up_at) VALUES ('r10', 'https://www.cyberdaily.au/security/10-x', 'missing', "
                 "'HTTP 404', 1, '2026-10-03', 'missing', '2026-10-03')")
    conn.commit()
    conn.close()
    return path


def _measured(path, thresholds):
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        out = qm.measure_cyberdaily(conn, thresholds, TODAY)
    finally:
        conn.close()
    return out, {name: evaluate(name, "coverage", m, thresholds) for name, m in out.items()}


def test_backfill_completion_warns_on_temporary_gaps_and_blocks_on_given_up_months(cd_db):
    out, judged = _measured(cd_db, _thresholds())
    assert out["coverage.cyberdaily.backfill_months_complete"].value == 6
    assert out["coverage.cyberdaily.backfill_months_not_searched"].examples == ["2026-09"]
    assert judged["coverage.cyberdaily.backfill_months_not_searched"].status == WARN
    retrying = judged["coverage.cyberdaily.backfill_months_retrying"]
    assert retrying.value == 1 and retrying.status == WARN and not retrying.blocking
    assert retrying.examples[0].startswith("2026-05: throttled")
    failed = judged["coverage.cyberdaily.backfill_months_failed"]
    assert failed.value == 1 and failed.status == FAIL and failed.blocking
    assert failed.examples[0].startswith("2026-08: missing")


def test_per_month_seen_stored_judged_and_short_months(cd_db):
    out, judged = _measured(cd_db, _thresholds())
    assert out["coverage.cyberdaily.stored.2026-01"].value == 40          # the Google copy is the same article
    assert out["coverage.cyberdaily.seen.2026-02"].value == 50
    assert out["coverage.cyberdaily.judged.2026-01"].value == 39          # r3 is still pending
    assert "coverage.cyberdaily.seen.2026-09" not in out                  # never searched: nothing seen
    assert out["coverage.cyberdaily.stored.2026-10"].value == 0           # the current month is shown
    short = judged["coverage.cyberdaily.short_months"]
    assert short.status == WARN and [e[:7] for e in short.examples] == ["2026-06"]
    gap = judged["coverage.cyberdaily.months_seen_not_stored"]
    assert gap.status == WARN and gap.examples == ["2026-02: feed listed 50, 42 stored"]
    assert out["coverage.cyberdaily.stored_not_judged"].value == 1


def test_incomplete_and_unrecoverable_stories(cd_db):
    out, judged = _measured(cd_db, _thresholds())
    # 21 records without text; r10 is gone from the site and no longer counts as repairable.
    assert out["coverage.cyberdaily.stories_incomplete"].value == 20
    assert judged["coverage.cyberdaily.stories_incomplete"].status == WARN
    unrecoverable = judged["coverage.cyberdaily.stories_unrecoverable"]
    assert unrecoverable.value == 1 and "1 missing" in unrecoverable.detail


def test_a_clean_history_passes(cd_db):
    conn = sqlite3.connect(cd_db)
    queue = RetryQueue(conn)
    for month in (5, 8, 9):                       # searched completely; nothing published (stored 0)
        queue.record_discovery("CyberDaily", 2026, month, 0, [], items_seen=0)
    queue.record_discovery("CyberDaily", 2026, 2, 42, [], items_seen=42)
    conn.close()
    _, judged = _measured(cd_db, _thresholds(min_neighbour_median=1000))
    for name in ("backfill_months_not_searched", "backfill_months_retrying", "backfill_months_failed",
                 "short_months", "months_seen_not_stored"):
        assert judged[f"coverage.cyberdaily.{name}"].status == PASS, name


def test_the_quality_report_includes_cyber_daily_and_the_gate_closes_on_a_given_up_month(cd_db):
    conn = sqlite3.connect(f"{cd_db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        report = run_quality_checks(conn, thresholds=_thresholds(), today=TODAY)
    finally:
        conn.close()
    assert any(r.startswith("coverage.cyberdaily.backfill_months_failed") for r in report.reasons)
    assert not any("retrying" in r for r in report.reasons)
    markdown = render_markdown(report)
    assert "Cyber Daily articles per publication month" in markdown
    assert "`coverage.cyberdaily.stored.2026-01`" not in markdown         # series go to the table, not the checks


def test_monthly_coverage_judges_cyber_daily_only_after_its_backfill_month():
    months = [f"2026-{m:02d}" for m in range(1, 13)] + ["2027-01"]      # 2027-01: current, not judged
    series = {"2026-10": 4682, "2026-11": 90, "2026-12": 0}
    flagged = qm.flag_months(series, months, months[:-1], 6, 0.5, 5, since="2026-11")
    assert flagged == ["2026-12: 0 (trailing median 90)"]                 # not every month before the source
    assert len(qm.flag_months(series, months, months[:-1], 6, 0.5, 5)) > 8
    committed = load_thresholds()
    assert "CyberDaily" in committed.setting("coverage", "monthly_sources")
    assert committed.setting("coverage", "source_judge_from")["CyberDaily"] == "2026-11"
