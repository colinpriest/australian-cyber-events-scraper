"""Rank duplicate candidates by what is specific to the incident, and cap what is asked.

The duplicate check used to ask about every pair of events sharing a victim:
229 candidates in a 2026-10-29 sample, 170 of them more than a year apart. Large
groups (a university with 20 events) grow with the square of their size, while a
bucket-size limit elsewhere (``Adjudicator.MAX_ENTITY_BUCKET``) simply skipped
them and the date-window rules skipped undated reports. A pair costs a Jev
call and, in the middle band, a reasoning-model call, so the number asked needs a
documented bound, and the pairs inside the bound should be the likeliest ones.

Each candidate is scored from four kinds of evidence about the *incident*, not
about how alike two titles are:

* **source relationships** - the two events cite the same article (a hub URL,
  one cited by more than ``HUB_MAX_EVENTS`` events, is a list or roundup and
  counts for nothing);
* **supplier clues** - both record the same vendor (a supplier breach reported
  once per customer);
* **incident-specific details** - corpus-rare words they share (a rare word is
  one few events contain, measured from the database, not curated), the same
  records-affected figure, the same attack method;
* **date ranges** - the closer the two date intervals, the likelier one
  incident; an unknown date neither helps nor counts against.

The weights only *order* candidates; they decide nothing. What merges is still
the calibrated cut-off on the model's answer, so a pair this module drops costs
recall, never precision, and the cost is reported (``before`` / ``after``).

Selection, per group (the victim organisation; all AI-involved events are one
group):

1. **Partners per event** (``PARTNERS_PER_EVENT``, 8): a pair within the window
   (or with an unknown gap) is kept when it is among the best ``K`` partners of
   either of its events. A group small enough that no event has more than ``K``
   partners keeps everything; a large group is thereby subdivided into each
   event's neighbourhood instead of growing with the square of its size.
2. **A reserved budget for strong long-gap or undated matches**
   (``LONG_GAP_BUDGET``, 5 per group): a pair more than ``WINDOW_DAYS`` apart, or
   with an unknown gap, that shares a source, a supplier or two rare words is
   kept (best first) up to the budget even when it lost on step 1. A misdated
   record and an undated report of the same incident are exactly the cases a
   date term would otherwise rank last.
3. A pair more than ``WINDOW_DAYS`` apart with no such evidence is not asked.

:func:`select` ranks pairs it is given, so a caller has to build every pair of a
group first. :mod:`bounded_candidates` does the same ranking for a large group
without building the pairs it does not keep (retrieval through source, supplier,
rare-word, figure and date-interval indexes), so CPU and memory no longer grow
with the square of the largest group.

Every parameter is an argument of :func:`select`, of
``duplicate_judgement.check_duplicates`` and of ``dedup_admin.py check-duplicates``
(``--partners-per-event``, ``--long-gap-budget``, ``--no-candidate-cap``), and
the counts before and after are in the findings file.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from cyber_data_collector.dedup import candidates as keys

logger = logging.getLogger(__name__)

#: Gap, in days, beyond which a pair is "long-gap" (needs strong evidence to be asked at all).
WINDOW_DAYS = 365
#: Best partners kept per event inside a group (0 = no cap).
PARTNERS_PER_EVENT = 8
#: Strong long-gap / undated pairs kept per group beyond the partner cap.
LONG_GAP_BUDGET = 5
#: Two shared corpus-rare words make a pair "strong" on their own.
STRONG_RARE_WORDS = 2
#: Ordering weights (see the module docstring: they order, they decide nothing).
W_SOURCE, W_SUPPLIER, W_RARE_WORD, MAX_RARE_WORDS = 3.0, 3.0, 1.5, 3
W_RECORDS, W_METHOD, W_DATE = 2.0, 0.5, 2.0


@dataclass
class EventFeatures:
    event_id: str
    tokens: Set[str] = field(default_factory=set)
    urls: Set[str] = field(default_factory=set)
    vendor: str = ""
    method: str = ""
    records_affected: Optional[int] = None


@dataclass
class Corpus:
    """What is measured over all active events: the features and the corpus-level statistics."""

    features: Dict[str, EventFeatures]
    rare_tokens: Set[str]
    hub_urls: Set[str]


def _norm(text: Optional[str]) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def load_corpus(conn: sqlite3.Connection) -> Corpus:
    """Features of every active event, with the corpus-rare words and the hub URLs measured from them."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(DeduplicatedEvents)")}

    def col(name: str) -> str:
        return name if name in columns else "NULL"

    rows = conn.execute(
        f"SELECT deduplicated_event_id, title, {col('summary')}, {col('description')}, "
        f"{col('vendor_organization_name')}, {col('attack_method')}, {col('records_affected')} "
        "FROM DeduplicatedEvents WHERE COALESCE(status, 'Active') = 'Active'").fetchall()
    features: Dict[str, EventFeatures] = {}
    for event_id, title, summary, description, vendor, method, records in rows:
        features[event_id] = EventFeatures(
            event_id, keys.subject_tokens(title, summary or description), set(), _norm(vendor), _norm(method),
            records if isinstance(records, int) and records > 0 else None)
    from cyber_data_collector.processing.active_evidence import member_condition
    try:
        url_rows = conn.execute(
            f"SELECT m.deduplicated_event_id, r.source_url FROM EventDeduplicationMap m "
            f"JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id "
            f"JOIN RawEvents r ON r.raw_event_id = e.raw_event_id WHERE r.source_url IS NOT NULL "
            f"{member_condition(conn)}").fetchall()
    except sqlite3.OperationalError:       # a schema without the record tables: no source evidence
        url_rows = []
    events_by_url: Dict[str, Set[str]] = defaultdict(set)
    for event_id, url in url_rows:
        norm = keys.normalise_url(url)
        if norm and event_id in features:
            features[event_id].urls.add(norm)
            events_by_url[norm].add(event_id)
    hubs = {u for u, ids in events_by_url.items() if len(ids) > keys.HUB_MAX_EVENTS}
    doc_freq: Dict[str, int] = defaultdict(int)
    for f in features.values():
        for token in f.tokens:
            doc_freq[token] += 1
    ceiling = max(keys.CONTENT_MIN_ABSOLUTE, int(len(features) * keys.CONTENT_MAX_SHARE))
    rare = {t for t, n in doc_freq.items() if 2 <= n <= ceiling}
    return Corpus(features, rare, hubs)


def corpus_from_records(records: Sequence[Any]) -> Corpus:
    """A :class:`Corpus` over enriched-event records (``adjudicator.EventRecord``): the same statistics, for
    candidate generation on records rather than deduplicated events. Records carry no vendor or method."""
    features: Dict[str, EventFeatures] = {}
    events_by_url: Dict[str, Set[str]] = defaultdict(set)
    for r in records:
        urls = {u for u in (keys.normalise_url(x) for x in r.all_urls()) if u}
        features[r.enriched_event_id] = EventFeatures(
            r.enriched_event_id, keys.subject_tokens(r.title, r.summary or r.description), urls, "", "",
            r.records_affected if isinstance(r.records_affected, int) and r.records_affected > 0 else None)
        for u in urls:
            events_by_url[u].add(r.enriched_event_id)
    hubs = {u for u, ids in events_by_url.items() if len(ids) > keys.HUB_MAX_EVENTS}
    doc_freq: Dict[str, int] = defaultdict(int)
    for f in features.values():
        for token in f.tokens:
            doc_freq[token] += 1
    ceiling = max(keys.CONTENT_MIN_ABSOLUTE, int(len(features) * keys.CONTENT_MAX_SHARE))
    return Corpus(features, {t for t, n in doc_freq.items() if 2 <= n <= ceiling}, hubs)


def subdivide(corpus: Corpus, members: Sequence[Any], gap_of: Callable[[Any, Any], Optional[int]],
              window_days: int = WINDOW_DAYS, partners_per_event: int = PARTNERS_PER_EVENT,
              long_gap_budget: int = LONG_GAP_BUDGET, day_of: Optional[Callable[[Any], Any]] = None,
              ) -> List[Tuple[str, str]]:
    """Pairs worth asking inside one oversized bucket of records: each member's best partners, plus the
    reserved budget of strong long-gap / undated pairs (:func:`select` with the bucket as one group).

    Replaces skipping a bucket larger than ``Adjudicator.MAX_ENTITY_BUCKET``, which lost every duplicate
    inside it. ``members`` are records with ``enriched_event_id``; ``gap_of(left, right)`` is the gap in days.
    With ``day_of`` (a member's date) a bucket of more than ``EXHAUSTIVE_GROUP_MAX`` records is ranked by
    bounded retrieval (:mod:`bounded_candidates`) instead of building every pair of it; the gap is then the
    days between those dates and ``gap_of`` is not used.
    """
    if day_of is not None:
        from cyber_data_collector.dedup import bounded_candidates as bounded

        spans = [bounded.Span(m.enriched_event_id, day_of(m), day_of(m)) for m in members]

        def make(a, b, gap, _group):
            return {"pair_id": bounded.pair_id(a.event_id, b.event_id), "a": a.event_id, "b": b.event_id,
                    "gap_days": gap if gap is not None else "unknown"}

        kept, _ = bounded.rank_groups(corpus, {0: spans}, make, window_days=window_days,
                                      partners_per_event=partners_per_event, long_gap_budget=long_gap_budget)
        return [(p["a"], p["b"]) for p in kept]
    pairs = []
    ordered = sorted(members, key=lambda r: r.enriched_event_id)
    for i, left in enumerate(ordered):
        for right in ordered[i + 1:]:
            gap = gap_of(left, right)
            pairs.append({"pair_id": f"{left.enriched_event_id}|{right.enriched_event_id}",
                          "a": left.enriched_event_id, "b": right.enriched_event_id,
                          "gap_days": gap if gap is not None else "unknown"})
    kept, _ = select(corpus, pairs, group_of=lambda p: 0, window_days=window_days,
                     partners_per_event=partners_per_event, long_gap_budget=long_gap_budget)
    return [(p["a"], p["b"]) for p in kept]


def score_pair(corpus: Corpus, a_id: str, b_id: str, gap: Optional[int],
               window_days: int = WINDOW_DAYS) -> Tuple[float, List[str], bool]:
    """``(score, reasons, strong)`` for one candidate pair; ``gap`` is None when a date is unknown."""
    a, b = corpus.features.get(a_id), corpus.features.get(b_id)
    score, reasons, strong = 0.0, [], False
    if a is None or b is None:
        return score, reasons, strong
    shared_sources = sorted((a.urls & b.urls) - corpus.hub_urls)
    if shared_sources:
        score += W_SOURCE
        reasons.append(f"shared source {shared_sources[0]}")
        strong = True
    if a.vendor and a.vendor == b.vendor:
        score += W_SUPPLIER
        reasons.append(f"same supplier '{a.vendor}'")
        strong = True
    words = sorted((a.tokens & b.tokens) & corpus.rare_tokens)
    if words:
        score += W_RARE_WORD * min(len(words), MAX_RARE_WORDS)
        reasons.append("shared incident-specific words: " + ", ".join(words[:MAX_RARE_WORDS]))
        strong = strong or len(words) >= STRONG_RARE_WORDS
    if a.records_affected is not None and a.records_affected == b.records_affected:
        score += W_RECORDS
        reasons.append(f"same records-affected figure ({a.records_affected})")
    if a.method and a.method == b.method:
        score += W_METHOD
        reasons.append("same attack method")
    if gap is not None and gap <= window_days:
        score += W_DATE * (1.0 - gap / window_days)
    return score, reasons, strong


def _gap(pair: Dict[str, Any]) -> Optional[int]:
    return pair["gap_days"] if isinstance(pair.get("gap_days"), int) else None


def select(corpus: Corpus, pairs: Sequence[Dict[str, Any]], group_of: Callable[[Dict[str, Any]], Any],
           window_days: int = WINDOW_DAYS, partners_per_event: int = PARTNERS_PER_EVENT,
           long_gap_budget: int = LONG_GAP_BUDGET, cap: bool = True) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Rank ``pairs`` and keep the ones that will be asked; returns ``(kept, stats)``.

    Kept pairs gain ``rank_score``, ``rank_reasons`` and ``rank_kept_as`` (``partner`` or
    ``reserved``). With ``cap`` False every pair is kept (still ranked). ``stats`` has the counts
    before and after, overall and per route of keeping, and the largest group.
    """
    scored: List[Dict[str, Any]] = []
    for pair in pairs:
        gap = _gap(pair)
        score, reasons, strong = score_pair(corpus, pair["a"], pair["b"], gap, window_days)
        scored.append({**pair, "rank_score": round(score, 4), "rank_reasons": reasons, "rank_strong": strong,
                       "rank_far": gap is not None and gap > window_days})
    groups: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for pair in scored:
        groups[group_of(pair)].append(pair)

    kept: Dict[str, Dict[str, Any]] = {}
    for members in groups.values():
        order = lambda p: (-p["rank_score"], p["pair_id"])           # noqa: E731 - deterministic
        if not cap:
            for p in members:
                kept[p["pair_id"]] = {**p, "rank_kept_as": "partner"}
            continue
        near = [p for p in members if not p["rank_far"]]
        by_event: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for p in near:
            by_event[p["a"]].append(p)
            by_event[p["b"]].append(p)
        for event_pairs in by_event.values():
            for p in sorted(event_pairs, key=order)[:partners_per_event or len(event_pairs)]:
                kept[p["pair_id"]] = {**p, "rank_kept_as": "partner"}
        reserve = [p for p in members if p["rank_strong"] and p["pair_id"] not in kept
                   and (p["rank_far"] or not isinstance(p["gap_days"], int))]
        for p in sorted(reserve, key=order)[:long_gap_budget]:
            kept[p["pair_id"]] = {**p, "rank_kept_as": "reserved"}
    out = sorted(kept.values(), key=lambda p: p["pair_id"])
    stats = {"before": len(scored), "after": len(out),
             "kept_as_partner": sum(1 for p in out if p["rank_kept_as"] == "partner"),
             "kept_as_reserved": sum(1 for p in out if p["rank_kept_as"] == "reserved"),
             "long_gap_before": sum(1 for p in scored if p["rank_far"]),
             "long_gap_after": sum(1 for p in out if p["rank_far"]),
             "unknown_gap_before": sum(1 for p in scored if not isinstance(p["gap_days"], int)),
             "unknown_gap_after": sum(1 for p in out if not isinstance(p["gap_days"], int)),
             "groups": len(groups), "largest_group_pairs": max((len(m) for m in groups.values()), default=0),
             "parameters": {"window_days": window_days, "partners_per_event": partners_per_event,
                            "long_gap_budget": long_gap_budget, "capped": cap}}
    logger.info("Candidate ranking kept %d of %d pair(s) (%d long-gap before, %d after)",
                stats["after"], stats["before"], stats["long_gap_before"], stats["long_gap_after"])
    return out, stats
