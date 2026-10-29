"""Is a proposed entity fold really one organisation? Perplexity decides; every verdict is kept.

:func:`entity_merge.canonical_groups` proposes folds from name structure -
containment, acronyms, spelling. Structure cannot tell an abbreviation from a
coincidence: on 2026-10-28 it would have folded Marks & Spencer, Media Super
and Moody's into Mackay Sugar ("MS"), IBM into Australian Cyber Security
Magazine, and a list of the four major banks into Westpac. Knowing that "NAB"
is National Australia Bank and "M&S" is not Mackay Sugar takes knowledge of
the organisations, so each proposed pair is put to Perplexity (search-grounded,
with citations) together with an incident each name appeared in, and only a
``same`` verdict - one legal entity, or its trading name, abbreviation, acronym
or former name - folds.

Every verdict is stored in ``EntityAliasDecisions`` (with its reason,
citations, model and prompt version) and never asked again while the prompt is
unchanged: an accepted pair becomes a known alias (``EntityAliases``, source
``llm``) that future ingests resolve; a rejected pair is never proposed again.
A decision is tied to the evidence it was made on (``evidence_hash``: the incident each name
appeared in and any stored profile of either organisation). When that evidence changes the model's
decision is **asked again, but only if the pair matters now**: it is proposed for folding, or it
vetoes a proposed fold. A decision no proposal touches is left as it is, however old; one a person
made (``model = 'human'``) is never asked again and outranks every model's. A decision with no
recorded evidence (made before the hash was kept) cannot be shown unchanged, so it is treated as
changed the first time it matters. Each decision replaced this way is kept in
``EntityAliasDecisionHistory``. A failed or unreadable answer leaves the pair undecided (retried
next run), never folded. Without ``PERPLEXITY_API_KEY`` nothing is verified and the
command fails - structural folds are not applied unverified.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from cyber_data_collector.utils.llm_models import perplexity_model

logger = logging.getLogger(__name__)

PROMPT_VERSION = "entity-alias-v1"
VERDICTS = ("same", "parent_subsidiary", "related", "different", "unsure")

SCHEMA = """
CREATE TABLE IF NOT EXISTS EntityAliasDecisions (
    name           TEXT NOT NULL,
    other_name     TEXT NOT NULL,
    verdict        TEXT NOT NULL,      -- same / parent_subsidiary / related / different / unsure
    canonical_name TEXT,               -- the organisation's usual name, when 'same'
    reason         TEXT,
    citations_json TEXT,
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    decided_at     TEXT NOT NULL,
    evidence_hash  TEXT,               -- the incidents (and profiles) the question showed; see pair_evidence
    PRIMARY KEY (name, other_name, prompt_version)
);
CREATE TABLE IF NOT EXISTS EntityAliasDecisionHistory (
    name           TEXT NOT NULL,
    other_name     TEXT NOT NULL,
    verdict        TEXT NOT NULL,
    canonical_name TEXT,
    reason         TEXT,
    citations_json TEXT,
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    decided_at     TEXT NOT NULL,
    evidence_hash  TEXT,
    superseded_at  TEXT NOT NULL,      -- when a newer decision on changed evidence replaced it
    superseded_by  TEXT                -- the new verdict
);
"""

PROMPT = """Are these two names used for the same organisation?

A: "{a}"{a_context}
B: "{b}"{b_context}

Both were recorded as organisations in reporting of Australian cyber-security incidents.

Answer with a JSON object only:
{{"verdict": "same" | "parent_subsidiary" | "related" | "different" | "unsure",
  "canonical_name": "<the organisation's usual full name if same, else null>",
  "reason": "<one sentence>"}}

- "same": one legal entity - including its trading name, brand used as its name, abbreviation, acronym
  or former name (for example "NAB" and "National Australia Bank").
- "parent_subsidiary": a parent and a subsidiary, division or brand that is a separate entity.
- "related": otherwise connected (same group, a programme an agency runs, a partner), but not one entity.
- "different": distinct organisations, including ones that share a word or an acronym.
- "unsure": the evidence does not settle it."""

Search = Callable[[str], Optional[Dict[str, Any]]]


class AliasVerificationUnavailable(RuntimeError):
    """Perplexity is not configured; structural folds cannot be verified."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    if "evidence_hash" not in {r[1] for r in conn.execute("PRAGMA table_info(EntityAliasDecisions)")}:
        conn.execute("ALTER TABLE EntityAliasDecisions ADD COLUMN evidence_hash TEXT")


def _pair(a: str, b: str) -> Tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def stored_verdicts(conn: sqlite3.Connection) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """The reusable decisions: this prompt version and this Perplexity model, or a person's.

    A decision made by another model is not reused (it is asked again), so changing ``PERPLEXITY_MODEL``
    cannot leave old verdicts standing as if the new model had given them. A ``human`` decision (an
    ``unmerge-entity``) is always reused and outranks a model's for the same pair. Each entry carries the
    ``model`` and the ``evidence_hash`` it was made on; :func:`verify_pair` re-asks a model's decision when
    that evidence changed and the pair matters (see :func:`is_current`).
    """
    ensure_schema(conn)
    rows = conn.execute("SELECT name, other_name, verdict, canonical_name, reason, evidence_hash, model "
                        "FROM EntityAliasDecisions WHERE prompt_version = ? AND model IN (?, 'human') "
                        "ORDER BY model = 'human'",
                        (PROMPT_VERSION, perplexity_model())).fetchall()
    return {(r[0], r[1]): {"verdict": r[2], "canonical_name": r[3], "reason": r[4], "evidence_hash": r[5],
                           "model": r[6]}
            for r in rows}


def is_current(stored: Dict[str, Any], evidence: str) -> bool:
    """Whether a stored decision still stands: a person's always does, a model's only on the evidence it was made on.

    A model's decision with no recorded evidence (made before the hash was kept) is not shown to stand.
    """
    return stored.get("model") == "human" or stored.get("evidence_hash") == evidence


def incident_context(conn: sqlite3.Connection, name: str) -> str:
    """An incident the name appeared in, to tell same-acronym organisations apart."""
    row = conn.execute(
        """SELECT e.title FROM EntitiesV2 v
           JOIN EnrichedEventEntities ee ON ee.entity_id = v.entity_id
           JOIN EnrichedEvents e ON e.enriched_event_id = ee.enriched_event_id
           WHERE v.entity_name = ? AND e.title IS NOT NULL ORDER BY e.enriched_event_id LIMIT 1""",
        (name,)).fetchone()
    return f'\n   (named in: "{row[0][:160]}")' if row else ""


def _context(conn: sqlite3.Connection, name: str) -> str:
    """What the question tells about one name: an incident it appeared in, and its stored profile if any."""
    from cyber_data_collector.dedup.entity_profiles import profile_text

    profile = profile_text(conn, name)
    return incident_context(conn, name) + (f"\n   (profile: {profile})" if profile else "")


def pair_evidence(conn: sqlite3.Connection, a: str, b: str) -> str:
    """A hash of what a question about the pair shows: the incident each name appeared in, and any stored
    profile of either organisation (:mod:`entity_profiles`). It changes when the evidence does.
    """
    import hashlib

    from cyber_data_collector.dedup.entity_profiles import profile_text

    blob = json.dumps({"a": incident_context(conn, a), "b": incident_context(conn, b),
                       "profile_a": profile_text(conn, a), "profile_b": profile_text(conn, b)}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def perplexity_search(prompt: str) -> Optional[Dict[str, Any]]:
    """Perplexity's answer and citations, or None when the call failed."""
    import requests

    key = os.getenv("PERPLEXITY_API_KEY")
    if not key:
        raise AliasVerificationUnavailable("PERPLEXITY_API_KEY is not set")
    try:
        response = requests.post(
            "https://api.perplexity.ai/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": perplexity_model(), "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 300, "temperature": 0.0},
            timeout=90)
        if response.status_code != 200:
            logger.warning("Perplexity returned %s for an alias question", response.status_code)
            return None
        payload = response.json()
        return {"text": payload["choices"][0]["message"]["content"], "citations": payload.get("citations") or []}
    except Exception as exc:  # noqa: BLE001 - one pair must not stop the run; it stays undecided
        logger.warning("Perplexity alias question failed: %s", exc)
        return None


def parse_verdict(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """The JSON verdict in an answer, or None when there is no valid one."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("verdict") not in VERDICTS:
        return None
    return {"verdict": data["verdict"], "canonical_name": data.get("canonical_name") or None,
            "reason": str(data.get("reason") or "")[:500]}


@dataclass
class VerificationReport:
    asked: int = 0
    reused: int = 0
    revisited: List[Tuple[str, str]] = field(default_factory=list)   # stored decisions asked again: evidence changed
    failed: List[Tuple[str, str]] = field(default_factory=list)
    accepted: List[Tuple[str, str]] = field(default_factory=list)
    rejected: List[Tuple[str, str, str]] = field(default_factory=list)


def verify_pair(conn: sqlite3.Connection, a: str, b: str, search: Search,
                known: Dict[Tuple[str, str], Dict[str, Any]], report: VerificationReport) -> Optional[str]:
    """The verdict for one pair (stored, else asked and stored); None when undecided.

    Callers ask about a pair only when it matters now (proposed for a fold, or vetoing one). A stored decision
    stands while the evidence it was made on is the evidence now (a person's always stands); otherwise the pair
    is asked again and the new decision replaces it, the old one being kept in ``EntityAliasDecisionHistory``.
    """
    key = _pair(a, b)
    evidence = pair_evidence(conn, key[0], key[1])
    if key in known:
        if is_current(known[key], evidence):
            report.reused += 1
            return known[key]["verdict"]
        report.revisited.append(key)
    prompt = PROMPT.format(a=key[0], b=key[1], a_context=_context(conn, key[0]), b_context=_context(conn, key[1]))
    report.asked += 1
    answer = search(prompt)
    verdict = parse_verdict(answer["text"]) if answer else None
    if verdict is None:
        report.failed.append(key)
        return None
    # Whatever model's decision this replaces (the current model's, or an older model's) is kept.
    conn.execute(
        "INSERT INTO EntityAliasDecisionHistory SELECT name, other_name, verdict, canonical_name, reason, "
        "citations_json, model, prompt_version, decided_at, evidence_hash, ?, ? FROM EntityAliasDecisions "
        "WHERE name = ? AND other_name = ? AND prompt_version = ? AND model != 'human'",
        (datetime.now().isoformat(timespec="seconds"), verdict["verdict"], key[0], key[1], PROMPT_VERSION))
    if key in known:
        logger.info("Alias decision for %r / %r re-asked on changed evidence: %s -> %s", key[0], key[1],
                    known[key]["verdict"], verdict["verdict"])
    conn.execute("INSERT OR REPLACE INTO EntityAliasDecisions (name, other_name, verdict, canonical_name, reason, "
                 "citations_json, model, prompt_version, decided_at, evidence_hash) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (key[0], key[1], verdict["verdict"], verdict["canonical_name"], verdict["reason"],
                  json.dumps(answer.get("citations") or []), perplexity_model(), PROMPT_VERSION,
                  datetime.now().isoformat(timespec="seconds"), evidence))
    conn.commit()
    known[key] = {**verdict, "evidence_hash": evidence, "model": perplexity_model()}
    return verdict["verdict"]


@dataclass
class StoredAliasReport:
    checked: int = 0
    spelling_only: int = 0                       # same name up to case/punctuation/legal suffix: kept
    confirmed: List[Tuple[str, str]] = field(default_factory=list)
    removed: List[Tuple[str, str, str]] = field(default_factory=list)


def verify_stored_aliases(conn: sqlite3.Connection, search: Optional[Search] = None,
                          report: Optional[VerificationReport] = None) -> StoredAliasReport:
    """Put every ``rule``-made alias (name structure alone) to Perplexity once.

    Confirmed aliases become ``llm`` aliases; rejected ones are deleted, so
    they stop filing new records under the wrong organisation, and are listed
    - the fold they made earlier is not undone here (the links' original
    names are gone) and needs review. A pair that differs only in case,
    punctuation or legal suffix is kept without asking. Commits.
    """
    from cyber_data_collector.dedup.entity_resolution import canonical_key

    search = search or perplexity_search
    report = report if report is not None else VerificationReport()
    out = StoredAliasReport()
    known = stored_verdicts(conn)
    rows = conn.execute("SELECT raw_name, canonical_name FROM EntityAliases WHERE source = 'rule' "
                        "ORDER BY raw_name").fetchall()
    for raw, canonical in rows:
        out.checked += 1
        if canonical_key(raw) == canonical_key(canonical):
            out.spelling_only += 1
            continue
        verdict = verify_pair(conn, raw, canonical, search, known, report)
        if verdict == "same":
            conn.execute("UPDATE EntityAliases SET source = 'llm', confidence = 1.0 WHERE raw_name = ?", (raw,))
            out.confirmed.append((raw, canonical))
        elif verdict is not None:
            conn.execute("DELETE FROM EntityAliases WHERE raw_name = ? AND source = 'rule'", (raw,))
            out.removed.append((raw, canonical, verdict))
            logger.warning("Removed alias %r -> %r: Perplexity says %s", raw, canonical, verdict)
        conn.commit()
    return out


#: Stored verdicts that say two names are not one entity. ``unsure`` settles nothing either way.
CONFLICT_VERDICTS = ("different", "parent_subsidiary", "related")


def _recorded_conflict(known: Dict[Tuple[str, str], Dict[str, Any]], name: str,
                       members: Sequence[str]) -> Optional[Tuple[str, str]]:
    """``(member, verdict)`` for the first stored verdict that ``name`` is not the same entity as a member."""
    for member in members:
        stored = known.get(_pair(name, member))
        if stored and stored["verdict"] in CONFLICT_VERDICTS:
            return member, stored["verdict"]
    return None


def _revisit_stale_vetoes(conn: sqlite3.Connection, known: Dict[Tuple[str, str], Dict[str, Any]], name: str,
                         members: Sequence[str], search: Search, report: VerificationReport) -> None:
    """Ask again each stored conflict verdict between ``name`` and a member that was made on other evidence.

    ``name`` is proposed to join the fold, so a stored "different" / "related" / "parent_subsidiary" that would
    bar it matters now - and a veto the evidence has outgrown must not stand. A person's verdict is not asked
    again. If the new question fails the old verdict keeps barring the name (the fold is the risky direction)
    and the pair is in ``report.failed``.
    """
    for member in members:
        key = _pair(name, member)
        stored = known.get(key)
        if stored is None or stored["verdict"] not in CONFLICT_VERDICTS:
            continue
        if not is_current(stored, pair_evidence(conn, key[0], key[1])):
            verify_pair(conn, key[0], key[1], search, known, report)


def verified_groups(conn: sqlite3.Connection, groups: Sequence[Tuple[str, List[str]]],
                    search: Optional[Search] = None,
                    report: Optional[VerificationReport] = None) -> List[Tuple[str, List[str]]]:
    """Keep only the folds Perplexity confirms and no recorded verdict contradicts.

    Each name is checked against the group's chosen name (the head), and
    **every stored verdict between any two members is honoured**: a name joins
    the fold only if none of ``different`` / ``parent_subsidiary`` /
    ``related`` is recorded between it and the head or any name already
    joined. So with A=B and A=C confirmed but B and C recorded as different
    organisations, B and C never fold together - the one with fewer recorded
    conflicts (then the earlier) joins A, the other is left out (reported as
    rejected with the conflicting verdict) without asking. A question is put
    to Perplexity only for a pair with no stored verdict whose answer can
    change the fold: a name already barred by a recorded conflict is not asked
    about. Names that are not folded are regrouped among themselves with their
    own chosen name (so "M&S" can still join "Marks & Spencer" when "Mackay
    Sugar" rejects both).
    """
    from cyber_data_collector.dedup.entity_merge import preferred_name

    search = search or perplexity_search
    report = report if report is not None else VerificationReport()
    known = stored_verdicts(conn)
    out: List[Tuple[str, List[str]]] = []
    for keep, drop in groups:
        pending = [keep, *drop]
        while len(pending) >= 2:
            head = pending[0] if pending[0] == keep else preferred_name(pending, {})
            others = [n for n in pending if n != head]
            # Fewest recorded conflicts first, so a consistent fold is as large as it can be.
            order = sorted(others, key=lambda n: (sum(1 for o in pending if o != n and _recorded_conflict(known, n, [o])),
                                                  others.index(n)))
            joined: List[str] = []
            barred: Dict[str, Tuple[str, str]] = {}
            for name in order:
                _revisit_stale_vetoes(conn, known, name, [head, *joined], search, report)
                conflict = _recorded_conflict(known, name, [head, *joined])
                if conflict is not None:
                    barred[name] = conflict
                    report.reused += 1          # a stored verdict decided it; nothing asked
                    continue
                verdict = verify_pair(conn, head, name, search, known, report)
                if verdict == "same":
                    joined.append(name)
                    report.accepted.append((name, head))
                else:
                    barred[name] = (head, verdict or "")
            for name in order:
                if name in barred and barred[name][1]:        # undecided pairs are retried, not rejected
                    report.rejected.append((name, *barred[name]))
            if joined:
                out.append((head, [n for n in others if n in joined]))
            pending, keep = [n for n in others if n in barred], None
    return out
