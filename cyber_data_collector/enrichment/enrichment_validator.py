"""
Enrichment Validator - Perform sensibility checks on extracted data.

This module validates extracted data for logical consistency, plausibility,
and common errors before storing in database.
"""

import logging
import re
import sqlite3
from typing import Dict, Any, List
from datetime import datetime, date


class EnrichmentValidator:
    """Perform comprehensive validation checks on extracted enrichment data"""

    def __init__(self, db_path: str):
        """Initialize validator with database path for duplicate checking"""
        self.db_path = db_path
        self.logger = logging.getLogger(__name__)
        self.event_title = None  # Will be set during validation
        self.event_url = None  # Will be set during validation

    def validate(self, extraction: Dict[str, Any], fact_check: Dict[str, Any],
                 event_title: str = None, event_url: str = None) -> Dict[str, Any]:
        """
        Run comprehensive validation checks.

        Args:
            extraction: GPT-4o extraction result
            fact_check: Perplexity fact-check result
            event_title: Original event title (for title-matching validation)
            event_url: Original event URL (for aggregate detection)

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

        # Store for use in other validation methods
        self.event_title = event_title
        self.event_url = event_url

        self.logger.info("Running validation checks...")

        # Check 1: Organization name validation
        org_check = self._validate_organization_name(
            extraction.get('victim', {}).get('organization')
        )
        if org_check['has_errors']:
            errors.extend(org_check['errors'])
        if org_check['has_warnings']:
            warnings.extend(org_check['warnings'])

        # Check 1b: Title matching validation (NEW)
        if event_title:
            title_check = self._validate_title_match(
                extraction.get('victim', {}).get('organization'),
                event_title,
                event_url
            )
            if title_check['has_warnings']:
                warnings.extend(title_check['warnings'])

        # Check 2: Date plausibility
        date_check = self._validate_dates(extraction.get('incident', {}))
        if date_check['has_errors']:
            errors.extend(date_check['errors'])

        # Check 3: Cross-field consistency
        consistency_check = self._validate_consistency(extraction)
        if consistency_check['has_warnings']:
            warnings.extend(consistency_check['warnings'])

        # Check 4: Fact-check alignment
        if fact_check['checks_failed'] > 0:
            failed_checks = [c for c in fact_check['details'] if not c['verified']]
            for check in failed_checks:
                warnings.append(f"Failed fact-check ({check['check_type']}): {check['query']}")

        # Check 5: Duplicate detection
        dup_check = self._check_for_duplicates(extraction)
        if dup_check['likely_duplicate']:
            warnings.append(
                f"Possible duplicate of event {dup_check['similar_event_id']}: "
                f"{dup_check['similar_event_title'][:60]}"
            )

        # Check 6: Specificity validation (NEW - catches GPT-4o misclassifications)
        spec_check = self._validate_specificity(extraction, event_title, event_url)
        if spec_check['has_warnings']:
            warnings.extend(spec_check['warnings'])

        # Use modified extraction if specificity overrides were applied
        final_extraction = spec_check['modified_extraction'] if spec_check['overrides'] else extraction

        # Calculate validation confidence
        validation_confidence = self._calculate_validation_confidence(
            errors, warnings, fact_check
        )

        self.logger.info(f"Validation complete: {len(errors)} errors, {len(warnings)} warnings")

        return {
            'is_valid': len(errors) == 0,
            'warnings': warnings,
            'errors': errors,
            'validation_confidence': validation_confidence,
            'modified_extraction': final_extraction,  # Include modified extraction with overrides
            'specificity_overrides': spec_check['overrides']  # Track which overrides were applied
        }

    def _validate_organization_name(self, org_name: str) -> Dict:
        """Validate organization name is specific and not generic"""

        errors = []
        warnings = []

        if not org_name:
            # No organization is acceptable (some events may not have identified victim)
            return {'has_errors': False, 'has_warnings': False, 'errors': [], 'warnings': []}

        # Check for generic patterns
        generic_patterns = [
            (r'australian\s+\w+\s+(company|firm|organization)', "Australian X company/firm"),
            (r'new\s+zealand\s+\w+\s+(company|firm)', "New Zealand X company"),
            (r'\w+\s+sector\s+organization', "X sector organization"),
            (r'\w+\s+industry\s+company', "X industry company"),
            (r'\w+\s+(companies|organizations|firms)$', "X companies/organizations (plural)"),
            (r'^(dutch|german|french|british|american|chinese)\s+\w+\s+companies', "Country X companies"),
            (r'^company$', "just 'company'"),
            (r'^organization$', "just 'organization'"),
            (r'^the\s+company$', "just 'the company'"),
            (r'^an?\s+\w+\s+company', "a/an X company"),
            (r'approximately\s+\d+\s+organizations', "approximately N organizations"),
            (r'multiple\s+(companies|organizations)', "multiple companies/organizations"),
        ]

        for pattern, description in generic_patterns:
            if re.search(pattern, org_name.lower()):
                errors.append(f"Generic organization name detected: '{org_name}' matches pattern '{description}'")
                break

        # Check if it's likely a person's name (common error)
        person_patterns = [
            (r'^(mr|mrs|ms|dr|prof)\.\s+\w+\s+\w+', "Title + Name"),
            (r'^\w+\s+\w+\s+(ceo|cto|ciso|expert|researcher|analyst)$', "Name + Title"),
            (r'^\w+\s+smith$', "First Last pattern with common surname"),
            (r'^\w+\s+jones$', "First Last pattern with common surname"),
        ]

        for pattern, description in person_patterns:
            if re.search(pattern, org_name.lower()):
                errors.append(f"Appears to be a person's name, not organization: '{org_name}' ({description})")
                break

        # Check length
        if len(org_name) < 2:
            errors.append(f"Organization name too short: '{org_name}'")

        if len(org_name) > 150:
            warnings.append(f"Organization name unusually long ({len(org_name)} chars): '{org_name[:50]}...'")

        # Check for suspicious patterns
        if org_name.lower() in ['unknown', 'n/a', 'null', 'none', 'not specified']:
            errors.append(f"Invalid organization name: '{org_name}'")

        return {
            'has_errors': len(errors) > 0,
            'has_warnings': len(warnings) > 0,
            'errors': errors,
            'warnings': warnings
        }

    def _validate_title_match(self, org_name: str, title: str, url: str = None) -> Dict:
        """
        Validate that the extracted victim organization appears in the article title.

        This helps catch cases where:
        - Aggregate blog posts mention multiple incidents
        - URL/content mismatch (title about A, content about B)
        - GPT-4o extracted a contextual mention instead of primary victim
        """

        warnings = []

        if not org_name or not title:
            return {'has_warnings': False, 'warnings': []}

        # Normalize for comparison
        org_lower = org_name.lower()
        title_lower = title.lower()

        # Check for exact match or partial match
        # Split org name into words and check if key parts appear in title
        org_words = org_lower.split()

        # For organizations like "Singtel Optus Pty Limited", check for "optus"
        # For "HWL Ebsworth", check for both "hwl" and "ebsworth"
        key_words = [w for w in org_words if len(w) > 3 and w not in [
            'limited', 'ltd', 'inc', 'corp', 'corporation', 'company', 'pty',
            'australia', 'australian', 'group', 'holdings'
        ]]

        # If no key words, use all non-stopwords
        if not key_words:
            key_words = [w for w in org_words if len(w) > 2]

        # Check if ANY key word appears in title
        found_match = False
        for word in key_words:
            if word in title_lower:
                found_match = True
                break

        # Also check full org name (for short names like "Qantas", "Optus")
        if org_lower in title_lower:
            found_match = True

        # If no match found, this is suspicious
        if not found_match:
            # Check if this is likely an aggregate URL
            is_aggregate_url = False
            if url:
                aggregate_patterns = [
                    'blog/', 'weekly', 'monthly', 'roundup', 'digest', 'update',
                    'news-feed', 'bulletin', 'newsletter', 'recap'
                ]
                url_lower = url.lower()
                is_aggregate_url = any(pattern in url_lower for pattern in aggregate_patterns)

            warning_msg = (
                f"Victim '{org_name}' not found in title '{title[:80]}...'. "
                f"This may indicate: (1) aggregate article extracting wrong incident, "
                f"(2) URL/content mismatch, or (3) contextual mention instead of primary victim."
            )

            if is_aggregate_url:
                warning_msg += f" URL appears to be aggregate content: {url}"

            warnings.append(warning_msg)

        return {
            'has_warnings': len(warnings) > 0,
            'warnings': warnings
        }

    def _validate_dates(self, incident: Dict) -> Dict:
        """Validate date fields are plausible"""

        errors = []

        incident_date = incident.get('date')
        if incident_date:
            try:
                date_obj = datetime.fromisoformat(incident_date).date()

                # Check not in future
                if date_obj > datetime.now().date():
                    errors.append(f"Incident date is in the future: {incident_date}")

                # Check not too old (before modern cybersecurity era)
                if date_obj.year < 1990:
                    errors.append(f"Incident date implausibly old (before 1990): {incident_date}")

            except (ValueError, TypeError) as e:
                errors.append(f"Invalid incident date format: {incident_date} ({e})")

        # Check date ordering
        discovery = incident.get('discovery_date')
        disclosure = incident.get('disclosure_date')

        if incident_date and discovery:
            try:
                inc_date_obj = datetime.fromisoformat(incident_date).date()
                disc_date_obj = datetime.fromisoformat(discovery).date()

                if disc_date_obj < inc_date_obj:
                    errors.append(
                        f"Discovery date ({discovery}) before incident date ({incident_date}) - logically impossible"
                    )
            except (ValueError, TypeError):
                pass  # Already caught above

        if discovery and disclosure:
            try:
                disc_date_obj = datetime.fromisoformat(discovery).date()
                discl_date_obj = datetime.fromisoformat(disclosure).date()

                if discl_date_obj < disc_date_obj:
                    errors.append(
                        f"Disclosure date ({disclosure}) before discovery date ({discovery}) - unusual"
                    )
            except (ValueError, TypeError):
                pass

        return {
            'has_errors': len(errors) > 0,
            'errors': errors
        }

    def _validate_consistency(self, extraction: Dict) -> Dict:
        """Check cross-field consistency"""

        warnings = []

        # Extract fields
        severity = extraction.get('incident', {}).get('severity')
        records = extraction.get('incident', {}).get('records_affected')
        industry = extraction.get('victim', {}).get('industry')
        org_name = extraction.get('victim', {}).get('organization')

        # Check severity vs records_affected consistency
        if severity and records:
            if severity == 'critical' and records < 1000:
                warnings.append(
                    f"Severity '{severity}' but only {records:,} records affected - may be inconsistent"
                )

            if severity == 'low' and records > 100000:
                warnings.append(
                    f"Severity '{severity}' but {records:,} records affected - may be inconsistent"
                )

        # Check industry vs organization consistency (for known organizations)
        if org_name and industry:
            known_orgs = {
                'medibank': 'HEALTHCARE',
                'qantas': 'TRANSPORTATION',
                'virgin australia': 'TRANSPORTATION',
                'telstra': 'TELECOMMUNICATIONS',
                'optus': 'TELECOMMUNICATIONS',
                'tpg': 'TELECOMMUNICATIONS',
                'iinet': 'TELECOMMUNICATIONS',
                'commonwealth bank': 'FINANCIAL_SERVICES',
                'westpac': 'FINANCIAL_SERVICES',
                'anz': 'FINANCIAL_SERVICES',
                'nab': 'FINANCIAL_SERVICES',
                'woolworths': 'RETAIL_COMMERCE',
                'coles': 'RETAIL_COMMERCE',
                'bhp': 'MINING',
                'rio tinto': 'MINING',
                'fortescue': 'MINING',
            }

            org_lower = org_name.lower()
            for known_org, expected_industry in known_orgs.items():
                if known_org in org_lower and industry != expected_industry:
                    warnings.append(
                        f"Industry classification mismatch: '{org_name}' classified as {industry}, "
                        f"expected {expected_industry}"
                    )
                    break

        return {
            'has_warnings': len(warnings) > 0,
            'warnings': warnings
        }

    def _check_for_duplicates(self, extraction: Dict) -> Dict:
        """Check if this event already exists in database"""

        org = extraction.get('victim', {}).get('organization')
        event_date = extraction.get('incident', {}).get('date')

        if not org or not event_date:
            return {'likely_duplicate': False}

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # Query for events with same victim and date
            cursor.execute("""
                SELECT enriched_event_id, title
                FROM EnrichedEvents
                WHERE victim_organization = ?
                  AND event_date = ?
                  AND status = 'Active'
                LIMIT 5
            """, (org, event_date))

            existing = cursor.fetchall()
            conn.close()

            if existing:
                return {
                    'likely_duplicate': True,
                    'similar_event_id': existing[0][0],
                    'similar_event_title': existing[0][1],
                    'match_count': len(existing)
                }

        except Exception as e:
            self.logger.warning(f"Duplicate check failed: {e}")

        return {'likely_duplicate': False}

    def _validate_specificity(self, extraction: Dict, event_title: str = None, event_url: str = None) -> Dict:
        """
        Validate is_specific_incident classification using heuristics.

        Catches GPT-4o mistakes by checking if the classification makes sense
        given other extracted signals.

        Returns:
            {
                'has_warnings': bool,
                'warnings': List[str],
                'overrides': List[Dict],
                'modified_extraction': Dict (with overrides applied)
            }
        """

        warnings = []
        overrides = []

        is_specific = extraction.get('specificity', {}).get('is_specific_incident')
        victim = extraction.get('victim', {}).get('organization')
        australian_rel = extraction.get('australian_relevance', {}).get('relevance_score', 0)
        records_affected = extraction.get('incident', {}).get('records_affected')
        incident_date = extraction.get('incident', {}).get('date')
        attack_type = extraction.get('attacker', {}).get('attack_type', '')

        # RULE 1: Override False -> True if strong incident indicators present
        if is_specific == False and victim and australian_rel > 0.7:
            # Check for concrete incident details
            has_concrete_details = (
                records_affected and records_affected > 0
            ) or (
                incident_date is not None
            ) or (
                attack_type and attack_type.lower() not in ['cyber incident', 'unknown', 'not specified']
            )

            if has_concrete_details:
                overrides.append({
                    'original': False,
                    'override': True,
                    'reason': f"Event has concrete incident details (victim: {victim}, australian_relevance: {australian_rel:.2f}, details present)"
                })
                warnings.append(
                    f"SPECIFICITY OVERRIDE: GPT-4o marked as non-specific, but event has victim + concrete details. "
                    f"Overriding to is_specific=True for Australian event about {victim}"
                )

        # RULE 2: Override False -> True if title contains incident keywords
        if is_specific == False and event_title and victim:
            title_lower = event_title.lower()
            # EXPANDED keywords to catch more legitimate incident reports
            incident_keywords = [
                'breach', 'attack', 'hack', 'ransomware', 'incident', 'compromised', 'exposed', 'hit by',
                'flags', 'reports', 'confirms', 'discloses', 'reveals', 'data leak', 'cyberattack'
            ]

            has_incident_keyword = any(kw in title_lower for kw in incident_keywords)
            victim_in_title = victim.lower() in title_lower or any(word.lower() in title_lower for word in victim.split() if len(word) > 3)

            # Check if it's NOT an aggregate URL
            is_aggregate = False
            if event_url:
                aggregate_patterns = ['blog/', 'weekly', 'monthly', 'roundup', 'digest', 'update']
                is_aggregate = any(pattern in event_url.lower() for pattern in aggregate_patterns)

            # LOWERED threshold from 0.5 to 0.3 to catch more Australian events
            if has_incident_keyword and victim_in_title and not is_aggregate and australian_rel > 0.3:
                overrides.append({
                    'original': False,
                    'override': True,
                    'reason': f"Title contains incident keywords and victim name, high Australian relevance"
                })
                warnings.append(
                    f"SPECIFICITY OVERRIDE: Title contains incident keywords + victim name. "
                    f"Overriding to is_specific=True"
                )

        # RULE 3: Override True -> False if clearly educational/generic
        if is_specific == True and event_title:
            title_lower = event_title.lower()
            educational_prefixes = ['how to', 'guide to', 'best practices', 'tips for', '5 ways', '10 steps']

            is_educational = any(title_lower.startswith(prefix) for prefix in educational_prefixes)

            if is_educational and not victim:
                overrides.append({
                    'original': True,
                    'override': False,
                    'reason': f"Title suggests educational content, no victim identified"
                })
                warnings.append(
                    f"SPECIFICITY OVERRIDE: Title appears educational without specific victim. "
                    f"Overriding to is_specific=False"
                )

        # Apply overrides to extraction if any
        modified_extraction = extraction.copy()
        if overrides:
            for override in overrides:
                if 'specificity' not in modified_extraction:
                    modified_extraction['specificity'] = {}
                modified_extraction['specificity']['is_specific_incident'] = override['override']

                # Update reasoning to show override was applied
                current_reasoning = modified_extraction['specificity'].get('specificity_reasoning', '')
                modified_extraction['specificity']['specificity_reasoning'] = (
                    f"{current_reasoning} [VALIDATOR OVERRIDE: {override['reason']}]"
                )

        return {
            'has_warnings': len(warnings) > 0,
            'warnings': warnings,
            'overrides': overrides,
            'modified_extraction': modified_extraction
        }

    def _calculate_validation_confidence(self, errors: List[str], warnings: List[str],
                                         fact_check: Dict) -> float:
        """Calculate overall validation confidence"""

        # Start with perfect confidence
        confidence = 1.0

        # Deduct for errors (severe penalty)
        confidence -= len(errors) * 0.3

        # Deduct for warnings (moderate penalty)
        confidence -= len(warnings) * 0.1

        # Incorporate fact-check results
        if fact_check['checks_performed'] > 0:
            fact_check_score = fact_check['checks_passed'] / fact_check['checks_performed']
            # Average with fact-check score
            confidence = (confidence + fact_check_score) / 2

        # Clamp to [0, 1]
        return max(0.0, min(1.0, confidence))
