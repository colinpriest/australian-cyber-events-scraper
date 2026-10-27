#!/usr/bin/env python3
"""
Backfill existing events with Perplexity enrichment.

This script:
1. Identifies events that need Perplexity validation
2. Prioritizes events with placeholder dates (1st of month)
3. Enriches events with validated data from Perplexity
4. Updates the database with enriched information
5. Triggers re-deduplication after enrichment

Usage:
    python scripts/perplexity_backfill_events.py [--limit N] [--priority-only] [--dry-run]
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional

from cyber_data_collector.storage.cyber_event_data_v2 import CyberEventDataV2
from cyber_data_collector.processing.enrichment_gate import (
    MAX_ENRICHMENT_ATTEMPTS,
    GateResult,
    evaluate_enrichment,
)
from cyber_data_collector.processing.perplexity_enrichment import PerplexityEnrichmentEngine
from cyber_data_collector.processing.record_evidence import RecordEvidence
from cyber_data_collector.utils import ConfigManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


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
        dry_run: bool = False
    ):
        self.db = db
        self.perplexity_engine = perplexity_engine
        self.dry_run = dry_run
        self.stats = {
            'total_candidates': 0,
            'enriched_successfully': 0,
            'enriched_failed': 0,
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
                    COALESCE(ee.perplexity_attempt_count, 0) AS perplexity_attempt_count,
                    -- The persisted record: enrichment reads the scraped article
                    -- stored here, not the processed description above.
                    re.raw_title,
                    re.raw_description,
                    re.raw_content,
                    re.source_url,
                    re.source_type,
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

            # Order by priority: placeholder dates first, then by date
            query += """
                ORDER BY
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

    def _record_failure(self, event: Dict, reason: str) -> None:
        """Record a failed attempt (or just log it on a dry run)."""
        logger.warning(f"Enrichment not accepted for '{(event.get('title') or '')[:60]}': {reason}")
        if self.dry_run:
            return
        try:
            status = self.db.record_enrichment_failure(event['enriched_event_id'], reason)
            if status == 'quarantined':
                self.stats['quarantined'] += 1
        except Exception as exc:  # bookkeeping must not abort the batch
            logger.error(f"Could not record failed attempt for {event.get('enriched_event_id')}: {exc}")

    async def enrich_event(self, event: Dict) -> Optional[Dict]:
        """Enrich a single event with Perplexity and gate the answer.

        The answer is judged against the record's *persisted* text, and only an
        answer that passes the acceptance gate is returned.  Anything else is
        recorded as a failed attempt (retried on later runs, quarantined after
        MAX_ENRICHMENT_ATTEMPTS) and None is returned.
        """

        logger.info(f"Enriching event: {event['title'][:60]}...")

        # Extract current entity name from title or description
        current_entity = self._extract_entity_name(event)

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
            self._record_failure(event, f"request failed: {str(exc)[:300]}")
            return None

        if enrichment is None:
            # No attempt was made (client unavailable); nothing to count.
            logger.warning(f"Perplexity made no attempt for event: {event['title'][:60]}...")
            return None

        gate = evaluate_enrichment(
            enrichment,
            record_text=evidence.match_text(),
            reference_date=_parse_day(evidence.discovered_at),
        )
        if not gate.is_accepted:
            self._record_failure(event, gate.failure_reason or "enrichment rejected by the acceptance gate")
            return None

        for fact, why in gate.rejected_facts.items():
            logger.info(f"  Dropped {fact}: {why}")
        logger.info(
            f"Enriched successfully (overall confidence: {gate.sanitized.overall_confidence:.2f}, "
            f"reliability: {gate.reliability})"
        )

        return {
            'enriched_event_id': event['enriched_event_id'],
            'enrichment': gate.sanitized,
            'gate': gate,
            'evidence': evidence,
            'original_event': event
        }

    def _extract_entity_name(self, event: Dict) -> Optional[str]:
        """Extract entity name from event data."""
        # Simple extraction - could be enhanced
        title = event.get('title', '')
        # Check if there's an entity in attacking_entity_name
        if event.get('attacking_entity_name'):
            return None  # This is the attacker, not the victim

        # Try to extract from title - very basic approach
        parts = title.split(' ')
        if len(parts) > 0:
            return parts[0]  # First word as a guess

        return None

    def apply_enrichment_to_database(self, enriched_data: Dict) -> bool:
        """Store an enrichment that has already passed the acceptance gate.

        Facts are written only if they do not weaken what is stored, and the
        record is marked validated with a reliability computed from the
        accepted facts - never a default.
        """

        if self.dry_run:
            logger.info(f"[DRY RUN] Would update event: {enriched_data['original_event']['title'][:60]}...")
            self._log_enrichment_changes(enriched_data)
            return True

        event_id = enriched_data['enriched_event_id']
        original_event = enriched_data['original_event']
        gate: GateResult = enriched_data['gate']

        try:
            outcome = self.db.record_enrichment_success(event_id, gate)
        except Exception as exc:
            self._record_failure(original_event, f"storage error: {str(exc)[:300]}")
            return False

        field_for_group = {
            'date': 'event_date', 'entity': 'entity_name', 'threat_actor': 'threat_actor',
            'attack_method': 'attack_method', 'victim_count': 'victim_count',
        }
        for group in outcome['applied']:
            self.stats['updated_fields'][field_for_group[group]] += 1
        if gate.sanitized and gate.sanitized.formal_entity_name:
            logger.info(
                f"  Formal entity name: {gate.sanitized.formal_entity_name} "
                f"(confidence: {(gate.sanitized.entity_confidence or 0.0):.2f})"
            )

        logger.info(f"  Updated {len(outcome['applied'])} fields for event: {original_event['title'][:60]}...")
        return True

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
        Replaces a serial loop that took ~3-6 sec per event.

        Returns counts dict: {'enriched': N, 'failed': M}
        """
        if not events:
            return {'enriched': 0, 'failed': 0}

        semaphore = asyncio.Semaphore(max_concurrent)
        enriched_count = 0
        failed_count = 0
        completed = 0
        total = len(events)

        async def _enrich_one(event: Dict) -> bool:
            async with semaphore:
                try:
                    enriched_data = await self.enrich_event(event)
                    if enriched_data:
                        # apply_enrichment_to_database is sync but uses self.db._lock
                        # internally, so it is safe to call from async tasks.
                        self.apply_enrichment_to_database(enriched_data)
                        return True
                    return False
                except Exception as exc:
                    logger.warning(f"Failed to enrich event {event.get('enriched_event_id')}: {exc}")
                    self._record_failure(event, f"unexpected error: {str(exc)[:300]}")
                    return False

        tasks = [asyncio.create_task(_enrich_one(event)) for event in events]
        for task in asyncio.as_completed(tasks):
            success = await task
            completed += 1
            if success:
                enriched_count += 1
            else:
                failed_count += 1
            if progress_log_every and completed % progress_log_every == 0:
                logger.info(f"Progress: {completed}/{total} events processed ({enriched_count} enriched)")

        self.report_quarantined()
        return {'enriched': enriched_count, 'failed': failed_count}

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
        logger.info(f"Quarantined this run: {self.stats['quarantined']}")
        logger.info(f"Skipped: {self.stats['skipped']}")
        logger.info("\nFields updated:")
        for field, count in self.stats['updated_fields'].items():
            if count > 0:
                logger.info(f"  {field}: {count}")
        logger.info("=" * 80)


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

    args = parser.parse_args()

    # Load environment configuration
    config_manager = ConfigManager()
    env_config = config_manager.load()
    
    # Initialize database
    db = CyberEventDataV2()

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
    
    perplexity_engine = PerplexityEnrichmentEngine(api_key=api_key)

    if not perplexity_engine.client:
        logger.error("Failed to initialize Perplexity client. Check your API key.")
        return 1

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
