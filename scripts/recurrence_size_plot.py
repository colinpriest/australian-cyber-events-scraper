"""Recurrence rate by organisation size band, for ``dashboard/recurrence.html``.

A boxplot needs a distribution, and two exist here, so the figure has two panels:

* **Across organisations.** Each organisation's own rate: its repeats over its follow-up. Most organisations have no
  repeat, so in most bands at least three quarters of them are at zero and the box is flat; those with a repeat are
  drawn as dots, because the box alone would hide them.
* **Each band's overall rate.** The band's repeats over its follow-up, which is what the size question is about, with
  the distribution of that rate when organisations are resampled (the box is the middle 50% of resamples, the whiskers
  the middle 95%). This box shows uncertainty about the rate, not differences between organisations.

Both are unadjusted. The adjusted multipliers and the test of the size effect are in the executive summary
(``scripts/recurrence_summary.py``). UNKNOWN has no place in an order of sizes, so it is counted and not plotted.

It reads the main run's ``recurrent_event_spells.csv`` and never touches the database.
"""

from __future__ import annotations

import base64
import html
import io
from typing import Any, Dict, List, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

SIZE_BANDS = ("SMALL", "MEDIUM", "LARGE", "HUGE")
DAYS_PER_YEAR = 365.25
#: Rates are per 100 organisation-years, the unit of every other rate in the reports.
RATE_UNIT = 100.0
BOOTSTRAP_REPLICATES = 4000
#: Fixed so that rebuilding the page from the same analysis draws the same figure.
BOOTSTRAP_SEED = 20261106
#: Below this the left panel's axis is linear, above it logarithmic: it has to show 0 and 400 together.
LINEAR_THRESHOLD = 10.0
INK = "#1d1d1b"
MUTED = "#6b6b66"
HAIRLINE = "#deded8"
ACCENT = "#2f5d8a"
ACCENT_FILL = "#c9d9e8"


def organisation_rates(spells: pd.DataFrame) -> pd.DataFrame:
    """One row per organisation with follow-up: its size band, repeats, years followed and rate."""
    frame = spells.assign(size=spells["org_size"].fillna("UNKNOWN").astype(str).str.upper())
    orgs = frame.groupby("entity_id", as_index=False).agg(
        size=("size", "first"), repeats=("event", "sum"), days=("duration_days", "sum"))
    orgs = orgs[orgs["days"] > 0].copy()
    orgs["years"] = orgs["days"] / DAYS_PER_YEAR
    orgs["rate"] = orgs["repeats"] / orgs["years"] * RATE_UNIT
    return orgs


def bootstrap_rates(orgs: pd.DataFrame, replicates: int = BOOTSTRAP_REPLICATES,
                    seed: int = BOOTSTRAP_SEED) -> Dict[str, np.ndarray]:
    """Each band's overall rate over organisations resampled with replacement within the band."""
    rng = np.random.default_rng(seed)
    out: Dict[str, np.ndarray] = {}
    for band in SIZE_BANDS:
        group = orgs[orgs["size"] == band]
        count = len(group)
        if not count:
            out[band] = np.array([])
            continue
        draws = rng.multinomial(count, np.full(count, 1.0 / count), size=replicates)
        out[band] = (draws @ group["repeats"].to_numpy(dtype=float)
                     / (draws @ group["years"].to_numpy(dtype=float)) * RATE_UNIT)
    return out


def band_table(orgs: pd.DataFrame, resampled: Dict[str, np.ndarray]) -> List[Dict[str, Any]]:
    """The numbers behind both panels, one row per size band."""
    rows: List[Dict[str, Any]] = []
    for band in SIZE_BANDS:
        group = orgs[orgs["size"] == band]
        if group.empty:
            continue
        rates = resampled[band]
        rows.append({
            "band": band,
            "organisations": int(len(group)),
            "with_repeat": int((group["repeats"] > 0).sum()),
            "repeats": int(group["repeats"].sum()),
            "years": float(group["years"].sum()),
            "rate": float(group["repeats"].sum() / group["years"].sum() * RATE_UNIT),
            "low": float(np.percentile(rates, 2.5)),
            "high": float(np.percentile(rates, 97.5)),
            "org_q3": float(group["rate"].quantile(0.75)),
        })
    return rows


def _style(ax: Any) -> None:
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(HAIRLINE)
    ax.grid(axis="y", color=HAIRLINE, linewidth=0.7)
    ax.set_axisbelow(True)
    ax.tick_params(colors=MUTED, length=0)
    ax.title.set_color(INK)
    ax.yaxis.label.set_color(MUTED)


def _box(ax: Any, data: Sequence[np.ndarray], whis: Any) -> None:
    ax.boxplot(data, positions=range(len(data)), widths=0.5, whis=whis, showfliers=False, patch_artist=True,
               boxprops={"facecolor": ACCENT_FILL, "edgecolor": ACCENT, "linewidth": 1},
               medianprops={"color": ACCENT, "linewidth": 2},
               whiskerprops={"color": ACCENT, "linewidth": 1},
               capprops={"color": ACCENT, "linewidth": 1})


def plot_png(orgs: pd.DataFrame, resampled: Dict[str, np.ndarray], rows: List[Dict[str, Any]]) -> bytes:
    """The two-panel figure as PNG bytes."""
    bands = [row["band"] for row in rows]
    fig, (left, right) = plt.subplots(1, 2, figsize=(11, 4.8))

    across = [orgs.loc[orgs["size"] == band, "rate"].to_numpy(dtype=float) for band in bands]
    _box(left, across, 1.5)
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    for position, rates in enumerate(across):
        repeated = rates[rates > 0]
        left.scatter(position + rng.uniform(-0.2, 0.2, len(repeated)), repeated, s=14, color=ACCENT, alpha=0.55,
                     linewidths=0, zorder=3)
    left.set_yscale("symlog", linthresh=LINEAR_THRESHOLD)
    top = max((float(r.max()) for r in across if len(r)), default=LINEAR_THRESHOLD)
    left.set_ylim(-1.5, top * 1.4)
    ticks = [t for t in (0, 10, 25, 50, 100, 200, 400) if t <= top * 1.4]
    left.set_yticks(ticks)
    left.set_yticklabels([str(t) for t in ticks])
    left.minorticks_off()
    left.set_xticks(range(len(bands)))
    left.set_xticklabels([f"{row['band']}\n{row['with_repeat']} of {row['organisations']}\nwith a repeat" for row in rows])
    left.set_ylabel("Repeats per 100 years of follow-up")
    left.set_title("Across organisations (dot = one with a repeat)", fontsize=11, loc="left")

    _box(right, [resampled[band] for band in bands], (2.5, 97.5))
    highest = max(row["high"] for row in rows)
    for position, row in enumerate(rows):
        right.text(position, row["high"] + highest * 0.02, f"{row['rate']:.1f}", ha="center", va="bottom", color=INK,
                   fontsize=10)
    right.set_ylim(0, highest * 1.12)
    right.set_xticks(range(len(bands)))
    right.set_xticklabels([f"{row['band']}\n{row['repeats']} repeats" for row in rows])
    right.set_ylabel("Repeats per 100 organisation-years")
    right.set_title("Each band's overall rate (box = resampling uncertainty)", fontsize=11, loc="left")

    _style(left)
    _style(right)
    fig.tight_layout()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=160, facecolor="white")
    plt.close(fig)
    return buffer.getvalue()


def section_html(spells: pd.DataFrame) -> str:
    """The page's section: the figure, how to read it and the table of its numbers."""
    orgs = organisation_rates(spells)
    resampled = bootstrap_rates(orgs)
    rows = band_table(orgs, resampled)
    if not rows:
        raise ValueError("No organisation with a known size and follow-up in the recurrence spells.")
    encoded = base64.b64encode(plot_png(orgs, resampled, rows)).decode("ascii")
    alt = "Recurrence rate by organisation size: " + ", ".join(
        f"{row['band']} {row['rate']:.1f} per 100 organisation-years" for row in rows)
    body = "".join(
        f"<tr><td>{html.escape(row['band'])}</td><td>{row['organisations']}</td><td>{row['with_repeat']}</td>"
        f"<td>{row['repeats']}</td><td>{row['years']:,.0f}</td><td>{row['rate']:.1f}</td>"
        f"<td>{row['low']:.1f} to {row['high']:.1f}</td><td>{row['org_q3']:.1f}</td></tr>"
        for row in rows)
    flat = [row["band"] for row in rows if row["org_q3"] == 0]
    flat_note = (f" In {', '.join(flat)} at least three quarters of organisations have no repeat, so the box sits flat "
                 "on zero." if flat else "")
    unknown = orgs[orgs["size"] == "UNKNOWN"]
    return (
        '<h2>Organisation size and recurrence rate</h2>\n'
        f'<figure><img alt="{html.escape(alt)}" src="data:image/png;base64,{encoded}"></figure>\n'
        '<p class="muted"><strong>Left:</strong> each organisation\'s own rate, its repeats over its follow-up. The box '
        'is the middle half of organisations and the whiskers reach 1.5 box-heights. The axis is logarithmic above 10. '
        f'One repeat in a short follow-up gives a very high rate, so the highest dots say little.{html.escape(flat_note)} '
        "<strong>Right:</strong> each band's overall rate, its repeats over its follow-up (the number above each box). "
        'The box and whiskers are the middle 50% and 95% of that rate when the band\'s organisations are resampled, so '
        'they show how sure the rate is, not how organisations differ.</p>\n'
        '<p class="muted">These rates are unadjusted, and every organisation here has at least one recorded incident. '
        'Size also stands in for visibility, attack exposure and the value of targets, which are not measured '
        'separately. The adjusted effect and its test are in the size answer above. '
        f'Left out: {len(unknown)} organisations of unknown size, with {int(unknown["repeats"].sum())} repeats.</p>\n'
        '<div class="scroll"><table><thead><tr><th>Size band</th><th>Organisations</th><th>With a repeat</th>'
        '<th>Repeats</th><th>Years of follow-up</th><th>Rate per 100 organisation-years</th><th>95% interval</th>'
        '<th>Upper quartile of organisations\' rates</th></tr></thead>'
        f'<tbody>{body}</tbody></table></div>\n'
    )
