"""
Confidence-based filtering system that assigns scores instead of binary accept/reject.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class FilterResult:
    """Result of applying a filter with confidence score and reasoning."""
    confidence_score: float  # 0.0 to 1.0
    is_cyber_relevant: bool  # True if score >= threshold
    reasoning: List[str]  # List of reasons for the score
    stage: str  # Which filtering stage this result is from

    @property
    def risk_level(self) -> str:
        """Convert confidence score to risk level."""
        if self.confidence_score >= 0.8:
            return "HIGH_CONFIDENCE"
        elif self.confidence_score >= 0.4:
            return "MEDIUM_CONFIDENCE"
        else:
            return "LOW_CONFIDENCE"


class ConfidenceBasedFilter:
    """
    Confidence-based filtering that assigns scores rather than binary decisions.

    This replaces the strict binary filtering with a more nuanced approach that:
    - Assigns confidence scores from 0.0 to 1.0
    - Provides reasoning for scores
    - Allows different thresholds for different stages
    - Prevents valid events from being discarded prematurely
    """

    def __init__(self):
        # Core cyber security terms (high confidence indicators)
        self.high_confidence_cyber_terms = [
            'cyber attack', 'cyberattack', 'cyber security', 'cybersecurity',
            'cyber threat', 'cyber incident', 'cyber breach', 'data breach',
            'ransomware', 'malware', 'phishing', 'ddos', 'dos attack',
            'hacking', 'hacker', 'hack', 'security breach', 'data leak',
            'vulnerability', 'exploit', 'zero-day', 'botnet', 'trojan',
            'spyware', 'adware', 'rootkit', 'keylogger', 'backdoor'
        ]

        # Medium confidence cyber terms
        self.medium_confidence_cyber_terms = [
            'virus', 'worm', 'firewall', 'antivirus', 'encryption',
            'authentication', 'authorization', 'intrusion', 'penetration',
            'social engineering', 'identity theft', 'fraud', 'scam',
            'credential', 'password', 'login', 'account', 'database',
            'network security', 'endpoint security', 'cloud security',
            'iot security', 'mobile security', 'web security'
        ]

        # Context terms that help disambiguate
        self.cyber_context_terms = [
            'security', 'attack', 'breach', 'incident', 'threat', 'risk',
            'compromise', 'unauthorized', 'malicious', 'suspicious',
            'investigation', 'forensics', 'response', 'mitigation',
            'patch', 'update', 'fix', 'protection', 'defense'
        ]

        # Strong negative indicators (clear non-cyber content)
        self.strong_negative_indicators = [
            # Events/celebrations
            'wedding', 'birthday', 'anniversary', 'graduation', 'ceremony',
            'parade', 'festival', 'celebration', 'party', 'holiday',
            'christmas', 'new year', 'easter', 'thanksgiving',

            # Sports
            'football', 'cricket', 'tennis', 'rugby', 'basketball',
            'olympics', 'world cup', 'championship', 'tournament',
            'match', 'game', 'player', 'team', 'coach', 'sport',

            # Entertainment
            'movie', 'film', 'music', 'concert', 'book', 'novel',
            'art', 'gallery', 'museum', 'theater', 'theatre',

            # Natural disasters
            'bushfire', 'wildfire', 'flood', 'drought', 'storm',
            'cyclone', 'hurricane', 'earthquake', 'tsunami'
        ]

        # Moderate negative indicators (usually non-cyber but could have cyber aspects)
        self.moderate_negative_indicators = [
            # Medical (but could include medical device security)
            'covid', 'coronavirus', 'pandemic', 'epidemic', 'health',
            'hospital', 'doctor', 'nurse', 'patient', 'medical',

            # Education (but could include university cyber incidents)
            'education', 'school', 'university', 'college', 'student',
            'teacher', 'professor', 'academic', 'curriculum',

            # Transportation (but could include transport system security)
            'transport', 'traffic', 'road', 'highway', 'airport',
            'flight', 'airline', 'train', 'railway', 'bus'
        ]

        # Australian context terms (boost relevance for Australian events)
        self.australian_terms = [
            'australia', 'australian', 'sydney', 'melbourne', 'brisbane',
            'perth', 'adelaide', 'canberra', 'darwin', 'hobart',
            'nsw', 'vic', 'qld', 'wa', 'sa', 'tas', 'nt', 'act',
            'commonwealth', 'federal', 'state government', 'council',
            'ato', 'centrelink', 'medicare', 'acsc', 'asd', 'asio'
        ]

        # Precompiled regex patterns for performance
        self._narrative_pattern = re.compile(
            '|'.join([
                r'(attacked|breached|compromised|hacked|infiltrated)',
                r'(stolen|leaked|exposed|accessed)\s+(data|information|records)',
                r'(security\s+(incident|breach|alert|warning))',
                r'(cyber\s+(attack|threat|incident))',
                r'(personal\s+information|customer\s+data|sensitive\s+data)',
                r'(unauthorized\s+(access|use|disclosure))',
                r'(malware|ransomware|virus)\s+(detected|found|discovered)',
                r'(systems?\s+(down|offline|compromised|affected))',
            ]),
            re.IGNORECASE,
        )
        self._technical_pattern = re.compile(
            '|'.join([
                r'(ip\s+address|network|server|database|firewall)',
                r'(vulnerability|exploit|patch|update|cve-\d+)',
                r'(encryption|decryption|certificate|ssl|tls)',
                r'(authentication|authorization|credential|password)',
                r'(endpoint|api|application|software|system)',
                r'(log|alert|detection|monitoring|forensic)',
                r'(backup|restore|recovery|business\s+continuity)',
            ]),
            re.IGNORECASE,
        )
        self._incident_pattern = re.compile(
            '|'.join([
                r'(incident\s+(response|team|management))',
                r'(investigation|forensic\s+analysis)',
                r'(containment|mitigation|remediation)',
                r'(affected\s+(customers|users|individuals))',
                r'(notification|disclosure|reporting)',
                r'(law\s+enforcement|authorities|police)',
                r'(privacy\s+(commissioner|office|authority))',
                r'(compliance|regulatory|audit|assessment)',
            ]),
            re.IGNORECASE,
        )

    def evaluate_discovery_stage(self, title: str, description: str = "",
                                url: str = "", metadata: Optional[Dict[str, Any]] = None) -> FilterResult:
        """
        Stage 1: Broad inclusion filter for discovery phase.

        Very permissive - designed to minimize false negatives.
        Only filters out obviously non-cyber content.
        """
        reasoning = []
        score = 0.5  # Start neutral

        # Combine all available text
        text = f"{title} {description}".lower()
        url_lower = url.lower()

        # Strong positive indicators (high boost)
        high_matches = self._count_term_matches(text, self.high_confidence_cyber_terms)
        if high_matches > 0:
            boost = min(0.4, high_matches * 0.2)
            score += boost
            reasoning.append(f"High-confidence cyber terms found ({high_matches})")

        # Medium positive indicators (moderate boost)
        medium_matches = self._count_term_matches(text, self.medium_confidence_cyber_terms)
        if medium_matches > 0:
            boost = min(0.3, medium_matches * 0.1)
            score += boost
            reasoning.append(f"Medium-confidence cyber terms found ({medium_matches})")

        # Context terms (small boost when combined with other indicators)
        context_matches = self._count_term_matches(text, self.cyber_context_terms)
        if context_matches > 0 and (high_matches > 0 or medium_matches > 0):
            boost = min(0.2, context_matches * 0.05)
            score += boost
            reasoning.append(f"Supportive context terms found ({context_matches})")

        # Australian relevance boost
        aus_matches = self._count_term_matches(text, self.australian_terms)
        if aus_matches > 0:
            boost = min(0.15, aus_matches * 0.05)
            score += boost
            reasoning.append(f"Australian context detected ({aus_matches})")

        # Strong negative indicators (significant penalty)
        strong_neg_matches = self._count_term_matches(text, self.strong_negative_indicators)
        if strong_neg_matches > 0:
            penalty = min(0.6, strong_neg_matches * 0.2)
            score -= penalty
            reasoning.append(f"Strong non-cyber indicators found ({strong_neg_matches})")

        # Moderate negative indicators (smaller penalty, can be overcome)
        mod_neg_matches = self._count_term_matches(text, self.moderate_negative_indicators)
        if mod_neg_matches > 0:
            penalty = min(0.3, mod_neg_matches * 0.1)
            score -= penalty
            reasoning.append(f"Moderate non-cyber indicators found ({mod_neg_matches})")

        # URL analysis
        if any(term in url_lower for term in ['security', 'cyber', 'hack', 'breach']):
            score += 0.1
            reasoning.append("Cyber-relevant URL detected")

        # Ensure score stays in valid range
        score = max(0.0, min(1.0, score))

        # Very permissive threshold for discovery stage
        threshold = 0.2

        return FilterResult(
            confidence_score=score,
            is_cyber_relevant=score >= threshold,
            reasoning=reasoning,
            stage="discovery"
        )

    def evaluate_content_stage(self, title: str, content: str, url: str = "",
                             metadata: Optional[Dict[str, Any]] = None) -> FilterResult:
        """
        Stage 2: Content-based refinement after scraping.

        More sophisticated analysis using full content.
        Balanced approach between precision and recall.
        """
        reasoning = []
        score = 0.5  # Start neutral

        if not content or len(content.strip()) < 50:
            return FilterResult(
                confidence_score=0.1,
                is_cyber_relevant=False,
                reasoning=["Insufficient content for analysis"],
                stage="content"
            )

        title_lower = title.lower() if title else ""
        content_lower = content.lower()
        url_lower = url.lower()

        # Combine all text for analysis
        full_text = f"{title_lower} {content_lower}"

        # High confidence terms get more weight in content analysis
        high_matches = self._count_term_matches(full_text, self.high_confidence_cyber_terms)
        if high_matches > 0:
            boost = min(0.5, high_matches * 0.15)
            score += boost
            reasoning.append(f"High-confidence cyber terms: {high_matches}")

        # Medium confidence terms
        medium_matches = self._count_term_matches(full_text, self.medium_confidence_cyber_terms)
        if medium_matches > 0:
            boost = min(0.3, medium_matches * 0.08)
            score += boost
            reasoning.append(f"Medium-confidence cyber terms: {medium_matches}")

        # Context analysis - look for cyber-security narrative
        if self._has_cyber_narrative(content_lower):
            score += 0.2
            reasoning.append("Cyber security narrative detected")

        # Technical indicators in content
        if self._has_technical_indicators(content_lower):
            score += 0.15
            reasoning.append("Technical security indicators found")

        # Incident reporting language
        if self._has_incident_language(content_lower):
            score += 0.15
            reasoning.append("Incident reporting language detected")

        # Australian context (more important after scraping)
        aus_matches = self._count_term_matches(full_text, self.australian_terms)
        if aus_matches > 0:
            boost = min(0.2, aus_matches * 0.08)
            score += boost
            reasoning.append(f"Australian relevance: {aus_matches}")

        # Negative indicators with more nuanced scoring
        strong_neg_matches = self._count_term_matches(full_text, self.strong_negative_indicators)
        if strong_neg_matches > 0:
            # Less harsh penalty if we have strong cyber indicators
            base_penalty = min(0.5, strong_neg_matches * 0.15)
            if high_matches > 0:
                base_penalty *= 0.5  # Reduce penalty if cyber terms present
            score -= base_penalty
            reasoning.append(f"Non-cyber indicators: {strong_neg_matches}")

        # Content quality indicators
        if len(content) > 500:
            score += 0.05
            reasoning.append("Substantial content available")

        if len(content) > 2000:
            score += 0.05
            reasoning.append("Comprehensive content available")

        # Ensure valid range
        score = max(0.0, min(1.0, score))

        # Moderate threshold for content stage
        threshold = 0.4

        return FilterResult(
            confidence_score=score,
            is_cyber_relevant=score >= threshold,
            reasoning=reasoning,
            stage="content"
        )

    def evaluate_final_stage(self, title: str, content: str, url: str = "",
                           llm_analysis: Optional[Dict[str, Any]] = None,
                           metadata: Optional[Dict[str, Any]] = None) -> FilterResult:
        """
        Stage 3: Final classification based directly on LLM analysis.
        """
        reasoning = []
        score = 0.0
        is_relevant = False

        if llm_analysis:
            is_australian = llm_analysis.get('is_australian_event', False)
            is_specific = llm_analysis.get('is_specific_event', False)

            if is_australian and is_specific:
                is_relevant = True
                score = 0.9
                reasoning.append("LLM confirmed the event is a specific, Australian cyber incident.")
            else:
                if not is_australian:
                    reasoning.append("LLM determined the event is not Australian.")
                if not is_specific:
                    reasoning.append("LLM determined the event is not a specific incident.")
        else:
            reasoning.append("No LLM analysis was provided.")

        return FilterResult(
            confidence_score=score,
            is_cyber_relevant=is_relevant,
            reasoning=reasoning,
            stage="final"
        )

    def _count_term_matches(self, text: str, terms: List[str]) -> int:
        """Count how many terms from the list appear in the text."""
        count = 0
        text_lower = text.lower()
        for term in terms:
            pattern = r'\b' + re.escape(term) + r'\b'
            if re.search(pattern, text_lower):
                count += 1
        return count

    def _has_cyber_narrative(self, content: str) -> bool:
        """Check if content has a cyber security incident narrative."""
        return bool(self._narrative_pattern.search(content))

    def _has_technical_indicators(self, content: str) -> bool:
        """Check for technical security indicators in content."""
        return bool(self._technical_pattern.search(content))

    def _has_incident_language(self, content: str) -> bool:
        """Check for incident reporting and response language."""
        return bool(self._incident_pattern.search(content))