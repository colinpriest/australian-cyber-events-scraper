"""Acronyms are candidates, not proof of identity, when victims are counted.

"AP" is the initialism of both Advance Press and Associated Press, so the structural test that joins
"FRV" to "Fire Rescue Victoria" used to join it to either - collapsing victim labels that are different
organisations within an event and across the recurrence counts. An ambiguous acronym resolves only through
an established identifier (a confirmed global alias, or this event's own stored resolution); an acronym
that fits one organisation and conflicts with nothing keeps resolving without a model.

Also the supplier-incident rule: one incident, several co-equal victims, kept distinct.
"""

from __future__ import annotations

import sqlite3

import pytest

from cyber_data_collector.dedup.contextual_aliases import add_contextual_alias, ensure_schema as ensure_contextual
from cyber_data_collector.dedup.entity_resolution import EntityResolver
from cyber_data_collector.dedup.victim_counting import (
    AcronymPolicy,
    one_row_per_organisation,
    organisation_groups,
    same_organisation,
    same_within_event,
    with_acronym_policy,
    within_event_duplicates,
)


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    # A test that reaches for a model must fail, not make a paid call.
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    monkeypatch.setenv("PERPLEXITY_API_KEY", "not-a-real-key")


def _db(names, links, aliases=(), contextual=(), resolutions=()):
    """Entities (ids from 1), ``(event id, name, role)`` links, global aliases, contextual aliases and
    ``(event id, raw, canonical)`` stored resolutions."""
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY AUTOINCREMENT, entity_name TEXT UNIQUE);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, status TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        CREATE TABLE EntityAliases (raw_name TEXT UNIQUE, canonical_name TEXT, canonical_key TEXT,
                                    source TEXT, confidence REAL);
    """)
    ensure_contextual(conn)
    conn.executemany("INSERT INTO EntitiesV2 (entity_name) VALUES (?)", [(n,) for n in names])
    ids = {n: i for i, n in conn.execute("SELECT entity_id, entity_name FROM EntitiesV2")}
    for event in sorted({e for e, _n, _r in links}):
        conn.execute("INSERT INTO DeduplicatedEvents VALUES (?, 'Active')", (event,))
    conn.executemany("INSERT INTO DeduplicatedEventEntities VALUES (?, ?, ?)",
                     [(e, ids[n], role) for e, n, role in links])
    conn.executemany("INSERT INTO EntityAliases VALUES (?, ?, '', ?, 1.0)", aliases)
    for raw, canonical in contextual:
        add_contextual_alias(conn, raw, canonical, "llm")
    conn.executemany("INSERT INTO EntityMentionResolutions VALUES ('DeduplicatedEventEntities', ?, ?, ?, 'jev', 0.95, "
                     "NULL, '2026-10-29')", resolutions)
    conn.commit()
    return conn, ids


def _victims(conn):
    return [(i, n, e) for i, n, e in conn.execute(
        "SELECT dee.entity_id, v.entity_name, dee.deduplicated_event_id FROM DeduplicatedEventEntities dee "
        "JOIN EntitiesV2 v ON v.entity_id = dee.entity_id WHERE dee.relationship_type = 'victim'")]


PRESSES = ["Advance Press", "Associated Press", "AP"]


def test_an_ambiguous_acronym_is_not_the_same_organisation_as_either_name():
    conn, _ = _db(PRESSES, [("e1", "AP", "victim")], contextual=[("AP", "Advance Press")])
    resolver = with_acronym_policy(conn, EntityResolver(conn))
    assert not same_organisation(resolver, "AP", "Advance Press")
    assert not same_organisation(resolver, "AP", "Associated Press")
    assert not same_organisation(resolver, "Associated Press", "AP")
    # ... though the two names it fits are different organisations from each other too.
    assert not same_organisation(resolver, "Advance Press", "Associated Press")


def test_ambiguity_does_not_depend_on_a_contextual_alias_being_recorded():
    conn, _ = _db(["University of Canberra", "University of California", "UC"], [("e1", "UC", "victim")])
    resolver = with_acronym_policy(conn, EntityResolver(conn))
    assert AcronymPolicy.from_connection(conn).organisations_fitting("UC") == [
        "University of California", "University of Canberra"]
    assert not same_organisation(resolver, "UC", "University of Canberra")
    assert not same_organisation(resolver, "UC", "University of California")


def test_victims_of_one_event_that_share_an_acronym_stay_distinct():
    conn, ids = _db(PRESSES, [("e1", "AP", "victim"), ("e1", "Associated Press", "victim"),
                              ("e2", "AP", "victim"), ("e2", "Advance Press", "victim")],
                    contextual=[("AP", "Advance Press")])
    assert within_event_duplicates(conn, _victims(conn), EntityResolver()) == set()
    # Across events: three organisations, not two.
    groups = organisation_groups(conn, ids.values())
    assert len(set(groups.values())) == 3 and groups[ids["AP"]] == ids["AP"]
    rows = one_row_per_organisation([(ids["AP"], "e1"), (ids["Associated Press"], "e1")], groups)
    assert len(rows) == 2


def test_an_event_that_resolved_the_acronym_counts_the_organisation_once():
    conn, _ = _db(PRESSES, [("e1", "AP", "victim"), ("e1", "Advance Press", "victim"),
                            ("e2", "AP", "victim"), ("e2", "Advance Press", "victim")],
                  contextual=[("AP", "Advance Press")], resolutions=[("e1", "AP", "Advance Press")])
    dropped = within_event_duplicates(conn, _victims(conn))
    assert {event for _eid, event in dropped} == {"e1"}          # e1's own context resolved it; e2's did not
    (eid, event), = dropped
    assert conn.execute("SELECT entity_name FROM EntitiesV2 WHERE entity_id = ?", (eid,)).fetchone()[0] in (
        "AP", "Advance Press")


def test_an_unresolved_mention_is_not_evidence():
    conn, _ = _db(PRESSES, [("e1", "AP", "victim"), ("e1", "Advance Press", "victim")],
                  contextual=[("AP", "Advance Press")])
    conn.execute("INSERT INTO EntityMentionResolutions VALUES ('DeduplicatedEventEntities', 'e1', 'AP', NULL, "
                 "'unresolved', NULL, NULL, '2026-10-29')")
    assert within_event_duplicates(conn, _victims(conn)) == set()


def test_a_confirmed_global_alias_resolves_the_acronym_without_a_model():
    conn, ids = _db(["Fire Rescue Victoria", "FRV", "Frontier Resources Ventures"],
                    [("e1", "FRV", "victim"), ("e1", "Fire Rescue Victoria", "victim")],
                    aliases=[("FRV", "Fire Rescue Victoria", "llm")])
    # FRV also fits Frontier Resources Ventures by initials, yet the confirmed alias decides.
    assert within_event_duplicates(conn, _victims(conn)) == {(ids["FRV"], "e1")} or \
        within_event_duplicates(conn, _victims(conn)) == {(ids["Fire Rescue Victoria"], "e1")}
    groups = organisation_groups(conn, ids.values())
    assert groups[ids["FRV"]] == groups[ids["Fire Rescue Victoria"]] != groups[ids["Frontier Resources Ventures"]]


def test_an_acronym_that_fits_one_organisation_and_conflicts_with_nothing_still_resolves():
    conn, ids = _db(["HWL Ebsworth", "HWLE"], [("e1", "HWLE", "victim"), ("e1", "HWL Ebsworth", "victim")])
    assert len(within_event_duplicates(conn, _victims(conn))) == 1


def test_reviewed_aliases_hold_for_acronyms_that_fit_several_names():
    # ANU is in EXPLICIT_ALIASES; "Australian Nursing Union" is another organisation ANU fits by initials.
    conn, ids = _db(["Australian National University", "Australian Nursing Union", "ANU"],
                    [("e1", "ANU", "victim"), ("e1", "Australian National University", "victim")])
    resolver = with_acronym_policy(conn, EntityResolver(conn))
    assert same_organisation(resolver, "ANU", "Australian National University")
    assert not same_organisation(resolver, "ANU", "Australian Nursing Union")


def test_a_resolver_without_a_policy_never_treats_an_acronym_as_identity():
    assert not same_organisation(EntityResolver(), "FRV", "Fire Rescue Victoria")
    assert not same_within_event(EntityResolver(), "FRV", "Fire Rescue Victoria")


# ----------------------------------------------------------------------
# Supplier incidents: one incident, several co-equal victims
# ----------------------------------------------------------------------

UNIVERSITIES = ["Monash University", "University of Sydney", "University of Technology Sydney",
                "Western Sydney University", "University of Western Australia", "The University of Sydney",
                "University of Canberra", "University of California", "UC", "ProctorU"]


def _supplier_db():
    return _db(UNIVERSITIES, [
        ("p1", "ProctorU", "vendor"),
        ("p1", "Monash University", "victim"), ("p1", "University of Sydney", "victim"),
        ("p1", "University of Technology Sydney", "victim"), ("p1", "Western Sydney University", "victim"),
        ("p1", "University of Western Australia", "victim"), ("p1", "UC", "victim"),
        ("p1", "University of Canberra", "victim"), ("p1", "University of California", "victim"),
        # A second report of the same incident, naming two of the customers differently.
        ("p2", "Monash University", "victim"), ("p2", "The University of Sydney", "victim"),
    ])


def test_co_equal_supplier_victims_stay_distinct_within_the_event():
    conn, _ = _supplier_db()
    victims = [v for v in _victims(conn) if v[2] == "p1"]
    assert len(victims) == 8
    assert within_event_duplicates(conn, victims, EntityResolver()) == set()


def test_reports_of_the_same_supplier_incident_link_by_organisation_not_by_word():
    conn, ids = _supplier_db()
    victims = [ids[n] for n in UNIVERSITIES if n != "ProctorU"]
    groups = organisation_groups(conn, victims)
    # The same customer under two spellings is one organisation ...
    assert groups[ids["University of Sydney"]] == groups[ids["The University of Sydney"]]
    # ... and every other customer is its own: none merges through "university", "sydney" or an acronym.
    others = [n for n in UNIVERSITIES if n not in ("ProctorU", "The University of Sydney")]
    assert len({groups[ids[n]] for n in others}) == len(others)
    # Counting: the incident appears once per organisation, p1 and p2 both listing Monash and Sydney.
    rows = one_row_per_organisation([(i, e) for i, _n, e in _victims(conn)
                                     if i in groups and _n != "ProctorU"], groups)
    assert sum(1 for _o, e in rows if e == "p1") == 8 and sum(1 for _o, e in rows if e == "p2") == 2
