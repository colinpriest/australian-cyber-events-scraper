"""A record's page and enrichment must describe the record's own article.

The live defect (2026-10-29): 840 enrichments were attached to the wrong stored record - the raw record's title,
URL and page agreed with each other, the enrichment's title was exactly another record's title. Every external
service (Perplexity) is a stub.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from cyber_data_collector.dedup import source_gate
from cyber_data_collector.processing import page_rescrape
from cyber_data_collector.processing import record_identity as ri
from cyber_data_collector.processing.record_evidence import (
    BASIS_ARTICLE,
    BASIS_SNIPPET_ONLY,
    AcceptanceLedger,
    prepare_records_for_processing,
)
from cyber_data_collector.storage.schema_init import initialize_database

PERTH = ("Perth Mint visitor data stolen after feedback survey company hacked. The Perth Mint confirmed that "
         "visitor details held by a survey provider were taken by attackers who breached the provider.")
CANVA = ("Canva security incident May 24 FAQs. Canva said attackers obtained usernames and email addresses of "
         "some Canva users in a security incident on May 24.")
TOLL = ("Toll Group shuts IT systems after cyber security incident. Toll said ransomware encrypted systems "
        "across its logistics network.")


def _research(org, when="2020-01-31"):
    return json.dumps({"formal_entity_name": org, "overall_confidence": 0.9, "earliest_event_date": when,
                       "reasoning": f"{org} incident"})


class World:
    """Records, enrichments and events, written the way the pipeline writes them."""

    def __init__(self, conn):
        self.conn = conn

    def record(self, raw, title, page, url="https://news.example/story-one", source="WebberInsurance"):
        self.conn.execute("INSERT INTO RawEvents (raw_event_id, source_type, raw_title, raw_content, source_url, "
                          "discovered_at, is_processed) VALUES (?, ?, ?, ?, ?, '2025-09-28', 1)",
                          (raw, source, title, page, url))

    def enrich(self, enriched, raw, title, org, event, created="2025-09-28T10:00:00", status="Active"):
        self.conn.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, status, "
                          "is_australian_event, is_specific_event, perplexity_enrichment_data, created_at) "
                          "VALUES (?, ?, ?, ?, 1, 1, ?, ?)", (enriched, raw, title, status, _research(org), created))
        self.conn.execute("INSERT OR IGNORE INTO DeduplicatedEvents (deduplicated_event_id, "
                          "master_enriched_event_id, title, status, victim_organization_name, event_date, "
                          "is_australian_event, is_specific_event) VALUES (?, ?, ?, 'Active', ?, '2020-01-31', 1, 1)",
                          (event, enriched, title, org))
        self.conn.execute("INSERT INTO EventDeduplicationMap (map_id, raw_event_id, enriched_event_id, "
                          "deduplicated_event_id) VALUES (?, ?, ?, ?)", (f"m-{enriched}", raw, enriched, event))

    def raw_of(self, enriched):
        return self.conn.execute("SELECT raw_event_id FROM EnrichedEvents WHERE enriched_event_id = ?",
                                 (enriched,)).fetchone()[0]

    def mapped_raw_of(self, enriched):
        return self.conn.execute("SELECT raw_event_id FROM EventDeduplicationMap WHERE enriched_event_id = ?",
                                 (enriched,)).fetchone()[0]


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "instance" / "t.db"
    initialize_database(str(path))
    c = sqlite3.connect(path)
    yield c
    c.close()


@pytest.fixture
def world(conn):
    return World(conn)


def swapped(world):
    """Two records whose enrichments were attached to each other (the 2025-09-28 shift)."""
    world.record("r-perth", "Perth Mint visitor data stolen after feedback survey company hacked", PERTH,
                 "https://watoday.example/perth-mint-hacked")
    world.record("r-canva", "Canva Security Incident May 24 FAQs - Canva Help Centre", CANVA,
                 "https://canva.example/help/incident-may24/", source="GoogleSearch")
    world.enrich("e-canva", "r-perth", "Canva Security Incident May 24 FAQs - Canva Help Centre", "Canva Pty Ltd", "d-canva")
    world.enrich("e-perth", "r-canva", "Perth Mint visitor data stolen after feedback survey company hacked",
                 "Gold Corporation", "d-perth")
    world.conn.commit()


# ---------------------------------------------------------------------------
# The two questions
# ---------------------------------------------------------------------------

def test_enrichment_matches_record():
    ok = ri.enrichment_matches_record
    assert ok("Canva Security Incident", ["Canva Pty Ltd"], "Canva Security Incident May 24 FAQs", CANVA)[0] is True
    # Another record carries exactly this title, so even a page that happens to name "Sydney" does not save it.
    assert ok("Western Sydney University Office 365 Breach", ["Western Sydney University"],
              "Toyota does U-turn on data breach", "Toyota customers ... Sydney ...", carried_by_another=True)[0] is False
    assert ok("Canva breach", ["Canva"], "Perth Mint visitor data stolen", PERTH)[0] is False
    assert ok("Canva breach", ["Canva"], "Perth Mint visitor data stolen", None)[0] is None


def test_url_kind(conn, world):
    for i in range(5):
        world.record(f"h{i}", f"Incident {i}", "x", "https://tracker.example/breaches/2025")
    world.record("a", "One story", "x", "https://news.example/one-story")
    assert ri.url_kind(conn, "https://tracker.example/") == "site_root"
    assert ri.url_kind(conn, "https://tracker.example/breaches/2025") == "hub"
    assert ri.url_kind(conn, "https://news.example/one-story") == "article"


# ---------------------------------------------------------------------------
# Re-attaching enrichments to their own records
# ---------------------------------------------------------------------------

def test_swapped_enrichments_go_home_and_nothing_else_changes(conn, world):
    swapped(world)
    findings = {f.enriched_event_id: f for f in ri.assess(conn)}
    assert findings["e-canva"].kind == ri.KIND_ENRICHMENT and findings["e-canva"].owner_raw_event_id == "r-canva"
    assert findings["e-perth"].owner_raw_event_id == "r-perth"

    report = ri.repair(conn, dry_run=True)
    assert report.repointed == 2 and world.raw_of("e-canva") == "r-perth"           # a dry run moves nothing
    assert conn.execute("SELECT COUNT(*) FROM RecordIdentityChecks").fetchone()[0] == 0

    report = ri.repair(conn)
    assert (report.repointed, report.sound) == (2, 0)
    assert world.raw_of("e-canva") == "r-canva" and world.raw_of("e-perth") == "r-perth"
    assert world.mapped_raw_of("e-canva") == "r-canva"                              # the event's lineage follows
    assert conn.execute("SELECT title FROM EnrichedEvents WHERE enriched_event_id = 'e-canva'").fetchone()[0].startswith("Canva")
    assert all(f.kind is None for f in ri.assess(conn))

    again = ri.repair(conn)                                                         # idempotent
    assert (again.repointed, again.requeued, again.rescrape_queued, again.searches, again.resolved) == (0, 0, 0, 0, 0)
    assert again.sound == 2


def test_a_reattachment_can_be_undone_and_is_not_made_again(conn, world):
    swapped(world)
    ri.repair(conn)
    assert ri.reverse(conn, "e-canva") == "r-perth"
    conn.commit()
    assert world.raw_of("e-canva") == "r-perth" and world.mapped_raw_of("e-canva") == "r-perth"
    ri.repair(conn)
    assert world.raw_of("e-canva") == "r-perth"                                     # decided by hand
    assert ri.reverse(conn, "e-never-moved") is None


def test_an_enrichment_never_displaces_one_that_describes_its_record(conn, world):
    world.record("r-perth", "Perth Mint visitor data stolen after feedback survey company hacked", PERTH)
    world.record("r-canva", "Canva Security Incident May 24 FAQs - Canva Help Centre", CANVA)
    world.record("r-toll", "Toll Group shuts IT systems after cyber security incident", TOLL)
    world.enrich("e-perth", "r-perth", "Perth Mint visitor data stolen after feedback survey company hacked", "Gold Corporation", "d-perth")
    # A second enrichment of the Perth Mint story, attached to the Toll record.
    world.enrich("e-copy", "r-toll", "Perth Mint visitor data stolen after feedback survey company hacked", "Gold Corporation", "d-copy")
    world.conn.commit()
    report = ri.repair(conn)
    assert report.repointed == 0 and world.raw_of("e-copy") == "r-toll" and world.raw_of("e-perth") == "r-perth"
    assert report.not_repointed == {"that record already has an enrichment of its own": 1}


def test_copies_of_one_enrichment_the_earliest_goes_home(conn, world):
    world.record("r-perth", "Perth Mint visitor data stolen after feedback survey company hacked", PERTH)
    world.record("r-toll", "Toll Group shuts IT systems after cyber security incident", TOLL)
    world.record("r-canva", "Canva Security Incident May 24 FAQs - Canva Help Centre", CANVA)
    title = "Perth Mint visitor data stolen after feedback survey company hacked"
    world.enrich("e-late", "r-toll", title, "Gold Corporation", "d-late", created="2025-09-28T12:00:00")
    world.enrich("e-early", "r-canva", title, "Gold Corporation", "d-early", created="2025-09-28T09:00:00")
    world.conn.commit()
    report = ri.repair(conn)
    assert world.raw_of("e-early") == "r-perth" and world.raw_of("e-late") == "r-toll"
    # The copy stays: its record now has the enrichment that is its own.
    assert report.not_repointed == {"that record already has an enrichment of its own": 1}


def test_a_move_that_would_put_a_record_in_one_event_twice_is_not_made(conn, world):
    world.record("r-perth", "Perth Mint visitor data stolen after feedback survey company hacked", PERTH)
    world.record("r-toll", "Toll Group shuts IT systems after cyber security incident", TOLL)
    title = "Perth Mint visitor data stolen after feedback survey company hacked"
    world.enrich("e-ok", "r-perth", title, "Gold Corporation", "d-one")
    world.enrich("e-stray", "r-toll", title, "Gold Corporation", "d-one")           # same event, copy on the wrong record
    world.conn.commit()
    report = ri.repair(conn)
    assert report.repointed == 0 and world.raw_of("e-stray") == "r-toll"
    assert "its event already holds that record through another enrichment" in report.not_repointed


def test_a_record_left_without_an_enrichment_is_reopened_for_the_retry_queue(conn, world):
    world.record("r-perth", "Perth Mint visitor data stolen after feedback survey company hacked", PERTH)
    world.record("r-toll", "Toll Group shuts IT systems after cyber security incident", TOLL)
    # The Perth Mint enrichment sits on the Toll record; the Toll record's own enrichment never existed.
    world.enrich("e-perth", "r-toll", "Perth Mint visitor data stolen after feedback survey company hacked", "Gold Corporation", "d-perth")
    world.conn.commit()
    report = ri.repair(conn)
    assert world.raw_of("e-perth") == "r-perth"
    assert report.requeued == 1
    assert conn.execute("SELECT is_processed FROM RawEvents WHERE raw_event_id = 'r-toll'").fetchone()[0] == 0
    assert conn.execute("SELECT is_processed FROM RawEvents WHERE raw_event_id = 'r-perth'").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Pages that are another article
# ---------------------------------------------------------------------------

def test_a_page_that_is_another_article_is_refetched_when_its_url_is_an_article(conn, world):
    world.record("r-toll", "Toll Group shuts IT systems after cyber security incident", PERTH,
                 "https://news.example/toll-group-shuts-systems", source="Perplexity")
    world.enrich("e-toll", "r-toll", "Toll Group shuts IT systems after cyber security incident", "Toll Holdings", "d-toll")
    world.conn.commit()

    report = ri.repair(conn)
    assert (report.repointed, report.rescrape_queued, report.searches) == (0, 1, 0)
    row = conn.execute("SELECT kind, action FROM RecordIdentityChecks WHERE raw_event_id = 'r-toll'").fetchone()
    assert tuple(row) == (ri.KIND_PAGE, ri.ACTION_RESCRAPE)
    assert conn.execute("SELECT status FROM PageRescrapes WHERE raw_event_id = 'r-toll'").fetchone()[0] == "queued"
    assert ri.repair(conn).rescrape_queued == 0                                     # asked for once

    # The re-scrape route picks it up although the stored text is long, and the new text is checked again.
    [page] = page_rescrape.short_pages(conn)
    assert page["raw_event_id"] == "r-toll" and page["chars"] == 0
    page_rescrape.apply_result(conn, page, TOLL * 3, {}, page_rescrape.RescrapeReport())
    assert conn.execute("SELECT raw_content FROM RawEvents WHERE raw_event_id = 'r-toll'").fetchone()[0].startswith("Toll Group")
    assert conn.execute("SELECT verdict FROM RecordIdentityChecks WHERE raw_event_id = 'r-toll'").fetchone()[0] == "match"
    assert page_rescrape.short_pages(conn) == []


def test_a_refetch_that_still_gives_another_article_goes_to_the_replacement_search(conn, world):
    world.record("r-toll", "Toll Group shuts IT systems after cyber security incident", PERTH,
                 "https://news.example/toll-group-shuts-systems", source="Perplexity")
    world.enrich("e-toll", "r-toll", "Toll Group shuts IT systems after cyber security incident", "Toll Holdings", "d-toll")
    world.conn.commit()
    ri.repair(conn)
    [page] = page_rescrape.short_pages(conn)
    page_rescrape.apply_result(conn, page, PERTH * 3, {}, page_rescrape.RescrapeReport())
    assert conn.execute("SELECT action FROM RecordIdentityChecks WHERE raw_event_id = 'r-toll'").fetchone()[0] == ri.ACTION_RESCRAPED

    asked = []

    def search(query, model):
        asked.append(query)
        return {"answer": "FOUND: yes\nINCIDENT: Toll was hit.\nSOURCES: https://news.example/toll-ransomware",
                "citations": ["https://news.example/toll-ransomware"]}

    report = ri.repair(conn, search=search)
    assert (report.searches, len(asked)) == (1, 1)
    new = conn.execute("SELECT source_type, source_url, is_processed FROM RawEvents "
                       "WHERE source_url = 'https://news.example/toll-ransomware'").fetchone()
    assert tuple(new) == ("Perplexity", "https://news.example/toll-ransomware", 0)    # a record of its own, with its own URL
    assert conn.execute("SELECT action FROM RecordIdentityChecks WHERE raw_event_id = 'r-toll'").fetchone()[0] == ri.ACTION_REPLACEMENT
    ri.repair(conn, search=search)
    assert len(asked) == 1                                                          # never searched twice


def test_a_site_root_goes_straight_to_the_replacement_search_and_a_missing_key_is_an_error(conn, world, monkeypatch):
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    world.record("r-toll", "Toll Group shuts IT systems after cyber security incident", PERTH,
                 "https://tracker.example/", source="Perplexity")
    world.enrich("e-toll", "r-toll", "Toll Group shuts IT systems after cyber security incident", "Toll Holdings", "d-toll")
    world.conn.commit()
    report = ri.repair(conn)
    assert report.rescrape_queued == 0 and len(report.errors) == 1 and "PERPLEXITY_API_KEY" in report.errors[0]
    dry = ri.repair(conn, dry_run=True)
    assert dry.searches == 1 and not dry.errors


def test_the_limit_holds_retrieval_back_until_the_next_run(conn, world):
    for i in range(3):
        world.record(f"r{i}", f"Toll Group number {i} shuts IT systems after incident", PERTH,
                     f"https://news.example/toll-group-{i}-shuts-systems", source="Perplexity")
        world.enrich(f"e{i}", f"r{i}", f"Toll Group number {i} shuts IT systems after incident", "Toll Holdings", f"d{i}")
    world.conn.commit()
    first = ri.repair(conn, limit=2)
    assert (first.rescrape_queued, first.waiting) == (2, 1)
    second = ri.repair(conn, limit=2)
    assert (second.rescrape_queued, second.waiting) == (1, 0)


# ---------------------------------------------------------------------------
# At ingest
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    from cyber_data_collector.storage.cyber_event_data_v2 import CyberEventDataV2
    path = tmp_path / "instance" / "ingest.db"
    initialize_database(str(path))
    database = CyberEventDataV2(str(path))
    yield database
    database.close()


def _event(raw_id, title):
    from cyber_data_collector.models.events import (
        ConfidenceScore, CyberEvent, CyberEventType, EventSeverity, EventSource)
    return CyberEvent(
        title=title, description="snippet", event_type=CyberEventType.OTHER, severity=EventSeverity.UNKNOWN,
        australian_relevance=True,
        confidence=ConfidenceScore(overall=0.8, source_reliability=0.8, data_completeness=0.5,
                                   temporal_accuracy=0.5, geographic_accuracy=0.5),
        data_sources=[EventSource(source_id="s", source_type="Perplexity", url="https://news.example/x",
                                  content_snippet="snippet", credibility_score=0.5, relevance_score=0.5)],
        raw_event_id=raw_id)


def _stored(db, title, page, url):
    return db.add_raw_event("Perplexity", {"title": title, "description": "snippet", "content": page,
                                           "event_date": None, "source_url": url, "metadata": {}})


def test_ingest_withholds_a_page_that_is_another_article_and_queues_its_retrieval(db):
    raw = _stored(db, "Toll Group shuts IT systems after cyber security incident", PERTH * 2,
                  "https://news.example/toll-group-shuts-systems")
    [prepared] = prepare_records_for_processing(db, [_event(raw, "Toll Group shuts IT systems after cyber security incident")],
                                                AcceptanceLedger())
    assert prepared.evidence_basis == BASIS_SNIPPET_ONLY and prepared.evidence_text is None
    row = db._conn.execute("SELECT stage, kind, action FROM RecordIdentityChecks WHERE raw_event_id = ?", (raw,)).fetchone()
    assert tuple(row) == (ri.STAGE_INGEST, ri.KIND_PAGE, ri.ACTION_RESCRAPE)
    assert db._conn.execute("SELECT status FROM PageRescrapes WHERE raw_event_id = ?", (raw,)).fetchone()[0] == "queued"


def test_ingest_keeps_the_page_of_the_records_own_article(db):
    raw = _stored(db, "Toll Group shuts IT systems after cyber security incident", TOLL * 2,
                  "https://news.example/toll-group-shuts-systems")
    [prepared] = prepare_records_for_processing(db, [_event(raw, "Toll Group shuts IT systems after cyber security incident")],
                                                AcceptanceLedger())
    assert prepared.evidence_basis == BASIS_ARTICLE and prepared.evidence_text
    assert db._conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'RecordIdentityChecks'").fetchone()[0] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM RecordIdentityChecks").fetchone()[0] == 0


def test_an_enrichment_that_does_not_describe_its_record_is_recorded_as_it_is_stored(db):
    raw = _stored(db, "Toll Group shuts IT systems after cyber security incident", TOLL * 2,
                  "https://news.example/toll-group-shuts-systems")
    ok = db.create_enriched_event(raw, {"title": "Toll Group ransomware attack", "description": "d",
                                        "is_australian_event": True, "is_specific_event": True})
    assert db._conn.execute("SELECT COUNT(*) FROM RecordIdentityChecks").fetchone()[0] == 0
    other = _stored(db, "Canva Security Incident May 24 FAQs - Canva Help Centre", CANVA * 2, "https://canva.example/help")
    bad = db.create_enriched_event(other, {"title": "Perth Mint visitor data stolen", "description": "d",
                                           "is_australian_event": True, "is_specific_event": True})
    rows = db._conn.execute("SELECT raw_event_id, enriched_event_id, kind, action FROM RecordIdentityChecks").fetchall()
    assert [tuple(r) for r in rows] == [(other, bad, ri.KIND_ENRICHMENT, ri.ACTION_REVIEW)]
    assert ok != bad


def test_records_beyond_the_limit_are_reopened_by_a_later_run(conn, world):
    titles = {f"r{i}": f"Distinct incident number {i} sparkling {chr(97 + i) * 5} outage" for i in range(3)}
    for i, (raw, title) in enumerate(titles.items()):
        world.record(raw, title, f"{title}. Details of {title}.")
        world.record(f"wrong{i}", f"Unrelated holder quokka {chr(120 + i) * 5}", "Unrelated holder. Quokka.")
    for i, (raw, title) in enumerate(titles.items()):
        world.enrich(f"e{i}", f"wrong{i}", title, f"Org {i}", f"d{i}")                # each enrichment on a stranger
    world.conn.commit()
    first = ri.repair(conn, limit=2)
    assert (first.repointed, first.requeued, first.waiting) == (3, 2, 1)
    second = ri.repair(conn, limit=2)
    assert (second.repointed, second.requeued, second.waiting) == (0, 1, 0)           # the held-back one, found again
    assert conn.execute("SELECT COUNT(*) FROM RawEvents WHERE is_processed = 0").fetchone()[0] == 3
    third = ri.repair(conn, limit=2)
    assert third.requeued == 0                                                        # not reopened twice


def test_perplexity_does_not_store_a_site_address_as_an_incidents_source():
    from cyber_data_collector.datasources.perplexity import (
        PerplexityDataSource, PerplexityEvent, PerplexitySearchResults)
    from cyber_data_collector.models.config import DataSourceConfig
    from cyber_data_collector.utils import RateLimiter

    source = PerplexityDataSource(DataSourceConfig(name="Perplexity"), RateLimiter(), {})
    results = PerplexitySearchResults(events=[PerplexityEvent(
        title="Quality Rugs & Furniture data breach", description="d", event_date=None, entity_name=None,
        event_type="Data Breach", impact_description=None,
        source_urls=["https://cybertrackerau.com/", "not a url", "https://news.example/quality-rugs-breach"])])
    [event] = source._convert_results_to_events(results)
    assert [s.url for s in event.data_sources] == ["https://news.example/quality-rugs-breach"]
    assert source._is_article_url("https://news.example/a") and not source._is_article_url("https://site.example")
