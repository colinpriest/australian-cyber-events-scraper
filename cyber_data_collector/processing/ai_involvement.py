"""AI involvement in every active event: AI as the attacker, the victim's AI attacked, or none.

Each active event is asked one Jev Choice over three options, from the same
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
* ``No-AI``: no AI involvement is reported.

The chosen label and all three probabilities are stored on
``DeduplicatedEvents`` (``ai_involvement``, ``ai_attacker_probability``,
``ai_vulnerability_probability``, ``no_ai_probability``); the full answer, the
question as sent and the input hash are in ``ModelJudgements`` and are reused
while the evidence, the question and the model are unchanged, so a rerun asks
only about new or changed events and a rebuilt ``DeduplicatedEvents`` is
refilled from the stored answers.

The label is the Choice's most probable option and is **not calibrated** on
labelled examples; the probabilities are stored so a cut-off can be chosen
later without re-asking.

Events with AI involvement also get a 2-4 sentence description, written by the
fast OpenAI model from the same evidence for the AI incident report and stored
in ``EventDescriptions`` (reused while the evidence, prompt and model are
unchanged). Jev is required: if it is unavailable nothing runs, and if any
event cannot be judged or described the step raises after storing the rest.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional

from cyber_data_collector.processing.breach_source import event_state

logger = logging.getLogger(__name__)

QUESTION_VERSION = "ai-involvement-v1"
QUESTION_ID = "ai_involvement"
DESCRIPTION_PURPOSE = "ai_incident_report"
DESCRIPTION_PROMPT_VERSION = "ai-description-v1"

AI_ATTACKER = "AI-attacker"
AI_VULNERABILITY = "AI-vulnerability"
NO_AI = "No-AI"
#: The two labels that count as AI involvement (charted and listed).
AI_LABELS = (AI_ATTACKER, AI_VULNERABILITY)

#: Jev option -> stored label, and -> probability column.
LABELS = {"ai_attacker": AI_ATTACKER, "ai_vulnerability": AI_VULNERABILITY, "no_ai": NO_AI}
PROBABILITY_COLUMNS = {
    "ai_attacker": "ai_attacker_probability",
    "ai_vulnerability": "ai_vulnerability_probability",
    "no_ai": "no_ai_probability",
}

INSTRUCTIONS = (
    "This is a reported cyber-security or data-breach event. Decide what part, if any, artificial "
    "intelligence played in THIS incident, judging from `event` (its title) and `records` - for each "
    "source record its title, `snippet`, an excerpt of its stored `page_text` (which may be boilerplate "
    "such as a site menu or privacy policy rather than the article) and `research` (separate Perplexity "
    "research: attack method, threat actor, victim count, reasoning). AI means an AI system: a large "
    "language model, AI agent, chatbot, generative-AI tool, deepfake, or machine-learning model. Go by "
    "what the reporting says happened, not by speculation that AI might have been used."
)
CRITERIA = {
    "ai_attacker": (
        "AI was the attacker or the attacker's instrument: an AI system or autonomous AI agent itself "
        "accessed systems or data without authorisation (for example an AI company's agent reaching a "
        "government portal during testing), or the reporting states that the attackers used AI to carry "
        "out the incident - AI-generated malware or phishing, a deepfake video or cloned voice used to "
        "deceive staff, or an AI tool that found and exploited the weakness."),
    "ai_vulnerability": (
        "The victim's own AI system was what was attacked, misused or leaked: prompt injection, a "
        "jailbroken or manipulated chatbot or AI assistant, an AI model, its training data, prompts or "
        "conversation logs exposed or stolen, data poisoning, or an AI feature that disclosed information "
        "it should not have. An ordinary breach of a company that happens to work on AI is not this "
        "unless its AI system was the weak point."),
    "no_ai": (
        "No AI involvement is reported: an ordinary attack, mistake or system fault, or AI is mentioned "
        "only in passing - as speculation, general commentary, a defensive tool, or the victim's line of "
        "business."),
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


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Add the four columns and the description table (idempotent)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(DeduplicatedEvents)")}
    if cols and "ai_involvement" not in cols:
        conn.execute("ALTER TABLE DeduplicatedEvents ADD COLUMN ai_involvement VARCHAR(20)")
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


def values_from_judgement(judgement: Any) -> Dict[str, Any]:
    """Column values from the Choice: the most probable label and each option's probability."""
    if judgement.answer not in LABELS:
        raise ValueError(f"Jev returned unknown AI-involvement option {judgement.answer!r}")
    probs = judgement.probabilities or {}
    missing = [opt for opt in LABELS if opt not in probs]
    if missing:
        raise ValueError(f"Jev returned no probability for {', '.join(missing)}")
    values: Dict[str, Any] = {"ai_involvement": LABELS[judgement.answer]}
    for option, column in PROBABILITY_COLUMNS.items():
        values[column] = float(probs[option])
    return values


def _active_events(conn: sqlite3.Connection, limit: Optional[int]) -> List[Dict[str, Any]]:
    columns = ", ".join(["deduplicated_event_id", "ai_involvement", *PROBABILITY_COLUMNS.values()])
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
                    describe: bool = True) -> AIInvolvementReport:
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
    events = _active_events(conn, limit)
    report.events = len(events)
    try:
        for i, event in enumerate(events, 1):
            event_id = event["deduplicated_event_id"]
            calls_before = judge.calls
            state = event_state(conn, event_id)
            try:
                answers = judge.ask("deduplicated_event", event_id, QUESTION_VERSION, state, qs)
                values = values_from_judgement(answers[QUESTION_ID])
            except Exception as exc:  # noqa: BLE001 - recorded and raised at the end
                report.failed[event_id] = f"{type(exc).__name__}: {exc}"
                logger.error("AI-involvement judgement failed for %s: %s", event_id, exc)
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
            if describe and label in AI_LABELS:
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
    """Active incidents labelled AI-attacker or AI-vulnerability, oldest first, with their descriptions.

    ``period`` is a :class:`ReportingPeriod`; ``None`` lists every active
    incident, dated or not. Victims are the ``victim`` roles (an incident with
    several co-equal victims lists each), else ``victim_organization_name``.
    """
    from cyber_data_collector.reporting.population import incident_filter

    if not has_schema(conn):
        return []
    where, params = incident_filter(period, "de")
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            f"""SELECT de.deduplicated_event_id, de.event_date, de.title, de.victim_organization_name,
                       de.ai_involvement, de.ai_attacker_probability, de.ai_vulnerability_probability,
                       de.no_ai_probability
                FROM DeduplicatedEvents de
                WHERE {where} AND de.ai_involvement IN (?, ?)
                ORDER BY de.event_date IS NULL, de.event_date, de.deduplicated_event_id""",
            [*params, *AI_LABELS])]
        for row in rows:
            victims = [r[0] for r in conn.execute(
                """SELECT DISTINCT e.entity_name FROM DeduplicatedEventEntities dee
                   JOIN EntitiesV2 e ON e.entity_id = dee.entity_id
                   WHERE dee.deduplicated_event_id = ? AND dee.relationship_type = 'victim'
                   ORDER BY e.entity_name""", (row["deduplicated_event_id"],)) if r[0]]
            row["victims"] = victims or ([row["victim_organization_name"]] if row["victim_organization_name"]
                                         else [])
            row["description"] = stored_description(conn, row["deduplicated_event_id"])
            row["probability"] = (row["ai_attacker_probability"] if row["ai_involvement"] == AI_ATTACKER
                                  else row["ai_vulnerability_probability"])
    finally:
        conn.row_factory = previous
    return rows
