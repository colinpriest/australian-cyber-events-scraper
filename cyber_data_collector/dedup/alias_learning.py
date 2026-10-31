"""Learn organisation aliases from evidence, not just from name shape.

``canonicalise-entities`` proposed folds from name structure alone - containment,
acronyms, spelling - and a Perplexity verdict confirmed each
(:mod:`entity_alias_verification`). Structure only sees names that are *both
still entities*. Two things it never saw, which is how coverage of one incident
came to name "Medicare", "Australian Medicare" and "Services Australia" for
months without anything joining them:

1. **A variant of a name that was folded away.** "Medicare" is a stored alias of
   Services Australia, so there is no "Medicare" entity left for "Australian
   Medicare" to be compared with. :func:`variant_proposals` compares every entity
   name with the *known variants* too (every stored alias): a name that contains,
   or is contained in, a known variant - the variant plus a qualifier - is
   proposed against that variant. Verified as the same organisation as the
   variant, it inherits the variant's canonical name.
2. **Names that co-refer in one incident.** Two records the duplicate check or a
   merge has put in one event each name a single victim, and the names differ:
   either two victims of one incident or two names for one. They need not share
   a word ("Australian health department" and the agency that runs the service).
   :func:`coreference_proposals` proposes those pairs.

Neither rule decides anything. Every proposal goes through the same verification
as before - Perplexity with citations, verdict ``same`` / ``parent_subsidiary`` /
``related`` / ``different`` / ``unsure`` - and only ``same`` folds; each verdict is
stored in ``EntityAliasDecisions`` and a pair is asked once. A learned alias is
stored in ``EntityAliases`` (source ``llm``) by the same fold step, so every later
ingest resolves the variant. Names a judgement has found descriptive
(:mod:`name_judgement`) are never proposed. Acronyms stay contextual
(:mod:`contextual_aliases`).

A *product or system named after an organisation* ("Medicare statistics portal")
is not folded - a portal is not the agency. The victim logic maps it to the
organisation instead (:func:`product_hosts`, used by the victim candidates).
"""

from __future__ import annotations

import itertools
import logging
import sqlite3
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Set, Tuple

from cyber_data_collector.dedup import entity_alias_verification as eav

logger = logging.getLogger(__name__)

#: A record-level co-reference is proposed only for events naming at most this many distinct victims
#: (an event with a dozen co-victims, such as a supplier's customers, is not a naming question).
MAX_COREFERENT_NAMES = 4


def _organisation_names(conn: sqlite3.Connection) -> List[str]:
    from cyber_data_collector.dedup.entity_merge import _is_date
    from cyber_data_collector.dedup.name_judgement import is_descriptive_name
    from cyber_data_collector.dedup.victim_selection import is_descriptive_label, is_non_organisation

    names = []
    for (name,) in conn.execute(
            """SELECT entity_name FROM EntitiesV2 WHERE entity_name IS NOT NULL AND TRIM(entity_name) != ''
               AND COALESCE(entity_kind, 'organisation') IN ('organisation', 'government_body')"""):
        if is_descriptive_label(name) or is_non_organisation(name) or _is_date(name) or is_descriptive_name(name):
            continue
        names.append(name)
    return names


def variant_proposals(conn: sqlite3.Connection) -> List[Tuple[str, str, str]]:
    """``(name, known_variant, canonical)``: entity names that are a known variant plus a qualifier.

    A known variant is any stored alias (``EntityAliases``) whose own entity no longer exists. The test
    is :func:`org_timeline.is_same_name` - one name's words contain the other's (legal forms aside) and
    the shorter still identifies something - so no word list or place list is involved.
    """
    from cyber_data_collector.dedup.entity_merge import distinctive_core
    from cyber_data_collector.dedup.entity_resolution import stored_aliases
    from cyber_data_collector.dedup.org_timeline import is_same_name

    aliases = stored_aliases(conn)
    entities = set(_organisation_names(conn))
    index: Dict[str, List[str]] = defaultdict(list)
    for raw in aliases:
        if raw in entities:
            continue
        for token in distinctive_core(raw):
            index[token].append(raw)
    found: List[Tuple[str, str, str]] = []
    for name in sorted(entities):
        if name in aliases:          # already a variant of something
            continue
        candidates: Set[str] = set()
        for token in distinctive_core(name):
            candidates.update(index.get(token, ()))
        for raw in sorted(candidates):
            # Not a known variant of this very name, and not already decided against it.
            if raw.lower() != name.lower() and aliases[raw] != name and is_same_name(name, raw):
                found.append((name, raw, aliases[raw]))
    return found


def coreference_proposals(conn: sqlite3.Connection) -> List[Tuple[str, str]]:
    """Pairs of victim names two records of one active event each give, as ``(a, b)`` with ``a < b``.

    Only records that name exactly one victim count, and only events whose single-victim records give
    between two and :data:`MAX_COREFERENT_NAMES` distinct names. Both must be entities, not descriptions.
    """
    ok = set(_organisation_names(conn))
    per_record: Dict[str, Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
    for event_id, record_id, name in conn.execute(
            """SELECT m.deduplicated_event_id, ee.enriched_event_id, v.entity_name
               FROM EventDeduplicationMap m
               JOIN DeduplicatedEvents d ON d.deduplicated_event_id = m.deduplicated_event_id AND d.status = 'Active'
               JOIN EnrichedEventEntities ee ON ee.enriched_event_id = m.enriched_event_id
                    AND ee.relationship_type = 'victim'
               JOIN EntitiesV2 v ON v.entity_id = ee.entity_id"""):
        per_record[event_id][record_id].add(name)
    pairs: Set[Tuple[str, str]] = set()
    for records in per_record.values():
        names = sorted({next(iter(s)) for s in records.values() if len(s) == 1} & ok)
        if 2 <= len(names) <= MAX_COREFERENT_NAMES:
            pairs.update(itertools.combinations(names, 2))
    return sorted(pairs)


def _abbreviations(name: str) -> List[str]:
    """Tokens of ``name`` written as an abbreviation (two or more capitals making up at least half the letters:
    "NAB", "RMIT", "PwC", "CS"; not a camel-cased brand such as "ActionAid"), as the lower-case letters of their
    capitals."""
    import re

    return [("".join(ch for ch in tok if ch.isupper())).lower() for tok in re.findall(r"[A-Za-z&]+", name or "")
            if sum(ch.isupper() for ch in tok) >= 2 and 2 * sum(ch.isupper() for ch in tok) >= len(tok)]


def _initials(name: str) -> str:
    """The initial of every word of ``name`` and of every capitalised part inside one ("PricewaterhouseCoopers"
    gives p, c), lower case."""
    import re

    out = []
    for word in re.findall(r"[A-Za-z]+", name or ""):
        parts = re.findall(r"[A-Z][a-z]*|[a-z]+", word) or [word]
        out.extend(p[0].lower() for p in parts)
    return "".join(out)


def abbreviation_fits(short: str, long: str) -> bool:
    """One of ``short``'s abbreviations is the initials of ``long`` taken in order from its first word
    ("ANZ" / "Australia and New Zealand Banking Group", "RMIT University" / "Royal Melbourne Institute of
    Technology"). A proposal test only: the verification decides."""
    initials = _initials(long)
    for abbr in _abbreviations(short):
        if len(abbr) < 2 or not initials or abbr[0] != initials[0]:
            continue
        it = iter(initials)
        if all(ch in it for ch in abbr):
            return True
    return False


def supplier_pair_proposals(conn: sqlite3.Connection) -> List[Tuple[str, str]]:
    """Pairs of names listed under one supplier incident - two customers, or the supplier and a customer - that may
    be one organisation (or one a part of the other), as ``(a, b)`` with ``a < b``.

    The owner's review of the supplier report (2026-10-31) found one customer under two names in one incident
    (NAB and "National Australia Bank (NAB)", "RMIT University" and "Royal Melbourne Institute of Technology",
    "Ministry of Health" and NSW Health), the ACT Health Directorate beside the ACT Government whose gateway was
    breached, and the supplier as its own customer ("PricewaterhouseCoopers (PwC)" under PwC). Nothing compared
    them: no structure rule paired them across the whole table. Within one incident they are proposed when they
    share a distinctive word, one abbreviates the other, or they share a word and their profiles agree
    (:func:`entity_profiles.agreement`). Only a proposal: the verification decides.
    """
    from cyber_data_collector.dedup.entity_merge import _PLACE_TOKENS, _tokens, distinctive_core
    from cyber_data_collector.dedup.entity_profiles import domain_of, profile_for

    def profiles_place_together(a: str, b: str) -> bool:
        # The same website, or the same jurisdiction below country level ("Australia, New South Wales"): two
        # national charities both "Australia" are not thereby one organisation.
        pa, pb = profile_for(conn, a), profile_for(conn, b)
        if domain_of(pa) and domain_of(pa) == domain_of(pb):
            return True
        ja, jb = (pa.get("jurisdiction") or [None])[0], (pb.get("jurisdiction") or [None])[0]
        return bool(ja and ja == jb and "," in ja)

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {"SupplierIncidents", "SupplierIncidentLinks"} <= tables:
        return []
    names_of: Dict[str, Set[str]] = defaultdict(set)
    for sid, vendor in conn.execute("SELECT supplier_incident_id, supplier_name FROM SupplierIncidents"):
        names_of[sid].add(vendor)
    for sid, name in conn.execute("SELECT supplier_incident_id, customer_name FROM SupplierIncidentLinks"):
        if name:
            names_of[sid].add(name)
    pairs: Set[Tuple[str, str]] = set()
    for names in names_of.values():
        for a, b in itertools.combinations(sorted(names), 2):
            if a.lower() == b.lower():
                continue
            if (distinctive_core(a) & distinctive_core(b) or abbreviation_fits(a, b) or abbreviation_fits(b, a)
                    or ((set(_tokens(a)) & set(_tokens(b))) - set(_PLACE_TOKENS)
                        and profiles_place_together(a, b))):
                pairs.add((a, b))
    return sorted(pairs)


def verify_supplier_pairs(conn: sqlite3.Connection, search: Optional[eav.Search] = None,
                          report: Optional[eav.VerificationReport] = None) -> eav.VerificationReport:
    """Ask the alias verification about every :func:`supplier_pair_proposals` pair (stored verdicts reused).

    Folds nothing. A verdict here was asked with the incident's context and holds for the names as listed in that
    incident: the supplier report reads them to count one customer once (:mod:`multi_customer_incidents`). A
    global fold would carry a generic name ("Ministry of Health", "Department of Education") into every other
    incident, so none is made from these pairs.
    """
    search = search or eav.perplexity_search
    report = report if report is not None else eav.VerificationReport()
    eav.ensure_schema(conn)
    known = eav.stored_verdicts(conn)
    for a, b in supplier_pair_proposals(conn):
        eav.verify_pair(conn, a, b, search, known, report)
    return report


def score(conn: sqlite3.Connection, a: str, b: str) -> int:
    """How likely a proposed pair is to be one organisation, for deciding which to ask about first.

    The distinctive name tokens the two share (each worth 1) plus what their profiles say: the same
    official website (+2), the same (+1) or a different (-1) jurisdiction (:func:`entity_profiles.agreement`).
    Only an ordering; the verification decides.
    """
    from cyber_data_collector.dedup.entity_merge import distinctive_core
    from cyber_data_collector.dedup.entity_profiles import agreement, profile_for

    return len(distinctive_core(a) & distinctive_core(b)) + agreement(profile_for(conn, a), profile_for(conn, b))


def rank(conn: sqlite3.Connection, proposals: Sequence, names) -> List:
    """``proposals`` best-supported first (ties keep their order); ``names(p)`` gives the two names to compare."""
    return sorted(proposals, key=lambda p: -score(conn, *names(p)))


def learn(conn: sqlite3.Connection, search: Optional[eav.Search] = None,
          report: Optional[eav.VerificationReport] = None,
          limit: Optional[int] = None) -> List[Tuple[str, List[str]]]:
    """Verify the evidence-based proposals; the folds Perplexity confirms as ``(keep, [names])`` groups.

    The groups are applied by the caller's fold step, which also stores the aliases. Raises
    :class:`entity_alias_verification.AliasVerificationUnavailable` without a Perplexity key; a pair the
    search could not answer is in ``report.failed`` and retried next run.
    """
    search = search or eav.perplexity_search
    report = report if report is not None else eav.VerificationReport()
    eav.ensure_schema(conn)
    known = eav.stored_verdicts(conn)
    entities = set(_organisation_names(conn))
    confirmed: List[Tuple[str, List[str]]] = []

    variants = rank(conn, variant_proposals(conn), lambda p: (p[0], p[1]))[: limit or None]
    for name, variant, canonical in variants:
        if canonical not in entities:
            logger.info("Alias %r -> %r not proposed: %r is not an entity", name, canonical, canonical)
            continue
        if eav.verify_pair(conn, name, variant, search, known, report) == "same":
            confirmed.append((canonical, [name]))
            report.accepted.append((name, canonical))
        else:
            verdict = known.get(eav._pair(name, variant), {}).get("verdict")
            if verdict:
                report.rejected.append((name, variant, verdict))

    pairs = rank(conn, coreference_proposals(conn), lambda p: p)[: limit or None]
    from cyber_data_collector.dedup.entity_merge import preferred_name

    for a, b in pairs:
        keep = preferred_name([a, b], {})
        drop = b if keep == a else a
        if eav.verify_pair(conn, keep, drop, search, known, report) == "same":
            confirmed.append((keep, [drop]))
            report.accepted.append((drop, keep))
        else:
            verdict = known.get(eav._pair(a, b), {}).get("verdict")
            if verdict:
                report.rejected.append((drop, keep, verdict))
    logger.info("Alias learning: %d variant proposal(s), %d co-reference pair(s), %d fold(s) confirmed",
                len(variants), len(pairs), len(confirmed))
    return confirmed


# ---------------------------------------------------------------- products named after an organisation

def product_hosts(conn: sqlite3.Connection, dedup_id: str) -> List[Tuple[str, str]]:
    """``(product, organisation)``: organisations a product named in the event is named after.

    A product or system the records link to ("Medicare statistics portal", kind ``product``) belongs to
    the organisation whose name it carries, found among the organisation entities and the known
    variants (stored aliases, resolved to their canonical name). The product is never folded into the
    organisation; the organisation is offered as a victim candidate and Jev or the attribution
    judgement decides. A match needs the organisation's whole name inside the product's words and a
    name that identifies something on its own (:func:`org_timeline.is_same_name`).
    """
    from cyber_data_collector.dedup.entity_merge import distinctive_core
    from cyber_data_collector.dedup.entity_resolution import stored_aliases
    from cyber_data_collector.dedup.org_timeline import _name_tokens

    products = [r[0] for r in conn.execute(
        """SELECT DISTINCT v.entity_name FROM EventDeduplicationMap m
           JOIN EnrichedEventEntities ee ON ee.enriched_event_id = m.enriched_event_id
           JOIN EntitiesV2 v ON v.entity_id = ee.entity_id
           WHERE m.deduplicated_event_id = ?
             AND (v.entity_kind = 'product' OR ee.relationship_type = 'product')""", (dedup_id,))]
    if not products:
        return []
    aliases = stored_aliases(conn)
    pool: Dict[str, str] = {name: name for name in _organisation_names(conn)}
    for raw, canonical in aliases.items():
        pool.setdefault(raw, canonical)
    found: List[Tuple[str, str]] = []
    for product in products:
        words = _name_tokens(product)
        for name, organisation in pool.items():
            tokens = _name_tokens(name)
            if tokens and tokens < words and distinctive_core(name) and organisation != product:
                pair = (product, organisation)
                if pair not in found:
                    found.append(pair)
    # A profile that says the organisation runs this service ("runs: Medicare Statistics Reporting Service").
    from cyber_data_collector.dedup.org_timeline import is_same_name

    try:
        runs = conn.execute("SELECT v.entity_name, p.value FROM EntityProfiles p "
                            "JOIN EntitiesV2 v ON v.entity_id = p.entity_id WHERE p.kind = 'runs'").fetchall()
    except sqlite3.OperationalError:
        runs = []
    for organisation, service in runs:
        for product in products:
            pair = (product, organisation)
            if pair not in found and is_same_name(product, service):
                found.append(pair)
    return found
