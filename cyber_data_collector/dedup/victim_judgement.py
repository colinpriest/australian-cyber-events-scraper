"""Is the recorded victim really the organisation the incident happened to? Jev, report-only.

Calibration labelling (2026-10-27) found events filed against the wrong
organisation that the deterministic ``check-victims`` rule cannot see,
because the wrong name is all over the records: a NSW driver-licence exposure
under Optus, Aussie Fluid Power under the AFP, UN Women Australia under the
University of Western Australia, Ambulance Tasmania under Ambulance Victoria,
TAFE SA under Melbourne TAFE. Those organisations each have several events,
so each has a researched incident timeline (:mod:`org_timeline`) - which is
exactly what shows an event is not one of theirs.

For every active event whose recorded victim has a timeline, Jev is asked
whether that organisation is the one the incident happened to, seeing the
event's evidence and the organisation's timeline. Answers are stored in
``ModelJudgements``. There are no labelled victim attributions yet, so there
is no calibrated cut-off: low-probability events are **reported**
(``instance/victim_attribution_findings.json``), never changed.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

QUESTION_ID = "is_recorded_victim"
QUESTION_VERSION = "victim-attribution-v1"
REPORT_BELOW = 0.5

INSTRUCTIONS = (
    "`recorded_victim` is the organisation this stored event is filed against. Decide whether the "
    "incident the event describes actually happened to that organisation (its systems, its data, or "
    "its customers' data held by it or by its supplier). Use the records' page text and Perplexity "
    "research, any `corroboration` search, and `organisation_timeline` - researched history of "
    "the recorded victim's own incidents. An event about a different organisation with a similar "
    "name, an acronym clash (AFP / Aussie Fluid Power), a sector-wide story, or an incident at an "
    "unrelated organisation that merely mentions the recorded victim, is NOT its incident."
)
CRITERIA = {
    "true": "The incident happened to the recorded victim, or exposed its data through a supplier.",
    "false": "The incident happened to some other organisation, or is not about the recorded victim at all.",
}


def question():
    from typesafe_sdk import Noul, NoulCriteria
    return Noul(instructions=INSTRUCTIONS, criteria=NoulCriteria(**CRITERIA))


def events_to_check(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Active events whose victim has a stored timeline."""
    from cyber_data_collector.dedup.org_timeline import ensure_schema
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT DISTINCT d.deduplicated_event_id, v.entity_id, v.entity_name, d.title
           FROM DeduplicatedEventEntities dee
           JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
           JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id
           WHERE dee.relationship_type = 'victim' AND d.status = 'Active'
             AND EXISTS (SELECT 1 FROM OrganisationTimelines t WHERE t.entity_id = v.entity_id)
           ORDER BY v.entity_name, d.deduplicated_event_id""").fetchall()
    return [{"deduplicated_event_id": r[0], "entity_id": r[1], "victim": r[2], "title": r[3]} for r in rows]


def check_victim_attribution(conn: sqlite3.Connection, judge=None) -> Dict[str, Any]:
    from cyber_data_collector.dedup.event_reality import build_state
    from cyber_data_collector.dedup.org_timeline import stored
    from cyber_data_collector.utils.jev import JevJudge

    owned = judge is None
    judge = judge or JevJudge(conn)
    q = {QUESTION_ID: question()}
    findings = []
    judged = 0
    try:
        for e in events_to_check(conn):
            timeline = stored(conn, e["entity_id"])
            state = build_state(conn, e["deduplicated_event_id"], corroborated=True)
            state["recorded_victim"] = e["victim"]
            state["organisation_timeline"] = {"incidents": timeline["timeline"], "citations": timeline["citations"]}
            sid = f"{e['deduplicated_event_id']}|{e['entity_id']}"
            try:
                p = judge.ask("event_victim", sid, QUESTION_VERSION, state, q)[QUESTION_ID].value
            except Exception as exc:  # noqa: BLE001
                logger.warning("Victim judgement failed for %s: %s", sid, exc)
                continue
            judged += 1
            if p < REPORT_BELOW:
                findings.append({**e, "p_recorded_victim": p})
    finally:
        if owned:
            judge.close()
    findings.sort(key=lambda f: f["p_recorded_victim"])
    return {"judged": judged, "reported": findings, "calibrated": False}
