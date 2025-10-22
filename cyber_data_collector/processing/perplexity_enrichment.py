"""
Perplexity Event Enrichment Module

This module uses Perplexity AI to validate and enrich individual cyber events with:
- Earliest/most accurate event dates
- Formal entity names
- Threat actor identification
- Attack method classification
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    import openai
except ImportError:
    openai = None

from pydantic import BaseModel, Field

from cyber_data_collector.utils import ConfigManager


class PerplexityEventEnrichment(BaseModel):
    """Structured response from Perplexity for event enrichment."""

    earliest_event_date: Optional[str] = Field(
        None,
        description="Earliest reported date of the incident in YYYY-MM-DD format"
    )
    date_confidence: Optional[float] = Field(
        None,
        description="Confidence in the date (0.0-1.0)"
    )

    formal_entity_name: Optional[str] = Field(
        None,
        description="Official/formal name of the affected organization"
    )
    entity_confidence: Optional[float] = Field(
        None,
        description="Confidence in the entity name (0.0-1.0)"
    )

    threat_actor: Optional[str] = Field(
        None,
        description="Name of the threat actor/group responsible"
    )
    threat_actor_confidence: Optional[float] = Field(
        None,
        description="Confidence in threat actor identification (0.0-1.0)"
    )

    attack_method: Optional[str] = Field(
        None,
        description="Primary attack method (ransomware, phishing, data breach, etc.)"
    )
    attack_method_confidence: Optional[float] = Field(
        None,
        description="Confidence in attack method classification (0.0-1.0)"
    )

    victim_count: Optional[int] = Field(
        None,
        description="Number of affected individuals/records"
    )
    victim_count_confidence: Optional[float] = Field(
        None,
        description="Confidence in victim count (0.0-1.0)"
    )

    sources_consulted: List[str] = Field(
        default_factory=list,
        description="List of sources Perplexity consulted"
    )

    overall_confidence: float = Field(
        0.0,
        description="Overall confidence in the enrichment"
    )

    reasoning: Optional[str] = Field(
        None,
        description="Explanation of how Perplexity arrived at these conclusions"
    )


class PerplexityDuplicateCheck(BaseModel):
    """Structured response for duplicate checking."""

    are_same_incident: bool = Field(
        description="Whether the two events describe the same incident"
    )
    confidence: float = Field(
        description="Confidence in the decision (0.0-1.0)"
    )
    reasoning: str = Field(
        description="Explanation of the decision"
    )


class PerplexityEnrichmentEngine:
    """Engine for enriching events using Perplexity AI."""

    def __init__(self, api_key: Optional[str] = None, logger: Optional[logging.Logger] = None):
        # Try to get API key from parameter, then environment, then .env file
        if api_key:
            self.api_key = api_key
        else:
            # First try environment variable
            self.api_key = os.environ.get("PERPLEXITY_API_KEY")
            
            # If not found, try loading from .env file
            if not self.api_key:
                try:
                    config_manager = ConfigManager()
                    env_config = config_manager.load()
                    self.api_key = env_config.get("PERPLEXITY_API_KEY")
                except Exception as e:
                    # ConfigManager might not be available, continue with None
                    pass
        
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self.client: Optional[openai.OpenAI] = None

        # Rate limiting
        self.min_request_interval = 2.0  # seconds between requests
        self.last_request_time = 0.0

        # Retry configuration
        self.max_retries = 3
        self.base_delay = 2.0
        self.max_delay = 60.0

        # Initialize client
        if self.api_key and openai:
            try:
                self.client = openai.OpenAI(
                    api_key=self.api_key,
                    base_url="https://api.perplexity.ai"
                )
                self.logger.info("Perplexity enrichment engine initialized")
            except Exception as e:
                self.logger.error(f"Failed to initialize Perplexity client: {e}")
        else:
            self.logger.warning("Perplexity API key not available or openai not installed")

    async def enrich_event(
        self,
        title: str,
        description: str,
        current_date: Optional[str] = None,
        current_entity: Optional[str] = None
    ) -> Optional[PerplexityEventEnrichment]:
        """
        Enrich a single event with validated information from Perplexity.

        Args:
            title: Event title
            description: Event description
            current_date: Currently assigned date (for validation)
            current_entity: Currently identified entity (for validation)

        Returns:
            PerplexityEventEnrichment object or None if enrichment fails
        """
        if not self.client:
            self.logger.warning("Perplexity client not initialized, skipping enrichment")
            return None

        # Rate limiting
        await self._rate_limit()

        # Build the enrichment prompt
        prompt = self._build_enrichment_prompt(title, description, current_date, current_entity)

        # Query Perplexity with retry logic
        try:
            response = await self._query_perplexity_with_retry(prompt)
            enrichment = self._parse_enrichment_response(response)

            self.logger.info(
                f"Enriched event '{title[:50]}...' - "
                f"Date: {enrichment.earliest_event_date} ({enrichment.date_confidence:.2f}), "
                f"Entity: {enrichment.formal_entity_name} ({enrichment.entity_confidence:.2f})"
            )

            return enrichment

        except Exception as e:
            self.logger.error(f"Failed to enrich event '{title[:50]}...': {e}")
            return None

    async def check_duplicate(
        self,
        event1_title: str,
        event1_description: str,
        event1_date: Optional[str],
        event1_entity: Optional[str],
        event2_title: str,
        event2_description: str,
        event2_date: Optional[str],
        event2_entity: Optional[str]
    ) -> Optional[PerplexityDuplicateCheck]:
        """
        Use Perplexity to determine if two events are the same incident.

        This is used for borderline cases where algorithmic similarity is uncertain.
        """
        if not self.client:
            self.logger.warning("Perplexity client not initialized, skipping duplicate check")
            return None

        # Rate limiting
        await self._rate_limit()

        # Build the duplicate check prompt
        prompt = self._build_duplicate_check_prompt(
            event1_title, event1_description, event1_date, event1_entity,
            event2_title, event2_description, event2_date, event2_entity
        )

        # Query Perplexity
        try:
            response = await self._query_perplexity_with_retry(prompt, max_tokens=500)
            check = self._parse_duplicate_check_response(response)

            self.logger.info(
                f"Duplicate check: '{event1_title[:30]}...' vs '{event2_title[:30]}...' - "
                f"Same: {check.are_same_incident} (confidence: {check.confidence:.2f})"
            )

            return check

        except Exception as e:
            self.logger.error(f"Failed duplicate check: {e}")
            return None

    def _build_enrichment_prompt(
        self,
        title: str,
        description: str,
        current_date: Optional[str],
        current_entity: Optional[str]
    ) -> str:
        """Build a detailed prompt for event enrichment."""

        prompt = f"""You are a cybersecurity incident analyst. Analyze the following Australian cyber security incident and provide validated, accurate information.

INCIDENT INFORMATION:
Title: {title}
Description: {description[:500]}...

{f"Currently assigned date: {current_date}" if current_date else "No date currently assigned"}
{f"Currently identified entity: {current_entity}" if current_entity else "No entity currently identified"}

TASK:
Research this incident using authoritative sources and provide:

1. **Earliest Event Date**: When did the incident actually occur (not when it was reported)?
   - Provide in YYYY-MM-DD format
   - If only month/year known, use YYYY-MM-01 or YYYY-01-01
   - Include confidence (0.0-1.0)

2. **Formal Entity Name**: What is the official legal name of the affected organization?
   - Not a brand name or abbreviation
   - As registered with government/regulators
   - Include confidence (0.0-1.0)

3. **Threat Actor**: Who was responsible (if known)?
   - Ransomware group, nation-state actor, individual, etc.
   - Only include if confirmed by reliable sources
   - Include confidence (0.0-1.0)

4. **Attack Method**: What was the primary attack vector?
   - Examples: ransomware, phishing, data breach, SQL injection, DDoS, etc.
   - Include confidence (0.0-1.0)

5. **Victim Count**: How many individuals/records were affected?
   - Numerical value only
   - Include confidence (0.0-1.0)

6. **Sources**: List the URLs of authoritative sources you consulted

7. **Reasoning**: Briefly explain how you arrived at these conclusions

Return your response as a JSON object with this exact structure:
{{
    "earliest_event_date": "YYYY-MM-DD or null",
    "date_confidence": 0.0-1.0,
    "formal_entity_name": "Official name or null",
    "entity_confidence": 0.0-1.0,
    "threat_actor": "Actor name or null",
    "threat_actor_confidence": 0.0-1.0,
    "attack_method": "Method or null",
    "attack_method_confidence": 0.0-1.0,
    "victim_count": number or null,
    "victim_count_confidence": 0.0-1.0,
    "sources_consulted": ["url1", "url2", ...],
    "overall_confidence": 0.0-1.0,
    "reasoning": "Brief explanation"
}}

If you cannot find reliable information for a field, set it to null and confidence to 0.0.
Only provide information you are confident about from authoritative sources.
"""
        return prompt

    def _build_duplicate_check_prompt(
        self,
        event1_title: str,
        event1_description: str,
        event1_date: Optional[str],
        event1_entity: Optional[str],
        event2_title: str,
        event2_description: str,
        event2_date: Optional[str],
        event2_entity: Optional[str]
    ) -> str:
        """Build a prompt for duplicate checking."""

        prompt = f"""You are a cybersecurity incident analyst. Determine if these two records describe the SAME cyber security incident.

EVENT 1:
Title: {event1_title}
Description: {event1_description[:300]}...
Date: {event1_date or "Unknown"}
Entity: {event1_entity or "Unknown"}

EVENT 2:
Title: {event2_title}
Description: {event2_description[:300]}...
Date: {event2_date or "Unknown"}
Entity: {event2_entity or "Unknown"}

DECISION CRITERIA:
- Same incident = Same organization suffered the same attack, with overlapping concrete details
- Different incident = Different organizations, OR same org but different attacks/dates/systems
- Consider: entity name variations, date reporting delays, updated victim counts

Return your response as JSON:
{{
    "are_same_incident": true or false,
    "confidence": 0.0-1.0,
    "reasoning": "Brief explanation of your decision"
}}

Be conservative: if uncertain, answer false.
"""
        return prompt

    async def _query_perplexity_with_retry(
        self,
        prompt: str,
        max_tokens: int = 1000
    ) -> str:
        """Query Perplexity with exponential backoff retry."""

        last_exception = None

        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0:
                    delay = min(
                        self.base_delay * (2 ** (attempt - 1)),
                        self.max_delay
                    )
                    self.logger.info(f"Retrying in {delay:.1f}s (attempt {attempt + 1}/{self.max_retries + 1})")
                    await asyncio.sleep(delay)

                response = self.client.chat.completions.create(
                    model="sonar-pro",
                    messages=[
                        {
                            "role": "system",
                            "content": "You are a precise cybersecurity analyst. Provide accurate, well-researched information in JSON format."
                        },
                        {
                            "role": "user",
                            "content": prompt
                        }
                    ],
                    temperature=0.1,
                    max_tokens=max_tokens
                )

                content = response.choices[0].message.content
                if not content:
                    raise ValueError("Empty response from Perplexity")

                return content

            except Exception as e:
                last_exception = e

                # Don't retry auth errors
                if "401" in str(e) or "403" in str(e) or "api key" in str(e).lower():
                    self.logger.error(f"Authentication error: {e}")
                    raise

                if attempt < self.max_retries:
                    self.logger.warning(f"Attempt {attempt + 1} failed: {e}")
                else:
                    self.logger.error(f"All {self.max_retries + 1} attempts failed")

        raise last_exception

    def _parse_enrichment_response(self, response: str) -> PerplexityEventEnrichment:
        """Parse Perplexity's JSON response into structured enrichment data."""
        try:
            # Extract JSON from response (handle markdown code blocks)
            json_str = response.strip()
            if json_str.startswith("```"):
                # Remove markdown code block
                lines = json_str.split("\n")
                json_str = "\n".join(lines[1:-1]) if len(lines) > 2 else json_str

            data = json.loads(json_str)
            return PerplexityEventEnrichment(**data)

        except (json.JSONDecodeError, ValueError) as e:
            self.logger.warning(f"Failed to parse Perplexity response: {e}")
            # Return empty enrichment with low confidence
            return PerplexityEventEnrichment(overall_confidence=0.0)

    def _parse_duplicate_check_response(self, response: str) -> PerplexityDuplicateCheck:
        """Parse duplicate check response."""
        try:
            json_str = response.strip()
            if json_str.startswith("```"):
                lines = json_str.split("\n")
                json_str = "\n".join(lines[1:-1]) if len(lines) > 2 else json_str

            data = json.loads(json_str)
            return PerplexityDuplicateCheck(**data)

        except (json.JSONDecodeError, ValueError) as e:
            self.logger.warning(f"Failed to parse duplicate check response: {e}")
            # Return conservative result
            return PerplexityDuplicateCheck(
                are_same_incident=False,
                confidence=0.0,
                reasoning="Failed to parse Perplexity response"
            )

    async def _rate_limit(self):
        """Ensure minimum time between requests."""
        import time
        current_time = time.time()
        time_since_last = current_time - self.last_request_time

        if time_since_last < self.min_request_interval:
            wait_time = self.min_request_interval - time_since_last
            await asyncio.sleep(wait_time)

        self.last_request_time = time.time()
