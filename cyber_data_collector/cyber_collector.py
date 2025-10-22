from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Dict, List, Optional

from tqdm import tqdm

from cyber_data_collector.datasources import (
    DataSource,
    GDELTDataSource,
    GoogleSearchDataSource,
    OAICDataSource,
    PerplexityDataSource,
    WebberInsuranceDataSource,
)
from cyber_data_collector.models.config import CollectionConfig
from cyber_data_collector.models.events import CyberEvent
from cyber_data_collector.processing.deduplication import DeduplicationEngine
from cyber_data_collector.processing.entity_extractor import EntityExtractor
from cyber_data_collector.processing.llm_classifier import LLMClassifier
from cyber_data_collector.storage import CacheManager, DatabaseManager
from cyber_data_collector.utils import ConfigManager, RateLimiter, ThreadManager


class CyberDataCollector:
    """Main class for collecting Australian cyber events from multiple sources."""

    def __init__(self, config: CollectionConfig, env_path: str = ".env") -> None:
        self.config = config
        self.env_config = ConfigManager(env_path).load()
        self.logger = self._setup_logging()

        self.rate_limiter = RateLimiter()
        self.thread_manager = ThreadManager(max_threads=config.max_threads)
        self.llm_classifier = LLMClassifier(self.env_config.get("OPENAI_API_KEY"))
        self.deduplication_engine = DeduplicationEngine()
        self.entity_extractor = EntityExtractor(self.llm_classifier)

        self.data_sources: Dict[str, DataSource] = {}
        self._initialize_data_sources()

        self.database_manager = DatabaseManager(self.env_config.get("DATABASE_URL"))
        self.cache_manager = CacheManager()

        self.collected_events: List[CyberEvent] = []
        self._lock = threading.Lock()

    def _setup_logging(self) -> logging.Logger:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            handlers=[
                logging.FileHandler("cyber_collector.log"),
                logging.StreamHandler(),
            ],
        )
        return logging.getLogger(self.__class__.__name__)

    def _initialize_data_sources(self) -> None:
        if self.config.gdelt_config.enabled:
            self.rate_limiter.set_limit("gdelt", per_minute=self.config.gdelt_config.rate_limit)
            self.data_sources["gdelt"] = GDELTDataSource(
                self.config.gdelt_config,
                self.rate_limiter,
                self.env_config,
            )

        if self.config.perplexity_config.enabled:
            self.rate_limiter.set_limit("perplexity", per_minute=self.config.perplexity_config.rate_limit)
            self.data_sources["perplexity"] = PerplexityDataSource(
                self.config.perplexity_config,
                self.rate_limiter,
                self.env_config,
            )

        if self.config.google_search_config.enabled:
            self.rate_limiter.set_limit("google_search", per_minute=self.config.google_search_config.rate_limit)
            self.data_sources["google_search"] = GoogleSearchDataSource(
                self.config.google_search_config,
                self.rate_limiter,
                self.env_config,
            )

        if self.config.webber_config.enabled:
            self.rate_limiter.set_limit("webber", per_minute=self.config.webber_config.rate_limit)
            self.data_sources["webber"] = WebberInsuranceDataSource(
                self.config.webber_config,
                self.rate_limiter,
                self.env_config,
            )

        if self.config.oaic_config.enabled:
            self.rate_limiter.set_limit("oaic", per_minute=self.config.oaic_config.rate_limit)
            self.data_sources["oaic"] = OAICDataSource(
                self.config.oaic_config,
                self.rate_limiter,
                self.env_config,
            )

    async def collect_all_events(self) -> List[CyberEvent]:
        self.logger.info("Starting cyber event collection")

        valid_sources: Dict[str, DataSource] = {}
        for name, source in self.data_sources.items():
            if source.validate_config():
                valid_sources[name] = source
            else:
                self.logger.error("Configuration validation failed for %s", name)

        collection_tasks = [self._collect_from_source(name, source) for name, source in valid_sources.items()]

        all_events: List[CyberEvent] = []
        for task in asyncio.as_completed(collection_tasks):
            try:
                source_events = await task
                all_events.extend(source_events)
            except Exception as exc:
                self.logger.error("Error collecting events: %s", exc)
                if "Reauthentication is needed" in str(exc):
                    raise RuntimeError(
                        "BigQuery authorization expired. Run `gcloud auth application-default login` "
                        "or `python setup_bigquery_auth.py` to renew credentials before rerunning."
                    ) from exc

        self.logger.info("Collected %s raw events", len(all_events))

        processed_events = await self._process_events(all_events)

        if self.config.enable_deduplication:
            processed_events = await self.deduplication_engine.deduplicate_events(processed_events)
            self.logger.info("Deduplicated to %s unique events", len(processed_events))

        high_confidence_events = [
            event for event in processed_events if event.confidence.overall >= self.config.confidence_threshold
        ]

        self.logger.info("Retained %s high-confidence events", len(high_confidence_events))

        self.collected_events = high_confidence_events
        return high_confidence_events

    async def _collect_from_source(self, source_name: str, source: DataSource) -> List[CyberEvent]:
        try:
            self.logger.info("Collecting events from %s", source_name)
            events = await source.collect_events(self.config.date_range)
            self.logger.info("Collected %s events from %s", len(events), source_name)
            return events
        except Exception as exc:
            self.logger.error("Failed to collect from %s: %s", source_name, exc)
            return []

    async def _process_events(self, events: List[CyberEvent]) -> List[CyberEvent]:
        self.logger.info("Processing and enhancing events")

        batch_size = self.config.batch_size
        processed_events: List[CyberEvent] = []

        total_batches = (len(events) + batch_size - 1) // batch_size

        # Progress bar for overall batch processing
        with tqdm(total=total_batches, desc="Processing batches", unit="batch") as pbar:
            for index in range(0, len(events), batch_size):
                batch = events[index : index + batch_size]

                # Pre-process events to fix GDELT descriptions
                batch = self._preprocess_events(batch)

                # Entity extraction with progress
                batch = await self.entity_extractor.extract_entities(batch)

                # LLM classification with progress
                batch = await self.llm_classifier.classify_events(batch)

                processed_events.extend(batch)
                batch_num = index // batch_size + 1
                pbar.set_postfix({"batch": f"{batch_num}/{total_batches}", "events": len(batch)})
                pbar.update(1)

        return processed_events

    def _preprocess_events(self, events: List[CyberEvent]) -> List[CyberEvent]:
        """Preprocess events to fix issues like generic GDELT descriptions."""
        processed_events = []

        for event in events:
            # Create a copy to avoid modifying the original
            event_copy = event.copy(deep=True)

            # Fix GDELT events with generic descriptions
            if any(source.source_type == "GDELT BigQuery" for source in event.data_sources):
                # Check if this has the generic GDELT description
                if ("Cyber event detected via GDELT BigQuery" in event.description):
                    # Use scraped content if available
                    for source in event.data_sources:
                        if hasattr(source, 'content_snippet') and source.content_snippet:
                            # Use first 500 chars of scraped content as description
                            event_copy.description = source.content_snippet[:500] + "..."
                            self.logger.debug(f"Replaced GDELT description with scraped content for: {event.title[:50]}...")
                            break
                    else:
                        # No scraped content, use title as description
                        if event.title and event.title != event.description:
                            event_copy.description = event.title
                            self.logger.debug(f"Replaced GDELT description with title for: {event.title[:50]}...")

            processed_events.append(event_copy)

        return processed_events

    def save_events(self, events: Optional[List[CyberEvent]] = None) -> bool:
        events_to_save = events or self.collected_events
        try:
            self.database_manager.save_events(events_to_save)
            self.logger.info("Saved %s events to database", len(events_to_save))
            return True
        except Exception as exc:
            self.logger.error("Failed to save events: %s", exc)
            return False

    def export_events(self, filename: str, format: str = "json") -> bool:
        try:
            if format.lower() == "json":
                with open(filename, "w", encoding="utf-8") as file:
                    json.dump([event.model_dump() for event in self.collected_events], file, indent=2, default=str)
            elif format.lower() == "csv":
                import pandas as pd

                df = pd.DataFrame([event.model_dump() for event in self.collected_events])
                df.to_csv(filename, index=False)
            elif format.lower() == "excel":
                import pandas as pd

                df = pd.DataFrame([event.model_dump() for event in self.collected_events])
                df.to_excel(filename, index=False)
            else:
                raise ValueError(f"Unsupported format: {format}")

            self.logger.info("Exported %s events to %s", len(self.collected_events), filename)
            return True
        except Exception as exc:
            self.logger.error("Failed to export events: %s", exc)
            return False

    def get_collection_stats(self) -> Dict[str, Any]:
        if not self.collected_events:
            return {"total_events": 0}

        stats: Dict[str, Any] = {
            "total_events": len(self.collected_events),
            "events_by_type": {},
            "events_by_severity": {},
            "events_by_source": {},
            "australian_events": 0,
            "average_confidence": 0.0,
            "date_range": {"earliest": None, "latest": None},
        }

        total_confidence = 0.0
        earliest_date: Optional[str] = None
        latest_date: Optional[str] = None

        for event in self.collected_events:
            event_type = event.event_type.value
            stats["events_by_type"][event_type] = stats["events_by_type"].get(event_type, 0) + 1

            severity = event.severity.value
            stats["events_by_severity"][severity] = stats["events_by_severity"].get(severity, 0) + 1

            for source in event.data_sources:
                source_type = source.source_type
                stats["events_by_source"][source_type] = stats["events_by_source"].get(source_type, 0) + 1

            if event.australian_relevance:
                stats["australian_events"] += 1

            total_confidence += event.confidence.overall

            if event.event_date:
                event_date_iso = event.event_date.isoformat()
                if not earliest_date or event_date_iso < earliest_date:
                    earliest_date = event_date_iso
                if not latest_date or event_date_iso > latest_date:
                    latest_date = event_date_iso

        stats["average_confidence"] = total_confidence / len(self.collected_events)
        stats["date_range"]["earliest"] = earliest_date
        stats["date_range"]["latest"] = latest_date

        return stats
