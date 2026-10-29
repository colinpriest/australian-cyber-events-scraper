"""Is a stored event a real cyber incident? An event-level judgement with a calibrated cut-off.

The page gate (:mod:`page_classifier` + :mod:`non_incident`) asks what each
stored *page* is. That cannot decide whether the *event* happened: for many
real incidents the page we hold is the victim's privacy policy or home page,
while the incident itself came from Perplexity's research (LUSH, Nexia, Legal
Aid ACT). On 2026-10-27 the page gate would have rejected 74 events, many of
them real.

This asks Jev one question about the whole event, with all of its evidence in
view: every record's own page text **and** the Perplexity research attached
to it (the organisation it identified, its reasoning, the sources it
consulted, its dates and attack method). The answer is a probability that
the event is a real, specific cyber incident, stored in ``ModelJudgements``.

Rejection uses a **calibrated** cut-off, never a default: a labelled sample
of this database's events (``evaluation/event_reality_labels.csv``) fixes the
largest cut-off whose rejections meet a precision target, and the result is
written to ``evaluation/event_reality_calibration.json`` together with the
model and question version it was measured on. :func:`active_calibration`
returns it only while the model, the question version **and the evidence
processing** (:func:`evidence_fingerprint`) all still match, so changing any of
them turns rejection back into a report until it is recalibrated on current
evidence (``scripts/dedup_admin.py recalibrate``; ``scripts/calibrate_event_reality.py``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from cyber_data_collector.dedup import calibration_guard, final_decisions
from cyber_data_collector.utils.llm_models import typesafe_model

logger = logging.getLogger(__name__)

QUESTION_ID = "is_real_incident"
# v1 sees the stored evidence only. Calibrated 2026-10-27 it ranked well
# (weighted AUC 0.957) but real incidents with thin stored evidence scored low,
# so no cut-off reached the precision target. It is now the *screen*: events
# it scores below CORROBORATE_BELOW get a web search (see corroboration), and
# v2 - the same evidence plus that search - is the question calibrated and
# used to reject.
#
# v2 (corroborated) calibrated at 0.25 with weighted precision 0.956. v3 adds
# the victim organisation's researched incident timeline (org_timeline), and
# events v3 scores inside ESCALATE_BAND - fixed before calibration - are
# re-judged by the reasoning model (utils.escalation). The *final* score is the
# escalation's where there is one, else Jev's; that is what is calibrated.
SCREEN_VERSION = "event-reality-v1"
QUESTION_VERSION = "event-reality-v3-timeline"
ESCALATION_QUESTION_ID = "is_real_incident_escalated"
ESCALATION_VERSION = "event-reality-escalation-v1"
ESCALATE_BAND = (0.1, 0.7)
CORROBORATE_BELOW = 0.5
MAX_RECORDS = 10
PAGE_CHARS = 1200
REASONING_CHARS = 800
#: Bump when what ``_evidence_state`` shows a model changes (which records, which pages,
#: how research is summarised). v1 (2026-10-27) showed every mapped record and every page;
#: v2 (2026-10-28) shows active records only and withholds pages of other articles
#: (``processing/active_evidence.py``). Every calibration measured before v2 is stale.
EVIDENCE_VERSION = "event-evidence-v2"
#: The Perplexity research fields a judgement is shown.
RESEARCH_FIELDS = ("formal_entity_name", "overall_confidence", "earliest_event_date", "attack_method",
                   "threat_actor", "victim_count", "sources_consulted")

ROOT = Path(__file__).resolve().parents[2]
LABELS_PATH = ROOT / "evaluation" / "event_reality_labels.csv"
CALIBRATION_PATH = ROOT / "evaluation" / "event_reality_calibration.json"

INSTRUCTIONS = (
    "Decide whether this stored event is a real, specific cyber incident: an identifiable "
    "cyber attack, data breach, ransomware or extortion claim, account compromise, or "
    "cyber-caused disruption, affecting a named or clearly described organisation (or its "
    "customers) at a particular time. Weigh all the evidence together. `records[].page_text` "
    "is the page actually stored for each record - it is often the organisation's privacy "
    "policy, home page or a news index rather than the article, so a page that does not "
    "describe the incident is NOT evidence against it. `records[].perplexity` is separate "
    "research about the record: a named victim with specific dates, attack details and "
    "sources is evidence for the incident; vague, generic or zero-confidence research is not. "
    "Generated titles such as 'X data breach (2025)' are not evidence on their own."
)
CORROBORATION_INSTRUCTIONS = (
    " `corroboration`, when present, is a fresh web search for public reporting of this "
    "incident (`answer` and `citations`). Credible reporting found there is strong evidence for "
    "the incident even when the stored pages are unhelpful. If the search found nothing and the "
    "records hold no concrete evidence either, that is evidence against it. A search that found a "
    "*different* incident at the same organisation is not evidence for this one."
)
TIMELINE_INSTRUCTIONS = (
    " `organisation_timeline`, when present, is researched history of the victim organisation's "
    "publicly reported cyber incidents, with dates and sources. An event that matches an incident "
    "in it is real. An event that matches none of them, for an organisation whose history was "
    "researched, needs concrete evidence of its own."
)
CRITERIA = {
    "true": ("A specific incident is described by the page text or by concrete research: who was "
             "affected, what happened, and roughly when."),
    "false": ("No specific incident: a service, information, policy, guidance or marketing page; "
              "a statistics, trend or roundup page; general commentary; a non-cyber event (fraud "
              "without a cyber element, a political or budget story); or research that only "
              "restates the page without finding an incident."),
}


def _perplexity(pe: Optional[str]) -> Dict[str, Any]:
    try:
        data = json.loads(pe or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {k: data.get(k) for k in RESEARCH_FIELDS if data.get(k) not in (None, "", [], {})}
    if data.get("reasoning"):
        out["reasoning"] = str(data["reasoning"])[:REASONING_CHARS]
    return out


def build_state(conn: sqlite3.Connection, dedup_id: str, corroborated: bool = False,
                max_records: int = MAX_RECORDS, with_timeline: bool = False,
                with_record_ids: bool = False) -> Dict[str, Any]:
    """Everything Jev sees about one event. Deterministic, so the input hash is stable.

    With ``corroborated``, a stored web search for the event is added when one
    exists; with ``with_timeline``, the victim organisation's stored incident
    timeline. With ``with_record_ids`` each record carries its ``enriched_event_id``
    so a caller can join other per-record facts (date provenance) **by id, never by
    position**; the caller removes it before the state goes to a model.
    """
    state = _evidence_state(conn, dedup_id, max_records, with_record_ids)
    if corroborated:
        from cyber_data_collector.dedup.corroboration import stored
        found = stored(conn, dedup_id)
        if found:
            state["corroboration"] = {"answer": found["answer"], "citations": found["citations"]}
    if with_timeline:
        from cyber_data_collector.dedup.org_timeline import timeline_for_event
        # organisation / incidents / citations, plus a warning only when the
        # history may be incomplete (stale, failed or declined research).
        timeline = timeline_for_event(conn, dedup_id)
        if timeline:
            state["organisation_timeline"] = timeline
    return state


def _evidence_state(conn: sqlite3.Connection, dedup_id: str, max_records: int = MAX_RECORDS,
                    with_record_ids: bool = False) -> Dict[str, Any]:
    event = conn.execute(
        "SELECT title, event_date, victim_organization_name FROM DeduplicatedEvents "
        "WHERE deduplicated_event_id = ?", (dedup_id,)).fetchone()
    from cyber_data_collector.processing.active_evidence import evidence_page, member_condition, research_names

    # Active members only, and a page only when it belongs to its record
    # (processing/active_evidence.py) - the rule every evidence reader shares.
    rows = conn.execute(
        f"""SELECT e.enriched_event_id, COALESCE(e.title, ''), COALESCE(r.source_url, ''),
                  COALESCE(NULLIF(r.raw_content, ''), r.raw_description, ''),
                  e.perplexity_enrichment_data
           FROM EventDeduplicationMap m
           JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
           LEFT JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
           WHERE m.deduplicated_event_id = ? {member_condition(conn)}""", (dedup_id,)).fetchall()
    victim = event[2] if event else None
    rows = [(r[0], r[1], r[2], *evidence_page(r[1], [*research_names(r[4]), victim], r[3]), r[4]) for r in rows]
    # Records with page text first, then a stable order, so a large event
    # always shows the same ten.
    rows = sorted(rows, key=lambda r: (not r[3], r[0]))
    records = []
    for r in rows[:max_records]:
        record = {"title": r[1], "url": r[2],
                  "page_text": (r[3] or "")[:PAGE_CHARS] or ("(withheld: it appears to be another article)"
                                                             if r[4] else "(none stored)"),
                  "perplexity": _perplexity(r[5])}
        if r[4]:
            record["page_withheld"] = r[4]
        if with_record_ids:
            record["enriched_event_id"] = r[0]
        records.append(record)
    return {
        "event": {"title": event[0] if event else "", "date": event[1] if event else None,
                  "victim": event[2] if event else None, "record_count": len(rows)},
        "records": records,
    }


def _instructions(version: str) -> str:
    if version == SCREEN_VERSION:
        return INSTRUCTIONS
    return INSTRUCTIONS + CORROBORATION_INSTRUCTIONS + TIMELINE_INSTRUCTIONS


def question(version: str = QUESTION_VERSION):
    from typesafe_sdk import Noul, NoulCriteria
    return Noul(instructions=_instructions(version), criteria=NoulCriteria(**CRITERIA))


def escalation_question() -> str:
    return (_instructions(QUESTION_VERSION) + f" YES means: {CRITERIA['true']} NO means: {CRITERIA['false']}"
            " Is this stored event a real, specific cyber incident?")


def judge_events(conn: sqlite3.Connection, dedup_ids: Sequence[str], judge=None,
                 refresh: bool = False, version: str = QUESTION_VERSION) -> Dict[str, float]:
    """P(real incident) for each event; stored answers are reused (see :mod:`jev`).

    ``version`` is :data:`SCREEN_VERSION` (stored evidence only) or
    :data:`QUESTION_VERSION` (plus any stored corroboration search).
    """
    from cyber_data_collector.utils.jev import JevJudge

    owned = judge is None
    judge = judge or JevJudge(conn)
    q = {QUESTION_ID: question(version)}
    full = version == QUESTION_VERSION
    out: Dict[str, float] = {}
    try:
        for i, dedup_id in enumerate(dedup_ids, 1):
            try:
                answer = judge.ask("deduplicated_event", dedup_id, version,
                                   build_state(conn, dedup_id, corroborated=full, with_timeline=full),
                                   q, refresh=refresh)[QUESTION_ID]
            except Exception as exc:  # noqa: BLE001 - one failure never decides an event
                logger.warning("Event reality judgement failed for %s: %s", dedup_id, exc)
                continue
            out[dedup_id] = float(answer.value)
            if i % 50 == 0:
                logger.info("Judged %d/%d events (%s)", i, len(dedup_ids), version)
    finally:
        if owned:
            judge.close()
    return out


def evidence_fingerprint() -> str:
    """A hash of everything that shapes the evidence a reality judgement sees.

    Also the evidence of the duplicate, victim-attribution and supplier judgements, which
    build on :func:`build_state`, so they nest this value in their own.
    """
    return calibration_guard.fingerprint(
        "event-reality", version=EVIDENCE_VERSION, max_records=MAX_RECORDS, page_chars=PAGE_CHARS,
        reasoning_chars=REASONING_CHARS, research_fields=list(RESEARCH_FIELDS),
        corroborate_below=CORROBORATE_BELOW, escalate_band=list(ESCALATE_BAND), **calibration_guard.shared_parts())


def calibration_identity() -> Dict[str, str]:
    """What a calibration must have been measured on to apply: question, escalation, both models, evidence."""
    from cyber_data_collector.utils.llm_models import openai_model
    return {"question_version": f"{QUESTION_VERSION}+{ESCALATION_VERSION}",
            "model": f"{typesafe_model()}+{openai_model()}", "evidence": evidence_fingerprint()}


def escalate_band(conn: sqlite3.Connection, probabilities: Dict[str, float], client=None,
                  failed: Optional[List[str]] = None) -> Dict[str, float]:
    """Re-judge with the reasoning model every event Jev scores inside ESCALATE_BAND.

    Events whose escalation fails are appended to ``failed``: they are left
    undecided (retried next run), never decided on Jev's mid-band score.
    """
    from cyber_data_collector.utils.escalation import escalate

    lo, hi = ESCALATE_BAND
    band = sorted(d for d, p in probabilities.items() if lo <= p < hi)
    out: Dict[str, float] = {}
    for i, did in enumerate(band, 1):
        value = escalate(conn, "deduplicated_event", did, ESCALATION_QUESTION_ID, ESCALATION_VERSION,
                         build_state(conn, did, corroborated=True, with_timeline=True),
                         escalation_question(), client=client)
        if value is not None:
            out[did] = value
        elif failed is not None:
            failed.append(did)
        if i % 10 == 0:
            logger.info("Escalated %d/%d mid-band events", i, len(band))
    logger.info("Escalated %d of %d events Jev scored in %s", len(out), len(band), ESCALATE_BAND)
    return out


STEP = final_decisions.Step(
    name="event-reality", question_id=QUESTION_ID, version=QUESTION_VERSION, make_question=question,
    escalation_id=ESCALATION_QUESTION_ID, escalation_version=ESCALATION_VERSION,
    escalation_text=escalation_question, band=ESCALATE_BAND)


def final_state(conn: sqlite3.Connection, dedup_id: str) -> Dict[str, Any]:
    """The evidence an event's final judgement (Jev and the escalation alike) is made on."""
    return build_state(conn, dedup_id, corroborated=True, with_timeline=True)


def record_final(conn: sqlite3.Connection, jev: Dict[str, float], escalated: Dict[str, float],
                 failed: Optional[Sequence[str]] = None) -> Dict[str, float]:
    """Store each scored event's final decision against the evidence both answers were given; returns the final
    scores (an event whose escalation failed has none)."""
    return final_decisions.record_scores(conn, STEP, [(d, lambda d=d: final_state(conn, d)) for d in jev],
                                         jev, escalated, failed or ())


def stored_final_probabilities(conn: sqlite3.Connection, ids: Sequence[str]) -> Dict[str, float]:
    """The final score per event from its decision on the evidence it has **now** (:mod:`final_decisions`).

    It used to be the newest Jev answer and the newest escalation, each chosen alone, so an escalation given
    before the event's evidence changed could be combined with a fresh Jev answer. An event with no decision on
    its current evidence has no score here: it is judged (``check-events``) first."""
    return final_decisions.read_current(conn, STEP, [(d, lambda d=d: final_state(conn, d)) for d in ids])


def corroborate_low(conn: sqlite3.Connection, screen: Dict[str, float], search=None) -> int:
    """Run (or reuse) a corroboration search for every event the screen scores low."""
    from cyber_data_collector.dedup.corroboration import corroborate

    low = [d for d, p in screen.items() if p < CORROBORATE_BELOW]
    done = 0
    for i, dedup_id in enumerate(sorted(low), 1):
        if corroborate(conn, dedup_id, _evidence_state(conn, dedup_id), search=search) is not None:
            done += 1
        if i % 25 == 0:
            logger.info("Corroborated %d/%d low-scoring events", i, len(low))
    logger.info("%d of %d low-scoring events have a corroboration search", done, len(low))
    return done


def active_events(conn: sqlite3.Connection) -> List[str]:
    return [r[0] for r in conn.execute(
        "SELECT deduplicated_event_id FROM DeduplicatedEvents WHERE status = 'Active' "
        "ORDER BY deduplicated_event_id")]


def active_calibration(path: Path = CALIBRATION_PATH) -> Optional[Dict[str, Any]]:
    """The calibration, only if it was measured on the current model, question and evidence processing."""
    try:
        cal = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.error("Event reality calibration unreadable (%s): %s - REPORT ONLY, nothing will be acted on",
                     path, exc)
        return None
    reason = calibration_guard.mismatch(cal, calibration_identity())
    if reason:
        logger.warning(calibration_guard.report_only_message("event reality", cal, reason))
        return None
    if cal.get("cutoff") is None:
        return None
    return cal


def protected_events(path: Path = LABELS_PATH) -> set:
    """Events the labelled set says are real: never rejected, whatever Jev scores.

    The labels are ground truth reviewed by people (or by labellers whose
    reasons are recorded for review); a calibrated filter that still errs on a
    known event should not be allowed to delete it. 2026-10-27: an NBN Co
    infrastructure-data sale claim was rejected at 0.20 although labelled real,
    and no other event recorded it.
    """
    import csv
    try:
        with Path(path).open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, ValueError) as exc:
        # Without the labels nothing protects known incidents: refuse to act.
        raise RuntimeError(f"event reality labels unreadable ({path}): {exc}; refusing to reject") from exc
    if not rows:
        raise RuntimeError(f"event reality labels empty ({path}); refusing to reject")
    return {r["deduplicated_event_id"] for r in rows if r.get("label") == "real"}


LABELLED_NON_INCIDENT_MAX_P = 0.5


def labelled_non_incidents(path: Path = LABELS_PATH) -> set:
    """Events the labelled set says are not incidents (ground truth, like ``protected_events``)."""
    import csv
    with Path(path).open(encoding="utf-8") as fh:
        return {r["deduplicated_event_id"] for r in csv.DictReader(fh) if r.get("label") == "not_incident"}


def labelled_out_of_scope(path: Path = LABELS_PATH) -> set:
    """Events labelled real incidents outside the dataset's scope (not Australian).

    Rejected whatever Jev scores - the question asks whether an incident is
    real, not whether it belongs here - and left out of calibration, where
    they would count as real (NHS Dumfries and Galloway, 2026-10-28)."""
    import csv
    with Path(path).open(encoding="utf-8") as fh:
        return {r["deduplicated_event_id"] for r in csv.DictReader(fh) if r.get("label") == "out_of_scope"}


def labelling_snapshot(conn: sqlite3.Connection, cal: Optional[Dict[str, Any]],
                       snapshot_path: Optional[Path] = None) -> Optional[Path]:
    """The database the labels were made on (``calibrated_on_database``, beside
    the live database), or None when it cannot be found."""
    if snapshot_path is None:
        name = (cal or {}).get("calibrated_on_database")
        row = conn.execute("PRAGMA database_list").fetchone()
        if not name or not row or not row[2]:
            return None
        snapshot_path = Path(row[2]).parent / name
    return Path(snapshot_path) if Path(snapshot_path).is_file() else None


def gained_since_labelling(conn: sqlite3.Connection, event_ids: Sequence[str],
                           snapshot: Optional[Path]) -> Optional[set]:
    """Events that hold a record they did not hold when labelled; None when the
    labelling snapshot is unavailable (then no event can be shown unchanged)."""
    if snapshot is None or not event_ids:
        return None if snapshot is None else set()
    snap = sqlite3.connect(f"file:{Path(snapshot).as_posix()}?mode=ro", uri=True)
    try:
        query = "SELECT enriched_event_id FROM EventDeduplicationMap WHERE deduplicated_event_id = ?"
        return {did for did in event_ids
                if {r[0] for r in conn.execute(query, (did,))} - {r[0] for r in snap.execute(query, (did,))}}
    except sqlite3.Error as exc:
        logger.error("Labelling snapshot %s unreadable (%s): labelled non-incidents held for review", snapshot, exc)
        return None
    finally:
        snap.close()


def restore_protected(conn: sqlite3.Connection, protected: set,
                      batch_prefix: str = "reject-event-reality") -> List[str]:
    """Undo a filter's rejection of any protected event (latest snapshot of its ``batch_prefix`` batches)."""
    from cyber_data_collector.dedup.ledger import DedupLedger

    if not protected or not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'DedupSnapshots'").fetchone():
        return []
    ledger = DedupLedger(conn)
    restored = []
    for did in sorted(protected):
        snap = conn.execute(
            """SELECT s.snapshot_id FROM DedupSnapshots s
               JOIN DeduplicatedEvents d ON d.deduplicated_event_id = s.deduplicated_event_id
               WHERE s.deduplicated_event_id = ? AND d.status = 'Rejected'
                 AND s.batch_id LIKE ?
               ORDER BY s.batch_id DESC LIMIT 1""", (did, f"{batch_prefix}-%")).fetchone()
        if snap is None:
            continue
        ledger.restore_snapshot(snap[0])
        conn.execute(
            "UPDATE EnrichedEvents SET status = 'Active', updated_at = ? WHERE enriched_event_id IN "
            "(SELECT enriched_event_id FROM EventDeduplicationMap WHERE deduplicated_event_id = ?)",
            (datetime.now(), did))
        restored.append(did)
    conn.commit()
    if restored:
        logger.info("Restored %d labelled event(s) this filter had rejected", len(restored))
    return restored


def reject_events(conn: sqlite3.Connection, findings: Sequence[Dict[str, Any]],
                  batch_prefix: str = "reject-event-reality", reason: str = "judged not to be incidents") -> None:
    """Reject events (by default those judged not to be incidents); snapshotted, so reversible."""
    from cyber_data_collector.dedup.ledger import DedupLedger

    ledger = DedupLedger(conn)
    batch_id = f"{batch_prefix}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    now = datetime.now()
    try:
        for f in findings:
            did = f["deduplicated_event_id"]
            ledger.snapshot_event(batch_id, did)
            conn.execute("UPDATE DeduplicatedEvents SET status = 'Rejected', updated_at = ? "
                         "WHERE deduplicated_event_id = ?", (now, did))
            conn.execute(
                "UPDATE EnrichedEvents SET status = 'Inactive', updated_at = ? WHERE enriched_event_id IN "
                "(SELECT enriched_event_id FROM EventDeduplicationMap WHERE deduplicated_event_id = ?)",
                (now, did))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    logger.info("Rejected %d event(s) %s (batch %s)", len(findings), reason, batch_id)


def check_events(conn: sqlite3.Connection, apply: bool = False, judge=None,
                 calibration_path: Path = CALIBRATION_PATH, search=None,
                 labels_path: Path = LABELS_PATH, escalation_client=None,
                 timeline_search=None, snapshot_path: Optional[Path] = None) -> Dict[str, Any]:
    """Screen, corroborate the low scorers, research timelines, judge, escalate the
    middle band, and reject below the calibrated cut-off.

    Without an active calibration, or without ``apply``, nothing is rejected:
    the would-be rejections are returned for review.
    """
    from cyber_data_collector.dedup.org_timeline import research_all

    # Labels decide first: an event the labels say is real is never rejected, whatever it scores, so it is not
    # (re-)asked about. Read first, so unreadable labels stop the run before anything is spent.
    protected = protected_events(labels_path) if apply else set()
    events = [d for d in active_events(conn) if d not in protected]
    screen = judge_events(conn, events, judge=judge, version=SCREEN_VERSION)
    corroborated = corroborate_low(conn, screen, search=search)
    research_all(conn, search=timeline_search)
    jev_scores = judge_events(conn, events, judge=judge, version=QUESTION_VERSION)
    escalation_failed: List[str] = []
    escalated = escalate_band(conn, jev_scores, client=escalation_client, failed=escalation_failed)
    probabilities = record_final(conn, jev_scores, escalated, escalation_failed)
    cal = active_calibration(calibration_path)
    cutoff = cal["cutoff"] if cal else None
    titles = dict(conn.execute("SELECT deduplicated_event_id, title FROM DeduplicatedEvents"))
    below = [] if cutoff is None else [
        {"deduplicated_event_id": did, "title": titles.get(did), "p_real": p}
        for did, p in sorted(probabilities.items(), key=lambda kv: kv[1])
        if p < cutoff and did not in protected]
    # Labels are ground truth both ways: a labelled non-incident is rejected
    # even above the cut-off. The one exception is an event holding a record it
    # did not hold when labelled (a merge into it may have brought in a real
    # incident): if it now scores as real it is reported for review instead.
    # Without the labelling snapshot no event can be shown unchanged, so the
    # model's lean decides as before.
    labelled_out: List[Dict[str, Any]] = []
    needs_review: List[Dict[str, Any]] = []
    if cutoff is not None and apply:
        already = {f["deduplicated_event_id"] for f in below}
        candidates = sorted(labelled_non_incidents(labels_path) & set(probabilities) - already - protected)
        gained = gained_since_labelling(conn, candidates, labelling_snapshot(conn, cal, snapshot_path))
        for did in candidates:
            entry = {"deduplicated_event_id": did, "title": titles.get(did), "p_real": probabilities[did],
                     "label": "not_incident"}
            unchanged = gained is not None and did not in gained
            (labelled_out if unchanged or probabilities[did] < LABELLED_NON_INCIDENT_MAX_P
             else needs_review).append(entry)
        for did in sorted(labelled_out_of_scope(labels_path) & set(probabilities) - already - protected):
            labelled_out.append({"deduplicated_event_id": did, "title": titles.get(did),
                                 "p_real": probabilities[did], "label": "out_of_scope"})
        below = below + labelled_out
    applied = bool(apply and cal and below)
    if applied:
        reject_events(conn, below)
    restored = restore_protected(conn, protected) if apply else []
    return {"judged": len(probabilities), "settled_by_label": len(protected), "corroborated": corroborated, "escalated": len(escalated),
            "escalation_failed": escalation_failed, "cutoff": cutoff,
            "labelled_rejections": labelled_out, "labelled_needing_review": needs_review,
            "calibrated": cal is not None, "applied": applied, "rejections": below,
            "restored_protected": restored}
