#!/usr/bin/env python3
"""
DEPRECATED: Event Deduplication Processor - Multithreaded Enhanced Edition

⚠️  WARNING: This script is DEPRECATED and will be removed in a future version.
    Use the new global deduplication system instead:
    - cyber_data_collector/processing/deduplication_v2.py
    - cyber_data_collector/storage/deduplication_storage.py

This script implements intelligent deduplication logic that:
1. Groups events by affected entity (victim organization)
2. Uses Perplexity to find earliest known dates for events
3. Performs deep duplicate checking regardless of timestamp
4. Merges duplicates intelligently using best data from all sources
5. Processes everything in parallel with 10 concurrent threads
6. Merges URLs, dates, threat actors, and record counts from all sources

LEGACY FEATURES (being replaced):
- Multithreaded processing with 10 workers
- Entity-based grouping (not just title similarity)
- Perplexity-based date correction
- Intelligent merging of all fields
- JSON-based responses for reliability
- Thread-safe database operations

MIGRATION PATH:
The new deduplication system provides:
- Object-oriented design with clear separation of concerns
- Comprehensive validation and error checking
- Merge lineage tracking for transparency
- Global deduplication (not month-by-month)
- Better performance and reliability
- Proper database constraints to prevent duplicates

To migrate:
1. Use the new global deduplication in discover_enrich_events.py
2. Run the migration script: migrate_to_global_deduplication.py
3. Remove this file after migration is complete
"""

import json
import logging
import os
import sqlite3
import sys
import threading
import time
import uuid
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Setup logging with UTF-8 encoding for Windows
import sys

# Create file handler with UTF-8 encoding
file_handler = logging.FileHandler('deduplication.log', encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

# Create console handler with UTF-8 encoding
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

# Configure root logger
logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, console_handler]
)
logger = logging.getLogger(__name__)

# Thread-safe counters
lock = threading.Lock()
stats = {
    'events_processed': 0,
    'duplicates_found': 0,
    'dates_corrected': 0,
    'dates_suspicious': 0,
    'dates_confirmed': 0,
    'dates_failed': 0,
    'json_retries': 0,
    'entities_grouped': 0,
    'groups_merged': 0,
    'urls_consolidated': 0
}


class PerplexityDateCorrector:
    """Use Perplexity to find earliest known dates for cyber events."""

    def __init__(self, api_key: str, max_retries: int = 3):
        self.api_key = api_key
        self.max_retries = max_retries

    def query_perplexity(self, query: str) -> Optional[Dict]:
        """Query Perplexity API with retry logic and JSON response."""
        url = "https://api.perplexity.ai/chat/completions"

        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json'
        }

        payload = {
            "model": "sonar-pro",
            "messages": [
                {
                    "role": "user",
                    "content": query
                }
            ],
            "max_tokens": 500,
            "temperature": 0.1,
            "stream": False
        }

        for attempt in range(self.max_retries):
            try:
                response = requests.post(url, json=payload, headers=headers, timeout=30)

                if response.status_code == 200:
                    data = response.json()
                    if 'choices' in data and data['choices']:
                        content = data['choices'][0]['message']['content'].strip()

                        # Try to extract JSON from the response
                        json_start = content.find('{')
                        json_end = content.rfind('}') + 1

                        if json_start >= 0 and json_end > json_start:
                            json_str = content[json_start:json_end]
                            try:
                                result = json.loads(json_str)
                                return result
                            except json.JSONDecodeError as e:
                                # Retry if JSON parsing fails (Perplexity didn't format correctly)
                                with lock:
                                    stats['json_retries'] += 1
                                if attempt < self.max_retries - 1:
                                    logger.warning(f"Failed to parse JSON (attempt {attempt + 1}/{self.max_retries}), retrying...")
                                    logger.debug(f"JSON parse error: {e}")
                                    logger.debug(f"Content received: {content[:200]}")
                                    time.sleep(1)  # Brief pause before retry
                                    continue
                                else:
                                    logger.error(f"Failed to parse JSON after {self.max_retries} attempts")
                                    logger.debug(f"Final content: {content[:500]}")
                                    with lock:
                                        stats['dates_failed'] += 1
                                    return None
                        else:
                            # No JSON object found - retry
                            with lock:
                                stats['json_retries'] += 1
                            if attempt < self.max_retries - 1:
                                logger.warning(f"No JSON object found (attempt {attempt + 1}/{self.max_retries}), retrying...")
                                logger.debug(f"Content received: {content[:200]}")
                                time.sleep(1)
                                continue
                            else:
                                logger.error(f"No JSON object found after {self.max_retries} attempts")
                                logger.debug(f"Final content: {content[:500]}")
                                with lock:
                                    stats['dates_failed'] += 1
                                return None

                elif response.status_code in [429, 503]:
                    wait_time = 2 ** attempt
                    retry_after = response.headers.get('Retry-After')
                    if retry_after:
                        wait_time = int(retry_after)
                    logger.warning(f"Rate limit hit, retrying in {wait_time}s (attempt {attempt + 1}/{self.max_retries})")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error(f"API error: HTTP {response.status_code}")
                    return None

            except Exception as e:
                if attempt < self.max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.warning(f"API call failed: {e}, retrying in {wait_time}s (attempt {attempt + 1}/{self.max_retries})")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error(f"API call failed after {self.max_retries} attempts: {e}")
                    return None

        return None

    def get_earliest_event_date(self, event_title: str, entity_name: str,
                                 summary: str, known_dates: List[str]) -> Optional[Dict]:
        """Query Perplexity for the earliest known date of an event."""

        dates_str = ", ".join(known_dates[:5]) if known_dates else "No dates available"
        summary_snippet = summary[:400] if summary else "No additional details"

        query = f"""
Cyber Security Incident Analysis:

AFFECTED ENTITY: {entity_name}
INCIDENT TITLE: {event_title}
DETAILS: {summary_snippet}
DATES FROM SOURCES: {dates_str}

CRITICAL TASK: Find the ACTUAL date when this cyber incident occurred or was first discovered.

WARNING: The provided dates may be:
- News article publication dates (not the incident date)
- Follow-up story dates (covering an older incident)
- Placeholder dates (1st of month)
- Incorrect dates from unreliable sources

Your job is to research this SPECIFIC incident involving {entity_name} and find:
1. When did the cyber attack/breach actually occur?
2. When was it first discovered/detected?
3. When was it first publicly disclosed?

Respond ONLY with valid JSON in this exact format:
{{
    "earliest_date": "YYYY-MM-DD",
    "date_type": "<one of: incident_date, discovery_date, disclosure_date>",
    "confidence": <0.0-1.0>,
    "explanation": "<source and reasoning>",
    "is_corrected": <true if different from provided dates, false otherwise>
}}

GUIDELINES:
- If provided dates are news publication dates about a historical event, find the actual incident date
- Search for official statements, regulatory filings, company announcements
- Cite specific sources (e.g., "OAIC report dated...", "Company statement on...")
- If uncertain, indicate lower confidence
- Do NOT just repeat the provided dates unless you verify they are correct

Do not include any text outside the JSON object.
"""

        return self.query_perplexity(query)


class EnhancedDeduplicationProcessor:
    """Enhanced deduplication processor with multithreading and intelligent merging."""

    def __init__(self, db_path: str, api_key: str = None):
        self.db_path = db_path
        self.date_corrector = PerplexityDateCorrector(api_key) if api_key else None
        self.similarity_threshold = 0.75

    def get_connection(self) -> sqlite3.Connection:
        """Get a database connection."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_all_events(self) -> List[Dict[str, Any]]:
        """Get all enriched events for deduplication."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                ee.enriched_event_id,
                ee.raw_event_id,
                ee.title,
                ee.description,
                ee.summary,
                ee.event_type,
                ee.severity,
                ee.event_date,
                ee.records_affected,
                ee.is_australian_event,
                ee.is_specific_event,
                ee.confidence_score,
                ee.australian_relevance_score,
                ee.attacking_entity_name,
                ee.attack_method,
                re.raw_title,
                re.source_url,
                re.source_type,
                re.discovered_at
            FROM EnrichedEvents ee
            JOIN RawEvents re ON ee.raw_event_id = re.raw_event_id
            WHERE ee.status = 'Active'
            ORDER BY ee.event_date ASC NULLS LAST, ee.created_at ASC
        """)

        events = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return events

    def extract_entity_name(self, title: str) -> Optional[str]:
        """Extract victim entity name from event title."""
        # Common patterns for entity extraction
        title_lower = title.lower()

        # Try various patterns
        import re

        # Pattern: "EntityName Data Breach", "EntityName Cyber Attack", etc.
        patterns = [
            r'^([A-Z][a-zA-Z0-9\s&\'-]+?)(?:\s+(?:data breach|cyber attack|hack|breach|incident|ransomware))',
            r'(?:breach at|attack on|incident at|hack of)\s+([A-Z][a-zA-Z0-9\s&\'-]+)',
            r'^([A-Z][a-zA-Z0-9\s&\'-]+?)(?:\s+suffers?|\s+experiences?|\s+reports?)',
        ]

        for pattern in patterns:
            match = re.search(pattern, title, re.IGNORECASE)
            if match:
                entity = match.group(1).strip()
                # Filter out too-short or generic terms
                if len(entity) > 3 and entity.lower() not in ['the', 'a', 'an', 'data', 'cyber']:
                    return entity

        # Fallback: use first 2-3 capitalized words
        words = title.split()
        entity_words = []
        for word in words[:5]:
            if word[0].isupper() and len(word) > 2:
                entity_words.append(word)
            elif entity_words:  # Stop after first non-capitalized word after starting
                break
            if len(entity_words) >= 3:
                break

        if entity_words:
            return ' '.join(entity_words)

        return None

    def group_events_by_entity(self, events: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        """Group events by affected entity, regardless of date."""
        entity_groups = {}

        for event in events:
            entity = self.extract_entity_name(event.get('title', ''))

            if entity:
                entity_lower = entity.lower().strip()

                # Find if this entity already exists (fuzzy matching)
                found_group = None
                for existing_entity in entity_groups.keys():
                    similarity = SequenceMatcher(None, entity_lower, existing_entity.lower()).ratio()
                    if similarity > 0.85:  # High threshold for entity matching
                        found_group = existing_entity
                        break

                if found_group:
                    entity_groups[found_group].append(event)
                else:
                    entity_groups[entity] = [event]
            else:
                # Events without clear entity go in a special group
                if 'UNKNOWN_ENTITY' not in entity_groups:
                    entity_groups['UNKNOWN_ENTITY'] = []
                entity_groups['UNKNOWN_ENTITY'].append(event)

        return entity_groups

    def are_events_duplicates(self, event1: Dict[str, Any], event2: Dict[str, Any]) -> Tuple[bool, float]:
        """Deep check if two events are duplicates."""

        # Calculate text similarity
        title1 = (event1.get('title') or '').lower()
        title2 = (event2.get('title') or '').lower()

        summary1 = (event1.get('summary') or event1.get('description') or '')[:300].lower()
        summary2 = (event2.get('summary') or event2.get('description') or '')[:300].lower()

        title_similarity = SequenceMatcher(None, title1, title2).ratio()
        summary_similarity = SequenceMatcher(None, summary1, summary2).ratio()

        # Check entity match
        entity1 = self.extract_entity_name(event1.get('title', ''))
        entity2 = self.extract_entity_name(event2.get('title', ''))

        entity_match = False
        if entity1 and entity2:
            entity_similarity = SequenceMatcher(None, entity1.lower(), entity2.lower()).ratio()
            entity_match = entity_similarity > 0.85

        # Check attack type similarity
        attack_type_match = False
        type1 = (event1.get('event_type') or '').lower()
        type2 = (event2.get('event_type') or '').lower()
        if type1 and type2 and type1 == type2:
            attack_type_match = True

        # Calculate overall similarity score
        score = (
            title_similarity * 0.5 +
            summary_similarity * 0.3 +
            (1.0 if entity_match else 0.0) * 0.15 +
            (1.0 if attack_type_match else 0.0) * 0.05
        )

        is_duplicate = score >= self.similarity_threshold

        return is_duplicate, score

    def find_duplicates_in_group(self, events: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """Find duplicate clusters within an entity group."""
        if len(events) <= 1:
            return [events]

        clusters = []
        processed = set()

        for i, event1 in enumerate(events):
            if i in processed:
                continue

            cluster = [event1]
            processed.add(i)

            for j, event2 in enumerate(events):
                if j <= i or j in processed:
                    continue

                is_dup, score = self.are_events_duplicates(event1, event2)
                if is_dup:
                    cluster.append(event2)
                    processed.add(j)

            clusters.append(cluster)

        return clusters

    def merge_duplicate_cluster(self, cluster: List[Dict[str, Any]],
                                use_date_correction: bool = False) -> Dict[str, Any]:
        """Intelligently merge a cluster of duplicate events."""

        # Sort by confidence score (highest first)
        cluster_sorted = sorted(cluster, key=lambda e: e.get('confidence_score', 0.0), reverse=True)

        # Choose best event as base
        best_event = cluster_sorted[0]

        # Collect all dates
        dates = []
        for event in cluster:
            if event.get('event_date'):
                try:
                    date_str = event['event_date']
                    if isinstance(date_str, str):
                        dates.append(date_str)
                except:
                    pass

        # Find earliest date
        earliest_date = min(dates) if dates else None

        # Check if date looks suspicious (likely placeholder or news article date)
        date_is_suspicious = False
        suspicious_reasons = []

        if earliest_date:
            try:
                from datetime import datetime as dt
                date_obj = dt.fromisoformat(earliest_date.replace('Z', ''))

                # Suspicious if it's the 1st of the month (likely placeholder)
                if date_obj.day == 1:
                    date_is_suspicious = True
                    suspicious_reasons.append("1st of month")

                # Suspicious if all dates are very close to each other but spread over weeks
                # (might be follow-up news articles about same incident)
                if len(dates) > 2:
                    date_objs = []
                    for d in dates:
                        try:
                            date_objs.append(dt.fromisoformat(d.replace('Z', '')))
                        except:
                            pass

                    if len(date_objs) >= 2:
                        date_range = (max(date_objs) - min(date_objs)).days
                        # Suspicious if dates span 7-60 days (likely follow-up stories)
                        if 7 <= date_range <= 60:
                            date_is_suspicious = True
                            suspicious_reasons.append(f"dates span {date_range} days")

                # Suspicious if date is very recent compared to typical incident discovery lag
                # (might be publication date of a historical story)
                if len(cluster) > 1:
                    # If we have multiple sources for same event, dates should be close
                    # unless one is a follow-up story
                    pass  # Already handled above

                if date_is_suspicious:
                    reason_str = ", ".join(suspicious_reasons)
                    logger.debug(f"  [WARNING] Suspicious date: {earliest_date} ({reason_str})")
                    with lock:
                        stats['dates_suspicious'] += 1
            except:
                pass

        # Extract entity name for better date correction
        entity_name = self.extract_entity_name(best_event.get('title', ''))
        if not entity_name:
            entity_name = "Unknown Entity"

        # Get best summary/description for context
        summary = best_event.get('summary') or best_event.get('description') or ""

        # Always use Perplexity when date correction is enabled
        # The AI will validate correct dates and fix incorrect ones
        if use_date_correction and self.date_corrector:
            try:
                date_result = self.date_corrector.get_earliest_event_date(
                    best_event.get('title', ''),
                    entity_name,
                    summary,
                    dates
                )

                if date_result and date_result.get('earliest_date'):
                    corrected_date = date_result['earliest_date']
                    confidence = date_result.get('confidence', 0.0)
                    explanation = date_result.get('explanation', 'No explanation')
                    date_type = date_result.get('date_type', 'unknown')
                    is_corrected = date_result.get('is_corrected', False)

                    # Only log and count if date actually changed
                    if corrected_date != earliest_date:
                        reason_marker = " (SUSPICIOUS)" if date_is_suspicious else ""
                        logger.info(f"  [DATE CHANGED]{reason_marker}: {earliest_date} -> {corrected_date} ({date_type})")
                        logger.info(f"     Confidence: {confidence:.2f} | {explanation[:120]}")
                        earliest_date = corrected_date
                        with lock:
                            stats['dates_corrected'] += 1
                    else:
                        if date_is_suspicious:
                            reason_str = ", ".join(suspicious_reasons)
                            logger.info(f"  [OK] Suspicious date ({reason_str}) verified as correct: {earliest_date}")
                            logger.info(f"     {explanation[:120]}")
                        logger.debug(f"  Date confirmed: {earliest_date} (confidence: {confidence:.2f})")
                        with lock:
                            stats['dates_confirmed'] += 1
            except Exception as e:
                logger.warning(f"  Date correction failed: {e}")

        # Merge URLs from all sources
        urls = set()
        for event in cluster:
            if event.get('source_url'):
                urls.add(event['source_url'])

        # Choose best threat actor (prefer non-Unknown, longest)
        threat_actors = [e.get('attacking_entity_name') for e in cluster
                        if e.get('attacking_entity_name')
                        and e['attacking_entity_name'] != 'Unknown'
                        and len(e['attacking_entity_name']) > 3]
        best_threat_actor = max(threat_actors, key=len) if threat_actors else 'Unknown'

        # Choose best attack method
        attack_methods = [e.get('attack_method') for e in cluster
                         if e.get('attack_method') and e['attack_method'] != 'Unknown']
        best_attack_method = max(attack_methods, key=len) if attack_methods else None

        # Choose most reliable records_affected (prefer higher confidence sources)
        records_affected = None
        best_confidence = 0.0
        for event in cluster:
            if event.get('records_affected') and event.get('records_affected', 0) > 0:
                confidence = event.get('confidence_score', 0.5)
                if confidence > best_confidence or records_affected is None:
                    records_affected = event['records_affected']
                    best_confidence = confidence

        # Choose best severity (prefer non-Unknown)
        severities = [e.get('severity') for e in cluster
                     if e.get('severity') and e['severity'] != 'Unknown']
        severity_order = {'Critical': 4, 'High': 3, 'Medium': 2, 'Low': 1}
        best_severity = max(severities, key=lambda s: severity_order.get(s, 0)) if severities else 'Unknown'

        # Merge descriptions (use longest/most detailed)
        descriptions = [e.get('description') or e.get('summary') for e in cluster if e.get('description') or e.get('summary')]
        best_description = max(descriptions, key=len) if descriptions else best_event.get('description')

        # Create merged event
        merged = {
            'deduplicated_event_id': str(uuid.uuid4()),
            'title': best_event.get('title'),
            'description': best_description,
            'summary': best_event.get('summary'),
            'event_type': best_event.get('event_type'),
            'severity': best_severity,
            'event_date': earliest_date,
            'records_affected': records_affected,
            'is_australian_event': best_event.get('is_australian_event'),
            'is_specific_event': best_event.get('is_specific_event'),
            'confidence_score': best_event.get('confidence_score'),
            'australian_relevance_score': best_event.get('australian_relevance_score'),
            'attacking_entity_name': best_threat_actor,
            'attack_method': best_attack_method,
            'total_data_sources': len(urls),
            'contributing_raw_events': len(set(e['raw_event_id'] for e in cluster)),
            'contributing_enriched_events': len(cluster),
            'master_enriched_event_id': best_event['enriched_event_id'],
            'source_urls': list(urls),
            'original_events': cluster
        }

        with lock:
            stats['urls_consolidated'] += len(urls)

        return merged

    def save_deduplicated_event(self, merged_event: Dict[str, Any]) -> str:
        """Save deduplicated event to database (thread-safe)."""
        with lock:
            conn = self.get_connection()
            cursor = conn.cursor()

            try:
                # Calculate similarity score
                cluster = merged_event['original_events']
                similarities = []
                for i in range(len(cluster)):
                    for j in range(i + 1, len(cluster)):
                        _, score = self.are_events_duplicates(cluster[i], cluster[j])
                        similarities.append(score)
                avg_similarity = sum(similarities) / len(similarities) if similarities else 1.0

                cursor.execute("""
                    INSERT INTO DeduplicatedEvents (
                        deduplicated_event_id, master_enriched_event_id, title, description, summary,
                        event_type, severity, event_date, records_affected, is_australian_event,
                        is_specific_event, confidence_score, australian_relevance_score,
                        total_data_sources, contributing_raw_events, contributing_enriched_events,
                        similarity_score, deduplication_method, attacking_entity_name, attack_method,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    merged_event['deduplicated_event_id'],
                    merged_event['master_enriched_event_id'],
                    merged_event['title'],
                    merged_event['description'],
                    merged_event['summary'],
                    merged_event['event_type'],
                    merged_event['severity'],
                    merged_event['event_date'],
                    merged_event['records_affected'],
                    merged_event['is_australian_event'],
                    merged_event['is_specific_event'],
                    merged_event['confidence_score'],
                    merged_event['australian_relevance_score'],
                    merged_event['total_data_sources'],
                    merged_event['contributing_raw_events'],
                    merged_event['contributing_enriched_events'],
                    avg_similarity,
                    'entity_based_multithreaded',
                    merged_event.get('attacking_entity_name'),
                    merged_event.get('attack_method'),
                    'Active',
                    datetime.now().isoformat(),
                    datetime.now().isoformat()
                ))

                # Save URLs
                for url in merged_event['source_urls']:
                    cursor.execute("""
                        INSERT OR IGNORE INTO DeduplicatedEventSources (
                            deduplicated_event_id, source_url, source_type,
                            credibility_score, discovered_at
                        ) VALUES (?, ?, ?, ?, ?)
                    """, (
                        merged_event['deduplicated_event_id'],
                        url,
                        'Web',
                        0.8,
                        datetime.now().isoformat()
                    ))

                # Create event mappings
                for event in merged_event['original_events']:
                    cursor.execute("""
                        INSERT OR IGNORE INTO EventDeduplicationMap (
                            map_id, raw_event_id, enriched_event_id, deduplicated_event_id,
                            contribution_type, similarity_score, data_source_weight, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        str(uuid.uuid4()),
                        event['raw_event_id'],
                        event['enriched_event_id'],
                        merged_event['deduplicated_event_id'],
                        'primary' if event['enriched_event_id'] == merged_event['master_enriched_event_id'] else 'supporting',
                        event.get('confidence_score', 0.8),
                        1.0,
                        datetime.now().isoformat()
                    ))

                conn.commit()
                conn.close()

                return merged_event['deduplicated_event_id']

            except Exception as e:
                conn.rollback()
                conn.close()
                logger.error(f"Error saving deduplicated event: {e}")
                raise

    def process_entity_group(self, entity_name: str, events: List[Dict[str, Any]],
                            group_num: int, total_groups: int,
                            use_date_correction: bool) -> Dict[str, Any]:
        """Process a single entity group (thread-safe)."""
        logger.info(f"[{group_num}/{total_groups}] Processing entity: {entity_name} ({len(events)} events)")

        try:
            # Find duplicate clusters within this entity group
            clusters = self.find_duplicates_in_group(events)

            deduplicated_ids = []
            duplicates_merged = 0

            for cluster in clusters:
                if len(cluster) > 1:
                    logger.info(f"  Found duplicate cluster with {len(cluster)} events")
                    duplicates_merged += len(cluster) - 1

                # Merge cluster
                merged_event = self.merge_duplicate_cluster(cluster, use_date_correction)

                # Save to database
                dedup_id = self.save_deduplicated_event(merged_event)
                deduplicated_ids.append(dedup_id)

            with lock:
                stats['events_processed'] += len(events)
                stats['duplicates_found'] += duplicates_merged
                stats['groups_merged'] += len(deduplicated_ids)

            return {
                'status': 'success',
                'entity': entity_name,
                'events_processed': len(events),
                'duplicates_merged': duplicates_merged,
                'deduplicated_events': len(deduplicated_ids)
            }

        except Exception as e:
            logger.error(f"  Error processing entity {entity_name}: {e}")
            return {
                'status': 'error',
                'entity': entity_name,
                'error': str(e)
            }


def main():
    import argparse
    
    # DEPRECATION WARNING
    print("⚠️  WARNING: This script is DEPRECATED!")
    print("   Use the new global deduplication system instead.")
    print("   See: cyber_data_collector/processing/deduplication_v2.py")
    print("   Migration script: migrate_to_global_deduplication.py")
    print()

    parser = argparse.ArgumentParser(
        description='DEPRECATED: Enhanced Deduplication Processor with Multithreading'
    )
    parser.add_argument('--db-path', default='instance/cyber_events.db',
                       help='Path to SQLite database file')
    parser.add_argument('--max-workers', type=int, default=10,
                       help='Number of concurrent workers (default: 10)')
    parser.add_argument('--correct-dates', action='store_true',
                       help='Use Perplexity to correct event dates (requires API key)')
    parser.add_argument('--limit', type=int,
                       help='Limit number of entity groups to process (for testing)')

    args = parser.parse_args()

    # Get API key if date correction is enabled
    api_key = None
    if args.correct_dates:
        api_key = os.getenv('PERPLEXITY_API_KEY')
        if not api_key:
            logger.error("PERPLEXITY_API_KEY required for date correction")
            sys.exit(1)
        logger.info("Date correction enabled via Perplexity")

    # Create processor
    processor = EnhancedDeduplicationProcessor(args.db_path, api_key)

    logger.info("="*60)
    logger.info("Enhanced Deduplication Processor")
    logger.info("="*60)
    logger.info(f"Database: {args.db_path}")
    logger.info(f"Max workers: {args.max_workers}")
    logger.info(f"Date correction: {args.correct_dates}")

    # Get all events
    logger.info("\nLoading events...")
    events = processor.get_all_events()
    logger.info(f"Loaded {len(events)} enriched events")

    if not events:
        logger.info("No events to process")
        return

    # Group by entity
    logger.info("\nGrouping events by affected entity...")
    entity_groups = processor.group_events_by_entity(events)

    logger.info(f"Found {len(entity_groups)} unique entities")
    with lock:
        stats['entities_grouped'] = len(entity_groups)

    # Show entity distribution
    multi_event_entities = [(name, len(events)) for name, events in entity_groups.items() if len(events) > 1]
    multi_event_entities.sort(key=lambda x: x[1], reverse=True)

    if multi_event_entities:
        logger.info(f"\nEntities with multiple events (top 10):")
        for name, count in multi_event_entities[:10]:
            logger.info(f"  {name}: {count} events")

    # Limit if requested
    entity_groups_to_process = list(entity_groups.items())
    if args.limit:
        entity_groups_to_process = entity_groups_to_process[:args.limit]
        logger.info(f"\nLimited to {len(entity_groups_to_process)} entity groups")

    # Process entity groups in parallel
    logger.info(f"\nProcessing with {args.max_workers} concurrent workers...")
    logger.info("="*60)

    start_time = time.time()

    results = []
    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        futures = []
        for i, (entity_name, entity_events) in enumerate(entity_groups_to_process, 1):
            future = executor.submit(
                processor.process_entity_group,
                entity_name,
                entity_events,
                i,
                len(entity_groups_to_process),
                args.correct_dates
            )
            futures.append(future)

        for future in as_completed(futures):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                logger.error(f"Task failed: {e}")

    end_time = time.time()
    processing_time = end_time - start_time

    # Summary
    logger.info("\n" + "="*60)
    logger.info("=== Deduplication Summary ===")
    logger.info("="*60)
    logger.info(f"Total entity groups: {stats['entities_grouped']}")
    logger.info(f"Events processed: {stats['events_processed']}")
    logger.info(f"Duplicates found and merged: {stats['duplicates_found']}")
    logger.info(f"Deduplicated events created: {stats['groups_merged']}")
    logger.info(f"URLs consolidated: {stats['urls_consolidated']}")
    if args.correct_dates:
        logger.info(f"\nDate Correction Statistics:")
        logger.info(f"  Suspicious dates found: {stats['dates_suspicious']}")
        logger.info(f"  Dates actually corrected: {stats['dates_corrected']}")
        logger.info(f"  Dates confirmed as correct: {stats['dates_confirmed']}")
        logger.info(f"  Date corrections failed: {stats['dates_failed']}")
        logger.info(f"  JSON parsing retries: {stats['json_retries']}")
    logger.info(f"\nProcessing time: {processing_time/60:.1f} minutes")

    success_count = sum(1 for r in results if r['status'] == 'success')
    error_count = sum(1 for r in results if r['status'] == 'error')
    logger.info(f"Successful: {success_count}, Errors: {error_count}")


if __name__ == "__main__":
    main()
