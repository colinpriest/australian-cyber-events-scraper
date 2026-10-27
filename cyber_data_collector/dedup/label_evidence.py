"""Evidence for labelled-real events whose only stored page is junk.

The source gate removes a page that does not report an incident, but an event
labelled real (``evaluation/event_reality_labels.csv``) is kept even when that
page was its only record, so the label is not contradicted. Such an event then
holds no active evidence, and its junk page stays mapped to it - which the
source-gate labels contradict, and which blocked publication on 2026-10-28
(Home Affairs / ZircoDATA, University of Sydney).

The labeller usually cited the reporting they found (the ``corroboration``
column). This module turns that citation into evidence, in two idempotent
steps run by ``dedup_admin.py label-evidence`` at the start of every hygiene
phase:

1. **queue** - for each Active labelled-real event with no active record, store
   the cited URL as an unprocessed ``RawEvents`` row (source type
   ``LabelledEvidence``). The next refresh scrapes, enriches and deduplicates it
   like any other record.
2. **attach** - once that record is in an event, the label says which incident
   it is evidence of: its event is merged into the labelled one (a cached
   ``different`` ruling still blocks it), and the labelled event's inactive
   records are split out together and rejected, now that it has real evidence.
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from cyber_data_collector.dedup import event_reality

logger = logging.getLogger(__name__)

ORIGIN = "labelled evidence"
SOURCE_TYPE = "LabelledEvidence"


def _cited_urls(labels_path: Path) -> Dict[str, Dict[str, str]]:
    """``{event_id: {"url", "rationale"}}`` for real labels that cite a URL."""
    with Path(labels_path).open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return {r["deduplicated_event_id"].strip(): {"url": r["corroboration"].strip(), "rationale": r["rationale"]}
            for r in rows
            if r.get("label", "").strip() == "real"
            and (r.get("corroboration") or "").strip().lower().startswith(("http://", "https://"))}


def _active_members(conn: sqlite3.Connection, event_id: str) -> List[str]:
    return [r[0] for r in conn.execute(
        """SELECT m.enriched_event_id FROM EventDeduplicationMap m
           JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
           WHERE m.deduplicated_event_id = ? AND e.status = 'Active'""", (event_id,))]


def _meta(text: Optional[str]) -> Dict[str, Any]:
    try:
        meta = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _same_page(a: str, b: str) -> bool:
    """URLs naming the same page, ignoring scheme, ``www.`` and a trailing slash."""
    def key(url: str) -> str:
        url = url.strip().lower().split("://", 1)[-1]
        return (url[4:] if url.startswith("www.") else url).rstrip("/")
    return key(a) == key(b)


def _queued(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Records holding a labelled event's cited report (``source_metadata.label_evidence``)."""
    out = []
    for raw_id, url, metadata, processed in conn.execute(
            "SELECT raw_event_id, source_url, source_metadata, COALESCE(is_processed, 0) FROM RawEvents "
            "WHERE source_metadata LIKE '%label_evidence%'"):
        link = _meta(metadata).get("label_evidence")
        if isinstance(link, dict) and link.get("event"):
            out.append({"raw_event_id": raw_id, "url": url, "event": link["event"], "processed": bool(processed)})
    return out


def queue(conn: sqlite3.Connection, labels_path: Path = event_reality.LABELS_PATH,
          dry_run: bool = False) -> List[Dict[str, str]]:
    """Queue the cited report of every labelled-real Active event that has no active record.

    A report already stored that never became a record (Cyber Daily's ZircoDATA
    article was stored in 2025, behind its registration wall, and dropped) is
    marked and processed again rather than stored twice; otherwise a new record
    is stored. Idempotent: a report already linked to the event is not queued
    again.
    """
    from cyber_data_collector.quality.regression import EventResolver

    resolver = EventResolver(conn)
    already = [(q["event"], q["url"]) for q in _queued(conn)]
    queued = []
    for event_id, cited in sorted(_cited_urls(labels_path).items()):
        now, status = resolver.resolve(event_id)
        if status != "Active" or _active_members(conn, now):
            continue
        if any(e == event_id and _same_page(u or "", cited["url"]) for e, u in already):
            continue
        title, when, victim = conn.execute(
            "SELECT title, event_date, victim_organization_name FROM DeduplicatedEvents "
            "WHERE deduplicated_event_id = ?", (now,)).fetchone()
        link = {"event": event_id, "labels_file": Path(labels_path).name}
        tail = cited["url"].rstrip("/").rsplit("/", 1)[-1]
        stored = [r for r in conn.execute(
            "SELECT raw_event_id, source_url, source_metadata FROM RawEvents WHERE source_url LIKE ?",
            (f"%{tail}%",)) if _same_page(r[1] or "", cited["url"])]
        reusable = [r for r in stored if not conn.execute(
            "SELECT 1 FROM EnrichedEvents WHERE raw_event_id = ?", (r[0],)).fetchone()]
        entry = {"event": event_id, "url": cited["url"], "title": title}
        if stored and not reusable:
            entry["action"] = "stored already and enriched; not queued (attach it with dedup_admin.py merge)"
            queued.append(entry)
            continue
        if reusable:
            raw_id, meta = reusable[0][0], _meta(reusable[0][2])
            entry.update(raw_event_id=raw_id, action="stored already, never a record: processed again")
            if not dry_run:
                conn.execute("UPDATE RawEvents SET is_processed = 0, processing_error = NULL, source_metadata = ? "
                             "WHERE raw_event_id = ?", (json.dumps({**meta, "label_evidence": link}), raw_id))
        else:
            raw_id = str(uuid.uuid4())
            entry.update(raw_event_id=raw_id, action="queued")
            if not dry_run:
                conn.execute(
                    "INSERT INTO RawEvents (raw_event_id, source_type, source_event_id, raw_title, raw_description, "
                    "raw_content, event_date, source_url, source_metadata, discovered_at, is_processed) "
                    "VALUES (?, ?, NULL, ?, ?, NULL, ?, ?, ?, ?, 0)",
                    (raw_id, SOURCE_TYPE, title or f"{victim} cyber incident", cited["rationale"],
                     str(when)[:10] if when else None, cited["url"],
                     json.dumps({"origin": ORIGIN, "label_evidence": link}), datetime.now().isoformat()))
        queued.append(entry)
    if not dry_run:
        conn.commit()
    return queued


def attach(conn: sqlite3.Connection, dry_run: bool = False) -> List[Dict[str, Any]]:
    """Put each processed label-evidence record into its labelled event, then
    split out and reject that event's inactive records.

    Records still unprocessed (or rejected at classification) are left alone;
    they are reported by the next run.
    """
    from cyber_data_collector.dedup.ledger import DedupLedger
    from cyber_data_collector.quality.regression import EventResolver

    done = []
    for q in _queued(conn):
        resolver = EventResolver(conn)
        target, status = resolver.resolve(q["event"])
        entry: Dict[str, Any] = {**q, "target": target}
        if status != "Active":
            entry["action"] = f"labelled event is {status}; nothing attached"
            done.append(entry)
            continue
        placed = sorted({e for (rid,) in conn.execute(
            "SELECT enriched_event_id FROM EnrichedEvents WHERE raw_event_id = ? AND status = 'Active'",
            (q["raw_event_id"],)) for e in resolver.events_of.get(rid, ())
            if resolver.status.get(e) == "Active"} - {target})
        ledger = DedupLedger(conn)
        merged, blocked = [], []
        for source in placed:
            if dry_run:
                merged.append(source)
                continue
            try:
                ledger.merge_events(target, source, actor="rule",
                                    reason=f"label evidence: {q['url']} is evidence of labelled event {q['event']}")
                merged.append(source)
            except ValueError as exc:  # a cached 'different' ruling wins over the label link
                blocked.append(f"{source}: {exc}")
        entry.update(merged=merged, blocked=blocked)
        members = conn.execute("SELECT enriched_event_id FROM EventDeduplicationMap WHERE deduplicated_event_id = ?",
                               (target,)).fetchall()
        active = set(_active_members(conn, target))
        has_evidence = bool(active) or bool(dry_run and merged)
        inactive = [m[0] for m in members if m[0] not in active]
        if has_evidence and inactive and not dry_run:
            split = ledger.split_members(target, inactive, actor="rule",
                                         reason="label evidence attached: inactive records are no evidence")
            event_reality.reject_events(conn, [{"deduplicated_event_id": split}])
            entry["rejected_split"] = split
        entry["inactive_split_out"] = inactive if has_evidence else []
        if has_evidence:
            entry["action"] = "attached"
        elif q["processed"]:
            error = conn.execute("SELECT processing_error FROM RawEvents WHERE raw_event_id = ?",
                                 (q["raw_event_id"],)).fetchone()[0]
            entry["action"] = (f"REJECTED: the cited report was processed but is not an active record "
                               f"({error or 'no reason recorded'}); the event still has no evidence")
        else:
            entry["action"] = "waiting: the cited report has not been processed yet"
        done.append(entry)
    if not dry_run:
        conn.commit()
    return done


def run(conn: sqlite3.Connection, labels_path: Path = event_reality.LABELS_PATH,
        dry_run: bool = False) -> Dict[str, Any]:
    """Attach what has been processed, then queue what is still missing."""
    attached = attach(conn, dry_run=dry_run)
    queued = queue(conn, labels_path, dry_run=dry_run)
    return {"attached": attached, "queued": queued}


def summary(result: Dict[str, Any]) -> Optional[str]:
    lines = []
    for a in result["attached"]:
        lines.append(f"  {a['event'][:8]} <- {a['url']}: {a['action']}"
                     + (f"; merged {', '.join(s[:8] for s in a.get('merged', []))}" if a.get("merged") else "")
                     + (f"; inactive split out and rejected: {len(a['inactive_split_out'])}"
                        if a.get("inactive_split_out") else "")
                     + (f"; BLOCKED {a['blocked']}" if a.get("blocked") else ""))
    for q in result["queued"]:
        lines.append(f"  {q['event'][:8]} {q['url']}: {q['action']}")
    return "\n".join(lines) or None
