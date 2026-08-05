"""Build the deduplication review dashboard.

A self-contained HTML page for inspecting *why* each deduplicated event looks
the way it does, and for correcting it. It answers the questions the previous
pipeline could not:

  - which source records were folded into this event, and from where?
  - what decided that, with what certainty, and on what evidence?
  - which merges are weakest and should be reviewed first?
  - how do I undo one, and will that survive the next rebuild?

The page is static (no server). Override and split actions are emitted as
ready-to-run ``dedup_admin.py`` commands the reviewer can copy, which keeps the
dashboard shareable while ensuring every change still goes through the audited
ledger rather than a direct database edit.

Usage:
    python scripts/build_dedup_dashboard.py
    python scripts/build_dedup_dashboard.py --out dashboard/dedup.html
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

logger = logging.getLogger(__name__)

DEFAULT_DB = "instance/cyber_events.db"
DEFAULT_OUT = "dashboard/dedup.html"


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def _display_org(stored, members):
    """Organisation to show: the stored victim, or a valid fallback."""
    from cyber_data_collector.dedup.victim_selection import (
        is_never_victim, is_non_organisation, is_threat_actor,
    )

    def ok(name):
        return bool(name) and not (is_non_organisation(name)
                                   or is_never_victim(name)
                                   or is_threat_actor(name))

    if ok(stored):
        return stored
    for member in members:
        if ok(member.get("entity")):
            return member["entity"]
    return None


def collect(conn: sqlite3.Connection, limit: Optional[int] = None) -> Dict[str, Any]:
    """Assemble every deduplicated event with its members and decisions."""
    conn.row_factory = sqlite3.Row

    limit_clause = f" LIMIT {int(limit)}" if limit else ""
    events = _rows(conn, f"""
        SELECT d.deduplicated_event_id AS id, d.title, d.event_date,
               d.victim_organization_name AS entity,
               d.vendor_organization_name AS vendor,
               d.roles_member_signature AS roles_sig,
               d.records_affected, d.severity, d.event_type,
               COALESCE(d.total_data_sources, 0) AS sources,
               COALESCE(d.has_human_override, 0) AS overridden,
               d.dedup_method, d.dedup_certainty,
               (SELECT COUNT(*) FROM EventDeduplicationMap m
                WHERE m.deduplicated_event_id = d.deduplicated_event_id) AS member_count
        FROM DeduplicatedEvents d
        WHERE COALESCE(d.status,'Active') = 'Active'
        ORDER BY member_count DESC, d.event_date DESC{limit_clause}
    """)

    # EnrichedEvents has no victim column; the affected organisation lives in
    # EntitiesV2 via the EnrichedEventEntities link table. Showing it per member
    # is what lets a reviewer spot a foreign organisation inside a merge group.
    # Candidate organisations per member record, ordered by role. The choice
    # is made in Python so it reuses is_non_organisation - the same rule that
    # governs the stored victim - instead of duplicating a list of vague names
    # in SQL. Picking by confidence alone showed "Australians" for the Origin
    # Energy breach and "students" for the Victorian Department of Education:
    # the people affected, not the breached organisation.
    from cyber_data_collector.dedup.victim_selection import (
        is_never_victim as _reporter,
        is_non_organisation as _not_org,
        is_threat_actor as _is_actor,
    )

    ROLE_ORDER = {"victim": 0, "vendor": 1, "affected_customer": 2,
                  "regulator": 4, "threat_actor": 5, "product": 6,
                  "bystander": 7}

    member_entities: Dict[str, List[str]] = {}
    for row in _rows(conn, """
        SELECT ee.enriched_event_id AS eid, v.entity_name AS name,
               ee.relationship_type AS role, ee.confidence_score AS conf,
               v.entity_kind AS kind
        FROM EnrichedEventEntities ee
        JOIN EntitiesV2 v ON v.entity_id = ee.entity_id
    """):
        if row["kind"] in ("person", "other", "threat_actor"):
            continue
        name = (row["name"] or "").strip()
        # The OAIC appears on nearly every incident because it was
        # notified. Showing it as the organisation implies it was breached.
        if not name or _not_org(name) or _is_actor(name) or _reporter(name):
            continue
        member_entities.setdefault(row["eid"], []).append(
            (ROLE_ORDER.get(row["role"], 3), -(row["conf"] or 0.0), name))

    best_entity = {
        eid: sorted(cands)[0][2] for eid, cands in member_entities.items() if cands
    }

    members_by_event: Dict[str, List[Dict]] = {}
    for row in _rows(conn, """
        SELECT m.deduplicated_event_id AS id, m.enriched_event_id, m.contribution_type,
               m.similarity_score, e.title, e.event_date,
               r.source_url, r.source_type
        FROM EventDeduplicationMap m
        LEFT JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
        LEFT JOIN RawEvents r ON r.raw_event_id = m.raw_event_id
        ORDER BY m.contribution_type DESC, e.event_date
    """):
        members_by_event.setdefault(row["id"], []).append({
            "enriched_event_id": row["enriched_event_id"],
            "contribution": row["contribution_type"],
            "similarity": row["similarity_score"],
            "title": row["title"],
            "date": str(row["event_date"]) if row["event_date"] else None,
            "entity": best_entity.get(row["enriched_event_id"]),
            "url": row["source_url"],
            "source_type": row["source_type"],
        })

    decisions_by_event: Dict[str, List[Dict]] = {}
    try:
        for row in _rows(conn, """
            SELECT deduplicated_event_id AS id, enriched_event_id, action, decided_by,
                   certainty, method, reasoning, evidence_json, superseded_by, created_at
            FROM DedupDecisions ORDER BY created_at
        """):
            evidence = {}
            if row["evidence_json"]:
                try:
                    evidence = json.loads(row["evidence_json"])
                except json.JSONDecodeError:
                    evidence = {}
            decisions_by_event.setdefault(row["id"], []).append({
                "enriched_event_id": row["enriched_event_id"],
                "action": row["action"],
                "decided_by": row["decided_by"],
                "certainty": row["certainty"],
                "method": row["method"],
                "reasoning": row["reasoning"],
                "superseded": bool(row["superseded_by"]),
                "created_at": str(row["created_at"]),
                "evidence": evidence,
            })
    except sqlite3.Error:
        logger.warning("DedupDecisions unavailable; run dedup_admin.py migrate")

    # Entity roles are decided per event against its members, so a later merge
    # or split leaves them describing a set of records the event no longer has.
    try:
        from cyber_data_collector.dedup.role_maintenance import stale_event_ids
        roles_stale_ids = set(stale_event_ids(conn))
    except Exception:  # noqa: BLE001 - the dashboard must still build
        roles_stale_ids = set()

    payload = []
    for event in events:
        members = members_by_event.get(event["id"], [])
        decisions = decisions_by_event.get(event["id"], [])
        certainties = [d["certainty"] for d in decisions
                       if d["certainty"] is not None and not d["superseded"]]
        payload.append({
            "id": event["id"],
            "title": event["title"],
            "date": str(event["event_date"]) if event["event_date"] else None,
            # The stored victim is shown only when it names an organisation.
            # Where it names the people in the database ("students") or a body
            # that merely received the notification (the OAIC), fall back to a
            # valid victim-role entity from the event's own records. This is a
            # display decision: the stored value is left untouched, because
            # overwriting it with a worse guess loses information permanently.
            "entity": _display_org(event["entity"], members),
            "stored_entity": event["entity"],
            "vendor": event["vendor"],
            "roles_stale": roles_stale_ids and event["id"] in roles_stale_ids,
            "records_affected": event["records_affected"],
            "severity": event["severity"],
            "event_type": event["event_type"],
            "sources": event["sources"],
            "member_count": event["member_count"],
            "overridden": bool(event["overridden"]),
            "method": event["dedup_method"],
            "min_certainty": min(certainties) if certainties else None,
            "members": members,
            "decisions": decisions,
        })

    stats = {
        "events": len(payload),
        "merged_events": sum(1 for e in payload if e["member_count"] > 1),
        "singletons": sum(1 for e in payload if e["member_count"] <= 1),
        "no_lineage": sum(1 for e in payload if e["member_count"] == 0),
        "overridden": sum(1 for e in payload if e["overridden"]),
        "supply_chain": sum(1 for e in payload if e["vendor"]),
        "roles_stale": sum(1 for e in payload if e["roles_stale"]),
        "unexplained": sum(1 for e in payload
                           if e["member_count"] > 1 and not e["decisions"]),
        "low_certainty": sum(1 for e in payload
                             if e["min_certainty"] is not None and e["min_certainty"] < 0.85),
        "total_members": sum(e["member_count"] for e in payload),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    return {"events": payload, "stats": stats}


TEMPLATE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Deduplication Review</title>
<style>
:root {
  --bg:#0f1115; --panel:#171a21; --panel2:#1e222b; --line:#2a2f3a;
  --text:#e6e9ef; --muted:#98a1b3; --accent:#4c8dff; --good:#37b978;
  --warn:#e0a03a; --bad:#e05c5c;
}
@media (prefers-color-scheme: light){
  :root { --bg:#f6f7f9; --panel:#fff; --panel2:#f0f2f6; --line:#dde1e8;
          --text:#1a1d23; --muted:#5c6472; }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
     font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{padding:20px 24px;border-bottom:1px solid var(--line);background:var(--panel)}
h1{margin:0 0 4px;font-size:19px}
.sub{color:var(--muted);font-size:13px}
.wrap{padding:20px 24px;max-width:1400px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px}
.card .n{font-size:24px;font-weight:600}
.card .l{color:var(--muted);font-size:12px;margin-top:2px}
.card.warn .n{color:var(--warn)} .card.bad .n{color:var(--bad)} .card.good .n{color:var(--good)}
.controls{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
input,select{background:var(--panel);color:var(--text);border:1px solid var(--line);
  border-radius:6px;padding:8px 10px;font-size:13px}
input[type=search]{flex:1;min-width:220px}
.ev{background:var(--panel);border:1px solid var(--line);border-radius:8px;margin-bottom:10px}
.ev summary{padding:12px 14px;cursor:pointer;display:flex;gap:10px;align-items:center;
  flex-wrap:wrap;list-style:none}
.ev summary::-webkit-details-marker{display:none}
.ev summary:hover{background:var(--panel2)}
.t{font-weight:600;flex:1;min-width:260px}
.badge{font-size:11px;padding:2px 8px;border-radius:99px;border:1px solid var(--line);
  color:var(--muted);white-space:nowrap}
.badge.g{color:var(--good);border-color:var(--good)}
.badge.w{color:var(--warn);border-color:var(--warn)}
.badge.b{color:var(--bad);border-color:var(--bad)}
.badge.a{color:var(--accent);border-color:var(--accent)}
.body{padding:0 14px 14px;border-top:1px solid var(--line)}
h4{margin:14px 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:11px;text-transform:uppercase}
td a{color:var(--accent);text-decoration:none} td a:hover{text-decoration:underline}
.reason{color:var(--text);background:var(--panel2);border-left:3px solid var(--accent);
  padding:8px 10px;border-radius:0 6px 6px 0;margin:6px 0;font-size:13px}
.reason.low{border-left-color:var(--warn)}
.ev-facts{color:var(--muted);font-size:12px;margin-top:4px}
.cmd{background:#0b0d11;color:#9ad;border:1px solid var(--line);border-radius:6px;
  padding:8px 10px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;
  overflow-x:auto;white-space:pre;margin:4px 0}
.copy{cursor:pointer;border:1px solid var(--line);background:var(--panel2);color:var(--text);
  border-radius:5px;padding:4px 9px;font-size:11px;margin-left:6px}
.none{color:var(--muted);font-style:italic;padding:8px 0}
.scroll{overflow-x:auto}
.hidden{display:none}
</style></head><body>
<header>
  <h1>Deduplication Review</h1>
  <div class="sub">Every merge, its evidence, and how to correct it &mdash; generated __GENERATED__</div>
</header>
<div class="wrap">
  <div class="cards" id="cards"></div>
  <div class="controls">
    <input type="search" id="q" placeholder="Search title, organisation or event id...">
    <select id="filter">
      <option value="all">All events</option>
      <option value="merged">Merged only (&gt;1 member)</option>
      <option value="unexplained">Merged but unexplained</option>
      <option value="low">Low certainty (&lt;0.85)</option>
      <option value="supplychain">Supply-chain (has a vendor)</option>
      <option value="rolesstale">Entity roles need refresh</option>
      <option value="overridden">Human-overridden</option>
      <option value="nolineage">No lineage</option>
    </select>
    <select id="sort">
      <option value="members">Sort: most members</option>
      <option value="certainty">Sort: lowest certainty</option>
      <option value="date">Sort: newest</option>
    </select>
  </div>
  <div id="list"></div>
</div>
<script>
const DATA = __DATA__;
const esc = s => (s==null?'':String(s)).replace(/[&<>"']/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function cards(){
  const s = DATA.stats;
  const defs = [
    ['events','Deduplicated events',''],
    ['merged_events','Merged (>1 source)','good'],
    ['total_members','Source records linked',''],
    ['unexplained','Merged, no recorded reason', s.unexplained? 'warn':'good'],
    ['low_certainty','Certainty below 0.85', s.low_certainty? 'warn':'good'],
    ['no_lineage','No lineage at all', s.no_lineage? 'bad':'good'],
    ['supply_chain','Via a third-party vendor',''],
    ['roles_stale','Entity roles need refresh', s.roles_stale? 'warn':'good'],
    ['overridden','Human-corrected','a'],
  ];
  document.getElementById('cards').innerHTML = defs.map(([k,l,cls])=>
    `<div class="card ${cls}"><div class="n">${s[k]??0}</div><div class="l">${l}</div></div>`
  ).join('');
}

function certBadge(c){
  if(c==null) return '<span class="badge">no certainty</span>';
  const cls = c>=0.9?'g':(c>=0.85?'':'w');
  return `<span class="badge ${cls}">certainty ${c.toFixed(2)}</span>`;
}

function memberTable(ev){
  if(!ev.members.length) return '<div class="none">No lineage recorded for this event.</div>';
  return `<div class="scroll"><table><thead><tr>
    <th>Role</th><th>Title</th><th>Date</th><th>Organisation</th><th>Source</th><th>Action</th>
    </tr></thead><tbody>` + ev.members.map(m=>`<tr>
      <td><span class="badge ${m.contribution==='master'?'a':''}">${esc(m.contribution||'?')}</span></td>
      <td>${esc(m.title)||'<i>untitled</i>'}</td>
      <td>${esc(m.date)||'-'}</td>
      <td>${esc(m.entity)||'-'}</td>
      <td>${m.url?`<a href="${esc(m.url)}" target="_blank" rel="noopener">${esc(m.source_type||'link')}</a>`:'-'}</td>
      <td>${ev.members.length>1 && m.contribution!=='master'
        ? `<button class="copy" onclick="cmdSplit('${esc(ev.id)}','${esc(m.enriched_event_id)}')">Split out</button>`
        : ''}</td>
    </tr>`).join('') + '</tbody></table></div>';
}

function decisionList(ev){
  if(!ev.decisions.length){
    return ev.member_count>1
      ? `<div class="none">No recorded decision. This merge predates the audit ledger &mdash;
         re-run <code>dedup_admin.py find-missed</code> to adjudicate it.</div>`
      : '<div class="none">Single-source event; nothing was merged.</div>';
  }
  return ev.decisions.map(d=>{
    const e = d.evidence||{};
    const facts = [];
    if(e.entity_canonical_left) facts.push(`entity: ${esc(e.entity_canonical_left)} vs ${esc(e.entity_canonical_right)}`);
    if(e.date_delta_days!=null) facts.push(`${e.date_delta_days} day(s) apart`);
    if(e.embedding_similarity!=null) facts.push(`embedding ${Number(e.embedding_similarity).toFixed(2)}`);
    if((e.supporting_facts||[]).length) facts.push('supports: '+e.supporting_facts.map(esc).join('; '));
    if((e.distinguishing_facts||[]).length) facts.push('against: '+e.distinguishing_facts.map(esc).join('; '));
    const low = (d.certainty!=null && d.certainty<0.85);
    return `<div class="reason ${low?'low':''}">
      <b>${esc(d.action)}</b> by <b>${esc(d.decided_by)}</b> ${certBadge(d.certainty)}
      ${d.superseded?'<span class="badge">superseded</span>':''}
      <div>${esc(d.reasoning)||'<i>no reasoning recorded</i>'}</div>
      ${facts.length?`<div class="ev-facts">${facts.join(' &middot; ')}</div>`:''}
    </div>`;
  }).join('');
}

function overrideBox(ev){
  const members = ev.members.filter(m=>m.contribution!=='master');
  if(!members.length) return '';
  const first = members[0];
  const master = (ev.members.find(m=>m.contribution==='master')||{}).enriched_event_id||'';
  return `<h4>Correct this event</h4>
    <div>Mark a pair as wrongly merged (survives future rebuilds):</div>
    <div class="cmd">python scripts/dedup_admin.py override ${esc(master)} ${esc(first.enriched_event_id)} different --reason "not the same incident"</div>
    <div>Then detach it:</div>
    <div class="cmd">python scripts/dedup_admin.py split ${esc(ev.id)} ${esc(first.enriched_event_id)} --reason "wrongly merged"</div>
    <div>Teach the pipeline from all corrections so far:</div>
    <div class="cmd">python scripts/dedup_admin.py learn</div>`;
}

function cmdSplit(dedupId, enrichedId){
  const cmd = `python scripts/dedup_admin.py split ${dedupId} ${enrichedId} --reason "wrongly merged"`;
  navigator.clipboard?.writeText(cmd);
  alert('Copied to clipboard:\\n\\n'+cmd);
}

function render(){
  const q = document.getElementById('q').value.toLowerCase().trim();
  const f = document.getElementById('filter').value;
  const sort = document.getElementById('sort').value;
  let rows = DATA.events.slice();

  if(f==='merged') rows = rows.filter(e=>e.member_count>1);
  if(f==='unexplained') rows = rows.filter(e=>e.member_count>1 && !e.decisions.length);
  if(f==='low') rows = rows.filter(e=>e.min_certainty!=null && e.min_certainty<0.85);
  if(f==='supplychain') rows = rows.filter(e=>e.vendor);
  if(f==='rolesstale') rows = rows.filter(e=>e.roles_stale);
  if(f==='overridden') rows = rows.filter(e=>e.overridden);
  if(f==='nolineage') rows = rows.filter(e=>e.member_count===0);
  if(q) rows = rows.filter(e=>
    (e.title||'').toLowerCase().includes(q) ||
    (e.entity||'').toLowerCase().includes(q) ||
    (e.id||'').toLowerCase().includes(q));

  if(sort==='members') rows.sort((a,b)=>b.member_count-a.member_count);
  if(sort==='certainty') rows.sort((a,b)=>(a.min_certainty??2)-(b.min_certainty??2));
  if(sort==='date') rows.sort((a,b)=>String(b.date||'').localeCompare(String(a.date||'')));

  document.getElementById('list').innerHTML = rows.slice(0,400).map(ev=>`
    <details class="ev"><summary>
      <span class="t">${esc(ev.title)||'<i>untitled</i>'}</span>
      <span class="badge">${esc(ev.date)||'no date'}</span>
      <span class="badge ${ev.member_count>1?'a':''}">${ev.member_count} source record(s)</span>
      ${ev.min_certainty!=null?certBadge(ev.min_certainty):''}
      ${ev.vendor?'<span class="badge w">supply chain</span>':''}
      ${ev.roles_stale?'<span class="badge w">roles stale</span>':''}
      ${ev.overridden?'<span class="badge a">human-corrected</span>':''}
      ${ev.member_count>1 && !ev.decisions.length?'<span class="badge w">unexplained</span>':''}
    </summary>
    <div class="body">
      <h4>Organisation</h4><div>${esc(ev.entity)||'<i>unknown</i>'}</div>
      ${ev.vendor ? `<h4>Third-party vendor</h4><div>${esc(ev.vendor)}
        <span class="badge">breach reached this organisation through its vendor</span></div>` : ''}
      <h4>Ancestry &mdash; records folded into this event</h4>${memberTable(ev)}
      <h4>Why these were merged</h4>${decisionList(ev)}
      ${overrideBox(ev)}
      <h4>Identifier</h4><div class="cmd">${esc(ev.id)}</div>
    </div></details>`).join('') ||
    '<div class="none">Nothing matches this filter.</div>';
}

cards();
['q','filter','sort'].forEach(id=>{
  document.getElementById(id).addEventListener('input', render);
  document.getElementById(id).addEventListener('change', render);
});
render();
</script></body></html>
"""


def build_html(data: Dict[str, Any]) -> str:
    return (TEMPLATE
            .replace("__DATA__", json.dumps(data, ensure_ascii=False, default=str))
            .replace("__GENERATED__", html.escape(data["stats"]["generated_at"])))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build the deduplication review dashboard")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = sqlite3.connect(args.db)
    try:
        data = collect(conn, limit=args.limit)
    finally:
        conn.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(data), encoding="utf-8")

    stats = data["stats"]
    logger.info("Wrote %s", out)
    logger.info(
        "%d events | %d merged | %d source records | %d unexplained | %d low-certainty",
        stats["events"], stats["merged_events"], stats["total_members"],
        stats["unexplained"], stats["low_certainty"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
