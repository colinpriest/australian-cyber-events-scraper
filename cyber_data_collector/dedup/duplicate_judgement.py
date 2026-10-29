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
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from cyber_data_collector.dedup import calibration_guard
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
#: Bump when what a pair's events show a model changes beyond event reality's evidence
#: (``event_reality.EVIDENCE_VERSION``) and the date provenance (``date_provenance.EVIDENCE_VERSION``).
#: v2: each record's date provenance is joined to it by id (v1 joined two independently ordered lists by position).
EVIDENCE_VERSION = "duplicate-evidence-v2"

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


def candidate_pairs(conn: sqlite3.Connection, skip: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    """Every pair of active events sharing a victim, earliest first within each pair.

    ``skip`` holds pair ids another route owns and asks itself: an AI-involved
    pair within the window belongs to :mod:`ai_duplicates` (one question per
    pair, the one that knows how rare AI incidents are), so it is not asked here
    as well. The calibration and recalibration of this route ask for every pair.

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

    skipped = set(skip or ())
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
                if pid in skipped:
                    continue
                pairs.setdefault(pid, {"pair_id": pid, "a": a.dedup_id, "b": b.dedup_id,
                                       "organisation": a.entity_name, "entity_id": a.entity_id,
                                       "group": groups.get(a.entity_id, a.entity_id),
                                       "gap_days": a.gap_to(b) if a.gap_to(b) is not None else GAP_UNKNOWN})
    return sorted(pairs.values(), key=lambda p: p["pair_id"])


def _event_with_dates(conn: sqlite3.Connection, dedup_id: str) -> Dict[str, Any]:
    """The reality filter's evidence for one event, plus how each date was determined.

    Each record's date provenance is attached **by the record's id**: the evidence and
    the provenance order their records by separate queries, so position says nothing
    (joined by position, a record was shown with another record's date and basis -
    331 of 1,190 displayed records on a 407-event sample, 2026-10-29). A displayed
    record with no provenance is an error, not a blank.
    """
    from cyber_data_collector.dedup.date_provenance import event_dates

    state = build_state(conn, dedup_id, corroborated=True, max_records=RECORDS_PER_EVENT, with_record_ids=True)
    dates = event_dates(conn, dedup_id, 10 ** 6)
    state["event"]["date_precision"] = dates["event_date_precision"]
    state["event"]["records_carrying_event_date"] = dates["records_carrying_event_date"]
    state["event"]["record_date_bases"] = dates["record_date_bases"]
    by_id = {r["enriched_event_id"]: {k: v for k, v in r.items() if k != "enriched_event_id"}
             for r in dates["records"]}
    for record in state["records"]:
        record_id = record.pop("enriched_event_id")
        if record_id not in by_id:
            raise ValueError(f"record {record_id} of event {dedup_id} has no date provenance")
        record["date"] = by_id[record_id]
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


def evidence_fingerprint() -> str:
    """A hash of the evidence a pair judgement sees: each event's reality evidence plus date provenance."""
    from cyber_data_collector.dedup import date_provenance, event_reality

    return calibration_guard.fingerprint(
        "duplicates", version=EVIDENCE_VERSION, records_per_event=RECORDS_PER_EVENT,
        escalate_band=list(ESCALATE_BAND), date_provenance=date_provenance.EVIDENCE_VERSION,
        events=event_reality.evidence_fingerprint())


def calibration_identity() -> Dict[str, str]:
    from cyber_data_collector.utils.llm_models import openai_model
    return {"question_version": f"{QUESTION_VERSION}+{ESCALATION_VERSION}",
            "model": f"{typesafe_model()}+{openai_model()}", "evidence": evidence_fingerprint()}


def escalate_band(conn: sqlite3.Connection, pairs: Sequence[Dict[str, Any]], probabilities: Dict[str, float],
                  client=None, failed: Optional[List[str]] = None, *,
                  question_id: str = ESCALATION_QUESTION_ID, version: str = ESCALATION_VERSION,
                  state_builder: Optional[Callable[[sqlite3.Connection, Dict[str, Any]], Dict[str, Any]]] = None,
                  question_text: Optional[Callable[[], str]] = None,
                  band: Tuple[float, float] = ESCALATE_BAND) -> Dict[str, float]:
    """Re-judge with the reasoning model every pair Jev scores inside ``band`` (ESCALATE_BAND).

    Pairs whose escalation fails are appended to ``failed`` and left undecided
    (retried next run) - never merged on Jev's mid-band score. The keyword
    arguments let the AI-pair route (:mod:`ai_duplicates`) ask its own question
    through the same procedure.
    """
    from cyber_data_collector.utils.escalation import escalate

    lo, hi = band
    build = state_builder or build_pair_state
    text = (question_text or escalation_question)()
    out: Dict[str, float] = {}
    for pair in pairs:
        p = probabilities.get(pair["pair_id"])
        if p is None or not lo <= p < hi:
            continue
        value = escalate(conn, "event_pair", pair["pair_id"], question_id, version,
                         build(conn, pair), text, client=client)
        if value is not None:
            out[pair["pair_id"]] = value
        elif failed is not None:
            failed.append(pair["pair_id"])
    logger.info("Escalated %d pair(s) Jev scored in %s", len(out), band)
    return out


def stored_final_probabilities(conn: sqlite3.Connection, *, question_id: str = QUESTION_ID,
                               version: str = QUESTION_VERSION, escalation_id: str = ESCALATION_QUESTION_ID,
                               escalation_version: str = ESCALATION_VERSION,
                               band: Tuple[float, float] = ESCALATE_BAND) -> Dict[str, float]:
    """Final score per pair from storage: the escalation's in the band, else Jev's."""
    from cyber_data_collector.utils.llm_models import openai_model

    def latest(qid: str, ver: str, model: str) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for sid, value in conn.execute(
                "SELECT subject_id, value FROM ModelJudgements WHERE question_id = ? AND question_version = ? "
                "AND model_requested = ? ORDER BY created_at, rowid", (qid, ver, model)):
            out[sid] = float(value)
        return out

    jev = latest(question_id, version, typesafe_model())
    escalated = latest(escalation_id, escalation_version, openai_model())
    lo, hi = band
    return {p: (escalated[p] if p in escalated and lo <= v < hi else v) for p, v in jev.items()}


def question():
    from typesafe_sdk import Noul, NoulCriteria
    return Noul(instructions=INSTRUCTIONS, criteria=NoulCriteria(**CRITERIA))


def judge_pairs(conn: sqlite3.Connection, pairs: Sequence[Dict[str, Any]], judge=None,
                refresh: bool = False, *, question_id: str = QUESTION_ID, version: str = QUESTION_VERSION,
                make_question: Optional[Callable[[], Any]] = None,
                state_builder: Optional[Callable[[sqlite3.Connection, Dict[str, Any]], Dict[str, Any]]] = None
                ) -> Dict[str, float]:
    """P(same incident) per pair id; stored answers are reused.

    The keyword arguments let the AI-pair route (:mod:`ai_duplicates`) ask its
    own question through the same procedure.
    """
    from cyber_data_collector.utils.jev import JevJudge

    owned = judge is None
    judge = judge or JevJudge(conn)
    q = {question_id: (make_question or question)()}
    build = state_builder or build_pair_state
    out: Dict[str, float] = {}
    try:
        for i, pair in enumerate(pairs, 1):
            try:
                answer = judge.ask("event_pair", pair["pair_id"], version,
                                   build(conn, pair), q, refresh=refresh)[question_id]
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
    """The merge calibration, only if measured on the current model, question and evidence processing."""
    try:
        cal = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.error("Duplicate calibration unreadable (%s): %s - REPORT ONLY, nothing will be acted on",
                     path, exc)
        return None
    reason = calibration_guard.mismatch(cal, calibration_identity())
    if reason:
        logger.warning(calibration_guard.report_only_message("duplicate", cal, reason))
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


def rulings_between(conn: sqlite3.Connection, events: Sequence[str]) -> Dict[str, str]:
    """``{pair_id: reason}`` for pairs of ``events`` with an active 'different' ruling between their members.

    One query for all rulings and one for the members, however many events (the ledger's
    ``override_conflict`` is a query per pair). A database with no rulings table has none;
    any other read error raises: an unreadable ruling must not read as "no ruling".
    """
    try:
        rulings = conn.execute("SELECT left_enriched_event_id, right_enriched_event_id, reason FROM DedupOverrides "
                               "WHERE active = 1 AND verdict = 'different'").fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return {}
        raise RuntimeError(f"cannot read DedupOverrides to check rulings: {exc}") from exc
    if not rulings or not events:
        return {}
    marks = ", ".join("?" for _ in events)
    event_of: Dict[str, List[str]] = defaultdict(list)
    for enriched_id, dedup_id in conn.execute(
            f"SELECT enriched_event_id, deduplicated_event_id FROM EventDeduplicationMap "
            f"WHERE deduplicated_event_id IN ({marks})", list(events)):
        event_of[enriched_id].append(dedup_id)
    out: Dict[str, str] = {}
    for left, right, reason in rulings:
        for x in event_of.get(left, ()):
            for y in event_of.get(right, ()):
                if x != y:
                    out.setdefault(pair_id(x, y), f"ruling {left[:8]} vs {right[:8]}: {reason or 'no reason'}")
    return out


def calibrated_negatives(conn: sqlite3.Connection, duplicate_calibration: Optional[Dict[str, Any]],
                         ai_calibration: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """``{pair_id: reason}`` for stored final scores below a route's *calibrated* ``different_cutoff``.

    A score is evidence of "different" only where a calibration measured how often pairs scoring
    that low are labelled different (``calibrate_*_duplicates`` write ``different_cutoff``). A
    route with no such cut-off - its calibration predates it, or does not apply - contributes
    none: an uncalibrated low score is not a veto.
    """
    from cyber_data_collector.dedup import ai_duplicates as ai

    out: Dict[str, str] = {}
    for name, cal, scores in (("organisation", duplicate_calibration, stored_final_probabilities),
                              ("AI", ai_calibration, ai.stored_final_probabilities)):
        bound = (cal or {}).get("different_cutoff")
        if bound is None:
            continue
        for pid, p in scores(conn).items():
            if p < bound:
                out[pid] = f"{name} judgement {p:.2f} is below its calibrated 'different' bound {bound:.2f}"
    return out


def partition_without_conflicts(edges: Sequence[Tuple[str, str, float]], conflicts: Mapping[str, str]
                                ) -> Tuple[List[List[str]], List[Dict[str, Any]]]:
    """Group events by their chosen links, strongest first, never joining two groups across a conflict.

    A chain A=B, B=C proposes one group, but if a calibrated judgement, a label or a ruling says
    A and C are different incidents, the plain connected component would still merge all three.
    Links are taken strongest first (a labelled-same link outranks any score); a link that would
    put two events with a recorded conflict in one group is *held* - reported with the conflict
    that blocked it, not merged and not re-asked. Only the conflicts that change the outcome are
    ever looked at: a conflict between events no link connects is never reached. No model call.

    Returns ``(groups of two or more events, held links)``.
    """
    parent: Dict[str, str] = {}
    members: Dict[str, List[str]] = {}

    def find(x: str) -> str:
        if x not in parent:
            parent[x] = x
            members[x] = [x]
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    held: List[Dict[str, Any]] = []
    for a, b, p in sorted(edges, key=lambda e: (-e[2], e[0], e[1])):
        ra, rb = find(a), find(b)
        if ra == rb:
            continue
        blocking = next(((x, y) for x in members[ra] for y in members[rb] if pair_id(x, y) in conflicts), None)
        if blocking is not None:
            held.append({"pair_id": pair_id(a, b), "p_same": p if p < 2 else None,
                         "blocked_by": pair_id(*blocking), "reason": conflicts[pair_id(*blocking)]})
            continue
        parent[ra] = rb
        members[rb].extend(members.pop(ra))
    return [sorted(m) for m in members.values() if len(m) > 1], held


def merge_components(conn: sqlite3.Connection, components: Sequence[Sequence[str]],
                     different: set, ai_pairs: Optional[set] = None) -> List[Dict[str, Any]]:
    """Fold each component into its largest event; never across a labelled-different pair.

    ``ai_pairs`` are the chosen pairs that came from the AI route
    (:mod:`ai_duplicates`); a merge of exactly such a pair says so in the ledger.
    """
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
            if ai_pairs and pair_id(source, target) in ai_pairs:
                reason = ("Jev same-incident-ai: AI-involved events judged the same incident above the "
                          "calibrated AI cut-off")
            else:
                reason = f"Jev {QUESTION_VERSION}: same incident above the calibrated cut-off"
            try:
                ledger.merge_events(target, source, reason=reason, actor="rule")
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
                     timeline_search=None, include_ai: bool = True,
                     ai_calibration_path: Optional[Path] = None,
                     ai_labels_path: Optional[Path] = None, cap_candidates: bool = True,
                     partners_per_event: Optional[int] = None,
                     long_gap_budget: Optional[int] = None) -> Dict[str, Any]:
    """Judge ranked candidate pairs (escalating the middle band); merge at or above the calibrated cut-offs.

    Candidates are every pair sharing a victim, ranked and capped by
    :mod:`candidate_ranking` (``cap_candidates`` False asks them all;
    ``partners_per_event`` and ``long_gap_budget`` override its defaults; the
    counts before and after are in ``candidates``). With ``include_ai`` (the
    default) the AI route runs too (:mod:`ai_duplicates`): every pair of
    AI-involved events within the window, ranked the same way, is judged with the
    AI-pair question and merged at the AI calibration's own cut-off - report only
    without one. A pair of that route is not asked here as well.

    Chosen pairs of both routes are grouped together, strongest link first, and a
    link is *held* rather than merged when it would put two events in one group
    that a label, a ruling or a calibrated low score says are different
    incidents (``held_conflicts``): a chain A=B, B=C with A different from C no
    longer merges all three.
    """
    from cyber_data_collector.dedup import ai_duplicates as ai
    from cyber_data_collector.dedup import candidate_ranking as ranking
    from cyber_data_collector.dedup.org_timeline import research_all

    options = {"cap": cap_candidates, "partners_per_event": partners_per_event, "long_gap_budget": long_gap_budget}
    org_options = {"cap": cap_candidates,
                   "partners_per_event": ranking.PARTNERS_PER_EVENT if partners_per_event is None else partners_per_event,
                   "long_gap_budget": ranking.LONG_GAP_BUDGET if long_gap_budget is None else long_gap_budget}
    research_all(conn, search=timeline_search)
    corpus = ranking.load_corpus(conn)
    ai_all = ai.candidate_pairs(conn) if include_ai else []
    ai_pairs, ai_stats = ai.ranked_candidates(conn, corpus=corpus, pairs=ai_all, **options) if include_ai else ([], None)
    all_pairs = candidate_pairs(conn, skip={p["pair_id"] for p in ai_all})
    pairs, org_stats = ranking.select(corpus, all_pairs, group_of=lambda p: p["group"], **org_options)
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
    proposals = [{**by_id[pid], "p_same": probabilities.get(pid), "label": labels.get(pid), "route": "organisation"}
                 for pid in sorted(chosen, key=lambda x: -(probabilities.get(x) or 0))]
    # Links carry their strength; a labelled-same link outranks any score.
    edges = [(by_id[p]["a"], by_id[p]["b"], 2.0 if labels.get(p) == "same" else probabilities[p]) for p in chosen]

    ai_result: Optional[Dict[str, Any]] = None
    ai_chosen: List[str] = []
    ai_cal = None
    if include_ai:
        ai_result = ai.assess(conn, ai_pairs, judge=judge, escalation_client=escalation_client,
                              calibration_path=ai_calibration_path or ai.CALIBRATION_PATH,
                              labels_path=ai_labels_path or ai.LABELS_PATH, org_labels=labels)
        ai_chosen = ai_result["chosen"]
        ai_cal = ai_result["calibration"]
        different |= ai_result["different"]
        ai_by_id = {p["pair_id"]: p for p in ai_pairs}
        edges += [(ai_by_id[p]["a"], ai_by_id[p]["b"],
                   2.0 if ai_result["labels"].get(p) == "same" else ai_result["probabilities"][p])
                  for p in ai_chosen]
        proposals += ai_result["proposals"]
        escalation_failed = escalation_failed + ai_result["escalation_failed"]

    groups, held = [], []
    if edges:
        events = sorted({x for a, b, _ in edges for x in (a, b)})
        conflicts = {p: "labelled different" for p in different}
        conflicts.update(rulings_between(conn, events))
        conflicts.update(calibrated_negatives(conn, cal, ai_cal))
        groups, held = partition_without_conflicts(edges, conflicts)
        for h in held:
            logger.warning("Duplicate link %s held: it would join %s, which is a conflict (%s)",
                           h["pair_id"], h["blocked_by"], h["reason"])
    merged = []
    if apply and groups:
        merged = merge_components(conn, groups, different, ai_pairs=set(ai_chosen))
    out = {"pairs": len(pairs), "judged": len(probabilities), "escalated": len(escalated),
           "escalation_failed": escalation_failed, "cutoff": cutoff,
           "calibrated": cal is not None, "proposals": proposals, "merged": merged,
           "held_conflicts": held, "candidates": {"organisation": org_stats, "ai": ai_stats}}
    if ai_result is not None:
        out["ai"] = {k: v for k, v in ai_result.items()
                     if k not in ("chosen", "different", "labels", "probabilities", "calibration")}
    return out
