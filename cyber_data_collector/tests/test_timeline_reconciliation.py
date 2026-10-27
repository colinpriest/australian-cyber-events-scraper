"""Timeline reconciliation, on the University of Western Australia case that motivated it."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest

pytest.importorskip("typesafe_sdk")

from cyber_data_collector.dedup import org_timeline as ot  # noqa: E402
from cyber_data_collector.dedup import timeline_reconciliation as tr  # noqa: E402
from cyber_data_collector.utils.jev import JevJudge  # noqa: E402

TIMELINE = ("1. **11 August 2025** | UWA detected unauthorised access to password information. | statement | "
            "https://abc/uwa\n"
            "2. **11 June 2026** | Unauthorised access to the Callista student system. | Cyber Daily | "
            "https://cyberdaily/uwa\n"
            "3. **July 2022** | Callista student records exposed. | notice | https://uwa/2022")


class FakeJev:
    """Answers by event title: which incident it matches, and what an unmatched one is."""

    MATCHES = {"UWA password breach": "incident_1", "Second UWA breach": "incident_2",
               "UN Women Australia thwarts attack": "none"}

    def system_one(self, state, questions, model=None):
        (qid, q), = questions.items()
        title = state["event"]["event"]["title"]
        if qid == tr.MATCH_QUESTION_ID:
            choice = self.MATCHES[title]
        else:
            choice = "wrong_organisation"
        probs = {k: (0.9 if k == choice else 0.1 / (len(q.criteria) - 1)) for k in q.criteria}
        return SimpleNamespace(model="jev-1.14.0", usage=None, answers={
            qid: SimpleNamespace(type="choice", choice=choice, confidence=0.8, probabilities=probs)})


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, title TEXT, event_date TEXT,
            victim_organization_name TEXT, status TEXT, updated_at TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title TEXT,
            event_date TEXT, perplexity_enrichment_data TEXT, status TEXT, updated_at TEXT);
        CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_type TEXT, source_event_id TEXT,
            raw_title TEXT, raw_description TEXT, raw_content TEXT, event_date TEXT, source_url TEXT,
            source_metadata TEXT, discovered_at TEXT, is_processed INTEGER);
        INSERT INTO EntitiesV2 VALUES (1, 'University of Western Australia');
    """)
    for did, title, when in [("e1", "UWA password breach", "2025-08-09"),
                             ("e2", "Second UWA breach", "2026-02-01"),
                             ("e3", "UN Women Australia thwarts attack", "2024-06-01")]:
        c.execute("INSERT INTO DeduplicatedEvents VALUES (?, ?, ?, 'UWA', 'Active', NULL)", (did, title, when))
        c.execute("INSERT INTO DeduplicatedEventEntities VALUES (?, 1, 'victim')", (did,))
        c.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?)", (f"{did}-r", did))
        c.execute("INSERT INTO EnrichedEvents VALUES (?, ?, ?, ?, '{}', 'Active', NULL)", (f"{did}-r", f"{did}-r", title, when))
        c.execute("INSERT INTO RawEvents VALUES (?, 'GoogleSearch', NULL, ?, '', 'Article text', ?, 'https://x', "
                  "NULL, '2026-06-20T00:00:00', 1)", (f"{did}-r", title, when))
    ot.research(c, 1, "University of Western Australia",
                search=lambda q, m: {"answer": TIMELINE, "citations": ["https://abc/uwa"]})
    return c


@pytest.mark.parametrize("text, expected", [
    ("11 August 2025", (date(2025, 8, 11), "day")),
    ("January–February 2025", (date(2025, 1, 1), "month")),
    ("9 July 2023–16 March 2024", (date(2023, 7, 9), "day")),
    ("27 February–11 August 2020", (date(2020, 2, 27), "day")),
    ("28 January–25 February 2025; publicised later", (date(2025, 1, 28), "day")),
    ("2016-02", (date(2016, 2, 1), "month")),
    ("2018–2019 (reported 25 May 2021)", (date(2018, 1, 1), "year")),
    ("Late 2018 (reported 11 March 2020)", (date(2018, 1, 1), "year")),
    ("May 12, 2014", (date(2014, 5, 12), "day")),
    ("2019", (date(2019, 1, 1), "year")),
    ("unknown", (None, "none")),
])
def test_incident_dates(text, expected):
    assert tr.incident_date(text) == expected


def test_reconciliation_finds_each_kind_of_disagreement(conn):
    calls = []

    def search(query, model):
        calls.append(query)
        return {"answer": "FOUND: yes\nINCIDENT: UWA Callista breach, July 2022.", "citations": ["https://news/uwa-2022"]}

    out = tr.reconcile_organisation(conn, 1, "University of Western Australia", JevJudge(conn, client=FakeJev()),
                                    queue_gaps=True, search=search)
    by_title = {e["title"]: e for e in out["events"]}
    assert by_title["UWA password breach"]["match"] == "incident_1"
    assert by_title["UN Women Australia thwarts attack"]["unmatched_kind"] == "wrong_organisation"
    # Matched to the June 2026 incident but stored as February: a date conflict.
    assert [d["title"] for d in out["date_conflicts"]] == ["Second UWA breach"]
    # The July 2022 incident has no event: a coverage gap, searched once and queued.
    assert [g["incident"]["number"] for g in out["gaps"]] == ["3"]
    assert out["gaps"][0]["queued_raw_event_id"]
    assert tr.queue_gap(conn, 1, "University of Western Australia", out["gaps"][0]["incident"], search=search) is None
    assert len(calls) == 1


def test_without_queue_gaps_nothing_is_added(conn):
    before = conn.execute("SELECT COUNT(*) FROM RawEvents").fetchone()[0]
    tr.reconcile_organisation(conn, 1, "University of Western Australia", JevJudge(conn, client=FakeJev()))
    assert conn.execute("SELECT COUNT(*) FROM RawEvents").fetchone()[0] == before


def test_gap_search_without_a_key_fails_loudly(conn, monkeypatch):
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    incident = {"date": "July 2022", "what": "Callista exposure", "source": "", "number": "3"}
    with pytest.raises(RuntimeError, match="PERPLEXITY_API_KEY"):
        tr.queue_gap(conn, 1, "University of Western Australia", incident)


@pytest.mark.parametrize("incident_precision, stored, expected", [
    ("day", {"rank": 2, "precision": "month only", "lagging": False, "carried_by_no_record": False}, True),
    ("month", {"rank": 1, "precision": "year only", "lagging": False, "carried_by_no_record": False}, True),
    ("day", {"rank": 3, "precision": "day", "lagging": True, "carried_by_no_record": False}, True),
    ("day", {"rank": 3, "precision": "day", "lagging": False, "carried_by_no_record": False}, False),
    ("month", {"rank": 2, "precision": "month only", "lagging": False, "carried_by_no_record": False}, False),
    ("none", {"rank": 0, "precision": "unknown", "lagging": False, "carried_by_no_record": True}, False),
])
def test_only_a_more_precise_incident_date_can_redate(incident_precision, stored, expected):
    assert tr.redate_eligible(incident_precision, stored)[0] is expected


def _redate_files(tmp_path, labels, cutoff=0.8):
    from cyber_data_collector.utils.llm_models import typesafe_model
    cal = tmp_path / "redate_cal.json"
    cal.write_text(json.dumps({"question_version": tr.MATCH_VERSION, "model": typesafe_model(), "cutoff": cutoff}),
                   encoding="utf-8")
    lab = tmp_path / "redate_labels.csv"
    lab.write_text("deduplicated_event_id,label,correct_date,rationale,labeller\n"
                   + "".join(f"{d},{label},,r,test\n" for d, label in labels), encoding="utf-8")
    return cal, lab


def _reconcile(conn, tmp_path, labels, apply=True):
    cal, lab = _redate_files(tmp_path, labels)
    conn.execute("CREATE TABLE IF NOT EXISTS DedupSnapshots (snapshot_id TEXT PRIMARY KEY, batch_id TEXT, "
                 "deduplicated_event_id TEXT, payload_json TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    return tr.reconcile_all(conn, judge=JevJudge(conn, client=FakeJev()), apply=apply,
                            timeline_search=lambda q, m: {"answer": TIMELINE, "citations": []},
                            redate_calibration_path=cal, redate_labels_path=lab)


def test_a_calibrated_date_conflict_is_redated_and_audited(conn, tmp_path):
    out = _reconcile(conn, tmp_path, [("other", "unsure")])
    assert out["redate_calibrated"] and out["redates_applied"]
    assert [r["deduplicated_event_id"] for r in out["redates"]] == ["e2"]
    assert conn.execute("SELECT event_date FROM DeduplicatedEvents WHERE deduplicated_event_id = 'e2'"
                        ).fetchone()[0] == "2026-06-11"
    row = conn.execute("SELECT old_date, new_date FROM TimelineRedates").fetchone()
    assert tuple(row) == ("2026-02-01", "2026-06-11")
    # The unmatched UN Women event is handed to the calibrated victim step, not resolved here.
    e3 = next(e for o in out["organisations"] for e in o["events"] if e["deduplicated_event_id"] == "e3")
    assert e3["resolution"]["step"] == "check-victim-attribution"


def test_a_conflict_labelled_otherwise_is_never_redated(conn, tmp_path):
    out = _reconcile(conn, tmp_path, [("e2", "stored_date_right")])
    assert out["redates"] == []
    assert conn.execute("SELECT event_date FROM DeduplicatedEvents WHERE deduplicated_event_id = 'e2'"
                        ).fetchone()[0] == "2026-02-01"


def test_redating_refuses_without_labels(conn, tmp_path):
    with pytest.raises(RuntimeError, match="refusing"):
        tr.reconcile_all(conn, judge=JevJudge(conn, client=FakeJev()), apply=True,
                         timeline_search=lambda q, m: {"answer": TIMELINE, "citations": []},
                         redate_labels_path=tmp_path / "missing.csv")
