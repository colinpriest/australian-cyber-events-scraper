from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from cyber_data_collector.models.events import ConfidenceScore, CyberEvent, CyberEventType, EventSeverity
from cyber_data_collector.processing.deduplication import DeduplicationEngine


@pytest.fixture
def sample_events() -> list[CyberEvent]:
    confidence = ConfidenceScore(
        overall=0.8,
        source_reliability=0.8,
        data_completeness=0.7,
        temporal_accuracy=0.8,
        geographic_accuracy=0.9,
    )

    event1 = CyberEvent(
        title="Data breach at Company ABC",
        description="Major data breach incident",
        event_type=CyberEventType.DATA_BREACH,
        severity=EventSeverity.MEDIUM,
        event_date=datetime(2024, 1, 15),
        australian_relevance=True,
        confidence=confidence,
    )

    event2 = CyberEvent(
        title="Data breach at ABC Company",
        description="Significant data breach at ABC",
        event_type=CyberEventType.DATA_BREACH,
        severity=EventSeverity.MEDIUM,
        event_date=datetime(2024, 1, 16),
        australian_relevance=True,
        confidence=confidence.copy(deep=True),
    )

    event3 = CyberEvent(
        title="Phishing campaign targeting XYZ",
        description="Phishing attack on XYZ organisation",
        event_type=CyberEventType.PHISHING,
        severity=EventSeverity.MEDIUM,
        event_date=datetime(2024, 2, 1),
        australian_relevance=True,
        confidence=confidence.copy(deep=True),
    )

    return [event1, event2, event3]


@pytest.mark.asyncio
async def test_deduplication_merges_similar_events(sample_events: list[CyberEvent]) -> None:
    engine = DeduplicationEngine()
    deduplicated = await engine.deduplicate_events(sample_events)

    assert len(deduplicated) == 2
    merged_events = next(event for event in deduplicated if event.event_type == CyberEventType.DATA_BREACH)
    assert len(merged_events.merged_events) == 1


@pytest.mark.asyncio
async def test_deduplication_preserves_unique_events(sample_events: list[CyberEvent]) -> None:
    engine = DeduplicationEngine()
    deduplicated = await engine.deduplicate_events(sample_events)

    phishing_event = next(event for event in deduplicated if event.event_type == CyberEventType.PHISHING)
    assert phishing_event.merged_events == []









