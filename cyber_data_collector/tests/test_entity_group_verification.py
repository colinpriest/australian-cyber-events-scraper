"""Group verification respects every recorded conflict, not just the head's.

Names were checked one by one against the group's chosen name, so A=B and A=C could both be confirmed while
B and C were recorded as different organisations - and the group still folded all three. Every stored
verdict between any two members is now honoured before a name joins, and nothing is asked about a name a
recorded conflict already bars.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from cyber_data_collector.dedup import entity_alias_verification as eav

A, B, C, D = "Alpha Holdings", "Alpha Hldgs", "Alpha H", "Alpha Group"


@pytest.fixture(autouse=True)
def _bogus_keys(monkeypatch):
    # A test that reaches for a model must fail, not make a paid call.
    monkeypatch.setenv("PERPLEXITY_API_KEY", "not-a-real-key")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")


def _db(verdicts, model="sonar-pro", evidence=None):
    """Entities A-D and the stored alias verdicts ``{(name, other): verdict}``."""
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT, entity_kind TEXT);
        CREATE TABLE EnrichedEventEntities (enriched_event_id TEXT, entity_id INTEGER);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT, title TEXT);
    """)
    conn.executemany("INSERT INTO EntitiesV2 (entity_name, entity_kind) VALUES (?, 'organisation')",
                     [(n,) for n in (A, B, C, D)])
    eav.ensure_schema(conn)
    for (a, b), verdict in verdicts.items():
        a, b = sorted((a, b))
        # Decided on the evidence as it is now, so each stored verdict stands.
        conn.execute("INSERT INTO EntityAliasDecisions VALUES (?, ?, ?, NULL, 'stored', '[]', ?, ?, 'x', ?)",
                     (a, b, verdict, model, eav.PROMPT_VERSION, evidence if evidence is not None else eav.pair_evidence(conn, a, b)))
    conn.commit()
    return conn


def _never_asked(prompt):
    raise AssertionError("a stored verdict decides this; Perplexity must not be asked: " + prompt[:80])


def test_a_recorded_conflict_between_two_other_members_keeps_them_out_of_one_fold():
    """A=B and A=C are confirmed but B and C are recorded as different organisations: never one group."""
    conn = _db({(A, B): "same", (A, C): "same", (B, C): "different"})
    report = eav.VerificationReport()
    groups = eav.verified_groups(conn, [(A, [B, C])], search=_never_asked, report=report)
    assert groups == [(A, [B])]                                   # one joins; the other is left out
    assert report.rejected == [(C, B, "different")]
    assert report.asked == 0 and report.failed == []


def test_a_parent_subsidiary_or_related_verdict_between_members_is_a_conflict_too():
    for verdict in ("parent_subsidiary", "related"):
        conn = _db({(A, B): "same", (A, C): "same", (B, C): verdict})
        groups = eav.verified_groups(conn, [(A, [B, C])], search=_never_asked)
        assert all(not {B, C} <= {keep, *drop} for keep, drop in groups), verdict


def test_unsure_between_members_is_not_a_conflict():
    conn = _db({(A, B): "same", (A, C): "same", (B, C): "unsure"})
    assert eav.verified_groups(conn, [(A, [B, C])], search=_never_asked) == [(A, [B, C])]


def test_nothing_is_asked_about_a_name_a_recorded_conflict_already_bars():
    """A=B is stored; B and C are different. A-C was never judged and is not asked: C cannot join B's fold."""
    conn = _db({(A, B): "same", (B, C): "different"})
    assert eav.verified_groups(conn, [(A, [B, C])], search=_never_asked) == [(A, [B])]


def test_the_members_with_fewer_conflicts_join_and_the_rest_are_regrouped():
    # B conflicts with both C and D; C and D conflict with nothing but B: they fold together, B stays out.
    conn = _db({(A, B): "same", (A, C): "same", (A, D): "same", (B, C): "different", (B, D): "different"})
    assert eav.verified_groups(conn, [(A, [B, C, D])], search=_never_asked) == [(A, [C, D])]


def test_without_a_recorded_conflict_the_group_is_asked_about_and_folds_as_before():
    conn = _db({})
    asked = []

    def search(prompt):
        asked.append(prompt)
        return {"text": json.dumps({"verdict": "same", "canonical_name": None, "reason": "r"}), "citations": []}

    assert eav.verified_groups(conn, [(A, [B, C])], search=search) == [(A, [B, C])]
    assert len(asked) == 2


def _answering(verdict, asked):
    def search(prompt):
        asked.append(prompt)
        return {"text": json.dumps({"verdict": verdict, "canonical_name": None, "reason": "r"}), "citations": []}
    return search


def test_a_conflict_verdict_made_on_other_evidence_is_asked_again_when_it_bars_a_proposed_fold():
    """B-C 'different' was decided on evidence that has since changed; it bars C from A's fold, so it is re-asked."""
    conn = _db({(A, B): "same", (A, C): "same"})
    conn.execute("INSERT INTO EntityAliasDecisions VALUES (?, ?, 'different', NULL, 'old', '[]', 'sonar-pro', ?, 'x', 'stale')",
                 (*sorted((B, C)), eav.PROMPT_VERSION))
    asked = []
    report = eav.VerificationReport()
    groups = eav.verified_groups(conn, [(A, [B, C])], search=_answering("same", asked), report=report)
    assert len(asked) == 1 and report.revisited == [tuple(sorted((B, C)))]
    assert groups == [(A, [B, C])]                                  # the outgrown veto no longer bars C
    old = conn.execute("SELECT verdict, evidence_hash, superseded_by FROM EntityAliasDecisionHistory").fetchall()
    assert old == [("different", "stale", "same")]                    # the replaced decision is kept


def test_a_veto_that_is_still_current_is_not_asked_again():
    conn = _db({(A, B): "same", (A, C): "same", (B, C): "different"})
    assert eav.verified_groups(conn, [(A, [B, C])], search=_never_asked) == [(A, [B])]


def test_a_failed_reask_leaves_the_old_veto_in_force_and_is_reported():
    conn = _db({(A, B): "same", (A, C): "same"})
    conn.execute("INSERT INTO EntityAliasDecisions VALUES (?, ?, 'different', NULL, 'old', '[]', 'sonar-pro', ?, 'x', 'stale')",
                 (*sorted((B, C)), eav.PROMPT_VERSION))
    report = eav.VerificationReport()
    groups = eav.verified_groups(conn, [(A, [B, C])], search=lambda prompt: None, report=report)
    assert groups == [(A, [B])] and tuple(sorted((B, C))) in report.failed        # C stays out: the fold is the risky direction


def test_a_stale_decision_no_proposal_touches_is_left_alone():
    conn = _db({(A, B): "same"})
    conn.execute("INSERT INTO EntityAliasDecisions VALUES (?, ?, 'different', NULL, 'old', '[]', 'sonar-pro', ?, 'x', 'stale')",
                 (*sorted((C, D)), eav.PROMPT_VERSION))
    assert eav.verified_groups(conn, [(A, [B])], search=_never_asked) == [(A, [B])]


def test_a_human_decision_is_never_asked_again_whatever_its_evidence():
    conn = _db({(A, B): "different"}, model="human", evidence="whatever was true then")
    report = eav.VerificationReport()
    assert eav.verified_groups(conn, [(A, [B])], search=_never_asked, report=report) == []
    assert report.revisited == [] and report.asked == 0


def test_a_decision_with_no_recorded_evidence_is_asked_again_the_first_time_it_matters():
    conn = _db({(A, B): "different"})
    conn.execute("UPDATE EntityAliasDecisions SET evidence_hash = NULL")
    asked = []
    groups = eav.verified_groups(conn, [(A, [B])], search=_answering("same", asked))
    assert len(asked) == 1 and groups == [(A, [B])]
    assert conn.execute("SELECT evidence_hash IS NOT NULL FROM EntityAliasDecisions").fetchone()[0] == 1
