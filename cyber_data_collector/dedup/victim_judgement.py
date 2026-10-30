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

**Flags are reassessed, never left to go stale.** Every pair is judged on
every run, and a stored answer is reused only while its input - the evidence,
the organisation's timeline and the event's membership and other victims - is
unchanged (``ModelJudgements`` keys on a hash of it). Each action row records
that input hash and the event's member signature. A pair whose latest action
is ``flagged`` therefore ends each run in one of three states: **confirmed**
(re-judged at or above the cut-off, or labelled correct; a ``confirmed`` row
clears the flag), **replaced** (demoted or re-attributed by the calibrated
rules above), or **still flagged** (a new row with the current reason when the
input or the reason changed; none when nothing did). A flagged pair is
*unresolved* (:func:`unresolved_attributions`): the incident stays, and victim
counting, the by-entity dashboard and the recurrence analysis mark it.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from cyber_data_collector.dedup import calibration_guard, final_decisions
from cyber_data_collector.utils.llm_models import openai_fast_model, typesafe_model
from cyber_data_collector.utils.timestamps import utc_now_iso

logger = logging.getLogger(__name__)

QUESTION_ID = "is_recorded_victim"
# v1 (report-only) judged only events whose victim had a timeline, and asked
# nothing about co-victims or collectives. v2 judges every event-victim pair.
QUESTION_VERSION = "victim-attribution-v2"
ESCALATION_QUESTION_ID = "is_recorded_victim_escalated"
ESCALATION_VERSION = "victim-attribution-escalation-v1"
ESCALATE_BAND: Optional[Tuple[float, float]] = (0.1, 0.7)
REPLACEMENT_QUESTION_ID = "replacement_victim"
# v2 (2026-10-29): the options include the organisations the event's text names, folded through the
# alias tables, without descriptive phrases or the attacking party; the choice sees what the text says of
# each (`candidate_context`) and who attacked (`attacking_parties`).
REPLACEMENT_VERSION = "victim-replacement-v2"
REPORT_BELOW = 0.5
MAX_CANDIDATES = 8
#: Bump when what the judgement shows beyond event reality's evidence changes (the victim, the other
#: victims, the victim's timeline, the replacement options). v2 (2026-10-29): text-named, alias-folded
#: candidates without descriptive phrases or the attacker; a recorded victim an attacker field names
#: carries a note (`note_on_recorded_victim`).
EVIDENCE_VERSION = "victim-evidence-v2"

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
    action                 TEXT NOT NULL,     -- demoted / reattributed / flagged / confirmed
    new_victim             TEXT,
    p_recorded_victim      REAL NOT NULL,
    replacement_probability REAL,
    cutoff                 REAL,
    replacement_threshold  REAL,
    calibration_json       TEXT,
    reason                 TEXT,
    roles_before_json      TEXT,              -- the event's entity roles before the change (for reversal)
    created_at             TEXT NOT NULL,
    input_hash             TEXT,              -- hash of the judged input (evidence, timeline, membership)
    member_signature       TEXT               -- the event's members when judged (role_maintenance)
);
"""
# Columns added after the table was first created: (name, declaration).
_ADDED_COLUMNS = (("input_hash", "TEXT"), ("member_signature", "TEXT"))

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


def evidence_fingerprint() -> str:
    """A hash of the evidence a victim judgement sees: the event's reality evidence plus its own additions."""
    from cyber_data_collector.dedup import entity_profiles, event_organisations, event_reality

    return calibration_guard.fingerprint(
        "victim-attribution", version=EVIDENCE_VERSION, max_candidates=MAX_CANDIDATES,
        extraction=event_organisations.EXTRACTION_VERSION, profiles=entity_profiles.PROMPT_VERSION,
        escalate_band=list(ESCALATE_BAND) if ESCALATE_BAND else None, events=event_reality.evidence_fingerprint())


def calibration_identity() -> Dict[str, str]:
    """What a calibration must have been measured on to apply: all three questions, the models (the fast
    one extracts the text-named candidates), evidence."""
    from cyber_data_collector.utils.llm_models import openai_model
    return {"question_version": f"{QUESTION_VERSION}+{ESCALATION_VERSION}+{REPLACEMENT_VERSION}",
            "model": f"{typesafe_model()}+{openai_model()}+{openai_fast_model()}", "evidence": evidence_fingerprint()}


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
    # Only when the stored evidence says so, so every other pair's input (and its stored answer) is unchanged.
    parties = acting_parties(conn, dedup_id)
    attacker = is_acting_party(victim, parties) or attacker_field_note(victim, parties)
    if attacker:
        state["note_on_recorded_victim"] = (
            f"The event's own records name the recorded victim in connection with the attacker: {attacker}. "
            "An attacker, or whoever ran the tool or agent that carried out the access, is not the victim of "
            "that access unless the evidence shows its own systems or data were affected.")
    timeline = for_judgement(conn, entity_id)
    if timeline:
        state["organisation_timeline"] = {k: v for k, v in timeline.items() if k != "organisation"}
    return state


# ---------------------------------------------------------------- judging

def judge_pairs(conn: sqlite3.Connection, pairs: Sequence[Dict[str, Any]], judge=None,
                escalation_client=None, inputs: Optional[Dict[str, Dict[str, str]]] = None) -> Dict[str, float]:
    """Final P(recorded victim is right) per pair: escalation in the band, else Jev. Raises on failure.

    With ``inputs``, also fills it with each pair's ``input_hash`` (the hash
    Jev's stored answer is keyed on) and the event's ``member_signature``.
    """
    from cyber_data_collector.dedup.role_maintenance import member_signature
    from cyber_data_collector.utils.escalation import escalate
    from cyber_data_collector.utils.jev import JevJudge, input_hash

    owned = judge is None
    judge = judge or JevJudge(conn)
    q = {QUESTION_ID: question()}
    out: Dict[str, float] = {}
    try:
        for i, pair in enumerate(pairs, 1):
            state = build_state(conn, pair["deduplicated_event_id"], pair["entity_id"], pair["victim"])
            if inputs is not None:
                inputs[pair["pair_id"]] = {
                    "input_hash": input_hash(state, q[QUESTION_ID], typesafe_model()),
                    "member_signature": member_signature(conn, pair["deduplicated_event_id"])}
            p = float(judge.ask("event_victim", pair["pair_id"], QUESTION_VERSION, state, q)[QUESTION_ID].value)
            escalated = bool(ESCALATE_BAND and ESCALATE_BAND[0] <= p < ESCALATE_BAND[1])
            if escalated:
                value = escalate(conn, "event_victim", pair["pair_id"], ESCALATION_QUESTION_ID,
                                 ESCALATION_VERSION, state, escalation_question(), client=escalation_client)
                if value is None:
                    raise RuntimeError(f"Escalation of victim judgement {pair['pair_id']} failed; see the log")
                p = float(value)
            out[pair["pair_id"]] = p
            # The final score, tied to the evidence both answers were given (see final_decisions).
            final_decisions.record(conn, STEP, pair["pair_id"], state, p, escalated)
            if i % 100 == 0:
                logger.info("Judged %d/%d event-victim pairs", i, len(pairs))
    finally:
        conn.commit()
        if owned:
            judge.close()
    return out


STEP = final_decisions.Step(
    name="victim-attribution", question_id=QUESTION_ID, version=QUESTION_VERSION, make_question=question,
    escalation_id=ESCALATION_QUESTION_ID, escalation_version=ESCALATION_VERSION,
    escalation_text=escalation_question, band=ESCALATE_BAND or None)


def stored_final_probabilities(conn: sqlite3.Connection, pairs: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    """Final score per event-victim pair from its decision on the evidence it has **now** (:mod:`final_decisions`).

    It used to be the newest Jev answer and the newest escalation, each chosen alone. A pair with no decision on
    its current evidence (its records, timeline or co-victims changed since it was judged) has no score here;
    ``check-victim-attribution`` judges it again. ``pairs`` carry ``pair_id``, ``deduplicated_event_id``,
    ``entity_id`` and ``victim`` (see :func:`pairs_to_check`)."""
    return final_decisions.read_current(
        conn, STEP, [(p["pair_id"], lambda p=p: build_state(conn, p["deduplicated_event_id"], p["entity_id"], p["victim"]))
                     for p in pairs])


# ---------------------------------------------------------------- replacement

def _usable(name: str) -> bool:
    from cyber_data_collector.dedup.victim_agreement import _is_unusable_victim
    return bool(name and name.strip()) and not _is_unusable_victim(name)


def canonical_candidate_name(conn: sqlite3.Connection, name: str, context: Optional[str] = None) -> str:
    """The organisation's canonical name: stored aliases, then the reviewed ``EXPLICIT_ALIASES``.

    "Australian Medicare" and "Medicare" are Services Australia. Reading the reviewed table directly means
    a candidate is folded before ``canonicalise-entities`` has written the alias to the database.
    """
    from cyber_data_collector.dedup.entity_merge import EXPLICIT_ALIASES
    from cyber_data_collector.dedup.entity_resolution import canonical_entity_name

    name = (name or "").strip()
    hit = canonical_entity_name(conn, name, context=context)
    if hit and hit.strip().lower() != name.lower():
        return hit.strip()
    wanted = name.lower()
    for raw, canonical in EXPLICIT_ALIASES.items():
        if raw.lower() == wanted:
            return canonical
    return name


def acting_parties(conn: sqlite3.Connection, dedup_id: str, extracted: Any = None) -> List[Dict[str, str]]:
    """What the event's evidence says did the attacking: ``[{"party", "why"}]``.

    * entities linked to the event's records with the ``threat_actor`` role;
    * the records' attacker fields (``attacking_entity_name``, and the Perplexity ``threat_actor``);
    * with ``extracted`` (an :class:`event_organisations.EventOrganisations`), the actors the text names.

    A party can be a description ("autonomous OpenAI AI agent"), so matching a candidate against it is
    :func:`is_acting_party`'s job. Deterministic apart from ``extracted``.
    """
    from cyber_data_collector.processing.active_evidence import member_condition

    found: List[Dict[str, str]] = []
    try:
        for (name,) in conn.execute(
                """SELECT DISTINCT v.entity_name FROM EventDeduplicationMap m
                   JOIN EnrichedEventEntities ee ON ee.enriched_event_id = m.enriched_event_id
                   JOIN EntitiesV2 v ON v.entity_id = ee.entity_id
                   WHERE m.deduplicated_event_id = ? AND ee.relationship_type = 'threat_actor'
                   ORDER BY v.entity_name""", (dedup_id,)):
            found.append({"party": name, "why": "linked to the event's records as the threat actor"})
    except sqlite3.OperationalError:
        pass
    has_attacker = any(r[1] == "attacking_entity_name" for r in conn.execute("PRAGMA table_info(EnrichedEvents)"))
    for attacker, research in conn.execute(
            f"""SELECT {"e.attacking_entity_name" if has_attacker else "NULL"}, e.perplexity_enrichment_data
                FROM EventDeduplicationMap m
                JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
                WHERE m.deduplicated_event_id = ? {member_condition(conn)} ORDER BY e.enriched_event_id""",
            (dedup_id,)):
        try:
            data = json.loads(research or "{}")
        except (TypeError, ValueError):
            data = {}
        for value in (attacker, data.get("threat_actor") if isinstance(data, dict) else None):
            if isinstance(value, str) and value.strip() and value.strip().lower() not in ("unknown", "n/a", "none"):
                found.append({"party": value.strip(), "why": "a record's attacker field names it"})
    for name in getattr(extracted, "actors", None) or ():
        found.append({"party": name, "why": "the event's text names it as having carried out the attack"})
    unique: List[Dict[str, str]] = []
    for item in found:
        if not any(item["party"].lower() == u["party"].lower() for u in unique):
            unique.append(item)
    return unique


def is_acting_party(name: Optional[str], parties: Sequence[Dict[str, str]]) -> Optional[str]:
    """Why ``name`` is one of the attacking parties (a sentence), else None.

    Only the same organisation counts (an equal name or a spelling of it): the entity role
    ``threat_actor``, an attacker field naming it, or an actor the event's text names. A description
    that merely *contains* the name ("autonomous OpenAI AI agent", "insider at Medibank") is not decided
    here - nothing but a judgement can tell an agent the organisation ran from a person who attacked it -
    so it is shown to the attribution judgement as a note instead (:func:`attacker_field_note`).
    """
    from cyber_data_collector.dedup.org_timeline import _name_tokens

    if not name or not name.strip():
        return None
    for party in parties:
        text = party["party"]
        # The same words (legal forms and "Australia" aside): not one name merely inside a longer description.
        if text.lower() == name.lower() or (_name_tokens(name) and _name_tokens(name) == _name_tokens(text)):
            return f"{text!r}: {party['why']}"
    return None


def attacker_field_note(name: Optional[str], parties: Sequence[Dict[str, str]]) -> Optional[str]:
    """The attacker description that contains ``name`` as whole words, if any (evidence, not a decision)."""
    from cyber_data_collector.dedup.event_organisations import _normal, _occurs

    wanted = _normal(name or "")
    if not wanted:
        return None
    for party in parties:
        if _occurs(_normal(party["party"]), wanted):
            return f"{party['party']!r} ({party['why']})"
    return None


def _known_non_victim(conn: sqlite3.Connection, name: str) -> bool:
    """The name is stored as an entity of a kind that cannot be a victim (person, product, threat actor)."""
    from cyber_data_collector.dedup.entity_classification import NON_VICTIM_KINDS, rule_kind

    row = conn.execute("SELECT entity_kind FROM EntitiesV2 WHERE entity_name = ? COLLATE NOCASE "
                       "AND entity_kind IS NOT NULL LIMIT 1", (name,)).fetchone()
    if row and row[0] in NON_VICTIM_KINDS:
        return True
    kind = rule_kind(name)
    return kind is not None and kind.value in NON_VICTIM_KINDS


def replacement_candidates(conn: sqlite3.Connection, dedup_id: str, exclude: Sequence[str],
                           organisations: Any = None, include_scalar: bool = False) -> List[Dict[str, Any]]:
    """Organisations the event's own evidence names, other than ``exclude``; best supported first.

    Sources (``sources`` on each candidate):

    * ``research`` - each record's Perplexity victim (weight 2);
    * ``link`` - the organisations linked to its records, whatever role they were given - a bystander
      link is still a name the evidence carries (not threat actors, products or people);
    * ``scalar`` (with ``include_scalar``) - the event's own victim name when it is usable, weight 2;
    * ``text`` - with ``organisations`` (:class:`event_organisations.EventOrganisations`), the names the
      event's text mentions as possible victims, weight 1.

    Names are folded through the alias tables (:func:`canonical_candidate_name`: Medicare is Services
    Australia), spellings of one name are counted together, descriptive phrases and the parties that did
    the attacking (:func:`acting_parties`) are left out, and ``candidate_evidence`` of a text-named
    candidate is its excerpt.
    """
    from cyber_data_collector.dedup.contextual_aliases import event_text
    from cyber_data_collector.dedup.coherence import load_event_members
    from cyber_data_collector.dedup.org_timeline import is_same_name

    context = event_text(conn, dedup_id)[1]
    parties = acting_parties(conn, dedup_id, organisations)
    weighted: List[Tuple[str, int, str, str, str]] = []     # raw name, weight, source, excerpt, name the text carries
    for m in load_event_members(conn, dedup_id):
        if m["org"]:
            weighted.append((m["org"].strip(), 2, "research", "", ""))   # the research's own answer to "who was breached"
    for (name,) in conn.execute(
            """SELECT v.entity_name FROM EventDeduplicationMap m
               JOIN EnrichedEventEntities ee ON ee.enriched_event_id = m.enriched_event_id
               JOIN EntitiesV2 v ON v.entity_id = ee.entity_id
               WHERE m.deduplicated_event_id = ?
                 AND COALESCE(v.entity_kind, 'organisation') IN ('organisation', 'government_body')
                 AND COALESCE(ee.relationship_type, '') NOT IN ('threat_actor', 'product')""", (dedup_id,)):
        weighted.append(((name or "").strip(), 1, "link", "", ""))
    if include_scalar:
        scalar = conn.execute("SELECT victim_organization_name FROM DeduplicatedEvents "
                              "WHERE deduplicated_event_id = ?", (dedup_id,)).fetchone()
        if scalar and scalar[0] and scalar[0].strip():
            weighted.append((scalar[0].strip(), 2, "scalar", "", ""))
    for item in getattr(organisations, "possible_victims", None) or ():
        if not _known_non_victim(conn, item["name"]):
            weighted.append((item["name"], 1, "text", item.get("excerpt", ""), ""))
    from cyber_data_collector.dedup.alias_learning import product_hosts

    for product, organisation in product_hosts(conn, dedup_id):
        weighted.append((organisation, 1, "product", f"the records link the product {product!r}, which carries "
                                                      "this organisation's name", product))

    excluded = [x for x in exclude if x] + [canonical_candidate_name(conn, x, context) for x in exclude if x]
    merged: List[Dict[str, Any]] = []
    for raw, weight, source, excerpt, carried_by in weighted:
        if not _usable(raw):
            continue
        name = canonical_candidate_name(conn, raw, context)
        if not _usable(name) or any(is_same_name(name, x) or name.lower() == x.lower() for x in excluded):
            continue
        if is_acting_party(name, parties) or is_acting_party(raw, parties):
            continue
        for c in merged:
            if is_same_name(name, c["name"]) or name.lower() == c["name"].lower():
                c["support"] += weight
                for spelling in (raw, carried_by):
                    if spelling and spelling not in c["spellings"]:
                        c["spellings"].append(spelling)
                if source not in c["sources"]:
                    c["sources"].append(source)
                if excerpt and not c.get("excerpt"):
                    c["excerpt"] = excerpt
                break
        else:
            merged.append({"name": name, "support": weight, "spellings": [x for x in (raw, carried_by) if x],
                           "sources": [source],
                           **({"excerpt": excerpt} if excerpt else {})})
    merged.sort(key=lambda c: (-c["support"], c["name"]))
    return merged[:MAX_CANDIDATES]


def _replacement_question(candidates: Sequence[Dict[str, Any]], instructions: str = REPLACEMENT_INSTRUCTIONS):
    from typesafe_sdk import Choice
    criteria = {f"org_{i}": c["name"] for i, c in enumerate(candidates, 1)}
    criteria["none"] = "None of these is clearly the organisation the incident happened to."
    return Choice(instructions=instructions, criteria=criteria)


def candidate_state(conn: sqlite3.Connection, dedup_id: str, candidates: Sequence[Dict[str, Any]],
                    parties: Sequence[Dict[str, str]] = ()) -> Dict[str, Any]:
    """The evidence a choice among ``candidates`` is shown: the event's, plus what the text says of each name.

    ``candidate_context`` holds, for a candidate the text names, the words around its first mention (a
    candidate the records' research or links named is judged from the records themselves). The attacking
    parties are listed so the choice does not mistake the attacker for the target.
    """
    from cyber_data_collector.dedup import event_reality

    state = event_reality.build_state(conn, dedup_id, corroborated=True)
    context = {c["name"]: c["excerpt"] for c in candidates if c.get("excerpt")}
    if context:
        state["candidate_context"] = context
    from cyber_data_collector.dedup.entity_profiles import candidate_profiles

    # What each candidate is (jurisdiction, sector, what it runs, the data it holds): evidence to weigh against
    # the incident's context, never a matcher. Only when profiles exist, so other inputs are unchanged.
    profiles = candidate_profiles(conn, [c["name"] for c in candidates])
    if profiles:
        state["candidate_profiles"] = profiles
    if parties:
        state["attacking_parties"] = [p["party"] for p in parties]
    return state


def choose_replacement(conn: sqlite3.Connection, dedup_id: str, wrong: Sequence[str], judge,
                       extractor=None, use_text: bool = True) -> Dict[str, Any]:
    """Jev's choice of the real victim among the evidence's candidates.

    ``{"name", "probability", "candidates"}``. The candidates include the organisations the event's text
    names (:mod:`event_organisations`, one stored extraction per event) unless ``use_text`` is False;
    a failed extraction raises :class:`event_organisations.ExtractionFailed` rather than offering fewer.
    """
    from cyber_data_collector.dedup import event_organisations

    organisations = (event_organisations.named_organisations(conn, dedup_id, extractor=extractor)
                     if use_text else None)
    candidates = replacement_candidates(conn, dedup_id, wrong, organisations=organisations)
    if not candidates:
        return {"name": None, "probability": None, "candidates": []}
    state = candidate_state(conn, dedup_id, candidates, acting_parties(conn, dedup_id, organisations))
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
    reason = calibration_guard.mismatch(cal, calibration_identity())
    if reason:
        logger.warning("VICTIM ATTRIBUTION IS REPORT ONLY. %s",
                       calibration_guard.report_only_message("victim attribution", cal, reason))
        return None
    if cal.get("cutoff") is None:
        logger.warning("VICTIM ATTRIBUTION IS REPORT ONLY: no cut-off reached the precision target")
        return None
    from cyber_data_collector.dedup import deployment

    # A replacement threshold the rule holds back is set to None: those pairs are flagged, not replaced.
    return deployment.gate("victim-attribution", cal, Path(path))


# ---------------------------------------------------------------- flags

def ensure_actions_schema(conn: sqlite3.Connection) -> None:
    """Create ``VictimAttributionActions`` or add the columns later versions record. Idempotent."""
    conn.executescript(ACTIONS_SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(VictimAttributionActions)")}
    for name, declaration in _ADDED_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE VictimAttributionActions ADD COLUMN {name} {declaration}")


def latest_actions(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """The latest stored action for each pair id (``event|entity``); empty before the first action."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(VictimAttributionActions)")}
    if not have:
        return {}
    extra = ", ".join(f"a.{c}" if c in have else f"NULL AS {c}" for c, _ in _ADDED_COLUMNS)
    rows = conn.execute(
        f"""SELECT a.deduplicated_event_id, a.entity_id, a.action, a.reason, a.p_recorded_victim,
                   a.created_at, {extra}
            FROM VictimAttributionActions a
            JOIN (SELECT deduplicated_event_id, entity_id, MAX(action_id) AS latest
                  FROM VictimAttributionActions GROUP BY deduplicated_event_id, entity_id) l
              ON l.latest = a.action_id""").fetchall()
    return {f"{r[0]}|{r[1]}": {"deduplicated_event_id": r[0], "entity_id": r[1], "action": r[2], "reason": r[3],
                               "p_recorded_victim": r[4], "created_at": r[5], "input_hash": r[6],
                               "member_signature": r[7]} for r in rows}


def unresolved_attributions(conn: sqlite3.Connection) -> Dict[Tuple[str, int], Dict[str, Any]]:
    """Active event-victim pairs whose latest attribution action is ``flagged``, keyed ``(event, entity)``.

    The judgement scored the recorded victim below the calibrated cut-off, and
    no co-victim or calibrated replacement could take its place. The incident
    is kept; only who it happened to is unresolved. Each value holds the
    flag's ``reason``, score and date.
    """
    flagged = {(a["deduplicated_event_id"], int(a["entity_id"])): a
               for a in latest_actions(conn).values() if a["action"] == "flagged"}
    if not flagged:
        return {}
    victims = {(r[0], int(r[1])) for r in conn.execute(
        """SELECT dee.deduplicated_event_id, dee.entity_id FROM DeduplicatedEventEntities dee
           JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id
           WHERE dee.relationship_type = 'victim' AND d.status = 'Active'""")}
    return {key: flag for key, flag in flagged.items() if key in victims}


def _change_since(flag: Dict[str, Any], current: Optional[Dict[str, str]]) -> str:
    """What changed in a pair's judged input since it was flagged, in words."""
    if not current:
        return "input not recorded"
    if not flag.get("input_hash"):
        return "flag predates input tracking"
    if flag.get("member_signature") and flag["member_signature"] != current.get("member_signature"):
        return "event membership changed"
    if flag["input_hash"] != current.get("input_hash"):
        return "evidence, timeline or co-victims changed"
    return "input unchanged"


def plan_confirmations(pairs: Sequence[Dict[str, Any]], flags: Dict[str, Dict[str, Any]],
                       scores: Dict[str, float], cal: Dict[str, Any], labels: Dict[str, Dict[str, str]],
                       inputs: Dict[str, Dict[str, str]]) -> List[Dict[str, Any]]:
    """Flagged pairs that are now right: labelled correct, or re-judged at or above the cut-off.

    A pair labelled wrong is never confirmed. Flagged pairs still below the
    cut-off are left to :func:`plan_actions` (demote, re-attribute or flag).
    """
    plans = []
    for pair in pairs:
        flag = flags.get(pair["pair_id"])
        p = scores.get(pair["pair_id"])
        if flag is None or p is None:
            continue
        label = labels.get(pair["pair_id"])
        change = _change_since(flag, inputs.get(pair["pair_id"]))
        if label and label.get("label") == "correct":
            reason = f"labelled correct ({change})"
        elif label and label.get("label") == "wrong":
            continue
        elif p >= cal["cutoff"]:
            reason = f"re-judged {p:.2f} >= cut-off {cal['cutoff']:.2f} ({change})"
        else:
            continue
        plans.append({**pair, "p_recorded_victim": p, "label": label, "action": "confirmed", "new_victim": None,
                      "replacement_probability": None, "reason": reason})
    return plans


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
    from cyber_data_collector.dedup.entity_resolution import canonical_entity_name

    from cyber_data_collector.dedup.contextual_aliases import event_text

    # An acronym alias applies only where the event's text names the organisation.
    name = canonical_entity_name(conn, name, context=event_text(conn, dedup_id)[1])
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
                 labels: Dict[str, Dict[str, str]], judge, extractor=None,
                 errors: Optional[List[Dict[str, str]]] = None) -> List[Dict[str, Any]]:
    """What --apply would do for every pair below the cut-off. Changes nothing.

    An event whose text-named candidates could not be extracted gets no plan (it is retried next run) and is
    appended to ``errors``; with no ``errors`` list the failure raises.
    """
    from cyber_data_collector.dedup.event_organisations import ExtractionFailed

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
        try:
            choice = choose_replacement(conn, did, [w["victim"] for w in wrong], judge, extractor=extractor)
        except ExtractionFailed as exc:
            if errors is None:
                raise
            logger.error("Victim replacement for %s not planned: %s", did, exc)
            errors.append({"deduplicated_event_id": did, "error": str(exc)})
            continue
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
    """Carry out planned demotions and re-attributions; snapshot first and audit every change.

    ``confirmed`` and ``flagged`` plans change no roles; they are audit rows. A
    flag identical to the latest one on record (reason, score and input) is
    not written again.
    """
    from cyber_data_collector.dedup.ledger import DedupLedger
    from cyber_data_collector.dedup.victim_agreement import apply_victim

    ensure_actions_schema(conn)
    ledger = DedupLedger(conn)
    batch_id = f"victim-attribution-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    now = utc_now_iso()
    cal_json = json.dumps({k: cal.get(k) for k in ("question_version", "model", "cutoff", "replacement_threshold",
                                                   "calibrated_on_database", "generated")})
    try:
        snapshotted = set()
        for plan in plans:
            did = plan["deduplicated_event_id"]
            if plan["action"] == "flagged" and conn.execute(
                    "SELECT 1 FROM VictimAttributionActions WHERE deduplicated_event_id = ? AND entity_id = ? "
                    "AND action = 'flagged' AND reason IS ? AND ABS(p_recorded_victim - ?) < 1e-9 "
                    "AND input_hash IS ? "
                    "AND action_id = (SELECT MAX(action_id) FROM VictimAttributionActions "
                    "WHERE deduplicated_event_id = ? AND entity_id = ?)",
                    (did, plan["entity_id"], plan.get("reason"), plan["p_recorded_victim"],
                     plan.get("input_hash"), did, plan["entity_id"])).fetchone():
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
                "calibration_json, reason, roles_before_json, created_at, input_hash, member_signature) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (batch_id, did, plan["entity_id"], plan["victim"], plan["action"], plan.get("new_victim"),
                 plan["p_recorded_victim"], plan.get("replacement_probability"), cal["cutoff"],
                 cal.get("replacement_threshold"), cal_json, plan.get("reason"), roles_before, now,
                 plan.get("input_hash"), plan.get("member_signature")))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return batch_id


def check_victim_attribution(conn: sqlite3.Connection, apply: bool = False, judge=None,
                             escalation_client=None, calibration_path: Path = CALIBRATION_PATH,
                             labels_path: Path = LABELS_PATH, extractor=None) -> Dict[str, Any]:
    """Judge every event-victim pair; with a calibration, plan (and with ``apply``, make) re-attributions.

    Pairs flagged earlier are settled too (:func:`plan_confirmations`): each
    ends confirmed, demoted / re-attributed, or still flagged; ``flag_review``
    counts them.

    Raises when ``apply`` is asked for and the labels are missing, unreadable
    or empty. Without a matching calibration nothing is changed (logged as
    report-only) and pairs below :data:`REPORT_BELOW` are reported.
    """
    from cyber_data_collector.utils.jev import JevJudge

    labels = load_labels(labels_path, required=apply)
    owned = judge is None
    judge = judge or JevJudge(conn)
    inputs: Dict[str, Dict[str, str]] = {}
    errors: List[Dict[str, str]] = []
    try:
        pairs = pairs_to_check(conn)
        scores = judge_pairs(conn, pairs, judge=judge, escalation_client=escalation_client, inputs=inputs)
        cal = active_calibration(calibration_path)
        report = [{**p, "p_recorded_victim": scores[p["pair_id"]]} for p in pairs
                  if scores.get(p["pair_id"], 1.0) < (cal["cutoff"] if cal else REPORT_BELOW)]
        report.sort(key=lambda f: f["p_recorded_victim"])
        flags = {pid: a for pid, a in latest_actions(conn).items() if a["action"] == "flagged" and pid in scores}
        plans = (plan_actions(conn, scores, cal, labels, judge, extractor=extractor, errors=errors)
                 + plan_confirmations(pairs, flags, scores, cal, labels, inputs)) if cal else []
        for plan in plans:
            plan.update(inputs.get(plan["pair_id"], {}))
    finally:
        if owned:
            judge.close()
    batch = apply_actions(conn, plans, cal) if (apply and cal and plans) else None
    counts = Counter(p["action"] for p in plans)
    outcome = {p["pair_id"]: p["action"] for p in plans}
    review = Counter(outcome.get(pid, "flagged") for pid in flags)
    flag_review = {"flagged_before": len(flags), "confirmed": review["confirmed"], "demoted": review["demoted"],
                   "reattributed": review["reattributed"], "still_flagged": review["flagged"],
                   "inputs_changed": sum(_change_since(f, inputs.get(pid)) != "input unchanged"
                                         for pid, f in flags.items()),
                   "unresolved_now": len(unresolved_attributions(conn))}
    return {"judged": len(scores), "calibrated": cal is not None, "cutoff": cal["cutoff"] if cal else None,
            "replacement_threshold": cal.get("replacement_threshold") if cal else None,
            "applied": batch is not None, "batch_id": batch, "reported": report,
            "actions": [{k: v for k, v in p.items() if k != "label"} for p in plans],
            "counts": dict(counts), "flag_review": flag_review, "errors": errors}
