"""Owner labels on an event's supplier: an organisation that is not the supplier of an event.

The role classifier names a supplier from the reporting, and a report often mentions another breach of the same
month: Outabox's May 2024 breach (ClubsNSW) beside Ticketek's, OracleCMS's April 2024 call-centre breach beside
Nissan's own December 2023 attack. The owner ruled on both (2026-11-01, 2026-11-02), and ``entity-roles-v10``
named them again (as service providers, trial on a copy, 2026-11-05). A label decides without a model call, as
every label does: a labelled organisation is not the event's vendor or service provider, at classification and in
what is stored.

``evaluation/supplier_role_labels.csv``: ``deduplicated_event_id,organisation,label,rationale,labeller``, label
``not_supplier``. The event id is followed through merges (``supplier_links.resolve_event``).
"""

from __future__ import annotations

import csv
import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Set

logger = logging.getLogger(__name__)

LABELS_PATH = Path(__file__).resolve().parents[2] / "evaluation" / "supplier_role_labels.csv"
NOT_SUPPLIER = "not_supplier"
SUPPLIER_ROLES = ("vendor", "service_provider")


def load(path: Path = LABELS_PATH) -> List[Dict[str, str]]:
    """The labels as rows; none when the file is absent (an empty registry is no error)."""
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return [row for row in csv.DictReader(f) if (row.get("label") or "").strip() == NOT_SUPPLIER]


def not_suppliers(conn: sqlite3.Connection, path: Path = LABELS_PATH) -> Dict[str, Set[str]]:
    """Active event -> the organisations labelled not its supplier."""
    from cyber_data_collector.dedup.supplier_links import resolve_event

    out: Dict[str, Set[str]] = {}
    for row in load(path):
        event = resolve_event(conn, (row.get("deduplicated_event_id") or "").strip())
        name = (row.get("organisation") or "").strip()
        if event and name:
            out.setdefault(event, set()).add(name)
    return out


def contradicts(conn: sqlite3.Connection, labels: Dict[str, Set[str]], event_id: str, supplier: str) -> bool:
    """True when ``event_id`` is labelled not an event of ``supplier``: it belongs to no incident of it."""
    names = labels.get(event_id)
    return bool(names) and is_labelled(conn, names, supplier)


def is_labelled(conn: sqlite3.Connection, names: Set[str], candidate: str) -> bool:
    from cyber_data_collector.dedup.victim_fill import names_match

    return any(candidate.strip().lower() == n.lower() or names_match(conn, candidate, n) for n in names)


def apply_to_result(conn: sqlite3.Connection, deduplicated_event_id: str, result,
                    labels: Dict[str, Set[str]]) -> List[str]:
    """Make a labelled organisation a bystander in a fresh classification; returns the names changed."""
    from cyber_data_collector.dedup.entity_classification import EntityRole

    names = labels.get(deduplicated_event_id)
    if not names:
        return []
    changed = []
    for a in result.assignments:
        if a.role in (EntityRole.VENDOR, EntityRole.SERVICE_PROVIDER) and is_labelled(conn, names, a.name):
            logger.info("%r is labelled not the supplier of %s: bystander", a.name, deduplicated_event_id)
            a.role = EntityRole.BYSTANDER
            changed.append(a.name)
    return changed


def apply_stored(conn: sqlite3.Connection, path: Path = LABELS_PATH) -> List[Dict[str, str]]:
    """Clear a labelled organisation's supplier roles on its event and on the event's records, and the vendor
    column when it names it. Idempotent; the caller commits. Returns what changed."""
    done: List[Dict[str, str]] = []
    for event, names in not_suppliers(conn, path).items():
        for entity_id, name in conn.execute(
                """SELECT DISTINCT v.entity_id, v.entity_name FROM EntitiesV2 v
                   WHERE v.entity_id IN (
                       SELECT entity_id FROM DeduplicatedEventEntities
                        WHERE deduplicated_event_id = ? AND relationship_type IN ('vendor', 'service_provider')
                       UNION
                       SELECT ee.entity_id FROM EventDeduplicationMap m
                         JOIN EnrichedEventEntities ee ON ee.enriched_event_id = m.enriched_event_id
                        WHERE m.deduplicated_event_id = ? AND ee.relationship_type IN ('vendor', 'service_provider'))""",
                (event, event)).fetchall():
            if not is_labelled(conn, names, name):
                continue
            conn.execute("UPDATE DeduplicatedEventEntities SET relationship_type = 'bystander' "
                         "WHERE deduplicated_event_id = ? AND entity_id = ? "
                         "AND relationship_type IN ('vendor', 'service_provider')", (event, entity_id))
            conn.execute("UPDATE EnrichedEventEntities SET relationship_type = 'bystander' WHERE entity_id = ? "
                         "AND relationship_type IN ('vendor', 'service_provider') AND enriched_event_id IN "
                         "(SELECT enriched_event_id FROM EventDeduplicationMap WHERE deduplicated_event_id = ?)",
                         (entity_id, event))
            done.append({"deduplicated_event_id": event, "organisation": name})
        column = conn.execute("SELECT vendor_organization_name FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                              (event,)).fetchone()
        if column and column[0] and is_labelled(conn, names, column[0]):
            conn.execute("UPDATE DeduplicatedEvents SET vendor_organization_name = NULL, "
                         "vendor_organization_industry = NULL WHERE deduplicated_event_id = ?", (event,))
            done.append({"deduplicated_event_id": event, "organisation": column[0]})
    for item in done:
        logger.info("Owner label: %s is not the supplier of %s", item["organisation"], item["deduplicated_event_id"])
    return done
