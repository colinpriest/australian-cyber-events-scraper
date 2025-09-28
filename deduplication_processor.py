#!/usr/bin/env python3
"""
Event Deduplication Processor for V2 Database Schema

This script implements the proper deduplication logic that:
1. Groups similar enriched events together
2. Creates deduplicated events in DeduplicatedEvents table
3. Maps all contributing raw events to deduplicated events
4. Consolidates data sources and entities
5. Provides full traceability from raw → enriched → deduplicated events
"""

import asyncio
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from difflib import SequenceMatcher

# Add parent directory to path for imports
sys.path.append(str(Path(__file__).parent))

from cyber_event_data_v2 import CyberEventDataV2


class DeduplicationProcessor:
    """Process event deduplication using the V2 database schema."""

    def __init__(self, db: CyberEventDataV2):
        self.db = db
        self.similarity_threshold = 0.8
        self.date_tolerance_days = 30  # 1 month tolerance

    def get_enriched_events_for_deduplication(self) -> List[Dict[str, Any]]:
        """Get all enriched events that need deduplication."""

        with self.db._lock:
            cursor = self.db._conn.cursor()

            # Get enriched events with their raw event details
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
                    re.raw_title,
                    re.source_url,
                    re.source_type,
                    re.discovered_at
                FROM EnrichedEvents ee
                JOIN RawEvents re ON ee.raw_event_id = re.raw_event_id
                WHERE ee.status = 'Active'
                ORDER BY ee.created_at ASC
            """)

            return [dict(row) for row in cursor.fetchall()]

    def group_similar_events(self, events: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """Group similar events together for deduplication."""

        groups = []
        processed_indices = set()

        for i, event in enumerate(events):
            if i in processed_indices:
                continue

            # Start a new group with this event
            group = [event]
            processed_indices.add(i)

            # Find similar events to add to this group
            for j in range(i + 1, len(events)):
                if j in processed_indices:
                    continue

                if self.are_events_similar(event, events[j]):
                    group.append(events[j])
                    processed_indices.add(j)

            groups.append(group)

        return groups

    def are_events_similar(self, event1: Dict[str, Any], event2: Dict[str, Any]) -> bool:
        """Determine if two events are similar enough to be considered duplicates."""

        # Check date similarity if both have dates
        if event1.get('event_date') and event2.get('event_date'):
            try:
                from datetime import datetime
                date1 = datetime.fromisoformat(event1['event_date']).date() if isinstance(event1['event_date'], str) else event1['event_date']
                date2 = datetime.fromisoformat(event2['event_date']).date() if isinstance(event2['event_date'], str) else event2['event_date']

                if date1 and date2:
                    date_diff = abs((date1 - date2).days)
                    if date_diff > self.date_tolerance_days:
                        return False
            except:
                pass  # Continue with text similarity if date comparison fails

        # Calculate text similarity
        title1 = (event1.get('title') or '').lower()
        title2 = (event2.get('title') or '').lower()

        summary1 = (event1.get('summary') or '').lower()[:200]  # First 200 chars
        summary2 = (event2.get('summary') or '').lower()[:200]

        title_similarity = SequenceMatcher(None, title1, title2).ratio()
        summary_similarity = SequenceMatcher(None, summary1, summary2).ratio()

        # Weighted average (title is more important)
        overall_similarity = (title_similarity * 0.7) + (summary_similarity * 0.3)

        return overall_similarity >= self.similarity_threshold

    def create_deduplicated_event(self, event_group: List[Dict[str, Any]]) -> str:
        """Create a deduplicated event from a group of similar events."""

        # Choose the best event as the master (highest confidence score)
        master_event = max(event_group, key=lambda e: e.get('confidence_score', 0.0))

        deduplicated_event_id = str(uuid.uuid4())

        # Calculate aggregate statistics
        total_data_sources = len(set(e['source_url'] for e in event_group if e.get('source_url')))
        contributing_raw_events = len(set(e['raw_event_id'] for e in event_group))
        contributing_enriched_events = len(event_group)

        # Calculate average similarity within the group
        similarities = []
        for i in range(len(event_group)):
            for j in range(i + 1, len(event_group)):
                title_sim = SequenceMatcher(
                    None,
                    (event_group[i].get('title') or '').lower(),
                    (event_group[j].get('title') or '').lower()
                ).ratio()
                similarities.append(title_sim)

        avg_similarity = sum(similarities) / len(similarities) if similarities else 1.0

        # Create deduplicated event
        with self.db._lock:
            cursor = self.db._conn.cursor()

            cursor.execute("""
                INSERT INTO DeduplicatedEvents (
                    deduplicated_event_id, master_enriched_event_id, title, description, summary,
                    event_type, severity, event_date, records_affected, is_australian_event,
                    is_specific_event, confidence_score, australian_relevance_score,
                    total_data_sources, contributing_raw_events, contributing_enriched_events,
                    similarity_score, deduplication_method, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                deduplicated_event_id,
                master_event['enriched_event_id'],
                master_event['title'],
                master_event['description'],
                master_event['summary'],
                master_event['event_type'],
                master_event['severity'],
                master_event['event_date'],
                master_event['records_affected'],
                master_event['is_australian_event'],
                master_event['is_specific_event'],
                master_event['confidence_score'],
                master_event['australian_relevance_score'],
                total_data_sources,
                contributing_raw_events,
                contributing_enriched_events,
                avg_similarity,
                'title_similarity',
                'Active',
                datetime.now().isoformat(),
                datetime.now().isoformat()
            ))

            self.db._conn.commit()

        return deduplicated_event_id

    def create_event_mappings(self, deduplicated_event_id: str, event_group: List[Dict[str, Any]]):
        """Create mappings from raw events to deduplicated events."""

        master_event = max(event_group, key=lambda e: e.get('confidence_score', 0.0))

        with self.db._lock:
            cursor = self.db._conn.cursor()

            for i, event in enumerate(event_group):
                map_id = str(uuid.uuid4())

                # Determine contribution type
                if event['enriched_event_id'] == master_event['enriched_event_id']:
                    contribution_type = 'primary'
                elif i < 2:  # First few are supporting
                    contribution_type = 'supporting'
                else:
                    contribution_type = 'duplicate'

                # Calculate similarity to master
                similarity = SequenceMatcher(
                    None,
                    (master_event.get('title') or '').lower(),
                    (event.get('title') or '').lower()
                ).ratio()

                cursor.execute("""
                    INSERT INTO EventDeduplicationMap (
                        map_id, raw_event_id, enriched_event_id, deduplicated_event_id,
                        contribution_type, similarity_score, data_source_weight, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    map_id,
                    event['raw_event_id'],
                    event['enriched_event_id'],
                    deduplicated_event_id,
                    contribution_type,
                    similarity,
                    1.0,  # Default weight
                    datetime.now().isoformat()
                ))

            self.db._conn.commit()

    def create_deduplication_cluster(self, deduplicated_event_id: str, event_group: List[Dict[str, Any]]):
        """Create deduplication cluster information."""

        # Calculate average pairwise similarity
        similarities = []
        for i in range(len(event_group)):
            for j in range(i + 1, len(event_group)):
                sim = SequenceMatcher(
                    None,
                    (event_group[i].get('title') or '').lower(),
                    (event_group[j].get('title') or '').lower()
                ).ratio()
                similarities.append(sim)

        avg_similarity = sum(similarities) / len(similarities) if similarities else 1.0

        cluster_id = str(uuid.uuid4())

        with self.db._lock:
            cursor = self.db._conn.cursor()

            cursor.execute("""
                INSERT INTO DeduplicationClusters (
                    cluster_id, deduplicated_event_id, cluster_size,
                    average_similarity, deduplication_timestamp, algorithm_version
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                cluster_id,
                deduplicated_event_id,
                len(event_group),
                avg_similarity,
                datetime.now().isoformat(),
                'v1.0'
            ))

            self.db._conn.commit()

    def consolidate_data_sources(self, deduplicated_event_id: str, event_group: List[Dict[str, Any]]):
        """Consolidate data sources for the deduplicated event."""

        # Collect unique data sources
        sources = {}
        for event in event_group:
            if event.get('source_url'):
                url = event['source_url']
                if url not in sources:
                    sources[url] = {
                        'source_url': url,
                        'source_type': event.get('source_type', 'Unknown'),
                        'credibility_score': 0.8,  # Default credibility
                        'content_snippet': None,  # Could be populated from raw content
                        'discovered_at': event.get('discovered_at')
                    }

        # Insert consolidated sources
        with self.db._lock:
            cursor = self.db._conn.cursor()

            for source_data in sources.values():
                cursor.execute("""
                    INSERT OR IGNORE INTO DeduplicatedEventSources (
                        deduplicated_event_id, source_url, source_type,
                        credibility_score, content_snippet, discovered_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    deduplicated_event_id,
                    source_data['source_url'],
                    source_data['source_type'],
                    source_data['credibility_score'],
                    source_data['content_snippet'],
                    source_data['discovered_at']
                ))

            self.db._conn.commit()

    async def process_deduplication(self):
        """Main deduplication processing function."""

        print("Starting event deduplication process...")

        # Get all enriched events
        events = self.get_enriched_events_for_deduplication()
        print(f"Found {len(events)} enriched events to process")

        if not events:
            print("No events to deduplicate")
            return

        # Group similar events
        print("Grouping similar events...")
        event_groups = self.group_similar_events(events)

        unique_groups = [g for g in event_groups if len(g) == 1]
        duplicate_groups = [g for g in event_groups if len(g) > 1]

        print(f"Found {len(unique_groups)} unique events and {len(duplicate_groups)} groups with duplicates")

        # Process each group
        deduplicated_count = 0
        total_events_processed = 0

        for i, group in enumerate(event_groups, 1):
            print(f"\nProcessing group {i}/{len(event_groups)} ({len(group)} events)")

            # Create deduplicated event
            deduplicated_event_id = self.create_deduplicated_event(group)

            # Create mappings
            self.create_event_mappings(deduplicated_event_id, group)

            # Create cluster info
            self.create_deduplication_cluster(deduplicated_event_id, group)

            # Consolidate data sources
            self.consolidate_data_sources(deduplicated_event_id, group)

            print(f"  Created deduplicated event: {deduplicated_event_id}")
            print(f"  Title: {group[0]['title'][:60]}...")

            deduplicated_count += 1
            total_events_processed += len(group)

        print(f"\nDeduplication complete!")
        print(f"  Processed: {total_events_processed} enriched events")
        print(f"  Created: {deduplicated_count} deduplicated events")
        print(f"  Reduction: {total_events_processed - deduplicated_count} duplicate events merged")


async def main():
    """Main function."""

    db = CyberEventDataV2()
    processor = DeduplicationProcessor(db)

    try:
        await processor.process_deduplication()
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())