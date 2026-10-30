"""Ask Jev (TypeSafe System One) bounded questions, and keep every answer.

Jev returns typed answers with probabilities - a Choice's distribution over its
options, a Noul's probability of yes, a Score's position and distribution over
levels - which is what filtering false-positive events and scoring duplicate
stories need: a threshold can be set, audited and moved without re-asking.

**Every answer is stored** in ``ModelJudgements``, with the exact question,
a hash of the input, the model version that answered and the full
distribution. Jev is not fully deterministic (the same question can come back
0.99 one call and 0.98 the next), so a re-run must not re-ask: a stored answer
is reused whenever the question, the input and the requested model are
unchanged, and a new call is made only when one of them changes. The database
is therefore the record of what was decided and why; a reviewer replaying the
pipeline gets the same decisions, and ``refresh=True`` re-asks deliberately.

The model is :func:`llm_models.typesafe_model` (``TYPESAFE_MODEL``, pinned to
a dated version by default). The response's own model string is stored too, so
an alias can never hide which version answered.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from cyber_data_collector.utils.llm_models import typesafe_model
from cyber_data_collector.utils.timestamps import utc_now_iso

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ModelJudgements (
    judgement_id      TEXT PRIMARY KEY,
    subject_type      TEXT NOT NULL,   -- enriched_event, event_pair, deduplicated_event, ...
    subject_id        TEXT NOT NULL,   -- for a pair: the two ids joined by '|', sorted
    question_id       TEXT NOT NULL,   -- stable name, e.g. 'page_kind'
    question_version  TEXT NOT NULL,   -- bumped whenever the wording or the input changes
    primitive         TEXT NOT NULL,   -- choice / noul / score
    answer            TEXT,            -- chosen option; 'yes'/'no' for a noul at 0.5
    value             REAL,            -- noul probability, score value, or chosen option's probability
    confidence        REAL,            -- choice/score concentration (NULL for a noul)
    probabilities_json TEXT,           -- full distribution as returned
    question_json     TEXT NOT NULL,   -- the question exactly as sent
    input_hash        TEXT NOT NULL,   -- sha256 of state + question + requested model
    model_requested   TEXT NOT NULL,
    model             TEXT,            -- the version that answered
    input_tokens      INTEGER,         -- for the whole request, repeated on each answer
    output_tokens     INTEGER,
    created_at        TEXT NOT NULL    -- UTC with offset (utils.timestamps); rows before 2026-10-30: naive local
);                                     -- time, so the newest answer is the highest rowid, not the largest stamp
CREATE INDEX IF NOT EXISTS idx_judgements_lookup
    ON ModelJudgements (question_id, input_hash);
CREATE INDEX IF NOT EXISTS idx_judgements_subject
    ON ModelJudgements (subject_type, subject_id, question_id);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@dataclass
class Judgement:
    """One answer, from a call or from the store."""

    question_id: str
    primitive: str
    answer: Optional[str]
    value: Optional[float]
    confidence: Optional[float]
    probabilities: Dict[str, float] = field(default_factory=dict)
    model: Optional[str] = None
    cached: bool = False


def available() -> bool:
    """True when a TypeSafe key is configured and the SDK is installed."""
    if not os.getenv("TYPESAFE_API_KEY"):
        return False
    try:
        import typesafe_sdk  # noqa: F401
    except ImportError:
        return False
    return True


class JevUnavailable(RuntimeError):
    """Jev is required and cannot be used. Never substitute another model for it."""


def require() -> None:
    """Raise :class:`JevUnavailable` unless Jev can be called.

    Steps that need Jev fail loudly instead of skipping or switching model: a
    silent fallback once let a whole run judge nothing (and classify pages with
    a different, more expensive model) while reporting success.
    """
    if not os.getenv("TYPESAFE_API_KEY"):
        raise JevUnavailable("TYPESAFE_API_KEY is not set (is the project .env loaded?); "
                             "this step requires Jev and will not substitute another model")
    try:
        import typesafe_sdk  # noqa: F401
    except ImportError as exc:
        raise JevUnavailable("typesafe-sdk is not installed (pip install -r requirements.lock)") from exc


def _question_payload(question: Any) -> Dict[str, Any]:
    return question.model_dump() if hasattr(question, "model_dump") else dict(question)


def input_hash(state: Mapping[str, Any], question: Any, model: str) -> str:
    """Hash of everything that determines the answer, apart from Jev's own sampling."""
    blob = json.dumps({"state": state, "question": _question_payload(question), "model": model},
                      sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _from_answer(question_id: str, answer: Any, model: Optional[str]) -> Judgement:
    kind = getattr(answer, "type", None)
    if kind == "choice":
        probs = dict(answer.probabilities or {})
        return Judgement(question_id, "choice", answer.choice, probs.get(answer.choice),
                         answer.confidence, probs, model)
    if kind == "noul":
        return Judgement(question_id, "noul", "yes" if answer.noul >= 0.5 else "no",
                         float(answer.noul), None, {"yes": float(answer.noul), "no": 1 - float(answer.noul)},
                         model)
    if kind == "score":
        probs = {str(k): v for k, v in (answer.probabilities or {}).items()}
        return Judgement(question_id, "score", None, float(answer.score), answer.confidence, probs, model)
    raise ValueError(f"Unsupported answer type {kind!r} for {question_id}")


class JevJudge:
    """Ask questions over one input; store and reuse every answer.

    Args:
        conn: Database the judgements are stored in.
        client: A ``typesafe_sdk.TypeSafeClient`` (or test double); created on
            first use when omitted.
    """

    def __init__(self, conn: sqlite3.Connection, client: Any = None):
        self.conn = conn
        self._client = client
        self.calls = 0
        ensure_schema(conn)

    def _get_client(self):
        if self._client is None:
            from typesafe_sdk import TypeSafeClient
            self._client = TypeSafeClient()
        return self._client

    def close(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            self._client.close()

    def _stored(self, question_id: str, digest: str) -> Optional[Judgement]:
        row = self.conn.execute(
            """SELECT primitive, answer, value, confidence, probabilities_json, model
               FROM ModelJudgements WHERE question_id = ? AND input_hash = ?
               ORDER BY rowid DESC LIMIT 1""", (question_id, digest)).fetchone()
        if row is None:
            return None
        return Judgement(question_id, row[0], row[1], row[2], row[3],
                         json.loads(row[4] or "{}"), row[5], cached=True)

    def ask(
        self,
        subject_type: str,
        subject_id: str,
        question_version: str,
        state: Mapping[str, Any],
        questions: Mapping[str, Any],
        refresh: bool = False,
    ) -> Dict[str, Judgement]:
        """Answer ``questions`` about ``state``, asking Jev only what is not stored.

        All unanswered questions go in one request, since Jev answers
        independent questions over the same state in parallel.
        """
        model = typesafe_model()
        digests = {qid: input_hash(state, q, model) for qid, q in questions.items()}
        results: Dict[str, Judgement] = {}
        if not refresh:
            for qid, digest in digests.items():
                stored = self._stored(qid, digest)
                if stored is not None:
                    results[qid] = stored
        pending = {qid: q for qid, q in questions.items() if qid not in results}
        if not pending:
            return results

        response = self._get_client().system_one(state=dict(state), questions=pending, model=model)
        self.calls += 1
        usage = getattr(response, "usage", None)
        now = utc_now_iso()
        rows = []
        for qid, question in pending.items():
            judgement = _from_answer(qid, response.answers[qid], response.model)
            results[qid] = judgement
            rows.append((
                str(uuid.uuid4()), subject_type, subject_id, qid, question_version, judgement.primitive,
                judgement.answer, judgement.value, judgement.confidence,
                json.dumps(judgement.probabilities), json.dumps(_question_payload(question), default=str),
                digests[qid], model, response.model,
                getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None), now,
            ))
        self.conn.executemany(
            "INSERT INTO ModelJudgements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self.conn.commit()
        logger.info("Jev %s on %s %s: %s", response.model, subject_type, subject_id,
                    ", ".join(f"{qid}={j.answer or round(j.value or 0, 3)} ({(j.value or 0):.2f})"
                              for qid, j in results.items() if not j.cached))
        return results
