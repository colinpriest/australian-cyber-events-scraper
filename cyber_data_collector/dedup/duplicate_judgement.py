"""Are two events about one organisation the same incident? Jev, with a calibrated merge cut-off.

Every pair of active events that share a victim is a candidate (159 on
2026-10-27, across the 54 organisations with more than one event). Jev sees
both events' evidence - each record's page text, its Perplexity research, and
any stored corroboration search (:mod:`corroboration`) - plus the gap in days,
and answers the probability that they are one real-world incident. Answers are
stored in ``ModelJudgements`` (``subject_type='event_pair'``).

Why it is needed: the GPT recurrence check (:mod:`recurrence_check`) merges
only at certainty 0.85, a bar nobody measured, and on 2026-10-27 it left an
agency's own web pages filed as second "attacks" beside its real incident
(ACT Government at 0.55, Spectrum Medical Imaging at 0.78) because the pages
themselves are vague. The corroboration searches already stored for those
pages name the real incident; this question sees them.

A merge needs a **calibrated** cut-off: ``scripts/calibrate_duplicates.py``
fixes the lowest P(same) at which merged pairs are, in the labelled set, at
least 95% truly the same incident, and records the Jev model and question
version it was measured on. Without a matching calibration nothing merges.
Labelled pairs are ground truth either way: ``different`` is never merged,
``same`` always is.
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from cyber_data_collector.dedup.event_reality import ROOT, build_state
from cyber_data_collector.utils.llm_models import typesafe_model

logger = logging.getLogger(__name__)

QUESTION_ID = "same_incident"
# v1 (never calibrated) lacked date provenance; v2 shows how each date was
# determined and how precise it is, because a gap built from publication dates
# or month-only placeholders is weak evidence of separate incidents.
# v2 calibrated at 0.75 (9/9 merges correct, recall 0.53). v3 adds the
# organisation's researched incident timeline, which is what decided the hard
# labelled cases (Western Sydney University's four separate incidents); pairs
# v3 scores inside ESCALATE_BAND - fixed before calibration - are re-judged by
# the reasoning model, and the final score is calibrated.
QUESTION_VERSION = "same-incident-v3-timeline"
ESCALATION_QUESTION_ID = "same_incident_escalated"
ESCALATION_VERSION = "same-incident-escalation-v1"
ESCALATE_BAND = (0.2, 0.8)
RECORDS_PER_EVENT = 6

LABELS_PATH = ROOT / "evaluation" / "duplicate_labels.csv"
CALIBRATION_PATH = ROOT / "evaluation" / "duplicate_calibration.json"

INSTRUCTIONS = (
    "`event_a` and `event_b` are two stored events recorded against the same organisation, "
    "`organisation`, `gap_days` apart. Decide whether they describe the SAME real-world cyber "
    "incident. Each event's `records` hold the page actually stored (often the organisation's own "
    "notice, privacy policy or an unrelated page) and separate Perplexity research; `corroboration`, "
    "when present, is a web search for public reporting of that event. Each record's `date` says "
    "how its date was determined (`basis`: stated in the record, Perplexity research, the "
    "publication date, or the date the record was found) and how precise it is (`precision`: day, "
    "month only, year only); each event gives its `date_precision`, how many of its records carry "
    "its date (none means a later repair pass set it), and the bases of its records' dates. A gap "
    "built from publication or found dates, from month- or year-only dates stored as the 1st, or "
    "from an event date no record carries, is weak evidence of separate incidents; a gap between "
    "two dates stated to the day in the records is strong evidence. `organisation_timeline`, when "
    "present, is researched history of the organisation's publicly reported incidents with dates "
    "and sources: if both events match the same incident in it they are one incident; if they "
    "match two different incidents in it they are two."
)
CRITERIA = {
    "true": ("One incident: the same breach or attack reported again - follow-up coverage, a later "
             "notification or regulator statement, a class action, an aggregator entry, or an "
             "organisation's own web page or notice that the evidence ties to that incident; also "
             "one supplier's breach recorded twice for the same customer."),
    "false": ("Two incidents: a separate attack or breach, a different time with no link between "
              "them, a new compromise after the first was resolved, or a different attacker or "
              "method - even when both are the same kind of incident at the same organisation."),
}


def pair_id(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


#: ``gap_days`` shown to Jev when either event has no date.
GAP_UNKNOWN = "unknown - one or both events have no date"


def candidate_pairs(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Every pair of active events sharing a victim, earliest first within each pair.

    Undated events are paired too: until 2026-10-28 they were left out, so 35
    undated reports of the OpenAI agent's Medicare incident were never
    compared with each other or with the dated event.

    "Sharing a victim" uses the organisation identity the recurrence analysis
    and the entity dashboard count by (:func:`victim_counting.organisation_groups`),
    not the entity row: until 2026-10-29 "ALS Global" / "ALS" and "St Vincent's
    Health Australia" / "St Vincent's Health" were one organisation in the
    analysis but never a candidate pair here, so a leak-site claim and the
    victim's disclosure days later counted as a repeat attack.
    """
    from cyber_data_collector.dedup.recurrence_check import load_recurrence_events
    from cyber_data_collector.dedup.victim_counting import organisation_groups

    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        events = load_recurrence_events(conn, include_undated=True)
    finally:
        conn.row_factory = previous
    groups = organisation_groups(conn, [e.entity_id for e in events])
    by_entity = defaultdict(list)
    for e in events:
        by_entity[groups.get(e.entity_id, e.entity_id)].append(e)
    pairs: Dict[str, Dict[str, Any]] = {}
    for es in by_entity.values():
        es = sorted(es, key=lambda e: (e.event_date is None, e.event_date or date.max, e.dedup_id))
        for i, a in enumerate(es):
            for b in es[i + 1:]:
                if a.dedup_id == b.dedup_id:
                    continue
                pid = pair_id(a.dedup_id, b.dedup_id)
                pairs.setdefault(pid, {"pair_id": pid, "a": a.dedup_id, "b": b.dedup_id,
                                       "organisation": a.entity_name, "entity_id": a.entity_id,
                                       "gap_days": a.gap_to(b) if a.gap_to(b) is not None else GAP_UNKNOWN})
    return sorted(pairs.values(), key=lambda p: p["pair_id"])


def _event_with_dates(conn: sqlite3.Connection, dedup_id: str) -> Dict[str, Any]:
    """The reality filter's evidence for one event, plus how each date was determined."""
    from cyber_data_collector.dedup.date_provenance import event_dates

    state = build_state(conn, dedup_id, corroborated=True, max_records=RECORDS_PER_EVENT)
    dates = event_dates(conn, dedup_id, RECORDS_PER_EVENT)
    state["event"]["date_precision"] = dates["event_date_precision"]
    state["event"]["records_carrying_event_date"] = dates["records_carrying_event_date"]
    state["event"]["record_date_bases"] = dates["record_date_bases"]
    for record, provenance in zip(state["records"], dates["records"]):
        record["date"] = provenance
    return state


def build_pair_state(conn: sqlite3.Connection, pair: Dict[str, Any]) -> Dict[str, Any]:
    from cyber_data_collector.dedup.org_timeline import for_judgement

    state = {"organisation": pair["organisation"], "gap_days": pair["gap_days"],
             "event_a": _event_with_dates(conn, pair["a"]),
             "event_b": _event_with_dates(conn, pair["b"])}
    timeline = for_judgement(conn, pair["entity_id"]) if pair.get("entity_id") is not None else None
    if timeline:
        # incidents and citations as before; a warning only when the history may be incomplete.
        state["organisation_timeline"] = {k: v for k, v in timeline.items() if k != "organisation"}
    return state


def escalation_question() -> str:
    return (INSTRUCTIONS + f" YES means: {CRITERIA['true']} NO means: {CRITERIA['false']}"
            " Do event_a and event_b describe the same real-world cyber incident?")


def calibration_identity() -> Dict[str, str]:
    from cyber_data_collector.utils.llm_models import openai_model
    return {"question_version": f"{QUESTION_VERSION}+{ESCALATION_VERSION}",
            "model": f"{typesafe_model()}+{openai_model()}"}


def escalate_band(conn: sqlite3.Connection, pairs: Sequence[Dict[str, Any]], probabilities: Dict[str, float],
                  client=None, failed: Optional[List[str]] = None) -> Dict[str, float]:
    """Re-judge with the reasoning model every pair Jev scores inside ESCALATE_BAND.

    Pairs whose escalation fails are appended to ``failed`` and left undecided
    (retried next run) - never merged on Jev's mid-band score.
    """
    from cyber_data_collector.utils.escalation import escalate

    lo, hi = ESCALATE_BAND
    out: Dict[str, float] = {}
    for pair in pairs:
        p = probabilities.get(pair["pair_id"])
        if p is None or not lo <= p < hi:
            continue
        value = escalate(conn, "event_pair", pair["pair_id"], ESCALATION_QUESTION_ID, ESCALATION_VERSION,
                         build_pair_state(conn, pair), escalation_question(), client=client)
        if value is not None:
            out[pair["pair_id"]] = value
        elif failed is not None:
            failed.append(pair["pair_id"])
    logger.info("Escalated %d pair(s) Jev scored in %s", len(out), ESCALATE_BAND)
    return out


def stored_final_probabilities(conn: sqlite3.Connection) -> Dict[str, float]:
    """Final score per pair from storage: the escalation's in the band, else Jev's."""
    from cyber_data_collector.utils.llm_models import openai_model

    def latest(question_id: str, version: str, model: str) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for sid, value in conn.execute(
                "SELECT subject_id, value FROM ModelJudgements WHERE question_id = ? AND question_version = ? "
                "AND model_requested = ? ORDER BY created_at, rowid", (question_id, version, model)):
            out[sid] = float(value)
        return out

    jev = latest(QUESTION_ID, QUESTION_VERSION, typesafe_model())
    escalated = latest(ESCALATION_QUESTION_ID, ESCALATION_VERSION, openai_model())
    lo, hi = ESCALATE_BAND
    return {p: (escalated[p] if p in escalated and lo <= v < hi else v) for p, v in jev.items()}


def question():
    from typesafe_sdk import Noul, NoulCriteria
    return Noul(instructions=INSTRUCTIONS, criteria=NoulCriteria(**CRITERIA))


def judge_pairs(conn: sqlite3.Connection, pairs: Sequence[Dict[str, Any]], judge=None,
                refresh: bool = False) -> Dict[str, float]:
    """P(same incident) per pair id; stored answers are reused."""
    from cyber_data_collector.utils.jev import JevJudge

    owned = judge is None
    judge = judge or JevJudge(conn)
    q = {QUESTION_ID: question()}
    out: Dict[str, float] = {}
    try:
        for i, pair in enumerate(pairs, 1):
            try:
                answer = judge.ask("event_pair", pair["pair_id"], QUESTION_VERSION,
                                   build_pair_state(conn, pair), q, refresh=refresh)[QUESTION_ID]
            except Exception as exc:  # noqa: BLE001 - one failure never merges anything
                logger.warning("Duplicate judgement failed for %s: %s", pair["pair_id"], exc)
                continue
            out[pair["pair_id"]] = float(answer.value)
            if i % 25 == 0:
                logger.info("Judged %d/%d pairs", i, len(pairs))
    finally:
        if owned:
            judge.close()
    return out


def labelled_pairs(path: Path = LABELS_PATH) -> Dict[str, str]:
    """Labelled pairs. Unreadable or empty labels raise: without them a pair
    labelled 'different' could be merged, so merging must not proceed."""
    try:
        with Path(path).open(encoding="utf-8") as fh:
            labels = {r["pair_id"]: r["label"] for r in csv.DictReader(fh)}
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError(f"duplicate labels unreadable ({path}): {exc}; refusing to merge") from exc
    if not labels:
        raise RuntimeError(f"duplicate labels empty ({path}); refusing to merge")
    return labels


def active_calibration(path: Path = CALIBRATION_PATH) -> Optional[Dict[str, Any]]:
    """The merge calibration, only if measured on the current model and question."""
    try:
        cal = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.error("Duplicate calibration unreadable (%s): %s - REPORT ONLY, nothing will be acted on",
                     path, exc)
        return None
    want = calibration_identity()
    if cal.get("question_version") != want["question_version"] or cal.get("model") != want["model"]:
        logger.warning("Duplicate calibration is for %s / %s, not %s / %s: report only",
                       cal.get("model"), cal.get("question_version"), want["model"], want["question_version"])
        return None
    return cal if cal.get("cutoff") is not None else None


def _components(edges: Sequence[Tuple[str, str]]) -> List[List[str]]:
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        parent[find(a)] = find(b)
    groups: Dict[str, List[str]] = defaultdict(list)
    for x in list(parent):
        groups[find(x)].append(x)
    return [sorted(g) for g in groups.values() if len(g) > 1]


def merge_components(conn: sqlite3.Connection, components: Sequence[Sequence[str]],
                     different: set) -> List[Dict[str, Any]]:
    """Fold each component into its largest event; never across a labelled-different pair."""
    from cyber_data_collector.dedup.ledger import DedupLedger

    ledger = DedupLedger(conn)
    merged = []
    for group in components:
        sizes = {d: conn.execute("SELECT COUNT(*) FROM EventDeduplicationMap WHERE deduplicated_event_id = ?",
                                 (d,)).fetchone()[0] for d in group}
        target = max(group, key=lambda d: (sizes[d], d))
        kept = [target]
        for source in sorted(d for d in group if d != target):
            if any(pair_id(source, k) in different for k in kept):
                continue
            try:
                ledger.merge_events(target, source, reason=f"Jev {QUESTION_VERSION}: same incident "
                                                           "above the calibrated cut-off", actor="rule")
            except ValueError as exc:  # a cached 'different' ruling wins
                logger.info("Merge %s -> %s refused: %s", source, target, exc)
                continue
            kept.append(source)
            merged.append({"target": target, "source": source})
    conn.commit()
    return merged


def check_duplicates(conn: sqlite3.Connection, apply: bool = False, judge=None,
                     calibration_path: Path = CALIBRATION_PATH,
                     labels_path: Path = LABELS_PATH, escalation_client=None,
                     timeline_search=None) -> Dict[str, Any]:
    """Judge every same-victim pair (escalating the middle band); merge at or above the calibrated cut-off."""
    from cyber_data_collector.dedup.org_timeline import research_all

    research_all(conn, search=timeline_search)
    pairs = candidate_pairs(conn)
    jev_scores = judge_pairs(conn, pairs, judge=judge)
    escalation_failed: List[str] = []
    escalated = escalate_band(conn, pairs, jev_scores, client=escalation_client, failed=escalation_failed)
    probabilities = {p: escalated.get(p, v) for p, v in jev_scores.items() if p not in escalation_failed}
    labels = labelled_pairs(labels_path)
    different = {p for p, label in labels.items() if label == "different"}
    cal = active_calibration(calibration_path)
    cutoff = cal["cutoff"] if cal else None
    by_id = {p["pair_id"]: p for p in pairs}
    chosen = [] if cutoff is None else [
        pid for pid, p in probabilities.items()
        if (p >= cutoff and pid not in different) or labels.get(pid) == "same"]
    proposals = [{**by_id[pid], "p_same": probabilities.get(pid), "label": labels.get(pid)}
                 for pid in sorted(chosen, key=lambda x: -(probabilities.get(x) or 0))]
    merged = []
    if apply and cal and chosen:
        merged = merge_components(conn, _components([(by_id[p]["a"], by_id[p]["b"]) for p in chosen]),
                                  different)
    return {"pairs": len(pairs), "judged": len(probabilities), "escalated": len(escalated),
            "escalation_failed": escalation_failed, "cutoff": cutoff,
            "calibrated": cal is not None, "proposals": proposals, "merged": merged}
