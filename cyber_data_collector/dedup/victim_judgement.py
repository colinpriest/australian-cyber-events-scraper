"""Is the recorded victim really the organisation the incident happened to? Calibrated re-attribution.

Calibration labelling (2026-10-27) found events filed against the wrong
organisation that the deterministic ``check-victims`` rule cannot see,
because the wrong name is all over the records: a NSW driver-licence exposure
under Optus, Aussie Fluid Power under the AFP, UN Women Australia under the
University of Western Australia, Ambulance Tasmania under Ambulance Victoria,
TAFE SA under Melbourne TAFE.

**The judgement.** For every active event and each organisation recorded as
its victim, Jev is asked whether that organisation is the one the incident
happened to (``is_recorded_victim``, a Noul), seeing the event's evidence -
every record's page text and Perplexity research, any stored corroboration
search - the event's other recorded victims, and the organisation's researched
incident timeline when it has one (:mod:`org_timeline`, with its warning when
the history may be incomplete). Answers in :data:`ESCALATE_BAND` are re-judged
by the reasoning model (:mod:`utils.escalation`); the final score is the
escalation's where there is one, else Jev's. Everything is stored in
``ModelJudgements``.

**The replacement.** The correct victim is never invented. Candidates are the
organisations the event's own evidence names - each record's Perplexity
victim (``formal_entity_name``) and the organisations linked to its records -
less the recorded victim(s), spellings of them, collectives, products and
threat actors. Jev chooses among them, or ``none`` (``replacement_victim``, a
Choice).

**Acting** (``check-victim-attribution --apply``) needs a calibration
(``evaluation/victim_attribution_calibration.json``, from
``scripts/calibrate_victim_attribution.py``) measured on the current models
and question versions, and a readable labels file
(``evaluation/victim_attribution_labels.csv``); without either it raises
rather than act unprotected. For each event-victim pair scored below the
calibrated cut-off:

* when the event has another recorded victim that is not itself below the
  cut-off, the wrong one is demoted to ``bystander`` (co-equal victims of a
  supplier breach keep theirs);
* otherwise the replacement is adopted only when Jev's choice is a candidate
  at or above the calibrated replacement threshold and the records' text names
  it; it is linked with :func:`victim_agreement.apply_victim`, the same
  re-attribution ``check-victims`` uses (victim role on the event and its
  records, scalar victim and industry, old victim demoted to bystander);
* otherwise the event is **flagged** and left unchanged.

Labels are ground truth: a pair labelled ``correct`` is never changed, and a
pair labelled ``wrong`` is re-attributed only to the victim its label names.
Every change is snapshotted by the ledger (reversible) and written to
``VictimAttributionActions`` with the scores, cut-offs and calibration that
justified it. Idempotent: once re-attributed, the old organisation is no
longer a recorded victim, so it is not judged again; stored answers are reused.
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

from cyber_data_collector.utils.llm_models import typesafe_model

logger = logging.getLogger(__name__)

QUESTION_ID = "is_recorded_victim"
# v1 (report-only) judged only events whose victim had a timeline, and asked
# nothing about co-victims or collectives. v2 judges every event-victim pair.
QUESTION_VERSION = "victim-attribution-v2"
ESCALATION_QUESTION_ID = "is_recorded_victim_escalated"
ESCALATION_VERSION = "victim-attribution-escalation-v1"
ESCALATE_BAND: Optional[Tuple[float, float]] = (0.1, 0.7)
REPLACEMENT_QUESTION_ID = "replacement_victim"
REPLACEMENT_VERSION = "victim-replacement-v1"
REPORT_BELOW = 0.5
MAX_CANDIDATES = 8

ROOT = Path(__file__).resolve().parents[2]
LABELS_PATH = ROOT / "evaluation" / "victim_attribution_labels.csv"
CALIBRATION_PATH = ROOT / "evaluation" / "victim_attribution_calibration.json"

ACTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS VictimAttributionActions (
    action_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id               TEXT NOT NULL,
    deduplicated_event_id  TEXT NOT NULL,
    entity_id              INTEGER NOT NULL,
    old_victim             TEXT NOT NULL,
    action                 TEXT NOT NULL,     -- demoted / reattributed / flagged
    new_victim             TEXT,
    p_recorded_victim      REAL NOT NULL,
    replacement_probability REAL,
    cutoff                 REAL,
    replacement_threshold  REAL,
    calibration_json       TEXT,
    reason                 TEXT,
    roles_before_json      TEXT,              -- the event's entity roles before the change (for reversal)
    created_at             TEXT NOT NULL
);
"""

INSTRUCTIONS = (
    "`recorded_victim` is the organisation this stored event is filed against. Decide whether the "
    "incident the event describes actually happened to that organisation: its systems or accounts, its "
    "data, or data about its customers, students or staff held by it or by a supplier on its behalf. "
    "Use the records' page text and Perplexity research (`records[].perplexity.formal_entity_name` is "
    "the organisation the research identified), any `corroboration` search, and `organisation_timeline` - "
    "researched history of the recorded victim's own incidents, when present (heed its `warning`, if "
    "any: an incomplete history is no evidence against an incident). One of several co-equal victims "
    "(`other_recorded_victims`, e.g. the customers of a breached supplier) counts, and so does the "
    "breached supplier itself. Legal, parent and brand names of the same organisation count. NOT its "
    "incident: an incident at a different organisation with a similar name or an acronym clash (AFP / "
    "Aussie Fluid Power), an organisation that only investigated, reported on, regulated or commented "
    "on the incident, a sector-wide story or campaign naming no breach of it, an impersonation scam "
    "that did not compromise it, or a generic or collective name ('Government', 'Clinic', 'Australian "
    "Government') when the evidence names the specific organisation affected."
)
CRITERIA = {
    "true": "The incident happened to the recorded victim (as defined above).",
    "false": "The incident happened to some other organisation, or the recorded victim was not breached.",
}
REPLACEMENT_INSTRUCTIONS = (
    "`recorded_victim` is NOT the organisation this incident happened to. From the organisations the "
    "event's own evidence names (the options), choose the one whose systems, accounts or data were "
    "actually compromised or exposed. For a supplier breach, choose the organisation the records "
    "report as affected. Choose `none` when none of them is, when several are co-equal and no one "
    "stands out, or when the evidence does not say."
)


def question():
    from typesafe_sdk import Noul, NoulCriteria
    return Noul(instructions=INSTRUCTIONS, criteria=NoulCriteria(**CRITERIA))


def escalation_question() -> str:
    return (INSTRUCTIONS + f" YES means: {CRITERIA['true']} NO means: {CRITERIA['false']}"
            " Did the incident happen to the recorded victim?")


def calibration_identity() -> Dict[str, str]:
    """What a calibration must have been measured on to apply: all three questions and both models."""
    from cyber_data_collector.utils.llm_models import openai_model
    return {"question_version": f"{QUESTION_VERSION}+{ESCALATION_VERSION}+{REPLACEMENT_VERSION}",
            "model": f"{typesafe_model()}+{openai_model()}"}


# ---------------------------------------------------------------- population

def pairs_to_check(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Every active event and each organisation recorded as its victim."""
    rows = conn.execute(
        """SELECT DISTINCT d.deduplicated_event_id, v.entity_id, v.entity_name, d.title, d.event_date
           FROM DeduplicatedEventEntities dee
           JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
           JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id
           WHERE dee.relationship_type = 'victim' AND d.status = 'Active'
           ORDER BY d.deduplicated_event_id, v.entity_id""").fetchall()
    return [{"pair_id": f"{r[0]}|{r[1]}", "deduplicated_event_id": r[0], "entity_id": r[1], "victim": r[2],
             "title": r[3], "event_date": r[4]} for r in rows]


def _victims(conn: sqlite3.Connection, dedup_id: str) -> List[Tuple[int, str]]:
    return [(r[0], r[1]) for r in conn.execute(
        """SELECT v.entity_id, v.entity_name FROM DeduplicatedEventEntities dee
           JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
           WHERE dee.deduplicated_event_id = ? AND dee.relationship_type = 'victim'
           ORDER BY v.entity_id""", (dedup_id,))]


def build_state(conn: sqlite3.Connection, dedup_id: str, entity_id: int, victim: str) -> Dict[str, Any]:
    """Everything the judgement sees about one event-victim pair. Deterministic."""
    from cyber_data_collector.dedup import event_reality
    from cyber_data_collector.dedup.org_timeline import for_judgement

    state = event_reality.build_state(conn, dedup_id, corroborated=True)
    state["recorded_victim"] = victim
    others = [name for eid, name in _victims(conn, dedup_id) if eid != entity_id]
    if others:
        state["other_recorded_victims"] = others
    timeline = for_judgement(conn, entity_id)
    if timeline:
        state["organisation_timeline"] = {k: v for k, v in timeline.items() if k != "organisation"}
    return state


# ---------------------------------------------------------------- judging

def judge_pairs(conn: sqlite3.Connection, pairs: Sequence[Dict[str, Any]], judge=None,
                escalation_client=None) -> Dict[str, float]:
    """Final P(recorded victim is right) per pair: escalation in the band, else Jev. Raises on failure."""
    from cyber_data_collector.utils.escalation import escalate
    from cyber_data_collector.utils.jev import JevJudge

    owned = judge is None
    judge = judge or JevJudge(conn)
    q = {QUESTION_ID: question()}
    out: Dict[str, float] = {}
    try:
        for i, pair in enumerate(pairs, 1):
            state = build_state(conn, pair["deduplicated_event_id"], pair["entity_id"], pair["victim"])
            p = float(judge.ask("event_victim", pair["pair_id"], QUESTION_VERSION, state, q)[QUESTION_ID].value)
            if ESCALATE_BAND and ESCALATE_BAND[0] <= p < ESCALATE_BAND[1]:
                value = escalate(conn, "event_victim", pair["pair_id"], ESCALATION_QUESTION_ID,
                                 ESCALATION_VERSION, state, escalation_question(), client=escalation_client)
                if value is None:
                    raise RuntimeError(f"Escalation of victim judgement {pair['pair_id']} failed; see the log")
                p = float(value)
            out[pair["pair_id"]] = p
            if i % 100 == 0:
                logger.info("Judged %d/%d event-victim pairs", i, len(pairs))
    finally:
        if owned:
            judge.close()
    return out


def stored_final_probabilities(conn: sqlite3.Connection) -> Dict[str, float]:
    """The final score per pair from storage (latest answers): the escalation's in the band, else Jev's."""
    from cyber_data_collector.utils.llm_models import openai_model

    def latest(question_id: str, version: str, model: str) -> Dict[str, float]:
        found: Dict[str, float] = {}
        for sid, value in conn.execute(
                "SELECT subject_id, value FROM ModelJudgements WHERE question_id = ? AND question_version = ? "
                "AND model_requested = ? ORDER BY created_at, rowid", (question_id, version, model)):
            found[sid] = float(value)
        return found

    jev = latest(QUESTION_ID, QUESTION_VERSION, typesafe_model())
    escalated = latest(ESCALATION_QUESTION_ID, ESCALATION_VERSION, openai_model())
    out = {}
    for sid, p in jev.items():
        in_band = ESCALATE_BAND and ESCALATE_BAND[0] <= p < ESCALATE_BAND[1]
        if in_band and sid not in escalated:
            continue    # never escalated: no final score
        out[sid] = escalated[sid] if in_band else p
    return out


# ---------------------------------------------------------------- replacement

def _usable(name: str) -> bool:
    from cyber_data_collector.dedup.victim_agreement import _is_unusable_victim
    return bool(name and name.strip()) and not _is_unusable_victim(name)


def replacement_candidates(conn: sqlite3.Connection, dedup_id: str,
                           exclude: Sequence[str]) -> List[Dict[str, Any]]:
    """Organisations the event's own evidence names, other than ``exclude``; commonest first.

    Sources: each record's Perplexity victim, and the organisations linked to
    its records (not threat actors, products or people). Spellings of one
    name are counted together.
    """
    from cyber_data_collector.dedup.coherence import load_event_members
    from cyber_data_collector.dedup.org_timeline import is_same_name

    counts: Counter = Counter()
    for m in load_event_members(conn, dedup_id):
        if m["org"]:
            counts[m["org"].strip()] += 2   # the research's own answer to "who was breached"
    for (name,) in conn.execute(
            """SELECT v.entity_name FROM EventDeduplicationMap m
               JOIN EnrichedEventEntities ee ON ee.enriched_event_id = m.enriched_event_id
               JOIN EntitiesV2 v ON v.entity_id = ee.entity_id
               WHERE m.deduplicated_event_id = ?
                 AND COALESCE(v.entity_kind, 'organisation') IN ('organisation', 'government_body')
                 AND COALESCE(ee.relationship_type, '') NOT IN ('threat_actor', 'product')""", (dedup_id,)):
        counts[(name or "").strip()] += 1
    merged: List[Dict[str, Any]] = []
    for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if not _usable(name) or any(is_same_name(name, x) or name.lower() == (x or "").lower() for x in exclude):
            continue
        for c in merged:
            if is_same_name(name, c["name"]) or name.lower() == c["name"].lower():
                c["support"] += n
                break
        else:
            merged.append({"name": name, "support": n})
    merged.sort(key=lambda c: (-c["support"], c["name"]))
    return merged[:MAX_CANDIDATES]


def _replacement_question(candidates: Sequence[Dict[str, Any]]):
    from typesafe_sdk import Choice
    criteria = {f"org_{i}": c["name"] for i, c in enumerate(candidates, 1)}
    criteria["none"] = "None of these is clearly the organisation the incident happened to."
    return Choice(instructions=REPLACEMENT_INSTRUCTIONS, criteria=criteria)


def choose_replacement(conn: sqlite3.Connection, dedup_id: str, wrong: Sequence[str], judge) -> Dict[str, Any]:
    """Jev's choice of the real victim among the evidence's candidates. ``{"name", "probability", "candidates"}``."""
    from cyber_data_collector.dedup import event_reality

    candidates = replacement_candidates(conn, dedup_id, wrong)
    if not candidates:
        return {"name": None, "probability": None, "candidates": []}
    state = event_reality.build_state(conn, dedup_id, corroborated=True)
    state["recorded_victim"] = "; ".join(wrong)
    answer = judge.ask("event_victim_replacement", dedup_id, REPLACEMENT_VERSION, state,
                       {REPLACEMENT_QUESTION_ID: _replacement_question(candidates)})[REPLACEMENT_QUESTION_ID]
    if not answer.answer or answer.answer == "none":
        return {"name": None, "probability": answer.value, "candidates": candidates}
    index = int(answer.answer.split("_", 1)[1]) - 1
    return {"name": candidates[index]["name"], "probability": answer.value, "candidates": candidates}


# ---------------------------------------------------------------- labels and calibration

def load_labels(path: Path = LABELS_PATH, required: bool = False) -> Dict[str, Dict[str, str]]:
    """Labels by pair id. With ``required``, a missing, unreadable or empty file raises.

    Acting without them would drop the protection of labelled pairs, so the
    ``--apply`` path always requires them.
    """
    try:
        with Path(path).open(encoding="utf-8", newline="") as fh:
            labels = {r["pair_id"]: r for r in csv.DictReader(fh) if r.get("pair_id")}
    except (OSError, csv.Error, KeyError) as exc:
        if required:
            raise RuntimeError(f"Victim attribution labels unreadable ({path}): {exc}; refusing to act") from exc
        return {}
    if required and not labels:
        raise RuntimeError(f"Victim attribution labels file {path} is empty; refusing to act")
    return labels


def active_calibration(path: Path = CALIBRATION_PATH) -> Optional[Dict[str, Any]]:
    """The calibration, only if it was measured on the current models and questions (else logged, None)."""
    try:
        cal = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("No victim attribution calibration (%s): REPORT ONLY", exc)
        return None
    want = calibration_identity()
    if cal.get("question_version") != want["question_version"] or cal.get("model") != want["model"]:
        logger.warning("VICTIM ATTRIBUTION IS REPORT ONLY: calibration is for %s / %s, current is %s / %s. "
                       "Rerun scripts/calibrate_victim_attribution.py.", cal.get("model"),
                       cal.get("question_version"), want["model"], want["question_version"])
        return None
    if cal.get("cutoff") is None:
        logger.warning("VICTIM ATTRIBUTION IS REPORT ONLY: no cut-off reached the precision target")
        return None
    return cal


# ---------------------------------------------------------------- acting

def _demote(conn: sqlite3.Connection, dedup_id: str, entity_id: int) -> None:
    conn.execute("UPDATE DeduplicatedEventEntities SET relationship_type = 'bystander' "
                 "WHERE deduplicated_event_id = ? AND entity_id = ? AND relationship_type = 'victim'",
                 (dedup_id, entity_id))
    conn.execute("UPDATE EnrichedEventEntities SET relationship_type = 'bystander' WHERE entity_id = ? "
                 "AND relationship_type = 'victim' AND enriched_event_id IN "
                 "(SELECT enriched_event_id FROM EventDeduplicationMap WHERE deduplicated_event_id = ?)",
                 (entity_id, dedup_id))


def _rescalar(conn: sqlite3.Connection, dedup_id: str) -> None:
    """Point the scalar victim at a remaining victim role when it named a demoted one."""
    from cyber_data_collector.dedup.entity_classification import industry_for

    current = conn.execute("SELECT victim_organization_name FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                           (dedup_id,)).fetchone()[0]
    remaining = [name for _, name in _victims(conn, dedup_id)]
    if remaining and current not in remaining:
        conn.execute("UPDATE DeduplicatedEvents SET victim_organization_name = ?, victim_organization_industry = ?, "
                     "updated_at = CURRENT_TIMESTAMP WHERE deduplicated_event_id = ?",
                     (remaining[0], industry_for(conn, remaining[0]), dedup_id))


def replacement_entity(conn: sqlite3.Connection, dedup_id: str, name: str, old_entity_id: int) -> int:
    """The entity for a replacement victim: one linked to the event that is a spelling of ``name``,
    else one named exactly ``name``, else a new organisation. Never the old victim.

    Strict on purpose (:func:`org_timeline.is_same_name`): the looser test
    ``victim_agreement`` uses matched "UN Women Australia" to the University
    of Western Australia - the very misattribution being corrected.
    """
    from cyber_data_collector.dedup.org_timeline import is_same_name

    linked = conn.execute(
        """SELECT DISTINCT v.entity_id, v.entity_name FROM EventDeduplicationMap m
           JOIN EnrichedEventEntities ee ON ee.enriched_event_id = m.enriched_event_id
           JOIN EntitiesV2 v ON v.entity_id = ee.entity_id
           WHERE m.deduplicated_event_id = ? ORDER BY v.entity_id""", (dedup_id,)).fetchall()
    for entity_id, entity_name in linked:
        if entity_id != old_entity_id and (entity_name.lower() == name.lower() or is_same_name(entity_name, name)):
            return entity_id
    row = conn.execute("SELECT entity_id FROM EntitiesV2 WHERE entity_name = ? COLLATE NOCASE AND entity_id != ? "
                       "ORDER BY entity_id LIMIT 1", (name, old_entity_id)).fetchone()
    if row:
        return row[0]
    return conn.execute("INSERT INTO EntitiesV2 (entity_name, entity_kind) VALUES (?, 'organisation')",
                        (name,)).lastrowid


def _named_in_records(conn: sqlite3.Connection, dedup_id: str, name: str) -> bool:
    from cyber_data_collector.dedup.coherence import load_event_members
    from cyber_data_collector.dedup.victim_agreement import _mentions
    members = load_event_members(conn, dedup_id)
    return _mentions(" ".join(m["title"] + " " + m["body"] + " " + (m["org"] or "") for m in members), name)


def _label_allows(label: Optional[Dict[str, str]], name: Optional[str]) -> bool:
    """A pair labelled wrong may be re-attributed only to the victim its label names."""
    from cyber_data_collector.dedup.org_timeline import is_same_name
    if not label or label.get("label") != "wrong":
        return True
    wanted = (label.get("correct_victim") or "").strip()
    return bool(name and wanted and (name.lower() == wanted.lower() or is_same_name(name, wanted)))


def plan_actions(conn: sqlite3.Connection, scores: Dict[str, float], cal: Dict[str, Any],
                 labels: Dict[str, Dict[str, str]], judge) -> List[Dict[str, Any]]:
    """What --apply would do for every pair below the cut-off. Changes nothing."""
    cutoff = cal["cutoff"]
    threshold = cal.get("replacement_threshold")
    by_event: Dict[str, List[Dict[str, Any]]] = {}
    for pair in pairs_to_check(conn):
        p = scores.get(pair["pair_id"])
        if p is None or p >= cutoff:
            continue
        label = labels.get(pair["pair_id"])
        if label and label.get("label") == "correct":
            continue   # ground truth: never acted on the other way
        by_event.setdefault(pair["deduplicated_event_id"], []).append({**pair, "p_recorded_victim": p,
                                                                     "label": label})
    plans = []
    for did, wrong in sorted(by_event.items()):
        wrong_ids = {w["entity_id"] for w in wrong}
        keep = [(eid, name) for eid, name in _victims(conn, did) if eid not in wrong_ids]
        if keep:
            plans += [{**w, "action": "demoted", "new_victim": None, "replacement_probability": None,
                       "reason": f"other recorded victim(s) kept: {', '.join(n for _, n in keep)}"} for w in wrong]
            continue
        choice = choose_replacement(conn, did, [w["victim"] for w in wrong], judge)
        name, prob = choice["name"], choice["probability"]
        if threshold is None:
            reason = "no calibrated replacement threshold"
        elif not name:
            reason = "no candidate chosen" if choice["candidates"] else "the evidence names no other organisation"
        elif prob is None or prob < threshold:
            reason = f"replacement {name!r} below threshold ({prob:.2f} < {threshold:.2f})"
        elif not _named_in_records(conn, did, name):
            reason = f"replacement {name!r} is not named in the records' text"
        elif not all(_label_allows(w["label"], name) for w in wrong):
            reason = f"replacement {name!r} differs from the labelled correct victim"
        else:
            reason = None
        action = "flagged" if reason else "reattributed"
        plans += [{**w, "action": action, "new_victim": None if reason else name,
                   "candidate": name, "replacement_probability": prob,
                   "reason": reason or f"chosen among {len(choice['candidates'])} evidence candidate(s)"}
                  for w in wrong]
    return plans


def apply_actions(conn: sqlite3.Connection, plans: Sequence[Dict[str, Any]], cal: Dict[str, Any]) -> str:
    """Carry out planned demotions and re-attributions; snapshot first and audit every change."""
    from cyber_data_collector.dedup.ledger import DedupLedger
    from cyber_data_collector.dedup.victim_agreement import apply_victim

    conn.executescript(ACTIONS_SCHEMA)
    ledger = DedupLedger(conn)
    batch_id = f"victim-attribution-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    now = datetime.now().isoformat()
    cal_json = json.dumps({k: cal.get(k) for k in ("question_version", "model", "cutoff", "replacement_threshold",
                                                   "calibrated_on_database", "generated")})
    try:
        snapshotted = set()
        for plan in plans:
            did = plan["deduplicated_event_id"]
            if plan["action"] == "flagged" and conn.execute(
                    "SELECT 1 FROM VictimAttributionActions WHERE deduplicated_event_id = ? AND entity_id = ? "
                    "AND action = 'flagged' AND reason IS ? AND ABS(p_recorded_victim - ?) < 1e-9 "
                    "AND action_id = (SELECT MAX(action_id) FROM VictimAttributionActions "
                    "WHERE deduplicated_event_id = ? AND entity_id = ?)",
                    (did, plan["entity_id"], plan.get("reason"), plan["p_recorded_victim"], did,
                     plan["entity_id"])).fetchone():
                continue   # the same flag, already on record
            # The ledger snapshot holds the event row and members; the roles it
            # does not hold go in the audit row, so every change can be reversed.
            roles_before = json.dumps([[r[0], r[1]] for r in conn.execute(
                "SELECT entity_id, relationship_type FROM DeduplicatedEventEntities "
                "WHERE deduplicated_event_id = ? ORDER BY entity_id", (did,))])
            if plan["action"] in ("demoted", "reattributed") and did not in snapshotted:
                ledger.snapshot_event(batch_id, did)
                snapshotted.add(did)
            if plan["action"] == "demoted":
                _demote(conn, did, plan["entity_id"])
                _rescalar(conn, did)
            elif plan["action"] == "reattributed":
                _demote(conn, did, plan["entity_id"])
                new_entity = replacement_entity(conn, did, plan["new_victim"], plan["entity_id"])
                plan["new_victim"] = apply_victim(conn, did, plan["victim"], plan["new_victim"], entity_id=new_entity)
            conn.execute(
                "INSERT INTO VictimAttributionActions (batch_id, deduplicated_event_id, entity_id, old_victim, "
                "action, new_victim, p_recorded_victim, replacement_probability, cutoff, replacement_threshold, "
                "calibration_json, reason, roles_before_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (batch_id, did, plan["entity_id"], plan["victim"], plan["action"], plan.get("new_victim"),
                 plan["p_recorded_victim"], plan.get("replacement_probability"), cal["cutoff"],
                 cal.get("replacement_threshold"), cal_json, plan.get("reason"), roles_before, now))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return batch_id


def check_victim_attribution(conn: sqlite3.Connection, apply: bool = False, judge=None,
                             escalation_client=None, calibration_path: Path = CALIBRATION_PATH,
                             labels_path: Path = LABELS_PATH) -> Dict[str, Any]:
    """Judge every event-victim pair; with a calibration, plan (and with ``apply``, make) re-attributions.

    Raises when ``apply`` is asked for and the labels are missing, unreadable
    or empty. Without a matching calibration nothing is changed (logged as
    report-only) and pairs below :data:`REPORT_BELOW` are reported.
    """
    from cyber_data_collector.utils.jev import JevJudge

    labels = load_labels(labels_path, required=apply)
    owned = judge is None
    judge = judge or JevJudge(conn)
    try:
        pairs = pairs_to_check(conn)
        scores = judge_pairs(conn, pairs, judge=judge, escalation_client=escalation_client)
        cal = active_calibration(calibration_path)
        report = [{**p, "p_recorded_victim": scores[p["pair_id"]]} for p in pairs
                  if scores.get(p["pair_id"], 1.0) < (cal["cutoff"] if cal else REPORT_BELOW)]
        report.sort(key=lambda f: f["p_recorded_victim"])
        plans = plan_actions(conn, scores, cal, labels, judge) if cal else []
    finally:
        if owned:
            judge.close()
    batch = apply_actions(conn, plans, cal) if (apply and cal and plans) else None
    counts = Counter(p["action"] for p in plans)
    return {"judged": len(scores), "calibrated": cal is not None, "cutoff": cal["cutoff"] if cal else None,
            "replacement_threshold": cal.get("replacement_threshold") if cal else None,
            "applied": batch is not None, "batch_id": batch, "reported": report,
            "actions": [{k: v for k, v in p.items() if k != "label"} for p in plans],
            "counts": dict(counts)}
