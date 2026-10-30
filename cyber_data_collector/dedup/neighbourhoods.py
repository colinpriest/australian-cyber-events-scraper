"""Match across large incident groups with bounded, overlapping neighbourhoods.

**The problem.** Whole-group adjudication (``cluster_adjudicator``) shows the
model every record of a group at once, which is what lets it see one incident
behind a long tail of coverage. But a group can only be so big (the prompt
loses track of indices past about 20 records), and the two ways of handling a
bigger one both lose incidents:

* a bucket of more than twice the limit was skipped outright;
* anything between was cut into **disjoint slices** (``candidate_components``
  refuses a union that would pass the size cap; ``_adjudicate_chunked`` cuts
  every 20 records), so two reports of one supplier incident that land on
  opposite sides of a cut are never in front of the model together. A supplier
  breach reported once per customer is exactly the shape that is large, spread
  over weeks, and chained through shared articles and rare words.

**What this does instead.**

1. *Link* events by the evidence that they are about one incident, using the
   same scoring that ranks duplicate candidates
   (:func:`candidate_ranking.score_pair`): a shared non-hub source article, the
   same supplier, corpus-rare words they share, an equal record count or attack
   method, and how close their date intervals are.
2. *Plan neighbourhoods* (:func:`plan`): a connected group of at most
   ``max_size`` events is one neighbourhood, exactly as before. A larger group
   is covered by overlapping neighbourhoods, each an event and its strongest
   ``neighbours`` links, chosen greedily so that every strong link is inside at
   least one neighbourhood that is shown to the model together. Nothing is
   skipped for size, and the number of neighbourhoods is reported.
3. *Ask* each neighbourhood for a partition (one model call each, answers
   stored by ``AdjudicationCache`` so a repeated run is free) within an
   explicit **call budget**: neighbourhoods are ordered by how many links they
   settle, the budget cuts the tail, and what is cut is counted and reported -
   the next run continues with cached answers free.
4. *Reconcile* the overlapping answers (:func:`reconcile`). Two neighbourhoods
   can put a shared event in different groups. A pair of events is joined only
   when (a) some neighbourhood showing both places them together at or above
   the certainty bar, (b) **no** neighbourhood showing both places them apart,
   (c) no ``different`` ruling or label blocks them. Incidents are then built
   strongest link first (most neighbourhoods agreeing, then highest certainty),
   and two partial incidents are unioned only when *no* cross pair is
   contradicted or blocked. A shared event between two disagreeing groups thus
   goes to the side that is consistent, or stays out of both; an inconsistent
   union is refused and counted. The certainty of a reconciled incident is the
   lowest certainty on the links that built it.

Nothing here is calibrated: a partition certainty is the model's own number,
and the callers keep their ``--min-certainty`` bar and report-only default. The
consistency rule decides what may be *unioned*; whether to merge is still the
caller's decision.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from cyber_data_collector.dedup import candidate_ranking as ranking
from cyber_data_collector.dedup.bounded_candidates import IntervalIndex, Span

logger = logging.getLogger(__name__)

#: Strongest links kept per event when a group is too large to show whole.
NEIGHBOURS_PER_EVENT = 8
#: Most events shown to the model together (matches ``cluster_adjudicator.MAX_CLUSTER_SIZE``).
MAX_NEIGHBOURHOOD_SIZE = 20
#: Model calls one run may make for neighbourhoods; cached answers are free and do not count.
CALL_BUDGET = 250
#: Floor of a link between pairs the caller already screened as candidates (see :func:`build_links`).
MIN_LINK_SCORE = 0.1
#: A neighbourhood answer joins two events only at or above this certainty.
LINK_MIN_CERTAINTY = 0.7

#: Reasons from :func:`candidate_ranking.score_pair` that are evidence of one incident (not just closeness).
IDENTITY_REASONS = ("shared source", "same supplier", "shared incident-specific words")

Pair = FrozenSet[str]
Blocked = Callable[[str, str], Optional[str]]


def pair_key(a: str, b: str) -> Pair:
    return frozenset((a, b))


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Link:
    """Evidence that two events are about one incident."""

    a: str
    b: str
    score: float
    reasons: Tuple[str, ...]

    @property
    def key(self) -> Pair:
        return pair_key(self.a, self.b)


Interval = Tuple[date, date]


def interval_gap(left: Optional[Interval], right: Optional[Interval]) -> Optional[int]:
    """Days between two date intervals: 0 when they overlap, None when either is unknown."""
    if left is None or right is None:
        return None
    if left[0] > right[1]:
        return (left[0] - right[1]).days
    if right[0] > left[1]:
        return (right[0] - left[1]).days
    return 0


def build_links(
    pairs: Iterable[Tuple[str, str]],
    corpus: ranking.Corpus,
    gap_of: Callable[[str, str], Optional[int]],
    min_score: float = 0.0,
    window_days: int = ranking.WINDOW_DAYS,
    bonus: Optional[Callable[[str, str], float]] = None,
    keep_all: bool = False,
    require_identity: bool = False,
) -> Dict[Pair, Link]:
    """Score ``pairs`` and keep those with evidence (score above ``min_score``).

    Uses :func:`candidate_ranking.score_pair`: shared source, same supplier,
    shared corpus-rare words, equal record count or attack method, and the
    closeness of the two events' dates (``gap_of`` gives the gap in days, 0 for
    overlapping intervals, None when a date is unknown). ``bonus`` adds further
    evidence the caller holds (text similarity) to the score. With ``keep_all``
    every pair is kept - the caller has already screened them as candidates
    (shared article, rare shared word, similar text) - at no less than
    :data:`MIN_LINK_SCORE`, so a candidate the ranking cannot add to still orders last. With
    ``require_identity`` a pair needs evidence that it is about one incident - a shared non-hub source, the same
    supplier or a shared corpus-rare word - and dates, record counts and attack method only order the
    neighbours; two events a few weeks apart are not linked on that alone.
    """
    links: Dict[Pair, Link] = {}
    for a, b in pairs:
        if a == b:
            continue
        gap = gap_of(a, b)
        score, reasons, _strong = ranking.score_pair(corpus, a, b, gap, window_days)
        extra = bonus(a, b) if bonus else 0.0
        if require_identity and not extra and not any(r.startswith(IDENTITY_REASONS) for r in reasons):
            continue
        if extra > 0:
            score += extra
            reasons = [*reasons, f"similar text ({extra:.2f})"]
        if keep_all:
            score = max(score, MIN_LINK_SCORE)
        if score > min_score:
            first, second = sorted((a, b))
            links[pair_key(a, b)] = Link(first, second, round(score, 4), tuple(reasons))
    return links


#: Most events of one supplier that are paired exhaustively; a supplier with more is subdivided by date.
MAX_SUPPLIER_GROUP = 100
#: In a subdivided supplier, the events each one is linked to on either side in date order.
SUPPLIER_NEIGHBOURS = NEIGHBOURS_PER_EVENT


def supplier_pairs(corpus: ranking.Corpus, intervals: Mapping[str, Optional[Interval]],
                   ids: Optional[Iterable[str]] = None, max_group: int = MAX_SUPPLIER_GROUP,
                   neighbours: int = SUPPLIER_NEIGHBOURS) -> Tuple[List[Tuple[str, str]], int]:
    """Pairs of events that record the same supplier: the supplier clue, which needs no shared article or word.

    A supplier breach reported once per customer names a different victim every time and may share no rare
    word, so the usual candidate keys never pair the reports. A supplier of at most ``max_group`` events gives
    every pair of them. A larger one is **subdivided**, not skipped: its events are ordered by date interval
    (``intervals``: an event's ``(earliest, latest)``, None or absent when undated) and each dated event is linked
    to the ``neighbours`` before and after it in start order and in end order (the interval index of
    :mod:`bounded_candidates`), each undated one to the ``neighbours`` before and after it in id order. Reports of
    one incident lie close in date, so they stay linked, directly or through each other, with O(n * neighbours)
    work. Returns ``(pairs, suppliers subdivided)``.
    """
    wanted = set(ids) if ids is not None else None
    by_vendor: Dict[str, List[str]] = defaultdict(list)
    for event_id, features in corpus.features.items():
        if features.vendor and (wanted is None or event_id in wanted):
            by_vendor[features.vendor].append(event_id)
    pairs: List[Tuple[str, str]] = []
    subdivided = 0
    for members in by_vendor.values():
        if len(members) < 2:
            continue
        ordered = sorted(members)
        if len(members) <= max_group:
            pairs.extend((a, b) for i, a in enumerate(ordered) for b in ordered[i + 1:])
            continue
        subdivided += 1
        spans = [Span(i, *(intervals.get(i) or (None, None))) for i in ordered]
        index = IntervalIndex(spans)
        linked: Set[Tuple[str, str]] = set()
        for position, span in enumerate(spans):
            partners = (index.adjacent(span, neighbours) if span.start is not None else
                        ordered[max(0, position - neighbours):position] + ordered[position + 1:position + 1 + neighbours])
            linked.update((min(span.event_id, other), max(span.event_id, other)) for other in partners)
        pairs.extend(sorted(linked))
    if subdivided:
        logger.info("Supplier links: %d supplier(s) with more than %d events subdivided by date", subdivided, max_group)
    return pairs, subdivided


def links_within(ids: Sequence[str], corpus: ranking.Corpus,
                 gap_of: Callable[[str, str], Optional[int]], **kwargs: Any) -> Dict[Pair, Link]:
    """Links among every pair of ``ids`` (a bucket that shares a blocking key already)."""
    ordered = sorted(ids)
    pairs = [(a, b) for i, a in enumerate(ordered) for b in ordered[i + 1:]]
    kwargs.setdefault("require_identity", True)
    return build_links(pairs, corpus, gap_of, **kwargs)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Neighbourhood:
    """Events shown to the model together."""

    centre: str
    members: Tuple[str, ...]
    gain: int                  # links this neighbourhood settles that no earlier one did
    is_whole_group: bool       # the group fits, so this is the group (no overlap needed)


@dataclass
class Plan:
    neighbourhoods: List[Neighbourhood] = field(default_factory=list)
    group_sizes: List[int] = field(default_factory=list)          # connected groups, largest first
    large_groups: int = 0                                          # groups above max_size
    events_in_large_groups: int = 0
    required_links: int = 0                                        # each event's strongest links (what the cover targets)
    covered_links: int = 0
    links_total: int = 0                                           # every link that has to be seen (all, or those touching the focus)
    links_shown: int = 0                                           # of those, inside at least one neighbourhood
    max_size: int = MAX_NEIGHBOURHOOD_SIZE
    neighbours: int = NEIGHBOURS_PER_EVENT

    def stats(self) -> Dict[str, Any]:
        sizes = [len(n.members) for n in self.neighbourhoods]
        return {
            "groups": len(self.group_sizes), "group_sizes_top": self.group_sizes[:10],
            "large_groups": self.large_groups, "events_in_large_groups": self.events_in_large_groups,
            "neighbourhoods": len(self.neighbourhoods),
            "whole_group_neighbourhoods": sum(1 for n in self.neighbourhoods if n.is_whole_group),
            "overlapping_neighbourhoods": sum(1 for n in self.neighbourhoods if not n.is_whole_group),
            "largest_neighbourhood": max(sizes, default=0),
            "required_links": self.required_links, "covered_links": self.covered_links,
            "links_total": self.links_total, "links_shown": self.links_shown,
            "max_size": self.max_size, "neighbours": self.neighbours,
        }


def _components(links: Dict[Pair, Link]) -> List[List[str]]:
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for key in links:
        a, b = sorted(key)
        parent[find(a)] = find(b)
    groups: Dict[str, List[str]] = defaultdict(list)
    for node in list(parent):
        groups[find(node)].append(node)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: (-len(g), g[0]))


def _grow(members: Set[str], adjacency: Dict[str, List[Tuple[float, str]]], max_size: int) -> Tuple[str, ...]:
    """Fill a neighbourhood up to ``max_size`` with the events most strongly linked to it.

    An event and its strongest links are the core; the room left is given to the events outside it with the
    highest total link strength to the members, best first, so one prompt settles as many links as it can hold.
    """
    members = set(members)
    while len(members) < max_size:
        strength: Dict[str, float] = defaultdict(float)
        for node in members:
            for score, other in adjacency[node]:
                if other not in members:
                    strength[other] += score
        if not strength:
            break
        members.add(max(strength, key=lambda n: (strength[n], n)))
    return tuple(sorted(members))


def plan(
    links: Dict[Pair, Link],
    *,
    max_size: int = MAX_NEIGHBOURHOOD_SIZE,
    neighbours: int = NEIGHBOURS_PER_EVENT,
    focus: Optional[Set[str]] = None,
) -> Plan:
    """Cover the linked groups with neighbourhoods of at most ``max_size`` events.

    Args:
        links: Evidence links (see :func:`build_links`).
        max_size: Most events shown together.
        neighbours: Strongest links kept per event in a group too large to show
            whole (capped at ``max_size - 1``).
        focus: When given, only links touching one of these events must be
            covered (and a whole group is kept only if it holds one) - the
            events changed recently.

    Returns:
        A :class:`Plan` whose neighbourhoods are ordered by how many links each
        settles, most first; ties by centre id, so the order is deterministic.
    """
    neighbours = max(1, min(int(neighbours), int(max_size) - 1))
    result = Plan(max_size=max_size, neighbours=neighbours)
    groups = _components(links)
    result.group_sizes = [len(g) for g in groups]
    adjacency: Dict[str, List[Tuple[float, str]]] = defaultdict(list)
    for link in links.values():
        adjacency[link.a].append((link.score, link.b))
        adjacency[link.b].append((link.score, link.a))
    for node in adjacency:
        adjacency[node].sort(key=lambda t: (-t[0], t[1]))

    def wanted(key: Pair) -> bool:
        return focus is None or bool(key & focus)

    group_index = {node: i for i, group in enumerate(groups) for node in group}
    links_of: Dict[int, List[Pair]] = defaultdict(list)
    for key in links:
        links_of[group_index[next(iter(key))]].append(key)

    picked: List[Neighbourhood] = []
    for index, group in enumerate(groups):
        required_here = [k for k in links_of[index] if wanted(k)]
        if not required_here:
            continue
        if len(group) <= max_size:
            result.required_links += len(required_here)
            result.covered_links += len(required_here)
            centre = max(group, key=lambda n: (sum(s for s, _ in adjacency[n]), n))
            picked.append(Neighbourhood(centre, tuple(group), len(required_here), True))
            continue
        result.large_groups += 1
        result.events_in_large_groups += len(group)
        # Required: each event's strongest links. Candidate neighbourhood: the event plus those neighbours,
        # grown with the events most strongly linked to them until the prompt is full.
        required: Set[Pair] = set()
        candidate: Dict[str, Tuple[str, ...]] = {}
        for node in group:
            top = [other for _, other in adjacency[node][:neighbours]]
            candidate[node] = _grow({node, *top}, adjacency, max_size)
            required.update(pair_key(node, other) for other in top if wanted(pair_key(node, other)))
        result.required_links += len(required)
        inside: Dict[str, Set[Pair]] = {
            node: {pair_key(a, b) for i, a in enumerate(members) for b in members[i + 1:]
                   if pair_key(a, b) in required}
            for node, members in candidate.items()}
        uncovered = set(required)
        while uncovered:
            best = max(candidate, key=lambda n: (len(inside[n] & uncovered),
                                                 sum(links[k].score for k in inside[n] & uncovered), n))
            gain = inside[best] & uncovered
            if not gain:
                break
            picked.append(Neighbourhood(best, candidate[best], len(gain), False))
            uncovered -= gain
        result.covered_links += len(required) - len(uncovered)
    seen: Set[Tuple[str, ...]] = set()
    for n in sorted(picked, key=lambda n: (-n.gain, n.centre)):
        if n.members in seen:
            continue
        seen.add(n.members)
        result.neighbourhoods.append(n)
    shown_in = [set(n.members) for n in result.neighbourhoods]
    for key in links:
        if wanted(key):
            result.links_total += 1
            result.links_shown += any(key <= members for members in shown_in)
    return result


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GroupAnswer:
    """One group of a neighbourhood's partition."""

    ids: Tuple[str, ...]
    certainty: float
    label: str = ""
    reasoning: str = ""


@dataclass(frozen=True)
class NeighbourhoodAnswer:
    """A valid partition of one neighbourhood (every member in exactly one group)."""

    members: Tuple[str, ...]
    groups: Tuple[GroupAnswer, ...]


@dataclass
class Incident:
    """Events reconciled into one incident."""

    ids: Tuple[str, ...]
    certainty: float                       # lowest certainty on the links that built it
    support: int                           # neighbourhoods that placed some pair of it together
    label: str
    reasoning: str
    representative: str                    # the member most strongly linked to the others


@dataclass
class Reconciliation:
    incidents: List[Incident] = field(default_factory=list)
    contradicted_links: List[Tuple[str, str, int, int]] = field(default_factory=list)   # a, b, together, apart
    blocked_links: List[Tuple[str, str, str]] = field(default_factory=list)             # a, b, why
    refused_unions: int = 0
    neighbourhoods: int = 0

    def stats(self) -> Dict[str, Any]:
        return {"neighbourhoods_answered": self.neighbourhoods, "incidents": len(self.incidents),
                "events_in_incidents": sum(len(i.ids) for i in self.incidents),
                "largest_incident": max((len(i.ids) for i in self.incidents), default=0),
                "contradicted_links": len(self.contradicted_links), "blocked_links": len(self.blocked_links),
                "refused_unions": self.refused_unions}


def reconcile(
    answers: Sequence[NeighbourhoodAnswer],
    *,
    min_certainty: float = LINK_MIN_CERTAINTY,
    blocked: Optional[Blocked] = None,
    links: Optional[Dict[Pair, Link]] = None,
) -> Reconciliation:
    """Join the overlapping answers into incidents, only where they are consistent.

    Args:
        answers: One valid partition per answered neighbourhood.
        min_certainty: A group places two events together only at or above this.
        blocked: ``(a, b) -> reason or None``; a pair it blocks (a ``different``
            ruling or label) is never joined, directly or through a third event.
        links: The evidence links, used to pick each incident's representative.

    Returns:
        The reconciled incidents (two or more events each), and what was refused.
    """
    together: Dict[Pair, List[float]] = defaultdict(list)
    apart: Dict[Pair, int] = defaultdict(int)
    group_of: Dict[Pair, GroupAnswer] = {}
    for answer in answers:
        placement: Dict[str, int] = {}
        for index, group in enumerate(answer.groups):
            for event in group.ids:
                placement[event] = index
        members = sorted(placement)
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                key = pair_key(a, b)
                if placement[a] == placement[b]:
                    group = answer.groups[placement[a]]
                    together[key].append(group.certainty)
                    best = group_of.get(key)
                    if best is None or group.certainty > best.certainty:
                        group_of[key] = group
                else:
                    apart[key] += 1

    result = Reconciliation(neighbourhoods=len(answers))
    block_memo: Dict[Pair, Optional[str]] = {}

    def block_reason(a: str, b: str) -> Optional[str]:
        key = pair_key(a, b)
        if key not in block_memo:
            block_memo[key] = blocked(a, b) if blocked else None
        return block_memo[key]

    candidates: List[Tuple[Pair, int, float]] = []
    for key, certainties in together.items():
        sure = [c for c in certainties if c >= min_certainty]
        if not sure:
            continue
        a, b = sorted(key)
        if apart.get(key, 0):
            result.contradicted_links.append((a, b, len(sure), apart[key]))
            continue
        reason = block_reason(a, b)
        if reason:
            result.blocked_links.append((a, b, reason))
            continue
        candidates.append((key, len(sure), min(sure)))
    candidates.sort(key=lambda c: (-c[1], -c[2], sorted(c[0])))

    parent: Dict[str, str] = {}
    members_of: Dict[str, Set[str]] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        members_of.setdefault(x, {x})
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    certainty_of: Dict[str, float] = {}
    support_of: Dict[str, int] = {}
    best_group: Dict[str, GroupAnswer] = {}

    def consistent(left: Set[str], right: Set[str]) -> bool:
        for x in left:
            for y in right:
                key = pair_key(x, y)
                if apart.get(key, 0) or block_reason(x, y):
                    return False
        return True

    for key, support, certainty in candidates:
        a, b = sorted(key)
        ra, rb = find(a), find(b)
        if ra == rb:
            continue
        if not consistent(members_of[ra], members_of[rb]):
            result.refused_unions += 1
            continue
        parent[ra] = rb
        members_of[rb] |= members_of.pop(ra)
        certainty_of[rb] = min(certainty, certainty_of.pop(ra, certainty), certainty_of.get(rb, certainty))
        support_of[rb] = max(support, support_of.pop(ra, 0), support_of.get(rb, 0))
        contenders = [g for g in (best_group.pop(ra, None), best_group.get(rb), group_of[key]) if g is not None]
        best_group[rb] = max(contenders, key=lambda g: (g.certainty, len(g.ids)))

    for root, members in members_of.items():
        if find(root) != root or len(members) < 2:
            continue
        ordered = tuple(sorted(members))
        strength = {m: sum(links[pair_key(m, o)].score for o in ordered if o != m and pair_key(m, o) in links)
                    for m in ordered} if links else {m: 0.0 for m in ordered}
        representative = max(ordered, key=lambda m: (strength[m], m))
        group = best_group.get(root)
        result.incidents.append(Incident(
            ids=ordered, certainty=certainty_of.get(root, 0.0), support=support_of.get(root, 0),
            label=group.label if group else "", reasoning=group.reasoning if group else "",
            representative=representative))
    result.incidents.sort(key=lambda i: (-len(i.ids), i.ids))
    return result
