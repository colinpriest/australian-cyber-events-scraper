"""Tests for multi-key candidate generation (missed-duplicate recall)."""
from __future__ import annotations

from cyber_data_collector.dedup import candidates as C
from cyber_data_collector.dedup.adjudicator import Adjudicator, EventRecord
from cyber_data_collector.dedup.models import LLMPairAdjudication


def _rec(eid, title, entity=None, date="2026-05-02", urls=(), alt=(), desc=None):
    return EventRecord(enriched_event_id=eid, title=title, entity_name=entity,
                       alt_entities=list(alt), event_date=date,
                       source_urls=list(urls), description=desc)


def test_normalise_url_drops_scheme_www_query_and_homepages():
    assert C.normalise_url("https://www.ABC.net.au/news/x/?utm=1#a") == "abc.net.au/news/x"
    assert C.normalise_url("https://partneredhealth.com.au/") is None
    assert C.normalise_url(None) is None


def test_hub_urls_do_not_link_events():
    urls = {f"e{i}": ["https://www.webberinsurance.com.au/data-breaches-list"] for i in range(6)}
    urls["a"] = ["https://www.theregister.com/2026/06/25/asio"]
    urls["b"] = ["https://www.theregister.com/2026/06/25/asio/"]
    reasons, hubs = C.url_pairs(urls)
    assert "webberinsurance.com.au/data-breaches-list" in hubs
    assert set(reasons) == {frozenset(("a", "b"))}


def test_content_pairs_need_a_rare_word_inside_the_window():
    texts = {f"n{i}": (f"Unrelated breach number {i}", None) for i in range(50)}
    texts.update({
        "a": ("Instructure cyber attack (May 2026)", None),
        "b": ("Queensland schools hit after Instructure breach", None),
        "c": ("Instructure notice", None),          # same word, 2 years later
    })
    dates = {k: "2026-05-02" for k in texts}
    dates["c"] = "2024-05-02"
    reasons = C.content_pairs(texts, dates)
    assert frozenset(("a", "b")) in reasons
    assert not any("c" in key for key in reasons)


def test_candidate_pairs_link_supplier_fragments_with_different_victims():
    """The Canvas case: different named victims, same supplier + article."""
    records = [_rec(f"n{i}", f"Org{i} breach", f"Org{i} Pty Ltd") for i in range(40)]
    records += [
        _rec("usyd", "University of Sydney investigates Canvas compromise", "University of Sydney",
             urls=["https://www.abc.net.au/news/2026-05-07/canvas-data-breach-instructure/1"]),
        _rec("qld", "Queensland education sector caught up in major security breach",
             "Queensland education sector", alt=["Instructure, Inc."]),
        _rec("inst", "Instructure cyber attack (May 2026)", None, alt=["Instructure, Inc."],
             urls=["https://www.abc.net.au/news/2026-05-07/canvas-data-breach-instructure/1"]),
    ]
    pairs = {frozenset((l.enriched_event_id, r.enriched_event_id))
             for l, r in Adjudicator().candidate_pairs(records)}
    assert frozenset(("usyd", "inst")) in pairs       # shared article
    assert frozenset(("qld", "inst")) in pairs        # shared supplier name


class _Fake:
    def __init__(self):
        self.calls = 0
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls += 1
        return LLMPairAdjudication(is_same_event=True, certainty=0.95,
                                   reasoning="same supplier breach",
                                   supporting_facts=["shared article"], distinguishing_facts=[])


def test_shared_article_goes_to_llm_despite_name_mismatch():
    fake = _Fake()
    adj = Adjudicator(openai_client=fake, require_entity_match=True)
    url = "https://www.theregister.com/2026/06/25/asio"
    left = _rec("a", "ASIO discloses compromise", "Australian critical infrastructure", urls=[url])
    right = _rec("b", "Nation-state compromise of provider", "Unnamed provider", urls=[url])
    adj.candidate_pairs([left, right])
    verdict = adj.adjudicate(left, right)
    assert fake.calls == 1, "shared article must reach the LLM, not the name gate"
    assert verdict.is_same_event and verdict.evidence.shared_urls == ["theregister.com/2026/06/25/asio"]


def test_shared_article_is_not_an_automatic_merge():
    """One article can cover two incidents; the LLM decides, not a rule."""
    fake = _Fake()
    adj = Adjudicator(openai_client=fake)
    url = "https://www.cyberdaily.au/security/14111-two-incidents"
    left, right = _rec("a", "A", "Acme", urls=[url]), _rec("b", "B", "Zeta", urls=[url])
    adj.candidate_pairs([left, right])
    assert adj.adjudicate(left, right).decided_by.value == "llm"


def test_name_gate_still_applies_to_name_only_pairs():
    fake = _Fake()
    adj = Adjudicator(openai_client=fake, require_entity_match=True)
    left, right = _rec("a", "Acme breach", "Acme Ltd"), _rec("b", "Zeta breach", "Zeta Ltd")
    adj.adjudicate(left, right)
    assert fake.calls == 0


def test_candidate_components_join_only_strong_edges():
    reasons = {
        frozenset(("a", "b")): {"shared source x.com/news/1"},
        frozenset(("b", "c")): {"shared subject 'instructure'"},
        frozenset(("c", "d")): {"entity 'smith'"},          # weak: must not chain d in
        frozenset(("e", "f")): {"shared source y.com/news/2"},
    }
    strong = lambda key, why: any(w.startswith(("shared source", "shared subject")) for w in why)
    groups = C.candidate_components(reasons, strong)
    assert groups == [["a", "b", "c"], ["e", "f"]]


def test_capped_components_keep_strongest_links_and_never_chain_past_the_cap():
    # a-b share an article (strongest); b-c share a subject; c-d only similar.
    reasons = {
        frozenset(("a", "b")): {"shared source x.com/1"},
        frozenset(("b", "c")): {"shared subject 'acme'"},
        frozenset(("c", "d")): {"entity 'acme'"},
    }
    strong = lambda key, why: True
    weight = lambda key, why: 2.0 if any("source" in w for w in why) else (1.0 if any("subject" in w for w in why) else 0.1)
    assert C.candidate_components(reasons, strong, weight=weight, max_size=2) == [["a", "b"], ["c", "d"]]
    assert C.candidate_components(reasons, strong, weight=weight, max_size=3) == [["a", "b", "c"]]
