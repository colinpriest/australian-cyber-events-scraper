"""Anti-aliases: names that look alike but are different entities ("Optus" / "Optus Stadium").

``EntityAliases`` says which names are one organisation; ``EntityAntiAliases`` says which are not. The name
tests that join victims for counting and propose folds work on name shape alone, and shape cannot tell
"Optus Stadium" (a venue) from "Optus Pty Ltd", or "Monash IVF" from "Genea IVF"-style variants of one
business. Every path that joins two names consults this table first (:func:`victim_counting.same_organisation`,
:func:`entity_alias_verification.verified_groups`, :func:`entity_resolution.stored_aliases`,
:func:`entity_merge_ledger.overturned_merges`), so one entry keeps a pair apart everywhere.

**Where entries come from.** Like aliases, from the pipeline and from people:

* ``llm`` - every reusable Perplexity verdict that two names are ``different``, ``related`` or
  ``parent_subsidiary`` (``EntityAliasDecisions``) is listed here by :func:`sync_from_verdicts`, and
  :func:`learn_from_counting` asks Perplexity (the same verified question as an alias fold) about every
  pair of names that victim counting joins with no alias and no verdict behind it - that is how
  "Optus" / "Optus Stadium" is decided. A later ``same`` verdict removes an ``llm`` entry.
* ``human`` - ``dedup_admin.py anti-alias add A B --reason "..."``. A person's entry is never replaced or
  removed by a model, and is refused when a reviewed alias (``EXPLICIT_ALIASES`` or a ``human`` row of
  ``EntityAliases``) says the two are one organisation: that contradiction is for a person to resolve.

Names are matched after :func:`entity_resolution.normalize_name` (case, punctuation, accents).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from cyber_data_collector.dedup.entity_resolution import normalize_name
from cyber_data_collector.utils.timestamps import utc_now_iso

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS EntityAntiAliases (
    name_key_a   TEXT NOT NULL,          -- normalize_name of the first name (the pair is stored sorted)
    name_key_b   TEXT NOT NULL,
    name_a       TEXT NOT NULL,          -- the names as written
    name_b       TEXT NOT NULL,
    relation     TEXT NOT NULL,          -- different | related | parent_subsidiary
    source       TEXT NOT NULL,          -- human | llm
    reason       TEXT,
    citations_json TEXT,
    model        TEXT,                   -- the model whose verdict it lists (llm); NULL for a person's entry
    created_at   TEXT NOT NULL,          -- UTC, with offset
    PRIMARY KEY (name_key_a, name_key_b)
);
"""

RELATIONS = ("different", "related", "parent_subsidiary")
SOURCE_HUMAN = "human"
SOURCE_LLM = "llm"


class AntiAliasError(ValueError):
    """An entry was refused (a reviewed alias says the opposite, or the names are one name)."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _has_table(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'EntityAntiAliases'"
                        ).fetchone() is not None


def pair_key(a: str, b: str) -> Tuple[str, str]:
    """The stored key of a pair: both names normalised, sorted."""
    ka, kb = normalize_name(a), normalize_name(b)
    return (ka, kb) if ka <= kb else (kb, ka)


def pair_set(conn: sqlite3.Connection) -> Set[Tuple[str, str]]:
    """Every anti-aliased pair, as :func:`pair_key` keys. Empty for a database without the table."""
    if not _has_table(conn):
        return set()
    return {(a, b) for a, b in conn.execute("SELECT name_key_a, name_key_b FROM EntityAntiAliases")}


def is_anti_alias(pairs: Set[Tuple[str, str]], a: Optional[str], b: Optional[str]) -> bool:
    return bool(a and b) and pair_key(a, b) in pairs


def _reviewed_alias(conn: sqlite3.Connection, a: str, b: str) -> Optional[str]:
    """Why ``a`` and ``b`` are recorded by a person as one organisation, if they are."""
    from cyber_data_collector.dedup.entity_merge import EXPLICIT_ALIASES

    for x, y in ((a, b), (b, a)):
        if EXPLICIT_ALIASES.get(x) == y:
            return "EXPLICIT_ALIASES"
    try:
        rows = conn.execute("SELECT raw_name, canonical_name FROM EntityAliases WHERE source = 'human'").fetchall()
    except sqlite3.Error as exc:
        if "no such table" in str(exc):
            return None
        raise
    key = pair_key(a, b)
    if any(pair_key(raw, canonical) == key for raw, canonical in rows):
        return "a human row of EntityAliases"
    return None


def add(conn: sqlite3.Connection, a: str, b: str, reason: str, relation: str = "different",
        source: str = SOURCE_HUMAN, model: Optional[str] = None, citations: Iterable[str] = ()) -> bool:
    """List ``a`` / ``b`` as different entities. Returns True when the table changed. Does not commit.

    A person's entry needs a reason and replaces a model's; a model's never replaces a person's. Raises
    :class:`AntiAliasError` for one name twice, an unknown relation, or a pair a reviewed alias joins.
    """
    ensure_schema(conn)
    if relation not in RELATIONS:
        raise AntiAliasError(f"relation must be one of {', '.join(RELATIONS)}, not {relation!r}")
    key = pair_key(a, b)
    if key[0] == key[1]:
        raise AntiAliasError(f"{a!r} and {b!r} are the same name")
    if source == SOURCE_HUMAN and not (reason or "").strip():
        raise AntiAliasError("a person's anti-alias needs a reason")
    reviewed = _reviewed_alias(conn, a, b)
    if reviewed:
        raise AntiAliasError(f"{a!r} / {b!r} is a reviewed alias ({reviewed}); resolve that contradiction first")
    existing = conn.execute("SELECT source, relation, reason FROM EntityAntiAliases WHERE name_key_a = ? AND "
                            "name_key_b = ?", key).fetchone()
    if existing is not None:
        if existing[0] == SOURCE_HUMAN and source != SOURCE_HUMAN:
            return False
        if tuple(existing) == (source, relation, reason):
            return False
    first, second = sorted((a, b), key=normalize_name)
    conn.execute("INSERT OR REPLACE INTO EntityAntiAliases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (*key, first, second, relation, source, reason, json.dumps(list(citations)), model, utc_now_iso()))
    return True


def remove(conn: sqlite3.Connection, a: str, b: str, source: Optional[str] = None) -> bool:
    """Remove the entry for ``a`` / ``b`` (only one of ``source`` when given). Returns True when one went."""
    if not _has_table(conn):
        return False
    sql = "DELETE FROM EntityAntiAliases WHERE name_key_a = ? AND name_key_b = ?"
    params: List[str] = list(pair_key(a, b))
    if source:
        sql += " AND source = ?"
        params.append(source)
    return conn.execute(sql, params).rowcount > 0


def entries(conn: sqlite3.Connection) -> List[Dict[str, str]]:
    if not _has_table(conn):
        return []
    cols = ["name_a", "name_b", "relation", "source", "reason", "model", "created_at"]
    return [dict(zip(cols, r)) for r in conn.execute(f"SELECT {', '.join(cols)} FROM EntityAntiAliases "
                                                     "ORDER BY source, name_key_a, name_key_b")]


@dataclass
class SyncReport:
    added: int = 0
    removed: int = 0


def sync_from_verdicts(conn: sqlite3.Connection) -> SyncReport:
    """List every reusable not-the-same verdict as an ``llm`` anti-alias; drop ``llm`` entries a verdict now calls
    the ``same`` organisation. A person's entries are left alone. Does not commit."""
    from cyber_data_collector.dedup.entity_alias_verification import CONFLICT_VERDICTS, stored_verdicts

    ensure_schema(conn)
    report = SyncReport()
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'EntityAliasDecisions'"
                        ).fetchone():
        return report
    for (a, b), stored in stored_verdicts(conn).items():
        if pair_key(a, b)[0] == pair_key(a, b)[1]:
            continue
        if stored["verdict"] in CONFLICT_VERDICTS:
            if _reviewed_alias(conn, a, b):
                continue                       # a reviewed alias outranks a model's verdict
            report.added += add(conn, a, b, stored.get("reason") or "", relation=stored["verdict"],
                                source=SOURCE_LLM, model=stored.get("model"))
        elif stored["verdict"] == "same":
            report.removed += remove(conn, a, b, source=SOURCE_LLM)
    return report


@dataclass
class LearnReport:
    candidates: int = 0                        # name pairs counting joined with nothing behind the join
    asked: int = 0
    listed: List[Tuple[str, str, str]] = field(default_factory=list)       # (a, b, relation)
    confirmed_same: List[Tuple[str, str]] = field(default_factory=list)
    failed: List[Tuple[str, str]] = field(default_factory=list)


def counting_joins(conn: sqlite3.Connection) -> List[Tuple[str, str]]:
    """Name pairs victim counting joins (:func:`victim_counting.organisation_groups` over every active victim)
    that no alias, spelling equivalence or verdict stands behind - the joins resting on name shape alone."""
    from collections import defaultdict

    from cyber_data_collector.dedup.entity_alias_verification import stored_verdicts
    from cyber_data_collector.dedup.entity_merge import EXPLICIT_ALIASES
    from cyber_data_collector.dedup.entity_resolution import canonical_key, stored_aliases
    from cyber_data_collector.dedup.victim_counting import organisation_groups

    ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT dee.entity_id FROM DeduplicatedEventEntities dee JOIN DeduplicatedEvents d "
        "ON d.deduplicated_event_id = dee.deduplicated_event_id "
        "WHERE dee.relationship_type = 'victim' AND COALESCE(d.status, 'Active') = 'Active'")]
    groups: Dict[int, List[int]] = defaultdict(list)
    for entity, rep in organisation_groups(conn, ids).items():
        groups[rep].append(entity)
    names = {}
    for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
        names.update(conn.execute(f"SELECT entity_id, entity_name FROM EntitiesV2 WHERE entity_id IN "
                                  f"({','.join('?' * len(chunk))})", chunk).fetchall())
    aliases = {**stored_aliases(conn), **EXPLICIT_ALIASES}
    alias_keys = {pair_key(raw, canonical) for raw, canonical in aliases.items()}
    verdicts = {pair_key(a, b) for a, b in stored_verdicts(conn)} if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'EntityAliasDecisions'").fetchone() else set()
    out = []
    for members in groups.values():
        member_names = sorted({names[m] for m in members if names.get(m)})
        for i, a in enumerate(member_names):
            for b in member_names[i + 1:]:
                key = pair_key(a, b)
                if key[0] == key[1] or canonical_key(a) == canonical_key(b) or key in alias_keys or key in verdicts:
                    continue
                out.append((a, b))
    return out


def learn_from_counting(conn: sqlite3.Connection, search: Optional[Callable] = None,
                        limit: Optional[int] = None) -> LearnReport:
    """Ask Perplexity about every pair :func:`counting_joins` finds and list the ones it says are not the same.

    The question and its stored answer are the alias verification's (:func:`entity_alias_verification.verify_pair`),
    asked once per pair. A pair Perplexity could not decide is in ``failed`` (counting keeps joining it) - the
    caller reports it and exits non-zero. Commits.
    """
    from cyber_data_collector.dedup.entity_alias_verification import (
        CONFLICT_VERDICTS, VerificationReport, perplexity_search, stored_verdicts, verify_pair,
    )

    report = LearnReport()
    pairs = counting_joins(conn)
    report.candidates = len(pairs)
    known = stored_verdicts(conn)
    verification = VerificationReport()
    for a, b in pairs[:limit] if limit else pairs:
        verdict = verify_pair(conn, a, b, search or perplexity_search, known, verification)
        report.asked += 1
        if verdict is None:
            report.failed.append((a, b))
        elif verdict in CONFLICT_VERDICTS:
            report.listed.append((a, b, verdict))
        elif verdict == "same":
            report.confirmed_same.append((a, b))
    sync_from_verdicts(conn)
    conn.commit()
    return report
