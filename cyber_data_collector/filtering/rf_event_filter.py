#!/usr/bin/env python3
"""
Random Forest Event Filter

This module provides a Random Forest-based filter for cyber events that replaces
the existing 3-stage progressive filtering system with a machine learning approach.
"""

from __future__ import annotations

import pickle
import logging
import re
import warnings
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

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
    
    def __init__(self, model_dir: Optional[str] = None):
        """
        Initialize the Random Forest filter.

        Args:
            model_dir: Directory containing the trained models. Defaults to the
                ``machine_learning_filter`` directory at the project root.
        """
        if model_dir is None:
            model_dir = str(Path(__file__).resolve().parent.parent.parent / "machine_learning_filter")
        self.model_dir = Path(model_dir)
        self.model = None
        self.source_type_encoder = None
        self.text_vectorizer = None
        self.is_loaded = False
        
        # Statistics
        self.stats = {
            'events_processed': 0,
            'events_kept': 0,
            'events_filtered': 0,
            'prediction_errors': 0
        }
        
        # Load models
        self._load_models()
    
    # Validation callables keyed by filename.  Each receives the unpickled
    # object and returns True when the object looks usable.
    _VALIDATORS: Dict[str, Any] = {
        'random_forest_filter.pkl': lambda obj: (
            callable(getattr(obj, 'predict', None))
            and callable(getattr(obj, 'predict_proba', None))
            and hasattr(obj, 'estimators_')
        ),
        'source_type_encoder.pkl': lambda obj: (
            callable(getattr(obj, 'transform', None))
            and hasattr(obj, 'classes_')
        ),
        'text_vectorizer.pkl': lambda obj: (
            callable(getattr(obj, 'transform', None))
            and hasattr(obj, 'vocabulary_')
        ),
    }

    def _load_pickle(self, path: Path) -> Any:
        """Load a pickle file, re-pickling it when a sklearn version mismatch is detected.

        If the warning fires the loaded object is validated via ``_VALIDATORS``.
        A valid object is re-pickled to silence the warning on future runs.
        An invalid object raises ``RuntimeError`` and stops the script.
        """
        from sklearn.exceptions import InconsistentVersionWarning

        caught_version_warnings: list = []
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", InconsistentVersionWarning)
            with open(path, 'rb') as f:
                obj = pickle.load(f)
            caught_version_warnings = [
                w for w in caught
                if issubclass(w.category, InconsistentVersionWarning)
            ]

        if not caught_version_warnings:
            return obj

        # Version mismatch detected — validate before trusting the object.
        validator = self._VALIDATORS.get(path.name)
        is_valid = validator(obj) if validator is not None else True

        if not is_valid:
            raise RuntimeError(
                f"Model file '{path}' failed validation after a sklearn version mismatch. "
                f"The loaded object appears corrupt or incompatible. "
                f"Re-train the model and re-save it with the current sklearn version. "
                f"Original warning: {caught_version_warnings[0].message}"
            )

        # Valid — re-pickle to silence the warning on future runs.
        with open(path, 'wb') as f:
            pickle.dump(obj, f)
        logger.info(
            "Re-pickled '%s' to the current sklearn version (mismatch: %s)",
            path.name, caught_version_warnings[0].message,
        )
        return obj

    def _load_models(self):
        """Load the trained Random Forest model and preprocessing components."""
        try:
            model_path = self.model_dir / 'random_forest_filter.pkl'
            encoder_path = self.model_dir / 'source_type_encoder.pkl'
            vectorizer_path = self.model_dir / 'text_vectorizer.pkl'

            if not all(path.exists() for path in [model_path, encoder_path, vectorizer_path]):
                raise FileNotFoundError(f"Model files not found in {self.model_dir}")

            logger.info("Loading Random Forest filter models...")

            self.model = self._load_pickle(model_path)
            self.source_type_encoder = self._load_pickle(encoder_path)
            self.text_vectorizer = self._load_pickle(vectorizer_path)

            self.is_loaded = True
            logger.info("Random Forest filter models loaded successfully")

        except Exception as e:
            logger.error(f"Failed to load Random Forest models: {e}")
            self.is_loaded = False
            raise
    
    def preprocess_url(self, url: str) -> str:
        """
        Preprocess URL by removing protocol and replacing separators with spaces.
        
        Args:
            url: Original URL
            
        Returns:
            Preprocessed URL text
        """
        if pd.isna(url) or not url:
            return ""
        
        # Remove protocol
        url = re.sub(r'^https?://', '', str(url))
        
        # Replace separators with spaces
        url = re.sub(r'[-_]', ' ', url)
        
        # Remove common URL patterns that don't add meaning
        url = re.sub(r'\.(com|org|net|edu|gov|au|uk|us)/?', ' ', url)
        url = re.sub(r'www\.', '', url)
        
        # Clean up multiple spaces
        url = re.sub(r'\s+', ' ', url).strip()
        
        return url
    
    def preprocess_text(self, text: str) -> str:
        """
        Basic text preprocessing for NLP features.
        
        Args:
            text: Original text
            
        Returns:
            Preprocessed text
        """
        if pd.isna(text) or not text:
            return ""
        
        # Convert to string and lowercase
        text = str(text).lower()
        
        # Remove extra whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        
        return text
    
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
            
            # Determine if event should be kept
            is_cyber_relevant = bool(prediction)
            
            # Generate reasoning
            combined_text = (
                self.preprocess_text(title) + ' ' +
                self.preprocess_text(description) + ' ' +
                self.preprocess_text(content) + ' ' +
                self.preprocess_text(self.preprocess_url(url))
            )
            
            reasoning = [
                f"Random Forest prediction: {'KEEP' if is_cyber_relevant else 'FILTER'}",
                f"Confidence score: {confidence_score:.3f}",
                f"Source type: {source_type}",
                f"Text length: {len(combined_text)}"
            ]
            
            # Update statistics
            self.stats['events_processed'] += 1
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
        logger.info(f"Prediction errors: {stats['prediction_errors']} ({stats['error_rate']:.1%})")
        logger.info("="*60)
