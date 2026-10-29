"""History of what was known when, and the reconstruction behind ``--as-known-at``."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from cyber_data_collector.dedup import input_history as ih
from cyber_data_collector.publication import snapshot as snaps


def _db(path: Path | str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, master_enriched_event_id TEXT,
            title TEXT, status TEXT, event_date TEXT, event_date_precision TEXT, is_australian_event INTEGER,
            is_specific_event INTEGER, updated_at TEXT);
        CREATE TABLE EventDeduplicationMap (map_id TEXT PRIMARY KEY, raw_event_id TEXT, enriched_event_id TEXT,
            deduplicated_event_id TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER,
            relationship_type TEXT, confidence_score REAL, source_count INTEGER,
            PRIMARY KEY (deduplicated_event_id, entity_id));
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT NOT NULL, size_estimate TEXT,
            industry TEXT);
        INSERT INTO EntitiesV2 VALUES (1, 'Acme', 'SMALL', 'Retail'), (2, 'Beta', 'LARGE', 'Health');
        INSERT INTO DeduplicatedEvents VALUES ('e1', 'm1', 'Acme breach', 'Active', '2026-01-10', 'day', 1, 1, NULL);
        INSERT INTO EventDeduplicationMap VALUES ('p1', 'r1', 'm1', 'e1');
        INSERT INTO DeduplicatedEventEntities VALUES ('e1', 1, 'victim', 0.9, 1);
    """)
    conn.commit()
    return conn


def _q(conn, sql):
    return [tuple(r) for r in conn.execute(sql)]


def _rows(conn, table, **where):
    clause = " AND ".join(f"{k} = ?" for k in where) or "1"
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE {clause} ORDER BY history_id",
                                          tuple(where.values()))]


def _stamp_all(conn, when: str) -> None:
    """Pretend everything recorded so far was recorded at ``when`` (UTC)."""
    for src in ih.SOURCES:
        conn.execute(f"UPDATE {src.history} SET known_at = ?", (when,))
    conn.execute("UPDATE InputHistoryStatus SET tracking_since = ?, baselined_at = ?", (when, when))
    columns = {r["source"]: json.loads(r["columns_json"]) for r in conn.execute("SELECT * FROM InputHistoryStatus")}
    for source, cols in columns.items():
        conn.execute("UPDATE InputHistoryStatus SET columns_json = ? WHERE source = ?",
                     (json.dumps({c: when for c in cols}), source))
    conn.commit()


def _stamp_after(conn, table: str, history_id: int, when: str) -> None:
    conn.execute(f"UPDATE {table} SET known_at = ? WHERE history_id > ?", (when, history_id))
    conn.commit()


def _last_id(conn, table: str) -> int:
    return conn.execute(f"SELECT COALESCE(MAX(history_id), 0) FROM {table}").fetchone()[0]


# --- schema and baseline ------------------------------------------------------------------------------------------

def test_ensure_schema_baselines_current_state_and_is_idempotent():
    conn = _db()
    done = ih.ensure_schema(conn)
    assert all("triggers installed" in what and "baseline" in what for what in done.values())
    rows = _rows(conn, "EventStateHistory")
    assert [(r["change"], r["backfilled"], r["deduplicated_event_id"], r["event_date"]) for r in rows] == [
        ("baseline", 1, "e1", "2026-01-10")]
    assert len(_rows(conn, "EntityStateHistory")) == 2 and len(_rows(conn, "EventEntityLinkHistory")) == 1
    before = {s.history: _last_id(conn, s.history) for s in ih.SOURCES}
    assert ih.ensure_schema(conn) == {}                      # nothing pending: no work, no new rows
    assert before == {s.history: _last_id(conn, s.history) for s in ih.SOURCES}


def test_tables_without_the_watched_keys_are_left_alone():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE EntitiesV2 (name TEXT)")      # not the real table: no entity_id
    assert ih.ensure_schema(conn) == {}
    conn.execute("INSERT INTO EntitiesV2 VALUES ('x')")      # and writing to it still works
    assert ih.status(conn) == []


def test_missing_history_is_an_error_not_a_silent_current_state():
    conn = _db()
    with pytest.raises(ih.HistoryError, match="input-history"):
        ih.rewind(conn, "2026-01-01")


# --- the triggers ---------------------------------------------------------------------------------------------------

def test_every_writer_leaves_a_history_row_and_untracked_columns_do_not():
    conn = _db()
    ih.ensure_schema(conn)
    conn.execute("UPDATE DeduplicatedEvents SET updated_at = 'x'")            # not tracked
    conn.execute("UPDATE DeduplicatedEvents SET event_date = '2026-01-12'")
    conn.execute("INSERT INTO DeduplicatedEvents VALUES ('e2', 'm2', 'Beta', 'Active', '2026-02-01', NULL, 1, 1, NULL)")
    conn.execute("DELETE FROM EventDeduplicationMap WHERE map_id = 'p1'")
    conn.execute("UPDATE EntitiesV2 SET size_estimate = 'MEDIUM' WHERE entity_id = 1")
    conn.commit()
    changes = [(r["deduplicated_event_id"], r["change"], r["event_date"]) for r in _rows(conn, "EventStateHistory")]
    assert changes == [("e1", "baseline", "2026-01-10"), ("e1", "update", "2026-01-12"),
                       ("e2", "insert", "2026-02-01")]
    assert [r["change"] for r in _rows(conn, "EventMembershipHistory")] == ["baseline", "delete"]
    assert [(r["change"], r["size_estimate"]) for r in _rows(conn, "EntityStateHistory", entity_id=1)] == [
        ("baseline", "SMALL"), ("update", "MEDIUM")]


def test_moving_a_link_to_another_entity_is_a_delete_and_an_insert():
    conn = _db()
    ih.ensure_schema(conn)
    conn.execute("UPDATE DeduplicatedEventEntities SET entity_id = 2 WHERE entity_id = 1")
    conn.commit()
    rows = [(r["entity_id"], r["change"]) for r in _rows(conn, "EventEntityLinkHistory")]
    assert rows == [(1, "baseline"), (1, "delete"), (2, "insert")]


def test_a_row_that_existed_before_tracking_gets_its_old_state_recorded_when_first_changed():
    conn = _db()
    ih.ensure_schema(conn)
    conn.execute("DROP TRIGGER trg_inputhistory_EntitiesV2_insert")
    conn.execute("INSERT INTO EntitiesV2 VALUES (3, 'Gamma', 'HUGE', 'Energy')")       # unseen by the triggers
    conn.execute("UPDATE EntitiesV2 SET size_estimate = 'LARGE' WHERE entity_id = 3")
    conn.commit()
    rows = _rows(conn, "EntityStateHistory", entity_id=3)
    assert [(r["change"], r["backfilled"], r["size_estimate"]) for r in rows] == [("baseline", 1, "HUGE"),
                                                                                 ("update", 0, "LARGE")]


def test_a_column_added_later_is_tracked_from_then_and_earlier_rows_use_the_current_value():
    conn = _db()
    ih.ensure_schema(conn)
    _stamp_all(conn, "2026-01-01 00:00:00")
    conn.execute("ALTER TABLE DeduplicatedEvents ADD COLUMN event_type TEXT")
    conn.execute("UPDATE DeduplicatedEvents SET event_type = 'Ransomware'")
    conn.commit()
    done = ih.ensure_schema(conn)
    assert "event_type" in done["events"]
    status = json.loads(conn.execute("SELECT columns_json FROM InputHistoryStatus WHERE source='events'").fetchone()[0])
    assert status["event_type"] > status["title"]
    conn.execute("UPDATE DeduplicatedEvents SET event_type = 'Phishing'")
    conn.commit()
    report = ih.rewind(conn, "2026-01-02")
    state = conn.execute("SELECT event_type FROM DeduplicatedEvents").fetchone()[0]
    assert state == "Phishing"                       # not tracked at that date: the current value stands in ...
    assert next(s for s in report.sources if s.name == "events").fallback_columns == {"event_type": 1}   # ... and is reported
    assert "events" in report.to_dict()["sources"][0]["name"]


# --- reconstruction -------------------------------------------------------------------------------------------------

def _history_with_changes():
    """Tracking from 2026-01-01; an event is redated 2026-03-01, a second event, link and size arrive later."""
    conn = _db()
    ih.ensure_schema(conn)
    _stamp_all(conn, "2026-01-01 00:00:00")
    marks = {s.history: _last_id(conn, s.history) for s in ih.SOURCES}
    conn.execute("UPDATE DeduplicatedEvents SET event_date = '2026-01-20', event_date_precision = 'month' "
                 "WHERE deduplicated_event_id = 'e1'")
    conn.commit()
    _stamp_after(conn, "EventStateHistory", marks["EventStateHistory"], "2026-03-01 09:00:00")
    marks = {s.history: _last_id(conn, s.history) for s in ih.SOURCES}
    conn.execute("INSERT INTO DeduplicatedEvents VALUES ('e2', 'm2', 'Beta breach', 'Active', '2026-02-15', 'day', 1, 1, NULL)")
    conn.execute("INSERT INTO EventDeduplicationMap VALUES ('p2', 'r2', 'm2', 'e2')")
    conn.execute("INSERT INTO DeduplicatedEventEntities VALUES ('e2', 2, 'victim', 0.8, 1)")
    conn.execute("UPDATE DeduplicatedEventEntities SET relationship_type = 'bystander' WHERE deduplicated_event_id = 'e1'")
    conn.execute("UPDATE EntitiesV2 SET size_estimate = 'HUGE' WHERE entity_id = 1")
    conn.execute("DELETE FROM EntitiesV2 WHERE entity_id = 2")
    conn.commit()
    for src in ih.SOURCES:
        _stamp_after(conn, src.history, marks[src.history], "2026-05-01 12:00:00")
    return conn


def test_rewind_returns_each_decision_as_it_stood_at_the_date():
    conn = _history_with_changes()
    ih.rewind(conn, "2026-02-01")
    assert _q(conn, "SELECT event_date, event_date_precision FROM DeduplicatedEvents") == [
        ("2026-01-10", "day")]                                                 # before the redate
    assert [r[0] for r in conn.execute("SELECT deduplicated_event_id FROM DeduplicatedEvents")] == ["e1"]
    assert [r[0] for r in conn.execute("SELECT map_id FROM EventDeduplicationMap")] == ["p1"]
    assert _q(conn, "SELECT entity_id, relationship_type FROM DeduplicatedEventEntities") == [(1, "victim")]
    assert _q(conn, "SELECT entity_id, size_estimate FROM EntitiesV2 ORDER BY entity_id") == [
        (1, "SMALL"), (2, "LARGE")]                                            # entity 2 restored after being deleted


def test_rewind_between_changes_and_after_the_last_one():
    conn = _history_with_changes()
    report = ih.rewind(conn, "2026-03-15")
    assert conn.execute("SELECT event_date FROM DeduplicatedEvents WHERE deduplicated_event_id = 'e1'").fetchone()[0] == "2026-01-20"
    assert [r[0] for r in conn.execute("SELECT deduplicated_event_id FROM DeduplicatedEvents ORDER BY 1")] == ["e1"]
    assert all(s.mode == "history" and s.exact for s in report.sources)        # tracking began before the cutoff
    assert report.fallback_fields == []

    now = _history_with_changes()
    report = ih.rewind(now, "2026-06-01")                                      # everything known: no change at all
    assert sum(s.changed + s.removed + s.restored for s in report.sources) == 0


def test_a_date_means_the_end_of_that_utc_day_and_a_datetime_is_exact():
    conn = _history_with_changes()
    conn.execute("UPDATE EventStateHistory SET known_at = '2026-03-01 23:59:59' WHERE change = 'update'")
    conn.commit()
    copy = _history_with_changes()
    copy.execute("UPDATE EventStateHistory SET known_at = '2026-03-01 23:59:59' WHERE change = 'update'")
    copy.commit()
    ih.rewind(conn, "2026-03-01")
    assert conn.execute("SELECT event_date FROM DeduplicatedEvents WHERE deduplicated_event_id='e1'").fetchone()[0] == "2026-01-20"
    ih.rewind(copy, "2026-03-01 23:59:58")
    assert copy.execute("SELECT event_date FROM DeduplicatedEvents WHERE deduplicated_event_id='e1'").fetchone()[0] == "2026-01-10"
    assert ih.parse_as_known_at("2026-03-01") == ("2026-03-01 23:59:59", date(2026, 3, 1))
    assert ih.parse_as_known_at("2026-03-01T08:30") == ("2026-03-01 08:30:00", date(2026, 3, 1))
    with pytest.raises(ValueError):
        ih.parse_as_known_at("last june")


def test_a_cutoff_before_tracking_uses_the_earliest_recorded_state_and_says_so():
    conn = _history_with_changes()
    report = ih.rewind(conn, "2025-12-01")
    # Nothing recorded before 2026-01-01: baseline rows stand in (even for the event later redated) ...
    assert conn.execute("SELECT event_date FROM DeduplicatedEvents WHERE deduplicated_event_id = 'e1'").fetchone()[0] == "2026-01-10"
    # ... but e2 was genuinely inserted after tracking began, so it did not exist then.
    assert [r[0] for r in conn.execute("SELECT deduplicated_event_id FROM DeduplicatedEvents")] == ["e1"]
    assert set(report.fallback_fields) == {s.label for s in ih.SOURCES}
    text = report.markdown()
    assert "current state (backfilled)" in text and "Limitation" in text and "2026-01-01 00:00:00 UTC" in text
    assert all(not s.exact and s.fallback_keys >= 1 for s in report.sources)


def test_decision_ledgers_are_filtered_by_their_own_timestamps_and_not_before_their_first_row():
    conn = _db()
    ih.ensure_schema(conn)
    conn.execute("CREATE TABLE VictimAttributionActions (action_id INTEGER PRIMARY KEY, created_at TEXT)")
    conn.execute("CREATE TABLE ModelJudgements (judgement_id INTEGER PRIMARY KEY, created_at TEXT)")
    conn.executemany("INSERT INTO VictimAttributionActions (created_at) VALUES (?)",
                     [("2026-02-01T10:00:00.123456",), ("2026-04-01T10:00:00",)])
    conn.executemany("INSERT INTO ModelJudgements (created_at) VALUES (?)", [("2026-04-01 10:00:00",)])
    _stamp_all(conn, "2025-01-01 00:00:00")
    report = ih.rewind(conn, "2026-03-01")
    by_table = {l.table: l for l in report.ledgers}
    assert (by_table["VictimAttributionActions"].mode, by_table["VictimAttributionActions"].rows_dropped) == ("filtered", 1)
    assert conn.execute("SELECT COUNT(*) FROM VictimAttributionActions").fetchone()[0] == 1
    assert by_table["ModelJudgements"].mode.startswith("not filtered")             # its first row is after the cutoff
    assert conn.execute("SELECT COUNT(*) FROM ModelJudgements").fetchone()[0] == 1
    assert by_table["VictimFillActions"].mode == "absent"


# --- the analysis input ---------------------------------------------------------------------------------------------

def _live(tmp_path: Path) -> Path:
    path = tmp_path / "live.db"
    conn = _history_with_changes()
    conn.execute("CREATE TABLE RawEvents (raw_event_id TEXT, discovered_at TEXT)")
    conn.executemany("INSERT INTO RawEvents VALUES (?, ?)", [("r1", "2026-01-11"), ("r2", "2026-02-16")])
    conn.commit()
    disk = sqlite3.connect(path)
    conn.backup(disk)
    disk.close()
    conn.close()
    return path


def test_analysis_input_reconstructs_the_working_copy_and_records_how_to_reproduce_it(tmp_path):
    live = _live(tmp_path)
    store = snaps.SnapshotStore(tmp_path / "store")
    out = tmp_path / "out"
    with snaps.AnalysisInput.open(live, store=store) as inputs:
        report = inputs.reconstruct("2026-02-01")
        with sqlite3.connect(inputs.path) as copy:
            assert copy.execute("SELECT COUNT(*) FROM DeduplicatedEvents").fetchone()[0] == 1
        record = json.loads(inputs.record(out, analysis="t").read_text(encoding="utf-8"))
        with pytest.raises(snaps.SnapshotError):
            inputs.reconstruct("2026-03-01")                   # one reconstruction per input
    assert record["as_known_at"] == "2026-02-01" == report.as_known_at
    assert record["reproduce"] == f"--snapshot-id {record['snapshot_id']} --as-known-at 2026-02-01"
    assert record["history_reconstruction"]["reconstructed_from_current_state"] == []
    # The retained snapshot is the live database, not the reconstruction: id plus date reproduce it.
    with sqlite3.connect(live) as db:
        assert db.execute("SELECT COUNT(*) FROM DeduplicatedEvents").fetchone()[0] == 2
    restored = store.restore(record["snapshot_id"], tmp_path / "again")
    with sqlite3.connect(restored) as db:
        assert db.execute("SELECT COUNT(*) FROM DeduplicatedEvents").fetchone()[0] == 2


def test_reconstructing_a_database_without_history_fails_loudly(tmp_path):
    path = tmp_path / "plain.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY)")
    with snaps.AnalysisInput.open(path, store=snaps.SnapshotStore(tmp_path / "store")) as inputs:
        with pytest.raises(ih.HistoryError):
            inputs.reconstruct("2026-02-01")


def test_extended_runner_reads_the_reconstructed_inputs_and_names_the_fallbacks(tmp_path, monkeypatch):
    from scripts import analyze_recurrence_extensions as runner

    live = _live(tmp_path)
    seen = []

    def fake_load(path):
        with sqlite3.connect(path) as db:
            ids = [r[0] for r in db.execute("SELECT deduplicated_event_id FROM DeduplicatedEvents ORDER BY 1")]
        seen.append(ids)
        return pd.DataFrame({"deduplicated_event_id": ids, "event_date": [date(2026, 1, 10)] * len(ids),
                             "entity_id": range(len(ids))})

    monkeypatch.setattr(runner, "load_victim_events", fake_load)
    for module in (runner.timing_precision, runner.linkage_supplier, runner.sequence_bias):
        monkeypatch.setattr(module, "run", lambda events, snapshot, end, out_dir, seed, **kw: {"section": "## S"})
    out = tmp_path / "out"
    summary = runner.run_analysis(live, out, bootstrap=1, date_draws=1, as_known_at="2026-02-01")
    assert seen == [["e1"]]                                    # the second event was not yet known
    assert summary["censor_date"] == "2026-02-01"              # the cutoff defaults to the date
    assert summary["history_reconstruction"]["reconstructed_from_current_state"] == []
    report = (out / "extended_recurrence_report.md").read_text(encoding="utf-8")
    assert "## Inputs as known at 2026-02-01" in report and "reconstructed from the recorded history" in report
    inputs = json.loads((out / "analysis_inputs.json").read_text(encoding="utf-8"))
    assert "--as-known-at 2026-02-01" in inputs["reproduce"]

    with pytest.raises(ValueError, match="later than --as-known-at"):
        runner.run_analysis(live, tmp_path / "o2", date(2026, 3, 1), bootstrap=1, date_draws=1, as_known_at="2026-02-01")

    seen.clear()
    plain = runner.run_analysis(live, tmp_path / "o3", date(2026, 3, 1), bootstrap=1, date_draws=1)
    assert seen == [["e1", "e2"]] and plain["history_reconstruction"] is None
    assert "Rerun with `--as-known-at DATE`" in (tmp_path / "o3" / "extended_recurrence_report.md").read_text(encoding="utf-8")


# --- the frozen cohorts ---------------------------------------------------------------------------------------------

def test_frozen_cohorts_are_read_never_written_and_compared_only_at_their_own_date(tmp_path):
    from scripts.recurrence_extended import history_reconstruction as hr

    cohort = [{"entity_id": "1", "anchor_date": "2026-01-10", "anchor_ids": ["e1"]},
              {"entity_id": "9", "anchor_date": "2025-05-05", "anchor_ids": ["z"]}]
    registration = tmp_path / "timing_peak_registration.json"
    registration.write_text(json.dumps({"version": 1, "registered_on": "2026-02-01", "snapshot_cutoff": "2026-02-01",
                                        "cohort": cohort}), encoding="utf-8")
    before = registration.read_bytes()
    events = pd.DataFrame({"entity_id": [1, 1, 2], "deduplicated_event_id": ["e0", "e1", "e2"],
                           "event_date": [date(2026, 1, 1), date(2026, 1, 10), date(2026, 1, 15)],
                           "discovery_date_known": [True, True, False]})
    check = hr.registration_check([tmp_path / 'elsewhere', tmp_path], events, date(2026, 2, 1))["timing_peak_registration.json"]
    assert (check["status"], check["in_both"], check["same_anchor_events"], check["only_in_frozen"],
            check["only_in_reconstruction"]) == ("compared", 1, 1, 1, 0)           # entity 2 has no known discovery date
    other = hr.registration_check([tmp_path / 'elsewhere', tmp_path], events, date(2026, 6, 30))["timing_peak_registration.json"]
    assert other["status"] == "not applicable"
    assert registration.read_bytes() == before
    assert "never written" in hr.registration_markdown({"timing_peak_registration.json": check})
