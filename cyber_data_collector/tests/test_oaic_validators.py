"""Tests for the OAIC dashboard validators added per the 10-check plan."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.oaic.oaic_validators import (
    CANONICAL_TIME_BUCKETS,
    OAIC_SECTOR_NOTIF_MAX,
    OAICValidationError,
    normalize_time_bucket,
    quarantine_extraction,
    sanitize_top_sectors,
    validate_cross_page_totals,
    validate_displayed_semester,
    validate_inter_semester_delta,
    validate_page_payload,
)


# ----------------------------------------------------------------------
# sanitize_top_sectors — safety net for implausible per-sector counts
# ----------------------------------------------------------------------

def test_sanitize_nulls_count_above_cap():
    sectors = [{"sector": "Education", "notifications": 448}]
    out = sanitize_top_sectors(sectors, total_notifications=527)
    assert out[0]["sector"] == "Education"
    assert out[0]["notifications"] is None  # 448 > 250 cap


def test_sanitize_nulls_count_exceeding_total():
    # 240 is under the 250 cap but impossible when the period total is 200.
    sectors = [{"sector": "Retail", "notifications": 240}]
    out = sanitize_top_sectors(sectors, total_notifications=200)
    assert out[0]["notifications"] is None


def test_sanitize_keeps_plausible_counts():
    sectors = [
        {"sector": "Health", "notifications": 96},
        {"sector": "Finance", "notifications": 73},
        {"sector": "Unknown", "notifications": None},
    ]
    out = sanitize_top_sectors(sectors, total_notifications=532)
    assert [s["notifications"] for s in out] == [96, 73, None]


def test_sanitize_handles_empty_and_missing_total():
    assert sanitize_top_sectors([], None) == []
    assert sanitize_top_sectors(None, None) == []
    # With no total, the absolute cap still applies.
    out = sanitize_top_sectors([{"sector": "X", "notifications": OAIC_SECTOR_NOTIF_MAX + 1}], None)
    assert out[0]["notifications"] is None


# ----------------------------------------------------------------------
# Rank 1 — Pydantic page schema validation
# ----------------------------------------------------------------------

VALID_PAGE2 = {
    "displayed_semester": "Jan-Jun 2025",
    "total_notifications": 532,
    "human_error_pct": 37,
    "malicious_attacks_pct": 60,
    "system_faults_pct": 3,
    "monthly_notifications": [
        {"month": "Jan", "count": 80},
        {"month": "Feb", "count": 90},
    ],
    "top_sectors": [
        {"sector": "Health service providers", "notifications": 96},
        {"sector": "Finance (incl. superannuation)", "notifications": 73},
        {"sector": "Australian Government", "notifications": 67},
        {"sector": "Education", "notifications": 38},
        {"sector": "Legal, accounting & management services", "notifications": 37},
    ],
}


def test_page2_valid_payload_passes():
    out = validate_page_payload(2, VALID_PAGE2, "Jan-Jun 2025")
    assert out["total_notifications"] == 532
    assert len(out["top_sectors"]) == 5


def test_page2_donut_sum_failure_is_rejected():
    bad = dict(VALID_PAGE2, human_error_pct=10, malicious_attacks_pct=10, system_faults_pct=10)
    with pytest.raises(OAICValidationError) as exc:
        validate_page_payload(2, bad, "Jan-Jun 2025")
    assert any("donut" in m for m in exc.value.errors)


def test_page2_top_sectors_below_floor_rejected():
    """Rank 9: a top-5 sector with a tiny count is implausible."""
    bad = json.loads(json.dumps(VALID_PAGE2))
    bad["top_sectors"][0]["notifications"] = 2
    with pytest.raises(OAICValidationError):
        validate_page_payload(2, bad, "Jan-Jun 2025")


# ----------------------------------------------------------------------
# Rank 3 — page-9 rank-value detector
# ----------------------------------------------------------------------

def test_page9_rank_view_rejected():
    """If every cell is in 1-5 across the matrix with multiple distinct
    values, we're seeing the 'rank' filter view, not real counts."""
    rank_view = {
        "displayed_semester": "Jan-Jun 2025",
        "sector_by_source": [
            {"sector": "A", "human_error": 1, "malicious_or_criminal": 2, "system_fault": 3},
            {"sector": "B", "human_error": 4, "malicious_or_criminal": 5, "system_fault": 1},
            {"sector": "C", "human_error": 2, "malicious_or_criminal": 3, "system_fault": 4},
            {"sector": "D", "human_error": 5, "malicious_or_criminal": 1, "system_fault": 2},
            {"sector": "E", "human_error": 3, "malicious_or_criminal": 4, "system_fault": 5},
        ],
    }
    with pytest.raises(OAICValidationError) as exc:
        validate_page_payload(9, rank_view, "Jan-Jun 2025")
    assert any("rank" in m.lower() for m in exc.value.errors)


def test_page9_real_counts_pass():
    real = {
        "displayed_semester": "Jan-Jun 2025",
        "sector_by_source": [
            {"sector": "Australian Government", "human_error": 32, "malicious_or_criminal": 32, "system_fault": 3},
            {"sector": "Education", "human_error": 28, "malicious_or_criminal": 6, "system_fault": 2},
            {"sector": "Finance (incl. superannuation)", "human_error": 22, "malicious_or_criminal": 48, "system_fault": 1},
            {"sector": "Health service providers", "human_error": 47, "malicious_or_criminal": 42, "system_fault": 3},
            {"sector": "Legal, accounting & management services", "human_error": 5, "malicious_or_criminal": 32, "system_fault": 0},
        ],
    }
    out = validate_page_payload(9, real, "Jan-Jun 2025")
    assert len(out["sector_by_source"]) == 5


# ----------------------------------------------------------------------
# Rank 6 + 8 — time-bucket sum + canonical normalization
# ----------------------------------------------------------------------

def test_time_buckets_sum_to_100():
    payload = {
        "displayed_semester": "Jan-Jun 2025",
        "time_to_identify_pct": [
            {"bucket": "Unknown", "current_pct": 5, "previous_pct": 4},
            {"bucket": "<= 10 days", "current_pct": 56, "previous_pct": 53},
            {"bucket": "11-20 days", "current_pct": 8, "previous_pct": 9},
            {"bucket": "21-30 days", "current_pct": 4, "previous_pct": 4},
            {"bucket": "> 30 days", "current_pct": 27, "previous_pct": 30},
        ],
    }
    out = validate_page_payload(7, payload, "Jan-Jun 2025")
    assert len(out["time_to_identify_pct"]) == 5


def test_time_buckets_sum_violation_rejected():
    """If percentages add to ~50, one bar was misread."""
    payload = {
        "time_to_identify_pct": [
            {"bucket": "Unknown", "current_pct": 5, "previous_pct": 4},
            {"bucket": "<= 10 days", "current_pct": 26, "previous_pct": 23},
            {"bucket": "11-20 days", "current_pct": 8, "previous_pct": 9},
            {"bucket": "21-30 days", "current_pct": 4, "previous_pct": 4},
            {"bucket": "> 30 days", "current_pct": 7, "previous_pct": 10},
        ],
    }
    with pytest.raises(OAICValidationError) as exc:
        validate_page_payload(7, payload, "Jan-Jun 2025")
    assert any("100" in m for m in exc.value.errors)


def test_time_buckets_alias_normalisation():
    """Rank 8: '0-10 days' is canonicalised to '<= 10 days' so the
    payload validates and downstream code sees consistent labels."""
    payload = {
        "displayed_semester": "Jan-Jun 2025",
        "time_to_identify_pct": [
            {"bucket": "Unknown", "current_pct": 5, "previous_pct": 4},
            {"bucket": "0-10 days", "current_pct": 56, "previous_pct": 53},
            {"bucket": "11-20 days", "current_pct": 8, "previous_pct": 9},
            {"bucket": "21-30 days", "current_pct": 4, "previous_pct": 4},
            {"bucket": "> 30 days", "current_pct": 27, "previous_pct": 30},
        ],
    }
    out = validate_page_payload(7, payload, "Jan-Jun 2025")
    buckets = [e["bucket"] for e in out["time_to_identify_pct"]]
    assert buckets == list(CANONICAL_TIME_BUCKETS)


def test_normalize_time_bucket_aliases():
    """Direct unit test on the normaliser - covers the alias surface."""
    assert normalize_time_bucket("≤ 10 days") == "<= 10 days"
    assert normalize_time_bucket("0-10 days") == "<= 10 days"
    assert normalize_time_bucket("more than 30 days") == "> 30 days"
    assert normalize_time_bucket("Unknown") == "Unknown"
    assert normalize_time_bucket("garbage") is None


# ----------------------------------------------------------------------
# Rank 2 — displayed_semester echo
# ----------------------------------------------------------------------

def test_displayed_semester_match_passes():
    validate_displayed_semester(2, {"displayed_semester": "Jan-Jun 2025"}, "Jan-Jun 2025")


def test_displayed_semester_mismatch_raises():
    with pytest.raises(OAICValidationError):
        validate_displayed_semester(2, {"displayed_semester": "Jul-Dec 2024"}, "Jan-Jun 2025")


def test_displayed_semester_full_month_name_tolerated():
    validate_displayed_semester(
        2, {"displayed_semester": "January-June 2025"}, "Jan-Jun 2025"
    )


# ----------------------------------------------------------------------
# Rank 5 — breach sources sanity
# ----------------------------------------------------------------------

def test_breach_sources_distinct_period_labels_required():
    payload = {
        "displayed_semester": "Jan-Jun 2025",
        "current_period_label": "Jan-Jun 2025",
        "previous_period_label": "Jan-Jun 2025",
        "breach_sources": {
            "human_error": {"current_period": 100, "previous_period": 80},
            "malicious_attack": {"current_period": 200, "previous_period": 180},
            "system_fault": {"current_period": 10, "previous_period": 12},
        },
    }
    with pytest.raises(OAICValidationError):
        validate_page_payload(6, payload, "Jan-Jun 2025")


# ----------------------------------------------------------------------
# Rank 7 — cross-page totals
# ----------------------------------------------------------------------

def test_cross_page_totals_consistent():
    consolidated = {
        "total_notifications": 532,
        "malicious_attacks": 308,
        "human_error": 193,
        "system_faults": 17,
        "monthly_notifications": [
            {"month": "Jan", "count": 90}, {"month": "Feb", "count": 85},
            {"month": "Mar", "count": 95}, {"month": "Apr", "count": 80},
            {"month": "May", "count": 90}, {"month": "Jun", "count": 92},
        ],
    }
    warnings = validate_cross_page_totals(consolidated, "Jan-Jun 2025")
    assert warnings == []


def test_cross_page_totals_huge_divergence_raises():
    consolidated = {
        "total_notifications": 532,
        "malicious_attacks": 100,  # much smaller than total
        "human_error": 50,
        "system_faults": 5,  # source_sum=155, total=532, 71% deviation
        "monthly_notifications": [{"month": "Jan", "count": 80}],
    }
    with pytest.raises(OAICValidationError):
        validate_cross_page_totals(consolidated, "Jan-Jun 2025")


def test_cross_page_totals_minor_divergence_warns_only():
    consolidated = {
        "total_notifications": 532,
        "malicious_attacks": 280,
        "human_error": 180,
        "system_faults": 15,  # source_sum=475, 11% deviation - warn but no raise
        "monthly_notifications": [],
    }
    warnings = validate_cross_page_totals(consolidated, "Jan-Jun 2025")
    assert any("source_sum" in w for w in warnings)


# ----------------------------------------------------------------------
# Rank 10 — inter-semester delta
# ----------------------------------------------------------------------

def test_inter_semester_delta_normal_change_silent():
    new = {"total_notifications": 532, "top_sectors": []}
    prior = {"total_notifications": 480, "top_sectors": []}
    assert validate_inter_semester_delta(new, prior) == []


def test_inter_semester_delta_huge_swing_warns():
    new = {"total_notifications": 1200, "top_sectors": []}
    prior = {"total_notifications": 480, "top_sectors": []}
    warnings = validate_inter_semester_delta(new, prior)
    assert any("swung" in w for w in warnings)


def test_inter_semester_delta_top_sectors_reordered_warns():
    new = {
        "total_notifications": 500,
        "top_sectors": [
            {"sector": "Retail"}, {"sector": "Energy"}, {"sector": "Telco"},
        ],
    }
    prior = {
        "total_notifications": 480,
        "top_sectors": [
            {"sector": "Health service providers"},
            {"sector": "Finance"}, {"sector": "Education"},
        ],
    }
    warnings = validate_inter_semester_delta(new, prior)
    assert any("re-ordered" in w for w in warnings)


# ----------------------------------------------------------------------
# Quarantine helper
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# Period-swap auto-correction (the bug behind 8+ "validation failed"
# errors per scrape on pages 6/7/8). The vision LLM consistently puts
# the previous-period values into the current_* fields and vice-versa.
# Auto-correction restores the right alignment when we can tell which
# label matches the requested semester.
# ----------------------------------------------------------------------

def test_page6_swap_auto_corrects():
    """Real-world Jul-Dec 2023 case where LLM swapped both labels and
    every per-source count."""
    swapped = {
        "displayed_semester": "Jan-Jun 2023",
        "current_period_label": "Jan-Jun 2023",
        "previous_period_label": "Jul-Dec 2023",
        "breach_sources": {
            "human_error": {"current_period": 106, "previous_period": 142},
            "malicious_attack": {"current_period": 290, "previous_period": 322},
            "system_fault": {"current_period": 14, "previous_period": 19},
        },
    }
    out = validate_page_payload(6, swapped, "Jul-Dec 2023")
    assert out["current_period_label"] == "Jul-Dec 2023"
    assert out["previous_period_label"] == "Jan-Jun 2023"
    assert out["displayed_semester"] == "Jul-Dec 2023"
    bs = out["breach_sources"]
    assert bs["human_error"]["current_period"] == 142
    assert bs["human_error"]["previous_period"] == 106
    assert bs["malicious_attack"]["current_period"] == 322
    assert bs["malicious_attack"]["previous_period"] == 290
    assert bs["system_fault"]["current_period"] == 19
    assert bs["system_fault"]["previous_period"] == 14


def test_page7_swap_auto_corrects():
    swapped = {
        "displayed_semester": "Jul-Dec 2023",
        "current_period_label": "Jan-Jun 2023",
        "previous_period_label": "Jul-Dec 2023",
        "time_to_identify_pct": [
            {"bucket": "Unknown", "current_pct": 4, "previous_pct": 5},
            {"bucket": "<= 10 days", "current_pct": 53, "previous_pct": 56},
            {"bucket": "11-20 days", "current_pct": 9, "previous_pct": 8},
            {"bucket": "21-30 days", "current_pct": 4, "previous_pct": 4},
            {"bucket": "> 30 days", "current_pct": 30, "previous_pct": 27},
        ],
    }
    out = validate_page_payload(7, swapped, "Jul-Dec 2023")
    assert out["current_period_label"] == "Jul-Dec 2023"
    assert out["previous_period_label"] == "Jan-Jun 2023"
    assert out["time_to_identify_pct"][0]["current_pct"] == 5
    assert out["time_to_identify_pct"][0]["previous_pct"] == 4


def test_page8_swap_auto_corrects():
    swapped = {
        "displayed_semester": "Jul-Dec 2023",
        "current_period_label": "Jan-Jun 2023",
        "previous_period_label": "Jul-Dec 2023",
        "time_to_notify_pct": [
            {"bucket": "Unknown", "current_pct": 0, "previous_pct": 0},
            {"bucket": "<= 10 days", "current_pct": 30, "previous_pct": 26},
            {"bucket": "11-20 days", "current_pct": 19, "previous_pct": 18},
            {"bucket": "21-30 days", "current_pct": 25, "previous_pct": 28},
            {"bucket": "> 30 days", "current_pct": 26, "previous_pct": 28},
        ],
    }
    out = validate_page_payload(8, swapped, "Jul-Dec 2023")
    assert out["current_period_label"] == "Jul-Dec 2023"
    # current_pct should now be the values previously stored in previous_pct
    assert out["time_to_notify_pct"][1]["current_pct"] == 26
    assert out["time_to_notify_pct"][1]["previous_pct"] == 30


def test_page6_no_swap_when_already_correct():
    """No swap should happen when current_period_label already matches
    the requested semester."""
    correct = {
        "displayed_semester": "Jul-Dec 2024",
        "current_period_label": "Jul-Dec 2024",
        "previous_period_label": "Jan-Jun 2024",
        "breach_sources": {
            "human_error": {"current_period": 171, "previous_period": 155},
            "malicious_attack": {"current_period": 410, "previous_period": 346},
            "system_fault": {"current_period": 12, "previous_period": 17},
        },
    }
    out = validate_page_payload(6, correct, "Jul-Dec 2024")
    # Values must be unchanged.
    assert out["breach_sources"]["human_error"]["current_period"] == 171
    assert out["breach_sources"]["malicious_attack"]["current_period"] == 410


def test_no_swap_when_neither_label_matches():
    """If neither label matches the requested semester, leave the data
    alone - the failure is a different one (e.g. wrong-period capture)."""
    bad = {
        "displayed_semester": "Jan-Jun 2025",
        "current_period_label": "Jan-Jun 2025",
        "previous_period_label": "Jul-Dec 2024",
        "breach_sources": {
            "human_error": {"current_period": 100, "previous_period": 50},
            "malicious_attack": {"current_period": 200, "previous_period": 100},
            "system_fault": {"current_period": 10, "previous_period": 5},
        },
    }
    # Requested period is Jul-Dec 2023 - matches neither label, so
    # validator should NOT swap. The displayed_semester check elsewhere
    # will fail this with its own clearer error.
    try:
        out = validate_page_payload(6, bad, "Jul-Dec 2023")
        assert out["current_period_label"] == "Jan-Jun 2025"  # unchanged
    except OAICValidationError:
        pass  # period_label_distinct check might still fire - either is acceptable


# ----------------------------------------------------------------------
# Cross-page tolerance widens for older periods
# ----------------------------------------------------------------------

def test_cross_page_old_period_tolerance_widened():
    """For pre-2022 periods, source-sum can deviate up to 20% before
    warning and 40% before raising."""
    consolidated = {
        "year": 2021,
        "period": "H1",
        "total_notifications": 441,
        "malicious_attacks": 280,
        "human_error": 200,
        "system_faults": 30,  # source_sum=510, 16% deviation
        "monthly_notifications": [],
    }
    # 16% deviation: under the 20% warn threshold for pre-2022, so silent.
    warnings = validate_cross_page_totals(consolidated, "Jan-Jun 2021")
    assert warnings == []


def test_cross_page_modern_period_warns_at_15pct():
    """For 2022+ periods, the 10% warn / 25% raise thresholds still apply."""
    consolidated = {
        "year": 2024,
        "period": "H2",
        "total_notifications": 600,
        "malicious_attacks": 400,
        "human_error": 280,
        "system_faults": 20,  # source_sum=700, ~17% deviation
        "monthly_notifications": [],
    }
    warnings = validate_cross_page_totals(consolidated, "Jul-Dec 2024")
    assert any("source_sum" in w for w in warnings)


def test_cross_page_old_period_still_fails_on_huge_divergence():
    """Even old periods must fail on a 50%+ divergence - that signals a
    genuine extraction error, not a known-difference."""
    consolidated = {
        "year": 2020,
        "period": "H1",
        "total_notifications": 400,
        "malicious_attacks": 100,
        "human_error": 50,
        "system_faults": 5,  # source_sum=155 vs total 400 = 61% off
        "monthly_notifications": [],
    }
    with pytest.raises(OAICValidationError):
        validate_cross_page_totals(consolidated, "Jan-Jun 2020")


# ----------------------------------------------------------------------
# Existing tests follow
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# Correction-aware retry prompt
# ----------------------------------------------------------------------

def test_correction_prompt_includes_prev_payload_and_errors():
    """The retry prompt must show the LLM exactly what it produced last
    time and what the validator complained about, so the LLM can target
    the specific cell/bar that was misread instead of re-running the
    same extraction blindly.
    """
    from scripts.oaic.OAIC_dashboard_scraper import DashboardVisionExtractor

    original = "Original prompt..."
    previous = {
        "displayed_semester": "Jan-Jun 2024",
        "time_to_identify_pct": [
            {"bucket": "Unknown", "current_pct": 1, "previous_pct": 1},
            {"bucket": "<= 10 days", "current_pct": 58, "previous_pct": 84},
        ],
    }
    errors = [
        "Time-bucket previous_pct sums to 120.0 (expected 100±8); "
        "likely partial extraction.",
    ]
    out = DashboardVisionExtractor._build_correction_prompt(
        original, previous, errors,
    )
    # Original prompt included
    assert "Original prompt..." in out
    # Previous payload echoed back
    assert "Jan-Jun 2024" in out
    assert "84" in out
    # Validator error echoed back
    assert "120.0" in out
    # Common-failure-mode hints present
    assert "LEFT bar" in out
    assert "tiny bar" in out.lower() or "0%" in out


def test_quarantine_writes_screenshot_and_payload(tmp_path: Path):
    out = quarantine_extraction(
        tmp_path, "Jan-Jun 2025", page=9,
        image_bytes=b"\x89PNG\r\n\x1a\n_pretend_screenshot_",
        payload={"sector_by_source": "rank-view"},
        errors=["rank values detected"],
    )
    assert out.exists()
    assert (out / "screenshot.png").exists()
    assert (out / "payload.json").exists()
    assert (out / "errors.txt").exists()
    assert "rank values detected" in (out / "errors.txt").read_text()
