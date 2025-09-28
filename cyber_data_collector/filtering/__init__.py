"""
Multi-stage progressive filtering system for cyber event detection.

This module implements a three-stage filtering approach with confidence-based thresholding:
1. Stage 1: Broad inclusion filter at discovery (minimal false negatives)
2. Stage 2: Content-based refinement after scraping (balanced approach)
3. Stage 3: LLM-based final classification (high precision)
"""

from .confidence_filter import ConfidenceBasedFilter
from .progressive_filter import ProgressiveFilterSystem

__all__ = ['ConfidenceBasedFilter', 'ProgressiveFilterSystem']