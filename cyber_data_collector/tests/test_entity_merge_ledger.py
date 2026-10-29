"""Entity folds are lossless, recorded and reversible.

The fold used to delete the dropped entity before inheriting its attributes, drop a link both spellings held
on one event without a trace, and leave nothing to undo it with. Every test runs on a real schema with fakes
only - an autouse fixture sets bogus keys so a call that reaches a model fails instead of being made.
"""

from __future__ import annotations

import json
import shutil
import sqlite3

import pytest

from cyber_data_collector.dedup import entity_alias_verification as eav
from cyber_data_collector.dedup import entity_merge_ledger as ledger
from cyber_data_collector.dedup.entity_merge import merge_entities
from cyber_data_collector.storage.schema_init import initialize_database


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.setenv("PERPLEXITY_API_KEY", "not-a-real-key")
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    path = tmp_path_factory.mktemp("schema") / "template.db"
    initialize_database(str(path))
    return path


@pytest.fixture
def conn(tmp_path, template):
    path = tmp_path / "merge.db"
    shutil.copy(template, path)
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys = ON")
    # Two spellings of one organisation: the better-spelled one is empty, the other carries attributes.
    c.execute("INSERT INTO EntitiesV2 (entity_id, entity_name, entity_kind) VALUES (1, 'Acme Health', NULL)")
    c.execute("INSERT INTO EntitiesV2 (entity_id, entity_name, entity_kind, industry, is_australian, size_estimate, "
              "size_confidence, size_method) VALUES (2, 'ACME HEALTH PTY LTD', 'organisation', 'Healthcare', 1, "
              "'LARGE', 0.8, 'perplexity')")
    c.execute("INSERT INTO EntitiesV2 (entity_id, entity_name) VALUES (3, 'Other Org')")
    c.execute("INSERT INTO RawEvents (raw_event_id, source_type, raw_title) VALUES ('r1', 'Perplexity', 't')")
    for eid in ("e1", "e2", "e3"):
        c.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, is_australian_event, is_specific_event) "
                  "VALUES (?, 'r1', 't', 1, 1)", (eid,))
    for did, name, vendor in (("d1", "ACME HEALTH PTY LTD", None), ("d2", "Other Org", "ACME HEALTH PTY LTD"),
                              ("d3", "Acme Health", None)):
        c.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, master_enriched_event_id, title, "
                  "is_australian_event, is_specific_event, victim_organization_name, vendor_organization_name, status) "
                  "VALUES (?, ?, 't', 1, 1, ?, ?, 'Active')",
                  (did, "e" + did[1], name, vendor))
    c.executemany("INSERT INTO EnrichedEventEntities (enriched_event_id, entity_id, relationship_type, "
                  "confidence_score) VALUES (?, ?, ?, ?)", [
                      ("e1", 1, "affected", 0.4), ("e1", 2, "victim", 0.9),     # both spellings on one record
                      ("e2", 2, "victim", 0.7), ("e3", 1, "victim", 0.6)])
    c.executemany("INSERT INTO DeduplicatedEventEntities (deduplicated_event_id, entity_id, relationship_type, "
                  "confidence_score, source_count) VALUES (?, ?, ?, ?, ?)", [
                      ("d1", 1, "affected_customer", 0.5, 2), ("d1", 2, "victim", 0.8, 3),    # collision
                      ("d2", 2, "vendor", 0.6, 1), ("d2", 3, "victim", 0.6, 1), ("d3", 1, "victim", 0.9, 4)])
    c.execute("INSERT INTO EntityAliases (raw_name, canonical_name, canonical_key, source, confidence) "
              "VALUES ('ACME HEALTH PTY LTD', 'Acme Health', 'acme health', 'llm', 1.0)")
    c.execute("CREATE TABLE EntityNotes (note_id INTEGER PRIMARY KEY, entity_id INTEGER, note TEXT)")
    c.execute("INSERT INTO EntityNotes VALUES (1, 2, 'sized from the annual report')")
    eav.ensure_schema(c)
    c.execute("INSERT INTO EntityAliasDecisions VALUES ('ACME HEALTH PTY LTD', 'Acme Health', 'same', 'Acme Health', "
              "'one company', '[]', 'sonar-pro', ?, '2026-10-29')", (eav.PROMPT_VERSION,))
    c.commit()
    return c


STATE_TABLES = ("EntitiesV2", "EnrichedEventEntities", "DeduplicatedEventEntities", "EntityNotes")


def _state(conn):
    """Every row the fold can touch, in a comparable form."""
    out = {}
    for table in STATE_TABLES:
        cursor = conn.execute(f"SELECT * FROM {table}")
        names = [d[0] for d in cursor.description]
        out[table] = sorted(json.dumps(dict(zip(names, row)), sort_keys=True, default=str) for row in cursor)
    out["events"] = sorted(conn.execute("SELECT deduplicated_event_id, victim_organization_name, "
                                        "vendor_organization_name FROM DeduplicatedEvents"))
    return out


def test_a_fold_inherits_before_deleting_keeps_the_stronger_role_and_loses_no_link(conn):
    assert merge_entities(conn, "Acme Health", ["ACME HEALTH PTY LTD"], source="llm") == 1
    assert conn.execute("SELECT 1 FROM EntitiesV2 WHERE entity_id = 2").fetchone() is None
    kept = conn.execute("SELECT entity_kind, industry, is_australian, size_estimate, size_confidence, size_method "
                        "FROM EntitiesV2 WHERE entity_id = 1").fetchone()
    assert kept == ("organisation", "Healthcare", 1, "LARGE", 0.8, "perplexity")        # inherited, not lost
    # e1 held both spellings: the stronger role (victim) survives with the higher confidence.
    assert conn.execute("SELECT relationship_type, confidence_score FROM EnrichedEventEntities "
                        "WHERE enriched_event_id = 'e1' AND entity_id = 1").fetchone() == ("victim", 0.9)
    assert conn.execute("SELECT relationship_type, confidence_score, source_count FROM DeduplicatedEventEntities "
                        "WHERE deduplicated_event_id = 'd1' AND entity_id = 1").fetchone() == ("victim", 0.8, 5)
    assert conn.execute("SELECT COUNT(*) FROM EnrichedEventEntities WHERE entity_id = 1").fetchone()[0] == 3
    assert conn.execute("SELECT victim_organization_name FROM DeduplicatedEvents WHERE deduplicated_event_id = 'd1'"
                        ).fetchone()[0] == "Acme Health"


def test_the_ledger_holds_what_is_needed_to_restore_the_fold(conn):
    merge_entities(conn, "Acme Health", ["ACME HEALTH PTY LTD"], source="llm")
    (entry,) = ledger._rows(conn, "SELECT * FROM EntityMerges")
    assert (entry["kept_entity_id"], entry["dropped_entity_id"], entry["status"]) == (1, 2, "applied")
    assert json.loads(entry["entity_row_json"])["industry"] == "Healthcare"
    assert json.loads(entry["kept_before_json"])["industry"] is None
    assert json.loads(entry["inherited_json"])["industry"] == {"before": None, "after": "Healthcare"}
    links = json.loads(entry["links_json"])
    assert sorted((l["table"], l["subject_id"], l["outcome"]) for l in links) == [
        ("DeduplicatedEventEntities", "d1", "collided"), ("DeduplicatedEventEntities", "d2", "moved"),
        ("EnrichedEventEntities", "e1", "collided"), ("EnrichedEventEntities", "e2", "moved")]
    collided = next(l for l in links if l["subject_id"] == "e1")
    assert collided["row"]["relationship_type"] == "victim" and collided["kept_row_before"]["relationship_type"] == "affected"
    assert json.loads(entry["roles_json"])["EnrichedEventEntities"] == {"victim": 2}
    assert json.loads(entry["source_ids_json"])["raw_event_ids"] == ["r1"]
    assert json.loads(entry["scalar_json"]) == {"victim": ["d1"], "vendor": ["d2"]}
    assert json.loads(entry["references_json"]) == {"EntityNotes": 1}
    assert json.loads(entry["alias_json"])["canonical_name"] == "Acme Health"
    assert json.loads(entry["decision_json"])["verdict"] == "same"


def test_merge_then_unmerge_is_an_exact_round_trip(conn):
    before = _state(conn)
    merge_entities(conn, "Acme Health", ["ACME HEALTH PTY LTD"], source="llm")
    assert _state(conn) != before
    (entry,) = ledger.list_merges(conn)
    report = ledger.unmerge_entity(conn, entry["merge_id"], reason="test", keep_alias=True)
    assert _state(conn) == before
    assert (report.links_restored, report.collisions_restored, report.events_renamed) == (2, 2, 2)
    assert ledger.list_merges(conn, "applied") == [] and ledger.list_merges(conn)[0]["status"] == "reversed"
    # The restored entity keeps its id, so rows still keyed by it point at it again.
    assert conn.execute("SELECT entity_name FROM EntitiesV2 WHERE entity_id = 2").fetchone()[0] == "ACME HEALTH PTY LTD"
    assert conn.execute("SELECT note FROM EntityNotes WHERE entity_id = 2").fetchone() is not None
    with pytest.raises(ledger.UnmergeError, match="already reversed"):
        ledger.unmerge_entity(conn, entry["merge_id"])


def test_unmerging_removes_the_alias_and_records_the_pair_as_different(conn):
    merge_entities(conn, "Acme Health", ["ACME HEALTH PTY LTD"], source="llm")
    (entry,) = ledger.list_merges(conn)
    report = ledger.unmerge_entity(conn, entry["merge_id"], reason="a different company")
    assert report.alias_removed
    assert conn.execute("SELECT COUNT(*) FROM EntityAliases").fetchone()[0] == 0
    assert eav.stored_verdicts(conn)[("ACME HEALTH PTY LTD", "Acme Health")]["verdict"] == "different"


def test_a_dry_run_changes_nothing(conn):
    merge_entities(conn, "Acme Health", ["ACME HEALTH PTY LTD"], source="llm")
    merged = _state(conn)
    (entry,) = ledger.list_merges(conn)
    report = ledger.unmerge_entity(conn, entry["merge_id"], dry_run=True)
    assert report.links_restored == 2 and _state(conn) == merged
    assert ledger.list_merges(conn)[0]["status"] == "applied"


def test_attributes_changed_since_the_fold_are_not_overwritten_on_unmerge(conn):
    merge_entities(conn, "Acme Health", ["ACME HEALTH PTY LTD"], source="llm")
    conn.execute("UPDATE EntitiesV2 SET industry = 'Aged care' WHERE entity_id = 1")
    (entry,) = ledger.list_merges(conn)
    report = ledger.unmerge_entity(conn, entry["merge_id"])
    assert "industry" in report.attributes_kept
    assert conn.execute("SELECT industry FROM EntitiesV2 WHERE entity_id = 1").fetchone()[0] == "Aged care"


def test_unmerge_refuses_when_the_database_has_moved_on(conn):
    merge_entities(conn, "Acme Health", ["ACME HEALTH PTY LTD"], source="llm")
    (entry,) = ledger.list_merges(conn)
    conn.execute("INSERT INTO EntitiesV2 (entity_name) VALUES ('ACME HEALTH PTY LTD')")
    with pytest.raises(ledger.UnmergeError, match="is now entity"):
        ledger.unmerge_entity(conn, entry["merge_id"])
    conn.execute("DELETE FROM EntitiesV2 WHERE entity_name = 'ACME HEALTH PTY LTD'")
    # The kept entity is itself folded away later: that fold is undone first.
    merge_entities(conn, "Other Org", ["Acme Health"], source="llm")
    with pytest.raises(ledger.UnmergeError, match="unmerge that first"):
        ledger.unmerge_entity(conn, entry["merge_id"])
    assert conn.execute("SELECT COUNT(*) FROM EntitiesV2 WHERE entity_id = 2").fetchone()[0] == 0   # nothing half done


def test_a_link_that_has_since_moved_is_reported_not_recreated(conn):
    merge_entities(conn, "Acme Health", ["ACME HEALTH PTY LTD"], source="llm")
    conn.execute("UPDATE EnrichedEventEntities SET entity_id = 3 WHERE enriched_event_id = 'e2' AND entity_id = 1")
    (entry,) = ledger.list_merges(conn)
    report = ledger.unmerge_entity(conn, entry["merge_id"])
    assert report.links_skipped == [("EnrichedEventEntities", "e2", "no longer linked to the kept entity")]
    assert conn.execute("SELECT entity_id FROM EnrichedEventEntities WHERE enriched_event_id = 'e2'").fetchone()[0] == 3


def test_a_failed_fold_leaves_nothing_behind(conn):
    before = _state(conn)
    # The ledger insert is the last statement of the fold; a table of the wrong shape makes it fail.
    conn.execute("CREATE TABLE EntityMerges (merge_id TEXT PRIMARY KEY)")
    conn.commit()
    with pytest.raises(sqlite3.Error):
        merge_entities(conn, "Acme Health", ["ACME HEALTH PTY LTD"], source="llm")
    assert _state(conn) == before


def test_role_strength_orders_roles():
    assert ledger.stronger_role("affected", "victim") == "victim"
    assert ledger.stronger_role("victim", "vendor") == "victim"
    assert ledger.stronger_role("bystander", "regulator") == "regulator"
    assert ledger.stronger_role(None, "mentioned") == "mentioned"
    assert ledger.stronger_role("victim", "something-new") == "victim"


# ----------------------------------------------------------------------
# A later decision overturns the fold
# ----------------------------------------------------------------------

def _set_verdict(conn, verdict):
    conn.execute("DELETE FROM EntityAliasDecisions")
    conn.execute("INSERT INTO EntityAliasDecisions VALUES ('ACME HEALTH PTY LTD', 'Acme Health', ?, NULL, 'now different', "
                 "'[]', 'sonar-pro', ?, '2026-10-30')", (verdict, eav.PROMPT_VERSION))


def test_a_model_fold_is_undone_when_its_verdict_changes(conn):
    before = _state(conn)
    merge_entities(conn, "Acme Health", ["ACME HEALTH PTY LTD"], source="llm")
    assert ledger.reverse_overturned_merges(conn) == ([], [])          # still 'same'
    _set_verdict(conn, "different")
    reports, refused = ledger.reverse_overturned_merges(conn, dry_run=True)
    assert len(reports) == 1 and refused == [] and ledger.list_merges(conn)[0]["status"] == "applied"
    reports, refused = ledger.reverse_overturned_merges(conn)
    assert len(reports) == 1 and refused == []
    assert _state(conn) == before
    assert ledger.reverse_overturned_merges(conn) == ([], [])          # idempotent


def test_a_unsure_or_a_changed_verdict_is_handled_like_a_not_same(conn):
    merge_entities(conn, "Acme Health", ["ACME HEALTH PTY LTD"], source="llm")
    _set_verdict(conn, "parent_subsidiary")
    (overturned,) = ledger.overturned_merges(conn)
    assert overturned["verdict"] == "parent_subsidiary"


def test_a_reviewed_fold_is_never_reversed_by_a_model(conn):
    merge_entities(conn, "Acme Health", ["ACME HEALTH PTY LTD"], source="human")
    _set_verdict(conn, "different")
    assert ledger.reverse_overturned_merges(conn) == ([], [])
    assert conn.execute("SELECT COUNT(*) FROM EntitiesV2 WHERE entity_id = 2").fetchone()[0] == 0


def test_an_overturned_fold_that_cannot_be_undone_is_reported_not_raised(conn):
    merge_entities(conn, "Acme Health", ["ACME HEALTH PTY LTD"], source="llm")
    _set_verdict(conn, "different")
    conn.execute("INSERT INTO EntitiesV2 (entity_name) VALUES ('ACME HEALTH PTY LTD')")
    reports, refused = ledger.reverse_overturned_merges(conn)
    assert reports == [] and len(refused) == 1 and "is now entity" in refused[0][1]


def test_the_command_lists_and_unmerges(conn, tmp_path, capsys):
    import argparse

    from scripts import dedup_admin

    merge_entities(conn, "Acme Health", ["ACME HEALTH PTY LTD"], source="llm")
    conn.commit()
    (entry,) = ledger.list_merges(conn)
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    assert dedup_admin.cmd_entity_merges(argparse.Namespace(db=path, status="applied")) == 0
    assert "ACME HEALTH PTY LTD" in capsys.readouterr().out
    args = argparse.Namespace(db=path, merge_id=entry["merge_id"], reason="wrong", keep_alias=False, dry_run=False)
    assert dedup_admin.cmd_unmerge_entity(args) == 0
    assert dedup_admin.cmd_unmerge_entity(args) == 1                     # already reversed
    check = sqlite3.connect(path)
    assert check.execute("SELECT entity_name FROM EntitiesV2 WHERE entity_id = 2").fetchone() is not None
