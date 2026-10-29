"""Administration CLI for deduplication v3.

Subcommands:
    migrate        Apply the v3 schema (idempotent)
    backfill       Repair lineage/provenance on existing rows
    find-missed    Detect deduplicated events that are really one incident
    apply-missed   Merge the confident findings from find-missed
    check-recurrences  Re-check repeat attacks less than 90 days apart
    size-entities  Estimate each entity's ordinal size band, researched online
    split          Detach one member from a merged event
    override       Record a human ruling on a pair
    learn          Fold human overrides back into aliases/thresholds
    ancestry       Print the provenance tree for one event

Run from the project root:
    python scripts/dedup_admin.py find-missed --limit 200
    python scripts/dedup_admin.py apply-missed --min-certainty 0.9
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Never let a character kill a run. On Windows a redirected stdout defaults to
# cp1252, and text that came back from a web search routinely carries something
# outside it - a non-breaking hyphen in one organisation's LinkedIn blurb ended
# an hour-long backfill with UnicodeEncodeError, discarding every lookup it had
# paid for. Reporting is never worth failing a data command over.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - non-standard stream
        pass

# Load the project's .env explicitly. Bare load_dotenv() resolves relative
# to the *calling* file, and only OPENAI_API_KEY happened to be set in the
# shell environment as well - so Perplexity silently had no key and every
# search-backed lookup returned nothing rather than failing loudly.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # pragma: no cover - optional dependency
    pass

from cyber_data_collector.dedup import repair, schema
from cyber_data_collector.utils.llm_models import (
    add_model_arguments,
    apply_model_arguments,
)
from cyber_data_collector.dedup.adjudicator import Adjudicator, EventRecord
from cyber_data_collector.dedup.backfill import run_backfill
from cyber_data_collector.dedup.cluster_adjudicator import ClusterAdjudicator
from cyber_data_collector.dedup.entity_classification import (
    ClassificationFailed,
    EntityRole,
    EntityRoleClassifier,
    resolve_victim,
)
from cyber_data_collector.dedup.role_maintenance import (
    invalidate_all,
    mark_classified,
    stale_event_ids,
    staleness_report,
    DeferredRoleRefresher,
)
from cyber_data_collector.dedup.entity_resolution import (
    EntityResolver,
    significant_tokens,
)
from cyber_data_collector.dedup.date_selection import (
    DateResearcher,
    date_from_url,
    derive_event_date,
    derive_records_affected,
    disagreement,
    extract_date,
)
from cyber_data_collector.dedup.entity_size import (
    EntitySize,
    EntitySizeResearcher,
    estimate_sizes,
    pending_entities,
)
from cyber_data_collector.dedup.recurrence_check import (
    DEFAULT_WINDOW_DAYS,
    RecurrenceAuditor,
    attach_source_urls,
    build_runs,
    findings_from_partition,
    load_recurrence_events,
    run_span,
)
from cyber_data_collector.dedup.entity_merge import (
    OrganisationResolver,
    canonical_groups,
    inherit_attributes,
    merge_entities,
)
from cyber_data_collector.dedup.ledger import DedupLedger, pair_key
from cyber_data_collector.dedup.state_restore import restore_dedup_state
from cyber_data_collector.dedup.non_incident import reject_non_incident_events
from cyber_data_collector.dedup import page_classifier
from cyber_data_collector.dedup import coherence
from cyber_data_collector.dedup.title_selection import (
    TitleGenerator, derive_title, needs_regeneration,
)
from cyber_data_collector.dedup.victim_selection import (
    derive_victim,
    is_descriptive_label,
    mention_share,
    should_replace,
    ubiquitous_entities,
)
from cyber_data_collector.dedup.models import OverrideVerdict

logger = logging.getLogger("dedup_admin")

DEFAULT_DB = "instance/cyber_events.db"
FINDINGS_PATH = Path("instance/dedup_missed_merges.json")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def load_dedup_records(conn: sqlite3.Connection,
                       recent_days: Optional[int] = None) -> List[EventRecord]:
    """One EventRecord per active deduplicated event, keyed by its dedup id.

    ``enriched_event_id`` carries the *deduplicated* id here because the
    reconciliation pass compares whole deduplicated events, not raw members.

    Each record carries every signal candidate generation keys on: all linked
    organisations (entity links, vendor, and Perplexity's formal victim name for
    every member record), all source URLs, and the members' enriched text.
    Loading only the scalar victim and one URL surfaced 9 of 21 known
    duplicates; this surfaces 20.

    Args:
        recent_days: When set, mark which events were created or updated in the
            last N days (``_recent_ids`` on the returned list's records is not
            used; callers filter pairs with :func:`recent_event_ids`).
    """
    rows = conn.execute(
        """
        SELECT d.deduplicated_event_id AS did, d.title, d.summary, d.description,
               d.event_date, d.records_affected,
               d.victim_organization_name AS entity,
               d.vendor_organization_name AS vendor
        FROM DeduplicatedEvents d
        WHERE COALESCE(d.status, 'Active') = 'Active'
        """
    ).fetchall()
    names: Dict[str, set] = {}
    urls: Dict[str, set] = {}
    texts: Dict[str, List[str]] = {}
    for did, name in conn.execute(
        """SELECT dee.deduplicated_event_id, v.entity_name
           FROM DeduplicatedEventEntities dee JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
           WHERE dee.relationship_type IN ('victim', 'vendor', 'affected_customer')"""):
        names.setdefault(did, set()).add(name)
    for did, pe, desc, summ, url in conn.execute(
        """SELECT m.deduplicated_event_id, e.perplexity_enrichment_data, e.description,
                  e.summary, r.source_url
           FROM EventDeduplicationMap m
           JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
           LEFT JOIN RawEvents r ON r.raw_event_id = e.raw_event_id"""):
        try:
            formal = json.loads(pe or "{}").get("formal_entity_name")
        except (TypeError, ValueError, AttributeError):
            formal = None
        if formal and str(formal).strip().lower() not in ("none", "null", "unknown", "n/a"):
            names.setdefault(did, set()).add(str(formal))
        if url:
            urls.setdefault(did, set()).add(url)
        body = " ".join(x for x in (desc, summ) if x)
        if body:
            texts.setdefault(did, []).append(body[:600])
    for did, url in conn.execute("SELECT deduplicated_event_id, source_url FROM DeduplicatedEventSources"):
        if url:
            urls.setdefault(did, set()).add(url)

    records = []
    for r in rows:
        did = r["did"]
        alt = sorted(n for n in names.get(did, set()) | ({r["vendor"]} if r["vendor"] else set())
                     if n and n != r["entity"])
        member_text = " ".join(texts.get(did, []))[:1500]
        records.append(EventRecord(
            enriched_event_id=did,
            title=r["title"] or "",
            summary=r["summary"],
            description=" ".join(x for x in (r["description"], member_text) if x) or None,
            entity_name=r["entity"],
            alt_entities=alt,
            event_date=str(r["event_date"]) if r["event_date"] else None,
            source_urls=sorted(urls.get(did, set())),
            records_affected=r["records_affected"],
        ))
    return records


def recent_event_ids(conn: sqlite3.Connection, days: int) -> set:
    """Active events created or updated in the last ``days`` days."""
    return {row[0] for row in conn.execute(
        """SELECT deduplicated_event_id FROM DeduplicatedEvents
           WHERE COALESCE(status,'Active') = 'Active'
             AND (created_at >= datetime('now', ?) OR updated_at >= datetime('now', ?))""",
        (f"-{days} days", f"-{days} days"))}


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------

def _make_refresher(args):
    """Refresher for commands that change membership, or None if opted out.

    Entity roles are decided against a specific set of records, so a merge or
    split leaves them describing a set the event no longer has. Wiring this
    into the ledger makes the refresh automatic rather than a follow-up command
    someone has to remember.
    """
    if getattr(args, "no_refresh_roles", False):
        return None
    return DeferredRoleRefresher()


def _flush_refresher(conn: sqlite3.Connection, refresher) -> None:
    """Reclassify everything the mutation invalidated. Safe to call with None.

    Runs in the command's ``finally``, so work already committed never ends up
    with roles describing a membership it no longer has - even if the command
    failed part-way.
    """
    if refresher is None:
        return
    try:
        stats = refresher.flush(conn)
        conn.commit()
        if stats.get("refreshed"):
            print(f"auto-refreshed entity roles for {stats['refreshed']} event(s)"
                  + (f", {stats['failed']} failed" if stats.get("failed") else ""))
    except Exception as exc:  # noqa: BLE001 - never mask the command's own result
        logger.warning("Automatic role refresh failed: %s", exc)


def cmd_migrate(args) -> int:
    schema.migrate(args.db)
    print("Schema v3 applied.")
    return 0


def cmd_backfill(args) -> int:
    schema.migrate(args.db)
    conn = _connect(args.db)
    try:
        report = run_backfill(conn, dry_run=args.dry_run)
        print(("DRY RUN: " if args.dry_run else "") + report.summary())
    finally:
        conn.close()
    return 0


def cmd_find_missed(args) -> int:
    """Find deduplicated events that actually describe one incident."""
    conn = _connect(args.db)
    try:
        ledger = DedupLedger(conn)
        resolver = EntityResolver(conn)
        adjudicator = Adjudicator(
            resolver=resolver,
            overrides=ledger.load_overrides(),
        )

        records = load_dedup_records(conn)
        print(f"Loaded {len(records)} active deduplicated event(s)")

        pairs = adjudicator.candidate_pairs(records)
        if getattr(args, "recent_days", None):
            # Routine refreshes only need pairs touching new or changed events;
            # older pairs were already judged by an earlier run.
            recent = recent_event_ids(conn, args.recent_days)
            pairs = [(l, r) for l, r in pairs
                     if l.enriched_event_id in recent or r.enriched_event_id in recent]
            print(f"Scoped to {len(pairs)} pair(s) touching {len(recent)} event(s) "
                  f"changed in the last {args.recent_days} day(s)")
        if args.limit:
            pairs = pairs[: args.limit]
        print(f"Adjudicating {len(pairs)} candidate pair(s)"
              + (" (LLM disabled)" if args.no_llm else ""))
        if not pairs:
            return 0

        if not args.no_llm:
            adjudicator.embed_records(records)

        findings: List[Dict] = []
        for index, (left, right) in enumerate(pairs, start=1):
            if args.no_llm:
                # Blocking-only preview: report candidates without judging.
                findings.append({
                    "left_id": left.enriched_event_id,
                    "right_id": right.enriched_event_id,
                    "left_title": left.title, "right_title": right.title,
                    "left_entity": left.entity_name, "right_entity": right.entity_name,
                    "left_date": left.event_date, "right_date": right.event_date,
                    "is_same_event": None, "certainty": None,
                    "reasoning": "candidate only (--no-llm)", "decided_by": "blocking",
                })
                continue

            verdict = adjudicator.adjudicate(left, right)
            if verdict.is_same_event and verdict.certainty >= args.min_certainty:
                findings.append({
                    "left_id": left.enriched_event_id,
                    "right_id": right.enriched_event_id,
                    "left_title": left.title, "right_title": right.title,
                    "left_entity": left.entity_name, "right_entity": right.entity_name,
                    "left_date": left.event_date, "right_date": right.event_date,
                    "is_same_event": True,
                    "certainty": verdict.certainty,
                    "reasoning": verdict.reasoning,
                    "decided_by": verdict.decided_by.value,
                    "candidate_reasons": sorted(adjudicator.pair_reasons.get(
                        frozenset((left.enriched_event_id, right.enriched_event_id)), [])),
                    "evidence": verdict.evidence.model_dump(),
                })
            if index % 25 == 0:
                print(f"  ...{index}/{len(pairs)} adjudicated, {len(findings)} match(es)")

        FINDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        FINDINGS_PATH.write_text(
            json.dumps(findings, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        print(f"\n{len(findings)} probable missed merge(s) -> {FINDINGS_PATH}")
        print(f"adjudicator stats: {adjudicator.stats}")
        for f in findings[:15]:
            cert = f"{f['certainty']:.2f}" if f["certainty"] else "n/a"
            print(f"  [{cert}] {str(f['left_title'])[:48]!r}\n"
                  f"         + {str(f['right_title'])[:48]!r}\n"
                  f"         {str(f['reasoning'])[:110]}")
    finally:
        conn.close()
    return 0


def cmd_apply_missed(args) -> int:
    if not FINDINGS_PATH.exists():
        print(f"No findings file at {FINDINGS_PATH}; run find-missed first.")
        return 1
    findings = json.loads(FINDINGS_PATH.read_text(encoding="utf-8"))
    eligible = [
        f for f in findings
        if f.get("is_same_event") and (f.get("certainty") or 0) >= args.min_certainty
    ]
    print(f"{len(eligible)} of {len(findings)} finding(s) at or above "
          f"certainty {args.min_certainty}")
    if args.dry_run:
        for f in eligible:
            print(f"  would merge {f['right_id']} -> {f['left_id']}  "
                  f"({str(f['left_title'])[:50]})")
        return 0

    conn = _connect(args.db)
    refresher = _make_refresher(args)
    try:
        ledger = DedupLedger(conn, role_refresher=refresher)
        merged = 0
        # Union-find so a chain A~B, B~C collapses into one target.
        parent: Dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for f in eligible:
            target, source = find(f["left_id"]), find(f["right_id"])
            if target == source:
                continue
            try:
                ledger.merge_events(
                    target, source,
                    reason=f"[{f.get('decided_by')}] {f.get('reasoning')}",
                    actor="pipeline",
                )
                parent[source] = target
                merged += 1
            except (sqlite3.Error, ValueError) as exc:
                logger.warning("Merge %s -> %s failed: %s", source, target, exc)
        conn.commit()
        print(f"Applied {merged} merge(s).")
    finally:
        _flush_refresher(conn, refresher)
        conn.close()
    return 0


LEGACY_PATH = Path("instance/dedup_legacy_review.json")


def load_legacy_merges(conn: sqlite3.Connection, limit: Optional[int] = None) -> List[sqlite3.Row]:
    """Active merged events that carry no recorded decision.

    These predate the audit ledger, so nothing explains why their members were
    grouped. They are exactly the population where v2's literal matching could
    have folded in a foreign organisation unnoticed.
    """
    limit_clause = f" LIMIT {int(limit)}" if limit else ""
    return conn.execute(f"""
        SELECT d.deduplicated_event_id AS did, d.title,
               d.master_enriched_event_id AS master_id,
               COUNT(m.enriched_event_id) AS members
        FROM DeduplicatedEvents d
        JOIN EventDeduplicationMap m ON m.deduplicated_event_id = d.deduplicated_event_id
        WHERE COALESCE(d.status,'Active') = 'Active'
        GROUP BY d.deduplicated_event_id
        HAVING members > 1
           AND NOT EXISTS (SELECT 1 FROM DedupDecisions x
                           WHERE x.deduplicated_event_id = d.deduplicated_event_id)
        ORDER BY members DESC{limit_clause}
    """).fetchall()


def load_member_records(conn: sqlite3.Connection, dedup_id: str) -> List[tuple]:
    """Return ``(EventRecord, contribution_type)`` for each member of an event."""
    rows = conn.execute("""
        SELECT m.enriched_event_id, m.contribution_type, e.title, e.summary,
               e.description, e.event_date, e.records_affected, r.source_url
        FROM EventDeduplicationMap m
        LEFT JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
        LEFT JOIN RawEvents r ON r.raw_event_id = m.raw_event_id
        WHERE m.deduplicated_event_id = ?
    """, (dedup_id,)).fetchall()

    # Load ALL linked organisations per member. Every row in
    # EnrichedEventEntities is tagged 'affected', so the highest-confidence
    # entity is often a regulator rather than the victim; taking the whole set
    # is what stops correct groups being judged as different organisations.
    out = []
    for row in rows:
        names = [r[0] for r in conn.execute("""
            SELECT v.entity_name FROM EnrichedEventEntities ee
            JOIN EntitiesV2 v ON v.entity_id = ee.entity_id
            WHERE ee.enriched_event_id = ?
            ORDER BY ee.confidence_score DESC
        """, (row["enriched_event_id"],))]
        out.append((
            EventRecord(
                enriched_event_id=row["enriched_event_id"],
                title=row["title"] or "",
                summary=row["summary"],
                description=row["description"],
                entity_name=names[0] if names else None,
                alt_entities=names[1:],
                event_date=str(row["event_date"]) if row["event_date"] else None,
                source_url=row["source_url"],
                records_affected=row["records_affected"],
            ),
            row["contribution_type"],
        ))
    return out


def cmd_review_legacy(args) -> int:
    """Adjudicate every member of every unexplained legacy merge.

    Each non-master member is judged against its group's master. Every verdict
    is written to the ledger, so the group stops being "unexplained" whichever
    way it falls, and members judged to be a different incident are collected
    for splitting.
    """
    conn = _connect(args.db)
    try:
        ledger = DedupLedger(conn)
        resolver = EntityResolver(conn)
        # These events are ALREADY grouped, and their stored organisation
        # labels are noisy (regulators recorded as 'affected' alongside
        # victims). A hard entity gate here rejects correct groups with high
        # confidence, so entity disagreement is downgraded to evidence and the
        # LLM judges on content.
        adjudicator = Adjudicator(
            resolver=resolver,
            overrides=ledger.load_overrides(),
            require_entity_match=False,
        )

        groups = load_legacy_merges(conn, limit=args.limit)
        total_pairs = sum(g["members"] - 1 for g in groups)
        print(f"{len(groups)} unexplained merged event(s), {total_pairs} member pair(s)")
        if not groups:
            return 0

        # Fit blocking on the whole corpus so entity comparisons are calibrated
        # the same way as a normal run.
        all_names = [r[0] for r in conn.execute(
            "SELECT DISTINCT victim_organization_name FROM DeduplicatedEvents "
            "WHERE victim_organization_name IS NOT NULL")]
        resolver.fit(all_names)

        batch_id = f"legacy-review-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        wrong: List[Dict] = []
        confirmed = 0
        adjudicated = 0

        for index, group in enumerate(groups, start=1):
            members = load_member_records(conn, group["did"])
            # Identify the master by DeduplicatedEvents.master_enriched_event_id,
            # not by contribution_type. A chained merge re-tags an intermediate
            # group's master row as 'merged', leaving 48 groups with no
            # 'master' row at all - keying on the tag silently skipped them.
            master = next(
                (r for r, _ in members if r.enriched_event_id == group["master_id"]),
                None,
            )
            if master is None:
                master = next((r for r, c in members if c == "master"), None)
            if master is None:
                logger.warning(
                    "Group %s has no resolvable master; skipping", group["did"])
                continue
            others = [r for r, _ in members
                      if r.enriched_event_id != master.enriched_event_id]
            if not others:
                continue

            if not args.no_llm:
                adjudicator.embed_records([master] + others)

            for member in others:
                verdict = adjudicator.adjudicate(master, member)
                adjudicated += 1
                ledger.record_pair_verdict(
                    batch_id, master.enriched_event_id, member.enriched_event_id,
                    verdict, deduplicated_event_id=group["did"],
                    method="legacy-review",
                )
                if verdict.is_same_event:
                    confirmed += 1
                elif verdict.certainty >= args.min_certainty:
                    wrong.append({
                        "dedup_id": group["did"],
                        "group_title": group["title"],
                        "master_id": master.enriched_event_id,
                        "master_title": master.title,
                        "master_entity": master.entity_name,
                        "member_id": member.enriched_event_id,
                        "member_title": member.title,
                        "member_entity": member.entity_name,
                        "certainty": verdict.certainty,
                        "decided_by": verdict.decided_by.value,
                        "reasoning": verdict.reasoning,
                    })

            conn.commit()
            if index % 20 == 0:
                print(f"  ...{index}/{len(groups)} groups | {adjudicated} judged | "
                      f"{confirmed} confirmed | {len(wrong)} wrongly merged")

        LEGACY_PATH.parent.mkdir(parents=True, exist_ok=True)
        LEGACY_PATH.write_text(
            json.dumps(wrong, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8")

        print(f"\nadjudicated {adjudicated} member(s) across {len(groups)} group(s)")
        print(f"  confirmed correct : {confirmed}")
        print(f"  wrongly merged    : {len(wrong)} -> {LEGACY_PATH}")
        print(f"  adjudicator stats : {adjudicator.stats}")
        for w in wrong[:20]:
            print(f"\n  [{w['certainty']:.2f}] group: {str(w['group_title'])[:60]}")
            print(f"        master: {str(w['master_title'])[:60]} ({w['master_entity']})")
            print(f"        member: {str(w['member_title'])[:60]} ({w['member_entity']})")
            print(f"        {str(w['reasoning'])[:130]}")
    finally:
        conn.close()
    return 0


def cmd_apply_legacy(args) -> int:
    """Split out the members the legacy review judged to be different incidents."""
    if not LEGACY_PATH.exists():
        print(f"No review file at {LEGACY_PATH}; run review-legacy first.")
        return 1
    wrong = json.loads(LEGACY_PATH.read_text(encoding="utf-8"))
    eligible = [w for w in wrong if (w.get("certainty") or 0) >= args.min_certainty]
    print(f"{len(eligible)} of {len(wrong)} at or above certainty {args.min_certainty}")

    if args.dry_run:
        for w in eligible[:40]:
            print(f"  would split {str(w['member_title'])[:60]!r} out of "
                  f"{str(w['group_title'])[:45]!r}")
        return 0

    conn = _connect(args.db)
    refresher = _make_refresher(args)
    try:
        ledger = DedupLedger(conn, role_refresher=refresher)
        split = 0
        for w in eligible:
            try:
                ledger.split_member(
                    w["dedup_id"], w["member_id"],
                    reason=f"[legacy-review {w['decided_by']} {w['certainty']:.2f}] "
                           f"{w['reasoning']}",
                    actor="pipeline",
                )
                split += 1
            except (sqlite3.Error, ValueError) as exc:
                logger.warning("Split %s failed: %s", w["member_id"], exc)
        conn.commit()
        print(f"Split {split} wrongly merged member(s) into their own events.")
    finally:
        _flush_refresher(conn, refresher)
        conn.close()
    return 0


def cmd_resolve_band(args) -> int:
    """Give every borderline legacy finding a final disposition.

    Findings in the 0.80-0.89 certainty band are genuinely ambiguous, so
    neither auto-applying them nor leaving them dangling is right. Each pair is
    re-adjudicated TWICE with the records in both orders. Re-running at
    temperature 0 in the same order would just repeat the first answer;
    swapping them is a real robustness test, because a verdict that flips on
    presentation order is not a verdict worth acting on.

        both say "different"  -> split (the merge was wrong)
        both say "same"       -> confirm the merge, record the decision
        they disagree         -> leave merged, record as genuinely ambiguous

    Every finding ends up resolved and audited either way.
    """
    conn = _connect(args.db)
    refresher = _make_refresher(args)
    try:
        ledger = DedupLedger(conn, role_refresher=refresher)
        resolver = EntityResolver(conn)
        adjudicator = Adjudicator(
            resolver=resolver,
            overrides=ledger.load_overrides(),
            require_entity_match=False,
        )

        if not LEGACY_PATH.exists():
            print(f"No review file at {LEGACY_PATH}; run review-legacy first.")
            return 1
        findings = json.loads(LEGACY_PATH.read_text(encoding="utf-8"))
        band = [f for f in findings
                if args.min_certainty <= (f.get("certainty") or 0) < args.max_certainty]

        pending = [
            f for f in band
            if conn.execute(
                "SELECT 1 FROM EventDeduplicationMap WHERE deduplicated_event_id=? "
                "AND enriched_event_id=?", (f["dedup_id"], f["member_id"])).fetchone()
        ]
        print(f"{len(band)} finding(s) in band [{args.min_certainty}, "
              f"{args.max_certainty}); {len(pending)} still unapplied")
        if not pending:
            return 0

        batch_id = f"band-resolve-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        split_list, confirmed, ambiguous = [], 0, 0

        for index, f in enumerate(pending, start=1):
            members = load_member_records(conn, f["dedup_id"])
            by_id = {r.enriched_event_id: r for r, _ in members}
            master = by_id.get(f["master_id"])
            member = by_id.get(f["member_id"])
            if master is None or member is None:
                continue

            adjudicator.embed_records([master, member])
            forward = adjudicator.adjudicate(master, member, force_llm=True)
            reverse = adjudicator.adjudicate(member, master, force_llm=True)

            agree = forward.is_same_event == reverse.is_same_event
            min_cert = min(forward.certainty, reverse.certainty)

            if agree and not forward.is_same_event and min_cert >= args.decide_at:
                split_list.append({**f, "resolved_certainty": min_cert,
                                   "resolved_reasoning": forward.reasoning})
                verdict, note = forward, "order-stable: different incidents"
            elif agree and forward.is_same_event:
                confirmed += 1
                verdict, note = forward, "order-stable: same incident, merge confirmed"
            else:
                ambiguous += 1
                verdict, note = forward, (
                    f"AMBIGUOUS: verdict flipped on record order "
                    f"(forward={forward.is_same_event}, reverse={reverse.is_same_event}); "
                    f"left merged for human review"
                )

            ledger.record_pair_verdict(
                batch_id, master.enriched_event_id, member.enriched_event_id,
                verdict, deduplicated_event_id=f["dedup_id"],
                method=f"band-resolve ({note})",
            )
            conn.commit()
            if index % 20 == 0:
                print(f"  ...{index}/{len(pending)} | split {len(split_list)} | "
                      f"confirmed {confirmed} | ambiguous {ambiguous}")

        print(f"\nresolved {len(pending)} borderline finding(s):")
        print(f"  confirmed as wrongly merged (will split) : {len(split_list)}")
        print(f"  confirmed as correctly merged            : {confirmed}")
        print(f"  order-unstable, left merged for review   : {ambiguous}")
        print(f"  adjudicator stats: {adjudicator.stats}")

        if args.dry_run:
            print("\nDry run - no splits applied.")
            return 0

        applied = 0
        for f in split_list:
            try:
                ledger.split_member(
                    f["dedup_id"], f["member_id"],
                    reason=f"[band-resolve {f['resolved_certainty']:.2f}] "
                           f"{f['resolved_reasoning']}",
                    actor="pipeline",
                )
                applied += 1
            except (sqlite3.Error, ValueError) as exc:
                logger.warning("Split %s failed: %s", f["member_id"], exc)
        conn.commit()
        print(f"\nApplied {applied} split(s).")
    finally:
        _flush_refresher(conn, refresher)
        conn.close()
    return 0


def cmd_recheck_splits(args) -> int:
    """Re-examine splits that separated records about the SAME organisation.

    The borderline band produces a specific false positive: one big incident
    generates a long tail of follow-up coverage - regulator statements, class
    actions, "government concludes response" - and the adjudicator reads the
    differing focus and dates as a different incident. Observed on the HWL
    Ebsworth and Regis Resources clusters, where five to seven records of a
    single breach were pulled apart.

    Only splits where both sides resolve to the same canonical entity are
    re-examined, since that is the only shape this error takes. The re-check
    prompt states the follow-up rule explicitly, and anything judged to be one
    incident is merged back through the ledger (fully audited and reversible).
    """
    conn = _connect(args.db)
    refresher = _make_refresher(args)
    try:
        ledger = DedupLedger(conn, role_refresher=refresher)
        resolver = EntityResolver(conn)
        adjudicator = Adjudicator(
            resolver=resolver, overrides=ledger.load_overrides(),
            require_entity_match=False,
        )

        # Splits made by the band-resolve / legacy passes, with the group each
        # record came from (the immediately preceding decision).
        rows = conn.execute("""
            SELECT d.enriched_event_id, d.deduplicated_event_id AS now_id,
                   d.reasoning, d.created_at,
                   (SELECT p.deduplicated_event_id FROM DedupDecisions p
                     WHERE p.enriched_event_id = d.enriched_event_id
                       AND p.deduplicated_event_id != d.deduplicated_event_id
                       AND p.created_at <= d.created_at
                     ORDER BY p.created_at DESC LIMIT 1) AS former_id
            FROM DedupDecisions d
            WHERE d.action = 'revert'
              AND (d.reasoning LIKE '[band-resolve%' OR d.reasoning LIKE '[legacy-review%')
        """).fetchall()

        candidates = []
        for r in rows:
            if not r["former_id"]:
                continue
            former = conn.execute(
                "SELECT victim_organization_name AS v, master_enriched_event_id AS m "
                "FROM DeduplicatedEvents WHERE deduplicated_event_id=?",
                (r["former_id"],)).fetchone()
            now = conn.execute(
                "SELECT victim_organization_name AS v FROM DeduplicatedEvents "
                "WHERE deduplicated_event_id=?", (r["now_id"],)).fetchone()
            if not former or not now:
                continue
            # Same-organisation splits only.
            if not resolver.are_candidates(former["v"], now["v"]):
                continue
            candidates.append((r, former))

        print(f"{len(rows)} split(s) from legacy/band passes; "
              f"{len(candidates)} separated records about the same organisation")
        if not candidates:
            return 0

        reverted, upheld = 0, 0
        for index, (r, former) in enumerate(candidates, start=1):
            members = load_member_records(conn, r["former_id"])
            by_id = {rec.enriched_event_id: rec for rec, _ in members}
            master = by_id.get(former["m"]) or (members[0][0] if members else None)
            moved = load_member_records(conn, r["now_id"])
            member = next((rec for rec, _ in moved
                           if rec.enriched_event_id == r["enriched_event_id"]), None)
            if master is None or member is None:
                continue

            adjudicator.embed_records([master, member])
            verdict = adjudicator.adjudicate(master, member, force_llm=True)

            if verdict.is_same_event and verdict.certainty >= args.min_certainty:
                if not args.dry_run:
                    try:
                        ledger.merge_events(
                            r["former_id"], r["now_id"],
                            reason=f"[recheck {verdict.certainty:.2f}] {verdict.reasoning}",
                            actor="pipeline",
                        )
                        conn.commit()
                    except (sqlite3.Error, ValueError) as exc:
                        logger.warning("Re-merge failed for %s: %s",
                                       r["enriched_event_id"], exc)
                        continue
                reverted += 1
                print(f"  REVERT {str(member.title)[:52]!r}\n"
                      f"         back into {str(master.title)[:52]!r}\n"
                      f"         {verdict.reasoning[:110]}")
            else:
                upheld += 1
            if index % 20 == 0:
                print(f"  ...{index}/{len(candidates)} | reverted {reverted} | "
                      f"upheld {upheld}")

        print(f"\n{'DRY RUN: ' if args.dry_run else ''}"
              f"re-merged {reverted} wrongly split record(s); upheld {upheld} split(s)")
        print(f"adjudicator stats: {adjudicator.stats}")
    finally:
        _flush_refresher(conn, refresher)
        conn.close()
    return 0


def cmd_consolidate(args) -> int:
    """Re-apply Rule 1: same canonical entity + same event date -> one event.

    Pairwise adjudication has a blind spot. When one breach generates a long
    tail of coverage - the incident report, the regulator statement, the class
    action, "government concludes response" - each article emphasises something
    different, so pair-by-pair an LLM keeps answering "different focus,
    therefore different incident". Compared against a vaguely-titled master it
    upholds every one of those splits, and a single breach ends up scattered:
    HWL Ebsworth reached 19 separate active events, all dated 2023-04-26.

    Looking at the cluster as a whole rather than pair by pair makes it
    obvious, and the project already documents the rule for it (docs/06):
    same entity + same date is one event. That is deterministic, free, and
    exactly the invariant the over-splitting violated.

    Events with no organisation or no date are left alone - there is not enough
    identity to group them safely.
    """
    conn = _connect(args.db)
    refresher = _make_refresher(args)
    try:
        ledger = DedupLedger(conn, role_refresher=refresher)
        resolver = EntityResolver(conn)

        rows = conn.execute("""
            SELECT deduplicated_event_id AS id, title, event_date,
                   victim_organization_name AS victim, created_at,
                   (SELECT COUNT(*) FROM EventDeduplicationMap m
                     WHERE m.deduplicated_event_id = d.deduplicated_event_id) AS members
            FROM DeduplicatedEvents d
            WHERE COALESCE(status,'Active')='Active'
              AND victim_organization_name IS NOT NULL
              AND victim_organization_name != ''
              AND event_date IS NOT NULL
        """).fetchall()

        resolver.fit([r["victim"] for r in rows])
        clusters: Dict[tuple, List] = {}
        skipped_generic = 0
        for r in rows:
            # A victim recorded as "Australia" or "Government" is a failed
            # entity extraction, not an organisation. Grouping on it would
            # merge unrelated incidents that happen to share a date, so those
            # clusters are left alone rather than guessed at.
            if not significant_tokens(r["victim"]):
                skipped_generic += 1
                continue
            clusters.setdefault(str(r["event_date"])[:10], []).append(r)

        # Optional date tolerance. Reporting of one incident often carries
        # dates a day or two apart (HWL Ebsworth records sit on both 2023-04-26
        # and 2023-04-28), which date-exact grouping cannot join. Defaults to 0
        # so the strict Rule 1 behaviour is unchanged unless asked for.
        if args.date_tolerance > 0:
            from datetime import date as _date
            def _parse(text):
                try:
                    return _date.fromisoformat(text)
                except ValueError:
                    return None
            ordered = sorted(clusters.items())
            joined: Dict[str, List] = {}
            anchors: List[str] = []
            for day, events in ordered:
                parsed = _parse(day)
                target = None
                if parsed is not None:
                    for anchor in anchors:
                        anchor_date = _parse(anchor)
                        if (anchor_date is not None
                                and abs((parsed - anchor_date).days) <= args.date_tolerance):
                            target = anchor
                            break
                if target is None:
                    anchors.append(day)
                    joined[day] = list(events)
                else:
                    joined[target].extend(events)
            clusters = joined

        # Within a date, group by entity *candidacy* rather than exact key.
        # Exact-key grouping leaves variant spellings apart - HWL Ebsworth
        # records carry several different victim strings, so keying on the
        # literal name left one breach in 15 pieces.
        merged_clusters: Dict[tuple, List] = {}
        for date, same_day in clusters.items():
            buckets: List[List] = []
            for event in same_day:
                for bucket in buckets:
                    if resolver.are_candidates(bucket[0]["victim"], event["victim"]):
                        bucket.append(event)
                        break
                else:
                    buckets.append([event])
            for bucket in buckets:
                if len(bucket) > 1:
                    key = (resolver.key_for(bucket[0]["victim"]), date)
                    merged_clusters[key] = bucket
        multi = merged_clusters
        if skipped_generic:
            print(f"skipped {skipped_generic} event(s) whose victim name carries no "
                  f"identifying token (e.g. 'Australia')")

        total_extra = sum(len(v) - 1 for v in multi.values())
        print(f"{len(rows)} dated events with an organisation; "
              f"{len(multi)} cluster(s) hold {total_extra} duplicate event(s)")
        for (entity, date), group in sorted(multi.items(),
                                            key=lambda kv: -len(kv[1]))[:12]:
            print(f"  {entity!r} {date}: {len(group)} events "
                  f"-> {str(group[0]['title'])[:46]!r}")
        if not multi:
            return 0
        if args.dry_run:
            print("\nDry run - nothing merged.")
            return 0

        merged = 0
        for (entity, date), group in multi.items():
            # Keep the richest event as the target; fold the rest into it.
            group = sorted(group, key=lambda r: (-r["members"], str(r["created_at"])))
            target = group[0]
            for source in group[1:]:
                try:
                    ledger.merge_events(
                        target["id"], source["id"],
                        reason=(f"[consolidate] Same organisation ({entity}) and same "
                                f"event date ({date}); Rule 1 treats these as one "
                                f"incident."),
                        actor="pipeline",
                    )
                    merged += 1
                except (sqlite3.Error, ValueError) as exc:
                    logger.warning("Consolidate %s -> %s failed: %s",
                                   source["id"], target["id"], exc)
            conn.commit()
        print(f"\nConsolidated {merged} duplicate event(s) into "
              f"{len(multi)} incident(s).")
    finally:
        _flush_refresher(conn, refresher)
        conn.close()
    return 0


def cmd_adjudicate_clusters(args) -> int:
    """Partition entity clusters into incidents, judging each cluster whole.

    Pairwise adjudication cannot see a cluster: asked about any two records of
    one breach it keeps answering "different focus, therefore different
    incident", consistently enough that re-checking pairs does not help. This
    shows the model every record in the cluster at once and asks for a
    partition, which is internally consistent by construction.
    """
    conn = _connect(args.db)
    refresher = _make_refresher(args)
    try:
        ledger = DedupLedger(conn, role_refresher=refresher)
        resolver = EntityResolver(conn)
        adjudicator = ClusterAdjudicator(
            resolver=resolver, max_cluster_size=args.max_cluster_size)

        rows = conn.execute("""
            SELECT d.deduplicated_event_id AS did, d.title, d.summary,
                   d.event_date, d.records_affected,
                   d.victim_organization_name AS entity
            FROM DeduplicatedEvents d
            WHERE COALESCE(d.status,'Active')='Active'
              AND d.victim_organization_name IS NOT NULL
              AND d.victim_organization_name != ''
        """).fetchall()
        records = [
            EventRecord(
                enriched_event_id=r["did"], title=r["title"] or "",
                summary=r["summary"], entity_name=r["entity"],
                event_date=str(r["event_date"]) if r["event_date"] else None,
                records_affected=r["records_affected"],
            ) for r in rows
        ]

        clusters = adjudicator.build_clusters(records)
        if args.entity:
            needle = args.entity.lower()
            clusters = [c for c in clusters
                        if any(needle in (r.entity_name or "").lower() for r in c)]
        if args.limit:
            clusters = sorted(clusters, key=len, reverse=True)[: args.limit]

        total = sum(len(c) for c in clusters)
        print(f"{len(records)} active event(s) -> {len(clusters)} multi-member "
              f"cluster(s) covering {total} event(s)")
        if not clusters:
            return 0

        planned_merges = 0
        for index, cluster in enumerate(sorted(clusters, key=len, reverse=True), start=1):
            partition = adjudicator.adjudicate_cluster(cluster)
            groups = partition.groups_as_ids(cluster)
            if len(groups) == len(cluster):
                continue  # every record its own incident: nothing to do

            entity = cluster[0].entity_name
            print(f"\n[{index}] {entity!r}: {len(cluster)} events -> "
                  f"{len(groups)} incident(s)")
            for group, ids in groups:
                print(f"    * {group.label[:66]} ({len(ids)} record(s), "
                      f"certainty {group.certainty:.2f})")
                if len(ids) > 1:
                    planned_merges += len(ids) - 1
                if args.verbose:
                    for rid in ids:
                        rec = next(r for r in cluster if r.enriched_event_id == rid)
                        print(f"        - {str(rec.title)[:64]}")

            if args.dry_run:
                continue

            for group, ids in groups:
                if len(ids) < 2 or group.certainty < args.min_certainty:
                    continue
                target, sources = ids[0], ids[1:]
                for source in sources:
                    try:
                        ledger.merge_events(
                            target, source,
                            reason=(f"[cluster {group.certainty:.2f}] {group.label}: "
                                    f"{group.reasoning}"),
                            actor="pipeline",
                        )
                    except (sqlite3.Error, ValueError) as exc:
                        logger.warning("Cluster merge %s -> %s failed: %s",
                                       source, target, exc)
            conn.commit()

        print(f"\n{'DRY RUN: ' if args.dry_run else ''}"
              f"{planned_merges} event(s) {'would be' if args.dry_run else ''} merged")
        print(f"cluster stats: {adjudicator.stats}")
    finally:
        _flush_refresher(conn, refresher)
        conn.close()
    return 0



RECURRENCE_FINDINGS_PATH = Path("instance/recurrence_findings.json")


def _member_ids(conn: sqlite3.Connection, dedup_id: str) -> List[str]:
    return [r[0] for r in conn.execute(
        "SELECT enriched_event_id FROM EventDeduplicationMap "
        "WHERE deduplicated_event_id = ?", (dedup_id,))]


def _override_blocks_merge(conn, overrides, target_id: str, source_id: str) -> bool:
    """True when a human has already ruled two of these records different.

    Overrides are keyed on ``enriched_event_id`` pairs because those are stable
    across rebuilds, so checking them for a *deduplicated* pair means checking
    every member pair. A single 'different' ruling vetoes the merge - it is a
    human answer to precisely this question.
    """
    if not overrides:
        return False
    left = _member_ids(conn, target_id)
    right = _member_ids(conn, source_id)
    for a in left:
        for b in right:
            verdict = overrides.get(frozenset((a, b)))
            if verdict is False:
                return True
    return False


def cmd_check_recurrences(args) -> int:
    """Re-check every repeat attack that follows another within the window.

    A repeat incident at the same organisation a few weeks after the last one is
    the shape a *reporting lag* produces: the follow-up article, the regulator's
    notification and the class action all describe the first breach but carry
    their own publication dates. Stored as separate events they read as a second
    attack that never happened - and they land in the short-elapsed-time band
    that every recurrence model is estimated from, so the error does not add
    noise, it manufactures signal.

    Neither existing pass covers this: ``consolidate`` requires the same date,
    and ``adjudicate-clusters`` only compares records its blocking put in the
    same cluster. This enumerates every short-gap run directly.

    Runs are judged whole rather than pair by pair, so the verdicts cannot
    contradict each other, and a human 'different' override vetoes any merge.
    """
    conn = _connect(args.db)
    refresher = _make_refresher(args)
    try:
        schema.migrate(conn)
        ledger = DedupLedger(conn, role_refresher=refresher)
        overrides = ledger.load_overrides()
        auditor = RecurrenceAuditor(window_days=args.window_days,
                                    max_run_size=args.max_run_size)

        events = load_recurrence_events(conn)
        runs = build_runs(events, window_days=args.window_days)
        if args.entity:
            needle = args.entity.lower()
            runs = [r for r in runs if needle in (r[0].entity_name or "").lower()]
        runs.sort(key=len, reverse=True)
        if args.limit:
            runs = runs[: args.limit]

        pairs = sum(len(r) - 1 for r in runs)
        print(f"{len(events)} victim-attributed event(s); {len(runs)} run(s) of "
              f"repeat attacks under {args.window_days} days apart, covering "
              f"{sum(len(r) for r in runs)} event(s) and {pairs} short gap(s)")
        if not runs:
            print("No repeat attacks fall inside the window - nothing to check.")
            return 0

        for run in runs:
            attach_source_urls(conn, run)

        all_findings: List[Dict] = []
        merged = 0
        blocked = 0
        for index, run in enumerate(runs, start=1):
            partition = auditor.audit_run(run)
            findings = findings_from_partition(run, partition)
            if not findings:
                if args.verbose:
                    print(f"\n[{index}] {run[0].entity_name!r}: {len(run)} events "
                          f"over {run_span(run)} days - all distinct incidents")
                continue

            print(f"\n[{index}] {run[0].entity_name!r}: {len(run)} events over "
                  f"{run_span(run)} days -> {len(partition.incidents)} incident(s)")
            for finding in findings:
                total = 1 + len(finding["sources"])
                print(f"    * {finding['label'][:64]} - {total} events are one "
                      f"incident (certainty {finding['certainty']:.2f}, "
                      f"max gap {finding['max_gap_days']}d)")
                print(f"      keep: {finding['target']['date']} "
                      f"{str(finding['target']['title'])[:58]!r}")
                for source in finding["sources"]:
                    print(f"      fold: {source['date']} "
                          f"{str(source['title'])[:58]!r}")
                if args.verbose:
                    print(f"      why: {finding['reasoning'][:200]}")
            all_findings.extend(findings)

            if args.dry_run:
                continue

            for finding in findings:
                if finding["certainty"] < args.min_certainty:
                    continue
                target = finding["target"]["id"]
                for source in finding["sources"]:
                    if _override_blocks_merge(conn, overrides, target, source["id"]):
                        blocked += 1
                        print(f"      (skipped {source['id'][:8]}: a human "
                              f"override rules these different)")
                        continue
                    try:
                        ledger.merge_events(
                            target, source["id"],
                            reason=(f"[recurrence {finding['certainty']:.2f}] "
                                    f"{finding['label']}: apparent repeat attack "
                                    f"{finding['max_gap_days']} days apart is the "
                                    f"same incident reported again. "
                                    f"{finding['reasoning']}"),
                            actor="pipeline",
                        )
                        merged += 1
                    except (sqlite3.Error, ValueError) as exc:
                        logger.warning("Recurrence merge %s -> %s failed: %s",
                                       source["id"], target, exc)
            conn.commit()

        if all_findings:
            RECURRENCE_FINDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            RECURRENCE_FINDINGS_PATH.write_text(
                json.dumps({"generated_at": datetime.now().isoformat(),
                            "window_days": args.window_days,
                            "findings": all_findings}, indent=2),
                encoding="utf-8")
            print(f"\nFindings written to {RECURRENCE_FINDINGS_PATH}")

        suspect = sum(len(f["sources"]) for f in all_findings)
        print(f"\n{'DRY RUN: ' if args.dry_run else ''}{suspect} event(s) look "
              f"like re-reporting of an earlier incident rather than a new attack"
              + (f"; {merged} merged" if not args.dry_run else "")
              + (f"; {blocked} blocked by human override" if blocked else ""))
        print(f"audit stats: {auditor.stats}")
    finally:
        _flush_refresher(conn, refresher)
        conn.close()
    return 0


def cmd_size_entities(args) -> int:
    """Give every entity an ordinal size band, researched online.

    ``EntitiesV2.employee_count`` and ``turnover`` have been NULL on every row
    since the table was created, so any analysis asking how big the victim was
    ran with one "unknown" level for the whole dataset. The band is recoverable
    where an exact headcount is not, and Perplexity establishes it by looking
    the organisation up rather than recalling it - which matters because most of
    these are small Australian businesses an LLM has never heard of but will
    still confidently size.

    Incremental by default: entities already carrying an estimate are skipped,
    so this is cheap to re-run after an ingest. Rows whose research could not be
    reached are marked ``unavailable`` and retried automatically next run.
    """
    conn = _connect(args.db)
    try:
        schema.migrate(conn)
        rows = pending_entities(
            conn, refresh=args.refresh, entity_filter=args.entity,
            linked_only=args.linked_only, limit=args.limit)
        if not rows:
            print("Every entity already has a size estimate. "
                  "Use --refresh to re-estimate.")
            return 0

        researcher = EntitySizeResearcher()
        if not researcher.perplexity_key:
            print("WARNING: PERPLEXITY_API_KEY is not set. Without it no "
                  "organisation can be looked up and every row would be "
                  "recorded 'unavailable'. Nothing done.")
            return 1

        print(f"{len(rows)} entit{'y' if len(rows) == 1 else 'ies'} to size"
              + (" (dry run)" if args.dry_run else ""))

        def show(name, result):
            band = result["size"]
            label = band.value if isinstance(band, EntitySize) else str(band)
            # flush: a full backfill runs for over an hour and is usually
            # redirected to a log, where block buffering would hide progress
            # until the very end.
            print(f"  {label:<8} {str(name)[:48]:<50} "
                  f"({result['confidence']:.2f}) {str(result['basis'])[:70]}",
                  flush=True)

        stats = estimate_sizes(conn, rows, researcher=researcher,
                               workers=args.workers, dry_run=args.dry_run,
                               progress=show if args.verbose else None)

        print(f"\n{'DRY RUN: ' if args.dry_run else ''}{stats['entities']} "
              f"entities sized from {stats['lookups']} lookup(s) "
              f"({stats['reused']} reused across name variants)")
        for band in ("SMALL", "MEDIUM", "LARGE", "HUGE", "UNKNOWN"):
            if stats["bands"].get(band):
                print(f"  {band:<8} {stats['bands'][band]}")
        print(f"research stats: {stats['researcher_stats']}")
    finally:
        conn.close()
    return 0


def cmd_reconcile_entities(args) -> int:
    """Re-examine every event recorded against the same organisation.

    The follow-up pass deduplication needs. Merging is driven by blocking, so
    anything blocking missed - a spelling variant, a missing date, an incident
    described in different words - stays missed no matter how good the
    adjudicator is. This ignores blocking entirely: it groups by the entity
    finally attributed to each event and asks whether those events are really
    one incident.

    Grouping uses the ``victim`` **role** rather than
    ``victim_organization_name``, so the events an entity actually shows under
    in the dashboard are the events compared - including the many with no
    scalar victim, which the cluster pass skipped altogether.
    """
    conn = _connect(args.db)
    refresher = _make_refresher(args)
    try:
        schema.migrate(conn)
        ledger = DedupLedger(conn, role_refresher=refresher)
        adjudicator = ClusterAdjudicator(
            resolver=EntityResolver(conn), max_cluster_size=args.max_cluster_size)

        rows = conn.execute("""
            SELECT v.entity_name AS entity, d.deduplicated_event_id AS did,
                   d.title, d.summary, d.event_date, d.records_affected
            FROM DeduplicatedEventEntities dee
            JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
            JOIN DeduplicatedEvents d
                 ON d.deduplicated_event_id = dee.deduplicated_event_id
            WHERE dee.relationship_type = 'victim'
              AND COALESCE(d.status,'Active') = 'Active'
            ORDER BY v.entity_name, d.event_date
        """).fetchall()

        by_entity: Dict[str, List[EventRecord]] = {}
        for r in rows:
            by_entity.setdefault(r["entity"], []).append(EventRecord(
                enriched_event_id=r["did"], title=r["title"] or "",
                summary=r["summary"], entity_name=r["entity"],
                event_date=str(r["event_date"]) if r["event_date"] else None,
                records_affected=r["records_affected"]))

        # One event can be listed under several victims; judging it under each
        # would merge the same pair twice, so an event is settled once.
        groups = {e: recs for e, recs in by_entity.items() if len(recs) > 1}
        if args.entity:
            needle = args.entity.lower()
            groups = {e: r for e, r in groups.items() if needle in e.lower()}
        ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))
        if args.limit:
            ordered = ordered[: args.limit]

        print(f"{len(by_entity)} entit(y/ies) with victim events; "
              f"{len(groups)} have more than one event"
              + (f"; examining {len(ordered)}" if args.limit else ""))

        settled: set = set()
        planned = 0
        applied = 0
        for index, (entity, records) in enumerate(ordered, start=1):
            cluster = [r for r in records if r.enriched_event_id not in settled]
            if len(cluster) < 2:
                continue
            partition = adjudicator.adjudicate_cluster(cluster)
            parts = partition.groups_as_ids(cluster)
            if len(parts) == len(cluster):
                continue

            print(f"\n[{index}] {entity!r}: {len(cluster)} events -> "
                  f"{len(parts)} incident(s)")
            for group, ids in parts:
                print(f"    * {group.label[:64]} ({len(ids)} record(s), "
                      f"certainty {group.certainty:.2f})")
                if args.verbose:
                    for rid in ids:
                        rec = next(r for r in cluster if r.enriched_event_id == rid)
                        print(f"        - [{rec.event_date or 'no date'}] "
                              f"{str(rec.title)[:58]}")
                if len(ids) > 1:
                    planned += len(ids) - 1

            if args.dry_run:
                continue

            for group, ids in parts:
                if len(ids) < 2 or group.certainty < args.min_certainty:
                    continue
                target, sources = ids[0], ids[1:]
                for source in sources:
                    try:
                        ledger.merge_events(
                            target, source,
                            reason=(f"[entity reconcile {group.certainty:.2f}] "
                                    f"{group.label}: {group.reasoning}"),
                            actor="pipeline")
                        settled.add(source)
                        applied += 1
                    except (sqlite3.Error, ValueError) as exc:
                        logger.warning("Reconcile merge %s -> %s failed: %s",
                                       source, target, exc)
            conn.commit()

        # "planned" counts every proposed grouping; only those at or above
        # --min-certainty are applied. Reporting planned as "merged" made a run
        # that changed nothing look like it had merged ten events.
        if args.dry_run:
            print(f"\nDRY RUN: {planned} merge(s) proposed")
        else:
            print(f"\n{planned} merge(s) proposed, {applied} applied "
                  f"({planned - applied} below certainty {args.min_certainty})")
        print(f"cluster stats: {adjudicator.stats}")
    finally:
        _flush_refresher(conn, refresher)
        conn.close()
    return 0


CANDIDATE_CLUSTER_FINDINGS = Path("instance/dedup_candidate_clusters.json")
def cmd_check_coherence(args) -> int:
    """Split out stray records - members describing a different incident.

    See :mod:`cyber_data_collector.dedup.coherence`. Suspects are found for
    free (a member naming another organisation); Jev judges each suspect
    (escalating its middle band to the reasoning model) and, with ``--apply``,
    suspects below the calibrated cut-off and members labelled 'different' are
    split out with the ledger. Without a calibration matching the current
    models only labelled strays are split. Findings are written next to the
    database as ``dedup_coherence_findings.json``.

    No fallback: a missing TypeSafe key or SDK or OpenAI key (exit 2), a Jev
    failure after the SDK's retries, or - with ``--apply`` - a missing,
    unreadable or empty labels or calibration file (exit 1) stops the command
    before anything is split. A failed escalation leaves that member
    undecided: it is not acted on, and the command exits 1.
    """
    from cyber_data_collector.utils import jev

    missing = []
    if not os.getenv("TYPESAFE_API_KEY"):
        missing.append("TYPESAFE_API_KEY")
    elif not jev.available():
        missing.append("the typesafe-sdk package")
    if not os.getenv("OPENAI_API_KEY"):
        missing.append("OPENAI_API_KEY (escalation model)")
    if missing:
        print(f"check-coherence cannot run: {', '.join(missing)} missing. Nothing judged or split.",
              file=sys.stderr)
        return 2
    conn = _connect(args.db)
    refresher = _make_refresher(args)
    try:
        ledger = DedupLedger(conn, role_refresher=refresher)
        try:
            result = coherence.check_coherence(conn, apply=args.apply, only=args.event or None,
                                               ledger=ledger)
        except (coherence.CoherenceJudgementError, coherence.CoherenceConfigError) as exc:
            print(f"check-coherence failed: {exc}. Nothing split; stored judgements are kept.",
                  file=sys.stderr)
            return 1
    finally:
        _flush_refresher(conn, refresher)
        conn.close()

    for f in result["findings"]:
        print(f"\n[{f['event'][:8]}] {(f['title'] or '')[:70]}  ({f['records']} records; victim {f['victim']})")
        for i, group in enumerate(f["groups"]):
            for s in group:
                score = "label" if s.get("basis") == "label" else f"{s['p_same']:.3f}"
                print(f"    - [{i + 1}] {score:>6}  {(s['org'] or '?')[:30]:30} {s['title'][:55]}")
        for s in f["held"]:
            score = "label" if s.get("label") else f"{s['p_same']:.3f}"
            print(f"    = held {score:>6}  {(s['org'] or '?')[:30]:30} {s['title'][:45]}  ({s['guard']})")
        if f["note"]:
            print(f"    note: {f['note']}")
    out = Path(args.db).parent / "dedup_coherence_findings.json"
    out.write_text(json.dumps({"generated": datetime.now().isoformat(), "apply": args.apply,
                               **{k: v for k, v in result.items() if k != "scores"}},
                              indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nJudged {result['members_judged']} suspect member(s) in {result['events_judged']} event(s) "
          f"({result['escalated']} escalated).")
    if not result["calibrated"]:
        print("REPORT ONLY for model-judged strays: no coherence calibration for this model and question "
              "(run scripts/calibrate_coherence.py); only labelled strays can be split.")
    verb = "Split out" if args.apply else "Would split out"
    print(f"{verb} {result['strays']} stray record(s) from {result['events_with_strays']} event(s)"
          + (f" at cut-off {result['cutoff']}" if result["cutoff"] is not None else "")
          + f"; findings in {out}")
    if result["undecided"]:
        print(f"check-coherence: {len(result['undecided'])} member(s) UNDECIDED - escalation failed; they "
              "were not acted on and will be retried next run.", file=sys.stderr)
        return 1
    return 0


def cmd_recalibrate(args) -> int:
    """Re-score the labelled cases on current evidence and recompute each stale cut-off.

    See :mod:`cyber_data_collector.dedup.recalibration`. A calibration is valid
    only for the model, question and *evidence processing* it was measured on;
    when a reader changes, its step is report-only until this has run. Labelled
    cases are scored on the labelling-time snapshot named in
    ``evaluation/calibration_snapshots.json`` (or ``--snapshot STEP=PATH``),
    using a kept copy under ``instance/recalibration/`` into which the live
    database's stored timelines and corroboration searches are carried. Spends
    Jev and escalation calls only for evidence not already judged.

    ``--status`` prints each calibration's state and spends nothing; ``--dry-run``
    also counts the labelled cases each snapshot holds. ``--if-stale`` limits the
    work to calibrations that exist but no longer match (what the refresh runs).
    Exit 2: a snapshot, key or label file is missing (nothing spent for that
    step); exit 1: a step failed (its old calibration is untouched).
    """
    from cyber_data_collector.dedup import recalibration as rc

    names = args.step or list(rc.SPECS)
    unknown = [n for n in names if n not in rc.SPECS]
    if unknown:
        print(f"recalibrate: unknown step(s) {', '.join(unknown)}; known: {', '.join(rc.SPECS)}", file=sys.stderr)
        return 2
    current = rc.states(names)
    print(f"{'step':30} {'state':12} {'cut-off':>9}  reason")
    for st in current:
        cutoff = "-" if st.get("cutoff") is None else f"{st['cutoff']:.4f}"
        print(f"{st['step']:30} {st['state']:12} {cutoff:>9}  {st['reason']}")
    if args.status:
        return 0
    if args.if_stale:
        todo = [st for st in current if st["state"] == "stale"]
    elif args.step:
        todo = current
    else:
        # A step never calibrated has no labels to re-score yet: name it, don't fail on it.
        todo = [st for st in current if st["state"] != "missing"]
        for st in current:
            if st["state"] == "missing":
                print(f"{st['step']}: never calibrated ({st['reason']}); label it, then --step {st['step']}")
    if not todo:
        print("Every calibration matches the current model, question and evidence processing.")
        return 0
    overrides = {}
    for item in args.snapshot or []:
        step, sep, value = item.partition("=")
        if not sep or step not in rc.SPECS:
            print(f"recalibrate: --snapshot wants STEP=PATH with a known step, got {item!r}", file=sys.stderr)
            return 2
        overrides[step] = value
    exit_code = 0
    for st in todo:
        spec = rc.SPECS[st["step"]]
        if not spec.automatic:
            print(f"\n{spec.name}: stale but not recalibrated here - run scripts/calibrate_supplier_victims.py "
                  "on a labelling-time snapshot (its --apply refuses until then)", file=sys.stderr)
            continue
        try:
            snapshot = rc.registered_snapshot(spec.name, Path(args.db), overrides)
            result = rc.recalibrate_step(spec.name, Path(args.db), snapshot, precision=args.precision,
                                         min_coverage=args.min_coverage, fresh=args.fresh, dry_run=args.dry_run)
        except rc.PrerequisiteMissing as exc:
            print(f"\n{spec.name}: cannot recalibrate - {exc}", file=sys.stderr)
            exit_code = max(exit_code, 2)
            continue
        except Exception as exc:  # noqa: BLE001 - one step failing must not hide the others
            logger.exception("recalibrate %s failed", spec.name)
            print(f"\n{spec.name}: FAILED - {exc}. Its calibration file is unchanged.", file=sys.stderr)
            exit_code = max(exit_code, 1)
            continue
        if result["dry_run"]:
            print(f"\n{spec.name}: would score {result['present_in_snapshot']} of {result['labelled']} labelled "
                  f"case(s) from {Path(result['snapshot']).name}; new evidence fingerprint {result['would_become']}")
        else:
            after = result["after"]
            print(f"\n{spec.name}: recalibrated on {result['cases_scored']} of {result['cases_labelled']} "
                  f"labelled case(s): cut-off {result['before'].get('cutoff')} -> {after.get('cutoff')}, "
                  f"state {after['state']}")
    return exit_code


def cmd_check_same_organisation(args) -> int:
    """Find separate incidents merged under one organisation; split them only when calibrated.

    See :mod:`cyber_data_collector.dedup.coherence_same_org`. A member naming the
    event's own organisation is a candidate only on evidence of a different
    incident (a verified date far from the event's, or a different incident in
    the organisation's researched timeline) - never a date gap by itself. Jev
    judges each candidate (the middle band escalated). With ``--apply``,
    candidates below the calibrated cut-off and members labelled 'different' are
    split out through the ledger; without a calibration matching the current
    models, questions and evidence processing only labelled members are split.
    Findings: ``dedup_coherence_same_org_findings.json`` beside the database.

    No fallback: a missing TypeSafe or OpenAI key (exit 2), a Jev failure, or an
    unreadable labels or calibration file (exit 1) stops the command before
    anything is split; a failed escalation leaves that member undecided (not
    acted on, exit 1).
    """
    from cyber_data_collector.dedup import coherence_same_org as same_org
    from cyber_data_collector.utils import jev

    missing = []
    if not os.getenv("TYPESAFE_API_KEY"):
        missing.append("TYPESAFE_API_KEY")
    elif not jev.available():
        missing.append("the typesafe-sdk package")
    if not os.getenv("OPENAI_API_KEY"):
        missing.append("OPENAI_API_KEY (escalation model)")
    if missing:
        print(f"check-same-organisation cannot run: {', '.join(missing)} missing. Nothing judged or split.",
              file=sys.stderr)
        return 2
    conn = _connect(args.db)
    refresher = _make_refresher(args)
    try:
        ledger = DedupLedger(conn, role_refresher=refresher)
        try:
            result = same_org.check_same_organisation(conn, apply=args.apply, only=args.event or None,
                                                      ledger=ledger)
        except (coherence.CoherenceJudgementError, coherence.CoherenceConfigError) as exc:
            print(f"check-same-organisation failed: {exc}. Nothing split; stored judgements are kept.",
                  file=sys.stderr)
            return 1
    finally:
        _flush_refresher(conn, refresher)
        conn.close()

    for f in result["findings"]:
        print(f"\n[{f['event'][:8]}] {(f['title'] or '')[:70]}  ({f['records']} records; victim {f['victim']})")
        for i, group in enumerate(f["groups"]):
            for s in group:
                score = "label" if s.get("basis") == "label" else f"{s['p_same']:.3f}"
                print(f"    - [{i + 1}] {score:>6}  {str(s.get('date') or '?'):10} {s['title'][:55]}")
        for s in f["held"]:
            score = "label" if s.get("label") else f"{s['p_same']:.3f}"
            print(f"    = held {score:>6}  {str(s.get('date') or '?'):10} {s['title'][:45]}  ({s['guard']})")
        if f["note"]:
            print(f"    note: {f['note']}")
    out = Path(args.db).parent / "dedup_coherence_same_org_findings.json"
    out.write_text(json.dumps({"generated": datetime.now().isoformat(), "apply": args.apply,
                               **{k: v for k, v in result.items() if k != "scores"}},
                              indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nJudged {result['candidates_judged']} same-organisation candidate(s) in {result['events_judged']} "
          f"event(s) ({result['escalated']} escalated).")
    if not result["calibrated"]:
        print("REPORT ONLY for model-judged candidates: no same-organisation calibration for this model, "
              "question and evidence (label the candidates, then run scripts/calibrate_coherence_same_org.py); "
              "only members labelled 'different' can be split.")
    verb = "Split out" if args.apply else "Would split out"
    print(f"{verb} {result['records_to_split']} record(s) from {result['events_with_splits']} event(s)"
          + (f" at cut-off {result['cutoff']}" if result["cutoff"] is not None else "")
          + f"; findings in {out}")
    if result["undecided"]:
        print(f"check-same-organisation: {len(result['undecided'])} member(s) UNDECIDED - escalation failed; "
              "they were not acted on and will be retried next run.", file=sys.stderr)
        return 1
    return 0


def cmd_resolve_label_contradictions(args) -> int:
    """Split labelled-different pairs that were merged into one event.

    See :mod:`cyber_data_collector.dedup.label_contradictions`. The quality gate's
    ``labels.duplicates.contradictions``: a pair labelled 'different' that now
    resolves to one Active event. The records of each labelled event (read from
    the database it was labelled on, ``--snapshot`` or the registry's
    'duplicates' entry) are separated again through the ledger: snapshotted,
    reversible, with 'different' rulings cached. No API calls. Exit 2 when the
    snapshot is needed and missing (the contradictions are still listed).
    """
    from cyber_data_collector.dedup import label_contradictions, recalibration

    conn = _connect(args.db)
    refresher = _make_refresher(args)
    try:
        found = label_contradictions.find(conn)
        if not found:
            print("No labelled-different pair is merged into one event.")
            return 0
        snapshot = None
        try:
            snapshot = recalibration.registered_snapshot(
                "duplicates", Path(args.db), {"duplicates": args.snapshot} if args.snapshot else None)
        except recalibration.PrerequisiteMissing as exc:
            print(f"resolve-label-contradictions: {exc}", file=sys.stderr)
        ledger = DedupLedger(conn, role_refresher=refresher)
        result = label_contradictions.resolve(conn, snapshot, apply=not args.dry_run, ledger=ledger)
    finally:
        _flush_refresher(conn, refresher)
        conn.close()
    unresolved = 0
    for c in result:
        print(f"\n{c.a[:8]} vs {c.b[:8]} (labelled different) are both in event {c.event[:8]}")
        if c.unresolved:
            unresolved += 1
            print(f"    UNRESOLVED: {c.unresolved}")
            continue
        print(f"    {len(c.side_a)} record(s) from {c.a[:8]}, {len(c.side_b)} from {c.b[:8]}; "
              f"{'would split' if args.dry_run else 'split'} out {len(c.leaving)}"
              + (f" as {c.new_event[:8]}" if c.new_event else ""))
    if args.dry_run:
        print("\nDRY RUN - nothing changed.")
    return 2 if snapshot is None else (1 if unresolved else 0)


def cmd_adjudicate_candidates(args) -> int:
    """Find missed duplicates by judging whole groups of candidates.

    Builds the multi-key candidate graph (shared article, rare shared subject,
    or a linked organisation backed by semantic similarity), takes its
    connected groups, and asks the model to partition each group into
    incidents. Groups at or above --min-certainty are merged into their
    best-sourced event; a merge contradicting a human 'different' override is
    skipped. Every proposal is written to instance/dedup_candidate_clusters.json.
    """
    from cyber_data_collector.dedup.adjudicator import EMBED_CANDIDATE_THRESHOLD
    from cyber_data_collector.dedup.candidates import candidate_components

    conn = _connect(args.db)
    refresher = _make_refresher(args)
    try:
        ledger = DedupLedger(conn, role_refresher=refresher)
        resolver = EntityResolver(conn)
        pair_judge = Adjudicator(resolver=resolver)
        records = load_dedup_records(conn)
        by_id = {r.enriched_event_id: r for r in records}
        pair_judge.candidate_pairs(records)
        pair_judge.embed_records(records)

        def strong(key, why) -> bool:
            if any(w.startswith(("shared source", "shared subject")) for w in why):
                return True
            left, right = sorted(key)
            sim = pair_judge.similarity(by_id[left], by_id[right])
            return sim is not None and sim >= EMBED_CANDIDATE_THRESHOLD

        def weight(key, why) -> float:
            # Shared article > shared subject > semantic similarity alone.
            left, right = sorted(key)
            sim = pair_judge.similarity(by_id[left], by_id[right]) or 0.0
            if any(w.startswith("shared source") for w in why):
                return 2.0 + sim
            if any(w.startswith("shared subject") for w in why):
                return 1.0 + sim
            return sim

        groups = candidate_components(pair_judge.pair_reasons, strong, weight=weight,
                                      max_size=args.max_cluster_size)
        if args.recent_days:
            recent = recent_event_ids(conn, args.recent_days)
            groups = [g for g in groups if recent & set(g)]
        if args.limit:
            groups = groups[: args.limit]
        print(f"{len(records)} active event(s) -> {len(groups)} candidate group(s) "
              f"covering {sum(len(g) for g in groups)} event(s)")

        sources = {r[0]: r[1] or 0 for r in conn.execute(
            "SELECT deduplicated_event_id, total_data_sources FROM DeduplicatedEvents")}
        masters = {r[0]: r[1] for r in conn.execute(
            "SELECT deduplicated_event_id, master_enriched_event_id FROM DeduplicatedEvents")}
        different = {key for key, same in ledger.load_overrides().items() if not same}

        judge = ClusterAdjudicator(resolver=resolver, max_cluster_size=args.max_cluster_size)
        findings: List[Dict] = []
        proposed = applied = vetoed = 0
        for index, ids in enumerate(groups, start=1):
            cluster = sorted((by_id[i] for i in ids), key=lambda r: r.event_date or "9999")
            partition = judge.adjudicate_cluster(cluster)
            for group, member_ids in partition.groups_as_ids(cluster):
                if len(member_ids) < 2:
                    continue
                ordered = sorted(member_ids, key=lambda i: (-sources.get(i, 0), by_id[i].event_date or "9999"))
                findings.append({
                    "label": group.label, "certainty": group.certainty,
                    "reasoning": group.reasoning, "target": ordered[0],
                    "members": [{"id": i, "title": by_id[i].title, "date": by_id[i].event_date}
                                for i in ordered],
                })
                proposed += len(ordered) - 1
                print(f"[{index}] {group.label[:70]} ({len(ordered)} events, "
                      f"certainty {group.certainty:.2f})")
                if args.verbose:
                    for i in ordered:
                        print(f"      - [{by_id[i].event_date or 'no date'}] {by_id[i].title[:70]}")
                if args.dry_run or group.certainty < args.min_certainty:
                    continue
                target = ordered[0]
                for source in ordered[1:]:
                    if frozenset((masters.get(target), masters.get(source))) in different:
                        vetoed += 1
                        continue
                    try:
                        ledger.merge_events(
                            target, source,
                            reason=f"[candidate cluster {group.certainty:.2f}] {group.label}: {group.reasoning}",
                            actor="pipeline")
                        applied += 1
                    except (sqlite3.Error, ValueError) as exc:
                        logger.warning("Candidate-cluster merge %s -> %s failed: %s", source, target, exc)
            conn.commit()

        CANDIDATE_CLUSTER_FINDINGS.parent.mkdir(parents=True, exist_ok=True)
        CANDIDATE_CLUSTER_FINDINGS.write_text(
            json.dumps(findings, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print(f"\n{proposed} merge(s) proposed; {applied} applied; {vetoed} vetoed by a "
              f"human 'different' override" + (" (DRY RUN)" if args.dry_run else
              f"; the rest were below certainty {args.min_certainty}"))
        print(f"cluster stats: {judge.stats}")
    finally:
        _flush_refresher(conn, refresher)
        conn.close()
    return 0


def cmd_retitle(args) -> int:
    """Give every deduplicated event the best available title.

    A merged event inherits its master record's headline, and master selection
    follows dedup mechanics rather than how well a headline describes the
    incident. The result was a 130-record Qantas breach called "Scattered
    Spider Ransomware Attacks" and a 43-record HWL Ebsworth breach called
    "Untitled Event".

    Candidates are the incident label from cluster adjudication (where one
    exists) plus every member's own title; each is scored and the best wins.
    This is only safe because identity now keys on the immutable
    master_enriched_event_id rather than on (title, event_date).
    """
    conn = _connect(args.db)
    try:
        schema.migrate(conn)
        ledger = DedupLedger(conn)
        generator = TitleGenerator() if args.generate else None
        unfixable: list = []

        rows = conn.execute("""
            SELECT d.deduplicated_event_id AS id, d.title, d.event_date,
                   d.victim_organization_name AS victim,
                   COUNT(m.enriched_event_id) AS members
            FROM DeduplicatedEvents d
            LEFT JOIN EventDeduplicationMap m
                   ON m.deduplicated_event_id = d.deduplicated_event_id
            WHERE COALESCE(d.status,'Active')='Active'
            GROUP BY d.deduplicated_event_id
            ORDER BY members DESC
        """).fetchall()

        changes = []
        for r in rows:
            if args.min_members and r["members"] < args.min_members:
                continue
            member_titles = [t[0] for t in conn.execute("""
                SELECT e.title FROM EventDeduplicationMap m
                JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
                WHERE m.deduplicated_event_id = ?
            """, (r["id"],))]
            label = ledger.incident_label_for(r["id"])
            new_title = derive_title(member_titles, victim=r["victim"],
                                     incident_label=label,
                                     current_title=r["title"])
            source = "label" if label else "member"

            # Selection can only pick the best of what exists. When every
            # candidate is a placeholder, a stored LLM paragraph or page
            # furniture, the incident has to be described from the evidence.
            reason = needs_regeneration(new_title)
            if reason and generator is not None:
                evidence = [e[0] for e in conn.execute("""
                    SELECT COALESCE(e.description, e.summary) FROM EventDeduplicationMap m
                    JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
                    WHERE m.deduplicated_event_id = ?
                      AND COALESCE(e.description, e.summary) IS NOT NULL
                    LIMIT 6
                """, (r["id"],))]
                got = generator.generate(evidence, event_date=str(r["event_date"] or ""),
                                         victim=r["victim"])
                if got and got.confidence >= 0.6 and not needs_regeneration(got.title):
                    new_title, source = got.title, f"generated/{reason}"
                elif reason:
                    unfixable.append((r["id"], r["title"], reason))
            elif reason:
                unfixable.append((r["id"], r["title"], reason))

            if new_title and new_title != r["title"]:
                changes.append((r["id"], r["title"], new_title, r["members"],
                                source))

        print(f"{len(rows)} active event(s); {len(changes)} would be retitled")
        if unfixable:
            print(f"{len(unfixable)} still unusable "
                  f"({'no evidence or low confidence' if args.generate else 'run with --generate'})")
        for _, old, new, members, tag in changes[:30]:
            print(f"  [{members:>3}] {str(old)[:44]!r}")
            print(f"        -> {str(new)[:60]!r}  ({tag})")

        if args.dry_run:
            print("\nDry run - nothing written.")
            return 0

        applied = 0
        for dedup_id, _, new_title, _, _ in changes:
            try:
                conn.execute(
                    "UPDATE DeduplicatedEvents SET title = ?, updated_at = "
                    "CURRENT_TIMESTAMP WHERE deduplicated_event_id = ?",
                    (new_title, dedup_id))
                applied += 1
            except sqlite3.Error as exc:
                logger.warning("Retitle failed for %s: %s", dedup_id, exc)
        conn.commit()
        print(f"\nRetitled {applied} event(s).")
    finally:
        conn.close()
    return 0


def _industry_for(conn: sqlite3.Connection, victim: str) -> Optional[str]:
    """Best industry for an organisation, or None when it cannot be justified.

    Sources, in order: the entity record, the industry other events about the
    same organisation agree on, then the entity type. Returning None is correct
    when none is available - an unjustified industry is worse than a blank one.
    """
    if not victim:
        return None

    row = conn.execute(
        "SELECT industry, entity_type FROM EntitiesV2 WHERE entity_name = ? LIMIT 1",
        (victim,)).fetchone()
    if row and row["industry"]:
        return row["industry"]

    siblings = conn.execute(
        """
        SELECT victim_organization_industry AS ind, COUNT(*) AS n
        FROM DeduplicatedEvents
        WHERE COALESCE(status,'Active')='Active'
          AND victim_organization_name LIKE ?
          AND victim_organization_industry IS NOT NULL
          AND victim_organization_industry != ''
        GROUP BY ind ORDER BY n DESC
        """,
        (f"%{victim[:28]}%",)).fetchall()
    if siblings and siblings[0]["n"] >= 2:
        return siblings[0]["ind"]

    if row and row["entity_type"]:
        from cyber_data_collector.utils.enum_values import industry_from_entity_type
        return industry_from_entity_type(row["entity_type"])
    return None


def cmd_revictim(args) -> int:
    """Re-derive each event's victim organisation from all of its members.

    The victim was copied from the master record's highest-confidence entity.
    Every entity link is tagged "affected" with no victim/regulator
    distinction, so that attributed the 106-record Optus breach to the
    Australian Cyber Security Centre and the global Canvas incident to a single
    university. Counting mentions across every member, and corroborating
    against the title, identifies the real victim.
    """
    conn = _connect(args.db)
    try:
        resolver = EntityResolver(conn)
        # Fit the resolver on the entity corpus. Unfitted, it blocks on the
        # first significant token, so "University of Technology Sydney" and
        # "University of Sydney" fold together on "university" - every
        # university in the database looks like one organisation, and a wrong
        # victim appears well-supported.
        resolver.fit([r[0] for r in conn.execute(
            "SELECT DISTINCT entity_name FROM EntitiesV2 WHERE entity_name IS NOT NULL")])

        # Fit "which names appear across unrelated events" from the corpus, so
        # bylines, regulators and news agencies are filtered by measurement
        # rather than by a hand-maintained list.
        per_event = {}
        for row in conn.execute("""
            SELECT m.deduplicated_event_id AS id, v.entity_name AS n
            FROM EventDeduplicationMap m
            JOIN EnrichedEventEntities ee ON ee.enriched_event_id = m.enriched_event_id
            JOIN EntitiesV2 v ON v.entity_id = ee.entity_id
        """):
            per_event.setdefault(row["id"], set()).add(row["n"])
        ubiquitous = ubiquitous_entities(per_event.values())

        rows = conn.execute("""
            SELECT d.deduplicated_event_id AS id, d.title,
                   d.victim_organization_name AS victim,
                   COUNT(m.enriched_event_id) AS members
            FROM DeduplicatedEvents d
            LEFT JOIN EventDeduplicationMap m
                   ON m.deduplicated_event_id = d.deduplicated_event_id
            WHERE COALESCE(d.status,'Active')='Active'
            GROUP BY d.deduplicated_event_id
            ORDER BY members DESC
        """).fetchall()

        changes = []
        for r in rows:
            if r["members"] < args.min_members:
                continue
            counts = [(x["n"], x["c"]) for x in conn.execute("""
                SELECT v.entity_name AS n, COUNT(*) AS c
                FROM EventDeduplicationMap m
                JOIN EnrichedEventEntities ee
                     ON ee.enriched_event_id = m.enriched_event_id
                JOIN EntitiesV2 v ON v.entity_id = ee.entity_id
                WHERE m.deduplicated_event_id = ?
                GROUP BY v.entity_name ORDER BY c DESC
            """, (r["id"],))]
            if not counts:
                continue
            attackers = {x[0] for x in conn.execute("""
                SELECT DISTINCT e.attacking_entity_name
                FROM EventDeduplicationMap m
                JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
                WHERE m.deduplicated_event_id = ?
                  AND e.attacking_entity_name IS NOT NULL
            """, (r["id"],)) if x[0]}

            new_victim = derive_victim(
                counts, r["members"], title=r["title"],
                known_attackers=attackers, current_victim=None,
                resolver=resolver, ubiquitous=ubiquitous,
            )
            if (r["victim"] or "").strip().lower() in ubiquitous:
                # e.g. a regulator or byline recorded as the victim.
                replace, why = (bool(new_victim),
                                f"current victim {r['victim']!r} appears across "
                                f"many unrelated events")
            else:
                replace, why = should_replace(
                    r["victim"], new_victim,
                    mention_share(r["victim"], counts, r["members"], resolver),
                    mention_share(new_victim, counts, r["members"], resolver),
                    known_attackers=attackers,
                )
            if replace and new_victim and new_victim != r["victim"]:
                changes.append((r["id"], r["victim"], new_victim, r["members"],
                                str(r["title"])[:44], why))

        filled = sum(1 for c in changes if not (c[1] or "").strip())
        corrected = len(changes) - filled
        print(f"{len(rows)} active event(s); {len(changes)} victim(s) would change "
              f"({filled} missing filled, {corrected} corrected)")
        for _, old, new, members, title, why in changes[:30]:
            shown = str(old)[:32] if (old or "").strip() else "(none)"
            print(f"  [{members:>3}] {shown!r} -> {str(new)[:34]!r}")
            print(f"        {title!r}  <- {why}")

        if args.dry_run:
            print("\nDry run - nothing written.")
            return 0

        for dedup_id, _, new_victim, _, _, _ in changes:
            # The industry describes the VICTIM, so it cannot survive a change
            # of victim. Carrying it over is exactly the "vendor tagged with
            # its clients' sector" defect check_data_integrity guards against:
            # leaving it behind tagged ProctorU, a technology vendor, with
            # "Education" - its universities' sector.
            industry = _industry_for(conn, new_victim)
            conn.execute(
                "UPDATE DeduplicatedEvents SET victim_organization_name = ?, "
                "victim_organization_industry = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE deduplicated_event_id = ?",
                (new_victim, industry, dedup_id))
        conn.commit()
        print(f"\nUpdated {len(changes)} victim organisation(s).")
    finally:
        conn.close()
    return 0


def cmd_classify_entities(args) -> int:
    """Label every entity with what it IS and what part it played.

    All 23,388 entity links carry relationship_type='affected', so the column
    conveys nothing and the victim had to be guessed from mention counts. That
    cannot separate the breached company from the software it was breached
    through, which is how the global Canvas incident came to be attributed to
    "Canvas Learning Management System" - a product, not an organisation.

    Writes the per-event role to EnrichedEventEntities.relationship_type and
    caches the invariant kind on EntitiesV2.entity_kind.
    """
    conn = _connect(args.db)
    try:
        schema.migrate(conn)
        classifier = EntityRoleClassifier()

        rows = conn.execute("""
            SELECT d.deduplicated_event_id AS id, d.title, d.summary,
                   COUNT(m.enriched_event_id) AS members
            FROM DeduplicatedEvents d
            JOIN EventDeduplicationMap m
                 ON m.deduplicated_event_id = d.deduplicated_event_id
            WHERE COALESCE(d.status,'Active')='Active'
            GROUP BY d.deduplicated_event_id
            ORDER BY members DESC
        """).fetchall()
        # Filter before limiting, so --entity with --limit selects matching
        # events rather than the largest ones that happen to match.
        if args.entity:
            needle = args.entity.lower()
            rows = [r for r in rows if needle in (r["title"] or "").lower()]
        if args.stale_only:
            stale = set(stale_event_ids(conn))
            rows = [r for r in rows if r["id"] in stale]
        if args.limit:
            rows = rows[: args.limit]

        report = staleness_report(conn)
        print(f"role status: {report['current']} current, {report['outdated']} "
              f"outdated, {report['never_classified']} never classified")
        print(f"classifying entities for {len(rows)} event(s)")
        victims = vendors = labelled = failed = 0

        for index, r in enumerate(rows, start=1):
            links = conn.execute("""
                SELECT DISTINCT ee.enriched_event_id AS eid, ee.entity_id AS entid,
                       v.entity_name AS name
                FROM EventDeduplicationMap m
                JOIN EnrichedEventEntities ee
                     ON ee.enriched_event_id = m.enriched_event_id
                JOIN EntitiesV2 v ON v.entity_id = ee.entity_id
                WHERE m.deduplicated_event_id = ?
            """, (r["id"],)).fetchall()
            if not links:
                # Nothing to classify, so the (empty) roles trivially match the
                # membership. Marking it keeps the event out of the stale queue
                # instead of being re-selected on every run for ever.
                if not args.dry_run:
                    mark_classified(conn, r["id"])
                    conn.commit()
                continue

            # Rank before truncating. Sorting alphabetically and cutting at
            # max_entities dropped "Instructure" and "University of Sydney"
            # from the Canvas event - everything after "Flinders University" -
            # so the classifier never saw the victim or the vendor.
            counts: Dict[str, int] = {}
            for l in links:
                if l["name"]:
                    counts[l["name"]] = counts.get(l["name"], 0) + 1
            title_lower = (r["title"] or "").lower()
            names = sorted(
                counts,
                key=lambda n: (n.lower() in title_lower, counts[n], -len(n)),
                reverse=True,
            )[: args.max_entities]

            try:
                result = classifier.classify(r["title"] or "", r["summary"], names)
            except ClassificationFailed as exc:
                failed += 1
                print(f"  FAILED {r['id'][:8]} {(r['title'] or '')[:50]}: {exc}")
                continue      # nothing written: still stale, retried next run
            if not result.assignments:
                continue

            by_name = {a.name.strip().lower(): a for a in result.assignments}
            for link in links:
                a = by_name.get((link["name"] or "").strip().lower())
                if not a:
                    continue
                labelled += 1
                if not args.dry_run:
                    conn.execute(
                        "UPDATE EnrichedEventEntities SET relationship_type = ? "
                        "WHERE enriched_event_id = ? AND entity_id = ?",
                        (a.role.value, link["eid"], link["entid"]))
                    conn.execute(
                        "UPDATE EntitiesV2 SET entity_kind = ? WHERE entity_id = ?",
                        (a.kind.value, link["entid"]))

            # Shared with the pipeline. This block used to be a second copy,
            # so corrections applied in entity_classification.py had no effect
            # when this command was the caller - "Cisco" survived two fixed
            # reruns because of it.
            victim, vendor = resolve_victim(conn, r["id"], result,
                                            dry_run=args.dry_run)
            if victim:
                victims += 1
            if vendor:
                vendors += 1
            if args.show_roles or index <= 8:
                print(f"  [{r['members']:>3}] {str(r['title'])[:48]!r}")
                print(f"        victim={victim!r} vendor={vendor!r}")
                if args.show_roles:
                    for a in result.assignments:
                        print(f"          {a.role.value:18} {a.kind.value:16} {a.name[:38]}")

            if not args.dry_run:
                # Roles now match this event's membership; a later merge or
                # split will change the signature and mark it stale again.
                mark_classified(conn, r["id"])
                conn.commit()

        print(f"\n{'DRY RUN: ' if args.dry_run else ''}labelled {labelled} link(s); "
              f"{victims} event(s) with a victim, {vendors} with a vendor")
        print(f"classifier stats: {classifier.stats}")
        if failed:
            print(f"{failed} event(s) not classified (model call failed); they stay stale and are retried")
    finally:
        conn.close()
    return 1 if failed else 0



def cmd_canonicalise_entities(args) -> int:
    """Collapse duplicate spellings of one organisation into a single entity.

    Blocking keys candidate pairs on the entity, so "Australian Securities and
    Investment Commission" and "Australian Securities and Investments
    Commission" were never compared - which is how three ASIC events with
    byte-identical titles, dates and descriptions all survived deduplication.
    Fixing the entities is a precondition for fixing those merges.

    The reviewed aliases in ``EXPLICIT_ALIASES`` fold directly. Every fold the
    name-structure rules propose is put to Perplexity first and folds only on
    a ``same organisation`` verdict (:mod:`entity_alias_verification`); the
    structure rules alone would have folded Marks & Spencer into Mackay Sugar.
    A confirmed acronym ("AV" for Ambulance Victoria) is not folded: it becomes
    a contextual alias and each link is placed by its own record's text
    (:mod:`contextual_aliases`), which also puts back links an earlier
    entity-wide fold moved without that context.
    Exit 2 without Perplexity or Jev, 1 when a pair or a mention could not be
    decided (left as it was).
    """
    from cyber_data_collector.dedup.contextual_aliases import (
        MentionResolver, add_contextual_alias, is_contextual_variant, migrate_contextual_aliases, resolve_links,
    )
    from cyber_data_collector.dedup.entity_alias_verification import (
        AliasVerificationUnavailable, VerificationReport, verified_groups, verify_stored_aliases,
    )
    from cyber_data_collector.utils.jev import JevUnavailable

    from cyber_data_collector.dedup.entity_merge import EXPLICIT_ALIASES

    conn = _connect(args.db)
    try:
        schema.migrate(conn)
        resolver = EntityResolver()
        # Acronym aliases leave the global table before anything folds on them.
        scoped_now = [] if args.dry_run else migrate_contextual_aliases(conn)
        conn.commit()
        sources = dict(conn.execute("SELECT raw_name, source FROM EntityAliases").fetchall())

        def fold(groups, source: str) -> int:
            folded = 0
            for keep, drop in groups:
                # Reviewed aliases hold everywhere; a model-confirmed acronym only in context.
                reviewed = {n for n in drop if EXPLICIT_ALIASES.get(n) == keep}
                scoped = [n for n in drop if n not in reviewed and is_contextual_variant(n, keep)]
                drop = [n for n in drop if n not in scoped]
                print(f"  {keep!r}")
                for name in drop:
                    print(f"      <- {name!r}")
                for name in scoped:
                    print(f"      <- {name!r} (acronym: only where the record's text says so)")
                if args.dry_run:
                    continue
                if drop:
                    folded += merge_entities(conn, keep, drop)
                    inherit_attributes(conn, keep)
                for name in drop:
                    # Remembered, so a future ingest resolves the variant instead of recreating it.
                    # Only EXPLICIT_ALIASES pairs are "human": a stored model-confirmed alias
                    # folded here keeps the source it had.
                    learned = "human" if name in reviewed else sources.get(name, source)
                    resolver.learn_alias(conn, name, keep, source=learned)
                for name in scoped:
                    add_contextual_alias(conn, name, keep, source)
            conn.commit()
            return folded

        explicit = canonical_groups(conn, explicit_only=True)
        print(f"{len(explicit)} organisation(s) with reviewed aliases (EXPLICIT_ALIASES):")
        merged = fold(explicit, "human")
        report = VerificationReport()
        if not args.explicit_only:
            proposed = canonical_groups(conn, include_ambiguous_acronyms=True)   # every fold verified below
            proposed = proposed[: args.limit] if args.limit else proposed
            print(f"\n{len(proposed)} group(s) proposed by name structure, {sum(len(d) for _, d in proposed)} "
                  "fold(s) - each confirmed by Perplexity before it is applied:")
            if args.dry_run:
                for keep, drop in proposed:
                    print(f"  {keep!r} <- {', '.join(repr(d) for d in drop)}")
            else:
                try:
                    stored = verify_stored_aliases(conn, report=report)
                    print(f"Stored rule-made aliases: {stored.checked} checked, {stored.spelling_only} spelling "
                          f"variants kept, {len(stored.confirmed)} confirmed, {len(stored.removed)} removed")
                    for raw, canonical, verdict in stored.removed:
                        print(f"  removed alias {raw!r} -> {canonical!r} ({verdict}); its earlier fold needs review")
                    confirmed = verified_groups(conn, proposed, report=report)
                except AliasVerificationUnavailable as exc:
                    print(f"ERROR: proposed folds cannot be verified: {exc}; none applied", file=sys.stderr)
                    return 2
                merged += fold(confirmed, "llm")
                for name, other, verdict in report.rejected:
                    print(f"  not folded: {name!r} / {other!r} ({verdict})")
                print(f"Perplexity: {report.asked} pair(s) asked, {report.reused} from stored verdicts; "
                      f"{len(report.accepted)} confirmed, {len(report.rejected)} rejected, "
                      f"{len(report.failed)} undecided")
        if args.dry_run:
            print("\nDry run - nothing written, nothing asked.")
            return 0
        print(f"\nFolded {merged} duplicate spelling(s) into their canonical entity.")
        scoped = scoped_now
        if scoped:
            print(f"{len(scoped)} acronym alias(es) now apply only in context: "
                  + ", ".join(f"{raw} -> {canonical}" for raw, canonical in scoped))
        try:
            links = resolve_links(conn, MentionResolver(conn))
        except JevUnavailable as exc:
            print(f"ERROR: acronym mentions cannot be placed: {exc}", file=sys.stderr)
            return 2
        print("Contextual aliases: " + links.summary())
        for table, subject, old, new in links.moved_to_variant:
            print(f"  {subject[:8]} ({table}): {old!r} -> {new!r}")
        for table, subject, old, new in links.moved_to_organisation:
            print(f"  {subject[:8]} ({table}): {old!r} -> {new!r}")
    finally:
        conn.close()
    if report.failed:
        print(f"ERROR: {len(report.failed)} proposed fold(s) could not be decided and were not applied; "
              "rerun to retry", file=sys.stderr)
    if links.failed:
        print(f"ERROR: {len(links.failed)} acronym mention(s) could not be decided (escalation failed) "
              "and were left where they were; rerun to retry", file=sys.stderr)
    return 1 if report.failed or links.failed else 0


def cmd_redate(args) -> int:
    """Repair event dates from member consensus, then from the description.

    Two failures, both of which cascade into deduplication because the
    adjudicator treats a date gap as evidence of separate incidents:

    * the merge rule took the *earliest* member date, so one bad extraction
      captured the event - MediSecure's 77 records, 64 of them April 2024, was
      stored as March 2019;
    * 160 events had no date at all while their own description said "On
      2026-07-11..." or "identified in April 2023".
    """
    conn = _connect(args.db)
    try:
        schema.migrate(conn)
        rows = conn.execute("""
            SELECT d.deduplicated_event_id AS id, d.title, d.event_date AS stored
            FROM DeduplicatedEvents d
            WHERE COALESCE(d.status,'Active') = 'Active'
        """).fetchall()

        researcher = DateResearcher() if args.research else None
        consensus, recovered, researched, published = [], [], [], []
        unresolved = 0
        for r in rows:
            members = [x[0] for x in conn.execute("""
                SELECT e.event_date FROM EventDeduplicationMap m
                JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
                WHERE m.deduplicated_event_id = ?""", (r["id"],))]
            best = derive_event_date(members, current=r["stored"], title=r["title"])

            if not best:
                # Nothing dated upstream: read the incident description, which
                # routinely states the date in prose.
                for text in conn.execute("""
                    SELECT COALESCE(e.description, e.summary) FROM EventDeduplicationMap m
                    JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
                    WHERE m.deduplicated_event_id = ?
                      AND COALESCE(e.description, e.summary) IS NOT NULL""", (r["id"],)):
                    got = extract_date(text[0])
                    if got:
                        recovered.append((r["id"], r["title"], got, text[0][:70]))
                        best = got
                        break
                if not best and researcher is not None:
                    # Nothing in the stored text dates it. Search for the
                    # incident: these are precisely the events that cannot be
                    # deduplicated, because a missing date reads as evidence of
                    # a different incident.
                    entity = conn.execute(
                        "SELECT victim_organization_name FROM DeduplicatedEvents "
                        "WHERE deduplicated_event_id = ?", (r["id"],)).fetchone()
                    desc = conn.execute("""
                        SELECT COALESCE(e.description, e.summary)
                        FROM EventDeduplicationMap m
                        JOIN EnrichedEvents e
                             ON e.enriched_event_id = m.enriched_event_id
                        WHERE m.deduplicated_event_id = ?
                          AND COALESCE(e.description, e.summary) IS NOT NULL
                        LIMIT 1""", (r["id"],)).fetchone()
                    got = researcher.resolve(r["title"] or "",
                                             desc[0] if desc else None,
                                             entity[0] if entity else None)
                    if got and got.confidence >= 0.6:
                        researched.append((r["id"], r["title"], got.event_date,
                                           got.confidence, got.basis))
                        best = got.event_date

                    if not best:
                        # Second tier: the month is usually recoverable even
                        # when the day is not, and month granularity is how
                        # the pipeline already represents such incidents.
                        got = researcher.resolve_month(
                            r["title"] or "", desc[0] if desc else None,
                            entity[0] if entity else None)
                        if got and got.confidence >= 0.6:
                            researched.append((r["id"], r["title"], got.event_date,
                                               got.confidence,
                                               "month only: " + got.basis))
                            best = got.event_date

                    if not best:
                        # A date the raw record carried but enrichment dropped.
                        # Police Bank's RawEvents row says 2024-12-01 while its
                        # enriched row says nothing, so consensus never saw it.
                        row = conn.execute("""
                            SELECT MIN(rw.event_date) FROM EventDeduplicationMap m
                            JOIN EnrichedEvents e
                                 ON e.enriched_event_id = m.enriched_event_id
                            JOIN RawEvents rw ON rw.raw_event_id = e.raw_event_id
                            WHERE m.deduplicated_event_id = ?
                              AND rw.event_date IS NOT NULL""", (r["id"],)).fetchone()
                        if row and row[0]:
                            best = str(row[0])[:10]
                            published.append((r["id"], r["title"], best,
                                              "raw record", ""))

                    if not best:
                        # Last tier: the article's own publication date. It
                        # postdates the incident, but an approximate date still
                        # lets the event be compared - a blank one cannot be.
                        for url in conn.execute("""
                            SELECT rw.source_url FROM EventDeduplicationMap m
                            JOIN EnrichedEvents e
                                 ON e.enriched_event_id = m.enriched_event_id
                            JOIN RawEvents rw ON rw.raw_event_id = e.raw_event_id
                            WHERE m.deduplicated_event_id = ?
                              AND rw.source_url IS NOT NULL""", (r["id"],)):
                            from_url = date_from_url(url[0])
                            if from_url:
                                published.append((r["id"], r["title"], from_url,
                                                  "URL", url[0]))
                                best = from_url
                                break
                    if not best:
                        row = conn.execute("""
                            SELECT MIN(rw.discovered_at) FROM EventDeduplicationMap m
                            JOIN EnrichedEvents e
                                 ON e.enriched_event_id = m.enriched_event_id
                            JOIN RawEvents rw ON rw.raw_event_id = e.raw_event_id
                            WHERE m.deduplicated_event_id = ?
                              AND rw.discovered_at IS NOT NULL""", (r["id"],)).fetchone()
                        if row and row[0]:
                            best = str(row[0])[:10]
                            published.append((r["id"], r["title"], best,
                                              "discovered", ""))
                if not best:
                    unresolved += 1
                    continue
            elif best != (str(r["stored"])[:10] if r["stored"] else None):
                agree, total = disagreement(members)
                consensus.append((r["id"], r["title"], r["stored"], best, agree, total))
            else:
                continue

            if not args.dry_run:
                conn.execute("UPDATE DeduplicatedEvents SET event_date = ?, "
                             "updated_at = CURRENT_TIMESTAMP "
                             "WHERE deduplicated_event_id = ?", (best, r["id"]))

        print(f"{len(consensus)} date(s) corrected by member consensus:")
        for _, title, old, new, agree, total in consensus[:20]:
            print(f"   {str(old)[:10]:<11} -> {new}  ({agree}/{total} records)  "
                  f"{str(title)[:44]}")
        print(f"\n{len(recovered)} date(s) recovered from the description:")
        for _, title, new, snippet in recovered[:20]:
            print(f"   {new}  {str(title)[:38]:<40} {snippet!r}")
        print(f"\n{len(researched)} date(s) established by search:")
        for _, title, new, conf, basis in researched[:25]:
            print(f"   {new}  ({conf:.2f}) {str(title)[:34]:<36} {basis[:44]}")
        print(f"{len(published)} date(s) taken from the article itself:")
        for _, title, new, how, src in published[:25]:
            print(f"   {new}  ({how:<10}) {str(title)[:34]:<36} {str(src)[:36]}")
        print(f"\n{unresolved} event(s) still undated")

        if args.dry_run:
            print("Dry run - nothing written.")
            return 0
        conn.commit()
    finally:
        conn.close()
    return 0


def cmd_recount(args) -> int:
    """Re-derive records_affected from each event's own source records."""
    conn = _connect(args.db)
    try:
        schema.migrate(conn)
        report = repair.recount(conn, dry_run=args.dry_run)

        print(f"{len(report['corrected'])} corrected to a figure a source record states:")
        for victim, title, old, new, reason in report["corrected"][:25]:
            print(f"   {str(old):>12} -> {new:<12,} {str(victim)[:18]:<20} "
                  f"{str(title)[:28]:<30} {reason}")
        print(f"{len(report['cleared'])} cleared as wholly unsupported:")
        for victim, title, old, _, _ in report["cleared"][:25]:
            print(f"   {old:>12,} -> (none)      {str(victim)[:18]:<20} {str(title)[:38]}")
        print(f"{len(report['flagged'])} left for review "
              f"(source records disagree by more than 10x):")
        for title, rec, members in report["flagged"][:15]:
            print(f"   {rec:>12,}  records={str(members)[:38]:<40} {str(title)[:28]}")

        if args.dry_run:
            print("Dry run - nothing written.")
    finally:
        conn.close()
    return 0


def cmd_name_entities(args) -> int:
    """Replace descriptions used as entity names with the organisation itself."""
    conn = _connect(args.db)
    try:
        schema.migrate(conn)
        report = repair.name_entities(
            conn, use_llm=not args.no_llm, search=not args.no_search,
            dry_run=args.dry_run)

        print(f"{report['examined']} descriptive phrase(s) recorded as the victim")
        print(f"{len(report['from_sibling'])} named from another entity on the same event:")
        for old, new, title in report["from_sibling"][:25]:
            print(f"   {str(old)[:32]!r:34} -> {new!r:28} {str(title)[:24]}")
        print(f"{len(report['from_text'])} named from the report or a search:")
        for old, new, conf, basis in report["from_text"][:25]:
            print(f"   {str(old)[:32]!r:34} -> {new!r:28} ({conf:.2f}) {str(basis)[:26]}")
        print(f"{len(report['unnamed'])} never publicly named (phrase demoted):")
        for old, title in report["unnamed"][:25]:
            print(f"   {str(old)[:32]!r:34}    {str(title)[:40]}")

        if args.dry_run:
            print("Dry run - nothing written.")
    finally:
        conn.close()
    return 0


def cmd_set_fact(args) -> int:
    """Record an explicit correction to an event fact; it outranks recomputation."""
    from cyber_data_collector.dedup.event_facts import set_fact
    conn = _connect(args.db)
    try:
        set_fact(conn, args.dedup_id, args.field, None if args.value.lower() == "null" else args.value,
                 args.reason)
        conn.commit()
    finally:
        conn.close()
    print(f"{args.dedup_id}: {args.field} = {args.value} (recorded; recomputation will keep it)")
    return 0


def cmd_refresh_facts(args) -> int:
    """Recompute facts for events whose membership changed since their facts were computed."""
    from cyber_data_collector.dedup.event_facts import refresh_facts
    conn = _connect(args.db)
    try:
        changed = refresh_facts(conn, dry_run=args.dry_run)
        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()
    for dedup_id, delta in list(changed.items())[:25]:
        print(f"  {dedup_id[:8]} " + "; ".join(f"{f}: {old} -> {new}" for f, (old, new) in delta.items()))
    print(f"{'Would change' if args.dry_run else 'Changed'} facts on {len(changed)} event(s)")
    return 0


def cmd_roles_status(args) -> int:
    """Report which events have entity roles matching their current members."""
    conn = _connect(args.db)
    try:
        schema.migrate(conn)
        if args.invalidate_all:
            n = invalidate_all(conn)
            conn.commit()
            print(f"marked {n} event(s) stale; re-run classify-entities --stale-only")
            return 0

        report = staleness_report(conn)
        print(f"active events        : {report['active']}")
        print(f"  roles current      : {report['current']}")
        print(f"  roles outdated     : {report['outdated']}  (membership changed since)")
        print(f"  never classified   : {report['never_classified']}")

        stale = stale_event_ids(conn, include_unclassified=False)
        if stale:
            print(f"\noutdated events (membership changed after classification):")
            for dedup_id in stale[: args.limit or 20]:
                row = conn.execute(
                    "SELECT title, (SELECT COUNT(*) FROM EventDeduplicationMap m "
                    " WHERE m.deduplicated_event_id = d.deduplicated_event_id) AS n "
                    "FROM DeduplicatedEvents d WHERE deduplicated_event_id = ?",
                    (dedup_id,)).fetchone()
                if row:
                    print(f"   [{row['n']:>3} members] {str(row['title'])[:58]!r}")
        if report["outdated"] or report["never_classified"]:
            print("\nrun: python scripts/dedup_admin.py classify-entities --stale-only")
    finally:
        conn.close()
    return 0


def cmd_split(args) -> int:
    conn = _connect(args.db)
    refresher = _make_refresher(args)
    try:
        ledger = DedupLedger(conn, role_refresher=refresher)
        ids = args.enriched_id
        if len(ids) == 1:
            new_id = ledger.split_member(args.dedup_id, ids[0], args.reason, actor="human")
        else:
            new_id = ledger.split_members(args.dedup_id, ids, args.reason, actor="human")
        conn.commit()
        print(f"Split {' '.join(ids)} out into new event {new_id}")
    finally:
        _flush_refresher(conn, refresher)
        conn.close()
    return 0


def cmd_override(args) -> int:
    conn = _connect(args.db)
    refresher = _make_refresher(args)
    try:
        ledger = DedupLedger(conn, role_refresher=refresher)
        ledger.add_override(
            args.left, args.right,
            OverrideVerdict.SAME if args.verdict == "same" else OverrideVerdict.DIFFERENT,
            reason=args.reason,
        )
        conn.commit()
        print(f"Override recorded: {args.left} / {args.right} -> {args.verdict}")
    finally:
        _flush_refresher(conn, refresher)
        conn.close()
    return 0


def cmd_merge(args) -> int:
    """Fold one deduplicated event into another, as a human ruling.

    ``override ... same`` only records a verdict; nothing moves until a later
    adjudication pass happens to consult it. This applies the merge now and
    also records the override on the two events' master records, so the
    ruling survives a future rebuild (dedup ids are regenerated; enriched ids
    are stable).
    """
    conn = _connect(args.db)
    refresher = _make_refresher(args)
    try:
        masters = {
            row["deduplicated_event_id"]: row
            for row in conn.execute(
                "SELECT deduplicated_event_id, master_enriched_event_id, status, title "
                "FROM DeduplicatedEvents WHERE deduplicated_event_id IN (?, ?)",
                (args.target, args.source),
            )
        }
        for dedup_id in (args.target, args.source):
            row = masters.get(dedup_id)
            if row is None:
                print(f"No such deduplicated event: {dedup_id}")
                return 1
            if row["status"] != "Active":
                print(f"Event {dedup_id} is {row['status']}, not Active - refusing to merge")
                return 1

        target, source = masters[args.target], masters[args.source]
        print(f"Merge: {source['title']!r}\n Into: {target['title']!r}")
        if args.dry_run:
            print("DRY RUN - re-run without --dry-run to apply.")
            return 0

        ledger = DedupLedger(conn, role_refresher=refresher)
        ledger.add_override(
            target["master_enriched_event_id"], source["master_enriched_event_id"],
            OverrideVerdict.SAME, reason=args.reason,
        )
        conflict = ledger.override_conflict(args.target, args.source)
        if conflict and not args.force:
            print(f"Refusing: a 'different' ruling exists ({conflict}). "
                  "Re-run with --force to overrule it.")
            return 1
        try:
            ledger.merge_events(args.target, args.source, args.reason, actor="human",
                                respect_overrides=not args.force)
        except ValueError as exc:
            conn.rollback()
            print(f"Refusing: {exc}")
            return 1
        conn.commit()
        print(f"Merged {args.source} into {args.target} (override recorded)")
    finally:
        _flush_refresher(conn, refresher)
        conn.close()
    return 0


def cmd_learn(args) -> int:
    conn = _connect(args.db)
    try:
        ledger = DedupLedger(conn)
        result = ledger.learn_from_overrides()
        conn.commit()
        for key, value in result.items():
            print(f"  {key:24} {value}")
    finally:
        conn.close()
    return 0


def cmd_ancestry(args) -> int:
    conn = _connect(args.db)
    try:
        tree = DedupLedger(conn).ancestry(args.dedup_id)
        if not tree:
            print(f"No such deduplicated event: {args.dedup_id}")
            return 1
        print(json.dumps(tree, indent=2, ensure_ascii=False, default=str))
    finally:
        conn.close()
    return 0


def cmd_classify_pages(args) -> int:
    """Classify suspect source pages (roundups, guidance, profiles, indexes).

    Only records matching a roundup/social title or URL pattern are sent to the
    model, and each verdict is stored, so re-runs only pay for new suspects.
    Run ``reject-non-incidents`` afterwards to act on the verdicts.
    """
    conn = _connect(args.db)
    try:
        suspects = page_classifier.pending_suspects(conn, only_unmapped=args.unmapped_only)
        if args.limit:
            suspects = suspects[: args.limit]
        print(f"{len(suspects)} unclassified suspect record(s)")
        verdicts = page_classifier.classify_pages(conn, suspects, dry_run=args.dry_run)
        counts: Dict[str, int] = {}
        for eid, title, *_ in suspects:
            v = verdicts.get(eid)
            if v is None:
                continue
            counts[v.kind] = counts.get(v.kind, 0) + 1
            if args.verbose or v.kind != "specific_incident":
                print(f"  {v.kind:22} {v.confidence:.2f}  {title[:70]}")
        print(("DRY RUN: " if args.dry_run else "") + f"verdicts: {counts}")
    finally:
        conn.close()
    return 0


def cmd_reject_non_incidents(args) -> int:
    """Reject events built only from pages that were never cyber incidents.

    See :mod:`cyber_data_collector.dedup.non_incident` for the gate. Each
    rejection is snapshotted, and new records meeting the gate are kept out of
    dedup automatically by run_global_deduplication.py.
    """
    conn = _connect(args.db)
    try:
        found = reject_non_incident_events(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    if not found:
        print("No non-incident events found.")
        return 0
    for event in found:
        print(f"  {event['event_date'] or '----------'}  {event['deduplicated_event_id']}  "
              f"{event['title']}  ({len(event['member_ids'])} record(s))")
    verb = "Would reject" if args.dry_run else "Rejected"
    print(f"{verb} {len(found)} event(s).")
    return 0


def _escalation_failures(result: Dict[str, Any]) -> int:
    """Non-zero when any escalation failed: those items were left undecided."""
    failed = result.get("escalation_failed") or []
    if failed:
        print(f"{len(failed)} escalation(s) FAILED; those items were left undecided and are retried next run")
        return 1
    return 0


def cmd_check_events(args) -> int:
    """Judge every active event with Jev; reject below the calibrated cut-off.

    See :mod:`cyber_data_collector.dedup.event_reality`. Findings are written
    to ``instance/event_reality_findings.json``.
    """
    from cyber_data_collector.dedup.event_reality import check_events
    from cyber_data_collector.utils import jev

    jev.require()   # fails loudly; never skip or switch model
    conn = _connect(args.db)
    try:
        result = check_events(conn, apply=args.apply)
    finally:
        conn.close()
    out = Path(args.db).parent / "event_reality_findings.json"
    out.write_text(json.dumps({"generated": datetime.now().isoformat(), **result}, indent=2, default=str),
                   encoding="utf-8")
    if not result["calibrated"]:
        print(f"Judged {result['judged']} event(s). No calibration for this model and question: "
              "nothing rejected. Run scripts/calibrate_event_reality.py.")
        return 0
    for f in result["rejections"]:
        print(f"  {f['p_real']:.3f}  {f['deduplicated_event_id'][:8]}  {(f['title'] or '')[:80]}")
    verb = "Rejected" if result["applied"] else "Would reject"
    print(f"{verb} {len(result['rejections'])} of {result['judged']} event(s) "
          f"below cut-off {result['cutoff']}; findings in {out}")
    return _escalation_failures(result)


def cmd_check_sources(args) -> int:
    """Google source gate; see cyber_data_collector.dedup.source_gate."""
    from cyber_data_collector.dedup.source_gate import check_sources
    from cyber_data_collector.utils import jev

    jev.require()   # fails loudly; never skip or switch model
    conn = _connect(args.db)
    try:
        result = check_sources(conn, apply=args.apply, only_unmapped=args.unmapped_only)
    finally:
        conn.close()
    out = Path(args.db).parent / "source_gate_findings.json"
    out.write_text(json.dumps({"generated": datetime.now().isoformat(), **result}, indent=2, default=str),
                   encoding="utf-8")
    if not result["calibrated"]:
        print(f"Judged {result['judged']} of {result['records']} Google record(s). No calibration for this "
              "model and question: nothing removed. Run scripts/calibrate_source_gate.py.")
        return 0
    verb = "Removed" if args.apply else "Would remove"
    print(f"{verb} {len(result['below_cutoff'] if not args.apply else result['removed'])} of {result['judged']} "
          f"Google record(s) below cut-off {result['cutoff']}; {len(result['replacements_queued'])} replacement "
          f"source(s) queued; findings in {out}")
    return 0


def cmd_label_evidence(args) -> int:
    """Queue and attach the reporting labellers cited for evidence-less events; see dedup.label_evidence."""
    from cyber_data_collector.dedup import label_evidence

    conn = _connect(args.db)
    try:
        result = label_evidence.run(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    prefix = "[dry run] " if args.dry_run else ""
    print(f"{prefix}{len(result['queued'])} cited report(s) queued; "
          f"{sum(1 for a in result['attached'] if a['action'] == 'attached')} of {len(result['attached'])} "
          "queued report(s) attached to their labelled event")
    detail = label_evidence.summary(result)
    if detail:
        print(detail)
    blocked = [a for a in result["attached"] if a.get("blocked")]
    return 1 if blocked else 0


def cmd_recover_evidence(args) -> int:
    """Diagnose incidents with no usable evidence, queue retrieval and mark evidence status; see evidence_recovery.

    Exit 2 when a replacement search was needed and could not run
    (no PERPLEXITY_API_KEY); the other retrieval and the statuses still happen.
    With --until-empty it also runs the retrieval (Cyber Daily re-fetch, page re-fetch, Perplexity research,
    replacement search) round after round; see :func:`_drain_evidence`.
    """
    from cyber_data_collector.processing import evidence_recovery

    if args.api_budget:
        from cyber_data_collector.utils.api_budget import configure as configure_api_budget
        configure_api_budget(args.api_budget)
    if args.until_empty:
        if args.dry_run or args.status_only:
            print("ERROR: --until-empty cannot be combined with --dry-run or --status-only", file=sys.stderr)
            return 2
        return _drain_evidence(args)
    conn = _connect(args.db)
    try:
        if args.status_only:
            evidence_recovery.ensure_schema(conn)
            counts = evidence_recovery.mark_status(conn)
            print("Evidence status: " + ", ".join(f"{s} {counts[s]}" for s in evidence_recovery.STATUSES)
                  + f" ({counts['changed']} changed)")
            return 0
        report = evidence_recovery.recover(conn, limit=args.limit, dry_run=args.dry_run)
    finally:
        conn.close()
    print(("[dry run] " if args.dry_run else "") + report.summary(dry_run=args.dry_run))
    if report.errors:
        print("ERROR: " + "; ".join(report.errors), file=sys.stderr)
        return 2
    return 0


def _drain_evidence(args) -> int:
    """``recover-evidence --until-empty``: rounds of queue -> fetch -> research until nothing more can be retrieved.

    Exit 0 when nothing is left to retrieve (what remains is exhausted or waits on another step), 2 when it
    stopped for a reason a later run can fix (the site throttled, an API budget was spent, a key is missing,
    no progress) - run it again to resume.
    """
    import asyncio

    from cyber_data_collector.processing import cyberdaily_refetch, evidence_recovery, page_rescrape
    from scripts.perplexity_backfill_events import enrich_waiting

    minutes = cyberdaily_refetch.DEFAULT_DRAIN_MINUTES if args.max_minutes is None else (args.max_minutes or None)
    steps = evidence_recovery.Steps(
        refetch=lambda conn, ids: cyberdaily_refetch.drain(
            conn, days=0, only=ids, first=ids, max_minutes=minutes, concurrency=cyberdaily_refetch.DRAIN_CONCURRENCY),
        rescrape=lambda conn: page_rescrape.rescrape_short_pages(conn).attempted,
        research=lambda conn, limit: asyncio.run(enrich_waiting(args.db, limit)),
    )
    conn = _connect(args.db)
    try:
        evidence_recovery.ensure_schema(conn)
        try:
            report = evidence_recovery.drain(conn, steps, limit=args.limit, max_rounds=args.max_rounds)
        except (page_rescrape.ScraperUnavailable, RuntimeError) as exc:
            print(f"ERROR: evidence recovery could not continue: {exc}", file=sys.stderr)
            return 2
    finally:
        conn.close()
    print(report.summary())
    from cyber_data_collector.utils.api_budget import budget
    print(budget.summary())
    return 0 if report.complete else 2


def cmd_check_record_identity(args) -> int:
    """Check every active record's page and enrichment against the record itself; see record_identity.

    Re-attaches enrichments that belong to another stored record, reopens records left without one, queues the
    re-fetch of pages that are another article and a replacement search where the record's URL cannot give the
    right page. No API call except the replacement searches (Perplexity); exit 2 when one was needed and
    PERPLEXITY_API_KEY is not set (the other routes still run).
    """
    from cyber_data_collector.processing import record_identity

    conn = _connect(args.db)
    try:
        if args.reverse:
            record_identity.ensure_schema(conn)
            back = record_identity.reverse(conn, args.reverse)
            conn.commit()
            print(f"Enrichment {args.reverse} " + (f"re-attached to {back}." if back else "was not re-attached by this command."))
            return 0 if back else 1
        report = record_identity.repair(conn, dry_run=args.dry_run, limit=args.limit)
    finally:
        conn.close()
    print(("[dry run] " if args.dry_run else "") + report.summary(dry_run=args.dry_run))
    if report.errors:
        print("ERROR: " + "; ".join(sorted(set(report.errors))), file=sys.stderr)
        return 2
    return 0


def cmd_reconcile_timelines(args) -> int:
    """Reconcile events against organisation timelines and resolve the mismatches; see timeline_reconciliation.

    Wrong-organisation and not-an-incident findings are resolved by
    check-victim-attribution and check-events (their outcome is shown);
    --apply re-dates date conflicts at the calibrated cut-off; --queue-gaps
    queues reporting of incidents no event covers. Fails (exit 2) when Jev or
    Perplexity is needed and unavailable, or --apply lacks its labels.
    """
    from cyber_data_collector.dedup.timeline_reconciliation import reconcile_all
    from cyber_data_collector.utils import jev

    if not jev.available():
        print("ERROR: TYPESAFE_API_KEY not set or typesafe-sdk missing; timelines cannot be reconciled.",
              file=sys.stderr)
        return 2
    conn = _connect(args.db)
    try:
        result = reconcile_all(conn, queue_gaps=args.queue_gaps, apply=args.apply)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    out = Path(args.db).parent / "timeline_reconciliation.json"
    out.write_text(json.dumps({"generated": datetime.now().isoformat(), **result}, indent=2, default=str),
                   encoding="utf-8")
    s = result["summary"]
    print(f"{s['organisations']} organisations, {s['events']} events: {s['matched']} matched a timeline incident "
          f"({s['low_confidence_matches']} with low confidence); unmatched: {s['unmatched']}; "
          f"{s['coverage_gaps']} coverage gap(s); {s['date_conflicts']} date conflict(s), "
          f"{s['redate_eligible']} with a more precise incident date.")
    for o in result["organisations"]:
        for e in o["events"]:
            if e.get("resolution") and e["resolution"]["step"] != "none":
                print(f"  {e['unmatched_kind']:18} {o['organisation'][:26]:26} {e['resolution']['outcome'][:60]:60} "
                      f"{(e['title'] or '')[:40]}")
    if not result["redate_calibrated"]:
        print("Re-dating is REPORT ONLY: no calibration for this model and question "
              "(scripts/calibrate_timeline_redates.py).")
    else:
        verb = "Re-dated" if result["redates_applied"] else "Would re-date (pass --apply)"
        for r in result["redates"]:
            print(f"  {str(r['stored_date'])[:10]} -> {r['new_date']}  ({r['match_probability']:.2f})  "
                  f"{(r['title'] or '')[:60]}")
        print(f"{verb} {len(result['redates'])} event(s) at match cut-off {result['redate_cutoff']}.")
    print(f"Findings in {out}")
    return 0


def cmd_research_timelines(args) -> int:
    """Research each multi-event organisation's incident history; see org_timeline.

    Re-researches only stale timelines (new evidence, or a changed question).
    Fails - non-zero exit - when research is needed and Perplexity is
    unavailable, rather than leaving stale timelines in use.
    """
    from cyber_data_collector.dedup.org_timeline import organisations_to_research, research_all

    conn = _connect(args.db)
    try:
        try:
            done = research_all(conn)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        total = len(organisations_to_research(conn))
    finally:
        conn.close()
    print(f"{done} of {total} organisations have a current timeline.")
    return 0


def cmd_check_victim_attribution(args) -> int:
    """Judge every event's recorded victim; with --apply, re-attribute below the calibrated cut-off.

    See :mod:`cyber_data_collector.dedup.victim_judgement`. Fails (exit 2)
    when Jev is unavailable or, with --apply, when the labels are missing.
    Without a calibration matching the current models and questions it
    reports only, and says so. Findings: ``instance/victim_attribution_findings.json``.
    """
    from cyber_data_collector.dedup.victim_judgement import check_victim_attribution
    from cyber_data_collector.utils import jev

    if not jev.available():
        print("ERROR: TYPESAFE_API_KEY not set or typesafe-sdk missing; victim attribution cannot run.",
              file=sys.stderr)
        return 2
    conn = _connect(args.db)
    try:
        result = check_victim_attribution(conn, apply=args.apply)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    out = Path(args.db).parent / "victim_attribution_findings.json"
    out.write_text(json.dumps({"generated": datetime.now().isoformat(), **result}, indent=2, default=str),
                   encoding="utf-8")
    if not result["calibrated"]:
        for f in result["reported"]:
            print(f"  {f['p_recorded_victim']:.3f}  {f['victim'][:30]:30}  {(f['title'] or '')[:60]}")
        print(f"REPORT ONLY - no calibration for these models and questions (run "
              f"scripts/calibrate_victim_attribution.py). Judged {result['judged']} event-victim pair(s); "
              f"{len(result['reported'])} below {0.5}; findings in {out}")
        return 0
    for a in result["actions"]:
        print(f"  {a['p_recorded_victim']:.3f}  {a['action']:12} {a['victim'][:28]:28} -> "
              f"{(a.get('new_victim') or a.get('candidate') or '-')[:28]:28} {(a['title'] or '')[:50]}")
    verb = "Applied" if result["applied"] else "Would apply (dry run; pass --apply)"
    print(f"{verb}: {result['counts']} at cut-off {result['cutoff']} (replacement threshold "
          f"{result['replacement_threshold']}); judged {result['judged']} pair(s); findings in {out}")
    review = result["flag_review"]
    print(f"Earlier flags: {review['flagged_before']} reassessed ({review['inputs_changed']} with changed input) - "
          f"{review['confirmed']} confirmed, {review['demoted']} demoted, {review['reattributed']} re-attributed, "
          f"{review['still_flagged']} still flagged; {review['unresolved_now']} attribution(s) unresolved now")
    return 0


def cmd_check_duplicates(args) -> int:
    """Judge same-victim event pairs, and AI-involved pairs, with Jev; merge at the calibrated cut-offs.

    See :mod:`cyber_data_collector.dedup.duplicate_judgement` (pairs sharing a
    victim) and :mod:`cyber_data_collector.dedup.ai_duplicates` (every pair of
    AI-involved events within a year, whatever the victim, judged with the
    AI-pair question and merged at its own calibrated cut-off; report only
    until calibrated). ``--skip-ai-pairs`` leaves the AI route out. Findings are
    written to ``instance/duplicate_findings.json``.
    """
    from cyber_data_collector.dedup.duplicate_judgement import check_duplicates
    from cyber_data_collector.utils import jev

    jev.require()   # fails loudly; never skip or switch model
    conn = _connect(args.db)
    try:
        result = check_duplicates(conn, apply=args.apply, include_ai=not args.skip_ai_pairs,
                                  cap_candidates=not args.no_candidate_cap,
                                  partners_per_event=args.partners_per_event, long_gap_budget=args.long_gap_budget)
    finally:
        conn.close()
    out = Path(args.db).parent / "duplicate_findings.json"
    out.write_text(json.dumps({"generated": datetime.now().isoformat(), **result}, indent=2, default=str),
                   encoding="utf-8")
    for route, stats in (result.get("candidates") or {}).items():
        if stats:
            print(f"Candidates ({route}): {stats['before']} -> {stats['after']} after ranking and the cap "
                  f"({stats['long_gap_before']} -> {stats['long_gap_after']} more than {stats['parameters']['window_days']} "
                  f"days apart; partners per event {stats['parameters']['partners_per_event']}, reserved long-gap/undated "
                  f"budget {stats['parameters']['long_gap_budget']} per group)")
    for held in result.get("held_conflicts") or []:
        print(f"  HELD {held['pair_id'][:8]}|...: would join {held['blocked_by'][:8]}|... ({held['reason']})")
    ai = result.get("ai")
    if ai:
        shown = ai["scored"][:10]
        state = (f"merging at cut-off {ai['cutoff']}" if ai["calibrated"] else
                 "REPORT ONLY - no calibration for these models, questions and evidence (label pairs and run "
                 "scripts/calibrate_ai_duplicates.py)")
        print(f"AI pairs: {ai['judged']} of {ai['pairs']} judged within {ai['window_days']} days, "
              f"{ai['escalated']} escalated; {state}. Highest scores:")
        for p in shown:
            gap = f"{p['gap_days']}d" if isinstance(p['gap_days'], int) else "undated"
            print(f"  {p['p_same']:.3f}  {gap:8} {p['pair_id'][:8]}|{p['pair_id'].split('|')[1][:8]}")
    if not result["calibrated"] and not (ai and ai["calibrated"]):
        print(f"Judged {result['judged']} of {result['pairs']} organisation pair(s). No calibration for this model "
              "and question: nothing merged. Run scripts/calibrate_duplicates.py.")
        return 0
    for p in result["proposals"]:
        gap = f"{p['gap_days']}d" if isinstance(p['gap_days'], int) else "undated"
        print(f"  {p['p_same']:.3f}  {(p.get('organisation') or '(AI pair)')[:30]:30}  {gap}  {p['pair_id'][:8]}|...")
    print(f"{len(result['merged'])} merge(s) applied of {len(result['proposals'])} proposed pair(s) at "
          f"cut-off {result['cutoff']}; findings in {out}")
    return _escalation_failures(result)


def cmd_find_supplier_victims(args) -> int:
    """Find every organisation affected by each supplier incident; see supplier_victims.

    Without ``--apply`` it reports. With ``--apply`` it merges the same supplier
    incident recorded under several customers and attaches accepted customers as
    victims - only with readable labels and a calibration measured on the current
    models and questions; otherwise it refuses (exit 2) and says why. Findings
    are written to ``instance/supplier_victim_findings.json``.
    """
    import os

    from cyber_data_collector.dedup import supplier_victims as sv
    from cyber_data_collector.utils import jev

    jev.require()   # fails loudly; never skip or switch model
    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set: the middle band cannot be escalated. Refusing to run.")
        return 2
    if args.research and not os.getenv("PERPLEXITY_API_KEY"):
        print("PERPLEXITY_API_KEY is not set: --research cannot search. Refusing to run.")
        return 2
    apply = args.apply and not args.dry_run
    conn = _connect(args.db)
    refresher = _make_refresher(args) if apply else None
    try:
        result = sv.find_supplier_victims(conn, apply=apply, research=args.research,
                                          role_refresher=refresher, limit=args.limit)
    except sv.CalibrationError as exc:
        print(f"Refusing to apply: {exc}")
        return 2
    finally:
        _flush_refresher(conn, refresher)
        conn.close()
    out = Path(args.db).parent / "supplier_victim_findings.json"
    out.write_text(json.dumps({"generated": datetime.now().isoformat(), **result}, indent=2, default=str),
                   encoding="utf-8")
    print(f"{result['supplier_incidents']} supplier incident(s); {result['victim_candidates']} victim "
          f"candidate(s), {result['pair_candidates']} other event(s) that may be the same incident.")
    print(f"Judged {result['judged']['victim']} victim and {result['judged']['same_incident']} event "
          f"candidate(s); escalated {result['escalated']['victim']} + {result['escalated']['same_incident']}.")
    for m in result["merged"]:
        if "refused" in m:
            print(f"  merge refused  {m['source'][:8]} -> {m['target'][:8]}: {m['refused']}")
        else:
            print(f"  merged  {m['source'][:8]} -> {m['target'][:8]}  victims carried: "
                  f"{', '.join(m['carried_victims']) or '-'}")
    for a in result["attached"]:
        ruled = a.get("own_events_ruled_different") or []
        print(f"  victim  {a['deduplicated_event_id'][:8]}  + {a['victim']}  ({a['decided_by']}, "
              f"p={a['p_affected'] if a['p_affected'] is None else round(a['p_affected'], 3)})"
              + (f"  own event(s) ruled different: {', '.join(r['own_event'][:8] for r in ruled)}" if ruled else ""))
    for h in result["held"]:
        print(f"  held    {h['deduplicated_event_id'][:8]}  {h['victim']}: own event {h['own_event'][:8]} - "
              f"{'; '.join(r['basis'] for r in h['own_event_rulings'])}")
    for v in result["variants"]:
        print(f"  variant {v['deduplicated_event_id'][:8]}  {v['candidate']} = {v['victim']} (not attached)")
    if apply:
        print(f"Merged {sum(1 for m in result['merged'] if 'refused' not in m)} event(s), attached "
              f"{len(result['attached'])} victim(s), held {len(result['held'])}, "
              f"{len(result['variants'])} other name(s) for existing victims (cut-offs "
              f"{result['cutoffs']}); findings in {out}")
    else:
        print(f"Report only (pass --apply to act); findings in {out}")
    problems = (len(result["failed"]["victim"]) + len(result["failed"]["same_incident"])
                + len(result["search_failures"]))
    undecided = len(result["undecided"]["victim"]) + len(result["undecided"]["same_incident"])
    if undecided:
        print(f"WARNING: {undecided} item(s) in the escalation band have no escalated answer and are "
              "undecided (never acted on).")
    if problems:
        print(f"ERROR: {problems} judgement(s) or search(es) failed; see the log and {out}.")
        return 1
    return 0


def cmd_check_victims(args) -> int:
    """Make each event's victim agree with the victim its own records name.

    See :mod:`cyber_data_collector.dedup.victim_agreement` for the guards.
    Findings are written to ``instance/victim_agreement_findings.json``.
    """
    from cyber_data_collector.dedup.victim_agreement import check_victims

    conn = _connect(args.db)
    try:
        findings = check_victims(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    for f in findings:
        print(f"  {f['event_date'] or '----------'}  {f['deduplicated_event_id'][:8]}  "
              f"{f['old_victim']!r} -> {f['new_victim']!r}  ({f['support']})  {(f['title'] or '')[:60]}")
    out = Path(args.db).parent / "victim_agreement_findings.json"
    out.write_text(json.dumps({"dry_run": args.dry_run, "generated": datetime.now().isoformat(),
                               "findings": findings}, indent=2, default=str), encoding="utf-8")
    verb = "Would re-attribute" if args.dry_run else "Re-attributed"
    print(f"{verb} {len(findings)} event(s); findings in {out}")
    return 0


def cmd_restore_dedup_state(args) -> int:
    """Put curated dedup state back from a backup after a destructive rebuild.

    A full rebuild regenerates every ``deduplicated_event_id``, which orphans
    ASD classifications and discards merges, splits and entity roles from the
    repair passes. The enriched records underneath are unchanged, so restoring
    only the dedup-derived tables recovers all of it; records added since the
    backup are then merged by the next incremental dedup run.
    """
    conn = _connect(args.db)
    try:
        report = restore_dedup_state(conn, args.backup, dry_run=args.dry_run)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Refusing to restore: {exc}")
        return 2
    finally:
        conn.close()

    print(f"{'table':28} {'live':>8} {'backup':>8} {'differing':>10}")
    for table, stats in report["tables"].items():
        print(f"{table:28} {stats['live']:>8} {stats['backup']:>8} "
              f"{stats['differing_rows']:>10}")
    if not report["changed"]:
        print("Dedup state already matches the backup - nothing to restore.")
    elif report["applied"]:
        print("Restored dedup state from the backup.")
    else:
        print("DRY RUN - re-run without --dry-run to restore.")
    print(f"Enriched events awaiting incremental dedup: {report['pending_incremental']} "
          "(run scripts/run_global_deduplication.py to merge them)")
    return 0


def cmd_rescrape_short_pages(args) -> int:
    """Re-fetch Google pages with too little stored text for the source gate; see page_rescrape."""
    from cyber_data_collector.processing.page_rescrape import (
        MAX_ATTEMPTS, ScraperUnavailable, rescrape_short_pages,
    )

    conn = _connect(args.db)
    try:
        try:
            report = rescrape_short_pages(conn, limit=args.limit, dry_run=args.dry_run,
                                          concurrency=args.concurrency)
        except ScraperUnavailable as exc:
            print(f"ERROR: re-scraping could not run: {exc}", file=sys.stderr)
            return 2
    finally:
        conn.close()
    if args.dry_run:
        print(f"{report.candidates} Google page(s) under the gate's minimum text would be re-fetched.")
        return 0
    print(f"Re-fetched {report.attempted} of {report.candidates} short Google page(s): "
          f"{len(report.stored)} stored, {len(report.failed)} failed "
          f"({report.gave_up} given up after {MAX_ATTEMPTS} attempts or unfetchable). "
          "Run check-sources to judge the stored pages.")
    return 0


def cmd_publication_dates(args) -> int:
    """Store every record's publication date and each event's first report; see publication_dates."""
    from cyber_data_collector.processing.incident_dates import (
        BASIS_STATED_IN_SOURCE,
        rederive_webber_dates,
        repair_webber_dates,
    )
    from cyber_data_collector.processing.publication_dates import (
        backfill,
        clear_listing_dates,
        refresh_event_dates,
    )

    conn = _connect(args.db)
    webber_failed = False
    try:
        webber = repair_webber_dates(conn, dry_run=args.dry_run)
        if webber.raw:
            print(f"Webber: {webber.raw} record(s) moved off the invented 15th to their heading month "
                  f"({webber.enriched} enriched record(s), {webber.events} event(s); {webber.publication_cleared} "
                  "heading month(s) cleared as publication dates)" + (" - dry run" if args.dry_run else ""))
        # A day counts as the incident's only where the text ties it to the breach; the first date in an
        # article is its byline. Records the old parser dated that way need the list heading's month back,
        # which only the live list gives (fetched only when such records exist).
        headings = None
        needs_list = conn.execute("SELECT 1 FROM RawEvents WHERE source_type = 'WebberInsurance' AND "
                                  "event_date_basis = ? LIMIT 1", (BASIS_STATED_IN_SOURCE,)).fetchone()
        if needs_list and not args.no_fetch:
            try:
                headings = _webber_headings()
            except Exception as exc:  # noqa: BLE001 - reported, and the records are left unresolved
                print(f"ERROR: Webber list could not be read ({type(exc).__name__}: {exc}); records dated by the "
                      "old parser keep their dates", file=sys.stderr)
                webber_failed = True
        redated = rederive_webber_dates(conn, dry_run=args.dry_run, headings=headings)
        print("Webber dates: " + redated.summary() + (" - dry run" if args.dry_run else ""))
        listing = clear_listing_dates(conn, dry_run=args.dry_run)
        if listing:
            print(f"{listing} record(s) carried a listing page's own date (a page cited for several incidents) "
                  "as their publication date; " + ("would be removed" if args.dry_run else "removed"))
        report = backfill(conn, limit=args.limit, concurrency=args.concurrency,
                          dry_run=args.dry_run or args.no_fetch, use_browser=not args.no_browser)
        if args.dry_run:
            print(f"{report.candidates} record(s) of active events have no publication date and would be fetched.")
            return 0
        events = refresh_event_dates(conn)
        conn.commit()
    finally:
        conn.close()
    if not args.no_fetch:
        print(report.summary())
        for how, count in sorted(report.how.items(), key=lambda kv: -kv[1]):
            print(f"  {count:5d}  {how}")
    print(events.summary())
    return 2 if webber_failed else 0


def _webber_headings() -> Dict[str, Any]:
    """List URL -> first day of its heading's month, from the live Webber data-breaches list."""
    import requests
    from bs4 import BeautifulSoup

    from cyber_data_collector.datasources.webber_insurance import WebberInsuranceDataSource
    from cyber_data_collector.models.config import DataSourceConfig
    from cyber_data_collector.utils import RateLimiter

    source = WebberInsuranceDataSource(DataSourceConfig(name="WebberInsurance"), RateLimiter(), {})
    response = requests.get(source.base_url, timeout=60,
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    response.raise_for_status()
    items = source._extract_all_event_links(BeautifulSoup(response.content, "html.parser"))
    return {item["url"]: item["section_date"].date() for item in items}


def cmd_normalise_enums(args) -> int:
    """Rewrite qualified enum names ('EventSeverity.MEDIUM') to their values; idempotent."""
    from cyber_data_collector.utils.enum_values import normalise_stored_enums

    conn = _connect(args.db)
    try:
        changed = normalise_stored_enums(conn, dry_run=args.dry_run)
    finally:
        conn.close()
    verb = "Would rewrite" if args.dry_run else "Rewrote"
    if not changed:
        print("No legacy enum spellings stored.")
        return 0
    for where, n in sorted(changed.items()):
        print(f"{verb} {n} row(s) in {where}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    from cyber_data_collector.utils.api_budget import ensure_configured
    ensure_configured()   # API_BUDGET_<PROVIDER> limits when run on its own
    parser = argparse.ArgumentParser(description="Deduplication v3 administration")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--no-refresh-roles", action="store_true",
        help="Do not reclassify entity roles for events whose membership "
             "changed. Roles are refreshed automatically otherwise.")
    add_model_arguments(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate").set_defaults(func=cmd_migrate)

    p = sub.add_parser("backfill")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("find-missed")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--recent-days", type=int, default=None,
                   help="Only judge pairs where at least one event was created or "
                        "updated in the last N days (routine refreshes).")
    p.add_argument("--min-certainty", type=float, default=0.80)
    p.add_argument("--no-llm", action="store_true",
                   help="Preview candidate pairs without calling the LLM.")
    p.set_defaults(func=cmd_find_missed)

    p = sub.add_parser("apply-missed")
    p.add_argument("--min-certainty", type=float, default=0.90)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_apply_missed)

    p = sub.add_parser("review-legacy",
                       help="Adjudicate members of merges that predate the ledger")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--min-certainty", type=float, default=0.80)
    p.add_argument("--no-llm", action="store_true")
    p.set_defaults(func=cmd_review_legacy)

    p = sub.add_parser("apply-legacy",
                       help="Split out members the legacy review rejected")
    p.add_argument("--min-certainty", type=float, default=0.85)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_apply_legacy)

    p = sub.add_parser("resolve-band",
                       help="Give borderline legacy findings a final disposition")
    p.add_argument("--min-certainty", type=float, default=0.80)
    p.add_argument("--max-certainty", type=float, default=0.90)
    p.add_argument("--decide-at", type=float, default=0.80,
                   help="Minimum agreed certainty required to act on a split.")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_resolve_band)

    p = sub.add_parser("recheck-splits",
                       help="Re-examine splits that separated the same organisation")
    p.add_argument("--min-certainty", type=float, default=0.80)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_recheck_splits)

    p = sub.add_parser("consolidate",
                       help="Rule 1: same entity + same date -> one event")
    p.add_argument("--date-tolerance", type=int, default=0,
                   help="Days of slack when matching event dates (default 0 = exact).")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_consolidate)

    p = sub.add_parser("adjudicate-clusters",
                       help="Partition entity clusters into incidents (whole-cluster)")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--entity", default=None, help="Only clusters matching this name.")
    p.add_argument("--min-certainty", type=float, default=0.75)
    p.add_argument("--max-cluster-size", type=int, default=20)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_adjudicate_clusters)

    p = sub.add_parser("check-recurrences",
                       help="Re-check every repeat attack on one entity that "
                            "follows the previous one within the window, in "
                            "case it is the same incident reported again")
    p.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS,
                   help="Inter-event gap below which a repeat is re-checked "
                        f"(default {DEFAULT_WINDOW_DAYS}).")
    p.add_argument("--min-certainty", type=float, default=0.85,
                   help="Merge only at or above this certainty.")
    p.add_argument("--max-run-size", type=int, default=12)
    p.add_argument("--entity", default=None, help="Only runs matching this name.")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_check_recurrences)

    p = sub.add_parser("size-entities",
                       help="Estimate an ordinal size band (SMALL/MEDIUM/LARGE/"
                            "HUGE/UNKNOWN) for every entity, researched online")
    p.add_argument("--refresh", action="store_true",
                   help="Re-estimate entities that already have a band.")
    p.add_argument("--entity", default=None, help="Only entities matching this name.")
    p.add_argument("--linked-only", action="store_true",
                   help="Only entities attached to an active event.")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--verbose", action="store_true",
                   help="Print each band as it is decided.")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_size_entities)

    p = sub.add_parser("retitle",
                       help="Give each event the best available title")
    p.add_argument("--min-members", type=int, default=1,
                   help="Only retitle events with at least this many records.")
    p.add_argument("--generate", action="store_true",
                   help="Write a headline with the strong OpenAI model when every candidate "
                        "title is a placeholder, prose or page furniture.")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_retitle)

    p = sub.add_parser("revictim",
                       help="Re-derive victim organisations from all members")
    p.add_argument("--min-members", type=int, default=1)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_revictim)

    p = sub.add_parser("classify-entities",
                       help="Label entity kind and per-event role")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--entity", default=None, help="Only events matching this title text.")
    p.add_argument("--max-entities", type=int, default=25)
    p.add_argument("--show-roles", action="store_true",
                   help="Print every entity's kind and role.")
    p.add_argument("--stale-only", action="store_true",
                   help="Only events whose membership changed since classification.")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_classify_entities)

    p = sub.add_parser("reconcile-entities",
                       help="Re-examine every event recorded against the same "
                            "organisation and merge those that are one incident")
    p.add_argument("--limit", type=int)
    p.add_argument("--entity")
    p.add_argument("--min-certainty", type=float, default=0.8)
    p.add_argument("--max-cluster-size", type=int, default=25)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_reconcile_entities)

    p = sub.add_parser("canonicalise-entities",
                       help="Collapse duplicate spellings of one organisation")
    p.add_argument("--limit", type=int)
    p.add_argument("--explicit-only", action="store_true",
                   help="Apply only the folds EXPLICIT_ALIASES names (no structural matching)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_canonicalise_entities)

    p = sub.add_parser("redate",
                       help="Repair event dates from member consensus and text")
    p.add_argument("--research", action="store_true",
                   help="Search (Perplexity + the strong OpenAI model) for events the stored "
                        "text cannot date.")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_redate)

    p = sub.add_parser("recount",
                       help="Re-derive records_affected from each event's own "
                            "source records")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_recount)

    p = sub.add_parser("name-entities",
                       help="Replace descriptions used as entity names with "
                            "the actual organisation")
    p.add_argument("--no-llm", action="store_true",
                   help="Only use names already recorded on the event.")
    p.add_argument("--no-search", action="store_true",
                   help="Do not fall back to a Perplexity search.")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_name_entities)

    p = sub.add_parser("set-fact", help="Correct an event fact explicitly (kept over recomputation)")
    p.add_argument("dedup_id")
    p.add_argument("field", choices=["records_affected", "event_type", "severity", "attack_method",
                                     "attacking_entity_name"])
    p.add_argument("value", help="the corrected value, or 'null' for unknown")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_set_fact)

    p = sub.add_parser("refresh-facts", help="Recompute facts of events whose membership changed")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_refresh_facts)

    p = sub.add_parser("roles-status",
                       help="Report whether entity roles match current membership")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--invalidate-all", action="store_true",
                   help="Mark every event stale (e.g. after changing the classifier).")
    p.set_defaults(func=cmd_roles_status)

    p = sub.add_parser("split",
                       help="Move one or more records out of an event into one new event; "
                            "the ruling is cached so no merge pass can undo it")
    p.add_argument("dedup_id")
    p.add_argument("enriched_id", nargs="+",
                   help="Record(s) to move out; several move together as one event.")
    p.add_argument("--reason", default="manual split")
    p.set_defaults(func=cmd_split)

    p = sub.add_parser("override")
    p.add_argument("left")
    p.add_argument("right")
    p.add_argument("verdict", choices=["same", "different"])
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_override)

    p = sub.add_parser("merge",
                       help="Fold SOURCE deduplicated event into TARGET now, and "
                            "record a 'same' override so it survives rebuilds")
    p.add_argument("target", help="deduplicated_event_id that survives")
    p.add_argument("source", help="deduplicated_event_id folded into target")
    p.add_argument("--reason", required=True)
    p.add_argument("--force", action="store_true",
                   help="Merge even if a 'different' ruling exists between the events.")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_merge)

    sub.add_parser("learn").set_defaults(func=cmd_learn)

    p = sub.add_parser("ancestry")
    p.add_argument("dedup_id")
    p.set_defaults(func=cmd_ancestry)

    p = sub.add_parser("check-coherence",
                       help="Ask Jev whether each suspect member is about its event's incident "
                            "and split out strays below the calibrated cut-off")
    p.add_argument("--event", action="append", default=None,
                   help="Only this event (dedup id or prefix); repeatable.")
    p.add_argument("--apply", action="store_true",
                   help="Split (model-judged strays only with a calibration matching the current "
                        "models and question; labelled strays always). Otherwise report only.")
    p.set_defaults(func=cmd_check_coherence)

    p = sub.add_parser("recalibrate",
                       help="Re-score the labelled cases on current evidence and recompute every cut-off "
                            "whose evidence processing changed (spends Jev/escalation calls)")
    p.add_argument("--step", action="append", default=None,
                   help="Only this step (source-gate, event-reality, coherence, "
                        "coherence-same-organisation, duplicates, ai-duplicates, victim-attribution, "
                        "supplier-victims); "
                        "repeatable. Default: all.")
    p.add_argument("--status", action="store_true", help="Print each calibration's state; spend nothing")
    p.add_argument("--if-stale", action="store_true",
                   help="Only calibrations that exist but no longer match the current model, question or "
                        "evidence processing (what the refresh runs)")
    p.add_argument("--dry-run", action="store_true",
                   help="Count the labelled cases each snapshot holds; judge and write nothing")
    p.add_argument("--snapshot", action="append", default=None, metavar="STEP=PATH",
                   help="Labelling-time database for a step, overriding evaluation/calibration_snapshots.json")
    p.add_argument("--precision", type=float, default=0.95)
    p.add_argument("--min-coverage", type=float, default=0.9,
                   help="Fail (keeping the old calibration) when fewer than this share of the previously "
                        "labelled cases are usable")
    p.add_argument("--fresh", action="store_true",
                   help="Rebuild the recalibration copy from the snapshot instead of reusing its stored answers")
    p.set_defaults(func=cmd_recalibrate)

    p = sub.add_parser("check-same-organisation",
                       help="Find separate incidents merged under one organisation (Jev, calibrated split; "
                            "report only until labelled and calibrated)")
    p.add_argument("--event", action="append", default=None,
                   help="Only this event (dedup id or prefix); repeatable.")
    p.add_argument("--apply", action="store_true",
                   help="Split (model-judged candidates only with a calibration matching the current models, "
                        "questions and evidence; labelled-different members always). Otherwise report only.")
    p.set_defaults(func=cmd_check_same_organisation)

    p = sub.add_parser("resolve-label-contradictions",
                       help="Split pairs labelled different that were merged into one event (ledger split, "
                            "reversible; no API calls)")
    p.add_argument("--dry-run", action="store_true", help="List what would be split; change nothing")
    p.add_argument("--snapshot", default=None,
                   help="The database the duplicate labels were made on (default: evaluation/"
                        "calibration_snapshots.json, 'duplicates')")
    p.set_defaults(func=cmd_resolve_label_contradictions)

    p = sub.add_parser("adjudicate-candidates",
                       help="Find missed duplicates by partitioning whole groups of "
                            "multi-key candidates (preferred over find-missed)")
    p.add_argument("--recent-days", type=int, default=None,
                   help="Only groups containing an event changed in the last N days.")
    p.add_argument("--min-certainty", type=float, default=0.9)
    p.add_argument("--max-cluster-size", type=int, default=20)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_adjudicate_candidates)

    p = sub.add_parser("classify-pages",
                       help="Classify suspect pages (roundups, guidance, profiles, "
                            "home pages) with a small model; verdicts are stored")
    p.add_argument("--unmapped-only", action="store_true",
                   help="Only records not yet in any event (new ingest).")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_classify_pages)

    p = sub.add_parser("reject-non-incidents",
                       help="Reject events whose every record is a non-incident: "
                            "a non-Australian page Perplexity could not tie to any "
                            "incident, or a page on the vendor blocklist "
                            "(cyber_data_collector/dedup/vendor_source_blocklist.txt)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_reject_non_incidents)

    p = sub.add_parser("check-events",
                       help="Ask Jev whether each active event is a real cyber incident "
                            "and reject those below the calibrated cut-off")
    p.add_argument("--apply", action="store_true",
                   help="Reject below the cut-off (only with a calibration matching the "
                        "current model and question; otherwise report only)")
    p.set_defaults(func=cmd_check_events)

    p = sub.add_parser("check-sources",
                       help="Ask Jev whether each Google-sourced page itself reports an incident; "
                            "remove records below the calibrated cut-off and queue replacement sources")
    p.add_argument("--apply", action="store_true",
                   help="Remove (only with a calibration matching the current model and question)")
    p.add_argument("--unmapped-only", action="store_true",
                   help="Only records not yet in an event (the ingestion gate)")
    p.set_defaults(func=cmd_check_sources)

    p = sub.add_parser("label-evidence",
                       help="For labelled-real events with no active record, queue the report the label "
                            "cites; once processed, merge it into the labelled event and split out the "
                            "event's inactive records")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_label_evidence)

    p = sub.add_parser("recover-evidence",
                       help="Find what each incident with no usable evidence lacks (page text, research), "
                            "queue retrieval through the existing re-fetch, enrichment, label-evidence and "
                            "replacement-search queues, and mark every incident's evidence status")
    p.add_argument("--limit", type=int, default=100, help="Incidents given new retrieval work per run")
    p.add_argument("--dry-run", action="store_true", help="Report what would be queued; write nothing")
    p.add_argument("--status-only", action="store_true",
                   help="Only (re)write DeduplicatedEvents.evidence_status; queue nothing")
    p.add_argument("--until-empty", action="store_true",
                   help="Drain: round after round, queue work, run the Cyber Daily re-fetch for these incidents' "
                        "stories, the page re-fetch queue and Perplexity research, until nothing more can be "
                        "retrieved, an API budget is spent, the site throttles or a round makes no progress "
                        "(resumable; --limit is then incidents queued per round)")
    p.add_argument("--max-rounds", type=int, default=20, help="Most rounds a drain makes (default 20)")
    p.add_argument("--max-minutes", type=float, default=None,
                   help="Time budget for each Cyber Daily re-fetch inside a drain, in minutes "
                        "(default: cyberdaily_refetch.DEFAULT_DRAIN_MINUTES)")
    p.add_argument("--api-budget", nargs="+", default=None, metavar="PROVIDER=N",
                   help="Requests allowed per paid API during this run, e.g. perplexity=800 "
                        "(default: API_BUDGET_<PROVIDER> env, else unlimited); a drain stops when it is spent")
    p.set_defaults(func=cmd_recover_evidence)

    p = sub.add_parser("check-record-identity",
                       help="Check that each active record's page and enrichment describe the record's own "
                            "article: re-attach enrichments that belong to another stored record, reopen "
                            "records left without one, queue the re-fetch of pages that are another article "
                            "and a replacement search where the URL cannot give the right page")
    p.add_argument("--limit", type=int, default=100,
                   help="New retrieval work per route per run (re-fetches, searches, records reopened)")
    p.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing")
    p.add_argument("--reverse", metavar="ENRICHED_EVENT_ID",
                   help="Undo the re-attachment of one enrichment (it goes back to the record it came from)")
    p.set_defaults(func=cmd_check_record_identity)

    p = sub.add_parser("reconcile-timelines",
                       help="Match each organisation's events to its researched timeline; report "
                            "unmatched events (gap / wrong organisation / not an incident), coverage "
                            "gaps and date conflicts")
    p.add_argument("--queue-gaps", action="store_true",
                   help="Search for and queue reporting of incidents no event covers (adds evidence only)")
    p.add_argument("--apply", action="store_true",
                   help="Re-date date conflicts at the calibrated match cut-off (only with a matching "
                        "calibration and readable labels)")
    p.set_defaults(func=cmd_reconcile_timelines)

    p = sub.add_parser("research-timelines",
                       help="Research (or reuse) the incident timeline of every organisation "
                            "with several events (Perplexity; stored)")
    p.set_defaults(func=cmd_research_timelines)

    p = sub.add_parser("check-victim-attribution",
                       help="Ask Jev whether each event's recorded victim is really the organisation "
                            "affected; re-attribute below the calibrated cut-off")
    p.add_argument("--apply", action="store_true",
                   help="Demote / re-attribute (only with a calibration matching the current models and "
                        "questions, and readable labels; otherwise refuses or reports)")
    p.set_defaults(func=cmd_check_victim_attribution)

    p = sub.add_parser("check-duplicates",
                       help="Ask Jev whether each pair of events sharing a victim - and each pair of "
                            "AI-involved events within a year, whatever the victim - is one "
                            "incident and merge pairs at or above the calibrated cut-offs")
    p.add_argument("--apply", action="store_true",
                   help="Merge (only with a calibration matching the current model and "
                        "question; otherwise report only)")
    p.add_argument("--skip-ai-pairs", action="store_true",
                   help="Leave out the AI-pair route (pairs of AI-involved events with any victim)")
    p.add_argument("--partners-per-event", type=int, default=None,
                   help="Best-ranked partners kept per event within a group (default 8; 16 for AI pairs; "
                        "0 = no limit). See dedup/candidate_ranking.py")
    p.add_argument("--long-gap-budget", type=int, default=None,
                   help="Strong long-gap or undated pairs kept per group beyond the partner limit (default 5)")
    p.add_argument("--no-candidate-cap", action="store_true",
                   help="Ask every candidate pair (still ranked); the pre-2026-10-29 behaviour")
    p.set_defaults(func=cmd_check_duplicates)

    p = sub.add_parser("find-supplier-victims",
                       help="Find every organisation affected by each supplier/third-party "
                            "incident (Jev, calibrated); with --apply merge the same supplier "
                            "incident recorded under several customers and attach co-victims")
    p.add_argument("--apply", action="store_true",
                   help="Act (only with readable labels and a calibration matching the current "
                        "models and questions; otherwise refuse with exit 2)")
    p.add_argument("--dry-run", action="store_true", help="Report only (the default)")
    p.add_argument("--research", action="store_true",
                   help="One stored Perplexity search per supplier incident for the "
                        "organisations it affected (reused on later runs)")
    p.add_argument("--limit", type=int, default=None, help="Only the first N supplier incidents")
    p.set_defaults(func=cmd_find_supplier_victims)

    p = sub.add_parser("check-victims",
                       help="Re-attribute events whose records all name a different "
                            "victim than the event's (no API calls)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_check_victims)

    p = sub.add_parser("rescrape-short-pages",
                       help="Re-fetch Google-sourced pages whose stored text is too short for the "
                            "source gate; store only real, longer page text (no Perplexity "
                            "substitutes); give up on a page after 3 failed attempts")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--dry-run", action="store_true", help="Count the pages; fetch nothing")
    p.set_defaults(func=cmd_rescrape_short_pages)

    p = sub.add_parser("publication-dates",
                       help="Store each record's publication date (the source's own, else the page's "
                            "metadata - never its modified date), then each event's first-report date "
                            "and reporting lag; an undated event is dated by its first report")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--no-fetch", action="store_true",
                   help="Fetch no pages: only copy source dates and refresh the events")
    p.add_argument("--no-browser", action="store_true",
                   help="Do not fetch pages a plain HTTP client was refused again with a browser")
    p.add_argument("--dry-run", action="store_true", help="Count the records; change nothing")
    p.set_defaults(func=cmd_publication_dates)

    p = sub.add_parser("normalise-enums",
                       help="Rewrite stored qualified enum names (EventSeverity.MEDIUM, "
                            "CyberEventType.OTHER, EntityType.X) to their values in every "
                            "table and in RawEvents.source_metadata (no API calls; idempotent)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_normalise_enums)

    p = sub.add_parser("restore-dedup-state",
                       help="Restore curated dedup tables and ASD classifications "
                            "from a backup after a destructive rebuild")
    p.add_argument("backup", help="SQLite backup holding the dedup state to restore")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_restore_dedup_state)

    args = parser.parse_args(argv)
    apply_model_arguments(args)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    # These emit the full request/response payload at DEBUG, which buries the
    # command's own output.
    for noisy in ("httpx", "httpcore", "openai", "instructor"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
