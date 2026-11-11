"""Tests for the two supplier figures of the paper-figure export (``scripts/export_paper_figures.py``)."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import matplotlib.pyplot as plt
import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cyber_data_collector.reporting.population import ReportingPeriod  # noqa: E402
from scripts import build_supplier_comparison as sc  # noqa: E402
from scripts import export_paper_figures as epf  # noqa: E402

PERIOD = ReportingPeriod.from_iso("2020-01-01", "2026-11-06")
PUB = {"snapshot_id": "snap-test", "published_at": "2026-11-11T22:30:20"}


def _event(i: int, supplier: bool, attack: str, records: Any) -> Dict[str, Any]:
    return {
        "event_id": f"e{i:03d}", "event_date": "2024-03-01", "records": records, "victims": 1,
        "bases": ["vendor"] if supplier else [], "incidents": [],
        "values": {
            "size": ("LARGE", None), "industry": ("Retail", None), "severity": ("High", None),
            "attack_type": (attack, None) if attack else (None, "Not recorded"),
            "asd_severity": (None, "Not classified"), "asd_impact": (None, "Not classified"),
            "ai": ("No-AI", None), "half_year": ("2024 H1", None),
        },
    }


@pytest.fixture
def comparison() -> sc.Comparison:
    events: List[Dict[str, Any]] = []
    attacks = ["Data Breach", "Data Breach", "Ransomware", "Phishing", None]
    counts = [5.0, 50.0, 700.0, 80_000.0, None, 2_000_000.0]
    for i in range(60):
        supplier = i % 3 == 0
        events.append(_event(i, supplier, attacks[(i + (0 if supplier else 2)) % len(attacks)],
                             counts[(i * (2 if supplier else 1)) % len(counts)]))
    return sc.analyse(events, PERIOD)


def _published_page(c: sc.Comparison, snapshot_id: str = "snap-test") -> str:
    page = sc.build_html(c, "test")
    return page.replace("<head>", f'<head><meta name="data-snapshot" content="{snapshot_id}">', 1)


def test_two_supplier_figures_follow_the_index_figures(comparison):
    figures = epf.supplier_figures(comparison)
    assert [f.page_id for f in figures] == epf.SUPPLIER_FIGURES == ["supplierRecordsHistogram",
                                                                   "supplierAttackTypeChart"]
    for f in figures:
        assert f.source_page == "supplier_comparison.html" and f.period == "2020-01-01 to 2026-11-06"
        assert "supplier" in f.description.lower() and f.description.endswith(".")
        assert {row[1] for row in f.dictionary} >= {"series", "raw data"}
        assert len(f.columns) == len(f.rows[0])


def test_records_histogram_data_is_the_page_histogram(comparison):
    records, _ = epf.supplier_figures(comparison)
    sup, oth = comparison.records
    decades, bands, a, b = sc.histogram_parts(sup, oth)
    assert [row[0] for row in records.rows] == bands
    assert [row[3] for row in records.rows] == a and [row[5] for row in records.rows] == b
    assert [row[1] for row in records.rows] == [10 ** k for k in range(decades)]
    assert records.rows[0][4] == pytest.approx(100.0 * a[0] / sup.known)
    assert records.totals["supplier_events"] == sup.known and records.totals["non_supplier_events"] == oth.known
    assert sum(row[4] for row in records.rows) == pytest.approx(100.0)
    # The raw data: one row per event with a count, and nothing else.
    columns, rows = records.extra_sheets["events"]
    assert columns == ["deduplicated_event_id", "event_date", "group", "records_affected"]
    assert len(rows) == sup.known + oth.known and all(row[3] is not None for row in rows)
    assert {row[2] for row in rows} == {sc.SUPPLIER, sc.OTHER}
    notes = dict(records.notes)
    assert notes["Supplier events with no figure (left out)"] == sup.missing
    assert notes["Test"] == "Mann-Whitney U test (two-sided)" and "Conclusion" in notes


def test_attack_type_data_is_the_page_chart(comparison):
    _, attack = epf.supplier_figures(comparison)
    tab = next(t for t in comparison.tabulations if t.attribute.key == "attack_type")
    assert [row[0] for row in attack.rows] == tab.labels
    assert [row[1] for row in attack.rows] == tab.supplier and [row[3] for row in attack.rows] == tab.other
    assert attack.totals["supplier_events"] == sum(tab.supplier)
    assert sum(row[4] for row in attack.rows) == pytest.approx(100.0)
    columns, rows = attack.extra_sheets["events"]
    assert len(rows) == len(comparison.events)                       # every event, the unrecorded ones marked
    assert sum(1 for row in rows if row[3] == "(Not recorded)") == sum(n_s + n_o for _, n_s, n_o in tab.left_out)
    notes = dict(attack.notes)
    assert notes["Not recorded: supplier / non-supplier events left out"] == "{} / {}".format(*tab.left_out[0][1:])
    assert notes["Conclusion"] == sc.verdict(next(r for r in comparison.results if r.key == "attack_type"))


def test_the_figures_draw_and_their_workbooks_reconcile(comparison, tmp_path):
    for nn, f in zip(("31", "32"), epf.supplier_figures(comparison)):
        fig = f.draw()
        assert isinstance(fig, plt.Figure) and len(fig.axes) == 1
        epf.save_figure(fig, tmp_path / f"figure{nn}.png", tmp_path / f"figure{nn}.pdf")
        assert (tmp_path / f"figure{nn}.png").stat().st_size > 10_000
        assert (tmp_path / f"figure{nn}.pdf").read_bytes().startswith(b"%PDF")
        xlsx = tmp_path / f"figure{nn}-data.xlsx"
        epf.write_xlsx(xlsx, f, nn, [("Source", f.source_page)], datetime(2026, 11, 6))
        checked = {name: got for name, _, got in epf.check_totals(xlsx, f)}
        assert checked["supplier_events"] == f.totals["supplier_events"]
        wb = openpyxl.load_workbook(xlsx, read_only=True)
        assert wb.sheetnames == ["data", "events", "notes"]
        assert wb["events"].max_row == len(f.extra_sheets["events"][1]) + 1
        wb.close()
        text = epf.dictionary_text(f, nn)
        assert text.startswith(f"Figure {nn}: {f.title}") and "Supplier events" in text


def test_the_published_supplier_page_must_hold_the_recomputed_charts(comparison, tmp_path):
    page = tmp_path / "supplier_comparison.html"
    page.write_text(_published_page(comparison), encoding="utf-8")
    epf.verify_supplier_page(comparison, page, PUB)                  # the same data: passes

    with pytest.raises(epf.ExportError, match="is from snapshot snap-other"):
        page.write_text(_published_page(comparison, "snap-other"), encoding="utf-8")
        epf.verify_supplier_page(comparison, page, PUB)

    # A page built from other data (one supplier event's count changed) is refused.
    changed = [dict(e) for e in comparison.events]
    first = next(e for e in changed if e["bases"] and e["records"] == 5.0)
    first["records"] = 5_000_000.0
    page.write_text(_published_page(sc.analyse(changed, PERIOD)), encoding="utf-8")
    with pytest.raises(epf.ExportError, match="records-affected histogram recomputed from the snapshot differs"):
        epf.verify_supplier_page(comparison, page, PUB)

    with pytest.raises(epf.ExportError, match="publish the dashboards first"):
        epf.verify_supplier_page(comparison, tmp_path / "missing.html", PUB)
