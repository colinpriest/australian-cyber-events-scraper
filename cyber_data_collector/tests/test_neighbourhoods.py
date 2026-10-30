"""Matching across large incident groups with bounded, overlapping neighbourhoods.

Reviewer finding: ``cluster_adjudicator`` skipped a bucket of more than twice the size limit and cut other
oversized groups into disjoint slices, so reports of one supplier incident on opposite sides of a cut were never
judged together. Covered here (no network and no model: the adjudicator is a stand-in that partitions by a known
key, and an autouse fixture sets bogus API keys so a call that reached a paid service would fail):

* planning: a group that fits is one neighbourhood; a larger one is covered by overlapping neighbourhoods of at
  most the size limit, every strong link inside at least one, and the old slicing demonstrably splits a link;
* reconciliation: overlapping answers join only where no neighbourhood separates the events and no ruling or
  label forbids it; a shared event goes to the consistent side; the certainty is the lowest on the way;
* the explicit call budget: spent calls are counted and reported, stored answers are free, and the next run
  continues;
* a merge keeps every customer victim recorded on the event merged away.
"""
from __future__ import annotations

import re
import sqlite3
from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest

from cyber_data_collector.dedup import adjudication_cache as stored
from cyber_data_collector.dedup import candidate_ranking as ranking
from cyber_data_collector.dedup import label_contradictions as lc
from cyber_data_collector.dedup import neighbourhoods as nb
from cyber_data_collector.dedup.adjudicator import EventRecord
from cyber_data_collector.dedup.candidates import candidate_components
from cyber_data_collector.dedup.cluster_adjudicator import (
    ClusterAdjudicator,
    ClusterPartition,
    IncidentGroup,
    partition_from_incidents,
)
from cyber_data_collector.dedup.entity_resolution import EntityResolver


@pytest.fixture(autouse=True)
def bogus_keys(monkeypatch):
    for name in ("OPENAI_API_KEY", "PERPLEXITY_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.setenv(name, "bogus-not-a-key")


def ids(n: int, prefix: str = "e") -> List[str]:
    return [f"{prefix}{i:02d}" for i in range(n)]


def chain_links(nodes: List[str], score: float = 2.0) -> Dict[nb.Pair, nb.Link]:
    return {nb.pair_key(a, b): nb.Link(*sorted((a, b)), score, ("shared source x",))
            for a, b in zip(nodes, nodes[1:])}


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

def test_interval_gap_is_zero_for_overlap_and_unknown_without_a_date():
    from datetime import date
    a, b, c = (date(2024, 1, 1), date(2024, 1, 31)), (date(2024, 1, 20), date(2024, 2, 5)), (date(2024, 3, 1), date(2024, 3, 1))
    assert nb.interval_gap(a, b) == 0
    assert nb.interval_gap(a, c) == 30 and nb.interval_gap(c, a) == 30
    assert nb.interval_gap(a, None) is None


def test_links_score_source_supplier_rare_words_and_dates():
    features = {
        "a": ranking.EventFeatures("a", {"proctoru"}, {"x.com/p"}, "proctoru", "", 100),
        "b": ranking.EventFeatures("b", {"proctoru"}, {"x.com/p"}, "proctoru", "", 100),
        "c": ranking.EventFeatures("c", {"other"}, set(), "", "", None),
    }
    corpus = ranking.Corpus(features, {"proctoru"}, set())
    links = nb.build_links([("a", "b"), ("a", "c")], corpus, lambda x, y: 10, require_identity=True)
    assert set(links) == {nb.pair_key("a", "b")}            # a and c are only close in time
    ab = links[nb.pair_key("a", "b")]
    assert ab.score > 8 and any(r.startswith("shared source") for r in ab.reasons)         and any("supplier" in r for r in ab.reasons)
    close = nb.build_links([("a", "c")], corpus, lambda x, y: 10)
    assert nb.pair_key("a", "c") in close                   # without the requirement, closeness alone is a link


def test_a_candidate_the_ranking_cannot_score_is_kept_when_the_caller_screened_it():
    corpus = ranking.Corpus({"a": ranking.EventFeatures("a"), "b": ranking.EventFeatures("b")}, set(), set())
    assert nb.build_links([("a", "b")], corpus, lambda x, y: None) == {}
    kept = nb.build_links([("a", "b")], corpus, lambda x, y: None, keep_all=True)
    assert kept[nb.pair_key("a", "b")].score == nb.MIN_LINK_SCORE
    boosted = nb.build_links([("a", "b")], corpus, lambda x, y: None, bonus=lambda x, y: 0.7)
    assert boosted[nb.pair_key("a", "b")].score == 0.7


def test_supplier_pairs_pair_every_report_of_a_supplier():
    features = {f"e{i}": ranking.EventFeatures(f"e{i}", vendor="canvas" if i < 4 else ("" if i < 6 else "x"))
                for i in range(8)}
    corpus = ranking.Corpus(features, set(), set())
    pairs, subdivided = nb.supplier_pairs(corpus, {})
    assert len(pairs) == 7 and subdivided == 0       # four canvas reports (6 pairs) and two reports of x
    assert nb.supplier_pairs(corpus, {}, ids=["e0", "e1", "e7"])[0] == [("e0", "e1")]


def test_a_supplier_with_many_reports_is_subdivided_by_date_not_skipped():
    """A supplier above the exhaustive limit used to be skipped, so its reports were never linked by the clue."""
    from datetime import date, timedelta

    n = 400
    features = {f"e{i:03d}": ranking.EventFeatures(f"e{i:03d}", vendor="bigvendor") for i in range(n)}
    features["solo"] = ranking.EventFeatures("solo", vendor="other")
    corpus = ranking.Corpus(features, set(), set())
    day0 = date(2024, 1, 1)
    # Reverse date order against the ids, every tenth report undated.
    intervals = {f"e{i:03d}": None if i % 10 == 0 else (day0 + timedelta(days=n - i), day0 + timedelta(days=n - i))
                 for i in range(n)}
    pairs, subdivided = nb.supplier_pairs(corpus, intervals, max_group=100, neighbours=3)
    assert subdivided == 1
    assert len(pairs) < n * 4 * 3                                   # linear in the reports, not 400 * 399 / 2
    linked = {frozenset(p) for p in pairs}
    assert frozenset(("e001", "e002")) in linked                     # neighbours in date
    assert frozenset(("e001", "e399")) not in linked                 # a year apart
    assert all(any("e%03d" % i in p for p in pairs) for i in range(n))     # nothing is left out, undated included
    # Linked through each other: one connected component.
    parent = {i: i for i in features if i != "solo"}

    def find(x):
        while parent[x] != x:
            x = parent[x]
        return x
    for a, b in pairs:
        parent[find(a)] = find(b)
    assert len({find(i) for i in parent}) == 1
    small, none_subdivided = nb.supplier_pairs(corpus, intervals, max_group=n)
    assert none_subdivided == 0 and len(small) == n * (n - 1) // 2


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def test_a_group_that_fits_is_one_neighbourhood():
    plan = nb.plan(chain_links(ids(6)), max_size=20)
    assert [(n.members, n.is_whole_group) for n in plan.neighbourhoods] == [(tuple(ids(6)), True)]
    assert plan.large_groups == 0 and plan.covered_links == plan.required_links == 5


def test_a_larger_group_is_covered_by_overlapping_neighbourhoods_within_the_size_limit():
    nodes = ids(60)
    links = chain_links(nodes)
    plan = nb.plan(links, max_size=20, neighbours=4)
    assert plan.large_groups == 1 and plan.events_in_large_groups == 60
    assert all(len(n.members) <= 20 for n in plan.neighbourhoods) and len(plan.neighbourhoods) > 1
    assert plan.covered_links == plan.required_links == 59
    assert plan.links_total == plan.links_shown == 59
    for key in links:       # every link is shown to the model together in at least one neighbourhood
        assert any(key <= set(n.members) for n in plan.neighbourhoods)
    # Neighbourhoods overlap (that is what lets the answers be reconciled).
    assert len(set().union(*(set(n.members) for n in plan.neighbourhoods))) == 60
    assert sum(len(n.members) for n in plan.neighbourhoods) > 60


def test_the_old_disjoint_slices_split_a_link_the_neighbourhoods_keep():
    nodes = ids(30)
    links = chain_links(nodes)
    reasons = {key: {"shared subject 'w'"} for key in links}
    slices = candidate_components(reasons, lambda k, w: True, max_size=20)
    split = [key for key in links if not any(key <= set(s) for s in slices)]
    assert split, "disjoint slices of at most 20 cannot hold every link of a chain of 30"
    plan = nb.plan(links, max_size=20)
    for key in split:
        assert any(key <= set(n.members) for n in plan.neighbourhoods)


def test_focus_limits_what_must_be_covered_to_links_touching_recent_events():
    links = {**chain_links(ids(6)), **chain_links(ids(6, "f"))}
    plan = nb.plan(links, max_size=20, focus={"f03"})
    assert [n.members for n in plan.neighbourhoods] == [tuple(ids(6, "f"))]
    assert plan.required_links == 2          # the two links of f03; the rest of that group is context


def test_neighbourhoods_are_ordered_by_links_settled_and_deterministic():
    nodes = ids(50)
    small = chain_links(ids(3, "s"))
    first = nb.plan({**chain_links(nodes), **small}, max_size=20)
    second = nb.plan({**small, **chain_links(nodes)}, max_size=20)
    assert [n.members for n in first.neighbourhoods] == [n.members for n in second.neighbourhoods]
    assert [n.gain for n in first.neighbourhoods] == sorted((n.gain for n in first.neighbourhoods), reverse=True)


def test_a_dense_group_is_grown_to_fill_the_prompt():
    nodes = ids(30)
    links = {nb.pair_key(a, b): nb.Link(a, b, 1.0, ("w",)) for i, a in enumerate(nodes) for b in nodes[i + 1:i + 4]}
    plan = nb.plan(links, max_size=12, neighbours=3)
    assert max(len(n.members) for n in plan.neighbourhoods) == 12


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def answer(members, *groups):
    return nb.NeighbourhoodAnswer(tuple(sorted(members)), tuple(
        nb.GroupAnswer(tuple(sorted(g[0])), g[1], g[2] if len(g) > 2 else "label", "why") for g in groups))


def test_overlapping_answers_join_a_supplier_incident_across_the_boundary():
    first = answer("abcx", ("abc", 0.95), ("x", 0.9))
    second = answer("cdey", ("cde", 0.9, "ProctorU"), ("y", 0.9))
    result = nb.reconcile([first, second])
    [incident] = result.incidents
    assert incident.ids == ("a", "b", "c", "d", "e") and incident.certainty == 0.9
    assert incident.label == "ProctorU" or incident.label == "label"
    assert result.refused_unions == 0 and result.contradicted_links == []


def test_a_neighbourhood_that_separates_two_events_vetoes_their_link():
    together = answer("abc", ("abc", 0.95))
    apart = answer("abd", ("ab", 0.9), ("d", 0.9))
    apart_ab = answer("ab", ("a", 0.9), ("b", 0.9))
    result = nb.reconcile([together, apart_ab])
    assert result.incidents == [] or all("a" not in i.ids or "b" not in i.ids for i in result.incidents)
    assert ("a", "b", 1, 1) in result.contradicted_links
    # a and b are separated in one view but still joined to c where nothing separates them.
    assert {tuple(i.ids) for i in nb.reconcile([together, apart]).incidents} == {("a", "b", "c")}


def test_a_union_through_a_third_event_is_refused_when_another_neighbourhood_separates_the_ends():
    # a~b and b~c are asserted, but a view containing a and c keeps them apart.
    one = answer("ab", ("ab", 0.95))
    two = answer("bc", ("bc", 0.9))
    three = answer("ac", ("a", 0.9), ("c", 0.9))
    result = nb.reconcile([one, two, three])
    assert result.refused_unions == 1
    [incident] = result.incidents
    assert incident.ids == ("a", "b")            # the stronger link (higher certainty) wins; c stays out


def test_a_pair_a_ruling_or_label_forbids_is_never_joined_directly_or_through_a_third():
    blocked = lambda a, b: "labelled different" if {a, b} == {"a", "c"} else None      # noqa: E731
    result = nb.reconcile([answer("ab", ("ab", 0.95)), answer("bc", ("bc", 0.9))], blocked=blocked)
    assert result.refused_unions == 1 and [i.ids for i in result.incidents] == [("a", "b")]
    direct = nb.reconcile([answer("ac", ("ac", 0.99))], blocked=blocked)
    assert direct.incidents == [] and direct.blocked_links == [("a", "c", "labelled different")]


def test_a_group_below_the_certainty_bar_places_nothing_together():
    result = nb.reconcile([answer("ab", ("ab", 0.4))], min_certainty=0.7)
    assert result.incidents == []


def test_the_incidents_certainty_is_the_lowest_on_the_links_that_built_it():
    result = nb.reconcile([answer("abc", ("abc", 0.99)), answer("cde", ("cd", 0.75), ("e", 0.9))])
    [incident] = result.incidents
    assert incident.ids == ("a", "b", "c", "d") and incident.certainty == 0.75
    assert incident.support >= 1


def test_the_representative_is_the_event_most_strongly_linked_to_the_others():
    links = {nb.pair_key("a", "b"): nb.Link("a", "b", 4.0, ()), nb.pair_key("b", "c"): nb.Link("b", "c", 5.0, ()),
             nb.pair_key("a", "c"): nb.Link("a", "c", 1.0, ())}
    [incident] = nb.reconcile([answer("abc", ("abc", 0.9))], links=links).incidents
    assert incident.representative == "b"


# ---------------------------------------------------------------------------
# The adjudicator
# ---------------------------------------------------------------------------

class KeyedClient:
    """Stands in for the model: groups the records it is shown by the ``SUP-<key>`` marker in their titles."""

    def __init__(self, certainty: float = 0.95):
        self.calls = 0
        self.certainty = certainty
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls += 1
        text = kwargs["messages"][1]["content"]
        rows = re.findall(r"^\[(\d+)\] title: (.*)$", text, re.M)
        by_key: Dict[Optional[str], List[int]] = {}
        groups: List[IncidentGroup] = []
        for index, title in rows:
            marker = re.search(r"SUP-(\w+)", title)
            if marker:
                by_key.setdefault(marker.group(1), []).append(int(index))
            else:
                groups.append(IncidentGroup(label="alone", member_indices=[int(index)], certainty=self.certainty,
                                            reasoning="no marker"))
        for key, members in by_key.items():
            groups.append(IncidentGroup(label=f"supplier {key}", member_indices=members, certainty=self.certainty,
                                        reasoning=f"all name supplier {key}"))
        return ClusterPartition(incidents=groups)


def chain_records(n: int = 40, supplier_span=range(8, 24)) -> List[EventRecord]:
    """A chain of events, each sharing one rare word with the next; the supplier's span crosses the slice cut."""
    out = []
    for i in range(n):
        mark = "SUP-acme " if i in supplier_span else ""
        out.append(EventRecord(enriched_event_id=f"e{i:02d}", title=f"{mark}word{i:02d} word{i + 1:02d} report",
                               entity_name=f"Customer {i:02d}", event_date="2024-05-10"))
    return out


def test_an_oversized_cluster_recovers_a_supplier_incident_that_slicing_would_cut():
    records = chain_records()
    client = KeyedClient()
    adjudicator = ClusterAdjudicator(openai_client=client, resolver=EntityResolver(), max_cluster_size=20)
    partition = adjudicator.adjudicate_cluster(records)
    assert partition.validate_covers(len(records)) == []
    groups = [ids for group, ids in partition.groups_as_ids(records) if len(ids) > 1]
    assert groups == [[f"e{i:02d}" for i in range(8, 24)]]
    assert adjudicator.stats["oversized"] == 1 and adjudicator.stats["llm_calls"] == client.calls > 1
    # A slice of 20 either side of the cut could not have seen all sixteen at once.
    assert not any(set(f"e{i:02d}" for i in range(8, 24)) <= {r.enriched_event_id for r in records[s:s + 20]}
                   for s in (0, 20))


def test_the_call_budget_bounds_the_model_calls_and_the_rest_is_reported_not_hidden():
    records = chain_records()
    client = KeyedClient()
    adjudicator = ClusterAdjudicator(openai_client=client, resolver=EntityResolver(), max_cluster_size=20,
                                     call_budget=2)
    partition = adjudicator.adjudicate_cluster(records)
    assert client.calls == 2 == adjudicator.stats["llm_calls"]
    assert adjudicator.stats["budget_skipped"] > 0
    assert partition.validate_covers(len(records)) == []         # a valid partition even though little was asked
    assert adjudicator.calls_remaining() == 0
    assert adjudicator.last_plan["asked"] > adjudicator.last_plan["answered"] == 2


def test_stored_answers_are_free_and_a_later_run_continues_where_the_budget_stopped():
    conn = sqlite3.connect(":memory:")
    cache = stored.AdjudicationCache(conn)
    records = chain_records(100)
    totals = []
    for _ in range(12):
        client = KeyedClient()
        adjudicator = ClusterAdjudicator(openai_client=client, resolver=EntityResolver(), max_cluster_size=20,
                                         call_budget=3, cache=cache)
        adjudicator.adjudicate_cluster(records)
        totals.append((client.calls, adjudicator.stats["budget_skipped"], adjudicator.stats["llm_cached"]))
        if adjudicator.stats["budget_skipped"] == 0:
            break
    assert totals[0][0] == 3 and totals[0][2] == 0 and totals[0][1] > 0
    assert totals[1][2] >= 3                    # the first run's answers were reused, not asked again
    assert totals[-1][1] == 0                   # eventually everything has been asked
    final = ClusterAdjudicator(openai_client=KeyedClient(), resolver=EntityResolver(), max_cluster_size=20,
                               call_budget=0, cache=cache)
    groups = [ids for g, ids in final.adjudicate_cluster(records).groups_as_ids(records) if len(ids) > 1]
    assert groups == [[f"e{i:02d}" for i in range(8, 24)]]        # budget 0: stored answers alone give the result
    assert final.stats["llm_calls"] == 0


def test_no_answer_is_not_the_same_as_an_answer_that_keeps_events_apart():
    records = chain_records(30)
    adjudicator = ClusterAdjudicator(openai_client=None, resolver=EntityResolver(), max_cluster_size=20)
    adjudicator._client_ready = True        # no API key resolved
    assert adjudicator.answer(records[:5]) is None
    partition = adjudicator.adjudicate_cluster(records)
    assert partition.validate_covers(30) == [] and all(len(g.member_indices) == 1 for g in partition.incidents)


def test_a_ruling_or_label_stops_two_events_being_unioned_through_the_neighbourhoods():
    records = chain_records()
    blocked = lambda a, b: "labelled different" if {a, b} == {"e10", "e20"} else None      # noqa: E731
    adjudicator = ClusterAdjudicator(openai_client=KeyedClient(), resolver=EntityResolver(), max_cluster_size=20,
                                     blocked=blocked)
    partition = adjudicator.adjudicate_cluster(records)
    big = [ids for g, ids in partition.groups_as_ids(records) if len(ids) > 1]
    assert not any("e10" in g and "e20" in g for g in big)


def test_a_bucket_larger_than_twice_the_limit_is_kept_and_only_a_diffuse_one_is_dropped_and_counted():
    records = [EventRecord(enriched_event_id=f"r{i}", title="sharedtoken incident report", entity_name="Same Org Pty Ltd",
                           event_date="2024-01-01") for i in range(40)]
    kept = ClusterAdjudicator(resolver=EntityResolver(), max_cluster_size=5).build_clusters(records)
    assert [len(c) for c in kept] == [40]
    diffuse = ClusterAdjudicator(resolver=EntityResolver(), max_cluster_size=5, max_bucket_size=30)
    assert diffuse.build_clusters(records) == [] and diffuse.stats["buckets_too_diffuse"] >= 1


def test_partition_from_incidents_covers_every_record_exactly_once():
    records = chain_records(6)
    incident = nb.Incident(("e01", "e03"), 0.8, 2, "supplier", "why", "e01")
    partition = partition_from_incidents(records, [incident, nb.Incident(("zz", "yy"), 0.9, 1, "", "", "zz")])
    assert partition.validate_covers(6) == []
    assert sorted(len(g.member_indices) for g in partition.incidents) == [1, 1, 1, 1, 2]


# ---------------------------------------------------------------------------
# Labels as constraints, and victims kept through a merge
# ---------------------------------------------------------------------------

def test_blocked_pairs_are_the_active_events_a_label_names(tmp_path, monkeypatch):
    from cyber_data_collector.dedup import schema
    from cyber_data_collector.dedup.backfill import run_backfill
    from cyber_data_collector.tests.test_dedup_v3 import BASE_DDL, _add_event

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(BASE_DDL)
    schema.migrate(conn)
    for key, date in (("a", "2024-08-14"), ("a2", "2024-08-14"), ("b", "2025-01-28")):
        _add_event(conn, key, f"Acme breach {key}", f"https://x/{key}", victim="Acme Ltd", date=date)
    run_backfill(conn)
    from cyber_data_collector.dedup.ledger import DedupLedger
    DedupLedger(conn).merge_events("ded-a", "ded-a2", reason="same incident", respect_overrides=False)
    labels = tmp_path / "duplicate_labels.csv"
    labels.write_text("pair_id,label,rationale,corroboration,labeller\n"
                      "ded-a2|ded-b,different,why,,t\nded-a|ded-b,same,why,,t\n", encoding="utf-8")
    monkeypatch.setattr(lc, "LABELS_PATH", labels)
    blocked = lc.blocked_pairs(conn, labels)
    # ded-a2 was merged into ded-a, so the label now forbids joining ded-a with ded-b.
    assert list(blocked) == [frozenset({"ded-a", "ded-b"})] and "labelled different" in blocked[frozenset({"ded-a", "ded-b"})]
    from scripts.dedup_admin import _merge_blocker
    check = _merge_blocker(conn, DedupLedger(conn))
    assert check("ded-b", "ded-a") and check("ded-a", "ded-b")


SUPPLIER_SCHEMA = """
CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT, entity_kind TEXT,
    employee_count INTEGER, turnover TEXT, size_estimate TEXT, size_confidence REAL,
    size_employees INTEGER, size_revenue_aud REAL, industry TEXT);
CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, title TEXT, event_date TEXT,
    victim_organization_name TEXT, vendor_organization_name TEXT, status TEXT, summary TEXT,
    description TEXT, updated_at TEXT);
CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER,
    relationship_type TEXT, confidence_score REAL, source_count INTEGER);
CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
CREATE TABLE EnrichedEventEntities (enriched_event_id TEXT, entity_id INTEGER, relationship_type TEXT,
    confidence_score REAL);
CREATE TABLE DedupSnapshots (snapshot_id TEXT, batch_id TEXT, deduplicated_event_id TEXT, payload_json TEXT);
"""


def test_a_merge_keeps_every_customer_victim_recorded_on_the_event_merged_away(monkeypatch):
    from cyber_data_collector.dedup.ledger import DedupLedger
    from scripts.dedup_admin import _merge_carrying_victims

    conn = sqlite3.connect(":memory:")
    conn.executescript(SUPPLIER_SCHEMA)
    conn.executemany("INSERT INTO EntitiesV2 (entity_id, entity_name, entity_kind) VALUES (?, ?, ?)",
                     [(1, "ProctorU", "organisation"), (2, "University of Sydney", "organisation"),
                      (3, "Curtin University", "organisation"), (4, "Australian universities", None),
                      (5, "University of Sydney Ltd", "organisation")])
    for did, victim in (("target", "University of Sydney"), ("source", "Curtin University")):
        conn.execute("INSERT INTO DeduplicatedEvents VALUES (?, 'x', '2020-08-01', ?, 'ProctorU', 'Active', "
                     "NULL, NULL, NULL)", (did, victim))
    conn.executemany("INSERT INTO DeduplicatedEventEntities VALUES (?, ?, ?, NULL, 1)",
                     [("target", 1, "vendor"), ("target", 2, "victim"),
                      ("source", 1, "vendor"), ("source", 3, "victim"), ("source", 1, "victim"),
                      ("source", 4, "victim"), ("source", 5, "victim")])

    def fake_merge(self, target, source, reason, actor="human", respect_overrides=True):
        self.conn.execute("UPDATE DeduplicatedEvents SET status = 'Merged' WHERE deduplicated_event_id = ?", (source,))

    monkeypatch.setattr(DedupLedger, "merge_events", fake_merge)
    carried = _merge_carrying_victims(conn, DedupLedger(conn), "target", "source", reason="same incident",
                                      certainty=0.95, cutoff=0.9, question_version="cluster-partition-v2")
    names = sorted(r[0] for r in conn.execute(
        "SELECT v.entity_name FROM DeduplicatedEventEntities dee JOIN EntitiesV2 v USING (entity_id) "
        "WHERE dee.deduplicated_event_id = 'target' AND dee.relationship_type = 'victim'"))
    assert carried == ["Curtin University"] and names == ["Curtin University", "University of Sydney"]
    audit = conn.execute("SELECT entity_name, decided_by, merged_from, question_version "
                         "FROM SupplierVictimAttachments").fetchall()
    assert [tuple(a) for a in audit] == [("Curtin University", "merge", "source", "cluster-partition-v2")]


def test_a_refused_merge_carries_no_victim(monkeypatch):
    from cyber_data_collector.dedup.ledger import DedupLedger
    from scripts.dedup_admin import _merge_carrying_victims

    conn = sqlite3.connect(":memory:")
    conn.executescript(SUPPLIER_SCHEMA)
    conn.execute("INSERT INTO EntitiesV2 (entity_id, entity_name, entity_kind) VALUES (3, 'Curtin University', "
                 "'organisation')")
    for did in ("target", "source"):
        conn.execute("INSERT INTO DeduplicatedEvents VALUES (?, 'x', '2020-08-01', NULL, NULL, 'Active', NULL, NULL, NULL)",
                     (did,))
    conn.execute("INSERT INTO DeduplicatedEventEntities VALUES ('source', 3, 'victim', NULL, 1)")

    def refuse(self, *args, **kwargs):
        raise ValueError("blocked by a 'different' override")

    monkeypatch.setattr(DedupLedger, "merge_events", refuse)
    with pytest.raises(ValueError):
        _merge_carrying_victims(conn, DedupLedger(conn), "target", "source", reason="x", certainty=0.9,
                                cutoff=0.9, question_version="v")
    assert conn.execute("SELECT COUNT(*) FROM DeduplicatedEventEntities WHERE deduplicated_event_id = 'target'"
                        ).fetchone()[0] == 0
