from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
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
        # Expand search window to 3 months back to catch late-reported events
        # Events may be reported months after they occurred
        from dateutil.relativedelta import relativedelta
        expanded_start = date_range.start_date - relativedelta(months=2)

        date_str = f"after:{expanded_start.strftime('%m/%d/%Y')}"
        if date_range.end_date:
            date_str += f" before:{date_range.end_date.strftime('%m/%d/%Y')}"

        self.logger.info(f"Perplexity searching with expanded 3-month window: {expanded_start.strftime('%Y-%m-%d')} to {date_range.end_date.strftime('%Y-%m-%d') if date_range.end_date else 'now'}")

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

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query},
        ]
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self.openai_client.chat.completions.create(
                model="sonar-pro",
                messages=messages,
                temperature=0.1,
                max_tokens=4000,
            )
        )

        # Track token usage
        from cyber_data_collector.utils.token_tracker import tracker
        if response.usage:
            tracker.record(
                "sonar-pro", response.usage.prompt_tokens,
                response.usage.completion_tokens, context="perplexity_discovery",
            )

        content = response.choices[0].message.content
        if not content:
            return PerplexitySearchResults(events=[])

        # Tier 1: Strip markdown code blocks (```json ... ```) and parse
        cleaned = self._strip_markdown_json(content)
        parsed = self._try_parse_json(cleaned)
        if parsed is not None:
            return self._dict_to_search_results(parsed)

        # Tier 2: Try to fix truncated JSON (missing closing brackets)
        self.logger.warning(
            "Failed to parse Perplexity response, attempting JSON repair. "
            "Raw content (first 500 chars): %s",
            content[:500],
        )
        fixed = self._try_fix_truncated_json(cleaned)
        if fixed is not None:
            self.logger.info("Successfully repaired truncated JSON")
            return self._dict_to_search_results(fixed)

        # Tier 3: Retry the query with strict no-markdown JSON instruction
        self.logger.info("Retrying Perplexity query with strict JSON-only instruction")
        await self.rate_limiter.wait("perplexity")
        retry_result = await self._retry_strict_json(query, date_range)
        if retry_result is not None:
            self.logger.info("Strict JSON retry succeeded")
            return retry_result

        # Tier 4: Extract partial events and ask Perplexity to complete them
        partial = self._extract_partial_events(content)
        if partial:
            self.logger.info(
                "Extracted %d partial events from malformed JSON, "
                "asking Perplexity to complete them",
                len(partial),
            )
            await self.rate_limiter.wait("perplexity")
            completion_result = await self._complete_partial_events(partial, date_range)
            if completion_result is not None:
                self.logger.info(
                    "Partial event completion succeeded with %d events",
                    len(completion_result.events),
                )
                return completion_result

        self.logger.warning(
            "All JSON recovery attempts failed for Perplexity response. "
            "Raw content (first 500 chars): %s",
            content[:500],
        )
        return PerplexitySearchResults(events=[])

    def _strip_markdown_json(self, content: str) -> str:
        """Strip markdown code block wrappers (```json ... ```) from content."""
        stripped = content.strip()
        if stripped.startswith("```"):
            # Try regex for properly closed code blocks
            match = re.search(r'```(?:json)?\s*\n?(.*?)```', stripped, re.DOTALL)
            if match:
                return match.group(1).strip()
            # No closing ``` (truncated response) — strip the opening line only
            lines = stripped.split("\n", 1)
            return lines[1].strip() if len(lines) > 1 else stripped
        return stripped

    def _try_parse_json(self, content: str) -> Optional[Dict[str, Any]]:
        """Try to parse JSON string, return None on failure."""
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
        return None

    def _try_fix_truncated_json(self, json_str: str) -> Optional[Dict[str, Any]]:
        """Attempt to fix truncated JSON by closing open structures.

        This handles cases where max_tokens cuts off the response mid-JSON,
        leaving unclosed brackets/braces.
        """
        # Strategy 1: Find last complete event object boundary and close
        last_event_end = json_str.rfind('},')
        if last_event_end > 0:
            truncated = json_str[:last_event_end + 1]
            for suffix in [']}', '\n]\n}']:
                result = self._try_parse_json(truncated + suffix)
                if result is not None and 'events' in result:
                    return result

        # Strategy 2: Find last } and try closing the outer structure
        last_brace = json_str.rfind('}')
        if last_brace > 0:
            truncated = json_str[:last_brace + 1]
            for suffix in [']}', '']:
                result = self._try_parse_json(truncated + suffix)
                if result is not None and 'events' in result:
                    return result

        # Strategy 3: Simple bracket counting — close all open brackets/braces
        cleaned = json_str.rstrip().rstrip(',')
        open_braces = cleaned.count('{') - cleaned.count('}')
        open_brackets = cleaned.count('[') - cleaned.count(']')
        if open_braces >= 0 and open_brackets >= 0:
            suffix = ']' * open_brackets + '}' * open_braces
            result = self._try_parse_json(cleaned + suffix)
            if result is not None:
                return result

        return None

    def _dict_to_search_results(self, data: Dict[str, Any]) -> PerplexitySearchResults:
        """Convert a parsed JSON dict to PerplexitySearchResults."""
        events = [
            PerplexityEvent(
                title=event.get("title", ""),
                description=event.get("description", ""),
                event_date=event.get("event_date"),
                entity_name=event.get("entity_name"),
                event_type=event.get("event_type"),
                impact_description=event.get("impact_description"),
                source_urls=event.get("source_urls", []),
            )
            for event in data.get("events", [])
            if event.get("title")  # skip entries without a title
        ]
        return PerplexitySearchResults(events=events)

    async def _retry_strict_json(
        self, query: str, date_range: DateRange
    ) -> Optional[PerplexitySearchResults]:
        """Retry the query with explicit instruction to return raw JSON only."""
        if not self.openai_client:
            return None

        system_prompt = (
            "You are a cybersecurity analyst. Extract detailed information about "
            "Australian cyber security incidents from search results.\n\n"
            "CRITICAL: Return ONLY raw JSON. Do NOT wrap it in markdown code blocks. "
            "Do NOT use ```json or ``` delimiters. Return the JSON object directly.\n\n"
            "Return this exact JSON structure:\n"
            '{"events": [{"title": "...", "description": "...", '
            '"event_date": "YYYY-MM-DD or null", "entity_name": "... or null", '
            '"event_type": "... or null", "impact_description": "... or null", '
            '"source_urls": ["url1"]}]}\n\n'
            "Only include real cyber security incidents related to Australia. "
            'If no relevant incidents are found, return: {"events": []}'
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query},
        ]

        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self.openai_client.chat.completions.create(
                    model="sonar-pro",
                    messages=messages,
                    temperature=0.0,
                    max_tokens=4000,
                ),
            )
            # Track token usage
            if response.usage:
                tracker.record(
                    "sonar-pro", response.usage.prompt_tokens,
                    response.usage.completion_tokens, context="perplexity_strict_json",
                )
            content = response.choices[0].message.content
            if not content:
                return None

            # Still strip markdown in case the model ignores the instruction
            cleaned = self._strip_markdown_json(content)
            parsed = self._try_parse_json(cleaned)
            if parsed is not None:
                return self._dict_to_search_results(parsed)

            # Try fixing truncated JSON from the retry too
            fixed = self._try_fix_truncated_json(cleaned)
            if fixed is not None:
                return self._dict_to_search_results(fixed)

        except Exception as exc:
            self.logger.warning("Strict JSON retry failed with error: %s", exc)

        return None

    def _extract_partial_events(self, content: str) -> List[Dict[str, str]]:
        """Extract whatever event data is recoverable from malformed JSON.

        Uses regex to find "title", "description", etc. field values even when
        the overall JSON structure is broken.
        """
        partial_events: List[Dict[str, str]] = []

        # Regex patterns for individual field values
        title_matches = re.findall(
            r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"', content
        )
        desc_matches = re.findall(
            r'"description"\s*:\s*"((?:[^"\\]|\\.)*)"', content
        )
        entity_matches = re.findall(
            r'"entity_name"\s*:\s*"((?:[^"\\]|\\.)*)"', content
        )
        date_matches = re.findall(
            r'"event_date"\s*:\s*"((?:[^"\\]|\\.)*)"', content
        )

        for i, title in enumerate(title_matches):
            if not title.strip():
                continue
            event: Dict[str, str] = {"title": title}
            if i < len(desc_matches):
                event["description"] = desc_matches[i]
            if i < len(entity_matches):
                event["entity_name"] = entity_matches[i]
            if i < len(date_matches):
                event["event_date"] = date_matches[i]
            partial_events.append(event)

        return partial_events

    async def _complete_partial_events(
        self,
        partial_events: List[Dict[str, str]],
        date_range: DateRange,
    ) -> Optional[PerplexitySearchResults]:
        """Send partial event data to Perplexity and ask it to complete the details."""
        if not self.openai_client or not partial_events:
            return None

        # Build a summary of the partial events we extracted
        event_summaries = []
        for i, event in enumerate(partial_events[:10], 1):  # cap at 10
            parts = [f"{i}. Title: {event.get('title', 'Unknown')}"]
            if event.get("description"):
                parts.append(f"   Description: {event['description'][:200]}")
            if event.get("entity_name"):
                parts.append(f"   Entity: {event['entity_name']}")
            if event.get("event_date"):
                parts.append(f"   Date: {event['event_date']}")
            event_summaries.append("\n".join(parts))

        events_text = "\n\n".join(event_summaries)

        system_prompt = (
            "You are a cybersecurity analyst. I have partial data about Australian "
            "cyber security incidents that needs to be completed.\n\n"
            "CRITICAL: Return ONLY raw JSON. Do NOT wrap it in markdown code blocks. "
            "Do NOT use ```json or ``` delimiters. Return the JSON object directly.\n\n"
            "Return this exact JSON structure:\n"
            '{"events": [{"title": "...", "description": "...", '
            '"event_date": "YYYY-MM-DD or null", "entity_name": "... or null", '
            '"event_type": "... or null", "impact_description": "... or null", '
            '"source_urls": ["url1"]}]}\n\n'
            "For each event below, verify the details and fill in any missing fields "
            "(event_type, impact_description, source_urls). "
            "Correct any obviously wrong information."
        )

        user_prompt = (
            f"Complete and verify these Australian cyber incidents:\n\n{events_text}"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self.openai_client.chat.completions.create(
                    model="sonar-pro",
                    messages=messages,
                    temperature=0.0,
                    max_tokens=4000,
                ),
            )
            # Track token usage
            if response.usage:
                tracker.record(
                    "sonar-pro", response.usage.prompt_tokens,
                    response.usage.completion_tokens, context="perplexity_partial_complete",
                )
            content = response.choices[0].message.content
            if not content:
                return None

            cleaned = self._strip_markdown_json(content)
            parsed = self._try_parse_json(cleaned)
            if parsed is not None:
                return self._dict_to_search_results(parsed)

            fixed = self._try_fix_truncated_json(cleaned)
            if fixed is not None:
                return self._dict_to_search_results(fixed)

        except Exception as exc:
            self.logger.warning("Partial event completion failed: %s", exc)

        return None

    def _convert_results_to_events(self, results: PerplexitySearchResults) -> List[CyberEvent]:
        events: List[CyberEvent] = []
        for item in results.events:
            try:
                data_sources = [
                    EventSource(
                        source_id=f"perplexity_{hashlib.md5(url.encode()).hexdigest()[:16]}",
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
