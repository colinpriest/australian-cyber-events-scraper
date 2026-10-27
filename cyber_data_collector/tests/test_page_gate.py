"""Discovery page gate: the calibrated Jev source-gate question replaces the Random Forest filter.

Jev is faked; no API is called.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from cyber_data_collector.dedup import source_gate
from cyber_data_collector.filtering import page_gate as pg
from cyber_data_collector.utils.api_budget import BudgetExhausted

PAGE = "Acme Logistics confirmed attackers encrypted its systems and stole customer records. " * 6
HELP = "How to report a scam. Contact us. Privacy policy. Cyber incident hotline. " * 6


class FakeJudge:
    """P(page reports an incident): low for help pages; can fail a set number of times."""

    def __init__(self, fail_times=0, error=None):
        self.calls = []
        self.fail_times = fail_times
        self.error = error or ConnectionError("connection reset")

    def ask(self, subject_type, subject_id, question_version, state, questions):
        self.calls.append((subject_type, subject_id, question_version, state))
        if self.fail_times:
            self.fail_times -= 1
            raise self.error
        p = 0.04 if "report a scam" in state["page_text"] else 0.93
        return {qid: SimpleNamespace(value=p) for qid in questions}


def _cal(tmp_path, **overrides):
    path = tmp_path / "cal.json"
    path.write_text(json.dumps({**source_gate.calibration_identity(), "cutoff": 0.58, **overrides}))
    return path


def _gate(tmp_path, judge=None, mode="enforce", cal=None, evaluation=None):
    return pg.DiscoveryPageGate(sqlite3.connect(":memory:"), judge=judge or FakeJudge(), mode=mode,
                                calibration_path=cal or _cal(tmp_path),
                                evaluation_path=evaluation or tmp_path / "no-evaluation.json",
                                sleep=lambda s: None)


def _row(source="GoogleSearch", content=PAGE, rid="r1"):
    return {"raw_event_id": rid, "source_type": source, "raw_title": "Title", "raw_description": "snippet",
            "raw_content": content, "source_url": "https://example.com/page"}


def test_google_page_below_cutoff_is_dropped(tmp_path):
    gate = _gate(tmp_path)
    decision = gate.decide(_row(content=HELP))
    assert decision.dropped and decision.below_cutoff and decision.probability == pytest.approx(0.04)
    assert gate.decide(_row(content=PAGE, rid="r2")).dropped is False


def test_uncalibrated_source_is_report_only(tmp_path):
    gate = _gate(tmp_path)
    decision = gate.decide(_row(source="Perplexity", content=HELP))
    assert decision.below_cutoff and not decision.dropped
    assert "not calibrated for Perplexity" in decision.reason
    assert gate.statistics()["would_drop"] == 1 and gate.statistics()["dropped"] == 0


def test_short_page_is_not_judged_and_kept(tmp_path):
    judge = FakeJudge()
    gate = _gate(tmp_path, judge=judge)
    decision = gate.decide(_row(content="Cookie wall"))
    assert not decision.judged and not decision.dropped and judge.calls == []


def test_explicit_report_only_never_drops(tmp_path):
    gate = _gate(tmp_path, mode="report-only")
    decision = gate.decide(_row(content=HELP))
    assert decision.below_cutoff and not decision.dropped


def test_calibration_for_another_model_means_report_only(tmp_path):
    gate = _gate(tmp_path, cal=_cal(tmp_path, model="jev-0.0.1"))
    assert gate.cutoff is None
    assert not gate.decide(_row(content=HELP)).dropped


def test_evaluation_can_extend_enforcement_only_for_the_same_gate(tmp_path):
    evaluation = tmp_path / "eval.json"
    evaluation.write_text(json.dumps({**source_gate.calibration_identity(), "cutoff": 0.58,
                                      "sources": {"Perplexity": {"accepted": True}, "OAIC": {"accepted": False}}}))
    gate = _gate(tmp_path, evaluation=evaluation)
    assert gate.enforced_sources == {"GoogleSearch", "Perplexity"}
    evaluation.write_text(json.dumps({**source_gate.calibration_identity(), "cutoff": 0.5,
                                      "sources": {"Perplexity": {"accepted": True}}}))
    assert _gate(tmp_path, evaluation=evaluation).enforced_sources == {"GoogleSearch"}


def test_state_is_the_calibrated_source_gate_state(tmp_path):
    judge = FakeJudge()
    _gate(tmp_path, judge=judge).decide(_row(content=PAGE))
    _, _, version, state = judge.calls[0]
    assert version == source_gate.QUESTION_VERSION
    record = {"title": "Title", "url": "https://example.com/page", "search_snippet": "snippet",
              "page_text": PAGE[:source_gate.PAGE_CHARS]}
    assert state == source_gate.page_state(record)


def test_transient_jev_error_is_retried(tmp_path):
    judge = FakeJudge(fail_times=2)
    assert _gate(tmp_path, judge=judge).decide(_row()).judged and len(judge.calls) == 3


def test_persistent_jev_error_raises_instead_of_keeping(tmp_path):
    judge = FakeJudge(fail_times=10)
    with pytest.raises(pg.PageGateUnavailable):
        _gate(tmp_path, judge=judge).decide(_row())
    assert len(judge.calls) == pg.MAX_JEV_ATTEMPTS


def test_budget_refusal_is_not_retried(tmp_path):
    judge = FakeJudge(fail_times=10, error=BudgetExhausted("API budget exhausted for typesafe"))
    with pytest.raises(pg.PageGateUnavailable):
        _gate(tmp_path, judge=judge).decide(_row())
    assert len(judge.calls) == 1


def test_missing_typesafe_key_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    gate = pg.DiscoveryPageGate(sqlite3.connect(":memory:"), calibration_path=_cal(tmp_path))
    with pytest.raises(pg.PageGateUnavailable, match="TYPESAFE_API_KEY"):
        gate.require_available()
    with pytest.raises(pg.PageGateUnavailable):
        gate.decide(_row())


def test_unknown_mode_is_refused(tmp_path):
    with pytest.raises(ValueError):
        _gate(tmp_path, mode="off")
