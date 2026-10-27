"""Enum members are stored by value; legacy qualified names are repaired."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from cyber_data_collector.models.events import CyberEventType, EntityType, EventSeverity
from cyber_data_collector.storage.cyber_event_data_v2 import CyberEventDataV2
from cyber_data_collector.storage.schema_init import initialize_database
from cyber_data_collector.utils.enum_values import (
    canonical_enum_value, industry_from_entity_type, legacy_spellings, normalise_stored_enums,
)
from cyber_data_collector.utils.validation import safe_str, validate_enrichment_data_for_storage

ROOT = Path(__file__).resolve().parents[2]


def _integrity_module():
    spec = importlib.util.spec_from_file_location("_check_integrity", ROOT / "scripts" / "check_data_integrity.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module   # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = tmp_path / "instance" / "t.db"
    initialize_database(str(path))
    return path


def test_members_stringify_to_their_value():
    assert str(EventSeverity.MEDIUM) == "Medium"
    assert f"{CyberEventType.DATA_BREACH}" == "Data Breach"
    assert f"{EntityType.FINANCIAL:>23}" == "  Financial Institution"
    assert EventSeverity("High") is EventSeverity.HIGH and EventSeverity.HIGH == "High"


@pytest.mark.parametrize("value, enum_cls, expected", [
    (EventSeverity.MEDIUM, None, "Medium"),
    ("EventSeverity.MEDIUM", None, "Medium"),
    ("EventSeverity.UNKNOW", None, "Unknown"),        # truncated by a VARCHAR(20) validator
    ("EventSeverity.CRITIC", None, "Critical"),
    ("CyberEventType.DATA_BREACH", CyberEventType, "Data Breach"),
    ("EntityType.NON_PROFIT", EntityType, "Non-Profit"),
    ("OTHER", EntityType, "Other"),                   # bare member name, only with the class given
    ("OTHER", None, "OTHER"),
    ("Medium", EventSeverity, "Medium"),
    ("EventSeverity.MEDIUM", CyberEventType, "EventSeverity.MEDIUM"),  # wrong class: untouched
    ("Organization", EntityType, "Organization"),
    (None, EventSeverity, None),
])
def test_canonical_enum_value(value, enum_cls, expected):
    assert canonical_enum_value(value, enum_cls) == expected


def test_safe_str_and_validation_store_values():
    assert safe_str(EventSeverity.UNKNOWN, max_length=20) == "Unknown"
    out = validate_enrichment_data_for_storage({"title": "t", "event_type": "CyberEventType.RANSOMWARE",
                               "severity": EventSeverity.HIGH})
    assert (out["event_type"], out["severity"]) == ("Ransomware", "High")


def test_industry_from_entity_type_unchanged_by_respelling():
    for member in EntityType:
        assert industry_from_entity_type(f"EntityType.{member.name}") == industry_from_entity_type(member.value)
    assert industry_from_entity_type("Financial Institution") == "Financial"
    assert industry_from_entity_type("Non-Profit") is None
    assert industry_from_entity_type(None) is None


def test_storage_write_paths_store_values(db):
    store = CyberEventDataV2(db)
    try:
        raw_id = store.add_raw_event("GoogleSearch", {
            "title": "t", "metadata": {"severity": "EventSeverity.HIGH", "event_type": CyberEventType.PHISHING}})
        # Re-enrichment copies event_type/severity from stored source_metadata.
        enriched_id = store.create_enriched_event(raw_id, {
            "title": "t", "event_type": "CyberEventType.OTHER", "severity": "EventSeverity.MEDIUM",
            "entities": [{"name": "Acme", "type": "EntityType.RETAIL"}, {"name": "Bob", "type": EntityType.INDIVIDUAL}],
        })
        conn = store._conn
        meta = json.loads(conn.execute("SELECT source_metadata FROM RawEvents WHERE raw_event_id = ?",
                                       (raw_id,)).fetchone()[0])
        assert (meta["severity"], meta["event_type"]) == ("High", "Phishing")
        row = conn.execute("SELECT event_type, severity FROM EnrichedEvents WHERE enriched_event_id = ?",
                           (enriched_id,)).fetchone()
        assert tuple(row) == ("Other", "Medium")
        types = dict(conn.execute("SELECT entity_name, entity_type FROM EntitiesV2").fetchall())
        assert types == {"Acme": "Retail", "Bob": "Individual"}
    finally:
        store.close()


def _seed_legacy(path):
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO RawEvents (raw_event_id, source_type, source_metadata) VALUES "
                 "('r1', 'Perplexity', ?)", (json.dumps({"severity": "EventSeverity.MEDIUM",
                                                         "event_type": "CyberEventType.OTHER", "x": 1}),))
    conn.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, event_type, severity, status, "
                 "is_australian_event, is_specific_event) "
                 "VALUES ('e1', 'r1', 't', 'CyberEventType.OTHER', 'EventSeverity.MEDIUM', 'Active', 1, 1)")
    conn.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, master_enriched_event_id, title, "
                 "event_type, severity, status, is_australian_event, is_specific_event) VALUES ('d1', 'e1', 't', "
                 "'CyberEventType.RANSOMWARE', 'EventSeverity.UNKNOW', 'Active', 1, 1)")
    conn.execute("INSERT INTO EntitiesV2 (entity_name, entity_type) VALUES ('Acme', 'EntityType.FINANCIAL')")
    conn.commit()
    return conn


def test_normaliser_repairs_every_location_idempotently(db):
    conn = _seed_legacy(db)
    from cyber_data_collector.dedup.record_fingerprints import refresh_changed_events

    refresh_changed_events(conn)   # no map rows: nothing placed, nothing remembered
    assert set(legacy_spellings(conn)) == {
        "EnrichedEvents.event_type", "EnrichedEvents.severity", "DeduplicatedEvents.event_type",
        "DeduplicatedEvents.severity", "EntitiesV2.entity_type",
        "RawEvents.source_metadata.severity", "RawEvents.source_metadata.event_type"}

    dry = normalise_stored_enums(conn, dry_run=True)
    assert dry["RawEvents.source_metadata"] == 1 and legacy_spellings(conn)   # nothing written
    changed = normalise_stored_enums(conn)
    assert changed == dry
    assert legacy_spellings(conn) == {}
    assert normalise_stored_enums(conn) == {}                                 # idempotent
    assert conn.execute("SELECT severity FROM DeduplicatedEvents").fetchone()[0] == "Unknown"
    assert conn.execute("SELECT entity_type FROM EntitiesV2").fetchone()[0] == "Financial Institution"
    meta = json.loads(conn.execute("SELECT source_metadata FROM RawEvents").fetchone()[0])
    assert meta == {"severity": "Medium", "event_type": "Other", "x": 1}

    checks = _integrity_module()
    for check in (checks.check_severity_no_truncated_or_qualified, checks.check_event_type_no_truncated_or_qualified,
                  checks.check_no_legacy_enum_spellings_upstream, checks.check_severity_in_known_set):
        assert check(conn).ok, check.__name__
    conn.close()


def test_integrity_check_fails_on_upstream_legacy_spellings(db):
    conn = _seed_legacy(db)
    result = _integrity_module().check_no_legacy_enum_spellings_upstream(conn)
    assert not result.ok and "normalise-enums" in result.summary
    assert any("RawEvents.source_metadata" in d for d in result.details)
    conn.close()


def test_normaliser_keeps_record_fingerprints_unchanged(db):
    """Re-spelling a value is not a content change for the record refresh."""
    from cyber_data_collector.dedup.record_fingerprints import refresh_changed_events

    conn = _seed_legacy(db)
    conn.execute("INSERT INTO EventDeduplicationMap (map_id, raw_event_id, enriched_event_id, deduplicated_event_id) "
                 "VALUES ('m1', 'r1', 'e1', 'd1')")
    conn.commit()
    assert refresh_changed_events(conn).baselined == 1
    conn.commit()
    normalise_stored_enums(conn)
    report = refresh_changed_events(conn, dry_run=True)
    assert report.changed_records == 0
    conn.close()


def test_dedup_admin_normalise_enums_command(db, capsys):
    from scripts import dedup_admin

    _seed_legacy(db).close()
    assert dedup_admin.main(["--db", str(db), "normalise-enums", "--dry-run"]) == 0
    assert "Would rewrite 1 row(s) in DeduplicatedEvents.severity" in capsys.readouterr().out
    assert dedup_admin.main(["--db", str(db), "normalise-enums"]) == 0
    assert dedup_admin.main(["--db", str(db), "normalise-enums"]) == 0
    assert "No legacy enum spellings stored." in capsys.readouterr().out
