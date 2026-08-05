"""Choose a representative title for a deduplicated event.

A merged event used to inherit whatever title its *master* record happened to
carry. Master selection is driven by dedup mechanics, not by how well a
headline describes the incident, so the results read badly: the 130-record
Qantas breach was labelled "Scattered Spider Ransomware Attacks", the Medibank
breach "Australia Blames Russian Hacker for Major Cyber...", and the 43-record
HWL Ebsworth breach simply "Untitled Event".

Titles are **display metadata, not identity**. Nothing may key on them - see
``schema.py``, which moves the uniqueness constraint onto the immutable
``master_enriched_event_id`` so a title can be improved at any time without
risking a collision.

Selection is deterministic and inspectable: every candidate is scored and the
best one wins. Candidates are the incident label produced by cluster
adjudication (when one exists) plus every member's own title.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional, Sequence, Set

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

PLACEHOLDER_TITLES: Set[str] = {
    "untitled event", "untitled", "none", "n/a", "no title", "unknown",
}

# Publisher / aggregator fragments. A headline that is only a masthead tells
# you nothing about the incident.
PUBLISHER_MARKERS: Set[str] = {
    "itnews", "cyber daily", "7news", "konbriefing", "wikipedia", "reddit",
    "help center", "help centre", "scamwatch", "newsroom", "media centre",
    "australian frequent flyer", "linkedin", "twitter", " / x", "facebook",
    "faqs", "frequently asked questions", "privacy policy", "privacy at",
    "welcome to", "home page", "media release", "blog",
}

# Words indicating the headline actually describes a security incident.
INCIDENT_WORDS: Set[str] = {
    "breach", "ransomware", "cyber", "attack", "incident", "hack", "hacked",
    "leak", "leaked", "compromise", "compromised", "exposed", "exposure",
    "phishing", "malware", "extortion", "intrusion", "stolen",
}

# Trailing " - Publisher" / " | Publisher" fragments to strip.
#
# A dash only separates a masthead when it is *spaced*. Allowing an unspaced
# one cut "ANZCTR cyber attack causes week-long research disruption" at the
# hyphen inside "week-long" and stored the result as "...causes week".
_TRAILING_SOURCE = re.compile(
    r"(?:\s+[-\u2013\u2014]\s+|\s*\|\s*)[^|\-\u2013\u2014]{2,40}$")
_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9]+")

IDEAL_MIN_LENGTH = 20
IDEAL_MAX_LENGTH = 95


def clean_title(title: Optional[str]) -> str:
    """Tidy a headline without changing its meaning.

    Collapses whitespace, drops a trailing masthead ("... - iTnews") and strips
    a trailing ellipsis. Never returns an empty string for non-empty input - a
    poor title still beats none.
    """
    if not title:
        return ""
    text = _WS.sub(" ", str(title)).strip()
    text = text.rstrip(". ").replace("\u2026", "").strip()

    stripped = _TRAILING_SOURCE.sub("", text).strip()
    # Only accept the strip when a usable headline remains.
    if len(stripped) >= IDEAL_MIN_LENGTH and any(
        w in stripped.lower() for w in INCIDENT_WORDS
    ):
        text = stripped
    return text


def _tokens(text: Optional[str]) -> Set[str]:
    return set(_TOKEN.findall((text or "").lower()))


def victim_tokens(victim: Optional[str]) -> Set[str]:
    """Distinctive tokens of an organisation name, for title matching."""
    generic = {
        "pty", "ltd", "limited", "inc", "incorporated", "corporation", "group",
        "holdings", "australia", "australian", "the", "of", "and", "services",
        "company", "co", "department", "government",
    }
    return {t for t in _tokens(victim) if len(t) >= 3 and t not in generic}


def score_title(
    title: Optional[str],
    victim: Optional[Set[str]] = None,
    is_incident_label: bool = False,
) -> float:
    """Score how well a string names the incident. Higher is better.

    The weights encode what makes a headline usable in a dashboard: name the
    organisation, say what happened, and do not be merely a masthead.
    """
    text = clean_title(title)
    if not text:
        return -100.0
    lowered = text.lower()
    if lowered in PLACEHOLDER_TITLES:
        return -50.0

    score = 0.0

    # Cluster-adjudication labels are written to describe the incident, so they
    # start ahead - but they are still scored, not blindly trusted.
    if is_incident_label:
        score += 6.0

    if victim:
        overlap = _tokens(text) & victim
        if overlap:
            score += 5.0 + min(len(overlap), 3)

    incident_hits = sum(1 for w in INCIDENT_WORDS if w in lowered)
    score += min(incident_hits, 3) * 2.0

    for marker in PUBLISHER_MARKERS:
        if marker in lowered:
            score -= 6.0
            break

    length = len(text)
    if IDEAL_MIN_LENGTH <= length <= IDEAL_MAX_LENGTH:
        score += 2.0
    elif length < IDEAL_MIN_LENGTH:
        score -= 3.0
    else:
        score -= 1.0

    # Shouty headlines read poorly and are usually verbatim scrapes.
    letters = [c for c in text if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        score -= 2.0

    # A trailing year, as cluster labels produce, aids disambiguation.
    if re.search(r"\((?:[A-Z][a-z]+ )?\d{4}\)$", text):
        score += 1.5

    return score


def derive_title(
    member_titles: Sequence[Optional[str]],
    victim: Optional[str] = None,
    incident_label: Optional[str] = None,
    current_title: Optional[str] = None,
) -> str:
    """Pick the best available title for a deduplicated event.

    Args:
        member_titles: Titles of every record folded into the event.
        victim: Recorded victim organisation, used to reward on-topic headlines.
        incident_label: Label from cluster adjudication, if the event has one.
        current_title: The title in use now; kept when nothing scores better.

    Returns:
        The highest-scoring candidate, or the current title when no candidate
        improves on it.
    """
    tokens = victim_tokens(victim)
    candidates: List[tuple] = []

    if incident_label:
        candidates.append((score_title(incident_label, tokens, True),
                           clean_title(incident_label)))
    for title in member_titles:
        cleaned = clean_title(title)
        if cleaned:
            candidates.append((score_title(title, tokens), cleaned))

    current_clean = clean_title(current_title)
    current_score = score_title(current_title, tokens) if current_clean else -100.0

    if not candidates:
        return current_clean

    best_score, best_title = max(candidates, key=lambda c: c[0])
    if best_score <= current_score:
        return current_clean or best_title
    return best_title


# ---------------------------------------------------------------------------
# Generation - for events where *every* candidate title is unusable.
# ---------------------------------------------------------------------------
#
# Selection above picks the best available headline. That fails when there is
# nothing good to pick from, which happens in three recurring shapes:
#
#   * placeholders            - "Untitled Event"
#   * an LLM answer stored raw - "The cybersecurity incident reported at the
#     provided URL concerns a major data breach affecting the **Tasmanian
#     Department of Education...**[1][2][3]" (a paragraph, with markdown and
#     citation markers, not a headline)
#   * page furniture           - "Uber - Wikipedia", "Privacy Policy | LUSH",
#     "Alerts and closures", "The followi" (a truncated fragment)
#
# In those cases the incident must be described from the evidence instead.

_PROSE_OPENERS = (
    "the cybersecurity incident", "the incident reported", "the reported",
    "the inaccessible url", "this incident", "the article", "the following",
    "the incident described", "the event reported", "the cyber incident",
)

# Titles that name only a page, not an incident.
_FURNITURE = (
    "privacy policy", "alerts and closures", "latest alerts", "cyber security -",
    "help center", "help centre", "frequently asked questions", "submission",
    "cyber incident update", "cyber incident notice", "cyber update",
)

MAX_TITLE_LENGTH = 120
MIN_TITLE_LENGTH = 22


def needs_regeneration(title: Optional[str]) -> Optional[str]:
    """Reason ``title`` cannot serve as an event headline, else ``None``.

    Detection only - it never edits. Callers decide whether to spend an LLM
    call. Kept deliberately conservative: a short but genuine headline such as
    "Nitro PDF Data Breach" names an organisation and an incident, so it is
    accepted.
    """
    if title is None or not title.strip():
        return "empty"
    t = title.strip()
    low = t.lower()

    if low in PLACEHOLDER_TITLES:
        return "placeholder"
    if len(t) > MAX_TITLE_LENGTH:
        return "prose, not a headline"
    if "**" in t or re.search(r"\[\d+\]", t):
        return "markdown/citation markers"
    if low.startswith(_PROSE_OPENERS):
        return "prose opener"
    if any(f in low for f in _FURNITURE):
        return "page furniture, not an incident"

    if len(t) < MIN_TITLE_LENGTH:
        # Short is fine when it says who and what. "Vinomofo Data Breach" does;
        # "The followi" and "Betwee" are truncations; "Uber - Wikipedia" is a
        # masthead.
        if any(m in low for m in PUBLISHER_MARKERS):
            return "publisher fragment"
        if not (_tokens(t) & INCIDENT_WORDS):
            return "too short and names no incident"
        if not re.search(r"[.!?)]$", t) and len(t) < 14:
            return "truncated fragment"
    return None


class GeneratedTitle(BaseModel):
    """A headline written from the incident evidence."""

    title: str = Field(
        description=(
            "A concise event headline, 25-90 characters, of the form "
            "'<Organisation> <incident type> (<Month Year>)', omitting "
            "the bracketed date entirely if the month and year are not "
            "known - never write '(Unknown Date)'. Name the "
            "Australian organisation whose systems or data were attacked. "
            "No markdown, no citation markers, no publisher name, no "
            "sentences - a headline, not a description."
        )
    )
    organisation: Optional[str] = Field(
        default=None,
        description=(
            "The organisation whose systems or data were attacked, exactly as "
            "named in the evidence. Null if the evidence does not identify one."
        ),
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="0-1 confidence that the title accurately describes the incident.",
    )


class TitleGenerator:
    """Write a headline for an event whose own titles are all unusable.

    Works from the members' enriched descriptions rather than their titles -
    the titles are the thing that is broken. Returns ``None`` on any failure so
    a bad title is never replaced by a worse one.
    """

    def __init__(self, openai_client=None, model: str = "gpt-4o") -> None:
        self.model = model
        self._client = openai_client
        if self._client is None:
            try:
                import os

                import instructor
                from openai import OpenAI

                api_key = os.getenv("OPENAI_API_KEY")
                if api_key:
                    self._client = instructor.from_openai(OpenAI(api_key=api_key))
            except Exception as exc:  # pragma: no cover - dependency issue
                logger.warning("instructor/openai unavailable (%s); no generation", exc)

    def generate(
        self,
        evidence: Sequence[str],
        event_date: Optional[str] = None,
        victim: Optional[str] = None,
    ) -> Optional[GeneratedTitle]:
        """Return a headline for the incident described by ``evidence``."""
        body = "\n\n".join(e.strip() for e in evidence if e and e.strip())[:6000]
        if not body or self._client is None:
            return None

        context = f"Recorded event date: {event_date or 'unknown'}."
        if victim:
            context += f" Recorded victim organisation: {victim}."

        try:
            return self._client.chat.completions.create(
                model=self.model,
                response_model=GeneratedTitle,
                temperature=0.0,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You write short, factual headlines for Australian "
                            "cyber security incidents. The organisation named "
                            "must be the one whose systems or data were "
                            "attacked - not a regulator that reported it, not "
                            "a vendor's product, and not the people whose "
                            "records were in the database."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"{context}\n\nEvidence:\n{body}\n\n"
                                   "Write the event headline.",
                    },
                ],
            )
        except Exception as exc:
            logger.warning("Title generation failed: %s", exc)
            return None
