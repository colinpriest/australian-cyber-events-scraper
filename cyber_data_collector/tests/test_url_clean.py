"""Tracking and identity parameters (a newsletter's subscriber token) never reach, or stay in, the database."""

from __future__ import annotations

import logging
import sqlite3

import pytest
from bs4 import BeautifulSoup

from cyber_data_collector.datasources import webber_insurance as wi
from cyber_data_collector.models.config import DataSourceConfig
from cyber_data_collector.processing import record_identity as ri
from cyber_data_collector.storage.schema_init import initialize_database
from cyber_data_collector.utils import RateLimiter
from cyber_data_collector.utils.url_clean import clean_text, clean_url, scrub_stored_urls

#: Not a real token: the shape of one (a JWT whose payload would name the subscriber).
FAKE_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJlbWFpbCI6InN1YnNjcmliZXJAZXhhbXBsZS5pbnZhbGlkIn0.c2ln"
DIRTY = f"https://www.itnews.com.au/news/story-538657?eid=3&edate=20200227&utm_source=20200227_PM&utm_medium=newsletter&token={FAKE_TOKEN}"
CLEAN = "https://www.itnews.com.au/news/story-538657"


@pytest.mark.parametrize("url, expected", [
    (DIRTY, CLEAN),
    ("https://x.example/a?id=7&utm_campaign=c&page=2#top", "https://x.example/a?id=7&page=2#top"),   # content kept
    ("https://x.example/a?eid=12", "https://x.example/a?eid=12"),                                      # eid alone may be an article id
    ("https://x.example/a?gclid=1&fbclid=2&mc_eid=3", "https://x.example/a"),
    ("https://x.example/a?v=1", "https://x.example/a?v=1"),
    ("https://x.example/a", "https://x.example/a"),
    (None, None),
    ("", ""),
])
def test_clean_url(url, expected):
    assert clean_url(url) == expected


def test_clean_text_cleans_urls_quoted_in_text():
    text = f'See "{DIRTY}" and (https://x.example/b?utm_source=n). No url here.'
    assert clean_text(text) == f'See "{CLEAN}" and (https://x.example/b). No url here.'


def test_webber_item_links_are_stored_without_the_subscribers_parameters():
    source = wi.WebberInsuranceDataSource(DataSourceConfig(name="WebberInsurance"), RateLimiter(), {})
    html = (f'<h3>Vertel - June 2025</h3><ul><li><a href="{DIRTY}&campaign=20_06_2025">Vertel hit by ransomware</a></li></ul>')
    [item] = source._extract_all_event_links(BeautifulSoup(html, "html.parser"))
    assert item["url"] == CLEAN and FAKE_TOKEN not in item["url"]
    assert item["url_date"].date().isoformat() == "2025-06-20"       # the newsletter date is kept as a publication date
    dated = BeautifulSoup('<h3>Vertel - June 2025</h3><ul><li><a href="https://n.example/a?utm_campaign=20_06_2025">'
                          'Vertel hit by ransomware</a></li></ul>', "html.parser")
    [item] = source._extract_all_event_links(dated)
    assert item["url"] == "https://n.example/a" and item["url_date"].date().isoformat() == "2025-06-20"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "instance" / "t.db"
    initialize_database(str(path))
    c = sqlite3.connect(path)
    yield c
    c.close()


def _seed(conn):
    conn.execute("INSERT INTO RawEvents (raw_event_id, source_type, raw_title, raw_description, raw_content, source_url, "
                 "source_metadata, discovered_at) VALUES ('w1', 'WebberInsurance', 't', ?, ?, ?, ?, '2026-10-28')",
                 (f"see {DIRTY}", "page text", DIRTY, f'{{"citations": ["{DIRTY}"]}}'))
    conn.execute("INSERT INTO RawEvents (raw_event_id, source_type, raw_title, source_url, discovered_at) "
                 "VALUES ('g1', 'GoogleSearch', 't', 'https://x.example/ok?id=7', '2026-10-28')")
    # A sources row whose cleaned key already exists: the dirty row is dropped, not duplicated.
    for url in (DIRTY, CLEAN):
        conn.execute("INSERT INTO DeduplicatedEventSources (deduplicated_event_id, source_url, source_type) "
                     "VALUES ('d1', ?, 'WebberInsurance')", (url,))
    conn.commit()


def test_stored_tracking_parameters_are_scrubbed_idempotently_and_never_reported(conn, caplog):
    _seed(conn)
    with caplog.at_level(logging.DEBUG):
        dry = scrub_stored_urls(conn, dry_run=True)
        assert dry.total >= 4 and conn.execute("SELECT source_url FROM RawEvents WHERE raw_event_id = 'w1'"
                                               ).fetchone()[0] == DIRTY          # a dry run writes nothing
        report = scrub_stored_urls(conn)
        again = scrub_stored_urls(conn)
    assert report.total == dry.total - 0 and report.removed_duplicates == 1
    assert again.total == 0 and again.removed_duplicates == 0
    assert conn.execute("SELECT source_url FROM RawEvents WHERE raw_event_id = 'w1'").fetchone()[0] == CLEAN
    assert conn.execute("SELECT source_url FROM RawEvents WHERE raw_event_id = 'g1'").fetchone()[0].endswith("?id=7")
    stored = conn.execute("SELECT raw_description, source_metadata FROM RawEvents WHERE raw_event_id = 'w1'").fetchone()
    assert all(FAKE_TOKEN not in value for value in stored)
    assert [r[0] for r in conn.execute("SELECT source_url FROM DeduplicatedEventSources")] == [CLEAN]
    # Counts only: neither the report nor the log holds the token or any parameter value.
    assert FAKE_TOKEN not in report.summary() and FAKE_TOKEN not in caplog.text and "subscriber" not in caplog.text


def test_new_raw_events_are_stored_without_them_and_found_again_by_their_clean_url(tmp_path):
    from cyber_data_collector.storage.cyber_event_data_v2 import CyberEventDataV2

    path = tmp_path / "instance" / "ingest.db"
    initialize_database(str(path))
    db = CyberEventDataV2(str(path))
    try:
        raw = db.add_raw_event("GoogleSearch", {"title": "Story", "description": f"quoted {DIRTY}", "content": None,
                                                "event_date": None, "source_url": DIRTY,
                                                "metadata": {"citations": [DIRTY]}})
        row = db._conn.execute("SELECT source_url, raw_description, source_metadata FROM RawEvents "
                               "WHERE raw_event_id = ?", (raw,)).fetchone()
        assert row[0] == CLEAN and all(FAKE_TOKEN not in value for value in row)
        assert db.find_existing_raw_event("GoogleSearch", DIRTY, "Story") == raw     # the same article, however linked
    finally:
        db.close()


def test_check_record_identity_scrubs_stored_urls_and_reports_a_count(conn):
    _seed(conn)
    report = ri.repair(conn)
    assert report.urls_cleaned >= 4 and FAKE_TOKEN not in report.summary()
    assert conn.execute("SELECT COUNT(*) FROM RawEvents WHERE source_url LIKE '%token=%'").fetchone()[0] == 0
    assert ri.repair(conn).urls_cleaned == 0
