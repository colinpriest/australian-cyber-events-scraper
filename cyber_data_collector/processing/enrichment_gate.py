"""Acceptance gate for enrichment facts.

Perplexity answers are free text turned into JSON.  Before this gate existed
the backfill wrote whatever parsed: an unparseable response became an empty
result with zero confidence and was still stored as ``perplexity_validated``
with a hard-coded reliability of 0.85, which also stopped the record from ever
being enriched again.  95 rows in the live database are in exactly that state.

The rule here is that *nothing is written unless it passes*, fact by fact:

* a date must be a real ISO ``YYYY-MM-DD`` date, not after the day the record
  was discovered and not absurdly old;
* every confidence must be a finite number in [0, 1];
* a count must be a non-negative integer;
* a fact must be attributable - the answer cites at least one real source URL,
  or the value appears in the record's own text.

A result with no fact left after the gate is a *failure*, not an empty
success.  The reliability stored for an accepted result is computed from what
was accepted, never defaulted.

This module is pure (no database, no network) so it can be tested with plain
values.  The storage layer calls :func:`plan_enrichment_update` under its own
lock to decide what to write.
"""
from __future__ import annotations

import calendar
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

from cyber_data_collector.processing.perplexity_enrichment import PerplexityEventEnrichment

#: Events older than this are treated as extraction errors (a mis-read year).
MIN_PLAUSIBLE_EVENT_DATE = date(1990, 1, 1)

#: A fact below this confidence is stored in the enrichment JSON for
#: reference but never promoted to the typed EnrichedEvents columns.
MIN_APPLY_CONFIDENCE = 0.6

#: Failed attempts allowed before a record is quarantined.
MAX_ENRICHMENT_ATTEMPTS = 3

#: Slack for timezones between the event, the discovery stamp and "today".
_DATE_SLACK = timedelta(days=1)

# Fact groups: name -> (value attribute, confidence attribute).  A group is
# accepted or rejected as a unit because a value without its confidence (or
# the reverse) is not usable.
FACT_GROUPS: Dict[str, Tuple[str, Optional[str]]] = {
    "date": ("earliest_event_date", "date_confidence"),
    "entity": ("formal_entity_name", "entity_confidence"),
    "threat_actor": ("threat_actor", "threat_actor_confidence"),
    "attack_method": ("attack_method", "attack_method_confidence"),
    "victim_count": ("victim_count", "victim_count_confidence"),
    # Industry has no confidence of its own; it is attributed like the rest.
    "industry": ("victim_industry", None),
}

# Words that carry no identity when matching an organisation name in text.
_CORPORATE_NOISE = {
    "pty", "ltd", "limited", "inc", "corp", "corporation", "the", "of", "and",
    "group", "holdings", "australia", "australian",
}

_MULTIPLIERS = {
    "thousand": 1_000, "k": 1_000,
    "million": 1_000_000, "m": 1_000_000, "mil": 1_000_000,
    "billion": 1_000_000_000, "bn": 1_000_000_000, "b": 1_000_000_000,
}


# ---------------------------------------------------------------------------
# Primitive validators (reused by the discovery storage path)
# ---------------------------------------------------------------------------

def validate_iso_date(
    value: Any,
    reference: Optional[date] = None,
) -> Tuple[Optional[date], Optional[str]]:
    """Validate an event date.

    Args:
        value: Candidate date; must be a ``YYYY-MM-DD`` string (or a date).
        reference: Latest acceptable date, normally the day the record was
            discovered.  Defaults to today.  An event cannot post-date its own
            discovery, so a later date is an extraction error.

    Returns:
        ``(date, None)`` when valid, else ``(None, reason)``.
    """
    if isinstance(value, datetime):
        parsed: Optional[date] = value.date()
    elif isinstance(value, date):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        # fromisoformat accepts forms such as "2024W101" that are not
        # YYYY-MM-DD, so check the shape first.
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return None, f"date {value!r} is not ISO YYYY-MM-DD"
        try:
            parsed = date.fromisoformat(text)
        except ValueError:
            return None, f"date {value!r} is not a real calendar date"
    else:
        return None, f"date has unsupported type {type(value).__name__}"

    # Never trust a reference later than today: a clock-skewed discovery stamp
    # must not widen the window.
    latest = min(reference or date.today(), date.today()) + _DATE_SLACK
    if parsed > latest:
        return None, f"date {parsed.isoformat()} is after discovery ({latest.isoformat()})"
    if parsed < MIN_PLAUSIBLE_EVENT_DATE:
        return None, f"date {parsed.isoformat()} is before {MIN_PLAUSIBLE_EVENT_DATE.isoformat()}"
    return parsed, None


def validate_confidence(value: Any) -> Tuple[Optional[float], Optional[str]]:
    """Return ``(confidence, None)`` if *value* is a finite number in [0, 1]."""
    if value is None:
        return None, "confidence missing"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"confidence {value!r} is not a number"
    if math.isnan(value) or math.isinf(value):
        return None, f"confidence {value!r} is not finite"
    if value < 0.0 or value > 1.0:
        return None, f"confidence {value} is outside [0, 1]"
    return float(value), None


def validate_count(value: Any) -> Tuple[Optional[int], Optional[str]]:
    """Return ``(count, None)`` if *value* is a non-negative whole number."""
    if value is None:
        return None, "count missing"
    if isinstance(value, bool):
        return None, "count is a boolean"
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value) or value != int(value):
            return None, f"count {value!r} is not a whole number"
        value = int(value)
    if not isinstance(value, int):
        return None, f"count {value!r} is not an integer"
    if value < 0:
        return None, f"count {value} is negative"
    return value, None


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

def valid_source_urls(sources: Any) -> List[str]:
    """Keep only real http(s) URLs from a ``sources_consulted`` list."""
    urls: List[str] = []
    if not isinstance(sources, (list, tuple)):
        return urls
    for item in sources:
        if not isinstance(item, str):
            continue
        candidate = item.strip()
        parsed = urlparse(candidate)
        # "url1" / "example" placeholders have no scheme or no dotted host.
        if parsed.scheme in ("http", "https") and "." in parsed.netloc:
            urls.append(candidate)
    return urls


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _text_supports_string(value: str, text: str) -> bool:
    needle = _normalise(value)
    if not needle:
        return False
    haystack = f" {_normalise(text)} "
    if f" {needle} " in haystack:
        return True
    tokens = [t for t in needle.split() if len(t) > 2 and t not in _CORPORATE_NOISE]
    return bool(tokens) and all(f" {t} " in haystack for t in tokens)


def _text_supports_count(value: int, text: str) -> bool:
    lowered = text.lower()
    for match in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(thousand|million|billion|mil|bn|k|m|b)?\b", lowered):
        digits = match.group(1).replace(",", "")
        try:
            number = float(digits)
        except ValueError:
            continue
        multiplier = _MULTIPLIERS.get(match.group(2) or "", 1)
        if abs(number * multiplier - value) < 0.5:
            return True
    return False


def _text_supports_date(value: date, text: str) -> bool:
    lowered = text.lower()
    if value.isoformat() in lowered:
        return True
    year = str(value.year)
    day = value.day
    for month in (calendar.month_name[value.month].lower(), calendar.month_abbr[value.month].lower()):
        patterns = (
            rf"\b{day}(?:st|nd|rd|th)?\s+{month}\.?,?\s+{year}\b",
            rf"\b{month}\.?\s+{day}(?:st|nd|rd|th)?,?\s+{year}\b",
            # Month and year alone: the prompt tells Perplexity to answer
            # YYYY-MM-01 when only the month is known.
            rf"\b{month}\.?,?\s+{year}\b",
        )
        if any(re.search(p, lowered) for p in patterns):
            return True
    return bool(re.search(rf"\b0?{day}[/.-]0?{value.month}[/.-]{year}\b", lowered))


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

@dataclass
class FactDecision:
    """Outcome for one fact group."""

    name: str
    is_accepted: bool
    reason: str = ""
    confidence: Optional[float] = None


@dataclass
class GateResult:
    """Outcome of gating one Perplexity answer.

    Attributes:
        is_accepted: True only if at least one fact survived and the answer
            itself was well formed.  A False result must not be written and
            must count as a failed attempt.
        sanitized: The answer with every rejected fact removed, safe to store.
        decisions: Per-fact outcome, for logs and the audit trail.
        reliability: Computed from the accepted facts, or None.
        failure_reason: Why the answer as a whole was rejected.
    """

    is_accepted: bool
    sanitized: Optional[PerplexityEventEnrichment] = None
    decisions: Dict[str, FactDecision] = field(default_factory=dict)
    reliability: Optional[float] = None
    failure_reason: Optional[str] = None

    @property
    def accepted_facts(self) -> List[str]:
        return [name for name, d in self.decisions.items() if d.is_accepted]

    @property
    def rejected_facts(self) -> Dict[str, str]:
        return {name: d.reason for name, d in self.decisions.items()
                if not d.is_accepted and d.reason}


def evaluate_enrichment(
    enrichment: Optional[PerplexityEventEnrichment],
    *,
    record_text: str = "",
    reference_date: Optional[date] = None,
) -> GateResult:
    """Gate a Perplexity answer fact by fact.

    Args:
        enrichment: The parsed answer (None means the request produced nothing).
        record_text: The record's own persisted text (article, else snippet),
            used to attribute facts when the answer cites no source.
        reference_date: The day the record was discovered.

    Returns:
        A :class:`GateResult`; never raises.
    """
    if enrichment is None:
        return GateResult(False, failure_reason="no response from Perplexity")
    if enrichment.parse_failed:
        return GateResult(
            False,
            failure_reason=enrichment.failure_reason or "response could not be parsed",
        )

    _, overall_problem = validate_confidence(enrichment.overall_confidence)
    if overall_problem:
        return GateResult(False, failure_reason=f"overall {overall_problem}")

    sources = valid_source_urls(enrichment.sources_consulted)
    has_sources = bool(sources)
    decisions: Dict[str, FactDecision] = {}
    kept: Dict[str, Any] = {}

    for name, (value_attr, conf_attr) in FACT_GROUPS.items():
        value = getattr(enrichment, value_attr)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue  # nothing claimed, nothing to judge

        confidence: Optional[float] = None
        if conf_attr is not None:
            confidence, problem = validate_confidence(getattr(enrichment, conf_attr))
            if problem:
                decisions[name] = FactDecision(name, False, problem)
                continue

        normalised_value: Any = value
        problem = None
        if name == "date":
            parsed, problem = validate_iso_date(value, reference_date)
            normalised_value = parsed
        elif name == "victim_count":
            normalised_value, problem = validate_count(value)
        elif not isinstance(value, str):
            problem = f"{name} is not text"
        if problem:
            decisions[name] = FactDecision(name, False, problem, confidence)
            continue

        if not has_sources:
            supported = _fact_in_record(name, normalised_value, record_text)
            if not supported:
                decisions[name] = FactDecision(
                    name, False,
                    "no source cited and the value is not in the record's own text",
                    confidence,
                )
                continue

        decisions[name] = FactDecision(name, True, "", confidence)
        kept[value_attr] = value
        if conf_attr is not None:
            kept[conf_attr] = confidence

    # Facts that were claimed but rejected also carry a stray confidence in
    # the raw answer; the sanitized copy starts from a blank model so none of
    # that leaks through.
    sanitized = PerplexityEventEnrichment(
        sources_consulted=sources,
        overall_confidence=float(enrichment.overall_confidence),
        reasoning=enrichment.reasoning,
        **kept,
    )

    accepted = [d for d in decisions.values() if d.is_accepted]
    if not accepted:
        rejected_note = "; ".join(f"{n}: {d.reason}" for n, d in decisions.items() if d.reason)
        reason = "no usable facts in the response"
        if rejected_note:
            reason = f"all facts rejected ({rejected_note})"
        return GateResult(False, sanitized=None, decisions=decisions, failure_reason=reason)

    return GateResult(
        True,
        sanitized=sanitized,
        decisions=decisions,
        reliability=compute_reliability(sanitized, decisions),
    )


def _fact_in_record(name: str, value: Any, text: str) -> bool:
    if not text:
        return False
    if name == "date":
        return _text_supports_date(value, text)
    if name == "victim_count":
        return _text_supports_count(value, text)
    return _text_supports_string(str(value), text)


def compute_reliability(
    enrichment: PerplexityEventEnrichment,
    decisions: Optional[Mapping[str, FactDecision]] = None,
) -> Optional[float]:
    """Reliability of an accepted answer, derived from what was accepted.

    The mean of the accepted facts' own confidences, averaged with the
    answer's overall confidence when it reported one.  Returns None when no
    accepted fact carries a confidence - unknown is stored as NULL, never as
    a flattering constant.
    """
    confidences: List[float] = []
    for name, (_, conf_attr) in FACT_GROUPS.items():
        if conf_attr is None:
            continue
        if decisions is not None and not (name in decisions and decisions[name].is_accepted):
            continue
        value = getattr(enrichment, conf_attr)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            confidences.append(float(value))
    if not confidences:
        return None
    fact_mean = sum(confidences) / len(confidences)
    overall = enrichment.overall_confidence
    if isinstance(overall, (int, float)) and overall > 0:
        fact_mean = (fact_mean + float(overall)) / 2.0
    return round(max(0.0, min(1.0, fact_mean)), 3)


# ---------------------------------------------------------------------------
# Never let a weaker answer replace a stronger one
# ---------------------------------------------------------------------------

def _confidence_of(data: Mapping[str, Any], conf_attr: Optional[str]) -> float:
    if conf_attr is None:
        return 0.0
    value = data.get(conf_attr)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def merge_enrichments(
    existing: Optional[Mapping[str, Any]],
    new: PerplexityEventEnrichment,
) -> Tuple[Dict[str, Any], List[str]]:
    """Merge a new accepted answer into the stored one, strongest fact wins.

    A fact the new answer does not make is kept from the stored answer; a fact
    both make is replaced only if the new confidence is strictly higher.

    Returns:
        ``(merged_dict, groups_won_by_new)``.
    """
    base: Dict[str, Any] = dict(existing) if existing else {}
    incoming = new.model_dump()
    merged: Dict[str, Any] = {**PerplexityEventEnrichment().model_dump(), **base}
    won: List[str] = []

    for name, (value_attr, conf_attr) in FACT_GROUPS.items():
        new_value = incoming.get(value_attr)
        if new_value is None:
            continue
        old_value = base.get(value_attr)
        if old_value is None:
            take = True
        elif conf_attr is None:
            take = False  # industry: fill gaps only
        else:
            take = _confidence_of(incoming, conf_attr) > _confidence_of(base, conf_attr)
        if take:
            merged[value_attr] = new_value
            if conf_attr is not None:
                merged[conf_attr] = incoming.get(conf_attr)
            won.append(name)

    # Industry describes the victim, so it follows the entity when that changed.
    if "entity" in won and incoming.get("victim_industry"):
        merged["victim_industry"] = incoming["victim_industry"]
        if "industry" not in won:
            won.append("industry")

    merged["sources_consulted"] = list(dict.fromkeys(
        list(base.get("sources_consulted") or []) + list(incoming.get("sources_consulted") or [])
    ))
    merged["overall_confidence"] = max(
        _confidence_of(base, "overall_confidence"), _confidence_of(incoming, "overall_confidence")
    )
    if won and incoming.get("reasoning"):
        merged["reasoning"] = incoming["reasoning"]
    # A stored answer is by definition one that passed the gate.
    merged["parse_failed"] = False
    merged["failure_reason"] = None
    merged["dropped_fields"] = []
    return merged, won


@dataclass
class EnrichmentUpdatePlan:
    """What to write for one accepted answer.

    Attributes:
        columns: EnrichedEvents column -> value (excludes bookkeeping columns).
        applied: Fact groups promoted to typed columns.
        reliability: Reliability for ``data_source_reliability`` (None = NULL).
        merged: The enrichment JSON to store.
    """

    columns: Dict[str, Any]
    applied: List[str]
    reliability: Optional[float]
    merged: Dict[str, Any]


def plan_enrichment_update(
    current: Mapping[str, Any],
    gate: GateResult,
    min_apply_confidence: float = MIN_APPLY_CONFIDENCE,
) -> EnrichmentUpdatePlan:
    """Decide the writes for an accepted answer against the stored row.

    Args:
        current: The EnrichedEvents row as stored now (``event_date``,
            ``date_confidence``, ``entity_confidence``, ``attacking_entity_name``,
            ``attack_method``, ``records_affected``, ``perplexity_enrichment_data``,
            ``data_source_reliability``).
        gate: An accepted :class:`GateResult`.
        min_apply_confidence: Confidence needed to promote a fact to a typed
            column (lower-confidence facts stay in the JSON only).

    Returns:
        The columns to set.  Facts that would weaken what is stored are left
        out, so a stored date, entity or count is only ever replaced by a more
        confident one.
    """
    if not gate.is_accepted or gate.sanitized is None:
        raise ValueError("plan_enrichment_update requires an accepted GateResult")

    stored_blob: Optional[Dict[str, Any]] = None
    raw_blob = current.get("perplexity_enrichment_data")
    if raw_blob:
        try:
            import json
            parsed = json.loads(raw_blob)
            stored_blob = parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            stored_blob = None

    new = gate.sanitized
    stored_date_conf = current.get("date_confidence")
    stored_date_conf = float(stored_date_conf) if isinstance(stored_date_conf, (int, float)) else None

    # The typed column may hold a stronger date than the stored JSON (the JSON
    # can be absent).  Keep a weaker date out of the JSON too, so the JSON and
    # the column never disagree about which date won.
    for_merge = new
    if (new.earliest_event_date and stored_date_conf is not None
            and (new.date_confidence or 0.0) <= stored_date_conf):
        for_merge = new.model_copy(update={"earliest_event_date": None, "date_confidence": None})
    merged, _ = merge_enrichments(stored_blob, for_merge)
    columns: Dict[str, Any] = {}
    applied: List[str] = []

    # event_date: the typed column carries its own confidence, which is the
    # authoritative "what is stored now" for the date.
    if new.earliest_event_date and (new.date_confidence or 0.0) >= min_apply_confidence:
        if stored_date_conf is None or new.date_confidence > stored_date_conf:
            columns["event_date"] = new.earliest_event_date
            columns["date_confidence"] = new.date_confidence
            applied.append("date")

    if new.formal_entity_name and (new.entity_confidence or 0.0) >= min_apply_confidence:
        stored_conf = current.get("entity_confidence")
        if not isinstance(stored_conf, (int, float)) or new.entity_confidence > stored_conf:
            columns["entity_confidence"] = new.entity_confidence
            applied.append("entity")

    if new.threat_actor and (new.threat_actor_confidence or 0.0) >= min_apply_confidence:
        if (not current.get("attacking_entity_name")
                or new.threat_actor_confidence > _confidence_of(
                    stored_blob or {}, "threat_actor_confidence")):
            columns["attacking_entity_name"] = new.threat_actor
            applied.append("threat_actor")

    if new.attack_method and (new.attack_method_confidence or 0.0) >= min_apply_confidence:
        if (not current.get("attack_method")
                or new.attack_method_confidence > _confidence_of(
                    stored_blob or {}, "attack_method_confidence")):
            columns["attack_method"] = new.attack_method
            applied.append("attack_method")

    if new.victim_count is not None and (new.victim_count_confidence or 0.0) >= min_apply_confidence:
        if (current.get("records_affected") is None
                or new.victim_count_confidence > _confidence_of(
                    stored_blob or {}, "victim_count_confidence")):
            columns["records_affected"] = new.victim_count
            applied.append("victim_count")

    # Reliability is recomputed over the merged answer so it reflects what is
    # now stored.  A previously stored value is deliberately not preserved: the
    # old pipeline wrote a constant 0.85 regardless of content.
    reliability = compute_reliability(PerplexityEventEnrichment(**merged))

    return EnrichmentUpdatePlan(columns=columns, applied=applied, reliability=reliability, merged=merged)
