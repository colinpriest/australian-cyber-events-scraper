"""Make each event's victim agree with the victim its own records name.

Every source record carries the organisation Perplexity identified as the one
breached (``formal_entity_name``), researched against the article. The event's
victim, by contrast, comes from an LLM role pass over the entity *mentions*,
and mentions can mislead: a Reuters piece on TPG's December 2022 email breach
mentions Optus, a story about Henning Harders' ransomware attack mentions Toll,
and a canonicalisation rule had folded Sharp Office Systems into Sharp Motor
Group. All three became second attacks on the wrong organisation weeks after its
real one - exactly the short-gap band the recurrence analysis is estimated from.

The rule is deliberately narrow, because the role pass is right far more often
than not:

* the event has exactly one victim, and *no* member's Perplexity organisation
  denotes it - a single dissenting record never moves a victim;
* at least two thirds of the members that name an organisation name the same
  one, and that organisation is not the event's vendor (Perplexity often names
  the supplier in a supply-chain breach, where the convention is that the
  customer is the victim);
* the organisation is a real one, not a collective or a threat actor, and the
  role pass did not label it the event's vendor, regulator or product;
* no record's title names the current victim (Perplexity often answers with a
  legal or parent name - "Insignia Financial" for MLC - that no string test can
  equate with the brand), and the replacement is named in the records' text.

``has_human_override`` is not consulted: every ledger merge sets it, automated
passes included, and the human decisions it reflects are about membership, not
about who the victim is.

"Denotes" combines resolver key overlap and structural equality ("ACT
Government" / "Australian Capital Territory Government"), vetoed when the two
names state different lines of business ("Sharp Office Systems" / "Sharp Motor
Group", which share the key ``sharp``).

Deterministic and free: no API calls. Idempotent - once the victim agrees, the
event is never selected again. Used by the ``check-victims`` command, the
refresh pipeline, and :func:`entity_classification.resolve_victim`, so a later
role reclassification cannot put the wrong victim back.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter
from typing import Any, Dict, List, Optional

from cyber_data_collector.dedup.coherence import load_event_members
from cyber_data_collector.dedup.entity_merge import (
    _tokens,
    distinctive_core,
    lines_of_business_conflict,
    same_organisation_by_structure,
)
from cyber_data_collector.dedup.entity_resolution import EntityResolver, canonical_entity_name

logger = logging.getLogger(__name__)

# Share of the organisation-naming members that must name the replacement.
MIN_MAJORITY = 2 / 3

_LEGAL_SUFFIX = re.compile(
    r"[\s,]+(pty\.?\s*(ltd|limited)\.?|ltd\.?|limited|inc\.?|incorporated|llc|plc)$", re.I)


def strip_legal_suffix(name: str) -> str:
    """'Sharp Office Systems Pty Ltd' -> 'Sharp Office Systems'."""
    previous = None
    name = name.strip()
    while previous != name:
        previous, name = name, _LEGAL_SUFFIX.sub("", name).strip()
    return name


def denotes(resolver: EntityResolver, left: Optional[str], right: Optional[str]) -> bool:
    """True when two names refer to the same organisation, for this rule's purposes."""
    if not left or not right:
        return False
    left, right = strip_legal_suffix(left), strip_legal_suffix(right)
    if lines_of_business_conflict(left, right):
        return False
    return bool(resolver.blocks_for(left) & resolver.blocks_for(right)
                or same_organisation_by_structure(left, right)
                or resolver.same_organisation(left, right))


_LEGAL_WORDS = {"pty", "ltd", "limited", "inc", "group", "holdings", "the", "of", "and", "co",
                "company", "corporation", "corp"}


def _mentions(text: str, name: str, unlike: Optional[str] = None) -> bool:
    """True when ``text`` names ``name`` by a whole-word distinctive token.

    With ``unlike``, only tokens that tell ``name`` apart from it count: a title
    saying "Sharp Office" names Sharp Office Systems, not Sharp Motor Group,
    though both share "sharp".
    """
    tokens = set(distinctive_core(name))
    if unlike:
        tokens -= distinctive_core(unlike)
        if not tokens:
            tokens = set(_tokens(name)) - set(_tokens(unlike)) - _LEGAL_WORDS
    # Tokenised the same way as the names, so "NSW" in a title matches the
    # expanded "new south wales" of "NSW Health".
    return bool(tokens & set(_tokens(text)))


def _named_parts(org: str) -> List[str]:
    """'Master Wealth Control Pty Ltd; Property Lovers' -> both names."""
    return [p.strip() for p in re.split(r";|\s/\s", org) if p.strip()]


def _may_be(resolver: EntityResolver, org: str, victim: str) -> bool:
    """Loose test: could this record's organisation be the current victim?

    Recall-oriented on purpose - the rule fires only when every record clearly
    names someone else, so "Ollies Place Kidswear" for Ollie's Place and
    "Gallerie degli Uffizi" for the Uffizi Galleries must count as agreement.
    """
    for part in _named_parts(org):
        if lines_of_business_conflict(part, victim):
            continue
        if denotes(resolver, part, victim) or resolver.are_candidates(part, victim):
            return True
        core_part, core_victim = distinctive_core(part), distinctive_core(victim)
        if core_part & core_victim:
            return True
    return False


def _is_unusable_victim(name: str) -> bool:
    from cyber_data_collector.dedup.entity_classification import (
        _is_collective_phrase, is_never_victim, is_non_organisation, is_threat_actor,
    )
    return (is_non_organisation(name) or is_never_victim(name) or is_threat_actor(name)
            or _is_collective_phrase(name))


def _victims(conn: sqlite3.Connection, dedup_id: str) -> List[str]:
    return [r[0] for r in conn.execute(
        """SELECT v.entity_name FROM DeduplicatedEventEntities dee
           JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
           WHERE dee.deduplicated_event_id = ? AND dee.relationship_type = 'victim'""",
        (dedup_id,))]


def proposed_victim(
    conn: sqlite3.Connection,
    dedup_id: str,
    victim: Optional[str],
    vendor: Optional[str],
    resolver: Optional[EntityResolver] = None,
) -> Optional[Dict[str, Any]]:
    """The organisation the event's records name instead of ``victim``, else None.

    Returns ``{"name", "support", "named"}``: the Perplexity name to adopt, how
    many members name it, and how many members name any organisation.
    """
    if not victim:
        return None
    resolver = resolver or EntityResolver(conn)
    members = load_event_members(conn, dedup_id)
    orgs = [m["org"] for m in members if m["org"]]
    if not orgs or any(_may_be(resolver, org, victim) for org in orgs):
        return None

    # Cluster the named organisations; spellings of one ("Toll Holdings
    # Limited", "Toll Group") count together.
    clusters: List[List[str]] = []
    for org in orgs:
        for cluster in clusters:
            if denotes(resolver, org, cluster[0]):
                cluster.append(org)
                break
        else:
            clusters.append([org])
    best = max(clusters, key=len)
    if len(best) < MIN_MAJORITY * len(orgs):
        return None
    name = strip_legal_suffix(Counter(best).most_common(1)[0][0])
    if denotes(resolver, name, vendor) or _is_unusable_victim(name):
        return None
    # Perplexity often answers with the legal or parent name ("International
    # Business Machines Corporation" for IBM, "Insignia Financial" for MLC),
    # which no string test can equate with the brand. When a record's own
    # title names the current victim, the reporting is about it: keep it.
    if _mentions(" ".join(m["title"] for m in members), victim, unlike=name):
        return None
    # The role pass judged it the supplier, regulator or product here (Oracle
    # in a campaign against Oracle E-Business Suite customers; an auditor-
    # general reporting on an agency): Perplexity's answer is the wrong party.
    for other, role in conn.execute(
            """SELECT DISTINCT v.entity_name, dee.relationship_type
               FROM DeduplicatedEventEntities dee JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
               WHERE dee.deduplicated_event_id = ?
                 AND dee.relationship_type IN ('vendor', 'regulator', 'product')""", (dedup_id,)):
        if denotes(resolver, name, other):
            return None
    # The replacement must be named in the records themselves, not only in
    # Perplexity's answer about them.
    if not _mentions(" ".join(m["title"] + " " + m["body"] for m in members), name):
        return None
    return {"name": name, "support": len(best), "named": len(orgs)}


def _entity_for(conn: sqlite3.Connection, dedup_id: str, name: str,
                resolver: EntityResolver) -> int:
    """Entity id for ``name``: one already linked to the event, else by name, else new."""
    linked = conn.execute(
        """SELECT DISTINCT v.entity_id, v.entity_name
           FROM EventDeduplicationMap m
           JOIN EnrichedEventEntities ee ON ee.enriched_event_id = m.enriched_event_id
           JOIN EntitiesV2 v ON v.entity_id = ee.entity_id
           WHERE m.deduplicated_event_id = ?""", (dedup_id,)).fetchall()
    for entity_id, entity_name in linked:
        if denotes(resolver, entity_name, name):
            return entity_id
    name = canonical_entity_name(conn, name)
    row = conn.execute("SELECT entity_id FROM EntitiesV2 WHERE entity_name = ? COLLATE NOCASE",
                       (name,)).fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        "INSERT INTO EntitiesV2 (entity_name, entity_kind) VALUES (?, 'organisation')", (name,))
    return cur.lastrowid


def apply_victim(conn: sqlite3.Connection, dedup_id: str, old_victim: Optional[str],
                 name: str, resolver: Optional[EntityResolver] = None,
                 entity_id: Optional[int] = None) -> str:
    """Link ``name`` as the event's victim and demote the old one to bystander.

    ``entity_id``, when given, is the entity to link; otherwise it is found
    with :func:`_entity_for`, whose loose ``denotes`` test can return the old
    victim itself for an acronym clash (UWA / UN Women Australia) - callers
    replacing a victim judged wrong pass the entity explicitly.

    Returns the entity name finally stored as the victim.
    """
    from cyber_data_collector.dedup.entity_classification import industry_for

    if entity_id is None:
        resolver = resolver or EntityResolver(conn)
        entity_id = _entity_for(conn, dedup_id, name, resolver)
    stored_name = conn.execute("SELECT entity_name FROM EntitiesV2 WHERE entity_id = ?",
                               (entity_id,)).fetchone()[0]
    members = [r[0] for r in conn.execute(
        "SELECT enriched_event_id FROM EventDeduplicationMap WHERE deduplicated_event_id = ?",
        (dedup_id,))]

    if old_victim:
        old = conn.execute("SELECT entity_id FROM EntitiesV2 WHERE entity_name = ?",
                           (old_victim,)).fetchone()
        if old and old[0] != entity_id:
            conn.execute("UPDATE DeduplicatedEventEntities SET relationship_type = 'bystander' "
                         "WHERE deduplicated_event_id = ? AND entity_id = ?", (dedup_id, old[0]))
            conn.executemany("UPDATE EnrichedEventEntities SET relationship_type = 'bystander' "
                             "WHERE enriched_event_id = ? AND entity_id = ?",
                             [(m, old[0]) for m in members])

    for member in members:
        updated = conn.execute("UPDATE EnrichedEventEntities SET relationship_type = 'victim' "
                               "WHERE enriched_event_id = ? AND entity_id = ?", (member, entity_id))
        if updated.rowcount == 0:
            conn.execute("INSERT INTO EnrichedEventEntities (enriched_event_id, entity_id, "
                         "relationship_type, confidence_score) VALUES (?, ?, 'victim', NULL)",
                         (member, entity_id))
    updated = conn.execute("UPDATE DeduplicatedEventEntities SET relationship_type = 'victim' "
                           "WHERE deduplicated_event_id = ? AND entity_id = ?", (dedup_id, entity_id))
    if updated.rowcount == 0:
        conn.execute("INSERT INTO DeduplicatedEventEntities (deduplicated_event_id, entity_id, "
                     "relationship_type, confidence_score, source_count) VALUES (?, ?, 'victim', NULL, ?)",
                     (dedup_id, entity_id, len(members)))
    conn.execute("UPDATE DeduplicatedEvents SET victim_organization_name = ?, "
                 "victim_organization_industry = ?, updated_at = CURRENT_TIMESTAMP "
                 "WHERE deduplicated_event_id = ?",
                 (stored_name, industry_for(conn, stored_name), dedup_id))
    return stored_name


def prune_conflicting_aliases(conn: sqlite3.Connection, dry_run: bool = True) -> List[tuple]:
    """Drop rule-made aliases joining names in different lines of business.

    canonicalise-entities records every fold in EntityAliases so later ingests
    resolve the variant. A fold the corrected canonicaliser now refuses
    ("Sharp Office Systems Pty Ltd" -> "Sharp Motor Group") would otherwise
    keep re-filing new records under the wrong organisation. Human and
    explicit aliases are never touched.
    """
    stale = [(raw, canonical) for raw, canonical in conn.execute(
        "SELECT raw_name, canonical_name FROM EntityAliases WHERE source = 'rule'")
        if lines_of_business_conflict(raw, canonical)]
    if stale and not dry_run:
        conn.executemany("DELETE FROM EntityAliases WHERE source = 'rule' AND raw_name = ? "
                         "AND canonical_name = ?", stale)
        logger.info("Dropped %d alias(es) joining different lines of business", len(stale))
    return stale


def check_victims(conn: sqlite3.Connection, dry_run: bool = True) -> List[Dict[str, Any]]:
    """Re-attribute every active event whose records name a different victim.

    Returns one finding per event changed (or that would be, in a dry run).
    """
    from cyber_data_collector.dedup.ledger import DedupLedger

    prune_conflicting_aliases(conn, dry_run=dry_run)

    events = conn.execute(
        """SELECT deduplicated_event_id, title, event_date, victim_organization_name,
                  vendor_organization_name
           FROM DeduplicatedEvents
           WHERE status = 'Active'""").fetchall()
    resolver = EntityResolver(conn)
    findings: List[Dict[str, Any]] = []
    for dedup_id, title, event_date, victim, vendor in events:
        victims = _victims(conn, dedup_id)
        if len(victims) > 1:
            continue  # co-equal victims (ProctorU's ten universities): not this rule's shape
        current = victim or (victims[0] if victims else None)
        proposal = proposed_victim(conn, dedup_id, current, vendor, resolver)
        if proposal is None:
            continue
        finding = {"deduplicated_event_id": dedup_id, "title": title, "event_date": event_date,
                   "old_victim": current, "new_victim": proposal["name"],
                   "support": f"{proposal['support']}/{proposal['named']}"}
        if not dry_run:
            DedupLedger(conn).snapshot_event(f"check-victims-{dedup_id}", dedup_id)
            finding["new_victim"] = apply_victim(conn, dedup_id, current, proposal["name"], resolver)
            logger.info("Victim of %s: %r -> %r (%s of the records name it)", dedup_id,
                        current, finding["new_victim"], finding["support"])
        findings.append(finding)
    if not dry_run:
        conn.commit()
    return findings
