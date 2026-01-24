#!/usr/bin/env python3
"""
Test script to fine-tune LLM filtering against training data.

This script:
1. Loads events from event_training_data.xlsx
2. Scrapes URLs to get content
3. Applies LLM filtering with different prompts and thresholds
4. Compares results with the filter_keep column
5. Provides metrics and recommendations for optimization
"""

import asyncio
import logging
import os
import pandas as pd
import requests
from typing import Dict, List, Optional, Tuple
import json
from datetime import datetime
import re
from dataclasses import dataclass
from urllib.parse import urlparse
import time
from bs4 import BeautifulSoup

# Import existing components
from cyber_data_collector.processing.llm_classifier import LLMClassifier, EventEnhancement
from cyber_data_collector.models.events import (
    ConfidenceScore,
    CyberEvent,
    CyberEventType,
    EventSeverity,
    EventSource,
)

@dataclass
class TestResult:
    """Result of testing LLM filter on a single event."""
    event_id: str
    title: str
    url: str
    expected_keep: bool
    llm_decision: Optional[bool]
    llm_confidence: Optional[float]
    llm_rejection_reason: Optional[str]
    scraped_content_length: int
    scraping_success: bool
    error_message: Optional[str]

@dataclass
class FilteringMetrics:
    """Metrics for evaluating filtering performance."""
    total_events: int
    successful_scrapes: int
    successful_llm_calls: int
    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int

    @property
    def precision(self) -> float:
        """Precision = TP / (TP + FP)"""
        if self.true_positives + self.false_positives == 0:
            return 0.0
        return self.true_positives / (self.true_positives + self.false_positives)

    @property
    def recall(self) -> float:
        """Recall = TP / (TP + FN)"""
        if self.true_positives + self.false_negatives == 0:
            return 0.0
        return self.true_positives / (self.true_positives + self.false_negatives)

    @property
    def f1_score(self) -> float:
        """F1 Score = 2 * (precision * recall) / (precision + recall)"""
        if self.precision + self.recall == 0:
            return 0.0
        return 2 * (self.precision * self.recall) / (self.precision + self.recall)

    @property
    def accuracy(self) -> float:
        """Accuracy = (TP + TN) / Total"""
        total_classified = self.true_positives + self.false_positives + self.true_negatives + self.false_negatives
        if total_classified == 0:
            return 0.0
        return (self.true_positives + self.true_negatives) / total_classified

class WebScraper:
    """Simple web scraper for getting article content."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })

    def scrape_url(self, url: str, timeout: int = 30) -> Tuple[bool, str, str]:
        """
        Scrape content from URL.

        Returns:
            (success, content, error_message)
        """
        try:
            response = self.session.get(url, timeout=timeout)
            response.raise_for_status()

            # Parse HTML and extract text
            soup = BeautifulSoup(response.content, 'html.parser')

            # Remove script and style elements
            for script in soup(["script", "style"]):
                script.decompose()

            # Get text content
            text = soup.get_text()

            # Clean up text
            lines = (line.strip() for line in text.splitlines())
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            text = ' '.join(chunk for chunk in chunks if chunk)

            return True, text, ""

        except Exception as e:
            return False, "", str(e)

class LLMFilterTester:
    """Test harness for fine-tuning LLM filtering."""

    def __init__(self, openai_api_key: str, training_data_path: str):
        self.openai_api_key = openai_api_key
        self.training_data_path = training_data_path
        self.scraper = WebScraper()
        self.results = []

        # Setup logging
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(__name__)

    def load_training_data(self) -> pd.DataFrame:
        """Load training data from Excel file."""
        self.logger.info(f"Loading training data from {self.training_data_path}")
        df = pd.read_excel(self.training_data_path)

        self.logger.info(f"Loaded {len(df)} events")
        self.logger.info(f"filter_keep distribution: {df['filter_keep'].value_counts().to_dict()}")

        return df

    async def test_filtering_configuration(self,
                                         df: pd.DataFrame,
                                         prompt_variant: str = "default",
                                         model: str = "gpt-4o-mini",
                                         confidence_threshold: float = 0.5,
                                         max_events: Optional[int] = None) -> FilteringMetrics:
        """
        Test a specific LLM filtering configuration.

        Args:
            df: Training data DataFrame
            prompt_variant: Which prompt variant to use
            model: OpenAI model to use
            confidence_threshold: Threshold for accepting events
            max_events: Limit number of events to test (for faster iteration)
        """

        # Create modified LLM classifier
        classifier = self._create_modified_classifier(prompt_variant, model)

        # Limit events if requested
        test_df = df.head(max_events) if max_events else df

        self.logger.info(f"Testing {len(test_df)} events with prompt={prompt_variant}, model={model}, threshold={confidence_threshold}")

        results = []

        for idx, row in test_df.iterrows():
            result = await self._test_single_event(row, classifier, confidence_threshold)
            results.append(result)

            # Log progress every 10 events
            if (idx + 1) % 10 == 0:
                self.logger.info(f"Processed {idx + 1}/{len(test_df)} events")

        self.results.extend(results)

        # Calculate metrics
        metrics = self._calculate_metrics(results)

        self.logger.info(f"Results: Precision={metrics.precision:.3f}, Recall={metrics.recall:.3f}, F1={metrics.f1_score:.3f}, Accuracy={metrics.accuracy:.3f}")

        return metrics, results

    async def _test_single_event(self, row: pd.Series, classifier: LLMClassifier, threshold: float) -> TestResult:
        """Test LLM filtering on a single event."""

        event_id = row['raw_event_id']
        title = row['raw_title']
        url = row['source_url']
        expected_keep = bool(row['filter_keep'])

        # Scrape content
        scraping_success, content, scrape_error = self.scraper.scrape_url(url)

        llm_decision = None
        llm_confidence = None
        llm_rejection_reason = None
        error_message = scrape_error

        if scraping_success and content:
            try:
                # Create a minimal CyberEvent for testing
                test_event = CyberEvent(
                    title=title,
                    description=row.get('raw_description', ''),
                    event_type=CyberEventType.OTHER,
                    severity=EventSeverity.UNKNOWN,
                    australian_relevance=True,
                    confidence=ConfidenceScore(
                        overall=0.5,
                        source_reliability=0.5,
                        data_completeness=0.5,
                        temporal_accuracy=0.5,
                        geographic_accuracy=0.5,
                    ),
                    data_sources=[
                        EventSource(
                            source_id=f"test-{event_id}",
                            source_type="test",
                            content_snippet=content[:2000],  # Limit content
                            credibility_score=0.5,
                            relevance_score=0.5,
                        )
                    ],
                )

                # Apply LLM classification
                enhanced_event = await classifier._enhance_single_event(test_event)

                if enhanced_event is None:
                    # Event was rejected
                    llm_decision = False
                    llm_confidence = 0.0

                    # Try to get rejection reason from LLM response
                    # This is a simplified approach - in reality we'd need to modify the classifier
                    # to return the enhancement object even for rejected events
                    llm_rejection_reason = "Event rejected by LLM"
                else:
                    # Event was accepted
                    llm_decision = True
                    llm_confidence = 1.0  # Simplified - actual confidence would need classifier modification

            except Exception as e:
                error_message = f"LLM error: {str(e)}"
                self.logger.warning(f"LLM failed for event {event_id}: {e}")

        return TestResult(
            event_id=event_id,
            title=title,
            url=url,
            expected_keep=expected_keep,
            llm_decision=llm_decision,
            llm_confidence=llm_confidence,
            llm_rejection_reason=llm_rejection_reason,
            scraped_content_length=len(content) if scraping_success else 0,
            scraping_success=scraping_success,
            error_message=error_message
        )

    def _create_modified_classifier(self, prompt_variant: str, model: str) -> LLMClassifier:
        """Create an LLM classifier with modified prompt."""

        # For now, we'll use the existing classifier
        # In a full implementation, we'd modify the prompt based on prompt_variant
        classifier = LLMClassifier(self.openai_api_key)

        # TODO: Modify the classifier to use different prompts and models
        # This would require extending the LLMClassifier class

        return classifier

    def _calculate_metrics(self, results: List[TestResult]) -> FilteringMetrics:
        """Calculate performance metrics from test results."""

        total_events = len(results)
        successful_scrapes = sum(1 for r in results if r.scraping_success)
        successful_llm_calls = sum(1 for r in results if r.llm_decision is not None)

        true_positives = 0
        false_positives = 0
        true_negatives = 0
        false_negatives = 0

        for result in results:
            if result.llm_decision is None:
                continue  # Skip failed classifications

            expected = result.expected_keep
            predicted = result.llm_decision

            if expected and predicted:
                true_positives += 1
            elif not expected and not predicted:
                true_negatives += 1
            elif not expected and predicted:
                false_positives += 1
            elif expected and not predicted:
                false_negatives += 1

        return FilteringMetrics(
            total_events=total_events,
            successful_scrapes=successful_scrapes,
            successful_llm_calls=successful_llm_calls,
            true_positives=true_positives,
            false_positives=false_positives,
            true_negatives=true_negatives,
            false_negatives=false_negatives
        )

    def analyze_false_positives(self, results: List[TestResult], limit: int = 10) -> List[TestResult]:
        """Analyze false positive results to understand filtering issues."""

        false_positives = [
            r for r in results
            if r.llm_decision is not None and not r.expected_keep and r.llm_decision
        ]

        self.logger.info(f"Found {len(false_positives)} false positives")

        for fp in false_positives[:limit]:
            self.logger.info(f"FALSE POSITIVE: {fp.title[:80]}...")
            self.logger.info(f"  URL: {fp.url}")
            self.logger.info(f"  Rejection reason: {fp.llm_rejection_reason}")
            self.logger.info("")

        return false_positives

    def analyze_false_negatives(self, results: List[TestResult], limit: int = 10) -> List[TestResult]:
        """Analyze false negative results to understand filtering issues."""

        false_negatives = [
            r for r in results
            if r.llm_decision is not None and r.expected_keep and not r.llm_decision
        ]

        self.logger.info(f"Found {len(false_negatives)} false negatives")

        for fn in false_negatives[:limit]:
            self.logger.info(f"FALSE NEGATIVE: {fn.title[:80]}...")
            self.logger.info(f"  URL: {fn.url}")
            self.logger.info(f"  Rejection reason: {fn.llm_rejection_reason}")
            self.logger.info("")

        return false_negatives

    def save_results(self, results: List[TestResult], filename: str):
        """Save test results to CSV for analysis."""

        results_data = []
        for r in results:
            results_data.append({
                'event_id': r.event_id,
                'title': r.title,
                'url': r.url,
                'expected_keep': r.expected_keep,
                'llm_decision': r.llm_decision,
                'llm_confidence': r.llm_confidence,
                'llm_rejection_reason': r.llm_rejection_reason,
                'scraped_content_length': r.scraped_content_length,
                'scraping_success': r.scraping_success,
                'error_message': r.error_message,
                'correct_prediction': r.llm_decision == r.expected_keep if r.llm_decision is not None else None
            })

        df = pd.DataFrame(results_data)
        df.to_csv(filename, index=False)
        self.logger.info(f"Saved {len(results)} results to {filename}")

async def main():
    """Main test function."""

    # Check for OpenAI API key
    openai_api_key = os.getenv('OPENAI_API_KEY')
    if not openai_api_key:
        print("ERROR: OPENAI_API_KEY environment variable not set")
        return

    # Initialize tester
    training_data_path = "./machine_learning_filter/event_training_data.xlsx"
    tester = LLMFilterTester(openai_api_key, training_data_path)

    # Load training data
    df = tester.load_training_data()

    # Test different configurations
    configurations = [
        {"prompt_variant": "default", "model": "gpt-4o-mini", "threshold": 0.5},
        {"prompt_variant": "default", "model": "gpt-4o-mini", "threshold": 0.3},
        {"prompt_variant": "default", "model": "gpt-4o-mini", "threshold": 0.7},
    ]

    all_results = {}

    for config in configurations:
        print(f"\n{'='*60}")
        print(f"Testing configuration: {config}")
        print('='*60)

        metrics, results = await tester.test_filtering_configuration(
            df=df,
            prompt_variant=config["prompt_variant"],
            model=config["model"],
            confidence_threshold=config["threshold"],
            max_events=20  # Start with a small sample for testing
        )

        config_name = f"{config['prompt_variant']}_{config['model']}_{config['threshold']}"
        all_results[config_name] = (metrics, results)

        # Analyze errors
        print(f"\nAnalyzing false positives for {config_name}:")
        fp = tester.analyze_false_positives(results, limit=5)

        print(f"\nAnalyzing false negatives for {config_name}:")
        fn = tester.analyze_false_negatives(results, limit=5)

        # Save results
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"test_results_{config_name}_{timestamp}.csv"
        tester.save_results(results, filename)

    # Summary comparison
    print(f"\n{'='*60}")
    print("CONFIGURATION COMPARISON")
    print('='*60)
    print(f"{'Config':<30} {'Precision':<10} {'Recall':<10} {'F1':<10} {'Accuracy':<10}")
    print('-'*70)

    for config_name, (metrics, _) in all_results.items():
        print(f"{config_name:<30} {metrics.precision:<10.3f} {metrics.recall:<10.3f} {metrics.f1_score:<10.3f} {metrics.accuracy:<10.3f}")

if __name__ == "__main__":
    asyncio.run(main())
