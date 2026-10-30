"""Is a stored event within the dataset's scope: does it affect Australia?

Scope was checked only at discovery, by the fast model's ``is_australian_event``
(which dedup then writes as a constant ``True``), and afterwards by one manual
``out_of_scope`` label in ``evaluation/event_reality_labels.csv``. On 2026-10-29
"OpenAI bots meddled with multiple US government agency sites - BBC" (victim
"Education Department": the US one, with the SEC and the Census Bureau) was an
active event; no calibrated judgement had ever asked the question of it.

This asks Jev one question about each active event, over the same evidence the
event-reality judgement sees (:func:`event_reality.build_state`: every active
record's page text and Perplexity research, plus the victim's researched
timeline) but not the corroboration search, which is about whether the
incident happened. The answer is P(in scope), stored in ``ModelJudgements``.
The middle band goes to the reasoning model (:mod:`utils.escalation`); the
*final* score is the escalation's where there is one, else Jev's.

Rejection (``dedup_admin.py check-scope --apply``) uses a **calibrated**
cut-off, never a default: blind labels of this database's events
(``evaluation/event_scope_labels.csv``, folded with the ``out_of_scope``
labels of ``event_reality_labels.csv``) fix the largest
cut-off whose rejections are at least 95% labelled out of scope, and
``evaluation/event_scope_calibration.json`` records it with the models, the
question versions and the evidence fingerprint it was measured on.
:func:`active_calibration` returns it only while all three still match
(:mod:`calibration_guard`); otherwise the step reports. Labels are ground
truth both ways: a labelled out-of-scope event is rejected whatever it scores,
a labelled in-scope one never is.
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from cyber_data_collector.dedup import calibration_guard, final_decisions
from cyber_data_collector.dedup import event_reality
from cyber_data_collector.utils.llm_models import typesafe_model

logger = logging.getLogger(__name__)

QUESTION_ID = "is_in_scope"
QUESTION_VERSION = "event-scope-v1"
ESCALATION_QUESTION_ID = "is_in_scope_escalated"
ESCALATION_VERSION = "event-scope-escalation-v1"
#: Jev scores inside this band go to the reasoning model. Fixed before calibration, the band event reality uses.
ESCALATE_BAND = (0.1, 0.7)
#: Bump when what :func:`build_state` shows a model changes. The event evidence nested in the
#: fingerprint (``event_reality.EVIDENCE_VERSION`` and its parameters) moves it on its own.
EVIDENCE_VERSION = "event-scope-evidence-v1"
#: Consecutive Jev failures after which the run stops: a bad key or an outage, not one event.
MAX_CONSECUTIVE_FAILURES = 5
#: Events listed in the findings when nothing is rejected, lowest score first.
REPORT_LOWEST = 60
BATCH_PREFIX = "reject-event-scope"

ROOT = event_reality.ROOT
LABELS_PATH = ROOT / "evaluation" / "event_scope_labels.csv"
CALIBRATION_PATH = ROOT / "evaluation" / "event_scope_calibration.json"
LABELS = ("in_scope", "out_of_scope", "unsure")
LABEL_COLUMNS = ("deduplicated_event_id", "label", "rationale", "labeller")

INSTRUCTIONS = (
    "This database records cyber incidents that affected Australia. Decide whether this stored event "
    "belongs in it, judging by who was actually harmed and where, as the records describe it. IN SCOPE: "
    "the affected organisation is Australian (an Australian company, government body or institution, "
    "including its operations abroad), or it is the Australian arm or operations of a foreign "
    "organisation, or the reporting says people or organisations in Australia were among those "
    "affected - a global or supplier breach that reached Australian customers, staff or organisations, "
    "or a foreign company's breach that exposed Australian customers' data. OUT OF SCOPE: the reporting "
    "identifies who was harmed and none of them is in Australia - the victims and the impact are "
    "outside Australia, even in a large incident that Australia reacts to. A reaction is not a victim. "
    "These do NOT bring an event into scope: an Australian outlet reporting a foreign incident; an "
    "Australian regulator, government, politician or commentator condemning, investigating or "
    "responding to a foreign incident; advice on what Australian organisations should learn from it; "
    "speculation that it could or may affect Australia; a threat actor or tool that also targets "
    "Australia in general; a separate Australian incident mentioned as background or comparison; or the "
    "victim merely having an Australian office the incident did not touch. "
    "Judge by the people and organisations the records say were harmed, not by `event.victim`: the "
    "recorded victim can be wrong - it may be the attacker, a vendor or a bystander, or a name that "
    "organisations in several countries share - and one incident can have both foreign and Australian "
    "victims, in which case an Australian victim is enough. If the records are too thin to say who was "
    "harmed or where (a stub page, empty research), do not call the event out of scope. "
    "`records[].page_text` is the page stored for each record and may be unrelated or thin; "
    "`records[].perplexity` is separate research about the record (the organisation it identified, "
    "its reasoning, sources, dates)."
)
TIMELINE_INSTRUCTIONS = (
    " `organisation_timeline`, when present, is researched history of the victim organisation's "
    "publicly reported cyber incidents in Australia, with dates and sources. An incident listed there "
    "is evidence of Australian involvement; absence from it is not evidence either way."
)
CRITERIA = {
    "true": ("An Australian organisation, or an Australian arm of a foreign one, was affected, or the "
             "reporting says people or organisations in Australia were among those affected, or the "
             "records are too thin to say who was harmed."),
    "false": ("The reporting identifies who was harmed and none of them is in Australia: a foreign "
              "incident with no Australian customers, staff, operations or organisations affected, "
              "however Australian outlets, regulators or commentators respond to it."),
}


class ScopeJudgementError(RuntimeError):
    """Jev kept failing; nothing was rejected."""


class ScopeConfigError(RuntimeError):
    """A labels or calibration file needed to act is missing or unreadable; nothing was rejected."""


def build_state(conn: sqlite3.Connection, dedup_id: str) -> Dict[str, Any]:
    """Everything Jev sees about one event: its records and the victim's stored timeline."""
    return event_reality.build_state(conn, dedup_id, corroborated=False, with_timeline=True)


def question():
    from typesafe_sdk import Noul, NoulCriteria
    return Noul(instructions=INSTRUCTIONS + TIMELINE_INSTRUCTIONS, criteria=NoulCriteria(**CRITERIA))


def escalation_question() -> str:
    return (INSTRUCTIONS + TIMELINE_INSTRUCTIONS + f" YES means: {CRITERIA['true']} NO means: {CRITERIA['false']}"
            " Is this stored event within the scope of a database of cyber incidents that affected Australia?")


def judge_events(conn: sqlite3.Connection, dedup_ids: Sequence[str], judge=None, refresh: bool = False,
                 failed: Optional[List[str]] = None,
                 states: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, float]:
    """P(in scope) for each event; stored answers are reused (see :mod:`jev`).

    An event Jev fails on is appended to ``failed`` and left undecided. Repeated consecutive
    failures raise :class:`ScopeJudgementError`: a bad key or an outage should not run 1,000 times.

    ``states`` freezes the evidence: an event already in it is asked on that state, any other is
    built and added, and the caller passes the same mapping to :func:`escalate_band` and
    :func:`record_final`, so Jev, the escalation and the stored decision see one evidence.
    """
    from cyber_data_collector.utils.jev import JevJudge

    owned = judge is None
    judge = judge or JevJudge(conn)
    q = {QUESTION_ID: question()}
    out: Dict[str, float] = {}
    streak = 0
    try:
        for i, dedup_id in enumerate(dedup_ids, 1):
            if states is None:
                state = build_state(conn, dedup_id)
            else:
                if dedup_id not in states:
                    states[dedup_id] = build_state(conn, dedup_id)
                state = states[dedup_id]
            try:
                answer = judge.ask("deduplicated_event", dedup_id, QUESTION_VERSION, state, q,
                                   refresh=refresh)[QUESTION_ID]
            except Exception as exc:  # noqa: BLE001 - one failure never decides an event
                logger.warning("Event scope judgement failed for %s: %s", dedup_id, exc)
                if failed is not None:
                    failed.append(dedup_id)
                streak += 1
                if streak >= MAX_CONSECUTIVE_FAILURES:
                    raise ScopeJudgementError(
                        f"{streak} consecutive Jev failures (last: {exc}); stopping. Nothing was rejected; "
                        "stored judgements are kept.") from exc
                continue
            streak = 0
            out[dedup_id] = float(answer.value)
            if i % 50 == 0:
                logger.info("Judged scope of %d/%d events", i, len(dedup_ids))
    finally:
        if owned:
            judge.close()
    return out


def evidence_fingerprint() -> str:
    """A hash of everything that shapes the evidence a scope judgement sees (the event evidence nested)."""
    return calibration_guard.fingerprint(
        "event-scope", version=EVIDENCE_VERSION, events=event_reality.evidence_fingerprint(),
        escalate_band=list(ESCALATE_BAND), **calibration_guard.shared_parts())


def calibration_identity() -> Dict[str, str]:
    """What a calibration must have been measured on to apply: question, escalation, both models, evidence."""
    from cyber_data_collector.utils.llm_models import openai_model
    return {"question_version": f"{QUESTION_VERSION}+{ESCALATION_VERSION}",
            "model": f"{typesafe_model()}+{openai_model()}", "evidence": evidence_fingerprint()}


def escalate_band(conn: sqlite3.Connection, probabilities: Dict[str, float], client=None,
                  failed: Optional[List[str]] = None,
                  states: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, float]:
    """Re-judge with the reasoning model every event Jev scores inside ESCALATE_BAND.

    Events whose escalation fails are appended to ``failed``: they are left undecided (retried
    next run), never decided on Jev's mid-band score. With ``states`` (from :func:`judge_events`)
    each event is shown the state Jev was asked on, never one built again.
    """
    from cyber_data_collector.utils.escalation import escalate

    lo, hi = ESCALATE_BAND
    band = sorted(d for d, p in probabilities.items() if lo <= p < hi)
    out: Dict[str, float] = {}
    for i, did in enumerate(band, 1):
        state = build_state(conn, did) if states is None else final_decisions.frozen(states, did)
        value = escalate(conn, "deduplicated_event", did, ESCALATION_QUESTION_ID, ESCALATION_VERSION,
                         state, escalation_question(), client=client)
        if value is not None:
            out[did] = value
        elif failed is not None:
            failed.append(did)
        if i % 10 == 0:
            logger.info("Escalated %d/%d mid-band events", i, len(band))
    logger.info("Escalated %d of %d events Jev scored in %s", len(out), len(band), ESCALATE_BAND)
    return out


STEP = final_decisions.Step(
    name="event-scope", question_id=QUESTION_ID, version=QUESTION_VERSION, make_question=question,
    escalation_id=ESCALATION_QUESTION_ID, escalation_version=ESCALATION_VERSION,
    escalation_text=escalation_question, band=ESCALATE_BAND)


def record_final(conn: sqlite3.Connection, jev: Dict[str, float], escalated: Dict[str, float],
                 failed: Optional[Sequence[str]] = None,
                 states: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, float]:
    """Store each scored event's final decision against the evidence both answers were given; returns the final
    scores (an event whose escalation failed has none). With ``states`` (from :func:`judge_events`) the decision is
    stored against the state Jev was asked on; without, each state is built here (callers that do not freeze)."""
    def build(d: str):
        return (lambda: build_state(conn, d)) if states is None else (lambda: final_decisions.frozen(states, d))
    return final_decisions.record_scores(conn, STEP, [(d, build(d)) for d in jev], jev, escalated, failed or ())


def stored_final_probabilities(conn: sqlite3.Connection, ids: Sequence[str]) -> Dict[str, float]:
    """The final score per event from its decision on the evidence it has **now** (:mod:`final_decisions`); an
    event with no decision on its current evidence has no score here."""
    return final_decisions.read_current(conn, STEP, [(d, lambda d=d: build_state(conn, d)) for d in ids])


def active_calibration(path: Path = CALIBRATION_PATH) -> Optional[Dict[str, Any]]:
    """The calibration, only if it was measured on the current models, questions and evidence processing."""
    try:
        cal = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("No event scope calibration (%s): REPORT ONLY, nothing will be rejected. Label a sample "
                       "and run scripts/calibrate_event_scope.py.", path)
        return None
    except (OSError, ValueError) as exc:
        logger.error("Event scope calibration unreadable (%s): %s - REPORT ONLY, nothing will be acted on",
                     path, exc)
        return None
    reason = calibration_guard.mismatch(cal, calibration_identity())
    if reason:
        logger.warning(calibration_guard.report_only_message("event scope", cal, reason))
        return None
    if cal.get("cutoff") is None:
        return None
    from cyber_data_collector.dedup import deployment

    return deployment.gate("event-scope", cal, Path(path))


# ----------------------------------------------------------------------
# Labels
# ----------------------------------------------------------------------

def _read_rows(path: Path) -> List[Dict[str, str]]:
    with Path(path).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def scope_labels(path: Path = LABELS_PATH, required: bool = True) -> Dict[str, str]:
    """``{event id: label}`` from the scope labels file (``in_scope`` / ``out_of_scope`` / ``unsure``).

    Raises on a missing or unreadable file and on a label outside :data:`LABELS`; with ``required``
    also on an empty file: acting without the ground truth that protects known events is refused.
    """
    try:
        rows = _read_rows(path)
    except (OSError, ValueError) as exc:
        raise ScopeConfigError(f"event scope labels unreadable ({path}): {exc}; refusing to reject") from exc
    if not rows and required:
        raise ScopeConfigError(f"event scope labels empty ({path}); refusing to reject")
    out: Dict[str, str] = {}
    for r in rows:
        label = (r.get("label") or "").strip()
        if label not in LABELS:
            raise ScopeConfigError(f"event scope labels {path}: {r.get('deduplicated_event_id')!r} has label "
                                   f"{label!r}, expected one of {', '.join(LABELS)}")
        out[r["deduplicated_event_id"].strip()] = label
    return out


def reality_scope_labels(path: Path = event_reality.LABELS_PATH) -> Dict[str, str]:
    """The scope the event-reality labels imply: only ``out_of_scope`` says anything about it.

    ``real`` means a real incident, not an Australian one - Mazda's and Tesla's breaches are labelled
    ``real`` - so it implies nothing about scope; folding it in as ``in_scope`` (until 2026-10-29) put
    real foreign incidents among the in-scope labels and held the calibrated cut-off at 0.03.
    ``not_incident`` and ``unsure`` say nothing either. An explicit scope label wins (see :func:`labels`).
    """
    mapping = {"out_of_scope": "out_of_scope"}
    return {r["deduplicated_event_id"].strip(): mapping[r["label"].strip()]
            for r in _read_rows(path) if (r.get("label") or "").strip() in mapping}


def labels(scope_path: Path = LABELS_PATH, reality_path: Path = event_reality.LABELS_PATH,
           required: bool = True) -> Dict[str, str]:
    """Every scope label: the event-reality labels' implied scope, overridden by the scope file's own.

    The scope file must be readable; with ``required`` there must be at least one label from either file."""
    merged = reality_scope_labels(reality_path)
    merged.update(scope_labels(scope_path, required=False))
    if required and not merged:
        raise ScopeConfigError(f"event scope labels empty ({scope_path}) and the event-reality labels imply "
                               "none; refusing to reject")
    return merged


# ----------------------------------------------------------------------
# Acting
# ----------------------------------------------------------------------

def reject_events(conn: sqlite3.Connection, findings: Sequence[Dict[str, Any]]) -> None:
    """Reject events judged out of scope; snapshotted by the ledger (batch ``reject-event-scope-*``), so reversible."""
    event_reality.reject_events(conn, findings, batch_prefix=BATCH_PREFIX, reason="out of scope")


def restore_protected(conn: sqlite3.Connection, protected: set) -> List[str]:
    """Undo this filter's rejection of any event labelled in scope (latest snapshot)."""
    return event_reality.restore_protected(conn, protected, batch_prefix=BATCH_PREFIX)


def _select(conn: sqlite3.Connection, only: Optional[Sequence[str]]) -> List[str]:
    events = event_reality.active_events(conn)
    if only:
        events = [e for e in events if any(e.startswith(p) for p in only)]
    return events


def check_scope(conn: sqlite3.Connection, apply: bool = False, judge=None,
                calibration_path: Path = CALIBRATION_PATH, labels_path: Path = LABELS_PATH,
                reality_labels_path: Path = event_reality.LABELS_PATH, escalation_client=None,
                only: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Judge every active event's scope, escalate the middle band, and reject below the calibrated cut-off.

    Without an active calibration, or without ``apply``, nothing is rejected: the would-be
    rejections are returned for review. With both, events below the cut-off and events labelled out
    of scope are rejected (snapshotted); events labelled in scope never are. An event Jev or the
    escalation failed on is in ``judgement_failed`` / ``escalation_failed`` and is left alone.

    Each event's evidence is built once: Jev is asked on it, the escalation is given it and the
    decision is stored against it. Immediately before rejecting, every event its score would reject
    is built again; one whose evidence changed since it was judged is not rejected but listed in
    ``evidence_changed`` and judged again next run.
    """
    events = _select(conn, only)
    cal = active_calibration(calibration_path)
    cutoff = cal["cutoff"] if cal else None
    # The labels are needed (and must be readable) only when this run could act on them - and, being read first,
    # they decide first: an event labelled in scope is never rejected and one labelled out of scope is rejected,
    # whatever the model says, so neither is (re-)asked about.
    known = labels(labels_path, reality_labels_path) if apply and cal else {}
    protected = {d for d, label in known.items() if label == "in_scope"}
    in_run = set(events)
    labelled_out = sorted(d for d, label in known.items() if label == "out_of_scope" and d in in_run)
    judged_events = [d for d in events if d not in protected and d not in set(labelled_out)]
    judgement_failed: List[str] = []
    states: Dict[str, Dict[str, Any]] = {}      # the evidence each judgement is made on, built once
    jev_scores = judge_events(conn, judged_events, judge=judge, failed=judgement_failed, states=states)
    escalation_failed: List[str] = []
    escalated = escalate_band(conn, jev_scores, client=escalation_client, failed=escalation_failed, states=states)
    probabilities = record_final(conn, jev_scores, escalated, escalation_failed, states=states)
    meta = {r[0]: r[1:] for r in conn.execute(
        "SELECT deduplicated_event_id, title, victim_organization_name, event_date FROM DeduplicatedEvents")}

    def entry(did: str, **extra: Any) -> Dict[str, Any]:
        title, victim, date = meta.get(did, (None, None, None))
        return {"deduplicated_event_id": did, "title": title, "victim": victim, "event_date": date,
                "p_in_scope": probabilities.get(did), **extra}

    below = [] if cutoff is None else [
        entry(did) for did, p in sorted(probabilities.items(), key=lambda kv: kv[1])
        if p < cutoff and did not in protected and did not in labelled_out]
    labelled_rejections = [entry(d, label="out_of_scope") for d in labelled_out] if cutoff is not None else []
    # A score is acted on only while it describes the event: an event whose evidence changed after it was judged
    # is left for the next run. Labelled rejections rest on the label, not on a score, and are not affected.
    evidence_changed: List[Dict[str, Any]] = []
    if apply and cal and below:
        changed = set(final_decisions.changed_since(
            states, [f["deduplicated_event_id"] for f in below], lambda d: build_state(conn, d)))
        evidence_changed = [f for f in below if f["deduplicated_event_id"] in changed]
        below = [f for f in below if f["deduplicated_event_id"] not in changed]
        for f in evidence_changed:
            logger.warning("NOT REJECTED: the evidence of %s changed after it was judged (P(in scope) %.3f); it is "
                           "judged again next run", f["deduplicated_event_id"], f["p_in_scope"])
    rejections = below + labelled_rejections
    applied = bool(apply and cal and rejections)
    if applied:
        reject_events(conn, rejections)
    restored = restore_protected(conn, protected) if apply and cal else []
    lowest = [entry(did) for did, _ in sorted(probabilities.items(), key=lambda kv: kv[1])[:REPORT_LOWEST]]
    return {"events": len(events), "judged": len(probabilities), "settled_by_label": len(events) - len(judged_events), "escalated": len(escalated),
            "judgement_failed": judgement_failed, "escalation_failed": escalation_failed, "cutoff": cutoff,
            "calibrated": cal is not None, "applied": applied, "rejections": rejections,
            "evidence_changed": evidence_changed, "labelled_rejections": labelled_rejections, "restored_protected": restored,
            "lowest_scoring": lowest, "distribution": _distribution(probabilities.values())}


def _distribution(values) -> Dict[str, int]:
    """Counts per 0.1 band of final P(in scope), to show where the mass lies when nothing is calibrated."""
    bands = {f"{i / 10:.1f}-{(i + 1) / 10:.1f}": 0 for i in range(10)}
    for v in values:
        bands[f"{min(int(v * 10), 9) / 10:.1f}-{(min(int(v * 10), 9) + 1) / 10:.1f}"] += 1
    return bands
