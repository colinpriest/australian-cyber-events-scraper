"""A global brand's Australian arm is bounded by Australia's population.

McDonald's Australia Limited was recorded as losing 64,000,000 records - the
worldwide McHire/Paradox.ai applicant total - because the name matched the
international-brand exemption on "mcdonald's" and skipped the population
ceiling entirely. Australia has about 27 million people.
"""

from __future__ import annotations

import pytest

from cyber_data_collector.utils.validation import (
    is_australian_arm,
    is_local_australian_entity,
    validate_records_affected,
)


@pytest.mark.parametrize("name", [
    "McDonald's Australia Limited",
    "Toyota Australia",
    "Nissan Australia",
    "American Express Australia Limited",
    "Dell (Australia) Pty Ltd",
    "Jaguar Land Rover Australia",
])
def test_australian_arms_are_local(name):
    assert is_australian_arm(name)
    assert is_local_australian_entity(name)


@pytest.mark.parametrize("name", [
    "Dell", "Ticketmaster", "Instructure", "Shell", "Optus",
    # A leading nationality is not a scoping suffix: the Red Cross's Australian
    # body is simply named that way.
    "Australian Red Cross",
])
def test_others_are_not_australian_arms(name):
    assert not is_australian_arm(name)


def test_global_total_on_an_australian_arm_is_rejected():
    assert validate_records_affected(
        64_000_000, "McDonald's AI chatbot hacked leaving Aussie applicants exposed",
        "McDonald's Australia Limited") is None


def test_genuinely_global_victim_keeps_its_figure():
    """Ticketmaster's 560 million is real; only misattribution is corrected."""
    assert validate_records_affected(
        560_000_000, "Ticketmaster/Live Nation breach", "Ticketmaster") == 560_000_000


def test_plausible_australian_figure_survives():
    assert validate_records_affected(
        9_700_000, "Optus data breach", "Optus") == 9_700_000
