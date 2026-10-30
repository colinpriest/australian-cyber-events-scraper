"""Names that are descriptions, not names: remembered, and never a victim.

"Australian university", "Australian sugar producer" and "Australian Super Funds" are
how a report describes an organisation it does not name (or several organisations).
Stored as a victim, such a description becomes an "organisation" in every count: two
unrelated universities' breaches turned into one organisation attacked twice, and the
sugar producer the reports named (Mackay Sugar) was filed under the description.

``DescriptiveNames`` remembers them, so every step avoids them:

* ``human`` - an owner's entry (``dedup_admin.py descriptive-name add NAME --reason ...``);
  never replaced or removed by a model, and it outranks a ``named`` label.
* ``label`` / ``llm`` - :func:`sync` lists every name labelled ``descriptive`` in
  ``evaluation/name_labels.csv`` and every name the calibrated proper-name judgement
  (:mod:`name_judgement`) scores below its cut-off, with the probability and model; a name
  that is later labelled ``named`` or scores above the cut-off is removed again (never a
  person's entry).

:func:`name_judgement.descriptive_names` includes every entry, so whatever already asks
:func:`name_judgement.is_descriptive_name` (role classification, victim fill and agreement,
alias learning, victim counting) honours the table. :func:`demote_victims` takes a
description that is already recorded as a victim off the event (its role becomes
``affected``; the scalar victim is cleared when it is the description), audited in
``DescriptiveVictimDemotions``, so victim fill can give the event the organisation its
evidence names. Names are matched case- and whitespace-insensitively.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from cyber_data_collector.utils.timestamps import utc_now_iso

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS DescriptiveNames (
    name_key     TEXT PRIMARY KEY,       -- name_key(name): lower case, single spaces
    name         TEXT NOT NULL,          -- as written
    source       TEXT NOT NULL,          -- human | label | llm
    reason       TEXT,
    probability  REAL,                   -- llm: P(the name identifies one specific organisation)
    model        TEXT,                   -- llm: the model that judged it
    created_at   TEXT NOT NULL           -- UTC, with offset
);
CREATE TABLE IF NOT EXISTS DescriptiveVictimDemotions (
    demotion_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    deduplicated_event_id  TEXT NOT NULL,
    entity_id              INTEGER,
    name                   TEXT NOT NULL,
    scalar_cleared         INTEGER NOT NULL DEFAULT 0,
    reason                 TEXT,
    created_at             TEXT NOT NULL
);
"""

SOURCE_HUMAN = "human"
SOURCE_LABEL = "label"
SOURCE_LLM = "llm"


class DescriptiveNameError(ValueError):
    """An entry was refused."""


def name_key(name: str) -> str:
    return " ".join((name or "").split()).lower()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _has_table(conn: sqlite3.Connection, name: str = "DescriptiveNames") -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def keys(conn: sqlite3.Connection) -> Set[str]:
    """Every listed name, as :func:`name_key`. Empty for a database without the table."""
    if not _has_table(conn):
        return set()
    return {r[0] for r in conn.execute("SELECT name_key FROM DescriptiveNames")}


def human_keys(conn: sqlite3.Connection) -> Set[str]:
    if not _has_table(conn):
        return set()
    return {r[0] for r in conn.execute("SELECT name_key FROM DescriptiveNames WHERE source = ?", (SOURCE_HUMAN,))}


def entries(conn: sqlite3.Connection) -> List[Dict[str, object]]:
    if not _has_table(conn):
        return []
    cur = conn.execute("SELECT name, source, reason, probability, model, created_at FROM DescriptiveNames "
                       "ORDER BY name_key")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def add(conn: sqlite3.Connection, name: str, reason: str, source: str = SOURCE_HUMAN,
        probability: Optional[float] = None, model: Optional[str] = None) -> bool:
    """List ``name``; True when written. A person's entry needs a reason and is never replaced by another source."""
    key = name_key(name)
    if not key:
        raise DescriptiveNameError("an empty name cannot be listed")
    if source == SOURCE_HUMAN and not (reason or "").strip():
        raise DescriptiveNameError("a person's entry needs a reason")
    ensure_schema(conn)
    existing = conn.execute("SELECT source FROM DescriptiveNames WHERE name_key = ?", (key,)).fetchone()
    if existing and existing[0] == SOURCE_HUMAN and source != SOURCE_HUMAN:
        return False
    conn.execute("INSERT OR REPLACE INTO DescriptiveNames (name_key, name, source, reason, probability, model, "
                 "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (key, " ".join(name.split()), source, reason, probability, model, utc_now_iso()))
    conn.commit()
    return True


def remove(conn: sqlite3.Connection, name: str, source: Optional[str] = None) -> bool:
    """Remove an entry (only of ``source`` when given); True when one was removed."""
    if not _has_table(conn):
        return False
    sql, args = "DELETE FROM DescriptiveNames WHERE name_key = ?", [name_key(name)]
    if source:
        sql += " AND source = ?"
        args.append(source)
    removed = conn.execute(sql, args).rowcount > 0
    conn.commit()
    return removed


def described(conn: sqlite3.Connection) -> Set[str]:
    """Every name known to be a description: the table, and the proper-name verdicts loaded for this run."""
    from cyber_data_collector.dedup import name_judgement as nj

    return keys(conn) | set(nj._DESCRIPTIVE)


def is_listed(descriptions: Set[str], name: Optional[str]) -> bool:
    return bool(name) and (name.strip().lower() in descriptions or name_key(name) in descriptions)


def joins_name_to_description(descriptions: Set[str], left: Optional[str], right: Optional[str]) -> bool:
    """One of the two is a description and the other is not: they are never one organisation.

    A description is not an organisation, so an alias or fold that joins a name to one files the named
    organisation under the description: "Mackay Sugar" was folded into "Australian sugar producer" and
    "AustralianSuper" into "Australian Super Funds" (2026-10-29), and every later victim fill of those events
    resolved the labelled organisation back to the description. Two descriptions joined is not this error.
    """
    return is_listed(descriptions, left) != is_listed(descriptions, right)


@dataclass
class SyncReport:
    added: int = 0
    removed: int = 0


def sync(conn: sqlite3.Connection) -> SyncReport:
    """List the labelled and the calibrated model verdicts; drop model/label entries no longer supported."""
    from cyber_data_collector.dedup import name_judgement as nj
    from cyber_data_collector.utils.llm_models import typesafe_model

    ensure_schema(conn)
    report = SyncReport()
    labels = nj.load_labels(nj.LABELS_PATH)
    labelled_named = {name_key(n) for n, r in labels.items() if (r.get("label") or "").strip() == "named"}
    labelled_named |= nj.labelled_victim_names()     # a label naming an event's victim by this name
    wanted: Dict[str, Tuple[str, str, Optional[float], Optional[str], str]] = {}
    for n, r in labels.items():
        if (r.get("label") or "").strip() == "descriptive":
            wanted[name_key(n)] = (r.get("name") or n, SOURCE_LABEL, None, None,
                                   (r.get("rationale") or "labelled descriptive").strip())
    cal = nj.active_calibration(nj.CALIBRATION_PATH, quiet=True)
    if cal:
        for n, p in nj.stored_probabilities(conn).items():
            k = name_key(n)
            if p < cal["cutoff"] and k not in labelled_named and k not in wanted:
                wanted[k] = (n, SOURCE_LLM, p, typesafe_model(),
                             f"P(names one organisation) {p:.2f} below the calibrated cut-off {cal['cutoff']:.2f}")
    current = {r[0]: r[1] for r in conn.execute("SELECT name_key, source FROM DescriptiveNames")}
    for k, (name, source, p, model, reason) in wanted.items():
        if k not in current:
            report.added += add(conn, name, reason, source=source, probability=p, model=model)
    for k, source in current.items():
        if source != SOURCE_HUMAN and k not in wanted:
            conn.execute("DELETE FROM DescriptiveNames WHERE name_key = ?", (k,))
            report.removed += 1
    conn.commit()
    return report


@dataclass
class Demotion:
    deduplicated_event_id: str
    entity_id: Optional[int]
    name: str
    scalar_cleared: bool = False


@dataclass
class DemotionReport:
    demoted: List[Demotion] = field(default_factory=list)


def demote_victims(conn: sqlite3.Connection, apply: bool = True) -> DemotionReport:
    """Take every description recorded as a victim of an Active event off it (role ``affected``; the scalar
    victim cleared when it is the description), audited; ``apply`` False only lists them."""
    from cyber_data_collector.dedup import name_judgement as nj

    descriptive = nj.descriptive_names(conn)
    report = DemotionReport()
    if not descriptive:
        return report
    is_desc = lambda n: bool(n) and (n.strip().lower() in descriptive or name_key(n) in descriptive)  # noqa: E731
    rows = conn.execute(
        """SELECT dee.deduplicated_event_id, dee.entity_id, v.entity_name, d.victim_organization_name
           FROM DeduplicatedEventEntities dee JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
           JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id
           WHERE dee.relationship_type = 'victim' AND d.status = 'Active'""").fetchall()
    seen_scalar: Set[str] = set()
    for event_id, entity_id, name, scalar in rows:
        if is_desc(name):
            report.demoted.append(Demotion(event_id, entity_id, name, scalar_cleared=is_desc(scalar)))
            seen_scalar.add(event_id)
    for event_id, scalar in conn.execute(
            "SELECT deduplicated_event_id, victim_organization_name FROM DeduplicatedEvents WHERE status = 'Active'"):
        if event_id not in seen_scalar and is_desc(scalar):
            report.demoted.append(Demotion(event_id, None, scalar, scalar_cleared=True))
    if apply and report.demoted:
        ensure_schema(conn)
        now = utc_now_iso()
        for d in report.demoted:
            if d.entity_id is not None:
                conn.execute("UPDATE DeduplicatedEventEntities SET relationship_type = 'affected' "
                             "WHERE deduplicated_event_id = ? AND entity_id = ? AND relationship_type = 'victim'",
                             (d.deduplicated_event_id, d.entity_id))
            if d.scalar_cleared:
                conn.execute("UPDATE DeduplicatedEvents SET victim_organization_name = NULL "
                             "WHERE deduplicated_event_id = ?", (d.deduplicated_event_id,))
            conn.execute("INSERT INTO DescriptiveVictimDemotions (deduplicated_event_id, entity_id, name, "
                         "scalar_cleared, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                         (d.deduplicated_event_id, d.entity_id, d.name, int(d.scalar_cleared),
                          "describes an organisation instead of naming it (DescriptiveNames)", now))
        conn.commit()
        logger.info("Demoted %d descriptive victim(s)", len(report.demoted))
    return report
