"""
Test High-Quality Enrichment Pipeline

This script tests the new 5-stage enrichment pipeline on sample events
to validate accuracy improvements over the old regex-based system.
"""

import os
import sys
import sqlite3
import logging
import json
from datetime import datetime
from typing import List, Dict, Any

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cyber_data_collector.enrichment.high_quality_enrichment_pipeline import HighQualityEnrichmentPipeline
from dotenv import load_dotenv


def setup_logging():
    """Setup logging configuration"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('high_quality_enrichment_test.log'),
            logging.StreamHandler(sys.stdout)
        ]
    )


def load_sample_events(db_path: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Load sample events for testing.

    Prioritizes events with known issues:
    1. Events without identified victims
    2. Events with generic victim names
    3. Events marked as non-specific incorrectly
    """

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Get diverse sample of events with different issue types
    queries = [
        # Events without victims (currently)
        """
        SELECT e.enriched_event_id, e.title, r.source_url as url, e.summary,
               NULL as victim_organization
        FROM EnrichedEvents e
        JOIN RawEvents r ON e.raw_event_id = r.raw_event_id
        LEFT JOIN EnrichedEventEntities ee ON e.enriched_event_id = ee.enriched_event_id
            AND ee.relationship_type = 'victim'
        WHERE ee.entity_id IS NULL
          AND e.status = 'Active'
          AND r.source_url IS NOT NULL
          AND r.source_url != ''
        ORDER BY e.event_date DESC
        LIMIT 3
        """,

        # Events with potentially generic victims
        """
        SELECT e.enriched_event_id, e.title, r.source_url as url, e.summary,
               ent.entity_name as victim_organization
        FROM EnrichedEvents e
        JOIN RawEvents r ON e.raw_event_id = r.raw_event_id
        JOIN EnrichedEventEntities ee ON e.enriched_event_id = ee.enriched_event_id
        JOIN EntitiesV2 ent ON ee.entity_id = ent.entity_id
        WHERE ee.relationship_type = 'victim'
          AND (ent.entity_name LIKE '%Australian%'
           OR ent.entity_name LIKE '%company%'
           OR ent.entity_name LIKE '%organization%')
          AND e.status = 'Active'
          AND r.source_url IS NOT NULL
          AND r.source_url != ''
        ORDER BY e.event_date DESC
        LIMIT 3
        """,

        # Recent high-profile events (should work well)
        """
        SELECT e.enriched_event_id, e.title, r.source_url as url, e.summary,
               ent.entity_name as victim_organization
        FROM EnrichedEvents e
        JOIN RawEvents r ON e.raw_event_id = r.raw_event_id
        JOIN EnrichedEventEntities ee ON e.enriched_event_id = ee.enriched_event_id
        JOIN EntitiesV2 ent ON ee.entity_id = ent.entity_id
        WHERE ee.relationship_type = 'victim'
          AND ent.entity_name IN ('Optus', 'Medibank', 'Latitude Financial', 'Toll Group')
          AND e.status = 'Active'
          AND r.source_url IS NOT NULL
          AND r.source_url != ''
        ORDER BY e.event_date DESC
        LIMIT 2
        """,

        # Random recent events
        """
        SELECT e.enriched_event_id, e.title, r.source_url as url, e.summary,
               ent.entity_name as victim_organization
        FROM EnrichedEvents e
        JOIN RawEvents r ON e.raw_event_id = r.raw_event_id
        LEFT JOIN EnrichedEventEntities ee ON e.enriched_event_id = ee.enriched_event_id
            AND ee.relationship_type = 'victim'
        LEFT JOIN EntitiesV2 ent ON ee.entity_id = ent.entity_id
        WHERE e.status = 'Active'
          AND r.source_url IS NOT NULL
          AND r.source_url != ''
          AND e.event_date >= '2023-01-01'
        ORDER BY RANDOM()
        LIMIT 2
        """
    ]

    events = []
    for query in queries:
        cursor.execute(query)
        rows = cursor.fetchall()
        for row in rows:
            events.append({
                'enriched_event_id': row['enriched_event_id'],
                'title': row['title'],
                'url': row['url'],
                'summary': row['summary'] if 'summary' in row.keys() else None,
                'old_victim_organization': row['victim_organization'] if 'victim_organization' in row.keys() else None
            })

    conn.close()

    # Remove duplicates and limit to requested number
    seen_ids = set()
    unique_events = []
    for event in events:
        if event['enriched_event_id'] not in seen_ids:
            seen_ids.add(event['enriched_event_id'])
            unique_events.append(event)
            if len(unique_events) >= limit:
                break

    return unique_events


def test_pipeline_on_events(pipeline: HighQualityEnrichmentPipeline,
                           events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Test the pipeline on sample events and collect results.
    """

    results = {
        'total_events': len(events),
        'successful': 0,
        'failed': 0,
        'auto_accept': 0,
        'accept_with_warning': 0,
        'rejected': 0,
        'events': []
    }

    for i, event in enumerate(events):
        print(f"\n{'='*100}")
        print(f"TEST {i+1}/{len(events)}: {event['enriched_event_id']}")
        print(f"{'='*100}")
        print(f"Title: {event['title'][:100]}...")
        print(f"URL: {event['url']}")
        print(f"Old Victim: {event.get('old_victim_organization', 'N/A')}")
        print(f"{'='*100}\n")

        try:
            # Run enrichment pipeline
            result = pipeline.enrich_event(event)

            # Extract key information
            decision = result['final_decision']['decision']
            confidence = result['final_decision']['final_confidence']
            new_victim = result['enrichment_result'].get('victim', {}).get('organization', 'None')

            # Update counters
            results['successful'] += 1
            if decision == 'AUTO_ACCEPT':
                results['auto_accept'] += 1
            elif decision == 'ACCEPT_WITH_WARNING':
                results['accept_with_warning'] += 1
            else:
                results['rejected'] += 1

            # Store event result
            event_result = {
                'event_id': event['enriched_event_id'],
                'title': event['title'],
                'url': event['url'],
                'old_victim': event.get('old_victim_organization'),
                'new_victim': new_victim,
                'decision': decision,
                'final_confidence': confidence,
                'stage_confidences': result['final_decision']['stage_confidences'],
                'fact_check_summary': result['final_decision']['fact_check_summary'],
                'validation_errors': result['validation_result']['errors'],
                'validation_warnings': result['validation_result']['warnings'],
                'extraction_notes': result['enrichment_result'].get('extraction_notes', ''),
                'is_specific_incident': result['enrichment_result'].get('specificity', {}).get('is_specific_incident'),
                'australian_relevance': result['enrichment_result'].get('australian_relevance', {}).get('relevance_score'),
                'audit_trail': result['audit_trail']
            }

            results['events'].append(event_result)

            # Print summary
            print(f"\n{'─'*100}")
            print(f"RESULT: {decision} (confidence: {confidence:.2f})")
            print(f"New Victim: {new_victim}")
            print(f"Old Victim: {event.get('old_victim_organization', 'N/A')}")
            print(f"Is Specific Incident: {event_result['is_specific_incident']}")
            print(f"Australian Relevance: {event_result['australian_relevance']:.2f}")
            print(f"\nStage Confidences:")
            for stage, conf in event_result['stage_confidences'].items():
                print(f"  {stage}: {conf:.2f}")
            print(f"\nFact-Check: {result['final_decision']['fact_check_summary']['checks_passed']}/{result['final_decision']['fact_check_summary']['checks_performed']} passed")

            if event_result['validation_errors']:
                print(f"\nValidation Errors:")
                for error in event_result['validation_errors']:
                    print(f"  ✗ {error}")

            if event_result['validation_warnings']:
                print(f"\nValidation Warnings (showing first 3):")
                for warning in event_result['validation_warnings'][:3]:
                    print(f"  ⚠ {warning}")

            print(f"{'─'*100}\n")

        except Exception as e:
            print(f"\n✗ PIPELINE FAILED: {e}")
            import traceback
            traceback.print_exc()

            results['failed'] += 1
            results['events'].append({
                'event_id': event['enriched_event_id'],
                'title': event['title'],
                'url': event['url'],
                'old_victim': event.get('old_victim_organization'),
                'error': str(e),
                'decision': 'ERROR'
            })

    return results


def generate_report(results: Dict[str, Any]) -> str:
    """Generate comprehensive test report"""

    report = f"""
{'='*100}
HIGH-QUALITY ENRICHMENT PIPELINE TEST REPORT
{'='*100}

Test Date: {datetime.now().isoformat()}
Total Events Tested: {results['total_events']}

OVERALL RESULTS:
{'─'*100}
Successful:           {results['successful']:>3} ({results['successful']/results['total_events']*100:.1f}%)
Failed:               {results['failed']:>3} ({results['failed']/results['total_events']*100:.1f}%)

DECISION BREAKDOWN:
{'─'*100}
AUTO_ACCEPT:          {results['auto_accept']:>3} ({results['auto_accept']/results['total_events']*100:.1f}%) - High confidence, ready to store
ACCEPT_WITH_WARNING:  {results['accept_with_warning']:>3} ({results['accept_with_warning']/results['total_events']*100:.1f}%) - Medium confidence, flag for review
REJECTED:             {results['rejected']:>3} ({results['rejected']/results['total_events']*100:.1f}%) - Low confidence, do not store

"""

    # Victim identification improvement
    report += f"\nVICTIM IDENTIFICATION COMPARISON:\n{'─'*100}\n"

    improvements = 0
    unchanged = 0
    worse = 0

    for event in results['events']:
        if event.get('decision') == 'ERROR':
            continue

        old_victim = event.get('old_victim', 'None')
        new_victim = event.get('new_victim', 'None')

        # Count improvements
        if old_victim in [None, 'None', 'Unknown', ''] and new_victim not in [None, 'None', 'Unknown', '']:
            improvements += 1
            report += f"\n✓ IMPROVEMENT: {event['event_id'][:15]}...\n"
            report += f"  Title: {event['title'][:70]}...\n"
            report += f"  Old: {old_victim} → New: {new_victim}\n"
            report += f"  Confidence: {event['final_confidence']:.2f}\n"

        elif old_victim != new_victim and old_victim not in [None, 'None', 'Unknown', '']:
            if new_victim in [None, 'None', 'Unknown', '']:
                worse += 1
                report += f"\n✗ WORSE: {event['event_id'][:15]}...\n"
                report += f"  Title: {event['title'][:70]}...\n"
                report += f"  Old: {old_victim} → New: {new_victim}\n"
                report += f"  Reason: {event.get('extraction_notes', 'N/A')}\n"
            else:
                # Different victim - could be correction
                report += f"\n⚠ CHANGED: {event['event_id'][:15]}...\n"
                report += f"  Title: {event['title'][:70]}...\n"
                report += f"  Old: {old_victim} → New: {new_victim}\n"
                report += f"  Confidence: {event['final_confidence']:.2f}\n"
        else:
            unchanged += 1

    report += f"\n{'─'*100}\n"
    report += f"Improvements:  {improvements:>3} (found victim where old system didn't)\n"
    report += f"Unchanged:     {unchanged:>3} (victim same as before)\n"
    report += f"Worse:         {worse:>3} (lost victim that old system had)\n"

    # Confidence distribution
    report += f"\n\nCONFIDENCE SCORE DISTRIBUTION:\n{'─'*100}\n"

    confidences = [e['final_confidence'] for e in results['events'] if 'final_confidence' in e]
    if confidences:
        avg_conf = sum(confidences) / len(confidences)
        min_conf = min(confidences)
        max_conf = max(confidences)

        report += f"Average:  {avg_conf:.2f}\n"
        report += f"Min:      {min_conf:.2f}\n"
        report += f"Max:      {max_conf:.2f}\n"

    report += f"\n{'='*100}\n"

    return report


def main():
    """Main test execution"""

    # Set UTF-8 encoding for Windows console
    if sys.platform == 'win32':
        import codecs
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
        sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')

    # Load environment variables
    load_dotenv()

    setup_logging()
    logger = logging.getLogger(__name__)

    print(f"\n{'='*100}")
    print("HIGH-QUALITY ENRICHMENT PIPELINE TEST")
    print(f"{'='*100}\n")

    # Database path (standard location)
    db_path = "instance/cyber_events.db"

    # Get API keys
    openai_api_key = os.getenv('OPENAI_API_KEY')
    perplexity_api_key = os.getenv('PERPLEXITY_API_KEY')

    if not openai_api_key:
        print("ERROR: OPENAI_API_KEY environment variable not set")
        print("Please set it with: export OPENAI_API_KEY='your-key-here'")
        return 1

    if not perplexity_api_key:
        print("ERROR: PERPLEXITY_API_KEY environment variable not set")
        print("Please set it with: export PERPLEXITY_API_KEY='your-key-here'")
        return 1

    # Initialize pipeline
    print("Initializing High-Quality Enrichment Pipeline...")
    pipeline = HighQualityEnrichmentPipeline(
        db_path=db_path,
        openai_api_key=openai_api_key,
        perplexity_api_key=perplexity_api_key
    )
    print("✓ Pipeline initialized\n")

    # Load sample events
    print(f"Loading sample events from database: {db_path}")
    events = load_sample_events(db_path, limit=10)
    print(f"✓ Loaded {len(events)} sample events\n")

    # Test pipeline
    print("Starting pipeline test...\n")
    results = test_pipeline_on_events(pipeline, events)

    # Generate report
    report = generate_report(results)
    print(report)

    # Save detailed results to JSON
    output_file = f"enrichment_test_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\n✓ Detailed results saved to: {output_file}")
    print(f"✓ Full logs saved to: high_quality_enrichment_test.log")

    return 0


if __name__ == '__main__':
    sys.exit(main())
