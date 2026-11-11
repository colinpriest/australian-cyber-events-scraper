"""Tests for the recurrence size-band figure of the paper-figure export (``scripts/export_paper_figures.py``)."""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import openpyxl
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from datetime import datetime  # noqa: E402

from scripts import export_paper_figures as epf  # noqa: E402
from scripts import recurrence_size_plot as rsp  # noqa: E402

PUB = {"snapshot_id": "snap-test", "published_at": "2026-11-11T22:30:20"}
SUMMARY = {"executive_summary": {
    "questions": [{"key": "size", "answer": "Yes: the recurrence rate rises with organisation size."}],
    "size_model": {"bootstrap_p": 0.002, "rate_ratio_vs_small": {"SMALL": 1.0, "MEDIUM": 1.4, "LARGE": 3.6,
                                                                  "HUGE": 9.0}}}}
INPUTS = {"snapshot_id": "snap-test", "censor_date": "2026-11-03", "recorded_at": "2026-11-06T16:09:11"}


def _spells() -> pd.DataFrame:
    rows = []
    for i, (size, repeats, days) in enumerate([("SMALL", 0, 900), ("SMALL", 1, 400), ("MEDIUM", 0, 700),
                                               ("MEDIUM", 1, 800), ("LARGE", 2, 600), ("HUGE", 3, 500),
                                               ("HUGE", 1, 300), (None, 0, 365)]):
        rows.append({"entity_id": i + 1, "org_size": size, "event": repeats, "duration_days": days})
    return pd.DataFrame(rows)


def test_the_figure_holds_the_page_rates_and_its_files_add_up(tmp_path):
    fig = epf.recurrence_size_figure(_spells(), SUMMARY, INPUTS)
    assert fig.page_id == "recurrenceSizeBandRates" and fig.source_page == "recurrence.html"
    _, _, rows = rsp.band_table, None, rsp.band_table(rsp.organisation_rates(_spells()),
                                                      rsp.bootstrap_rates(rsp.organisation_rates(_spells())))
    assert [r[0] for r in fig.rows] == ["SMALL", "MEDIUM", "LARGE", "HUGE"]
    assert [r[5] for r in fig.rows] == [round(r["rate"], 4) for r in rows]
    assert fig.totals == {"organisations": 7, "repeats": 8}
    assert dict(fig.notes)["Organisations of unknown size (not plotted)"] == 1
    assert sorted(r[0] for r in fig.extra_sheets["organisations"][1]) == list(range(1, 9))
    drawn = fig.draw()
    assert isinstance(drawn, plt.Figure)
    plt.close(drawn)
    path = tmp_path / "figure33-data.xlsx"
    epf.write_xlsx(path, fig, "33", [("Snapshot id", "snap-test")], datetime(2026, 11, 6))
    assert [c for c, _, _ in epf.check_totals(path, fig)] == ["organisations", "repeats"]
    assert openpyxl.load_workbook(path).sheetnames == ["data", "organisations", "notes"]
    text = epf.dictionary_text(fig, "33")
    assert "Repeats per 100 organisation-years" in text and "Whiskers" in text


def test_the_page_must_show_the_same_rates_from_the_same_snapshot(tmp_path):
    _, _, rows = epf.recurrence_size_parts(_spells())
    page = tmp_path / "recurrence.html"
    page.write_text('<meta name="data-snapshot" content="snap-test">'
                    f'<img alt="{html.escape(rsp.alt_text(rows))}">', encoding="utf-8")
    epf.verify_recurrence_page(rows, page, PUB)
    page.write_text('<meta name="data-snapshot" content="snap-test"><img alt="Recurrence rate by organisation '
                    'size: SMALL 9.9 per 100 organisation-years">', encoding="utf-8")
    with pytest.raises(epf.ExportError, match="are not the ones"):
        epf.verify_recurrence_page(rows, page, PUB)
    page.write_text('<meta name="data-snapshot" content="snap-other">', encoding="utf-8")
    with pytest.raises(epf.ExportError, match="snap-other"):
        epf.verify_recurrence_page(rows, page, PUB)


def test_the_run_must_be_on_the_published_snapshot(tmp_path):
    _spells().to_csv(tmp_path / "recurrent_event_spells.csv", index=False)
    (tmp_path / "analysis_summary.json").write_text(json.dumps(SUMMARY), encoding="utf-8")
    (tmp_path / "analysis_inputs.json").write_text(json.dumps(dict(INPUTS, snapshot_id="snap-old")), encoding="utf-8")
    with pytest.raises(epf.ExportError, match="snap-old"):
        epf.read_recurrence_run(tmp_path, PUB)
    (tmp_path / "analysis_inputs.json").write_text(json.dumps(INPUTS), encoding="utf-8")
    spells, summary, inputs = epf.read_recurrence_run(tmp_path, PUB)
    assert len(spells) == 8 and inputs["snapshot_id"] == "snap-test"
