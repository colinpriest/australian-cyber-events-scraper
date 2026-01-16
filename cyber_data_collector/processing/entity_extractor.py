from __future__ import annotations

import asyncio
import logging
from typing import List

from pydantic import BaseModel, Field
from tqdm import tqdm

from cyber_data_collector.models.events import AffectedEntity, CyberEvent, EntityType
from .llm_classifier import LLMClassifier


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
        if not self.llm_classifier.client:
            return events

        if not events:
            return events

        enhanced_events: List[CyberEvent] = []
        
        # Create semaphore to limit concurrent requests
        semaphore = asyncio.Semaphore(10)
        
        async def process_event_with_semaphore(event: CyberEvent) -> CyberEvent:
            async with semaphore:
                try:
                    return await self._extract_entities_for_event(event)
                except Exception as exc:
                    self.logger.error("Failed to extract entities for event %s: %s", event.event_id, exc)
                    return event  # Return original event on error
        
        # Create tasks for all events
        tasks = [process_event_with_semaphore(event) for event in events]
        
        # Process events concurrently with progress bar
        with tqdm(total=len(events), desc="Entity Extraction", unit="event", leave=False) as pbar:
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
        text_content = f"{event.title} {event.description}"
        for source in event.data_sources:
            if source.content_snippet:
                text_content += f" {source.content_snippet}"

        request = EntityExtractionRequest(
            text_content=text_content[:2000],
            existing_entities=[entity.name for entity in event.affected_entities],
        )

        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: self.llm_classifier.client.chat.completions.create(
                        model="gpt-4o-mini",
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
                    )
                ),
                timeout=60  # 60 second timeout
            )
        except asyncio.TimeoutError:
            self.logger.warning(f"Entity extraction timed out for event: {event.title[:50]}...")
            return event  # Return original event on timeout

        event_copy = event.copy(deep=True)
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

        if enhanced_entities:
            event_copy.affected_entities = enhanced_entities
            event_copy.primary_entity = enhanced_entities[0]
        else:
            event_copy.primary_entity = event.primary_entity

        return event_copy
