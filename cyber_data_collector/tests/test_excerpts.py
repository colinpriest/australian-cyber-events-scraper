"""Incident-relevant, diverse excerpts of source text for the adjudicators. Offline and deterministic."""

from __future__ import annotations

import sqlite3

import pytest

from cyber_data_collector.dedup import excerpts as ex

ACME = ex.Anchors.for_record(["Acme Health Pty Ltd"], "Ransomware via Citrix gateway", "Qilin")

PAGE = (
    "Home | News | About us | Contact | Subscribe to our newsletter today\n"
    "Subscribe to our weekly newsletter and never miss a story from our team of reporters around the country.\n"
    "Acme Health confirmed on 14 May 2024 that attackers reached its patient systems through a Citrix gateway. "
    "The Qilin group claimed it had taken 1.2 million records.\n"
    "A different company, Beta Bank, reported a breach in March 2023 affecting 50,000 customers across Australia.\n"
    "Acme Health said it had notified the privacy commissioner and was working with the Australian Cyber Security "
    "Centre to contain the incident and restore its systems.\n"
    "Copyright 2024 News Example. All rights reserved. Terms and conditions apply to every page on this website."
)


@pytest.fixture(autouse=True)
def bogus_keys(monkeypatch):
    for name in ("OPENAI_API_KEY", "PERPLEXITY_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.setenv(name, "bogus-not-a-key")


def test_passages_name_the_organisation_or_the_attack_not_just_a_date():
    scores = {p: ex.score_passage(p, ACME) for p in ex.split_passages(PAGE)}
    incident = next(s for p, s in scores.items() if p.startswith("Acme Health confirmed"))
    assert incident == ex.SCORE_ORGANISATION + ex.SCORE_ATTACK + ex.SCORE_DATE
    count = next(s for p, s in scores.items() if p.startswith("The Qilin group claimed"))
    assert count == ex.SCORE_ATTACK + ex.SCORE_COUNT
    beta = next(s for p, s in scores.items() if p.startswith("A different company"))
    assert beta == 0           # a date and a count about another organisation count for nothing


def test_the_excerpt_prefers_the_incident_over_the_masthead_and_footer():
    text = ex.select_excerpts([PAGE], ACME, budget=500)
    assert "Citrix gateway" in text and "privacy commissioner" in text
    assert "Subscribe" not in text and "Copyright" not in text and "Beta Bank" not in text
    assert len(text) <= 500


def test_menus_and_short_lines_are_not_passages():
    assert ex.split_passages("Home | News | About | Contact | Login\nShort line\n12 34 56 78 90 12 34 56 78 90 12 34") == []


def test_long_passages_are_cut_at_word_boundaries():
    parts = ex.split_passages("word " * 200)
    assert parts and all(len(p) <= ex.MAX_PASSAGE_CHARS for p in parts)
    assert all(not p.endswith("wor") for p in parts)


def test_a_source_with_nothing_relevant_falls_back_to_its_opening():
    plain = ("The council met on Tuesday evening to discuss the new library budget for the coming year. "
             "Members voted to approve the plan after a long debate about funding for local services.")
    text = ex.select_excerpts([plain], ACME, budget=300)
    assert text.startswith("The council met on Tuesday")


def test_sources_share_the_budget_and_repeats_are_dropped():
    syndicated = PAGE
    other = ("Acme Health patients were warned on 15 May 2024 to watch for phishing after the Qilin attack, "
             "which the company said began through a vendor connection and exposed contact details.")
    text = ex.select_excerpts([PAGE, syndicated, other], ACME, budget=900)
    assert text.count("Citrix gateway") == 1                 # the copy of the same paragraph is skipped
    assert "phishing" in text                               # the other source still gets its turn


def test_the_same_input_always_gives_the_same_excerpt():
    first = ex.select_excerpts([PAGE, "Acme Health said more on 2 June 2024 about the 1.2 million records."],
                               ACME, budget=400)
    assert all(ex.select_excerpts([PAGE, "Acme Health said more on 2 June 2024 about the 1.2 million records."],
                                  ACME, budget=400) == first for _ in range(3))


def test_budget_is_respected_even_for_one_long_passage():
    sentence = "Acme Health " + "reported a very serious problem " * 8 + "."
    assert len(ex.select_excerpts([sentence], ACME, budget=100)) <= 100


def test_attack_anchors_leave_out_the_organisation_and_generic_words():
    anchors = ex.Anchors.for_record(["Acme Health"], "Data breach via Acme Health Citrix gateway", None)
    assert "citrix" in anchors.attack_words and "gateway" in anchors.attack_words
    assert "breach" not in anchors.attack_words and "acme" not in anchors.attack_words


# ---------------------------------------------------------------------------
# Reading the database
# ---------------------------------------------------------------------------

def db():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, status TEXT);
        CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title TEXT,
            summary TEXT, description TEXT, attack_method TEXT, attacking_entity_name TEXT,
            perplexity_enrichment_data TEXT, status TEXT);
        CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, raw_content TEXT);
    """)
    c.execute("INSERT INTO DeduplicatedEvents VALUES ('E1', 'Active'), ('E2', 'Merged')")
    c.execute("INSERT INTO EnrichedEvents VALUES ('a', 'ra', 'Acme Health breach', 'Acme Health was attacked.', "
              "'Acme Health reported the attack to the regulator on 14 May 2024.', 'Ransomware via Citrix gateway', "
              "'Qilin', NULL, 'Active')")
    c.execute("INSERT INTO EnrichedEvents VALUES ('b', 'rb', 'Acme Health breach', 'Summary b about Acme Health describes the patient notification and the regulator response.', "
              "NULL, NULL, NULL, NULL, 'Active')")
    c.execute("INSERT INTO EnrichedEvents VALUES ('gone', 'rg', 'Acme Health breach', 'Withdrawn record text about "
              "Acme Health that must never be shown.', NULL, NULL, NULL, NULL, 'Rejected')")
    c.execute("INSERT INTO RawEvents VALUES ('ra', ?)", (PAGE,))
    c.execute("INSERT INTO RawEvents VALUES ('rb', ?)",
              ("An unrelated article about a football club and its new stadium plans for next season.",))
    c.execute("INSERT INTO RawEvents VALUES ('rg', 'x')")
    c.executemany("INSERT INTO EventDeduplicationMap VALUES (?, ?)",
                  [("a", "E1"), ("b", "E1"), ("gone", "E1"), ("a", "E2")])
    return c


def test_event_excerpts_use_active_members_and_withhold_pages_of_another_article():
    conn = db()
    found = ex.load_event_excerpts(conn, {"E1": ["Acme Health"], "E2": ["Acme Health"]}, budget=700)
    assert set(found) == {"E1"}                              # the merged event has no excerpts
    text = found["E1"]
    assert "Citrix gateway" in text and "Summary b about Acme Health" in text
    assert "football" not in text and "Withdrawn" not in text


def test_member_excerpts_serve_any_group_of_records():
    conn = db()
    found = ex.load_member_excerpts(conn, {"half": ["a"], "other": ["b"]}, {"half": ["Acme Health"]}, budget=700)
    assert "Citrix gateway" in found["half"] and "Summary b" not in found["half"]
    assert "Summary b about Acme Health describes" in found["other"]
