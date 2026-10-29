"""Ranking a large group's duplicate candidates without building every pair of it.

``candidate_ranking.select`` needs the pairs built first, so CPU and memory grew with the square of the largest
group. ``bounded_candidates`` retrieves a limited set of candidates per event through indexes, keeps the same best
partners and the same reserved route for strong long-gap / undated matches, and never builds a pair it does not keep.
"""

from __future__ import annotations

import random
import time
from datetime import date, timedelta

import pytest

from cyber_data_collector.dedup import bounded_candidates as bc
from cyber_data_collector.dedup import candidate_ranking as ranking
from cyber_data_collector.dedup.candidate_ranking import Corpus, EventFeatures

DAY0 = date(2024, 1, 1)


@pytest.fixture(autouse=True)
def bogus_keys(monkeypatch):
    # Nothing here may reach a model; a test that tried would fail on these keys instead of spending.
    for name in ("OPENAI_API_KEY", "TYPESAFE_API_KEY", "PERPLEXITY_API_KEY"):
        monkeypatch.setenv(name, "bogus-key-for-tests")


def _spans(n, undated=0, seed=3):
    """``n`` events on distinct days (so no two tie on the date), the first ``undated`` of them without a date."""
    rng = random.Random(seed)
    days = rng.sample(range(0, 6000), n)
    return [bc.Span(f"u{i:04d}", None, None) if i < undated else
            bc.Span(f"e{i:04d}", DAY0 + timedelta(days=days[i]), DAY0 + timedelta(days=days[i]))
            for i in range(n)]


def _make(a, b, gap, group):
    return {"pair_id": bc.pair_id(a.event_id, b.event_id), "a": a.event_id, "b": b.event_id,
            "gap_days": gap if gap is not None else "unknown", "group": group}


def _corpus(spans, families=(), rare=()):
    """Features for ``spans``; ``families`` are groups of event ids sharing a rare word (``rare`` are rare tokens)."""
    feats = {s.event_id: EventFeatures(s.event_id) for s in spans}
    for k, ids in enumerate(families):
        for i in ids:
            feats[i].tokens.add(f"word{k}")
    return Corpus(feats, {f"word{k}" for k in range(len(families))} | set(rare), set())


class Counting:
    """``make_pair`` that counts the pairs built."""

    def __init__(self):
        self.built = 0

    def __call__(self, a, b, gap, group):
        self.built += 1
        return _make(a, b, gap, group)


def test_a_large_group_is_ranked_without_building_its_pairs():
    spans = _spans(1500, undated=40)
    corpus = _corpus(spans)
    make = Counting()
    start = time.perf_counter()
    kept, counts = bc.select_bounded(corpus, spans, make)
    elapsed = time.perf_counter() - start
    n = len(spans)
    assert counts["before"] == n * (n - 1) // 2 == 1_124_250
    assert make.built < 30 * n                      # linear in the group, not the 1.1 million pairs
    assert len(kept) <= 8 * n * 2 and elapsed < 20
    assert all(p["rank_kept_as"] in ("partner", "reserved") for p in kept)


def test_the_counts_before_the_cap_equal_the_enumerated_ones():
    spans = _spans(120, undated=9)
    corpus = _corpus(spans)
    pairs = [_make(a, b, bc.span_gap(a, b), 0) for i, a in enumerate(sorted(spans, key=bc.span_order))
             for b in sorted(spans, key=bc.span_order)[i + 1:]]
    _, exhaustive = ranking.select(corpus, pairs, group_of=lambda p: 0)
    _, counts = bc.select_bounded(corpus, spans, _make)
    assert counts == {k: exhaustive[k] for k in ("before", "long_gap_before", "unknown_gap_before")}


def test_bounded_retrieval_keeps_exactly_what_exhaustive_ranking_keeps():
    """Rare-word families scattered over a group with undated events: with no two events on one day the bounded
    ranking is the exhaustive one, pair for pair, without building the pairs."""
    spans = _spans(400, undated=25, seed=11)
    ids = [s.event_id for s in spans]
    rng = random.Random(5)
    corpus = _corpus(spans, [rng.sample(ids, 3) for _ in range(40)])
    ordered = sorted(spans, key=bc.span_order)
    pairs = [_make(a, b, bc.span_gap(a, b), 0) for i, a in enumerate(ordered) for b in ordered[i + 1:]]
    exhaustive, _ = ranking.select(corpus, pairs, group_of=lambda p: 0)
    make = Counting()
    bounded, _ = bc.select_bounded(corpus, spans, make)
    assert [(p["pair_id"], p["rank_kept_as"]) for p in bounded] == [(p["pair_id"], p["rank_kept_as"]) for p in exhaustive]
    assert make.built < len(pairs) / 4


def test_a_strong_long_gap_or_undated_match_is_reserved_however_big_the_group():
    spans = _spans(600, undated=30, seed=7)
    ids = [s.event_id for s in spans]
    far = sorted((s for s in spans if s.start), key=lambda s: s.start)
    early, late = far[0].event_id, far[-1].event_id             # years apart
    undated = ids[0]
    corpus = _corpus(spans, [[early, late], [undated, far[300].event_id]])
    corpus.features[early].urls.add("news.example/a")
    corpus.features[late].urls.add("news.example/a")            # the same article, years apart
    kept, _ = bc.select_bounded(corpus, spans, _make)
    by_id = {p["pair_id"]: p for p in kept}
    assert by_id[bc.pair_id(early, late)]["rank_kept_as"] == "reserved"
    assert bc.pair_id(undated, far[300].event_id) in by_id      # an undated report with a rare word in common


def test_the_hub_url_rule_still_holds():
    spans = _spans(80)
    corpus = _corpus(spans)
    a, b = spans[0].event_id, spans[1].event_id
    for i in (a, b):
        corpus.features[i].urls.add("news.example/roundup")
    corpus.hub_urls.add("news.example/roundup")                  # cited by many events: a list, not an article
    kept, _ = bc.select_bounded(corpus, spans, _make, partners_per_event=2)
    assert all(not p["rank_reasons"] or "shared source" not in " ".join(p["rank_reasons"]) for p in kept)


def test_a_small_group_is_ranked_exactly_as_before():
    spans = _spans(40, undated=4)
    corpus = _corpus(spans)
    kept, stats = bc.rank_groups(corpus, {0: spans}, _make)
    ordered = sorted(spans, key=bc.span_order)
    pairs = [_make(a, b, bc.span_gap(a, b), 0) for i, a in enumerate(ordered) for b in ordered[i + 1:]]
    expected, expected_stats = ranking.select(corpus, pairs, group_of=lambda p: 0)
    assert [p["pair_id"] for p in kept] == [p["pair_id"] for p in expected]
    assert stats["bounded_groups"] == 0 and stats["before"] == expected_stats["before"]


def test_a_large_group_goes_through_bounded_retrieval_and_the_caller_can_force_either_path():
    spans = _spans(90)
    corpus = _corpus(spans)
    make = Counting()
    _, stats = bc.rank_groups(corpus, {0: spans}, make)
    assert stats["bounded_groups"] == 1 and make.built < 90 * 89 // 2
    make = Counting()
    _, stats = bc.rank_groups(corpus, {0: spans}, make, exhaustive_max=1000)
    assert stats["bounded_groups"] == 0 and make.built == 90 * 89 // 2
    make = Counting()
    bc.rank_groups(corpus, {0: spans}, make, cap=False)          # asked for every pair: every pair is built
    assert make.built == 90 * 89 // 2


def test_pairs_another_route_owns_are_excluded_in_both_paths():
    spans = _spans(90)
    corpus = _corpus(spans)
    owned = lambda a, b: a.endswith("0") and b.endswith("0")     # noqa: E731
    for limit in (1000, 10):
        kept, _ = bc.rank_groups(corpus, {0: spans}, _make, exclude=owned, exhaustive_max=limit)
        assert not any(owned(p["a"], p["b"]) for p in kept)


def test_a_pair_two_groups_both_produce_is_kept_once():
    spans = _spans(30)
    kept, _ = bc.rank_groups(_corpus(spans), {"g1": spans, "g2": spans}, _make)
    assert len({p["pair_id"] for p in kept}) == len(kept)


def test_subdivide_ranks_a_large_bucket_of_records_by_retrieval():
    class Record:
        def __init__(self, i, day):
            self.enriched_event_id = f"r{i:04d}"
            self.day = day

    rng = random.Random(2)
    members = [Record(i, DAY0 + timedelta(days=rng.randrange(0, 900))) for i in range(300)]
    corpus = _corpus([bc.Span(m.enriched_event_id, m.day, m.day) for m in members])

    def gap_of(l, r):
        raise AssertionError("with day_of the gap is computed from the dates, not asked for")

    pairs = ranking.subdivide(corpus, members, gap_of, day_of=lambda m: m.day)
    assert 0 < len(pairs) <= 300 * 8 * 2
    legacy = ranking.subdivide(corpus, members[:20], lambda l, r: abs((l.day - r.day).days))   # unchanged callers
    assert legacy


@pytest.mark.parametrize("ppe", [0, -1])
def test_bounded_retrieval_needs_a_positive_cap(ppe):
    with pytest.raises(ValueError):
        bc.select_bounded(_corpus(_spans(5)), _spans(5), _make, partners_per_event=ppe)
