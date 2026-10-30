"""Folding one entity into another - recorded, lossless and reversible.

``merge_entities`` used to repoint the dropped entity's links, delete the
entity row and leave no trace: a link both spellings held on one event was
dropped silently (``UPDATE OR IGNORE`` then ``DELETE``), the attributes the
dropped row carried were inherited only *after* it was gone (so never), and a
fold that later proved wrong - Perplexity re-judging a pair as a different
organisation - could not be undone because the original names were gone.

A fold is now one transaction that writes an ``EntityMerges`` row holding
everything needed to put the database back:

* the dropped entity row and the kept row as they were (every column);
* every link the dropped entity held (both link tables), with what happened
  to it - ``moved`` onto the kept entity, or ``collided`` with a link the kept
  entity already held on that record or event. A collision keeps the **stronger
  role** (:data:`ROLE_STRENGTH`), the higher confidence and the summed source
  count on the surviving link, and records both original rows;
* the attributes inherited from the dropped row (blanks of the kept row only,
  taken **before** the dropped row is deleted);
* the events whose scalar victim or vendor name was renamed;
* the dropped entity's mentions (``EntityMentionResolutions``), the roles and
  record ids it held, the other tables that reference its id (left in place -
  :func:`unmerge_entity` restores the same id, so they point at it again), the
  alias row the fold relied on, and the verdict that decided it.

:func:`unmerge_entity` restores exactly that, refusing (and saying why) when
the database has moved on in a way it cannot reconcile - the dropped name is
in use again, or the kept entity was itself folded away later. It removes the
alias that would otherwise fold the pair again. :func:`reverse_overturned_merges`
runs it automatically for a fold decided by a model whose verdict has since
changed (the pair is now ``different`` / ``parent_subsidiary`` / ``related`` /
``unsure``); folds a person reviewed (``human``) are never reversed by a model.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Link table -> the column naming the record or event the link belongs to.
LINK_SUBJECTS = {"EnrichedEventEntities": "enriched_event_id", "DeduplicatedEventEntities": "deduplicated_event_id"}

#: Strongest first. When one record or event links both spellings, the link
#: keeps the stronger role and the other is recorded in the ledger.
ROLE_STRENGTH = ("victim", "vendor", "affected_customer", "primary_target", "affected", "threat_actor",
                 "regulator", "product", "mentioned", "bystander")

SCHEMA = """
CREATE TABLE IF NOT EXISTS EntityMerges (
    merge_id          TEXT PRIMARY KEY,
    kept_entity_id    INTEGER NOT NULL,
    kept_name         TEXT NOT NULL,
    dropped_entity_id INTEGER NOT NULL,
    dropped_name      TEXT NOT NULL,
    source            TEXT NOT NULL,       -- human (reviewed alias) / llm (model-confirmed) / structure
    decision_json     TEXT,                -- the EntityAliasDecisions verdict the fold rested on
    entity_row_json   TEXT NOT NULL,       -- the dropped EntitiesV2 row
    kept_before_json  TEXT NOT NULL,       -- the kept EntitiesV2 row before the fold
    inherited_json    TEXT,                -- {column: {"before": .., "after": ..}} filled on the kept row
    links_json        TEXT NOT NULL,       -- every link the dropped entity held, and what became of it
    scalar_json       TEXT,                -- events whose victim / vendor name was renamed
    mentions_json     TEXT,                -- EntityMentionResolutions rows naming the dropped entity
    roles_json        TEXT,                -- {table: {role: count}}
    source_ids_json   TEXT,                -- enriched / raw / deduplicated event ids of the links
    references_json   TEXT,                -- {table: row count} still keyed by the dropped id (left in place)
    alias_json        TEXT,                -- the EntityAliases row for dropped_name at fold time
    status            TEXT NOT NULL DEFAULT 'applied',     -- applied / reversed
    merged_at         TEXT NOT NULL,
    reversed_at       TEXT,
    reversed_reason   TEXT
);
CREATE INDEX IF NOT EXISTS idx_entitymerges_names ON EntityMerges(status, kept_name, dropped_name);
"""

REVERSIBLE_SOURCES = ("llm", "structure", "rule", "unspecified")
NOT_SAME = ("different", "parent_subsidiary", "related", "unsure")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the ledger table. Statement by statement: ``executescript`` would commit the caller's transaction."""
    for statement in SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)


def _ensure_decisions_table(conn: sqlite3.Connection) -> None:
    """The alias-verdict table, created before a savepoint opens (its creation commits)."""
    from cyber_data_collector.dedup.entity_alias_verification import ensure_schema as ensure_decisions

    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'EntityAliasDecisions'").fetchone():
        ensure_decisions(conn)


class UnmergeError(RuntimeError):
    """The fold cannot be undone as recorded; the message says what to resolve first."""


def _rows(conn: sqlite3.Connection, sql: str, params: Tuple = ()) -> List[Dict[str, Any]]:
    cursor = conn.execute(sql, params)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _tables(conn: sqlite3.Connection) -> List[str]:
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]


def role_rank(role: Optional[str]) -> int:
    """Lower is stronger; an unknown or missing role is weakest."""
    role = (role or "").strip()
    return ROLE_STRENGTH.index(role) if role in ROLE_STRENGTH else len(ROLE_STRENGTH)


def stronger_role(left: Optional[str], right: Optional[str]) -> Optional[str]:
    """The stronger of two roles; ties and unknowns keep the first."""
    return right if role_rank(right) < role_rank(left) else left


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


#: Size columns are one estimate: they are inherited together or not at all.
_SIZE_BLOCK = ("size_estimate", "size_confidence", "size_employees", "size_revenue_aud", "size_basis",
               "size_method", "size_sources", "size_estimated_at")
_NOT_INHERITED = {"entity_id", "entity_name", "created_at", "updated_at", *_SIZE_BLOCK}


def inherit_attributes(conn: sqlite3.Connection, keep_id: int, drop_id: int) -> Dict[str, Dict[str, Any]]:
    """Fill the kept entity's blank attributes from the dropped one; return ``{column: {before, after}}``.

    The best-spelled name is not always the best-populated row, so a fold
    must not lose an ``is_australian`` flag, industry or size estimate that
    the dropped spelling carried. Only blanks are filled - nothing is
    overwritten - and the size estimate moves as a unit. Call it **before** the
    dropped row is deleted.
    """
    keep = _rows(conn, "SELECT * FROM EntitiesV2 WHERE entity_id = ?", (keep_id,))
    drop = _rows(conn, "SELECT * FROM EntitiesV2 WHERE entity_id = ?", (drop_id,))
    if not keep or not drop:
        return {}
    keep, drop = keep[0], drop[0]
    changes: Dict[str, Dict[str, Any]] = {}
    for column, value in drop.items():
        if column in _NOT_INHERITED or _blank(value) or not _blank(keep.get(column)):
            continue
        changes[column] = {"before": keep.get(column), "after": value}
    if not _blank(drop.get("size_estimate")) and _blank(keep.get("size_estimate")):
        for column in _SIZE_BLOCK:
            if column in drop and drop[column] != keep.get(column):
                changes[column] = {"before": keep.get(column), "after": drop[column]}
    for column, change in changes.items():
        conn.execute(f"UPDATE EntitiesV2 SET {column} = ? WHERE entity_id = ?", (change["after"], keep_id))
    return changes


def _decision(conn: sqlite3.Connection, a: str, b: str) -> Optional[Dict[str, Any]]:
    """The stored alias verdict for the pair (current prompt version), if any."""
    try:
        from cyber_data_collector.dedup.entity_alias_verification import PROMPT_VERSION

        key = (a, b) if a <= b else (b, a)
        rows = _rows(conn, "SELECT verdict, canonical_name, reason, model, decided_at FROM EntityAliasDecisions "
                           "WHERE name = ? AND other_name = ? AND prompt_version = ?", (key[0], key[1], PROMPT_VERSION))
    except sqlite3.Error:
        return None
    return rows[0] if rows else None


def merge_pair(conn: sqlite3.Connection, keep_id: int, drop_id: int, source: str = "unspecified",
               merged_at: Optional[str] = None) -> Optional[str]:
    """Fold entity ``drop_id`` into ``keep_id`` and record it; return the ledger ``merge_id``.

    Atomic: a savepoint covers the ledger row and every change, so a failure
    leaves nothing half done. Does not commit. ``None`` when there is nothing
    to fold (same id, or either entity is missing).
    """
    ensure_schema(conn)
    if keep_id == drop_id:
        return None
    keep = _rows(conn, "SELECT * FROM EntitiesV2 WHERE entity_id = ?", (keep_id,))
    drop = _rows(conn, "SELECT * FROM EntitiesV2 WHERE entity_id = ?", (drop_id,))
    if not keep or not drop:
        return None
    keep, drop = keep[0], drop[0]
    merge_id = "em-" + uuid.uuid4().hex[:12]
    conn.execute("SAVEPOINT entity_merge")
    try:
        links: List[Dict[str, Any]] = []
        roles: Dict[str, Dict[str, int]] = {}
        source_ids: Dict[str, List[str]] = {"enriched_event_ids": [], "raw_event_ids": [], "deduplicated_event_ids": []}
        for table, subject_column in LINK_SUBJECTS.items():
            try:
                dropped_links = _rows(conn, f"SELECT * FROM {table} WHERE entity_id = ?", (drop_id,))
            except sqlite3.Error:
                continue
            columns = _columns(conn, table)
            for row in dropped_links:
                subject = row[subject_column]
                held = _rows(conn, f"SELECT * FROM {table} WHERE {subject_column} = ? AND entity_id = ?",
                             (subject, keep_id))
                entry: Dict[str, Any] = {"table": table, "subject_column": subject_column, "subject_id": subject,
                                         "row": row}
                role = row.get("relationship_type") or ""
                roles.setdefault(table, {})[role] = roles.setdefault(table, {}).get(role, 0) + 1
                if held:
                    kept_row = held[0]
                    entry["outcome"] = "collided"
                    entry["kept_row_before"] = kept_row
                    survivor = dict(kept_row)
                    survivor["relationship_type"] = stronger_role(kept_row.get("relationship_type"),
                                                                  row.get("relationship_type"))
                    confidences = [c for c in (kept_row.get("confidence_score"), row.get("confidence_score"))
                                   if c is not None]
                    if "confidence_score" in columns:
                        survivor["confidence_score"] = max(confidences) if confidences else None
                    if "source_count" in columns:
                        survivor["source_count"] = (kept_row.get("source_count") or 0) + (row.get("source_count") or 0)
                    entry["survivor_row"] = survivor
                    sets = [c for c in columns if c not in (subject_column, "entity_id")]
                    conn.execute(f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in sets)} "
                                 f"WHERE {subject_column} = ? AND entity_id = ?",
                                 [survivor[c] for c in sets] + [subject, keep_id])
                    conn.execute(f"DELETE FROM {table} WHERE {subject_column} = ? AND entity_id = ?",
                                 (subject, drop_id))
                else:
                    entry["outcome"] = "moved"
                    conn.execute(f"UPDATE {table} SET entity_id = ? WHERE {subject_column} = ? AND entity_id = ?",
                                 (keep_id, subject, drop_id))
                links.append(entry)
                if table == "EnrichedEventEntities":
                    source_ids["enriched_event_ids"].append(subject)
                    raw = conn.execute("SELECT raw_event_id FROM EnrichedEvents WHERE enriched_event_id = ?",
                                       (subject,)).fetchone()
                    if raw and raw[0] not in source_ids["raw_event_ids"]:
                        source_ids["raw_event_ids"].append(raw[0])
                else:
                    source_ids["deduplicated_event_ids"].append(subject)

        # Inherit from the dropped row while it still exists.
        inherited = inherit_attributes(conn, keep_id, drop_id)

        scalar: Dict[str, List[str]] = {}
        for label, column in (("victim", "victim_organization_name"), ("vendor", "vendor_organization_name")):
            try:
                ids = [r[0] for r in conn.execute(
                    f"SELECT deduplicated_event_id FROM DeduplicatedEvents WHERE {column} = ?", (drop["entity_name"],))]
            except sqlite3.Error:
                ids = []
            if ids:
                scalar[label] = ids
                conn.execute(f"UPDATE DeduplicatedEvents SET {column} = ? WHERE {column} = ?",
                             (keep["entity_name"], drop["entity_name"]))

        try:
            mentions = _rows(conn, "SELECT * FROM EntityMentionResolutions WHERE raw_name = ? OR canonical_name = ? "
                                   "OR moved_from = ?", (drop["entity_name"],) * 3)
        except sqlite3.Error:
            mentions = []
        references: Dict[str, List[Dict[str, Any]]] = {}
        from cyber_data_collector.dedup.input_history import SOURCES as history_sources

        # The input history is an append-only record of past states, not a live reference to the entity.
        history_tables = {s.history for s in history_sources}
        for table in _tables(conn):
            if (table in LINK_SUBJECTS or table == "EntitiesV2" or table in history_tables
                    or "entity_id" not in _columns(conn, table)):
                continue
            held = _rows(conn, f"SELECT * FROM {table} WHERE entity_id = ?", (drop_id,))
            if held:
                references[table] = held
        try:
            alias = _rows(conn, "SELECT * FROM EntityAliases WHERE raw_name = ?", (drop["entity_name"],))
        except sqlite3.Error:
            alias = []

        conn.execute("DELETE FROM EntitiesV2 WHERE entity_id = ?", (drop_id,))
        conn.execute(
            "INSERT INTO EntityMerges (merge_id, kept_entity_id, kept_name, dropped_entity_id, dropped_name, source, "
            "decision_json, entity_row_json, kept_before_json, inherited_json, links_json, scalar_json, "
            "mentions_json, roles_json, source_ids_json, references_json, alias_json, status, merged_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'applied', ?)",
            (merge_id, keep_id, keep["entity_name"], drop_id, drop["entity_name"], source,
             json.dumps(_decision(conn, keep["entity_name"], drop["entity_name"])), json.dumps(drop),
             json.dumps(keep), json.dumps(inherited), json.dumps(links), json.dumps(scalar), json.dumps(mentions),
             json.dumps(roles), json.dumps(source_ids), json.dumps({t: len(r) for t, r in references.items()}),
             json.dumps(alias[0] if alias else None), merged_at or datetime.now().isoformat(timespec="seconds")))
        conn.execute("RELEASE entity_merge")
    except Exception:
        conn.execute("ROLLBACK TO entity_merge")
        conn.execute("RELEASE entity_merge")
        raise
    return merge_id


def _insert(conn: sqlite3.Connection, table: str, row: Dict[str, Any]) -> None:
    columns = list(row)
    conn.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                 [row[c] for c in columns])


@dataclass
class UnmergeReport:
    merge_id: str
    kept_name: str = ""
    dropped_name: str = ""
    links_restored: int = 0
    collisions_restored: int = 0
    links_skipped: List[Tuple[str, str, str]] = field(default_factory=list)     # (table, subject, why)
    attributes_restored: List[str] = field(default_factory=list)
    attributes_kept: List[str] = field(default_factory=list)                    # changed since; left as they are
    events_renamed: int = 0
    alias_removed: bool = False
    still_aliased_by: Optional[str] = None      # a reviewed alias folds the pair again on the next run
    dry_run: bool = False

    def summary(self) -> str:
        return (f"{self.dropped_name!r} restored from {self.kept_name!r}: {self.links_restored} link(s) moved back, "
                f"{self.collisions_restored} collided link(s) split, {self.events_renamed} event name(s), "
                f"{len(self.attributes_restored)} attribute(s); {len(self.links_skipped)} link(s) skipped"
                + ("; alias removed" if self.alias_removed else ""))


def load_merge(conn: sqlite3.Connection, merge_id: str) -> Dict[str, Any]:
    ensure_schema(conn)
    rows = _rows(conn, "SELECT * FROM EntityMerges WHERE merge_id = ?", (merge_id,))
    if not rows:
        raise UnmergeError(f"no entity merge {merge_id!r}")
    return rows[0]


def unmerge_entity(conn: sqlite3.Connection, merge_id: str, reason: str = "", keep_alias: bool = False,
                   dry_run: bool = False) -> UnmergeReport:
    """Undo one recorded fold: bring the dropped entity back with its id, links, roles and attributes.

    Checks first, changes nothing when one fails (:class:`UnmergeError`): the
    fold must still be ``applied``, the dropped id and name must be free, and
    the kept entity must still exist (if it was folded away since, undo that
    fold first). Links are restored only where the kept entity still holds
    them; a link that has since moved elsewhere is reported, not recreated.
    Attributes changed since the fold stay as they are. The alias that
    resolved the dropped name to the kept one is removed (``keep_alias`` keeps
    it), and the pair is recorded as ``different`` so it is not proposed again.
    Does not commit.
    """
    from cyber_data_collector.dedup.entity_merge import EXPLICIT_ALIASES

    entry = load_merge(conn, merge_id)
    report = UnmergeReport(merge_id, entry["kept_name"], entry["dropped_name"], dry_run=dry_run)
    if entry["status"] != "applied":
        raise UnmergeError(f"merge {merge_id} is already {entry['status']}")
    drop_id, keep_id, drop_name = entry["dropped_entity_id"], entry["kept_entity_id"], entry["dropped_name"]
    if conn.execute("SELECT 1 FROM EntitiesV2 WHERE entity_id = ?", (drop_id,)).fetchone():
        raise UnmergeError(f"entity id {drop_id} exists again; cannot restore {drop_name!r}")
    holder = conn.execute("SELECT entity_id FROM EntitiesV2 WHERE entity_name = ?", (drop_name,)).fetchone()
    if holder:
        raise UnmergeError(f"the name {drop_name!r} is now entity {holder[0]}; resolve that first")
    if not conn.execute("SELECT 1 FROM EntitiesV2 WHERE entity_id = ?", (keep_id,)).fetchone():
        later = conn.execute("SELECT merge_id FROM EntityMerges WHERE dropped_entity_id = ? AND status = 'applied'",
                             (keep_id,)).fetchone()
        raise UnmergeError(f"the kept entity {entry['kept_name']!r} no longer exists"
                           + (f"; it was folded away by {later[0]} - unmerge that first" if later else ""))

    row = json.loads(entry["entity_row_json"])
    links = json.loads(entry["links_json"])
    inherited = json.loads(entry["inherited_json"] or "{}")
    scalar = json.loads(entry["scalar_json"] or "{}")

    if not keep_alias:
        _ensure_decisions_table(conn)
    conn.execute("SAVEPOINT entity_unmerge")
    try:
        _insert(conn, "EntitiesV2", row)
        current = _rows(conn, "SELECT * FROM EntitiesV2 WHERE entity_id = ?", (keep_id,))[0]
        for column, change in inherited.items():
            if current.get(column) == change["after"]:
                conn.execute(f"UPDATE EntitiesV2 SET {column} = ? WHERE entity_id = ?", (change["before"], keep_id))
                report.attributes_restored.append(column)
            else:
                report.attributes_kept.append(column)
        for link in links:
            table, column, subject = link["table"], link["subject_column"], link["subject_id"]
            held = conn.execute(f"SELECT 1 FROM {table} WHERE {column} = ? AND entity_id = ?",
                                (subject, keep_id)).fetchone()
            if not held:
                report.links_skipped.append((table, subject, "no longer linked to the kept entity"))
                continue
            if link["outcome"] == "collided":
                before = link["kept_row_before"]
                sets = [c for c in before if c not in (column, "entity_id")]
                conn.execute(f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in sets)} WHERE {column} = ? "
                             "AND entity_id = ?", [before[c] for c in sets] + [subject, keep_id])
                _insert(conn, table, link["row"])
                report.collisions_restored += 1
            else:
                conn.execute(f"UPDATE {table} SET entity_id = ? WHERE {column} = ? AND entity_id = ?",
                             (drop_id, subject, keep_id))
                report.links_restored += 1
        for label, column in (("victim", "victim_organization_name"), ("vendor", "vendor_organization_name")):
            for event_id in scalar.get(label, []):
                changed = conn.execute(f"UPDATE DeduplicatedEvents SET {column} = ? WHERE deduplicated_event_id = ? "
                                       f"AND {column} = ?", (drop_name, event_id, entry["kept_name"])).rowcount
                report.events_renamed += changed
        if not keep_alias:
            report.alias_removed = bool(conn.execute(
                "DELETE FROM EntityAliases WHERE raw_name = ? AND canonical_name = ?",
                (drop_name, entry["kept_name"])).rowcount)
            _record_not_same(conn, entry["kept_name"], drop_name, reason or "entity merge reversed")
        if EXPLICIT_ALIASES.get(drop_name) == entry["kept_name"]:
            report.still_aliased_by = "EXPLICIT_ALIASES"
        if dry_run:
            conn.execute("ROLLBACK TO entity_unmerge")
        else:
            conn.execute("UPDATE EntityMerges SET status = 'reversed', reversed_at = ?, reversed_reason = ? "
                         "WHERE merge_id = ?", (datetime.now().isoformat(timespec="seconds"), reason or None, merge_id))
        conn.execute("RELEASE entity_unmerge")
    except Exception:
        conn.execute("ROLLBACK TO entity_unmerge")
        conn.execute("RELEASE entity_unmerge")
        raise
    return report


def _record_not_same(conn: sqlite3.Connection, a: str, b: str, reason: str) -> None:
    """Remember the pair as ``different`` so it is not proposed again.

    Replaces a stored ``same`` (the verdict being overturned); a stored verdict that is already
    not-same (``parent_subsidiary``, ``related``, ``unsure``, ``different``) is kept.
    """
    # Without this verdict a later canonicalise-entities could fold the pair straight back, so a failure
    # raises. The table exists already (_ensure_decisions_table, before the savepoint opened).
    from cyber_data_collector.dedup.entity_alias_verification import PROMPT_VERSION

    key = (a, b) if a <= b else (b, a)
    conn.execute("DELETE FROM EntityAliasDecisions WHERE name = ? AND other_name = ? AND prompt_version = ? "
                 "AND verdict = 'same'", (key[0], key[1], PROMPT_VERSION))
    conn.execute("INSERT OR IGNORE INTO EntityAliasDecisions (name, other_name, verdict, canonical_name, reason, "
                 "citations_json, model, prompt_version, decided_at) VALUES (?, ?, 'different', NULL, ?, '[]', 'human', ?, ?)",
                 (key[0], key[1], reason[:500], PROMPT_VERSION, datetime.now().isoformat(timespec="seconds")))


def list_merges(conn: sqlite3.Connection, status: Optional[str] = None) -> List[Dict[str, Any]]:
    ensure_schema(conn)
    where, params = ("WHERE status = ?", (status,)) if status else ("", ())
    return _rows(conn, "SELECT merge_id, kept_name, dropped_name, source, status, merged_at, reversed_at, "
                       f"reversed_reason FROM EntityMerges {where} ORDER BY rowid", params)


def overturned_merges(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Applied folds a model decided whose pair now has a verdict other than ``same``, or is listed as an
    anti-alias (:mod:`anti_aliases`)."""
    from cyber_data_collector.dedup.anti_aliases import is_anti_alias, pair_set
    from cyber_data_collector.dedup.entity_merge import is_organisation_list

    ensure_schema(conn)
    anti = pair_set(conn)
    out = []
    for entry in _rows(conn, "SELECT merge_id, kept_name, dropped_name, source FROM EntityMerges "
                             "WHERE status = 'applied' ORDER BY rowid"):
        if entry["source"] not in REVERSIBLE_SOURCES:
            continue
        if is_anti_alias(anti, entry["kept_name"], entry["dropped_name"]):
            out.append({**entry, "verdict": "anti-alias", "reason": "listed in EntityAntiAliases"})
            continue
        if is_organisation_list(entry["kept_name"]) or is_organisation_list(entry["dropped_name"]):
            out.append({**entry, "verdict": "organisation list",
                        "reason": "a list of organisations or a case caption is no one organisation"})
            continue
        verdict = _decision(conn, entry["kept_name"], entry["dropped_name"])
        if verdict and verdict["verdict"] in NOT_SAME:
            out.append({**entry, "verdict": verdict["verdict"], "reason": verdict["reason"]})
    return out


def reverse_overturned_merges(conn: sqlite3.Connection, dry_run: bool = False
                              ) -> Tuple[List[UnmergeReport], List[Tuple[Dict[str, Any], str]]]:
    """Unmerge every fold a later verdict overturned; return ``(reports, refusals)``. Commits unless ``dry_run``.

    Later folds are undone first (a fold of the kept entity must go before the
    fold that produced it). A refusal (:class:`UnmergeError`) is returned with
    its reason, never raised, so one stuck fold does not hold up the rest.
    """
    reports: List[UnmergeReport] = []
    refused: List[Tuple[Dict[str, Any], str]] = []
    for entry in reversed(overturned_merges(conn)):
        try:
            report = unmerge_entity(conn, entry["merge_id"],
                                    reason=f"verdict changed to {entry['verdict']}: {entry['reason'] or ''}".strip(),
                                    dry_run=dry_run)
        except UnmergeError as exc:
            refused.append((entry, str(exc)))
            continue
        reports.append(report)
        if not dry_run:
            conn.commit()
    return reports, refused
