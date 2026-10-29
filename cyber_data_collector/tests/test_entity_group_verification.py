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


def _db(verdicts):
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
        conn.execute("INSERT INTO EntityAliasDecisions VALUES (?, ?, ?, NULL, 'stored', '[]', 'sonar-pro', ?, 'x', NULL)",
                     (a, b, verdict, eav.PROMPT_VERSION))
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
