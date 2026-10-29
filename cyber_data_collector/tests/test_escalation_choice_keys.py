"""escalate_choice maps an answer keyed by an option's line or text back to the option - and never guesses."""

from __future__ import annotations

from cyber_data_collector.utils.escalation import _keyed_by_option

OPTIONS = {"none": "None of these is clearly the organisation the incident happened to.",
           "org_1": "BleepingComputer", "org_2": "HMK Group"}


def test_keys_by_name_line_or_text_are_mapped():
    answered = {"none: None of these is clearly the organisation the incident happened to.": 0.1,
                "org_1: BleepingComputer": 0.1, "HMK Group": 0.8}
    assert _keyed_by_option(answered, OPTIONS) == {"none": 0.1, "org_1": 0.1, "org_2": 0.8}


def test_names_pass_through_and_case_and_spacing_are_ignored():
    assert _keyed_by_option({"NONE": 0.2, "org_1": 0.3, " hmk  group ": 0.5}, OPTIONS) == \
        {"none": 0.2, "org_1": 0.3, "org_2": 0.5}


def test_an_unknown_key_is_kept_so_the_exact_cover_check_fails():
    out = _keyed_by_option({"none": 0.2, "org_1": 0.3, "the gym's booking API": 0.5}, OPTIONS)
    assert set(out) != set(OPTIONS)


def test_two_keys_for_one_option_are_not_merged():
    out = _keyed_by_option({"org_1": 0.3, "BleepingComputer": 0.3, "none": 0.4}, OPTIONS)
    assert set(out) != set(OPTIONS)


def test_text_shared_by_two_options_identifies_neither():
    options = {"org_1": "Acme", "org_2": "Acme", "none": "neither"}
    assert set(_keyed_by_option({"Acme": 0.5, "org_2": 0.3, "none": 0.2}, options)) != set(options)
