"""Field-level provenance of Perplexity enrichment facts.

``EnrichmentFactSupport`` holds one row per (record, fact): whether the fact is
stored because a source supports it (and which source - the record's own text
or a cited page, with its URL - and how that source states it: a date's
precision and basis, a count's unit, basis and scope), or was withheld because
nothing does (with the claim and the reason).  The withheld claim is kept here,
not in the enrichment JSON, so nothing downstream reads an unsupported value,
yet a re-check can judge the same claim again once a cited page becomes
readable.

Kept in its own table rather than inside ``perplexity_enrichment_data``: that
JSON is a tracked field of each record, and rewriting it on every record just
to add provenance would mark every event's roles stale for no change in
content.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping

FACT_SUPPORT_DDL = """
CREATE TABLE IF NOT EXISTS EnrichmentFactSupport (
    enriched_event_id TEXT NOT NULL,
    fact              TEXT NOT NULL,
    status            TEXT NOT NULL,      -- 'supported' | 'withheld'
    value_json        TEXT,
    confidence        REAL,
    source_kind       TEXT,               -- 'record' | 'cited_document' | 'entity'
    source_url        TEXT,
    reason            TEXT,
    details_json      TEXT,               -- date precision/basis; count unit/basis/scope
    checked_at        TEXT NOT NULL,
    PRIMARY KEY (enriched_event_id, fact)
)
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create ``EnrichmentFactSupport`` and ``CitedDocuments`` if missing (idempotent)."""
    from cyber_data_collector.processing import cited_documents

    conn.execute(FACT_SUPPORT_DDL)
    cited_documents.ensure_schema(conn)


def load_fact_support(conn: sqlite3.Connection,
                      enriched_event_ids: Iterable[str]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Stored support rows: enriched_event_id -> fact -> row."""
    ids = list(dict.fromkeys(enriched_event_ids))
    result: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        rows = conn.execute(
            "SELECT enriched_event_id, fact, status, value_json, confidence, source_kind, source_url, reason, "
            f"details_json FROM EnrichmentFactSupport WHERE enriched_event_id IN ({','.join('?' * len(chunk))})",
            chunk,
        ).fetchall()
        for row in rows:
            result.setdefault(row[0], {})[row[1]] = {
                "fact": row[1], "status": row[2],
                "value": json.loads(row[3]) if row[3] is not None else None,
                "confidence": row[4], "source_kind": row[5], "source_url": row[6], "reason": row[7],
                "details": json.loads(row[8]) if row[8] else None,
            }
    return result


def _row_values(enriched_event_id: str, row: Mapping[str, Any], now: str) -> tuple:
    details = row.get("details")
    return (enriched_event_id, row["fact"], row["status"],
            json.dumps(row.get("value"), default=str) if row.get("value") is not None else None,
            row.get("confidence"), row.get("source_kind"), row.get("source_url"), row.get("reason"),
            json.dumps(details, sort_keys=True, default=str) if details else None, now)


def upsert_fact_support(conn: sqlite3.Connection, enriched_event_id: str,
                        rows: Iterable[Mapping[str, Any]]) -> int:
    """Insert or replace the given facts' rows (other facts untouched)."""
    now = datetime.now().isoformat(timespec="seconds")
    values: List[tuple] = [_row_values(enriched_event_id, row, now) for row in rows]
    conn.executemany(
        "INSERT OR REPLACE INTO EnrichmentFactSupport (enriched_event_id, fact, status, value_json, "
        "confidence, source_kind, source_url, reason, details_json, checked_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        values)
    return len(values)


def replace_fact_support(conn: sqlite3.Connection, enriched_event_id: str,
                         rows: Iterable[Mapping[str, Any]]) -> int:
    """Make *rows* the record's complete set of support rows."""
    conn.execute("DELETE FROM EnrichmentFactSupport WHERE enriched_event_id = ?", (enriched_event_id,))
    return upsert_fact_support(conn, enriched_event_id, rows)
