"""Build the detailed by-entity event table (``entity_events_detail.html`` and ``.xlsx``).

The same rows as ``entities.html`` - one per victim organisation and event, sorted by organisation then
event date, an organisation stored under several name variants listed once under its representative,
an event with several victims under each - with every attribute of the organisation and the event
beside them: what the organisation is (type, kind, size, researched jurisdiction and sector), what
happened (attack type, severity, records affected, AI category, attacker and method), when (the stored
date, its verified precision and interval, its basis, the first report), the supply chain (vendor,
service provider, supplier incident), the ASD classification (severity category, organisation type,
impact type), and the evidence behind it.

The rows are ``build_entity_dashboard.load_rows`` itself, so the two pages cannot disagree about which
organisation an event is listed under; the attributes are read by event and entity id. Nothing is
inferred: an attribute the database does not hold is an empty cell, and the page and the workbook say
for each column how many rows are empty.

The page is self-contained (no external scripts): a sortable table with a filter per column, column
groups that can be hidden, light and dark mode, and a container that scrolls sideways on a phone.
The workbook holds the same rows and columns (sheet ``Entity events``) and a ``Columns`` sheet that
describes each column and where it comes from.

Usage:
    python scripts/build_entity_events_detail.py [--db PATH] [--out dashboard/entity_events_detail.html]
                                                 [--xlsx dashboard/entity_events_detail.xlsx]
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.reporting.population import incident_filter  # noqa: E402
from cyber_data_collector.utils.html_safe import script_json  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_DB = "instance/cyber_events.db"
DEFAULT_OUT = "dashboard/entity_events_detail.html"
DEFAULT_XLSX = "dashboard/entity_events_detail.xlsx"
SHEET = "Entity events"
COLUMNS_SHEET = "Columns"

# A column is reported as mostly empty when at least this share of its rows is blank.
MOSTLY_EMPTY = 0.5


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    group: str
    kind: str          # "text", "number", "id" (a number shown as is), "date" or "long" (wrapping text)
    source: str        # where the value comes from


GROUPS = ("Organisation", "Event", "Dates", "Supply chain", "ASD", "Evidence")

COLUMNS: Tuple[Column, ...] = (
    # --- the organisation -----------------------------------------------------------------------------
    Column("entity", "Organisation", "Organisation", "text",
           "EntitiesV2.entity_name of the organisation's representative name variant (as entities.html)"),
    Column("entity_id", "Entity id", "Organisation", "id", "EntitiesV2.entity_id"),
    Column("role", "Role on event", "Organisation", "text",
           "DeduplicatedEventEntities.relationship_type (every row is a victim role, as on entities.html)"),
    Column("entity_type", "Industry (entity type)", "Organisation", "text", "EntitiesV2.entity_type"),
    Column("industry", "Industry detail", "Organisation", "text", "EntitiesV2.industry"),
    Column("entity_kind", "Entity kind", "Organisation", "text", "EntitiesV2.entity_kind"),
    Column("size", "Size band", "Organisation", "text",
           "EntitiesV2.size_estimate (UNKNOWN when researched without a size or never researched)"),
    Column("size_researched", "Size researched", "Organisation", "text",
           "yes when EntitiesV2.size_estimate is set (UNKNOWN can be a researched answer)"),
    Column("size_employees", "Employees (estimate)", "Organisation", "number", "EntitiesV2.size_employees"),
    Column("size_revenue_aud", "Revenue A$ (estimate)", "Organisation", "number", "EntitiesV2.size_revenue_aud"),
    Column("size_confidence", "Size confidence", "Organisation", "number", "EntitiesV2.size_confidence"),
    Column("size_basis", "Size basis", "Organisation", "long", "EntitiesV2.size_basis"),
    Column("is_australian", "Australian (entity flag)", "Organisation", "text", "EntitiesV2.is_australian"),
    Column("headquarters", "Headquarters", "Organisation", "text", "EntitiesV2.headquarters_location"),
    Column("jurisdiction", "Jurisdiction (profile)", "Organisation", "text",
           "EntityProfiles kind 'jurisdiction' (a person's values first)"),
    Column("sector", "Sector (profile)", "Organisation", "text", "EntityProfiles kind 'sector'"),
    Column("profile_type", "Organisation type (profile)", "Organisation", "text", "EntityProfiles kind 'type'"),
    Column("administered_by", "Administered by (profile)", "Organisation", "text",
           "EntityProfiles kind 'administered_by'"),
    Column("website", "Website", "Organisation", "text",
           "EntityProfiles kind 'website', else EntitiesV2.website_url"),
    Column("attribution_unresolved", "Attribution unresolved", "Organisation", "text",
           "'attribution unresolved' when the calibrated victim-attribution check flagged this victim and could "
           "not resolve it (dedup/victim_counting.unresolved_rows)"),
    Column("attribution_reason", "Attribution reason", "Organisation", "long",
           "The attribution check's reason for the flag"),
    # --- the event ----------------------------------------------------------------------------------
    Column("event_id", "Event id", "Event", "text", "DeduplicatedEvents.deduplicated_event_id"),
    Column("title", "Event", "Event", "long", "DeduplicatedEvents.title"),
    Column("description", "Description", "Event", "long",
           "DeduplicatedEvents.summary / description, else the master EnrichedEvents record's"),
    Column("event_type", "Attack type", "Event", "text", "DeduplicatedEvents.event_type"),
    Column("severity", "Severity", "Event", "text", "DeduplicatedEvents.severity"),
    Column("records_affected", "Records affected", "Event", "number", "DeduplicatedEvents.records_affected"),
    Column("ai_involvement", "AI category", "Event", "text", "DeduplicatedEvents.ai_involvement"),
    Column("ai_involvement_basis", "AI category basis", "Event", "long", "DeduplicatedEvents.ai_involvement_basis"),
    Column("p_ai", "P(AI involved)", "Event", "number", "1 - DeduplicatedEvents.no_ai_probability"),
    Column("attacker", "Attacker", "Event", "text", "DeduplicatedEvents.attacking_entity_name"),
    Column("threat_actors", "Threat actors (roles)", "Event", "text",
           "Entities with role 'threat_actor' on the event (DeduplicatedEventEntities)"),
    Column("attack_method", "Attack method", "Event", "long", "DeduplicatedEvents.attack_method"),
    Column("victims_on_event", "Victim organisations on event", "Event", "number",
           "Rows of this table for the event (victims counted once per organisation)"),
    Column("stored_victim", "Stored victim name", "Event", "text", "DeduplicatedEvents.victim_organization_name"),
    Column("stored_victim_industry", "Stored victim industry", "Event", "text",
           "DeduplicatedEvents.victim_organization_industry"),
    Column("confidence_score", "Event confidence", "Event", "number",
           "DeduplicatedEvents.confidence_score (empty = unknown, never a default)"),
    # --- dates --------------------------------------------------------------------------------------
    Column("event_date", "Event date", "Dates", "date", "DeduplicatedEvents.event_date"),
    Column("event_date_precision", "Verified precision", "Dates", "text",
           "DeduplicatedEvents.event_date_precision (day / month / year; empty = unverified or conflicting)"),
    Column("event_date_earliest", "Earliest possible date", "Dates", "date", "DeduplicatedEvents.event_date_earliest"),
    Column("event_date_latest", "Latest possible date", "Dates", "date", "DeduplicatedEvents.event_date_latest"),
    Column("date_precision", "Displayed precision", "Dates", "text", "DeduplicatedEvents.date_precision"),
    Column("date_basis", "Date basis", "Dates", "text", "DeduplicatedEvents.date_basis"),
    Column("first_reported_date", "First reported", "Dates", "date", "DeduplicatedEvents.first_reported_date"),
    Column("reporting_lag_days", "Reporting lag (days)", "Dates", "number", "DeduplicatedEvents.reporting_lag_days"),
    # --- supply chain -------------------------------------------------------------------------------
    Column("vendor", "Vendor", "Supply chain", "text",
           "DeduplicatedEvents.vendor_organization_name and entities with role 'vendor' on the event"),
    Column("service_provider", "Service provider", "Supply chain", "text",
           "Entities with role 'service_provider' on the event"),
    Column("supplier_incident", "Supplier incident", "Supply chain", "text",
           "SupplierIncidents linked to the event (SupplierIncidentLinks), with the event's part in it"),
    # --- ASD ----------------------------------------------------------------------------------------
    Column("asd_severity", "ASD severity category", "ASD", "text", "ASDRiskClassifications.severity_category"),
    Column("asd_stakeholder", "ASD organisation type", "ASD", "text",
           "ASDRiskClassifications.primary_stakeholder_category"),
    Column("asd_impact", "ASD impact type", "ASD", "text", "ASDRiskClassifications.impact_type"),
    # --- evidence and OAIC categories ---------------------------------------------------------------
    Column("evidence_status", "Evidence status", "Evidence", "text", "DeduplicatedEvents.evidence_status"),
    Column("evidence_reason", "Evidence reason", "Evidence", "long", "DeduplicatedEvents.evidence_status_reason"),
    Column("evidence_uncertain_fields", "Uncertain fields", "Evidence", "text",
           "DeduplicatedEvents.evidence_uncertain_fields"),
    Column("breach_source_category", "Breach source (OAIC)", "Evidence", "text",
           "DeduplicatedEvents.breach_source_category"),
    Column("personal_info_types", "Personal information types", "Evidence", "text",
           "DeduplicatedEvents.personal_info_types_json (the types judged present)"),
    Column("source_records", "Source records", "Evidence", "number", "EventDeduplicationMap rows for the event"),
    Column("total_data_sources", "Data sources", "Evidence", "number", "DeduplicatedEvents.total_data_sources"),
    Column("dedup_method", "Dedup method", "Evidence", "text", "DeduplicatedEvents.dedup_method"),
    Column("dedup_certainty", "Dedup certainty", "Evidence", "number", "DeduplicatedEvents.dedup_certainty"),
    Column("human_override", "Human override", "Evidence", "text", "DeduplicatedEvents.has_human_override"),
)

COLUMN_KEYS = tuple(c.key for c in COLUMNS)

# DeduplicatedEvents columns read (a database without one of them reads it as empty).
EVENT_FIELDS = (
    "event_type", "severity", "records_affected", "ai_involvement", "ai_involvement_basis", "no_ai_probability",
    "attacking_entity_name", "attack_method", "victim_organization_name", "victim_organization_industry",
    "confidence_score", "event_date", "event_date_precision", "event_date_earliest", "event_date_latest",
    "date_precision", "date_basis", "first_reported_date", "reporting_lag_days", "vendor_organization_name",
    "breach_source_category", "personal_info_types_json", "total_data_sources", "dedup_method",
    "dedup_certainty", "has_human_override", "evidence_uncertain_fields",
)
ENTITY_FIELDS = (
    "entity_type", "industry", "entity_kind", "size_employees", "size_revenue_aud", "is_australian",
    "headquarters_location", "website_url",
)
PROFILE_KINDS = {"jurisdiction": "jurisdiction", "sector": "sector", "type": "profile_type",
                 "administered_by": "administered_by", "website": "website"}


def _columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn: sqlite3.Connection) -> Set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _select(have: Set[str], fields: Iterable[str], alias: str) -> str:
    return ", ".join(f"{alias}.{f} AS {f}" if f in have else f"NULL AS {f}" for f in fields)


def _chunks(values: Sequence[Any], size: int = 500) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _by_id(conn: sqlite3.Connection, sql: str, ids: Sequence[Any]) -> List[sqlite3.Row]:
    """Rows of ``sql`` (with one ``{ids}`` placeholder) for every id, in chunks under SQLite's variable limit."""
    out: List[sqlite3.Row] = []
    for chunk in _chunks(list(ids)):
        out += conn.execute(sql.format(ids=",".join("?" * len(chunk))), list(chunk)).fetchall()
    return out


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _date(value: Any) -> Optional[str]:
    text = _text(value)
    return text[:10] if text else None


def _flag(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    return "yes" if str(value).strip().lower() in ("1", "true", "yes") else "no"


def _join(values: Iterable[Optional[str]]) -> Optional[str]:
    seen: List[str] = []
    for value in values:
        text = _text(value)
        if text and text.casefold() not in {s.casefold() for s in seen}:
            seen.append(text)
    return "; ".join(seen) or None


def personal_info_types(raw: Any) -> Optional[str]:
    """The types judged present, as words; ``(none)`` when every type is absent; ``(malformed)`` for bad JSON."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return "(malformed)"
    if not isinstance(data, dict):
        return "(malformed)"
    present = [str(k).replace("_", " ") for k, v in data.items() if v is True]
    return ", ".join(present) if present else "(none)"


def uncertain_fields(raw: Any) -> Optional[str]:
    """The fields no evidence supports, as a list; ``(none)`` for an empty list; the stored text if it is not JSON."""
    text = _text(raw)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if isinstance(data, list):
        return ", ".join(str(v) for v in data) if data else "(none)"
    return text


def _event_attributes(conn: sqlite3.Connection, event_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    from cyber_data_collector.processing.evidence_recovery import select_columns

    have = _columns(conn, "DeduplicatedEvents")
    where, params = incident_filter(None, "d")
    sql = (f"SELECT d.deduplicated_event_id AS event_id, d.title AS title, {_select(have, EVENT_FIELDS, 'd')}, "
           f"{select_columns(conn, 'd')}, "
           "COALESCE(NULLIF(TRIM(d.summary), ''), NULLIF(TRIM(d.description), ''), "
           "NULLIF(TRIM(e.summary), ''), NULLIF(TRIM(e.description), ''), '') AS full_description, "
           "(SELECT COUNT(*) FROM EventDeduplicationMap m "
           " WHERE m.deduplicated_event_id = d.deduplicated_event_id) AS source_records "
           "FROM DeduplicatedEvents d LEFT JOIN EnrichedEvents e ON e.enriched_event_id = d.master_enriched_event_id "
           f"WHERE {where} AND d.deduplicated_event_id IN ({{ids}})")
    out: Dict[str, Dict[str, Any]] = {}
    for chunk in _chunks(list(event_ids)):
        for row in conn.execute(sql.format(ids=",".join("?" * len(chunk))), list(params) + list(chunk)):
            out[row["event_id"]] = dict(row)
    return out


def _entity_attributes(conn: sqlite3.Connection, entity_ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
    have = _columns(conn, "EntitiesV2")
    fields = ENTITY_FIELDS + ("size_estimate", "size_confidence", "size_basis")
    rows = _by_id(conn, f"SELECT v.entity_id AS entity_id, {_select(have, fields, 'v')} FROM EntitiesV2 v "
                        "WHERE v.entity_id IN ({ids})", entity_ids)
    return {r["entity_id"]: dict(r) for r in rows}


def _profiles(conn: sqlite3.Connection, entity_ids: Sequence[int]) -> Dict[int, Dict[str, List[str]]]:
    if "EntityProfiles" not in _tables(conn):
        return {}
    out: Dict[int, Dict[str, List[str]]] = {}
    # A person's value first, as entity_profiles.profile_for orders them.
    for row in _by_id(conn, "SELECT entity_id, kind, value FROM EntityProfiles WHERE entity_id IN ({ids}) "
                            "ORDER BY entity_id, kind, source = 'llm', value", entity_ids):
        out.setdefault(row["entity_id"], {}).setdefault(row["kind"], []).append(row["value"])
    return out


def _roles(conn: sqlite3.Connection, event_ids: Sequence[str]) -> Dict[str, Dict[str, List[str]]]:
    out: Dict[str, Dict[str, List[str]]] = {}
    for row in _by_id(conn, "SELECT dee.deduplicated_event_id AS event_id, dee.relationship_type AS role, "
                            "v.entity_name AS name FROM DeduplicatedEventEntities dee "
                            "JOIN EntitiesV2 v ON v.entity_id = dee.entity_id "
                            "WHERE dee.deduplicated_event_id IN ({ids}) "
                            "ORDER BY v.entity_name COLLATE NOCASE", event_ids):
        out.setdefault(row["event_id"], {}).setdefault(row["role"] or "", []).append(row["name"])
    return out


def _asd(conn: sqlite3.Connection, event_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """ASD classifications joined to Active events only (the reporting population's rule)."""
    if "ASDRiskClassifications" not in _tables(conn):
        return {}
    where, params = incident_filter(None, "de")
    sql = ("SELECT arc.deduplicated_event_id AS event_id, arc.severity_category, arc.primary_stakeholder_category, "
           "arc.impact_type FROM ASDRiskClassifications arc "
           "JOIN DeduplicatedEvents de ON arc.deduplicated_event_id = de.deduplicated_event_id "
           f"WHERE {where} AND arc.deduplicated_event_id IN ({{ids}}) ORDER BY arc.updated_at, arc.rowid")
    out: Dict[str, Dict[str, Any]] = {}
    for chunk in _chunks(list(event_ids)):
        for row in conn.execute(sql.format(ids=",".join("?" * len(chunk))), list(params) + list(chunk)):
            out[row["event_id"]] = dict(row)            # the latest classification of the event
    return out


def _supplier_incidents(conn: sqlite3.Connection) -> Dict[str, List[str]]:
    """Each Active event's supplier incidents: ``supplier (customer's event)`` / ``supplier (supplier's own event)``."""
    if not {"SupplierIncidents", "SupplierIncidentLinks"} <= _tables(conn):
        return {}
    from cyber_data_collector.dedup.supplier_links import resolve_event

    out: Dict[str, List[str]] = {}

    def add(event_id: Optional[str], text: str) -> None:
        now = resolve_event(conn, event_id)
        if now and text not in out.setdefault(now, []):
            out[now].append(text)

    for event_id, supplier in conn.execute(
            "SELECT l.customer_event_id, i.supplier_name FROM SupplierIncidentLinks l "
            "JOIN SupplierIncidents i ON i.supplier_incident_id = l.supplier_incident_id "
            "WHERE l.customer_event_id IS NOT NULL ORDER BY i.supplier_name, i.first_date"):
        add(event_id, f"{supplier} (customer's event)")
    for event_id, supplier in conn.execute(
            "SELECT supplier_event_id, supplier_name FROM SupplierIncidents WHERE supplier_event_id IS NOT NULL "
            "ORDER BY supplier_name, first_date"):
        add(event_id, f"{supplier} (supplier's own event)")
    return out


def load_rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """One row per (organisation, event) of ``entities.html``, in its order, with every attribute (see COLUMNS)."""
    from scripts.build_entity_dashboard import load_rows as entity_rows

    conn.row_factory = sqlite3.Row
    base = entity_rows(conn)
    event_ids = sorted({r["event_id"] for r in base})
    entity_ids = sorted({r["entity_id"] for r in base})
    events = _event_attributes(conn, event_ids)
    entities = _entity_attributes(conn, entity_ids)
    profiles = _profiles(conn, entity_ids)
    roles = _roles(conn, event_ids)
    asd = _asd(conn, event_ids)
    suppliers = _supplier_incidents(conn)
    victims_on_event: Dict[str, int] = {}
    for r in base:
        victims_on_event[r["event_id"]] = victims_on_event.get(r["event_id"], 0) + 1

    out: List[Dict[str, Any]] = []
    for r in base:
        ev = events.get(r["event_id"], {})
        en = entities.get(r["entity_id"], {})
        pr = profiles.get(r["entity_id"], {})
        rl = roles.get(r["event_id"], {})
        cl = asd.get(r["event_id"], {})
        description = (ev.get("full_description") or "").strip()
        if description.startswith("%PDF"):          # raw page scrape, as entities.html drops it
            description = ""
        no_ai = ev.get("no_ai_probability")
        row = {
            "entity": r["entity"],
            "entity_id": r["entity_id"],
            "role": "victim",
            "entity_type": _text(en.get("entity_type")),
            "industry": _text(en.get("industry")),
            "entity_kind": _text(en.get("entity_kind")),
            "size": r["size"],
            "size_researched": "yes" if r["estimated"] else "no",
            "size_employees": en.get("size_employees"),
            "size_revenue_aud": en.get("size_revenue_aud"),
            "size_confidence": r["size_confidence"],
            "size_basis": _text(r["size_basis"]),
            "is_australian": _flag(en.get("is_australian")),
            "headquarters": _text(en.get("headquarters_location")),
            "attribution_unresolved": _text(r["attribution"]),
            "attribution_reason": _text(r["attribution_reason"]),
            "event_id": r["event_id"],
            "title": r["title"],
            "description": description or None,
            "event_type": _text(ev.get("event_type")),
            "severity": _text(ev.get("severity")),
            "records_affected": ev.get("records_affected"),
            "ai_involvement": _text(ev.get("ai_involvement")),
            "ai_involvement_basis": _text(ev.get("ai_involvement_basis")),
            "p_ai": round(1 - float(no_ai), 4) if no_ai is not None else None,
            "attacker": _text(ev.get("attacking_entity_name")),
            "threat_actors": _join(rl.get("threat_actor", [])),
            "attack_method": _text(ev.get("attack_method")),
            "victims_on_event": victims_on_event[r["event_id"]],
            "stored_victim": _text(ev.get("victim_organization_name")),
            "stored_victim_industry": _text(ev.get("victim_organization_industry")),
            "confidence_score": ev.get("confidence_score"),
            "event_date": _date(ev.get("event_date")),
            "event_date_precision": _text(ev.get("event_date_precision")),
            "event_date_earliest": _date(ev.get("event_date_earliest")),
            "event_date_latest": _date(ev.get("event_date_latest")),
            "date_precision": _text(ev.get("date_precision")),
            "date_basis": _text(ev.get("date_basis")),
            "first_reported_date": _date(ev.get("first_reported_date")),
            "reporting_lag_days": ev.get("reporting_lag_days"),
            "vendor": _join([ev.get("vendor_organization_name")] + rl.get("vendor", [])),
            "service_provider": _join(rl.get("service_provider", [])),
            "supplier_incident": _join(suppliers.get(r["event_id"], [])),
            "asd_severity": _text(cl.get("severity_category")),
            "asd_stakeholder": _text(cl.get("primary_stakeholder_category")),
            "asd_impact": _text(cl.get("impact_type")),
            "evidence_status": _text(r["evidence"]),
            "evidence_reason": _text(r["evidence_reason"]),
            "evidence_uncertain_fields": uncertain_fields(ev.get("evidence_uncertain_fields")),
            "breach_source_category": _text(ev.get("breach_source_category")),
            "personal_info_types": personal_info_types(ev.get("personal_info_types_json")),
            "source_records": r["members"],
            "total_data_sources": ev.get("total_data_sources"),
            "dedup_method": _text(ev.get("dedup_method")),
            "dedup_certainty": ev.get("dedup_certainty"),
            "human_override": _flag(ev.get("has_human_override")),
        }
        for kind, key in PROFILE_KINDS.items():
            row[key] = _join(pr.get(kind, []))
        if not row["website"]:
            row["website"] = _text(en.get("website_url"))
        out.append({key: row.get(key) for key in COLUMN_KEYS})
    return out


def empty_shares(rows: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    """Share of rows with no value, per column (0.0 when there are no rows)."""
    if not rows:
        return {key: 0.0 for key in COLUMN_KEYS}
    return {key: sum(1 for r in rows if r.get(key) in (None, "")) / len(rows) for key in COLUMN_KEYS}


def mostly_empty(rows: Sequence[Dict[str, Any]], threshold: float = MOSTLY_EMPTY) -> List[Tuple[str, float]]:
    """``(label, share empty)`` of every column at least ``threshold`` empty, emptiest first."""
    labels = {c.key: c.label for c in COLUMNS}
    shares = empty_shares(rows)
    return sorted(((labels[k], s) for k, s in shares.items() if s >= threshold), key=lambda x: (-x[1], x[0]))


# --- HTML ---------------------------------------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cyber events by entity: every attribute</title>
<style>
:root { color-scheme: light dark;
  --bg:#f6f7f9; --panel:#fff; --head:#eef1f5; --line:#dde1e8; --soft:#eef0f3; --text:#16181d;
  --muted:#5f6672; --accent:#2b5a86; --mark:#fff6d6; --flag:#8c2f2f; --flagbg:#fdecec; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#14161a; --panel:#1b1e24; --head:#232730; --line:#2e333d; --soft:#23272e; --text:#e6e8ec;
  --muted:#9aa1ac; --accent:#8ab8e6; --mark:#2f2914; --flag:#e88a8a; --flagbg:#331a1a; } }
* { box-sizing:border-box; }
body { margin:0; padding:16px; background:var(--bg); color:var(--text);
       font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
h1 { font-size:20px; margin:0 0 4px; }
.sub { color:var(--muted); font-size:13px; margin:0 0 12px; max-width:110ch; }
.bar { display:flex; flex-wrap:wrap; gap:10px 16px; align-items:center; background:var(--panel);
       border:1px solid var(--line); border-radius:8px; padding:10px 12px; margin-bottom:10px; }
.bar input[type=search] { flex:1 1 240px; min-width:180px; }
input, button { font:inherit; font-size:13px; color:var(--text); background:var(--bg);
                border:1px solid var(--line); border-radius:6px; padding:6px 9px; }
button { cursor:pointer; }
.groups { display:flex; flex-wrap:wrap; gap:4px 12px; font-size:13px; }
.groups label { display:flex; gap:5px; align-items:center; white-space:nowrap; cursor:pointer; }
.stat { color:var(--muted); font-size:13px; margin-left:auto; }
.scroll { overflow:auto; max-height:calc(100vh - 170px); border:1px solid var(--line); border-radius:8px;
          background:var(--panel); -webkit-overflow-scrolling:touch; }
table { border-collapse:separate; border-spacing:0; font-size:13px; }
th, td { padding:6px 9px; border-bottom:1px solid var(--soft); text-align:left; vertical-align:top;
         white-space:nowrap; }
thead th { position:sticky; background:var(--head); z-index:2; border-bottom:1px solid var(--line); }
thead tr.labels th { top:0; font-size:12px; color:var(--muted); cursor:pointer; user-select:none; }
thead tr.labels th:hover { color:var(--text); }
thead tr.filters th { top:var(--labels-h, 31px); padding:4px 6px; }
thead tr.filters input { width:100%; min-width:70px; padding:3px 6px; font-size:12px; }
th .dir { color:var(--accent); }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
td.date { font-variant-numeric:tabular-nums; }
td.long { white-space:normal; min-width:240px; max-width:420px; }
td.long.desc { min-width:320px; }
.clamp { display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; cursor:pointer; }
td.open .clamp { display:block; }
.c-entity { position:sticky; left:0; z-index:1; background:var(--panel); font-weight:600;
            max-width:220px; white-space:normal; }
thead .c-entity { z-index:3; background:var(--head); }
tr.first td { border-top:2px solid var(--line); }
td.flag { color:var(--flag); background:var(--flagbg); font-weight:600; }
tbody tr:hover td { background:var(--mark); }
.empty { padding:24px; color:var(--muted); }
GROUP_CSS
@media (max-width: 640px) { body { padding:8px; } .scroll { max-height:none; } .c-entity { max-width:140px; } }
</style></head><body>
<h1>Cyber events by entity: every attribute</h1>
<p class="sub">__SUB__</p>
<div class="bar">
  <input type="search" id="q" placeholder="Filter every column..." aria-label="Filter every column">
  <div class="groups" id="groups"></div>
  <button type="button" id="reset">Reset sort and filters</button>
  <span class="stat" id="stat"></span>
</div>
<div class="scroll"><table id="t"><thead></thead><tbody></tbody></table>
<div class="empty" id="none" hidden>Nothing matches these filters.</div></div>
<script>
const COLS = __COLS__;
const ROWS = __ROWS__;
const GROUPS = __GROUPS__;
const table = document.getElementById('t'), thead = table.tHead, tbody = table.tBodies[0];
const q = document.getElementById('q'), stat = document.getElementById('stat');
const none = document.getElementById('none');
const slug = g => 'g-' + g.toLowerCase().replace(/[^a-z]+/g, '-');
// The organisation column stays visible (and pinned) whichever groups are hidden.
const cls = (c, i) => i === 0 ? 'c-entity' : slug(c.group);

function show(v, kind) {
  if (v === null || v === undefined || v === '') return '';
  if (kind === 'number' && typeof v === 'number') return v.toLocaleString(undefined, {maximumFractionDigits: 4});
  return String(v);
}

// Header: labels (click to sort) and one filter box per column.
const labels = document.createElement('tr'); labels.className = 'labels';
const filters = document.createElement('tr'); filters.className = 'filters';
const boxes = [];
COLS.forEach((c, i) => {
  const th = document.createElement('th');
  th.className = cls(c, i);
  th.textContent = c.label + ' ';
  const dir = document.createElement('span'); dir.className = 'dir'; th.appendChild(dir);
  th.title = c.source + ' - ' + c.empty + '% of rows empty. Click to sort.';
  th.addEventListener('click', () => sortBy(i));
  labels.appendChild(th);
  const fh = document.createElement('th');
  fh.className = cls(c, i);
  const box = document.createElement('input');
  box.type = 'search';
  box.placeholder = (c.kind === 'number' || c.kind === 'id') ? '>, <, =' : 'filter';
  box.setAttribute('aria-label', 'Filter ' + c.label);
  box.addEventListener('input', apply);
  fh.appendChild(box); boxes.push(box); filters.appendChild(fh);
});
thead.appendChild(labels); thead.appendChild(filters);
document.documentElement.style.setProperty('--labels-h', labels.getBoundingClientRect().height + 'px');

// Body: every row built once; filtering hides rows, sorting reorders them.
const trs = ROWS.map((r, n) => {
  const tr = document.createElement('tr');
  COLS.forEach((c, i) => {
    const td = document.createElement('td');
    td.className = cls(c, i) +
      (c.kind === 'number' || c.kind === 'id' ? ' num' : c.kind === 'date' ? ' date' : c.kind === 'long' ? ' long' : '') +
      (c.key === 'description' ? ' desc' : '') +
      (c.key === 'attribution_unresolved' && r[i] ? ' flag' : '');
    const text = show(r[i], c.kind);
    if (c.kind === 'long' && text) {
      // Long text shows three lines; a click (or tap) shows all of it.
      const div = document.createElement('div'); div.className = 'clamp'; div.textContent = text;
      td.appendChild(div); td.addEventListener('click', () => td.classList.toggle('open'));
    } else td.textContent = text;
    tr.appendChild(td);
  });
  tr._n = n;
  tr._text = r.map(v => v === null ? '' : String(v)).join(' \\u0001 ').toLowerCase();
  return tr;
});
trs.forEach(tr => tbody.appendChild(tr));

let order = trs.slice(), sortCol = -1, sortDir = 1;

function sortBy(i) {
  if (sortCol === i) sortDir = -sortDir; else { sortCol = i; sortDir = 1; }
  const kind = COLS[i].kind;
  order = trs.slice().sort((a, b) => {
    const x = ROWS[a._n][i], y = ROWS[b._n][i];
    if (x === null || x === '') return (y === null || y === '') ? a._n - b._n : 1;   // empty last
    if (y === null || y === '') return -1;
    const c = (kind === 'number' || kind === 'id') ? x - y : String(x).localeCompare(String(y), undefined, {sensitivity: 'base', numeric: true});
    return c ? c * sortDir : a._n - b._n;
  });
  labels.querySelectorAll('.dir').forEach((s, j) => { s.textContent = j === i ? (sortDir > 0 ? '\\u25B2' : '\\u25BC') : ''; });
  order.forEach(tr => tbody.appendChild(tr));
  apply();
}

function test(value, kind, f) {
  if (kind === 'number' || kind === 'id') {
    const m = f.match(/^(>=|<=|>|<|=)\\s*(-?[\\d.,]+)$/);
    if (m) {
      if (value === null || value === '') return false;
      const n = parseFloat(m[2].replace(/,/g, '')), v = Number(value);
      return m[1] === '>' ? v > n : m[1] === '<' ? v < n : m[1] === '>=' ? v >= n : m[1] === '<=' ? v <= n : v === n;
    }
  }
  if (f === '(empty)') return value === null || value === '';
  return (value === null ? '' : String(value)).toLowerCase().includes(f);
}

function apply() {
  const needle = q.value.trim().toLowerCase();
  const fs = boxes.map(b => b.value.trim().toLowerCase());
  let shown = 0; const events = new Set(), orgs = new Set(); let prev = null;
  for (const tr of order) {
    const r = ROWS[tr._n];
    const ok = (!needle || tr._text.includes(needle)) &&
               fs.every((f, i) => !f || test(r[i], COLS[i].kind, f));
    tr.hidden = !ok;
    tr.classList.remove('first');
    if (ok) {
      shown++; events.add(r[EVENT]); orgs.add(r[0]);
      if (sortCol <= 0 && r[0] !== prev) tr.classList.add('first');
      prev = r[0];
    }
  }
  none.hidden = shown > 0;
  stat.textContent = shown + ' of ' + ROWS.length + ' rows; ' + orgs.size + ' organisation(s), ' + events.size + ' event(s)';
}
const EVENT = COLS.findIndex(c => c.key === 'event_id');

// Column groups can be hidden (a phone shows fewer at a time).
const groupBox = document.getElementById('groups');
GROUPS.forEach(g => {
  const label = document.createElement('label');
  const box = document.createElement('input'); box.type = 'checkbox'; box.checked = true;
  box.addEventListener('change', () => table.classList.toggle('hide-' + slug(g), !box.checked));
  label.appendChild(box); label.appendChild(document.createTextNode(g)); groupBox.appendChild(label);
});

q.addEventListener('input', apply);
document.getElementById('reset').addEventListener('click', () => {
  q.value = ''; boxes.forEach(b => { b.value = ''; });
  sortCol = -1; sortDir = 1; order = trs.slice();
  labels.querySelectorAll('.dir').forEach(s => { s.textContent = ''; });
  order.forEach(tr => tbody.appendChild(tr)); apply();
});
apply();
</script></body></html>
"""


def _slug(group: str) -> str:
    out = "".join(ch if ch.isalpha() else "-" for ch in group.lower())
    while "--" in out:
        out = out.replace("--", "-")
    return "g-" + out


def build_html(rows: Sequence[Dict[str, Any]], note: str = "") -> str:
    """The page for ``rows`` (from :func:`load_rows`); every database value is escaped."""
    shares = empty_shares(rows)
    columns = [{"key": c.key, "label": c.label, "group": c.group, "kind": c.kind, "source": c.source,
                "empty": round(100 * shares[c.key])} for c in COLUMNS]
    events = len({r["event_id"] for r in rows})
    orgs = len({r["entity"] for r in rows})
    sparse = mostly_empty(rows)
    sub = (f"{len(rows)} row(s): one per victim organisation and event, the same rows as entities.html "
           f"({orgs} organisation(s), {events} event(s)), sorted by organisation then event date, with "
           f"{len(COLUMNS)} columns. Click a heading to sort; type in the box under it to filter (numbers take "
           f">, <, >=, <= or =; '(empty)' finds blanks); click long text to see all of it. Hover a heading for its source and how much of it is "
           f"empty. "
           + (f"Mostly empty: {', '.join(f'{label} ({share:.0%})' for label, share in sparse)}. " if sparse else "")
           + note
           + f"Generated {datetime.now():%Y-%m-%d %H:%M}.")
    group_css = "\n".join(f"table.hide-{_slug(g)} .{_slug(g)} {{ display:none; }}" for g in GROUPS)
    data = [[r.get(c.key) for c in COLUMNS] for r in rows]
    return (PAGE.replace("GROUP_CSS", group_css)
            .replace("__SUB__", html.escape(sub))
            .replace("__GROUPS__", script_json(list(GROUPS)))
            .replace("__COLS__", script_json(columns, ensure_ascii=False))
            .replace("__ROWS__", script_json(data, ensure_ascii=False, default=str)))


# --- Excel ----------------------------------------------------------------------------------------------------

WIDTHS = {"text": 22, "number": 14, "id": 10, "date": 12, "long": 50}


def build_xlsx(rows: Sequence[Dict[str, Any]], path: Path):
    """The same rows and columns as the page (sheet ``Entity events``), and a ``Columns`` sheet describing them."""
    from scripts.export.table_workbook import Sheet, write_workbook

    shares = empty_shares(rows)
    data = Sheet(SHEET, [c.label for c in COLUMNS], [[r.get(c.key) for c in COLUMNS] for r in rows],
                 [WIDTHS[c.kind] if c.key != "entity" else 32 for c in COLUMNS])
    legend = Sheet(COLUMNS_SHEET, ["Column", "Group", "Source", "Share of rows empty"],
                   [[c.label, c.group, c.source, round(shares[c.key], 4)] for c in COLUMNS], [28, 14, 90, 18])
    return write_workbook(path, [data, legend])


def main(argv: Optional[List[str]] = None) -> int:
    from scripts.build_entity_dashboard import _connect, population_note, summarise
    from scripts.export.table_workbook import describe

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--xlsx", default=DEFAULT_XLSX)
    args = parser.parse_args(argv)

    conn = _connect(args.db)
    try:
        rows = load_rows(conn)
        note = population_note(conn, summarise(rows))
    finally:
        conn.close()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(rows, note), encoding="utf-8")
    report = build_xlsx(rows, Path(args.xlsx))
    print(f"Wrote {out} and {args.xlsx}: {len(rows)} rows x {len(COLUMNS)} columns")
    print(f"  {describe(report)}")
    for label, share in mostly_empty(rows):
        print(f"  mostly empty: {label} ({share:.0%})")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
