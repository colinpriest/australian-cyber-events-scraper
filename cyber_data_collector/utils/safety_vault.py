"""Safety vault: snapshot user-applied data corrections before destructive
operations (e.g. force-dedup), and restore them automatically afterwards.

Today's pain point: ``run_global_deduplication.py --force`` rebuilds
``DeduplicatedEvents`` from scratch. That cascade-deletes the ASDRiskClassifications
rows AND wipes any industry corrections that exist only on
``DeduplicatedEvents.victim_organization_industry``. Without a vault those
are simply gone.

The vault snapshots BOTH classes of correction keyed by
``master_enriched_event_id`` (the only ID that survives a dedup rebuild)
and restores them after the rebuild completes.

Usage::

    vault = SafetyVault('instance/cyber_events.db')
    vault.snapshot()                # before destructive op
    # ... rebuild DeduplicatedEvents ...
    n_asd, n_ind = vault.restore()  # after destructive op
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class SafetyVault:
    """Snapshot/restore ASD classifications + industry corrections.

    The two things that get wiped on a force-dedup are:

    1. ASDRiskClassifications - cascade-deleted by FK when DeduplicatedEvents rows
       are dropped and re-inserted with new IDs.
    2. Manual ``victim_organization_industry`` corrections - re-derived from
       enrichment data on rebuild, which may overwrite session-level fixes.

    The vault stores both in JSON files keyed by ``master_enriched_event_id``,
    which is stable across dedup rebuilds.
    """

    DEFAULT_VAULT_DIR = Path("instance/safety_vault")

    # OAIC-style sector severity ordering for tie-breaking when several old
    # records merge into one new record - we keep the most severe (lowest C-N).
    _SEVERITY_ORDER = {"C1": 1, "C2": 2, "C3": 3, "C4": 4, "C5": 5, "C6": 6}

    def __init__(self, db_path: str, vault_dir: Optional[Path] = None):
        self.db_path = db_path
        self.vault_dir = Path(vault_dir) if vault_dir else self.DEFAULT_VAULT_DIR
        self.vault_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ snapshot

    def snapshot(self, label: str = "auto") -> Dict[str, int]:
        """Snapshot the current ASD classifications + industry overrides to JSON.

        Returns a dict with row counts for each snapshot kind.
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = f"{ts}_{label}"
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            asd_path = self.vault_dir / f"{prefix}_asd_classifications.json"
            ind_path = self.vault_dir / f"{prefix}_industry_overrides.json"

            asd_rows = self._snapshot_asd(conn, asd_path)
            ind_rows = self._snapshot_industries(conn, ind_path)

            # Always rewrite the "latest_*.json" symlink-like pointer so
            # auto-restore knows which snapshot to use.
            self._write_latest_pointer("asd_classifications", asd_path)
            self._write_latest_pointer("industry_overrides", ind_path)

            logger.info(
                "SafetyVault: snapshot saved to %s "
                "(asd=%d, industry_overrides=%d)",
                self.vault_dir, asd_rows, ind_rows,
            )
            return {"asd": asd_rows, "industry_overrides": ind_rows}
        finally:
            conn.close()

    def _snapshot_asd(self, conn: sqlite3.Connection, path: Path) -> int:
        rows = list(conn.execute(
            """
            SELECT arc.classification_id, arc.severity_category,
                   arc.primary_stakeholder_category, arc.impact_type,
                   arc.reasoning_json, arc.confidence_score, arc.model_used,
                   arc.created_at, arc.updated_at,
                   de.master_enriched_event_id
            FROM ASDRiskClassifications arc
            JOIN DeduplicatedEvents de
              ON arc.deduplicated_event_id = de.deduplicated_event_id
            """
        ))
        records = [dict(r) for r in rows]
        path.write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
        return len(records)

    def _snapshot_industries(self, conn: sqlite3.Connection, path: Path) -> int:
        """Snapshot industry assignments keyed by master_enriched_event_id.

        We snapshot every Active row (not just "non-default" ones) because we
        cannot reliably tell which were corrected. The restore step skips
        any value that already matches what the rebuild produced.
        """
        rows = list(conn.execute(
            """
            SELECT master_enriched_event_id,
                   victim_organization_name,
                   victim_organization_industry
            FROM DeduplicatedEvents
            WHERE status='Active' AND victim_organization_industry IS NOT NULL
            """
        ))
        records = [dict(r) for r in rows]
        path.write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
        return len(records)

    def _write_latest_pointer(self, kind: str, path: Path) -> None:
        pointer = self.vault_dir / f"latest_{kind}.json"
        # Plain JSON pointer; Windows symlinks need admin so we keep this portable
        pointer.write_text(json.dumps({"path": str(path)}, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------- restore

    def restore(self) -> Tuple[int, int]:
        """Restore ASD classifications + industry overrides into the current DB.

        Uses the most recent snapshot. Skips entries that no longer have a
        matching ``master_enriched_event_id`` in DeduplicatedEvents.

        Returns (n_asd_restored, n_industry_restored).
        """
        asd_pointer = self.vault_dir / "latest_asd_classifications.json"
        ind_pointer = self.vault_dir / "latest_industry_overrides.json"
        asd_path = self._resolve_pointer(asd_pointer)
        ind_path = self._resolve_pointer(ind_pointer)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            new_master_to_dedup = {
                r["master_enriched_event_id"]: r["deduplicated_event_id"]
                for r in conn.execute(
                    "SELECT deduplicated_event_id, master_enriched_event_id "
                    "FROM DeduplicatedEvents WHERE status='Active'"
                )
            }

            n_asd = 0
            if asd_path:
                n_asd = self._restore_asd(conn, asd_path, new_master_to_dedup)

            n_ind = 0
            if ind_path:
                n_ind = self._restore_industries(conn, ind_path, new_master_to_dedup)

            conn.commit()
            logger.info("SafetyVault: restored asd=%d, industry_overrides=%d",
                        n_asd, n_ind)
            return (n_asd, n_ind)
        finally:
            conn.close()

    def _resolve_pointer(self, pointer: Path) -> Optional[Path]:
        if not pointer.exists():
            logger.warning("SafetyVault: no snapshot pointer at %s", pointer)
            return None
        try:
            target = json.loads(pointer.read_text(encoding="utf-8")).get("path")
        except Exception as e:
            logger.error("SafetyVault: pointer %s unreadable: %s", pointer, e)
            return None
        p = Path(target) if target else None
        if not p or not p.exists():
            logger.warning("SafetyVault: pointed-to snapshot %s is missing", p)
            return None
        return p

    def _restore_asd(
        self, conn: sqlite3.Connection, path: Path,
        master_to_dedup: Dict[str, str]
    ) -> int:
        records: List[dict] = json.loads(path.read_text(encoding="utf-8"))

        # Sort most-severe first so on collision (multiple old records map to
        # one new dedup event) we keep the worst severity.
        records.sort(key=lambda r: self._SEVERITY_ORDER.get(
            r.get("severity_category") or "C6", 9
        ))

        cols = [r["name"] for r in conn.execute(
            "PRAGMA table_info(ASDRiskClassifications)"
        )]

        seen_dedup_ids = set()
        n_inserted = 0
        for r in records:
            new_id = master_to_dedup.get(r.get("master_enriched_event_id"))
            if not new_id or new_id in seen_dedup_ids:
                continue
            seen_dedup_ids.add(new_id)
            payload = {c: r.get(c) for c in cols}
            payload["deduplicated_event_id"] = new_id
            placeholders = ",".join("?" * len(cols))
            conn.execute(
                f"INSERT OR REPLACE INTO ASDRiskClassifications "
                f"({','.join(cols)}) VALUES ({placeholders})",
                [payload[c] for c in cols],
            )
            n_inserted += 1
        return n_inserted

    def _restore_industries(
        self, conn: sqlite3.Connection, path: Path,
        master_to_dedup: Dict[str, str]
    ) -> int:
        records: List[dict] = json.loads(path.read_text(encoding="utf-8"))
        n_updated = 0
        for r in records:
            new_id = master_to_dedup.get(r.get("master_enriched_event_id"))
            if not new_id:
                continue
            saved_industry = r.get("victim_organization_industry")
            if not saved_industry:
                continue
            cur = conn.execute(
                "UPDATE DeduplicatedEvents SET victim_organization_industry = ? "
                "WHERE deduplicated_event_id = ? "
                "AND (victim_organization_industry IS NULL "
                "     OR victim_organization_industry != ?)",
                (saved_industry, new_id, saved_industry),
            )
            if cur.rowcount:
                n_updated += 1
        return n_updated
