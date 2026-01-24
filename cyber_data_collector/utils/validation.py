"""
Data validation utilities for cyber event enrichment.

Provides common-sense validation rules for extracted data fields.
"""

from typing import Optional, Any
import logging
import json

logger = logging.getLogger(__name__)

# Major international companies that may legitimately have >20M records affected
# These are global tech/financial giants with billions of users worldwide
MAJOR_INTERNATIONAL_ORGANIZATIONS = {
    'facebook', 'meta', 'instagram', 'whatsapp',
    'google', 'alphabet', 'youtube', 'gmail', 'chrome',
    'microsoft', 'linkedin', 'windows', 'azure',
    'amazon', 'aws', 'amazon web services',
    'apple', 'icloud',
    'twitter', 'x corp',
    'tiktok', 'bytedance',
    'yahoo', 'verizon media',
    'alibaba', 'alipay',
    'tencent', 'wechat', 'qq',
    'uber', 'lyft',
    'airbnb', 'booking.com',
    'paypal', 'venmo',
    'visa', 'mastercard',
    'equifax', 'experian', 'transunion',
    'marriott', 'starwood', 'hilton',
    'adobe', 'salesforce', 'oracle',
    'ebay', 'etsy', 'shopify',
    'spotify', 'netflix', 'disney',
    'sony', 'playstation', 'xbox',
    'samsung', 'huawei', 'xiaomi',
    # Additional verified international organizations
    'ticketmaster', 'live nation',
    'moneygram', 'western union',
    'canva',
    'gravatar', 'wordpress', 'automattic',
    'nitro pdf', 'nitro',
    'mcdonald\'s', 'mcdonalds',
    'internet archive', 'archive.org',
    'dji'
}

# Major Australian organizations with large customer bases (up to 30M records)
# These are major banks, telcos, healthcare providers that serve millions of Australians
MAJOR_AUSTRALIAN_ORGANIZATIONS = {
    # Major banks and financial institutions
    'commonwealth bank', 'cba', 'commbank',
    'westpac', 'nab', 'national australia bank',
    'anz', 'australia and new zealand banking',
    'suncorp', 'bendigo bank', 'macquarie',
    'amp', 'qbe', 'iag',
    # Major telcos
    'telstra', 'optus', 'vodafone', 'tpg',
    # Major healthcare
    'medisecure', 'medibank', 'bupa', 'nib', 'hcf',
    # Major retailers
    'woolworths', 'coles', 'bunnings', 'jb hi-fi', 'harvey norman',
    # Major utilities
    'agl', 'origin energy', 'energyaustralia',
    # Major insurers
    'allianz', 'zurich', 'axa',
    # Other large organizations
    'qantas', 'virgin australia', 'australia post',
    'latitude', 'latitude financial', 'genworth'
}

# Australian government organization identifiers
# These may legitimately have up to 30M records affected (due to Australia's population ~26M)
AUSTRALIAN_GOVERNMENT_IDENTIFIERS = {
    'government', 'govt',
    'department of', 'dept of',
    'ministry of',
    'home affairs', 'foreign affairs', 'defence', 'defense',
    'ato', 'australian taxation office',
    'medicare', 'centrelink',
    'services australia',
    'treasury',
    'infrastructure', 'transport',
    'health', 'education',
    'attorney-general',
    'parliament',
    'commission', 'authority', 'agency',
    'nsw government', 'vic government', 'qld government', 'sa government',
    'wa government', 'tas government', 'nt government', 'act government',
    'state government', 'federal government',
    'commonwealth'
}


def validate_records_affected(value: Optional[int], event_title: str = "") -> Optional[int]:
    """
    Validate and sanitize records_affected values using common-sense rules.

    Args:
        value: The records_affected value to validate
        event_title: Event title for logging context

    Returns:
        Validated/corrected value or None if invalid

    Rules:
        - Maximum realistic value: 1 billion (1,000,000,000)
        - Minimum realistic value: 50 (smaller values likely indicate parsing errors)
        - Values > 1 billion are rejected (too unrealistic to trust)
        - Values < 50 are rejected (likely missed "thousand" or "million" units)
        - Negative values are rejected
    """
    if not isinstance(event_title, str):
        raise TypeError("event_title must be a string")

    if value is None:
        return None

    # Convert to int if needed
    try:
        value = int(value)
    except (ValueError, TypeError):
        logger.warning(f"Invalid records_affected value '{value}' for event: {event_title}")
        return None

    # Reject negative values
    if value < 0:
        logger.warning(f"Negative records_affected ({value}) rejected for event: {event_title}")
        return None

    # Reject zero (use None instead)
    if value == 0:
        return None

    # Reject suspiciously low values (likely parsing error where units were missed)
    MIN_REALISTIC_RECORDS = 50
    if value < MIN_REALISTIC_RECORDS:
        logger.warning(
            f"Suspiciously low records_affected ({value}) rejected for event: {event_title}. "
            f"Likely parsing error (missed 'thousand' or 'million' units). "
            f"Minimum realistic value is {MIN_REALISTIC_RECORDS}."
        )
        return None

    # Check organization type for high record counts
    title_lower = event_title.lower()

    # Check if it's a major international organization (allows up to 1 billion)
    is_international = any(org in title_lower for org in MAJOR_INTERNATIONAL_ORGANIZATIONS)

    # Check if it's a major Australian organization (allows up to 30 million)
    is_major_au = any(org in title_lower for org in MAJOR_AUSTRALIAN_ORGANIZATIONS)

    # Check if it's an Australian government organization (allows up to 30 million)
    is_gov = any(identifier in title_lower for identifier in AUSTRALIAN_GOVERNMENT_IDENTIFIERS)

    # Apply tiered limits based on organization type
    SMALL_ORG_MAX = 20_000_000
    LARGE_AU_ORG_MAX = 30_000_000

    if value > SMALL_ORG_MAX and not (is_international or is_major_au or is_gov):
        # Small/unknown organization exceeds 20M cap
        logger.warning(
            f"High records_affected ({value:,}) rejected for small/unknown organization. "
            f"Event: {event_title}. "
            f"Record counts > {SMALL_ORG_MAX:,} only accepted for major organizations. "
            f"Local/regional organizations capped at {SMALL_ORG_MAX:,}."
        )
        return None

    if value > LARGE_AU_ORG_MAX and (is_major_au or is_gov) and not is_international:
        # Major Australian organization exceeds 30M cap
        logger.warning(
            f"High records_affected ({value:,}) rejected for major Australian organization. "
            f"Event: {event_title}. "
            f"Major Australian organizations capped at {LARGE_AU_ORG_MAX:,} "
            f"(based on Australia's population of ~26M)."
        )
        return None

    # Reject values over 1 billion (no single breach can realistically affect more)
    MAX_RECORDS = 1_000_000_000
    if value > MAX_RECORDS:
        logger.warning(
            f"Unrealistic records_affected ({value:,}) rejected (exceeds maximum of {MAX_RECORDS:,}) "
            f"for event: {event_title}"
        )
        return None

    return value


def validate_and_correct_enrichment_data(enrichment_data: dict, event_title: str = "") -> dict:
    """
    Validate and correct all fields in enrichment data.

    Args:
        enrichment_data: Dictionary containing enrichment fields
        event_title: Event title for logging context

    Returns:
        Corrected enrichment data dictionary
    """
    if not isinstance(enrichment_data, dict):
        raise TypeError("enrichment_data must be a dictionary")
    if not isinstance(event_title, str):
        raise TypeError("event_title must be a string")

    corrected = enrichment_data.copy()

    # Validate records_affected
    if 'records_affected' in corrected:
        corrected['records_affected'] = validate_records_affected(
            corrected.get('records_affected'),
            event_title
        )

    return corrected


def safe_json_dumps(value: Any, context: str, **kwargs: Any) -> str:
    """
    Safely serialize data to JSON with type validation.

    Args:
        value: Data to serialize.
        context: Description used in error messages.
        **kwargs: Passed through to json.dumps.

    Returns:
        JSON string.
    """
    if not isinstance(context, str):
        raise TypeError("context must be a string")

    try:
        return json.dumps(value, **kwargs)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{context} contains non-JSON-serializable data: {exc}") from exc
