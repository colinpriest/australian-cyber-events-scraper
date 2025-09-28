#!/usr/bin/env python3
"""
Debug script to understand why Random Forest filter is rejecting valid events.
"""

import pandas as pd
import logging
from rf_event_filter import RfEventFilter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def debug_rf_vs_training_data():
    """Compare Random Forest predictions with training data labels."""

    # Load training data
    df = pd.read_excel("./machine_learning_filter/event_training_data.xlsx")

    # Initialize Random Forest filter
    try:
        rf_filter = RfEventFilter()
    except Exception as e:
        print(f"Failed to load Random Forest filter: {e}")
        return

    print("RANDOM FOREST vs TRAINING DATA COMPARISON")
    print("=" * 80)

    correct_predictions = 0
    total_predictions = 0
    false_positives = []
    false_negatives = []

    for idx, row in df.iterrows():
        title = row['raw_title']
        description = row.get('raw_description', '')
        content = row.get('raw_content', '')
        url = row['source_url']
        source_type = row.get('source_type', 'unknown')
        expected_keep = bool(row['filter_keep'])

        # Get Random Forest prediction
        try:
            result = rf_filter.should_keep_event(
                source_type=source_type,
                title=title,
                description=description,
                content=content,
                url=url
            )

            predicted_keep = result.is_cyber_relevant
            confidence = result.confidence_score

            is_correct = predicted_keep == expected_keep

            if is_correct:
                correct_predictions += 1
            else:
                if expected_keep and not predicted_keep:
                    false_negatives.append({
                        'title': title,
                        'confidence': confidence,
                        'reasoning': result.reasoning
                    })
                elif not expected_keep and predicted_keep:
                    false_positives.append({
                        'title': title,
                        'confidence': confidence,
                        'reasoning': result.reasoning
                    })

            total_predictions += 1

            if not is_correct:
                print(f"\n❌ MISMATCH:")
                print(f"Title: {title[:80]}...")
                print(f"Expected: {'KEEP' if expected_keep else 'REJECT'}")
                print(f"RF Predicted: {'KEEP' if predicted_keep else 'REJECT'}")
                print(f"RF Confidence: {confidence:.3f}")

        except Exception as e:
            print(f"Error processing event {idx}: {e}")

    accuracy = correct_predictions / total_predictions if total_predictions > 0 else 0

    print(f"\n{'='*80}")
    print(f"OVERALL PERFORMANCE:")
    print(f"Total events: {total_predictions}")
    print(f"Correct predictions: {correct_predictions}")
    print(f"Accuracy: {accuracy:.1%}")
    print(f"False positives: {len(false_positives)}")
    print(f"False negatives: {len(false_negatives)}")

    if false_negatives:
        print(f"\n❌ FALSE NEGATIVES (should keep but RF rejects):")
        for i, fn in enumerate(false_negatives[:5], 1):
            print(f"{i}. {fn['title'][:70]}...")
            print(f"   Confidence: {fn['confidence']:.3f}")

    if false_positives:
        print(f"\n❌ FALSE POSITIVES (should reject but RF keeps):")
        for i, fp in enumerate(false_positives[:5], 1):
            print(f"{i}. {fp['title'][:70]}...")
            print(f"   Confidence: {fp['confidence']:.3f}")

    return accuracy, false_negatives, false_positives

def analyze_specific_patterns():
    """Analyze specific patterns that might be causing issues."""

    # Load training data
    df = pd.read_excel("./machine_learning_filter/event_training_data.xlsx")

    print(f"\n{'='*80}")
    print("PATTERN ANALYSIS:")
    print("="*80)

    # Events that should be kept
    keep_events = df[df['filter_keep'] == 1]
    print(f"\nEvents that should be KEPT ({len(keep_events)}):")
    for title in keep_events['raw_title'].head(10):
        print(f"  ✓ {title}")

    # Events that should be rejected
    reject_events = df[df['filter_keep'] == 0]
    print(f"\nEvents that should be REJECTED ({len(reject_events)}):")
    for title in reject_events['raw_title'].head(10):
        print(f"  ✗ {title}")

    # Look for patterns in titles
    print(f"\nKEY PATTERNS:")
    print("Events to KEEP typically:")
    print("- Name specific organizations (Toll Group, Perth Mint)")
    print("- Describe individual incidents")
    print("- Use past tense (was hacked, suffered breach)")

    print("\nEvents to REJECT typically:")
    print("- Use words like 'Multiple', 'Various', 'Several'")
    print("- Describe time periods (January 2020, Q1 2020)")
    print("- Are regulatory reports (OAIC, government summaries)")
    print("- Are policy documents (action plans, frameworks)")

def test_january_2020_events():
    """Test specifically with January 2020 events that are being filtered."""

    print(f"\n{'='*80}")
    print("JANUARY 2020 EVENT ANALYSIS:")
    print("="*80)

    # Sample events based on what we know are being processed
    test_events = [
        {
            "title": "Toll Group Ransomware Attack",
            "description": "Toll Group was hit by a ransomware attack affecting their systems",
            "expected": True,
            "reason": "Specific incident affecting named company"
        },
        {
            "title": "Multiple Cyber Incidents Reported in Australia (January 2020)",
            "description": "OAIC report summarizing various cybersecurity incidents",
            "expected": False,
            "reason": "General summary report, not specific incident"
        },
        {
            "title": "OAIC Notifiable Data Breaches: January–June 2020",
            "description": "Regulatory report covering multiple breach notifications",
            "expected": False,
            "reason": "Regulatory summary document"
        }
    ]

    try:
        rf_filter = RfEventFilter()

        for event in test_events:
            result = rf_filter.should_keep_event(
                source_type="test",
                title=event["title"],
                description=event["description"],
                content="",
                url=""
            )

            predicted = result.is_cyber_relevant
            expected = event["expected"]

            status = "✓ CORRECT" if predicted == expected else "❌ WRONG"

            print(f"\n{status}")
            print(f"Title: {event['title']}")
            print(f"Expected: {'KEEP' if expected else 'REJECT'} ({event['reason']})")
            print(f"RF Predicted: {'KEEP' if predicted else 'REJECT'}")
            print(f"RF Confidence: {result.confidence_score:.3f}")

    except Exception as e:
        print(f"Error testing events: {e}")

def main():
    """Main debug function."""

    print("DEBUGGING RANDOM FOREST FILTER")
    print("=" * 80)

    # Compare RF predictions with training data
    accuracy, false_negatives, false_positives = debug_rf_vs_training_data()

    # Analyze patterns
    analyze_specific_patterns()

    # Test specific events
    test_january_2020_events()

    print(f"\n{'='*80}")
    print("RECOMMENDATIONS:")
    print("="*80)

    if accuracy < 0.8:
        print("❌ Random Forest accuracy is too low!")
        print("Consider:")
        print("1. Retrain the model with better features")
        print("2. Adjust confidence threshold")
        print("3. Replace with rule-based filtering")
        print("4. Use LLM filtering only")
    else:
        print("✓ Random Forest performance is acceptable")

    if len(false_negatives) > 5:
        print(f"\n❌ Too many false negatives ({len(false_negatives)})")
        print("This is causing valid events to be filtered out")
        print("Consider lowering confidence threshold or improving model")

    print(f"\n💡 IMMEDIATE FIX:")
    print("To get more events through to LLM enhancement:")
    print("1. Lower RF confidence threshold in discover_enrich_events.py")
    print("2. Or bypass RF filter temporarily for debugging")
    print("3. Or replace RF filter with simpler rule-based pre-filter")

if __name__ == "__main__":
    main()