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

import json
import logging
import os
import re
import sqlite3
from datetime import datetime
from typing import Dict, List, Literal, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# A non-incident verdict below this confidence is ignored.
MIN_CONFIDENCE = 0.8

# Bumped whenever the question or the evidence shown changes; a stored verdict
# from another version is re-asked. v1 showed the enrichment *summary*, which
# an earlier model had written as if every page reported an incident ("A
# cyber-attack targeted the ACT Government" for the ACT business-services
# page), so every government service page was judged a specific incident. v2
# shows the page's own scraped text.
QUESTION_VERSION = "page-kind-v2-source-text"
SOURCE_CHARS = 3000

KIND_DESCRIPTIONS = {
    "specific_incident": (
        "The page reports ONE identifiable cyber incident at a named or clearly described "
        "organisation - even if it is a roundup, a LinkedIn post or a law-firm note, as long as "
        "this page is about that one incident."),
    "roundup_or_list": "A weekly or monthly review, breach list, statistics or trends page covering many incidents.",
    "guidance_or_marketing": (
        "Advice, a toolkit, fact sheet, scam warning, a government or company service page, a "
        "form, or vendor marketing - including pages ABOUT data breaches that report none."),
    "profile_or_personal": "A person's profile, or a post that does not report an incident.",
    "index_or_homepage": "A site home page, section page, or topic/tag index.",
}

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

# Government service and information pages ("Business - ACT Government", Service
# NSW's "Report identity theft" form, Services Australia's data-matching notice)
# named no roundup or guidance pattern, so they were never classified and stood
# as events - several of them as second "attacks" on the agency within weeks of
# its real one. News and media-release paths are left out; everything else on a
# .gov.au host is a suspect for the model to decide, incident words included,
# because a service page about breaches is still not a breach.
SUSPECT_GOV_URL = re.compile(r"\.gov\.au/(?!.*(news|media|release|statement))", re.I)
# A title with no incident vocabulary at all is a page title, not a report of an
# incident: "Free three-year-old preschool - ACT Government", "Deloitte -
# Wikipedia", "Untitled Event".
INCIDENT_VOCABULARY = re.compile(
    r"breach|hack|attack|ransom|cyber|incident|leak|expos|compromis|stol|theft|scam|fraud"
    r"|malware|phish|ddos|outage|vulnerab|exploit|intrusion|unauthori|espionage|extort"
    r"|threat|disrupt|data",
    re.I,
)

NON_INCIDENT_KINDS ={"roundup_or_list", "guidance_or_marketing", "profile_or_personal", "index_or_homepage"}

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
    columns = {r[1] for r in conn.execute("PRAGMA table_info(PageClassifications)")}
    if "question_version" not in columns:
        conn.execute("ALTER TABLE PageClassifications ADD COLUMN question_version TEXT")


def is_suspect(title: Optional[str], url: Optional[str]) -> bool:
    return (bool(SUSPECT_TITLE.search(title or "")) or bool(SUSPECT_URL.search(url or ""))
            or bool(SUSPECT_GOV_URL.search(url or ""))
            or not INCIDENT_VOCABULARY.search(title or ""))


def load_verdicts(conn: sqlite3.Connection) -> Dict[str, Tuple[str, float]]:
    """``{enriched_event_id: (kind, confidence)}``; empty when the table is absent."""
    try:
        rows = conn.execute("SELECT enriched_event_id, kind, confidence FROM PageClassifications").fetchall()
    except sqlite3.Error:
        return {}
    return {r[0]: (r[1], float(r[2])) for r in rows}


def is_non_incident_verdict(verdict: Optional[Tuple[str, float]]) -> bool:
    return bool(verdict) and verdict[0] in NON_INCIDENT_KINDS and verdict[1] >= MIN_CONFIDENCE


def pending_suspects(conn: sqlite3.Connection, only_unmapped: bool = False) -> List[Tuple[str, str, str, str, str]]:
    """Active suspect records with no current verdict: (id, title, url, page text, summary).

    A verdict from an earlier ``QUESTION_VERSION`` does not count as current.
    """
    ensure_schema(conn)
    rows = conn.execute(
        f"""
        SELECT e.enriched_event_id, COALESCE(e.title, ''), COALESCE(r.source_url, ''),
               SUBSTR(COALESCE(NULLIF(r.raw_content, ''), r.raw_description, ''), 1, {SOURCE_CHARS}),
               COALESCE(e.summary, e.description, '')
        FROM EnrichedEvents e
        LEFT JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
        WHERE e.status = 'Active'
          AND NOT EXISTS (SELECT 1 FROM PageClassifications p
                          WHERE p.enriched_event_id = e.enriched_event_id
                            AND p.question_version = ?)
          {"AND NOT EXISTS (SELECT 1 FROM EventDeduplicationMap m WHERE m.enriched_event_id = e.enriched_event_id)" if only_unmapped else ""}
        """, (QUESTION_VERSION,)
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


_INSTRUCTIONS = (
    "Classify this scraped web page for an Australian cyber-incident database: does the page "
    "itself report one specific, identifiable cyber incident, or is it something else? Judge "
    "from `page_text`, the page's own scraped text, together with `title` and `url`. "
    "`enrichment_summary` was written by an earlier model and often describes an incident the "
    "page never reports; use it only to understand what was extracted, never as evidence that "
    "the page reports an incident.")


def _state(title: str, url: str, page_text: str, summary: str) -> Dict[str, str]:
    return {"title": title, "url": url, "page_text": page_text or "(no page text stored)",
            "enrichment_summary": summary}


def _side_probability(kind: str, probabilities: Dict[str, float]) -> float:
    """Probability of the chosen side - incident or non-incident - not just the chosen kind.

    The gate asks "is this a non-incident?", so a page split 0.5 guidance /
    0.4 roundup is a confident non-incident (0.9), not an unsure one (0.5).
    """
    non_incident = sum(p for k, p in probabilities.items() if k in NON_INCIDENT_KINDS)
    return non_incident if kind in NON_INCIDENT_KINDS else 1.0 - non_incident


def _classify_with_jev(conn, records, dry_run: bool) -> Dict[str, PageVerdict]:
    from typesafe_sdk import Choice
    from cyber_data_collector.utils.jev import JevJudge

    judge = JevJudge(conn)
    question = Choice(instructions=_INSTRUCTIONS, criteria=KIND_DESCRIPTIONS)
    results: Dict[str, PageVerdict] = {}
    try:
        for eid, title, url, page_text, summary in records:
            try:
                judgement = judge.ask("enriched_event", eid, QUESTION_VERSION,
                                      _state(title, url, page_text, summary), {"page_kind": question})["page_kind"]
            except Exception as exc:  # noqa: BLE001 - never let one page break a run
                logger.warning("Jev page classification failed for %s: %s", eid, exc)
                continue
            confidence = _side_probability(judgement.answer, judgement.probabilities)
            results[eid] = PageVerdict(kind=judgement.answer, confidence=round(confidence, 4),
                                       reasoning="jev: " + json.dumps(judgement.probabilities))
            if not dry_run:
                _store(conn, eid, results[eid], judgement.model)
    finally:
        judge.close()
    return results


def _classify_with_openai(conn, records, client, dry_run: bool) -> Dict[str, PageVerdict]:
    from cyber_data_collector.utils.llm_models import chat_params, openai_fast_model

    model = openai_fast_model()
    criteria = "\n".join(f"- {k}: {v}" for k, v in KIND_DESCRIPTIONS.items())
    results: Dict[str, PageVerdict] = {}
    for eid, title, url, page_text, summary in records:
        try:
            verdict: PageVerdict = client.chat.completions.create(
                **chat_params(model),
                response_model=PageVerdict,
                messages=[
                    {"role": "system", "content": f"{_INSTRUCTIONS}\n\nKinds:\n{criteria}"},
                    {"role": "user", "content": json.dumps(_state(title, url, page_text, summary))},
                ],
            )
        except Exception as exc:  # noqa: BLE001 - never let one page break a run
            logger.warning("Page classification failed for %s: %s", eid, exc)
            continue
        results[eid] = verdict
        if not dry_run:
            _store(conn, eid, verdict, model)
    return results


def _store(conn, eid: str, verdict: PageVerdict, model: Optional[str]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO PageClassifications "
        "(enriched_event_id, kind, confidence, reasoning, model, created_at, question_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (eid, verdict.kind, verdict.confidence, verdict.reasoning, model,
         datetime.now().isoformat(), QUESTION_VERSION))
    conn.commit()


def classify_pages(conn: sqlite3.Connection, records: Sequence[Tuple[str, ...]],
                   client=None, dry_run: bool = False) -> Dict[str, PageVerdict]:
    """Classify suspect records and store the verdicts.

    Jev answers when a TypeSafe key is configured, and its full distribution
    is kept in ``ModelJudgements`` (see :mod:`cyber_data_collector.utils.jev`);
    otherwise the fast OpenAI model does, or ``client`` when one is passed.
    Records are ``(id, title, url, page_text, summary)``; a four-field record
    is read as ``(id, title, url, summary)`` with no page text.

    Failures are skipped (no verdict stored), so a record is retried next run
    and an outage never hardens into a rejection.
    """
    from cyber_data_collector.utils import jev

    ensure_schema(conn)
    records = [r if len(r) == 5 else (r[0], r[1], r[2], "", r[3]) for r in records]
    if client is None and jev.available():
        return _classify_with_jev(conn, records, dry_run)
    client = client or _client()
    if client is None:
        logger.warning("No Jev or OpenAI client; %d suspect page(s) left unclassified", len(records))
        return {}
    return _classify_with_openai(conn, records, client, dry_run)
