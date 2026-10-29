"""Durable retry queue for discovered records that were stored but not processed.

A record discovery stores in ``RawEvents`` is finished only when it has been
classified (and stored as an enriched event, or rejected by the classifier) or
rejected by a filter. A classification timeout left it ``is_processed = 0``,
but a refresh only processed records it *rediscovered* - and OAIC skips URLs it
has already stored - so one timeout could strand an incident for good.

Every discovery run now first drains this queue: every stored record that is
neither processed nor rejected, and whose back-off has expired, goes through
the same scrape -> page gate -> classify -> store path as a new record.

State per record lives in ``DiscoveryRecordState``:

* the **source and discovery month** it was found under, so completion can be
  reported per source and month (view ``SourceMonthCompletion``);
* **temporary** failures (timeouts, 429/5xx, connection errors, API-budget
  refusals): counted, backed off, and never a reason to give up - the provider,
  not the record, failed;
* **permanent** failures (the record itself cannot be processed, *recognised*
  as such - malformed model output for its content, a request the provider
  refuses as invalid, a store that fails validation or an integrity rule):
  counted, backed off, and after :data:`MAX_PERMANENT_ATTEMPTS` the record is
  quarantined - marked processed with ``Rejected at retry_quarantine: ...`` so
  it leaves every queue - and reported. ``python scripts/discovery_queue.py
  release`` puts quarantined records back;
* **unknown** failures (an exception nothing recognises as the provider's or
  the record's fault - an unexpected library error, a code bug): not assumed
  permanent, so never quarantined. Retried with a long back-off (6 hours
  doubling to a week) and logged at ERROR every time; the status command and
  the quality report count them. They used to be classed permanent and
  quarantined after three tries, which hid a bug behind a "bad record".

A failed *store* keeps its exception: :class:`StorageError` carries the stage
(``store_raw`` / ``store_enriched`` / ``mark_processed``), the kind and the
message of what failed. A locked or busy database, a disk I/O error or a full
disk is the store failing, not the record, so it is temporary and never counts
toward quarantine; an integrity or validation error is the record's and is
permanent. A record quarantined before that distinction, whose stored error
identifies a temporary cause, is released automatically
(:meth:`RetryQueue.release_temporary_storage_quarantines`, called by every
discovery run); one whose error text does not say why (the old generic
"enriched event could not be stored") is released with ``release``.

The queue itself is ``RawEvents.is_processed = 0``: state rows only add
back-off and history, so a record from a run that crashed before any state was
written is still drained.

Each source's search of each month is recorded in ``SourceMonthDiscovery``
with the error type of every failure (:mod:`~cyber_data_collector.processing.fetch_errors`)
and its class (permanent / temporary / unknown). A failed search of a month
discovery has already processed (a backfill month) is searched again on a later
run after a back-off, up to :data:`MAX_SOURCE_MONTH_ATTEMPTS` consecutive
failures, then:

* a **permanent** failure (404/410) is *given up* at once: never searched again
  - by a probe or a configuration change - until ``python scripts/discovery_queue.py
  release`` (or ``backfill --force``), and reported;
* a **temporary** or **unknown** one is *parked*, not abandoned. The source is
  probed - one parked month searched after :data:`SOURCE_PROBE_INTERVAL`, then
  at doubling intervals while the probe fails - and a successful probe reopens
  every parked month of that source. A parked month also reopens when the
  configuration it failed under changes (the source's API key, query version or
  client version, stored with the give-up; see
  :mod:`~cyber_data_collector.processing.source_config`). Nothing waits for a
  person; ``status`` and the quality report show what is parked, why, when the
  next probe is due and what would reopen it.
"""
from __future__ import annotations

import asyncio
import errno
import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

from cyber_data_collector.processing import source_config
from cyber_data_collector.processing.fetch_errors import (
    CLASS_PERMANENT, UNKNOWN, deciding_type, error_type_from_text, failure_class,
)
from cyber_data_collector.processing.record_evidence import REJECTION_PREFIX, format_rejection
from cyber_data_collector.utils.api_budget import is_budget_refusal

logger = logging.getLogger(__name__)

STAGE_RETRY_QUARANTINE = "retry_quarantine"

KIND_TEMPORARY = "temporary"
KIND_PERMANENT = "permanent"
#: Neither recognised as the provider's fault nor as the record's: retried with a long back-off, never quarantined.
KIND_UNKNOWN = "unknown"

#: Permanent failures a record may have before it is quarantined.
MAX_PERMANENT_ATTEMPTS = 3
#: Back-off after the n-th temporary failure: 15 min, 30 min, 1 h ... capped.
TEMPORARY_BACKOFF_BASE = timedelta(minutes=15)
TEMPORARY_BACKOFF_CAP = timedelta(hours=12)
#: Back-off after the n-th permanent failure: 1 h, 2 h, 4 h ... capped.
PERMANENT_BACKOFF_BASE = timedelta(hours=1)
PERMANENT_BACKOFF_CAP = timedelta(days=7)
#: Back-off after the n-th unknown failure of a record: 6 h, 12 h, 1 d ... capped at a week.
UNKNOWN_BACKOFF_BASE = timedelta(hours=6)
UNKNOWN_BACKOFF_CAP = timedelta(days=7)
#: Consecutive failed searches of one source-month before it is parked.
MAX_SOURCE_MONTH_ATTEMPTS = 5
#: Back-off before searching a failed source-month again: 30 min, 1 h, 2 h ... capped.
SOURCE_MONTH_BACKOFF_BASE = timedelta(minutes=30)
SOURCE_MONTH_BACKOFF_CAP = timedelta(days=1)
#: The same for an unknown failure: 6 h, 12 h, 1 d ... capped at a week.
UNKNOWN_MONTH_BACKOFF_BASE = timedelta(hours=6)
UNKNOWN_MONTH_BACKOFF_CAP = timedelta(days=7)
#: A source with parked months is probed (one parked month searched) after this long...
SOURCE_PROBE_INTERVAL = timedelta(days=7)
#: ...and, while the probe keeps failing, after twice as long each time up to this.
SOURCE_PROBE_CAP = timedelta(days=28)

#: Search states of one source-month (:meth:`RetryQueue.source_month_retry`).
MONTH_NEVER = "never"          # not searched yet
MONTH_COMPLETE = "complete"    # last search lost nothing
MONTH_DUE = "due"              # last search failed; back-off over, search again
MONTH_WAITING = "waiting"      # last search failed; inside its back-off
MONTH_GAVE_UP = "gave_up"      # permanent failure (404/410): never searched again until released
MONTH_PARKED = "parked"        # MAX_SOURCE_MONTH_ATTEMPTS temporary/unknown failures: probed, reopened automatically

SCHEMA = """
CREATE TABLE IF NOT EXISTS DiscoveryRecordState (
    raw_event_id         TEXT PRIMARY KEY,
    source_type          TEXT,
    discovery_year       INTEGER,
    discovery_month      INTEGER,
    attempts             INTEGER NOT NULL DEFAULT 0,  -- permanent failures (count toward quarantine)
    temporary_failures   INTEGER NOT NULL DEFAULT 0,  -- provider failures (never count toward quarantine)
    unknown_failures     INTEGER NOT NULL DEFAULT 0,  -- unrecognised failures (long back-off, never quarantine)
    last_stage           TEXT,
    last_error           TEXT,
    last_error_kind      TEXT,                        -- 'temporary' / 'permanent' / 'unknown'
    last_error_type      TEXT,                        -- fetch_errors type: throttled / network / ...
    first_failed_at      TEXT,
    last_attempt_at      TEXT,
    next_attempt_at      TEXT,                        -- NULL = due now
    quarantined_at       TEXT,
    completed_at         TEXT,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_discovery_state_month
    ON DiscoveryRecordState (source_type, discovery_year, discovery_month);
CREATE TABLE IF NOT EXISTS SourceMonthDiscovery (
    source_type     TEXT NOT NULL,
    year            INTEGER NOT NULL,
    month           INTEGER NOT NULL,
    status          TEXT NOT NULL,      -- 'complete' / 'failed' (results lost: searched again next run)
    events_found    INTEGER NOT NULL DEFAULT 0,
    failures_json   TEXT,               -- what failed, as noted by the data source
    attempted_at    TEXT NOT NULL,
    completed_at    TEXT,               -- last time the source-month finished without failure
    error_type      TEXT,               -- the type deciding the retry (fetch_errors.deciding_type)
    error_types_json TEXT,              -- the type of each entry of failures_json
    failed_attempts INTEGER NOT NULL DEFAULT 0,  -- consecutive failed searches (0 after a complete one)
    next_attempt_at TEXT,               -- a failed month is not searched again before this
    gave_up_at      TEXT,               -- parked (temporary/unknown cap reached) or given up (permanent)
    items_seen      INTEGER,            -- items the source listed for the month (Cyber Daily: feed articles)
    give_up_reason  TEXT,               -- the failure class that parked it: 'permanent' / 'temporary' / 'unknown'
    give_up_config  TEXT,               -- the source configuration it failed under (source_config.encode)
    reopened_at     TEXT,               -- last time it was reopened without being released by hand
    reopen_reason   TEXT,               -- 'probe succeeded' / 'configuration changed: <components>'
    PRIMARY KEY (source_type, year, month)
);
CREATE TABLE IF NOT EXISTS SourceProbes (
    source_type          TEXT PRIMARY KEY,
    last_probe_at        TEXT,
    next_probe_at        TEXT,           -- the next probe of the source's parked months is due then
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error_type      TEXT,
    last_success_at      TEXT
);
"""

#: Recreated on every ensure_schema: its failing count includes unknown failures, which older databases lack.
VIEW = """
DROP VIEW IF EXISTS SourceMonthCompletion;
CREATE VIEW IF NOT EXISTS SourceMonthCompletion AS
SELECT s.source_type,
       s.discovery_year,
       s.discovery_month,
       COUNT(*) AS stored,
       SUM(CASE WHEN COALESCE(r.is_processed, 0) != 0
                 AND COALESCE(r.processing_error, '') NOT LIKE 'Rejected at %' THEN 1 ELSE 0 END) AS processed,
       SUM(CASE WHEN COALESCE(r.is_processed, 0) != 0
                 AND r.processing_error LIKE 'Rejected at %'
                 AND r.processing_error NOT LIKE 'Rejected at retry_quarantine%' THEN 1 ELSE 0 END) AS rejected,
       SUM(CASE WHEN COALESCE(r.is_processed, 0) = 0 THEN 1 ELSE 0 END) AS pending,
       SUM(CASE WHEN COALESCE(r.is_processed, 0) = 0
                 AND (s.attempts + s.temporary_failures + s.unknown_failures) > 0 THEN 1 ELSE 0 END) AS failing,
       SUM(CASE WHEN COALESCE(r.is_processed, 0) != 0
                 AND r.processing_error LIKE 'Rejected at retry_quarantine%' THEN 1 ELSE 0 END) AS quarantined
FROM DiscoveryRecordState s
JOIN RawEvents r ON r.raw_event_id = s.raw_event_id
GROUP BY s.source_type, s.discovery_year, s.discovery_month;
"""

_TEMPORARY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}
_TEMPORARY_NAMES = {
    "TimeoutError", "APITimeoutError", "APIConnectionError", "RateLimitError", "InternalServerError",
    "ServiceUnavailableError", "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
    "PoolTimeout", "ReadError", "RemoteProtocolError", "NetworkError", "TimeoutException",
    "ConnectionError", "ConnectionResetError", "ConnectionAbortedError", "ConnectionRefusedError",
    "BrokenPipeError", "BudgetExhausted", "PageGateUnavailable", "OperationalError", "BlockingIOError",
    "InterruptedError",
}
_TEMPORARY_TEXT = ("timed out", "timeout", "rate limit", "temporarily", "overloaded", "connection reset",
                   "connection aborted", "connection refused", "service unavailable", "bad gateway",
                   "gateway timeout", "too many requests", "database is locked", "database is busy",
                   "database table is locked", "disk i/o error", "disk is full", "unable to open database file",
                   "sqlite_busy", "operationalerror")
#: OS errors that pass: an I/O hiccup, a busy or full disk, too many open files, an interrupted call.
_TEMPORARY_ERRNO = {errno.EIO, errno.EBUSY, errno.EAGAIN, errno.EINTR, errno.ENOSPC, errno.EMFILE, errno.ENFILE,
                    errno.ETIMEDOUT}
#: What the store returns for a record it will never accept (or a statement that cannot be run for it).
_PERMANENT_STORE_ERRORS = (sqlite3.IntegrityError, sqlite3.DataError)
#: Statuses that say the request itself is invalid, whoever sends it again: malformed, too large, unprocessable.
#: 401/403 are not here: a credential or a block is cured by changing it, not by the record.
_PERMANENT_STATUS = {400, 404, 410, 413, 414, 415, 422}
#: Class names of validation failures (a pydantic or instructor model rejecting the record's content).
_PERMANENT_NAMES = {"ValidationError", "BadRequestError", "UnprocessableEntityError", "JSONDecodeError"}

#: The stages of :class:`StorageError`.
STAGE_STORE_RAW = "store_raw"
STAGE_STORE_ENRICHED = "store_enriched"
STAGE_MARK_PROCESSED = "mark_processed"
STORAGE_STAGES = (STAGE_STORE_RAW, STAGE_STORE_ENRICHED, STAGE_MARK_PROCESSED)
#: ``"3 permanent failures at store_enriched; last: <error>"``, as written by :meth:`RetryQueue.record_failure`.
_QUARANTINE_REASON = re.compile(r"\d+ permanent failures at (?P<stage>\w+); last: (?P<last>.*)", re.DOTALL)
#: What discovery stored as the error of a failed store before :class:`StorageError` kept the cause.
LEGACY_STORE_REASONS = ("enriched event could not be stored",)


def classify_failure(exc: BaseException) -> str:
    """``'temporary'``, ``'permanent'`` or ``'unknown'`` for a failed attempt.

    Temporary: timeouts, rate limits, 429/5xx, connection errors, a locked or
    busy database, transient I/O errors and API-budget refusals - anything that
    may succeed unchanged later.

    Permanent only when it is *recognised* as the record's own: an integrity,
    data or programming error from the store, a validation failure of the
    record's content (``ValueError``, a pydantic ``ValidationError``, malformed
    JSON, a value SQLite cannot bind) or a request the provider refuses as invalid (400/404/410/413/414/415/422).

    Unknown: everything else. An exception nothing here recognises is not
    evidence about the record - it was classed permanent, so an unexpected
    library error or a code bug quarantined good records after three tries.
    Unknown failures are retried with a long back-off and never quarantine.
    """
    if is_budget_refusal(exc):
        return KIND_TEMPORARY
    seen = set()
    current: Optional[BaseException] = exc
    permanent_seen = False
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, _PERMANENT_STORE_ERRORS):
            return KIND_PERMANENT
        if isinstance(current, sqlite3.ProgrammingError) and "binding parameter" in str(current).lower():
            return KIND_PERMANENT          # a value of the record that SQLite cannot store; other misuse is unknown
        if isinstance(current, (asyncio.TimeoutError, TimeoutError, ConnectionError, sqlite3.OperationalError)):
            return KIND_TEMPORARY
        if isinstance(current, OSError) and current.errno in _TEMPORARY_ERRNO:
            return KIND_TEMPORARY
        names = {cls.__name__ for cls in type(current).__mro__}
        if names & _TEMPORARY_NAMES:
            return KIND_TEMPORARY
        status = getattr(current, "status_code", None)
        if status is None:
            status = getattr(getattr(current, "response", None), "status_code", None)
        if isinstance(status, int) and (status in _TEMPORARY_STATUS or status >= 500):
            return KIND_TEMPORARY
        text = str(current).lower()
        if any(fragment in text for fragment in _TEMPORARY_TEXT):
            return KIND_TEMPORARY
        # A permanent sign counts only if nothing further down the cause chain says the provider failed.
        if isinstance(current, ValueError) or names & _PERMANENT_NAMES or (
                isinstance(status, int) and status in _PERMANENT_STATUS):
            permanent_seen = True
        current = current.__cause__ or current.__context__
    return KIND_PERMANENT if permanent_seen else KIND_UNKNOWN


def has_temporary_cause(text: Optional[str]) -> bool:
    """Whether a stored error message names a temporary cause (a locked database, a timeout ...)."""
    lowered = (text or "").lower()
    return any(fragment in lowered for fragment in _TEMPORARY_TEXT)


class StorageError(RuntimeError):
    """A store call failed; ``kind`` says whether the store (temporary) or the record (permanent) did.

    Raised instead of returning ``None`` so the caller keeps *why*: a locked
    database must not be recorded the way an integrity error is.
    """

    def __init__(self, stage: str, message: str, kind: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.kind = kind

    @classmethod
    def from_exception(cls, stage: str, exc: BaseException) -> "StorageError":
        """The failure of ``stage`` that ``exc`` was, typed by :func:`classify_failure`."""
        return cls(stage, f"{type(exc).__name__}: {exc}"[:500], classify_failure(exc))

    @property
    def is_temporary(self) -> bool:
        return self.kind == KIND_TEMPORARY


def source_month_backoff(count: int, failure: str = KIND_TEMPORARY) -> timedelta:
    """Delay before searching a source-month again after ``count`` consecutive failures of class ``failure``."""
    base, cap = ((UNKNOWN_MONTH_BACKOFF_BASE, UNKNOWN_MONTH_BACKOFF_CAP) if failure == KIND_UNKNOWN
                 else (SOURCE_MONTH_BACKOFF_BASE, SOURCE_MONTH_BACKOFF_CAP))
    return min(cap, base * (2 ** max(0, count - 1)))


def probe_backoff(failures: int) -> timedelta:
    """Delay before a source with parked months is probed again: a week, then 2 and 4 weeks while probes fail."""
    return min(SOURCE_PROBE_CAP, SOURCE_PROBE_INTERVAL * (2 ** max(0, failures)))


def backoff(kind: str, count: int) -> timedelta:
    """Delay before the next attempt after ``count`` failures of ``kind``."""
    base, cap = {KIND_TEMPORARY: (TEMPORARY_BACKOFF_BASE, TEMPORARY_BACKOFF_CAP),
                 KIND_UNKNOWN: (UNKNOWN_BACKOFF_BASE, UNKNOWN_BACKOFF_CAP)}.get(
        kind, (PERMANENT_BACKOFF_BASE, PERMANENT_BACKOFF_CAP))
    return min(cap, base * (2 ** max(0, count - 1)))


_ADDED_COLUMNS = {
    "DiscoveryRecordState": (("last_error_type", "TEXT"), ("unknown_failures", "INTEGER NOT NULL DEFAULT 0")),
    "SourceMonthDiscovery": (("error_type", "TEXT"), ("error_types_json", "TEXT"),
                             ("failed_attempts", "INTEGER NOT NULL DEFAULT 0"), ("next_attempt_at", "TEXT"),
                             ("gave_up_at", "TEXT"), ("items_seen", "INTEGER"), ("give_up_reason", "TEXT"),
                             ("give_up_config", "TEXT"), ("reopened_at", "TEXT"), ("reopen_reason", "TEXT")),
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the tables, add columns later versions need, and type legacy failures.

    Idempotent. A failed source-month recorded before error types existed gets
    its type from its stored message and counts as one failed attempt. A month
    given up before failure classes existed gets the class of its error type
    (``give_up_reason``); its configuration was not recorded, so the first
    ``reopen_changed_config`` after this searches it again once.
    """
    import json

    conn.executescript(SCHEMA)
    for table, added in _ADDED_COLUMNS.items():
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, decl in added:
            if column not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    legacy = conn.execute("SELECT source_type, year, month, failures_json FROM SourceMonthDiscovery "
                          "WHERE status != 'complete' AND error_type IS NULL").fetchall()
    for source_type, year, month, failures_json in legacy:
        try:
            failures = [str(f) for f in json.loads(failures_json or "[]")]
        except ValueError:
            failures = [failures_json or ""]
        types = [error_type_from_text(f) for f in failures] or [UNKNOWN]
        conn.execute("UPDATE SourceMonthDiscovery SET error_type = ?, error_types_json = ?, "
                     "failed_attempts = MAX(failed_attempts, 1) WHERE source_type = ? AND year = ? AND month = ?",
                     (deciding_type(types), json.dumps(types), source_type, year, month))
    for source_type, year, month, error_type in conn.execute(
            "SELECT source_type, year, month, error_type FROM SourceMonthDiscovery "
            "WHERE gave_up_at IS NOT NULL AND give_up_reason IS NULL").fetchall():
        conn.execute("UPDATE SourceMonthDiscovery SET give_up_reason = ? WHERE source_type = ? AND year = ? "
                     "AND month = ?", (failure_class(error_type), source_type, year, month))
    conn.executescript(VIEW)
    conn.commit()


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


class RetryQueue:
    """The queue over one database.

    Args:
        conn: SQLite connection (``RawEvents`` must exist).
        lock: Lock guarding ``conn`` when it is shared between threads (the
            ``CyberEventDataV2`` lock); a private one is used when omitted.
        clock: Returns the current time (tests pass a controllable one).
        configuration: Returns the current configuration of a source as named
            components (:func:`source_config.current_components`); a give-up
            stores it, and a parked month reopens when it no longer matches.
    """

    def __init__(self, conn: sqlite3.Connection, lock: Optional[Any] = None,
                 clock: Callable[[], datetime] = datetime.now,
                 configuration: Callable[[str], Dict[str, str]] = source_config.current_components) -> None:
        self.conn = conn
        self._lock = lock or threading.Lock()
        self.clock = clock
        self.configuration = configuration
        with self._locked() as c:
            ensure_schema(c)

    @classmethod
    def for_db(cls, db: Any, clock: Callable[[], datetime] = datetime.now,
               configuration: Callable[[str], Dict[str, str]] = source_config.current_components) -> "RetryQueue":
        """Queue over a ``CyberEventDataV2`` (shares its connection and lock)."""
        return cls(db._conn, db._lock, clock, configuration)

    @contextmanager
    def _locked(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self.conn

    # --- registration ------------------------------------------------------

    def register(self, raw_event_ids: Iterable[str], source_types: Dict[str, str],
                 year: int, month: int, now: Optional[datetime] = None) -> None:
        """Record the source and discovery month of records discovery stored.

        The first month a record was found under is kept (a record rediscovered
        in a later month stays with its first month).
        """
        stamp = _iso(now or self.clock())
        rows = [(rid, source_types.get(rid), year, month, stamp) for rid in raw_event_ids if rid]
        if not rows:
            return
        with self._locked() as c:
            c.executemany(
                "INSERT OR IGNORE INTO DiscoveryRecordState "
                "(raw_event_id, source_type, discovery_year, discovery_month, updated_at) VALUES (?, ?, ?, ?, ?)",
                rows)
            c.commit()

    def _ensure_row(self, c: sqlite3.Connection, raw_event_id: str, stamp: str) -> None:
        """A state row for a record discovery never registered (legacy or crashed run)."""
        c.execute(
            """INSERT OR IGNORE INTO DiscoveryRecordState
                   (raw_event_id, source_type, discovery_year, discovery_month, updated_at)
               SELECT raw_event_id, source_type,
                      CAST(strftime('%Y', COALESCE(event_date, discovered_at)) AS INTEGER),
                      CAST(strftime('%m', COALESCE(event_date, discovered_at)) AS INTEGER), ?
               FROM RawEvents WHERE raw_event_id = ?""", (stamp, raw_event_id))

    # --- the queue ---------------------------------------------------------

    def due(self, now: Optional[datetime] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Stored records not yet processed or rejected whose back-off has expired."""
        stamp = _iso(now or self.clock())
        sql = """SELECT r.raw_event_id, r.source_type, r.raw_title, r.raw_description, r.raw_content,
                        CAST(r.event_date AS TEXT) AS event_date, r.source_url, r.source_metadata,
                        CAST(r.discovered_at AS TEXT) AS discovered_at,
                        s.attempts, s.temporary_failures, s.discovery_year, s.discovery_month
                 FROM RawEvents r
                 LEFT JOIN DiscoveryRecordState s ON s.raw_event_id = r.raw_event_id
                 WHERE COALESCE(r.is_processed, 0) = 0
                   AND (s.next_attempt_at IS NULL OR s.next_attempt_at <= ?)
                 ORDER BY r.discovered_at, r.raw_event_id"""
        params: List[Any] = [stamp]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        with self._locked() as c:
            cur = c.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def waiting(self, now: Optional[datetime] = None) -> int:
        """Unprocessed records still inside their back-off."""
        stamp = _iso(now or self.clock())
        with self._locked() as c:
            return c.execute(
                """SELECT COUNT(*) FROM RawEvents r JOIN DiscoveryRecordState s ON s.raw_event_id = r.raw_event_id
                   WHERE COALESCE(r.is_processed, 0) = 0 AND s.next_attempt_at > ?""", (stamp,)).fetchone()[0]

    # --- outcomes ----------------------------------------------------------

    def record_failure(self, raw_event_id: str, stage: str, error: str, kind: str,
                       now: Optional[datetime] = None, error_type: Optional[str] = None) -> str:
        """Record one failed attempt. Returns ``'retry'`` or ``'quarantined'``.

        A temporary or unknown failure never quarantines (unknown ones are
        retried with a long back-off and logged at ERROR). The
        ``MAX_PERMANENT_ATTEMPTS``-th permanent failure marks the record
        processed with a ``Rejected at retry_quarantine`` marker, so it leaves
        every queue. ``error_type`` (a :mod:`fetch_errors` type) is classified
        from ``error`` when not given.
        """
        now = now or self.clock()
        stamp = _iso(now)
        error = (error or "")[:500]
        error_type = error_type or error_type_from_text(error)
        with self._locked() as c:
            self._ensure_row(c, raw_event_id, stamp)
            row = c.execute("SELECT attempts, temporary_failures, unknown_failures FROM DiscoveryRecordState "
                            "WHERE raw_event_id = ?", (raw_event_id,)).fetchone()
            attempts, temporary, unknown = (row[0] or 0, row[1] or 0, row[2] or 0) if row else (0, 0, 0)
            if kind == KIND_TEMPORARY:
                temporary += 1
                next_at = _iso(now + backoff(KIND_TEMPORARY, temporary))
            elif kind == KIND_UNKNOWN:
                unknown += 1
                next_at = _iso(now + backoff(KIND_UNKNOWN, unknown))
            else:
                attempts += 1
                next_at = _iso(now + backoff(KIND_PERMANENT, attempts))
            outcome = ("quarantined" if kind not in (KIND_TEMPORARY, KIND_UNKNOWN)
                       and attempts >= MAX_PERMANENT_ATTEMPTS else "retry")
            c.execute(
                """UPDATE DiscoveryRecordState
                   SET attempts = ?, temporary_failures = ?, unknown_failures = ?, last_stage = ?, last_error = ?,
                       last_error_kind = ?, last_error_type = ?, first_failed_at = COALESCE(first_failed_at, ?),
                       last_attempt_at = ?, next_attempt_at = ?, quarantined_at = ?, updated_at = ?
                   WHERE raw_event_id = ?""",
                (attempts, temporary, unknown, stage, error, kind, error_type, stamp, stamp,
                 None if outcome == "quarantined" else next_at,
                 stamp if outcome == "quarantined" else None, stamp, raw_event_id))
            if outcome == "quarantined":
                reason = f"{attempts} permanent failures at {stage}; last: {error}"
                c.execute("UPDATE RawEvents SET is_processed = 1, processing_attempted_at = ?, processing_error = ? "
                          "WHERE raw_event_id = ?",
                          (stamp, format_rejection(STAGE_RETRY_QUARANTINE, reason), raw_event_id))
            c.commit()
        if outcome == "quarantined":
            logger.error("[RETRY] Quarantined %s after %d permanent failures at %s: %s",
                         raw_event_id, attempts, stage, error[:200])
        elif kind == KIND_UNKNOWN:
            logger.error("[RETRY] Unrecognised failure #%d of %s at %s (not assumed permanent; retried after %s): %s",
                         unknown, raw_event_id, stage, backoff(KIND_UNKNOWN, unknown), error[:200])
        return outcome

    def record_success(self, raw_event_ids: Iterable[str], now: Optional[datetime] = None) -> None:
        """The records are finished (processed or rejected): no further attempts."""
        stamp = _iso(now or self.clock())
        ids = [rid for rid in raw_event_ids if rid]
        if not ids:
            return
        with self._locked() as c:
            for rid in ids:
                self._ensure_row(c, rid, stamp)
            c.executemany("UPDATE DiscoveryRecordState SET next_attempt_at = NULL, completed_at = ?, updated_at = ? "
                          "WHERE raw_event_id = ?", [(stamp, stamp, rid) for rid in ids])
            c.commit()

    def release_quarantine(self, now: Optional[datetime] = None) -> int:
        """Put every quarantined record back in the queue with its permanent attempts reset."""
        stamp = _iso(now or self.clock())
        with self._locked() as c:
            ids = [r[0] for r in c.execute(
                "SELECT raw_event_id FROM RawEvents WHERE processing_error LIKE ?",
                (f"{REJECTION_PREFIX}{STAGE_RETRY_QUARANTINE}%",))]
            self._release(c, ids, stamp)
        return len(ids)

    @staticmethod
    def _release(c: sqlite3.Connection, ids: List[str], stamp: str) -> None:
        c.executemany("UPDATE RawEvents SET is_processed = 0, processing_error = NULL WHERE raw_event_id = ?",
                      [(rid,) for rid in ids])
        c.executemany("UPDATE DiscoveryRecordState SET attempts = 0, next_attempt_at = NULL, "
                      "quarantined_at = NULL, updated_at = ? WHERE raw_event_id = ?",
                      [(stamp, rid) for rid in ids])
        c.commit()

    def _storage_quarantines(self, keep: Callable[[str], bool]) -> List[Dict[str, Any]]:
        """Quarantined records whose reason is a store stage failing with a last error ``keep`` accepts."""
        out = []
        for row in self.quarantined():
            match = _QUARANTINE_REASON.search(row.get("processing_error") or "")
            if match and match.group("stage") in STORAGE_STAGES and keep(match.group("last")):
                out.append({**row, "stage": match.group("stage"), "last_error": match.group("last")})
        return out

    def temporary_storage_quarantines(self) -> List[Dict[str, Any]]:
        """Quarantined records whose stored reason is a *store* stage failing for a temporary cause.

        A temporary storage failure never counts toward quarantine now, so these
        were quarantined by an earlier version that did. Only a reason that
        says why is read as temporary; the old generic "enriched event could
        not be stored" does not (see :meth:`unexplained_storage_quarantines`).
        """
        return self._storage_quarantines(has_temporary_cause)

    def unexplained_storage_quarantines(self) -> List[Dict[str, Any]]:
        """Records quarantined at a store stage with the old generic reason, which does not say why.

        Their cause is unknown, so they are not released automatically; ``release``
        returns them to the queue, where a repeat failure now records the real error.
        """
        return self._storage_quarantines(lambda last: last.strip() in LEGACY_STORE_REASONS)

    def release_temporary_storage_quarantines(self, now: Optional[datetime] = None) -> int:
        """Release records quarantined for a temporary storage failure. Idempotent; returns how many."""
        stamp = _iso(now or self.clock())
        ids = [row["raw_event_id"] for row in self.temporary_storage_quarantines()]
        if ids:
            with self._locked() as c:
                self._release(c, ids, stamp)
            logger.warning("[RETRY] Released %d record(s) quarantined for a temporary storage failure", len(ids))
        return len(ids)

    def quarantined(self) -> List[Dict[str, Any]]:
        with self._locked() as c:
            cur = c.execute(
                """SELECT r.raw_event_id, r.source_type, r.raw_title, r.source_url, r.processing_error,
                          s.attempts, s.temporary_failures, s.last_stage, s.quarantined_at
                   FROM RawEvents r LEFT JOIN DiscoveryRecordState s ON s.raw_event_id = r.raw_event_id
                   WHERE r.processing_error LIKE ? ORDER BY s.quarantined_at""",
                (f"{REJECTION_PREFIX}{STAGE_RETRY_QUARANTINE}%",))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    # --- completion --------------------------------------------------------

    def completion(self, year: Optional[int] = None, month: Optional[int] = None) -> List[Dict[str, Any]]:
        """Per source and discovery month: stored / processed / rejected / pending / failing / quarantined."""
        sql = "SELECT * FROM SourceMonthCompletion"
        params: List[Any] = []
        if year is not None and month is not None:
            sql += " WHERE discovery_year = ? AND discovery_month = ?"
            params = [year, month]
        sql += " ORDER BY discovery_year, discovery_month, source_type"
        with self._locked() as c:
            cur = c.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def incomplete_months(self) -> List[Dict[str, Any]]:
        """Source-months with records still pending (stored but neither processed nor rejected)."""
        return [row for row in self.completion() if row["pending"]]

    def is_month_complete(self, year: int, month: int) -> bool:
        """Every source searched without failure and every record found finished."""
        with self._locked() as c:
            failed = c.execute("SELECT 1 FROM SourceMonthDiscovery WHERE year = ? AND month = ? AND status != 'complete'",
                               (year, month)).fetchone()
        return failed is None and not any(row["pending"] for row in self.completion(year, month))

    # --- discovery (search) completeness ------------------------------------

    def record_discovery(self, source_type: str, year: int, month: int, events_found: int,
                         failures: List[str], now: Optional[datetime] = None,
                         error_types: Optional[List[str]] = None, items_seen: Optional[int] = None) -> str:
        """Record one source's search of one month. Returns ``'complete'`` or ``'failed'``.

        ``error_types`` gives the type of each failure (classified from its
        message when missing). A complete search clears the failure state. A
        failed one counts a consecutive attempt and is backed off
        (:func:`source_month_backoff`; longer for an unknown failure). A
        permanent failure (404/410) is given up at once. A temporary or unknown
        one that reaches :data:`MAX_SOURCE_MONTH_ATTEMPTS` is *parked*: the
        configuration it ran under is stored with it, the source is probed
        (:meth:`probe_due`) and the month reopens on a successful probe or a
        configuration change. A parked month that fails again (a probe) stays
        parked. ``items_seen`` is kept from the last complete search.
        """
        import json

        now = now or self.clock()
        stamp = _iso(now)
        failures = list(failures or [])
        types = list(error_types or [])[:len(failures)]
        types += [error_type_from_text(f) for f in failures[len(types):]]
        status = "failed" if failures else "complete"
        newly = None
        with self._locked() as c:
            row = c.execute("SELECT failed_attempts, gave_up_at, give_up_reason FROM SourceMonthDiscovery "
                            "WHERE source_type = ? AND year = ? AND month = ?", (source_type, year, month)).fetchone()
            if failures:
                attempts = (row[0] or 0) + 1 if row else 1
                error_type = deciding_type(types)
                failure = failure_class(error_type)
                was_given_up = bool(row and row[1])
                if failure == CLASS_PERMANENT:
                    gave_up_at = row[1] if was_given_up and row[2] == CLASS_PERMANENT else stamp
                    next_at, config = None, None
                    newly = CLASS_PERMANENT if not (was_given_up and row[2] == CLASS_PERMANENT) else None
                elif was_given_up or attempts >= MAX_SOURCE_MONTH_ATTEMPTS:
                    gave_up_at = row[1] if was_given_up and row[2] != CLASS_PERMANENT else stamp
                    next_at = None
                    # The configuration this attempt ran under: a failure under the current one is not cured by it.
                    config = source_config.encode(self.configuration(source_type))
                    newly = failure if gave_up_at == stamp else None
                else:
                    gave_up_at, next_at, config = None, _iso(now + source_month_backoff(attempts, failure)), None
                state = (error_type, json.dumps(types[:50]), attempts, next_at, gave_up_at,
                         failure if gave_up_at else None, config)
            else:
                state = (None, None, 0, None, None, None, None)
            c.execute(
                """INSERT INTO SourceMonthDiscovery
                       (source_type, year, month, status, events_found, failures_json, attempted_at, completed_at,
                        error_type, error_types_json, failed_attempts, next_attempt_at, gave_up_at, give_up_reason,
                        give_up_config, items_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (source_type, year, month) DO UPDATE SET
                       status = excluded.status, events_found = excluded.events_found,
                       failures_json = excluded.failures_json, attempted_at = excluded.attempted_at,
                       completed_at = COALESCE(excluded.completed_at, SourceMonthDiscovery.completed_at),
                       error_type = excluded.error_type, error_types_json = excluded.error_types_json,
                       failed_attempts = excluded.failed_attempts, next_attempt_at = excluded.next_attempt_at,
                       gave_up_at = excluded.gave_up_at, give_up_reason = excluded.give_up_reason,
                       give_up_config = excluded.give_up_config,
                       items_seen = COALESCE(excluded.items_seen, SourceMonthDiscovery.items_seen)""",
                (source_type, year, month, status, events_found, json.dumps(failures[:50]) if failures else None,
                 stamp, None if failures else stamp, state[0], state[1], state[2], state[3], state[4], state[5],
                 state[6], None if failures else items_seen))
            c.commit()
        if newly == CLASS_PERMANENT:
            logger.error("[DISCOVERY] %s %d-%02d given up (%s): it does not exist, so it is not searched again "
                         "until released: python scripts/discovery_queue.py release", source_type, year, month,
                         state[0])
        elif newly:
            logger.error("[DISCOVERY] %s %d-%02d parked after %d failed search(es) (%s, %s failure): not searched "
                         "on its own, but the source is probed after %s and the month reopens when the probe "
                         "succeeds or the source's configuration changes", source_type, year, month, state[2],
                         state[0], newly, SOURCE_PROBE_INTERVAL)
        return status

    def discovery_status(self, source_type: str, year: int, month: int) -> Optional[str]:
        """The latest search status of one source-month, or None if it was never searched."""
        with self._locked() as c:
            row = c.execute("SELECT status FROM SourceMonthDiscovery WHERE source_type = ? AND year = ? "
                            "AND month = ?", (source_type, year, month)).fetchone()
            return row[0] if row else None

    def source_month_retry(self, source_type: str, year: int, month: int,
                           now: Optional[datetime] = None) -> str:
        """Whether a source-month should be searched now: one of the ``MONTH_*`` states."""
        stamp = _iso(now or self.clock())
        with self._locked() as c:
            row = c.execute("SELECT status, next_attempt_at, gave_up_at, give_up_reason FROM SourceMonthDiscovery "
                            "WHERE source_type = ? AND year = ? AND month = ?", (source_type, year, month)).fetchone()
        if row is None:
            return MONTH_NEVER
        status, next_at, gave_up, reason = row
        if status == "complete":
            return MONTH_COMPLETE
        if gave_up:
            return MONTH_GAVE_UP if reason == CLASS_PERMANENT else MONTH_PARKED
        return MONTH_WAITING if next_at and next_at > stamp else MONTH_DUE

    def release_source_months(self, source_type: Optional[str] = None) -> int:
        """Make given-up and parked source-months searchable again (attempts reset). Returns how many.

        This is the manual override for what needs a person: a permanent
        failure (404/410) that has since been fixed. Parked months reopen by
        themselves.
        """
        sql = ("UPDATE SourceMonthDiscovery SET gave_up_at = NULL, next_attempt_at = NULL, failed_attempts = 0, "
               "give_up_reason = NULL, give_up_config = NULL WHERE gave_up_at IS NOT NULL")
        probes = "DELETE FROM SourceProbes"
        params: List[Any] = []
        if source_type:
            sql += " AND source_type = ?"
            probes += " WHERE source_type = ?"
            params.append(source_type)
        with self._locked() as c:
            count = c.execute(sql, params).rowcount
            c.execute(probes, params)
            c.commit()
        return count

    def failed_discoveries(self) -> List[Dict[str, Any]]:
        """Source-months whose latest search lost results."""
        with self._locked() as c:
            cur = c.execute("SELECT * FROM SourceMonthDiscovery WHERE status != 'complete' ORDER BY year, month, "
                            "source_type")
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    # --- parked months: probes and configuration changes ------------------------

    def parked_months(self, source_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Months parked after repeated temporary or unknown failures (not permanent ones)."""
        return self._given_up(source_type, permanent=False)

    def given_up_months(self, source_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Months given up on a permanent failure (404/410): only a person reopens these."""
        return self._given_up(source_type, permanent=True)

    def _given_up(self, source_type: Optional[str], permanent: bool) -> List[Dict[str, Any]]:
        sql = ("SELECT * FROM SourceMonthDiscovery WHERE gave_up_at IS NOT NULL AND COALESCE(give_up_reason, '') "
               + ("= ?" if permanent else "!= ?"))
        params: List[Any] = [CLASS_PERMANENT]
        if source_type:
            sql += " AND source_type = ?"
            params.append(source_type)
        with self._locked() as c:
            cur = c.execute(sql + " ORDER BY source_type, year, month", params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    @staticmethod
    def _reopen(c: sqlite3.Connection, source_type: str, year: int, month: int, stamp: str, reason: str) -> None:
        c.execute("UPDATE SourceMonthDiscovery SET gave_up_at = NULL, next_attempt_at = NULL, failed_attempts = 0, "
                  "give_up_reason = NULL, give_up_config = NULL, reopened_at = ?, reopen_reason = ? "
                  "WHERE source_type = ? AND year = ? AND month = ?", (stamp, reason, source_type, year, month))

    def reopen_changed_config(self, source_type: Optional[str] = None, now: Optional[datetime] = None,
                              dry_run: bool = False) -> List[Dict[str, Any]]:
        """Reopen parked months whose recorded configuration is not the source's current one.

        A month parked before configurations were recorded counts as changed
        (nothing shows it is not), so it is searched again once; the new
        give-up, if it comes, records the configuration. Permanent give-ups are
        left alone. Returns one entry per month: ``source_type``, ``year``,
        ``month`` and the ``changed`` component names. ``dry_run`` reports
        without reopening.
        """
        stamp = _iso(now or self.clock())
        found: List[Dict[str, Any]] = []
        current_by_source: Dict[str, Dict[str, str]] = {}
        for row in self.parked_months(source_type):
            source = row["source_type"]
            if source not in current_by_source:
                current_by_source[source] = self.configuration(source)
            changed = source_config.changed_components(source_config.decode(row.get("give_up_config")),
                                                       current_by_source[source])
            if changed:
                found.append({"source_type": source, "year": row["year"], "month": row["month"],
                              "changed": changed})
        if found and not dry_run:
            with self._locked() as c:
                for entry in found:
                    self._reopen(c, entry["source_type"], entry["year"], entry["month"], stamp,
                                 "configuration changed: " + ", ".join(entry["changed"]))
                for source in {e["source_type"] for e in found}:
                    if not c.execute("SELECT 1 FROM SourceMonthDiscovery WHERE source_type = ? AND gave_up_at IS NOT "
                                     "NULL AND COALESCE(give_up_reason, '') != ?", (source, CLASS_PERMANENT)).fetchone():
                        c.execute("DELETE FROM SourceProbes WHERE source_type = ?", (source,))
                c.commit()
            for source in sorted({e["source_type"] for e in found}):
                logger.warning("[DISCOVERY] %s: %d parked month(s) reopened, the source's configuration changed (%s)",
                               source, sum(1 for e in found if e["source_type"] == source),
                               ", ".join(sorted({n for e in found if e["source_type"] == source for n in e["changed"]})))
        return found

    def _next_probe_at(self, c: sqlite3.Connection, source_type: str) -> Optional[str]:
        """When the source's parked months are next probed (None if none are parked)."""
        parked = c.execute("SELECT MIN(gave_up_at) FROM SourceMonthDiscovery WHERE source_type = ? AND "
                           "gave_up_at IS NOT NULL AND COALESCE(give_up_reason, '') != ?",
                           (source_type, CLASS_PERMANENT)).fetchone()[0]
        if parked is None:
            return None
        probe = c.execute("SELECT next_probe_at FROM SourceProbes WHERE source_type = ?", (source_type,)).fetchone()
        if probe and probe[0]:
            return probe[0]
        return _iso(datetime.fromisoformat(parked) + SOURCE_PROBE_INTERVAL)

    def probe_due(self, source_type: str, months: Optional[Iterable[tuple]] = None,
                  now: Optional[datetime] = None) -> Optional[tuple]:
        """The ``(year, month)`` to search as this source's probe now, or None.

        A source with parked months is probed once its probe time has come: a
        week after the first month was parked, then at doubling intervals while
        probes fail. The most recent parked month (of ``months``, when given)
        is the probe: if the source works for it, every parked month reopens
        (:meth:`record_probe`).
        """
        stamp = _iso(now or self.clock())
        wanted = None if months is None else {(int(y), int(m)) for y, m in months}
        with self._locked() as c:
            due_at = self._next_probe_at(c, source_type)
            if due_at is None or due_at > stamp:
                return None
            parked = [(r[0], r[1]) for r in c.execute(
                "SELECT year, month FROM SourceMonthDiscovery WHERE source_type = ? AND gave_up_at IS NOT NULL "
                "AND COALESCE(give_up_reason, '') != ?", (source_type, CLASS_PERMANENT))]
        candidates = [ym for ym in parked if wanted is None or ym in wanted]
        return max(candidates) if candidates else None

    def record_probe(self, source_type: str, succeeded: bool, error_type: Optional[str] = None,
                     now: Optional[datetime] = None) -> List[tuple]:
        """Record a probe's outcome. Returns the ``(year, month)`` pairs a success reopened.

        Success reopens every parked month of the source and clears its probe
        schedule. Failure schedules the next probe (:func:`probe_backoff`) and
        keeps the months parked.
        """
        now = now or self.clock()
        stamp = _iso(now)
        reopened: List[tuple] = []
        with self._locked() as c:
            row = c.execute("SELECT consecutive_failures FROM SourceProbes WHERE source_type = ?",
                            (source_type,)).fetchone()
            failures = (row[0] or 0) if row else 0
            if succeeded:
                reopened = [(r[0], r[1]) for r in c.execute(
                    "SELECT year, month FROM SourceMonthDiscovery WHERE source_type = ? AND gave_up_at IS NOT NULL "
                    "AND COALESCE(give_up_reason, '') != ?", (source_type, CLASS_PERMANENT))]
                for year, month in reopened:
                    self._reopen(c, source_type, year, month, stamp, "probe succeeded")
                c.execute(
                    """INSERT INTO SourceProbes (source_type, last_probe_at, next_probe_at, consecutive_failures,
                                                 last_error_type, last_success_at)
                       VALUES (?, ?, NULL, 0, NULL, ?)
                       ON CONFLICT (source_type) DO UPDATE SET last_probe_at = excluded.last_probe_at,
                           next_probe_at = NULL, consecutive_failures = 0, last_error_type = NULL,
                           last_success_at = excluded.last_success_at""", (source_type, stamp, stamp))
            else:
                failures += 1
                c.execute(
                    """INSERT INTO SourceProbes (source_type, last_probe_at, next_probe_at, consecutive_failures,
                                                 last_error_type)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT (source_type) DO UPDATE SET last_probe_at = excluded.last_probe_at,
                           next_probe_at = excluded.next_probe_at,
                           consecutive_failures = excluded.consecutive_failures,
                           last_error_type = excluded.last_error_type""",
                    (source_type, stamp, _iso(now + probe_backoff(failures)), failures, error_type))
            c.commit()
        if succeeded:
            logger.warning("[DISCOVERY] %s probe succeeded: %d parked month(s) reopened", source_type, len(reopened))
        else:
            logger.error("[DISCOVERY] %s probe failed (%s), %d in a row: next probe after %s", source_type,
                         error_type or "?", failures, probe_backoff(failures))
        return reopened

    def health(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """The state of searches and unknown record failures (:func:`discovery_health`)."""
        with self._locked() as c:
            return discovery_health(c, now or self.clock(), self.configuration)


def discovery_health(conn: sqlite3.Connection, now: datetime,
                     configuration: Callable[[str], Dict[str, str]] = source_config.current_components
                     ) -> Dict[str, Any]:
    """What is given up, what is parked, when each source is probed next and what would reopen it.

    Read-only. Keys: ``given_up`` (permanent: 404/410), ``parked`` (temporary or
    unknown; each with ``reason``, ``error_type``, ``parked_since`` and
    ``would_reopen`` - the configuration components that changed since, empty if
    none), ``probes`` (per source with parked months: ``parked``, ``next_probe_at``,
    ``overdue``, ``consecutive_failures``, ``last_error_type``) and ``unknown_records``
    (unprocessed records whose latest failure was unrecognised: ``count``, ``by_stage``).
    """
    stamp = _iso(now)
    rows = conn.execute(
        "SELECT source_type, year, month, error_type, failed_attempts, gave_up_at, give_up_reason, give_up_config "
        "FROM SourceMonthDiscovery WHERE gave_up_at IS NOT NULL ORDER BY source_type, year, month").fetchall()
    probes = {r[0]: tuple(r) for r in conn.execute(
        "SELECT source_type, last_probe_at, next_probe_at, consecutive_failures, last_error_type FROM SourceProbes")}
    unknown_stages = [tuple(r) for r in conn.execute(
        """SELECT COALESCE(s.last_stage, '?'), COUNT(*) FROM DiscoveryRecordState s
           JOIN RawEvents r ON r.raw_event_id = s.raw_event_id
           WHERE COALESCE(r.is_processed, 0) = 0 AND s.last_error_kind = ? GROUP BY 1""", (KIND_UNKNOWN,))]

    given_up, parked = [], []
    current: Dict[str, Dict[str, str]] = {}
    for source, year, month, error_type, attempts, gave_up_at, reason, config in rows:
        entry = {"source_type": source, "year": year, "month": month, "error_type": error_type,
                 "failed_attempts": attempts, "reason": reason, "parked_since": gave_up_at}
        if reason == CLASS_PERMANENT:
            given_up.append(entry)
            continue
        if source not in current:
            current[source] = configuration(source)
        entry["would_reopen"] = source_config.changed_components(source_config.decode(config), current[source])
        parked.append(entry)

    per_source: Dict[str, Dict[str, Any]] = {}
    for entry in parked:
        info = per_source.setdefault(entry["source_type"], {"source_type": entry["source_type"], "parked": 0,
                                                            "first_parked": entry["parked_since"]})
        info["parked"] += 1
        info["first_parked"] = min(info["first_parked"], entry["parked_since"])
    for source, info in per_source.items():
        probe = probes.get(source)
        next_at = probe[2] if probe and probe[2] else _iso(datetime.fromisoformat(info.pop("first_parked"))
                                                           + SOURCE_PROBE_INTERVAL)
        info.pop("first_parked", None)
        info.update(next_probe_at=next_at, overdue=next_at <= stamp,
                    consecutive_failures=(probe[3] or 0) if probe else 0,
                    last_probe_at=probe[1] if probe else None, last_error_type=probe[4] if probe else None)
    return {"given_up": given_up, "parked": parked, "probes": sorted(per_source.values(), key=lambda p: p["source_type"]),
            "unknown_records": {"count": sum(n for _, n in unknown_stages),
                                "by_stage": {stage: n for stage, n in unknown_stages}}}
