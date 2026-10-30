"""Reconcile each organisation's events against its researched incident timeline, and resolve what disagrees.

Shown side by side (``dashboard/timelines.html``), an organisation's events
and its researched timeline disagree, and each disagreement means at least
one of them is wrong. University of Western Australia, 2026-10-27: the
timeline listed 2 incidents and the database held 6 events - the timeline
missed the 2020 ProctorU and 2022 Callista breaches, while the database held
UN Women Australia's attack under UWA and dated the June 2026 Callista breach
February 2026.

For every organisation with a timeline (researched or refreshed first, so a
stale history is never reconciled against - :mod:`org_timeline`):

1. **Match.** Jev chooses, for each event, which timeline incident it is, or
   ``none`` (a Choice whose options are the incidents themselves).
2. **Unmatched events** are classified (a second Choice, with the event's
   evidence, any corroboration search and the timeline): ``timeline_gap``,
   ``wrong_organisation`` or ``not_an_incident``.
3. **Unmatched incidents** are coverage gaps: discovery never found them.
4. **Date conflicts**: a matched event whose date disagrees with the
   incident's by more than the incident date's own precision allows.

**Resolution - no new reject or re-attribute path.** Each class is resolved
by a step that is already calibrated, or is left as a report:

* ``wrong_organisation`` -> the calibrated victim re-attribution
  (:mod:`victim_judgement`, ``check-victim-attribution``), which judges every
  event-victim pair; the finding shows that judgement's score and outcome.
* ``not_an_incident`` -> the calibrated event-reality rejection
  (:mod:`event_reality`, ``check-events``), which judges every active event;
  the finding shows its score and whether it falls below the cut-off.
* ``timeline_gap`` -> the timeline was refreshed before matching (a changed
  question or newer evidence re-researches it), so a gap that remains is a
  real incident the research still misses: the event is kept, report only.
* **date conflicts** -> re-dated to the incident's date only where the match
  is at or above a cut-off calibrated on labelled conflicts
  (``scripts/calibrate_timeline_redates.py``) and the incident's date is more
  precise than the stored one (:func:`redate_eligible`, using
  :mod:`date_provenance`); labelled conflicts are ground truth, and ``--apply``
  refuses without a readable labels file. Without a matching calibration,
  report only (logged).
* **coverage gaps** -> ``queue_gaps`` searches once for proper reporting and
  queues it as a raw record for normal ingestion (adds evidence only).

Every answer is stored in ``ModelJudgements``; every re-date is snapshotted by
the ledger and written to ``TimelineRedates``. Missing keys or failed calls
raise: nothing is silently skipped. Findings go to
``instance/timeline_reconciliation.json`` and onto the timeline dashboard.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import sqlite3
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from cyber_data_collector.utils.llm_models import typesafe_model

logger = logging.getLogger(__name__)

MATCH_QUESTION_ID = "timeline_match"
MATCH_VERSION = "timeline-match-v1"
CLASS_QUESTION_ID = "unmatched_event_kind"
CLASS_VERSION = "unmatched-event-v1"
LOW_CONFIDENCE = 0.6
INCIDENT_CHARS = 400

ROOT = Path(__file__).resolve().parents[2]
REDATE_LABELS_PATH = ROOT / "evaluation" / "timeline_redate_labels.csv"
REDATE_CALIBRATION_PATH = ROOT / "evaluation" / "timeline_redate_calibration.json"

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
REDATES_SCHEMA = """
CREATE TABLE IF NOT EXISTS TimelineRedates (
    redate_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id              TEXT NOT NULL,
    deduplicated_event_id TEXT NOT NULL,
    entity_id             INTEGER NOT NULL,
    old_date              TEXT,
    new_date              TEXT NOT NULL,
    incident_date_text    TEXT NOT NULL,
    incident_text         TEXT NOT NULL,
    match_probability     REAL NOT NULL,
    cutoff                REAL NOT NULL,
    calibration_json      TEXT,
    reason                TEXT,
    created_at            TEXT NOT NULL
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
_DAY_MONTH_YEAR = re.compile(r"(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})")
_DAY_MONTH_RANGE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s*[–—-]\s*\d{1,2}\s+[A-Za-z]+,?\s+(\d{4})")
_MONTH_DAY_YEAR = re.compile(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})")
_ISO_DAY = re.compile(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b")
_ISO_MONTH = re.compile(r"\b((?:19|20)\d{2})-(\d{2})\b")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
PRECISION_RANK = {"none": 0, "year": 1, "month": 2, "day": 3}


def incident_date(text: str) -> Tuple[Optional[date], str]:
    """The first date in an incident's date field, and its precision (day / month / year / none)."""
    text = text or ""
    # "2018–2019 (reported 25 May 2021)", "Late 2018; disclosed March 2020":
    # the incident date comes first; a reporting date after it is not it.
    head = re.split(r"[(;]", text, maxsplit=1)[0]
    if _YEAR.search(head):
        text = head
    found: List[Tuple[int, date, str]] = []
    # A range whose start carries no year ("27 February–11 August 2020")
    # starts on its first day, in the year written at its end.
    for m in _DAY_MONTH_RANGE.finditer(text):
        if m.group(2).lower() in _MONTHS:
            try:
                found.append((m.start(), date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1))), "day"))
            except ValueError:
                pass
    for m in _DAY_MONTH_YEAR.finditer(text):
        if m.group(2).lower() in _MONTHS:
            found.append((m.start(), date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1))), "day"))
    for m in _MONTH_DAY_YEAR.finditer(text):
        if m.group(1).lower() in _MONTHS:
            found.append((m.start(), date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2))), "day"))
    for m in _ISO_DAY.finditer(text):
        try:
            found.append((m.start(), date(int(m.group(1)), int(m.group(2)), int(m.group(3))), "day"))
        except ValueError:
            pass
    if found:
        _, when, precision = min(found, key=lambda f: f[0])
        return when, precision
    m = _ISO_MONTH.search(text)
    if m and 1 <= int(m.group(2)) <= 12:
        return date(int(m.group(1)), int(m.group(2)), 1), "month"
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
    """Search once for proper reporting of an incident no event covers; queue it if found.

    Raises when the search is needed and cannot be made (no key, or it fails).
    """
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
            raise RuntimeError("PERPLEXITY_API_KEY is not set: cannot search for coverage gaps (--queue-gaps)")
        search = _perplexity_search
    model = perplexity_model()
    query = REPLACEMENT_PROMPT.format(org=org, date=incident["date"] or "unknown", summary=incident["what"][:600])
    try:
        result = search(query, model)
    except Exception as exc:
        raise RuntimeError(f"Coverage-gap search failed for {org} / {key}: {exc}") from exc
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


# ---------------------------------------------------------------- dates

def stored_date_quality(conn: sqlite3.Connection, dedup_id: str, stored_date: Optional[str]) -> Dict[str, Any]:
    """How precise the stored event date is, and whether it is a lagging (publication / found) date."""
    from cyber_data_collector.dedup.date_provenance import event_dates

    prov = event_dates(conn, dedup_id, max_records=1000)
    day = str(stored_date or "")[:10]
    precision = prov["event_date_precision"]
    rank = {"day": 3, "unknown": 0}.get(precision, 2 if precision.startswith("month") else 1)
    bases = [r["basis"] for r in prov["records"] if r["date"] == day]
    lagging = bool(bases) and all(b.startswith(("publication date", "date the record was found")) for b in bases)
    return {"precision": precision, "rank": rank, "carrying_bases": sorted(set(bases)),
            "lagging": lagging, "carried_by_no_record": not bases}


def redate_eligible(incident_precision: str, stored: Dict[str, Any]) -> Tuple[bool, str]:
    """Is the incident's date more precise than the stored date? (The ``redate`` convention, applied to a timeline.)

    More precise means a finer precision (a day against a month placeholder, a
    month against a year), or a day against a stored day that only the
    publication / discovery date carries - a date known to lag the incident.
    A stored day stated in the records is never overruled by the timeline.
    """
    want = PRECISION_RANK.get(incident_precision, 0)
    if want == 0:
        return False, "the incident's date is not parseable"
    if want > stored["rank"]:
        return True, f"incident date ({incident_precision}) is finer than the stored date ({stored['precision']})"
    if want == stored["rank"] == 3 and (stored["lagging"] or stored["carried_by_no_record"]):
        why = "a publication / discovery date" if stored["lagging"] else "carried by no record"
        return True, f"the stored day is {why}; the incident date is stated"
    return False, "the stored date is at least as precise as the incident's"


def load_redate_labels(path: Path = REDATE_LABELS_PATH, required: bool = False) -> Dict[str, Dict[str, str]]:
    """Labelled date conflicts by event id; with ``required``, missing/unreadable/empty raises."""
    try:
        with Path(path).open(encoding="utf-8", newline="") as fh:
            labels = {r["deduplicated_event_id"]: r for r in csv.DictReader(fh) if r.get("deduplicated_event_id")}
    except (OSError, csv.Error, KeyError) as exc:
        if required:
            raise RuntimeError(f"Timeline re-date labels unreadable ({path}): {exc}; refusing to re-date") from exc
        return {}
    if required and not labels:
        raise RuntimeError(f"Timeline re-date labels file {path} is empty; refusing to re-date")
    return labels


def redate_calibration_identity() -> Dict[str, str]:
    return {"question_version": MATCH_VERSION, "model": typesafe_model()}


def active_redate_calibration(path: Path = REDATE_CALIBRATION_PATH) -> Optional[Dict[str, Any]]:
    try:
        cal = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("TIMELINE RE-DATING IS REPORT ONLY: no calibration (%s)", exc)
        return None
    want = redate_calibration_identity()
    if cal.get("question_version") != want["question_version"] or cal.get("model") != want["model"]:
        logger.warning("TIMELINE RE-DATING IS REPORT ONLY: calibration is for %s / %s, current is %s / %s",
                       cal.get("model"), cal.get("question_version"), want["model"], want["question_version"])
        return None
    if cal.get("cutoff") is None:
        logger.warning("TIMELINE RE-DATING IS REPORT ONLY: no cut-off reached the precision target")
        return None
    from cyber_data_collector.dedup import deployment

    return deployment.gate("timeline-redates", cal, Path(path))


# ---------------------------------------------------------------- reconciliation

def reconcile_organisation(conn: sqlite3.Connection, entity_id: int, org: str, judge,
                           queue_gaps: bool = False, search=None) -> Dict[str, Any]:
    from cyber_data_collector.dedup.corroboration import stored as stored_corroboration
    from cyber_data_collector.dedup.duplicate_judgement import _event_with_dates
    from cyber_data_collector.dedup.org_timeline import limitations, parse_timeline, stored

    timeline = stored(conn, entity_id)
    incidents = parse_timeline(timeline["timeline"]) if timeline else []
    events = conn.execute(
        """SELECT d.deduplicated_event_id, d.title, d.event_date FROM DeduplicatedEventEntities dee
           JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id
           WHERE dee.entity_id = ? AND dee.relationship_type = 'victim' AND d.status = 'Active'
           ORDER BY d.event_date""", (entity_id,)).fetchall()
    result: Dict[str, Any] = {"organisation": org, "entity_id": entity_id, "incidents": incidents,
                              "timeline_researched_at": timeline["researched_at"] if timeline else None,
                              "timeline_limitations": limitations(conn, entity_id) if timeline else [],
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
        entry = {"deduplicated_event_id": did, "entity_id": entity_id, "victim": org, "title": title,
                 "event_date": event_date, "match": match.answer, "match_probability": match.value,
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
                quality = stored_date_quality(conn, did, event_date)
                eligible, why = redate_eligible(precision, quality)
                result["date_conflicts"].append({
                    "deduplicated_event_id": did, "entity_id": entity_id, "title": title,
                    "stored_date": event_date, "stored_date_quality": quality,
                    "incident_date": incident["date"], "incident_parsed": when.isoformat(),
                    "incident_precision": precision, "incident": number, "incident_text": incident["what"],
                    "incident_source": incident["source"], "match_probability": match.value,
                    "redate_eligible": eligible, "eligibility": why})
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


def _resolve_unmatched(conn: sqlite3.Connection, organisations: List[Dict[str, Any]]) -> None:
    """Attach, to each unmatched event, the calibrated step that resolves it and what it decided."""
    from cyber_data_collector.dedup import event_reality as er
    from cyber_data_collector.dedup import victim_judgement as vj

    events = [e for o in organisations for e in o["events"] if e.get("unmatched_kind")]
    # Scores are read from the decision made on each event's evidence as it is now: a judgement given before the
    # event's records, timeline or co-victims changed says nothing about it today.
    victim_scores = vj.stored_final_probabilities(conn, [
        {"pair_id": f"{e['deduplicated_event_id']}|{e['entity_id']}", "deduplicated_event_id": e["deduplicated_event_id"],
         "entity_id": e["entity_id"], "victim": e["victim"]}
        for e in events if e["unmatched_kind"] == "wrong_organisation"])
    victim_cal = vj.active_calibration()
    reality = er.stored_final_probabilities(conn, [e["deduplicated_event_id"] for e in events])
    reality_cal = er.active_calibration()
    for e in events:
        kind = e["unmatched_kind"]
        if kind == "wrong_organisation":
            p = victim_scores.get(f"{e['deduplicated_event_id']}|{e['entity_id']}")
            cut = victim_cal["cutoff"] if victim_cal else None
            if p is None:
                outcome = "no judgement on its current evidence: run check-victim-attribution"
            elif cut is None:
                outcome = "victim attribution is report-only (no matching calibration)"
            else:
                outcome = ("re-attributed or flagged by check-victim-attribution --apply" if p < cut
                           else "kept: the calibrated victim judgement confirms the organisation")
            e["resolution"] = {"step": "check-victim-attribution", "p_recorded_victim": p, "cutoff": cut,
                               "outcome": outcome}
        elif kind == "not_an_incident":
            p = reality.get(e["deduplicated_event_id"])
            cut = reality_cal["cutoff"] if reality_cal else None
            if p is None:
                outcome = "no judgement on its current evidence: run check-events"
            elif cut is None:
                outcome = "event reality is report-only (no matching calibration)"
            else:
                outcome = ("rejected by check-events --apply" if p < cut
                           else "kept: the calibrated event-reality judgement says it is real")
            e["resolution"] = {"step": "check-events", "p_real": p, "cutoff": cut, "outcome": outcome}
        else:
            e["resolution"] = {"step": "none", "outcome": "kept: a real incident the refreshed timeline still "
                                                          "misses (report only)"}


def plan_redates(organisations: List[Dict[str, Any]], cal: Optional[Dict[str, Any]],
                 labels: Dict[str, Dict[str, str]]) -> List[Dict[str, Any]]:
    """Date conflicts to re-date: eligible, matched at or above the cut-off, and not labelled otherwise."""
    if not cal:
        return []
    plans = []
    for o in organisations:
        for c in o["date_conflicts"]:
            label = labels.get(c["deduplicated_event_id"])
            if label and label.get("label") != "timeline_date_right":
                continue   # ground truth: never acted on the other way
            if not c["redate_eligible"] or (c["match_probability"] or 0) < cal["cutoff"]:
                continue
            plans.append({**c, "new_date": c["incident_parsed"]})
    return plans


def apply_redates(conn: sqlite3.Connection, plans: List[Dict[str, Any]], cal: Dict[str, Any]) -> str:
    from cyber_data_collector.dedup.ledger import DedupLedger

    conn.executescript(REDATES_SCHEMA)
    ledger = DedupLedger(conn)
    batch_id = f"timeline-redate-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    now = datetime.now().isoformat()
    cal_json = json.dumps({k: cal.get(k) for k in ("question_version", "model", "cutoff", "generated",
                                                   "calibrated_on_database")})
    try:
        for p in plans:
            ledger.snapshot_event(batch_id, p["deduplicated_event_id"])
            conn.execute("UPDATE DeduplicatedEvents SET event_date = ?, updated_at = CURRENT_TIMESTAMP "
                         "WHERE deduplicated_event_id = ?", (p["new_date"], p["deduplicated_event_id"]))
            conn.execute(
                "INSERT INTO TimelineRedates (batch_id, deduplicated_event_id, entity_id, old_date, new_date, "
                "incident_date_text, incident_text, match_probability, cutoff, calibration_json, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (batch_id, p["deduplicated_event_id"], p["entity_id"], p["stored_date"], p["new_date"],
                 p["incident_date"], p["incident_text"][:1000], p["match_probability"], cal["cutoff"], cal_json,
                 p["eligibility"], now))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return batch_id


def reconcile_all(conn: sqlite3.Connection, judge=None, queue_gaps: bool = False, search=None,
                  apply: bool = False, timeline_search=None,
                  redate_calibration_path: Path = REDATE_CALIBRATION_PATH,
                  redate_labels_path: Path = REDATE_LABELS_PATH) -> Dict[str, Any]:
    """Refresh timelines, reconcile every organisation, attach resolutions; with ``apply``, re-date.

    Raises when a timeline cannot be refreshed, a judgement fails, or
    ``apply`` is asked for without readable re-date labels.
    """
    from cyber_data_collector.dedup.org_timeline import organisations_to_research, research_all
    from cyber_data_collector.utils.jev import JevJudge

    labels = load_redate_labels(redate_labels_path, required=apply)
    research_all(conn, search=timeline_search)
    owned = judge is None
    judge = judge or JevJudge(conn)
    organisations = []
    try:
        for org in organisations_to_research(conn):
            organisations.append(reconcile_organisation(conn, org["entity_id"], org["entity_name"], judge,
                                                        queue_gaps=queue_gaps, search=search))
    finally:
        if owned:
            judge.close()
    _resolve_unmatched(conn, organisations)
    cal = active_redate_calibration(redate_calibration_path)
    redates = plan_redates(organisations, cal, labels)
    batch = apply_redates(conn, redates, cal) if (apply and cal and redates) else None
    events = [e for o in organisations for e in o["events"]]
    conflicts = [c for o in organisations for c in o["date_conflicts"]]
    return {
        "organisations": organisations,
        "redates": redates, "redates_applied": batch is not None, "redate_batch_id": batch,
        "redate_calibrated": cal is not None, "redate_cutoff": cal["cutoff"] if cal else None,
        "summary": {
            "organisations": len(organisations),
            "events": len(events),
            "matched": sum(1 for e in events if e["match"] not in (None, "none")),
            "unmatched": {k: sum(1 for e in events if e.get("unmatched_kind") == k) for k in CLASS_CRITERIA},
            "low_confidence_matches": sum(1 for e in events if e["low_confidence"]),
            "coverage_gaps": sum(len(o["gaps"]) for o in organisations),
            "date_conflicts": len(conflicts),
            "redate_eligible": sum(1 for c in conflicts if c["redate_eligible"]),
            "redates_planned": len(redates),
        },
    }
