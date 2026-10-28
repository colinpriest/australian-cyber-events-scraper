"""Write dashboard/ai_incidents.html: every incident that involved AI, oldest first.

Lists the active incidents in the dashboard window (2020 to date) whose
``ai_involvement`` is AI-attacker or AI-vulnerability, with date, victim(s),
category (with Jev's P(AI), or "labelled") and a 2-4 sentence description. Labels
and descriptions are written by ``scripts/classify_ai_involvement.py`` (a
pipeline step); this page only reads them, so it makes no model calls. An
incident with no stored description says so on the page and in the log.

The main dashboard build writes this page next to index.html, so both are
published together. To build it alone, from the project root:
    python scripts/build_ai_incident_report.py [--db-path PATH] [--out-dir DIR]
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import date
from html import escape as html_escape
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cyber_data_collector.processing import ai_involvement as ai  # noqa: E402
from cyber_data_collector.reporting.population import ReportingPeriod, count_incidents, dashboard_period  # noqa: E402

logger = logging.getLogger(__name__)

REPORT_FILE = "ai_incidents.html"
CATEGORY_TEXT = {ai.AI_ATTACKER: "AI attacker", ai.AI_VULNERABILITY: "AI vulnerability",
                 ai.AI_UNCERTAIN: "AI - unsettled"}
BADGE = {ai.AI_ATTACKER: "attacker", ai.AI_VULNERABILITY: "vulnerability", ai.AI_UNCERTAIN: "uncertain"}
NO_DESCRIPTION = "Description not generated yet (run scripts/classify_ai_involvement.py)."


def _unjudged(conn: sqlite3.Connection, period: ReportingPeriod) -> int:
    """Active incidents in the period with no AI-involvement label."""
    from cyber_data_collector.reporting.population import incident_filter

    if not ai.has_schema(conn):
        return count_incidents(conn, period)
    where, params = incident_filter(period, "de")
    return int(conn.execute(f"SELECT COUNT(*) FROM DeduplicatedEvents de WHERE {where} "
                            "AND de.ai_involvement IS NULL", params).fetchone()[0])


def build_report_html(conn: sqlite3.Connection, period: Optional[ReportingPeriod] = None,
                      built_on: Optional[date] = None) -> str:
    """The report page for ``period`` (default: the dashboard window)."""
    period = period or dashboard_period()
    incidents = ai.ai_incidents(conn, period)
    total = count_incidents(conn, period)
    unjudged = _unjudged(conn, period)
    counts = {label: sum(1 for i in incidents if i["ai_involvement"] == label) for label in ai.AI_INVOLVED_LABELS}
    missing = [i["deduplicated_event_id"] for i in incidents if not i["description"]]
    if missing:
        logger.warning("AI incident report: %d incident(s) have no description yet: %s",
                       len(missing), ", ".join(m[:8] for m in missing[:10]))
    if unjudged:
        logger.warning("AI incident report: %d of %d incident(s) have no AI-involvement judgement",
                       unjudged, total)

    rows: List[str] = []
    for item in incidents:
        when = html_escape((item["event_date"] or "undated")[:10])
        victims = "<br>".join(html_escape(v) for v in item["victims"]) or '<span class="muted">not recorded</span>'
        category = html_escape(CATEGORY_TEXT[item["ai_involvement"]])
        if item.get("ai_involvement_basis") == ai.BASIS_LABEL:
            prob_text = '<div class="muted">labelled</div>'
        elif item.get("p_ai") is not None:
            prob_text = f'<div class="muted">P(AI) = {item["p_ai"]:.2f}</div>'
        else:
            prob_text = ""
        badge = BADGE[item["ai_involvement"]]
        if item.get("ai_involvement_basis") and item["ai_involvement"] == ai.AI_UNCERTAIN:
            prob_text += f'<div class="muted">{html_escape(item["ai_involvement_basis"])}</div>'
        if item["description"]:
            description = html_escape(item["description"])
        else:
            description = f'<span class="missing">{html_escape(NO_DESCRIPTION)}</span>'
        rows.append(
            f'<tr data-category="{badge}"><td class="nowrap">{when}</td><td>{victims}</td>'
            f'<td><span class="badge {badge}">{category}</span>{prob_text}</td>'
            f'<td><div class="title">{html_escape(item["title"] or "")}</div>{description}</td></tr>')
    body = "\n".join(rows) or '<tr><td colspan="4" class="muted">No incidents with AI involvement.</td></tr>'

    notes = [f"{len(incidents)} of {total} incidents dated {period.start_iso} to {period.end_iso} involved AI: "
             f"{counts[ai.AI_ATTACKER]} with AI as the attacker, "
             f"{counts[ai.AI_VULNERABILITY]} with the victim's AI attacked, "
             f"{counts[ai.AI_UNCERTAIN]} where the evidence does not settle whether or how AI was involved "
             "(listed, not counted as either)."]
    if unjudged:
        notes.append(f'<span class="missing">{unjudged} incident(s) have not been judged yet and are not '
                     "listed.</span>")
    if missing:
        notes.append(f'<span class="missing">{len(missing)} listed incident(s) have no description yet.</span>')
    note_html = " ".join(notes)
    built = (built_on or date.today()).isoformat()
    calibration = ai.active_calibration()
    if calibration:
        at = calibration.get("at_cutoff") or {}
        method_html = html_escape(
            f"Listed when Jev's probability that AI was involved, P(AI), judged from the news sources' full "
            f"text, reaches {calibration['cutoff']:.2f} - the cut-off at which "
            f"{at.get('correct_labelled')} of {at.get('flagged_labelled')} blind-labelled events were "
            f"really AI incidents (weighted precision {at.get('weighted_precision', 0):.2f}). The category is "
            "the more probable of attacker and vulnerability; labelled events show their label.")
    else:
        method_html = html_escape(
            "Category is Jev's most probable of three (AI attacker, AI vulnerability, no AI) from the news "
            "sources' full text; P(AI) is 1 minus the probability of no AI. NOT CALIBRATED: no labelled cut-off "
            "matches the current model and question.")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>AI Cyber Incidents</title>
<style>
  :root {{ --bg: #f8f9fa; --card: #ffffff; --text: #1f2937; --muted: #6b7280; --line: #e5e7eb;
          --attacker: #b91c1c; --attacker-bg: #fee2e2; --vuln: #1d4ed8; --vuln-bg: #dbeafe; --warn: #b45309; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #111827; --card: #1f2937; --text: #e5e7eb; --muted: #9ca3af; --line: #374151;
            --attacker: #fca5a5; --attacker-bg: #450a0a; --vuln: #93c5fd; --vuln-bg: #172554; --warn: #fbbf24; }}
  }}
  body {{ margin: 0; background: var(--bg); color: var(--text);
         font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
  header {{ background: linear-gradient(135deg, #2563eb, #1e40af); color: #fff; padding: 1.5rem 16px; }}
  header h1 {{ margin: 0 0 .25rem; font-size: 1.6rem; }}
  header p {{ margin: 0; color: #e5e7eb; font-size: .9rem; }}
  header a {{ color: #fff; }}
  main {{ max-width: 1200px; margin: 0 auto; padding: 1.25rem 16px 3rem; }}
  .summary {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: .9rem 1rem;
              margin-bottom: 1rem; }}
  .filters {{ margin: .5rem 0 0; display: flex; gap: 1rem; flex-wrap: wrap; font-size: .9rem; }}
  .table-wrap {{ overflow-x: auto; background: var(--card); border: 1px solid var(--line); border-radius: 10px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; vertical-align: top; padding: .6rem .75rem; border-bottom: 1px solid var(--line); }}
  th {{ font-size: .8rem; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }}
  td.nowrap {{ white-space: nowrap; }}
  .title {{ font-weight: 600; margin-bottom: .2rem; }}
  .badge {{ display: inline-block; padding: .1rem .5rem; border-radius: 999px; font-size: .8rem; font-weight: 600;
            white-space: nowrap; }}
  .badge.attacker {{ color: var(--attacker); background: var(--attacker-bg); }}
  .badge.vulnerability {{ color: var(--vuln); background: var(--vuln-bg); }}
  .badge.uncertain {{ color: var(--muted); background: var(--line); }}
  .muted {{ color: var(--muted); font-size: .85rem; }}
  .missing {{ color: var(--warn); }}
</style>
</head>
<body>
<header>
  <h1>AI Cyber Incidents</h1>
  <p>Australian cyber incidents in which AI was the attacker, or the victim's AI was attacked.
     Built {html_escape(built)}. <a href="index.html">Main dashboard</a></p>
</header>
<main>
  <div class="summary">
    <div>{note_html}</div>
    <div class="muted">{method_html}</div>
    <div class="filters">
      <label><input type="checkbox" data-filter="attacker" checked> AI attacker</label>
      <label><input type="checkbox" data-filter="vulnerability" checked> AI vulnerability</label>
      <label><input type="checkbox" data-filter="uncertain" checked> Unsettled</label>
    </div>
  </div>
  <div class="table-wrap">
  <table>
    <thead><tr><th>Date</th><th>Victim</th><th>AI category</th><th>Description</th></tr></thead>
    <tbody>
{body}
    </tbody>
  </table>
  </div>
</main>
<script>
  document.querySelectorAll('input[data-filter]').forEach(function (box) {{
    box.addEventListener('change', function () {{
      var shown = {{}};
      document.querySelectorAll('input[data-filter]').forEach(function (b) {{ shown[b.dataset.filter] = b.checked; }});
      document.querySelectorAll('tbody tr[data-category]').forEach(function (row) {{
        row.style.display = shown[row.dataset.category] ? '' : 'none';
      }});
    }});
  }});
</script>
</body>
</html>
"""


def write_report(conn: sqlite3.Connection, out_dir: str, period: Optional[ReportingPeriod] = None) -> str:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / REPORT_FILE
    path.write_text(build_report_html(conn, period), encoding="utf-8")
    logger.info("AI incident report generated: %s", path)
    return str(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", default="instance/cyber_events.db")
    parser.add_argument("--out-dir", default="dashboard")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not Path(args.db_path).exists():
        print(f"ERROR: database not found: {args.db_path}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{Path(args.db_path).as_posix()}?mode=ro", uri=True)
    try:
        print(f"AI incident report generated: {write_report(conn, args.out_dir)}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
