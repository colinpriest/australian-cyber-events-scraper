"""Manual validation exceptions: incorrect results the owner reported, kept and re-checked.

Every time the owner identifies a wrong result in the published data ("these two are the same incident", "the
victim should be Latrobe City Council"), it is stored here with the owner's words. Each exception is a test the
data must pass from then on; :func:`check` re-evaluates all of them and the quality report counts the failures,
so a later change to a pipeline algorithm is measured against every case the owner has found. They are acceptance
tests, never inputs to a rule: nothing in the pipeline reads this table to decide anything.

An exception is anchored on the **records** of the event it was reported against (``record_ids_json``), not on
the event id alone: event ids change with every merge and split. At check time the event is the Active event
holding the most of those records (a tie, or none Active, is *unchecked*, never a pass).

Kinds:

* ``victim``: the event's victim roles include ``value`` (after alias resolution);
* ``not_victim``: no victim role of the event is ``value``;
* ``same_incident_as``: the event and the event ``value`` (anchored on its records too) are one Active event;
* ``different_incident_from``: they are two Active events;
* ``incident_month``: the event's incident date (its verified interval when it has one) falls in ``value``
  (``YYYY-MM``).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from cyber_data_collector.utils.timestamps import utc_now_iso

KINDS = ("victim", "not_victim", "same_incident_as", "different_incident_from", "incident_month")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ValidationExceptions (
    exception_id          TEXT PRIMARY KEY,
    reported_at           TEXT NOT NULL,
    reported_by           TEXT NOT NULL,          -- owner
    kind                  TEXT NOT NULL,          -- victim | not_victim | same_incident_as | different_incident_from | incident_month
    deduplicated_event_id TEXT NOT NULL,          -- as reported
    record_ids_json       TEXT NOT NULL,          -- the event's Active records when reported (the anchor)
    value                 TEXT NOT NULL,          -- an organisation name, or the other event's id
    value_record_ids_json TEXT,                   -- the other event's records, for the two pair kinds
    observed              TEXT,                   -- what was wrong when reported
    note                  TEXT NOT NULL,          -- the owner's words
    root_cause            TEXT,                   -- what was found and fixed
    last_checked_at       TEXT,
    last_result           TEXT,                   -- pass | fail | unchecked
    last_detail           TEXT
);
"""


@dataclass
class Result:
    exception_id: str
    kind: str
    result: str            # pass | fail | unchecked
    detail: str


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _records(conn: sqlite3.Connection, event_id: str) -> List[str]:
    has_status = "status" in {r[1] for r in conn.execute("PRAGMA table_info(EnrichedEvents)")}
    active = "AND COALESCE(e.status, 'Active') = 'Active'" if has_status else ""
    return [r[0] for r in conn.execute(
        f"""SELECT m.enriched_event_id FROM EventDeduplicationMap m
            JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
            WHERE m.deduplicated_event_id = ? {active}
            ORDER BY m.enriched_event_id""", (event_id,))]


def add(conn: sqlite3.Connection, kind: str, event_id: str, value: str, note: str,
        observed: Optional[str] = None, reported_by: str = "owner") -> str:
    """Store one exception, anchored on the event's records (and the other event's, for a pair)."""
    ensure_schema(conn)
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if not (note or "").strip():
        raise ValueError("an exception needs the owner's words (note)")
    records = _records(conn, event_id)
    if not records:
        raise ValueError(f"event {event_id} has no Active records to anchor the exception on")
    other = None
    if kind in ("same_incident_as", "different_incident_from"):
        other = _records(conn, value)
        if not other:
            raise ValueError(f"event {value} has no Active records to anchor the exception on")
    existing = conn.execute(
        "SELECT exception_id FROM ValidationExceptions WHERE kind = ? AND deduplicated_event_id = ? AND value = ?",
        (kind, event_id, value)).fetchone()
    if existing:
        return existing[0]
    exception_id = str(uuid.uuid4())
    conn.execute("INSERT INTO ValidationExceptions (exception_id, reported_at, reported_by, kind, deduplicated_event_id, "
                 "record_ids_json, value, value_record_ids_json, observed, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (exception_id, utc_now_iso(), reported_by, kind, event_id, json.dumps(records), value,
                  json.dumps(other) if other is not None else None, observed, note.strip()))
    conn.commit()
    return exception_id


def current_event(conn: sqlite3.Connection, records: Sequence[str]) -> Optional[str]:
    """The Active event holding the most of ``records``; None when none does or two tie."""
    if not records:
        return None
    marks = ",".join("?" * len(records))
    counts = conn.execute(
        f"""SELECT m.deduplicated_event_id, COUNT(*) FROM EventDeduplicationMap m
            JOIN DeduplicatedEvents d ON d.deduplicated_event_id = m.deduplicated_event_id
            WHERE d.status = 'Active' AND m.enriched_event_id IN ({marks})
            GROUP BY m.deduplicated_event_id ORDER BY COUNT(*) DESC""", list(records)).fetchall()
    if not counts or (len(counts) > 1 and counts[0][1] == counts[1][1]):
        return None
    return counts[0][0]


def _victims(conn: sqlite3.Connection, event_id: str) -> List[str]:
    return [r[0] for r in conn.execute(
        """SELECT v.entity_name FROM DeduplicatedEventEntities dee JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
           WHERE dee.deduplicated_event_id = ? AND dee.relationship_type = 'victim'""", (event_id,))]


def evaluate(conn: sqlite3.Connection, row: Dict) -> Result:
    """Whether the data now satisfies one exception."""
    from cyber_data_collector.dedup.victim_fill import names_match

    eid, kind = row["exception_id"], row["kind"]
    now = current_event(conn, json.loads(row["record_ids_json"]))
    if now is None:
        return Result(eid, kind, "unchecked", "no single Active event holds the reported records")
    if kind == "incident_month":
        have = {r[1] for r in conn.execute("PRAGMA table_info(DeduplicatedEvents)")}
        cols = ["event_date"] + [c for c in ("event_date_earliest", "event_date_latest") if c in have]
        found = conn.execute(f"SELECT {', '.join(cols)} FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                             (now,)).fetchone()
        date, lo, hi = (list(found) + [None, None])[:3]
        want = row["value"]
        ok = bool(date) and (str(date)[:7] == want or bool(lo and hi and str(lo)[:7] <= want <= str(hi)[:7]))
        return Result(eid, kind, "pass" if ok else "fail", f"event {now[:8]} dated {date or 'none'}")
    if kind in ("victim", "not_victim"):
        victims = _victims(conn, now)
        hit = any(names_match(conn, row["value"], v) for v in victims)
        ok = hit if kind == "victim" else not hit
        return Result(eid, kind, "pass" if ok else "fail",
                      f"event {now[:8]} victims: {', '.join(victims) or 'none'}")
    other = current_event(conn, json.loads(row["value_record_ids_json"] or "[]"))
    if other is None:
        return Result(eid, kind, "unchecked", "no single Active event holds the other event's records")
    same = other == now
    ok = same if kind == "same_incident_as" else not same
    return Result(eid, kind, "pass" if ok else "fail",
                  f"events {now[:8]} and {other[:8]}" if not same else f"one event {now[:8]}")


def check(conn: sqlite3.Connection, record: bool = True) -> List[Result]:
    """Evaluate every exception; ``record`` stores each result on its row."""
    ensure_schema(conn)
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM ValidationExceptions ORDER BY reported_at")]
    finally:
        conn.row_factory = previous
    results = [evaluate(conn, r) for r in rows]
    if record and results:
        now = utc_now_iso()
        conn.executemany("UPDATE ValidationExceptions SET last_checked_at = ?, last_result = ?, last_detail = ? "
                         "WHERE exception_id = ?", [(now, r.result, r.detail, r.exception_id) for r in results])
        conn.commit()
    return results


def revise(conn: sqlite3.Connection, exception_id: str, kind: str, value: str, note: str) -> None:
    """Restate an exception after a later owner decision: new kind and value, the owner's new words appended to
    the original ones (kept). Anchored on the same records; a pair kind anchors ``value``'s records afresh."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    row = conn.execute("SELECT note FROM ValidationExceptions WHERE exception_id = ?", (exception_id,)).fetchone()
    if row is None:
        raise ValueError(f"no validation exception {exception_id}")
    other = None
    if kind in ("same_incident_as", "different_incident_from"):
        other = _records(conn, value)
        if not other:
            raise ValueError(f"event {value} has no Active records to anchor the exception on")
    conn.execute("UPDATE ValidationExceptions SET kind = ?, value = ?, value_record_ids_json = ?, note = ?, "
                 "last_result = NULL, last_detail = NULL, last_checked_at = NULL WHERE exception_id = ?",
                 (kind, value, json.dumps(other) if other is not None else None,
                  f"{row[0]} | revised {utc_now_iso()[:10]}: {note.strip()}", exception_id))
    conn.commit()


def set_root_cause(conn: sqlite3.Connection, exception_id: str, root_cause: str) -> None:
    conn.execute("UPDATE ValidationExceptions SET root_cause = ? WHERE exception_id = ?", (root_cause, exception_id))
    conn.commit()
