from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List

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
        """Collect cyber events from this data source."""

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

