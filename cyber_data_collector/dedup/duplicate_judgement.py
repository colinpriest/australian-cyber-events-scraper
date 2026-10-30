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
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

from cyber_data_collector.dedup import bounded_candidates as bounded
from cyber_data_collector.dedup import calibration_guard, final_decisions
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


#: ``skip`` for the candidate functions: pair ids another route owns, or a predicate on two event ids.
Skip = Union[Iterable[str], Callable[[str, str], bool], None]


def _skip_predicate(skip: Skip) -> Optional[Callable[[str, str], bool]]:
    if skip is None:
        return None
    if callable(skip):
        return skip
    ids = set(skip)
    return (lambda a, b: pair_id(a, b) in ids) if ids else None


class PairUniverse:
    """Active events grouped by the organisation they happened to, from which candidate pairs are built on demand.

    "Sharing a victim" uses the organisation identity the recurrence analysis and the entity dashboard count
    by (:func:`victim_counting.organisation_groups`), not the entity row: until 2026-10-29 "ALS Global" / "ALS"
    and "St Vincent's Health Australia" / "St Vincent's Health" were one organisation in the analysis but never
    a candidate pair here, so a leak-site claim and the victim's disclosure days later counted as a repeat attack.

    Undated events are in it too: until 2026-10-28 they were left out, so 35 undated reports of the OpenAI
    agent's Medicare incident were never compared with each other or with the dated event.

    Loading is linear in the events. :meth:`all_pairs` builds every pair (calibration, recalibration, the
    measurements that count them); :meth:`ranked` builds only the ones that are asked, retrieving them through
    indexes in a large group (:mod:`bounded_candidates`); :meth:`pair` builds one.
    """

    def __init__(self, conn: sqlite3.Connection):
        from cyber_data_collector.dedup.recurrence_check import load_recurrence_events
        from cyber_data_collector.dedup.victim_counting import organisation_groups

        previous = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            events = load_recurrence_events(conn, include_undated=True)
        finally:
            conn.row_factory = previous
        group_of = organisation_groups(conn, [e.entity_id for e in events])
        # Groups in the order their first event is loaded; within a group, earliest first (undated last), and the
        # first row of an event names its organisation.
        by_group: Dict[Any, List[Any]] = {}
        for e in events:
            by_group.setdefault(group_of.get(e.entity_id, e.entity_id), []).append(e)
        self.groups: Dict[Any, Dict[str, bounded.Span]] = {}
        for gid, members in by_group.items():
            spans: Dict[str, bounded.Span] = {}
            for e in sorted(members, key=lambda e: (e.event_date is None, e.event_date or date.max, e.dedup_id)):
                spans.setdefault(e.dedup_id, bounded.Span(e.dedup_id, e.event_date, e.event_date, payload=e))
            self.groups[gid] = spans
        self._order = {gid: i for i, gid in enumerate(self.groups)}
        self._groups_of: Dict[str, List[Any]] = defaultdict(list)
        for gid, spans in self.groups.items():
            for event_id in spans:
                self._groups_of[event_id].append(gid)

    @staticmethod
    def _make(a: bounded.Span, b: bounded.Span, gap: Optional[int], gid: Any) -> Dict[str, Any]:
        first = a.payload
        return {"pair_id": pair_id(a.event_id, b.event_id), "a": a.event_id, "b": b.event_id,
                "organisation": first.entity_name, "entity_id": first.entity_id, "group": gid,
                "gap_days": gap if gap is not None else GAP_UNKNOWN}

    def events(self) -> Set[str]:
        return set(self._groups_of)

    def shared_group(self, a_id: str, b_id: str) -> Optional[Any]:
        """The group two events share (the first in load order if several), or None."""
        common = set(self._groups_of.get(a_id, ())) & set(self._groups_of.get(b_id, ()))
        return min(common, key=self._order.__getitem__) if common else None

    def pair(self, a_id: str, b_id: str) -> Optional[Dict[str, Any]]:
        """The candidate pair of two events, or None when they share no organisation."""
        if a_id == b_id:
            return None
        gid = self.shared_group(a_id, b_id)
        if gid is None:
            return None
        a, b = sorted((self.groups[gid][a_id], self.groups[gid][b_id]), key=bounded.span_order)
        return self._make(a, b, bounded.span_gap(a, b), gid)

    def all_pairs(self, skip: Skip = None) -> List[Dict[str, Any]]:
        """Every pair of active events sharing a victim, earliest first within each pair."""
        skipped = _skip_predicate(skip)
        pairs: Dict[str, Dict[str, Any]] = {}
        for gid, spans in self.groups.items():
            ordered = sorted(spans.values(), key=bounded.span_order)
            for i, a in enumerate(ordered):
                for b in ordered[i + 1:]:
                    pid = pair_id(a.event_id, b.event_id)
                    if pid in pairs or (skipped is not None and skipped(a.event_id, b.event_id)):
                        continue
                    pairs[pid] = self._make(a, b, bounded.span_gap(a, b), gid)
        return sorted(pairs.values(), key=lambda p: p["pair_id"])

    def ranked(self, corpus, skip: Skip = None, cap: bool = True, partners_per_event: Optional[int] = None,
               long_gap_budget: Optional[int] = None, exhaustive_max: Optional[int] = None,
               uncapped_max: Optional[int] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """The pairs that are asked: ranked by :mod:`candidate_ranking` and, in a group of more than
        ``uncapped_max`` (``bounded.UNCAPPED_GROUP_MAX``, 60) events, capped - without building the pairs of a
        large group (``(pairs, stats)``). Every pair of a smaller group is a candidate. ``skip`` removes the pairs
        another route owns."""
        from cyber_data_collector.dedup import candidate_ranking as ranking

        return bounded.rank_groups(
            corpus, {gid: list(spans.values()) for gid, spans in self.groups.items()}, self._make, cap=cap,
            partners_per_event=ranking.PARTNERS_PER_EVENT if partners_per_event is None else partners_per_event,
            long_gap_budget=ranking.LONG_GAP_BUDGET if long_gap_budget is None else long_gap_budget,
            exclude=_skip_predicate(skip),
            exhaustive_max=bounded.EXHAUSTIVE_GROUP_MAX if exhaustive_max is None else exhaustive_max,
            uncapped_max=bounded.UNCAPPED_GROUP_MAX if uncapped_max is None else uncapped_max)


def candidate_pairs(conn: sqlite3.Connection, skip: Skip = None) -> List[Dict[str, Any]]:
    """Every pair of active events sharing a victim, earliest first within each pair (:class:`PairUniverse`).

    ``skip`` holds pair ids (or a predicate on two event ids) another route owns and asks itself: an
    AI-involved pair within the window belongs to :mod:`ai_duplicates` (one question per pair, the one that
    knows how rare AI incidents are), so it is not asked here as well. The calibration and recalibration of this
    route ask for every pair. ``check_duplicates`` does not use this: it asks :meth:`PairUniverse.ranked`.
    """
    return PairUniverse(conn).all_pairs(skip)


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


def ensure_schema(conn: sqlite3.Connection) -> None:
    final_decisions.ensure_schema(conn)


state_hash = final_decisions.state_hash


@dataclass(frozen=True)
class Route:
    """One duplicate question as the pipeline asks it: its Jev question, the escalation of its middle band,
    and how a pair's evidence is built. The organisation route and the AI route (:mod:`ai_duplicates`) each
    have one; ``name`` keys the stored decisions (``FinalDecisions.step`` is ``duplicates-<name>``)."""

    name: str
    question_id: str
    version: str
    make_question: Callable[[], Any]
    state_builder: Callable[[sqlite3.Connection, Dict[str, Any]], Dict[str, Any]]
    escalation_id: str
    escalation_version: str
    escalation_text: Callable[[], str]
    band: Tuple[float, float]

    @property
    def step(self) -> final_decisions.Step:
        return final_decisions.Step(
            name=f"duplicates-{self.name}", question_id=self.question_id, version=self.version,
            make_question=self.make_question, escalation_id=self.escalation_id,
            escalation_version=self.escalation_version, escalation_text=self.escalation_text, band=self.band)

    @property
    def question_version(self) -> str:
        return self.step.question_version

    def models(self) -> Tuple[str, str]:
        return self.step.models()


def _memoised(builder: Callable[[sqlite3.Connection, Dict[str, Any]], Dict[str, Any]],
              states: Dict[str, Dict[str, Any]]) -> Callable[[sqlite3.Connection, Dict[str, Any]], Dict[str, Any]]:
    """``builder`` that builds each pair's state once per ``states`` (a pair's evidence is read from the database
    to be asked, escalated, hashed and checked; none of that should read it again)."""
    def build(conn: sqlite3.Connection, pair: Dict[str, Any]) -> Dict[str, Any]:
        if pair["pair_id"] not in states:
            states[pair["pair_id"]] = builder(conn, pair)
        return states[pair["pair_id"]]
    return build


@dataclass
class ScoredPairs:
    """What :func:`score_pairs` found. ``final`` holds only pairs with a decision on their current evidence."""

    final: Dict[str, float] = field(default_factory=dict)
    jev: Dict[str, float] = field(default_factory=dict)
    escalated: Dict[str, float] = field(default_factory=dict)
    failed: List[str] = field(default_factory=list)       # in the escalation band, escalation failed: undecided
    evidence: Dict[str, str] = field(default_factory=dict)  # pair id -> the evidence hash it was decided on


def score_pairs(conn: sqlite3.Connection, pairs: Sequence[Dict[str, Any]], route: "Route", judge=None,
                escalation_client=None, refresh: bool = False,
                states: Optional[Dict[str, Dict[str, Any]]] = None) -> ScoredPairs:
    """Judge ``pairs`` (Jev, then the reasoning model for the middle band) and store each final decision against
    the evidence it was made on.

    A pair's evidence is built once and both models are shown that same state, so the final score is
    Jev's or the escalation's **on one evidence**; the decision is stored in ``FinalDecisions`` with the hash of
    that evidence, the models and the question versions. A pair whose escalation failed has no decision (it
    is retried next run, never merged on Jev's mid-band score). Stored model answers are reused whenever their
    own input hash matches, so a run with unchanged evidence buys nothing.
    """
    ensure_schema(conn)
    states = {} if states is None else states
    build = _memoised(route.state_builder, states)
    out = ScoredPairs(jev=judge_pairs(conn, pairs, judge=judge, refresh=refresh, question_id=route.question_id,
                                      version=route.version, make_question=route.make_question,
                                      state_builder=build))
    out.escalated = escalate_band(conn, pairs, out.jev, client=escalation_client, failed=out.failed,
                                  question_id=route.escalation_id, version=route.escalation_version,
                                  state_builder=build, question_text=route.escalation_text, band=route.band)
    out.final = final_decisions.record_scores(
        conn, route.step, [(p["pair_id"], lambda p=p: states[p["pair_id"]]) for p in pairs],
        out.jev, out.escalated, out.failed)
    out.evidence = {pid: state_hash(states[pid]) for pid in out.final}
    return out


def current_decisions(conn: sqlite3.Connection, route: "Route", pairs: Sequence[Dict[str, Any]],
                      states: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, float]:
    """Final score per pair from the decision made on the pair's **current** evidence; nothing for a pair with none.

    The pair's state is built now and hashed; only a decision stored under that hash, the current models and the
    current question versions is read. A decision made on other evidence is not a score for this pair - it is
    :func:`stale_decisions`. Nothing is asked.
    """
    states = {} if states is None else states
    build = _memoised(route.state_builder, states)
    return final_decisions.read_current(
        conn, route.step, [(p["pair_id"], lambda p=p: build(conn, p)) for p in pairs])


def stale_decisions(conn: sqlite3.Connection, route: "Route", pair_ids: Optional[Iterable[str]] = None
                    ) -> Dict[str, Tuple[float, str]]:
    """``{pair_id: (final score, evidence hash)}``: the newest decision per pair under the current models and
    question versions, whatever evidence it was made on. A score here is **not** a verdict on the pair as it is
    now; it says only what it was once judged to be, and whether it needs revisiting is the caller's question."""
    return final_decisions.latest(conn, route.step, pair_ids)


def stored_final_probabilities(conn: sqlite3.Connection, pairs: Sequence[Dict[str, Any]],
                               route: Optional["Route"] = None,
                               states: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, float]:
    """Final score per pair from storage: the decision made on the pair's current evidence (:func:`current_decisions`).

    It used to select the latest Jev answer and the latest escalation for a pair independently, with no check
    that they were given the same evidence, so a stale escalation could be combined with a fresh Jev answer (or
    stand beside an old low score as a veto). Now only a stored decision whose evidence matches counts.
    """
    return current_decisions(conn, route or ORGANISATION, pairs, states)


def unvalidated_final_probabilities(conn: sqlite3.Connection, *, question_id: str = QUESTION_ID,
                                    version: str = QUESTION_VERSION, escalation_id: str = ESCALATION_QUESTION_ID,
                                    escalation_version: str = ESCALATION_VERSION,
                                    band: Tuple[float, float] = ESCALATE_BAND) -> Dict[str, float]:
    """The newest Jev answer per pair, replaced by the newest escalation in the band - **not tied to any evidence**.

    For reading a labelling-time snapshot's history (``scripts/evaluate_dedup.py``), where nothing can be rebuilt
    to compare with. The pipeline never acts on this: it uses :func:`stored_final_probabilities`.
    """
    from cyber_data_collector.utils.llm_models import openai_model

    def latest(qid: str, ver: str, model: str) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for sid, value in conn.execute(
                "SELECT subject_id, value FROM ModelJudgements WHERE question_id = ? AND question_version = ? "
                "AND model_requested = ? ORDER BY rowid", (qid, ver, model)):
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


#: The organisation route: pairs of events recorded against one organisation.
ORGANISATION = Route(
    name="organisation", question_id=QUESTION_ID, version=QUESTION_VERSION, make_question=question,
    state_builder=build_pair_state, escalation_id=ESCALATION_QUESTION_ID, escalation_version=ESCALATION_VERSION,
    escalation_text=escalation_question, band=ESCALATE_BAND)


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
                         ai_calibration: Optional[Dict[str, Any]], events: Iterable[str],
                         pair_lookups: Mapping[str, Callable[[str, str], Optional[Dict[str, Any]]]], judge=None,
                         escalation_client=None, attempted: Iterable[str] = ()
                         ) -> Tuple[Dict[str, str], List[str]]:
    """``({pair_id: reason}, re-judged pair ids)``: final scores below a route's *calibrated* ``different_cutoff``
    that are still true of the pair as it is now, among ``events`` - the events a proposed group involves.

    A score is evidence of "different" only where a calibration measured how often pairs scoring that low are
    labelled different (``calibrate_*_duplicates`` write ``different_cutoff``); a route with no such cut-off
    contributes none. It is evidence about the pair only on the evidence it was made on:

    * a pair whose stored decision matches its current evidence vetoes at that score;
    * a pair whose decision was made on **other** evidence and was low is revisited - asked again, on the
      current evidence, and the new answer decides (a pair the question cannot answer now, or whose escalation
      fails, vetoes nothing). This is the only place a stale decision is re-asked: it could still block a
      proposed merge, so it must be right; a stale decision that could not (it was high, or the pair is not
      among the proposed events) is left alone;
    * a pair with no decision, or no longer a candidate, vetoes nothing.

    ``pair_lookups`` give a route's pair (``{"organisation": universe.pair, "ai": ai_universe.pair}``);
    ``attempted`` are pair ids already asked this run (a failed escalation is not tried twice).
    """
    from cyber_data_collector.dedup import ai_duplicates as ai

    in_play = set(events)
    tried = set(attempted)
    out: Dict[str, str] = {}
    revisited: List[str] = []
    for name, route, cal in (("organisation", ORGANISATION, duplicate_calibration), ("AI", ai.ROUTE, ai_calibration)):
        bound = (cal or {}).get("different_cutoff")
        if bound is None:
            continue
        lookup = pair_lookups[route.name]
        states: Dict[str, Dict[str, Any]] = {}
        for pid, (old_score, _) in stale_decisions(conn, route).items():
            ids = pid.split("|")
            if len(ids) != 2 or not set(ids) <= in_play or pid in tried:
                continue
            pair = lookup(*ids)
            if pair is None:
                continue
            current = current_decisions(conn, route, [pair], states)
            if pid in current:
                score, note = current[pid], ""
            elif old_score < bound:
                scored = score_pairs(conn, [pair], route, judge=judge, escalation_client=escalation_client,
                                     states=states)
                revisited.append(pid)
                if pid not in scored.final:
                    logger.warning("Duplicate pair %s: its stored decision was made on other evidence and could not "
                                   "be re-asked (%s); it vetoes nothing this run", pid,
                                   "escalation failed" if pid in scored.failed else "no answer")
                    continue
                score, note = scored.final[pid], " (re-judged: its evidence had changed)"
            else:
                continue
            if score < bound:
                out[pid] = f"{name} judgement {score:.2f} is below its calibrated 'different' bound {bound:.2f}{note}"
    return out, revisited


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
    org_options = dict(options)
    research_all(conn, search=timeline_search)
    corpus = ranking.load_corpus(conn)
    universe = PairUniverse(conn)
    ai_universe = ai.AIUniverse(conn, universe) if include_ai else None
    ai_pairs, ai_stats = ai_universe.ranked(corpus, **options) if include_ai else ([], None)
    # Pairs are retrieved and ranked per group (bounded_candidates): the pairs that are not asked are never built.
    pairs, org_stats = universe.ranked(corpus, skip=ai_universe.owns if include_ai else None, **org_options)
    labels = labelled_pairs(labels_path)
    # Labels decide first: a pair labelled same is merged and one labelled different never is, whatever the model
    # says, so neither is (re-)asked about.
    scored = score_pairs(conn, [p for p in pairs if labels.get(p["pair_id"]) not in ("same", "different")],
                         ORGANISATION, judge=judge, escalation_client=escalation_client)
    escalated = scored.escalated
    escalation_failed: List[str] = list(scored.failed)
    probabilities = scored.final
    different = {p for p, label in labels.items() if label == "different"}
    cal = active_calibration(calibration_path)
    cutoff = cal["cutoff"] if cal else None
    by_id = {p["pair_id"]: p for p in pairs}
    # A labelled-same pair is merged without a score; any other pair needs its score at the cut-off.
    chosen = [] if cutoff is None else [
        pid for pid in sorted({q["pair_id"] for q in pairs} | set(probabilities))
        if labels.get(pid) == "same"
        or (pid in probabilities and probabilities[pid] >= cutoff and pid not in different)]
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

    groups, held, rejudged = [], [], []
    if edges:
        events = sorted({x for a, b, _ in edges for x in (a, b)})
        conflicts = {p: "labelled different" for p in different}
        conflicts.update(rulings_between(conn, events))
        # A stored low score vetoes only on the evidence it was made on: one made on other evidence is
        # asked again here (and only here), because it could still block this proposed merge.
        negatives, rejudged = calibrated_negatives(
            conn, cal, ai_cal, events,
            {ORGANISATION.name: universe.pair, ai.ROUTE.name: ai_universe.pair if include_ai else (lambda a, b: None)},
            judge=judge, escalation_client=escalation_client, attempted=escalation_failed)
        conflicts.update(negatives)
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
           "held_conflicts": held, "veto_rejudged": rejudged,
           "candidates": {"organisation": org_stats, "ai": ai_stats}}
    if ai_result is not None:
        out["ai"] = {k: v for k, v in ai_result.items()
                     if k not in ("chosen", "different", "labels", "probabilities", "calibration")}
    return out
