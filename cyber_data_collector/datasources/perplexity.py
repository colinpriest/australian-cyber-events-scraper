from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

try:  # pragma: no cover - optional dependency
    import instructor
    import openai
except ImportError:  # pragma: no cover
    instructor = None  # type: ignore
    openai = None  # type: ignore

from pydantic import BaseModel, Field

from cyber_data_collector.datasources.base import DataSource
from cyber_data_collector.models.config import DataSourceConfig, DateRange
from cyber_data_collector.models.events import (
    ConfidenceScore,
    CyberEvent,
    CyberEventType,
    EventSeverity,
    EventSource,
)
from cyber_data_collector.utils import RateLimiter


class PerplexityEvent(BaseModel):
    title: str
    description: str
    event_date: Optional[str]
    entity_name: Optional[str]
    event_type: Optional[str]
    impact_description: Optional[str]
    source_urls: List[str] = Field(default_factory=list)


class PerplexitySearchResults(BaseModel):
    events: List[PerplexityEvent] = Field(default_factory=list)


class PerplexityDataSource(DataSource):
    """Perplexity Search API data source."""

    def __init__(self, config: DataSourceConfig, rate_limiter: RateLimiter, env_config: Dict[str, Optional[str]]):
        super().__init__(config, rate_limiter)
        self.api_key = env_config.get("PERPLEXITY_API_KEY")
        self.client: Optional[instructor.Instructor] = None
        self.openai_client: Optional[openai.OpenAI] = None

        # Retry configuration
        self.max_retries = 3
        self.base_delay = 2.0  # seconds
        self.max_delay = 60.0  # seconds
        self.backoff_multiplier = 2.0

        # Track API health
        self.consecutive_failures = 0
        self.last_success_time = time.time()
        self.circuit_breaker_threshold = 5  # failures before backing off

    def validate_config(self) -> bool:
        if not self.api_key or not instructor or not openai:
            self.logger.error("PERPLEXITY_API_KEY not configured")
            return False

        try:
            # Use OpenAI client directly without instructor for Perplexity
            self.openai_client = openai.OpenAI(
                api_key=self.api_key,
                base_url="https://api.perplexity.ai"
            )
            return True
        except Exception as exc:
            self.logger.error("Failed to initialize Perplexity client: %s", exc)
            return False

    async def collect_events(self, date_range: DateRange) -> List[CyberEvent]:
        if not self.openai_client:
            self.logger.warning("Perplexity client not initialized")
            return []

        # Check circuit breaker
        if self._should_skip_due_to_circuit_breaker():
            self.logger.warning("Skipping Perplexity collection due to circuit breaker (too many recent failures)")
            return []

        queries = self._generate_search_queries(date_range)
        all_events: List[CyberEvent] = []
        successful_queries = 0
        failed_queries = 0

        for i, query in enumerate(queries):
            try:
                self.logger.debug(f"Processing Perplexity query {i+1}/{len(queries)}: {query[:50]}...")

                await self.rate_limiter.wait("perplexity")
                results = await self._search_with_retry(query, date_range)
                events = self._convert_results_to_events(results)
                all_events.extend(events)

                successful_queries += 1
                self._record_success()

                self.logger.debug(f"Successfully processed query {i+1}, found {len(events)} events")

            except Exception as exc:
                failed_queries += 1
                self._record_failure()

                # Log different types of errors appropriately
                if self._is_auth_error(exc):
                    self.logger.error(f"Perplexity authentication failed for query '{query[:50]}...': {exc}")
                    self.logger.error("Please check your PERPLEXITY_API_KEY configuration")
                elif self._is_rate_limit_error(exc):
                    self.logger.warning(f"Perplexity rate limit hit for query '{query[:50]}...': {exc}")
                    # Add extra delay for rate limiting
                    await asyncio.sleep(30)
                elif self._is_network_error(exc):
                    self.logger.warning(f"Perplexity network error for query '{query[:50]}...': {exc}")
                else:
                    self.logger.error(f"Perplexity search failed for query '{query[:50]}...': {exc}")

        self.logger.info(f"Perplexity collection completed: {successful_queries} successful, {failed_queries} failed queries")

        if successful_queries == 0 and failed_queries > 0:
            self.logger.warning("All Perplexity queries failed - check API key and network connectivity")

        return all_events

    def _generate_search_queries(self, date_range: DateRange) -> List[str]:
        date_str = f"after:{date_range.start_date.strftime('%m/%d/%Y')}"
        if date_range.end_date:
            date_str += f" before:{date_range.end_date.strftime('%m/%d/%Y')}"

        base_queries = [
            "Australian cyber attack {date_range} data breach security incident",
            "Australia cybersecurity breach {date_range} ransomware malware",
            "Australian company cyber incident {date_range} hacking data leak",
            "Australia government cyber attack {date_range} infrastructure security",
            "Australian bank financial cyber breach {date_range} fraud security",
            "Australia healthcare cyber attack {date_range} medical data breach",
            "Australian university cyber incident {date_range} education security",
        ]

        return [query.format(date_range=date_str) for query in base_queries]

    async def _search_with_retry(self, query: str, date_range: DateRange) -> PerplexitySearchResults:
        """Search with exponential backoff retry logic."""
        last_exception = None

        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0:
                    # Calculate delay with exponential backoff and jitter
                    delay = min(
                        self.base_delay * (self.backoff_multiplier ** (attempt - 1)),
                        self.max_delay
                    )
                    # Add jitter to prevent thundering herd
                    jitter = delay * 0.1 * (0.5 - asyncio.get_event_loop().time() % 1)
                    total_delay = delay + jitter

                    self.logger.info(f"Retrying Perplexity query in {total_delay:.1f}s (attempt {attempt + 1}/{self.max_retries + 1})")
                    await asyncio.sleep(total_delay)

                return await self._search(query, date_range)

            except Exception as exc:
                last_exception = exc

                # Don't retry on authentication errors - they won't resolve with retries
                if self._is_auth_error(exc):
                    self.logger.error(f"Authentication error on attempt {attempt + 1}, not retrying: {exc}")
                    raise exc

                # Don't retry on client errors (4xx except 429)
                if self._is_client_error(exc) and not self._is_rate_limit_error(exc):
                    self.logger.error(f"Client error on attempt {attempt + 1}, not retrying: {exc}")
                    raise exc

                if attempt < self.max_retries:
                    if self._is_rate_limit_error(exc):
                        self.logger.warning(f"Rate limit hit on attempt {attempt + 1}, will retry with longer delay")
                    elif self._is_server_error(exc):
                        self.logger.warning(f"Server error on attempt {attempt + 1}, will retry: {exc}")
                    elif self._is_network_error(exc):
                        self.logger.warning(f"Network error on attempt {attempt + 1}, will retry: {exc}")
                    else:
                        self.logger.warning(f"Unknown error on attempt {attempt + 1}, will retry: {exc}")

        # All retries exhausted
        self.logger.error(f"All {self.max_retries + 1} attempts failed for query: {query[:50]}...")
        raise last_exception

    async def _search(self, query: str, date_range: DateRange) -> PerplexitySearchResults:
        if not self.openai_client:
            raise RuntimeError("Perplexity client not configured")

        system_prompt = """You are a cybersecurity analyst. Extract detailed information about Australian cyber security incidents from search results.

        Return your response as a JSON object with this exact structure:
        {
            "events": [
                {
                    "title": "Event title here",
                    "description": "Event description here",
                    "event_date": "YYYY-MM-DD format or null",
                    "entity_name": "Affected entity name or null",
                    "event_type": "Type of cyber event or null",
                    "impact_description": "Description of impact or null",
                    "source_urls": ["url1", "url2"]
                }
            ]
        }

        Only include real cyber security incidents related to Australia. If no relevant incidents are found, return {"events": []}.
        """

        response = self.openai_client.chat.completions.create(
            model="sonar-pro",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ],
            temperature=0.1,
            max_tokens=2000,
        )

        # Parse the JSON response manually
        try:
            content = response.choices[0].message.content
            if not content:
                return PerplexitySearchResults(events=[])

            data = json.loads(content)
            events = [
                PerplexityEvent(
                    title=event.get("title", ""),
                    description=event.get("description", ""),
                    event_date=event.get("event_date"),
                    entity_name=event.get("entity_name"),
                    event_type=event.get("event_type"),
                    impact_description=event.get("impact_description"),
                    source_urls=event.get("source_urls", [])
                )
                for event in data.get("events", [])
            ]
            return PerplexitySearchResults(events=events)
        except (json.JSONDecodeError, KeyError) as exc:
            self.logger.warning("Failed to parse Perplexity response: %s", exc)
            return PerplexitySearchResults(events=[])

    def _convert_results_to_events(self, results: PerplexitySearchResults) -> List[CyberEvent]:
        events: List[CyberEvent] = []
        for item in results.events:
            try:
                data_sources = [
                    EventSource(
                        source_id=f"perplexity_{hash(url)}",
                        source_type="Perplexity Result",
                        url=url,
                        credibility_score=0.6,
                        relevance_score=0.7,
                    )
                    for url in item.source_urls
                ]

                confidence = ConfidenceScore(
                    overall=0.65,
                    source_reliability=0.6,
                    data_completeness=0.6,
                    temporal_accuracy=0.6,
                    geographic_accuracy=0.7,
                )

                events.append(
                    CyberEvent(
                        title=item.title,
                        description=item.description,
                        event_type=self._infer_event_type(item.event_type),
                        severity=EventSeverity.MEDIUM,
                        australian_relevance=True,
                        data_sources=data_sources,
                        confidence=confidence,
                    )
                )
            except Exception as exc:
                self.logger.debug("Failed to convert Perplexity event '%s': %s", item.title, exc)
        return events

    def _infer_event_type(self, event_type: Optional[str]) -> CyberEventType:
        if not event_type:
            return CyberEventType.OTHER

        value = event_type.lower()
        if "ransomware" in value:
            return CyberEventType.RANSOMWARE
        if "breach" in value or "data" in value:
            return CyberEventType.DATA_BREACH
        if "phish" in value:
            return CyberEventType.PHISHING
        return CyberEventType.OTHER

    def get_source_info(self) -> Dict[str, Any]:
        return {
            "name": "Perplexity Search API",
            "description": "AI-powered web search with real-time information",
            "update_frequency": "Real-time",
            "coverage": "Global web content with Australian focus",
            "data_types": ["Web search results", "News articles", "Reports"],
        }

    def _is_auth_error(self, exc: Exception) -> bool:
        """Check if the error is an authentication/authorization error."""
        error_str = str(exc).lower()
        return (
            "401" in error_str or
            "403" in error_str or
            "authorization" in error_str or
            "unauthorized" in error_str or
            "forbidden" in error_str or
            "invalid api key" in error_str or
            "api key" in error_str
        )

    def _is_rate_limit_error(self, exc: Exception) -> bool:
        """Check if the error is a rate limiting error."""
        error_str = str(exc).lower()
        return (
            "429" in error_str or
            "rate limit" in error_str or
            "too many requests" in error_str or
            "quota exceeded" in error_str
        )

    def _is_server_error(self, exc: Exception) -> bool:
        """Check if the error is a server-side error (5xx)."""
        error_str = str(exc).lower()
        return (
            "500" in error_str or
            "502" in error_str or
            "503" in error_str or
            "504" in error_str or
            "internal server error" in error_str or
            "bad gateway" in error_str or
            "service unavailable" in error_str or
            "gateway timeout" in error_str
        )

    def _is_client_error(self, exc: Exception) -> bool:
        """Check if the error is a client-side error (4xx)."""
        error_str = str(exc).lower()
        return (
            "400" in error_str or
            "401" in error_str or
            "403" in error_str or
            "404" in error_str or
            "405" in error_str or
            "408" in error_str or
            "409" in error_str or
            "410" in error_str or
            "422" in error_str or
            "429" in error_str or
            "bad request" in error_str or
            "not found" in error_str or
            "method not allowed" in error_str
        )

    def _is_network_error(self, exc: Exception) -> bool:
        """Check if the error is a network/connectivity error."""
        error_str = str(exc).lower()
        exc_type = type(exc).__name__.lower()
        return (
            "timeout" in error_str or
            "connection" in error_str or
            "network" in error_str or
            "dns" in error_str or
            "resolve" in error_str or
            exc_type in ["connectionerror", "timeout", "connecttimeout", "readtimeout"]
        )

    def _should_skip_due_to_circuit_breaker(self) -> bool:
        """Check if we should skip API calls due to circuit breaker."""
        if self.consecutive_failures >= self.circuit_breaker_threshold:
            # Back off for 5 minutes after circuit breaker threshold
            time_since_last_success = time.time() - self.last_success_time
            return time_since_last_success < 300  # 5 minutes
        return False

    def _record_success(self) -> None:
        """Record a successful API call."""
        self.consecutive_failures = 0
        self.last_success_time = time.time()

    def _record_failure(self) -> None:
        """Record a failed API call."""
        self.consecutive_failures += 1
