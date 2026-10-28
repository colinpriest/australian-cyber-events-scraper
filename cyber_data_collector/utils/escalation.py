"""Escalate uncertain judgements to a reasoning model, and keep every answer.

Jev answers in one fast step, without reasoning or tools; most cases need no
more. The cases it puts in the middle - neither clearly yes nor clearly no -
are where the labellers who calibrated the filters did better, by working
through the evidence. Those cases, and only those, are sent to the strong
OpenAI model (:func:`llm_models.openai_model`) with reasoning turned up, over
the same evidence Jev saw (including any organisation timeline and
corroboration search). It returns a probability and its rationale.

A reasoning model is no more repeatable than Jev (GPT-5 accepts no
temperature with reasoning on), so answers are stored in ``ModelJudgements``
alongside Jev's - ``primitive='escalation'``, keyed by a hash of the evidence,
the question, the model and the reasoning effort - and reused on re-runs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Dict, Mapping, Optional

from pydantic import BaseModel, Field

from cyber_data_collector.utils.jev import ensure_schema
from cyber_data_collector.utils.llm_models import chat_params, openai_model

logger = logging.getLogger(__name__)

REASONING_EFFORT = "high"


class EscalatedJudgement(BaseModel):
    """A reasoned yes/no judgement with a probability."""

    rationale: str = Field(description="Two to four sentences: the decisive evidence, including "
                                       "anything in the organisation timeline or search that settles it.")
    probability: float = Field(ge=0.0, le=1.0, description="Probability that the answer is YES.")


def _hash(state: Mapping[str, Any], question: str, model: str) -> str:
    blob = json.dumps({"state": state, "question": question, "model": model, "effort": REASONING_EFFORT},
                      sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _client():
    import instructor
    from openai import OpenAI
    # JSON mode: GPT-5 rejects function tools - instructor's default - when
    # reasoning is on in chat completions (verified 2026-10-27).
    return instructor.from_openai(OpenAI(), mode=instructor.Mode.JSON)


def escalate(conn: sqlite3.Connection, subject_type: str, subject_id: str, question_id: str,
             question_version: str, state: Mapping[str, Any], question: str,
             client=None) -> Optional[float]:
    """P(yes) from the reasoning model; stored answers are reused. None on failure."""
    ensure_schema(conn)
    model = openai_model()
    digest = _hash(state, question, model)
    row = conn.execute("SELECT value FROM ModelJudgements WHERE question_id = ? AND input_hash = ? "
                       "ORDER BY created_at DESC, rowid DESC LIMIT 1", (question_id, digest)).fetchone()
    if row is not None:
        return float(row[0])
    client = client or _client()
    try:
        judgement, completion = client.chat.completions.create_with_completion(
            **chat_params(model, reasoning_effort=REASONING_EFFORT),
            response_model=EscalatedJudgement,
            messages=[
                {"role": "system", "content": "You check records in an Australian cyber-incident research "
                                              "database. Answer the question from the evidence given; think it "
                                              "through before answering."},
                {"role": "user", "content": f"QUESTION: {question}\n\nEVIDENCE (JSON):\n"
                                            f"{json.dumps(state, ensure_ascii=False, default=str)}"},
            ],
        )
    except Exception as exc:  # noqa: BLE001 - a failed escalation leaves Jev's answer in force
        logger.warning("Escalation failed for %s %s: %s", subject_type, subject_id, exc)
        return None
    usage = getattr(completion, "usage", None)
    conn.execute(
        "INSERT INTO ModelJudgements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), subject_type, subject_id, question_id, question_version, "escalation",
         "yes" if judgement.probability >= 0.5 else "no", judgement.probability, None,
         json.dumps({"yes": judgement.probability, "no": 1 - judgement.probability,
                     "rationale": judgement.rationale}),
         json.dumps({"question": question, "reasoning_effort": REASONING_EFFORT}),
         digest, model, getattr(completion, "model", None),
         getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None),
         datetime.now().isoformat()))
    conn.commit()
    return float(judgement.probability)


class EscalatedChoice(BaseModel):
    """A reasoned choice among named options, with a probability for each."""

    rationale: str = Field(description="Two to four sentences: the decisive evidence and why the other "
                                       "options do not fit.")
    probabilities: Dict[str, float] = Field(description="Probability of each option, by option name; "
                                                        "they must sum to 1.")


def escalate_choice(conn: sqlite3.Connection, subject_type: str, subject_id: str, question_id: str,
                    question_version: str, state: Mapping[str, Any], question: str,
                    options: Mapping[str, str], client=None) -> Optional[Dict[str, float]]:
    """A distribution over ``options`` from the reasoning model; stored answers are reused.

    ``options`` maps each option name to its definition. None on failure or an
    answer that does not cover exactly those options - never a guess.
    """
    ensure_schema(conn)
    model = openai_model()
    full_question = (f"{question}\n\nOPTIONS (answer with a probability for each, by name):\n"
                     + "\n".join(f"- {name}: {text}" for name, text in options.items()))
    digest = _hash(state, full_question, model)
    row = conn.execute("SELECT probabilities_json FROM ModelJudgements WHERE question_id = ? AND input_hash = ? "
                       "ORDER BY created_at DESC, rowid DESC LIMIT 1", (question_id, digest)).fetchone()
    if row is not None:
        stored = json.loads(row[0])
        return {name: float(stored[name]) for name in options}
    client = client or _client()
    try:
        judgement, completion = client.chat.completions.create_with_completion(
            **chat_params(model, reasoning_effort=REASONING_EFFORT),
            response_model=EscalatedChoice,
            messages=[
                {"role": "system", "content": "You check records in an Australian cyber-incident research "
                                              "database. Answer the question from the evidence given; think it "
                                              "through before answering."},
                {"role": "user", "content": f"QUESTION: {full_question}\n\nEVIDENCE (JSON):\n"
                                            f"{json.dumps(state, ensure_ascii=False, default=str)}"},
            ],
        )
    except Exception as exc:  # noqa: BLE001 - recorded by the caller; the item stays undecided
        logger.warning("Escalation failed for %s %s: %s", subject_type, subject_id, exc)
        return None
    probs = judgement.probabilities
    total = sum(max(0.0, float(probs.get(name, 0.0))) for name in options)
    if set(probs) != set(options) or total <= 0:
        logger.warning("Escalation for %s %s answered options %s, not %s", subject_type, subject_id,
                       sorted(probs), sorted(options))
        return None
    probs = {name: max(0.0, float(probs[name])) / total for name in options}
    usage = getattr(completion, "usage", None)
    conn.execute(
        "INSERT INTO ModelJudgements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), subject_type, subject_id, question_id, question_version, "escalation_choice",
         max(probs, key=probs.get), max(probs.values()), None,
         json.dumps({**probs, "rationale": judgement.rationale}),
         json.dumps({"question": full_question, "reasoning_effort": REASONING_EFFORT}),
         digest, model, getattr(completion, "model", None),
         getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None),
         datetime.now().isoformat()))
    conn.commit()
    return probs
