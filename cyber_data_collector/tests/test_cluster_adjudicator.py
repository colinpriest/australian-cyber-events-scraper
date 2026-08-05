"""Tests for cluster-level adjudication.

Cluster adjudication exists because pairwise comparison cannot see a cluster:
the follow-up coverage of one breach reads as "different focus, therefore
different incident" on every pair, consistently, so re-checking pairs does not
help. These tests pin the behaviour that makes the cluster path safe - a
partition must cover every record exactly once, and anything unusable must fall
back to "change nothing" rather than guess.

No network: the adjudicator is driven with an injected fake client.
"""
from __future__ import annotations

import pytest

from cyber_data_collector.dedup.adjudicator import EventRecord
from cyber_data_collector.dedup.cluster_adjudicator import (
    ClusterAdjudicator,
    ClusterPartition,
    IncidentGroup,
    singleton_partition,
)
from cyber_data_collector.dedup.entity_resolution import EntityResolver


def _rec(key, title, entity=None, date="2023-04-26"):
    return EventRecord(enriched_event_id=key, title=title,
                       entity_name=entity, event_date=date)


class _FakeClient:
    """Stands in for the instructor-wrapped OpenAI client."""

    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = 0
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls += 1
                outer.last_kwargs = kwargs
                if outer._error:
                    raise outer._error
                return outer._response

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


# --------------------------------------------------------------------------
# Partition validation
# --------------------------------------------------------------------------

def test_valid_partition_reports_no_problems():
    part = ClusterPartition(incidents=[
        IncidentGroup(label="A", member_indices=[1, 2], certainty=0.9, reasoning="x"),
        IncidentGroup(label="B", member_indices=[3], certainty=0.9, reasoning="y"),
    ])
    assert part.validate_covers(3) == []


def test_partition_detects_unassigned_record():
    """A dropped record would silently vanish from the dataset."""
    part = ClusterPartition(incidents=[
        IncidentGroup(label="A", member_indices=[1, 2], certainty=0.9, reasoning="x"),
    ])
    problems = part.validate_covers(3)
    assert any("not assigned" in p for p in problems)


def test_partition_detects_duplicate_assignment():
    """A record in two incidents would be double-counted."""
    part = ClusterPartition(incidents=[
        IncidentGroup(label="A", member_indices=[1, 2], certainty=0.9, reasoning="x"),
        IncidentGroup(label="B", member_indices=[2, 3], certainty=0.9, reasoning="y"),
    ])
    problems = part.validate_covers(3)
    assert any("more than one incident" in p for p in problems)


def test_partition_detects_out_of_range_index():
    part = ClusterPartition(incidents=[
        IncidentGroup(label="A", member_indices=[1, 9], certainty=0.9, reasoning="x"),
    ])
    assert any("out of range" in p for p in part.validate_covers(2))


def test_partition_rejects_empty_incident():
    with pytest.raises(Exception):
        ClusterPartition(incidents=[
            IncidentGroup(label="A", member_indices=[], certainty=0.9, reasoning="x")])


def test_singleton_partition_is_a_no_op():
    part = singleton_partition(4)
    assert len(part.incidents) == 4
    assert part.validate_covers(4) == []
    assert all(len(g.member_indices) == 1 for g in part.incidents)


def test_groups_as_ids_maps_indices_to_records():
    records = [_rec("a", "A"), _rec("b", "B"), _rec("c", "C")]
    part = ClusterPartition(incidents=[
        IncidentGroup(label="one", member_indices=[1, 3], certainty=0.9, reasoning="x"),
        IncidentGroup(label="two", member_indices=[2], certainty=0.9, reasoning="y"),
    ])
    groups = part.groups_as_ids(records)
    assert [ids for _, ids in groups] == [["a", "c"], ["b"]]


# --------------------------------------------------------------------------
# Adjudication behaviour
# --------------------------------------------------------------------------

def test_cluster_is_partitioned_by_the_llm():
    response = ClusterPartition(incidents=[
        IncidentGroup(label="HWL Ebsworth breach", member_indices=[1, 2, 3],
                      certainty=0.95, reasoning="All describe the April 2023 breach."),
    ])
    fake = _FakeClient(response)
    adj = ClusterAdjudicator(openai_client=fake)
    records = [_rec("a", "HWL Ebsworth breach"),
               _rec("b", "OAIC investigates HWL Ebsworth"),
               _rec("c", "Government responds to HWL Ebsworth hack")]

    part = adj.adjudicate_cluster(records)

    assert fake.calls == 1, "one call for the whole cluster, not one per pair"
    assert len(part.incidents) == 1
    assert part.incidents[0].member_indices == [1, 2, 3]


def test_invalid_partition_falls_back_to_changing_nothing():
    """A partition that drops a record must not be applied."""
    bad = ClusterPartition(incidents=[
        IncidentGroup(label="partial", member_indices=[1], certainty=0.9, reasoning="x"),
    ])
    adj = ClusterAdjudicator(openai_client=_FakeClient(bad))
    part = adj.adjudicate_cluster([_rec("a", "A"), _rec("b", "B"), _rec("c", "C")])

    assert len(part.incidents) == 3, "must degrade to singletons"
    assert adj.stats["invalid_partitions"] == 1


def test_llm_failure_falls_back_to_changing_nothing():
    adj = ClusterAdjudicator(openai_client=_FakeClient(error=RuntimeError("api down")))
    part = adj.adjudicate_cluster([_rec("a", "A"), _rec("b", "B")])
    assert len(part.incidents) == 2
    assert adj.stats["llm_failures"] == 1


def test_no_client_changes_nothing():
    adj = ClusterAdjudicator(openai_client=None)
    adj._client_ready = True  # simulate "no API key resolved"
    part = adj.adjudicate_cluster([_rec("a", "A"), _rec("b", "B")])
    assert len(part.incidents) == 2


def test_single_record_cluster_needs_no_call():
    fake = _FakeClient(None)
    adj = ClusterAdjudicator(openai_client=fake)
    adj.adjudicate_cluster([_rec("a", "A")])
    assert fake.calls == 0


def test_oversized_cluster_is_chunked():
    response = ClusterPartition(incidents=[
        IncidentGroup(label="all", member_indices=[1, 2], certainty=0.9, reasoning="x")])
    fake = _FakeClient(response)
    adj = ClusterAdjudicator(openai_client=fake, max_cluster_size=2)
    records = [_rec(str(i), f"Title {i}") for i in range(6)]

    part = adj.adjudicate_cluster(records)

    assert adj.stats["chunked"] == 1
    assert fake.calls == 3, "six records in chunks of two"
    assert part.validate_covers(6) == []


# --------------------------------------------------------------------------
# Cluster construction
# --------------------------------------------------------------------------

def test_shared_rare_title_token_forms_a_cluster():
    """Entity labels are unreliable, so titles must be able to cluster.

    In production the OAIC is recorded as the victim of the HWL Ebsworth
    breach; entity-only clustering found 2 of 13 records.
    """
    records = [
        _rec("a", "HWL Ebsworth data breach", "HWL Ebsworth Lawyers"),
        _rec("b", "OAIC opens investigation into HWL Ebsworth", "OAIC"),
        _rec("c", "Government responds to HWL Ebsworth hack", "Australian Government"),
    ] + [_rec(f"n{i}", f"Unrelated incident {i}", f"Org{i} Pty Ltd") for i in range(30)]

    clusters = ClusterAdjudicator(resolver=EntityResolver()).build_clusters(records)
    ebsworth = [c for c in clusters
                if sum("ebsworth" in (r.title or "").lower() for r in c) >= 3]
    assert ebsworth, "records sharing a rare title token must cluster"


def test_common_words_do_not_form_clusters():
    """'data' and 'breach' appear everywhere and must not link anything."""
    records = [_rec(f"n{i}", f"Company{i} data breach incident", f"Org{i} Pty Ltd")
               for i in range(40)]
    clusters = ClusterAdjudicator(resolver=EntityResolver()).build_clusters(records)
    assert all(len(c) <= 3 for c in clusters), \
        f"common words chained records: sizes {[len(c) for c in clusters]}"


def test_clusters_do_not_chain_transitively():
    """Regression: union-find over shared tokens collapsed 807 of 845 events.

    A chains to B, B to C, and the corpus becomes a single cluster. Buckets
    must stay independent.
    """
    records = [
        _rec(f"r{i}", f"tokenx{i} tokenx{i + 1} incident", f"Org{i} Pty Ltd")
        for i in range(60)
    ]
    clusters = ClusterAdjudicator(resolver=EntityResolver()).build_clusters(records)
    biggest = max((len(c) for c in clusters), default=0)
    assert biggest <= 5, f"transitive chaining produced a cluster of {biggest}"


def test_cluster_size_is_capped():
    adj = ClusterAdjudicator(resolver=EntityResolver(), max_cluster_size=5)
    records = [_rec(f"r{i}", "sharedtoken incident report", "Same Org Pty Ltd")
               for i in range(40)]
    clusters = adj.build_clusters(records)
    assert all(len(c) <= 10 for c in clusters), \
        f"oversized cluster survived: {[len(c) for c in clusters]}"
