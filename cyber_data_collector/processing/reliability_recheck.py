"""Re-judge stored Perplexity enrichments with the current acceptance gate.

Before the gate, every validated record was given ``data_source_reliability =
0.85`` whatever its answer said, and any cited URL - read or not, relevant or
not - let its facts into the typed columns.  This pass applies the rule new
answers now meet to every stored answer:

* each claimed fact must be stated in the record's own text or in a cited page
  about the same incident (pages are retrieved and cached in
  ``CitedDocuments``);
* reliability is recomputed from the accepted facts (mean fact confidence,
  averaged with the overall confidence when that is > 0; NULL when no accepted
  fact has a confidence);
* a withheld fact leaves the stored JSON (its claim is kept in
  ``EnrichmentFactSupport``), a typed column holding it is cleared - a
  withheld date reverts to the date the record carried before enrichment
  (``RawEvents.event_date``) - and, when the claim was confident enough to
  have been promoted, the record is un-validated and re-queued for
  enrichment (quarantine is kept).

Idempotent: claims are rebuilt from the JSON plus the withheld claims, so a
second run judges the same claims against the same cached pages and writes
nothing.  Run: ``python scripts/perplexity_backfill_events.py
--recompute-reliability [--dry-run]``.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence

from cyber_data_collector.processing.cited_documents import CitedDocumentStore
from cyber_data_collector.processing.enrichment_gate import (
    MAX_ENRICHMENT_ATTEMPTS,
    MIN_APPLY_CONFIDENCE,
    StoredRecheckPlan,
    evaluate_enrichment,
    plan_stored_recheck,
    stored_claims,
    valid_source_urls,
)
from cyber_data_collector.processing.record_evidence import RecordEvidence
from cyber_data_collector.storage.cyber_event_data_v2 import CyberEventDataV2
from cyber_data_collector.storage.enrichment_support import (
    ensure_schema,
    load_fact_support,
    replace_fact_support,
)

logger = logging.getLogger(__name__)

_ROW_QUERY = """
    SELECT ee.enriched_event_id, ee.status, CAST(ee.event_date AS TEXT) AS event_date,
           ee.date_confidence, ee.entity_confidence, ee.attacking_entity_name, ee.attack_method,
           ee.records_affected, ee.records_affected_unit, ee.records_affected_basis, ee.records_affected_scope,
           ee.records_affected_source, ee.perplexity_validated, ee.perplexity_enrichment_data,
           ee.data_source_reliability, COALESCE(ee.perplexity_attempt_count, 0) AS perplexity_attempt_count,
           ee.perplexity_status, ee.perplexity_last_error,
           re.raw_event_id, re.raw_title, re.raw_description, re.raw_content, re.source_url, re.source_type,
           CAST(re.discovered_at AS TEXT) AS discovered_at, CAST(re.event_date AS TEXT) AS raw_event_date,
           re.source_metadata
    FROM EnrichedEvents ee
    LEFT JOIN RawEvents re ON re.raw_event_id = ee.raw_event_id
    WHERE ee.perplexity_enrichment_data IS NOT NULL
"""

_WRITABLE = frozenset({
    "event_date", "date_confidence", "entity_confidence", "attacking_entity_name", "attack_method",
    "records_affected", "data_source_reliability", "perplexity_validated", "perplexity_status",
    "perplexity_last_error", "records_affected_unit", "records_affected_basis", "records_affected_scope",
    "records_affected_source", "perplexity_attempt_count",
})


@dataclass
class RecheckReport:
    """What a recheck found (or, on a dry run, would change)."""

    rows_examined: int = 0
    rows_changed: int = 0
    blobs_changed: int = 0
    reliability_changed: int = 0
    was_constant_085: int = 0
    reliability_bins: Counter = field(default_factory=Counter)
    rows_with_withheld: int = 0
    rows_unvalidated: int = 0
    rows_revalidated: int = 0
    active_in_retry_queue: int = 0
    count_details: Counter = field(default_factory=Counter)
    date_details: Counter = field(default_factory=Counter)
    quarantined: int = 0
    rows_rejected_entirely: int = 0
    unreadable_blobs: int = 0
    facts_withheld: Counter = field(default_factory=Counter)
    facts_pending: Counter = field(default_factory=Counter)
    columns_cleared: Counter = field(default_factory=Counter)
    support_sources: Counter = field(default_factory=Counter)
    documents: Counter = field(default_factory=Counter)
    by_status: Counter = field(default_factory=Counter)

    def lines(self) -> List[str]:
        out = [
            f"Rows examined: {self.rows_examined} (constant 0.85 before: {self.was_constant_085})",
            f"Rows changed: {self.rows_changed} (reliability changed: {self.reliability_changed}; "
            f"enrichment JSON changed: {self.blobs_changed})",
            "New reliability: " + ", ".join(f"{k}: {v}" for k, v in sorted(self.reliability_bins.items())),
            f"Rows with a withheld fact: {self.rows_with_withheld} "
            f"(re-queued for enrichment: {self.rows_unvalidated}; no fact supported: "
            f"{self.rows_rejected_entirely}; unreadable JSON: {self.unreadable_blobs})",
            f"Rows validated again: {self.rows_revalidated}",
            f"Active rows left in the enrichment retry queue (each costs up to {MAX_ENRICHMENT_ATTEMPTS - 1} "
            f"more Perplexity calls): {self.active_in_retry_queue}; quarantined: {self.quarantined}",
            "Facts withheld: " + ", ".join(f"{k} {v}" for k, v in self.facts_withheld.most_common()),
            "Withheld facts that re-queue the record: "
            + ", ".join(f"{k} {v}" for k, v in self.facts_pending.most_common()),
            "Typed columns cleared: " + ", ".join(f"{k} {v}" for k, v in self.columns_cleared.most_common()),
            "Accepted facts supported by: " + ", ".join(f"{k} {v}" for k, v in self.support_sources.most_common()),
            "Cited pages: " + ", ".join(f"{k} {v}" for k, v in self.documents.most_common()),
            "Rows by EnrichedEvents.status: " + ", ".join(f"{k} {v}" for k, v in self.by_status.most_common()),
            "Accepted counts: " + ", ".join(f"{k} {v}" for k, v in sorted(self.count_details.items())),
            "Accepted dates: " + ", ".join(f"{k} {v}" for k, v in sorted(self.date_details.items())),
        ]
        return out


def _bin(value: Optional[float]) -> str:
    if value is None:
        return "NULL"
    low = min(int(value * 10), 9) / 10
    return f"{low:.1f}-{low + 0.1:.1f}"


def _publication_day(source_metadata: Optional[str]) -> Optional[str]:
    """``publication_date`` from a RawEvents.source_metadata JSON, if any."""
    if not source_metadata:
        return None
    try:
        meta = json.loads(source_metadata)
    except (TypeError, ValueError):
        return None
    value = meta.get("publication_date") if isinstance(meta, dict) else None
    return str(value)[:10] if value else None


def _parse_day(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _load_organisations(conn: sqlite3.Connection, ids: Sequence[str]) -> Dict[str, List[str]]:
    roles = CyberEventDataV2.INCIDENT_ORGANISATION_ROLES
    result: Dict[str, List[str]] = {}
    for start in range(0, len(ids), 500):
        chunk = list(ids[start:start + 500])
        rows = conn.execute(
            "SELECT x.enriched_event_id, e.entity_name FROM EnrichedEventEntities x "
            "JOIN EntitiesV2 e ON e.entity_id = x.entity_id "
            f"WHERE x.enriched_event_id IN ({','.join('?' * len(chunk))}) "
            f"AND x.relationship_type IN ({','.join('?' * len(roles))})",
            (*chunk, *roles),
        ).fetchall()
        for enriched_id, name in rows:
            if name:
                result.setdefault(enriched_id, []).append(name)
    return result


def recheck_stored_enrichments(
    conn: sqlite3.Connection,
    store: CitedDocumentStore,
    *,
    dry_run: bool = True,
    limit: Optional[int] = None,
    enriched_event_ids: Optional[Sequence[str]] = None,
    min_apply_confidence: float = MIN_APPLY_CONFIDENCE,
) -> RecheckReport:
    """Re-judge every stored enrichment and (unless *dry_run*) write the result.

    Args:
        conn: Database connection (``row_factory`` is set to ``sqlite3.Row``
            for the duration).
        store: Cited-page cache; pages not yet read are fetched.  A dry run
            still caches the pages it reads (they are a cache of the web, not
            enrichment data), so the real run does not fetch them again.
        dry_run: Report only.
        limit / enriched_event_ids: Restrict the rows examined.

    Returns:
        A :class:`RecheckReport`.
    """
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        conn.commit()
        query = _ROW_QUERY
        params: List[Any] = []
        if enriched_event_ids:
            query += f" AND ee.enriched_event_id IN ({','.join('?' * len(enriched_event_ids))})"
            params.extend(enriched_event_ids)
        query += " ORDER BY ee.enriched_event_id"
        if limit:
            query += f" LIMIT {int(limit)}"
        rows = [dict(r) for r in conn.execute(query, params).fetchall()]
        ids = [r["enriched_event_id"] for r in rows]
        support = load_fact_support(conn, ids)
        organisations = _load_organisations(conn, ids)
    finally:
        conn.row_factory = previous_factory

    report = RecheckReport(rows_examined=len(rows))
    prepared = []
    all_urls: List[str] = []
    for row in rows:
        blob: Optional[Dict[str, Any]] = None
        is_readable = True
        try:
            parsed = json.loads(row["perplexity_enrichment_data"])
            if isinstance(parsed, dict):
                blob = parsed
            else:
                is_readable = False
        except (TypeError, ValueError):
            is_readable = False
        claims = stored_claims(blob if is_readable else {}, support.get(row["enriched_event_id"]))
        urls = valid_source_urls(claims.sources_consulted)
        all_urls.extend(urls)
        prepared.append((row, blob, is_readable, claims, urls))

    unique_urls = list(dict.fromkeys(all_urls))
    logger.info("Rechecking %d stored enrichment(s) citing %d distinct page(s)", len(rows), len(unique_urls))

    def _progress(done: int, total: int) -> None:
        if done % 250 == 0 or done == total:
            logger.info("  cited pages read: %d/%d", done, total)

    documents = store.get_documents(unique_urls, progress=_progress)
    report.documents.update(documents[u].status for u in unique_urls)

    for row, blob, is_readable, claims, urls in prepared:
        enriched_id = row["enriched_event_id"]
        report.by_status[row.get("status") or "?"] += 1
        if row.get("data_source_reliability") is not None and abs(row["data_source_reliability"] - 0.85) < 1e-9:
            report.was_constant_085 += 1
        evidence = RecordEvidence.from_row(row)
        gate = evaluate_enrichment(
            claims,
            record_text=evidence.match_text(),
            cited_documents={u: documents[u] for u in urls},
            organisations=organisations.get(enriched_id, []),
            reference_date=_parse_day(evidence.discovered_at),
            record_published=_publication_day(row.get("source_metadata")),
        )
        plan = plan_stored_recheck(row, blob, gate, is_blob_readable=is_readable,
                                   min_apply_confidence=min_apply_confidence)
        _tally(report, row, blob, plan, gate, is_readable)
        if not dry_run:
            _apply(conn, enriched_id, blob, plan, support.get(enriched_id, {}))

    if not dry_run:
        conn.commit()
    return report


def _tally(report: RecheckReport, row: Dict[str, Any], blob: Optional[Dict[str, Any]],
           plan: StoredRecheckPlan, gate: Any, is_readable: bool) -> None:
    report.reliability_bins[_bin(plan.reliability)] += 1
    blob_changed = plan.blob is not None and plan.blob != (blob or {})
    if blob_changed:
        report.blobs_changed += 1
    if "data_source_reliability" in plan.columns:
        report.reliability_changed += 1
    if plan.columns or blob_changed:
        report.rows_changed += 1
    if not is_readable:
        report.unreadable_blobs += 1
    if plan.withheld:
        report.rows_with_withheld += 1
        report.facts_withheld.update(plan.withheld.keys())
        report.facts_pending.update(plan.pending)
    if not gate.is_accepted:
        report.rows_rejected_entirely += 1
    if row.get("perplexity_validated") and not plan.is_validated:
        report.rows_unvalidated += 1
    status = plan.columns.get("perplexity_status", row.get("perplexity_status"))
    if not plan.is_validated and status == "retrying" and row.get("status") == "Active":
        report.active_in_retry_queue += 1
    if status == "quarantined":
        report.quarantined += 1
    if not row.get("perplexity_validated") and plan.is_validated:
        report.rows_revalidated += 1
    report.columns_cleared.update(plan.cleared)
    for decision in gate.decisions.values():
        if decision.is_accepted:
            report.support_sources[decision.source_kind or "?"] += 1
            if decision.name == "victim_count":
                report.count_details.update(f"{k}={v}" for k, v in sorted(decision.details.items()))
            if decision.name == "date":
                report.date_details.update(f"{k}={v}" for k, v in sorted(decision.details.items()))


_SUPPORT_KEYS = ("status", "value", "confidence", "source_kind", "source_url", "reason", "details")


def _support_unchanged(existing: Dict[str, Dict[str, Any]], rows: List[Dict[str, Any]]) -> bool:
    if set(existing) != {r["fact"] for r in rows}:
        return False
    for row in rows:
        old = existing[row["fact"]]
        for key in _SUPPORT_KEYS:
            # Values round-trip through JSON (a date is stored as its text).
            new_value = json.loads(json.dumps(row.get(key), default=str))
            if old.get(key) != new_value:
                return False
    return True


def _apply(conn: sqlite3.Connection, enriched_id: str, blob: Optional[Dict[str, Any]],
           plan: StoredRecheckPlan, existing_support: Dict[str, Dict[str, Any]]) -> None:
    assignments: List[str] = []
    values: List[Any] = []
    for column, value in plan.columns.items():
        if column not in _WRITABLE:
            raise ValueError(f"refusing to write unexpected column {column!r}")
        assignments.append(f"{column} = ?")
        values.append(value)
    if plan.blob is not None and plan.blob != (blob or {}):
        assignments.append("perplexity_enrichment_data = ?")
        values.append(json.dumps(plan.blob, default=str))
    if assignments:
        assignments.append("updated_at = ?")
        values.append(datetime.now().isoformat())
        conn.execute(f"UPDATE EnrichedEvents SET {', '.join(assignments)} WHERE enriched_event_id = ?",
                     (*values, enriched_id))
    if not _support_unchanged(existing_support, plan.support_rows):
        replace_fact_support(conn, enriched_id, plan.support_rows)
