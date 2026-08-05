"""Tests for entity kind/role classification.

Every entity link in the database carries relationship_type='affected', so the
column conveys nothing and the victim had to be guessed from mention counts.
That cannot separate the breached company from the software it was breached
through - the global Canvas incident was attributed to "Canvas Learning
Management System", a product.

No network: the classifier is driven with an injected fake client.
"""
from __future__ import annotations

import sqlite3

import pytest

from cyber_data_collector.dedup.entity_classification import (
    stored_victim_is_invalid,
    EntityAssignment,
    EntityKind,
    EntityRole,
    EntityRoleClassifier,
    EventEntityRoles,
    rule_kind,
)


class _FakeClient:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = 0
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls += 1
                outer.last_kwargs = kwargs
                if outer._error:
                    raise outer._error
                return outer._response

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def _a(name, kind, role, confidence=0.9):
    return EntityAssignment(name=name, kind=kind, role=role, confidence=confidence)


# --------------------------------------------------------------------------
# Rule-based kinds
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name, kind", [
    ("Canvas Learning Management System", EntityKind.PRODUCT),
    ("Microsoft SharePoint", EntityKind.PRODUCT),
    ("Australian Cyber Security Centre", EntityKind.GOVERNMENT_BODY),
    ("ShinyHunters", EntityKind.THREAT_ACTOR),
    ("hospitals and clinics", EntityKind.OTHER),
    ("March 2023", EntityKind.OTHER),
])
def test_rule_kind_decides_the_clear_cases(name, kind):
    assert rule_kind(name) == kind


def test_rule_kind_defers_on_ordinary_organisations():
    """Ambiguous names are left for the model, which can see the event."""
    assert rule_kind("Instructure") is None
    assert rule_kind("University of Sydney") is None


# --------------------------------------------------------------------------
# Extracting victim and vendor
# --------------------------------------------------------------------------

def test_victim_and_vendor_are_separated():
    """The Canvas case: a university is the victim, Instructure the vendor."""
    roles = EventEntityRoles(assignments=[
        _a("Canvas", EntityKind.PRODUCT, EntityRole.PRODUCT),
        _a("University of Sydney", EntityKind.ORGANISATION, EntityRole.VICTIM),
        _a("Instructure", EntityKind.ORGANISATION, EntityRole.VENDOR),
        _a("ShinyHunters", EntityKind.THREAT_ACTOR, EntityRole.THREAT_ACTOR),
    ])
    assert roles.victim() == "University of Sydney"
    assert roles.vendor() == "Instructure"


def test_a_product_is_never_returned_as_the_victim():
    """Even mislabelled, a product must not become the victim."""
    roles = EventEntityRoles(assignments=[
        _a("Canvas Learning Management System", EntityKind.PRODUCT, EntityRole.VICTIM),
    ])
    assert roles.victim() is None


def test_highest_confidence_victim_wins():
    roles = EventEntityRoles(assignments=[
        _a("Acme Ltd", EntityKind.ORGANISATION, EntityRole.VICTIM, 0.6),
        _a("Zenith Ltd", EntityKind.ORGANISATION, EntityRole.VICTIM, 0.95),
    ])
    assert roles.victim() == "Zenith Ltd"


def test_no_victim_when_everyone_is_a_bystander():
    roles = EventEntityRoles(assignments=[
        _a("Some Reporter", EntityKind.PERSON, EntityRole.BYSTANDER),
    ])
    assert roles.victim() is None
    assert roles.vendor() is None


def test_by_role_lists_members():
    roles = EventEntityRoles(assignments=[
        _a("A Ltd", EntityKind.ORGANISATION, EntityRole.AFFECTED_CUSTOMER),
        _a("B Ltd", EntityKind.ORGANISATION, EntityRole.AFFECTED_CUSTOMER),
        _a("C Ltd", EntityKind.ORGANISATION, EntityRole.VICTIM),
    ])
    assert roles.by_role(EntityRole.AFFECTED_CUSTOMER) == ["A Ltd", "B Ltd"]


# --------------------------------------------------------------------------
# Classifier behaviour
# --------------------------------------------------------------------------

def test_classifier_uses_the_llm_result():
    response = EventEntityRoles(assignments=[
        _a("Instructure", EntityKind.ORGANISATION, EntityRole.VENDOR),
        _a("University of Sydney", EntityKind.ORGANISATION, EntityRole.VICTIM),
    ])
    fake = _FakeClient(response)
    classifier = EntityRoleClassifier(openai_client=fake)

    result = classifier.classify(
        "University of Sydney investigates Canvas cyberattack", None,
        ["Instructure", "University of Sydney"])

    assert fake.calls == 1
    assert result.victim() == "University of Sydney"
    assert result.vendor() == "Instructure"


def test_hallucinated_entities_are_discarded():
    """Only entities we asked about may enter the database."""
    response = EventEntityRoles(assignments=[
        _a("Acme Ltd", EntityKind.ORGANISATION, EntityRole.VICTIM),
        _a("Never Mentioned Pty Ltd", EntityKind.ORGANISATION, EntityRole.VENDOR),
    ])
    classifier = EntityRoleClassifier(openai_client=_FakeClient(response))
    result = classifier.classify("Acme breach", None, ["Acme Ltd"])

    names = {a.name for a in result.assignments}
    assert names == {"Acme Ltd"}
    assert result.vendor() is None


def test_entities_the_model_skipped_fall_back_to_rules():
    response = EventEntityRoles(assignments=[
        _a("Acme Ltd", EntityKind.ORGANISATION, EntityRole.VICTIM),
    ])
    classifier = EntityRoleClassifier(openai_client=_FakeClient(response))
    result = classifier.classify("Acme breach", None,
                                 ["Acme Ltd", "Australian Cyber Security Centre"])

    names = {a.name for a in result.assignments}
    assert names == {"Acme Ltd", "Australian Cyber Security Centre"}
    acsc = next(a for a in result.assignments
                if a.name == "Australian Cyber Security Centre")
    assert acsc.kind == EntityKind.GOVERNMENT_BODY


def test_llm_failure_falls_back_to_rules():
    classifier = EntityRoleClassifier(
        openai_client=_FakeClient(error=RuntimeError("api down")))
    result = classifier.classify("Acme breach", None, ["Canvas", "ShinyHunters"])

    assert classifier.stats["llm_failures"] == 1
    kinds = {a.name: a.kind for a in result.assignments}
    assert kinds["Canvas"] == EntityKind.PRODUCT
    assert kinds["ShinyHunters"] == EntityKind.THREAT_ACTOR


def test_no_entities_needs_no_call():
    fake = _FakeClient(None)
    classifier = EntityRoleClassifier(openai_client=fake)
    assert classifier.classify("t", None, []).assignments == []
    assert fake.calls == 0


def test_rule_assignments_never_invent_a_victim():
    """Rules alone cannot tell which organisation was breached."""
    classifier = EntityRoleClassifier(openai_client=None)
    classifier._client_ready = True
    result = classifier.classify("Acme breach", None, ["Acme Ltd", "Canvas"])
    assert result.victim() is None


# ---------------------------------------------------------------------------
# A stored victim the recorded entity kind rules out
# ---------------------------------------------------------------------------

def _entities_db():
    """Minimal EntitiesV2 with one entity of each disqualifying kind."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE EntitiesV2 (
        entity_id INTEGER PRIMARY KEY, entity_name TEXT, entity_kind TEXT,
        is_australian INTEGER)""")
    rows = [
        (1, "Australian Emails and Addresses", "other", 1),
        (2, "PDF-1.7", "product", 0),
        (3, "Scott Morrison", "person", 1),
        (4, "UBEL", "threat_actor", 0),
        (5, "Optus", "organisation", 1),
        (6, "Services Australia", "government_body", 1),
        (7, "Untyped Pty Ltd", None, 1),
    ]
    conn.executemany("INSERT INTO EntitiesV2 VALUES (?,?,?,?)", rows)
    return conn


@pytest.mark.parametrize("name", [
    "Australian Emails and Addresses",   # the stolen data, not an organisation
    "PDF-1.7",                           # a file header the scraper picked up
    "Scott Morrison",
    "UBEL",
])
def test_disqualifying_kinds_are_rejected(name):
    conn = _entities_db()
    assert stored_victim_is_invalid(conn, name) is not None


@pytest.mark.parametrize("name", ["Optus", "Services Australia"])
def test_real_organisations_are_kept(name):
    conn = _entities_db()
    assert stored_victim_is_invalid(conn, name) is None


def test_untyped_entity_is_not_rejected_on_kind_alone():
    """Absence of a kind is not evidence against the name.

    Rejecting on a missing kind would clear victims that were simply never
    classified - the failure mode that destroyed 83 correct victim names.
    """
    conn = _entities_db()
    assert stored_victim_is_invalid(conn, "Untyped Pty Ltd") is None


def test_blank_victim_is_not_a_finding():
    conn = _entities_db()
    assert stored_victim_is_invalid(conn, None) is None
    assert stored_victim_is_invalid(conn, "   ") is None
