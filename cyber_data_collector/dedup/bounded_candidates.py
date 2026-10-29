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
  once over the group; a key shared by many events (a vendor, a figure) is
  followed to its ``PER_KEY_LIMIT`` nearest in date;
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
so a strong match is retrieved however far apart in time, or undated. The
counts before the cap (all pairs, long-gap, unknown-gap) are computed
arithmetically (``bisect``), not by enumeration.

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
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

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
#: Events followed per key shared by many events (a vendor, a records-affected figure): the nearest in date.
PER_KEY_LIMIT = 24


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


#: ``make_pair(a, b, gap, group)`` builds a route's pair dict; ``a`` is the earlier event.
MakePair = Callable[[Span, Span, Optional[int], Any], Dict[str, Any]]


def select_bounded(corpus: Corpus, spans: Sequence[Span], make_pair: MakePair, group: Any = None,
                   window_days: int = WINDOW_DAYS, partners_per_event: int = PARTNERS_PER_EVENT,
                   long_gap_budget: int = LONG_GAP_BUDGET, per_key_limit: int = PER_KEY_LIMIT,
                   exclude: Optional[Callable[[str, str], bool]] = None
                   ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """:func:`candidate_ranking.select` for one group, without building the group's pairs.

    ``spans`` are the group's events (one per id). Returns ``(kept pairs, counts)``; the counts are the
    arithmetic ones (``before``, ``long_gap_before``, ``unknown_gap_before``) over **all** the group's pairs,
    including any ``exclude`` removes. ``partners_per_event`` must be positive: no cap means every pair, which
    is the exhaustive path's job.
    """
    if partners_per_event <= 0:
        raise ValueError("bounded retrieval needs a positive partners_per_event")
    ordered = sorted({s.event_id: s for s in spans}.values(), key=span_order)
    n = len(ordered)
    by_start = [s for s in ordered if s.start is not None]            # already in start order
    by_end = sorted(by_start, key=lambda s: (s.end, s.event_id))
    start_pos = {s.event_id: i for i, s in enumerate(by_start)}
    end_pos = {s.event_id: i for i, s in enumerate(by_end)}
    by_id = {s.event_id: s for s in ordered}
    all_ids = sorted(by_id)
    undated_ids = sorted(s.event_id for s in ordered if s.start is None)

    # Arithmetic counts over every pair of the group (a far pair is counted once: from its earlier event).
    starts = [s.start for s in by_start]
    window = timedelta(days=window_days)
    counts = {"before": _combinations(n),
              "long_gap_before": sum(len(starts) - bisect.bisect_right(starts, s.end + window) for s in by_start),
              "unknown_gap_before": _combinations(n) - _combinations(len(by_start))}

    # Posting lists: the events of this group that carry each key.
    postings: Dict[Tuple[str, Any], List[str]] = defaultdict(list)
    for s in ordered:
        f = corpus.features.get(s.event_id)
        if f is None:
            continue
        for url in f.urls - corpus.hub_urls:
            postings[("url", url)].append(s.event_id)
        if f.vendor:
            postings[("vendor", f.vendor)].append(s.event_id)
        for token in f.tokens & corpus.rare_tokens:
            postings[("token", token)].append(s.event_id)
        if f.records_affected is not None:
            postings[("records", f.records_affected)].append(s.event_id)

    def nearest(span: Span, ids: List[str]) -> List[str]:
        """``ids`` limited to ``per_key_limit``, nearest in date to ``span`` (undated last), ties by id."""
        def key(i: str) -> Tuple[int, str]:
            g = span_gap(span, by_id[i])
            return (g if g is not None else 10 ** 9, i)
        return sorted(ids, key=key)[:per_key_limit]

    def retrieve(span: Span) -> Set[str]:
        found: Set[str] = set()
        f = corpus.features.get(span.event_id)
        if f is not None:
            for url in f.urls - corpus.hub_urls:
                found.update(postings[("url", url)])
            for token in f.tokens & corpus.rare_tokens:
                found.update(postings[("token", token)])
            if f.vendor:
                found.update(nearest(span, postings[("vendor", f.vendor)]))
            if f.records_affected is not None:
                found.update(nearest(span, postings[("records", f.records_affected)]))
        k = partners_per_event
        # No date term: the K smallest pair ids among the partners the date cannot rank.
        found.update(_smallest_pair_ids(all_ids if span.start is None else undated_ids, span.event_id, k))
        if span.start is not None:
            i, j = start_pos[span.event_id], end_pos[span.event_id]
            found.update(x.event_id for x in by_start[max(0, i - k):i])
            found.update(x.event_id for x in by_start[i + 1:i + 1 + k])
            found.update(x.event_id for x in by_end[max(0, j - k):j])
            found.update(x.event_id for x in by_end[j + 1:j + 1 + k])
        found.discard(span.event_id)
        return found

    scored: Dict[str, Dict[str, Any]] = {}
    top: List[List[str]] = []
    for span in ordered:
        near: List[Tuple[Tuple[float, str], str]] = []
        for other_id in retrieve(span):
            if exclude is not None and exclude(span.event_id, other_id):
                continue
            pid = pair_id(span.event_id, other_id)
            if pid not in scored:
                other = by_id[other_id]
                a, b = (span, other) if span_order(span) <= span_order(other) else (other, span)
                gap = span_gap(a, b)
                score, reasons, strong = score_pair(corpus, a.event_id, b.event_id, gap, window_days)
                scored[pid] = {**make_pair(a, b, gap, group), "rank_score": round(score, 4),
                               "rank_reasons": reasons, "rank_strong": strong,
                               "rank_far": gap is not None and gap > window_days}
            if not scored[pid]["rank_far"]:
                near.append(((-scored[pid]["rank_score"], pid), pid))
        top.append([pid for _, pid in sorted(near)[:partners_per_event]])

    kept: Dict[str, Dict[str, Any]] = {}
    for pids in top:
        for pid in pids:
            kept[pid] = {**scored[pid], "rank_kept_as": "partner"}
    reserve = [p for pid, p in scored.items() if p["rank_strong"] and pid not in kept
               and (p["rank_far"] or not isinstance(p["gap_days"], int))]
    for p in sorted(reserve, key=lambda p: (-p["rank_score"], p["pair_id"]))[:long_gap_budget]:
        kept[p["pair_id"]] = {**p, "rank_kept_as": "reserved"}
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
    :func:`candidate_ranking.select` returns, plus ``bounded_groups`` and ``uncapped_groups``. ``before`` for a
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
    stats["parameters"]["uncapped_group_max"] = uncapped_max
    for gid, ordered in big:
        kept, counts = select_bounded(corpus, ordered, make_pair, gid, window_days, partners_per_event,
                                      long_gap_budget, exclude=exclude)
        for p in kept:
            merged.setdefault(p["pair_id"], p)
        stats["before"] += counts["before"]
        stats["long_gap_before"] += counts["long_gap_before"]
        stats["unknown_gap_before"] += counts["unknown_gap_before"]
        stats["groups"] += 1
        stats["largest_group_pairs"] = max(stats["largest_group_pairs"], counts["before"])
    out = sorted(merged.values(), key=lambda p: p["pair_id"])
    stats.update(after=len(out),
                 kept_as_partner=sum(1 for p in out if p["rank_kept_as"] == "partner"),
                 kept_as_reserved=sum(1 for p in out if p["rank_kept_as"] == "reserved"),
                 long_gap_after=sum(1 for p in out if p["rank_far"]),
                 unknown_gap_after=sum(1 for p in out if not isinstance(p["gap_days"], int)))
    return out, stats
