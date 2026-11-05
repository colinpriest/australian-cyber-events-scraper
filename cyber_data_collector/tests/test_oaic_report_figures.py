"""The older OAIC reports: figures read with each report's own wording, and the chart in one unit."""

from __future__ import annotations

import pytest

from scripts.build_static_dashboard import prepare_oaic_attack_types_data
from scripts.oaic import oaic_report_figures as orf

SHARES = {"phishing": 32, "compromised_credentials": 28, "ransomware": 23, "hacking": 8, "brute_force": 5,
          "malware": 3, "other": 1}


def _report(period):
    return next(r for r in orf.REPORTS if r.period == period)


def test_the_total_is_this_periods_not_the_previous_periods_or_a_months():
    text = ("In November 2019, with 106 notifications the most in a month. 537 breaches were notified under the scheme, "
            "up from 460. accounting for 343 breaches. The OAIC received 230 notifications under this category.")
    assert orf.report_figures(_report("2019 H2"), text) == {
        "total_notifications": 537, "malicious_attacks": 343, "cyber_incidents_total": 230}


def test_a_report_that_does_not_read_as_expected_is_an_error_not_a_blank():
    with pytest.raises(orf.FigureNotFound):
        orf.report_figures(_report("2022 H2"), "Nothing the patterns know.")


def test_shares_must_cover_every_category_and_sum_to_about_100():
    assert orf.check_shares("p", SHARES) == SHARES
    with pytest.raises(orf.FigureNotFound):
        orf.check_shares("p", {**SHARES, "malware": None})
    with pytest.raises(orf.FigureNotFound):
        orf.check_shares("p", {**SHARES, "phishing": 50})


def test_figures_replace_the_mixed_method_fields():
    record = {"year": 2021, "period": "H2", "total_notifications": 436, "ransomware": 23, "phishing": 32}
    out = orf.apply_figures(record, {"total_notifications": 464, "malicious_attacks": 256,
                                     "cyber_incidents_total": 173}, SHARES)
    assert out["total_notifications"] == 464 and out["cyber_incidents_percentage"] == 37.3
    assert out["ransomware"] is None and out["cyber_incident_breakdown_pct"]["ransomware"] == 23


def test_the_chart_plots_percentages_and_names_periods_with_no_split():
    data = prepare_oaic_attack_types_data([
        {"year": 2024, "period": "H1", "start_month": 1, "ransomware": 80, "cyber_incident_breakdown_pct": SHARES},
        {"year": 2025, "period": "H2", "start_month": 7, "ransomware": None}])
    assert data["attack_types"]["ransomware"] == [23, None]
    assert data["attack_types"]["brute_force"] == [5, None]
    assert data["not_published"] == ["2025 H2"]
