from __future__ import annotations

"""
Enhanced deduplication system with object-oriented design, comprehensive validation,
and merge lineage tracking.

===========================================================================================
CRITICAL: EVENT DEFINITION AND DEDUPLICATION RULES
===========================================================================================

**AN EVENT REPRESENTS A SINGLE REAL-WORLD CYBER SECURITY INCIDENT.**

Multiple news articles, blog posts, or reports about the SAME breach/incident MUST be
MERGED into ONE event. We are tracking security INCIDENTS, not news ARTICLES.

**DEDUPLICATION RULES (in order of priority):**

1. **RULE 1: Same Entity + Same Date → MERGE**
   - If victim_organization_name matches AND event_date matches → ALWAYS merge
   - Different titles are OK (e.g., "Ticketmaster hacked" vs "Live Nation breach")

2. **RULE 2: Same Entity + Similar Descriptions → MERGE**
   - If victim_organization_name matches AND descriptions are similar → MERGE
   - Even if dates differ (news articles report on different dates)
   - Use the EARLIEST reliable date as the canonical incident date

3. **RULE 3: Different dates mean different reporting, NOT different incidents**
   - A breach disclosed on May 20 may have articles published April 15, May 20, May 21
   - These are all reports about the SAME breach → 1 event with earliest date

**Examples:**
- ✓ CORRECT: "Ticketmaster hack" (Apr 15) + "Live Nation breach" (May 20) → 1 event dated Apr 15
- ✗ WRONG: "Ticketmaster hack" (Apr 15) + "Live Nation breach" (May 20) → 2 separate events

**The goal is: "How many distinct security incidents occurred at each organization?"**
NOT "How many news articles were published?"

===========================================================================================

This module provides a complete deduplication solution that:
- Uses clear separation of concerns
- Provides comprehensive error checking and validation
- Tracks merge lineage for transparency
- Ensures idempotent operations
- Supports both algorithmic and LLM-based similarity detection
- **Prioritizes incident matching over text similarity**
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
import difflib
import hashlib

@dataclass
class CyberEvent:
    """Simplified cyber event model for deduplication"""
    event_id: str
    title: str
    summary: Optional[str] = None
    description: Optional[str] = None  # Raw scraped text content for semantic matching
    event_date: Optional[date] = None
    event_type: Optional[str] = None
    severity: Optional[str] = None
    records_affected: Optional[int] = None
    victim_organization_name: Optional[str] = None
    victim_organization_industry: Optional[str] = None
    data_sources: List[str] = None
    urls: List[str] = None
    confidence: float = 0.5

    def __post_init__(self):
        if self.data_sources is None:
            self.data_sources = []
        if self.urls is None:
            self.urls = []

from .entity_extractor import EntityExtractor
from ..utils.validation import validate_records_affected

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ValidationError:
    """Represents a validation error with context"""
    error_type: str
    message: str
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SimilarityScore:
    """Comprehensive similarity score between two events"""
    overall_score: float
    title_similarity: float
    entity_similarity: float
    content_similarity: float
    temporal_similarity: float
    confidence: float
    reasoning: str


@dataclass(frozen=True)
class ArbiterDecision:
    """LLM arbiter decision for uncertain similarity cases"""
    is_similar: bool
    confidence: float
    reasoning: str
    original_score: float


@dataclass(frozen=True)
class MergeGroup:
    """Represents a group of events merged into one"""
    master_event: CyberEvent
    merged_events: List[CyberEvent]
    similarity_scores: Dict[str, float]
    merge_reason: str
    confidence: float
    merge_timestamp: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class DeduplicationStats:
    """Statistics about the deduplication process"""
    input_events: int
    output_events: int
    merge_groups: int
    total_merges: int
    avg_confidence: float
    processing_time_seconds: float


@dataclass(frozen=True)
class DeduplicationResult:
    """Immutable result of deduplication operation"""
    unique_events: List[CyberEvent]
    merge_groups: List[MergeGroup]
    statistics: DeduplicationStats
    validation_errors: List[ValidationError]
    processing_timestamp: datetime = field(default_factory=datetime.now)


class DeduplicationValidator:
    """Validates deduplication results and inputs"""
    
    def __init__(self):
        self.logger = logging.getLogger(f"{__name__}.DeduplicationValidator")
    
    def validate_inputs(self, events: List[CyberEvent]) -> List[ValidationError]:
        """Validate input events for deduplication"""
        errors = []
        
        if not events:
            errors.append(ValidationError(
                error_type="EMPTY_INPUT",
                message="No events provided for deduplication",
                context={"event_count": 0}
            ))
            return errors
        
        # Check for duplicate IDs
        event_ids = [e.event_id for e in events if e.event_id]
        if len(event_ids) != len(set(event_ids)):
            duplicates = [id for id in event_ids if event_ids.count(id) > 1]
            errors.append(ValidationError(
                error_type="DUPLICATE_EVENT_IDS",
                message=f"Found duplicate event IDs: {duplicates}",
                context={"duplicate_ids": duplicates}
            ))
        
        # Check for required fields
        for i, event in enumerate(events):
            if not event.title or event.title.strip() == "":
                errors.append(ValidationError(
                    error_type="MISSING_TITLE",
                    message=f"Event at index {i} has missing or empty title",
                    context={"event_index": i, "event_id": event.event_id}
                ))
        
        self.logger.info(f"Input validation: {len(errors)} errors found")
        return errors
    
    def validate_no_duplicates(self, events: List[CyberEvent]) -> List[ValidationError]:
        """Check that no duplicate events exist in the result"""
        errors = []
        
        # Check for exact title+date duplicates
        seen_combinations = set()
        for event in events:
            key = (event.title.lower().strip(), event.event_date)
            if key in seen_combinations:
                errors.append(ValidationError(
                    error_type="DUPLICATE_EVENT",
                    message=f"Found duplicate event: {event.title} on {event.event_date}",
                    context={"title": event.title, "date": event.event_date}
                ))
            seen_combinations.add(key)
        
        # Check for very similar titles with same date (only for exact duplicates)
        # Skip this check as it's too strict and catches legitimate similar events
        # for i, event1 in enumerate(events):
        #     for j, event2 in enumerate(events[i+1:], i+1):
        #         if (event1.event_date == event2.event_date and 
        #             self._titles_very_similar(event1.title, event2.title)):
        #             errors.append(ValidationError(
        #                 error_type="SIMILAR_EVENT",
        #                 message=f"Found very similar events: '{event1.title}' and '{event2.title}'",
        #                 context={
        #                     "event1": {"title": event1.title, "date": event1.event_date},
        #                     "event2": {"title": event2.title, "date": event2.event_date}
        #                 }
        #             ))
        
        self.logger.info(f"Duplicate validation: {len(errors)} errors found")
        return errors
    
    def validate_merge_groups(self, groups: List[MergeGroup]) -> List[ValidationError]:
        """Validate merge groups for consistency"""
        errors = []
        
        for group in groups:
            if not group.master_event:
                errors.append(ValidationError(
                    error_type="MISSING_MASTER_EVENT",
                    message="Merge group has no master event",
                    context={"group_size": len(group.merged_events)}
                ))
            
            # Only validate merge groups that actually have multiple events
            # Single-event groups are normal for events without duplicates
            if len(group.merged_events) == 0 and group.master_event:
                # This is a single event that wasn't merged - this is normal
                pass
            elif len(group.merged_events) < 1:
                # This is a real problem - a merge group with no merged events
                errors.append(ValidationError(
                    error_type="INSUFFICIENT_MERGES",
                    message="Merge group has no events to merge",
                    context={"group_size": len(group.merged_events)}
                ))
        
        self.logger.info(f"Merge group validation: {len(errors)} errors found")
        return errors
    
    def validate_data_integrity(self, events: List[CyberEvent]) -> List[ValidationError]:
        """Validate data integrity of events"""
        errors = []
        
        for event in events:
            # Check for reasonable dates
            if event.event_date and event.event_date > datetime.now().date():
                errors.append(ValidationError(
                    error_type="FUTURE_DATE",
                    message=f"Event has future date: {event.event_date}",
                    context={"event_id": event.event_id, "date": event.event_date}
                ))
            
            # Check for reasonable records affected
            if event.records_affected and event.records_affected < 0:
                errors.append(ValidationError(
                    error_type="NEGATIVE_RECORDS",
                    message=f"Event has negative records affected: {event.records_affected}",
                    context={"event_id": event.event_id, "records": event.records_affected}
                ))
        
        self.logger.info(f"Data integrity validation: {len(errors)} errors found")
        return errors
    
    def _titles_very_similar(self, title1: str, title2: str) -> bool:
        """Check if two titles are very similar (likely duplicates)"""
        if not title1 or not title2:
            return False
        
        # Normalize titles
        norm1 = re.sub(r'\s+', ' ', title1.lower().strip())
        norm2 = re.sub(r'\s+', ' ', title2.lower().strip())
        
        # Check for very high similarity
        similarity = difflib.SequenceMatcher(None, norm1, norm2).ratio()
        return similarity > 0.9


class SimilarityCalculator:
    """Calculates similarity between events using multiple algorithms"""
    
    def __init__(self, entity_extractor: Optional[EntityExtractor] = None):
        self.entity_extractor = entity_extractor
        self.logger = logging.getLogger(f"{__name__}.SimilarityCalculator")
    
    def calculate_similarity(self, event1: CyberEvent, event2: CyberEvent) -> SimilarityScore:
        """Calculate comprehensive similarity between two events"""
        title_sim = self._title_similarity(event1, event2)
        entity_sim = self._entity_similarity(event1, event2)
        content_sim = self._content_similarity(event1, event2)
        temporal_sim = self._temporal_similarity(event1, event2)
        
        # Weighted overall score
        overall_score = (
            title_sim * 0.4 +
            entity_sim * 0.3 +
            content_sim * 0.2 +
            temporal_sim * 0.1
        )
        
        # Calculate confidence based on consistency of scores
        scores = [title_sim, entity_sim, content_sim, temporal_sim]
        confidence = 1.0 - (max(scores) - min(scores))  # Higher confidence when scores agree
        
        reasoning = self._generate_reasoning(event1, event2, {
            'title': title_sim,
            'entity': entity_sim,
            'content': content_sim,
            'temporal': temporal_sim
        })
        
        return SimilarityScore(
            overall_score=overall_score,
            title_similarity=title_sim,
            entity_similarity=entity_sim,
            content_similarity=content_sim,
            temporal_similarity=temporal_sim,
            confidence=confidence,
            reasoning=reasoning
        )
    
    def _title_similarity(self, event1: CyberEvent, event2: CyberEvent) -> float:
        """Calculate title similarity using multiple methods"""
        if not event1.title or not event2.title:
            return 0.0
        
        # Normalize titles
        title1 = self._normalize_text(event1.title)
        title2 = self._normalize_text(event2.title)
        
        # Exact match
        if title1 == title2:
            return 1.0
        
        # Check for very similar titles (common variations)
        if self._are_titles_very_similar(title1, title2):
            return 0.95
        
        # Sequence similarity
        seq_sim = difflib.SequenceMatcher(None, title1, title2).ratio()
        
        # Check for substring matches
        if title1 in title2 or title2 in title1:
            seq_sim = max(seq_sim, 0.8)
        
        # Check for common keywords
        words1 = set(title1.split())
        words2 = set(title2.split())
        if words1 and words2:
            word_overlap = len(words1.intersection(words2)) / len(words1.union(words2))
            seq_sim = max(seq_sim, word_overlap)
        
        return seq_sim
    
    def _are_titles_very_similar(self, title1: str, title2: str) -> bool:
        """Check if titles are very similar (likely duplicates)"""
        # Remove common variations
        variations = [
            (' - ', ' '),
            (' | ', ' '),
            ('...', ''),
            ('...', ''),
            ('  ', ' '),  # Double spaces
        ]
        
        norm1 = title1
        norm2 = title2
        
        for old, new in variations:
            norm1 = norm1.replace(old, new)
            norm2 = norm2.replace(old, new)
        
        # Check if normalized titles are very similar
        if norm1 == norm2:
            return True
        
        # Check for high similarity
        similarity = difflib.SequenceMatcher(None, norm1, norm2).ratio()
        return similarity > 0.9
    
    def _entity_similarity(self, event1: CyberEvent, event2: CyberEvent) -> float:
        """Calculate entity similarity between events"""
        entities1 = self._extract_entities(event1)
        entities2 = self._extract_entities(event2)
        
        if not entities1 and not entities2:
            return 0.0
        
        if not entities1 or not entities2:
            return 0.0
        
        # Calculate Jaccard similarity
        set1 = set(entities1)
        set2 = set(entities2)
        intersection = len(set1.intersection(set2))
        union = len(set1.union(set2))
        
        return intersection / union if union > 0 else 0.0
    
    def _content_similarity(self, event1: CyberEvent, event2: CyberEvent) -> float:
        """Calculate content similarity from summary/description"""
        content1 = self._get_content_text(event1)
        content2 = self._get_content_text(event2)
        
        if not content1 or not content2:
            return 0.0
        
        # Normalize content
        norm1 = self._normalize_text(content1)
        norm2 = self._normalize_text(content2)
        
        # Use sequence similarity
        return difflib.SequenceMatcher(None, norm1, norm2).ratio()
    
    def _temporal_similarity(self, event1: CyberEvent, event2: CyberEvent) -> float:
        """Calculate temporal similarity based on event dates"""
        if not event1.event_date or not event2.event_date:
            return 0.0
        
        # Calculate days difference
        date_diff = abs((event1.event_date - event2.event_date).days)
        
        # Exponential decay: closer dates = higher similarity
        if date_diff == 0:
            return 1.0
        elif date_diff <= 7:
            return 0.8
        elif date_diff <= 30:
            return 0.6
        elif date_diff <= 90:
            return 0.4
        elif date_diff <= 365:
            return 0.2
        else:
            return 0.0
    
    def _extract_entities(self, event: CyberEvent) -> List[str]:
        """Extract entities from event using regex-based heuristics.

        Note: EntityExtractor.extract_entities expects List[CyberEvent] and is async,
        so external entity_extractor instances are not supported here.  The regex
        fallback is always used instead.
        """
        entities = []

        if self.entity_extractor is not None:
            self.logger.warning(
                "External entity_extractor is not supported in SimilarityCalculator; "
                "using regex fallback"
            )

        # Always use the regex fallback
        if event.title:
            # Extract potential company names (simple heuristic)
            # Look for capitalized words that might be company names
            title_words = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', event.title)
            entities.extend(title_words)

            stopwords = {
                "Data",
                "Breach",
                "Incident",
                "Security",
                "Attack",
                "Cyber",
                "Cybersecurity",
                "Ransomware",
                "Malware",
                "Hack",
                "Hackers",
                "Outage",
                "Leak",
                "Exposure",
                "Network",
            }
            for phrase in title_words:
                for token in phrase.split():
                    if token not in stopwords:
                        entities.append(token)
        
        return list(set(entities))  # Remove duplicates
    
    def _get_content_text(self, event: CyberEvent) -> str:
        """Get content text from event (summary or description)"""
        if event.summary:
            return event.summary
        elif hasattr(event, 'description') and event.description:
            return event.description
        return ""
    
    def _normalize_text(self, text: str) -> str:
        """Normalize text for comparison"""
        if not text:
            return ""
        
        # Convert to lowercase and remove extra whitespace
        normalized = re.sub(r'\s+', ' ', text.lower().strip())
        
        # Remove common punctuation that might vary
        normalized = re.sub(r'[^\w\s]', '', normalized)
        
        return normalized
    
    def _generate_reasoning(self, event1: CyberEvent, event2: CyberEvent, scores: Dict[str, float]) -> str:
        """Generate human-readable reasoning for similarity score"""
        reasons = []
        
        if scores['title'] > 0.8:
            reasons.append("titles are very similar")
        elif scores['title'] > 0.5:
            reasons.append("titles have some similarity")
        
        if scores['entity'] > 0.7:
            reasons.append("share common entities")
        elif scores['entity'] > 0.3:
            reasons.append("have some entity overlap")
        
        if scores['temporal'] > 0.6:
            reasons.append("occurred around the same time")
        
        if not reasons:
            return "events have minimal similarity"
        
        return f"events {', '.join(reasons)}"


class LLMArbiter:
    """LLM-based decision maker for uncertain similarity cases"""
    
    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-3.5-turbo"):
        self.api_key = api_key
        self.model = model
        self.logger = logging.getLogger(f"{__name__}.LLMArbiter")
    
    def decide_similarity(self, event1: CyberEvent, event2: CyberEvent, algo_score: float) -> ArbiterDecision:
        """Use LLM to decide if events are similar when algorithmic score is uncertain"""
        if not self.api_key:
            # Fallback to algorithmic decision
            return ArbiterDecision(
                is_similar=algo_score > 0.5,
                confidence=0.5,
                reasoning="No LLM API key available, using algorithmic score",
                original_score=algo_score
            )
        
        if not self._should_use_arbiter(algo_score):
            return ArbiterDecision(
                is_similar=algo_score > 0.5,
                confidence=0.8,
                reasoning="Algorithmic score is confident, no LLM needed",
                original_score=algo_score
            )
        
        try:
            prompt = self._format_prompt(event1, event2, algo_score)
            response = self._call_llm(prompt)
            return self._parse_llm_response(response, algo_score)
        except Exception as e:
            self.logger.warning(f"LLM arbiter failed: {e}, falling back to algorithmic score")
            return ArbiterDecision(
                is_similar=algo_score > 0.5,
                confidence=0.5,
                reasoning=f"LLM failed ({e}), using algorithmic score",
                original_score=algo_score
            )
    
    def _should_use_arbiter(self, algo_score: float) -> bool:
        """Determine if LLM arbiter should be used"""
        # Use LLM when algorithmic score is uncertain (0.3-0.7 range)
        return 0.3 <= algo_score <= 0.7
    
    def _format_prompt(self, event1: CyberEvent, event2: CyberEvent, algo_score: float) -> str:
        """Format prompt for LLM"""
        return f"""
You are analyzing two cybersecurity events to determine if they are duplicates or different incidents.

Event 1:
- Title: {event1.title}
- Date: {event1.event_date}
- Summary: {event1.summary or 'No summary available'}
- Type: {event1.event_type or 'Unknown'}

Event 2:
- Title: {event2.title}
- Date: {event2.event_date}
- Summary: {event2.summary or 'No summary available'}
- Type: {event2.event_type or 'Unknown'}

Algorithmic similarity score: {algo_score:.2f}

Are these the same cybersecurity incident? Consider:
1. Are they about the same breach/attack?
2. Do they involve the same organization?
3. Are the dates consistent with the same incident?
4. Are the details describing the same event?

Respond with JSON:
{{
    "is_similar": true/false,
    "confidence": 0.0-1.0,
    "reasoning": "Brief explanation of your decision"
}}
"""
    
    def _call_llm(self, prompt: str) -> str:
        """Call LLM API - not yet implemented."""
        raise NotImplementedError(
            "LLMArbiter._call_llm is not implemented. "
            "Set api_key=None to use rule-based deduplication only."
        )
    
    def _parse_llm_response(self, response: str, original_score: float) -> ArbiterDecision:
        """Parse LLM response into ArbiterDecision"""
        try:
            import json
            data = json.loads(response)
            return ArbiterDecision(
                is_similar=data.get("is_similar", False),
                confidence=data.get("confidence", 0.5),
                reasoning=data.get("reasoning", "LLM decision"),
                original_score=original_score
            )
        except Exception as e:
            self.logger.error(f"Failed to parse LLM response: {e}")
            return ArbiterDecision(
                is_similar=original_score > 0.5,
                confidence=0.3,
                reasoning=f"Failed to parse LLM response: {e}",
                original_score=original_score
            )


class DeduplicationEngine:
    """Main deduplication orchestrator with comprehensive validation"""

    def __init__(self,
                 similarity_threshold: float = 0.75,
                 llm_arbiter: Optional[LLMArbiter] = None,
                 validators: Optional[List[DeduplicationValidator]] = None,
                 entity_mappings: Optional[Dict[str, str]] = None):
        self.similarity_threshold = similarity_threshold
        self.llm_arbiter = llm_arbiter
        self.validators = validators or [DeduplicationValidator()]
        self.logger = logging.getLogger(f"{__name__}.DeduplicationEngine")
        self.similarity_calculator = SimilarityCalculator()  # Reuse calculator instance
        # Entity mappings: source_entity (lowercase) -> canonical_entity
        self.entity_mappings = {k.lower(): v for k, v in (entity_mappings or {}).items()}
    
    def deduplicate(self, events: List[CyberEvent]) -> DeduplicationResult:
        """Perform comprehensive deduplication with validation"""
        start_time = datetime.now()
        
        # Validate inputs
        input_errors = []
        for validator in self.validators:
            input_errors.extend(validator.validate_inputs(events))
        
        if input_errors:
            self.logger.error(f"Input validation failed: {len(input_errors)} errors")
            return DeduplicationResult(
                unique_events=[],
                merge_groups=[],
                statistics=DeduplicationStats(0, 0, 0, 0, 0.0, 0.0),
                validation_errors=input_errors
            )
        
        # Group similar events
        event_groups = self._group_similar_events(events)
        
        # Merge each group
        unique_events = []
        merge_groups = []
        total_merges = 0
        
        for group in event_groups:
            if len(group) == 1:
                # Single event, no merging needed
                unique_events.append(group[0])
            else:
                # Multiple events, merge them
                merged_event, merge_group = self._merge_group(group)
                unique_events.append(merged_event)
                merge_groups.append(merge_group)
                total_merges += len(group) - 1
        
        # Validate outputs
        output_errors = []
        for validator in self.validators:
            output_errors.extend(validator.validate_no_duplicates(unique_events))
            output_errors.extend(validator.validate_merge_groups(merge_groups))
            output_errors.extend(validator.validate_data_integrity(unique_events))
        
        # Calculate statistics
        processing_time = (datetime.now() - start_time).total_seconds()
        avg_confidence = sum(g.confidence for g in merge_groups) / len(merge_groups) if merge_groups else 1.0
        
        statistics = DeduplicationStats(
            input_events=len(events),
            output_events=len(unique_events),
            merge_groups=len(merge_groups),
            total_merges=total_merges,
            avg_confidence=avg_confidence,
            processing_time_seconds=processing_time
        )
        
        self.logger.info(f"Deduplication complete: {len(events)} -> {len(unique_events)} events")
        
        return DeduplicationResult(
            unique_events=unique_events,
            merge_groups=merge_groups,
            statistics=statistics,
            validation_errors=output_errors
        )
    
    def _group_similar_events(self, events: List[CyberEvent]) -> List[List[CyberEvent]]:
        """Group events by similarity"""
        groups = []
        processed = set()
        total_events = len(events)

        self.logger.info(f"Grouping {total_events} events by similarity...")

        for i, event1 in enumerate(events):
            if i in processed:
                continue

            # Log progress every 100 events
            if i % 100 == 0:
                self.logger.info(f"Progress: {i}/{total_events} events processed ({i*100//total_events}%)")

            # Start a new group with this event
            group = [event1]
            processed.add(i)

            # Find similar events
            for j, event2 in enumerate(events[i+1:], i+1):
                if j in processed:
                    continue

                # RULE 1: Same entity + same date → ALWAYS merge (regardless of title)
                same_entity = self._same_entity(event1, event2)
                same_date = event1.event_date and event2.event_date and event1.event_date == event2.event_date

                if same_entity and same_date:
                    group.append(event2)
                    processed.add(j)
                    self.logger.debug(f"RULE 1: Merged same entity+date: {event1.victim_organization_name} on {event1.event_date}")
                    continue

                # Check for exact duplicates (same title and date - case insensitive)
                if (event1.title.lower().strip() == event2.title.lower().strip() and
                    event1.event_date == event2.event_date):
                    group.append(event2)
                    processed.add(j)
                    continue

                # RULE 2: Same entity + similar descriptions → MERGE (even if dates differ)
                if same_entity:
                    # For same entity, be MORE aggressive about merging (lower threshold)
                    # Check title/description similarity
                    title_sim = self._quick_title_similarity(event1.title, event2.title)

                    # If titles have any reasonable overlap for same entity, likely same incident
                    # Use very low threshold (0.15) since we're already confident it's the same organization
                    if title_sim >= 0.15:  # Very low threshold for same entity
                        group.append(event2)
                        processed.add(j)
                        self.logger.debug(f"RULE 2: Merged same entity+similar desc: {event1.victim_organization_name or event2.victim_organization_name or 'via title'} (title_sim={title_sim:.2f})")
                        continue

                    # RULE 2b: Same entity + low title similarity but high description similarity → MERGE
                    # This catches cases like Ticketmaster where titles are completely different
                    # but the scraped text clearly describes the same incident
                    if event1.description and event2.description:
                        desc_sim = self._quick_title_similarity(event1.description, event2.description)
                        # Use threshold of 0.20 for description similarity when same entity
                        if desc_sim >= 0.20:
                            group.append(event2)
                            processed.add(j)
                            self.logger.debug(f"RULE 2b: Merged same entity+similar content: {event1.victim_organization_name or event2.victim_organization_name or 'via title'} (title_sim={title_sim:.2f}, desc_sim={desc_sim:.2f})")
                            continue

                # RULE 3: Description-based similarity fallback (even when entity names missing/don't match)
                # This catches cases where entity extraction failed but the actual scraped content
                # clearly describes the same incident (e.g., one event has NULL organization name)
                if event1.description and event2.description:
                    desc_sim = self._quick_title_similarity(event1.description, event2.description)
                    # Use HIGHER threshold (0.35) when entities don't match to avoid false positives
                    # Also check that dates are reasonably close (within 90 days)
                    if desc_sim >= 0.35:
                        if event1.event_date and event2.event_date:
                            date_diff = abs((event1.event_date - event2.event_date).days)
                            if date_diff <= 90:  # Events within 3 months
                                group.append(event2)
                                processed.add(j)
                                entity_info = event1.victim_organization_name or event2.victim_organization_name or "unknown entity"
                                self.logger.debug(f"RULE 3: Merged via high description similarity: {entity_info} (desc_sim={desc_sim:.2f}, date_diff={date_diff}d)")
                                continue

                # Quick pre-filter: skip events with very different dates (>365 days apart)
                # BUT only if they're different entities
                if not same_entity and event1.event_date and event2.event_date:
                    date_diff = abs((event1.event_date - event2.event_date).days)
                    if date_diff > 365:
                        continue  # Skip detailed comparison

                # Quick pre-filter: check title similarity first (cheap operation)
                title_sim = self._quick_title_similarity(event1.title, event2.title)
                if title_sim < 0.3:  # Very different titles
                    continue  # Skip detailed comparison

                # Calculate full similarity for potentially similar events
                similarity = self._calculate_event_similarity(event1, event2)

                if similarity.overall_score >= self.similarity_threshold:
                    group.append(event2)
                    processed.add(j)

            groups.append(group)

        self.logger.info(f"Grouping complete: {total_events} events -> {len(groups)} groups")
        return groups

    def _normalize_entity_name(self, entity_name: Optional[str]) -> Optional[str]:
        """
        Normalize entity name using the entity mappings table.
        Maps subsidiary/brand names to their canonical parent entity.
        Returns the canonical name if a mapping exists, otherwise the original name.
        """
        if not entity_name:
            return entity_name

        name_lower = entity_name.lower().strip()

        # Check for exact match in mappings
        if name_lower in self.entity_mappings:
            canonical = self.entity_mappings[name_lower]
            self.logger.debug(f"Entity mapping applied: '{entity_name}' -> '{canonical}'")
            return canonical

        # Check for partial match (e.g., "Ticketmaster" matches "Ticketmaster LLC")
        for source, canonical in self.entity_mappings.items():
            if source in name_lower or name_lower in source:
                self.logger.debug(f"Entity mapping (partial) applied: '{entity_name}' -> '{canonical}'")
                return canonical

        return entity_name

    def _same_entity(self, event1: CyberEvent, event2: CyberEvent) -> bool:
        """
        Check if two events have the same victim organization.

        IMPORTANT: This is a KEY method for deduplication - events with the same victim
        organization are more likely to be the same incident (RULE 1 and RULE 2).

        Returns True if:
        - Both have victim_organization_name AND they match (case-insensitive)
        - Handles variations like "Ticketmaster LLC" vs "Live Nation Entertainment, Inc."
        - If organization names missing/don't match, checks if titles reference same company
        """
        # Normalize entity names using mappings (e.g., Ticketmaster -> Live Nation)
        name1_raw = event1.victim_organization_name
        name2_raw = event2.victim_organization_name
        name1_normalized = self._normalize_entity_name(name1_raw)
        name2_normalized = self._normalize_entity_name(name2_raw)

        # Try organization name matching (only if both have organization names)
        if name1_normalized and name2_normalized:
            name1 = name1_normalized.lower().strip()
            name2 = name2_normalized.lower().strip()

            # Exact match
            if name1 == name2:
                return True

            # Check if one name contains the other (e.g., "Ticketmaster LLC" vs "Ticketmaster")
            # But avoid matching "Bank" with "Commonwealth Bank"
            if len(name1) >= 5 and len(name2) >= 5:  # Avoid matching short generic words
                if name1 in name2 or name2 in name1:
                    return True

            # Check for known parent-subsidiary/related company relationships
            # E.g., Ticketmaster is owned by Live Nation
            related_companies = [
                {'ticketmaster', 'live nation'},
                # Add more related companies as needed
            ]

            for related_set in related_companies:
                # Check if both organizations match companies in the same related set
                name1_matches = any(company in name1 for company in related_set)
                name2_matches = any(company in name2 for company in related_set)
                if name1_matches and name2_matches:
                    self.logger.debug(
                        f"Matched related companies: '{event1.victim_organization_name}' "
                        f"and '{event2.victim_organization_name}'"
                    )
                    return True

        # FALLBACK: If organization names are NULL or don't match, check if titles
        # reference the same company (e.g., both mention "Ticketmaster")
        # This handles cases where enrichment failed to extract organization correctly
        # IMPORTANT: This runs even if one/both organization names are NULL
        if event1.title and event2.title:
            title1_lower = event1.title.lower()
            title2_lower = event2.title.lower()

            # Common company names to check for
            # Add more as needed for better matching
            # NOTE: Also add keywords from EntityMappings table (e.g., Nitro PDF -> Nitro Software)
            company_keywords = [
                'ticketmaster', 'live nation', 'medibank', 'optus', 'singtel',
                'latitude', 'myob', 'canva', 'woolworths', 'coles',
                'nitro', 'nitro pdf', 'nitro software',  # Nitro variants
                'qantas', 'telstra', 'commonwealth bank', 'westpac', 'nab', 'anz',
                'bunnings', 'kmart', 'target', 'myer', 'david jones',
                'toyota', 'mazda', 'ford', 'holden',
                'university', 'council', 'hospital', 'health',
            ]

            for keyword in company_keywords:
                if keyword in title1_lower and keyword in title2_lower:
                    self.logger.debug(
                        f"Matched entities via title keyword '{keyword}': "
                        f"'{event1.title}' and '{event2.title}'"
                    )
                    return True

        return False

    def _quick_title_similarity(self, title1: str, title2: str) -> float:
        """Fast title similarity check for pre-filtering"""
        if not title1 or not title2:
            return 0.0

        # Normalize
        t1 = title1.lower().strip()
        t2 = title2.lower().strip()

        # Exact match
        if t1 == t2:
            return 1.0

        # Quick word overlap check (faster than sequence matching)
        words1 = set(t1.split())
        words2 = set(t2.split())

        if not words1 or not words2:
            return 0.0

        # Jaccard similarity
        intersection = len(words1 & words2)
        union = len(words1 | words2)

        return intersection / union if union > 0 else 0.0

    def _calculate_event_similarity(self, event1: CyberEvent, event2: CyberEvent) -> SimilarityScore:
        """Calculate similarity between two events"""
        # Use shared similarity calculator instance
        similarity = self.similarity_calculator.calculate_similarity(event1, event2)
        
        # Use LLM arbiter if available and score is uncertain
        if self.llm_arbiter and 0.3 <= similarity.overall_score <= 0.7:
            arbiter_decision = self.llm_arbiter.decide_similarity(event1, event2, similarity.overall_score)
            
            # Adjust score based on arbiter decision
            if arbiter_decision.is_similar:
                similarity = SimilarityScore(
                    overall_score=max(similarity.overall_score, 0.8),
                    title_similarity=similarity.title_similarity,
                    entity_similarity=similarity.entity_similarity,
                    content_similarity=similarity.content_similarity,
                    temporal_similarity=similarity.temporal_similarity,
                    confidence=arbiter_decision.confidence,
                    reasoning=f"LLM arbiter: {arbiter_decision.reasoning}"
                )
            else:
                similarity = SimilarityScore(
                    overall_score=min(similarity.overall_score, 0.3),
                    title_similarity=similarity.title_similarity,
                    entity_similarity=similarity.entity_similarity,
                    content_similarity=similarity.content_similarity,
                    temporal_similarity=similarity.temporal_similarity,
                    confidence=arbiter_decision.confidence,
                    reasoning=f"LLM arbiter: {arbiter_decision.reasoning}"
                )
        
        return similarity
    
    def _merge_group(self, events: List[CyberEvent]) -> Tuple[CyberEvent, MergeGroup]:
        """Merge a group of similar events into one master event"""
        if len(events) == 1:
            return events[0], MergeGroup(
                master_event=events[0],
                merged_events=[],
                similarity_scores={},
                merge_reason="Single event",
                confidence=1.0
            )
        
        # Choose master event (most complete or most recent)
        master_event = self._select_master_event(events)
        merged_events = [e for e in events if e.event_id != master_event.event_id]
        
        # Merge data from all events
        merged_event = self._merge_event_data(events)
        
        # Calculate similarity scores
        similarity_scores = {}
        for event in merged_events:
            similarity = self._calculate_event_similarity(master_event, event)
            similarity_scores[event.event_id] = similarity.overall_score
        
        # Calculate average confidence
        avg_confidence = sum(similarity_scores.values()) / len(similarity_scores) if similarity_scores else 1.0
        
        merge_group = MergeGroup(
            master_event=merged_event,
            merged_events=merged_events,
            similarity_scores=similarity_scores,
            merge_reason=f"Merged {len(events)} similar events",
            confidence=avg_confidence
        )
        
        return merged_event, merge_group
    
    def _select_master_event(self, events: List[CyberEvent]) -> CyberEvent:
        """Select the best event to be the master"""
        # Score each event based on completeness and quality
        scored_events = []
        
        for event in events:
            score = 0
            
            # Prefer events with more complete data
            if event.summary and len(event.summary) > 50:
                score += 2
            if event.records_affected and event.records_affected > 0:
                score += 1
            if event.severity:
                score += 1
            if event.event_type:
                score += 1
            
            # Prefer more recent events
            if event.event_date:
                days_ago = (datetime.now().date() - event.event_date).days
                score += max(0, 1 - (days_ago / 365))  # Decay over time
            
            scored_events.append((score, event))
        
        # Return highest scoring event
        scored_events.sort(key=lambda x: x[0], reverse=True)
        return scored_events[0][1]
    
    def _merge_event_data(self, events: List[CyberEvent]) -> CyberEvent:
        """
        Merge data from multiple events into one.

        IMPORTANT: Uses EARLIEST date (RULE 2) - multiple news articles about the same
        incident may be published on different dates, but we want the original incident date.
        """
        master = events[0]  # Start with first event

        # Merge all unique data sources
        all_sources = set()
        for event in events:
            if hasattr(event, 'data_sources') and event.data_sources:
                all_sources.update(event.data_sources)

        # Merge all unique URLs
        all_urls = set()
        for event in events:
            if hasattr(event, 'urls') and event.urls:
                all_urls.update(event.urls)

        # Use the most complete summary
        best_summary = master.summary
        for event in events:
            if event.summary and len(event.summary) > len(best_summary or ""):
                best_summary = event.summary

        # Use the highest VALIDATED records affected
        # Apply reasonableness validation to prevent clearly incorrect values
        max_records = master.records_affected or 0
        for event in events:
            if event.records_affected and event.records_affected > max_records:
                max_records = event.records_affected

        # Validate the max_records value - use master title for context
        if max_records > 0:
            validated_records = validate_records_affected(max_records, master.title)
            if validated_records != max_records:
                self.logger.warning(
                    f"Merge: records_affected {max_records:,} rejected for '{master.title[:50]}' - "
                    f"using {validated_records if validated_records else 'NULL'}"
                )
                max_records = validated_records if validated_records else 0

        # RULE 2: Use the EARLIEST date from all merged events
        # This is the canonical incident date, not the news article publication date
        all_dates = [e.event_date for e in events if e.event_date is not None]
        if all_dates:
            earliest_date = min(all_dates)
            self.logger.debug(
                f"Selected earliest date {earliest_date} from {len(all_dates)} events "
                f"(dates: {sorted(all_dates)})"
            )
        else:
            earliest_date = master.event_date
            self.logger.debug("No dates available, using master event date")

        # Preserve the best victim_organization_name (first non-null value)
        # Normalize using entity mappings
        best_org_name = None
        best_org_industry = None
        for event in events:
            if event.victim_organization_name and not best_org_name:
                best_org_name = self._normalize_entity_name(event.victim_organization_name) or event.victim_organization_name
            if hasattr(event, 'victim_organization_industry') and event.victim_organization_industry and not best_org_industry:
                best_org_industry = event.victim_organization_industry

        # Create merged event
        merged_event = CyberEvent(
            event_id=master.event_id,  # Keep original ID
            title=master.title,
            summary=best_summary,
            event_date=earliest_date,  # CHANGED: Use earliest date instead of master
            event_type=master.event_type,
            severity=master.severity,
            records_affected=max_records if max_records > 0 else None,  # 0 becomes NULL
            victim_organization_name=best_org_name,
            victim_organization_industry=best_org_industry,
            data_sources=list(all_sources) if all_sources else master.data_sources,
            urls=list(all_urls) if all_urls else master.urls
        )

        return merged_event
