"""Decide a deduplicated event's date, and recover it when it is missing.

Two defects motivated this module.

**Earliest-wins is the wrong merge rule.** Deduplication took the earliest date
among an event's members, so a single mis-extracted date captured the whole
event no matter how much evidence contradicted it. The MediSecure breach has 77
source records, 64 of which say April 2024 - it was stored as **March 2019**,
on the strength of one outlier. That then propagated: the cluster adjudicator
saw a 2019 event and a 2024 event under the same name and concluded they were
two separate incidents, so the fragments could never be merged.

Consensus fixes this. The modal month wins, because agreement among independent
sources is evidence and a lone contrary date is usually a extraction error. The
earliest date *within* the winning month is then used, preserving the original
intent - that an incident is dated from when it began, not when it was reported
- without letting an outlier decide which month that is.

**Dates go missing that are plainly stated in the text.** 160 of 906 active
events had no date at all while their descriptions read "On 2026-07-11, the
DragonForce ransomware group..." or "Between 2025-08-01 and 2025-08-31...".
:func:`extract_date` recovers those without an LLM call.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import date
from typing import Iterable, List, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))

# "2026-07-11", "2026/07/11"
_ISO = re.compile(r"\b(19|20)(\d{2})[-/](\d{1,2})[-/](\d{1,2})\b")
# "11 July 2026", "11th July 2026"
_DMY = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_ALT})\.?\s+((?:19|20)\d{{2}})\b",
    re.IGNORECASE)
# "July 11, 2026", "July 2026"
_MDY = re.compile(
    rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+((?:19|20)\d{{2}})\b",
    re.IGNORECASE)
_MY = re.compile(rf"\b({_MONTH_ALT})\.?\s+((?:19|20)\d{{2}})\b", re.IGNORECASE)

# Earliest plausible incident and a ceiling against typos like "2202".
MIN_YEAR = 1995
MAX_YEAR = date.today().year + 1


def _valid(year: int, month: int, day: int = 1) -> Optional[str]:
    if not (MIN_YEAR <= year <= MAX_YEAR):
        return None
    if not (1 <= month <= 12):
        return None
    day = max(1, min(day, 28 if month == 2 else 30 if month in (4, 6, 9, 11) else 31))
    return f"{year:04d}-{month:02d}-{day:02d}"


def extract_date(text: Optional[str]) -> Optional[str]:
    """First explicit calendar date in ``text`` as ``YYYY-MM-DD``, else None.

    Formats are tried most-specific first so "On 11 July 2026" yields the day
    rather than falling through to a month-only match. A bare month and year
    ("in April 2024") resolves to the first of that month, which is how the
    rest of the pipeline already represents month-granularity incidents.
    """
    if not text:
        return None

    m = _ISO.search(text)
    if m:
        got = _valid(int(m.group(1) + m.group(2)), int(m.group(3)), int(m.group(4)))
        if got:
            return got

    m = _DMY.search(text)
    if m:
        got = _valid(int(m.group(3)), MONTHS[m.group(2).lower().rstrip(".")],
                     int(m.group(1)))
        if got:
            return got

    m = _MDY.search(text)
    if m:
        got = _valid(int(m.group(3)), MONTHS[m.group(1).lower().rstrip(".")],
                     int(m.group(2)))
        if got:
            return got

    m = _MY.search(text)
    if m:
        got = _valid(int(m.group(2)), MONTHS[m.group(1).lower().rstrip(".")])
        if got:
            return got
    return None


def _month_of(value: Optional[str]) -> Optional[str]:
    text = str(value or "").strip()
    return text[:7] if len(text) >= 7 and text[4] == "-" else None


_YEAR = re.compile(r"\b(19|20)\d{2}\b")


def _title_asserts(title: str, current: str) -> bool:
    """True when the headline itself dates the incident to ``current``.

    Checked at month granularity when the title carries a month, and at year
    granularity otherwise - "Optus White Pages breach (2019)" names only a
    year, but that is still a deliberate assertion by whoever wrote the title
    and outranks coverage that has drifted to a different incident.
    """
    titled = extract_date(title)
    if titled:
        return _month_of(titled) == _month_of(current)
    years = {m.group(0) for m in _YEAR.finditer(title)}
    return str(current)[:4] in years


# Consensus must be substantial before it overrules a date already stored.
# Without these floors the pass "corrected" the Optus White Pages breach from
# 2019 to 2022, contaminated by coverage of the larger 2022 Optus breach.
MIN_SUPPORTING = 3
MIN_SHARE = 0.6


def derive_event_date(
    member_dates: Sequence[Optional[str]],
    current: Optional[str] = None,
    title: Optional[str] = None,
) -> Optional[str]:
    """The date best supported by an event's members.

    The modal month wins; within it, the earliest date - so an incident is
    still dated from when it began, without one stray extraction deciding
    which month that is.

    Overruling a date that is already stored requires real agreement
    (:data:`MIN_SUPPORTING` records and :data:`MIN_SHARE` of those dated), and
    is refused outright when the title states a date of its own: a headline
    reading "Optus White Pages breach (2019)" is better evidence than coverage
    that drifted toward a different Optus incident.

    Filling a *blank* date has no such bar - any supported date beats none.
    """
    months = Counter(m for m in (_month_of(d) for d in member_dates) if m)
    if not months:
        return current

    top = max(months.values())
    winner = min(month for month, n in months.items() if n == top)
    same_month = sorted(str(d)[:10] for d in member_dates if _month_of(d) == winner)
    if not same_month:
        return current
    best = same_month[0]

    if not current:
        return best
    if _month_of(current) == winner:
        return current

    if title and _title_asserts(title, current):
        logger.debug("Keeping %s: the title states it", current)
        return current

    dated = sum(months.values())
    if top < MIN_SUPPORTING or top / dated < MIN_SHARE:
        return current
    return best


def disagreement(member_dates: Sequence[Optional[str]]) -> Tuple[int, int]:
    """``(supporting, total)`` counts for the winning month.

    Lets a caller show *why* a date changed - "64 of 77 records say 2024-04" is
    the justification for overruling a stored 2019 date.
    """
    months = [m for m in (_month_of(d) for d in member_dates) if m]
    if not months:
        return (0, 0)
    counts = Counter(months)
    top = max(counts.values())
    winner = min(month for month, n in counts.items() if n == top)
    return (counts[winner], len(months))


class ResolvedDate(BaseModel):
    """A date established by searching for the incident."""

    event_date: Optional[str] = Field(
        default=None,
        description=("The date the incident occurred or was disclosed, as "
                     "YYYY-MM-DD. Use the first of the month when only the "
                     "month is known. Null if it genuinely cannot be "
                     "established - do not guess."))
    confidence: float = Field(ge=0.0, le=1.0)
    basis: str = Field(description="Briefly, what established the date.")


class DateResearcher:
    """Establish an event date by searching, when the stored text cannot.

    Used only for events that survive both consensus and text extraction. The
    incident is usually documented somewhere even when the scraped record is
    thin, so a search answers what parsing cannot: "DECYP cyber security
    incident" alone yields no date, but the incident is dated in coverage.

    Returns ``None`` on any failure or low confidence - an invented date is far
    worse than a blank one, because dates now drive deduplication.
    """

    def __init__(self, perplexity_key: Optional[str] = None, openai_client=None):
        import os

        self.perplexity_key = perplexity_key or os.getenv("PERPLEXITY_API_KEY")
        self._client = openai_client
        if self._client is None:
            try:
                import instructor
                from openai import OpenAI

                key = os.getenv("OPENAI_API_KEY")
                if key:
                    self._client = instructor.from_openai(OpenAI(api_key=key))
            except Exception as exc:  # pragma: no cover - dependency issue
                logger.warning("instructor/openai unavailable (%s)", exc)

    def _search(self, query: str) -> Optional[str]:
        """Raw Perplexity answer for ``query``, or None."""
        if not self.perplexity_key:
            return None
        try:
            import requests

            response = requests.post(
                "https://api.perplexity.ai/chat/completions",
                headers={"Authorization": f"Bearer {self.perplexity_key}",
                         "Content-Type": "application/json"},
                json={"model": "sonar-pro",
                      "messages": [{"role": "user", "content": query}],
                      "max_tokens": 400, "temperature": 0.0},
                timeout=60)
            if response.status_code != 200:
                logger.warning("Perplexity returned %s", response.status_code)
                return None
            return response.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            logger.warning("Perplexity search failed: %s", exc)
            return None

    def resolve(
        self,
        title: str,
        description: Optional[str] = None,
        entity: Optional[str] = None,
    ) -> Optional[ResolvedDate]:
        """Best-supported date for this incident, or None."""
        query = (f"When did this Australian cyber security incident occur? "
                 f"Incident: {title}. "
                 + (f"Organisation: {entity}. " if entity else "")
                 + (f"Details: {(description or '')[:600]}. ")
                 + "Answer with the date it occurred or was disclosed, and say "
                   "if it cannot be determined.")
        evidence = self._search(query)

        # A search result is preferred, but the description alone is still
        # worth putting to the model - it often dates the incident indirectly
        # ("services were restored by 4 May", so the incident preceded it).
        material = evidence or (description or "")
        if not material.strip() or self._client is None:
            return None

        try:
            got = self._client.chat.completions.create(
                model="gpt-4o",
                response_model=ResolvedDate,
                temperature=0.0,
                messages=[
                    {"role": "system",
                     "content": ("You establish when a cyber security incident "
                                 "occurred. Prefer the date of the incident "
                                 "itself over the date it was reported. Return "
                                 "null rather than guessing.")},
                    {"role": "user",
                     "content": f"Incident: {title}\n\nEvidence:\n{material[:3000]}"},
                ])
        except Exception as exc:
            logger.warning("Date resolution failed for %r: %s", title[:50], exc)
            return None

        if not got.event_date:
            return None
        # The model can return a well-formed but implausible date.
        parsed = extract_date(got.event_date) or extract_date(str(got.event_date))
        if not parsed:
            return None
        got.event_date = parsed
        return got

    def resolve_month(
        self,
        title: str,
        description: Optional[str] = None,
        entity: Optional[str] = None,
    ) -> Optional[ResolvedDate]:
        """The month an incident occurred, dated to the first of that month.

        Second tier: an exact day is often unrecoverable, but the month usually
        is, and month granularity is how the rest of the pipeline already
        represents incidents whose day is unknown.
        """
        if self._client is None:
            return None
        evidence = self._search(
            f"In which month and year did this Australian cyber security "
            f"incident occur? Incident: {title}. "
            + (f"Organisation: {entity}. " if entity else "")
            + (f"Details: {(description or '')[:400]}. ")
            + "Answer with the month and year, or say it cannot be determined.")
        material = evidence or (description or "")
        if not material.strip():
            return None

        got = self._ask_month(title, material)
        if got and got.event_date:
            parsed = extract_date(got.event_date)
            if parsed:
                # Month granularity: the day is not claimed.
                got.event_date = parsed[:7] + "-01"
                return got
        return None

    def _ask_month(self, title: str, evidence: str) -> Optional[ResolvedDate]:
        try:
            return self._client.chat.completions.create(
                model="gpt-4o",
                response_model=ResolvedDate,
                temperature=0.0,
                messages=[
                    {"role": "system",
                     "content": ("You establish the month a cyber security "
                                 "incident occurred. Answer with the first day "
                                 "of that month in YYYY-MM-DD form. Return null "
                                 "if even the month cannot be established.")},
                    {"role": "user",
                     "content": f"Incident: {title}\n\nEvidence:\n{evidence[:3000]}"},
                ])
        except Exception as exc:
            logger.warning("Month resolution failed for %r: %s", title[:50], exc)
            return None


# ---------------------------------------------------------------------------
# Records affected - the same defect class as the date
# ---------------------------------------------------------------------------
#
# A deduplicated event's ``records_affected`` was not derived from its members
# either, so values appeared that no source record supports: NSW Health was
# stored as 12,900,000 while its one source record says 600, and Health
# Management Systems carried the same 12.9 million although none of its five
# records states a figure at all. Both had inherited MediSecure's number.

def derive_records_affected(
    member_counts: Sequence[Optional[int]],
    current: Optional[int] = None,
) -> Tuple[Optional[int], str]:
    """The record count best supported by an event's members.

    Returns ``(value, reason)``. The value must be one a member actually
    reports; the most-supported wins, ties going to the larger figure because
    later reporting usually revises a breach upward.

    Two refusals, both deliberate:

    * with no member figure at all, the stored value came from nowhere and is
      cleared - an unsupported number is worse than a blank, since it is read
      as fact downstream;
    * when every member reports a different figure and they span more than an
      order of magnitude, the members are contaminated by coverage of another
      incident (the Optus White Pages event carries 41,278 alongside
      9,700,000, the later and much larger Optus breach). Picking any of them
      would be a guess, so the stored value is left for a human.
    """
    values = [int(v) for v in member_counts if v is not None and int(v) > 0]
    if not values:
        return (None, "no source record states a figure")

    if current is not None and current in values:
        return (current, "supported")

    counts = Counter(values)
    top = max(counts.values())
    best = max(v for v, n in counts.items() if n == top)

    if top == 1 and len(counts) > 1 and max(values) > 10 * min(values):
        return (current, "members disagree by more than 10x; left for review")

    return (best, f"{top} of {len(values)} record(s) report {best:,}")


# ---------------------------------------------------------------------------
# Last-resort dating: the month, then the article's own date
# ---------------------------------------------------------------------------
#
# A handful of events resist every earlier method: the coverage is a weekly
# threat-intelligence round-up, a vendor's services page, or a Reddit thread,
# none of which states when the incident happened. An approximate date is still
# far better than none, because a missing date reads to the cluster adjudicator
# as evidence of a separate incident and blocks deduplication outright.

# Dates embedded in article URLs: "/blog/2026/2/25/", "/2026-02-25-".
_URL_YMD = re.compile(r"/((?:19|20)\d{2})[/-](\d{1,2})[/-](\d{1,2})(?:/|-|$)")
_URL_YM = re.compile(r"/((?:19|20)\d{2})[/-](\d{1,2})(?:/|-|$)")
# Slugs such as "29th-january-2026-cyber-update".
_URL_SLUG = re.compile(
    rf"(?<![0-9])(\d{{1,2}})(?:st|nd|rd|th)?-({_MONTH_ALT})-((?:19|20)\d{{2}})",
    re.IGNORECASE)
_URL_SLUG_MY = re.compile(rf"({_MONTH_ALT})-((?:19|20)\d{{2}})", re.IGNORECASE)


def date_from_url(url: Optional[str]) -> Optional[str]:
    """Publication date encoded in an article URL, or None.

    Publishers put the date in the path far more often than in the page text
    the scraper captured, so this frequently succeeds where extraction failed.
    """
    if not url:
        return None

    m = _URL_YMD.search(url)
    if m:
        got = _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if got:
            return got

    m = _URL_SLUG.search(url)
    if m:
        got = _valid(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
        if got:
            return got

    m = _URL_SLUG_MY.search(url)
    if m:
        got = _valid(int(m.group(2)), MONTHS[m.group(1).lower()])
        if got:
            return got

    m = _URL_YM.search(url)
    if m:
        got = _valid(int(m.group(1)), int(m.group(2)))
        if got:
            return got
    return None
