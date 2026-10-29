"""Stored embeddings and adjudication answers: a rerun asks only what changed.

Offline: the embedding and chat clients are fakes that count their calls, and an
autouse fixture sets bogus keys so any test that reaches a paid service fails
instead of spending.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from cyber_data_collector.dedup import adjudication_cache as store
from cyber_data_collector.dedup import candidates
from cyber_data_collector.dedup import adjudicator as adj
from cyber_data_collector.dedup.adjudicator import Adjudicator, EventRecord
from cyber_data_collector.dedup.cluster_adjudicator import (
    ClusterAdjudicator,
    ClusterPartition,
    IncidentGroup,
)
from cyber_data_collector.dedup.models import LLMPairAdjudication


@pytest.fixture(autouse=True)
def bogus_keys(monkeypatch):
    for name in ("OPENAI_API_KEY", "PERPLEXITY_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.setenv(name, "bogus-not-a-key")


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


class FakeClient:
    """Counts embedding and chat requests; embeds a text as a vector derived from its characters."""

    def __init__(self, answer=None, fail=False):
        self.embedding_requests = []
        self.chat_requests = 0
        self.answer = answer
        self.fail = fail
        outer = self

        class _Embeddings:
            def create(self, model, input):
                outer.embedding_requests.append((model, list(input)))
                return SimpleNamespace(data=[SimpleNamespace(embedding=[float(len(t)), float(sum(map(ord, t)) % 97),
                                                                        0.25]) for t in input])

        class _Completions:
            def create(self, **kwargs):
                outer.chat_requests += 1
                if outer.fail:
                    raise RuntimeError("boom")
                return outer.answer

        self.embeddings = _Embeddings()
        self.chat = SimpleNamespace(completions=_Completions())


def rec(key, title="Acme Health breach", **fields):
    return EventRecord(enriched_event_id=key, title=title, entity_name="Acme Health",
                       event_date="2024-05-14", summary=f"{title} summary", **fields)


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

def test_embeddings_persist_across_adjudicators(conn):
    records = [rec("a", "Alpha breach"), rec("b", "Beta breach"), rec("c", "Gamma breach")]
    first_client = FakeClient()
    first = Adjudicator(openai_client=first_client, cache=store.AdjudicationCache(conn))
    first.embed_records(records)
    assert len(first_client.embedding_requests) == 1 and first.stats["embeddings_new"] == 3

    second_client = FakeClient()
    second = Adjudicator(openai_client=second_client, cache=store.AdjudicationCache(conn))
    second.embed_records(records)
    assert second_client.embedding_requests == []
    assert second.stats["embeddings_cached"] == 3 and second.stats["embedding_calls"] == 0
    assert second._embeddings == first._embeddings          # a cached run compares the same numbers


def test_only_changed_text_is_embedded_again(conn):
    cache = store.AdjudicationCache(conn)
    Adjudicator(openai_client=FakeClient(), cache=cache).embed_records([rec("a", "Alpha"), rec("b", "Beta")])
    client = FakeClient()
    Adjudicator(openai_client=client, cache=cache).embed_records([rec("a", "Alpha"), rec("b", "Beta, updated")])
    assert [len(batch) for _, batch in client.embedding_requests] == [1]
    assert "Beta, updated" in client.embedding_requests[0][1][0]


def test_embeddings_are_keyed_on_model_and_text_version(conn, monkeypatch):
    cache = store.AdjudicationCache(conn)
    Adjudicator(openai_client=FakeClient(), cache=cache).embed_records([rec("a")])
    other_model = FakeClient()
    Adjudicator(openai_client=other_model, cache=cache, embed_model="text-embedding-3-large").embed_records([rec("a")])
    assert len(other_model.embedding_requests) == 1
    monkeypatch.setattr(adj, "EMBED_TEXT_VERSION", "embed-text-v2")
    new_version = FakeClient()
    Adjudicator(openai_client=new_version, cache=cache).embed_records([rec("a")])
    assert len(new_version.embedding_requests) == 1


def test_a_failed_embedding_batch_stores_nothing(conn):
    class Failing(FakeClient):
        def __init__(self):
            super().__init__()
            self.embeddings = SimpleNamespace(create=lambda **k: (_ for _ in ()).throw(RuntimeError("down")))

    cache = store.AdjudicationCache(conn)
    Adjudicator(openai_client=Failing(), cache=cache).embed_records([rec("a")])
    assert conn.execute("SELECT COUNT(*) FROM EmbeddingCache").fetchone()[0] == 0


def test_vectors_round_trip_as_float32():
    assert store.unpack(store.pack([0.1, 0.5, 2.0])) == pytest.approx([0.1, 0.5, 2.0], rel=1e-6)


def test_a_read_only_cache_reads_nothing_and_refuses_writes():
    c = sqlite3.connect(":memory:")
    cache = store.AdjudicationCache(c, create=False)
    assert not cache.available and cache.embeddings(["x"], "m", "v") == {} and cache.decision("k") is None
    with pytest.raises(RuntimeError, match="read-only"):
        cache.store_decision("k", "pair", "p", "e", "m", ["a"], {})


def test_the_cache_does_not_commit_the_callers_open_transaction(conn):
    cache = store.AdjudicationCache(conn)
    conn.execute("CREATE TABLE Work (x)")
    conn.execute("INSERT INTO Work VALUES (1)")
    assert conn.in_transaction
    cache.store_embeddings({"h": [1.0, 2.0]}, "m", "v")
    assert conn.in_transaction            # still the caller's to commit or roll back
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM Work").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Pair adjudication
# ---------------------------------------------------------------------------

SAME = LLMPairAdjudication(is_same_event=True, certainty=0.95, reasoning="same breach", supporting_facts=["x"],
                           distinguishing_facts=[])


def test_a_pair_is_asked_once_for_the_same_evidence_prompt_and_model(conn):
    cache = store.AdjudicationCache(conn)
    left, right = rec("a", "Acme Health breach"), rec("b", "Acme Health hit by ransomware")
    first_client = FakeClient(answer=SAME)
    first = Adjudicator(openai_client=first_client, cache=cache)
    verdict = first.adjudicate(left, right)
    assert verdict.is_same_event and first_client.chat_requests == 1

    second_client = FakeClient(answer=SAME)
    second = Adjudicator(openai_client=second_client, cache=cache)
    again = second.adjudicate(left, right)
    assert second_client.chat_requests == 0 and second.stats["llm_cached"] == 1
    assert (again.is_same_event, again.certainty, again.reasoning) == (True, 0.95, "same breach")
    assert cache.stats["decisions_new"] == 1 and cache.stats["decisions_cached"] == 1


def test_a_cached_answer_needs_no_client_at_all(conn, monkeypatch):
    cache = store.AdjudicationCache(conn)
    left, right = rec("a"), rec("b", "Acme Health hit by ransomware")
    Adjudicator(openai_client=FakeClient(answer=SAME), cache=cache).adjudicate(left, right)

    offline = Adjudicator(cache=cache)
    monkeypatch.setattr(offline, "_get_client", lambda: (_ for _ in ()).throw(AssertionError("asked a model")))
    assert offline.adjudicate(left, right).is_same_event


def test_changed_evidence_prompt_version_model_or_order_asks_again(conn, monkeypatch):
    cache = store.AdjudicationCache(conn)
    left, right = rec("a"), rec("b", "Acme Health hit by ransomware")
    Adjudicator(openai_client=FakeClient(answer=SAME), cache=cache).adjudicate(left, right)

    def asks(adjudicator, a, b):
        client = adjudicator._client
        before = client.chat_requests
        adjudicator.adjudicate(a, b)
        return client.chat_requests - before

    assert asks(Adjudicator(openai_client=FakeClient(answer=SAME), cache=cache), left, right) == 0
    assert asks(Adjudicator(openai_client=FakeClient(answer=SAME), cache=cache), right, left) == 1   # order swapped
    left_with_evidence = rec("a", evidence="Acme Health said on 14 May 2024 that 50,000 records were taken.")
    assert asks(Adjudicator(openai_client=FakeClient(answer=SAME), cache=cache), left_with_evidence, right) == 1
    assert asks(Adjudicator(openai_client=FakeClient(answer=SAME), cache=cache, adjudication_model="other-model"),
                left, right) == 1
    monkeypatch.setattr(adj, "PAIR_PROMPT_VERSION", "pair-adjudication-v3")
    assert asks(Adjudicator(openai_client=FakeClient(answer=SAME), cache=cache), left, right) == 1


def test_a_failed_call_is_not_cached_and_is_retried(conn):
    cache = store.AdjudicationCache(conn)
    left, right = rec("a"), rec("b", "Acme Health hit by ransomware")
    failing = Adjudicator(openai_client=FakeClient(fail=True), cache=cache)
    assert failing.adjudicate(left, right).certainty == 0.3
    retry_client = FakeClient(answer=SAME)
    assert Adjudicator(openai_client=retry_client, cache=cache).adjudicate(left, right).is_same_event
    assert retry_client.chat_requests == 1


def test_excerpts_replace_the_opening_in_the_prompt():
    from cyber_data_collector.dedup.models import MatchEvidence

    left = rec("a", evidence="Passage chosen for the incident.")
    right = rec("b")
    prompt = Adjudicator._render_prompt(left, right, MatchEvidence(entity_match=True))
    assert "Passage chosen for the incident." in prompt
    assert "Detail: Acme Health breach summary" in prompt     # no excerpts: the opening, as before


# ---------------------------------------------------------------------------
# Cluster adjudication
# ---------------------------------------------------------------------------

def partition(*groups):
    return ClusterPartition(incidents=[IncidentGroup(label=f"G{i}", member_indices=list(ids), certainty=0.9,
                                                     reasoning="r") for i, ids in enumerate(groups, 1)])


def test_a_valid_partition_is_cached_and_an_invalid_one_is_not(conn):
    cache = store.AdjudicationCache(conn)
    cluster = [rec("a"), rec("b", "Acme Health hit by ransomware"), rec("c", "Acme statement")]

    good = FakeClient(answer=partition([1, 2], [3]))
    first = ClusterAdjudicator(openai_client=good, cache=cache)
    assert len(first.adjudicate_cluster(cluster).incidents) == 2
    again_client = FakeClient(answer=partition([1, 2], [3]))
    again = ClusterAdjudicator(openai_client=again_client, cache=cache)
    assert len(again.adjudicate_cluster(cluster).incidents) == 2
    assert again_client.chat_requests == 0 and again.stats["llm_cached"] == 1

    other = [rec("x"), rec("y", "Different title y")]
    bad_client = FakeClient(answer=partition([1]))            # leaves record 2 out
    ClusterAdjudicator(openai_client=bad_client, cache=cache).adjudicate_cluster(other)
    retry = FakeClient(answer=partition([1, 2]))
    assert len(ClusterAdjudicator(openai_client=retry, cache=cache).adjudicate_cluster(other).incidents) == 1
    assert retry.chat_requests == 1


def test_cluster_prompt_uses_excerpts_when_present():
    text = ClusterAdjudicator._render_cluster([rec("a", evidence="Chosen passage."), rec("b")])
    assert "detail: Chosen passage." in text and "detail: Acme Health breach summary" in text


# ---------------------------------------------------------------------------
# Scoping embeddings to the recent events
# ---------------------------------------------------------------------------

def pairs(*edges):
    return {frozenset(edge): {"entity 'x'"} for edge in edges}


def test_scope_is_everything_linked_to_a_recent_event():
    reasons = pairs(("a", "b"), ("b", "c"), ("d", "e"), ("f", "g"))
    assert candidates.scope_ids(reasons, {"a"}) == {"a", "b", "c"}
    assert candidates.scope_ids(reasons, {"e", "f"}) == {"d", "e", "f", "g"}
    assert candidates.scope_ids(reasons, {"z"}) == {"z"}            # recent but linked to nothing
    assert candidates.scope_ids(reasons, set()) == set()


def test_scope_keeps_every_group_that_holds_a_recent_event():
    """The groups built from the scoped events equal those built from all events, for the recent ones."""
    reasons = {
        frozenset(("a", "b")): {"shared source x"}, frozenset(("b", "c")): {"shared subject y"},
        frozenset(("d", "e")): {"shared source z"}, frozenset(("e", "f")): {"shared source w"},
    }
    strong = lambda key, why: True        # noqa: E731
    recent = {"c"}
    in_scope = candidates.scope_ids(reasons, recent)
    everywhere = [g for g in candidates.candidate_components(reasons, strong) if recent & set(g)]
    scoped = [g for g in candidates.candidate_components(
        {k: v for k, v in reasons.items() if k <= in_scope}, strong) if recent & set(g)]
    assert everywhere == scoped == [["a", "b", "c"]]
