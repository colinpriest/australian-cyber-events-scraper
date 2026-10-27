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
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
import difflib
import hashlib

# Legal entity suffixes commonly trailing organisation names. We strip these
# during canonicalisation so "Latitude Financial Services Pty Ltd" matches
# "Latitude Financial Services Limited" and "Latitude Financial Services Holdings".
# Order matters: longer/more-specific phrases first so "pty limited" is consumed
# before "limited" alone.
_LEGAL_SUFFIX_TOKENS = (
    'pty ltd', 'pty. ltd.', 'pty limited', 'pty. limited',
    'pty', 'pty.',
    'limited', 'ltd', 'ltd.',
    'incorporated', 'inc', 'inc.',
    'l.l.c.', 'llc', 'llc.',
    'p.l.c.', 'plc', 'plc.',
    'corporation', 'corp', 'corp.',
    'company', 'co.', 'co',
    'holdings', 'holding', 'group',
    'gmbh', 'a.g.', 'ag',
    's.a.', 'sa',
    'n.v.', 'nv',
    'b.v.', 'bv',
    'international',
)
# Regex: one-or-more trailing suffix tokens, separated by whitespace/punctuation.
# Pre-compile once. We strip iteratively so chains like "Holdings Pty Ltd" all go.
_LEGAL_SUFFIX_RE = re.compile(
    r'(?:\s+|^)(?:' + '|'.join(re.escape(s) for s in _LEGAL_SUFFIX_TOKENS) + r')\.?$',
    flags=re.IGNORECASE,
)

# Hard list separators (always indicate a compound name). " and "/" & " are
# treated separately because they're ambiguous: ANZ's full legal name
# "Australia and New Zealand Banking Group Limited" contains " and " inside.
_COMPOUND_PRIMARY_SPLIT_RE = re.compile(r'\s*[,;|/]\s*')
# Pattern matching " and X" or " & X" at the start of a token (Oxford-comma
# tail). Used after a primary split when the trailing token starts with "and".
_LEADING_AND_RE = re.compile(r'^(?:and|&)\s+', flags=re.IGNORECASE)
# Pattern matching "X and Y" / "X & Y" inside a single token. Used to split
# the LAST primary token only (the "...NAB and Westpac" tail of an Oxford list,
# or a standalone two-org compound like "Apple & Microsoft").
_INNER_AND_RE = re.compile(r'^(.+?)\s+(?:and|&)\s+(.+)$', flags=re.IGNORECASE)

from tqdm import tqdm

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
from ..utils.llm_models import chat_params, openai_fast_model
from ..utils.validation import llm_validate_records_affected, validate_records_affected

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
    
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key
        self.model = model or openai_fast_model()
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
            self.logger.error(f"LLM arbiter failed: {e}, falling back to algorithmic score")
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
        """Call OpenAI API to evaluate event similarity."""
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key)
        response = client.chat.completions.create(
            **chat_params(self.model, max_tokens=200),
            messages=[
                {"role": "system", "content": "You are a cybersecurity analyst comparing breach reports. Respond only with valid JSON."},
                {"role": "user", "content": prompt},
            ],
        )
        # Track token usage
        from ..utils.token_tracker import tracker
        if response.usage:
            tracker.record(
                self.model, response.usage.prompt_tokens,
                response.usage.completion_tokens, context="dedup_arbiter",
            )
        return response.choices[0].message.content
    
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
        # Cached 'different incident' rulings (DedupOverrides, keyed on
        # enriched_event_id pairs). Every merge path consults them through
        # merge_decision, so a split cannot be undone by grouping or rebuild.
        self.different_pairs: Set[frozenset] = set()
    
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

        for i, event1 in tqdm(enumerate(events), total=total_events,
                              desc="Deduplicating events", unit="event", smoothing=0):
            if i in processed:
                continue

            # Start a new group with this event
            group = [event1]
            group_ids = {event1.event_id}
            processed.add(i)

            # Find events this one certainly duplicates. Only deterministic,
            # evidence-backed merges happen here (see merge_decision); anything
            # uncertain stays a separate event for the calibrated Jev duplicate
            # check (dedup/duplicate_judgement.py) that runs right after.
            for j, event2 in enumerate(events[i+1:], i+1):
                if j in processed:
                    continue
                reason = self.merge_decision(event1, event2, members1=group_ids)
                if reason:
                    group.append(event2)
                    group_ids.add(event2.event_id)
                    processed.add(j)
                    self.logger.debug(f"Merged ({reason}): {event1.title!r} + {event2.title!r}")

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

    @staticmethod
    def _canonicalize_entity(name: Optional[str]) -> str:
        """Single-name canonical form for cross-record matching.

        Handles three classes of pre-canonical noise the dedup engine has been
        seen to miss:

        1. Curly/smart-quotes vs straight quotes (NFKC + manual mapping).
           "Austin's Financial Solutions" == "Austin's Financial Solutions"

        2. Legal-suffix variants. "Latitude Financial Services Pty Ltd",
           "Latitude Financial Services Limited" and
           "Latitude Financial Services Australia Holdings Pty Ltd" all
           collapse to "latitude financial services".

        3. Trailing punctuation, multi-space runs, capitalisation drift.

        Returns a lowercase whitespace-collapsed token-bag string.
        Empty / None input returns "".
        """
        if not name:
            return ""

        # NFKC fixes most ligatures and most quote shapes, but not all dashes
        # and apostrophes. Map the remaining common ones explicitly.
        text = unicodedata.normalize('NFKC', name)
        text = (text
                .replace('‘', "'").replace('’', "'")
                .replace('“', '"').replace('”', '"')
                .replace('–', '-').replace('—', '-'))

        text = text.lower().strip()

        # Drop "(parenthetical descriptions)" e.g. "OracleCMS (multiple Victorian councils affected)"
        text = re.sub(r'\s*\([^)]*\)\s*', ' ', text)

        # Iteratively peel trailing legal suffixes: "...holdings pty ltd" needs
        # ltd, then pty, then holdings to be removed.
        prev = None
        while prev != text:
            prev = text
            text = _LEGAL_SUFFIX_RE.sub('', text).rstrip(' .,;')

        # Strip the geographic-suffix variant ("...Australia") once at end - it
        # commonly appears before legal suffixes ("Latitude Financial Services
        # Australia Holdings Pty Ltd") and is a hint, not part of the brand.
        # NEGATIVE lookbehind for " of " preserves names like
        # "Commonwealth Bank of Australia" where "Australia" IS part of the brand.
        text = re.sub(r'(?<!of)\s+(?:australia|australasia|asia[ -]pacific|apac|us|usa|uk|nz)$',
                      '', text, flags=re.IGNORECASE)

        # Collapse internal punctuation/whitespace.
        text = re.sub(r'[\s\-_,.]+', ' ', text).strip()
        return text

    @staticmethod
    def _split_compound_entity(name: Optional[str]) -> List[str]:
        """Split a compound name like "ANZ; CBA; NAB; Westpac" into parts.

        Two-stage split:
        1. Primary separators (``,;|/``) always split. These are unambiguous.
        2. ``and`` / ``&`` only split if either:
           * the LAST primary part starts with "and " (Oxford-comma list:
             ``A, B, and C`` -> ``[A, B, C]``); or
           * the LAST primary part contains an inner " and " / " & " AND BOTH
             sides are short standalone names (heuristic to handle
             ``A, B, C and D`` while preserving names like
             ``Australia and New Zealand Banking Group``); or
           * there are NO primary parts and the whole name is a short
             ``X & Y`` style two-org compound.

        Returns the original (single-element) list for non-compound names.
        """
        if not name:
            return []

        primary_parts = [p.strip() for p in _COMPOUND_PRIMARY_SPLIT_RE.split(name)
                         if p and p.strip()]

        if len(primary_parts) <= 1:
            # No hard list separators. Only treat the whole name as a compound
            # if it cleanly splits into TWO short names ("Apple & Microsoft").
            match = _INNER_AND_RE.match(name.strip())
            if match:
                left, right = match.group(1).strip(), match.group(2).strip()
                if 0 < len(left.split()) <= 2 and 0 < len(right.split()) <= 2:
                    return [left, right]
            return [name.strip()]

        # Multiple primary parts. Only the LAST may have an "and X" tail.
        last = primary_parts[-1]
        leading_and = _LEADING_AND_RE.match(last)
        if leading_and:
            primary_parts[-1] = last[leading_and.end():].strip()
        else:
            inner = _INNER_AND_RE.match(last)
            if inner:
                left, right = inner.group(1).strip(), inner.group(2).strip()
                # Only split if BOTH halves look like standalone short names.
                if 0 < len(left.split()) <= 4 and 0 < len(right.split()) <= 4:
                    primary_parts[-1] = left
                    primary_parts.append(right)
        return primary_parts

    # Generic single-word stems that should NEVER be treated as a brand hint -
    # they're too common across unrelated companies and would cause false matches.
    _GENERIC_WORD_STEMS = frozenset({
        'australia', 'australian', 'national', 'international', 'global',
        'commonwealth', 'group', 'holdings', 'industries', 'industry',
        'services', 'limited', 'company', 'corporate', 'corporation',
        'pacific', 'asian', 'european', 'american', 'state',
        'royal', 'general', 'united', 'central', 'regional',
    })

    def _entity_components(self, name: Optional[str], brand_stems: bool = True) -> Set[str]:
        """Return the set of canonical components that make up *name*.

        For "ANZ; CBA; NAB; Westpac" returns {"anz", "cba", "nab", "westpac"};
        for "Latitude Financial Services Pty Ltd" returns
        {"latitude financial services", "latitude"}; the extra single-word
        "brand-stem" entry lets bare "Latitude" match the longer form.

        Empty / None input returns an empty set.
        """
        if not name:
            return set()
        # Run entity_mappings normalisation first so e.g. "Ticketmaster LLC"
        # resolves to "Live Nation" before suffix stripping.
        normalized = self._normalize_entity_name(name) or name
        parts = self._split_compound_entity(normalized) or [normalized]
        canonicals = {self._canonicalize_entity(p) for p in parts}
        canonicals = {c for c in canonicals if c}
        if not brand_stems:
            return canonicals

        # Brand-stem hints: for each multi-word canonical name, also include
        # the first word as a synthetic component IF it is long enough (>=5 chars)
        # and not in the generic-word blocklist. This lets
        # "Finsure Finance & Insurance" match bare "Finsure" without doing
        # unsafe substring matching on every short token.
        brand_stems = set()
        for canonical in canonicals:
            words = canonical.split()
            if len(words) < 2:
                continue
            head = words[0]
            if len(head) >= 5 and head not in self._GENERIC_WORD_STEMS:
                brand_stems.add(head)
        return canonicals | brand_stems

    # Words that say what happened, not to whom: a title made only of these
    # ("Data breach", "Cyber attack update") identifies no incident.
    GENERIC_TITLE_WORDS = frozenset("""
        data breach breaches cyber cyberattack cyberattacks attack attacks incident incidents
        security ransomware hack hacked hackers hacking leak leaked leaks exposed exposure
        customer customers information personal systems system network outage phishing malware
        scam scams notice update updates statement australian australia the and for with from
        after into new latest major investigation investigating alleged confirms confirmed report
        reports reported affected impacted impacts compromise compromised unauthorised
        unauthorized access event events threat threats response media release news notification
        notifiable privacy hit hits suffers suffered says said amid over its their our your
        stolen theft breached targeted target warning warns
    """.split())

    def _title_identifies_something(self, title: Optional[str]) -> bool:
        tokens = re.findall(r"[a-z][a-z0-9'&.-]{2,}", (title or "").lower())
        return any(t.strip("'.-") not in self.GENERIC_TITLE_WORDS for t in tokens)

    def victim_compatibility(self, event1: CyberEvent, event2: CyberEvent) -> str:
        """'same', 'conflict' (both name a victim, different organisations) or 'unknown'."""
        if not event1.victim_organization_name or not event2.victim_organization_name:
            return "unknown"
        return "same" if self._same_entity(event1, event2) else "conflict"

    def ruled_different(self, members1: Iterable[str], members2: Iterable[str]) -> bool:
        """True when any record of one side has a cached 'different' ruling against the other."""
        if not self.different_pairs:
            return False
        members2 = set(members2)
        return any(frozenset((a, b)) in self.different_pairs for a in members1 for b in members2)

    def merge_decision(self, event1: CyberEvent, event2: CyberEvent,
                       members1: Optional[Iterable[str]] = None,
                       members2: Optional[Iterable[str]] = None) -> Optional[str]:
        """Why two events are certainly one incident, or None when that is not certain.

        Only two deterministic rules remain: an identical title on the same
        date, and the same organisation (by identity, not by shared words) on
        the same exact day. The fuzzy rules that used to run here merged events
        whose titles were 15% alike or whose descriptions were 20-35% alike - a
        reviewer reproduced different organisations merged because both titles
        contained "university", and one organisation's incidents years apart
        merged on title overlap. Uncertain pairs are now kept apart and decided
        by the calibrated Jev duplicate check, which sees both events'
        evidence, how each date was determined and the organisation's
        researched incident timeline.

        The 1st of a month is not treated as an exact day: it is how a date
        known only to the month is stored.

        The one decision every merge path uses (grouping new records, the full
        rebuild, attaching to existing events), with three vetoes first:
        a cached 'different' ruling between any record of either side
        (``members1`` / ``members2``, enriched_event_ids; default the events
        themselves), and conflicting victims. An identical title merges only
        when the victims agree, or neither is known and the title names
        something - two universities' events both titled "Data breach" on one
        day used to merge.
        """
        if not event1.event_date or event1.event_date != event2.event_date:
            return None
        if self.ruled_different(members1 or {event1.event_id}, members2 or {event2.event_id}):
            return None
        victims = self.victim_compatibility(event1, event2)
        if victims == "conflict":
            return None
        if (event1.title and event2.title
                and event1.title.lower().strip() == event2.title.lower().strip()):
            if victims == "same":
                return "identical title, date and victim"
            if (not event1.victim_organization_name and not event2.victim_organization_name
                    and self._title_identifies_something(event1.title)):
                return "identical specific title and date, no victim recorded"
        if event1.event_date.day != 1 and victims == "same":
            return "same organisation on the same day"
        return None

    def _same_entity(self, event1: CyberEvent, event2: CyberEvent) -> bool:
        """True when both events name the same victim organisation, by identity.

        Names are compared as organisations - legal-suffix and spelling
        variants, acronyms, known aliases - never by words they share: the old
        title-keyword fallback treated any two titles containing "university",
        "council", "hospital" or "health" as the same organisation. An event
        with no recorded victim matches nothing; it is not guessed from its
        title. Names stating different lines of business (Sharp Motor Group /
        Sharp Office Systems) are different organisations.
        """
        from cyber_data_collector.dedup.entity_merge import lines_of_business_conflict
        from cyber_data_collector.dedup.entity_resolution import EntityResolver

        name1 = self._normalize_entity_name(event1.victim_organization_name)
        name2 = self._normalize_entity_name(event2.victim_organization_name)
        if not name1 or not name2:
            return False
        if lines_of_business_conflict(name1, name2):
            return False
        if name1.lower().strip() == name2.lower().strip():
            return True
        # Compound names ("ANZ; CBA; NAB; Westpac" vs "ANZ, CBA, NAB, Westpac")
        # match when they share a whole canonical component. Not brand stems:
        # the first-word stem of "University of Sydney" is "university".
        components1 = self._entity_components(event1.victim_organization_name, brand_stems=False)
        components2 = self._entity_components(event2.victim_organization_name, brand_stems=False)
        if components1 and components2 and components1 & components2:
            return True
        if not hasattr(self, "_resolver"):
            self._resolver = EntityResolver()
        return self._resolver.same_organisation(name1, name2)

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

        # Validate the max_records value - use LLM fallback for uncertain cases
        if max_records > 0:
            import os
            validated_records, _ = llm_validate_records_affected(
                max_records, master.title,
                org_name=master.victim_organization_name,
                description=master.description or master.summary,
                perplexity_api_key=os.getenv('PERPLEXITY_API_KEY'),
            )
            if validated_records != max_records:
                # INFO: validator caught and corrected an LLM overshoot during
                # merge. The validator is doing its job; the user can't act on
                # individual rejections. Aggregate quality issues are surfaced
                # by separate phase-level sanity checks.
                self.logger.info(
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
