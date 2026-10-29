"""AI-involved events are duplicate candidates of each other, whatever their victim, with their own calibrated cut-off.

The organisation route of ``check-duplicates`` (:mod:`duplicate_judgement`)
pairs events by victim. Coverage of one AI incident defeats that. On
2026-10-29 the OpenAI agent that reached Services Australia's Medicare
statistics portal in June 2026 was spread over about 15 active events: most had
no victim, a vague one ("Australian health"), or a wrong one ("Australian
Parliament", "Data USA", "OpenAI"), so they never met the main event in a pair;
the one fragment that did (sharing the Australian Institute of Health and
Welfare) scored 0.80 against the 0.81 cut-off. A second AI-agent incident (a
gym's booking system) was spread over four.

AI incidents are rare in this database - a few per half-year - so two AI events
within a few months of each other are far more often reports of one incident
than the same pair of events would be for, say, ransomware. "Merge AI events
more aggressively" is therefore done in three calibrated parts, not by lowering
a threshold by hand:

1. **Candidates.** Every pair of active events whose ``ai_involvement`` is one
   of ``AI_INVOLVED_LABELS`` and whose dates are within ``AI_WINDOW_DAYS`` of
   each other (the distance between their date *intervals*, 0 when they
   overlap; an undated event is paired with every other: a missing date is no
   evidence of a separate incident) is a candidate, victim or no victim. A pair
   in the window belongs to this route and is not asked by the organisation
   route as well (one question per pair); the pair's shared organisation, if it
   has one, is still shown, with that organisation's researched timeline.
2. **The question knows the base rate.** Jev (and, in the middle band, the
   reasoning model) is told how many AI incidents the database holds per
   completed half-year, computed from the data, beside the number of all
   incidents, and sees each event's AI category and description. Two AI events
   a few months apart are probably one incident unless the evidence shows a
   different victim, system or actor.
3. **Its own calibration.** The merge cut-off is the lowest P(same) at which the
   pairs it would merge are, weighted, at least 95% labelled ``same`` on blind
   labels of AI pairs (``scripts/calibrate_ai_duplicates.py``), with a
   bootstrap of the whole choose-then-test procedure. If the model is
   conservative on these pairs the cut-off comes out below the organisation
   route's 0.81; if it is not, it does not. Without a calibration measured on
   the current models, questions and evidence processing (``calibration_guard``)
   the route reports and merges nothing.

Labelled pairs are ground truth in both directions (``AI_LABELS`` here and the
organisation route's ``duplicate_labels.csv``): ``different`` is never merged,
``same`` is merged once the route is calibrated. Merges go through the ledger
with the organisation route's, so a chain A=B, B=C across both routes is
merged consistently, snapshotted, reversible and respecting cached
``different`` rulings.

Why 365 days. Reporting of an AI incident is spread over weeks (news,
commentary, a regulator's or government's response, a company's disclosure of
more detail) and an event is dated by its incident date when known and by its
first report otherwise. On the 2026-10-29 copy the 90th percentile of the lag
between an incident and its first report was 257 days, and the follow-up of the
two AI-agent incidents above ran 3 to 5 months past the incident; a year takes
in both and stays well short of the multi-year gaps (Clearview AI 2019, Kmart
2020) between unrelated AI privacy cases. The window only decides what is
asked: the calibrated cut-off decides what merges, and a wider window costs
calls (36 AI events gave 476 of the 630 possible pairs), not precision. It is in the evidence
fingerprint, so changing it makes the calibration stale.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from cyber_data_collector.dedup import calibration_guard
from cyber_data_collector.dedup import duplicate_judgement as dj
from cyber_data_collector.dedup.event_reality import ROOT
from cyber_data_collector.processing import ai_involvement
from cyber_data_collector.utils.llm_models import typesafe_model

logger = logging.getLogger(__name__)

#: Pair ids, the Jev question and the reasoning-model escalation are stored beside the organisation route's
#: (``ModelJudgements``, ``subject_type='event_pair'``) under their own question ids.
QUESTION_ID = "same_incident_ai"
QUESTION_VERSION = "same-incident-ai-v1"
ESCALATION_QUESTION_ID = "same_incident_ai_escalated"
ESCALATION_VERSION = "same-incident-ai-escalation-v1"
#: Fixed before calibration, like the organisation route's: scores inside it are re-judged by the reasoning model.
ESCALATE_BAND = (0.2, 0.8)
#: See the module docstring for why a year.
AI_WINDOW_DAYS = 365
#: Best partners kept per AI event (the general default is 8). One AI incident is reported by a dozen or more
#: events, each of which should keep most of its siblings, and 36 AI events make the whole set cheap to ask.
AI_PARTNERS_PER_EVENT = 16
#: How many completed half-years of the base rate a judgement is shown.
BASE_RATE_HALF_YEARS = 6
RECORDS_PER_EVENT = dj.RECORDS_PER_EVENT
#: Bump when what a pair's events (or the base rate) show a model changes beyond the organisation route's evidence.
EVIDENCE_VERSION = "ai-duplicate-evidence-v1"

LABELS_PATH = ROOT / "evaluation" / "ai_duplicate_labels.csv"
CALIBRATION_PATH = ROOT / "evaluation" / "ai_duplicate_calibration.json"
SAMPLE_PATH = ROOT / "evaluation" / "ai_duplicate_sample.csv"

ROUTE_ORGANISATION = "organisation"
ROUTE_AI = "ai"

INSTRUCTIONS = (
    "`event_a` and `event_b` are two stored events in which artificial intelligence played a part "
    "(`ai_involvement`: the category an earlier judgement gave the event; `ai_description`: that "
    "judgement's short description of it - either may be wrong). They are paired because both involve AI "
    "and their dates are within a year, NOT because they name the same organisation: `gap_days` is the "
    "shortest distance between the two events' date intervals (0 when they overlap; unknown when either "
    "has no date), and an event's recorded `victim` may be missing, vague, a regulator, the AI company or "
    "simply wrong, so judge the incident the records describe, not the label. Decide whether they describe "
    "the SAME real-world incident. `ai_incident_base_rate` says how rare AI incidents are in this "
    "database: the AI-involved incidents recorded per completed half-year, beside all incidents. One AI "
    "incident is reported many times over - news, commentary, explainers, a government's, regulator's or "
    "company's response, later disclosure of detail - and each report is stored as its own event until "
    "merged, so given how few AI incidents there are, two AI events a few weeks or months apart about the "
    "same kind of AI action are far more likely to be coverage of one incident than two incidents. Treat "
    "them as different only when the evidence shows it: a different victim organisation or system, a "
    "different AI system, operator or actor, a date of occurrence or discovery that the records state to "
    "the day and that differs, or a record that itself presents the second as a new, separate incident "
    "(another case, a second disclosure, a further victim). The same AI vendor or product in both is not "
    "enough to make them one incident. Each event's `records` hold the page actually stored (often the "
    "organisation's own notice, a privacy policy or an unrelated page) and separate Perplexity research; "
    "`corroboration`, when present, is a web search for public reporting of that event. Each record's "
    "`date` says how its date was determined (`basis`) and how precise it is (`precision`); a gap built "
    "from publication or found dates, from month- or year-only dates, or from an event date no record "
    "carries, is weak evidence of separate incidents. `organisation_timeline`, when present, is researched "
    "history of the shared organisation's publicly reported incidents: if both events match the same "
    "incident in it they are one incident; if they match two different incidents in it they are two."
)
CRITERIA = {
    "true": ("One incident: the same AI-involved attack, breach or misuse reported again - follow-up "
             "coverage, commentary or analysis of it, a government, regulator or company response, an "
             "explainer, a later disclosure of more detail - even where the reports name a missing, vague "
             "or different organisation as the victim."),
    "false": ("Two incidents: separate occasions, victims, systems or actors - including a second incident "
              "disclosed later that involves the same AI system, vendor or operator - or a report of a "
              "different matter altogether."),
}


# ----------------------------------------------------------------------
# Candidates
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class AIEvent:
    dedup_id: str
    title: str
    label: str
    start: Optional[date]          # earliest day the event's date interval allows
    end: Optional[date]            # latest


def _day(value: Any) -> Optional[date]:
    from cyber_data_collector.dedup.recurrence_check import parse_event_date
    return parse_event_date(value)


def ai_events(conn: sqlite3.Connection) -> List[AIEvent]:
    """Active events with (possible) AI involvement, with the date interval each one's date allows.

    A day-precise date is a point; a month-precise one spans the month
    (``event_date_earliest`` / ``event_date_latest``, set by incident_dates when
    the records agree on a verified precision); any other stored date is a point
    (its precision is unverified). No date, no interval.
    """
    columns = {r[1] for r in conn.execute("PRAGMA table_info(DeduplicatedEvents)")}
    if "ai_involvement" not in columns:
        return []
    earliest = "event_date_earliest" if "event_date_earliest" in columns else "NULL"
    latest = "event_date_latest" if "event_date_latest" in columns else "NULL"
    marks = ", ".join("?" for _ in ai_involvement.AI_INVOLVED_LABELS)
    rows = conn.execute(
        f"SELECT deduplicated_event_id, COALESCE(title, ''), ai_involvement, event_date, {earliest}, {latest} "
        f"FROM DeduplicatedEvents WHERE COALESCE(status, 'Active') = 'Active' AND ai_involvement IN ({marks}) "
        "ORDER BY deduplicated_event_id", ai_involvement.AI_INVOLVED_LABELS).fetchall()
    out = []
    for dedup_id, title, label, event_date, lo, hi in rows:
        point = _day(event_date)
        start, end = _day(lo) or point, _day(hi) or point
        if start is not None and end is not None and end < start:
            start, end = end, start
        if start is None or end is None:
            start = end = None
        out.append(AIEvent(dedup_id, title, label, start, end))
    return out


def interval_gap(a: AIEvent, b: AIEvent) -> Optional[int]:
    """Days between the two events' date intervals (0 when they overlap); None when either is undated."""
    if a.start is None or b.start is None:
        return None
    if a.start <= b.end and b.start <= a.end:
        return 0
    return (b.start - a.end).days if b.start > a.end else (a.start - b.end).days


def candidate_pairs(conn: sqlite3.Connection, window_days: int = AI_WINDOW_DAYS) -> List[Dict[str, Any]]:
    """Every pair of AI-involved active events within ``window_days`` (or with an unknown gap).

    Earliest event first within a pair, undated last. A pair that also shares a
    victim organisation carries it (``organisation``, ``entity_id``) so the
    judgement shows the organisation's timeline; ``shared_organisation`` says so.
    """
    events = ai_events(conn)
    shared = {p["pair_id"]: p for p in dj.candidate_pairs(conn)} if len(events) > 1 else {}
    events = sorted(events, key=lambda e: (e.start is None, e.start or date.max, e.end or date.max, e.dedup_id))
    pairs: List[Dict[str, Any]] = []
    for i, a in enumerate(events):
        for b in events[i + 1:]:
            gap = interval_gap(a, b)
            if gap is not None and gap > window_days:
                continue
            pid = dj.pair_id(a.dedup_id, b.dedup_id)
            org = shared.get(pid)
            pairs.append({"pair_id": pid, "a": a.dedup_id, "b": b.dedup_id,
                          "organisation": org["organisation"] if org else None,
                          "entity_id": org["entity_id"] if org else None,
                          "shared_organisation": org is not None,
                          "gap_days": gap if gap is not None else dj.GAP_UNKNOWN})
    return sorted(pairs, key=lambda p: p["pair_id"])


def ranked_candidates(conn: sqlite3.Connection, corpus=None, pairs: Optional[Sequence[Dict[str, Any]]] = None,
                      cap: bool = True, partners_per_event: Optional[int] = None,
                      long_gap_budget: Optional[int] = None,
                      window_days: int = AI_WINDOW_DAYS) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """The candidate pairs that are asked: ranked and capped like every route's
    (:mod:`candidate_ranking`), all AI-involved events being one group.

    ``pairs`` are :func:`candidate_pairs` when already computed. Returns
    ``(pairs, stats)`` with the counts before and after. The calibration's
    sample, the packet, the score step and ``check-duplicates`` use this;
    recalibration scores the labelled pairs from :func:`candidate_pairs`, so a
    pair that ranks outside the cap today can still be re-scored.
    """
    from cyber_data_collector.dedup import candidate_ranking as ranking

    corpus = corpus or ranking.load_corpus(conn)
    pairs = candidate_pairs(conn, window_days) if pairs is None else pairs
    return ranking.select(
        corpus, pairs, group_of=lambda p: ROUTE_AI, window_days=window_days, cap=cap,
        partners_per_event=AI_PARTNERS_PER_EVENT if partners_per_event is None else partners_per_event,
        long_gap_budget=ranking.LONG_GAP_BUDGET if long_gap_budget is None else long_gap_budget)


# ----------------------------------------------------------------------
# Evidence
# ----------------------------------------------------------------------

def half_year_label(day: date) -> str:
    return f"{day.year} H{1 if day.month <= 6 else 2}"


def _completed_half_years(today: date, count: int) -> List[str]:
    """The ``count`` most recent half-years that ended before ``today``, oldest first."""
    year, half = today.year, 1 if today.month <= 6 else 2
    labels: List[str] = []
    for _ in range(count):
        half -= 1
        if half == 0:
            year, half = year - 1, 2
        labels.append(f"{year} H{half}")
    return list(reversed(labels))


def base_rate(conn: sqlite3.Connection, today: Optional[date] = None,
              half_years: int = BASE_RATE_HALF_YEARS) -> Dict[str, Any]:
    """How rare AI incidents are in this database, from the data.

    Active incidents with (possible) AI involvement, and all active incidents,
    by half-year of incident date, over the most recent completed half-years.
    The half-year in progress is left out: it is incomplete, and counting it
    would change every pair's evidence (and so re-ask every pair) each time an
    event arrives. A single AI incident reported many times is several events
    until merged, so the counts can overstate the number of incidents.
    """
    today = today or date.today()
    wanted = _completed_half_years(today, half_years)
    ai_ids = {e.dedup_id for e in ai_events(conn)}
    ai_counts = dict.fromkeys(wanted, 0)
    all_counts = dict.fromkeys(wanted, 0)
    for dedup_id, event_date in conn.execute(
            "SELECT deduplicated_event_id, event_date FROM DeduplicatedEvents "
            "WHERE COALESCE(status, 'Active') = 'Active'"):
        day = _day(event_date)
        if day is None:
            continue
        label = half_year_label(day)
        if label in all_counts:
            all_counts[label] += 1
            if dedup_id in ai_ids:
                ai_counts[label] += 1
    return {"ai_incidents_per_half_year": ai_counts, "all_incidents_per_half_year": all_counts,
            "counted": f"active events by incident date (first report when the incident date is unknown), "
                       f"the {half_years} most recent completed half-years",
            "caution": "one incident reported many times is several events until merged, so counts can "
                       "overstate the number of incidents"}


def _ai_event_state(conn: sqlite3.Connection, dedup_id: str, labels: Mapping[str, str]) -> Dict[str, Any]:
    state = dj._event_with_dates(conn, dedup_id)
    state["event"]["ai_involvement"] = labels.get(dedup_id)
    description = ai_involvement.stored_description(conn, dedup_id)
    if description:
        state["event"]["ai_description"] = description
    return state


def build_pair_state(conn: sqlite3.Connection, pair: Dict[str, Any], today: Optional[date] = None) -> Dict[str, Any]:
    from cyber_data_collector.dedup.org_timeline import for_judgement

    labels = {e.dedup_id: e.label for e in ai_events(conn)}
    state: Dict[str, Any] = {"organisation": pair.get("organisation"), "gap_days": pair["gap_days"],
                             "event_a": _ai_event_state(conn, pair["a"], labels),
                             "event_b": _ai_event_state(conn, pair["b"], labels),
                             "ai_incident_base_rate": base_rate(conn, today)}
    timeline = for_judgement(conn, pair["entity_id"]) if pair.get("entity_id") is not None else None
    if timeline:
        state["organisation_timeline"] = {k: v for k, v in timeline.items() if k != "organisation"}
    return state


def question():
    from typesafe_sdk import Noul, NoulCriteria
    return Noul(instructions=INSTRUCTIONS, criteria=NoulCriteria(**CRITERIA))


def escalation_question() -> str:
    return (INSTRUCTIONS + f" YES means: {CRITERIA['true']} NO means: {CRITERIA['false']}"
            " Do event_a and event_b describe the same real-world incident?")


def evidence_fingerprint() -> str:
    """A hash of what an AI-pair judgement sees: the pair's evidence as the organisation route shows it, the AI
    category and description, the base rate's definition, and the window and ranking that decide which pairs are
    asked (the labelled sample is drawn from them)."""
    from cyber_data_collector.dedup import candidate_ranking as ranking

    return calibration_guard.fingerprint(
        "ai-duplicates", version=EVIDENCE_VERSION, window_days=AI_WINDOW_DAYS, escalate_band=list(ESCALATE_BAND),
        ranking=[AI_PARTNERS_PER_EVENT, ranking.LONG_GAP_BUDGET, ranking.STRONG_RARE_WORDS,
                 ranking.W_SOURCE, ranking.W_SUPPLIER, ranking.W_RARE_WORD, ranking.W_RECORDS, ranking.W_METHOD,
                 ranking.W_DATE],
        base_rate_half_years=BASE_RATE_HALF_YEARS, ai_labels=list(ai_involvement.AI_INVOLVED_LABELS),
        ai_category=ai_involvement.QUESTION_VERSION, ai_description=ai_involvement.DESCRIPTION_PROMPT_VERSION,
        duplicates=dj.evidence_fingerprint())


def calibration_identity() -> Dict[str, str]:
    from cyber_data_collector.utils.llm_models import openai_model
    return {"question_version": f"{QUESTION_VERSION}+{ESCALATION_VERSION}",
            "model": f"{typesafe_model()}+{openai_model()}", "evidence": evidence_fingerprint()}


# ----------------------------------------------------------------------
# Judging
# ----------------------------------------------------------------------

def judge_pairs(conn: sqlite3.Connection, pairs: Sequence[Dict[str, Any]], judge=None,
                refresh: bool = False) -> Dict[str, float]:
    """P(same incident) per pair id from the AI-pair question; stored answers are reused."""
    return dj.judge_pairs(conn, pairs, judge=judge, refresh=refresh, question_id=QUESTION_ID,
                          version=QUESTION_VERSION, make_question=question, state_builder=build_pair_state)


def escalate_band(conn: sqlite3.Connection, pairs: Sequence[Dict[str, Any]], probabilities: Dict[str, float],
                  client=None, failed: Optional[List[str]] = None) -> Dict[str, float]:
    return dj.escalate_band(conn, pairs, probabilities, client=client, failed=failed,
                            question_id=ESCALATION_QUESTION_ID, version=ESCALATION_VERSION,
                            state_builder=build_pair_state, question_text=escalation_question, band=ESCALATE_BAND)


def stored_final_probabilities(conn: sqlite3.Connection) -> Dict[str, float]:
    """Final score per AI pair from storage: the escalation's in the band, else Jev's."""
    return dj.stored_final_probabilities(conn, question_id=QUESTION_ID, version=QUESTION_VERSION,
                                         escalation_id=ESCALATION_QUESTION_ID,
                                         escalation_version=ESCALATION_VERSION, band=ESCALATE_BAND)


def score_pairs(conn: sqlite3.Connection, pairs: Sequence[Dict[str, Any]], judge=None,
                escalation_client=None) -> Tuple[Dict[str, float], int, List[str]]:
    """``(final scores, escalated count, pairs whose escalation failed)``.

    A pair whose escalation failed has no final score: it is undecided, not scored on Jev's middle-band answer.
    """
    jev_scores = judge_pairs(conn, pairs, judge=judge)
    failed: List[str] = []
    escalated = escalate_band(conn, pairs, jev_scores, client=escalation_client, failed=failed)
    return {p: escalated.get(p, v) for p, v in jev_scores.items() if p not in failed}, len(escalated), failed


# ----------------------------------------------------------------------
# Labels and calibration
# ----------------------------------------------------------------------

def labelled_pairs(path: Optional[Path] = None, required: bool = False) -> Dict[str, str]:
    """Labelled AI pairs (``same`` / ``different`` / ``unsure``).

    A file that does not exist is "not labelled yet" and reads as empty unless
    ``required`` (an active calibration or a calibration run needs them). An
    unreadable file always raises: it must not read as "no labels", or a pair
    labelled ``different`` could be merged.
    """
    path = path or LABELS_PATH
    try:
        with Path(path).open(encoding="utf-8", newline="") as fh:
            labels = {r["pair_id"]: (r["label"] or "").strip() for r in csv.DictReader(fh)}
    except FileNotFoundError:
        if required:
            raise RuntimeError(f"AI duplicate labels missing ({path}); refusing to merge") from None
        return {}
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError(f"AI duplicate labels unreadable ({path}): {exc}; refusing to merge") from exc
    if not labels and required:
        raise RuntimeError(f"AI duplicate labels empty ({path}); refusing to merge")
    return labels


def active_calibration(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The AI-pair calibration, only if measured on the current models, questions and evidence processing."""
    path = path or CALIBRATION_PATH
    try:
        cal = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("REPORT ONLY: AI duplicate pairs have no calibration (%s); nothing is merged for them "
                       "until scripts/calibrate_ai_duplicates.py calibrate has run on labelled pairs", path)
        return None
    except (OSError, ValueError) as exc:
        logger.error("AI duplicate calibration unreadable (%s): %s - REPORT ONLY, nothing will be acted on",
                     path, exc)
        return None
    reason = calibration_guard.mismatch(cal, calibration_identity())
    if reason:
        logger.warning(calibration_guard.report_only_message("AI duplicate", cal, reason))
        return None
    return cal if cal.get("cutoff") is not None else None


def labels_sha256(path: Optional[Path] = None) -> str:
    return hashlib.sha256(Path(path or LABELS_PATH).read_bytes()).hexdigest()


# ----------------------------------------------------------------------
# The route, as check-duplicates runs it
# ----------------------------------------------------------------------

def assess(conn: sqlite3.Connection, pairs: Sequence[Dict[str, Any]], judge=None, escalation_client=None,
           calibration_path: Optional[Path] = None, labels_path: Optional[Path] = None,
           org_labels: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Judge the AI pairs and decide which are merged, without merging.

    Returns ``pairs``, ``judged``, ``escalated``, ``escalation_failed``,
    ``cutoff`` / ``calibrated``, ``scored`` (every judged pair, highest first -
    what a report-only run shows), ``proposals`` (empty without a calibration),
    ``chosen`` (pair ids to merge), ``different`` (pair ids labelled different
    under either label file) and ``labels``.

    ``org_labels`` are the organisation route's labelled pairs: an AI pair
    labelled there is protected (or merged) the same way; where both files label
    a pair, this one's label wins.
    """
    probabilities, escalated, failed = (score_pairs(conn, pairs, judge=judge, escalation_client=escalation_client)
                                        if pairs else ({}, 0, []))
    cal = active_calibration(calibration_path)
    cutoff = cal["cutoff"] if cal else None
    labels = {**(org_labels or {}), **labelled_pairs(labels_path, required=cal is not None)}
    different = {p for p, label in labels.items() if label == "different"}
    by_id = {p["pair_id"]: p for p in pairs}
    chosen = [] if cutoff is None else [
        pid for pid, p in probabilities.items()
        if (p >= cutoff and pid not in different) or labels.get(pid) == "same"]
    order = sorted(probabilities, key=lambda x: -probabilities[x])
    return {"pairs": len(pairs), "judged": len(probabilities), "escalated": escalated,
            "escalation_failed": failed, "cutoff": cutoff, "calibrated": cal is not None,
            "window_days": AI_WINDOW_DAYS,
            "scored": [{**by_id[pid], "p_same": probabilities[pid], "label": labels.get(pid)} for pid in order],
            "proposals": [{**by_id[pid], "p_same": probabilities.get(pid), "label": labels.get(pid),
                           "route": ROUTE_AI}
                          for pid in sorted(chosen, key=lambda x: -(probabilities.get(x) or 0))],
            "chosen": chosen, "different": different, "labels": labels, "probabilities": probabilities,
            "calibration": cal}
