"""Separate incidents merged under the same organisation: candidates, a judgement, a calibrated split.

:mod:`coherence` finds *strays* - members whose organisation is not the
event's - and deliberately leaves same-organisation members alone: they are
usually misdated copies or follow-ups of the one incident (Partnered Health,
Latitude, OpenAI in the 2026-10-02 review), and splitting them on a model's
say-so had wrongly separated such copies. That leaves the opposite error
unseen: two real incidents at one organisation joined into one event. The
2026-10-28 recurrence check merged two labelled-different pairs that way
(Western Sydney University's August 2024 and January 2025 breaches; youX's
March 2025 S3 exposure and February 2026 MongoDB theft), and the quality gate
caught them only because they happened to be labelled.

This adds the missing check without reopening the old wound:

1. **Candidates (free).** A member naming the event's own organisation (or no
   organisation) becomes a candidate only on *evidence of a different
   incident*, never a date gap by itself:

   * **date** - the member's incident date is verified (stated in the record
     or researched, not a publication or discovery date; or a precision the
     source established) and its interval lies more than
     :data:`DATE_GAP_DAYS` from the event's date interval; or
   * **timeline** - the member's date matches one incident in the
     organisation's researched timeline (``OrganisationTimelines``) and the
     event's date matches a *different* one.

2. **Judgement (Jev, stored).** One question per candidate, a versioned
   variant of the stray question (``member-coherence-sameorg-v1``): does the
   record report the SAME incident as the event, or a separate incident at the
   same organisation? The candidate reasons are stated as hints to look at,
   not evidence; the record's own content and the timeline decide. Scores
   inside :data:`ESCALATE_BAND` go to the reasoning model; the final score is
   its answer where there is one. A failed answer raises / leaves the member
   undecided exactly as in :mod:`coherence` - no fallback.

3. **Repair, only when calibrated.** Splitting needs its own cut-off, measured
   on labelled same-organisation cases
   (``evaluation/coherence_same_org_labels.csv``,
   ``scripts/calibrate_coherence_same_org.py``) and recorded with the models,
   question versions and evidence fingerprint. With no matching calibration
   the step reports. Labels are ground truth: ``same`` never leaves,
   ``different`` always does. Guards: the departing records must be fewer than
   those that stay (otherwise the event's own label is what is in doubt and it
   is held for review), and a split goes through the ledger
   (:meth:`DedupLedger.split_members`: snapshotted, reversible, cached as
   ``different`` rulings), the departing records leaving together when they
   lie within :data:`coherence.HOME_WINDOW_DAYS` of each other.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from cyber_data_collector.dedup import calibration_guard, coherence, final_decisions
from cyber_data_collector.dedup.entity_resolution import EntityResolver
from cyber_data_collector.dedup.event_reality import ROOT
from cyber_data_collector.utils.llm_models import typesafe_model

logger = logging.getLogger(__name__)

SUBJECT_TYPE = "event_member_same_org"
QUESTION_ID = "member_same_incident_same_org"
QUESTION_VERSION = "member-coherence-sameorg-v1"
ESCALATION_QUESTION_ID = "member_same_incident_same_org_escalated"
ESCALATION_VERSION = "member-coherence-sameorg-escalation-v1"
ESCALATE_BAND = (0.1, 0.7)
#: A verified member date further than this from the event's date interval makes a candidate (the
#: recurrence analysis's short-gap window: closer than this, reporting lag explains the difference).
DATE_GAP_DAYS = 90
#: How a record's date was established, for a date that can be trusted as the incident's.
VERIFIED_BASES = ("stated in the record", "Perplexity research")
#: Bump when what a candidate judgement shows changes (the hints, the provenance fields, the selection).
EVIDENCE_VERSION = "coherence-sameorg-evidence-v1"

LABELS_PATH = ROOT / "evaluation" / "coherence_same_org_labels.csv"
CALIBRATION_PATH = ROOT / "evaluation" / "coherence_same_org_calibration.json"

INSTRUCTIONS = (
    "`event` is a stored event in an Australian cyber-incident database, recorded against ONE "
    "organisation (`event.victim`). It should hold records about ONE real-world incident. `record` "
    "is one of its records and names the same organisation, but it was flagged (`flagged_because`) as "
    "possibly about a DIFFERENT incident at that organisation: its date lies far from the event's, or "
    "matches a different incident in the organisation's researched `organisation_timeline`. The flags are "
    "hints about where to look and are NOT evidence: a record's date is often wrong (a publication date, "
    "the date the record was found, or a month stored as the 1st), and follow-up coverage, a regulator "
    "notice or a class action months later is the same incident. `other_records` are the event's other "
    "records. Judge from what the record itself says happened - who, what system, what attacker or method, "
    "when - using its `title`, `summary`, `perplexity` research and `page_text` (which can be a different "
    "article stored by mistake, so title, summary and research outrank it), `date` provenance, and the "
    "timeline. Decide whether `record` reports the SAME real-world incident as the event, or a SEPARATE "
    "incident at the same organisation."
)
CRITERIA = {
    "true": ("Same incident: the record reports the event's incident - first reports, later coverage, a "
             "notification, a regulator statement, a class action, an aggregator entry, the organisation's own "
             "notice - including copies that carry a wrong date."),
    "false": ("A separate incident at the same organisation: a different attack, breach or exposure (a different "
              "time, system, attacker or method, or a new compromise after the first was resolved), which the "
              "record's own content or the timeline shows - not merely a different date."),
}


# ----------------------------------------------------------------------
# Candidates
# ----------------------------------------------------------------------

def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _day(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _heuristic_precision(day_text: Optional[str]) -> Optional[str]:
    """day / month / year from the shape of a stored date (a 1st may be a month, a 1 January a year)."""
    from cyber_data_collector.dedup.date_provenance import precision

    text = precision(day_text)
    if text == "unknown":
        return None
    return "year" if text.startswith("year") else "month" if text.startswith("month") else "day"


def _interval(value: Any, precision: Optional[str]) -> Optional[Tuple[date, date]]:
    from cyber_data_collector.processing.incident_dates import interval

    start, end = interval(value, precision)
    return (date.fromisoformat(start), date.fromisoformat(end)) if start and end else None


def interval_gap_days(a: Tuple[date, date], b: Tuple[date, date]) -> int:
    """Days between two intervals; 0 when they overlap."""
    if a[1] < b[0]:
        return (b[0] - a[1]).days
    if b[1] < a[0]:
        return (a[0] - b[1]).days
    return 0


def member_date_facts(conn: sqlite3.Connection, enriched_id: str) -> Dict[str, Any]:
    """A member's incident date, how it was established, whether that can be trusted, and its interval."""
    from cyber_data_collector.dedup.date_provenance import record_dates
    from cyber_data_collector.processing.incident_dates import PRECISIONS

    raw = _columns(conn, "RawEvents")
    row = conn.execute(
        f"""SELECT e.event_date, r.event_date, r.source_metadata, r.discovered_at, e.perplexity_enrichment_data,
                   COALESCE(NULLIF(r.raw_content, ''), r.raw_description, ''),
                   {"r.publication_date" if "publication_date" in raw else "NULL"},
                   {"r.event_date_precision" if "event_date_precision" in raw else "NULL"}
            FROM EnrichedEvents e LEFT JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
            WHERE e.enriched_event_id = ?""", (enriched_id,)).fetchone()
    if row is None:
        raise ValueError(f"no enriched record {enriched_id}")
    facts = record_dates(row[0], row[1], row[2], row[3], row[4], text=row[5], publication_date=row[6])
    source_precision = row[7] if row[7] in PRECISIONS else None
    day_text = facts["date"] or None      # date_provenance gives the day as text (YYYY-MM-DD)
    precision = source_precision or _heuristic_precision(day_text)
    verified = bool(day_text) and (facts["basis"] in VERIFIED_BASES or source_precision is not None)
    return {"date": day_text, "basis": facts["basis"], "precision": precision, "verified": verified,
            "interval": _interval(day_text, precision) if day_text else None}


def event_date_interval(conn: sqlite3.Connection, dedup_id: str) -> Optional[Tuple[date, date]]:
    """The event's date as an interval: the verified one when stored, else from the date's shape."""
    columns = _columns(conn, "DeduplicatedEvents")
    extra = ("event_date_earliest, event_date_latest" if {"event_date_earliest", "event_date_latest"} <= columns
             else "NULL, NULL")
    row = conn.execute(f"SELECT event_date, {extra} FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                       (dedup_id,)).fetchone()
    if row is None or not _day(row[0]):
        return None
    if _day(row[1]) and _day(row[2]):
        return _day(row[1]), _day(row[2])
    return _interval(row[0], _heuristic_precision(str(row[0])[:10]))


def _timeline_dates(timeline: Optional[Dict[str, Any]]) -> List[Tuple[str, date, str]]:
    from cyber_data_collector.dedup.org_timeline import parse_timeline
    from cyber_data_collector.dedup.timeline_reconciliation import incident_date

    out = []
    # ``incidents`` is the researched answer as stored (free text in a requested shape).
    for incident in parse_timeline((timeline or {}).get("incidents") or ""):
        when, precision = incident_date(incident.get("date") or "")
        if when is not None:
            out.append((str(incident.get("number")), when, precision))
    return out


def matched_incident(interval: Optional[Tuple[date, date]], dated: Sequence[Tuple[str, date, str]]) -> Optional[str]:
    """The researched incident whose date (± a tolerance by its precision) the interval touches; the nearest wins."""
    from cyber_data_collector.dedup.timeline_reconciliation import TOLERANCE_DAYS

    if interval is None:
        return None
    best: Optional[Tuple[int, str]] = None
    for number, when, precision in dated:
        tolerance = timedelta(days=TOLERANCE_DAYS.get(precision, 45))
        if interval[0] - tolerance <= when <= interval[1] + tolerance:
            distance = interval_gap_days(interval, (when, when))
            if best is None or distance < best[0]:
                best = (distance, number)
    return best[1] if best else None


def candidate_reasons(conn: sqlite3.Connection, dedup_id: str, member_id: str,
                      event_interval: Optional[Tuple[date, date]], dated_timeline: Sequence[Tuple[str, date, str]],
                      facts: Optional[Dict[str, Any]] = None) -> List[str]:
    """Why a same-organisation member might belong to a different incident; empty when no evidence says so."""
    facts = facts or member_date_facts(conn, member_id)
    reasons: List[str] = []
    if facts["verified"] and facts["interval"] and event_interval:
        gap = interval_gap_days(facts["interval"], event_interval)
        if gap > DATE_GAP_DAYS:
            reasons.append(f"its date ({facts['date']}, {facts['basis']}) is {gap} days from the event's date interval")
    if len(dated_timeline) >= 2 and facts["interval"] and event_interval:
        theirs, ours = matched_incident(facts["interval"], dated_timeline), matched_incident(event_interval, dated_timeline)
        if theirs and ours and theirs != ours:
            reasons.append(f"its date matches incident {theirs} in the organisation's timeline, "
                           f"the event's matches incident {ours}")
    return reasons


def find_candidate_events(conn: sqlite3.Connection, resolver: Optional[EntityResolver] = None,
                          only: Optional[Sequence[str]] = None,
                          include: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """Active multi-member events with at least one same-organisation candidate member.

    Each result is :func:`coherence.find_suspect_events`-shaped, with ``candidates``
    (``{enriched id: [reasons]}``) and ``suspect_ids`` limited to other-organisation
    members (judged by :mod:`coherence`, not here). ``include`` adds events by id
    even with no candidate (events holding a labelled member).
    """
    from cyber_data_collector.dedup.org_timeline import timeline_for_event

    events = coherence._multi_member_events(conn)
    if only:
        events = [e for e in events if any(e[0].startswith(p) for p in only)]
    members_by_event = {e[0]: coherence.load_event_members(conn, e[0]) for e in events}
    resolver = coherence.fitted_resolver(conn, resolver, events, members_by_event)
    wanted = set(include or ())
    found = []
    for did, title, event_date, victim, vendor in events:
        members = members_by_event[did]
        strays = {m["id"] for m in coherence.suspect_members(resolver, members, victim, vendor)}
        event_interval = event_date_interval(conn, did)
        dated_timeline = _timeline_dates(timeline_for_event(conn, did))
        candidates = {}
        for m in members:
            if m["id"] in strays:
                continue
            reasons = candidate_reasons(conn, did, m["id"], event_interval, dated_timeline)
            if reasons:
                candidates[m["id"]] = reasons
        if candidates or did in wanted:
            found.append({"id": did, "title": title, "date": event_date, "victim": victim, "vendor": vendor,
                          "members": members, "suspect_ids": sorted(strays), "candidates": candidates})
    return found


# ----------------------------------------------------------------------
# What Jev sees
# ----------------------------------------------------------------------

def build_member_state(conn: sqlite3.Connection, event: Dict[str, Any], enriched_id: str) -> Dict[str, Any]:
    """The stray question's evidence for the member, plus date provenance and why it was flagged."""
    from cyber_data_collector.dedup.date_provenance import event_date_provenance

    state = coherence.build_member_state(conn, event, enriched_id)
    facts = member_date_facts(conn, enriched_id)
    state["record"]["date_provenance"] = {"date": facts["date"], "basis": facts["basis"],
                                          "precision": facts["precision"]}
    provenance = event_date_provenance(conn, event["id"])
    state["event"]["date_basis"] = provenance["date_basis"]
    state["event"]["date_precision"] = provenance["date_precision"]
    state["flagged_because"] = list((event.get("candidates") or {}).get(enriched_id)
                                    or candidate_reasons(conn, event["id"], enriched_id,
                                                         event_date_interval(conn, event["id"]),
                                                         _timeline_dates(state.get("organisation_timeline")),
                                                         facts))
    return state


def question():
    from typesafe_sdk import Noul, NoulCriteria
    return Noul(instructions=INSTRUCTIONS, criteria=NoulCriteria(**CRITERIA))


def escalation_question() -> str:
    return (INSTRUCTIONS + f" YES means: {CRITERIA['true']} NO means: {CRITERIA['false']}"
            " Does `record` report the same real-world cyber incident as the event?")


def evidence_fingerprint() -> str:
    """The stray question's evidence plus this module's selection and hints."""
    return calibration_guard.fingerprint(
        "coherence-same-org", version=EVIDENCE_VERSION, date_gap_days=DATE_GAP_DAYS,
        verified_bases=list(VERIFIED_BASES), escalate_band=list(ESCALATE_BAND),
        stray_evidence=coherence.evidence_fingerprint())


def calibration_identity() -> Dict[str, str]:
    from cyber_data_collector.utils.llm_models import openai_model
    return {"question_version": f"{QUESTION_VERSION}+{ESCALATION_VERSION}",
            "model": f"{typesafe_model()}+{openai_model()}", "evidence": evidence_fingerprint()}


# ----------------------------------------------------------------------
# Judging
# ----------------------------------------------------------------------

def judge_members(conn: sqlite3.Connection, items: Sequence[Tuple[Dict[str, Any], str]], judge=None,
                  escalation_client=None, refresh: bool = False) -> Dict[str, Dict[str, Any]]:
    """Final P(same incident) for each (event, member id); same contract as :func:`coherence.judge_members`."""
    from cyber_data_collector.utils.escalation import escalate
    from cyber_data_collector.utils.jev import JevJudge

    owned = judge is None
    judge = judge or JevJudge(conn)
    q = {QUESTION_ID: question()}
    lo, hi = ESCALATE_BAND
    out: Dict[str, Dict[str, Any]] = {}
    try:
        for i, (event, enriched_id) in enumerate(items, 1):
            sid = coherence.member_subject_id(event["id"], enriched_id)
            state = build_member_state(conn, event, enriched_id)
            try:
                answer = judge.ask(SUBJECT_TYPE, sid, QUESTION_VERSION, state, q, refresh=refresh)[QUESTION_ID]
            except Exception as exc:  # noqa: BLE001 - re-raised with what failed
                raise coherence.CoherenceJudgementError(
                    f"Jev ({typesafe_model()}) could not judge same-organisation member {sid}: {exc}") from exc
            p_jev = float(answer.value)
            if not lo <= p_jev < hi:
                out[sid] = {"p_jev": p_jev, "p_escalated": None, "p": p_jev, "error": None}
                final_decisions.record(conn, STEP, sid, state, p_jev, escalated=False)
                continue
            p_esc = escalate(conn, SUBJECT_TYPE, sid, ESCALATION_QUESTION_ID, ESCALATION_VERSION,
                             state, escalation_question(), client=escalation_client)
            if p_esc is None:
                logger.error("UNDECIDED: escalation failed for same-organisation member %s (Jev %.3f, inside %s)",
                             sid, p_jev, ESCALATE_BAND)
                out[sid] = {"p_jev": p_jev, "p_escalated": None, "p": None,
                            "error": "escalation failed (see log); retried next run"}
            else:
                out[sid] = {"p_jev": p_jev, "p_escalated": p_esc, "p": p_esc, "error": None}
                final_decisions.record(conn, STEP, sid, state, p_esc, escalated=True)
            if i % 25 == 0:
                logger.info("Judged %d/%d same-organisation candidates", i, len(items))
    finally:
        conn.commit()
        if owned:
            judge.close()
    return out


STEP = final_decisions.Step(
    name="coherence-same-organisation", question_id=QUESTION_ID, version=QUESTION_VERSION, make_question=question,
    escalation_id=ESCALATION_QUESTION_ID, escalation_version=ESCALATION_VERSION,
    escalation_text=escalation_question, band=ESCALATE_BAND)


def stored_final_probabilities(conn: sqlite3.Connection, items: Sequence[Tuple[Dict[str, Any], str]]
                               ) -> Dict[str, float]:
    """Final score per ``(event, member id)`` from its decision on the evidence it has **now**
    (:mod:`final_decisions`); a member with no decision on its current evidence has no score here."""
    return final_decisions.read_current(
        conn, STEP, [(coherence.member_subject_id(event["id"], mid), lambda e=event, m=mid: build_member_state(conn, e, m)) for event, mid in items])


# ----------------------------------------------------------------------
# Deciding and repairing
# ----------------------------------------------------------------------

def active_calibration(path: Path = CALIBRATION_PATH) -> Optional[Dict[str, Any]]:
    """The split calibration, only if measured on the current models, questions and evidence processing.

    Unlike the stray check there is no 'missing file is a fault': this step has no
    calibration until its labels exist, and reports until then (logged at WARNING).
    """
    import json

    try:
        cal = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("REPORT ONLY: no same-organisation coherence calibration (%s); candidates are judged "
                       "and reported, nothing is split except labelled-different members. Label the "
                       "candidates and run scripts/calibrate_coherence_same_org.py.", Path(path).name)
        return None
    except (OSError, ValueError) as exc:
        raise coherence.CoherenceConfigError(f"same-organisation calibration {path} unreadable: {exc}") from exc
    reason = calibration_guard.mismatch(cal, calibration_identity())
    if reason:
        logger.warning("%s Nothing will be split except labelled-different members.",
                       calibration_guard.report_only_message("same-organisation coherence", cal, reason))
        return None
    if cal.get("cutoff") is None:
        logger.warning("REPORT ONLY: the same-organisation calibration found no cut-off meeting its precision "
                       "target; nothing will be split except labelled-different members.")
        return None
    from cyber_data_collector.dedup import deployment

    return deployment.gate("coherence-same-organisation", cal, Path(path))


def decide_splits(event: Dict[str, Any], scores: Dict[str, Dict[str, Any]], labels: Dict[str, str],
                  cutoff: Optional[float]) -> Dict[str, Any]:
    """Which candidates of one event leave, and what a guard held back. ``{"leave": [...], "held": [...], "note"}``."""
    members = {m["id"]: m for m in event["members"]}
    leave, held = [], []
    for mid in event.get("candidates") or {}:
        sid = coherence.member_subject_id(event["id"], mid)
        label, score = labels.get(sid), scores.get(sid) or {}
        entry = {**members[mid], "subject_id": sid, "p_same": score.get("p"), "p_jev": score.get("p_jev"),
                 "p_escalated": score.get("p_escalated"), "label": label,
                 "reasons": event["candidates"][mid]}
        if label == "same":
            continue
        if label == "different":
            leave.append({**entry, "basis": "label"})
        elif cutoff is not None and score.get("p") is not None and score["p"] < cutoff:
            leave.append({**entry, "basis": "model"})
    # labelled-different members of the event that are not candidates leave too (labels win both ways)
    for mid, m in members.items():
        sid = coherence.member_subject_id(event["id"], mid)
        if labels.get(sid) == "different" and mid not in (event.get("candidates") or {}) \
                and mid not in event.get("suspect_ids", []):
            leave.append({**m, "subject_id": sid, "p_same": None, "p_jev": None, "p_escalated": None,
                          "label": "different", "reasons": ["labelled a separate incident"], "basis": "label"})
    note = None
    stay = len(members) - len(leave)
    if leave and stay <= len(leave) and any(s["basis"] == "model" for s in leave):
        # The majority leaving means the event's own picture of the incident is in doubt, not its stragglers.
        held = [{**s, "guard": "the departing records are not fewer than those that stay"} for s in leave
                if s["basis"] == "model"]
        leave = [s for s in leave if s["basis"] == "label"]
        note = "model-judged splits held: they would leave at least as many records as stay (review the event)"
    if len(leave) >= len(members):
        held.extend({**s, "guard": "would leave the event with no records"} for s in leave)
        leave = []
    return {"leave": leave, "held": held, "note": note}


def _public(s: Dict[str, Any]) -> Dict[str, Any]:
    keep = ("id", "title", "date", "org", "url", "p_same", "p_jev", "p_escalated", "label", "basis", "guard",
            "reasons")
    return {k: s.get(k) for k in keep if k in s}


def check_same_organisation(conn: sqlite3.Connection, apply: bool = False, judge=None, escalation_client=None,
                            only: Optional[Sequence[str]] = None, calibration_path: Path = CALIBRATION_PATH,
                            labels_path: Path = LABELS_PATH, ledger=None) -> Dict[str, Any]:
    """Judge every same-organisation candidate; split those below the calibrated cut-off (and labelled ones).

    Without a matching calibration only members labelled 'different' are split, and only with ``apply``.
    A missing labels file is no labels yet (the step has none until candidates are labelled); an
    unreadable or malformed one raises :class:`coherence.CoherenceConfigError`, as does an unreadable
    calibration. A Jev failure raises :class:`coherence.CoherenceJudgementError`; a failed escalation
    leaves the member undecided (``undecided`` in the result).
    """
    labels: Dict[str, str] = {}
    incidents: Dict[str, str] = {}
    if Path(labels_path).exists():
        labels = coherence.load_labels(labels_path)
        incidents = coherence.load_label_incidents(labels_path)
    elif apply:
        logger.warning("No same-organisation labels (%s): nothing protects a member labelled 'same' - "
                       "which is only a risk once labels exist", Path(labels_path).name)
    cal = active_calibration(calibration_path)
    cutoff = cal["cutoff"] if cal else None
    resolver = EntityResolver(conn)
    events = find_candidate_events(conn, resolver, only=only, include={sid.split("|", 1)[0] for sid in labels})
    # Labels decide first (a member labelled same stays, one labelled different leaves), so it is not asked about.
    items = [(e, mid) for e in events for mid in e["candidates"]
             if labels.get(coherence.member_subject_id(e["id"], mid)) not in ("same", "different")]
    scores = judge_members(conn, items, judge=judge, escalation_client=escalation_client)
    undecided = sorted(sid for sid, s in scores.items() if s.get("error"))

    findings, splits = [], []
    for event in events:
        decision = decide_splits(event, scores, labels, cutoff)
        if not decision["leave"] and not decision["held"]:
            continue
        # Members with no organisation of their own are the event's organisation (that is why they are
        # here), so records leaving together group by it; a labelled incident name groups them too.
        groups = coherence.group_strays(
            [{**s, "org": s.get("org") or event["victim"],
              "incident": incidents.get(s["subject_id"]) if s.get("label") == "different" else None}
             for s in decision["leave"]], resolver)
        entry = {"event": event["id"], "title": event["title"], "victim": event["victim"],
                 "records": len(event["members"]), "note": decision["note"],
                 "groups": [[_public(s) for s in g] for g in groups],
                 "held": [_public(s) for s in decision["held"]], "new_events": []}
        if apply and groups:
            entry["new_events"] = _split(conn, event["id"], groups, ledger)
            splits.extend(entry["new_events"])
        findings.append(entry)
    return {"events_judged": len(events), "candidates_judged": len(scores), "undecided": undecided,
            "settled_by_label": sum(len(e["candidates"]) for e in events) - len(items),
            "escalated": sum(1 for s in scores.values() if s["p_escalated"] is not None),
            "calibrated": cal is not None, "cutoff": cutoff, "applied": bool(apply and splits),
            "records_to_split": sum(len(g) for f in findings for g in f["groups"]),
            "events_with_splits": sum(1 for f in findings if f["groups"]),
            "split_records": sum(len(s["records"]) for s in splits), "findings": findings, "scores": scores}


def _split(conn: sqlite3.Connection, dedup_id: str, groups: Sequence[Sequence[Dict[str, Any]]],
           ledger=None) -> List[Dict[str, Any]]:
    """Split each group out of ``dedup_id`` through the ledger (snapshotted, 'different' rulings cached)."""
    from cyber_data_collector.dedup.ledger import DedupLedger

    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        ledger = ledger or DedupLedger(conn)
        out = []
        for group in groups:
            ids = [s["id"] for s in group]
            why = ("labelled a separate incident" if any(s.get("basis") == "label" for s in group) else
                   f"P(same) {max(s['p_same'] for s in group):.3f} below the calibrated cut-off ({QUESTION_VERSION})")
            reason = f"[coherence-same-org] separate incident at the same organisation: {why}"
            out.append({"new_event": ledger.split_members(dedup_id, ids, reason=reason, actor="rule"),
                        "records": ids})
        conn.commit()
        return out
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.row_factory = previous
