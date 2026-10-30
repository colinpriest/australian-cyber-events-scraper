"""Measure what overlapping neighbourhoods recover that disjoint slices miss, on a copy of the database.

No model is called. The adjudicator is a stand-in that partitions whatever it is shown by a *known key*, so
the only thing measured is the planning and reconciliation: which groups of events that share the key end up in
one incident, with the old method (the linked groups cut into disjoint slices of at most ``--max-cluster-size``
events, ``candidate_components``) and the new one (bounded, overlapping neighbourhoods whose answers are
reconciled, ``neighbourhoods``).

Keys (what "the same incident" means for this measurement):

* ``vendor`` - events recording the same supplier organisation;
* ``source`` - events chained together by non-hub source articles they share.

Candidate edges are the strong ones that need no embeddings (a shared non-hub article or a corpus-rare shared
subject word), so the run makes no paid call. The database is opened read-only; use a copy.

Usage:
    python scripts/measure_neighbourhoods.py --db D:/tmp/copy.db [--key vendor|source] [--out result.json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cyber_data_collector.dedup import candidate_ranking as ranking  # noqa: E402
from cyber_data_collector.dedup import neighbourhoods as nb  # noqa: E402
from cyber_data_collector.dedup.adjudicator import Adjudicator, EventRecord  # noqa: E402
from cyber_data_collector.dedup.candidates import candidate_components  # noqa: E402
from cyber_data_collector.dedup.cluster_adjudicator import (  # noqa: E402
    ClusterAdjudicator,
    ClusterPartition,
    IncidentGroup,
)
from cyber_data_collector.dedup.entity_resolution import EntityResolver  # noqa: E402


class KeyedAdjudicator(ClusterAdjudicator):
    """Partitions the records it is shown by a known key (records with no key stay alone). Makes no model call.

    Counts a call for every partition it makes, and keeps the real adjudicator's call budget, so the figures are
    those a model-backed run would spend.
    """

    def __init__(self, key_of: Dict[str, Optional[str]], certainty: float = 0.95, **kwargs: Any):
        super().__init__(openai_client=object(), **kwargs)
        self.key_of = key_of
        self.certainty = certainty
        self.partitions: List[List[str]] = []

    def answer(self, records: Sequence[EventRecord]) -> Optional[ClusterPartition]:
        if self.call_budget is not None and self.stats["llm_calls"] >= self.call_budget:
            self.stats["budget_skipped"] += 1
            return None
        self.stats["llm_calls"] += 1
        by_key: Dict[str, List[int]] = defaultdict(list)
        groups: List[IncidentGroup] = []
        for index, record in enumerate(records, start=1):
            key = self.key_of.get(record.enriched_event_id)
            if key is None:
                groups.append(IncidentGroup(label=f"alone {index}", member_indices=[index], certainty=self.certainty,
                                            reasoning="no key"))
            else:
                by_key[key].append(index)
        for key, indices in by_key.items():
            groups.append(IncidentGroup(label=key, member_indices=indices, certainty=self.certainty,
                                        reasoning=f"known key {key}"))
        return ClusterPartition(incidents=groups)


def vendor_key(conn: sqlite3.Connection, ids: Set[str]) -> Dict[str, Optional[str]]:
    keys: Dict[str, Optional[str]] = {}
    for did, vendor in conn.execute("SELECT deduplicated_event_id, vendor_organization_name FROM DeduplicatedEvents "
                                    "WHERE COALESCE(status, 'Active') = 'Active'"):
        norm = ranking._norm(vendor)
        keys[did] = norm or None
    return {i: keys.get(i) for i in ids}


def source_key(records: Sequence[EventRecord], hubs: Set[str]) -> Dict[str, Optional[str]]:
    """Events chained by a shared non-hub article share a key (the smallest event id of the chain)."""
    from cyber_data_collector.dedup import candidates as candidate_keys

    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_url: Dict[str, List[str]] = defaultdict(list)
    for r in records:
        for u in r.all_urls():
            norm = candidate_keys.normalise_url(u)
            if norm and norm not in hubs:
                by_url[norm].append(r.enriched_event_id)
    for ids in by_url.values():
        for other in ids[1:]:
            parent[find(other)] = find(ids[0])
    groups: Dict[str, List[str]] = defaultdict(list)
    for node in list(parent):
        groups[find(node)].append(node)
    keys: Dict[str, Optional[str]] = {r.enriched_event_id: None for r in records}
    for members in groups.values():
        if len(members) >= 2:
            for m in members:
                keys[m] = min(members)
    return keys


def run(db: str, key: str, max_size: int, neighbours: int, call_budget: int,
        supplier_links: bool = False) -> Dict[str, Any]:
    conn = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        from scripts.dedup_admin import load_dedup_records

        records = load_dedup_records(conn)
        by_id = {r.enriched_event_id: r for r in records}
        judge_pairs = Adjudicator(resolver=EntityResolver(conn))
        judge_pairs.candidate_pairs(records)
        pair_reasons = judge_pairs.pair_reasons

        def strong(k, why) -> bool:
            return any(w.startswith(("shared source", "shared subject")) for w in why)

        def weight(k, why) -> float:
            return 2.0 if any(w.startswith("shared source") for w in why) else 1.0

        edges = [tuple(sorted(k)) for k, why in pair_reasons.items() if strong(k, why)]
        corpus = ranking.load_corpus(conn)
        old_edges = len(edges)
        suppliers_subdivided = 0
        if supplier_links:
            extra, suppliers_subdivided = nb.supplier_pairs(
                corpus, _intervals(conn, {i for i, f in corpus.features.items() if f.vendor}))
            edges = sorted(set(edges) | {tuple(sorted(e)) for e in extra})
        nodes = {i for e in edges for i in e}
        key_of = (vendor_key(conn, set(by_id)) if key == "vendor" else source_key(records, judge_pairs.hub_urls))
        intervals = _intervals(conn, nodes)
        links = nb.build_links(edges, corpus, lambda a, b: nb.interval_gap(intervals.get(a), intervals.get(b)),
                               keep_all=True)

        # The truth: events sharing a key, restricted to those candidate generation linked at all.
        truth: Dict[str, Set[str]] = defaultdict(set)
        for i in nodes:
            if key_of.get(i):
                truth[key_of[i]].add(i)
        truth = {k: v for k, v in truth.items() if len(v) >= 2}

        components = nb._components(links)
        component_of = {n: idx for idx, c in enumerate(components) for n in c}
        large = {idx for idx, c in enumerate(components) if len(c) > max_size}

        # Old: disjoint slices.
        slices = candidate_components(pair_reasons, strong, weight=lambda k, w: weight(k, w), max_size=max_size)
        old_judge = KeyedAdjudicator(key_of, max_cluster_size=max_size, call_budget=call_budget)
        old_incident_of: Dict[str, int] = {}
        slice_of: Dict[str, int] = {}
        next_id = 0
        for sl_index, ids in enumerate(slices):
            for i in ids:
                slice_of[i] = sl_index
            cluster = sorted((by_id[i] for i in ids), key=lambda r: r.event_date or "9999")
            part = old_judge.adjudicate_cluster(cluster)
            for group, member_ids in part.groups_as_ids(cluster):
                if len(member_ids) >= 2:
                    for m in member_ids:
                        old_incident_of[m] = next_id
                    next_id += 1

        # New: overlapping neighbourhoods, answers reconciled.
        plan = nb.plan(links, max_size=max_size, neighbours=neighbours)
        new_judge = KeyedAdjudicator(key_of, max_cluster_size=max_size, call_budget=call_budget)
        result = new_judge.adjudicate_plan(plan, by_id, links)
        new_incident_of = {m: idx for idx, inc in enumerate(result.incidents) for m in inc.ids}

        rows = []
        for k, members in sorted(truth.items(), key=lambda kv: -len(kv[1])):
            comps = {component_of[m] for m in members}
            old_slices = {slice_of.get(m) for m in members}
            in_large = any(c in large for c in comps)
            crosses_slice = len(old_slices) > 1 or None in old_slices
            rows.append({
                "key": k, "events": len(members), "linked_components": len(comps), "in_large_group": in_large,
                "crosses_slices_in_old": crosses_slice,
                "recovered_old": len({old_incident_of.get(m, f"x{m}") for m in members}) == 1,
                "recovered_new": len({new_incident_of.get(m, f"x{m}") for m in members}) == 1,
            })

        def count(pred) -> int:
            return sum(1 for r in rows if pred(r))

        reachable = [r for r in rows if r["linked_components"] == 1]
        impure = sum(1 for inc in result.incidents if len({key_of.get(m) for m in inc.ids}) > 1)
        return {
            "key": key, "database": str(db), "events": len(records), "linked_events": len(nodes),
            "strong_edges": old_edges, "edges_with_supplier_links": len(edges), "supplier_links": supplier_links,
            "suppliers_subdivided": suppliers_subdivided,
            "max_size": max_size, "neighbours": neighbours,
            "bucket_sizes": {"groups": len(components), "largest": [len(c) for c in components[:10]],
                             "larger_than_max_size": len(large),
                             "events_in_those": sum(len(components[i]) for i in large)},
            "truth_groups": len(rows),
            "truth_groups_in_one_linked_group": len(reachable),
            "truth_groups_in_large_groups": count(lambda r: r["in_large_group"]),
            "truth_groups_cross_slice_in_old": count(lambda r: r["crosses_slices_in_old"]),
            "recovered_old_all": count(lambda r: r["recovered_old"]),
            "recovered_new_all": count(lambda r: r["recovered_new"]),
            "recovered_old_cross_slice": count(lambda r: r["crosses_slices_in_old"] and r["recovered_old"]),
            "recovered_new_cross_slice": count(lambda r: r["crosses_slices_in_old"] and r["recovered_new"]),
            "recovered_old_in_large": count(lambda r: r["in_large_group"] and r["recovered_old"]),
            "recovered_new_in_large": count(lambda r: r["in_large_group"] and r["recovered_new"]),
            "impure_incidents_new": impure,
            "old": {"slices": len(slices), "calls": old_judge.stats["llm_calls"],
                    "budget_skipped": old_judge.stats["budget_skipped"],
                    "links_shown_together": sum(1 for k in links if any(k <= set(sl) for sl in slices)),
                    "links": len(links)},
            "new": {"plan": plan.stats(), "calls": new_judge.stats["llm_calls"], "call_budget": call_budget,
                    "budget_skipped": new_judge.stats["budget_skipped"], "reconciliation": result.stats()},
            "rows": rows,
        }
    finally:
        conn.close()


def _intervals(conn: sqlite3.Connection, ids: Set[str]) -> Dict[str, Any]:
    from datetime import date

    have = {r[1] for r in conn.execute("PRAGMA table_info(DeduplicatedEvents)")}
    early = "event_date_earliest" if "event_date_earliest" in have else "NULL"
    late = "event_date_latest" if "event_date_latest" in have else "NULL"

    def day(v):
        try:
            return date.fromisoformat(str(v)[:10]) if v else None
        except ValueError:
            return None

    out: Dict[str, Any] = {}
    for did, d, lo, hi in conn.execute(f"SELECT deduplicated_event_id, event_date, {early}, {late} FROM DeduplicatedEvents"):
        if did not in ids:
            continue
        point, start, end = day(d), day(lo), day(hi)
        out[did] = (start, end) if start and end and start <= end else ((point, point) if point else None)
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="a COPY of the database (opened read-only)")
    parser.add_argument("--key", choices=("vendor", "source"), default="vendor")
    parser.add_argument("--max-cluster-size", type=int, default=nb.MAX_NEIGHBOURHOOD_SIZE)
    parser.add_argument("--neighbours", type=int, default=nb.NEIGHBOURS_PER_EVENT)
    parser.add_argument("--call-budget", type=int, default=nb.CALL_BUDGET)
    parser.add_argument("--supplier-links", action="store_true",
                        help="also link events recording the same supplier (circular with --key vendor); the old "
                             "method never had these edges, so they are used by the new method only")
    parser.add_argument("--out", default=None, help="write the full result (with one row per truth group) as JSON")
    args = parser.parse_args(argv)
    result = run(args.db, args.key, args.max_cluster_size, args.neighbours, args.call_budget, args.supplier_links)
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    summary = {k: v for k, v in result.items() if k != "rows"}
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
