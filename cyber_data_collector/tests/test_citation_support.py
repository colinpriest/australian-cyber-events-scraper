"""Cited sources must actually support the facts they are cited for.

Reviewer finding: the acceptance gate treated any syntactically valid cited URL
as support, so an unrelated page let invented dates, organisation names and a
huge records-affected count through at high reliability.  Covered here:

* cited pages are retrieved and cached (failures too) - no test touches the
  network;
* each fact must be stated in the record's text or in a cited page about the
  same incident (naming an organisation the record names);
* date support matches the claim's precision and is never a page's own
  publication date; count support records unit, basis and scope;
* unsupported confident claims are withheld and the record stays in the
  retry queue; a corrupt stored answer is quarantined, never overwritten;
* the backfill's counts expose failures (storage failures included) and
  budget refusals separately;
* ``--recompute-reliability`` re-judges stored answers idempotently;
* the Perplexity model comes from ``PERPLEXITY_MODEL`` everywhere.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from cyber_data_collector.processing import cited_documents as cited_documents_module
from cyber_data_collector.processing.cited_documents import (
    STATUS_FETCH_ERROR,
    STATUS_HTTP_ERROR,
    STATUS_OK,
    STATUS_UNSUPPORTED,
    CitedDocument,
    CitedDocumentStore,
    fetch_document,
)
from cyber_data_collector.processing.enrichment_gate import (
    PRECISION_DAY,
    PRECISION_MONTH,
    CorruptStoredEnrichment,
    evaluate_enrichment,
    plan_enrichment_update,
    valid_source_urls,
)
from cyber_data_collector.processing.perplexity_enrichment import (
    PerplexityEnrichmentEngine,
    PerplexityEventEnrichment,
)
from cyber_data_collector.processing.reliability_recheck import recheck_stored_enrichments
from cyber_data_collector.storage.cyber_event_data_v2 import CyberEventDataV2
from cyber_data_collector.storage.enrichment_support import load_fact_support
from cyber_data_collector.utils.api_budget import BudgetExhausted
from scripts.perplexity_backfill_events import PerplexityBackfillProcessor

ROOT = Path(__file__).resolve().parents[2]

ARTICLE = (
    "Acme Logistics Pty Ltd confirmed on 14 March 2025 that attackers encrypted its "
    "warehouse systems and stole the details of 120,000 customers. The company said "
    "the LockBit group claimed responsibility for the ransomware attack. "
) * 2
REFERENCE = date(2025, 6, 1)
NOW = datetime.now().isoformat()
UNRELATED_URL = "https://news.example.org/zeta-mining"
RELATED_URL = "https://news.example.com.au/acme-update"
UNRELATED_PAGE = ("Zeta Mining Corporation disclosed that 275,000,000 records were stolen on "
                  "2 February 2024 in a global breach of its systems. ") * 3
RELATED_PAGE = ("Acme Logistics has now confirmed intruders first entered its network on 9 March 2025, "
                "and that up to 150,000 customers in Australia were affected. ") * 3


def _page(url: str, text: str, published: Optional[str] = None) -> CitedDocument:
    return CitedDocument(url, STATUS_OK, NOW, text=text, published_date=published)


def _unreadable(url: str) -> CitedDocument:
    return CitedDocument(url, STATUS_FETCH_ERROR, NOW, error="offline in tests")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(cited_documents_module, "fetch_document", _unreadable)


def _answer(**overrides: Any) -> PerplexityEventEnrichment:
    fields: Dict[str, Any] = dict(
        earliest_event_date="2025-03-14", date_confidence=0.9,
        formal_entity_name="Acme Logistics Pty Ltd", entity_confidence=0.85,
        victim_industry="Transportation", threat_actor="LockBit", threat_actor_confidence=0.7,
        attack_method="ransomware", attack_method_confidence=0.8,
        victim_count=120000, victim_count_confidence=0.75,
        sources_consulted=[], overall_confidence=0.8,
    )
    fields.update(overrides)
    return PerplexityEventEnrichment(**fields)


def _gate(answer, pages: Optional[Dict[str, CitedDocument]] = None, record_text: str = ARTICLE, **kwargs):
    documents = {url: _unreadable(url) for url in valid_source_urls(answer.sources_consulted)}
    documents.update(pages or {})
    return evaluate_enrichment(answer, record_text=record_text, cited_documents=documents,
                               reference_date=kwargs.pop("reference_date", REFERENCE), **kwargs)


# ---------------------------------------------------------------------------
# Retrieval and cache
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status=200, content_type="text/html; charset=utf-8", body=b"", url="https://x.example.com/a"):
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self.encoding = "utf-8"
        self._body = body
        self.url = url

    def iter_content(self, chunk_size=65536):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]

    def close(self):
        pass


class _FakeSession:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


class TestFetchDocument:
    HTML = (b"<html><head><meta property='article:published_time' content='2025-03-20T08:00:00+10:00'>"
            b"<script>var junk = 'ransomware 999';</script></head><body><nav>Home | Cyber incident</nav>"
            b"<article><p>" + ("Acme Logistics confirmed the breach. " * 10).encode() + b"</p></article>"
            b"<footer>Privacy policy</footer></body></html>")

    def test_html_text_is_extracted_without_scripts_menus_or_footers(self):
        session = _FakeSession(_FakeResponse(body=self.HTML))
        document = fetch_document("https://x.example.com/a", session=session)
        assert document.status == STATUS_OK and document.is_usable
        assert "Acme Logistics confirmed the breach." in document.text
        assert "var junk" not in document.text and "Cyber incident" not in document.text
        assert "Privacy policy" not in document.text
        assert document.published_date == "2025-03-20"
        # Bounded: a timeout is always passed.
        assert session.calls[0][1]["timeout"]

    def test_http_error_and_network_error_are_returned_not_raised(self):
        assert fetch_document("https://x.example.com/a",
                              session=_FakeSession(_FakeResponse(status=404))).status == STATUS_HTTP_ERROR
        failed = fetch_document("https://x.example.com/a", session=_FakeSession(error=TimeoutError("slow")))
        assert failed.status == STATUS_FETCH_ERROR and "slow" in failed.error

    def test_unsupported_type_and_byte_cap(self):
        image = fetch_document("https://x.example.com/a.png",
                               session=_FakeSession(_FakeResponse(content_type="image/png", body=b"\x89PNG")))
        assert image.status == STATUS_UNSUPPORTED
        big = _FakeResponse(content_type="text/plain", body=b"word " * 10_000)
        capped = fetch_document("https://x.example.com/t", session=_FakeSession(big), max_bytes=1000)
        assert capped.status == STATUS_OK and len(capped.text) <= 1000


class TestCitedDocumentStore:
    def test_pages_are_fetched_once_and_failures_are_cached_too(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        calls: List[str] = []

        def fetcher(url):
            calls.append(url)
            return _page(url, "text " * 50) if "good" in url else _unreadable(url)

        store = CitedDocumentStore(conn, fetcher=fetcher)
        urls = ["https://a.example.com/good", "https://a.example.com/bad"]
        first = store.get_documents(urls)
        assert set(first) == set(urls)  # every URL answered, failures included
        assert first[urls[0]].is_usable and not first[urls[1]].is_usable
        second = store.get_documents(urls)
        assert calls == urls  # nothing fetched twice
        assert second[urls[1]].status == STATUS_FETCH_ERROR

    def test_a_stale_failure_is_fetched_again(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        calls: List[str] = []

        def fetcher(url):
            calls.append(url)
            return _unreadable(url)

        store = CitedDocumentStore(conn, fetcher=fetcher, retry_failed_after=timedelta(0))
        store.get_documents(["https://a.example.com/x"])
        store.get_documents(["https://a.example.com/x"])
        assert len(calls) == 2


# ---------------------------------------------------------------------------
# The support check
# ---------------------------------------------------------------------------

class TestSupportCheck:
    def test_an_unrelated_cited_page_does_not_let_invented_facts_through(self):
        """The reviewer's case: invented date, organisation and a huge count, cited to an unrelated page."""
        answer = _answer(earliest_event_date="2024-02-02", date_confidence=0.95,
                         formal_entity_name="Zeta Mining Corporation", entity_confidence=0.95,
                         victim_count=275_000_000, victim_count_confidence=0.95,
                         threat_actor=None, threat_actor_confidence=None, attack_method=None,
                         attack_method_confidence=None, victim_industry=None,
                         sources_consulted=[UNRELATED_URL], overall_confidence=0.95)
        result = _gate(answer, {UNRELATED_URL: _page(UNRELATED_URL, UNRELATED_PAGE)})
        assert not result.is_accepted
        assert set(result.withheld_facts) == {"date", "entity", "victim_count"}
        assert "0 about the incident" in result.decisions["victim_count"].reason

    def test_a_cited_page_about_the_same_organisation_supports_facts_the_record_lacks(self):
        answer = _answer(earliest_event_date="2025-03-09", victim_count=150_000,
                         sources_consulted=[RELATED_URL])
        result = _gate(answer, {RELATED_URL: _page(RELATED_URL, RELATED_PAGE)})
        assert result.is_accepted
        for fact in ("date", "victim_count"):
            decision = result.decisions[fact]
            assert decision.is_accepted
            assert (decision.source_kind, decision.source_url) == ("cited_document", RELATED_URL)
        assert result.decisions["entity"].source_kind == "record"
        assert result.decisions["industry"].source_kind == "entity"

    def test_a_page_naming_only_the_claimed_name_is_not_about_the_record(self):
        # The record names nobody the page names: the page cannot vouch for anything.
        answer = _answer(formal_entity_name="Zeta Mining Corporation", sources_consulted=[UNRELATED_URL],
                         earliest_event_date="2024-02-02")
        result = _gate(answer, {UNRELATED_URL: _page(UNRELATED_URL, UNRELATED_PAGE)},
                       record_text="A breach notice. " * 20)
        assert "entity" in result.withheld_facts and "date" in result.withheld_facts

    def test_linked_organisations_anchor_pages_when_the_claimed_name_is_not_in_the_record(self):
        record = "P&N Bank told customers about a breach at its hosting provider. " * 3
        page = "Police & Nurses Limited, trading as P&N Bank, said 100,000 customers were affected. " * 3
        answer = _answer(formal_entity_name="Police & Nurses Limited", earliest_event_date=None,
                         date_confidence=None, threat_actor=None, threat_actor_confidence=None,
                         attack_method=None, attack_method_confidence=None, victim_count=100_000,
                         sources_consulted=["https://bank.example.com/x"])
        result = _gate(answer, {"https://bank.example.com/x": _page("https://bank.example.com/x", page)},
                       record_text=record, organisations=["P&N Bank"])
        assert result.decisions["entity"].is_accepted
        assert result.decisions["entity"].source_url == "https://bank.example.com/x"

    def test_a_citation_that_was_not_retrieved_is_an_error_not_a_silent_rejection(self):
        answer = _answer(sources_consulted=[RELATED_URL])
        with pytest.raises(ValueError, match="not retrieved"):
            evaluate_enrichment(answer, record_text=ARTICLE, cited_documents={})

    def test_attack_method_needs_every_category_it_names(self):
        assert _gate(_answer(attack_method="ransomware")).decisions["attack_method"].is_accepted
        both = _gate(_answer(attack_method="Ransomware delivered by phishing email"))
        assert "attack_method" in both.withheld_facts

    def test_industry_falls_with_an_unsupported_victim(self):
        result = _gate(_answer(formal_entity_name="Zeta Mining Corporation"))
        assert "entity" in result.withheld_facts and "industry" in result.withheld_facts
        assert result.sanitized.victim_industry is None


class TestDatePrecisionAndBasis:
    def test_month_only_text_supports_only_a_first_of_month_claim_at_month_precision(self):
        record = "Acme Logistics was attacked in March 2025 and is still recovering. " * 3
        exact = _gate(_answer(earliest_event_date="2025-03-14"), record_text=record)
        assert "date" in exact.withheld_facts  # an exact day the text never gives
        month = _gate(_answer(earliest_event_date="2025-03-01"), record_text=record)
        assert month.decisions["date"].is_accepted
        assert month.decisions["date"].details["precision"] == PRECISION_MONTH
        assert month.sanitized.date_precision == PRECISION_MONTH

    def test_day_precise_text_gives_day_precision(self):
        result = _gate(_answer())
        assert result.decisions["date"].details == {"precision": PRECISION_DAY, "basis": "stated"}
        assert result.sanitized.date_basis == "stated"

    def test_a_date_that_is_only_the_page_byline_is_not_the_incident_date(self):
        record = ("Published 2 April 2025. Acme Logistics said attackers stole customer data. " * 3)
        result = _gate(_answer(earliest_event_date="2025-04-02"), record_text=record)
        assert "date" in result.withheld_facts
        assert "publication date" in result.decisions["date"].reason

    def test_a_date_equal_to_the_publication_date_in_the_header_only_is_withheld(self):
        page = "2 April 2025 | Cyber news. " + "Acme Logistics said attackers stole customer data. " * 10
        answer = _answer(earliest_event_date="2025-04-02", sources_consulted=[RELATED_URL])
        result = _gate(answer, {RELATED_URL: _page(RELATED_URL, page, published="2025-04-02")})
        assert "date" in result.withheld_facts

    def test_the_same_day_stated_in_the_body_is_kept_and_its_basis_recorded(self):
        page = ("Acme Logistics said attackers stole customer data. " * 8
                + "The intrusion was detected on 2 April 2025, the company said.")
        answer = _answer(earliest_event_date="2025-04-02", sources_consulted=[RELATED_URL])
        result = _gate(answer, {RELATED_URL: _page(RELATED_URL, page, published="2025-04-02")})
        assert result.decisions["date"].is_accepted
        assert "publication" in result.decisions["date"].details["basis"]


class TestAffectedCounts:
    @pytest.mark.parametrize("text, count, expected", [
        ("Acme Logistics said up to 120,000 customers were affected.", 120_000,
         {"unit": "customers", "basis": "estimate", "scope": None}),
        ("Acme Logistics said more than 9.7 million Australians had data exposed.", 9_700_000,
         {"unit": "people", "basis": "at_least", "scope": "australian"}),
        ("Acme Logistics: 275 million records were exposed worldwide.", 275_000_000,
         {"unit": "records", "basis": "stated", "scope": "global"}),
        ("Acme Logistics said 5,000 were affected.", 5_000,
         {"unit": None, "basis": "stated", "scope": None}),
    ])
    def test_unit_basis_and_scope_come_from_the_wording(self, text, count, expected):
        result = _gate(_answer(victim_count=count), record_text=text * 3)
        decision = result.decisions["victim_count"]
        assert decision.is_accepted and decision.details == expected
        assert result.sanitized.victim_count_unit == expected["unit"]

    def test_a_count_no_source_states_is_withheld_not_guessed(self):
        result = _gate(_answer(victim_count=121_000))
        assert "victim_count" in result.withheld_facts and result.sanitized.victim_count is None


# ---------------------------------------------------------------------------
# Storage and the backfill
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
"""


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.executescript(DDL)
    conn.close()
    database = CyberEventDataV2(path)
    yield database
    database.close()


def _record(db: CyberEventDataV2, content: str = ARTICLE, raw_date: str = "2025-03-10", **columns: Any) -> str:
    raw_id = db.add_raw_event("GoogleSearch", {
        "title": "Acme Logistics breach", "description": "snippet", "content": content,
        "event_date": raw_date, "source_url": "https://news.example.com.au/acme",
        "metadata": {"publication_date": "2025-03-20"},
    })
    with db._lock:
        db._conn.execute("UPDATE RawEvents SET discovered_at = ? WHERE raw_event_id = ?",
                         (REFERENCE.isoformat() + "T09:00:00", raw_id))
        db._conn.commit()
    enriched_id = db.create_enriched_event(raw_id, {
        "title": "Acme Logistics breach", "description": "d", "event_date": "2025-03-14",
        "is_australian_event": True, "is_specific_event": True, "confidence_score": 0.7,
    })
    if columns:
        with db._lock:
            db._conn.execute(f"UPDATE EnrichedEvents SET {', '.join(f'{k} = ?' for k in columns)} "
                             "WHERE enriched_event_id = ?", [*columns.values(), enriched_id])
            db._conn.commit()
    return enriched_id


def _row(db: CyberEventDataV2, enriched_id: str) -> Dict[str, Any]:
    with db._lock:
        row = db._conn.execute("SELECT *, CAST(event_date AS TEXT) AS event_day FROM EnrichedEvents "
                               "WHERE enriched_event_id = ?", (enriched_id,)).fetchone()
        return dict(row)


class _Engine:
    client = object()

    def __init__(self, answers):
        self.answers, self.calls, self.entities = list(answers), [], []

    async def enrich_event(self, title, description, current_date=None, current_entity=None, evidence=None):
        self.calls.append(title)
        self.entities.append(current_entity)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _store(db, pages: Optional[Dict[str, CitedDocument]] = None) -> CitedDocumentStore:
    pages = pages or {}
    return CitedDocumentStore(db._conn, db._lock, fetcher=lambda url: pages.get(url) or _unreadable(url))


def _processor(db, answers, pages=None, dry_run=False) -> PerplexityBackfillProcessor:
    return PerplexityBackfillProcessor(db, _Engine(answers), dry_run=dry_run, cited_documents=_store(db, pages))


def _run(processor) -> Dict[str, int]:
    return asyncio.run(processor.enrich_events_concurrent(processor.get_events_needing_enrichment(),
                                                          max_concurrent=2))


class TestBackfillWithheldClaims:
    def test_unsupported_confident_claims_are_withheld_and_the_record_retried(self, db):
        enriched_id = _record(db)
        answer = _answer(victim_count=9_999_999, victim_count_confidence=0.9,
                         sources_consulted=[UNRELATED_URL])
        processor = _processor(db, [answer], {UNRELATED_URL: _page(UNRELATED_URL, UNRELATED_PAGE)})
        counts = _run(processor)

        row = _row(db, enriched_id)
        assert counts["partially_supported"] == 1 and counts["validated"] == 0 and counts["failed"] == 0
        assert not row["perplexity_validated"] and row["perplexity_status"] == "retrying"
        assert row["perplexity_attempt_count"] == 1 and "victim_count" in row["perplexity_last_error"]
        assert row["records_affected"] is None  # never promoted
        stored = json.loads(row["perplexity_enrichment_data"])
        assert stored["victim_count"] is None and stored["formal_entity_name"] == "Acme Logistics Pty Ltd"
        support = load_fact_support(db._conn, [enriched_id])[enriched_id]
        assert support["victim_count"]["status"] == "withheld" and support["victim_count"]["value"] == 9_999_999
        assert support["entity"] == {**support["entity"], "status": "supported", "source_kind": "record"}
        # Still queued for another attempt.
        assert [e["enriched_event_id"] for e in processor.get_events_needing_enrichment()] == [enriched_id]

    def test_supported_count_is_stored_with_its_unit_basis_scope_and_source(self, db):
        enriched_id = _record(db)
        answer = _answer(victim_count=150_000, victim_count_confidence=0.9, sources_consulted=[RELATED_URL])
        counts = _run(_processor(db, [answer], {RELATED_URL: _page(RELATED_URL, RELATED_PAGE)}))
        row = _row(db, enriched_id)
        assert counts["validated"] == 1
        assert row["records_affected"] == 150_000
        assert (row["records_affected_unit"], row["records_affected_basis"], row["records_affected_scope"],
                row["records_affected_source"]) == ("customers", "estimate", "australian", RELATED_URL)
        stored = json.loads(row["perplexity_enrichment_data"])
        assert stored["victim_count_unit"] == "customers" and stored["date_precision"] == "day"

    def test_a_corrupt_stored_answer_is_quarantined_without_calling_the_api(self, db):
        enriched_id = _record(db, perplexity_enrichment_data="{not json", records_affected=5000)
        processor = _processor(db, [_answer()])
        counts = _run(processor)
        row = _row(db, enriched_id)
        assert processor.perplexity_engine.calls == []
        assert counts["failed"] == 1 and counts["corrupt_baseline"] == 1
        assert row["perplexity_status"] == "quarantined" and "baseline unknown" in row["perplexity_last_error"]
        assert row["perplexity_enrichment_data"] == "{not json" and row["records_affected"] == 5000

    def test_storage_refuses_to_plan_over_a_corrupt_baseline(self, db):
        enriched_id = _record(db, perplexity_enrichment_data="[1, 2]")
        with pytest.raises(CorruptStoredEnrichment):
            db.record_enrichment_success(enriched_id, _gate(_answer()))
        with pytest.raises(CorruptStoredEnrichment):
            plan_enrichment_update({"perplexity_enrichment_data": "{bad"}, _gate(_answer()))
        assert _row(db, enriched_id)["perplexity_enrichment_data"] == "[1, 2]"

    def test_a_failed_write_counts_as_a_failure_not_a_success(self, db, monkeypatch):
        enriched_id = _record(db)
        processor = _processor(db, [_answer()])

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(db, "record_enrichment_success", broken)
        counts = _run(processor)
        assert counts["failed"] == 1 and counts["storage_failed"] == 1 and counts["enriched"] == 0
        assert processor.last_counts == counts
        assert processor.stats["storage_failures"] == 1
        row = _row(db, enriched_id)
        assert row["perplexity_attempt_count"] == 1 and "storage error" in row["perplexity_last_error"]

    def test_apply_returns_false_when_the_write_fails(self, db, monkeypatch):
        _record(db)
        processor = _processor(db, [_answer()])
        [event] = processor.get_events_needing_enrichment()
        data = asyncio.run(processor.enrich_event(event))
        monkeypatch.setattr(db, "record_enrichment_success",
                            lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
        assert processor.apply_enrichment_to_database(data) is False

    def test_a_budget_refusal_is_not_a_failure_and_not_an_attempt(self, db):
        enriched_id = _record(db)
        counts = _run(_processor(db, [BudgetExhausted("API budget exhausted for perplexity (1 requests this run)")]))
        assert counts["budget_refused"] == 1 and counts["failed"] == 0 and counts["enriched"] == 0
        assert _row(db, enriched_id)["perplexity_attempt_count"] == 0


# ---------------------------------------------------------------------------
# --recompute-reliability
# ---------------------------------------------------------------------------

def _old_row(db, answer: PerplexityEventEnrichment, **columns) -> str:
    values = dict(perplexity_validated=1, data_source_reliability=0.85,
                  perplexity_enrichment_data=json.dumps(answer.model_dump()))
    values.update(columns)
    return _record(db, **values)


class TestRecomputeReliability:
    def test_supported_rows_get_computed_reliability_and_unsupported_facts_are_withheld(self, db):
        good = _old_row(db, _answer())
        bad = _old_row(db, _answer(earliest_event_date="2024-02-02", victim_count=275_000_000,
                                   victim_count_confidence=0.95, sources_consulted=[UNRELATED_URL]),
                       event_date="2024-02-02", date_confidence=0.9, records_affected=275_000_000,
                       attack_method="ransomware")
        store = _store(db, {UNRELATED_URL: _page(UNRELATED_URL, UNRELATED_PAGE)})
        report = recheck_stored_enrichments(db._conn, store, dry_run=False)

        assert report.rows_examined == 2 and report.was_constant_085 == 2
        g, b = _row(db, good), _row(db, bad)
        assert g["perplexity_validated"] and g["data_source_reliability"] == pytest.approx(0.8, abs=0.001)
        assert g["perplexity_status"] == "validated"
        assert g["records_affected_unit"] is None  # records_affected column was never the count
        # The invented date and count leave the typed columns; the date reverts
        # to the record's own date; the record is re-queued with one attempt used.
        assert b["records_affected"] is None and b["date_confidence"] is None
        assert b["event_day"] == "2025-03-10"
        assert b["attack_method"] == "ransomware"  # supported, untouched
        assert not b["perplexity_validated"] and b["perplexity_status"] == "retrying"
        assert b["perplexity_attempt_count"] == 1
        stored = json.loads(b["perplexity_enrichment_data"])
        assert stored["victim_count"] is None and stored["earliest_event_date"] is None
        assert b["data_source_reliability"] != 0.85
        assert report.facts_withheld["victim_count"] == 1 and report.columns_cleared["records_affected"] == 1

    def test_a_second_run_changes_nothing(self, db):
        _old_row(db, _answer())
        _old_row(db, _answer(victim_count=275_000_000, sources_consulted=[UNRELATED_URL]),
                 records_affected=275_000_000)
        store = _store(db, {UNRELATED_URL: _page(UNRELATED_URL, UNRELATED_PAGE)})
        recheck_stored_enrichments(db._conn, store, dry_run=False)
        snapshot = db._conn.execute("SELECT * FROM EnrichedEvents ORDER BY enriched_event_id").fetchall()
        support = db._conn.execute("SELECT * FROM EnrichmentFactSupport ORDER BY 1, 2").fetchall()
        report = recheck_stored_enrichments(db._conn, store, dry_run=False)
        assert report.rows_changed == 0
        assert [tuple(r) for r in db._conn.execute(
            "SELECT * FROM EnrichedEvents ORDER BY enriched_event_id").fetchall()] == [tuple(r) for r in snapshot]
        assert [tuple(r) for r in db._conn.execute(
            "SELECT * FROM EnrichmentFactSupport ORDER BY 1, 2").fetchall()] == [tuple(r) for r in support]

    def test_a_fact_withheld_for_an_unreadable_page_is_restored_once_the_page_reads(self, db):
        enriched_id = _old_row(db, _answer(victim_count=150_000, sources_consulted=[RELATED_URL]),
                               records_affected=150_000)
        recheck_stored_enrichments(db._conn, _store(db), dry_run=False)
        assert _row(db, enriched_id)["records_affected"] is None
        # The page becomes readable (a cached failure is retried once stale).
        db._conn.execute("UPDATE CitedDocuments SET fetched_at = '2000-01-01T00:00:00'")
        recheck_stored_enrichments(db._conn, _store(db, {RELATED_URL: _page(RELATED_URL, RELATED_PAGE)}),
                                   dry_run=False)
        row = _row(db, enriched_id)
        assert row["perplexity_validated"] and json.loads(row["perplexity_enrichment_data"])["victim_count"] == 150_000

    def test_an_unreadable_stored_answer_is_quarantined_and_left_alone(self, db):
        enriched_id = _record(db, perplexity_validated=1, data_source_reliability=0.85,
                              perplexity_enrichment_data="{oops", records_affected=42)
        report = recheck_stored_enrichments(db._conn, _store(db), dry_run=False)
        row = _row(db, enriched_id)
        assert report.unreadable_blobs == 1
        assert row["perplexity_status"] == "quarantined" and not row["perplexity_validated"]
        assert row["perplexity_enrichment_data"] == "{oops" and row["records_affected"] == 42

    def test_dry_run_writes_no_enrichment_data(self, db):
        enriched_id = _old_row(db, _answer(victim_count=1, sources_consulted=[UNRELATED_URL]))
        before = _row(db, enriched_id)
        report = recheck_stored_enrichments(db._conn, _store(db), dry_run=True)
        assert report.rows_changed == 1
        assert _row(db, enriched_id) == before
        assert db._conn.execute("SELECT COUNT(*) FROM EnrichmentFactSupport").fetchone()[0] == 0

    def test_no_supported_fact_means_no_reliability(self, db):
        enriched_id = _old_row(db, _answer(formal_entity_name="Zeta Mining Corporation",
                                           earliest_event_date="2024-02-02", victim_count=275_000_000,
                                           threat_actor=None, threat_actor_confidence=None,
                                           attack_method=None, attack_method_confidence=None),
                               content="A breach notice with no details. " * 10)
        recheck_stored_enrichments(db._conn, _store(db), dry_run=False)
        row = _row(db, enriched_id)
        assert row["data_source_reliability"] is None and not row["perplexity_validated"]


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------

class TestPerplexityModelSelection:
    def test_engine_uses_and_records_the_configured_model(self, monkeypatch):
        import importlib
        token_tracker = importlib.import_module("cyber_data_collector.utils.token_tracker")

        monkeypatch.setenv("PERPLEXITY_MODEL", "sonar-test-model")
        recorded: List[str] = []
        monkeypatch.setattr(token_tracker.tracker, "record", lambda model, *a, **k: recorded.append(model))
        sent: Dict[str, Any] = {}

        def create(**kwargs):
            sent.update(kwargs)
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=5, completion_tokens=7),
                                   choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))])

        engine = PerplexityEnrichmentEngine.__new__(PerplexityEnrichmentEngine)
        engine.logger = __import__("logging").getLogger("t")
        engine.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        engine.max_retries, engine.base_delay, engine.max_delay = 0, 0, 0
        asyncio.run(engine._query_perplexity_with_retry("prompt"))
        assert sent["model"] == "sonar-test-model" and recorded == ["sonar-test-model"]

    def test_engine_fails_loudly_without_a_key(self, monkeypatch):
        monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
        from cyber_data_collector.processing import perplexity_enrichment as pe

        class _NoKeyConfig:
            def load(self):
                return {}

        monkeypatch.setattr(pe, "ConfigManager", _NoKeyConfig)
        with pytest.raises(RuntimeError, match="PERPLEXITY_API_KEY"):
            PerplexityEnrichmentEngine()

    def test_budget_refusal_is_not_retried(self):
        attempts: List[int] = []

        def create(**kwargs):
            attempts.append(1)
            raise BudgetExhausted("API budget exhausted for perplexity (0 requests this run)")

        engine = PerplexityEnrichmentEngine.__new__(PerplexityEnrichmentEngine)
        engine.logger = __import__("logging").getLogger("t")
        engine.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        engine.max_retries, engine.base_delay, engine.max_delay = 3, 0, 0
        with pytest.raises(BudgetExhausted):
            asyncio.run(engine._query_perplexity_with_retry("prompt"))
        assert len(attempts) == 1

    def test_unparseable_duplicate_check_raises_instead_of_inventing_an_answer(self):
        engine = PerplexityEnrichmentEngine.__new__(PerplexityEnrichmentEngine)
        engine.logger = __import__("logging").getLogger("t")
        with pytest.raises(ValueError):
            engine._parse_duplicate_check_response("not json")

    def test_missing_detail_enricher_uses_the_configured_model_and_raises_on_api_errors(self, monkeypatch):
        from cyber_data_collector.processing.perplexity_enricher import PerplexityEventEnricher

        monkeypatch.setenv("PERPLEXITY_MODEL", "sonar-test-model")
        enricher = PerplexityEventEnricher(":memory:", "key")
        sent: Dict[str, Any] = {}

        def post(url, json=None, timeout=None):
            sent.update(json)
            return SimpleNamespace(status_code=500, text="upstream")

        monkeypatch.setattr(enricher.session, "post", post)
        event = {"title": "t", "summary": "s", "event_date": "2025-01-01"}
        with pytest.raises(RuntimeError, match="500"):
            asyncio.run(enricher.query_perplexity_for_details(event, ["severity"]))
        assert sent["model"] == "sonar-test-model"

    def test_entity_size_research_model_follows_the_setting(self, monkeypatch):
        from cyber_data_collector.dedup.entity_size import EntitySizeResearcher

        monkeypatch.setenv("PERPLEXITY_MODEL", "sonar-test-model")
        assert EntitySizeResearcher(perplexity_key="k").research_model == "sonar-test-model"

    def test_no_perplexity_model_is_hard_coded_outside_llm_models(self):
        offenders = []
        for path in (ROOT / "cyber_data_collector").rglob("*.py"):
            if "tests" in path.parts or path.name in ("llm_models.py", "token_tracker.py"):
                continue
            if re.search(r"""["']sonar(-[a-z-]+)?["']""", path.read_text(encoding="utf-8")):
                offenders.append(str(path))
        for path in (ROOT / "scripts").rglob("*.py"):
            if re.search(r"""["']sonar(-[a-z-]+)?["']""", path.read_text(encoding="utf-8")):
                offenders.append(str(path))
        assert offenders == []


class TestPromptEntity:
    def test_the_prompt_gets_linked_organisations_never_a_guessed_word(self, db):
        enriched_id = _record(db)
        processor = _processor(db, [_answer(), _answer()])
        [event] = processor.get_events_needing_enrichment()
        asyncio.run(processor.enrich_event(event))
        assert processor.perplexity_engine.entities == [None]  # nothing linked: no entity claimed

        with db._lock:
            db._conn.execute("INSERT INTO EntitiesV2 (entity_name) VALUES ('Acme Logistics')")
            db._conn.execute("INSERT INTO EnrichedEventEntities VALUES (?, 1, 'victim', 0.9)", (enriched_id,))
            db._conn.commit()
        asyncio.run(processor.enrich_event(event))
        assert processor.perplexity_engine.entities[-1] == "Acme Logistics"
