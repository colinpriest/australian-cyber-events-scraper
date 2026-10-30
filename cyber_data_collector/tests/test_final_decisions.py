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


# ----------------------------------------------------------------------
# The evidence is frozen: built once, and checked again before acting
# ----------------------------------------------------------------------

class EditingJev(FakeJev):
    """Jev answering ``p``, which - while it is asked about event ``a`` on the final question - changes ``a``'s
    evidence in the database, as another process (a re-enrichment, a timeline refresh) could."""

    def __init__(self, conn, p, final_question=lambda questions: True):
        super().__init__(p)
        self.conn, self.final_question, self.asked = conn, final_question, {}

    def system_one(self, state, questions, model=None):
        if self.final_question(questions):
            did = state["event"]["title"].rsplit(" ", 1)[-1]
            self.asked[did] = json.loads(json.dumps(state))
            if did == "a":
                _edit_page(self.conn, "a")
        return super().system_one(state, questions, model)


def _final_question(questions):
    """The event-reality final question (the screen has no corroboration instructions)."""
    return "corroboration" in next(iter(questions.values())).instructions


def _reality_files(tmp_path, cutoff):
    cal = tmp_path / "reality_cal.json"
    cal.write_text(json.dumps({"cutoff": cutoff, **er.calibration_identity()}))
    labels = tmp_path / "reality_labels.csv"
    labels.write_text("deduplicated_event_id,label\nunrelated,real\n", encoding="utf-8")
    return cal, labels


def _scope_files(tmp_path, cutoff):
    cal = tmp_path / "scope_cal.json"
    cal.write_text(json.dumps({"cutoff": cutoff, **es.calibration_identity()}))
    scope = tmp_path / "scope_labels.csv"
    scope.write_text("deduplicated_event_id,label,rationale,labeller\nunrelated,in_scope,r,t\n", encoding="utf-8")
    reality = tmp_path / "reality_labels.csv"
    reality.write_text("deduplicated_event_id,label\n", encoding="utf-8")
    return cal, scope, reality


@pytest.fixture
def ledger(monkeypatch):
    """Rejections without the ledger's snapshot tables (the ledger has its own tests)."""
    from cyber_data_collector.dedup.ledger import DedupLedger
    monkeypatch.setattr(DedupLedger, "snapshot_event", lambda self, batch, did: None)


def _status(conn, did):
    return conn.execute("SELECT status FROM DeduplicatedEvents WHERE deduplicated_event_id = ?", (did,)).fetchone()[0]


def _assert_one_evidence(conn, step, escalation_question_id, escalation_text, asked):
    """Jev, the escalation and the stored decision of ``a`` all rest on the state Jev was asked on."""
    from cyber_data_collector.utils import escalation as esc
    from cyber_data_collector.utils.llm_models import openai_model

    jev_state = asked["a"]
    evidence, escalation_digest = conn.execute(
        "SELECT evidence_hash, escalation_input_hash FROM FinalDecisions WHERE step = ? AND subject_id = 'a'",
        (step,)).fetchone()
    assert evidence == fd.state_hash(jev_state)
    assert escalation_digest == esc.input_hash(jev_state, escalation_text(), openai_model())
    # The escalation was asked once, on that state - not on the edited one.
    stored = [r[0] for r in conn.execute("SELECT input_hash FROM ModelJudgements WHERE question_id = ? "
                                         "AND subject_id = 'a'", (escalation_question_id,))]
    assert stored == [escalation_digest]


def test_check_events_does_not_reject_an_event_whose_evidence_changed_after_it_was_judged(conn, tmp_path, ledger):
    cal, labels = _reality_files(tmp_path, cutoff=0.3)
    jev = EditingJev(conn, 0.5, final_question=_final_question)          # mid-band: escalated
    result = er.check_events(conn, apply=True, judge=JevJudge(conn, client=jev), escalation_client=FakeReasoner(0.04),
                             search=lambda q, m: {"answer": "FOUND: no\nINCIDENT: none found", "citations": []},
                             timeline_search=lambda q, m: {"answer": "NONE FOUND", "citations": []},
                             calibration_path=cal, labels_path=labels)
    assert [f["deduplicated_event_id"] for f in result["rejections"]] == ["b"]
    assert [f["deduplicated_event_id"] for f in result["evidence_changed"]] == ["a"]
    assert _status(conn, "a") == "Active" and _status(conn, "b") == "Rejected"
    _assert_one_evidence(conn, "event-reality", er.ESCALATION_QUESTION_ID, er.escalation_question, jev.asked)
    # The decision is on record against the evidence it was made on, which is no longer "a"'s evidence.
    assert fd.state_hash(er.final_state(conn, "a")) != fd.state_hash(jev.asked["a"])
    assert er.stored_final_probabilities(conn, ["a"]) == {}


def test_check_scope_does_not_reject_an_event_whose_evidence_changed_after_it_was_judged(conn, tmp_path, ledger):
    cal, scope, reality = _scope_files(tmp_path, cutoff=0.3)
    jev = EditingJev(conn, 0.5)
    result = es.check_scope(conn, apply=True, judge=JevJudge(conn, client=jev), escalation_client=FakeReasoner(0.04),
                            calibration_path=cal, labels_path=scope, reality_labels_path=reality)
    assert [f["deduplicated_event_id"] for f in result["rejections"]] == ["b"]
    assert [f["deduplicated_event_id"] for f in result["evidence_changed"]] == ["a"]
    assert _status(conn, "a") == "Active" and _status(conn, "b") == "Rejected"
    _assert_one_evidence(conn, "event-scope", es.ESCALATION_QUESTION_ID, es.escalation_question, jev.asked)


def test_unchanged_evidence_is_rejected_and_nothing_is_reported_as_changed(conn, tmp_path, ledger):
    cal, scope, reality = _scope_files(tmp_path, cutoff=0.3)
    result = es.check_scope(conn, apply=True, judge=JevJudge(conn, client=FakeJev(0.05)),
                            escalation_client=FakeReasoner(None), calibration_path=cal, labels_path=scope,
                            reality_labels_path=reality)
    assert result["evidence_changed"] == [] and len(result["rejections"]) == 2


def test_a_frozen_state_is_never_rebuilt_behind_the_callers_back(conn):
    states = {}
    jev = er.judge_events(conn, ["a"], judge=JevJudge(conn, client=FakeJev(0.5)), states=states)
    _edit_page(conn, "a")
    escalated = er.escalate_band(conn, jev, client=FakeReasoner(0.2), states=states)
    er.record_final(conn, jev, escalated, states=states)
    assert fd.latest(conn, er.STEP, ["a"])["a"][1] == fd.state_hash(states["a"])
    assert fd.changed_since(states, ["a"], lambda d: er.final_state(conn, d)) == ["a"]
    with pytest.raises(ValueError):            # a subject Jev was not asked about on these states
        er.escalate_band(conn, {"b": 0.5}, client=FakeReasoner(0.2), states=states)
    with pytest.raises(ValueError):            # the screen is asked on its own, cheaper state
        er.judge_events(conn, ["a"], judge=JevJudge(conn, client=FakeJev(0.5)), version=er.SCREEN_VERSION,
                        states={})
