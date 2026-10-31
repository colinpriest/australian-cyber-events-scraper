"""Build the multi-customer supplier attack report (``dashboard/supplier_incidents.html``).

Every attack on a known vendor that affected more than one of the vendor's customers
(:mod:`cyber_data_collector.dedup.multi_customer_incidents`) - the 2026 Canvas attack on Instructure and the
universities it reached - ordered by vendor name, date of attack and customer name, with a brief description of
each attack. Published with the other dashboards.

Usage:
    python scripts/build_supplier_incidents_report.py [--db PATH] [--out dashboard/supplier_incidents.html]
"""

from __future__ import annotations

import argparse
import html
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.dedup import multi_customer_incidents as mci  # noqa: E402

PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vendor attacks affecting several customers</title>
<style>
  :root {{ color-scheme: light dark; --bg: #f6f7f9; --card: #fff; --ink: #16181d; --muted: #667; --line: #dfe3e8; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #15171b; --card: #1d2026; --ink: #e6e8ec; --muted: #9aa1ab; --line: #2e333b; }} }}
  * {{ box-sizing: border-box; }}
  body {{ font: 15px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 24px 16px;
         background: var(--bg); color: var(--ink); }}
  main {{ max-width: 1100px; margin: 0 auto; }}
  h1 {{ font-size: 21px; margin: 0 0 4px; }}
  .sub {{ color: var(--muted); font-size: 13px; margin-bottom: 18px; }}
  .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 8px; margin-bottom: 14px;
           padding: 12px 16px; }}
  .vendor {{ font-weight: 700; font-size: 16px; }}
  .meta {{ font-size: 12px; color: var(--muted); }}
  .desc {{ font-size: 14px; margin: 6px 0 8px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13.5px; }}
  th, td {{ text-align: left; padding: 5px 8px; border-top: 1px solid var(--line); vertical-align: top; }}
  th {{ font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }}
  code {{ font-size: 11.5px; word-break: break-all; }}
  @media (max-width: 640px) {{ .ev {{ display: none; }} }}
</style>
<main>
<h1>Vendor attacks affecting several customers</h1>
<div class="sub">{intro}</div>
{cards}
</main>
"""


def build_html(incidents: List[mci.Incident], generated: str) -> str:
    cards = []
    for inc in incidents:
        rows = "".join(
            f"<tr><td>{html.escape(c.name)}"
            + (f'<br><span class="meta">also listed as {html.escape(", ".join(c.also))}</span>' if c.also else "")
            + "</td>"
            f'<td class="ev">{html.escape(c.event_title or "named in the reporting; no event of its own")}'
            + (f'<br><code>{html.escape(c.event_id)}</code>' if c.event_id else "")
            + "</td></tr>"
            for c in inc.customers)
        left_out = []
        if inc.outside:
            left_out.append(f"{len(inc.outside)} named organisation(s) outside Australia: "
                            + html.escape(", ".join(inc.outside)))
        if inc.unplaced:
            left_out.append(f"{len(inc.unplaced)} named organisation(s) whose name or jurisdiction is not yet checked: "
                            + html.escape(", ".join(inc.unplaced)))
        description = inc.description or "No description yet: written by the next supplier step."
        if inc.description and not inc.description_current:
            description += " (written from older evidence)"
        note = (f'<div class="meta" style="margin-top:6px">Not listed: {"; ".join(left_out)}.</div>'
                if left_out else "")
        cards.append(
            f'<div class="card"><div class="vendor">{html.escape(inc.vendor)}</div>'
            f'<div class="meta">attack dated {html.escape(mci.attack_label(inc))} &middot; '
            f"{len(inc.customers)} Australian customers affected</div>"
            f'<div class="desc">{html.escape(description)}</div>'
            f"<table><tr><th>Customer</th><th class=\"ev\">Customer's event</th></tr>{rows}"
            f"</table>{note}</div>")
    intro = (f"{len(incidents)} attack(s) on a known vendor that affected more than one of the vendor's Australian "
             "customers, ordered by vendor name, date of attack and customer name. Each attack has one date: the "
             "vendor's own event's date, else the earliest of its customers' events. A customer is listed once "
             "whether it has its own event or is only named in the reporting; one only named is listed when its "
             "researched jurisdiction is Australian. Other names of a customer, and parts of it (a directorate of "
             "a government), are shown with it. The vendor itself, descriptions such as \"Australian charities\" "
             f"and lists of organisations are not counted. Generated {html.escape(generated)}.")
    return PAGE.format(intro=intro, cards="\n".join(cards) or "<p>No vendor attack with several customers.</p>")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default="instance/cyber_events.db")
    ap.add_argument("--out", default="dashboard/supplier_incidents.html")
    args = ap.parse_args(argv)
    conn = sqlite3.connect(args.db)
    try:
        incidents = mci.find(conn)
    finally:
        conn.close()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(incidents, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")), encoding="utf-8")
    print(f"{len(incidents)} vendor attack(s) with several customers -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
