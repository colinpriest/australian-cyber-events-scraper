#!/usr/bin/env python3
"""
Experimental LLM classifier with configurable prompts for fine-tuning.

This module extends the existing LLM classifier to allow for prompt experimentation
and detailed confidence scoring.
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any
import instructor
import openai
from pydantic import BaseModel, Field

from cyber_data_collector.processing.llm_classifier import (
    LLMClassifier, EventEnhancement, EventEnhancementRequest
)

class ExperimentalEventEnhancement(BaseModel):
    """Enhanced event information with detailed confidence metrics."""

    is_cybersecurity_event: bool = Field(description="True if this is a genuine cybersecurity incident")
    is_australian_relevant: bool = Field(description="True if this specifically affects Australian organizations")
    rejection_reason: Optional[str] = Field(default=None, description="Detailed reason for rejection")

    # Confidence breakdown
    content_confidence: float = Field(default=0.0, description="Confidence in content analysis (0.0-1.0)")
    specificity_confidence: float = Field(default=0.0, description="Confidence this is a specific incident (0.0-1.0)")
    australian_confidence: float = Field(default=0.0, description="Confidence in Australian relevance (0.0-1.0)")
    overall_confidence: float = Field(default=0.0, description="Overall confidence in classification (0.0-1.0)")

    # Detailed analysis
    detected_indicators: List[str] = Field(default_factory=list, description="Cybersecurity indicators found")
    rejection_indicators: List[str] = Field(default_factory=list, description="Indicators for rejection")
    australian_indicators: List[str] = Field(default_factory=list, description="Australian relevance indicators")

class ExperimentalLLMClassifier(LLMClassifier):
    """Extended LLM classifier for experimentation."""

    def __init__(self, openai_api_key: Optional[str], prompt_variant: str = "default", model: str = "gpt-4o-mini"):
        super().__init__(openai_api_key)
        self.prompt_variant = prompt_variant
        self.model = model
        self.logger = logging.getLogger(self.__class__.__name__)

    async def classify_single_event_detailed(self, title: str, content: str,
                                           description: str = "") -> ExperimentalEventEnhancement:
        """
        Classify a single event with detailed confidence metrics.

        Returns detailed analysis including confidence breakdown.
        """

        if not self.client:
            return ExperimentalEventEnhancement(
                is_cybersecurity_event=False,
                is_australian_relevant=False,
                rejection_reason="No LLM client configured",
                overall_confidence=0.0
            )

        enhancement_request = EventEnhancementRequest(
            title=title,
            description=description,
            entity_names=[],
            raw_data_sources=[content[:2000]]  # Limit content length
        )

        try:
            response = await self._invoke_experimental_llm(enhancement_request)
            return response
        except Exception as e:
            self.logger.error(f"LLM classification failed: {e}")
            return ExperimentalEventEnhancement(
                is_cybersecurity_event=False,
                is_australian_relevant=False,
                rejection_reason=f"LLM error: {str(e)}",
                overall_confidence=0.0
            )

    async def _invoke_experimental_llm(self, request: EventEnhancementRequest) -> ExperimentalEventEnhancement:
        """Invoke LLM with experimental prompt variants."""

        if not self.client:
            raise RuntimeError("LLM client not configured")

        # Select prompt based on variant
        if self.prompt_variant == "strict":
            user_prompt = self._get_strict_prompt(request)
            system_prompt = self._get_strict_system_prompt()
        elif self.prompt_variant == "lenient":
            user_prompt = self._get_lenient_prompt(request)
            system_prompt = self._get_lenient_system_prompt()
        elif self.prompt_variant == "detailed":
            user_prompt = self._get_detailed_prompt(request)
            system_prompt = self._get_detailed_system_prompt()
        else:  # default
            user_prompt = self._get_default_prompt(request)
            system_prompt = self._get_default_system_prompt()

        response = await asyncio.to_thread(
            lambda: self.client.chat.completions.create(
                model=self.model,
                response_model=ExperimentalEventEnhancement,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_retries=2,
            )
        )
        return response

    def _get_default_prompt(self, request: EventEnhancementRequest) -> str:
        """Default prompt variant - current production prompt."""
        return f"""
FIRST, determine if this is actually a cybersecurity INCIDENT and if it's Australian-relevant.

Event Title: {request.title}
Event Description: {request.description}
Affected Entities: {', '.join(request.entity_names)}
Raw Data Snippets: {' '.join(request.raw_data_sources)}

STEP 1 - VALIDATION (CRITICAL):
- `is_cybersecurity_event`: Is this genuinely about ONE SPECIFIC cybersecurity INCIDENT that actually happened to a named organization?
  - Return TRUE ONLY for: actual data breaches, cyber attacks, malware infections, ransomware attacks, phishing campaigns that OCCURRED to ONE specific named company/organization
  - Return FALSE for general summaries, policy documents, training materials, multiple incidents, trend reports

- `is_australian_relevant`: Does this SPECIFIC INCIDENT affect Australian organizations, systems, or citizens?

STEP 2 - CONFIDENCE ASSESSMENT:
- `content_confidence`: How confident are you this is cybersecurity content? (0.0-1.0)
- `specificity_confidence`: How confident are you this is a specific incident vs. general content? (0.0-1.0)
- `australian_confidence`: How confident are you this is Australian-relevant? (0.0-1.0)
- `overall_confidence`: Overall confidence in your classification (0.0-1.0)

STEP 3 - EVIDENCE:
- `detected_indicators`: List specific cybersecurity indicators found
- `rejection_indicators`: List reasons for rejection (if applicable)
- `australian_indicators`: List Australian relevance indicators

Be extremely conservative. When in doubt, reject the event.
"""

    def _get_default_system_prompt(self) -> str:
        """Default system prompt."""
        return """You are a strict cybersecurity incident analyst. ONLY classify events that are genuine cybersecurity INCIDENTS where actual damage, compromise, or breach occurred to ONE SPECIFIC NAMED ORGANIZATION. Be extremely conservative and provide detailed confidence metrics."""

    def _get_strict_prompt(self, request: EventEnhancementRequest) -> str:
        """Strict prompt variant - very conservative filtering."""
        return f"""
STRICT CYBERSECURITY INCIDENT CLASSIFICATION

Event Title: {request.title}
Event Description: {request.description}
Content: {' '.join(request.raw_data_sources)}

CRITICAL REQUIREMENTS - ALL MUST BE TRUE:
1. Must be about ONE specific named organization that was actually attacked/breached
2. Must describe an actual incident that already occurred (not future risks, plans, or policies)
3. Must involve actual compromise of systems, data, or security
4. Must be recent and specific (not historical summaries or trend reports)
5. Must clearly affect Australian entities (organizations, citizens, or systems)

AUTOMATIC REJECTION for:
- Any mention of "multiple", "several", "various" incidents
- Policy documents, frameworks, guidelines, recommendations
- Training materials, educational content, best practices
- General risk assessments or security advice
- Market reports, trend analyses, statistics
- Time period summaries (e.g., "2020 breaches", "Q1 incidents")
- Government guidance or regulatory updates
- Future-focused or planning documents

CONFIDENCE SCORING:
- Only assign high confidence (>0.8) if you are absolutely certain
- Use medium confidence (0.4-0.8) for borderline cases
- Use low confidence (<0.4) for unclear or questionable content

Provide detailed evidence for your classification.
"""

    def _get_strict_system_prompt(self) -> str:
        """Strict system prompt."""
        return """You are an extremely strict cybersecurity incident classifier. Reject anything that is not clearly a specific cybersecurity incident affecting a named Australian organization. When in doubt, REJECT. Provide detailed confidence metrics and evidence."""

    def _get_lenient_prompt(self, request: EventEnhancementRequest) -> str:
        """Lenient prompt variant - more permissive filtering."""
        return f"""
CYBERSECURITY RELEVANCE ASSESSMENT

Event Title: {request.title}
Event Description: {request.description}
Content: {' '.join(request.raw_data_sources)}

CLASSIFICATION CRITERIA:
1. Is this related to cybersecurity, data protection, or digital security?
2. Does it involve Australian organizations, systems, or citizens?
3. Is it about a specific incident, breach, or security event?

ACCEPT if:
- Describes any cybersecurity incident, breach, or attack
- Mentions Australian organizations in security context
- Reports on specific security events or investigations
- Discusses actual security compromises or threats

REJECT ONLY if:
- Clearly not cybersecurity related (sports, entertainment, weather)
- No Australian connection whatsoever
- Pure policy/regulatory text with no incident details

CONFIDENCE SCORING:
- Be more generous with confidence scoring
- Consider partial matches and indirect relevance
- Account for potential cybersecurity implications

Provide evidence for your decision.
"""

    def _get_lenient_system_prompt(self) -> str:
        """Lenient system prompt."""
        return """You are a permissive cybersecurity content classifier. Include events that have potential cybersecurity relevance or Australian connection. Be generous in your interpretation."""

    def _get_detailed_prompt(self, request: EventEnhancementRequest) -> str:
        """Detailed analysis prompt variant."""
        return f"""
COMPREHENSIVE CYBERSECURITY INCIDENT ANALYSIS

Event Title: {request.title}
Event Description: {request.description}
Content: {' '.join(request.raw_data_sources)}

ANALYSIS FRAMEWORK:

1. CONTENT ANALYSIS:
   - Identify all cybersecurity-related terms and concepts
   - Assess the narrative structure (incident reporting vs. general discussion)
   - Evaluate specificity of details provided

2. INCIDENT SPECIFICITY:
   - Is this about a single, specific incident?
   - Does it name specific organizations or entities?
   - Does it describe actual events that occurred?

3. AUSTRALIAN RELEVANCE:
   - Direct mentions of Australian organizations
   - Australian geographic references
   - Australian regulatory or government context
   - Impact on Australian citizens or systems

4. CONFIDENCE ASSESSMENT:
   - Content confidence: Strength of cybersecurity indicators
   - Specificity confidence: Evidence this is a specific incident
   - Australian confidence: Strength of Australian connection
   - Overall confidence: Combined assessment

5. DETAILED EVIDENCE:
   - List all cybersecurity indicators found
   - List all specificity indicators
   - List all Australian relevance indicators
   - List any rejection indicators

Provide a thorough, evidence-based classification with detailed reasoning.
"""

    def _get_detailed_system_prompt(self) -> str:
        """Detailed analysis system prompt."""
        return """You are a thorough cybersecurity analyst conducting detailed incident classification. Provide comprehensive analysis with extensive evidence and detailed confidence metrics for each aspect of the classification."""

# Additional utility functions for testing

class LLMFilterTuner:
    """Utility class for systematic prompt tuning."""

    def __init__(self, openai_api_key: str):
        self.openai_api_key = openai_api_key
        self.results_cache = {}

    async def test_prompt_variants(self, events_data: List[Dict],
                                 variants: List[str] = None) -> Dict[str, Any]:
        """Test multiple prompt variants against the same events."""

        if variants is None:
            variants = ["default", "strict", "lenient", "detailed"]

        results = {}

        for variant in variants:
            print(f"Testing prompt variant: {variant}")
            classifier = ExperimentalLLMClassifier(
                self.openai_api_key,
                prompt_variant=variant
            )

            variant_results = []
            for event_data in events_data:
                result = await classifier.classify_single_event_detailed(
                    title=event_data['title'],
                    content=event_data['content'],
                    description=event_data.get('description', '')
                )
                variant_results.append({
                    'event_id': event_data.get('event_id', ''),
                    'result': result,
                    'expected': event_data.get('expected_keep', None)
                })

            results[variant] = variant_results

        return results

    def analyze_prompt_performance(self, results: Dict[str, Any]) -> Dict[str, Dict]:
        """Analyze performance metrics for each prompt variant."""

        performance = {}

        for variant, variant_results in results.items():
            tp = fp = tn = fn = 0

            for item in variant_results:
                expected = item.get('expected')
                result = item['result']

                if expected is None:
                    continue

                predicted = result.is_cybersecurity_event and result.is_australian_relevant

                if expected and predicted:
                    tp += 1
                elif not expected and not predicted:
                    tn += 1
                elif not expected and predicted:
                    fp += 1
                elif expected and not predicted:
                    fn += 1

            # Calculate metrics
            precision = tp / (tp + fp) if (tp + fp) > 0 else 0
            recall = tp / (tp + fn) if (tp + fn) > 0 else 0
            f1 = 2 * (precision * recall) / (precision + recall) if (precision + recall) > 0 else 0
            accuracy = (tp + tn) / (tp + fp + tn + fn) if (tp + fp + tn + fn) > 0 else 0

            # Average confidence scores
            avg_confidence = sum(item['result'].overall_confidence for item in variant_results) / len(variant_results)

            performance[variant] = {
                'precision': precision,
                'recall': recall,
                'f1_score': f1,
                'accuracy': accuracy,
                'avg_confidence': avg_confidence,
                'true_positives': tp,
                'false_positives': fp,
                'true_negatives': tn,
                'false_negatives': fn
            }

        return performance