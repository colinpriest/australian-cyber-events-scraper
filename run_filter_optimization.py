#!/usr/bin/env python3
"""
Main script to run LLM filter optimization against training data.

This script systematically tests different prompt variants and configurations
to find the optimal settings for matching the training data results.
"""

import asyncio
import os
import pandas as pd
import logging
from typing import Dict, List, Any
import json
from datetime import datetime
import requests
from bs4 import BeautifulSoup

from experimental_llm_classifier import ExperimentalLLMClassifier, LLMFilterTuner

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class WebContentScraper:
    """Enhanced web scraper with error handling."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })

    def scrape_content(self, url: str, timeout: int = 30) -> tuple[bool, str, str]:
        """
        Scrape content from URL with enhanced error handling.

        Returns:
            (success, content, error_message)
        """
        try:
            # Handle different URL schemes
            if not url.startswith(('http://', 'https://')):
                url = 'https://' + url

            response = self.session.get(url, timeout=timeout, allow_redirects=True)
            response.raise_for_status()

            # Check content type
            content_type = response.headers.get('content-type', '').lower()
            if 'text/html' not in content_type and 'text/plain' not in content_type:
                return False, "", f"Unsupported content type: {content_type}"

            # Parse HTML
            soup = BeautifulSoup(response.content, 'html.parser')

            # Remove unwanted elements
            for element in soup(['script', 'style', 'nav', 'header', 'footer', 'aside']):
                element.decompose()

            # Extract main content
            main_content = soup.find('main') or soup.find('article') or soup.find('div', class_=['content', 'article', 'post'])
            if main_content:
                text = main_content.get_text()
            else:
                text = soup.get_text()

            # Clean text
            lines = (line.strip() for line in text.splitlines())
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            clean_text = ' '.join(chunk for chunk in chunks if chunk)

            # Truncate if too long
            if len(clean_text) > 10000:
                clean_text = clean_text[:10000] + "... [truncated]"

            return True, clean_text, ""

        except requests.RequestException as e:
            return False, "", f"Request error: {str(e)}"
        except Exception as e:
            return False, "", f"Parsing error: {str(e)}"

async def load_and_prepare_training_data(file_path: str, max_samples: int = None) -> List[Dict]:
    """Load training data and prepare it for testing."""

    logger.info(f"Loading training data from {file_path}")
    df = pd.read_excel(file_path)

    if max_samples:
        df = df.head(max_samples)
        logger.info(f"Limited to {max_samples} samples for testing")

    logger.info(f"Training data summary:")
    logger.info(f"  Total events: {len(df)}")
    logger.info(f"  Should keep: {df['filter_keep'].sum()}")
    logger.info(f"  Should reject: {len(df) - df['filter_keep'].sum()}")

    # Prepare events for testing
    scraper = WebContentScraper()
    events_data = []

    for idx, row in df.iterrows():
        logger.info(f"Processing event {idx + 1}/{len(df)}: {row['raw_title'][:60]}...")

        # Scrape content
        success, content, error = scraper.scrape_content(row['source_url'])

        if not success:
            logger.warning(f"Failed to scrape {row['source_url']}: {error}")
            content = row.get('raw_description', '') + ' ' + row.get('raw_content', '')

        event_data = {
            'event_id': row['raw_event_id'],
            'title': row['raw_title'],
            'description': row.get('raw_description', ''),
            'content': content,
            'url': row['source_url'],
            'expected_keep': bool(row['filter_keep']),
            'scraping_success': success,
            'scraping_error': error if not success else None
        }
        events_data.append(event_data)

    successful_scrapes = sum(1 for e in events_data if e['scraping_success'])
    logger.info(f"Successfully scraped {successful_scrapes}/{len(events_data)} URLs")

    return events_data

async def test_configuration(openai_api_key: str, events_data: List[Dict],
                           prompt_variant: str, model: str, confidence_threshold: float) -> Dict:
    """Test a specific configuration."""

    logger.info(f"Testing: prompt={prompt_variant}, model={model}, threshold={confidence_threshold}")

    classifier = ExperimentalLLMClassifier(
        openai_api_key=openai_api_key,
        prompt_variant=prompt_variant,
        model=model
    )

    results = []
    correct_predictions = 0
    total_processed = 0

    for event_data in events_data:
        try:
            # Classify event
            enhancement = await classifier.classify_single_event_detailed(
                title=event_data['title'],
                content=event_data['content'],
                description=event_data['description']
            )

            # Apply threshold
            predicted_keep = (
                enhancement.is_cybersecurity_event and
                enhancement.is_australian_relevant and
                enhancement.overall_confidence >= confidence_threshold
            )

            expected_keep = event_data['expected_keep']
            is_correct = predicted_keep == expected_keep

            if is_correct:
                correct_predictions += 1
            total_processed += 1

            result = {
                'event_id': event_data['event_id'],
                'title': event_data['title'],
                'url': event_data['url'],
                'expected_keep': expected_keep,
                'predicted_keep': predicted_keep,
                'is_correct': is_correct,
                'is_cybersecurity_event': enhancement.is_cybersecurity_event,
                'is_australian_relevant': enhancement.is_australian_relevant,
                'overall_confidence': enhancement.overall_confidence,
                'content_confidence': enhancement.content_confidence,
                'specificity_confidence': enhancement.specificity_confidence,
                'australian_confidence': enhancement.australian_confidence,
                'rejection_reason': enhancement.rejection_reason,
                'detected_indicators': enhancement.detected_indicators,
                'rejection_indicators': enhancement.rejection_indicators,
                'australian_indicators': enhancement.australian_indicators
            }
            results.append(result)

            # Log progress
            if total_processed % 5 == 0:
                accuracy = correct_predictions / total_processed
                logger.info(f"  Processed {total_processed}/{len(events_data)}, accuracy so far: {accuracy:.3f}")

        except Exception as e:
            logger.error(f"Error processing event {event_data['event_id']}: {e}")

    # Calculate final metrics
    tp = fp = tn = fn = 0
    for result in results:
        if result['expected_keep'] and result['predicted_keep']:
            tp += 1
        elif not result['expected_keep'] and not result['predicted_keep']:
            tn += 1
        elif not result['expected_keep'] and result['predicted_keep']:
            fp += 1
        elif result['expected_keep'] and not result['predicted_keep']:
            fn += 1

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1_score = 2 * (precision * recall) / (precision + recall) if (precision + recall) > 0 else 0
    accuracy = (tp + tn) / (tp + fp + tn + fn) if (tp + fp + tn + fn) > 0 else 0

    avg_confidence = sum(r['overall_confidence'] for r in results) / len(results) if results else 0

    metrics = {
        'prompt_variant': prompt_variant,
        'model': model,
        'confidence_threshold': confidence_threshold,
        'total_processed': total_processed,
        'accuracy': accuracy,
        'precision': precision,
        'recall': recall,
        'f1_score': f1_score,
        'true_positives': tp,
        'false_positives': fp,
        'true_negatives': tn,
        'false_negatives': fn,
        'avg_confidence': avg_confidence
    }

    logger.info(f"Results: Accuracy={accuracy:.3f}, Precision={precision:.3f}, Recall={recall:.3f}, F1={f1_score:.3f}")

    return {
        'metrics': metrics,
        'results': results
    }

def analyze_results(all_results: Dict[str, Dict]) -> None:
    """Analyze and compare results across all configurations."""

    logger.info("\n" + "="*80)
    logger.info("CONFIGURATION COMPARISON")
    logger.info("="*80)

    # Sort by F1 score
    sorted_configs = sorted(all_results.items(), key=lambda x: x[1]['metrics']['f1_score'], reverse=True)

    print(f"{'Rank':<4} {'Prompt':<10} {'Model':<12} {'Thresh':<6} {'Acc':<6} {'Prec':<6} {'Rec':<6} {'F1':<6} {'TP':<3} {'FP':<3} {'TN':<3} {'FN':<3}")
    print("-" * 80)

    for rank, (config_name, config_results) in enumerate(sorted_configs, 1):
        m = config_results['metrics']
        print(f"{rank:<4} {m['prompt_variant']:<10} {m['model']:<12} {m['confidence_threshold']:<6.2f} "
              f"{m['accuracy']:<6.3f} {m['precision']:<6.3f} {m['recall']:<6.3f} {m['f1_score']:<6.3f} "
              f"{m['true_positives']:<3} {m['false_positives']:<3} {m['true_negatives']:<3} {m['false_negatives']:<3}")

    # Analyze false positives and negatives for best configuration
    best_config_name, best_config = sorted_configs[0]
    logger.info(f"\nAnalyzing errors for best configuration: {best_config_name}")

    false_positives = [r for r in best_config['results'] if not r['expected_keep'] and r['predicted_keep']]
    false_negatives = [r for r in best_config['results'] if r['expected_keep'] and not r['predicted_keep']]

    logger.info(f"\nFALSE POSITIVES ({len(false_positives)}):")
    for fp in false_positives[:5]:  # Show first 5
        logger.info(f"  - {fp['title'][:60]}...")
        logger.info(f"    Confidence: {fp['overall_confidence']:.3f}")
        logger.info(f"    Cyber: {fp['is_cybersecurity_event']}, Aus: {fp['is_australian_relevant']}")
        if fp['detected_indicators']:
            logger.info(f"    Indicators: {', '.join(fp['detected_indicators'][:3])}")

    logger.info(f"\nFALSE NEGATIVES ({len(false_negatives)}):")
    for fn in false_negatives[:5]:  # Show first 5
        logger.info(f"  - {fn['title'][:60]}...")
        logger.info(f"    Confidence: {fn['overall_confidence']:.3f}")
        logger.info(f"    Cyber: {fn['is_cybersecurity_event']}, Aus: {fn['is_australian_relevant']}")
        logger.info(f"    Rejection: {fn['rejection_reason']}")

def save_detailed_results(all_results: Dict[str, Dict], timestamp: str) -> None:
    """Save detailed results to files."""

    # Save metrics comparison
    metrics_data = []
    for config_name, config_results in all_results.items():
        metrics_data.append(config_results['metrics'])

    metrics_df = pd.DataFrame(metrics_data)
    metrics_file = f"filter_optimization_metrics_{timestamp}.csv"
    metrics_df.to_csv(metrics_file, index=False)
    logger.info(f"Saved metrics to {metrics_file}")

    # Save detailed results for best configuration
    best_config = max(all_results.items(), key=lambda x: x[1]['metrics']['f1_score'])
    detailed_results = best_config[1]['results']

    detailed_df = pd.DataFrame(detailed_results)
    detailed_file = f"filter_optimization_detailed_{timestamp}.csv"
    detailed_df.to_csv(detailed_file, index=False)
    logger.info(f"Saved detailed results to {detailed_file}")

    # Save configuration summary
    summary = {
        'timestamp': timestamp,
        'best_configuration': {
            'name': best_config[0],
            'metrics': best_config[1]['metrics']
        },
        'all_configurations': {name: results['metrics'] for name, results in all_results.items()}
    }

    summary_file = f"filter_optimization_summary_{timestamp}.json"
    with open(summary_file, 'w') as f:
        json.dump(summary, f, indent=2)
    logger.info(f"Saved summary to {summary_file}")

async def main():
    """Main optimization routine."""

    # Check for OpenAI API key
    openai_api_key = os.getenv('OPENAI_API_KEY')
    if not openai_api_key:
        logger.error("OPENAI_API_KEY environment variable not set")
        return

    # Configuration
    training_data_path = "./machine_learning_filter/event_training_data.xlsx"
    max_samples = 30  # Limit for initial testing

    # Test configurations
    configurations = [
        # Default configurations with different thresholds
        {"prompt_variant": "default", "model": "gpt-4o-mini", "confidence_threshold": 0.3},
        {"prompt_variant": "default", "model": "gpt-4o-mini", "confidence_threshold": 0.5},
        {"prompt_variant": "default", "model": "gpt-4o-mini", "confidence_threshold": 0.7},

        # Strict configurations
        {"prompt_variant": "strict", "model": "gpt-4o-mini", "confidence_threshold": 0.3},
        {"prompt_variant": "strict", "model": "gpt-4o-mini", "confidence_threshold": 0.5},

        # Lenient configurations
        {"prompt_variant": "lenient", "model": "gpt-4o-mini", "confidence_threshold": 0.3},
        {"prompt_variant": "lenient", "model": "gpt-4o-mini", "confidence_threshold": 0.5},

        # Detailed configuration
        {"prompt_variant": "detailed", "model": "gpt-4o-mini", "confidence_threshold": 0.5},
    ]

    try:
        # Load and prepare training data
        events_data = await load_and_prepare_training_data(training_data_path, max_samples)

        # Test all configurations
        all_results = {}
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        for i, config in enumerate(configurations, 1):
            logger.info(f"\n{'='*60}")
            logger.info(f"Testing configuration {i}/{len(configurations)}")
            logger.info(f"{'='*60}")

            config_name = f"{config['prompt_variant']}_{config['model']}_{config['confidence_threshold']}"
            result = await test_configuration(openai_api_key, events_data, **config)
            all_results[config_name] = result

        # Analyze results
        analyze_results(all_results)

        # Save results
        save_detailed_results(all_results, timestamp)

        logger.info(f"\nOptimization complete! Check the generated CSV and JSON files for detailed results.")

    except Exception as e:
        logger.error(f"Optimization failed: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(main())