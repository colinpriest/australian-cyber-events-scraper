"""The deterministic same-day merge needs incident-specific corroboration.

A shared organisation and day is a candidate signal; the records must also
agree on something specific to the incident (a shared article, record count,
attacker or attack method) with no conflict, or the pair is left to the
calibrated duplicate check. All offline: an autouse fixture sets bogus keys so a
test that reaches a paid service fails instead of spending.
"""

from __future__ import annotations

from datetime import date

import pytest

from cyber_data_collector.dedup import merge_corroboration as mc
from cyber_data_collector.processing import deduplication_v2 as dd
from cyber_data_collector.processing.deduplication_v2 import CyberEvent, DeduplicationEngine

DAY = date(2024, 5, 14)


@pytest.fixture(autouse=True)
def bogus_keys(monkeypatch):
    for name in ("OPENAI_API_KEY", "PERPLEXITY_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.setenv(name, "bogus-not-a-key")
    # Merging a group validates its record count; it must never reach a service here.
    monkeypatch.setattr(dd, "llm_validate_records_affected", lambda value, *a, **k: (value, None))


def event(key, **fields):
    base = dict(event_id=key, title=f"Acme Health incident {key}", event_date=DAY,
                victim_organization_name="Acme Health")
    base.update(fields)
    return CyberEvent(**base)


def corpus_of(*events):
    return mc.CorpusStats.from_events(list(events))


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def test_a_shared_day_alone_corroborates_nothing():
    result = mc.assess(event("1"), event("2"))
    assert result.verdict == mc.UNCORROBORATED
    assert result.describe() == "nothing beyond the shared day"


def test_shared_article_corroborates_even_against_a_conflict():
    a = event("1", urls=["https://www.news.example/story/acme-breach?utm=1"], record_counts=[50_000])
    b = event("2", urls=["http://news.example/story/acme-breach/"], record_counts=[9_000_000])
    result = mc.assess(a, b)
    assert "shared article" in result.agreements and "record count" in result.conflicts
    assert result.verdict == mc.CORROBORATED


def test_hub_urls_are_not_a_shared_article():
    """A list page cited by many events says nothing about two of them."""
    hub = "https://news.example/data-breaches-in-australia"
    others = [event(str(i), urls=[hub], victim_organization_name=f"Other {i}") for i in range(4)]
    a, b = event("a", urls=[hub]), event("b", urls=[hub])
    corpus = corpus_of(a, b, *others)
    assert hub.split("://")[1] in corpus.hub_urls
    assert mc.assess(a, b, corpus).verdict == mc.UNCORROBORATED
    assert mc.assess(a, b).verdict == mc.CORROBORATED       # without the corpus the URL looks specific


def test_a_bare_domain_is_not_an_article():
    result = mc.assess(event("1", urls=["https://news.example/"]), event("2", urls=["https://news.example"]))
    assert result.verdict == mc.UNCORROBORATED


def test_record_counts_agree_within_a_quarter_and_conflict_beyond_a_factor_of_two():
    assert mc.assess(event("1", records_affected=9_800_000), event("2", records_affected=9_700_000)).verdict \
        == mc.CORROBORATED
    assert mc.assess(event("1", records_affected=5_700_000), event("2", records_affected=6_000_000)).agreements \
        == ("record count",)
    far = mc.assess(event("1", records_affected=9_000), event("2", records_affected=275_000_000))
    assert far.conflicts == ("record count",) and far.verdict == mc.CONFLICT
    between = mc.assess(event("1", records_affected=1_000_000), event("2", records_affected=2_000_000))
    assert between.verdict == mc.UNCORROBORATED             # neither agree nor conflict


def test_small_counts_coincide_by_chance_and_are_ignored():
    result = mc.assess(event("1", records_affected=21), event("2", records_affected=21))
    assert result.verdict == mc.UNCORROBORATED


def test_an_event_of_several_records_agrees_if_any_pairing_agrees():
    a = event("1", record_counts=[12_000, 5_700_000])
    b = event("2", record_counts=[5_650_000])
    assert mc.assess(a, b).agreements == ("record count",)


def test_attackers_agree_without_their_qualifiers_and_conflict_when_they_share_no_word():
    agree = mc.assess(event("1", threat_actors=["Scattered Spider (UNC3944)"]),
                      event("2", threat_actors=["Scattered Spider (suspected, not confirmed)"]))
    assert agree.agreements == ("attacker",)
    inside = mc.assess(event("1", threat_actors=["Termite"]), event("2", threat_actors=["Termite ransomware group"]))
    assert inside.agreements == ("attacker",)
    differ = mc.assess(event("1", threat_actors=["Aleksandr Ermakov"]),
                       event("2", threat_actors=["Extortion Gang (linked to a Russian hacking group)"]))
    assert differ.conflicts == ("attacker",) and differ.verdict == mc.CONFLICT


def test_placeholders_are_not_attackers():
    result = mc.assess(event("1", threat_actors=["Unknown individual(s) using alias 'x'"]),
                       event("2", threat_actors=["None"]))
    assert result.agreements == () and result.conflicts == ()
    # A real name that merely starts like a placeholder word is still a name.
    assert mc.assess(event("1", threat_actors=["Nation State X"]),
                     event("2", threat_actors=["Nation State X"])).agreements == ("attacker",)


def test_attack_method_needs_a_word_that_is_distinctive_in_the_corpus():
    common = [event(f"c{i}", attack_methods=["Data breach via ransomware"], victim_organization_name=f"Org {i}")
              for i in range(60)]
    a = event("a", attack_methods=["Ransomware data breach via Citrix gateway exploit"])
    b = event("b", attack_methods=["Citrix gateway exploit leading to ransomware"])
    generic_a = event("ga", attack_methods=["Data breach via ransomware"])
    generic_b = event("gb", attack_methods=["Ransomware data breach"])
    corpus = corpus_of(a, b, generic_a, generic_b, *common)
    assert mc.assess(a, b, corpus).agreements == ("attack method",)
    assert mc.assess(generic_a, generic_b, corpus).agreements == ()
    assert mc.assess(a, b).agreements == ()                 # no corpus: the signal is silent, never guessed


def test_one_conflict_defers_a_pair_that_otherwise_agrees():
    a = event("1", record_counts=[5_700_000], threat_actors=["Scattered Spider"])
    b = event("2", record_counts=[5_700_000], threat_actors=["ShinyHunters"])
    result = mc.assess(a, b)
    assert result.agreements == ("record count",) and result.conflicts == ("attacker",)
    assert result.verdict == mc.CONFLICT


# ---------------------------------------------------------------------------
# The engine's decision
# ---------------------------------------------------------------------------

def test_same_day_with_corroboration_merges_and_without_is_deferred():
    engine = DeduplicationEngine()
    assert engine.merge_decision(event("1", records_affected=50_000), event("2", records_affected=48_000)) \
        == "same organisation on the same day, corroborated (record count)"
    assert engine.deferred == []
    assert engine.merge_decision(event("3"), event("4")) is None
    assert [(a, b) for a, b, _ in engine.deferred] == [("3", "4")]
    assert engine.deferred[0][2].startswith("uncorroborated")


def test_a_conflict_is_recorded_as_deferred_with_the_reason():
    engine = DeduplicationEngine()
    assert engine.merge_decision(event("1", records_affected=1_000), event("2", records_affected=900_000)) is None
    assert "conflict on record count" in engine.deferred[0][2]


def test_the_first_of_a_month_is_never_corroborated_into_a_merge():
    engine = DeduplicationEngine()
    first = date(2024, 5, 1)
    assert engine.merge_decision(event("1", event_date=first, records_affected=50_000),
                                 event("2", event_date=first, records_affected=50_000)) is None
    assert engine.deferred == []                            # not even a candidate on this signal


def test_identical_specific_title_still_merges_on_its_own():
    engine = DeduplicationEngine()
    a = event("1", title="Acme Health ransomware attack")
    b = event("2", title="acme health ransomware attack")
    assert engine.merge_decision(a, b) == "identical title, date and victim"


def test_identical_generic_title_is_a_candidate_not_a_verdict():
    """"Data breach" is the same words for any incident: it needs corroboration like a bare shared day."""
    engine = DeduplicationEngine()
    a, b = event("1", title="Data breach"), event("2", title="Data breach")
    assert engine.merge_decision(a, b) is None
    assert len(engine.deferred) == 1
    a, b = event("1", title="Data breach", records_affected=7_000), event("2", title="Data breach",
                                                                          records_affected=7_100)
    assert engine.merge_decision(a, b).startswith("same organisation on the same day, corroborated")


def test_cached_different_ruling_still_vetoes_a_corroborated_pair():
    engine = DeduplicationEngine()
    engine.different_pairs = {frozenset(("1", "2"))}
    assert engine.merge_decision(event("1", records_affected=50_000), event("2", records_affected=50_000)) is None
    assert engine.deferred == []


def test_deduplicate_learns_hub_urls_and_carries_facts_into_the_merged_event():
    hub = "https://news.example/roundup"
    engine = DeduplicationEngine()
    events = [
        event("1", urls=[hub], threat_actors=["Qilin"], attack_methods=["Ransomware"], records_affected=60_000),
        event("2", urls=[hub], threat_actors=["Qilin ransomware group"], records_affected=61_000),
        event("3", urls=[hub], victim_organization_name="Other A"),
        event("4", urls=[hub], victim_organization_name="Other B"),
        event("5", urls=[hub], victim_organization_name="Other C"),
    ]
    result = engine.deduplicate(events)
    assert hub.split("://")[1] in engine.corpus.hub_urls
    merged = [e for e in result.unique_events if e.event_id == "1"]
    assert len(merged) == 1 and len(result.unique_events) == 4
    assert merged[0].threat_actors == ["Qilin", "Qilin ransomware group"]
    assert merged[0].record_counts == [60_000, 61_000]
    assert set(merged[0].urls) == {hub}


def test_existing_event_facts_corroborate_a_new_record():
    """Incremental matching: the existing event carries every member's counts."""
    engine = DeduplicationEngine()
    new = event("new", records_affected=5_650_000)
    existing = event("existing", record_counts=[12_000, 5_700_000])
    assert engine.merge_decision(new, existing)
