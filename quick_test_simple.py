"""
Simple Quick Test - Re-process specific events from previous test results

This avoids database schema issues by using events from the JSON results file.
"""

import json
import os
from dotenv import load_dotenv
from cyber_data_collector.enrichment.high_quality_enrichment_pipeline import HighQualityEnrichmentPipeline

load_dotenv()


def test_events():
    """Test specific events with new settings"""

    # Load previous test results
    results = json.load(open('batch_enrichment_results_20251028_174300.json'))

    # Initialize pipeline with new settings
    openai_api_key = os.getenv('OPENAI_API_KEY')
    perplexity_api_key = os.getenv('PERPLEXITY_API_KEY')

    pipeline = HighQualityEnrichmentPipeline(
        db_path='instance/cyber_events.db',
        openai_api_key=openai_api_key,
        perplexity_api_key=perplexity_api_key
    )

    # Test cases: Event IDs to test
    test_event_ids = [
        '19af3f4f-ceea-49b0-9bc7-21d05119662c',  # Event #9 - TPG/iiNet (was REJECT)
        'b5440b85-7922-4ca3-b2c1-ef3feb1714ec',  # Event #2 - Hanson Chambers (should stay REJECT)
        '782a04ff-0f50-4b67-915a-9c3fd22ead02',  # Event #1 - iiNet (was AUTO_ACCEPT)
        'b6b1717e-289c-46de-8b6c-5a1c4ec0f1e9',  # Event #7 - iiNet (was AUTO_ACCEPT)
    ]

    print("=" * 100)
    print("QUICK TEST: Retest #3 Changes Verification")
    print("=" * 100)

    test_results = []

    for event_id in test_event_ids:
        # Find event in previous results
        prev_event = next((e for e in results['events'] if e['event_id'] == event_id), None)

        if not prev_event:
            print(f"\n[X] Event {event_id} not found in previous results")
            continue

        print(f"\n{'*' * 100}")
        print(f"Testing: {prev_event['title'][:80]}")
        print(f"Previous Result (Retest #2): {prev_event['decision']} ({prev_event['confidence']:.2f})")
        print('*' * 100)

        # Create event dict for pipeline
        event = {
            'event_id': event_id,
            'title': prev_event['title'],
            'url': prev_event['url']
        }

        # Test with new pipeline
        try:
            result = pipeline.enrich_event(event)

            # Extract results
            is_specific = result['enrichment_result'].get('specificity', {}).get('is_specific_incident')
            victim = result['enrichment_result'].get('victim', {}).get('organization', 'None')
            australian_rel = result['enrichment_result'].get('australian_relevance', {}).get('relevance_score', 0)
            decision = result['final_decision']['decision']
            confidence = result['final_decision']['final_confidence']
            overrides = result['validation_result'].get('specificity_overrides', [])
            penalties = result['final_decision'].get('applied_penalties', {})

            print(f"\n[+] NEW Results:")
            print(f"  Victim: {victim}")
            print(f"  is_specific: {is_specific}")
            print(f"  Australian relevance: {australian_rel:.2f}")
            print(f"  Overrides: {len(overrides)}")
            if overrides:
                for override in overrides:
                    print(f"    - {override['original']} -> {override['override']}: {override['reason'][:60]}...")
            print(f"  Penalties: {penalties}")
            print(f"  Final Confidence: {confidence:.2f}")
            print(f"  Decision: {decision}")

            # Show change
            prev_decision = prev_event['decision']
            prev_confidence = prev_event['confidence']

            if decision != prev_decision:
                print(f"\n  [>] CHANGED: {prev_decision} ({prev_confidence:.2f}) -> {decision} ({confidence:.2f})")
            else:
                conf_change = confidence - prev_confidence
                print(f"\n  [=] SAME: {decision} (confidence: {prev_confidence:.2f} -> {confidence:.2f}, {conf_change:+.2f})")

            test_results.append({
                'event_id': event_id,
                'title': prev_event['title'],
                'prev_decision': prev_decision,
                'prev_confidence': prev_confidence,
                'new_decision': decision,
                'new_confidence': confidence,
                'overrides': len(overrides),
                'changed': decision != prev_decision
            })

        except Exception as e:
            print(f"[X] Error: {e}")
            import traceback
            traceback.print_exc()

    # Summary
    print("\n" + "=" * 100)
    print("SUMMARY")
    print("=" * 100)

    changes = sum(1 for r in test_results if r['changed'])
    print(f"\nEvents Changed: {changes}/{len(test_results)}")
    print()

    for i, r in enumerate(test_results, 1):
        status = "[>]" if r['changed'] else "[=]"
        print(f"{i}. {status} {r['title'][:60]}")
        print(f"   {r['prev_decision']} ({r['prev_confidence']:.2f}) -> {r['new_decision']} ({r['new_confidence']:.2f})")
        if r['overrides'] > 0:
            print(f"   Validation overrides: {r['overrides']}")
        print()

    # Event #9 Analysis
    event9 = next((r for r in test_results if '19af3f4f' in r['event_id']), None)
    if event9:
        print("=" * 100)
        print("KEY TEST: Event #9 (TPG/iiNet)")
        print("=" * 100)
        print(f"Previous: {event9['prev_decision']} ({event9['prev_confidence']:.2f})")
        print(f"New: {event9['new_decision']} ({event9['new_confidence']:.2f})")
        print(f"Overrides: {event9['overrides']}")
        print()
        if event9['new_decision'] in ['AUTO_ACCEPT', 'ACCEPT_WITH_WARNING']:
            print("[+] SUCCESS: Event #9 is now ACCEPTED")
            print("   The 'flags' keyword and/or softened penalty worked!")
        elif event9['new_confidence'] > event9['prev_confidence']:
            print("[~] PARTIAL: Event #9 confidence improved but still REJECT")
            print(f"   Improved from {event9['prev_confidence']:.2f} to {event9['new_confidence']:.2f}")
        else:
            print("[X] NO CHANGE: Event #9 still REJECT with same/lower confidence")


if __name__ == '__main__':
    test_events()
