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
# v1 sees the stored evidence only. Calibrated 2026-10-27 it ranked well
# (weighted AUC 0.957) but real incidents with thin stored evidence scored low,
# so no cut-off reached the precision target. It is now the *screen*: events
# it scores below CORROBORATE_BELOW get a web search (see corroboration), and
# v2 - the same evidence plus that search - is the question calibrated and
# used to reject.
SCREEN_VERSION = "event-reality-v1"
QUESTION_VERSION = "event-reality-v2-corroborated"
CORROBORATE_BELOW = 0.5
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
CORROBORATION_INSTRUCTIONS = (
    " `corroboration`, when present, is a fresh web search for public reporting of this "
    "incident (`answer` and `citations`). Credible reporting found there is strong evidence for "
    "the incident even when the stored pages are unhelpful. If the search found nothing and the "
    "records hold no concrete evidence either, that is evidence against it. A search that found a "
    "*different* incident at the same organisation is not evidence for this one."
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


def build_state(conn: sqlite3.Connection, dedup_id: str, corroborated: bool = False,
                max_records: int = MAX_RECORDS) -> Dict[str, Any]:
    """Everything Jev sees about one event. Deterministic, so the input hash is stable.

    With ``corroborated``, a stored web search for the event is added when one
    exists.
    """
    state = _evidence_state(conn, dedup_id, max_records)
    if corroborated:
        from cyber_data_collector.dedup.corroboration import stored
        found = stored(conn, dedup_id)
        if found:
            state["corroboration"] = {"answer": found["answer"], "citations": found["citations"]}
    return state


def _evidence_state(conn: sqlite3.Connection, dedup_id: str, max_records: int = MAX_RECORDS) -> Dict[str, Any]:
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
                "perplexity": _perplexity(r[4])} for r in rows[:max_records]]
    return {
        "event": {"title": event[0] if event else "", "date": event[1] if event else None,
                  "victim": event[2] if event else None, "record_count": len(rows)},
        "records": records,
    }


def question(version: str = QUESTION_VERSION):
    from typesafe_sdk import Noul, NoulCriteria
    instructions = INSTRUCTIONS + (CORROBORATION_INSTRUCTIONS if version == QUESTION_VERSION else "")
    return Noul(instructions=instructions, criteria=NoulCriteria(**CRITERIA))


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
    corroborated = version == QUESTION_VERSION
    out: Dict[str, float] = {}
    try:
        for i, dedup_id in enumerate(dedup_ids, 1):
            try:
                answer = judge.ask("deduplicated_event", dedup_id, version,
                                   build_state(conn, dedup_id, corroborated), q, refresh=refresh)[QUESTION_ID]
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
            return {r["deduplicated_event_id"] for r in csv.DictReader(fh) if r.get("label") == "real"}
    except OSError:
        return set()


def restore_protected(conn: sqlite3.Connection, protected: set) -> List[str]:
    """Undo this filter's rejection of any protected event (latest snapshot)."""
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
                 AND s.batch_id LIKE 'reject-event-reality-%'
               ORDER BY s.batch_id DESC LIMIT 1""", (did,)).fetchone()
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
        logger.info("Restored %d labelled-real event(s) this filter had rejected", len(restored))
    return restored


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
                 calibration_path: Path = CALIBRATION_PATH, search=None,
                 labels_path: Path = LABELS_PATH) -> Dict[str, Any]:
    """Screen, corroborate the low scorers, judge again, reject below the calibrated cut-off.

    Without an active calibration, or without ``apply``, nothing is rejected:
    the would-be rejections are returned for review.
    """
    events = active_events(conn)
    screen = judge_events(conn, events, judge=judge, version=SCREEN_VERSION)
    corroborated = corroborate_low(conn, screen, search=search)
    probabilities = judge_events(conn, events, judge=judge, version=QUESTION_VERSION)
    cal = active_calibration(calibration_path)
    cutoff = cal["cutoff"] if cal else None
    titles = dict(conn.execute("SELECT deduplicated_event_id, title FROM DeduplicatedEvents"))
    protected = protected_events(labels_path)
    below = [] if cutoff is None else [
        {"deduplicated_event_id": did, "title": titles.get(did), "p_real": p}
        for did, p in sorted(probabilities.items(), key=lambda kv: kv[1])
        if p < cutoff and did not in protected]
    applied = bool(apply and cal and below)
    if applied:
        reject_events(conn, below)
    restored = restore_protected(conn, protected) if apply else []
    return {"judged": len(probabilities), "corroborated": corroborated, "cutoff": cutoff,
            "calibrated": cal is not None, "applied": applied, "rejections": below,
            "restored_protected": restored}
