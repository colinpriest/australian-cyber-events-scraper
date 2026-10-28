"""
Discovery and enrichment pipeline shared across entry points.

This module contains the canonical discovery/enrichment implementation used by the unified
pipeline as well as the legacy CLI entry point.
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tqdm import tqdm

# Handle Windows encoding issues
if sys.platform == "win32" and sys.stdout.encoding != "utf-8":
    import codecs
    sys.stdout = codecs.getwriter("utf-8")(sys.stdout.buffer)

from cyber_data_collector.storage.cyber_event_data_v2 import CyberEventDataV2
from cyber_data_collector.utils.entity_scraper import PlaywrightScraper, is_blocked_domain
from cyber_data_collector.utils.llm_extractor import extract_event_details_with_llm

# Data collection imports
from cyber_data_collector import CyberDataCollector, CollectionConfig, DateRange
from cyber_data_collector.models.config import DataSourceConfig
from cyber_data_collector.utils import ConfigManager, setup_logging
from cyber_data_collector.utils.validation import (
    safe_bool,
    safe_float,
    safe_int,
    safe_json_dumps,
    safe_str,
    validate_enrichment_data_for_storage,
)
from cyber_data_collector.filtering.page_gate import DiscoveryPageGate, GateDecision, PageGateUnavailable
from cyber_data_collector.models.events import ConfidenceScore, CyberEvent, CyberEventType, EventSeverity, EventSource
from cyber_data_collector.processing.enrichment_gate import validate_count, validate_iso_date
from cyber_data_collector.processing.record_evidence import (
    STAGE_DOMAIN_BLOCKLIST,
    STAGE_PAGE_GATE,
    AcceptanceLedger,
    RecordEvidence,
    persist_rejections,
    prepare_records_for_processing,
)
from cyber_data_collector.processing.fetch_errors import INCOMPLETE, OTHER, classify_error, error_type_from_text
from cyber_data_collector.processing.retry_queue import (
    KIND_PERMANENT,
    KIND_TEMPORARY,
    MONTH_COMPLETE,
    MONTH_GAVE_UP,
    MONTH_WAITING,
    RetryQueue,
)
from cyber_data_collector.utils.api_budget import is_budget_refusal


#: How far back Cyber Daily's first run searches (the dashboard window starts in 2020).
CYBERDAILY_BACKFILL_SINCE: Tuple[int, int] = (2020, 1)
#: Stored Cyber Daily stories re-fetched each run: published/discovered this many days back.
CYBERDAILY_REFETCH_DAYS = 90

#: ``CyberDataCollector.data_sources`` keys -> ``RawEvents.source_type``.
SOURCE_TYPES_BY_COLLECTOR = {
    "perplexity": "Perplexity",
    "google_search": "GoogleSearch",
    "webber": "WebberInsurance",
    "oaic": "OAIC",
    "gdelt": "GDELT",
    "cyberdaily": "CyberDaily",
}


def _enum_value_or_str(value: Any) -> Optional[str]:
    """Serialise an enum (or any value) for DB storage.

    Returns ``value.value`` for Enum members (e.g. ``EventSeverity.HIGH`` -> ``"High"``)
    rather than ``str(value)`` which yields the qualified name (``"EventSeverity.HIGH"``)
    on Python 3.11+ and gets truncated by downstream VARCHAR validators.
    """
    if value is None:
        return None
    return value.value if hasattr(value, 'value') else str(value)


def _parse_event_date(raw: Any):
    """Coerce a SQLite-returned event_date into a ``datetime.date``.

    SQLite's behaviour depends on column type (TEXT vs DATE) AND the
    connection's detect_types config: it can return any of str / date /
    datetime / None for the same logical column. This helper handles
    every shape so callers don't trip the recurring
    ``fromisoformat: argument must be str`` bug.

    Returns ``None`` for unparseable / missing values.
    """
    from datetime import date as _date, datetime as _datetime
    if raw is None:
        return None
    if isinstance(raw, _datetime):
        return raw.date()
    if isinstance(raw, _date):
        return raw
    if isinstance(raw, str):
        try:
            return _datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


# Configure logging with Unicode support + tqdm-safe output
class UnicodeStreamHandler(logging.StreamHandler):
    def emit(self, record):
        try:
            msg = self.format(record)
            # Remove Unicode characters that cause issues on Windows
            msg = msg.encode('ascii', errors='replace').decode('ascii')
            # Use tqdm.write() so log lines don't corrupt active progress bars
            try:
                from tqdm import tqdm as _tqdm
                _tqdm.write(msg, file=self.stream)
            except ImportError:
                self.stream.write(msg + self.terminator)
                self.flush()
        except Exception:
            self.handleError(record)

setup_logging(
    log_file="logs/discover_enrich_events.log",
    stream_handler=UnicodeStreamHandler(),
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

    def __init__(self, db_path: str = "instance/cyber_events.db", page_gate_mode: Optional[str] = None,
                 page_gate: Optional[DiscoveryPageGate] = None):
        """
        Args:
            db_path: Database to work on.
            page_gate_mode: ``'enforce'`` (default) or ``'report-only'`` for the
                Jev page gate; ``None`` reads ``DISCOVERY_PAGE_GATE_MODE``.
            page_gate: A ready gate (tests inject one with a fake judge).
        """
        self.db_path = str(db_path)
        self.db = CyberEventDataV2(db_path)
        self.config_manager = ConfigManager()
        self.env_config = self.config_manager.load()

        # The discovery filter: the calibrated Jev page gate (the Random Forest
        # filter is retired - it dropped real incidents as often as junk).
        self.page_gate = page_gate or DiscoveryPageGate(self.db._conn, mode=page_gate_mode)
        # Stored-but-unprocessed records, retried on every run with back-off.
        self.retry_queue = RetryQueue.for_db(self.db)

        # Statistics
        self.stats = {
            'events_discovered': 0,
            'events_scraped': 0,
            'events_analyzed': 0,
            'events_enriched': 0,
            'errors': 0,
            'retry_due': 0,
            'retry_completed': 0,
            'records_failed_temporary': 0,
            'records_failed_permanent': 0,
            'records_quarantined': 0,
            'source_months_failed': 0,
        }

    # =========================================================================
    # DISCOVERY PHASE
    # =========================================================================

    async def discover_events(self, source_types: List[str] = None, date_range_days: int = 7, max_events: int = 1000,
                              cyberdaily_backfill_since: Optional[Tuple[int, int]] = CYBERDAILY_BACKFILL_SINCE,
                              cyberdaily_refetch_days: Optional[int] = CYBERDAILY_REFETCH_DAYS,
                              cyberdaily_refetch: bool = True):
        """
        Discover new events from configured data sources, processing month by month.

        With CyberDaily among the sources, two more steps run after the months
        (see :meth:`_cyberdaily_history`): every processed month back to
        ``cyberdaily_backfill_since`` that Cyber Daily has not searched yet is
        searched (the first run goes back to 2020; later runs find nothing left
        to do), and stored Cyber Daily stories are re-fetched
        (``cyberdaily_refetch_days``; None = all of them).

        Args:
            source_types: List of sources to use ('GDELT', 'Perplexity', 'GoogleSearch', 'WebberInsurance',
                'OAIC', 'CyberDaily')
            date_range_days: How many days back to search (limits the month range to process)
            max_events: Maximum events per source per month
        """
        # Default to all sources if none specified
        if not source_types:
            source_types = ['Perplexity', 'GoogleSearch', 'WebberInsurance', 'OAIC', 'CyberDaily']

        # Fail before any paid discovery call if a required model is missing.
        self.require_prerequisites()

        # Records stored by earlier runs but never finished (a classification
        # timeout, a crash) are not rediscovered - OAIC skips stored URLs - so
        # they are drained first, from the database.
        await self.drain_retry_queue()

        # Get current year and month for dynamic date range
        from datetime import datetime
        now = datetime.now()
        current_year = now.year
        current_month = now.month

        # Determine date window based on lookback days
        if date_range_days and date_range_days > 0:
            start_date = now - timedelta(days=date_range_days)
            start_year = start_date.year
            start_month = start_date.month
            logger.info(f"[DISCOVERY] Using {date_range_days}-day lookback from {start_date.date()} to {now.date()}")
        else:
            start_date = datetime(2020, 1, 1)
            start_year = 2020
            start_month = 1
            logger.info("[DISCOVERY] No lookback specified; processing all months from Jan 2020")

        # Get list of unprocessed months in the selected range
        unprocessed_months = self.db.get_unprocessed_months(
            start_year=start_year, start_month=start_month,
            end_year=current_year, end_month=current_month
        )

        # Build a list of all months in range to keep rolling window constrained
        allowed_months = set()
        temp_year, temp_month = start_year, start_month
        while (temp_year < current_year) or (temp_year == current_year and temp_month <= current_month):
            allowed_months.add((temp_year, temp_month))
            temp_month += 1
            if temp_month > 12:
                temp_month = 1
                temp_year += 1

        # Always reprocess the last 3 calendar months (including current) to catch late-reported events
        recent_months = []
        temp_year, temp_month = current_year, current_month
        for _ in range(3):
            if (temp_year, temp_month) in allowed_months:
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
            logger.info(
                "[DISCOVERY] All months from %s to %s have been processed!",
                start_date.strftime('%b %Y'),
                now.strftime('%b %Y'),
            )
            await self._cyberdaily_history(source_types, max_events, cyberdaily_backfill_since,
                                           cyberdaily_refetch_days if cyberdaily_refetch else False)
            self.report_incomplete_months()
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
        await self._cyberdaily_history(source_types, max_events, cyberdaily_backfill_since,
                                       cyberdaily_refetch_days if cyberdaily_refetch else False)
        self.report_incomplete_months()

        # Deduplication is deliberately NOT run here. It is owned by the caller
        # (run_full_pipeline.UnifiedPipeline.run_deduplication_phase), which runs
        # the incremental DeduplicationMigration after Perplexity enrichment.
        #
        # This method used to clear DeduplicatedEvents and rebuild it from
        # scratch whenever a single new event was discovered. That erased every
        # curated merge, split, canonicalised victim and entity role from the
        # dedup v3 repair passes, and orphaned every ASD classification, on
        # every routine refresh (observed 2026-09-15: 762 curated events
        # rebuilt as 1,087, MediSecure fragmented back into 10).

    async def _cyberdaily_history(self, source_types: List[str], max_events: int,
                                  backfill_since: Optional[Tuple[int, int]], refetch_days: Any) -> None:
        """Cyber Daily's deep backfill and re-fetch (no-op unless CyberDaily is a source).

        ``refetch_days`` is a window in days, None for every stored story, or
        False to skip the re-fetch. A failed re-fetch is counted and logged as
        an error; it does not stop the run.
        """
        if "CyberDaily" not in (source_types or []):
            return
        if backfill_since:
            now = datetime.now()
            months, (year, month) = [], backfill_since
            while (year, month) <= (now.year, now.month):
                months.append((year, month))
                year, month = (year + 1, 1) if month == 12 else (year, month + 1)
            result = await self.backfill_source("CyberDaily", months, max_events)
            self.stats['cyberdaily_backfill_months'] = len(result["searched"])
            logger.info("[CYBERDAILY] Backfill since %d-%02d: searched %d month(s), %d already done, %d not "
                        "processed yet, %d failed, %d waiting for back-off, %d given up", backfill_since[0],
                        backfill_since[1], len(result["searched"]), len(result["already_done"]),
                        len(result["not_processed"]), len(result["failed"]), len(result["waiting"]),
                        len(result["gave_up"]))
            if result["gave_up"]:
                logger.error("[CYBERDAILY] Backfill months given up (not searched again until released): %s",
                             ", ".join(f"{y}-{m:02d}" for y, m in result["gave_up"]))
        if refetch_days is False:
            return
        from cyber_data_collector.processing import cyberdaily_refetch

        report = await cyberdaily_refetch.refetch_stories_async(self.db._conn, days=refetch_days)
        outcomes = report.outcomes
        self.stats['cyberdaily_refetch_failed'] = outcomes[cyberdaily_refetch.FAILED]
        logger.info("[CYBERDAILY] Re-fetched %d stored stories (%d of them incomplete): %d unchanged, %d updated "
                    "(%d enrichment(s) queued, %d record(s) reopened for classification), %d kept (fetched page "
                    "cut off), %d failed (%d still incomplete, %d missing, %d given up)", report.candidates,
                    report.repair_candidates, outcomes["unchanged"], outcomes["updated"],
                    report.requeued_enrichments, report.reopened, outcomes["kept_stored"], outcomes["failed"],
                    report.still_incomplete, len(report.missing), len(report.gave_up))
        for failure in report.failed[:10]:
            logger.error("[CYBERDAILY] Re-fetch failed for %s (%s): %s", failure["url"], failure["error_type"],
                         failure["error"])
        for story in report.missing[:10]:
            logger.error("[CYBERDAILY] Stored story is gone from the site (%s), not fetched again: %s",
                         story["error"], story["url"])

    async def backfill_source(self, source_type: str, months: List[Tuple[int, int]],
                              max_events: int = 1000, force: bool = False) -> Dict[str, List[Tuple[int, int]]]:
        """Search already-processed months with one source added after they were processed.

        ``MonthProcessed`` is per month, not per source, so a source added later
        (Cyber Daily, 2026-10) is only ever run for the rolling three-month
        window. This runs it alone over earlier months, through the same path
        as discovery (storage, retry queue, page gate, classification).

        Idempotent: a month the source has already searched completely
        (``SourceMonthDiscovery``) is skipped, so an interrupted backfill
        resumes (``force=True`` searches them again). A month nothing has
        processed yet is skipped too - normal
        discovery covers it with every source, and recording it as processed
        after one source would stop the others from ever searching it.

        A month whose last search failed is searched again once its back-off
        has passed (``waiting`` until then), and not at all once it is given
        up (a 404/410, or ``retry_queue.MAX_SOURCE_MONTH_ATTEMPTS`` failures:
        ``gave_up``, reported, and searched again only after ``release`` or
        with ``force=True``).

        Returns:
            ``{'searched': [...], 'already_done': [...], 'not_processed': [...],
            'failed': [...], 'waiting': [...], 'gave_up': [...]}`` of ``(year, month)``.
        """
        self.require_prerequisites()
        await self.drain_retry_queue()
        result: Dict[str, List[Tuple[int, int]]] = {
            "searched": [], "already_done": [], "not_processed": [], "failed": [], "waiting": [], "gave_up": []}
        skip = {MONTH_COMPLETE: "already_done", MONTH_WAITING: "waiting", MONTH_GAVE_UP: "gave_up"}
        for year, month in months:
            if not self.db.is_month_processed(year, month):
                result["not_processed"].append((year, month))
                continue
            state = self.retry_queue.source_month_retry(source_type, year, month)
            if not force and state in skip:
                result[skip[state]].append((year, month))
                continue
            logger.info("[BACKFILL] %s %d-%02d", source_type, year, month)
            await self._discover_events_for_month(year, month, [source_type], max_events, mark_processed=False)
            status = self.retry_queue.discovery_status(source_type, year, month)
            result["searched" if status == "complete" else "failed"].append((year, month))
        self.report_incomplete_months()
        return result

    async def _discover_events_for_month(self, year: int, month: int, source_types: List[str], max_events: int,
                                         mark_processed: bool = True) -> int:
        """
        Discover events for a specific month.
        Returns the number of events discovered.

        ``mark_processed=False`` (a backfill of an already-processed month with
        one source) leaves ``MonthProcessed`` as it is, so its totals and notes
        keep describing the full run.
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
            collector = CyberDataCollector(collection_config, ".env", db_path=self.db_path)

            # Inject known URLs so OAIC source can skip already-stored articles.
            # Safe because a stored record that was never finished is drained
            # from the retry queue, not left waiting for a rediscovery.
            if 'oaic' in collector.data_sources:
                known = self.db.get_known_source_urls('OAIC')
                collector.data_sources['oaic'].known_urls = known
                if known:
                    logger.info(f"[OAIC] {len(known)} already-known URLs will be skipped")
            # A Cyber Daily article already stored by any source (Webber,
            # Google and Perplexity cite it too) is not stored a second time.
            if 'cyberdaily' in collector.data_sources:
                collector.data_sources['cyberdaily'].known_urls = self.db.get_source_urls_containing("cyberdaily.au")

            # Step 1: Raw collection (same as collector.collect_all_events but broken down)
            valid_sources = {}
            for name, source in collector.data_sources.items():
                if source.validate_config():
                    valid_sources[name] = source

            # Collect raw events from all sources.  A provider failure that
            # loses results (an exception, a failed query, an exhausted quota)
            # is recorded per source-month; such a month is not marked
            # processed, so it is searched again on the next run.
            collected = await asyncio.gather(*[
                self._collect_source(name, source, date_range) for name, source in valid_sources.items()])
            all_raw_events = []
            failed_sources: Dict[str, List[str]] = {}
            for name, source_events, failures, error_types, items_seen in collected:
                all_raw_events.extend(source_events)
                source_type = SOURCE_TYPES_BY_COLLECTOR.get(name, name)
                status = self.retry_queue.record_discovery(source_type, year, month, len(source_events), failures,
                                                           error_types=error_types, items_seen=items_seen)
                if status != "complete":
                    failed_sources[source_type] = failures
            for name in set(collector.data_sources) - set(valid_sources):
                # Enabled but not configured (no key, no client): nothing was searched.
                source_type = SOURCE_TYPES_BY_COLLECTOR.get(name, name)
                problem = [f"{source_type} is enabled but not configured"]
                self.retry_queue.record_discovery(source_type, year, month, 0, problem, error_types=[OTHER])
                failed_sources[source_type] = problem

            logger.info(f"[RAW] Collected {len(all_raw_events)} raw events for {year}-{month:02d}")

            # Step 2: Store raw events
            raw_events_stored = 0
            raw_event_ids = []
            source_by_id: Dict[str, str] = {}
            for event in all_raw_events:
                raw_event_id = await self._store_raw_event(event, start_date)
                if raw_event_id:
                    raw_events_stored += 1
                    raw_event_ids.append(raw_event_id)
                    source_by_id[raw_event_id] = self._determine_source_type(event.data_sources)
                    setattr(event, "raw_event_id", raw_event_id)
                    # Log successful raw event storage
                    await self._log_processing_success(raw_event_id, 'raw_event_storage', {
                        'source_type': source_by_id[raw_event_id],
                        'title': event.title[:100]
                    })
            # Durable before any fallible step: what was found, from which
            # source, for which month.
            self.retry_queue.register(raw_event_ids, source_by_id, year, month)

            # Steps 3-5: scrape, page gate, classify, store - the same path the
            # retry queue uses.
            outcome = await self._process_stored_records(
                collector, all_raw_events, raw_event_ids, f"{year}-{month:02d}")
            enriched_events_stored = len(outcome["enriched_event_ids"])
            # No confidence threshold is applied here.  The old "final filtering
            # by confidence" ran after the records were stored and excluded
            # nothing downstream; an uncalibrated cut-off may not drop records
            # (see docs/04-event-filtering.md), so it is gone rather than
            # moved before storage.

            events_for_month = len(all_raw_events)  # Count original raw events for the month tracking

            logger.info(f"[STORAGE] Month {year}-{month:02d}: {raw_events_stored} raw, {enriched_events_stored} enriched events stored")

            # MonthProcessed records that the month's discovery searches ran.
            # Whether every record found has been processed is tracked per
            # source in SourceMonthCompletion; pending records are retried
            # from the queue on the next run without searching again.
            pending = sum(row["pending"] for row in self.retry_queue.completion(year, month))
            notes = f"Processed with sources: {', '.join(source_types)}"
            if pending:
                notes += f"; {pending} record(s) pending retry"
                logger.warning(f"[RETRY] Month {year}-{month:02d} is incomplete: {pending} record(s) pending retry")
            if not mark_processed:
                if failed_sources:
                    self.stats['source_months_failed'] += len(failed_sources)
                    for source_type, failures in sorted(failed_sources.items()):
                        logger.error("[BACKFILL] %s search of %d-%02d lost results (%d failure(s), first: %s); "
                                     "run the backfill again", source_type, year, month, len(failures),
                                     failures[0] if failures else "?")
            elif failed_sources:
                # Never record an empty or partial search as the month's answer.
                self.stats['source_months_failed'] += len(failed_sources)
                for source_type, failures in sorted(failed_sources.items()):
                    logger.error("[DISCOVERY] %s search of %d-%02d lost results (%d failure(s), first: %s); the "
                                 "month stays unprocessed and is searched again next run",
                                 source_type, year, month, len(failures), failures[0] if failures else "?")
            else:
                self.db.mark_month_as_processed(
                    year=year,
                    month=month,
                    total_raw_events=events_for_month,
                    processing_notes=notes,
                )
                logger.info(f"[SUCCESS] Month {year}-{month:02d} complete: {events_for_month} events stored")

            # Note: Cross-month deduplication removed - will be handled globally

        except Exception as e:
            logger.error(f"[ERROR] Discovery failed for {year}-{month:02d}: {e}")
            logger.error(f"[TRACEBACK] {traceback.format_exc()}")
            self.stats['errors'] += 1
            # Don't mark as processed if there was an error
            raise

        return events_for_month

    # =========================================================================
    # PROCESSING STORED RECORDS (new discoveries and the retry queue)
    # =========================================================================

    async def _collect_source(self, name: str, source: Any, date_range: DateRange):
        """Collect from one source: ``(name, events, failures, error_types, items_seen)``.

        A raised error is a failure. ``error_types`` holds the type of each
        failure (:mod:`~cyber_data_collector.processing.fetch_errors`);
        ``items_seen`` is what the source listed for the range, where it says
        (Cyber Daily), else None.
        """
        try:
            events = await source.collect_events(date_range)
        except Exception as exc:  # recorded as a failed source-month, never as "no events"
            logger.error("[DISCOVERY] Collecting from %s failed: %s", name, exc)
            return name, [], [f"collection raised {type(exc).__name__}: {exc}"[:500]], [classify_error(exc)], None
        failures = list(getattr(source, "collection_failures", []) or [])
        error_types = list(getattr(source, "collection_error_types", []) or [])
        items_seen = getattr(source, "items_seen", None)
        logger.info("[DISCOVERY] %s: %d event(s)%s", name, len(events or []),
                    f", {len(failures)} failure(s)" if failures else "")
        return name, list(events or []), failures, error_types, items_seen if isinstance(items_seen, int) else None

    def require_prerequisites(self) -> None:
        """Raise unless every model discovery depends on is configured.

        No silent fallbacks: without OpenAI the classifier used to return
        events unclassified (and they were stored); without Jev the page gate
        cannot decide. Both now stop the run before any discovery spend.
        """
        self.page_gate.require_available()
        if not (self.env_config.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")):
            raise RuntimeError("OPENAI_API_KEY is not configured: discovered records cannot be classified. "
                               "Discovery does not run without it.")

    def _make_collector(self) -> Any:
        """A collector with no data source enabled, for processing stored records."""
        now = datetime.now()
        config = self._build_collection_config([], 1000, DateRange(start_date=now - timedelta(days=30), end_date=now))
        return CyberDataCollector(config, ".env", db_path=self.db_path)

    async def drain_retry_queue(self, now: Optional[datetime] = None, limit: Optional[int] = None) -> Dict[str, int]:
        """Process stored records that are neither processed nor rejected.

        Every discovery run calls this first. A record whose back-off has not
        expired waits; a temporary failure (timeout, rate limit, outage, API
        budget) is retried indefinitely; a permanent one is quarantined after
        ``retry_queue.MAX_PERMANENT_ATTEMPTS``.

        Returns:
            ``{'due': n, 'completed': n, 'waiting': n}``
        """
        due = self.retry_queue.due(now=now, limit=limit)
        waiting = self.retry_queue.waiting(now=now)
        self.stats['retry_due'] += len(due)
        if not due:
            logger.info("[RETRY] No stored records awaiting processing" +
                        (f" ({waiting} waiting for back-off)" if waiting else ""))
            return {"due": 0, "completed": 0, "waiting": waiting}

        logger.info("[RETRY] Draining %d stored-but-unprocessed record(s) before discovery (%d waiting for back-off)",
                    len(due), waiting)
        events = [self._event_from_raw_row(row) for row in due]
        ids = [row["raw_event_id"] for row in due]
        outcome = await self._process_stored_records(self._make_collector(), events, ids, "retry queue")
        completed = len(outcome["completed_ids"])
        self.stats['retry_completed'] += completed
        logger.info("[RETRY] Retry queue: %d of %d record(s) finished; %d still pending", completed, len(due),
                    len(due) - completed)
        return {"due": len(due), "completed": completed, "waiting": waiting}

    @staticmethod
    def _event_from_raw_row(row: Dict[str, Any]) -> CyberEvent:
        """Rebuild the in-memory event discovery would have held for a stored record.

        Only what discovery stored is available - title, snippet, URL, dates
        and the metadata recorded at storage; classification reads the
        persisted article through ``prepare_records_for_processing`` anyway.
        """
        try:
            metadata = json.loads(row.get("source_metadata") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}

        def _when(value: Any) -> Optional[datetime]:
            parsed = _parse_event_date(value)
            return datetime(parsed.year, parsed.month, parsed.day) if parsed else None

        try:
            confidence = float(metadata.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = min(1.0, max(0.0, confidence))
        source_type = row.get("source_type") or "Unknown"
        description = row.get("raw_description") or ""
        return CyberEvent(
            title=row.get("raw_title") or "Untitled Event",
            description=description,
            event_type=CyberEventType.OTHER,
            severity=EventSeverity.UNKNOWN,
            event_date=_when(row.get("event_date")),
            australian_relevance=bool(metadata.get("australian_relevance") or False),
            confidence=ConfidenceScore(overall=confidence, source_reliability=0.5, data_completeness=0.5,
                                       temporal_accuracy=0.5, geographic_accuracy=0.5),
            data_sources=[EventSource(
                source_id=row["raw_event_id"], source_type=source_type, url=row.get("source_url"),
                title=row.get("raw_title"), content_snippet=description or None,
                publication_date=_when(metadata.get("publication_date")),
                credibility_score=0.5, relevance_score=0.5,
            )],
            raw_event_id=row["raw_event_id"],
        )

    async def _process_stored_records(
        self,
        collector: Any,
        events: List[Any],
        raw_event_ids: List[str],
        label: str,
    ) -> Dict[str, Any]:
        """Scrape, gate, classify and store records already in ``RawEvents``.

        The one path for a month's new records and for the retry queue. Every
        record ends this call finished (processed, or rejected with a stage
        and reason) or still pending with its failure recorded in the retry
        queue - never silently dropped.

        Raises:
            PageGateUnavailable: Jev is not configured or kept failing; the
                record that failed is queued as a temporary failure and the
                rest stay pending.
        """
        # Step 3: Scrape URL content.  A filter that rejects a record writes
        # that decision to the ledger; it is persisted so it holds for every
        # later stage, run and entry point.
        ledger = AcceptanceLedger()
        logger.info(f"[PIPELINE] Scraping URLs for {label}")
        scraped_count = await self._scrape_raw_events_for_month(raw_event_ids, ledger)
        logger.info(f"[SCRAPED] Successfully scraped {scraped_count} URLs for {label}")

        # Step 3b: the page gate judges what scraping stored.
        held_ids = self._apply_page_gate(raw_event_ids, ledger, label)

        # Step 4: Process events (entity extraction, LLM classification) from
        # what scraping persisted - not the pre-scrape in-memory objects.
        logger.info(f"[PIPELINE] Processing events for {label}")
        to_process = [e for e in events if getattr(e, "raw_event_id", None) not in held_ids]
        processed_events = await self._process_persisted_records(collector, to_process, ledger)
        logger.info(f"[PROCESSED] Enhanced {len(processed_events)} events for {label}")
        if len(ledger):
            logger.info(f"[ACCEPTANCE] {len(ledger)} record(s) rejected before classification for {label}")

        # An LLM error is not an acceptance: those records are neither stored
        # nor marked processed; the retry queue records why and when to retry.
        for event in processed_events:
            if event.classification_status not in ("classified", "failed"):
                # Never stored as an incident: nothing judged it.
                event.classification_error = (f"classifier returned the record with status "
                                              f"'{event.classification_status}', not 'classified'")
                event.classification_error_kind = KIND_PERMANENT
                event.classification_status = "failed"
        classification_failed = [e for e in processed_events if e.classification_status == "failed"]
        processed_events = [e for e in processed_events if e.classification_status == "classified"]
        failed_raw_event_ids = set()
        for event in classification_failed:
            raw_event_id = getattr(event, "raw_event_id", None)
            if not raw_event_id:
                continue
            failed_raw_event_ids.add(raw_event_id)
            self._queue_failure(raw_event_id, "classification",
                                event.classification_error or "classification failed",
                                event.classification_error_kind or KIND_PERMANENT)
        if classification_failed:
            logger.warning(
                "[WARNING] %d event(s) could not be classified; they stay in the retry queue",
                len(classification_failed),
            )

        # Step 5: Store enriched events
        logger.info(f"[PIPELINE] Starting to store {len(processed_events)} enriched events for {label}")
        enriched_event_ids = []
        enriched_event_map = {}
        processed_raw_event_ids = set()
        successful_raw_event_ids = set()
        failed_stores = 0

        for i, event in enumerate(processed_events):
            if i % 10 == 0:  # Log every 10th event
                logger.info(f"[PIPELINE] Storing enriched event {i+1}/{len(processed_events)} for {label}")
            raw_event_id = getattr(event, "raw_event_id", None)
            if raw_event_id:
                processed_raw_event_ids.add(raw_event_id)
            enriched_event_id = await self._store_enriched_event(event, raw_event_id)
            if enriched_event_id:
                enriched_event_ids.append(enriched_event_id)
                enriched_event_map[enriched_event_id] = event
                if raw_event_id:
                    successful_raw_event_ids.add(raw_event_id)
                    await self._log_processing_success(raw_event_id, 'enrichment', {
                        'enriched_event_id': enriched_event_id,
                        'confidence_score': event.confidence.overall if hasattr(event, 'confidence') and event.confidence else 0.7,
                        'evidence_basis': event.evidence_basis,
                    })
            else:
                failed_stores += 1
                if raw_event_id:
                    failed_raw_event_ids.add(raw_event_id)
                    self._queue_failure(raw_event_id, "store_enriched", "enriched event could not be stored",
                                        KIND_PERMANENT)

        logger.info(f"[PIPELINE] Completed storing {len(enriched_event_ids)} enriched events for {label}")

        # Check if too many events failed to store
        if failed_stores > 0:
            failure_rate = failed_stores / len(processed_events)
            logger.warning(f"[WARNING] {failed_stores}/{len(processed_events)} events failed to store ({failure_rate:.1%} failure rate)")
            if failure_rate > 0.5:  # More than 50% failure rate
                logger.error(f"[CRITICAL] High failure rate ({failure_rate:.1%}) - stopping pipeline")
                raise RuntimeError(f"Too many events failed to store: {failed_stores}/{len(processed_events)} ({failure_rate:.1%})")

        # Records the ledger rejected were already marked processed with the
        # stage and reason; re-marking them here would overwrite that with a
        # generic message.  Failed and held records stay unprocessed.
        ledger_rejected_ids = {d.raw_event_id for d in ledger.rejected()}
        filtered_raw_event_ids = (
            set(raw_event_ids) - processed_raw_event_ids - ledger_rejected_ids - failed_raw_event_ids - held_ids
        )
        raw_events_to_mark = successful_raw_event_ids | filtered_raw_event_ids

        logger.info(f"[PIPELINE] Marking {len(raw_events_to_mark)} raw events as processed for {label}")
        for raw_event_id in sorted(raw_events_to_mark):
            error_message = None
            if raw_event_id in filtered_raw_event_ids:
                error_message = "Filtered out during processing"
            self.db.mark_raw_event_processed(raw_event_id, error_message=error_message)
        completed_ids = raw_events_to_mark | (ledger_rejected_ids & set(raw_event_ids))
        self.retry_queue.record_success(completed_ids)

        pending = failed_raw_event_ids | held_ids
        if pending:
            logger.warning(
                "[RETRY] %d record(s) for %s remain unprocessed and will be retried from the queue",
                len(pending), label,
            )
        return {
            "processed_events": processed_events,
            "enriched_event_ids": enriched_event_ids,
            "enriched_event_map": enriched_event_map,
            "completed_ids": completed_ids,
            "pending_ids": pending,
        }

    def _queue_failure(self, raw_event_id: str, stage: str, error: str, kind: str) -> str:
        outcome = self.retry_queue.record_failure(raw_event_id, stage, error, kind)
        key = 'records_failed_temporary' if kind == KIND_TEMPORARY else 'records_failed_permanent'
        self.stats[key] += 1
        if outcome == "quarantined":
            self.stats['records_quarantined'] += 1
        return outcome

    def _apply_page_gate(self, raw_event_ids: List[str], ledger: AcceptanceLedger, label: str) -> set:
        """Judge each new, unrejected record's stored page; reject where calibrated.

        Returns the ids *held* this run (not judged because the TypeSafe budget
        ran out): they stay pending, are not classified, and are retried.

        Raises:
            PageGateUnavailable: Jev is not configured or kept failing.
        """
        rows = []
        with self.db._lock:
            cursor = self.db._conn.cursor()
            for raw_event_id in raw_event_ids:
                if ledger.is_rejected(raw_event_id):
                    continue
                cursor.execute(
                    "SELECT raw_event_id, source_type, raw_title, raw_description, raw_content, source_url "
                    "FROM RawEvents WHERE raw_event_id = ? AND COALESCE(is_processed, 0) = 0",
                    (raw_event_id,))
                row = cursor.fetchone()
                if row:
                    rows.append(dict(row))
        held: set = set()
        decisions: List[GateDecision] = []
        for index, row in enumerate(rows):
            try:
                decision = self.page_gate.decide(row)
            except PageGateUnavailable as exc:
                if is_budget_refusal(exc):
                    # The documented budget behaviour: stop asking, leave the
                    # rest for the next run (the run ends degraded).
                    for remaining in rows[index:]:
                        held.add(remaining["raw_event_id"])
                        self._queue_failure(remaining["raw_event_id"], STAGE_PAGE_GATE, str(exc), KIND_TEMPORARY)
                    logger.warning("[PAGE_GATE] TypeSafe budget exhausted: %d record(s) held for the next run",
                                   len(rows) - index)
                    break
                self._queue_failure(row["raw_event_id"], STAGE_PAGE_GATE, str(exc), KIND_TEMPORARY)
                persist_rejections(self.db, ledger)
                logger.error("[PAGE_GATE] Jev unavailable - stopping discovery; %s", exc)
                raise
            decisions.append(decision)
            if decision.dropped:
                ledger.reject(decision.raw_event_id, STAGE_PAGE_GATE, decision.reason)
            self.db.log_processing_attempt(
                decision.raw_event_id, STAGE_PAGE_GATE,
                "rejected" if decision.dropped else "would_drop" if decision.below_cutoff
                else "kept" if decision.judged else "not_judged",
                result_data={"probability": decision.probability, "cutoff": self.page_gate.cutoff,
                             "enforced": self.page_gate.is_enforcing(decision.source_type),
                             "reason": decision.reason},
            )
        if decisions:
            dropped = sum(d.dropped for d in decisions)
            would = sum(d.below_cutoff and not d.dropped for d in decisions)
            logger.info("[PAGE_GATE] %s: judged %d, not judgeable %d, dropped %d, would drop (report-only) %d",
                        label, sum(d.judged for d in decisions), sum(not d.judged for d in decisions), dropped, would)
        return held

    def report_incomplete_months(self) -> List[Dict[str, Any]]:
        """Log every source-month that still has records pending; return them."""
        incomplete = self.retry_queue.incomplete_months()
        for row in incomplete:
            logger.warning(
                "[RETRY] %s %d-%02d incomplete: %d of %d record(s) pending (%d failing)",
                row["source_type"], row["discovery_year"], row["discovery_month"], row["pending"], row["stored"],
                row["failing"],
            )
        if self.stats['records_quarantined']:
            logger.error("[RETRY] %d record(s) quarantined this run after repeated permanent failures; "
                         "see python scripts/discovery_queue.py status", self.stats['records_quarantined'])
        return incomplete

    async def _process_persisted_records(
        self,
        collector: Any,
        events: List[Any],
        ledger: AcceptanceLedger,
    ) -> List[Any]:
        """Classify and extract entities from the *persisted* record.

        Scraping writes the fetched article to ``RawEvents.raw_content``, but
        the events discovery holds in memory still carry only the search
        snippet.  Handing those to the classifier made it judge a Google
        snippet that quoted a site menu as if it were the article.  Here every
        event is re-read from the database after scraping: rejected records
        are dropped and the rest carry the stored article (or an explicit
        marker that there is none).
        """
        records = prepare_records_for_processing(self.db, events, ledger)
        # Persist before the (slow, fallible) LLM calls so a crash mid-batch
        # does not forget what was already rejected.
        persist_rejections(self.db, ledger)
        logger.info(
            "[EVIDENCE] %d of %d records continue; basis: %s",
            len(records), len(events),
            ", ".join(f"{basis}={sum(1 for r in records if r.evidence_basis == basis)}"
                      for basis in ("article", "snippet_only", "none")),
        )
        if not records:
            return []
        return await collector._process_events(records)

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

        cyberdaily_config = DataSourceConfig(
            enabled='CyberDaily' in source_types,
            rate_limit=30,
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
            oaic_config=oaic_config,
            cyberdaily_config=cyberdaily_config,
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
                    'event_type': _enum_value_or_str(getattr(event, 'event_type', None)),
                    'severity': _enum_value_or_str(getattr(event, 'severity', None)),
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
        """Store a processed event as an enriched event without fallback dates."""
        try:
            logger.debug(f"[ENRICHED] Starting to store event: {getattr(event, 'title', 'NO_TITLE')[:50]}...")

            if not raw_event_id:
                logger.warning(f"[WARNING] No raw_event_id provided for event: {event.title[:50]}...")
                return None
            if getattr(event, "classification_status", None) != "classified":
                # Storage marks every row a specific incident, so an event the
                # classifier did not judge must never reach it.
                logger.error("[ENRICHED] Refusing to store unclassified event '%s' (status %r)",
                             event.title[:50], getattr(event, "classification_status", None))
                return None

            logger.debug(f"[ENRICHED] Processing event data...")
            # Handle event_date without fallback dates
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

            if not event_date:
                logger.debug(f"[WARNING] No event date or publication date for '{event.title[:50]}...'")

            # Convert date to ISO format string for SQLite
            event_date_str = event_date.isoformat() if hasattr(event_date, 'isoformat') else None

            # Acceptance gate: an event cannot be in the future or absurdly old.
            # A bad date is dropped (stored as NULL), not "corrected" by guessing.
            if event_date_str:
                valid_date, date_problem = validate_iso_date(event_date_str)
                if date_problem:
                    logger.warning(f"[GATE] Dropping event date for '{event.title[:50]}': {date_problem}")
                    event_date_str = None

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
                'event_type': _enum_value_or_str(getattr(event, 'event_type', None)),
                'severity': _enum_value_or_str(getattr(event, 'severity', None)),
                'event_date': event_date_str,
                'records_affected': self._valid_count_or_none(
                    event.financial_impact.customers_affected if event.financial_impact else None, title
                ),
                'is_australian_event': is_australian,  # NOT NULL - guaranteed boolean
                'is_specific_event': is_specific,  # NOT NULL - guaranteed boolean
                'confidence_score': event.confidence.overall if hasattr(event, 'confidence') and event.confidence else 0.7,
                'australian_relevance_score': float(getattr(event, 'australian_relevance', 0.0)) if isinstance(getattr(event, 'australian_relevance', None), (int, float)) and not isinstance(getattr(event, 'australian_relevance', None), bool) else (1.0 if getattr(event, 'australian_relevance', False) else 0.0),
                'status': 'Active',
                'entities': entities  # Pass entities to be created in one transaction
            }

            # Validate and coerce all data types before database insertion
            entities_backup = enriched_data.pop('entities', [])
            primary_org = getattr(event, 'primary_entity', None)
            primary_org_name = getattr(primary_org, 'name', None) if primary_org else None
            enriched_data = validate_enrichment_data_for_storage(
                enriched_data, event_title=title,
                org_name=primary_org_name,
                perplexity_api_key=os.getenv('PERPLEXITY_API_KEY'),
            )
            enriched_data['entities'] = entities_backup

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

    @staticmethod
    def _valid_count_or_none(value: Any, title: str) -> Optional[int]:
        """Acceptance gate for a count: non-negative whole number, else NULL."""
        if value is None:
            return None
        count, problem = validate_count(value)
        if problem:
            logger.warning(f"[GATE] Dropping records_affected for '{title[:50]}': {problem}")
        return count

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

                    # Validate all values before INSERT to prevent type errors
                    title_val = safe_str(event.title, default='Untitled Event', field_name='title')
                    desc_val = safe_str(event.description, field_name='description')
                    summary_val = safe_str(getattr(event, 'summary', None), field_name='summary')
                    event_type_val = safe_str(event.event_type, field_name='event_type') if hasattr(event, 'event_type') else None
                    severity_val = safe_str(event.severity, field_name='severity') if hasattr(event, 'severity') else None

                    records_affected_val = None
                    if event.financial_impact:
                        records_affected_val = safe_int(
                            event.financial_impact.customers_affected,
                            field_name='records_affected'
                        )

                    is_australian_val = safe_bool(
                        getattr(event, 'australian_relevance', False),
                        field_name='is_australian_event'
                    )

                    confidence_val = 0.7
                    if hasattr(event, 'confidence') and event.confidence:
                        confidence_val = safe_float(
                            event.confidence.overall, default=0.7,
                            field_name='confidence_score'
                        )
                    confidence_val = max(0.0, min(1.0, confidence_val or 0.7))

                    aus_relevance_raw = getattr(event, 'australian_relevance', None)
                    if isinstance(aus_relevance_raw, (int, float)):
                        aus_relevance_val = safe_float(aus_relevance_raw, default=0.0, field_name='australian_relevance_score')
                    else:
                        aus_relevance_val = 1.0 if safe_bool(aus_relevance_raw, field_name='australian_relevance') else 0.0
                    aus_relevance_val = max(0.0, min(1.0, aus_relevance_val or 0.0))

                    total_sources_val = safe_int(
                        len(event.data_sources) if hasattr(event, 'data_sources') and event.data_sources else 1,
                        default=1, field_name='total_data_sources'
                    )
                    contributing_raw_val = safe_int(
                        event.contributing_raw_events, default=1,
                        field_name='contributing_raw_events'
                    )
                    contributing_enriched_val = safe_int(
                        event.contributing_enriched_events, default=1,
                        field_name='contributing_enriched_events'
                    )
                    similarity_val = safe_float(
                        getattr(event, 'similarity_score', 1.0), default=1.0,
                        field_name='similarity_score'
                    )
                    similarity_val = max(0.0, min(1.0, similarity_val or 1.0))

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
                        title_val,
                        desc_val,
                        summary_val,
                        event_type_val,
                        severity_val,
                        event_date_value,
                        records_affected_val,
                        is_australian_val,
                        True,
                        confidence_val,
                        aus_relevance_val,
                        total_sources_val,
                        contributing_raw_val,
                        contributing_enriched_val,
                        similarity_val,
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
            logger.warning(f"[TRACEBACK] {traceback.format_exc()}")

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
            # A stored alias ("NAB", "Medicare") files under its organisation.
            from cyber_data_collector.dedup.entity_resolution import canonical_entity_name
            entity_name = canonical_entity_name(self.db._conn, entity_name.strip())

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
                except Exception as exc:
                    logger.debug("Skipping invalid event_date value %s: %s", event.get('event_date'), exc)
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
            logger.warning(
                f"[CROSS-MONTH] {merges_performed} cross-month merges detected but "
                f"database updates are not yet implemented. Duplicates may remain."
            )

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
                },
                'CyberDaily': {
                    'source_name': 'Cyber Daily',
                    'source_type': 'News Publication',
                    'base_url': 'https://www.cyberdaily.au',
                    'api_endpoint': 'https://www.cyberdaily.au/security?format=feed&type=rss',
                    'description': 'Australian cyber security news (Momentum Media), section RSS feeds',
                    'data_frequency': 'Daily',
                    'coverage_scope': 'Australia',
                    'reliability_score': 0.75
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
                elif 'Cyber Daily' in source.source_type:
                    return 'CyberDaily'

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

    def _store_publication_date(self, raw_event_id: str, scrape_meta: Dict[str, Any]) -> None:
        """Keep the page's own publication date (its metadata) unless the source already gave one."""
        from cyber_data_collector.processing.publication_dates import SOURCE_PAGE_META, store

        with self.db._lock:
            store(self.db._conn, raw_event_id, scrape_meta['publication_date'],
                  f"{SOURCE_PAGE_META}: {scrape_meta.get('publication_date_how')}")
            self.db._conn.commit()

    def _fallback_event_date(self, event, month_start: Optional[datetime] = None) -> Optional[datetime.date]:
        """Return a fallback event date when no event date is available."""
        if hasattr(event, 'data_sources') and event.data_sources:
            for source in event.data_sources:
                fallback_date = getattr(source, 'search_start_date', None)
                if fallback_date:
                    if isinstance(fallback_date, datetime):
                        return fallback_date.date()
                    try:
                        return datetime.strptime(str(fallback_date), '%Y-%m-%d').date()
                    except Exception:
                        continue

        return None

    # =========================================================================
    # SCRAPING PHASE
    # =========================================================================

    async def _scrape_raw_events_for_month(
        self, raw_event_ids: List[str], ledger: Optional[AcceptanceLedger] = None
    ) -> int:
        """
        Scrape URLs for specific raw events from the current month.

        A record on a blocked domain is rejected in *ledger* so later stages do
        not process it.  Nothing else is dropped before scraping: judging a
        page needs its text, which the page gate does afterwards.
        Returns the number of events successfully scraped.
        """
        if ledger is None:
            ledger = AcceptanceLedger()
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

        # PRE-SCRAPE: only social/profile/marketplace domains are skipped.  No
        # record is dropped on its title or URL alone (the retired Random
        # Forest pre-filter did that and lost real incidents).
        scraped_count = 0
        failed_scrapes = []
        events_after_prefilter = []
        prefilter_blocked = 0

        for event in events_with_urls:
            url = event.get('source_url') or ''
            title = event.get('raw_title') or ''

            # Hard-skip blocked domains (LinkedIn, Facebook, X, YouTube, etc.)
            if is_blocked_domain(url):
                prefilter_blocked += 1
                ledger.reject(event['raw_event_id'], STAGE_DOMAIN_BLOCKLIST,
                              'social/profile/marketplace domain - not a news article')
                failed_scrapes.append({
                    'title': title[:50] if title else 'Unknown',
                    'url': url or 'No URL',
                    'reason': 'Skipped (social/profile/marketplace domain - not a news article)',
                    'perplexity_attempted': False,
                    'perplexity_succeeded': False,
                })
                continue

            events_after_prefilter.append(event)

        if prefilter_blocked:
            logger.info(f"[SCRAPING] Skipped {prefilter_blocked} blocked-domain URLs")

        if not events_after_prefilter:
            logger.info("[SCRAPING] All candidate URLs dropped by pre-filter; nothing to scrape")
            # Still report failures so caller has accurate counts
            self._log_scrape_failures(failed_scrapes, total=len(events_with_urls))
            return 0

        events_with_urls = events_after_prefilter
        logger.info(f"[SCRAPING] Found {len(events_with_urls)} events to scrape for this month")

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

            async for result in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Scraping URLs", smoothing=0):
                try:
                    scrape_result = await asyncio.wait_for(result, timeout=120.0)  # 2 minute timeout
                    if scrape_result['success']:
                        # The page gate judges the stored page after scraping
                        # (``_apply_page_gate``), not here.
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
        self._log_scrape_failures(failed_scrapes)
        return scraped_count

    def _log_scrape_failures(self, failed_scrapes: List[Dict[str, Any]], total: Optional[int] = None) -> None:
        """Emit the standard failure summary used by the scraping phase."""
        if not failed_scrapes:
            logger.info("[SCRAPING] All events scraped successfully!")
            return

        logger.info(f"[SCRAPING] Failed to scrape {len(failed_scrapes)} events:")

        perplexity_attempted_count = sum(1 for f in failed_scrapes if f.get('perplexity_attempted', False))
        perplexity_succeeded_count = sum(1 for f in failed_scrapes if f.get('perplexity_succeeded', False))
        if perplexity_attempted_count > 0:
            logger.info(
                f"[SCRAPING] Perplexity fallback: {perplexity_attempted_count} attempted, "
                f"{perplexity_succeeded_count} succeeded"
            )

        for i, failed in enumerate(failed_scrapes[:10], 1):
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

        # Pre-scrape: skip blocked domains only (nothing is dropped on title or
        # URL alone).  Rejections are persisted: otherwise the later
        # enrich_events() phase, which reads every unprocessed raw event, would
        # classify them anyway.
        scraped_count = 0
        failed_scrapes = []
        events_after_prefilter = []
        prefilter_blocked = 0
        ledger = AcceptanceLedger()

        for event in events_with_urls:
            url = event.get('source_url') or ''
            title = event.get('raw_title') or ''

            if is_blocked_domain(url):
                prefilter_blocked += 1
                ledger.reject(event['raw_event_id'], STAGE_DOMAIN_BLOCKLIST,
                              'social/profile/marketplace domain - not a news article')
                failed_scrapes.append({
                    'title': title[:50] if title else 'Unknown',
                    'url': url or 'No URL',
                    'reason': 'Skipped (social/profile/marketplace domain - not a news article)',
                })
                continue

            events_after_prefilter.append(event)

        persist_rejections(self.db, ledger)

        if prefilter_blocked:
            logger.info(f"[SCRAPING] Skipped {prefilter_blocked} blocked-domain URLs")

        events_with_urls = events_after_prefilter
        logger.info(f"[SCRAPING] Found {len(events_with_urls)} events with URLs to scrape")

        async with PlaywrightScraper() as scraper:
            # Create tasks with event info attached
            tasks = []
            for event in events_with_urls:
                task = asyncio.create_task(self._scrape_single_event_with_info(event, scraper))
                tasks.append(task)
            
            with tqdm(total=len(tasks), desc="Web Scraping URLs", unit="URL", smoothing=0) as pbar:
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

            content, scrape_meta = await scraper.get_page_text(url, event_date=event_date, return_metadata=True)
            processing_time_ms = int((time.time() - start_time) * 1000)

            perplexity_attempted = scrape_meta.get('perplexity_attempted', False)
            perplexity_succeeded = scrape_meta.get('perplexity_succeeded', False)
            if scrape_meta.get('publication_date'):
                self._store_publication_date(event['raw_event_id'], scrape_meta)
            if content and len(content.strip()) > 50:
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
                if not perplexity_attempted:  # Perplexity hasn't been tried yet
                    logger.debug(f"[PERPLEXITY] Content too short ({len(content) if content else 0} chars), trying Perplexity fallback for: {url}")
                    perplexity_attempted = True
                    perplexity_content = await scraper._perplexity_fallback(url, event_date)
                    if perplexity_content and len(perplexity_content.strip()) > 50:
                        # Perplexity found better content
                        perplexity_succeeded = True
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
                if not perplexity_attempted:
                    perplexity_attempted = True
                perplexity_succeeded = False
                
                # Why the page could not be read, with its type: a 429 or a
                # timeout is not a short page (the Cyber Daily re-fetch
                # repairs either on a later run).
                scrape_error = scrape_meta.get('error') or (
                    f"page text too short ({len(content.strip()) if content else 0} chars)")
                self.db.log_processing_attempt(
                    event['raw_event_id'], 'url_scraping', 'failed',
                    result_data={'url': url, 'scrape_error': scrape_error,
                                 'error_type': (INCOMPLETE if content and content.strip()
                                                else error_type_from_text(scrape_error))},
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
            perplexity_attempted = False
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
                if cursor.rowcount != 1:
                    raise RuntimeError(f"raw event {raw_event_id} not found; scraped content not stored")
                self.db._conn.commit()
            except Exception as e:
                # Raised, not swallowed: the caller reports the scrape as failed
                # instead of logging success for content that was never stored.
                logger.error(f"Failed to update raw event content: {e}")
                self.db._conn.rollback()
                raise

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
        with tqdm(total=len(events_to_process), desc="LLM Analysis", unit="event", smoothing=0) as pbar:
            for event in events_to_process:
                try:
                    success = await self._enrich_single_event(event)
                    if success:
                        enriched_count += 1
                        pbar.set_postfix({"enriched": f"{enriched_count}/{len(events_to_process)}"})
                    else:
                        pbar.set_postfix({"skipped": "Not Australian/specific"})
                except PageGateUnavailable:
                    raise  # no silent fallback: the run stops, records stay unprocessed
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
            # The page gate first: a page that does not itself report an
            # incident (where that is calibrated) is rejected before any LLM call.
            decision = self.page_gate.decide(raw_event)
            if decision.dropped:
                ledger = AcceptanceLedger()
                ledger.reject(raw_event_id, STAGE_PAGE_GATE, decision.reason)
                persist_rejections(self.db, ledger)
                return False

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

            enriched_event_data = self._prepare_enriched_event_data(raw_event, enriched_data)
            self.db.create_enriched_event(raw_event_id, enriched_event_data)
            self.db.mark_raw_event_processed(raw_event_id)
            return True

        except PageGateUnavailable:
            raise
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
        evidence = RecordEvidence.from_row(raw_event)

        # Add title
        if raw_event.get('raw_title'):
            content_parts.append(f"Title: {raw_event['raw_title']}")

        # Say what the text is.  Presenting a search snippet as "Description"
        # beside no article invited the model to treat it as the article.
        if evidence.has_article:
            if raw_event.get('raw_description'):
                content_parts.append(f"Discovery summary: {raw_event['raw_description']}")
            content_parts.append(f"Article Content: {evidence.article_text}")
        elif evidence.snippet:
            content_parts.append(
                f"Search-result snippet (NO article text was retrieved - {evidence.reason}): {evidence.snippet}"
            )

        # Add metadata context
        if raw_event.get('source_metadata'):
            try:
                metadata = json.loads(raw_event['source_metadata'])
                if metadata:
                    content_parts.append(
                        f"Source Information: {safe_json_dumps(metadata, 'source metadata', indent=2)}"
                    )
            except (json.JSONDecodeError, TypeError) as exc:
                logger.debug("Failed to parse source metadata for content assembly: %s", exc)

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
            except (json.JSONDecodeError, TypeError) as exc:
                logger.debug("Failed to parse source metadata for enriched event: %s", exc)

        # Determine title (prefer enriched, fall back to raw)
        title = raw_event.get('raw_title', 'Untitled Event')
        if llm_data.primary_entity and raw_event['source_type'] == 'GDELT':
            # For GDELT events, create better title using primary entity
            title = f"Cyber incident affecting {llm_data.primary_entity}"

        # Use LLM-extracted event date first, then raw event date (no fallback dates)
        event_date = llm_data.event_date if llm_data.event_date else raw_event.get('event_date')
        logger.debug(
            "[ENRICHMENT] Using %s event date: %s",
            "LLM-extracted" if llm_data.event_date else "raw",
            event_date,
        )

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
        """Print the page gate's statistics and the retry queue for the current run."""
        print("\n" + "="*60)
        print("[FILTERING] PAGE GATE (Jev: does the page itself report an incident?)")
        print("="*60)
        for line in self.page_gate.summary_lines():
            print(f"[PAGE_GATE] {line}")
        print(f"[RETRY] Records due from the retry queue: {self.stats['retry_due']}, "
              f"finished: {self.stats['retry_completed']}")
        print(f"[RETRY] Failures this run: {self.stats['records_failed_temporary']} temporary, "
              f"{self.stats['records_failed_permanent']} permanent, "
              f"{self.stats['records_quarantined']} quarantined")
        try:
            for row in self.retry_queue.incomplete_months():
                print(f"[RETRY] Incomplete: {row['source_type']} {row['discovery_year']}-{row['discovery_month']:02d} "
                      f"- {row['pending']} of {row['stored']} pending")
        except Exception as exc:  # reporting must not hide the run's real outcome
            print(f"[WARNING] Could not read source-month completion: {exc}")
        print("="*60)

    def close(self):
        """Clean up resources"""
        for line in self.page_gate.summary_lines():
            logger.info(f"[PAGE_GATE] {line}")
        self.page_gate.close()
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
    parser.add_argument('--source', choices=['Perplexity', 'GoogleSearch', 'WebberInsurance', 'OAIC', 'CyberDaily'],
                        help='Run only specific source (can be used multiple times)', action='append')
    parser.add_argument('--max-events', type=int, default=0,
                        help='Maximum events to process per phase (0 or negative for unlimited; default: unlimited for scraping/enrichment, 1000 for discovery)')
    parser.add_argument('--days', type=int, default=7,
                        help='Number of days to look back for discovery (default: 7)')
    parser.add_argument('--db-path', default=None,
                        help='Path to database file (defaults to DATABASE_URL/.env setting)')
    parser.add_argument('--no-australian-filter', action='store_true',
                        help='Disable Australian-only filtering for enrichment')

    args = parser.parse_args()
    from cyber_data_collector.utils.config_manager import resolve_db_path
    args.db_path = resolve_db_path(args.db_path)

    # If no phase specified, run all phases
    if not any([args.discover, args.scrape, args.enrich]):
        args.discover = args.scrape = args.enrich = True

    # Load environment
    if Path(".env").exists():
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
            print("   python pipeline.py init")
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
