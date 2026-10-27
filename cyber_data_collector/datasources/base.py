from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from cyber_data_collector.models.config import DataSourceConfig, DateRange
from cyber_data_collector.models.events import CyberEvent


class DataSource(ABC):
    """Abstract base class for all data sources."""

    def __init__(self, config: DataSourceConfig, rate_limiter: "RateLimiter"):
        self.config = config
        self.rate_limiter = rate_limiter
        self.logger = logging.getLogger(self.__class__.__name__)

    @abstractmethod
    async def collect_events(self, date_range: DateRange) -> List[CyberEvent]:
        """Collect cyber events from this data source.

        A provider failure that loses results (a failed query, an exhausted
        quota, an unparseable answer, a page that could not be fetched) must be
        reported with :meth:`note_failure`, not only logged: the caller then
        treats the source-month as incomplete and searches it again, instead of
        recording an empty or partial result as the month's complete answer.
        """

    @property
    def collection_failures(self) -> List[str]:
        """Failures noted since this source was created (one collection run)."""
        return self.__dict__.setdefault("_collection_failures", [])

    def note_failure(self, what: str, exc: Optional[BaseException] = None) -> None:
        """Record a failure that lost results, and log it as an error."""
        message = f"{what}: {exc}" if exc is not None else what
        self.collection_failures.append(message[:500])
        logging.getLogger(self.__class__.__name__).error("Collection incomplete - %s", message)

    @abstractmethod
    def validate_config(self) -> bool:
        """Validate data source configuration."""

    @abstractmethod
    def get_source_info(self) -> Dict[str, Any]:
        """Get information about this data source."""


class EventProcessor(ABC):
    """Abstract base class for event processors."""

    @abstractmethod
    async def process_events(self, events: List[CyberEvent]) -> List[CyberEvent]:
        """Process and enhance cyber events."""

