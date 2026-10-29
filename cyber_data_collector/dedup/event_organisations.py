"""Organisations an event's own text names, and the one that did the attacking.

Victim attribution and victim filling choose the victim among organisations the
event's evidence names. That list came from two places only - each record's
Perplexity ``formal_entity_name`` and the organisations *linked* to its records
by entity extraction - so an organisation that appears in the text and nowhere
else could never be chosen. "OpenAI reveals another hack into a government
agency in Australia" names "the NSW National Parks and Wildlife Service's Fire
History service" in its body; the record had no research, its links were OpenAI
and the vague phrase, and the true victim was never a candidate.

**The extraction.** The fast OpenAI model reads the event's title, its records'
descriptions, their stored pages (those that belong to their record,
:mod:`processing.active_evidence`) and any research, and lists

* ``possible_victims`` - organisations named in it that could be the one whose
  systems or data were affected; and
* ``actors`` - organisations that carried out the attack, or whose own tool,
  agent or system did (OpenAI, for its agent's access to a government website).

**Nothing the model returns is trusted as written.** A name is kept only when
it occurs verbatim (case-insensitive) in the text the model was shown and
passes the same usability test as every victim (:func:`victim_judgement._usable`:
not a descriptive phrase, product, threat actor, collective...). The model can
therefore only *select* from the text; it cannot add an organisation.

**Stored, not re-asked.** Each answer is written to ``ModelJudgements``
(``primitive='extraction'``) keyed by a hash of the text shown, the
instructions and the model, and reused on every re-run. A failed extraction
raises :class:`ExtractionFailed` - it is never turned into an empty list, which
would read as "the text names nobody".
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

from cyber_data_collector.utils.jev import ensure_schema
from cyber_data_collector.utils.llm_models import chat_params, openai_fast_model

logger = logging.getLogger(__name__)

QUESTION_ID = "event_named_organisations"
#: Bump when the instructions or the text shown change; part of the fingerprints that use it.
EXTRACTION_VERSION = "event-organisations-v1"
MAX_RECORDS = 10
PAGE_CHARS = 12000
TOTAL_CHARS = 40000
EXCERPT_CHARS = 160

INSTRUCTIONS = (
    "You read the stored coverage of one Australian cyber incident and list the organisations it names. "
    "Return `possible_victims`: every organisation named in the text that could be the one whose systems, "
    "accounts or data were affected - including one named only in passing, one that runs the service, website "
    "or portal that was accessed, and a supplier whose breach exposed others. Return `actors`: organisations "
    "that carried out the attack, or whose own tool, agent, AI system or staff did (the developer of an AI "
    "agent that broke into a site is an actor). Copy each name EXACTLY as it is written in the text. Do not "
    "list people, news outlets that merely report, products or software, regulators that only responded, "
    "countries, or generic descriptions such as 'a government agency'. Leave a list empty when the text names "
    "nobody for it."
)


class NamedOrganisations(BaseModel):
    """The model's answer, before validation against the text."""

    possible_victims: List[str] = Field(default_factory=list, description="Organisations that could be the victim.")
    actors: List[str] = Field(default_factory=list, description="Organisations that did the attacking.")


class ExtractionFailed(RuntimeError):
    """The extraction for one event could not be made. Never treated as 'the text names nobody'."""


@dataclass
class EventOrganisations:
    """A validated extraction: names that are in the text, each with a short excerpt."""

    possible_victims: List[Dict[str, str]] = field(default_factory=list)   # {"name", "excerpt"}
    actors: List[str] = field(default_factory=list)
    cached: bool = False

    @property
    def victim_names(self) -> List[str]:
        return [v["name"] for v in self.possible_victims]


# ---------------------------------------------------------------- the text shown

def extraction_text(conn: sqlite3.Connection, dedup_id: str) -> str:
    """Everything the model is shown for one event, as one deterministic text (so its hash is stable)."""
    from cyber_data_collector.processing.active_evidence import evidence_page, member_condition, research_names

    event = conn.execute("SELECT COALESCE(title, ''), victim_organization_name FROM DeduplicatedEvents "
                         "WHERE deduplicated_event_id = ?", (dedup_id,)).fetchone() or ("", None)
    rows = conn.execute(
        f"""SELECT e.enriched_event_id, COALESCE(e.title, ''), COALESCE(e.description, e.summary, ''),
                   COALESCE(NULLIF(r.raw_content, ''), r.raw_description, ''), e.perplexity_enrichment_data
            FROM EventDeduplicationMap m
            JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
            LEFT JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
            WHERE m.deduplicated_event_id = ? {member_condition(conn)}
            ORDER BY e.enriched_event_id""", (dedup_id,)).fetchall()
    parts = [f"EVENT TITLE: {event[0]}"]
    for _eid, title, description, page, research in rows[:MAX_RECORDS]:
        shown, _withheld = evidence_page(title, [*research_names(research), event[1]], page)
        piece = [f"RECORD TITLE: {title}"]
        if description:
            piece.append(f"DESCRIPTION: {description}")
        try:
            data = json.loads(research or "{}")
        except (TypeError, ValueError):
            data = {}
        if isinstance(data, dict):
            if data.get("formal_entity_name"):
                piece.append(f"RESEARCH ORGANISATION: {data['formal_entity_name']}")
            if data.get("threat_actor"):
                piece.append(f"RESEARCH THREAT ACTOR: {data['threat_actor']}")
        if shown:
            piece.append(f"PAGE: {shown[:PAGE_CHARS]}")
        parts.append("\n".join(piece))
    return "\n\n".join(parts)[:TOTAL_CHARS]


def _hash(text: str, model: str) -> str:
    blob = json.dumps({"text": text, "instructions": INSTRUCTIONS, "model": model, "version": EXTRACTION_VERSION},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- the model

def default_extractor() -> Callable[[str], NamedOrganisations]:
    """The fast model through instructor. Raises :class:`ExtractionFailed` without a key; no substitute."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ExtractionFailed("OPENAI_API_KEY is not set; organisation extraction needs the fast model "
                               "and no other model will be substituted")
    try:
        import instructor
        from openai import OpenAI
    except ImportError as exc:   # pragma: no cover - dependency missing
        raise ExtractionFailed(f"instructor/openai unavailable: {exc}") from exc
    client = instructor.from_openai(OpenAI(api_key=api_key))
    model = openai_fast_model()

    def extract(text: str) -> NamedOrganisations:
        return client.chat.completions.create(
            **chat_params(model), response_model=NamedOrganisations,
            messages=[{"role": "system", "content": INSTRUCTIONS},
                      {"role": "user", "content": text}])

    return extract


# ---------------------------------------------------------------- validation

def _normal(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("’", "'").replace("�", "'")).lower()


def _occurs(haystack: str, needle: str) -> Optional[re.Match]:
    """``needle`` as whole words in ``haystack`` (both normalised): 'AP' is not found inside 'capital'."""
    if not needle:
        return None
    return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", haystack)


def _excerpt(text: str, name: str) -> str:
    """The normalised text around the name's first occurrence."""
    haystack = _normal(text)
    found = _occurs(haystack, _normal(name))
    if not found:
        return ""
    return haystack[max(0, found.start() - EXCERPT_CHARS):min(len(haystack), found.end() + EXCERPT_CHARS)].strip()


def validated(text: str, answer: NamedOrganisations, usable: Optional[Callable[[str], bool]] = None) -> EventOrganisations:
    """Keep only names that occur verbatim in ``text`` and pass ``usable`` (the victim usability test)."""
    if usable is None:
        from cyber_data_collector.dedup.victim_judgement import _usable as usable   # noqa: N811
    haystack = _normal(text)
    seen: set = set()
    victims: List[Dict[str, str]] = []
    for raw in answer.possible_victims:
        name = (raw or "").strip().strip('"').strip()
        key = name.lower()
        if not name or key in seen or not _occurs(haystack, _normal(name)) or not usable(name):
            continue
        seen.add(key)
        victims.append({"name": name, "excerpt": _excerpt(text, name)})
    actors: List[str] = []
    for raw in answer.actors:
        name = (raw or "").strip().strip('"').strip()
        if name and _occurs(haystack, _normal(name)) and name.lower() not in {a.lower() for a in actors}:
            actors.append(name)
    return EventOrganisations(victims, actors)


# ---------------------------------------------------------------- storage

def named_organisations(conn: sqlite3.Connection, dedup_id: str,
                        extractor: Optional[Callable[[str], NamedOrganisations]] = None,
                        stored_only: bool = False) -> Optional[EventOrganisations]:
    """The validated extraction for an event; stored answers are reused.

    With ``stored_only`` nothing is asked: returns None when no answer is stored. Otherwise raises
    :class:`ExtractionFailed` when the extraction cannot be made.
    """
    ensure_schema(conn)
    text = extraction_text(conn, dedup_id)
    model = openai_fast_model()
    digest = _hash(text, model)
    row = conn.execute("SELECT answer FROM ModelJudgements WHERE question_id = ? AND input_hash = ? "
                       "ORDER BY created_at DESC, rowid DESC LIMIT 1", (QUESTION_ID, digest)).fetchone()
    if row is not None:
        found = validated(text, NamedOrganisations(**json.loads(row[0])))
        found.cached = True
        return found
    if stored_only:
        return None
    try:
        extractor = extractor or default_extractor()
        answer = extractor(text)
        if not isinstance(answer, NamedOrganisations):
            answer = NamedOrganisations(**dict(answer))
    except ExtractionFailed:
        raise
    except Exception as exc:  # noqa: BLE001 - reported to the caller, never an empty list
        raise ExtractionFailed(f"organisation extraction failed for {dedup_id}: {exc}") from exc
    conn.execute(
        "INSERT INTO ModelJudgements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), "deduplicated_event", dedup_id, QUESTION_ID, EXTRACTION_VERSION, "extraction",
         json.dumps(answer.model_dump(), ensure_ascii=False), None, None, "{}",
         json.dumps({"instructions": INSTRUCTIONS}), digest, model, model, None, None,
         datetime.now().isoformat()))
    conn.commit()
    return validated(text, answer)


def estimated_text_names(conn: sqlite3.Connection, dedup_id: str, known_names: Sequence[str]) -> List[str]:
    """A free estimate of what the extraction would add: known entity names that occur in the event's text.

    For dry runs, which must not call a model. Only names the database already knows are found, so it
    under-counts (the NSW National Parks and Wildlife Service is not an entity until something names it).
    """
    haystack = _normal(extraction_text(conn, dedup_id))
    return [n for n in known_names if len(n) > 3 and _occurs(haystack, _normal(n))]
