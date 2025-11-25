"""
Batch Enrichment Script - Process multiple events through the high-quality enrichment pipeline

This script processes events in batches, stores results in the database,
and generates comprehensive reports.
"""

import os
import sys
import sqlite3
import logging
import json
from datetime import datetime
from typing import List, Dict, Any
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cyber_data_collector.enrichment.high_quality_enrichment_pipeline import HighQualityEnrichmentPipeline
from cyber_data_collector.enrichment.enrichment_audit_storage import EnrichmentAuditStorage
from dotenv import load_dotenv


def setup_logging(log_file: str):
    """Setup logging configuration"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ]
    )


def load_events_for_enrichment(db_path: str, limit: int = 100,
                               strategy: str = 'mixed') -> List[Dict[str, Any]]:
    """
    Load events for enrichment based on strategy.

    Strategies:
    - 'no_victims': Only events without identified victims
    - 'mixed': Mix of events with/without victims for comparison
    - 'recent': Most recent events
    - 'random': Random sample
    """

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Special handling for rerun_failed strategy
    if strategy == 'rerun_failed':
        # Load failed event IDs from file
        try:
            with open('failed_events_to_rerun.txt', 'r') as f:
                event_ids = [line.strip() for line in f if line.strip()]

            if not event_ids:
                conn.close()
                return []

            # Build parameterized query for these specific event IDs
            placeholders = ','.join(['?' for _ in event_ids])
            query = f"""
                SELECT e.enriched_event_id, e.title, r.source_url as url, e.summary, r.raw_content
                FROM EnrichedEvents e
                JOIN RawEvents r ON e.raw_event_id = r.raw_event_id
                WHERE e.enriched_event_id IN ({placeholders})
                AND e.status = 'Active'
                AND r.source_url IS NOT NULL
                AND r.source_url != ''
                ORDER BY e.event_date DESC
                LIMIT ?
            """
            cursor.execute(query, event_ids + [limit])
        except FileNotFoundError:
            logging.error("failed_events_to_rerun.txt not found! Run rerun_failed_events.py first.")
            conn.close()
            return []
    else:
        queries = {
            'no_victims': """
                SELECT e.enriched_event_id, e.title, r.source_url as url, e.summary, r.raw_content
                FROM EnrichedEvents e
                JOIN RawEvents r ON e.raw_event_id = r.raw_event_id
                LEFT JOIN EnrichedEventEntities ee ON e.enriched_event_id = ee.enriched_event_id
                    AND ee.relationship_type = "victim"
                WHERE ee.entity_id IS NULL
                AND e.status = 'Active'
                AND r.source_url IS NOT NULL
                AND r.source_url != ''
                ORDER BY e.event_date DESC
                LIMIT ?
            """,

            'mixed': """
                SELECT e.enriched_event_id, e.title, r.source_url as url, e.summary, r.raw_content
                FROM EnrichedEvents e
                JOIN RawEvents r ON e.raw_event_id = r.raw_event_id
                WHERE e.status = 'Active'
                AND r.source_url IS NOT NULL
                AND r.source_url != ''
                ORDER BY RANDOM()
                LIMIT ?
            """,

            'recent': """
                SELECT e.enriched_event_id, e.title, r.source_url as url, e.summary, r.raw_content
                FROM EnrichedEvents e
                JOIN RawEvents r ON e.raw_event_id = r.raw_event_id
                WHERE e.status = 'Active'
                AND r.source_url IS NOT NULL
                AND r.source_url != ''
                ORDER BY e.created_at DESC
                LIMIT ?
            """,

            'random': """
                SELECT e.enriched_event_id, e.title, r.source_url as url, e.summary, r.raw_content
                FROM EnrichedEvents e
                JOIN RawEvents r ON e.raw_event_id = r.raw_event_id
                WHERE e.status = 'Active'
                AND r.source_url IS NOT NULL
                AND r.source_url != ''
                ORDER BY RANDOM()
                LIMIT ?
            """
        }

        query = queries.get(strategy, queries['mixed'])
        cursor.execute(query, (limit,))

    events = []
    for row in cursor.fetchall():
        events.append({
            'enriched_event_id': row['enriched_event_id'],
            'title': row['title'],
            'url': row['url'],
            'summary': row['summary'],
            'cached_content': row['raw_content'] if row['raw_content'] else None
        })

    conn.close()
    return events


def process_batch(pipeline: HighQualityEnrichmentPipeline,
                 audit_storage: EnrichmentAuditStorage,
                 events: List[Dict[str, Any]],
                 save_to_db: bool = True) -> Dict[str, Any]:
    """
    Process a batch of events through the enrichment pipeline.

    Args:
        pipeline: Enrichment pipeline instance
        audit_storage: Audit storage instance
        events: List of events to process
        save_to_db: Whether to save audit trails to database

    Returns:
        Batch processing results with statistics
    """

    logger = logging.getLogger(__name__)

    results = {
        'total_events': len(events),
        'processed': 0,
        'failed': 0,
        'auto_accept': 0,
        'accept_with_warning': 0,
        'rejected': 0,
        'errors': 0,
        'events': [],
        'start_time': datetime.now().isoformat(),
        'processing_times': []
    }

    for i, event in enumerate(events):
        event_id = event['enriched_event_id']
        logger.info(f"\n{'='*100}")
        logger.info(f"Processing {i+1}/{len(events)}: {event_id}")
        logger.info(f"Title: {event['title'][:80]}...")
        logger.info(f"{'='*100}")

        start_time = time.time()

        try:
            # Run enrichment pipeline
            result = pipeline.enrich_event(event)

            processing_time = time.time() - start_time
            results['processing_times'].append(processing_time)

            # Extract key metrics
            decision = result['final_decision']['decision']
            confidence = result['final_decision']['final_confidence']
            new_victim = result['enrichment_result'].get('victim', {}).get('organization')

            # Update counters
            results['processed'] += 1
            if decision == 'AUTO_ACCEPT':
                results['auto_accept'] += 1
            elif decision == 'ACCEPT_WITH_WARNING':
                results['accept_with_warning'] += 1
            elif decision == 'REJECT':
                results['rejected'] += 1
            else:
                results['errors'] += 1

            # Save audit trail to database
            if save_to_db:
                try:
                    audit_id = audit_storage.save_audit_trail(result)
                    logger.info(f"Saved audit trail: {audit_id}")
                except Exception as e:
                    logger.error(f"Failed to save audit trail: {e}")

            # Store event result
            event_result = {
                'event_id': event_id,
                'title': event['title'],
                'url': event['url'],
                'decision': decision,
                'confidence': confidence,
                'victim': new_victim,
                'is_specific': result['enrichment_result'].get('specificity', {}).get('is_specific_incident'),
                'australian_relevance': result['enrichment_result'].get('australian_relevance', {}).get('relevance_score'),
                'fact_checks': {
                    'performed': result['fact_check_result']['checks_performed'],
                    'passed': result['fact_check_result']['checks_passed']
                },
                'validation': {
                    'errors': len(result['validation_result']['errors']),
                    'warnings': len(result['validation_result']['warnings'])
                },
                'processing_time': processing_time
            }

            results['events'].append(event_result)

            logger.info(f"Result: {decision} (confidence: {confidence:.2f})")
            logger.info(f"Victim: {new_victim or 'None'}")
            logger.info(f"Processing time: {processing_time:.1f}s")

        except Exception as e:
            logger.error(f"Failed to process event {event_id}: {e}")
            import traceback
            traceback.print_exc()

            results['failed'] += 1
            results['events'].append({
                'event_id': event_id,
                'title': event['title'],
                'url': event['url'],
                'error': str(e),
                'decision': 'ERROR'
            })

    results['end_time'] = datetime.now().isoformat()
    results['total_time'] = sum(results['processing_times'])
    results['avg_time'] = results['total_time'] / len(results['processing_times']) if results['processing_times'] else 0

    return results


def generate_report(results: Dict[str, Any], output_file: str):
    """Generate comprehensive batch processing report"""

    total = results['total_events']
    processed = results['processed']

    report = f"""
{'='*100}
BATCH ENRICHMENT REPORT - PHASE 1 TESTING
{'='*100}

Processing Summary:
{'-'*100}
Start Time:               {results['start_time']}
End Time:                 {results['end_time']}
Total Events:             {total}
Successfully Processed:   {processed} ({processed/total*100:.1f}%)
Failed:                   {results['failed']} ({results['failed']/total*100:.1f}%)

Decision Breakdown:
{'-'*100}
AUTO_ACCEPT:              {results['auto_accept']} ({results['auto_accept']/total*100:.1f}%)
ACCEPT_WITH_WARNING:      {results['accept_with_warning']} ({results['accept_with_warning']/total*100:.1f}%)
REJECT:                   {results['rejected']} ({results['rejected']/total*100:.1f}%)
ERROR:                    {results['errors']} ({results['errors']/total*100:.1f}%)

Performance Metrics:
{'-'*100}
Total Processing Time:    {results['total_time']:.1f} seconds
Average Time per Event:   {results['avg_time']:.1f} seconds
Estimated Cost:           ${total * 0.14:.2f}

"""

    # Victim identification analysis
    victims_found = sum(1 for e in results['events']
                       if e.get('victim') and e['victim'] not in ['None', 'Unknown', None])

    report += f"""
Victim Identification:
{'-'*100}
Events with Victim Found: {victims_found} ({victims_found/total*100:.1f}%)
Events without Victim:    {total - victims_found} ({(total-victims_found)/total*100:.1f}%)

"""

    # Confidence distribution
    confidences = [e['confidence'] for e in results['events'] if 'confidence' in e]
    if confidences:
        avg_conf = sum(confidences) / len(confidences)
        min_conf = min(confidences)
        max_conf = max(confidences)

        high_conf = sum(1 for c in confidences if c >= 0.8)
        med_conf = sum(1 for c in confidences if 0.5 <= c < 0.8)
        low_conf = sum(1 for c in confidences if c < 0.5)

        report += f"""
Confidence Distribution:
{'-'*100}
Average Confidence:       {avg_conf:.2f}
Min Confidence:           {min_conf:.2f}
Max Confidence:           {max_conf:.2f}

High (>= 0.8):            {high_conf} ({high_conf/len(confidences)*100:.1f}%)
Medium (0.5-0.8):         {med_conf} ({med_conf/len(confidences)*100:.1f}%)
Low (< 0.5):              {low_conf} ({low_conf/len(confidences)*100:.1f}%)

"""

    # Fact-checking statistics
    total_checks = sum(e['fact_checks']['performed'] for e in results['events'] if 'fact_checks' in e)
    total_passed = sum(e['fact_checks']['passed'] for e in results['events'] if 'fact_checks' in e)

    report += f"""
Fact-Checking Statistics:
{'-'*100}
Total Fact-Checks:        {total_checks}
Checks Passed:            {total_passed} ({total_passed/total_checks*100:.1f}% if total_checks > 0 else 0)
Checks Failed:            {total_checks - total_passed}

"""

    # Validation statistics
    total_errors = sum(e['validation']['errors'] for e in results['events'] if 'validation' in e)
    total_warnings = sum(e['validation']['warnings'] for e in results['events'] if 'validation' in e)

    report += f"""
Validation Statistics:
{'-'*100}
Total Validation Errors:  {total_errors}
Total Validation Warnings: {total_warnings}
Events with Errors:       {sum(1 for e in results['events'] if e.get('validation', {}).get('errors', 0) > 0)}
Events with Warnings:     {sum(1 for e in results['events'] if e.get('validation', {}).get('warnings', 0) > 0)}

"""

    # Sample results
    report += f"""
Sample Results (First 10):
{'-'*100}
"""

    for i, event in enumerate(results['events'][:10]):
        if 'error' in event:
            report += f"\n{i+1}. ERROR: {event['event_id'][:20]}...\n"
            report += f"   Error: {event['error'][:80]}...\n"
        else:
            report += f"\n{i+1}. {event['decision']} (conf: {event.get('confidence', 0):.2f})\n"
            report += f"   Event: {event['event_id'][:20]}...\n"
            report += f"   Title: {event['title'][:60]}...\n"
            report += f"   Victim: {event.get('victim') or 'None'}\n"
            report += f"   Time: {event['processing_time']:.1f}s\n"

    report += f"\n{'='*100}\n"
    report += f"Phase 1 Testing Complete!\n"
    report += f"{'='*100}\n"

    # Write report to file
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(report)

    return report


def main():
    """Main execution"""

    # Load environment
    load_dotenv()

    # Configuration
    db_path = "instance/cyber_events.db"
    batch_size = int(os.getenv('BATCH_SIZE', '100'))  # Default 100, can override
    strategy = os.getenv('ENRICHMENT_STRATEGY', 'no_victims')  # Can override with env var
    save_to_db = True

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = f"batch_enrichment_{timestamp}.log"
    results_file = f"batch_enrichment_results_{timestamp}.json"
    report_file = f"PHASE1_TEST_REPORT_{timestamp}.txt"

    setup_logging(log_file)
    logger = logging.getLogger(__name__)

    print(f"\n{'='*100}")
    print("PHASE 1 BATCH ENRICHMENT - 100 EVENT TEST")
    print(f"{'='*100}\n")

    # Check API keys
    openai_key = os.getenv('OPENAI_API_KEY')
    perplexity_key = os.getenv('PERPLEXITY_API_KEY')

    if not openai_key or not perplexity_key:
        print("ERROR: API keys not found in environment")
        print("Required: OPENAI_API_KEY and PERPLEXITY_API_KEY")
        return 1

    # Initialize pipeline
    print("Initializing enrichment pipeline...")
    pipeline = HighQualityEnrichmentPipeline(
        db_path=db_path,
        openai_api_key=openai_key,
        perplexity_api_key=perplexity_key
    )

    audit_storage = EnrichmentAuditStorage(db_path)
    print("Pipeline initialized\n")

    # Load events
    print(f"Loading {batch_size} events (strategy: {strategy})...")
    events = load_events_for_enrichment(db_path, limit=batch_size, strategy=strategy)
    print(f"Loaded {len(events)} events\n")

    if len(events) == 0:
        print("ERROR: No events found to process")
        return 1

    print(f"Estimated cost: ${len(events) * 0.14:.2f}")
    print(f"Estimated time: {len(events) * 35 / 60:.1f} minutes\n")

    response = input("Proceed with batch enrichment? (y/n): ").strip().lower()
    if response != 'y':
        print("Batch enrichment cancelled")
        return 0

    # Process batch
    print("\nStarting batch processing...\n")
    results = process_batch(pipeline, audit_storage, events, save_to_db)

    # Save detailed results to JSON
    with open(results_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\n\nDetailed results saved to: {results_file}")

    # Generate report
    print("\nGenerating report...")
    report = generate_report(results, report_file)
    print(report)

    print(f"\nReport saved to: {report_file}")
    print(f"Log saved to: {log_file}")

    print("\n" + "="*100)
    print("PHASE 1 TESTING COMPLETE!")
    print("="*100)
    print(f"\nProcessed: {results['processed']}/{results['total_events']} events")
    print(f"Auto-accept: {results['auto_accept']} ({results['auto_accept']/results['total_events']*100:.1f}%)")
    print(f"Rejected: {results['rejected']} ({results['rejected']/results['total_events']*100:.1f}%)")
    print(f"Average confidence: {sum(e.get('confidence', 0) for e in results['events'])/len(results['events']):.2f}")
    print(f"\nNext step: Review report and analyze quality improvements")
    print("="*100)

    return 0


if __name__ == '__main__':
    sys.exit(main())
