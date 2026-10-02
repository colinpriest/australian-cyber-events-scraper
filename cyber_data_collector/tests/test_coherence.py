"""Tests for stray-record detection (event coherence)."""
from __future__ import annotations

import json
import sqlite3

import pytest

from cyber_data_collector.dedup import coherence as co


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
        CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_url TEXT);
    """)
    def event(did, title, victim, members, vendor=None, date="2026-08-23"):
        c.execute("INSERT INTO DeduplicatedEvents VALUES (?,?,?,?,?, 'Active')", (did, title, date, victim, vendor))
        for eid, t, org in members:
            c.execute("INSERT INTO EnrichedEvents VALUES (?,?,?,?,NULL,NULL,?)", (eid, eid, t, date, _pe(org)))
            c.execute("INSERT INTO RawEvents VALUES (?, ?)", (eid, f"https://news.example.com/{eid}"))
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


def test_suspects_are_members_naming_another_organisation(conn):
    found = {e["id"]: e["suspect_ids"] for e in co.find_suspect_events(conn)}
    assert found["sharp"] == ["x1"]
    assert "clean" not in found          # Optus / Singtel Optus share the 'optus' key
    assert "canvas" in found             # a customer record is a suspect; the model decides


class _Fake:
    def __init__(self, belongs):
        self.belongs = belongs
        self.chat = self
        self.completions = self

    def create(self, messages, **kwargs):
        n = messages[-1]["content"].count("\n[")
        return co.CoherenceVerdict(event_incident="x", members=[
            co.MemberJudgement(index=i, belongs=self.belongs(i), certainty=0.95,
                               actual_incident="" if self.belongs(i) else "other", reasoning="r")
            for i in range(1, n + 1)])


def test_only_confident_non_members_are_strays_and_never_all(conn):
    event = next(e for e in co.find_suspect_events(conn) if e["id"] == "sharp")
    verdict = co.judge_event(event, client=_Fake(lambda i: i != 3))
    assert [s["id"] for s in co.strays(event, verdict, 0.9)] == ["x1"]
    # Same-organisation members never leave, however the model rules.
    same_org = co.judge_event(event, client=_Fake(lambda i: i == 3))   # says s1, s2 foreign
    assert co.strays(event, same_org, 0.9) == []
    assert co.strays(event, verdict, 0.99) == []                       # below the bar
    everything = co.judge_event(event, client=_Fake(lambda i: False))
    # Even if the model rejects every record, only the suspect can leave.
    assert [s["id"] for s in co.strays(event, everything, 0.9)] == ["x1"]


def test_verdict_not_covering_every_record_is_ignored(conn):
    event = next(e for e in co.find_suspect_events(conn) if e["id"] == "sharp")

    class Short(_Fake):
        def create(self, messages, **kwargs):
            return co.CoherenceVerdict(event_incident="x", members=[
                co.MemberJudgement(index=1, belongs=True, certainty=0.9, actual_incident="", reasoning="r")])
    assert co.judge_event(event, client=Short(None)) is None


def test_home_is_the_single_same_organisation_event_within_the_window(conn):
    resolver = co.EntityResolver(conn)
    resolver.fit(["Mathspace Pty Ltd", "Sharp Motor Group", "Optus", "Instructure"])
    assert co.find_home(conn, resolver, "Mathspace Pty Ltd", "2026-08-12", exclude=["sharp"]) == "math"
    assert co.find_home(conn, resolver, "Mathspace Pty Ltd", "2024-01-01", exclude=["sharp"]) is None
    assert co.find_home(conn, resolver, None, "2026-08-12", exclude=[]) is None


def test_mislabelled_event_is_not_gutted():
    """youX case: most members 'foreign' to the label, all naming one incident."""
    members = [{"id": f"y{i}", "org": "youX Pty Ltd", "title": "youX breach", "date": None, "body": "", "url": None}
               for i in range(4)] + [{"id": "v1", "org": "Vroom", "title": "Vroom", "date": None, "body": "", "url": None}]
    event = {"id": "e", "members": members, "suspect_ids": [f"y{i}" for i in range(4)]}
    verdict = co.CoherenceVerdict(event_incident="Vroom by YouX", members=[
        co.MemberJudgement(index=i, belongs=(i == 5), certainty=1.0,
                           actual_incident="" if i == 5 else "youX Data Breach", reasoning="r")
        for i in range(1, 6)])
    assert co.strays(event, verdict, 0.95) == []


def test_member_whose_true_home_is_the_event_itself_stays():
    resolver = co.EntityResolver()
    resolver.fit(["youX Pty Ltd", "Vroom", "Mathspace", "Sharp Motor Group"])
    members = [{"id": "a", "org": "Vroom", "title": "Vroom by YouX breach", "date": None, "body": "", "url": None},
               {"id": "b", "org": "Vroom", "title": "Vroom breach", "date": None, "body": "", "url": None},
               {"id": "y", "org": "youX Pty Ltd", "title": "youX breach", "date": None, "body": "", "url": None}]
    event = {"id": "e", "title": "Vroom by YouX Data Breach", "members": members, "suspect_ids": ["y"]}
    verdict = co.CoherenceVerdict(event_incident="Vroom", members=[
        co.MemberJudgement(index=1, belongs=True, certainty=1.0, actual_incident="", reasoning="r"),
        co.MemberJudgement(index=2, belongs=True, certainty=1.0, actual_incident="", reasoning="r"),
        co.MemberJudgement(index=3, belongs=False, certainty=1.0, actual_incident="youX Data Breach", reasoning="r")])
    assert co.strays(event, verdict, 0.99, resolver=resolver) == []
