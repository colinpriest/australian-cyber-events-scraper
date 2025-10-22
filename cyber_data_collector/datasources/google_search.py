from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Dict, List, Any

import requests

from cyber_data_collector.datasources.base import DataSource
from cyber_data_collector.models.config import DataSourceConfig, DateRange
from cyber_data_collector.models.events import ConfidenceScore, CyberEvent, CyberEventType, EventSeverity, EventSource
from cyber_data_collector.utils import RateLimiter


class GoogleSearchDataSource(DataSource):
    """Google Custom Search API data source."""

    def __init__(self, config: DataSourceConfig, rate_limiter: RateLimiter, env_config: Dict[str, str | None]):
        super().__init__(config, rate_limiter)
        self.api_key = env_config.get("GOOGLE_CUSTOMSEARCH_API_KEY")
        self.cx_key = env_config.get("GOOGLE_CUSTOMSEARCH_CX_KEY")

    def validate_config(self) -> bool:
        if not self.api_key:
            self.logger.error("GOOGLE_CUSTOMSEARCH_API_KEY not configured")
            return False
        if not self.cx_key:
            self.logger.error("GOOGLE_CUSTOMSEARCH_CX_KEY not configured")
            return False
        return True

    async def collect_events(self, date_range: DateRange) -> List[CyberEvent]:
        queries = self._generate_google_queries(date_range)
        all_events: List[CyberEvent] = []

        for query in queries:
            try:
                await self.rate_limiter.wait("google_search")
                results = await self._execute_google_search(query, date_range)
                events = self._process_search_results(results)
                all_events.extend(events)
            except Exception as exc:
                self.logger.error("Google search failed for query '%s': %s", query, exc)

        return all_events

    def _generate_google_queries(self, date_range: DateRange) -> List[str]:
        return [
            'australian cybersecurity ("data breach" OR "ransomware" OR "cyber attack")',
            'australia "data breach" notification privacy commissioner',
            'australian company "cyber incident" OR "security breach"',
            'australia government "cyber attack" OR "security incident"',
        ]

    async def _execute_google_search(self, query: str, date_range: DateRange) -> List[Dict]:
        url = "https://www.googleapis.com/customsearch/v1"

        date_filter = f"date:r:{date_range.start_date.strftime('%Y%m%d')}:"
        if date_range.end_date:
            date_filter += date_range.end_date.strftime("%Y%m%d")
        else:
            date_filter += datetime.now().strftime("%Y%m%d")

        all_results: List[Dict] = []
        for page in range(5):
            params = {
                "key": self.api_key,
                "cx": self.cx_key,
                "q": query,
                "num": 10,
                "sort": date_filter,
                "start": 1 + (page * 10),
            }

            try:
                response = requests.get(url, params=params, timeout=self.config.timeout)
                if response.status_code == 429:
                    self.logger.warning("Rate limited by Google API, waiting...")
                    await asyncio.sleep(60)
                    response = requests.get(url, params=params, timeout=self.config.timeout)

                response.raise_for_status()
                data = response.json()
                results = data.get("items", [])
                if not results:
                    break

                all_results.extend(results)
                await asyncio.sleep(1)
            except Exception as exc:
                self.logger.error("Google API request failed: %s", exc)
                break

        return all_results

    def _process_search_results(self, results: List[Dict]) -> List[CyberEvent]:
        events: List[CyberEvent] = []
        for item in results:
            try:
                title = item.get("title", "Google Search Result")
                snippet = item.get("snippet", "")
                link = item.get("link")

                data_source = EventSource(
                    source_id=f"google_{hash(link)}",
                    source_type="Google Search",
                    url=link,
                    title=title,
                    content_snippet=snippet,
                    credibility_score=0.5,
                    relevance_score=0.6,
                )

                confidence = ConfidenceScore(
                    overall=0.5,
                    source_reliability=0.5,
                    data_completeness=0.4,
                    temporal_accuracy=0.4,
                    geographic_accuracy=0.5,
                )

                is_australian = (
                    "australia" in title.lower()
                    or "australia" in snippet.lower()
                    or ".com.au" in link
                    or ".net.au" in link
                    or ".org.au" in link
                    or ".gov.au" in link
                    or ".edu.au" in link
                )

                events.append(
                    CyberEvent(
                        title=title,
                        description=snippet,
                        event_type=CyberEventType.OTHER,
                        severity=EventSeverity.MEDIUM,
                        australian_relevance=is_australian,
                        data_sources=[data_source],
                        confidence=confidence,
                    )
                )
            except Exception as exc:
                self.logger.debug("Failed to process Google search result: %s", exc)
        return events

    def get_source_info(self) -> Dict[str, Any]:
        return {
            "name": "Google Custom Search API",
            "description": "Web search results for Australian cyber events",
            "update_frequency": "Real-time",
            "coverage": "Global web content",
            "data_types": ["Web pages", "News articles", "Reports"],
        }









