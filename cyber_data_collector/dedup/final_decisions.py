"""The final score a judging step acts on, stored against the evidence both models were shown.

Several steps ask Jev, and re-ask the reasoning model for the middle band
(:mod:`utils.escalation`): event reality, event scope, member coherence (both
variants), victim attribution and the duplicate checks. Each answer is stored in
``ModelJudgements`` under a hash of its own input, which is right for reuse. What
was wrong was how the *final* score was read back: the newest Jev answer and the
newest escalation for a subject were picked independently, with no check that
they were given the same evidence. After an event's evidence changed, an old
escalation could be combined with a fresh Jev answer, and an old low score could
stand as if it still described the subject.

``FinalDecisions`` holds the score the step acts on with the hash of the state
(the exact evidence) that Jev and the escalation were both shown, the models and
the question versions. :func:`current` reads a decision only when that hash is
the hash of the subject's evidence now; :func:`latest` returns the newest
decision whatever its evidence, which says what the subject was once judged to be
and nothing about it today. A step that must act on a stale score asks again on
the current evidence - and only where it could change what the step does (see the
duplicate check's ``calibrated_negatives``); a report that merely shows a score
says it is out of date.

The state is built **once** per subject and run: the object Jev is asked on is
the one the escalation is given and the one :func:`record` hashes
(:func:`frozen`), so nothing that changes in between - a timeline refreshed, a
record re-enriched, another process - can make the three disagree. A step that
acts (rejects, splits, merges) builds the state again immediately before acting
and leaves any subject whose evidence has changed since it was judged for the
next run (:func:`changed_since`).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from cyber_data_collector.utils.llm_models import openai_model, typesafe_model

SCHEMA = """
CREATE TABLE IF NOT EXISTS FinalDecisions (
    step                  TEXT NOT NULL,   -- event-reality / event-scope / coherence / ... / duplicates-organisation / duplicates-ai
    subject_id            TEXT NOT NULL,   -- the event, member (event|record), event-victim or event-pair id
    evidence_hash         TEXT NOT NULL,   -- sha256 of the state exactly as Jev and the escalation were shown it
    question_version      TEXT NOT NULL,   -- the Jev question version + the escalation question version
    jev_model             TEXT NOT NULL,
    escalation_model      TEXT NOT NULL,
    final_score           REAL NOT NULL,   -- the probability the step acts on
    basis                 TEXT NOT NULL,   -- jev: Jev's answer; escalated: the reasoning model's, Jev's being mid-band
    jev_input_hash        TEXT NOT NULL,   -- the ModelJudgements rows this rests on (question_id + input_hash)
    escalation_input_hash TEXT,
    decided_at            TEXT NOT NULL,
    PRIMARY KEY (step, subject_id, evidence_hash, question_version, jev_model, escalation_model)
);
CREATE INDEX IF NOT EXISTS idx_finaldecisions_subject ON FinalDecisions (step, subject_id);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'FinalDecisions'").fetchone() is None:
        conn.executescript(SCHEMA)


def _missing_table(exc: sqlite3.OperationalError) -> bool:
    return "no such table" in str(exc)


def state_hash(state: Mapping[str, Any]) -> str:
    """A fingerprint of the evidence a judgement was shown (the state sent to Jev and to the escalation)."""
    blob = json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Step:
    """A judging step: its Jev question, the escalation of its middle band (``band`` None: never escalated),
    and the question text each is asked with. ``name`` keys the stored decisions."""

    name: str
    question_id: str
    version: str
    make_question: Callable[[], Any]
    escalation_id: str
    escalation_version: str
    escalation_text: Callable[[], str]
    band: Optional[Tuple[float, float]]

    @property
    def question_version(self) -> str:
        return f"{self.version}+{self.escalation_version}"

    def models(self) -> Tuple[str, str]:
        return typesafe_model(), openai_model()

    def escalates(self, p: float) -> bool:
        return bool(self.band) and self.band[0] <= p < self.band[1]


def record(conn: sqlite3.Connection, step: Step, subject_id: str, state: Mapping[str, Any], final: float,
           escalated: bool) -> None:
    """Store ``final`` as the step's decision for the subject on this state. Does not commit."""
    from cyber_data_collector.utils import escalation as esc
    from cyber_data_collector.utils import jev as jev_module

    ensure_schema(conn)
    jev_model, escalation_model = step.models()
    conn.execute(
        "INSERT OR REPLACE INTO FinalDecisions (step, subject_id, evidence_hash, question_version, jev_model, "
        "escalation_model, final_score, basis, jev_input_hash, escalation_input_hash, decided_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (step.name, subject_id, state_hash(state), step.question_version, jev_model, escalation_model, float(final),
         "escalated" if escalated else "jev", jev_module.input_hash(state, step.make_question(), jev_model),
         esc.input_hash(state, step.escalation_text(), escalation_model) if escalated else None,
         datetime.now().isoformat(timespec="seconds")))


def current(conn: sqlite3.Connection, step: Step, subject_id: str, state: Mapping[str, Any]) -> Optional[float]:
    """The decision made on exactly this state under the current models and question versions, else None."""
    jev_model, escalation_model = step.models()
    try:        # a database that has never recorded a decision (or is read-only) simply has none
        row = conn.execute(
            "SELECT final_score FROM FinalDecisions WHERE step = ? AND subject_id = ? AND evidence_hash = ? "
            "AND question_version = ? AND jev_model = ? AND escalation_model = ?",
            (step.name, subject_id, state_hash(state), step.question_version, jev_model,
             escalation_model)).fetchone()
    except sqlite3.OperationalError as exc:
        if _missing_table(exc):
            return None
        raise
    return float(row[0]) if row is not None else None


def read_current(conn: sqlite3.Connection, step: Step,
                 subjects: Iterable[Tuple[str, Callable[[], Mapping[str, Any]]]]) -> Dict[str, float]:
    """Final score per subject from its decision on its **current** evidence; nothing for a subject with none.

    ``subjects`` are ``(subject_id, build)`` where ``build()`` returns the state the subject would be judged on now.
    """
    out: Dict[str, float] = {}
    for sid, build in subjects:
        score = current(conn, step, sid, build())
        if score is not None:
            out[sid] = score
    return out


def latest(conn: sqlite3.Connection, step: Step, subject_ids: Optional[Iterable[str]] = None
           ) -> Dict[str, Tuple[float, str]]:
    """``{subject_id: (final score, evidence hash)}``: the newest decision per subject under the current models and
    question versions, whatever evidence it was made on. Not a verdict on the subject as it is now."""
    jev_model, escalation_model = step.models()
    wanted = None if subject_ids is None else set(subject_ids)
    out: Dict[str, Tuple[float, str]] = {}
    try:
        rows = conn.execute(
            "SELECT subject_id, final_score, evidence_hash FROM FinalDecisions WHERE step = ? AND question_version = ? "
            "AND jev_model = ? AND escalation_model = ? ORDER BY decided_at, rowid",
            (step.name, step.question_version, jev_model, escalation_model)).fetchall()
    except sqlite3.OperationalError as exc:
        if _missing_table(exc):
            return out
        raise
    for sid, score, evidence in rows:
        if wanted is None or sid in wanted:
            out[sid] = (float(score), evidence)
    return out


def frozen(states: Mapping[str, Mapping[str, Any]], subject_id: str) -> Mapping[str, Any]:
    """The state ``subject_id`` was judged on, from the ``states`` a step built once for its Jev question.

    A step that shows the escalation and records the decision on the state Jev saw must never build a new one
    in between (a timeline refreshed or a record re-enriched in the meantime would make the three disagree), so a
    subject missing from ``states`` is a caller's error, not a reason to read the database again.
    """
    try:
        return states[subject_id]
    except KeyError:
        raise ValueError(f"{subject_id!r} has no frozen state: it was not asked on these states") from None


def changed_since(states: Mapping[str, Mapping[str, Any]], subject_ids: Iterable[str],
                  build: Callable[[str], Mapping[str, Any]]) -> List[str]:
    """The subjects whose evidence now differs from the state they were judged on (``states``).

    Called immediately before a step acts: each subject's state is built again and its :func:`state_hash`
    compared with the frozen one. A subject whose evidence changed since it was judged (another process,
    a refreshed timeline, a re-enriched record) has a decision nobody made on its evidence now; the step leaves
    it for the next run instead of acting on it.
    """
    return [sid for sid in subject_ids if state_hash(build(sid)) != state_hash(frozen(states, sid))]


def record_scores(conn: sqlite3.Connection, step: Step, subjects: Sequence[Tuple[str, Callable[[], Mapping[str, Any]]]],
                  jev: Mapping[str, float], escalated: Mapping[str, float],
                  failed: Iterable[str] = ()) -> Dict[str, float]:
    """Store the final decision of every subject Jev scored, and return the final scores.

    The final score is the escalation's where the subject was escalated, else Jev's; a subject whose escalation
    failed (``failed``) has none and is left undecided. States are built by ``build()`` for the subjects scored
    only. Commits.
    """
    failed_set = set(failed)
    final: Dict[str, float] = {}
    for sid, build in subjects:
        if sid not in jev or sid in failed_set:
            continue
        value = escalated.get(sid, jev[sid])
        record(conn, step, sid, build(), value, sid in escalated)
        final[sid] = value
    conn.commit()
    return final
