"""Reconcile each organisation's events against its researched incident timeline.

Shown side by side (``dashboard/timelines.html``), an organisation's events
and its researched timeline disagree, and each disagreement means at least
one of them is wrong. University of Western Australia, 2026-10-27: the
timeline listed 2 incidents and the database held 6 events - the timeline
missed the 2020 ProctorU and 2022 Callista breaches, while the database held
UN Women Australia's attack under UWA and dated the June 2026 Callista breach
February 2026.

For every organisation with a timeline:

1. **Match.** Jev chooses, for each event, which timeline incident it is, or
   ``none`` (a Choice whose options are the incidents themselves).
2. **Unmatched events** are classified (a second Choice, with the event's
   evidence, any corroboration search and the timeline): ``timeline_gap`` - a
   real incident at this organisation the research missed; ``wrong_organisation``
   - an incident at another organisation filed here; ``not_an_incident``.
3. **Unmatched incidents** are coverage gaps: discovery never found them.
   With ``queue_gaps`` a targeted search for proper reporting is run and, if
   found, queued as a raw record for normal ingestion (adds evidence only).
4. **Date conflicts**: a matched event whose date disagrees with the
   incident's by more than the incident date's own precision allows.

Every answer is stored in ``ModelJudgements``. Nothing is re-attributed,
rejected or re-dated here: those need labelled outcomes and calibrated
cut-offs first, as every other filter did. Findings go to
``instance/timeline_reconciliation.json`` and onto the timeline dashboard.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MATCH_QUESTION_ID = "timeline_match"
MATCH_VERSION = "timeline-match-v1"
CLASS_QUESTION_ID = "unmatched_event_kind"
CLASS_VERSION = "unmatched-event-v1"
LOW_CONFIDENCE = 0.6
INCIDENT_CHARS = 400

GAPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS ReconciliationGaps (
    entity_id      INTEGER NOT NULL,
    incident_key   TEXT NOT NULL,     -- sha-free key: incident date + first words
    incident_text  TEXT NOT NULL,
    query          TEXT,
    model          TEXT,
    answer         TEXT,
    citations_json TEXT,
    raw_event_id   TEXT,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (entity_id, incident_key)
);
"""

MATCH_INSTRUCTIONS = (
    "`event` is a stored event recorded against `organisation`; `incidents` is the organisation's "
    "researched history of publicly reported cyber incidents. Which incident is this event? Stored "
    "dates are often publication dates or month/year placeholders (see each record's `date`), so "
    "match on what happened, the attacker, the systems and data affected - not on the date alone. "
    "Choose `none` when the event is a different incident from all of them, is about a different "
    "organisation, or is not an incident."
)
CLASS_INSTRUCTIONS = (
    "`event` is recorded against `organisation` but matches no incident in the organisation's "
    "researched timeline (`incidents`). Decide which of these it is, using the event's records, any "
    "`corroboration` search, and the timeline."
)
CLASS_CRITERIA = {
    "timeline_gap": ("A real, specific incident that did happen to this organisation (or exposed its "
                     "data through a supplier) and that the researched timeline simply missed."),
    "wrong_organisation": ("A real incident at a DIFFERENT organisation filed under this one - a "
                           "similar name, an acronym clash, or an article that merely mentions it."),
    "not_an_incident": ("No specific real incident: a generic page, commentary, a campaign or advisory "
                        "naming no affected organisation, or a non-cyber event."),
}

_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july",
                                         "august", "september", "october", "november", "december"], 1)}
_DAY_MONTH_YEAR = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")


def incident_date(text: str) -> Tuple[Optional[date], str]:
    """The first date in an incident's date field, and its precision (day / month / year / none)."""
    text = text or ""
    m = _DAY_MONTH_YEAR.search(text)
    if m and m.group(2).lower() in _MONTHS:
        return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1))), "day"
    # A range ("January–February 2025") starts at its first month, which may
    # not be written next to the year.
    months = [w.lower() for w in re.findall(r"[A-Za-z]+", text) if w.lower() in _MONTHS]
    m = _YEAR.search(text)
    if months and m:
        return date(int(m.group(0)), _MONTHS[months[0]], 1), "month"
    if m:
        return date(int(m.group(0)), 1, 1), "year"
    return None, "none"


TOLERANCE_DAYS = {"day": 45, "month": 75, "year": 400}


def _match_question(incidents: List[Dict[str, str]]):
    from typesafe_sdk import Choice
    criteria = {f"incident_{i['number']}": f"{i['date']}: {i['what'][:INCIDENT_CHARS]}" for i in incidents}
    criteria["none"] = "The event is none of these incidents."
    return Choice(instructions=MATCH_INSTRUCTIONS, criteria=criteria)


def _class_question():
    from typesafe_sdk import Choice
    return Choice(instructions=CLASS_INSTRUCTIONS, criteria=CLASS_CRITERIA)


def _incident_key(incident: Dict[str, str]) -> str:
    return (incident["date"] + " " + " ".join(incident["what"].split()[:8]))[:200]


def queue_gap(conn: sqlite3.Connection, entity_id: int, org: str, incident: Dict[str, str],
              search: Optional[Callable[[str, str], Dict[str, Any]]] = None) -> Optional[str]:
    """Search once for proper reporting of an incident no event covers; queue it if found."""
    import os

    from cyber_data_collector.dedup.corroboration import _perplexity_search
    from cyber_data_collector.dedup.source_gate import REPLACEMENT_PROMPT
    from cyber_data_collector.utils.llm_models import perplexity_model

    conn.executescript(GAPS_SCHEMA)
    key = _incident_key(incident)
    if conn.execute("SELECT 1 FROM ReconciliationGaps WHERE entity_id = ? AND incident_key = ?",
                    (entity_id, key)).fetchone():
        return None
    if search is None:
        if not os.getenv("PERPLEXITY_API_KEY"):
            return None
        search = _perplexity_search
    model = perplexity_model()
    query = REPLACEMENT_PROMPT.format(org=org, date=incident["date"] or "unknown", summary=incident["what"][:600])
    try:
        result = search(query, model)
    except Exception as exc:  # noqa: BLE001 - retried next run
        logger.warning("Gap search failed for %s / %s: %s", org, key, exc)
        return None
    answer = result.get("answer") or ""
    citations = [c for c in (result.get("citations") or []) if c] or ([incident["source"]] if incident["source"] else [])
    raw_id = None
    when, _ = incident_date(incident["date"])
    if answer.strip().upper().startswith("FOUND: YES") and citations:
        raw_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO RawEvents (raw_event_id, source_type, source_event_id, raw_title, raw_description, "
            "raw_content, event_date, source_url, source_metadata, discovered_at, is_processed) "
            "VALUES (?, 'Perplexity', NULL, ?, ?, NULL, ?, ?, ?, ?, 0)",
            (raw_id, f"{org} cyber incident", answer, when.isoformat() if when else None, citations[0],
             json.dumps({"origin": "timeline reconciliation coverage gap", "organisation": org,
                         "incident": incident, "citations": citations[:8]}), datetime.now().isoformat()))
    conn.execute("INSERT INTO ReconciliationGaps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (entity_id, key, json.dumps(incident), query, model, answer, json.dumps(citations), raw_id,
                  datetime.now().isoformat()))
    conn.commit()
    return raw_id


def reconcile_organisation(conn: sqlite3.Connection, entity_id: int, org: str, judge,
                           queue_gaps: bool = False, search=None) -> Dict[str, Any]:
    from cyber_data_collector.dedup.corroboration import stored as stored_corroboration
    from cyber_data_collector.dedup.duplicate_judgement import _event_with_dates
    from cyber_data_collector.dedup.org_timeline import parse_timeline, stored

    timeline = stored(conn, entity_id)
    incidents = parse_timeline(timeline["timeline"]) if timeline else []
    events = conn.execute(
        """SELECT d.deduplicated_event_id, d.title, d.event_date FROM DeduplicatedEventEntities dee
           JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id
           WHERE dee.entity_id = ? AND dee.relationship_type = 'victim' AND d.status = 'Active'
           ORDER BY d.event_date""", (entity_id,)).fetchall()
    result: Dict[str, Any] = {"organisation": org, "entity_id": entity_id, "incidents": incidents,
                              "events": [], "gaps": [], "date_conflicts": []}
    if not incidents:
        result["note"] = "No parseable timeline; nothing to reconcile against."
        return result
    timeline_text = "\n".join(f"{i['number']}. {i['date']} | {i['what'][:INCIDENT_CHARS]}" for i in incidents)
    matched_numbers = set()
    for did, title, event_date in events:
        evidence = _event_with_dates(conn, did)
        state = {"organisation": org, "event": evidence, "incidents": timeline_text}
        match = judge.ask("deduplicated_event", f"{did}|{entity_id}", MATCH_VERSION, state,
                          {MATCH_QUESTION_ID: _match_question(incidents)})[MATCH_QUESTION_ID]
        entry = {"deduplicated_event_id": did, "title": title, "event_date": event_date,
                 "match": match.answer, "match_probability": match.value,
                 "low_confidence": (match.value or 0) < LOW_CONFIDENCE}
        if match.answer and match.answer != "none":
            number = match.answer.split("_", 1)[1]
            matched_numbers.add(number)
            incident = next(i for i in incidents if i["number"] == number)
            when, precision = incident_date(incident["date"])
            try:
                stored_day = datetime.strptime(str(event_date)[:10], "%Y-%m-%d").date()
            except (TypeError, ValueError):
                stored_day = None
            if when and stored_day and precision in TOLERANCE_DAYS and \
                    abs((stored_day - when).days) > TOLERANCE_DAYS[precision]:
                result["date_conflicts"].append({"deduplicated_event_id": did, "title": title,
                                                 "stored_date": event_date, "incident_date": incident["date"],
                                                 "incident": number})
        else:
            found = stored_corroboration(conn, did)
            cstate = dict(state)
            if found:
                cstate["corroboration"] = {"answer": found["answer"], "citations": found["citations"]}
            kind = judge.ask("deduplicated_event", f"{did}|{entity_id}", CLASS_VERSION, cstate,
                             {CLASS_QUESTION_ID: _class_question()})[CLASS_QUESTION_ID]
            entry.update({"unmatched_kind": kind.answer, "kind_probabilities": kind.probabilities})
        result["events"].append(entry)
    for incident in incidents:
        if incident["number"] in matched_numbers:
            continue
        gap = {"incident": incident}
        if queue_gaps:
            gap["queued_raw_event_id"] = queue_gap(conn, entity_id, org, incident, search=search)
        result["gaps"].append(gap)
    return result


def reconcile_all(conn: sqlite3.Connection, judge=None, queue_gaps: bool = False, search=None) -> Dict[str, Any]:
    from cyber_data_collector.dedup.org_timeline import organisations_with_several_events, research_all
    from cyber_data_collector.utils.jev import JevJudge

    research_all(conn)
    owned = judge is None
    judge = judge or JevJudge(conn)
    organisations = []
    try:
        for org in organisations_with_several_events(conn):
            try:
                organisations.append(reconcile_organisation(conn, org["entity_id"], org["entity_name"], judge,
                                                            queue_gaps=queue_gaps, search=search))
            except Exception as exc:  # noqa: BLE001 - one organisation never stops the rest
                logger.warning("Reconciliation failed for %s: %s", org["entity_name"], exc)
    finally:
        if owned:
            judge.close()
    events = [e for o in organisations for e in o["events"]]
    return {
        "organisations": organisations,
        "summary": {
            "organisations": len(organisations),
            "events": len(events),
            "matched": sum(1 for e in events if e["match"] not in (None, "none")),
            "unmatched": {k: sum(1 for e in events if e.get("unmatched_kind") == k) for k in CLASS_CRITERIA},
            "low_confidence_matches": sum(1 for e in events if e["low_confidence"]),
            "coverage_gaps": sum(len(o["gaps"]) for o in organisations),
            "date_conflicts": sum(len(o["date_conflicts"]) for o in organisations),
        },
    }
