"""Repairs of the 2026-10-31 per-customer split, and the owner's validation exceptions.

Each test is one of the owner's reports: SA's Frontier breach split in two, City of Ballarat left on the OracleCMS
event, Australian Conservation Foundation still a victim of Pareto Phone's event, "Top Education Group" folded
into Ticketek's parent "TEG", a person's alias refused by a model's anti-alias.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from cyber_data_collector.dedup import schema, split_rulings
from cyber_data_collector.dedup import contextual_aliases as ca
from cyber_data_collector.dedup import supplier_links as sl
from cyber_data_collector.dedup import validation_exceptions as ve
from cyber_data_collector.dedup import entity_alias_verification as eav
from cyber_data_collector.dedup import supplier_victims as sv
from cyber_data_collector.dedup.backfill import backfill_event_entities, run_backfill
from cyber_data_collector.dedup.entity_resolution import EntityResolver
from cyber_data_collector.dedup.ledger import DedupLedger, repair_departed_links
from cyber_data_collector.tests.test_dedup_v3 import BASE_DDL, _add_event

import pytest


class _Every(dict):
    """Every name has a proper-name verdict (the fixtures' named customers are organisations)."""

    def __contains__(self, key):
        return True


@pytest.fixture(autouse=True)
def _names_judged(monkeypatch):
    from cyber_data_collector.dedup import name_judgement

    monkeypatch.setattr(name_judgement, "stored_probabilities", lambda conn: _Every())
    monkeypatch.setattr(name_judgement, "descriptive_names", lambda conn, *a, **k: set())


def _world(records, victim="South Australian Government"):
    """One event ``ded-<first>`` holding ``records`` [(key, entity, role)], each record linked to its entity."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(BASE_DDL)
    schema.migrate(conn)
    sl.ensure_schema(conn)
    ids = {}
    for key, entity, role in records:
        _add_event(conn, key, f"{entity} breach {key}", f"https://x/{key}", victim=victim, date="2021-11-13")
        if entity not in ids:
            ids[entity] = conn.execute("INSERT INTO EntitiesV2 (entity_name) VALUES (?)", (entity,)).lastrowid
        conn.execute("INSERT INTO EnrichedEventEntities VALUES (?, ?, ?, 0.9)", (f"enr-{key}", ids[entity], role))
    run_backfill(conn)
    ledger = DedupLedger(conn)
    first = f"ded-{records[0][0]}"
    for key, _, _ in records[1:]:
        ledger.merge_events(first, f"ded-{key}", reason="t", respect_overrides=False)
    backfill_event_entities(conn, [first])
    sv.ensure_schema(conn)
    eav.ensure_schema(conn)
    conn.commit()
    return conn, ledger, first


def _victims(conn, did):
    return sorted(r[0] for r in conn.execute(
        """SELECT v.entity_name FROM DeduplicatedEventEntities dee JOIN EntitiesV2 v USING (entity_id)
           WHERE dee.deduplicated_event_id = ? AND dee.relationship_type = 'victim'""", (did,)))


def _event_of(conn, record):
    return conn.execute("""SELECT m.deduplicated_event_id FROM EventDeduplicationMap m JOIN DeduplicatedEvents d
                           USING (deduplicated_event_id) WHERE m.enriched_event_id = ? AND d.status = 'Active'""",
                        (record,)).fetchone()[0]


def test_a_supplier_split_whose_two_events_record_one_victim_is_void():
    """SA's Frontier payroll breach: one record split out "as SA's event", the rest stayed in SA's own event."""
    conn, ledger, big = _world([("sa1", "South Australian Government", "victim"),
                                ("sa2", "South Australian Government", "victim"),
                                ("sa3", "South Australian Government", "victim")])
    part = ledger.split_members(big, ["enr-sa3"], reason="[supplier-split] the records of SA leave", actor="rule")
    conn.commit()
    assert {r.override_id for r in split_rulings.same_victim_supplier_rulings(conn)}
    split_rulings.void_orphaned(conn)
    assert conn.execute("SELECT COUNT(*) FROM DedupOverrides WHERE active = 1").fetchone()[0] == 0
    ledger.merge_events(big, part, reason="calibrated duplicate check 0.98")          # no longer refused
    # Two customers' events keep their rulings.
    conn2, ledger2, big2 = _world([("u1", "University of Canberra", "victim"), ("u2", "Instructure", "victim")],
                                  victim="Instructure")
    part2 = ledger2.split_members(big2, ["enr-u1"], reason="[supplier-split] the records of UC leave", actor="rule")
    conn2.execute("UPDATE DeduplicatedEvents SET victim_organization_name = 'University of Canberra' "
                  "WHERE deduplicated_event_id = ?", (part2,))
    assert split_rulings.same_victim_supplier_rulings(conn2) == []


def test_a_split_takes_the_links_only_its_records_carried():
    """City of Ballarat's notice left the OracleCMS event; Ballarat must not stay a victim there."""
    conn, ledger, big = _world([("lat", "Latrobe City Council", "victim"), ("bor", "City of Boroondara", "victim"),
                                ("bal", "City of Ballarat", "victim")], victim="Latrobe City Council")
    assert "City of Ballarat" in _victims(conn, big)
    ledger.split_members(big, ["enr-bal"], reason="test", actor="rule")
    assert "City of Ballarat" not in _victims(conn, big)
    assert _victims(conn, _event_of(conn, "enr-bal")) == ["City of Ballarat"]
    # A link left behind by an earlier split is removed by the repair, once.
    conn.execute("INSERT INTO DeduplicatedEventEntities (deduplicated_event_id, entity_id, relationship_type) "
                 "SELECT ?, entity_id, 'victim' FROM EntitiesV2 WHERE entity_name = 'City of Ballarat'", (big,))
    assert repair_departed_links(conn) == [(big, 1)]
    assert repair_departed_links(conn) == []


def test_a_split_out_customer_is_an_affected_customer_of_the_event_it_left():
    """Australian Conservation Foundation stayed a victim of Pareto Phone's own event after its records left."""
    conn, ledger, big = _world([("p1", "Pareto Phone", "victim"), ("p2", "Australian Conservation Foundation", "victim"),
                                ("p3", "Australian Conservation Foundation", "victim")], victim="Australian charities")
    part = ledger.split_members(big, ["enr-p3"], reason="test", actor="rule")
    conn.execute("INSERT INTO SupplierLinkActions VALUES ('a1', 'split', ?, ?, NULL, '{}', 'test', '2026-10-31')",
                 (big, part))
    assert "Australian Conservation Foundation" in _victims(conn, big)            # p2 still mentions it
    [done] = sl.demote_split_customers(conn)
    assert done["customer"] == "Australian Conservation Foundation"
    assert _victims(conn, big) == ["Pareto Phone"]
    assert sl.demote_split_customers(conn) == []


def test_the_events_own_victim_is_never_demoted_whatever_its_parts_record():
    conn, ledger, big = _world([("sa1", "South Australian Government", "victim"),
                                ("sa2", "South Australian Government", "victim")])
    part = ledger.split_members(big, ["enr-sa2"], reason="test", actor="rule")
    conn.execute("INSERT INTO SupplierLinkActions VALUES ('a1', 'split', ?, ?, NULL, '{}', 'test', '2026-10-31')",
                 (big, part))
    assert sl.demote_split_customers(conn) == []


def test_a_record_belongs_to_the_customer_its_own_links_name_not_to_the_breached_supplier():
    """Latrobe's own OracleCMS notice: Perplexity names OracleCMS as breached; its links name Latrobe."""
    conn, _, big = _world([("lat", "Latrobe City Council", "affected_customer"),
                           ("bor", "City of Boroondara", "affected_customer")], victim="OracleCMS")
    conn.execute("ALTER TABLE EnrichedEvents ADD COLUMN perplexity_enrichment_data TEXT")
    conn.execute("UPDATE EnrichedEvents SET perplexity_enrichment_data = ?",
                 (json.dumps({"formal_entity_name": "OracleCMS Pty Ltd"}),))
    for name in ("Latrobe City Council", "City of Boroondara"):
        conn.execute("INSERT INTO SupplierVictimAttachments (attachment_id, deduplicated_event_id, entity_id, "
                     "entity_name, decided_by, created_at) SELECT ?, ?, entity_id, entity_name, 'jev', 'x' "
                     "FROM EntitiesV2 WHERE entity_name = ?", (f"a-{name}", big, name))
    resolver = EntityResolver(conn)
    resolver.fit(r[0] for r in conn.execute("SELECT entity_name FROM EntitiesV2"))
    plans, _ = sl.plan_splits(conn, resolver, {big: ["OracleCMS"]})
    [plan] = plans
    assert {plan.stays, *plan.leaving} == {"Latrobe City Council", "City of Boroondara"}
    assert all(len(r) == 1 for r in plan.leaving.values())


def test_a_role_only_victim_is_not_a_customer():
    """Medlab Pathology was a victim role on Australian Clinical Labs' event, never a judged customer."""
    conn, _, big = _world([("acl", "Australian Clinical Labs", "victim"), ("med", "Medlab Pathology", "victim")],
                          victim="Australian Clinical Labs")
    conn.execute("ALTER TABLE EnrichedEvents ADD COLUMN perplexity_enrichment_data TEXT")
    resolver = EntityResolver(conn)
    resolver.fit(r[0] for r in conn.execute("SELECT entity_name FROM EntitiesV2"))
    plans, _ = sl.plan_splits(conn, resolver, {big: ["Qbit"]})
    assert plans == []


def test_an_acronym_two_organisations_fold_into_becomes_contextual():
    """"Top Education Group" -> "TEG" and Ticketek's parent "TEG Pty Ltd" -> "TEG" filed both under TEG."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(BASE_DDL)
    schema.migrate(conn)
    conn.executemany("INSERT INTO EntityAliases (raw_name, canonical_name, canonical_key, source) VALUES (?, ?, '', 'llm')",
                     [("Top Education Group", "TEG"), ("TEG Pty Ltd", "TEG"), ("National Australia Bank", "NAB")])
    moved = ca.migrate_contextual_aliases(conn)
    assert moved == [("Top Education Group", "TEG")]
    assert ca.contextual_aliases(conn) == {"TEG": ["Top Education Group"]}
    assert {r[0] for r in conn.execute("SELECT raw_name FROM EntityAliases")} == {"TEG Pty Ltd", "National Australia Bank"}
    # Learning such an alias afresh keeps it contextual from the start.
    conn.execute("DELETE FROM ContextualEntityAliases")
    EntityResolver(conn).learn_alias(conn, "Top Education Group", "TEG", source="llm")
    assert ca.contextual_aliases(conn) == {"TEG": ["Top Education Group"]}
    assert conn.execute("SELECT 1 FROM EntityAliases WHERE raw_name = 'Top Education Group'").fetchone() is None


def test_validation_exceptions_are_anchored_on_records_and_rechecked():
    conn, ledger, big = _world([("i1", "Ingram Micro", "victim"), ("i2", "Ingram Micro", "victim")],
                               victim="Ingram Micro")
    part = ledger.split_members(big, ["enr-i2"], reason="test", actor="rule")
    conn.commit()
    same = ve.add(conn, "same_incident_as", big, part, note="owner: clearly the same incident")
    victim = ve.add(conn, "victim", big, "Ingram Micro", note="owner: the victim is Ingram Micro")
    assert ve.add(conn, "victim", big, "Ingram Micro", note="again") == victim               # stored once
    results = {r.exception_id: r.result for r in ve.check(conn)}
    assert results == {same: "fail", victim: "pass"}
    ledger.merge_events(big, part, reason="t", respect_overrides=False)
    from cyber_data_collector.quality.regression import measure_validation_exceptions

    assert measure_validation_exceptions(conn)["acceptance.exceptions.failures"].value == 0
    assert conn.execute("SELECT last_result FROM ValidationExceptions WHERE exception_id = ?",
                        (same,)).fetchone()[0] == "fail"                                   # measuring writes nothing
    assert {r.result for r in ve.check(conn)} == {"pass"}


def test_a_persons_alias_outranks_a_models_anti_alias(tmp_path):
    """Medlab Pathology / Australian Clinical Labs: a model's parent_subsidiary verdict gives way to the owner."""
    from cyber_data_collector.dedup import anti_aliases
    from scripts import dedup_admin

    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.executescript(BASE_DDL)
    schema.migrate(conn)
    anti_aliases.ensure_schema(conn)
    eav.ensure_schema(conn)
    anti_aliases.add(conn, "Australian Clinical Labs", "Medlab Pathology", reason="model", relation="parent_subsidiary",
                     source=anti_aliases.SOURCE_LLM)
    anti_aliases.add(conn, "Optus", "Optus Stadium", reason="person")
    conn.commit()
    conn.close()
    args = SimpleNamespace(db=str(db), action="add", variant="Medlab Pathology", canonical="Australian Clinical Labs",
                           reason="owner 2026-10-31: Medlab Pathology is owned by Australian Clinical Labs")
    assert dedup_admin.cmd_alias(args) == 0
    args = SimpleNamespace(db=str(db), action="add", variant="Optus Stadium", canonical="Optus", reason="no")
    assert dedup_admin.cmd_alias(args) == 2                                                # a person's entry stands
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT source FROM EntityAliases WHERE raw_name = 'Medlab Pathology'").fetchone()[0] == "human"


def test_an_incident_month_exception_reads_the_events_date():
    """The AI agent that hacked a gym: dated by its August coverage; the owner's research says April 2026."""
    conn, _, big = _world([("g1", "Australian gym", "affected")], victim=None)
    conn.execute("UPDATE DeduplicatedEvents SET event_date = '2026-08-09'")
    eid = ve.add(conn, "incident_month", big, "2026-04", note="owner: it happened in April 2026")
    assert [r.result for r in ve.check(conn)] == ["fail"]
    conn.execute("UPDATE DeduplicatedEvents SET event_date = '2026-04-01'")
    assert [r.result for r in ve.check(conn)] == ["pass"] and eid


def test_the_report_lists_vendor_attacks_with_several_customers_in_order():
    """Instructure's 2026 Canvas attack reached several universities; one named customer is not enough."""
    from cyber_data_collector.dedup import multi_customer_incidents as mci
    from scripts.build_supplier_incidents_report import build_html

    conn, _, big = _world([("c1", "University of Canberra", "victim"), ("c2", "University of Sydney", "victim")],
                          victim="University of Canberra")
    conn.executemany("INSERT INTO SupplierIncidents VALUES (?, ?, NULL, ?, NULL, ?, ?, 'x', 'x')",
                     [("si-1", "instructure", "Instructure", "2026-04-25", "2026-04-25"),
                      ("si-2", "zirco", "ZircoDATA", "2024-01-24", "2024-01-24"),
                      ("si-3", "acme", "Acme Hosting", "2025-01-01", "2025-01-01")])
    conn.executemany("INSERT INTO SupplierIncidentLinks VALUES (?, ?, ?, ?, 'customer-named', NULL, 'jev', 'x', 'x')",
                     [("si-1", 1, "University of Sydney", None), ("si-1", 2, "University of Canberra", big),
                      ("si-1", 3, "Instructure", None), ("si-1", 4, "Australian universities", None),
                      ("si-2", 5, "Monash Health", None), ("si-2", 6, "Department of Home Affairs", None),
                      ("si-3", 7, "Only Customer Pty Ltd", None)])
    from cyber_data_collector.dedup import descriptive_names

    descriptive_names.ensure_schema(conn)
    conn.execute("INSERT INTO DescriptiveNames (name_key, name, source, reason, created_at) VALUES "
                 "('australian universities', 'Australian universities', 'human', 'a description', 'x')")
    _jurisdictions(conn, {1: "Australia, New South Wales", 5: "Australia, Victoria", 6: "Australia"})
    found = mci.find(conn)
    assert [(i.vendor, [c.name for c in i.customers]) for i in found] == [
        ("Instructure", ["University of Canberra", "University of Sydney"]),
        ("ZircoDATA", ["Department of Home Affairs", "Monash Health"])]
    assert found[0].customers[0].event_id == big
    page = build_html(found, "now")
    assert page.index("Instructure") < page.index("ZircoDATA") and "Acme Hosting" not in page


def _jurisdictions(conn, places):
    from cyber_data_collector.dedup import entity_profiles

    entity_profiles.ensure_schema(conn)
    conn.executemany("INSERT INTO EntityProfiles (entity_id, kind, value, source, created_at) "
                     "VALUES (?, 'jurisdiction', ?, 'llm', 'x')", list(places.items()))


def test_a_named_customer_is_listed_only_when_its_researched_jurisdiction_is_australian():
    """Accellion's global victim list named Kroger and Stanford University: not Australian customers."""
    from cyber_data_collector.dedup import multi_customer_incidents as mci
    from scripts.build_supplier_incidents_report import build_html

    conn, _, big = _world([("c1", "Transport for NSW", "victim")], victim="Transport for NSW")
    if "event_date_precision" not in {r[1] for r in conn.execute("PRAGMA table_info(DeduplicatedEvents)")}:
        conn.execute("ALTER TABLE DeduplicatedEvents ADD COLUMN event_date_precision TEXT")
    conn.execute("UPDATE DeduplicatedEvents SET event_date = '2020-12-01', event_date_precision = 'month' "
                 "WHERE deduplicated_event_id = ?", (big,))
    conn.execute("INSERT INTO SupplierIncidents VALUES ('si-1', 'accellion', NULL, 'Accellion', NULL, "
                 "'2020-12-01', '2021-07-07', 'x', 'x')")
    conn.executemany("INSERT INTO SupplierIncidentLinks VALUES ('si-1', ?, ?, ?, 'customer-named', NULL, 'jev', 'x', 'x')",
                     [(1, "Transport for NSW", big), (2, "SBS", None), (3, "Stanford University", None),
                      (4, "Kroger", None)])
    _jurisdictions(conn, {2: "Australia", 3: "United States"})
    assert mci.unplaced_customers(conn) == [(4, "Kroger")]
    [inc] = mci.find(conn)
    assert [c.name for c in inc.customers] == ["SBS", "Transport for NSW"]
    assert (inc.outside, inc.unplaced) == (["Stanford University"], ["Kroger"])
    assert mci.attack_label(inc) == "December 2020"            # one date for the attack, at its precision
    page = build_html([inc], "now")
    assert "Event date" not in page and "outside Australia: Stanford University" in page


def _separate(events):
    """Separate Active events [(key, victim, date, vendor)]."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(BASE_DDL)
    schema.migrate(conn)
    sl.ensure_schema(conn)
    sv.ensure_schema(conn)
    for key, victim, date, vendor in events:
        _add_event(conn, key, f"{victim} breach", f"https://x/{key}", victim=victim, date=date)
        conn.execute("UPDATE DeduplicatedEvents SET vendor_organization_name = ? WHERE deduplicated_event_id = ?",
                     (vendor, f"ded-{key}"))
    run_backfill(conn)
    conn.commit()
    return conn


def _accellion():
    conn = _separate([("t", "Transport for NSW", "2020-12-01", "Accellion"), ("a", "ASIC", "2020-12-28", "Accellion"),
                      ("e", "NSW Department of Education", "2021-07-07", "Accellion"),
                      ("x", "Amazon", "2023-05-27", None)])
    conn.executemany("INSERT INTO SupplierIncidents VALUES (?, 'accellion', NULL, 'Accellion', NULL, ?, ?, 'x', 'x')",
                     [("si-a", "2020-12-01", "2020-12-28"), ("si-b", "2020-12-28", "2021-07-07")])
    conn.execute("INSERT INTO SupplierIncidents VALUES ('si-p', 'pwc', NULL, 'PwC', NULL, '2023-05-27', '2023-05-27', "
                 "'x', 'x')")
    conn.executemany("INSERT INTO SupplierIncidentLinks VALUES (?, ?, ?, ?, ?, ?, ?, 'x', 'x')",
                     [("si-a", 1, "Transport for NSW", "ded-t", "split-customer", None, "rule"),
                      ("si-a", 2, "ASIC", "ded-a", "judged-same-incident", 0.97, "jev"),
                      ("si-b", 2, "ASIC", "ded-a", "judged-same-incident", 0.96, "jev"),
                      ("si-b", 3, "NSW Department of Education", "ded-e", "vendor-recorded", None, "rule"),
                      ("si-b", 4, "SBS", None, "customer-named", 0.88, "jev"),
                      ("si-p", 5, "Amazon", "ded-x", "vendor-recorded", None, "rule")])
    conn.commit()
    resolver = EntityResolver(conn)
    resolver.fit(["Accellion", "PwC"])
    return conn, resolver


def test_two_incidents_of_one_supplier_sharing_an_event_become_one():
    """Accellion: ASIC in two incidents, the second anchored by NSW Education's July 2021 attack, which a pair
    label rules a different incident; PwC's link to Amazon's event, which no longer records PwC."""
    conn, resolver = _accellion()
    labels = {(sv.KIND_PAIR, "ded-a|ded-e"): "different"}
    report = sl.repair_registry(conn, resolver, labels)
    assert {(r["supplier"], r["customer"]) for r in report["stale_links_removed"]} == {
        ("PwC", "Amazon"), ("Accellion", "NSW Department of Education")}
    assert [(r["kept"], r["merged"]) for r in report["incidents_merged"]] == [("si-a", "si-b")]
    rows = conn.execute("SELECT supplier_incident_id, customer_name, customer_event_id FROM SupplierIncidentLinks "
                        "ORDER BY customer_name").fetchall()
    assert [tuple(r) for r in rows] == [("si-a", "ASIC", "ded-a"), ("si-a", "SBS", None),
                                        ("si-a", "Transport for NSW", "ded-t")]
    assert [r[0] for r in conn.execute("SELECT supplier_incident_id FROM SupplierIncidents")] == ["si-a"]
    assert tuple(conn.execute("SELECT first_date, last_date FROM SupplierIncidents").fetchone()) == (
        "2020-12-01", "2020-12-28")
    assert sl.repair_registry(conn, resolver, labels)["incidents_merged"] == []            # idempotent


def test_without_a_label_the_rule_linked_event_stays_and_the_incidents_merge():
    conn, resolver = _accellion()
    report = sl.repair_registry(conn, resolver, {})
    assert [(r["kept"], r["merged"]) for r in report["incidents_merged"]] == [("si-a", "si-b")]
    assert conn.execute("SELECT COUNT(*) FROM SupplierIncidentLinks WHERE supplier_incident_id = 'si-a'"
                        ).fetchone()[0] == 4


def test_a_placed_event_stays_in_its_incident():
    """A later run used to file ASIC under whichever Accellion incident was nearest in date at the time."""
    conn, resolver = _accellion()
    registry = sl.Registry(conn, resolver)
    assert registry.incident("Accellion", "2020-12-28", "ded-a").supplier_incident_id == "si-a"
    conn.execute("DELETE FROM SupplierIncidentLinks WHERE supplier_incident_id = 'si-a' AND customer_event_id = 'ded-a'")
    registry = sl.Registry(conn, resolver)
    assert registry.incident("Accellion", "2020-12-28", "ded-a").supplier_incident_id == "si-b"
    # A pair label against a stored member keeps a new event out of that incident.
    registry = sl.Registry(conn, resolver)
    placed = registry.incident("Accellion", "2020-12-20", "ded-new", apart=lambda x, y: y == "ded-t")
    assert placed.supplier_incident_id == "si-b"


def test_supplier_incident_exceptions():
    """Owner 2026-10-31: the Accellion customers are one incident; the Reserve Bank of New Zealand is no customer."""
    from cyber_data_collector.dedup import entity_profiles

    conn, resolver = _accellion()
    sl.repair_registry(conn, resolver, {})
    entity_profiles.ensure_schema(conn)
    conn.execute("INSERT INTO SupplierIncidentLinks VALUES ('si-a', 9, 'Reserve Bank of New Zealand', NULL, "
                 "'customer-named', 0.94, 'escalation', 'x', 'x')")
    one = ve.add(conn, "same_supplier_incident_as", "ded-t", "ded-a", note="owner: clearly one incident")
    out = ve.add(conn, "not_supplier_customer", "ded-t", "Reserve Bank of New Zealand", note="owner: not in Australia")
    results = {r.exception_id: r.result for r in ve.check(conn)}
    assert results[one] == "pass" and results[out] == "pass"       # no researched jurisdiction: not listed
    conn.execute("INSERT INTO EntityProfiles (entity_id, kind, value, source, created_at) "
                 "VALUES (9, 'jurisdiction', 'Australia', 'human', 'x')")
    assert {r.exception_id: r.result for r in ve.check(conn)}[out] == "fail"
    conn.execute("INSERT INTO SupplierIncidents VALUES ('si-z', 'accellion', NULL, 'Accellion', NULL, NULL, NULL, "
                 "'x', 'x')")
    conn.execute("INSERT INTO SupplierIncidentLinks VALUES ('si-z', 2, 'ASIC', 'ded-a', 'judged-same-incident', 0.9, "
                 "'jev', 'x', 'x')")
    assert {r.exception_id: r.result for r in ve.check(conn)}[one] == "fail"


def test_an_exception_is_revised_by_a_later_owner_decision_keeping_the_first_words():
    """Stake: first "the same incident", then "do not merge into the supplier's event"."""
    conn, ledger, big = _world([("s1", "Stake", "victim"), ("s2", "Stake", "victim")], victim=None)
    part = ledger.split_members(big, ["enr-s2"], reason="test", actor="rule")
    eid = ve.add(conn, "same_incident_as", big, part, note="owner: clearly the same incident")
    ve.revise(conn, eid, "not_victim", "Stake", note="owner: do not merge into the supplier's event")
    kind, value, note = conn.execute("SELECT kind, value, note FROM ValidationExceptions").fetchone()
    assert (kind, value) == ("not_victim", "Stake") and note.startswith("owner: clearly") and "do not merge" in note
    assert [r.result for r in ve.check(conn)] == ["fail"]                 # big still records Stake as its victim


def test_a_named_customer_the_index_misses_reuses_its_existing_entity_row():
    """2026-10-31: the supplier step stopped on UNIQUE(entity_name) when the index did not hold a stored name."""
    conn, _ = _accellion()
    existing = conn.execute("INSERT INTO EntitiesV2 (entity_name) VALUES ('Reserve Bank of New Zealand')").lastrowid

    class Missing:
        def find(self, name):
            return None

    cand = sv.VictimCandidate("ded-t", "Reserve Bank of New Zealand", None, None)
    assert sv._entity_for(conn, cand, Missing()) == existing
    other = sv.VictimCandidate("ded-t", "Kroger", None, None)
    first = sv._entity_for(conn, other, Missing())
    assert sv._entity_for(conn, other, Missing()) == first        # created once, then found


def test_customer_events_on_one_date_with_and_without_a_precision_sort():
    """2026-10-31: the report and the exception check stopped on (date, None) < (date, 'month')."""
    from cyber_data_collector.dedup import multi_customer_incidents as mci

    conn, resolver = _accellion()
    sl.repair_registry(conn, resolver, {})
    if "event_date_precision" not in {r[1] for r in conn.execute("PRAGMA table_info(DeduplicatedEvents)")}:
        conn.execute("ALTER TABLE DeduplicatedEvents ADD COLUMN event_date_precision TEXT")
    conn.execute("UPDATE DeduplicatedEvents SET event_date = '2020-12-01'")
    conn.execute("UPDATE DeduplicatedEvents SET event_date_precision = 'month' WHERE deduplicated_event_id = 'ded-a'")
    [inc] = [i for i in mci.find(conn) if i.vendor == "Accellion"]
    assert inc.attack_date == "2020-12-01"


def test_proposals_pair_names_of_one_customer_within_an_incident_and_not_unrelated_ones():
    """Owner review 2026-10-31: RMIT, ANZ, PwC, the ACT Health Directorate; TEG is not Ticketek."""
    from cyber_data_collector.dedup import alias_learning as al

    assert al.abbreviation_fits("RMIT University", "Royal Melbourne Institute of Technology")
    assert al.abbreviation_fits("ANZ", "Australia and New Zealand Banking Group Limited")
    assert al.abbreviation_fits("PwC", "PricewaterhouseCoopers")
    assert not al.abbreviation_fits("TEG", "Ticketek")
    conn, _ = _accellion()
    conn.executemany("INSERT INTO SupplierIncidentLinks VALUES ('si-a', ?, ?, NULL, 'customer-named', 0.9, 'jev', "
                     "'x', 'x')",
                     [(20, "ACT Government"), (21, "ACT Health Directorate"), (22, "RMIT University"),
                      (23, "Royal Melbourne Institute of Technology"), (24, "ActionAid Australia"),
                      (25, "Amnesty International Australia"), (26, "Ticketek"), (27, "TEG")])
    pairs = set(al.supplier_pair_proposals(conn))
    assert ("ACT Government", "ACT Health Directorate") in pairs
    assert ("RMIT University", "Royal Melbourne Institute of Technology") in pairs
    assert ("ActionAid Australia", "Amnesty International Australia") not in pairs
    assert ("TEG", "Ticketek") not in pairs


def _verdict(conn, a, b, verdict, model="sonar-pro"):
    from cyber_data_collector.dedup import entity_alias_verification as eav

    eav.ensure_schema(conn)
    a, b = sorted((a, b))
    conn.execute("INSERT INTO EntityAliasDecisions (name, other_name, verdict, model, prompt_version, decided_at) "
                 "VALUES (?, ?, ?, ?, 'entity-alias-v1', '2026-10-31')", (a, b, verdict, model))


def test_one_customer_under_two_names_or_with_its_part_is_listed_once_and_the_vendor_is_no_customer(monkeypatch):
    """NAB / National Australia Bank (NAB); ACT Government / ACT Health Directorate; the vendor under another name;
    "The College" is a description."""
    from cyber_data_collector.dedup import multi_customer_incidents as mci, name_judgement

    conn, resolver = _accellion()
    sl.repair_registry(conn, resolver, {})
    _jurisdictions(conn, {n: "Australia" for n in range(30, 36)})
    conn.executemany("INSERT INTO SupplierIncidentLinks VALUES ('si-a', ?, ?, NULL, 'customer-named', 0.9, 'jev', "
                     "'x', 'x')",
                     [(30, "NAB"), (31, "National Australia Bank (NAB)"), (32, "ACT Health Directorate"),
                      (33, "Accellion Inc"), (34, "The College"), (35, "ACT Government")])
    _verdict(conn, "NAB", "National Australia Bank (NAB)", "same")
    _verdict(conn, "ACT Government", "ACT Health Directorate", "parent_subsidiary")
    _verdict(conn, "Accellion", "Accellion Inc", "same", model="human")
    monkeypatch.setattr(name_judgement, "descriptive_names", lambda conn, *a, **k: {"the college"})
    [inc] = [i for i in mci.find(conn) if i.vendor == "Accellion"]
    rows = {c.name: c.also for c in inc.customers}
    assert rows.get("ACT Government") == ["ACT Health Directorate"]
    assert rows.get("NAB") == ["National Australia Bank (NAB)"]
    assert "Accellion Inc" not in rows and "The College" not in rows and "The College" not in inc.unplaced


def test_a_named_customer_with_no_name_verdict_is_not_listed_until_judged(monkeypatch):
    from cyber_data_collector.dedup import multi_customer_incidents as mci, name_judgement

    conn, resolver = _accellion()
    sl.repair_registry(conn, resolver, {})
    _jurisdictions(conn, {40: "Australia"})
    conn.execute("INSERT INTO SupplierIncidentLinks VALUES ('si-a', 40, 'Early Learning', NULL, 'customer-named', "
                 "0.9, 'jev', 'x', 'x')")
    monkeypatch.setattr(name_judgement, "stored_probabilities", lambda conn: {"asic": 0.9})
    [inc] = [i for i in mci.find(conn) if i.vendor == "Accellion"]
    assert "Early Learning" not in [c.name for c in inc.customers] and "Early Learning" in inc.unplaced


def test_each_attack_gets_a_description_rewritten_only_when_its_evidence_changes():
    from cyber_data_collector.dedup import multi_customer_incidents as mci

    conn, resolver = _accellion()
    sl.repair_registry(conn, resolver, {})
    calls = []

    def describer(state):
        calls.append(state)
        return f"Accellion's file transfer appliance was attacked ({len(state['accounts'])} accounts)."

    assert mci.describe_incidents(conn, describer=describer, model="m")["written"] == 1
    assert mci.describe_incidents(conn, describer=describer, model="m")["current"] == 1 and len(calls) == 1
    conn.execute("UPDATE EnrichedEvents SET description = 'ASIC confirms the Accellion breach' "
                 "WHERE enriched_event_id = 'enr-a'")
    assert mci.describe_incidents(conn, describer=describer, model="m")["written"] == 1 and len(calls) == 2


def test_an_event_recording_a_vendor_under_the_old_role_prompt_is_classified_again():
    """v3: ESET, an agency and a photo credit were recorded as suppliers."""
    from cyber_data_collector.dedup import role_maintenance as rm

    conn, _ = _accellion()
    entity = conn.execute("INSERT INTO EntitiesV2 (entity_name) VALUES ('ESET')").lastrowid
    conn.execute("INSERT INTO DeduplicatedEventEntities (deduplicated_event_id, entity_id, relationship_type) "
                 "VALUES ('ded-a', ?, 'vendor')", (entity,))
    members = [r[0] for r in conn.execute("SELECT enriched_event_id FROM EventDeduplicationMap "
                                          "WHERE deduplicated_event_id = 'ded-a'")]
    conn.execute("UPDATE DeduplicatedEvents SET roles_member_signature = ?, roles_prompt_version = 'entity-roles-v2' "
                 "WHERE deduplicated_event_id = 'ded-a'", (rm.signature_for_members(members),))
    assert "ded-a" in rm.stale_event_ids(conn)


def test_vendor_and_customer_once_exceptions():
    conn, resolver = _accellion()
    sl.repair_registry(conn, resolver, {})
    _jurisdictions(conn, {30: "Australia", 31: "Australia"})
    conn.executemany("INSERT INTO SupplierIncidentLinks VALUES ('si-a', ?, ?, NULL, 'customer-named', 0.9, 'jev', "
                     "'x', 'x')", [(30, "NAB"), (31, "National Australia Bank (NAB)")])
    once = ve.add(conn, "supplier_customers_once", "ded-t", "NAB|National Australia Bank (NAB)", note="owner")
    vendor = ve.add(conn, "not_vendor", "ded-t", "ESET", note="owner")
    results = {r.exception_id: r.result for r in ve.check(conn)}
    assert results[once] == "fail" and results[vendor] == "pass"
    _verdict(conn, "NAB", "National Australia Bank (NAB)", "same", model="human")
    conn.execute("UPDATE DeduplicatedEvents SET vendor_organization_name = 'ESET' WHERE deduplicated_event_id = 'ded-t'")
    results = {r.exception_id: r.result for r in ve.check(conn)}
    assert results[once] == "pass" and results[vendor] == "fail"


def test_the_dashboard_counts_vendor_attacks_per_half_year_of_the_attack():
    from scripts.build_static_dashboard import get_vendor_attacks_by_half_year

    conn, resolver = _accellion()
    sl.repair_registry(conn, resolver, {})
    chart = get_vendor_attacks_by_half_year(conn, "2020-01-01", "2021-12-31")
    assert dict(zip(chart["periods"], chart["counts"]))["2020 H2"] == 1
    assert sum(chart["counts"]) == 1 and chart["undated"] == 0 and chart["outside"] == 0
    assert get_vendor_attacks_by_half_year(conn, "2022-01-01", "2022-12-31")["outside"] == 1


def test_a_usable_headline_beats_a_stored_llm_paragraph():
    """Owner 2026-10-31: the Optus White Pages event's title was a 528-character Perplexity answer."""
    from cyber_data_collector.dedup.title_selection import derive_title

    prose = ("The incident reported at the original URL concerns a **data breach at Optus** that occurred prior to "
             "April 2020, involving the exposure of personal information of approximately **50,000 customers** due to "
             "a system error that resulted in their details being published in the White Pages directory[4][2][3]")
    assert derive_title([prose, "Optus White Pages listing breach (2019)"], victim="Optus",
                        current_title=prose) == "Optus White Pages listing breach (2019)"


def test_a_named_customer_gets_a_record_from_its_own_reporting_once():
    """Owner 2026-10-31: an event for a customer only named in the reporting, from its own reporting - never the
    supplier's article that named it."""
    from cyber_data_collector.dedup import multi_customer_incidents as mci

    conn, resolver = _accellion()
    sl.repair_registry(conn, resolver, {})
    _jurisdictions(conn, {4: "Australia"})
    have = {r[1] for r in conn.execute("PRAGMA table_info(RawEvents)")}
    for column in ("source_event_id", "raw_content", "event_date", "source_metadata", "is_processed"):
        if column not in have:
            conn.execute(f"ALTER TABLE RawEvents ADD COLUMN {column} TEXT")
    supplier_article = conn.execute("SELECT source_url FROM RawEvents WHERE raw_event_id = 'raw-t'").fetchone()[0]
    asked = []

    def search(query, model):
        asked.append(query)
        return {"answer": "FOUND: yes\nINCIDENT: SBS confirmed its files were taken.\nSOURCES: ...",
                "citations": [supplier_article, "https://www.sbs.com.au/news/sbs-accellion-statement"]}

    assert [c.name for _, c in mci.customers_to_search(conn)] == ["SBS"]
    counts = mci.search_customer_reports(conn, search=search)
    assert counts == {"searched": 1, "queued": 1, "pending": 0} and "SBS" in asked[0] and "Accellion" in asked[0]
    url, title, done = conn.execute("SELECT source_url, raw_title, is_processed FROM RawEvents "
                                    "WHERE source_url LIKE '%sbs.com.au%'").fetchone()
    assert url == "https://www.sbs.com.au/news/sbs-accellion-statement" and title == "SBS cyber incident" and int(done) == 0
    assert mci.customers_to_search(conn) == [] and mci.search_customer_reports(conn, search=search)["searched"] == 0
