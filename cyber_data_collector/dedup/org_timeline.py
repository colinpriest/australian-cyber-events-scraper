"""An organisation's researched cyber-incident history, for judging its events.

Judging events one at a time, or a pair at a time, cannot answer the questions
that decide them: how many incidents did this organisation actually have, and
when? The labellers who calibrated the reality and duplicate filters answered
exactly that by searching - Western Sydney University turned out to have four
separate incidents (2023, August 2024, January 2025, June 2025) - and that is
what the stored evidence lacked.

One Perplexity search per organisation with several events asks for its full
incident history: dated, with what happened and a source per incident,
including supplier breaches that exposed its data. The answer is stored in
``OrganisationTimelines`` and reused - a live search cannot be replayed - and
given to Jev (and to the escalation model) as ``organisation_timeline`` for
reality, duplicate and victim judgements.

**Keeping timelines current.** A timeline is re-researched whenever a record
about the organisation's events arrives after it was researched
(:func:`is_stale`), and when an organisation first reaches two events. Each
research is a new row, so every version is kept and the latest is used; a new
version changes the input of every judgement that shows it, so the
organisation's events and pairs are re-judged on the next run without any
other bookkeeping. Organisations with no new evidence cost nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from cyber_data_collector.utils.llm_models import perplexity_model

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS OrganisationTimelines (
    entity_id      INTEGER NOT NULL,
    entity_name    TEXT NOT NULL,
    query_hash     TEXT NOT NULL,
    query          TEXT NOT NULL,
    model          TEXT NOT NULL,
    answer         TEXT,
    citations_json TEXT,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (entity_id, query_hash)
);
"""
TIMELINE_CHARS = 3000
MAX_CITATIONS = 10

PROMPT = """List every publicly reported cyber incident affecting {name}{aliases}: data breaches, \
ransomware or extortion (including leak-site listings), account or email compromise, DDoS, \
accidental exposure of data, and breaches at its suppliers or service providers that exposed its \
data. Use news, regulator and company statements, court or parliamentary records and breach \
trackers. Treat incidents as separate only when they are separate attacks or exposures; list \
follow-up coverage of one incident under that incident.

Answer as a numbered list, oldest first, one incident per line:
DATE (to the day if reported, else month or year) | WHAT HAPPENED (attacker or method, systems, \
data and people affected) | WHEN AND HOW IT BECAME PUBLIC | SOURCE URL

If you find no incidents, answer: NONE FOUND."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def organisations_with_several_events(conn: sqlite3.Connection, minimum: int = 2) -> List[Dict[str, Any]]:
    """Organisations that are the victim of at least ``minimum`` active events."""
    rows = conn.execute(
        """SELECT v.entity_id, v.entity_name, COUNT(DISTINCT d.deduplicated_event_id) AS n
           FROM DeduplicatedEventEntities dee
           JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
           JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id
           WHERE dee.relationship_type = 'victim' AND d.status = 'Active'
           GROUP BY v.entity_id, v.entity_name HAVING n >= ?
           ORDER BY v.entity_name""", (minimum,)).fetchall()
    return [{"entity_id": r[0], "entity_name": r[1], "events": r[2]} for r in rows]


def _aliases(conn: sqlite3.Connection, entity_id: int, name: str) -> List[str]:
    """Other names the organisation's records use (Perplexity's formal names), most common first."""
    names: Counter = Counter()
    for (pe,) in conn.execute(
            """SELECT e.perplexity_enrichment_data
               FROM DeduplicatedEventEntities dee
               JOIN EventDeduplicationMap m ON m.deduplicated_event_id = dee.deduplicated_event_id
               JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
               WHERE dee.entity_id = ? AND dee.relationship_type = 'victim'""", (entity_id,)):
        try:
            formal = json.loads(pe or "{}").get("formal_entity_name")
        except (TypeError, ValueError, AttributeError):
            formal = None
        if formal and str(formal).strip().lower() != name.strip().lower():
            names[str(formal).strip()] += 1
    return [n for n, _ in names.most_common(2)]


def build_query(conn: sqlite3.Connection, entity_id: int, name: str) -> str:
    aliases = _aliases(conn, entity_id, name)
    alias_text = f" (also recorded as: {'; '.join(aliases)})" if aliases else ""
    return PROMPT.format(name=name, aliases=alias_text)


def stored(conn: sqlite3.Connection, entity_id: int) -> Optional[Dict[str, Any]]:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT entity_name, answer, citations_json, model, created_at FROM OrganisationTimelines "
        "WHERE entity_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (entity_id,)).fetchone()
    if row is None:
        return None
    return {"organisation": row[0], "timeline": (row[1] or "")[:TIMELINE_CHARS],
            "citations": json.loads(row[2] or "[]")[:MAX_CITATIONS], "model": row[3], "researched_at": row[4]}


def latest_evidence(conn: sqlite3.Connection, entity_id: int) -> Optional[str]:
    """When the newest record about this organisation's events was found."""
    row = conn.execute(
        """SELECT MAX(r.discovered_at)
           FROM DeduplicatedEventEntities dee
           JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id
           JOIN EventDeduplicationMap m ON m.deduplicated_event_id = d.deduplicated_event_id
           JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
           JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
           WHERE dee.entity_id = ? AND dee.relationship_type = 'victim' AND d.status = 'Active'""",
        (entity_id,)).fetchone()
    return row[0] if row and row[0] else None


def is_stale(conn: sqlite3.Connection, entity_id: int) -> bool:
    """A timeline is stale when a record about the organisation arrived after it was researched.

    A timeline that predates an incident does not list it, and the judgements
    read "matches nothing in the researched history" as evidence against an
    event - so a new real incident at an organisation with an old timeline
    would look unsupported. Refreshing whenever new evidence arrives keeps the
    timeline at least as recent as anything it is used to judge.
    """
    current = stored(conn, entity_id)
    if current is None:
        return True
    latest = latest_evidence(conn, entity_id)
    return bool(latest and str(latest) > str(current["researched_at"]))


def research(conn: sqlite3.Connection, entity_id: int, name: str,
             search: Optional[Callable[[str, str], Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """Research an organisation's timeline when it has none or it is stale; else reuse it.

    Every version is kept (one row per research); the latest is used, and
    because the timeline text is part of every judgement's input, a refreshed
    timeline re-judges the organisation's events and pairs automatically.
    """
    from cyber_data_collector.dedup.corroboration import _perplexity_search

    ensure_schema(conn)
    if not is_stale(conn, entity_id):
        return stored(conn, entity_id)
    model = perplexity_model()
    query = build_query(conn, entity_id, name)
    as_of = latest_evidence(conn, entity_id) or ""
    digest = hashlib.sha256(f"{model}\n{query}\n{as_of}".encode("utf-8")).hexdigest()
    if conn.execute("SELECT 1 FROM OrganisationTimelines WHERE entity_id = ? AND query_hash = ?",
                    (entity_id, digest)).fetchone():
        return stored(conn, entity_id)
    if search is None:
        if not os.getenv("PERPLEXITY_API_KEY"):
            return stored(conn, entity_id)
        search = _perplexity_search
    try:
        result = search(query, model)
    except Exception as exc:  # noqa: BLE001 - a failed search is retried next run
        logger.warning("Timeline research failed for %s: %s", name, exc)
        return stored(conn, entity_id)
    conn.execute("INSERT INTO OrganisationTimelines VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (entity_id, name, digest, query, model, result.get("answer"),
                  json.dumps(result.get("citations") or []), datetime.now().isoformat()))
    conn.commit()
    return stored(conn, entity_id)


def research_all(conn: sqlite3.Connection, search=None) -> int:
    """Research (or reuse) a timeline for every organisation with several events."""
    orgs = organisations_with_several_events(conn)
    done = sum(1 for o in orgs if research(conn, o["entity_id"], o["entity_name"], search=search))
    logger.info("%d of %d organisations with several events have a timeline", done, len(orgs))
    return done


_ITEM = re.compile(r"^\s*(\d+)[.)]\s*(.+)$")
_URL = re.compile(r"https?://\S+")


def parse_timeline(text: str) -> List[Dict[str, str]]:
    """Split a researched answer into incidents: date | what | how it became public | source.

    The answer is free text in a requested format, so a continuation line is
    appended to the incident above it rather than dropped, and an answer with
    no numbered list ("NONE FOUND", a refusal) parses to no incidents.
    """
    items: List[Dict[str, str]] = []
    for line in (text or "").splitlines():
        m = _ITEM.match(line)
        if not m:
            if items and line.strip():
                items[-1]["what"] += " " + line.strip()
            continue
        body = m.group(2).replace("**", "").strip()
        parts = [p.strip() for p in body.split("|")]
        urls = _URL.findall(body)
        items.append({
            "number": m.group(1),
            "date": parts[0] if len(parts) > 1 else "",
            "what": parts[1] if len(parts) > 1 else body,
            "public": parts[2] if len(parts) > 3 else "",
            "source": urls[-1].rstrip(").,") if urls else "",
        })
    return items


def timeline_for_event(conn: sqlite3.Connection, dedup_id: str) -> Optional[Dict[str, Any]]:
    """The stored timeline of the event's victim, when it has one."""
    ensure_schema(conn)
    row = conn.execute(
        """SELECT t.entity_id FROM DeduplicatedEventEntities dee
           JOIN OrganisationTimelines t ON t.entity_id = dee.entity_id
           WHERE dee.deduplicated_event_id = ? AND dee.relationship_type = 'victim'
           ORDER BY t.entity_id LIMIT 1""", (dedup_id,)).fetchone()
    return stored(conn, row[0]) if row else None
