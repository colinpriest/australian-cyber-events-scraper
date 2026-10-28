"""Conclusion wording for the recurrence reports follows the numbers it describes."""

from __future__ import annotations

import math
import re
from pathlib import Path

import pandas as pd
import pytest

from scripts.recurrence_conclusions import (
    DELAYED_PEAK_PERIODS,
    Contrast,
    contrast_from_record,
    describe_ci_width_sensitivity,
    describe_contrast,
    describe_joint_test,
    describe_lrt,
    event_note,
)

ANALYSIS_DIR = Path(__file__).resolve().parents[2] / "analysis"

# The 91-180 vs 0-90 day contrast the old report called "statistically strong".
PEAK_VS_IMMEDIATE = Contrast(
    comparison="91-180 vs 0-90",
    rate_ratio=2.206,
    ci_low=0.551,
    ci_high=8.831,
    p_value=0.132,
    test="greater",
    events={"91-180 days": 6, "0-90 days": 3},
)


def test_significant_two_sided():
    text = describe_contrast(Contrast(
        "long-term vs immediate", 2.5, 1.3, 4.8, 0.006, events={"long-term": 40, "immediate": 25},
    ))
    assert "Statistically significant at the 5% level" in text
    assert "reject equal rates" in text and "do not reject" not in text
    assert "rate ratio 2.50 (95% CI 1.30-4.80)" in text and "two-sided p = 0.006" in text
    assert "Small count" not in text and "opposite direction" not in text
    assert "40 in long-term and 25 in immediate" in text


def test_non_significant_p_0132_ci_spanning_one():
    text = describe_contrast(PEAK_VS_IMMEDIATE)
    assert text.startswith("91-180 vs 0-90: rate ratio 2.21 (95% CI 0.55-8.83)")
    assert "one-sided p = 0.132 (alternative: higher)" in text
    assert "Not statistically significant at the 5% level" in text
    assert "Statistically significant" not in text
    assert "do not reject the null of no increase" in text
    assert "0.55 to 8.83, which includes 1" in text
    assert "(borderline)" not in text
    assert "strong" not in text.lower()
    # 6 and 3 events: both flagged
    assert "Small count (6 in 91-180 days, 3 in 0-90 days" in text


def test_opposite_direction_is_named():
    one_sided = describe_contrast(Contrast("91-180 vs post-180", 0.8, 0.4, 1.6, 0.74, test="greater",
                                           events={"a": 20, "b": 50}))
    assert "opposite direction to the hypothesis (lower, not higher)" in one_sided
    two_sided = describe_contrast(Contrast("later vs early", 0.78, 0.35, 1.77, 0.557,
                                           hypothesis="greater", events={"a": 16, "b": 9}))
    assert "opposite direction to the hypothesis (lower, not higher)" in two_sided
    assert "do not reject equal rates" in two_sided
    falling = describe_contrast(Contrast("response vs immediate", 1.2, 0.6, 2.4, 0.7, test="less"))
    assert "opposite direction to the hypothesis (higher, not lower)" in falling
    as_expected = describe_contrast(Contrast("x", 1.5, 0.8, 2.8, 0.1, test="greater"))
    assert "opposite direction" not in as_expected


def test_borderline_and_threshold():
    borderline = describe_contrast(Contrast("x", 1.8, 0.95, 3.4, 0.07, events={"a": 30, "b": 30}))
    assert "Not statistically significant at the 5% level (borderline)" in borderline
    at_alpha = describe_contrast(Contrast("x", 1.8, 1.0, 3.2, 0.05))
    assert "Not statistically significant" in at_alpha  # p = 0.05 does not reject
    above_band = describe_contrast(Contrast("x", 1.2, 0.7, 2.0, 0.10))
    assert "(borderline)" not in above_band


def test_one_sided_significance_with_ci_spanning_one():
    text = describe_contrast(Contrast("x", 1.9, 0.97, 3.7, 0.031, test="greater",
                                      events={"a": 25, "b": 40}))
    assert "Statistically significant at the 5% level: the data reject the null of no increase" in text
    assert "two-sided 95% CI still includes 1, so the evidence is one-sided only" in text


def test_not_estimable_and_validation():
    text = describe_contrast(Contrast("x", math.nan, math.nan, math.nan, math.nan))
    assert "not estimable" in text and "significant" not in text
    with pytest.raises(ValueError):
        Contrast("x", 1.0, 0.5, 2.0, 0.5, test="higher")
    with pytest.raises(ValueError):
        Contrast("x", 1.0, 0.5, 2.0, 0.5, test="greater", hypothesis="less")


def test_event_note():
    assert event_note(None) == "Event counts behind this estimate were not recorded."
    assert "Small count" not in event_note({"a": 10, "b": 12})
    assert "Small count (9 in b; fewer than 10)" in event_note({"a": 10, "b": 9})


def test_joint_test_wording():
    post = Contrast("91-180 vs post-180", 1.127, 0.471, 2.696, 0.394, test="greater")
    text = describe_joint_test("delayed peak", "a delayed 91-180 day peak", 0.394, [PEAK_VS_IMMEDIATE, post])
    assert "intersection p = 0.394" in text
    assert "do not reject the null of no delayed peak" in text
    assert "point estimates have the hypothesised shape" in text
    assert "weakest part is 91-180 vs post-180 (p = 0.394)" in text

    opposite = Contrast("91-180 vs post-180", 0.9, 0.4, 2.0, 0.6, test="greater")
    text = describe_joint_test("delayed peak", "a delayed 91-180 day peak", 0.6, [PEAK_VS_IMMEDIATE, opposite])
    assert "do not have the hypothesised shape: 91-180 vs post-180 goes the other way" in text

    strong = [Contrast("a", 2.0, 1.2, 3.3, 0.004, test="greater"), Contrast("b", 1.8, 1.1, 2.9, 0.01, test="greater")]
    text = describe_joint_test("delayed peak", "a delayed 91-180 day peak", 0.01, strong)
    assert "The data support a delayed 91-180 day peak at the 5% level (one-sided)" in text
    assert "could not be computed" in describe_joint_test("x", "y", math.nan, strong)


def test_lrt_wording():
    assert describe_lrt("The model", 0.03, "a constant hazard", events=57) == (
        "The model rejects a constant hazard at the 5% level (LRT p = 0.030). It rests on 57 observed repeats."
    )
    text = describe_lrt("The model", 0.08, "a constant hazard", events=7)
    assert "does not reject a constant hazard at the 5% level (LRT p = 0.080, borderline)" in text
    assert "Small count" in text


def test_contrast_from_record_picks_the_test_that_was_run():
    events = {"immediate_0_90": 3, "peak_91_180": 6, "post_180": 47}
    directional = contrast_from_record(
        {"comparison": "91-180 vs 0-90", "rate_ratio": 2.206, "ci_low": 0.551, "ci_high": 8.831,
         "p_two_sided": 0.264, "directional_alternative": "greater", "p_directional": 0.132},
        DELAYED_PEAK_PERIODS, events,
    )
    assert directional.test == "greater" and directional.p_value == 0.132
    assert directional.events == {"91-180 days": 6, "0-90 days": 3}
    two_sided = contrast_from_record(
        {"comparison": "post-180 vs 0-90", "rate_ratio": 1.956, "ci_low": 0.596, "ci_high": 6.419,
         "p_two_sided": 0.268, "directional_alternative": "", "p_directional": None},
        DELAYED_PEAK_PERIODS, events,
    )
    assert two_sided.test == "two-sided" and two_sided.p_value == 0.268
    assert contrast_from_record({"comparison": "x", "rate_ratio": 1.0}, DELAYED_PEAK_PERIODS, None).events is None


def test_ci_width_sensitivity():
    rows = [
        {"variant_label": "All covariates", "comparison": "c", "ci_low": 0.5, "ci_high": 2.0},
        {"variant_label": "Drop size", "comparison": "c", "ci_low": 0.7, "ci_high": 1.4},
        {"variant_label": "All covariates", "comparison": "d", "ci_low": 0.5, "ci_high": 2.0},
        {"variant_label": "Drop size", "comparison": "d", "ci_low": 0.4, "ci_high": 2.5},
    ]
    text = describe_ci_width_sensitivity(rows, "All covariates")
    assert "For c, the narrowest 95% CI is the 'Drop size' model's (0.70-1.40), 50% narrower" in text
    assert "For d, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.50-2.00)" in text
    slight = describe_ci_width_sensitivity(
        [rows[0], {"variant_label": "Drop size", "comparison": "c", "ci_low": 0.51, "ci_high": 1.97}],
        "All covariates",
    )
    assert "no variant narrows the 95% CI by 5% or more" in slight


def test_phase_conclusions_from_model_tables():
    from scripts.analyze_recurrent_timing import phase_conclusions

    contrasts = pd.DataFrame([
        {"comparison": "91-180 vs 0-90", "rate_ratio": 2.206, "ci_low": 0.551, "ci_high": 8.831,
         "p_two_sided": 0.264, "directional_alternative": "greater", "p_directional": 0.132},
        {"comparison": "91-180 vs post-180", "rate_ratio": 1.127, "ci_low": 0.471, "ci_high": 2.696,
         "p_two_sided": 0.787, "directional_alternative": "greater", "p_directional": 0.394},
        {"comparison": "post-180 vs 0-90", "rate_ratio": 1.956, "ci_low": 0.596, "ci_high": 6.419,
         "p_two_sided": 0.268, "directional_alternative": "", "p_directional": math.nan},
    ])
    rates = pd.DataFrame({"delayed_peak_phase": ["immediate_0_90", "peak_91_180", "post_180"],
                          "events": [3, 6, 47]})
    lines = phase_conclusions(contrasts, rates, "delayed_peak_phase", DELAYED_PEAK_PERIODS,
                              "delayed peak", "a delayed 91-180 day peak", 0.394)
    assert lines[0].startswith("Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher)")
    assert len(lines) == 5 and lines[1] == ""
    assert "one-sided p = 0.132" in lines[2] and "Not statistically significant" in lines[2]
    assert "two-sided p = 0.268" in lines[4]


def test_dashboard_conclusions_from_summary():
    from scripts.build_recurrence_dashboard import delayed_peak_conclusions

    summary = {
        "delayed_peak_sensitivity_adjusted_contrasts": [
            {"comparison": "91-180 vs 0-90", "rate_ratio": 2.206, "ci_low": 0.551, "ci_high": 8.831,
             "p_two_sided": 0.264, "directional_alternative": "greater", "p_directional": 0.132},
        ],
        "delayed_peak_sensitivity_phase_rates": [
            {"delayed_peak_phase": "immediate_0_90", "events": 3},
            {"delayed_peak_phase": "peak_91_180", "events": 6},
        ],
        "delayed_peak_sensitivity_lrt": {"delayed_peak_intersection_p_value": 0.394},
    }
    lines = delayed_peak_conclusions(summary)
    assert "do not reject the null of no delayed peak" in lines[0]
    assert "Not statistically significant" in lines[1] and "Small count" in lines[1]
    assert delayed_peak_conclusions({}) == ["The delayed-peak contrasts were not computed for this run."]


SENTENCE = re.compile(r"^- (?P<text>.+?: rate ratio .+)$", re.MULTILINE)
P_VALUE = re.compile(r"(?:two-sided|one-sided) p (?P<op>=|<) (?P<p>\d+\.\d+)")


@pytest.mark.parametrize("report", sorted(ANALYSIS_DIR.glob("recurrent_timing*/recurrent_timing_report.md")),
                         ids=lambda p: p.parent.name)
def test_committed_reports_conclusions_match_their_p_values(report: Path):
    text = report.read_text(encoding="utf-8")
    for phrase in ("statistically strong", "suggestive", "clearer than", "much higher"):
        assert phrase not in text, f"{report.parent.name}: unsupported phrase {phrase!r}"
    sentences = SENTENCE.findall(text)
    assert sentences, "report has no generated contrast sentences"
    for sentence in sentences:
        match = P_VALUE.search(sentence)
        assert match, sentence
        significant = match.group("op") == "<" or float(match.group("p")) < 0.05
        assert ("Not statistically significant" not in sentence) == significant, sentence
