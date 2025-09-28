from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

import instructor
import openai
from pydantic import BaseModel, Field
from tqdm import tqdm

from cyber_data_collector.models.events import (
    CyberEvent,
    CyberEventType,
    EventSeverity,
    FinancialImpact,
)


class EventEnhancementRequest(BaseModel):
    """Request payload for LLM event enhancement."""

    title: str
    description: str
    entity_names: List[str]
    raw_data_sources: List[str]


class EventEnhancement(BaseModel):
    """Enhanced event information returned by LLM."""

    is_cybersecurity_event: bool = Field(description="True if this is a genuine cybersecurity incident, False otherwise")
    is_australian_relevant: bool = Field(description="True if this specifically affects Australian organizations/systems/citizens")
    rejection_reason: Optional[str] = Field(default=None, description="If not a cyber event, explain why")
    event_type: CyberEventType = Field(default=CyberEventType.OTHER, description="Event type - defaults to OTHER for rejected events")
    secondary_types: List[CyberEventType] = Field(default_factory=list)
    severity: EventSeverity = Field(default=EventSeverity.UNKNOWN, description="Event severity - defaults to UNKNOWN for rejected events")
    detailed_description: str = Field(default="", description="Detailed description - can be empty for rejected events")
    technical_details: str = Field(default="")  # Changed to simple string instead of complex dict
    estimated_customers_affected: Optional[int] = None
    estimated_financial_impact: Optional[float] = None
    regulatory_fine: Optional[float] = None  # Made explicitly Optional with default
    regulatory_undertaking: Optional[str] = None  # Made explicitly Optional with default
    response_actions: List[str] = Field(default_factory=list)
    attribution: Optional[str] = None
    confidence_adjustments: Dict[str, float] = Field(default_factory=dict)


class LLMClassifier:
    """LLM-based event classification and enhancement."""

    def __init__(self, openai_api_key: Optional[str]) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        if not openai_api_key:
            self.logger.warning("OpenAI API key not provided; classifier runs in degraded mode.")
            self.client = None
        else:
            self.client = instructor.from_openai(openai.OpenAI(api_key=openai_api_key))

    async def classify_events(self, events: List[CyberEvent]) -> List[CyberEvent]:
        """Classify and enhance events using the configured LLM with concurrent processing."""

        if not self.client:
            return events

        if not events:
            return events

        enhanced_events: List[CyberEvent] = []
        rejected_count = 0
        
        # Create semaphore to limit concurrent requests
        semaphore = asyncio.Semaphore(10)
        
        async def process_event_with_semaphore(event: CyberEvent) -> Optional[CyberEvent]:
            async with semaphore:
                try:
                    return await self._enhance_single_event(event)
                except Exception as exc:
                    self.logger.error("Failed to enhance event %s: %s", event.event_id, exc)
                    return event  # Return original event on error
        
        # Create tasks for all events
        tasks = [process_event_with_semaphore(event) for event in events]
        
        # Process events concurrently with progress bar
        with tqdm(total=len(events), desc="LLM Classification", unit="event", leave=False) as pbar:
            for task in asyncio.as_completed(tasks):
                try:
                    enhanced_event = await task
                    if enhanced_event is not None:
                        enhanced_events.append(enhanced_event)
                        pbar.set_postfix({"accepted": len(enhanced_events), "rejected": rejected_count})
                    else:
                        rejected_count += 1
                        pbar.set_postfix({"accepted": len(enhanced_events), "rejected": rejected_count})
                except Exception as exc:
                    self.logger.error("Task failed: %s", exc)
                    rejected_count += 1
                    pbar.set_postfix({"accepted": len(enhanced_events), "rejected": rejected_count})
                pbar.update(1)

        self.logger.info(f"LLM classification completed: {len(enhanced_events)} accepted, {rejected_count} rejected")
        return enhanced_events

    async def _enhance_single_event(self, event: CyberEvent) -> Optional[CyberEvent]:
        """Enhance a single event using LLM, or return None if rejected."""

        # For GDELT events, use the scraped content instead of the generic description
        description = event.description
        raw_data_sources = [source.content_snippet for source in event.data_sources if source.content_snippet]

        if any(source.source_type == "GDELT BigQuery" for source in event.data_sources):
            # For GDELT events, the description is always generic, so prioritize other content
            if raw_data_sources:
                # Use scraped content if available
                description = raw_data_sources[0][:1000]  # Use first 1000 chars of scraped content
            elif event.title and event.title != event.description:
                # Fall back to title if it's different from the generic description
                description = event.title

        enhancement_request = EventEnhancementRequest(
            title=event.title,
            description=description,
            entity_names=[entity.name for entity in event.affected_entities],
            raw_data_sources=[source.content_snippet for source in event.data_sources if source.content_snippet],
        )

        response = await self._invoke_llm(enhancement_request)
        return self._apply_enhancement_to_event(event, response)

    async def _invoke_llm(self, request: EventEnhancementRequest) -> EventEnhancement:
        if not self.client:
            raise RuntimeError("LLM client not configured")

        user_prompt = f"""
FIRST, determine if this is actually a cybersecurity INCIDENT and if it's Australian-relevant.

Event Title: {request.title}
Event Description: {request.description}
Affected Entities: {', '.join(request.entity_names)}
Raw Data Snippets: {' '.join(request.raw_data_sources)}

STEP 1 - VALIDATION (CRITICAL):
- `is_cybersecurity_event`: Is this genuinely about ONE SPECIFIC cybersecurity INCIDENT that actually happened to a named organization?
  - Return TRUE ONLY for: actual data breaches, cyber attacks, malware infections, ransomware attacks, phishing campaigns that OCCURRED to ONE specific named company/organization
  - Return FALSE for:
    * ANY document with words: "action plan", "plan for", "framework", "guidance", "guideline", "principle", "policy", "recommendation"
    * ANY document about creating, developing, or implementing policies/plans/frameworks
    * ANY document describing "multiple", "several", "various" incidents or breaches
    * ANY general market summaries, industry reports, or trend analyses
    * ANY document about incidents in a time period (e.g., "January 2020", "Q1 2020", "2020 breaches")
    * ANY document reporting statistics like "X reports", "X incidents", "X cybercrime reports"
    * Regulatory guidance, compliance documents, privacy principles, legal frameworks
    * Risk assessments, security recommendations, policy documents
    * General statistics, trend reports, sector overviews
    * Training materials, educational content, best practices
    * Celebrations, fireworks, New Year events, holidays, festivals, concerts, sports, elections, entertainment
    * "Email highlighted as key risk" - this is guidance, not an incident
    * "Summary of Privacy Principles" - this is regulatory guidance, not an incident
    * "Australian Data Breaches: January 2020" - this is a general market summary, not a specific incident
    * "Multiple Ransomware and Malware Data Breaches" - this is a general summary, not a specific incident
    * "Data breach action plan for health service providers" - this is a policy document, not an incident
    * "Action plans", "frameworks", "guidelines" - these are policies, not incidents
    * Anything about "creating", "developing", "implementing" plans or policies
- `is_australian_relevant`: Does this SPECIFIC INCIDENT affect Australian organizations, systems, or citizens?
  - Return FALSE for: generic global events, events in other countries without Australian impact
- `rejection_reason`: If either above is false, explain why this should be rejected

STEP 2 - CLASSIFICATION:
- `event_type`: If cybersecurity incident, classify into appropriate category. If rejected, use "Other".
- `secondary_types`: List any other relevant event categories (empty list if rejected).
- `severity`: If cybersecurity incident, assess severity. If rejected, use "Unknown".
- `detailed_description`: If cybersecurity incident, provide detailed description. If rejected, can be empty.
- `technical_details`: If cybersecurity incident, provide technical details. If rejected, leave empty.
- `estimated_customers_affected`: If cybersecurity incident and mentioned, extract number. Otherwise null.
- `estimated_financial_impact`: If cybersecurity incident and mentioned, extract amount. Otherwise null.
- `regulatory_fine`: If mentioned, extract amount. Otherwise null.
- `regulatory_undertaking`: If mentioned, describe. Otherwise null.
- `response_actions`: If cybersecurity incident, list response actions. Otherwise empty list.
- `attribution`: If cybersecurity incident and mentioned, identify threat actor. Otherwise null.

CRITICAL REQUIREMENTS:
- AUTOMATICALLY REJECT if title contains: "action plan", "plan for", "framework", "guidance", "guideline", "principle", "policy", "recommendation", "creating", "developing", "implementing", "multiple", "several", "various", "reports", "incidents"
- AUTOMATICALLY REJECT any title describing time periods: "January 2020", "Q1 2020", "2020 breaches", etc.
- REJECT anything that is regulatory guidance, compliance documents, or policy frameworks
- REJECT general risk assessments or sector trend reports
- REJECT general market summaries, industry reports, or analyses of multiple incidents
- REJECT educational materials or best practice guides
- REJECT anything about creating, developing, or implementing policies/plans/frameworks
- ONLY ACCEPT specific incidents where data was actually compromised, systems were actually attacked, or security was actually breached at ONE specific named organization
- Must be about something that HAPPENED to one organization, not multiple incidents or general summaries
- Be extremely conservative. When in doubt, reject the event.
- Always provide all fields even for rejected events (use defaults for rejected events).
"""

        response = await asyncio.to_thread(
            lambda: self.client.chat.completions.create(
                model="gpt-4o-mini",
                response_model=EventEnhancement,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a strict cybersecurity incident analyst. ONLY classify events that are genuine cybersecurity INCIDENTS "
                            "where actual damage, compromise, or breach occurred to ONE SPECIFIC NAMED ORGANIZATION. "
                            "AUTOMATICALLY REJECT any title containing these words: 'action plan', 'plan for', 'framework', 'guidance', 'guideline', 'principle', 'policy', 'recommendation', 'creating', 'developing', 'implementing', 'multiple', 'several', 'various', 'reports', 'incidents'. "
                            "AUTOMATICALLY REJECT any title describing incidents in time periods like 'January 2020', 'Q1 2020', '2020 breaches'. "
                            "REJECT any content that is NOT a specific incident that actually happened to one organization: "
                            "- Regulatory guidance, compliance documents, privacy principles, legal frameworks "
                            "- Risk assessments, security recommendations, policy documents, action plans "
                            "- General statistics, trend reports, sector overviews, educational materials "
                            "- General market summaries, industry reports, or analyses of multiple incidents "
                            "- Training content, best practices, guidelines, frameworks "
                            "- Celebrations, fireworks, New Year events, holidays, festivals, concerts, sports, elections, entertainment "
                            "Examples to REJECT: 'Australian Data Breaches: January 2020', 'Multiple Ransomware and Malware Data Breaches', 'COVID-19 themed malicious cyber activity', 'ACSC has received over 45 cybercrime reports', 'Email highlighted as key risk', 'Summary of Privacy Principles', 'Data breach action plan for health service providers' "
                            "ONLY accept specific incidents like: 'Medibank data breach', 'Toll Group ransomware attack', 'Canva security incident' "
                            "ONLY flag as Australian if the SPECIFIC INCIDENT affects Australian organizations, systems, or citizens. "
                            "Be extremely conservative. When in doubt, REJECT the event."
                        ),
                    },
                    {
                        "role": "user",
                        "content": user_prompt,
                    },
                ],
                max_retries=2,
            )
        )
        return response

    def _apply_enhancement_to_event(self, event: CyberEvent, enhancement: EventEnhancement) -> Optional[CyberEvent]:
        """Apply LLM enhancement to the event record, or return None if rejected."""

        # Check if LLM determined this is not a cybersecurity event
        if not enhancement.is_cybersecurity_event:
            self.logger.debug(f"LLM rejected event '{event.title[:50]}...': {enhancement.rejection_reason}")
            return None

        # Check Australian relevance
        if not enhancement.is_australian_relevant:
            self.logger.debug(f"LLM rejected event as not Australian relevant '{event.title[:50]}...': {enhancement.rejection_reason}")
            return None

        event_copy = event.copy(deep=True)
        event_copy.event_type = enhancement.event_type
        event_copy.secondary_types = enhancement.secondary_types
        event_copy.severity = enhancement.severity
        event_copy.description = enhancement.detailed_description or event.description

        if enhancement.technical_details:
            # enhancement.technical_details is a string, not a dict
            if event_copy.technical_details:
                # If event already has technical_details, combine them
                if isinstance(event_copy.technical_details, dict):
                    event_copy.technical_details["llm_analysis"] = enhancement.technical_details
                else:
                    event_copy.technical_details = {
                        "original": str(event_copy.technical_details),
                        "llm_analysis": enhancement.technical_details
                    }
            else:
                # Create new technical_details with LLM analysis
                event_copy.technical_details = {"llm_analysis": enhancement.technical_details}

        if event_copy.financial_impact is None:
            event_copy.financial_impact = FinancialImpact()

        if event_copy.financial_impact:
            if enhancement.estimated_financial_impact is not None:
                event_copy.financial_impact.estimated_cost = enhancement.estimated_financial_impact
            if enhancement.regulatory_fine is not None:
                event_copy.financial_impact.regulatory_fine = enhancement.regulatory_fine
            if enhancement.regulatory_undertaking is not None:
                event_copy.financial_impact.regulatory_undertaking = enhancement.regulatory_undertaking
            if enhancement.estimated_customers_affected is not None:
                event_copy.financial_impact.customers_affected = enhancement.estimated_customers_affected

        if enhancement.response_actions:
            existing_actions = set(event_copy.response_actions)
            event_copy.response_actions = list(existing_actions.union(enhancement.response_actions))

        event_copy.attribution = enhancement.attribution or event_copy.attribution

        if enhancement.confidence_adjustments and event_copy.confidence:
            overall_adjustment = enhancement.confidence_adjustments.get("overall", 0.0)
            event_copy.confidence.overall = min(max(event_copy.confidence.overall + overall_adjustment, 0.0), 1.0)

        return event_copy
