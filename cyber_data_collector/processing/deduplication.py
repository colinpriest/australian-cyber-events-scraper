from __future__ import annotations

import logging
import os
from difflib import SequenceMatcher
from typing import Any, Dict, List

from cyber_data_collector.models.events import AffectedEntity, CyberEvent


class DeduplicationEngine:
    """Intelligent event deduplication system."""

    def __init__(self, perplexity_arbiter=None) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        self.entity_similarity_threshold = 0.7  # Lowered from 0.8 for better fuzzy matching
        self.date_tolerance_days = None  # Removed hard limit - use as scoring factor instead
        self.perplexity_arbiter = perplexity_arbiter  # Optional Perplexity arbiter for uncertain cases

    async def deduplicate_events(self, events: List[CyberEvent]) -> List[CyberEvent]:
        """Deduplicate events while preserving all source information."""

        self.logger.info("Deduplicating %s events", len(events))

        # Debug: Check event_date values in input events
        events_with_dates = 0
        events_without_dates = 0
        for event in events:
            if event.event_date:
                events_with_dates += 1
                self.logger.debug(f"Input event has date: '{event.title[:30]}...' - {event.event_date}")
            else:
                events_without_dates += 1
                self.logger.debug(f"Input event missing date: '{event.title[:30]}...'")

        self.logger.info(f"Input events: {events_with_dates} with dates, {events_without_dates} without dates")
        event_groups = self._group_similar_events(events)
        deduplicated_events: List[CyberEvent] = []
        for group in event_groups:
            if len(group) == 1:
                # Single event - ensure contributing counts are set correctly
                single_event = group[0]
                single_event.contributing_raw_events = 1
                single_event.contributing_enriched_events = 1
                deduplicated_events.append(single_event)
            else:
                merged_event = self._merge_event_group(group)
                deduplicated_events.append(merged_event)
        # Debug: Check output events
        output_with_dates = 0
        output_without_dates = 0
        for event in deduplicated_events:
            if event.event_date:
                output_with_dates += 1
                self.logger.debug(f"Output event has date: '{event.title[:30]}...' - {event.event_date}")
            else:
                output_without_dates += 1
                self.logger.warning(f"Output event missing date: '{event.title[:30]}...'")

        self.logger.info(f"Output events: {output_with_dates} with dates, {output_without_dates} without dates")
        self.logger.info("Deduplicated to %s unique events", len(deduplicated_events))
        return deduplicated_events

    def _group_similar_events(self, events: List[CyberEvent]) -> List[List[CyberEvent]]:
        groups: List[List[CyberEvent]] = []
        processed_indices = set()

        for index, event in enumerate(events):
            if index in processed_indices:
                continue

            group = [event]
            processed_indices.add(index)

            for other_index in range(index + 1, len(events)):
                if other_index in processed_indices:
                    continue

                if self._are_events_similar(event, events[other_index]):
                    group.append(events[other_index])
                    processed_indices.add(other_index)

            groups.append(group)

        return groups

    def _are_events_similar(self, event1: CyberEvent, event2: CyberEvent) -> bool:
        # Entity similarity check is crucial. If we can't confirm the entities are similar, the events are not.
        entity1_name = None
        if event1.primary_entity:
            entity1_name = event1.primary_entity.name
        else:
            entity1_name = self._extract_entity_from_title(event1.title)

        entity2_name = None
        if event2.primary_entity:
            entity2_name = event2.primary_entity.name
        else:
            entity2_name = self._extract_entity_from_title(event2.title)

        self.logger.debug(f"[ENTITY COMPARISON] Event1: '{event1.title[:50]}...' -> Entity: '{entity1_name}'")
        self.logger.debug(f"[ENTITY COMPARISON] Event2: '{event2.title[:50]}...' -> Entity: '{entity2_name}'")

        # Special debug for identical titles
        if event1.title == event2.title:
            if entity1_name != entity2_name:
                self.logger.warning(f"[IDENTICAL TITLES] Found events with identical titles but different entities: '{event1.title}'")
                self.logger.warning(f"[IDENTICAL TITLES] Event1 entity: '{entity1_name}', Event2 entity: '{entity2_name}'")
            else:
                self.logger.debug(f"[IDENTICAL TITLES] Found events with identical titles and same entities: '{event1.title}' (entity: '{entity1_name}')")

        # Special case: if titles are identical, skip entity checking and proceed to content similarity
        if event1.title == event2.title:
            self.logger.debug(f"[IDENTICAL TITLES] Skipping entity check for identical titles: '{event1.title[:50]}...'")
            entity_similarity = 1.0  # Assume perfect entity match for identical titles
        elif entity1_name and entity2_name:
            entity_similarity = self._calculate_entity_similarity(entity1_name, entity2_name)
            self.logger.debug(f"[ENTITY SIMILARITY] '{entity1_name}' vs '{entity2_name}' = {entity_similarity:.3f}")
            if entity_similarity < self.entity_similarity_threshold:
                self.logger.debug(f"[ENTITY REJECT] Entities too different ({entity_similarity:.3f} < {self.entity_similarity_threshold})")
                return False  # Entities are different, so events are different.
        else:
            # If we can't identify an entity in one or both titles, we can't be sure they are the same.
            # It's safer to treat them as different events to avoid incorrect merging.
            self.logger.debug(f"[ENTITY REJECT] Missing entity names - Entity1: '{entity1_name}', Entity2: '{entity2_name}'")
            return False

        # If entities are similar enough, proceed with more detailed content similarity checks.
        self.logger.debug(f"[ENTITY PASS] Entities similar enough ({entity_similarity:.3f} >= {self.entity_similarity_threshold}), checking content similarity")
        result = self._check_cyber_event_similarity(event1, event2)
        self.logger.debug(f"[SIMILARITY RESULT] Final result: {result}")
        return result

    def _extract_entity_from_title(self, title: str) -> str:
        """Extract organization name from title."""
        import re
        # Common patterns for organization names in cyber event titles
        patterns = [
            r'^([^:]+(?:Inc|Corp|Ltd|Limited|Company|Corp|LLC|Pty|Group|Bank|Insurance|University|College|Hospital|Health|Airways|Air|Telecom))\s*[:\-\s]',
            r'^([A-Z][a-zA-Z\s&]+?)\s+(?:suffers?|confirms?|experiences?|reports?|admits?|reveals?|discloses?|investigates?)',
            r'^([A-Z][a-zA-Z\s&]+?)\s+(?:cyber|data\s+breach|hack|attack|incident)',
            r'^([A-Z][a-zA-Z\s&]+?)\s+(?:hit|struck|targeted|affected|impacted)',
            # Special pattern for "X held to ransom" format
            r'^([A-Z][a-zA-Z\s&]+?)\s+held\s+to\s+ransom',
            # Pattern for "X tight-lipped" format
            r'^([A-Z][a-zA-Z\s&]+?)\s+tight-lipped',
            # Pattern for "X shuts" format
            r'^([A-Z][a-zA-Z\s&]+?)\s+shuts?',
            # Pattern for "Ransomware Attack on X" format
            r'(?:Ransomware\s+Attack\s+on|Attack\s+on)\s+([A-Z][a-zA-Z\s&]+?)(?:\s|$)',
            # Pattern for "The X hack" format
            r'The\s+([A-Z][a-zA-Z\s&]+?)\s+hack',
            # Pattern for "X members compromised" format (for Defence Force, etc.)
            r'(?:details\s+of\s+|private\s+details\s+of\s+)?([A-Z][a-zA-Z\s&]+?\s+(?:Force|Forces|Department|Ministry|Agency|Service))\s+members?\s+compromised',
            # Pattern for incidents affecting organization members/employees/customers
            r'(?:details\s+of\s+)?([A-Z][a-zA-Z\s&]+?)\s+(?:members?|employees?|customers?|staff|personnel)\s+(?:compromised|affected|exposed)',
            # Pattern for "data breach involving X" format
            r'(?:data\s+breach|breach|incident)\s+involving\s+([A-Z][a-zA-Z\s&]+?)(?:\s|,|\.|\bin\b)',
            # Pattern for "X data breach" format
            r'\b([A-Z][a-zA-Z\s&]+?)\s+(?:data\s+breach|breach|cyber\s+attack|attack)',
            # Pattern for "X facing class action" format
            r'^([A-Z][a-zA-Z\s&]+?)\s+facing\s+(?:class\s+action|lawsuit|legal\s+action)',
            r'([A-Z][a-zA-Z\s&]+?)\s+(?:Contact\s+Centre|Call\s+Center|Contact\s+Center)',
            # Pattern for "Company Security Incident" format
            r'^([A-Z][a-zA-Z\s&]+?)\s+(?:Security\s+Incident|Data\s+Breach|Cyber\s+Attack|Privacy\s+Incident)',
            # Pattern for titles that start with company name followed by dash/colon
            r'^([A-Z][a-zA-Z\s&]{2,15}?)\s*[:\-–]\s*(?:Security|Data|Breach|Incident|Cyber|Privacy|FAQ)',
        ]

        for pattern in patterns:
            match = re.search(pattern, title, re.IGNORECASE)
            if match:
                entity = match.group(1).strip()
                # Clean up common prefixes/suffixes
                entity = re.sub(r'^(Exclusive|Breaking):\s*', '', entity, flags=re.IGNORECASE)
                return entity

        # More conservative fallback: only use if it clearly looks like an organization name
        words = title.split()
        if len(words) >= 2:
            # Look for obvious organizational indicators
            org_indicators = ['inc', 'corp', 'ltd', 'limited', 'company', 'llc', 'pty',
                            'group', 'bank', 'insurance', 'university', 'college',
                            'hospital', 'health', 'airways', 'telecom', 'technologies']

            # Check first 4 words for org indicators
            first_words = ' '.join(words[:4]).lower()
            if any(indicator in first_words for indicator in org_indicators):
                # Find the span that includes the org indicator
                for i in range(min(4, len(words))):
                    if any(indicator in words[i].lower() for indicator in org_indicators):
                        return ' '.join(words[:i+1])

            # Very conservative fallback: only if first word is clearly a proper noun
            # and second word suggests it's an organization
            if (words[0][0].isupper() and len(words[0]) > 2 and
                len(words) >= 2 and words[1][0].isupper() and len(words[1]) > 2):
                return ' '.join(words[:2])

        return ""

    def _calculate_entity_similarity(self, entity1: str, entity2: str) -> float:
        """Calculate entity similarity with special handling for common variations, acronyms, and known aliases."""
        entity1_lower = entity1.lower().strip()
        entity2_lower = entity2.lower().strip()

        # Exact match
        if entity1_lower == entity2_lower:
            return 1.0

        # Acronym matching (e.g., "FBI" vs "Federal Bureau of Investigation")
        acronym_similarity = self._check_acronym_match(entity1_lower, entity2_lower)
        if acronym_similarity > 0.9:
            self.logger.debug(f"[ACRONYM MATCH] '{entity1}' <-> '{entity2}' = {acronym_similarity:.3f}")
            return acronym_similarity

        # Common abbreviations and variations
        common_variations = {
            'boa': 'bank of america',
            'bofa': 'bank of america',
            'jpmc': 'jpmorgan chase',
            'jpm': 'jpmorgan',
            'anz': 'australia and new zealand banking group',
            'nab': 'national australia bank',
            'cba': 'commonwealth bank',
            'westpac': 'westpac banking corporation',
        }

        # Check if either entity is a known abbreviation
        for abbrev, full_name in common_variations.items():
            if (abbrev in entity1_lower and full_name in entity2_lower) or \
               (abbrev in entity2_lower and full_name in entity1_lower):
                self.logger.debug(f"[VARIATION MATCH] '{entity1}' <-> '{entity2}' via '{abbrev}'")
                return 0.95

        # Handle common entity variations
        # Remove common suffixes/variations
        def normalize_entity(name):
            name = name.lower().strip()
            # Remove common organizational suffixes
            suffixes = ['group', 'company', 'corp', 'corporation', 'inc', 'incorporated',
                       'ltd', 'limited', 'llc', 'pty', 'bank', 'insurance', 'holding', 'holdings']
            words = name.split()

            # Remove trailing suffixes
            while words and words[-1] in suffixes:
                words.pop()

            # Also try removing leading/middle suffixes for cases like "Group X" vs "X"
            filtered_words = [w for w in words if w not in suffixes]

            return ' '.join(filtered_words) if filtered_words else ' '.join(words)

        normalized1 = normalize_entity(entity1_lower)
        normalized2 = normalize_entity(entity2_lower)

        # Check if one is a subset of the other (e.g., "Toll" vs "Toll Group")
        if normalized1 in normalized2 or normalized2 in normalized1:
            return 0.95  # Very high similarity for subset matches

        # Check if normalized versions are the same
        if normalized1 == normalized2:
            return 0.95

        # Use sequence matcher on normalized names
        normalized_similarity = SequenceMatcher(None, normalized1, normalized2).ratio()

        # Also check original names
        original_similarity = SequenceMatcher(None, entity1_lower, entity2_lower).ratio()

        # Return the higher of the two similarities
        return max(normalized_similarity, original_similarity)

    def _check_acronym_match(self, name1: str, name2: str) -> float:
        """Check if one name is an acronym of the other."""
        def get_acronym(text: str) -> str:
            """Generate acronym from text."""
            words = text.split()
            # Skip very short words (articles, prepositions)
            skip_words = {'of', 'the', 'and', 'for', 'in', 'on', 'at', 'to', 'a', 'an'}
            significant_words = [w for w in words if w not in skip_words and len(w) > 1]
            return ''.join(w[0] for w in significant_words)

        # Check if one is short (likely acronym) and other is long (likely full name)
        if len(name1) <= 5 and len(name2) > 10:
            acronym2 = get_acronym(name2)
            if name1.replace(' ', '') == acronym2:
                return 0.98
        elif len(name2) <= 5 and len(name1) > 10:
            acronym1 = get_acronym(name1)
            if name2.replace(' ', '') == acronym1:
                return 0.98

        return 0.0

    def _are_both_generic_summaries(self, event1: CyberEvent, event2: CyberEvent) -> bool:
        """Check if both events are generic summaries that should be merged."""
        import re

        title1 = event1.title.lower()
        title2 = event2.title.lower()
        desc1 = event1.description.lower()[:200] if event1.description else ""
        desc2 = event2.description.lower()[:200] if event2.description else ""

        # Patterns that indicate generic summaries
        generic_patterns = [
            r'\b(?:multiple|several|various)\b.*\b(?:breach|attack|incident)',
            r'\b(?:australian|australia)\b.*\b(?:data\s+breach|breach|attack|incident|cyber)',
            r'\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\b.*\b(?:202\d|201\d)\b',
            r'\b(?:q1|q2|q3|q4)\b.*\b(?:202\d|201\d)\b',
            r'\b(?:202\d|201\d)\b.*\b(?:breach|attack|incident|cyber)',
            r'\b(?:office.*australian.*information.*commissioner|oaic)\b.*\b(?:report|notification)',
            r'\b(?:covid|coronavirus)\b.*\b(?:themed|campaign|activity|cyber|phishing)',
            r'\b(?:phishing\s+campaign|malicious\s+cyber\s+activity)\b.*\b(?:targeting|australian)',
            r'\b(?:\d+)\s+(?:cybercrime|cyber\s+security|incident)\s+reports?\b',
        ]

        # Check if both titles match generic patterns
        title1_is_generic = any(re.search(pattern, title1) for pattern in generic_patterns)
        title2_is_generic = any(re.search(pattern, title2) for pattern in generic_patterns)

        if not (title1_is_generic and title2_is_generic):
            return False

        # If both are generic, check for common elements that suggest they're about the same thing
        common_elements = [
            ('january', 'jan'), ('february', 'feb'), ('march', 'mar'),
            ('april', 'apr'), ('may', 'may'), ('june', 'jun'),
            ('july', 'jul'), ('august', 'aug'), ('september', 'sep'),
            ('october', 'oct'), ('november', 'nov'), ('december', 'dec'),
            '2020', '2021', '2022', '2023', '2024', '2025',
            'australia', 'australian', 'data breach', 'ransomware', 'malware',
            'oaic', 'commissioner', 'multiple', 'several', 'various',
            ('covid', 'coronavirus'), 'phishing', 'campaign', 'themed',
            'cyber activity', 'malicious', 'targeting', 'acsc', 'reports'
        ]

        # Check for overlap in key terms
        common_terms_count = 0
        for element in common_elements:
            if isinstance(element, tuple):
                # Handle month abbreviations
                if any(term in title1 or term in desc1 for term in element) and \
                   any(term in title2 or term in desc2 for term in element):
                    common_terms_count += 1
            else:
                if (element in title1 or element in desc1) and (element in title2 or element in desc2):
                    common_terms_count += 1

        # If they share multiple common terms, they're likely the same generic summary
        if common_terms_count >= 3:
            self.logger.debug(f"[GENERIC MATCH] Found {common_terms_count} common terms between generic summaries")
            return True

        return False

    def _check_same_company_different_incidents(self, event1: CyberEvent, event2: CyberEvent) -> bool:
        """Check if these are different incidents for the same company."""

        # Extract financial impact (customers affected) if available
        customers1 = None
        customers2 = None

        if hasattr(event1, 'financial_impact') and event1.financial_impact and hasattr(event1.financial_impact, 'customers_affected'):
            customers1 = event1.financial_impact.customers_affected
        if hasattr(event2, 'financial_impact') and event2.financial_impact and hasattr(event2.financial_impact, 'customers_affected'):
            customers2 = event2.financial_impact.customers_affected

        # If both have customer counts and they're very different, check if they're genuinely different incidents
        if customers1 and customers2 and customers1 != customers2:
            ratio = max(customers1, customers2) / min(customers1, customers2)
            if ratio > 10:  # More than 10x difference (e.g., 50K vs 1M)
                # Check if descriptions suggest they're actually different incidents (different attack methods/times)
                desc1 = event1.description.lower() if event1.description else ""
                desc2 = event2.description.lower() if event2.description else ""

                # Look for indicators of genuinely different incidents (different attack methods and clearly different timeframes)
                clearly_different_indicators = [
                    # Only count as different if they have different attack methods AND different years
                    ('white pages', 'api', '2020', '2022'),  # White Pages 2020 vs API 2022
                    ('directory', 'coding error', '2020', '2022'),  # Directory 2020 vs Coding error 2022
                ]

                for method1, method2, time1, time2 in clearly_different_indicators:
                    # Must have both different methods AND different timeframes to be considered separate
                    if ((method1 in desc1 and method2 in desc2 and time1 in desc1 and time2 in desc2) or
                        (method2 in desc1 and method1 in desc2 and time2 in desc1 and time1 in desc2)):
                        self.logger.debug(f"[DIFFERENT INCIDENTS] Found clearly different incidents: {method1}/{time1} vs {method2}/{time2}")
                        return True

                # If just different numbers but similar attack methods/timeframes, treat as updates to same incident
                self.logger.debug(f"[INCIDENT UPDATES] Different customer counts ({customers1} vs {customers2}) but likely same incident with updated scope")

        return False

    def _check_incident_updates(self, event1: CyberEvent, event2: CyberEvent) -> float:
        """Check if these events are updates to the same incident with revised impact numbers."""

        # Extract customer counts
        customers1 = None
        customers2 = None

        if hasattr(event1, 'financial_impact') and event1.financial_impact and hasattr(event1.financial_impact, 'customers_affected'):
            customers1 = event1.financial_impact.customers_affected
        if hasattr(event2, 'financial_impact') and event2.financial_impact and hasattr(event2.financial_impact, 'customers_affected'):
            customers2 = event2.financial_impact.customers_affected

        # If we have different customer counts, check if they're updates to same incident
        if customers1 and customers2 and customers1 != customers2:
            # Look for common incident characteristics
            desc1 = event1.description.lower() if event1.description else ""
            desc2 = event2.description.lower() if event2.description else ""

            # Common incident indicators (same company, similar attack method/timeframe)
            common_incident_indicators = [
                'optus', 'medibank', 'canva', 'toll', 'telstra',  # Same company
                'data breach', 'cyber attack', 'security incident',  # Same type
                'customers', 'personal details', 'compromised',  # Same impact type
            ]

            # Count common indicators
            common_count = 0
            for indicator in common_incident_indicators:
                if indicator in desc1 and indicator in desc2:
                    common_count += 1

            # If they share many common indicators but have different customer counts, likely updates
            if common_count >= 4:
                # Higher customer count is likely the updated/final count
                ratio = max(customers1, customers2) / min(customers1, customers2)
                if 2 <= ratio <= 50:  # Reasonable update range (2x to 50x - common in incident updates)
                    self.logger.debug(f"[INCIDENT UPDATE] Detected incident update: {customers1} vs {customers2} customers ({common_count} common indicators)")
                    return 0.9  # High similarity boost for incident updates

        return 0.0

    def _check_cyber_event_similarity(self, event1: CyberEvent, event2: CyberEvent) -> bool:
        """Enhanced similarity checking specifically for cyber events."""

        # Special check for generic summary reports
        if self._are_both_generic_summaries(event1, event2):
            self.logger.debug(f"[GENERIC SUMMARIES] Both events appear to be generic summaries of the same period/type")
            return True

        # Calculate various similarity metrics
        title_similarity = SequenceMatcher(None, event1.title.lower(), event2.title.lower()).ratio()

        # Enhanced title similarity for truncated titles
        title1_words = set(event1.title.lower().split())
        title2_words = set(event2.title.lower().split())

        # Check if one title is a truncated version of another (common pattern)
        if len(title1_words) != len(title2_words):
            shorter_words = title1_words if len(title1_words) < len(title2_words) else title2_words
            longer_words = title2_words if len(title1_words) < len(title2_words) else title1_words

            # If shorter title's words are all contained in longer title, it's likely truncated
            if shorter_words.issubset(longer_words):
                truncation_similarity = len(shorter_words) / len(longer_words)
                if truncation_similarity > 0.7:  # At least 70% of longer title is in shorter
                    self.logger.debug(f"[TRUNCATION] Detected truncated title similarity: {truncation_similarity:.3f}")
                    title_similarity = max(title_similarity, 0.9)  # Boost similarity for truncated matches

        # Check for titles that start very similarly (common for same incidents)
        title1_lower = event1.title.lower()
        title2_lower = event2.title.lower()
        min_length = min(len(title1_lower), len(title2_lower))

        if min_length > 20:  # Only for reasonably long titles
            # Check how much of the beginning matches
            prefix_similarity = SequenceMatcher(None, title1_lower[:min_length], title2_lower[:min_length]).ratio()
            if prefix_similarity > 0.8:  # Strong match in the beginning
                self.logger.debug(f"[PREFIX] Strong title prefix similarity: {prefix_similarity:.3f}")
                title_similarity = max(title_similarity, prefix_similarity)

        desc_similarity = SequenceMatcher(
            None,
            event1.description.lower()[:300],
            event2.description.lower()[:300],
        ).ratio()

        # Enhanced description similarity for similar incidents
        if desc_similarity > 0.3:  # Only if there's some baseline similarity
            # Extract key terms from descriptions to check for semantic overlap
            desc1_words = set(event1.description.lower().split()) if event1.description else set()
            desc2_words = set(event2.description.lower().split()) if event2.description else set()

            # Check for overlap in significant terms
            key_terms = {'optus', 'telstra', 'medibank', 'canva', 'toll', 'defence', 'white', 'pages',
                        'directory', 'sensis', 'customers', 'personal', 'details', 'disclosed',
                        'published', 'breach', 'data', 'compromised', 'names', 'addresses',
                        'phone', 'numbers', 'unlisted', 'api', 'coding', 'error'}

            desc1_key_terms = desc1_words.intersection(key_terms)
            desc2_key_terms = desc2_words.intersection(key_terms)
            common_key_terms = desc1_key_terms.intersection(desc2_key_terms)

            if len(common_key_terms) >= 4:  # Significant overlap in key terms
                key_term_boost = min(len(common_key_terms) / 10.0, 0.3)  # Up to 0.3 boost
                desc_similarity = min(desc_similarity + key_term_boost, 1.0)
                self.logger.debug(f"[DESC BOOST] Found {len(common_key_terms)} common key terms, boosted desc similarity to {desc_similarity:.3f}")

        # Extract key terms for semantic similarity
        key_terms_sim = self._calculate_key_terms_similarity(event1, event2)

        # Date proximity factor - be more lenient for identical titles
        date_factor = self._calculate_date_factor(event1, event2)
        if title_similarity == 1.0:  # Identical titles should not be heavily penalized for missing dates
            date_factor = max(date_factor, 0.95)
            self.logger.debug(f"Boosted date_factor to {date_factor:.3f} for identical titles")

        # Event type similarity
        type_factor = 1.0 if event1.event_type == event2.event_type else 0.7

        # Special check for same company but different breach scales
        same_company_different_scale = self._check_same_company_different_incidents(event1, event2)
        if same_company_different_scale:
            self.logger.debug(f"[DIFFERENT INCIDENTS] Same company but likely different incidents (different scales)")
            return False

        # Check for incident updates (same company, different customer counts but similar timeframe/method)
        incident_update_boost = self._check_incident_updates(event1, event2)

        # Check for strong indicators of the same incident
        strong_indicators = self._check_strong_incident_indicators(event1, event2)

        # Apply incident update boost
        if incident_update_boost > 0:
            strong_indicators = max(strong_indicators, incident_update_boost)

        # If strong indicators suggest same incident, use a more lenient approach
        if strong_indicators >= 0.8:
            # Weighted similarity with emphasis on key terms and strong indicators
            weighted_similarity = (
                title_similarity * 0.2 +
                max(desc_similarity, 0.3) * 0.1 +  # Don't penalize too much for different description styles
                key_terms_sim * 0.5 +
                strong_indicators * 0.2
            ) * date_factor

            threshold = 0.6  # Lower threshold for strong indicator cases
        else:
            # Standard weighted similarity calculation
            weighted_similarity = (
                title_similarity * 0.3 +
                desc_similarity * 0.2 +
                key_terms_sim * 0.4 +
                type_factor * 0.1
            ) * date_factor

            threshold = 0.7  # Standard threshold

        self.logger.debug(f"Similarity metrics - Title: {title_similarity:.3f}, Desc: {desc_similarity:.3f}, "
                         f"KeyTerms: {key_terms_sim:.3f}, StrongIndicators: {strong_indicators:.3f}, "
                         f"Date: {date_factor:.3f}, Weighted: {weighted_similarity:.3f}, Threshold: {threshold:.3f}")

        # If algorithmic similarity is uncertain (0.50-0.85 range), use arbiter for final decision
        if 0.50 <= weighted_similarity < 0.85:
            self.logger.debug(f"Similarity uncertain ({weighted_similarity:.3f}), checking with arbiter")

            # Try Perplexity arbiter first (more reliable for uncertain cases)
            if self.perplexity_arbiter:
                try:
                    import asyncio
                    loop = asyncio.get_event_loop()
                    perplexity_check = loop.run_until_complete(
                        self.perplexity_arbiter.check_duplicate(
                            event1.title,
                            event1.description,
                            str(event1.event_date) if event1.event_date else None,
                            event1.primary_entity.name if event1.primary_entity else None,
                            event2.title,
                            event2.description,
                            str(event2.event_date) if event2.event_date else None,
                            event2.primary_entity.name if event2.primary_entity else None
                        )
                    )

                    if perplexity_check and perplexity_check.confidence >= 0.7:
                        decision = perplexity_check.are_same_incident
                        self.logger.info(
                            f"Perplexity arbiter: {'SAME' if decision else 'DIFFERENT'} "
                            f"(confidence: {perplexity_check.confidence:.2f}, "
                            f"reasoning: {perplexity_check.reasoning[:100]}...)"
                        )
                        return decision
                except Exception as e:
                    self.logger.warning(f"Perplexity arbiter failed: {e}, falling back to LLM")

            # Fallback to LLM arbiter if Perplexity unavailable/failed
            if weighted_similarity >= 0.6:  # Only use LLM for closer matches
                llm_decision = self._llm_similarity_check(event1, event2)
                if llm_decision:
                    self.logger.info(f"LLM override: Events deemed similar despite algorithmic score of {weighted_similarity:.3f}")
                    return True

        return weighted_similarity >= threshold

    def _check_strong_incident_indicators(self, event1: CyberEvent, event2: CyberEvent) -> float:
        """Check for strong indicators that suggest the same incident."""
        import re

        text1 = (event1.title + " " + event1.description).lower()
        text2 = (event2.title + " " + event2.description).lower()

        indicators = []

        # Same specific platform/system mentioned
        specific_systems = [
            r'(?:contact\s+centre|call\s+center|contact\s+center)',
            r'(?:third[- ]party\s+platform)',
            r'(?:airline\s+contact\s+centre)'
        ]

        for pattern in specific_systems:
            if re.search(pattern, text1) and re.search(pattern, text2):
                indicators.append(0.3)  # High weight for specific system matches

        # Same date mentioned in content (even if event_date differs)
        date_mentions1 = re.findall(r'june\s+30|30\s+june|june\s+1|1\s+june', text1)
        date_mentions2 = re.findall(r'june\s+30|30\s+june|june\s+1|1\s+june', text2)

        if date_mentions1 and date_mentions2:
            # Check if any date matches
            common_dates = set(date_mentions1) & set(date_mentions2)
            if common_dates:
                indicators.append(0.4)  # Very high weight for same date mentioned

        # Same specific data types compromised
        data_types = [
            r'email\s+addresses?',
            r'phone\s+numbers?',
            r'frequent\s+flyer',
            r'birth\s+dates?',
            r'customer\s+records?'
        ]

        data_matches = 0
        for pattern in data_types:
            if re.search(pattern, text1) and re.search(pattern, text2):
                data_matches += 1

        if data_matches >= 2:
            indicators.append(0.3)  # Multiple data types match

        # Same detection method/timeline
        detection_patterns = [
            r'unusual\s+activity\s+detected',
            r'detected\s+unusual\s+activity'
        ]

        for pattern in detection_patterns:
            if re.search(pattern, text1) and re.search(pattern, text2):
                indicators.append(0.2)

        # Same threat actor or attack method
        threat_patterns = [
            r'scattered\s+spider',
            r'phishing',
            r'social\s+engineering',
            r'mfa\s+bombing'
        ]

        for pattern in threat_patterns:
            if re.search(pattern, text1) and re.search(pattern, text2):
                indicators.append(0.3)

        return min(sum(indicators), 1.0)  # Cap at 1.0

    def _calculate_key_terms_similarity(self, event1: CyberEvent, event2: CyberEvent) -> float:
        """Calculate similarity based on key cyber security terms."""
        import re

        # Extract key terms from both events
        text1 = (event1.title + " " + event1.description).lower()
        text2 = (event2.title + " " + event2.description).lower()

        # Key terms that indicate the same incident
        key_patterns = [
            r'(?:contact\s+centre|call\s+center|contact\s+center)',
            r'(?:third[- ]party|3rd[- ]party)',
            r'(?:platform|system|service)',
            r'(?:unusual\s+activity|suspicious\s+activity)',
            r'(?:personal\s+data|customer\s+data|sensitive\s+data)',
            r'(?:email\s+addresses?|phone\s+numbers?|frequent\s+flyer)',
            r'(?:ransomware|phishing|malware|breach|hack|attack)',
            r'(?:scattered\s+spider|killsec|qilin|akira)'  # Known threat actors
        ]

        matches1 = set()
        matches2 = set()

        for pattern in key_patterns:
            if re.search(pattern, text1):
                matches1.add(pattern)
            if re.search(pattern, text2):
                matches2.add(pattern)

        if not matches1 and not matches2:
            return 0.0

        # Calculate Jaccard similarity
        intersection = len(matches1.intersection(matches2))
        union = len(matches1.union(matches2))

        return intersection / union if union > 0 else 0.0

    def _calculate_date_factor(self, event1: CyberEvent, event2: CyberEvent) -> float:
        """Calculate date proximity factor for similarity weighting.

        Note: Removed hard date cutoff. Many breaches are disclosed months/years later.
        Date difference is now used as a scoring factor, not a hard gate.
        """
        if not event1.event_date or not event2.event_date:
            return 0.8  # Neutral factor if dates missing (don't penalize too much)

        date_diff = abs((event1.event_date - event2.event_date).days)

        if date_diff == 0:
            return 1.0  # Same date
        elif date_diff <= 7:
            return 0.98  # Within a week
        elif date_diff <= 30:
            return 0.90  # Within a month (common for reporting delays)
        elif date_diff <= 90:
            return 0.80  # Within 3 months (investigation/disclosure delays)
        elif date_diff <= 180:
            return 0.70  # Within 6 months (reasonable for delayed disclosures)
        elif date_diff <= 365:
            return 0.60  # Within a year (possible for late-reported incidents)
        else:
            # More than a year apart - still possible if other indicators are strong
            # Use a sliding scale based on how strong other similarities are
            return max(0.4, 1.0 - (date_diff / 1000.0))  # Gradual decline, minimum 0.4

    def _merge_event_group(self, events: List[CyberEvent]) -> CyberEvent:
        base_event = max(events, key=lambda event: event.confidence.overall)
        merged_event = base_event.copy(deep=True)

        # Track how many events were merged
        merged_event.contributing_raw_events = len(events)
        merged_event.contributing_enriched_events = len(events)

        # Merge data sources
        all_sources = {source.url or source.source_id: source for event in events for source in event.data_sources}
        merged_event.data_sources = list(all_sources.values())

        # Merge affected entities
        entity_map: Dict[str, Dict[str, Any]] = {}
        for event in events:
            for entity in event.affected_entities:
                key = entity.name.lower()
                if key not in entity_map:
                    entity_map[key] = entity.model_dump()
        merged_event.affected_entities = [AffectedEntity(**data) for data in entity_map.values()]

        if merged_event.affected_entities:
            merged_event.primary_entity = merged_event.affected_entities[0]

        # Preserve the best available event_date (prefer earliest date that isn't 1st of month)
        all_dates = []
        self.logger.debug(f"Merging {len(events)} events - checking dates:")
        for i, event in enumerate(events):
            self.logger.debug(f"  Event {i}: '{event.title[:30]}...' - event_date: {event.event_date} (type: {type(event.event_date)})")
            if event.event_date:
                all_dates.append(event.event_date)

        if all_dates:
            # Separate dates into specific dates (not 1st of month) and fallback dates (1st of month)
            specific_dates = [date for date in all_dates if date.day != 1]
            fallback_dates = [date for date in all_dates if date.day == 1]

            if specific_dates:
                # Use the earliest specific date (more likely to be accurate)
                best_date = min(specific_dates)
                self.logger.debug(f"Using earliest specific date (not 1st of month): {best_date}")
            elif fallback_dates:
                # If only fallback dates available, use the earliest one
                best_date = min(fallback_dates)
                self.logger.debug(f"Using earliest fallback date (1st of month): {best_date}")
            else:
                # This shouldn't happen since we have all_dates, but fallback
                best_date = all_dates[0]  # Use first available date
                self.logger.debug(f"Using first available date as fallback: {best_date}")
        else:
            # No dates available in any event - keep original base event date
            best_date = merged_event.event_date
            self.logger.warning("No valid dates found in any event during merge - keeping base event date")

        merged_event.event_date = best_date

        # Log the merge results for debugging
        if best_date:
            self.logger.debug(f"Merged event '{merged_event.title[:30]}...' final event_date: {best_date}")
        else:
            self.logger.warning(f"Merged event '{merged_event.title[:30]}...' has NO event_date after merge!")

        # Merge other important fields that might be missing in base event
        # Use the most complete title (longest)
        best_title = merged_event.title
        for event in events:
            if event.title and len(event.title) > len(best_title):
                best_title = event.title
        merged_event.title = best_title

        # Use the most complete description (longest)
        best_description = merged_event.description
        for event in events:
            if event.description and len(event.description) > len(best_description):
                best_description = event.description
        merged_event.description = best_description

        merged_event.merged_events = [event.event_id for event in events if event.event_id != merged_event.event_id]
        source_count = len(all_sources)
        confidence_boost = min(source_count * 0.1, 0.3)
        merged_event.confidence.overall = min(merged_event.confidence.overall + confidence_boost, 1.0)

        self.logger.debug(f"Merged {len(events)} events into: {merged_event.title[:50]}... with date: {merged_event.event_date}")

        return merged_event

    def _llm_similarity_check(self, event1: CyberEvent, event2: CyberEvent) -> bool:
        """Use LLM to make final similarity decision for borderline cases."""
        try:
            # Import here to avoid issues if openai isn't available
            import openai

            # Get OpenAI API key from environment
            import os
            api_key = os.environ.get('OPENAI_API_KEY')
            if not api_key:
                self.logger.warning("OpenAI API key not found, skipping LLM similarity check")
                return False

            # Create a detailed conservative prompt for the LLM
            prompt = f"""You are a careful cyber-incident deduplicator. Be conservative: if not clearly the same incident, answer NO.

TASK

Decide whether the two event records below describe the same cyber incident.

Event 1

Title: {event1.title}
Date (as given): {event1.event_date}
Description (truncated): {event1.description[:300]}...

Event 2

Title: {event2.title}
Date (as given): {event2.event_date}
Description (truncated): {event2.description[:300]}...

DEFINITIONS

Same incident = The same organization (or the same explicitly named legal entity) suffered the same compromise, described with overlapping concrete details (e.g., threat actor/tool, victim system, location/site, data types/record counts, regulator case ID, or unique IOCs).

Different incident = Different orgs/entities, or same org but different compromises (different site, system, data set, or attack on a different date window) unless text explicitly states they are the same event.

DECISION PROCEDURE (follow in order)

Entity resolution (hard gate).

Normalize names and check: org legal name, subsidiary/brand, ticker, domain, country/state, sector.

If the organizations differ, answer NO unless the text explicitly equates them (e.g., "Acme Corp (parent of Beta Pty Ltd)" or "Acme trading as Beta"). Shared vendors, customers, or platforms ≠ same entity.

Extract anchors from each event.

Incident type/category (ransomware, phishing→credential theft, data leak, BEC, DDoS, vuln exploit/CVE, etc.)

Threat actor/malware/campaign name (e.g., "LockBit", "ALPHV", "Clop"), IOCs (domains/IPs/file hashes), or vuln IDs (CVE-…).

Affected system/site (plant/facility, region, cloud tenant, specific product).

Data compromised: kind (PII/PHI/PCI), specific fields (SSN, passport), scale (counts/"millions").

Regulator/reporting identifiers (case number, notification ID).

Timeframe: incident/containment/reporting dates.

Match test (require multiple independent matches).
Answer YES only if all are true:

Same organization/entity per step 1, and

At least two of the following anchors align with clear overlap:
a) same incident type and similar TTPs/actor/malware/CVE;
b) same affected site/system/product or same cloud tenant/application;
c) same data type and similar scale (e.g., "~2.5M patient records" vs "2.6M PHI");
d) same regulator/notification ID or uniquely identifying detail (specific domain/hostname, ticket #).

Date consistency: incident windows overlap or are plausibly the same (e.g., within ~1 month when anchors strongly match). Minor differences between incident vs disclosure dates may be ignored only if anchors match.

Disqualifiers (any ⇒ NO):

Different legal entities with no explicit parent/DBA equivalence stated.

Different countries/states or facilities when location is central to the description.

Different incident types or clearly different TTPs/actors (e.g., LockBit vs BEC wiring fraud).

Different data types or clearly different scales (e.g., "internal IT outage, no data exposed" vs "2M customer SSNs leaked").

One refers to a campaign or vendor compromise affecting many customers; the other is a specific customer impact without explicit linkage.

"Ongoing" or "possible" in one report with no unique anchors tying it to the other.

OUTPUT

Return only a single token: YES or NO.

If uncertain or anchors are insufficient, return NO.
"""

            client = openai.OpenAI(api_key=api_key)
            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are an expert at identifying duplicate cyber security incidents from news reports."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=10,
                temperature=0.1
            )

            result = response.choices[0].message.content.strip().upper()
            is_similar = result == "YES"

            self.logger.debug(f"LLM similarity decision: {result} ('{event1.title[:30]}...' vs '{event2.title[:30]}...')")
            return is_similar

        except Exception as e:
            self.logger.warning(f"LLM similarity check failed: {e}")
            return False
