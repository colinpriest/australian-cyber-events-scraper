"""
GPT-4o Enricher - Primary extraction using GPT-4o with ultra-specific prompts.

This module extracts structured cybersecurity incident data from full article text
using GPT-4o's superior reasoning capabilities.
"""

from __future__ import annotations

import logging
import json
from typing import Dict, Any
from datetime import datetime
from openai import OpenAI
from cyber_data_collector.utils.validation import llm_validate_records_affected


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

        full_text = content.get('full_text', '')
        if len(full_text) > 8000:
            self.logger.info(
                f"Article content truncated from {len(full_text)} to 8000 chars for: "
                f"{content.get('title', '')[:50]}"
            )
            full_text = full_text[:8000]

        return f"""You are a cybersecurity incident analyst extracting structured data from news articles about cyber attacks.

ARTICLE CONTENT:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title: {content.get('title', 'N/A')}
URL: {content.get('url', 'N/A')}
Publication Date: {content.get('publication_date', 'N/A')}
Source Reliability: {content.get('source_reliability', 0.6)}

Full Article Text:
{full_text}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EXTRACTION TASK:
Extract the following information about this cyber security incident. Be EXTREMELY precise and conservative.

═══════════════════════════════════════════════════════════════════════════════
1. VICTIM ORGANIZATION EXTRACTION
═══════════════════════════════════════════════════════════════════════════════

🎯 IMPORTANT: Check the TITLE first to identify the primary victim organization! 🎯

The article TITLE indicates which organization is the primary focus. Strongly prefer extracting
organizations mentioned in the title, unless the title is clearly wrong/misleading.

═══════════════════════════════════════════════════════════════════════════════
FEW-SHOT EXAMPLES: Tricky Cases (Learn from these!)
═══════════════════════════════════════════════════════════════════════════════

🔸 EXAMPLE 1: Aggregate Blog Post with Specific Title

URL: "https://fortian.com.au/blog/august-2025-cyber-update.html"
Title: "iiNet Data Breach Exposes 280,000 Customers"
Article: "iiNet, owned by TPG Telecom, confirmed a data breach affecting 280,000 customers...
[Later in article]: Optus also faced legal action this month for its 2022 breach...
[Later]: Dutch telecommunications companies were compromised by Salt Typhoon, a Chinese state-sponsored group...
[Later]: University of Western Australia experienced a DDoS attack..."

❌ WRONG extraction:
{{
  "victim": {{
    "organization": "Dutch telecommunications companies",  // WRONG! Not in title, just mentioned in article body
    "reasoning": "Article discusses Dutch telecoms breach by Salt Typhoon"
  }}
}}

✅ CORRECT extraction:
{{
  "victim": {{
    "organization": "iiNet",  // CORRECT! Matches title, even though article is aggregate blog
    "reasoning": "Title specifically focuses on iiNet data breach, making it the primary subject despite article being a monthly roundup"
  }},
  "specificity": {{
    "is_specific_incident": true,  // TRUE because title indicates specific incident focus
    "reasoning": "Title focuses on specific iiNet breach despite being from aggregate blog post"
  }}
}}

🔸 EXAMPLE 2: Aggregate Blog with Multiple Mentions

URL: "https://cybersecurity-blog.com/weekly-roundup.html"
Title: "Aussie ISP iiNet confirms data breach impacting more than 200k customers"
Article: "Australian ISP iiNet has confirmed a data breach... This comes after similar incidents at Optus and Medibank...
Salt Typhoon, a Chinese threat actor, has been targeting telecommunications companies globally, including Dutch firms KPN and VodafoneZiggo...
Meanwhile, Qantas announced enhanced security measures..."

❌ WRONG extraction:
{{
  "victim": {{
    "organization": "Qantas",  // WRONG! Mentioned last, seems recent, but not in title
    "reasoning": "Qantas mentioned in article"
  }}
}}

❌ ALSO WRONG:
{{
  "victim": {{
    "organization": "Dutch telecommunications companies",  // WRONG! Generic + not in title
    "reasoning": "Article discusses Salt Typhoon attacks on Dutch telecoms"
  }}
}}

✅ CORRECT extraction:
{{
  "victim": {{
    "organization": "iiNet",  // CORRECT! In title and opening paragraph
    "reasoning": "Title and article opening clearly identify iiNet as primary subject. Other organizations (Optus, Medibank, Qantas, Dutch telecoms) mentioned for context only"
  }},
  "specificity": {{
    "is_specific_incident": true,
    "reasoning": "Title indicates specific iiNet breach is the focus, despite article being a weekly roundup"
  }}
}}

🔸 EXAMPLE 3: Parent Company vs Subsidiary

URL: "https://news-site.com/cyber-incident"
Title: "Australia's TPG Telecom flags cyber incident in its iiNet system"
Article: "TPG Telecom has disclosed a cybersecurity incident affecting its subsidiary iiNet...
The breach compromised iiNet customer data... TPG acquired iiNet in 2015..."

✅ CORRECT extraction (Option A - Focus on subsidiary):
{{
  "victim": {{
    "organization": "iiNet",  // CORRECT! The actual system breached
    "reasoning": "Title mentions both TPG and iiNet, but iiNet system was specifically compromised"
  }}
}}

✅ ALSO CORRECT (Option B - Focus on parent):
{{
  "victim": {{
    "organization": "TPG Telecom",  // ALSO CORRECT! Parent company disclosing incident
    "reasoning": "Title leads with TPG Telecom as the organization disclosing the incident"
  }}
}}

Either is acceptable when both are mentioned in title. Key: Don't extract unrelated organizations mentioned later in the article.

═══════════════════════════════════════════════════════════════════════════════
END OF FEW-SHOT EXAMPLES
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
- records_affected: Integer (number of PEOPLE/CUSTOMERS/USERS whose data was compromised) OR null

  ⚠️ CRITICAL VALIDATION RULES FOR records_affected:

  DEFINITION: Number of distinct individuals/customers/users whose personal data was compromised.
  This is NOT: transaction count, database records, files, bytes, dollars, or financial figures.

  WHAT TO EXTRACT (⚠️ ALWAYS INCLUDE UNITS - "million", "thousand", etc.):
  ✓ "280,000 customers affected" → 280000
  ✓ "280 thousand customers" → 280000 (NOT 280!)
  ✓ "9.7 million people" → 9700000 (NOT 9 or 97!)
  ✓ "500,000 individuals" → 500000
  ✓ "500 thousand individuals" → 500000 (NOT 500!)
  ✓ "1.5 million users" → 1500000 (NOT 15 or 1500!)
  ✓ "50,000 patients" → 50000
  ✓ "6 million records" → 6000000 (NOT 6!)

  ⚠️ UNIT PARSING IS CRITICAL:
  • "6 million" means 6,000,000 not 6
  • "280 thousand" means 280,000 not 280
  • "1.5 million" means 1,500,000 not 15 or 1500
  • ALWAYS multiply by the stated unit (thousand = ×1,000, million = ×1,000,000)
  • Minimum realistic value: 50 records (anything smaller likely indicates missed units)

  WHAT NOT TO EXTRACT (use null instead):
  ✗ "3 million transactions processed" → null (transactions ≠ customers)
  ✗ "500 GB of data stolen" → null (bytes ≠ customers)
  ✗ "$2.5 million fine" → null (dollars ≠ customers)
  ✗ "100,000 database records" → null (ambiguous - might be transaction records)
  ✗ Phone numbers, dates, reference numbers → null
  ✗ Revenue or turnover figures → null

  VALIDATION CONSTRAINTS:
  • Maximum realistic value: 1,000,000,000 (1 billion customers)
  • Minimum realistic value: 50 (smaller numbers indicate parsing error)
  • If article doesn't specify number of PEOPLE affected, use null
  • DO NOT concatenate or combine multiple numbers
  • DO NOT extract numbers from unrelated context
  • If unsure whether it's people or something else, use null
  • ALWAYS apply unit multipliers (thousand, million, billion)

  AMBIGUOUS CASES:
  • "500,000 records" → Only extract if context clearly indicates these are customer/user records
  • If article uses "records" ambiguously without saying "customer records", use null
  • If you see a number with units like "6 million" but are unsure, extract null rather than risk missing the units

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

CRITICAL QUESTION: Does this article describe a SPECIFIC, CONCRETE cyber incident?

⚠️ IMPORTANT: Focus on WHETHER an incident is described, NOT on the article's style or purpose

SPECIFIC INCIDENT (is_specific_incident: true):

The article must describe a CONCRETE cyber incident with:
✓ An identified victim organization (who was attacked)
✓ A specific type of attack (breach, ransomware, DDoS, etc.)
✓ Concrete details about what happened

THESE ARE ALL SPECIFIC INCIDENTS (even if not primary sources):
✓ "Medibank confirms October 2022 data breach affecting 9.7M customers" (official announcement)
✓ "Qantas cyber security breach: What personal details were exposed" (investigation/analysis)
✓ "Timeline: How the Optus breach unfolded" (retrospective analysis)
✓ "Experts analyze the iiNet credential stuffing attack" (expert commentary on specific incident)
✓ "Lessons from the Toll Group ransomware attack" (using specific incident as case study)

NOT SPECIFIC (is_specific_incident: false):

✗ General cybersecurity news without specific victim ("Ransomware threats increase in 2024")
✗ Educational content without specific incident ("How to protect your business from phishing")
✗ Industry trends without specific incident ("Healthcare sector faces growing cyber risks")
✗ Opinion pieces about cybersecurity in general ("Why Australia needs better cyber laws")
✗ Multiple separate incidents in one article ("5 major breaches this quarter" - UNLESS title focuses on one)
✗ Aggregate blog/news roundups with generic titles ("Weekly Cyber News Roundup")

KEY DECISION RULE:

Ask yourself: "After reading this article, can I name:
  1. Which specific organization was attacked?
  2. What type of attack it was?
  3. When it happened (approximately)?"

If YES to all 3 → is_specific_incident: TRUE
If NO to any → is_specific_incident: FALSE

IMPORTANT CLARIFICATIONS:

✓ Analysis, commentary, or investigation OF a specific incident = SPECIFIC
✓ Follow-up reporting on a known incident = SPECIFIC
✓ Lessons/implications from a specific incident = SPECIFIC
✗ General advice not tied to a specific incident = NOT SPECIFIC
✗ Multiple incidents without focus on one = NOT SPECIFIC

EXTRACT:
- is_specific_incident: Boolean
- specificity_reasoning: String (cite which details from the article confirm this is/isn't a specific incident)

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
✗ AGGREGATE NEWS/BLOG POSTS covering multiple separate incidents (see critical note below)

CRITICAL EXAMPLE:
"HWL Ebsworth data breach exposes client data from Department of Home Affairs, Department of Defence"
→ has_multiple_victims: FALSE
→ victim_organization: "HWL Ebsworth" ONLY
→ reasoning: "HWL Ebsworth is the organization that was breached. Government departments are clients whose data was exposed in HWL's systems, but they were not directly attacked."

⚠️ CRITICAL: AGGREGATE BLOG POSTS & NEWS ROUNDUPS ⚠️

URLs like "blog/monthly-cyber-update.html", "roundup", "weekly-news", or "digest" often contain MULTIPLE SEPARATE incidents.

RULES FOR AGGREGATE ARTICLES:
1. Check the URL - does it suggest an aggregate/roundup? (e.g., blog/august-2025-update, weekly-digest, etc.)
2. Check the article content - does it discuss 5+ separate organizations or incidents?
3. If YES to either: This is likely an AGGREGATE article, NOT a specific incident
4. For aggregate articles:
   ✓ If the TITLE mentions a SPECIFIC organization → extract ONLY that organization
   ✓ If the TITLE is generic (e.g., "Weekly Cyber News") → mark as NOT a specific incident
   ✗ DO NOT extract organizations from the article body if title doesn't mention them

AGGREGATE ARTICLE EXAMPLES:

Example 1: URL: "fortian.com.au/blog/august-2025-cyber-update.html"
Title: "iiNet Data Breach Exposes 280,000 Customers"
Article: Discusses iiNet breach, Optus legal action, Dutch telecoms attack, university incidents
→ victim_organization: "iiNet" (matches title, even though article mentions others)
→ is_specific_incident: TRUE
→ specificity_reasoning: "Title specifically focuses on iiNet breach, though article is from a blog roundup"

Example 2: URL: "cybersecurity-news.com/weekly-roundup-march-2025.html"
Title: "Weekly Cybersecurity Roundup - March 2025"
Article: Discusses 10 different breaches across various companies
→ victim_organization: null
→ is_specific_incident: FALSE
→ specificity_reasoning: "Aggregate weekly news roundup covering multiple separate incidents"

Example 3: URL: "news-site.com/blog/monthly-update"
Title: "Optus Faces Record Fine for 2022 Data Breach"
Article: Also mentions Medibank, Latitude, and industry trends
→ victim_organization: "Optus" (matches title)
→ is_specific_incident: TRUE
→ specificity_reasoning: "Title focuses on specific Optus incident, despite being from monthly blog"

TITLE PRIORITIZATION RULE:
When in doubt, ALWAYS prioritize what the TITLE says over what's in the article body. The title indicates the primary focus of the article.

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

            # Track token usage
            from cyber_data_collector.utils.token_tracker import tracker
            if response.usage:
                tracker.record(
                    self.model, response.usage.prompt_tokens,
                    response.usage.completion_tokens, context="gpt4o_enrichment",
                )

            # Parse JSON response
            result = json.loads(response.choices[0].message.content)

            # Apply validation to records_affected with LLM fallback
            if 'incident' in result and 'records_affected' in result['incident']:
                import os
                original_value = result['incident']['records_affected']
                victim_org = result.get('victim', {}).get('organization')
                validated_value, _ = llm_validate_records_affected(
                    original_value,
                    content.get('title', 'Unknown Event'),
                    org_name=victim_org,
                    description=content.get('clean_summary') or content.get('full_text', '')[:500],
                    perplexity_api_key=os.getenv('PERPLEXITY_API_KEY'),
                )
                if validated_value != original_value and original_value is not None:
                    self.logger.warning(
                        f"Adjusted records_affected from {original_value:,} to "
                        f"{validated_value if validated_value is not None else 'NULL'} "
                        f"for event: {content.get('title', '')}"
                    )
                result['incident']['records_affected'] = validated_value

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
                'organization': None,
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
