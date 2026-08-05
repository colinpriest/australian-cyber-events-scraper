"""Structural entity matching.

Every "should match" case is one a user reported as a duplicate entity. Every
"should not" case is a merge an earlier version of this matcher actually made -
the negatives matter more than the positives here, because a wrong merge silently
fuses two organisations' incident histories.
"""

from __future__ import annotations

import pytest

from cyber_data_collector.dedup.entity_merge import (
    distinctive_core,
    initialism,
    is_acronym_of,
    preferred_name,
    same_organisation_by_structure,
)


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
