"""AI involvement in every active event: AI as the attacker, the victim's AI attacked, AI misused, or none.

Each active event is asked one Jev Choice over four options, from the same
evidence the breach-source question reads (the event's title and, for up to
five member records, each record's title, snippet, stored page excerpt and
Perplexity research - :func:`breach_source.event_state`):

* ``AI-attacker``: an AI system carried out or drove the attack (the OpenAI
  agent that accessed Services Australia's Medicare statistics portal in 2026),
  or the reporting states the attacker's instrument was AI (deepfakes,
  AI-generated phishing or malware);
* ``AI-vulnerability``: the victim's own AI system was attacked, misused or
  leaked data (prompt injection, a jailbroken chatbot, exposed model or
  training data);
* ``AI-misuse``: no one attacked anything - the organisation's own use of AI
  was the breach (Kmart's in-store facial recognition, Clearview AI's
  face-scraping tool, a contractor uploading applicants' data to ChatGPT);
* ``No-AI``: no AI involvement is reported.

The chosen label and all four probabilities are stored on
``DeduplicatedEvents`` (``ai_involvement``, ``ai_attacker_probability``,
``ai_vulnerability_probability``, ``ai_misuse_probability``,
``no_ai_probability``); the full answer, the
question as sent and the input hash are in ``ModelJudgements`` and are reused
while the evidence, the question and the model are unchanged, so a rerun asks
only about new or changed events and a rebuilt ``DeduplicatedEvents`` is
refilled from the stored answers.

Which label an event gets is decided by a **calibrated cut-off** when one
matches the current model and question (``evaluation/ai_involvement_calibration.json``,
made by ``scripts/calibrate_ai_involvement.py`` from blind labels): the event
is AI-involved when P(AI) = 1 - P(No-AI) reaches the cut-off, and its category
is the most probable of attacker, vulnerability and misuse. Labelled events take their
label (ground truth). With no matching calibration the label is the most
probable option and ``ai_involvement_basis`` says so, as do the dashboard and
the report. Evidence is whole pages within a budget (:func:`breach_source.event_state`).

Events with AI involvement also get a 2-4 sentence description, written by the
fast OpenAI model from the same evidence for the AI incident report and stored
in ``EventDescriptions`` (reused while the evidence, prompt and model are
unchanged).

Freshness: each label stores ``ai_involvement_input_hash`` - a hash of the
evidence, question, models, cut-off and any label it was decided on - and
``ai_involvement_error`` when its last re-judgement failed (the old label is
kept). :func:`stale_judgements` re-derives the hash from the current evidence,
so a label whose inputs changed since it was judged is reported stale until the
step runs again; a description is used only for the evidence it was written
from, and an older one is shown marked as such. Jev is required: if it is unavailable nothing runs, and if any
event cannot be judged or described the step raises after storing the rest.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from cyber_data_collector.processing.breach_source import event_state

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
LABELS_PATH = ROOT / "evaluation" / "ai_involvement_labels.csv"
CALIBRATION_PATH = ROOT / "evaluation" / "ai_involvement_calibration.json"
#: ``ai_involvement_basis`` values (a calibrated one names the cut-off).
BASIS_LABEL = "labelled"

QUESTION_VERSION = "ai-involvement-v4"
QUESTION_ID = "ai_involvement"
ESCALATION_QUESTION_ID = "ai_involvement_escalated"
ESCALATION_VERSION = "ai-involvement-escalation-v2"
#: Bump when what the question is shown changes (``breach_source.event_state``);
#: its parameters are in :func:`evidence_fingerprint` and need no bump.
EVIDENCE_VERSION = "ai-involvement-evidence-v1"
#: Jev's answer is settled when its top option reaches DOMINANT or leads the
#: next by MARGIN; otherwise the reasoning model is asked (Vercel, 2026-10-28:
#: attacker 0.48 / vulnerability 0.42 had been published as "AI-attacker").
DOMINANT = 0.8
MARGIN = 0.3
#: Without a calibration, AI is involved at P(AI) >= INVOLVED and not at
#: <= NOT_INVOLVED; in between, and whenever no category holds CATEGORY_SHARE
#: of P(AI), the event is AI-uncertain - never a definite category.
INVOLVED = 0.65
NOT_INVOLVED = 0.35
CATEGORY_SHARE = 0.75
DESCRIPTION_PURPOSE = "ai_incident_report"
DESCRIPTION_PROMPT_VERSION = "ai-description-v1"

AI_ATTACKER = "AI-attacker"
AI_VULNERABILITY = "AI-vulnerability"
AI_MISUSE = "AI-misuse"
AI_UNCERTAIN = "AI-uncertain"
NO_AI = "No-AI"
#: The definite AI categories.
AI_LABELS = (AI_ATTACKER, AI_VULNERABILITY, AI_MISUSE)
#: Everything charted and listed as (possible) AI involvement.
AI_INVOLVED_LABELS = (AI_ATTACKER, AI_VULNERABILITY, AI_MISUSE, AI_UNCERTAIN)

#: Jev option -> stored label, and -> probability column.
LABELS = {"ai_attacker": AI_ATTACKER, "ai_vulnerability": AI_VULNERABILITY, "ai_misuse": AI_MISUSE,
          "no_ai": NO_AI}
#: The options that mean AI was involved (all but no_ai), and their names in a basis.
AI_OPTIONS = ("ai_attacker", "ai_vulnerability", "ai_misuse")
CATEGORY_NAMES = {"ai_attacker": "attacker", "ai_vulnerability": "vulnerability", "ai_misuse": "misuse"}
PROBABILITY_COLUMNS = {
    "ai_attacker": "ai_attacker_probability",
    "ai_vulnerability": "ai_vulnerability_probability",
    "ai_misuse": "ai_misuse_probability",
    "no_ai": "no_ai_probability",
}

INSTRUCTIONS = (
    "This is a reported cyber-security or data-breach event. Decide what part, if any, artificial "
    "intelligence played in THIS incident - the one named in `event` - judging from `records`: for each "
    "source record its title, `snippet`, its stored `page_text` (the whole page unless `page_text_cut` "
    "says otherwise; it may include site menus, other stories or boilerplate such as a privacy policy) "
    "and `research` (separate Perplexity research: attack method, threat actor, victim count, "
    "reasoning). A record plainly about a different incident from the one named in `event` is not "
    "evidence about this one; neither is another story on the same page. AI means an AI system: a "
    "large language model, AI agent, chatbot, generative-AI tool, deepfake, or machine-learning model. "
    "Go by what the reporting says happened: a statement by the victim, investigators or authorities "
    "that AI was used counts even when hedged ('likely'); a journalist's or commentator's speculation "
    "that AI might have been used does not. "
    "Decide in two steps. First: did an AI system play a reported part in the incident? Second, if it "
    "did: what part? If there was no attack at all and the incident IS the organisation's own use of AI - "
    "an AI system it chose to deploy collected, used or disclosed people's information unlawfully or "
    "improperly, or its staff or contractors put personal or confidential data into an AI tool - choose "
    "ai_misuse. Otherwise there was an attack, a manipulation or a fault, and the question is whose AI it "
    "was. If the AI was on the ATTACKING side - the attacker's own tool, or an AI agent that itself did "
    "the unauthorised access - choose ai_attacker. If the AI was on the VICTIM's side - a system the "
    "victim (or its supplier) owned, ran or had connected to its systems, which was attacked, "
    "manipulated, leaked data through a flaw or was the way in - choose ai_vulnerability, even when the "
    "attacker also used AI. Worked boundaries: an AI company's agent that broke into someone else's "
    "portal is ai_attacker (the portal's owner is the victim); a third-party AI tool an employee had "
    "connected to the company's accounts, whose compromise let attackers in, is ai_vulnerability; a "
    "deepfake call to staff is ai_attacker; a customer-service chatbot tricked into revealing data is "
    "ai_vulnerability; a hiring chatbot whose weak security exposed applicants' records is "
    "ai_vulnerability; a retailer's in-store facial recognition that the privacy regulator found "
    "unlawful is ai_misuse; a company scraping people's photos into its own face-recognition tool is "
    "ai_misuse; an employee uploading customers' records to ChatGPT is ai_misuse."
)
CRITERIA = {
    "ai_attacker": (
        "The AI was on the attacking side: an AI system or autonomous AI agent itself accessed systems or "
        "data without authorisation, or the reporting states that the attackers used their own AI to carry "
        "out the incident - AI-generated malware or phishing, a deepfake video or cloned voice used to "
        "deceive staff, or an AI tool that found and exploited an ordinary (non-AI) weakness."),
    "ai_vulnerability": (
        "The AI was on the victim's side and was the weak point: an AI system the victim or its supplier "
        "owned, ran or had connected to its systems was attacked, manipulated or leaked data - prompt "
        "injection, a jailbroken or manipulated chatbot or assistant, an AI model, its training data, "
        "prompts or conversation logs exposed or stolen, data poisoning, an AI feature that disclosed "
        "information through a flaw, or a compromised AI tool or integration through which the attackers "
        "got in. An ordinary breach of a company that happens to work on AI is not this unless its AI "
        "system was the weak point."),
    "ai_misuse": (
        "No one attacked or manipulated anything: the organisation's own use of AI was the breach. An AI "
        "system it chose to deploy collected, used or disclosed people's personal information unlawfully "
        "or improperly - facial recognition on customers or the public, scraping images or data into its "
        "own AI model, training or running AI on personal data without consent - or its staff or "
        "contractors disclosed personal or confidential data by entering it into an AI tool such as "
        "ChatGPT. The AI worked as intended (or as its users meant); the wrong is in how it was used."),
    "no_ai": (
        "No AI involvement is reported for this incident: an ordinary attack, mistake or system fault, or "
        "AI is mentioned only in passing - as speculation, general commentary, a defensive tool, the "
        "victim's line of business, or in another story on the page."),
}

DESCRIPTION_SYSTEM = (
    "You are a cyber-security analyst writing an entry for a list of cyber incidents that involved "
    "artificial intelligence. From the evidence given, write a factual description of THIS incident "
    "in two to four sentences: what happened, to which organisation, when (if known), what was "
    "affected, and the role AI played. Use only the evidence; do not speculate or add advice. "
    "Describe the incident itself as reported - never the evidence (do not mention snippets, search "
    "results, articles or what the evidence lacks); where a fact is only alleged, say 'reportedly'. "
    "Return only the description, as plain prose."
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS EventDescriptions (
    deduplicated_event_id TEXT NOT NULL,
    purpose               TEXT NOT NULL,   -- what the description is written for
    prompt_version        TEXT NOT NULL,
    input_hash            TEXT NOT NULL,   -- sha256 of evidence + prompt + model
    model                 TEXT NOT NULL,
    description           TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    PRIMARY KEY (deduplicated_event_id, purpose, input_hash)
);
"""


class AIInvolvementIncomplete(RuntimeError):
    """Some events could not be judged or described; what was done has been stored."""


@dataclass
class AIInvolvementReport:
    events: int = 0
    asked: int = 0                 # events that needed a Jev request
    reused: int = 0                # events answered from stored judgements
    updated: int = 0               # rows whose stored values changed (or would)
    described: int = 0             # AI events given a new description
    failed: Dict[str, str] = field(default_factory=dict)
    distribution: Dict[str, int] = field(default_factory=dict)
    cutoff: Optional[float] = None  # the calibrated cut-off applied; None = uncalibrated thresholds
    escalated: int = 0             # events whose Jev answer was unsettled and went to the reasoning model


#: Why a stored label is stale (:func:`stale_judgements`).
STALE_CHANGED = "evidence, question, model or calibration changed since it was judged"
STALE_FAILED = "last re-judgement failed"


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Add the label, basis, escalation, freshness and probability columns and the description table (idempotent)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(DeduplicatedEvents)")}
    if cols and "ai_involvement" not in cols:
        conn.execute("ALTER TABLE DeduplicatedEvents ADD COLUMN ai_involvement VARCHAR(20)")
    if cols and "ai_involvement_basis" not in cols:
        conn.execute("ALTER TABLE DeduplicatedEvents ADD COLUMN ai_involvement_basis TEXT")
    if cols and "ai_involvement_escalated" not in cols:
        conn.execute("ALTER TABLE DeduplicatedEvents ADD COLUMN ai_involvement_escalated INTEGER")
    for column in ("ai_involvement_input_hash", "ai_involvement_error"):
        if cols and column not in cols:
            conn.execute(f"ALTER TABLE DeduplicatedEvents ADD COLUMN {column} TEXT")
    for column in PROBABILITY_COLUMNS.values():
        if cols and column not in cols:
            conn.execute(f"ALTER TABLE DeduplicatedEvents ADD COLUMN {column} REAL")
    conn.executescript(SCHEMA)
    conn.commit()


def has_schema(conn: sqlite3.Connection) -> bool:
    """True when the database has the AI-involvement columns (readers must not add them)."""
    return "ai_involvement" in {r[1] for r in conn.execute("PRAGMA table_info(DeduplicatedEvents)")}


def question() -> Any:
    from typesafe_sdk import Choice

    return Choice(instructions=INSTRUCTIONS, criteria=CRITERIA)


def evidence_fingerprint() -> str:
    """A hash of everything that shapes the evidence the question sees (``breach_source.event_state``)."""
    from cyber_data_collector.dedup import calibration_guard
    from cyber_data_collector.processing import active_evidence
    from cyber_data_collector.processing import breach_source as bs
    from cyber_data_collector.utils import escalation

    return calibration_guard.fingerprint(
        "ai-involvement", version=EVIDENCE_VERSION, max_records=bs.MAX_RECORDS, page_chars=bs.PAGE_CHARS,
        event_page_chars=bs.EVENT_PAGE_CHARS, other_titles=bs.OTHER_TITLES, snippet_chars=bs.SNIPPET_CHARS,
        reasoning_chars=bs.REASONING_CHARS, active_evidence=active_evidence.rules_fingerprint(),
        escalation_effort=escalation.REASONING_EFFORT)


def calibration_identity() -> Dict[str, str]:
    """What a calibration must have been measured on to apply: question, escalation, both models, evidence."""
    from cyber_data_collector.utils.llm_models import openai_model, typesafe_model

    return {"question_version": f"{QUESTION_VERSION}+{ESCALATION_VERSION}",
            "model": f"{typesafe_model()}+{openai_model()}", "evidence": evidence_fingerprint()}


def active_calibration(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The calibration, only if it was measured on the current model and question and chose a cut-off."""
    path = path or CALIBRATION_PATH
    try:
        cal = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("AI involvement has no calibration (%s): labels are the most probable option", path)
        return None
    except (OSError, ValueError) as exc:
        logger.error("AI involvement calibration unreadable (%s): %s - labels are the most probable option",
                     path, exc)
        return None
    from cyber_data_collector.dedup import calibration_guard

    reason = calibration_guard.mismatch(cal, calibration_identity())
    if reason:
        logger.warning("AI involvement calibration does not apply (%s): labels are the most probable "
                       "option until scripts/calibrate_ai_involvement.py calibrate is rerun", reason)
        return None
    if cal.get("cutoff") is None:
        return None
    from cyber_data_collector.dedup import deployment

    # A calibration the deployment rule holds back is treated as a stale one: labels are the most probable option.
    return deployment.gate("ai-involvement", cal, Path(path))


def labelled_events(path: Optional[Path] = None) -> Dict[str, str]:
    """``{deduplicated_event_id: label}`` from the labels file (empty when there is none)."""
    path = path or LABELS_PATH
    try:
        with Path(path).open(encoding="utf-8") as fh:
            return {r["deduplicated_event_id"]: (r.get("label") or "").strip() for r in csv.DictReader(fh)}
    except FileNotFoundError:
        return {}


def probabilities_from_judgement(judgement: Any) -> Dict[str, float]:
    """Jev's distribution over the four options (raises on an unknown option or a missing probability)."""
    if judgement.answer not in LABELS:
        raise ValueError(f"Jev returned unknown AI-involvement option {judgement.answer!r}")
    probs = judgement.probabilities or {}
    missing = [opt for opt in LABELS if opt not in probs]
    if missing:
        raise ValueError(f"Jev returned no probability for {', '.join(missing)}")
    return {opt: float(probs[opt]) for opt in LABELS}


def is_settled(probs: Mapping[str, float]) -> bool:
    """One option dominates: the top reaches DOMINANT or leads the runner-up by MARGIN."""
    top, second = sorted(probs.values(), reverse=True)[:2]
    return top >= DOMINANT or top - second >= MARGIN


def decide(probs: Mapping[str, float], cutoff: Optional[float] = None) -> Tuple[str, str]:
    """``(label, basis)`` from a distribution.

    Involvement: P(AI) = 1 - P(No-AI) at or above the calibrated ``cutoff``;
    without one, at or above INVOLVED (and AI-uncertain between NOT_INVOLVED
    and INVOLVED). Category: the most probable of attacker, vulnerability and
    misuse, only when it holds CATEGORY_SHARE of P(AI); otherwise AI-uncertain.
    """
    p_ai = 1.0 - probs["no_ai"]
    if cutoff is not None:
        if p_ai < cutoff:
            return NO_AI, f"P(AI) {p_ai:.2f} < calibrated cut-off {cutoff:.3f}"
        rule = f"P(AI) {p_ai:.2f} >= calibrated cut-off {cutoff:.3f}"
    else:
        if p_ai <= NOT_INVOLVED:
            return NO_AI, f"P(AI) {p_ai:.2f} <= {NOT_INVOLVED} (not calibrated)"
        if p_ai < INVOLVED:
            return AI_UNCERTAIN, f"P(AI) {p_ai:.2f} between {NOT_INVOLVED} and {INVOLVED} (not calibrated)"
        rule = f"P(AI) {p_ai:.2f} >= {INVOLVED} (not calibrated)"
    total = sum(probs[o] for o in AI_OPTIONS)
    shares = {o: probs[o] / total if total else 0.0 for o in AI_OPTIONS}
    top = max(AI_OPTIONS, key=lambda o: shares[o])
    if shares[top] >= CATEGORY_SHARE:
        return LABELS[top], f"{rule}; {CATEGORY_NAMES[top]} {shares[top]:.0%} of it"
    split = " / ".join(f"{CATEGORY_NAMES[o]} {shares[o]:.0%}" for o in AI_OPTIONS)
    return AI_UNCERTAIN, f"{rule}; {split} - category unsettled"


def column_values(probs: Mapping[str, float], escalated: bool, cutoff: Optional[float] = None,
                  label: Optional[str] = None) -> Dict[str, Any]:
    """The stored columns: label, how it was decided, whether escalated, and the final probabilities."""
    if label in LABELS:
        decided, basis = LABELS[label], BASIS_LABEL
    else:
        decided, basis = decide(probs, cutoff)
        if escalated:
            basis = f"escalated (Jev unsettled); {basis}"
    values: Dict[str, Any] = {"ai_involvement": decided, "ai_involvement_basis": basis,
                              "ai_involvement_escalated": int(escalated)}
    for option, column in PROBABILITY_COLUMNS.items():
        values[column] = float(probs[option])
    return values


def values_from_judgement(judgement: Any, cutoff: Optional[float] = None,
                          label: Optional[str] = None) -> Dict[str, Any]:
    """Column values from a Jev answer alone (no escalation)."""
    return column_values(probabilities_from_judgement(judgement), False, cutoff, label)


def escalation_question() -> str:
    return INSTRUCTIONS


def stored_probabilities(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """``{event: {"ai": P(AI), "category": option}}`` - the final (escalated where unsettled)
    probabilities stored on every judged active event."""
    out: Dict[str, Dict[str, Any]] = {}
    columns = ", ".join(PROBABILITY_COLUMNS[o] for o in AI_OPTIONS)
    rows = conn.execute(f"SELECT deduplicated_event_id, {columns}, no_ai_probability FROM DeduplicatedEvents "
                        "WHERE status = 'Active' AND no_ai_probability IS NOT NULL").fetchall()
    for subject, *ai_probs, no_ai in rows:
        by_option = {o: float(p or 0.0) for o, p in zip(AI_OPTIONS, ai_probs)}
        out[subject] = {"ai": 1.0 - float(no_ai), "category": max(AI_OPTIONS, key=lambda o: by_option[o])}
    return out


def _active_events(conn: sqlite3.Connection, limit: Optional[int]) -> List[Dict[str, Any]]:
    columns = ", ".join(["deduplicated_event_id", "ai_involvement", "ai_involvement_basis",
                         "ai_involvement_escalated", "ai_involvement_input_hash", "ai_involvement_error",
                         *PROBABILITY_COLUMNS.values()])
    sql = (f"SELECT {columns} FROM DeduplicatedEvents WHERE status = 'Active' "
           "ORDER BY event_date DESC, deduplicated_event_id")
    if limit:
        sql += f" LIMIT {int(limit)}"
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.row_factory = previous


def judgement_hash(state: Mapping[str, Any], cutoff: Optional[float], label: Optional[str]) -> str:
    """What a stored label was decided on: evidence, question, both models, cut-off and any label."""
    blob = json.dumps({"state": state, "question": f"{QUESTION_VERSION}+{ESCALATION_VERSION}",
                       "model": calibration_identity()["model"], "cutoff": cutoff, "label": label},
                      sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _stale_reason(stored_hash: Optional[str], error: Optional[str], state: Mapping[str, Any],
                  cutoff: Optional[float], label: Optional[str]) -> Optional[str]:
    if error:
        return f"{STALE_FAILED}: {error}"
    if stored_hash != judgement_hash(state, cutoff, label):
        return STALE_CHANGED
    return None


def stale_judgements(conn: sqlite3.Connection, period: Any = None) -> Dict[str, str]:
    """``{event: reason}`` for labelled active incidents (in ``period``) whose label is out of date.

    Out of date: the last re-judgement failed, or the current evidence,
    question, models, cut-off or label differ from what the label was decided
    on (a label stored before input hashes were kept counts as changed).
    Unjudged incidents are not listed - they have no label to be stale.
    """
    from cyber_data_collector.reporting.population import incident_filter

    if not has_schema(conn):
        return {}
    present = {r[1] for r in conn.execute("PRAGMA table_info(DeduplicatedEvents)")}
    optional = ", ".join(f"de.{c}" if c in present else "NULL"
                         for c in ("ai_involvement_input_hash", "ai_involvement_error"))
    where, params = incident_filter(period, "de")
    calibration = active_calibration()
    cutoff = calibration["cutoff"] if calibration else None
    labels = labelled_events()
    out: Dict[str, str] = {}
    for event_id, stored_hash, error in conn.execute(
            f"SELECT de.deduplicated_event_id, {optional} FROM DeduplicatedEvents de "
            f"WHERE {where} AND de.ai_involvement IS NOT NULL", params).fetchall():
        reason = _stale_reason(stored_hash, error, event_state(conn, event_id), cutoff, labels.get(event_id))
        if reason:
            out[event_id] = reason
    return out


def description_hash(state: Mapping[str, Any], model: str) -> str:
    blob = json.dumps({"state": state, "system": DESCRIPTION_SYSTEM, "version": DESCRIPTION_PROMPT_VERSION,
                       "model": model}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def stored_description(conn: sqlite3.Connection, dedup_id: str,
                       digest: Optional[str] = None) -> Optional[str]:
    """The latest stored description for an event (for ``digest`` only, when given)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'EventDescriptions'"
                        ).fetchone():
        return None
    sql = "SELECT description FROM EventDescriptions WHERE deduplicated_event_id = ? AND purpose = ?"
    params: List[Any] = [dedup_id, DESCRIPTION_PURPOSE]
    if digest is not None:
        sql += " AND input_hash = ?"
        params.append(digest)
    row = conn.execute(sql + " ORDER BY created_at DESC, rowid DESC LIMIT 1", params).fetchone()
    return row[0] if row else None


def openai_describer(client: Any = None) -> Callable[[Mapping[str, Any]], str]:
    """Write a description with the fast OpenAI model (``OPENAI_FAST_MODEL``)."""
    from cyber_data_collector.utils.llm_models import chat_params, openai_fast_model

    if client is None:
        from openai import OpenAI
        client = OpenAI()

    def describe(state: Mapping[str, Any]) -> str:
        response = client.chat.completions.create(
            **chat_params(openai_fast_model(), max_tokens=400),
            messages=[{"role": "system", "content": DESCRIPTION_SYSTEM},
                      {"role": "user", "content": json.dumps(state, ensure_ascii=False, default=str)}])
        return (response.choices[0].message.content or "").strip()

    return describe


def describe_event(conn: sqlite3.Connection, dedup_id: str, state: Mapping[str, Any],
                   describer: Callable[[Mapping[str, Any]], str], model: str) -> bool:
    """Store a description for the event unless one exists for this evidence. True when written."""
    digest = description_hash(state, model)
    if stored_description(conn, dedup_id, digest) is not None:
        return False
    text = describer(state)
    if not text:
        raise ValueError("the description model returned no text")
    conn.execute("INSERT INTO EventDescriptions VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (dedup_id, DESCRIPTION_PURPOSE, DESCRIPTION_PROMPT_VERSION, digest, model, text,
                  datetime.now().isoformat()))
    conn.commit()
    return True


def classify_events(conn: sqlite3.Connection, dry_run: bool = False, limit: Optional[int] = None,
                    judge: Any = None, describer: Optional[Callable[[Mapping[str, Any]], str]] = None,
                    describe: bool = True, escalation_client: Any = None) -> AIInvolvementReport:
    """Judge every active event (asking Jev only what is not stored) and describe the AI ones.

    ``dry_run`` still asks Jev for events with no stored answer (answers are
    stored, so a later real run reuses them) but writes no event column and no
    description. ``describe=False`` skips the descriptions.

    Raises:
        JevUnavailable: Jev is not configured (checked before anything runs).
        AIInvolvementIncomplete: after storing everything it could, when any
            event failed; the report is attached as ``exc.report``.
    """
    from cyber_data_collector.utils import jev
    from cyber_data_collector.utils.api_budget import is_budget_refusal
    from cyber_data_collector.utils.llm_models import openai_fast_model

    if judge is None:
        jev.require()
    ensure_schema(conn)
    owned = judge is None
    judge = judge or jev.JevJudge(conn)
    describe = describe and not dry_run
    if describe and describer is None:
        describer = openai_describer()
    qs = {QUESTION_ID: question()}
    report = AIInvolvementReport()
    calibration = active_calibration()
    report.cutoff = calibration["cutoff"] if calibration else None
    labels = labelled_events()
    events = _active_events(conn, limit)
    report.events = len(events)
    try:
        for i, event in enumerate(events, 1):
            event_id = event["deduplicated_event_id"]
            calls_before = judge.calls
            state = event_state(conn, event_id)
            try:
                answers = judge.ask("deduplicated_event", event_id, QUESTION_VERSION, state, qs)
                probs = probabilities_from_judgement(answers[QUESTION_ID])
                escalated = False
                if not is_settled(probs) and labels.get(event_id) not in LABELS:
                    # No dominant option: the reasoning model works through the same evidence.
                    from cyber_data_collector.utils.escalation import escalate_choice

                    better = escalate_choice(conn, "deduplicated_event", event_id, ESCALATION_QUESTION_ID,
                                             ESCALATION_VERSION, state, escalation_question(), CRITERIA,
                                             client=escalation_client)
                    if better is None:
                        raise RuntimeError("Jev's answer was unsettled and the escalation failed")
                    probs, escalated = better, True
                    report.escalated += 1
                values = column_values(probs, escalated, report.cutoff, labels.get(event_id))
                values["ai_involvement_input_hash"] = judgement_hash(state, report.cutoff, labels.get(event_id))
                values["ai_involvement_error"] = None
            except Exception as exc:  # noqa: BLE001 - recorded and raised at the end
                report.failed[event_id] = f"{type(exc).__name__}: {exc}"
                logger.error("AI-involvement judgement failed for %s: %s", event_id, exc)
                if not dry_run:
                    # The old label stays, marked: readers report it stale, not current.
                    conn.execute("UPDATE DeduplicatedEvents SET ai_involvement_error = ? "
                                 "WHERE deduplicated_event_id = ?", (report.failed[event_id][:500], event_id))
                if is_budget_refusal(exc):
                    break   # every later request would be refused too
                continue
            if judge.calls > calls_before:
                report.asked += 1
            else:
                report.reused += 1
            label = values["ai_involvement"]
            report.distribution[label] = report.distribution.get(label, 0) + 1
            if any(event.get(k) != v for k, v in values.items()):
                report.updated += 1
                if not dry_run:
                    assignments = ", ".join(f"{k} = ?" for k in values)
                    conn.execute(f"UPDATE DeduplicatedEvents SET {assignments} WHERE deduplicated_event_id = ?",
                                 (*values.values(), event_id))
            if describe and label in AI_INVOLVED_LABELS:
                try:
                    report.described += describe_event(conn, event_id, state, describer, openai_fast_model())
                except Exception as exc:  # noqa: BLE001 - recorded and raised at the end
                    report.failed[event_id] = f"description: {type(exc).__name__}: {exc}"
                    logger.error("AI incident description failed for %s: %s", event_id, exc)
                    if is_budget_refusal(exc):
                        break
            if i % 100 == 0:
                if not dry_run:
                    conn.commit()
                logger.info("AI involvement: %d/%d events (%d asked, %d reused)",
                            i, len(events), report.asked, report.reused)
        if not dry_run:
            conn.commit()
    finally:
        if owned:
            judge.close()
    if report.failed:
        exc = AIInvolvementIncomplete(
            f"{len(report.failed)} of {report.events} event(s) could not be judged or described; "
            "the rest are stored - rerun to finish")
        exc.report = report  # type: ignore[attr-defined]
        raise exc
    return report


def ai_incidents(conn: sqlite3.Connection, period: Any = None) -> List[Dict[str, Any]]:
    """Active incidents with (possible) AI involvement, oldest first, with their descriptions.

    ``period`` is a :class:`ReportingPeriod`; ``None`` lists every active
    incident, dated or not. Victims are the ``victim`` roles (an incident with
    several co-equal victims lists each), else ``victim_organization_name``.
    ``stale`` is why the label is out of date (:func:`stale_judgements`), else
    None. ``description`` is the one written from the current evidence; when
    there is none, the latest older one, with ``description_stale`` True.
    """
    from cyber_data_collector.reporting.population import incident_filter
    from cyber_data_collector.utils.llm_models import openai_fast_model

    if not has_schema(conn):
        return []
    where, params = incident_filter(period, "de")
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    present = {r[1] for r in conn.execute("PRAGMA table_info(DeduplicatedEvents)")}
    optional = ", ".join(f"de.{c} AS {c}" if c in present else f"NULL AS {c}"
                         for c in ("ai_involvement_basis", "ai_misuse_probability",
                                   "ai_involvement_input_hash", "ai_involvement_error"))
    marks = ", ".join("?" for _ in AI_INVOLVED_LABELS)
    calibration = active_calibration()
    cutoff = calibration["cutoff"] if calibration else None
    labels = labelled_events()
    describer_model = openai_fast_model()
    try:
        rows = [dict(r) for r in conn.execute(
            f"""SELECT de.deduplicated_event_id, de.event_date, de.title, de.victim_organization_name,
                       de.ai_involvement, de.ai_attacker_probability, de.ai_vulnerability_probability,
                       de.no_ai_probability, {optional}
                FROM DeduplicatedEvents de
                WHERE {where} AND de.ai_involvement IN ({marks})
                ORDER BY de.event_date IS NULL, de.event_date, de.deduplicated_event_id""",
            [*params, *AI_INVOLVED_LABELS])]
        for row in rows:
            victims = [r[0] for r in conn.execute(
                """SELECT DISTINCT e.entity_name FROM DeduplicatedEventEntities dee
                   JOIN EntitiesV2 e ON e.entity_id = dee.entity_id
                   WHERE dee.deduplicated_event_id = ? AND dee.relationship_type = 'victim'
                   ORDER BY e.entity_name""", (row["deduplicated_event_id"],)) if r[0]]
            row["victims"] = victims or ([row["victim_organization_name"]] if row["victim_organization_name"]
                                         else [])
            state = event_state(conn, row["deduplicated_event_id"])
            row["stale"] = _stale_reason(row["ai_involvement_input_hash"], row["ai_involvement_error"], state,
                                         cutoff, labels.get(row["deduplicated_event_id"]))
            current = stored_description(conn, row["deduplicated_event_id"],
                                         description_hash(state, describer_model))
            row["description"] = current or stored_description(conn, row["deduplicated_event_id"])
            row["description_stale"] = current is None and row["description"] is not None
            option = next((o for o in AI_OPTIONS if LABELS[o] == row["ai_involvement"]), None)
            row["probability"] = row[PROBABILITY_COLUMNS[option]] if option else None
            row["p_ai"] = 1.0 - row["no_ai_probability"] if row["no_ai_probability"] is not None else None
    finally:
        conn.row_factory = previous
    return rows
