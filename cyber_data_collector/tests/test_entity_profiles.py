"""Entity profiles, the alias decision cache's freshness, and pair ranking. Offline: the search is a fake."""

from __future__ import annotations

import json
import sqlite3

import pytest

from cyber_data_collector.dedup import alias_learning as al
from cyber_data_collector.dedup import entity_alias_verification as eav
from cyber_data_collector.dedup import entity_profiles as ep


@pytest.fixture(autouse=True)
def _bogus_keys(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "bogus-not-a-real-key")


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT, entity_kind TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, title TEXT);
        CREATE TABLE EnrichedEventEntities (enriched_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, status TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
    """)
    return c


def _entity(conn, name, kind="government_body"):
    return conn.execute("INSERT INTO EntitiesV2 (entity_name, entity_kind) VALUES (?, ?)", (name, kind)).lastrowid


def _victim(conn, entity_id, title="Portal breach"):
    conn.execute("INSERT OR IGNORE INTO DeduplicatedEvents VALUES ('e1', 'Active')")
    conn.execute("INSERT INTO DeduplicatedEventEntities VALUES ('e1', ?, 'victim')", (entity_id,))
    conn.execute("INSERT OR IGNORE INTO EnrichedEvents VALUES ('r1', ?)", (title,))
    conn.execute("INSERT INTO EnrichedEventEntities VALUES ('r1', ?, 'victim')", (entity_id,))


ANSWER = {"text": json.dumps({"jurisdiction": "Australia", "sector": ["health"], "type": "government agency",
                              "administered_by": None, "runs": ["Medicare Statistics Reporting Service"],
                              "holds_data": ["health records"], "known_as": ["Medicare"], "website": "https://www.servicesaustralia.gov.au"}),
          "citations": ["https://www.servicesaustralia.gov.au/about"]}


def test_a_human_row_needs_a_reason_and_is_never_replaced_by_research(conn):
    sa = _entity(conn, "Services Australia")
    with pytest.raises(ValueError):
        ep.set_profile(conn, "Services Australia", "jurisdiction", "Australia", "")
    with pytest.raises(ValueError):
        ep.set_profile(conn, "Services Australia", "colour", "blue", "why")
    ep.set_profile(conn, "Services Australia", "jurisdiction", "Australia (Commonwealth)", "administers Medicare")
    ep.store_researched(conn, sa, {"jurisdiction": "Australia", "sector": ["health"]}, ["https://x"])
    profile = ep.profile_for(conn, "Services Australia")
    assert profile["jurisdiction"] == ["Australia (Commonwealth)"] and profile["sector"] == ["health"]
    ep.store_researched(conn, sa, {"sector": ["welfare"]}, [])
    assert ep.profile_for(conn, sa)["sector"] == ["welfare"] and ep.profile_for(conn, sa)["jurisdiction"]
    assert conn.execute("SELECT COUNT(*) FROM EntityProfiles WHERE source = 'human'").fetchone()[0] == 1


def test_research_is_asked_once_per_entity_until_its_evidence_changes(conn):
    sa = _entity(conn, "Services Australia")
    _victim(conn, sa)
    asked = []

    def search(prompt):
        asked.append(prompt)
        return ANSWER

    assert ep.pending(conn) == [(sa, "Services Australia")]
    assert ep.research_profile(conn, sa, "Services Australia", search=search) == 7
    assert ep.pending(conn) == [] and len(asked) == 1
    profile = ep.profile_for(conn, sa)
    assert profile["runs"] == ["Medicare Statistics Reporting Service"] and ep.domain_of(profile) == "servicesaustralia.gov.au"
    conn.execute("INSERT INTO EnrichedEvents VALUES ('r2', 'A different incident')")
    conn.execute("INSERT INTO EnrichedEventEntities VALUES ('r2', ?, 'victim')", (sa,))
    assert ep.pending(conn) == [(sa, "Services Australia")]            # new evidence: asked again
    # A failed lookup is retried, never stored as an empty profile.
    assert ep.research_profile(conn, sa, "Services Australia", search=lambda p: None) is None
    assert ep.pending(conn) == [(sa, "Services Australia")]


def test_a_service_the_organisation_runs_maps_the_product_to_it_without_folding(conn):
    sa = _entity(conn, "Services Australia")
    product = _entity(conn, "Medicare Statistics Reporting Service", "product")
    ep.store_researched(conn, sa, {"runs": ["Medicare Statistics Reporting Service"]}, [])
    conn.execute("INSERT INTO DeduplicatedEvents VALUES ('e1', 'Active')")
    conn.execute("INSERT INTO EnrichedEvents VALUES ('r1', 't')")
    conn.executescript("CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);"
                       "CREATE TABLE EntityAliases (raw_name TEXT, canonical_name TEXT, source TEXT);")
    conn.execute("INSERT INTO EventDeduplicationMap VALUES ('r1', 'e1')")
    conn.execute("INSERT INTO EnrichedEventEntities VALUES ('r1', ?, 'product')", (product,))
    assert al.product_hosts(conn, "e1") == [("Medicare Statistics Reporting Service", "Services Australia")]


def test_an_unsure_decision_is_asked_again_only_when_its_evidence_or_model_changed(conn, monkeypatch):
    a, b = _entity(conn, "Australian Medicare"), _entity(conn, "Medicare")
    conn.execute("INSERT INTO EnrichedEvents VALUES ('r1', 'Medicare portal breach')")
    conn.execute("INSERT INTO EnrichedEventEntities VALUES ('r1', ?, 'victim')", (a,))
    answers = iter(["unsure", "unsure", "same"])
    asked = []

    def search(prompt):
        asked.append(prompt)
        return {"text": json.dumps({"verdict": next(answers), "canonical_name": None, "reason": "r"}), "citations": []}

    def ask():
        report = eav.VerificationReport()
        return eav.verify_pair(conn, "Australian Medicare", "Medicare", search, eav.stored_verdicts(conn), report)

    assert ask() == "unsure" and len(asked) == 1
    assert ask() == "unsure" and len(asked) == 1                      # same evidence: the stored unsure stands
    ep.store_researched(conn, a, {"known_as": ["Medicare"]}, [])      # a profile appears: the evidence changed
    assert ask() == "unsure" and len(asked) == 2 and "profile:" in asked[1]
    assert ask() == "unsure" and len(asked) == 2
    monkeypatch.setenv("PERPLEXITY_MODEL", "another-model")           # another model: nothing is reused
    assert ask() == "same" and len(asked) == 3


def test_a_settled_verdict_survives_changed_evidence(conn):
    a, b = _entity(conn, "Acme Health"), _entity(conn, "Acme")
    asked = []

    def search(prompt):
        asked.append(prompt)
        return {"text": json.dumps({"verdict": "different", "reason": "r"}), "citations": []}

    for _ in range(2):
        eav.verify_pair(conn, "Acme", "Acme Health", search, eav.stored_verdicts(conn), eav.VerificationReport())
        ep.store_researched(conn, a, {"sector": ["health"]}, [])
    assert len(asked) == 1


def test_pairs_are_ranked_by_shared_distinctive_tokens_and_reliable_profile_evidence(conn):
    a, b, c = _entity(conn, "Northern Health Network"), _entity(conn, "Northern Health"), _entity(conn, "Southern Metals")
    ep.store_researched(conn, a, {"jurisdiction": "Australia", "website": "https://www.nhn.org.au"}, [])
    ep.store_researched(conn, b, {"jurisdiction": "Australia", "website": "https://nhn.org.au"}, [])
    ep.store_researched(conn, c, {"jurisdiction": "New Zealand"}, [])
    pairs = [("Southern Metals", "Northern Health Network"), ("Northern Health Network", "Northern Health")]
    assert al.rank(conn, pairs, lambda p: p)[0] == ("Northern Health Network", "Northern Health")
    assert ep.agreement(ep.profile_for(conn, a), ep.profile_for(conn, b)) == 3
    assert ep.agreement(ep.profile_for(conn, a), ep.profile_for(conn, c)) == -1
