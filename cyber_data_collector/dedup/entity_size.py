"""Ordinal size estimate for every entity: SMALL, MEDIUM, LARGE, HUGE, UNKNOWN.

**Why an ordinal.** ``EntitiesV2`` has carried ``employee_count`` and
``turnover`` since v1 and both are NULL on all 3,133 rows - nothing ever
populated them. Every analysis that asked how big the victim was therefore ran
with the answer unknown; ``scripts/analyze_recurrent_timing.py`` requests
``employee_size`` and ``turnover_size`` as covariates and gets a single
"unknown" level for the entire dataset. An exact headcount is unobtainable for
most of these organisations - private companies do not publish one, and neither
do most government bodies in a comparable form - but the *band* is nearly always
recoverable, and a band is what the analysis needs.

**Why the bands are these bands.** The three-way cut follows the ABS definition
of business size (small 0-19 employees, medium 20-199, large 200+), which is the
standard an Australian dataset should be readable against. LARGE is then split
at 5,000 because that band otherwise runs from a suburban accounting firm's
larger cousin all the way to Telstra, and the distinction between a 300-person
company and a 50,000-person one is exactly what a cyber-incident analysis turns
on. Employee count is the primary axis and revenue the tiebreaker, because
government departments and universities have headcounts but no turnover in the
commercial sense.

**Why Perplexity.** The band depends on facts about the real organisation, not
on anything in the event text - the description of a breach rarely says how big
the victim is. An LLM asked from memory will confidently size an organisation it
has never heard of, and this dataset is full of small Australian businesses that
fall exactly into that gap. Perplexity looks the organisation up first and the
band is derived from what it finds, with the citations kept so an estimate can
be traced back to its evidence.

UNKNOWN is a real answer, not a failure code: a product ("Canvas"), a person, a
ransomware group and a collective noun ("Australian hospitals") have no size,
and neither does an organisation that cannot be identified. Recording UNKNOWN is
correct in all of those cases and is strictly better than a guess, because a
guessed band is indistinguishable from a researched one once it is in the
database.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

from cyber_data_collector.dedup.victim_selection import (
    is_non_organisation,
    is_threat_actor,
)

logger = logging.getLogger(__name__)

RESEARCH_MODEL = "sonar-pro"
STRUCTURING_MODEL = "gpt-4o"

# Entity kinds that have no meaningful organisational size. Decided by rule so
# no API call is spent on them - roughly a third of the table.
UNSIZEABLE_KINDS = {"product", "person", "threat_actor", "other"}


class EntitySize(str, Enum):
    """Ordinal size band for an organisation."""

    SMALL = "SMALL"
    MEDIUM = "MEDIUM"
    LARGE = "LARGE"
    HUGE = "HUGE"
    UNKNOWN = "UNKNOWN"

    @property
    def rank(self) -> int:
        """Sort order, with UNKNOWN last rather than in the middle."""
        return {
            EntitySize.SMALL: 0, EntitySize.MEDIUM: 1,
            EntitySize.LARGE: 2, EntitySize.HUGE: 3, EntitySize.UNKNOWN: 4,
        }[self]


# The band definitions, kept in one place so the prompt, the fallback
# derivation and the documentation cannot drift apart.
BAND_DEFINITIONS: Dict[EntitySize, str] = {
    EntitySize.SMALL:
        "fewer than 20 employees, or under A$10 million annual revenue",
    EntitySize.MEDIUM:
        "20 to 199 employees, or A$10 million to A$100 million annual revenue",
    EntitySize.LARGE:
        "200 to 4,999 employees, or A$100 million to A$1 billion annual revenue",
    EntitySize.HUGE:
        "5,000 or more employees, or over A$1 billion annual revenue; also "
        "national and state government departments, the major universities, "
        "and ASX-100 companies",
    EntitySize.UNKNOWN:
        "the organisation cannot be identified, or is not an organisation",
}

EMPLOYEE_BOUNDS = ((20, EntitySize.SMALL), (200, EntitySize.MEDIUM),
                   (5_000, EntitySize.LARGE))
REVENUE_BOUNDS = ((10_000_000, EntitySize.SMALL),
                  (100_000_000, EntitySize.MEDIUM),
                  (1_000_000_000, EntitySize.LARGE))


def band_from_employees(count: Optional[float]) -> Optional[EntitySize]:
    """The band a headcount falls in, or None when there is no headcount."""
    if count is None or count < 0:
        return None
    for bound, band in EMPLOYEE_BOUNDS:
        if count < bound:
            return band
    return EntitySize.HUGE


def band_from_revenue(amount: Optional[float]) -> Optional[EntitySize]:
    """The band an annual revenue figure falls in, or None."""
    if amount is None or amount < 0:
        return None
    for bound, band in REVENUE_BOUNDS:
        if amount < bound:
            return band
    return EntitySize.HUGE


def rule_size(name: Optional[str], entity_kind: Optional[str]) -> Optional[EntitySize]:
    """UNKNOWN where that is certain without research, else None.

    Only decides the cases where research could not help: things that are not
    organisations at all. Everything else is left to :class:`EntitySizeResearcher`,
    which can actually look the organisation up.
    """
    text = (name or "").strip()
    if not text:
        return EntitySize.UNKNOWN
    if (entity_kind or "").strip().lower() in UNSIZEABLE_KINDS:
        return EntitySize.UNKNOWN
    # A collective noun ("Australian hospitals", "the education sector") names a
    # group rather than an organisation, so it has no headcount of its own.
    if is_non_organisation(text) or is_threat_actor(text):
        return EntitySize.UNKNOWN
    return None


class SizeEstimate(BaseModel):
    """Structured reading of what the research found about one organisation."""

    size: EntitySize = Field(
        description="The single band that best fits the evidence. Use UNKNOWN "
                    "when the organisation could not be identified."
    )
    employees: Optional[int] = Field(
        default=None,
        description="Best point estimate of headcount, if the evidence gives "
                    "one. Use the midpoint of a stated range. Null otherwise."
    )
    revenue_aud: Optional[float] = Field(
        default=None,
        description="Approximate annual revenue, turnover or budget in "
                    "Australian dollars. Convert other currencies. Null if not "
                    "stated."
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="0.0-1.0 confidence in the band. Below 0.5 when the "
                    "evidence names no figures, or when several different "
                    "organisations share the name."
    )
    basis: str = Field(
        description="One or two sentences naming the decisive facts: the "
                    "headcount or revenue found and where it came from."
    )


def _derive_band(estimate: SizeEstimate) -> EntitySize:
    """Reconcile the stated band with the figures the same answer reported.

    The model occasionally reports "about 45,000 staff" and then labels the
    organisation LARGE. Where a figure is present it decides the band, because
    the figure is the evidence and the label is an inference from it. The
    thresholds live in one place (``EMPLOYEE_BOUNDS``) so this cannot disagree
    with what the prompt asked for.
    """
    derived = band_from_employees(estimate.employees)
    if derived is None:
        derived = band_from_revenue(estimate.revenue_aud)
    if derived is None or derived == estimate.size:
        return estimate.size
    # A stated UNKNOWN alongside a real figure means the figure was found but
    # the organisation was not confidently identified; trust the caution.
    if estimate.size == EntitySize.UNKNOWN:
        return EntitySize.UNKNOWN
    logger.debug("Band corrected %s -> %s from reported figures",
                 estimate.size.value, derived.value)
    return derived


RESEARCH_PROMPT = """Look up the organisation "{name}" online and estimate how big it is.

{context}
Use current public sources - the organisation's own website, annual reports,
LinkedIn, ABN Lookup or ASIC records, Wikipedia, news coverage - and report:

1. Approximate number of employees (a number or a range).
2. Approximate annual revenue, turnover or budget, naming the currency.
3. What the organisation does and where it operates.
4. Whether several different organisations share this name, and if so which one
   this is most likely to be.

Then place it in exactly one of these size bands:
{bands}

State the band, the figures you based it on, and how confident you are. If you
cannot identify the organisation, answer UNKNOWN - do not guess from the name.
"""

STRUCTURING_SYSTEM_PROMPT = (
    "You convert research notes about an organisation into a size band.\n\n"
    "Bands:\n{bands}\n\n"
    "Employee count decides the band; revenue is the tiebreaker when no "
    "headcount is given. Government departments and universities are sized by "
    "headcount, not revenue.\n\n"
    "Report only what the notes support. If the notes say the organisation "
    "could not be identified, or describe several different organisations "
    "sharing the name without settling which one is meant, answer UNKNOWN with "
    "low confidence. Never infer size from the name alone."
)


def _band_listing() -> str:
    return "\n".join(f"{band.value:<8}- {text}"
                     for band, text in BAND_DEFINITIONS.items())


class EntitySizeResearcher:
    """Establishes an organisation's size band by looking it up online.

    Two steps, matching :class:`~cyber_data_collector.dedup.date_selection.DateResearcher`:
    Perplexity does the research and proposes a band, then GPT-4o turns that
    prose into the structured record that gets stored. Splitting them keeps the
    web lookup and the schema-conformance concerns apart - a search answer that
    is right but unparseable should not become an UNKNOWN.

    Never raises. Any failure returns UNKNOWN with method ``unavailable``, which
    a later run retries rather than treating as a settled answer.
    """

    def __init__(
        self,
        perplexity_key: Optional[str] = None,
        openai_client=None,
        model: str = STRUCTURING_MODEL,
        research_model: str = RESEARCH_MODEL,
    ):
        self.perplexity_key = perplexity_key or os.getenv("PERPLEXITY_API_KEY")
        self.model = model
        self.research_model = research_model
        self._client = openai_client
        self._client_ready = openai_client is not None
        self._lock = threading.Lock()
        self.stats: Dict[str, int] = {
            "researched": 0, "search_calls": 0, "search_failures": 0,
            "llm_calls": 0, "llm_failures": 0, "unavailable": 0,
        }

    def _get_client(self):
        with self._lock:
            if self._client_ready:
                return self._client
            self._client_ready = True
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                logger.warning("OPENAI_API_KEY not set; size estimation disabled")
                self._client = None
                return None
            try:
                import instructor
                from openai import OpenAI

                self._client = instructor.from_openai(OpenAI(api_key=api_key))
            except ImportError as exc:  # pragma: no cover - dependency issue
                logger.warning("instructor/openai unavailable (%s)", exc)
                self._client = None
            return self._client

    def _bump(self, key: str) -> None:
        with self._lock:
            self.stats[key] = self.stats.get(key, 0) + 1

    def search(self, name: str, context: str = "") -> Optional[Dict[str, Any]]:
        """Perplexity's researched answer for one organisation.

        Returns ``{"text": ..., "citations": [...]}`` or None. The citations are
        kept so a stored band can be traced back to the pages it came from.
        """
        if not self.perplexity_key:
            return None
        prompt = RESEARCH_PROMPT.format(
            name=name,
            context=(context + "\n") if context else "",
            bands=_band_listing(),
        )
        try:
            import requests

            self._bump("search_calls")
            response = requests.post(
                "https://api.perplexity.ai/chat/completions",
                headers={"Authorization": f"Bearer {self.perplexity_key}",
                         "Content-Type": "application/json"},
                json={"model": self.research_model,
                      "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 700, "temperature": 0.0},
                timeout=90)
            if response.status_code != 200:
                self._bump("search_failures")
                logger.warning("Perplexity returned %s for %r",
                               response.status_code, name)
                return None
            payload = response.json()
            return {
                "text": payload["choices"][0]["message"]["content"],
                "citations": payload.get("citations") or [],
            }
        except Exception as exc:  # noqa: BLE001 - one entity must not stop a run
            self._bump("search_failures")
            logger.warning("Perplexity lookup failed for %r: %s", name, exc)
            return None

    def estimate(
        self,
        name: str,
        industry: Optional[str] = None,
        is_australian: Optional[bool] = None,
        entity_kind: Optional[str] = None,
        incident_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Size band for one organisation, with its evidence.

        The extra arguments are disambiguation context, not inputs to the size:
        several organisations share a name, and knowing that this one is an
        Australian healthcare provider is what makes the lookup find the right
        one.
        """
        self._bump("researched")
        ruled = rule_size(name, entity_kind)
        if ruled is not None:
            return {
                "size": ruled, "employees": None, "revenue_aud": None,
                "confidence": 0.9, "method": "rule", "sources": [],
                "basis": f"Not a sizeable organisation ({entity_kind or 'unnamed'}).",
            }

        context_bits: List[str] = []
        if is_australian:
            context_bits.append("It is an Australian organisation.")
        if industry:
            context_bits.append(f"It operates in: {industry}.")
        if entity_kind == "government_body":
            context_bits.append("It is a government body.")
        if incident_hint:
            context_bits.append(
                f"It was named in this cyber security incident, which may help "
                f"identify it: {incident_hint[:200]}")
        evidence = self.search(name, " ".join(context_bits))

        client = self._get_client()
        if evidence is None or client is None:
            self._bump("unavailable")
            return {
                "size": EntitySize.UNKNOWN, "employees": None,
                "revenue_aud": None, "confidence": 0.0,
                "method": "unavailable", "sources": [],
                "basis": ("No research available (Perplexity or OpenAI "
                          "unreachable); not yet estimated."),
            }

        try:
            self._bump("llm_calls")
            estimate: SizeEstimate = client.chat.completions.create(
                model=self.model,
                response_model=SizeEstimate,
                temperature=0.0,
                messages=[
                    {"role": "system",
                     "content": STRUCTURING_SYSTEM_PROMPT.format(
                         bands=_band_listing())},
                    {"role": "user",
                     "content": (f"Organisation: {name}\n"
                                 + (" ".join(context_bits) + "\n" if context_bits else "")
                                 + f"\nResearch notes:\n{evidence['text'][:4000]}")},
                ])
        except Exception as exc:  # noqa: BLE001 - one entity must not stop a run
            self._bump("llm_failures")
            logger.warning("Size structuring failed for %r: %s", name, exc)
            return {
                "size": EntitySize.UNKNOWN, "employees": None,
                "revenue_aud": None, "confidence": 0.0,
                "method": "unavailable", "sources": evidence["citations"],
                "basis": f"Research retrieved but could not be structured: {exc}",
            }

        return {
            "size": _derive_band(estimate),
            "employees": estimate.employees,
            "revenue_aud": estimate.revenue_aud,
            "confidence": estimate.confidence,
            "method": "perplexity",
            "sources": evidence["citations"],
            "basis": estimate.basis,
        }


# ----------------------------------------------------------------------
# Applying estimates to the stored entities
# ----------------------------------------------------------------------

def normalise_name(name: Optional[str]) -> str:
    """Key for reusing one lookup across spelling variants of a name.

    "Optus", "Optus Pty Ltd" and "Singtel Optus Pty Limited" are the same
    organisation and the same size; researching each separately would cost three
    lookups to reach one answer.
    """
    import re

    text = (name or "").strip().lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    suffixes = {"pty", "ltd", "limited", "inc", "incorporated", "llc", "plc",
                "corp", "corporation", "group", "holdings", "australia",
                "australian", "the", "co", "company"}
    tokens = [t for t in text.split() if t and t not in suffixes]
    return " ".join(tokens) or text.strip()


def pending_entities(
    conn,
    refresh: bool = False,
    entity_filter: Optional[str] = None,
    linked_only: bool = False,
    limit: Optional[int] = None,
) -> List[Any]:
    """Entities still needing a size estimate.

    ``unavailable`` rows are selected again on every run by design: they record
    that research could not be reached, not that the answer is UNKNOWN, so a
    later run with a working key fills them in without ``--refresh``.
    """
    where = ["v.entity_name IS NOT NULL", "TRIM(v.entity_name) != ''"]
    params: List[Any] = []
    if not refresh:
        where.append("(v.size_estimate IS NULL OR v.size_method = 'unavailable')")
    # A human ruling is never overwritten by an automated pass.
    where.append("COALESCE(v.size_method, '') != 'human'")
    if entity_filter:
        where.append("v.entity_name LIKE ?")
        params.append(f"%{entity_filter}%")
    if linked_only:
        where.append(
            "EXISTS (SELECT 1 FROM DeduplicatedEventEntities dee "
            "JOIN DeduplicatedEvents d "
            "  ON d.deduplicated_event_id = dee.deduplicated_event_id "
            "WHERE dee.entity_id = v.entity_id "
            "  AND COALESCE(d.status,'Active') = 'Active')")

    query = f"""
        SELECT v.entity_id, v.entity_name, v.industry, v.is_australian,
               v.entity_kind,
               (SELECT d.title FROM DeduplicatedEventEntities dee
                JOIN DeduplicatedEvents d
                     ON d.deduplicated_event_id = dee.deduplicated_event_id
                WHERE dee.entity_id = v.entity_id
                  AND COALESCE(d.status,'Active') = 'Active'
                LIMIT 1) AS incident_hint
        FROM EntitiesV2 v
        WHERE {' AND '.join(where)}
        ORDER BY v.entity_name COLLATE NOCASE
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    return conn.execute(query, params).fetchall()


def store_estimate(conn, entity_id: int, result: Dict[str, Any]) -> None:
    """Persist one estimate, with the evidence it rests on."""
    conn.execute(
        """
        UPDATE EntitiesV2
           SET size_estimate = ?, size_confidence = ?, size_employees = ?,
               size_revenue_aud = ?, size_basis = ?, size_method = ?,
               size_sources = ?, size_estimated_at = ?,
               employee_count = COALESCE(?, employee_count),
               updated_at = CURRENT_TIMESTAMP
         WHERE entity_id = ?
        """,
        (
            result["size"].value if isinstance(result["size"], EntitySize)
            else str(result["size"]),
            result.get("confidence"),
            result.get("employees"),
            result.get("revenue_aud"),
            result.get("basis"),
            result.get("method"),
            json.dumps(result.get("sources") or [])[:4000],
            datetime.now().isoformat(timespec="seconds"),
            result.get("employees"),
            entity_id,
        ),
    )


def estimate_sizes(
    conn,
    rows: Sequence[Any],
    researcher: Optional[EntitySizeResearcher] = None,
    workers: int = 6,
    dry_run: bool = False,
    progress=None,
) -> Dict[str, Any]:
    """Estimate and store a size band for every entity in ``rows``.

    Lookups are shared between spelling variants of one organisation, so a table
    holding "Optus", "Optus Pty Ltd" and "Singtel Optus Pty Limited" costs one
    search rather than three. Writes happen on the calling thread - SQLite
    connections are not shareable across threads - while the network work fans
    out.
    """
    researcher = researcher or EntitySizeResearcher()
    by_key: Dict[str, List[Any]] = {}
    for row in rows:
        by_key.setdefault(normalise_name(row["entity_name"]), []).append(row)

    counts: Dict[str, int] = {}
    stored = 0
    reused = 0
    committed = 0

    def work(key: str) -> tuple:
        representative = by_key[key][0]
        return key, researcher.estimate(
            name=representative["entity_name"],
            industry=representative["industry"],
            is_australian=bool(representative["is_australian"]),
            entity_kind=representative["entity_kind"],
            incident_hint=representative["incident_hint"],
        )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(work, key): key for key in by_key}
        for done in as_completed(futures):
            try:
                key, result = done.result()
            except Exception as exc:  # noqa: BLE001 - never abort the batch
                logger.warning("Size estimation failed for %r: %s",
                               futures[done], exc)
                continue
            band = result["size"]
            label = band.value if isinstance(band, EntitySize) else str(band)
            counts[label] = counts.get(label, 0) + len(by_key[key])
            for row in by_key[key]:
                if progress:
                    # Reporting must never cost the batch its results: an
                    # unprintable character in researched text once aborted a
                    # backfill and discarded every lookup it had paid for.
                    try:
                        progress(row["entity_name"], result)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Progress callback failed: %s", exc)
                        progress = None
                if dry_run:
                    continue
                store_estimate(conn, row["entity_id"], result)
                stored += 1
            reused += len(by_key[key]) - 1
            # Commit on elapsed progress, not on `stored % 25 == 0`: one key can
            # cover several rows, so `stored` steps over the multiples and the
            # condition almost never fires. A full backfill is over an hour of
            # paid lookups, and an uncommitted crash loses all of it.
            if not dry_run and stored - committed >= 25:
                conn.commit()
                committed = stored

    if not dry_run:
        conn.commit()
    return {
        "entities": len(rows),
        "lookups": len(by_key),
        "reused": reused,
        "stored": stored,
        "bands": counts,
        "researcher_stats": dict(researcher.stats),
    }
