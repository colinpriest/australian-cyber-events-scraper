"""Two victims of one event that may be one organisation: decided for that event only.

A review of the entity dashboard (2026-11-05) found one organisation listed twice on one event under two names -
"UOW" and "University of Wollongong", "Sydney University" and "University of Sydney", "Service Australia" and
"Services Australia", "VRC" and "Victoria Racing Club Limited" - and descriptions listed beside the organisation
they describe ("Perth uni" beside the University of Notre Dame Australia, "Australia's second-largest telecom
provider" beside Optus, "South Australian council" beside Wattle Range Council). Each counted as two victim
organisations. Nothing had compared them: alias learning proposes names that look alike across the whole entity
table, and names two *records* of one event give as the victim (:func:`alias_learning.coreference_proposals`), but
never two victim roles of one event; of 237 such pairs on the live database, 41 had ever been asked about.

Here every pair of victims of one active event is put to Perplexity with that incident's reporting - its title,
its records' titles, the sentences that name each name (:func:`entity_classification.entity_mentions`), any stored
profile and what a person recorded about either name. Proposed: every pair on an event with at most
:data:`MAX_VICTIMS` victim organisations, and on a larger event (a supplier's customers) only pairs that share a
distinctive word, where one abbreviates the other, or that have the same words in another order. Pairs victim counting already treats as one organisation are
not asked.

**The verdict holds for that event only.** ``same`` joins the two names on that event - victim counting
(:func:`victim_counting.within_event_duplicates`) then counts the organisation once, and the entity dashboard lists
the event once - and folds nothing globally: "Perth uni" is the University of Notre Dame in one report and not a
name of it anywhere else, and acronyms are contextual already (:mod:`contextual_aliases`). Every other verdict
(``parent_subsidiary``, ``related``, ``different``, ``unsure``) keeps both. A person's alias or alias decision about
the pair decides without a question.

Verdicts are stored in ``EventVictimCoreference`` with a hash of the question as sent (prompt and evidence), and a
verdict counts only while the event's question is unchanged: a merge, a new record or a changed page asks again.
Like the alias verification it builds on, the verdict is not calibrated on labels; it changes only which victim
links count as one organisation, never an event's membership, and every row records the reason and citations.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from cyber_data_collector.dedup import entity_alias_verification as eav
from cyber_data_collector.utils.llm_models import perplexity_model
from cyber_data_collector.utils.timestamps import utc_now_iso

logger = logging.getLogger(__name__)

PROMPT_VERSION = "covictim-coreference-v1"
#: Every pair of victims is asked on an event with at most this many victim organisations; on a larger one only
#: pairs whose names share a distinctive word or abbreviate one another.
MAX_VICTIMS = 4
MAX_RECORD_TITLES = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS EventVictimCoreference (
    deduplicated_event_id TEXT NOT NULL,
    name                  TEXT NOT NULL,
    other_name            TEXT NOT NULL,
    verdict               TEXT NOT NULL,   -- same / parent_subsidiary / related / different / unsure
    canonical_name        TEXT,
    reason                TEXT,
    citations_json        TEXT,
    model                 TEXT NOT NULL,   -- the Perplexity model, or 'human' (a person's alias or decision)
    prompt_version        TEXT NOT NULL,
    evidence_hash         TEXT NOT NULL,   -- hash of the question as sent
    decided_at            TEXT NOT NULL,
    PRIMARY KEY (deduplicated_event_id, name, other_name, prompt_version)
);
"""

PROMPT = """In the reporting of one cyber-security incident, two names were recorded as victims. In this incident, do
they refer to the same organisation?

Incident: "{title}"
Reports in this incident:
{records}

A: "{a}"
{a_context}
B: "{b}"
{b_context}

Answer with a JSON object only:
{{"verdict": "same" | "parent_subsidiary" | "related" | "different" | "unsure",
  "canonical_name": "<the organisation's usual full name if same, else null>",
  "reason": "<one sentence>"}}

- "same": in this incident the two names refer to one organisation: its trading name, abbreviation, acronym, former
  name or a misspelling of it, or a description the reporting uses for it (for example "a Perth university" for the
  university the report names, or "Australia's second-largest telco" for the telco it names).
- "parent_subsidiary": one is a parent, subsidiary, division, network, brand or unit of the other that is a
  separate entity (a TV channel and its listed owner, a local health network and its state health system).
- "related": otherwise connected, but not one organisation in this incident.
- "different": two organisations, each affected in its own right.
- "unsure": the reporting does not settle it.
The answer is about this incident only."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _victims(conn: sqlite3.Connection) -> List[Tuple[int, str, str]]:
    """``(entity id, entity name, event id)`` for every victim link of an active event."""
    return [(int(r[0]), r[1], r[2]) for r in conn.execute(
        """SELECT d.entity_id, v.entity_name, d.deduplicated_event_id
           FROM DeduplicatedEventEntities d JOIN EntitiesV2 v ON v.entity_id = d.entity_id
           JOIN DeduplicatedEvents e ON e.deduplicated_event_id = d.deduplicated_event_id
           WHERE e.status = 'Active' AND d.relationship_type = 'victim' AND v.entity_name IS NOT NULL""")]


def proposals(conn: sqlite3.Connection) -> List[Tuple[str, str, str]]:
    """``(event id, a, b)`` with ``a < b``: victim pairs of one active event that may be one organisation."""
    from cyber_data_collector.dedup.alias_learning import abbreviation_fits
    from cyber_data_collector.dedup.entity_merge import distinctive_core
    from cyber_data_collector.dedup.victim_counting import within_event_duplicates

    rows = _victims(conn)
    already = within_event_duplicates(conn, rows, use_coreference=False)
    names_of: Dict[str, Set[str]] = {}
    for eid, name, did in rows:
        if (eid, did) not in already:
            names_of.setdefault(did, set()).add(name)
    out: List[Tuple[str, str, str]] = []
    for did, names in sorted(names_of.items()):
        ordered = sorted(names)
        for a, b in itertools.combinations(ordered, 2):
            if a.lower() == b.lower():
                continue
            if len(ordered) <= MAX_VICTIMS or (distinctive_core(a) & distinctive_core(b)
                                              or abbreviation_fits(a, b) or abbreviation_fits(b, a)
                                              or _same_words(a, b)):
                out.append((did, a, b))
    return out


def _same_words(a: str, b: str) -> bool:
    """The same words in another order or inflection ("Sydney University" / "University of Sydney", "Service
    Australia" / "Services Australia"): their word stems, without "of", "the" and "and", are equal."""
    from cyber_data_collector.dedup.entity_merge import _tokens

    def stems(name: str) -> Set[str]:
        return {t[:-1] if len(t) > 3 and t.endswith("s") else t for t in _tokens(name)} - {"of", "the", "and"}

    sa, sb = stems(a), stems(b)
    return bool(sa) and sa == sb


def _context(conn: sqlite3.Connection, name: str, sentences: Sequence[str]) -> str:
    from cyber_data_collector.dedup.entity_profiles import profile_text

    lines = [f'   - "{s}"' for s in sentences] or ["   (named in no sentence of the stored reporting)"]
    profile = profile_text(conn, name)
    if profile:
        lines.append(f"   (profile: {profile})")
    lines += [f"   (recorded by a person: {line})" for line in eav.person_decisions(conn, name)]
    return "\n".join(lines)


def question(conn: sqlite3.Connection, event_id: str, a: str, b: str) -> str:
    """The prompt for one pair of one event, as it is sent (and hashed)."""
    from cyber_data_collector.dedup.entity_classification import entity_mentions
    from cyber_data_collector.processing.active_evidence import member_condition

    row = conn.execute("SELECT title FROM DeduplicatedEvents WHERE deduplicated_event_id = ?", (event_id,)).fetchone()
    titles = [r[0] for r in conn.execute(
        f"""SELECT e.title FROM EventDeduplicationMap m JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
            WHERE m.deduplicated_event_id = ? {member_condition(conn)} AND e.title IS NOT NULL
            ORDER BY e.enriched_event_id""", (event_id,))]
    titles = list(dict.fromkeys(titles))[:MAX_RECORD_TITLES]
    mentions = entity_mentions(conn, event_id, [a, b])
    return PROMPT.format(title=(row[0] if row and row[0] else "")[:200],
                         records="\n".join(f"- {t[:200]}" for t in titles) or "- (no record titles)",
                         a=a, b=b, a_context=_context(conn, a, mentions.get(a, [])),
                         b_context=_context(conn, b, mentions.get(b, [])))


def _hash(prompt: str) -> str:
    return hashlib.sha256(f"{PROMPT_VERSION}\n{prompt}".encode("utf-8")).hexdigest()[:16]


def person_verdict(conn: sqlite3.Connection, a: str, b: str) -> Optional[str]:
    """What a person recorded about the pair: an alias joining them (``same``) or an alias decision; else None."""
    try:
        if conn.execute("SELECT 1 FROM EntityAliases WHERE source = 'human' AND ((raw_name = ? AND canonical_name = ?) "
                        "OR (raw_name = ? AND canonical_name = ?))", (a, b, b, a)).fetchone():
            return "same"
        row = conn.execute("SELECT verdict FROM EntityAliasDecisions WHERE model = 'human' AND ((name = ? AND "
                           "other_name = ?) OR (name = ? AND other_name = ?))", (a, b, b, a)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def _stored(conn: sqlite3.Connection) -> Dict[Tuple[str, str, str], Dict[str, str]]:
    ensure_schema(conn)
    return {(r[0], r[1], r[2]): {"verdict": r[3], "evidence_hash": r[4], "model": r[5]} for r in conn.execute(
        "SELECT deduplicated_event_id, name, other_name, verdict, evidence_hash, model FROM EventVictimCoreference "
        "WHERE prompt_version = ?", (PROMPT_VERSION,))}


@dataclass
class Report:
    proposed: int = 0
    asked: int = 0
    reused: int = 0
    by_person: int = 0
    failed: List[Tuple[str, str, str]] = field(default_factory=list)
    verdicts: Dict[str, int] = field(default_factory=dict)
    same: List[Tuple[str, str, str]] = field(default_factory=list)


def verify(conn: sqlite3.Connection, search: Optional[eav.Search] = None, limit: Optional[int] = None,
           dry_run: bool = False) -> Report:
    """Decide every :func:`proposals` pair: a person's record, else the stored verdict on an unchanged question,
    else Perplexity (stored). With ``dry_run`` nothing is asked or stored and ``asked`` counts the questions a run
    would send. A failed question is in ``failed`` and asked again next run."""
    search = search or eav.perplexity_search
    report = Report()
    stored = _stored(conn)
    todo = proposals(conn)
    report.proposed = len(todo)
    for did, a, b in todo:
        human = person_verdict(conn, a, b)
        if human:
            verdict, evidence, answer = human, "human", None
            report.by_person += 1
        else:
            prompt = question(conn, did, a, b)
            evidence = _hash(prompt)
            known = stored.get((did, a, b))
            if known and known["evidence_hash"] == evidence and known["model"] == perplexity_model():
                report.reused += 1
                verdict = known["verdict"]
                report.verdicts[verdict] = report.verdicts.get(verdict, 0) + 1
                if verdict == "same":
                    report.same.append((did, a, b))
                continue
            if limit is not None and report.asked >= limit:
                continue
            report.asked += 1
            if dry_run:
                continue
            answer = search(prompt)
            parsed = eav.parse_verdict(answer["text"]) if answer else None
            if parsed is None:
                report.failed.append((did, a, b))
                continue
            verdict = parsed["verdict"]
        if dry_run:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO EventVictimCoreference (deduplicated_event_id, name, other_name, verdict, "
            "canonical_name, reason, citations_json, model, prompt_version, evidence_hash, decided_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (did, a, b, verdict, parsed["canonical_name"] if answer else None,
             parsed["reason"] if answer else "a person's alias or alias decision",
             json.dumps(answer.get("citations") or []) if answer else "[]",
             perplexity_model() if answer else "human", PROMPT_VERSION, evidence, utc_now_iso()))
        conn.commit()
        report.verdicts[verdict] = report.verdicts.get(verdict, 0) + 1
        if verdict == "same":
            report.same.append((did, a, b))
    logger.info("Co-victim coreference: %d pair(s) proposed, %d asked, %d reused, %d by a person, %d failed, "
                "verdicts %s", report.proposed, report.asked, report.reused, report.by_person, len(report.failed),
                report.verdicts)
    return report


def same_pairs(conn: sqlite3.Connection, event_ids: Optional[Sequence[str]] = None) -> Dict[str, Set[frozenset]]:
    """``{event id: {frozenset({a, b})}}``: the pairs decided ``same`` for each event on its current question.

    A model's verdict counts only while the question it answered is the question now (the event's reporting, the
    two names' sentences and profiles); a person's always counts. No table, no pairs.
    """
    try:
        rows = conn.execute("SELECT deduplicated_event_id, name, other_name, model, evidence_hash "
                            "FROM EventVictimCoreference WHERE prompt_version = ? AND verdict = 'same'",
                            (PROMPT_VERSION,)).fetchall()
    except sqlite3.OperationalError:
        return {}
    wanted = set(event_ids) if event_ids is not None else None
    out: Dict[str, Set[frozenset]] = {}
    for did, a, b, model, evidence in rows:
        if wanted is not None and did not in wanted:
            continue
        if model == "human":
            if person_verdict(conn, a, b) != "same":
                continue
        elif evidence != _hash(question(conn, did, a, b)):
            continue
        out.setdefault(did, set()).add(frozenset((a, b)))
    return out
