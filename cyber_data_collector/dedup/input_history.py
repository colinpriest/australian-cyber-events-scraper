"""What was known when: an append-only history of the inputs the recurrence analysis reads.

The recurrence analyses used to take a historical cutoff by filtering on discovery date but read today's
incident links, dates, victims and size bands, so a result "as at June" described October's database.
This module records when each of those decisions became known, so an analysis can reconstruct its inputs
as they stood at a date (``--as-known-at``).

**What is recorded (SQLite triggers, so every writer is covered, present and future).**

========================  ============================  ==========================================
Decision                  Table watched                 History table
========================  ============================  ==========================================
incident dates / status   ``DeduplicatedEvents``        ``EventStateHistory``
event membership          ``EventDeduplicationMap``     ``EventMembershipHistory``
victim attribution        ``DeduplicatedEventEntities`` ``EventEntityLinkHistory``
organisation size band    ``EntitiesV2``                ``EntityStateHistory``
========================  ============================  ==========================================

Each history row is the full tracked state of one key after a change (``insert`` / ``update`` /
``delete``) with ``known_at`` (UTC, from the database clock), or ``baseline``. Rows are only appended.
A change to a column nobody tracks writes nothing. A merge, split, role refresh, entity fold, redate
or re-sizing therefore leaves a history row whichever code path made it.

**Decision ledgers that already hold their own history are reused, not copied.** ``VictimAttributionActions``,
``VictimFillActions``, ``EntityAliases``, ``ContextualEntityAliases``, ``EntityMentionResolutions``,
``DedupOverrides`` and ``ModelJudgements`` carry a timestamp per row; the reconstruction drops the rows
dated after the cutoff (:data:`TIMESTAMPED_LEDGERS`). ``EntityMerges`` and ``DedupSnapshots`` hold the state
immediately *before* one change but not since when it held, and not every change writes one, so they cannot
date a state; their effects are in the history tables from the moment tracking began.

**History before tracking began cannot be invented.** The first time a source is tracked its current state is
written as ``baseline`` rows (``backfilled = 1``, ``known_at`` = that moment) and ``InputHistoryStatus`` records
``tracking_since``. A cutoff earlier than that is *reconstructed from current state*: each key takes its
earliest recorded state, and the reconstruction says so, per source and column, with counts. A key whose first
row is a genuine ``insert`` did not exist before it, whatever the cutoff.

Ledger tables and the four history tables are created, and the triggers (re)installed, by
:func:`ensure_schema` - idempotent, cheap when nothing changed, and safe on a database without these tables.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

#: ``known_at`` stamps are UTC, second resolution, written by the database clock (not by Python), so every
#: connection that touches a watched table stamps the same way.
NOW_SQL = "strftime('%Y-%m-%d %H:%M:%S','now')"
STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
NL = chr(10)
CHANGES = ("baseline", "insert", "update", "delete")


@dataclass(frozen=True)
class Source:
    """One watched table: where its history goes and which columns are recorded."""

    name: str
    table: str
    history: str
    key: Tuple[str, ...]
    columns: Tuple[str, ...]
    label: str


SOURCES: Tuple[Source, ...] = (
    Source("events", "DeduplicatedEvents", "EventStateHistory", ("deduplicated_event_id",),
           ("master_enriched_event_id", "title", "status", "event_date", "event_date_precision",
            "event_date_earliest", "event_date_latest", "date_precision", "date_basis", "date_confidence",
            "is_australian_event", "is_specific_event", "event_type", "records_affected",
            "victim_organization_name", "vendor_organization_name", "victim_organization_industry",
            "confidence_score", "total_data_sources"),
           "incident dates, status and fields"),
    Source("membership", "EventDeduplicationMap", "EventMembershipHistory", ("map_id",),
           ("raw_event_id", "enriched_event_id", "deduplicated_event_id"),
           "event membership (records in each incident)"),
    Source("links", "DeduplicatedEventEntities", "EventEntityLinkHistory", ("deduplicated_event_id", "entity_id"),
           ("relationship_type", "confidence_score", "source_count"),
           "victim attribution (event-organisation links and roles)"),
    Source("entities", "EntitiesV2", "EntityStateHistory", ("entity_id",),
           ("entity_name", "entity_type", "entity_kind", "industry", "is_australian", "employee_count", "turnover",
            "size_estimate", "size_confidence", "size_employees", "size_revenue_aud"),
           "organisation size band and attributes"),
)
SOURCE_BY_NAME = {s.name: s for s in SOURCES}

#: Decision ledgers with a timestamp per row: table -> timestamp column. Rows dated after the cutoff were not
#: known at it. A ledger is trusted as history only from its first row; before that it says nothing, so a cutoff
#: earlier than its first row leaves the table as it is, and says so.
TIMESTAMPED_LEDGERS: Dict[str, str] = {
    "VictimAttributionActions": "created_at",
    "VictimFillActions": "created_at",
    "EntityAliases": "created_at",
    "ContextualEntityAliases": "created_at",
    "EntityMentionResolutions": "decided_at",
    "DedupOverrides": "created_at",
    "ModelJudgements": "created_at",
}

STATUS_SCHEMA = """
CREATE TABLE IF NOT EXISTS InputHistoryStatus (
    source         TEXT PRIMARY KEY,
    table_name     TEXT NOT NULL,
    tracking_since TEXT NOT NULL,      -- from here every change to the tracked columns is recorded
    baselined_at   TEXT NOT NULL,      -- when the then-current state was written as 'baseline' rows
    columns_json   TEXT NOT NULL       -- {tracked column: time it began to be tracked}
)
"""


class HistoryError(RuntimeError):
    """The history needed to reconstruct a date is missing or unusable; the message says what to run."""


# --- schema ------------------------------------------------------------------------------------------------------

def _tables(conn: sqlite3.Connection) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _history_ddl(src: Source) -> str:
    # Columns are declared without types and keys without NOT NULL: a value is stored exactly as the watched
    # table held it, and a history trigger can never reject a write to the table it watches.
    lines = ["history_id INTEGER PRIMARY KEY AUTOINCREMENT", ", ".join(src.key),
             "change TEXT NOT NULL CHECK (change IN ('baseline','insert','update','delete'))",
             "known_at TEXT NOT NULL", "backfilled INTEGER NOT NULL DEFAULT 0", ", ".join(src.columns)]
    return f"CREATE TABLE IF NOT EXISTS {src.history} (" + NL + "    " + ("," + NL + "    ").join(lines) + NL + ")"


def _column_list(src: Source) -> str:
    return ", ".join(list(src.key) + ["change", "known_at", "backfilled"] + list(src.columns))


def _key_match(src: Source, prefix: str) -> str:
    return " AND ".join(f"h.{k} IS {prefix}.{k}" for k in src.key)


def _trigger_sql(src: Source, present: set) -> Dict[str, str]:
    """The three triggers for ``src``, a pure function of which tracked columns the table has."""
    tracked = [c for c in src.columns if c in present]
    cols = _column_list(src)
    changed = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in list(src.key) + tracked)
    key_changed = " OR ".join(f"OLD.{k} IS NOT NEW.{k}" for k in src.key)

    def row(prefix: str, change_sql: str, backfilled: int) -> str:
        return (f"SELECT {', '.join(f'{prefix}.{k}' for k in src.key)}, {change_sql}, {NOW_SQL}, {backfilled}"
                + "".join(f", {prefix}.{c}" if c in present else ", NULL" for c in src.columns))

    insert_into = f"INSERT INTO {src.history} ({cols})"
    # A key that existed before tracking but has no history row yet gets its pre-change state recorded first.
    heal = (f"{insert_into} {row('OLD', repr('baseline'), 1)} "
            f"WHERE NOT EXISTS (SELECT 1 FROM {src.history} h WHERE {_key_match(src, 'OLD')})")
    change = f"CASE WHEN {key_changed} THEN 'insert' ELSE 'update' END"
    t = src.table
    return {
        f"trg_inputhistory_{t}_insert": NL.join([
            f"CREATE TRIGGER trg_inputhistory_{t}_insert AFTER INSERT ON {t}", "BEGIN",
            f"  {insert_into} {row('NEW', repr('insert'), 0)};", "END"]),
        f"trg_inputhistory_{t}_update": NL.join([
            f"CREATE TRIGGER trg_inputhistory_{t}_update AFTER UPDATE ON {t}", f"WHEN {changed}", "BEGIN",
            f"  {heal};",
            f"  {insert_into} {row('OLD', repr('delete'), 0)} WHERE {key_changed};",
            f"  {insert_into} {row('NEW', change, 0)};", "END"]),
        f"trg_inputhistory_{t}_delete": NL.join([
            f"CREATE TRIGGER trg_inputhistory_{t}_delete AFTER DELETE ON {t}", "BEGIN",
            f"  {heal};", f"  {insert_into} {row('OLD', repr('delete'), 0)};", "END"]),
    }


def _installed_triggers(conn: sqlite3.Connection, table: str) -> Dict[str, str]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name LIKE ?",
        (f"trg_inputhistory_{table}_%",))}


def _pending(conn: sqlite3.Connection) -> bool:
    """True when :func:`ensure_schema` has something to do (read-only, so the steady state takes no write lock)."""
    tables = _tables(conn)
    if "InputHistoryStatus" not in tables:
        return True
    known = {r[0]: json.loads(r[1]) for r in conn.execute("SELECT source, columns_json FROM InputHistoryStatus")}
    for src in SOURCES:
        if src.table not in tables:
            continue
        present = set(_columns(conn, src.table))
        if not set(src.key) <= present:
            continue
        if src.history not in tables or src.name not in known:
            return True
        if _installed_triggers(conn, src.table) != _trigger_sql(src, present):
            return True
        if any(c in present and c not in known[src.name] for c in src.columns):
            return True
        if not set(src.columns) <= set(_columns(conn, src.history)):
            return True
    return False


def ensure_schema(conn: sqlite3.Connection) -> Dict[str, str]:
    """Create the history tables, install or refresh the triggers, and baseline newly tracked state.

    Idempotent, and a read-only check when nothing is pending. A watched table that does not exist yet (or is
    not the table this module watches: a bare test schema without its key columns) is skipped; a later call
    picks it up. Returns ``{source: what was done}`` ('' when nothing was), for logging and tests. The first
    call for a source writes its current state as ``baseline`` rows; a later call that finds new tracked columns
    (a column added to the watched table, or to :data:`SOURCES`) refreshes the triggers and baselines again,
    recording since when each column is tracked. Runs in its own write transaction unless the caller has one open.
    """
    if not _pending(conn):
        return {}
    owns = not conn.in_transaction
    if owns:
        conn.execute("BEGIN IMMEDIATE")      # concurrent first calls serialise here and the second finds nothing to do
    try:
        done = _apply(conn)
        if owns:
            conn.commit()
    except BaseException:
        if owns:
            conn.rollback()
        raise
    return done


def _apply(conn: sqlite3.Connection) -> Dict[str, str]:
    done: Dict[str, str] = {}
    conn.execute(STATUS_SCHEMA)
    tables = _tables(conn)
    for src in SOURCES:
        if src.table not in tables:
            continue
        if not set(src.key) <= set(_columns(conn, src.table)):
            continue                                    # not the table this module watches (a bare test schema)
        conn.execute(_history_ddl(src))
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{src.history.lower()}_key "
                     f"ON {src.history}({', '.join(src.key)}, history_id)")
        have = set(_columns(conn, src.history))
        for column in src.columns:
            if column not in have:
                conn.execute(f"ALTER TABLE {src.history} ADD COLUMN {column}")
        present = set(_columns(conn, src.table))
        wanted = _trigger_sql(src, present)
        installed = _installed_triggers(conn, src.table)
        actions = []
        if installed != wanted:
            for name in installed:
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            for sql in wanted.values():
                conn.execute(sql)
            actions.append("triggers installed")
        tracked = [c for c in src.columns if c in present]
        status = conn.execute("SELECT columns_json FROM InputHistoryStatus WHERE source = ?", (src.name,)).fetchone()
        known = json.loads(status[0]) if status else {}
        added = [c for c in tracked if c not in known]
        if added:
            now = conn.execute(f"SELECT {NOW_SQL}").fetchone()[0]
            # The baseline is written before any change the triggers record next, in this transaction.
            inserted = _baseline(conn, src, present, only_missing=status is None)
            columns = {**known, **{c: now for c in added}}
            conn.execute("INSERT INTO InputHistoryStatus (source, table_name, tracking_since, baselined_at, "
                         "columns_json) VALUES (?, ?, ?, ?, ?) ON CONFLICT(source) DO UPDATE SET "
                         "baselined_at = excluded.baselined_at, columns_json = excluded.columns_json",
                         (src.name, src.table, now, now, json.dumps(columns, sort_keys=True)))
            actions.append(f"baseline of {inserted} row(s) ({', '.join(added) if status else 'all columns'})")
        done[src.name] = "; ".join(actions)
        if actions:
            logger.info("Input history for %s: %s", src.table, done[src.name])
    return done


def _baseline(conn: sqlite3.Connection, src: Source, present: set, only_missing: bool) -> int:
    """Write the current state of every row as ``baseline`` (``backfilled = 1``). Returns rows written."""
    cols = _column_list(src)
    select = (f"SELECT {', '.join(f't.{k}' for k in src.key)}, 'baseline', {NOW_SQL}, 1"
              + "".join(f", t.{c}" if c in present else ", NULL" for c in src.columns)
              + f" FROM {src.table} t")
    if only_missing:
        select += f" WHERE NOT EXISTS (SELECT 1 FROM {src.history} h WHERE {_key_match(src, 't')})"
    before = conn.total_changes
    conn.execute(f"INSERT INTO {src.history} ({cols}) {select}")
    return conn.total_changes - before


# --- cutoffs -----------------------------------------------------------------------------------------------------

def parse_as_known_at(value: Union[str, date, datetime]) -> Tuple[str, date]:
    """``(cutoff stamp, cutoff date)`` for a date or datetime, read as UTC.

    A date means the end of that day (everything stamped on it is known); a datetime is taken exactly.
    Stamps compare as strings in ``YYYY-MM-DD HH:MM:SS``.
    """
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day, 23, 59, 59)
    else:
        text = str(value).strip().replace("T", " ")
        try:
            if len(text) == 10:
                parsed = datetime.strptime(text, "%Y-%m-%d")
                moment = parsed.replace(hour=23, minute=59, second=59)
            else:
                moment = datetime.fromisoformat(text[:19])
        except ValueError as exc:
            raise ValueError(f"--as-known-at must be YYYY-MM-DD or YYYY-MM-DD HH:MM[:SS] (UTC), got {value!r}") from exc
    return moment.strftime(STAMP_FORMAT), moment.date()


def _norm(column: str) -> str:
    """SQL turning a stored timestamp (``T`` or space separated, any precision) into ``YYYY-MM-DD HH:MM:SS``."""
    return f"replace(substr({column}, 1, 19), 'T', ' ')"


# --- reconstruction ----------------------------------------------------------------------------------------------

@dataclass
class SourceReport:
    name: str
    table: str
    label: str
    tracking_since: str
    mode: str                                   # 'history' | 'current-state fallback'
    keys_in_history: int = 0
    unchanged: int = 0
    changed: int = 0                            # rows whose reconstructed state differs from the working copy
    removed: int = 0                            # rows that did not exist yet and were removed
    restored: int = 0                           # rows deleted since, put back
    fallback_keys: int = 0                      # state taken from the earliest recorded (baseline) state
    untracked_keys: int = 0                     # present now, never recorded: left as they are
    fallback_columns: Dict[str, int] = field(default_factory=dict)   # tracked later than the row: current value used

    @property
    def exact(self) -> bool:
        """True when every row and column came from recorded history, none from an earlier or current state."""
        return self.mode == "history" and not self.fallback_keys and not self.fallback_columns

    def verdict(self) -> str:
        if self.mode == "history" and self.exact:
            return f"reconstructed from history (recorded since {self.tracking_since} UTC)"
        if self.mode == "history":
            return (f"reconstructed from history (recorded since {self.tracking_since} UTC); {self.fallback_keys} row(s) "
                    "predate their first record and take that state")
        return (f"reconstructed from current state: history is recorded only since {self.tracking_since} UTC; "
                f"{self.fallback_keys} of {self.keys_in_history} rows take the state recorded when tracking began "
                "(the current state at backfill; later changes are undone)")


@dataclass
class LedgerReport:
    table: str
    first_row: Optional[str]
    mode: str                                   # 'filtered' | 'not filtered (cutoff before first row)' | 'absent'
    rows_dropped: int = 0
    rows_kept: int = 0


@dataclass
class HistoryReconstruction:
    """What an ``--as-known-at`` reconstruction did, for the report and ``analysis_inputs.json``."""

    as_known_at: str
    cutoff: str
    cutoff_date: date
    sources: List[SourceReport] = field(default_factory=list)
    ledgers: List[LedgerReport] = field(default_factory=list)

    @property
    def fallback_fields(self) -> List[str]:
        """Labels of the inputs reconstructed from current state (history did not reach the cutoff)."""
        return [s.label for s in self.sources if not s.exact]

    def to_dict(self) -> Dict[str, Any]:
        return {"as_known_at": self.as_known_at, "cutoff_utc": self.cutoff,
                "reconstructed_from_current_state": self.fallback_fields,
                "sources": [{**s.__dict__, "verdict": s.verdict()} for s in self.sources],
                "ledgers": [l.__dict__ for l in self.ledgers]}

    def markdown(self) -> str:
        lines = [f"## Inputs as known at {self.as_known_at}", "",
                 f"The incident links, dates, victims and size bands below were reconstructed from the recorded history "
                 f"of what was known by {self.cutoff} UTC (`--as-known-at`; `InputHistoryStatus`). Where the history "
                 "does not reach back that far the field is the state recorded when tracking began (the current state at backfill), and is marked so.", "",
                 "| Input | Basis | Rows reconstructed | Removed (not yet known) | Restored | Changed |",
                 "|---|---|---:|---:|---:|---:|"]
        for s in self.sources:
            basis = "history" if s.exact else "**current state (backfilled)**"
            lines.append(f"| {s.label} (`{s.table}`) | {basis}; {s.verdict()} | {s.keys_in_history} | {s.removed} | "
                         f"{s.restored} | {s.changed} |")
        lines += ["", "| Decision ledger | Basis | Rows dropped (dated after the cutoff) | Rows kept |", "|---|---|---:|---:|"]
        for l in self.ledgers:
            lines.append(f"| `{l.table}` | {l.mode}{'' if l.first_row is None else f' (first row {l.first_row})'} | "
                         f"{l.rows_dropped} | {l.rows_kept} |")
        columns = {f"{s.table}.{c}": n for s in self.sources for c, n in s.fallback_columns.items()}
        if columns:
            lines += ["", "Columns tracked only after some rows were recorded take their current value for those rows: "
                      + ", ".join(f"`{c}` ({n})" for c, n in sorted(columns.items())) + "."]
        if self.fallback_fields:
            lines += ["", "**Limitation.** Reconstructed from current state, not from history: "
                      + "; ".join(self.fallback_fields) + ". A cutoff before the history began cannot be reconstructed "
                      "more precisely than the earliest recorded state; this is a narrowing of the limitation, not its removal."]
        return "\n".join(lines) + "\n"


def _state_at(rows: List[sqlite3.Row], cutoff: str) -> Tuple[Optional[sqlite3.Row], str]:
    """``(state row or None for 'does not exist', basis)`` for one key's history, oldest first.

    basis: 'recorded' (a row at or before the cutoff decides it), 'absent' (first row is a genuine insert after
    the cutoff: it did not exist), 'earliest' (only later rows exist and the first is a baseline: the state
    before it is unknown, so the earliest recorded state stands in).
    """
    latest = None
    for row in rows:
        if row["known_at"] <= cutoff:
            latest = row
        else:
            break
    if latest is not None:
        return (None if latest["change"] == "delete" else latest), "recorded"
    first = rows[0]
    if first["backfilled"]:
        return first, "earliest"
    return None, "absent"


def _rewind_source(conn: sqlite3.Connection, src: Source, cutoff: str, status: sqlite3.Row) -> SourceReport:
    since = status["tracking_since"]
    columns_since = json.loads(status["columns_json"])
    report = SourceReport(src.name, src.table, src.label, since, "history" if cutoff >= since else
                          "current-state fallback")
    present = set(_columns(conn, src.table))
    tracked = [c for c in src.columns if c in present and c in columns_since]
    cols = ", ".join(list(src.key) + [c for c in tracked])
    keys = len(src.key)
    history: Dict[tuple, List[sqlite3.Row]] = {}
    for row in conn.execute(f"SELECT history_id, change, known_at, backfilled, {_column_list_plain(src)} "
                            f"FROM {src.history} ORDER BY history_id"):
        history.setdefault(tuple(row[k] for k in src.key), []).append(row)
    report.keys_in_history = len(history)
    current = {tuple(r)[:keys]: tuple(r)[keys:] for r in conn.execute(f"SELECT {cols} FROM {src.table}")}
    fallback_columns: Dict[str, int] = {}
    deletes: List[tuple] = []
    updates: List[tuple] = []
    inserts: List[tuple] = []
    for key, rows in history.items():
        state, basis = _state_at(rows, cutoff)
        if basis == "earliest":
            report.fallback_keys += 1
        now = current.get(key)
        if state is None:
            if now is not None:
                deletes.append(key)
                report.removed += 1
            continue
        values = []
        for index, column in enumerate(tracked):
            if state["known_at"] < columns_since[column]:
                # The column was not tracked yet when this row was written: its value is unknown then.
                values.append(now[index] if now is not None else None)
                fallback_columns[column] = fallback_columns.get(column, 0) + 1
            else:
                values.append(state[column])
        values_t = tuple(values)
        if now is None:
            inserts.append(key + values_t)
            report.restored += 1
        elif now != values_t:
            updates.append(values_t + key)
            report.changed += 1
        else:
            report.unchanged += 1
    report.untracked_keys = sum(1 for key in current if key not in history)
    report.fallback_columns = fallback_columns
    where = " AND ".join(f"{k} = ?" for k in src.key)
    if deletes:
        conn.executemany(f"DELETE FROM {src.table} WHERE {where}", deletes)
    if updates and tracked:
        conn.executemany(f"UPDATE {src.table} SET {', '.join(f'{c} = ?' for c in tracked)} WHERE {where}", updates)
    if inserts:
        conn.executemany(f"INSERT INTO {src.table} ({cols}) VALUES ({', '.join('?' * (keys + len(tracked)))})", inserts)
    return report


def _column_list_plain(src: Source) -> str:
    return ", ".join(list(src.key) + list(src.columns))


def _rewind_ledger(conn: sqlite3.Connection, table: str, column: str, cutoff: str) -> LedgerReport:
    if table not in _tables(conn) or column not in _columns(conn, table):
        return LedgerReport(table, None, "absent")
    first = conn.execute(f"SELECT MIN({_norm(column)}) FROM {table} WHERE {column} IS NOT NULL").fetchone()[0]
    total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if first is None or cutoff < first:
        return LedgerReport(table, first, "not filtered (cutoff before the first row)", 0, total)
    dropped = conn.execute(f"DELETE FROM {table} WHERE {column} IS NOT NULL AND {_norm(column)} > ?", (cutoff,)).rowcount
    return LedgerReport(table, first, "filtered", dropped, total - dropped)


def rewind(conn: sqlite3.Connection, as_known_at: Union[str, date, datetime]) -> HistoryReconstruction:
    """Rewrite ``conn``'s database in place to the state known at ``as_known_at``. Use a COPY.

    Raises :class:`HistoryError` when no history was ever recorded (run ``dedup_admin.py input-history``).
    The caller commits (or rolls back); nothing is committed here.
    """
    cutoff, cutoff_date = parse_as_known_at(as_known_at)
    conn.row_factory = sqlite3.Row
    if "InputHistoryStatus" not in _tables(conn):
        raise HistoryError("this database has no input history; run `python scripts/dedup_admin.py input-history` "
                           "(or `python pipeline.py init`) to start recording it")
    statuses = {r["source"]: r for r in conn.execute("SELECT * FROM InputHistoryStatus")}
    result = HistoryReconstruction(str(as_known_at), cutoff, cutoff_date)
    for src in SOURCES:
        if src.table not in _tables(conn):
            continue
        if src.name not in statuses:
            raise HistoryError(f"{src.table} has no recorded history; run `python scripts/dedup_admin.py input-history`")
        result.sources.append(_rewind_source(conn, src, cutoff, statuses[src.name]))
    for table, column in TIMESTAMPED_LEDGERS.items():
        result.ledgers.append(_rewind_ledger(conn, table, column, cutoff))
    return result


def rewind_database(path: Path, as_known_at: Union[str, date, datetime]) -> HistoryReconstruction:
    """:func:`rewind` on the database file ``path`` (a working copy), committed."""
    with closing(sqlite3.connect(str(path))) as conn:
        try:
            result = rewind(conn, as_known_at)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return result


def status(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Per source: tracking start, tracked columns and history rows by kind."""
    conn.row_factory = sqlite3.Row
    out = []
    if "InputHistoryStatus" not in _tables(conn):
        return out
    for row in conn.execute("SELECT * FROM InputHistoryStatus ORDER BY source"):
        src = SOURCE_BY_NAME[row["source"]]
        counts = {r[0]: r[1] for r in conn.execute(f"SELECT change, COUNT(*) FROM {src.history} GROUP BY change")}
        out.append({"source": row["source"], "table": src.table, "tracking_since": row["tracking_since"],
                    "baselined_at": row["baselined_at"], "tracked_columns": json.loads(row["columns_json"]),
                    "rows_by_change": counts})
    return out
