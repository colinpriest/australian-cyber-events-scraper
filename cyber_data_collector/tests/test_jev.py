"""Jev judgements: every answer stored with its probabilities, and reused on re-runs.

Jev is not fully deterministic, so replicability rests on the store: the same
question about the same input must return the stored answer, not a fresh one.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

pytest.importorskip("typesafe_sdk")
from typesafe_sdk import Choice, Noul  # noqa: E402

from cyber_data_collector.dedup import page_classifier as pc  # noqa: E402
from cyber_data_collector.utils import jev  # noqa: E402


class FakeTypeSafe:
    """Answers from a script; counts requests."""

    def __init__(self, choice_probs=None, noul=0.9):
        self.choice_probs = choice_probs or {"specific_incident": 0.1, "guidance_or_marketing": 0.7,
                                             "roundup_or_list": 0.2}
        self.noul = noul
        self.requests = []

    def system_one(self, state, questions, model=None):
        self.requests.append({"state": state, "questions": list(questions), "model": model})
        answers = {}
        for qid, q in questions.items():
            if q.type == "choice":
                best = max(self.choice_probs, key=self.choice_probs.get)
                answers[qid] = SimpleNamespace(type="choice", choice=best, confidence=0.6,
                                               probabilities=dict(self.choice_probs))
            else:
                answers[qid] = SimpleNamespace(type="noul", noul=self.noul)
        return SimpleNamespace(model="jev-1.14.0", answers=answers,
                               usage=SimpleNamespace(input_tokens=100, output_tokens=10))


@pytest.fixture
def conn():
    return sqlite3.connect(":memory:")


def test_answers_are_stored_with_their_distribution(conn):
    fake = FakeTypeSafe()
    judge = jev.JevJudge(conn, client=fake)
    out = judge.ask("enriched_event", "e1", "v1", {"title": "Business - ACT Government"},
                    {"kind": Choice(instructions="What is this page?", criteria={"a": None, "b": None}),
                     "is_incident": Noul(instructions="Does the page report an incident?")})

    assert out["kind"].answer == "guidance_or_marketing"
    assert out["kind"].value == pytest.approx(0.7)
    assert out["is_incident"].value == pytest.approx(0.9)
    rows = conn.execute("SELECT question_id, primitive, probabilities_json, model, model_requested, "
                        "question_version, subject_id FROM ModelJudgements ORDER BY question_id").fetchall()
    assert [r[0] for r in rows] == ["is_incident", "kind"]
    assert json.loads(rows[1][2])["roundup_or_list"] == pytest.approx(0.2)
    assert rows[1][3] == "jev-1.14.0" and rows[1][5] == "v1" and rows[1][6] == "e1"
    assert len(fake.requests) == 1, "independent questions over one state go in one request"


def test_same_question_same_input_is_not_asked_again(conn):
    fake = FakeTypeSafe()
    judge = jev.JevJudge(conn, client=fake)
    question = {"q": Noul(instructions="Does `t` report a cyber incident?")}
    first = judge.ask("enriched_event", "e1", "v1", {"t": "Toll Group hit by ransomware"}, question)
    fake.noul = 0.2  # Jev drifting between calls must not change a stored decision
    again = judge.ask("enriched_event", "e1", "v1", {"t": "Toll Group hit by ransomware"}, question)

    assert len(fake.requests) == 1
    assert again["q"].cached and again["q"].value == pytest.approx(first["q"].value)


def test_changed_input_or_refresh_asks_again(conn):
    fake = FakeTypeSafe()
    judge = jev.JevJudge(conn, client=fake)
    question = {"q": Noul(instructions="Does `t` report a cyber incident?")}
    judge.ask("enriched_event", "e1", "v1", {"t": "one"}, question)
    judge.ask("enriched_event", "e1", "v1", {"t": "two"}, question)
    judge.ask("enriched_event", "e1", "v1", {"t": "two"}, question, refresh=True)

    assert len(fake.requests) == 3
    assert conn.execute("SELECT COUNT(*) FROM ModelJudgements").fetchone()[0] == 3


def test_model_is_a_parameter(conn, monkeypatch):
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-preview")
    fake = FakeTypeSafe()
    jev.JevJudge(conn, client=fake).ask("x", "1", "v1", {"t": "a"}, {"q": Noul(instructions="?")})
    assert fake.requests[0]["model"] == "jev-preview"


def test_page_classifier_uses_jev_and_sums_the_non_incident_side(conn, monkeypatch):
    fake = FakeTypeSafe(choice_probs={"specific_incident": 0.08, "guidance_or_marketing": 0.5,
                                      "roundup_or_list": 0.42})
    monkeypatch.setattr(jev, "available", lambda: True)
    monkeypatch.setattr(jev.JevJudge, "_get_client", lambda self: fake)

    verdicts = pc.classify_pages(conn, [("r1", "Business - ACT Government", "https://www.act.gov.au/business",
                                         "Helping Canberra businesses to start up.",
                                         "A cyber-attack targeted the ACT Government.")])

    verdict = verdicts["r1"]
    assert verdict.kind == "guidance_or_marketing"
    assert verdict.confidence == pytest.approx(0.92)   # 0.5 + 0.42: confidently not an incident
    assert pc.is_non_incident_verdict((verdict.kind, verdict.confidence))
    state = fake.requests[0]["state"]
    assert state["page_text"].startswith("Helping Canberra")
    stored = conn.execute("SELECT kind, model, question_version FROM PageClassifications").fetchone()
    assert stored == ("guidance_or_marketing", "jev-1.14.0", pc.QUESTION_VERSION)
