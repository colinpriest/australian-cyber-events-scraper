"""Store enum members by their value, and repair rows stored by their name.

``CyberEventType``, ``EventSeverity`` and ``EntityType`` are ``(str, Enum)``
mixins. On Python 3.11+ ``str(member)`` and ``f"{member}"`` return the
qualified name (``"EventSeverity.MEDIUM"``), not the value (``"Medium"``), so
every writer that stringified a member stored the name. Those spellings then
spread: raw records kept them in ``RawEvents.source_metadata`` and each
re-enrichment copied them into ``EnrichedEvents`` and on to
``DeduplicatedEvents`` (28 enriched rows on 2026-10-27, all re-enrichments of
old raw records), and ``EntitiesV2.entity_type`` held only names. A
``VARCHAR(20)`` validator also cut ``"EventSeverity.UNKNOWN"`` to
``"EventSeverity.UNKNOW"``.

:func:`canonical_enum_value` is the one conversion every write path uses, and
:func:`normalise_stored_enums` rewrites rows already stored (idempotent: a
second run changes nothing).
"""

from __future__ import annotations

import json
import sqlite3
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Type

from cyber_data_collector.models.events import CyberEventType, EntityType, EventSeverity

ENUM_CLASSES: Dict[str, Type[Enum]] = {
    "CyberEventType": CyberEventType,
    "EventSeverity": EventSeverity,
    "EntityType": EntityType,
}

#: Columns that hold one of these enums' values.
ENUM_COLUMNS: Tuple[Tuple[str, str, Type[Enum]], ...] = (
    ("EnrichedEvents", "event_type", CyberEventType),
    ("EnrichedEvents", "severity", EventSeverity),
    ("DeduplicatedEvents", "event_type", CyberEventType),
    ("DeduplicatedEvents", "severity", EventSeverity),
    ("EntitiesV2", "entity_type", EntityType),
)
#: JSON keys in ``RawEvents.source_metadata`` that are copied into those columns.
METADATA_KEYS: Dict[str, Type[Enum]] = {"event_type": CyberEventType, "severity": EventSeverity}


def _member(enum_cls: Type[Enum], name: str) -> Optional[Enum]:
    """The member called ``name``, or the only member a truncated name can be."""
    if name in enum_cls.__members__:
        return enum_cls.__members__[name]
    matches = [m for n, m in enum_cls.__members__.items() if name and n.startswith(name)]
    return matches[0] if len(matches) == 1 else None


def canonical_enum_value(value: Any, enum_cls: Optional[Type[Enum]] = None) -> Any:
    """The value to store for ``value``.

    * an Enum member -> its ``.value``;
    * a qualified name (``"EventSeverity.MEDIUM"``, or a truncated legacy one
      such as ``"EventSeverity.UNKNOW"``) -> that member's value;
    * with ``enum_cls``, a bare member name (``"OTHER"``) -> its value.

    Anything else, including None and values already canonical, is returned
    unchanged, so the function is safe on every write.
    """
    if isinstance(value, Enum):
        return value.value
    if not isinstance(value, str):
        return value
    text = value.strip()
    prefix, dot, name = text.partition(".")
    if dot and prefix in ENUM_CLASSES and (enum_cls is None or ENUM_CLASSES[prefix] is enum_cls):
        member = _member(ENUM_CLASSES[prefix], name)
        if member is not None:
            return member.value
    if enum_cls is not None and text in enum_cls.__members__:
        return enum_cls.__members__[text].value
    return value


def canonical_metadata(metadata: Any) -> Any:
    """``source_metadata`` with its enum fields stored by value."""
    if not isinstance(metadata, dict):
        return metadata
    out = dict(metadata)
    for key, enum_cls in METADATA_KEYS.items():
        if key in out:
            out[key] = canonical_enum_value(out[key], enum_cls)
    return out


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def legacy_spellings(conn: sqlite3.Connection) -> Dict[str, Dict[str, int]]:
    """Every stored legacy spelling, as ``{"Table.column": {value: rows}}``.

    ``RawEvents.source_metadata`` is reported per JSON key
    (``RawEvents.source_metadata.severity``).
    """
    found: Dict[str, Dict[str, int]] = {}
    for table, column, enum_cls in ENUM_COLUMNS:
        if not _table_exists(conn, table):
            continue
        for value, n in conn.execute(f"SELECT {column}, COUNT(*) FROM {table} "
                                     f"WHERE typeof({column}) = 'text' GROUP BY {column}"):
            if canonical_enum_value(value, enum_cls) != value:
                found.setdefault(f"{table}.{column}", {})[value] = n
    if _table_exists(conn, "RawEvents"):
        for (blob,) in conn.execute(
                "SELECT source_metadata FROM RawEvents WHERE source_metadata LIKE '%EventSeverity.%' "
                "OR source_metadata LIKE '%CyberEventType.%'"):
            try:
                meta = json.loads(blob)
            except (TypeError, ValueError):
                continue
            if not isinstance(meta, dict):
                continue
            for key, enum_cls in METADATA_KEYS.items():
                value = meta.get(key)
                if canonical_enum_value(value, enum_cls) != value:
                    bucket = found.setdefault(f"RawEvents.source_metadata.{key}", {})
                    bucket[value] = bucket.get(value, 0) + 1
    return found


def normalise_stored_enums(conn: sqlite3.Connection, dry_run: bool = False) -> Dict[str, int]:
    """Rewrite every stored legacy spelling to the canonical value.

    Returns rows changed (or that would change) per ``Table.column``. Rerunning
    is a no-op. One transaction: either every column is repaired or none is.
    """
    changed: Dict[str, int] = {}
    try:
        for table, column, enum_cls in ENUM_COLUMNS:
            if not _table_exists(conn, table):
                continue
            values = [v for (v,) in conn.execute(
                f"SELECT DISTINCT {column} FROM {table} WHERE typeof({column}) = 'text'")]
            for value in values:
                canonical = canonical_enum_value(value, enum_cls)
                if canonical == value:
                    continue
                if dry_run:
                    n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (value,)).fetchone()[0]
                else:
                    n = conn.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                                     (canonical, value)).rowcount
                changed[f"{table}.{column}"] = changed.get(f"{table}.{column}", 0) + n
        if _table_exists(conn, "RawEvents"):
            updates: List[Tuple[str, str]] = []
            for raw_id, blob in conn.execute(
                    "SELECT raw_event_id, source_metadata FROM RawEvents "
                    "WHERE source_metadata LIKE '%EventSeverity.%' OR source_metadata LIKE '%CyberEventType.%'"):
                try:
                    meta = json.loads(blob)
                except (TypeError, ValueError):
                    continue
                fixed = canonical_metadata(meta)
                if fixed != meta:
                    updates.append((json.dumps(fixed), raw_id))
            if updates:
                changed["RawEvents.source_metadata"] = len(updates)
                if not dry_run:
                    conn.executemany("UPDATE RawEvents SET source_metadata = ? WHERE raw_event_id = ?", updates)
        n = _normalise_record_fingerprints(conn, dry_run)
        if n:
            changed["RecordFingerprints.values_json"] = n
        if not dry_run:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    return changed


def _normalise_record_fingerprints(conn: sqlite3.Connection, dry_run: bool) -> int:
    """Re-spell the remembered record values, so the repair is not seen as an edit.

    ``RecordFingerprints`` remembers each placed record's values to detect
    re-enrichment. Without this, rewriting ``"EventSeverity.MEDIUM"`` to
    ``"Medium"`` would look like a changed record on the next refresh.
    """
    if not _table_exists(conn, "RecordFingerprints"):
        return 0
    from cyber_data_collector.dedup.record_fingerprints import fingerprint

    updates: List[Tuple[str, str, str]] = []
    for record_id, blob in conn.execute(
            "SELECT enriched_event_id, values_json FROM RecordFingerprints "
            "WHERE values_json LIKE '%EventSeverity.%' OR values_json LIKE '%CyberEventType.%'"):
        try:
            values = json.loads(blob)
        except (TypeError, ValueError):
            continue
        fixed = canonical_metadata(values)
        if fixed != values:
            updates.append((fingerprint(fixed), json.dumps(fixed, default=str, ensure_ascii=False), record_id))
    if updates and not dry_run:
        conn.executemany("UPDATE RecordFingerprints SET fingerprint = ?, values_json = ? "
                         "WHERE enriched_event_id = ?", updates)
    return len(updates)


def industry_from_entity_type(entity_type: Any) -> Optional[str]:
    """The industry label the entity type implies, whichever spelling is stored.

    The industry fallback used to read the member name out of the stored
    qualified name (``"EntityType.FINANCIAL"`` -> ``"Financial"``). Stored by
    value the same entity reads ``"Financial Institution"``; mapping the value
    back to its member keeps the industry each entity is given unchanged.
    """
    if not entity_type:
        return None
    text = str(canonical_enum_value(entity_type, EntityType))
    try:
        raw = EntityType(text).name
    except ValueError:
        raw = text.split(".")[-1]
    if raw and raw.isalpha():
        return raw.title()
    return None
