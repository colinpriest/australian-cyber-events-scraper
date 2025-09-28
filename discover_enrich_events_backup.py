#!/usr/bin/env python3
"""
Australian Cyber Events Discovery and Enrichment Pipeline

This script combines event discovery from multiple sources with intelligent enrichment
to create a comprehensive database of Australian cyber security events.

Features:
- Discovers events from GDELT, Perplexity, Google Search, and Webber Insurance
- Scrapes URLs for full content (especially GDELT events)
- Uses LLM analysis to filter for Australian-specific cyber events
- Creates separate raw and enriched event records
- Tracks processing status to avoid duplicate work
- Comprehensive logging and error handling

Usage:
    python discover_enrich_events.py [--discover] [--enrich] [--max-events N] [--source SOURCE]
"""

import argparse
import asyncio
import concurrent.futures
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from tqdm import tqdm

# Handle Windows encoding issues
if sys.platform == "win32" and sys.stdout.encoding != "utf-8":
    import codecs
    sys.stdout = codecs.getwriter("utf-8")(sys.stdout.buffer)

from dotenv import load_dotenv

# Local imports
from cyber_event_data_v2 import CyberEventDataV2
from entity_scraper import SeleniumScraper
from llm_extractor import extract_event_details_with_llm

# Data collection imports
from cyber_data_collector import CyberDataCollector, CollectionConfig, DateRange
from cyber_data_collector.models.config import DataSourceConfig
from cyber_data_collector.utils import ConfigManager


# Configure logging with Unicode support
class UnicodeStreamHandler(logging.StreamHandler):
    def emit(self, record):
        try:
            msg = self.format(record)
            # Remove Unicode characters that cause issues on Windows
            msg = msg.encode('ascii', errors='replace').decode('ascii')
            self.stream.write(msg + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        UnicodeStreamHandler(),
        logging.FileHandler('discover_enrich_events.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


class HttpQuietFilter(logging.Filter):
    """Filter out verbose HTTP request logs from httpx/urllib3."""

    noisy_prefixes = (
        "HTTP Request:",
        "HTTP Response:",
    )

    def filter(self, record: logging.LogRecord) -> bool:  # pragma: no cover - simple filter
        message = record.getMessage()
        return not any(message.startswith(prefix) for prefix in self.noisy_prefixes)


# Apply filter to httpx/urllib3 loggers
for noisy_logger in ("httpx", "httpcore", "urllib3", "openai", "instructor"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)
    logging.getLogger(noisy_logger).addFilter(HttpQuietFilter())


class EventDiscoveryEnrichmentPipeline:
    """Main pipeline for discovering and enriching cyber events"""

    def __init__(self, db_path: str = "instance/cyber_events.db"):
        self.db = CyberEventDataV2(db_path)
        self.config_manager = ConfigManager()
        self.env_config = self.config_manager.load()

        # Statistics
        self.stats = {
            'events_discovered': 0,
            'events_scraped': 0,
            'events_analyzed': 0,
            'events_enriched': 0,
            'errors': 0
        }

    # =========================================================================
    # DISCOVERY PHASE
    # =========================================================================

    async def discover_events(self, source_types: List[str] = None, date_range_days: int = 7, max_events: int = 1000):
        """
        Discover new events from configured data sources.

        Args:
            source_types: List of sources to use ('GDELT', 'Perplexity', 'GoogleSearch', 'WebberInsurance')
            date_range_days: How many days back to search
            max_events: Maximum events per source
        """
        logger.info(f"[DISCOVERY] Starting event discovery from {start_date.date()} to {end_date.date()} with max {max_events} events per source")

        # Default to all sources if none specified
        if not source_types:
            source_types = ['GDELT', 'Perplexity', 'GoogleSearch', 'WebberInsurance', 'OAIC']

        # Setup date range - Fixed to June 2025 range
        start_date = datetime(2025, 6, 1)  # 01-Jun-2025
        end_date = datetime(2025, 6, 30)   # 30-Jun-2025
        date_range = DateRange(start_date=start_date, end_date=end_date)

        logger.info(f"[DISCOVERY] Using fixed date range: {start_date.date()} to {end_date.date()}")

        # Configure data sources
        collection_config = self._build_collection_config(source_types, max_events, date_range)

        try:
            # Create collector and run discovery
            collector = CyberDataCollector(collection_config, ".env")
            events = await collector.collect_all_events()

            logger.info(f"[COLLECTION] Discovered {len(events)} total events from all sources")

            # Store as raw events
            for event in events:
                await self._store_raw_event(event)

            self.stats['events_discovered'] = len(events)
            logger.info(f"[SUCCESS] Discovery complete: {self.stats['events_discovered']} events stored")

        except Exception as e:
            logger.error(f"[ERROR] Discovery failed: {e}")
            self.stats['errors'] += 1
            raise

    def _build_collection_config(self, source_types: List[str], max_events: int, date_range: DateRange) -> CollectionConfig:
        """Build collection configuration for specified sources"""

        # Configure individual sources based on what's requested
        gdelt_config = DataSourceConfig(
            enabled='GDELT' in source_types,
            custom_config={
                'max_records': max_events,
                'access_method': 'bigquery'  # Use BigQuery for better results
            }
        )

        perplexity_config = DataSourceConfig(
            enabled='Perplexity' in source_types
        )

        google_search_config = DataSourceConfig(
            enabled='GoogleSearch' in source_types
        )

        webber_config = DataSourceConfig(
            enabled='WebberInsurance' in source_types
        )

        oaic_config = DataSourceConfig(
            enabled='OAIC' in source_types
        )

        return CollectionConfig(
            date_range=date_range,
            max_threads=4,
            batch_size=max_events,
            enable_deduplication=True,
            confidence_threshold=0.6,
            gdelt_config=gdelt_config,
            perplexity_config=perplexity_config,
            google_search_config=google_search_config,
            webber_config=webber_config,
            oaic_config=oaic_config
        )

    async def _store_raw_event(self, event):
        """Store a discovered event as a raw event, avoiding duplicates"""
        try:
            # Determine source type from data sources
            source_type = self._determine_source_type(event.data_sources)
            source_url = event.data_sources[0].url if event.data_sources else None
            title = event.title

            # Check if this event already exists
            if source_url and title:
                existing_id = self.db.find_existing_raw_event(source_type, source_url, title)
                if existing_id:
                    logger.debug(f"[DUPLICATE] Skipping duplicate event {existing_id} from {source_type}: {title[:50]}...")
                    return existing_id

            # Prepare raw event data
            raw_data = {
                'source_event_id': event.external_ids.get('gdelt_id') if hasattr(event, 'external_ids') else None,
                'title': title,
                'description': event.description,
                'content': None,  # Will be filled during scraping phase
                'event_date': event.event_date.date() if event.event_date else None,
                'source_url': source_url,
                'metadata': {
                    'event_type': str(event.event_type) if hasattr(event, 'event_type') else None,
                    'severity': str(event.severity) if hasattr(event, 'severity') else None,
                    'confidence': event.confidence.overall if hasattr(event, 'confidence') and event.confidence else None,
                    'australian_relevance': event.australian_relevance if hasattr(event, 'australian_relevance') else None,
                    'data_sources_count': len(event.data_sources) if event.data_sources else 0
                }
            }

            # Store in database
            raw_event_id = self.db.add_raw_event(source_type, raw_data)
            logger.debug(f"[STORED] Stored raw event {raw_event_id} from {source_type}: {event.title[:50]}...")
            return raw_event_id

        except Exception as e:
            logger.warning(f"[WARNING] Failed to store raw event '{event.title[:50]}...': {e}")
            return None

    def _determine_source_type(self, data_sources) -> str:
        """Determine source type from event data sources"""
        if not data_sources:
            return 'Unknown'

        # Check source identifiers
        for source in data_sources:
            if hasattr(source, 'source_type'):
                if 'GDELT' in source.source_type or 'gdelt' in source.source_type.lower():
                    return 'GDELT'
                elif 'Perplexity' in source.source_type:
                    return 'Perplexity'
                elif 'Google' in source.source_type:
                    return 'GoogleSearch'
                elif 'Webber' in source.source_type:
                    return 'WebberInsurance'
                elif 'OAIC' in source.source_type:
                    return 'OAIC'

            # Check URLs as fallback
            if hasattr(source, 'url') and source.url:
                url = source.url.lower()
                if 'gdelt' in url:
                    return 'GDELT'
                elif 'perplexity' in url:
                    return 'Perplexity'
                elif 'google' in url or 'googleapis' in url:
                    return 'GoogleSearch'
                elif 'webber' in url:
                    return 'WebberInsurance'
                elif 'oaic.gov.au' in url:
                    return 'OAIC'

        return 'Unknown'

    # =========================================================================
    # SCRAPING PHASE
    # =========================================================================

    async def scrape_event_urls(self, max_events: Optional[int] = None, source_types: List[str] = None):
        """
        Scrape URLs from raw events to get full content.
        This is especially important for GDELT events which often only have titles.

        Args:
            max_events: Maximum events to process
            source_types: Source types to process, or None for all
        """
        logger.info("[SCRAPING] Starting URL scraping phase" + (f" for max {max_events} events" if max_events else " without limit"))

        # Get events that need scraping
        events_to_scrape = self.db.get_unprocessed_raw_events(source_types, max_events)

        if not events_to_scrape:
            logger.info("[QUEUE] No events need URL scraping")
            return

        # Filter for events with URLs
        events_with_urls = [e for e in events_to_scrape if e.get('source_url')]
        logger.info(f"[SCRAPING] Found {len(events_with_urls)} events with URLs to scrape")

        # Process events in batches with threading
        scraped_count = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            # Submit all scraping tasks
            future_to_event = {
                executor.submit(self._scrape_single_event, event): event
                for event in events_with_urls
            }

            # Process completed tasks with progress bar
            with tqdm(total=len(events_with_urls), desc="Web Scraping URLs", unit="URL") as pbar:
                for future in concurrent.futures.as_completed(future_to_event):
                    event = future_to_event[future]
                    try:
                        success = future.result()
                        if success:
                            scraped_count += 1
                            pbar.set_postfix({"success": f"{scraped_count}/{len(events_with_urls)}"})
                        else:
                            pbar.set_postfix({"failed": "Content too short"})
                    except Exception as e:
                        logger.error(f"[ERROR] Scraping error for {event.get('raw_title', 'Unknown')}: {e}")
                        # Log failed attempt
                        self.db.log_processing_attempt(
                            event['raw_event_id'], 'url_scraping', 'failed',
                            error_message=str(e)
                        )
                        self.stats['errors'] += 1
                        pbar.set_postfix({"error": "Exception"})
                    pbar.update(1)

        self.stats['events_scraped'] = scraped_count
        logger.info(f"[SUCCESS] Scraping complete: {scraped_count} events successfully scraped")

    def _scrape_single_event(self, event: Dict[str, Any]) -> bool:
        """
        Scrape a single event URL and update the database.

        Args:
            event: Raw event dictionary

        Returns:
            True if successful, False otherwise
        """
        scraper = None
        start_time = time.time()

        try:
            url = event['source_url']
            if not url:
                return False

            # Scraping URL

            scraper = SeleniumScraper()
            content = scraper.get_page_text(url)

            processing_time_ms = int((time.time() - start_time) * 1000)

            if content and len(content.strip()) > 50:  # Minimum viable content
                # Update raw event with scraped content
                self._update_raw_event_content(event['raw_event_id'], content, url)

                # Log successful scraping
                self.db.log_processing_attempt(
                    event['raw_event_id'], 'url_scraping', 'success',
                    result_data={'content_length': len(content), 'url': url},
                    processing_time_ms=processing_time_ms
                )

                # Successfully scraped content
                return True
            else:
                # Log failed scraping (insufficient content)
                self.db.log_processing_attempt(
                    event['raw_event_id'], 'url_scraping', 'failed',
                    error_message="Insufficient content retrieved",
                    processing_time_ms=processing_time_ms
                )
                return False

        except Exception as e:
            processing_time_ms = int((time.time() - start_time) * 1000)
            self.db.log_processing_attempt(
                event['raw_event_id'], 'url_scraping', 'failed',
                error_message=str(e),
                processing_time_ms=processing_time_ms
            )
            raise
        finally:
            if scraper:
                scraper.close()

    def _update_raw_event_content(self, raw_event_id: str, content: str, url: str):
        """Update raw event with scraped content"""
        if not self.db._conn:
            raise ConnectionError("Database not connected")

        with self.db._lock:
            cursor = self.db._conn.cursor()
            try:
                cursor.execute("""
                    UPDATE RawEvents SET raw_content = ? WHERE raw_event_id = ?
                """, (content, raw_event_id))
                self.db._conn.commit()
            except Exception as e:
                logger.error(f"Failed to update raw event content: {e}")
                self.db._conn.rollback()

    # =========================================================================
    # ENRICHMENT PHASE
    # =========================================================================

    async def enrich_events(self, max_events: Optional[int] = None, australian_only: bool = True):
        """
        Enrich raw events using LLM analysis.
        Only processes events that appear to be Australian cyber events.

        Args:
            max_events: Maximum events to process
            australian_only: Only process events that appear Australian
        """
        logger.info("[LLM] Starting LLM enrichment phase" + (f" for max {max_events} events" if max_events else " without limit"))

        # Get events ready for processing
        events_to_process = self.db.get_raw_events_for_processing(australian_only, max_events)

        if not events_to_process:
            logger.info("[QUEUE] No events ready for enrichment")
            return

        logger.info(f"[LLM] Found {len(events_to_process)} events ready for LLM analysis")

        # Process events with progress bar
        enriched_count = 0
        with tqdm(total=len(events_to_process), desc="LLM Analysis", unit="event") as pbar:
            for event in events_to_process:
                try:
                    success = await self._enrich_single_event(event)
                    if success:
                        enriched_count += 1
                        pbar.set_postfix({"enriched": f"{enriched_count}/{len(events_to_process)}"})
                    else:
                        pbar.set_postfix({"skipped": "Not Australian/specific"})
                except Exception as e:
                    logger.error(f"[ERROR] Enrichment error for {event.get('raw_title', 'Unknown')}: {e}")
                    self.stats['errors'] += 1
                    pbar.set_postfix({"error": "Failed"})
                pbar.update(1)

        self.stats['events_enriched'] = enriched_count
        logger.info(f"[SUCCESS] Enrichment complete: {enriched_count} events successfully enriched")

    async def _enrich_single_event(self, raw_event: Dict[str, Any]) -> bool:
        """
        Enrich a single raw event using LLM analysis.

        Args:
            raw_event: Raw event dictionary

        Returns:
            True if successful, False otherwise
        """
        start_time = time.time()
        raw_event_id = raw_event['raw_event_id']

        try:
            # Prepare content for LLM analysis
            content = self._prepare_content_for_analysis(raw_event)
            if not content:
                self.db.log_processing_attempt(
                    raw_event_id, 'llm_analysis', 'skipped',
                    error_message="No content available for analysis"
                )
                return False

            # Analyzing event with LLM

            # Perform LLM analysis
            enriched_data = extract_event_details_with_llm(content)

            processing_time_ms = int((time.time() - start_time) * 1000)

            if not enriched_data:
                self.db.log_processing_attempt(
                    raw_event_id, 'llm_analysis', 'failed',
                    error_message="LLM analysis returned no results",
                    processing_time_ms=processing_time_ms
                )
                return False

            # Log successful analysis
            self.db.log_processing_attempt(
                raw_event_id, 'llm_analysis', 'success',
                result_data=enriched_data.model_dump(),
                processing_time_ms=processing_time_ms
            )

            # Only create enriched event if it's Australian and specific
            if enriched_data.is_australian_event and enriched_data.is_specific_event:
                enriched_event_data = self._prepare_enriched_event_data(raw_event, enriched_data)
                enriched_event_id = self.db.create_enriched_event(raw_event_id, enriched_event_data)

                # Created enriched event successfully

                # Mark raw event as processed
                self.db.mark_raw_event_processed(raw_event_id)
                return True
            else:
                # Event not suitable for enrichment but processing was successful
                reasons = []
                if not enriched_data.is_australian_event:
                    reasons.append("not Australian")
                if not enriched_data.is_specific_event:
                    reasons.append("not specific event")

                # Skipped enrichment - not suitable for database

                self.db.log_processing_attempt(
                    raw_event_id, 'llm_analysis', 'skipped',
                    result_data={'reasons': reasons},
                    processing_time_ms=processing_time_ms
                )

                # Mark as processed but don't create enriched event
                self.db.mark_raw_event_processed(raw_event_id)
                return False

        except Exception as e:
            processing_time_ms = int((time.time() - start_time) * 1000)
            self.db.log_processing_attempt(
                raw_event_id, 'llm_analysis', 'failed',
                error_message=str(e),
                processing_time_ms=processing_time_ms
            )
            raise

    def _prepare_content_for_analysis(self, raw_event: Dict[str, Any]) -> Optional[str]:
        """
        Prepare content for LLM analysis by combining available text.

        Args:
            raw_event: Raw event dictionary

        Returns:
            Combined content string or None if insufficient content
        """
        content_parts = []

        # Add title
        if raw_event.get('raw_title'):
            content_parts.append(f"Title: {raw_event['raw_title']}")

        # Add description
        if raw_event.get('raw_description'):
            content_parts.append(f"Description: {raw_event['raw_description']}")

        # Add scraped content (most important for GDELT events)
        if raw_event.get('raw_content'):
            content_parts.append(f"Article Content: {raw_event['raw_content']}")

        # Add metadata context
        if raw_event.get('source_metadata'):
            try:
                metadata = json.loads(raw_event['source_metadata'])
                if metadata:
                    content_parts.append(f"Source Information: {json.dumps(metadata, indent=2)}")
            except (json.JSONDecodeError, TypeError):
                pass

        combined_content = "\n\n".join(content_parts)

        # Must have at least 100 characters of content
        return combined_content if len(combined_content) >= 100 else None

    def _prepare_enriched_event_data(self, raw_event: Dict[str, Any], llm_data) -> Dict[str, Any]:
        """
        Prepare enriched event data from raw event and LLM analysis.

        Args:
            raw_event: Raw event dictionary
            llm_data: LLM analysis results

        Returns:
            Dictionary suitable for creating enriched event
        """
        # Parse source metadata if available
        source_metadata = {}
        if raw_event.get('source_metadata'):
            try:
                source_metadata = json.loads(raw_event['source_metadata'])
            except (json.JSONDecodeError, TypeError):
                pass

        # Determine title (prefer enriched, fall back to raw)
        title = raw_event.get('raw_title', 'Untitled Event')
        if llm_data.primary_entity and raw_event['source_type'] == 'GDELT':
            # For GDELT events, create better title using primary entity
            title = f"Cyber incident affecting {llm_data.primary_entity}"

        # Use LLM-extracted event date first, then fallback options
        event_date = llm_data.event_date if llm_data.event_date else raw_event.get('event_date')

        if not event_date:
            # Fallback 1: Use discovery date from raw event
            discovered_at = raw_event.get('discovered_at')
            if discovered_at:
                try:
                    if isinstance(discovered_at, str):
                        event_date = datetime.fromisoformat(discovered_at.replace('Z', '+00:00')).date()
                    else:
                        event_date = discovered_at.date() if hasattr(discovered_at, 'date') else discovered_at
                except:
                    # Fallback 2: Use middle of our search range as reasonable fallback
                    event_date = datetime(2025, 6, 4).date()  # Middle of June 1-7 range
            else:
                # Fallback 3: Use middle of our search range as reasonable fallback
                event_date = datetime(2025, 6, 4).date()  # Middle of June 1-7 range

            logger.debug(f"[ENRICHMENT] Using fallback event date: {event_date}")
        else:
            logger.debug(f"[ENRICHMENT] Using {'LLM-extracted' if llm_data.event_date else 'raw'} event date: {event_date}")

        enriched_data = {
            'title': title,
            'description': raw_event.get('raw_description', ''),
            'summary': llm_data.summary,
            'event_type': source_metadata.get('event_type'),
            'severity': source_metadata.get('severity', 'Medium'),
            'event_date': event_date,
            'is_australian_event': llm_data.is_australian_event,
            'is_specific_event': llm_data.is_specific_event,
            'confidence_score': source_metadata.get('confidence', 0.7),
            'australian_relevance_score': 1.0 if llm_data.is_australian_event else 0.0,
            'status': 'Active'
        }

        # Prepare entities
        entities = []
        if llm_data.primary_entity:
            entities.append({
                'name': llm_data.primary_entity,
                'type': 'Organization',
                'relationship_type': 'primary_target',
                'is_australian': llm_data.is_australian_event,
                'confidence_score': 0.8
            })

        for entity_name in llm_data.affected_entities:
            entities.append({
                'name': entity_name,
                'type': 'Organization',
                'relationship_type': 'affected',
                'is_australian': llm_data.is_australian_event,
                'confidence_score': 0.6
            })

        if entities:
            enriched_data['entities'] = entities

        return enriched_data

    # =========================================================================
    # REPORTING AND UTILITIES
    # =========================================================================

    def print_statistics(self):
        """Print pipeline execution statistics"""
        print("\n" + "="*60)
        print("[RESULTS] PIPELINE EXECUTION SUMMARY")
        print("="*60)

        print(f"[STATS] Events discovered: {self.stats['events_discovered']}")
        print(f"[SCRAPING] URLs scraped: {self.stats['events_scraped']}")
        print(f"[LLM] Events analyzed: {self.stats['events_analyzed']}")
        print(f"[ENRICHED] Events enriched: {self.stats['events_enriched']}")
        print(f"[ERROR] Errors encountered: {self.stats['errors']}")

        # Database statistics
        print(f"\n[DATABASE] DATABASE STATUS:")
        try:
            db_stats = self.db.get_summary_statistics()
            print(f"[COLLECTION] Raw events total: {db_stats.get('raw_events_total', 0)}")
            print(f"[STORED] Raw events processed: {db_stats.get('raw_events_processed', 0)}")
            print(f"[ENRICHED] Enriched events total: {db_stats.get('enriched_events_total', 0)}")
            print(f"[AUSTRALIAN] Australian events: {db_stats.get('enriched_events_australian', 0)}")
            print(f"[RESULTS] Specific events: {db_stats.get('enriched_events_specific', 0)}")

            # Processing queue status
            queue_stats = self.db.get_processing_queue_status()
            print(f"\n[PROCESSING] PROCESSING QUEUE:")
            print(f"[QUEUE] Events needing scraping: {queue_stats.get('needs_scraping', 0)}")
            print(f"[LLM] Events ready for analysis: {queue_stats.get('needs_analysis', 0)}")

        except Exception as e:
            print(f"[WARNING] Could not retrieve database statistics: {e}")

        print("="*60)

    def close(self):
        """Clean up resources"""
        self.db.close()


# =========================================================================
# COMMAND LINE INTERFACE
# =========================================================================

async def main():
    parser = argparse.ArgumentParser(
        description="Discover and enrich Australian cyber security events",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full pipeline (discover, scrape, enrich)
  python discover_enrich_events.py

  # Only discovery phase with limited events per source
  python discover_enrich_events.py --discover --max-events 250

  # Only enrichment phase, unlimited items
  python discover_enrich_events.py --enrich

  # Specific source only
  python discover_enrich_events.py --source GDELT --max-events 500

  # Extended date range (if custom date handling enabled)
  python discover_enrich_events.py --days 14 --max-events 400
        """
    )

    parser.add_argument('--discover', action='store_true',
                        help='Run discovery phase to find new events')
    parser.add_argument('--scrape', action='store_true',
                        help='Run scraping phase to get full content')
    parser.add_argument('--enrich', action='store_true',
                        help='Run enrichment phase using LLM analysis')
    parser.add_argument('--source', choices=['GDELT', 'Perplexity', 'GoogleSearch', 'WebberInsurance', 'OAIC'],
                        help='Run only specific source (can be used multiple times)', action='append')
    parser.add_argument('--max-events', type=int, default=0,
                        help='Maximum events to process per phase (0 or negative for unlimited; default: unlimited for scraping/enrichment, 1000 for discovery)')
    parser.add_argument('--days', type=int, default=7,
                        help='Number of days to look back for discovery (default: 7)')
    parser.add_argument('--db-path', default='instance/cyber_events.db',
                        help='Path to database file')
    parser.add_argument('--no-australian-filter', action='store_true',
                        help='Disable Australian-only filtering for enrichment')

    args = parser.parse_args()

    # If no phase specified, run all phases
    if not any([args.discover, args.scrape, args.enrich]):
        args.discover = args.scrape = args.enrich = True

    # Load environment
    if Path('.env').exists():
        load_dotenv('.env')
        logger.info("[SUCCESS] Loaded environment variables from .env")

    # Check database schema
    try:
        # Quick check if V2 schema exists
        import sqlite3
        conn = sqlite3.connect(args.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='RawEvents'")
        if not cursor.fetchone():
            print("[ERROR] Database V2 schema not found!")
            print("[QUEUE] Please run the migration script first:")
            print("   python database_migration_v2.py")
            return 1
        conn.close()
    except Exception as e:
        logger.error(f"Database check failed: {e}")
        return 1

    # Initialize pipeline
    pipeline = EventDiscoveryEnrichmentPipeline(args.db_path)

    try:
        logger.info("[PIPELINE] Starting Australian Cyber Events Discovery & Enrichment Pipeline")

        # Normalize max events: <=0 means unlimited for scrape/enrich, but keep sensible default for discovery
        discovery_max = args.max_events if args.max_events and args.max_events > 0 else 1000
        processing_limit = args.max_events if args.max_events and args.max_events > 0 else None

        # Discovery Phase
        if args.discover:
            await pipeline.discover_events(
                source_types=args.source,
                date_range_days=args.days,
                max_events=discovery_max
            )

        # Scraping Phase
        if args.scrape:
            await pipeline.scrape_event_urls(
                max_events=processing_limit,
                source_types=args.source
            )

        # Enrichment Phase
        if args.enrich:
            await pipeline.enrich_events(
                max_events=processing_limit,
                australian_only=not args.no_australian_filter
            )

        # Print results
        pipeline.print_statistics()

        logger.info("[COMPLETED] Pipeline execution completed successfully!")
        return 0

    except Exception as e:
        logger.error(f"[ERROR] Pipeline execution failed: {e}")
        return 1

    finally:
        pipeline.close()


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n[WARNING] Pipeline interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"[ERROR] Unexpected error: {e}")
        sys.exit(1)