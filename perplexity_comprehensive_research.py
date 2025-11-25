#!/usr/bin/env python3
"""
Perplexity Comprehensive Research Script

Uses Perplexity's deep research capabilities to discover cyber events
by systematically researching time periods rather than individual searches.

This supplements the existing discovery sources by asking Perplexity to
comprehensively list all cyber events within specific date ranges.

Usage:
    # Research a specific month
    python perplexity_comprehensive_research.py --month 2025-09

    # Research multiple months
    python perplexity_comprehensive_research.py --start-month 2025-08 --end-month 2025-10

    # Research last N days
    python perplexity_comprehensive_research.py --days 70
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Add current directory to path
sys.path.append(str(Path(__file__).parent))

from cyber_event_data_v2 import CyberEventDataV2

try:
    import openai
except ImportError:
    print("Error: openai package not installed. Run: pip install openai")
    sys.exit(1)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('perplexity_research.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


class PerplexityComprehensiveResearcher:
    """
    Uses Perplexity to comprehensively research cyber events by time period.
    """

    def __init__(self, db_path: str = "instance/cyber_events.db"):
        self.db = CyberEventDataV2(db_path)
        self.api_key = os.getenv("PERPLEXITY_API_KEY")

        if not self.api_key:
            raise ValueError("PERPLEXITY_API_KEY environment variable is required")

        # Initialize Perplexity client
        self.client = openai.OpenAI(
            api_key=self.api_key,
            base_url="https://api.perplexity.ai"
        )

        # Retry configuration
        self.max_retries = 3
        self.retry_delay = 2.0

        # Statistics
        self.stats = {
            'months_processed': 0,
            'events_discovered': 0,
            'events_skipped_duplicate': 0,
            'api_calls': 0,
            'errors': 0
        }

    def research_month(self, year: int, month: int) -> List[Dict]:
        """
        Research all cyber events for a specific month using Perplexity.

        Args:
            year: Year (e.g., 2025)
            month: Month (1-12)

        Returns:
            List of discovered event dictionaries
        """
        # Calculate date range
        start_date = datetime(year, month, 1)

        if month == 12:
            end_date = datetime(year + 1, 1, 1) - timedelta(days=1)
        else:
            end_date = datetime(year, month + 1, 1) - timedelta(days=1)

        start_date_str = start_date.strftime("%Y-%m-%d")
        end_date_str = end_date.strftime("%Y-%m-%d")

        logger.info(f"Researching Australian cyber events from {start_date_str} to {end_date_str}")

        # Comprehensive research query
        research_query = f"""List ALL cyber security incidents, data breaches, ransomware attacks, and
cybersecurity events that occurred in Australia between {start_date_str} and {end_date_str}.

For EACH event, provide:
- Exact date (YYYY-MM-DD format, or best estimate if exact date unknown)
- Organization/victim name (be specific - company name, government agency, university, etc.)
- Type of incident (ransomware, data breach, DDoS, phishing, etc.)
- Brief description (1-2 sentences)
- Threat actor or ransomware group if known
- Any known impacts (records affected, downtime, ransom demand, etc.)

Include:
- Publicly reported incidents from news sources
- Ransomware group leak site announcements
- Government advisories and reports
- Court cases and regulatory actions related to breaches
- Both major incidents affecting large organizations AND smaller incidents affecting SMEs
- Educational institutions, healthcare, government, private sector

Be comprehensive - list every incident you can find, even minor ones. If you find 20+ incidents,
list them all. Provide specific details and cite sources."""

        try:
            # Call Perplexity API with retry logic
            response = None
            for attempt in range(self.max_retries):
                try:
                    response = self.client.chat.completions.create(
                        model="sonar-pro",  # Best model for research tasks
                        messages=[
                            {
                                "role": "system",
                                "content": "You are a cybersecurity research assistant specializing in Australian cyber events. "
                                         "Provide comprehensive, factual lists of incidents with specific details. "
                                         "Always cite sources for verification."
                            },
                            {
                                "role": "user",
                                "content": research_query
                            }
                        ],
                        temperature=0.2,  # Lower temperature for factual responses
                        top_p=0.9,
                        stream=False
                    )
                    self.stats['api_calls'] += 1
                    break
                except Exception as e:
                    if attempt < self.max_retries - 1:
                        logger.warning(f"API call failed (attempt {attempt + 1}/{self.max_retries}): {e}")
                        time.sleep(self.retry_delay * (attempt + 1))
                    else:
                        raise

            if not response:
                logger.error(f"Failed to get response from Perplexity for {start_date_str} to {end_date_str}")
                self.stats['errors'] += 1
                return []

            # Extract response text
            response_text = response.choices[0].message.content

            # Extract citations if available
            citations = []
            if hasattr(response, 'citations'):
                citations = response.citations

            logger.info(f"Perplexity research complete. Response length: {len(response_text)} chars")

            # Parse response to extract individual events
            events = self._parse_research_response(response_text, citations, start_date_str, end_date_str)

            logger.info(f"Discovered {len(events)} events for {year}-{month:02d} via Perplexity research")
            self.stats['months_processed'] += 1

            return events

        except Exception as e:
            logger.error(f"Error during Perplexity research for {year}-{month:02d}: {e}")
            self.stats['errors'] += 1
            return []

    def _parse_research_response(self,
                                 response_text: str,
                                 citations: List[str],
                                 start_date: str,
                                 end_date: str) -> List[Dict]:
        """
        Parse Perplexity's research response to extract individual events.

        Returns:
            List of event dictionaries ready for database insertion
        """
        events = []

        # Split response into potential event entries
        lines = response_text.split('\n')

        current_event_lines = []

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Check if line starts a new event
            is_new_event = (
                re.match(r'^\d+[\\.\\)]\\s+', line) or  # "1. " or "1) "
                re.match(r'^[-\\*]\\s+', line) or       # "- " or "* "
                re.match(r'^\\d{4}-\\d{2}-\\d{2}', line) or  # Date at start
                re.search(r'(January|February|March|April|May|June|July|August|September|October|November|December)\\s+\\d{1,2},?\\s+\\d{4}', line)
            )

            if is_new_event and current_event_lines:
                # Save previous event
                event = self._create_event_from_text(
                    '\\n'.join(current_event_lines),
                    citations,
                    start_date,
                    end_date
                )
                if event:
                    events.append(event)
                current_event_lines = [line]
            else:
                current_event_lines.append(line)

        # Don't forget the last event
        if current_event_lines:
            event = self._create_event_from_text(
                '\\n'.join(current_event_lines),
                citations,
                start_date,
                end_date
            )
            if event:
                events.append(event)

        # If we didn't find any clear event delimiters, treat sections separated by blank lines as events
        if len(events) == 0:
            paragraphs = response_text.split('\\n\\n')
            for para in paragraphs:
                para = para.strip()
                if len(para) > 50:  # Skip very short paragraphs
                    event = self._create_event_from_text(para, citations, start_date, end_date)
                    if event:
                        events.append(event)

        logger.info(f"Parsed {len(events)} events from Perplexity research response")
        return events

    def _create_event_from_text(self,
                                event_text: str,
                                citations: List[str],
                                start_date: str,
                                end_date: str) -> Optional[Dict]:
        """
        Create an event dictionary from parsed event text.

        Returns:
            Event dictionary ready for database insertion, or None if invalid
        """
        if len(event_text) < 30:  # Too short to be a real event
            return None

        # Extract title (first line or sentence)
        title_match = re.match(r'^[^\\n\\.]+', event_text)
        title = title_match.group(0).strip() if title_match else event_text[:100]

        # Clean up title (remove numbering, bullets, etc.)
        title = re.sub(r'^\\d+[\\.\\)]\\s+', '', title)
        title = re.sub(r'^[-\\*]\\s+', '', title)
        title = title.strip()

        # Try to extract date from text
        event_date_str = None

        # Try ISO format date
        date_pattern = r'(\\d{4}-\\d{2}-\\d{2})'
        date_match = re.search(date_pattern, event_text)
        if date_match:
            event_date_str = date_match.group(1)

        # Also try natural language dates
        if not event_date_str:
            month_pattern = r'(January|February|March|April|May|June|July|August|September|October|November|December)\\s+(\\d{1,2}),?\\s+(\\d{4})'
            month_match = re.search(month_pattern, event_text)
            if month_match:
                try:
                    month_name, day, year = month_match.groups()
                    event_date = datetime.strptime(f"{month_name} {day} {year}", "%B %d %Y")
                    event_date_str = event_date.strftime("%Y-%m-%d")
                except ValueError:
                    pass

        # Create unique source_event_id based on content
        content_hash = hashlib.md5(event_text.encode()).hexdigest()[:12]
        source_event_id = f"perplexity_research_{start_date}_{content_hash}"

        # Create pseudo-URL for tracking
        pseudo_url = f"perplexity://research/{start_date}/{content_hash}"

        # Create metadata
        metadata = {
            'research_period_start': start_date,
            'research_period_end': end_date,
            'citations': citations,
            'discovery_method': 'perplexity_comprehensive_research',
            'discovered_at': datetime.now().isoformat()
        }

        # Create event dictionary matching CyberEventDataV2 schema
        event = {
            'source_event_id': source_event_id,
            'title': title,
            'description': event_text,
            'content': event_text,  # Full text as content
            'event_date': event_date_str,  # May be None - enrichment will handle
            'source_url': pseudo_url,
            'metadata': metadata
        }

        return event

    def store_events(self, events: List[Dict]) -> int:
        """
        Store discovered events in the database.

        Returns:
            Number of events successfully stored (excluding duplicates)
        """
        stored_count = 0

        for event in events:
            try:
                # Check if event already exists
                existing_id = self.db.find_existing_raw_event(
                    source_type="PerplexityResearch",
                    source_url=event['source_url'],
                    title=event['title']
                )

                if existing_id:
                    logger.debug(f"Skipping duplicate event: {event['title'][:50]}...")
                    self.stats['events_skipped_duplicate'] += 1
                    continue

                # Add new raw event
                raw_event_id = self.db.add_raw_event(
                    source_type="PerplexityResearch",
                    raw_data=event
                )

                logger.info(f"Stored event: {event['title'][:60]}... (ID: {raw_event_id})")
                stored_count += 1
                self.stats['events_discovered'] += 1

            except Exception as e:
                logger.error(f"Error storing event '{event.get('title', 'Unknown')}': {e}")
                self.stats['errors'] += 1

        return stored_count

    def research_date_range(self, start_month: str, end_month: str):
        """
        Research all months between start and end (inclusive).

        Args:
            start_month: Start month in YYYY-MM format
            end_month: End month in YYYY-MM format
        """
        start_year, start_mon = map(int, start_month.split('-'))
        end_year, end_mon = map(int, end_month.split('-'))

        current = datetime(start_year, start_mon, 1)
        end = datetime(end_year, end_mon, 1)

        while current <= end:
            logger.info(f"\\n{'='*80}")
            logger.info(f"Processing {current.year}-{current.month:02d}")
            logger.info(f"{'='*80}")

            events = self.research_month(current.year, current.month)

            if events:
                stored = self.store_events(events)
                logger.info(f"Stored {stored} new events for {current.year}-{current.month:02d}")

            # Move to next month
            if current.month == 12:
                current = datetime(current.year + 1, 1, 1)
            else:
                current = datetime(current.year, current.month + 1, 1)

        self.print_summary()

    def print_summary(self):
        """Print execution summary."""
        logger.info("\\n" + "="*80)
        logger.info("PERPLEXITY COMPREHENSIVE RESEARCH SUMMARY")
        logger.info("="*80)
        logger.info(f"Months processed: {self.stats['months_processed']}")
        logger.info(f"Events discovered: {self.stats['events_discovered']}")
        logger.info(f"Events skipped (duplicates): {self.stats['events_skipped_duplicate']}")
        logger.info(f"API calls made: {self.stats['api_calls']}")
        logger.info(f"Errors encountered: {self.stats['errors']}")
        logger.info("="*80)


def main():
    parser = argparse.ArgumentParser(
        description="Use Perplexity to comprehensively research cyber events by time period"
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--month', help='Single month to research (YYYY-MM)')
    group.add_argument('--start-month', help='Start month for range (YYYY-MM)')
    group.add_argument('--days', type=int, help='Research last N days')

    parser.add_argument('--end-month', help='End month for range (YYYY-MM, use with --start-month)')
    parser.add_argument('--db-path', default='instance/cyber_events.db', help='Path to database')

    args = parser.parse_args()

    try:
        researcher = PerplexityComprehensiveResearcher(db_path=args.db_path)

        if args.month:
            # Single month
            year, month = map(int, args.month.split('-'))
            events = researcher.research_month(year, month)
            stored = researcher.store_events(events)
            logger.info(f"Stored {stored} new events")
            researcher.print_summary()

        elif args.start_month:
            # Date range
            if not args.end_month:
                parser.error("--end-month is required when using --start-month")
            researcher.research_date_range(args.start_month, args.end_month)

        elif args.days:
            # Last N days - convert to month range
            end_date = datetime.now()
            start_date = end_date - timedelta(days=args.days)

            start_month = start_date.strftime("%Y-%m")
            end_month = end_date.strftime("%Y-%m")

            logger.info(f"Researching last {args.days} days: {start_month} to {end_month}")
            researcher.research_date_range(start_month, end_month)

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
