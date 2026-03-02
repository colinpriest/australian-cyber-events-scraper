"""
Progressive filtering system that applies different filtering strategies at different stages.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from .confidence_filter import ConfidenceBasedFilter, FilterResult

logger = logging.getLogger(__name__)


class ProgressiveFilterSystem:
    """
    Multi-stage progressive filtering system.

    Implements Strategy 2: Multi-Stage Progressive Filtering
    - Stage 1: Broad inclusion filter at discovery (minimal false negatives)
    - Stage 2: Content-based refinement after scraping (balanced approach)
    - Stage 3: LLM-based final classification (high precision)

    And Strategy 4: Confidence-Based Thresholding
    - High confidence events: score ≥ 0.8 (auto-include)
    - Medium confidence: 0.4-0.8 (flag for review or additional validation)
    - Low confidence: < 0.4 (reject)
    """

    def __init__(self):
        self.confidence_filter = ConfidenceBasedFilter()
        self._stats_lock = threading.Lock()
        self.stats = {
            'discovery_processed': 0,
            'discovery_passed': 0,
            'content_processed': 0,
            'content_passed': 0,
            'final_processed': 0,
            'final_passed': 0,
            'high_confidence': 0,
            'medium_confidence': 0,
            'low_confidence': 0
        }

    def should_discover_event(self, title: str, description: str = "",
                            url: str = "", metadata: Optional[Dict[str, Any]] = None) -> FilterResult:
        """
        Stage 1: Discovery filter - very permissive to minimize false negatives.

        Args:
            title: Event title
            description: Event description (if available)
            url: Source URL
            metadata: Additional metadata

        Returns:
            FilterResult with confidence score and reasoning
        """
        try:
            result = self.confidence_filter.evaluate_discovery_stage(
                title=title,
                description=description,
                url=url,
                metadata=metadata
            )

            with self._stats_lock:
                self.stats['discovery_processed'] += 1
                if result.is_cyber_relevant:
                    self.stats['discovery_passed'] += 1

            logger.debug(f"[DISCOVERY] {title[:50]}... -> {result.confidence_score:.2f} "
                        f"({result.risk_level}) - {'PASS' if result.is_cyber_relevant else 'REJECT'}")

            return result

        except Exception as e:
            logger.warning(f"[DISCOVERY] Filter error for '{title[:50]}...': {e}")
            # On error, be permissive and let it through
            return FilterResult(
                confidence_score=0.3,
                is_cyber_relevant=True,
                reasoning=[f"Filter error (permissive): {str(e)[:100]}"],
                stage="discovery"
            )

    def should_process_content(self, title: str, content: str, url: str = "",
                             metadata: Optional[Dict[str, Any]] = None) -> FilterResult:
        """
        Stage 2: Content filter - balanced approach after scraping.

        Args:
            title: Event title
            content: Scraped content
            url: Source URL
            metadata: Additional metadata

        Returns:
            FilterResult with confidence score and reasoning
        """
        try:
            result = self.confidence_filter.evaluate_content_stage(
                title=title,
                content=content,
                url=url,
                metadata=metadata
            )

            with self._stats_lock:
                self.stats['content_processed'] += 1
                if result.is_cyber_relevant:
                    self.stats['content_passed'] += 1

            # Update confidence level stats
            self._update_confidence_stats(result)

            logger.debug(f"[CONTENT] {title[:50]}... -> {result.confidence_score:.2f} "
                        f"({result.risk_level}) - {'PASS' if result.is_cyber_relevant else 'REJECT'}")

            return result

        except Exception as e:
            logger.warning(f"[CONTENT] Filter error for '{title[:50]}...': {e}")
            # On error, use moderate confidence
            return FilterResult(
                confidence_score=0.5,
                is_cyber_relevant=True,
                reasoning=[f"Filter error (moderate): {str(e)[:100]}"],
                stage="content"
            )

    def should_enrich_event(self, title: str, content: str, url: str = "",
                          llm_analysis: Optional[Dict[str, Any]] = None,
                          metadata: Optional[Dict[str, Any]] = None) -> FilterResult:
        """
        Stage 3: Final filter - high precision with LLM analysis.

        Args:
            title: Event title
            content: Scraped content
            url: Source URL
            llm_analysis: LLM analysis results
            metadata: Additional metadata

        Returns:
            FilterResult with confidence score and reasoning
        """
        try:
            result = self.confidence_filter.evaluate_final_stage(
                title=title,
                content=content,
                url=url,
                llm_analysis=llm_analysis,
                metadata=metadata
            )

            with self._stats_lock:
                self.stats['final_processed'] += 1
                if result.is_cyber_relevant:
                    self.stats['final_passed'] += 1

            # Update confidence level stats
            self._update_confidence_stats(result)

            logger.debug(f"[FINAL] {title[:50]}... -> {result.confidence_score:.2f} "
                        f"({result.risk_level}) - {'PASS' if result.is_cyber_relevant else 'REJECT'}")

            return result

        except Exception as e:
            logger.warning(f"[FINAL] Filter error for '{title[:50]}...': {e}")
            # On error in final stage, be more conservative
            return FilterResult(
                confidence_score=0.3,
                is_cyber_relevant=False,
                reasoning=[f"Filter error (conservative): {str(e)[:100]}"],
                stage="final"
            )

    def get_filtering_statistics(self) -> Dict[str, Any]:
        """Get comprehensive filtering statistics."""
        with self._stats_lock:
            stats_snapshot = dict(self.stats)

        total_processed = stats_snapshot['discovery_processed']
        if total_processed == 0:
            return stats_snapshot

        # Calculate pass rates
        discovery_rate = (stats_snapshot['discovery_passed'] / stats_snapshot['discovery_processed']) * 100 if stats_snapshot['discovery_processed'] > 0 else 0
        content_rate = (stats_snapshot['content_passed'] / stats_snapshot['content_processed']) * 100 if stats_snapshot['content_processed'] > 0 else 0
        final_rate = (stats_snapshot['final_passed'] / stats_snapshot['final_processed']) * 100 if stats_snapshot['final_processed'] > 0 else 0

        # Calculate confidence distribution
        total_confidence_events = stats_snapshot['high_confidence'] + stats_snapshot['medium_confidence'] + stats_snapshot['low_confidence']
        high_pct = (stats_snapshot['high_confidence'] / total_confidence_events) * 100 if total_confidence_events > 0 else 0
        medium_pct = (stats_snapshot['medium_confidence'] / total_confidence_events) * 100 if total_confidence_events > 0 else 0
        low_pct = (stats_snapshot['low_confidence'] / total_confidence_events) * 100 if total_confidence_events > 0 else 0

        return {
            **stats_snapshot,
            'discovery_pass_rate': discovery_rate,
            'content_pass_rate': content_rate,
            'final_pass_rate': final_rate,
            'high_confidence_pct': high_pct,
            'medium_confidence_pct': medium_pct,
            'low_confidence_pct': low_pct,
            'overall_efficiency': (stats_snapshot['final_passed'] / total_processed) * 100 if total_processed > 0 else 0
        }

    def reset_statistics(self):
        """Reset all filtering statistics."""
        for key in self.stats:
            self.stats[key] = 0

    def log_filtering_summary(self):
        """Log a summary of filtering performance."""
        stats = self.get_filtering_statistics()

        logger.info("[FILTER_SUMMARY] Progressive Filtering Performance:")
        logger.info(f"[DISCOVERY] {stats['discovery_processed']} processed, "
                   f"{stats['discovery_passed']} passed ({stats['discovery_pass_rate']:.1f}%)")

        if stats['content_processed'] > 0:
            logger.info(f"[CONTENT] {stats['content_processed']} processed, "
                       f"{stats['content_passed']} passed ({stats['content_pass_rate']:.1f}%)")

        if stats['final_processed'] > 0:
            logger.info(f"[FINAL] {stats['final_processed']} processed, "
                       f"{stats['final_passed']} passed ({stats['final_pass_rate']:.1f}%)")

        if stats['high_confidence'] > 0 or stats['medium_confidence'] > 0 or stats['low_confidence'] > 0:
            logger.info(f"[CONFIDENCE] High: {stats['high_confidence']} ({stats['high_confidence_pct']:.1f}%), "
                       f"Medium: {stats['medium_confidence']} ({stats['medium_confidence_pct']:.1f}%), "
                       f"Low: {stats['low_confidence']} ({stats['low_confidence_pct']:.1f}%)")

        if stats['discovery_processed'] > 0:
            logger.info(f"[EFFICIENCY] Overall pass rate: {stats['overall_efficiency']:.1f}%")

    def _update_confidence_stats(self, result: FilterResult) -> None:
        """Update confidence level statistics."""
        with self._stats_lock:
            if result.confidence_score >= 0.8:
                self.stats['high_confidence'] += 1
            elif result.confidence_score >= 0.4:
                self.stats['medium_confidence'] += 1
            else:
                self.stats['low_confidence'] += 1

    def get_stage_thresholds(self) -> Dict[str, float]:
        """Get the confidence thresholds for each stage."""
        return {
            'discovery': 0.2,  # Very permissive
            'content': 0.4,    # Balanced
            'final': 0.6       # High precision
        }

    def should_flag_for_manual_review(self, result: FilterResult) -> bool:
        """
        Determine if an event should be flagged for manual review.

        Events with medium confidence (0.4-0.8) that are borderline
        may benefit from human review.
        """
        return (0.3 <= result.confidence_score <= 0.7 and
                result.stage in ['content', 'final'] and
                len(result.reasoning) >= 2)

    def explain_filtering_decision(self, result: FilterResult) -> str:
        """
        Generate a human-readable explanation of the filtering decision.
        """
        decision = "ACCEPTED" if result.is_cyber_relevant else "REJECTED"
        confidence = f"{result.confidence_score:.2f}"
        stage = result.stage.upper()

        explanation = f"[{stage}] {decision} (confidence: {confidence}, {result.risk_level})\n"
        explanation += "Reasoning:\n"

        for i, reason in enumerate(result.reasoning, 1):
            explanation += f"  {i}. {reason}\n"

        if self.should_flag_for_manual_review(result):
            explanation += "\n⚠️  RECOMMENDED FOR MANUAL REVIEW (borderline confidence)\n"

        return explanation