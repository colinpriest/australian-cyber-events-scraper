"""Propagate changes in source records to the events built from them.

Incremental deduplication places each record once, when it first appears. A
record re-enriched afterwards - a corrected date, a new victim from Perplexity,
a fixed record count - used to change nothing downstream: the event kept the
values it copied at creation. This module remembers what every placed record
looked like (``RecordFingerprints``) and, when a record has changed, refreshes
the events containing it while keeping their ids:

- title and date are re-derived from all members (member consensus, as merges
  and splits do), when a member's title or date changed;
- a field the event copied from its master record is updated to the master's
  new value - but only while the event still holds the master's *old* value,
  so an event-level correction made since (``fix_records_affected``, a human
  edit) is never overwritten;
- the event's entity roles are marked stale, because the victim is derived
  from its members' Perplexity research, which is part of the fingerprint.

Everything downstream keys on its own inputs and so follows automatically:
ASD classifications carry an input fingerprint, and Jev judgements a hash of
their input, so a refreshed event is reclassified and re-judged.

A record with no stored fingerprint is a baseline, not a change: its values are
recorded and nothing is refreshed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Fields of EnrichedEvents that events are built from.
TRACKED_FIELDS = (
    "title", "description", "summary", "event_type", "severity", "event_date",
    "records_affected", "attacking_entity_name", "attack_method",
    "is_australian_event", "is_specific_event", "perplexity_enrichment_data",
)
# Fields re-derived from every member rather than copied from the master.
IDENTITY_FIELDS = ("title", "event_date")
# Fields an event copies from its master record (DeduplicatedEvents column = EnrichedEvents column).
MASTER_COPIED_FIELDS = (
    "description", "summary", "event_type", "severity", "records_affected",
    "attacking_entity_name", "attack_method", "is_australian_event", "is_specific_event",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS RecordFingerprints (
    enriched_event_id TEXT PRIMARY KEY,
    fingerprint       TEXT NOT NULL,
    values_json       TEXT NOT NULL,
    recorded_at       TEXT NOT NULL
)
"""


@dataclass
class RefreshReport:
    baselined: int = 0
    changed_records: int = 0
    refreshed_events: List[str] = field(default_factory=list)
    fields_updated: Dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        fields = ", ".join(f"{k} x{v}" for k, v in sorted(self.fields_updated.items())) or "none"
        return (f"{self.changed_records} changed record(s) -> {len(self.refreshed_events)} event(s) "
                f"refreshed (fields: {fields}); {self.baselined} record(s) baselined")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)


def _values(row: sqlite3.Row) -> Dict[str, Any]:
    return {name: row[name] for name in TRACKED_FIELDS}


def fingerprint(values: Dict[str, Any]) -> str:
    raw = json.dumps(values, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _placed_records(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Active records in active events, with their event and whether they are its master."""
    columns = ", ".join(f"e.{name}" for name in TRACKED_FIELDS)
    return conn.execute(f"""
        SELECT DISTINCT e.enriched_event_id, d.deduplicated_event_id,
               (d.master_enriched_event_id = e.enriched_event_id) AS is_master, {columns}
        FROM EventDeduplicationMap m
        JOIN DeduplicatedEvents d ON d.deduplicated_event_id = m.deduplicated_event_id
        JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
        WHERE d.status = 'Active' AND e.status = 'Active'
    """).fetchall()


def refresh_changed_events(conn: sqlite3.Connection, dry_run: bool = False) -> RefreshReport:
    """Refresh every active event containing a record that changed since it was last seen.

    Commits nothing; the caller owns the transaction.
    """
    from cyber_data_collector.dedup.ledger import DedupLedger

    ensure_schema(conn)
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    report = RefreshReport()
    try:
        stored = {row["enriched_event_id"]: row for row in conn.execute(
            "SELECT enriched_event_id, fingerprint, values_json FROM RecordFingerprints")}
        now = datetime.now().isoformat()
        changed_by_event: Dict[str, List[Dict[str, Any]]] = {}
        to_store: Dict[str, tuple] = {}
        for row in _placed_records(conn):
            record_id = row["enriched_event_id"]
            values = _values(row)
            new_fp = fingerprint(values)
            to_store[record_id] = (new_fp, json.dumps(values, default=str, ensure_ascii=False))
            old = stored.get(record_id)
            if old is None:
                report.baselined += 1
                continue
            if old["fingerprint"] == new_fp:
                continue
            old_values = json.loads(old["values_json"])
            changed = {name for name in TRACKED_FIELDS
                       if json.dumps(old_values.get(name), default=str)
                       != json.dumps(values[name], default=str)}
            changed_by_event.setdefault(row["deduplicated_event_id"], []).append({
                "is_master": bool(row["is_master"]), "changed": changed,
                "old": old_values, "new": values,
            })
        report.changed_records = sum(len(v) for v in changed_by_event.values())

        if not dry_run:
            ledger = DedupLedger(conn)
            for dedup_id, changes in changed_by_event.items():
                updated = _refresh_event(conn, ledger, dedup_id, changes)
                for name in updated:
                    report.fields_updated[name] = report.fields_updated.get(name, 0) + 1
                report.refreshed_events.append(dedup_id)
            conn.executemany(
                "INSERT OR REPLACE INTO RecordFingerprints "
                "(enriched_event_id, fingerprint, values_json, recorded_at) VALUES (?, ?, ?, ?)",
                [(rid, fp, values_json, now) for rid, (fp, values_json) in to_store.items()],
            )
        else:
            report.refreshed_events = list(changed_by_event)
    finally:
        conn.row_factory = previous_factory
    if report.changed_records or report.baselined:
        logger.info("Record changes: %s", report.summary())
    return report


def _refresh_event(conn: sqlite3.Connection, ledger: Any, dedup_id: str,
                   changes: List[Dict[str, Any]]) -> Set[str]:
    """Apply one event's member changes; returns the event fields updated."""
    updated: Set[str] = set()
    if any(change["changed"] & set(IDENTITY_FIELDS) for change in changes):
        before = conn.execute("SELECT title, event_date FROM DeduplicatedEvents "
                              "WHERE deduplicated_event_id = ?", (dedup_id,)).fetchone()
        ledger._recompute_identity(dedup_id)
        after = conn.execute("SELECT title, event_date FROM DeduplicatedEvents "
                             "WHERE deduplicated_event_id = ?", (dedup_id,)).fetchone()
        updated |= {name for name in IDENTITY_FIELDS if before[name] != after[name]}

    current = conn.execute("SELECT * FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                           (dedup_id,)).fetchone()
    for change in changes:
        if not change["is_master"]:
            continue
        for name in sorted(change["changed"] & set(MASTER_COPIED_FIELDS)):
            # Only while the event still holds what it copied: a value that
            # differs was corrected at event level and outranks the record.
            if _same(current[name], change["old"].get(name)):
                conn.execute(f"UPDATE DeduplicatedEvents SET {name} = ? WHERE deduplicated_event_id = ?",
                             (change["new"][name], dedup_id))
                updated.add(name)

    if any("perplexity_enrichment_data" in change["changed"] for change in changes):
        # The victim is derived from members' research: mark the roles outdated
        # (a signature no membership produces) so the hygiene step's
        # ``classify-entities --stale-only`` redoes them.
        conn.execute("UPDATE DeduplicatedEvents SET roles_member_signature = 'record-content-changed' "
                     "WHERE deduplicated_event_id = ?", (dedup_id,))
        updated.add("roles")
    if updated:
        conn.execute("UPDATE DeduplicatedEvents SET updated_at = ? WHERE deduplicated_event_id = ?",
                     (datetime.now().isoformat(), dedup_id))
    return updated


def _same(left: Any, right: Any) -> bool:
    return str(left) == str(right) if left is not None and right is not None else left is right
