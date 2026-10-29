"""Separate incidents merged under one organisation: candidates need evidence of a different incident
(never a date gap by itself), a calibrated cut-off decides, labels win, and nothing splits uncalibrated."""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

pytest.importorskip("typesafe_sdk")

from cyber_data_collector.dedup import coherence as co  # noqa: E402
from cyber_data_collector.dedup import coherence_same_org as so  # noqa: E402
from cyber_data_collector.dedup import org_timeline  # noqa: E402
from cyber_data_collector.dedup.entity_resolution import EntityResolver  # noqa: E402
from cyber_data_collector.utils.jev import JevJudge  # noqa: E402

EVENT_DATE = "2025-03-10"
TIMELINE = ("1. March 2025 | S3 bucket exposure of 27,000 records | found by a researcher | https://x.example/1\n"
            "2. February 2026 | MongoDB Atlas theft of 444,000 borrowers | extortion claim | https://x.example/2")


def _pe(name, date=None):
    data = {"formal_entity_name": name}
    if date:
        data["earliest_event_date"] = date
    return json.dumps(data)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, title TEXT, event_date TEXT,
            victim_organization_name TEXT, vendor_organization_name TEXT, status TEXT);
        CREATE TABLE EventDeduplicationMap (map_id TEXT PRIMARY KEY, enriched_event_id TEXT, deduplicated_event_id TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title TEXT, event_date TEXT,
            summary TEXT, description TEXT, perplexity_enrichment_data TEXT);
        CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_url TEXT, raw_content TEXT, raw_description TEXT,
            event_date TEXT, source_metadata TEXT, discovered_at TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT);
    """)
    org_timeline.ensure_schema(c)

    def event(did, title, victim, members, date=EVENT_DATE):
        c.execute("INSERT INTO DeduplicatedEvents VALUES (?,?,?,?,NULL,'Active')", (did, title, date, victim))
        for eid, t, org, when, pe_date, discovered in members:
            c.execute("INSERT INTO EnrichedEvents VALUES (?,?,?,?,NULL,NULL,?)", (eid, eid, t, when, _pe(org, pe_date)))
            c.execute("INSERT INTO RawEvents VALUES (?,?,?,NULL,NULL,NULL,?)",
                      (eid, f"https://news.example.com/{eid}", f"Page about {t}", discovered))
            c.execute("INSERT INTO EventDeduplicationMap VALUES (?,?,?)", ("m" + eid, eid, did))
    # a record's date is verified when Perplexity researched that very date; a record dated the day it
    # was discovered is only a publication/found date.
    event("youx", "youX data breach", "youX", [
        ("a1", "youX S3 exposure", "youX", EVENT_DATE, EVENT_DATE, "2026-06-01"),
        ("a2", "youX bucket left open", "youX Pty Ltd", EVENT_DATE, EVENT_DATE, "2026-06-01"),
        ("a3", "youX exposure confirmed", "youX", EVENT_DATE, EVENT_DATE, "2026-06-01"),
        ("far_verified", "youX MongoDB theft", "youX", "2026-02-10", "2026-02-10", "2026-06-01"),
        ("far_found", "youX breach report", "youX", "2026-02-10", None, "2026-02-10"),
        ("far_unknown", "youX update", "youX", "2026-02-10", None, "2026-06-01"),
        ("other_org", "Mathspace breach", "Mathspace Pty Ltd", EVENT_DATE, EVENT_DATE, "2026-06-01")])
    event("quiet", "Optus breach", "Optus", [
        ("o1", "Optus breach", "Optus", "2022-09-22", "2022-09-22", "2026-06-01"),
        ("o2", "Optus data exposed", "Singtel Optus Pty Limited", "2022-09-30", "2022-09-30", "2026-06-01")],
        date="2022-09-22")
    c.commit()
    yield c
    c.close()


class FakeTypeSafe:
    def __init__(self, p_by_title=None, default=0.95):
        self.p_by_title, self.default, self.states = p_by_title or {}, default, []

    def system_one(self, state, questions, model=None):
        self.states.append(state)
        title = state["record"]["title"]
        p = next((v for k, v in self.p_by_title.items() if k in title), self.default)
        return SimpleNamespace(model="jev-1.14.0", usage=None,
                               answers={q: SimpleNamespace(type="noul", noul=p) for q in questions})


class FakeEscalation:
    def __init__(self, p=None, fail=False):
        self.p, self.fail, self.calls = p, fail, 0
        self.chat = self
        self.completions = self

    def create_with_completion(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("openai down")
        return SimpleNamespace(probability=self.p, rationale="r"), SimpleNamespace(model="gpt-5.5-2026-04-23", usage=None)


class FakeLedger:
    def __init__(self):
        self.splits = []

    def split_members(self, dedup_id, ids, reason, actor="human"):
        self.splits.append((dedup_id, list(ids), reason))
        return f"new-{len(self.splits)}"


@pytest.fixture(autouse=True)
def _models(monkeypatch):
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    # A test that forgets to pass a fake escalation client must fail, not make a paid call.
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")


def _cal(tmp_path, cutoff, **override):
    path = tmp_path / "cal.json"
    path.write_text(json.dumps({"cutoff": cutoff, **so.calibration_identity(), **override}))
    return path


def _labels(tmp_path, rows=()):
    path = tmp_path / "labels.csv"
    path.write_text("deduplicated_event_id,enriched_event_id,label,rationale,incident,labeller\n"
                    + "".join(f"{d},{e},{lab},r,{inc},t\n" for d, e, lab, inc in rows), encoding="utf-8")
    return path


def _event(conn, did="youx"):
    return next(e for e in so.find_candidate_events(conn, EntityResolver(conn)) if e["id"] == did)


# --- candidates -------------------------------------------------------------------------------

def test_only_a_verified_far_date_makes_a_candidate(conn):
    event = _event(conn)
    assert sorted(event["candidates"]) == ["far_verified"]
    assert "days from the event's date interval" in event["candidates"]["far_verified"][0]


def test_a_date_that_is_only_a_publication_or_found_date_is_no_evidence(conn):
    event = _event(conn)
    # same far date, but the date equals the day the record was found / has no support: not a candidate
    assert "far_found" not in event["candidates"] and "far_unknown" not in event["candidates"]
    facts = so.member_date_facts(conn, "far_found")
    assert facts["verified"] is False and "found" in facts["basis"]


def test_members_naming_another_organisation_belong_to_the_stray_check(conn):
    event = _event(conn)
    assert event["suspect_ids"] == ["other_org"] and "other_org" not in event["candidates"]


def test_a_close_date_is_not_a_candidate(conn):
    conn.execute("UPDATE EnrichedEvents SET event_date = '2025-04-20' WHERE enriched_event_id = 'far_verified'")
    conn.execute("UPDATE EnrichedEvents SET perplexity_enrichment_data = ? WHERE enriched_event_id = 'far_verified'",
                 (_pe("youX", "2025-04-20"),))
    assert "youx" not in {e["id"] for e in so.find_candidate_events(conn, EntityResolver(conn))}


def test_a_different_incident_in_the_organisations_timeline_makes_a_candidate(conn):
    conn.execute("INSERT INTO EntitiesV2 VALUES (1, 'youX')")
    conn.execute("INSERT INTO DeduplicatedEventEntities VALUES ('youx', 1, 'victim')")
    conn.execute("INSERT INTO OrganisationTimelines VALUES (1, 'youX', 'h', 'q', 'm', ?, '[]', '2026-10-28')", (TIMELINE,))
    # close in time to the event (so no date reason), but matching incident 2 in the timeline
    conn.execute("UPDATE EnrichedEvents SET event_date = '2026-02-20' WHERE enriched_event_id = 'far_unknown'")
    conn.execute("UPDATE EnrichedEvents SET perplexity_enrichment_data = ? WHERE enriched_event_id = 'far_unknown'",
                 (_pe("youX", "2026-02-20"),))
    event = _event(conn)
    assert any("incident 2 in the organisation's timeline" in r for r in event["candidates"]["far_unknown"])


def test_no_timeline_and_no_verified_date_means_no_candidate(conn):
    assert {e["id"] for e in so.find_candidate_events(conn, EntityResolver(conn))} == {"youx"}


# --- evidence ---------------------------------------------------------------------------------

def test_the_member_state_carries_date_provenance_and_why_it_was_flagged(conn):
    event = _event(conn)
    state = so.build_member_state(conn, event, "far_verified")
    assert state["record"]["date_provenance"]["basis"] == "Perplexity research"
    assert state["flagged_because"] and "event" in state and state["event"]["victim"] == "youX"
    assert "other_records" in state


def test_the_question_is_a_distinct_versioned_variant(conn):
    assert so.QUESTION_ID != co.QUESTION_ID and so.QUESTION_VERSION != co.QUESTION_VERSION
    text = so.INSTRUCTIONS
    assert "NOT evidence" in text and "SAME real-world incident" in text


# --- judging ----------------------------------------------------------------------------------

def test_candidates_are_judged_and_answers_stored_under_their_own_subject_type(conn):
    event = _event(conn)
    judge = JevJudge(conn, client=FakeTypeSafe(default=0.9))
    scores = so.judge_members(conn, [(event, "far_verified")], judge=judge)
    assert scores["youx|far_verified"]["p"] == 0.9
    assert conn.execute("SELECT subject_type, question_id FROM ModelJudgements").fetchone() == (
        so.SUBJECT_TYPE, so.QUESTION_ID)


def test_a_jev_failure_raises_rather_than_skipping(conn):
    class Broken:
        def system_one(self, *a, **k):
            raise RuntimeError("down")

    with pytest.raises(co.CoherenceJudgementError, match="same-organisation member"):
        so.judge_members(conn, [(_event(conn), "far_verified")], judge=JevJudge(conn, client=Broken()))


def test_the_middle_band_is_escalated_and_a_failed_escalation_leaves_the_member_undecided(conn):
    event = _event(conn)
    ok = so.judge_members(conn, [(event, "far_verified")], judge=JevJudge(conn, client=FakeTypeSafe(default=0.4)),
                          escalation_client=FakeEscalation(p=0.05))
    assert ok["youx|far_verified"]["p"] == 0.05 and ok["youx|far_verified"]["p_jev"] == 0.4
    conn.execute("DELETE FROM ModelJudgements")
    bad = so.judge_members(conn, [(event, "far_verified")], judge=JevJudge(conn, client=FakeTypeSafe(default=0.4)),
                           escalation_client=FakeEscalation(fail=True))
    assert bad["youx|far_verified"]["p"] is None and bad["youx|far_verified"]["error"]


# --- deciding and repairing -------------------------------------------------------------------

def test_nothing_is_split_without_a_calibration(conn, tmp_path):
    ledger = FakeLedger()
    result = so.check_same_organisation(conn, apply=True, judge=JevJudge(conn, client=FakeTypeSafe(default=0.01)),
                                        calibration_path=tmp_path / "none.json",
                                        labels_path=tmp_path / "no_labels.csv", ledger=ledger)
    assert not result["calibrated"] and ledger.splits == [] and result["candidates_judged"] == 1


def test_a_stale_calibration_splits_nothing(conn, tmp_path):
    ledger = FakeLedger()
    so.check_same_organisation(conn, apply=True, judge=JevJudge(conn, client=FakeTypeSafe(default=0.01)),
                               calibration_path=_cal(tmp_path, 0.3, evidence="coherence-same-org:0000"),
                               labels_path=tmp_path / "no_labels.csv", ledger=ledger)
    assert ledger.splits == []


def test_a_candidate_below_the_calibrated_cutoff_leaves_through_the_ledger(conn, tmp_path):
    ledger = FakeLedger()
    result = so.check_same_organisation(conn, apply=True, judge=JevJudge(conn, client=FakeTypeSafe(default=0.05)),
                                        calibration_path=_cal(tmp_path, 0.3), labels_path=tmp_path / "no_labels.csv",
                                        ledger=ledger)
    assert result["calibrated"] and result["applied"]
    assert ledger.splits == [("youx", ["far_verified"], ledger.splits[0][2])]
    assert "[coherence-same-org]" in ledger.splits[0][2] and "0.050" in ledger.splits[0][2]


def test_without_apply_the_finding_is_reported_not_split(conn, tmp_path):
    ledger = FakeLedger()
    result = so.check_same_organisation(conn, apply=False, judge=JevJudge(conn, client=FakeTypeSafe(default=0.05)),
                                        calibration_path=_cal(tmp_path, 0.3), labels_path=tmp_path / "no_labels.csv",
                                        ledger=ledger)
    assert ledger.splits == [] and result["records_to_split"] == 1


def test_above_the_cutoff_it_stays(conn, tmp_path):
    ledger = FakeLedger()
    so.check_same_organisation(conn, apply=True, judge=JevJudge(conn, client=FakeTypeSafe(default=0.8)),
                               calibration_path=_cal(tmp_path, 0.3), labels_path=tmp_path / "no_labels.csv", ledger=ledger)
    assert ledger.splits == []


def test_labels_are_ground_truth_both_ways(conn, tmp_path):
    ledger = FakeLedger()
    # labelled same never leaves, whatever the model; labelled different leaves even uncalibrated
    so.check_same_organisation(conn, apply=True, judge=JevJudge(conn, client=FakeTypeSafe(default=0.0)),
                               calibration_path=_cal(tmp_path, 0.3),
                               labels_path=_labels(tmp_path, [("youx", "far_verified", "same", "")]), ledger=ledger)
    assert ledger.splits == []
    so.check_same_organisation(conn, apply=True, judge=JevJudge(conn, client=FakeTypeSafe(default=0.99)),
                               calibration_path=tmp_path / "none.json",
                               labels_path=_labels(tmp_path, [("youx", "a3", "different", "S3 exposure (2025)")]),
                               ledger=ledger)
    assert ledger.splits and ledger.splits[0][1] == ["a3"] and "labelled a separate incident" in ledger.splits[0][2]


def test_the_departing_records_must_be_fewer_than_those_that_stay():
    members = [{"id": f"m{i}", "org": "Acme", "title": "t", "date": None, "body": "", "url": None} for i in range(4)]
    event = {"id": "e", "title": "Acme breach", "victim": "Acme", "members": members, "suspect_ids": [],
             "candidates": {"m0": ["r"], "m1": ["r"], "m2": ["r"]}}
    scores = {f"e|m{i}": {"p": 0.01, "p_jev": 0.01, "p_escalated": None} for i in range(3)}
    out = so.decide_splits(event, scores, {}, 0.3)
    assert out["leave"] == [] and {h["guard"] for h in out["held"]} == {
        "the departing records are not fewer than those that stay"}
    assert "review the event" in out["note"]


def test_an_event_is_never_stripped_bare():
    members = [{"id": "m0", "org": "Acme", "title": "t", "date": None, "body": "", "url": None},
               {"id": "m1", "org": "Acme", "title": "t", "date": None, "body": "", "url": None}]
    event = {"id": "e", "title": "Acme breach", "victim": "Acme", "members": members, "suspect_ids": [],
             "candidates": {}}
    labels = {"e|m0": "different", "e|m1": "different"}
    out = so.decide_splits(event, {}, labels, None)
    assert out["leave"] == [] and out["held"]


def test_a_malformed_labels_file_raises(conn, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("deduplicated_event_id,enriched_event_id,label\nyoux,a1,maybe\n")
    with pytest.raises(co.CoherenceConfigError):
        so.check_same_organisation(conn, judge=JevJudge(conn, client=FakeTypeSafe()), labels_path=bad,
                                   calibration_path=tmp_path / "none.json")
