"""Entity profiles: what an organisation is, where, and what it runs - reference evidence for disambiguation.

A name alone cannot say that "Medicare" is an Australian government programme holding health data, that the
Medicare Statistics Reporting Service is run by an agency, or that "AP" in an Australian cyber story is more
likely Advance Press than Associated Press. A profile records that, as attributes of the entity:

========================  ====================================================================
``jurisdiction``          country / state it is in or answers to
``sector``                industry or function (health, education, banking...)
``type``                  government agency, company, university, non-profit...
``administered_by``       its parent or administering organisation
``runs``                  services or systems it operates ("Medicare Statistics Reporting Service")
``holds_data``            the kinds of data it holds (health records, student records...)
``known_as``              other names it is known by
``website``               its official website (the most reliable identity evidence)
========================  ====================================================================

Rows come from two sources. ``human`` rows are entered with ``dedup_admin.py entity-profile set`` (a reason is
required) and are **never replaced or removed by a research run**; ``llm`` rows come from one Perplexity lookup
per organisation (:func:`research_profile`, citations kept), asked once and again only when the organisation's
evidence changes. A curated attribute is reference *input* to the rules below, not an edit of an outcome.

**Use.** Profiles are shown to Jev as state - the candidates of a victim choice, the candidates of an acronym,
both names of an alias question - and fingerprinted into the alias cache, so a changed profile re-asks an
``unsure`` pair. They are evidence, never a matcher: nothing is decided from a profile alone.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from cyber_data_collector.utils.llm_models import perplexity_model

logger = logging.getLogger(__name__)

KINDS = ("jurisdiction", "sector", "type", "administered_by", "runs", "holds_data", "known_as", "website")
#: Kinds with one value: a human value for one of these replaces whatever research says.
SINGLE_VALUED = ("jurisdiction", "type", "administered_by", "website")
PROMPT_VERSION = "entity-profile-v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS EntityProfiles (
    profile_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id   INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    value       TEXT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('llm', 'human')),
    evidence    TEXT,                 -- citations (llm) or the reason (human)
    created_at  TEXT NOT NULL,
    UNIQUE (entity_id, kind, value)
);
CREATE INDEX IF NOT EXISTS idx_entity_profiles_entity ON EntityProfiles(entity_id);
CREATE TABLE IF NOT EXISTS EntityProfileResearch (
    entity_id      INTEGER PRIMARY KEY,
    evidence_hash  TEXT NOT NULL,     -- the incidents the lookup was told about
    prompt_version TEXT NOT NULL,
    model          TEXT NOT NULL,
    status         TEXT NOT NULL,     -- researched / unavailable
    citations_json TEXT,
    researched_at  TEXT NOT NULL
);
"""

RESEARCH_PROMPT = """Look up the organisation "{name}" online. It was named in reporting of Australian cyber-security incidents{context}.

Answer with a JSON object only, using what public sources (its own website, Wikipedia, registers, news) say, and
null or [] where you cannot tell - never guess from the name:
{{"jurisdiction": "<country, and state if it is a state body>", "sector": ["<industry or function>"],
  "type": "<government agency | company | university | non-profit | programme | other>",
  "administered_by": "<the parent or administering organisation, or null>",
  "runs": ["<services, systems or portals it operates>"], "holds_data": ["<kinds of data it holds>"],
  "known_as": ["<other names it is known by>"], "website": "<official website or null>"}}"""

Search = Callable[[str], Optional[Dict[str, Any]]]


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _entity_id(conn: sqlite3.Connection, name_or_id: Any) -> Optional[int]:
    if isinstance(name_or_id, int):
        return name_or_id
    row = conn.execute("SELECT entity_id FROM EntitiesV2 WHERE entity_name = ? COLLATE NOCASE ORDER BY entity_id LIMIT 1",
                       (str(name_or_id),)).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------- human entry

def set_profile(conn: sqlite3.Connection, entity: Any, kind: str, value: str, reason: str) -> int:
    """Record a human attribute; a reason is required. A single-valued kind replaces every other value of it.

    Raises ``ValueError`` for an unknown kind, a blank value or reason, or an unknown entity. Returns the entity id.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown profile kind {kind!r}; one of {', '.join(KINDS)}")
    if not (value or "").strip() or not (reason or "").strip():
        raise ValueError("a value and a reason are required")
    entity_id = _entity_id(conn, entity)
    if entity_id is None:
        raise ValueError(f"no entity named {entity!r}")
    ensure_schema(conn)
    if kind in SINGLE_VALUED:
        conn.execute("DELETE FROM EntityProfiles WHERE entity_id = ? AND kind = ?", (entity_id, kind))
    conn.execute("INSERT OR REPLACE INTO EntityProfiles (entity_id, kind, value, source, evidence, created_at) "
                 "VALUES (?, ?, ?, 'human', ?, ?)",
                 (entity_id, kind, value.strip(), reason.strip(), datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return entity_id


def store_researched(conn: sqlite3.Connection, entity_id: int, attributes: Dict[str, Any], citations: Sequence[str]) -> int:
    """Replace the entity's ``llm`` rows with ``attributes``; ``human`` rows are never touched or contradicted.

    A researched value of a single-valued kind is dropped when a human row of that kind exists. Returns the
    number of rows stored.
    """
    ensure_schema(conn)
    human_kinds = {r[0] for r in conn.execute(
        "SELECT DISTINCT kind FROM EntityProfiles WHERE entity_id = ? AND source = 'human'", (entity_id,))}
    conn.execute("DELETE FROM EntityProfiles WHERE entity_id = ? AND source = 'llm'", (entity_id,))
    evidence = json.dumps(list(citations)[:5])
    now = datetime.now().isoformat(timespec="seconds")
    stored = 0
    for kind in KINDS:
        if kind in SINGLE_VALUED and kind in human_kinds:
            continue
        raw = attributes.get(kind)
        values = raw if isinstance(raw, list) else [raw]
        for value in values:
            text = str(value).strip() if isinstance(value, (str, int, float)) else ""
            if not text or text.lower() in ("null", "none", "unknown", "n/a"):
                continue
            cursor = conn.execute("INSERT OR IGNORE INTO EntityProfiles (entity_id, kind, value, source, evidence, "
                                  "created_at) VALUES (?, ?, ?, 'llm', ?, ?)", (entity_id, kind, text[:200], evidence, now))
            stored += cursor.rowcount
    conn.commit()
    return stored


# ---------------------------------------------------------------- reading

def profile_for(conn: sqlite3.Connection, name_or_id: Any) -> Dict[str, List[str]]:
    """``{kind: [values]}`` for an entity (human values first); empty when it has none or the table is absent."""
    entity_id = _entity_id(conn, name_or_id) if not isinstance(name_or_id, int) else name_or_id
    if entity_id is None:
        return {}
    try:
        rows = conn.execute("SELECT kind, value FROM EntityProfiles WHERE entity_id = ? "
                            "ORDER BY kind, source = 'llm', value", (entity_id,)).fetchall()
    except sqlite3.OperationalError:
        return {}
    profile: Dict[str, List[str]] = {}
    for kind, value in rows:
        profile.setdefault(kind, []).append(value)
    return profile


def profile_text(conn: sqlite3.Connection, name_or_id: Any) -> str:
    """The profile as one stable line (empty when there is none), for a prompt and for fingerprints."""
    profile = profile_for(conn, name_or_id)
    return "; ".join(f"{kind}: {', '.join(profile[kind])}" for kind in KINDS if kind in profile)


def candidate_profiles(conn: sqlite3.Connection, names: Sequence[str]) -> Dict[str, Dict[str, List[str]]]:
    """``{name: profile}`` for the names that have one: what a choice among candidates is shown."""
    return {n: p for n in names if (p := profile_for(conn, n))}


def domain_of(profile: Dict[str, List[str]]) -> Optional[str]:
    """The registrable-looking host of the profile's website, lowercase without ``www.``."""
    site = (profile.get("website") or [""])[0].lower()
    match = re.search(r"(?:https?://)?(?:www\.)?([a-z0-9.-]+\.[a-z]{2,})", site)
    return match.group(1) if match else None


def agreement(left: Dict[str, List[str]], right: Dict[str, List[str]]) -> int:
    """How far two profiles agree: +2 same website host, +1 same jurisdiction, -1 different jurisdiction.

    Reliable identity evidence for ranking which pairs to ask about first; not a decision.
    """
    score = 0
    a, b = domain_of(left), domain_of(right)
    if a and b and a == b:
        score += 2
    ja, jb = (left.get("jurisdiction") or [None])[0], (right.get("jurisdiction") or [None])[0]
    if ja and jb:
        score += 1 if ja.lower() == jb.lower() else -1
    return score


# ---------------------------------------------------------------- research

def evidence_hash(conn: sqlite3.Connection, entity_id: int) -> str:
    """A hash of the incidents the organisation is linked to (their titles): the evidence a lookup is told about."""
    titles = [r[0] for r in conn.execute(
        """SELECT DISTINCT e.title FROM EnrichedEventEntities ee JOIN EnrichedEvents e
           ON e.enriched_event_id = ee.enriched_event_id WHERE ee.entity_id = ? AND e.title IS NOT NULL
           ORDER BY e.title LIMIT 3""", (entity_id,))]
    return hashlib.sha256(json.dumps([titles, PROMPT_VERSION, perplexity_model()]).encode("utf-8")).hexdigest()[:16]


def pending(conn: sqlite3.Connection, limit: Optional[int] = None) -> List[Tuple[int, str]]:
    """Organisations to look up: victims (any role) of active events with no research on the current evidence."""
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT DISTINCT v.entity_id, v.entity_name FROM DeduplicatedEventEntities dee
           JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
           JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id AND d.status = 'Active'
           WHERE dee.relationship_type = 'victim'
             AND COALESCE(v.entity_kind, 'organisation') IN ('organisation', 'government_body')
           ORDER BY v.entity_id""").fetchall()
    done = {r[0]: r[1] for r in conn.execute(
        "SELECT entity_id, evidence_hash FROM EntityProfileResearch WHERE status = 'researched'")}
    todo = [(i, n) for i, n in rows if done.get(i) != evidence_hash(conn, i)]
    return todo[:limit] if limit else todo


def perplexity_profile_search(prompt: str) -> Optional[Dict[str, Any]]:
    import requests

    key = os.getenv("PERPLEXITY_API_KEY")
    if not key:
        raise RuntimeError("PERPLEXITY_API_KEY is not set; entity profiles need the lookup and nothing else is substituted")
    try:
        response = requests.post(
            "https://api.perplexity.ai/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": perplexity_model(), "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 600, "temperature": 0.0}, timeout=90)
        if response.status_code != 200:
            logger.warning("Perplexity returned %s for a profile lookup", response.status_code)
            return None
        payload = response.json()
        return {"text": payload["choices"][0]["message"]["content"], "citations": payload.get("citations") or []}
    except Exception as exc:  # noqa: BLE001 - one entity must not stop the run; it stays pending
        logger.warning("Perplexity profile lookup failed: %s", exc)
        return None


def research_profile(conn: sqlite3.Connection, entity_id: int, name: str, search: Optional[Search] = None) -> Optional[int]:
    """One lookup for one organisation; stores its attributes. None when the lookup failed (retried next run)."""
    search = search or perplexity_profile_search
    titles = [r[0] for r in conn.execute(
        """SELECT DISTINCT e.title FROM EnrichedEventEntities ee JOIN EnrichedEvents e
           ON e.enriched_event_id = ee.enriched_event_id WHERE ee.entity_id = ? AND e.title IS NOT NULL
           ORDER BY e.title LIMIT 3""", (entity_id,))]
    context = f', for example in "{titles[0][:160]}"' if titles else ""
    answer = search(RESEARCH_PROMPT.format(name=name, context=context))
    match = re.search(r"\{.*\}", (answer or {}).get("text") or "", re.S)
    try:
        attributes = json.loads(match.group(0)) if match else None
    except ValueError:
        attributes = None
    ensure_schema(conn)
    if not isinstance(attributes, dict):
        conn.execute("INSERT OR REPLACE INTO EntityProfileResearch VALUES (?, ?, ?, ?, 'unavailable', NULL, ?)",
                     (entity_id, evidence_hash(conn, entity_id), PROMPT_VERSION, perplexity_model(),
                      datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        return None
    stored = store_researched(conn, entity_id, attributes, answer.get("citations") or [])
    conn.execute("INSERT OR REPLACE INTO EntityProfileResearch VALUES (?, ?, ?, ?, 'researched', ?, ?)",
                 (entity_id, evidence_hash(conn, entity_id), PROMPT_VERSION, perplexity_model(),
                  json.dumps(answer.get("citations") or []), datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return stored
