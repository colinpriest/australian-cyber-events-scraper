#!/usr/bin/env python3
"""
===========================================================================================
⚠️ DEPRECATED: Use run_full_pipeline.py instead
===========================================================================================

This script is DEPRECATED and maintained only for backward compatibility.

**DO NOT USE THIS SCRIPT DIRECTLY**

Instead, use:
    python run_full_pipeline.py

The unified pipeline provides:
✓ Consistent Perplexity AI enrichment (this script uses outdated GPT-4o-mini)
✓ Advanced entity-based deduplication
✓ Automatic dashboard generation
✓ Single source of truth for all operations

This legacy script uses the old GPT-4o-mini enrichment which has a 70% rejection rate
compared to the modern Perplexity AI enrichment.

===========================================================================================
LEGACY DOCUMENTATION (for reference only):
===========================================================================================

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
from datetime import datetime, timedelta, date
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
from entity_scraper import PlaywrightScraper 
from llm_extractor import extract_event_details_with_llm

# Data collection imports
from cyber_data_collector import CyberDataCollector, CollectionConfig, DateRange
from cyber_data_collector.models.config import DataSourceConfig
from cyber_data_collector.utils import ConfigManager
from rf_event_filter import RfEventFilter


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


async def check_gdelt_authentication() -> bool:
    """
    Check if GDELT BigQuery authentication is working by actually testing a query.
    Returns True if authentication is valid, False otherwise.
    """
    try:
        # Import GDELT data source to test authentication
        from cyber_data_collector.datasources.gdelt import GDELTDataSource
        from cyber_data_collector.models.config import DataSourceConfig
        from cyber_data_collector.utils import ConfigManager, RateLimiter
        from cyber_data_collector.models.config import DateRange

        # Load environment config
        env_config = ConfigManager(".env").load()

        # Create a test GDELT config
        test_config = DataSourceConfig(
            enabled=True,
            rate_limit=10,
            max_results=1,
            custom_config={"max_records": 1}
        )

        # Create rate limiter
        rate_limiter = RateLimiter()
        rate_limiter.set_limit("gdelt", per_minute=10)

        # Create GDELT data source
        gdelt_source = GDELTDataSource(test_config, rate_limiter, env_config)

        # First check if client can be initialized
        if not gdelt_source.validate_config():
            return False

        # Now actually test a query to verify authentication works
        # Use a broader date range to ensure we find events (last 30 days)
        test_start = datetime.now() - timedelta(days=30)
        test_end = datetime.now()
        test_range = DateRange(start_date=test_start, end_date=test_end)

        # Try to collect events - this will test the actual BigQuery query execution
        test_events = await gdelt_source.collect_events(test_range)

        # If we get here without exception, authentication is working
        logger.info(f"GDELT authentication test passed (found {len(test_events)} events)")
        return True

    except Exception as e:
        error_msg = str(e)
        if "Reauthentication is needed" in error_msg or "invalid_grant" in error_msg or "expired" in error_msg.lower():
            logger.warning(f"GDELT authentication failed: {error_msg}")
            return False
        else:
            # Log other errors but don't consider them auth failures
            logger.warning(f"GDELT authentication check failed with unexpected error: {e}")
            # For unexpected errors, assume auth is OK and let the main process handle it
            return True


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

        # Initialize Random Forest filtering system
        self.filter_system = RfEventFilter()
        # Reset statistics for this run
        self.filter_system.reset_statistics()

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
        Discover new events from configured data sources, processing month by month.

        Args:
            source_types: List of sources to use ('GDELT', 'Perplexity', 'GoogleSearch', 'WebberInsurance')
            date_range_days: How many days back to search (ignored - now processes all months)
            max_events: Maximum events per source per month
        """
        # Default to all sources if none specified
        if not source_types:
            source_types = ['Perplexity', 'GoogleSearch', 'WebberInsurance', 'OAIC']

        # Get current year and month for dynamic date range
        from datetime import datetime
        now = datetime.now()
        current_year = now.year
        current_month = now.month

        # Get list of unprocessed months from January 2020 to current month
        unprocessed_months = self.db.get_unprocessed_months(
            start_year=2020, start_month=1,
            end_year=current_year, end_month=current_month
        )

        # Always reprocess the last 3 calendar months (including current) to catch late-reported events
        recent_months = []
        temp_year, temp_month = current_year, current_month
        for _ in range(3):
            recent_months.append((temp_year, temp_month))
            temp_month -= 1
            if temp_month == 0:
                temp_month = 12
                temp_year -= 1
        recent_months_set = set(recent_months)

        months_to_process = []
        for m in unprocessed_months:
            if m not in months_to_process:
                months_to_process.append(m)
        for m in recent_months:
            if m not in months_to_process:
                months_to_process.append(m)

        if not months_to_process:
            logger.info(f"[DISCOVERY] All months from Jan 2020 to {now.strftime('%b %Y')} have been processed!")
            return

        logger.info(f"[DISCOVERY] Found {len(unprocessed_months)} unprocessed months to process")
        logger.info("[DISCOVERY] Adding rolling 3-month lookback (including current month) for late-reported events")

        total_events_discovered = 0
        for year, month in months_to_process:
            logger.info(f"[DISCOVERY] Processing {year}-{month:02d}")

            # Skip if already processed and not part of the rolling 3-month lookback
            if self.db.is_month_processed(year, month) and (year, month) not in recent_months_set:
                logger.info(f"[DISCOVERY] Month {year}-{month:02d} was already processed, skipping")
                continue
            elif self.db.is_month_processed(year, month):
                logger.info(f"[DISCOVERY] Reprocessing {year}-{month:02d} (3-month late-reporting window)")

            month_events = await self._discover_events_for_month(
                year, month, source_types, max_events
            )
            total_events_discovered += month_events

        self.stats['events_discovered'] = total_events_discovered
        logger.info(f"[DISCOVERY] Completed processing all months. Total events discovered: {total_events_discovered}")

        # Run global deduplication after all data collection is complete
        # Also run if DeduplicatedEvents is empty (e.g., was cleared but needs repopulation)
        dedup_count = self._get_deduplicated_event_count()
        if total_events_discovered > 0 or dedup_count == 0:
            if dedup_count == 0:
                logger.info("[GLOBAL DEDUPLICATION] DeduplicatedEvents table is empty, running deduplication to repopulate...")
            else:
                logger.info("[GLOBAL DEDUPLICATION] Starting global deduplication process...")
            await self.run_global_deduplication()

    async def _discover_events_for_month(self, year: int, month: int, source_types: List[str], max_events: int) -> int:
        """
        Discover events for a specific month.
        Returns the number of events discovered.
        """
        # Calculate month boundaries
        from calendar import monthrange
        last_day = monthrange(year, month)[1]
        start_date = datetime(year, month, 1)
        end_date = datetime(year, month, last_day)

        date_range = DateRange(start_date=start_date, end_date=end_date)
        logger.info(f"[DISCOVERY] Processing month {year}-{month:02d}: {start_date.date()} to {end_date.date()}")

        # Configure data sources
        collection_config = self._build_collection_config(source_types, max_events, date_range)

        events_for_month = 0
        try:
            # Initialize data sources metadata if needed
            await self._initialize_data_sources_metadata(source_types)

            # Create collector and capture ALL stages of processing
            collector = CyberDataCollector(collection_config, ".env")

            # We need to hack into the collector to get all stages
            # Let's manually run the collection pipeline to capture each stage

            # Step 1: Raw collection (same as collector.collect_all_events but broken down)
            valid_sources = {}
            for name, source in collector.data_sources.items():
                if source.validate_config():
                    valid_sources[name] = source

            # Collect raw events from all sources
            import asyncio
            collection_tasks = [collector._collect_from_source(name, source) for name, source in valid_sources.items()]
            all_raw_events = []
            for task in asyncio.as_completed(collection_tasks):
                try:
                    source_events = await task
                    all_raw_events.extend(source_events)
                except Exception as exc:
                    logger.error(f"Error collecting from source: {exc}")

            logger.info(f"[RAW] Collected {len(all_raw_events)} raw events for {year}-{month:02d}")

            # Step 2: Store raw events
            raw_events_stored = 0
            raw_event_ids = []
            for event in all_raw_events:
                raw_event_id = await self._store_raw_event(event, start_date)
                if raw_event_id:
                    raw_events_stored += 1
                    raw_event_ids.append(raw_event_id)
                    # Log successful raw event storage
                    await self._log_processing_success(raw_event_id, 'raw_event_storage', {
                        'source_type': self._determine_source_type(event.data_sources),
                        'title': event.title[:100]
                    })

            # Step 3: Scrape URL content for raw events
            logger.info(f"[PIPELINE] Scraping URLs for {year}-{month:02d}")
            scraped_count = await self._scrape_raw_events_for_month(raw_event_ids)
            logger.info(f"[SCRAPED] Successfully scraped {scraped_count} URLs for {year}-{month:02d}")

            # Step 4: Process events (entity extraction, LLM classification)
            logger.info(f"[PIPELINE] Processing events for {year}-{month:02d}")
            processed_events = await collector._process_events(all_raw_events)
            logger.info(f"[PROCESSED] Enhanced {len(processed_events)} events for {year}-{month:02d}")

            # Step 5: Store enriched events
            logger.info(f"[PIPELINE] Starting to store {len(processed_events)} enriched events for {year}-{month:02d}")
            enriched_events_stored = 0
            enriched_event_ids = []
            failed_stores = 0
            
            for i, event in enumerate(processed_events):
                if i % 10 == 0:  # Log every 10th event
                    logger.info(f"[PIPELINE] Storing enriched event {i+1}/{len(processed_events)} for {year}-{month:02d}")
                raw_event_id = raw_event_ids[i] if i < len(raw_event_ids) else None
                enriched_event_id = await self._store_enriched_event(event, raw_event_id, year, month)
                if enriched_event_id:
                    enriched_events_stored += 1
                    enriched_event_ids.append(enriched_event_id)
                    # Log successful enrichment
                    if raw_event_id:
                        await self._log_processing_success(raw_event_id, 'enrichment', {
                            'enriched_event_id': enriched_event_id,
                            'confidence_score': event.confidence.overall if hasattr(event, 'confidence') and event.confidence else 0.7
                        })
                else:
                    failed_stores += 1
                    
            logger.info(f"[PIPELINE] Completed storing {enriched_events_stored} enriched events for {year}-{month:02d}")
            
            # Check if too many events failed to store
            if failed_stores > 0:
                failure_rate = failed_stores / len(processed_events)
                logger.warning(f"[WARNING] {failed_stores}/{len(processed_events)} events failed to store ({failure_rate:.1%} failure rate)")
                if failure_rate > 0.5:  # More than 50% failure rate
                    logger.error(f"[CRITICAL] High failure rate ({failure_rate:.1%}) - stopping pipeline")
                    raise RuntimeError(f"Too many events failed to store: {failed_stores}/{len(processed_events)} ({failure_rate:.1%})")

            # Mark all raw events as processed (regardless of whether they became enriched events)
            logger.info(f"[PIPELINE] Marking {len(raw_event_ids)} raw events as processed for {year}-{month:02d}")
            for i, raw_event_id in enumerate(raw_event_ids):
                if i % 20 == 0:  # Log every 20th event
                    logger.info(f"[PIPELINE] Marking raw event {i+1}/{len(raw_event_ids)} as processed for {year}-{month:02d}")
                self.db.mark_raw_event_processed(raw_event_id)
            logger.info(f"[PIPELINE] Completed marking all raw events as processed for {year}-{month:02d}")

            # Step 6: Deduplicate events (within this month only)
            if collector.config.enable_deduplication:
                logger.info(f"[PIPELINE] Loading enriched events from database for deduplication for {year}-{month:02d}")
                # Load enriched events from database for this month to ensure we have the proper dates
                enriched_events_from_db = []
                for enriched_event_id in enriched_event_ids:
                    try:
                        with self.db._lock:
                            cursor = self.db._conn.cursor()
                            cursor.execute("""
                                SELECT
                                    enriched_event_id, title, description, summary, event_type, severity,
                                    event_date, records_affected, is_australian_event, is_specific_event,
                                    confidence_score, australian_relevance_score
                                FROM EnrichedEvents
                                WHERE enriched_event_id = ? AND status = 'Active'
                            """, (enriched_event_id,))
                            row = cursor.fetchone()
                            if row:
                                enriched_events_from_db.append(dict(row))
                    except Exception as e:
                        logger.warning(f"[WARNING] Failed to load enriched event {enriched_event_id}: {e}")

                logger.info(f"[PIPELINE] Loaded {len(enriched_events_from_db)} enriched events for deduplication for {year}-{month:02d}")
                # Convert database records back to CyberEvent objects for deduplication

                enriched_cyber_events = []
                for db_event in enriched_events_from_db:
                    # Find corresponding processed event to get full object structure
                    matching_event = None
                    for proc_event in processed_events:
                        if proc_event.title == db_event['title']:
                            matching_event = proc_event
                            break

                    if matching_event:
                        # Create a copy and update with database values
                        event_copy = matching_event.model_copy(deep=True)
                        if db_event['event_date']:
                            event_copy.event_date = datetime.fromisoformat(db_event['event_date'].replace('Z', '+00:00')).date()
                        enriched_cyber_events.append(event_copy)

                # Step 7: Store enriched events (no deduplication yet)
                logger.info(f"[PIPELINE] Storing {len(enriched_cyber_events)} enriched events for {year}-{month:02d}")
                # Store enriched events directly without deduplication
                enriched_events_stored = len(enriched_cyber_events)
                deduplicated_events = enriched_cyber_events
                deduplicated_events_stored = 0  # No deduplication at month level

            # Final filtering by confidence
            high_confidence_events = [
                event for event in deduplicated_events
                if event.confidence.overall >= collector.config.confidence_threshold
            ]

            events_for_month = len(all_raw_events)  # Count original raw events for the month tracking

            logger.info(f"[STORAGE] Month {year}-{month:02d}: {raw_events_stored} raw, {enriched_events_stored} enriched, {deduplicated_events_stored} deduplicated events stored")

            # Mark month as processed
            self.db.mark_month_as_processed(
                year=year,
                month=month,
                total_raw_events=events_for_month,
                processing_notes=f"Processed with sources: {', '.join(source_types)}"
            )

            logger.info(f"[SUCCESS] Month {year}-{month:02d} complete: {events_for_month} events stored")

            # Note: Cross-month deduplication removed - will be handled globally

        except Exception as e:
            logger.error(f"[ERROR] Discovery failed for {year}-{month:02d}: {e}")
            self.stats['errors'] += 1
            # Don't mark as processed if there was an error
            raise

        return events_for_month

    def _build_collection_config(self, source_types: List[str], max_events: int, date_range: DateRange) -> CollectionConfig:
        """Build collection configuration for specified sources"""

        # Configure individual sources based on what's requested
        # GDELT disabled - BigQuery authentication issues
        gdelt_config = DataSourceConfig(
            enabled=False,  # GDELT disabled
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

    async def _store_raw_event(self, event, month_start: Optional[datetime] = None):
        """Store a discovered event as a raw event, avoiding duplicates"""
        try:
            # Determine source type from data sources
            source_type = self._determine_source_type(event.data_sources)
            source_url = event.data_sources[0].url if event.data_sources else None
            title = event.title

            # Extract publication date from data sources
            publication_date = self._extract_publication_date(event.data_sources)

            # Determine initial event date
            event_date = event.event_date.date() if event.event_date else None

            if not event_date:
                if publication_date:
                    event_date = publication_date.date()
                else:
                    event_date = self._fallback_event_date(event, month_start)
            
            # Convert date to ISO format string for SQLite (fixes Python 3.12 deprecation warning)
            event_date_str = event_date.isoformat() if event_date else None

            # Check if this event already exists
            if source_url and title:
                existing_id = self.db.find_existing_raw_event(source_type, source_url, title)
                if existing_id:
                    logger.debug(f"[DUPLICATE] Skipping duplicate event {existing_id} from {source_type}: {title[:50]}...")
                    return existing_id

            # Prepare raw event data
            raw_data = {
                'source_event_id': (event.external_ids.get('gdelt_id') if hasattr(event, 'external_ids') else None),
                'title': title,
                'description': event.description,
                'content': None,  # Will be filled during scraping phase
                'event_date': event_date_str,
                'source_url': source_url,
                'metadata': {
                    'event_type': str(event.event_type) if hasattr(event, 'event_type') else None,
                    'severity': str(event.severity) if hasattr(event, 'severity') else None,
                    'confidence': event.confidence.overall if hasattr(event, 'confidence') and event.confidence else None,
                    'australian_relevance': event.australian_relevance if hasattr(event, 'australian_relevance') else None,
                    'data_sources_count': len(event.data_sources) if event.data_sources else 0,
                    'publication_date': publication_date.isoformat() if publication_date else None
                }
            }

            # Store in database
            raw_event_id = self.db.add_raw_event(source_type, raw_data)
            logger.debug(f"[STORED] Stored raw event {raw_event_id} from {source_type}: {event.title[:50]}...")
            return raw_event_id

        except Exception as e:
            logger.error(f"[ERROR] Failed to store raw event '{event.title[:50]}...': {e}")
            # For critical database errors, we should stop the pipeline
            if "NOT NULL constraint" in str(e) or "FOREIGN KEY constraint" in str(e) or "UNIQUE constraint" in str(e):
                logger.error(f"[CRITICAL] Database constraint violation - stopping pipeline")
                raise RuntimeError(f"Critical database error storing raw event: {e}") from e
            return None

    async def _store_enriched_event(self, event, raw_event_id: str, processing_year: int = None, processing_month: int = None) -> Optional[str]:
        """Store a processed event as an enriched event with improved date fallback logic"""
        try:
            logger.debug(f"[ENRICHED] Starting to store event: {getattr(event, 'title', 'NO_TITLE')[:50]}...")

            if not raw_event_id:
                logger.warning(f"[WARNING] No raw_event_id provided for event: {event.title[:50]}...")
                return None

            logger.debug(f"[ENRICHED] Processing event data...")
            # Handle event_date - required field with improved fallback logic
            event_date = None

            # Try event date first
            if event.event_date:
                event_date = event.event_date.date() if hasattr(event.event_date, 'date') else event.event_date
                logger.debug(f"[ENRICHED] Using event date: {event_date}")

            # Try published date as fallback
            elif hasattr(event, 'data_sources') and event.data_sources:
                for source in event.data_sources:
                    if hasattr(source, 'publication_date') and source.publication_date:
                        event_date = source.publication_date.date() if hasattr(source.publication_date, 'date') else source.publication_date
                        logger.debug(f"[ENRICHED] Using publication date from source: {event_date}")
                        break

            # Use 1st day of processing month as final fallback
            if not event_date:
                if processing_year and processing_month:
                    event_date = date(processing_year, processing_month, 1)
                    logger.debug(f"[ENRICHED] Using 1st day of processing month: {event_date}")
                else:
                    # Absolute fallback to current date (should rarely happen)
                    event_date = date.today()
                    logger.debug(f"[WARNING] No event date, publication date, or processing month for '{event.title[:50]}...', using current date")

            # Convert date to ISO format string for SQLite (fixes Python 3.12 deprecation warning)
            event_date_str = event_date.isoformat() if hasattr(event_date, 'isoformat') else str(event_date)

            # Ensure title is not empty (NOT NULL constraint)
            title = event.title if event.title and event.title.strip() else "Untitled Event"

            # Ensure boolean fields are proper booleans (NOT NULL constraints)
            is_australian = bool(getattr(event, 'australian_relevance', False))
            is_specific = True  # Since these came from CyberDataCollector processing

            logger.debug(f"[ENRICHED] Prepared data - title: {title[:30]}..., date: {event_date_str}, australian: {is_australian}")

            # Prepare entities for storage
            entities = []
            if hasattr(event, 'affected_entities') and event.affected_entities:
                for entity in event.affected_entities:
                    entities.append({
                        'name': getattr(entity, 'name', 'Unknown Entity'),
                        'type': str(getattr(entity, 'entity_type', 'OTHER')),
                        'is_australian': getattr(entity, 'australian_entity', False),
                        'confidence_score': getattr(entity, 'confidence_score', 0.8),
                        'relationship_type': 'affected'  # Default relationship
                    })

            # Prepare enriched event data with all NOT NULL fields guaranteed
            enriched_data = {
                'title': title,  # NOT NULL - guaranteed not empty
                'description': event.description,
                'summary': getattr(event, 'summary', None),
                'event_type': str(event.event_type) if hasattr(event, 'event_type') else None,
                'severity': str(event.severity) if hasattr(event, 'severity') else None,
                'event_date': event_date_str,  # NOT NULL - ISO format string for SQLite
                'records_affected': event.financial_impact.customers_affected if event.financial_impact else None,
                'is_australian_event': is_australian,  # NOT NULL - guaranteed boolean
                'is_specific_event': is_specific,  # NOT NULL - guaranteed boolean
                'confidence_score': event.confidence.overall if hasattr(event, 'confidence') and event.confidence else 0.7,
                'australian_relevance_score': getattr(event, 'australian_relevance', 0.0) if hasattr(getattr(event, 'australian_relevance', None), '__float__') else (1.0 if getattr(event, 'australian_relevance', False) else 0.0),
                'status': 'Active',
                'entities': entities  # Pass entities to be created in one transaction
            }

            logger.debug(f"[ENRICHED] Calling create_enriched_event with {len(entities)} entities...")
            # Store in database using the correct method
            enriched_event_id = self.db.create_enriched_event(raw_event_id, enriched_data)
            logger.debug(f"[ENRICHED] Database insert successful, event ID: {enriched_event_id}")

            logger.debug(f"[ENRICHED] Successfully stored enriched event {enriched_event_id}: {event.title[:50]}...")
            return enriched_event_id

        except Exception as e:
            logger.error(f"[ERROR] Failed to store enriched event '{event.title[:50]}...': {e}")
            logger.error(f"[ERROR] Event data: title='{getattr(event, 'title', 'NO_TITLE')}', event_date='{getattr(event, 'event_date', 'NO_DATE')}', description='{getattr(event, 'description', 'NO_DESC')[:100]}...'")
            # For critical database errors, we should stop the pipeline
            if "NOT NULL constraint" in str(e) or "FOREIGN KEY constraint" in str(e) or "UNIQUE constraint" in str(e):
                logger.error(f"[CRITICAL] Database constraint violation - stopping pipeline")
                raise RuntimeError(f"Critical database error storing enriched event: {e}") from e
            return None

    async def _store_deduplicated_events(self, deduplicated_events, enriched_event_ids, raw_event_ids) -> tuple[int, list]:
        """Store deduplicated events and create mapping tables"""
        stored_count = 0
        deduplicated_event_ids = []

        try:
            import uuid
            from datetime import datetime
            for i, event in enumerate(deduplicated_events):
                # Create deduplicated event record
                deduplicated_event_id = str(uuid.uuid4())

                # Find the master enriched event and corresponding raw event
                master_enriched_event_id = enriched_event_ids[i] if i < len(enriched_event_ids) else None
                master_raw_event_id = raw_event_ids[i] if i < len(raw_event_ids) else None

                if not master_enriched_event_id or not master_raw_event_id:
                    logger.warning(f"[WARNING] Missing enriched_event_id ({master_enriched_event_id}) or raw_event_id ({master_raw_event_id}) for deduplicated event {i}")
                    continue

                # Insert into DeduplicatedEvents table
                with self.db._lock:
                    cursor = self.db._conn.cursor()

                    # Debug logging for event_date
                    event_date_value = None
                    if event.event_date:
                        if hasattr(event.event_date, 'date'):
                            event_date_value = event.event_date.date().isoformat()
                        else:
                            event_date_value = str(event.event_date)
                        logger.debug(f"[DEDUPLICATED] Event '{event.title[:30]}...' has event_date: {event_date_value}")
                    else:
                        logger.warning(f"[WARNING] Event '{event.title[:30]}...' missing event_date - will be NULL in database")

                    cursor.execute("""
                        INSERT INTO DeduplicatedEvents (
                            deduplicated_event_id, master_enriched_event_id, title, description,
                            summary, event_type, severity, event_date, records_affected,
                            is_australian_event, is_specific_event, confidence_score,
                            australian_relevance_score, total_data_sources,
                            contributing_raw_events, contributing_enriched_events,
                            similarity_score, deduplication_method, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        deduplicated_event_id,
                        master_enriched_event_id,
                        event.title,
                        event.description,
                        getattr(event, 'summary', None),
                        str(event.event_type) if hasattr(event, 'event_type') else None,
                        str(event.severity) if hasattr(event, 'severity') else None,
                        event_date_value,
                        event.financial_impact.customers_affected if event.financial_impact else None,
                        getattr(event, 'australian_relevance', False),
                        True,
                        event.confidence.overall if hasattr(event, 'confidence') and event.confidence else 0.7,
                        getattr(event, 'australian_relevance', 0.0) if isinstance(getattr(event, 'australian_relevance', None), (int, float)) else (1.0 if getattr(event, 'australian_relevance', False) else 0.0),
                        len(event.data_sources) if hasattr(event, 'data_sources') and event.data_sources else 1,
                        event.contributing_raw_events,
                        event.contributing_enriched_events,
                        getattr(event, 'similarity_score', 1.0),
                        'LLM_Enhanced',
                        'Active',
                        datetime.now().isoformat(),
                        datetime.now().isoformat()
                    ))

                    # Create mapping in EventDeduplicationMap
                    map_id = str(uuid.uuid4())
                    cursor.execute("""
                        INSERT INTO EventDeduplicationMap (
                            map_id, raw_event_id, enriched_event_id, deduplicated_event_id,
                            contribution_type, similarity_score, data_source_weight, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        map_id,
                        master_raw_event_id,
                        master_enriched_event_id,
                        deduplicated_event_id,
                        'primary',
                        1.0,
                        1.0,
                        datetime.now().isoformat()
                    ))

                    self.db._conn.commit()
                    stored_count += 1
                    deduplicated_event_ids.append(deduplicated_event_id)

                    logger.debug(f"[DEDUPLICATED] Stored deduplicated event {deduplicated_event_id}: {event.title[:50]}...")

        except Exception as e:
            logger.warning(f"[WARNING] Failed to store deduplicated events: {e}")

        return stored_count, deduplicated_event_ids


    def _store_entity(self, entity) -> Optional[int]:
        """Store entity in EntitiesV2 table"""
        try:
            entity_name = getattr(entity, 'name', None)
            if not entity_name or not entity_name.strip():
                logger.warning(f"[ENTITY] Skipping entity with empty name")
                return None
                
            logger.debug(f"[ENTITY] Starting to store entity: {entity_name}")
            
            # Don't acquire lock here - caller should already have it
            logger.debug(f"[ENTITY] Using existing database connection for entity: {entity_name}")
            cursor = self.db._conn.cursor()

            # Check if entity already exists
            logger.debug(f"[ENTITY] Checking if entity exists: {entity_name}")
            cursor.execute("SELECT entity_id FROM EntitiesV2 WHERE entity_name = ?", (entity_name,))
            existing = cursor.fetchone()
            if existing:
                logger.debug(f"[ENTITY] Entity already exists with ID: {existing[0]}")
                return existing[0]

            # Insert new entity
            logger.debug(f"[ENTITY] Inserting new entity: {entity_name}")
            cursor.execute("""
                INSERT INTO EntitiesV2 (
                    entity_name, entity_type, is_australian, confidence_score,
                    industry, headquarters_location, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entity_name,
                str(entity.entity_type) if hasattr(entity, 'entity_type') else 'OTHER',
                getattr(entity, 'australian_entity', False),
                getattr(entity, 'confidence_score', 0.8),
                getattr(entity, 'industry', None),
                getattr(entity, 'location', None),
                datetime.now().isoformat(),
                datetime.now().isoformat()
            ))

            entity_id = cursor.lastrowid
            logger.debug(f"[ENTITY] Entity inserted with ID: {entity_id}")
            # Don't commit here - let the caller handle the transaction
            logger.debug(f"[ENTITY] Entity insert successful: {entity_id}")
            return entity_id

        except Exception as e:
            logger.warning(f"[WARNING] Failed to store entity '{entity.name}': {e}")
            return None

    async def _store_data_sources_for_deduplicated_event(self, event, deduplicated_event_id: str):
        """Store data sources for deduplicated event"""
        try:
            if not hasattr(event, 'data_sources') or not event.data_sources:
                return

            with self.db._lock:
                cursor = self.db._conn.cursor()

                for source in event.data_sources:
                    cursor.execute("""
                        INSERT INTO DeduplicatedEventSources (
                            deduplicated_event_id, source_url, source_type,
                            credibility_score, content_snippet, discovered_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        deduplicated_event_id,
                        getattr(source, 'url', None),
                        getattr(source, 'source_type', 'Unknown'),
                        getattr(source, 'credibility_score', 0.8),
                        getattr(source, 'content_snippet', None),
                        source.publication_date.isoformat() if source.publication_date else None
                    ))

                self.db._conn.commit()

        except Exception as e:
            logger.warning(f"[WARNING] Failed to store data sources: {e}")

    async def _store_deduplication_cluster(self, deduplicated_events, deduplicated_event_ids):
        """Store deduplication cluster information"""
        try:
            if len(deduplicated_events) <= 1:
                return

            import uuid
            from datetime import datetime

            with self.db._lock:
                cursor = self.db._conn.cursor()

                for i, event in enumerate(deduplicated_events):
                    if i < len(deduplicated_event_ids):
                        cluster_id = str(uuid.uuid4())
                        cursor.execute("""
                            INSERT INTO DeduplicationClusters (
                                cluster_id, deduplicated_event_id, cluster_size,
                                average_similarity, deduplication_timestamp, algorithm_version
                            ) VALUES (?, ?, ?, ?, ?, ?)
                        """, (
                            cluster_id,
                            deduplicated_event_ids[i],
                            len(deduplicated_events),
                            getattr(event, 'similarity_score', 0.8),
                            datetime.now().isoformat(),
                            'LLM_Enhanced_v1'
                        ))

                self.db._conn.commit()

        except Exception as e:
            logger.warning(f"[WARNING] Failed to store deduplication clusters: {e}")

    async def _perform_cross_month_deduplication_final(self, deduplication_engine):
        """Perform cross-month deduplication as a final step to merge events across months."""
        from cyber_data_collector.processing.deduplication_v2 import CyberEvent

        logger.info(f"[CROSS-MONTH] Loading deduplicated events from last 3 months for cross-month deduplication...")

        # Load deduplicated events from last 3 months (not all enriched events)
        all_deduplicated_events = []
        try:
            with self.db._lock:
                cursor = self.db._conn.cursor()
                cursor.execute("""
                    SELECT
                        deduplicated_event_id, title, description, summary, event_type, severity,
                        event_date, records_affected, is_australian_event, is_specific_event,
                        confidence_score, australian_relevance_score, created_at
                    FROM DeduplicatedEvents
                    WHERE status = 'Active'
                    AND event_date >= date('now', '-3 months')
                    ORDER BY event_date ASC
                """)
                rows = cursor.fetchall()
                all_deduplicated_events = [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"[ERROR] Failed to load deduplicated events for cross-month deduplication: {e}")
            return 0

        logger.info(f"[CROSS-MONTH] Loaded {len(all_deduplicated_events)} deduplicated events for analysis")

        # Check if we actually have events from multiple months
        if len(all_deduplicated_events) == 0:
            logger.info(f"[CROSS-MONTH] No deduplicated events found - skipping cross-month deduplication")
            return 0

        # Check date range to see if we have events from multiple months (using event_date, not created_at)
        from datetime import datetime
        dates = []
        for event in all_deduplicated_events:
            if event['event_date']:
                try:
                    # event_date is already a date object or ISO string
                    if isinstance(event['event_date'], str):
                        event_date = datetime.fromisoformat(event['event_date'].replace('Z', '+00:00')).date()
                    else:
                        event_date = event['event_date']
                    dates.append(event_date)
                except:
                    continue

        if len(dates) < 2:
            logger.info(f"[CROSS-MONTH] Only {len(dates)} events with valid event dates - skipping cross-month deduplication")
            return 0

        # Check if events span multiple months
        min_date = min(dates)
        max_date = max(dates)
        if min_date.year == max_date.year and min_date.month == max_date.month:
            logger.info(f"[CROSS-MONTH] All events are from the same month ({min_date.year}-{min_date.month:02d}) - skipping cross-month deduplication")
            return 0

        logger.info(f"[CROSS-MONTH] Events span from {min_date.year}-{min_date.month:02d} to {max_date.year}-{max_date.month:02d} - proceeding with cross-month deduplication")

        # Convert to CyberEvent objects
        from cyber_data_collector.processing.deduplication_v2 import CyberEventType, EventSeverity, ConfidenceScore
        cyber_events = []
        for db_event in all_deduplicated_events:
            try:
                # Parse enum values properly
                event_type = db_event['event_type']
                if isinstance(event_type, str):
                    # Handle string representations like "CyberEventType.RANSOMWARE"
                    if event_type.startswith('CyberEventType.'):
                        event_type = event_type.replace('CyberEventType.', '')
                    # Map to enum
                    event_type = CyberEventType[event_type.upper().replace(' ', '_')]

                severity = db_event['severity']
                if isinstance(severity, str):
                    # Handle string representations like "EventSeverity.HIGH"
                    if severity.startswith('EventSeverity.'):
                        severity = severity.replace('EventSeverity.', '')
                    # Map to enum
                    severity = EventSeverity[severity.upper()]

                event = CyberEvent(
                    event_id=db_event['deduplicated_event_id'],
                    title=db_event['title'],
                    description=db_event['description'] or "",
                    event_type=event_type,
                    severity=severity,
                    data_sources=[],
                    affected_entities=[],
                    # Add required fields with defaults
                    australian_relevance=bool(db_event.get('is_australian_event', True)),
                    confidence=ConfidenceScore(
                        overall=float(db_event.get('confidence_score', 0.8)),
                        source_reliability=0.8,
                        data_completeness=0.8,
                        temporal_accuracy=0.8,
                        geographic_accuracy=0.8
                    )
                )
                if db_event['event_date']:
                    event.event_date = datetime.fromisoformat(db_event['event_date'].replace('Z', '+00:00')).date()
                event.enriched_event_id = db_event['deduplicated_event_id']
                cyber_events.append(event)
            except Exception as e:
                logger.warning(f"[WARNING] Failed to convert event to CyberEvent: {e}")

        # Perform deduplication across ALL events
        logger.info(f"[CROSS-MONTH] Running deduplication engine on {len(cyber_events)} events...")
        deduplicated_events = await deduplication_engine.deduplicate_events(cyber_events)

        # Count how many events were merged
        merges_performed = len(cyber_events) - len(deduplicated_events)
        logger.info(f"[CROSS-MONTH] Deduplication reduced {len(cyber_events)} events to {len(deduplicated_events)} events ({merges_performed} merges)")

        # Update existing deduplicated events to mark duplicates as inactive
        if merges_performed > 0:
            logger.info(f"[CROSS-MONTH] Updating database to reflect cross-month merges...")
            # For now, just log the results - implementing the database updates would require more complex logic
            # to track which enriched events were merged and update the deduplicated events table accordingly
            logger.info(f"[CROSS-MONTH] Cross-month merges detected but database updates not yet implemented")

        return merges_performed

    async def _store_entities_for_deduplicated_events(self, deduplicated_events, deduplicated_event_ids):
        """Store entities for deduplicated events"""
        try:
            for i, event in enumerate(deduplicated_events):
                if i < len(deduplicated_event_ids) and hasattr(event, 'affected_entities') and event.affected_entities:
                    deduplicated_event_id = deduplicated_event_ids[i]

                    with self.db._lock:
                        cursor = self.db._conn.cursor()

                        for entity in event.affected_entities:
                            # Get or create entity
                            entity_id = self._store_entity(entity)
                            if entity_id:
                                # Link to deduplicated event
                                cursor.execute("""
                                    INSERT OR IGNORE INTO DeduplicatedEventEntities (
                                        deduplicated_event_id, entity_id, relationship_type,
                                        confidence_score, source_count
                                    ) VALUES (?, ?, ?, ?, ?)
                                """, (
                                    deduplicated_event_id,
                                    entity_id,
                                    'affected',
                                    getattr(entity, 'confidence_score', 0.8),
                                    1  # source_count
                                ))

                        self.db._conn.commit()

        except Exception as e:
            logger.warning(f"[WARNING] Failed to store entities for deduplicated events: {e}")

    async def _log_processing_success(self, raw_event_id: str, stage: str, result_data: dict = None):
        """Log successful processing"""
        try:
            self.db.log_processing_attempt(raw_event_id, stage, 'success', result_data)
        except Exception as e:
            logger.warning(f"[WARNING] Failed to log processing success: {e}")

    async def _initialize_data_sources_metadata(self, source_types: List[str]):
        """Initialize DataSourcesV2 table with metadata about enabled sources"""
        try:
            from datetime import datetime

            source_metadata = {
                'GDELT': {
                    'source_name': 'GDELT Project',
                    'source_type': 'News Archive',
                    'base_url': 'https://gdeltproject.org',
                    'api_endpoint': 'BigQuery: gdelt-bq.gdeltv2.events',
                    'description': 'Global Database of Events, Language, and Tone',
                    'data_frequency': 'Real-time',
                    'coverage_scope': 'Global',
                    'reliability_score': 0.8
                },
                'WebberInsurance': {
                    'source_name': 'Webber Insurance Cyber Timeline',
                    'source_type': 'Specialized Timeline',
                    'base_url': 'https://webbinc.com.au',
                    'description': 'Australian cyber incidents timeline',
                    'data_frequency': 'Monthly',
                    'coverage_scope': 'Australia',
                    'reliability_score': 0.9
                },
                'Perplexity': {
                    'source_name': 'Perplexity AI Search',
                    'source_type': 'AI Search Engine',
                    'base_url': 'https://perplexity.ai',
                    'description': 'AI-powered search for cyber incidents',
                    'data_frequency': 'On-demand',
                    'coverage_scope': 'Global',
                    'reliability_score': 0.85
                },
                'GoogleSearch': {
                    'source_name': 'Google Custom Search',
                    'source_type': 'Web Search',
                    'base_url': 'https://www.googleapis.com',
                    'description': 'Google custom search for cyber incidents',
                    'data_frequency': 'On-demand',
                    'coverage_scope': 'Global',
                    'reliability_score': 0.75
                },
                'OAIC': {
                    'source_name': 'Australian Information Commissioner',
                    'source_type': 'Government Registry',
                    'base_url': 'https://www.oaic.gov.au',
                    'description': 'Official Australian privacy breach notifications',
                    'data_frequency': 'As reported',
                    'coverage_scope': 'Australia',
                    'reliability_score': 0.95
                }
            }

            with self.db._lock:
                cursor = self.db._conn.cursor()

                for source_type in source_types:
                    if source_type in source_metadata:
                        metadata = source_metadata[source_type]

                        # Check if already exists
                        cursor.execute("SELECT source_id FROM DataSourcesV2 WHERE source_name = ?", (metadata['source_name'],))
                        if not cursor.fetchone():
                            # Insert new data source using only existing columns
                            cursor.execute("""
                                INSERT INTO DataSourcesV2 (
                                    source_name, source_type, base_url, api_endpoint,
                                    credibility_score, is_active, last_accessed, created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                metadata['source_name'],
                                metadata['source_type'],
                                metadata['base_url'],
                                metadata.get('api_endpoint'),
                                metadata['reliability_score'],
                                True,  # is_active
                                datetime.now().isoformat(),  # last_accessed
                                datetime.now().isoformat()   # created_at
                            ))

                self.db._conn.commit()

        except Exception as e:
            logger.warning(f"[WARNING] Failed to initialize data sources metadata: {e}")

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

    @staticmethod
    def _extract_publication_date(data_sources) -> Optional[datetime]:
        """Extract the earliest publication date from available data sources"""
        publication_dates: List[datetime] = []

        if not data_sources:
            return None

        for source in data_sources:
            publication_date = getattr(source, 'publication_date', None)
            if publication_date:
                # Normalize to datetime without timezone for consistency
                if isinstance(publication_date, datetime):
                    publication_dates.append(publication_date)
                else:
                    try:
                        publication_dates.append(datetime.fromisoformat(str(publication_date)))
                    except Exception:
                        continue

        if publication_dates:
            return min(publication_dates)

        return None

    def _fallback_event_date(self, event, month_start: Optional[datetime]) -> datetime.date:
        """Fallback to first day of the configured search window if event date unavailable"""
        if hasattr(event, 'data_sources') and event.data_sources:
            for source in event.data_sources:
                fallback_date = getattr(source, 'search_start_date', None)
                if fallback_date:
                    if isinstance(fallback_date, datetime):
                        return fallback_date.date()
                    try:
                        return datetime.fromisoformat(str(fallback_date)).date()
                    except Exception:
                        continue

        if month_start:
            return month_start.date()

        # Default fallback: first day of fixed search month (June 2025)
        return datetime(2025, 6, 1).date()

    # =========================================================================
    # SCRAPING PHASE
    # =========================================================================

    async def _scrape_raw_events_for_month(self, raw_event_ids: List[str]) -> int:
        """
        Scrape URLs for specific raw events from the current month.
        Returns the number of events successfully scraped.
        """
        if not raw_event_ids:
            return 0

        # Get events that need scraping from the provided IDs
        events_to_scrape = []
        with self.db._lock:
            cursor = self.db._conn.cursor()
            for raw_event_id in raw_event_ids:
                cursor.execute("""
                    SELECT raw_event_id, source_url, raw_title
                    FROM RawEvents
                    WHERE raw_event_id = ? AND source_url IS NOT NULL AND raw_content IS NULL
                """, (raw_event_id,))
                result = cursor.fetchone()
                if result:
                    events_to_scrape.append({
                        'raw_event_id': result[0],
                        'source_url': result[1],
                        'raw_title': result[2]
                    })

        if not events_to_scrape:
            logger.debug("[SCRAPING] No events need URL scraping for this month")
            return 0

        # Filter events with valid URLs
        events_with_urls = [e for e in events_to_scrape if e.get('source_url')]
        if not events_with_urls:
            logger.debug("[SCRAPING] No events have URLs to scrape for this month")
            return 0

        logger.info(f"[SCRAPING] Found {len(events_with_urls)} events to scrape for this month")

        scraped_count = 0
        failed_scrapes = []
        # Use async context manager like the existing scraping code
        async with PlaywrightScraper() as scraper:
            # Process events in parallel batches
            import asyncio
            from tqdm.asyncio import tqdm

            # Create tasks with event info attached
            tasks = []
            for event in events_with_urls:
                task = asyncio.create_task(self._scrape_single_event_with_info(event, scraper))
                tasks.append(task)

            async for result in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Scraping URLs"):
                try:
                    scrape_result = await asyncio.wait_for(result, timeout=120.0)  # 2 minute timeout
                    if scrape_result['success']:
                        # Apply Random Forest content filtering for all events
                        if not self._apply_rf_content_filter(scrape_result['event']):
                            # Content was filtered out - mark as failed
                            failed_scrapes.append({
                                'title': scrape_result['event']['raw_title'][:50] if scrape_result['event'].get('raw_title') else 'Unknown',
                                'url': scrape_result['event'].get('source_url', 'No URL'),
                                'reason': 'Content filtered out as non-cyber (Random Forest filter)',
                                'perplexity_attempted': scrape_result.get('perplexity_attempted', False),
                                'perplexity_succeeded': scrape_result.get('perplexity_succeeded', False)
                            })
                            continue
                        scraped_count += 1
                    else:
                        # Track failed scrapes for summary with Perplexity details
                        failed_scrapes.append({
                            'title': scrape_result['event']['raw_title'][:50] if scrape_result['event'].get('raw_title') else 'Unknown',
                            'url': scrape_result['event'].get('source_url', 'No URL'),
                            'reason': scrape_result['reason'],
                            'perplexity_attempted': scrape_result.get('perplexity_attempted', False),
                            'perplexity_succeeded': scrape_result.get('perplexity_succeeded', False)
                        })
                except asyncio.TimeoutError:
                    logger.warning(f"Scraping task timed out after 120s")
                    self.stats['errors'] += 1
                except Exception as e:
                    logger.debug(f"[ERROR] Scraping error: {e}")
                    self.stats['errors'] += 1

        logger.info(f"[SCRAPING] Successfully scraped {scraped_count}/{len(events_with_urls)} events for this month")
        
        # Report failed scrapes at the end
        if failed_scrapes:
            logger.info(f"[SCRAPING] Failed to scrape {len(failed_scrapes)} events:")
            
            # Count Perplexity attempts
            perplexity_attempted_count = sum(1 for f in failed_scrapes if f.get('perplexity_attempted', False))
            perplexity_succeeded_count = sum(1 for f in failed_scrapes if f.get('perplexity_succeeded', False))
            
            if perplexity_attempted_count > 0:
                logger.info(f"[SCRAPING] Perplexity fallback: {perplexity_attempted_count} attempted, {perplexity_succeeded_count} succeeded")
            
            for i, failed in enumerate(failed_scrapes[:10], 1):  # Show first 10 failures
                perplexity_info = ""
                if failed.get('perplexity_attempted', False):
                    if failed.get('perplexity_succeeded', False):
                        perplexity_info = " (Perplexity fallback succeeded but content still too short)"
                    else:
                        perplexity_info = " (Perplexity fallback attempted but failed)"
                
                logger.info(f"[SCRAPING] {i}. {failed['title']}... - {failed['reason']}{perplexity_info}")
                logger.info(f"[SCRAPING]    URL: {failed['url']}")
            if len(failed_scrapes) > 10:
                logger.info(f"[SCRAPING] ... and {len(failed_scrapes) - 10} more failures")
        else:
            logger.info("[SCRAPING] All events scraped successfully!")
            
        return scraped_count

    def _apply_rf_content_filter(self, event: Dict[str, Any]) -> bool:
        """Apply Random Forest content filtering to determine if event should be kept."""
        try:
            # Get the scraped content from the database
            with self.db._lock:
                cursor = self.db._conn.cursor()
                cursor.execute("""
                    SELECT raw_title, raw_description, raw_content, source_url, source_type FROM RawEvents WHERE raw_event_id = ?
                """, (event.get('raw_event_id'),))
                result = cursor.fetchone()
                if not result:
                    return False

                title, description, content, url, source_type = result

            # Use the Random Forest filtering system
            filter_result = self.filter_system.should_keep_event(
                source_type=source_type or "",
                title=title or "",
                description=description or "",
                content=content or "",
                url=url or ""
            )

            # Store the confidence score in the raw event metadata for later use
            if filter_result.confidence_score is not None:
                self._update_raw_event_confidence(event.get('raw_event_id'), filter_result)

            return filter_result.is_cyber_relevant

        except Exception as e:
            logger.debug(f"[RF_FILTER] Error applying Random Forest filter: {e}")
            # If filtering fails, err on the side of caution and keep the event
            return True

    def _update_raw_event_confidence(self, raw_event_id: str, filter_result):
        """Update raw event with confidence score and filtering metadata."""
        try:
            # Update the raw event with confidence information
            with self.db._lock:
                cursor = self.db._conn.cursor()

                # Get existing metadata
                cursor.execute("""
                    SELECT source_metadata FROM RawEvents WHERE raw_event_id = ?
                """, (raw_event_id,))
                result = cursor.fetchone()

                existing_metadata = {}
                if result and result[0]:
                    try:
                        existing_metadata = json.loads(result[0])
                    except (json.JSONDecodeError, TypeError):
                        existing_metadata = {}

                # Add filtering information
                existing_metadata.update({
                    'content_filter_confidence': filter_result.confidence_score,
                    'content_filter_reasoning': filter_result.reasoning,
                    'content_filter_stage': filter_result.stage,
                    'content_filter_risk_level': filter_result.risk_level
                })

                # Update the metadata
                cursor.execute("""
                    UPDATE RawEvents SET source_metadata = ? WHERE raw_event_id = ?
                """, (json.dumps(existing_metadata), raw_event_id))

                self.db._conn.commit()

        except Exception as e:
            logger.warning(f"[WARNING] Failed to update raw event confidence: {e}")

    async def scrape_event_urls(self, max_events: Optional[int] = None, source_types: List[str] = None):
        """
        Scrape URLs from raw events to get full content using Playwright.
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

        scraped_count = 0
        failed_scrapes = []
        async with PlaywrightScraper() as scraper:
            # Create tasks with event info attached
            tasks = []
            for event in events_with_urls:
                task = asyncio.create_task(self._scrape_single_event_with_info(event, scraper))
                tasks.append(task)
            
            with tqdm(total=len(tasks), desc="Web Scraping URLs", unit="URL") as pbar:
                for future in asyncio.as_completed(tasks):
                    try:
                        result = await future
                        if result['success']:
                            scraped_count += 1
                            pbar.set_postfix({"success": f"{scraped_count}/{len(events_with_urls)}"})
                        else:
                            # Track failed scrapes for summary
                            failed_scrapes.append({
                                'title': result['event']['raw_title'][:50] if result['event'].get('raw_title') else 'Unknown',
                                'url': result['event'].get('source_url', 'No URL'),
                                'reason': result['reason']
                            })
                            pbar.set_postfix({"failed": "Content too short"})
                    except Exception as e:
                        logger.debug(f"[ERROR] Scraping error: {e}")
                        self.stats['errors'] += 1
                        pbar.set_postfix({"error": "Exception"})
                    pbar.update(1)

        self.stats['events_scraped'] = scraped_count
        logger.info(f"[SUCCESS] Scraping complete: {scraped_count} events successfully scraped")

        # Report failed scrapes at the end
        if failed_scrapes:
            logger.info(f"[SCRAPING] Failed to scrape {len(failed_scrapes)} events:")
            for i, failed in enumerate(failed_scrapes[:10], 1):  # Show first 10 failures
                logger.info(f"[SCRAPING] {i}. {failed['title']}... - {failed['reason']}")
                logger.info(f"[SCRAPING]    URL: {failed['url']}")
            if len(failed_scrapes) > 10:
                logger.info(f"[SCRAPING] ... and {len(failed_scrapes) - 10} more failures")
        else:
            logger.info("[SCRAPING] All events scraped successfully!")

    async def _scrape_single_event_with_info(self, event: Dict[str, Any], scraper: PlaywrightScraper) -> Dict[str, Any]:
        """
        Scrape a single event URL and return success status with event info.
        
        Returns:
            Dict with 'success' (bool), 'event' (dict), 'reason' (str), 'perplexity_attempted' (bool), 'perplexity_succeeded' (bool) keys
        """
        try:
            result = await self._scrape_single_event(event, scraper)
            return {
                'success': result['success'],
                'event': event,
                'reason': result['reason'],
                'perplexity_attempted': result['perplexity_attempted'],
                'perplexity_succeeded': result['perplexity_succeeded']
            }
        except Exception as e:
            return {
                'success': False,
                'event': event,
                'reason': f'Exception: {str(e)[:100]}',
                'perplexity_attempted': False,
                'perplexity_succeeded': False
            }

    async def _scrape_single_event(self, event: Dict[str, Any], scraper: PlaywrightScraper) -> Dict[str, Any]:
        """
        Scrape a single event URL and update the database using Playwright.

        Args:
            event: Raw event dictionary
            scraper: An instance of PlaywrightScraper

        Returns:
            Dict with 'success' (bool), 'perplexity_attempted' (bool), 'perplexity_succeeded' (bool), 'reason' (str)
        """
        start_time = time.time()
        url = event.get('source_url')
        if not url:
            return {'success': False, 'perplexity_attempted': False, 'perplexity_succeeded': False, 'reason': 'No URL provided'}

        try:
            # Extract event date for Perplexity fallback context
            event_date = None
            if 'event_date' in event and event['event_date']:
                event_date = str(event['event_date'])
            elif 'publication_date' in event and event['publication_date']:
                event_date = str(event['publication_date'])

            # Track if we're about to attempt Perplexity fallback
            original_url = url
            content = await scraper.get_page_text(url, event_date=event_date)
            processing_time_ms = int((time.time() - start_time) * 1000)

            # Check if Perplexity fallback was used by comparing URLs
            perplexity_attempted = False
            perplexity_succeeded = False
            if content and len(content.strip()) > 50:
                # Check if the URL changed (indicating Perplexity found an alternative)
                if url != original_url:
                    perplexity_attempted = True
                    perplexity_succeeded = True
                
                self._update_raw_event_content(event['raw_event_id'], content, url)
                self.db.log_processing_attempt(
                    event['raw_event_id'], 'url_scraping', 'success',
                    result_data={'content_length': len(content), 'url': url, 'perplexity_used': perplexity_succeeded},
                    processing_time_ms=processing_time_ms
                )
                return {
                    'success': True, 
                    'perplexity_attempted': perplexity_attempted, 
                    'perplexity_succeeded': perplexity_succeeded,
                    'reason': 'Success' + (' (via Perplexity fallback)' if perplexity_succeeded else '')
                }
            else:
                # Content was retrieved but too short - try Perplexity fallback if not already attempted
                if url == original_url:  # Perplexity hasn't been tried yet
                    logger.debug(f"[PERPLEXITY] Content too short ({len(content) if content else 0} chars), trying Perplexity fallback for: {url}")
                    perplexity_content = await scraper._perplexity_fallback(url, event_date)
                    if perplexity_content and len(perplexity_content.strip()) > 50:
                        # Perplexity found better content
                        self._update_raw_event_content(event['raw_event_id'], perplexity_content, url)
                        self.db.log_processing_attempt(
                            event['raw_event_id'], 'url_scraping', 'success',
                            result_data={'content_length': len(perplexity_content), 'url': url, 'perplexity_used': True},
                            processing_time_ms=processing_time_ms
                        )
                        return {
                            'success': True, 
                            'perplexity_attempted': True, 
                            'perplexity_succeeded': True,
                            'reason': 'Success via Perplexity fallback (content too short)'
                        }
                
                # Perplexity was attempted but failed, or already tried
                perplexity_attempted = (url != original_url) or True  # We tried it above
                perplexity_succeeded = False
                
                self.db.log_processing_attempt(
                    event['raw_event_id'], 'url_scraping', 'failed',
                    error_message="Insufficient content retrieved",
                    processing_time_ms=processing_time_ms
                )
                return {
                    'success': False, 
                    'perplexity_attempted': perplexity_attempted, 
                    'perplexity_succeeded': perplexity_succeeded,
                    'reason': 'Content too short or empty' + (' (Perplexity fallback attempted but failed)' if perplexity_attempted else '')
                }
        except Exception as e:
            processing_time_ms = int((time.time() - start_time) * 1000)
            # Check if Perplexity was attempted based on URL change
            perplexity_attempted = (url != original_url)
            perplexity_succeeded = False
            
            self.db.log_processing_attempt(
                event['raw_event_id'], 'url_scraping', 'failed',
                error_message=str(e),
                processing_time_ms=processing_time_ms
            )
            logger.debug(f"[ERROR] Scraping error for {event.get('raw_title', 'Unknown')}: {e}")
            return {
                'success': False, 
                'perplexity_attempted': perplexity_attempted, 
                'perplexity_succeeded': perplexity_succeeded,
                'reason': f'Exception: {str(e)[:100]}' + (' (Perplexity fallback attempted but failed)' if perplexity_attempted else '')
            }

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

            # Apply final stage filtering with Random Forest
            content = self._prepare_content_for_analysis(raw_event)
            final_filter_result = self.filter_system.should_keep_event(
                source_type=raw_event.get('source_type', ''),
                title=raw_event.get('raw_title', ''),
                description=raw_event.get('raw_description', ''),
                content=content or '',
                url=raw_event.get('source_url', ''),
                metadata={
                    'llm_analysis': {
                        'is_australian_event': enriched_data.is_australian_event,
                        'is_specific_event': enriched_data.is_specific_event,
                        'confidence_score': getattr(enriched_data, 'confidence_score', 0.7),
                        'primary_entity': getattr(enriched_data, 'primary_entity', None)
                    }
                }
            )

            # Only create enriched event if it passes final filtering
            if final_filter_result.is_cyber_relevant:
                enriched_event_data = self._prepare_enriched_event_data(raw_event, enriched_data)
                # Add final filtering confidence to the enriched event data
                enriched_event_data['final_filter_confidence'] = final_filter_result.confidence_score
                enriched_event_data['final_filter_reasoning'] = json.dumps(final_filter_result.reasoning)

                enriched_event_id = self.db.create_enriched_event(raw_event_id, enriched_event_data)

                # Log successful final filtering
                self.db.log_processing_attempt(
                    raw_event_id, 'final_filtering', 'success',
                    result_data={
                        'final_confidence': final_filter_result.confidence_score,
                        'reasoning': final_filter_result.reasoning,
                        'risk_level': final_filter_result.risk_level
                    },
                    processing_time_ms=processing_time_ms
                )

                # Mark raw event as processed
                self.db.mark_raw_event_processed(raw_event_id)
                return True
            else:
                # Event filtered out in final stage
                reasons = final_filter_result.reasoning

                self.db.log_processing_attempt(
                    raw_event_id, 'final_filtering', 'filtered_out',
                    result_data={
                        'final_confidence': final_filter_result.confidence_score,
                        'reasons': reasons,
                        'risk_level': final_filter_result.risk_level,
                        'llm_australian': enriched_data.is_australian_event,
                        'llm_specific': enriched_data.is_specific_event
                    },
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

    def print_filtering_statistics(self):
        """Print Random Forest filtering statistics for the current run"""
        print("\n" + "="*60)
        print("[FILTERING] RANDOM FOREST FILTERING STATISTICS (CURRENT RUN)")
        print("="*60)

        filter_stats = self.filter_system.get_filtering_statistics()

        print(f"[RF_FILTER] Events processed: {filter_stats['events_processed']}")
        print(f"[RF_FILTER] Events kept: {filter_stats['events_kept']} ({filter_stats['keep_rate']:.1%})")
        print(f"[RF_FILTER] Events filtered: {filter_stats['events_filtered']} ({filter_stats['filter_rate']:.1%})")
        print(f"[RF_FILTER] Prediction errors: {filter_stats['prediction_errors']} ({filter_stats['error_rate']:.1%})")

        print("="*60)

    async def run_global_deduplication(self):
        """Run global deduplication once after all data collection"""
        logger.info("[GLOBAL DEDUPLICATION] Starting global deduplication...")
        
        try:
            # Import the new deduplication system
            from cyber_data_collector.processing.deduplication_v2 import DeduplicationEngine, LLMArbiter, DeduplicationValidator
            from cyber_data_collector.storage.deduplication_storage import DeduplicationStorage
            
            # Load ALL enriched events (not just recent months)
            all_events = await self._load_all_enriched_events()
            logger.info(f"[GLOBAL DEDUPLICATION] Loaded {len(all_events)} enriched events for deduplication")
            
            if not all_events:
                logger.warning("[GLOBAL DEDUPLICATION] No enriched events found, skipping deduplication")
                return
            
            # Clear existing deduplications
            storage = DeduplicationStorage(self.db._conn)
            storage.clear_existing_deduplications()
            logger.info("[GLOBAL DEDUPLICATION] Cleared existing deduplicated events")
            
            # Load entity mappings from database
            entity_mappings = self._load_entity_mappings()
            if entity_mappings:
                logger.info(f"[GLOBAL DEDUPLICATION] Loaded {len(entity_mappings)} entity mappings")

            # Run deduplication
            engine = DeduplicationEngine(
                similarity_threshold=0.75,
                llm_arbiter=LLMArbiter(api_key=os.getenv('OPENAI_API_KEY')),
                validators=[DeduplicationValidator()],
                entity_mappings=entity_mappings
            )
            
            logger.info("[GLOBAL DEDUPLICATION] Running deduplication engine...")
            result = engine.deduplicate(all_events)
            
            # Log validation warnings (non-fatal - still proceed with storage)
            if result.validation_errors:
                logger.warning(f"[GLOBAL DEDUPLICATION] Validation found {len(result.validation_errors)} issues (non-fatal)")
                for error in result.validation_errors:
                    logger.warning(f"[VALIDATION WARNING] {error.error_type}: {error.message}")

            # Store result
            logger.info("[GLOBAL DEDUPLICATION] Storing deduplication results...")
            storage_result = storage.store_deduplication_result(result)
            
            if not storage_result.success:
                logger.error(f"[GLOBAL DEDUPLICATION] Storage failed: {len(storage_result.validation_errors)} errors")
                raise ValueError("Failed to store deduplication results")
            
            # Final validation
            logger.info("[GLOBAL DEDUPLICATION] Validating storage integrity...")
            integrity_errors = storage.validate_storage_integrity()
            if integrity_errors:
                logger.error(f"[GLOBAL DEDUPLICATION] Storage integrity check failed: {len(integrity_errors)} issues")
                for error in integrity_errors:
                    logger.error(f"[INTEGRITY ERROR] {error.error_type}: {error.message}")
                raise ValueError("Database contains duplicates after deduplication")
            
            # Log success statistics
            stats = result.statistics
            logger.info(f"[GLOBAL DEDUPLICATION] Deduplication complete: {stats.input_events} -> {stats.output_events} events")
            logger.info(f"[GLOBAL DEDUPLICATION] Merge groups: {stats.merge_groups}, Total merges: {stats.total_merges}")
            logger.info(f"[GLOBAL DEDUPLICATION] Average confidence: {stats.avg_confidence:.2f}")
            logger.info(f"[GLOBAL DEDUPLICATION] Processing time: {stats.processing_time_seconds:.1f}s")
            
            return result.statistics
            
        except Exception as e:
            logger.error(f"[GLOBAL DEDUPLICATION] Failed: {e}")
            raise

    def _get_deduplicated_event_count(self) -> int:
        """Get the count of active deduplicated events"""
        try:
            cursor = self.db._conn.execute("""
                SELECT COUNT(*) FROM DeduplicatedEvents WHERE status = 'Active'
            """)
            return cursor.fetchone()[0]
        except Exception as e:
            logger.warning(f"[DEDUPLICATION] Could not count deduplicated events: {e}")
            return 0

    def _load_entity_mappings(self) -> Dict[str, str]:
        """Load entity mappings from the EntityMappings table.

        Returns a dict mapping source_entity -> canonical_entity.
        Used to normalize entity names during deduplication (e.g., Ticketmaster -> Live Nation).
        """
        try:
            cursor = self.db._conn.execute("""
                SELECT source_entity, canonical_entity
                FROM EntityMappings
            """)
            mappings = {row[0]: row[1] for row in cursor.fetchall()}
            return mappings
        except Exception as e:
            logger.warning(f"[ENTITY MAPPINGS] Could not load entity mappings: {e}")
            return {}

    async def _load_all_enriched_events(self):
        """Load all enriched events from the database for global deduplication"""
        try:
            # Query all enriched events from the database
            cursor = self.db._conn.cursor()
            cursor.execute("""
                SELECT enriched_event_id, title, summary, event_date, event_type, severity, 
                       records_affected, confidence_score
                FROM EnrichedEvents
                WHERE status = 'Active'
                ORDER BY event_date DESC
            """)
            
            enriched_events = []
            for row in cursor.fetchall():
                # Convert database row to CyberEvent object
                event = CyberEvent(
                    event_id=row[0],
                    title=row[1],
                    summary=row[2],
                    event_date=datetime.fromisoformat(row[3]).date() if row[3] else None,
                    event_type=row[4],
                    severity=row[5],
                    records_affected=row[6],
                    data_sources=[],  # Not available in EnrichedEvents
                    urls=[],  # Not available in EnrichedEvents
                    confidence=row[7] if row[7] else 0.5
                )
                enriched_events.append(event)
            
            logger.info(f"[GLOBAL DEDUPLICATION] Loaded {len(enriched_events)} enriched events from database")
            return enriched_events
            
        except Exception as e:
            logger.error(f"[GLOBAL DEDUPLICATION] Failed to load enriched events: {e}")
            return []

    def close(self):
        """Clean up resources"""
        # Log final filtering summary
        self.filter_system.log_filtering_summary()
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
    parser.add_argument('--source', choices=['Perplexity', 'GoogleSearch', 'WebberInsurance', 'OAIC'],
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

    # GDELT authentication check removed - GDELT disabled

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

        # Print filtering statistics
        pipeline.print_filtering_statistics()

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
