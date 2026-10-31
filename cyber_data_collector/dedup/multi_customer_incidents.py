"""Attacks on a known vendor that affected more than one of its Australian customers.

The 2026 Canvas attack on Instructure reached several Australian universities; Pareto Phone's 2023 breach reached a
dozen charities. Each such attack is a supplier incident in ``SupplierIncidents`` with one link per affected customer
in ``SupplierIncidentLinks`` (:mod:`supplier_links`): the customer's own event when it has one, else the customer
named by the reporting. :func:`find` lists every incident with at least two distinct Australian customers, for the
``supplier_incidents.html`` report.

**Australian customers only.** The dataset is Australian incidents. A customer with its own Active event is in it
because that event passed the calibrated scope check. A customer only *named* in the reporting (Accellion's global
victim list: Kroger, Stanford University, the Reserve Bank of New Zealand) is listed only when its researched entity
profile (:mod:`entity_profiles`, a Perplexity lookup, ``jurisdiction``) places it in Australia; one placed elsewhere
is counted as outside Australia and one with no researched jurisdiction as not placed, and neither is listed as a
customer or counted towards the two (:func:`unplaced_customers` lists those to research).

A customer is not counted when it is the supplier itself, a description rather than an organisation ("Australian
charities"), or a list of organisations; two names of one organisation count once (:func:`victim_fill.names_match`).
A customer's event is the Active event now holding it (merges are followed).

**One attack date per incident**: the supplier's own event's date, else the earliest date of the linked customer
events, shown at the precision it was verified to. Customers' events carry their own report dates and are not
shown as attack dates.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

DESCRIPTION_CHARS = 320


@dataclass
class Customer:
    name: str
    entity_id: Optional[int]
    event_id: Optional[str]
    event_title: Optional[str]
    event_date: Optional[str]
    basis: str


@dataclass
class Incident:
    supplier_incident_id: str
    vendor: str
    attack_date: Optional[str]
    description: str
    customers: List[Customer] = field(default_factory=list)
    attack_precision: Optional[str] = None
    outside: List[str] = field(default_factory=list)      # named customers placed outside Australia
    unplaced: List[str] = field(default_factory=list)     # named customers with no researched jurisdiction


def _brief(text: Optional[str]) -> str:
    text = " ".join((text or "").split())
    if len(text) <= DESCRIPTION_CHARS:
        return text
    cut = text[:DESCRIPTION_CHARS]
    stop = cut.rfind(". ")
    return cut[:stop + 1] if stop > DESCRIPTION_CHARS // 2 else cut.rstrip() + "..."


def _event(conn: sqlite3.Connection, event_id: Optional[str]):
    from cyber_data_collector.dedup.supplier_links import resolve_event

    if not event_id:
        return None
    now = resolve_event(conn, event_id) or event_id
    have = {r[1] for r in conn.execute("PRAGMA table_info(DeduplicatedEvents)")}
    precision = "event_date_precision" if "event_date_precision" in have else "NULL"
    return conn.execute(f"SELECT deduplicated_event_id, title, event_date, description, status, {precision} "
                        "FROM DeduplicatedEvents WHERE deduplicated_event_id = ? AND status = 'Active'",
                        (now,)).fetchone()


def jurisdiction(conn: sqlite3.Connection, entity_id: Optional[int]) -> Optional[str]:
    """``australia`` / ``outside`` from the organisation's researched jurisdiction; None when it has none."""
    from cyber_data_collector.dedup.entity_profiles import profile_for

    if entity_id is None:
        return None
    values = profile_for(conn, int(entity_id)).get("jurisdiction") or []
    if not values:
        return None
    return "australia" if any("australia" in v.casefold() for v in values) else "outside"


def unplaced_customers(conn: sqlite3.Connection) -> List[Tuple[int, str]]:
    """Named customers (no event of their own) with no researched jurisdiction: what to look up."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "SupplierIncidentLinks" not in tables:
        return []
    rows = conn.execute("SELECT DISTINCT customer_entity_id, customer_name FROM SupplierIncidentLinks "
                        "WHERE customer_event_id IS NULL ORDER BY customer_entity_id").fetchall()
    return [(e, n) for e, n in rows if e is not None and jurisdiction(conn, e) is None]


def event_suppliers(conn: sqlite3.Connection) -> Dict[str, str]:
    """The supplier of each Active event, where it has one: the vendor it records, else the supplier of the supplier
    incident that links it as a customer's event (Stake's own event, linked to DriveWealth's attack). For the
    reports' Supplier column."""
    out: Dict[str, str] = {}
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if {"SupplierIncidents", "SupplierIncidentLinks"} <= tables:
        from cyber_data_collector.dedup.supplier_links import resolve_event

        for event_id, supplier in conn.execute(
                """SELECT l.customer_event_id, i.supplier_name FROM SupplierIncidentLinks l
                   JOIN SupplierIncidents i ON i.supplier_incident_id = l.supplier_incident_id
                   WHERE l.customer_event_id IS NOT NULL ORDER BY i.supplier_name"""):
            out.setdefault(resolve_event(conn, event_id) or event_id, supplier)
    for event_id, vendor in conn.execute(
            "SELECT deduplicated_event_id, vendor_organization_name FROM DeduplicatedEvents "
            "WHERE status = 'Active' AND vendor_organization_name IS NOT NULL AND TRIM(vendor_organization_name) != ''"):
        out[event_id] = vendor
    return out


def find(conn: sqlite3.Connection) -> List[Incident]:
    """Every supplier incident with two or more distinct Australian customers, ordered by vendor and date; each
    incident's customers in name order, with the named customers left out (outside Australia, or not placed)."""
    from cyber_data_collector.dedup import descriptive_names, name_judgement
    from cyber_data_collector.dedup.entity_merge import is_organisation_list
    from cyber_data_collector.dedup.victim_fill import names_match

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {"SupplierIncidents", "SupplierIncidentLinks"} <= tables:
        return []
    described = descriptive_names.described(conn) if "DescriptiveNames" in tables else set(name_judgement._DESCRIPTIVE)
    out: List[Incident] = []
    for sid, vendor, own_event, first, _last in conn.execute(
            "SELECT supplier_incident_id, supplier_name, supplier_event_id, first_date, last_date FROM SupplierIncidents"):
        customers: List[Customer] = []
        outside: List[str] = []
        unplaced: List[str] = []
        for name, entity_id, event_id, basis in conn.execute(
                "SELECT customer_name, customer_entity_id, customer_event_id, basis FROM SupplierIncidentLinks "
                "WHERE supplier_incident_id = ? ORDER BY customer_name", (sid,)):
            if (not name or names_match(conn, name, vendor) or descriptive_names.is_listed(described, name)
                    or is_organisation_list(name)):
                continue
            ev = _event(conn, event_id)
            same = next((c for c in customers if names_match(conn, c.name, name)), None)
            if same is not None:
                if same.event_id is None and ev is not None:
                    same.event_id, same.event_title, same.event_date = ev[0], ev[1], ev[2]
                continue
            if ev is None:
                place = jurisdiction(conn, entity_id)
                if place != "australia":
                    bucket = outside if place == "outside" else unplaced
                    if not any(names_match(conn, n, name) for n in bucket):
                        bucket.append(name)
                    continue
            customers.append(Customer(name, entity_id, ev[0] if ev else None, ev[1] if ev else None,
                                      ev[2] if ev else None, basis or ""))
        listed = [c.name for c in customers]
        outside = [n for n in outside if not any(names_match(conn, n, c) for c in listed)]
        unplaced = [n for n in unplaced if not any(names_match(conn, n, c) for c in listed)]
        if len(customers) < 2:
            continue
        own = _event(conn, own_event)
        dated = sorted(((row[2], row[5]) for c in customers if c.event_id
                        for row in [_event(conn, c.event_id)] if row and row[2]),
                       key=lambda d: (str(d[0]), d[1] or ""))       # a precision may be NULL
        if own and own[2]:
            attack_date, precision = own[2], own[5]
        elif dated:
            attack_date, precision = dated[0]
        else:
            attack_date, precision = first, None
        description = _brief(own[3] if own and own[3] else next(
            (row[3] for c in customers if c.event_id for row in [_event(conn, c.event_id)] if row and row[3]), ""))
        customers.sort(key=lambda c: c.name.casefold())
        out.append(Incident(sid, vendor, attack_date, description, customers, precision,
                            sorted(outside, key=str.casefold), sorted(unplaced, key=str.casefold)))
    out.sort(key=lambda i: (i.vendor.casefold(), i.attack_date or "9999", i.supplier_incident_id))
    return out


def attack_label(incident: Incident) -> str:
    """The attack date at the precision it was verified to: a month shown as the month, not as its 1st."""
    date = str(incident.attack_date or "")
    if not date:
        return "undated"
    if incident.attack_precision == "month" and len(date) >= 7:
        from calendar import month_name

        return f"{month_name[int(date[5:7])]} {date[:4]}"
    if incident.attack_precision == "year":
        return date[:4]
    if incident.attack_precision == "day":
        return date[:10]
    return f"{date[:10]} (precision unverified)"
