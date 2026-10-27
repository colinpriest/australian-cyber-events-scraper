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
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

from cyber_data_collector.dedup.entity_resolution import EntityResolver


def _resolver(conn: sqlite3.Connection) -> EntityResolver:
    resolver = EntityResolver(conn)
    try:
        names = [r[0] for r in conn.execute("SELECT entity_name FROM EntitiesV2")]
    except sqlite3.Error:
        names = []
    # Fitted on every entity name, so the one-distinctive-word rule ("Qantas"
    # vs "Qantas Airways") only fires for words that really are distinctive.
    resolver.fit(names)
    return resolver


_IGNORED = {"the", "pty", "ltd", "limited", "inc", "incorporated", "llc", "plc", "corp", "corporation", "co"}


def same_organisation(resolver: EntityResolver, left: Optional[str], right: Optional[str]) -> bool:
    """Do two victim names denote one organisation? Strict, because a false join loses a victim.

    Same when they share a canonical key or a learned alias, have the same
    words apart from "the" and legal forms, or pass the canonicaliser's
    structural test (:func:`entity_merge.same_organisation_by_structure`).
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
    if same_organisation_by_structure(left, right):
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
    resolver = resolver or _resolver(conn)
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


def within_event_duplicates(conn: sqlite3.Connection, rows: Iterable[Tuple[int, str, str]],
                            resolver: Optional[EntityResolver] = None) -> set:
    """``(entity id, event id)`` rows that repeat another victim of the same event under another name.

    ``rows`` are ``(entity id, entity name, event id)``. Within each event the
    variant with most victim roles overall is kept (then the lowest id).
    """
    resolver = resolver or _resolver(conn)
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
    drop = set()
    for did, victims in by_event.items():
        kept: List[Tuple[int, str]] = []
        for eid, name in sorted(victims, key=lambda v: (-weight.get(v[0], 0), v[0])):
            if any(k[0] == eid or same_within_event(resolver, k[1], name) for k in kept):
                if not any(k[0] == eid for k in kept):
                    drop.add((eid, did))
                continue
            kept.append((eid, name))
    return drop


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
