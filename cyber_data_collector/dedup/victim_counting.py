"""Count an incident once for each victim organisation - and never twice for one organisation.

A supplier breach has several co-equal victims (ProctorU: ten universities),
stored as several ``victim`` roles on one event. Everything that counts events
per organisation - the repeat-timing analysis and the by-entity dashboard -
reads those roles, so a multi-victim incident counts once for each victim.

The opposite error is counting one organisation twice: the same organisation
recorded as a victim under two name variants ("Optus" and "Singtel Optus Pty
Limited"; a learned alias), either on one event - which would list the
incident twice under it - or on two events, which would hide a genuine repeat
by splitting the organisation in two. :func:`organisation_groups` joins
variants with the project's strict test,
:meth:`EntityResolver.same_organisation` (aliases first), so callers can key
on one representative entity per organisation and drop duplicate
(organisation, event) rows.

**Acronyms are candidates, not proof.** "AP" is Advance Press in one record and
Associated Press in another, and ``AP`` is the initialism of both, so the
structural test that joins "FRV" to "Fire Rescue Victoria" would join it to
either, collapsing victims that are different organisations - inside an event
and across the recurrence counts. :class:`AcronymPolicy` therefore accepts an
acronym as the organisation only through an established identifier: a
reviewed or model-confirmed global alias (``EntityAliases``,
``EXPLICIT_ALIASES``), or - inside one event - a stored resolution of that
very event (``EntityMentionResolutions``). An acronym that fits several
organisations, or is registered as a contextual alias
(``ContextualEntityAliases``), and has neither stays unresolved and distinct.
An acronym that fits exactly one organisation and conflicts with no alias
keeps resolving without a model call.

**Unresolved attributions.** When the calibrated victim-attribution step
scores a recorded victim as probably wrong but no co-victim or calibrated
replacement can take its place, it flags the pair and leaves the role
(:func:`victim_judgement.unresolved_attributions`). Such an incident is real
and still counted, but under an organisation it may not have happened to.
:func:`unresolved_rows` marks those rows so callers can show them as
:data:`ATTRIBUTION_UNRESOLVED` and report the analysis with and without them.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

from cyber_data_collector.dedup.entity_resolution import EntityResolver, canonical_key


def _resolver(conn: sqlite3.Connection) -> EntityResolver:
    resolver = EntityResolver(conn)
    try:
        names = [r[0] for r in conn.execute("SELECT entity_name FROM EntitiesV2")]
    except sqlite3.Error:
        names = []
    # Fitted on every entity name, so the one-distinctive-word rule ("Qantas"
    # vs "Qantas Airways") only fires for words that really are distinctive.
    resolver.fit(names)
    return with_acronym_policy(conn, resolver)


class AcronymPolicy:
    """When may an acronym be treated as the organisation it abbreviates?

    ``entity_merge.is_acronym_of`` says only that an acronym *fits* a name.
    Built from the entity names (to see every organisation an acronym fits),
    the trusted global aliases and the contextual aliases, this decides
    whether the fit is identity:

    1. A trusted global alias (reviewed, or confirmed by Perplexity) settles
       it: the acronym is the organisation its alias names, and the name it is
       compared with is that organisation or is not.
    2. A registered contextual alias (``ContextualEntityAliases``) is
       ambiguous by definition - the design is that such an acronym is placed
       one record at a time - so the fit is not identity.
    3. An acronym that fits more than one distinct organisation is ambiguous.
    4. Otherwise the acronym fits exactly one organisation and nothing
       contradicts it: identity, as before.

    Event-level context (a stored resolution of this very event) is applied by
    :func:`within_event_duplicates`.
    """

    def __init__(self, names: Iterable[str], trusted: Optional[Dict[str, str]] = None,
                 contextual: Optional[Dict[str, List[str]]] = None):
        from cyber_data_collector.dedup.entity_merge import acronym_forms

        self.trusted: Dict[str, str] = dict(trusted or {})
        self.contextual: Dict[str, List[str]] = dict(contextual or {})
        self._by_form: Dict[str, List[str]] = defaultdict(list)
        for name in {n for n in names if n}:
            for form in acronym_forms(name):
                self._by_form[form].append(name)
        self._cache: Dict[Tuple[str, str], bool] = {}

    @classmethod
    def from_connection(cls, conn: sqlite3.Connection) -> "AcronymPolicy":
        from cyber_data_collector.dedup.contextual_aliases import contextual_aliases
        from cyber_data_collector.dedup.entity_merge import EXPLICIT_ALIASES
        from cyber_data_collector.dedup.entity_resolution import stored_aliases

        try:
            names = [r[0] for r in conn.execute("SELECT entity_name FROM EntitiesV2")]
        except sqlite3.Error:
            names = []
        try:
            contextual = contextual_aliases(conn)
        except sqlite3.Error:
            contextual = {}
        return cls(names, {**stored_aliases(conn), **EXPLICIT_ALIASES}, contextual)

    @staticmethod
    def relation(left: Optional[str], right: Optional[str]) -> Optional[Tuple[str, str]]:
        """``(acronym, longer name)`` when one name is the initialism of the other."""
        from cyber_data_collector.dedup.entity_merge import is_acronym_of

        if not left or not right:
            return None
        if is_acronym_of(left, right):
            return left, right
        if is_acronym_of(right, left):
            return right, left
        return None

    @staticmethod
    def _same_without_acronyms(left: str, right: str) -> bool:
        from cyber_data_collector.dedup.entity_merge import same_organisation_by_structure

        if canonical_key(left) and canonical_key(left) == canonical_key(right):
            return True
        return same_organisation_by_structure(left, right, acronyms=False)

    def organisations_fitting(self, short: str) -> List[str]:
        """One name per distinct organisation the acronym fits (stored names and contextual candidates)."""
        letters = "".join(t for t in re.split(r"[^a-z0-9]+", short.lower()) if t)
        fits = list(self._by_form.get(letters, ())) + list(self.contextual.get(short.strip(), ()))
        reps: List[str] = []
        for name in sorted(set(fits)):
            if not any(self._same_without_acronyms(name, rep) for rep in reps):
                reps.append(name)
        return reps

    def is_identity(self, short: str, long: str) -> bool:
        """May the fit of ``short`` to ``long`` be treated as one organisation?"""
        key = (short, long)
        if key not in self._cache:
            self._cache[key] = self._decide(short, long)
        return self._cache[key]

    def _decide(self, short: str, long: str) -> bool:
        alias = self.trusted.get(short.strip())
        if alias is not None:
            return self._same_without_acronyms(alias, long) or canonical_key(alias) == canonical_key(long)
        if short.strip() in self.contextual:
            return False
        return len(self.organisations_fitting(short)) <= 1


def with_acronym_policy(conn: sqlite3.Connection, resolver: EntityResolver) -> EntityResolver:
    """Attach (once) the :class:`AcronymPolicy` of ``conn`` to ``resolver``; returns it."""
    if getattr(resolver, "acronym_policy", None) is None:
        resolver.acronym_policy = AcronymPolicy.from_connection(conn)
    return resolver


_IGNORED = {"the", "pty", "ltd", "limited", "inc", "incorporated", "llc", "plc", "corp", "corporation", "co"}


def same_organisation(resolver: EntityResolver, left: Optional[str], right: Optional[str]) -> bool:
    """Do two victim names denote one organisation? Strict, because a false join loses a victim.

    Same when they share a canonical key or a learned alias, have the same
    words apart from "the" and legal forms, or pass the canonicaliser's
    structural test (:func:`entity_merge.same_organisation_by_structure`).
    An acronym and the name it abbreviates are the same only when
    :class:`AcronymPolicy` (attached to ``resolver``) accepts the fit; a
    resolver without a policy accepts none.
    Otherwise :meth:`EntityResolver.same_organisation` must hold **and** the
    shorter name must keep an identifying word once place, portfolio and
    institution words are removed (:func:`entity_merge.distinctive_core`),
    that core must lead the longer name's core, and the two must not name
    different lines of business. On the live victims (2026-10-27) the resolver
    alone joined Service NSW with the NSW Government, Football Australia with
    Fremantle Football Club, Spirit Super with Super SA and Sharp Motor Group
    with Sharp Office Systems; these guards keep Medibank / Medibank Private
    Limited, Meta / Meta Platforms, Inc. and BlueScope / BlueScope Steel.
    """
    from cyber_data_collector.dedup.entity_merge import (
        _PLACE_TOKENS, _tokens, distinctive_core, lines_of_business_conflict, same_organisation_by_structure,
    )

    if not left or not right:
        return False
    if resolver.key_for(left) and resolver.key_for(left) == resolver.key_for(right):
        return True
    words_l = [t for t in _tokens(left) if t not in _IGNORED]
    words_r = [t for t in _tokens(right) if t not in _IGNORED]
    if words_l and words_l == words_r:
        return True
    if same_organisation_by_structure(left, right, acronyms=False):
        return True
    fit = AcronymPolicy.relation(left, right)
    if fit is not None:
        # An acronym fits, which is a candidate and not identity; see AcronymPolicy.
        policy = getattr(resolver, "acronym_policy", None)
        if policy is not None and policy.is_identity(*fit):
            return True
    if not resolver.same_organisation(left, right) or lines_of_business_conflict(left, right):
        return False
    # A place word in one name only scopes it to a jurisdiction the other does
    # not name ("Super SA" - South Australia's fund - is not "Super VPN").
    if {t for t in _tokens(left) if t in _PLACE_TOKENS} ^ {t for t in _tokens(right) if t in _PLACE_TOKENS}:
        return False
    short, long = sorted((left, right), key=lambda n: (len(distinctive_core(n)), len(n)))
    core_short = distinctive_core(short)
    if not core_short:
        return False
    ordered_long = [t for t in _tokens(long) if t in distinctive_core(long)]
    return set(ordered_long[:len(core_short)]) == set(core_short)


def same_within_event(resolver: EntityResolver, left: Optional[str], right: Optional[str]) -> bool:
    """Are two victims *of one event* one organisation? Looser than :func:`same_organisation`.

    Inside one incident two names sharing their identifying words are one
    organisation far more often than not: "Australian Red Cross" / "Australian
    Red Cross Society", "Block (ASX:SQ2)" / "Block, Inc. (Cash App Investing)".
    Same when the strict test holds, or when - bracketed text removed - one
    name's identifying words (:func:`entity_merge.distinctive_core`) are a
    subset of the other's and the extra words name no place, jurisdiction or
    other line of business ("International Committee of the Red Cross" stays
    apart from the Australian Red Cross).
    """
    import re

    from cyber_data_collector.dedup.entity_merge import (
        _JURISDICTION_BLOCKERS, _PLACE_TOKENS, _tokens, distinctive_core, lines_of_business_conflict,
    )

    if same_organisation(resolver, left, right):
        return True
    if not left or not right:
        return False
    a, b = (re.sub(r"\([^)]*\)", " ", n).strip() for n in (left, right))
    core_a, core_b = distinctive_core(a), distinctive_core(b)
    if not core_a or not core_b or not (core_a <= core_b or core_b <= core_a):
        return False
    extra = set(_tokens(a)) ^ set(_tokens(b))
    if extra & (_PLACE_TOKENS | _JURISDICTION_BLOCKERS) - {"australian", "australia"}:
        return False
    return not lines_of_business_conflict(a, b)


def organisation_groups(conn: sqlite3.Connection, entity_ids: Iterable[int],
                        resolver: Optional[EntityResolver] = None) -> Dict[int, int]:
    """Map each entity id to one representative id per organisation.

    The representative is the variant with the most victim roles on active
    events, then the lowest id, so the choice is stable between runs.
    """
    ids = sorted({int(i) for i in entity_ids if i is not None})
    if not ids:
        return {}
    resolver = with_acronym_policy(conn, resolver) if resolver else _resolver(conn)
    names: Dict[int, str] = {}
    for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
        for eid, name in conn.execute(
                f"SELECT entity_id, entity_name FROM EntitiesV2 WHERE entity_id IN ({','.join('?' * len(chunk))})",
                chunk):
            names[int(eid)] = name or ""
    weight: Dict[int, int] = defaultdict(int)
    try:
        for eid, n in conn.execute(
                """SELECT dee.entity_id, COUNT(*) FROM DeduplicatedEventEntities dee
                   JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id
                   WHERE dee.relationship_type = 'victim' AND COALESCE(d.status, 'Active') = 'Active'
                   GROUP BY dee.entity_id"""):
            weight[int(eid)] = n
    except sqlite3.Error:
        pass

    parent = {i: i for i in ids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    buckets: Dict[str, List[int]] = defaultdict(list)
    for eid in ids:
        name = names.get(eid, "")
        keys = {"key:" + resolver.key_for(name)} | {"block:" + b for b in resolver.blocks_for(name)}
        for key in keys:
            if key not in ("key:", "block:"):
                buckets[key].append(eid)
    same: Dict[int, set] = defaultdict(set)
    for members in buckets.values():
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if b not in same[a] and same_organisation(resolver, names.get(a), names.get(b)):
                    same[a].add(b)
                    same[b].add(a)
    # Complete linkage: a variant joins a group only when it is the same
    # organisation as every member, so "Federal Group" cannot chain "Federal
    # Court" to the Australian Federal Police.
    for a in ids:
        for b in sorted(same[a]):
            ra, rb = find(a), find(b)
            if ra == rb:
                continue
            group_a = [x for x in ids if find(x) == ra]
            group_b = [x for x in ids if find(x) == rb]
            if all(y in same[x] for x in group_a for y in group_b):
                parent[ra] = rb

    groups: Dict[int, List[int]] = defaultdict(list)
    for eid in ids:
        groups[find(eid)].append(eid)
    out: Dict[int, int] = {}
    for members in groups.values():
        rep = min(members, key=lambda e: (-weight.get(e, 0), e))
        for eid in members:
            out[eid] = rep
    return out


def _resolved_acronyms(conn: sqlite3.Connection, by_event: Dict[str, list]) -> Dict[str, List[Tuple[str, str]]]:
    """``{event id: [(acronym, organisation it was resolved to)]}`` from the event's stored resolutions.

    Only decisions about the event's own victim links count
    (``EntityMentionResolutions``, link table ``DeduplicatedEventEntities``,
    subject = the event) and only those that name an organisation: an
    unresolved mention is not evidence.
    """
    out: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    ids = sorted(by_event)
    try:
        for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
            for subject, raw, canonical in conn.execute(
                    "SELECT subject_id, raw_name, canonical_name FROM EntityMentionResolutions "
                    "WHERE link_table = 'DeduplicatedEventEntities' AND canonical_name IS NOT NULL "
                    f"AND subject_id IN ({','.join('?' * len(chunk))})", chunk):
                out[subject].append((raw, canonical))
    except sqlite3.Error:
        pass
    return out


def _resolved_to(resolver: EntityResolver, resolutions: Iterable[Tuple[str, str]],
                 left: str, right: str) -> bool:
    """Did this event's own context resolve the acronym in one name to the organisation in the other?"""
    fit = AcronymPolicy.relation(left, right)
    if fit is None:
        return False
    short, long = fit
    return any(raw == short and (canonical == long or same_organisation(resolver, canonical, long))
               for raw, canonical in resolutions)


def within_event_duplicates(conn: sqlite3.Connection, rows: Iterable[Tuple[int, str, str]],
                            resolver: Optional[EntityResolver] = None) -> set:
    """``(entity id, event id)`` rows that repeat another victim of the same event under another name.

    ``rows`` are ``(entity id, entity name, event id)``. Within each event the
    variant with most victim roles overall is kept (then the lowest id).
    """
    resolver = with_acronym_policy(conn, resolver) if resolver else _resolver(conn)
    weight: Dict[int, int] = defaultdict(int)
    try:
        for eid, n in conn.execute(
                "SELECT entity_id, COUNT(*) FROM DeduplicatedEventEntities WHERE relationship_type = 'victim' "
                "GROUP BY entity_id"):
            weight[int(eid)] = n
    except sqlite3.Error:
        pass
    by_event: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
    for eid, name, did in rows:
        if (int(eid), name) not in by_event[did]:
            by_event[did].append((int(eid), name))
    resolved = _resolved_acronyms(conn, by_event)
    drop = set()
    for did, victims in by_event.items():
        kept: List[Tuple[int, str]] = []
        for eid, name in sorted(victims, key=lambda v: (-weight.get(v[0], 0), v[0])):
            if any(k[0] == eid or same_within_event(resolver, k[1], name)
                   or _resolved_to(resolver, resolved.get(did, ()), k[1], name) for k in kept):
                if not any(k[0] == eid for k in kept):
                    drop.add((eid, did))
                continue
            kept.append((eid, name))
    return drop


ATTRIBUTION_UNRESOLVED = "attribution unresolved"


def unresolved_attributions(conn: sqlite3.Connection) -> Dict[Tuple[str, int], Dict[str, object]]:
    """Flagged active victim pairs, keyed ``(event id, entity id)``; see :mod:`victim_judgement`."""
    from cyber_data_collector.dedup.victim_judgement import unresolved_attributions as flagged
    return flagged(conn)


def unresolved_rows(conn: sqlite3.Connection, rows: Iterable[Tuple[int, str, str]], drop: set,
                    resolver: Optional[EntityResolver] = None,
                    flagged: Optional[Dict[Tuple[str, int], Dict[str, object]]] = None) -> Dict[Tuple[int, str], str]:
    """Kept ``(entity id, event id)`` victim rows whose attribution is unresolved, with the flag's reason.

    ``rows`` are ``(entity id, entity name, event id)`` before
    :func:`within_event_duplicates` removed ``drop``. A kept row is unresolved
    when its own pair is flagged, or when it stands for a flagged variant of
    the same organisation dropped from that event.
    """
    flagged = unresolved_attributions(conn) if flagged is None else flagged
    if not flagged:
        return {}
    rows = list(rows)
    names = {(int(eid), did): name for eid, name, did in rows}
    out: Dict[Tuple[int, str], str] = {}
    for eid, name, did in rows:
        key = (int(eid), did)
        if key in drop:
            continue
        flag = flagged.get((did, int(eid)))
        if flag is None:
            for (other, other_did) in drop:
                if other_did == did and (did, other) in flagged:
                    resolver = with_acronym_policy(conn, resolver) if resolver else _resolver(conn)
                    if same_within_event(resolver, name, names.get((other, did))):
                        flag = flagged[(did, other)]
                        break
        if flag is not None:
            out[key] = str(flag.get("reason") or "flagged by victim attribution")
    return out


def one_row_per_organisation(rows: Iterable[Tuple[int, str]],
                             groups: Dict[int, int]) -> List[Tuple[int, str]]:
    """``(entity id, event id)`` pairs reduced to one per (organisation, event), in order."""
    seen = set()
    out = []
    for eid, did in rows:
        key = (groups.get(eid, eid), did)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out
