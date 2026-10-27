"""An event's facts are recomputed from all its reports whenever its membership changes.

An event used to keep the facts its first (master) record carried: a report
attached later - by incremental deduplication, a merge, a calibrated duplicate
merge - could confirm 10,000 affected records and the event still said 100.
Now every event stores a signature of its members (``facts_member_signature``);
when the membership differs, or a member's content changed
(``record_fingerprints``), the facts are recomputed from all members:

- ``records_affected``: the largest count any member report gives that passes
  the victim-aware sanity check (``validate_records_affected``). Counts grow as
  an incident is investigated, so the latest, largest supported figure is the
  best estimate; implausible values (a global vendor total attributed to an
  Australian customer) are rejected by the same rule ``fix_records_affected``
  applies.
- ``event_type``, ``severity``, ``attack_method``, ``attacking_entity_name``:
  the master's value stands (it is the event's narrative); a blank is filled
  with the members' most common value.

Explicit corrections win: a value recorded with ``dedup_admin.py set-fact`` is
kept in ``EventFactOverrides`` and applied instead of anything computed.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections import Counter
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

COUNT_FIELD = "records_affected"
FILL_FIELDS = ("event_type", "severity", "attack_method", "attacking_entity_name")
FACT_FIELDS = (COUNT_FIELD,) + FILL_FIELDS

SCHEMA = """
CREATE TABLE IF NOT EXISTS EventFactOverrides (
    deduplicated_event_id TEXT NOT NULL,
    field                 TEXT NOT NULL,
    value                 TEXT,
    reason                TEXT NOT NULL,
    created_by            TEXT NOT NULL DEFAULT 'human',
    created_at            TEXT NOT NULL,
    PRIMARY KEY (deduplicated_event_id, field)
)
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(DeduplicatedEvents)")}
    if columns and "facts_member_signature" not in columns:
        conn.execute("ALTER TABLE DeduplicatedEvents ADD COLUMN facts_member_signature TEXT")


def member_signature(member_ids: Iterable[str]) -> str:
    return hashlib.sha256("|".join(sorted(set(member_ids))).encode("utf-8")).hexdigest()


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _members(conn: sqlite3.Connection, dedup_id: str) -> List[Tuple]:
    present = _columns(conn, "EnrichedEvents")
    selected = ", ".join(f"e.{f}" if f in present else "NULL" for f in FACT_FIELDS)
    status = "AND e.status = 'Active'" if "status" in present else ""
    return conn.execute(f"""
        SELECT DISTINCT e.enriched_event_id, {selected}
        FROM EventDeduplicationMap m
        JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
        WHERE m.deduplicated_event_id = ? {status}
    """, (dedup_id,)).fetchall()


def _overrides(conn: sqlite3.Connection, dedup_id: str) -> Dict[str, Any]:
    return {field: value for field, value in conn.execute(
        "SELECT field, value FROM EventFactOverrides WHERE deduplicated_event_id = ?", (dedup_id,))}


def compute_facts(event: Dict[str, Any], members: List[Tuple], overrides: Dict[str, Any]) -> Dict[str, Any]:
    """The facts an event should carry, from its current values, its members and corrections."""
    from cyber_data_collector.utils.validation import validate_records_affected

    title = event.get("title") or ""
    victim = event.get("victim_organization_name") or ""
    index = {field: i + 1 for i, field in enumerate(FACT_FIELDS)}
    facts: Dict[str, Any] = {}

    counts = []
    for row in members:
        value = row[index[COUNT_FIELD]]
        if value is None:
            continue
        try:
            checked = validate_records_affected(int(value), title, victim)
        except (TypeError, ValueError):
            checked = None
        if checked is not None:
            counts.append(checked)
    if counts:
        facts[COUNT_FIELD] = max(counts)

    master = event.get("master_enriched_event_id")
    for field in FILL_FIELDS:
        if event.get(field) not in (None, ""):
            continue
        values = [row[index[field]] for row in members if row[index[field]] not in (None, "")]
        if not values:
            continue
        tally = Counter(values)
        best = max(tally.values())
        master_value = next((row[index[field]] for row in members if row[0] == master), None)
        facts[field] = master_value if tally.get(master_value) == best else \
            next(v for v in values if tally[v] == best)

    for field, value in overrides.items():
        if field in FACT_FIELDS:
            facts[field] = int(value) if field == COUNT_FIELD and value not in (None, "") else value
    return facts


def recompute_facts(conn: sqlite3.Connection, dedup_id: str, dry_run: bool = False) -> Dict[str, Tuple[Any, Any]]:
    """Recompute one event's facts; returns ``{field: (old, new)}`` for what changed.

    Commits nothing; the caller owns the transaction.
    """
    ensure_schema(conn)
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                           (dedup_id,)).fetchone()
        if row is None:
            return {}
        event = dict(row)
        conn.row_factory = None
        members = _members(conn, dedup_id)
        if not members:
            return {}
        facts = compute_facts(event, members, _overrides(conn, dedup_id))
        changes = {field: (event.get(field), value) for field, value in facts.items()
                   if field in event and str(event.get(field)) != str(value)}
        if not dry_run:
            for field, (_, value) in changes.items():
                conn.execute(f"UPDATE DeduplicatedEvents SET {field} = ? WHERE deduplicated_event_id = ?",
                             (value, dedup_id))
            stamp = ", updated_at = ?" if changes else ""
            conn.execute(f"UPDATE DeduplicatedEvents SET facts_member_signature = ?{stamp} "
                         "WHERE deduplicated_event_id = ?",
                         (member_signature(m[0] for m in members),
                          *([datetime.now().isoformat()] if changes else []), dedup_id))
        return changes
    finally:
        conn.row_factory = previous


def stale_events(conn: sqlite3.Connection) -> List[str]:
    """Active events whose membership differs from the one their facts were computed for."""
    ensure_schema(conn)
    members: Dict[str, List[str]] = {}
    for dedup_id, enriched_id in conn.execute("""
            SELECT m.deduplicated_event_id, m.enriched_event_id FROM EventDeduplicationMap m
            JOIN DeduplicatedEvents d ON d.deduplicated_event_id = m.deduplicated_event_id
            JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
            WHERE d.status = 'Active'"""):
        members.setdefault(dedup_id, []).append(enriched_id)
    stored = dict(conn.execute(
        "SELECT deduplicated_event_id, facts_member_signature FROM DeduplicatedEvents WHERE status = 'Active'"))
    return sorted(d for d, ids in members.items() if stored.get(d) != member_signature(ids))


def refresh_facts(conn: sqlite3.Connection, also: Iterable[str] = (), dry_run: bool = False
                  ) -> Dict[str, Dict[str, Tuple[Any, Any]]]:
    """Recompute every event whose membership changed, plus ``also`` (content changes)."""
    targets = sorted(set(stale_events(conn)) | set(also))
    changed: Dict[str, Dict[str, Tuple[Any, Any]]] = {}
    for dedup_id in targets:
        delta = recompute_facts(conn, dedup_id, dry_run=dry_run)
        if delta:
            changed[dedup_id] = delta
    if targets:
        fields = Counter(f for delta in changed.values() for f in delta)
        logger.info("Event facts: %d event(s) checked, %d changed (%s)", len(targets), len(changed),
                    ", ".join(f"{k} x{v}" for k, v in sorted(fields.items())) or "none")
    return changed


def set_fact(conn: sqlite3.Connection, dedup_id: str, field: str, value: Optional[str], reason: str,
             created_by: str = "human") -> None:
    """Record an explicit correction; it outranks anything computed from the members."""
    if field not in FACT_FIELDS:
        raise ValueError(f"{field!r} is not a recomputed fact; expected one of {FACT_FIELDS}")
    ensure_schema(conn)
    conn.execute("INSERT OR REPLACE INTO EventFactOverrides VALUES (?, ?, ?, ?, ?, ?)",
                 (dedup_id, field, value, reason, created_by, datetime.now().isoformat()))
    recompute_facts(conn, dedup_id)
