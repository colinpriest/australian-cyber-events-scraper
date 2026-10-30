"""Rank a large group's duplicate candidates without building every pair of it.

:func:`candidate_ranking.select` ranks pairs it is given, so a caller has to
build every pair of a group first: a group of 3,000 events is 4.5 million pairs,
built, scored and sorted before the cap throws nearly all of them away. CPU and
memory grew with the square of the largest group even though the paid calls
stayed bounded. This module keeps that bound without the enumeration.

A group of at most ``EXHAUSTIVE_GROUP_MAX`` events is still ranked exactly as
:func:`candidate_ranking.select` does (at most 1,770 pairs built). A larger one
goes through :func:`select_bounded`, which never builds a pair it does not keep.
For each event it **retrieves** a limited candidate set through the same kinds
of evidence the score uses, then scores only those and keeps the best ``K``
(``partners_per_event``):

* the events citing the same non-hub source, naming the same vendor, sharing a
  corpus-rare word or the same records-affected figure - posting lists built
  once over the group. A posting of more than ``PER_KEY_LIMIT`` events (a
  vendor, a figure, a rare word many reports of a big group carry) is followed
  to its ``PER_KEY_LIMIT`` nearest in date **and** its ``PER_KEY_LIMIT`` undated
  members (smallest pair ids first), through an :class:`IntervalIndex` built
  once per posting - never by sorting the posting for every event that carries
  it. An event's keys are followed rarest first, and at most
  ``RETRIEVAL_BUDGET`` candidates are taken from them (counted:
  ``events_at_retrieval_budget``);
* the ``K`` events nearest in time on either side by date interval - events
  sorted by interval start and by interval end, walked with indexes. Right of
  an event in start order, and left of it in end order, the interval gap is
  monotone, so these are its nearest by date;
* the pairs an undated event has no date term for. The exhaustive ranking
  fills a place no evidence claims by pair id (an undated pair has no date
  score, so zero-evidence ties break on the id); the ``K`` smallest pair ids
  of an event's undated partners (of all partners, for an undated event) are
  found by ``bisect`` over the sorted ids, so the same pairs are retrieved.

The reserved route for strong long-gap or undated matches is fed from the same
source / supplier / rare-word postings, which are exactly what "strong" means,
so a strong match is retrieved however far apart in time, or undated, as long as
its posting is short (a non-hub source is cited by at most
``candidates.HUB_MAX_EVENTS`` events by definition); in a longer posting the
undated members are still followed, and the dated ones nearest in date. The
counts before the cap (all pairs, long-gap, unknown-gap) are computed
arithmetically (``bisect``), not by enumeration.

Work per event is bounded by ``K``, ``PER_KEY_LIMIT`` and ``RETRIEVAL_BUDGET``
(times a logarithm), plus the events whose interval overlaps the event's own
in a long posting (all at gap 0, so all are looked at to break the tie by id).

What differs from the exhaustive ranking is only how ties break among dated
pairs of equal evidence and equal gap (start order here, pair id there), and a
pair of two events whose ids are prefixes of one another (pair ids of fixed-width
ids order as the ids do). ``scripts/measure_candidate_generation.py`` measures the
difference on a database and against the labelled pairs.

Nothing here is part of a calibration: it changes how the asked pairs are found
in a large group, never how a pair is scored or judged.
"""

from __future__ import annotations

import bisect
import heapq
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from cyber_data_collector.dedup.candidate_ranking import (
    LONG_GAP_BUDGET, PARTNERS_PER_EVENT, WINDOW_DAYS, Corpus, score_pair, select,
)

#: A group of at most this many events is ranked exhaustively (at most 1,770 pairs built); a larger one by
#: bounded retrieval.
EXHAUSTIVE_GROUP_MAX = 60
#: A group of at most this many events has no cap at all on the organisation route: every pair of it is a
#: candidate (still ranked). The cap exists to bound a large group's cost; for a small one it only lost
#: recall - on the labelling-time snapshot it dropped 6 of the 17 labelled-same organisation pairs (misdated
#: copies 707 to 4,603 days apart), which the route used to judge. The AI route, one big group by construction,
#: keeps its cap. Documented in README.md; the findings report ``uncapped_groups``.
UNCAPPED_GROUP_MAX = EXHAUSTIVE_GROUP_MAX
#: Events followed per key shared by many events (a vendor, a figure, a common rare word): the nearest in date,
#: and as many undated ones.
PER_KEY_LIMIT = 24
#: Most candidates one event takes from its postings (rarest key first). On the 2026-10-29 database the most any
#: event touches is about 300 (101 cited sources of at most 3 events each); the bound is for a large group.
RETRIEVAL_BUDGET = 1024


def pair_id(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


@dataclass(frozen=True)
class Span:
    """One event in a group: its id and the date interval it allows (None, None when undated).

    ``payload`` is for the caller's pair builder and takes no part in equality.
    """

    event_id: str
    start: Optional[date]
    end: Optional[date]
    payload: Any = field(default=None, compare=False)


def span_gap(a: Span, b: Span) -> Optional[int]:
    """Days between two events' date intervals (0 when they overlap); None when either is undated."""
    if a.start is None or b.start is None:
        return None
    if a.start <= b.end and b.start <= a.end:
        return 0
    return (b.start - a.end).days if b.start > a.end else (a.start - b.end).days


def span_order(s: Span) -> Tuple[Any, ...]:
    """Earliest first, undated last, ties by id: the order a pair's ``a`` and ``b`` are taken in."""
    return (s.start is None, s.start or date.max, s.end or date.max, s.event_id)


def _combinations(n: int) -> int:
    return n * (n - 1) // 2


def _smallest_pair_ids(sorted_ids: List[str], pivot: str, k: int) -> List[str]:
    """The ``k`` ids whose pair id with ``pivot`` sorts first: those below ``pivot`` ascending (``x|pivot``), then
    those above it ascending (``pivot|y``). ``sorted_ids`` is ascending; ``pivot`` itself is skipped."""
    i = bisect.bisect_left(sorted_ids, pivot)
    out = sorted_ids[:i][:k]
    if len(out) < k:
        j = i + 1 if i < len(sorted_ids) and sorted_ids[i] == pivot else i
        out += sorted_ids[j:j + k - len(out)]
    return out


class IntervalIndex:
    """Events sorted once by date interval, for nearest-in-date queries that never sort per query.

    Dated events are held in start order (start, end, id) and in end order (end, id), as day ordinals; undated
    ones and all ids in id order. Right of a query interval in start order, and left of it in end order, the gap
    grows monotonically, so the nearest are found by ``bisect`` and a bounded walk out from the query.
    """

    def __init__(self, spans: Iterable[Span]):
        unique = {s.event_id: s for s in spans}
        dated = sorted((s for s in unique.values() if s.start is not None),
                       key=lambda s: (s.start, s.end, s.event_id))
        self.by_start: List[Span] = dated
        self.by_end: List[Span] = sorted(dated, key=lambda s: (s.end, s.event_id))
        self.starts = [s.start.toordinal() for s in self.by_start]
        self.start_ends = [s.end.toordinal() for s in self.by_start]
        self.ends = [s.end.toordinal() for s in self.by_end]
        self.end_starts = [s.start.toordinal() for s in self.by_end]
        self.start_pos = {s.event_id: i for i, s in enumerate(self.by_start)}
        self.end_pos = {s.event_id: i for i, s in enumerate(self.by_end)}
        self.undated_ids = sorted(s.event_id for s in unique.values() if s.start is None)
        self.all_ids = sorted(unique)
        self.max_length = max((e - s for s, e in zip(self.starts, self.start_ends)), default=0)

    def __len__(self) -> int:
        return len(self.all_ids)

    def adjacent(self, span: Span, k: int) -> List[str]:
        """The ``k`` events before and after a member ``span`` in start order and in end order."""
        if span.start is None or k <= 0:
            return []
        i, j = self.start_pos[span.event_id], self.end_pos[span.event_id]
        return [x.event_id for x in (*self.by_start[max(0, i - k):i], *self.by_start[i + 1:i + 1 + k],
                                     *self.by_end[max(0, j - k):j], *self.by_end[j + 1:j + 1 + k])]

    def _overlapping(self, start: int, end: int) -> List[str]:
        """Ids of the dated events whose interval overlaps ``[start, end]`` (day ordinals): gap 0. Scans whichever
        of the start-order or end-order range that can hold them is shorter (the longest interval bounds both)."""
        s_lo = bisect.bisect_left(self.starts, start - self.max_length)
        s_hi = bisect.bisect_right(self.starts, end)
        e_lo = bisect.bisect_left(self.ends, start)
        e_hi = bisect.bisect_right(self.ends, end + self.max_length)
        if s_hi - s_lo <= e_hi - e_lo:
            return [self.by_start[p].event_id for p in range(s_lo, s_hi) if self.start_ends[p] >= start]
        return [self.by_end[p].event_id for p in range(e_lo, e_hi) if self.end_starts[p] <= end]

    def nearest(self, span: Span, k: int) -> List[str]:
        """The ``k`` members nearest in date to ``span`` (undated last, ties by id): exactly
        ``sorted(members, key=(span_gap or infinity, id))[:k]``, ``span`` itself included when it is a member."""
        if k <= 0:
            return []
        if span.start is None:                     # no gap to anyone: the k smallest ids
            return self.all_ids[:k]
        start, end = span.start.toordinal(), span.end.toordinal()
        overlap = self._overlapping(start, end)
        if len(overlap) >= k:
            return heapq.nsmallest(k, overlap)
        out = sorted(overlap)
        n = len(self.by_start)
        right = bisect.bisect_right(self.starts, end)          # by_start[right:] start after the span
        left = bisect.bisect_left(self.ends, start) - 1        # by_end[:left + 1] end before it
        while len(out) < k and (right < n or left >= 0):
            gap = min(self.starts[right] - end if right < n else 10 ** 9,
                      start - self.ends[left] if left >= 0 else 10 ** 9)
            tied: List[str] = []
            while right < n and self.starts[right] - end == gap:
                tied.append(self.by_start[right].event_id)
                right += 1
            while left >= 0 and start - self.ends[left] == gap:
                tied.append(self.by_end[left].event_id)
                left -= 1
            out.extend(sorted(tied)[:k - len(out)])
        out.extend(self.undated_ids[:k - len(out)])
        return out

    def undated_by_pair_id(self, pivot: str, k: int) -> List[str]:
        """The ``k`` undated members whose pair id with ``pivot`` sorts first."""
        return _smallest_pair_ids(self.undated_ids, pivot, k)


#: ``make_pair(a, b, gap, group)`` builds a route's pair dict; ``a`` is the earlier event.
MakePair = Callable[[Span, Span, Optional[int], Any], Dict[str, Any]]


def select_bounded(corpus: Corpus, spans: Sequence[Span], make_pair: MakePair, group: Any = None,
                   window_days: int = WINDOW_DAYS, partners_per_event: int = PARTNERS_PER_EVENT,
                   long_gap_budget: int = LONG_GAP_BUDGET, per_key_limit: int = PER_KEY_LIMIT,
                   exclude: Optional[Callable[[str, str], bool]] = None,
                   retrieval_budget: int = RETRIEVAL_BUDGET,
                   ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """:func:`candidate_ranking.select` for one group, without building the group's pairs.

    ``spans`` are the group's events (one per id). Returns ``(kept pairs, counts)``; the counts are the
    arithmetic ones (``before``, ``long_gap_before``, ``unknown_gap_before``) over **all** the group's pairs,
    including any ``exclude`` removes; ``events_at_retrieval_budget``, the events whose keys were not all
    followed because ``retrieval_budget`` candidates had been taken; and ``pairs_scored``, the pairs retrieved and
    scored (``make_pair`` is called only for the pairs kept). ``partners_per_event`` must be positive: no
    cap means every pair, which is the exhaustive path's job.
    """
    if partners_per_event <= 0:
        raise ValueError("bounded retrieval needs a positive partners_per_event")
    ordered = sorted({s.event_id: s for s in spans}.values(), key=span_order)
    n = len(ordered)
    by_id = {s.event_id: s for s in ordered}
    index = IntervalIndex(ordered)                                    # the group's date-interval index, built once

    # Arithmetic counts over every pair of the group (a far pair is counted once: from its earlier event).
    starts = index.starts
    counts = {"before": _combinations(n),
              "long_gap_before": sum(len(starts) - bisect.bisect_right(starts, e + window_days)
                                     for e in index.start_ends),
              "unknown_gap_before": _combinations(n) - _combinations(len(starts)),
              "events_at_retrieval_budget": 0}

    # Posting lists: the events of this group that carry each key; each event's keys, rarest first.
    postings: Dict[Tuple[str, Any], List[str]] = defaultdict(list)
    keys_of: Dict[str, List[Tuple[str, Any]]] = {}
    for s in ordered:
        f = corpus.features.get(s.event_id)
        if f is None:
            continue
        keys = [("url", url) for url in f.urls - corpus.hub_urls]
        if f.vendor:
            keys.append(("vendor", f.vendor))
        keys.extend(("token", token) for token in f.tokens & corpus.rare_tokens)
        if f.records_affected is not None:
            keys.append(("records", f.records_affected))
        for key in keys:
            postings[key].append(s.event_id)
        keys_of[s.event_id] = keys
    for keys in keys_of.values():
        keys.sort(key=lambda key: (len(postings[key]), key[0], str(key[1])))
    posting_index: Dict[Tuple[str, Any], IntervalIndex] = {}

    def follow(span: Span, key: Tuple[str, Any]) -> List[str]:
        """A posting, or for a long one its ``per_key_limit`` nearest in date and as many undated members."""
        ids = postings[key]
        if len(ids) <= per_key_limit:
            return ids
        idx = posting_index.get(key)
        if idx is None:
            idx = posting_index[key] = IntervalIndex(by_id[i] for i in ids)
        return idx.nearest(span, per_key_limit) + idx.undated_by_pair_id(span.event_id, per_key_limit)

    def retrieve(span: Span) -> Set[str]:
        found: Set[str] = set()
        taken = 0
        for key in keys_of.get(span.event_id, ()):
            if taken >= retrieval_budget:
                counts["events_at_retrieval_budget"] += 1
                break
            got = follow(span, key)
            taken += len(got)
            found.update(got)
        k = partners_per_event
        # No date term: the K smallest pair ids among the partners the date cannot rank.
        found.update(_smallest_pair_ids(index.all_ids if span.start is None else index.undated_ids,
                                        span.event_id, k))
        found.update(index.adjacent(span, k))
        found.discard(span.event_id)
        return found

    # Every retrieved pair is scored once and remembered compactly - (score, strong, gap, earlier id, later id) -
    # so the other event's retrieval reuses it; the route's pair dict is built only for the pairs kept.
    scored: Dict[str, Tuple[float, bool, Optional[int], str, str]] = {}
    top: List[List[str]] = []
    for span in ordered:
        near: List[Tuple[float, str]] = []
        for other_id in retrieve(span):
            if exclude is not None and exclude(span.event_id, other_id):
                continue
            pid = pair_id(span.event_id, other_id)
            entry = scored.get(pid)
            if entry is None:
                other = by_id[other_id]
                a, b = (span, other) if span_order(span) <= span_order(other) else (other, span)
                gap = span_gap(a, b)
                score, _reasons, strong = score_pair(corpus, a.event_id, b.event_id, gap, window_days)
                entry = scored[pid] = (round(score, 4), strong, gap, a.event_id, b.event_id)
            if entry[2] is None or entry[2] <= window_days:
                near.append((-entry[0], pid))
        near.sort()
        top.append([pid for _, pid in near[:partners_per_event]])
    counts["pairs_scored"] = len(scored)

    def build(pid: str, kept_as: str) -> Dict[str, Any]:
        score, strong, gap, a_id, b_id = scored[pid]
        _score, reasons, _strong = score_pair(corpus, a_id, b_id, gap, window_days)
        return {**make_pair(by_id[a_id], by_id[b_id], gap, group), "rank_score": score, "rank_reasons": reasons,
                "rank_strong": strong, "rank_far": gap is not None and gap > window_days, "rank_kept_as": kept_as}

    kept: Dict[str, Dict[str, Any]] = {}
    for pids in top:
        for pid in pids:
            if pid not in kept:
                kept[pid] = build(pid, "partner")
    reserve = [(-entry[0], pid) for pid, entry in scored.items()
               if entry[1] and pid not in kept and (entry[2] is None or entry[2] > window_days)]
    for _, pid in sorted(reserve)[:long_gap_budget]:
        kept[pid] = build(pid, "reserved")
    return sorted(kept.values(), key=lambda p: p["pair_id"]), counts


def rank_groups(corpus: Corpus, groups: Dict[Any, Sequence[Span]], make_pair: MakePair,
                window_days: int = WINDOW_DAYS, partners_per_event: int = PARTNERS_PER_EVENT,
                long_gap_budget: int = LONG_GAP_BUDGET, cap: bool = True,
                exclude: Optional[Callable[[str, str], bool]] = None,
                exhaustive_max: int = EXHAUSTIVE_GROUP_MAX,
                uncapped_max: int = 0) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Rank and cap the candidate pairs of every group: :func:`candidate_ranking.select` for a small group,
    :func:`select_bounded` for a large one (and for none of them when ``cap`` is False: every pair is then
    built, as asked).

    ``groups`` maps a group key to its events; a pair that two groups both produce is kept once, from the
    first group. ``make_pair(a, b, gap, group)`` builds the pair dict (``pair_id``, ``a``, ``b``, ``gap_days``
    and whatever the route's judgement needs), ``a`` being the earlier event, undated last.
    ``exclude(a_id, b_id)`` removes the pairs another route owns. Returns ``(kept, stats)`` with the keys
    :func:`candidate_ranking.select` returns, plus ``bounded_groups``, ``uncapped_groups`` and
    ``events_at_retrieval_budget`` and ``pairs_scored`` (:func:`select_bounded`; 0 for groups ranked by
    enumeration). ``before`` for a
    large group counts every pair of it, including excluded ones.

    A group of at most ``uncapped_max`` events keeps **every** pair: the cap bounds a large group's cost, and for a
    small one it only lost recall. Its pairs are still scored and ranked (``rank_score``, ``rank_reasons``), so a
    report shows the likeliest first; they count as ``kept_as_partner``.
    """
    exhaustive = not cap or partners_per_event <= 0
    small: Dict[str, Tuple[Dict[str, Any], Any]] = {}
    uncapped: Dict[str, Tuple[Dict[str, Any], Any]] = {}
    big: List[Tuple[Any, List[Span]]] = []
    uncapped_groups = 0
    for gid, members in groups.items():
        ordered = sorted({s.event_id: s for s in members}.values(), key=span_order)
        if exhaustive or len(ordered) <= max(exhaustive_max, uncapped_max):
            keep_all = len(ordered) <= uncapped_max
            uncapped_groups += keep_all
            for i, a in enumerate(ordered):
                for b in ordered[i + 1:]:
                    pid = pair_id(a.event_id, b.event_id)
                    if pid in small or pid in uncapped or (exclude is not None and exclude(a.event_id, b.event_id)):
                        continue
                    (uncapped if keep_all else small)[pid] = (make_pair(a, b, span_gap(a, b), gid), gid)
        else:
            big.append((gid, ordered))
    kept_small, stats = select(corpus, [p for p, _ in small.values()],
                               group_of=lambda p: small[p["pair_id"]][1], window_days=window_days,
                               partners_per_event=partners_per_event, long_gap_budget=long_gap_budget, cap=cap)
    merged = {p["pair_id"]: p for p in kept_small}
    if uncapped:
        kept_all, extra = select(corpus, [p for p, _ in uncapped.values()],
                                 group_of=lambda p: uncapped[p["pair_id"]][1], window_days=window_days,
                                 partners_per_event=partners_per_event, long_gap_budget=long_gap_budget, cap=False)
        merged.update({p["pair_id"]: p for p in kept_all})
        for key in ("before", "groups", "long_gap_before", "unknown_gap_before"):
            stats[key] += extra[key]
        stats["largest_group_pairs"] = max(stats["largest_group_pairs"], extra["largest_group_pairs"])
    stats["bounded_groups"] = len(big)
    stats["uncapped_groups"] = uncapped_groups
    stats["events_at_retrieval_budget"] = 0
    stats["pairs_scored"] = 0
    stats["parameters"]["uncapped_group_max"] = uncapped_max
    for gid, ordered in big:
        kept, counts = select_bounded(corpus, ordered, make_pair, gid, window_days, partners_per_event,
                                      long_gap_budget, exclude=exclude)
        for p in kept:
            merged.setdefault(p["pair_id"], p)
        stats["before"] += counts["before"]
        stats["long_gap_before"] += counts["long_gap_before"]
        stats["unknown_gap_before"] += counts["unknown_gap_before"]
        stats["events_at_retrieval_budget"] += counts["events_at_retrieval_budget"]
        stats["pairs_scored"] += counts["pairs_scored"]
        stats["groups"] += 1
        stats["largest_group_pairs"] = max(stats["largest_group_pairs"], counts["before"])
    out = sorted(merged.values(), key=lambda p: p["pair_id"])
    stats.update(after=len(out),
                 kept_as_partner=sum(1 for p in out if p["rank_kept_as"] == "partner"),
                 kept_as_reserved=sum(1 for p in out if p["rank_kept_as"] == "reserved"),
                 long_gap_after=sum(1 for p in out if p["rank_far"]),
                 unknown_gap_after=sum(1 for p in out if not isinstance(p["gap_days"], int)))
    return out, stats
