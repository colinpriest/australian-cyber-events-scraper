"""
Data validation utilities for cyber event enrichment.

Provides common-sense validation rules for extracted data fields,
type coercion helpers for database values, and validation for LLM outputs.
"""
from __future__ import annotations

import logging
import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Type coercion helpers – safely convert untrusted values to expected types.
# These should be used at EVERY system boundary: database reads, LLM outputs,
# API responses, and user inputs.
# ---------------------------------------------------------------------------


def safe_int(value: Any, default: Optional[int] = None, field_name: str = "") -> Optional[int]:
    """Safely convert a value to int, returning *default* on failure.

    Handles: None, int, float (truncated), str (parsed), bool, and garbage.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        # bool is subclass of int in Python – treat True/False as 1/0
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        # Handle strings like "1,234" or "1 234"
        cleaned = str(value).replace(",", "").replace(" ", "").strip()
        if not cleaned:
            return default
        return int(float(cleaned))  # int(float(...)) handles "1.0"
    except (ValueError, TypeError, OverflowError):
        if field_name:
            logger.warning("Cannot convert %r to int for field '%s', using default %s", value, field_name, default)
        return default


def safe_float(value: Any, default: Optional[float] = None, field_name: str = "") -> Optional[float]:
    """Safely convert a value to float, returning *default* on failure."""
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        cleaned = str(value).replace(",", "").strip()
        if not cleaned:
            return default
        return float(cleaned)
    except (ValueError, TypeError, OverflowError):
        if field_name:
            logger.warning("Cannot convert %r to float for field '%s', using default %s", value, field_name, default)
        return default


def safe_str(value: Any, default: Optional[str] = None, max_length: int = 0) -> Optional[str]:
    """Safely convert a value to str, returning *default* on failure.

    If *max_length* > 0 the result is truncated.
    """
    if value is None:
        return default
    result = str(value).strip()
    if not result:
        return default
    if max_length > 0:
        result = result[:max_length]
    return result


def safe_bool(value: Any, default: bool = False) -> bool:
    """Safely convert a value to bool."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return default


def safe_date(value: Any, field_name: str = "") -> Optional[date]:
    """Safely parse a value to a ``datetime.date``, returning None on failure.

    Accepts: date, datetime, ISO-format strings, None.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            # Handle ISO format with optional timezone
            cleaned = cleaned.replace("Z", "+00:00")
            return datetime.fromisoformat(cleaned).date()
        except (ValueError, TypeError):
            if field_name:
                logger.warning("Cannot parse %r as date for field '%s'", value, field_name)
            return None
    if field_name:
        logger.warning("Unexpected type %s for date field '%s'", type(value).__name__, field_name)
    return None


def safe_datetime(value: Any, field_name: str = "") -> Optional[datetime]:
    """Safely parse a value to a ``datetime``, returning None on failure."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            cleaned = cleaned.replace("Z", "+00:00")
            return datetime.fromisoformat(cleaned)
        except (ValueError, TypeError):
            if field_name:
                logger.warning("Cannot parse %r as datetime for field '%s'", value, field_name)
            return None
    if field_name:
        logger.warning("Unexpected type %s for datetime field '%s'", type(value).__name__, field_name)
    return None


# ---------------------------------------------------------------------------
# Database row validation – validate and coerce a dict of database values.
# ---------------------------------------------------------------------------

# Expected types for EnrichedEvents columns
ENRICHED_EVENT_SCHEMA: Dict[str, str] = {
    "enriched_event_id": "str",
    "title": "str",
    "description": "str",
    "summary": "str",
    "event_type": "str",
    "severity": "str",
    "event_date": "date",
    "records_affected": "int",
    "is_australian_event": "bool",
    "is_specific_event": "bool",
    "confidence_score": "float",
    "australian_relevance_score": "float",
}


def validate_db_row(row: Dict[str, Any], schema: Dict[str, str], context: str = "") -> Dict[str, Any]:
    """Validate and coerce a database row dict against an expected schema.

    Args:
        row: Dictionary from ``dict(cursor.fetchone())``.
        schema: Mapping of column names to expected type names
                 (``"str"``, ``"int"``, ``"float"``, ``"bool"``, ``"date"``, ``"datetime"``).
        context: Description for log messages.

    Returns:
        A new dict with coerced values.  Unknown columns are passed through.
    """
    coerced: Dict[str, Any] = {}
    prefix = f"[{context}] " if context else ""

    for key, value in row.items():
        expected = schema.get(key)
        if expected is None:
            # Column not in schema – pass through
            coerced[key] = value
            continue

        if expected == "str":
            coerced[key] = safe_str(value)
        elif expected == "int":
            coerced[key] = safe_int(value, field_name=f"{prefix}{key}")
        elif expected == "float":
            coerced[key] = safe_float(value, field_name=f"{prefix}{key}")
        elif expected == "bool":
            coerced[key] = safe_bool(value)
        elif expected == "date":
            coerced[key] = safe_date(value, field_name=f"{prefix}{key}")
        elif expected == "datetime":
            coerced[key] = safe_datetime(value, field_name=f"{prefix}{key}")
        else:
            coerced[key] = value

    return coerced


def validate_enriched_event_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and coerce an EnrichedEvents database row."""
    return validate_db_row(row, ENRICHED_EVENT_SCHEMA, context="EnrichedEvent")


# ---------------------------------------------------------------------------
# Enrichment data validation – validate data before writing to the database.
# ---------------------------------------------------------------------------


def validate_enrichment_data_for_storage(
    data: Dict[str, Any],
    event_title: str = "",
    org_name: Optional[str] = None,
    perplexity_api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate and coerce enrichment data before storing in the database.

    Ensures all values have the correct types for SQLite insertion.
    When *perplexity_api_key* is provided, uncertain records_affected
    rejections are verified via a Perplexity LLM call before being discarded.
    """
    validated = data.copy()

    # Ensure title is a non-empty string
    validated["title"] = safe_str(validated.get("title"), default="Untitled Event", max_length=500)

    # Ensure description and summary are strings or None
    validated["description"] = safe_str(validated.get("description"))
    validated["summary"] = safe_str(validated.get("summary"))

    # Ensure event_type and severity are strings or None
    validated["event_type"] = safe_str(validated.get("event_type"), max_length=50)
    validated["severity"] = safe_str(validated.get("severity"), max_length=20)

    # Validate event_date – must be ISO format string or None
    raw_date = validated.get("event_date")
    if raw_date is not None:
        parsed = safe_date(raw_date, field_name="event_date")
        validated["event_date"] = parsed.isoformat() if parsed else None
    else:
        validated["event_date"] = None

    # Validate records_affected – must be int or None
    validated["records_affected"] = safe_int(
        validated.get("records_affected"), field_name="records_affected"
    )
    # Apply domain-level validation with optional LLM fallback
    records_value, is_cyber_incident = llm_validate_records_affected(
        validated["records_affected"], event_title,
        org_name=org_name,
        description=validated.get("description"),
        perplexity_api_key=perplexity_api_key,
    )
    validated["records_affected"] = records_value
    # If Perplexity explicitly confirmed this is not a cyber incident, reject the event
    if not is_cyber_incident:
        logger.warning(
            "Perplexity confirmed '%s' is not a cyber incident — marking event as rejected.",
            event_title[:80],
        )
        validated["is_australian_event"] = False
        validated["is_specific_event"] = False

    # Ensure boolean fields
    validated["is_australian_event"] = safe_bool(validated.get("is_australian_event"), default=False)
    validated["is_specific_event"] = safe_bool(validated.get("is_specific_event"), default=False)

    # Ensure confidence_score is a float in [0, 1]
    score = safe_float(validated.get("confidence_score"), default=0.0, field_name="confidence_score")
    if score is not None:
        score = max(0.0, min(1.0, score))
    validated["confidence_score"] = score or 0.0

    # Ensure australian_relevance_score is a float
    aus_score = safe_float(validated.get("australian_relevance_score"), default=0.0, field_name="australian_relevance_score")
    if aus_score is not None:
        aus_score = max(0.0, min(1.0, aus_score))
    validated["australian_relevance_score"] = aus_score or 0.0

    # Ensure status is a valid string
    validated["status"] = safe_str(validated.get("status"), default="Active")

    return validated

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


def llm_validate_records_affected(
    value: Optional[int],
    event_title: str = "",
    org_name: Optional[str] = None,
    description: Optional[str] = None,
    perplexity_api_key: Optional[str] = None,
) -> Tuple[Optional[int], bool]:
    """Validate records_affected with an LLM fallback via Perplexity.

    First runs the rule-based ``validate_records_affected``.  If the value is
    rejected **and** a Perplexity API key is available, a targeted prompt asks
    Perplexity whether the number is plausible for the organisation.

    When Perplexity confirms the org is large, it is dynamically added to the
    module-level ``MAJOR_AUSTRALIAN_ORGANIZATIONS`` or
    ``MAJOR_INTERNATIONAL_ORGANIZATIONS`` sets so that future rule-based checks
    pass without an API call.

    If Perplexity returns a ``corrected_value`` (e.g. "20" was actually
    "20,000"), that corrected value is returned instead.

    Graceful degradation: no API key → identical behaviour to
    ``validate_records_affected``.

    Returns:
        A ``(records_affected, is_cyber_incident)`` tuple.  ``is_cyber_incident``
        is ``False`` only when Perplexity explicitly determines the event is not
        a cyber security incident; it defaults to ``True`` in all other cases so
        that the caller does not reject events on uncertainty alone.
    """
    # 1. Run rule-based validation first
    rule_result = validate_records_affected(value, event_title)

    if rule_result is not None:
        # Rule-based validation accepted the value – nothing more to do
        return rule_result, True

    # value was rejected.  If we have no LLM key, return the rejection.
    if value is None or not perplexity_api_key:
        return None, True

    # 2. Call Perplexity to verify
    try:
        import requests as _requests

        org_display = org_name or event_title or "unknown"
        context_text = ""
        if description:
            context_text = description[:500]

        prompt = (
            "You are verifying a data point from a cyber security breach report.\n\n"
            f'Event: "{event_title}"\n'
            f'Organization: "{org_display}"\n'
            f"Claimed records affected: {value:,}\n"
        )
        if context_text:
            prompt += f'Context: "{context_text}"\n'
        prompt += (
            "\nQuestions:\n"
            "1. Is this event genuinely about a CYBER SECURITY incident "
            "(data breach, ransomware, hacking, phishing, etc.)? "
            "Or is it about physical security, airline safety, border security, "
            "workplace safety, or some other non-cyber topic? "
            "The word 'security' alone does NOT make it a cyber incident.\n"
            f'2. Is "{org_display}" a major organization? Estimate their customer/user base size.\n'
            f"3. Is {value:,} records affected plausible for this organization and this incident?\n"
            "4. Could this number be a parsing error (e.g. '20' when the article said "
            "'20,000' or '20 million')?\n\n"
            "Respond with JSON only:\n"
            "{\n"
            '  "is_cyber_incident": true or false,\n'
            '  "is_plausible": true or false,\n'
            '  "corrected_value": null or integer (if the number was likely misread),\n'
            '  "org_size_category": "major_international" | "major_australian" | "small_regional" | "unknown",\n'
            '  "estimated_customer_base": integer or null,\n'
            '  "reasoning": "brief explanation"\n'
            "}\n"
        )

        payload = {
            "model": "sonar-pro",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 500,
            "temperature": 0.1,
        }

        resp = _requests.post(
            "https://api.perplexity.ai/chat/completions",
            headers={
                "Authorization": f"Bearer {perplexity_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )

        if resp.status_code != 200:
            logger.error(
                "Perplexity records_affected validation failed (HTTP %s): %s",
                resp.status_code, resp.text[:200],
            )
            return None, True

        # Track token usage
        from cyber_data_collector.utils.token_tracker import tracker
        usage = resp.json().get("usage", {})
        if usage:
            tracker.record(
                "sonar-pro",
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
                context="records_affected_validation",
            )

        content = resp.json()["choices"][0]["message"]["content"]
        # Strip markdown fences if present
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]

        decoder = json.JSONDecoder()
        result, _ = decoder.raw_decode(content.strip())

        is_cyber = result.get("is_cyber_incident", True)  # default True for backwards compat
        is_plausible = result.get("is_plausible", False)
        corrected_value = result.get("corrected_value")
        org_category = result.get("org_size_category", "unknown")
        reasoning = result.get("reasoning", "no reasoning provided")

        logger.info(
            "Perplexity records_affected check for '%s': is_cyber=%s, plausible=%s, "
            "category=%s, reasoning=%s",
            event_title[:60], is_cyber, is_plausible, org_category, reasoning,
        )

        # If the event is not a cyber incident, reject the event entirely
        if not is_cyber:
            logger.warning(
                "Perplexity determined '%s' is NOT a cyber security incident: %s. "
                "Event will be rejected.",
                event_title[:60], reasoning,
            )
            return None, False

        # 3. Dynamically update org lists based on Perplexity's assessment
        org_key = (org_name or event_title or "").lower().strip()
        if org_key and org_category == "major_australian":
            MAJOR_AUSTRALIAN_ORGANIZATIONS.add(org_key)
            logger.info(
                "Dynamically added '%s' to MAJOR_AUSTRALIAN_ORGANIZATIONS", org_key,
            )
        elif org_key and org_category == "major_international":
            MAJOR_INTERNATIONAL_ORGANIZATIONS.add(org_key)
            logger.info(
                "Dynamically added '%s' to MAJOR_INTERNATIONAL_ORGANIZATIONS", org_key,
            )

        # 4. Return decision
        if corrected_value is not None:
            corrected_int = safe_int(corrected_value, field_name="corrected_value")
            if corrected_int and corrected_int > 0:
                # Re-validate the corrected value through the same rules — Perplexity
                # can return impossible numbers (e.g. 148 billion) when misled by the
                # org's total customer-lifetime exposure rather than breach scope.
                validated_corrected = validate_records_affected(corrected_int, event_title)
                if validated_corrected is not None:
                    logger.info(
                        "Perplexity corrected records_affected from %s to %s for '%s'",
                        value, validated_corrected, event_title[:60],
                    )
                    return validated_corrected, True
                else:
                    logger.warning(
                        "Perplexity's corrected_value %s for '%s' failed validation — ignoring",
                        corrected_int, event_title[:60],
                    )

        if is_plausible:
            logger.info(
                "Perplexity accepted records_affected=%s for '%s' (rule-based had rejected)",
                f"{value:,}", event_title[:60],
            )
            return value, True

        logger.info(
            "Perplexity also rejected records_affected=%s for '%s': %s",
            f"{value:,}", event_title[:60], reasoning,
        )
        return None, True

    except Exception as e:
        logger.error("Perplexity records_affected validation error: %s", e)
        return None, True


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
