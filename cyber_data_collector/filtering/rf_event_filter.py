#!/usr/bin/env python3
"""
Random Forest Event Filter - RETIRED (2026-10-27)

Discovery no longer uses this filter. The only bundle, ``rf-legacy-2025-09``,
failed acceptance (drop precision 0.53, 5.9% of real incidents lost on
organisation-separated labels) and retraining did not fix it. Discovery now
uses the calibrated Jev page gate (``filtering/page_gate.py``). The module,
the bundles and ``scripts/train_rf_filter.py`` are kept so the evaluation can
be reproduced; nothing in the pipeline imports them.

This module provides a Random Forest-based filter for cyber events that replaces
the existing 3-stage progressive filtering system with a machine learning approach.

The model is loaded from a versioned bundle (``rf_bundle``) named by
``machine_learning_filter/ACTIVE_BUNDLE``. A bundle drops records only if it
passed acceptance on organisation-separated labelled data; otherwise the filter
is report-only - it logs what it would have filtered and keeps everything.
"""

from __future__ import annotations

import logging
import re
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

from cyber_data_collector.filtering import rf_bundle

logger = logging.getLogger(__name__)

@dataclass
class FilterResult:
    """Result of filtering operation."""
    is_cyber_relevant: bool
    confidence_score: float
    reasoning: List[str]
    stage: str = "rf_filter"
    risk_level: str = "medium"  # For compatibility with existing code

class RfEventFilter:
    """
    Random Forest-based event filter that loads a trained model to predict
    whether raw events should be kept for enrichment.
    """
    
    def __init__(self, model_dir: Optional[str] = None, bundle_id: Optional[str] = None):
        """
        Initialize the Random Forest filter.

        Args:
            model_dir: Directory holding ``ACTIVE_BUNDLE`` and ``bundles/``. Defaults to
                the ``machine_learning_filter`` directory at the project root.
            bundle_id: Load this bundle instead of the active one.
        """
        self.model_dir = Path(model_dir) if model_dir else rf_bundle.MODEL_DIR
        self.bundle_id = bundle_id or rf_bundle.active_bundle_id(self.model_dir)
        self.model = None
        self.source_type_encoder = None
        self.text_vectorizer = None
        self.is_loaded = False
        self.enforcing = False
        
        # Statistics
        self.stats = {
            'events_processed': 0,
            'events_kept': 0,
            'events_filtered': 0,
            'would_filter': 0,
            'prediction_errors': 0
        }

        # Load models
        self._load_models()
    
    def _load_models(self):
        """Load the active bundle: hashes verified, predictions checked against its
        reference after any library change, and nothing ever rewritten."""
        if not self.bundle_id:
            raise FileNotFoundError(f"No ACTIVE_BUNDLE in {self.model_dir}; "
                                    "run scripts/train_rf_filter.py")
        try:
            bundle = rf_bundle.load_bundle(self.bundle_id, self.model_dir / "bundles")
        except Exception as e:
            logger.error(f"Failed to load Random Forest bundle {self.bundle_id}: {e}")
            self.is_loaded = False
            raise
        self.model = bundle.model
        self.text_vectorizer = bundle.vectorizer
        self.source_type_encoder = bundle.encoder
        self.enforcing = bundle.enforcing
        self.is_loaded = True
        logger.info("Random Forest filter bundle %s loaded (%s)", self.bundle_id,
                    "enforcing" if self.enforcing else "report-only: not accepted on labelled data")

    def preprocess_url(self, url: str) -> str:
        return rf_bundle.preprocess_url(url)

    def preprocess_text(self, text: str) -> str:
        return rf_bundle.preprocess_text(text)

    def _prepare_features(self, source_type: str, title: str, description: str, 
                         content: str, url: str) -> np.ndarray:
        """
        Prepare features for the Random Forest model.
        
        Args:
            source_type: Event source type
            title: Event title
            description: Event description
            content: Event content
            url: Event URL
            
        Returns:
            Feature array for prediction
        """
        if not self.is_loaded:
            raise RuntimeError("Models not loaded")
        
        # Preprocess URL
        processed_url = self.preprocess_url(url)
        
        # Combine all text features
        combined_text = (
            self.preprocess_text(title) + ' ' +
            self.preprocess_text(description) + ' ' +
            self.preprocess_text(content) + ' ' +
            self.preprocess_text(processed_url)
        )
        
        # Encode source type
        try:
            source_type_encoded = self.source_type_encoder.transform([source_type])[0]
        except ValueError:
            # Handle unknown source types
            source_type_encoded = 0  # Default to first category
        
        # Vectorize text
        text_features = self.text_vectorizer.transform([combined_text])
        
        # Combine features
        features = np.hstack([
            np.array([[source_type_encoded]]),
            text_features.toarray()
        ])
        
        return features
    
    def should_keep_event(self, source_type: str, title: str, description: str = "",
                         content: str = "", url: str = "", metadata: Dict = None) -> FilterResult:
        """
        Determine if an event should be kept for enrichment using Random Forest.
        
        Args:
            source_type: Event source type
            title: Event title
            description: Event description
            content: Event content
            url: Event URL
            metadata: Additional metadata (unused but kept for compatibility)
            
        Returns:
            FilterResult with prediction and confidence
        """
        if not self.is_loaded:
            logger.warning("Random Forest models not loaded, defaulting to keep event")
            return FilterResult(
                is_cyber_relevant=True,
                confidence_score=0.5,
                reasoning=["Models not loaded - defaulting to keep"],
                stage="rf_filter"
            )
        
        try:
            # Prepare features
            features = self._prepare_features(source_type, title, description, content, url)
            
            # Make prediction
            prediction = self.model.predict(features)[0]
            prediction_proba = self.model.predict_proba(features)[0]
            
            # Get confidence score (probability of positive class)
            confidence_score = float(prediction_proba[1]) if len(prediction_proba) > 1 else 0.5
            
            # Determine if event should be kept. A bundle that did not pass
            # acceptance only reports: on labelled data the 2025-09 model
            # dropped real incidents about as often as junk.
            would_keep = bool(prediction)
            is_cyber_relevant = would_keep or not self.enforcing
            
            # Generate reasoning
            combined_text = (
                self.preprocess_text(title) + ' ' +
                self.preprocess_text(description) + ' ' +
                self.preprocess_text(content) + ' ' +
                self.preprocess_text(self.preprocess_url(url))
            )
            
            reasoning = [
                f"Random Forest prediction: {'KEEP' if would_keep else 'FILTER'}"
                + ("" if self.enforcing or would_keep else
                   f" (report-only: bundle {self.bundle_id} not accepted, record kept)"),
                f"Confidence score: {confidence_score:.3f}",
                f"Source type: {source_type}",
                f"Text length: {len(combined_text)}"
            ]
            
            # Update statistics
            self.stats['events_processed'] += 1
            if not would_keep:
                self.stats['would_filter'] += 1
            if is_cyber_relevant:
                self.stats['events_kept'] += 1
            else:
                self.stats['events_filtered'] += 1
            
            logger.debug(f"[RF_FILTER] {title[:50]}... -> {confidence_score:.3f} "
                        f"({'KEEP' if is_cyber_relevant else 'FILTER'})")
            
            # Determine risk level based on confidence score
            if confidence_score >= 0.8:
                risk_level = "low"
            elif confidence_score >= 0.4:
                risk_level = "medium"
            else:
                risk_level = "high"
            
            return FilterResult(
                is_cyber_relevant=is_cyber_relevant,
                confidence_score=confidence_score,
                reasoning=reasoning,
                stage="rf_filter",
                risk_level=risk_level
            )
            
        except Exception as e:
            self.stats['prediction_errors'] += 1
            logger.warning(f"[RF_FILTER] Prediction error for '{title[:50]}...': {e}")
            
            # On error, be conservative and keep the event
            return FilterResult(
                is_cyber_relevant=True,
                confidence_score=0.3,
                reasoning=[f"Prediction error (conservative): {str(e)[:100]}"],
                stage="rf_filter",
                risk_level="high"
            )
    
    def reset_statistics(self):
        """Reset filtering statistics for a new run."""
        self.stats = {
            'events_processed': 0,
            'events_kept': 0,
            'events_filtered': 0,
            'would_filter': 0,
            'prediction_errors': 0
        }
        logger.debug("[RF_FILTER] Statistics reset for new run")
    
    def get_filtering_statistics(self) -> Dict[str, Any]:
        """Get filtering statistics for the current run."""
        stats = self.stats.copy()
        
        if stats['events_processed'] > 0:
            stats['keep_rate'] = stats['events_kept'] / stats['events_processed']
            stats['filter_rate'] = stats['events_filtered'] / stats['events_processed']
            stats['error_rate'] = stats['prediction_errors'] / stats['events_processed']
        else:
            stats['keep_rate'] = 0.0
            stats['filter_rate'] = 0.0
            stats['error_rate'] = 0.0
        
        return stats
    
    def log_filtering_summary(self):
        """Log a summary of filtering statistics for the current run."""
        stats = self.get_filtering_statistics()
        
        logger.info("="*60)
        logger.info("[RF_FILTER] RANDOM FOREST FILTERING SUMMARY (CURRENT RUN)")
        logger.info("="*60)
        logger.info(f"Events processed: {stats['events_processed']}")
        logger.info(f"Events kept: {stats['events_kept']} ({stats['keep_rate']:.1%})")
        logger.info(f"Events filtered: {stats['events_filtered']} ({stats['filter_rate']:.1%})")
        if not self.enforcing:
            logger.info(f"Would have filtered (report-only bundle {self.bundle_id}): {stats['would_filter']}")
        logger.info(f"Prediction errors: {stats['prediction_errors']} ({stats['error_rate']:.1%})")
        logger.info("="*60)
