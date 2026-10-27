"""Build the repeat-event timing dashboard.

``analyze_recurrent_timing.py`` writes one folder per elapsed-time banding -
a Markdown report, CSVs and diagnostic plots - and the bandings are only
interesting side by side: whether a delayed early peak is real depends on how
the first six months are cut. This page puts every run in one self-contained
HTML file: an overview of the headline tests across runs, then each run's full
report with its plots embedded inline.

It reads the analysis outputs only; it never touches the database, so it is
cheap to rebuild after rerunning the analysis.

Usage:
    python scripts/build_recurrence_dashboard.py
    python scripts/build_recurrence_dashboard.py --out dashboard/recurrence.html
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import logging
import re
from datetime import datetime
from pathlib import Path

import markdown

from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_OUT = "dashboard/recurrence.html"

# (folder under analysis/, tab label). Order is the reading order on the page.
RUNS = (
    ("recurrent_timing", "Main: 0-180 / 181-365 / 366-730 / 731-1460 / >1460"),
    ("recurrent_timing_90_180_peak", "Delayed peak: 0-90 / 91-180 / 181-365 / ..."),
    ("recurrent_timing_90d", "90-day first band: 0-90 / 91-365 / ..."),
)

PLOT_LINE = re.compile(r"^- (?P<label>[^:]+): `(?P<file>[\w.-]+\.png)`$", re.MULTILINE)


def _image_tag(path: Path, label: str) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return (
        f'<figure><img alt="{html.escape(label)}" src="data:image/png;base64,{encoded}">'
        f"<figcaption>{html.escape(label)}</figcaption></figure>"
    )


def render_report(run_dir: Path) -> str:
    """Render a run's Markdown report, replacing plot filenames with the plots."""
    text = (run_dir / "recurrent_timing_report.md").read_text(encoding="utf-8")
    # Drop the report's own H1; the tab label names the run.
    text = re.sub(r"\A# .*\n", "", text)
    figures: Dict[str, str] = {}

    def _swap(match: re.Match) -> str:
        png = run_dir / match.group("file")
        if not png.exists():
            return match.group(0)
        key = f"FIGURE{len(figures)}PLACEHOLDER"
        figures[key] = _image_tag(png, match.group("label"))
        return f"\n{key}\n"

    text = PLOT_LINE.sub(_swap, text)
    body = markdown.markdown(text, extensions=["tables"])
    for key, tag in figures.items():
        body = body.replace(f"<p>{key}</p>", tag)
    return body


def _p(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _contrast(summary: Dict[str, Any], comparison: str) -> str:
    for row in summary.get("delayed_peak_sensitivity_adjusted_contrasts") or []:
        if row.get("comparison") == comparison:
            return f"{row['rate_ratio']:.2f}x ({row['ci_low']:.2f}-{row['ci_high']:.2f})"
    return "n/a"


def overview_rows(analysis_dir: Path) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for folder, label in RUNS:
        path = analysis_dir / folder / "analysis_summary.json"
        if not path.exists():
            continue
        summary = json.loads(path.read_text(encoding="utf-8"))
        quality = summary.get("data_quality", {})
        rows.append({
            "run": label,
            "censor": str(quality.get("censor date", "")),
            "repeats": str(quality.get("observed repeat spells", "")),
            "unadjusted": _p((summary.get("unadjusted_piecewise_lrt") or {}).get("p_value")),
            "adjusted": _p((summary.get("adjusted_piecewise_lrt") or {}).get("p_value")),
            "weibull": _p((summary.get("weibull_vs_exponential_lrt") or {}).get("p_value")),
            "best_aic": str(summary.get("best_aic_model", "")),
        })
    return rows


def build_html(analysis_dir: Path) -> str:
    runs = [(f, label) for f, label in RUNS if (analysis_dir / f / "recurrent_timing_report.md").exists()]
    if not runs:
        raise FileNotFoundError(f"No recurrent timing reports under {analysis_dir}; run analyze_recurrent_timing.py first.")

    main_summary_path = analysis_dir / "recurrent_timing" / "analysis_summary.json"
    main_summary = json.loads(main_summary_path.read_text(encoding="utf-8")) if main_summary_path.exists() else {}

    overview = "".join(
        "<tr>" + "".join(f"<td>{html.escape(r[k])}</td>" for k in
                         ("run", "censor", "repeats", "unadjusted", "adjusted", "weibull", "best_aic")) + "</tr>"
        for r in overview_rows(analysis_dir)
    )
    tabs = "".join(
        f'<button class="tab{" active" if i == 0 else ""}" data-target="run{i}">{html.escape(label)}</button>'
        for i, (_, label) in enumerate(runs)
    )
    panels = "".join(
        f'<section class="panel{" active" if i == 0 else ""}" id="run{i}">{render_report(analysis_dir / folder)}</section>'
        for i, (folder, _) in enumerate(runs)
    )
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Repeat-Event Timing</title>
<style>
:root {{ --bg:#fbfbfa; --fg:#1d1d1b; --muted:#6b6b66; --line:#deded8; --accent:#2f5d8a; --card:#ffffff; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#161615; --fg:#ececea; --muted:#a3a39d; --line:#34342f; --accent:#8db4dc; --card:#1f1f1d; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:1100px; margin:0 auto; padding:24px 16px 64px; }}
h1 {{ font-size:1.6rem; margin:0 0 4px; }}
h2 {{ font-size:1.2rem; margin:32px 0 8px; border-bottom:1px solid var(--line); padding-bottom:4px; }}
.muted {{ color:var(--muted); }}
.scroll {{ overflow-x:auto; }}
table {{ border-collapse:collapse; font-size:13px; margin:8px 0 16px; font-variant-numeric:tabular-nums; }}
th, td {{ border-bottom:1px solid var(--line); padding:5px 10px; text-align:left; vertical-align:top; }}
th {{ color:var(--muted); font-weight:600; }}
.tabs {{ display:flex; flex-wrap:wrap; gap:6px; margin:24px 0 0; border-bottom:1px solid var(--line); }}
.tab {{ background:none; border:1px solid transparent; border-bottom:none; padding:8px 12px; color:var(--muted); font:inherit; cursor:pointer; border-radius:6px 6px 0 0; }}
.tab.active {{ color:var(--fg); border-color:var(--line); background:var(--card); }}
.panel {{ display:none; background:var(--card); border:1px solid var(--line); border-top:none; padding:8px 20px 20px; }}
.panel.active {{ display:block; }}
.panel table {{ display:block; overflow-x:auto; max-width:100%; }}
figure {{ margin:16px 0; }}
figure img {{ max-width:100%; height:auto; background:#fff; border:1px solid var(--line); }}
figcaption {{ color:var(--muted); font-size:13px; }}
code {{ font-size:0.9em; }}
</style>
</head>
<body>
<main>
<h1>Repeat cyber-event timing</h1>
<p class="muted">Is the risk of a further attack on the same organisation constant over time since the last one?
Generated {generated} from <code>{html.escape(str(analysis_dir))}</code>.</p>

<h2>Headline tests across bandings</h2>
<p class="muted">p-values test constant elapsed-time risk; the adjusted model controls for calendar period, prior event
number, organisation size, sector, entity kind and records affected.</p>
<div class="scroll"><table>
<thead><tr><th>Run</th><th>Censor date</th><th>Repeats</th><th>Unadjusted p</th><th>Adjusted p</th><th>Weibull vs exponential p</th><th>Best AIC</th></tr></thead>
<tbody>{overview}</tbody>
</table></div>
<p class="muted">Delayed-peak contrasts (adjusted rate ratio, 95% CI): 91-180 vs 0-90 days
{html.escape(_contrast(main_summary, "91-180 vs 0-90"))}; 91-180 vs post-180 days
{html.escape(_contrast(main_summary, "91-180 vs post-180"))}.</p>

<div class="tabs">{tabs}</div>
{panels}
</main>
<script>
document.querySelectorAll('.tab').forEach(function (btn) {{
  btn.addEventListener('click', function () {{
    document.querySelectorAll('.tab, .panel').forEach(function (el) {{ el.classList.remove('active'); }});
    btn.classList.add('active');
    document.getElementById(btn.dataset.target).classList.add('active');
  }});
}});
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--analysis-dir", default="analysis", help="Folder holding the recurrent_timing* run folders.")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output HTML path.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(Path(args.analysis_dir)), encoding="utf-8")
    logger.info("Recurrence dashboard written: %s", out.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
