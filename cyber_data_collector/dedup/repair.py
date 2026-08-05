"""Repeatable repairs for values that were never derived from their evidence.

Both defects here are the same shape as the date defect in
:mod:`cyber_data_collector.dedup.date_selection`: a field on
``DeduplicatedEvents`` was written once and never re-checked against the source
records behind it, so a wrong value survived every rerun.

* :func:`recount` - ``records_affected``. NSW Health held 12,900,000 while its
  single source record says 600; Health Management Systems held the same figure
  although none of its five records states one. Both had inherited MediSecure's
  total, and the three organisations are unrelated.
* :func:`name_entities` - victims recorded as a description rather than a name.
  Coverage often withholds the organisation in the headline ("Australia's
  largest home builder confirms ransomware attack"), and the description was
  stored as the entity. It names nobody, matches nothing during blocking, and
  reads on the dashboard as though it were the attacked organisation.

These live in the package rather than in a script so they can be re-run on
future ingests, tested, and called from the pipeline - the values they correct
are re-introduced by every new batch of records.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Callable, Dict, List, Optional, Tuple

from cyber_data_collector.dedup.date_selection import derive_records_affected
from cyber_data_collector.dedup.entity_merge import OrganisationResolver
from cyber_data_collector.utils.validation import validate_records_affected
from cyber_data_collector.dedup.victim_selection import (
    is_descriptive_label,
    is_never_victim,
    is_threat_actor,
)

logger = logging.getLogger(__name__)

# Confidence below which a resolved organisation name is not trusted.
MIN_NAME_CONFIDENCE = 0.7


class RepairReport(dict):
    """Plain dict of result lists, with a readable summary."""

    def summary(self) -> str:
        return "; ".join(
            f"{key.replace('_', ' ')}: {len(value)}"
            for key, value in sorted(self.items())
            if isinstance(value, list)
        )


def recount(conn: sqlite3.Connection, dry_run: bool = False) -> RepairReport:
    """Re-derive ``records_affected`` from each event's own source records.

    Returns a report with ``corrected`` (a figure some record states),
    ``cleared`` (no record states any figure) and ``flagged`` (records disagree
    by more than an order of magnitude, so any choice would be a guess).
    """
    report = RepairReport(corrected=[], cleared=[], flagged=[])

    rows = conn.execute(
        """
        SELECT deduplicated_event_id AS id, title, records_affected AS rec,
               victim_organization_name AS victim
        FROM DeduplicatedEvents WHERE COALESCE(status, 'Active') = 'Active'
        """
    ).fetchall()

    for row in rows:
        dedup_id = row["id"] if isinstance(row, sqlite3.Row) else row[0]
        title = row["title"] if isinstance(row, sqlite3.Row) else row[1]
        stored = row["rec"] if isinstance(row, sqlite3.Row) else row[2]
        victim = row["victim"] if isinstance(row, sqlite3.Row) else row[3]

        members = [
            r[0] for r in conn.execute(
                """
                SELECT e.records_affected FROM EventDeduplicationMap m
                JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
                WHERE m.deduplicated_event_id = ?
                """, (dedup_id,))
        ]
        value, reason = derive_records_affected(members, stored)

        # Source records can agree on a figure that still cannot be true - the
        # "Queensland education sector" records all report 200,000,000, roughly
        # seven times Australia's population. Plausibility outranks consensus,
        # and running it here keeps this pass and fix_records_affected.py from
        # undoing each other on every run.
        if value is not None:
            checked = validate_records_affected(value, title or "", victim or "")
            if checked != value:
                value = checked
                reason = "consensus figure fails victim-aware plausibility"

        if "disagree" in reason:
            if stored is not None:
                report["flagged"].append(
                    (title, stored, sorted({int(v) for v in members if v})))
            continue
        if reason == "supported" or value == stored:
            continue
        if stored is None and value is None:
            continue

        entry = (victim, title, stored, value, reason)
        report["cleared" if value is None else "corrected"].append(entry)
        if not dry_run:
            conn.execute(
                "UPDATE DeduplicatedEvents SET records_affected = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE deduplicated_event_id = ?",
                (value, dedup_id))

    if not dry_run:
        conn.commit()
    return report


def _accept_name(name: Optional[str]) -> bool:
    """Whether a resolved organisation name may be used as the victim.

    The resolver has no victim rules of its own. Without this it returned the
    ACSC - a regulator that reports breaches - and a US children's hospital as
    Australian victims.
    """
    return bool(name) and not (
        is_descriptive_label(name) or is_never_victim(name) or is_threat_actor(name)
    )


def _replace_entity(
    conn: sqlite3.Connection, dedup_id: str, old_entity_id: int, replacement: str
) -> None:
    """Point this event's links at ``replacement``, creating it if needed."""
    found = conn.execute(
        "SELECT entity_id FROM EntitiesV2 WHERE entity_name = ?", (replacement,)
    ).fetchone()
    if found is None:
        conn.execute(
            "INSERT INTO EntitiesV2 (entity_name, is_australian, entity_kind) "
            "VALUES (?, 1, 'organisation')", (replacement,))
        found = conn.execute(
            "SELECT entity_id FROM EntitiesV2 WHERE entity_name = ?",
            (replacement,)).fetchone()
    new_id = found[0]

    conn.execute(
        "UPDATE OR IGNORE DeduplicatedEventEntities SET entity_id = ? "
        "WHERE deduplicated_event_id = ? AND entity_id = ?",
        (new_id, dedup_id, old_entity_id))
    conn.execute(
        "UPDATE OR IGNORE EnrichedEventEntities SET entity_id = ? WHERE entity_id = ? "
        "AND enriched_event_id IN (SELECT enriched_event_id FROM "
        "EventDeduplicationMap WHERE deduplicated_event_id = ?)",
        (new_id, old_entity_id, dedup_id))
    conn.execute(
        "UPDATE DeduplicatedEvents SET victim_organization_name = ? "
        "WHERE deduplicated_event_id = ?", (replacement, dedup_id))


def name_entities(
    conn: sqlite3.Connection,
    resolver: Optional[OrganisationResolver] = None,
    use_llm: bool = True,
    search: bool = True,
    dry_run: bool = False,
) -> RepairReport:
    """Replace descriptions used as entity names with the organisation itself.

    Sources, cheapest first: another entity already recorded on the same event,
    then the event's own description, then a search. The phrase itself is
    always demoted - it is never the organisation that was attacked, whether or
    not a name was found.
    """
    report = RepairReport(from_sibling=[], from_text=[], unnamed=[])
    if resolver is None and use_llm:
        resolver = OrganisationResolver()

    rows = conn.execute(
        """
        SELECT DISTINCT v.entity_id AS eid, v.entity_name AS name,
               d.deduplicated_event_id AS did, d.title AS title,
               d.victim_organization_name AS scalar
        FROM DeduplicatedEventEntities dee
        JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
        JOIN DeduplicatedEvents d
             ON d.deduplicated_event_id = dee.deduplicated_event_id
        WHERE dee.relationship_type = 'victim'
          AND COALESCE(d.status, 'Active') = 'Active'
        """
    ).fetchall()
    targets = [r for r in rows if is_descriptive_label(r["name"])]
    report["examined"] = len(targets)

    for r in targets:
        siblings = [
            x[0] for x in conn.execute(
                """
                SELECT v.entity_name FROM DeduplicatedEventEntities dee
                JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
                WHERE dee.deduplicated_event_id = ? AND dee.entity_id != ?
                  AND dee.relationship_type IN ('victim', 'affected_customer')
                """, (r["did"], r["eid"]))
        ]
        named = [n for n in siblings if _accept_name(n)]

        replacement = None
        if named:
            replacement = r["scalar"] if r["scalar"] in named else named[0]
            report["from_sibling"].append((r["name"], replacement, r["title"]))
        elif resolver is not None:
            desc = conn.execute(
                """
                SELECT COALESCE(e.description, e.summary) FROM EventDeduplicationMap m
                JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
                WHERE m.deduplicated_event_id = ?
                  AND COALESCE(e.description, e.summary) IS NOT NULL LIMIT 1
                """, (r["did"],)).fetchone()
            got = resolver.resolve(r["name"], r["title"] or "",
                                   desc[0] if desc else None, search=search)
            if (got and got.confidence >= MIN_NAME_CONFIDENCE
                    and _accept_name(got.name)):
                replacement = got.name
                report["from_text"].append(
                    (r["name"], got.name, got.confidence, got.basis))

        if replacement is None:
            report["unnamed"].append((r["name"], r["title"]))
        if dry_run:
            continue

        if replacement:
            _replace_entity(conn, r["did"], r["eid"], replacement)
        else:
            conn.execute(
                "UPDATE DeduplicatedEvents SET victim_organization_name = NULL "
                "WHERE deduplicated_event_id = ? AND victim_organization_name = ?",
                (r["did"], r["name"]))

        conn.execute(
            "UPDATE DeduplicatedEventEntities SET relationship_type = 'bystander' "
            "WHERE deduplicated_event_id = ? AND entity_id = ?",
            (r["did"], r["eid"]))
        conn.execute("UPDATE EntitiesV2 SET entity_kind = 'other' WHERE entity_id = ?",
                     (r["eid"],))

    if not dry_run:
        conn.commit()
    return report
