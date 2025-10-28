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

    def validate(self, extraction: Dict[str, Any], fact_check: Dict[str, Any]) -> Dict[str, Any]:
        """
        Run comprehensive validation checks.

        Args:
            extraction: GPT-4o extraction result
            fact_check: Perplexity fact-check result

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

        self.logger.info("Running validation checks...")

        # Check 1: Organization name validation
        org_check = self._validate_organization_name(
            extraction.get('victim', {}).get('organization')
        )
        if org_check['has_errors']:
            errors.extend(org_check['errors'])
        if org_check['has_warnings']:
            warnings.extend(org_check['warnings'])

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

        # Calculate validation confidence
        validation_confidence = self._calculate_validation_confidence(
            errors, warnings, fact_check
        )

        self.logger.info(f"Validation complete: {len(errors)} errors, {len(warnings)} warnings")

        return {
            'is_valid': len(errors) == 0,
            'warnings': warnings,
            'errors': errors,
            'validation_confidence': validation_confidence
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
