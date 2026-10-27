"""Event-level reality filter: Jev sees page text and Perplexity research, and
nothing is rejected without a calibration measured on the current model and question.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

pytest.importorskip("typesafe_sdk")

from cyber_data_collector.dedup import event_reality as er  # noqa: E402
from cyber_data_collector.dedup.ledger import DedupLedger  # noqa: E402
from cyber_data_collector.utils.jev import JevJudge  # noqa: E402


NO_ESCALATION = object()   # never called: the fake Jev scores sit outside the escalation band


def NO_FINDINGS(query, model):
    return {"answer": "FOUND: no\nINCIDENT: none found", "citations": []}


class FakeTypeSafe:
    """P(real) by event title; records what it was shown."""

    def __init__(self, by_title):
        self.by_title = by_title
        self.states = []

    def system_one(self, state, questions, model=None):
        self.states.append(state)
        p = self.by_title[state["event"]["title"]]
        return SimpleNamespace(model="jev-1.14.0", usage=None,
                               answers={qid: SimpleNamespace(type="noul", noul=p) for qid in questions})


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setattr(DedupLedger, "snapshot_event", lambda self, batch, did: None)
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, title TEXT, event_date TEXT,
            victim_organization_name TEXT, status TEXT, updated_at TEXT);
        CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER,
            relationship_type TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title TEXT,
            perplexity_enrichment_data TEXT, status TEXT, updated_at TEXT);
        CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_url TEXT, raw_content TEXT,
            raw_description TEXT);
    """)
    events = [
        ("act", "Business - ACT Government", "Helping Canberra businesses to start up.",
         {"formal_entity_name": "Australian Capital Territory Government", "overall_confidence": 0.83}),
        ("lush", "LUSH data breach", "Privacy Policy. We respect your privacy.",
         {"formal_entity_name": "Lush Retail", "overall_confidence": 0.2,
          "reasoning": "Lush confirmed unauthorised access to customer data in January 2024.",
          "sources_consulted": ["https://example.com/lush-breach"]}),
    ]
    for did, title, page, pplx in events:
        c.execute("INSERT INTO DeduplicatedEvents VALUES (?, ?, '2024-01-26', NULL, 'Active', NULL)", (did, title))
        c.execute("INSERT INTO EnrichedEvents VALUES (?, ?, ?, ?, 'Active', NULL)",
                  (f"{did}-r", f"{did}-r", title, json.dumps(pplx)))
        c.execute("INSERT INTO RawEvents VALUES (?, 'https://example.com', ?, NULL)", (f"{did}-r", page))
        c.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?)", (f"{did}-r", did))
    return c


def _calibration(tmp_path, cutoff, model=None, version=None):
    identity = er.calibration_identity()
    path = tmp_path / "cal.json"
    path.write_text(json.dumps({"cutoff": cutoff, "model": model or identity["model"],
                                "question_version": version or identity["question_version"]}))
    return path


def test_state_carries_page_text_and_perplexity_research(conn):
    state = er.build_state(conn, "lush")
    record = state["records"][0]
    assert record["page_text"].startswith("Privacy Policy")
    assert record["perplexity"]["formal_entity_name"] == "Lush Retail"
    assert "January 2024" in record["perplexity"]["reasoning"]
    assert record["perplexity"]["sources_consulted"] == ["https://example.com/lush-breach"]


def test_without_calibration_nothing_is_rejected(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    judge = JevJudge(conn, client=FakeTypeSafe({"Business - ACT Government": 0.02, "LUSH data breach": 0.9}))
    result = er.check_events(conn, timeline_search=NO_FINDINGS, escalation_client=NO_ESCALATION, apply=True, judge=judge, calibration_path=tmp_path / "missing.json",
                             search=NO_FINDINGS)
    assert result["judged"] == 2 and not result["calibrated"] and not result["applied"]
    assert conn.execute("SELECT COUNT(*) FROM DeduplicatedEvents WHERE status = 'Active'").fetchone()[0] == 2


def test_calibration_for_another_model_or_question_is_ignored(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    assert er.active_calibration(_calibration(tmp_path, 0.3, model="jev-1.13.0+gpt-5.5-2026-04-23")) is None
    assert er.active_calibration(_calibration(tmp_path, 0.3, version="event-reality-v0")) is None
    assert er.active_calibration(_calibration(tmp_path, 0.3))["cutoff"] == 0.3


def test_calibrated_cutoff_rejects_only_below_it(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    judge = JevJudge(conn, client=FakeTypeSafe({"Business - ACT Government": 0.02, "LUSH data breach": 0.9}))
    result = er.check_events(conn, timeline_search=NO_FINDINGS, escalation_client=NO_ESCALATION, apply=True, judge=judge, calibration_path=_calibration(tmp_path, 0.3),
                             search=NO_FINDINGS)

    assert result["applied"] and [r["deduplicated_event_id"] for r in result["rejections"]] == ["act"]
    status = dict(conn.execute("SELECT deduplicated_event_id, status FROM DeduplicatedEvents"))
    assert status == {"act": "Rejected", "lush": "Active"}
    assert conn.execute("SELECT status FROM EnrichedEvents WHERE enriched_event_id = 'act-r'").fetchone()[0] == "Inactive"


def test_only_low_screen_scores_are_searched_and_v2_sees_the_result(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    fake = FakeTypeSafe({"Business - ACT Government": 0.02, "LUSH data breach": 0.9})
    queries = []

    def search(query, model):
        queries.append(query)
        return {"answer": "FOUND: no\nINCIDENT: none found", "citations": ["https://example.com/x"]}

    er.check_events(conn, timeline_search=NO_FINDINGS, escalation_client=NO_ESCALATION, judge=JevJudge(conn, client=fake), calibration_path=tmp_path / "none.json",
                    search=search)

    assert len(queries) == 1 and "Business - ACT Government" in queries[0]
    v2_states = [s for s in fake.states if "corroboration" in s]
    assert len(v2_states) == 1 and v2_states[0]["corroboration"]["citations"] == ["https://example.com/x"]
    versions = [r[0] for r in conn.execute("SELECT DISTINCT question_version FROM ModelJudgements")]
    assert set(versions) == {er.SCREEN_VERSION, er.QUESTION_VERSION}


def test_a_search_is_stored_and_never_repeated(conn):
    from cyber_data_collector.dedup import corroboration as co
    calls = []

    def search(query, model):
        calls.append(query)
        return {"answer": "FOUND: yes\nINCIDENT: Lush confirmed a breach in January 2024.",
                "citations": ["https://example.com/lush"]}

    state = er.build_state(conn, "lush")
    first = co.corroborate(conn, "lush", state, search=search)
    again = co.corroborate(conn, "lush", state, search=search)
    assert len(calls) == 1 and again["answer"] == first["answer"]
    assert "January 2024" in er.build_state(conn, "lush", corroborated=True)["corroboration"]["answer"]
    row = conn.execute("SELECT model, query FROM EventCorroborations").fetchone()
    assert row[0] and "LUSH data breach" in row[1]


def test_a_failed_search_stores_nothing(conn):
    from cyber_data_collector.dedup import corroboration as co

    def broken(query, model):
        raise RuntimeError("timeout")

    assert co.corroborate(conn, "lush", er.build_state(conn, "lush"), search=broken) is None
    assert conn.execute("SELECT COUNT(*) FROM EventCorroborations").fetchone()[0] == 0


def test_an_event_labelled_real_is_never_rejected(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    labels = tmp_path / "labels.csv"
    labels.write_text("deduplicated_event_id,label,rationale,corroboration,labeller\n"
                      "act,real,confirmed elsewhere,packet,test\n", encoding="utf-8")
    judge = JevJudge(conn, client=FakeTypeSafe({"Business - ACT Government": 0.02, "LUSH data breach": 0.9}))
    result = er.check_events(conn, timeline_search=NO_FINDINGS, escalation_client=NO_ESCALATION, apply=True, judge=judge, calibration_path=_calibration(tmp_path, 0.3),
                             search=NO_FINDINGS, labels_path=labels)
    assert result["rejections"] == [] and not result["applied"]
    assert conn.execute("SELECT status FROM DeduplicatedEvents WHERE deduplicated_event_id = 'act'").fetchone()[0] == "Active"


def test_report_only_without_apply(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    judge = JevJudge(conn, client=FakeTypeSafe({"Business - ACT Government": 0.02, "LUSH data breach": 0.9}))
    result = er.check_events(conn, timeline_search=NO_FINDINGS, escalation_client=NO_ESCALATION, apply=False, judge=judge, calibration_path=_calibration(tmp_path, 0.3),
                             search=NO_FINDINGS)
    assert len(result["rejections"]) == 1 and not result["applied"]
    assert conn.execute("SELECT COUNT(*) FROM DeduplicatedEvents WHERE status = 'Active'").fetchone()[0] == 2


def test_a_labelled_non_incident_is_rejected_even_above_the_cutoff(conn, tmp_path, monkeypatch):
    """Labels are ground truth both ways (13 labelled non-incidents were still Active)."""
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    labels = tmp_path / "labels.csv"
    labels.write_text("deduplicated_event_id,label,rationale,corroboration,labeller\n"
                      "act,not_incident,a service page,packet,test\n"
                      "lush,not_incident,a privacy policy,packet,test\n", encoding="utf-8")
    judge = JevJudge(conn, client=FakeTypeSafe({"Business - ACT Government": 0.02, "LUSH data breach": 0.9}))
    result = er.check_events(conn, timeline_search=NO_FINDINGS, escalation_client=NO_ESCALATION, apply=True,
                             judge=judge, calibration_path=_calibration(tmp_path, 0.01),
                             search=NO_FINDINGS, labels_path=labels)
    assert [f["deduplicated_event_id"] for f in result["labelled_rejections"]] == ["act"]
    # LUSH now scores as real: possibly merged with a real incident since labelling - reviewed, not rejected.
    assert [f["deduplicated_event_id"] for f in result["labelled_needing_review"]] == ["lush"]
    status = dict(conn.execute("SELECT deduplicated_event_id, status FROM DeduplicatedEvents"))
    assert status == {"act": "Rejected", "lush": "Active"}


def test_missing_labels_refuse_to_reject(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    judge = JevJudge(conn, client=FakeTypeSafe({"Business - ACT Government": 0.02, "LUSH data breach": 0.9}))
    with pytest.raises(RuntimeError, match="refusing to reject"):
        er.check_events(conn, timeline_search=NO_FINDINGS, escalation_client=NO_ESCALATION, apply=True,
                        judge=judge, calibration_path=_calibration(tmp_path, 0.3),
                        search=NO_FINDINGS, labels_path=tmp_path / "missing.csv")
    assert conn.execute("SELECT COUNT(*) FROM DeduplicatedEvents WHERE status = 'Active'").fetchone()[0] == 2
