"""Two victims of one event that may be one organisation: proposed, asked with the incident's reporting, stored
against the question, and honoured by victim counting for that event only."""

from __future__ import annotations

import json
import sqlite3

import pytest

from cyber_data_collector.dedup import covictim_coreference as cc
from cyber_data_collector.dedup import entity_alias_verification as eav
from cyber_data_collector.dedup.victim_counting import within_event_duplicates

SCHEMA = """
CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT, entity_kind TEXT);
CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, title TEXT, victim_organization_name TEXT,
    status TEXT);
CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title TEXT, description TEXT);
CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, raw_content TEXT);
CREATE TABLE EntityAliases (raw_name TEXT, canonical_name TEXT, source TEXT);
"""


def _event(c, did, title, victims, records):
    c.execute("INSERT INTO DeduplicatedEvents VALUES (?, ?, NULL, 'Active')", (did, title))
    for eid, name in victims:
        c.execute("INSERT OR IGNORE INTO EntitiesV2 VALUES (?, ?, 'organisation')", (eid, name))
        c.execute("INSERT INTO DeduplicatedEventEntities VALUES (?, ?, 'victim')", (did, eid))
    for rid, rtitle, desc in records:
        c.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?)", (rid, did))
        c.execute("INSERT INTO EnrichedEvents VALUES (?, ?, ?, ?)", (rid, rid, rtitle, desc))
        c.execute("INSERT INTO RawEvents VALUES (?, NULL)", (rid,))


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    eav.ensure_schema(c)
    _event(c, "e1", "University of Wollongong data breach", [(1, "UOW"), (2, "University of Wollongong")],
           [("r1", "UOW confirms data breach", "The University of Wollongong (UOW) said staff data was taken.")])
    _event(c, "e2", "Perth university hacked", [(3, "Perth uni"), (4, "University of Notre Dame Australia")],
           [("r2", "Perth uni hit by hackers", "A Perth uni, the University of Notre Dame Australia, was hacked.")])
    _event(c, "e3", "Card dump names two banks", [(5, "Westpac"), (6, "ANZ")],
           [("r3", "Card dump", "Westpac and ANZ cards were listed for sale.")])
    # An event of many customers: only names sharing a distinctive word or abbreviating one another are proposed.
    _event(c, "e4", "Canvas breach", [(7, "Curtin University"), (8, "University of Sydney"),
                                      (9, "University of Melbourne"), (10, "Sydney University"), (11, "Deakin University")],
           [("r4", "Canvas", "Curtin, Sydney University, Melbourne and Deakin were affected.")])
    return c


class Search:
    """A stand-in for Perplexity: the verdict for each pair, and the prompts it was sent."""

    def __init__(self, verdicts):
        self.verdicts, self.prompts = verdicts, []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        for (a, b), verdict in self.verdicts.items():
            if f'A: "{a}"' in prompt and f'B: "{b}"' in prompt:
                return {"text": json.dumps({"verdict": verdict, "canonical_name": None, "reason": "test"}),
                        "citations": ["https://example.org"]}
        return {"text": json.dumps({"verdict": "different", "canonical_name": None, "reason": "test"}),
                "citations": []}


def _rows(c):
    return [(int(r[0]), r[1], r[2]) for r in c.execute(
        "SELECT d.entity_id, v.entity_name, d.deduplicated_event_id FROM DeduplicatedEventEntities d "
        "JOIN EntitiesV2 v USING (entity_id) WHERE d.relationship_type = 'victim'")]


def test_proposals_every_pair_on_small_events_and_name_fits_on_large(conn):
    pairs = {(d, a, b) for d, a, b in cc.proposals(conn)}
    assert ("e1", "UOW", "University of Wollongong") in pairs
    assert ("e2", "Perth uni", "University of Notre Dame Australia") in pairs
    assert ("e3", "ANZ", "Westpac") in pairs
    large = {(a, b) for d, a, b in pairs if d == "e4"}
    assert ("Sydney University", "University of Sydney") in large
    assert ("Curtin University", "Deakin University") not in large


def test_same_verdict_counts_the_organisation_once_on_that_event_only(conn):
    search = Search({("UOW", "University of Wollongong"): "same",
                     ("Perth uni", "University of Notre Dame Australia"): "same"})
    report = cc.verify(conn, search=search)
    assert not report.failed and report.asked == len(cc.proposals(conn))
    drop = within_event_duplicates(conn, _rows(conn))
    assert {did for _, did in drop} >= {"e1", "e2"}
    assert not any(did == "e3" for _, did in drop)
    # Nothing global: no alias is written.
    assert conn.execute("SELECT COUNT(*) FROM EntityAliases").fetchone()[0] == 0
    # The question shows the incident's reporting.
    asked = next(p for p in search.prompts if 'A: "Perth uni"' in p)
    assert "Notre Dame Australia, was hacked" in asked and "Perth university hacked" in asked


def test_an_unchanged_question_is_not_asked_again_and_a_changed_one_is(conn):
    cc.verify(conn, search=Search({("UOW", "University of Wollongong"): "same"}))
    again = Search({})
    report = cc.verify(conn, search=again)
    assert report.asked == 0 and report.reused == len(cc.proposals(conn))
    conn.execute("UPDATE EnrichedEvents SET description = 'UOW: names, tax file numbers.' WHERE enriched_event_id = 'r1'")
    # The stored 'same' no longer counts on the changed reporting ...
    assert "e1" not in cc.same_pairs(conn)
    # ... and that pair alone is asked again.
    report = cc.verify(conn, search=again)
    assert report.asked == 1


def test_a_person_decides_without_a_question(conn):
    conn.execute("INSERT INTO EntityAliasDecisions (name, other_name, verdict, model, prompt_version, decided_at) "
                 "VALUES ('ANZ', 'Westpac', 'different', 'human', ?, '2026-11-05')", (eav.PROMPT_VERSION,))
    conn.execute("INSERT INTO EntityAliases VALUES ('Sydney University', 'University of Sydney', 'human')")
    search = Search({})
    report = cc.verify(conn, search=search)
    assert report.by_person == 2
    assert not any('A: "ANZ"' in p and 'B: "Westpac"' in p for p in search.prompts)
    assert frozenset(("Sydney University", "University of Sydney")) in cc.same_pairs(conn)["e4"]


def test_dry_run_asks_and_stores_nothing(conn):
    search = Search({})
    report = cc.verify(conn, search=search, dry_run=True)
    assert report.asked == len(cc.proposals(conn)) and not search.prompts
    assert conn.execute("SELECT COUNT(*) FROM EventVictimCoreference").fetchone()[0] == 0


def test_a_failed_question_is_left_undecided(conn):
    report = cc.verify(conn, search=lambda prompt: None)
    assert len(report.failed) == len(cc.proposals(conn))
    assert cc.same_pairs(conn) == {}
