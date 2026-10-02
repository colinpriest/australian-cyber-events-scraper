"""Classify suspect source pages: one specific incident, or not an incident at all.

Roundups ("AUSCERT Week in Review"), news-topic indexes, scam-alert pages,
guidance toolkits, LinkedIn profiles and home pages were stored as events. They
inflate monthly counts and, worse, the duplicate adjudicator keeps matching them
to the incidents they mention - most of the wrong merges in the 2026-10-02
evaluation involved one.

Title/URL patterns alone are not safe: of 20 live events whose every record
matched a roundup/social pattern, 6 were real incidents whose only source was a
roundup or a post (Ausfec, Insignia, LexisNexis, Perth Mint ...). So patterns
only pick *suspects* (cheap, high recall) and a small model decides each one.
Verdicts are stored in ``PageClassifications`` - inspectable, and never paid
for twice.

A record counts as a non-incident only on a confident non-incident verdict; an
event is rejected only when every one of its records is a non-incident (see
:mod:`non_incident`).
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime
from typing import Dict, List, Literal, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

PAGE_MODEL = "gpt-4o-mini"
# A non-incident verdict below this confidence is ignored.
MIN_CONFIDENCE = 0.8

SUSPECT_TITLE = re.compile(
    r"(week in review|news headlines|weekly (threat|cyber|security) (briefing|update|roundup|wrap)"
    r"|threat briefing|environment update|monthly (roundup|update|wrap)|cyber update|round-?up"
    r"|newsletter|digest|latest (scams|fraud)|scam alerts?|fraud and scam|statistics|\bstats\b"
    r"|trends\b|toolkit|fact ?sheet|guidance|guide to|what (you|we) need to know|lessons (from|in)"
    r"|explained|how to |\btips\b|\bfaq\b|home ?page|blog & media|media cent(er|re)|linkedin"
    r"|'s post|podcast|webinar|\btop \d+|biggest .* (breaches|attacks)|list of data breaches"
    r"|data breaches (in|of) |\| [a-z ]+ news$|services? \||solutions? \|)",
    re.I,
)
SUSPECT_URL = re.compile(
    r"(linkedin\.com/(posts|in|pulse)/|/tag/|/tags/|/category/|/categories/|/topics?/|/author/"
    r"|youtube\.com/|week-in-review|newsletter|roundup)",
    re.I,
)

NON_INCIDENT_KINDS = {"roundup_or_list", "guidance_or_marketing", "profile_or_personal", "index_or_homepage"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS PageClassifications (
    enriched_event_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    confidence REAL NOT NULL,
    reasoning TEXT,
    model TEXT,
    created_at TEXT NOT NULL
)
"""


class PageVerdict(BaseModel):
    """What a scraped page actually is."""

    kind: Literal["specific_incident", "roundup_or_list", "guidance_or_marketing",
                  "profile_or_personal", "index_or_homepage"] = Field(
        description=(
            "specific_incident: the record reports ONE identifiable cyber incident at a "
            "named or clearly described organisation - even if the page is a roundup, a "
            "LinkedIn post or a law-firm note, as long as THIS record is about that one "
            "incident. roundup_or_list: a weekly/monthly review, breach list, statistics "
            "or trends page covering many incidents. guidance_or_marketing: advice, "
            "toolkits, fact sheets, scam warnings, vendor services. profile_or_personal: "
            "a person's profile or a post not reporting an incident. index_or_homepage: a "
            "site home page or a topic/tag index."
        )
    )
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(description="One sentence naming the decisive evidence.")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)


def is_suspect(title: Optional[str], url: Optional[str]) -> bool:
    return bool(SUSPECT_TITLE.search(title or "")) or bool(SUSPECT_URL.search(url or ""))


def load_verdicts(conn: sqlite3.Connection) -> Dict[str, Tuple[str, float]]:
    """``{enriched_event_id: (kind, confidence)}``; empty when the table is absent."""
    try:
        rows = conn.execute("SELECT enriched_event_id, kind, confidence FROM PageClassifications").fetchall()
    except sqlite3.Error:
        return {}
    return {r[0]: (r[1], float(r[2])) for r in rows}


def is_non_incident_verdict(verdict: Optional[Tuple[str, float]]) -> bool:
    return bool(verdict) and verdict[0] in NON_INCIDENT_KINDS and verdict[1] >= MIN_CONFIDENCE


def pending_suspects(conn: sqlite3.Connection, only_unmapped: bool = False) -> List[Tuple[str, str, str, str]]:
    """Active suspect records with no stored verdict: (id, title, url, text)."""
    ensure_schema(conn)
    rows = conn.execute(
        f"""
        SELECT e.enriched_event_id, COALESCE(e.title, ''), COALESCE(r.source_url, ''),
               COALESCE(e.summary, e.description, '')
        FROM EnrichedEvents e
        LEFT JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
        WHERE e.status = 'Active'
          AND NOT EXISTS (SELECT 1 FROM PageClassifications p WHERE p.enriched_event_id = e.enriched_event_id)
          {"AND NOT EXISTS (SELECT 1 FROM EventDeduplicationMap m WHERE m.enriched_event_id = e.enriched_event_id)" if only_unmapped else ""}
        """
    ).fetchall()
    return [tuple(r) for r in rows if is_suspect(r[1], r[2])]


def _client():
    if not os.getenv("OPENAI_API_KEY"):
        return None
    try:
        import instructor
        from openai import OpenAI
        return instructor.from_openai(OpenAI(api_key=os.environ["OPENAI_API_KEY"]))
    except ImportError:
        return None


def classify_pages(conn: sqlite3.Connection, records: Sequence[Tuple[str, str, str, str]],
                   client=None, dry_run: bool = False) -> Dict[str, PageVerdict]:
    """Classify suspect records and store the verdicts.

    Failures are skipped (no verdict stored), so a record is retried next run
    and an outage never hardens into a rejection.
    """
    client = client or _client()
    if client is None:
        logger.warning("No OpenAI client; %d suspect page(s) left unclassified", len(records))
        return {}
    ensure_schema(conn)
    results: Dict[str, PageVerdict] = {}
    for eid, title, url, text in records:
        try:
            verdict: PageVerdict = client.chat.completions.create(
                model=PAGE_MODEL,
                response_model=PageVerdict,
                temperature=0.0,
                messages=[
                    {"role": "system", "content": (
                        "You classify scraped web records for an Australian cyber incident "
                        "database. Decide whether THIS record reports one specific, "
                        "identifiable cyber incident, or is something else. A roundup page "
                        "or social post can still be a specific_incident record if the "
                        "record itself is about one incident at one organisation.")},
                    {"role": "user", "content": f"Title: {title}\nURL: {url}\nText: {text[:700]}"},
                ],
            )
        except Exception as exc:  # noqa: BLE001 - never let one page break a run
            logger.warning("Page classification failed for %s: %s", eid, exc)
            continue
        results[eid] = verdict
        if not dry_run:
            conn.execute(
                "INSERT OR REPLACE INTO PageClassifications VALUES (?, ?, ?, ?, ?, ?)",
                (eid, verdict.kind, verdict.confidence, verdict.reasoning, PAGE_MODEL,
                 datetime.now().isoformat()),
            )
    if not dry_run:
        conn.commit()
    return results
