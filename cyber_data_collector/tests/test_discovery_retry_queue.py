"""Stored-but-unprocessed records are retried from a durable queue, and a
source-month is complete only when its search and its records are.

Regression for the reviewer finding: a classification timeout left a record
unprocessed, but a refresh only processed records it *rediscovered* (and OAIC
skips stored URLs), so the incident was stranded for good.

Every external service (sources, Playwright, OpenAI, Jev) is a fake.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from cyber_data_collector.dedup import source_gate
from cyber_data_collector.filtering.page_gate import DiscoveryPageGate, PageGateUnavailable
from cyber_data_collector.models.events import (
    ConfidenceScore,
    CyberEvent,
    CyberEventType,
    EventSeverity,
    EventSource,
)
from cyber_data_collector.pipelines import discovery
from cyber_data_collector.processing import retry_queue as rq
from cyber_data_collector.processing.llm_classifier import failed_copy
from cyber_data_collector.storage.cyber_event_data_v2 import CyberEventDataV2
from cyber_data_collector.storage.schema_init import initialize_database
from cyber_data_collector.utils.api_budget import BudgetExhausted

URL = "https://www.oaic.gov.au/news/acme-breach"
ARTICLE = ("Acme Logistics Pty Ltd confirmed on 14 March 2025 that attackers encrypted its warehouse systems "
           "and stole the details of 120,000 customers. ") * 4


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _event(title="Acme Logistics data breach", url=URL) -> CyberEvent:
    return CyberEvent(
        title=title, description="OAIC statement on the Acme Logistics breach",
        event_type=CyberEventType.DATA_BREACH, severity=EventSeverity.MEDIUM, australian_relevance=True,
        event_date=datetime(2025, 3, 14),
        confidence=ConfidenceScore(overall=0.7, source_reliability=0.7, data_completeness=0.5,
                                   temporal_accuracy=0.5, geographic_accuracy=0.5),
        data_sources=[EventSource(source_id="s1", source_type="OAIC", url=url, credibility_score=0.8,
                                  relevance_score=0.8)],
    )


class FakeSource:
    def __init__(self, events, failures=(), raises=None):
        self.events, self.failures, self.raises = list(events), list(failures), raises

    def validate_config(self):
        return True

    @property
    def collection_failures(self):
        return self.failures

    async def collect_events(self, date_range):
        if self.raises:
            raise self.raises
        return [e.model_copy(deep=True) for e in self.events]


class FakeCollector:
    """Stands in for CyberDataCollector; behaviour set per run on the class."""

    sources: Dict[str, Any] = {}
    classify = "ok"          # 'ok' | 'timeout' | 'bad' | 'unclassified'
    received: List[CyberEvent] = []

    def __init__(self, config, env_path=".env", db_path=None):
        self.config = config
        self.data_sources = dict(FakeCollector.sources) if any(
            getattr(config, f).enabled for f in ("perplexity_config", "google_search_config", "webber_config",
                                                 "oaic_config")) else {}

    async def _process_events(self, events):
        FakeCollector.received = list(events)
        if FakeCollector.classify == "timeout":
            return [failed_copy(e, asyncio.TimeoutError()) for e in events]
        if FakeCollector.classify == "bad":
            return [failed_copy(e, ValueError("model output failed validation for this content")) for e in events]
        if FakeCollector.classify == "unclassified":
            return list(events)
        return [e.model_copy(update={"classification_status": "classified"}) for e in events]


class FakeScraper:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_page_text(self, url, event_date=None, return_metadata=False):
        meta = {"perplexity_attempted": False, "perplexity_succeeded": False}
        return (ARTICLE, meta) if return_metadata else ARTICLE

    async def _perplexity_fallback(self, url, event_date):
        return None


class FakeJudge:
    def __init__(self, fail=None):
        self.fail = fail

    def ask(self, subject_type, subject_id, question_version, state, questions):
        if self.fail:
            raise self.fail
        return {qid: SimpleNamespace(value=0.9) for qid in questions}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 27, 9, 0)

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    monkeypatch.setattr(discovery, "CyberDataCollector", FakeCollector)
    monkeypatch.setattr(discovery, "PlaywrightScraper", FakeScraper)
    FakeCollector.sources, FakeCollector.classify, FakeCollector.received = {}, "ok", []
    db_path = tmp_path / "instance" / "t.db"
    initialize_database(str(db_path))
    cal = tmp_path / "cal.json"
    cal.write_text(__import__("json").dumps({**source_gate.calibration_identity(), "cutoff": 0.58}))
    clock = Clock()

    def make(judge=None):
        gate = DiscoveryPageGate(None, judge=judge or FakeJudge(), mode="enforce", calibration_path=cal,
                                 evaluation_path=tmp_path / "none.json", sleep=lambda s: None)
        pipeline = discovery.EventDiscoveryEnrichmentPipeline(str(db_path), page_gate=gate)
        pipeline.retry_queue.clock = clock
        return pipeline

    return SimpleNamespace(path=db_path, make=make, clock=clock)


def _raw(db_path, column="*"):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT {column} FROM RawEvents")]
    finally:
        conn.close()


def _count(db_path, sql):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The two-run regression
# ---------------------------------------------------------------------------

def test_record_that_timed_out_is_processed_next_run_without_rediscovery(env):
    # Run 1: OAIC finds the article; classification times out.
    FakeCollector.sources = {"oaic": FakeSource([_event()])}
    FakeCollector.classify = "timeout"
    run1 = env.make()
    asyncio.run(run1.discover_events(source_types=["OAIC"], date_range_days=1))
    run1.close()
    [row] = _raw(env.path)
    assert not row["is_processed"] and row["processing_error"] is None
    assert _count(env.path, "SELECT COUNT(*) FROM EnrichedEvents") == 0
    conn = sqlite3.connect(env.path)
    state = conn.execute("SELECT temporary_failures, attempts, last_error_kind FROM DiscoveryRecordState").fetchone()
    pending = conn.execute("SELECT SUM(pending) FROM SourceMonthCompletion").fetchone()[0]
    conn.close()
    assert state == (1, 0, "temporary") and pending == 1

    # Run 2, an hour later: OAIC skips the stored URL, so nothing is rediscovered.
    env.clock.now += timedelta(hours=1)
    FakeCollector.sources = {"oaic": FakeSource([])}
    FakeCollector.classify = "ok"
    run2 = env.make()
    asyncio.run(run2.discover_events(source_types=["OAIC"], date_range_days=1))
    run2.close()

    [row] = _raw(env.path)
    assert row["is_processed"] and row["processing_error"] is None
    assert _count(env.path, "SELECT COUNT(*) FROM EnrichedEvents") == 1
    assert _count(env.path, "SELECT COALESCE(SUM(pending), 0) FROM SourceMonthCompletion") == 0
    assert run2.stats["retry_completed"] == 1


def test_back_off_holds_a_record_until_it_expires(env):
    FakeCollector.sources = {"oaic": FakeSource([_event()])}
    FakeCollector.classify = "timeout"
    asyncio.run(env.make().discover_events(source_types=["OAIC"], date_range_days=1))
    FakeCollector.sources, FakeCollector.classify = {"oaic": FakeSource([])}, "ok"
    pipeline = env.make()
    result = asyncio.run(pipeline.drain_retry_queue())
    assert result == {"due": 0, "completed": 0, "waiting": 1}
    env.clock.now += rq.backoff(rq.KIND_TEMPORARY, 1)
    assert asyncio.run(pipeline.drain_retry_queue())["completed"] == 1


def test_permanent_failures_quarantine_temporary_ones_never_do(env):
    FakeCollector.sources = {"oaic": FakeSource([_event()])}
    FakeCollector.classify = "bad"
    asyncio.run(env.make().discover_events(source_types=["OAIC"], date_range_days=1))
    FakeCollector.sources = {"oaic": FakeSource([])}
    for _ in range(rq.MAX_PERMANENT_ATTEMPTS - 1):
        env.clock.now += timedelta(days=8)
        asyncio.run(env.make().drain_retry_queue())
    [row] = _raw(env.path)
    assert row["is_processed"] and row["processing_error"].startswith("Rejected at retry_quarantine")
    assert _count(env.path, "SELECT SUM(quarantined) FROM SourceMonthCompletion") == 1

    # Released records come back with their attempts reset.
    pipeline = env.make()
    assert pipeline.retry_queue.release_quarantine() == 1
    [row] = _raw(env.path)
    assert not row["is_processed"]


def test_temporary_failures_never_give_up(tmp_path):
    conn = sqlite3.connect(tmp_path / "q.db")
    conn.execute("CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_type TEXT, raw_title TEXT, "
                 "raw_description TEXT, raw_content TEXT, event_date TEXT, source_url TEXT, source_metadata TEXT, "
                 "discovered_at TEXT, is_processed INTEGER DEFAULT 0, processing_error TEXT, "
                 "processing_attempted_at TEXT)")
    conn.execute("INSERT INTO RawEvents (raw_event_id, source_type, event_date) VALUES ('r', 'OAIC', '2025-03-14')")
    queue = rq.RetryQueue(conn)
    for _ in range(25):
        assert queue.record_failure("r", "classification", "timed out", rq.KIND_TEMPORARY) == "retry"
    assert conn.execute("SELECT is_processed FROM RawEvents").fetchone()[0] == 0
    # A record that never had state (an older crashed run) is still due.
    conn.execute("INSERT INTO RawEvents (raw_event_id, source_type) VALUES ('legacy', 'Perplexity')")
    assert "legacy" in {r["raw_event_id"] for r in queue.due()}


@pytest.mark.parametrize("exc,kind", [
    (asyncio.TimeoutError(), rq.KIND_TEMPORARY),
    (BudgetExhausted("API budget exhausted for openai"), rq.KIND_TEMPORARY),
    (ConnectionResetError("reset"), rq.KIND_TEMPORARY),
    (type("RateLimitError", (Exception,), {})("429"), rq.KIND_TEMPORARY),
    (type("APIStatusError", (Exception,), {"status_code": 503})("unavailable"), rq.KIND_TEMPORARY),
    (type("BadRequestError", (Exception,), {"status_code": 400})("context length exceeded"), rq.KIND_PERMANENT),
    (ValueError("validation failed for EventEnhancement"), rq.KIND_PERMANENT),
])
def test_failure_kinds(exc, kind):
    assert rq.classify_failure(exc) == kind


# ---------------------------------------------------------------------------
# Source-month completeness
# ---------------------------------------------------------------------------

def test_failed_search_leaves_the_month_unprocessed(env):
    FakeCollector.sources = {"oaic": FakeSource([_event()], failures=["OAIC article could not be scraped: x"])}
    pipeline = env.make()
    asyncio.run(pipeline.discover_events(source_types=["OAIC"], date_range_days=1))
    assert _count(env.path, "SELECT COUNT(*) FROM MonthProcessed WHERE is_processed") == 0
    assert _count(env.path, "SELECT COUNT(*) FROM SourceMonthDiscovery WHERE status = 'failed'") >= 1
    assert pipeline.stats["source_months_failed"] >= 1
    # The records that were found are still processed.
    assert _count(env.path, "SELECT COUNT(*) FROM EnrichedEvents") == 1


def test_source_that_raises_is_a_failed_month_not_an_empty_one(env):
    FakeCollector.sources = {"oaic": FakeSource([], raises=RuntimeError("HTTP 503"))}
    asyncio.run(env.make().discover_events(source_types=["OAIC"], date_range_days=1))
    assert _count(env.path, "SELECT COUNT(*) FROM MonthProcessed WHERE is_processed") == 0
    assert _count(env.path, "SELECT COUNT(*) FROM SourceMonthDiscovery WHERE status = 'complete'") == 0


def test_clean_search_marks_the_month_processed(env):
    FakeCollector.sources = {"oaic": FakeSource([_event()])}
    asyncio.run(env.make().discover_events(source_types=["OAIC"], date_range_days=1))
    assert _count(env.path, "SELECT COUNT(*) FROM MonthProcessed WHERE is_processed") >= 1
    assert _count(env.path, "SELECT COUNT(*) FROM SourceMonthDiscovery WHERE status != 'complete'") == 0


def test_google_quota_is_a_noted_failure():
    from cyber_data_collector.datasources.google_search import GoogleSearchDataSource
    from cyber_data_collector.models.config import DataSourceConfig, DateRange

    source = GoogleSearchDataSource.__new__(GoogleSearchDataSource)
    source.config, source.logger = DataSourceConfig(enabled=True), __import__("logging").getLogger("t")
    source._check_quota_reset = lambda: None
    GoogleSearchDataSource._daily_quota_exceeded = True
    try:
        out = asyncio.run(source.collect_events(DateRange(start_date=datetime(2025, 3, 1),
                                                          end_date=datetime(2025, 3, 31))))
    finally:
        GoogleSearchDataSource._daily_quota_exceeded = False
    assert out == [] and source.collection_failures and "quota" in source.collection_failures[0]


# ---------------------------------------------------------------------------
# No silent fallbacks
# ---------------------------------------------------------------------------

def test_page_gate_outage_stops_the_run_and_keeps_the_record_queued(env):
    FakeCollector.sources = {"oaic": FakeSource([_event()])}
    pipeline = env.make(judge=FakeJudge(fail=ConnectionError("TypeSafe unreachable")))
    with pytest.raises(PageGateUnavailable):
        asyncio.run(pipeline.discover_events(source_types=["OAIC"], date_range_days=1))
    [row] = _raw(env.path)
    assert not row["is_processed"]
    assert _count(env.path, "SELECT temporary_failures FROM DiscoveryRecordState") == 1
    assert _count(env.path, "SELECT COUNT(*) FROM MonthProcessed WHERE is_processed") == 0
    assert FakeCollector.received == []  # nothing was classified without the gate


def test_budget_refusal_at_the_gate_holds_records_without_failing(env):
    FakeCollector.sources = {"oaic": FakeSource([_event()])}
    pipeline = env.make(judge=FakeJudge(fail=BudgetExhausted("API budget exhausted for typesafe")))
    asyncio.run(pipeline.discover_events(source_types=["OAIC"], date_range_days=1))
    [row] = _raw(env.path)
    assert not row["is_processed"] and FakeCollector.received == []


def test_missing_openai_key_stops_discovery(env, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    pipeline = env.make()
    pipeline.env_config = {}
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        asyncio.run(pipeline.discover_events(source_types=["OAIC"], date_range_days=1))


def test_unclassified_record_is_never_stored_as_an_incident(env):
    FakeCollector.sources = {"oaic": FakeSource([_event()])}
    FakeCollector.classify = "unclassified"
    asyncio.run(env.make().discover_events(source_types=["OAIC"], date_range_days=1))
    assert _count(env.path, "SELECT COUNT(*) FROM EnrichedEvents") == 0
    [row] = _raw(env.path)
    assert not row["is_processed"]


def test_classifier_without_credentials_raises():
    from cyber_data_collector.processing.llm_classifier import LLMClassifier

    with pytest.raises(RuntimeError, match="not configured"):
        asyncio.run(LLMClassifier(None).classify_events([_event()]))


def test_entity_extraction_failure_fails_the_record():
    from cyber_data_collector.processing.entity_extractor import EntityExtractor

    class Broken:
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kw: (_ for _ in ()).throw(TimeoutError("timed out")))))

    [out] = asyncio.run(EntityExtractor(Broken()).extract_entities([_event()]))
    assert out.classification_status == "failed" and out.classification_error_kind == rq.KIND_TEMPORARY


def test_no_entities_extracted_leaves_identity_unresolved():
    from cyber_data_collector.models.events import AffectedEntity, EntityType
    from cyber_data_collector.processing.entity_extractor import EntityExtractor, ExtractedEntities

    guessed = AffectedEntity(name="Guessed Pty Ltd", entity_type=EntityType.OTHER, australian_entity=True,
                             confidence_score=0.9)
    event = _event().model_copy(update={"affected_entities": [guessed], "primary_entity": guessed})
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: ExtractedEntities(entities=[]))))
    [out] = asyncio.run(EntityExtractor(SimpleNamespace(client=client)).extract_entities([event]))
    assert out.affected_entities == [] and out.primary_entity is None


def test_failed_content_write_is_a_failed_scrape(tmp_path):
    db_path = tmp_path / "s.db"
    initialize_database(str(db_path))
    db = CyberEventDataV2(db_path)
    try:
        raw_id = db.add_raw_event("OAIC", {"title": "x", "source_url": URL, "metadata": {}})
        pipeline = discovery.EventDiscoveryEnrichmentPipeline.__new__(discovery.EventDiscoveryEnrichmentPipeline)
        pipeline.db = db
        # The write itself refuses a record that is not there ...
        with pytest.raises(RuntimeError):
            pipeline._update_raw_event_content("does-not-exist", ARTICLE, URL)

        # ... and a write that fails makes the scrape fail, not succeed.
        def locked(*args):
            raise sqlite3.OperationalError("database is locked")

        pipeline._update_raw_event_content = locked
        result = asyncio.run(pipeline._scrape_single_event(
            {"raw_event_id": raw_id, "source_url": URL, "raw_title": "x"}, FakeScraper()))
        status = db._conn.execute("SELECT status FROM ProcessingLog WHERE raw_event_id = ?", (raw_id,)).fetchone()[0]
    finally:
        db.close()
    assert result["success"] is False and status == "failed"
