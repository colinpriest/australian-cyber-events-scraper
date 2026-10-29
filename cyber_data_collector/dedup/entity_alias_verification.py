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
A failed or unreadable answer leaves the pair undecided (retried next run),
never folded. Without ``PERPLEXITY_API_KEY`` nothing is verified and the
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
    """The reusable decisions: this prompt version and this Perplexity model.

    A decision made by another model is not reused (it is asked again), so changing ``PERPLEXITY_MODEL``
    cannot leave old verdicts standing as if the new model had given them. Each entry carries the
    ``evidence_hash`` it was made on; :func:`verify_pair` re-asks an ``unsure`` one when that changed.
    """
    ensure_schema(conn)
    rows = conn.execute("SELECT name, other_name, verdict, canonical_name, reason, evidence_hash "
                        "FROM EntityAliasDecisions WHERE prompt_version = ? AND model = ?",
                        (PROMPT_VERSION, perplexity_model())).fetchall()
    return {(r[0], r[1]): {"verdict": r[2], "canonical_name": r[3], "reason": r[4], "evidence_hash": r[5]}
            for r in rows}


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
    failed: List[Tuple[str, str]] = field(default_factory=list)
    accepted: List[Tuple[str, str]] = field(default_factory=list)
    rejected: List[Tuple[str, str, str]] = field(default_factory=list)


def verify_pair(conn: sqlite3.Connection, a: str, b: str, search: Search,
                known: Dict[Tuple[str, str], Dict[str, Any]], report: VerificationReport) -> Optional[str]:
    """The verdict for one pair (stored, else asked and stored); None when undecided."""
    key = _pair(a, b)
    evidence = pair_evidence(conn, key[0], key[1])
    if key in known:
        # A settled verdict stands. An `unsure` one was the evidence's fault: it is asked again when the
        # evidence (the incidents or profiles shown) is not what it was asked on.
        if known[key]["verdict"] != "unsure" or known[key].get("evidence_hash") == evidence:
            report.reused += 1
            return known[key]["verdict"]
    prompt = PROMPT.format(a=key[0], b=key[1], a_context=_context(conn, key[0]), b_context=_context(conn, key[1]))
    report.asked += 1
    answer = search(prompt)
    verdict = parse_verdict(answer["text"]) if answer else None
    if verdict is None:
        report.failed.append(key)
        return None
    conn.execute("INSERT OR REPLACE INTO EntityAliasDecisions (name, other_name, verdict, canonical_name, reason, "
                 "citations_json, model, prompt_version, decided_at, evidence_hash) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (key[0], key[1], verdict["verdict"], verdict["canonical_name"], verdict["reason"],
                  json.dumps(answer.get("citations") or []), perplexity_model(), PROMPT_VERSION,
                  datetime.now().isoformat(timespec="seconds"), evidence))
    conn.commit()
    known[key] = {**verdict, "evidence_hash": evidence}
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


def verified_groups(conn: sqlite3.Connection, groups: Sequence[Tuple[str, List[str]]],
                    search: Optional[Search] = None,
                    report: Optional[VerificationReport] = None) -> List[Tuple[str, List[str]]]:
    """Keep only the folds Perplexity confirms; names it rejects are regrouped among themselves.

    Each name is checked against the group's chosen name; names that are not
    the same organisation form a residual group with its own chosen name (so
    "M&S" can still join "Marks & Spencer" when "Mackay Sugar" rejects both).
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
            joined, left = [], []
            for name in pending:
                if name == head:
                    continue
                verdict = verify_pair(conn, head, name, search, known, report)
                if verdict == "same":
                    joined.append(name)
                    report.accepted.append((name, head))
                else:
                    left.append(name)
                    if verdict is not None:
                        report.rejected.append((name, head, verdict))
            if joined:
                out.append((head, joined))
            pending, keep = left, None
    return out
