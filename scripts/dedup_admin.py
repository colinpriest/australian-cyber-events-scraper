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
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
from cyber_data_collector.dedup.adjudicator import Adjudicator, EventRecord
from cyber_data_collector.dedup.backfill import run_backfill
from cyber_data_collector.dedup.cluster_adjudicator import ClusterAdjudicator
from cyber_data_collector.dedup.entity_classification import (
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


def load_dedup_records(conn: sqlite3.Connection) -> List[EventRecord]:
    """One EventRecord per active deduplicated event, keyed by its dedup id.

    ``enriched_event_id`` carries the *deduplicated* id here because the
    reconciliation pass compares whole deduplicated events, not raw members.
    """
    rows = conn.execute(
        """
        SELECT d.deduplicated_event_id AS did, d.title, d.summary,
               d.event_date, d.records_affected,
               d.victim_organization_name AS entity,
               (SELECT s.source_url FROM DeduplicatedEventSources s
                WHERE s.deduplicated_event_id = d.deduplicated_event_id
                LIMIT 1) AS source_url
        FROM DeduplicatedEvents d
        WHERE COALESCE(d.status, 'Active') = 'Active'
        """
    ).fetchall()
    return [
        EventRecord(
            enriched_event_id=r["did"],
            title=r["title"] or "",
            summary=r["summary"],
            entity_name=r["entity"],
            event_date=str(r["event_date"]) if r["event_date"] else None,
            source_url=r["source_url"],
            records_affected=r["records_affected"],
        )
        for r in rows
    ]


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
                    except (sqlite3.Error, ValueError) as exc:
                        logger.warning("Reconcile merge %s -> %s failed: %s",
                                       source, target, exc)
            conn.commit()

        print(f"\n{'DRY RUN: ' if args.dry_run else ''}{planned} event(s) "
              f"{'would be ' if args.dry_run else ''}merged")
        print(f"cluster stats: {adjudicator.stats}")
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
        raw = str(row["entity_type"]).split(".")[-1]
        if raw and raw.isalpha():
            return raw.title()
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
        victims = vendors = labelled = 0

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

            result = classifier.classify(r["title"] or "", r["summary"], names)
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
    finally:
        conn.close()
    return 0



def cmd_canonicalise_entities(args) -> int:
    """Collapse duplicate spellings of one organisation into a single entity.

    Blocking keys candidate pairs on the entity, so "Australian Securities and
    Investment Commission" and "Australian Securities and Investments
    Commission" were never compared - which is how three ASIC events with
    byte-identical titles, dates and descriptions all survived deduplication.
    Fixing the entities is a precondition for fixing those merges.
    """
    conn = _connect(args.db)
    try:
        schema.migrate(conn)
        resolver = EntityResolver()
        groups = canonical_groups(conn)
        print(f"{len(groups)} organisation(s) recorded under more than one name")

        merged = 0
        for keep, drop in groups[: args.limit] if args.limit else groups:
            print(f"  {keep!r}")
            for name in drop:
                print(f"      <- {name!r}")
            if args.dry_run:
                continue
            merged += merge_entities(conn, keep, drop)
            inherit_attributes(conn, keep)
            for name in drop:
                # Remembered, so a future ingest resolves the variant instead
                # of recreating it.
                resolver.learn_alias(conn, name, keep, source="rule")
        if args.dry_run:
            print("\nDry run - nothing written.")
            return 0
        conn.commit()
        print(f"\nFolded {merged} duplicate spelling(s) into their canonical entity.")
    finally:
        conn.close()
    return 0


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
        new_id = ledger.split_member(
            args.dedup_id, args.enriched_id, args.reason, actor="human"
        )
        conn.commit()
        print(f"Split {args.enriched_id} out into new event {new_id}")
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Deduplication v3 administration")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--no-refresh-roles", action="store_true",
        help="Do not reclassify entity roles for events whose membership "
             "changed. Roles are refreshed automatically otherwise.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate").set_defaults(func=cmd_migrate)

    p = sub.add_parser("backfill")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("find-missed")
    p.add_argument("--limit", type=int, default=None)
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
                   help="Write a headline with GPT-4o when every candidate "
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
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_canonicalise_entities)

    p = sub.add_parser("redate",
                       help="Repair event dates from member consensus and text")
    p.add_argument("--research", action="store_true",
                   help="Search (Perplexity + GPT-4o) for events the stored "
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

    p = sub.add_parser("roles-status",
                       help="Report whether entity roles match current membership")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--invalidate-all", action="store_true",
                   help="Mark every event stale (e.g. after changing the classifier).")
    p.set_defaults(func=cmd_roles_status)

    p = sub.add_parser("split")
    p.add_argument("dedup_id")
    p.add_argument("enriched_id")
    p.add_argument("--reason", default="manual split")
    p.set_defaults(func=cmd_split)

    p = sub.add_parser("override")
    p.add_argument("left")
    p.add_argument("right")
    p.add_argument("verdict", choices=["same", "different"])
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_override)

    sub.add_parser("learn").set_defaults(func=cmd_learn)

    p = sub.add_parser("ancestry")
    p.add_argument("dedup_id")
    p.set_defaults(func=cmd_ancestry)

    args = parser.parse_args(argv)
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
