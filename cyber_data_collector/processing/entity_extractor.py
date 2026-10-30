from __future__ import annotations

import asyncio
import logging
from typing import List

from pydantic import BaseModel, Field
from tqdm import tqdm

from cyber_data_collector.models.events import AffectedEntity, CyberEvent, EntityType
from cyber_data_collector.utils import llm_retry
from cyber_data_collector.utils.llm_models import chat_params, openai_fast_model
from .llm_classifier import LLMClassifier, failed_copy


class EntityExtractionRequest(BaseModel):
    """Request payload for entity extraction."""

    text_content: str
    existing_entities: List[str]


class ExtractedEntity(BaseModel):
    """Single extracted entity."""

    name: str
    entity_type: EntityType
    industry_sector: str | None
    location: str | None
    is_australian: bool
    confidence: float = Field(..., ge=0.0, le=1.0)


class ExtractedEntities(BaseModel):
    """Response from entity extraction."""

    entities: List[ExtractedEntity] = Field(default_factory=list)


class EntityExtractor:
    """Extract and classify entities from cyber events."""

    def __init__(self, llm_classifier: LLMClassifier) -> None:
        self.llm_classifier = llm_classifier
        self.logger = logging.getLogger(self.__class__.__name__)

    async def extract_entities(self, events: List[CyberEvent]) -> List[CyberEvent]:
        if not events:
            return events

        if not self.llm_classifier.client:
            # No silent fallback: events without entities used to carry on.
            raise RuntimeError("OpenAI API key not configured: entities cannot be extracted")

        enhanced_events: List[CyberEvent] = []
        
        # Create semaphore to limit concurrent requests
        semaphore = asyncio.Semaphore(10)
        
        async def process_event_with_semaphore(event: CyberEvent) -> CyberEvent:
            async with semaphore:
                try:
                    return await self._extract_entities_for_event(event)
                except Exception as exc:
                    # Not a silent pass: the event is flagged failed, so it is
                    # neither classified nor stored without its entities; the
                    # discovery retry queue tries it again.
                    self.logger.error("Failed to extract entities for event %s: %s", event.event_id, exc)
                    return failed_copy(event, exc)
        
        # Create tasks for all events
        tasks = [process_event_with_semaphore(event) for event in events]
        
        # Process events concurrently with progress bar
        with tqdm(total=len(events), desc="Entity Extraction", unit="event", leave=False, smoothing=0) as pbar:
            for task in asyncio.as_completed(tasks):
                try:
                    enhanced_event = await task
                    enhanced_events.append(enhanced_event)
                    pbar.set_postfix({"processed": len(enhanced_events)})
                except Exception as exc:
                    self.logger.error("Task failed: %s", exc)
                    pbar.set_postfix({"processed": len(enhanced_events), "error": "Failed"})
                pbar.update(1)
        
        return enhanced_events

    async def _extract_entities_for_event(self, event: CyberEvent) -> CyberEvent:
        if getattr(event, "evidence_basis", None) == "article" and getattr(event, "evidence_text", None):
            # The persisted scraped article, not the discovery snippet: entities
            # named only in a site menu must not become victims.
            text_content = f"{event.title} {event.evidence_text}"
        else:
            text_content = f"{event.title} {event.description}"
            for source in event.data_sources:
                if source.content_snippet:
                    text_content += f" {source.content_snippet}"

        request = EntityExtractionRequest(
            text_content=text_content[:2000],
            existing_entities=[entity.name for entity in event.affected_entities],
        )

        model_name = openai_fast_model()

        def call() -> ExtractedEntities:
            return self.llm_classifier.client.chat.completions.create(
                **chat_params(model_name),
                response_model=ExtractedEntities,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Extract all entities mentioned in this cyber security event. Identify organizations, government agencies, "
                            "companies, and individuals. Classify their type and determine if they are Australian entities with confidence scores."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"Extract entities from: {request.model_dump_json()}",
                    },
                ],
                max_retries=llm_retry.validation_retries(),
            )

        # The classifier's client times out at the HTTP level and is not retried by the SDK; this one loop
        # retries transient failures (utils/llm_retry.py), so a slow request is cancelled, never left running.
        response = await llm_retry.call_with_retries(
            call, what=f"Entity extraction for {event.title[:50]!r}", logger=self.logger,
            sleep=getattr(self.llm_classifier, "sleep", asyncio.sleep))
        # Track token usage from instructor-wrapped response
        from cyber_data_collector.utils.token_tracker import tracker
        raw = getattr(response, '_raw_response', None)
        if raw and hasattr(raw, 'usage') and raw.usage:
            tracker.record(
                model_name, raw.usage.prompt_tokens,
                raw.usage.completion_tokens, context="entity_extraction",
            )

        event_copy = event.model_copy(deep=True)
        enhanced_entities: List[AffectedEntity] = []
        for extracted in response.entities:
            entity = AffectedEntity(
                name=extracted.name,
                entity_type=extracted.entity_type,
                industry_sector=extracted.industry_sector,
                location=extracted.location,
                australian_entity=extracted.is_australian,
                confidence_score=extracted.confidence,
            )
            enhanced_entities.append(entity)

        # The extraction's answer replaces whatever discovery guessed (a search
        # result's entity name, an OAIC title heuristic) and its confidence.
        # Finding nothing leaves the identity unresolved - it is not filled
        # back in from the guess.
        event_copy.affected_entities = enhanced_entities
        event_copy.primary_entity = enhanced_entities[0] if enhanced_entities else None
        if not enhanced_entities:
            self.logger.debug("No entities extracted for '%s': identity left unresolved", event.title[:50])

        return event_copy
