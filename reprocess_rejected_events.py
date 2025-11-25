#!/usr/bin/env python3
"""
Re-process rejected raw events using Perplexity AI enrichment.

This script targets raw events that were rejected by GPT-4o-mini during initial
discovery, giving them a second chance with the more sophisticated Perplexity AI.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime
from typing import List, Optional
from dotenv import load_dotenv

from cyber_event_data_v2 import CyberEventDataV2
from cyber_data_collector.processing.perplexity_enrichment import PerplexityEnrichmentEngine

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('reprocess_rejected_events.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class RejectedEventReprocessor:
    """Re-processes rejected raw events using Perplexity AI."""

    def __init__(self, db_path: str = "instance/cyber_events.db"):
        self.db = CyberEventDataV2(db_path)
        self.perplexity_api_key = os.getenv('PERPLEXITY_API_KEY')

        if not self.perplexity_api_key:
            raise ValueError("PERPLEXITY_API_KEY not found in environment")

        self.perplexity_engine = PerplexityEnrichmentEngine(self.perplexity_api_key)

        self.stats = {
            'loaded': 0,
            'enriched': 0,
            'failed': 0,
            'skipped': 0
        }

    def get_rejected_events(self, start_month: str, end_month: str, limit: Optional[int] = None) -> List:
        """
        Get raw events that were rejected by GPT-4o-mini.

        Args:
            start_month: Start month in YYYY-MM format
            end_month: End month in YYYY-MM format
            limit: Maximum number of events to process

        Returns:
            List of rejected raw event records
        """
        query = """
            SELECT r.*
            FROM RawEvents r
            LEFT JOIN EnrichedEvents e ON r.raw_event_id = e.raw_event_id
            WHERE r.is_processed = 1
              AND e.enriched_event_id IS NULL
              AND strftime('%Y-%m', r.event_date) >= ?
              AND strftime('%Y-%m', r.event_date) <= ?
            ORDER BY r.event_date DESC
        """

        if limit:
            query += f" LIMIT {limit}"

        with self.db.get_connection() as conn:
            cursor = conn.execute(query, (start_month, end_month))
            return cursor.fetchall()

    async def enrich_raw_event(self, raw_event) -> Optional[dict]:
        """
        Enrich a raw event using Perplexity AI.

        Args:
            raw_event: Raw event record from database

        Returns:
            Enriched event data dict, or None if enrichment failed
        """
        try:
            # Prepare event data for Perplexity
            event_data = {
                'title': raw_event['raw_title'],
                'description': raw_event['raw_description'],
                'event_date': raw_event['event_date'],
                'source_url': raw_event['source_url'],
                'source_type': raw_event['source_type']
            }

            logger.info(f"Enriching: {raw_event['raw_title'][:80]}...")

            # Use Perplexity to enrich
            enriched_data = await self.perplexity_engine.enrich_event(event_data)

            if not enriched_data:
                logger.warning(f"Perplexity returned no data for event {raw_event['raw_event_id']}")
                return None

            # Check if Perplexity considers it a valid Australian cyber event
            if not enriched_data.get('is_australian_event', False):
                logger.info(f"Event rejected by Perplexity as non-Australian: {raw_event['raw_title'][:80]}")
                return None

            # Add raw_event_id for linking
            enriched_data['raw_event_id'] = raw_event['raw_event_id']

            return enriched_data

        except Exception as e:
            logger.error(f"Error enriching event {raw_event['raw_event_id']}: {e}")
            return None

    def save_enriched_event(self, enriched_data: dict) -> bool:
        """
        Save enriched event to database.

        Args:
            enriched_data: Enriched event data

        Returns:
            True if saved successfully
        """
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    INSERT INTO EnrichedEvents (
                        raw_event_id,
                        title,
                        summary,
                        event_date,
                        victim_organization_name,
                        attack_type,
                        severity,
                        records_affected,
                        perplexity_validated,
                        perplexity_enrichment_data,
                        is_australian_event,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    enriched_data['raw_event_id'],
                    enriched_data.get('title'),
                    enriched_data.get('summary'),
                    enriched_data.get('event_date'),
                    enriched_data.get('victim_organization_name'),
                    enriched_data.get('attack_type'),
                    enriched_data.get('severity'),
                    enriched_data.get('records_affected'),
                    1,  # perplexity_validated
                    str(enriched_data),  # Store full data as JSON string
                    1,  # is_australian_event
                    datetime.now().isoformat()
                ))
                conn.commit()

                logger.info(f"✓ Saved enriched event: {enriched_data.get('title', '')[:80]}")
                return True

        except Exception as e:
            logger.error(f"Error saving enriched event: {e}")
            return False

    async def process_rejected_events(
        self,
        start_month: str = "2025-09",
        end_month: str = "2025-10",
        limit: Optional[int] = None
    ):
        """
        Main processing logic.

        Args:
            start_month: Start month in YYYY-MM format
            end_month: End month in YYYY-MM format
            limit: Maximum number of events to process
        """
        logger.info(f"Loading rejected events from {start_month} to {end_month}...")

        rejected_events = self.get_rejected_events(start_month, end_month, limit)
        self.stats['loaded'] = len(rejected_events)

        logger.info(f"Found {len(rejected_events)} rejected events to reprocess")

        if not rejected_events:
            logger.info("No rejected events found")
            return

        # Process each event
        for idx, raw_event in enumerate(rejected_events, 1):
            logger.info(f"\nProcessing {idx}/{len(rejected_events)}: {raw_event['raw_title'][:80]}")

            try:
                # Enrich with Perplexity
                enriched_data = await self.enrich_raw_event(raw_event)

                if enriched_data:
                    # Save to database
                    if self.save_enriched_event(enriched_data):
                        self.stats['enriched'] += 1
                    else:
                        self.stats['failed'] += 1
                else:
                    self.stats['skipped'] += 1

                # Progress update every 10 events
                if idx % 10 == 0:
                    logger.info(
                        f"Progress: {idx}/{len(rejected_events)} | "
                        f"Enriched: {self.stats['enriched']} | "
                        f"Skipped: {self.stats['skipped']} | "
                        f"Failed: {self.stats['failed']}"
                    )

            except Exception as e:
                logger.error(f"Error processing event {raw_event['raw_event_id']}: {e}")
                self.stats['failed'] += 1

        # Final summary
        logger.info("\n" + "="*80)
        logger.info("REPROCESSING COMPLETE")
        logger.info("="*80)
        logger.info(f"Rejected events loaded:  {self.stats['loaded']}")
        logger.info(f"Successfully enriched:   {self.stats['enriched']}")
        logger.info(f"Skipped (not Australian): {self.stats['skipped']}")
        logger.info(f"Failed:                  {self.stats['failed']}")
        logger.info(f"Recovery rate:           {self.stats['enriched'] / self.stats['loaded'] * 100:.1f}%")
        logger.info("="*80)


async def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Re-process rejected events with Perplexity AI")
    parser.add_argument('--start-month', default='2025-09', help='Start month (YYYY-MM)')
    parser.add_argument('--end-month', default='2025-10', help='End month (YYYY-MM)')
    parser.add_argument('--limit', type=int, help='Limit number of events to process')
    parser.add_argument('--db-path', default='instance/cyber_events.db', help='Path to database')

    args = parser.parse_args()

    logger.info("="*80)
    logger.info("REJECTED EVENT REPROCESSOR - Using Perplexity AI")
    logger.info("="*80)
    logger.info(f"Date range: {args.start_month} to {args.end_month}")
    logger.info(f"Database: {args.db_path}")
    if args.limit:
        logger.info(f"Limit: {args.limit} events")
    logger.info("="*80)

    try:
        processor = RejectedEventReprocessor(args.db_path)
        await processor.process_rejected_events(
            start_month=args.start_month,
            end_month=args.end_month,
            limit=args.limit
        )
    except KeyboardInterrupt:
        logger.info("\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
