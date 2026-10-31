"""Attacks on a known vendor that affected more than one of its customers.

The 2026 Canvas attack on Instructure reached several Australian universities; Pareto Phone's 2023 breach reached a
dozen charities. Each such attack is a supplier incident in ``SupplierIncidents`` with one link per affected customer
in ``SupplierIncidentLinks`` (:mod:`supplier_links`): the customer's own event when it has one, else the customer
named by the reporting. :func:`find` lists every incident with at least two distinct customers, for the
``supplier_incidents.html`` report.

A customer is not counted when it is the supplier itself, a description rather than an organisation ("Australian
charities"), or a list of organisations; two names of one organisation count once (:func:`victim_fill.names_match`).
A customer's event is the Active event now holding it (merges are followed).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional

DESCRIPTION_CHARS = 320


@dataclass
class Customer:
    name: str
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
    return conn.execute("SELECT deduplicated_event_id, title, event_date, description, status FROM DeduplicatedEvents "
                        "WHERE deduplicated_event_id = ? AND status = 'Active'", (now,)).fetchone()


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
    """Every supplier incident with two or more distinct affected customers, ordered by vendor and date; each
    incident's customers in name order."""
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
        for name, event_id, basis in conn.execute(
                "SELECT customer_name, customer_event_id, basis FROM SupplierIncidentLinks WHERE supplier_incident_id = ? "
                "ORDER BY customer_name", (sid,)):
            if (not name or names_match(conn, name, vendor) or descriptive_names.is_listed(described, name)
                    or is_organisation_list(name)):
                continue
            ev = _event(conn, event_id)
            same = next((c for c in customers if names_match(conn, c.name, name)), None)
            if same is not None:
                if same.event_id is None and ev is not None:
                    same.event_id, same.event_title, same.event_date = ev[0], ev[1], ev[2]
                continue
            customers.append(Customer(name, ev[0] if ev else None, ev[1] if ev else None, ev[2] if ev else None,
                                      basis or ""))
        if len(customers) < 2:
            continue
        own = _event(conn, own_event)
        dated = sorted(c.event_date for c in customers if c.event_date)
        attack_date = (own[2] if own and own[2] else None) or first or (dated[0] if dated else None)
        description = _brief(own[3] if own and own[3] else next(
            (row[3] for c in customers if c.event_id for row in [_event(conn, c.event_id)] if row and row[3]), ""))
        customers.sort(key=lambda c: c.name.casefold())
        out.append(Incident(sid, vendor, attack_date, description, customers))
    out.sort(key=lambda i: (i.vendor.casefold(), i.attack_date or "9999", i.supplier_incident_id))
    return out
