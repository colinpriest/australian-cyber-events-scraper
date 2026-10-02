"""Tests for preserving curated dedup state across refreshes.

Covers the 2026-09-15 incident: the discovery pipeline cleared and rebuilt
DeduplicatedEvents on every refresh, erasing the dedup v3 repairs and orphaning
every ASD classification. These tests pin the fix (discovery no longer
deduplicates) and the recovery command (restore the dedup tables from a backup).
"""
from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from cyber_data_collector.dedup.state_restore import (
    DEDUP_STATE_TABLES,
    restore_dedup_state,
)

_SCHEMA = """
CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, status TEXT);
CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY,
    master_enriched_event_id TEXT, title TEXT, status TEXT);
CREATE TABLE DeduplicationClusters (cluster_id TEXT PRIMARY KEY,
    deduplicated_event_id TEXT REFERENCES DeduplicatedEvents(deduplicated_event_id));
CREATE TABLE DeduplicatedEventSources (source_id TEXT PRIMARY KEY,
    deduplicated_event_id TEXT REFERENCES DeduplicatedEvents(deduplicated_event_id));
CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT
    REFERENCES DeduplicatedEvents(deduplicated_event_id), entity_id INTEGER);
CREATE TABLE EventDeduplicationMap (map_id TEXT PRIMARY KEY, enriched_event_id TEXT,
    deduplicated_event_id TEXT REFERENCES DeduplicatedEvents(deduplicated_event_id));
CREATE TABLE ASDRiskClassifications (classification_id TEXT PRIMARY KEY,
    deduplicated_event_id TEXT REFERENCES DeduplicatedEvents(deduplicated_event_id),
    severity_category TEXT);
"""


def _make_db(path: Path, dedup_ids, enriched_ids) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    for eid in enriched_ids:
        conn.execute("INSERT INTO EnrichedEvents VALUES (?, 'Active')", (eid,))
    for i, did in enumerate(dedup_ids):
        eid = enriched_ids[i]
        conn.execute("INSERT INTO DeduplicatedEvents VALUES (?, ?, ?, 'Active')",
                     (did, eid, f"title {did}"))
        conn.execute("INSERT INTO DeduplicationClusters VALUES (?, ?)", (f"c-{did}", did))
        conn.execute("INSERT INTO DeduplicatedEventSources VALUES (?, ?)", (f"s-{did}", did))
        conn.execute("INSERT INTO DeduplicatedEventEntities VALUES (?, ?)", (did, i))
        conn.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?, ?)",
                     (f"m-{did}", eid, did))
        conn.execute("INSERT INTO ASDRiskClassifications VALUES (?, ?, 'C4')",
                     (f"a-{did}", did))
    conn.commit()
    conn.close()


@pytest.fixture
def live_and_backup(tmp_path):
    """Backup: curated ids for e1-e2. Live: rebuilt ids, plus a new event e3."""
    backup = tmp_path / "backup.db"
    live = tmp_path / "live.db"
    _make_db(backup, ["curated-1", "curated-2"], ["e1", "e2"])
    _make_db(live, ["rebuilt-1", "rebuilt-2", "rebuilt-3"], ["e1", "e2", "e3"])
    conn = sqlite3.connect(live)
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn, backup
    conn.close()


def _ids(conn, table="DeduplicatedEvents", col="deduplicated_event_id"):
    return sorted(r[0] for r in conn.execute(f"SELECT {col} FROM {table}"))


def test_dry_run_reports_changes_without_writing(live_and_backup):
    conn, backup = live_and_backup
    report = restore_dedup_state(conn, backup, dry_run=True)

    assert report["changed"] is True
    assert report["applied"] is False
    assert report["tables"]["DeduplicatedEvents"] == {
        "live": 3, "backup": 2, "differing_rows": 5}
    assert _ids(conn) == ["rebuilt-1", "rebuilt-2", "rebuilt-3"]


def test_apply_restores_every_dedup_table_and_keeps_upstream(live_and_backup):
    conn, backup = live_and_backup
    report = restore_dedup_state(conn, backup, dry_run=False)

    assert report["applied"] is True
    for table in DEDUP_STATE_TABLES:
        assert report["tables"][table]["differing_rows"] > 0
    assert _ids(conn) == ["curated-1", "curated-2"]
    assert _ids(conn, "ASDRiskClassifications", "classification_id") == [
        "a-curated-1", "a-curated-2"]
    # The enriched record added after the backup is kept, and is left for the
    # next incremental dedup run to merge.
    assert _ids(conn, "EnrichedEvents", "enriched_event_id") == ["e1", "e2", "e3"]
    assert report["pending_incremental"] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_second_run_is_a_no_op(live_and_backup):
    conn, backup = live_and_backup
    restore_dedup_state(conn, backup, dry_run=False)
    again = restore_dedup_state(conn, backup, dry_run=False)

    assert again["changed"] is False
    assert again["applied"] is False
    assert all(s["differing_rows"] == 0 for s in again["tables"].values())


def test_refuses_when_column_layout_differs(live_and_backup, tmp_path):
    conn, backup = live_and_backup
    other = sqlite3.connect(backup)
    other.execute("ALTER TABLE ASDRiskClassifications ADD COLUMN extra TEXT")
    other.commit()
    other.close()

    with pytest.raises(ValueError, match="ASDRiskClassifications"):
        restore_dedup_state(conn, backup, dry_run=False)
    assert _ids(conn) == ["rebuilt-1", "rebuilt-2", "rebuilt-3"]


def test_refuses_missing_backup(live_and_backup, tmp_path):
    conn, _ = live_and_backup
    with pytest.raises(FileNotFoundError):
        restore_dedup_state(conn, tmp_path / "nope.db")


def test_discovery_pipeline_cannot_rebuild_dedup_state():
    """Regression: discovery wiped DeduplicatedEvents on every refresh.

    Deduplication belongs to run_full_pipeline's incremental phase only.
    """
    from cyber_data_collector.pipelines import discovery

    assert not hasattr(discovery.EventDiscoveryEnrichmentPipeline, "run_global_deduplication")
    source = inspect.getsource(discovery)
    assert "clear_existing_deduplications" not in source
    assert "DeduplicationStorage" not in source


def test_pipeline_refresh_args_cover_every_dedup_phase_option():
    """pipeline.py once omitted these, so post-dedup steps failed silently."""
    import pipeline

    args = pipeline._build_pipeline_args(
        db_path="x.db", sources=["OAIC"], max_events=1, days=1,
        out_dir="dashboard", skip_classification=False,
    )
    assert args.force_dedup is False
    for name in ("skip_recurrence_check", "recurrence_window",
                 "recurrence_min_certainty", "skip_entity_sizing",
                 "entity_size_limit", "skip_missed_merge_check", "missed_merge_apply",
                 "missed_merge_days", "missed_merge_min_certainty"):
        assert hasattr(args, name), name


def test_incremental_dedup_backfills_entity_links():
    """Regression: incremental dedup never wrote DeduplicatedEventEntities.

    The removed full rebuild used to repopulate it, so after the fix every new
    event was missing from the entity dashboard and recurrence analysis.
    """
    from scripts import run_global_deduplication as rgd

    source = inspect.getsource(rgd.DeduplicationMigration._run_incremental_deduplication)
    assert "run_backfill(conn)" in source


def test_refresh_runs_missed_merge_check_before_recurrence_check():
    import run_full_pipeline

    source = inspect.getsource(run_full_pipeline.UnifiedPipeline.run_deduplication_phase)
    assert source.index("run_missed_merge_check") < source.index("run_recurrence_check")


def test_missed_merge_check_is_report_only_by_default():
    """Auto-merging is opt-in until adjudication precision is fixed."""
    import pipeline
    import run_full_pipeline

    args = pipeline._build_pipeline_args(db_path="x.db", sources=["OAIC"], max_events=1,
                                         days=1, out_dir="d", skip_classification=False)
    assert args.missed_merge_apply is False
    source = inspect.getsource(run_full_pipeline.UnifiedPipeline.run_missed_merge_check)
    assert "missed_merge_apply" in source and '"--dry-run"' in source
