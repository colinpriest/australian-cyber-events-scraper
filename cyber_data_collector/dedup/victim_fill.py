"""Give an incident with no usable victim the organisation its own evidence says it happened to.

On 2026-10-29, 186 of 1,090 active events had no ``victim`` role and another
nine only a placeholder. ``check-victim-attribution`` judges recorded
event-victim pairs, so an event with no victim is never looked at; role
classification, shown a blank "detail" for 97% of events, had labelled every
entity of the OpenAI-agent coverage a bystander.

**Why a separate step, not an extension of attribution.** Attribution asks "is
the recorded victim right?" and its calibrated cut-off is the probability of a
*yes* for a named pair; choosing a victim where none is recorded is a different
question over a different population (all candidates, no recorded answer to
anchor on), so it needs its own threshold, its own labels and its own audit
table. Both steps share the candidate builder
(:func:`victim_judgement.replacement_candidates`) and the adoption code
(:func:`victim_agreement.apply_victim`), so a candidate the text names helps both.

**Population.** An active event whose victim roles are none, or only unusable
names (descriptions judged by :mod:`name_judgement`, products, threat actors,
collectives, regulators that only receive reports). An event with linked
organisations all labelled bystander is in it.

**The choice.** Candidates are what the event's evidence names: each record's
Perplexity victim, every linked organisation whatever its role, a usable scalar
victim, the organisations its text names (:mod:`event_organisations`), and the
organisation a linked product is named after (:func:`alias_learning.product_hosts`),
folded through the alias tables (Medicare is Services Australia) and without the
parties that did the attacking. Jev chooses among them or ``none``
(``fill_victim``, a Choice); a leading option under :data:`SETTLED_AT` is
re-judged by the reasoning model over the same evidence and its distribution
replaces Jev's. A failed escalation or extraction leaves the event undecided (an
error, exit non-zero), never filled.

**Acting** (``fill-victims --apply``) needs a calibration
(``evaluation/victim_fill_calibration.json``, from
``scripts/calibrate_victim_fill.py``) measured on the current models, questions
and evidence: the best candidate's probability at or above the calibrated cut-off,
and the records' text naming it (under any spelling). Labels are ground truth
(``evaluation/victim_fill_labels.csv``): an event labelled ``none_named`` is never
filled; one labelled ``named`` takes its labelled victim when that is among the
candidates, calibrated or not. The change is ledger-snapshotted (reversible) and
written to ``VictimFillActions``. Idempotent: a filled event has a victim and
leaves the population; stored answers are reused.
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from cyber_data_collector.dedup import calibration_guard
from cyber_data_collector.utils.llm_models import openai_fast_model, openai_model, typesafe_model

logger = logging.getLogger(__name__)

QUESTION_ID = "fill_victim"
QUESTION_VERSION = "victim-fill-v1"
ESCALATION_QUESTION_ID = "fill_victim_escalated"
ESCALATION_VERSION = "victim-fill-escalation-v1"
#: A leading option at or above this is settled; below it the reasoning model re-judges (fixed before calibration).
SETTLED_AT = 0.9
#: Bump when what the choice is shown, or the population, changes.
EVIDENCE_VERSION = "victim-fill-evidence-v1"

ROOT = Path(__file__).resolve().parents[2]
LABELS_PATH = ROOT / "evaluation" / "victim_fill_labels.csv"
CALIBRATION_PATH = ROOT / "evaluation" / "victim_fill_calibration.json"
LABEL_VALUES = ("named", "none_named", "unsure")

ACTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS VictimFillActions (
    action_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id              TEXT NOT NULL,
    deduplicated_event_id TEXT NOT NULL,
    action                TEXT NOT NULL,     -- filled
    new_victim            TEXT NOT NULL,
    choice_probability    REAL,
    cutoff                REAL,
    candidates_json       TEXT,
    replaced_json         TEXT,              -- unusable victims demoted: [[entity_id, name], ...]
    calibration_json      TEXT,
    reason                TEXT,
    roles_before_json     TEXT,              -- the event's entity roles before the change (for reversal)
    member_signature      TEXT,
    created_at            TEXT NOT NULL
);
"""

INSTRUCTIONS = (
    "This stored cyber-incident event has no recorded victim. From the organisations the event's own evidence "
    "names (the options), choose the one whose systems, accounts or data were actually compromised or exposed. "
    "The coverage may name the target only by a service, website or portal it runs; then the organisation that "
    "runs it is the victim (`candidate_context` says where the text names an option, and a product named after "
    "an organisation). For a supplier breach choose the organisation the records report as affected. An "
    "organisation that carried out the attack, ran the tool or agent that did (`attacking_parties`), only "
    "reported, regulated or commented is NOT the victim. Choose `none` when none of them is, when several are "
    "co-equal and no one stands out, or when the evidence does not say."
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(ACTIONS_SCHEMA)


# ---------------------------------------------------------------- identity

def evidence_fingerprint() -> str:
    from cyber_data_collector.dedup import (
        entity_profiles, event_organisations, event_reality, name_judgement, victim_judgement as vj,
    )

    return calibration_guard.fingerprint(
        "victim-fill", version=EVIDENCE_VERSION, max_candidates=vj.MAX_CANDIDATES, settled_at=SETTLED_AT,
        candidates=vj.EVIDENCE_VERSION, extraction=event_organisations.EXTRACTION_VERSION,
        names=name_judgement.QUESTION_VERSION, profiles=entity_profiles.PROMPT_VERSION,
        events=event_reality.evidence_fingerprint())


def calibration_identity() -> Dict[str, str]:
    """What a calibration must have been measured on: both questions, the models (the fast one extracts), evidence."""
    from cyber_data_collector.dedup import event_organisations

    return {"question_version": f"{QUESTION_VERSION}+{ESCALATION_VERSION}+{event_organisations.EXTRACTION_VERSION}",
            "model": f"{typesafe_model()}+{openai_model()}+{openai_fast_model()}", "evidence": evidence_fingerprint()}


def active_calibration(path: Path = CALIBRATION_PATH) -> Optional[Dict[str, Any]]:
    """The calibration, only if measured on the current models, questions and evidence (else logged, None)."""
    try:
        cal = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("No victim-fill calibration (%s): REPORT ONLY (labelled events are still filled)", exc)
        return None
    reason = calibration_guard.mismatch(cal, calibration_identity())
    if reason:
        logger.warning("VICTIM FILL IS REPORT ONLY. %s", calibration_guard.report_only_message("victim fill", cal, reason))
        return None
    if cal.get("cutoff") is None:
        logger.warning("VICTIM FILL IS REPORT ONLY: no cut-off reached the precision target")
        return None
    from cyber_data_collector.dedup import deployment

    return deployment.gate("victim-fill", cal, Path(path))


def load_labels(path: Path = LABELS_PATH, required: bool = False) -> Dict[str, Dict[str, str]]:
    """Labels by event id. With ``required`` (``--apply``), a missing, unreadable or empty file raises."""
    try:
        with Path(path).open(encoding="utf-8", newline="") as fh:
            labels = {r["deduplicated_event_id"]: r for r in csv.DictReader(fh) if r.get("deduplicated_event_id")}
    except (OSError, csv.Error, KeyError) as exc:
        if required:
            raise RuntimeError(f"Victim fill labels unreadable ({path}): {exc}; refusing to act") from exc
        return {}
    if required and not labels:
        raise RuntimeError(f"Victim fill labels file {path} is empty; refusing to act")
    return labels


# ---------------------------------------------------------------- population

def victimless_events(conn: sqlite3.Connection, only: Optional[str] = None) -> List[Dict[str, Any]]:
    """Active events whose victim roles are none, or all unusable. ``unusable`` lists those roles (id, name)."""
    from cyber_data_collector.dedup.victim_agreement import _is_unusable_victim

    victims: Dict[str, List[Tuple[int, str]]] = {}
    for did, entity_id, name in conn.execute(
            """SELECT dee.deduplicated_event_id, v.entity_id, v.entity_name FROM DeduplicatedEventEntities dee
               JOIN EntitiesV2 v ON v.entity_id = dee.entity_id WHERE dee.relationship_type = 'victim'
               ORDER BY v.entity_id"""):
        victims.setdefault(did, []).append((entity_id, name))
    out = []
    for did, title, event_date, scalar in conn.execute(
            """SELECT deduplicated_event_id, title, event_date, victim_organization_name FROM DeduplicatedEvents
               WHERE status = 'Active' ORDER BY deduplicated_event_id"""):
        if only and not did.startswith(only):
            continue
        roles = victims.get(did, [])
        if any(not _is_unusable_victim(name) for _, name in roles):
            continue
        out.append({"deduplicated_event_id": did, "title": title, "event_date": event_date, "scalar": scalar,
                    "unusable": roles})
    return out


# ---------------------------------------------------------------- the choice

def _options(candidates: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    options = {f"org_{i}": c["name"] for i, c in enumerate(candidates, 1)}
    options["none"] = "None of these is clearly the organisation the incident happened to."
    return options


def _final_distribution(answer, candidates, conn, dedup_id, state, escalation_client) -> Tuple[Dict[str, float], bool]:
    """Jev's distribution, or the reasoning model's when Jev's leading option is unsettled. Raises when that fails."""
    from cyber_data_collector.utils.escalation import escalate_choice

    probs = {k: float(v) for k, v in (answer.probabilities or {}).items()}
    if not probs or max(probs.values()) >= SETTLED_AT:
        return probs, False
    question = ("Which organisation did the incident this stored event describes happen to? "
                + INSTRUCTIONS)
    escalated = escalate_choice(conn, "event_victim_fill", dedup_id, ESCALATION_QUESTION_ID, ESCALATION_VERSION,
                                state, question, _options(candidates), client=escalation_client)
    if escalated is None:
        raise EscalationFailed(f"Escalation of the victim choice for {dedup_id} failed; see the log")
    return escalated, True


class EscalationFailed(RuntimeError):
    """The reasoning model could not settle an unsettled choice; the event is left undecided."""


def choose_victim(conn: sqlite3.Connection, dedup_id: str, judge, extractor=None, escalation_client=None,
                  use_text: bool = True, stored_text_only: bool = False) -> Dict[str, Any]:
    """The best-supported victim candidate for an event with none recorded.

    Returns ``{"name", "probability", "candidates", "escalated", "best", "best_probability"}``: ``name`` is the
    leading candidate when it beats ``none`` (else None), ``probability`` its final probability.
    ``stored_text_only`` reads only extractions already stored (a dry run that asks no model). Raises
    :class:`event_organisations.ExtractionFailed` or :class:`EscalationFailed`.
    """
    from cyber_data_collector.dedup import event_organisations
    from cyber_data_collector.dedup import victim_judgement as vj

    organisations = None
    if use_text:
        organisations = event_organisations.named_organisations(conn, dedup_id, extractor=extractor,
                                                                stored_only=stored_text_only)
    needs_extraction = bool(use_text and stored_text_only and organisations is None)
    candidates = vj.replacement_candidates(conn, dedup_id, [], organisations=organisations, include_scalar=True)
    if not candidates:
        return {"name": None, "probability": None, "candidates": [], "escalated": False, "best": None,
                "best_probability": None, "needs_extraction": needs_extraction}
    state = vj.candidate_state(conn, dedup_id, candidates, vj.acting_parties(conn, dedup_id, organisations))
    state["recorded_victim"] = None
    answer = judge.ask("event_victim_fill", dedup_id, QUESTION_VERSION, state,
                       {QUESTION_ID: vj._replacement_question(candidates, INSTRUCTIONS)})[QUESTION_ID]
    final, escalated = _final_distribution(answer, candidates, conn, dedup_id, state, escalation_client)
    scored = [(final.get(f"org_{i}", 0.0), c) for i, c in enumerate(candidates, 1)]
    best_p, best = max(scored, key=lambda t: t[0])
    chosen = best_p > final.get("none", 0.0)
    return {"name": best["name"] if chosen else None, "probability": best_p if chosen else None,
            "candidates": candidates, "escalated": escalated, "best": best["name"], "best_probability": best_p,
            "needs_extraction": needs_extraction}


def stored_best_probabilities(conn: sqlite3.Connection) -> Dict[str, float]:
    """The best candidate's final stored probability per event: the escalation's where there is one, else Jev's."""
    def latest(question_id: str, model: str) -> Dict[str, Dict[str, Any]]:
        found: Dict[str, Dict[str, Any]] = {}
        for sid, pj in conn.execute(
                "SELECT subject_id, probabilities_json FROM ModelJudgements WHERE question_id = ? "
                "AND model_requested = ? ORDER BY created_at, rowid", (question_id, model)):
            found[sid] = json.loads(pj or "{}")
        return found

    def best(probs: Dict[str, Any]) -> float:
        return max([float(v) for k, v in probs.items() if k.startswith("org_")] or [0.0])

    jev = latest(QUESTION_ID, typesafe_model())
    escalated = latest(ESCALATION_QUESTION_ID, openai_model())
    out = {}
    for sid, probs in jev.items():
        settled = probs and max(float(v) for k, v in probs.items() if k == "none" or k.startswith("org_")) >= SETTLED_AT
        if settled:
            out[sid] = best(probs)
        elif sid in escalated:
            out[sid] = best(escalated[sid])
    return out


def names_match(conn: sqlite3.Connection, left: Optional[str], right: Optional[str]) -> bool:
    """Two organisation names are the same organisation: equal, a spelling of one another, or aliases of one."""
    from cyber_data_collector.dedup.org_timeline import is_same_name
    from cyber_data_collector.dedup.victim_judgement import canonical_candidate_name

    if not left or not right:
        return False
    a, b = canonical_candidate_name(conn, left), canonical_candidate_name(conn, right)
    return a.lower() == b.lower() or is_same_name(a, b)


# ---------------------------------------------------------------- planning and acting

def _named_in_records(conn: sqlite3.Connection, dedup_id: str, candidate: Dict[str, Any]) -> bool:
    from cyber_data_collector.dedup.victim_judgement import _named_in_records as named

    if "text" in candidate.get("sources", ()):
        return True        # the extraction kept it only because it occurs verbatim in the text it read
    return any(named(conn, dedup_id, spelling) for spelling in [candidate["name"], *candidate.get("spellings", [])])


def plan_fills(conn: sqlite3.Connection, events: Sequence[Dict[str, Any]], cal: Optional[Dict[str, Any]],
               labels: Dict[str, Dict[str, str]], judge, extractor=None, escalation_client=None,
               use_text: bool = True, stored_text_only: bool = False,
               errors: Optional[List[Dict[str, str]]] = None) -> List[Dict[str, Any]]:
    """What ``--apply`` would do for every event in the population; changes nothing.

    One plan per event: ``filled`` (with ``new_victim``) or ``unfilled`` (with the reason). An event whose
    extraction or escalation failed gets no plan and is appended to ``errors`` (raised when None).
    """
    from cyber_data_collector.dedup.event_organisations import ExtractionFailed

    plans = []
    for event in events:
        did = event["deduplicated_event_id"]
        label = labels.get(did) or {}
        try:
            choice = choose_victim(conn, did, judge, extractor=extractor, escalation_client=escalation_client,
                                   use_text=use_text, stored_text_only=stored_text_only)
        except (ExtractionFailed, EscalationFailed) as exc:
            if errors is None:
                raise
            logger.error("Victim fill for %s not planned: %s", did, exc)
            errors.append({"deduplicated_event_id": did, "error": str(exc)})
            continue
        plan = {**event, "candidates": choice["candidates"], "choice_probability": choice["probability"],
                "best": choice["best"], "best_probability": choice["best_probability"],
                "action": "unfilled", "new_victim": None}
        by_name = {c["name"]: c for c in choice["candidates"]}
        wanted = (label.get("correct_victim") or "").strip()
        if label.get("label") == "none_named":
            plan["reason"] = "labelled none_named: the evidence names no victim"
        elif label.get("label") == "named":
            hit = next((c for c in choice["candidates"] if names_match(conn, c["name"], wanted)), None)
            if hit is None:
                plan["reason"] = f"labelled victim {wanted!r} is not among the evidence's candidates"
            else:
                plan.update(action="filled", new_victim=hit["name"], reason="labelled", chosen=hit)
        elif choice["name"] is None:
            plan["reason"] = ("the evidence names no candidate" if not choice["candidates"]
                              else "no candidate chosen over none")
        elif cal is None:
            plan["reason"] = (f"report only: no calibration for these models and questions "
                              f"(best {choice['name']!r} at {choice['probability']:.2f})")
        elif choice["probability"] < cal["cutoff"]:
            plan["reason"] = (f"{choice['name']!r} below the cut-off ({choice['probability']:.2f} < "
                              f"{cal['cutoff']:.2f})")
        elif not _named_in_records(conn, did, by_name[choice["name"]]):
            plan["reason"] = f"{choice['name']!r} is not named in the records' text"
        else:
            plan.update(action="filled", new_victim=choice["name"], chosen=by_name[choice["name"]],
                        reason=f"chosen among {len(choice['candidates'])} evidence candidate(s)")
        plans.append(plan)
    return plans


def apply_fills(conn: sqlite3.Connection, plans: Sequence[Dict[str, Any]], cal: Optional[Dict[str, Any]]) -> str:
    """Make the planned fills; snapshot each event first and audit every change. Returns the batch id."""
    from cyber_data_collector.dedup.ledger import DedupLedger
    from cyber_data_collector.dedup.role_maintenance import member_signature
    from cyber_data_collector.dedup.victim_agreement import apply_victim
    from cyber_data_collector.dedup.victim_judgement import _demote, replacement_entity

    ensure_schema(conn)
    ledger = DedupLedger(conn)
    batch_id = f"victim-fill-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    now = datetime.now().isoformat()
    cal_json = json.dumps({k: (cal or {}).get(k) for k in ("question_version", "model", "cutoff",
                                                           "calibrated_on_database", "generated")})
    try:
        for plan in plans:
            if plan["action"] != "filled":
                continue
            did = plan["deduplicated_event_id"]
            roles_before = json.dumps([[r[0], r[1]] for r in conn.execute(
                "SELECT entity_id, relationship_type FROM DeduplicatedEventEntities "
                "WHERE deduplicated_event_id = ? ORDER BY entity_id", (did,))])
            ledger.snapshot_event(batch_id, did)
            for entity_id, _name in plan["unusable"]:
                _demote(conn, did, entity_id)
            entity_id = replacement_entity(conn, did, plan["new_victim"], 0)
            stored = apply_victim(conn, did, None, plan["new_victim"], entity_id=entity_id)
            plan["new_victim"] = stored
            conn.execute(
                "INSERT INTO VictimFillActions (batch_id, deduplicated_event_id, action, new_victim, "
                "choice_probability, cutoff, candidates_json, replaced_json, calibration_json, reason, "
                "roles_before_json, member_signature, created_at) VALUES (?, ?, 'filled', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (batch_id, did, stored, plan.get("choice_probability"), (cal or {}).get("cutoff"),
                 json.dumps([{k: c.get(k) for k in ("name", "support", "sources")} for c in plan["candidates"]]),
                 json.dumps([[e, n] for e, n in plan["unusable"]]), cal_json, plan.get("reason"), roles_before,
                 member_signature(conn, did), now))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return batch_id


def stored_candidates(conn: sqlite3.Connection, only_event: Optional[str] = None) -> List[Dict[str, Any]]:
    """Each eligible event with the candidates its *stored* evidence gives; asks no model, writes nothing.

    ``stored`` are the candidates before any text extraction; ``with_text`` adds those from an extraction
    already stored (None when there is none, which a live run would ask the fast model for).
    """
    from cyber_data_collector.dedup import event_organisations
    from cyber_data_collector.dedup import victim_judgement as vj

    out = []
    for event in victimless_events(conn, only_event):
        did = event["deduplicated_event_id"]
        base = vj.replacement_candidates(conn, did, [], include_scalar=True)
        extracted = event_organisations.named_organisations(conn, did, stored_only=True)
        out.append({**event, "stored": [c["name"] for c in base],
                    "with_text": None if extracted is None else
                    [c["name"] for c in vj.replacement_candidates(conn, did, [], organisations=extracted,
                                                                  include_scalar=True)]})
    return out


def fill_victims(conn: sqlite3.Connection, apply: bool = False, judge=None, escalation_client=None,
                 extractor=None, calibration_path: Path = CALIBRATION_PATH, labels_path: Path = LABELS_PATH,
                 only_event: Optional[str] = None, use_text: bool = True,
                 stored_text_only: bool = False) -> Dict[str, Any]:
    """Choose a victim for every active event that has none; with a calibration, fill (with ``apply``).

    Raises when ``apply`` is asked for under a calibration and the labels are missing, unreadable or empty.
    Without a matching calibration only labelled events are filled; every other event is reported with
    its best candidate.
    ``stored_text_only`` is a dry run that asks no model for the text extraction (events with no stored
    extraction are listed as ``needs_extraction``).
    """
    from cyber_data_collector.utils.jev import JevJudge

    cal = active_calibration(calibration_path)
    # Labels protect what a calibrated cut-off would act on; with no calibration nothing is acted on but
    # labelled events, so a missing file then simply means none are labelled yet.
    labels = load_labels(labels_path, required=apply and cal is not None)
    owned = judge is None
    judge = judge or JevJudge(conn)
    errors: List[Dict[str, str]] = []
    try:
        events = victimless_events(conn, only_event)
        plans = plan_fills(conn, events, cal, labels, judge, extractor=extractor, escalation_client=escalation_client,
                           use_text=use_text, stored_text_only=stored_text_only, errors=errors)
    finally:
        if owned:
            judge.close()
    batch = apply_fills(conn, plans, cal) if apply and any(p["action"] == "filled" for p in plans) else None
    counts = Counter(p["action"] for p in plans)
    return {"eligible": len(events), "calibrated": cal is not None, "cutoff": cal["cutoff"] if cal else None,
            "applied": batch is not None, "batch_id": batch, "counts": dict(counts), "errors": errors,
            "plans": [{**{k: v for k, v in p.items() if k not in ("chosen", "candidates")},
                       "candidates": [c["name"] for c in p["candidates"]]} for p in plans]}
