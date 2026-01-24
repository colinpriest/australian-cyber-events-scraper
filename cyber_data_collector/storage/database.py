from __future__ import annotations

import logging
from typing import Iterable, List, Optional
from urllib.parse import urlparse

from cyber_data_collector.models.events import CyberEvent
from cyber_event_data_v2 import CyberEventDataV2


class DatabaseManager:
    """Database manager responsible for event persistence."""

    def __init__(self, database_url: Optional[str]) -> None:
        self.database_url = database_url
        self.logger = logging.getLogger(self.__class__.__name__)
        self.database_path = self._resolve_database_path(database_url)
        self._db = CyberEventDataV2(self.database_path)

    def save_events(self, events: Iterable[CyberEvent]) -> None:
        """Persist events to the configured database."""

        saved = 0
        skipped = 0

        for event in events:
            source = event.data_sources[0] if event.data_sources else None
            source_type = source.source_type if source else "Collector"
            source_url = source.url if source else None
            raw_title = event.title

            if source_url:
                existing = self._db.find_existing_raw_event(source_type, source_url, raw_title)
                if existing:
                    skipped += 1
                    continue

            raw_data = {
                "source_event_id": event.event_id,
                "title": event.title,
                "description": event.description,
                "content": source.content_snippet if source and source.content_snippet else event.description,
                "event_date": event.event_date.isoformat() if event.event_date else None,
                "source_url": source_url,
                "metadata": event.model_dump(mode="json"),
            }

            self._db.add_raw_event(source_type, raw_data)
            saved += 1

        self.logger.info(
            "Saved %s events to database (skipped %s duplicates) at %s",
            saved,
            skipped,
            self.database_path,
        )

    def load_recent_events(self, limit: int = 100) -> List[CyberEvent]:
        """Load recent events from the database (not yet implemented)."""

        self.logger.warning("load_recent_events is not implemented for the V2 schema; returning empty list")
        return []

    def get_event_by_id(self, event_id: str) -> Optional[CyberEvent]:
        """Retrieve an event by its ID (not yet implemented)."""

        self.logger.warning("get_event_by_id is not implemented; returning None")
        return None

    @staticmethod
    def _resolve_database_path(database_url: Optional[str]) -> str:
        if not database_url:
            return "instance/cyber_events.db"

        if database_url.startswith("sqlite:///"):
            return database_url.replace("sqlite:///", "", 1)

        if database_url.startswith("sqlite://"):
            return database_url.replace("sqlite://", "", 1)

        parsed = urlparse(database_url)
        if parsed.scheme:
            return "instance/cyber_events.db"

        return database_url

















