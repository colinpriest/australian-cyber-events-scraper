"""Tests for suspect-page classification and its effect on the non-incident gate."""
from __future__ import annotations

import sqlite3

import pytest

from cyber_data_collector.dedup import page_classifier as pc
from cyber_data_collector.dedup.non_incident import find_non_incident_events


@pytest.mark.parametrize("title, url, expected", [
    ("AUSCERT Week in Review for 20th February 2026", "https://auscert.org.au/week-in-review/x", True),
    ("Matt Lemon PhD's Post - LinkedIn", "https://www.linkedin.com/posts/mattlemon_x", True),
    ("Latest fraud and scam alerts - NAB", "https://www.nab.com.au/security/x", True),
    ("Your Community Health: Home Page", "https://www.yourch.org.au/", True),
    ("Mackay Sugar cyber attack flagged as broader risk", "https://www.cyberdaily.au/security/13734-x", False),
])
def test_is_suspect(title, url, expected):
    assert pc.is_suspect(title, url) is expected


class _Fake:
    def __init__(self, kinds):
        self.kinds = kinds
        self.chat = self
        self.completions = self
        self.calls = 0

    def create(self, messages, **kwargs):
        self.calls += 1
        text = messages[-1]["content"]
        for needle, kind in self.kinds.items():
            if needle in text:
                return pc.PageVerdict(kind=kind, confidence=0.95, reasoning="test")
        raise RuntimeError("no verdict")


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title TEXT,
            summary TEXT, description TEXT, status TEXT, is_australian_event INTEGER,
            perplexity_enrichment_data TEXT, updated_at TEXT);
        CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_url TEXT);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, title TEXT,
            event_date TEXT, status TEXT);
        CREATE TABLE EventDeduplicationMap (map_id TEXT PRIMARY KEY, enriched_event_id TEXT,
            deduplicated_event_id TEXT);
    """)
    rows = [  # (event, record, title, url)
        ("d-wir", "r-wir", "AUSCERT Week in Review for 20th February 2026", "https://auscert.org.au/week-in-review/1"),
        ("d-ausfec", "r-ausfec", "May 2025 Cyber Update", "https://acsm.com.au/may-2025-cyber-update"),
        ("d-mixed", "r-news", "Acme confirms ransomware attack", "https://www.abc.net.au/news/acme"),
        ("d-mixed", "r-post", "Acme breach - Jane's Post - LinkedIn", "https://www.linkedin.com/posts/jane_acme"),
    ]
    for did, rid, title, url in rows:
        c.execute("INSERT INTO EnrichedEvents VALUES (?, ?, ?, NULL, NULL, 'Active', 1, NULL, NULL)", (rid, rid, title))
        c.execute("INSERT INTO RawEvents VALUES (?, ?)", (rid, url))
        c.execute("INSERT OR IGNORE INTO DeduplicatedEvents VALUES (?, ?, '2026-02-20', 'Active')", (did, did))
        c.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?, ?)", ("m-" + rid, rid, did))
    c.commit()
    yield c
    c.close()


def test_only_suspects_are_sent_and_verdicts_are_stored_once(conn):
    fake = _Fake({"Week in Review": "roundup_or_list", "May 2025": "specific_incident",
                  "LinkedIn": "profile_or_personal"})
    suspects = pc.pending_suspects(conn)
    assert {s[0] for s in suspects} == {"r-wir", "r-ausfec", "r-post"}   # news article not sent
    pc.classify_pages(conn, suspects, client=fake)
    assert fake.calls == 3
    assert pc.pending_suspects(conn) == []                                # never paid for twice


def test_roundup_event_rejected_but_real_incident_from_a_roundup_kept(conn):
    fake = _Fake({"Week in Review": "roundup_or_list", "May 2025": "specific_incident",
                  "LinkedIn": "profile_or_personal"})
    pc.classify_pages(conn, pc.pending_suspects(conn), client=fake)
    found = {e["deduplicated_event_id"] for e in find_non_incident_events(conn, blocklist=[])}
    assert found == {"d-wir"}          # Ausfec-style record is a specific incident; mixed event has news


def test_low_confidence_or_failed_classification_never_rejects(conn):
    class Unsure(_Fake):
        def create(self, messages, **kwargs):
            return pc.PageVerdict(kind="roundup_or_list", confidence=0.5, reasoning="unsure")

    pc.classify_pages(conn, pc.pending_suspects(conn), client=Unsure({}))
    assert find_non_incident_events(conn, blocklist=[]) == []
    # A failure stores nothing, so the record is retried next run.
    c2 = _Fake({})
    conn.execute("DELETE FROM PageClassifications")
    pc.classify_pages(conn, pc.pending_suspects(conn), client=c2)
    assert len(pc.pending_suspects(conn)) == 3
