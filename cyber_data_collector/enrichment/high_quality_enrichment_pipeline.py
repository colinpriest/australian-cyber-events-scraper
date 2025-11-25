"""
High-Quality Enrichment Pipeline - Orchestrates the complete multi-stage enrichment process.

This module coordinates:
1. Content Acquisition (fetch full article text)
2. GPT-4o Primary Extraction (ultra-specific prompts)
3. Perplexity Fact-Checking (verify extracted facts)
4. Validation (sensibility checks)
5. Confidence Aggregation (final decision)
"""

import logging
from typing import Dict, Any
from datetime import datetime

from .content_acquisition import ContentAcquisitionService
from .gpt4o_enricher import GPT4oEnricher
from .perplexity_fact_checker import PerplexityFactChecker
from .enrichment_validator import EnrichmentValidator


class HighQualityEnrichmentPipeline:
    """
    Complete enrichment pipeline with validation and fact-checking.

    This replaces the old regex-based enrichment with a robust,
    multi-stage LLM-based approach.
    """

    def __init__(self, db_path: str, openai_api_key: str, perplexity_api_key: str):
        """
        Initialize the high-quality enrichment pipeline.

        Args:
            db_path: Path to SQLite database
            openai_api_key: OpenAI API key for GPT-4o
            perplexity_api_key: Perplexity API key for fact-checking
        """
        self.content_service = ContentAcquisitionService()
        self.gpt4o_enricher = GPT4oEnricher(openai_api_key)
        self.fact_checker = PerplexityFactChecker(perplexity_api_key)
        self.validator = EnrichmentValidator(db_path)
        self.logger = logging.getLogger(__name__)

    def enrich_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """
        Run complete enrichment pipeline on a single event.

        Args:
            event: Event dict with at minimum 'url' field

        Returns:
            {
                'enrichment_result': Dict (extracted data),
                'fact_check_result': Dict (verification results),
                'validation_result': Dict (validation results),
                'final_decision': Dict (accept/reject + confidence),
                'audit_trail': Dict (complete processing history)
            }
        """

        event_id = event.get('enriched_event_id', 'unknown')
        url = event.get('url', '')

        self.logger.info(f"\n{'='*80}")
        self.logger.info(f"ENRICHING EVENT: {event_id}")
        self.logger.info(f"URL: {url}")
        self.logger.info(f"{'='*80}\n")

        audit_trail = {
            'event_id': event_id,
            'url': url,
            'started_at': datetime.now().isoformat(),
            'stages': []
        }

        try:
            # STAGE 1: Content Acquisition
            self.logger.info("STAGE 1: Content Acquisition")
            self.logger.info("-" * 80)

            content_result = self.content_service.acquire_content(event)

            audit_trail['stages'].append({
                'stage': 1,
                'name': 'content_acquisition',
                'success': content_result['extraction_success'],
                'method': content_result.get('extraction_method'),
                'content_length': content_result.get('content_length'),
                'source_reliability': content_result.get('source_reliability')
            })

            if not content_result['extraction_success']:
                return self._error_result(
                    event_id,
                    f"Content acquisition failed: {content_result['error']}",
                    audit_trail
                )

            self.logger.info(f"✓ Acquired {content_result['content_length']} words using {content_result['extraction_method']}")
            self.logger.info(f"  Source reliability: {content_result['source_reliability']:.2f}")

            # STAGE 2: GPT-4o Primary Extraction
            self.logger.info("\nSTAGE 2: GPT-4o Primary Extraction")
            self.logger.info("-" * 80)

            extraction_result = self.gpt4o_enricher.extract(content_result)

            audit_trail['stages'].append({
                'stage': 2,
                'name': 'gpt4o_extraction',
                'success': extraction_result.get('overall_confidence', 0) > 0,
                'confidence': extraction_result.get('overall_confidence'),
                'victim_found': extraction_result.get('victim', {}).get('organization') is not None
            })

            if extraction_result.get('extraction_notes', '').startswith('ERROR'):
                return self._error_result(
                    event_id,
                    f"GPT-4o extraction failed: {extraction_result['extraction_notes']}",
                    audit_trail
                )

            victim_org = extraction_result.get('victim', {}).get('organization', 'None')
            self.logger.info(f"✓ Extracted victim: {victim_org}")
            self.logger.info(f"  Extraction confidence: {extraction_result.get('overall_confidence', 0):.2f}")
            self.logger.info(f"  Is specific incident: {extraction_result.get('specificity', {}).get('is_specific_incident')}")

            # STAGE 3: Perplexity Fact-Checking
            self.logger.info("\nSTAGE 3: Perplexity Fact-Checking")
            self.logger.info("-" * 80)

            fact_check_result = self.fact_checker.fact_check_extraction(extraction_result)

            audit_trail['stages'].append({
                'stage': 3,
                'name': 'perplexity_fact_check',
                'checks_performed': fact_check_result['checks_performed'],
                'checks_passed': fact_check_result['checks_passed'],
                'checks_failed': fact_check_result['checks_failed'],
                'verification_confidence': fact_check_result['overall_verification_confidence']
            })

            self.logger.info(f"✓ Fact-checking complete: {fact_check_result['checks_passed']}/{fact_check_result['checks_performed']} passed")
            self.logger.info(f"  Verification confidence: {fact_check_result['overall_verification_confidence']:.2f}")

            # STAGE 4: Validation
            self.logger.info("\nSTAGE 4: Validation & Sensibility Checks")
            self.logger.info("-" * 80)

            validation_result = self.validator.validate(
                extraction_result,
                fact_check_result,
                event_title=event.get('title'),
                event_url=event.get('url')
            )

            # Use modified extraction if validator applied specificity overrides
            if validation_result.get('specificity_overrides'):
                extraction_result = validation_result['modified_extraction']
                self.logger.info(f"✓ Applied {len(validation_result['specificity_overrides'])} specificity override(s)")
                for override in validation_result['specificity_overrides']:
                    self.logger.info(f"  Override: {override['original']} -> {override['override']} ({override['reason']})")

            audit_trail['stages'].append({
                'stage': 4,
                'name': 'validation',
                'is_valid': validation_result['is_valid'],
                'error_count': len(validation_result['errors']),
                'warning_count': len(validation_result['warnings']),
                'validation_confidence': validation_result['validation_confidence'],
                'specificity_overrides': validation_result.get('specificity_overrides', [])
            })

            self.logger.info(f"✓ Validation complete: {'VALID' if validation_result['is_valid'] else 'INVALID'}")
            self.logger.info(f"  Errors: {len(validation_result['errors'])}, Warnings: {len(validation_result['warnings'])}")
            self.logger.info(f"  Validation confidence: {validation_result['validation_confidence']:.2f}")

            if validation_result['errors']:
                for error in validation_result['errors']:
                    self.logger.warning(f"  ERROR: {error}")

            if validation_result['warnings']:
                for warning in validation_result['warnings'][:3]:  # Show first 3
                    self.logger.warning(f"  WARNING: {warning}")

            # STAGE 5: Confidence Aggregation & Final Decision
            self.logger.info("\nSTAGE 5: Confidence Aggregation & Decision")
            self.logger.info("-" * 80)

            final_decision = self._aggregate_confidence(
                extraction_result,  # Now uses modified extraction if overrides were applied
                fact_check_result,
                validation_result,
                content_result
            )

            audit_trail['stages'].append({
                'stage': 5,
                'name': 'final_decision',
                'decision': final_decision['decision'],
                'final_confidence': final_decision['final_confidence']
            })

            audit_trail['completed_at'] = datetime.now().isoformat()
            audit_trail['total_time_seconds'] = (
                datetime.fromisoformat(audit_trail['completed_at']) -
                datetime.fromisoformat(audit_trail['started_at'])
            ).total_seconds()

            self.logger.info(f"✓ FINAL DECISION: {final_decision['decision']}")
            self.logger.info(f"  Final confidence: {final_decision['final_confidence']:.2f}")
            self.logger.info(f"  Action: {final_decision['action']}")
            self.logger.info(f"\n{'='*80}\n")

            return {
                'enrichment_result': extraction_result,
                'content_acquisition': content_result,
                'fact_check_result': fact_check_result,
                'validation_result': validation_result,
                'final_decision': final_decision,
                'audit_trail': audit_trail
            }

        except Exception as e:
            self.logger.error(f"Pipeline failed with exception: {e}")
            import traceback
            traceback.print_exc()

            audit_trail['completed_at'] = datetime.now().isoformat()
            audit_trail['error'] = str(e)

            return self._error_result(event_id, f"Pipeline exception: {e}", audit_trail)

    def _aggregate_confidence(self,
                             gpt_extraction: Dict,
                             fact_check: Dict,
                             validation: Dict,
                             content: Dict) -> Dict:
        """
        Combine confidences from all stages into final decision.

        Weighting:
        - GPT-4o extraction: 30%
        - Perplexity fact-check: 30%
        - Validation: 20%
        - Source reliability: 20%
        """

        gpt_conf = gpt_extraction.get('overall_confidence', 0.5)
        fact_conf = fact_check.get('overall_verification_confidence', 0.5)
        val_conf = validation.get('validation_confidence', 0.5)
        source_conf = content.get('source_reliability', 0.6)

        # Weighted average
        final_confidence = (
            0.30 * gpt_conf +
            0.30 * fact_conf +
            0.20 * val_conf +
            0.20 * source_conf
        )

        # Apply penalties
        penalties_applied = {}

        if validation['errors']:
            final_confidence *= 0.3  # Severe penalty for validation errors (70% reduction)
            penalties_applied['validation_errors'] = 0.3

        if len(validation['warnings']) > 3:
            final_confidence *= 0.8  # Moderate penalty for multiple warnings
            penalties_applied['many_warnings'] = 0.8

        # NEW PENALTY 1: Non-specific incidents (analysis articles, general discussions)
        is_specific = gpt_extraction.get('specificity', {}).get('is_specific_incident')
        if is_specific == False:
            final_confidence *= 0.8  # Moderate penalty - SOFTENED from 0.5 to reduce over-rejection
            penalties_applied['non_specific_incident'] = 0.8

        # NEW PENALTY 2: Very low Australian relevance (foreign events)
        australian_relevance = gpt_extraction.get('australian_relevance', {}).get('relevance_score', 1.0)
        if australian_relevance < 0.3:
            final_confidence *= 0.4  # Heavy penalty for non-Australian events
            penalties_applied['low_australian_relevance'] = 0.4

        # NEW PENALTY 3: Low fact-check pass rate (unreliable data)
        checks_performed = fact_check.get('checks_performed', 0)
        checks_passed = fact_check.get('checks_passed', 0)
        if checks_performed > 0:
            fact_check_pass_rate = checks_passed / checks_performed
            if fact_check_pass_rate < 0.5:
                final_confidence *= 0.5  # Half confidence for <50% pass rate
                penalties_applied['low_factcheck_pass_rate'] = 0.5

        # NEW PENALTY 4: Geographic mismatch (title mentions Australian but victim is foreign)
        # This catches cases like "Australian Government" in title but "DaVita Inc." extracted
        if content.get('title'):
            title_lower = content.get('title', '').lower()
            if 'australian' in title_lower and australian_relevance < 0.3:
                final_confidence *= 0.3  # Strong penalty for geographic mismatch
                penalties_applied['geographic_mismatch'] = 0.3

        # Ensure within bounds
        final_confidence = max(0.0, min(1.0, final_confidence))

        # Decision thresholds
        if final_confidence >= 0.8:
            decision = 'AUTO_ACCEPT'
            action = 'Store in database with high confidence'
        elif final_confidence >= 0.5:
            decision = 'ACCEPT_WITH_WARNING'
            action = 'Store in database with flag for manual review'
        else:
            decision = 'REJECT'
            action = 'Do not store - confidence too low, flag for manual review'

        return {
            'final_confidence': final_confidence,
            'decision': decision,
            'action': action,
            'stage_confidences': {
                'gpt4o_extraction': gpt_conf,
                'perplexity_fact_check': fact_conf,
                'validation': val_conf,
                'source_reliability': source_conf
            },
            'applied_penalties': penalties_applied,
            'fact_check_summary': {
                'checks_performed': fact_check['checks_performed'],
                'checks_passed': fact_check['checks_passed'],
                'checks_failed': fact_check['checks_failed']
            }
        }

    def _error_result(self, event_id: str, error_message: str, audit_trail: Dict) -> Dict:
        """Return error result structure"""

        audit_trail['error'] = error_message
        audit_trail['completed_at'] = datetime.now().isoformat()

        return {
            'enrichment_result': {},
            'content_acquisition': {'extraction_success': False, 'error': error_message},
            'fact_check_result': {'checks_performed': 0, 'checks_passed': 0, 'checks_failed': 0},
            'validation_result': {'is_valid': False, 'errors': [error_message], 'warnings': []},
            'final_decision': {
                'decision': 'REJECT',
                'final_confidence': 0.0,
                'action': f'Pipeline failed: {error_message}'
            },
            'audit_trail': audit_trail
        }
