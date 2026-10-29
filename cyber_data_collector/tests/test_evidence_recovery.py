"""Evidence recovery: diagnosis, queueing through the existing mechanisms, and the evidence status."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from cyber_data_collector.dedup import source_gate
from cyber_data_collector.processing import evidence_recovery as er
from cyber_data_collector.processing import page_rescrape as pr
from cyber_data_collector.quality.metrics import measure_evidence_status
from cyber_data_collector.quality.model import load_thresholds
from cyber_data_collector.storage.schema_init import initialize_database

ARTICLE = ("Acme Health confirmed on 12 March 2024 that a ransomware attack encrypted its patient "
           "systems and that the details of 5,000 patients were taken. ") * 4
RESEARCH = json.dumps({"formal_entity_name": "Acme Health Pty Ltd", "earliest_event_date": "2024-03-12",
                       "overall_confidence": 0.9})
FOUND = {"answer": "FOUND: yes\nINCIDENT: Acme was attacked.\nSOURCES: https://news.example/report",
         "citations": ["https://news.example/report"]}


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    path = tmp_path / "instance" / "t.db"
    initialize_database(str(path))
    c = sqlite3.connect(path)
    yield c
    c.close()


def _event(c, event, victim, members, event_date="2024-03-12"):
    """``members``: (raw id, source, url, content, metadata, validated, research, p_status, attempts, specific)."""
    for i, (rid, source, url, content, meta, validated, research, p_status, attempts, specific) in enumerate(members):
        c.execute("INSERT INTO RawEvents (raw_event_id, source_type, raw_title, raw_content, source_url, "
                  "source_metadata, is_processed) VALUES (?, ?, ?, ?, ?, ?, 1)",
                  (rid, source, rid, content, url, json.dumps(meta or {})))
        c.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, status, is_australian_event, "
                  "is_specific_event, perplexity_validated, perplexity_enrichment_data, perplexity_status, "
                  "perplexity_attempt_count, perplexity_last_error) VALUES (?, ?, ?, 'Active', 1, ?, ?, ?, ?, ?, ?)",
                  (f"e-{rid}", rid, rid, specific, validated, research, p_status, attempts,
                   "unsupported claims withheld" if attempts else None))
        if i == 0:
            c.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, master_enriched_event_id, title, "
                      "event_date, victim_organization_name, event_type, status, is_australian_event, "
                      "is_specific_event) VALUES (?, ?, ?, ?, ?, 'Ransomware', 'Active', 1, 1)",
                      (event, f"e-{rid}", f"{victim} breach", event_date, victim))
        c.execute("INSERT INTO EventDeduplicationMap (map_id, raw_event_id, enriched_event_id, deduplicated_event_id, "
                  "contribution_type, similarity_score) VALUES (?, ?, ?, ?, 'primary', 1.0)",
                  (f"map-{rid}", rid, f"e-{rid}", event))
    c.commit()


def _member(rid, source="GoogleSearch", url=None, content=None, meta=None, validated=0, research=None,
            p_status=None, attempts=0, specific=1):
    return (rid, source, url if url is not None else f"https://news.example/{rid}", content, meta, validated,
            research, p_status, attempts, specific)


@pytest.fixture
def populated(conn):
    _event(conn, "ev-supported", "Acme Health", [_member("sup", content=ARTICLE, validated=1, research=RESEARCH)])
    _event(conn, "ev-page-only", "Beta Bank", [_member("page", content=ARTICLE)])
    _event(conn, "ev-alternate", "Gamma Group", [_member(
        "alt", content=ARTICLE, meta={"content_origin": "perplexity_alternative",
                                      "content_source_url": "https://other.example/story"})])
    _event(conn, "ev-bare", "Delta Council", [_member("bare", source="Perplexity")])
    _event(conn, "ev-cyberdaily", "Epsilon Ltd", [_member(
        "cd", source="CyberDaily", url="https://www.cyberdaily.au/security/12345-epsilon-hacked")])
    _event(conn, "ev-exhausted", "Zeta Health", [_member("dead", url="", p_status="quarantined", attempts=3)])
    return conn


def _by_id(conn, **kw):
    return {e.event_id: e for e in er.assess_events(conn, labels_path=None, **kw)}


def test_diagnosis_and_status(populated):
    events = _by_id(populated)
    assert events["ev-supported"].status == er.SUPPORTED
    assert events["ev-page-only"].status == er.PARTIAL
    assert "no validated research" in events["ev-page-only"].reason
    # Another page's text counts as that page's evidence, never as the record's own.
    alternate = events["ev-alternate"]
    assert alternate.status == er.PARTIAL and alternate.members[0].page == er.PAGE_ALTERNATE
    assert "https://other.example/story" in alternate.reason
    bare = events["ev-bare"]
    assert bare.status == er.UNSUPPORTED and bare.recovery == er.ROUTE_NEEDED
    assert bare.members[0].page_route.state == er.ROUTE_NEEDED
    assert bare.members[0].research_route.state == er.ROUTE_PENDING      # the enrichment queue retries it
    assert {"title", "event_date", "victim_organization_name", "event_type"} <= set(bare.uncertain_fields)
    # Cyber Daily stories are re-fetched by the Cyber Daily re-fetch, not queued twice.
    assert events["ev-cyberdaily"].members[0].page_route.state == er.ROUTE_PENDING
    exhausted = events["ev-exhausted"]
    assert exhausted.members[0].page_route.state == er.ROUTE_EXHAUSTED
    assert exhausted.members[0].research_route.state == er.ROUTE_EXHAUSTED
    assert exhausted.replacement_route.state == er.ROUTE_NEEDED
    assert events["ev-supported"].uncertain_fields == [] and events["ev-page-only"].uncertain_fields == []


def test_a_page_found_to_be_another_article_is_not_the_records_own(populated):
    from cyber_data_collector.processing import record_identity

    record_identity.ensure_schema(populated)
    populated.execute("INSERT INTO RecordIdentityChecks (raw_event_id, enriched_event_id, stage, verdict, kind, "
                      "checked_at) VALUES ('page', '', 'audit', 'mismatch', 'page_other_article', '2026-10-29')")
    member = _by_id(populated)["ev-page-only"].members[0]
    assert member.page == er.PAGE_OTHER_ARTICLE and member.page_route.state != er.ROUTE_DONE
    assert "another article" in member.describe()
    # Resolved (the right page was fetched) counts as its own page again.
    populated.execute("UPDATE RecordIdentityChecks SET resolved_at = '2026-10-30'")
    assert _by_id(populated)["ev-page-only"].members[0].page == er.PAGE_OWN


def test_recover_queues_through_existing_mechanisms_and_is_idempotent(populated):
    calls = []

    def search(query, model):
        calls.append(query)
        return FOUND

    report = er.recover(populated, search=search, labels_path=None)
    assert report.pages_queued == ["bare"]                      # not the Cyber Daily story, not the URL-less one
    assert len(calls) == 1 and "Zeta Health" in calls[0]       # only the event every other route has failed
    status = dict(populated.execute("SELECT deduplicated_event_id, evidence_status FROM DeduplicatedEvents"))
    assert status == {"ev-supported": "supported", "ev-page-only": "partial", "ev-alternate": "partial",
                      "ev-bare": "unsupported", "ev-cyberdaily": "unsupported", "ev-exhausted": "unsupported"}
    # The replacement is a new record with its own URL, linked to the incident it is for.
    raw_id, url, meta, processed = populated.execute(
        "SELECT raw_event_id, source_url, source_metadata, is_processed FROM RawEvents "
        "WHERE source_metadata LIKE '%evidence_for%'").fetchone()
    assert url == "https://news.example/report" and not processed
    assert json.loads(meta)["evidence_for"] == "ev-exhausted"
    assert populated.execute("SELECT purpose, deduplicated_event_id FROM SourceReplacements").fetchone() == \
        (er.PURPOSE, "ev-exhausted")

    again = er.recover(populated, search=search, labels_path=None)
    assert again.pages_queued == [] and again.acted_on == [] and len(calls) == 1
    assert again.counts["changed"] == 0
    assert again.by_recovery == {er.ROUTE_PENDING: 3}
    reason = populated.execute("SELECT evidence_status_reason FROM DeduplicatedEvents "
                               "WHERE deduplicated_event_id = 'ev-exhausted'").fetchone()[0]
    assert "replacement report queued (https://news.example/report)" in reason


def test_queued_page_is_rescraped_and_its_research_queued_again(populated):
    er.recover(populated, search=lambda q, m: FOUND, labels_path=None)
    populated.execute("UPDATE EnrichedEvents SET perplexity_status = 'quarantined', perplexity_attempt_count = 3 "
                      "WHERE raw_event_id = 'bare'")
    populated.commit()
    assert {p["raw_event_id"] for p in pr.short_pages(populated)} >= {"bare"}   # a Perplexity record

    async def fetch(url):
        return (ARTICLE, {"content_origin": "page"}) if url.endswith("/bare") else (None, {"error": "HTTP 404"})

    report = pr.rescrape_short_pages(populated, fetch=fetch)
    assert [s["raw_event_id"] for s in report.stored] == ["bare"] and report.requeued_research == 1
    assert populated.execute("SELECT perplexity_status, perplexity_attempt_count FROM EnrichedEvents "
                             "WHERE raw_event_id = 'bare'").fetchone() == (None, 0)
    # Validated research is not touched by a re-fetch.
    assert populated.execute("SELECT perplexity_validated FROM EnrichedEvents WHERE raw_event_id = 'sup'"
                             ).fetchone()[0] == 1
    counts = er.mark_status(populated, labels_path=None)
    assert counts[er.UNSUPPORTED] == 2 and counts["changed"] >= 1
    assert populated.execute("SELECT evidence_status, evidence_uncertain_fields FROM DeduplicatedEvents "
                             "WHERE deduplicated_event_id = 'ev-bare'").fetchone() == ("partial", "[]")


def test_failed_search_is_pending_and_retried_with_its_query(populated):
    def broken(query, model):
        raise TimeoutError("timed out")

    er.recover(populated, search=broken, labels_path=None)
    assert populated.execute("SELECT status FROM SourceReplacements").fetchone()[0] == source_gate.STATUS_PENDING
    # The source gate's retry leaves it to evidence recovery (its record has no research to search from).
    assert source_gate.retry_pending_replacements(populated, search=lambda q, m: FOUND) == []
    queries = []
    report = er.recover(populated, search=lambda q, m: queries.append(q) or FOUND, labels_path=None)
    assert len(queries) == 1 and "Zeta Health" in queries[0]
    assert any(s["outcome"].startswith("retried: queued") for s in report.searches)
    # A queued evidence-recovery replacement is not pruned as "covered" by the incident it is for.
    assert source_gate.prune_redundant_replacements(populated) == []


def test_no_search_key_fails_loudly_and_the_rest_still_happens(populated):
    report = er.recover(populated, labels_path=None)
    assert report.errors and "PERPLEXITY_API_KEY" in report.errors[0]
    assert report.pages_queued == ["bare"]
    assert report.counts[er.UNSUPPORTED] == 3


def test_cap_defers_incidents_to_the_next_run(populated):
    _event(populated, "ev-bare-2", "Eta Shire", [_member("bare2", source="WebberInsurance")], "2024-05-01")
    report = er.recover(populated, limit=1, search=lambda q, m: FOUND, labels_path=None)
    assert len(report.acted_on) == 1 and report.deferred == 2
    assert er.recover(populated, limit=1, search=lambda q, m: FOUND, labels_path=None).acted_on


def test_dry_run_writes_nothing(populated):
    report = er.recover(populated, dry_run=True, search=lambda q, m: pytest.fail("searched"), labels_path=None)
    assert report.pages_queued == ["bare"] and report.searches[0]["outcome"] == "would search"
    assert populated.execute("SELECT COUNT(*) FROM PageRescrapes").fetchone()[0] == 0
    assert populated.execute("SELECT COUNT(*) FROM DeduplicatedEvents WHERE evidence_status IS NOT NULL"
                             ).fetchone()[0] == 0


def test_labelled_report_is_queued_before_any_search(populated, tmp_path):
    labels = tmp_path / "labels.csv"
    labels.write_text("deduplicated_event_id,label,rationale,corroboration,labeller\n"
                      "ev-exhausted,real,Zeta Health was attacked.,https://zeta.example/notice,t\n", encoding="utf-8")
    report = er.recover(populated, search=lambda q, m: pytest.fail("searched"), labels_path=labels)
    assert [q["event"] for q in report.labels_queued] == ["ev-exhausted"]
    url, meta = populated.execute("SELECT source_url, source_metadata FROM RawEvents "
                                  "WHERE source_type = 'LabelledEvidence'").fetchone()
    assert url == "https://zeta.example/notice" and json.loads(meta)["label_evidence"]["event"] == "ev-exhausted"
    again = er.recover(populated, search=lambda q, m: pytest.fail("searched"), labels_path=labels)
    assert again.labels_queued == []
    [event] = er.assess_events(populated, ["ev-exhausted"], labels_path=labels)
    assert "labelled report https://zeta.example/notice queued" in event.reason


def test_status_timestamp_moves_only_when_the_status_changes(populated):
    er.mark_status(populated, labels_path=None)
    populated.execute("UPDATE DeduplicatedEvents SET evidence_status_at = 'then', evidence_status_reason = 'old'")
    populated.commit()
    assert er.mark_status(populated, labels_path=None)["changed"] == 6
    assert {r[0] for r in populated.execute("SELECT evidence_status_at FROM DeduplicatedEvents")} == {"then"}


def test_quality_metrics_and_staleness(populated):
    found = measure_evidence_status(populated, load_thresholds(), date(2026, 10, 28))
    assert found["evidence.status.unsupported"].value == 3
    assert found["evidence.status.unsupported_share"].value == 0.5
    assert found["evidence.recovery.needed"].value == 2
    assert found["evidence.status.stored_stale"].value == 6           # never marked
    er.mark_status(populated, labels_path=None)
    assert measure_evidence_status(populated, load_thresholds(), date(2026, 10, 28)
                                   )["evidence.status.stored_stale"].value == 0


def test_exports_and_dashboards_read_the_status(populated):
    sql = f"SELECT {er.select_columns(populated, 'd')} FROM DeduplicatedEvents d WHERE deduplicated_event_id = ?"
    assert populated.execute(sql, ("ev-bare",)).fetchone()[0] == er.NOT_ASSESSED
    er.mark_status(populated, labels_path=None)
    status, reason, fields = populated.execute(sql, ("ev-bare",)).fetchone()
    assert status == er.UNSUPPORTED and "no page text" in reason and "event_date" in json.loads(fields)
    counts = er.stored_counts(populated)
    assert counts == {er.SUPPORTED: 1, er.PARTIAL: 2, er.UNSUPPORTED: 3, er.NOT_ASSESSED: 0}
    assert "3 unsupported" in er.describe_counts(counts)


def test_unsupported_records_are_enriched_first(populated, tmp_path):
    from cyber_data_collector.storage.cyber_event_data_v2 import CyberEventDataV2
    from scripts.perplexity_backfill_events import PerplexityBackfillProcessor

    er.mark_status(populated, labels_path=None)
    path = populated.execute("PRAGMA database_list").fetchone()[2]
    db = CyberEventDataV2(path)
    try:
        processor = PerplexityBackfillProcessor(db, perplexity_engine=None)
        order = [e["raw_event_id"] for e in processor.get_events_needing_enrichment()]
    finally:
        db.close()
    unsupported = {"bare", "cd"}
    assert set(order[:len(unsupported)]) == unsupported and "page" in order
