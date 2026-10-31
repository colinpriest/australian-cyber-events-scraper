"""Build the close-pairs exception report (``dashboard/close_pairs.html``).

Every pair of Active events of one organisation that carry the same stored date
or whose possible incident periods overlap (:mod:`cyber_data_collector.dedup.close_pairs`),
with what the duplicate check decided on the pair's current evidence and what
stopped a merge. Two separate incidents at one organisation this close together
are rare, so each row is an exception the pipeline reports, not an accepted
"tie": it is either a duplicate the merging steps missed or a pair the evidence
shows to be two incidents.

Usage:
    python scripts/build_close_pairs_report.py [--db PATH] [--out dashboard/close_pairs.html]
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

from cyber_data_collector.dedup import close_pairs  # noqa: E402

PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Close-together incident pairs</title>
<style>
  :root {{ color-scheme: light dark; --bg: #f6f7f9; --card: #fff; --ink: #16181d; --muted: #667;
           --line: #dfe3e8; --warn: #8a5312; --warnbg: #fff3e0; --ok: #2f6b3d; --okbg: #e8f4ea; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #15171b; --card: #1d2026; --ink: #e6e8ec; --muted: #9aa1ab; --line: #2e333b;
             --warn: #f0c27a; --warnbg: #3a2c16; --ok: #9fd3a9; --okbg: #1f3324; }} }}
  * {{ box-sizing: border-box; }}
  body {{ font: 15px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 24px 16px;
         background: var(--bg); color: var(--ink); }}
  main {{ max-width: 1100px; margin: 0 auto; }}
  h1 {{ font-size: 21px; margin: 0 0 4px; }}
  .sub {{ color: var(--muted); font-size: 13px; margin-bottom: 18px; }}
  .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 8px; margin-bottom: 14px;
           padding: 12px 16px; }}
  .org {{ font-weight: 700; font-size: 16px; }}
  .rel {{ font-size: 11px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; color: var(--muted); }}
  .pair {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 8px 0; }}
  .side {{ border: 1px solid var(--line); border-radius: 6px; padding: 8px 10px; min-width: 0; }}
  .t {{ font-size: 14px; }}
  .m {{ font-size: 12px; color: var(--muted); }}
  .why {{ font-size: 13px; background: var(--warnbg); color: var(--warn); border-radius: 6px; padding: 6px 10px; }}
  .lab {{ font-size: 13px; background: var(--okbg); color: var(--ok); border-radius: 6px; padding: 6px 10px; }}
  code {{ font-size: 11.5px; word-break: break-all; }}
  @media (max-width: 760px) {{ .pair {{ grid-template-columns: 1fr; }} }}
</style>
<main>
<h1>Close-together incident pairs</h1>
<div class="sub">{intro}</div>
{cards}
</main>
"""


def _side(s: close_pairs.Side) -> str:
    period = f"{s.period[0]} to {s.period[1]}" if s.period else "undated"
    return (f'<div class="side"><div class="t">{html.escape(s.title or "")}</div>'
            f'<div class="m">{html.escape(str(s.event_date or "undated"))} &middot; possible period {period} '
            f'({html.escape(s.basis)}) &middot; {s.records} record(s)</div>'
            + (f'<div class="m">Supplier: {html.escape(s.supplier)}</div>' if s.supplier else "") +
            f'<div class="m"><code>{html.escape(s.event_id)}</code></div></div>')


def build_html(pairs: List[close_pairs.ClosePair], generated: str) -> str:
    cards = []
    for p in pairs:
        score = "no decision on current evidence" if p.score is None else f"{p.score:.2f}"
        cut = "" if p.cutoff is None else f" (merge cut-off {p.cutoff:.2f})"
        label = f'<div class="lab">Duplicate label: {html.escape(p.label)}</div>' if p.label else ""
        why = "".join(f'<div class="why">{html.escape(b)}</div>' for b in p.blockers)
        cards.append(f'<div class="card"><div class="org">{html.escape(p.organisation)}</div>'
                     f'<div class="rel">{html.escape(p.relation)} &middot; duplicate check {score}{cut}</div>'
                     f'<div class="pair">{_side(p.a)}{_side(p.b)}</div>{label}{why}</div>')
    intro = (f"{len(pairs)} pair(s) of Active events of one organisation carry the same stored date or have "
             "overlapping possible incident periods (a date stored as the 1st with unverified precision counts as "
             "its whole month). Two separate incidents at one organisation this close together are rare: each "
             "pair is a duplicate the merging steps missed or a pair the evidence shows to be two incidents. The "
             f"reason it is still two events is under each pair. Generated {html.escape(generated)}.")
    return PAGE.format(intro=intro, cards="\n".join(cards) or "<p>No close-together pairs.</p>")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default="instance/cyber_events.db")
    ap.add_argument("--out", default="dashboard/close_pairs.html")
    args = ap.parse_args(argv)
    conn = sqlite3.connect(args.db)
    try:
        pairs = close_pairs.find(conn)
    finally:
        conn.close()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(pairs, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")), encoding="utf-8")
    print(f"{len(pairs)} close pair(s) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
