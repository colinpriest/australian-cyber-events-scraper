"""Tests for victim-organisation selection.

The victim used to be copied from the master record's highest-confidence
entity. All entity links are tagged "affected" with no victim/regulator
distinction, so that attributed the 106-record Optus breach to the Australian
Cyber Security Centre, and left 30% of active events with no victim at all
despite the entities being present on their members.
"""
from __future__ import annotations

import pytest

from cyber_data_collector.dedup.victim_selection import (
    derive_victim,
    is_non_organisation,
    is_regulator,
    is_threat_actor,
    mention_share,
    should_replace,
    ubiquitous_entities,
)


# --------------------------------------------------------------------------
# Category tests
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "Australian Cyber Security Centre",
    "Office of the Australian Information Commissioner",
    "Federal Court",
    "Australian Federal Police",
])
def test_regulators_are_recognised(name):
    assert is_regulator(name)


@pytest.mark.parametrize("name", [
    "Optus Pty Limited", "Medibank Private Limited", "HWL Ebsworth Lawyers",
])
def test_real_organisations_are_not_regulators(name):
    assert not is_regulator(name)


@pytest.mark.parametrize("name", [
    "March 2023", "2024", "hms.com.au", "hospitals and clinics", "hackers",
    "Australians", "Australian universities", "Privacy Act 1988",
    "12.9 million individuals", "Australia",
])
def test_non_organisations_are_rejected(name):
    assert is_non_organisation(name)


@pytest.mark.parametrize("name", [
    "Oxfam Australia", "Deakin University", "Compumedics Limited",
    "Brydens Lawyers", "Finite Recruitment", "Health Management Systems",
])
def test_real_organisations_survive_filtering(name):
    """Regression: a Capitalised-Capitalised shape is not a person.

    Treating that shape as a personal name rejected all of these as invalid
    victims.
    """
    assert not is_non_organisation(name)


@pytest.mark.parametrize("name", [
    "Mailto ransomware", "Scattered Spider", "LockBit", "DragonForce",
])
def test_threat_actors_are_rejected(name):
    assert is_threat_actor(name)


def test_recorded_attackers_are_rejected():
    assert is_threat_actor("Some Crew", known_attackers={"Some Crew"})


# --------------------------------------------------------------------------
# Ubiquity
# --------------------------------------------------------------------------

def test_ubiquitous_names_are_detected():
    """A byline appears on every article; a victim appears on its own."""
    events = [{"David Hollingworth", f"Victim{i} Pty Ltd"} for i in range(40)]
    ubiquitous = ubiquitous_entities(events)
    assert "david hollingworth" in ubiquitous
    assert "victim1 pty ltd" not in ubiquitous


def test_dominant_victim_beats_the_ubiquity_penalty():
    """Regression: the largest victims are also corpus-common.

    Rejecting ubiquitous names outright handed the 106-record Optus breach to
    a bystander mentioned once.
    """
    counts = [("Optus", 100), ("Otto IT", 1)]
    result = derive_victim(counts, member_count=106, title="Optus cyber attack",
                           ubiquitous={"optus"})
    assert "Optus" in result


# --------------------------------------------------------------------------
# Derivation
# --------------------------------------------------------------------------

def test_victim_is_the_dominant_organisation():
    counts = [("Australian Cyber Security Centre", 7), ("Optus", 100)]
    assert derive_victim(counts, 106, title="Optus data breach") == "Optus"


def test_regulators_lose_to_the_organisation_they_are_investigating():
    counts = [("OAIC", 11), ("MediSecure", 73)]
    assert derive_victim(counts, 77, title="MediSecure breach") == "MediSecure"


def test_formal_spelling_is_preferred_among_variants():
    counts = [("Optus", 50), ("Singtel Optus Pty Limited", 20)]
    assert derive_victim(counts, 60) == "Singtel Optus Pty Limited"


def test_no_candidates_keeps_the_current_victim():
    assert derive_victim([], 3, current_victim="Acme Ltd") == "Acme Ltd"


# --------------------------------------------------------------------------
# Replacement policy
# --------------------------------------------------------------------------

def test_missing_victim_is_always_filled():
    replace, why = should_replace(None, "Acme Ltd", 0.0, 0.9)
    assert replace and "no victim" in why


def test_valid_victim_is_left_alone():
    """Frequency alone must not re-decide a victim that is already correct.

    Trialling that proposed "Evolution Mining Limited" -> "Reuters".
    """
    replace, _ = should_replace("Evolution Mining Limited", "Reuters", 0.5, 0.6)
    assert not replace


def test_bystander_regulator_is_replaced():
    """The ACSC receives breach reports; it is never the breached party."""
    replace, why = should_replace("Australian Cyber Security Centre",
                                  "Optus", 0.07, 0.94)
    assert replace
    assert "never the victim" in why or "regulator" in why


def test_report_receiving_bodies_are_never_the_victim():
    """The OAIC is named on nearly every incident because it was notified.

    That is the opposite of being breached, so no mention count can make it
    the victim.
    """
    from cyber_data_collector.dedup.victim_selection import is_never_victim

    for body in ("Office of the Australian Information Commissioner (OAIC)",
                 "OAIC", "ACSC", "Australian Cyber Security Centre",
                 "National Office of Cyber Security"):
        assert is_never_victim(body), body

    # Even dominating its own event must not make it the victim.
    counts = [("OAIC", 100), ("Acme Ltd", 2)]
    assert derive_victim(counts, 100, title="Acme breach") != "OAIC"

    replace, why = should_replace("OAIC", "Acme Ltd", 0.99, 0.02)
    assert replace and "never the victim" in why


def test_police_and_courts_can_be_victims():
    """They hold data and are genuinely breached; this database has one.

    Police were briefly added to the never-victim set, which mislabelled a real
    "AFP officer data leak".
    """
    from cyber_data_collector.dedup.victim_selection import is_never_victim

    assert not is_never_victim("Australian Federal Police")
    assert not is_never_victim("Federal Court")


def test_people_in_the_database_are_never_the_victim():
    """The victim is the organisation attacked, not who was in its database."""
    from cyber_data_collector.dedup.victim_selection import is_non_organisation

    for group in ("Australians", "students", "customers", "sheep farmers",
                  "Australian organisations", "law firm", "IT provider"):
        assert is_non_organisation(group), group


def test_real_organisations_ending_in_a_class_noun_survive():
    """Regression: rejecting names ending in "University"/"Company" destroyed
    correct victims - Western Sydney University became "IDCARE", The Plastic
    Bag Company became a journalist's name.
    """
    from cyber_data_collector.dedup.victim_selection import is_non_organisation

    for name in ("Western Sydney University", "The Plastic Bag Company",
                 "TMA Group of Companies", "Albright Institute of Languages",
                 "Internet Archive", "Queensland Department of Education"):
        assert not is_non_organisation(name), name


def test_breached_regulator_is_retained():
    """ASIC was compromised through Accellion; ASIC is still the victim."""
    replace, why = should_replace(
        "Australian Securities and Investments Commission", "Accellion", 0.8, 0.5)
    assert not replace
    assert "dominates" in why


def test_barely_mentioned_victim_is_replaced():
    replace, why = should_replace("Crown Princess Mary Cancer Centre",
                                  "MediSecure", 0.0, 1.0)
    assert replace and "mentioned by" in why


def test_threat_actor_recorded_as_victim_is_replaced():
    replace, why = should_replace("Mailto ransomware", "Toll Holdings Limited",
                                  0.3, 0.8)
    assert replace and "threat actor" in why


def test_mention_share_counts_variants():
    counts = [("Optus", 50), ("Singtel Optus Pty Limited", 10), ("OAIC", 5)]
    assert mention_share("Optus Pty Limited", counts, 60) > 0.9
    assert mention_share("OAIC", counts, 60) < 0.2
