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
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    import openai
except ImportError:
    openai = None

from pydantic import BaseModel, Field

from cyber_data_collector.processing.record_evidence import RecordEvidence
from cyber_data_collector.utils import ConfigManager
from cyber_data_collector.utils.api_budget import is_budget_refusal
from cyber_data_collector.utils.llm_models import perplexity_model

#: How much persisted article text the enrichment prompt quotes.
ENRICHMENT_PROMPT_CHARS = 4000


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
    # Set by the acceptance gate, never taken from the answer: how precisely
    # a source states the date ('day' or 'month' - a month-only statement
    # supports only a YYYY-MM-01 claim) and on what basis ('stated'; a date a
    # source carries only as its publication date is never accepted).
    date_precision: Optional[str] = Field(
        None,
        description="Precision the supporting source states the date to (set by the gate)"
    )
    date_basis: Optional[str] = Field(
        None,
        description="How the supporting source states the date (set by the gate)"
    )

    formal_entity_name: Optional[str] = Field(
        None,
        description="Official/formal name of the affected organization"
    )
    entity_confidence: Optional[float] = Field(
        None,
        description="Confidence in the entity name (0.0-1.0)"
    )
    victim_industry: Optional[str] = Field(
        None,
        description="Industry sector of the victim organization (e.g., Healthcare, Finance, Education, Government, Technology, Retail)"
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
    # Set by the acceptance gate from the supporting source's wording, never
    # taken from the answer; None when the source does not make it clear.
    victim_count_unit: Optional[str] = Field(
        None, description="What the count counts: records / accounts / customers / people (set by the gate)"
    )
    victim_count_basis: Optional[str] = Field(
        None, description="stated / estimate (incl. 'up to') / at_least (set by the gate)"
    )
    victim_count_scope: Optional[str] = Field(
        None, description="australian / global, when the source frames it (set by the gate)"
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

    # Parse bookkeeping.  A response that could not be used is marked here
    # instead of masquerading as an empty, zero-confidence success: the
    # acceptance gate refuses it and the backfill records a failed attempt.
    parse_failed: bool = Field(
        False,
        description="True when the response was missing, unparseable or not usable"
    )
    failure_reason: Optional[str] = Field(
        None,
        description="Why the response could not be used"
    )
    dropped_fields: List[str] = Field(
        default_factory=list,
        description="Fields discarded because their values were invalid"
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
    """Engine for enriching events using Perplexity AI.

    The model is ``PERPLEXITY_MODEL`` (:func:`llm_models.perplexity_model`),
    read at call time.  Construction fails loudly when the API key or the
    ``openai`` package is missing: there is no degraded mode that quietly
    skips enrichment.
    """

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
                    logging.getLogger(__name__).debug(f"ConfigManager unavailable: {e}")

        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self.client: Optional[openai.OpenAI] = None

        # Rate limiting
        self.min_request_interval = 2.0  # seconds between requests
        self.last_request_time = 0.0
        self._rate_lock = asyncio.Lock()

        # Retry configuration
        self.max_retries = 3
        self.base_delay = 2.0
        self.max_delay = 60.0

        # Initialize client - or fail, naming what is missing.
        if not self.api_key:
            raise RuntimeError("Perplexity enrichment needs PERPLEXITY_API_KEY (not set in the environment or .env)")
        if openai is None:
            raise RuntimeError("Perplexity enrichment needs the 'openai' package (pip install -r requirements.txt)")
        try:
            self.client = openai.OpenAI(
                api_key=self.api_key,
                base_url="https://api.perplexity.ai"
            )
        except Exception as e:
            raise RuntimeError(f"Failed to initialize the Perplexity client: {e}") from e
        self.logger.info("Perplexity enrichment engine initialized (model %s)", perplexity_model())

    async def enrich_event(
        self,
        title: str,
        description: str,
        current_date: Optional[str] = None,
        current_entity: Optional[str] = None,
        evidence: Optional[RecordEvidence] = None,
    ) -> Optional[PerplexityEventEnrichment]:
        """
        Enrich a single event with validated information from Perplexity.

        Args:
            title: Event title
            description: Event description
            current_date: Currently assigned date (for validation)
            current_entity: Currently identified entity (for validation)
            evidence: The record's persisted article text (or the lack of it).
                When given, the prompt quotes the article that was actually
                scraped instead of the processed description.

        Returns:
            The parsed answer.  A request or parse failure comes back as an
            enrichment with ``parse_failed=True`` and a ``failure_reason`` so
            the caller can record the failed attempt; it is never an empty
            success.

        Raises:
            RuntimeError: If the engine has no client (it was not constructed
                normally); nothing is quietly skipped.
        """
        if not self.client:
            raise RuntimeError("Perplexity client not initialized; cannot enrich")

        # Rate limiting
        await self._rate_limit()

        # Build the enrichment prompt
        prompt = self._build_enrichment_prompt(
            title, description, current_date, current_entity, evidence=evidence
        )

        # Query Perplexity with retry logic
        try:
            response = await self._query_perplexity_with_retry(prompt)
            enrichment = self._parse_enrichment_response(response)

            self.logger.info(
                f"Enriched event '{title[:50]}...' - "
                f"Date: {enrichment.earliest_event_date} ({(enrichment.date_confidence or 0.0):.2f}), "
                f"Entity: {enrichment.formal_entity_name} ({(enrichment.entity_confidence or 0.0):.2f})"
            )

            return enrichment

        except Exception as e:
            self.logger.error(f"Failed to enrich event '{title[:50]}...': {e}")
            return PerplexityEventEnrichment(
                parse_failed=True,
                failure_reason=f"request failed: {str(e)[:300]}",
            )

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

        Raises:
            RuntimeError: If there is no client, the request fails or the
                answer cannot be parsed.  A made-up "not the same, confidence
                0" answer is never returned in place of a real one.
        """
        if not self.client:
            raise RuntimeError("Perplexity client not initialized; cannot check duplicates")

        # Rate limiting
        await self._rate_limit()

        # Build the duplicate check prompt
        prompt = self._build_duplicate_check_prompt(
            event1_title, event1_description, event1_date, event1_entity,
            event2_title, event2_description, event2_date, event2_entity
        )

        # Query Perplexity (failures propagate to the caller)
        response = await self._query_perplexity_with_retry(prompt, max_tokens=500)
        check = self._parse_duplicate_check_response(response)

        self.logger.info(
            f"Duplicate check: '{event1_title[:30]}...' vs '{event2_title[:30]}...' - "
            f"Same: {check.are_same_incident} (confidence: {check.confidence:.2f})"
        )
        return check

    def _build_enrichment_prompt(
        self,
        title: str,
        description: str,
        current_date: Optional[str],
        current_entity: Optional[str],
        evidence: Optional[RecordEvidence] = None,
    ) -> str:
        """Build a detailed prompt for event enrichment."""

        if evidence is not None:
            # Quote the persisted article, or say plainly that there is none.
            description_block = evidence.prompt_block(max_chars=ENRICHMENT_PROMPT_CHARS)
        else:
            description_block = f"Description: {description[:500]}..."

        prompt = f"""You are a cybersecurity incident analyst. Analyze the following Australian cyber security incident and provide validated, accurate information.

INCIDENT INFORMATION:
Title: {title}
{description_block}

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
    "victim_industry": "Industry sector (e.g., Healthcare, Finance, Education, Government, Technology, Retail, Manufacturing, Energy, Transportation, Telecommunications) or null",
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
        max_tokens: int = 1500
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

                loop = asyncio.get_running_loop()
                model = perplexity_model()
                response = await loop.run_in_executor(
                    None,
                    lambda: self.client.chat.completions.create(
                        model=model,
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
                )

                # Track token usage
                from cyber_data_collector.utils.token_tracker import tracker
                if hasattr(response, 'usage') and response.usage:
                    tracker.record(
                        model, response.usage.prompt_tokens,
                        response.usage.completion_tokens, context="perplexity_enrichment",
                    )

                content = response.choices[0].message.content
                if not content:
                    raise ValueError("Empty response from Perplexity")

                return content

            except Exception as e:
                last_exception = e

                # A request refused by the run's API budget was never sent;
                # retrying it would only sleep and be refused again.
                if is_budget_refusal(e):
                    raise

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
        import re as _re
        try:
            # Extract JSON from response (handle markdown code blocks)
            json_str = response.strip()
            if json_str.startswith("```"):
                match = _re.search(r'```(?:json)?\s*\n?(.*?)\n?```', json_str, _re.DOTALL)
                if match:
                    json_str = match.group(1)
                else:
                    lines = json_str.split("\n")
                    json_str = "\n".join(lines[1:-1]) if len(lines) > 2 else json_str

            try:
                data = json.loads(json_str)
            except json.JSONDecodeError as e:
                # Perplexity frequently truncates the response at max_tokens,
                # cutting off the trailing reasoning/sources fields mid-string.
                # Salvage the fields that completed rather than discarding the
                # whole (otherwise useful) enrichment.
                data = self._repair_truncated_json(json_str)
                if data is None:
                    raise
                self.logger.info(
                    "Recovered %d field(s) from truncated Perplexity enrichment "
                    "response (%s)",
                    len(data), e,
                )

            if not isinstance(data, dict):
                raise ValueError(f"expected a JSON object, got {type(data).__name__}")
            return self._build_enrichment(data)

        except (json.JSONDecodeError, ValueError) as e:
            self.logger.warning(f"Failed to parse Perplexity response: {e}")
            # An unusable response is marked, not dressed up as an empty
            # success: the acceptance gate refuses it and the caller records a
            # failed attempt instead of marking the record validated.
            return PerplexityEventEnrichment(
                overall_confidence=0.0,
                parse_failed=True,
                failure_reason=f"unparseable response: {str(e)[:200]}",
            )

    def _build_enrichment(self, data: dict) -> PerplexityEventEnrichment:
        """Build the model, discarding fields whose values are invalid.

        One bad field (a fractional victim count, a confidence of "high") used
        to fail the whole answer and throw away the valid facts beside it.
        """
        from pydantic import ValidationError

        try:
            return PerplexityEventEnrichment(**data)
        except ValidationError as exc:
            bad = {str(err["loc"][0]) for err in exc.errors() if err.get("loc")}
            cleaned = {k: v for k, v in data.items() if k not in bad}
            enrichment = PerplexityEventEnrichment(**cleaned)
            enrichment.dropped_fields = sorted(bad)
            self.logger.info("Dropped invalid Perplexity field(s): %s", ", ".join(sorted(bad)))
            return enrichment

    @staticmethod
    def _repair_truncated_json(json_str: str) -> Optional[dict]:
        """Repair JSON truncated mid-value (e.g. by max_tokens).

        Scans the string tracking string/escape state and bracket depth, then
        cuts back to the last top-level comma (dropping the incomplete trailing
        field) and closes any still-open brackets/braces. Returns the parsed
        dict, or None if it still cannot be parsed.
        """
        stack: List[str] = []
        in_string = False
        escape = False
        last_comma_idx: Optional[int] = None
        last_comma_stack: Optional[List[str]] = None

        for i, ch in enumerate(json_str):
            if escape:
                escape = False
                continue
            if in_string:
                if ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]":
                if stack:
                    stack.pop()
            elif ch == ",":
                # Everything before this comma is a complete run of values.
                last_comma_idx = i
                last_comma_stack = list(stack)

        if last_comma_idx is None or not last_comma_stack:
            return None

        repaired = json_str[:last_comma_idx] + "".join(reversed(last_comma_stack))
        try:
            data = json.loads(repaired)
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, ValueError):
            return None

    def _parse_duplicate_check_response(self, response: str) -> PerplexityDuplicateCheck:
        """Parse duplicate check response."""
        import re as _re
        try:
            json_str = response.strip()
            if json_str.startswith("```"):
                match = _re.search(r'```(?:json)?\s*\n?(.*?)\n?```', json_str, _re.DOTALL)
                if match:
                    json_str = match.group(1)
                else:
                    lines = json_str.split("\n")
                    json_str = "\n".join(lines[1:-1]) if len(lines) > 2 else json_str

            try:
                data = json.loads(json_str)
            except json.JSONDecodeError as e:
                data = self._repair_truncated_json(json_str)
                if data is None:
                    raise
                # reasoning is the last (and required) field, so it is the most
                # likely casualty of truncation — backfill it if dropped.
                data.setdefault("reasoning", "Recovered from truncated response")
                self.logger.info(
                    "Recovered duplicate-check fields from truncated response (%s)", e
                )

            return PerplexityDuplicateCheck(**data)

        except (json.JSONDecodeError, ValueError) as e:
            # No invented "different, confidence 0" verdict: the caller must
            # see that there is no answer.
            raise ValueError(f"unparseable Perplexity duplicate-check response: {e}") from e

    async def _rate_limit(self):
        """Ensure minimum time between requests."""
        async with self._rate_lock:
            current_time = time.time()
            time_since_last = current_time - self.last_request_time

            if time_since_last < self.min_request_interval:
                wait_time = self.min_request_interval - time_since_last
                await asyncio.sleep(wait_time)

            self.last_request_time = time.time()
