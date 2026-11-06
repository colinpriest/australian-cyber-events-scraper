"""The size boxplot on the recurrence page: rates, resampling and what it says it left out."""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts import recurrence_size_plot as rsp


def _spells() -> pd.DataFrame:
    rows = []
    # Ten small organisations, one with a repeat; ten huge, five with one; two of unknown size.
    for i in range(10):
        rows.append({"entity_id": i, "org_size": "SMALL", "event": int(i == 0), "duration_days": 365.25})
    for i in range(10, 20):
        rows.append({"entity_id": i, "org_size": "HUGE", "event": int(i < 15), "duration_days": 365.25})
    rows.append({"entity_id": 30, "org_size": None, "event": 0, "duration_days": 100})
    rows.append({"entity_id": 31, "org_size": "UNKNOWN", "event": 0, "duration_days": 100})
    return pd.DataFrame(rows)


def test_an_organisations_spells_are_added_up_and_no_follow_up_is_no_rate():
    spells = pd.DataFrame([
        {"entity_id": 1, "org_size": "LARGE", "event": 1, "duration_days": 365.25},
        {"entity_id": 1, "org_size": "LARGE", "event": 0, "duration_days": 365.25},
        {"entity_id": 2, "org_size": "LARGE", "event": 0, "duration_days": 0},
    ])
    orgs = rsp.organisation_rates(spells)
    assert list(orgs["entity_id"]) == [1]
    assert orgs.iloc[0]["rate"] == 50.0


def test_a_band_rate_is_its_repeats_over_its_follow_up_not_a_mean_of_organisations():
    orgs = rsp.organisation_rates(pd.DataFrame([
        {"entity_id": 1, "org_size": "HUGE", "event": 1, "duration_days": 36.525},
        {"entity_id": 2, "org_size": "HUGE", "event": 0, "duration_days": 3652.5},
    ]))
    row = rsp.band_table(orgs, rsp.bootstrap_rates(orgs))[0]
    assert abs(row["rate"] - 1 / 10.1 * 100) < 1e-9


def test_the_resampling_is_repeatable_and_brackets_the_rate():
    orgs = rsp.organisation_rates(_spells())
    first, second = rsp.bootstrap_rates(orgs), rsp.bootstrap_rates(orgs)
    assert np.array_equal(first["HUGE"], second["HUGE"])
    row = {r["band"]: r for r in rsp.band_table(orgs, first)}["HUGE"]
    assert row["low"] < row["rate"] == 50.0 < row["high"]


def test_the_section_shows_the_figure_and_counts_what_it_leaves_out():
    section = rsp.section_html(_spells())
    assert "data:image/png;base64," in section
    assert "Left out: 2 organisations of unknown size, with 0 repeats." in section
    # MEDIUM and LARGE have no organisations here, so they are not drawn as empty bands.
    assert "<td>SMALL</td>" in section and "<td>HUGE</td>" in section and "<td>LARGE</td>" not in section
    assert "In SMALL at least three quarters of organisations have no repeat" in section
