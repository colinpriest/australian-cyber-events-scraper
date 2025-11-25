#!/usr/bin/env python3
"""
Process PerplexityResearch events that are sitting in RawEvents.

These events don't need scraping since they already contain the full text from Perplexity.
This script directly creates EnrichedEvents from them and marks them as processed.
"""

import logging
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
sys.path.append(str(Path(__file__).parent))

from cyber_event_data_v2 import CyberEventDataV2

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def process_perplexity_research_events():
    """Process unprocessed PerplexityResearch events."""

    db = CyberEventDataV2()

    # Get unprocessed PerplexityResearch events
    events = db.get_unprocessed_raw_events(source_types=['PerplexityResearch'])

    logger.info(f"Found {len(events)} unprocessed PerplexityResearch events")

    processed_count = 0

    for event in events:
        try:
            # Create enriched event directly
            # The event already has good text from Perplexity research
            enriched_data = {
                'title': event['raw_title'],
                'description': event['raw_description'] or event['raw_content'],
                'event_date': event.get('event_date'),
                'is_australian_event': True,  # We specifically searched for Australian events
                'is_specific_event': True,  # Each is a specific incident
                'confidence_score': 0.8,  # High confidence from Perplexity research
                'australian_relevance_score': 1.0,  # Specifically searched for Australian events
                'status': 'Active'
            }

            # Create enriched event
            enriched_id = db.create_enriched_event(
                raw_event_id=event['raw_event_id'],
                enriched_data=enriched_data
            )

            # Mark raw event as processed
            db.mark_raw_event_processed(event['raw_event_id'])

            logger.info(f"Processed: {event['raw_title'][:60]}... → {enriched_id}")
            processed_count += 1

        except Exception as e:
            logger.error(f"Error processing event {event['raw_event_id']}: {e}")

    logger.info(f"Successfully processed {processed_count} PerplexityResearch events")
    logger.info("These events will now be enriched with Perplexity AI in the next pipeline run")

    return processed_count


if __name__ == "__main__":
    count = process_perplexity_research_events()

    if count > 0:
        logger.info("\n" + "="*80)
        logger.info("NEXT STEP: Run the full pipeline to enrich these events:")
        logger.info("  python run_full_pipeline.py")
        logger.info("="*80)
