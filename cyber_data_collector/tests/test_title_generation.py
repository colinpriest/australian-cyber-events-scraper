"""Detection of titles that cannot serve as an event headline.

Every case here is one that reached the dashboard: a placeholder, a stored LLM
paragraph, a masthead, or a fragment cut mid-word.
"""

from __future__ import annotations

import pytest

from cyber_data_collector.dedup.title_selection import (
    clean_title,
    needs_regeneration,
)


@pytest.mark.parametrize("title", [
    "Untitled Event",
    "untitled",
    "",
    "   ",
    None,
])
def test_placeholders_are_flagged(title):
    assert needs_regeneration(title)


@pytest.mark.parametrize("title", [
    # Stored verbatim from an LLM answer, complete with markdown and citations.
    "The cybersecurity incident reported at the provided URL concerns a major "
    "data breach affecting the **Tasmanian Department of Education, Children "
    "and Young People** in March 2023[1][2][3]",
    "The incident reported at the original URL concerns **ASIC's legal action "
    "against RI Advice Group Pty Ltd** for inadequate cybersecurity systems",
])
def test_prose_answers_are_flagged(title):
    assert needs_regeneration(title)


@pytest.mark.parametrize("title", [
    "The followi",        # truncated mid-word
    "Betwee",
    "News",
    "Uber - Wikipedia",    # masthead only
    "JCU: - Facebook",
    "Privacy Policy | LUSH",
    "Alerts and closures",
])
def test_fragments_and_furniture_are_flagged(title):
    assert needs_regeneration(title)


@pytest.mark.parametrize("title", [
    "Optus data breach (September 2022)",
    "Medibank Private Limited cyber attack (August 2022)",
    "Nitro PDF Data Breach",
    "Vinomofo Data Breach",
    "ANZCTR cyber attack causes week-long research disruption",
])
def test_real_headlines_are_left_alone(title):
    assert needs_regeneration(title) is None


def test_hyphenated_words_survive_cleaning():
    """A dash only separates a masthead when it is spaced.

    Treating any hyphen as a separator truncated "...causes week-long research
    disruption" to "...causes week".
    """
    title = "ANZCTR cyber attack causes week-long research disruption"
    assert clean_title(title) == title
    assert clean_title("Multi-factor authentication bypass at Acme") == (
        "Multi-factor authentication bypass at Acme")


def test_spaced_dash_still_strips_publisher():
    assert clean_title("Hacked! Lessons from a Cyber Attack | Article") == (
        "Hacked! Lessons from a Cyber Attack")
