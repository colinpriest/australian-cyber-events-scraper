"""Measure duplicate-candidate generation: pairs built, time, peak memory, and recall on the labelled pairs.

``check-duplicates`` only asks a ranked, capped subset of the pairs of events that share an organisation (or, for
the AI route, are AI-involved and near in time). It used to build every pair of a group before the cap threw most
away, so CPU and memory grew with the square of the largest group. This script runs both the way it was
(``enumerate``: every pair built, then :func:`candidate_ranking.select`) and the way it is
(``retrieve``: :mod:`bounded_candidates`; ``retrieve-forced`` also sends small groups through retrieval), on a
database copy, and reports for each route

* groups, the largest group, and the pairs that exist (arithmetically);
* pairs built, pairs kept, wall time, peak Python memory (``tracemalloc``);
* **recall on the labelled pairs**: of the labelled pairs that exist as candidates in this database
  (``evaluation/duplicate_labels.csv`` / ``evaluation/ai_duplicate_labels.csv``), how many each path keeps, for
  ``same`` and ``different``, and the labelled ``same`` pairs a path loses against the enumerated one;
* how the kept sets differ from the enumerated one.

Nothing is asked of any model and nothing is written to the database (it is opened read-only). Run it on the
labelling-time snapshot (``instance/calibration_duplicates.db``, ``instance/calibration_ai_duplicates.db``), where
the labelled pairs still exist, and on a copy of the live database for the sizes:

    python scripts/measure_candidate_generation.py --db D:/tmp/copy.db
    python scripts/measure_candidate_generation.py --db instance/calibration_duplicates.db --route organisation
    python scripts/measure_candidate_generation.py --synthetic 2000      # one group of 2,000 events, no database
    python scripts/measure_candidate_generation.py --synthetic 20000 --no-enumeration
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
import tracemalloc
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.dedup import ai_duplicates as ad  # noqa: E402
from cyber_data_collector.dedup import bounded_candidates as bounded  # noqa: E402
from cyber_data_collector.dedup import candidate_ranking as ranking  # noqa: E402
from cyber_data_collector.dedup import duplicate_judgement as dj  # noqa: E402

PATHS = ("enumerate", "retrieve", "retrieve-forced")


class Counter:
    """Wraps a pair builder, counting the pairs it builds."""

    def __init__(self, make: Callable):
        self.make, self.built = make, 0

    def __call__(self, *args, **kwargs):
        self.built += 1
        return self.make(*args, **kwargs)


def _measured(run: Callable[[], Any]) -> Tuple[Any, float, float]:
    """``(result, seconds, peak MiB)`` of ``run()``."""
    tracemalloc.start()
    started = time.perf_counter()
    result = run()
    seconds = time.perf_counter() - started
    peak = tracemalloc.get_traced_memory()[1] / (1024 * 1024)
    tracemalloc.stop()
    return result, seconds, peak


def labels_of(route: str, path: Optional[str]) -> Dict[str, str]:
    if route == "organisation":
        return dj.labelled_pairs(Path(path) if path else dj.LABELS_PATH)
    return ad.labelled_pairs(Path(path) if path else ad.LABELS_PATH)


def recall(kept: Set[str], exists: Set[str], labels: Dict[str, str]) -> Dict[str, int]:
    out = {"labelled_existing_same": 0, "kept_same": 0, "labelled_existing_different": 0, "kept_different": 0}
    for pid, label in labels.items():
        if label in ("same", "different") and pid in exists:
            out[f"labelled_existing_{label}"] += 1
            out[f"kept_{label}"] += pid in kept
    return out


def _route_runner(conn: sqlite3.Connection, route: str, corpus) -> Dict[str, Callable[[], Tuple[List[Dict], Dict, int]]]:
    """For each path, a function returning ``(kept pairs, stats, pairs built)``; plus ``exists``, every pair."""
    if route == "organisation":
        universe = dj.PairUniverse(conn)
        make_attr = "_make"

        def enumerate_() -> Tuple[List[Dict], Dict, int]:
            pairs = universe.all_pairs()
            kept, stats = ranking.select(corpus, pairs, group_of=lambda p: p["group"])
            return kept, stats, len(pairs)

        def retrieve(exhaustive_max: Optional[int]) -> Callable[[], Tuple[List[Dict], Dict, int]]:
            def run() -> Tuple[List[Dict], Dict, int]:
                counter = Counter(universe._make)
                setattr(universe, make_attr, counter)
                try:
                    kept, stats = universe.ranked(corpus, exhaustive_max=exhaustive_max)
                finally:
                    delattr(universe, make_attr)
                return kept, stats, counter.built
            return run

        groups = {gid: len(spans) for gid, spans in universe.groups.items()}
        exists = lambda: {p["pair_id"] for p in universe.all_pairs()}      # noqa: E731
    else:
        universe = ad.AIUniverse(conn)

        def enumerate_() -> Tuple[List[Dict], Dict, int]:
            pairs = universe.all_pairs()
            kept, stats = ranking.select(corpus, pairs, group_of=lambda p: ad.ROUTE_AI,
                                         partners_per_event=ad.AI_PARTNERS_PER_EVENT)
            return kept, stats, len(pairs)

        def retrieve(exhaustive_max: Optional[int]) -> Callable[[], Tuple[List[Dict], Dict, int]]:
            def run() -> Tuple[List[Dict], Dict, int]:
                counter = Counter(universe._make)
                universe._make = counter                                     # type: ignore[method-assign]
                try:
                    kept, stats = universe.ranked(corpus, exhaustive_max=exhaustive_max)
                finally:
                    del universe._make
                return kept, stats, counter.built
            return run

        groups = {ad.ROUTE_AI: len(universe.events)}
        exists = lambda: {p["pair_id"] for p in universe.all_pairs()}      # noqa: E731
    return {"enumerate": enumerate_, "retrieve": retrieve(None), "retrieve-forced": retrieve(0),
            "_groups": groups, "_exists": exists}                                  # type: ignore[dict-item]


def measure_database(db: str, route: str, labels_path: Optional[str] = None) -> Dict[str, Any]:
    conn = sqlite3.connect(f"file:{Path(db).resolve().as_posix()}?mode=ro", uri=True)
    corpus = ranking.load_corpus(conn)
    runners = _route_runner(conn, route, corpus)
    groups = runners.pop("_groups")                                                # type: ignore[arg-type]
    exists_fn = runners.pop("_exists")                                             # type: ignore[arg-type]
    labels = labels_of(route, labels_path)
    exists = exists_fn()                                                           # type: ignore[operator]
    sizes = sorted(groups.values(), reverse=True)
    out: Dict[str, Any] = {
        "route": route, "database": Path(db).name, "events_in_groups": sum(sizes), "groups": len(sizes),
        "largest_groups": sizes[:5], "pairs_that_exist": len(exists),
        "labelled_pairs": sum(1 for v in labels.values() if v in ("same", "different")),
        "labelled_pairs_existing_here": sum(1 for pid, v in labels.items() if v in ("same", "different") and pid in exists),
        "paths": {}}
    kept_by_path: Dict[str, Set[str]] = {}
    for name in PATHS:
        (kept, stats, built), seconds, peak = _measured(runners[name])             # type: ignore[arg-type]
        ids = {p["pair_id"] for p in kept}
        kept_by_path[name] = ids
        out["paths"][name] = {"pairs_built": built, "pairs_kept": len(ids), "seconds": round(seconds, 3),
                              "peak_mib": round(peak, 2), "bounded_groups": stats.get("bounded_groups", 0),
                              "recall": recall(ids, exists, labels)}
    base = kept_by_path["enumerate"]
    for name in PATHS[1:]:
        ids = kept_by_path[name]
        lost = sorted(pid for pid in base - ids)
        out["paths"][name]["only_in_enumerated"] = len(lost)
        out["paths"][name]["only_in_this_path"] = len(ids - base)
        out["paths"][name]["labelled_same_lost_against_enumerated"] = sorted(
            pid for pid in lost if labels.get(pid) == "same")
    return out


def synthetic(n: int, seed: int = 7, with_enumeration: bool = True) -> Dict[str, Any]:
    """One group of ``n`` events (5% undated, 10% sharing a rare word with two others): the enumerated path against
    retrieval, on a group too big for the database to show the growth."""
    rng = random.Random(seed)
    days = rng.sample(range(0, max(6000, n * 3)), n)
    start = date(2015, 1, 1)
    spans = [bounded.Span(f"e{i:06d}", None, None) if i < n // 20 else
             bounded.Span(f"e{i:06d}", start + timedelta(days=days[i]), start + timedelta(days=days[i]))
             for i in range(n)]
    ids = [s.event_id for s in spans]
    feats = {i: ranking.EventFeatures(i) for i in ids}
    families = [rng.sample(ids, 3) for _ in range(n // 10)]
    for k, family in enumerate(families):
        for i in family:
            feats[i].tokens.add(f"word{k}")
    corpus = ranking.Corpus(feats, {f"word{k}" for k in range(len(families))}, set())

    def make(a, b, gap, group):
        return {"pair_id": bounded.pair_id(a.event_id, b.event_id), "a": a.event_id, "b": b.event_id,
                "gap_days": gap if gap is not None else "unknown"}

    def enumerate_():
        ordered = sorted(spans, key=bounded.span_order)
        pairs = [make(a, b, bounded.span_gap(a, b), 0) for i, a in enumerate(ordered) for b in ordered[i + 1:]]
        kept, _ = ranking.select(corpus, pairs, group_of=lambda p: 0)
        return kept, len(pairs)

    def retrieve():
        counter = Counter(make)
        kept, _ = bounded.rank_groups(corpus, {0: spans}, counter)
        return kept, counter.built

    out: Dict[str, Any] = {"events": n, "pairs_that_exist": n * (n - 1) // 2}
    kept_ids: Dict[str, Set[str]] = {}
    for name, fn in (("retrieve", retrieve), ("enumerate", enumerate_)):
        if name == "enumerate" and not with_enumeration:
            continue
        (kept, built), seconds, peak = _measured(fn)
        kept_ids[name] = {p["pair_id"] for p in kept}
        out[name] = {"pairs_built": built, "pairs_kept": len(kept), "seconds": round(seconds, 2),
                     "peak_mib": round(peak, 1)}
    if with_enumeration:
        out["only_in_enumerated"] = len(kept_ids["enumerate"] - kept_ids["retrieve"])
        out["only_in_retrieved"] = len(kept_ids["retrieve"] - kept_ids["enumerate"])
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", help="database to measure (opened read-only; use a copy or a snapshot)")
    parser.add_argument("--route", choices=("organisation", "ai", "both"), default="both")
    parser.add_argument("--labels", help="labels file for --route organisation or ai (default: the committed one)")
    parser.add_argument("--synthetic", type=int, metavar="N", help="measure one group of N synthetic events instead")
    parser.add_argument("--no-enumeration", action="store_true",
                        help="with --synthetic, do not also build every pair (it needs gigabytes beyond ~3,000 events)")
    parser.add_argument("--save", help="write the result as JSON")
    args = parser.parse_args(argv)
    if args.synthetic:
        result: Any = synthetic(args.synthetic, with_enumeration=not args.no_enumeration)
    elif args.db:
        routes = ("organisation", "ai") if args.route == "both" else (args.route,)
        result = [measure_database(args.db, r, args.labels) for r in routes]
    else:
        parser.error("give --db or --synthetic")
    text = json.dumps(result, indent=2)
    print(text)
    if args.save:
        Path(args.save).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
