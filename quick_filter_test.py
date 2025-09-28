#!/usr/bin/env python3
"""
Quick test script to validate the LLM filter tuning approach with a small sample.
"""

import asyncio
import os
import pandas as pd
import logging
import requests
from bs4 import BeautifulSoup

from experimental_llm_classifier import ExperimentalLLMClassifier

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def quick_test():
    """Run a quick test with a few events to validate the approach."""

    # Check API key
    openai_api_key = os.getenv('OPENAI_API_KEY')
    if not openai_api_key:
        print("ERROR: OPENAI_API_KEY environment variable not set")
        return

    # Load training data
    df = pd.read_excel("./machine_learning_filter/event_training_data.xlsx")

    # Select a few representative events
    test_events = [
        # Should keep (specific incidents)
        {"idx": 2, "expected": True, "description": "Specific ransomware attack"},
        {"idx": 5, "expected": True, "description": "Specific data theft incident"},

        # Should reject (general reports)
        {"idx": 0, "expected": False, "description": "General multi-incident report"},
        {"idx": 1, "expected": False, "description": "Regulatory report"},
    ]

    print("Quick Filter Test Results:")
    print("=" * 80)

    # Test with default prompt
    classifier = ExperimentalLLMClassifier(openai_api_key, prompt_variant="default")

    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    })

    correct = 0
    total = 0

    for test_event in test_events:
        row = df.iloc[test_event["idx"]]
        title = row['raw_title']
        url = row['source_url']
        expected = test_event["expected"]

        print(f"\nTesting: {title[:60]}...")
        print(f"URL: {url}")
        print(f"Expected: {'KEEP' if expected else 'REJECT'}")

        # Scrape content
        try:
            response = session.get(url, timeout=30)
            soup = BeautifulSoup(response.content, 'html.parser')
            for element in soup(['script', 'style', 'nav', 'header', 'footer']):
                element.decompose()
            content = soup.get_text()[:2000]  # Limit content
        except Exception as e:
            print(f"Scraping failed: {e}")
            content = row.get('raw_description', '') + ' ' + row.get('raw_content', '')

        # Test LLM classification
        try:
            result = await classifier.classify_single_event_detailed(
                title=title,
                content=content,
                description=row.get('raw_description', '')
            )

            predicted = result.is_cybersecurity_event and result.is_australian_relevant
            is_correct = predicted == expected

            print(f"Predicted: {'KEEP' if predicted else 'REJECT'}")
            print(f"Confidence: {result.overall_confidence:.3f}")
            print(f"Cyber: {result.is_cybersecurity_event}, Australian: {result.is_australian_relevant}")
            print(f"Correct: {'✓' if is_correct else '✗'}")

            if result.rejection_reason:
                print(f"Rejection reason: {result.rejection_reason}")

            if result.detected_indicators:
                print(f"Detected indicators: {', '.join(result.detected_indicators[:3])}")

            if is_correct:
                correct += 1
            total += 1

        except Exception as e:
            print(f"LLM classification failed: {e}")

    print(f"\n{'='*80}")
    print(f"Quick Test Summary: {correct}/{total} correct ({correct/total*100:.1f}%)")
    print(f"{'='*80}")

if __name__ == "__main__":
    asyncio.run(quick_test())