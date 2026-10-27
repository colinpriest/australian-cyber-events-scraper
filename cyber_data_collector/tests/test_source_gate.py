"""Google source gate: only pages that themselves report an incident become evidence."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

pytest.importorskip("typesafe_sdk")

from cyber_data_collector.dedup import source_gate as sg  # noqa: E402
from cyber_data_collector.dedup.ledger import DedupLedger  # noqa: E402
from cyber_data_collector.utils.jev import JevJudge  # noqa: E402


class FakeJev:
    """P(page reports an incident) by URL."""

    def __init__(self, by_url):
        self.by_url = by_url

    def system_one(self, state, questions, model=None):
        p = self.by_url[state["url"]]
        return SimpleNamespace(model="jev-1.14.0", usage=None,
                               answers={q: SimpleNamespace(type="noul", noul=p) for q in questions})


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    monkeypatch.setattr(DedupLedger, "snapshot_event", lambda self, batch, did: None)

    def split_member(self, did, eid, reason, actor="human", record_override=True):
        new = f"{did}-split"
        self.conn.execute("INSERT INTO DeduplicatedEvents VALUES (?, 'split', NULL, NULL, 'Active', NULL)", (new,))
        self.conn.execute("UPDATE EventDeduplicationMap SET deduplicated_event_id = ? WHERE enriched_event_id = ?",
                          (new, eid))
        return new

    monkeypatch.setattr(DedupLedger, "split_member", split_member)
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, title TEXT, event_date TEXT,
            victim_organization_name TEXT, status TEXT, updated_at TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title TEXT,
            perplexity_enrichment_data TEXT, status TEXT, updated_at TEXT);
        CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_type TEXT, source_event_id TEXT,
            raw_title TEXT, raw_description TEXT, raw_content TEXT, event_date TEXT, source_url TEXT,
            source_metadata TEXT, discovered_at TEXT, is_processed INTEGER);
    """)

    def record(eid, source, url, page, event=None, research=None):
        # Pages need enough text to be judged (MIN_PAGE_CHARS); pad the fixtures.
        c.execute("INSERT INTO RawEvents VALUES (?, ?, NULL, ?, 'snippet', ?, NULL, ?, NULL, NULL, 1)",
                  (eid, source, eid, (page + ". ") * 40, url))
        c.execute("INSERT INTO EnrichedEvents VALUES (?, ?, ?, ?, 'Active', NULL)",
                  (eid, eid, eid, json.dumps(research or {})))
        if event:
            if not c.execute("SELECT 1 FROM DeduplicatedEvents WHERE deduplicated_event_id = ?", (event,)).fetchone():
                c.execute("INSERT INTO DeduplicatedEvents VALUES (?, ?, '2024-01-26', NULL, 'Active', NULL)",
                          (event, event))
            c.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?)", (eid, event))

    # A help page that is its event's only record (the Service NSW shape).
    record("help", "GoogleSearch", "https://gov/help", "How to report a scam", event="ev-help")
    # A privacy policy whose research names a real breach nobody else records (the LUSH shape).
    record("policy", "GoogleSearch", "https://lush/privacy", "Privacy policy",
           research={"formal_entity_name": "Lush Retail", "overall_confidence": 0.8,
                     "earliest_event_date": "2024-01-26", "reasoning": "Akira claimed Lush data."})
    # A real article and a junk page in the same event.
    record("article", "GoogleSearch", "https://news/acme", "Acme confirms ransomware attack", event="ev-acme")
    record("menu", "GoogleSearch", "https://acme/home", "Home | Products | Cyber incident", event="ev-acme")
    # Not from Google: never judged by this gate.
    record("pplx", "Perplexity", "https://news/other", "Other incident", event="ev-other")
    return c


JEV = {"https://gov/help": 0.02, "https://lush/privacy": 0.03, "https://news/acme": 0.97, "https://acme/home": 0.04}


def _cal(tmp_path, cutoff=0.3):
    path = tmp_path / "cal.json"
    path.write_text(json.dumps({"cutoff": cutoff, **sg.calibration_identity()}))
    return path


def _labels(tmp_path, rows=""):
    path = tmp_path / "labels.csv"
    path.write_text("enriched_event_id,label,rationale,labeller\n" + rows, encoding="utf-8")
    return path


def _found(calls):
    def search(query, model):
        calls.append(query)
        return {"answer": "FOUND: yes\nINCIDENT: Akira listed Lush on 26 January 2024.",
                "citations": ["https://news/lush-akira"]}
    return search


def test_only_google_records_are_judged(conn, tmp_path):
    result = sg.check_sources(conn, judge=JevJudge(conn, client=FakeJev(JEV)),
                              calibration_path=tmp_path / "none.json", labels_path=_labels(tmp_path))
    assert result["judged"] == 4 and not result["calibrated"] and result["removed"] == []


def test_gate_removes_junk_and_keeps_articles(conn, tmp_path):
    calls = []
    result = sg.check_sources(conn, apply=True, judge=JevJudge(conn, client=FakeJev(JEV)),
                              calibration_path=_cal(tmp_path), labels_path=_labels(tmp_path), search=_found(calls))
    actions = {r["enriched_event_id"]: r["action"] for r in result["removed"]}
    assert actions == {"help": "event rejected (its only record)",
                       "policy": "deactivated before deduplication",
                       "menu": "split out of its event and rejected"}
    status = dict(conn.execute("SELECT deduplicated_event_id, status FROM DeduplicatedEvents"))
    assert status["ev-help"] == "Rejected" and status["ev-acme"] == "Active" and status["ev-acme-split"] == "Rejected"
    # The policy pointed at an uncovered real incident: proper reporting is queued once.
    queued = [tuple(r) for r in conn.execute("SELECT source_type, source_url, is_processed FROM RawEvents "
                                             "WHERE source_metadata LIKE '%replacement%'")]
    assert queued == [("Perplexity", "https://news/lush-akira", 0)] and len(calls) == 1
    assert sg.replace_evidence(conn, "policy", None, search=_found(calls)) is None and len(calls) == 1


def test_labels_override_the_model(conn, tmp_path):
    labels = _labels(tmp_path, "help,reports_incident,r,t\narticle,not_incident,r,t\n")
    result = sg.check_sources(conn, judge=JevJudge(conn, client=FakeJev(JEV)), calibration_path=_cal(tmp_path),
                              labels_path=labels)
    flagged = {r["enriched_event_id"] for r in result["below_cutoff"]}
    assert "help" not in flagged and "article" in flagged


def test_no_replacement_when_the_incident_is_already_covered(conn):
    # The event carries the trading name; research gave the formal name ("Lush Retail").
    conn.execute("INSERT INTO DeduplicatedEvents VALUES ('ev-lush', 'Lush breach', '2024-02-01', 'Lush', "
                 "'Active', NULL)")
    calls = []
    assert sg.replace_evidence(conn, "policy", None, search=_found(calls)) is None
    assert calls == []


def test_a_page_too_short_to_judge_is_left_alone(conn, tmp_path):
    conn.execute("UPDATE RawEvents SET raw_content = 'Cookie wall' WHERE raw_event_id = 'help'")
    result = sg.check_sources(conn, judge=JevJudge(conn, client=FakeJev(JEV)), calibration_path=_cal(tmp_path),
                              labels_path=_labels(tmp_path))
    assert "help" not in {r["enriched_event_id"] for r in result["below_cutoff"]}
    assert result["unjudgeable"] == 1
