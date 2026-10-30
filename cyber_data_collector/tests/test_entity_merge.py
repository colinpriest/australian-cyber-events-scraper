"""Structural entity matching.

Every "should match" case is one a user reported as a duplicate entity. Every
"should not" case is a merge an earlier version of this matcher actually made -
the negatives matter more than the positives here, because a wrong merge silently
fuses two organisations' incident histories.
"""

from __future__ import annotations

import pytest

from cyber_data_collector.dedup.entity_merge import (
    canonical_groups,
    distinctive_core,
    initialism,
    is_acronym_of,
    lines_of_business_conflict,
    preferred_name,
    same_organisation_by_structure,
)


@pytest.mark.parametrize("left,right", [
    # A shared brand word across different lines of business.
    ("Sharp Motor Group", "Sharp Office Systems Pty Ltd"),
    ("Apex Bank", "Apex Airlines"),
])
def test_line_of_business_conflict_blocks_merge(left, right):
    assert lines_of_business_conflict(left, right)
    assert not same_organisation_by_structure(left, right)


def test_line_of_business_conflict_cannot_chain_through_a_bare_name():
    """'Sharp Office' matches both sides; the union must still keep them apart."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT, entity_kind TEXT);
        CREATE TABLE EnrichedEventEntities (enriched_event_id TEXT, entity_id INTEGER);
    """)
    conn.executemany("INSERT INTO EntitiesV2 (entity_name, entity_kind) VALUES (?, 'organisation')",
                     [("Sharp Motor Group",), ("Sharp Office",), ("Sharp Office Systems",)])
    for keep, drop in canonical_groups(conn):
        group = {keep, *drop}
        assert not {"Sharp Motor Group", "Sharp Office Systems"} <= group


@pytest.mark.parametrize("left,right", [
    # containment: a fuller trading name
    ("Genea", "Genea IVF Clinic"),
    ("Epworth", "Epworth HealthCare"),
    ("HWL Ebsworth", "HWL Ebsworth Lawyers"),
    ("Latitude", "Latitude Financial Services"),
    ("Medlab", "Medlab Pathology"),
    ("Suncorp", "Suncorp Bank"),
    ("ReadyTech", "ReadyTech Holdings Limited"),
    ("Nitro", "Nitro PDF"),
    ("Origin", "Origin Energy"),
    ("Qantas", "Qantas Airways"),
    ("PwC", "PwC Australia"),
    ("Toll", "Toll Group"),
    ("Toyota", "Toyota Motor Corporation (TMC)"),
    ("Victoria Racing", "Victoria Racing Club"),
    ("The Fullerton Hotel Sydney", "The Fullerton Hotels and Resorts"),
    # both sides qualified differently
    ("Epworth Hospital", "Epworth HealthCare"),
    ("Genea Fertility", "Genea IVF"),
    ("Pound Road Medical Center", "Pound Road Medical Centre"),
    # acronyms
    ("HWLE", "HWL Ebsworth"),
    ("JLR", "Jaguar Land Rover"),
    ("FRV", "Fire Rescue Victoria"),
    ("NAB", "National Australia Bank"),
    ("QUT", "Queensland University of Technology"),
    ("UNSW", "University of New South Wales"),
    ("UWA", "University of Western Australia"),
    # spacing, slashes, sub-units
    ("Manage My Health", "ManageMyHealth"),
    ("Dodo", "Dodo/iPrimus"),
    ("UNSW School of Physics", "UNSW"),
    # state abbreviation expansion
    ("NSW Government", "New South Wales government"),
])
def test_same_organisation(left, right):
    assert same_organisation_by_structure(left, right)
    assert same_organisation_by_structure(right, left), "must be symmetric"


@pytest.mark.parametrize("left,right", [
    # A jurisdiction word names a different legal body.
    ("Amnesty International Australia", "Amnesty International Canada"),
    ("New Zealand Government", "Australian Government"),
    # Shared place name is not shared identity. This exact case chained 60
    # unrelated bodies into "Australian Government".
    ("Queensland Health", "Bank of Queensland"),
    ("Queensland Health", "Queensland University of Technology"),
    ("Queensland Government", "Australian Government"),
    ("Tasmanian Government", "Queensland Government"),
    # Shared category word is not shared identity.
    ("Bank of Queensland", "Bank of Melbourne"),
    ("University of Sydney", "University of Melbourne"),
    ("University of Sydney", "Western Sydney University"),
    ("NSW Health", "Queensland Health"),
    # Portfolio name shared by every jurisdiction's department.
    ("Department of Education", "NSW Department of Education"),
    ("Victorian Department of Education", "Queensland Department of Education"),
    # A prefix match on arbitrary words merged these.
    ("David", "David Jones"),
    ("Police", "Police Bank"),
    ("Court", "Court Services Victoria"),
    ("Ransomware", "INC Ransomware Group"),
    ("Aussie", "Aussie poultry processor"),
    ("ABC", "ABC Gippsland"),
    # Unrelated.
    ("Optus", "Telstra"),
    ("NAB", "Jaguar Land Rover"),
    ("Eastern Health", "Epworth HealthCare"),
    ("UNSW", "University of Newcastle"),
])
def test_different_organisations(left, right):
    assert not same_organisation_by_structure(left, right)
    assert not same_organisation_by_structure(right, left), "must be symmetric"


def test_place_and_category_words_are_not_identity():
    """A name made only of place and category words identifies nobody."""
    assert distinctive_core("Queensland Health") == frozenset()
    assert distinctive_core("Australian Government") == frozenset()
    assert distinctive_core("Genea IVF Clinic") == frozenset({"genea"})


def test_initialism_skips_joining_words():
    assert initialism("Queensland University of Technology") == "qut"
    assert is_acronym_of("QUT", "Queensland University of Technology")
    assert not is_acronym_of("QUT", "Queensland Urban Utilities Board Ltd")


def test_preferred_name_avoids_shouting_and_whispering():
    usage = {"Orica Australia Pty. Ltd.": 3, "ORICA AUSTRALIA PTY. LTD.": 9}
    assert preferred_name(list(usage), usage) == "Orica Australia Pty. Ltd."


def test_preferred_name_keeps_genuine_acronyms():
    """"ANZ" is not shouting; it is the organisation's name."""
    usage = {"ANZ": 12, "anz": 1}
    assert preferred_name(list(usage), usage) == "ANZ"


# ---------------------------------------------------------------------------
# 2026-10-28: proposals are verified, lists and shared acronyms do not chain
# ---------------------------------------------------------------------------

def _entities(names):
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT, entity_kind TEXT);
        CREATE TABLE EnrichedEventEntities (enriched_event_id TEXT, entity_id INTEGER);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT, title TEXT);
    """)
    conn.executemany("INSERT INTO EntitiesV2 (entity_name, entity_kind) VALUES (?, 'organisation')",
                     [(n,) for n in names])
    return conn


def test_a_shared_acronym_does_not_chain_different_organisations():
    conn = _entities(["Mackay Sugar", "M&S", "Marks & Spencer", "Media Super"])
    for keep, drop in canonical_groups(conn):
        assert not {"Mackay Sugar", "Marks & Spencer"} <= {keep, *drop}
        assert not {"Mackay Sugar", "Media Super"} <= {keep, *drop}


def test_a_list_of_organisations_is_not_folded_into_one_of_them():
    from cyber_data_collector.dedup.entity_merge import is_organisation_list

    banks = ("Australia and New Zealand Banking Group Limited, National Australia Bank Limited, "
             "Westpac Banking Corporation, Commonwealth Bank of Australia")
    assert is_organisation_list(banks) and not is_organisation_list("Smith, Jones & Co")
    conn = _entities(["Westpac", "Westpac Banking Corporation", banks])
    assert all(banks not in {keep, *drop} for keep, drop in canonical_groups(conn))


def test_explicit_only_applies_reviewed_aliases_and_nothing_structural():
    conn = _entities(["Services Australia", "Medicare", "Genea", "Genea IVF Clinic"])
    assert canonical_groups(conn, explicit_only=True) == [("Services Australia", ["Medicare"])]


def test_proposed_folds_apply_only_when_perplexity_confirms_and_verdicts_are_reused():
    import json

    from cyber_data_collector.dedup import entity_alias_verification as eav

    conn = _entities(["NAB", "National Australia Bank", "Mackay Sugar", "M&S"])
    asked = []

    def search(prompt):
        asked.append(prompt)
        same = 'A: "NAB"' in prompt and 'B: "National Australia Bank"' in prompt   # names sorted
        verdict = {"verdict": "same" if same else "different", "canonical_name": None, "reason": "r"}
        return {"text": "Answer: " + json.dumps(verdict), "citations": ["https://example.org"]}

    proposed = [("National Australia Bank", ["NAB"]), ("Mackay Sugar", ["M&S"])]
    report = eav.VerificationReport()
    assert eav.verified_groups(conn, proposed, search=search, report=report) == [("National Australia Bank", ["NAB"])]
    assert report.rejected == [("M&S", "Mackay Sugar", "different")] and len(asked) == 2
    # Stored: asked once, ever.
    again = eav.VerificationReport()
    eav.verified_groups(conn, proposed, search=search, report=again)
    assert len(asked) == 2 and again.reused == 2


def test_an_unreadable_answer_leaves_the_pair_undecided_and_unfolded():
    from cyber_data_collector.dedup import entity_alias_verification as eav

    conn = _entities(["NAB", "National Australia Bank"])
    report = eav.VerificationReport()
    groups = eav.verified_groups(conn, [("National Australia Bank", ["NAB"])],
                                 search=lambda prompt: {"text": "I think so", "citations": []}, report=report)
    assert groups == [] and report.failed == [("NAB", "National Australia Bank")]
    assert eav.parse_verdict('{"verdict": "maybe"}') is None


def _alias_db(aliases):
    conn = _entities(["National Australia Bank", "Services Australia"])
    conn.execute("CREATE TABLE EntityAliases (raw_name TEXT UNIQUE, canonical_name TEXT, canonical_key TEXT, "
                 "source TEXT, confidence REAL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.executemany("INSERT INTO EntityAliases (raw_name, canonical_name, canonical_key, source, confidence) "
                     "VALUES (?, ?, '', ?, 1.0)", aliases)
    return conn


def test_stored_aliases_are_looked_up_before_an_entity_is_found_or_created():
    from cyber_data_collector.dedup.entity_resolution import canonical_entity_name

    conn = _alias_db([("NAB", "National Australia Bank", "llm"), ("Medicare", "Services Australia", "human")])
    assert canonical_entity_name(conn, "NAB") == "National Australia Bank"
    assert canonical_entity_name(conn, "medicare") == "Services Australia"          # case-insensitive
    assert canonical_entity_name(conn, "Westpac") == "Westpac"                      # unknown: unchanged


def test_trusted_stored_aliases_fold_without_asking():
    conn = _alias_db([("NAB", "National Australia Bank", "llm")])
    conn.execute("INSERT INTO EntitiesV2 (entity_name, entity_kind) VALUES ('NAB', 'organisation')")
    assert ("National Australia Bank", ["NAB"]) in canonical_groups(conn, explicit_only=True)


def test_rule_made_aliases_are_verified_once_and_rejected_ones_removed():
    import json

    from cyber_data_collector.dedup import entity_alias_verification as eav

    conn = _alias_db([("Centre for Digital Business", "Transport for NSW", "rule"),
                      ("NAB", "National Australia Bank", "rule"),
                      ("ORICA AUSTRALIA PTY. LTD.", "Orica Australia Pty. Ltd.", "rule")])
    asked = []

    def search(prompt):
        asked.append(prompt)
        verdict = "same" if 'A: "NAB"' in prompt else "different"
        return {"text": json.dumps({"verdict": verdict, "canonical_name": None, "reason": "r"}), "citations": []}

    out = eav.verify_stored_aliases(conn, search=search)
    assert out.spelling_only == 1 and len(asked) == 2                               # the spelling variant not asked
    assert out.confirmed == [("NAB", "National Australia Bank")]
    assert out.removed == [("Centre for Digital Business", "Transport for NSW", "different")]
    assert dict(conn.execute("SELECT raw_name, source FROM EntityAliases").fetchall()) == {
        "NAB": "llm", "ORICA AUSTRALIA PTY. LTD.": "rule"}
    eav.verify_stored_aliases(conn, search=search)
    assert len(asked) == 2                                                          # verdicts stored
