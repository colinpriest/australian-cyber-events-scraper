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
* **permanent** failures (the record itself cannot be processed - malformed
  model output for its content, a request the provider refuses as invalid, a
  store that fails validation): counted, backed off, and after
  :data:`MAX_PERMANENT_ATTEMPTS` the record is quarantined - marked processed
  with ``Rejected at retry_quarantine: ...`` so it leaves every queue - and
  reported. ``python scripts/discovery_queue.py release`` puts quarantined
  records back.

The queue itself is ``RawEvents.is_processed = 0``: state rows only add
back-off and history, so a record from a run that crashed before any state was
written is still drained.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

from cyber_data_collector.processing.record_evidence import REJECTION_PREFIX, format_rejection
from cyber_data_collector.utils.api_budget import is_budget_refusal

logger = logging.getLogger(__name__)

STAGE_RETRY_QUARANTINE = "retry_quarantine"

KIND_TEMPORARY = "temporary"
KIND_PERMANENT = "permanent"

#: Permanent failures a record may have before it is quarantined.
MAX_PERMANENT_ATTEMPTS = 3
#: Back-off after the n-th temporary failure: 15 min, 30 min, 1 h ... capped.
TEMPORARY_BACKOFF_BASE = timedelta(minutes=15)
TEMPORARY_BACKOFF_CAP = timedelta(hours=12)
#: Back-off after the n-th permanent failure: 1 h, 2 h, 4 h ... capped.
PERMANENT_BACKOFF_BASE = timedelta(hours=1)
PERMANENT_BACKOFF_CAP = timedelta(days=7)

SCHEMA = """
CREATE TABLE IF NOT EXISTS DiscoveryRecordState (
    raw_event_id         TEXT PRIMARY KEY,
    source_type          TEXT,
    discovery_year       INTEGER,
    discovery_month      INTEGER,
    attempts             INTEGER NOT NULL DEFAULT 0,  -- permanent failures (count toward quarantine)
    temporary_failures   INTEGER NOT NULL DEFAULT 0,  -- provider failures (never count toward quarantine)
    last_stage           TEXT,
    last_error           TEXT,
    last_error_kind      TEXT,                        -- 'temporary' / 'permanent'
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
    PRIMARY KEY (source_type, year, month)
);
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
                 AND (s.attempts + s.temporary_failures) > 0 THEN 1 ELSE 0 END) AS failing,
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
    "BrokenPipeError", "BudgetExhausted", "PageGateUnavailable", "OperationalError",
}
_TEMPORARY_TEXT = ("timed out", "timeout", "rate limit", "temporarily", "overloaded", "connection reset",
                   "connection aborted", "connection refused", "service unavailable", "bad gateway",
                   "gateway timeout", "too many requests", "database is locked")


def classify_failure(exc: BaseException) -> str:
    """``'temporary'`` when the provider (not the record) failed, else ``'permanent'``.

    Temporary: timeouts, rate limits, 429/5xx, connection errors, a locked
    database and API-budget refusals - anything that may succeed unchanged
    later. Everything else counts toward quarantine.
    """
    if is_budget_refusal(exc):
        return KIND_TEMPORARY
    seen = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
            return KIND_TEMPORARY
        if any(cls.__name__ in _TEMPORARY_NAMES for cls in type(current).__mro__):
            return KIND_TEMPORARY
        status = getattr(current, "status_code", None)
        if status is None:
            status = getattr(getattr(current, "response", None), "status_code", None)
        if isinstance(status, int) and (status in _TEMPORARY_STATUS or status >= 500):
            return KIND_TEMPORARY
        text = str(current).lower()
        if any(fragment in text for fragment in _TEMPORARY_TEXT):
            return KIND_TEMPORARY
        current = current.__cause__ or current.__context__
    return KIND_PERMANENT


def backoff(kind: str, count: int) -> timedelta:
    """Delay before the next attempt after ``count`` failures of ``kind``."""
    base, cap = ((TEMPORARY_BACKOFF_BASE, TEMPORARY_BACKOFF_CAP) if kind == KIND_TEMPORARY
                 else (PERMANENT_BACKOFF_BASE, PERMANENT_BACKOFF_CAP))
    return min(cap, base * (2 ** max(0, count - 1)))


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


class RetryQueue:
    """The queue over one database.

    Args:
        conn: SQLite connection (``RawEvents`` must exist).
        lock: Lock guarding ``conn`` when it is shared between threads (the
            ``CyberEventDataV2`` lock); a private one is used when omitted.
        clock: Returns the current time (tests pass a controllable one).
    """

    def __init__(self, conn: sqlite3.Connection, lock: Optional[Any] = None,
                 clock: Callable[[], datetime] = datetime.now) -> None:
        self.conn = conn
        self._lock = lock or threading.Lock()
        self.clock = clock
        with self._locked() as c:
            ensure_schema(c)

    @classmethod
    def for_db(cls, db: Any, clock: Callable[[], datetime] = datetime.now) -> "RetryQueue":
        """Queue over a ``CyberEventDataV2`` (shares its connection and lock)."""
        return cls(db._conn, db._lock, clock)

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
                       now: Optional[datetime] = None) -> str:
        """Record one failed attempt. Returns ``'retry'`` or ``'quarantined'``.

        A temporary failure never quarantines. The ``MAX_PERMANENT_ATTEMPTS``-th
        permanent failure marks the record processed with a
        ``Rejected at retry_quarantine`` marker, so it leaves every queue.
        """
        now = now or self.clock()
        stamp = _iso(now)
        error = (error or "")[:500]
        with self._locked() as c:
            self._ensure_row(c, raw_event_id, stamp)
            row = c.execute("SELECT attempts, temporary_failures FROM DiscoveryRecordState WHERE raw_event_id = ?",
                            (raw_event_id,)).fetchone()
            attempts, temporary = (row[0] or 0, row[1] or 0) if row else (0, 0)
            if kind == KIND_TEMPORARY:
                temporary += 1
                next_at = _iso(now + backoff(KIND_TEMPORARY, temporary))
            else:
                attempts += 1
                next_at = _iso(now + backoff(KIND_PERMANENT, attempts))
            outcome = "quarantined" if kind != KIND_TEMPORARY and attempts >= MAX_PERMANENT_ATTEMPTS else "retry"
            c.execute(
                """UPDATE DiscoveryRecordState
                   SET attempts = ?, temporary_failures = ?, last_stage = ?, last_error = ?, last_error_kind = ?,
                       first_failed_at = COALESCE(first_failed_at, ?), last_attempt_at = ?,
                       next_attempt_at = ?, quarantined_at = ?, updated_at = ?
                   WHERE raw_event_id = ?""",
                (attempts, temporary, stage, error, kind, stamp, stamp,
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
        """Put every quarantined record back in the queue with its attempts reset."""
        stamp = _iso(now or self.clock())
        with self._locked() as c:
            ids = [r[0] for r in c.execute(
                "SELECT raw_event_id FROM RawEvents WHERE processing_error LIKE ?",
                (f"{REJECTION_PREFIX}{STAGE_RETRY_QUARANTINE}%",))]
            c.executemany("UPDATE RawEvents SET is_processed = 0, processing_error = NULL WHERE raw_event_id = ?",
                          [(rid,) for rid in ids])
            c.executemany("UPDATE DiscoveryRecordState SET attempts = 0, next_attempt_at = NULL, "
                          "quarantined_at = NULL, updated_at = ? WHERE raw_event_id = ?",
                          [(stamp, rid) for rid in ids])
            c.commit()
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
                         failures: List[str], now: Optional[datetime] = None) -> str:
        """Record one source's search of one month. Returns ``'complete'`` or ``'failed'``."""
        import json

        stamp = _iso(now or self.clock())
        status = "failed" if failures else "complete"
        with self._locked() as c:
            c.execute(
                """INSERT INTO SourceMonthDiscovery
                       (source_type, year, month, status, events_found, failures_json, attempted_at, completed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (source_type, year, month) DO UPDATE SET
                       status = excluded.status, events_found = excluded.events_found,
                       failures_json = excluded.failures_json, attempted_at = excluded.attempted_at,
                       completed_at = COALESCE(excluded.completed_at, SourceMonthDiscovery.completed_at)""",
                (source_type, year, month, status, events_found, json.dumps(failures[:50]) if failures else None,
                 stamp, None if failures else stamp))
            c.commit()
        return status

    def discovery_status(self, source_type: str, year: int, month: int) -> Optional[str]:
        """The latest search status of one source-month, or None if it was never searched."""
        with self._locked() as c:
            row = c.execute("SELECT status FROM SourceMonthDiscovery WHERE source_type = ? AND year = ? "
                            "AND month = ?", (source_type, year, month)).fetchone()
            return row[0] if row else None

    def failed_discoveries(self) -> List[Dict[str, Any]]:
        """Source-months whose latest search lost results."""
        with self._locked() as c:
            cur = c.execute("SELECT * FROM SourceMonthDiscovery WHERE status != 'complete' ORDER BY year, month, "
                            "source_type")
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
