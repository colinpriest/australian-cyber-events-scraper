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
charities", "The College": the proper-name judgement, :mod:`name_judgement`; a name only named and not yet judged
is left out as not checked), or a list of organisations. Two names of one customer count once: the same name up to
spelling or a stored alias (:func:`victim_fill.names_match`), or a pair the alias verification - asked with the
incident's context (:func:`alias_learning.verify_supplier_pairs`) - or a person ruled ``same`` (NAB and "National
Australia Bank (NAB)") or ``parent_subsidiary`` (the ACT Health Directorate under the ACT Government, whose gateway
was breached). A name verified as the supplier itself ("PricewaterhouseCoopers (PwC)" under PwC) is the supplier.
A customer's event is the Active event now holding it (merges are followed).

**A description per attack** (``EventDescriptions`` purpose ``supplier-incident``, keyed by the supplier incident):
2-3 sentences by the fast OpenAI model from the records of the supplier's own event and its customers' events,
written by ``find-supplier-victims --research --apply`` (:func:`describe_incidents`) and rewritten only when that
evidence changes. Events themselves carry no description (the column is empty on every Active event), so the report
used to print "No description recorded" for every attack.

**One attack date per incident**: the supplier's own event's date, else the earliest date of the linked customer
events, shown at the precision it was verified to. Customers' events carry their own report dates and are not
shown as attack dates.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple

DESCRIPTION_PURPOSE = "supplier-incident"
DESCRIPTION_PROMPT_VERSION = "supplier-incident-description-v1"
DESCRIPTION_SYSTEM = (
    "You write a short description of an attack on a supplier (a vendor or service provider) that affected "
    "several of its customers in Australia. You are given the supplier, the date and short accounts of the "
    "reporting about the supplier and about its customers. In two or three plain sentences say what happened to "
    "the supplier (how it was attacked or what was compromised, when), what data or services of its customers were "
    "affected, and how many customers if the reporting says. Use only the accounts given; do not speculate or "
    "advise. Describe the incident, never the evidence; where a fact is only alleged, say 'reportedly'. Return "
    "only the description."
)
#: Accounts shown to the describer: per event, and in all.
ACCOUNTS_PER_EVENT = 2
ACCOUNT_CHARS = 600
MAX_ACCOUNTS = 12


@dataclass
class Customer:
    name: str
    entity_id: Optional[int]
    event_id: Optional[str]
    event_title: Optional[str]
    event_date: Optional[str]
    basis: str
    also: List[str] = field(default_factory=list)       # other names of this customer, or its parts, in the incident


@dataclass
class Incident:
    supplier_incident_id: str
    vendor: str
    attack_date: Optional[str]
    description: str
    customers: List[Customer] = field(default_factory=list)
    attack_precision: Optional[str] = None
    outside: List[str] = field(default_factory=list)      # named customers placed outside Australia
    unplaced: List[str] = field(default_factory=list)     # named customers not yet checked (name or jurisdiction)
    description_current: bool = True                      # False: written from older evidence


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


def pair_verdicts(conn: sqlite3.Connection) -> Dict[frozenset, str]:
    """``{frozenset(lower names): verdict}`` from ``EntityAliasDecisions``: a person's decision first, else the latest."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "EntityAliasDecisions" not in tables:
        return {}
    out: Dict[frozenset, str] = {}
    for a, b, verdict, model in conn.execute(
            "SELECT name, other_name, verdict, model FROM EntityAliasDecisions "
            "ORDER BY model = 'human', decided_at"):
        out[frozenset((a.strip().lower(), b.strip().lower()))] = verdict     # later rows (a person's last) win
    return out


def _one(conn: sqlite3.Connection, verdicts: Dict[frozenset, str], a: str, b: str,
         kinds: Tuple[str, ...] = ("same", "parent_subsidiary")) -> bool:
    from cyber_data_collector.dedup.victim_fill import names_match

    return names_match(conn, a, b) or verdicts.get(frozenset((a.strip().lower(), b.strip().lower()))) in kinds


def find(conn: sqlite3.Connection) -> List[Incident]:
    """Every supplier incident with two or more distinct Australian customers, ordered by vendor and date; each
    incident's customers in name order, with the named customers left out (outside Australia, or not checked)."""
    from cyber_data_collector.dedup import descriptive_names, name_judgement
    from cyber_data_collector.dedup.entity_merge import is_organisation_list

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {"SupplierIncidents", "SupplierIncidentLinks"} <= tables:
        return []
    described = descriptive_names.described(conn) if "DescriptiveNames" in tables else set(name_judgement._DESCRIPTIVE)
    judged_descriptive = name_judgement.descriptive_names(conn)
    judged = name_judgement.stored_probabilities(conn)
    verdicts = pair_verdicts(conn)
    out: List[Incident] = []
    for sid, vendor, own_event, first, _last in conn.execute(
            "SELECT supplier_incident_id, supplier_name, supplier_event_id, first_date, last_date FROM SupplierIncidents"):
        customers: List[Customer] = []
        outside: List[str] = []
        unplaced: List[str] = []
        rows = conn.execute(
            "SELECT customer_name, customer_entity_id, customer_event_id, basis FROM SupplierIncidentLinks "
            "WHERE supplier_incident_id = ?", (sid,)).fetchall()
        # A customer with its own event first, so the other names of one customer join it.
        rows.sort(key=lambda r: (_event(conn, r[2]) is None, (r[0] or "").casefold()))
        for name, entity_id, event_id, basis in rows:
            key = " ".join((name or "").split()).lower()
            if (not name or _one(conn, verdicts, name, vendor, ("same",)) or descriptive_names.is_listed(described, name)
                    or key in judged_descriptive or is_organisation_list(name)):
                continue
            ev = _event(conn, event_id)
            same = next((c for c in customers if _one(conn, verdicts, c.name, name)
                         or any(_one(conn, verdicts, o, name) for o in c.also)), None)
            if same is not None:
                if same.event_id is None and ev is not None:
                    same.event_id, same.event_title, same.event_date = ev[0], ev[1], ev[2]
                if name.strip().lower() != same.name.strip().lower() and name not in same.also:
                    same.also.append(name)
                continue
            if ev is None:
                place = jurisdiction(conn, entity_id)
                if place != "australia" or key not in judged:
                    bucket = outside if place == "outside" else unplaced
                    if not any(_one(conn, verdicts, n, name) for n in bucket):
                        bucket.append(name)
                    continue
            customers.append(Customer(name, entity_id, ev[0] if ev else None, ev[1] if ev else None,
                                      ev[2] if ev else None, basis or ""))
        listed = [n for c in customers for n in [c.name, *c.also]]
        outside = [n for n in outside if not any(_one(conn, verdicts, n, c) for c in listed)]
        unplaced = [n for n in unplaced if not any(_one(conn, verdicts, n, c) for c in listed)]
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
        for c in customers:
            c.also.sort(key=str.casefold)
        customers.sort(key=lambda c: c.name.casefold())
        text, current = stored_description(conn, sid, description_state(conn, sid))
        out.append(Incident(sid, vendor, attack_date, text or "", customers, precision,
                            sorted(outside, key=str.casefold), sorted(unplaced, key=str.casefold), current))
    out.sort(key=lambda i: (i.vendor.casefold(), i.attack_date or "9999", i.supplier_incident_id))
    return out


# ---------------------------------------------------------------- descriptions

def description_state(conn: sqlite3.Connection, supplier_incident_id: str) -> Dict[str, Any]:
    """What the describer is shown: the supplier, its own event and its customers' events, each with the
    descriptions of its active records (the same accounts for the same evidence, in a stable order)."""
    from cyber_data_collector.processing.active_evidence import member_condition
    from cyber_data_collector.dedup.supplier_links import resolve_event

    row = conn.execute("SELECT supplier_name, supplier_event_id, first_date FROM SupplierIncidents "
                       "WHERE supplier_incident_id = ?", (supplier_incident_id,)).fetchone()
    if row is None:
        return {}
    vendor, own, first = row
    events: List[Tuple[str, str]] = []
    if own:
        events.append(("supplier", resolve_event(conn, own) or own))
    for name, event_id in conn.execute(
            "SELECT customer_name, customer_event_id FROM SupplierIncidentLinks WHERE supplier_incident_id = ? "
            "AND customer_event_id IS NOT NULL ORDER BY customer_name", (supplier_incident_id,)):
        events.append((name, resolve_event(conn, event_id) or event_id))
    accounts: List[Dict[str, str]] = []
    for whose, event_id in events:
        texts = [r[0] for r in conn.execute(
            f"""SELECT DISTINCT COALESCE(NULLIF(TRIM(e.description), ''), NULLIF(TRIM(e.summary), ''))
                FROM EventDeduplicationMap m JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
                WHERE m.deduplicated_event_id = ? {member_condition(conn)} ORDER BY e.enriched_event_id""",
            (event_id,)) if r[0]][:ACCOUNTS_PER_EVENT]
        accounts.extend({"about": whose, "account": t[:ACCOUNT_CHARS]} for t in texts)
    named = [r[0] for r in conn.execute("SELECT customer_name FROM SupplierIncidentLinks WHERE supplier_incident_id = ? "
                                        "ORDER BY customer_name", (supplier_incident_id,))]
    return {"supplier": vendor, "date": first, "customers_named": named, "accounts": accounts[:MAX_ACCOUNTS]}


def description_hash(state: Mapping[str, Any], model: str) -> str:
    blob = json.dumps({"state": state, "system": DESCRIPTION_SYSTEM, "version": DESCRIPTION_PROMPT_VERSION,
                       "model": model}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def stored_description(conn: sqlite3.Connection, supplier_incident_id: str,
                       state: Mapping[str, Any]) -> Tuple[Optional[str], bool]:
    """``(text, current)``: the description written from this evidence, else the latest one (not current)."""
    from cyber_data_collector.utils.llm_models import openai_fast_model

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "EventDescriptions" not in tables:
        return None, True
    digest = description_hash(state, openai_fast_model())
    row = conn.execute("SELECT description FROM EventDescriptions WHERE deduplicated_event_id = ? AND purpose = ? "
                       "AND input_hash = ? ORDER BY rowid DESC LIMIT 1",
                       (supplier_incident_id, DESCRIPTION_PURPOSE, digest)).fetchone()
    if row:
        return row[0], True
    row = conn.execute("SELECT description FROM EventDescriptions WHERE deduplicated_event_id = ? AND purpose = ? "
                       "ORDER BY rowid DESC LIMIT 1", (supplier_incident_id, DESCRIPTION_PURPOSE)).fetchone()
    return (row[0], False) if row else (None, True)


def openai_describer(client: Any = None) -> Callable[[Mapping[str, Any]], str]:
    """Write a description with the fast OpenAI model (``OPENAI_FAST_MODEL``)."""
    from cyber_data_collector.utils.llm_models import chat_params, openai_fast_model

    if client is None:
        from openai import OpenAI
        client = OpenAI()

    def describe(state: Mapping[str, Any]) -> str:
        response = client.chat.completions.create(
            **chat_params(openai_fast_model(), max_tokens=300),
            messages=[{"role": "system", "content": DESCRIPTION_SYSTEM},
                      {"role": "user", "content": json.dumps(state, ensure_ascii=False, default=str)}])
        return (response.choices[0].message.content or "").strip()

    return describe


def describe_incidents(conn: sqlite3.Connection, describer: Optional[Callable[[Mapping[str, Any]], str]] = None,
                       model: Optional[str] = None) -> Dict[str, int]:
    """Describe every attack the report lists whose evidence has no description yet. A failure is counted and
    raised after the others are written (never replaced by an empty description)."""
    from cyber_data_collector.processing.ai_involvement import SCHEMA
    from cyber_data_collector.utils.llm_models import openai_fast_model

    conn.executescript(SCHEMA)
    model = model or openai_fast_model()
    counts = {"written": 0, "current": 0, "failed": 0}
    errors: List[str] = []
    for inc in find(conn):
        state = description_state(conn, inc.supplier_incident_id)
        digest = description_hash(state, model)
        if conn.execute("SELECT 1 FROM EventDescriptions WHERE deduplicated_event_id = ? AND purpose = ? "
                        "AND input_hash = ?", (inc.supplier_incident_id, DESCRIPTION_PURPOSE, digest)).fetchone():
            counts["current"] += 1
            continue
        try:
            describer = describer or openai_describer()
            text = describer(state)
            if not text:
                raise ValueError("the description model returned no text")
        except Exception as exc:  # noqa: BLE001 - counted and raised below; the others are still written
            counts["failed"] += 1
            errors.append(f"{inc.vendor} ({inc.supplier_incident_id}): {exc}")
            continue
        conn.execute("INSERT INTO EventDescriptions VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (inc.supplier_incident_id, DESCRIPTION_PURPOSE, DESCRIPTION_PROMPT_VERSION, digest, model, text,
                      datetime.now().isoformat()))
        conn.commit()
        counts["written"] += 1
    if errors:
        raise RuntimeError(f"{len(errors)} supplier incident description(s) failed: " + "; ".join(errors[:5]))
    return counts


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
