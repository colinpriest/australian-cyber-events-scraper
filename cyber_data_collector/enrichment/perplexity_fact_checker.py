"""
Perplexity Fact Checker - Verify extracted facts using Perplexity's real-time search.

This module cross-validates information extracted by GPT-4o by querying
Perplexity API for verification.
"""

from __future__ import annotations

import logging
import json
import time
import requests
from typing import Dict, Any, List
from datetime import datetime


class PerplexityFactChecker:
    """Use Perplexity to verify extracted facts with real-time web search"""

    def __init__(self, api_key: str):
        """Initialize Perplexity fact checker"""
        if not api_key:
            raise ValueError("Perplexity API key required")

        self.api_key = api_key
        self.api_url = "https://api.perplexity.ai/chat/completions"
        self.logger = logging.getLogger(__name__)

    def fact_check_extraction(self, extraction: Dict[str, Any]) -> Dict[str, Any]:
        """
        Cross-verify extracted facts using Perplexity's real-time search.

        Args:
            extraction: GPT-4o extraction result

        Returns:
            {
                'checks_performed': int,
                'checks_passed': int,
                'checks_failed': int,
                'details': List[Dict],
                'overall_verification_confidence': float
            }
        """

        checks = []

        # Check 1: Verify organization exists
        if extraction.get('victim', {}).get('organization'):
            self.logger.info("Fact-check 1: Verifying organization exists...")
            org_check = self.verify_organization_exists(
                extraction['victim']['organization']
            )
            checks.append(org_check)

        # Check 2: Verify incident occurred
        if (extraction.get('victim', {}).get('organization') and
            extraction.get('incident', {}).get('date')):
            self.logger.info("Fact-check 2: Verifying incident occurred...")
            incident_check = self.verify_incident_occurred(
                extraction['victim']['organization'],
                extraction['incident']['date']
            )
            checks.append(incident_check)

        # Check 3: Verify attacker attribution if claimed
        if (extraction.get('attacker', {}).get('name') and
            extraction['attacker']['name'] not in ['Unknown', 'unknown', None]):
            self.logger.info("Fact-check 3: Verifying attacker attribution...")
            attacker_check = self.verify_attacker_attribution(
                extraction['attacker']['name'],
                extraction['victim'].get('organization')
            )
            checks.append(attacker_check)

        # Check 4: Verify records affected if specified
        if extraction.get('incident', {}).get('records_affected'):
            self.logger.info("Fact-check 4: Verifying records count...")
            records_check = self.verify_records_count(
                extraction['victim'].get('organization'),
                extraction['incident']['records_affected']
            )
            checks.append(records_check)

        checks_passed = sum(1 for c in checks if c['verified'])
        checks_failed = sum(1 for c in checks if not c['verified'])

        result = {
            'checks_performed': len(checks),
            'checks_passed': checks_passed,
            'checks_failed': checks_failed,
            'details': checks,
            'overall_verification_confidence': self._calculate_verification_confidence(checks),
            'timestamp': datetime.now().isoformat()
        }

        self.logger.info(f"Fact-checking complete: {checks_passed}/{len(checks)} passed")

        return result

    def verify_organization_exists(self, org_name: str) -> Dict[str, Any]:
        """Verify that the organization is real and not a generic description"""

        prompt = f"""Is "{org_name}" a real, specific organization (not a generic description)?

Search for information about this organization and answer with JSON:

{{
  "is_real_organization": true or false,
  "organization_type": "company|government|nonprofit|university|other|generic",
  "country": "primary country of operation or null",
  "confidence": 0.0 to 1.0,
  "reasoning": "brief explanation with sources if possible",
  "sources": ["URL1", "URL2"] or []
}}

CRITICAL RULES:
- is_real_organization: FALSE if it's a generic description like "Australian IT company" or "healthcare provider"
- is_real_organization: TRUE only if you can confirm it's a specific legal entity with web presence
- Provide sources (URLs) if you find them
- If you cannot find ANY information about this organization, confidence should be < 0.3

Examples:
- "Medibank Private" → is_real_organization: true (major Australian health insurer)
- "Australian IT company" → is_real_organization: false (generic description)
- "John Smith" → is_real_organization: false (person's name, not organization)
"""

        response = self._call_perplexity(prompt)

        if not response:
            return self._check_error_result('organization_exists', org_name, "Perplexity API call failed")

        return {
            'check_type': 'organization_exists',
            'query': org_name,
            'verified': response.get('is_real_organization', False),
            'confidence': response.get('confidence', 0.0),
            'details': response,
            'timestamp': datetime.now().isoformat()
        }

    def verify_incident_occurred(self, org_name: str, incident_date: str) -> Dict[str, Any]:
        """Verify that the organization experienced a cyber incident around the specified date"""

        prompt = f"""Did "{org_name}" experience a cybersecurity incident (data breach, ransomware attack, cyber attack, etc.) on or around {incident_date}?

Search for reliable news sources and answer with JSON:

{{
  "incident_confirmed": true or false,
  "actual_date": "YYYY-MM-DD if different from provided date, or null",
  "incident_type": "type of incident (e.g., ransomware, data breach) or null",
  "sources": ["URL1", "URL2"],
  "confidence": 0.0 to 1.0,
  "reasoning": "brief explanation citing sources"
}}

CRITICAL RULES:
- incident_confirmed: TRUE only if you find credible news sources confirming the incident
- If date is slightly different (within 30 days), still confirm but provide actual_date
- Prefer Australian news sources (ABC, SMH, AFR) and cybersecurity sources (iTnews, ZDNet)
- If you cannot find ANY sources, confidence should be < 0.3
- Include at least 2 sources if possible
"""

        response = self._call_perplexity(prompt)

        if not response:
            return self._check_error_result('incident_occurred', f"{org_name} {incident_date}", "Perplexity API call failed")

        return {
            'check_type': 'incident_occurred',
            'query': f"{org_name} cyber incident {incident_date}",
            'verified': response.get('incident_confirmed', False),
            'confidence': response.get('confidence', 0.0),
            'details': response,
            'timestamp': datetime.now().isoformat()
        }

    def verify_attacker_attribution(self, attacker: str, victim: str) -> Dict[str, Any]:
        """Verify attacker attribution claims"""

        prompt = f"""Has the threat actor "{attacker}" been credibly linked to a cyber attack on "{victim}"?

Search for attribution information and answer with JSON:

{{
  "attribution_confirmed": true or false,
  "attribution_confidence": "confirmed|suspected|unverified",
  "sources": ["URL1", "URL2"],
  "confidence": 0.0 to 1.0,
  "reasoning": "brief explanation"
}}

CRITICAL RULES:
- attribution_confirmed: TRUE only if multiple credible sources confirm the attribution
- attribution_confidence:
  * "confirmed": Official attribution by government/cybersecurity firms
  * "suspected": Multiple sources suggest link but not officially confirmed
  * "unverified": Single source or speculation only
- Consider attribution claims carefully - they are often uncertain
- If no sources found, confidence < 0.3
"""

        response = self._call_perplexity(prompt)

        if not response:
            return self._check_error_result('attacker_attribution', f"{attacker} → {victim}", "Perplexity API call failed")

        return {
            'check_type': 'attacker_attribution',
            'query': f"{attacker} attack on {victim}",
            'verified': response.get('attribution_confirmed', False),
            'confidence': response.get('confidence', 0.0),
            'details': response,
            'timestamp': datetime.now().isoformat()
        }

    def verify_records_count(self, org_name: str, records_count: int) -> Dict[str, Any]:
        """Verify the number of records affected"""

        prompt = f"""In the "{org_name}" cyber incident, were approximately {records_count:,} records/people affected?

Search for reliable sources and answer with JSON:

{{
  "count_confirmed": true or false,
  "actual_count": integer (if you find a different verified number) or null,
  "count_range": "e.g., 9-10 million, or 50,000-100,000",
  "sources": ["URL1", "URL2"],
  "confidence": 0.0 to 1.0,
  "reasoning": "brief explanation citing sources"
}}

CRITICAL RULES:
- count_confirmed: TRUE if the number is within ±20% of the stated number
- If you find a significantly different number (>20% variance), set count_confirmed: FALSE and provide actual_count
- Provide count_range if sources give a range rather than exact number
- Prefer official statements from the organization or regulatory filings
- If no sources found, confidence < 0.3
"""

        response = self._call_perplexity(prompt)

        if not response:
            return self._check_error_result('records_affected', f"{org_name} {records_count}", "Perplexity API call failed")

        # Check if within acceptable range (±20%)
        verified = response.get('count_confirmed', False)

        if response.get('actual_count') and records_count > 0:
            variance = abs(records_count - response['actual_count']) / records_count
            verified = variance <= 0.20

        return {
            'check_type': 'records_affected',
            'query': f"{org_name} breach {records_count:,} records",
            'verified': verified,
            'confidence': response.get('confidence', 0.0),
            'details': response,
            'timestamp': datetime.now().isoformat()
        }

    def _call_perplexity(self, prompt: str) -> Dict[str, Any]:
        """Call Perplexity API with retry logic"""

        max_retries = 3

        for attempt in range(max_retries):
            try:
                payload = {
                    "model": "sonar-pro",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 1000,
                    "temperature": 0.1
                }

                response = requests.post(
                    self.api_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json"
                    },
                    json=payload,
                    timeout=60
                )

                if response.status_code == 200:
                    content = response.json()['choices'][0]['message']['content']

                    # Clean markdown formatting
                    if content.startswith('```json'):
                        content = content[7:]
                    if content.endswith('```'):
                        content = content[:-3]

                    try:
                        result = json.loads(content.strip())
                        return result
                    except json.JSONDecodeError as e:
                        self.logger.warning(f"Attempt {attempt + 1}: Failed to parse Perplexity JSON: {e}")
                        if attempt < max_retries - 1:
                            wait_time = 2 ** attempt  # 1s, 2s, 4s
                            time.sleep(wait_time)
                            continue
                        return None
                else:
                    self.logger.warning(f"Attempt {attempt + 1}: Perplexity API returned status {response.status_code}")
                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt  # 1s, 2s, 4s
                        time.sleep(wait_time)
                        continue

            except Exception as e:
                self.logger.warning(f"Attempt {attempt + 1}: Perplexity request failed: {e}")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # 1s, 2s, 4s
                    time.sleep(wait_time)
                    continue

        self.logger.error("All Perplexity API attempts failed")
        return None

    def _calculate_verification_confidence(self, checks: List[Dict]) -> float:
        """Calculate overall verification confidence from all checks"""

        if not checks:
            return 0.0

        # Weight different check types
        weights = {
            'organization_exists': 0.4,  # Most important - is org even real?
            'incident_occurred': 0.4,    # Second most important - did incident happen?
            'attacker_attribution': 0.1,  # Nice to have but often uncertain
            'records_affected': 0.1       # Nice to have but often estimated
        }

        weighted_sum = 0.0
        total_weight = 0.0

        for check in checks:
            check_type = check['check_type']
            weight = weights.get(check_type, 0.1)

            if check['verified']:
                # Passed check contributes positively
                weighted_sum += weight * check['confidence']
            else:
                # Failed check contributes negatively
                weighted_sum += weight * (1.0 - check['confidence']) * 0.5  # 50% penalty

            total_weight += weight

        return weighted_sum / total_weight if total_weight > 0 else 0.0

    def _check_error_result(self, check_type: str, query: str, error: str) -> Dict[str, Any]:
        """Return error result for a failed check"""
        return {
            'check_type': check_type,
            'query': query,
            'verified': False,
            'confidence': 0.0,
            'details': {
                'error': error
            },
            'timestamp': datetime.now().isoformat()
        }
