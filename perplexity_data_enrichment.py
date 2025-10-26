#!/usr/bin/env python3
"""
Perplexity API Data Enrichment Script for EnrichedEvents Table

This script uses the Perplexity API to intelligently fill in missing data:
1. Extract and correct event dates
2. Identify attack victim entities
3. Determine vulnerability details
4. Estimate records affected counts
5. Improve data quality with AI-powered analysis

The script processes events in batches and uses structured prompts to get
consistent, high-quality data enrichment.
"""

import sqlite3
import logging
import json
import time
import requests
from datetime import datetime, date
from typing import List, Dict, Any, Optional, Tuple
import re
from dataclasses import dataclass
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import queue
import os
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class EnrichmentResult:
    """Result of Perplexity enrichment for a single event"""
    event_id: str
    corrected_date: Optional[str] = None
    victim_entity: Optional[str] = None
    vulnerability_details: Optional[str] = None
    records_affected: Optional[int] = None
    confidence_score: float = 0.0
    reasoning: str = ""


class PerplexityEnricher:
    """Handles Perplexity API enrichment for cyber events with multithreading"""
    
    def __init__(self, db_path: str, api_key: str, max_threads: int = 12):
        self.db_path = db_path
        self.api_key = api_key
        self.api_url = "https://api.perplexity.ai/chat/completions"
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        self.max_threads = max_threads
        self.enrichment_stats = {
            'events_processed': 0,
            'dates_corrected': 0,
            'entities_identified': 0,
            'vulnerabilities_identified': 0,
            'records_estimated': 0,
            'api_calls_made': 0,
            'errors': 0
        }
        self.stats_lock = threading.Lock()
    
    def get_events_needing_enrichment(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get events that need enrichment"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT enriched_event_id, title, summary, event_date, 
                   attack_victim_entity, vulnerability_details, records_affected
            FROM EnrichedEvents 
            WHERE status = 'Active'
            AND (
                event_date IS NULL OR event_date = '' OR
                attack_victim_entity IS NULL OR attack_victim_entity = '' OR attack_victim_entity = 'Unknown Organization' OR
                vulnerability_details IS NULL OR vulnerability_details = '' OR vulnerability_details = 'Cyber attack, Data breach' OR
                records_affected IS NULL OR records_affected <= 0
            )
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,))
        
        events = []
        for row in cursor.fetchall():
            events.append({
                'enriched_event_id': row[0],
                'title': row[1],
                'summary': row[2],
                'event_date': row[3],
                'attack_victim_entity': row[4],
                'vulnerability_details': row[5],
                'records_affected': row[6]
            })
        
        conn.close()
        return events
    
    def create_enrichment_prompt(self, event: Dict[str, Any]) -> str:
        """Create a structured prompt for Perplexity API"""
        title = event.get('title', '')
        summary = event.get('summary', '')
        current_date = event.get('event_date', '')
        current_entity = event.get('attack_victim_entity', '')
        current_vulnerability = event.get('vulnerability_details', '')
        current_records = event.get('records_affected', '')
        
        prompt = f"""
Analyze this cyber security incident and provide structured information:

TITLE: {title}
SUMMARY: {summary}
CURRENT DATE: {current_date}
CURRENT ENTITY: {current_entity}
CURRENT VULNERABILITY: {current_vulnerability}
CURRENT RECORDS AFFECTED: {current_records}

Please provide a JSON response with the following structure:
{{
    "corrected_date": "YYYY-MM-DD format, or null if cannot determine",
    "victim_entity": "Name of the organization/company that was attacked, or null if unclear",
    "vulnerability_details": "Specific attack method/vulnerability used, or null if unclear",
    "records_affected": "Number of records/people affected (integer), or null if cannot estimate",
    "confidence_score": "0.0 to 1.0 confidence in the analysis",
    "reasoning": "Brief explanation of the analysis"
}}

Focus on:
1. Extract the actual date of the incident (not when it was reported)
2. Identify the specific organization that was attacked
3. Determine the attack method/vulnerability used
4. Estimate the number of people/records affected
5. Be conservative with estimates - only provide data you're confident about

Respond with ONLY the JSON object, no additional text.
"""
        return prompt
    
    def call_perplexity_api(self, prompt: str) -> Optional[Dict[str, Any]]:
        """Call Perplexity API with the prompt"""
        try:
            payload = {
                "model": "sonar-pro",
                "messages": [
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                "max_tokens": 1000,
                "temperature": 0.1
            }
            
            response = requests.post(
                self.api_url,
                headers=self.headers,
                json=payload,
                timeout=30
            )
            
            if response.status_code == 200:
                data = response.json()
                content = data['choices'][0]['message']['content'].strip()
                
                # Try to parse JSON response
                try:
                    # Remove any markdown formatting
                    if content.startswith('```json'):
                        content = content[7:]
                    if content.endswith('```'):
                        content = content[:-3]
                    
                    result = json.loads(content)
                    with self.stats_lock:
                        self.enrichment_stats['api_calls_made'] += 1
                    return result
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse JSON response: {e}")
                    logger.warning(f"Response content: {content}")
                    return None
            else:
                logger.error(f"API call failed: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Error calling Perplexity API: {e}")
            with self.stats_lock:
                self.enrichment_stats['errors'] += 1
            return None
    
    def enrich_event(self, event: Dict[str, Any]) -> EnrichmentResult:
        """Enrich a single event using Perplexity API"""
        prompt = self.create_enrichment_prompt(event)
        api_result = self.call_perplexity_api(prompt)
        
        if not api_result:
            return EnrichmentResult(
                event_id=event['enriched_event_id'],
                confidence_score=0.0,
                reasoning="API call failed"
            )
        
        # Process the API result
        result = EnrichmentResult(
            event_id=event['enriched_event_id'],
            confidence_score=api_result.get('confidence_score', 0.0),
            reasoning=api_result.get('reasoning', '')
        )
        
        # Only update fields if confidence is reasonable
        if result.confidence_score >= 0.3:
            result.corrected_date = api_result.get('corrected_date')
            result.victim_entity = api_result.get('victim_entity')
            result.vulnerability_details = api_result.get('vulnerability_details')
            result.records_affected = api_result.get('records_affected')
        
        return result
    
    def apply_enrichment(self, result: EnrichmentResult) -> bool:
        """Apply enrichment result to database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Build update query dynamically
            updates = []
            params = []
            
            if result.corrected_date:
                updates.append("event_date = ?")
                params.append(result.corrected_date)
                with self.stats_lock:
                    self.enrichment_stats['dates_corrected'] += 1
            
            if result.victim_entity:
                updates.append("attack_victim_entity = ?")
                params.append(result.victim_entity)
                with self.stats_lock:
                    self.enrichment_stats['entities_identified'] += 1
            
            if result.vulnerability_details:
                updates.append("vulnerability_details = ?")
                params.append(result.vulnerability_details)
                with self.stats_lock:
                    self.enrichment_stats['vulnerabilities_identified'] += 1
            
            if result.records_affected:
                updates.append("records_affected = ?")
                params.append(result.records_affected)
                with self.stats_lock:
                    self.enrichment_stats['records_estimated'] += 1
            
            if updates:
                # Add data quality notes
                updates.append("data_quality_notes = ?")
                params.append(f"Perplexity enriched: {result.reasoning}")
                
                # Add updated timestamp
                updates.append("updated_at = CURRENT_TIMESTAMP")
                
                # Add the event ID
                params.append(result.event_id)
                
                query = f"""
                    UPDATE EnrichedEvents 
                    SET {', '.join(updates)}
                    WHERE enriched_event_id = ?
                """
                
                cursor.execute(query, params)
                conn.commit()
                conn.close()
                return True
            
            conn.close()
            return False
            
        except Exception as e:
            logger.error(f"Error applying enrichment for event {result.event_id}: {e}")
            with self.stats_lock:
                self.enrichment_stats['errors'] += 1
            return False
    
    def enrich_single_event(self, event: Dict[str, Any]) -> Tuple[bool, str]:
        """Enrich a single event (thread-safe)"""
        try:
            logger.info(f"Processing event: {event['title'][:50]}...")
            
            # Enrich the event
            result = self.enrich_event(event)
            
            # Apply enrichment if successful
            if result.confidence_score >= 0.3:
                if self.apply_enrichment(result):
                    with self.stats_lock:
                        self.enrichment_stats['events_processed'] += 1
                    logger.info(f"✅ Enriched event {result.event_id} (confidence: {result.confidence_score:.2f})")
                    return True, f"Success: {result.event_id}"
                else:
                    logger.warning(f"⚠️ Failed to apply enrichment for event {result.event_id}")
                    return False, f"Failed to apply: {result.event_id}"
            else:
                logger.warning(f"⚠️ Low confidence enrichment for event {result.event_id} (confidence: {result.confidence_score:.2f})")
                return False, f"Low confidence: {result.event_id}"
                
        except Exception as e:
            logger.error(f"Error processing event {event.get('enriched_event_id', 'unknown')}: {e}")
            with self.stats_lock:
                self.enrichment_stats['errors'] += 1
            return False, f"Error: {str(e)}"
    
    def enrich_events_multithreaded(self, batch_size: int = 100, delay_seconds: float = 0.1) -> Dict[str, Any]:
        """Enrich events using multithreaded processing"""
        logger.info(f"Starting multithreaded Perplexity enrichment with {self.max_threads} threads")
        
        events = self.get_events_needing_enrichment(limit=batch_size)
        total_events = len(events)
        logger.info(f"Found {total_events} events needing enrichment")
        
        if total_events == 0:
            logger.info("No events need enrichment")
            return self.enrichment_stats
        
        successful = 0
        failed = 0
        
        # Use ThreadPoolExecutor for concurrent processing
        with ThreadPoolExecutor(max_workers=self.max_threads) as executor:
            # Submit all tasks
            future_to_event = {
                executor.submit(self.enrich_single_event, event): event 
                for event in events
            }
            
            # Process completed tasks
            for future in as_completed(future_to_event):
                event = future_to_event[future]
                try:
                    success, message = future.result()
                    if success:
                        successful += 1
                    else:
                        failed += 1
                        logger.warning(f"Failed to enrich event {event['enriched_event_id']}: {message}")
                    
                    # Small delay to avoid overwhelming the API
                    time.sleep(delay_seconds)
                    
                except Exception as e:
                    failed += 1
                    logger.error(f"Exception processing event {event['enriched_event_id']}: {e}")
                    with self.stats_lock:
                        self.enrichment_stats['errors'] += 1
        
        logger.info(f"Multithreaded enrichment complete: {successful}/{total_events} events successfully enriched")
        return self.enrichment_stats
    
    def update_data_quality_flags(self):
        """Update data quality flags after enrichment"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            UPDATE EnrichedEvents 
            SET has_complete_date = (event_date IS NOT NULL AND event_date != ''),
                has_complete_entity = (attack_victim_entity IS NOT NULL AND attack_victim_entity != '' AND attack_victim_entity != 'Unknown Organization'),
                has_complete_vulnerability = (vulnerability_details IS NOT NULL AND vulnerability_details != '' AND vulnerability_details != 'Cyber attack, Data breach'),
                has_complete_records_count = (records_affected IS NOT NULL AND records_affected > 0),
                data_completeness_score = (
                    CASE WHEN (event_date IS NOT NULL AND event_date != '') THEN 1 ELSE 0 END +
                    CASE WHEN (attack_victim_entity IS NOT NULL AND attack_victim_entity != '' AND attack_victim_entity != 'Unknown Organization') THEN 1 ELSE 0 END +
                    CASE WHEN (vulnerability_details IS NOT NULL AND vulnerability_details != '' AND vulnerability_details != 'Cyber attack, Data breach') THEN 1 ELSE 0 END +
                    CASE WHEN (records_affected IS NOT NULL AND records_affected > 0) THEN 1 ELSE 0 END
                ) / 4.0,
                updated_at = CURRENT_TIMESTAMP
            WHERE status = 'Active'
        """)
        
        conn.commit()
        conn.close()
        logger.info("Updated data quality flags")
    
    def generate_enrichment_report(self) -> Dict[str, Any]:
        """Generate enrichment report"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Get current statistics
        cursor.execute("""
            SELECT 
                COUNT(*) as total_events,
                COUNT(CASE WHEN event_date IS NOT NULL AND event_date != '' THEN 1 END) as events_with_dates,
                COUNT(CASE WHEN attack_victim_entity IS NOT NULL AND attack_victim_entity != '' AND attack_victim_entity != 'Unknown Organization' THEN 1 END) as events_with_entities,
                COUNT(CASE WHEN vulnerability_details IS NOT NULL AND vulnerability_details != '' AND vulnerability_details != 'Cyber attack, Data breach' THEN 1 END) as events_with_vulnerabilities,
                COUNT(CASE WHEN records_affected IS NOT NULL AND records_affected > 0 THEN 1 END) as events_with_records,
                AVG(data_completeness_score) as avg_completeness_score
            FROM EnrichedEvents 
            WHERE status = 'Active'
        """)
        
        stats = cursor.fetchone()
        conn.close()
        
        return {
            'enrichment_stats': self.enrichment_stats,
            'current_state': {
                'total_events': stats[0],
                'events_with_dates': stats[1],
                'events_with_entities': stats[2],
                'events_with_vulnerabilities': stats[3],
                'events_with_records': stats[4],
                'avg_completeness_score': stats[5]
            }
        }
    
    def close(self):
        """Close method for compatibility (no connection to close)"""
        pass


def main():
    """Main enrichment function"""
    import argparse
    
    # Load environment variables from .env file
    load_dotenv()
    
    parser = argparse.ArgumentParser(description='Enrich EnrichedEvents table using Perplexity API')
    parser.add_argument('--db-path', default='instance/cyber_events.db', help='Path to database file')
    parser.add_argument('--api-key', help='Perplexity API key (or set PERPLEXITY_API_KEY in .env file)')
    parser.add_argument('--batch-size', type=int, default=100, help='Number of events to process in this run')
    parser.add_argument('--threads', type=int, default=12, help='Number of concurrent threads')
    parser.add_argument('--delay', type=float, default=0.1, help='Delay between API calls in seconds')
    parser.add_argument('--verbose', action='store_true', help='Enable verbose logging')
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Get API key from .env file or command line
    api_key = args.api_key or os.getenv('PERPLEXITY_API_KEY')
    if not api_key:
        logger.error("Perplexity API key is required. Set PERPLEXITY_API_KEY in .env file or use --api-key")
        return
    
    # Initialize enricher
    enricher = PerplexityEnricher(args.db_path, api_key, max_threads=args.threads)
    
    try:
        logger.info(f"🤖 Starting multithreaded Perplexity API enrichment with {args.threads} threads...")
        
        # Run multithreaded enrichment
        stats = enricher.enrich_events_multithreaded(
            batch_size=args.batch_size,
            delay_seconds=args.delay
        )
        
        # Update data quality flags
        enricher.update_data_quality_flags()
        
        # Generate report
        report = enricher.generate_enrichment_report()
        
        print("\n" + "="*60)
        print("MULTITHREADED PERPLEXITY ENRICHMENT REPORT")
        print("="*60)
        print(f"Threads used: {args.threads}")
        print(f"Events processed: {stats['events_processed']}")
        print(f"API calls made: {stats['api_calls_made']}")
        print(f"Errors: {stats['errors']}")
        print(f"\nImprovements made:")
        print(f"  - Dates corrected: {stats['dates_corrected']}")
        print(f"  - Entities identified: {stats['entities_identified']}")
        print(f"  - Vulnerabilities identified: {stats['vulnerabilities_identified']}")
        print(f"  - Records estimated: {stats['records_estimated']}")
        print(f"\nCurrent state:")
        print(f"  - Total events: {report['current_state']['total_events']}")
        print(f"  - Events with dates: {report['current_state']['events_with_dates']}")
        print(f"  - Events with entities: {report['current_state']['events_with_entities']}")
        print(f"  - Events with vulnerabilities: {report['current_state']['events_with_vulnerabilities']}")
        print(f"  - Events with records: {report['current_state']['events_with_records']}")
        print(f"  - Average completeness score: {report['current_state']['avg_completeness_score']:.2f}")
        print("="*60)
        
    except KeyboardInterrupt:
        logger.info("Enrichment interrupted by user")
    except Exception as e:
        logger.error(f"Enrichment failed: {e}")
    finally:
        enricher.close()


if __name__ == "__main__":
    main()
