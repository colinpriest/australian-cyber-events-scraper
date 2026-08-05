"""Date consensus and recovery.

Every case is one that reached production: a date hijacked by a single bad
extraction, or a date left blank while the description plainly stated it.
"""

from __future__ import annotations

import pytest

from cyber_data_collector.dedup.date_selection import (
    date_from_url,
    derive_event_date,
    disagreement,
    extract_date,
)


@pytest.mark.parametrize("text,expected", [
    ("On 2026-07-11, the DragonForce ransomware group claimed a breach", "2026-07-11"),
    ("Between 2025-08-01 and 2025-08-31, Australia experienced incidents", "2025-08-01"),
    ("The incident reportedly occurred on April 7, 2025, affecting staff", "2025-04-07"),
    ("In April 2026, Anthropic experienced a security breach", "2026-04-01"),
    ("a data breach on 27 November 2023 affecting members", "2023-11-27"),
    ("restored most services as of May 4, 2026.", "2026-05-04"),
    ("no date at all in this text", None),
    ("", None),
    (None, None),
])
def test_extract_date(text, expected):
    assert extract_date(text) == expected


def test_impossible_dates_rejected():
    """A four-digit number is not automatically a year."""
    assert extract_date("reference 1823-99-99 in the filing") is None


def test_consensus_beats_a_single_outlier():
    """MediSecure: 64 of 77 records say April 2024, one says March 2019.

    Earliest-wins stored March 2019, which then convinced the cluster
    adjudicator that the 2019 and 2024 records were separate incidents.
    """
    members = ["2024-04-15"] * 64 + ["2023-11-01"] * 6 + ["2019-03-01"]
    assert derive_event_date(members, current="2019-03-01") == "2024-04-15"
    assert disagreement(members) == (64, 71)


def test_earliest_within_the_winning_month():
    """An incident is still dated from when it began."""
    members = ["2024-04-20", "2024-04-03", "2024-04-15"]
    assert derive_event_date(members) == "2024-04-03"


def test_weak_consensus_does_not_overrule_a_stored_date():
    """Two records are not enough to move a date that is already recorded."""
    members = ["2022-09-01", "2022-09-02", "2019-10-01"]
    assert derive_event_date(members, current="2019-10-01") == "2019-10-01"


def test_title_date_defends_the_stored_date():
    """"Optus White Pages breach (2019)" was being moved to 2022.

    The members had drifted toward coverage of the larger 2022 Optus breach.
    A date asserted in the headline outranks that drift.
    """
    members = ["2022-09-01"] * 5 + ["2019-10-01"]
    assert derive_event_date(members, current="2019-10-01",
                             title="Optus White Pages breach (2019)") == "2019-10-01"
    # Without the title, strong consensus is allowed to win.
    assert derive_event_date(members, current="2019-10-01") == "2022-09-01"


def test_blank_date_is_filled_without_a_consensus_bar():
    assert derive_event_date(["2026-04-06"], current=None) == "2026-04-06"


def test_never_blanks_a_date_it_cannot_improve():
    assert derive_event_date([None, None], current="2022-01-01") == "2022-01-01"
    assert derive_event_date([], current="2022-01-01") == "2022-01-01"


# ---------------------------------------------------------------------------
# Last-resort dating from the article URL
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://www.leansecurity.com.au/blog/2026/2/25/australian-cyber-threat",
     "2026-02-25"),
    ("https://cloudsecurityalliance.org/blog/2025/06/09/football-australia",
     "2025-06-09"),
    ("https://www.cybernewscentre.com/29th-january-2026-cyber-update-cl0p",
     "2026-01-29"),
    # No date anywhere in the path.
    ("https://www.gtlaw.com.au/expertise/services/cyber-security", None),
    ("https://www.reddit.com/r/AusLegal/comments/1ryi9n8/speedx_breach", None),
])
def test_date_from_url(url, expected):
    assert date_from_url(url) == expected


def test_path_date_beats_a_date_in_the_slug():
    """The path is the publication date; slug prose is incidental.

    "...as-it-was-2604-april-2026" yielded 4 April because the slug pattern
    matched "04" out of "2604" and ran first.
    """
    url = ("https://kirbyidau.com/2026/05/04/"
           "australian-cyber-aware-as-it-was-2604-april-2026/")
    assert date_from_url(url) == "2026-05-04"


def test_slug_day_needs_a_boundary():
    """A four-digit run is not a day: "2604-april-2026" is not 4 April.

    With no path date to fall back on, the slug still legitimately says April
    2026, so the month is returned - just not a fabricated day.
    """
    assert date_from_url("https://example.com/as-it-was-2604-april-2026/") == "2026-04-01"
