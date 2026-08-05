"""Build the by-entity event dashboard.

Lists every deduplicated event grouped under the organisation it happened to,
sorted by entity and then by event date. It answers a question the other two
dashboards do not: *what has happened to this organisation, in order?*

That ordering also makes under-deduplication visible. When one incident is
still stored as several events, they surface as consecutive rows under the same
entity - which is how the Latitude, Medibank and Dymocks fragments were spotted.
The "only entities with multiple events" filter exists for exactly that review:
it hides every organisation with a single event, leaving the candidates.

Entity attribution comes from the ``victim`` role in DeduplicatedEventEntities,
not from ``victim_organization_name``, because an event can have several
co-equal victims - the ProctorU breach has ten Australian universities - and a
single scalar column cannot represent that. An event with several victims is
listed under each of them.

Each organisation carries its ordinal size band (SMALL / MEDIUM / LARGE / HUGE /
UNKNOWN, from ``EntitiesV2.size_estimate``), so the page can be read by scale -
"what happens to small businesses" is a different question from "what happens to
the ASX-100", and the page could not previously tell them apart. Hovering a
badge shows the evidence the band was based on.

The page is static and self-contained; no server required.

Usage:
    python scripts/build_entity_dashboard.py
    python scripts/build_entity_dashboard.py --out dashboard/entities.html
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
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logger = logging.getLogger(__name__)

DEFAULT_DB = "instance/cyber_events.db"
DEFAULT_OUT = "dashboard/entities.html"

# Long enough to judge the incident, short enough to scan a table.
DESCRIPTION_CHARS = 320


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def load_rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """One row per (entity, event) pair, ordered by entity then date.

    The description falls back through the deduplicated event, then its master
    enriched record, because ``DeduplicatedEvents.description`` is empty on
    every row in the current database - the text lives upstream.
    """
    # size_estimate arrived with the v3 schema migration; a database that has
    # not been migrated yet should still render rather than fail.
    has_size = any(r[1] == "size_estimate"
                   for r in conn.execute("PRAGMA table_info(EntitiesV2)"))
    size_columns = ("v.size_estimate AS size, v.size_basis AS size_basis, "
                    "v.size_confidence AS size_confidence"
                    if has_size else
                    "NULL AS size, NULL AS size_basis, NULL AS size_confidence")

    rows = conn.execute(
        f"""
        SELECT v.entity_name                          AS entity,
               {size_columns},
               d.deduplicated_event_id                AS event_id,
               d.title                                AS title,
               d.event_date                           AS event_date,
               d.records_affected                     AS records_affected,
               d.vendor_organization_name             AS vendor,
               COALESCE(
                   NULLIF(TRIM(d.summary), ''),
                   NULLIF(TRIM(d.description), ''),
                   NULLIF(TRIM(e.summary), ''),
                   NULLIF(TRIM(e.description), ''),
                   ''
               )                                      AS description,
               (SELECT COUNT(*) FROM EventDeduplicationMap m
                WHERE m.deduplicated_event_id = d.deduplicated_event_id) AS members
        FROM DeduplicatedEventEntities dee
        JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
        JOIN DeduplicatedEvents d
             ON d.deduplicated_event_id = dee.deduplicated_event_id
        LEFT JOIN EnrichedEvents e
             ON e.enriched_event_id = d.master_enriched_event_id
        WHERE dee.relationship_type = 'victim'
          AND COALESCE(d.status, 'Active') = 'Active'
          AND v.entity_name IS NOT NULL
          AND TRIM(v.entity_name) != ''
        ORDER BY v.entity_name COLLATE NOCASE, d.event_date, d.title
        """
    ).fetchall()

    out: List[Dict[str, Any]] = []
    for r in rows:
        description = (r["description"] or "").strip()
        # A description that is still raw page scrape (PDF headers, nav bars)
        # is worse than none; it pushes the real content out of the column.
        if description.startswith("%PDF"):
            description = ""
        if len(description) > DESCRIPTION_CHARS:
            description = description[:DESCRIPTION_CHARS].rsplit(" ", 1)[0] + "…"
        out.append({
            "entity": r["entity"],
            # An entity that has never been researched reads as UNKNOWN, which
            # is what it is - not a blank cell that looks like a rendering bug.
            # `estimated` keeps the two apart for the footer, because "not yet
            # looked up" is pending work and "looked up, no size" is not.
            "size": (r["size"] or "UNKNOWN").upper(),
            "estimated": r["size"] is not None,
            "size_basis": r["size_basis"] or "",
            "size_confidence": r["size_confidence"],
            "event_id": r["event_id"],
            "title": r["title"] or "(untitled)",
            "date": str(r["event_date"])[:10] if r["event_date"] else "",
            "records": r["records_affected"],
            "vendor": r["vendor"],
            "members": r["members"],
            "description": description,
        })
    return out


SIZE_ORDER = ["SMALL", "MEDIUM", "LARGE", "HUGE", "UNKNOWN"]


def summarise(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    sizes: Dict[str, str] = {}
    estimated: Dict[str, bool] = {}
    for r in rows:
        counts[r["entity"]] = counts.get(r["entity"], 0) + 1
        sizes[r["entity"]] = r.get("size", "UNKNOWN")
        estimated[r["entity"]] = r.get("estimated", False)
    multi = {name for name, n in counts.items() if n > 1}
    # Counted per entity, not per row: a HUGE organisation with nine events
    # would otherwise dominate a distribution meant to describe the entities.
    by_size: Dict[str, int] = {}
    for size in sizes.values():
        by_size[size] = by_size.get(size, 0) + 1
    return {
        "entities": len(counts),
        "events": len({r["event_id"] for r in rows}),
        "rows": len(rows),
        "multi_entities": len(multi),
        "multi_rows": sum(1 for r in rows if r["entity"] in multi),
        "by_size": {size: by_size.get(size, 0) for size in SIZE_ORDER
                    if by_size.get(size)},
        "estimated": sum(1 for done in estimated.values() if done),
        "unestimated": sum(1 for done in estimated.values() if not done),
    }


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Cyber events by entity</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font: 15px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
         margin: 0; padding: 24px; background: #f6f7f9; color: #16181d; }}
  h1 {{ font-size: 21px; margin: 0 0 4px; }}
  .sub {{ color: #667; margin-bottom: 18px; font-size: 13px; }}
  .bar {{ display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
          background: #fff; border: 1px solid #dfe3e8; border-radius: 8px;
          padding: 12px 14px; margin-bottom: 16px; position: sticky; top: 0;
          z-index: 5; }}
  input[type=search] {{ flex: 1 1 260px; min-width: 200px; padding: 7px 10px;
      border: 1px solid #ccd2d9; border-radius: 6px; font-size: 14px; }}
  label {{ display: flex; align-items: center; gap: 7px; font-size: 13px;
           white-space: nowrap; cursor: pointer; }}
  .stat {{ font-size: 13px; color: #667; margin-left: auto; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff;
           border: 1px solid #dfe3e8; border-radius: 8px; overflow: hidden; }}
  th {{ text-align: left; font-size: 12px; text-transform: uppercase;
        letter-spacing: .04em; color: #667; background: #f0f2f5;
        padding: 9px 12px; border-bottom: 1px solid #dfe3e8;
        position: sticky; top: 62px; }}
  td {{ padding: 9px 12px; border-bottom: 1px solid #eef0f3;
        vertical-align: top; }}
  tr.first-of-entity td {{ border-top: 2px solid #dfe3e8; }}
  .entity {{ font-weight: 600; white-space: nowrap; }}
  .entity .n {{ font-weight: 400; color: #8a8f98; font-size: 12px; }}
  .size {{ display: inline-block; font-size: 10.5px; font-weight: 700;
           letter-spacing: .05em; padding: 2px 6px; border-radius: 4px;
           border: 1px solid transparent; white-space: nowrap; cursor: help; }}
  .size-SMALL   {{ background: #e8f4ea; color: #2f6b3d; border-color: #cbe3d1; }}
  .size-MEDIUM  {{ background: #e7f0fa; color: #2b5a86; border-color: #cadcef; }}
  .size-LARGE   {{ background: #fdf0e2; color: #8a5312; border-color: #f0dcc2; }}
  .size-HUGE    {{ background: #fbe6e6; color: #8c2f2f; border-color: #f0cbcb; }}
  .size-UNKNOWN {{ background: #eef0f3; color: #7b818b; border-color: #dfe3e8; }}
  .date {{ white-space: nowrap; color: #444; font-variant-numeric: tabular-nums; }}
  .title {{ font-weight: 500; }}
  .desc {{ color: #555; font-size: 13.5px; }}
  .meta {{ color: #8a8f98; font-size: 12px; margin-top: 2px; }}
  .dup {{ background: #fff8e6; }}
  .empty {{ padding: 30px; text-align: center; color: #889; background: #fff;
            border: 1px solid #dfe3e8; border-radius: 8px; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #14161a; color: #e6e8ec; }}
    .bar, table, .empty {{ background: #1b1e24; border-color: #2c313a; }}
    th {{ background: #21252c; color: #9aa1ac; border-color: #2c313a; }}
    td {{ border-color: #23272e; }}
    tr.first-of-entity td {{ border-top-color: #313742; }}
    .desc {{ color: #b3b8c0; }}
    input[type=search], select {{ background: #14161a; color: #e6e8ec;
                                  border-color: #333944; }}
    .dup {{ background: #2a2412; }}
    .size-SMALL   {{ background: #1c2f22; color: #8ed0a1; border-color: #2b4634; }}
    .size-MEDIUM  {{ background: #182634; color: #8ab8e6; border-color: #27394d; }}
    .size-LARGE   {{ background: #33260f; color: #e0ad63; border-color: #4a3818; }}
    .size-HUGE    {{ background: #331a1a; color: #e88a8a; border-color: #4d2626; }}
    .size-UNKNOWN {{ background: #21252c; color: #8b919b; border-color: #2c313a; }}
  }}
</style>
<h1>Cyber events by entity</h1>
<div class="sub">{sub}</div>
<div class="bar">
  <input type="search" id="q" placeholder="Filter by entity, event or description…">
  <label>Size
    <select id="size">
      <option value="">all</option>
      <option value="SMALL">SMALL</option>
      <option value="MEDIUM">MEDIUM</option>
      <option value="LARGE">LARGE</option>
      <option value="HUGE">HUGE</option>
      <option value="UNKNOWN">UNKNOWN</option>
    </select>
  </label>
  <label><input type="checkbox" id="multi"> Only entities with more than one event</label>
  <span class="stat" id="stat"></span>
</div>
<div id="host"></div>
<script>
const ROWS = {rows};

const host = document.getElementById('host');
const q = document.getElementById('q');
const multi = document.getElementById('multi');
const size = document.getElementById('size');
const stat = document.getElementById('stat');

function esc(s) {{
  return String(s == null ? '' : s).replace(/[&<>"]/g,
    c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}})[c]);
}}

function render() {{
  const needle = q.value.trim().toLowerCase();
  const counts = {{}};
  for (const r of ROWS) counts[r.entity] = (counts[r.entity] || 0) + 1;

  let rows = ROWS;
  // The multi-event filter counts an entity's events *before* the text
  // filter, so searching inside a duplicated group does not make the group
  // look single-event and vanish.
  if (multi.checked) rows = rows.filter(r => counts[r.entity] > 1);
  if (size.value) rows = rows.filter(r => r.size === size.value);
  if (needle) rows = rows.filter(r =>
    (r.entity + ' ' + r.title + ' ' + r.description).toLowerCase().includes(needle));

  const entities = new Set(rows.map(r => r.entity));
  stat.textContent = rows.length + ' event row(s) across ' +
                     entities.size + ' entit' + (entities.size === 1 ? 'y' : 'ies');

  if (!rows.length) {{
    host.innerHTML = '<div class="empty">Nothing matches that filter.</div>';
    return;
  }}

  let html = '<table><thead><tr><th>Entity</th><th>Size</th><th>Date</th>' +
             '<th>Event</th><th>Description</th></tr></thead><tbody>';
  let prev = null;
  for (const r of rows) {{
    const isNew = r.entity !== prev;
    const dup = counts[r.entity] > 1;
    html += '<tr class="' + (isNew ? 'first-of-entity ' : '') +
            (dup ? 'dup' : '') + '">';
    html += '<td class="entity">' + (isNew ? esc(r.entity) +
            (dup ? ' <span class="n">×' + counts[r.entity] + '</span>' : '') : '') + '</td>';
    // The badge repeats on every row of a group so a row read on its own -
    // after a text filter, or scrolled past the group heading - still says
    // how big the organisation is.
    const tip = r.size_basis
      ? r.size_basis + (r.size_confidence != null
          ? ' (confidence ' + Number(r.size_confidence).toFixed(2) + ')' : '')
      : 'No size research recorded for this organisation yet.';
    html += '<td><span class="size size-' + esc(r.size) + '" title="' +
            esc(tip) + '">' + esc(r.size) + '</span></td>';
    html += '<td class="date">' + esc(r.date) + '</td>';
    html += '<td><div class="title">' + esc(r.title) + '</div><div class="meta">' +
            r.members + ' source record(s)' +
            (r.records ? ' · ' + Number(r.records).toLocaleString() + ' records' : '') +
            (r.vendor ? ' · via ' + esc(r.vendor) : '') + '</div></td>';
    html += '<td class="desc">' + esc(r.description) + '</td>';
    html += '</tr>';
    prev = r.entity;
  }}
  host.innerHTML = html + '</tbody></table>';
}}

q.addEventListener('input', render);
multi.addEventListener('change', render);
size.addEventListener('change', render);
render();
</script>
"""


def build_html(rows: List[Dict[str, Any]], stats: Dict[str, Any]) -> str:
    by_size = ", ".join(f"{n} {size}" for size, n in stats["by_size"].items())
    sub = (f"{stats['events']} event(s) attributed to {stats['entities']} entit"
           f"{'y' if stats['entities'] == 1 else 'ies'}; "
           f"{stats['multi_entities']} entit"
           f"{'y has' if stats['multi_entities'] == 1 else 'ies have'} more than "
           f"one event. "
           + (f"By size: {by_size}. " if by_size else "")
           + f"Generated {datetime.now():%Y-%m-%d %H:%M}.")
    return PAGE.format(sub=html.escape(sub),
                       rows=json.dumps(rows, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()

    conn = _connect(args.db)
    try:
        rows = load_rows(conn)
    finally:
        conn.close()

    stats = summarise(rows)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(rows, stats), encoding="utf-8")

    print(f"Entity dashboard generated: {out}")
    print(f"  {stats['events']} event(s), {stats['entities']} entities, "
          f"{stats['rows']} row(s)")
    print(f"  {stats['multi_entities']} entities have more than one event "
          f"({stats['multi_rows']} rows) - the review candidates")
    print(f"  {stats['estimated']} of {stats['entities']} entities have been "
          f"sized: "
          + (", ".join(f"{n} {size}" for size, n in stats["by_size"].items())
             or "none"))
    # UNKNOWN is a researched answer, not pending work; only a never-looked-up
    # entity is something to go and do.
    if stats["unestimated"]:
        print(f"  {stats['unestimated']} not yet looked up - run "
              "`python scripts/dedup_admin.py size-entities`")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
