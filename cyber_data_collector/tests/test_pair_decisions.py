"""A duplicate decision is tied to the evidence that produced it.

The final score used to be rebuilt by picking the newest Jev answer and the newest escalation for a pair
independently, so an escalation made on old evidence could be combined with a fresh Jev answer, and a stale low
score could veto a merge. The final decision is now stored with the hash of the evidence both models were shown
(``PairDecisions``), read only when that evidence is the pair's evidence now, and a stale low score is asked again
only where it could still block a proposed merge.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

pytest.importorskip("typesafe_sdk")

from cyber_data_collector.dedup import duplicate_judgement as dj  # noqa: E402
from cyber_data_collector.utils.escalation import EscalatedJudgement  # noqa: E402
from cyber_data_collector.utils.jev import JevJudge  # noqa: E402


@pytest.fixture(autouse=True)
def bogus_keys(monkeypatch):
    # A test that reaches for a real model must fail, not make a paid call.
    for name in ("OPENAI_API_KEY", "TYPESAFE_API_KEY", "PERPLEXITY_API_KEY"):
        monkeypatch.setenv(name, "bogus-key-for-tests")
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")


class FakeJev:
    """Answers every question with ``p``; counts the requests."""

    def __init__(self, p):
        self.p = p
        self.requests = 0

    def system_one(self, state, questions, model=None):
        self.requests += 1
        return SimpleNamespace(model="jev-1.14.0", usage=None,
                               answers={q: SimpleNamespace(type="noul", noul=self.p) for q in questions})


class FakeReasoner:
    """Stands in for the reasoning model: ``p`` for every case, or a failure."""

    def __init__(self, p=None):
        self.p = p
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create_with_completion=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        if self.p is None:
            raise RuntimeError("the reasoning model is unavailable")
        return EscalatedJudgement(rationale="r", probability=self.p), SimpleNamespace(usage=None, model="gpt-5.5")


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, title TEXT, event_date TEXT,
            victim_organization_name TEXT, status TEXT, records_affected INTEGER, attack_method TEXT,
            event_type TEXT, vendor_organization_name TEXT, summary TEXT, description TEXT,
            master_enriched_event_id TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title TEXT,
            event_date TEXT, summary TEXT, description TEXT, perplexity_enrichment_data TEXT);
        CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_url TEXT, raw_content TEXT,
            raw_description TEXT, event_date TEXT, source_metadata TEXT, discovered_at TEXT);
        INSERT INTO EntitiesV2 VALUES (1, 'ACT Government');
    """)
    for did, title, date in [("e1", "Business - ACT Government", "2023-05-24"),
                             ("e2", "Free preschool - ACT Government", "2023-06-08"),
                             ("e3", "Another notice - ACT Government", "2023-06-10")]:
        c.execute("INSERT INTO DeduplicatedEvents VALUES (?, ?, ?, 'ACT Government', 'Active', NULL, NULL, NULL, "
                  "NULL, NULL, NULL, ?)", (did, title, date, f"{did}-r"))
        c.execute("INSERT INTO DeduplicatedEventEntities VALUES (?, 1, 'victim')", (did,))
        c.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?)", (f"{did}-r", did))
        c.execute("INSERT INTO EnrichedEvents VALUES (?, ?, ?, ?, NULL, NULL, '{}')", (f"{did}-r", f"{did}-r", title, date))
        c.execute("INSERT INTO RawEvents VALUES (?, 'https://www.act.gov.au/x', 'Page text', NULL, ?, ?, '2026-05-03')",
                  (f"{did}-r", date, json.dumps({"publication_date": date})))
    return c


def _pair(conn, pid="e1|e2"):
    return next(p for p in dj.candidate_pairs(conn) if p["pair_id"] == pid)


def _redate(conn, dedup_id, day):
    """Changes the evidence a pair judgement is shown (the gap and the dates) without touching anything else."""
    conn.execute("UPDATE DeduplicatedEvents SET event_date = ? WHERE deduplicated_event_id = ?", (day, dedup_id))
    conn.execute("UPDATE EnrichedEvents SET event_date = ? WHERE enriched_event_id = ?", (day, f"{dedup_id}-r"))


def test_a_final_decision_is_stored_against_the_evidence_models_and_versions(conn):
    pair = _pair(conn)
    scored = dj.score_pairs(conn, [pair], dj.ORGANISATION, judge=JevJudge(conn, client=FakeJev(0.9)))
    assert scored.final == {"e1|e2": 0.9} and scored.failed == []
    row = conn.execute("SELECT * FROM PairDecisions").fetchone()
    assert row["route"] == "organisation" and row["pair_id"] == "e1|e2" and row["final_score"] == 0.9
    assert row["basis"] == "jev" and row["escalation_input_hash"] is None
    assert row["evidence_hash"] == dj.state_hash(dj.build_pair_state(conn, pair)) == scored.evidence["e1|e2"]
    assert row["question_version"] == f"{dj.QUESTION_VERSION}+{dj.ESCALATION_VERSION}"
    assert (row["jev_model"], row["escalation_model"].startswith("gpt")) == ("jev-1.14.0", True)
    # The decision names the stored Jev answer it rests on.
    assert conn.execute("SELECT COUNT(*) FROM ModelJudgements WHERE question_id = ? AND input_hash = ?",
                        (dj.QUESTION_ID, row["jev_input_hash"])).fetchone()[0] == 1


def test_an_escalated_decision_names_the_escalation_it_rests_on(conn):
    pair = _pair(conn)
    scored = dj.score_pairs(conn, [pair], dj.ORGANISATION, judge=JevJudge(conn, client=FakeJev(0.5)),
                            escalation_client=FakeReasoner(0.12))
    assert scored.final == {"e1|e2": 0.12} and scored.escalated == {"e1|e2": 0.12}
    row = conn.execute("SELECT basis, escalation_input_hash FROM PairDecisions").fetchone()
    assert row["basis"] == "escalated"
    assert conn.execute("SELECT value FROM ModelJudgements WHERE question_id = ? AND input_hash = ?",
                        (dj.ESCALATION_QUESTION_ID, row["escalation_input_hash"])).fetchone()[0] == 0.12


def test_a_second_run_on_unchanged_evidence_asks_nothing(conn):
    pair = _pair(conn)
    jev, reasoner = FakeJev(0.5), FakeReasoner(0.12)
    dj.score_pairs(conn, [pair], dj.ORGANISATION, judge=JevJudge(conn, client=jev), escalation_client=reasoner)
    again = dj.score_pairs(conn, [pair], dj.ORGANISATION, judge=JevJudge(conn, client=jev), escalation_client=reasoner)
    assert again.final == {"e1|e2": 0.12} and (jev.requests, reasoner.calls) == (1, 1)
    assert conn.execute("SELECT COUNT(*) FROM PairDecisions").fetchone()[0] == 1


def test_only_a_decision_on_the_current_evidence_is_read(conn):
    pair = _pair(conn)
    dj.score_pairs(conn, [pair], dj.ORGANISATION, judge=JevJudge(conn, client=FakeJev(0.9)))
    assert dj.stored_final_probabilities(conn, [pair]) == {"e1|e2": 0.9}
    _redate(conn, "e2", "2023-07-01")                                      # the gap, and so the evidence, changed
    assert dj.stored_final_probabilities(conn, [_pair(conn)]) == {}
    score, evidence = dj.stale_decisions(conn, dj.ORGANISATION)["e1|e2"]  # still on record, as stale
    assert score == 0.9 and evidence != dj.state_hash(dj.build_pair_state(conn, _pair(conn)))


def test_a_stale_escalation_is_not_combined_with_a_fresh_jev_answer(conn):
    """The old reader: newest Jev answer + newest escalation, each chosen alone. After the evidence changed, a fresh
    mid-band Jev answer picked up the old escalation's 0.05 as if it had been given on the new evidence."""
    pair = _pair(conn)
    dj.score_pairs(conn, [pair], dj.ORGANISATION, judge=JevJudge(conn, client=FakeJev(0.5)),
                   escalation_client=FakeReasoner(0.05))
    assert dj.unvalidated_final_probabilities(conn) == {"e1|e2": 0.05}
    _redate(conn, "e2", "2023-07-01")
    new_pair = _pair(conn)
    # A fresh Jev answer in the band, and the reasoning model unavailable: there is no decision.
    scored = dj.score_pairs(conn, [new_pair], dj.ORGANISATION, judge=JevJudge(conn, client=FakeJev(0.5)),
                            escalation_client=FakeReasoner(None))
    assert scored.final == {} and scored.failed == ["e1|e2"]
    assert dj.stored_final_probabilities(conn, [new_pair]) == {}
    # The old reader would have called this pair 0.05; the pipeline no longer can.
    assert dj.unvalidated_final_probabilities(conn) == {"e1|e2": 0.05}
    # With the reasoning model back, the decision is the one made on the new evidence.
    scored = dj.score_pairs(conn, [new_pair], dj.ORGANISATION, judge=JevJudge(conn, client=FakeJev(0.5)),
                            escalation_client=FakeReasoner(0.7))
    assert scored.final == {"e1|e2": 0.7}


CAL = {"different_cutoff": 0.2}
LOOKUPS = lambda conn: {"organisation": dj.PairUniverse(conn).pair, "ai": lambda a, b: None}   # noqa: E731


def _low_decision(conn):
    pair = _pair(conn)
    dj.score_pairs(conn, [pair], dj.ORGANISATION, judge=JevJudge(conn, client=FakeJev(0.05)))
    return pair


def test_a_low_score_on_the_current_evidence_vetoes_without_asking(conn):
    _low_decision(conn)
    jev = FakeJev(0.99)
    conflicts, revisited = dj.calibrated_negatives(conn, CAL, None, {"e1", "e2"}, LOOKUPS(conn),
                                                   judge=JevJudge(conn, client=jev))
    assert list(conflicts) == ["e1|e2"] and revisited == [] and jev.requests == 0
    assert "below its calibrated 'different' bound" in conflicts["e1|e2"]


def test_a_stale_low_score_does_not_veto_and_is_asked_again_because_it_could_block_a_merge(conn):
    _low_decision(conn)
    _redate(conn, "e2", "2023-07-01")
    jev = FakeJev(0.95)                                                    # on the new evidence the pair is the same
    conflicts, revisited = dj.calibrated_negatives(conn, CAL, None, {"e1", "e2"}, LOOKUPS(conn),
                                                   judge=JevJudge(conn, client=jev))
    assert conflicts == {} and revisited == ["e1|e2"] and jev.requests == 1
    assert dj.stored_final_probabilities(conn, [_pair(conn)]) == {"e1|e2": 0.95}   # the new decision is on record


def test_a_stale_low_score_that_is_still_low_on_the_new_evidence_vetoes(conn):
    _low_decision(conn)
    _redate(conn, "e2", "2023-07-01")
    conflicts, revisited = dj.calibrated_negatives(conn, CAL, None, {"e1", "e2"}, LOOKUPS(conn),
                                                   judge=JevJudge(conn, client=FakeJev(0.03)))
    assert list(conflicts) == ["e1|e2"] and "re-judged" in conflicts["e1|e2"] and revisited == ["e1|e2"]


def test_a_stale_low_score_whose_escalation_fails_vetoes_nothing(conn):
    _low_decision(conn)
    _redate(conn, "e2", "2023-07-01")
    conflicts, revisited = dj.calibrated_negatives(conn, CAL, None, {"e1", "e2"}, LOOKUPS(conn),
                                                   judge=JevJudge(conn, client=FakeJev(0.5)),
                                                   escalation_client=FakeReasoner(None))
    assert conflicts == {} and revisited == ["e1|e2"]


def test_a_stale_decision_outside_the_proposed_events_is_left_alone(conn):
    _low_decision(conn)
    _redate(conn, "e2", "2023-07-01")
    jev = FakeJev(0.95)
    conflicts, revisited = dj.calibrated_negatives(conn, CAL, None, {"e1", "e3"}, LOOKUPS(conn),
                                                   judge=JevJudge(conn, client=jev))
    assert conflicts == {} and revisited == [] and jev.requests == 0


def test_a_stale_high_score_is_not_revisited(conn):
    pair = _pair(conn)
    dj.score_pairs(conn, [pair], dj.ORGANISATION, judge=JevJudge(conn, client=FakeJev(0.95)))
    _redate(conn, "e2", "2023-07-01")
    jev = FakeJev(0.01)
    conflicts, revisited = dj.calibrated_negatives(conn, CAL, None, {"e1", "e2"}, LOOKUPS(conn),
                                                   judge=JevJudge(conn, client=jev))
    assert conflicts == {} and revisited == [] and jev.requests == 0       # it could not have vetoed


def test_without_a_calibrated_bound_a_low_score_is_no_veto(conn):
    _low_decision(conn)
    assert dj.calibrated_negatives(conn, {}, None, {"e1", "e2"}, LOOKUPS(conn)) == ({}, [])


def test_the_universe_ranks_by_retrieval_to_the_same_pairs_as_by_enumeration_on_a_small_group(conn):
    from cyber_data_collector.dedup import candidate_ranking as ranking

    corpus = ranking.load_corpus(conn)
    universe = dj.PairUniverse(conn)
    enumerated, e_stats = universe.ranked(corpus)
    retrieved, r_stats = universe.ranked(corpus, exhaustive_max=0)           # force bounded retrieval
    assert [p["pair_id"] for p in retrieved] == [p["pair_id"] for p in enumerated] == ["e1|e2", "e1|e3", "e2|e3"]
    assert e_stats["bounded_groups"] == 0 and r_stats["bounded_groups"] == 1
    assert r_stats["before"] == e_stats["before"] == 3
    assert [p["pair_id"] for p in universe.all_pairs()] == ["e1|e2", "e1|e3", "e2|e3"]
    assert universe.pair("e3", "e1")["pair_id"] == "e1|e3" and universe.pair("e1", "nope") is None


def test_the_decisions_table_is_part_of_the_database_schema(tmp_path):
    from cyber_data_collector.storage.schema_init import _module_schema_steps

    c = sqlite3.connect(":memory:")
    c.executescript("CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT);")
    try:
        _module_schema_steps(c)
    except sqlite3.Error:
        pass            # other modules' tables are not under test; the decisions table is created before they fail
    assert c.execute("SELECT name FROM sqlite_master WHERE name = 'PairDecisions'").fetchone() is not None
