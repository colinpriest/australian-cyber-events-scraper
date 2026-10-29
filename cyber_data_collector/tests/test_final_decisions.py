"""The final score of a judging step is tied to the evidence both models were shown.

Event reality, event scope, member coherence and victim attribution (and the duplicate checks, see
``test_pair_decisions``) read their final score back as the newest Jev answer and the newest escalation, each
chosen alone, so an escalation given before the evidence changed could be combined with a fresh Jev answer. They
now store the decision in ``FinalDecisions`` against the hash of the state both were shown and read only a decision
on the subject's current evidence.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

pytest.importorskip("typesafe_sdk")

from cyber_data_collector.dedup import event_reality as er  # noqa: E402
from cyber_data_collector.dedup import event_scope as es  # noqa: E402
from cyber_data_collector.dedup import final_decisions as fd  # noqa: E402
from cyber_data_collector.utils.escalation import EscalatedJudgement  # noqa: E402
from cyber_data_collector.utils.jev import JevJudge  # noqa: E402


@pytest.fixture(autouse=True)
def bogus_keys(monkeypatch):
    # A test that reaches for a real model must fail, not make a paid call.
    for name in ("OPENAI_API_KEY", "TYPESAFE_API_KEY", "PERPLEXITY_API_KEY"):
        monkeypatch.setenv(name, "bogus-key-for-tests")
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")


class FakeJev:
    def __init__(self, p):
        self.p = p

    def system_one(self, state, questions, model=None):
        return SimpleNamespace(model="jev-1.14.0", usage=None,
                               answers={q: SimpleNamespace(type="noul", noul=self.p) for q in questions})


class FakeReasoner:
    """``p`` for every case, or (``p`` None) a failure."""

    def __init__(self, p=None):
        self.p = p
        self.chat = SimpleNamespace(completions=SimpleNamespace(create_with_completion=self._create))

    def _create(self, **kwargs):
        if self.p is None:
            raise RuntimeError("the reasoning model is unavailable")
        return EscalatedJudgement(rationale="r", probability=self.p), SimpleNamespace(usage=None, model="gpt-5.5")


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, title TEXT, event_date TEXT,
            victim_organization_name TEXT, status TEXT, updated_at TEXT);
        CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title TEXT,
            perplexity_enrichment_data TEXT, status TEXT, updated_at TEXT);
        CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_url TEXT, raw_content TEXT, raw_description TEXT);
    """)
    for did in ("a", "b"):
        c.execute("INSERT INTO DeduplicatedEvents VALUES (?, ?, '2024-01-26', NULL, 'Active', NULL)", (did, f"Event {did}"))
        c.execute("INSERT INTO EnrichedEvents VALUES (?, ?, ?, ?, 'Active', NULL)",
                  (f"{did}-r", f"{did}-r", f"Event {did}", json.dumps({"formal_entity_name": "Acme"})))
        c.execute("INSERT INTO RawEvents VALUES (?, 'https://example.com', 'Original page text', NULL)", (f"{did}-r",))
        c.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?)", (f"{did}-r", did))
    return c


def _edit_page(conn, did):
    """Changes the evidence an event is judged on."""
    conn.execute("UPDATE EnrichedEvents SET title = 'Event with a corrected title' WHERE enriched_event_id = ?",
                 (f"{did}-r",))


@pytest.mark.parametrize("module, judge_kwargs", [(er, {"version": er.QUESTION_VERSION}), (es, {})])
def test_a_stale_escalation_is_never_combined_with_a_fresh_jev_answer(conn, module, judge_kwargs):
    ids = ["a", "b"]
    jev = module.judge_events(conn, ids, judge=JevJudge(conn, client=FakeJev(0.5)), **judge_kwargs)   # mid-band
    escalated = module.escalate_band(conn, jev, client=FakeReasoner(0.04))
    assert module.record_final(conn, jev, escalated) == {"a": 0.04, "b": 0.04}
    assert module.stored_final_probabilities(conn, ids) == {"a": 0.04, "b": 0.04}
    _edit_page(conn, "a")
    # "a" is judged again: a fresh mid-band Jev answer, and the reasoning model is down.
    jev = module.judge_events(conn, ["a"], judge=JevJudge(conn, client=FakeJev(0.5)), **judge_kwargs)
    failed = []
    escalated = module.escalate_band(conn, jev, client=FakeReasoner(None), failed=failed)
    assert failed == ["a"] and module.record_final(conn, jev, escalated, failed) == {}
    # The old 0.04 is not "a"'s score on the evidence it has now; "b", unchanged, keeps its decision.
    assert module.stored_final_probabilities(conn, ids) == {"b": 0.04}
    assert fd.latest(conn, module.STEP, ["a"])["a"][0] == 0.04                      # still on record, as stale


def test_a_decision_records_the_evidence_models_versions_and_the_answers_it_rests_on(conn):
    jev = er.judge_events(conn, ["a"], judge=JevJudge(conn, client=FakeJev(0.5)))
    escalated = er.escalate_band(conn, jev, client=FakeReasoner(0.12))
    er.record_final(conn, jev, escalated)
    row = conn.execute("SELECT step, subject_id, evidence_hash, question_version, jev_model, escalation_model, "
                       "final_score, basis, jev_input_hash, escalation_input_hash FROM FinalDecisions").fetchone()
    assert row[:2] == ("event-reality", "a") and row[2] == fd.state_hash(er.final_state(conn, "a"))
    assert row[3] == f"{er.QUESTION_VERSION}+{er.ESCALATION_VERSION}" and row[4] == "jev-1.14.0"
    assert row[5].startswith("gpt") and row[6] == pytest.approx(0.12) and row[7] == "escalated"
    for question_id, digest in ((er.QUESTION_ID, row[8]), (er.ESCALATION_QUESTION_ID, row[9])):
        assert conn.execute("SELECT COUNT(*) FROM ModelJudgements WHERE question_id = ? AND input_hash = ?",
                            (question_id, digest)).fetchone()[0] == 1


def test_decisions_are_per_step_and_a_missing_table_reads_as_no_decision(conn):
    jev = er.judge_events(conn, ["a"], judge=JevJudge(conn, client=FakeJev(0.9)))
    er.record_final(conn, jev, {})
    assert er.stored_final_probabilities(conn, ["a"]) == {"a": 0.9}
    assert es.stored_final_probabilities(conn, ["a"]) == {}                       # scope has not judged it
    empty = sqlite3.connect(":memory:")
    assert fd.latest(empty, er.STEP) == {} and fd.current(empty, er.STEP, "a", {}) is None


def test_a_check_events_run_leaves_a_decision_on_the_evidence_it_judged(conn, tmp_path):
    result = er.check_events(conn, judge=JevJudge(conn, client=FakeJev(0.9)), escalation_client=FakeReasoner(None),
                             search=lambda q, m: {"answer": "FOUND: no\nINCIDENT: none found", "citations": []},
                             timeline_search=lambda q, m: {"answer": "NONE FOUND", "citations": []},
                             calibration_path=tmp_path / "none.json")
    assert result["judged"] == 2
    assert er.stored_final_probabilities(conn, ["a", "b"]) == {"a": pytest.approx(0.9), "b": pytest.approx(0.9)}
