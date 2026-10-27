"""Clean-install smoke test: an empty database is initialised by the committed
schema, takes in mocked source responses, deduplicates them and reports -
with no API key and nothing pre-existing."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cyber_data_collector.storage.cyber_event_data_v2 import CyberEventDataV2
from cyber_data_collector.storage.schema_init import initialize_database

ROOT = Path(__file__).resolve().parents[2]

# What data sources hand to storage: two reports of one incident, one of another.
SOURCE_RESPONSES = [
    ("Perplexity", {"title": "Acme Health ransomware attack", "description": "Acme Health hit by ransomware",
                    "content": "Acme Health confirmed a ransomware attack on 12 March 2024 affecting 5,000 patients.",
                    "event_date": "2024-03-12", "source_url": "https://news.example/acme-1"},
     {"victim": "Acme Health"}),
    ("GoogleSearch", {"title": "Acme Health ransomware attack", "description": "Ransomware at Acme Health",
                      "content": "Acme Health says ransomware encrypted systems on 12 March 2024.",
                      "event_date": "2024-03-12", "source_url": "https://news.example/acme-2"},
     {"victim": "Acme Health"}),
    ("OAIC", {"title": "Borealis Bank data breach", "description": "Borealis Bank customer data exposed",
              "content": "Borealis Bank reported a data breach on 3 July 2025 exposing 1,200 customers.",
              "event_date": "2025-07-03", "source_url": "https://news.example/borealis"},
     {"victim": "Borealis Bank"}),
]


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = tmp_path / "instance" / "fresh.db"
    report = initialize_database(str(path))
    assert report.applied == ["0001_base_schema", "0002_reference_data"]
    return path


def test_init_is_idempotent(db):
    report = initialize_database(str(db))
    assert not report.applied and not report.changed_since_applied


def test_storage_refuses_an_uninitialised_database_with_the_fix(tmp_path):
    with pytest.raises(RuntimeError, match="pipeline.py init"):
        CyberEventDataV2(tmp_path / "empty.db")


def test_mocked_sources_flow_through_to_events_and_status(db, capsys):
    store = CyberEventDataV2(db)
    try:
        for source, raw, extra in SOURCE_RESPONSES:
            raw_id = store.add_raw_event(source, raw)
            store.create_enriched_event(raw_id, {
                "title": raw["title"], "description": raw["description"], "summary": raw["description"],
                "event_type": "Ransomware" if "ransomware" in raw["title"].lower() else "Data Breach",
                "severity": "High", "event_date": raw["event_date"], "records_affected": 5000,
                "is_australian_event": True, "is_specific_event": True, "confidence_score": 0.9,
                "australian_relevance_score": 0.9,
                "entities": [{"name": extra["victim"], "entity_type": "Company", "relationship_type": "victim"}],
            })
    finally:
        store.close()

    from scripts.run_global_deduplication import DeduplicationMigration
    assert DeduplicationMigration(str(db)).run_migration()

    conn = sqlite3.connect(db)
    try:
        active = conn.execute("SELECT COUNT(*) FROM DeduplicatedEvents WHERE status = 'Active'").fetchone()[0]
        mapped = conn.execute("SELECT COUNT(DISTINCT enriched_event_id) FROM EventDeduplicationMap").fetchone()[0]
    finally:
        conn.close()
    assert active == 2            # the two Acme reports (same title, same day) are one incident
    assert mapped == 3            # and every record belongs to an event

    from scripts.project_status import report_status
    assert report_status(str(db)) == 0
    assert "2025-07-03" in capsys.readouterr().out


def test_environment_template_lists_every_required_key():
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("OPENAI_API_KEY", "PERPLEXITY_API_KEY", "TYPESAFE_API_KEY", "DATABASE_URL"):
        assert f"{key}=" in template
