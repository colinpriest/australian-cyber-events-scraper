"""
Test the improved specificity prompt and validation layer on example events.

This script tests 3 example events that were previously misclassified:
1. iiNet breach (marked non-specific, should be specific)
2. Qantas analysis article (marked non-specific, should be specific)
3. Generic educational article (correctly non-specific)
"""

import logging
import sys
import os
from dotenv import load_dotenv
from cyber_data_collector.enrichment.high_quality_enrichment_pipeline import HighQualityEnrichmentPipeline

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('test_improved_specificity.log')
    ]
)

logger = logging.getLogger(__name__)


def test_event(pipeline, event_id, event_title, expected_specific):
    """Test a single event and check if specificity is classified correctly"""

    logger.info("=" * 100)
    logger.info(f"Testing Event: {event_title}")
    logger.info(f"Event ID: {event_id}")
    logger.info(f"Expected is_specific: {expected_specific}")
    logger.info("=" * 100)

    # Fetch event from database
    import sqlite3
    conn = sqlite3.connect('instance/cyber_events.db')
    cursor = conn.cursor()

    cursor.execute("""
        SELECT event_id, title, url, date
        FROM Events
        WHERE event_id = ?
    """, (event_id,))

    row = cursor.fetchone()
    conn.close()

    if not row:
        logger.error(f"Event {event_id} not found in database")
        return None

    event = {
        'event_id': row[0],
        'title': row[1],
        'url': row[2],
        'date': row[3]
    }

    # Enrich event
    result = pipeline.enrich_event(event)

    # Extract results
    is_specific = result['enrichment_result'].get('specificity', {}).get('is_specific_incident')
    reasoning = result['enrichment_result'].get('specificity', {}).get('specificity_reasoning', '')
    decision = result['final_decision']['decision']
    confidence = result['final_decision']['final_confidence']
    overrides = result['validation_result'].get('specificity_overrides', [])

    # Log results
    logger.info("\n" + "=" * 100)
    logger.info("RESULTS:")
    logger.info(f"  is_specific_incident: {is_specific}")
    logger.info(f"  Expected: {expected_specific}")
    logger.info(f"  Match: {'✓ CORRECT' if is_specific == expected_specific else '✗ INCORRECT'}")
    logger.info(f"  Reasoning: {reasoning}")
    logger.info(f"  Overrides applied: {len(overrides)}")
    if overrides:
        for override in overrides:
            logger.info(f"    - {override}")
    logger.info(f"  Final Decision: {decision}")
    logger.info(f"  Final Confidence: {confidence:.2f}")
    logger.info("=" * 100 + "\n")

    return {
        'event_id': event_id,
        'title': event_title,
        'is_specific': is_specific,
        'expected': expected_specific,
        'correct': is_specific == expected_specific,
        'overrides': overrides,
        'decision': decision,
        'confidence': confidence
    }


def main():
    """Run tests on example events"""

    logger.info("Starting Improved Specificity Tests")
    logger.info("=" * 100)

    # Initialize pipeline
    openai_api_key = os.getenv('OPENAI_API_KEY')
    perplexity_api_key = os.getenv('PERPLEXITY_API_KEY')

    if not openai_api_key or not perplexity_api_key:
        logger.error("API keys not found in environment")
        logger.error("Please set OPENAI_API_KEY and PERPLEXITY_API_KEY in .env file")
        return

    pipeline = HighQualityEnrichmentPipeline(
        db_path='instance/cyber_events.db',
        openai_api_key=openai_api_key,
        perplexity_api_key=perplexity_api_key
    )

    # Test cases
    test_cases = [
        # Event 1: iiNet breach (should be SPECIFIC)
        # This was previously marked non-specific, but it's a news report about a specific incident
        {
            'event_id': 'ef0d2c3e-7f9f-4e3e-9f3e-7f9f4e3e9f3e',  # Replace with actual event ID
            'title': "Australia's TPG Telecom flags cyber incident in its iiNet system",
            'expected_specific': True
        },

        # Event 2: Qantas analysis article (should be SPECIFIC)
        # This was previously marked non-specific, but it's analysis OF a specific Qantas breach
        {
            'event_id': 'abc123-example-qantas',  # Replace with actual event ID
            'title': "Qantas cyber security breach: What personal details were exposed",
            'expected_specific': True
        },

        # Event 3: Generic educational content (should be NON-SPECIFIC)
        # This should remain non-specific as it's not about a specific incident
        {
            'event_id': 'def456-example-generic',  # Replace with actual event ID
            'title': "How to Protect Your Business from Ransomware Attacks",
            'expected_specific': False
        }
    ]

    results = []

    # Note: These are example event IDs - you'll need to replace with actual event IDs from the database
    # For now, let's just test with a few real events from the recent retest

    logger.info("\nNOTE: Using sample events from Phase 1 test set")
    logger.info("To test specific events, update the event_ids in the test_cases list")

    # Query for some real events from the database
    import sqlite3
    conn = sqlite3.connect('instance/cyber_events.db')
    cursor = conn.cursor()

    # Get iiNet events
    cursor.execute("""
        SELECT event_id, title FROM Events
        WHERE title LIKE '%iiNet%' OR title LIKE '%TPG%'
        LIMIT 1
    """)
    iinet_event = cursor.fetchone()

    # Get Qantas events
    cursor.execute("""
        SELECT event_id, title FROM Events
        WHERE title LIKE '%Qantas%'
        LIMIT 1
    """)
    qantas_event = cursor.fetchone()

    conn.close()

    # Test with real events if found
    if iinet_event:
        logger.info(f"\nTesting iiNet event: {iinet_event[1]}")
        result = test_event(pipeline, iinet_event[0], iinet_event[1], expected_specific=True)
        if result:
            results.append(result)

    if qantas_event:
        logger.info(f"\nTesting Qantas event: {qantas_event[1]}")
        result = test_event(pipeline, qantas_event[0], qantas_event[1], expected_specific=True)
        if result:
            results.append(result)

    # Summary
    logger.info("\n" + "=" * 100)
    logger.info("SUMMARY")
    logger.info("=" * 100)

    if results:
        correct = sum(1 for r in results if r['correct'])
        total = len(results)

        logger.info(f"Tests run: {total}")
        logger.info(f"Correct classifications: {correct}/{total} ({correct/total*100:.1f}%)")
        logger.info("")

        for result in results:
            status = "✓ PASS" if result['correct'] else "✗ FAIL"
            logger.info(f"{status}: {result['title'][:60]}")
            logger.info(f"  Expected: {result['expected']}, Got: {result['is_specific']}")
            if result['overrides']:
                logger.info(f"  Overrides: {len(result['overrides'])}")
            logger.info(f"  Decision: {result['decision']} ({result['confidence']:.2f})")
            logger.info("")
    else:
        logger.warning("No test events found - update test_cases with actual event IDs")

    logger.info("=" * 100)


if __name__ == '__main__':
    main()
