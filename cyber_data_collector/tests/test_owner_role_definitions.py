"""The owner's victim, vendor and service-provider definitions (entity-roles-v10, 2026-11-04).

Victim: an organisation whose systems were accessed, disrupted, damaged or encrypted, or whose data was taken,
exposed or destroyed - never the people in the data ("Bunnings customers"). Vendor: the company whose software,
platform or device the attack came through. A service firm (law firm, call centre, records storage) is never a
vendor: it is the service provider in a client's report, and its clients' events stay linked to it.
"""
from __future__ import annotations

import sqlite3

from cyber_data_collector.dedup import entity_classification as ec
from cyber_data_collector.dedup.entity_classification import (EntityAssignment, EntityKind, EntityRole,
                                                              EventEntityRoles)


def _a(name, role, confidence=0.9, kind=EntityKind.ORGANISATION, evidence=None):
    return EntityAssignment(name=name, kind=kind, role=role, confidence=confidence, evidence=evidence)


def test_the_prompt_states_the_owners_definitions():
    prompt = ec.SYSTEM_PROMPT
    assert "service_provider" in prompt and "Bunnings customers" in prompt
    assert "stolen credentials" in prompt and "impersonated" in prompt
    assert ec.ROLES_PROMPT_VERSION == "entity-roles-v10"
    described = EntityAssignment.model_json_schema()["properties"]["role"]["description"]
    assert "service_provider" in described and "never the people" in described


def test_a_service_firm_is_the_service_provider_not_the_vendor():
    roles = EventEntityRoles(assignments=[
        _a("Latrobe City Council", EntityRole.VICTIM),
        _a("OracleCMS", EntityRole.SERVICE_PROVIDER),
        _a("Bunnings customers", EntityRole.BYSTANDER),
    ])
    assert roles.victim() == "Latrobe City Council"
    assert roles.vendor() is None
    assert roles.service_provider() == "OracleCMS"


def test_a_service_provider_label_must_quote_the_reporting():
    roles = EventEntityRoles(assignments=[
        _a("HWL Ebsworth", EntityRole.SERVICE_PROVIDER, evidence="HWL Ebsworth's systems were breached by ALPHV."),
        _a("Allens", EntityRole.SERVICE_PROVIDER, evidence="nothing like this was said"),
    ])
    mentions = {"HWL Ebsworth": ["HWL Ebsworth's systems were breached by ALPHV."], "Allens": ["Allens commented."]}
    assert ec.grounded_vendors(roles, mentions) == ["Allens"]
    assert {a.name: a.role for a in roles.assignments} == {"HWL Ebsworth": EntityRole.SERVICE_PROVIDER,
                                                           "Allens": EntityRole.BYSTANDER}


def _roles_db():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT, status TEXT);
        CREATE TABLE EnrichedEventEntities (enriched_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        INSERT INTO EntitiesV2 VALUES (1, 'Bunnings Group Limited'), (2, 'Bunnings customers'), (3, 'Bunnings New Zealand'),
                                      (4, 'FlexBooker');
        INSERT INTO EventDeduplicationMap VALUES ('r1', 'ev'), ('r2', 'ev');
        INSERT INTO EnrichedEvents VALUES ('r1', 'Active'), ('r2', 'Active');
        INSERT INTO DeduplicatedEventEntities VALUES ('ev', 1, 'victim'), ('ev', 2, 'victim'), ('ev', 3, 'victim'),
                                                     ('ev', 4, 'vendor');
        INSERT INTO EnrichedEventEntities VALUES ('r1', 1, 'victim'), ('r1', 2, 'bystander'), ('r2', 2, 'bystander'),
                                                 ('r1', 4, 'vendor');
    """)
    return c


def test_a_victim_link_its_records_call_a_bystander_is_cleared():
    """Bunnings' FlexBooker breach kept "Bunnings customers" as a co-victim (owner, 2026-11-04)."""
    c = _roles_db()
    assert ec.clear_bystander_victims(c, "ev", "Bunnings Group Limited") == 1
    roles = dict(c.execute("SELECT e.entity_name, l.relationship_type FROM DeduplicatedEventEntities l "
                           "JOIN EntitiesV2 e USING (entity_id)").fetchall())
    # A co-victim no record links (attached by another step) keeps its role.
    assert roles == {"Bunnings Group Limited": "victim", "Bunnings customers": "affected",
                     "Bunnings New Zealand": "victim", "FlexBooker": "vendor"}
    assert ec.clear_bystander_victims(c, "ev", "Bunnings Group Limited") == 0      # idempotent


def test_the_chosen_victim_is_never_cleared_and_inactive_records_do_not_count():
    c = _roles_db()
    c.execute("INSERT INTO EventDeduplicationMap VALUES ('r3', 'ev')")
    c.execute("INSERT INTO EnrichedEvents VALUES ('r3', 'Merged')")
    c.execute("INSERT INTO EnrichedEventEntities VALUES ('r3', 2, 'victim')")      # a record no longer a member
    assert ec.clear_bystander_victims(c, "ev", "Bunnings customers") == 0
    assert ec.clear_bystander_victims(c, "ev", None) == 1


def test_a_stale_service_provider_link_is_cleared_like_a_vendor():
    c = _roles_db()
    c.execute("INSERT INTO EntitiesV2 VALUES (5, 'OracleCMS')")
    c.execute("INSERT INTO DeduplicatedEventEntities VALUES ('ev', 5, 'service_provider')")
    assert ec.clear_stale_vendors(c, "ev", None, role="service_provider") == 1
    assert ec.clear_stale_vendors(c, "ev", None) == 0                              # a record labels FlexBooker vendor
    roles = dict(c.execute("SELECT e.entity_name, l.relationship_type FROM DeduplicatedEventEntities l "
                           "JOIN EntitiesV2 e USING (entity_id)").fetchall())
    assert roles["OracleCMS"] == "affected" and roles["FlexBooker"] == "vendor"


def test_a_service_provider_on_this_event_is_no_victim():
    c = _roles_db()
    c.row_factory = sqlite3.Row
    c.execute("ALTER TABLE EntitiesV2 ADD COLUMN entity_kind TEXT")
    c.execute("INSERT INTO EntitiesV2 (entity_id, entity_name) VALUES (6, 'HWL Ebsworth')")
    c.execute("INSERT INTO DeduplicatedEventEntities VALUES ('ev', 6, 'service_provider')")
    assert "service_provider" in ec.stored_victim_is_invalid(c, "HWL Ebsworth", "ev")


def test_events_with_more_than_one_victim_are_classified_again_under_v10():
    from cyber_data_collector.dedup import role_maintenance as rm

    members = ["r1", "r2"]
    row = {"id": "ev", "stored": rm.signature_for_members(members), "version": "entity-roles-v9",
           "has_version_column": True, "members": members, "has_victim": True, "has_vendor": False,
           "victim_departed": False, "victim_is_supplier": False, "victim_count": 2}
    assert rm._state(row) == "prompt"
    assert rm._state({**row, "victim_count": 1}) == "current"
    assert rm._state({**row, "version": ec.ROLES_PROMPT_VERSION}) == "current"


def _supplier_db():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT, vendor_organization_name TEXT, status TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        INSERT INTO EntitiesV2 VALUES (1, 'OracleCMS'), (2, 'Instructure');
        INSERT INTO DeduplicatedEvents VALUES ('latrobe', NULL, 'Active'), ('boroondara', NULL, 'Active'),
                                              ('usyd', 'Instructure', 'Active');
        INSERT INTO DeduplicatedEventEntities VALUES ('latrobe', 1, 'service_provider'),
                                                     ('boroondara', 1, 'service_provider'), ('usyd', 2, 'vendor');
    """)
    return c


def test_a_service_provider_is_a_supplier_its_clients_events_are_linked_to():
    from cyber_data_collector.dedup import supplier_links

    c = _supplier_db()
    assert supplier_links._event_vendors(c, "latrobe") == ["OracleCMS"]
    assert supplier_links._vendor_names(c)["boroondara"] == {"OracleCMS"}


def test_the_supplier_report_marks_a_service_provider():
    from cyber_data_collector.dedup.multi_customer_incidents import supplier_kind

    c = _supplier_db()
    assert supplier_kind(c, ["latrobe", "boroondara"], "OracleCMS") == "service provider"
    assert supplier_kind(c, ["usyd"], "Instructure") == "vendor"
    assert supplier_kind(c, [], "OracleCMS") == "vendor"


def test_a_known_supplier_labelled_victim_beside_its_customers_is_the_reports_vendor():
    """The Canvas roundup under v10: Instructure and its customers all victims, Instructure no longer the supplier."""
    roles = EventEntityRoles(assignments=[
        _a("Instructure", EntityRole.VICTIM, 0.9), _a("University of Adelaide", EntityRole.VICTIM, 0.8),
        _a("RMIT University", EntityRole.VICTIM, 0.8)])
    assert ec.supplier_victims(roles, {"instructure"}) == ["Instructure"]
    assert roles.vendor() == "Instructure" and roles.victim() in ("University of Adelaide", "RMIT University")
    alone = EventEntityRoles(assignments=[_a("Instructure", EntityRole.VICTIM)])
    assert ec.supplier_victims(alone, {"instructure"}) == []                    # its own report: it is the victim
    unknown = EventEntityRoles(assignments=[_a("Optus", EntityRole.VICTIM), _a("Medibank", EntityRole.VICTIM)])
    assert ec.supplier_victims(unknown, {"instructure"}) == []


def _report_db():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT, entity_kind TEXT);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT, vendor_organization_name TEXT, status TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT, status TEXT);
        CREATE TABLE EnrichedEventEntities (enriched_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        INSERT INTO EntitiesV2 VALUES (1, 'DriveWealth', 'organisation'), (2, 'Stake', 'organisation'),
                                      (3, 'Revolut', 'organisation'), (4, 'ZircoDATA', 'organisation'),
                                      (5, 'Monash Health', 'organisation');
        INSERT INTO DeduplicatedEvents VALUES ('report', 'DriveWealth', 'Active'), ('stake', NULL, 'Active'),
                                              ('monash', NULL, 'Active');
        INSERT INTO DeduplicatedEventEntities VALUES ('report', 1, 'vendor'), ('report', 2, 'victim'),
                                                     ('report', 3, 'victim'), ('stake', 2, 'victim'),
                                                     ('monash', 5, 'victim');
        INSERT INTO EventDeduplicationMap VALUES ('r1', 'monash'), ('r2', 'monash');
        INSERT INTO EnrichedEvents VALUES ('r1', 'Active'), ('r2', 'Merged');
        INSERT INTO EnrichedEventEntities VALUES ('r1', 4, 'service_provider'), ('r2', 1, 'vendor');
    """)
    return c


def test_a_customers_own_event_is_not_paired_with_its_suppliers_multi_customer_report():
    """Stake's own DriveWealth report was merged into "Stake and Revolut both impacted" (trial, 2026-11-04)."""
    from cyber_data_collector.dedup.duplicate_judgement import pair_id, supplier_report_pairs

    c = _report_db()
    skip = supplier_report_pairs(c, {})
    assert skip("report", "stake") and skip("stake", "report")
    assert not skip("stake", "monash")
    assert not supplier_report_pairs(c, {pair_id("report", "stake"): "same"})("report", "stake")   # a label decides


def test_a_customers_own_event_is_found_through_the_suppliers_its_records_name():
    from cyber_data_collector.dedup import supplier_links

    names = supplier_links._record_supplier_names(_report_db())
    assert names == {"monash": {"ZircoDATA"}}                                   # an inactive record does not count


def test_an_owner_label_takes_a_supplier_off_an_event_and_its_records(tmp_path):
    """Owner 2026-11-02: Outabox (a breach of that month, mentioned as background) is not Ticketek's supplier."""
    from cyber_data_collector.dedup import supplier_role_labels as srl

    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT, status TEXT, vendor_organization_name TEXT,
                                         vendor_organization_industry TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
        CREATE TABLE EnrichedEventEntities (enriched_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        INSERT INTO EntitiesV2 VALUES (1, 'Outabox'), (2, 'Ticketek');
        INSERT INTO DeduplicatedEvents VALUES ('tk', 'Active', 'Outabox', 'Technology');
        INSERT INTO DeduplicatedEventEntities VALUES ('tk', 1, 'service_provider'), ('tk', 2, 'victim');
        INSERT INTO EventDeduplicationMap VALUES ('r1', 'tk');
        INSERT INTO EnrichedEventEntities VALUES ('r1', 1, 'service_provider'), ('r1', 2, 'victim');
    """)
    labels = tmp_path / "supplier_role_labels.csv"
    labels.write_text("deduplicated_event_id,organisation,label,rationale,labeller\n"
                      "tk,Outabox,not_supplier,background breach,owner\n", encoding="utf-8")
    assert srl.contradicts(c, srl.not_suppliers(c, labels), "tk", "Outabox")
    assert len(srl.apply_stored(c, labels)) == 2                               # the role and the vendor column
    assert c.execute("SELECT relationship_type FROM DeduplicatedEventEntities WHERE entity_id = 1").fetchone()[0] == "bystander"
    assert c.execute("SELECT relationship_type FROM EnrichedEventEntities WHERE entity_id = 1").fetchone()[0] == "bystander"
    assert c.execute("SELECT vendor_organization_name FROM DeduplicatedEvents").fetchone()[0] is None
    assert srl.apply_stored(c, labels) == []                                    # idempotent
    roles = EventEntityRoles(assignments=[_a("Outabox", EntityRole.SERVICE_PROVIDER), _a("Ticketek", EntityRole.VICTIM)])
    assert srl.apply_to_result(c, "tk", roles, srl.not_suppliers(c, labels)) == ["Outabox"]


def test_a_customer_split_out_of_an_event_is_demoted_there_even_as_its_recorded_victim():
    """v10 made Stake the recorded victim of DriveWealth's report, which Stake's own event was split out of."""
    from cyber_data_collector.dedup import supplier_links

    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT, status TEXT, victim_organization_name TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        INSERT INTO EntitiesV2 VALUES (1, 'Stake'), (2, 'Revolut');
        INSERT INTO DeduplicatedEvents VALUES ('dw', 'Active', 'Stake'), ('own', 'Active', 'Stake');
        INSERT INTO DeduplicatedEventEntities VALUES ('dw', 1, 'victim'), ('dw', 2, 'victim'), ('own', 1, 'victim');
    """)
    supplier_links.ensure_schema(c)
    c.execute("INSERT INTO SupplierLinkActions (action_id, action, deduplicated_event_id, new_event_id, entity_id, "
              "detail_json, reason, created_at) VALUES ('a', 'split', 'dw', 'own', NULL, ?, 'r', 't')",
              ('{"customer": "Stake", "stays": "DriveWealth"}',))
    assert supplier_links.demote_split_customers(c) == [{"deduplicated_event_id": "dw", "customer": "Stake"}]
    roles = dict(c.execute("SELECT entity_id, relationship_type FROM DeduplicatedEventEntities "
                           "WHERE deduplicated_event_id = 'dw'"))
    assert roles == {1: "affected_customer", 2: "victim"}
    assert c.execute("SELECT victim_organization_name FROM DeduplicatedEvents WHERE deduplicated_event_id = 'dw'"
                     ).fetchone()[0] == "Revolut"
