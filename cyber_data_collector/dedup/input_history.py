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
dated after the cutoff (:data:`LEDGERS`), always - a cutoff before a ledger's first row drops every row, and what
the ledger held then is recorded as unknown. ``EntityMerges`` and ``DedupSnapshots`` hold the state immediately
*before* one change but not since when it held, and not every change writes one, so they cannot date a state;
their effects are in the history tables from the moment tracking began.

**Everything is compared in UTC.** ``known_at`` is UTC from the database clock. Ledger writers now stamp
``utils.timestamps.utc_now_iso()`` (explicit ``+00:00``); older ledger stamps have no offset and were written
either by ``CURRENT_TIMESTAMP`` (UTC) or by ``datetime.now()`` (local time), which :data:`LEDGERS` declares per
table. An explicit offset is always honoured; a naive stamp is read by its table's convention, local time being
converted with the local-time rules for that date. Stored values are never rewritten.

**History before tracking began cannot be invented, and is not stood in for silently.** The first time a source
is tracked its current state is written as ``baseline`` rows (``backfilled = 1``, ``known_at`` = that moment) and
``InputHistoryStatus`` records ``tracking_since``. A key with no record at or before the cutoff whose first row is
a baseline has *unknown* state at the cutoff: it keeps the baseline state as a stand-in, and the reconstruction
lists it in ``InputHistoryUnknown`` in the working copy (so do keys never recorded, columns tracked only later,
and ledger rows that cannot be dated), so an analysis can exclude or flag what rests on it
(:func:`unknown_victim_rows`). A key whose first row is a genuine ``insert`` did not exist before it, whatever
the cutoff.

Ledger tables and the four history tables are created, and the triggers (re)installed, by
:func:`ensure_schema` - idempotent, cheap when nothing changed, and safe on a database without these tables.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, tzinfo
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union

from cyber_data_collector.utils.timestamps import NAIVE_LOCAL, NAIVE_UTC, local_zone_label, parse_stamp, to_utc

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

@dataclass(frozen=True)
class Ledger:
    """A decision ledger with a timestamp per row, and how its stamps were written.

    ``naive``: how a stamp WITHOUT an offset is read - ``'utc'``, ``'local'`` (the writing machine's local time)
    or ``'ambiguous'`` (writers of both kinds; a stamp is known only if both readings fall on one side of the
    cutoff). Every writer now stamps ``utils.timestamps.utc_now_iso()`` (explicit ``+00:00``), which is honoured
    whatever ``naive`` says. ``replaces``: rows are replaced or updated in place, so a row dated after the cutoff
    may have replaced a decision that held at it, which is then unknown.
    """

    table: str
    column: str
    naive: str
    replaces: bool
    why: str


#: The one place the timestamp convention of each ledger is declared. Rows dated after the cutoff were not known
#: at it and are dropped. A ledger is trusted as history only from its first row: a cutoff earlier than that drops
#: every row, and what the ledger held then is recorded as unknown.
LEDGERS: Tuple[Ledger, ...] = (
    Ledger("VictimAttributionActions", "created_at", NAIVE_LOCAL, False,
           "victim_judgement.apply_actions wrote datetime.now().isoformat() (local, microseconds)"),
    Ledger("VictimFillActions", "created_at", NAIVE_LOCAL, False,
           "victim_fill.apply_fills wrote datetime.now().isoformat() (local, microseconds)"),
    Ledger("EntityAliases", "created_at", NAIVE_UTC, True,
           "written by the column's DEFAULT CURRENT_TIMESTAMP (UTC); learned aliases are upserted, rule aliases "
           "re-sourced or deleted in place"),
    Ledger("ContextualEntityAliases", "created_at", NAIVE_LOCAL, False,
           "contextual_aliases.add_contextual_alias wrote datetime.now().isoformat(timespec='seconds') (local)"),
    Ledger("EntityMentionResolutions", "decided_at", NAIVE_LOCAL, True,
           "contextual_aliases._record wrote datetime.now() (local) with INSERT OR REPLACE"),
    Ledger("DedupOverrides", "created_at", NAIVE_UTC, True,
           "DEFAULT CURRENT_TIMESTAMP and an upsert setting CURRENT_TIMESTAMP (UTC); a new ruling replaces the old"),
    Ledger("ModelJudgements", "created_at", NAIVE_LOCAL, False,
           "jev, escalation and event_organisations wrote datetime.now().isoformat() (local, microseconds)"),
)
LEDGER_BY_TABLE = {l.table: l for l in LEDGERS}
#: table -> timestamp column (kept for callers that only need the column).
TIMESTAMPED_LEDGERS: Dict[str, str] = {l.table: l.column for l in LEDGERS}

#: Unknown at the cutoff: recorded in the reconstructed copy (``InputHistoryUnknown``) for the analyses to
#: exclude or flag, never passed off as known.
UNKNOWN_SCHEMA = """
CREATE TABLE InputHistoryUnknown (
    unknown_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,      -- history source ('events', 'membership', 'links', 'entities') or 'ledger'
    table_name   TEXT NOT NULL,
    key_json     TEXT,               -- the row's key, {column: value}; NULL = the table as a whole
    columns_json TEXT,               -- the columns whose value is unknown; NULL = the row's existence and state
    reason       TEXT NOT NULL
)
"""
REASON_BEFORE_FIRST_RECORD = ("predates its first record: existence and state at the cutoff unknown; "
                              "the state recorded when tracking began stands in")
REASON_NEVER_RECORDED = "never recorded in the history: existence and state at the cutoff unknown; the current state stands in"
REASON_COLUMN_LATER = "column tracked only after this state was recorded: value at the cutoff unknown; the current value stands in"
REASON_LEDGER_BEFORE_FIRST_ROW = "cutoff before the ledger's first row: what it held at the cutoff is unknown (every row dropped)"
REASON_REPLACED = ("dated after the cutoff in a ledger that replaces rows in place: dropped; an earlier decision for "
                   "this key may have held at the cutoff and is unknown")
REASON_AMBIGUOUS_STAMP = ("stamp without an offset in a ledger with mixed writers reads as before the cutoff one way "
                          "and after it the other: dropped")
REASON_UNREADABLE_STAMP = "stamp missing or unreadable: when it became known is unknown (dropped)"

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
    """``(cutoff stamp, cutoff date)``, both UTC, for a date, a datetime or their text.

    A date means the end of that UTC day (everything stamped on it is known). A datetime is taken exactly: one
    with an offset (``2026-06-30T08:30+10:00``, ``...Z``) is converted to UTC, one without is read as UTC. The
    stamp is ``YYYY-MM-DD HH:MM:SS`` (how ``known_at`` is stamped); the date is the cutoff's UTC date.
    """
    if isinstance(value, datetime):
        moment: Optional[datetime] = value
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day, 23, 59, 59)
    else:
        text = str(value).strip()
        if len(text) == 10:
            try:
                moment = datetime.strptime(text, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            except ValueError:
                moment = None
        else:
            moment = parse_stamp(text)
        if moment is None:
            raise ValueError("--as-known-at must be YYYY-MM-DD (end of that UTC day) or YYYY-MM-DD HH:MM[:SS] "
                             f"(UTC, or with an offset such as +10:00), got {value!r}")
    moment = to_utc(moment, NAIVE_UTC).replace(tzinfo=None, microsecond=0)
    return moment.strftime(STAMP_FORMAT), moment.date()


def _cutoff_moment(cutoff: str) -> datetime:
    return datetime.strptime(cutoff, STAMP_FORMAT).replace(tzinfo=timezone.utc)


def _readings(moment: Optional[datetime], naive: str, local_zone: Optional[tzinfo]) -> Optional[List[datetime]]:
    """Every UTC moment a parsed ledger stamp can mean (one, or two for an ambiguous one); ``None``: unreadable.

    Compared at second resolution, as the history tables stamp ``known_at``.
    """
    if moment is None:
        return None
    if moment.tzinfo is not None:
        readings = [to_utc(moment, NAIVE_UTC)]
    elif naive == "ambiguous":
        readings = [to_utc(moment, NAIVE_UTC), to_utc(moment, NAIVE_LOCAL, local_zone)]
    else:
        readings = [to_utc(moment, naive, local_zone)]
    return [r.replace(microsecond=0) for r in readings]


# --- reconstruction ----------------------------------------------------------------------------------------------

@dataclass
class SourceReport:
    name: str
    table: str
    label: str
    tracking_since: str
    mode: str                                   # 'history' | 'before tracking began'
    keys_in_history: int = 0
    unchanged: int = 0
    changed: int = 0                            # rows whose reconstructed state differs from the working copy
    removed: int = 0                            # rows that did not exist yet and were removed
    restored: int = 0                           # rows deleted since, put back
    fallback_keys: int = 0                      # predate their first record: unknown, the baseline state stands in
    untracked_keys: int = 0                     # present now, never recorded: unknown, the current state stands in
    fallback_columns: Dict[str, int] = field(default_factory=dict)   # tracked later than the row: unknown value

    @property
    def unknown_rows(self) -> int:
        """Rows whose existence and state at the cutoff are unknown (columns tracked later not counted)."""
        return self.fallback_keys + self.untracked_keys

    @property
    def exact(self) -> bool:
        """True when every row and column came from recorded history and nothing is unknown at the cutoff."""
        return self.mode == "history" and not self.unknown_rows and not self.fallback_columns

    def verdict(self) -> str:
        if self.exact:
            return f"reconstructed from history (recorded since {self.tracking_since} UTC)"
        unknown = (f"{self.unknown_rows} of {self.keys_in_history + self.untracked_keys} row(s) have unknown state at "
                   "the cutoff (marked in `InputHistoryUnknown`")
        stand_in = "; the state recorded when tracking began, or the current state, stands in)"
        if self.mode == "history":
            return f"reconstructed from history (recorded since {self.tracking_since} UTC); {unknown}{stand_in}"
        return f"history is recorded only since {self.tracking_since} UTC: {unknown}{stand_in}"


@dataclass
class LedgerReport:
    table: str
    naive: str                                  # how a stamp without an offset was read (see LEDGERS)
    first_row: Optional[str]                    # earliest stamp, UTC
    mode: str                                   # 'filtered' | 'filtered; cutoff before the first row ...' | 'empty' | 'absent'
    rows_dropped: int = 0                       # dated after the cutoff, or not datable (below)
    rows_kept: int = 0
    rows_unknown: int = 0                       # dropped because the stamp is unreadable or ambiguous at the cutoff
    rows_replaced_unknown: int = 0              # dropped from a ledger that replaces rows in place (see REASON_REPLACED)
    rows_with_offset: int = 0                   # stamps carrying an explicit offset (honoured)
    cutoff_before_first_row: bool = False
    note: str = ""


@dataclass
class HistoryReconstruction:
    """What an ``--as-known-at`` reconstruction did, for the report and ``analysis_inputs.json``."""

    as_known_at: str
    cutoff: str
    cutoff_date: date
    local_zone: str = ""
    sources: List[SourceReport] = field(default_factory=list)
    ledgers: List[LedgerReport] = field(default_factory=list)
    unknown: Dict[str, Dict[str, int]] = field(default_factory=dict)   # table -> reason -> rows (InputHistoryUnknown)

    @property
    def fallback_fields(self) -> List[str]:
        """Labels of the inputs with state unknown at the cutoff (history did not reach it for every row)."""
        return [s.label for s in self.sources if not s.exact]

    @property
    def unknown_total(self) -> int:
        return sum(sum(v.values()) for v in self.unknown.values())

    def to_dict(self) -> Dict[str, Any]:
        return {"as_known_at": self.as_known_at, "cutoff_utc": self.cutoff,
                "naive_local_stamps_read_as": self.local_zone,
                "reconstructed_from_current_state": self.fallback_fields,
                "unknown_at_cutoff": self.unknown, "unknown_rows_table": "InputHistoryUnknown",
                "sources": [{**s.__dict__, "unknown_rows": s.unknown_rows, "verdict": s.verdict()}
                            for s in self.sources],
                "ledgers": [l.__dict__ for l in self.ledgers]}

    def markdown(self) -> str:
        lines = [f"## Inputs as known at {self.as_known_at}", "",
                 f"The incident links, dates, victims and size bands below were reconstructed from the recorded history "
                 f"of what was known by {self.cutoff} UTC (`--as-known-at`; `InputHistoryStatus`). A row the history "
                 "cannot place at that moment (it predates its first record, or a column was tracked only later) is "
                 "**unknown**, not known: it holds the state recorded when tracking began (or the current value) as a "
                 "stand-in and is listed in `InputHistoryUnknown` in the reconstructed copy, so an analysis can flag "
                 "or exclude it.", "",
                 "| Input | Basis | Rows reconstructed | Unknown at the cutoff | Removed (not yet known) | Restored | Changed |",
                 "|---|---|---:|---:|---:|---:|---:|"]
        for s in self.sources:
            basis = "history" if s.exact else "**partly unknown**"
            lines.append(f"| {s.label} (`{s.table}`) | {basis}; {s.verdict()} | {s.keys_in_history} | {s.unknown_rows} | "
                         f"{s.removed} | {s.restored} | {s.changed} |")
        lines += ["", f"Decision ledgers are filtered by their own timestamps, compared in UTC. A stamp with an offset is "
                  f"honoured; one without is read by the convention its writer used (UTC for `CURRENT_TIMESTAMP` "
                  f"defaults, local time - here {self.local_zone} - for `datetime.now()`).", "",
                  "| Decision ledger | Stamps without offset | Basis | Rows dropped | of which unknown | Kept |",
                  "|---|---|---|---:|---:|---:|"]
        for l in self.ledgers:
            first = "" if l.first_row is None else f" (first row {l.first_row} UTC)"
            lines.append(f"| `{l.table}` | {l.naive} | {l.mode}{first} | {l.rows_dropped} | "
                         f"{l.rows_unknown + l.rows_replaced_unknown} | {l.rows_kept} |")
        columns = {f"{s.table}.{c}": n for s in self.sources for c, n in s.fallback_columns.items()}
        if columns:
            lines += ["", "Columns tracked only after some rows were recorded are unknown for those rows (the current "
                      "value stands in): " + ", ".join(f"`{c}` ({n})" for c, n in sorted(columns.items())) + "."]
        if self.unknown:
            lines += ["", f"**Unknown at the cutoff: {self.unknown_total} row(s)** (`InputHistoryUnknown`): "
                      + "; ".join(f"`{t}` {sum(r.values())}" for t, r in sorted(self.unknown.items())) + "."]
        if self.fallback_fields:
            lines += ["", "**Limitation.** History does not reach the cutoff for every row of: "
                      + "; ".join(self.fallback_fields) + ". Their state then cannot be reconstructed; it is marked "
                      "unknown rather than inferred."]
        return "\n".join(lines) + "\n"


def _state_at(rows: List[sqlite3.Row], cutoff: str) -> Tuple[Optional[sqlite3.Row], str]:
    """``(state row or None for 'does not exist', basis)`` for one key's history, oldest first.

    basis: 'recorded' (a row at or before the cutoff decides it), 'absent' (first row is a genuine insert after
    the cutoff: it did not exist), 'earliest' (only later rows exist and the first is a baseline: the state
    before it is unknown; the earliest recorded state stands in and the key is recorded as unknown).
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


def _key_json(columns: Iterable[str], values: Iterable[Any]) -> str:
    return json.dumps(dict(zip(columns, values)), sort_keys=True, default=str)


UnknownRow = Tuple[str, str, Optional[str], Optional[str], str]


def _rewind_source(conn: sqlite3.Connection, src: Source, cutoff: str, status: sqlite3.Row,
                   unknown: List[UnknownRow]) -> SourceReport:
    since = status["tracking_since"]
    columns_since = json.loads(status["columns_json"])
    report = SourceReport(src.name, src.table, src.label, since, "history" if cutoff >= since else
                          "before tracking began")
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
            unknown.append((src.name, src.table, _key_json(src.key, key), None, REASON_BEFORE_FIRST_RECORD))
        now = current.get(key)
        if state is None:
            if now is not None:
                deletes.append(key)
                report.removed += 1
            continue
        values = []
        later: List[str] = []
        for index, column in enumerate(tracked):
            if state["known_at"] < columns_since[column]:
                # The column was not tracked yet when this row was written: its value is unknown then.
                values.append(now[index] if now is not None else None)
                fallback_columns[column] = fallback_columns.get(column, 0) + 1
                later.append(column)
            else:
                values.append(state[column])
        if later and basis != "earliest":       # an 'earliest' key is unknown as a whole already
            unknown.append((src.name, src.table, _key_json(src.key, key), json.dumps(later), REASON_COLUMN_LATER))
        values_t = tuple(values)
        if now is None:
            inserts.append(key + values_t)
            report.restored += 1
        elif now != values_t:
            updates.append(values_t + key)
            report.changed += 1
        else:
            report.unchanged += 1
    untracked = [key for key in current if key not in history]
    report.untracked_keys = len(untracked)
    unknown.extend((src.name, src.table, _key_json(src.key, key), None, REASON_NEVER_RECORDED) for key in untracked)
    report.fallback_columns = fallback_columns
    where = " AND ".join(f"{k} = ?" for k in src.key)
    if deletes:
        conn.executemany(f"DELETE FROM {src.table} WHERE {where}", deletes)
    if updates and tracked:
        # Rows are taken out and put back with their reconstructed values rather than updated one at a time: a
        # chain of moves (a record moved between events and another into its place) passes through states a
        # UNIQUE index forbids, though the state at the cutoff satisfies it. Untracked columns keep their values.
        all_columns = _columns(conn, src.table)
        changed = {u[len(tracked):]: u[:len(tracked)] for u in updates}
        rows_out = []
        for row in conn.execute(f"SELECT {', '.join(all_columns)} FROM {src.table}"):
            key = tuple(row[k] for k in src.key)
            if key in changed:
                values = dict(zip(all_columns, tuple(row)))
                values.update(zip(tracked, changed[key]))
                rows_out.append(tuple(values[c] for c in all_columns))
        conn.executemany(f"DELETE FROM {src.table} WHERE {where}", list(changed))
        conn.executemany(f"INSERT INTO {src.table} ({', '.join(all_columns)}) "
                         f"VALUES ({', '.join('?' * len(all_columns))})", rows_out)
    if inserts:
        conn.executemany(f"INSERT INTO {src.table} ({cols}) VALUES ({', '.join('?' * (keys + len(tracked)))})", inserts)
    return report


def _column_list_plain(src: Source) -> str:
    return ", ".join(list(src.key) + list(src.columns))


def _primary_key(conn: sqlite3.Connection, table: str) -> List[str]:
    pk = sorted((r[5], r[1]) for r in conn.execute(f"PRAGMA table_info({table})") if r[5])
    return [name for _, name in pk] or ["rowid"]


def _rewind_ledger(conn: sqlite3.Connection, ledger: Ledger, cutoff: datetime, local_zone: Optional[tzinfo],
                   unknown: List[UnknownRow]) -> LedgerReport:
    """Drop the rows of ``ledger`` not known at ``cutoff`` (UTC) and record what it cannot say."""
    table, column = ledger.table, ledger.column
    if table not in _tables(conn) or column not in _columns(conn, table):
        return LedgerReport(table, ledger.naive, None, "absent", note=ledger.why)
    keys = _primary_key(conn, table)
    rows = conn.execute(f"SELECT rowid, {column}, {', '.join(keys)} FROM {table}").fetchall()
    report = LedgerReport(table, ledger.naive, None, "filtered", note=ledger.why)
    if not rows:
        report.mode = "empty"
        return report
    verdicts: List[Tuple[int, str, Optional[str]]] = []      # (rowid, key_json, why dropped: None = known)
    first: Optional[datetime] = None
    for row in rows:
        moment = parse_stamp(row[1])
        readings = _readings(moment, ledger.naive, local_zone)
        key = _key_json(keys, tuple(row)[2:])
        if readings is None:
            verdicts.append((row[0], key, REASON_UNREADABLE_STAMP))
            continue
        report.rows_with_offset += int(moment.tzinfo is not None)
        first = min(readings) if first is None else min(first, *readings)
        if all(r <= cutoff for r in readings):
            verdicts.append((row[0], key, None))
        elif all(r > cutoff for r in readings):
            verdicts.append((row[0], key, "later"))
        else:
            verdicts.append((row[0], key, REASON_AMBIGUOUS_STAMP))
    report.first_row = None if first is None else first.strftime(STAMP_FORMAT)
    report.cutoff_before_first_row = first is not None and cutoff < first
    drop = []
    for rowid, key, why in verdicts:
        if why is None:
            continue
        drop.append((rowid,))
        if why != "later":
            report.rows_unknown += 1
            unknown.append(("ledger", table, key, None, why))
        elif ledger.replaces and not report.cutoff_before_first_row:
            report.rows_replaced_unknown += 1
            unknown.append(("ledger", table, key, None, REASON_REPLACED))
    if report.cutoff_before_first_row:
        # Every row is dated after the cutoff; what the ledger held before it began recording cannot be said.
        report.mode = "filtered; cutoff before the first row: what the ledger held then is unknown"
        unknown.append(("ledger", table, None, None, REASON_LEDGER_BEFORE_FIRST_ROW))
    conn.executemany(f"DELETE FROM {table} WHERE rowid = ?", drop)
    report.rows_dropped = len(drop)
    report.rows_kept = len(rows) - len(drop)
    return report


def rewind(conn: sqlite3.Connection, as_known_at: Union[str, date, datetime],
           local_zone: Optional[tzinfo] = None) -> HistoryReconstruction:
    """Rewrite ``conn``'s database in place to the state known at ``as_known_at``. Use a COPY.

    What cannot be placed at the cutoff is written to ``InputHistoryUnknown`` (see :data:`UNKNOWN_SCHEMA`;
    :func:`unknown_victim_rows` and :func:`unknown_summary` read it). Ledger stamps written in local time without
    an offset are converted with ``local_zone`` (default: the system's local-time rules). Raises
    :class:`HistoryError` when no history was ever recorded (run ``dedup_admin.py input-history``). The caller
    commits (or rolls back); nothing is committed here.
    """
    cutoff, cutoff_date = parse_as_known_at(as_known_at)
    conn.row_factory = sqlite3.Row
    if "InputHistoryStatus" not in _tables(conn):
        raise HistoryError("this database has no input history; run `python scripts/dedup_admin.py input-history` "
                           "(or `python pipeline.py init`) to start recording it")
    statuses = {r["source"]: r for r in conn.execute("SELECT * FROM InputHistoryStatus")}
    result = HistoryReconstruction(str(as_known_at), cutoff, cutoff_date, local_zone_label(local_zone))
    unknown: List[UnknownRow] = []
    for src in SOURCES:
        if src.table not in _tables(conn):
            continue
        if src.name not in statuses:
            raise HistoryError(f"{src.table} has no recorded history; run `python scripts/dedup_admin.py input-history`")
        result.sources.append(_rewind_source(conn, src, cutoff, statuses[src.name], unknown))
    moment = _cutoff_moment(cutoff)
    for ledger in LEDGERS:
        result.ledgers.append(_rewind_ledger(conn, ledger, moment, local_zone, unknown))
    conn.execute("DROP TABLE IF EXISTS InputHistoryUnknown")
    conn.execute(UNKNOWN_SCHEMA)
    conn.executemany("INSERT INTO InputHistoryUnknown (source, table_name, key_json, columns_json, reason) "
                     "VALUES (?, ?, ?, ?, ?)", unknown)
    result.unknown = unknown_summary(conn)
    return result


def rewind_database(path: Path, as_known_at: Union[str, date, datetime],
                    local_zone: Optional[tzinfo] = None) -> HistoryReconstruction:
    """:func:`rewind` on the database file ``path`` (a working copy), committed."""
    with closing(sqlite3.connect(str(path))) as conn:
        try:
            result = rewind(conn, as_known_at, local_zone)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return result


# --- reading what is unknown -----------------------------------------------------------------------------------

def is_reconstruction(conn: sqlite3.Connection) -> bool:
    """True when ``conn`` is a working copy :func:`rewind` reconstructed (it holds ``InputHistoryUnknown``)."""
    return "InputHistoryUnknown" in _tables(conn)


def unknown_summary(conn: sqlite3.Connection) -> Dict[str, Dict[str, int]]:
    """``{table: {reason: rows}}`` from ``InputHistoryUnknown``; empty when ``conn`` is not a reconstruction."""
    if not is_reconstruction(conn):
        return {}
    out: Dict[str, Dict[str, int]] = {}
    for table, reason, n in conn.execute("SELECT table_name, reason, COUNT(*) FROM InputHistoryUnknown "
                                         "GROUP BY table_name, reason ORDER BY table_name, reason"):
        out.setdefault(table, {})[reason] = n
    return out


def _as_entity(value: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def unknown_victim_rows(conn: sqlite3.Connection,
                        victims: Iterable[Tuple[Any, Any]]) -> Dict[Tuple[Any, str], List[str]]:
    """Which ``(entity_id, deduplicated_event_id)`` victim rows rest on an input unknown at the cutoff, and why.

    A row is unknown when its incident's state, its victim link, its organisation's state or any of its
    incident's records (membership) is listed in ``InputHistoryUnknown``. Empty when ``conn`` is not a
    reconstruction. Ledger-level unknowns (a whole table, a dropped row) are not tied to a victim row; they are
    in :func:`unknown_summary`.
    """
    wanted = {(_as_entity(e), str(d)) for e, d in victims}
    if not wanted or not is_reconstruction(conn):
        return {}
    events: Set[str] = set()
    links: Set[Tuple[str, Any]] = set()
    entities: Set[Any] = set()
    maps: Set[str] = set()
    for source, key_json in conn.execute("SELECT source, key_json FROM InputHistoryUnknown "
                                         "WHERE source != 'ledger' AND key_json IS NOT NULL"):
        key = json.loads(key_json)
        if source == "events":
            events.add(str(key["deduplicated_event_id"]))
        elif source == "links":
            links.add((str(key["deduplicated_event_id"]), _as_entity(key["entity_id"])))
        elif source == "entities":
            entities.add(_as_entity(key["entity_id"]))
        elif source == "membership":
            maps.add(str(key["map_id"]))
    membership: Set[str] = set()
    if maps and "EventDeduplicationMap" in _tables(conn):
        membership = {str(r[1]) for r in conn.execute("SELECT map_id, deduplicated_event_id FROM EventDeduplicationMap")
                      if str(r[0]) in maps and r[1] is not None}
    out: Dict[Tuple[Any, str], List[str]] = {}
    for entity, event in wanted:
        why = [label for label, hit in (("incident state", event in events),
                                        ("victim link", (event, entity) in links),
                                        ("organisation state", entity in entities),
                                        ("incident membership", event in membership)) if hit]
        if why:
            out[(entity, event)] = why
    return out


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
