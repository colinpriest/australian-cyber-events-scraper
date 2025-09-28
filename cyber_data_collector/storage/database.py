from __future__ import annotations

import logging
from typing import Iterable, List, Optional

from cyber_data_collector.models.events import CyberEvent


class DatabaseManager:
    """Database manager responsible for event persistence."""

    def __init__(self, database_url: Optional[str]) -> None:
        self.database_url = database_url
        self.logger = logging.getLogger(self.__class__.__name__)

    def save_events(self, events: Iterable[CyberEvent]) -> None:
        """Persist events to the configured database.

        This method is a placeholder and should be extended to integrate with the project's database layer.
        """

        count = 0
        for count, _ in enumerate(events, start=1):
            pass
        self.logger.info("Pretending to save %s events to %s", count, self.database_url)

    def load_recent_events(self, limit: int = 100) -> List[CyberEvent]:
        """Load recent events from the database (not yet implemented)."""

        self.logger.warning("load_recent_events is not implemented; returning empty list")
        return []

    def get_event_by_id(self, event_id: str) -> Optional[CyberEvent]:
        """Retrieve an event by its ID (not yet implemented)."""

        self.logger.warning("get_event_by_id is not implemented; returning None")
        return None

