"""Tests for the supplier vs non-supplier comparison page (``scripts/build_supplier_comparison.py``)."""

from __future__ import annotations

import math
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pytest
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cyber_data_collector.reporting.population import ReportingPeriod  # noqa: E402
from scripts import build_supplier_comparison as sc  # noqa: E402

HOSTILE = '</script><script>alert("x")</script><img src=x onerror=alert(1)>'
PERIOD = ReportingPeriod.from_iso("2020-01-01", "2026-12-31")


# --- fixtures --------------------------------------------------------------------------------------------

@pytest.fixture
def conn():
    """Events covering every reason for being a supplier event, and the ones that are not."""
    c = sqlite3.connect(":memory:")
    c.executescript(
        """
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT, entity_type TEXT,
            entity_kind TEXT, size_estimate TEXT, size_confidence REAL, size_method TEXT);
        CREATE TABLE DeduplicatedEvents (
            deduplicated_event_id TEXT PRIMARY KEY, master_enriched_event_id TEXT, title TEXT, event_date TEXT,
            event_type TEXT, severity TEXT, records_affected INTEGER, vendor_organization_name TEXT, status TEXT,
            ai_involvement TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        CREATE TABLE EventDeduplicationMap (deduplicated_event_id TEXT, enriched_event_id TEXT);
        CREATE TABLE ASDRiskClassifications (classification_id TEXT PRIMARY KEY, deduplicated_event_id TEXT,
            severity_category TEXT, impact_type TEXT, updated_at TEXT);
        CREATE TABLE SupplierIncidents (supplier_incident_id TEXT PRIMARY KEY, supplier_name TEXT,
            supplier_event_id TEXT);
        CREATE TABLE SupplierIncidentLinks (supplier_incident_id TEXT, customer_entity_id INTEGER,
            customer_name TEXT, customer_event_id TEXT);
        """
    )
    c.executemany("INSERT INTO EntitiesV2 VALUES (?,?,?,?,?,?,?)", [
        (1, "Northwind Bank", "Financial Institution", "organisation", "HUGE", 0.9, "perplexity"),
        (2, "Harbour Council", "Government", "government_body", "MEDIUM", 0.8, "perplexity"),
        (3, "Tiny Bakery", "Retail", "organisation", "SMALL", 0.7, "perplexity"),
        (4, "CloudCo", "Technology", "organisation", "LARGE", 0.9, "perplexity"),
        (5, "Records Store", "Organization", "organisation", None, None, None),
        (6, HOSTILE, HOSTILE, "organisation", "LARGE", 0.5, "perplexity"),
    ])
    events = [
        # id, date, type, severity, records, vendor column, status, ai
        ("plain", "2023-03-01", "Ransomware", "High", 500, None, "Active", "No-AI"),
        ("customer", "2024-02-10", "Data Breach", "Medium", 20000, None, "Active", "No-AI"),
        ("own", "2024-02-01", "Data Breach", "Critical", 900000, None, "Active", "No-AI"),
        ("vendor_column", "2022-08-01", "Data Breach", "Medium", None, "CloudCo", "Active", "AI-attacker"),
        ("vendor_role", "2021-05-05", "Phishing", "Low", 0, "  ", "Active", None),
        ("provider_role", "2025-07-07", "Data Breach", "Unknown", 12, None, "Active", "No-AI"),
        ("holder", "2024-03-01", "Data Breach", "High", 300, None, "Active", "No-AI"),
        ("merged_away", "2024-03-02", "Data Breach", "High", 300, None, "Merged", "No-AI"),
        ("rejected", "2024-04-01", "Other", "Low", 5, "CloudCo", "Rejected", "No-AI"),
        ("before_window", "2019-06-01", "Data Breach", "High", 80, "CloudCo", "Active", "No-AI"),
        ("two_victims", "2023-09-09", HOSTILE, "High", 1000, None, "Active", "No-AI"),
    ]
    c.executemany("INSERT INTO DeduplicatedEvents (deduplicated_event_id, event_date, event_type, severity, "
                  "records_affected, vendor_organization_name, status, ai_involvement) VALUES (?,?,?,?,?,?,?,?)",
                  events)
    c.executemany("INSERT INTO DeduplicatedEventEntities VALUES (?,?,?)", [
        ("plain", 3, "victim"), ("customer", 1, "victim"), ("own", 4, "victim"), ("vendor_column", 2, "victim"),
        ("vendor_role", 2, "victim"), ("vendor_role", 4, "vendor"),
        ("provider_role", 1, "victim"), ("provider_role", 5, "service_provider"),
        ("holder", 2, "victim"), ("rejected", 3, "victim"), ("rejected", 4, "vendor"), ("before_window", 3, "victim"),
        ("two_victims", 1, "victim"), ("two_victims", 3, "victim"), ("plain", 4, "bystander"),
    ])
    c.executemany("INSERT INTO EventDeduplicationMap VALUES (?,?)", [("holder", "rec-h"), ("holder", "rec-m")])
    c.execute("UPDATE DeduplicatedEvents SET master_enriched_event_id = 'rec-m' WHERE deduplicated_event_id = 'merged_away'")
    c.executemany("INSERT INTO ASDRiskClassifications VALUES (?,?,?,?,?)", [
        ("a1", "plain", "C5", "Isolated compromise", "2026-01-01"),
        ("a2", "customer", "C3", "Extensive compromise", "2026-01-01"),
        ("a3", "own", "Insufficient information", "Insufficient information", "2026-01-01"),
        ("a4", "holder", "C4", "Isolated compromise", "2026-01-01"),
        ("a5", "holder", "C2", "Extensive compromise", "2026-02-01"),        # a later classification replaces it
    ])
    c.executemany("INSERT INTO SupplierIncidents VALUES (?,?,?)", [("inc1", "CloudCo", "own"), ("inc2", "Other", None)])
    c.executemany("INSERT INTO SupplierIncidentLinks VALUES (?,?,?,?)", [
        ("inc1", 1, "Northwind Bank", "customer"),
        ("inc1", 2, "Harbour Council", "merged_away"),      # counts for the Active event that holds its record
        ("inc1", 3, "Tiny Bakery", None),                   # a customer named only: no event
        ("inc2", 3, "Tiny Bakery", "rejected"),             # a rejected event counts for none
    ])
    return c


def _event(event_id: str, supplier: bool, *, attack: str = "Data Breach", severity: str = "High",
           records: Optional[float] = None, incidents: Optional[List[str]] = None,
           day: str = "2024-01-01") -> Dict[str, Any]:
    """A hand-made event row, as ``load_events`` returns them."""
    return {
        "event_id": event_id, "event_date": day, "records": records, "victims": 1,
        "bases": ["vendor"] if supplier else [], "incidents": incidents or [],
        "values": {
            "size": ("LARGE", None), "industry": ("Retail", None), "severity": (severity, None),
            "attack_type": (attack, None), "asd_severity": (None, "Not classified"),
            "asd_impact": (None, "Not classified"), "ai": ("No-AI", None),
            "half_year": (sc.half_year_label(day), None),
        },
    }


def _events(supplier_attacks: Dict[str, int], other_attacks: Dict[str, int]) -> List[Dict[str, Any]]:
    out, i = [], 0
    for supplier, attacks in ((True, supplier_attacks), (False, other_attacks)):
        for attack, n in attacks.items():
            for _ in range(n):
                i += 1
                out.append(_event(f"e{i:04d}", supplier, attack=attack, records=float(10 * i)))
    return out


# --- the group definition --------------------------------------------------------------------------------

def test_supplier_event_definition(conn):
    events = {e["event_id"]: e for e in sc.load_events(conn, PERIOD)}
    # Active events of the window only: no merged or rejected event, none before 2020.
    assert set(events) == {"plain", "customer", "own", "vendor_column", "vendor_role", "provider_role", "holder",
                           "two_victims"}
    assert {k: e["bases"] for k, e in events.items()} == {
        "plain": [],                              # a bystander role is not a supplier
        "customer": ["customer_event"],
        "own": ["supplier_event"],
        "vendor_column": ["vendor"],
        "vendor_role": ["vendor"],                # a blank vendor column alone would not count
        "provider_role": ["service_provider"],
        "holder": ["customer_event"],             # the link named the event merged into it
        "two_victims": [],
    }
    assert [k for k, e in events.items() if sc.is_supplier(e)] == [
        k for k, e in events.items() if e["bases"]]
    assert events["customer"]["incidents"] == ["inc1"] and events["holder"]["incidents"] == ["inc1"]
    assert events["vendor_column"]["incidents"] == []


def test_missing_supplier_tables_are_an_error_not_an_empty_group(conn):
    conn.execute("DROP TABLE SupplierIncidentLinks")
    with pytest.raises(sc.ComparisonError, match="SupplierIncidentLinks"):
        sc.load_events(conn, PERIOD)


def test_the_unit_is_the_event_and_victim_attributes_need_agreement(conn):
    events = {e["event_id"]: e for e in sc.load_events(conn, PERIOD)}
    assert len(events) == 8                                              # one row per event, whatever its victims
    assert events["two_victims"]["victims"] == 2
    assert events["two_victims"]["values"]["size"] == (None, "Several victims of different size bands")
    assert events["two_victims"]["values"]["industry"] == (None, "Several victims of different entity types")
    assert events["customer"]["values"]["size"] == ("HUGE", None)
    assert events["customer"]["values"]["industry"] == ("Financial Institution", None)
    assert events["own"]["values"]["asd_severity"] == (None, "Insufficient information")
    assert events["holder"]["values"]["asd_severity"] == ("C2 (very severe)", None)   # the latest classification
    assert events["customer"]["values"]["asd_severity"] == ("C3 (severe)", None)
    assert events["vendor_column"]["values"]["asd_severity"] == (None, "Not classified")
    assert events["provider_role"]["values"]["severity"] == (None, "Unknown or not recorded")
    assert events["vendor_role"]["values"]["ai"] == (None, "Not judged")
    assert events["vendor_role"]["records"] is None                      # a count under 1 is no count
    assert events["vendor_column"]["records"] is None
    assert events["customer"]["values"]["half_year"] == ("2024 H1", None)
    assert events["provider_role"]["values"]["half_year"] == ("2025 H2", None)


def test_size_and_industry_rules():
    assert sc.size_value([]) == (None, "No victim organisation recorded")
    assert sc.size_value([{"size": "UNKNOWN"}]) == (None, "UNKNOWN size band")
    assert sc.size_value([{"size": "SMALL"}, {"size": "SMALL"}]) == ("SMALL", None)
    assert sc.industry_value([{"entity_type": None}]) == (None, "Entity type not recorded")
    assert sc.industry_value([{"entity_type": "Organization"}]) == (None, "Generic type (Organization), no sector")
    assert sc.industry_value([{"entity_type": "Other"}]) == ("Other", None)       # a residual sector is a category


def test_one_event_per_incident_keeps_the_earliest_of_each_cluster():
    events = [
        _event("a", True, incidents=["i1"], day="2024-03-01"),
        _event("b", True, incidents=["i1", "i2"], day="2024-02-01"),
        _event("c", True, incidents=["i2"], day="2024-01-15"),      # i1 and i2 share b: one cluster, c is earliest
        _event("d", True, incidents=["i3"], day="2024-05-01"),
        _event("e", True, day="2024-05-02"),                        # a vendor recorded, no incident: kept
        _event("f", False, day="2024-05-03"),
    ]
    assert [e["event_id"] for e in sc.one_event_per_incident(events)] == ["c", "d", "e", "f"]


# --- the chi-squared table and the pooling rule ---------------------------------------------------------

def test_expected_counts_and_the_sparse_rule():
    expected = sc.expected_counts([10, 20, 2], [30, 60, 2])
    assert expected.shape == (2, 3)
    assert expected[0, 0] == pytest.approx(32 * 40 / 124)
    assert expected.sum() == pytest.approx(124)
    # 2 of 6 cells (the third category's) are under 5: 33% > 20%.
    assert sc.sparse_share(expected) == pytest.approx(2 / 6)
    assert sc.is_sparse(expected)
    assert not sc.is_sparse(sc.expected_counts([10, 20], [30, 60]))


def test_unordered_pooling_joins_the_smallest_categories_into_one_group():
    labels = ["Data Breach", "Ransomware", "Phishing", "Malware", "Insider"]
    groups, a, b = sc.pool_sparse(labels, [40, 30, 2, 1, 0], [60, 50, 6, 3, 2], ordered=False)
    assert groups == [["Data Breach"], ["Ransomware"], ["Phishing", "Malware", "Insider"]]
    assert (a, b) == ([40, 30, 3], [60, 50, 11])
    assert not sc.is_sparse(sc.expected_counts(a, b))
    assert sc.group_label(groups[2], ordered=False) == sc.POOLED_LABEL


def test_ordered_pooling_joins_a_sparse_category_with_its_smaller_neighbour():
    labels = ["C1", "C2", "C3", "C4", "C5", "C6"]
    groups, a, b = sc.pool_sparse(labels, [1, 20, 30, 25, 10, 0], [1, 40, 60, 50, 30, 2], ordered=True)
    assert groups == [["C1", "C2"], ["C3"], ["C4"], ["C5", "C6"]]          # pooled categories stay adjacent
    assert (a, b) == ([21, 30, 25, 10], [41, 60, 50, 32])
    assert sc.group_label(groups[0], ordered=True) == "C1 to C2"


def test_nothing_is_pooled_when_the_rule_holds():
    groups, a, b = sc.pool_sparse(["x", "y", "z"], [30, 40, 50], [35, 45, 55], ordered=False)
    assert groups == [["x"], ["y"], ["z"]] and (a, b) == ([30, 40, 50], [35, 45, 55])


def test_chi_squared_matches_scipy_and_reports_cramers_v():
    supplier, other = [40, 30, 10], [60, 90, 70]
    r = sc.categorical_test("k", "Attribute", ["a", "b", "c"], supplier, other, ordered=False)
    chi2, p, dof, _ = stats.chi2_contingency(np.array([supplier, other]), correction=False)
    assert r.ran and r.method == "Chi-squared test of homogeneity" and not r.exact
    assert (r.statistic, r.dof) == (pytest.approx(chi2), dof) and r.p == pytest.approx(p)
    assert r.effect == pytest.approx(math.sqrt(chi2 / 300)) and r.effect_name == "Cramér's V"
    assert (r.n_supplier, r.n_other) == (80, 220)
    assert r.pooled == [] and r.sparse_before == 0
    assert r.residuals["a"] > sc.RESIDUAL_CUT and r.residuals["c"] < -sc.RESIDUAL_CUT


def test_pooled_categories_are_tested_together_and_named():
    r = sc.categorical_test("k", "Attack type", ["Data Breach", "Ransomware", "Phishing", "Malware", "Insider"],
                            [40, 30, 2, 1, 0], [60, 50, 6, 3, 2], ordered=False)
    assert r.tested_labels == ["Data Breach", "Ransomware", sc.POOLED_LABEL]
    assert r.pooled == [["Phishing", "Malware", "Insider"]] and r.dof == 2
    assert r.sparse_before > sc.MAX_SPARSE_SHARE and r.sparse_after <= sc.MAX_SPARSE_SHARE
    assert "Phishing, Malware and Insider" in sc.pooling_note(r)


def test_a_sparse_two_by_two_table_uses_fishers_exact_test():
    r = sc.categorical_test("ai", "AI involvement", ["No-AI", "AI-attacker", "AI-misuse"], [50, 1, 0], [200, 6, 3],
                            ordered=False)
    assert r.tested_labels == ["No-AI", sc.POOLED_LABEL] and r.exact
    assert r.method == "Fisher's exact test (2 x 2)"
    assert r.p == pytest.approx(stats.fisher_exact([[50, 1], [200, 9]])[1])
    # An exact test has no statistic and no df: the row must not report another test's chi-squared.
    assert r.statistic is None and r.dof is None and r.statistic_name == ""
    assert sc.fmt_statistic(r) == "none (exact test)"
    assert r.effect_name == "Cramér's V" and 0 <= r.effect <= 1
    r.holm_p = r.p
    sentence = sc.conclusion(r, family=9)
    assert "Fisher's exact test (2 x 2): exact p = " in sentence and "chi-squared" not in sentence
    row = sc.tests_table([r], family=9)
    assert "<td class=n>none (exact test)</td><td class=n>-</td>" in row and "chi-squared" not in row
    assert "Fisher's exact test" in sc.pooling_note(r) and "no test statistic" in sc.pooling_note(r)


def test_empty_categories_are_dropped_and_untestable_tables_say_why():
    r = sc.categorical_test("k", "A", ["a", "b", "never"], [10, 20, 0], [20, 10, 0], ordered=True)
    assert r.tested_labels == ["a", "b"]
    none = sc.categorical_test("k", "A", ["a", "b"], [0, 0], [20, 10], ordered=False)
    assert not none.ran and none.reason == "a group has no event with a known value" and none.p is None
    one = sc.categorical_test("k", "A", ["a", "b"], [5, 0], [20, 0], ordered=False)
    assert not one.ran and one.reason == "fewer than two categories are observed"


def test_unknown_values_are_left_out_of_the_test_and_counted():
    events = _events({"Data Breach": 3}, {"Ransomware": 4})
    events[0]["values"]["attack_type"] = (None, "Not recorded")
    events[5]["values"]["attack_type"] = (None, "Not recorded")
    t = sc.tabulate(events, next(a for a in sc.ATTRIBUTES if a.key == "attack_type"))
    assert (t.labels, t.supplier, t.other) == (["Ransomware", "Data Breach"], [0, 2], [3, 0])
    assert t.left_out == [("Not recorded", 1, 1)] and t.group_sizes == (3, 4)


def test_mann_whitney_on_records_affected():
    supplier, other = [100.0, 5000.0, 80000.0, 1e6], [10.0, 20.0, 300.0, 4000.0, 50.0]
    r = sc.rank_test("records", "Records affected", supplier, other)
    u, p = stats.mannwhitneyu(supplier, other, alternative="two-sided")
    assert r.ran and r.statistic == pytest.approx(u) and r.p == pytest.approx(p)
    assert r.superiority == pytest.approx(u / 20) and r.effect == pytest.approx(abs(2 * u / 20 - 1))
    assert not sc.rank_test("records", "Records affected", [], other).ran
    assert sc.quartiles([1, 2, 3, 4, 5]) == (2.0, 3.0, 4.0)


# --- Holm ------------------------------------------------------------------------------------------------

def test_holm_adjustment():
    assert sc.holm([0.01, 0.04, 0.03, 0.005]) == pytest.approx([0.03, 0.06, 0.06, 0.02])
    assert sc.holm([0.5, 0.9]) == pytest.approx([1.0, 1.0])            # capped at 1
    assert sc.holm([0.02]) == pytest.approx([0.02])
    assert sc.holm([]) == []
    adjusted = sc.holm([0.04, 0.001, 0.03])
    assert adjusted == pytest.approx([0.06, 0.003, 0.06])              # monotone in the order of the raw values


def test_the_family_is_the_tests_that_ran():
    ran = sc.TestResult("a", "A", "categorical", True, p=0.01)
    other = sc.TestResult("b", "B", "categorical", True, p=0.04)
    skipped = sc.TestResult("c", "C", "categorical", False, reason="a group has no event with a known value")
    sc.adjust([ran, skipped, other])
    assert (ran.holm_p, other.holm_p, skipped.holm_p) == (pytest.approx(0.02), pytest.approx(0.04), None)


# --- wording ---------------------------------------------------------------------------------------------

DIRECTION_WORDS = ("more ", "less ", "fewer", "higher", "lower", "larger", "smaller", "greater", "tend")


def _result(p: float, holm_p: float, kind: str = "categorical") -> sc.TestResult:
    r = sc.categorical_test("severity", "Severity", ["High", "Medium", "Low"], [60, 30, 10], [100, 150, 150], True)
    if kind == "ranks":
        r = sc.rank_test("records", "Records affected", [5.0, 600.0, 7000.0, 9000.0], [1.0, 2.0, 3.0, 40.0])
    r.p, r.holm_p = p, holm_p
    return r


@pytest.mark.parametrize("kind", ["categorical", "ranks"])
def test_a_non_significant_result_is_no_evidence_with_no_direction(kind):
    text = sc.conclusion(_result(0.30, 0.60, kind), family=9)
    assert "no evidence of a difference" in text
    assert not any(word in text.lower() for word in DIRECTION_WORDS)
    assert "p = 0.300" in text and "Holm-adjusted p = 0.600" in text


@pytest.mark.parametrize("kind", ["categorical", "ranks"])
def test_a_raw_p_that_does_not_survive_adjustment_is_still_no_evidence(kind):
    text = sc.conclusion(_result(0.02, 0.18, kind), family=9)
    assert "no evidence of a difference" in text and "does not hold after adjustment for the 9 tests" in text
    assert not any(word in text.lower() for word in DIRECTION_WORDS)


def test_a_significant_result_names_the_categories_that_differ():
    text = sc.conclusion(_result(1e-9, 1e-8), family=9)
    assert text.startswith("Severity: a medium difference between supplier and non-supplier events (")
    assert "no evidence" not in text
    assert "More common among supplier events: High (60.0% of supplier events against 25.0%)" in text
    assert "Less common among supplier events: Low (10.0% of supplier events against 37.5%)" in text
    ranks = sc.conclusion(_result(1e-4, 1e-3, "ranks"), family=9)
    assert ranks.startswith("Records affected: a large difference between supplier and non-supplier events (")
    assert "Supplier events tend to have more records affected" in ranks


@pytest.mark.parametrize("effect,word", [(0.05, "negligible"), (0.099, "negligible"), (0.1, "small"),
                                         (0.17, "small"), (0.3, "medium"), (0.359, "medium"), (0.5, "large")])
def test_a_significant_difference_is_labelled_with_its_size_never_as_a_bare_difference(effect, word):
    for kind in ("categorical", "ranks"):
        r = _result(1e-6, 1e-5, kind)
        r.effect = effect
        assert sc.verdict(r) == f"{word} difference"
        assert sc.verdict_phrase(r) == f"a {word} difference"
        assert f": a {word} difference between supplier and non-supplier events" in sc.conclusion(r, family=9)
        assert sc.fmt_effect(r).endswith(f"({word})")
        table = sc.tests_table([r], family=9)
        assert f"<td>{word} difference</td>" in table and "<td>difference</td>" not in table


def test_verdicts_of_results_that_are_not_significant_or_not_run():
    assert sc.verdict(_result(0.2, 0.4)) == "no evidence of a difference"
    assert sc.verdict_phrase(_result(0.2, 0.4)) == "no evidence of a difference"
    skipped = sc.categorical_test("k", "A", ["a"], [3], [4], ordered=False)
    assert sc.verdict(skipped) == "not tested: fewer than two categories are observed"


def test_the_summary_gives_the_size_of_each_significant_difference():
    # Attack type differs strongly; severity, size, industry and AI are the same in both groups.
    events = _events({"Data Breach": 55, "Ransomware": 5}, {"Data Breach": 40, "Ransomware": 200})
    comparison = sc.analyse(events, PERIOD)
    attack = next(r for r in comparison.results if r.key == "attack_type")
    assert attack.significant and attack.size_word == "large"
    summary = " ".join(sc.headline_sentences(comparison))
    assert "a statistically significant difference between the groups at the 0.05 level" in summary
    # (records affected differ too in this fixture, so the size sentence can name both)
    assert re.search(r"Size of the difference: (?:[^.]*; )?large for [^;.]*Attack type", summary), summary
    assert "differ between the groups" not in summary             # never a bare "differ"
    page = sc.build_html(comparison, "test")
    assert "<td>large difference</td>" in page and "<td>difference</td>" not in page


def test_asd_categories_carry_words_not_just_codes():
    assert sc.ASD_ORDER == ("C1 (most severe)", "C2 (very severe)", "C3 (severe)", "C4 (moderate)", "C5 (low)",
                            "C6 (least severe)")
    assert sc.asd_category("C4") == ("C4 (moderate)", None)
    assert sc.asd_category("c1") == ("C1 (most severe)", None)
    assert sc.asd_category("Insufficient information") == (None, "Insufficient information")
    assert sc.asd_category(None) == (None, "Not classified")
    assert sc.asd_category("C9") == (None, "Unrecognised value (C9)")
    # A pooled range of an ordered attribute names both ends in words.
    assert sc.group_label(list(sc.ASD_ORDER[:2]), ordered=True) == "C1 (most severe) to C2 (very severe)"
    events = [_event(f"e{i}", i % 2 == 0) for i in range(40)]
    for i, e in enumerate(events):
        e["values"]["asd_severity"] = (sc.ASD_ORDER[2 if i % 4 < 2 else 3], None)
    page = sc.build_html(sc.analyse(events, PERIOD), "test")
    assert '<div class="cat">C3 (severe)</div>' in page and "<td>C4 (moderate)</td>" in page
    assert '<div class="cat">C3</div>' not in page


def test_an_untested_attribute_says_so():
    r = sc.categorical_test("k", "ASD impact type", ["a"], [3], [4], ordered=False)
    assert sc.conclusion(r, family=8) == "ASD impact type: not tested (fewer than two categories are observed)."


def test_the_summary_and_the_page_carry_no_direction_for_non_significant_results():
    # The two groups have the same attack-type mix and overlapping record counts: nothing differs.
    events = _events({"Data Breach": 30, "Ransomware": 30}, {"Data Breach": 60, "Ransomware": 60})
    for i, e in enumerate(events):
        e["records"] = float(100 + (i * 37) % 900)
    comparison = sc.analyse(events, PERIOD)
    attack = next(r for r in comparison.results if r.key == "attack_type")
    assert attack.ran and not attack.significant
    sentences = sc.headline_sentences(comparison)
    assert sentences[0] == ("Of 180 Active events dated 2020-01-01 to 2026-12-31, 60 (33.3%) are supplier events "
                            "and 120 (66.7%) are non-supplier events.")
    assert any(s.startswith("No evidence of a difference:") and "Attack type" in s for s in sentences)
    for r in comparison.results:
        if r.ran and not r.significant:
            assert not any(word in sc.conclusion(r, comparison.family).lower() for word in DIRECTION_WORDS)
    page = sc.build_html(comparison, "test")
    assert "Attack type: no evidence of a difference between supplier and non-supplier events" in page


def test_a_changed_conclusion_under_one_event_per_incident_is_reported():
    # Every supplier ransomware event belongs to one incident; with one kept the difference is gone.
    events = [_event(f"s{i:03d}", True, attack="Ransomware", incidents=["big"], day=f"2024-01-{i + 1:02d}")
              for i in range(25)]
    events += [_event(f"t{i:03d}", True, attack="Data Breach") for i in range(25)]
    events += [_event(f"o{i:03d}", False, attack="Ransomware" if i % 10 == 0 else "Data Breach") for i in range(250)]
    comparison = sc.analyse(events, PERIOD)
    assert len(comparison.events) - len(comparison.sensitivity_events) == 24
    attack = next(r for r in comparison.results if r.key == "attack_type")
    again = next(r for r in comparison.sensitivity if r.key == "attack_type")
    assert attack.significant and not again.significant
    sentence = sc.sensitivity_sentence(comparison)
    assert "1 of 1 conclusion changes: Attack type (a medium difference on all events" in sentence
    assert "Cramér's V = " in sentence
    assert "no evidence of a difference with one event per incident" in sentence
    assert "Check, one event per supplier incident" in sc.build_html(comparison, "test")


# --- the page --------------------------------------------------------------------------------------------

def test_hostile_text_is_escaped_everywhere(conn):
    comparison = sc.collect(conn, PERIOD)
    page = sc.build_html(comparison, HOSTILE)
    assert "<script" not in page.lower()                 # the page has no script at all
    assert "<img" not in page.lower() and "onerror=alert(1)>" not in page
    assert "&lt;/script&gt;&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in page
    # The hostile attack type is a category of the chart, its tooltip and its table.
    assert page.count("&lt;img src=x onerror=alert(1)&gt;") >= 3


def test_the_page_states_the_definition_the_unit_and_the_group_sizes(conn):
    comparison = sc.collect(conn, PERIOD)
    page = sc.build_html(comparison, "from snapshot snap-test")
    assert len(comparison.events) == 8 and len(comparison.supplier_events) == 6
    assert "Of 8 Active events dated 2020-01-01 to 2026-12-31, 6 (75.0%) are supplier events and 2 (25.0%) are " \
           "non-supplier events." in page
    assert "<b>Supplier event:</b> an Active event that is linked to a supplier incident" in page
    assert "<b>Unit: the event</b>, not the victim organisation-event pair" in page
    assert "Are the differences statistically significant?" in page
    assert page.rstrip().endswith("</html>") and "from snapshot snap-test" in page
    assert "http://" not in page and "https://" not in page          # self-contained: no external file
    for attribute in sc.ATTRIBUTES:
        assert f'id="{attribute.key}"' in page
    assert f'id="{sc.RECORDS_KEY}"' in page and 'class="box s"' in page
    assert "@media (prefers-color-scheme: dark)" in page and "@media (max-width: 640px)" in page


def test_percentages_are_within_each_group_with_counts_in_tooltips():
    events = _events({"Data Breach": 3, "Ransomware": 1}, {"Data Breach": 2, "Ransomware": 6})
    t = sc.tabulate(events, next(a for a in sc.ATTRIBUTES if a.key == "attack_type"))
    chart = sc.bar_chart(t)
    assert 'title="Supplier events: Data Breach - 3 of 4 (75.0%)"' in chart
    assert 'title="Non-supplier events: Data Breach - 2 of 8 (25.0%)"' in chart
    table = sc.distribution_table(t, sc.categorical_test("attack_type", "Attack type", t.labels, t.supplier,
                                                         t.other, False))
    assert "<td class=n>3</td><td class=n>75.0%</td><td class=n>2</td><td class=n>25.0%</td>" in table


def test_records_table_reports_median_iqr_and_share_missing():
    events = _events({"Data Breach": 4}, {"Data Breach": 4})
    for e, records in zip(events, [10, 20, 30, None, 100, None, None, None]):
        e["records"] = records
    supplier, other = sc.records_summaries(events)
    assert (supplier.known, supplier.missing, other.known, other.missing) == (3, 1, 1, 3)
    table = sc.records_table(supplier, other)
    assert "1 (25.0%)" in table and "3 (75.0%)" in table
    assert "<td class=n>20</td><td class=n>15 to 25</td>" in table


def test_histogram_bins_are_powers_of_ten():
    # 10^k up to, not including, 10^(k+1); a count on the axis top goes in the last bin.
    assert sc.records_bins([1, 9, 9.5, 10, 99, 100, 999, 1000], 4) == [3, 2, 2, 1]
    assert sc.records_bins([10_000], 4) == [0, 0, 0, 1]
    assert sc.records_bins([], 3) == [0, 0, 0]
    assert sc.bin_label(0, 4, 5000) == "1 to 9" and sc.bin_label(3, 4, 5000) == "1,000 to 9,999"
    assert sc.bin_label(3, 4, 10_000) == "1,000 to 10,000"          # a count reaches the axis top
    summaries = (sc.RecordsSummary(sc.SUPPLIER, 5, 3, [20.0, 300.0, 5000.0]),
                 sc.RecordsSummary(sc.OTHER, 9, 4, [1.0, 2.0, 30.0, 400.0]))
    assert sc.log_decades(*summaries) == 4


def test_histogram_compares_the_shares_of_each_group_with_counts_in_tooltips():
    events = _events({"Data Breach": 4}, {"Data Breach": 8})
    for e, records in zip(events, [50, 500, 5000, None, 1, 2, 3, 40, 50, 600, None, None]):
        e["records"] = records
    supplier, other = sc.records_summaries(events)
    chart = sc.records_histogram(supplier, other)
    assert chart.count('class="hbin"') == 4 and chart.count('class="hbar s"') == 4
    assert 'title="Supplier events: 10 to 99 records - 1 of 3 (33.3%)"' in chart
    assert 'title="Non-supplier events: 1 to 9 records - 3 of 6 (50.0%)"' in chart
    # Heights are shares within the group on one scale: the largest share (50%) is the tallest bar.
    assert 'style="height:84.00%" title="Non-supplier events: 1 to 9 records' in chart
    assert 'style="height:56.00%" title="Supplier events: 10 to 99 records' in chart
    assert 'style="height:0.00%" title="Supplier events: 1 to 9 records - 0 of 3 (0.0%)"></div>' in chart
    table = sc.histogram_table(supplier, other)
    assert "<td>10 to 99</td><td class=n>1</td><td class=n>33.3%</td><td class=n>2</td><td class=n>33.3%</td>" in table
    assert "<td class=n><b>3</b></td>" in table and "<td class=n><b>6</b></td>" in table
    page = sc.build_html(sc.analyse(events, PERIOD), "test")
    assert 'class="hist"' in page and "Histogram" in page and 'class="box s"' in page   # both visualisations


def test_histogram_with_an_empty_group_draws_no_bar_for_it():
    supplier = sc.RecordsSummary(sc.SUPPLIER, 2, 0, [])
    other = sc.RecordsSummary(sc.OTHER, 3, 2, [5.0, 50.0])
    chart = sc.records_histogram(supplier, other)
    assert chart.count('style="height:0.00%" title="Supplier events') == 2
    assert "0 of 0 (-)" in chart


def test_the_page_is_published_with_the_other_outputs(conn, tmp_path):
    from cyber_data_collector.publication import outputs as pub
    from cyber_data_collector.publication import snapshot as snapshots

    assert pub.DEFAULT_BUILDERS["supplier_comparison"] is pub.build_supplier_comparison
    db = tmp_path / "snap.db"
    conn.commit()                 # a backup waits for the fixture's open transaction
    disk = sqlite3.connect(db)
    conn.backup(disk)
    disk.close()
    snap = snapshots.Snapshot("snap-test", "0" * 64, "2026-11-06T16:06:34", str(db), db, db.stat().st_size)
    ctx = pub.BuildContext(snap, tmp_path, tmp_path, tmp_path, tmp_path, db)
    (built,) = pub.build_supplier_comparison(ctx)
    page = built.read_text(encoding="utf-8")
    assert built.name == "supplier_comparison.html"
    assert "from snapshot snap-test" in page and "dated 2020-01-01 to 2026-11-06" in page
