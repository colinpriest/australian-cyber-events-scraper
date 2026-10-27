"""Build the organisation timeline dashboard.

For the organisations with the most events, shows the researched incident
timeline (``OrganisationTimelines``, see ``dedup/org_timeline.py``) beside the
events the database holds for that organisation, both in date order. Reading
them side by side is the point:

* an incident in the timeline with no matching event is a **coverage gap** -
  discovery never found it;
* an event matching no incident in the timeline is **suspect** - junk evidence,
  a wrong victim, or a misdated duplicate.

Each card shows when the timeline was researched, by which model, its sources,
and whether it is stale (a record about the organisation has arrived since).
The page is static and self-contained; no server required.

Usage:
    python scripts/build_timeline_dashboard.py
    python scripts/build_timeline_dashboard.py --top 30 --out dashboard/timelines.html
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.dedup import org_timeline  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_DB = "instance/cyber_events.db"
DEFAULT_OUT = "dashboard/timelines.html"
DEFAULT_TOP = 20

parse_timeline = org_timeline.parse_timeline


def load_reconciliation(path: Path) -> Dict[int, Dict[str, Any]]:
    """Reconciliation findings per entity id, from ``dedup_admin.py reconcile-timelines``."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {o["entity_id"]: o for o in data.get("organisations", [])}


def load(conn: sqlite3.Connection, top: int, reconciliation: Optional[Dict[int, Dict[str, Any]]] = None
         ) -> List[Dict[str, Any]]:
    """The ``top`` organisations by active victim events, with timeline, events and reconciliation."""
    reconciliation = reconciliation or {}
    org_timeline.ensure_schema(conn)
    has_size = any(r[1] == "size_estimate" for r in conn.execute("PRAGMA table_info(EntitiesV2)"))
    orgs = conn.execute(
        f"""SELECT v.entity_id, v.entity_name, {'v.size_estimate' if has_size else 'NULL'} AS size,
                   COUNT(DISTINCT d.deduplicated_event_id) AS n
            FROM DeduplicatedEventEntities dee
            JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
            JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id
            WHERE dee.relationship_type = 'victim' AND d.status = 'Active'
            GROUP BY v.entity_id ORDER BY n DESC, v.entity_name LIMIT ?""", (top,)).fetchall()
    out = []
    for entity_id, name, size, n in orgs:
        events = conn.execute(
            """SELECT d.event_date, d.title, d.deduplicated_event_id,
                      (SELECT COUNT(*) FROM EventDeduplicationMap m
                        WHERE m.deduplicated_event_id = d.deduplicated_event_id)
               FROM DeduplicatedEventEntities dee
               JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id
               WHERE dee.entity_id = ? AND dee.relationship_type = 'victim' AND d.status = 'Active'
               ORDER BY d.event_date""", (entity_id,)).fetchall()
        timeline = org_timeline.stored(conn, entity_id)
        rec = reconciliation.get(entity_id, {})
        verdicts = {e["deduplicated_event_id"]: e for e in rec.get("events", [])}
        conflicts = {c["deduplicated_event_id"] for c in rec.get("date_conflicts", [])}
        gaps = {g["incident"]["number"] for g in rec.get("gaps", [])}
        out.append({
            "name": name, "size": size or "UNKNOWN", "events": [
                {"date": e[0] or "", "title": e[1] or "", "records": e[3], "verdict": verdicts.get(e[2]),
                 "date_conflict": e[2] in conflicts} for e in events],
            "gaps": gaps, "reconciled": bool(rec),
            "timeline": timeline,
            "incidents": parse_timeline(timeline["timeline"]) if timeline else [],
            "stale": org_timeline.is_stale(conn, entity_id) if timeline else None,
            "event_count": n,
        })
    return out


PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Organisation incident timelines</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font: 15px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
         margin: 0; padding: 24px; background: #f6f7f9; color: #16181d; }}
  h1 {{ font-size: 21px; margin: 0 0 4px; }}
  .sub {{ color: #667; margin-bottom: 18px; font-size: 13px; max-width: 980px; }}
  .bar {{ display: flex; gap: 14px; align-items: center; flex-wrap: wrap; background: #fff;
          border: 1px solid #dfe3e8; border-radius: 8px; padding: 12px 14px; margin-bottom: 16px;
          position: sticky; top: 0; z-index: 5; }}
  input[type=search] {{ flex: 1 1 260px; min-width: 200px; padding: 7px 10px;
      border: 1px solid #ccd2d9; border-radius: 6px; font-size: 14px; }}
  .stat {{ font-size: 13px; color: #667; margin-left: auto; }}
  .card {{ background: #fff; border: 1px solid #dfe3e8; border-radius: 8px; margin-bottom: 18px; }}
  .head {{ display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; padding: 12px 16px;
           border-bottom: 1px solid #eef0f3; }}
  .head h2 {{ font-size: 17px; margin: 0; }}
  .head .meta {{ color: #8a8f98; font-size: 12px; }}
  .badge {{ font-size: 10.5px; font-weight: 700; letter-spacing: .05em; padding: 2px 6px;
            border-radius: 4px; border: 1px solid #dfe3e8; color: #7b818b; background: #eef0f3; }}
  .stale {{ background: #fff3e0; color: #8a5312; border-color: #f0dcc2; }}
  .fresh {{ background: #e8f4ea; color: #2f6b3d; border-color: #cbe3d1; }}
  .cols {{ display: grid; grid-template-columns: 1fr 1fr; }}
  .col {{ padding: 12px 16px; min-width: 0; }}
  .col + .col {{ border-left: 1px solid #eef0f3; }}
  .col h3 {{ font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: #667;
             margin: 0 0 8px; }}
  ol {{ margin: 0; padding-left: 0; list-style: none; }}
  li {{ padding: 7px 0; border-bottom: 1px solid #f0f2f5; }}
  li:last-child {{ border-bottom: 0; }}
  .date {{ font-variant-numeric: tabular-nums; color: #444; font-size: 13px; font-weight: 600; }}
  .what {{ font-size: 13.5px; }}
  .public {{ color: #8a8f98; font-size: 12px; }}
  a {{ color: #2b5a86; word-break: break-all; font-size: 12px; }}
  .none {{ color: #889; font-size: 13px; padding: 6px 0; }}
  details {{ padding: 0 16px 12px; font-size: 12px; color: #667; }}
  @media (max-width: 760px) {{
    body {{ padding: 12px 16px; }}
    .cols {{ grid-template-columns: 1fr; }}
    .col + .col {{ border-left: 0; border-top: 1px solid #eef0f3; }}
  }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #14161a; color: #e6e8ec; }}
    .bar, .card {{ background: #1b1e24; border-color: #2c313a; }}
    .head, .col + .col, li {{ border-color: #23272e; }}
    .date {{ color: #c9cdd3; }}
    a {{ color: #8ab8e6; }}
    input[type=search] {{ background: #14161a; color: #e6e8ec; border-color: #333944; }}
    .badge {{ background: #21252c; color: #8b919b; border-color: #2c313a; }}
    .stale {{ background: #33260f; color: #e0ad63; border-color: #4a3818; }}
    .fresh {{ background: #1c2f22; color: #8ed0a1; border-color: #2b4634; }}
  }}
</style>
<h1>Organisation incident timelines</h1>
<div class="sub">{sub}</div>
<div class="bar">
  <input type="search" id="q" placeholder="Filter by organisation, incident or event…">
  <span class="stat" id="stat"></span>
</div>
{cards}
<script>
  const q = document.getElementById('q'), stat = document.getElementById('stat');
  const cards = [...document.querySelectorAll('.card')];
  function apply() {{
    const t = q.value.toLowerCase(); let shown = 0;
    for (const c of cards) {{ const on = !t || c.textContent.toLowerCase().includes(t);
      c.style.display = on ? '' : 'none'; shown += on; }}
    stat.textContent = shown + ' of ' + cards.length + ' organisations';
  }}
  q.addEventListener('input', apply); apply();
</script>
"""


def _card(org: Dict[str, Any]) -> str:
    e = html.escape
    tl = org["timeline"]
    if tl is None:
        status = '<span class="badge">no timeline researched</span>'
        meta = ""
    else:
        status = ('<span class="badge stale">stale - newer evidence since research</span>' if org["stale"]
                  else '<span class="badge fresh">current</span>')
        meta = f'researched {e(str(tl["researched_at"])[:16].replace("T", " "))} by {e(tl["model"])}'
    def incident_flag(i: Dict[str, str]) -> str:
        if not org.get("reconciled"):
            return ""
        return ('<span class="badge stale">no event - coverage gap</span>' if i.get("number") in org["gaps"]
                else '<span class="badge fresh">matched</span>')

    def event_flags(ev: Dict[str, Any]) -> str:
        v = ev.get("verdict")
        if not v:
            return ""
        flags = []
        if v["match"] and v["match"] != "none":
            flags.append(f'<span class="badge fresh">= incident {e(v["match"].split("_", 1)[1])}</span>')
        else:
            kind = (v.get("unmatched_kind") or "unmatched").replace("_", " ")
            flags.append(f'<span class="badge stale">matches nothing: {e(kind)}</span>')
        if v.get("low_confidence"):
            flags.append('<span class="badge">low confidence</span>')
        if ev.get("date_conflict"):
            flags.append('<span class="badge stale">date conflicts with timeline</span>')
        return " ".join(flags)

    incidents = "".join(
        f'<li><div class="date">{(e(i.get("number", "")) + ". ") if i.get("number") else ""}'
        f'{e(i["date"]) or "&mdash;"} {incident_flag(i)}</div><div class="what">{e(i["what"])}</div>'
        + (f'<div class="public">{e(i["public"])}</div>' if i["public"] else "")
        + (f'<a href="{e(i["source"])}" target="_blank" rel="noopener">{e(i["source"])}</a>' if i["source"] else "")
        + "</li>" for i in org["incidents"]) or (
            # No list: show what research actually said ("NONE FOUND", or a refusal
            # to list a multinational's incidents) - that is itself informative.
            f'<div class="none">No incident list. Research answered: {e((tl or {}).get("timeline") or "nothing")}</div>')
    events = "".join(
        f'<li><div class="date">{e(ev["date"])} {event_flags(ev)}</div><div class="what">{e(ev["title"])}</div>'
        f'<div class="public">{ev["records"]} record{"s" if ev["records"] != 1 else ""}</div></li>'
        for ev in org["events"]) or '<div class="none">No active events.</div>'
    sources = ""
    if tl and tl["citations"]:
        links = "".join(f'<div><a href="{e(c)}" target="_blank" rel="noopener">{e(c)}</a></div>'
                        for c in tl["citations"])
        sources = f"<details><summary>Research sources ({len(tl['citations'])})</summary>{links}</details>"
    return (f'<section class="card"><div class="head"><h2>{e(org["name"])}</h2>'
            f'<span class="badge">{e(org["size"])}</span>{status}'
            f'<span class="meta">{org["event_count"]} events in the database · {len(org["incidents"])} '
            f'incidents researched{(" · " + meta) if meta else ""}</span></div>'
            f'<div class="cols"><div class="col"><h3>Researched incident timeline</h3><ol>{incidents}</ol></div>'
            f'<div class="col"><h3>Events in the database</h3><ol>{events}</ol></div></div>{sources}</section>')


def build_html(orgs: List[Dict[str, Any]]) -> str:
    sub = (f"The {len(orgs)} organisations with the most active events. Left: the organisation's publicly "
           "reported incidents as researched by Perplexity. Right: what this database holds. An incident on the "
           "left with nothing on the right is a coverage gap; an event on the right matching nothing on the left "
           f"is suspect. Generated {datetime.now():%Y-%m-%d %H:%M}.")
    return PAGE.format(sub=html.escape(sub), cards="".join(_card(o) for o in orgs))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--top", type=int, default=DEFAULT_TOP)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    conn = sqlite3.connect(args.db)
    orgs = load(conn, args.top, load_reconciliation(Path(args.db).parent / "timeline_reconciliation.json"))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(orgs), encoding="utf-8")
    logger.info("Timeline dashboard written: %s (%d organisations)", out.resolve(), len(orgs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
