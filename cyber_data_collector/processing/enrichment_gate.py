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
* a fact must be **supported**: the value appears in the record's own
  persisted text, or in the retrieved text of at least one cited document that
  is about the same incident - it names an organisation the record itself
  names.  Citing a URL is not support; an unrelated page let invented dates,
  organisation names and a huge records-affected count through at high
  reliability when any valid URL counted.

A valid but unsupported fact is *withheld*: it never reaches the typed
columns or the stored JSON, its claim is kept in ``EnrichmentFactSupport`` for
audit, and - when it was confident enough to have been promoted - the record
stays in the retry queue.  Every accepted fact records which source supported
it (field-level provenance).

A result with no fact left after the gate is a *failure*, not an empty
success.  The reliability stored for an accepted result is computed from what
was accepted, never defaulted.

This module is pure (no database, no network) so it can be tested with plain
values.  The cited documents are retrieved by
:mod:`cyber_data_collector.processing.cited_documents` and passed in; the
storage layer calls :func:`plan_enrichment_update` under its own lock to
decide what to write, and :func:`plan_stored_recheck` re-judges a stored
answer (``perplexity_backfill_events.py --recompute-reliability``).
"""
from __future__ import annotations

import calendar
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
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
    # Industry has no confidence of its own and is a classification, not a
    # quoted fact: it stands or falls with the (supported) victim it describes.
    "industry": ("victim_industry", None),
}

# How the supporting source states a fact, stored beside the value in the
# enrichment JSON (attribute -> key of FactDecision.details).  Set by the
# gate from the source text, never taken from the answer.
FACT_DETAIL_FIELDS: Dict[str, Dict[str, str]] = {
    "date": {"date_precision": "precision", "date_basis": "basis"},
    "victim_count": {"victim_count_unit": "unit", "victim_count_basis": "basis",
                     "victim_count_scope": "scope"},
}

# Provenance: where an accepted fact was found.
SUPPORT_RECORD = "record"
SUPPORT_CITED_DOCUMENT = "cited_document"
SUPPORT_ENTITY = "entity"

# Fact-support statuses stored in EnrichmentFactSupport.
FACT_SUPPORTED = "supported"
FACT_WITHHELD = "withheld"

# Words that carry no identity when matching an organisation name in text.
_CORPORATE_NOISE = {
    "pty", "ltd", "limited", "inc", "corp", "corporation", "the", "of", "and",
    "group", "holdings", "australia", "australian",
}

# Words too common in organisation names to identify one on their own: a name
# is matched on its distinctive words ("Police & Nurses" in "Police & Nurses
# Limited"), or as a whole phrase when it has none ("P&N Bank").
_GENERIC_ORG_WORDS = _CORPORATE_NOISE | {
    "bank", "university", "council", "department", "health", "healthcare", "services",
    "service", "government", "company", "co", "hospital", "school", "college", "insurance",
    "energy", "network", "networks", "systems", "solutions", "technologies", "technology",
    "international", "national", "state", "city", "shire", "agency", "authority", "office",
    "commission", "foundation", "association", "institute", "centre", "center", "club",
    "trust", "fund", "partners", "media", "digital", "data", "cyber", "security", "for",
    "new", "south", "wales", "victoria", "queensland", "western", "northern", "territory",
    "tasmania", "nsw", "qld", "vic", "plc", "llc", "gmbh", "trading", "as",
}

# Attack-method vocabulary.  A free-text method ("Third-party hosting provider
# compromise") is supported when every category it names is named in the
# source, in any of that category's words.
_ATTACK_TERMS: Dict[str, Tuple[str, ...]] = {
    "ransomware": (r"ransom\w*",),
    "phishing": (r"phish\w*", r"business email compromise", r"\bbec\b", r"smish\w*", r"vish\w*"),
    "ddos": (r"ddos", r"denial[ -]of[ -]service"),
    "malware": (r"malware", r"trojan", r"spyware", r"info[ -]?stealer\w*", r"stealer", r"virus",
                r"\bworm\b", r"backdoor", r"keylogger", r"botnet"),
    "credentials": (r"credential\w*", r"password\w*", r"log[ -]?in details", r"account takeover"),
    "brute_force": (r"brute[ -]?forc\w*",),
    "injection": (r"sql injection", r"\binjection\b"),
    "vulnerability": (r"vulnerabilit\w*", r"exploit\w*", r"zero[ -]day", r"\bcve-\d+", r"unpatched"),
    "misconfiguration": (r"misconfigur\w*", r"unsecured", r"exposed (?:database|server|bucket)",
                         r"publicly accessible", r"open (?:database|server|bucket)"),
    "insider": (r"insider", r"(?:former |rogue )?(?:employee|staff member|contractor)"),
    "third_party": (r"third[ -]party", r"supplier", r"vendor", r"service provider", r"supply[ -]chain",
                    r"hosting provider", r"contractor"),
    "unauthorised_access": (r"unauthori[sz]ed access", r"\bhack\w*", r"intrusion", r"compromis\w*",
                            r"breach\w*", r"infiltrat\w*", r"accessed"),
    "data_breach": (r"breach\w*", r"leak\w*", r"exposed", r"stolen", r"exfiltrat\w*", r"theft"),
    "fraud": (r"scam\w*", r"fraud\w*", r"impersonat\w*"),
    "defacement": (r"defac\w*",),
}

# Which categories a claimed method names (matched in the claim itself).
_CLAIM_CATEGORY_PATTERNS: Dict[str, Tuple[str, ...]] = {
    **_ATTACK_TERMS,
    # A bare "hack"/"cyber attack" claim is an unauthorised-access claim.
    "unauthorised_access": _ATTACK_TERMS["unauthorised_access"] + (r"cyber[ -]?attack",),
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
# Support: does a source's text state the claimed value?
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
    return list(dict.fromkeys(urls))


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


class _SourceText:
    """A source's text, lower-cased and normalised once for repeated matching.

    ``published`` is the source's own publication date when known: a claimed
    date that is only the page's date is not the incident's.
    """

    __slots__ = ("kind", "url", "lowered", "padded", "published")

    def __init__(self, kind: str, url: Optional[str], text: str, published: Optional[date] = None) -> None:
        self.kind = kind
        self.url = url
        self.lowered = text.lower()
        self.padded = f" {_normalise(text)} "
        self.published = published


def names_organisation(name: str, source: Any) -> bool:
    """True when *source* (text or :class:`_SourceText`) names the organisation *name*.

    The whole normalised name, or every distinctive word of it ("police",
    "nurses" for "Police & Nurses Limited").  A name made only of generic
    words ("P&N Bank" -> "bank") must appear as a phrase.
    """
    padded = source.padded if isinstance(source, _SourceText) else f" {_normalise(str(source))} "
    needle = _normalise(str(name or ""))
    if not needle:
        return False
    if f" {needle} " in padded:
        return True
    distinctive = [t for t in needle.split() if len(t) > 2 and t not in _GENERIC_ORG_WORDS]
    return bool(distinctive) and all(f" {t} " in padded for t in distinctive)


def _text_supports_string(value: str, source: Any) -> bool:
    padded = source.padded if isinstance(source, _SourceText) else f" {_normalise(str(source))} "
    needle = _normalise(value)
    if not needle:
        return False
    if f" {needle} " in padded:
        return True
    tokens = [t for t in needle.split() if len(t) > 2 and t not in _CORPORATE_NOISE]
    return bool(tokens) and all(f" {t} " in padded for t in tokens)


def _lowered(source: Any) -> str:
    return source.lowered if isinstance(source, _SourceText) else str(source).lower()


def _count_spans(value: int, lowered: str) -> List[Tuple[int, int]]:
    """(start, end) of each statement of the number *value* in the text."""
    spans: List[Tuple[int, int]] = []
    for match in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(thousand|million|billion|mil|bn|k|m|b)?\b", lowered):
        digits = match.group(1).replace(",", "")
        try:
            number = float(digits)
        except ValueError:
            continue
        multiplier = _MULTIPLIERS.get(match.group(2) or "", 1)
        if abs(number * multiplier - value) < 0.5:
            spans.append((match.start(), match.end()))
    return spans


def _text_supports_count(value: int, source: Any) -> bool:
    return bool(_count_spans(value, _lowered(source)))


# What an affected count counts, read from the words right after the number.
COUNT_UNITS: Tuple[Tuple[str, str], ...] = (
    ("records", r"(?:data )?records?|files?|documents?|rows?"),
    ("accounts", r"(?:user |customer |email )?accounts?|logins?|credentials?|email addresses"),
    ("customers", r"(?:current and former |former |existing )?(?:customers?|clients?|members?|policyholders?|"
                  r"subscribers?|patients?|students?|users?|borrowers?|applicants?|donors?|"
                  r"passengers?|guests?|employees?|staff|workers?)"),
    ("people", r"(?:people|persons?|individuals?|australians?|citizens?|residents?|victims?|"
               r"(?:new zealanders|kiwis))"),
)
_COUNT_UNIT_RES = [(unit, re.compile(rf"^\W*(?:[a-z'-]+\W+){{0,3}}?(?:{words})\b")) for unit, words in COUNT_UNITS]
_COUNT_ESTIMATE = re.compile(
    r"(?:up to|as many as|around|about|approximately|approx\.?|roughly|nearly|almost|an estimated|"
    r"estimated|potentially|possibly|may have|could have|believed|reportedly|some)\W*$")
_COUNT_AT_LEAST = re.compile(r"(?:more than|over|at least|in excess of|exceeding|upwards of)\W*$")
_SCOPE_AUSTRALIAN = re.compile(r"\baustralian?s?\b|\bin australia\b|\bnsw\b|\bvictorian?s?\b|\bqueensland(?:er)?s?\b")
_SCOPE_GLOBAL = re.compile(r"\bworldwide\b|\bglobal(?:ly)?\b|\baround the world\b|\bacross the world\b|"
                           r"\binternational(?:ly)?\b|\bin \d+ countries\b|\bacross \d+ countries\b")

COUNT_BASIS_STATED = "stated"
COUNT_BASIS_ESTIMATE = "estimate"
COUNT_BASIS_AT_LEAST = "at_least"


def _count_statement(value: int, source: Any) -> Optional[Dict[str, Optional[str]]]:
    """How *source* states the count *value*: unit, basis and scope.

    ``unit`` is what the number counts (records / accounts / customers /
    people), ``basis`` whether it is stated plainly, as an estimate or
    "up to", or as a lower bound, and ``scope`` whether the sentence frames
    it as Australian or worldwide.  Anything the text does not make clear is
    None - never guessed.  Returns None when the number is not in the text.
    """
    lowered = _lowered(source)
    spans = _count_spans(value, lowered)
    if not spans:
        return None
    best: Optional[Dict[str, Optional[str]]] = None
    for start, end in spans:
        after = lowered[end:end + 60]
        before = lowered[max(0, start - 40):start]
        units = [unit for unit, pattern in _COUNT_UNIT_RES if pattern.search(after)]
        unit = units[0] if len(units) == 1 else None
        if _COUNT_ESTIMATE.search(before):
            basis = COUNT_BASIS_ESTIMATE
        elif _COUNT_AT_LEAST.search(before):
            basis = COUNT_BASIS_AT_LEAST
        else:
            basis = COUNT_BASIS_STATED
        sentence_start = max(lowered.rfind(".", 0, start), lowered.rfind("\n", 0, start), start - 200) + 1
        sentence_end_candidates = [i for i in (lowered.find(".", end), lowered.find("\n", end)) if i != -1]
        sentence_end = min(sentence_end_candidates + [end + 200])
        sentence = lowered[sentence_start:sentence_end]
        is_au, is_global = bool(_SCOPE_AUSTRALIAN.search(sentence)), bool(_SCOPE_GLOBAL.search(sentence))
        scope = "australian" if is_au and not is_global else "global" if is_global and not is_au else None
        candidate = {"unit": unit, "basis": basis, "scope": scope}
        # Prefer the statement that says the most about the number.
        if best is None or sum(v is not None for v in candidate.values()) > sum(
                v is not None for v in best.values()):
            best = candidate
    return best


#: Words that, just before a date, mark it as the page's own date (a byline,
#: an update stamp) rather than the date of the incident.
_PUBLICATION_CONTEXT = re.compile(
    r"(?:published|posted|updated|last modified|modified|first published|date published|"
    r"copyright|©)\W*(?:on\W*|at\W*)?(?:[a-z]+\W+){0,2}$"
)
#: A page's own date usually sits in its header; a date equal to the page's
#: publication date that occurs only this close to the top is its byline.
HEADER_ZONE_CHARS = 300

PRECISION_DAY = "day"
PRECISION_MONTH = "month"
BASIS_STATED = "stated"
BASIS_STATED_ON_PUBLICATION_DAY = "stated (same day as the source's publication date)"


@dataclass(frozen=True)
class _DateStatement:
    """How a source states a claimed date (None fields: it does not)."""

    precision: Optional[str] = None
    basis: Optional[str] = None
    publication_only: bool = False


def _date_spans(value: date, lowered: str) -> Tuple[List[int], List[int]]:
    """Start offsets of day-precise and of month-precise statements of *value*."""
    year = str(value.year)
    day = value.day
    months = (calendar.month_name[value.month].lower(), calendar.month_abbr[value.month].lower())
    day_re = rf"0?{day}(?:st|nd|rd|th)?"
    day_patterns = [re.escape(value.isoformat()), rf"\b0?{day}[/.-]0?{value.month}[/.-]{year}\b"]
    for month in months:
        day_patterns += [rf"\b{day_re}\s+(?:of\s+)?{month}\.?,?\s+{year}\b",
                         rf"\b{month}\.?\s+{day_re},?\s+{year}\b"]
    if re.search(rf"\b{year}\b", lowered):
        # "On 12 December, ..." in a text that names the year elsewhere.
        for month in months:
            day_patterns += [rf"\b{day_re}\s+(?:of\s+)?{month}\b(?!\.?,?\s+\d{{4}})",
                             rf"\b{month}\.?\s+{day_re}\b(?!\s*,?\s*\d{{4}})"]
    day_spans = sorted({m.start() for p in day_patterns for m in re.finditer(p, lowered)})
    month_spans: List[int] = []
    if day == 1:
        # The prompt answers YYYY-MM-01 when only the month is known; a
        # month-only statement supports exactly that, at month precision.
        month_spans = sorted({m.start() for month in months
                              for m in re.finditer(rf"\b{month}\.?,?\s+(?:of\s+)?{year}\b", lowered)})
    return day_spans, month_spans


def _date_statement(value: date, source: Any, published: Optional[date] = None) -> _DateStatement:
    """How *source* states *value*: to what precision, and whether as the
    incident date or only as the page's own publication date.

    A day-precise claim needs that day in the text.  A month-only mention
    ("December 2019") supports only a first-of-month claim, and only at
    month precision - never an exact day.  A date that appears only in a
    byline/update stamp, or only in the header of a page published that day,
    is the page's date, not the incident's, and supports nothing.
    """
    lowered = _lowered(source)
    if published is None and isinstance(source, _SourceText):
        published = source.published
    day_spans, month_spans = _date_spans(value, lowered)

    def _is_publication(start: int) -> bool:
        if _PUBLICATION_CONTEXT.search(lowered[max(0, start - 60):start]):
            return True
        return published == value and start < HEADER_ZONE_CHARS

    basis = BASIS_STATED_ON_PUBLICATION_DAY if published == value else BASIS_STATED
    if any(not _is_publication(s) for s in day_spans):
        return _DateStatement(PRECISION_DAY, basis)
    if any(not _is_publication(s) for s in month_spans):
        return _DateStatement(PRECISION_MONTH, BASIS_STATED)
    return _DateStatement(publication_only=bool(day_spans or month_spans))


def _text_supports_date(value: date, source: Any) -> bool:
    """True when the text states *value* (see :func:`_date_statement`)."""
    return _date_statement(value, source).precision is not None


def _attack_categories(claim: str) -> List[str]:
    lowered = claim.lower()
    return [name for name, patterns in _CLAIM_CATEGORY_PATTERNS.items()
            if any(re.search(p, lowered) for p in patterns)]


def _text_supports_attack_method(value: str, source: Any) -> bool:
    categories = _attack_categories(value)
    if not categories:
        # A method outside the vocabulary must be stated in its own words.
        return _text_supports_string(value, source)
    lowered = _lowered(source)
    return all(any(re.search(p, lowered) for p in _ATTACK_TERMS[c]) for c in categories)


def _source_supports(name: str, value: Any, source: _SourceText) -> Tuple[bool, Dict[str, Any]]:
    """Whether *source* supports the fact, with how it states it.

    The details are stored with the fact: a date's ``precision`` and
    ``basis``; a count's ``unit``, ``basis`` and ``scope``; and
    ``publication_only`` when a date appears only as the page's own date.
    """
    if name == "date":
        statement = _date_statement(value, source)
        if statement.precision is None:
            return False, ({"publication_only": True} if statement.publication_only else {})
        return True, {"precision": statement.precision, "basis": statement.basis}
    if name == "victim_count":
        details = _count_statement(value, source)
        return details is not None, (details or {})
    if name == "attack_method":
        return _text_supports_attack_method(str(value), source), {}
    if name in ("entity", "threat_actor"):
        return names_organisation(str(value), source), {}
    return _text_supports_string(str(value), source), {}


def _fact_in_record(name: str, value: Any, text: str) -> bool:
    """True when the record's own text supports the fact (kept for callers)."""
    if not text:
        return False
    return _source_supports(name, value, _SourceText(SUPPORT_RECORD, None, text))[0]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

@dataclass
class FactDecision:
    """Outcome for one fact group.

    Attributes:
        is_accepted: The fact is valid and supported; it may be stored.
        is_withheld: The fact is valid but no source supports it.  It is kept
            out of the typed columns and the stored JSON.
        value: The value as claimed (dates as their ISO text).
        source_kind / source_url: Where an accepted fact was found.
        details: How the source states it - a date's ``precision`` and
            ``basis``; a count's ``unit``, ``basis`` and ``scope``.
    """

    name: str
    is_accepted: bool
    reason: str = ""
    confidence: Optional[float] = None
    value: Any = None
    is_withheld: bool = False
    source_kind: Optional[str] = None
    source_url: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def provenance(self) -> Dict[str, Any]:
        """The row stored in ``EnrichmentFactSupport`` for this fact."""
        return {
            "fact": self.name,
            "status": FACT_SUPPORTED if self.is_accepted else FACT_WITHHELD,
            "value": self.value,
            "confidence": self.confidence,
            "source_kind": self.source_kind,
            "source_url": self.source_url,
            "reason": self.reason or None,
            "details": dict(self.details) if self.details else None,
        }


@dataclass
class GateResult:
    """Outcome of gating one Perplexity answer.

    Attributes:
        is_accepted: True only if at least one fact survived and the answer
            itself was well formed.  A False result must not be written and
            must count as a failed attempt.
        sanitized: The answer with every rejected or withheld fact removed,
            safe to store.
        decisions: Per-fact outcome with provenance, for storage and the log.
        reliability: Computed from the accepted facts, or None.
        failure_reason: Why the answer as a whole was rejected.
        documents_note: How many cited pages were read and were about the
            incident, for messages.
    """

    is_accepted: bool
    sanitized: Optional[PerplexityEventEnrichment] = None
    decisions: Dict[str, FactDecision] = field(default_factory=dict)
    reliability: Optional[float] = None
    failure_reason: Optional[str] = None
    documents_note: str = ""

    @property
    def accepted_facts(self) -> List[str]:
        return [name for name, d in self.decisions.items() if d.is_accepted]

    @property
    def rejected_facts(self) -> Dict[str, str]:
        return {name: d.reason for name, d in self.decisions.items()
                if not d.is_accepted and d.reason}

    @property
    def withheld_facts(self) -> Dict[str, str]:
        """Valid facts no source supports."""
        return {name: d.reason for name, d in self.decisions.items() if d.is_withheld}

    def pending_facts(self, min_apply_confidence: float = MIN_APPLY_CONFIDENCE) -> List[str]:
        """Claimed facts that were not accepted but were confident enough to
        have been promoted - the ones a retry may still establish."""
        return [name for name, d in self.decisions.items()
                if not d.is_accepted and d.confidence is not None
                and d.confidence >= min_apply_confidence]


def _claimed_value_text(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _as_day(value: Any) -> Optional[date]:
    """A ``YYYY-MM-DD`` prefix as a date, else None."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and re.match(r"\d{4}-\d{2}-\d{2}", value.strip()):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def evaluate_enrichment(
    enrichment: Optional[PerplexityEventEnrichment],
    *,
    record_text: str,
    cited_documents: Mapping[str, Any],
    organisations: Sequence[str] = (),
    reference_date: Optional[date] = None,
    record_published: Any = None,
) -> GateResult:
    """Gate a Perplexity answer fact by fact.

    Args:
        enrichment: The parsed answer (None means the request produced nothing).
        record_text: The record's own persisted text (title plus article, else
            snippet).  A fact stated here is supported by the record itself.
        cited_documents: url -> retrieved document (``CitedDocument`` or any
            object with ``is_usable`` and ``text``) for **every** valid URL the
            answer cites, as returned by ``CitedDocumentStore.get_documents``.
            A page that could not be read is present with a failure status.
        organisations: Organisations already linked to the record (victim,
            affected party, vendor).  With the claimed victim name, those of
            them the record's own text names decide which cited pages are
            about the same incident.
        reference_date: The day the record was discovered.
        record_published: The record's own publication date
            (``source_metadata.publication_date``), if known.  A claimed date
            the record carries only as its publication date is withheld.

    Returns:
        A :class:`GateResult`; never raises for a bad answer.

    Raises:
        ValueError: If a cited URL has no entry in *cited_documents* - the
            caller did not retrieve it, and judging without it would silently
            treat the citation as unsupported.
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
    missing = [url for url in sources if url not in cited_documents]
    if missing:
        raise ValueError(f"cited documents were not retrieved for {len(missing)} URL(s), e.g. {missing[0]}")

    record = _SourceText(SUPPORT_RECORD, None, record_text or "", published=_as_day(record_published))
    # Anchors: organisations the record itself names.  A cited page is about
    # this incident only if it names one of them.
    candidates = [enrichment.formal_entity_name] + list(organisations)
    anchors = list(dict.fromkeys(n.strip() for n in candidates
                                 if isinstance(n, str) and n.strip() and names_organisation(n, record)))
    readable = [url for url in sources if getattr(cited_documents[url], "is_usable", False)]
    relevant: List[_SourceText] = []
    for url in readable:
        document = cited_documents[url]
        text = _SourceText(SUPPORT_CITED_DOCUMENT, url, document.text or "",
                           published=_as_day(getattr(document, "published_date", None)))
        if any(names_organisation(anchor, text) for anchor in anchors):
            relevant.append(text)
    documents_note = (f"{len(sources)} cited, {len(readable)} read, {len(relevant)} about the incident"
                      + ("" if anchors else " (the record names no known organisation)"))

    decisions: Dict[str, FactDecision] = {}
    kept: Dict[str, Any] = {}

    for name, (value_attr, conf_attr) in FACT_GROUPS.items():
        value = getattr(enrichment, value_attr)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue  # nothing claimed, nothing to judge

        claimed = _claimed_value_text(value)
        raw_confidence = getattr(enrichment, conf_attr) if conf_attr is not None else None
        confidence: Optional[float] = None
        if conf_attr is not None:
            confidence, problem = validate_confidence(raw_confidence)
            if problem:
                decisions[name] = FactDecision(name, False, problem, None, claimed)
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
            decisions[name] = FactDecision(name, False, problem, confidence, claimed)
            continue

        if name == "industry":
            entity = decisions.get("entity")
            if entity is None or not entity.is_accepted:
                decisions[name] = FactDecision(
                    name, False, "describes a victim name that is not supported", None, claimed,
                    is_withheld=True)
                continue
            decisions[name] = FactDecision(name, True, "", None, claimed,
                                           source_kind=SUPPORT_ENTITY, source_url=entity.source_url)
            kept[value_attr] = value
            continue

        support: Optional[_SourceText] = None
        details: Dict[str, Any] = {}
        is_publication_only = False
        for source in [record] + relevant:
            is_supported, found = _source_supports(name, normalised_value, source)
            if not is_supported:
                is_publication_only = is_publication_only or bool(found.get("publication_only"))
                continue
            # A day-precise statement beats a month-only one wherever it is.
            if support is None or (name == "date" and details.get("precision") != PRECISION_DAY
                                   and found.get("precision") == PRECISION_DAY):
                support, details = source, found
            if name != "date" or details.get("precision") == PRECISION_DAY:
                break
        if support is None:
            why = (f"the date appears only as a source's own publication date, not as the incident date "
                   f"({documents_note})" if is_publication_only else
                   f"not stated in the record's text or in any cited page about the incident ({documents_note})")
            decisions[name] = FactDecision(name, False, why, confidence, claimed, is_withheld=True)
            continue

        decisions[name] = FactDecision(name, True, "", confidence, claimed,
                                       source_kind=support.kind, source_url=support.url, details=details)
        kept[value_attr] = value
        if conf_attr is not None:
            kept[conf_attr] = confidence
        for attr, key in FACT_DETAIL_FIELDS.get(name, {}).items():
            kept[attr] = details.get(key)

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
        return GateResult(False, sanitized=None, decisions=decisions, failure_reason=reason,
                          documents_note=documents_note)

    return GateResult(
        True,
        sanitized=sanitized,
        decisions=decisions,
        reliability=compute_reliability(sanitized, decisions),
        documents_note=documents_note,
    )


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
            # How the source states the value travels with the value.
            for attr in FACT_DETAIL_FIELDS.get(name, {}):
                merged[attr] = incoming.get(attr)
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
        support_rows: ``EnrichmentFactSupport`` rows to upsert (provenance of
            each fact now stored, and the claims withheld).
        pending: Facts withheld that a retry may still establish.  When not
            empty the record is not marked validated and stays queued.
    """

    columns: Dict[str, Any]
    applied: List[str]
    reliability: Optional[float]
    merged: Dict[str, Any]
    support_rows: List[Dict[str, Any]] = field(default_factory=list)
    pending: List[str] = field(default_factory=list)


class CorruptStoredEnrichment(ValueError):
    """The stored enrichment JSON cannot be read, so the baseline is unknown.

    A weaker claim must not overwrite facts whose confidence cannot be read:
    the record is flagged (quarantined) for a human, not treated as if
    nothing were stored.
    """


def read_stored_blob(raw_blob: Any) -> Optional[Dict[str, Any]]:
    """The stored enrichment JSON as a dict, None when nothing is stored.

    Raises:
        CorruptStoredEnrichment: If something is stored but is not a JSON object.
    """
    if raw_blob is None or (isinstance(raw_blob, str) and not raw_blob.strip()):
        return None
    try:
        parsed = json.loads(raw_blob)
    except (TypeError, ValueError) as exc:
        raise CorruptStoredEnrichment(f"stored perplexity_enrichment_data is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CorruptStoredEnrichment(
            f"stored perplexity_enrichment_data is a JSON {type(parsed).__name__}, not an object")
    return parsed


#: Typed EnrichedEvents columns describing ``records_affected``: what it
#: counts, how it was stated, its scope and where it was found.  NULL means
#: the source did not make it clear (never guessed).
RECORDS_AFFECTED_PROVENANCE_COLUMNS = (
    "records_affected_unit", "records_affected_basis", "records_affected_scope", "records_affected_source",
)


def count_provenance_columns(decision: Optional["FactDecision"]) -> Dict[str, Any]:
    """Values of :data:`RECORDS_AFFECTED_PROVENANCE_COLUMNS` for an accepted count."""
    if decision is None or not decision.is_accepted:
        return {column: None for column in RECORDS_AFFECTED_PROVENANCE_COLUMNS}
    details = decision.details or {}
    return {
        "records_affected_unit": details.get("unit"),
        "records_affected_basis": details.get("basis"),
        "records_affected_scope": details.get("scope"),
        "records_affected_source": decision.source_url or decision.source_kind,
    }


def plan_enrichment_update(
    current: Mapping[str, Any],
    gate: GateResult,
    min_apply_confidence: float = MIN_APPLY_CONFIDENCE,
    current_support: Optional[Mapping[str, Mapping[str, Any]]] = None,
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
        current_support: The record's stored ``EnrichmentFactSupport`` rows,
            fact -> row.  A fact already stored *with support* is not pending
            when a new answer's claim for it is withheld.

    Returns:
        The columns to set.  Facts that would weaken what is stored are left
        out, so a stored date, entity or count is only ever replaced by a more
        confident one.  Withheld facts are never among them.
    """
    if not gate.is_accepted or gate.sanitized is None:
        raise ValueError("plan_enrichment_update requires an accepted GateResult")

    stored_blob = read_stored_blob(current.get("perplexity_enrichment_data"))

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
    merged, won = merge_enrichments(stored_blob, for_merge)
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
            columns.update(count_provenance_columns(gate.decisions["victim_count"]))
            applied.append("victim_count")

    # Provenance follows what is stored: a fact this answer put in the JSON
    # gets this answer's support; a fact kept from the stored answer keeps
    # its own.  A withheld claim is recorded unless the fact is already
    # stored with support (a stronger, supported value is not "pending").
    support = current_support or {}
    supported_now = {name for name, row in support.items() if row.get("status") == FACT_SUPPORTED}
    rows: List[Dict[str, Any]] = []
    for name in won:
        decision = gate.decisions.get(name)
        if decision is not None and decision.is_accepted:
            rows.append(decision.provenance())
            supported_now.add(name)
    pending: List[str] = []
    for name, decision in gate.decisions.items():
        if decision.is_accepted or name in supported_now:
            continue
        rows.append(decision.provenance())
        if decision.confidence is not None and decision.confidence >= min_apply_confidence:
            pending.append(name)

    # Reliability is recomputed over the merged answer so it reflects what is
    # now stored.  A previously stored value is deliberately not preserved: the
    # old pipeline wrote a constant 0.85 regardless of content.
    reliability = compute_reliability(PerplexityEventEnrichment(**_model_fields(merged)))

    return EnrichmentUpdatePlan(columns=columns, applied=applied, reliability=reliability,
                                merged=merged, support_rows=rows, pending=pending)


def _model_fields(data: Mapping[str, Any]) -> Dict[str, Any]:
    known = PerplexityEventEnrichment.model_fields
    return {k: v for k, v in data.items() if k in known}


# ---------------------------------------------------------------------------
# Re-judging a stored answer (--recompute-reliability)
# ---------------------------------------------------------------------------

def stored_claims(
    blob: Optional[Mapping[str, Any]],
    support: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> PerplexityEventEnrichment:
    """The answer as originally claimed, rebuilt from what is stored.

    A withheld fact is removed from the JSON but its claim is kept in
    ``EnrichmentFactSupport``; putting it back means a re-check judges the
    same claims every time (so the re-check is idempotent) and a fact
    withheld only because its page could not be read is restored once it can.
    """
    data: Dict[str, Any] = dict(blob or {})
    for name, (value_attr, conf_attr) in FACT_GROUPS.items():
        row = (support or {}).get(name)
        if row is None or row.get("status") != FACT_WITHHELD or data.get(value_attr) is not None:
            continue
        data[value_attr] = row.get("value")
        if conf_attr is not None:
            data[conf_attr] = row.get("confidence")
    fields = _model_fields(data)
    fields["parse_failed"] = False
    fields["failure_reason"] = None
    fields["dropped_fields"] = []
    return PerplexityEventEnrichment(**fields)


@dataclass
class StoredRecheckPlan:
    """Writes for one stored enrichment re-judged by the current gate.

    Attributes:
        columns: EnrichedEvents columns to set (only those that change).
        blob: The enrichment JSON to store (withheld facts removed, facts
            supported again restored), or None to leave it unchanged.
        support_rows: The complete ``EnrichmentFactSupport`` set for the row.
        withheld: Fact -> reason for every claimed fact not accepted.
        pending: Withheld facts confident enough to have been promoted; the
            record goes back into the retry queue.
        cleared: Typed columns cleared or reverted because they held a
            withheld value.
    """

    columns: Dict[str, Any]
    blob: Optional[Dict[str, Any]]
    support_rows: List[Dict[str, Any]]
    reliability: Optional[float]
    withheld: Dict[str, str]
    pending: List[str]
    cleared: List[str]
    is_validated: bool


def _looks_like_date(value: Any) -> bool:
    return isinstance(value, str) and bool(re.match(r"\d{4}-\d{2}-\d{2}", value.strip()))


def _same_value(stored: Any, claimed: Any) -> bool:
    """True when a typed column holds the claimed value."""
    if stored is None or claimed is None:
        return False
    if isinstance(claimed, (int, float)) and not isinstance(claimed, bool):
        try:
            return abs(float(stored) - float(claimed)) < 1e-9
        except (TypeError, ValueError):
            return False
    stored_text, claimed_text = str(stored).strip(), str(claimed).strip()
    if _looks_like_date(claimed_text):
        return stored_text[:10] == claimed_text[:10]
    return stored_text.lower() == claimed_text.lower()


def plan_stored_recheck(
    row: Mapping[str, Any],
    blob: Optional[Mapping[str, Any]],
    gate: GateResult,
    *,
    is_blob_readable: bool = True,
    min_apply_confidence: float = MIN_APPLY_CONFIDENCE,
    max_attempts: int = MAX_ENRICHMENT_ATTEMPTS,
) -> StoredRecheckPlan:
    """Decide the writes that bring a stored enrichment in line with the gate.

    Args:
        row: The EnrichedEvents row (``event_date``, ``date_confidence``,
            ``entity_confidence``, ``attacking_entity_name``, ``attack_method``,
            ``records_affected``, ``data_source_reliability``,
            ``perplexity_validated``, ``perplexity_status``,
            ``perplexity_attempt_count``, ``perplexity_last_error``) plus
            ``raw_event_date``: the date the record carried before enrichment,
            which a withheld date reverts to.
        blob: The stored enrichment JSON (None if absent or unreadable).
        gate: The gate's verdict on :func:`stored_claims`.
        is_blob_readable: False when the stored JSON could not be parsed: the
            baseline is unknown, so the row is un-validated and quarantined
            for a human, and its JSON and typed columns are left as they are.

    Returns:
        A :class:`StoredRecheckPlan`.  Applying it twice changes nothing the
        second time.
    """
    decisions = gate.decisions
    withheld = {name: (d.reason or "not accepted") for name, d in decisions.items() if not d.is_accepted}
    if not gate.is_accepted and not decisions:
        withheld = {"answer": gate.failure_reason or "not accepted"}
    pending = [name for name, d in decisions.items()
               if not d.is_accepted and d.confidence is not None and d.confidence >= min_apply_confidence]

    new_blob: Optional[Dict[str, Any]] = None
    if is_blob_readable:
        new_blob = dict(blob or {})
        for name, (value_attr, conf_attr) in FACT_GROUPS.items():
            decision = decisions.get(name)
            detail_attrs = list(FACT_DETAIL_FIELDS.get(name, {}))
            if decision is not None and decision.is_accepted:
                new_blob[value_attr] = getattr(gate.sanitized, value_attr)
                if conf_attr is not None:
                    new_blob[conf_attr] = getattr(gate.sanitized, conf_attr)
                for attr in detail_attrs:
                    new_blob[attr] = getattr(gate.sanitized, attr)
            elif decision is not None or not gate.is_accepted:
                new_blob[value_attr] = None
                if conf_attr is not None:
                    new_blob[conf_attr] = None
                for attr in detail_attrs:
                    if attr in new_blob:
                        new_blob[attr] = None

    reliability = gate.reliability if gate.is_accepted else None
    is_validated = gate.is_accepted and not pending and is_blob_readable

    columns: Dict[str, Any] = {}
    cleared: List[str] = []

    def _set(column: str, value: Any) -> None:
        if row.get(column) != value:
            columns[column] = value

    for name in decisions:
        decision = decisions[name]
        if decision.is_accepted:
            continue
        value = decision.value
        if name == "date" and _same_value(row.get("event_date"), value):
            revert, _ = validate_iso_date(row.get("raw_event_date"), None) \
                if row.get("raw_event_date") else (None, None)
            revert_text = revert.isoformat() if revert else None
            if revert_text != (str(row.get("event_date"))[:10] if row.get("event_date") else None):
                columns["event_date"] = revert_text
                cleared.append("event_date")
            if row.get("date_confidence") is not None:
                columns["date_confidence"] = None
                cleared.append("date_confidence")
        elif name == "entity" and decision.confidence is not None \
                and _same_value(row.get("entity_confidence"), decision.confidence):
            columns["entity_confidence"] = None
            cleared.append("entity_confidence")
        elif name == "threat_actor" and _same_value(row.get("attacking_entity_name"), value):
            columns["attacking_entity_name"] = None
            cleared.append("attacking_entity_name")
        elif name == "attack_method" and _same_value(row.get("attack_method"), value):
            columns["attack_method"] = None
            cleared.append("attack_method")
        elif name == "victim_count" and _same_value(row.get("records_affected"), value):
            columns["records_affected"] = None
            cleared.append("records_affected")

    # The count's provenance columns describe the stored records_affected only
    # when it is the supported count; otherwise they are unknown (NULL).
    if is_blob_readable:
        count = decisions.get("victim_count")
        records = columns.get("records_affected", row.get("records_affected"))
        is_described = count is not None and count.is_accepted and _same_value(records, count.value)
        for column, value in count_provenance_columns(count if is_described else None).items():
            _set(column, value)

    _set("data_source_reliability", reliability)
    _set("perplexity_validated", 1 if is_validated else 0)
    attempts = int(row.get("perplexity_attempt_count") or 0)
    if is_validated:
        _set("perplexity_status", "validated")
        _set("perplexity_last_error", None)
    else:
        # The stored answer was one attempt: a re-queued record gets the
        # remaining attempts, not a fresh allowance (max() keeps it idempotent).
        if is_blob_readable and attempts < 1:
            attempts = 1
            _set("perplexity_attempt_count", attempts)
        # An unreadable stored answer means the baseline is unknown: flag it
        # for a human (quarantine) rather than re-enrich over it.
        status = ("quarantined" if not is_blob_readable or row.get("perplexity_status") == "quarantined"
                  or attempts >= max_attempts else "retrying")
        _set("perplexity_status", status)
        detail = "; ".join(f"{name}: {why}" for name, why in sorted(withheld.items()))
        if not is_blob_readable:
            detail = "stored enrichment JSON could not be parsed"
        _set("perplexity_last_error", f"recheck: withheld {detail}"[:500])

    support_rows = [decisions[name].provenance() for name in decisions]
    return StoredRecheckPlan(columns=columns, blob=new_blob, support_rows=support_rows,
                             reliability=reliability, withheld=withheld, pending=pending,
                             cleared=cleared, is_validated=is_validated)
