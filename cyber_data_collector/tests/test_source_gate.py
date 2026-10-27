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


UNRELATED = "elsewhere,reports_incident,r,t\n"


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
                              calibration_path=_cal(tmp_path), labels_path=_labels(tmp_path, UNRELATED),
                              search=_found(calls))
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


# --- reviewer findings 6, 9, 10: no silent fallbacks -------------------------

def test_apply_refuses_without_readable_labels(conn, tmp_path):
    for path in (tmp_path / "missing.csv", _labels(tmp_path)):  # missing, then header-only
        with pytest.raises(sg.LabelsUnavailable):
            sg.check_sources(conn, apply=True, judge=JevJudge(conn, client=FakeJev(JEV)),
                             calibration_path=_cal(tmp_path), labels_path=path, search=_found([]))
    status = dict(conn.execute("SELECT enriched_event_id, status FROM EnrichedEvents"))
    assert set(status.values()) == {"Active"}


def test_unreadable_calibration_is_report_only_and_logged(conn, tmp_path, caplog):
    bad = tmp_path / "cal.json"
    bad.write_text("{not json")
    result = sg.check_sources(conn, apply=True, judge=JevJudge(conn, client=FakeJev(JEV)),
                              calibration_path=bad, labels_path=_labels(tmp_path, UNRELATED), search=_found([]))
    assert result["removed"] == [] and not result["calibrated"]
    assert any(r.levelname == "ERROR" and "REPORT-ONLY" in r.getMessage() for r in caplog.records)


def test_unjudged_page_stops_the_run(conn, tmp_path):
    class Flaky(FakeJev):
        def system_one(self, state, questions, model=None):
            if state["url"] == "https://acme/home":
                raise ConnectionError("TypeSafe unreachable")
            return super().system_one(state, questions, model)

    with pytest.raises(RuntimeError, match="could not be judged"):
        sg.check_sources(conn, apply=True, judge=JevJudge(conn, client=Flaky(JEV)), calibration_path=_cal(tmp_path),
                         labels_path=_labels(tmp_path, UNRELATED), search=_found([]))
    assert conn.execute("SELECT status FROM EnrichedEvents WHERE enriched_event_id = 'menu'").fetchone()[0] == "Active"


def test_failed_replacement_search_is_retried_after_the_record_is_gone(conn, tmp_path):
    def broken(query, model):
        raise ConnectionError("Perplexity unreachable")

    labels = _labels(tmp_path, UNRELATED)
    first = sg.check_sources(conn, apply=True, judge=JevJudge(conn, client=FakeJev(JEV)),
                             calibration_path=_cal(tmp_path), labels_path=labels, search=broken)
    assert "policy" in {r["enriched_event_id"] for r in first["removed"]}
    status = conn.execute("SELECT status FROM SourceReplacements WHERE enriched_event_id = 'policy'").fetchone()[0]
    assert status == sg.STATUS_PENDING
    # Next run: 'policy' is inactive and no longer judged, but its search is retried.
    calls = []
    second = sg.check_sources(conn, apply=True, judge=JevJudge(conn, client=FakeJev(JEV)),
                              calibration_path=_cal(tmp_path), labels_path=labels, search=_found(calls))
    assert {r["replaces"] for r in second["replacements_queued"]} == {"policy"} and len(calls) == 1
    row = conn.execute("SELECT status, attempts FROM SourceReplacements WHERE enriched_event_id = 'policy'").fetchone()
    assert tuple(row) == (sg.STATUS_SEARCHED, 2)


def test_apply_without_any_search_refuses_before_removing(conn, tmp_path, monkeypatch):
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    with pytest.raises(sg.ReplacementSearchUnavailable):
        sg.check_sources(conn, apply=True, judge=JevJudge(conn, client=FakeJev(JEV)),
                         calibration_path=_cal(tmp_path), labels_path=_labels(tmp_path, UNRELATED))
    assert set(dict(conn.execute("SELECT enriched_event_id, status FROM EnrichedEvents")).values()) == {"Active"}


def test_malformed_research_date_never_suppresses_the_search(conn):
    # A different Lush attack years earlier must not count as covering an undated incident.
    conn.execute("INSERT INTO DeduplicatedEvents VALUES ('ev-lush-2016', 'Lush 2016 breach', '2016-01-01', 'Lush', "
                 "'Active', NULL)")
    conn.execute("UPDATE EnrichedEvents SET perplexity_enrichment_data = ? WHERE enriched_event_id = 'policy'",
                 (json.dumps({"formal_entity_name": "Lush Retail", "overall_confidence": 0.8,
                              "earliest_event_date": "early 2024", "reasoning": "Akira claimed Lush data."}),))
    calls = []
    assert sg.replace_evidence(conn, "policy", None, search=_found(calls)) is not None and len(calls) == 1


def test_an_event_being_rejected_does_not_cover_its_own_incident(conn, tmp_path):
    # The help page's event holds only that record, so it is rejected and cannot "cover" the incident.
    conn.execute("UPDATE EnrichedEvents SET perplexity_enrichment_data = ? WHERE enriched_event_id = 'help'",
                 (json.dumps({"formal_entity_name": "Service NSW", "overall_confidence": 0.9,
                              "earliest_event_date": "2024-01-26", "reasoning": "x"}),))
    conn.execute("UPDATE DeduplicatedEvents SET victim_organization_name = 'Service NSW' "
                 "WHERE deduplicated_event_id = 'ev-help'")
    calls = []
    result = sg.check_sources(conn, apply=True, judge=JevJudge(conn, client=FakeJev(JEV)),
                              calibration_path=_cal(tmp_path), labels_path=_labels(tmp_path, UNRELATED),
                              search=_found(calls))
    assert "help" in {r["replaces"] for r in result["replacements_queued"]}


def test_a_labelled_real_event_keeps_going_without_its_junk_page(conn, tmp_path):
    """Rejecting it contradicted the event label, and check-events then restored the junk page with it."""
    event_labels = tmp_path / "event_labels.csv"
    event_labels.write_text("deduplicated_event_id,label,rationale,corroboration,labeller\n"
                            "ev-help,real,reported elsewhere,packet,test\n", encoding="utf-8")
    result = sg.check_sources(conn, apply=True, judge=JevJudge(conn, client=FakeJev(JEV)),
                              calibration_path=_cal(tmp_path), labels_path=_labels(tmp_path, UNRELATED),
                              search=_found([]), event_labels_path=event_labels)
    actions = {r["enriched_event_id"]: r["action"] for r in result["removed"]}
    assert actions["help"].startswith("deactivated; event kept")
    assert conn.execute("SELECT status FROM DeduplicatedEvents WHERE deduplicated_event_id = 'ev-help'").fetchone()[0] \
        == "Active"
    assert conn.execute("SELECT status FROM EnrichedEvents WHERE enriched_event_id = 'help'").fetchone()[0] == "Inactive"
