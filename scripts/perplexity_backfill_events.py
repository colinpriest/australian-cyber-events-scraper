#!/usr/bin/env python3
"""
Backfill existing events with Perplexity enrichment.

This script:
1. Identifies events that need Perplexity validation
2. Prioritizes events with placeholder dates (1st of month)
3. Enriches events with validated data from Perplexity
4. Updates the database with enriched information
5. Triggers re-deduplication after enrichment

Every answer passes the acceptance gate (processing/enrichment_gate.py): each
fact must be stated in the record's own text or in a cited page about the same
incident, which is retrieved and cached first (processing/cited_documents.py).

Usage:
    python scripts/perplexity_backfill_events.py [--limit N] [--priority-only] [--dry-run]
    python scripts/perplexity_backfill_events.py --recompute-reliability [--dry-run] [--limit N]
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Runnable as a script from the project root (python scripts/...).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.storage.cyber_event_data_v2 import CyberEventDataV2
from cyber_data_collector.processing.cited_documents import CitedDocumentStore
from cyber_data_collector.processing.enrichment_gate import (
    MAX_ENRICHMENT_ATTEMPTS,
    CorruptStoredEnrichment,
    GateResult,
    evaluate_enrichment,
    read_stored_blob,
    valid_source_urls,
)
from cyber_data_collector.processing.perplexity_enrichment import PerplexityEnrichmentEngine
from cyber_data_collector.processing.record_evidence import RecordEvidence
from cyber_data_collector.utils import ConfigManager
from cyber_data_collector.utils.api_budget import is_budget_refusal

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# Per-record outcomes of one enrichment attempt.
OUTCOME_VALIDATED = 'validated'            # facts stored, record validated
OUTCOME_PARTIAL = 'partially_supported'    # supported facts stored; confident unsupported claims withheld, record re-queued
OUTCOME_REJECTED = 'rejected'              # answer failed the gate (failed attempt)
OUTCOME_REQUEST_FAILED = 'request_failed'  # the API call failed (failed attempt)
OUTCOME_BUDGET_REFUSED = 'budget_refused'  # refused by the run's API budget: not attempted, not a failure
OUTCOME_STORAGE_FAILED = 'storage_failed'  # accepted, but writing it failed (failed attempt)
OUTCOME_CORRUPT_BASELINE = 'corrupt_baseline'  # stored enrichment unreadable: quarantined, nothing written
OUTCOME_DRY_RUN = 'dry_run'                # accepted; dry run wrote nothing

FAILURE_OUTCOMES = frozenset({OUTCOME_REJECTED, OUTCOME_REQUEST_FAILED, OUTCOME_STORAGE_FAILED,
                              OUTCOME_CORRUPT_BASELINE})


def _publication_day(source_metadata: Optional[str]) -> Optional[str]:
    """``publication_date`` from a RawEvents.source_metadata JSON, if any."""
    if not source_metadata:
        return None
    try:
        meta = json.loads(source_metadata)
    except (TypeError, ValueError):
        return None
    value = meta.get('publication_date') if isinstance(meta, dict) else None
    return str(value)[:10] if value else None


def _parse_day(value: Optional[str]) -> Optional[date]:
    """Best-effort ``YYYY-MM-DD`` prefix of a stored timestamp."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


class PerplexityBackfillProcessor:
    """Process backfilling of existing events with Perplexity enrichment."""

    def __init__(
        self,
        db: CyberEventDataV2,
        perplexity_engine: PerplexityEnrichmentEngine,
        dry_run: bool = False,
        cited_documents: Optional[CitedDocumentStore] = None,
    ):
        self.db = db
        self.perplexity_engine = perplexity_engine
        self.dry_run = dry_run
        # Cited pages are read (and cached) so each fact can be checked
        # against what its sources actually say.  A dry run still caches the
        # pages it reads: they are a copy of the web, not enrichment data.
        self.cited_documents = cited_documents or CitedDocumentStore(db._conn, db._lock)
        #: Counts from the last enrich_events_concurrent call (see its docstring).
        self.last_counts: Dict[str, int] = {}
        self.stats = {
            'total_candidates': 0,
            'enriched_successfully': 0,
            'enriched_failed': 0,
            'partially_supported': 0,
            'storage_failures': 0,
            'bookkeeping_failures': 0,
            'quarantined': 0,
            'skipped': 0,
            'updated_fields': {
                'event_date': 0,
                'entity_name': 0,
                'threat_actor': 0,
                'attack_method': 0,
                'victim_count': 0
            }
        }

    def get_events_needing_enrichment(
        self,
        limit: Optional[int] = None,
        priority_only: bool = False
    ) -> List[Dict]:
        """Get events that need Perplexity enrichment, prioritized by need."""

        with self.db._lock:
            cursor = self.db._conn.cursor()

            # Build query to find events needing enrichment
            query = """
                SELECT
                    ee.enriched_event_id,
                    ee.title,
                    ee.description,
                    ee.summary,
                    ee.event_date,
                    ee.event_type,
                    ee.records_affected,
                    ee.confidence_score,
                    ee.perplexity_validated,
                    ee.attacking_entity_name,
                    ee.attack_method,
                    ee.raw_event_id,
                    ee.perplexity_enrichment_data,
                    COALESCE(ee.perplexity_attempt_count, 0) AS perplexity_attempt_count,
                    -- The persisted record: enrichment reads the scraped article
                    -- stored here, not the processed description above.
                    re.raw_title,
                    re.raw_description,
                    re.raw_content,
                    re.source_url,
                    re.source_type,
                    re.source_metadata,
                    CAST(re.discovered_at AS TEXT) AS discovered_at
                FROM EnrichedEvents ee
                LEFT JOIN RawEvents re ON re.raw_event_id = ee.raw_event_id
                WHERE ee.status = 'Active'
                  AND (ee.perplexity_validated IS NULL OR ee.perplexity_validated = FALSE)
                  AND ee.is_specific_event = TRUE
                  -- Quarantined records have used up their attempts; they are
                  -- reported, not retried, until a human releases them.
                  AND COALESCE(ee.perplexity_status, '') != 'quarantined'
                  AND COALESCE(ee.perplexity_attempt_count, 0) < ?
            """

            params = [MAX_ENRICHMENT_ATTEMPTS]

            if priority_only:
                # Only process high-priority events (placeholder dates or missing entity)
                query += """
                    AND (
                        -- Placeholder dates (1st of month)
                        CAST(strftime('%d', ee.event_date) AS INTEGER) = 1
                        -- OR missing/low confidence entity data would go here
                    )
                """

            # Order by priority: records of incidents with no usable evidence
            # first (evidence_recovery marks them), then placeholder dates,
            # then by date.
            query += f"""
                ORDER BY
                    {self._unsupported_first()}
                    CASE WHEN CAST(strftime('%d', ee.event_date) AS INTEGER) = 1 THEN 0 ELSE 1 END,
                    COALESCE(ee.perplexity_attempt_count, 0) ASC,
                    ee.event_date DESC,
                    ee.confidence_score ASC
            """

            if limit:
                query += f" LIMIT {limit}"

            cursor.execute(query, params)
            events = [dict(row) for row in cursor.fetchall()]

            logger.info(f"Found {len(events)} events needing enrichment")
            return events

    def _unsupported_first(self) -> str:
        """ORDER BY term putting records of ``unsupported`` incidents first ('' before the column exists)."""
        from cyber_data_collector.processing import evidence_recovery
        from cyber_data_collector.reporting.population import incident_filter

        if not evidence_recovery.has_schema(self.db._conn):
            return ""
        where, _ = incident_filter(alias="d")
        return f"""CASE WHEN EXISTS (
                        SELECT 1 FROM EventDeduplicationMap m
                        JOIN DeduplicatedEvents d ON d.deduplicated_event_id = m.deduplicated_event_id
                        WHERE m.enriched_event_id = ee.enriched_event_id AND {where}
                          AND d.evidence_status = '{evidence_recovery.UNSUPPORTED}') THEN 0 ELSE 1 END,"""

    def _record_failure(self, event: Dict, reason: str, exc: Optional[BaseException] = None) -> None:
        """Record a failed attempt (or just log it on a dry run)."""
        logger.warning(f"Enrichment not accepted for '{(event.get('title') or '')[:60]}': {reason}")
        refused = is_budget_refusal(exc) if exc is not None else is_budget_refusal(RuntimeError(reason))
        if self.dry_run or refused:
            # A request refused by the run's API budget was never attempted:
            # counting it would quarantine records for running out of budget.
            return
        try:
            status = self.db.record_enrichment_failure(event['enriched_event_id'], reason)
            if status == 'quarantined':
                self.stats['quarantined'] += 1
        except Exception as exc:  # bookkeeping must not abort the batch - but it is counted
            self.stats['bookkeeping_failures'] += 1
            logger.error(f"Could not record failed attempt for {event.get('enriched_event_id')}: {exc}")

    async def enrich_event(self, event: Dict) -> Optional[Dict]:
        """Enrich a single event with Perplexity and gate the answer.

        The answer is judged against the record's *persisted* text and the
        retrieved text of the pages it cites, and only an answer that passes
        the acceptance gate is returned.  Anything else is recorded as a
        failed attempt (retried on later runs, quarantined after
        MAX_ENRICHMENT_ATTEMPTS) and None is returned.
        """
        _, data = await self._enrich(event)
        return data

    async def _enrich(self, event: Dict) -> Tuple[str, Optional[Dict]]:
        """Like :meth:`enrich_event`, also returning the outcome (``OUTCOME_*``)."""

        logger.info(f"Enriching event: {event['title'][:60]}...")

        # A stored enrichment that cannot be read leaves no baseline to weigh
        # a new answer against: flag the record for a human instead of paying
        # for an answer that could only overwrite blindly.
        try:
            read_stored_blob(event.get('perplexity_enrichment_data'))
        except CorruptStoredEnrichment as exc:
            self._quarantine(event, f"{exc}; baseline unknown - repair or clear it, then --release-quarantine")
            return OUTCOME_CORRUPT_BASELINE, None

        # The organisations already linked to the record (never a guess).
        organisations = self.db.get_record_organisations(event['enriched_event_id'])
        current_entity = self._extract_entity_name(event, organisations)

        # What is actually stored for this record: the scraped article if
        # there is one, otherwise a snippet that the prompt labels as such.
        evidence = RecordEvidence.from_row(event)

        # Call Perplexity for enrichment
        try:
            enrichment = await self.perplexity_engine.enrich_event(
                title=event['title'],
                description=event.get('description') or event.get('summary', ''),
                current_date=str(event.get('event_date')) if event.get('event_date') else None,
                current_entity=current_entity,
                evidence=evidence,
            )
        except Exception as exc:
            self._record_failure(event, f"request failed: {str(exc)[:300]}", exc)
            return (OUTCOME_BUDGET_REFUSED if is_budget_refusal(exc) else OUTCOME_REQUEST_FAILED), None
        if enrichment is not None and enrichment.parse_failed and is_budget_refusal(
                RuntimeError(enrichment.failure_reason or "")):
            self._record_failure(event, enrichment.failure_reason)
            return OUTCOME_BUDGET_REFUSED, None

        # Read the pages the answer cites (cached; failures are cached too).
        urls = [] if enrichment is None or enrichment.parse_failed else valid_source_urls(
            enrichment.sources_consulted)
        loop = asyncio.get_running_loop()
        documents = await loop.run_in_executor(None, self.cited_documents.get_documents, urls)

        gate = evaluate_enrichment(
            enrichment,
            record_text=evidence.match_text(),
            cited_documents=documents,
            organisations=organisations,
            reference_date=_parse_day(evidence.discovered_at),
            record_published=_publication_day(event.get('source_metadata')),
        )
        if not gate.is_accepted:
            self._record_failure(event, gate.failure_reason or "enrichment rejected by the acceptance gate")
            return OUTCOME_REJECTED, None

        for fact, why in gate.rejected_facts.items():
            logger.info(f"  Withheld {fact}: {why}")
        for fact in gate.accepted_facts:
            decision = gate.decisions[fact]
            logger.debug(f"  {fact} supported by {decision.source_kind} {decision.source_url or ''}")
        logger.info(
            f"Enriched successfully (overall confidence: {gate.sanitized.overall_confidence:.2f}, "
            f"reliability: {gate.reliability})"
        )

        return OUTCOME_VALIDATED, {
            'enriched_event_id': event['enriched_event_id'],
            'enrichment': gate.sanitized,
            'gate': gate,
            'evidence': evidence,
            'original_event': event
        }

    def _quarantine(self, event: Dict, reason: str) -> None:
        logger.error(f"Quarantining '{(event.get('title') or '')[:60]}': {reason}")
        if self.dry_run:
            return
        self.db.quarantine_enrichment(event['enriched_event_id'], reason)
        self.stats['quarantined'] += 1

    def _extract_entity_name(self, event: Dict, organisations: Optional[List[str]] = None) -> Optional[str]:
        """The organisations already linked to the record, for the prompt.

        None when none is linked: the prompt then says no entity is
        identified.  (This used to send the title's first word as "currently
        identified entity" - a guess presented to Perplexity as a fact.)
        """
        names = [n for n in (organisations or []) if n]
        return "; ".join(names[:5]) if names else None

    def apply_enrichment_to_database(self, enriched_data: Dict) -> bool:
        """Store an enrichment that has already passed the acceptance gate.

        Facts are written only if they do not weaken what is stored, and the
        record is marked validated with a reliability computed from the
        accepted facts - never a default.

        Returns:
            True only when the write succeeded (or on a dry run).  A failed
            write is recorded as a failed attempt and returns False.
        """
        return self._apply(enriched_data) not in FAILURE_OUTCOMES

    def _apply(self, enriched_data: Dict) -> str:
        """Store an accepted enrichment; returns the outcome (``OUTCOME_*``)."""

        if self.dry_run:
            logger.info(f"[DRY RUN] Would update event: {enriched_data['original_event']['title'][:60]}...")
            self._log_enrichment_changes(enriched_data)
            return OUTCOME_DRY_RUN

        event_id = enriched_data['enriched_event_id']
        original_event = enriched_data['original_event']
        gate: GateResult = enriched_data['gate']

        try:
            outcome = self.db.record_enrichment_success(event_id, gate)
        except CorruptStoredEnrichment as exc:
            self._quarantine(original_event, f"{exc}; baseline unknown - nothing written")
            return OUTCOME_CORRUPT_BASELINE
        except Exception as exc:
            self.stats['storage_failures'] += 1
            self._record_failure(original_event, f"storage error: {str(exc)[:300]}")
            return OUTCOME_STORAGE_FAILED

        field_for_group = {
            'date': 'event_date', 'entity': 'entity_name', 'threat_actor': 'threat_actor',
            'attack_method': 'attack_method', 'victim_count': 'victim_count',
        }
        for group in outcome['applied']:
            self.stats['updated_fields'][field_for_group[group]] += 1
        if outcome.get('pending'):
            # Supported facts are stored; the unsupported confident claims keep
            # the record in the retry queue.
            self.stats['partially_supported'] += 1
            if outcome.get('status') == 'quarantined':
                self.stats['quarantined'] += 1
            logger.info(f"  Not validated - unsupported claims withheld: {', '.join(outcome['pending'])} "
                        f"(status {outcome.get('status')})")
        if gate.sanitized and gate.sanitized.formal_entity_name:
            logger.info(
                f"  Formal entity name: {gate.sanitized.formal_entity_name} "
                f"(confidence: {(gate.sanitized.entity_confidence or 0.0):.2f})"
            )

        logger.info(f"  Updated {len(outcome['applied'])} fields for event: {original_event['title'][:60]}...")
        return OUTCOME_PARTIAL if outcome.get('pending') else OUTCOME_VALIDATED

    def _log_enrichment_changes(self, enriched_data: Dict):
        """Log what changes would be made (for dry run)."""
        enrichment = enriched_data['enrichment']
        original = enriched_data['original_event']

        logger.info(f"  Proposed changes (accepted facts only):")
        if enrichment.earliest_event_date:
            logger.info(f"    Date: {original.get('event_date')} -> {enrichment.earliest_event_date} (conf: {enrichment.date_confidence:.2f})")
        if enrichment.formal_entity_name:
            logger.info(f"    Entity: -> {enrichment.formal_entity_name} (conf: {enrichment.entity_confidence:.2f})")
        if enrichment.threat_actor:
            logger.info(f"    Threat Actor: {original.get('attacking_entity_name')} -> {enrichment.threat_actor} (conf: {enrichment.threat_actor_confidence:.2f})")
        if enrichment.attack_method:
            logger.info(f"    Attack Method: {original.get('attack_method')} -> {enrichment.attack_method} (conf: {enrichment.attack_method_confidence:.2f})")
        if enrichment.victim_count:
            logger.info(f"    Victim Count: {original.get('records_affected')} -> {enrichment.victim_count} (conf: {enrichment.victim_count_confidence:.2f})")

    async def enrich_events_concurrent(
        self,
        events: List[Dict],
        max_concurrent: int = 10,
        progress_log_every: int = 10,
    ) -> Dict[str, int]:
        """Run Perplexity enrichment for many events concurrently.

        Perplexity tolerates ~10 concurrent calls comfortably; this is the same
        semaphore size used by llm_classifier.py and entity_extractor.py.

        Returns:
            Counts (also kept as ``self.last_counts``):

            * ``attempted`` - records processed (``len(events)``);
            * ``enriched`` - facts written: ``validated`` + ``partially_supported``;
            * ``validated`` - written and marked validated;
            * ``partially_supported`` - supported facts written, confident
              unsupported claims withheld, record left in the retry queue;
            * ``failed`` - rejected by the gate, request failed, write failed,
              or quarantined because the stored enrichment is unreadable;
            * ``storage_failed`` / ``corrupt_baseline`` - subsets of ``failed``;
            * ``budget_refused`` - not attempted (API budget spent); neither
              a success nor a failure;
            * ``dry_run`` - accepted on a dry run (nothing written).
        """
        counts: Dict[str, int] = {
            'attempted': len(events), 'enriched': 0, 'validated': 0, 'partially_supported': 0,
            'failed': 0, 'storage_failed': 0, 'corrupt_baseline': 0, 'budget_refused': 0, 'dry_run': 0,
        }
        if not events:
            self.last_counts = counts
            return counts

        semaphore = asyncio.Semaphore(max_concurrent)
        completed = 0
        total = len(events)

        async def _enrich_one(event: Dict) -> str:
            async with semaphore:
                try:
                    outcome, enriched_data = await self._enrich(event)
                    if enriched_data is None:
                        return outcome
                    # _apply is sync but uses self.db._lock internally, so it
                    # is safe to call from async tasks.
                    return self._apply(enriched_data)
                except Exception as exc:
                    logger.warning(f"Failed to enrich event {event.get('enriched_event_id')}: {exc}")
                    self._record_failure(event, f"unexpected error: {str(exc)[:300]}", exc)
                    return OUTCOME_BUDGET_REFUSED if is_budget_refusal(exc) else OUTCOME_REQUEST_FAILED

        tasks = [asyncio.create_task(_enrich_one(event)) for event in events]
        for task in asyncio.as_completed(tasks):
            outcome = await task
            completed += 1
            if outcome in (OUTCOME_VALIDATED, OUTCOME_PARTIAL):
                counts['enriched'] += 1
                counts['validated' if outcome == OUTCOME_VALIDATED else 'partially_supported'] += 1
            elif outcome == OUTCOME_BUDGET_REFUSED:
                counts['budget_refused'] += 1
            elif outcome == OUTCOME_DRY_RUN:
                counts['dry_run'] += 1
            else:
                counts['failed'] += 1
                if outcome in (OUTCOME_STORAGE_FAILED, OUTCOME_CORRUPT_BASELINE):
                    counts[outcome] += 1
            if progress_log_every and completed % progress_log_every == 0:
                logger.info(f"Progress: {completed}/{total} events processed ({counts['enriched']} enriched)")

        self.last_counts = counts
        self.report_quarantined()
        return counts

    def report_quarantined(self, limit: int = 20) -> List[Dict]:
        """Log the records that have used up their enrichment attempts."""
        try:
            quarantined = self.db.get_quarantined_enrichments()
        except Exception as exc:
            logger.warning(f"Could not read quarantined enrichments: {exc}")
            return []
        if quarantined:
            logger.warning(
                f"{len(quarantined)} event(s) quarantined after {MAX_ENRICHMENT_ATTEMPTS} failed "
                "Perplexity attempts; they will not be retried until released "
                "(--release-quarantine):"
            )
            for row in quarantined[:limit]:
                logger.warning(f"  {row['enriched_event_id']}: {(row.get('title') or '')[:60]} "
                               f"- {row.get('perplexity_last_error')}")
            if len(quarantined) > limit:
                logger.warning(f"  ... and {len(quarantined) - limit} more")
        return quarantined

    async def process_backfill(
        self,
        limit: Optional[int] = None,
        priority_only: bool = False,
        max_concurrent: int = 10,
    ):
        """Main backfill processing function."""

        logger.info("=" * 80)
        logger.info("Starting Perplexity Backfill Process")
        logger.info("=" * 80)

        if self.dry_run:
            logger.warning("DRY RUN MODE - No database changes will be made")

        # Get events needing enrichment
        events = self.get_events_needing_enrichment(limit, priority_only)
        self.stats['total_candidates'] = len(events)

        if not events:
            logger.info("No events need enrichment")
            return

        logger.info(f"Processing {len(events)} events with max_concurrent={max_concurrent}...")

        if self.dry_run:
            # Dry run still serial so we can print proposed changes per event
            for i, event in enumerate(events, 1):
                logger.info(f"\nProcessing {i}/{len(events)}: {event['title'][:60]}...")
                try:
                    enriched = await self.enrich_event(event)
                    if enriched:
                        self.apply_enrichment_to_database(enriched)
                        self.stats['enriched_successfully'] += 1
                    else:
                        self.stats['enriched_failed'] += 1
                except Exception as e:
                    logger.error(f"Error processing event: {e}")
                    self.stats['enriched_failed'] += 1
        else:
            counts = await self.enrich_events_concurrent(events, max_concurrent=max_concurrent)
            self.stats['enriched_successfully'] = counts['enriched']
            self.stats['enriched_failed'] = counts['failed']

        # Print summary
        self._print_summary()

    def _print_summary(self):
        """Print processing summary."""
        logger.info("\n" + "=" * 80)
        logger.info("BACKFILL SUMMARY")
        logger.info("=" * 80)
        logger.info(f"Total candidates: {self.stats['total_candidates']}")
        logger.info(f"Successfully enriched: {self.stats['enriched_successfully']}")
        logger.info(f"Failed to enrich: {self.stats['enriched_failed']}")
        logger.info(f"Stored but not validated (unsupported claims withheld): {self.stats['partially_supported']}")
        logger.info(f"Quarantined this run: {self.stats['quarantined']}")
        logger.info(f"Skipped: {self.stats['skipped']}")
        logger.info("\nFields updated:")
        for field, count in self.stats['updated_fields'].items():
            if count > 0:
                logger.info(f"  {field}: {count}")
        logger.info("=" * 80)


async def enrich_waiting(db_path: str, limit: int) -> int:
    """Research the first ``limit`` records waiting for Perplexity (records of unsupported incidents first).

    Used by ``dedup_admin.py recover-evidence --until-empty``. Returns the number of records tried. Raises
    ``RuntimeError`` when ``PERPLEXITY_API_KEY`` is not set: no other model is substituted.
    """
    api_key = ConfigManager().load().get("PERPLEXITY_API_KEY")
    if not api_key:
        raise RuntimeError("PERPLEXITY_API_KEY is not set; records cannot be researched")
    db = CyberEventDataV2(db_path)
    try:
        processor = PerplexityBackfillProcessor(db=db, perplexity_engine=PerplexityEnrichmentEngine(api_key=api_key))
        await processor.process_backfill(limit=limit)
        return int(processor.stats['total_candidates'])
    finally:
        db.close()


async def main():
    """Main function."""
    parser = argparse.ArgumentParser(description="Backfill events with Perplexity enrichment")
    parser.add_argument("--limit", type=int, help="Limit number of events to process")
    parser.add_argument("--priority-only", action="store_true", help="Only process high-priority events (placeholder dates)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    parser.add_argument("--requeue-empty", action="store_true",
                        help="Un-validate records stored as validated with no facts (old unparseable/empty answers) so they are enriched again")
    parser.add_argument("--report-quarantine", action="store_true",
                        help="List quarantined records and exit")
    parser.add_argument("--release-quarantine", action="store_true",
                        help="Put quarantined records back in the retry queue")
    parser.add_argument("--recompute-reliability", action="store_true",
                        help="Re-judge every stored enrichment with the current gate (cited pages are read "
                             "and cached), recompute data_source_reliability and withhold unsupported facts; "
                             "with --dry-run, report only. Needs no Perplexity key.")
    parser.add_argument("--fetch-workers", type=int, default=8,
                        help="Concurrent cited-page fetches for --recompute-reliability (default 8)")
    parser.add_argument("--db-path", default="instance/cyber_events.db",
                        help="Database file (default: instance/cyber_events.db)")

    args = parser.parse_args()

    # Load environment configuration
    config_manager = ConfigManager()
    env_config = config_manager.load()
    
    # Initialize database
    db = CyberEventDataV2(args.db_path)

    if args.recompute_reliability:
        from cyber_data_collector.processing.reliability_recheck import recheck_stored_enrichments

        store = CitedDocumentStore(db._conn, db._lock, max_workers=args.fetch_workers)
        try:
            report = recheck_stored_enrichments(db._conn, store, dry_run=args.dry_run, limit=args.limit)
        finally:
            db.close()
        logger.info("%sRecompute reliability:", "[DRY RUN] " if args.dry_run else "")
        for line in report.lines():
            logger.info("  %s", line)
        return 0

    # Maintenance actions that need no Perplexity key.
    if args.requeue_empty:
        logger.info(f"Re-queued {db.requeue_empty_validations()} empty validation(s)")
    if args.release_quarantine:
        logger.info(f"Released {db.release_quarantined_enrichments()} quarantined record(s)")
    if args.report_quarantine:
        rows = db.get_quarantined_enrichments()
        logger.info(f"{len(rows)} quarantined record(s)")
        for row in rows:
            logger.info(f"  {row['enriched_event_id']}: {(row.get('title') or '')[:60]} - {row.get('perplexity_last_error')}")
        db.close()
        return 0

    # Initialize Perplexity engine with API key from .env
    api_key = env_config.get("PERPLEXITY_API_KEY")
    if not api_key:
        logger.error("Perplexity API not configured. Set PERPLEXITY_API_KEY in .env file.")
        return 1
    
    # Raises (naming what is missing) if the client cannot be created.
    perplexity_engine = PerplexityEnrichmentEngine(api_key=api_key)

    # Initialize processor
    processor = PerplexityBackfillProcessor(
        db=db,
        perplexity_engine=perplexity_engine,
        dry_run=args.dry_run
    )

    try:
        # Run backfill
        await processor.process_backfill(
            limit=args.limit,
            priority_only=args.priority_only
        )

    finally:
        db.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
