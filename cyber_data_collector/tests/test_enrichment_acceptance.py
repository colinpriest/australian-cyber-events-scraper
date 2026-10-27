"""Enrichment must rest on the evidence actually scraped, and nothing is
accepted - or allowed to stop a retry - without passing an explicit gate.

Covers the three reported defects:

1. classification/enrichment read the pre-scrape in-memory objects, so a search
   snippet (a site menu, a privacy policy clause) was judged as the article;
2. a filter rejection did not stop the record;
3. a malformed or empty Perplexity answer was stored as validated with a
   hard-coded reliability of 0.85, so it was never enriched again.

Every external service (Perplexity, OpenAI, Playwright) is a fake.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from cyber_data_collector.models.events import (
    ConfidenceScore,
    CyberEvent,
    CyberEventType,
    EventSeverity,
    EventSource,
)
from cyber_data_collector.pipelines import discovery
from cyber_data_collector.processing.enrichment_gate import (
    MAX_ENRICHMENT_ATTEMPTS,
    evaluate_enrichment,
    plan_enrichment_update,
    validate_confidence,
    validate_count,
    validate_iso_date,
)
from cyber_data_collector.processing.llm_classifier import (
    SNIPPET_ONLY_CONFIDENCE_CAP,
    EventEnhancement,
    LLMClassifier,
)
from cyber_data_collector.processing.perplexity_enrichment import (
    PerplexityEnrichmentEngine,
    PerplexityEventEnrichment,
)
from cyber_data_collector.processing.record_evidence import (
    BASIS_ARTICLE,
    BASIS_NONE,
    BASIS_SNIPPET_ONLY,
    MIN_ARTICLE_CHARS,
    AcceptanceLedger,
    RecordEvidence,
    persist_rejections,
    prepare_records_for_processing,
)
from cyber_data_collector.storage.cyber_event_data_v2 import CyberEventDataV2
from scripts.perplexity_backfill_events import PerplexityBackfillProcessor

ARTICLE = (
    "Acme Logistics Pty Ltd confirmed on 14 March 2025 that attackers encrypted its "
    "warehouse systems and stole the details of 120,000 customers. The company said "
    "the LockBit group claimed responsibility for the ransomware attack. "
) * 2
SNIPPET = "Cyber incident | Report a cyber incident | Privacy policy: we may notify of a data breach"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

RAW_EVENTS_DDL = """
CREATE TABLE RawEvents (
    raw_event_id TEXT PRIMARY KEY, source_type VARCHAR(50), source_event_id TEXT,
    raw_title TEXT, raw_description TEXT, raw_content TEXT, event_date DATE,
    source_url VARCHAR(2048), source_metadata TEXT,
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, is_processed BOOLEAN DEFAULT FALSE,
    processing_attempted_at TIMESTAMP, processing_error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)
"""
# Deliberately without the attempt-tracking columns: opening the database must
# add them (the migration under test).
ENRICHED_EVENTS_DDL = """
CREATE TABLE EnrichedEvents (
    enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title VARCHAR(255),
    description TEXT, summary TEXT, event_type VARCHAR(50), severity VARCHAR(20),
    event_date DATE, records_affected BIGINT, is_australian_event BOOLEAN,
    is_specific_event BOOLEAN, confidence_score REAL, australian_relevance_score REAL,
    status VARCHAR(20) DEFAULT 'Active', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, perplexity_validated BOOLEAN DEFAULT FALSE,
    perplexity_validated_at TIMESTAMP, perplexity_enrichment_data TEXT, date_confidence REAL,
    entity_confidence REAL, data_source_reliability REAL, attacking_entity_id INTEGER,
    attacking_entity_name VARCHAR(255), attack_method VARCHAR(100))
"""
PROCESSING_LOG_DDL = """
CREATE TABLE ProcessingLog (
    log_id TEXT PRIMARY KEY, raw_event_id TEXT, processing_stage TEXT, status TEXT,
    result_data TEXT, error_message TEXT, processing_time_ms INTEGER, created_at TIMESTAMP)
"""


def _make_db(path) -> CyberEventDataV2:
    conn = sqlite3.connect(path)
    conn.executescript(RAW_EVENTS_DDL + ";" + ENRICHED_EVENTS_DDL + ";" + PROCESSING_LOG_DDL)
    conn.commit()
    conn.close()
    return CyberEventDataV2(path)


@pytest.fixture
def db(tmp_path):
    database = _make_db(tmp_path / "t.db")
    yield database
    database.close()


def _add_record(
    db: CyberEventDataV2,
    *,
    title: str = "Acme Logistics breach",
    description: str = SNIPPET,
    content: Optional[str] = None,
    url: str = "https://news.example.com.au/acme",
    source_type: str = "GoogleSearch",
    discovered: Optional[date] = None,
) -> str:
    raw_id = db.add_raw_event(source_type, {
        "title": title, "description": description, "content": content,
        "event_date": "2025-03-14", "source_url": url, "metadata": {},
    })
    if discovered is not None:
        with db._lock:
            db._conn.execute("UPDATE RawEvents SET discovered_at = ? WHERE raw_event_id = ?",
                             (discovered.isoformat() + "T09:00:00", raw_id))
            db._conn.commit()
    return raw_id


def _add_enriched(db: CyberEventDataV2, raw_id: str, **columns: Any) -> str:
    enriched_id = db.create_enriched_event(raw_id, {
        "title": "Acme Logistics breach", "description": "d", "event_date": "2025-03-14",
        "is_australian_event": True, "is_specific_event": True, "confidence_score": 0.7,
    })
    if columns:
        sets = ", ".join(f"{k} = ?" for k in columns)
        with db._lock:
            db._conn.execute(f"UPDATE EnrichedEvents SET {sets} WHERE enriched_event_id = ?",
                             [*columns.values(), enriched_id])
            db._conn.commit()
    return enriched_id


def _row(db: CyberEventDataV2, enriched_id: str) -> Dict[str, Any]:
    with db._lock:
        cur = db._conn.cursor()
        cur.execute(
            "SELECT CAST(event_date AS TEXT) AS event_date, date_confidence, entity_confidence, "
            "attacking_entity_name, attack_method, records_affected, perplexity_validated, "
            "perplexity_enrichment_data, data_source_reliability, perplexity_attempt_count, "
            "perplexity_last_error, perplexity_status, perplexity_last_attempt_at "
            "FROM EnrichedEvents WHERE enriched_event_id = ?", (enriched_id,))
        return dict(cur.fetchone())


def _good_answer(**overrides: Any) -> PerplexityEventEnrichment:
    fields: Dict[str, Any] = dict(
        earliest_event_date="2025-03-14", date_confidence=0.9,
        formal_entity_name="Acme Logistics Pty Ltd", entity_confidence=0.85,
        victim_industry="Transportation", threat_actor="LockBit", threat_actor_confidence=0.7,
        attack_method="ransomware", attack_method_confidence=0.8,
        victim_count=120000, victim_count_confidence=0.75,
        sources_consulted=["https://www.example.com/acme-breach"], overall_confidence=0.8,
    )
    fields.update(overrides)
    return PerplexityEventEnrichment(**fields)


def _make_event(raw_id: Optional[str], title: str = "Acme Logistics breach",
                description: str = SNIPPET, url: str = "https://news.example.com.au/acme") -> CyberEvent:
    return CyberEvent(
        title=title, description=description, event_type=CyberEventType.OTHER,
        severity=EventSeverity.UNKNOWN, australian_relevance=True,
        confidence=ConfidenceScore(overall=0.8, source_reliability=0.8, data_completeness=0.5,
                                   temporal_accuracy=0.5, geographic_accuracy=0.5),
        data_sources=[EventSource(source_id="s1", source_type="Google Search", url=url,
                                  content_snippet=description, credibility_score=0.5,
                                  relevance_score=0.5)],
        raw_event_id=raw_id,
    )


# ---------------------------------------------------------------------------
# Acceptance gate: primitives
# ---------------------------------------------------------------------------

class TestGatePrimitives:
    def test_iso_date_accepted(self):
        parsed, problem = validate_iso_date("2025-03-14", date(2025, 6, 1))
        assert parsed == date(2025, 3, 14) and problem is None

    @pytest.mark.parametrize("bad", ["March 2025", "2025-13-01", "2025-02-30", "14/03/2025",
                                     "2025-3-4", "", "not a date", 20250314])
    def test_non_iso_or_impossible_dates_rejected(self, bad):
        parsed, problem = validate_iso_date(bad, date(2026, 1, 1))
        assert parsed is None and problem

    def test_date_after_discovery_rejected(self):
        discovered = date(2025, 3, 20)
        parsed, problem = validate_iso_date("2025-04-30", discovered)
        assert parsed is None and "after discovery" in problem

    def test_date_in_the_future_rejected_even_with_late_reference(self):
        tomorrow_plus = (date.today() + timedelta(days=40)).isoformat()
        parsed, problem = validate_iso_date(tomorrow_plus, date.today() + timedelta(days=400))
        assert parsed is None and problem

    def test_absurdly_old_date_rejected(self):
        parsed, problem = validate_iso_date("1899-01-01", date(2025, 1, 1))
        assert parsed is None and "before" in problem

    @pytest.mark.parametrize("bad", [1.4, -0.1, float("nan"), float("inf"), "0.9", True, None])
    def test_confidence_must_be_number_in_unit_interval(self, bad):
        value, problem = validate_confidence(bad)
        assert value is None and problem

    @pytest.mark.parametrize("good", [0, 0.0, 0.5, 1, 1.0])
    def test_confidence_bounds_accepted(self, good):
        value, problem = validate_confidence(good)
        assert value == float(good) and problem is None

    @pytest.mark.parametrize("bad", [-1, -5000, 12.5, True, "1200", float("nan"), None])
    def test_counts_must_be_non_negative_integers(self, bad):
        value, problem = validate_count(bad)
        assert value is None and problem

    def test_zero_and_whole_floats_are_counts(self):
        assert validate_count(0) == (0, None)
        assert validate_count(1200.0) == (1200, None)


# ---------------------------------------------------------------------------
# Acceptance gate: whole answers
# ---------------------------------------------------------------------------

class TestEvaluateEnrichment:
    def test_good_answer_is_accepted_with_computed_reliability(self):
        result = evaluate_enrichment(_good_answer(), record_text=ARTICLE, reference_date=date(2025, 6, 1))
        assert result.is_accepted
        assert set(result.accepted_facts) >= {"date", "entity", "victim_count", "attack_method"}
        # Mean fact confidence (0.9, .85, .7, .8, .75 = 0.8), averaged with overall 0.8.
        assert result.reliability == pytest.approx(0.8, abs=0.001)
        assert result.reliability != 0.85

    def test_reliability_tracks_the_accepted_facts_not_a_constant(self):
        weak = evaluate_enrichment(
            _good_answer(date_confidence=0.2, entity_confidence=0.2, threat_actor_confidence=0.2,
                         attack_method_confidence=0.2, victim_count_confidence=0.2, overall_confidence=0.2),
            record_text=ARTICLE, reference_date=date(2025, 6, 1))
        assert weak.is_accepted and weak.reliability == pytest.approx(0.2, abs=0.001)

    def test_future_date_is_dropped_but_other_facts_survive(self):
        answer = _good_answer(earliest_event_date="2025-12-25")
        result = evaluate_enrichment(answer, record_text=ARTICLE, reference_date=date(2025, 6, 1))
        assert result.is_accepted
        assert "date" in result.rejected_facts
        assert result.sanitized.earliest_event_date is None
        assert result.sanitized.date_confidence is None
        assert result.sanitized.formal_entity_name == "Acme Logistics Pty Ltd"

    def test_out_of_range_fact_confidence_drops_that_fact(self):
        result = evaluate_enrichment(_good_answer(entity_confidence=1.7), record_text=ARTICLE,
                                     reference_date=date(2025, 6, 1))
        assert "entity" in result.rejected_facts
        assert result.sanitized.formal_entity_name is None

    def test_missing_fact_confidence_drops_that_fact(self):
        result = evaluate_enrichment(_good_answer(threat_actor_confidence=None), record_text=ARTICLE,
                                     reference_date=date(2025, 6, 1))
        assert "threat_actor" in result.rejected_facts

    def test_out_of_range_overall_confidence_rejects_the_whole_answer(self):
        result = evaluate_enrichment(_good_answer(overall_confidence=3.0), record_text=ARTICLE,
                                     reference_date=date(2025, 6, 1))
        assert not result.is_accepted and "overall" in result.failure_reason

    def test_negative_count_is_dropped(self):
        result = evaluate_enrichment(_good_answer(victim_count=-40), record_text=ARTICLE,
                                     reference_date=date(2025, 6, 1))
        assert "victim_count" in result.rejected_facts
        assert result.sanitized.victim_count is None

    def test_fact_with_no_source_and_not_in_the_record_is_dropped(self):
        answer = _good_answer(sources_consulted=[], formal_entity_name="Zeta Mining Corp",
                              victim_count=9999999)
        result = evaluate_enrichment(answer, record_text=ARTICLE, reference_date=date(2025, 6, 1))
        assert "entity" in result.rejected_facts and "victim_count" in result.rejected_facts
        # The date, actor and method are in the article, so they stand on the record's own content.
        assert {"date", "threat_actor"} <= set(result.accepted_facts)

    def test_fact_without_a_source_is_kept_when_the_record_text_contains_it(self):
        answer = _good_answer(sources_consulted=[])
        result = evaluate_enrichment(answer, record_text=ARTICLE, reference_date=date(2025, 6, 1))
        assert {"entity", "victim_count", "date"} <= set(result.accepted_facts)

    def test_placeholder_urls_do_not_count_as_a_source(self):
        answer = _good_answer(sources_consulted=["url1", "example", "ftp://x.y"],
                              formal_entity_name="Zeta Mining Corp")
        result = evaluate_enrichment(answer, record_text=ARTICLE, reference_date=date(2025, 6, 1))
        assert "entity" in result.rejected_facts
        assert result.sanitized.sources_consulted == []

    def test_parse_failure_is_rejected_not_an_empty_success(self):
        failed = PerplexityEventEnrichment(parse_failed=True, failure_reason="unparseable response")
        result = evaluate_enrichment(failed, record_text=ARTICLE)
        assert not result.is_accepted
        assert result.failure_reason == "unparseable response"
        assert result.reliability is None

    def test_empty_answer_is_rejected(self):
        result = evaluate_enrichment(PerplexityEventEnrichment(overall_confidence=0.0), record_text=ARTICLE)
        assert not result.is_accepted and result.reliability is None

    def test_answer_whose_every_fact_fails_is_rejected(self):
        answer = PerplexityEventEnrichment(earliest_event_date="2099-01-01", date_confidence=0.9,
                                           sources_consulted=["https://a.example.com/x"],
                                           overall_confidence=0.9)
        result = evaluate_enrichment(answer, record_text=ARTICLE, reference_date=date(2025, 6, 1))
        assert not result.is_accepted and "date" in result.failure_reason


class TestEngineParsing:
    def _engine(self) -> PerplexityEnrichmentEngine:
        engine = PerplexityEnrichmentEngine.__new__(PerplexityEnrichmentEngine)
        engine.logger = logging.getLogger("test")
        return engine

    def test_unparseable_response_is_marked_failed(self):
        result = self._engine()._parse_enrichment_response("this is not json at all")
        assert result.parse_failed and result.failure_reason
        assert evaluate_enrichment(result, record_text=ARTICLE).is_accepted is False

    def test_non_object_json_is_marked_failed(self):
        result = self._engine()._parse_enrichment_response('["a", "b"]')
        assert result.parse_failed

    def test_one_invalid_field_does_not_discard_the_valid_ones(self):
        raw = ('{"formal_entity_name": "Acme Logistics Pty Ltd", "entity_confidence": 0.9, '
               '"victim_count": 12.5, "victim_count_confidence": 0.8, "overall_confidence": 0.7}')
        result = self._engine()._parse_enrichment_response(raw)
        assert not result.parse_failed
        assert result.formal_entity_name == "Acme Logistics Pty Ltd"
        assert result.victim_count is None and "victim_count" in result.dropped_fields

    def test_request_error_surfaces_as_failed_not_none(self):
        engine = self._engine()
        engine.client = object()

        async def _no_wait():
            return None

        async def _boom(prompt, max_tokens=1500):
            raise RuntimeError("503 upstream")

        engine._rate_limit = _no_wait
        engine._query_perplexity_with_retry = _boom
        result = asyncio.run(engine.enrich_event("t", "d"))
        assert result.parse_failed and "503" in result.failure_reason

    def test_prompt_quotes_the_scraped_article_not_the_snippet(self):
        engine = self._engine()
        evidence = RecordEvidence.from_row({"raw_event_id": "r", "raw_title": "t",
                                            "raw_description": SNIPPET, "raw_content": ARTICLE,
                                            "source_url": "https://news.example.com.au/acme"})
        prompt = engine._build_enrichment_prompt("t", "processed description", None, None, evidence=evidence)
        assert "ARTICLE TEXT (scraped from https://news.example.com.au/acme" in prompt
        assert "LockBit" in prompt
        assert "processed description" not in prompt
        assert SNIPPET not in prompt

    def test_prompt_says_plainly_when_there_is_no_article(self):
        engine = self._engine()
        evidence = RecordEvidence.from_row({"raw_event_id": "r", "raw_title": "t",
                                            "raw_description": SNIPPET, "raw_content": None,
                                            "source_url": "https://news.example.com.au/acme"})
        prompt = engine._build_enrichment_prompt("t", "d", None, None, evidence=evidence)
        assert "NO ARTICLE TEXT WAS RETRIEVED" in prompt
        assert "ARTICLE TEXT (scraped" not in prompt


# ---------------------------------------------------------------------------
# Persisted evidence
# ---------------------------------------------------------------------------

class TestRecordEvidence:
    def test_bases(self):
        base = {"raw_event_id": "r", "raw_title": "t", "source_url": "u"}
        assert RecordEvidence.from_row({**base, "raw_description": SNIPPET, "raw_content": ARTICLE}).basis == BASIS_ARTICLE
        short = RecordEvidence.from_row({**base, "raw_description": SNIPPET, "raw_content": "x" * 80})
        assert short.basis == BASIS_SNIPPET_ONLY and "too short" in short.reason
        assert RecordEvidence.from_row({**base, "raw_description": SNIPPET, "raw_content": None}).basis == BASIS_SNIPPET_ONLY
        assert RecordEvidence.from_row({**base, "raw_description": "", "raw_content": None}).basis == BASIS_NONE

    def test_threshold_is_the_documented_one(self):
        row = {"raw_event_id": "r", "raw_title": "t", "raw_description": "", "source_url": "u"}
        assert RecordEvidence.from_row({**row, "raw_content": "x" * MIN_ARTICLE_CHARS}).has_article
        assert not RecordEvidence.from_row({**row, "raw_content": "x" * (MIN_ARTICLE_CHARS - 1)}).has_article

    def test_prepare_attaches_persisted_article_not_the_in_memory_snippet(self, db):
        raw_id = _add_record(db, content=None)
        event = _make_event(raw_id)
        # Scraping happens after the in-memory object was built.
        with db._lock:
            db._conn.execute("UPDATE RawEvents SET raw_content = ? WHERE raw_event_id = ?", (ARTICLE, raw_id))
            db._conn.commit()
        assert event.evidence_text is None  # the pre-scrape object knows nothing of it

        [prepared] = prepare_records_for_processing(db, [event], AcceptanceLedger())
        assert prepared.evidence_basis == BASIS_ARTICLE
        assert prepared.evidence_text == ARTICLE.strip()
        assert prepared is not event

    def test_prepare_marks_unscraped_records_snippet_only(self, db):
        raw_id = _add_record(db, content=None)
        [prepared] = prepare_records_for_processing(db, [_make_event(raw_id)], AcceptanceLedger())
        assert prepared.evidence_basis == BASIS_SNIPPET_ONLY and prepared.evidence_text is None

    def test_prepare_drops_ledger_rejections_and_unstored_events(self, db):
        kept, rejected = _add_record(db, content=ARTICLE), _add_record(db, title="Other", content=ARTICLE)
        ledger = AcceptanceLedger()
        ledger.reject(rejected, "rf_content_filter", "non-cyber")
        out = prepare_records_for_processing(
            db, [_make_event(kept), _make_event(rejected, title="Other"), _make_event(None, title="never stored")],
            ledger)
        assert [e.raw_event_id for e in out] == [kept]

    def test_persisted_rejection_holds_on_a_later_run(self, db):
        raw_id = _add_record(db, content=ARTICLE)
        first = AcceptanceLedger()
        first.reject(raw_id, "rf_content_filter", "non-cyber")
        assert persist_rejections(db, first) == 1

        later = AcceptanceLedger()
        assert prepare_records_for_processing(db, [_make_event(raw_id)], later) == []
        assert later.get(raw_id).stage == "rf_content_filter"
        assert persist_rejections(db, later) == 0  # read back, not rewritten

    def test_rejected_record_leaves_the_enrichment_queue(self, db):
        raw_id = _add_record(db, content=ARTICLE, url="https://news.example.com.au/acme")
        ledger = AcceptanceLedger()
        ledger.reject(raw_id, "rf_url_prefilter", "non-cyber")
        persist_rejections(db, ledger)
        queue = db.get_raw_events_for_processing(australian_only=False)
        assert raw_id not in [r["raw_event_id"] for r in queue]


# ---------------------------------------------------------------------------
# Classification sees the scraped content (OpenAI faked)
# ---------------------------------------------------------------------------

class _FakeOpenAI:
    """Stands in for the instructor-wrapped client; records every prompt."""

    def __init__(self, response: Any = None, error: Optional[Exception] = None):
        self.prompts: List[str] = []
        self._response = response
        self._error = error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.prompts.append(kwargs["messages"][-1]["content"])
        if self._error:
            raise self._error
        return self._response


def _accepting_response() -> EventEnhancement:
    return EventEnhancement(is_cybersecurity_event=True, is_australian_relevant=True,
                            event_type=CyberEventType.RANSOMWARE, severity=EventSeverity.HIGH,
                            detailed_description="Acme was hit by ransomware.")


class TestClassifierReceivesPersistedEvidence:
    def _classifier(self, client: _FakeOpenAI) -> LLMClassifier:
        classifier = LLMClassifier(None)
        classifier.client = client
        return classifier

    def test_prompt_contains_the_scraped_article(self, db):
        raw_id = _add_record(db, content=ARTICLE)
        [prepared] = prepare_records_for_processing(db, [_make_event(raw_id)], AcceptanceLedger())
        client = _FakeOpenAI(_accepting_response())
        out = asyncio.run(self._classifier(client).classify_events([prepared]))

        prompt = client.prompts[0]
        assert "ARTICLE TEXT (scraped from the source page" in prompt
        assert "LockBit group claimed responsibility" in prompt
        assert "NO ARTICLE TEXT" not in prompt
        assert out[0].classification_status == "classified"

    def test_snippet_only_record_is_labelled_and_confidence_capped(self, db):
        raw_id = _add_record(db, content=None)
        [prepared] = prepare_records_for_processing(db, [_make_event(raw_id)], AcceptanceLedger())
        client = _FakeOpenAI(_accepting_response())
        out = asyncio.run(self._classifier(client).classify_events([prepared]))

        prompt = client.prompts[0]
        assert "NO ARTICLE TEXT WAS RETRIEVED" in prompt
        assert "ARTICLE TEXT (scraped" not in prompt
        assert "menu link" in prompt  # the warning about boilerplate quoted as incidents
        assert out[0].confidence.overall <= SNIPPET_ONLY_CONFIDENCE_CAP

    def test_llm_error_is_not_an_acceptance(self, db):
        raw_id = _add_record(db, content=ARTICLE)
        [prepared] = prepare_records_for_processing(db, [_make_event(raw_id)], AcceptanceLedger())
        client = _FakeOpenAI(error=RuntimeError("openai down"))
        out = asyncio.run(self._classifier(client).classify_events([prepared]))
        assert [e.classification_status for e in out] == ["failed"]


# ---------------------------------------------------------------------------
# Discovery: scrape -> persist -> classify from the stored record
# (Playwright, Random Forest and the collector are faked)
# ---------------------------------------------------------------------------

class _FakeFilter:
    """RF filter: anything whose content mentions 'recipe' is non-cyber."""

    def should_keep_event(self, source_type, title, description, content, url, metadata=None):
        relevant = "recipe" not in (content or "").lower() and "recipe" not in (title or "").lower()
        return SimpleNamespace(is_cyber_relevant=relevant, confidence_score=0.9 if relevant else 0.1,
                               reasoning="fake", stage="fake", risk_level="low")


class _FakeScraper:
    pages: Dict[str, str] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_page_text(self, url, event_date=None, return_metadata=False):
        text = self.pages.get(url, "")
        return (text, {"perplexity_attempted": False, "perplexity_succeeded": False}) if return_metadata else text

    async def _perplexity_fallback(self, url, event_date):
        return None


class _FakeCollector:
    def __init__(self):
        self.received: List[CyberEvent] = []

    async def _process_events(self, events):
        self.received = list(events)
        return list(events)


@pytest.fixture
def pipeline(db, monkeypatch):
    instance = discovery.EventDiscoveryEnrichmentPipeline.__new__(discovery.EventDiscoveryEnrichmentPipeline)
    instance.db = db
    instance.filter_system = _FakeFilter()
    instance.stats = {"errors": 0}
    monkeypatch.setattr(discovery, "PlaywrightScraper", _FakeScraper)
    return instance


class TestDiscoveryHonoursScrapedContentAndRejections:
    def test_classification_input_is_the_scraped_article(self, pipeline, db):
        raw_id = _add_record(db, content=None, url="https://news.example.com.au/acme")
        _FakeScraper.pages = {"https://news.example.com.au/acme": ARTICLE}
        event = _make_event(raw_id)  # built before scraping, carries only the snippet
        ledger = AcceptanceLedger()

        scraped = asyncio.run(pipeline._scrape_raw_events_for_month([raw_id], ledger))
        collector = _FakeCollector()
        out = asyncio.run(pipeline._process_persisted_records(collector, [event], ledger))

        assert scraped == 1 and len(out) == 1
        [seen] = collector.received
        assert seen.evidence_basis == BASIS_ARTICLE
        assert seen.evidence_text == ARTICLE.strip()
        assert event.evidence_text is None  # the original object was not the source

    def test_failed_scrape_reaches_classification_labelled_as_snippet(self, pipeline, db):
        raw_id = _add_record(db, content=None, url="https://news.example.com.au/blocked-by-paywall")
        _FakeScraper.pages = {}  # nothing retrievable
        ledger = AcceptanceLedger()
        asyncio.run(pipeline._scrape_raw_events_for_month([raw_id], ledger))
        collector = _FakeCollector()
        asyncio.run(pipeline._process_persisted_records(collector, [_make_event(raw_id)], ledger))

        [seen] = collector.received
        assert seen.evidence_basis == BASIS_SNIPPET_ONLY
        assert seen.evidence_text is None  # the snippet is not passed off as the article

    def test_content_filter_rejection_stops_the_record(self, pipeline, db):
        good = _add_record(db, title="Acme breach", content=None, url="https://news.example.com.au/acme")
        junk = _add_record(db, title="Help centre", content=None, url="https://help.example.com.au/page")
        _FakeScraper.pages = {
            "https://news.example.com.au/acme": ARTICLE,
            "https://help.example.com.au/page": "A recipe for banana bread. " * 20,
        }
        events = [_make_event(good, title="Acme breach"), _make_event(junk, title="Help centre")]
        ledger = AcceptanceLedger()

        asyncio.run(pipeline._scrape_raw_events_for_month([good, junk], ledger))
        collector = _FakeCollector()
        out = asyncio.run(pipeline._process_persisted_records(collector, events, ledger))

        assert [e.raw_event_id for e in collector.received] == [good]
        assert [e.raw_event_id for e in out] == [good]
        assert ledger.get(junk).stage == "rf_content_filter"
        # ... and the rejection is durable: processed, marked, and out of the queue.
        with db._lock:
            row = db._conn.execute(
                "SELECT is_processed, processing_error FROM RawEvents WHERE raw_event_id = ?", (junk,)
            ).fetchone()
        assert row["is_processed"] and row["processing_error"].startswith("Rejected at rf_content_filter")
        assert junk not in [r["raw_event_id"] for r in db.get_raw_events_for_processing(australian_only=False)]

    def test_blocked_domain_stops_the_record(self, pipeline, db):
        raw_id = _add_record(db, content=None, url="https://www.linkedin.com/in/someone")
        ledger = AcceptanceLedger()
        asyncio.run(pipeline._scrape_raw_events_for_month([raw_id], ledger))
        collector = _FakeCollector()
        out = asyncio.run(pipeline._process_persisted_records(collector, [_make_event(raw_id)], ledger))
        assert out == [] and collector.received == []
        assert ledger.get(raw_id).stage == "domain_blocklist"

    def test_rejection_from_an_earlier_run_still_stops_the_record(self, pipeline, db):
        raw_id = _add_record(db, content=ARTICLE)
        earlier = AcceptanceLedger()
        earlier.reject(raw_id, "rf_content_filter", "non-cyber")
        persist_rejections(db, earlier)

        collector = _FakeCollector()
        out = asyncio.run(pipeline._process_persisted_records(collector, [_make_event(raw_id)], AcceptanceLedger()))
        assert out == []


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

class TestMigration:
    def test_tracking_columns_added_once_and_idempotent(self, tmp_path):
        path = tmp_path / "m.db"
        first = _make_db(path)
        first.close()
        second = CyberEventDataV2(path)  # reopening must not fail or duplicate
        with second._lock:
            cols = [r[1] for r in second._conn.execute("PRAGMA table_info(EnrichedEvents)")]
        second.close()
        for name in ("perplexity_attempt_count", "perplexity_last_attempt_at",
                     "perplexity_last_error", "perplexity_status"):
            assert cols.count(name) == 1

    def test_existing_rows_start_with_zero_attempts(self, tmp_path):
        path = tmp_path / "m.db"
        conn = sqlite3.connect(path)
        conn.executescript(RAW_EVENTS_DDL + ";" + ENRICHED_EVENTS_DDL + ";" + PROCESSING_LOG_DDL)
        conn.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title) VALUES ('e1','r1','t')")
        conn.commit()
        conn.close()
        database = CyberEventDataV2(path)
        row = database.get_enrichment_row("e1")
        database.close()
        assert row["perplexity_attempt_count"] == 0 and row["perplexity_status"] is None


# ---------------------------------------------------------------------------
# Backfill: enrichment sees scraped content; failures retried then quarantined
# ---------------------------------------------------------------------------

class _FakePerplexityEngine:
    """Returns queued answers; remembers what evidence it was shown."""

    client = object()

    def __init__(self, answers: List[Any]):
        self.answers = list(answers)
        self.calls: List[Dict[str, Any]] = []

    async def enrich_event(self, title, description, current_date=None, current_entity=None, evidence=None):
        self.calls.append({"title": title, "description": description, "evidence": evidence})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _run(processor: PerplexityBackfillProcessor) -> Dict[str, int]:
    events = processor.get_events_needing_enrichment()
    return asyncio.run(processor.enrich_events_concurrent(events, max_concurrent=2))


def _malformed() -> PerplexityEventEnrichment:
    return PerplexityEventEnrichment(overall_confidence=0.0, parse_failed=True,
                                     failure_reason="unparseable response: Expecting value")


class TestBackfillEnrichment:
    def test_enrichment_is_shown_the_scraped_article(self, db):
        raw_id = _add_record(db, content=ARTICLE, discovered=date(2025, 6, 1))
        _add_enriched(db, raw_id, description="processed description from the snippet")
        engine = _FakePerplexityEngine([_good_answer()])
        _run(PerplexityBackfillProcessor(db, engine))

        [call] = engine.calls
        assert call["evidence"].basis == BASIS_ARTICLE
        assert call["evidence"].article_text == ARTICLE.strip()
        assert call["evidence"].snippet == SNIPPET

    def test_enrichment_is_told_when_nothing_was_scraped(self, db):
        raw_id = _add_record(db, content=None, discovered=date(2025, 6, 1))
        _add_enriched(db, raw_id)
        engine = _FakePerplexityEngine([_good_answer()])
        _run(PerplexityBackfillProcessor(db, engine))
        assert engine.calls[0]["evidence"].basis == BASIS_SNIPPET_ONLY
        assert engine.calls[0]["evidence"].article_text is None

    def test_accepted_answer_is_validated_with_computed_reliability(self, db):
        raw_id = _add_record(db, content=ARTICLE, discovered=date(2025, 6, 1))
        enriched_id = _add_enriched(db, raw_id)
        counts = _run(PerplexityBackfillProcessor(db, _FakePerplexityEngine([_good_answer()])))

        row = _row(db, enriched_id)
        assert counts == {"enriched": 1, "failed": 0}
        assert row["perplexity_validated"] and row["perplexity_status"] == "validated"
        assert row["data_source_reliability"] == pytest.approx(0.8, abs=0.001)
        assert row["data_source_reliability"] != 0.85
        assert row["perplexity_attempt_count"] == 1 and row["perplexity_last_error"] is None
        assert row["records_affected"] == 120000 and row["attack_method"] == "ransomware"
        assert json.loads(row["perplexity_enrichment_data"])["formal_entity_name"] == "Acme Logistics Pty Ltd"

    def test_rejected_facts_are_not_written(self, db):
        raw_id = _add_record(db, content=ARTICLE, discovered=date(2025, 6, 1))
        enriched_id = _add_enriched(db, raw_id)
        answer = _good_answer(earliest_event_date="2031-01-01", victim_count=-3)
        _run(PerplexityBackfillProcessor(db, _FakePerplexityEngine([answer])))

        row = _row(db, enriched_id)
        stored = json.loads(row["perplexity_enrichment_data"])
        assert row["event_date"] == "2025-03-14"  # untouched
        assert row["records_affected"] is None
        assert stored["earliest_event_date"] is None and stored["victim_count"] is None
        assert stored["formal_entity_name"] == "Acme Logistics Pty Ltd"

    def test_malformed_response_is_not_validated_and_gets_no_default_reliability(self, db):
        raw_id = _add_record(db, content=ARTICLE)
        enriched_id = _add_enriched(db, raw_id)
        counts = _run(PerplexityBackfillProcessor(db, _FakePerplexityEngine([_malformed()])))

        row = _row(db, enriched_id)
        assert counts == {"enriched": 0, "failed": 1}
        assert not row["perplexity_validated"]
        assert row["data_source_reliability"] is None
        assert row["perplexity_enrichment_data"] is None
        assert row["perplexity_attempt_count"] == 1
        assert "unparseable" in row["perplexity_last_error"]
        assert row["perplexity_last_attempt_at"]
        assert row["perplexity_status"] == "retrying"

    def test_empty_answer_is_a_failure_not_a_success(self, db):
        raw_id = _add_record(db, content=ARTICLE)
        enriched_id = _add_enriched(db, raw_id)
        _run(PerplexityBackfillProcessor(db, _FakePerplexityEngine([PerplexityEventEnrichment()])))
        row = _row(db, enriched_id)
        assert not row["perplexity_validated"] and row["data_source_reliability"] is None
        assert row["perplexity_attempt_count"] == 1

    def test_request_exception_is_recorded_as_a_failed_attempt(self, db):
        raw_id = _add_record(db, content=ARTICLE)
        enriched_id = _add_enriched(db, raw_id)
        _run(PerplexityBackfillProcessor(db, _FakePerplexityEngine([RuntimeError("503 upstream")])))
        row = _row(db, enriched_id)
        assert not row["perplexity_validated"] and "503" in row["perplexity_last_error"]

    def test_failed_record_is_retried_on_later_runs_then_quarantined(self, db):
        raw_id = _add_record(db, content=ARTICLE)
        enriched_id = _add_enriched(db, raw_id)
        engine = _FakePerplexityEngine([_malformed() for _ in range(10)])
        processor = PerplexityBackfillProcessor(db, engine)

        for attempt in range(1, MAX_ENRICHMENT_ATTEMPTS + 1):
            queued = processor.get_events_needing_enrichment()
            assert [e["enriched_event_id"] for e in queued] == [enriched_id], f"run {attempt}"
            asyncio.run(processor.enrich_events_concurrent(queued, max_concurrent=1))
            assert _row(db, enriched_id)["perplexity_attempt_count"] == attempt

        row = _row(db, enriched_id)
        assert row["perplexity_status"] == "quarantined"
        assert not row["perplexity_validated"]
        assert processor.stats["quarantined"] == 1
        # Quarantined: no longer retried, but reported.
        assert processor.get_events_needing_enrichment() == []
        assert len(engine.calls) == MAX_ENRICHMENT_ATTEMPTS
        assert [q["enriched_event_id"] for q in db.get_quarantined_enrichments()] == [enriched_id]
        assert [q["enriched_event_id"] for q in processor.report_quarantined()] == [enriched_id]

    def test_a_later_success_clears_the_failure_state(self, db):
        raw_id = _add_record(db, content=ARTICLE, discovered=date(2025, 6, 1))
        enriched_id = _add_enriched(db, raw_id)
        engine = _FakePerplexityEngine([_malformed(), _good_answer()])
        processor = PerplexityBackfillProcessor(db, engine)
        _run(processor)
        _run(processor)
        row = _row(db, enriched_id)
        assert row["perplexity_validated"] and row["perplexity_status"] == "validated"
        assert row["perplexity_last_error"] is None and row["perplexity_attempt_count"] == 2

    def test_release_quarantine_puts_records_back_in_the_queue(self, db):
        raw_id = _add_record(db, content=ARTICLE)
        enriched_id = _add_enriched(db, raw_id)
        for _ in range(MAX_ENRICHMENT_ATTEMPTS):
            db.record_enrichment_failure(enriched_id, "boom")
        processor = PerplexityBackfillProcessor(db, _FakePerplexityEngine([]))
        assert processor.get_events_needing_enrichment() == []
        assert db.release_quarantined_enrichments() == 1
        assert [e["enriched_event_id"] for e in processor.get_events_needing_enrichment()] == [enriched_id]

    def test_dry_run_records_nothing(self, db):
        raw_id = _add_record(db, content=ARTICLE)
        enriched_id = _add_enriched(db, raw_id)
        processor = PerplexityBackfillProcessor(db, _FakePerplexityEngine([_malformed()]), dry_run=True)
        events = processor.get_events_needing_enrichment()
        assert asyncio.run(processor.enrich_event(events[0])) is None
        assert _row(db, enriched_id)["perplexity_attempt_count"] == 0


class TestWeakerNeverReplacesStronger:
    def _accept(self, db, enriched_id, answer, reference=date(2025, 6, 1)):
        gate = evaluate_enrichment(answer, record_text=ARTICLE, reference_date=reference)
        assert gate.is_accepted
        return db.record_enrichment_success(enriched_id, gate)

    def test_lower_confidence_answer_keeps_stored_date_entity_and_count(self, db):
        raw_id = _add_record(db, content=ARTICLE)
        enriched_id = _add_enriched(db, raw_id)
        self._accept(db, enriched_id, _good_answer())  # date 0.9, entity 0.85, count 0.75
        before = _row(db, enriched_id)

        weaker = _good_answer(earliest_event_date="2025-03-20", date_confidence=0.65,
                              formal_entity_name="ACME Logistics", entity_confidence=0.62,
                              victim_count=150000, victim_count_confidence=0.61,
                              threat_actor="BlackCat", threat_actor_confidence=0.6,
                              overall_confidence=0.6)
        outcome = self._accept(db, enriched_id, weaker)
        after = _row(db, enriched_id)

        assert outcome["applied"] == []
        for column in ("event_date", "date_confidence", "entity_confidence",
                       "records_affected", "attacking_entity_name"):
            assert after[column] == before[column], column
        stored = json.loads(after["perplexity_enrichment_data"])
        assert stored["formal_entity_name"] == "Acme Logistics Pty Ltd"
        assert stored["earliest_event_date"] == "2025-03-14"
        assert stored["victim_count"] == 120000

    def test_higher_confidence_answer_does_replace(self, db):
        raw_id = _add_record(db, content=ARTICLE)
        enriched_id = _add_enriched(db, raw_id)
        self._accept(db, enriched_id, _good_answer(date_confidence=0.7))
        outcome = self._accept(db, enriched_id, _good_answer(earliest_event_date="2025-03-15",
                                                               date_confidence=0.95))
        assert "date" in outcome["applied"]
        row = _row(db, enriched_id)
        assert row["event_date"] == "2025-03-15" and row["date_confidence"] == pytest.approx(0.95)

    def test_missing_facts_in_a_new_answer_never_blank_the_stored_ones(self, db):
        raw_id = _add_record(db, content=ARTICLE)
        enriched_id = _add_enriched(db, raw_id)
        self._accept(db, enriched_id, _good_answer())
        sparse = PerplexityEventEnrichment(attack_method="ransomware", attack_method_confidence=0.99,
                                           sources_consulted=["https://www.example.com/x"],
                                           overall_confidence=0.9)
        self._accept(db, enriched_id, sparse)
        stored = json.loads(_row(db, enriched_id)["perplexity_enrichment_data"])
        assert stored["formal_entity_name"] == "Acme Logistics Pty Ltd"
        assert stored["victim_count"] == 120000

    def test_weaker_date_than_the_stored_column_stays_out_of_the_json_too(self, db):
        raw_id = _add_record(db, content=ARTICLE)
        enriched_id = _add_enriched(db, raw_id, date_confidence=0.95)
        self._accept(db, enriched_id, _good_answer(earliest_event_date="2025-04-01", date_confidence=0.7))
        row = _row(db, enriched_id)
        assert row["event_date"] == "2025-03-14" and row["date_confidence"] == pytest.approx(0.95)
        assert json.loads(row["perplexity_enrichment_data"])["earliest_event_date"] is None

    def test_failure_never_touches_a_stored_enrichment(self, db):
        raw_id = _add_record(db, content=ARTICLE)
        enriched_id = _add_enriched(db, raw_id)
        self._accept(db, enriched_id, _good_answer())
        before = _row(db, enriched_id)
        db.record_enrichment_failure(enriched_id, "later attempt failed")
        after = _row(db, enriched_id)
        for column in ("event_date", "date_confidence", "records_affected",
                       "perplexity_enrichment_data", "data_source_reliability", "perplexity_validated"):
            assert after[column] == before[column], column

    def test_plan_refuses_an_unaccepted_result(self):
        with pytest.raises(ValueError):
            plan_enrichment_update({}, evaluate_enrichment(PerplexityEventEnrichment(), record_text=""))


class TestRequeueEmptyValidations:
    def test_old_empty_validations_are_requeued_and_real_ones_kept(self, db):
        empty_raw, real_raw = _add_record(db, title="a"), _add_record(db, title="b")
        empty_id = _add_enriched(
            db, empty_raw, perplexity_validated=1, data_source_reliability=0.85,
            perplexity_enrichment_data=json.dumps(PerplexityEventEnrichment().model_dump()))
        real_id = _add_enriched(
            db, real_raw, perplexity_validated=1, data_source_reliability=0.85,
            perplexity_enrichment_data=json.dumps(_good_answer().model_dump()))

        assert db.requeue_empty_validations() == 1
        assert db.requeue_empty_validations() == 0  # idempotent

        empty, real = _row(db, empty_id), _row(db, real_id)
        assert not empty["perplexity_validated"] and empty["data_source_reliability"] is None
        assert empty["perplexity_enrichment_data"] is None
        assert real["perplexity_validated"] and real["data_source_reliability"] == 0.85
        queued = PerplexityBackfillProcessor(db, _FakePerplexityEngine([])).get_events_needing_enrichment()
        assert [e["enriched_event_id"] for e in queued] == [empty_id]
