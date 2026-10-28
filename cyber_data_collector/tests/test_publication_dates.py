"""Publication dates: kept apart from incident dates, read from the source or the page's
metadata, rolled up to each event's first report and reporting lag (no network)."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from cyber_data_collector.dedup import date_provenance as dp
from cyber_data_collector.processing import publication_dates as pd
from cyber_data_collector.storage.cyber_event_data_v2 import CyberEventDataV2
from cyber_data_collector.storage.schema_init import initialize_database

FOUND = date(2026, 10, 28)


def _page(head: str = "", body: str = "") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


@pytest.mark.parametrize("html, expected", [
    (_page('<script type="application/ld+json">{"@type": "NewsArticle", "datePublished": "2026-06-18T09:30:00+10:00",'
           ' "dateModified": "2026-07-01"}</script>'), ("2026-06-18", "JSON-LD datePublished")),
    (_page('<script type="application/ld+json">{"@graph": [{"@type": "WebSite"}, {"@type": "Article",'
           ' "datePublished": "2025-03-12"}]}</script>'), ("2025-03-12", "JSON-LD datePublished")),
    (_page('<meta property="article:published_time" content="2024-04-30T01:00:00Z">'),
     ("2024-04-30", "meta article:published_time")),
    (_page('<meta name="DC.date.issued" content="2023-06-08">'), ("2023-06-08", "meta dc.date.issued")),
    (_page(body='<span itemprop="datePublished" content="2022-09-22">22 Sep</span>'),
     ("2022-09-22", "itemprop datePublished")),
    (_page(body='<article><time datetime="2021-11-05">5 Nov</time></article>'), ("2021-11-05", "article <time>")),
])
def test_reads_the_pages_own_publication_date(html, expected):
    assert pd.from_html(html, FOUND) == expected


def test_a_modified_date_is_never_a_publication_date():
    html = _page('<meta property="article:modified_time" content="2026-07-01">'
                 '<script type="application/ld+json">{"@type": "Article", "dateModified": "2026-07-01"}</script>',
                 '<aside><time datetime="2026-07-02">related story</time></aside>')
    assert pd.from_html(html, FOUND) is None


def test_implausible_dates_are_not_publication_dates():
    future = _page('<meta property="article:published_time" content="2027-01-01">')
    ancient = _page('<meta property="article:published_time" content="1970-01-01">')
    assert pd.from_html(future, FOUND) is None and pd.from_html(ancient, FOUND) is None
    assert pd.normalise("Tue, 18 Jun 2026 09:00:00 +1000") == "2026-06-18"
    assert pd.normalise("not a date") is None


@pytest.mark.parametrize("url, expected", [
    ("https://www.nytimes.com/2025/10/12/world/australia/qantas-cyber-attack.html", "2025-10-12"),
    ("https://www.westernsydney.edu.au/news/cyber-details/october-23-2025", "2025-10-23"),
    ("https://example.com/news/2025/10/qantas", None),          # a month is not a date
    ("https://example.com/story?date=2025/10/12", None),        # the query string is not the path
])
def test_url_path_dates(url, expected):
    assert pd.from_url(url, FOUND) == expected


def test_google_results_carry_the_page_metatags():
    item = {"pagemap": {"metatags": [{"og:title": "x", "article:published_time": "2026-06-19T02:00:00Z"}]}}
    assert pd.from_google_item(item, FOUND) == "2026-06-19"
    assert pd.from_google_item({"title": "no pagemap"}, FOUND) is None


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "instance" / "t.db"
    initialize_database(str(path))
    c = sqlite3.connect(path)
    # e1: three reports of one incident, undated (2026-06-18 first); e2: incident stated
    # as 2026-03-01 to the day, reported 2026-03-11; e3: no date and no report date.
    records = [("r1", "e1", None, "https://news.example/2026/06/18/agent", None),
               ("r2", "e1", None, "https://news.example/agent-follow-up", None),
               ("r3", "e1", None, "https://news.example/gone", None),
               ("r4", "e2", "2026-03-01", "https://feed.example/x", "2026-03-11"),
               ("r5", "e3", None, "https://news.example/nothing", None)]
    for raw, dedup, incident, url, published in records:
        c.execute("INSERT INTO RawEvents (raw_event_id, source_type, source_url, raw_content, source_metadata, "
                  "discovered_at) VALUES (?, 'GoogleSearch', ?, ?, ?, '2026-10-28T06:00:00')",
                  (raw, url, "The breach began on 1 March 2026." if dedup == "e2" else "text",
                   json.dumps({"publication_date": published})))
        c.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, event_date, status, "
                  "is_australian_event, is_specific_event) VALUES (?, ?, 't', ?, 'Active', 1, 1)",
                  (f"x{raw}", raw, incident))
        c.execute("INSERT INTO EventDeduplicationMap (map_id, raw_event_id, enriched_event_id, deduplicated_event_id, "
                  "contribution_type, similarity_score) VALUES (?, ?, ?, ?, 'merged', 1.0)",
                  (f"m{raw}", raw, f"x{raw}", dedup))
    for dedup, incident in (("e1", None), ("e2", "2026-03-01"), ("e3", None)):
        c.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, master_enriched_event_id, title, "
                  "event_date, status, is_australian_event, is_specific_event) VALUES (?, ?, 't', ?, 'Active', 1, 1)",
                  (dedup, f"x{dedup}", incident))
    c.commit()
    yield c
    c.close()


def _fetcher(pages):
    calls = []

    def fetch(url):
        calls.append(url)
        return pages[url]
    fetch.calls = calls
    return fetch


PAGES = {
    "https://news.example/2026/06/18/agent": (None, "HTTP 403"),                  # refused; URL has the date
    "https://news.example/agent-follow-up": (
        _page('<meta property="article:published_time" content="2026-06-20">'), None),
    "https://news.example/gone": (None, "HTTP 404"),
    "https://news.example/nothing": (_page(body="<p>no date anywhere</p>"), None),
}


def test_backfill_reads_source_then_page_then_url_and_records_failures(conn):
    browser_calls = []

    def browser(urls):
        browser_calls.extend(urls)
        return {u: (None, "HTTP 403 (browser)") for u in urls}

    report = pd.backfill(conn, fetch=_fetcher(PAGES), browser_fetch=browser)
    stored = dict(conn.execute("SELECT raw_event_id, publication_date || ' | ' || publication_date_source "
                               "FROM RawEvents WHERE publication_date IS NOT NULL").fetchall())
    assert stored == {"r1": "2026-06-18 | URL path",
                      "r2": "2026-06-20 | page metadata: meta article:published_time",
                      "r4": "2026-03-11 | source"}
    assert browser_calls == ["https://news.example/2026/06/18/agent"]          # only the refused page
    assert (report.from_source, report.stored, report.no_date, report.gave_up) == (1, 2, 1, 1)
    status = dict(conn.execute("SELECT raw_event_id, status FROM PublicationDateFetches").fetchall())
    assert status["r3"] == pd.STATUS_GAVE_UP and status["r5"] == pd.STATUS_NO_DATE
    # Idempotent: nothing left to fetch.
    again = _fetcher(PAGES)
    assert pd.backfill(conn, fetch=again, browser_fetch=browser).candidates == 0 and again.calls == []


def test_a_page_the_browser_can_read_is_dated_from_its_metadata(conn):
    pages = {**PAGES, "https://news.example/2026/06/18/agent": (None, "SSLError: certificate verify failed")}
    browser = lambda urls: {u: (_page('<meta name="pubdate" content="2026-06-17">'), None) for u in urls}
    pd.backfill(conn, fetch=_fetcher(pages), browser_fetch=browser)
    assert conn.execute("SELECT publication_date, publication_date_source FROM RawEvents "
                        "WHERE raw_event_id = 'r1'").fetchone() == ("2026-06-17", "page metadata: meta pubdate")


def test_events_get_first_report_lag_and_a_date_only_when_they_had_none(conn):
    pd.backfill(conn, fetch=_fetcher(PAGES), browser_fetch=lambda urls: {u: (None, "x") for u in urls})
    report = pd.refresh_event_dates(conn)
    rows = {r[0]: r[1:] for r in conn.execute(
        "SELECT deduplicated_event_id, event_date, date_basis, first_reported_date, reporting_lag_days "
        "FROM DeduplicatedEvents")}
    # Undated: dated by its first report, and says so; no lag (the date IS the report).
    assert rows["e1"] == ("2026-06-18", pd.BASIS_PUBLICATION, "2026-06-18", None)
    # Dated incident: its date is kept; lag = first report - incident.
    assert rows["e2"] == ("2026-03-01", "stated in the record", "2026-03-11", 10)
    # Nothing to go on: stays undated.
    assert rows["e3"] == (None, "no date", None, None)
    assert report.dated_by_first_report == ["e1"] and report.lags == 1
    # The provenance refresh run after every dedup keeps the publication basis.
    dp.refresh_event_date_provenance(conn)
    assert conn.execute("SELECT date_basis FROM DeduplicatedEvents WHERE deduplicated_event_id = 'e1'"
                        ).fetchone()[0] == pd.BASIS_PUBLICATION
    assert pd.refresh_event_dates(conn).dated_by_first_report == []           # idempotent


def test_storage_writes_the_sources_publication_date_to_its_own_column(tmp_path):
    path = tmp_path / "instance" / "s.db"
    initialize_database(str(path))
    db = CyberEventDataV2(path)
    try:
        raw_id = db.add_raw_event("CyberDaily", {"title": "t", "source_url": "https://x", "event_date": "2026-05-04",
                                                 "metadata": {"publication_date": "2026-05-04T08:00:00"}})
        row = db._conn.execute("SELECT publication_date, publication_date_source FROM RawEvents "
                               "WHERE raw_event_id = ?", (raw_id,)).fetchone()
    finally:
        db.close()
    assert tuple(row) == ("2026-05-04", pd.SOURCE_FROM_SOURCE)
