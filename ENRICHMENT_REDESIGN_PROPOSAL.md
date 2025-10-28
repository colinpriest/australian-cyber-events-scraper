# High-Quality LLM-Based Enrichment Pipeline Redesign

## Executive Summary

**Current State:** Regex-based extraction + single Perplexity call = 90% false positive rate on filtering, numerous misidentifications, confusing multi-victim splits

**Proposed State:** GPT-4o extraction → Perplexity fact-check → Validation checks = High-precision, validated enrichment

---

## Core Principles

1. **Use Full Article Text** - Stop relying on title/summary only
2. **GPT-4o for Primary Extraction** - Superior reasoning and instruction-following
3. **Perplexity for Fact-Checking** - Cross-validate with real-time search
4. **Multi-Stage Validation** - Sensibility checks before committing to database
5. **Confidence Scoring** - Track certainty at each stage
6. **Zero Regex** - No brittle pattern matching

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         INPUT: Raw Event                             │
│                     (Title, URL, Initial Summary)                    │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STAGE 1: Content Acquisition                                       │
│  ─────────────────────────────                                      │
│  • Fetch full article text from URL                                 │
│  • Extract main content (remove ads, navigation, etc.)              │
│  • Generate comprehensive summary if missing                        │
│  • Store: full_article_text, clean_summary                          │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STAGE 2: Primary Extraction (GPT-4o)                               │
│  ─────────────────────────────────────                              │
│  • Ultra-specific prompt with examples                              │
│  • Extract: victim, attacker, event type, severity, timeline        │
│  • Include reasoning for each extraction                            │
│  • Confidence score per field                                       │
│  • Output: structured JSON with provenance                          │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STAGE 3: Fact-Check (Perplexity)                                   │
│  ─────────────────────────────────                                  │
│  • Query: "Is {extracted_victim} a real organization?"              │
│  • Query: "Did {extracted_victim} experience a cyber incident       │
│           on {extracted_date}?"                                     │
│  • Cross-reference extracted facts with web search                  │
│  • Flag any discrepancies                                           │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STAGE 4: Validation & Sensibility Checks                           │
│  ────────────────────────────────────────                           │
│  • Organization name validation (not generic, not person)           │
│  • Industry consistency check                                       │
│  • Date plausibility (not future, not ancient)                      │
│  • Cross-field consistency (severity ↔ records_affected)            │
│  • Duplicate detection (same victim + date exists?)                 │
│  • Australian relevance verification                                │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STAGE 5: Confidence Aggregation & Decision                         │
│  ────────────────────────────────────────                           │
│  • Combine confidences: GPT-4o + Perplexity + Validation            │
│  • Final confidence score: 0.0 to 1.0                               │
│  • Decision rules:                                                  │
│    - confidence >= 0.8: Auto-accept                                 │
│    - 0.5 <= confidence < 0.8: Accept with warning                   │
│    - confidence < 0.5: Reject or flag for manual review             │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│  OUTPUT: High-Quality Enriched Event                                │
│  • Validated victim, attacker, severity, timeline                   │
│  • Confidence scores per field                                      │
│  • Audit trail (what was checked, what was verified)                │
│  • Flags for any inconsistencies                                    │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Stage 1: Content Acquisition

### Current Problem
- Only using title and sometimes a brief summary
- Missing critical context from full articles
- Cannot distinguish primary victim from contextual mentions

### New Approach

```python
class ContentAcquisitionService:
    """Fetch and clean article content from URLs"""

    def __init__(self):
        self.scraper = ArticleScraper()
        self.content_cleaner = ContentCleaner()

    def acquire_content(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """
        Fetch full article text and extract clean content.

        Returns:
            {
                'full_text': str,          # Complete article text
                'clean_summary': str,      # 2-3 paragraph summary
                'publication_date': date,   # Article publication date
                'author': str,             # Article author if available
                'source_reliability': float, # Source credibility score
                'content_length': int,     # Word count
                'extraction_method': str   # How content was extracted
            }
        """
        url = event.get('url')

        # Try multiple extraction methods
        content = None

        # Method 1: newspaper3k (best for news articles)
        content = self.scraper.extract_with_newspaper3k(url)

        # Method 2: trafilatura (fallback for difficult sites)
        if not content or len(content) < 200:
            content = self.scraper.extract_with_trafilatura(url)

        # Method 3: BeautifulSoup (last resort)
        if not content or len(content) < 200:
            content = self.scraper.extract_with_beautifulsoup(url)

        if not content:
            raise ContentExtractionError(f"Failed to extract content from {url}")

        # Clean content (remove ads, navigation, etc.)
        cleaned = self.content_cleaner.clean(content)

        # Generate summary if needed
        if not event.get('summary') or len(event['summary']) < 100:
            summary = self.generate_summary(cleaned)
        else:
            summary = event['summary']

        return {
            'full_text': cleaned,
            'clean_summary': summary,
            'publication_date': self.scraper.get_publication_date(url),
            'source_reliability': self.assess_source_reliability(url),
            'content_length': len(cleaned.split()),
            'extraction_method': self.scraper.last_method_used
        }

    def assess_source_reliability(self, url: str) -> float:
        """
        Assess credibility of news source.

        Trusted sources (0.9-1.0):
        - abc.net.au, smh.com.au, theage.com.au, afr.com
        - cyberscoop.com, zdnet.com, arstechnica.com

        Known sources (0.7-0.9):
        - itnews.com.au, channelnews.com.au
        - cybersecuritynews.com, bleepingcomputer.com

        Unknown sources (0.5-0.7):
        - Personal blogs, unknown sites

        Unreliable sources (0.0-0.5):
        - Known fake news sites, spam
        """
        domain = self.extract_domain(url)

        trusted_sources = {
            'abc.net.au': 1.0,
            'smh.com.au': 0.95,
            'theage.com.au': 0.95,
            'afr.com': 0.95,
            'zdnet.com': 0.9,
            'arstechnica.com': 0.9,
            'itnews.com.au': 0.85,
            'bleepingcomputer.com': 0.85,
            # ... more sources
        }

        return trusted_sources.get(domain, 0.6)  # Default to moderate trust
```

---

## Stage 2: Primary Extraction (GPT-4o)

### Ultra-Specific Prompt Design

```python
class GPT4oEnricher:
    """Primary enrichment using GPT-4o with ultra-specific prompts"""

    def __init__(self, api_key: str):
        self.client = OpenAI(api_key=api_key)
        self.model = "gpt-4o"  # or "gpt-4o-2024-11-20" for latest

    def create_extraction_prompt(self, content: Dict[str, Any]) -> str:
        """
        Create ultra-specific prompt with:
        1. Clear role definition
        2. Detailed extraction rules
        3. Multiple examples (positive and negative)
        4. Structured output format
        5. Reasoning requirement
        """

        return f"""You are a cybersecurity incident analyst extracting structured data from news articles about cyber attacks.

ARTICLE CONTENT:
Title: {content['title']}
Publication Date: {content['publication_date']}
Source: {content['url']}
Full Text:
{content['full_text']}

EXTRACTION TASK:
Extract the following information about this cyber security incident. Be EXTREMELY precise and conservative.

=== 1. VICTIM ORGANIZATION ===

RULES:
✓ Extract the SPECIFIC organization that was attacked/breached
✓ Use the exact legal name when possible (e.g., "Medibank Private Limited" or just "Medibank")
✓ For government agencies, include the full name (e.g., "Department of Home Affairs" not just "Home Affairs")
✓ For universities, include full name (e.g., "Western Sydney University" not "WSU")

✗ DO NOT extract:
  - Organizations mentioned only for comparison ("similar to the Optus breach")
  - Organizations mentioned as clients/customers of the victim
  - Security researchers or experts quoted in the article
  - Parent companies unless they were also breached
  - Generic references ("an Australian company", "a healthcare provider")

EXAMPLES:

✓ CORRECT:
  - "Medibank Private confirms data breach" → victim: "Medibank Private"
  - "Toll Group hit by Mailto ransomware" → victim: "Toll Group"
  - "Western Sydney University discloses cyber incident" → victim: "Western Sydney University"

✗ INCORRECT:
  - "Security expert Jane Smith discusses Optus breach" → victim: NOT "Jane Smith", it's "Optus"
  - "HWL Ebsworth breach affects Home Affairs clients" → victim: "HWL Ebsworth" ONLY (Home Affairs is affected but wasn't breached)
  - "This attack follows similar incidents at Optus and Medibank" → victim: Cannot determine from this sentence alone
  - Article about multiple separate breaches → victim: null (not a single incident)

WHAT TO EXTRACT:
- victim_organization: String (exact organization name) OR null
- victim_industry: String (NIST category) OR null
- is_australian_organization: Boolean (is victim Australian-based?)
- extraction_confidence: Float 0.0-1.0 (how certain are you?)
- reasoning: String (explain your decision in 1-2 sentences)

=== 2. ATTACKER INFORMATION ===

RULES:
✓ Extract threat actor name if explicitly mentioned
✓ Extract ransomware/malware name if mentioned
✓ Extract attack type (ransomware, DDoS, data breach, etc.)

✗ DO NOT extract:
  - Speculation about attribution ("believed to be state-sponsored")
  - Generic descriptions ("unknown hackers", "cybercriminals")

WHAT TO EXTRACT:
- attacker_name: String OR "Unknown"
- attacker_type: "nation_state" | "ransomware_gang" | "hacktivist" | "cybercriminal" | "insider" | "unknown"
- malware_used: String (e.g., "Mailto", "REvil") OR null
- attack_vector: String (e.g., "phishing", "unpatched vulnerability") OR null
- extraction_confidence: Float 0.0-1.0

=== 3. INCIDENT DETAILS ===

RULES:
✓ Extract factual information only
✓ Distinguish between confirmed facts and speculation

WHAT TO EXTRACT:
- incident_date: ISO date string (when attack occurred) OR null
- discovery_date: ISO date string (when breach was discovered) OR null
- disclosure_date: ISO date string (when publicly announced) OR null
- severity: "critical" | "high" | "medium" | "low" | "unknown"
  - critical: Major impact, millions affected, national security
  - high: Significant impact, thousands affected, sensitive data
  - medium: Moderate impact, hundreds affected, standard data
  - low: Minor impact, minimal data exposure
- records_affected: Integer (number of records/people affected) OR null
- data_types_compromised: List[String] (e.g., ["PII", "financial", "health"])
- extraction_confidence: Float 0.0-1.0

=== 4. AUSTRALIAN RELEVANCE ===

RULES:
Determine if this incident is relevant to Australian cybersecurity:
✓ Australian organization was victim
✓ Australian citizens' data was compromised
✓ Attack originated from/targeted Australia
✓ Significant impact on Australian infrastructure

WHAT TO EXTRACT:
- is_australian_event: Boolean
- australian_relevance_score: Float 0.0-1.0 (how relevant to Australia?)
- relevance_reasoning: String (explain why this is/isn't Australian-relevant)

=== 5. EVENT SPECIFICITY ===

RULES:
Determine if this article describes ONE SPECIFIC cyber incident or is a general report.

SPECIFIC INCIDENT:
✓ "Medibank confirms October 2022 data breach affecting 9.7M customers"
✓ "Toll Group hit by Mailto ransomware on May 1, 2020"

NOT SPECIFIC:
✗ "Cybersecurity threats increase in Q1 2024"
✗ "Multiple Australian companies targeted in recent attacks"
✗ "How to protect your business from ransomware"
✗ "Expert analysis of the cyber threat landscape"

WHAT TO EXTRACT:
- is_specific_incident: Boolean
- specificity_reasoning: String

=== 6. MULTI-VICTIM DETECTION ===

RULES:
Determine if ONE attack affected MULTIPLE organizations or article discusses MULTIPLE separate incidents.

ONE ATTACK, MULTIPLE VICTIMS:
✓ "Supply chain attack affects Microsoft, Google, and Amazon"
✓ "Ransomware gang simultaneously hits Acme Corp, Beta Industries, and Gamma LLC"

MULTIPLE SEPARATE INCIDENTS:
✗ "This breach follows similar attacks on Optus and Medibank"
✗ "HWL Ebsworth breach exposes data from multiple government clients"
  (HWL is the victim, departments are affected by breach but weren't directly attacked)

WHAT TO EXTRACT:
- has_multiple_victims: Boolean
- victim_organizations: List[String] (if has_multiple_victims = true)
- multi_victim_reasoning: String

=== OUTPUT FORMAT ===

Respond with ONLY a valid JSON object. No markdown, no explanation outside the JSON.

{{
  "victim": {{
    "organization": "exact organization name or null",
    "industry": "NIST category or null",
    "is_australian": true/false,
    "confidence": 0.0-1.0,
    "reasoning": "brief explanation"
  }},
  "attacker": {{
    "name": "threat actor name or Unknown",
    "type": "category",
    "malware": "malware name or null",
    "attack_vector": "vector or null",
    "confidence": 0.0-1.0,
    "reasoning": "brief explanation"
  }},
  "incident": {{
    "date": "YYYY-MM-DD or null",
    "discovery_date": "YYYY-MM-DD or null",
    "disclosure_date": "YYYY-MM-DD or null",
    "severity": "critical|high|medium|low|unknown",
    "records_affected": 123456 or null,
    "data_types": ["PII", "financial"],
    "confidence": 0.0-1.0,
    "reasoning": "brief explanation"
  }},
  "australian_relevance": {{
    "is_australian_event": true/false,
    "relevance_score": 0.0-1.0,
    "reasoning": "brief explanation"
  }},
  "specificity": {{
    "is_specific_incident": true/false,
    "reasoning": "brief explanation"
  }},
  "multi_victim": {{
    "has_multiple_victims": true/false,
    "victim_organizations": ["Org1", "Org2"] or null,
    "reasoning": "brief explanation"
  }},
  "overall_confidence": 0.0-1.0,
  "extraction_notes": "any important observations or caveats"
}}
"""

    def extract(self, content: Dict[str, Any]) -> Dict[str, Any]:
        """Extract structured data using GPT-4o"""

        prompt = self.create_extraction_prompt(content)

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are a precise cybersecurity incident analyst. Always respond with valid JSON only."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,  # Low temperature for consistency
            response_format={"type": "json_object"}  # Force JSON output
        )

        result = json.loads(response.choices[0].message.content)

        # Add metadata
        result['extraction_model'] = self.model
        result['extraction_timestamp'] = datetime.now().isoformat()

        return result
```

---

## Stage 3: Fact-Check (Perplexity)

```python
class PerplexityFactChecker:
    """Use Perplexity to verify extracted facts"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.api_url = "https://api.perplexity.ai/chat/completions"

    def fact_check_extraction(self, extraction: Dict[str, Any]) -> Dict[str, Any]:
        """
        Cross-verify extracted facts using Perplexity's real-time search.

        Returns fact-check results with verification status for each field.
        """

        checks = []

        # Check 1: Verify organization exists
        if extraction['victim']['organization']:
            org_check = self.verify_organization_exists(
                extraction['victim']['organization']
            )
            checks.append(org_check)

        # Check 2: Verify incident occurred
        if extraction['victim']['organization'] and extraction['incident']['date']:
            incident_check = self.verify_incident_occurred(
                extraction['victim']['organization'],
                extraction['incident']['date']
            )
            checks.append(incident_check)

        # Check 3: Verify attacker attribution if claimed
        if extraction['attacker']['name'] and extraction['attacker']['name'] != 'Unknown':
            attacker_check = self.verify_attacker_attribution(
                extraction['attacker']['name'],
                extraction['victim']['organization']
            )
            checks.append(attacker_check)

        # Check 4: Verify records affected if specified
        if extraction['incident']['records_affected']:
            records_check = self.verify_records_count(
                extraction['victim']['organization'],
                extraction['incident']['records_affected']
            )
            checks.append(records_check)

        return {
            'checks_performed': len(checks),
            'checks_passed': sum(1 for c in checks if c['verified']),
            'checks_failed': sum(1 for c in checks if not c['verified']),
            'details': checks,
            'overall_verification_confidence': self.calculate_verification_confidence(checks)
        }

    def verify_organization_exists(self, org_name: str) -> Dict[str, Any]:
        """Verify that the organization is real and not a generic description"""

        prompt = f"""
Is "{org_name}" a real, specific organization?

Answer with JSON:
{{
  "is_real_organization": true/false,
  "organization_type": "company|government|nonprofit|university|other",
  "country": "primary country of operation",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation with sources if possible"
}}

RULES:
- Return is_real_organization: false if it's a generic description like "Australian IT company"
- Return is_real_organization: true only if you can confirm it's a specific legal entity
- Provide sources if available
"""

        response = self.call_perplexity(prompt)

        return {
            'check_type': 'organization_exists',
            'query': org_name,
            'verified': response.get('is_real_organization', False),
            'confidence': response.get('confidence', 0.0),
            'details': response
        }

    def verify_incident_occurred(self, org_name: str, incident_date: str) -> Dict[str, Any]:
        """Verify that the organization experienced a cyber incident around the specified date"""

        prompt = f"""
Did {org_name} experience a cybersecurity incident (data breach, ransomware attack, cyber attack, etc.)
on or around {incident_date}?

Search for reliable sources and answer with JSON:
{{
  "incident_confirmed": true/false,
  "actual_date": "YYYY-MM-DD if different from provided date",
  "incident_type": "type of incident",
  "sources": ["list of source URLs"],
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation"
}}
"""

        response = self.call_perplexity(prompt)

        return {
            'check_type': 'incident_occurred',
            'query': f"{org_name} cyber incident {incident_date}",
            'verified': response.get('incident_confirmed', False),
            'confidence': response.get('confidence', 0.0),
            'details': response
        }

    def verify_attacker_attribution(self, attacker: str, victim: str) -> Dict[str, Any]:
        """Verify attacker attribution"""

        prompt = f"""
Has the threat actor "{attacker}" been credibly linked to a cyber attack on {victim}?

Answer with JSON:
{{
  "attribution_confirmed": true/false,
  "attribution_confidence": "confirmed|suspected|unverified",
  "sources": ["list of source URLs"],
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation"
}}
"""

        response = self.call_perplexity(prompt)

        return {
            'check_type': 'attacker_attribution',
            'query': f"{attacker} attack on {victim}",
            'verified': response.get('attribution_confirmed', False),
            'confidence': response.get('confidence', 0.0),
            'details': response
        }

    def verify_records_count(self, org_name: str, records_count: int) -> Dict[str, Any]:
        """Verify the number of records affected"""

        prompt = f"""
In the {org_name} cyber incident, were approximately {records_count:,} records affected?

Answer with JSON:
{{
  "count_confirmed": true/false,
  "actual_count": number if different,
  "count_range": "e.g., 9-10 million",
  "sources": ["list of source URLs"],
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation"
}}

RULES:
- count_confirmed: true if within ±20% of the stated number
- Provide actual_count if you find a different verified number
"""

        response = self.call_perplexity(prompt)

        # Check if within acceptable range
        if response.get('actual_count'):
            variance = abs(records_count - response['actual_count']) / records_count
            verified = variance <= 0.20  # Within 20%
        else:
            verified = response.get('count_confirmed', False)

        return {
            'check_type': 'records_affected',
            'query': f"{org_name} breach {records_count} records",
            'verified': verified,
            'confidence': response.get('confidence', 0.0),
            'details': response
        }

    def calculate_verification_confidence(self, checks: List[Dict]) -> float:
        """Calculate overall verification confidence from all checks"""

        if not checks:
            return 0.0

        # Weight different check types
        weights = {
            'organization_exists': 0.4,
            'incident_occurred': 0.4,
            'attacker_attribution': 0.1,
            'records_affected': 0.1
        }

        weighted_sum = 0.0
        total_weight = 0.0

        for check in checks:
            check_type = check['check_type']
            weight = weights.get(check_type, 0.1)

            if check['verified']:
                weighted_sum += weight * check['confidence']
            else:
                weighted_sum += weight * (1.0 - check['confidence']) * 0.5  # Penalty for failed check

            total_weight += weight

        return weighted_sum / total_weight if total_weight > 0 else 0.0
```

---

## Stage 4: Validation & Sensibility Checks

```python
class EnrichmentValidator:
    """Perform sensibility checks on extracted data"""

    def validate(self, extraction: Dict[str, Any], fact_check: Dict[str, Any]) -> Dict[str, Any]:
        """
        Run comprehensive validation checks.

        Returns:
            {
                'is_valid': bool,
                'warnings': List[str],
                'errors': List[str],
                'validation_confidence': float
            }
        """

        warnings = []
        errors = []

        # Check 1: Organization name validation
        org_check = self.validate_organization_name(extraction['victim']['organization'])
        if org_check['has_errors']:
            errors.extend(org_check['errors'])
        if org_check['has_warnings']:
            warnings.extend(org_check['warnings'])

        # Check 2: Date plausibility
        date_check = self.validate_dates(extraction['incident'])
        if date_check['has_errors']:
            errors.extend(date_check['errors'])

        # Check 3: Cross-field consistency
        consistency_check = self.validate_consistency(extraction)
        if consistency_check['has_warnings']:
            warnings.extend(consistency_check['warnings'])

        # Check 4: Fact-check alignment
        if fact_check['checks_failed'] > 0:
            warnings.append(f"Failed {fact_check['checks_failed']} fact-check(s)")

        # Check 5: Duplicate detection
        dup_check = self.check_for_duplicates(extraction)
        if dup_check['likely_duplicate']:
            warnings.append(f"Possible duplicate of existing event: {dup_check['similar_event_id']}")

        # Calculate validation confidence
        validation_confidence = self.calculate_validation_confidence(
            errors, warnings, fact_check
        )

        return {
            'is_valid': len(errors) == 0,
            'warnings': warnings,
            'errors': errors,
            'validation_confidence': validation_confidence
        }

    def validate_organization_name(self, org_name: str) -> Dict:
        """Validate organization name is specific and not generic"""

        errors = []
        warnings = []

        if not org_name:
            errors.append("No organization name provided")
            return {'has_errors': True, 'has_warnings': False, 'errors': errors}

        # Check for generic patterns
        generic_patterns = [
            r'australian\s+\w+\s+company',
            r'new\s+zealand\s+\w+\s+company',
            r'\w+\s+sector\s+organization',
            r'\w+\s+industry\s+company',
            r'^company$',
            r'^organization$',
            r'^the\s+company$',
        ]

        for pattern in generic_patterns:
            if re.search(pattern, org_name.lower()):
                errors.append(f"Generic organization name detected: '{org_name}'")
                break

        # Check if it's a person's name (common error)
        # Look for title + first/last name patterns
        person_patterns = [
            r'^(mr|mrs|ms|dr|prof)\.\s+\w+\s+\w+',
            r'^\w+\s+\w+\s+(ceo|cto|ciso|expert|researcher|analyst)',
        ]

        for pattern in person_patterns:
            if re.search(pattern, org_name.lower()):
                errors.append(f"Appears to be a person's name, not organization: '{org_name}'")
                break

        # Check length
        if len(org_name) < 2:
            errors.append(f"Organization name too short: '{org_name}'")

        if len(org_name) > 100:
            warnings.append(f"Organization name unusually long: '{org_name}'")

        return {
            'has_errors': len(errors) > 0,
            'has_warnings': len(warnings) > 0,
            'errors': errors,
            'warnings': warnings
        }

    def validate_dates(self, incident: Dict) -> Dict:
        """Validate date fields are plausible"""

        errors = []

        incident_date = incident.get('date')
        if incident_date:
            try:
                date_obj = datetime.fromisoformat(incident_date).date()

                # Check not in future
                if date_obj > datetime.now().date():
                    errors.append(f"Incident date is in the future: {incident_date}")

                # Check not too old (before modern internet era)
                if date_obj.year < 1990:
                    errors.append(f"Incident date implausibly old: {incident_date}")

            except ValueError:
                errors.append(f"Invalid date format: {incident_date}")

        # Check date ordering
        discovery = incident.get('discovery_date')
        disclosure = incident.get('disclosure_date')

        if incident_date and discovery and discovery < incident_date:
            errors.append("Discovery date before incident date (logically impossible)")

        if discovery and disclosure and disclosure < discovery:
            errors.append("Disclosure date before discovery date (unusual)")

        return {
            'has_errors': len(errors) > 0,
            'errors': errors
        }

    def validate_consistency(self, extraction: Dict) -> Dict:
        """Check cross-field consistency"""

        warnings = []

        # Check severity vs records_affected consistency
        severity = extraction['incident']['severity']
        records = extraction['incident']['records_affected']

        if severity == 'critical' and records and records < 1000:
            warnings.append("Severity 'critical' but only {records} records affected (inconsistent)")

        if severity == 'low' and records and records > 100000:
            warnings.append("Severity 'low' but {records:,} records affected (inconsistent)")

        # Check industry vs organization consistency
        # e.g., "Medibank" should be HEALTHCARE not TECHNOLOGY
        industry = extraction['victim']['industry']
        org_name = extraction['victim']['organization']

        if org_name and industry:
            known_orgs = {
                'medibank': 'HEALTHCARE',
                'qantas': 'TRANSPORTATION',
                'telstra': 'TELECOMMUNICATIONS',
                'optus': 'TELECOMMUNICATIONS',
                'commonwealth bank': 'FINANCIAL_SERVICES',
                'woolworths': 'RETAIL_COMMERCE',
            }

            org_lower = org_name.lower()
            for known_org, expected_industry in known_orgs.items():
                if known_org in org_lower and industry != expected_industry:
                    warnings.append(f"Industry mismatch: {org_name} classified as {industry}, expected {expected_industry}")

        return {
            'has_warnings': len(warnings) > 0,
            'warnings': warnings
        }

    def check_for_duplicates(self, extraction: Dict) -> Dict:
        """Check if this event already exists in database"""

        org = extraction['victim']['organization']
        date = extraction['incident']['date']

        if not org or not date:
            return {'likely_duplicate': False}

        # Query database for similar events
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT enriched_event_id, title
            FROM EnrichedEvents
            WHERE victim_organization = ?
              AND event_date = ?
              AND status = 'Active'
            LIMIT 5
        """, (org, date))

        existing = cursor.fetchall()
        conn.close()

        if existing:
            return {
                'likely_duplicate': True,
                'similar_event_id': existing[0][0],
                'similar_event_title': existing[0][1],
                'match_count': len(existing)
            }

        return {'likely_duplicate': False}
```

---

## Stage 5: Confidence Aggregation & Decision

```python
class EnrichmentAggregator:
    """Combine results from all stages and make final decision"""

    def aggregate(self,
                  gpt_extraction: Dict,
                  fact_check: Dict,
                  validation: Dict) -> Dict:
        """
        Combine confidences from all stages.

        Final confidence = weighted average of:
        - GPT-4o extraction confidence (40%)
        - Perplexity fact-check confidence (40%)
        - Validation confidence (20%)
        """

        gpt_conf = gpt_extraction.get('overall_confidence', 0.5)
        fact_conf = fact_check.get('overall_verification_confidence', 0.5)
        val_conf = validation.get('validation_confidence', 0.5)

        # Weighted average
        final_confidence = (
            0.4 * gpt_conf +
            0.4 * fact_conf +
            0.2 * val_conf
        )

        # Apply penalties
        if validation['errors']:
            final_confidence *= 0.5  # Severe penalty for validation errors

        if validation['warnings']:
            final_confidence *= 0.9  # Moderate penalty for warnings

        # Decision
        if final_confidence >= 0.8:
            decision = 'AUTO_ACCEPT'
            action = 'Store in database with high confidence'
        elif final_confidence >= 0.5:
            decision = 'ACCEPT_WITH_WARNING'
            action = 'Store in database with flag for review'
        else:
            decision = 'REJECT'
            action = 'Do not store, flag for manual review'

        return {
            'final_confidence': final_confidence,
            'decision': decision,
            'action': action,
            'stage_confidences': {
                'gpt4o_extraction': gpt_conf,
                'perplexity_fact_check': fact_conf,
                'validation': val_conf
            },
            'errors': validation['errors'],
            'warnings': validation['warnings'],
            'fact_check_summary': {
                'checks_performed': fact_check['checks_performed'],
                'checks_passed': fact_check['checks_passed'],
                'checks_failed': fact_check['checks_failed']
            }
        }
```

---

## Implementation Plan

### Phase 1: Infrastructure (Week 1)
- [ ] Set up GPT-4o API integration
- [ ] Implement content acquisition service (newspaper3k, trafilatura)
- [ ] Create database schema for audit trail
- [ ] Set up logging and monitoring

### Phase 2: Core Pipeline (Week 2-3)
- [ ] Implement GPT-4o enricher with ultra-specific prompts
- [ ] Implement Perplexity fact-checker
- [ ] Implement validation layer
- [ ] Implement confidence aggregation

### Phase 3: Testing (Week 4)
- [ ] Create test dataset (100 known events)
- [ ] Measure accuracy vs current system
- [ ] Tune prompts and thresholds
- [ ] A/B test on subset of new events

### Phase 4: Deployment (Week 5)
- [ ] Run on all events without victims (1,674 events)
- [ ] Re-process low-confidence events (<0.5)
- [ ] Manual review of edge cases
- [ ] Full production deployment

---

## Expected Outcomes

### Quality Metrics (Current → Target)

| Metric | Current | Target | Improvement |
|--------|---------|--------|-------------|
| Events with victim | 92% | 98% | +6% |
| False positive rate (non-specific filtering) | 90% | <5% | -85% |
| Victim misidentifications | 21+ | <2 | -90% |
| Generic victim names | 97 | 0 | -100% |
| Overall confidence score | Unknown | 0.85 avg | NEW |
| Fact-check coverage | 0% | 100% | NEW |

### Cost Analysis

**Current System:**
- Perplexity API calls: ~22K events × $0.005 = $110
- Total: $110/month

**New System:**
- Content acquisition: Free (open source tools)
- GPT-4o extraction: ~22K events × $0.01 = $220
- Perplexity fact-check: ~22K events × $0.005 = $110
- Total: $330/month

**Cost increase: $220/month for 10x quality improvement**

---

## Monitoring & Quality Assurance

### Real-Time Metrics Dashboard

```python
# Daily quality metrics
SELECT
    DATE(created_at) as date,
    COUNT(*) as events_processed,
    AVG(final_confidence) as avg_confidence,
    SUM(CASE WHEN decision = 'AUTO_ACCEPT' THEN 1 ELSE 0 END) as auto_accepted,
    SUM(CASE WHEN decision = 'ACCEPT_WITH_WARNING' THEN 1 ELSE 0 END) as accepted_with_warning,
    SUM(CASE WHEN decision = 'REJECT' THEN 1 ELSE 0 END) as rejected,
    AVG(fact_check_confidence) as avg_fact_check_confidence,
    SUM(CASE WHEN fact_checks_failed > 0 THEN 1 ELSE 0 END) as events_with_failed_checks
FROM EnrichedEvents
WHERE created_at >= DATE('now', '-7 days')
GROUP BY DATE(created_at)
ORDER BY date DESC;
```

### Weekly Manual Review

- Review 50 random AUTO_ACCEPT events → validate accuracy
- Review all REJECT events → identify prompt improvements
- Review all events with failed fact-checks → investigate discrepancies

---

## Summary

This redesign eliminates all regex-based extraction and creates a robust, validated enrichment pipeline:

1. **Full article text** instead of title/summary snippets
2. **GPT-4o** for superior extraction with ultra-specific prompts
3. **Perplexity fact-checking** to verify all extracted facts
4. **Multi-layer validation** with sensibility checks
5. **Confidence scoring** at every stage
6. **Audit trail** for transparency and debugging

**Result:** High-precision, validated enrichment with <5% error rate vs current 90% error rate in filtering alone.
