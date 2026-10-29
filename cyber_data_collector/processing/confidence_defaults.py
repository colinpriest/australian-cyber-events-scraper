"""Confidence values that were defaults, not scores - found by provenance, cleared on request.

Discovery used to store ``0.7`` when an event's confidence was missing and a
constant ``0.8`` / ``0.6`` for the entities of the single-record path, and
validation turned a missing score into ``0.0``. Stored, those looked exactly
like measured model output. New records store NULL; this module finds the old
values and clears **only those whose provenance proves they were defaulted**.

The proof is a trace of the code path that wrote the default, not the value:

* **EnrichedEvents.confidence_score = 0.7** is a default when the record's
  ``source_metadata`` holds no numeric ``confidence`` *and* ``ProcessingLog``
  shows the ``llm_analysis`` stage succeeded for it - the one path that wrote
  ``source_metadata.get('confidence', 0.7)``. (On the 2026-10-29 copy: all 54
  such rows have both, and the 28 other ``llm_analysis`` rows hold the
  source's own 0.65 / 0.5 and are left alone.)
* **EnrichedEventEntities.confidence_score** of 0.8 or 0.6 on an enriched event
  written by that same path: its entities got the constants 0.8 (primary
  target) and 0.6 (affected), whatever role they later had.
* **DeduplicatedEvents.confidence_score** equal to a cleared master's old value:
  the deduplicated event copies its master's score.

Anything else at 0.7 / 0.8 is **ambiguous** - a source prior, a classifier
adjustment and a default can all land on the same number - and is reported,
never changed. Every cleared value is kept in ``ConfidenceRepairs`` (table, key,
old value, rule, time), so the repair can be audited or reversed, and a second
run finds nothing to do.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: The default the single-record path gave an event with no score.
EVENT_DEFAULT = 0.7
#: The constants the same path gave its entities.
ENTITY_DEFAULTS = (0.8, 0.6)
#: Values the finding is about; counted per table so ambiguity can be reported.
SUSPECT_VALUES = (0.7, 0.8)

RULE_EVENT = "event: llm_analysis path without a source confidence (0.7)"
RULE_ENTITY = "entity link: llm_analysis path constants (0.8 primary target, 0.6 affected)"
RULE_DEDUP = "deduplicated event: copy of a cleared master"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ConfidenceRepairs (
    table_name  TEXT NOT NULL,
    row_key     TEXT NOT NULL,
    column_name TEXT NOT NULL,
    old_value   REAL NOT NULL,
    rule        TEXT NOT NULL,
    repaired_at TEXT NOT NULL,
    PRIMARY KEY (table_name, row_key, column_name)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _has_numeric_confidence(source_metadata: Optional[str]) -> bool:
    """Whether a record's stored source metadata holds a confidence the source gave."""
    try:
        metadata = json.loads(source_metadata or "{}")
    except ValueError:
        return False
    value = metadata.get("confidence") if isinstance(metadata, dict) else None
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass
class Finding:
    table: str
    key: str                       # row identifier (for links: "<enriched_event_id>|<entity_id>")
    column: str
    value: float
    rule: str


@dataclass
class ConfidenceReport:
    """What is provably defaulted (``proven``) and what is only suspect (``ambiguous``)."""

    proven: List[Finding] = field(default_factory=list)
    #: ``(table, value)`` -> rows at exactly that value (0.7 / 0.8) that no proof covers.
    ambiguous: Dict[Tuple[str, float], int] = field(default_factory=dict)
    #: Ambiguous EnrichedEvents by ``(source_type, value)``.
    ambiguous_events_by_source: Dict[Tuple[str, float], int] = field(default_factory=dict)
    unscored_events: int = 0

    @property
    def proven_by_rule(self) -> Dict[str, int]:
        return dict(Counter(f.rule for f in self.proven))

    def lines(self) -> List[str]:
        out = [f"Proven defaults (cleared by the repair): {len(self.proven)}"]
        for rule, n in sorted(self.proven_by_rule.items()):
            out.append(f"  {n:>6}  {rule}")
        total_ambiguous = sum(self.ambiguous.values())
        out.append(f"Ambiguous values at exactly 0.7 / 0.8, provenance cannot tell (left unchanged): {total_ambiguous}")
        for (table, value), n in sorted(self.ambiguous.items()):
            out.append(f"  {n:>6}  {table}.confidence_score = {value}")
        for (source, value), n in sorted(self.ambiguous_events_by_source.items()):
            out.append(f"  {n:>6}  EnrichedEvents from {source} = {value}")
        out.append(f"Enriched events with no score (NULL): {self.unscored_events}")
        return out


def _llm_path_raw_ids(conn: sqlite3.Connection) -> set:
    if not _has_table(conn, "ProcessingLog"):
        return set()
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT raw_event_id FROM ProcessingLog WHERE processing_stage = 'llm_analysis' "
        "AND status = 'success'")}


def _near(column: str, value: float) -> str:
    return f"ABS({column} - {value}) < 1e-9"


def find_defaults(conn: sqlite3.Connection) -> ConfidenceReport:
    """Read-only: classify every stored 0.7 / 0.8 (and the path constants) as proven or ambiguous."""
    report = ConfidenceReport()
    llm_raw = _llm_path_raw_ids(conn)

    proven_events: Dict[str, float] = {}
    if _has_table(conn, "EnrichedEvents") and "confidence_score" in _columns(conn, "EnrichedEvents"):
        report.unscored_events = conn.execute(
            "SELECT COUNT(*) FROM EnrichedEvents WHERE confidence_score IS NULL").fetchone()[0]
        for eid, raw_id, value, source_type, metadata in conn.execute(
                "SELECT e.enriched_event_id, e.raw_event_id, e.confidence_score, r.source_type, r.source_metadata "
                "FROM EnrichedEvents e LEFT JOIN RawEvents r ON r.raw_event_id = e.raw_event_id "
                "WHERE " + " OR ".join(_near("e.confidence_score", v) for v in SUSPECT_VALUES)):
            if (value is not None and abs(value - EVENT_DEFAULT) < 1e-9 and raw_id in llm_raw
                    and not _has_numeric_confidence(metadata)):
                report.proven.append(Finding("EnrichedEvents", eid, "confidence_score", value, RULE_EVENT))
                proven_events[eid] = value
            else:
                key = ("EnrichedEvents", round(value, 2))
                report.ambiguous[key] = report.ambiguous.get(key, 0) + 1
                skey = (source_type or "?", round(value, 2))
                report.ambiguous_events_by_source[skey] = report.ambiguous_events_by_source.get(skey, 0) + 1

    # Entities of an event the llm_analysis path wrote carry its constants (0.8 / 0.6).
    if _has_table(conn, "EnrichedEventEntities") and _has_table(conn, "EnrichedEvents"):
        llm_events = {eid for eid, raw_id in conn.execute("SELECT enriched_event_id, raw_event_id FROM EnrichedEvents")
                      if raw_id in llm_raw}
        for eid, entity_id, value in conn.execute(
                "SELECT enriched_event_id, entity_id, confidence_score FROM EnrichedEventEntities "
                "WHERE confidence_score IS NOT NULL"):
            if eid in llm_events and any(abs(value - d) < 1e-9 for d in ENTITY_DEFAULTS):
                report.proven.append(Finding("EnrichedEventEntities", f"{eid}|{entity_id}", "confidence_score",
                                             value, RULE_ENTITY))
            elif any(abs(value - v) < 1e-9 for v in SUSPECT_VALUES):
                key = ("EnrichedEventEntities", round(value, 2))
                report.ambiguous[key] = report.ambiguous.get(key, 0) + 1

    # A deduplicated event copies its master's score; it is a default when the master's was (cleared now, or earlier).
    proven_masters = dict(proven_events)
    for table_name, row_key, old_value in (conn.execute(
            "SELECT table_name, row_key, old_value FROM ConfidenceRepairs WHERE table_name = 'EnrichedEvents'")
            if _has_table(conn, "ConfidenceRepairs") else ()):
        proven_masters[row_key] = old_value
    if _has_table(conn, "DeduplicatedEvents"):
        for did, master, value in conn.execute(
                "SELECT deduplicated_event_id, master_enriched_event_id, confidence_score FROM DeduplicatedEvents "
                "WHERE " + " OR ".join(_near("confidence_score", v) for v in SUSPECT_VALUES)):
            if master in proven_masters and abs(value - proven_masters[master]) < 1e-9:
                report.proven.append(Finding("DeduplicatedEvents", did, "confidence_score", value, RULE_DEDUP))
            else:
                key = ("DeduplicatedEvents", round(value, 2))
                report.ambiguous[key] = report.ambiguous.get(key, 0) + 1

    # Rows with no provenance in the schema at all: counted, never touched.
    for table in ("DeduplicatedEventEntities", "EntitiesV2"):
        if _has_table(conn, table) and "confidence_score" in _columns(conn, table):
            for value, n in conn.execute(
                    f"SELECT ROUND(confidence_score, 2), COUNT(*) FROM {table} WHERE "
                    + " OR ".join(_near("confidence_score", v) for v in SUSPECT_VALUES) + " GROUP BY 1"):
                report.ambiguous[(table, value)] = n

    return report


def repair(conn: sqlite3.Connection, dry_run: bool = False) -> ConfidenceReport:
    """Set every proven default to NULL, recording each in ``ConfidenceRepairs``. Idempotent.

    With ``dry_run`` nothing is written. The returned report is the one
    computed before the repair (what was, or would be, cleared).
    """
    report = find_defaults(conn)
    if dry_run or not report.proven:
        return report
    ensure_schema(conn)
    stamp = datetime.now().isoformat(timespec="seconds")
    try:
        for f in report.proven:
            conn.execute("INSERT OR IGNORE INTO ConfidenceRepairs VALUES (?, ?, ?, ?, ?, ?)",
                         (f.table, f.key, f.column, f.value, f.rule, stamp))
            if f.table == "EnrichedEventEntities":
                eid, entity_id = f.key.split("|", 1)
                conn.execute("UPDATE EnrichedEventEntities SET confidence_score = NULL WHERE enriched_event_id = ? "
                             "AND entity_id = ?", (eid, int(entity_id)))
            else:
                key_column = {"EnrichedEvents": "enriched_event_id",
                              "DeduplicatedEvents": "deduplicated_event_id"}[f.table]
                conn.execute(f"UPDATE {f.table} SET confidence_score = NULL WHERE {key_column} = ?", (f.key,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    logger.info("Cleared %d confidence default(s); each is recorded in ConfidenceRepairs", len(report.proven))
    return report


def remaining_provable(conn: sqlite3.Connection) -> int:
    """Provable defaults still stored (the quality report's number; 0 after the repair)."""
    return len(find_defaults(conn).proven)


def summary_counts(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Counts for the quality report: provable defaults, ambiguous 0.7/0.8, unscored events."""
    report = find_defaults(conn)
    return {"proven": len(report.proven), "ambiguous": sum(report.ambiguous.values()),
            "unscored_events": report.unscored_events}
