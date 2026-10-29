"""A missing confidence is NULL - unknown - never 0.7, 0.8, 0.5 or 0.0.

Regression for the reviewer finding: discovery stored 0.7 when an event's
confidence was absent and 0.8 when an entity had none, and validation turned a
missing score into 0.0. Stored, those read as measured model output.

Everything is local (the autouse fixture sets bogus keys so a call that reached
a paid service would fail instead of being made).
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from cyber_data_collector.models.events import (
    CLASSIFIER_ADJUSTED_VERSION,
    SOURCE_PRIOR_VERSION,
    ConfidenceScore,
    CyberEvent,
    CyberEventType,
    EventSeverity,
    EventSource,
)
from cyber_data_collector.pipelines import discovery
from cyber_data_collector.processing import confidence_defaults as cd
from cyber_data_collector.processing.deduplication import DeduplicationEngine
from cyber_data_collector.processing.llm_classifier import EventEnhancement, LLMClassifier
from cyber_data_collector.processing.record_evidence import BASIS_SNIPPET_ONLY
from cyber_data_collector.quality import metrics as qm
from cyber_data_collector.quality.model import load_thresholds
from cyber_data_collector.storage.cyber_event_data_v2 import CyberEventDataV2
from cyber_data_collector.storage.schema_init import initialize_database
from cyber_data_collector.tests.test_discovery_retry_queue import (  # noqa: F401 - env is a fixture
    FakeCollector,
    FakeSource,
    env,
)
from cyber_data_collector.utils.validation import validate_enrichment_data_for_storage

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def bogus_keys(monkeypatch):
    for name in ("OPENAI_API_KEY", "PERPLEXITY_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.setenv(name, "bogus-not-a-key")


def _score(overall, **kwargs):
    return ConfidenceScore(overall=overall, source_reliability=0.5, data_completeness=0.5, temporal_accuracy=0.5,
                           geographic_accuracy=0.5, **kwargs)


def _cyber_event(overall, title="Acme Logistics data breach") -> CyberEvent:
    return CyberEvent(
        title=title, description="d", event_type=CyberEventType.DATA_BREACH, severity=EventSeverity.MEDIUM,
        australian_relevance=True, event_date=datetime(2025, 3, 14), confidence=_score(overall),
        data_sources=[EventSource(source_id="s", source_type="OAIC", url="https://www.oaic.gov.au/news/acme-breach",
                                  credibility_score=0.8, relevance_score=0.8)])


# ---------------------------------------------------------------------------
# The model, validation and storage
# ---------------------------------------------------------------------------

def test_a_confidence_score_can_be_unknown_and_says_where_a_known_one_came_from():
    unknown = _score(None)
    assert unknown.overall is None
    assert _score(0.65).score_version == SOURCE_PRIOR_VERSION and _score(0.65).model is None


@pytest.mark.parametrize("given,stored", [(None, None), (0.0, 0.0), (0.65, 0.65), (1.4, 1.0), ("0.3", 0.3),
                                          ("not a number", None)])
def test_validation_keeps_unknown_unknown_and_zero_zero(given, stored):
    data = {"title": "t", "confidence_score": given, "confidence_model": "m", "confidence_version": "v"}
    out = validate_enrichment_data_for_storage(data, event_title="t")
    assert out["confidence_score"] == stored
    if stored is None:
        assert out["confidence_model"] is None and out["confidence_version"] is None


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "instance" / "t.db"
    initialize_database(str(path))
    return path


def _enrich(db_path, **data):
    db = CyberEventDataV2(db_path)
    raw = db.add_raw_event("Perplexity", {"title": "Acme breach", "source_url": "https://x.test/a"})
    eid = db.create_enriched_event(raw, {"title": "Acme breach", "is_australian_event": True,
                                         "is_specific_event": True, **data})
    db.close()
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT confidence_score, confidence_model, confidence_version FROM EnrichedEvents "
                       "WHERE enriched_event_id = ?", (eid,)).fetchone()
    conn.close()
    return eid, row


def test_a_missing_score_is_stored_as_null_with_no_provenance(db_path):
    _, row = _enrich(db_path, confidence_model="gpt", confidence_version="v9")
    assert row == (None, None, None)


def test_a_real_score_is_stored_with_its_model_and_version_and_a_real_zero_stays_zero(db_path):
    assert _enrich(db_path, confidence_score=0.62, confidence_model="gpt-x", confidence_version="v2")[1] == \
        (0.62, "gpt-x", "v2")
    assert _enrich(db_path, confidence_score=0.0)[1][0] == 0.0


def test_entities_without_a_score_are_stored_with_null_not_a_default(db_path):
    eid, _ = _enrich(db_path, entities=[{"name": "Acme Logistics Pty Ltd", "type": "Organization"},
                                        {"name": "Beta Corp", "type": "Organization", "confidence_score": 0.9}])
    conn = sqlite3.connect(db_path)
    links = dict(conn.execute("SELECT v.entity_name, ee.confidence_score FROM EnrichedEventEntities ee "
                              "JOIN EntitiesV2 v USING (entity_id) WHERE ee.enriched_event_id = ?", (eid,)))
    entities = dict(conn.execute("SELECT entity_name, confidence_score FROM EntitiesV2"))
    conn.close()
    assert links["Acme Logistics Pty Ltd"] is None and links["Beta Corp"] == 0.9
    assert entities["Acme Logistics Pty Ltd"] is None and entities["Beta Corp"] == 0.9


# ---------------------------------------------------------------------------
# Discovery stores what the event carried
# ---------------------------------------------------------------------------

def _run_discovery(env, event):
    FakeCollector.sources = {"oaic": FakeSource([event])}
    pipeline = env.make()
    asyncio.run(pipeline.discover_events(source_types=["OAIC"], date_range_days=1))
    pipeline.close()
    conn = sqlite3.connect(env.path)
    row = conn.execute("SELECT confidence_score, confidence_model, confidence_version FROM EnrichedEvents").fetchone()
    conn.close()
    return row


def test_discovery_stores_null_for_an_event_with_no_confidence(env):
    assert _run_discovery(env, _cyber_event(None)) == (None, None, None)


def test_discovery_stores_the_score_and_its_version_for_an_event_that_has_one(env):
    assert _run_discovery(env, _cyber_event(0.65)) == (0.65, None, SOURCE_PRIOR_VERSION)


def test_a_record_rebuilt_for_the_retry_queue_without_a_stored_score_stays_unscored():
    pipeline = discovery.EventDiscoveryEnrichmentPipeline.__new__(discovery.EventDiscoveryEnrichmentPipeline)
    row = {"raw_event_id": "r", "raw_title": "t", "raw_description": "d", "source_type": "OAIC",
           "source_url": "https://x.test/a", "event_date": None, "source_metadata": json.dumps({"confidence": None})}
    assert pipeline._event_from_raw_row(row).confidence.overall is None
    row["source_metadata"] = json.dumps({"confidence": 0.65})
    assert pipeline._event_from_raw_row(row).confidence.overall == 0.65


# ---------------------------------------------------------------------------
# Readers handle unknown explicitly
# ---------------------------------------------------------------------------

def _enhancement(adjust=None):
    return EventEnhancement(is_cybersecurity_event=True, is_australian_relevant=True,
                            event_type=CyberEventType.DATA_BREACH, severity=EventSeverity.HIGH,
                            confidence_adjustments={"overall": adjust} if adjust is not None else {})


def test_the_classifier_adjusts_a_known_score_and_records_the_model_but_leaves_unknown_unknown(monkeypatch):
    monkeypatch.setenv("OPENAI_FAST_MODEL", "fast-model-under-test")
    classifier = LLMClassifier(None)
    adjusted = classifier._apply_enhancement_to_event(_cyber_event(0.6), _enhancement(0.1))
    assert adjusted.confidence.overall == pytest.approx(0.7)
    assert adjusted.confidence.model == "fast-model-under-test"
    assert adjusted.confidence.score_version == CLASSIFIER_ADJUSTED_VERSION
    unknown = classifier._apply_enhancement_to_event(_cyber_event(None), _enhancement(0.1))
    assert unknown.confidence.overall is None and unknown.confidence.model is None
    capped = classifier._apply_enhancement_to_event(
        _cyber_event(None).model_copy(update={"evidence_basis": BASIS_SNIPPET_ONLY}), _enhancement())
    assert capped.confidence.overall is None            # nothing to cap


def test_the_in_memory_deduplicator_ranks_a_scored_record_above_an_unscored_one_and_invents_no_score():
    engine = DeduplicationEngine.__new__(DeduplicationEngine)
    engine.logger = SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None,
                                    info=lambda *a, **k: None)
    scored, unscored = _cyber_event(0.4, "Acme breach"), _cyber_event(None, "Acme breach again")
    merged = engine._merge_event_group([unscored, scored])
    assert merged.confidence.overall is not None and merged.confidence.overall >= 0.4   # the scored record leads
    only_unknown = engine._merge_event_group([_cyber_event(None, "A"), _cyber_event(None, "B")])
    assert only_unknown.confidence.overall is None        # more sources raise a score; they do not invent one


def test_collector_statistics_average_only_what_was_scored_and_say_how_many_were_not():
    from cyber_data_collector.cyber_collector import CyberDataCollector

    collector = CyberDataCollector.__new__(CyberDataCollector)
    collector.collected_events = [_cyber_event(0.4), _cyber_event(0.8), _cyber_event(None)]
    stats = collector.get_collection_stats()
    assert stats["average_confidence"] == pytest.approx(0.6) and stats["events_without_confidence"] == 1
    collector.collected_events = [_cyber_event(None)]
    stats = collector.get_collection_stats()
    assert stats["average_confidence"] is None and stats["events_without_confidence"] == 1


def test_a_null_score_survives_deduplication_instead_of_becoming_a_default(db_path):
    from scripts.run_global_deduplication import DeduplicationMigration

    db = CyberEventDataV2(db_path)
    raw = db.add_raw_event("Perplexity", {"title": "Acme breach", "source_url": "https://x.test/a"})
    db.create_enriched_event(raw, {"title": "Acme breach", "is_australian_event": True, "is_specific_event": True,
                                   "event_date": "2025-03-14"})
    db.create_enriched_event(db.add_raw_event("Perplexity", {"title": "Beta breach", "source_url": "https://x.test/b"}),
                             {"title": "Beta breach", "is_australian_event": True, "is_specific_event": True,
                              "event_date": "2025-04-14", "confidence_score": 0.0})
    db.close()
    events = {e.title: e for e in DeduplicationMigration(str(db_path))._load_enriched_events()}
    assert events["Acme breach"].confidence is None
    assert events["Beta breach"].confidence == 0.0       # a real 0.0 used to become 0.5


# ---------------------------------------------------------------------------
# The repair: provenance decides, nothing else
# ---------------------------------------------------------------------------

def _seed(db_path):
    """Records written by each path, with the values they used to get."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    now = datetime.now().isoformat()

    def raw(rid, source, metadata, logged=()):
        conn.execute("INSERT INTO RawEvents (raw_event_id, source_type, raw_title, source_metadata, discovered_at, "
                     "is_processed) VALUES (?, ?, ?, ?, ?, 1)", (rid, source, rid, json.dumps(metadata), now))
        for stage in logged:
            conn.execute("INSERT INTO ProcessingLog (log_id, raw_event_id, processing_stage, status, created_at) "
                         "VALUES (?, ?, ?, 'success', ?)", (f"{rid}-{stage}", rid, stage, now))

    def enriched(eid, rid, score, status="Active"):
        conn.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, is_australian_event, "
                     "is_specific_event, confidence_score, status) VALUES (?, ?, ?, 1, 1, ?, ?)",
                     (eid, rid, eid, score, status))

    # the single-record path with no source score: 0.7 was its default
    raw("r-default", "Perplexity", {"event_type": "x"}, logged=("llm_analysis",))
    enriched("e-default", "r-default", 0.7)
    # the same path, but the source gave 0.65 (stored as such): a real score
    raw("r-real", "Perplexity", {"confidence": 0.65}, logged=("llm_analysis",))
    enriched("e-real", "r-real", 0.65)
    # 0.7 on the main path: a source prior plus an adjustment can equal it; nothing proves a default
    raw("r-main", "Perplexity", {"confidence": 0.65}, logged=("enrichment",))
    enriched("e-main", "r-main", 0.7)
    # 0.7 with no trace of how it was written
    raw("r-untraced", "PerplexityResearch", {})
    enriched("e-untraced", "r-untraced", 0.8)
    # a source whose own score is 0.7 with the same path: the metadata says so, so it is a score
    raw("r-source07", "Perplexity", {"confidence": 0.7}, logged=("llm_analysis",))
    enriched("e-source07", "r-source07", 0.7)
    conn.execute("INSERT INTO EntitiesV2 (entity_name, entity_type) VALUES ('Acme', 'Organization'), ('Beta', "
                 "'Organization'), ('Gamma', 'Organization')")
    ids = dict(conn.execute("SELECT entity_name, entity_id FROM EntitiesV2"))
    for eid, name, score in (("e-default", "Acme", 0.8), ("e-default", "Beta", 0.6),     # the path's constants
                             ("e-main", "Gamma", 0.8),                                    # not that path
                             ("e-real", "Gamma", 0.6)):                                   # that path, but a real... see below
        conn.execute("INSERT INTO EnrichedEventEntities (enriched_event_id, entity_id, relationship_type, "
                     "confidence_score) VALUES (?, ?, 'affected', ?)", (eid, ids[name], score))
    conn.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, master_enriched_event_id, title, "
                 "is_australian_event, is_specific_event, confidence_score, status) VALUES ('d-default', "
                 "'e-default', 't', 1, 1, 0.7, 'Active'), ('d-main', 'e-main', 't', 1, 1, 0.7, 'Active')")
    conn.commit()
    return conn


def test_dry_run_reports_proven_and_ambiguous_and_changes_nothing(db_path):
    conn = _seed(db_path)
    before = conn.execute("SELECT confidence_score FROM EnrichedEvents ORDER BY enriched_event_id").fetchall()
    report = cd.repair(conn, dry_run=True)
    assert conn.execute("SELECT confidence_score FROM EnrichedEvents ORDER BY enriched_event_id").fetchall() == before
    proven = {(f.table, f.key) for f in report.proven}
    assert ("EnrichedEvents", "e-default") in proven and ("DeduplicatedEvents", "d-default") in proven
    assert {k for t, k in proven if t == "EnrichedEventEntities"} == {
        f"e-default|{i}" for i in (1, 2)} | {"e-real|3"}, "every entity link on the llm_analysis path was a constant"
    # not proven: the main path's 0.7, the untraced 0.8, a source's own 0.7
    assert ("EnrichedEvents", "e-main") not in proven and ("EnrichedEvents", "e-untraced") not in proven
    assert ("EnrichedEvents", "e-source07") not in proven and ("DeduplicatedEvents", "d-main") not in proven
    assert report.ambiguous[("EnrichedEvents", 0.7)] == 2 and report.ambiguous[("EnrichedEvents", 0.8)] == 1
    assert report.ambiguous[("EnrichedEventEntities", 0.8)] == 1 and report.ambiguous[("DeduplicatedEvents", 0.7)] == 1
    assert conn.execute("SELECT COUNT(*) FROM ConfidenceRepairs").fetchone()[0] == 0


def test_repair_clears_only_proven_defaults_logs_each_and_is_idempotent(db_path):
    conn = _seed(db_path)
    first = cd.repair(conn)
    scores = dict(conn.execute("SELECT enriched_event_id, confidence_score FROM EnrichedEvents"))
    assert scores == {"e-default": None, "e-real": 0.65, "e-main": 0.7, "e-untraced": 0.8, "e-source07": 0.7}
    assert dict(conn.execute("SELECT deduplicated_event_id, confidence_score FROM DeduplicatedEvents")) == {
        "d-default": None, "d-main": 0.7}
    links = conn.execute("SELECT enriched_event_id, confidence_score FROM EnrichedEventEntities "
                         "ORDER BY enriched_event_id, entity_id").fetchall()
    assert links == [("e-default", None), ("e-default", None), ("e-main", 0.8), ("e-real", None)]
    logged = conn.execute("SELECT table_name, COUNT(*), SUM(old_value) FROM ConfidenceRepairs GROUP BY 1 "
                          "ORDER BY 1").fetchall()
    assert [(t, n) for t, n, _ in logged] == [("DeduplicatedEvents", 1), ("EnrichedEventEntities", 3),
                                             ("EnrichedEvents", 1)] and len(first.proven) == 5
    again = cd.repair(conn)
    assert again.proven == [] and conn.execute("SELECT COUNT(*) FROM ConfidenceRepairs").fetchone()[0] == 5
    conn.close()


def test_a_deduplicated_event_of_an_already_cleared_master_is_still_recognised(db_path):
    conn = _seed(db_path)
    cd.repair(conn)
    conn.execute("UPDATE DeduplicatedEvents SET confidence_score = 0.7 WHERE deduplicated_event_id = 'd-default'")
    conn.commit()
    assert [(f.table, f.key) for f in cd.find_defaults(conn).proven] == [("DeduplicatedEvents", "d-default")]


def _script(name):
    spec = importlib.util.spec_from_file_location(f"_{name}_under_test", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_repair_command_has_a_dry_run_and_reports_without_writing(db_path, capsys):
    _seed(db_path).close()
    script = _script("repair_confidence_defaults")
    assert script.main(["--db-path", str(db_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "Proven defaults (cleared by the repair): 5" in out and "Ambiguous" in out
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT confidence_score FROM EnrichedEvents WHERE enriched_event_id = 'e-default'"
                        ).fetchone()[0] == 0.7
    conn.close()
    assert script.main(["--db-path", str(db_path)]) == 0
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT confidence_score FROM EnrichedEvents WHERE enriched_event_id = 'e-default'"
                        ).fetchone()[0] is None
    conn.close()
    assert script.main(["--db-path", str(db_path / "missing.db")]) == 2


def test_quality_report_counts_proven_defaults_ambiguous_values_and_unscored_events(db_path):
    conn = _seed(db_path)
    thresholds = load_thresholds()
    before = qm.measure_confidence(conn, thresholds, datetime(2026, 10, 29).date())
    assert before["confidence.defaults_proven"].value == 5 and before["confidence.events_unscored"].value == 0
    assert before["confidence.defaults_ambiguous"].value > 0
    cd.repair(conn)
    after = qm.measure_confidence(conn, thresholds, datetime(2026, 10, 29).date())
    assert after["confidence.defaults_proven"].value == 0 and after["confidence.events_unscored"].value == 1
    for name in qm.CONFIDENCE_METRICS:
        assert thresholds.rule_for(name) is not None, name
    conn.close()
