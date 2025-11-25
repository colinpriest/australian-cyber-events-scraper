"""
Quick Test for Retest #3 Changes

Tests specific events to verify:
1. Event #9 (TPG/iiNet) now correctly classified
2. Softened penalty working as expected
3. Validation overrides triggering with new keywords
"""

import logging
import sys
import os
from dotenv import load_dotenv
from cyber_data_collector.enrichment.high_quality_enrichment_pipeline import HighQualityEnrichmentPipeline
import sqlite3

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


def test_event(pipeline, event_id, event_title, expected_decision):
    """Test a single event and show results"""

    print("\n" + "=" * 100)
    print(f"Testing: {event_title[:80]}")
    print(f"Event ID: {event_id}")
    print(f"Expected: {expected_decision}")
    print("=" * 100)

    # Fetch event from database
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
        print(f"❌ Event {event_id} not found")
        return None

    event = {
        'event_id': row[0],
        'title': row[1],
        'url': row[2],
        'date': row[3]
    }

    # Enrich event
    try:
        result = pipeline.enrich_event(event)

        # Extract key results
        is_specific = result['enrichment_result'].get('specificity', {}).get('is_specific_incident')
        victim = result['enrichment_result'].get('victim', {}).get('organization', 'None')
        australian_rel = result['enrichment_result'].get('australian_relevance', {}).get('relevance_score', 0)
        decision = result['final_decision']['decision']
        confidence = result['final_decision']['final_confidence']
        overrides = result['validation_result'].get('specificity_overrides', [])
        penalties = result['final_decision'].get('applied_penalties', {})

        # Show results
        print(f"\n✓ Results:")
        print(f"  Victim: {victim}")
        print(f"  is_specific_incident: {is_specific}")
        print(f"  Australian relevance: {australian_rel:.2f}")
        print(f"  Overrides: {len(overrides)}")
        if overrides:
            for override in overrides:
                print(f"    - {override['original']} → {override['override']}: {override['reason'][:60]}")
        print(f"  Penalties: {penalties}")
        print(f"  Final Confidence: {confidence:.2f}")
        print(f"  Decision: {decision}")

        # Check if matches expected
        match = "✓ PASS" if decision == expected_decision else "✗ FAIL"
        print(f"\n{match}: Expected {expected_decision}, Got {decision}")

        return {
            'event_id': event_id,
            'title': event_title,
            'victim': victim,
            'is_specific': is_specific,
            'australian_rel': australian_rel,
            'overrides': len(overrides),
            'confidence': confidence,
            'decision': decision,
            'expected': expected_decision,
            'passed': decision == expected_decision
        }

    except Exception as e:
        print(f"❌ Error processing event: {e}")
        import traceback
        traceback.print_exc()
        return None


def main():
    """Run quick tests"""

    print("=" * 100)
    print("QUICK TEST: Retest #3 Changes Verification")
    print("=" * 100)

    # Initialize pipeline
    openai_api_key = os.getenv('OPENAI_API_KEY')
    perplexity_api_key = os.getenv('PERPLEXITY_API_KEY')

    if not openai_api_key or not perplexity_api_key:
        logger.error("API keys not found in environment")
        return

    pipeline = HighQualityEnrichmentPipeline(
        db_path='instance/cyber_events.db',
        openai_api_key=openai_api_key,
        perplexity_api_key=perplexity_api_key
    )

    # Test cases
    test_cases = [
        {
            'event_id': '19af3f4f-ceea-49b0-9bc7-21d05119662c',
            'title': "Australia's TPG Telecom flags cyber incident in its iiNet system",
            'expected': 'AUTO_ACCEPT',  # Should be AUTO_ACCEPT or at least ACCEPT_WITH_WARNING
            'note': 'Event #9 - Was REJECT in Retest #2, should improve with "flags" keyword'
        },
        {
            'event_id': 'b5440b85-7922-4ca3-b2c1-ef3feb1714ec',
            'title': 'Ransomware Attack on Hanson Chambers',
            'expected': 'REJECT',
            'note': 'Non-Australian (UK law firm) - should stay REJECT'
        },
        {
            'event_id': '782a04ff-0f50-4b67-915a-9c3fd22ead02',
            'title': 'Australian Schools Increasingly Targeted by Cyber Attacks',
            'expected': 'AUTO_ACCEPT',
            'note': 'Event #1 - Was AUTO_ACCEPT in Retest #2, should stay AUTO_ACCEPT'
        },
        {
            'event_id': 'b6b1717e-289c-46de-8b6c-5a1c4ec0f1e9',
            'title': 'iiNet Data Breach Exposes 280,000+ Customer Records',
            'expected': 'AUTO_ACCEPT',
            'note': 'Event #7 - Was AUTO_ACCEPT in Retest #2, should stay AUTO_ACCEPT'
        }
    ]

    results = []

    for i, test_case in enumerate(test_cases, 1):
        print(f"\n{'*' * 100}")
        print(f"TEST {i}/4")
        print(f"Note: {test_case['note']}")
        print('*' * 100)

        result = test_event(
            pipeline,
            test_case['event_id'],
            test_case['title'],
            test_case['expected']
        )

        if result:
            results.append(result)

    # Summary
    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)

    passed = sum(1 for r in results if r['passed'])
    total = len(results)

    print(f"\nTests Passed: {passed}/{total} ({passed/total*100:.0f}%)")
    print()

    for i, result in enumerate(results, 1):
        status = "✓ PASS" if result['passed'] else "✗ FAIL"
        print(f"{i}. {status}: {result['title'][:60]}")
        print(f"   Expected: {result['expected']}, Got: {result['decision']} (confidence: {result['confidence']:.2f})")
        if result['overrides'] > 0:
            print(f"   Overrides: {result['overrides']}")
        print()

    # Highlight Event #9
    event9 = next((r for r in results if '19af3f4f' in r['event_id']), None)
    if event9:
        print("=" * 100)
        print("KEY TEST: Event #9 (TPG/iiNet)")
        print("=" * 100)
        print(f"Decision: {event9['decision']}")
        print(f"Confidence: {event9['confidence']:.2f}")
        print(f"Overrides: {event9['overrides']}")
        print(f"is_specific: {event9['is_specific']}")
        print()
        if event9['decision'] in ['AUTO_ACCEPT', 'ACCEPT_WITH_WARNING']:
            print("✓ SUCCESS: Event #9 is now ACCEPTED (was REJECT in Retest #2)")
        else:
            print("✗ FAILURE: Event #9 is still REJECTED")
            print("   This indicates the changes did not have the expected effect")


if __name__ == '__main__':
    main()
