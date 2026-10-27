"""Search the web for public reporting of one stored event, and keep what was found.

The event-reality judgement (:mod:`event_reality`) can only be as sure as its
evidence. Calibration on 168 labelled events (2026-10-27) showed where that
fails: real incidents - Apunipima, ASIC's Accellion breach, Ventia, Western
Sydney University, LUSH - scored as non-incidents because the page stored for
them was a different page or the attached research was thin, while a web
search confirmed each in seconds. This supplies that search.

One Perplexity query per event asks whether credible public reporting of
*this* incident exists, built from the event's title, recorded victim, date,
and the organisations and titles on its records. Perplexity publishes no
dated model snapshots and the web changes, so a search cannot be replayed:
the query, the answer, the citations and the model are stored in
``EventCorroborations`` and reused, and the judgement reads the stored answer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from cyber_data_collector.utils.llm_models import perplexity_model

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS EventCorroborations (
    deduplicated_event_id TEXT NOT NULL,
    query_hash            TEXT NOT NULL,
    query                 TEXT NOT NULL,
    model                 TEXT NOT NULL,
    answer                TEXT,
    citations_json        TEXT,
    created_at            TEXT NOT NULL,
    PRIMARY KEY (deduplicated_event_id, query_hash)
);
"""
MAX_CITATIONS = 8

PROMPT = """Search for credible public reporting of the specific cyber incident described below \
(news, regulator or company statements, ransomware leak-site trackers, court or parliamentary \
records). The stored description may be wrong or generated from an unrelated page, so look for \
the incident itself.

{details}

Answer in exactly this format:
FOUND: yes / no / unclear
INCIDENT: one or two sentences - who was affected, what happened, when. "none found" if nothing.
SOURCES: the URLs you relied on, one per line."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def build_query(state: Dict[str, Any]) -> str:
    """The query for an event, from the same state the judgement sees."""
    event = state.get("event", {})
    records = state.get("records", [])
    orgs = []
    for r in records:
        name = (r.get("perplexity") or {}).get("formal_entity_name")
        if name and name not in orgs:
            orgs.append(name)
    titles = [r["title"] for r in records if r.get("title")][:3]
    lines = [f"Event title: {event.get('title') or '(none)'}",
             f"Recorded victim: {event.get('victim') or '(none)'}",
             f"Recorded date: {event.get('date') or '(unknown)'}"]
    if orgs:
        lines.append("Organisations named in research on its records: " + "; ".join(orgs[:3]))
    if titles:
        lines.append("Titles of its records: " + " | ".join(titles))
    return PROMPT.format(details="\n".join(lines))


def _query_hash(query: str, model: str) -> str:
    return hashlib.sha256(f"{model}\n{query}".encode("utf-8")).hexdigest()


def stored(conn: sqlite3.Connection, dedup_id: str) -> Optional[Dict[str, Any]]:
    """The latest stored corroboration for an event, or None."""
    ensure_schema(conn)
    row = conn.execute(
        "SELECT answer, citations_json, model, created_at FROM EventCorroborations "
        "WHERE deduplicated_event_id = ? ORDER BY created_at DESC LIMIT 1", (dedup_id,)).fetchone()
    if row is None:
        return None
    return {"answer": row[0], "citations": json.loads(row[1] or "[]")[:MAX_CITATIONS],
            "model": row[2], "searched_at": row[3]}


def _perplexity_search(query: str, model: str) -> Dict[str, Any]:
    import requests

    response = requests.post(
        "https://api.perplexity.ai/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['PERPLEXITY_API_KEY']}",
                 "Content-Type": "application/json"},
        json={"model": model, "messages": [{"role": "user", "content": query}], "temperature": 0.0},
        timeout=90,
    )
    response.raise_for_status()
    payload = response.json()
    return {"answer": payload["choices"][0]["message"]["content"],
            "citations": payload.get("citations") or []}


def corroborate(conn: sqlite3.Connection, dedup_id: str, state: Dict[str, Any],
                search: Optional[Callable[[str, str], Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """Search once for this event and store the result; reuse it on later runs.

    Returns the stored corroboration, or None when no search could be made
    (no key, or the search failed - retried next run).
    """
    ensure_schema(conn)
    model = perplexity_model()
    query = build_query(state)
    digest = _query_hash(query, model)
    if conn.execute("SELECT 1 FROM EventCorroborations WHERE deduplicated_event_id = ? AND query_hash = ?",
                    (dedup_id, digest)).fetchone():
        return stored(conn, dedup_id)
    if search is None:
        if not os.getenv("PERPLEXITY_API_KEY"):
            return None
        search = _perplexity_search
    try:
        result = search(query, model)
    except Exception as exc:  # noqa: BLE001 - one failed search never decides an event
        logger.warning("Corroboration search failed for %s: %s", dedup_id, exc)
        return None
    conn.execute("INSERT INTO EventCorroborations VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (dedup_id, digest, query, model, result.get("answer"),
                  json.dumps(result.get("citations") or []), datetime.now().isoformat()))
    conn.commit()
    return stored(conn, dedup_id)
