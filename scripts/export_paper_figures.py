#!/usr/bin/env python3
"""Export every unique chart of the published dashboard as print figures for the research paper.

For each unique visualisation in ``dashboard/index.html``, in page order, writes to
``dashboard/Figures``:

* ``figureNN.png``                 - the chart at 300 dpi;
* ``figureNN.pdf``                 - the same chart as a vector PDF;
* ``figureNN-description.txt``     - one sentence on what the chart measures;
* ``figureNN-data-dictionary.txt`` - each data input, axis and group label with what it refers to;
* ``figureNN-data.xlsx``           - the plotted data (sheet ``data``) and the notes the chart carries
                                     (sheet ``notes``: exclusions, counts left out, provenance).

The charts are redrawn with matplotlib from the data ``build_static_dashboard.py`` prepares
(``assemble_dashboard_data``), computed on the retained snapshot the dashboard was published from
(``dashboard/publication.json``) over the publication's reporting period. Before anything is written:

* the OAIC dataset must be the one the publication hashed (it lives outside the database);
* the prepared data must equal, value for value, the data embedded in the published ``index.html``;
* after writing, every chart's totals are read back from its xlsx and compared with the data.

After the charts of ``index.html`` come two figures from ``dashboard/supplier_comparison.html``
(``scripts/build_supplier_comparison.py``), computed on the same snapshot: the histogram of records
affected and the distribution of attack types, each for supplier events against non-supplier events.
Their xlsx also holds the events behind the chart (sheet ``events``), and the recomputed chart must be
found, tooltip for tooltip, in the published page.

Last comes one figure from ``dashboard/recurrence.html`` (``scripts/recurrence_size_plot.py``): the
recurrence rate of each organisation size band, with the box and whiskers of that rate when the band's
organisations are resampled. It is drawn from the main recurrence run's ``recurrent_event_spells.csv``
under ``analysis/recurrent_timing``; the run must be computed on the published snapshot, and the band
rates must be the ones the published page shows. Its xlsx also lists the organisations (sheet
``organisations``).

Any mismatch stops the export (exit 1) instead of writing figures that differ from the page.
The live database is never opened: the snapshot is decompressed into a temporary folder.

Usage (from the project root)::

    python scripts/export_paper_figures.py
    python scripts/export_paper_figures.py --list          # the figure list only
"""

from __future__ import annotations

import argparse
import colorsys
import gc
import hashlib
import json
import logging
import math
import os
import re
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import to_rgba  # noqa: E402
from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter  # noqa: E402
import numpy as np  # noqa: E402
import openpyxl  # noqa: E402
from openpyxl.styles import Font  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cyber_data_collector.publication import snapshot as snapshots  # noqa: E402
from cyber_data_collector.reporting.population import dashboard_period  # noqa: E402
from scripts import build_supplier_comparison as supplier  # noqa: E402
from scripts import recurrence_size_plot as size_plot  # noqa: E402
from scripts.build_static_dashboard import assemble_dashboard_data  # noqa: E402

logger = logging.getLogger("export_paper_figures")

DPI = 300
FIG_W = 7.0          # inches: the text width of a one-column paper page
FIG_H = 4.3

# Dashboard colours (scripts/build_static_dashboard.py, the `colors` object)
PRIMARY = "#2563eb"
SECONDARY = "#10b981"
SUCCESS = "#10b981"
WARNING = "#f59e0b"
DANGER = "#ef4444"
INFO = "#06b6d4"
SEVERITY_COLOURS = {"Critical": "#dc2626", "High": "#ea580c", "Medium": "#ca8a04", "Low": "#16a34a",
                    "Unknown": "#6b7280"}
SEVERITY_TICKS = {1: "Low", 2: "Medium", 3: "High", 4: "Critical"}
SUBTITLE_GREY = "#6b7280"

# Black-and-white print: each bar group gets a hatch, each line a dash pattern and a marker.
HATCHES = ["", "///", "\\\\\\", "xxx", "...", "ooo", "+++", "---", "|||", "***", "OO", "//..", "\\\\--", "xx.."]
LINESTYLES = ["-", "--", "-.", ":", (0, (5, 1.5, 1, 1.5, 1, 1.5)), (0, (8, 2)), (0, (1, 1))]
MARKERS = ["o", "s", "^", "D", "v", "P", "X", "*", "<", ">"]

# The const names build_html embeds, and the data key each holds.
EMBEDDED = {
    "monthlyCounts": "monthly_counts",
    "monthlyBF": "monthly_bf",
    "severityTrends": "severity_trends",
    "recordsAffected": "records_affected",
    "eventTypeMix": "event_type_mix",
    "overallEventTypeMix": "overall_event_type_mix",
    "entityTypes": "entity_types",
    "recordsHistogram": "records_histogram",
    "maxRecordsPerMonth": "max_records_per_month",
    "recordsByAttackType": "records_by_attack_type",
    "aiInvolvement": "ai_involvement",
    "vendorAttacks": "vendor_attacks",
    "monthlyCountsStats": "monthly_counts_stats",
    "eventTypeCorrelation": "event_type_correlation",
    "oaicComparison": "oaic_comparison",
    "oaicCyberIncidents": "oaic_cyber_incidents",
    "oaicAttackTypes": "oaic_attack_types",
    "oaicSectors": "oaic_sectors",
    "oaicIndividualsAffected": "oaic_individuals_affected",
    "oaicMonthlyComparison": "oaic_monthly_comparison",
    "oaicIndAffDist": "oaic_individuals_affected_distribution",
    "oaicSourceSplit": "oaic_source_split",
    "oaicTimeToIdentify": "oaic_time_to_identify",
    "oaicTimeToNotify": "oaic_time_to_notify",
    "oaicPersonalInfoTypes": "oaic_personal_info_types",
    "asdRiskAll": "asd_risk_all",
    "asdRiskCurrent": "asd_risk_current",
    "asdRiskPrevious": "asd_risk_previous",
    "maxSeverityData": "max_severity_per_month",
    "severityByIndustry": "severity_by_industry",
    "severityByAttackType": "severity_by_attack_type",
}

# The page's 31 visualisations in order (28 canvases, the three ASD risk-matrix tables placed where they
# appear). "oaicSectorRatioChart" plots the same sector counts as "oaicSectorsChart" (the ratio of its two
# series), so it is exported once, with the ratio in that figure's data.
PAGE_VISUALISATIONS = [
    "monthlyTrendsChart", "severityTrendsChart", "recordsAffectedChart", "eventTypeMixChart",
    "entityTypeChart", "overallEventTypePieChart", "recordsHistogramChart", "maxSeverityChart",
    "maxRecordsChart", "severityByIndustryChart", "severityByAttackTypeChart", "recordsByAttackTypeChart",
    "aiInvolvementChart", "vendorAttacksChart", "monthlyCountsHistChart", "oaicComparisonChart",
    "oaicCyberIncidentsChart", "oaicAttackTypesChart", "oaicSectorsChart", "oaicSectorRatioChart",
    "oaicIndividualsAffectedChart", "oaicMonthlyComparisonChart", "oaicIndAffDistChart",
    "oaicSourceSplitChart", "oaicTimeToIdentifyChart", "oaicTimeToNotifyChart", "oaicPersonalInfoChart",
    "asdRiskMatrixAll", "asdRiskMatrixPrevious", "asdRiskMatrixCurrent", "correlationMatrixChart",
]
DUPLICATES = {"oaicSectorRatioChart": "oaicSectorsChart"}

# The figures taken from supplier_comparison.html, after those of index.html.
SUPPLIER_PAGE = "supplier_comparison.html"
SUPPLIER_FIGURES = ["supplierRecordsHistogram", "supplierAttackTypeChart"]

# The figure taken from recurrence.html, after the supplier figures: the right panel of its size chart.
RECURRENCE_PAGE = "recurrence.html"
RECURRENCE_RUN = Path("analysis") / "recurrent_timing"
RECURRENCE_FIGURES = ["recurrenceSizeBandRates"]
# The supplier page's two series colours (its light theme).
SUPPLIER_COLOUR = "#eb6834"
NON_SUPPLIER_COLOUR = "#2a78d6"


class ExportError(RuntimeError):
    """The figures cannot be shown to match the published dashboard."""


# --------------------------------------------------------------------------------------------------------
# Figure specification
# --------------------------------------------------------------------------------------------------------
@dataclass
class Figure:
    """One exported figure: how to draw it and the files that describe it."""

    page_id: str
    title: str
    description: str
    draw: Callable[[], plt.Figure]
    dictionary: List[Tuple[str, str, str]]        # (label, role, description)
    columns: List[str]
    rows: List[List[Any]]
    notes: List[Tuple[str, Any]]
    totals: Dict[str, float] = field(default_factory=dict)   # data-sheet column -> total from the data
    source_page: str = "index.html"                           # the published page the chart is on
    period: Optional[str] = None                              # its reporting period, when not index.html's
    # Further sheets of the xlsx: sheet name -> (columns, rows). The supplier figures list their events.
    extra_sheets: Dict[str, Tuple[List[str], List[List[Any]]]] = field(default_factory=dict)


def hsl(h: float, s: float, light: float) -> str:
    """CSS hsl() as a hex colour (h in degrees, s and l in percent)."""
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360.0, light / 100.0, s / 100.0)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def golden_colours(n: int) -> List[str]:
    """The dashboard's generateUniqueColors: golden-angle hues at 70% saturation, 50% lightness."""
    return [hsl(i * 137.508, 70, 50) for i in range(n)]


def alpha(colour: str, a: float) -> Tuple[float, float, float, float]:
    return to_rgba(colour, a)


def num(v: Any) -> float:
    return float(v) if v is not None else float("nan")


def month_label(m: str) -> str:
    y, mo = m.split("-")
    return datetime(int(y), int(mo), 1).strftime("%b %Y")


def new_figure(title: str, subtitle: Optional[str] = None, size: Tuple[float, float] = (FIG_W, FIG_H)):
    fig, ax = plt.subplots(figsize=size)
    set_titles(fig, title, subtitle)
    return fig, ax


def set_titles(fig: plt.Figure, title: str, subtitle: Optional[str] = None) -> None:
    fig.suptitle(title, fontsize=11, fontweight="bold", color="#374151")
    if subtitle:
        fig.text(0.5, 0.915, subtitle, ha="center", va="top", fontsize=8, style="italic", color=SUBTITLE_GREY,
                 wrap=True)


def finish(fig: plt.Figure, top: float = 0.86, bottom: Optional[float] = None, footer: Optional[str] = None,
           rect: Optional[Tuple[float, float, float, float]] = None) -> plt.Figure:
    if footer:
        fig.text(0.5, 0.012, footer, ha="center", va="bottom", fontsize=7.5, color="#374151", wrap=True)
    lines = footer.count(chr(10)) + 1 if footer else 0
    rect = rect or (0, 0.02 + 0.035 * lines if footer else 0, 1, top)
    fig.tight_layout(rect=rect)
    if bottom is not None:
        fig.subplots_adjust(bottom=bottom)
    fill_rect(fig, rect)
    return fig


def fill_rect(fig: plt.Figure, rect: Tuple[float, float, float, float], pad: float = 0.01) -> None:
    """Stretch a single-axes figure so the axes with its legend and labels fill ``rect`` vertically.

    tight_layout leaves a band of empty space when the legend sits outside the axes.
    """
    if len(fig.axes) != 1:
        return
    ax = fig.axes[0]
    fig.canvas.draw()
    box = ax.get_tightbbox(fig.canvas.get_renderer()).transformed(fig.transFigure.inverted())
    top_gap = rect[3] - pad - box.y1
    bottom_gap = box.y0 - (rect[1] + pad)
    pos = ax.get_position()
    if top_gap > 0 or bottom_gap > 0:
        y0 = pos.y0 - max(bottom_gap, 0)
        ax.set_position([pos.x0, y0, pos.width, pos.y1 + max(top_gap, 0) - y0])


def month_axis(ax, months: Sequence[str], every: int = 6, rotate: int = 90) -> None:
    """Month labels on a categorical axis, thinned so they print legibly."""
    ticks = [i for i, m in enumerate(months) if i % every == 0]
    ax.set_xticks(ticks)
    ax.set_xticklabels([months[i] for i in ticks], rotation=rotate, fontsize=7)
    ax.set_xlim(-0.6, len(months) - 0.4)


def style_axes(ax) -> None:
    ax.grid(axis="y", color="#e5e7eb", linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(labelsize=8)


def legend(ax, loc: str = "upper center", ncol: int = 3, outside: str = "top", fontsize: float = 7.5,
           below: float = -0.18, **kw):
    if outside == "top":
        return ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=ncol, fontsize=fontsize,
                         frameon=False, **kw)
    if outside == "bottom":
        return ax.legend(loc="upper center", bbox_to_anchor=(0.5, below), ncol=ncol, fontsize=fontsize,
                         frameon=False, **kw)
    if outside == "right":
        return ax.legend(loc="center left", bbox_to_anchor=(1.0, 0.5), ncol=1, fontsize=fontsize,
                         frameon=False, **kw)
    return ax.legend(loc=loc, ncol=ncol, fontsize=fontsize, frameon=False, **kw)


def stacked_bars(ax, labels: Sequence[str], series: Sequence[Tuple[str, Sequence[float], Any]],
                 width: float = 0.8, offset: float = 0.0, hatch_start: int = 0) -> None:
    x = np.arange(len(labels)) + offset
    bottom = np.zeros(len(labels))
    for k, (name, values, colour) in enumerate(series):
        vals = np.array([0 if v is None else v for v in values], dtype=float)
        ax.bar(x, vals, width, bottom=bottom, label=name, color=colour, edgecolor="#1f2937", linewidth=0.3,
               hatch=HATCHES[(k + hatch_start) % len(HATCHES)])
        bottom += vals


def compact(value: float, _pos=None) -> str:
    if value <= 0:
        return "0"
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= div:
            v = value / div
            return f"{v:.0f}{suffix}" if v >= 10 or v == int(v) else f"{v:.1f}{suffix}"
    return f"{value:g}"


def thousands(value: float, _pos=None) -> str:
    return f"{value:,.0f}"


def wrap(text: str, width: int) -> str:
    return "\n".join(textwrap.wrap(text, width, break_long_words=False)) or text


# --------------------------------------------------------------------------------------------------------
# The figures, in page order
# --------------------------------------------------------------------------------------------------------
def build_figures(d: Dict[str, Any], published: Dict[str, Any]) -> List[Figure]:
    incomplete = (d.get("incomplete_month") or {}).get("note") or ""
    figs: List[Figure] = []

    # ---- monthlyTrendsChart -----------------------------------------------------------------------------
    mc = d["monthly_counts"]
    months, counts = mc["months"], mc["counts"]
    n = len(counts)
    xs = np.arange(n)
    slope, intercept = np.polyfit(xs, counts, 1) if n > 1 else (0.0, float(counts[0]) if n else 0.0)
    trend = [slope * i + intercept for i in range(n)]
    bf = d.get("monthly_bf") or {}
    bf_by = {m["month"]: m for m in (bf.get("months") or [])}

    def draw_monthly():
        sub = "; ".join([p for p in (incomplete, "dashed = estimated full count, 80% interval" if bf_by else "")
                         if p])
        fig, ax = new_figure("Database: Monthly Trends in Unique Event Count", sub)
        ax.fill_between(xs, counts, color=alpha(PRIMARY, 0.125), linewidth=0)
        ax.plot(xs, counts, color=PRIMARY, linewidth=1.6, marker="o", markersize=2.2, label="Unique Events")
        ax.plot(xs, trend, color="#dc2626", linewidth=1.4, linestyle=(0, (5, 5)), label="Trend")
        if bf_by:
            bx = [i for i, m in enumerate(months) if m in bf_by]
            est = [round(bf_by[months[i]]["estimate"]) for i in bx]
            lo = [round(bf_by[months[i]]["low"]) for i in bx]
            hi = [round(bf_by[months[i]]["high"]) for i in bx]
            ax.errorbar(bx, est, yerr=[np.array(est) - lo, np.array(hi) - est], color="#7c3aed", linewidth=1.4,
                        linestyle=(0, (6, 4)), marker="o", markersize=4, markerfacecolor="white",
                        markeredgewidth=1.4, capsize=2.5, elinewidth=1.1,
                        label="Estimated full count (Bornhuetter-Ferguson)")
        ax.set_ylim(bottom=0)
        ax.set_xlabel("Month", fontsize=9)
        month_axis(ax, months)
        style_axes(ax)
        legend(ax, ncol=3)
        return finish(fig, top=0.88)

    rows = []
    for i, m in enumerate(months):
        b = bf_by.get(m)
        rows.append([m, counts[i], round(trend[i], 4),
                     round(b["estimate"]) if b else None, round(b["low"]) if b else None,
                     round(b["high"]) if b else None, b["observed"] if b else None,
                     round(b["reported_fraction"], 4) if b else None])
    figs.append(Figure(
        "monthlyTrendsChart", "Database: Monthly Trends in Unique Event Count",
        "The number of unique (deduplicated) Australian cyber incidents in the database by month of the "
        "incident, with a least-squares linear trend and Bornhuetter-Ferguson estimates of the full count for "
        "recent months still under-reported when the data were collected.",
        draw_monthly,
        [("Month", "x axis", "Calendar month of the incident date (YYYY-MM), January 2020 to the last complete "
                             "month before data collection."),
         ("Unique Events", "series (y)", "Active deduplicated incidents dated in the month."),
         ("Trend", "series (y)", "Ordinary least-squares line fitted to the monthly counts against month index."),
         ("Estimated full count (Bornhuetter-Ferguson)", "series (y)",
          "Expected final count for a recent month after reporting lag, with its 80% bootstrap interval as "
          "error bars; shown only for months whose reporting was incomplete at data collection."),
         ("y axis (unlabelled)", "y axis", "Number of incidents per month.")],
        ["month", "unique_events", "trend", "bf_estimate", "bf_low_80", "bf_high_80", "bf_observed",
         "bf_reported_fraction"],
        rows,
        [("Excluded month", incomplete or "none"),
         ("Incidents plotted (sum of unique_events)", sum(counts)),
         ("Trend slope (incidents per month)", round(float(slope), 6)),
         ("Trend intercept (incidents, first month)", round(float(intercept), 6)),
         ("Bornhuetter-Ferguson method", bf.get("method")),
         ("Bornhuetter-Ferguson extraction date", bf.get("extraction_date")),
         ("Bornhuetter-Ferguson expected monthly rate", bf.get("rate")),
         ("Bornhuetter-Ferguson rate months", ", ".join(bf.get("rate_months") or [])),
         ("Bornhuetter-Ferguson reported-fraction threshold", bf.get("threshold")),
         ("Bornhuetter-Ferguson lag sample size", bf.get("lag_sample_size"))],
        {"unique_events": sum(counts)},
    ))

    # ---- severityTrendsChart ----------------------------------------------------------------------------
    st = d["severity_trends"]
    sev_names = list(st["data"].keys())

    def draw_severity():
        fig, ax = new_figure("Database: Monthly Trends in Severity", incomplete)
        stacked_bars(ax, st["months"], [(s, st["data"][s], SEVERITY_COLOURS.get(s, "#6b7280")) for s in sev_names])
        ax.set_xlabel("Month", fontsize=9)
        ax.set_ylabel("Events", fontsize=9)
        month_axis(ax, st["months"])
        style_axes(ax)
        legend(ax, ncol=5)
        return finish(fig, top=0.88)

    figs.append(Figure(
        "severityTrendsChart", "Database: Monthly Trends in Severity",
        "The number of unique incidents per month, stacked by the severity assigned to each incident "
        "(Critical, High, Medium, Low or Unknown).",
        draw_severity,
        [("Month", "x axis", "Calendar month of the incident date (YYYY-MM)."),
         ("Events", "y axis", "Number of incidents in the month (bars stacked)."),
         *[(s, "group (stack)", f"Incidents whose recorded severity is {s}"
            + (" (missing or unrecognised severity)." if s == "Unknown" else ".")) for s in sev_names]],
        ["month", *sev_names, "total"],
        [[m, *[st["data"][s][i] for s in sev_names], sum(st["data"][s][i] for s in sev_names)]
         for i, m in enumerate(st["months"])],
        [("Excluded month", incomplete or "none"),
         *[(f"Total {s}", sum(st["data"][s])) for s in sev_names],
         ("Total incidents", sum(sum(v) for v in st["data"].values()))],
        {**{s: sum(st["data"][s]) for s in sev_names}, "total": sum(sum(v) for v in st["data"].values())},
    ))

    # ---- recordsAffectedChart ---------------------------------------------------------------------------
    ra = d["records_affected"]

    def draw_records():
        fig, ax = new_figure("Database: Monthly Trends in Average Records Affected", incomplete)
        x = np.arange(len(ra["months"]))
        ax.plot(x, ra["averages"], color=SECONDARY, linewidth=1.6, marker="o", markersize=2.2,
                label="Average Records Affected")
        ax.plot(x, ra["medians"], color=PRIMARY, linewidth=1.4, linestyle=(0, (5, 5)), marker="s",
                markersize=2.2, label="Median Records Affected")
        ax.set_yscale("log")
        ax.yaxis.set_major_formatter(FuncFormatter(compact))
        ax.set_ylabel("Records (log)", fontsize=9)
        ax.set_xlabel("Month", fontsize=9)
        month_axis(ax, ra["months"])
        style_axes(ax)
        legend(ax, ncol=2)
        return finish(fig, top=0.88)

    figs.append(Figure(
        "recordsAffectedChart", "Database: Monthly Trends in Average Records Affected",
        "The mean and median number of records affected per incident, by month, over the incidents that "
        "state a records-affected figure.",
        draw_records,
        [("Month", "x axis", "Calendar month of the incident date (YYYY-MM); months with no incident stating "
                             "a records-affected figure are absent."),
         ("Records (log)", "y axis", "Records affected per incident, logarithmic scale."),
         ("Average Records Affected", "series", "Arithmetic mean of records affected over the month's "
                                                "incidents that state a figure."),
         ("Median Records Affected", "series", "Median of the same figures."),
         ("sample_size", "data input (not drawn)", "Number of incidents in the month stating a figure."),
         ("ci_low / ci_high", "data input (not drawn)", "Interval the builder stores beside the mean; the "
                                                         "dashboard does not draw it.")],
        ["month", "average_records_affected", "median_records_affected", "sample_size", "ci_low", "ci_high"],
        [[m, ra["averages"][i], ra["medians"][i], ra["sample_sizes"][i],
          (ra["confidence_intervals"][i] or [None, None])[0], (ra["confidence_intervals"][i] or [None, None])[1]]
         for i, m in enumerate(ra["months"])],
        [("Excluded month", incomplete or "none"),
         ("Months plotted", len(ra["months"])),
         ("Incidents with a records-affected figure (sum of sample_size)", sum(ra["sample_sizes"]))],
        {"sample_size": sum(ra["sample_sizes"]), "average_records_affected": sum(ra["averages"]),
         "median_records_affected": sum(ra["medians"])},
    ))

    # ---- eventTypeMixChart ------------------------------------------------------------------------------
    etm = d["event_type_mix"]
    et_keys = list(etm["types"].keys())
    et_colours = golden_colours(len(et_keys))

    def draw_mix():
        fig, ax = new_figure("Database: Monthly Trends in Event Type Mix", incomplete, size=(FIG_W, 4.8))
        stacked_bars(ax, etm["months"], [(t, etm["types"][t], et_colours[i]) for i, t in enumerate(et_keys)])
        month_axis(ax, etm["months"])
        style_axes(ax)
        legend(ax, ncol=4, fontsize=7)
        return finish(fig, top=0.84)

    figs.append(Figure(
        "eventTypeMixChart", "Database: Monthly Trends in Event Type Mix",
        "The number of unique incidents per month, stacked by event type.",
        draw_mix,
        [("x axis (unlabelled)", "x axis", "Calendar month of the incident date (YYYY-MM)."),
         ("y axis (unlabelled)", "y axis", "Number of incidents in the month (bars stacked)."),
         *[(t, "group (stack)", f"Incidents whose event type is '{t}'.") for t in et_keys]],
        ["month", *et_keys, "total"],
        [[m, *[etm["types"][t][i] for t in et_keys], sum(etm["types"][t][i] for t in et_keys)]
         for i, m in enumerate(etm["months"])],
        [("Excluded month", incomplete or "none"),
         *[(f"Total {t}", sum(etm["types"][t])) for t in et_keys],
         ("Total incidents", sum(sum(v) for v in etm["types"].values()))],
        {**{t: sum(etm["types"][t]) for t in et_keys}, "total": sum(sum(v) for v in etm["types"].values())},
    ))

    # ---- entityTypeChart --------------------------------------------------------------------------------
    ent = d["entity_types"]["types"]
    ent_labels = [t["type"] for t in ent]
    ent_values = [t["events"] for t in ent]
    ent_colours = [hsl(i * 360 / len(ent_labels), 70, 50) for i in range(len(ent_labels))]

    def draw_entity():
        return pie_figure("Database: Overall Mix of Entity Types", ent_labels, ent_values, ent_colours,
                          donut=True)

    figs.append(Figure(
        "entityTypeChart", "Database: Overall Mix of Entity Types",
        "The share of incidents by the industry group of the organisation recorded as the victim.",
        draw_entity,
        [("Segment label", "group", "Industry group of the victim organisation ('Unknown' where none is "
                                    "recorded, 'Others' for groups outside the named ones)."),
         ("Segment size (events)", "data input", "Number of incidents whose victim is in the group."),
         ("entities", "data input (not drawn)", "Number of entity links counted for the group.")],
        ["entity_type", "events", "entities", "percent_of_events"],
        [[t["type"], t["events"], t.get("entities"), round(100 * t["events"] / sum(ent_values), 3)] for t in ent],
        [("Total events", sum(ent_values))],
        {"events": sum(ent_values)},
    ))

    # ---- overallEventTypePieChart -----------------------------------------------------------------------
    oe = d["overall_event_type_mix"]

    def draw_pie():
        return pie_figure("Database: Overall Event Type Mix (Pie)", oe["labels"], oe["counts"],
                          golden_colours(len(oe["labels"])), donut=False)

    figs.append(Figure(
        "overallEventTypePieChart", "Database: Overall Event Type Mix (Pie)",
        "The share of all incidents in the reporting window by event type.",
        draw_pie,
        [("Segment label", "group", "Event type of the incident."),
         ("Segment size", "data input", "Number of incidents of that type, 2020-01-01 to the publication date "
                                        "(the incomplete latest month included).")],
        ["event_type", "incidents", "percent"],
        [[lab, c, round(100 * c / sum(oe["counts"]), 3)] for lab, c in zip(oe["labels"], oe["counts"])],
        [("Total incidents", sum(oe["counts"]))],
        {"incidents": sum(oe["counts"])},
    ))

    # ---- recordsHistogramChart --------------------------------------------------------------------------
    rh = d["records_histogram"]

    def draw_hist():
        fig, ax = new_figure("Database: Records Affected Histogram")
        ax.bar(rh["bins"], rh["frequencies"], color=alpha(INFO, 0.5), edgecolor=INFO, linewidth=1)
        for i, v in enumerate(rh["frequencies"]):
            ax.text(i, v, f"{v}", ha="center", va="bottom", fontsize=7)
        style_axes(ax)
        return finish(fig)

    figs.append(Figure(
        "recordsHistogramChart", "Database: Records Affected Histogram",
        "The number of incidents in each order-of-magnitude band of records affected, over incidents that "
        "state a records-affected figure.",
        draw_hist,
        [("x axis (bins)", "x axis", "Band of records affected per incident (1-10, 11-100, ... 10M+)."),
         ("Frequency", "series (y)", "Number of incidents whose records-affected figure falls in the band.")],
        ["records_affected_band", "incidents"],
        [[b, f] for b, f in zip(rh["bins"], rh["frequencies"])],
        [("Incidents with a records-affected figure", sum(rh["frequencies"]))],
        {"incidents": sum(rh["frequencies"])},
    ))

    # ---- maxSeverityChart -------------------------------------------------------------------------------
    ms = d["max_severity_per_month"]

    def draw_maxsev():
        fig, ax = new_figure("Database: Severity per Month", incomplete)
        x = np.arange(len(ms["months"]))
        ax.plot(x, ms["severity_numeric"], color="#dc2626", linewidth=1.8, marker="o", markersize=3,
                label="Maximum Severity")
        ax.plot(x, ms["avg_severity_numeric"], color="#f59e0b", linewidth=1.4, linestyle=(0, (5, 5)),
                marker="s", markersize=2.2, label="Average Severity")
        ax.set_ylim(0, 4.15)
        ax.set_yticks([0, 1, 2, 3, 4])
        ax.set_yticklabels(["", "Low", "Medium", "High", "Critical"])
        ax.set_ylabel("Severity Level", fontsize=9)
        ax.set_xlabel("Month", fontsize=9)
        month_axis(ax, ms["months"])
        style_axes(ax)
        legend(ax, ncol=2)
        return finish(fig, top=0.88)

    figs.append(Figure(
        "maxSeverityChart", "Database: Severity per Month",
        "The highest and the average severity of the incidents in each month, on a scale of Low (1) to "
        "Critical (4).",
        draw_maxsev,
        [("Month", "x axis", "Calendar month of the incident date (YYYY-MM)."),
         ("Severity Level", "y axis", "Severity on the scale Low = 1, Medium = 2, High = 3, Critical = 4."),
         ("Maximum Severity", "series", "Highest severity among the month's incidents."),
         ("Average Severity", "series", "Mean severity of the month's incidents with a known severity."),
         ("title / entity", "data input (tooltip)", "Title and victim of the incident that set the maximum.")],
        ["month", "maximum_severity", "maximum_severity_numeric", "average_severity_numeric",
         "maximum_incident_title", "maximum_incident_entity"],
        [[m, ms["severities"][i], ms["severity_numeric"][i], ms["avg_severity_numeric"][i], ms["titles"][i],
          ms["entities"][i]] for i, m in enumerate(ms["months"])],
        [("Excluded month", incomplete or "none"), ("Months plotted", len(ms["months"]))],
        {"maximum_severity_numeric": sum(ms["severity_numeric"]),
         "average_severity_numeric": sum(ms["avg_severity_numeric"])},
    ))

    # ---- maxRecordsChart --------------------------------------------------------------------------------
    mr = d["max_records_per_month"]

    def draw_maxrec():
        fig, ax = new_figure("Database: Maximum Records Affected Per Month", incomplete)
        x = np.arange(len(mr["months"]))
        ax.plot(x, mr["max_records"], color="#dc2626", linewidth=1.6, marker="o", markersize=3)
        ax.set_yscale("log")
        ax.yaxis.set_major_formatter(FuncFormatter(compact))
        ax.set_ylabel("Records Affected (log scale)", fontsize=9)
        ax.set_xlabel("Month", fontsize=9)
        month_axis(ax, mr["months"])
        style_axes(ax)
        return finish(fig, top=0.88)

    figs.append(Figure(
        "maxRecordsChart", "Database: Maximum Records Affected Per Month",
        "The largest number of records affected by any single incident in each month.",
        draw_maxrec,
        [("Month", "x axis", "Calendar month of the incident date (YYYY-MM); months with no incident "
                             "stating a records-affected figure are absent."),
         ("Records Affected (log scale)", "y axis", "Records affected, logarithmic scale."),
         ("Maximum Records Affected", "series", "Largest records-affected figure among the month's incidents."),
         ("title / entity", "data input (tooltip)", "Title and victim of that incident.")],
        ["month", "maximum_records_affected", "incident_title", "incident_entity"],
        [[m, mr["max_records"][i], mr["titles"][i], mr["entities"][i]] for i, m in enumerate(mr["months"])],
        [("Excluded month", incomplete or "none"), ("Months plotted", len(mr["months"]))],
        {"maximum_records_affected": sum(mr["max_records"])},
    ))

    # ---- severityByIndustryChart / severityByAttackTypeChart (radar) ------------------------------------
    for page_id, key, label_key, title, colour, group_word in (
            ("severityByIndustryChart", "severity_by_industry", "industries",
             "Database: Average Severity by Industry", "#2563eb", "industry group"),
            ("severityByAttackTypeChart", "severity_by_attack_type", "attack_types",
             "Database: Average Severity by Attack Type", "#059669", "attack type")):
        sd = d[key]
        note = (f"{sd.get('unknown_severity_total', 0):,} event(s) with unknown severity excluded from the "
                f"averages") if sd.get("unknown_severity_total") else None

        def draw_radar(sd=sd, label_key=label_key, title=title, colour=colour, note=note):
            return radar_figure(title, sd[label_key], sd["avg_severities"], colour, note)

        figs.append(Figure(
            page_id, title,
            f"The mean severity (Low = 1 to Critical = 4) of incidents in each {group_word}, over incidents "
            f"with a known severity.",
            draw_radar,
            [("Spoke label", "group", f"The incident's {group_word}."),
             ("Radial axis", "axis", "Mean severity, 0 to 4: Low = 1, Medium = 2, High = 3, Critical = 4."),
             ("Average Severity", "series", f"Mean severity of the {group_word}'s incidents with a known "
                                             f"severity."),
             ("rated_count", "data input (not drawn)", "Incidents with a known severity in the group."),
             ("unknown_severity_count", "data input (note)", "Incidents with unknown severity, excluded.")],
            [group_word.replace(" ", "_"), "average_severity", "rated_count", "unknown_severity_count"],
            [[g, sd["avg_severities"][i], sd["rated_counts"][i], sd["unknown_severity_counts"][i]]
             for i, g in enumerate(sd[label_key])],
            [("Note on the chart", note or "none"),
             ("Incidents with a known severity", sum(sd["rated_counts"])),
             ("Incidents with unknown severity (excluded)", sd.get("unknown_severity_total"))],
            {"rated_count": sum(sd["rated_counts"]), "unknown_severity_count": sum(sd["unknown_severity_counts"]),
             "average_severity": sum(sd["avg_severities"])},
        ))

    # ---- recordsByAttackTypeChart -----------------------------------------------------------------------
    rb = d["records_by_attack_type"]

    def draw_rbat():
        fig, ax = new_figure("Database: Average Records Affected by Attack Type", size=(FIG_W, 4.6))
        y = np.arange(len(rb["attack_types"]))
        ax.barh(y, rb["avg_records"], color="#7c3aed", edgecolor="#7c3aed")
        ax.set_yticks(y)
        ax.set_yticklabels(rb["attack_types"], fontsize=8)
        ax.invert_yaxis()
        ax.set_xscale("log")
        ax.xaxis.set_major_formatter(FuncFormatter(thousands))
        ax.xaxis.set_minor_formatter(NullFormatter())
        ax.tick_params(axis="x", labelrotation=30)
        ax.set_xlabel("Average Records Affected (log scale)", fontsize=9)
        ax.set_ylabel("Attack Type", fontsize=9)
        ax.grid(axis="x", color="#e5e7eb", linewidth=0.6)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.tick_params(labelsize=8)
        return finish(fig)

    figs.append(Figure(
        "recordsByAttackTypeChart", "Database: Average Records Affected by Attack Type",
        "The mean number of records affected per incident for each attack type, over incidents that state a "
        "records-affected figure.",
        draw_rbat,
        [("Attack Type", "y axis (group)", "Event type of the incident."),
         ("Average Records Affected (log scale)", "x axis", "Mean records affected per incident of that type, "
                                                            "logarithmic scale.")],
        ["attack_type", "average_records_affected"],
        [[t, v] for t, v in zip(rb["attack_types"], rb["avg_records"])],
        [("Attack types plotted", len(rb["attack_types"]))],
        {"average_records_affected": sum(rb["avg_records"])},
    ))

    # ---- aiInvolvementChart -----------------------------------------------------------------------------
    ai = d["ai_involvement"]
    ai_series = [("AI attacker", ai["attacker"], "#dc2626"),
                 ("AI vulnerability (victim's AI attacked)", ai["vulnerability"], "#2563eb"),
                 ("AI misuse (organisation's own use of AI)", ai.get("misuse") or [0] * len(ai["periods"]),
                  "#d97706"),
                 ("AI involvement unsettled", ai.get("uncertain") or [0] * len(ai["periods"]), "#9ca3af")]
    ai_total = sum(ai["attacker"]) + sum(ai["vulnerability"]) + sum(ai.get("misuse") or [])
    ai_parts = [f"{ai_total:,} incident(s) involved AI"]
    if sum(ai.get("uncertain") or []):
        ai_parts.append(f"{sum(ai['uncertain']):,} more unsettled (grey; not counted)")
    if ai.get("unjudged"):
        ai_parts.append(f"{ai['unjudged']:,} not judged yet")
    if ai.get("stale"):
        ai_parts.append(f"{ai['stale']:,} label(s) stale")
    if not ai.get("available"):
        raise ExportError("AI involvement is not classified in this snapshot")

    def draw_ai():
        fig, ax = new_figure("Database: Incidents Involving AI per Half-Year", "; ".join(ai_parts))
        stacked_bars(ax, ai["periods"], ai_series, width=0.7)
        ax.set_xticks(np.arange(len(ai["periods"])))
        ax.set_xticklabels(ai["periods"], rotation=45, ha="right", fontsize=8)
        ax.yaxis.get_major_locator().set_params(integer=True)
        ax.set_xlabel("Half-year", fontsize=9)
        ax.set_ylabel("Unique events", fontsize=9)
        style_axes(ax)
        legend(ax, ncol=2, outside="bottom", below=-0.33)
        return finish(fig, top=0.88)

    ai_cols = ["attacker", "vulnerability", "misuse", "uncertain"]
    figs.append(Figure(
        "aiInvolvementChart", "Database: Incidents Involving AI per Half-Year",
        "The number of unique incidents per half-year in which artificial intelligence was involved, by "
        "category: AI used by the attacker, the victim's AI attacked, or the organisation's own misuse of AI.",
        draw_ai,
        [("Half-year", "x axis", "Calendar half-year of the incident date (H1 = January-June, H2 = "
                                 "July-December)."),
         ("Unique events", "y axis", "Number of incidents (bars stacked)."),
         ("AI attacker", "group (stack)", "An AI system was the attacker or the attacker's instrument."),
         ("AI vulnerability (victim's AI attacked)", "group (stack)",
          "The victim's own AI system was attacked, manipulated or leaked data."),
         ("AI misuse (organisation's own use of AI)", "group (stack)",
          "No attack: the organisation's own use of AI was the breach."),
         ("AI involvement unsettled", "group (stack)",
          "AI involvement likely but no category is settled; drawn in grey and not counted in the total.")],
        ["half_year", *ai_cols],
        [[p, ai["attacker"][i], ai["vulnerability"][i], (ai.get("misuse") or [0] * 99)[i],
          (ai.get("uncertain") or [0] * 99)[i]] for i, p in enumerate(ai["periods"])],
        [("Subtitle", "; ".join(ai_parts)),
         ("Incidents involving AI (attacker + vulnerability + misuse)", ai_total),
         ("Unsettled (not counted)", sum(ai.get("uncertain") or [])),
         ("Not judged yet", ai.get("unjudged", 0)),
         ("Stale labels", ai.get("stale", 0)),
         ("Incident list on the dashboard", ai.get("report"))],
        {"attacker": sum(ai["attacker"]), "vulnerability": sum(ai["vulnerability"]),
         "misuse": sum(ai.get("misuse") or []), "uncertain": sum(ai.get("uncertain") or [])},
    ))

    # ---- vendorAttacksChart -----------------------------------------------------------------------------
    va = d["vendor_attacks"]
    va_parts = [f"{sum(va['counts']):,} vendor attack(s) in the window"]
    if va.get("undated"):
        va_parts.append(f"{va['undated']:,} undated (not plotted)")
    if va.get("outside"):
        va_parts.append(f"{va['outside']:,} dated outside the window")

    def draw_vendor():
        fig, ax = new_figure("Database: Vendor Attacks Affecting Several Australian Customers per Half-Year",
                             "; ".join(va_parts))
        x = np.arange(len(va["periods"]))
        ax.bar(x, va["counts"], 0.7, color="#7c3aed", label="Vendor attacks with two or more Australian customers")
        ax.set_xticks(x)
        ax.set_xticklabels(va["periods"], rotation=45, ha="right", fontsize=8)
        ax.yaxis.get_major_locator().set_params(integer=True)
        ax.set_xlabel("Half-year of the attack", fontsize=9)
        ax.set_ylabel("Vendor attacks", fontsize=9)
        style_axes(ax)
        legend(ax, ncol=1, outside="bottom", below=-0.33)
        return finish(fig, top=0.88)

    figs.append(Figure(
        "vendorAttacksChart", "Database: Vendor Attacks Affecting Several Australian Customers per Half-Year",
        "The number of attacks on a known vendor or service provider that affected two or more of its "
        "Australian customers, by half-year of the attack.",
        draw_vendor,
        [("Half-year of the attack", "x axis", "Calendar half-year of the attack's date (the supplier's own "
                                               "event, else its customers' earliest)."),
         ("Vendor attacks", "y axis", "Number of supplier incidents."),
         ("Vendor attacks with two or more Australian customers", "series",
          "Supplier incidents in the registry linked to at least two Australian customer organisations.")],
        ["half_year", "vendor_attacks"],
        [[p, c] for p, c in zip(va["periods"], va["counts"])],
        [("Subtitle", "; ".join(va_parts)), ("Undated (not plotted)", va.get("undated", 0)),
         ("Dated outside the window (not plotted)", va.get("outside", 0)),
         ("Attack list on the dashboard", va.get("report"))],
        {"vendor_attacks": sum(va["counts"])},
    ))

    # ---- monthlyCountsHistChart -------------------------------------------------------------------------
    mcs = d["monthly_counts_stats"]
    od = mcs.get("overdispersion") or {}
    mean, var = od.get("mean") or mcs.get("mean"), od.get("variance") or mcs.get("variance")
    disp = ""
    if mean and var:
        pct = (var - mean) / mean * 100
        disp = (f"Overdispersion: +{pct:.1f}% vs Poisson" if var > mean
                else f"Underdispersion: {pct:.1f}% vs Poisson" if var < mean
                else "Dispersion: 0% vs Poisson (perfect match)")
    phi = od.get("overdispersion_param")
    if phi:
        model = (f"Negative Binomial (n={od.get('nb_n')}, p={od.get('nb_p')})" if phi > 1.5
                 else f"Poisson (λ={od.get('poisson_lambda')})" if phi < 1.2 else "Mixed distribution")
        stats_line = (f"Overdispersion Analysis: φ = {phi} ({od.get('interpretation') or mcs.get('model')})\n"
                      f"Mean: {od.get('mean')} • Variance: {od.get('variance')} • {model}")
    else:
        stats_line = f"Mean: {mcs.get('mean'):.2f} • Variance: {mcs.get('variance'):.2f} • Model: {mcs.get('model')}"

    def draw_mch():
        fig, ax = new_figure("Database: Histogram of Monthly Unique Event Counts", disp)
        ax.bar(mcs["bins"], mcs["frequencies"], color=alpha("#64748b", 0.5), edgecolor="#64748b", linewidth=1)
        for i, v in enumerate(mcs["frequencies"]):
            ax.text(i, v, f"{v}", ha="center", va="bottom", fontsize=7)
        ax.tick_params(axis="x", labelrotation=30)
        style_axes(ax)
        return finish(fig, top=0.88, footer=stats_line)

    figs.append(Figure(
        "monthlyCountsHistChart", "Database: Histogram of Monthly Unique Event Counts",
        "The distribution of the number of unique incidents per month, with its dispersion compared with a "
        "Poisson distribution.",
        draw_mch,
        [("x axis (bins)", "x axis", "Band of monthly incident counts (equal-width bins from the smallest to the "
                                     "largest monthly count)."),
         ("Months", "series (y)", "Number of months whose incident count falls in the band (the incomplete "
                                  "latest month included)."),
         ("Overdispersion", "subtitle", "(variance - mean) / mean of the monthly counts, against a Poisson "
                                        "distribution whose variance equals its mean."),
         ("φ, Mean, Variance, Negative Binomial (n, p)", "note", "Dispersion ratio, moments and the fitted "
                                                                  "negative binomial parameters.")],
        ["monthly_count_band", "months"],
        [[b, f] for b, f in zip(mcs["bins"], mcs["frequencies"])],
        [("Subtitle", disp), ("Statistics line", stats_line.replace("\n", " | ")),
         ("Months counted", sum(mcs["frequencies"])), ("Mean", mcs.get("mean")), ("Variance", mcs.get("variance")),
         ("NB dispersion k (estimate)", mcs.get("k_estimate")), ("Model", mcs.get("model")),
         *[(f"overdispersion.{k}", v) for k, v in od.items()]],
        {"months": sum(mcs["frequencies"])},
    ))

    # ---- oaicComparisonChart ----------------------------------------------------------------------------
    oc = d["oaic_comparison"]
    est = oc.get("partial_estimate") or {}
    est_label = ("Estimated Full Period (Bornhuetter-Ferguson)" if est.get("method") == "bornhuetter_ferguson"
                 else "Estimated Full Period (Pro-rata)")
    ratios = [db / o for db, o in zip(oc["database_counts"], oc["oaic_counts"]) if db and o]
    oc_stats = [f"Database periods: {oc['database_available']}"]
    if oc.get("oaic_available"):
        oc_stats.append(f"OAIC periods: {oc['oaic_available']}")
        if ratios:
            oc_stats.append(f"Avg ratio (DB/OAIC): {sum(ratios) / len(ratios):.2f}x")
    if est.get("estimate") is not None:
        line = f"{est['period']} (partial to {est['extraction_date']}): {est['observed']} → Est. {est['estimate']}"
        if est.get("low") is not None and est.get("high") is not None:
            line += f" (80%: {est['low']}-{est['high']})"
        line += (" [Bornhuetter-Ferguson, adjusts for reporting lag]" if est.get("method") == "bornhuetter_ferguson"
                 else " [pro-rata fallback]")
        oc_stats.append(line)

    def draw_oc():
        fig, ax = new_figure("Comparison: Database vs OAIC Official Notifications (Half-Yearly)")
        x = np.arange(len(oc["periods"]))
        ax.plot(x, oc["database_counts"], color=PRIMARY, linewidth=1.6, marker="o", markersize=4,
                label="Database Events")
        if oc.get("oaic_available"):
            ax.plot(x, [num(v) for v in oc["oaic_counts"]], color=DANGER, linewidth=1.6, linestyle="--",
                    marker="s", markersize=4, label="OAIC Official Notifications")
        if est.get("estimate") is not None and oc["periods"][-1] == est.get("period"):
            last = len(oc["periods"]) - 1
            yerr = None
            if est.get("low") is not None and est.get("high") is not None:
                yerr = [[est["estimate"] - est["low"]], [est["high"] - est["estimate"]]]
            ax.errorbar([last], [est["estimate"]], yerr=yerr, color=SECONDARY, marker="^", markersize=8,
                        linestyle="none", capsize=4, elinewidth=1.6, label=est_label)
            if last >= 1 and oc["database_counts"][last - 1] is not None:
                ax.plot([last - 1, last], [oc["database_counts"][last - 1], est["estimate"]], color=SECONDARY,
                        linestyle=(0, (6, 4)), linewidth=1.5)
        ax.set_xticks(x)
        ax.set_xticklabels(oc["periods"], rotation=45, ha="right", fontsize=8)
        ax.set_ylim(bottom=0)
        ax.set_ylabel("Number of Events/Notifications", fontsize=9)
        ax.set_xlabel("Half-Year Period", fontsize=9)
        style_axes(ax)
        legend(ax, ncol=3, fontsize=7)
        return finish(fig, top=0.9, footer=wrap(" • ".join(oc_stats), 150))

    figs.append(Figure(
        "oaicComparisonChart", "Comparison: Database vs OAIC Official Notifications (Half-Yearly)",
        "The number of unique incidents in the database per half-year against the number of data breach "
        "notifications the OAIC received, with an estimate of the full count for the half-year in progress.",
        draw_oc,
        [("Half-Year Period", "x axis", "Calendar half-year (H1 = January-June, H2 = July-December)."),
         ("Number of Events/Notifications", "y axis", "Count per half-year."),
         ("Database Events", "series", "Active deduplicated incidents dated in the half-year."),
         ("OAIC Official Notifications", "series", "Total notifications under the Notifiable Data Breaches "
                                                   "scheme the OAIC reports for the half-year (all causes)."),
         (est_label, "series", "Estimated final database count for the half-year in progress at data "
                               "collection, with its 80% interval."),
         ("Avg ratio (DB/OAIC)", "note", "Mean of database count / OAIC count over half-years with both.")],
        ["half_year", "database_events", "oaic_notifications", "estimate_full_period", "estimate_low_80",
         "estimate_high_80"],
        [[p, oc["database_counts"][i], oc["oaic_counts"][i],
          est.get("estimate") if p == est.get("period") else None,
          est.get("low") if p == est.get("period") else None,
          est.get("high") if p == est.get("period") else None] for i, p in enumerate(oc["periods"])],
        [("Statistics line", " • ".join(oc_stats)),
         *[(f"partial_estimate.{k}", v if not isinstance(v, (list, dict)) else json.dumps(v))
           for k, v in est.items()]],
        {"database_events": sum(oc["database_counts"]),
         "oaic_notifications": sum(v for v in oc["oaic_counts"] if v is not None)},
    ))

    # ---- oaicCyberIncidentsChart ------------------------------------------------------------------------
    ci = d["oaic_cyber_incidents"]
    mal = ci.get("malicious_attacks") or ci.get("cyber_incidents")

    def draw_ci():
        fig, ax = new_figure("OAIC: Malicious Attacks vs Total Notifications")
        x = np.arange(len(ci["periods"]))
        ax.fill_between(x, [num(v) for v in mal], color=alpha(DANGER, 0.125), linewidth=0)
        ax.plot(x, [num(v) for v in mal], color=DANGER, linewidth=1.6, marker="o", markersize=3.5,
                label="Malicious or Criminal Attacks")
        only = [num(v) for v in (ci.get("cyber_incident_only") or [])]
        ax.plot(x, only, color=WARNING, linestyle=(0, (6, 4)), marker="D", markersize=6, linewidth=1.4,
                label="Cyber Incidents (OAIC sub-source, published from 2025 H2)")
        ax.fill_between(x, [num(v) for v in ci["total_notifications"]], color=alpha(PRIMARY, 0.125), linewidth=0)
        ax.plot(x, [num(v) for v in ci["total_notifications"]], color=PRIMARY, linewidth=1.6, linestyle="--",
                marker="s", markersize=3.5, label="Total Notifications")
        ax.set_xticks(x)
        ax.set_xticklabels(ci["periods"], rotation=45, ha="right", fontsize=8)
        ax.set_ylim(bottom=0)
        ax.set_ylabel("Count", fontsize=9)
        style_axes(ax)
        legend(ax, ncol=2, fontsize=7)
        return finish(fig, top=0.86)

    figs.append(Figure(
        "oaicCyberIncidentsChart", "OAIC: Malicious Attacks vs Total Notifications",
        "The number of data breach notifications the OAIC received per half-year, in total and those caused "
        "by malicious or criminal attacks, with the narrower cyber-incident sub-source where the OAIC "
        "publishes it.",
        draw_ci,
        [("x axis (unlabelled)", "x axis", "OAIC reporting half-year."),
         ("Count", "y axis", "Notifications per half-year."),
         ("Malicious or Criminal Attacks", "series", "Notifications whose source the OAIC classes as a malicious "
                                                     "or criminal attack."),
         ("Cyber Incidents (OAIC sub-source, published from 2025 H2)", "series",
          "OAIC's narrower 'cyber incident' sub-source of malicious attacks; published only on data.gov.au "
          "from 2025 H2, blank before."),
         ("Total Notifications", "series", "All notifications in the half-year.")],
        ["half_year", "malicious_or_criminal_attacks", "cyber_incident_only", "total_notifications"],
        [[p, mal[i], (ci.get("cyber_incident_only") or [None] * 99)[i], ci["total_notifications"][i]]
         for i, p in enumerate(ci["periods"])],
        [("Cyber incident sub-source", "published from 2025 H2 only; blank cells are not published"),
         ("OAIC status", (d.get("oaic_status") or {}).get("status_line")),
         *[(f"OAIC withheld: {n.get('period')}", f"{', '.join(n.get('fields') or [])} - {n.get('reason')}")
           for n in (d.get("oaic_status") or {}).get("notes") or []]],
        {"malicious_or_criminal_attacks": sum(v for v in mal if v is not None),
         "total_notifications": sum(v for v in ci["total_notifications"] if v is not None),
         "cyber_incident_only": sum(v for v in (ci.get("cyber_incident_only") or []) if v is not None)},
    ))

    # ---- oaicAttackTypesChart ---------------------------------------------------------------------------
    at = d["oaic_attack_types"]
    at_colours = {"ransomware": "#dc2626", "phishing": "#ea580c", "hacking": "#ca8a04", "malware": "#16a34a",
                  "brute_force": "#0891b2", "compromised_credentials": "#7c3aed"}
    at_note = ("OAIC publishes no split by attack type for " + ", ".join(at["not_published"])
               if at.get("not_published") else "")

    def at_label(k: str) -> str:
        return re.sub(r"\b\w", lambda m: m.group(0).upper(), k.replace("_", " "))

    def draw_at():
        fig, ax = new_figure("OAIC: Attack Types Over Time (% of cyber incidents)", at_note)
        x = np.arange(len(at["periods"]))
        for k, (name, vals) in enumerate(at["attack_types"].items()):
            ax.plot(x, [num(v) for v in vals], color=at_colours.get(name, PRIMARY), linewidth=1.5,
                    linestyle=LINESTYLES[k % len(LINESTYLES)], marker=MARKERS[k % len(MARKERS)], markersize=3.5,
                    label=at_label(name))
        ax.set_xticks(x)
        ax.set_xticklabels(at["periods"], rotation=45, ha="right", fontsize=8)
        ax.set_ylim(bottom=0)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v:g}%"))
        ax.set_ylabel("% of cyber incidents", fontsize=9)
        style_axes(ax)
        legend(ax, ncol=6, fontsize=7)
        return finish(fig, top=0.88)

    at_keys = list(at["attack_types"].keys())
    figs.append(Figure(
        "oaicAttackTypesChart", "OAIC: Attack Types Over Time (% of cyber incidents)",
        "The share of the cyber incidents notified to the OAIC each half-year that were ransomware, phishing, "
        "hacking, malware, brute-force attacks or compromised credentials.",
        draw_at,
        [("x axis (unlabelled)", "x axis", "OAIC reporting half-year."),
         ("% of cyber incidents", "y axis", "Percentage of the half-year's cyber-incident notifications."),
         *[(at_label(k), "series", f"Percentage of cyber incidents the OAIC attributes to {at_label(k).lower()}.")
           for k in at_keys]],
        ["half_year", *at_keys],
        [[p, *[at["attack_types"][k][i] for k in at_keys]] for i, p in enumerate(at["periods"])],
        [("Subtitle", at_note or "none"), ("Not published", ", ".join(at.get("not_published") or []) or "none")],
        {k: sum(v for v in at["attack_types"][k] if v is not None) for k in at_keys},
    ))

    # ---- oaicSectorsChart (+ oaicSectorRatioChart: the same data) ---------------------------------------
    sc = d["oaic_sectors"]
    sc_title = f"OAIC Top {sc.get('sector_count') or len(sc['sectors'])} Affected Sectors ({sc.get('period_label') or ''})"

    def draw_sc():
        fig, ax = new_figure(sc_title, size=(FIG_W, 4.8))
        y = np.arange(len(sc["sectors"]))
        h = 0.4
        ax.barh(y - h / 2, sc["oaic_counts"], h, color=PRIMARY, edgecolor="#1f2937", linewidth=0.3,
                label="OAIC Official Count")
        ax.barh(y + h / 2, sc["database_counts"], h, color=SUCCESS, edgecolor="#1f2937", linewidth=0.3,
                hatch="///", label="Database Count")
        ax.set_yticks(y)
        ax.set_yticklabels([wrap(s, 34) for s in sc["sectors"]], fontsize=7.5)
        ax.invert_yaxis()
        ax.set_xlabel(f"Total Notifications ({sc.get('period_label') or ''})", fontsize=9)
        ax.grid(axis="x", color="#e5e7eb", linewidth=0.6)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.tick_params(labelsize=8)
        legend(ax, ncol=2)
        return finish(fig, top=0.9)

    figs.append(Figure(
        "oaicSectorsChart", sc_title,
        "For the sectors with the most notifications to the OAIC, the total notifications the OAIC received "
        "against the number of database incidents in the same sector and period.",
        draw_sc,
        [("y axis (sector)", "group", "OAIC industry sector; database incidents are mapped to it from the "
                                      "victim's industry."),
         (f"Total Notifications ({sc.get('period_label') or ''})", "x axis",
          f"Count summed over {sc.get('period_start')} to {sc.get('period_end')}."),
         ("OAIC Official Count", "series", "Notifications the OAIC reports for the sector, summed over the "
                                           "period."),
         ("Database Count", "series", "Database incidents in the sector over the same period."),
         ("Database/OAIC %", "data input (dashboard's ratio chart)",
          "Database Count / OAIC Official Count x 100, rounded; drawn on the dashboard as the separate chart "
          "'Comparison: Database/OAIC Ratio by Sector', exported here as data.")],
        ["sector", "oaic_count", "database_count", "ratio", "database_over_oaic_pct"],
        [[s, sc["oaic_counts"][i], sc["database_counts"][i], sc["ratios"][i], round(sc["ratios"][i] * 100)]
         for i, s in enumerate(sc["sectors"])],
        [("Period", f"{sc.get('period_start')} to {sc.get('period_end')} ({sc.get('period_label')})"),
         ("Merged visualisation", "The dashboard's 'Comparison: Database/OAIC Ratio by Sector' plots the ratio "
                                  "of this figure's two series; it is the column database_over_oaic_pct "
                                  "(bars >= 100% green, < 100% orange on the dashboard)."),
         ("Total OAIC", sum(sc["oaic_counts"])), ("Total database", sum(sc["database_counts"]))],
        {"oaic_count": sum(sc["oaic_counts"]), "database_count": sum(sc["database_counts"])},
    ))

    # ---- oaicIndividualsAffectedChart -------------------------------------------------------------------
    ia = d["oaic_individuals_affected"]
    ia_sub = ("OAIC states no overall average or median: both are estimated from its bands of individuals "
              "affected")

    def draw_ia():
        fig, ax = new_figure("OAIC: Individuals Affected Trends", ia_sub)
        x = np.arange(len(ia["periods"]))
        series = [("OAIC Average Individuals Affected (estimated)", ia["averages"], DANGER, "-", "o"),
                  ("OAIC Median Individuals Affected (estimated)", ia["medians"], PRIMARY, "--", "s"),
                  ("Database Average Records Affected", ia["db_averages"], SUCCESS, "-.", "^")]
        floor = min(v for _, vals, *_ in series for v in vals if v) / 2
        for name, vals, colour, ls, mk in series:
            v = [num(a) if a else float("nan") for a in vals]
            ax.fill_between(x, floor, v, color=alpha(colour, 0.125), linewidth=0)
            ax.plot(x, v, color=colour, linestyle=ls, marker=mk, markersize=3.5, linewidth=1.5, label=name)
        ax.set_yscale("log")
        ax.set_ylim(bottom=floor)
        ax.yaxis.set_major_formatter(FuncFormatter(compact))
        ax.set_xticks(x)
        ax.set_xticklabels(ia["periods"], rotation=45, ha="right", fontsize=8)
        ax.set_ylabel("Number of Individuals (log scale)", fontsize=9)
        style_axes(ax)
        legend(ax, ncol=2, fontsize=7)
        return finish(fig, top=0.86)

    figs.append(Figure(
        "oaicIndividualsAffectedChart", "OAIC: Individuals Affected Trends",
        "The average and median number of individuals affected per notified breach each half-year, estimated "
        "from the OAIC's bands, against the database's average records affected per incident.",
        draw_ia,
        [("x axis (unlabelled)", "x axis", "OAIC reporting half-year."),
         ("Number of Individuals (log scale)", "y axis", "Individuals (or records) per breach, logarithmic."),
         ("OAIC Average Individuals Affected (estimated)", "series",
          "Mean individuals affected per notification, estimated from the OAIC's distribution by band."),
         ("OAIC Median Individuals Affected (estimated)", "series", "Median, estimated from the same bands."),
         ("Database Average Records Affected", "series", "Mean records affected per database incident dated in "
                                                         "the half-year, over incidents stating a figure.")],
        ["half_year", "oaic_average_estimated", "oaic_median_estimated", "database_average_records"],
        [[p, ia["averages"][i], ia["medians"][i], ia["db_averages"][i]] for i, p in enumerate(ia["periods"])],
        [("Subtitle", ia_sub)],
        {"oaic_average_estimated": sum(v for v in ia["averages"] if v is not None),
         "oaic_median_estimated": sum(v for v in ia["medians"] if v is not None),
         "database_average_records": sum(v for v in ia["db_averages"] if v is not None)},
    ))

    # ---- oaicMonthlyComparisonChart ---------------------------------------------------------------------
    om = d["oaic_monthly_comparison"]

    def draw_om():
        fig, ax = new_figure("Comparison: Monthly Notifications - OAIC vs Database")
        x = np.arange(len(om["months"]))
        w = 0.42
        ax.bar(x - w / 2, [0 if v is None else v for v in om["oaic_counts"]], w, color=alpha("#2563eb", 0.7),
               label="OAIC")
        ax.bar(x + w / 2, [0 if v is None else v for v in om["db_counts"]], w, color=alpha("#22c55e", 0.7),
               edgecolor="#1f2937", linewidth=0.2, hatch="////", label="Database (news-discovered)")
        ax.set_ylabel("Notifications", fontsize=9)
        month_axis(ax, om["months"])
        style_axes(ax)
        legend(ax, ncol=2)
        return finish(fig, top=0.9)

    figs.append(Figure(
        "oaicMonthlyComparisonChart", "Comparison: Monthly Notifications - OAIC vs Database",
        "The number of notifications the OAIC received each month against the number of database incidents "
        "dated in that month.",
        draw_om,
        [("x axis (unlabelled)", "x axis", "Calendar month (YYYY-MM)."),
         ("Notifications", "y axis", "Count per month."),
         ("OAIC", "series", "Notifications in the month, as the OAIC report for the month's half-year gives "
                            "them (OAIC revises earlier months in later reports)."),
         ("Database (news-discovered)", "series", "Database incidents dated in the month.")],
        ["month", "oaic_notifications", "database_incidents"],
        [[m, om["oaic_counts"][i], om["db_counts"][i]] for i, m in enumerate(om["months"])],
        [("Months", f"{om['months'][0]} to {om['months'][-1]}" if om["months"] else "none"),
         ("Total OAIC", sum(v for v in om["oaic_counts"] if v is not None)),
         ("Total database", sum(v for v in om["db_counts"] if v is not None))],
        {"oaic_notifications": sum(v for v in om["oaic_counts"] if v is not None),
         "database_incidents": sum(v for v in om["db_counts"] if v is not None)},
    ))

    # ---- oaicIndAffDistChart ----------------------------------------------------------------------------
    idd = d["oaic_individuals_affected_distribution"]

    def draw_idd():
        fig, ax = new_figure("Comparison: Individuals-Affected Distribution - OAIC vs Database (2022+)")
        x = np.arange(len(idd["buckets"]))
        w = 0.42
        ax.bar(x - w / 2, idd["oaic_counts"], w, color=alpha("#2563eb", 0.7), label="OAIC")
        ax.bar(x + w / 2, idd["db_counts"], w, color=alpha("#22c55e", 0.7), edgecolor="#1f2937", linewidth=0.2,
               hatch="////", label="Database")
        ax.set_xticks(x)
        ax.set_xticklabels(idd["buckets"], rotation=45, ha="right", fontsize=7.5)
        ax.set_ylabel("Breaches in bucket", fontsize=9)
        style_axes(ax)
        legend(ax, ncol=2)
        return finish(fig, top=0.9)

    figs.append(Figure(
        "oaicIndAffDistChart", "Comparison: Individuals-Affected Distribution - OAIC vs Database (2022+)",
        "The number of breaches in each band of individuals affected, as notified to the OAIC and as recorded "
        "in the database, over the same half-years.",
        draw_idd,
        [("x axis (buckets)", "x axis", "OAIC band of individuals affected per breach."),
         ("Breaches in bucket", "y axis", "Number of breaches in the band, summed over the period."),
         ("OAIC", "series", "Notifications in the band, summed over the OAIC half-years "
                            f"{idd.get('period_start')} to {idd.get('period_end')}."),
         ("Database", "series", "Database incidents whose records-affected figure falls in the band, over the "
                                "same dates.")],
        ["individuals_affected_band", "oaic_breaches", "database_incidents"],
        [[b, idd["oaic_counts"][i], idd["db_counts"][i]] for i, b in enumerate(idd["buckets"])],
        [("Period", f"{idd.get('period_start')} to {idd.get('period_end')} ({idd.get('period_label')})"),
         ("Total OAIC", sum(idd["oaic_counts"])), ("Total database", sum(idd["db_counts"]))],
        {"oaic_breaches": sum(idd["oaic_counts"]), "database_incidents": sum(idd["db_counts"])},
    ))

    # ---- oaicSourceSplitChart ---------------------------------------------------------------------------
    ss = d["oaic_source_split"]
    has_db = any(v for k in ("db_malicious", "db_human", "db_system") for v in (ss.get(k) or []))
    ss_title = ("Comparison: Source of Breaches - OAIC vs Database (per semester)" if has_db
                else "OAIC: Source of Breaches (per semester)")
    ss_sub = ("OAIC stacks left, DB stacks right (DB classified by LLM)" if has_db
              else "OAIC notifications (database events not yet classified by breach source)")

    def draw_ss():
        fig, ax = new_figure(ss_title, ss_sub)
        oaic_series = [("OAIC: Malicious", ss["oaic_malicious"], alpha("#dc2626", 0.7)),
                       ("OAIC: Human err", ss["oaic_human"], alpha("#f59e0b", 0.7)),
                       ("OAIC: System flt", ss["oaic_system"], alpha("#6b7280", 0.7))]
        w = 0.38 if has_db else 0.7
        stacked_bars(ax, ss["periods"], oaic_series, width=w, offset=-w / 2 - 0.01 if has_db else 0)
        if has_db:
            db_series = [("DB: Malicious", ss["db_malicious"], alpha("#22c55e", 0.7)),
                         ("DB: Human err", ss["db_human"], alpha("#22c55e", 0.4)),
                         ("DB: System flt", ss["db_system"], alpha("#22c55e", 0.2))]
            stacked_bars(ax, ss["periods"], db_series, width=w, offset=w / 2 + 0.01, hatch_start=3)
        ax.set_xticks(np.arange(len(ss["periods"])))
        ax.set_xticklabels(ss["periods"], fontsize=8)
        ax.set_ylabel("Notifications", fontsize=9)
        style_axes(ax)
        legend(ax, ncol=3, outside="bottom", fontsize=7)
        return finish(fig, top=0.88)

    ss_keys = ["oaic_malicious", "oaic_human", "oaic_system", "db_malicious", "db_human", "db_system"]
    figs.append(Figure(
        "oaicSourceSplitChart", ss_title,
        "The number of breaches per half-year by source - malicious or criminal attack, human error, or system "
        "fault - as notified to the OAIC and as classified in the database.",
        draw_ss,
        [("x axis (unlabelled)", "x axis", "Half-year (OAIC reporting period)."),
         ("Notifications", "y axis", "Count per half-year (bars stacked by source)."),
         ("OAIC: Malicious / Human err / System flt", "group (left stack)",
          "Notifications whose source the OAIC classes as a malicious or criminal attack, human error, or "
          "system fault."),
         ("DB: Malicious / Human err / System flt", "group (right stack)",
          "Database incidents in the half-year by breach source, as classified by a model from each incident's "
          "evidence (incidents it cannot classify are not counted).")],
        ["half_year", *ss_keys],
        [[p, *[(ss.get(k) or [None] * 99)[i] for k in ss_keys]] for i, p in enumerate(ss["periods"])],
        [("Chart title line", ss_sub),
         *[(f"Total {k}", sum(v for v in (ss.get(k) or []) if v is not None)) for k in ss_keys]],
        {k: sum(v for v in (ss.get(k) or []) if v is not None) for k in ss_keys},
    ))

    # ---- oaicTimeToIdentifyChart / oaicTimeToNotifyChart ------------------------------------------------
    tcols = ["#1d4ed8", "#16a34a", "#f59e0b", "#dc2626", "#7c3aed", "#0891b2", "#ea580c"]
    for page_id, key, title, what in (
            ("oaicTimeToIdentifyChart", "oaic_time_to_identify", "OAIC: Time-to-Identify (% per bucket, time-series)",
             "identify the breach"),
            ("oaicTimeToNotifyChart", "oaic_time_to_notify", "OAIC: Time-to-Notify (% per bucket, time-series)",
             "notify the OAIC after becoming aware of the breach")):
        td = d[key]

        def draw_t(td=td, title=title):
            fig, ax = new_figure(title)
            x = np.arange(len(td["periods"]))
            for k, b in enumerate(td["buckets"]):
                ax.plot(x, [num(v) for v in td["series"].get(b) or []], color=tcols[k % len(tcols)],
                        linestyle=LINESTYLES[k % len(LINESTYLES)], marker=MARKERS[k % len(MARKERS)],
                        markersize=4, linewidth=1.5, label=b)
            ax.set_xticks(x)
            ax.set_xticklabels(td["periods"], fontsize=8)
            ax.set_ylim(bottom=0)
            ax.set_ylabel("% of breaches", fontsize=9)
            style_axes(ax)
            legend(ax, ncol=5)
            return finish(fig, top=0.9)

        figs.append(Figure(
            page_id, title,
            f"The percentage of breaches notified to the OAIC each half-year by the time the entity took to "
            f"{what}.",
            draw_t,
            [("x axis (unlabelled)", "x axis", "OAIC reporting half-year."),
             ("% of breaches", "y axis", "Percentage of the half-year's notifications."),
             *[(b, "series", f"Notifications where the time taken to {what} was "
                             + ("not known." if b == "Unknown" else f"{b}.")) for b in td["buckets"]]],
            ["half_year", *td["buckets"], "total_pct"],
            [[p, *[(td["series"].get(b) or [None] * 99)[i] for b in td["buckets"]],
              sum((td["series"].get(b) or [0] * 99)[i] or 0 for b in td["buckets"])]
             for i, p in enumerate(td["periods"])],
            [("Note", "Percentages as the OAIC publishes them; each half-year sums to about 100 (rounding).")],
            {b: sum(v for v in td["series"].get(b) or [] if v is not None) for b in td["buckets"]},
        ))

    # ---- oaicPersonalInfoChart --------------------------------------------------------------------------
    pi = d["oaic_personal_info_types"]
    cats = list(pi["series"].keys())
    pi_has_db = any(v for v in pi.get("db_classified") or [] if v)
    pi_title = ("Comparison: Personal Information Types over Time - OAIC vs Database (2022+)" if pi_has_db
                else "OAIC: Personal Information Types over Time (2022+)")
    pi_sub = ("Solid = OAIC, Dashed = DB (LLM-classified)" if pi_has_db
              else "OAIC notifications (database events not yet classified by information type)")
    mal_n = sum(v or 0 for v in pi.get("db_malformed") or [])
    uncl_n = sum(v or 0 for v in pi.get("db_unclassified") or [])
    cls_n = sum(v or 0 for v in pi.get("db_classified") or [])
    pi_note = (f"Database rates exclude {mal_n:,} incident(s) with a malformed classification and {uncl_n:,} not "
               f"yet classified (of {cls_n + mal_n + uncl_n:,} in these semesters).") if (mal_n or uncl_n) else None
    pcols = ["#1d4ed8", "#16a34a", "#f59e0b", "#dc2626", "#7c3aed", "#0891b2"]

    def draw_pi():
        fig, ax = new_figure(pi_title, pi_sub + (f"\n{pi_note}" if pi_note else ""), size=(FIG_W, 4.8))
        x = np.arange(len(pi["periods"]))
        for k, c in enumerate(cats):
            colour = pcols[k % len(pcols)]
            base = c.replace("_", " ")
            ax.plot(x, [num(v) for v in (pi.get("series_pct") or {}).get(c) or []], color=colour, linewidth=1.6,
                    marker=MARKERS[k % len(MARKERS)], markersize=4, label="OAIC: " + base)
            if pi_has_db and (pi.get("db_series_pct") or {}).get(c):
                ax.plot(x, [num(v) for v in pi["db_series_pct"][c]], color=colour, linewidth=1.3,
                        linestyle=(0, (6, 4)), marker=MARKERS[k % len(MARKERS)], markersize=4,
                        markerfacecolor="white", label="DB: " + base)
        ax.set_xticks(x)
        ax.set_xticklabels(pi["periods"], fontsize=8)
        ax.set_ylim(bottom=0)
        ax.set_ylabel("% of breaches involving this category", fontsize=9)
        style_axes(ax)
        legend(ax, ncol=4, outside="bottom", fontsize=6.8)
        return finish(fig, top=0.88)

    pi_cols = [f"oaic_pct_{c}" for c in cats] + ([f"db_pct_{c}" for c in cats] if pi_has_db else []) + \
        [f"oaic_count_{c}" for c in cats] + ([f"db_count_{c}" for c in cats] if pi_has_db else []) + \
        ["db_classified", "db_malformed", "db_unclassified"]

    def pi_row(i: int) -> List[Any]:
        row = [(pi.get("series_pct") or {}).get(c, [None] * 99)[i] for c in cats]
        if pi_has_db:
            row += [(pi.get("db_series_pct") or {}).get(c, [None] * 99)[i] for c in cats]
        row += [pi["series"][c][i] for c in cats]
        if pi_has_db:
            row += [(pi.get("db_series") or {}).get(c, [None] * 99)[i] for c in cats]
        row += [(pi.get(k) or [None] * 99)[i] for k in ("db_classified", "db_malformed", "db_unclassified")]
        return row

    figs.append(Figure(
        "oaicPersonalInfoChart", pi_title,
        "The percentage of breaches each half-year that involved each type of personal information, as "
        "notified to the OAIC (solid) and as classified in the database (dashed).",
        draw_pi,
        [("x axis (unlabelled)", "x axis", "OAIC reporting half-year."),
         ("% of breaches involving this category", "y axis",
          "Percentage of the half-year's breaches involving the information type (a breach can involve several)."),
         *[("OAIC: " + c.replace("_", " "), "series (solid)",
            f"Percentage of OAIC notifications involving {c.replace('_', ' ')}.") for c in cats],
         *([("DB: " + c.replace("_", " "), "series (dashed)",
             f"Percentage of database incidents with a well-formed classification involving "
             f"{c.replace('_', ' ')}.") for c in cats] if pi_has_db else []),
         ("db_classified / db_malformed / db_unclassified", "data input (note)",
          "Database incidents in the half-year with a well-formed, malformed or no classification; only the "
          "first are the database rates' denominator.")],
        ["half_year", *pi_cols],
        [[p, *pi_row(i)] for i, p in enumerate(pi["periods"])],
        [("Chart title line", pi_sub), ("Exclusion note", pi_note or "none (no malformed or unclassified incidents)"),
         ("Database incidents classified", cls_n), ("Malformed (excluded)", mal_n), ("Unclassified (excluded)", uncl_n)],
        {**{f"oaic_count_{c}": sum(v for v in pi["series"][c] if v is not None) for c in cats},
         "db_classified": cls_n},
    ))

    # ---- ASD risk matrices ------------------------------------------------------------------------------
    end_label = published["end"].strftime("%d %b %Y").lstrip("0")
    for page_id, key, title in (
            ("asdRiskMatrixAll", "asd_risk_all",
             f"Database: ASD Risk Matrix ({(d['asd_risk_all'].get('period_start') or '')[:4]} to date)"),
            ("asdRiskMatrixPrevious", "asd_risk_previous",
             f"Database: ASD Risk Matrix (Last Year: {d['asd_risk_previous'].get('period_label')})"),
            ("asdRiskMatrixCurrent", "asd_risk_current",
             f"Database: ASD Risk Matrix (Current Year: {d['asd_risk_current'].get('period_label')} to "
             f"{end_label})")):
        figs.append(asd_figure(page_id, title, d[key]))

    # ---- correlationMatrixChart -------------------------------------------------------------------------
    corr = d["event_type_correlation"]
    figs.append(correlation_figure(corr))

    return figs


def pie_figure(title: str, labels: Sequence[str], values: Sequence[float], colours: Sequence[str],
               donut: bool) -> plt.Figure:
    fig, ax = plt.subplots(figsize=(FIG_W, 4.4))
    set_titles(fig, title)
    total = float(sum(values))
    wedges, _ = ax.pie(values, colors=colours, startangle=90, counterclock=False,
                       wedgeprops={"width": 0.5 if donut else 1.0, "edgecolor": "white", "linewidth": 0.8})
    for k, w in enumerate(wedges):
        w.set_hatch(HATCHES[k % len(HATCHES)])
        w.set_edgecolor("#1f2937")
        w.set_linewidth(0.3)
    ax.set_aspect("equal")
    ax.legend(wedges, [f"{lab}: {v:,} ({100 * v / total:.1f}%)" for lab, v in zip(labels, values)],
              loc="center left", bbox_to_anchor=(1.0, 0.5), fontsize=7.5, frameon=False)
    return finish(fig, top=0.92)


def radar_figure(title: str, labels: Sequence[str], values: Sequence[float], colour: str,
                 note: Optional[str]) -> plt.Figure:
    fig = plt.figure(figsize=(FIG_W, 5.2))
    set_titles(fig, title, note)
    ax = fig.add_subplot(111, projection="polar")
    k = len(labels)
    ax.set_theta_offset(math.pi / 2)        # first spoke at the top, clockwise, as Chart.js draws a radar
    ax.set_theta_direction(-1)
    angles = [2 * math.pi * i / k for i in range(k)]
    vals = list(values)
    ax.plot(angles + angles[:1], vals + vals[:1], color=colour, linewidth=2, marker="o", markersize=4)
    ax.fill(angles + angles[:1], vals + vals[:1], color=alpha(colour, 0.125))
    ax.set_thetagrids([math.degrees(a) for a in angles], [wrap(s, 18) for s in labels], fontsize=7.5)
    ax.set_thetalim(0, 2 * math.pi)
    ax.set_ylim(0, 4)
    ax.set_yticks([1, 2, 3, 4])
    ax.set_yticklabels(["Low", "Medium", "High", "Critical"], fontsize=7, color="#374151")
    ax.set_rlabel_position(360 / k / 2)
    ax.tick_params(axis="x", pad=6)
    ax.grid(color="#d1d5db", linewidth=0.6)
    fig.tight_layout(rect=(0, 0, 1, 0.88))
    return fig


def asd_figure(page_id: str, title: str, m: Dict[str, Any]) -> Figure:
    groups = m["stakeholder_groups"]
    impacts = [r["impact_type"] for r in m["matrix"]]
    grid = [[(r.get("counts") or {}).get(g, 0) or 0 for g in groups] for r in m["matrix"]]
    max_value = m.get("max_value") or 0
    unmapped = m.get("unmapped_classifications") or 0
    insufficient = m.get("insufficient_information") or 0
    parts = []
    if insufficient:
        parts.append(f"{insufficient:,} with insufficient information to classify")
    if unmapped - insufficient:
        parts.append(f"{unmapped - insufficient:,} with values outside the ASD vocabulary")
    footer = f"Not placed in the matrix: {'; '.join(parts)}." if unmapped else None
    badge = f"Classifications: {m.get('total_classifications', 0):,}"

    def draw():
        fig, ax = plt.subplots(figsize=(FIG_W, 4.8))
        set_titles(fig, title, badge)
        nr, nc = len(impacts), len(groups)
        for i in range(nr):
            for j in range(nc):
                v = grid[i][j]
                if v and max_value:
                    op = 0.18 + 0.72 * (v / max_value)
                    face = alpha("#2563eb", op)
                    text_colour = "#ffffff" if op > 0.45 else "#111827"
                else:
                    face, text_colour = "white", "#111827"
                ax.add_patch(plt.Rectangle((j, i), 1, 1, facecolor=face, edgecolor="#9ca3af", linewidth=0.6))
                if v:
                    ax.text(j + 0.5, i + 0.5, f"{v}", ha="center", va="center", fontsize=9, fontweight="bold",
                            color=text_colour)
        ax.set_xlim(0, nc)
        ax.set_ylim(nr, 0)
        ax.set_xticks(np.arange(nc) + 0.5)
        ax.set_xticklabels([wrap(g, 12) for g in groups], fontsize=6.3)
        ax.xaxis.tick_top()
        ax.set_yticks(np.arange(nr) + 0.5)
        ax.set_yticklabels([wrap(t, 34) for t in impacts], fontsize=7.5)
        ax.tick_params(length=0)
        for s in ax.spines.values():
            s.set_visible(False)
        ax.set_ylabel("Impact Type", fontsize=9)
        ax.set_xlabel("Stakeholder group", fontsize=9)
        ax.xaxis.set_label_position("top")
        return finish(fig, top=0.86, footer=footer)

    total_cells = sum(sum(r) for r in grid)
    return Figure(
        page_id, title,
        f"The number of incidents dated {m.get('period_start')} to {m.get('period_end')} in each cell of the "
        f"Australian Signals Directorate risk matrix, crossing the incident's impact type (severity category) "
        f"with the stakeholder group affected.",
        draw,
        [("Impact Type", "y axis (rows)", "ASD impact category of the incident, most severe (sustained disruption "
                                          "of essential systems) at the top."),
         ("Stakeholder group (column headers)", "x axis (columns)", "ASD stakeholder group the incident "
                                                                    "affected, from members of the public to "
                                                                    "Commonwealth / national-level entities."),
         ("Cell value", "data input", "Number of active incidents with that ASD classification; shading "
                                      "darkens with the count, blank = none."),
         ("Classifications", "badge", "Incidents placed in the matrix."),
         ("Not placed in the matrix", "note", "Incidents the classifier found insufficient information to "
                                              "place, or placed outside the ASD vocabulary.")],
        ["impact_type", *groups, "row_total"],
        [[t, *grid[i], sum(grid[i])] for i, t in enumerate(impacts)],
        [("Period", f"{m.get('period_start')} to {m.get('period_end')} ({m.get('period_label')})"),
         ("Classifications placed (badge)", m.get("total_classifications")),
         ("Incidents in the population", m.get("population_classifications")),
         ("Not placed: insufficient information", insufficient),
         ("Not placed: outside the ASD vocabulary", unmapped - insufficient),
         ("Largest cell", max_value)],
        {"row_total": total_cells, **{g: sum(r[j] for r in grid) for j, g in enumerate(groups)}},
    )


def correlation_figure(corr: Dict[str, Any]) -> Figure:
    labels, matrix = corr["labels"], corr["correlation_matrix"]
    k = len(labels)
    best_pos, best_neg = (-1.0, ""), (1.0, "")
    for i in range(k):
        for j in range(i + 1, k):
            v = matrix[i][j]
            if v > best_pos[0]:
                best_pos = (v, f"{labels[i]} & {labels[j]}")
            if v < best_neg[0]:
                best_neg = (v, f"{labels[i]} & {labels[j]}")
    stats = [f"Event types: {k}", f"Min correlation: {corr['min_correlation']:.3f}",
             f"Max correlation: {corr['max_correlation']:.3f}"]
    if best_pos[1]:
        stats.append(f"Strongest positive: {best_pos[1]} ({best_pos[0]:.3f})")
    if best_neg[1]:
        stats.append(f"Strongest negative: {best_neg[1]} ({best_neg[0]:.3f})")

    def draw():
        fig, ax = plt.subplots(figsize=(FIG_W, 6.2))
        set_titles(fig, "Database: Event Type Correlation Matrix (Monthly Counts)")
        for i in range(k):
            for j in range(k):
                v = matrix[i][j]
                if v < 0:
                    face, tc = alpha("#ef4444", 0.3 + abs(v) * 0.7), ("white" if abs(v) > 0.5 else "black")
                elif v > 0:
                    face, tc = alpha("#3b82f6", 0.3 + v * 0.7), ("white" if v > 0.5 else "black")
                else:
                    face, tc = alpha("#9ca3af", 0.3), "black"
                ax.add_patch(plt.Rectangle((j, i), 1, 1, facecolor=face, edgecolor="#dddddd", linewidth=0.6))
                ax.text(j + 0.5, i + 0.5, f"{v:.3f}", ha="center", va="center", fontsize=6.3, fontweight="bold",
                        color=tc)
        ax.set_xlim(0, k)
        ax.set_ylim(k, 0)
        ax.set_xticks(np.arange(k) + 0.5)
        ax.set_xticklabels(labels, fontsize=7, rotation=40, ha="left", rotation_mode="anchor")
        ax.xaxis.tick_top()
        ax.set_yticks(np.arange(k) + 0.5)
        ax.set_yticklabels(labels, fontsize=7, fontweight="bold")
        ax.tick_params(length=0)
        for s in ax.spines.values():
            s.set_visible(False)
        ax.set_aspect("equal")
        return finish(fig, top=0.94, footer=wrap(" • ".join(stats), 130))

    return Figure(
        "correlationMatrixChart", "Database: Event Type Correlation Matrix (Monthly Counts)",
        "The Pearson correlation between the monthly counts of each pair of event types.",
        draw,
        [("Row / column labels", "groups", "Event types with enough months of data to correlate."),
         ("Cell value", "data input", "Pearson correlation coefficient of the two types' monthly incident counts "
                                      "(January 2020 to the publication month); blue = positive, red = "
                                      "negative, shade = strength."),
         ("Strongest positive / negative", "note", "The off-diagonal pairs with the largest and smallest "
                                                   "coefficients.")],
        ["event_type", *labels],
        [[lab, *matrix[i]] for i, lab in enumerate(labels)],
        [("Statistics line", " • ".join(stats)), ("Min correlation", corr["min_correlation"]),
         ("Max correlation", corr["max_correlation"])],
        {lab: sum(matrix[i][j] for i in range(k)) for j, lab in enumerate(labels)},
    )


# --------------------------------------------------------------------------------------------------------
# Inputs: the published snapshot and the published page
# --------------------------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------------------------
# Supplier vs non-supplier figures (supplier_comparison.html)
# --------------------------------------------------------------------------------------------------------
def _percent(n: int, whole: int) -> float:
    return 100.0 * n / whole if whole else 0.0


def _supplier_notes(c: "supplier.Comparison", r: "supplier.TestResult") -> List[Tuple[str, Any]]:
    """The notes both supplier figures carry: the groups, the unit, the test and the incident check."""
    again = next(x for x in c.sensitivity if x.key == r.key)
    n_supplier = len(c.supplier_events)
    notes: List[Tuple[str, Any]] = [
        ("Supplier event", "An Active event linked to a supplier incident (as a customer's event or the supplier's "
                           "own event) or recording a vendor or a service provider; every other Active event of "
                           "the reporting period is a non-supplier event"),
        ("Unit", "The event (an event with several victim organisations is counted once)"),
        ("Supplier events in the reporting period", n_supplier),
        ("Non-supplier events in the reporting period", len(c.events) - n_supplier),
        ("Test", r.method if r.ran else f"not run: {r.reason}"),
    ]
    if r.ran:
        notes += [
            ("Test statistic", supplier.fmt_statistic(r)),
            ("Degrees of freedom", r.dof),
            ("p-value", r.p),
            (f"Holm-adjusted p-value (across the {c.family} tests of the page)", r.holm_p),
            ("Effect size", supplier.fmt_effect(r)),
            ("Conclusion", supplier.verdict(r)),
            ("Conclusion sentence (generated)", supplier.conclusion(r, c.family)),
        ]
        if again.ran:
            notes += [
                ("Check, one event per supplier incident: Holm-adjusted p-value", again.holm_p),
                ("Check, one event per supplier incident: effect size", supplier.fmt_effect(again)),
                ("Check, one event per supplier incident: conclusion", supplier.verdict(again)),
            ]
    return notes


def supplier_figures(c: "supplier.Comparison") -> List[Figure]:
    """The two figures taken from ``supplier_comparison.html``: records affected and attack type by group."""
    sup, oth = c.records
    period = f"{c.period.start_iso} to {c.period.end_iso}"
    group_of = {True: supplier.SUPPLIER, False: supplier.OTHER}
    edge = "#1f2937"

    # ---- supplierRecordsHistogram ----------------------------------------------------------------------
    decades, bands, a, b = supplier.histogram_parts(sup, oth)
    rec = next(r for r in c.results if r.key == supplier.RECORDS_KEY)
    rec_title = "Records Affected: Supplier vs Non-Supplier Events"
    rec_sub = (f"Share of each group's events that state a records-affected figure "
               f"({sup.known} of {sup.events} supplier, {oth.known} of {oth.events} non-supplier)")
    rec_footer = (f"{supplier.verdict(rec).capitalize()}: Mann-Whitney U test, Holm-adjusted p = "
                  f"{supplier.fmt_p(rec.holm_p)}, {rec.effect_name} = {rec.effect:.3f}" if rec.ran
                  else f"Not tested: {rec.reason}")

    def draw_records():
        fig, ax = new_figure(rec_title, rec_sub)
        x = np.arange(decades)
        w = 0.4
        for offset, counts, summary, colour, hatch in (
                (-w / 2, a, sup, SUPPLIER_COLOUR, HATCHES[0]), (w / 2, b, oth, NON_SUPPLIER_COLOUR, HATCHES[1])):
            shares = [_percent(n, summary.known) for n in counts]
            ax.bar(x + offset, shares, w, label=f"{summary.group} (n = {summary.known})", color=alpha(colour, 0.85),
                   edgecolor=edge, linewidth=0.3, hatch=hatch)
            for i, (n, share) in enumerate(zip(counts, shares)):
                if n:
                    ax.text(i + offset, share, f"{share:.0f}%", ha="center", va="bottom", fontsize=6.5)
        ax.set_xticks(x)
        ax.set_xticklabels([f"{compact(10 ** k)} to\n<{compact(10 ** (k + 1))}" for k in range(decades)], fontsize=7.5)
        ax.set_xlabel("Records affected (order-of-magnitude band)", fontsize=9)
        ax.set_ylabel("Share of the group's events (%)", fontsize=9)
        style_axes(ax)
        legend(ax, ncol=2)
        return finish(fig, top=0.86, footer=rec_footer)

    rec_rows = [[band, 10 ** k, 10 ** (k + 1), n_s, _percent(n_s, sup.known), n_o, _percent(n_o, oth.known)]
                for k, (band, n_s, n_o) in enumerate(zip(bands, a, b))]
    rec_events = [[e["event_id"], e["event_date"], group_of[supplier.is_supplier(e)], e["records"]]
                  for e in c.events if e["records"] is not None]
    rec_figure = Figure(
        "supplierRecordsHistogram", rec_title,
        "The share of supplier events and of non-supplier events in each order-of-magnitude band of records "
        "affected, over the events of each group that state a records-affected figure; a supplier event is an "
        "Active event linked to a supplier incident or recording a vendor or service provider.",
        draw_records,
        [("Records affected (x axis)", "x axis",
          "Order-of-magnitude band of the event's records-affected figure: from the lower bound up to, not "
          "including, ten times it (1 to 9, 10 to 99, ...)."),
         ("Share of the group's events (%)", "y axis",
          "Events of the group in the band as a percentage of the group's events that state a figure; each "
          "group's bars sum to 100%."),
         (supplier.SUPPLIER, "series",
          "Active events linked to a supplier incident (as a customer's event or the supplier's own event) or "
          "recording a vendor or a service provider."),
         (supplier.OTHER, "series", "Every other Active event of the reporting period."),
         ("records_affected_band", "data column", "The band, as whole records (1,000 to 9,999)."),
         ("band_from / band_below", "data columns", "Lower bound of the band (included) and upper bound (excluded)."),
         ("supplier_events / non_supplier_events", "data columns", "Events of the group in the band."),
         ("supplier_percent / non_supplier_percent", "data columns",
          "The same as a percentage of the group's events with a figure (the plotted heights)."),
         ("events (sheet)", "raw data",
          "One row per event with a records-affected figure: event id, event date, group and the figure.")],
        ["records_affected_band", "band_from", "band_below", "supplier_events", "supplier_percent",
         "non_supplier_events", "non_supplier_percent"],
        rec_rows,
        [("Supplier events with a records-affected figure", sup.known),
         ("Supplier events with no figure (left out)", sup.missing),
         ("Non-supplier events with a records-affected figure", oth.known),
         ("Non-supplier events with no figure (left out)", oth.missing),
         ("Median records affected, supplier events", supplier.quartiles(sup.values)[1] if sup.values else None),
         ("Median records affected, non-supplier events", supplier.quartiles(oth.values)[1] if oth.values else None),
         *_supplier_notes(c, rec)],
        {"supplier_events": sup.known, "non_supplier_events": oth.known,
         "supplier_percent": 100.0 if sup.known else 0.0, "non_supplier_percent": 100.0 if oth.known else 0.0},
        source_page=SUPPLIER_PAGE, period=period,
        extra_sheets={"events": (["deduplicated_event_id", "event_date", "group", "records_affected"], rec_events)},
    )

    # ---- supplierAttackTypeChart -----------------------------------------------------------------------
    tab = next(t for t in c.tabulations if t.attribute.key == "attack_type")
    att = next(r for r in c.results if r.key == "attack_type")
    known_s, known_o = sum(tab.supplier), sum(tab.other)
    pooled = {name for group in att.pooled for name in group}
    att_title = "Attack Type: Supplier vs Non-Supplier Events"
    att_sub = (f"Share of each group's events with a recorded attack type "
               f"({known_s} supplier, {known_o} non-supplier)")
    att_footer = (f"{supplier.verdict(att).capitalize()}: {att.method.lower()}, Holm-adjusted p = "
                  f"{supplier.fmt_p(att.holm_p)}, {att.effect_name} = {att.effect:.3f}" if att.ran
                  else f"Not tested: {att.reason}")

    def draw_attack():
        fig, ax = new_figure(att_title, att_sub, size=(FIG_W, max(FIG_H, 1.6 + 0.3 * len(tab.labels))))
        y = np.arange(len(tab.labels))
        h = 0.4
        for offset, counts, whole, name, colour, hatch in (
                (-h / 2, tab.supplier, known_s, supplier.SUPPLIER, SUPPLIER_COLOUR, HATCHES[0]),
                (h / 2, tab.other, known_o, supplier.OTHER, NON_SUPPLIER_COLOUR, HATCHES[1])):
            shares = [_percent(n, whole) for n in counts]
            ax.barh(y + offset, shares, h, label=f"{name} (n = {whole})", color=alpha(colour, 0.85),
                    edgecolor=edge, linewidth=0.3, hatch=hatch)
            for i, share in enumerate(shares):
                ax.text(share, i + offset, f" {share:.1f}%", ha="left", va="center", fontsize=6.5)
        ax.set_yticks(y)
        ax.set_yticklabels(tab.labels, fontsize=8)
        ax.invert_yaxis()
        ax.set_xlabel("Share of the group's events (%)", fontsize=9)
        ax.margins(x=0.08)
        style_axes(ax)
        ax.grid(False, axis="y")
        ax.grid(axis="x", color="#e5e7eb", linewidth=0.6)
        legend(ax, ncol=2)
        return finish(fig, top=0.86, footer=att_footer)

    att_rows = [[name, n_s, _percent(n_s, known_s), n_o, _percent(n_o, known_o), "yes" if name in pooled else "no"]
                for name, n_s, n_o in zip(tab.labels, tab.supplier, tab.other)]
    att_events = [[e["event_id"], e["event_date"], group_of[supplier.is_supplier(e)],
                   e["values"]["attack_type"][0] or f"({e['values']['attack_type'][1]})"] for e in c.events]
    left_out = [(f"{why}: supplier / non-supplier events left out", f"{n_s} / {n_o}")
                for why, n_s, n_o in tab.left_out]
    att_figure = Figure(
        "supplierAttackTypeChart", att_title,
        "The percentage distribution of attack types among supplier events and among non-supplier events, over "
        "the events of each group with a recorded attack type; a supplier event is an Active event linked to a "
        "supplier incident or recording a vendor or service provider.",
        draw_attack,
        [("Attack type (y axis)", "y axis",
          "DeduplicatedEvents.event_type of the event, largest category (both groups together) first."),
         ("Share of the group's events (%)", "x axis",
          "Events of the group with the attack type as a percentage of the group's events with a recorded "
          "attack type; each group's bars sum to 100%."),
         (supplier.SUPPLIER, "series",
          "Active events linked to a supplier incident (as a customer's event or the supplier's own event) or "
          "recording a vendor or a service provider."),
         (supplier.OTHER, "series", "Every other Active event of the reporting period."),
         ("supplier_events / non_supplier_events", "data columns", "Events of the group with the attack type."),
         ("supplier_percent / non_supplier_percent", "data columns",
          "The same as a percentage of the group's events with a recorded attack type (the plotted lengths)."),
         ("pooled_for_test", "data column",
          "yes when the category was pooled with other sparse categories for the chi-squared test (expected "
          "counts too small); the chart shows every category unpooled."),
         ("events (sheet)", "raw data",
          "One row per event of the reporting period: event id, event date, group and attack type (in brackets "
          "when not recorded, which the chart leaves out).")],
        ["attack_type", "supplier_events", "supplier_percent", "non_supplier_events", "non_supplier_percent",
         "pooled_for_test"],
        att_rows,
        [("Supplier events with a recorded attack type", known_s),
         ("Non-supplier events with a recorded attack type", known_o),
         *left_out,
         ("Expected counts and pooling", supplier.pooling_note(att)),
         *_supplier_notes(c, att)],
        {"supplier_events": known_s, "non_supplier_events": known_o,
         "supplier_percent": 100.0 if known_s else 0.0, "non_supplier_percent": 100.0 if known_o else 0.0},
        source_page=SUPPLIER_PAGE, period=period,
        extra_sheets={"events": (["deduplicated_event_id", "event_date", "group", "attack_type"], att_events)},
    )
    return [rec_figure, att_figure]


# --------------------------------------------------------------------------------------------------------
# Recurrence rate by organisation size band (recurrence.html)
# --------------------------------------------------------------------------------------------------------
def read_recurrence_run(run_dir: Path, pub: Dict[str, Any]) -> Tuple["pd.DataFrame", Dict[str, Any], Dict[str, Any]]:
    """The main recurrence run's spells, summary and inputs; the run must be on the published snapshot."""
    import pandas as pd

    spells, summary, inputs = (run_dir / "recurrent_event_spells.csv", run_dir / "analysis_summary.json",
                               run_dir / "analysis_inputs.json")
    for path in (spells, summary, inputs):
        if not path.is_file():
            raise ExportError(f"no {path}: run scripts/analyze_recurrent_timing.py on the published snapshot first")
    inputs_json = json.loads(inputs.read_text(encoding="utf-8"))
    if inputs_json.get("snapshot_id") != pub["snapshot_id"]:
        raise ExportError(f"the recurrence run in {run_dir} is on snapshot {inputs_json.get('snapshot_id')}, not "
                          f"the published {pub['snapshot_id']}; rerun the analyses on the published snapshot")
    return pd.read_csv(spells), json.loads(summary.read_text(encoding="utf-8")), inputs_json


def recurrence_size_parts(spells: "pd.DataFrame") -> Tuple["pd.DataFrame", Dict[str, np.ndarray], List[Dict[str, Any]]]:
    """Organisations, resampled band rates and band rows, exactly as the page computes them."""
    orgs = size_plot.organisation_rates(spells)
    resampled = size_plot.bootstrap_rates(orgs)
    rows = size_plot.band_table(orgs, resampled)
    if not rows:
        raise ExportError("no organisation with a known size and follow-up in the recurrence spells")
    return orgs, resampled, rows


def verify_recurrence_page(rows: List[Dict[str, Any]], page: Path, pub: Dict[str, Any]) -> None:
    """The published recurrence page must come from this snapshot and show these band rates."""
    import html as html_lib

    if not page.is_file():
        raise ExportError(f"no {page}: publish the dashboards first")
    text = page.read_text(encoding="utf-8")
    stamped = re.search(r'<meta name="data-snapshot" content="([^"]+)"', text)
    if not stamped or stamped.group(1) != pub["snapshot_id"]:
        raise ExportError(f"{page.name} is from snapshot {stamped.group(1) if stamped else '(none)'}, not the "
                          f"published {pub['snapshot_id']}; republish first")
    if f'alt="{html_lib.escape(size_plot.alt_text(rows))}"' not in text:
        raise ExportError(f"the size-band rates recomputed from the recurrence run ({size_plot.alt_text(rows)}) "
                          f"are not the ones in the published {page.name}; republish first")


def recurrence_size_figure(spells: "pd.DataFrame", summary: Dict[str, Any], inputs: Dict[str, Any]) -> Figure:
    """The right panel of recurrence.html's size chart: each size band's overall recurrence rate."""
    orgs, resampled, rows = recurrence_size_parts(spells)
    bands = [row["band"] for row in rows]
    stats = {band: size_plot.box_stats(resampled, band) for band in bands}
    unknown = orgs[orgs["size"] == "UNKNOWN"]
    executive = summary.get("executive_summary") or {}
    size_model = executive.get("size_model") or {}
    ratios = size_model.get("rate_ratio_vs_small") or {}
    answer = next((q.get("answer") for q in executive.get("questions", []) if q.get("key") == "size"), None)
    if not ratios or answer is None:
        raise ExportError("the recurrence run's analysis_summary.json has no size model in its executive summary")
    title = "Recurrence Rate by Organisation Size Band"
    subtitle = ("Repeats per 100 organisation-years of follow-up (unadjusted); box and whiskers: middle 50% and "
                "95% of the rate when each band's organisations are resampled")
    steps = ", ".join(f"{band} {ratios[band]:.1f}" for band in bands if band != "SMALL" and band in ratios)
    p = size_model.get("bootstrap_p")
    footer = (f"Adjusted (monotone model with elapsed-time bands, prior event number and a calendar trend): "
              f"{steps} times SMALL; bootstrap p = {p:.3f}. Organisations of unknown size ({len(unknown)}) are "
              f"not shown." if p is not None else None)

    def draw():
        fig, ax = new_figure(title, subtitle)
        data = [resampled[band] for band in bands]
        ax.boxplot(data, positions=range(len(bands)), widths=0.5, whis=(2.5, 97.5), showfliers=False,
                   patch_artist=True,
                   boxprops={"facecolor": size_plot.ACCENT_FILL, "edgecolor": size_plot.ACCENT, "linewidth": 1},
                   medianprops={"color": size_plot.ACCENT, "linewidth": 2},
                   whiskerprops={"color": size_plot.ACCENT, "linewidth": 1},
                   capprops={"color": size_plot.ACCENT, "linewidth": 1})
        highest = max(row["high"] for row in rows)
        for i, row in enumerate(rows):
            ax.text(i, row["high"] + highest * 0.02, f"{row['rate']:.1f}", ha="center", va="bottom", fontsize=9)
        ax.set_ylim(0, highest * 1.14)
        ax.set_xticks(range(len(bands)))
        ax.set_xticklabels([f"{row['band']}\n{row['organisations']} organisations\n{row['repeats']} repeats"
                            for row in rows], fontsize=8)
        ax.set_ylabel("Repeats per 100 organisation-years", fontsize=9)
        style_axes(ax)
        return finish(fig, top=0.84, footer=footer)

    data_rows = [[row["band"], row["organisations"], row["with_repeat"], row["repeats"], round(row["years"], 4),
                  round(row["rate"], 4), round(row["low"], 4), round(row["high"], 4),
                  round(stats[row["band"]]["box_low"], 4), round(stats[row["band"]]["box_median"], 4),
                  round(stats[row["band"]]["box_high"], 4), round(row["org_q3"], 4), ratios.get(row["band"])]
                 for row in rows]
    org_rows = [[int(r.entity_id), r.size, int(r.repeats), round(float(r.years), 4), round(float(r.rate), 4),
                 "yes" if r.size in bands else "no (unknown size)"]
                for r in orgs.sort_values(["size", "entity_id"]).itertuples()]
    dictionary = [
        ("Organisation size band (x axis)", "x axis",
         "The victim organisation's size band, SMALL (fewer than 20 employees or under A$10m revenue), MEDIUM "
         "(20-199 or A$10m-100m), LARGE (200-4,999 or A$100m-1b) or HUGE (5,000+ or over A$1b), reconciled "
         "across the names one organisation is stored under."),
        ("Repeats per 100 organisation-years", "y axis",
         "The band's recurrence rate: its organisations' repeat incidents divided by their years of follow-up "
         "after an incident, times 100. Unadjusted."),
        ("Number above each box", "label", "The band's overall rate (column rate_per_100_org_years)."),
        ("Box", "group spread",
         "The middle 50% (25th to 75th percentile) of the band's rate over 4,000 resamples of the band's "
         "organisations with replacement; it shows how sure the rate is, not how organisations differ."),
        ("Line in the box", "group spread", "The median of the resampled rates."),
        ("Whiskers", "group spread", "The 2.5th to 97.5th percentiles of the resampled rates (a 95% interval)."),
        ("organisations / repeats (axis labels)", "group size",
         "Organisations in the band with follow-up, and the repeat incidents among them."),
        ("band", "data column", "Size band."),
        ("organisations", "data column", "Victim organisations in the band with follow-up after an incident."),
        ("organisations_with_repeat", "data column", "Of those, organisations with at least one repeat incident."),
        ("repeats", "data column", "Repeat incidents: a later incident of the same organisation."),
        ("follow_up_years", "data column", "Total follow-up of the band's organisations, in years (365.25 days)."),
        ("rate_per_100_org_years", "data column", "repeats / follow_up_years x 100 (the number plotted above each box)."),
        ("interval_low / interval_high", "data columns", "Whisker ends: 2.5th and 97.5th percentiles of the resampled rate."),
        ("box_low / box_median / box_high", "data columns", "25th, 50th and 75th percentiles of the resampled rate."),
        ("upper_quartile_of_organisation_rates", "data column",
         "75th percentile of the band's organisations' own rates (0 where at least three quarters have no repeat)."),
        ("adjusted_rate_ratio_vs_small", "data column",
         "The band's rate relative to SMALL in the adjusted monotone model of the recurrence analysis "
         "(elapsed-time bands, prior event number and a calendar trend held)."),
        ("organisations (sheet)", "raw data",
         "One row per organisation with follow-up: entity id, size band, repeats, years of follow-up, its own rate "
         "and whether it is plotted (unknown size is not)."),
    ]
    notes = [
        ("Unit", "A victim organisation's follow-up after each recorded incident (a spell); a repeat is its next "
                 "incident. Events before 2020 are not analysed."),
        ("Adjustment", "None in the plotted rates. Size also stands in for visibility, attack exposure and the value "
                       "of targets, which are not measured separately."),
        ("Adjusted effect and test (generated, executive summary)", answer),
        ("Adjusted rate ratios vs SMALL", "; ".join(f"{k} {v:.3f}" for k, v in ratios.items())),
        ("Bootstrap p-value of the adjusted size effect", p),
        ("Organisations of unknown size (not plotted)", len(unknown)),
        ("Repeats among organisations of unknown size", int(unknown["repeats"].sum())),
        ("Resamples per band", size_plot.BOOTSTRAP_REPLICATES),
        ("Resampling seed", size_plot.BOOTSTRAP_SEED),
        ("Recurrence run", str(RECURRENCE_RUN).replace("\\", "/")),
        ("Recurrence run censor date", inputs.get("censor_date")),
        ("Recurrence run recorded at", inputs.get("recorded_at")),
    ]
    totals = {"organisations": sum(r["organisations"] for r in rows), "repeats": sum(r["repeats"] for r in rows)}
    return Figure(
        RECURRENCE_FIGURES[0], title,
        "The recurrence rate of cyber incidents (repeat incidents per 100 organisation-years of follow-up) for "
        "victim organisations in each size band, with the uncertainty of each band's rate from resampling its "
        "organisations; unadjusted.",
        draw, dictionary,
        ["band", "organisations", "organisations_with_repeat", "repeats", "follow_up_years", "rate_per_100_org_years",
         "interval_low", "interval_high", "box_low", "box_median", "box_high", "upper_quartile_of_organisation_rates",
         "adjusted_rate_ratio_vs_small"],
        data_rows, notes, totals, source_page=RECURRENCE_PAGE,
        period=f"2020-01-01 to {inputs.get('censor_date')} (recurrence follow-up, censored at the last ingest)",
        extra_sheets={"organisations": (["entity_id", "size_band", "repeats", "follow_up_years",
                                         "rate_per_100_org_years", "plotted"], org_rows)},
    )


def verify_supplier_page(c: "supplier.Comparison", page: Path, pub: Dict[str, Any]) -> None:
    """The published supplier page must come from this snapshot and hold the charts recomputed here.

    The page embeds no data block, so the check is on the charts themselves: the histogram and the
    attack-type bars built from the recomputed comparison - every bar's size and its tooltip with the
    count, the group total and the percentage - must appear verbatim in the published page.
    """
    if not page.is_file():
        raise ExportError(f"no {page}: publish the dashboards first")
    text = page.read_text(encoding="utf-8")
    stamped = re.search(r'<meta name="data-snapshot" content="([^"]+)"', text)
    if not stamped or stamped.group(1) != pub["snapshot_id"]:
        raise ExportError(f"{page.name} is from snapshot {stamped.group(1) if stamped else '(none)'}, not the "
                          f"published {pub['snapshot_id']}; republish first")
    attack = next(t for t in c.tabulations if t.attribute.key == "attack_type")
    for name, chart in (("records-affected histogram", supplier.records_histogram(*c.records)),
                        ("attack-type chart", supplier.bar_chart(attack))):
        if chart not in text:
            raise ExportError(f"the {name} recomputed from the snapshot differs from the one in the published "
                              f"{page.name}; republish first")


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_publication(root: Path) -> Dict[str, Any]:
    path = root / "dashboard" / "publication.json"
    if not path.is_file():
        raise ExportError(f"no publication manifest at {path}: publish the dashboard first")
    pub = json.loads(path.read_text(encoding="utf-8"))
    for key in ("snapshot_id", "published_at"):
        if not pub.get(key):
            raise ExportError(f"{path} has no {key}")
    pub["end"] = datetime.fromisoformat(pub["published_at"]).date()
    return pub


def check_oaic_input(root: Path, pub: Dict[str, Any]) -> None:
    """The OAIC dataset is outside the snapshot: it must be the one the publication hashed."""
    expected = (pub.get("external_inputs") or {}).get("oaic_statistics.json")
    path = root / "instance" / "oaic" / "oaic_statistics.json"
    if not expected:
        raise ExportError("publication.json records no hash of oaic_statistics.json")
    if not path.is_file():
        raise ExportError(f"OAIC dataset missing: {path}")
    actual = file_sha256(path)
    if actual != expected:
        raise ExportError(f"OAIC dataset changed since publication ({actual[:12]} != {expected[:12]}): the OAIC "
                          f"charts would not match the published page; republish first")


def embedded_data(index_html: Path) -> Dict[str, Any]:
    text = index_html.read_text(encoding="utf-8")
    out: Dict[str, Any] = {}
    for m in re.finditer(r"^\s*const (\w+) = (\{.*\}|null);\s*$", text, re.M):
        if m.group(1) in EMBEDDED:
            out[m.group(1)] = json.loads(m.group(2))
    missing = sorted(set(EMBEDDED) - set(out))
    if missing:
        raise ExportError(f"{index_html} does not embed {', '.join(missing)}: the page has changed shape")
    return out


def differences(a: Any, b: Any, path: str = "") -> List[str]:
    """Where two JSON values differ (floats to 1e-9 relative)."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{path}.{k}: present on one side only")
            else:
                out += differences(a[k], b[k], f"{path}.{k}")
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: length {len(a)} != {len(b)}"]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += differences(x, y, f"{path}[{i}]")
        return out
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return [] if math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12) else [f"{path}: {a} != {b}"]
    return [] if a == b else [f"{path}: {a!r} != {b!r}"]


def verify_against_page(data: Dict[str, Any], page: Dict[str, Any]) -> None:
    problems = []
    for const, key in EMBEDDED.items():
        mine = json.loads(json.dumps(data.get(key), default=str))
        diff = differences(mine, page[const], const)
        problems += diff[:5]
    if problems:
        raise ExportError("the data recomputed from the snapshot differs from the published page:\n  "
                          + "\n  ".join(problems[:40]))


# --------------------------------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------------------------------
def dictionary_text(fig: Figure, nn: str) -> str:
    rows = [("Label", "Role", "Description"), *fig.dictionary]
    w1 = min(max(len(r[0]) for r in rows), 48)
    w2 = max(len(r[1]) for r in rows)
    lines = [f"Figure {nn}: {fig.title}", "Data dictionary", ""]
    for k, (a, b, c) in enumerate(rows):
        a_lines = textwrap.wrap(a, w1) or [""]
        c_lines = textwrap.wrap(c, 70) or [""]
        for i in range(max(len(a_lines), len(c_lines))):
            lines.append(f"{(a_lines[i] if i < len(a_lines) else ''):<{w1}}  "
                         f"{(b if i == 0 else ''):<{w2}}  {(c_lines[i] if i < len(c_lines) else '')}".rstrip())
        if k == 0:
            lines.append(f"{'-' * w1}  {'-' * w2}  {'-' * 70}")
    return "\n".join(lines) + "\n"


def cell(v: Any) -> Any:
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    return v


def write_xlsx(path: Path, fig: Figure, nn: str, provenance: List[Tuple[str, Any]], stamp: datetime) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "data"
    ws.append(fig.columns)
    for c in ws[1]:
        c.font = Font(bold=True)
    for row in fig.rows:
        ws.append([cell(v) for v in row])
    for i, col in enumerate(fig.columns, start=1):
        width = max([len(str(col))] + [len(str(r[i - 1])) for r in fig.rows if r[i - 1] is not None][:200])
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = min(max(width + 2, 10), 60)
    ws.freeze_panes = "B2"
    for name, (columns, sheet_rows) in fig.extra_sheets.items():
        extra = wb.create_sheet(name)
        extra.append(columns)
        for c in extra[1]:
            c.font = Font(bold=True)
        for row in sheet_rows:
            extra.append([cell(v) for v in row])
        for i, col in enumerate(columns, start=1):
            width = max([len(str(col))] + [len(str(r[i - 1])) for r in sheet_rows[:200] if r[i - 1] is not None])
            extra.column_dimensions[openpyxl.utils.get_column_letter(i)].width = min(max(width + 2, 10), 60)
        extra.freeze_panes = "A2"
    notes = wb.create_sheet("notes")
    notes.append(["Item", "Value"])
    for c in notes[1]:
        c.font = Font(bold=True)
    notes.append(["Figure", f"figure{nn}: {fig.title}"])
    for k, v in [*fig.notes, *provenance]:
        notes.append([k, cell(v) if not isinstance(v, (list, dict)) else json.dumps(v)])
    notes.column_dimensions["A"].width = 55
    notes.column_dimensions["B"].width = 100
    wb.properties.creator = "scripts/export_paper_figures.py"
    wb.properties.created = stamp
    wb.properties.modified = stamp
    tmp = path.with_suffix(".xlsx.tmp")
    wb.save(tmp)
    os.replace(tmp, path)


def check_totals(path: Path, fig: Figure) -> List[Tuple[str, float, float]]:
    """Read the xlsx back and compare each column's total with the total from the data."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["data"]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    header = list(rows[0])
    out = []
    for col, expected in fig.totals.items():
        if col not in header:
            raise ExportError(f"{path.name}: no column {col!r} to check")
        j = header.index(col)
        got = sum(float(r[j]) for r in rows[1:] if isinstance(r[j], (int, float)))
        if not math.isclose(got, float(expected), rel_tol=1e-9, abs_tol=1e-9):
            raise ExportError(f"{path.name}: column {col} totals {got} in the xlsx but {expected} in the data")
        out.append((col, float(expected), got))
    return out


def cross_checks(d: Dict[str, Any]) -> List[str]:
    """Totals that must agree between charts (the builder's reconciliation already proved them against the
    population; these are restated so the report shows them)."""
    out = []
    mc_total = sum(d["monthly_counts"]["counts"])
    sev_total = sum(sum(v) for v in d["severity_trends"]["data"].values())
    mix_total = sum(sum(v) for v in d["event_type_mix"]["types"].values())
    pie_total = sum(d["overall_event_type_mix"]["counts"])
    hist_months = sum(d["monthly_counts_stats"]["frequencies"])
    pop = (d.get("population") or {})
    checks = [
        ("monthly counts (fig 01) = severity stacks (fig 02)", mc_total, sev_total),
        ("monthly counts (fig 01) = event-type stacks (fig 04)", mc_total, mix_total),
        ("months in the monthly histogram (fig 15) = months Jan 2020 to the publication month",
         hist_months, len(d["monthly_counts"]["months"]) + (1 if d.get("incomplete_month") else 0)),
    ]
    if pop.get("active_in_period") is not None:
        checks.append(("event-type pie (fig 06) = active incidents in the window", pie_total,
                       pop["active_in_period"]))
    for name, a, b in checks:
        if a != b:
            raise ExportError(f"cross-check failed: {name}: {a} != {b}")
        out.append(f"{name}: {a} = {b}")
    for key in ("asd_risk_all", "asd_risk_previous", "asd_risk_current"):
        m = d[key]
        cells = sum(sum((r.get("counts") or {}).values()) for r in m["matrix"])
        if cells != m["total_classifications"] or cells + m["unmapped_classifications"] != m["population_classifications"]:
            raise ExportError(f"cross-check failed: {key} cells {cells}, badge {m['total_classifications']}, "
                              f"not placed {m['unmapped_classifications']}, population "
                              f"{m['population_classifications']}")
        out.append(f"{key}: cells {cells} = badge; + {m['unmapped_classifications']} not placed = "
                   f"{m['population_classifications']} incidents")
    return out


def save_figure(fig: plt.Figure, png: Path, pdf: Path) -> None:
    for path, kw in ((png, {"dpi": DPI, "metadata": {"Software": None}}),
                     (pdf, {"metadata": {"CreationDate": None, "ModDate": None, "Creator": None,
                                         "Producer": None}})):
        tmp = path.with_name(path.stem + ".tmp" + path.suffix)
        fig.savefig(tmp, facecolor="white", bbox_inches="tight", pad_inches=0.08, **kw)
        os.replace(tmp, path)
    plt.close(fig)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", default=".", help="Project root holding dashboard/ and instance/ (default: .)")
    parser.add_argument("--out", default=None, help="Output folder (default: <root>/dashboard/Figures)")
    parser.add_argument("--list", action="store_true", help="Print the figure list and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    plt.rcParams.update({"font.family": "DejaVu Sans", "hatch.linewidth": 0.5, "pdf.fonttype": 42,
                         "svg.hashsalt": "paper-figures"})

    root = Path(args.root).resolve()
    out_dir = Path(args.out).resolve() if args.out else root / "dashboard" / "Figures"
    try:
        pub = read_publication(root)
        check_oaic_input(root, pub)
        page = embedded_data(root / "dashboard" / "index.html")
        live_db = root / "instance" / "cyber_events.db"
        store = snapshots.store_for(live_db)
        period = dashboard_period(pub["end"])
        print(f"Published snapshot {pub['snapshot_id']} (published {pub['published_at']}); reporting period "
              f"{period.start_iso} to {period.end_iso}")
        with tempfile.TemporaryDirectory(prefix="paper-figures-") as tmp:
            db = store.restore(pub["snapshot_id"], Path(tmp))
            cwd = os.getcwd()
            os.chdir(root)          # the OAIC dataset is read relative to the project root
            try:
                data = assemble_dashboard_data(str(db), period)
                # The supplier page's window ends on the day the snapshot was taken (publication/outputs.py).
                taken = str(pub.get("snapshot_taken_at") or pub["published_at"])[:10]
                conn = supplier.connect_read_only(db)
                try:
                    comparison = supplier.collect(conn, dashboard_period(taken))
                finally:
                    conn.close()
            finally:
                os.chdir(cwd)
                gc.collect()        # the builder's sqlite connections close on collection; Windows locks the copy
        verify_against_page(data, page)
        print("Prepared data equals the data embedded in the published index.html (31 data blocks).")
        verify_supplier_page(comparison, root / "dashboard" / SUPPLIER_PAGE, pub)
        print(f"The supplier charts recomputed from the snapshot are the ones in the published {SUPPLIER_PAGE}.")
        spells, run_summary, run_inputs = read_recurrence_run(root / RECURRENCE_RUN, pub)
        verify_recurrence_page(recurrence_size_parts(spells)[2], root / "dashboard" / RECURRENCE_PAGE, pub)
        print(f"The size-band rates from the recurrence run (snapshot {run_inputs['snapshot_id']}) are the ones in "
              f"the published {RECURRENCE_PAGE}.")
        figures = build_figures(data, pub)
        print(f"\nThe page has {len(PAGE_VISUALISATIONS)} visualisations; {len(DUPLICATES)} duplicate "
              f"({', '.join(f'{k} = {v}' for k, v in DUPLICATES.items())}), so {len(figures)} figures; "
              f"{len(SUPPLIER_FIGURES)} more come from {SUPPLIER_PAGE}:")
        figures += supplier_figures(comparison)
        figures.append(recurrence_size_figure(spells, run_summary, run_inputs))
        for k, f in enumerate(figures, start=1):
            print(f"  {k:02d}  {f.title}  [{f.page_id}]")
        expected_ids = [p for p in PAGE_VISUALISATIONS if p not in DUPLICATES] + SUPPLIER_FIGURES + RECURRENCE_FIGURES
        if [f.page_id for f in figures] != expected_ids:
            raise ExportError("figure order does not follow the page")
        if args.list:
            return 0

        out_dir.mkdir(parents=True, exist_ok=True)
        stale = [p for p in out_dir.glob("figure*") if not re.match(rf"figure(0[1-9]|[1-9]\d)", p.name)
                 or int(p.name[6:8]) > len(figures)]
        for p in stale:
            p.unlink()
        stamp = datetime.fromisoformat(pub.get("snapshot_taken_at") or pub["published_at"])
        def provenance_of(f: Figure) -> List[Tuple[str, Any]]:
            return [("Source", f"Australian cyber events database, published dashboard {f.source_page}"),
                    ("Snapshot id", pub["snapshot_id"]), ("Snapshot sha-256", pub.get("snapshot_sha256")),
                    ("Published at", pub["published_at"]),
                    ("Reporting period", f.period or f"{period.start_iso} to {period.end_iso}"),
                    ("Code version at publication", pub.get("code_version"))]

        print()
        for k, f in enumerate(figures, start=1):
            nn = f"{k:02d}"
            save_figure(f.draw(), out_dir / f"figure{nn}.png", out_dir / f"figure{nn}.pdf")
            (out_dir / f"figure{nn}-description.txt").write_text(f.description + "\n", encoding="utf-8")
            (out_dir / f"figure{nn}-data-dictionary.txt").write_text(dictionary_text(f, nn), encoding="utf-8")
            xlsx = out_dir / f"figure{nn}-data.xlsx"
            write_xlsx(xlsx, f, nn, provenance_of(f), stamp)
            checked = check_totals(xlsx, f)
            print(f"figure{nn}: totals match ({'; '.join(f'{c} {e:g}' for c, e, _ in checked)})")
        print("\nCross-checks:")
        for line in cross_checks(data):
            print(f"  {line}")
        files = sorted(p.name for p in out_dir.iterdir() if p.is_file())
        if len(files) != 5 * len(figures):
            raise ExportError(f"{out_dir} holds {len(files)} files, expected {5 * len(figures)}")
        print(f"\nWrote {len(files)} files ({len(figures)} figures x 5) to {out_dir}")
        return 0
    except ExportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
