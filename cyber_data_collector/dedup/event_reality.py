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
returns it only while both still match, so changing the question or the model
turns rejection back into a report until it is recalibrated
(``scripts/calibrate_event_reality.py``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from cyber_data_collector.utils.llm_models import typesafe_model

logger = logging.getLogger(__name__)

QUESTION_ID = "is_real_incident"
QUESTION_VERSION = "event-reality-v1"
MAX_RECORDS = 10
PAGE_CHARS = 1200
REASONING_CHARS = 800

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
    keep = ("formal_entity_name", "overall_confidence", "earliest_event_date", "attack_method",
            "threat_actor", "victim_count", "sources_consulted")
    out = {k: data.get(k) for k in keep if data.get(k) not in (None, "", [], {})}
    if data.get("reasoning"):
        out["reasoning"] = str(data["reasoning"])[:REASONING_CHARS]
    return out


def build_state(conn: sqlite3.Connection, dedup_id: str) -> Dict[str, Any]:
    """Everything Jev sees about one event. Deterministic, so the input hash is stable."""
    event = conn.execute(
        "SELECT title, event_date, victim_organization_name FROM DeduplicatedEvents "
        "WHERE deduplicated_event_id = ?", (dedup_id,)).fetchone()
    rows = conn.execute(
        """SELECT e.enriched_event_id, COALESCE(e.title, ''), COALESCE(r.source_url, ''),
                  COALESCE(NULLIF(r.raw_content, ''), r.raw_description, ''),
                  e.perplexity_enrichment_data
           FROM EventDeduplicationMap m
           JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
           LEFT JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
           WHERE m.deduplicated_event_id = ?""", (dedup_id,)).fetchall()
    # Records with page text first, then a stable order, so a large event
    # always shows the same ten.
    rows = sorted(rows, key=lambda r: (not r[3], r[0]))
    records = [{"title": r[1], "url": r[2], "page_text": (r[3] or "")[:PAGE_CHARS] or "(none stored)",
                "perplexity": _perplexity(r[4])} for r in rows[:MAX_RECORDS]]
    return {
        "event": {"title": event[0] if event else "", "date": event[1] if event else None,
                  "victim": event[2] if event else None, "record_count": len(rows)},
        "records": records,
    }


def question():
    from typesafe_sdk import Noul, NoulCriteria
    return Noul(instructions=INSTRUCTIONS, criteria=NoulCriteria(**CRITERIA))


def judge_events(conn: sqlite3.Connection, dedup_ids: Sequence[str], judge=None,
                 refresh: bool = False) -> Dict[str, float]:
    """P(real incident) for each event; stored answers are reused (see :mod:`jev`)."""
    from cyber_data_collector.utils.jev import JevJudge

    owned = judge is None
    judge = judge or JevJudge(conn)
    q = {QUESTION_ID: question()}
    out: Dict[str, float] = {}
    try:
        for i, dedup_id in enumerate(dedup_ids, 1):
            try:
                answer = judge.ask("deduplicated_event", dedup_id, QUESTION_VERSION,
                                   build_state(conn, dedup_id), q, refresh=refresh)[QUESTION_ID]
            except Exception as exc:  # noqa: BLE001 - one failure never decides an event
                logger.warning("Event reality judgement failed for %s: %s", dedup_id, exc)
                continue
            out[dedup_id] = float(answer.value)
            if i % 50 == 0:
                logger.info("Judged %d/%d events", i, len(dedup_ids))
    finally:
        if owned:
            judge.close()
    return out


def active_events(conn: sqlite3.Connection) -> List[str]:
    return [r[0] for r in conn.execute(
        "SELECT deduplicated_event_id FROM DeduplicatedEvents WHERE status = 'Active' "
        "ORDER BY deduplicated_event_id")]


def active_calibration(path: Path = CALIBRATION_PATH) -> Optional[Dict[str, Any]]:
    """The calibration, only if it was measured on the current model and question."""
    try:
        cal = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if cal.get("question_version") != QUESTION_VERSION or cal.get("model") != typesafe_model():
        logger.warning("Event reality calibration is for %s / %s, not %s / %s: report only",
                       cal.get("model"), cal.get("question_version"), typesafe_model(), QUESTION_VERSION)
        return None
    if cal.get("cutoff") is None:
        return None
    return cal


def reject_events(conn: sqlite3.Connection, findings: Sequence[Dict[str, Any]]) -> None:
    """Reject events judged not to be incidents; snapshotted, so reversible."""
    from cyber_data_collector.dedup.ledger import DedupLedger

    ledger = DedupLedger(conn)
    batch_id = f"reject-event-reality-{datetime.now().strftime('%Y%m%d%H%M%S')}"
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
    logger.info("Rejected %d event(s) judged not to be incidents (batch %s)", len(findings), batch_id)


def check_events(conn: sqlite3.Connection, apply: bool = False, judge=None,
                 calibration_path: Path = CALIBRATION_PATH) -> Dict[str, Any]:
    """Judge every active event and reject those below the calibrated cut-off.

    Without an active calibration, or without ``apply``, nothing is rejected:
    the would-be rejections are returned for review.
    """
    probabilities = judge_events(conn, active_events(conn), judge=judge)
    cal = active_calibration(calibration_path)
    cutoff = cal["cutoff"] if cal else None
    titles = dict(conn.execute("SELECT deduplicated_event_id, title FROM DeduplicatedEvents"))
    below = [] if cutoff is None else [
        {"deduplicated_event_id": did, "title": titles.get(did), "p_real": p}
        for did, p in sorted(probabilities.items(), key=lambda kv: kv[1]) if p < cutoff]
    applied = bool(apply and cal and below)
    if applied:
        reject_events(conn, below)
    return {"judged": len(probabilities), "cutoff": cutoff, "calibrated": cal is not None,
            "applied": applied, "rejections": below}
