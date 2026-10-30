"""Tests for stray-record detection (event coherence): suspects, Jev judgement with
escalation and no fallback, calibrated splits, labels as ground truth, and the guards."""
from __future__ import annotations

import json
import logging
import sqlite3
from types import SimpleNamespace

import pytest

pytest.importorskip("typesafe_sdk")

from cyber_data_collector.dedup import coherence as co  # noqa: E402
from cyber_data_collector.utils.jev import JevJudge  # noqa: E402


def _pe(name):
    return json.dumps({"formal_entity_name": name})


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, title TEXT, event_date TEXT,
            victim_organization_name TEXT, vendor_organization_name TEXT, status TEXT);
        CREATE TABLE EventDeduplicationMap (map_id TEXT PRIMARY KEY, enriched_event_id TEXT, deduplicated_event_id TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title TEXT, event_date TEXT,
            summary TEXT, description TEXT, perplexity_enrichment_data TEXT);
        CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_url TEXT, raw_content TEXT, raw_description TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
    """)

    def event(did, title, victim, members, vendor=None, date="2026-08-23"):
        c.execute("INSERT INTO DeduplicatedEvents VALUES (?,?,?,?,?, 'Active')", (did, title, date, victim, vendor))
        for eid, t, org in members:
            c.execute("INSERT INTO EnrichedEvents VALUES (?,?,?,?,NULL,NULL,?)", (eid, eid, t, date, _pe(org)))
            c.execute("INSERT INTO RawEvents VALUES (?, ?, ?, NULL)",
                      (eid, f"https://news.example.com/{eid}", f"Page about {t}"))
            c.execute("INSERT INTO EventDeduplicationMap VALUES (?,?,?)", ("m" + eid, eid, did))
    event("sharp", "Sharp Motor Group cyber incident", "Sharp Motor Group", [
        ("s1", "Sharp Motor Group confirms incident", "Sharp Motor Group Pty Ltd"),
        ("s2", "Sharp Motor Group ransomware", "Sharp Motor Group"),
        ("x1", "Mathspace says 1m Aussies affected", "Mathspace Pty Ltd")])
    event("canvas", "Canvas LMS breach", "Instructure", vendor="Instructure, Inc.", members=[
        ("c1", "Instructure confirms breach", "Instructure, Inc."),
        ("c2", "Instructure Canvas incident", "Instructure, Inc."),
        ("c3", "University of Sydney Canvas data", "University of Sydney")])
    event("clean", "Optus breach", "Optus", [
        ("o1", "Optus breach", "Optus"), ("o2", "Optus data exposed", "Singtel Optus Pty Limited")])
    event("math", "Mathspace breach", "Mathspace Pty Ltd", [("m1", "Mathspace breach", "Mathspace Pty Ltd")],
          date="2026-08-10")
    c.commit()
    yield c
    c.close()


class FakeTypeSafe:
    """Jev double: P(same) by enriched id of the record under review."""

    def __init__(self, p_by_record, default=0.95):
        self.p_by_record = p_by_record
        self.default = default
        self.states = []

    def system_one(self, state, questions, model=None):
        self.states.append(state)
        title = state["record"]["title"]
        p = next((v for k, v in self.p_by_record.items() if k in title), self.default)
        return SimpleNamespace(model="jev-1.14.0", usage=None,
                               answers={q: SimpleNamespace(type="noul", noul=p) for q in questions})


class Broken:
    def system_one(self, state, questions, model=None):
        raise RuntimeError("service unavailable")


class FakeEscalation:
    def __init__(self, p=None, fail=False):
        self.p, self.fail, self.calls = p, fail, 0
        self.chat = self
        self.completions = self

    def create_with_completion(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("openai down")
        return (SimpleNamespace(probability=self.p, rationale="r"),
                SimpleNamespace(model="gpt-5.5-2026-04-23", usage=None))


class FakeLedger:
    def __init__(self):
        self.splits = []

    def split_members(self, dedup_id, ids, reason, actor="human"):
        self.splits.append((dedup_id, list(ids), reason))
        return f"new-{len(self.splits)}"


def _cal(tmp_path, cutoff, **override):
    path = tmp_path / "cal.json"
    path.write_text(json.dumps({"cutoff": cutoff, **co.calibration_identity(), **override}))
    return path


def _labels(tmp_path, rows=()):
    path = tmp_path / "labels.csv"
    path.write_text("deduplicated_event_id,enriched_event_id,label,rationale,evidence,labeller\n"
                    + "".join(f"{d},{e},{lab},r,packet,t\n" for d, e, lab in rows), encoding="utf-8")
    return path


# --- suspects -----------------------------------------------------------------

def test_suspects_are_members_naming_another_organisation(conn):
    found = {e["id"]: e["suspect_ids"] for e in co.find_suspect_events(conn)}
    assert found["sharp"] == ["x1"]
    assert "clean" not in found          # Optus / Singtel Optus share the 'optus' key
    assert "canvas" in found             # a customer record is a suspect; the model decides


def test_labelled_events_are_included_without_a_suspect(conn):
    found = {e["id"] for e in co.find_suspect_events(conn, include=["clean"])}
    assert "clean" in found


# --- judging ----------------------------------------------------------------------

def test_member_state_carries_record_evidence_and_the_event(conn):
    event = next(e for e in co.find_suspect_events(conn) if e["id"] == "sharp")
    state = co.build_member_state(conn, event, "x1")
    assert state["event"]["victim"] == "Sharp Motor Group"
    assert state["record"]["title"] == "Mathspace says 1m Aussies affected"
    assert state["record"]["page_text"].startswith("Page about")
    assert [r["title"] for r in state["other_records"]] == ["Sharp Motor Group confirms incident",
                                                            "Sharp Motor Group ransomware"]
    assert "organisation_timeline" not in state


def test_member_state_carries_the_victims_timeline_in_judgement_shape(conn, monkeypatch):
    from cyber_data_collector.dedup import org_timeline
    shown = {"organisation": "Sharp Motor Group", "incidents": "2025-05: ransomware", "citations": ["u"]}
    monkeypatch.setattr(org_timeline, "timeline_for_event", lambda c, d: dict(shown))
    event = next(e for e in co.find_suspect_events(conn) if e["id"] == "sharp")
    assert co.build_member_state(conn, event, "x1")["organisation_timeline"] == shown


def test_judgements_are_stored_and_reused(conn):
    event = next(e for e in co.find_suspect_events(conn) if e["id"] == "sharp")
    fake = FakeTypeSafe({"Mathspace": 0.02})
    scores = co.judge_members(conn, [(event, "x1")], judge=JevJudge(conn, client=fake))
    assert scores["sharp|x1"]["p"] == pytest.approx(0.02)
    again = co.judge_members(conn, [(event, "x1")], judge=JevJudge(conn, client=Broken()))
    assert again["sharp|x1"]["p"] == pytest.approx(0.02)     # stored answer, no call
    assert len(fake.states) == 1


def test_middle_band_is_escalated_and_its_score_is_final(conn):
    event = next(e for e in co.find_suspect_events(conn) if e["id"] == "sharp")
    esc = FakeEscalation(p=0.05)
    scores = co.judge_members(conn, [(event, "x1")], judge=JevJudge(conn, client=FakeTypeSafe({"Mathspace": 0.4})),
                              escalation_client=esc)
    assert scores["sharp|x1"] == {"p_jev": pytest.approx(0.4), "p_escalated": pytest.approx(0.05),
                                  "p": pytest.approx(0.05), "error": None}
    assert esc.calls == 1
    assert co.stored_final_probabilities(conn, [(event, "x1")])["sharp|x1"] == pytest.approx(0.05)


def test_a_member_score_is_read_only_from_a_decision_on_its_current_evidence(conn):
    event = next(e for e in co.find_suspect_events(conn) if e["id"] == "sharp")
    co.judge_members(conn, [(event, "x1")], judge=JevJudge(conn, client=FakeTypeSafe({"Mathspace": 0.4})),
                     escalation_client=FakeEscalation(p=0.05))
    assert co.stored_final_probabilities(conn, [(event, "x1")]) == {"sharp|x1": pytest.approx(0.05)}
    conn.execute("UPDATE EnrichedEvents SET title = 'Corrected title' WHERE enriched_event_id = 'x1'")
    # The record changed: the old escalation (0.05) is not a score for it any more, and is not combined with
    # a fresh Jev answer either - it has no score until it is judged again.
    assert co.stored_final_probabilities(conn, [(event, "x1")]) == {}


def test_jev_failure_raises_instead_of_falling_back(conn):
    event = next(e for e in co.find_suspect_events(conn) if e["id"] == "sharp")
    with pytest.raises(co.CoherenceJudgementError, match="Jev"):
        co.judge_members(conn, [(event, "x1")], judge=JevJudge(conn, client=Broken()))


def test_failed_escalation_leaves_the_member_undecided_not_judged_by_jev(conn):
    event = next(e for e in co.find_suspect_events(conn) if e["id"] == "sharp")
    scores = co.judge_members(conn, [(event, "x1")],
                              judge=JevJudge(conn, client=FakeTypeSafe({"Mathspace": 0.15})),
                              escalation_client=FakeEscalation(fail=True))
    assert scores["sharp|x1"]["p"] is None and scores["sharp|x1"]["error"]
    # Not split on Jev's 0.15, even with a cut-off above it.
    out = co.decide_strays(event, scores, {}, 0.3, co.EntityResolver(conn))
    assert out["strays"] == []


def test_undecided_member_defers_every_model_split_in_its_event(conn, tmp_path):
    judge = JevJudge(conn, client=FakeTypeSafe({"Mathspace": 0.02, "University of Sydney": 0.15}))
    ledger = FakeLedger()
    report = co.check_coherence(conn, apply=True, judge=judge, ledger=ledger,
                                escalation_client=FakeEscalation(fail=True),
                                calibration_path=_cal(tmp_path, 0.3), labels_path=_labels(tmp_path, [("math", "m1", "same")]))
    assert report["undecided"] == ["canvas|c3"]
    assert [s[:2] for s in ledger.splits] == [("sharp", ["x1"])]   # the other event is unaffected


# --- calibration and labels -------------------------------------------------------

def test_calibration_must_match_models_and_question(tmp_path, caplog):
    assert co.active_calibration(_cal(tmp_path, 0.3))["cutoff"] == 0.3
    with caplog.at_level(logging.WARNING):
        assert co.active_calibration(_cal(tmp_path, 0.3, question_version="member-coherence-v0")) is None
    assert "REPORT ONLY" in caplog.text
    assert co.active_calibration(_cal(tmp_path, None)) is None       # no cut-off met the target


@pytest.mark.parametrize("content", [None, "", "{}", "not json"])
def test_missing_or_broken_calibration_is_an_error(tmp_path, content):
    path = tmp_path / "cal.json"
    if content is not None:
        path.write_text(content)
    with pytest.raises(co.CoherenceConfigError):
        co.active_calibration(path)


@pytest.mark.parametrize("content", [None, "", "deduplicated_event_id,enriched_event_id,label\n",
                                     "deduplicated_event_id,enriched_event_id,label\ne,m,maybe\n"])
def test_missing_or_broken_labels_are_an_error(tmp_path, content):
    path = tmp_path / "labels.csv"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(co.CoherenceConfigError):
        co.load_labels(path)


def test_apply_refuses_without_labels_or_calibration(conn, tmp_path):
    judge = JevJudge(conn, client=FakeTypeSafe({"Mathspace": 0.02}))
    ledger = FakeLedger()
    with pytest.raises(co.CoherenceConfigError):
        co.check_coherence(conn, apply=True, judge=judge, ledger=ledger,
                           calibration_path=_cal(tmp_path, 0.3), labels_path=tmp_path / "gone.csv")
    with pytest.raises(co.CoherenceConfigError):
        co.check_coherence(conn, apply=True, judge=judge, ledger=ledger,
                           calibration_path=tmp_path / "gone.json", labels_path=_labels(tmp_path, [("a", "b", "same")]))
    assert ledger.splits == []
    # Without --apply the same run reports.
    report = co.check_coherence(conn, apply=False, judge=judge, ledger=ledger,
                                calibration_path=tmp_path / "gone.json", labels_path=tmp_path / "gone.csv")
    assert report["calibrated"] is False and ledger.splits == []


# --- deciding -------------------------------------------------------------------

def _scores(event, p_by_id):
    return {co.member_subject_id(event["id"], k): {"p": v, "p_jev": v, "p_escalated": None}
            for k, v in p_by_id.items()}


def test_only_suspects_below_the_cutoff_leave(conn):
    resolver = co.EntityResolver(conn)
    event = next(e for e in co.find_suspect_events(conn, resolver) if e["id"] == "sharp")
    scores = _scores(event, {"x1": 0.1, "s1": 0.01})          # s1 is not a suspect
    out = co.decide_strays(event, scores, {}, 0.3, resolver)
    assert [s["id"] for s in out["strays"]] == ["x1"]
    assert co.decide_strays(event, scores, {}, 0.05, resolver)["strays"] == []   # above the cut-off
    assert co.decide_strays(event, scores, {}, None, resolver)["strays"] == []   # uncalibrated


def test_labels_are_ground_truth(conn):
    resolver = co.EntityResolver(conn)
    event = next(e for e in co.find_suspect_events(conn, resolver) if e["id"] == "sharp")
    scores = _scores(event, {"x1": 0.1})
    keep = co.decide_strays(event, scores, {"sharp|x1": "same"}, 0.3, resolver)
    assert keep["strays"] == []
    # 'different' leaves even when the model is sure it belongs, uncalibrated, and not a suspect.
    out = co.decide_strays(event, _scores(event, {"x1": 0.99}), {"sharp|s2": "different", "sharp|x1": "different"},
                           None, resolver)
    assert sorted(s["id"] for s in out["strays"]) == ["s2", "x1"]
    assert all(s["basis"] == "label" for s in out["strays"])


def test_mislabelled_event_is_not_gutted():
    """youX case: most members leave, all naming one organisation - the label is wrong."""
    resolver = co.EntityResolver()
    resolver.fit(["youX Pty Ltd", "Vroom", "Mathspace", "Sharp Motor Group", "Optus"])
    members = [{"id": f"y{i}", "org": "youX Pty Ltd", "title": "youX breach", "date": None, "body": "", "url": None}
               for i in range(4)] + [{"id": "v1", "org": "Vroom", "title": "Vroom", "date": None, "body": "",
                                      "url": None}]
    event = {"id": "e", "title": "Vroom data exposure", "members": members,
             "suspect_ids": [f"y{i}" for i in range(4)]}
    out = co.decide_strays(event, _scores(event, {f"y{i}": 0.01 for i in range(4)}), {}, 0.3, resolver)
    assert out["strays"] == []
    assert out["note"] and {h["guard"] for h in out["held"]} == {"mislabelled event"}


def test_member_named_in_the_event_title_stays():
    resolver = co.EntityResolver()
    resolver.fit(["youX Pty Ltd", "Vroom", "Mathspace", "Sharp Motor Group"])
    members = [{"id": "a", "org": "Vroom", "title": "Vroom by YouX breach", "date": None, "body": "", "url": None},
               {"id": "b", "org": "Vroom", "title": "Vroom breach", "date": None, "body": "", "url": None},
               {"id": "y", "org": "youX Pty Ltd", "title": "youX breach", "date": None, "body": "", "url": None}]
    event = {"id": "e", "title": "Vroom by YouX Data Breach", "members": members, "suspect_ids": ["y"]}
    out = co.decide_strays(event, _scores(event, {"y": 0.01}), {}, 0.3, resolver)
    assert out["strays"] == [] and out["held"][0]["guard"] == "organisation named in the event title"


def test_never_strips_an_event_bare():
    resolver = co.EntityResolver()
    resolver.fit(["Alpha", "Beta", "Gamma"])
    members = [{"id": "a", "org": "Alpha", "title": "a", "date": None, "body": "", "url": None},
               {"id": "b", "org": "Beta", "title": "b", "date": None, "body": "", "url": None}]
    event = {"id": "e", "title": "Gamma incident", "members": members, "suspect_ids": ["a", "b"]}
    assert co.decide_strays(event, _scores(event, {"a": 0.01, "b": 0.01}), {}, 0.3, resolver)["strays"] == []


def test_labelled_strays_naming_one_incident_leave_together():
    resolver = co.EntityResolver()
    resolver.fit(["Western Australia Department of Health", "Updoc", "Instructure", "University of Canberra"])
    strays = [{"id": "1", "org": "Western Australia Department of Health", "incident": "WA Health pager leak"},
              {"id": "2", "org": "Department of Health, Western Australia", "incident": "WA health pager leak "},
              {"id": "3", "org": "Instructure, Inc.", "incident": "Canvas breach (2026)"},
              {"id": "4", "org": "University of Canberra", "incident": "Canvas breach (2026)"}]
    groups = co.group_strays(strays, resolver)
    assert sorted(sorted(s["id"] for s in g) for g in groups) == [["1", "2"], ["3", "4"]]


def test_model_strays_group_by_organisation_only_within_the_window():
    resolver = co.EntityResolver()
    resolver.fit(["University of Sydney", "University of Tasmania", "Updoc", "Optus", "Medibank"])
    strays = [{"id": "code", "org": "The University of Sydney", "date": "2025-12-11"},
              {"id": "canvas", "org": "The University of Sydney", "date": "2026-05-01"},
              {"id": "canvas2", "org": "University of Sydney", "date": "2026-05-03"},
              {"id": "up", "org": "Updoc Pty Ltd", "date": "2026-05-02"}]
    groups = co.group_strays(strays, resolver)
    assert sorted(sorted(s["id"] for s in g) for g in groups) == [["canvas", "canvas2"], ["code"], ["up"]]


# --- end to end -------------------------------------------------------------------

def test_check_coherence_splits_only_with_a_matching_calibration(conn, tmp_path):
    judge = JevJudge(conn, client=FakeTypeSafe({"Mathspace": 0.02, "University of Sydney": 0.9}))
    ledger = FakeLedger()
    labels = _labels(tmp_path, [("math", "m1", "same")])
    report = co.check_coherence(conn, apply=True, judge=judge, ledger=ledger, labels_path=labels,
                                calibration_path=_cal(tmp_path, 0.3, model="jev-0.0+gpt-old"))
    assert report["calibrated"] is False and ledger.splits == []
    assert report["strays"] == 0

    report = co.check_coherence(conn, apply=True, judge=judge, ledger=ledger, labels_path=labels,
                                calibration_path=_cal(tmp_path, 0.3))
    assert ledger.splits[0][:2] == ("sharp", ["x1"])          # Canvas customer (0.9) stays
    assert "below the calibrated cut-off" in ledger.splits[0][2]
    assert report["split_records"] == 1


def test_check_coherence_without_apply_changes_nothing(conn, tmp_path):
    judge = JevJudge(conn, client=FakeTypeSafe({"Mathspace": 0.02}))
    ledger = FakeLedger()
    report = co.check_coherence(conn, apply=False, judge=judge, ledger=ledger,
                                calibration_path=_cal(tmp_path, 0.3),
                                labels_path=_labels(tmp_path, [("clean", "o2", "different")]))
    assert ledger.splits == []
    assert report["strays"] == 2                                # x1 by the model, o2 by its label


def test_labels_decide_first_so_a_labelled_member_is_not_asked_about(conn, tmp_path):
    fake = FakeTypeSafe({"Mathspace": 0.9})                       # the model would keep x1; its label says it leaves
    report = co.check_coherence(conn, apply=False, judge=JevJudge(conn, client=fake), ledger=FakeLedger(),
                                calibration_path=_cal(tmp_path, 0.3),
                                labels_path=_labels(tmp_path, [("sharp", "x1", "different")]))
    assert report["settled_by_label"] == 1 and "sharp|x1" not in report["scores"]
    assert report["strays"] >= 1                                  # decided by the label, not asked about


def test_command_fails_loudly_without_keys(monkeypatch, capsys):
    from scripts import dedup_admin
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    rc = dedup_admin.cmd_check_coherence(SimpleNamespace(db=":memory:", apply=True, event=None))
    assert rc == 2
    assert "TYPESAFE_API_KEY" in capsys.readouterr().err


def test_a_member_naming_no_organisation_is_a_suspect_only_when_the_victim_is_in_doubt(conn, monkeypatch):
    # The OpenAI rogue-agent event held a report of the separate Medicare breach whose research named no one.
    def event_with(victim_doubted):
        monkeypatch.setattr(co, "victim_in_doubt", lambda c: {"rogue"} if victim_doubted else set())
        return next((e for e in co.find_suspect_events(conn, co.EntityResolver(conn)) if e["id"] == "rogue"), None)

    conn.execute("INSERT INTO DeduplicatedEvents VALUES ('rogue', 'OpenAI rogue agent', '2026-07-08', 'OpenAI', NULL, 'Active')")
    for eid, title, org in (("r1", "OpenAI discloses more victims", "OpenAI"), ("r2", "OpenAI slows training", "OpenAI"),
                            ("r3", "Australia PM criticizes OpenAI over security breach", None)):
        conn.execute("INSERT INTO EnrichedEvents VALUES (?,?,?,?,NULL,NULL,?)", (eid, eid, title, "2026-07-08",
                                                                               json.dumps({"formal_entity_name": org})))
        conn.execute("INSERT INTO RawEvents VALUES (?, ?, ?, NULL)", (eid, f"https://news.example.com/{eid}", title))
        conn.execute("INSERT INTO EventDeduplicationMap VALUES (?,?,?)", ("m" + eid, eid, "rogue"))
    assert event_with(False) is None                                     # the victim vouches for them all
    event = event_with(True)
    assert event["suspect_ids"] == ["r3"] and event["unidentified_ids"] == ["r3"]
    resolver = co.EntityResolver(conn)
    # Judged and reported, but outside the calibrated suspects: the model alone does not split it ...
    out = co.decide_strays(event, _scores(event, {"r3": 0.02}), {}, 0.3, resolver)
    assert out["strays"] == [] and [h["id"] for h in out["held"]] == ["r3"]
    # ... a label does.
    out = co.decide_strays(event, _scores(event, {"r3": 0.02}), {"rogue|r3": "different"}, 0.3, resolver)
    assert [s["id"] for s in out["strays"]] == ["r3"] and out["strays"][0]["basis"] == "label"
