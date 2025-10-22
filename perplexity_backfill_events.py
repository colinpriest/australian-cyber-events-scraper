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
    python perplexity_backfill_events.py [--limit N] [--priority-only] [--dry-run]
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

# Add parent directory to path for imports
sys.path.append(str(Path(__file__).parent))

from cyber_event_data_v2 import CyberEventDataV2
from cyber_data_collector.processing.perplexity_enrichment import PerplexityEnrichmentEngine
from cyber_data_collector.utils import ConfigManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


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
                    ee.attack_method
                FROM EnrichedEvents ee
                WHERE ee.status = 'Active'
                  AND (ee.perplexity_validated IS NULL OR ee.perplexity_validated = FALSE)
                  AND ee.is_specific_event = TRUE
            """

            params = []

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
                    ee.event_date DESC,
                    ee.confidence_score ASC
            """

            if limit:
                query += f" LIMIT {limit}"

            cursor.execute(query, params)
            events = [dict(row) for row in cursor.fetchall()]

            logger.info(f"Found {len(events)} events needing enrichment")
            return events

    async def enrich_event(self, event: Dict) -> Optional[Dict]:
        """Enrich a single event with Perplexity."""

        logger.info(f"Enriching event: {event['title'][:60]}...")

        # Extract current entity name from title or description
        current_entity = self._extract_entity_name(event)

        # Call Perplexity for enrichment
        enrichment = await self.perplexity_engine.enrich_event(
            title=event['title'],
            description=event.get('description') or event.get('summary', ''),
            current_date=str(event.get('event_date')) if event.get('event_date') else None,
            current_entity=current_entity
        )

        if not enrichment:
            logger.warning(f"Failed to enrich event: {event['title'][:60]}...")
            return None

        logger.info(
            f"Enriched successfully (overall confidence: {enrichment.overall_confidence:.2f})"
        )

        return {
            'enriched_event_id': event['enriched_event_id'],
            'enrichment': enrichment,
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
        """Apply Perplexity enrichment to the database."""

        if self.dry_run:
            logger.info(f"[DRY RUN] Would update event: {enriched_data['original_event']['title'][:60]}...")
            self._log_enrichment_changes(enriched_data)
            return True

        event_id = enriched_data['enriched_event_id']
        enrichment = enriched_data['enrichment']
        original_event = enriched_data['original_event']

        updates = []
        values = []

        # Update event_date if Perplexity has higher confidence
        if enrichment.earliest_event_date and enrichment.date_confidence and enrichment.date_confidence >= 0.6:
            updates.append("event_date = ?")
            values.append(enrichment.earliest_event_date)
            updates.append("date_confidence = ?")
            values.append(enrichment.date_confidence)
            self.stats['updated_fields']['event_date'] += 1

        # Update entity name if available (would need to link to EntitiesV2 table)
        # For now, we'll store it in a comment or log it
        if enrichment.formal_entity_name and enrichment.entity_confidence and enrichment.entity_confidence >= 0.6:
            updates.append("entity_confidence = ?")
            values.append(enrichment.entity_confidence)
            self.stats['updated_fields']['entity_name'] += 1
            logger.info(f"  Formal entity name: {enrichment.formal_entity_name} (confidence: {enrichment.entity_confidence:.2f})")

        # Update threat actor
        if enrichment.threat_actor and enrichment.threat_actor_confidence and enrichment.threat_actor_confidence >= 0.6:
            updates.append("attacking_entity_name = ?")
            values.append(enrichment.threat_actor)
            self.stats['updated_fields']['threat_actor'] += 1

        # Update attack method
        if enrichment.attack_method and enrichment.attack_method_confidence and enrichment.attack_method_confidence >= 0.6:
            updates.append("attack_method = ?")
            values.append(enrichment.attack_method)
            self.stats['updated_fields']['attack_method'] += 1

        # Update victim count
        if enrichment.victim_count and enrichment.victim_count_confidence and enrichment.victim_count_confidence >= 0.6:
            updates.append("records_affected = ?")
            values.append(enrichment.victim_count)
            self.stats['updated_fields']['victim_count'] += 1

        # Always update Perplexity validation metadata
        updates.append("perplexity_validated = ?")
        values.append(True)
        updates.append("perplexity_validated_at = ?")
        values.append(datetime.now().isoformat())
        updates.append("perplexity_enrichment_data = ?")
        values.append(json.dumps(enrichment.model_dump(), default=str))
        updates.append("data_source_reliability = ?")
        values.append(0.85)  # Perplexity gets high reliability score

        # Execute update
        if updates:
            with self.db._lock:
                cursor = self.db._conn.cursor()
                query = f"""
                    UPDATE EnrichedEvents
                    SET {', '.join(updates)}, updated_at = ?
                    WHERE enriched_event_id = ?
                """
                values.extend([datetime.now().isoformat(), event_id])

                cursor.execute(query, values)
                self.db._conn.commit()

            logger.info(f"  Updated {len(updates)} fields for event: {original_event['title'][:60]}...")
            return True

        return False

    def _log_enrichment_changes(self, enriched_data: Dict):
        """Log what changes would be made (for dry run)."""
        enrichment = enriched_data['enrichment']
        original = enriched_data['original_event']

        logger.info(f"  Proposed changes:")
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

    async def process_backfill(
        self,
        limit: Optional[int] = None,
        priority_only: bool = False
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

        logger.info(f"Processing {len(events)} events...")

        # Process each event
        for i, event in enumerate(events, 1):
            logger.info(f"\nProcessing {i}/{len(events)}: {event['title'][:60]}...")

            try:
                # Enrich with Perplexity
                enriched = await self.enrich_event(event)

                if enriched:
                    # Apply enrichment to database
                    success = self.apply_enrichment_to_database(enriched)
                    if success:
                        self.stats['enriched_successfully'] += 1
                    else:
                        self.stats['skipped'] += 1
                else:
                    self.stats['enriched_failed'] += 1

                # Rate limiting - wait between requests
                if i < len(events):  # Don't wait after last event
                    await asyncio.sleep(2.0)

            except Exception as e:
                logger.error(f"Error processing event: {e}")
                self.stats['enriched_failed'] += 1

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

    args = parser.parse_args()

    # Load environment configuration
    config_manager = ConfigManager()
    env_config = config_manager.load()
    
    # Initialize database
    db = CyberEventDataV2()

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
