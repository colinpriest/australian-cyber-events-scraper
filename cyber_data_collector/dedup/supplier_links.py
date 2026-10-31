"""Supplier incidents as linked events: the supplier's event, one event per customer, and a link table.

Owner decision (2026-10-31): a supplier breach is recorded as

* the **supplier's own event** (victim = the supplier), when one exists;
* **one event per major affected customer** (victim = that customer, vendor = the supplier);
* rows in ``SupplierIncidents`` / ``SupplierIncidentLinks`` that tie them together, so a report can
  aggregate everything one supplier's breach caused.

No event carries several customers as co-victims, and customer events are never merged into each other
or into the supplier event. This replaces the convention that one supplier breach reported about several
customers is one event, which :mod:`supplier_victims` used to enforce by attaching customers as
co-victims and merging customers' events (Allens filed under the NSW Health Accellion event beside its
own January 2021 Accellion event, 2026-10-30).

**Tables.** ``SupplierIncidents`` holds one row per supplier incident: the supplier (canonical key,
entity, name), its own event when one exists, and the date span of what is linked. Each of the
supplier's own events anchors its own incident - two of them are never one incident, so Ivanti's
separate exploitation campaigns (January 2024 Connect Secure, October 2024 EPM, January and April 2025)
stay four incidents, with no customers unless an Australian customer's event is linked. A customer
event joins the incident of that supplier nearest in date within
:data:`supplier_victims.OWN_EVENT_WINDOW_DAYS`; with none, a new incident is started.
``SupplierIncidentLinks`` holds one row per affected customer: its event (NULL for a customer the
reporting names that has no event of its own), the basis and the judgement that decided it. Event ids
are stored as they were; readers resolve a Merged id to the Active event its records are in now
(:func:`resolve_event`). Upserts are idempotent: a row changes only when what it records changes, and a
known event is never replaced by NULL.

**What decides a link** (:func:`plan_links`; labels decide first, then the calibrated judgements of
:mod:`supplier_victims`, which are no longer merge or attachment decisions):

* ``vendor-recorded``: a detected supplier incident event whose one recorded victim is a customer of its
  recorded vendor is that customer's event (``decided_by`` ``rule``); one whose victim is the vendor is the
  supplier's own event;
* ``label`` / ``judged-same-incident``: another event judged the same supplier incident (a ``same`` pair
  label, or the same-incident judgement at its calibrated cut-off) is linked as its customer's event;
* ``customer-named``: a customer the evidence names, accepted by the victim judgement (label ``affected`` or
  the calibrated cut-off), with no Active event of its own near the incident, is linked with no event;
* ``converted-attachment``: a customer an earlier version attached as a co-victim (see below).

An event with several recorded victims, an event with no vendor recorded, or a customer that has an own
event the pair judgement did not link is **reported** (``ambiguous``), never guessed.

**Conversion of the old state** (:func:`convert`, with ``apply``):

1. every victim role ``SupplierVictimAttachments`` added that is still present is removed (audit row
   ``reversed`` in ``SupplierVictimReversals``, reason the owner decision) and the customer becomes a link
   row - with its own Active event near the incident that records the same supplier, else none;
2. an Active event that the supplier step joined across customers (an attachment, a merge of
   ``same-supplier-incident`` reasoning, or a ``supplier-victims-*`` snapshot batch) and whose records name
   several of those customers is split per customer through :meth:`DedupLedger.split_members`: each record
   goes to the customer its Perplexity research names (the strict same-organisation test the module uses),
   records naming no organisation or another organisation stay. When some records name the supplier,
   those stay and the event becomes the supplier's event, every customer group leaving (the ZircoDATA
   event of January 2024 held ZircoDATA's own breach and its customers under the victim Monash Health);
   otherwise the group of the event's own recorded victim stays, else the largest. A part that leaves
   records the supplier as its vendor and is linked (``split-customer``); the part that stays is the
   supplier's event or its customer's linked event. Each leaving group becomes one event, which is then
   linked. The same split applies to any detected supplier incident whose records name both the supplier
   and a recorded victim. A record naming two of the customers keeps the event from being split and is
   reported.

:func:`supplier_incident_summary` is the reporting helper.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS SupplierIncidents (
    supplier_incident_id TEXT PRIMARY KEY,
    supplier_key         TEXT NOT NULL,       -- canonical key of the supplier's name
    supplier_entity_id   INTEGER,             -- NULL when no entity row holds the supplier's name
    supplier_name        TEXT NOT NULL,
    supplier_event_id    TEXT,                -- the supplier's own event, when one exists
    first_date           TEXT,
    last_date            TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_supplier_incidents_key ON SupplierIncidents (supplier_key);
CREATE TABLE IF NOT EXISTS SupplierIncidentLinks (
    supplier_incident_id TEXT NOT NULL,
    customer_entity_id   INTEGER NOT NULL,
    customer_name        TEXT NOT NULL,
    customer_event_id    TEXT,                -- NULL: a customer the reporting names, with no event of its own
    basis                TEXT NOT NULL,       -- vendor-recorded / label / judged-same-incident /
                                              -- customer-named / converted-attachment
    probability          REAL,
    decided_by           TEXT NOT NULL,       -- rule / label / jev / escalation / attachment's decider
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    PRIMARY KEY (supplier_incident_id, customer_entity_id)
);
CREATE INDEX IF NOT EXISTS idx_supplier_links_event ON SupplierIncidentLinks (customer_event_id);
CREATE TABLE IF NOT EXISTS SupplierLinkActions (
    action_id             TEXT PRIMARY KEY,
    action                TEXT NOT NULL,      -- split
    deduplicated_event_id TEXT NOT NULL,      -- the event split
    new_event_id          TEXT,
    entity_id             INTEGER,            -- the customer whose records left
    detail_json           TEXT,
    reason                TEXT NOT NULL,
    created_at            TEXT NOT NULL
);
"""

BASIS_VENDOR = "vendor-recorded"
BASIS_LABEL = "label"
BASIS_JUDGED = "judged-same-incident"
BASIS_NAMED = "customer-named"
BASIS_CONVERTED = "converted-attachment"
BASIS_SPLIT = "split-customer"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now().isoformat()


# ----------------------------------------------------------------------
# Reading: events through merges
# ----------------------------------------------------------------------

def resolve_event(conn: sqlite3.Connection, event_id: Optional[str], resolver=None) -> Optional[str]:
    """The Active event holding ``event_id``'s records now (itself when Active), or None.

    A Merged event resolves to the Active event that holds most of its mapped records; a Rejected one, or
    a Merged one whose records are in no Active event, to None. ``resolver`` is accepted for callers and
    unused.
    """
    if not event_id:
        return None
    row = conn.execute("SELECT status FROM DeduplicatedEvents WHERE deduplicated_event_id = ?", (event_id,)).fetchone()
    if row is None:
        return None
    if row[0] == "Active":
        return event_id
    if row[0] != "Merged":
        return None
    held = conn.execute(
        """SELECT m2.deduplicated_event_id, COUNT(*) FROM EventDeduplicationMap m1
           JOIN EventDeduplicationMap m2 ON m2.enriched_event_id = m1.enriched_event_id
           JOIN DeduplicatedEvents d ON d.deduplicated_event_id = m2.deduplicated_event_id
           WHERE m1.deduplicated_event_id = ? AND d.status = 'Active'
           GROUP BY m2.deduplicated_event_id ORDER BY COUNT(*) DESC, m2.deduplicated_event_id LIMIT 1""",
        (event_id,)).fetchone()
    return held[0] if held else None


# ----------------------------------------------------------------------
# Incidents and links (in memory, then written)
# ----------------------------------------------------------------------

@dataclass
class Incident:
    supplier_incident_id: str
    supplier_key: str
    supplier_name: str
    supplier_entity_id: Optional[int] = None
    supplier_event_id: Optional[str] = None
    dates: Set[str] = field(default_factory=set)
    events: Set[str] = field(default_factory=set)       # events placed in it this run
    anchor_date: Optional[str] = None                     # the supplier event's date, else the first placed
    first_date: Optional[str] = None
    last_date: Optional[str] = None
    stored: bool = False


@dataclass
class Link:
    supplier_incident_id: str
    customer_entity_id: int
    customer_name: str
    customer_event_id: Optional[str]
    basis: str
    probability: Optional[float]
    decided_by: str


class Registry:
    """Supplier incidents and their links: the stored rows plus what this run adds."""

    def __init__(self, conn: sqlite3.Connection, resolver):
        from cyber_data_collector.dedup.supplier_victims import OWN_EVENT_WINDOW_DAYS

        ensure_schema(conn)
        self.conn, self.resolver, self.window = conn, resolver, OWN_EVENT_WINDOW_DAYS
        self.incidents: Dict[str, Incident] = {}
        for row in conn.execute(
                """SELECT supplier_incident_id, supplier_key, supplier_name, supplier_entity_id, supplier_event_id,
                          first_date, last_date FROM SupplierIncidents"""):
            self.incidents[row[0]] = Incident(row[0], row[1], row[2], supplier_entity_id=row[3],
                                              supplier_event_id=row[4], first_date=row[5], last_date=row[6],
                                              stored=True)
        self.links: Dict[Tuple[str, int], Link] = {}
        self.stored_links: Dict[Tuple[str, int], Tuple] = {}
        for row in conn.execute(
                """SELECT supplier_incident_id, customer_entity_id, customer_name, customer_event_id, basis,
                          probability, decided_by FROM SupplierIncidentLinks"""):
            self.stored_links[(row[0], row[1])] = row

    def key(self, supplier: str) -> str:
        return self.resolver.key_for(supplier)

    def incident(self, supplier: str, event_date: Optional[str], anchor: str,
                 supplier_event: Optional[str] = None, apart=None) -> Incident:
        """The incident an event belongs to.

        ``supplier_event``: the supplier's own event, which anchors its own incident (never shared with
        another of the supplier's events). Otherwise the supplier's incident nearest ``event_date`` within
        the window, else a new one. ``apart(a, b)`` True means two events were ruled different incidents
        (a pair label); an incident holding such an event is not joined.
        """
        from cyber_data_collector.dedup.supplier_victims import _gap

        key = self.key(supplier)
        mine = [inc for inc in self.incidents.values() if inc.supplier_key == key]
        if supplier_event is not None:
            for inc in mine:
                if supplier_event in (inc.supplier_event_id, resolve_event(self.conn, inc.supplier_event_id)):
                    if event_date:
                        inc.dates.add(event_date)
                    return inc
            sid = "si-" + hashlib.sha1(f"{key}|{supplier_event}".encode("utf-8")).hexdigest()[:16]
            inc = self.incidents.setdefault(sid, Incident(sid, key, supplier))
            inc.supplier_event_id = supplier_event
            inc.anchor_date = inc.anchor_date or event_date
            inc.events.add(supplier_event)
            if event_date:
                inc.dates.add(event_date)
            return inc
        best: Optional[Tuple[int, str]] = None
        for inc in mine:
            if apart is not None and any(apart(anchor, e) for e in inc.events | {inc.supplier_event_id} if e):
                continue
            # Measured from the incident's anchor, not from its latest member, so incidents cannot chain.
            ref = inc.anchor_date or inc.first_date
            gap = _gap(event_date, ref) if ref and event_date else None
            if gap is None:
                gap = self.window          # undated on either side: joins only when nothing nearer exists
            if gap <= self.window and (best is None or (gap, inc.supplier_incident_id) < best):
                best = (gap, inc.supplier_incident_id)
        if best is not None:
            inc = self.incidents[best[1]]
        else:
            sid = "si-" + hashlib.sha1(f"{key}|{anchor}".encode("utf-8")).hexdigest()[:16]
            inc = self.incidents.setdefault(sid, Incident(sid, key, supplier))
            inc.anchor_date = inc.anchor_date or event_date
        inc.events.add(anchor)
        if event_date:
            inc.dates.add(event_date)
        return inc

    def link(self, link: Link) -> None:
        """Keep the strongest claim per (incident, customer): an event beats none, then a label, then a score."""
        key = (link.supplier_incident_id, link.customer_entity_id)
        old = self.links.get(key)
        if old is None or _rank(link) > _rank(old):
            if old is not None and link.customer_event_id is None:
                link.customer_event_id = old.customer_event_id
            self.links[key] = link
        elif old.customer_event_id is None and link.customer_event_id is not None:
            old.customer_event_id = link.customer_event_id

    def write(self) -> Dict[str, int]:
        """Upsert incidents and links; a row changes only when what it records changes."""
        now = _now()
        counts = {"incidents_created": 0, "incidents_updated": 0, "links_created": 0, "links_updated": 0}
        for inc in self.incidents.values():
            dates = sorted(d for d in {*inc.dates, inc.first_date, inc.last_date} if d)
            first, last = (dates[0], dates[-1]) if dates else (None, None)
            if not inc.stored:
                self.conn.execute(
                    "INSERT INTO SupplierIncidents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (inc.supplier_incident_id, inc.supplier_key, inc.supplier_entity_id, inc.supplier_name,
                     inc.supplier_event_id, first, last, now, now))
                counts["incidents_created"] += 1
                inc.stored = True
            else:
                cur = self.conn.execute(
                    """UPDATE SupplierIncidents SET supplier_entity_id = COALESCE(?, supplier_entity_id),
                              supplier_event_id = COALESCE(?, supplier_event_id), first_date = ?, last_date = ?,
                              updated_at = ?
                       WHERE supplier_incident_id = ? AND NOT (
                              supplier_entity_id IS COALESCE(?, supplier_entity_id)
                          AND supplier_event_id IS COALESCE(?, supplier_event_id)
                          AND first_date IS ? AND last_date IS ?)""",
                    (inc.supplier_entity_id, inc.supplier_event_id, first, last, now, inc.supplier_incident_id,
                     inc.supplier_entity_id, inc.supplier_event_id, first, last))
                counts["incidents_updated"] += cur.rowcount
            inc.first_date, inc.last_date = first, last
        for key, link in self.links.items():
            stored = self.stored_links.get(key)
            values = (link.customer_name, link.customer_event_id, link.basis, link.probability, link.decided_by)
            if stored is None:
                self.conn.execute("INSERT INTO SupplierIncidentLinks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                  (link.supplier_incident_id, link.customer_entity_id, *values, now, now))
                counts["links_created"] += 1
            else:
                event = link.customer_event_id or stored[3]     # a known event is never replaced by NULL
                new = (link.customer_name, event, link.basis, link.probability, link.decided_by)
                if _rank_row(stored) > _rank(link) and event == stored[3]:
                    continue
                if new != tuple(stored[2:]):
                    self.conn.execute(
                        """UPDATE SupplierIncidentLinks SET customer_name = ?, customer_event_id = ?, basis = ?,
                                  probability = ?, decided_by = ?, updated_at = ?
                           WHERE supplier_incident_id = ? AND customer_entity_id = ?""",
                        (*new, now, *key))
                    counts["links_updated"] += 1
            self.stored_links[key] = (*key, link.customer_name, link.customer_event_id or (stored or [None] * 4)[3],
                                      link.basis, link.probability, link.decided_by)
        return counts


_BASIS_RANK = {BASIS_LABEL: 4, BASIS_VENDOR: 3, BASIS_JUDGED: 3, BASIS_SPLIT: 3, BASIS_CONVERTED: 2,
               BASIS_NAMED: 1}


def _rank(link: Link) -> Tuple[int, int, float]:
    return (link.customer_event_id is not None, _BASIS_RANK.get(link.basis, 0), link.probability or 0.0)


def _rank_row(row: Tuple) -> Tuple[int, int, float]:
    return (row[3] is not None, _BASIS_RANK.get(row[4], 0), row[5] or 0.0)


# ----------------------------------------------------------------------
# Planning links from detection, labels and calibrated judgements
# ----------------------------------------------------------------------

def _entity_id(conn: sqlite3.Connection, index, name: str) -> Optional[int]:
    hit = index.find(name)
    return hit[0] if hit else None


def _supplier_event(conn, registry: Registry, inc: Incident, own: Dict[int, List[Tuple[str, Optional[str]]]],
                    names: Dict[int, str], exclude: Set[str] = frozenset()) -> Optional[str]:
    """The supplier's own Active event nearest the incident, if any (not one anchoring another incident)."""
    from cyber_data_collector.dedup.supplier_victims import _gap, same_org

    best: Optional[Tuple[int, str]] = None
    for entity_id, events in own.items():
        if not same_org(registry.resolver, inc.supplier_name, names.get(entity_id, "")):
            continue
        for did, edate in events:
            if did in exclude:
                continue
            gaps = [g for g in (_gap(edate, d) for d in inc.dates) if g is not None]
            gap = min(gaps) if gaps else None
            if gap is not None and gap > registry.window:
                continue
            cand = (gap if gap is not None else registry.window + 1, did)
            if best is None or cand < best:
                best = cand
    return best[1] if best else None


def plan_links(conn: sqlite3.Connection, g, labels, vscores, pscores, vcut: Optional[float],
               pcut: Optional[float], registry: Registry) -> Dict[str, Any]:
    """Add every link the evidence supports to ``registry``; return what was ambiguous."""
    from cyber_data_collector.dedup import supplier_victims as sv

    ambiguous: List[Dict[str, Any]] = []
    own = sv.victim_events(conn)
    names = dict(conn.execute("SELECT entity_id, entity_name FROM EntitiesV2"))
    by_id = g.by_id
    incident_of: Dict[str, Incident] = {}

    def customer_of(did: str) -> Tuple[Optional[Tuple[int, str]], List[str]]:
        victims = sv.event_victims(conn, did)
        return ((victims[0][0], victims[0][1]) if len(victims) == 1 else None), [v[1] for v in victims]

    different = {sid for (kind, sid), label in labels.items()
                 if kind == sv.KIND_PAIR and label in sv.NEGATIVE[sv.KIND_PAIR]}

    def apart(a: str, b: str) -> bool:
        return "|".join(sorted((a, b))) in different

    customer_events = []
    for x in g.incidents:
        if not x.vendors:
            ambiguous.append({"deduplicated_event_id": x.dedup_id, "title": x.title,
                              "why": "describes a third-party compromise but records no supplier"})
            continue
        single, all_victims = customer_of(x.dedup_id)
        if any(sv.same_org(g.resolver, x.vendors[0], v) for v in all_victims):
            # The supplier's own event: it anchors an incident of its own.
            inc = incident_of[x.dedup_id] = registry.incident(x.vendors[0], x.event_date, x.dedup_id,
                                                               supplier_event=x.dedup_id)
            if inc.supplier_entity_id is None:
                inc.supplier_entity_id = _entity_id(conn, g.index, x.vendors[0])
            continue
        customer_events.append((x, single, all_victims))
    for x, single, all_victims in customer_events:
        vendor = x.vendors[0]
        inc = incident_of[x.dedup_id] = registry.incident(vendor, x.event_date, x.dedup_id, apart=apart)
        if inc.supplier_entity_id is None:
            inc.supplier_entity_id = _entity_id(conn, g.index, vendor)
        if single is None:
            if not all_victims and any(l.customer_event_id == x.dedup_id for l in registry.links.values()):
                continue        # a part split out this run: linked already, its roles not yet classified
            ambiguous.append({"deduplicated_event_id": x.dedup_id, "title": x.title, "victims": all_victims,
                              "why": ("records no victim" if not all_victims else
                                      "records several victims: one event per customer is needed first")})
            continue
        registry.link(Link(inc.supplier_incident_id, single[0], single[1], x.dedup_id, BASIS_VENDOR, None, "rule"))

    pair_ok = sv.accepted(pscores.final, labels, sv.KIND_PAIR, pcut)
    for p in g.pairs:
        how = pair_ok.get(p.pair_id)
        inc = incident_of.get(p.supplier_id)
        if not how or inc is None:
            continue
        single, all_victims = customer_of(p.other_id)
        if any(sv.same_org(g.resolver, inc.supplier_name, v) for v in all_victims):
            if inc.supplier_event_id is None and not any(
                    i.supplier_event_id == p.other_id for i in registry.incidents.values()):
                inc.supplier_event_id = p.other_id
            continue
        if single is None:
            ambiguous.append({"deduplicated_event_id": p.other_id, "victims": all_victims,
                              "why": f"judged the same incident as {p.supplier_id[:8]} but records "
                                     + ("no victim" if not all_victims else "several victims")})
            continue
        edate = conn.execute("SELECT event_date FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                             (p.other_id,)).fetchone()
        if edate and edate[0]:
            inc.dates.add(edate[0])
        inc.events.add(p.other_id)
        decided = "label" if how == "label" else ("escalation" if p.pair_id in pscores.escalated else "jev")
        registry.link(Link(inc.supplier_incident_id, single[0], single[1], p.other_id,
                           BASIS_LABEL if how == "label" else BASIS_JUDGED, pscores.final.get(p.pair_id), decided))

    victim_ok = sv.accepted(vscores.final, labels, sv.KIND_VICTIM, vcut)
    flat = {c.subject_id: c for cs in g.candidates.values() for c in cs}
    for sid, how in sorted(victim_ok.items()):
        cand = flat.get(sid)
        if cand is None or cand.dedup_id not in incident_of:
            continue
        inc, x = incident_of[cand.dedup_id], by_id[cand.dedup_id]
        if sv.same_org(g.resolver, inc.supplier_name, cand.name):
            continue
        if sv.variant_of_victim(conn, x.dedup_id, cand.entity_id, cand.name, g.resolver) or any(
                sv.same_org(g.resolver, cand.name, v[1]) for v in sv.event_victims(conn, x.dedup_id)):
            continue
        if sv.nearby_own_events(cand, x, own):
            # Its own event decides: linked above when judged the same incident, otherwise reported.
            if not any(l.customer_entity_id == cand.entity_id and l.customer_event_id
                       for l in registry.links.values() if l.supplier_incident_id == inc.supplier_incident_id):
                ambiguous.append({"deduplicated_event_id": x.dedup_id, "customer": cand.name,
                                  "why": "named as affected, but its own event near the incident was not judged "
                                         "the same supplier incident"})
            continue
        entity_id = cand.entity_id if cand.entity_id is not None else sv._entity_for(conn, cand, g.index)
        decided = "label" if how == "label" else ("escalation" if sid in vscores.escalated else "jev")
        registry.link(Link(inc.supplier_incident_id, entity_id, cand.name, None, BASIS_NAMED,
                           vscores.final.get(sid), decided))

    anchored = {i.supplier_event_id for i in registry.incidents.values() if i.supplier_event_id}
    for inc in registry.incidents.values():
        if inc.supplier_event_id is None:
            found = _supplier_event(conn, registry, inc, own, names, exclude=anchored)
            if found:
                inc.supplier_event_id = found
                anchored.add(found)
    return {"ambiguous": ambiguous}


# ----------------------------------------------------------------------
# Converting the old state: attachments and cross-customer events
# ----------------------------------------------------------------------

def touched_events(conn: sqlite3.Connection) -> Dict[str, Set[str]]:
    """Active events the supplier step joined across customers: event id -> the customers it attached there."""
    from cyber_data_collector.dedup.supplier_victims import PAIR_VERSION

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    touched: Dict[str, Set[str]] = defaultdict(set)
    if "SupplierVictimAttachments" in tables:
        for did, name in conn.execute("SELECT deduplicated_event_id, entity_name FROM SupplierVictimAttachments"):
            touched[did].add(name)
    if "DedupDecisions" in tables:
        for (did,) in conn.execute("SELECT DISTINCT deduplicated_event_id FROM DedupDecisions "
                                   "WHERE action = 'merge' AND reasoning LIKE ?", (f"%{PAIR_VERSION}%",)):
            touched[did]
    if "DedupSnapshots" in tables:
        for (did,) in conn.execute("SELECT DISTINCT deduplicated_event_id FROM DedupSnapshots "
                                   "WHERE batch_id LIKE 'supplier-victims-%'"):
            touched[did]
    active = {r[0] for r in conn.execute("SELECT deduplicated_event_id FROM DeduplicatedEvents "
                                         "WHERE status = 'Active'")}
    return {d: names for d, names in touched.items() if d in active}


def reversed_customers(conn: sqlite3.Connection, dedup_id: str) -> Set[str]:
    """Customers the supplier step once attached to this event as separate victims, since reversed."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'SupplierVictimReversals'").fetchone():
        return set()
    return {r[0] for r in conn.execute("SELECT entity_name FROM SupplierVictimReversals "
                                       "WHERE deduplicated_event_id = ? AND action = 'reversed'", (dedup_id,))}


def _record_orgs(conn: sqlite3.Connection, dedup_id: str) -> List[Dict[str, Any]]:
    """Each member record, the organisation its Perplexity research names (None when it names none), and the
    organisations the record's own entity links give as harmed (``victim`` / ``affected_customer``)."""
    from cyber_data_collector.dedup.coherence import _formal_name

    harmed: Dict[str, List[str]] = defaultdict(list)
    for rid, name in conn.execute(
            """SELECT ee.enriched_event_id, v.entity_name FROM EventDeduplicationMap m
               JOIN EnrichedEventEntities ee ON ee.enriched_event_id = m.enriched_event_id
               JOIN EntitiesV2 v ON v.entity_id = ee.entity_id
               WHERE m.deduplicated_event_id = ? AND ee.relationship_type IN ('victim', 'affected_customer')""",
            (dedup_id,)):
        harmed[rid].append(name)
    return [{"id": rid, "org": _formal_name(pe), "harmed": harmed.get(rid, [])} for rid, pe in conn.execute(
        """SELECT m.enriched_event_id, e.perplexity_enrichment_data FROM EventDeduplicationMap m
           JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
           WHERE m.deduplicated_event_id = ? ORDER BY m.enriched_event_id""", (dedup_id,))]


@dataclass
class SplitPlan:
    event_id: str
    stays: str                                       # the customer whose records stay
    leaving: Dict[str, List[str]]                    # customer -> its records that leave
    ambiguous: List[str] = field(default_factory=list)


def plan_splits(conn: sqlite3.Connection, resolver, vendors_of: Dict[str, List[str]]) -> Tuple[
        List[SplitPlan], List[Dict[str, Any]]]:
    """Per candidate event, the records of each customer it holds; a plan when it holds two or more
    customers, or the supplier and a customer.

    Candidates: events the supplier step touched (with the customers it attached there), and detected
    supplier incidents with a recorded supplier (whose customers are their recorded victims).
    """
    from cyber_data_collector.dedup.supplier_victims import same_org

    # A split that was undone because both parts recorded the same victim is not made again
    # (split_rulings.same_victim_supplier_rulings): Block and Afterpay would otherwise part every run.
    try:
        undone = {frozenset(r) for r in conn.execute(
            "SELECT left_enriched_event_id, right_enriched_event_id FROM DedupOverrides WHERE active = 0 "
            "AND reason LIKE '%[void: both records'' events now record the same victim%'")}
    except sqlite3.OperationalError:       # no rulings table, or one without active / reason
        undone = set()
    candidates: Dict[str, Set[str]] = {d: set(n) for d, n in touched_events(conn).items()}
    for did, vendors in vendors_of.items():
        if vendors:
            candidates.setdefault(did, set())
    plans, ambiguous = [], []
    for did, attached in sorted(candidates.items()):
        # Customers are the organisations the calibrated supplier-victim judgement attached here as separate
        # victims, and the event's own recorded victim - not every victim role. Role classification marks parents,
        # subsidiaries, regional arms, lists and descriptions as victims (Block for Afterpay, Medlab for Australian
        # Clinical Labs, "Ingram Micro Holding Corporation" for Ingram Micro, Nissan for Nissan Oceania, "five
        # Australian firms"), and each became a "customer" split off its own incident.
        scalar = conn.execute("SELECT victim_organization_name FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                              (did,)).fetchone()
        customers = set(attached) | ({scalar[0]} if scalar and scalar[0] else set())
        vendors = vendors_of.get(did, [])
        # One customer under two names ("NSW Health" / "NSW Ministry of Health") is one customer.
        distinct: List[str] = []
        for c in sorted(customers, key=lambda c: (len(c), c)):
            if not any(same_org(resolver, c, v) for v in vendors) and not any(
                    same_org(resolver, c, d) for d in distinct):
                distinct.append(c)
        customers = set(distinct)
        groups: Dict[str, List[str]] = defaultdict(list)
        supplier_records: List[str] = []
        unclear = []
        for m in _record_orgs(conn, did):
            # Whose record it is: the customer its own entity links give as harmed, when exactly one is. In a
            # supplier breach Perplexity's ``formal_entity_name`` is the breached supplier even for a customer's
            # own notice (Latrobe City Council's and Boroondara's OracleCMS notices, 7 of SA's Frontier payroll
            # records), so it decides only for a record whose links name no customer. A record naming several
            # customers is general coverage and stays.
            linked = sorted({c for c in customers for h in m["harmed"]
                             if not any(same_org(resolver, h, v) for v in vendors) and same_org(resolver, h, c)})
            if len(linked) == 1:
                groups[linked[0]].append(m["id"])
                continue
            if linked or not m["org"]:
                continue
            if any(same_org(resolver, m["org"], v) for v in vendors):
                supplier_records.append(m["id"])
                continue
            hits = sorted({c for c in customers if same_org(resolver, m["org"], c)})
            if len(hits) == 1:
                groups[hits[0]].append(m["id"])
            elif len(hits) > 1:
                unclear.append(f"{m['id'][:8]} names {m['org']!r}, which fits {', '.join(hits)}")
        scalar = conn.execute("SELECT victim_organization_name FROM DeduplicatedEvents "
                              "WHERE deduplicated_event_id = ?", (did,)).fetchone()
        own = next((c for c in sorted(customers) if scalar and scalar[0] and same_org(resolver, c, scalar[0])), None)
        if len(groups) + (1 if supplier_records else 0) < 2:
            # The event's own victim is a customer even when none of its records names it in a matching
            # form ("Medical information leaked in NSW Health hack": its NSW Health records name "New South
            # Wales Ministry of Health" or nothing, beside three Transport for NSW records). Then only the
            # customers this module attached here as separate victims, and reversed, leave: the attachment
            # was the calibrated judgement that they are other organisations, where a name test is not
            # (Tasmania's DECYP under two spellings, SA Health and its department).
            separate = reversed_customers(conn, did)
            leaving = {c: r for c, r in groups.items() if c != own and c in separate}
            if own and own not in groups and leaving:
                plans.append(SplitPlan(did, own, {c: sorted(r) for c, r in leaving.items()}))
            continue
        if unclear:
            ambiguous.append({"deduplicated_event_id": did, "why": "a record fits two customers: " + "; ".join(unclear)})
            continue
        # The party with the most records keeps the event: a tie goes to the supplier (its own records then
        # keep it as the supplier's event), then to the event's own recorded victim, then to the customer of
        # the event's master record. One Frontier-named page no longer takes SA's 20 records' event from it.
        master = conn.execute("SELECT master_enriched_event_id FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                              (did,)).fetchone()
        master = master[0] if master else None
        parties = {c: len(r) for c, r in groups.items()}
        if supplier_records:
            parties[vendors[0]] = len(supplier_records)
        stays = sorted(parties, key=lambda p: (-parties[p], p != (vendors[0] if supplier_records else None),
                                                p != own, master not in groups.get(p, ()), p))[0]
        staying = {m["id"] for m in _record_orgs(conn, did)} - {r for c, rs in groups.items() if c != stays for r in rs}
        leaving = {c: sorted(r) for c, r in groups.items() if c != stays
                   and not any(frozenset((x, y)) in undone for x in r for y in staying)}
        if leaving:
            plans.append(SplitPlan(did, stays, leaving))
    return plans, ambiguous


def record_majority(conn: sqlite3.Connection, resolver, dedup_id: str) -> Optional[str]:
    """The organisation at least two thirds (and at least two) of the event's naming records name, if any."""
    from cyber_data_collector.dedup.supplier_victims import same_org

    orgs = [m["org"] for m in _record_orgs(conn, dedup_id) if m["org"]]
    groups: List[List[str]] = []
    for org in orgs:
        group = next((g for g in groups if same_org(resolver, g[0], org)), None)
        if group is None:
            groups.append([org])
        else:
            group.append(org)
    best = max(groups, key=len) if groups else []
    return best[0] if len(best) >= 2 and 3 * len(best) >= 2 * len(orgs) else None


def merged_customer_plans(conn: sqlite3.Connection, resolver) -> List[SplitPlan]:
    """Undo the earlier version's cross-customer merges where the event's own records say whose it is.

    The supplier step merged "the same supplier incident recorded under another customer" into one event
    (``DedupDecisions`` reasoning naming :data:`supplier_victims.PAIR_VERSION`). ASIC's Accellion event took in
    NSW Health's events that way, and its victim was later rewritten to NSW Health. Each merge batch snapshots
    the source event, so the records each source brought in are known. A source's records still in the event
    leave together as its customer's event when all of these hold - each a recorded fact, not a name test:

    * the event's own organisation is what two thirds of its naming records name (:func:`record_majority`;
      the scalar victim is not used, it was rewritten), and the source's customer is another organisation;
    * the merge carried that customer across as a separate victim (``SupplierVictimAttachments``
      ``decided_by`` ``merge``) and the conversion reversed it (``SupplierVictimReversals``, reversed or
      restored) - the calibrated judgement that it was another customer;
    * the customer is not a list of organisations, and none of the source's records names the event's own
      organisation.

    A source with no recorded victim, or only a description, stays.
    """
    from cyber_data_collector.dedup import descriptive_names
    from cyber_data_collector.dedup.entity_merge import is_organisation_list
    from cyber_data_collector.dedup.supplier_victims import PAIR_VERSION, same_org

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {"DedupDecisions", "DedupSnapshots", "SupplierVictimAttachments", "SupplierVictimReversals"} <= tables:
        return []
    batches: Dict[str, Set[str]] = defaultdict(set)
    for batch, target in conn.execute(
            "SELECT DISTINCT batch_id, deduplicated_event_id FROM DedupDecisions WHERE action = 'merge' "
            "AND reasoning LIKE ?", (f"%{PAIR_VERSION}%",)):
        batches[batch].add(target)
    status = dict(conn.execute("SELECT deduplicated_event_id, status FROM DeduplicatedEvents"))
    members: Dict[str, Set[str]] = defaultdict(set)
    for record, did in conn.execute("SELECT enriched_event_id, deduplicated_event_id FROM EventDeduplicationMap"):
        members[did].add(record)
    carried: Dict[str, Set[str]] = defaultdict(set)
    for did, name in conn.execute(
            """SELECT a.deduplicated_event_id, a.entity_name FROM SupplierVictimAttachments a
               JOIN SupplierVictimReversals r ON r.attachment_id = a.attachment_id
               WHERE a.decided_by = 'merge' AND r.action IN ('reversed', 'restored')"""):
        carried[did].add(name)
    descriptions = descriptive_names.described(conn)
    owns: Dict[str, Optional[str]] = {}
    leaving: Dict[str, Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
    for batch, targets in sorted(batches.items()):
        for did, payload in conn.execute("SELECT deduplicated_event_id, payload_json FROM DedupSnapshots "
                                         "WHERE batch_id = ?", (batch,)):
            if did in targets or status.get(did) != "Merged":
                continue
            try:
                snap = json.loads(payload)
            except (ValueError, TypeError):
                continue
            customer = (snap.get("event") or {}).get("victim_organization_name")
            if (not customer or descriptive_names.is_listed(descriptions, customer)
                    or is_organisation_list(customer)):
                continue
            brought = {m.get("enriched_event_id") for m in snap.get("members", []) if isinstance(m, dict)}
            for target in targets:
                still = brought & members.get(target, set())
                if status.get(target) != "Active" or not still:
                    continue
                if not any(same_org(resolver, customer, c) for c in carried.get(target, ())):
                    continue
                if target not in owns:
                    owns[target] = record_majority(conn, resolver, target)
                own = owns[target]
                if not own or is_organisation_list(own) or same_org(resolver, customer, own):
                    continue
                if any(m["org"] and same_org(resolver, m["org"], own)
                       for m in _record_orgs(conn, target) if m["id"] in still):
                    continue
                key = next((c for c in leaving[target] if same_org(resolver, c, customer)), customer)
                leaving[target][key] |= still
    return [SplitPlan(t, owns[t], {c: sorted(r) for c, r in groups.items()}) for t, groups in sorted(leaving.items())]


def _entity_named(conn: sqlite3.Connection, g, name: str, event_id: Optional[str] = None) -> Optional[int]:
    """The entity of ``name``: the one the event already records for it (victim or attached), else by name."""
    from cyber_data_collector.dedup.supplier_victims import same_org

    if event_id:
        for entity_id, recorded in conn.execute(
                """SELECT dee.entity_id, v.entity_name FROM DeduplicatedEventEntities dee
                   JOIN EntitiesV2 v USING (entity_id) WHERE dee.deduplicated_event_id = ?
                   UNION SELECT entity_id, entity_name FROM SupplierVictimAttachments
                   WHERE deduplicated_event_id = ?""", (event_id, event_id)):
            if recorded == name or same_org(g.resolver, recorded, name):
                return entity_id
    hit = g.index.find(name)
    return hit[0] if hit else None


def _link_split(conn: sqlite3.Connection, g, registry: Registry, plan: SplitPlan,
                new_event_of: Dict[Tuple[str, str], str], vendors_of: Dict[str, List[str]],
                ambiguous: List[Dict[str, Any]]) -> None:
    """Record the supplier on each part that left and link every part to the supplier's incident."""
    from cyber_data_collector.dedup import supplier_victims as sv

    vendors = vendors_of.get(plan.event_id) or []
    if not vendors:
        ambiguous.append({"deduplicated_event_id": plan.event_id,
                          "why": "split per customer, but the event records no supplier to link the parts to"})
        return
    vendor = vendors[0]
    date = conn.execute("SELECT event_date FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                        (plan.event_id,)).fetchone()
    date = date[0] if date else None
    if sv.same_org(g.resolver, plan.stays, vendor):
        inc = registry.incident(vendor, date, plan.event_id, supplier_event=plan.event_id)
    else:
        inc = registry.incident(vendor, date, plan.event_id)
        entity = _entity_named(conn, g, plan.stays, plan.event_id)
        if entity is not None:
            registry.link(Link(inc.supplier_incident_id, entity, plan.stays, plan.event_id, BASIS_SPLIT, None, "rule"))
    if inc.supplier_entity_id is None:
        inc.supplier_entity_id = _entity_named(conn, g, vendor)
    for customer in plan.leaving:
        new = new_event_of[(plan.event_id, customer)]
        conn.execute("UPDATE DeduplicatedEvents SET vendor_organization_name = ? WHERE deduplicated_event_id = ? "
                     "AND (vendor_organization_name IS NULL OR vendor_organization_name = '')", (vendor, new))
        entity = _entity_named(conn, g, customer, plan.event_id)
        if entity is None:
            ambiguous.append({"deduplicated_event_id": new, "customer": customer,
                              "why": "split out, but no entity row holds the customer's name"})
            continue
        inc.events.add(new)
        registry.link(Link(inc.supplier_incident_id, entity, customer, new, BASIS_SPLIT, None, "rule"))


def demote_split_customers(conn: sqlite3.Connection, apply: bool = True) -> List[Dict[str, Any]]:
    """A customer split out of an event as its own event is no longer a victim of the event it left.

    The split moved the customer's records; its victim role stayed on the origin wherever another record there
    still mentions it, so the by-entity view listed each customer twice: Australian Conservation Foundation under
    Pareto Phone's own event, Macquarrie Corporation under Auto-IT's, City of Ballarat under the OracleCMS event,
    TasTAFE under Instructure's Global Canvas event (TasTAFE is a victim of the Tasmanian education event split
    from it), Stake under DriveWealth's. Each such role on the origin becomes ``affected_customer`` - the supplier
    link records the relationship - when the customer is a victim of an Active event split from that origin.
    Idempotent; ``apply`` False rolls back.
    """
    from cyber_data_collector.dedup.victim_fill import names_match

    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'SupplierLinkActions'").fetchone():
        return []
    victims: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
    for did, eid, name in conn.execute(
            """SELECT dee.deduplicated_event_id, dee.entity_id, v.entity_name FROM DeduplicatedEventEntities dee
               JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
               JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id
               WHERE dee.relationship_type = 'victim' AND d.status = 'Active'"""):
        victims[did].append((eid, name))
    scalar = dict(conn.execute("SELECT deduplicated_event_id, victim_organization_name FROM DeduplicatedEvents"))
    split_from: Dict[str, Set[str]] = defaultdict(set)
    for origin, new in conn.execute("SELECT deduplicated_event_id, new_event_id FROM SupplierLinkActions "
                                    "WHERE action = 'split' AND new_event_id IS NOT NULL"):
        split_from[origin].add(resolve_event(conn, new) or new)
    done = []
    for origin, parts in sorted(split_from.items()):
        if origin not in victims:
            continue
        theirs = [name for p in parts if p != origin for _, name in victims.get(p, [])]
        for eid, name in victims[origin]:
            # The origin's own recorded victim is not a departed customer, whatever its parts record.
            if scalar.get(origin) and names_match(conn, name, scalar[origin]):
                continue
            if any(names_match(conn, name, t) for t in theirs):
                conn.execute("UPDATE DeduplicatedEventEntities SET relationship_type = 'affected_customer' "
                             "WHERE deduplicated_event_id = ? AND entity_id = ? AND relationship_type = 'victim'",
                             (origin, eid))
                done.append({"deduplicated_event_id": origin, "customer": name})
    if apply:
        conn.commit()
    else:
        conn.rollback()
    return done


def convert(conn: sqlite3.Connection, g, registry: Registry, apply: bool, split: bool = False) -> Dict[str, Any]:
    """Reverse the old attachments into links (``apply``) and correct the roles of customers already split out.

    Splitting events joined across customers was the one-off conversion of 2026-10-31. Its role list treated
    parents, subsidiaries, regional arms and lists as customers, so further splits are proposals (``splits`` with
    ``new_event`` None) unless ``split`` is set too.
    """
    from cyber_data_collector.dedup import supplier_victims as sv
    from cyber_data_collector.dedup.ledger import DedupLedger

    vendors_of = {x.dedup_id: x.vendors for x in g.incidents}
    rows = {r[0]: r for r in conn.execute(
        "SELECT attachment_id, probability, decided_by FROM SupplierVictimAttachments")} \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'SupplierVictimAttachments'").fetchone() else {}
    sv.restore_own_victims(conn, apply=apply)       # a reversal never takes the event's own victim
    reversed_ = sv.reverse_attachments(conn, apply=apply)
    plans, ambiguous = plan_splits(conn, g.resolver, vendors_of)
    # Cross-customer merges of the earlier version are undone first; a record leaves once.
    merged = merged_customer_plans(conn, g.resolver)
    taken = {r for p in merged for rs in p.leaving.values() for r in rs}
    plans = merged + [p for p in (SplitPlan(p.event_id, p.stays, {c: [r for r in rs if r not in taken]
                                                                  for c, rs in p.leaving.items()
                                                                  if set(rs) - taken}) for p in plans)
                      if p.leaving]
    splits: List[Dict[str, Any]] = []
    new_event_of: Dict[Tuple[str, str], str] = {}
    if apply and split:
        ledger = DedupLedger(conn)
        previous = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            for plan in plans:
                for customer, records in sorted(plan.leaving.items()):
                    reason = (f"[supplier-split] {sv.OWNER_DECISION}: the records of {customer} leave "
                              f"{plan.event_id[:8]} as their own event")
                    new = ledger.split_members(plan.event_id, records, reason=reason, actor="rule")
                    new_event_of[(plan.event_id, customer)] = new
                    conn.execute("INSERT INTO SupplierLinkActions VALUES (?, 'split', ?, ?, NULL, ?, ?, ?)",
                                 (str(uuid.uuid4()), plan.event_id, new,
                                  json.dumps({"customer": customer, "records": records, "stays": plan.stays}),
                                  sv.OWNER_DECISION, _now()))
                    splits.append({"event": plan.event_id, "customer": customer, "records": len(records),
                                   "new_event": new, "stays": plan.stays})
                _link_split(conn, g, registry, plan, new_event_of, vendors_of, ambiguous)
            conn.commit()
        finally:
            conn.row_factory = previous
    else:
        splits = [{"event": p.event_id, "customer": c, "records": len(r), "new_event": None, "stays": p.stays}
                  for p in plans for c, r in sorted(p.leaving.items())]

    # Each reversed (or still-present, when planning) attachment becomes a link row.
    own = sv.victim_events(conn)
    dates = dict(conn.execute("SELECT deduplicated_event_id, event_date FROM DeduplicatedEvents"))
    vendor_names: Dict[str, Set[str]] = defaultdict(set)
    for did_, vendor in conn.execute(
            """SELECT deduplicated_event_id, vendor_organization_name FROM DeduplicatedEvents
               WHERE vendor_organization_name IS NOT NULL AND vendor_organization_name != ''
               UNION SELECT dee.deduplicated_event_id, v.entity_name FROM DeduplicatedEventEntities dee
               JOIN EntitiesV2 v USING (entity_id) WHERE dee.relationship_type = 'vendor'"""):
        vendor_names[did_].add(vendor)
    converted = 0
    for item in reversed_:
        did = item["deduplicated_event_id"]
        vendors = vendors_of.get(did) or []
        if not vendors:
            ambiguous.append({"deduplicated_event_id": did, "customer": item["victim"],
                              "why": "attachment reversed, but its event records no supplier to link it to"})
            continue
        inc = registry.incident(vendors[0], dates.get(did), did)
        entity_id = conn.execute("SELECT entity_id FROM SupplierVictimAttachments WHERE attachment_id = ?",
                                 (item["attachment_id"],)).fetchone()[0]
        event = new_event_of.get((did, item["victim"]))
        if event is None:
            # Only an own event of this supplier's breach: another of the customer's incidents is not it.
            near = [(sv._gap(dates.get(did), e[1]), e[0]) for e in own.get(entity_id, [])
                    if e[0] != did and any(sv.same_org(g.resolver, vendors[0], v) for v in vendor_names.get(e[0], ()))]
            near = sorted((gap if gap is not None else sv.OWN_EVENT_WINDOW_DAYS + 1, e) for gap, e in near
                          if gap is None or gap <= sv.OWN_EVENT_WINDOW_DAYS)
            event = near[0][1] if near else None
        row = rows.get(item["attachment_id"])
        registry.link(Link(inc.supplier_incident_id, entity_id, item["victim"], event, BASIS_CONVERTED,
                           row[1] if row else None, (row[2] if row else None) or "attachment"))
        converted += 1
    demoted = demote_split_customers(conn, apply=apply)
    return {"attachments_reversed": reversed_, "attachments_converted": converted, "splits": splits,
            "ambiguous": ambiguous, "split_customers_demoted": demoted}


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------

def supplier_incident_summary(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Per supplier incident: the supplier, its event, its customers' Active events and named customers."""
    ensure_schema(conn)
    previous = conn.row_factory
    conn.row_factory = None
    resolver = None
    try:
        titles = dict(conn.execute("SELECT deduplicated_event_id, title FROM DeduplicatedEvents"))
        out = []
        for sid, name, supplier_event, first, last in conn.execute(
                """SELECT supplier_incident_id, supplier_name, supplier_event_id, first_date, last_date
                   FROM SupplierIncidents ORDER BY supplier_name, first_date"""):
            customers, named = [], []
            for cname, event, basis, p in conn.execute(
                    """SELECT customer_name, customer_event_id, basis, probability FROM SupplierIncidentLinks
                       WHERE supplier_incident_id = ? ORDER BY customer_name""", (sid,)):
                now = resolve_event(conn, event, resolver)
                if now:
                    customers.append({"customer": cname, "event": now, "title": titles.get(now), "basis": basis,
                                      "probability": p})
                else:
                    named.append({"customer": cname, "basis": basis, "probability": p,
                                  "stored_event": event})
            sup = resolve_event(conn, supplier_event, resolver)
            out.append({"supplier_incident_id": sid, "supplier": name, "supplier_event": sup,
                        "supplier_event_title": titles.get(sup) if sup else None,
                        "customer_events": customers, "named_customers_without_event": named,
                        "first_date": first, "last_date": last})
        return out
    finally:
        conn.row_factory = previous
