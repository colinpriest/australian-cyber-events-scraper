"""Find and remove stray records: members of an event that describe a different incident.

The Sharp Motor Group event (2026-10-02) held 18 records of which only 5 were
about Sharp Motor Group: 9 were unrelated Cyber Daily articles and 4 were about
a different company, Sharp Office Systems. Its date came from a stray Partnered
Health record, and strays like it pull every later duplicate check toward
wrong matches. A scan found the same defect in other events (Partnered Health
holding Ochre Medical and NT Health records, Toll holding Manheim ...).

Three stages, cheapest first:

1. **Suspects (free).** A member is suspect when the organisation Perplexity
   identified for it does not resolve to the event's victim, vendor or the
   organisation most of its members name.
2. **Judgement (one call per suspect event).** The model sees the event's
   incident and every member, and says which members do not belong. The
   project rule that one supplier's breach reported about several customers is
   ONE event is stated, so Canvas/Instructure, Frontier payroll and Pareto Phone
   style events are not torn apart.
3. **Repair.** Members judged foreign at or above the certainty bar are split
   out with the ledger (snapshotted, reversible). A split-off record is folded
   into an existing event only when exactly one active event for the same
   organisation lies within ``HOME_WINDOW_DAYS``; otherwise it stays its own.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections import Counter
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

from cyber_data_collector.dedup.entity_resolution import EntityResolver

logger = logging.getLogger(__name__)

COHERENCE_MODEL = "gpt-4o"
HOME_WINDOW_DAYS = 90
_EMPTY = {"", "none", "null", "unknown", "n/a"}


class MemberJudgement(BaseModel):
    index: int = Field(description="1-based index of the record in the list shown.")
    belongs: bool = Field(description=(
        "True if this record reports the SAME real-world incident as the event. "
        "Follow-up coverage, regulator action and class actions about it belong. A "
        "breach of one supplier reported about one of its customers belongs to that "
        "supplier's incident. A different organisation's own breach, a different "
        "incident at the same organisation, or a roundup item about something else "
        "does NOT belong."))
    certainty: float = Field(ge=0.0, le=1.0)
    actual_incident: str = Field(description="If it does not belong: short name of the incident it is actually about; else ''.")
    reasoning: str = Field(description="One sentence naming the decisive facts.")


class CoherenceVerdict(BaseModel):
    event_incident: str = Field(description="Short name of the incident this event is about.")
    members: List[MemberJudgement]


def _formal_name(pe: Optional[str]) -> Optional[str]:
    try:
        name = json.loads(pe or "{}").get("formal_entity_name")
    except (TypeError, ValueError, AttributeError):
        return None
    if name is None or str(name).strip().lower() in _EMPTY:
        return None
    return str(name)


def load_event_members(conn: sqlite3.Connection, dedup_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT e.enriched_event_id, e.title, e.event_date, COALESCE(e.summary, e.description, '') AS body,
               e.perplexity_enrichment_data AS pe, r.source_url
        FROM EventDeduplicationMap m
        JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
        LEFT JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
        WHERE m.deduplicated_event_id = ?
        ORDER BY e.event_date
        """, (dedup_id,)).fetchall()
    return [{"id": r[0], "title": r[1] or "", "date": r[2], "body": r[3] or "",
             "org": _formal_name(r[4]), "url": r[5]} for r in rows]


def find_suspect_events(conn: sqlite3.Connection, resolver: Optional[EntityResolver] = None,
                        only: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """Active multi-member events with at least one member naming another organisation."""
    resolver = resolver or EntityResolver(conn)
    events = conn.execute(
        """SELECT deduplicated_event_id, title, event_date, victim_organization_name, vendor_organization_name
           FROM DeduplicatedEvents d
           WHERE status = 'Active'
             AND (SELECT COUNT(*) FROM EventDeduplicationMap m
                  WHERE m.deduplicated_event_id = d.deduplicated_event_id) >= 2""").fetchall()
    if only:
        events = [e for e in events if any(e[0].startswith(p) for p in only)]
    members_by_event = {e[0]: load_event_members(conn, e[0]) for e in events}
    resolver.fit([m["org"] for ms in members_by_event.values() for m in ms if m["org"]]
                 + [e[3] for e in events if e[3]] + [e[4] for e in events if e[4]])

    suspects = []
    for did, title, event_date, victim, vendor in events:
        members = members_by_event[did]
        orgs = [m["org"] for m in members if m["org"]]
        if not orgs:
            continue
        majority = Counter(orgs).most_common(1)[0][0]
        refs = [n for n in (victim, vendor, majority) if n]
        # Distinctive blocking keys, not are_candidates: its name-similarity
        # fallback treats "University of Tasmania" and "University of Sydney"
        # as one organisation, hiding real strays, while a shared key keeps
        # true variants together ("Singtel Optus"/"Optus", "youX"/"Vroom by YouX").
        ref_keys = set().union(*(resolver.blocks_for(r) for r in refs))
        odd = [m for m in members if m["org"] and not (resolver.blocks_for(m["org"]) & ref_keys)]
        if odd:
            suspects.append({"id": did, "title": title, "date": event_date, "victim": victim,
                             "vendor": vendor, "members": members, "suspect_ids": [m["id"] for m in odd]})
    return suspects


def _client():
    if not os.getenv("OPENAI_API_KEY"):
        return None
    try:
        import instructor
        from openai import OpenAI
        return instructor.from_openai(OpenAI(api_key=os.environ["OPENAI_API_KEY"]))
    except ImportError:
        return None


def render_event(event: Dict[str, Any]) -> str:
    lines = [f"EVENT: {event['title']}",
             f"Victim: {event.get('victim') or 'unknown'}   Vendor: {event.get('vendor') or 'none'}   Date: {event.get('date') or 'unknown'}",
             "", "RECORDS:"]
    for i, m in enumerate(event["members"], start=1):
        lines.append(f"[{i}] {m['title']}\n    organisation: {m['org'] or 'unknown'}   date: {m['date'] or 'unknown'}\n"
                     f"    source: {m['url'] or 'unknown'}\n    text: {m['body'][:260]}")
    return "\n".join(lines)


SYSTEM_PROMPT = (
    "You audit one event in a cyber-incident database. The event should contain only records "
    "about ONE real-world incident. Some records were merged in by mistake. For EVERY record, "
    "decide whether it belongs to the event's incident.\n"
    "Rules: follow-up coverage, statements, regulator action and class actions about the "
    "incident belong. A breach of ONE supplier's systems reported about its different customers "
    "is ONE incident - customer records belong to the supplier's incident. A parent company and "
    "its brand (Singtel/Optus, TEG/Ticketek, TPG/iiNet) are the same organisation. A record about "
    "a different organisation's own breach, a different incident at the same organisation, or a "
    "roundup item about something else does NOT belong. Organisation fields can be wrong - judge "
    "by what the record says happened. Use certainty >= 0.9 only when it is clear."
)


def judge_event(event: Dict[str, Any], client=None) -> Optional[CoherenceVerdict]:
    client = client or _client()
    if client is None:
        return None
    try:
        verdict: CoherenceVerdict = client.chat.completions.create(
            model=COHERENCE_MODEL, response_model=CoherenceVerdict, temperature=0.0,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": render_event(event)}])
    except Exception as exc:  # noqa: BLE001 - one event must not break a run
        logger.warning("Coherence judgement failed for %s: %s", event["id"], exc)
        return None
    indices = sorted(j.index for j in verdict.members)
    if indices != list(range(1, len(event["members"]) + 1)):
        logger.warning("Coherence verdict for %s does not cover every record; ignored", event["id"])
        return None
    return verdict


def strays(event: Dict[str, Any], verdict: CoherenceVerdict, min_certainty: float,
           resolver: Optional[EntityResolver] = None) -> List[Dict[str, Any]]:
    """Members to split out: suspects the model judged foreign at >= ``min_certainty``.

    Guards, each from a wrong split seen in the 2026-10-02 dry run:

    * Only *suspects* (members naming another organisation) can leave. The
      model also "split" same-organisation records - a misdated copy of the
      Partnered Health breach, a Latitude follow-up, OpenAI's later disclosure
      - which are the same incident.
    * When the departing members are half or more of the event and all name
      one incident, the event's *label* is what is wrong, not its contents:
      12 youX records were judged foreign to "Vroom by YouX" (youX's product).
      Nothing is split; it is left for review.
    * When the model's own name for where a member belongs shares a
      distinctive name with the event's title, it is describing the event
      itself ("youX Data Breach" leaving "Vroom by YouX Data Breach").
    * Never strip an event bare.
    """
    title_keys = resolver.blocks_for(event.get("title")) if resolver else set()
    suspects = set(event.get("suspect_ids") or [])
    out = []
    for j in verdict.members:
        member = event["members"][j.index - 1]
        if not j.belongs and j.certainty >= min_certainty and member["id"] in suspects:
            if title_keys and resolver.blocks_for(j.actual_incident) & title_keys:
                continue
            out.append({**member, "actual_incident": j.actual_incident, "reasoning": j.reasoning,
                        "certainty": j.certainty})
    total = len(event["members"])
    if len(out) >= total:
        return []
    if out and len(out) * 2 >= total and len({s["actual_incident"].strip().lower() for s in out}) == 1:
        logger.info("Coherence: %s looks mislabelled rather than contaminated; left for review", event["id"])
        return []
    return out


def find_home(conn: sqlite3.Connection, resolver: EntityResolver, org: Optional[str],
              record_date: Optional[str], exclude: Sequence[str]) -> Optional[str]:
    """The single active event for ``org`` within HOME_WINDOW_DAYS, else None."""
    if not org:
        return None
    try:
        when = date.fromisoformat(str(record_date)[:10]) if record_date else None
    except ValueError:
        when = None
    hits = []
    for did, victim, edate in conn.execute(
            "SELECT deduplicated_event_id, victim_organization_name, event_date FROM DeduplicatedEvents "
            "WHERE status = 'Active' AND victim_organization_name IS NOT NULL"):
        if did in exclude or not resolver.are_candidates(org, victim):
            continue
        if when and edate:
            try:
                if abs((date.fromisoformat(str(edate)[:10]) - when).days) > HOME_WINDOW_DAYS:
                    continue
            except ValueError:
                continue
        hits.append(did)
    return hits[0] if len(hits) == 1 else None
