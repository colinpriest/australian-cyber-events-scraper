"""Append-only decision ledger: audit, reversibility, overrides and learning.

Why an append-only ledger rather than mutating ``DeduplicatedEvents``:

The previous design regenerated every ``deduplicated_event_id`` on each
rebuild, so anything keyed on those ids - provenance, source links, human
corrections - was silently destroyed. That is how the database ended up with
``DeduplicatedEventSources`` empty and ``total_data_sources`` zero on all 1,034
rows. Here, every decision is written once and never updated; a change is a new
row that supersedes the old one. Reverting is therefore a *write*, not a
recomputation, and needs no pipeline rerun.

Human overrides are keyed on ``enriched_event_id`` pairs, which are stable
across rebuilds, so corrections survive a full re-deduplication.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from cyber_data_collector.dedup.entity_resolution import EntityResolver
from cyber_data_collector.dedup.title_selection import derive_title
from cyber_data_collector.dedup.models import (
    DecidedBy,
    DedupAction,
    DedupDecision,
    MatchEvidence,
    OverrideVerdict,
    PairVerdict,
)

logger = logging.getLogger(__name__)


def pair_key(left_id: str, right_id: str) -> str:
    """Order-independent key for a pair of enriched events."""
    return "|".join(sorted((str(left_id), str(right_id))))


class DedupLedger:
    """Reads and writes the deduplication decision history."""

    def __init__(self, conn: sqlite3.Connection, role_refresher=None):
        """
        Args:
            conn: Open database connection.
            role_refresher: Optional callable invoked with a
                ``deduplicated_event_id`` whenever that event's membership
                changes. Entity roles are decided against a specific set of
                records, so a merge or split leaves them describing a set the
                event no longer has; this is what makes refreshing automatic
                instead of a follow-up command someone has to remember. See
                :class:`~cyber_data_collector.dedup.role_maintenance.DeferredRoleRefresher`.
        """
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.role_refresher = role_refresher

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------

    def record_decision(self, decision: DedupDecision) -> str:
        row = decision.to_row()
        self.conn.execute(
            """
            INSERT INTO DedupDecisions (
                decision_id, batch_id, enriched_event_id, deduplicated_event_id,
                cluster_key, action, decided_by, certainty, method, reasoning,
                evidence_json, superseded_by, created_at
            ) VALUES (
                :decision_id, :batch_id, :enriched_event_id, :deduplicated_event_id,
                :cluster_key, :action, :decided_by, :certainty, :method, :reasoning,
                :evidence_json, :superseded_by, :created_at
            )
            """,
            row,
        )
        return decision.decision_id

    def record_pair_verdict(
        self,
        batch_id: str,
        left_id: str,
        right_id: str,
        verdict: PairVerdict,
        deduplicated_event_id: Optional[str] = None,
        method: Optional[str] = None,
    ) -> str:
        """Persist a pair verdict as a decision on the right-hand member."""
        decision = DedupDecision(
            decision_id=str(uuid.uuid4()),
            batch_id=batch_id,
            enriched_event_id=right_id,
            deduplicated_event_id=deduplicated_event_id,
            cluster_key=pair_key(left_id, right_id),
            action=DedupAction.MERGE if verdict.is_same_event else DedupAction.KEEP_SEPARATE,
            decided_by=verdict.decided_by,
            certainty=verdict.certainty,
            method=method or f"{verdict.decided_by.value}-adjudication",
            reasoning=verdict.reasoning,
            evidence=verdict.evidence,
        )
        return self.record_decision(decision)

    def live_decisions_for(self, deduplicated_event_id: str) -> List[sqlite3.Row]:
        """Decisions that currently apply to a dedup event (not superseded)."""
        return self.conn.execute(
            """
            SELECT * FROM DedupDecisions
            WHERE deduplicated_event_id = ? AND superseded_by IS NULL
            ORDER BY created_at
            """,
            (deduplicated_event_id,),
        ).fetchall()

    def history_for_event(self, enriched_event_id: str) -> List[sqlite3.Row]:
        """Full decision history for one enriched event, oldest first."""
        return self.conn.execute(
            "SELECT * FROM DedupDecisions WHERE enriched_event_id = ? ORDER BY created_at",
            (enriched_event_id,),
        ).fetchall()

    # ------------------------------------------------------------------
    # Snapshots + reversibility
    # ------------------------------------------------------------------

    def snapshot_event(self, batch_id: str, deduplicated_event_id: str) -> Optional[str]:
        """Capture a dedup row and its members so the state can be restored."""
        row = self.conn.execute(
            "SELECT * FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
            (deduplicated_event_id,),
        ).fetchone()
        if row is None:
            return None

        members = [
            dict(m) for m in self.conn.execute(
                "SELECT * FROM EventDeduplicationMap WHERE deduplicated_event_id = ?",
                (deduplicated_event_id,),
            )
        ]
        payload = {
            "event": {k: _jsonable(row[k]) for k in row.keys()},
            "members": [{k: _jsonable(v) for k, v in m.items()} for m in members],
        }
        snapshot_id = str(uuid.uuid4())
        self.conn.execute(
            """
            INSERT INTO DedupSnapshots (snapshot_id, batch_id, deduplicated_event_id,
                                        payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (snapshot_id, batch_id, deduplicated_event_id,
             json.dumps(payload, ensure_ascii=False)),
        )
        return snapshot_id

    def split_member(
        self,
        deduplicated_event_id: str,
        enriched_event_id: str,
        reason: str,
        actor: str = "human",
    ) -> str:
        """Detach one member from a merged event, creating its own dedup row.

        This is the reverse of a merge and runs entirely against stored state -
        no re-scraping, no re-enrichment, no full pipeline run.
        """
        batch_id = f"revert-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        self.snapshot_event(batch_id, deduplicated_event_id)

        member = self.conn.execute(
            """
            SELECT m.*, e.title, e.summary, e.event_date, e.event_type, e.severity,
                   e.records_affected
            FROM EventDeduplicationMap m
            JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
            WHERE m.deduplicated_event_id = ? AND m.enriched_event_id = ?
            """,
            (deduplicated_event_id, enriched_event_id),
        ).fetchone()
        if member is None:
            raise ValueError(
                f"{enriched_event_id} is not a member of {deduplicated_event_id}"
            )

        # The group's own event_date is often the DEPARTING member's date: v2
        # merges take the earliest date across members, so removing that member
        # leaves the group asserting a date no record of its own supports. It
        # also makes the split impossible - the new row would carry the same
        # (title, event_date) as the group it just left, colliding with the
        # partial unique index idx_dedup_unique_event.
        #
        # Recomputing the group from what actually remains fixes both: it is
        # required for correctness regardless, and it frees the collision.
        self._recompute_identity(deduplicated_event_id, exclude=enriched_event_id)

        # A split member frequently duplicates an event that already exists
        # (DeduplicatedEvents has UNIQUE(title, event_date)). Creating a new row
        # then fails outright, leaving the member stranded in the wrong group.
        # Attaching it to the existing event is both legal and semantically
        # correct - that IS where the record belongs.
        existing = self.conn.execute(
            """
            SELECT deduplicated_event_id FROM DeduplicatedEvents
            WHERE title = ? AND COALESCE(event_date,'') = COALESCE(?,'')
              AND deduplicated_event_id != ?
              AND COALESCE(status,'Active') = 'Active'
            LIMIT 1
            """,
            (member["title"], member["event_date"], deduplicated_event_id),
        ).fetchone()
        if existing is not None:
            target = existing["deduplicated_event_id"]
            self.conn.execute(
                """
                UPDATE EventDeduplicationMap
                SET deduplicated_event_id = ?, contribution_type = 'merged'
                WHERE deduplicated_event_id = ? AND enriched_event_id = ?
                """,
                (target, deduplicated_event_id, enriched_event_id),
            )
            self._supersede_decisions(deduplicated_event_id, enriched_event_id)
            self.record_decision(DedupDecision(
                decision_id=str(uuid.uuid4()),
                batch_id=batch_id,
                enriched_event_id=enriched_event_id,
                deduplicated_event_id=target,
                action=DedupAction.REVERT,
                decided_by=DecidedBy.HUMAN if actor == "human" else DecidedBy.RULE,
                certainty=1.0,
                method="manual-split-reattach",
                reasoning=f"{reason} (reattached to existing event with the "
                          f"same title and date)",
                evidence=MatchEvidence(),
            ))
            self._refresh_counts(deduplicated_event_id)
            self._refresh_counts(target)
            self._roles_changed(deduplicated_event_id, target)
            logger.info(
                "Split %s out of %s and reattached to existing %s",
                enriched_event_id, deduplicated_event_id, target,
            )
            return target

        # Splitting out the record that *defines* the event is contradictory:
        # its id is the event's master_enriched_event_id, which is the identity
        # key. Promote another member to master first, so the event keeps a
        # valid identity and the departing record is free to take its own.
        owner = self.conn.execute(
            "SELECT master_enriched_event_id AS m FROM DeduplicatedEvents "
            "WHERE deduplicated_event_id = ?", (deduplicated_event_id,)
        ).fetchone()
        if owner is not None and owner["m"] == enriched_event_id:
            replacement = self.conn.execute(
                "SELECT enriched_event_id FROM EventDeduplicationMap "
                "WHERE deduplicated_event_id = ? AND enriched_event_id != ? LIMIT 1",
                (deduplicated_event_id, enriched_event_id),
            ).fetchone()
            if replacement is None:
                raise ValueError(
                    f"{enriched_event_id} is the only member of "
                    f"{deduplicated_event_id}; splitting it would leave an "
                    "event with no records"
                )
            self.conn.execute(
                "UPDATE DeduplicatedEvents SET master_enriched_event_id = ? "
                "WHERE deduplicated_event_id = ?",
                (replacement["enriched_event_id"], deduplicated_event_id),
            )
            self.conn.execute(
                "UPDATE EventDeduplicationMap SET contribution_type = 'master' "
                "WHERE deduplicated_event_id = ? AND enriched_event_id = ?",
                (deduplicated_event_id, replacement["enriched_event_id"]),
            )
            logger.info("Promoted %s to master of %s before splitting %s",
                        replacement["enriched_event_id"], deduplicated_event_id,
                        enriched_event_id)

        # The split creates a row whose master is this member. If an active row
        # already claims that master, the immutable identity index rejects the
        # insert - so attach the member there instead. That is also the correct
        # outcome: the record already has an event of its own.
        existing_master = self.conn.execute(
            "SELECT deduplicated_event_id FROM DeduplicatedEvents "
            "WHERE master_enriched_event_id = ? AND COALESCE(status,'Active')='Active' "
            "AND deduplicated_event_id != ? LIMIT 1",
            (enriched_event_id, deduplicated_event_id),
        ).fetchone()
        if existing_master is not None:
            target = existing_master["deduplicated_event_id"]
            self.conn.execute(
                """
                UPDATE EventDeduplicationMap
                SET deduplicated_event_id = ?, contribution_type = 'master'
                WHERE deduplicated_event_id = ? AND enriched_event_id = ?
                """,
                (target, deduplicated_event_id, enriched_event_id),
            )
            self._supersede_decisions(deduplicated_event_id, enriched_event_id)
            self.record_decision(DedupDecision(
                decision_id=str(uuid.uuid4()),
                batch_id=batch_id,
                enriched_event_id=enriched_event_id,
                deduplicated_event_id=target,
                action=DedupAction.REVERT,
                decided_by=DecidedBy.HUMAN if actor == "human" else DecidedBy.RULE,
                certainty=1.0,
                method="manual-split-existing-master",
                reasoning=f"{reason} (returned to its own existing event)",
                evidence=MatchEvidence(),
            ))
            self._refresh_counts(deduplicated_event_id)
            self._refresh_counts(target)
            self._roles_changed(deduplicated_event_id, target)
            logger.info("Split %s out of %s back to its own event %s",
                        enriched_event_id, deduplicated_event_id, target)
            return target

        source_victim = self.conn.execute(
            "SELECT victim_organization_name, victim_organization_industry "
            "FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
            (deduplicated_event_id,),
        ).fetchone()

        new_dedup_id = str(uuid.uuid4())
        self.conn.execute(
            """
            INSERT INTO DeduplicatedEvents (
                deduplicated_event_id, master_enriched_event_id, title, summary,
                event_date, event_type, severity, records_affected,
                is_australian_event, is_specific_event, confidence_score,
                status, created_at, updated_at, total_data_sources,
                dedup_method, dedup_batch_id, has_human_override,
                victim_organization_name, victim_organization_industry
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, 'Active', ?, ?, 1,
                      'manual-split', ?, 1, ?, ?)
            """,
            (
                new_dedup_id, enriched_event_id, member["title"], member["summary"],
                member["event_date"], member["event_type"], member["severity"],
                member["records_affected"], 0.5,
                datetime.now(), datetime.now(), batch_id,
                # Carry the organisation across. Without this a split-out event
                # has a NULL victim, which breaks sector reporting and makes
                # same-organisation checks silently match nothing.
                source_victim["victim_organization_name"] if source_victim else None,
                source_victim["victim_organization_industry"] if source_victim else None,
            ),
        )
        self.conn.execute(
            """
            UPDATE EventDeduplicationMap
            SET deduplicated_event_id = ?, contribution_type = 'master'
            WHERE deduplicated_event_id = ? AND enriched_event_id = ?
            """,
            (new_dedup_id, deduplicated_event_id, enriched_event_id),
        )
        self._supersede_decisions(deduplicated_event_id, enriched_event_id)

        self.record_decision(DedupDecision(
            decision_id=str(uuid.uuid4()),
            batch_id=batch_id,
            enriched_event_id=enriched_event_id,
            deduplicated_event_id=new_dedup_id,
            action=DedupAction.REVERT,
            decided_by=DecidedBy.HUMAN if actor == "human" else DecidedBy.RULE,
            certainty=1.0,
            method="manual-split",
            reasoning=reason,
            evidence=MatchEvidence(),
        ))
        self._refresh_counts(deduplicated_event_id)
        self._refresh_counts(new_dedup_id)
        # Both sides changed: one lost a record, the other is brand new.
        self._roles_changed(deduplicated_event_id, new_dedup_id)
        logger.info(
            "Split %s out of %s into %s", enriched_event_id,
            deduplicated_event_id, new_dedup_id,
        )
        return new_dedup_id

    def merge_events(
        self,
        target_dedup_id: str,
        source_dedup_id: str,
        reason: str,
        actor: str = "human",
    ) -> None:
        """Fold ``source_dedup_id``'s members into ``target_dedup_id``.

        Used to repair the under-merging case (one incident stored as several
        dedup events) without a rebuild.
        """
        if target_dedup_id == source_dedup_id:
            raise ValueError("cannot merge an event into itself")
        batch_id = f"merge-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        self.snapshot_event(batch_id, target_dedup_id)
        self.snapshot_event(batch_id, source_dedup_id)

        members = self.conn.execute(
            "SELECT enriched_event_id FROM EventDeduplicationMap WHERE deduplicated_event_id = ?",
            (source_dedup_id,),
        ).fetchall()

        self.conn.execute(
            """
            UPDATE EventDeduplicationMap
            SET deduplicated_event_id = ?, contribution_type = 'merged'
            WHERE deduplicated_event_id = ?
            """,
            (target_dedup_id, source_dedup_id),
        )
        self.conn.execute(
            "UPDATE DeduplicatedEvents SET status = 'Merged', updated_at = ? "
            "WHERE deduplicated_event_id = ?",
            (datetime.now(), source_dedup_id),
        )

        for member in members:
            self.record_decision(DedupDecision(
                decision_id=str(uuid.uuid4()),
                batch_id=batch_id,
                enriched_event_id=member["enriched_event_id"],
                deduplicated_event_id=target_dedup_id,
                action=DedupAction.MERGE,
                decided_by=DecidedBy.HUMAN if actor == "human" else DecidedBy.RULE,
                certainty=1.0,
                method="manual-merge",
                reasoning=reason,
                evidence=MatchEvidence(),
            ))
        self.conn.execute(
            "UPDATE DeduplicatedEvents SET has_human_override = 1 "
            "WHERE deduplicated_event_id = ?",
            (target_dedup_id,),
        )
        # The target now covers more records, so its title may no longer be the
        # best description of the incident. Safe to rewrite: identity keys on
        # master_enriched_event_id, not on the title.
        self._recompute_identity(target_dedup_id)
        self._refresh_counts(target_dedup_id)
        # The target now covers a different set of records, so the roles
        # decided for the old set no longer apply.
        self._roles_changed(target_dedup_id)
        logger.info("Merged %s into %s", source_dedup_id, target_dedup_id)

    def restore_snapshot(self, snapshot_id: str) -> None:
        """Restore a dedup event and its membership from a snapshot."""
        row = self.conn.execute(
            "SELECT payload_json, deduplicated_event_id FROM DedupSnapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown snapshot {snapshot_id}")

        payload = json.loads(row["payload_json"])
        event = payload["event"]
        dedup_id = row["deduplicated_event_id"]

        columns = [c for c in event if c != "deduplicated_event_id"]
        assignments = ", ".join(f"{c} = ?" for c in columns)
        self.conn.execute(
            f"UPDATE DeduplicatedEvents SET {assignments} WHERE deduplicated_event_id = ?",
            [event[c] for c in columns] + [dedup_id],
        )
        self.conn.execute(
            "DELETE FROM EventDeduplicationMap WHERE deduplicated_event_id = ?",
            (dedup_id,),
        )
        for member in payload["members"]:
            cols = ", ".join(member.keys())
            marks = ", ".join("?" for _ in member)
            self.conn.execute(
                f"INSERT OR REPLACE INTO EventDeduplicationMap ({cols}) VALUES ({marks})",
                list(member.values()),
            )
        self._refresh_counts(dedup_id)
        logger.info("Restored snapshot %s for %s", snapshot_id, dedup_id)

    def _supersede_decisions(self, dedup_id: str, enriched_id: str) -> None:
        marker = str(uuid.uuid4())
        self.conn.execute(
            """
            UPDATE DedupDecisions SET superseded_by = ?
            WHERE deduplicated_event_id = ? AND enriched_event_id = ?
              AND superseded_by IS NULL
            """,
            (marker, dedup_id, enriched_id),
        )

    def _recompute_identity(self, dedup_id: str, exclude: Optional[str] = None) -> None:
        """Re-derive a group's title and event_date from its remaining members.

        Applies the project's documented "earliest date wins" rule to the
        members that are actually left, and takes the title from the master
        event. Without this, a group keeps identity fields inherited from a
        record that is no longer part of it.
        """
        remaining = self.conn.execute(
            """
            SELECT e.enriched_event_id, e.title, e.event_date
            FROM EventDeduplicationMap m
            JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
            WHERE m.deduplicated_event_id = ?
              AND (? IS NULL OR m.enriched_event_id != ?)
            """,
            (dedup_id, exclude, exclude),
        ).fetchall()
        if not remaining:
            return

        master_id = self.conn.execute(
            "SELECT master_enriched_event_id FROM DeduplicatedEvents "
            "WHERE deduplicated_event_id = ?",
            (dedup_id,),
        ).fetchone()
        master_id = master_id["master_enriched_event_id"] if master_id else None

        # Derive the title rather than copying the master's. Master selection
        # follows dedup mechanics, not how well a headline describes the
        # incident, which is why merged events ended up called "Scattered
        # Spider Ransomware Attacks" or plain "Untitled Event".
        current = self.conn.execute(
            "SELECT title, victim_organization_name FROM DeduplicatedEvents "
            "WHERE deduplicated_event_id = ?",
            (dedup_id,),
        ).fetchone()
        title = derive_title(
            [r["title"] for r in remaining],
            victim=current["victim_organization_name"] if current else None,
            incident_label=self.incident_label_for(dedup_id),
            current_title=current["title"] if current else None,
        )
        dates = sorted(str(r["event_date"]) for r in remaining if r["event_date"])
        earliest = dates[0] if dates else None

        self.conn.execute(
            "UPDATE DeduplicatedEvents SET title = ?, event_date = ?, updated_at = ? "
            "WHERE deduplicated_event_id = ?",
            (title, earliest, datetime.now(), dedup_id),
        )

    def incident_label_for(self, dedup_id: str) -> Optional[str]:
        """Recover the incident label produced by cluster adjudication.

        Cluster decisions are recorded as "[cluster 0.95] <label>: <reasoning>",
        so the label the model chose for the incident - "HWL Ebsworth
        ransomware breach (April 2023)" - can be read back and reused as a
        title without another LLM call.
        """
        row = self.conn.execute(
            """
            SELECT reasoning FROM DedupDecisions
            WHERE deduplicated_event_id = ? AND reasoning LIKE '[cluster%'
            ORDER BY created_at DESC LIMIT 1
            """,
            (dedup_id,),
        ).fetchone()
        if not row or not row["reasoning"]:
            return None
        match = re.match(r"\[cluster [\d.]+\]\s*(.+?):\s", row["reasoning"])
        return match.group(1).strip() if match else None

    def _roles_changed(self, *deduplicated_event_ids: Optional[str]) -> None:
        """Tell the refresher that these events' membership changed."""
        if not self.role_refresher:
            return
        for dedup_id in deduplicated_event_ids:
            if dedup_id:
                try:
                    self.role_refresher(dedup_id)
                except Exception as exc:  # noqa: BLE001 - never break a merge
                    logger.warning("Could not schedule role refresh for %s: %s",
                                   dedup_id, exc)

    def _refresh_counts(self, dedup_id: str) -> None:
        """Recompute total_data_sources from actual membership."""
        count = self.conn.execute(
            "SELECT COUNT(DISTINCT enriched_event_id) FROM EventDeduplicationMap "
            "WHERE deduplicated_event_id = ?",
            (dedup_id,),
        ).fetchone()[0]
        self.conn.execute(
            "UPDATE DeduplicatedEvents SET total_data_sources = ?, updated_at = ? "
            "WHERE deduplicated_event_id = ?",
            (max(count, 0), datetime.now(), dedup_id),
        )

    # ------------------------------------------------------------------
    # Overrides + learning
    # ------------------------------------------------------------------

    def add_override(
        self,
        left_enriched_id: str,
        right_enriched_id: str,
        verdict: OverrideVerdict,
        reason: str = "",
        created_by: str = "human",
    ) -> str:
        """Record a human ruling on a pair. Survives full rebuilds."""
        override_id = str(uuid.uuid4())
        self.conn.execute(
            """
            INSERT INTO DedupOverrides (override_id, pair_key,
                left_enriched_event_id, right_enriched_event_id, verdict,
                reason, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pair_key) DO UPDATE SET
                verdict = excluded.verdict,
                reason = excluded.reason,
                created_by = excluded.created_by,
                created_at = CURRENT_TIMESTAMP,
                active = 1
            """,
            (override_id, pair_key(left_enriched_id, right_enriched_id),
             left_enriched_id, right_enriched_id, verdict.value, reason, created_by),
        )
        logger.info(
            "Override recorded: %s vs %s -> %s",
            left_enriched_id, right_enriched_id, verdict.value,
        )
        return override_id

    def load_overrides(self) -> Dict[frozenset, bool]:
        """Active overrides as ``{frozenset(pair): is_same_event}``."""
        overrides: Dict[frozenset, bool] = {}
        try:
            rows = self.conn.execute(
                "SELECT left_enriched_event_id, right_enriched_event_id, verdict "
                "FROM DedupOverrides WHERE active = 1"
            ).fetchall()
        except sqlite3.Error:
            return overrides
        for row in rows:
            key = frozenset((row["left_enriched_event_id"], row["right_enriched_event_id"]))
            overrides[key] = row["verdict"] == OverrideVerdict.SAME.value
        return overrides

    def learn_from_overrides(self, resolver: Optional[EntityResolver] = None) -> Dict[str, Any]:
        """Fold human corrections back into the pipeline's defaults.

        Two concrete forms of learning, both inspectable rather than opaque:

        1. **Entity aliases.** When a reviewer says two records with *different*
           victim names are the same incident, the names are bound together in
           ``EntityAliases`` so future runs block them into the same candidate
           bucket automatically. This is what stops the same variant-name miss
           recurring.
        2. **Threshold calibration.** Overrides are graded against what the
           pipeline decided, and the auto-merge threshold is nudged toward
           whichever error the human is correcting more often. Recorded in
           ``DedupCalibration`` with its sample size so the adjustment can be
           audited or rolled back.
        """
        resolver = resolver or EntityResolver(self.conn)
        # EnrichedEvents carries no victim column: the affected organisation is
        # reachable either through EnrichedEventEntities -> EntitiesV2, or off
        # the DeduplicatedEvents row the event currently belongs to. Try both,
        # preferring the entity link.
        rows = self.conn.execute(
            """
            SELECT o.*,
                   l.title AS left_title,
                   r.title AS right_title,
                   COALESCE(
                     (SELECT v.entity_name FROM EnrichedEventEntities ee
                        JOIN EntitiesV2 v ON v.entity_id = ee.entity_id
                       WHERE ee.enriched_event_id = o.left_enriched_event_id
                       ORDER BY ee.confidence_score DESC LIMIT 1),
                     (SELECT d.victim_organization_name FROM DeduplicatedEvents d
                       WHERE d.master_enriched_event_id = o.left_enriched_event_id
                       LIMIT 1)
                   ) AS left_entity,
                   COALESCE(
                     (SELECT v.entity_name FROM EnrichedEventEntities ee
                        JOIN EntitiesV2 v ON v.entity_id = ee.entity_id
                       WHERE ee.enriched_event_id = o.right_enriched_event_id
                       ORDER BY ee.confidence_score DESC LIMIT 1),
                     (SELECT d.victim_organization_name FROM DeduplicatedEvents d
                       WHERE d.master_enriched_event_id = o.right_enriched_event_id
                       LIMIT 1)
                   ) AS right_entity
            FROM DedupOverrides o
            LEFT JOIN EnrichedEvents l ON l.enriched_event_id = o.left_enriched_event_id
            LEFT JOIN EnrichedEvents r ON r.enriched_event_id = o.right_enriched_event_id
            WHERE o.active = 1
            """
        ).fetchall()

        aliases_learned = 0
        false_merges = 0   # human said different, pipeline merged -> too loose
        missed_merges = 0  # human said same, pipeline split      -> too tight

        for row in rows:
            same = row["verdict"] == OverrideVerdict.SAME.value
            left_entity, right_entity = row["left_entity"], row["right_entity"]

            if same:
                # Count every "should have merged" ruling, whether or not the
                # entity names resolved - the calibration signal is the human's
                # verdict, not our ability to name the organisation.
                missed_merges += 1
                if (left_entity and right_entity
                        and left_entity != right_entity
                        and not resolver.are_candidates(left_entity, right_entity)):
                    # Blocking would never have paired these - teach it.
                    # Choose the target by sort order, not by length: two names
                    # of equal length made max() and min() return the SAME
                    # string, which aliased a name to itself and silently
                    # taught the resolver nothing.
                    canonical, other = sorted((left_entity, right_entity))
                    resolver.learn_alias(self.conn, other, canonical, source="learned")
                    aliases_learned += 1
            else:
                false_merges += 1

        sample = len(rows)
        threshold = self._current_threshold()
        if sample >= 5:
            # Move the threshold toward the error the human is correcting more.
            if false_merges > missed_merges:
                threshold = min(0.97, threshold + 0.02)
            elif missed_merges > false_merges:
                threshold = max(0.60, threshold - 0.02)

            self.conn.execute(
                """
                INSERT INTO DedupCalibration (calibration_id, sample_size,
                    false_merges, missed_merges, merge_threshold, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), sample, false_merges, missed_merges, threshold,
                 "auto-calibrated from human overrides"),
            )

        result = {
            "overrides_considered": sample,
            "aliases_learned": aliases_learned,
            "false_merges": false_merges,
            "missed_merges": missed_merges,
            "merge_threshold": threshold,
        }
        logger.info("Learned from overrides: %s", result)
        return result

    def _current_threshold(self, default: float = 0.85) -> float:
        try:
            row = self.conn.execute(
                "SELECT merge_threshold FROM DedupCalibration "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            return default
        return float(row["merge_threshold"]) if row else default

    # ------------------------------------------------------------------
    # Ancestry (for the dashboard)
    # ------------------------------------------------------------------

    def ancestry(self, deduplicated_event_id: str) -> Dict[str, Any]:
        """Full provenance tree for one deduplicated event."""
        event = self.conn.execute(
            "SELECT * FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
            (deduplicated_event_id,),
        ).fetchone()
        if event is None:
            return {}

        members = self.conn.execute(
            """
            SELECT m.enriched_event_id, m.raw_event_id, m.contribution_type,
                   m.similarity_score, e.title, e.event_date,
                   (SELECT v.entity_name FROM EnrichedEventEntities ee
                      JOIN EntitiesV2 v ON v.entity_id = ee.entity_id
                     WHERE ee.enriched_event_id = m.enriched_event_id
                     ORDER BY ee.confidence_score DESC LIMIT 1) AS entity,
                   r.source_url, r.source_type
            FROM EventDeduplicationMap m
            LEFT JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
            LEFT JOIN RawEvents r ON r.raw_event_id = m.raw_event_id
            WHERE m.deduplicated_event_id = ?
            ORDER BY m.contribution_type DESC, e.event_date
            """,
            (deduplicated_event_id,),
        ).fetchall()

        decisions = self.conn.execute(
            """
            SELECT decision_id, enriched_event_id, action, decided_by, certainty,
                   method, reasoning, evidence_json, superseded_by, created_at
            FROM DedupDecisions
            WHERE deduplicated_event_id = ?
            ORDER BY created_at
            """,
            (deduplicated_event_id,),
        ).fetchall()

        return {
            "event": {k: _jsonable(event[k]) for k in event.keys()},
            "members": [{k: _jsonable(m[k]) for k in m.keys()} for m in members],
            "decisions": [{k: _jsonable(d[k]) for k in d.keys()} for d in decisions],
        }


def _jsonable(value: Any) -> Any:
    """Coerce sqlite values into something json.dumps can handle."""
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return value
