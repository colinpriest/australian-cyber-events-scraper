"""History of what was known when, and the reconstruction behind ``--as-known-at``."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from cyber_data_collector.dedup import input_history as ih
from cyber_data_collector.publication import snapshot as snaps
from cyber_data_collector.utils import timestamps as ts


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


def test_a_chain_of_moves_is_undone_although_its_steps_break_a_unique_index():
    conn = _db()
    conn.execute("CREATE UNIQUE INDEX idx_map_once ON EventDeduplicationMap(raw_event_id, deduplicated_event_id)")
    conn.execute("INSERT INTO EventDeduplicationMap VALUES ('p2', 'r1', 'm2', 'e2')")
    conn.commit()
    ih.ensure_schema(conn)
    _stamp_all(conn, "2026-01-01 00:00:00")
    conn.execute("UPDATE EventDeduplicationMap SET deduplicated_event_id = 'e3' WHERE map_id = 'p1'")
    conn.execute("UPDATE EventDeduplicationMap SET deduplicated_event_id = 'e1' WHERE map_id = 'p2'")
    conn.commit()
    ih.rewind(conn, "2026-01-02")                 # moving p1 back to e1 first would collide with p2
    assert _q(conn, "SELECT map_id, enriched_event_id, deduplicated_event_id FROM EventDeduplicationMap ORDER BY 1") == [
        ("p1", "m1", "e1"), ("p2", "m2", "e2")]


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


def _unknown(conn):
    return [(r["source"], r["table_name"], None if r["key_json"] is None else json.loads(r["key_json"]),
             None if r["columns_json"] is None else json.loads(r["columns_json"]), r["reason"])
            for r in conn.execute("SELECT * FROM InputHistoryUnknown ORDER BY unknown_id")]


def test_a_cutoff_before_tracking_marks_the_stand_in_state_unknown():
    conn = _history_with_changes()
    report = ih.rewind(conn, "2025-12-01")
    # Nothing recorded before 2026-01-01: the baseline state stands in (even for the event later redated) ...
    assert conn.execute("SELECT event_date FROM DeduplicatedEvents WHERE deduplicated_event_id = 'e1'").fetchone()[0] == "2026-01-10"
    # ... but e2 was genuinely inserted after tracking began, so it did not exist then.
    assert [r[0] for r in conn.execute("SELECT deduplicated_event_id FROM DeduplicatedEvents")] == ["e1"]
    assert set(report.fallback_fields) == {s.label for s in ih.SOURCES}
    # The stand-ins are not passed off as known: every one is listed in the reconstructed copy.
    unknown = _unknown(conn)
    assert ("events", "DeduplicatedEvents", {"deduplicated_event_id": "e1"}, None, ih.REASON_BEFORE_FIRST_RECORD) in unknown
    assert ("links", "DeduplicatedEventEntities", {"deduplicated_event_id": "e1", "entity_id": 1}, None,
            ih.REASON_BEFORE_FIRST_RECORD) in unknown
    assert {(u[1], json.dumps(u[2], sort_keys=True)) for u in unknown if u[0] != "ledger"} == {
        ("DeduplicatedEvents", '{"deduplicated_event_id": "e1"}'), ("EventDeduplicationMap", '{"map_id": "p1"}'),
        ("DeduplicatedEventEntities", '{"deduplicated_event_id": "e1", "entity_id": 1}'),
        ("EntitiesV2", '{"entity_id": 1}'), ("EntitiesV2", '{"entity_id": 2}')}
    assert report.unknown["EntitiesV2"] == {ih.REASON_BEFORE_FIRST_RECORD: 2} and report.unknown_total == 5
    assert ih.unknown_victim_rows(conn, [(1, "e1"), (2, "e9")]) == {
        (1, "e1"): ["incident state", "victim link", "organisation state", "incident membership"],
        (2, "e9"): ["organisation state"]}
    text = report.markdown()
    assert "partly unknown" in text and "Limitation" in text and "2026-01-01 00:00:00 UTC" in text
    assert "Unknown at the cutoff: 5 row(s)" in text and "InputHistoryUnknown" in text
    assert all(not s.exact and s.fallback_keys >= 1 and s.unknown_rows == s.fallback_keys for s in report.sources)
    assert report.to_dict()["unknown_at_cutoff"]["DeduplicatedEvents"] == {ih.REASON_BEFORE_FIRST_RECORD: 1}


def test_a_cutoff_inside_the_history_leaves_nothing_unknown_and_plain_copies_have_nothing_unknown():
    conn = _history_with_changes()
    assert ih.unknown_victim_rows(conn, [(1, "e1")]) == {} and ih.unknown_summary(conn) == {}   # not a reconstruction
    report = ih.rewind(conn, "2026-02-01")
    assert _unknown(conn) == [] and report.unknown == {} and ih.is_reconstruction(conn)
    assert ih.unknown_victim_rows(conn, [(1, "e1")]) == {}


def test_rows_never_recorded_and_columns_tracked_later_are_recorded_as_unknown():
    conn = _db()
    ih.ensure_schema(conn)
    _stamp_all(conn, "2026-01-01 00:00:00")
    conn.execute("DROP TRIGGER trg_inputhistory_EntitiesV2_insert")
    conn.execute("INSERT INTO EntitiesV2 VALUES (3, 'Gamma', 'HUGE', 'Energy')")        # never seen by the triggers
    conn.execute("ALTER TABLE DeduplicatedEvents ADD COLUMN event_type TEXT")
    conn.execute("UPDATE DeduplicatedEvents SET event_type = 'Ransomware'")
    conn.commit()
    ih.ensure_schema(conn)
    report = ih.rewind(conn, "2026-01-02")
    unknown = _unknown(conn)
    assert ("entities", "EntitiesV2", {"entity_id": 3}, None, ih.REASON_NEVER_RECORDED) in unknown
    assert ("events", "DeduplicatedEvents", {"deduplicated_event_id": "e1"}, ["event_type"], ih.REASON_COLUMN_LATER) in unknown
    entities = next(s for s in report.sources if s.name == "entities")
    assert (entities.untracked_keys, entities.unknown_rows, entities.exact) == (1, 1, False)
    assert ih.unknown_victim_rows(conn, [(3, "e1")])[(3, "e1")] == ["incident state", "organisation state"]


# --- decision ledgers: always filtered, compared in UTC -------------------------------------------------------------

BRISBANE = timezone(timedelta(hours=10))


def _ledgers(conn, rows):
    """``{table: [stamp, ...]}`` into minimal ledger tables (``rowid`` keys)."""
    for table, stamps in rows.items():
        column = ih.TIMESTAMPED_LEDGERS[table]
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY, {column} TEXT)")
        conn.executemany(f"INSERT INTO {table} ({column}) VALUES (?)", [(s,) for s in stamps])
    conn.commit()


def _left(conn, table):
    return [r[0] for r in conn.execute(f"SELECT {ih.TIMESTAMPED_LEDGERS[table]} FROM {table} ORDER BY id")]


def test_ledger_rows_dated_after_the_cutoff_are_dropped_even_before_its_first_row():
    conn = _db()
    ih.ensure_schema(conn)
    _ledgers(conn, {"VictimAttributionActions": ["2026-02-01T10:00:00.123456", "2026-04-01T10:00:00"],
                    "ModelJudgements": ["2026-04-01T10:00:00", "2026-05-01T10:00:00"]})
    _stamp_all(conn, "2025-01-01 00:00:00")
    report = ih.rewind(conn, "2026-03-01", local_zone=BRISBANE)
    by_table = {l.table: l for l in report.ledgers}
    assert (by_table["VictimAttributionActions"].mode, by_table["VictimAttributionActions"].rows_dropped) == ("filtered", 1)
    assert _left(conn, "VictimAttributionActions") == ["2026-02-01T10:00:00.123456"]
    # The cutoff is before ModelJudgements' first row: every row is a decision made later, so none is kept, and
    # what the ledger held at the cutoff is recorded as unknown rather than taken from the present.
    judgements = by_table["ModelJudgements"]
    assert judgements.mode.startswith("filtered; cutoff before the first row") and judgements.cutoff_before_first_row
    assert (judgements.rows_dropped, judgements.rows_kept, judgements.first_row) == (2, 0, "2026-04-01 00:00:00")
    assert _left(conn, "ModelJudgements") == []
    assert ("ledger", "ModelJudgements", None, None, ih.REASON_LEDGER_BEFORE_FIRST_ROW) in _unknown(conn)
    assert by_table["VictimFillActions"].mode == "absent"
    assert "first row 2026-04-01 00:00:00 UTC" in report.markdown()


def test_naive_local_naive_utc_and_offset_stamps_are_compared_in_utc():
    conn = _db()
    ih.ensure_schema(conn)
    _stamp_all(conn, "2025-01-01 00:00:00")
    _ledgers(conn, {
        # datetime.now() writers: a naive stamp is Brisbane time, UTC+10.
        "ModelJudgements": ["2026-03-01T14:00:00.5",            # 04:00 UTC: known
                            "2026-03-01T16:00:00",               # 06:00 UTC: not yet (a UTC reading would keep it)
                            "2026-03-01T14:30:00+00:00",         # explicit UTC: not yet (a local reading would keep it)
                            "2026-03-01T15:00:00+10:00",         # 05:00 UTC exactly: known
                            "2026-03-01T04:59:59Z"],             # known
        # CURRENT_TIMESTAMP writers: a naive stamp is UTC; the ledger replaces rows in place.
        "DedupOverrides": ["2026-03-01 05:00:00", "2026-03-01 05:00:01", "2026-03-01T06:00:00+02:00"],
        "VictimFillActions": ["not a time", None],
    })
    report = ih.rewind(conn, "2026-03-01T15:00+10:00", local_zone=BRISBANE)      # = 05:00 UTC
    assert report.cutoff == "2026-03-01 05:00:00"
    assert _left(conn, "ModelJudgements") == ["2026-03-01T14:00:00.5", "2026-03-01T15:00:00+10:00",
                                              "2026-03-01T04:59:59Z"]
    assert _left(conn, "DedupOverrides") == ["2026-03-01 05:00:00", "2026-03-01T06:00:00+02:00"]   # 04:00 UTC
    by_table = {l.table: l for l in report.ledgers}
    assert (by_table["ModelJudgements"].naive, by_table["ModelJudgements"].rows_with_offset) == ("local", 3)
    # A later ruling in a ledger that replaces rows may have replaced one that held at the cutoff: unknown.
    assert by_table["DedupOverrides"].rows_replaced_unknown == 1
    unknown = _unknown(conn)
    assert ("ledger", "DedupOverrides", {"id": 2}, None, ih.REASON_REPLACED) in unknown
    # Stamps that cannot be read are not passed off as known: dropped and recorded.
    assert _left(conn, "VictimFillActions") == [] and by_table["VictimFillActions"].rows_unknown == 2
    assert [u[4] for u in unknown if u[1] == "VictimFillActions"] == [ih.REASON_UNREADABLE_STAMP] * 2
    assert "UTC+10:00" in report.local_zone and "UTC+10:00" in report.markdown()


def test_a_ledger_with_mixed_writers_counts_stamps_either_side_of_the_cutoff_as_unknown(monkeypatch):
    mixed = tuple(ih.Ledger(l.table, l.column, "ambiguous", l.replaces, "mixed writers")
                  if l.table == "VictimFillActions" else l for l in ih.LEDGERS)
    monkeypatch.setattr(ih, "LEDGERS", mixed)
    conn = _db()
    ih.ensure_schema(conn)
    _stamp_all(conn, "2025-01-01 00:00:00")
    _ledgers(conn, {"VictimFillActions": [
        "2026-02-27 10:00:00",           # before the cutoff read as UTC or as UTC+10: known
        "2026-03-01 10:00:00",           # 10:00 UTC or 00:00 UTC: on both sides of a 05:00 UTC cutoff
        "2026-03-01 20:00:00",           # 20:00 or 10:00 UTC: after it either way
        "2026-03-01T10:00:00+10:00"]})   # an offset settles it: 00:00 UTC, known
    report = ih.rewind(conn, "2026-03-01 05:00:00", local_zone=BRISBANE)
    assert _left(conn, "VictimFillActions") == ["2026-02-27 10:00:00", "2026-03-01T10:00:00+10:00"]
    fills = next(l for l in report.ledgers if l.table == "VictimFillActions")
    assert (fills.naive, fills.rows_dropped, fills.rows_unknown) == ("ambiguous", 2, 1)
    assert _unknown(conn) == [("ledger", "VictimFillActions", {"id": 2}, None, ih.REASON_AMBIGUOUS_STAMP)]
    assert "| `VictimFillActions` | ambiguous |" in report.markdown()


def test_as_known_at_accepts_an_offset_and_converts_it_to_utc():
    assert ih.parse_as_known_at("2026-06-30T08:30+10:00") == ("2026-06-29 22:30:00", date(2026, 6, 29))
    assert ih.parse_as_known_at("2026-06-30 08:30:15Z") == ("2026-06-30 08:30:15", date(2026, 6, 30))
    assert ih.parse_as_known_at(datetime(2026, 6, 30, 1, 0, tzinfo=BRISBANE)) == ("2026-06-29 15:00:00", date(2026, 6, 29))
    assert ih.parse_as_known_at(datetime(2026, 6, 30, 1, 0)) == ("2026-06-30 01:00:00", date(2026, 6, 30))
    assert ih.parse_as_known_at(date(2026, 6, 30)) == ("2026-06-30 23:59:59", date(2026, 6, 30))
    for bad in ("2026-06-31", "2026-06-30T25:00", "30/06/2026"):
        with pytest.raises(ValueError):
            ih.parse_as_known_at(bad)


# --- the ledger writers -----------------------------------------------------------------------------------------------

def test_utc_stamps_and_their_parsing():
    stamp = ts.utc_now_iso()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", stamp)
    assert ts.parse_stamp(stamp).utcoffset() == timedelta(0)
    assert ts.parse_stamp("2026-03-01") == datetime(2026, 3, 1)
    assert ts.parse_stamp("2026-03-01 04:05:06,25").microsecond == 250000
    assert ts.parse_stamp("2026-03-01T04:05+0530").utcoffset() == timedelta(hours=5, minutes=30)
    assert ts.parse_stamp("yesterday") is None and ts.parse_stamp(None) is None
    naive = datetime(2026, 3, 1, 14, 0)
    assert ts.to_utc(naive, ts.NAIVE_LOCAL, BRISBANE) == datetime(2026, 3, 1, 4, 0, tzinfo=timezone.utc)
    assert ts.to_utc(naive, ts.NAIVE_LOCAL) == naive.astimezone(timezone.utc)          # system rules for that date
    assert ts.to_utc(naive, ts.NAIVE_UTC) == naive.replace(tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        ts.to_utc(naive, "ambiguous")


# Modules whose only stamps are ledger stamps, and the others that also write a ledger stamp.
LOCAL_FREE_WRITERS = ("dedup/victim_judgement.py", "dedup/victim_fill.py", "dedup/contextual_aliases.py",
                      "dedup/event_organisations.py", "utils/jev.py", "utils/escalation.py")
OTHER_WRITERS = ("dedup/ledger.py", "dedup/entity_resolution.py", "dedup/entity_alias_verification.py")


def test_no_ledger_writer_stamps_local_time_or_relies_on_a_naive_default():
    root = Path(ih.__file__).resolve().parent.parent
    for rel in LOCAL_FREE_WRITERS + OTHER_WRITERS:
        source = (root / rel).read_text(encoding="utf-8")
        assert "utc_now_iso" in source, rel
        if rel in LOCAL_FREE_WRITERS:
            assert not re.search(r"datetime\.now\(\)\.isoformat", source), rel
    assert "created_at = CURRENT_TIMESTAMP" not in (root / "dedup/ledger.py").read_text(encoding="utf-8")


def test_override_alias_and_mention_writers_stamp_utc_with_an_offset():
    from cyber_data_collector.dedup import contextual_aliases as ca
    from cyber_data_collector.dedup.entity_resolution import EntityResolver
    from cyber_data_collector.dedup.ledger import DedupLedger
    from cyber_data_collector.dedup.models import OverrideVerdict
    from cyber_data_collector.dedup.schema import migrate as dedup_schema

    conn = sqlite3.connect(":memory:")
    dedup_schema(conn)
    DedupLedger(conn).add_override("a", "b", OverrideVerdict.DIFFERENT, "test")
    resolver = EntityResolver()
    resolver.learn_alias(conn, "Acme Pty Ltd", "Acme")
    ca.add_contextual_alias(conn, "AC", "Acme")
    stamps = [conn.execute("SELECT created_at FROM DedupOverrides").fetchone()[0],
              conn.execute("SELECT created_at FROM EntityAliases").fetchone()[0],
              conn.execute("SELECT created_at FROM ContextualEntityAliases").fetchone()[0]]
    assert all(s.endswith("+00:00") for s in stamps), stamps
    # An alias re-learned unchanged keeps its date; a changed one is dated when it changed.
    conn.execute("UPDATE EntityAliases SET created_at = '2026-01-01T00:00:00+00:00'")
    resolver.learn_alias(conn, "Acme Pty Ltd", "Acme")
    assert conn.execute("SELECT created_at FROM EntityAliases").fetchone()[0] == "2026-01-01T00:00:00+00:00"
    resolver.learn_alias(conn, "Acme Pty Ltd", "Acme Group")
    assert conn.execute("SELECT created_at FROM EntityAliases").fetchone()[0] != "2026-01-01T00:00:00+00:00"


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


def test_victim_rows_with_unknown_inputs_are_counted_and_dropped_only_on_request():
    from scripts.analyze_recurrent_timing import apply_history_unknown_policy

    events = pd.DataFrame({"entity_id": [1, 1, 2], "deduplicated_event_id": ["e1", "e2", "e3"],
                           "history_unknown": [True, False, True]})
    kept, info = apply_history_unknown_policy(events, "include")
    assert len(kept) == 3 and info["victim-event attributions with unknown inputs at the cutoff"] == 2
    assert (info["events with unknown inputs"], info["organisations with unknown inputs"]) == (2, 2)
    dropped, info = apply_history_unknown_policy(events, "exclude")
    assert dropped["deduplicated_event_id"].tolist() == ["e2"] and info["victim-event attributions analysed"] == 1
    assert apply_history_unknown_policy(events.drop(columns="history_unknown"), "exclude")[0].shape[0] == 3
    with pytest.raises(ValueError):
        apply_history_unknown_policy(events, "guess")


def test_extended_runner_counts_and_can_exclude_rows_unknown_at_the_cutoff(tmp_path, monkeypatch):
    from scripts import analyze_recurrence_extensions as runner

    live = _live(tmp_path)
    with closing(sqlite3.connect(live)) as db:
        db.execute("UPDATE RawEvents SET discovered_at = '2025-11-11'")         # known before the cutoff
        db.commit()

    def fake_load(path):
        with sqlite3.connect(path) as db:
            ids = [r[0] for r in db.execute("SELECT deduplicated_event_id FROM DeduplicatedEvents ORDER BY 1")]
            unknown = ih.unknown_victim_rows(db, [(i + 1, e) for i, e in enumerate(ids)])
        return pd.DataFrame({"deduplicated_event_id": ids, "event_date": [date(2025, 11, 10)] * len(ids),
                             "entity_id": [i + 1 for i in range(len(ids))],
                             "history_unknown": [(i + 1, e) in unknown for i, e in enumerate(ids)]})

    monkeypatch.setattr(runner, "load_victim_events", fake_load)
    for module in (runner.timing_precision, runner.linkage_supplier, runner.sequence_bias):
        monkeypatch.setattr(module, "run", lambda events, snapshot, end, out_dir, seed, **kw: {"section": "## S"})
    # Before tracking began (2026-01-01) e1 holds its baseline state as a stand-in: unknown, and counted.
    summary = runner.run_analysis(live, tmp_path / "a", bootstrap=1, date_draws=1, as_known_at="2025-12-01")
    assert summary["history_unknown"]["victim-event attributions with unknown inputs at the cutoff"] == 1
    assert summary["history_unknown"]["rows unknown by table (InputHistoryUnknown)"]["EntitiesV2"] == 2
    report = (tmp_path / "a" / "extended_recurrence_report.md").read_text(encoding="utf-8")
    assert "state at --as-known-at is unknown" in report and "included here (--history-unknown)" in report
    with pytest.raises(RuntimeError, match="No victim events"):              # excluded, nothing known is left
        runner.run_analysis(live, tmp_path / "b", bootstrap=1, date_draws=1, as_known_at="2025-12-01",
                            history_unknown="exclude")
    with pytest.raises(ValueError, match="needs as_known_at"):
        runner.run_analysis(live, tmp_path / "c", date(2026, 3, 1), bootstrap=1, date_draws=1,
                            history_unknown="exclude")


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
