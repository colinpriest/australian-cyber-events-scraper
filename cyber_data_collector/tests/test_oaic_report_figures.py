"""The older OAIC reports: figures read with each report's own wording, and the chart in one unit."""

from __future__ import annotations

import sqlite3

import pytest

from scripts.build_static_dashboard import prepare_oaic_attack_types_data, prepare_oaic_monthly_comparison
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


def test_the_sector_table_is_read_from_its_first_row_to_its_last():
    page = ("February 2020\nTotal received July to December 2019 537\nTop five industry sectors NDBs received\n"
            "Health service providers 117\nFinance (incl. superannuation)2 77\nEducation3 49\n"
            "Legal, accounting & management services 40\nPersonal services4 23\n"
            "Notifiable Data Breaches Report Page 5")
    assert orf.read_sectors("p", page) == [
        {"sector": "Health service providers", "notifications": 117},
        {"sector": "Finance (incl. superannuation)", "notifications": 77}, {"sector": "Education", "notifications": 49},
        {"sector": "Legal, accounting & management services", "notifications": 40},
        {"sector": "Personal services", "notifications": 23}]
    with_total = "Health service providers2 71 14%\nFinance3 68 14%\nInsurance 42 8%\nLegal 37 7%\nRecruitment 35 7%\nTotal 253 51%"
    assert [r["notifications"] for r in orf.read_sectors("p", with_total)] == [71, 68, 42, 37, 35]
    with pytest.raises(orf.FigureNotFound):
        orf.read_sectors("p", "No table here 12")


def test_a_distribution_must_sum_to_the_periods_total():
    reading = {"1": 111, "2-10": 70, "11-100": 75, "101-1,000": 67, "1,001-5,000": 39, "5,001-10,000": 11,
               "10,001-25,000": 5, "25,001-50,000": 3, "50,001-100,000": 1, "100,001-250,000": 4,
               "250,001-500,000": None, "500,001-1,000,000": 3, "1,000,001-10,000,000": 4, "10,000,001+": None,
               "Unknown": 3}
    rows = orf.check_distribution("2022 H1", reading, 396)
    assert rows[0] == {"range": "1", "count": 111} and "250,001-500,000" not in [r["range"] for r in rows]
    with pytest.raises(orf.FigureNotFound):
        orf.check_distribution("2022 H1", reading, 497)
    with pytest.raises(orf.FigureNotFound):
        orf.check_distribution("2022 H1", {**reading, "2 to 10": 1}, 396)


def test_the_sub_category_average_is_removed_when_the_distribution_arrives():
    record = {"individuals_affected_average": 523998}
    figures = {"total_notifications": 446, "malicious_attacks": 289, "cyber_incidents_total": 192}
    out = orf.apply_figures(record, figures, SHARES, [{"sector": "Health", "notifications": 85}],
                            [{"range": "1", "count": 116}])
    assert out["individuals_affected_average"] is None and out["top_sectors"][0]["notifications"] == 85


def test_the_six_months_of_a_period_must_sum_to_its_total():
    reading = {"2020-06": 99, "2021-01": 45, "2021-02": 83, "2021-03": 102, "2021-04": 64, "2021-05": 78, "2021-06": 74}
    rows = orf.check_monthly("2021 H1", reading, 446)
    assert rows[0] == {"month": "Jan", "count": 45} and len(rows) == 6
    with pytest.raises(orf.FigureNotFound):
        orf.check_monthly("2021 H1", reading, 436)
    with pytest.raises(orf.FigureNotFound):
        orf.check_monthly("2021 H2", reading, 446)
    assert orf.apply_figures({}, {"total_notifications": 446, "malicious_attacks": 289, "cyber_incidents_total": 192},
                             SHARES, monthly=rows)["monthly_notifications"] == rows


def test_the_monthly_comparison_starts_when_the_database_does(tmp_path):
    db = tmp_path / "e.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE DeduplicatedEvents (event_date TEXT, status TEXT)")
    conn.executemany("INSERT INTO DeduplicatedEvents VALUES (?, 'Active')", [("2019-12-05",), ("2020-01-09",)])
    conn.commit()
    conn.close()
    data = prepare_oaic_monthly_comparison([
        {"year": 2019, "period": "H2", "monthly_notifications": [{"month": "Dec", "count": 84}]},
        {"year": 2020, "period": "H1", "monthly_notifications": [{"month": "Jan", "count": 63}]}], db_path=str(db))
    assert data == {"months": ["2020-01"], "oaic_counts": [63], "db_counts": [1]}


def test_the_chart_plots_percentages_and_names_periods_with_no_split():
    data = prepare_oaic_attack_types_data([
        {"year": 2024, "period": "H1", "start_month": 1, "ransomware": 80, "cyber_incident_breakdown_pct": SHARES},
        {"year": 2025, "period": "H2", "start_month": 7, "ransomware": None}])
    assert data["attack_types"]["ransomware"] == [23, None]
    assert data["attack_types"]["brute_force"] == [5, None]
    assert data["not_published"] == ["2025 H2"]
