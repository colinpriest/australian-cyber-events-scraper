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
import re
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
    if held is None:
        # A merge moves the merged event's mapped records to the event it joined, so its master record is what
        # still names it (Nissan's split-off event add9ab5b, merged back into 05a642a0, resolved to nothing).
        held = conn.execute(
            """SELECT m.deduplicated_event_id FROM DeduplicatedEvents x
               JOIN EventDeduplicationMap m ON m.enriched_event_id = x.master_enriched_event_id
               JOIN DeduplicatedEvents d ON d.deduplicated_event_id = m.deduplicated_event_id
               WHERE x.deduplicated_event_id = ? AND d.status = 'Active'
               ORDER BY m.deduplicated_event_id LIMIT 1""", (event_id,)).fetchone()
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
        from cyber_data_collector.dedup.multi_customer_incidents import pair_verdicts
        from cyber_data_collector.dedup.supplier_victims import OWN_EVENT_WINDOW_DAYS

        ensure_schema(conn)
        self.conn, self.resolver, self.window = conn, resolver, OWN_EVENT_WINDOW_DAYS
        self.verdicts = pair_verdicts(conn)
        self.incidents: Dict[str, Incident] = {}
        for row in conn.execute(
                """SELECT supplier_incident_id, supplier_key, supplier_name, supplier_entity_id, supplier_event_id,
                          first_date, last_date FROM SupplierIncidents"""):
            self.incidents[row[0]] = Incident(row[0], row[1], row[2], supplier_entity_id=row[3],
                                              supplier_event_id=row[4], first_date=row[5], last_date=row[6],
                                              stored=True)
        self.links: Dict[Tuple[str, int], Link] = {}
        self.stored_links: Dict[Tuple[str, int], Tuple] = {}
        # Incident -> the customers (entity ids and lower-case names) its events' evidence names this run, accepted
        # or not (:func:`plan_links`): what a named-only link of that incident can rest on.
        self.named_in: Dict[str, Set] = defaultdict(set)
        # Where each customer event is linked already (supplier key, resolved event) -> incident: a placed
        # event stays in its incident, so a later run cannot file it under a second incident of the supplier.
        self.placed: Dict[Tuple[str, str], str] = {}
        for row in conn.execute(
                """SELECT supplier_incident_id, customer_entity_id, customer_name, customer_event_id, basis,
                          probability, decided_by FROM SupplierIncidentLinks"""):
            self.stored_links[(row[0], row[1])] = row
            inc = self.incidents.get(row[0])
            if row[3] and inc is not None:
                event = resolve_event(conn, row[3]) or row[3]
                self.placed.setdefault((inc.supplier_key, event), row[0])
                inc.events.add(event)       # so a pair label against a stored member keeps an event out

    def key(self, supplier: str) -> str:
        """The supplier's key; an organisation already stored under another spelling (:func:`is_supplier`) keeps that
        incident's key."""
        key = self.resolver.key_for(supplier)
        if any(inc.supplier_key == key for inc in self.incidents.values()):
            return key
        matches = [(inc.supplier_name, inc.supplier_key) for inc in self.incidents.values()
                   if is_supplier(self.resolver, self.verdicts, inc.supplier_name, supplier)]
        best = closest_name(supplier, [name for name, _key in matches])
        return next(k for name, k in sorted(matches) if name == best) if best is not None else key

    def incident(self, supplier: str, event_date: Optional[str], anchor: str,
                 supplier_event: Optional[str] = None, apart=None, create: bool = True) -> Optional[Incident]:
        """The incident an event belongs to.

        ``supplier_event``: the supplier's own event, which anchors its own incident (never shared with
        another of the supplier's events). Otherwise the supplier's incident nearest ``event_date`` within
        the window, else a new one - or None when ``create`` is False. ``apart(a, b)`` True means two events
        were ruled different incidents (a pair label); an incident holding such an event is not joined.
        """
        from cyber_data_collector.dedup.supplier_victims import OWN_EVENT_WINDOW_DAYS, _gap

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
        placed = self.incidents.get(self.placed.get((key, anchor), ""))
        if placed is not None and not (apart is not None and any(
                apart(anchor, e) for e in placed.events | {placed.supplier_event_id} if e)):
            placed.events.add(anchor)
            if event_date:
                placed.dates.add(event_date)
            return placed
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
        elif not create:
            return None
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

    def write(self, prune_named: bool = False) -> Dict[str, int]:
        """Upsert incidents and links; a row changes only when what it records changes.

        ``prune_named``: this run planned every customer only named in the reporting (no limit, no failed
        judgement). A stored named-only link (not a label's) has moved, and is removed, when this run planned that
        customer under another incident and nothing the incident's own events say names it any more. The councils
        of OracleCMS's report, filed under Oracle by an earlier run, stayed there after the report was filed under
        OracleCMS: links were only ever added. A customer named under two suppliers (Stanford University under
        Accellion and Instructure) stays under both; a named link no run re-plans is left alone.
        """
        now = _now()
        counts = {"incidents_created": 0, "incidents_updated": 0, "links_created": 0, "links_updated": 0}
        if prune_named:
            counts["named_links_removed"] = 0
            planned_in = defaultdict(set)
            for sid, ent in self.links:
                planned_in[ent].add(sid)
            for key, row in list(self.stored_links.items()):
                if key in self.links or row[3] is not None or row[4] != BASIS_NAMED or row[6] == "label":
                    continue
                if not planned_in.get(key[1], set()) - {key[0]}:
                    continue
                if key[1] in self.named_in.get(key[0], ()) or (row[2] or "").strip().lower() in self.named_in.get(
                        key[0], ()):
                    continue
                self.conn.execute("DELETE FROM SupplierIncidentLinks WHERE supplier_incident_id = ? "
                                  "AND customer_entity_id = ?", key)
                del self.stored_links[key]
                counts["named_links_removed"] += 1
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


def _event_vendors(conn: sqlite3.Connection, event_id: str) -> List[str]:
    """The suppliers an event records: its vendor column and its vendor and service-provider roles (a service
    firm is no vendor, owner 2026-11-04, but its clients' events are linked to it the same way)."""
    row = conn.execute("SELECT vendor_organization_name FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                       (event_id,)).fetchone()
    names = [row[0]] if row and row[0] else []
    return names + [r[0] for r in conn.execute(
        """SELECT v.entity_name FROM DeduplicatedEventEntities dee JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
           WHERE dee.deduplicated_event_id = ? AND dee.relationship_type IN ('vendor', 'service_provider')""",
        (event_id,))]


def _compact(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def closest_name(name: str, candidates: Sequence[str]) -> Optional[str]:
    """The candidate nearest ``name``: the same letters ignoring spaces and punctuation first ("Oracle CMS" is
    "OracleCMS"), then the nearest in length, then the first in order. ``same_org`` joined "Oracle CMS" with both
    "Oracle" and "OracleCMS", and the first stored incident by id (Oracle's) took the councils' report."""
    if not candidates:
        return None
    target = _compact(name)
    return min(sorted(candidates), key=lambda c: (_compact(c) != target, abs(len(_compact(c)) - len(target))))


_CONFLICT_VERDICTS = ("different", "related", "parent_subsidiary")
_STATED_BASIS = "stated in the record"


def placement_date(conn: sqlite3.Connection, event_id: str, event_date: Optional[str]) -> Optional[str]:
    """The date an event is placed under a supplier's incident by: its own date, unless that is only the date it was
    published or found ("may lag the incident") and one of its records states an earlier incident date in its own
    text - then the earliest such. Research dates are not used: they can describe another of the organisation's
    incidents. Singtel's report of data from Accellion's January 2021 attack, posted in October 2022, is dated by its
    publication; placed by that date it started a second Accellion attack (owner, 2026-11-03)."""
    from cyber_data_collector.dedup import date_provenance

    if not event_date or "date_basis" not in {c[1] for c in conn.execute("PRAGMA table_info(DeduplicatedEvents)")}:
        return event_date
    row = conn.execute("SELECT date_basis FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                       (event_id,)).fetchone()
    if not row or not row[0] or "may lag" not in row[0]:
        return event_date
    stated = sorted(r["date"] for r in date_provenance.event_dates(conn, event_id, max_records=10_000)["records"]
                    if r["date"] and r["basis"] == _STATED_BASIS and r["date"] < event_date[:10])
    return stated[0] if stated else event_date


def _name_parts(name: str) -> Set[str]:
    """A name and, for "X (Y)", X and Y: "Oracle Customer Management Solutions (OracleCMS)" is also both."""
    whole = name.strip().lower()
    parts = {whole}
    match = re.fullmatch(r"(.+?)\s*\(([^()]+)\)", whole)
    if match:
        parts.update(p.strip() for p in match.groups() if p.strip())
    return parts


def is_supplier(resolver, verdicts: Dict[frozenset, str], supplier: str, name: str) -> bool:
    """Whether ``name`` is the supplier itself: ruled ``same`` in a supplier incident's context
    (``alias_learning.verify_supplier_pairs``: "Oracle Customer Management Solutions (OracleCMS)" for OracleCMS), or
    one organisation by name (``same_org``) when no verdict rules them apart. Verdicts are read for the names and for
    the parts of an "X (Y)" name: "Oracle" and "Oracle Customer Management Solutions" are recorded as different, and
    the name test took "Oracle Customer Management Solutions (OracleCMS)" for Oracle. ``verdicts`` is
    :func:`multi_customer_incidents.pair_verdicts`."""
    from cyber_data_collector.dedup import supplier_victims as sv

    if not name or not supplier:
        return False
    found = {verdicts.get(frozenset((a, b))) for a in _name_parts(supplier) for b in _name_parts(name) if a != b}
    if found & set(_CONFLICT_VERDICTS):
        return False
    return "same" in found or sv.same_org(resolver, supplier, name)


def anchors_incident(conn: sqlite3.Connection, resolver, verdicts: Dict[frozenset, str], supplier: str,
                     event_id: str) -> bool:
    """Whether an event can be the supplier's own event (the anchor of its incident).

    It must record the supplier as its vendor or be the supplier's report (:func:`_supplier_report`): its recorded
    victim, or its only victim. Nissan's April 2024 notice records Nissan as its victim and OracleCMS only as a
    co-victim, and anchored a second OracleCMS incident that then could not merge with the first. Jetstar's
    cancellations after the CrowdStrike outage record CrowdStrike in no role and anchored CrowdStrike's card. A
    supplier's breach report naming its customers as victims (Cisco's, Instructure's) is an anchor.
    """
    if any(is_supplier(resolver, verdicts, supplier, v) for v in _event_vendors(conn, event_id)):
        return True
    return _supplier_report(conn, resolver, verdicts, supplier, event_id)


def _supplier_report(conn: sqlite3.Connection, resolver, verdicts: Dict[frozenset, str], supplier: str,
                     event_id: str) -> bool:
    """Whether the event is the supplier's own report: its recorded victim (the victim field) is the supplier, or,
    with no victim field, the supplier is its only victim role."""
    row = conn.execute("SELECT victim_organization_name FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                       (event_id,)).fetchone()
    if row and row[0]:
        return is_supplier(resolver, verdicts, supplier, row[0])
    return _only_victim(conn, resolver, verdicts, supplier, event_id)


def _only_victim(conn: sqlite3.Connection, resolver, verdicts: Dict[frozenset, str], supplier: str,
                 event_id: str) -> bool:
    """Whether the supplier is the event's only recorded victim (its victim field and its victim roles)."""
    from cyber_data_collector.dedup import supplier_victims as sv

    row = conn.execute("SELECT victim_organization_name FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                       (event_id,)).fetchone()
    victims = ([row[0]] if row and row[0] else []) + [v[1] for v in sv.event_victims(conn, event_id)]
    return bool(victims) and all(is_supplier(resolver, verdicts, supplier, v) for v in victims)


def _event_names(conn: sqlite3.Connection, event_id: str) -> List[Tuple[Optional[int], str]]:
    """``(entity_id, name)`` of every organisation an event records, in any role, and its victim field."""
    row = conn.execute("SELECT victim_organization_name FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                       (event_id,)).fetchone()
    return ([(None, row[0])] if row and row[0] else []) + [tuple(r) for r in conn.execute(
        """SELECT dee.entity_id, v.entity_name FROM DeduplicatedEventEntities dee
           JOIN EntitiesV2 v ON v.entity_id = dee.entity_id WHERE dee.deduplicated_event_id = ?""", (event_id,))]


def _records_supplier(conn: sqlite3.Connection, resolver, verdicts: Dict[frozenset, str], supplier: str,
                      event_id: str) -> bool:
    """Whether an event records the supplier in any role, its victim field or its vendor field."""
    return any(is_supplier(resolver, verdicts, supplier, n) for _e, n in _event_names(conn, event_id)) or any(
        is_supplier(resolver, verdicts, supplier, v) for v in _event_vendors(conn, event_id))


def _split_origins(conn: sqlite3.Connection, event_id: str) -> Set[str]:
    """The Active events a per-customer split of ``event_id`` came from (``SupplierLinkActions``): the event it was
    split out of, or itself when it is the part that stayed."""
    out: Set[str] = set()
    for origin, new in conn.execute("SELECT deduplicated_event_id, new_event_id FROM SupplierLinkActions "
                                    "WHERE action = 'split' AND (new_event_id = ? OR deduplicated_event_id = ?)",
                                    (event_id, event_id)):
        now = resolve_event(conn, origin)
        if now:
            out.add(now)
    return out


def repair_registry(conn: sqlite3.Connection, resolver, labels: Optional[Dict] = None,
                    pair_labels: Optional[Sequence[Tuple[str, str]]] = None) -> Dict[str, Any]:
    """Undo drift in the stored supplier incidents before a run places anything (no commit; the caller's).

    Links are only ever added, so two kinds of drift outlived their cause:

    * a ``vendor-recorded`` link whose event no longer records the supplier (NSW Education's July 2021
      attack, once filed as an Accellion customer event, still anchored an Accellion incident of its own
      after its vendor was cleared). Its basis is gone, so the link is removed;
    * one customer event linked under two incidents of the same supplier (Allens, ASIC and NSW Health in two
      Accellion incidents; Canva and McGraw Hill in two Salesforce incidents): a later run placed it by
      nearest date into another incident. Incidents that share a customer event are one incident and are
      merged: into the one with the supplier's own event, else the earliest, keeping the stronger link per
      customer. Two incidents that each have the supplier's own event (Ivanti's separate campaigns), or
      whose events a pair label rules different, are not merged; the shared event is reported. An incident's
      anchor counts as one of its events, and only an anchor on the supplier's own report (the supplier its only
      victim) keeps an incident apart.

    * a link whose customer is the supplier's own organisation (``same_org``): a supplier is not its own customer.
      v7 recorded Nissan Oceania as the vendor of Nissan's own breach, and the per-customer split filed Nissan under
      a "Nissan Oceania" supplier incident;
    * incidents of one organisation under two spellings ("Oracle CMS", "OracleCMS"; ``key_for`` differs) are given
      one supplier key, so they are grouped (and merged when they share an event) like any other pair;
    * a supplier event that can no longer anchor the incident (:func:`anchors_incident`), or that lies farther
      from the customers' events than a linked report of the supplier's own, is replaced by that report, else
      cleared; the customers only named in its reporting go with it (a run re-adds those still supported).
      Nissan's notice, which records OracleCMS beside Nissan, anchored a second OracleCMS incident;
      Jetstar's cancellations after the CrowdStrike outage, which now record neither CrowdStrike nor Jetstar,
      anchored CrowdStrike's;
    * a link to an event that a pair label (``evaluation/duplicate_labels.csv``, ``pair_labels`` in tests) rules a
      different incident from another of the incident's events, when it records the supplier in no role and the
      other does: Nissan's December 2023 attack on its own systems, linked to the OracleCMS incident by an old
      co-victim attachment, is a different incident from Nissan's report of the April 2024 OracleCMS breach (owner,
      2026-11-01);
    * a link from a same-incident judgement whose customer event no longer records the customer in any role: the
      event is no longer that customer's report (Jetstar under CrowdStrike). A judged link names the event's own
      victim entity; split and converted links name the customer as the reporting spelled it, so a spelling the
      event's roles do not share ("Department for Education, Children and Young People") is not evidence;
    * a ``split-customer`` link when neither its event nor the event it was split from (``SupplierLinkActions``)
      records the supplier any longer, in any role or field. The split was made because that event recorded the
      supplier as vendor; once role classification no longer does, the basis is gone. Ticketek's May 2024 breach
      was split from an event that had recorded Outabox (another breach of that month, mentioned as background) as
      its vendor, and stayed listed under Outabox after the vendor was corrected (owner, 2026-11-02). Stake keeps its
      DriveWealth link: the event it was split from is DriveWealth's own.

    An incident is removed with its links once nothing supports it: not the supplier's own event, not a link from a
    label or a same-incident judgement, and no linked event that still records the supplier as its vendor. The
    other links (rule placements, splits, conversions, named customers) all came from some event recording the
    supplier as vendor, so they lapse with it. The "Delta Air Lines" incident of 16 named customers outlived
    Delta's correction from CrowdStrike's vendor to a co-victim, ESET's and the Singapore agency's their removal as
    vendors, and the "Nissan Oceania" incident the correction of Nissan's arm from Nissan's vendor.
    """
    from cyber_data_collector.dedup import supplier_victims as sv
    from cyber_data_collector.dedup.multi_customer_incidents import pair_verdicts

    ensure_schema(conn)
    verdicts = pair_verdicts(conn)
    report: Dict[str, Any] = {"stale_links_removed": [], "incidents_merged": [], "incidents_removed": [],
                              "shared_not_merged": [], "own_organisation_links_removed": [], "keys_joined": [],
                              "keys_reset": [], "own_events_replaced": [], "labelled_links_removed": []}
    incidents = {r[0]: {"key": r[1], "name": r[2], "event": r[3], "first": r[4]} for r in conn.execute(
        "SELECT supplier_incident_id, supplier_key, supplier_name, supplier_event_id, first_date FROM SupplierIncidents")}
    # An event the owner labelled not the supplier's is in no incident of it, whatever the link's basis.
    from cyber_data_collector.dedup import supplier_role_labels

    role_labels = supplier_role_labels.not_suppliers(conn)
    for sid, entity_id, event_id in conn.execute(
            "SELECT supplier_incident_id, customer_entity_id, customer_event_id FROM SupplierIncidentLinks "
            "WHERE customer_event_id IS NOT NULL").fetchall():
        inc = incidents.get(sid)
        now = resolve_event(conn, event_id)
        if inc and now and supplier_role_labels.contradicts(conn, role_labels, now, inc["name"]):
            conn.execute("DELETE FROM SupplierIncidentLinks WHERE supplier_incident_id = ? AND customer_entity_id = ? "
                         "AND customer_event_id = ?", (sid, entity_id, event_id))
            report["labelled_links_removed"].append({"supplier_incident_id": sid, "supplier": inc["name"],
                                                     "customer_event_id": now})
    for sid, entity_id, name, event_id in conn.execute(
            "SELECT supplier_incident_id, customer_entity_id, customer_name, customer_event_id FROM SupplierIncidentLinks "
            "WHERE basis = ? AND customer_event_id IS NOT NULL", (BASIS_VENDOR,)).fetchall():
        inc = incidents.get(sid)
        now = resolve_event(conn, event_id)
        if inc is None or now is None:
            continue
        if any(sv.same_org(resolver, inc["name"], v) for v in _event_vendors(conn, now)):
            continue
        if any(sv.same_org(resolver, inc["name"], v[1]) for v in sv.event_victims(conn, now)):
            continue
        conn.execute("DELETE FROM SupplierIncidentLinks WHERE supplier_incident_id = ? AND customer_entity_id = ?",
                     (sid, entity_id))
        report["stale_links_removed"].append({"supplier_incident_id": sid, "supplier": inc["name"],
                                              "customer": name, "event": now})

    for sid, entity_id, name, event_id in conn.execute(
            "SELECT supplier_incident_id, customer_entity_id, customer_name, customer_event_id FROM SupplierIncidentLinks "
            "WHERE basis = ? AND customer_event_id IS NOT NULL", (BASIS_JUDGED,)).fetchall():
        now = resolve_event(conn, event_id)
        if sid not in incidents or now is None:
            continue
        recorded = _event_names(conn, now)
        if any(e is not None and e == entity_id for e, _n in recorded) or any(
                sv.same_org(resolver, name, n) for _e, n in recorded):
            continue
        conn.execute("DELETE FROM SupplierIncidentLinks WHERE supplier_incident_id = ? AND customer_entity_id = ?",
                     (sid, entity_id))
        report["stale_links_removed"].append({"supplier_incident_id": sid, "supplier": incidents[sid]["name"],
                                              "customer": name, "event": now,
                                              "why": "the event no longer records the customer"})

    for sid, entity_id, name, event_id in conn.execute(
            "SELECT supplier_incident_id, customer_entity_id, customer_name, customer_event_id FROM SupplierIncidentLinks "
            "WHERE basis = ? AND customer_event_id IS NOT NULL", (BASIS_SPLIT,)).fetchall():
        inc = incidents.get(sid)
        now = resolve_event(conn, event_id)
        if inc is None or now is None:
            continue
        if any(_records_supplier(conn, resolver, verdicts, inc["name"], ev)
               for ev in {now} | _split_origins(conn, event_id)):
            continue
        conn.execute("DELETE FROM SupplierIncidentLinks WHERE supplier_incident_id = ? AND customer_entity_id = ?",
                     (sid, entity_id))
        report["stale_links_removed"].append({"supplier_incident_id": sid, "supplier": inc["name"],
                                              "customer": name, "event": now,
                                              "why": "neither the event nor the event it was split from records the "
                                                     "supplier"})

    for sid, entity_id, name in conn.execute(
            "SELECT supplier_incident_id, customer_entity_id, customer_name FROM SupplierIncidentLinks").fetchall():
        inc = incidents.get(sid)
        if inc is None or not sv.same_org(resolver, inc["name"], name):
            continue
        conn.execute("DELETE FROM SupplierIncidentLinks WHERE supplier_incident_id = ? AND customer_entity_id = ?",
                     (sid, entity_id))
        report["own_organisation_links_removed"].append({"supplier_incident_id": sid, "supplier": inc["name"],
                                                         "customer": name})

    # Keys are worked out afresh from the names, not kept: a key taken from another supplier's incident outlives the
    # match that gave it. Three OracleCMS incidents carried Oracle's key "oracle", so Oracle's 2025 cloud breach
    # (Optus's event) was filed under OracleCMS, and "Oracle Customer Management Solutions (OracleCMS)" kept a key of
    # its own, so its incident and OracleCMS's both held Nissan's April 2024 notice.
    # Worked out before the merges below (spellings of one supplier group together) and again after them (an
    # incident merged away no longer lends its key); written once, at the end.
    stored_key = {sid: inc["key"] for sid, inc in incidents.items()}

    def assign_keys() -> Set[str]:
        """Each incident's key from its name, joined to the closest stored spelling of the same supplier."""
        for sid, inc in incidents.items():
            inc["key"] = resolver.key_for(inc["name"]) or stored_key[sid]
        first_name: Dict[str, str] = {}
        joined_ids: Set[str] = set()
        for sid, inc in sorted(incidents.items(), key=lambda kv: (kv[1]["key"], kv[0])):
            matches = {n: k for k, n in first_name.items()
                       if k != inc["key"] and is_supplier(resolver, verdicts, n, inc["name"])}
            closest = closest_name(inc["name"], list(matches))
            joined = matches[closest] if closest is not None else None
            if joined is None:
                first_name.setdefault(inc["key"], inc["name"])
                continue
            inc["key"] = joined
            joined_ids.add(sid)
        return joined_ids

    assign_keys()

    from cyber_data_collector.dedup.supplier_victims import OWN_EVENT_WINDOW_DAYS, _gap

    def date_of(event_id: str) -> Optional[str]:
        row = conn.execute("SELECT event_date FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                           (event_id,)).fetchone()
        return row[0] if row else None

    # A rule placement made by a publication date the event's own record contradicts moves to the supplier's
    # incident nearest the stated date: placed events otherwise stay put, and Singtel's report stayed in the second
    # Accellion incident its publication date had started.
    for sid, entity_id, name, event_id in conn.execute(
            "SELECT supplier_incident_id, customer_entity_id, customer_name, customer_event_id FROM SupplierIncidentLinks "
            "WHERE basis = ? AND customer_event_id IS NOT NULL ORDER BY supplier_incident_id, customer_entity_id",
            (BASIS_VENDOR,)).fetchall():
        inc = incidents.get(sid)
        now = resolve_event(conn, event_id) or event_id
        stored = date_of(now)
        when = placement_date(conn, now, stored)
        if inc is None or not when or when == stored:
            continue
        own = _gap(when, inc["first"]) if inc["first"] else None
        nearer = [(gap, other) for other, o in incidents.items() if other != sid and o["key"] == inc["key"]
                  for gap in [_gap(when, o["first"]) if o["first"] else None]
                  if gap is not None and gap <= OWN_EVENT_WINDOW_DAYS and (own is None or gap < own)]
        if not nearer:
            continue
        conn.execute("DELETE FROM SupplierIncidentLinks WHERE supplier_incident_id = ? AND customer_entity_id = ?",
                     (sid, entity_id))
        report["stale_links_removed"].append({
            "supplier_incident_id": sid, "supplier": inc["name"], "customer": name, "event": now,
            "why": f"its record states {when}, nearer the supplier's incident {min(nearer)[1]}"})

    for sid, inc in sorted(incidents.items()):
        if inc["event"] is None:
            continue
        now = resolve_event(conn, inc["event"])
        valid = now is not None and anchors_incident(conn, resolver, verdicts, inc["name"], now)
        taken = {resolve_event(conn, i["event"]) or i["event"] for k, i in incidents.items() if k != sid and i["event"]}
        linked = [(ent, resolve_event(conn, ev) or ev) for ent, ev in conn.execute(
            "SELECT customer_entity_id, customer_event_id FROM SupplierIncidentLinks WHERE supplier_incident_id = ? "
            "AND customer_event_id IS NOT NULL ORDER BY customer_event_id", (sid,)).fetchall()]
        events = sorted({ev for _ent, ev in linked})

        def distance(ev: str) -> int:
            """Days from the event to the nearest other linked event (an undated one is the farthest)."""
            gaps = [g for g in (_gap(date_of(ev), date_of(o)) for o in events if o != ev) if g is not None]
            return min(gaps) if gaps else OWN_EVENT_WINDOW_DAYS + 1

        # A linked report of the supplier's own takes over from an anchor that no longer is one, or from one farther
        # from the customers' events: Cisco's October 2024 incident had been re-anchored on a Cisco advisory of
        # September 2025. A customer's event that names the supplier as vendor is never adopted.
        reports = sorted((ev for ev in events if ev not in taken
                          and _supplier_report(conn, resolver, verdicts, inc["name"], ev)),
                         key=lambda ev: (not _only_victim(conn, resolver, verdicts, inc["name"], ev), distance(ev), ev))
        best = reports[0] if reports else None
        if valid and (best is None or best == now or distance(best) >= distance(now)):
            continue
        adopted = best
        conn.execute("UPDATE SupplierIncidents SET supplier_event_id = ?, updated_at = ? WHERE supplier_incident_id = ?",
                     (adopted, _now(), sid))
        if adopted is not None and _only_victim(conn, resolver, verdicts, inc["name"], adopted):
            for ent, ev in linked:          # the supplier's own event is not a customer's
                if ev == adopted:
                    conn.execute("DELETE FROM SupplierIncidentLinks WHERE supplier_incident_id = ? "
                                 "AND customer_entity_id = ?", (sid, ent))
        # Customers only named in the reporting were gathered and judged with the old anchor's evidence: six
        # OracleCMS councils were filed under the Oracle Cloud incident anchored on OracleCMS's report. They go
        # with it; a run re-adds those its incident's current evidence supports.
        conn.execute("DELETE FROM SupplierIncidentLinks WHERE supplier_incident_id = ? AND customer_event_id IS NULL "
                     "AND basis = ? AND decided_by != 'label'", (sid, BASIS_NAMED))
        report["own_events_replaced"].append({"supplier_incident_id": sid, "supplier": inc["name"],
                                              "was": inc["event"], "now": adopted})
        inc["event"] = adopted

    different: Set[str] = set()
    for (kind, pair), label in (labels or {}).items():
        if kind == sv.KIND_PAIR and label in sv.NEGATIVE[sv.KIND_PAIR]:
            a, _, b = pair.partition("|")
            ra, rb = resolve_event(conn, a) or a, resolve_event(conn, b) or b
            different.add("|".join(sorted((ra, rb))))
    from cyber_data_collector.dedup import label_contradictions

    if pair_labels is None:
        pair_labels = label_contradictions.labelled_different_pairs()
    known = {r[0] for r in conn.execute("SELECT deduplicated_event_id FROM DeduplicatedEvents")}
    pair_labels = [(a, b) for a, b in pair_labels if a in known and b in known]   # labels of this database
    if pair_labels:
        sides = label_contradictions.SideResolver(conn)
        for a, b in pair_labels:
            ra, rb = sides.resolve(a)[0], sides.resolve(b)[0]
            if ra and rb and ra != rb:
                different.add("|".join(sorted((ra, rb))))

    def events_of(sid: str) -> Set[str]:
        out = {resolve_event(conn, r[0]) or r[0] for r in conn.execute(
            "SELECT customer_event_id FROM SupplierIncidentLinks WHERE supplier_incident_id = ? "
            "AND customer_event_id IS NOT NULL", (sid,))}
        own = incidents[sid]["event"]
        return out | ({resolve_event(conn, own) or own} if own else set())

    def own_report(sid: str) -> bool:
        """The incident is anchored on the supplier's own report (the supplier its only victim), not on a
        customer's event that names it as vendor."""
        own = incidents[sid]["event"]
        now = resolve_event(conn, own) if own else None
        return now is not None and _only_victim(conn, resolver, verdicts, incidents[sid]["name"], now)

    changed = True
    while changed:
        changed = False
        by_event: Dict[Tuple[str, str], List[str]] = defaultdict(list)
        for sid, event_id in conn.execute(
                "SELECT supplier_incident_id, customer_event_id FROM SupplierIncidentLinks "
                "WHERE customer_event_id IS NOT NULL ORDER BY supplier_incident_id").fetchall():
            if sid in incidents:
                by_event[(incidents[sid]["key"], resolve_event(conn, event_id) or event_id)].append(sid)
        # An incident's anchor is one of its events too: OracleCMS's own report, linked as a customer event of the
        # incident anchored on the councils' report, was given an incident of its own by a later run.
        for sid, inc in sorted(incidents.items()):
            if inc["event"]:
                by_event[(inc["key"], resolve_event(conn, inc["event"]) or inc["event"])].append(sid)
        for (key, event_id), sids in sorted(by_event.items()):
            sids = sorted(set(sids))
            if len(sids) < 2:
                continue
            a, b = sorted(sids[:2], key=lambda s: (not own_report(s), incidents[s]["event"] is None,
                                                   incidents[s]["first"] or "9999", s))
            ea, eb = events_of(a), events_of(b)
            conflicts = [(x, y) for x in ea for y in eb if x != y and "|".join(sorted((x, y))) in different]
            if conflicts and not (incidents[a]["event"] and incidents[b]["event"]):
                # A label outranks the date window: an event placed by vendor-recorded rule that a pair label rules
                # a different incident from the other side's events leaves (NSW Education's July 2021 attack).
                dropped = _drop_rule_links(conn, {a, b}, conflicts)
                if dropped:
                    report["stale_links_removed"].extend(
                        {"supplier_incident_id": sid, "supplier": incidents[sid]["name"], "customer": name,
                         "event": ev, "why": "a pair label rules it a different incident"} for sid, name, ev in dropped)
                    changed = True
                    break
            # Two of the supplier's own reports are two incidents (Ivanti's separate campaigns); an anchor on a
            # customer's event that names the supplier as vendor does not keep an incident apart.
            blocked = ("both have the supplier's own event" if own_report(a) and own_report(b) else
                       "a pair label rules their events different" if conflicts else None)
            if blocked:
                note = {"event": event_id, "incidents": [a, b], "why": blocked}
                if note not in report["shared_not_merged"]:
                    report["shared_not_merged"].append(note)
                continue
            _merge_incident(conn, keep=a, drop=b)
            report["incidents_merged"].append({"supplier": incidents[a]["name"], "kept": a, "merged": b,
                                               "shared_event": event_id})
            incidents.pop(b)
            changed = True
            break

    def records_supplier(event_id: str, supplier: str) -> bool:
        return _records_supplier(conn, resolver, verdicts, supplier, event_id)

    for sid, inc in sorted(incidents.items()):
        evs = sorted(events_of(sid))
        losers: Set[str] = set()
        for i, x in enumerate(evs):
            for y in evs[i + 1:]:
                if "|".join(sorted((x, y))) not in different:
                    continue
                rx, ry = records_supplier(x, inc["name"]), records_supplier(y, inc["name"])
                if rx != ry:
                    losers.add(y if rx else x)
        for ent, name, ev, basis in conn.execute(
                "SELECT customer_entity_id, customer_name, customer_event_id, basis FROM SupplierIncidentLinks "
                "WHERE supplier_incident_id = ? AND customer_event_id IS NOT NULL", (sid,)).fetchall():
            now = resolve_event(conn, ev) or ev
            if now in losers and basis != BASIS_LABEL:
                conn.execute("DELETE FROM SupplierIncidentLinks WHERE supplier_incident_id = ? "
                             "AND customer_entity_id = ?", (sid, ent))
                report["stale_links_removed"].append({
                    "supplier_incident_id": sid, "supplier": inc["name"], "customer": name, "event": now,
                    "why": "a pair label rules it a different incident from an event that records the supplier"})

    for sid, inc in list(incidents.items()):
        if _supported(conn, resolver, sid, inc["name"], inc["event"]):
            continue
        named = [r[0] for r in conn.execute("SELECT customer_name FROM SupplierIncidentLinks "
                                            "WHERE supplier_incident_id = ? ORDER BY customer_name", (sid,))]
        conn.execute("DELETE FROM SupplierIncidentLinks WHERE supplier_incident_id = ?", (sid,))
        conn.execute("DELETE FROM SupplierIncidents WHERE supplier_incident_id = ?", (sid,))
        report["incidents_removed"].append({"supplier_incident_id": sid, "supplier": inc["name"],
                                            "named_customers_dropped": named})
        incidents.pop(sid)
    joined_ids = assign_keys()
    for sid, inc in sorted(incidents.items()):
        if inc["key"] == stored_key[sid]:
            continue
        conn.execute("UPDATE SupplierIncidents SET supplier_key = ?, updated_at = ? WHERE supplier_incident_id = ?",
                     (inc["key"], _now(), sid))
        report["keys_joined" if sid in joined_ids else "keys_reset"].append(
            {"supplier_incident_id": sid, "supplier": inc["name"], "key": inc["key"], "was": stored_key[sid]})
    # The span is what is linked now, not what a removed or merged link once contributed.
    for sid in incidents:
        linked = sorted(events_of(sid))
        if not linked:
            continue
        dates = sorted(r[0] for r in conn.execute(
            "SELECT event_date FROM DeduplicatedEvents WHERE status = 'Active' AND event_date IS NOT NULL "
            f"AND deduplicated_event_id IN ({','.join('?' * len(linked))})", linked))
        if dates:
            conn.execute("UPDATE SupplierIncidents SET first_date = ?, last_date = ?, updated_at = ? "
                         "WHERE supplier_incident_id = ? AND NOT (first_date IS ? AND last_date IS ?)",
                         (dates[0], dates[-1], _now(), sid, dates[0], dates[-1]))
    return report


def _supported(conn: sqlite3.Connection, resolver, sid: str, supplier: str, own_event: Optional[str]) -> bool:
    """Whether anything still supports a supplier incident (see :func:`repair_registry`)."""
    from cyber_data_collector.dedup import supplier_victims as sv

    if own_event and resolve_event(conn, own_event):
        return True
    events: Set[str] = set()
    for event_id, basis in conn.execute("SELECT customer_event_id, basis FROM SupplierIncidentLinks "
                                        "WHERE supplier_incident_id = ? AND customer_event_id IS NOT NULL", (sid,)):
        now = resolve_event(conn, event_id)
        if now is None:
            continue
        if basis in (BASIS_LABEL, BASIS_JUDGED):
            return True
        events.add(now)
    return any(sv.same_org(resolver, supplier, v) for e in sorted(events) for v in _event_vendors(conn, e))


def _drop_rule_links(conn: sqlite3.Connection, sids: Set[str], conflicts: List[Tuple[str, str]]
                     ) -> List[Tuple[str, str, str]]:
    """Remove the ``vendor-recorded`` links, in ``sids``, of events on one side of a labelled-different pair whose
    other side was linked on stronger evidence (a label or a judgement). Returns (incident, customer, event)."""
    marks = ",".join("?" * len(sids))
    links = [(sid, ent, name, resolve_event(conn, ev) or ev, basis) for sid, ent, name, ev, basis in conn.execute(
        f"SELECT supplier_incident_id, customer_entity_id, customer_name, customer_event_id, basis "
        f"FROM SupplierIncidentLinks WHERE supplier_incident_id IN ({marks}) AND customer_event_id IS NOT NULL",
        sorted(sids)).fetchall()]
    basis_of: Dict[str, Set[str]] = defaultdict(set)
    for _sid, _ent, _name, ev, basis in links:
        basis_of[ev].add(basis)
    weak = {ev for ev, bases in basis_of.items() if bases == {BASIS_VENDOR}}
    losers = {x for pair in conflicts for x, y in (pair, pair[::-1]) if x in weak and y not in weak}
    dropped = []
    for sid, ent, name, ev, _basis in links:
        if ev in losers:
            conn.execute("DELETE FROM SupplierIncidentLinks WHERE supplier_incident_id = ? AND customer_entity_id = ?",
                         (sid, ent))
            dropped.append((sid, name, ev))
    return dropped


def _merge_incident(conn: sqlite3.Connection, keep: str, drop: str) -> None:
    """Move ``drop``'s links into ``keep`` (the stronger link per customer; a known event is never lost)."""
    cols = "supplier_incident_id, customer_entity_id, customer_name, customer_event_id, basis, probability, decided_by"
    kept = {r[1]: r for r in conn.execute(f"SELECT {cols} FROM SupplierIncidentLinks WHERE supplier_incident_id = ?",
                                          (keep,))}
    for row in conn.execute(f"SELECT {cols} FROM SupplierIncidentLinks WHERE supplier_incident_id = ?",
                            (drop,)).fetchall():
        old = kept.get(row[1])
        if old is None:
            conn.execute("UPDATE SupplierIncidentLinks SET supplier_incident_id = ?, updated_at = ? "
                         "WHERE supplier_incident_id = ? AND customer_entity_id = ?", (keep, _now(), drop, row[1]))
            continue
        if _rank_row(row) > _rank_row(old):
            conn.execute("""UPDATE SupplierIncidentLinks SET customer_name = ?, customer_event_id = ?, basis = ?,
                                   probability = ?, decided_by = ?, updated_at = ?
                            WHERE supplier_incident_id = ? AND customer_entity_id = ?""",
                         (row[2], row[3] or old[3], row[4], row[5], row[6], _now(), keep, row[1]))
        elif old[3] is None and row[3] is not None:
            conn.execute("UPDATE SupplierIncidentLinks SET customer_event_id = ?, updated_at = ? "
                         "WHERE supplier_incident_id = ? AND customer_entity_id = ?", (row[3], _now(), keep, row[1]))
    conn.execute("DELETE FROM SupplierIncidentLinks WHERE supplier_incident_id = ?", (drop,))
    dates = sorted(d for r in conn.execute("SELECT first_date, last_date FROM SupplierIncidents "
                                           "WHERE supplier_incident_id IN (?, ?)", (keep, drop)) for d in r if d)
    if dates:
        conn.execute("UPDATE SupplierIncidents SET first_date = ?, last_date = ?, updated_at = ? "
                     "WHERE supplier_incident_id = ?", (dates[0], dates[-1], _now(), keep))
    conn.execute("DELETE FROM SupplierIncidents WHERE supplier_incident_id = ?", (drop,))


# ----------------------------------------------------------------------
# Planning links from detection, labels and calibrated judgements
# ----------------------------------------------------------------------

def _entity_id(conn: sqlite3.Connection, index, name: str) -> Optional[int]:
    hit = index.find(name)
    return hit[0] if hit else None


def _supplier_event(conn, registry: Registry, inc: Incident, own: Dict[int, List[Tuple[str, Optional[str]]]],
                    names: Dict[int, str], exclude: Set[str] = frozenset(),
                    verdicts: Optional[Dict[frozenset, str]] = None) -> Optional[str]:
    """The supplier's own Active event (:func:`anchors_incident`) nearest the incident, if any (not one anchoring
    another incident)."""
    from cyber_data_collector.dedup.supplier_victims import _gap, same_org

    best: Optional[Tuple[int, str]] = None
    for entity_id, events in own.items():
        if not same_org(registry.resolver, inc.supplier_name, names.get(entity_id, "")):
            continue
        for did, edate in events:
            if did in exclude or not anchors_incident(conn, registry.resolver, verdicts or {}, inc.supplier_name, did):
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
    from cyber_data_collector.dedup.multi_customer_incidents import pair_verdicts

    from cyber_data_collector.dedup import supplier_role_labels

    ambiguous: List[Dict[str, Any]] = []
    own = sv.victim_events(conn)
    verdicts = pair_verdicts(conn)
    names = dict(conn.execute("SELECT entity_id, entity_name FROM EntitiesV2"))
    by_id = g.by_id
    incident_of: Dict[str, Incident] = {}
    role_labels = supplier_role_labels.not_suppliers(conn)

    def customer_of(did: str, supplier: Optional[str] = None) -> Tuple[Optional[Tuple[int, str]], List[str]]:
        """The event's one customer and all its victims; with ``supplier``, the supplier recorded beside its
        customer as a co-victim is not one of them."""
        victims = [v for v in sv.event_victims(conn, did)
                   if supplier is None or not is_supplier(g.resolver, verdicts, supplier, v[1])]
        return ((victims[0][0], victims[0][1]) if len(victims) == 1 else None), [v[1] for v in victims]

    def own_of(supplier: str, all_victims: List[str]) -> bool:
        return bool(all_victims) and all(is_supplier(g.resolver, verdicts, supplier, v) for v in all_victims)

    different = set()
    for (kind, sid), label in labels.items():
        if kind == sv.KIND_PAIR and label in sv.NEGATIVE[sv.KIND_PAIR]:
            a, _, b = sid.partition("|")
            different.add(sid)      # as labelled, and as the events are now (labels name events as they were)
            different.add("|".join(sorted((resolve_event(conn, a) or a, resolve_event(conn, b) or b))))

    def apart(a: str, b: str) -> bool:
        return "|".join(sorted((a, b))) in different

    customer_events = []
    for x in g.incidents:
        if not x.vendors:
            ambiguous.append({"deduplicated_event_id": x.dedup_id, "title": x.title,
                              "why": "describes a third-party compromise but records no supplier"})
            continue
        if own_of(x.vendors[0], customer_of(x.dedup_id)[1]):
            # The supplier's own event: it anchors an incident of its own.
            inc = incident_of[x.dedup_id] = registry.incident(x.vendors[0], x.event_date, x.dedup_id,
                                                               supplier_event=x.dedup_id)
            if inc.supplier_entity_id is None:
                inc.supplier_entity_id = _entity_id(conn, g.index, x.vendors[0])
            continue
        single, all_victims = customer_of(x.dedup_id, x.vendors[0])
        customer_events.append((x, single, all_victims))
    # An event that records no victim names no customer, so it never starts an incident; it joins one of the
    # supplier's incidents near its date, placed first from the events that do name a customer. A Whitehorse City
    # Council report of OracleCMS's April 2024 breach, misdated 2022-05-11 and with no victim recorded, made an
    # incident of its own, and the councils its reporting named were listed under a "2022" attack.
    customer_events.sort(key=lambda item: not item[2])
    for x, single, all_victims in customer_events:
        vendor = x.vendors[0]
        if supplier_role_labels.contradicts(conn, role_labels, x.dedup_id, vendor):
            continue        # the owner labelled this event not one of the supplier's
        inc = registry.incident(vendor, placement_date(conn, x.dedup_id, x.event_date), x.dedup_id, apart=apart,
                                create=bool(all_victims))
        if inc is None:
            ambiguous.append({"deduplicated_event_id": x.dedup_id, "title": x.title, "victims": [],
                              "why": "records no victim, and no incident of its supplier is near its date"})
            continue
        incident_of[x.dedup_id] = inc
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
        if own_of(inc.supplier_name, customer_of(p.other_id)[1]):
            if inc.supplier_event_id is None and not any(
                    i.supplier_event_id == p.other_id for i in registry.incidents.values()):
                inc.supplier_event_id = p.other_id
            continue
        if supplier_role_labels.contradicts(conn, role_labels, p.other_id, inc.supplier_name):
            continue        # the owner labelled this event not one of the supplier's
        single, all_victims = customer_of(p.other_id, inc.supplier_name)
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
    for cand in flat.values():
        if cand.dedup_id in incident_of:
            named = registry.named_in[incident_of[cand.dedup_id].supplier_incident_id]
            named.add(cand.name.strip().lower())
            if cand.entity_id is not None:
                named.add(cand.entity_id)
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
            found = _supplier_event(conn, registry, inc, own, names, exclude=anchored, verdicts=verdicts)
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
    organisations the record's own entity links give as harmed (``victim`` / ``affected_customer``), and the
    kinds recorded for them (``harmed_kinds``)."""
    from cyber_data_collector.dedup.coherence import _formal_name

    harmed: Dict[str, List[str]] = defaultdict(list)
    kinds: Dict[str, Dict[str, Optional[str]]] = defaultdict(dict)
    for rid, name, kind in conn.execute(
            """SELECT ee.enriched_event_id, v.entity_name, v.entity_kind FROM EventDeduplicationMap m
               JOIN EnrichedEventEntities ee ON ee.enriched_event_id = m.enriched_event_id
               JOIN EntitiesV2 v ON v.entity_id = ee.entity_id
               WHERE m.deduplicated_event_id = ? AND ee.relationship_type IN ('victim', 'affected_customer')""",
            (dedup_id,)):
        harmed[rid].append(name)
        kinds[rid][name] = kind
    return [{"id": rid, "org": _formal_name(pe), "harmed": harmed.get(rid, []), "harmed_kinds": kinds.get(rid, {})}
            for rid, pe in conn.execute(
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


def departed_customers(conn: sqlite3.Connection, origin: str) -> List[str]:
    """The victims of Active events split from ``origin`` as their own customer events (``SupplierLinkActions``).

    Such a customer's incident is recorded on its own event, linked to the supplier: on ``origin`` it is an
    affected customer, never a victim (owner decision 2026-10-31)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'SupplierLinkActions'").fetchone():
        return []
    parts = {resolve_event(conn, new) or new for (new,) in conn.execute(
        "SELECT new_event_id FROM SupplierLinkActions WHERE action = 'split' AND new_event_id IS NOT NULL "
        "AND deduplicated_event_id = ?", (origin,))} - {origin}
    names: List[str] = []
    for part in sorted(parts):
        names.extend(name for (name,) in conn.execute(
            """SELECT v.entity_name FROM DeduplicatedEventEntities dee
               JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
               JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id
               WHERE dee.deduplicated_event_id = ? AND dee.relationship_type = 'victim' AND d.status = 'Active'""",
            (part,)))
    return names


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
    named: Dict[str, Set[str]] = defaultdict(set)          # origin -> the customers its split actions recorded
    for origin, new, detail in conn.execute("SELECT deduplicated_event_id, new_event_id, detail_json "
                                            "FROM SupplierLinkActions WHERE action = 'split' AND new_event_id IS NOT NULL"):
        split_from[origin].add(resolve_event(conn, new) or new)
        try:
            customer = (json.loads(detail or "{}") or {}).get("customer")
        except (TypeError, ValueError):
            customer = None
        if customer:
            named[origin].add(customer)
    done = []
    for origin, parts in sorted(split_from.items()):
        if origin not in victims:
            continue
        theirs = [name for p in parts if p != origin for _, name in victims.get(p, [])]
        for eid, name in victims[origin]:
            # The origin's own recorded victim is not a departed customer, whatever its parts record - unless the
            # split recorded it as the customer that left: entity-roles-v10 made Stake the recorded victim of
            # DriveWealth's report, the event Stake's own event was split out of (trial, 2026-11-05).
            departed = any(names_match(conn, name, c) for c in named.get(origin, ()))
            if scalar.get(origin) and names_match(conn, name, scalar[origin]) and not departed:
                continue
            if any(names_match(conn, name, t) for t in theirs):
                conn.execute("UPDATE DeduplicatedEventEntities SET relationship_type = 'affected_customer' "
                             "WHERE deduplicated_event_id = ? AND entity_id = ? AND relationship_type = 'victim'",
                             (origin, eid))
                if scalar.get(origin) and names_match(conn, name, scalar[origin]):
                    left = [n for e, n in victims[origin] if e != eid and not any(names_match(conn, n, t) for t in theirs)]
                    conn.execute("UPDATE DeduplicatedEvents SET victim_organization_name = ? "
                                 "WHERE deduplicated_event_id = ?", (left[0] if len(left) == 1 else None, origin))
                done.append({"deduplicated_event_id": origin, "customer": name})
    if apply:
        conn.commit()
    else:
        conn.rollback()
    return done


# The first version of the rule (2026-11-02) counted victim *names*, not organisations, and trusted a victim
# column left empty or naming the supplier (Perplexity names the supplier even on a customer's own notice).
# "University of Canberra", "UC staff" and "UC students" made UC's own Canvas event Instructure's, and the
# duplicate check then merged it into Instructure's report; Nissan's, Bunnings', PwC's and Discovery Parks' own
# events went the same way, while ZircoDATA's report (victim column Monash Health) was left alone. Its conversions
# are reversed (:func:`reverse_name_counted_reports`) and the rule is :func:`double_counted_customers`.
SUPPLIER_REPORT_DECISION_V1 = ("owner 2026-11-02: a report of a supplier's breach naming several customers is the "
                               "supplier's own event; its customers are affected customers, each counted through its "
                               "own event (the OracleCMS hack report had counted Nissan twice; ZircoDATA's breach had "
                               "Monash Health as a victim)")
OWN_EVENT_DECISION = ("owner 2026-11-02: each customer of a supplier's breach is counted once, through its own event; "
                      "on another event of the breach it is an affected customer")
SUPPLIER_REPORT_DECISION = ("owner 2026-11-02: a report of a supplier's breach whose customers each have their own "
                            "event of it is the supplier's own event")
REPORT_REVERSAL_DECISION = ("owner 2026-11-03: the 2026-11-02 supplier-report rule counted names, not organisations, "
                            "and made customers' own events the supplier's (University of Canberra, Nissan, PwC); "
                            "reversed")


def _vendor_names(conn: sqlite3.Connection) -> Dict[str, Set[str]]:
    """Event -> the names it records as supplier (the vendor column, vendor and service-provider roles)."""
    out: Dict[str, Set[str]] = defaultdict(set)
    for did_, vendor in conn.execute(
            """SELECT deduplicated_event_id, vendor_organization_name FROM DeduplicatedEvents
               WHERE vendor_organization_name IS NOT NULL AND vendor_organization_name != ''
               UNION SELECT dee.deduplicated_event_id, v.entity_name FROM DeduplicatedEventEntities dee
               JOIN EntitiesV2 v USING (entity_id) WHERE dee.relationship_type IN ('vendor', 'service_provider')"""):
        out[did_].add(vendor)
    return out


def _record_supplier_names(conn: sqlite3.Connection) -> Dict[str, Set[str]]:
    """Active event -> the names its active member records label vendor or service provider."""
    from cyber_data_collector.processing.active_evidence import member_condition

    out: Dict[str, Set[str]] = defaultdict(set)
    for did, name in conn.execute(
            f"""SELECT m.deduplicated_event_id, v.entity_name FROM EventDeduplicationMap m
                JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
                JOIN EnrichedEventEntities ee ON ee.enriched_event_id = m.enriched_event_id
                JOIN EntitiesV2 v ON v.entity_id = ee.entity_id
                JOIN DeduplicatedEvents d ON d.deduplicated_event_id = m.deduplicated_event_id
                WHERE d.status = 'Active' AND ee.relationship_type IN ('vendor', 'service_provider')
                  {member_condition(conn, "e")}"""):
        if name:
            out[did].add(name)
    return out


def _victim_organisations(conn: sqlite3.Connection, resolver, verdicts: Dict[frozenset, str],
                          vendors: Dict[str, Set[str]]) -> Dict[str, List[List[Tuple[int, str]]]]:
    """Active event recording a supplier -> its victims, grouped into organisations.

    Left out: a name that is not one organisation ("UC staff", "Bunnings customers", a sector), a listed
    description, and the event's own supplier. Names are one organisation when :func:`victim_fill.names_match`
    says so ("University of Canberra" and "University of Canberra (UC)").
    """
    from cyber_data_collector.dedup import descriptive_names
    from cyber_data_collector.dedup import supplier_victims as sv
    from cyber_data_collector.dedup.victim_fill import names_match

    descriptions = descriptive_names.described(conn)
    names: Dict[str, List[Tuple[int, str, Optional[str]]]] = defaultdict(list)
    for did, eid, name, kind in conn.execute(
            """SELECT dee.deduplicated_event_id, dee.entity_id, v.entity_name, v.entity_kind
               FROM DeduplicatedEventEntities dee JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
               JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id
               WHERE dee.relationship_type = 'victim' AND d.status = 'Active'
               ORDER BY dee.deduplicated_event_id, v.entity_name"""):
        names[did].append((eid, name, kind))
    out: Dict[str, List[List[Tuple[int, str]]]] = {}
    for did, rows in names.items():
        if not vendors.get(did):
            continue
        groups: List[List[Tuple[int, str]]] = []
        for eid, name, kind in rows:
            if not sv.is_candidate_organisation(name, kind) or descriptive_names.is_listed(descriptions, name):
                continue
            if any(is_supplier(resolver, verdicts, s, name) for s in vendors.get(did, ())):
                continue
            home = next((g for g in groups if any(names_match(conn, name, n) for _e, n in g)), None)
            if home is None:
                groups.append([(eid, name)])
            else:
                home.append((eid, name))
        out[did] = groups
    return out


def double_counted_customers(conn: sqlite3.Connection, resolver=None,
                             apply: bool = True) -> Dict[str, List[Dict[str, Any]]]:
    """Each customer of a supplier's breach is a victim of one event of it: its own.

    A customer's *own* event of a breach records the customer as its only victim organisation and the supplier as
    its vendor, within :data:`supplier_victims.OWN_EVENT_WINDOW_DAYS`. On any other event that records the same
    supplier and two or more victim organisations, that customer is an affected customer (``own-event-customer``
    in ``SupplierLinkActions``): ZircoDATA's breach report recorded Monash Health, which has its own notice;
    Instructure's "Aussie schools breach" recorded universities that each have their own Canvas event. When every
    customer of such an event has its own event, the event is the supplier's report and the supplier becomes its
    victim (``supplier-report``). An event that is some customer's own event (one victim organisation) is never
    changed, so two events of one customer cannot take the customer from each other.

    Run by ``find-supplier-victims`` and after every role classification, which re-derives roles. Idempotent;
    an action row is written only for a role that changed. ``apply`` False rolls back.

    Returns:
        ``{"demoted": [...], "reports": [...]}``.
    """
    from cyber_data_collector.dedup import supplier_victims as sv
    from cyber_data_collector.dedup.entity_resolution import EntityResolver
    from cyber_data_collector.dedup.multi_customer_incidents import pair_verdicts
    from cyber_data_collector.dedup.victim_fill import names_match

    from cyber_data_collector.dedup import descriptive_names

    ensure_schema(conn)
    resolver = resolver or EntityResolver(conn)
    verdicts = pair_verdicts(conn)
    vendors = _vendor_names(conn)
    descriptions = descriptive_names.described(conn)
    rows = conn.execute("SELECT deduplicated_event_id, event_date, victim_organization_name, vendor_organization_name, "
                        "title FROM DeduplicatedEvents WHERE status = 'Active'").fetchall()
    dates = {r[0]: r[1] for r in rows}
    scalar = {r[0]: r[2] for r in rows}
    column_vendor = {r[0]: r[3] for r in rows}
    titles = {r[0]: r[4] for r in rows}
    orgs = _victim_organisations(conn, resolver, verdicts, vendors)
    # Own events can only be events that record a supplier; index them by vendor name. An event's records may name
    # a supplier the event does not: Monash Health's own event holds its January 2024 Health Management Systems
    # records and its November 2024 ZircoDATA notices, and the event records only HMS (trial, 2026-11-04).
    record_suppliers = _record_supplier_names(conn)
    by_vendor: Dict[str, List[str]] = defaultdict(list)
    for did, groups in orgs.items():
        if len(groups) == 1:
            for v in set(vendors.get(did, ())) | record_suppliers.get(did, set()):
                by_vendor[v].append(did)
    supplier_cache: Dict[Tuple[str, str], bool] = {}

    def same_supplier(s: str, v: str) -> bool:
        if (s, v) not in supplier_cache:
            supplier_cache[(s, v)] = sv.same_org(resolver, s, v) or is_supplier(resolver, verdicts, s, v)
        return supplier_cache[(s, v)]

    def own_event(x: str, members: List[Tuple[int, str]]) -> Optional[str]:
        candidates = sorted({y for s in vendors.get(x, ()) for v, ys in by_vendor.items() if same_supplier(s, v)
                             for y in ys if y != x})
        for y in candidates:
            if not any(names_match(conn, n, t) for _e, n in members for _f, t in orgs[y][0]):
                continue
            gap = sv._gap(dates.get(x), dates.get(y))
            if gap is None or gap <= sv.OWN_EVENT_WINDOW_DAYS:
                return y
        return None

    def customer_records(did: str, members: List[Tuple[int, str]], groups: List[List[Tuple[int, str]]]) -> List[str]:
        """The records of ``did`` that are this customer's own: every organisation their entity links give as
        harmed (the supplier aside) is this customer; or, for a record whose links name none, its Perplexity
        research names it. A record naming any other organisation as harmed is general coverage and stays,
        whether or not that organisation is recorded on the event: the Canvas report's records listing TasTAFE
        beside USyd, UTS and QLearn were taken as TasTAFE's (only TasTAFE was a victim there), and the group
        then absorbed TasTAFE's own event (trial on a copy, 2026-11-04). Only a harmed name that can be a victim
        organisation counts, by the test that groups the event's victims: Tasmania's DECYP notice of the Canvas
        breach was linked to "Queensland", "South Australia" and "Tasmania" as well, WA's notice to "WA schools",
        and both stayed on an event recording only the University of Newcastle (owner review, 2026-11-05).
        Empty unless some record stays."""
        names = [n for _e, n in members]
        supplier = vendors.get(did, ())
        records = _record_orgs(conn, did)
        mine = []
        for m in records:
            harmed = [h for h in m["harmed"] if not any(same_supplier(h, v) for v in supplier)
                      and sv.is_candidate_organisation(h, m["harmed_kinds"].get(h))
                      and not descriptive_names.is_listed(descriptions, h)]
            if harmed:
                if all(any(names_match(conn, h, n) for n in names) for h in harmed):
                    mine.append(m["id"])
            elif m["org"] and not any(same_supplier(m["org"], v) for v in supplier) and \
                    any(names_match(conn, m["org"], n) for n in names) and \
                    [i for i, g in enumerate(groups) if any(names_match(conn, m["org"], n) for _e, n in g)] == \
                    [groups.index(members)]:
                mine.append(m["id"])
        return mine if 0 < len(mine) < len(records) else []

    # Customers an earlier run demoted here are still customers of the event for the split: their records stayed
    # behind (every demotion before 2026-11-04). Their own event is the one recorded with the demotion.
    earlier: Dict[str, List[Tuple[List[Tuple[int, str]], str]]] = defaultdict(list)
    for did, own, entity_id in conn.execute(
            """SELECT a.deduplicated_event_id, a.new_event_id, a.entity_id FROM SupplierLinkActions a
               JOIN DeduplicatedEventEntities dee ON dee.deduplicated_event_id = a.deduplicated_event_id
                AND dee.entity_id = a.entity_id AND dee.relationship_type = 'affected_customer'
               WHERE a.action = 'own-event-customer' ORDER BY a.created_at""").fetchall():
        own = resolve_event(conn, own) or own
        name = conn.execute("SELECT entity_name FROM EntitiesV2 WHERE entity_id = ?", (entity_id,)).fetchone()
        status = conn.execute("SELECT status FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                              (own,)).fetchone() if own else None
        if did in orgs and own and own != did and name and status is not None and status[0] == "Active" and \
                not any(names_match(conn, name[0], n) for g in orgs[did] for _e, n in g) and \
                not any(names_match(conn, name[0], n) for g, _o in earlier[did] for _e, n in g):
            earlier[did].append(([(entity_id, name[0])], own))

    demoted: List[Dict[str, Any]] = []
    reports: List[Dict[str, Any]] = []
    split_out: List[Dict[str, Any]] = []
    for did, groups in sorted(orgs.items()):
        if not vendors.get(did):
            continue
        before = earlier.get(did, [])
        groups = groups + [g for g, _own in before]
        if len(groups) < 2:
            continue
        gone = [(members, own) for members in groups for own in [own_event(did, members)] if own]
        gone += [(g, own) for g, own in before if all(g is not m for m, _o in gone)]
        if not gone:
            continue
        gone_names: Set[str] = set()
        for members, own in gone:
            # The customer's own records leave with it (owner 2026-11-04). Demoted in place, they kept an event
            # that is mostly the customer's under its other customers: Tasmania's DECYP Canvas event (two ABC
            # reports of DECYP, one WA notice, one Newcastle page) became Newcastle's and WA's, and was never
            # compared with DECYP's own event. Split out, they are an event of the customer that the calibrated
            # duplicate check pairs with its own event.
            records = customer_records(did, members, groups)
            if records:
                customer = sorted({n for _e, n in members})
                if apply:
                    from cyber_data_collector.dedup.ledger import DedupLedger
                    previous = conn.row_factory
                    conn.row_factory = sqlite3.Row
                    try:
                        new = DedupLedger(conn).split_members(
                            did, records, actor="rule",
                            reason=f"[supplier-split] {OWN_EVENT_DECISION}: the records of {customer[0]} leave "
                                   f"{did[:8]}; its own event of the breach is {own[:8]}")
                    finally:
                        conn.row_factory = previous
                    conn.execute("INSERT INTO SupplierLinkActions VALUES (?, 'split', ?, ?, NULL, ?, ?, ?)",
                                 (str(uuid.uuid4()), did, new,
                                  json.dumps({"customer": customer[0], "records": records, "own_event": own}),
                                  OWN_EVENT_DECISION, _now()))
                else:
                    new = None
                split_out.append({"deduplicated_event_id": did, "customer": customer, "records": len(records),
                                  "new_event": new, "own_event": own})
            changed = 0
            for eid, _name in members:
                changed += conn.execute(
                    "UPDATE DeduplicatedEventEntities SET relationship_type = 'affected_customer' "
                    "WHERE deduplicated_event_id = ? AND entity_id = ? AND relationship_type = 'victim'",
                    (did, eid)).rowcount
            customer = sorted({n for _e, n in members})
            gone_names.update(customer)
            if changed:
                demoted.append({"deduplicated_event_id": did, "title": titles.get(did), "customer": customer,
                                "own_event": own})
                conn.execute("INSERT INTO SupplierLinkActions VALUES (?, 'own-event-customer', ?, ?, ?, ?, ?, ?)",
                             (str(uuid.uuid4()), did, own, members[0][0],
                              json.dumps({"customer": customer, "supplier": sorted(vendors[did])}),
                              OWN_EVENT_DECISION, _now()))
        left = [g for g in groups if all(g is not members for members, _own in gone)]
        if left:
            if scalar.get(did) in gone_names:
                conn.execute("UPDATE DeduplicatedEvents SET victim_organization_name = ? WHERE "
                             "deduplicated_event_id = ?", (left[0][0][1] if len(left) == 1 else None, did))
            continue
        # Every customer has its own event: this is the supplier's report.
        supplier = column_vendor.get(did) or sorted(vendors[did])[0]
        link = conn.execute(
            """SELECT dee.entity_id FROM DeduplicatedEventEntities dee JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
               WHERE dee.deduplicated_event_id = ? AND dee.relationship_type IN ('vendor', 'service_provider', 'victim')
               ORDER BY v.entity_name != ?, v.entity_name LIMIT 1""", (did, supplier)).fetchone()
        changed = 0
        if link is not None:
            changed = conn.execute("UPDATE DeduplicatedEventEntities SET relationship_type = 'victim' WHERE "
                                   "deduplicated_event_id = ? AND entity_id = ? AND relationship_type != 'victim'",
                                   (did, link[0])).rowcount
        if scalar.get(did) != supplier:
            conn.execute("UPDATE DeduplicatedEvents SET victim_organization_name = ? WHERE deduplicated_event_id = ?",
                         (supplier, did))
            changed += 1
        if changed:
            reports.append({"deduplicated_event_id": did, "title": titles.get(did), "supplier": supplier,
                            "customers": sorted(gone_names)})
            conn.execute("INSERT INTO SupplierLinkActions VALUES (?, 'supplier-report', ?, NULL, ?, ?, ?, ?)",
                         (str(uuid.uuid4()), did, link[0] if link else None,
                          json.dumps({"supplier": supplier, "customers": sorted(gone_names)}),
                          SUPPLIER_REPORT_DECISION, _now()))
    if apply:
        conn.commit()
    else:
        conn.rollback()
    return {"demoted": demoted, "reports": reports, "split_out": split_out}


def _merged_away_records(conn: sqlite3.Connection, event_id: str, since: str) -> List[str]:
    """The records ``event_id`` held when a merge after ``since`` (local ISO time) took it into another event."""
    for batch_id, payload in conn.execute(
            "SELECT batch_id, payload_json FROM DedupSnapshots WHERE deduplicated_event_id = ? AND batch_id LIKE "
            "'merge-%' ORDER BY rowid DESC", (event_id,)).fetchall():
        try:
            when = datetime.strptime(batch_id.split("-", 1)[1], "%Y%m%d%H%M%S")
        except ValueError:
            continue
        if when < datetime.fromisoformat(since[:19]):
            return []
        members = [m.get("enriched_event_id") for m in json.loads(payload).get("members", [])]
        return [m for m in members if m]
    return []


def reverse_name_counted_reports(conn: sqlite3.Connection, apply: bool = True) -> List[Dict[str, Any]]:
    """Undo the conversions of the first supplier-report rule (:data:`SUPPLIER_REPORT_DECISION_V1`).

    The listed customers are victims again and the supplier its vendor; the victim column, set to the supplier,
    goes back to the one remaining victim (else empty). A converted event that a later merge took into another
    event - UC's and Tasmania's Canvas events, merged into Instructure's report once all three recorded Instructure
    as victim - leaves that event again with the records it held (``DedupLedger.split_members``, with rulings).
    :func:`double_counted_customers` then demotes only customers with their own events. Audited as
    ``supplier-report-reversed``; idempotent. ``apply`` False reports without changing anything.
    """
    from cyber_data_collector.dedup.ledger import DedupLedger

    ensure_schema(conn)
    done_ids = {json.loads(d or "{}").get("action_id") for (d,) in conn.execute(
        "SELECT detail_json FROM SupplierLinkActions WHERE action = 'supplier-report-reversed'")}
    rows = conn.execute("SELECT action_id, deduplicated_event_id, entity_id, detail_json, created_at "
                        "FROM SupplierLinkActions WHERE action = 'supplier-report' AND reason = ? ORDER BY created_at",
                        (SUPPLIER_REPORT_DECISION_V1,)).fetchall()
    out: List[Dict[str, Any]] = []
    for action_id, did, supplier_id, detail, created in rows:
        if action_id in done_ids:
            continue
        info = json.loads(detail or "{}")
        entry: Dict[str, Any] = {"action_id": action_id, "deduplicated_event_id": did,
                                 "supplier": info.get("supplier"), "customers": info.get("customers", []),
                                 "split_back": None}
        out.append(entry)
        if not apply:
            continue
        target = did
        holder = resolve_event(conn, did)
        if holder and holder != did:
            records = [r for r in _merged_away_records(conn, did, created) if conn.execute(
                "SELECT 1 FROM EventDeduplicationMap WHERE deduplicated_event_id = ? AND enriched_event_id = ?",
                (holder, r)).fetchone()]
            if records:
                previous = conn.row_factory
                conn.row_factory = sqlite3.Row
                try:
                    target = DedupLedger(conn).split_members(
                        holder, records, reason=f"[supplier-report-reversed] {REPORT_REVERSAL_DECISION}", actor="rule")
                finally:
                    conn.row_factory = previous
                entry["split_back"] = {"from": holder, "new_event": target, "records": len(records)}
        customers = entry["customers"]
        if customers:
            conn.execute(
                f"""UPDATE DeduplicatedEventEntities SET relationship_type = 'victim'
                    WHERE deduplicated_event_id = ? AND relationship_type = 'affected_customer' AND entity_id IN
                    (SELECT entity_id FROM EntitiesV2 WHERE entity_name IN ({','.join('?' * len(customers))}))""",
                (target, *customers))
        if supplier_id is not None:
            conn.execute("UPDATE DeduplicatedEventEntities SET relationship_type = 'vendor' WHERE "
                         "deduplicated_event_id = ? AND entity_id = ? AND relationship_type = 'victim'",
                         (target, supplier_id))
        row = conn.execute("SELECT victim_organization_name FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                           (target,)).fetchone()
        if row and row[0] == entry["supplier"]:
            remaining = [r[0] for r in conn.execute(
                "SELECT v.entity_name FROM DeduplicatedEventEntities dee JOIN EntitiesV2 v USING (entity_id) "
                "WHERE dee.deduplicated_event_id = ? AND dee.relationship_type = 'victim'", (target,))]
            conn.execute("UPDATE DeduplicatedEvents SET victim_organization_name = ? WHERE deduplicated_event_id = ?",
                         (remaining[0] if len(remaining) == 1 else None, target))
        conn.execute("INSERT INTO SupplierLinkActions VALUES (?, 'supplier-report-reversed', ?, ?, ?, ?, ?, ?)",
                     (str(uuid.uuid4()), did, target if target != did else None, supplier_id,
                      json.dumps(entry), REPORT_REVERSAL_DECISION, _now()))
        conn.commit()
    return out


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
               JOIN EntitiesV2 v USING (entity_id) WHERE dee.relationship_type IN ('vendor', 'service_provider')"""):
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
    reports_reversed = reverse_name_counted_reports(conn, apply=apply)
    customers = double_counted_customers(conn, g.resolver, apply=apply)
    return {"attachments_reversed": reversed_, "attachments_converted": converted, "splits": splits,
            "ambiguous": ambiguous, "split_customers_demoted": demoted, "supplier_reports": customers["reports"],
            "own_event_customers": customers["demoted"], "supplier_reports_reversed": reports_reversed}


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
