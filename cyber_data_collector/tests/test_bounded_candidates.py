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
    assert {k: counts[k] for k in ("before", "long_gap_before", "unknown_gap_before")} == {
        k: exhaustive[k] for k in ("before", "long_gap_before", "unknown_gap_before")}
    assert counts["events_at_retrieval_budget"] == 0 and 0 < counts["pairs_scored"] < counts["before"]


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


def test_a_group_up_to_the_uncapped_threshold_keeps_every_pair_and_a_larger_one_is_capped():
    spans = _spans(30, seed=21)                                  # no evidence at all: only the date ranks them
    corpus = _corpus(spans)
    capped, c_stats = bc.rank_groups(corpus, {0: spans}, _make)
    everything, e_stats = bc.rank_groups(corpus, {0: spans}, _make, uncapped_max=30)
    assert len(everything) == 30 * 29 // 2 and e_stats["uncapped_groups"] == 1
    assert len(capped) < len(everything) and c_stats["uncapped_groups"] == 0
    assert {p["pair_id"] for p in capped} <= {p["pair_id"] for p in everything}
    assert all(p["rank_kept_as"] == "partner" and "rank_score" in p for p in everything)
    # One event over the threshold and the cap applies again.
    assert bc.rank_groups(corpus, {0: spans}, _make, uncapped_max=29)[1]["uncapped_groups"] == 0
    assert e_stats["before"] == c_stats["before"] == 435 and e_stats["after"] == 435


# ---------------------------------------------------------------------------
# CPU and memory, not only paid calls: the interval index and bounded posting walks
# ---------------------------------------------------------------------------

def _random_spans(rng, n, undated_share=0.1, prefix="x"):
    """Events with day, month and year intervals, many on the same days and overlapping, some undated."""
    out = []
    for i in range(n):
        event_id = f"{prefix}{rng.randrange(10 ** 6):06d}{i:04d}"
        if rng.random() < undated_share:
            out.append(bc.Span(event_id, None, None))
            continue
        day = DAY0 + timedelta(days=rng.randrange(0, 400))
        kind = rng.random()
        if kind < 0.15:
            out.append(bc.Span(event_id, date(day.year, 1, 1), date(day.year, 12, 31)))
        elif kind < 0.4:
            first = day.replace(day=1)
            out.append(bc.Span(event_id, first, (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)))
        else:
            out.append(bc.Span(event_id, day, day + timedelta(days=rng.choice((0, 0, 0, 3)))))
    return out


def _sorting_nearest(span, members, k):
    """What ``select_bounded`` did before the index: sort the whole posting for every event that carries the key."""
    def key(s):
        g = bc.span_gap(span, s)
        return (g if g is not None else 10 ** 9, s.event_id)
    return [s.event_id for s in sorted(members, key=key)][:k]


@pytest.mark.parametrize("seed", range(12))
def test_the_interval_index_finds_exactly_the_nearest_the_full_sort_found(seed):
    rng = random.Random(seed)
    members = _random_spans(rng, rng.choice((5, 30, 120, 300)))
    index = bc.IntervalIndex(members)
    queries = members + _random_spans(rng, 20, prefix="q")          # members (self included) and outsiders
    for k in (1, 3, 24, len(members) + 5):
        for q in queries:
            assert index.nearest(q, k) == _sorting_nearest(q, members, k), (seed, k, q)


def test_the_adjacent_neighbours_are_the_start_and_end_order_slices():
    rng = random.Random(4)
    members = _random_spans(rng, 200)
    index = bc.IntervalIndex(members)
    by_start = sorted((s for s in members if s.start), key=bc.span_order)
    by_end = sorted(by_start, key=lambda s: (s.end, s.event_id))
    for s in by_start:
        i, j = by_start.index(s), by_end.index(s)
        expected = [x.event_id for x in by_start[max(0, i - 8):i] + by_start[i + 1:i + 9]
                    + by_end[max(0, j - 8):j] + by_end[j + 1:j + 9]]
        assert index.adjacent(s, 8) == expected
    assert index.adjacent(bc.Span("undated", None, None), 8) == []


def test_a_rare_word_many_events_carry_is_followed_only_to_its_nearest_and_its_undated_members():
    """A token carried by every event of a big group used to be unioned whole for every event (quadratic)."""
    spans = _spans(1200, undated=30, seed=13)
    corpus = _corpus(spans, rare={"everywhere"})
    for s in spans:
        corpus.features[s.event_id].tokens.add("everywhere")
    make = Counting()
    kept, counts = bc.select_bounded(corpus, spans, make)
    assert counts["pairs_scored"] < 1200 * (2 * bc.PER_KEY_LIMIT + 4 * 8 + 8)   # linear, not 1200 * 1199 / 2
    assert make.built == len(kept)                                           # only kept pairs are built
    assert counts["events_at_retrieval_budget"] == 0 and kept


def test_a_strong_undated_match_inside_long_postings_is_still_retrieved():
    spans = _spans(500, undated=10, seed=17)
    ids = [s.event_id for s in spans]
    dated = sorted((s for s in spans if s.start), key=bc.span_order)
    undated, partner = ids[0], dated[250].event_id
    rng = random.Random(9)
    one = {s.event_id for s in rng.sample(dated[:240], 99)} | {undated, partner}
    two = {s.event_id for s in rng.sample(dated[260:], 99)} | {undated, partner}   # they share only this pair
    corpus = _corpus(spans, rare={"alpha", "beta"})
    for i in one:
        corpus.features[i].tokens.add("alpha")
    for i in two:
        corpus.features[i].tokens.add("beta")
    kept, _ = bc.select_bounded(corpus, spans, _make)
    pair = {p["pair_id"]: p for p in kept}.get(bc.pair_id(undated, partner))
    assert pair is not None and pair["rank_strong"]                         # two shared rare words, unknown gap


def test_an_event_with_more_keys_than_the_budget_is_counted_not_hidden():
    spans = _spans(400, seed=19)
    corpus = _corpus(spans, rare={f"t{k}" for k in range(10)})
    for s in spans:
        corpus.features[s.event_id].tokens.update(f"t{k}" for k in range(10))   # ten long postings each
    _, counts = bc.select_bounded(corpus, spans, _make, retrieval_budget=2 * bc.PER_KEY_LIMIT)
    assert counts["events_at_retrieval_budget"] == 400
    _, stats = bc.rank_groups(corpus, {0: spans}, _make)
    assert stats["events_at_retrieval_budget"] == 0                          # the default budget holds them
