"""
GPT-4o Enricher - Primary extraction using GPT-4o with ultra-specific prompts.

This module extracts structured cybersecurity incident data from full article text
using GPT-4o's superior reasoning capabilities.
"""

import logging
import json
from typing import Dict, Any
from datetime import datetime
from openai import OpenAI


class GPT4oEnricher:
    """Primary enrichment using GPT-4o with ultra-specific prompts"""

    NIST_CATEGORIES = [
        'GOVERNMENT', 'FINANCIAL_SERVICES', 'HEALTHCARE', 'EDUCATION',
        'TECHNOLOGY', 'MEDIA_COMMUNICATIONS', 'RETAIL_COMMERCE',
        'TELECOMMUNICATIONS', 'TRANSPORTATION', 'ENERGY_UTILITIES',
        'MANUFACTURING', 'NON_PROFIT', 'LEGAL_SERVICES', 'REAL_ESTATE',
        'AGRICULTURE', 'MINING', 'CONSTRUCTION', 'HOSPITALITY',
        'ENTERTAINMENT', 'OTHER'
    ]

    def __init__(self, api_key: str):
        """Initialize GPT-4o enricher with OpenAI API key"""
        if not api_key:
            raise ValueError("OpenAI API key required for GPT4oEnricher")

        self.client = OpenAI(api_key=api_key)
        self.model = "gpt-4o"  # Latest GPT-4o model
        self.logger = logging.getLogger(__name__)

    def create_extraction_prompt(self, content: Dict[str, Any]) -> str:
        """
        Create ultra-specific prompt with detailed extraction rules and examples.

        Args:
            content: Dict with 'title', 'full_text', 'url', 'publication_date'

        Returns:
            Detailed extraction prompt
        """

        categories_list = ', '.join(self.NIST_CATEGORIES)

        return f"""You are a cybersecurity incident analyst extracting structured data from news articles about cyber attacks.

ARTICLE CONTENT:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title: {content.get('title', 'N/A')}
URL: {content.get('url', 'N/A')}
Publication Date: {content.get('publication_date', 'N/A')}
Source Reliability: {content.get('source_reliability', 0.6)}

Full Article Text:
{content.get('full_text', '')[:8000]}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EXTRACTION TASK:
Extract the following information about this cyber security incident. Be EXTREMELY precise and conservative.

═══════════════════════════════════════════════════════════════════════════════
1. VICTIM ORGANIZATION EXTRACTION
═══════════════════════════════════════════════════════════════════════════════

CRITICAL RULES:
✓ Extract the SPECIFIC organization that was DIRECTLY attacked/breached
✓ Use the exact legal name when mentioned (e.g., "Medibank Private Limited" or "Medibank")
✓ For government agencies, include full name (e.g., "Department of Home Affairs")
✓ For universities, use full official name (e.g., "Western Sydney University" not "WSU")

✗ DO NOT EXTRACT:
  • Organizations mentioned ONLY for comparison ("similar to the Optus breach last year")
  • Organizations mentioned as clients/customers of the actual victim
  • Security researchers, experts, or consultants quoted in the article
  • Parent companies unless they were also directly breached
  • Generic references ("an Australian company", "a healthcare provider", "a financial institution")
  • Multiple organizations mentioned in different contexts

CORRECT EXAMPLES:

Example 1: "Medibank Private confirms data breach affecting 9.7 million customers"
→ victim_organization: "Medibank Private"
→ reasoning: "Medibank Private is explicitly identified as the organization that was breached"

Example 2: "Toll Group hit by Mailto ransomware attack, halting deliveries"
→ victim_organization: "Toll Group"
→ reasoning: "Toll Group is clearly identified as the target of the ransomware attack"

Example 3: "Western Sydney University discloses cyber incident affecting student records"
→ victim_organization: "Western Sydney University"
→ reasoning: "WSU is explicitly named as experiencing the cyber incident"

INCORRECT EXAMPLES (What NOT to do):

Example 1: "Security expert Jane Smith discusses the Optus breach at a conference"
→ ✗ WRONG: victim_organization: "Jane Smith"
→ ✓ CORRECT: victim_organization: "Optus" (Jane Smith is an expert, not the victim)

Example 2: "HWL Ebsworth data breach exposes information from Home Affairs, PM&C, and Defence clients"
→ ✗ WRONG: victim_organization: "Department of Home Affairs"
→ ✓ CORRECT: victim_organization: "HWL Ebsworth" (the law firm was breached; government departments are affected clients)

Example 3: "This attack follows similar incidents at Optus and Medibank last year"
→ ✗ WRONG: victim_organization: "Optus" or "Medibank"
→ ✓ CORRECT: victim_organization: null (these are mentioned for context only, not the subject of this article)

Example 4: "Multiple Australian companies targeted in Q1 2024 cyber campaign"
→ ✗ WRONG: victim_organization: "Australian companies"
→ ✓ CORRECT: victim_organization: null (too generic, not a specific incident)

Example 5: "The breach at an unnamed Australian healthcare provider affected 50,000 patients"
→ ✗ WRONG: victim_organization: "Australian healthcare provider"
→ ✓ CORRECT: victim_organization: null (organization not specifically named)

EXTRACT:
- victim_organization: String (exact organization name) OR null
- victim_industry: One of [{categories_list}] OR null
- is_australian_organization: Boolean (is victim Australian-based?)
- extraction_confidence: Float 0.0-1.0 (how certain are you?)
- reasoning: String (explain your decision in 1-2 sentences, citing specific text)

═══════════════════════════════════════════════════════════════════════════════
2. ATTACKER INFORMATION EXTRACTION
═══════════════════════════════════════════════════════════════════════════════

RULES:
✓ Extract threat actor name if EXPLICITLY mentioned (e.g., "LockBit", "APT29", "REvil")
✓ Extract ransomware/malware name if mentioned (e.g., "Mailto", "WannaCry")
✓ Classify attacker type based on evidence

✗ DO NOT EXTRACT:
  • Speculation ("believed to be state-sponsored" without confirmation)
  • Generic descriptions ("unknown hackers", "cybercriminals") - use "Unknown" instead

ATTACKER TYPES:
- nation_state: State-sponsored actors (e.g., APT29, Fancy Bear)
- ransomware_gang: Ransomware groups (e.g., LockBit, REvil)
- hacktivist: Ideologically motivated (e.g., Anonymous)
- cybercriminal: Financially motivated criminals
- insider: Internal threat actor
- unknown: Cannot determine from article

EXTRACT:
- attacker_name: String OR "Unknown"
- attacker_type: One of the types above
- malware_used: String (e.g., "Mailto", "REvil") OR null
- attack_vector: String (e.g., "phishing", "unpatched Citrix vulnerability") OR null
- extraction_confidence: Float 0.0-1.0
- reasoning: String (cite specific evidence)

═══════════════════════════════════════════════════════════════════════════════
3. INCIDENT DETAILS EXTRACTION
═══════════════════════════════════════════════════════════════════════════════

RULES:
✓ Extract ONLY confirmed facts, not speculation
✓ Distinguish between incident date (when attack occurred), discovery date (when found), and disclosure date (when announced)
✓ Be conservative with severity assessment

SEVERITY LEVELS:
- critical: National security impact, millions affected, critical infrastructure
- high: Significant impact, thousands affected, sensitive data (health, financial)
- medium: Moderate impact, hundreds affected, standard personal data
- low: Minor impact, minimal data exposure
- unknown: Cannot determine from article

EXTRACT:
- incident_date: "YYYY-MM-DD" OR null (when attack/breach occurred)
- discovery_date: "YYYY-MM-DD" OR null (when breach was discovered)
- disclosure_date: "YYYY-MM-DD" OR null (when publicly announced)
- severity: One of the levels above
- records_affected: Integer (number of records/people) OR null
- data_types_compromised: List of strings (e.g., ["personal_information", "financial_data", "health_records"])
- extraction_confidence: Float 0.0-1.0
- reasoning: String (explain severity assessment and what data supports your extraction)

═══════════════════════════════════════════════════════════════════════════════
4. AUSTRALIAN RELEVANCE ASSESSMENT
═══════════════════════════════════════════════════════════════════════════════

RULES:
Consider an incident Australian-relevant if ANY of these apply:
✓ Australian organization was the victim
✓ Australian citizens' data was compromised
✓ Attack originated from or targeted Australian infrastructure
✓ Significant impact on Australian critical infrastructure

EXTRACT:
- is_australian_event: Boolean
- australian_relevance_score: Float 0.0-1.0 (how relevant to Australia?)
- relevance_reasoning: String (explain why this is/isn't Australian-relevant)

═══════════════════════════════════════════════════════════════════════════════
5. EVENT SPECIFICITY ASSESSMENT
═══════════════════════════════════════════════════════════════════════════════

SPECIFIC INCIDENT (is_specific_incident: true):
✓ Article describes ONE specific cyber attack/breach with identified victim
✓ Example: "Medibank confirms October 2022 data breach affecting 9.7M customers"
✓ Example: "Toll Group hit by Mailto ransomware on May 1, 2020"

NOT SPECIFIC (is_specific_incident: false):
✗ General cybersecurity news or analysis ("Ransomware threats increase in 2024")
✗ Multiple separate incidents in one article ("5 major breaches this quarter")
✗ Educational content ("How to protect your business from phishing")
✗ Expert commentary without specific incident focus

EXTRACT:
- is_specific_incident: Boolean
- specificity_reasoning: String (explain decision)

═══════════════════════════════════════════════════════════════════════════════
6. MULTI-VICTIM DETECTION
═══════════════════════════════════════════════════════════════════════════════

CRITICAL DISTINCTION:

ONE ATTACK, MULTIPLE VICTIMS (has_multiple_victims: true):
✓ A SINGLE attack/campaign simultaneously affecting multiple distinct organizations
✓ Example: "SolarWinds supply chain attack affects Microsoft, FireEye, and multiple government agencies"
✓ Example: "Ransomware gang simultaneously breaches Acme Corp, Beta Inc, and Gamma LLC"

NOT MULTI-VICTIM (has_multiple_victims: false):
✗ One organization breached, affecting its clients (victim = the breached org, not the clients)
✗ Article mentions past breaches for context
✗ Article discusses industry trends affecting multiple companies separately

CRITICAL EXAMPLE:
"HWL Ebsworth data breach exposes client data from Department of Home Affairs, Department of Defence"
→ has_multiple_victims: FALSE
→ victim_organization: "HWL Ebsworth" ONLY
→ reasoning: "HWL Ebsworth is the organization that was breached. Government departments are clients whose data was exposed in HWL's systems, but they were not directly attacked."

EXTRACT:
- has_multiple_victims: Boolean
- victim_organizations: List of strings (only if has_multiple_victims = true) OR null
- multi_victim_reasoning: String (explain decision)

═══════════════════════════════════════════════════════════════════════════════
RESPONSE FORMAT
═══════════════════════════════════════════════════════════════════════════════

Respond with ONLY a valid JSON object. No markdown, no explanation outside the JSON.

{{
  "victim": {{
    "organization": "exact organization name or null",
    "industry": "{categories_list[0]} or null",
    "is_australian": true or false,
    "confidence": 0.0 to 1.0,
    "reasoning": "brief explanation citing specific text from article"
  }},
  "attacker": {{
    "name": "threat actor name or Unknown",
    "type": "nation_state|ransomware_gang|hacktivist|cybercriminal|insider|unknown",
    "malware": "malware name or null",
    "attack_vector": "vector description or null",
    "confidence": 0.0 to 1.0,
    "reasoning": "brief explanation citing evidence"
  }},
  "incident": {{
    "date": "YYYY-MM-DD or null",
    "discovery_date": "YYYY-MM-DD or null",
    "disclosure_date": "YYYY-MM-DD or null",
    "severity": "critical|high|medium|low|unknown",
    "records_affected": integer or null,
    "data_types": ["type1", "type2"] or [],
    "confidence": 0.0 to 1.0,
    "reasoning": "brief explanation of severity and data support"
  }},
  "australian_relevance": {{
    "is_australian_event": true or false,
    "relevance_score": 0.0 to 1.0,
    "reasoning": "brief explanation"
  }},
  "specificity": {{
    "is_specific_incident": true or false,
    "reasoning": "brief explanation"
  }},
  "multi_victim": {{
    "has_multiple_victims": true or false,
    "victim_organizations": ["Org1", "Org2"] or null,
    "reasoning": "brief explanation"
  }},
  "overall_confidence": 0.0 to 1.0,
  "extraction_notes": "any important observations or caveats"
}}
"""

    def extract(self, content: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract structured data using GPT-4o.

        Args:
            content: Dict with full article content from ContentAcquisitionService

        Returns:
            Extracted structured data with confidence scores
        """

        prompt = self.create_extraction_prompt(content)

        try:
            self.logger.info(f"Calling GPT-4o for extraction...")

            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a precise cybersecurity incident analyst. Always respond with valid JSON only. Be extremely conservative and accurate in your extractions."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0.1,  # Low temperature for consistency
                response_format={"type": "json_object"}  # Force JSON output
            )

            # Parse JSON response
            result = json.loads(response.choices[0].message.content)

            # Add metadata
            result['extraction_metadata'] = {
                'model': self.model,
                'timestamp': datetime.now().isoformat(),
                'tokens_used': response.usage.total_tokens,
                'source_url': content.get('url'),
                'source_reliability': content.get('source_reliability')
            }

            self.logger.info(f"✓ GPT-4o extraction successful (confidence: {result.get('overall_confidence', 0.0):.2f})")

            return result

        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse GPT-4o response as JSON: {e}")
            return self._error_result(f"JSON parsing error: {e}")

        except Exception as e:
            self.logger.error(f"GPT-4o extraction failed: {e}")
            return self._error_result(str(e))

    def _error_result(self, error_message: str) -> Dict[str, Any]:
        """Return error result structure"""
        return {
            'victim': {
                'organization': null,
                'industry': None,
                'is_australian': False,
                'confidence': 0.0,
                'reasoning': f"Extraction failed: {error_message}"
            },
            'attacker': {
                'name': 'Unknown',
                'type': 'unknown',
                'malware': None,
                'attack_vector': None,
                'confidence': 0.0,
                'reasoning': 'Extraction failed'
            },
            'incident': {
                'date': None,
                'discovery_date': None,
                'disclosure_date': None,
                'severity': 'unknown',
                'records_affected': None,
                'data_types': [],
                'confidence': 0.0,
                'reasoning': 'Extraction failed'
            },
            'australian_relevance': {
                'is_australian_event': False,
                'relevance_score': 0.0,
                'reasoning': 'Extraction failed'
            },
            'specificity': {
                'is_specific_incident': False,
                'reasoning': 'Extraction failed'
            },
            'multi_victim': {
                'has_multiple_victims': False,
                'victim_organizations': None,
                'reasoning': 'Extraction failed'
            },
            'overall_confidence': 0.0,
            'extraction_notes': f"ERROR: {error_message}",
            'extraction_metadata': {
                'model': self.model,
                'timestamp': datetime.now().isoformat(),
                'error': error_message
            }
        }
