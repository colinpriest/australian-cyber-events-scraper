from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

try:
    from google.cloud import bigquery
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials
    import json
    BIGQUERY_AVAILABLE = True
except ImportError:
    bigquery = None
    service_account = None
    Credentials = None
    json = None
    BIGQUERY_AVAILABLE = False

from cyber_data_collector.datasources.base import DataSource
from cyber_data_collector.models.config import DataSourceConfig, DateRange
from cyber_data_collector.models.events import (
    AffectedEntity,
    ConfidenceScore,
    CyberEvent,
    CyberEventType,
    EntityType,
    EventSeverity,
    EventSource,
)
from cyber_data_collector.utils import RateLimiter
from cyber_data_collector.filtering import ProgressiveFilterSystem


class GDELTDataSource(DataSource):
    """GDELT Project data source backed exclusively by BigQuery."""

    def __init__(self, config: DataSourceConfig, rate_limiter: RateLimiter, env_config: Dict[str, Optional[str]]):
        super().__init__(config, rate_limiter)
        self.max_records = int(self.config.custom_config.get("max_records", 1000))

        self.bigquery_client = None
        self.google_cloud_project = env_config.get("GOOGLE_CLOUD_PROJECT")
        self.google_credentials_path = env_config.get("GOOGLE_APPLICATION_CREDENTIALS")

        # Initialize progressive filtering system
        self.filter_system = ProgressiveFilterSystem()

    def validate_config(self) -> bool:
        """Validate configuration and initialize BigQuery client."""
        if not BIGQUERY_AVAILABLE:
            self.logger.error("google-cloud-bigquery library is not installed")
            return False

        if not self.google_cloud_project:
            self.logger.error("GOOGLE_CLOUD_PROJECT environment variable not set")
            return False

        try:
            credentials = None

            oauth_token_file = "bigquery_token.json"
            if os.path.exists(oauth_token_file) and json:
                try:
                    with open(oauth_token_file, "r", encoding="utf-8") as file:
                        token_data = json.load(file)
                    credentials = Credentials(
                        token=token_data.get("token"),
                        refresh_token=token_data.get("refresh_token"),
                        token_uri=token_data.get("token_uri"),
                        client_id=token_data.get("client_id"),
                        client_secret=token_data.get("client_secret"),
                    )
                    self.logger.info("Using OAuth credentials for BigQuery")
                except Exception as exc:
                    self.logger.warning("Failed to load OAuth credentials: %s", exc)

            if credentials is None and self.google_credentials_path and os.path.exists(self.google_credentials_path):
                credentials = service_account.Credentials.from_service_account_file(self.google_credentials_path)
                self.logger.info("Using service account credentials for BigQuery")

            if credentials:
                self.bigquery_client = bigquery.Client(
                    project=self.google_cloud_project,
                    credentials=credentials,
                )
            else:
                self.bigquery_client = bigquery.Client(project=self.google_cloud_project)
                self.logger.info("Using default application credentials for BigQuery")

            self.logger.info("BigQuery client initialized successfully for GDELT")
            return True

        except Exception as exc:
            message = str(exc)
            if "Reauthentication is needed" in message or "invalid_grant" in message:
                self.logger.error(
                    "Failed to initialize BigQuery client: %s", message
                )
                raise RuntimeError(
                    "BigQuery authorization expired. Run `gcloud auth application-default login` "
                    "or `python setup_bigquery_auth.py` to renew credentials before rerunning."
                ) from exc

            self.logger.error("Failed to initialize BigQuery client: %s", exc)
            return False

    async def collect_events(self, date_range: DateRange) -> List[CyberEvent]:
        """Collect events strictly from BigQuery."""
        if not self.bigquery_client:
            raise RuntimeError("BigQuery client not initialized for GDELT")

        return await self._collect_from_bigquery(date_range)

    async def _collect_from_bigquery(self, date_range: DateRange) -> List[CyberEvent]:
        """Collect events from GDELT BigQuery dataset."""
        if not self.bigquery_client:
            raise RuntimeError("BigQuery client not available")

        # Build BigQuery query for Australian cyber events using parameterized queries
        start_date = date_range.start_date.strftime("%Y%m%d") + "000000"
        end_date = (date_range.end_date or datetime.utcnow()).strftime("%Y%m%d") + "235959"

        query = """
        SELECT
            GLOBALEVENTID,
            DATEADDED,
            Actor1Name,
            Actor2Name,
            Actor1CountryCode,
            Actor2CountryCode,
            EventBaseCode,
            EventCode,
            GoldsteinScale,
            NumSources,
            SourceURL,
            ActionGeo_CountryCode,
            ActionGeo_FullName,
            ActionGeo_Lat,
            ActionGeo_Long
        FROM `gdelt-bq.gdeltv2.events`
        WHERE CAST(DATEADDED AS STRING) >= @start_date
          AND CAST(DATEADDED AS STRING) <= @end_date
          AND (
            ActionGeo_CountryCode = 'AS' OR
            Actor1CountryCode = 'AS' OR
            Actor2CountryCode = 'AS'
          )
          AND (
            (LOWER(Actor1Name) LIKE '%data breach%' OR LOWER(Actor2Name) LIKE '%data breach%') OR
            (LOWER(Actor1Name) LIKE '%cyber breach%' OR LOWER(Actor2Name) LIKE '%cyber breach%') OR
            (LOWER(Actor1Name) LIKE '%security breach%' OR LOWER(Actor2Name) LIKE '%security breach%') OR
            (LOWER(Actor1Name) LIKE '%ransomware%' OR LOWER(Actor2Name) LIKE '%ransomware%') OR
            (LOWER(Actor1Name) LIKE '%malware%' OR LOWER(Actor2Name) LIKE '%malware%') OR
            (LOWER(Actor1Name) LIKE '%cyber attack%' OR LOWER(Actor2Name) LIKE '%cyber attack%') OR
            (LOWER(Actor1Name) LIKE '%cyberattack%' OR LOWER(Actor2Name) LIKE '%cyberattack%') OR
            (LOWER(Actor1Name) LIKE '%phishing%' OR LOWER(Actor2Name) LIKE '%phishing%') OR
            (LOWER(Actor1Name) LIKE '%ddos%' OR LOWER(Actor2Name) LIKE '%ddos%') OR
            (LOWER(Actor1Name) LIKE '%credential%' OR LOWER(Actor2Name) LIKE '%credential%') OR
            (LOWER(Actor1Name) LIKE '%hack%' OR LOWER(Actor2Name) LIKE '%hack%') OR
            (LOWER(Actor1Name) LIKE '%vulnerability%' OR LOWER(Actor2Name) LIKE '%vulnerability%') OR
            (LOWER(Actor1Name) LIKE '%exploit%' OR LOWER(Actor2Name) LIKE '%exploit%') OR
            EventCode LIKE '141%' OR
            EventCode LIKE '172%' OR
            EventCode LIKE '210%'
          )
          -- Exclude obvious non-cyber events
          AND NOT (
            LOWER(Actor1Name) LIKE '%firework%' OR LOWER(Actor2Name) LIKE '%firework%' OR
            LOWER(Actor1Name) LIKE '%celebration%' OR LOWER(Actor2Name) LIKE '%celebration%' OR
            LOWER(Actor1Name) LIKE '%new year%' OR LOWER(Actor2Name) LIKE '%new year%' OR
            LOWER(Actor1Name) LIKE '%holiday%' OR LOWER(Actor2Name) LIKE '%holiday%' OR
            LOWER(Actor1Name) LIKE '%festival%' OR LOWER(Actor2Name) LIKE '%festival%' OR
            LOWER(Actor1Name) LIKE '%concert%' OR LOWER(Actor2Name) LIKE '%concert%' OR
            LOWER(Actor1Name) LIKE '%sports%' OR LOWER(Actor2Name) LIKE '%sports%' OR
            LOWER(Actor1Name) LIKE '%election%' OR LOWER(Actor2Name) LIKE '%election%'
          )
          AND NumSources >= 2  -- At least 2 sources for credibility
        ORDER BY DATEADDED DESC
        LIMIT @max_records
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("start_date", "STRING", start_date),
                bigquery.ScalarQueryParameter("end_date", "STRING", end_date),
                bigquery.ScalarQueryParameter("max_records", "INT64", self.max_records),
            ]
        )

        # Execute query
        query_job = self.bigquery_client.query(query, job_config=job_config)
        results = query_job.result()

        events: List[CyberEvent] = []
        for row in results:
            event = self._convert_bigquery_event(dict(row))
            if event:
                events.append(event)

        return events

    def filter_scraped_content(self, title: str, content: str, url: str) -> bool:
        """
        Post-scraping filter using the new progressive filtering system.

        Args:
            title: The title of the scraped content
            content: The full text content of the scraped page
            url: The URL that was scraped

        Returns:
            True if the content appears to be about cyber security, False otherwise
        """
        try:
            # Use the content stage of the progressive filter
            result = self.filter_system.should_process_content(
                title=title or "",
                content=content or "",
                url=url or "",
                metadata={'source': 'GDELT'}
            )

            # Log the filtering decision with reasoning
            if result.is_cyber_relevant:
                self.logger.debug(f"Accepted GDELT event (score: {result.confidence_score:.2f}): {title[:50]}...")
                for reason in result.reasoning:
                    self.logger.debug(f"  - {reason}")
            else:
                self.logger.debug(f"Rejected GDELT event (score: {result.confidence_score:.2f}): {title[:50]}...")
                for reason in result.reasoning:
                    self.logger.debug(f"  - {reason}")

            return result.is_cyber_relevant

        except Exception as e:
            self.logger.warning(f"Error in progressive filter, falling back to permissive: {e}")
            # If filtering fails, err on the side of caution and keep the event
            return True

    def filter_at_discovery(self, title: str, description: str = "", url: str = "") -> bool:
        """
        Discovery-stage filtering using the progressive filtering system.

        This is a new method that applies Stage 1 filtering during event discovery
        to avoid scraping obviously non-cyber content.

        Args:
            title: Event title
            description: Event description (if available)
            url: Source URL

        Returns:
            True if the event should be collected and scraped, False otherwise
        """
        try:
            # Use the discovery stage of the progressive filter
            result = self.filter_system.should_discover_event(
                title=title or "",
                description=description or "",
                url=url or "",
                metadata={'source': 'GDELT'}
            )

            # Log the filtering decision
            if result.is_cyber_relevant:
                self.logger.debug(f"Discovery stage PASS (score: {result.confidence_score:.2f}): {title[:50]}...")
            else:
                self.logger.debug(f"Discovery stage REJECT (score: {result.confidence_score:.2f}): {title[:50]}...")

            return result.is_cyber_relevant

        except Exception as e:
            self.logger.warning(f"Error in discovery filter, being permissive: {e}")
            # If filtering fails, be permissive at discovery stage
            return True

    def get_filter_statistics(self) -> Dict[str, Any]:
        """
        Get filtering performance statistics.

        Returns:
            Dictionary with filtering statistics
        """
        return self.filter_system.get_filtering_statistics()

    def log_filter_summary(self):
        """
        Log a summary of filtering performance.
        """
        self.filter_system.log_filtering_summary()

    def _convert_bigquery_event(self, row: Dict[str, Any]) -> Optional[CyberEvent]:
        """Convert BigQuery row to CyberEvent."""
        try:
            # Extract basic event information for filtering
            title = str(row.get("GLOBALEVENTID", ""))  # Use event ID as title for now
            source_url = row.get("SourceURL", "")
            actor1_name = str(row.get("Actor1Name") or "")
            actor2_name = str(row.get("Actor2Name") or "")

            # Create a meaningful description from available data
            description = f"Event involving {actor1_name}" if actor1_name else ""
            if actor2_name:
                description += f" and {actor2_name}"

            # Apply discovery-stage filtering using the progressive filter system
            if not self.filter_at_discovery(
                title=description or title,  # Use description as title if available
                description="",  # No additional description at this stage
                url=source_url
            ):
                return None



            event_date = self._parse_bigquery_date(row.get("DATEADDED"))

            data_source = EventSource(
                source_id=f"gdelt_bq_{row.get('GLOBALEVENTID')}",
                source_type="GDELT BigQuery",
                url=row.get("SourceURL"),
                publication_date=event_date,
                credibility_score=self._credibility_from_sources(row.get("NumSources")),
                relevance_score=0.8,
            )

            entities: List[AffectedEntity] = []
            for actor_name in [row.get("Actor1Name"), row.get("Actor2Name")]:
                if actor_name and "australia" in actor_name.lower():
                    entities.append(
                        AffectedEntity(
                            name=actor_name,
                            entity_type=EntityType.OTHER,
                            australian_entity=True,
                            confidence_score=0.8,
                        )
                    )

            confidence = ConfidenceScore(
                overall=0.8,
                source_reliability=self._credibility_from_sources(row.get("NumSources")),
                data_completeness=0.8,
                temporal_accuracy=0.9,
                geographic_accuracy=0.9,
            )

            return CyberEvent(
                external_ids={"gdelt_id": str(row.get("GLOBALEVENTID"))},
                title=row.get("Actor1Name", "GDELT Cyber Event"),
                description=f"Cyber event detected via GDELT BigQuery (Code: {row.get('EventCode')})",
                event_type=self._map_cameo_to_event_type(row.get("EventCode", "")),
                severity=EventSeverity.MEDIUM,
                event_date=event_date,
                affected_entities=entities,
                location=row.get("ActionGeo_FullName"),
                coordinates=self._parse_coordinates_bigquery(row),
                australian_relevance=self._is_australian_event_bigquery(row),
                data_sources=[data_source],
                confidence=confidence,
            )
        except Exception as exc:
            self.logger.debug("Failed to convert BigQuery event: %s", exc)
            return None

    def _parse_bigquery_date(self, value: Any) -> Optional[datetime]:
        """Parse BigQuery date format."""
        if not value:
            return None
        try:
            if isinstance(value, str) and len(value) == 14:
                return datetime.strptime(value, "%Y%m%d%H%M%S")
            elif isinstance(value, int):
                return datetime.strptime(str(value), "%Y%m%d%H%M%S")
        except ValueError as exc:
            self.logger.debug("Failed to parse BigQuery date %s: %s", value, exc)
        return None

    def _parse_coordinates_bigquery(self, row: Dict[str, Any]) -> Optional[tuple[float, float]]:
        """Parse coordinates from BigQuery row."""
        lat = row.get("ActionGeo_Lat")
        lng = row.get("ActionGeo_Long")
        try:
            if lat is not None and lng is not None:
                return float(lat), float(lng)
        except (TypeError, ValueError) as exc:
            self.logger.debug("Failed to parse BigQuery coordinates %s/%s: %s", lat, lng, exc)
            return None
        return None

    def _is_australian_event_bigquery(self, row: Dict[str, Any]) -> bool:
        """Check if BigQuery event is Australian-related."""
        return (
            row.get("ActionGeo_CountryCode") == "AS"
            or row.get("Actor1CountryCode") == "AS"
            or row.get("Actor2CountryCode") == "AS"
            or ("australia" in str(row.get("Actor1Name") or "").lower())
            or ("australia" in str(row.get("Actor2Name") or "").lower())
        )

    def _credibility_from_sources(self, sources: Any) -> float:
        """Calculate credibility score from number of sources."""
        try:
            count = float(sources) if sources else 0.0
        except (TypeError, ValueError) as exc:
            self.logger.debug("Failed to parse credibility sources %s: %s", sources, exc)
            count = 0.0
        return max(0.3, min(count * 0.2, 1.0))

    def _map_cameo_to_event_type(self, event_code: str) -> CyberEventType:
        """Map CAMEO event codes to cyber event types."""
        if not event_code:
            return CyberEventType.OTHER

        if event_code.startswith("172"):
            return CyberEventType.STATE_SPONSORED_ATTACK
        elif event_code.startswith("210"):
            return CyberEventType.INFRASTRUCTURE_ATTACK
        elif event_code.startswith("141"):
            return CyberEventType.DATA_BREACH
        else:
            return CyberEventType.OTHER

    def _map_title_to_event_type(self, title: str) -> CyberEventType:
        """Map article title to cyber event type."""
        title_lower = title.lower()

        if any(word in title_lower for word in ["ransomware", "ransom"]):
            return CyberEventType.RANSOMWARE
        elif any(word in title_lower for word in ["data breach", "breach", "leaked"]):
            return CyberEventType.DATA_BREACH
        elif any(word in title_lower for word in ["ddos", "denial of service"]):
            return CyberEventType.DENIAL_OF_SERVICE
        elif any(word in title_lower for word in ["phishing", "email scam"]):
            return CyberEventType.PHISHING
        elif any(word in title_lower for word in ["malware", "virus", "trojan"]):
            return CyberEventType.MALWARE
        elif any(word in title_lower for word in ["infrastructure", "critical"]):
            return CyberEventType.INFRASTRUCTURE_ATTACK
        elif any(word in title_lower for word in ["state", "nation", "government"]):
            return CyberEventType.STATE_SPONSORED_ATTACK
        else:
            return CyberEventType.OTHER

    def _map_title_to_severity(self, title: str) -> EventSeverity:
        """Map article title to event severity."""
        title_lower = title.lower()

        if any(word in title_lower for word in ["critical", "major", "massive", "widespread"]):
            return EventSeverity.CRITICAL
        elif any(word in title_lower for word in ["significant", "serious", "major"]):
            return EventSeverity.HIGH
        elif any(word in title_lower for word in ["minor", "small", "limited"]):
            return EventSeverity.LOW
        else:
            return EventSeverity.MEDIUM

    def get_source_info(self) -> Dict[str, Any]:
        access_methods = ["BigQuery"] if self.bigquery_client else []

        return {
            "name": "GDELT Project Multi-Access",
            "description": "Global Database of Events, Language, and Tone with multiple access methods",
            "update_frequency": "15 minutes",
            "coverage": "Global events with Australian cyber security focus",
            "data_types": ["Events", "News Articles", "Full Text Search"],
            "access_methods": access_methods,
            "api_version": "2.0",
        }
