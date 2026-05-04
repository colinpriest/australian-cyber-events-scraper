"""Regression tests for the OAIC data merge logic in
scripts.build_static_dashboard.load_oaic_data.

Covers the bug where a newer JSON file with all-null `notifications`
entries was treated as "valid" and prevented the merge from falling
through to older files that had real counts. Result: the dashboard's
"OAIC: Top Affected Sectors" chart silently rendered "No data available".
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest


@pytest.fixture
def make_oaic_files(tmp_path, monkeypatch):
    """Create an isolated cwd with N OAIC JSON files (newest mtime
    first), then return load_oaic_data() output for that directory.
    """

    def _make(records_per_file: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        # Materialise files in tmp_path, oldest first so mtime ordering
        # matches list order: index 0 = OLDEST, last = NEWEST.
        # load_oaic_data sorts newest-first internally.
        for i, records in enumerate(records_per_file):
            path = tmp_path / f"oaic_cyber_statistics_2026{i:04d}_000000.json"
            path.write_text(json.dumps(records), encoding="utf-8")
            # Force mtime ordering so index 0 is oldest.
            import os
            os.utime(path, (1700000000 + i * 60, 1700000000 + i * 60))

        monkeypatch.chdir(tmp_path)
        # Re-import to pick up the new cwd in glob.
        sys.modules.pop("scripts.build_static_dashboard", None)
        mod = importlib.import_module("scripts.build_static_dashboard")
        return mod.load_oaic_data()

    return _make


def _record(year: int, period: str, top_sectors: List[Dict]) -> Dict:
    return {
        "year": year,
        "period": period,
        "total_notifications": 500,
        "top_sectors": top_sectors,
    }


def test_load_oaic_data_falls_through_when_newest_has_all_null_notifications(make_oaic_files):
    """Regression: the newest file's top_sectors entries all have
    notifications=null. Merge logic must fall through to the older file
    that has real counts."""
    older = [
        _record(2024, "H1", [
            {"sector": "Health service providers", "notifications": 96},
            {"sector": "Finance (incl. superannuation)", "notifications": 73},
        ]),
    ]
    newer = [
        _record(2024, "H1", [
            {"sector": "Health service providers", "notifications": None},
            {"sector": "Finance (incl. superannuation)", "notifications": None},
        ]),
    ]
    # Order: oldest first - last entry is newest mtime
    merged = make_oaic_files([older, newer])
    rec = next(r for r in merged if r.get("year") == 2024 and r.get("period") == "H1")
    counts = [s.get("notifications") for s in rec["top_sectors"]]
    assert 96 in counts, f"Expected to recover 96 from older file, got {counts}"
    assert 73 in counts


def test_load_oaic_data_keeps_newest_when_newest_has_valid_counts(make_oaic_files):
    """Sanity check: when the newest file DOES have real counts, those
    win - we don't accidentally fall back to older data."""
    older = [
        _record(2024, "H1", [{"sector": "Health service providers", "notifications": 50}]),
    ]
    newer = [
        _record(2024, "H1", [{"sector": "Health service providers", "notifications": 96}]),
    ]
    merged = make_oaic_files([older, newer])
    rec = next(r for r in merged if r.get("year") == 2024 and r.get("period") == "H1")
    counts = [s.get("notifications") for s in rec["top_sectors"]]
    assert 96 in counts and 50 not in counts


def test_load_oaic_data_handles_missing_top_sectors(make_oaic_files):
    """If the newest record has no top_sectors at all, the older file's
    top_sectors are used."""
    older = [
        _record(2024, "H1", [{"sector": "Health service providers", "notifications": 96}]),
    ]
    newer = [{"year": 2024, "period": "H1", "total_notifications": 500}]
    merged = make_oaic_files([older, newer])
    rec = next(r for r in merged if r.get("year") == 2024 and r.get("period") == "H1")
    assert rec.get("top_sectors") and rec["top_sectors"][0]["notifications"] == 96
