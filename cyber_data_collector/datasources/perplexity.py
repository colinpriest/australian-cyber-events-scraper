"""Perplexity discovery: one search per query and date window, one record per cited article.

**Reading the answer.** The answer is JSON ``{"events": [...]}``. When it does
not parse, it is read one incident object at a time (:func:`scan_event_objects`,
:func:`read_event_object`): a field is taken only from inside the object it
belongs to - from a closed object, or, from the cut-off final object of a
truncated answer, only a value that is itself complete. The old reader matched
every ``"title"``, every ``"event_date"`` ... across the whole text and paired
them by position, so one missing field moved every later date and entity onto
the wrong incident.

**Incomplete is not empty** (:class:`SearchOutcome`). An empty answer, an
answer cut off at ``max_tokens`` (``finish_reason == "length"``) or one that was
readable only after repair or object-by-object recovery is not a complete
search: its usable incidents are kept and the search is noted as a failure of
type ``incomplete``, so the source-month is searched again. A truncated answer
is first searched again over the two halves of its date window (recursively,
down to :data:`MIN_WINDOW_DAYS` and :data:`MAX_SPLIT_DEPTH`); only a window that
cannot be split further and is still truncated is kept as read and recorded
incomplete. A clean ``{"events": []}`` is a complete, empty answer.

**One record per cited article** (:meth:`PerplexityDataSource._convert_results_to_events`).
Each article URL an incident cites becomes its own record (the same title,
description and answer fields), so each page is scraped, gated, classified
and deduplicated on its own merits: a citation that is not about the incident
is rejected without taking its siblings down, and deduplication joins the
ones that are. The record's ``source_metadata.provenance`` says where it came
from (see :func:`answer_item_id` and docs/data-dictionary.md).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:  # pragma: no cover - optional dependency
    import instructor
    import openai
except ImportError:  # pragma: no cover
    instructor = None  # type: ignore
    openai = None  # type: ignore

from pydantic import BaseModel, Field

from cyber_data_collector.datasources.base import DataSource
from cyber_data_collector.models.config import DataSourceConfig, DateRange
from cyber_data_collector.models.events import (
    AffectedEntity,
    ConfidenceScore,
    CyberEvent,
    CyberEventType,
    EntityType,
    EventSeverity,
    EventSource,
)
from cyber_data_collector.processing.fetch_errors import INCOMPLETE, PARSE
from cyber_data_collector.utils import RateLimiter
from cyber_data_collector.utils.llm_models import perplexity_model
from cyber_data_collector.utils.token_tracker import tracker
from cyber_data_collector.utils.url_clean import clean_url

#: ``RawEvents.source_metadata.provenance.origin`` of a record made from a Perplexity discovery answer.
PROVENANCE_ORIGIN = "perplexity_answer"
#: Version of the record shape built from an answer item (bumped when the provenance changes meaning).
PROVENANCE_VERSION = "perplexity-children-v1"
#: ``RawEvents.event_date_basis`` of a date the answer gives. The answer is a model's account, not a
#: citation-checked statement, so no precision is recorded for it (``incident_dates`` treats a raw
#: record's precision as established by its source); enrichment verifies the date against the pages.
BASIS_PERPLEXITY_ANSWER = "stated in the Perplexity discovery answer (unverified)"

#: How an incident object was read (``PerplexityEvent.recovery``; ``None`` = from an answer that parsed whole).
RECOVERY_REPAIRED = "repaired"                  # a truncated answer closed after its last complete object
RECOVERY_OBJECT = "object"                      # a closed object read out of an answer that did not parse
RECOVERY_TRUNCATED_OBJECT = "truncated_object"  # the cut-off final object: only its complete values
RECOVERY_COMPLETED = "completed"                # filled in by the completion call from recovered fields

#: A date window is split in two while each half is at least this long ...
MIN_WINDOW_DAYS = 3
#: ... and at most this many times (a 3-month window reaches about 3 days).
MAX_SPLIT_DEPTH = 5


class PerplexityEvent(BaseModel):
    title: str
    description: str
    event_date: Optional[str]
    entity_name: Optional[str]
    event_type: Optional[str]
    impact_description: Optional[str]
    source_urls: List[str] = Field(default_factory=list)
    recovery: Optional[str] = Field(None, description="How the object was read; None = from a whole answer")


class PerplexitySearchResults(BaseModel):
    events: List[PerplexityEvent] = Field(default_factory=list)


@dataclass
class SearchOutcome:
    """What one search returned and whether it is the window's complete answer.

    ``incomplete`` names why it is not (``None`` = complete); ``truncated`` is
    True when the answer was cut off at the token limit, which a smaller date
    window can fix.
    """

    results: PerplexitySearchResults = field(default_factory=PerplexitySearchResults)
    incomplete: Optional[str] = None
    truncated: bool = False
    error_type: str = INCOMPLETE


# ---------------------------------------------------------------------------
# Reading a malformed answer one incident object at a time
# ---------------------------------------------------------------------------

_EVENTS_ARRAY = re.compile(r'"events"\s*:\s*\[')
_DECODER = json.JSONDecoder()


def scan_event_objects(text: str) -> List[Tuple[str, bool]]:
    """``(object_text, closed)`` for each object of the answer's ``events`` array, in order.

    String-aware: braces and brackets inside string values do not count. The
    final object of a cut-off answer is returned with ``closed=False``.
    Returns ``[]`` when no events array (or bare top-level array) is found -
    nothing can then be attributed to an incident.
    """
    match = _EVENTS_ARRAY.search(text or "")
    if match:
        position = match.end()
    elif (text or "").lstrip().startswith("["):
        position = text.index("[") + 1
    else:
        return []
    objects: List[Tuple[str, bool]] = []
    depth, start, in_string, escaped = 0, None, False, False
    for index in range(position, len(text)):
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            if depth == 0 and ch == "{":
                start = index
            depth += 1
        elif ch in "}]":
            if depth == 0:
                if ch == "]":   # the end of the events array
                    return objects
                continue
            depth -= 1
            if depth == 0 and start is not None:
                objects.append((text[start:index + 1], True))
                start = None
    if start is not None:
        objects.append((text[start:], False))
    return objects


def read_event_object(object_text: str, closed: bool) -> Dict[str, Any]:
    """The fields of one incident object that certainly belong to it.

    A closed object that parses gives all its fields. Otherwise its top-level
    ``"key": value`` pairs are read in order, each value only if it decodes
    whole and is followed by the next field or the object's end; reading
    stops at the first value that is not (a cut-off or malformed value, after
    which nothing can be placed). A string or array running to the end of a
    cut-off object is complete (it closed); a number or literal there could
    itself be cut ("12" of "120000") and is not kept.
    """
    if closed:
        try:
            parsed = json.loads(object_text)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    fields: Dict[str, Any] = {}
    position, length = 1, len(object_text)
    while position < length:
        while position < length and object_text[position] in " \t\r\n,":
            position += 1
        if position >= length or object_text[position] != '"':
            break
        try:
            key, position = _DECODER.raw_decode(object_text, position)
        except ValueError:
            break
        while position < length and object_text[position] in " \t\r\n":
            position += 1
        if position >= length or object_text[position] != ":":
            break
        position += 1
        while position < length and object_text[position] in " \t\r\n":
            position += 1
        try:
            value, end = _DECODER.raw_decode(object_text, position)
        except ValueError:
            break
        rest = object_text[end:].lstrip()
        if rest and rest[0] not in ",}":
            # Not followed by the next field or the object's end: the value was malformed (an
            # unescaped quote ends a string early), so neither it nor what follows can be placed.
            break
        if not rest and not isinstance(value, (str, list, dict)):
            break
        fields[key] = value
        position = end
    return fields


def recover_event_objects(text: str) -> List[Dict[str, Any]]:
    """Every incident object of a malformed answer that has a title, with ``recovery`` set."""
    recovered: List[Dict[str, Any]] = []
    for object_text, closed in scan_event_objects(text):
        fields = read_event_object(object_text, closed)
        title = fields.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        fields["recovery"] = RECOVERY_OBJECT if closed else RECOVERY_TRUNCATED_OBJECT
        recovered.append(fields)
    return recovered


def answer_item_id(item: PerplexityEvent) -> str:
    """A stable id of one answer item, shared by the records made from its cited URLs.

    A hash of the item's own fields (URLs cleaned of tracking parameters), so
    the same item in another answer has the same id.
    """
    fields = {
        "title": item.title, "description": item.description, "event_date": item.event_date,
        "entity_name": item.entity_name, "event_type": item.event_type,
        "impact_description": item.impact_description,
        "source_urls": [clean_url((url or "").strip()) for url in item.source_urls],
    }
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _text_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _incident_date(value: Optional[str]) -> Optional[datetime]:
    """The answer's ``event_date`` as an incident date: a valid, not-future ``YYYY-MM-DD`` only.

    A month or a year alone is kept in the provenance, not stored as the 1st.
    """
    from cyber_data_collector.processing.enrichment_gate import validate_iso_date

    if not value:
        return None
    parsed, problem = validate_iso_date(str(value).strip())
    if problem or parsed is None:
        return None
    return datetime(parsed.year, parsed.month, parsed.day)


class PerplexityDataSource(DataSource):
    """Perplexity Search API data source."""

    def __init__(self, config: DataSourceConfig, rate_limiter: RateLimiter, env_config: Dict[str, Optional[str]]):
        super().__init__(config, rate_limiter)
        self.api_key = env_config.get("PERPLEXITY_API_KEY")
        self.client: Optional[instructor.Instructor] = None
        self.openai_client: Optional[openai.OpenAI] = None

        # Retry configuration
        self.max_retries = 3
        self.base_delay = 2.0  # seconds
        self.max_delay = 60.0  # seconds
        self.backoff_multiplier = 2.0

        # Track API health
        self.consecutive_failures = 0
        self.last_success_time = time.time()
        self.circuit_breaker_threshold = 5  # failures before backing off

    def validate_config(self) -> bool:
        if not self.api_key or not instructor or not openai:
            self.logger.error("PERPLEXITY_API_KEY not configured")
            return False

        try:
            # Use OpenAI client directly without instructor for Perplexity
            self.openai_client = openai.OpenAI(
                api_key=self.api_key,
                base_url="https://api.perplexity.ai"
            )
            return True
        except Exception as exc:
            self.logger.error("Failed to initialize Perplexity client: %s", exc)
            return False

    async def collect_events(self, date_range: DateRange) -> List[CyberEvent]:
        if not self.openai_client:
            self.note_failure("Perplexity client not initialized; no queries run")
            return []

        # Check circuit breaker
        if self._should_skip_due_to_circuit_breaker():
            self.note_failure("Perplexity collection skipped by the circuit breaker (too many recent failures)")
            return []

        start, end = self._search_window(date_range)
        base_queries = self._base_queries()
        all_events: List[CyberEvent] = []
        successful_queries = 0
        failed_queries = 0

        for i, base_query in enumerate(base_queries):
            query = self._format_query(base_query, start, end)
            try:
                self.logger.debug(f"Processing Perplexity query {i+1}/{len(base_queries)}: {query[:50]}...")

                answers = await self._search_window_split(base_query, start, end)
                events: List[CyberEvent] = []
                for answered_query, results in answers:
                    events.extend(self._convert_results_to_events(results, query=answered_query))
                all_events.extend(events)

                successful_queries += 1
                self._record_success()

                self.logger.debug(f"Successfully processed query {i+1}, found {len(events)} record(s)")

            except Exception as exc:
                failed_queries += 1
                self._record_failure()
                self.note_failure(f"Perplexity query '{query[:60]}' failed", exc)

                # Log different types of errors appropriately
                if self._is_auth_error(exc):
                    self.logger.error(f"Perplexity authentication failed for query '{query[:50]}...': {exc}")
                    self.logger.error("Please check your PERPLEXITY_API_KEY configuration")
                elif self._is_rate_limit_error(exc):
                    self.logger.warning(f"Perplexity rate limit hit for query '{query[:50]}...': {exc}")
                    # Add extra delay for rate limiting
                    await asyncio.sleep(30)
                elif self._is_network_error(exc):
                    self.logger.warning(f"Perplexity network error for query '{query[:50]}...': {exc}")
                else:
                    self.logger.error(f"Perplexity search failed for query '{query[:50]}...': {exc}")

        self.logger.info(f"Perplexity collection completed: {successful_queries} successful, {failed_queries} failed queries")

        if successful_queries == 0 and failed_queries > 0:
            self.logger.warning("All Perplexity queries failed - check API key and network connectivity")

        return all_events

    _BASE_QUERIES = (
        "Australian cyber attack {date_range} data breach security incident",
        "Australia cybersecurity breach {date_range} ransomware malware",
        "Australian company cyber incident {date_range} hacking data leak",
        "Australia government cyber attack {date_range} infrastructure security",
        "Australian bank financial cyber breach {date_range} fraud security",
        "Australia healthcare cyber attack {date_range} medical data breach",
        "Australian university cyber incident {date_range} education security",
    )

    def _base_queries(self) -> List[str]:
        return list(self._BASE_QUERIES)

    @staticmethod
    def _window_filter(start: datetime, end: Optional[datetime]) -> str:
        """The ``after:/before:`` filter that expresses a date window in a query."""
        date_str = f"after:{start.strftime('%m/%d/%Y')}"
        if end:
            date_str += f" before:{end.strftime('%m/%d/%Y')}"
        return date_str

    def _format_query(self, base_query: str, start: datetime, end: Optional[datetime]) -> str:
        return base_query.format(date_range=self._window_filter(start, end))

    def _search_window(self, date_range: DateRange) -> Tuple[datetime, Optional[datetime]]:
        """``(start, end)`` the queries search for a collection range (see :meth:`_generate_search_queries`)."""
        # We have two competing concerns:
        #  - Late-reported events: an event from January may surface in news in March,
        #    so back-expanding the window catches them when scraping a single old month.
        #  - The user's --days N flag: when the caller is doing a small rolling refresh
        #    (e.g. --days 33), the pipeline already iterates the recent calendar months,
        #    so a per-month +2-month expansion just turns "33 days" into "~90 days" and
        #    causes massive overlap with the months we've already scraped.
        # Compromise: only back-expand if the window we were given is *not* already recent.
        # Threshold: if start_date is within the last 60 days, skip expansion.
        from dateutil.relativedelta import relativedelta
        days_to_today = (datetime.now() - date_range.start_date).days
        if days_to_today <= 60:
            expanded_start = date_range.start_date
            window_label = "recent (no back-expansion - rolling pipeline handles late reporting)"
        else:
            expanded_start = date_range.start_date - relativedelta(months=2)
            window_label = "expanded 3-month window for late-reported events"

        self.logger.info(
            f"Perplexity searching {window_label}: "
            f"{expanded_start.strftime('%Y-%m-%d')} to "
            f"{date_range.end_date.strftime('%Y-%m-%d') if date_range.end_date else 'now'}"
        )
        return expanded_start, date_range.end_date

    def _generate_search_queries(self, date_range: DateRange) -> List[str]:
        start, end = self._search_window(date_range)
        return [self._format_query(query, start, end) for query in self._base_queries()]

    @staticmethod
    def _split(start: datetime, end: Optional[datetime], depth: int) -> Optional[List[Tuple[datetime, datetime]]]:
        """The two halves of a window, or None when it is too short or split too often already.

        The halves share their middle day: Perplexity's ``after:/before:``
        filters are not exact, and an overlap only finds an incident twice
        (stored once: records are keyed on source, URL and title), where a gap
        would lose it.
        """
        end = end or datetime.now()
        if depth >= MAX_SPLIT_DEPTH or (end - start).days < 2 * MIN_WINDOW_DAYS:
            return None
        middle = start + timedelta(days=(end - start).days // 2)
        return [(start, middle), (middle, end)]

    async def _search_window_split(self, base_query: str, start: datetime, end: Optional[datetime],
                                   depth: int = 0) -> List[Tuple[str, PerplexitySearchResults]]:
        """Search one query over a window; a truncated answer is searched again over the window's halves.

        Returns ``(query, results)`` for every answer kept. The halves replace
        the truncated answer; if a half cannot be searched (its error is noted)
        the truncated answer's incidents are kept as well, since they are what
        was read. A window that cannot be split and is still truncated, and any
        other incomplete answer, keeps what was read and is noted as an
        ``incomplete`` failure, so the source-month is searched again.

        Raises:
            Exception: the search of the whole window (depth 0) failed.
        """
        query = self._format_query(base_query, start, end)
        await self.rate_limiter.wait("perplexity")
        outcome = await self._search_with_retry(query, DateRange(start_date=start, end_date=end))
        halves = self._split(start, end, depth) if outcome.truncated else None
        if halves:
            self.logger.info("Perplexity answer for '%s' was cut off (%s); searching the window's halves "
                             "%s and %s", query[:60], outcome.incomplete,
                             *(f"{a:%Y-%m-%d}..{b:%Y-%m-%d}" for a, b in halves))
            answers: List[Tuple[str, PerplexitySearchResults]] = []
            kept_truncated = False
            for half_start, half_end in halves:
                try:
                    answers.extend(await self._search_window_split(base_query, half_start, half_end, depth + 1))
                except Exception as exc:  # noqa: BLE001 - noted; the month is searched again
                    self._record_failure()
                    self.note_failure(f"Perplexity query '{self._format_query(base_query, half_start, half_end)[:80]}'"
                                      " (half of a truncated window) failed", exc)
                    if not kept_truncated:
                        answers.append((query, outcome.results))
                        kept_truncated = True
            return answers
        if outcome.incomplete:
            self.note_failure(f"Perplexity answer for '{query[:80]}' is incomplete ({outcome.incomplete}); "
                              f"{len(outcome.results.events)} incident(s) kept", error_type=outcome.error_type)
        return [(query, outcome.results)]

    async def _search_with_retry(self, query: str, date_range: DateRange) -> SearchOutcome:
        """Search with exponential backoff retry logic."""
        last_exception = None

        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0:
                    # Calculate delay with exponential backoff and jitter
                    delay = min(
                        self.base_delay * (self.backoff_multiplier ** (attempt - 1)),
                        self.max_delay
                    )
                    # Add jitter to prevent thundering herd
                    jitter = delay * 0.1 * (0.5 - asyncio.get_event_loop().time() % 1)
                    total_delay = delay + jitter

                    self.logger.info(f"Retrying Perplexity query in {total_delay:.1f}s (attempt {attempt + 1}/{self.max_retries + 1})")
                    await asyncio.sleep(total_delay)

                return await self._search(query, date_range)

            except Exception as exc:
                last_exception = exc

                # Don't retry on authentication errors - they won't resolve with retries
                if self._is_auth_error(exc):
                    self.logger.error(f"Authentication error on attempt {attempt + 1}, not retrying: {exc}")
                    raise exc

                # Don't retry on client errors (4xx except 429)
                if self._is_client_error(exc) and not self._is_rate_limit_error(exc):
                    self.logger.error(f"Client error on attempt {attempt + 1}, not retrying: {exc}")
                    raise exc

                if attempt < self.max_retries:
                    if self._is_rate_limit_error(exc):
                        self.logger.warning(f"Rate limit hit on attempt {attempt + 1}, will retry with longer delay")
                    elif self._is_server_error(exc):
                        self.logger.warning(f"Server error on attempt {attempt + 1}, will retry: {exc}")
                    elif self._is_network_error(exc):
                        self.logger.warning(f"Network error on attempt {attempt + 1}, will retry: {exc}")
                    else:
                        self.logger.warning(f"Unknown error on attempt {attempt + 1}, will retry: {exc}")

        # All retries exhausted
        self.logger.error(f"All {self.max_retries + 1} attempts failed for query: {query[:50]}...")
        raise last_exception

    _SYSTEM_PROMPT = """You are a cybersecurity analyst. Extract detailed information about Australian cyber security incidents from search results.

        Return your response as a JSON object with this exact structure:
        {
            "events": [
                {
                    "title": "Event title here",
                    "description": "Event description here",
                    "event_date": "YYYY-MM-DD format or null",
                    "entity_name": "Affected entity name or null",
                    "event_type": "Type of cyber event or null",
                    "impact_description": "Description of impact or null",
                    "source_urls": ["url1", "url2"]
                }
            ]
        }

        event_date is the date the incident happened (or was discovered) as the sources state it - not the date an article about it was published. Use null when the sources do not state it.
        source_urls are the addresses of the articles that report this incident (not a site's home page).
        Only include real cyber security incidents related to Australia. If no relevant incidents are found, return {"events": []}.
        """

    async def _ask(self, messages: List[Dict[str, str]], temperature: float,
                   context: str) -> Tuple[Optional[str], Optional[str]]:
        """One chat completion: ``(content, finish_reason)``. Token usage is recorded under ``context``."""
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self.openai_client.chat.completions.create(
                model=perplexity_model(),
                messages=messages,
                temperature=temperature,
                max_tokens=4000,
            )
        )
        usage = getattr(response, "usage", None)
        if usage:
            tracker.record(perplexity_model(), usage.prompt_tokens, usage.completion_tokens, context=context)
        choice = response.choices[0]
        return choice.message.content, getattr(choice, "finish_reason", None)

    def _read_answer(self, content: str, finish_reason: Optional[str]) -> Optional[SearchOutcome]:
        """An answer that parses whole, or closes after its last complete object; None otherwise.

        Hitting the token limit (``finish_reason == "length"``) makes even an
        answer that parses incomplete: what was cut is unknown.
        """
        cut_off = finish_reason == "length"
        cleaned = self._strip_markdown_json(content)
        parsed = self._try_parse_json(cleaned)
        if parsed is not None:
            return SearchOutcome(self._dict_to_search_results(parsed),
                                 incomplete="answer cut off at the token limit" if cut_off else None,
                                 truncated=cut_off)
        fixed = self._try_fix_truncated_json(cleaned)
        if fixed is not None:
            self.logger.info("Repaired a truncated Perplexity answer after its last complete incident")
            return SearchOutcome(self._dict_to_search_results(fixed, recovery=RECOVERY_REPAIRED),
                                 incomplete="truncated answer repaired after its last complete incident",
                                 truncated=True)
        return None

    async def _search(self, query: str, date_range: DateRange) -> SearchOutcome:
        if not self.openai_client:
            raise RuntimeError("Perplexity client not configured")

        messages = [
            {"role": "system", "content": self._SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ]
        content, finish_reason = await self._ask(messages, 0.1, "perplexity_discovery")
        cut_off = finish_reason == "length"
        if not content:
            # Not "no incidents": a validated empty answer is {"events": []}.
            return SearchOutcome(incomplete=f"empty answer (finish_reason {finish_reason!r})", truncated=cut_off)

        # Tier 1: strip markdown code blocks and parse; Tier 2: close a truncated answer after its last
        # complete incident.
        outcome = self._read_answer(content, finish_reason)
        if outcome is not None:
            return outcome
        self.logger.warning("Failed to parse Perplexity response (finish_reason %r). Raw content (first 500 "
                            "chars): %s", finish_reason, content[:500])

        # Tier 3: ask again for raw JSON. Not for a cut-off answer: it would be cut off again, and a
        # smaller window is the remedy.
        if not cut_off:
            self.logger.info("Retrying Perplexity query with strict JSON-only instruction")
            await self.rate_limiter.wait("perplexity")
            retry = await self._retry_strict_json(query, date_range)
            if retry is not None:
                self.logger.info("Strict JSON retry succeeded")
                return retry

        # Tier 4: read the incident objects one at a time.
        partial = self._extract_partial_events(content)
        if partial:
            events = [PerplexityEvent(**self._event_fields(fields)) for fields in partial]
            self.logger.info("Recovered %d incident(s) object by object from a malformed answer", len(events))
            missing_sources = [e for e in events if not e.source_urls]
            if missing_sources and not cut_off:
                # Fill in the citations of objects that lost them; the rest are kept as read.
                await self.rate_limiter.wait("perplexity")
                completed = await self._complete_partial_events(
                    [{k: v for k, v in e.model_dump().items() if v} for e in missing_sources], date_range)
                if completed is not None and completed.events:
                    self.logger.info("Partial event completion succeeded with %d events", len(completed.events))
                    events = [e for e in events if e.source_urls] + [
                        e.model_copy(update={"recovery": RECOVERY_COMPLETED}) for e in completed.events]
            return SearchOutcome(PerplexitySearchResults(events=events),
                                 incomplete=f"answer did not parse; {len(events)} incident(s) recovered object "
                                            f"by object", truncated=cut_off)

        # The answer existed but could not be read: its events are lost, so
        # this is a failure of the search, not an empty result.
        return SearchOutcome(incomplete=f"answer could not be parsed (first 200 chars: {content[:200]!r})",
                             truncated=cut_off, error_type=INCOMPLETE if cut_off else PARSE)

    def _strip_markdown_json(self, content: str) -> str:
        """Strip markdown code block wrappers (```json ... ```) from content."""
        stripped = content.strip()
        if stripped.startswith("```"):
            # Try regex for properly closed code blocks
            match = re.search(r'```(?:json)?\s*\n?(.*?)```', stripped, re.DOTALL)
            if match:
                return match.group(1).strip()
            # No closing ``` (truncated response) — strip the opening line only
            lines = stripped.split("\n", 1)
            return lines[1].strip() if len(lines) > 1 else stripped
        return stripped

    def _try_parse_json(self, content: str) -> Optional[Dict[str, Any]]:
        """Try to parse JSON string, return None on failure."""
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
        return None

    def _try_fix_truncated_json(self, json_str: str) -> Optional[Dict[str, Any]]:
        """Close a truncated answer after its last complete incident object.

        This handles cases where max_tokens cuts off the response mid-JSON.
        Only cuts that leave whole objects are tried: closing every open
        bracket (the old third strategy) also "completed" the cut-off final
        object, keeping a value cut mid-number. That object's complete values
        are read by :func:`recover_event_objects` instead.
        """
        # Strategy 1: Find last complete event object boundary and close
        last_event_end = json_str.rfind('},')
        if last_event_end > 0:
            truncated = json_str[:last_event_end + 1]
            for suffix in [']}', '\n]\n}']:
                result = self._try_parse_json(truncated + suffix)
                if result is not None and 'events' in result:
                    return result

        # Strategy 2: Find last } and try closing the outer structure
        last_brace = json_str.rfind('}')
        if last_brace > 0:
            truncated = json_str[:last_brace + 1]
            for suffix in [']}', '']:
                result = self._try_parse_json(truncated + suffix)
                if result is not None and 'events' in result:
                    return result

        return None

    @staticmethod
    def _event_fields(event: Dict[str, Any]) -> Dict[str, Any]:
        """The :class:`PerplexityEvent` fields of one answer object (strings as strings, URLs as a list)."""
        urls = event.get("source_urls")
        if isinstance(urls, str):
            urls = [urls]
        return {
            "title": str(event.get("title") or ""),
            "description": str(event.get("description") or ""),
            "event_date": _text_or_none(event.get("event_date")),
            "entity_name": _text_or_none(event.get("entity_name")),
            "event_type": _text_or_none(event.get("event_type")),
            "impact_description": _text_or_none(event.get("impact_description")),
            "source_urls": [str(url) for url in (urls or []) if isinstance(url, str)],
            "recovery": event.get("recovery"),
        }

    def _dict_to_search_results(self, data: Dict[str, Any], recovery: Optional[str] = None) -> PerplexitySearchResults:
        """Convert a parsed JSON dict to PerplexitySearchResults."""
        events = [
            PerplexityEvent(**{**self._event_fields(event), "recovery": recovery or event.get("recovery")})
            for event in data.get("events", []) or []
            if isinstance(event, dict) and event.get("title")  # skip entries without a title
        ]
        return PerplexitySearchResults(events=events)

    async def _retry_strict_json(self, query: str, date_range: DateRange) -> Optional[SearchOutcome]:
        """Retry the query with explicit instruction to return raw JSON only.

        Returns the retry's outcome (complete only if it parsed whole and was
        not cut off), or None when the retry failed or could not be read; the
        failure is logged and the caller goes on to recover the first answer.
        """
        if not self.openai_client:
            return None

        system_prompt = (
            "You are a cybersecurity analyst. Extract detailed information about "
            "Australian cyber security incidents from search results.\n\n"
            "CRITICAL: Return ONLY raw JSON. Do NOT wrap it in markdown code blocks. "
            "Do NOT use ```json or ``` delimiters. Return the JSON object directly.\n\n"
            "Return this exact JSON structure:\n"
            '{"events": [{"title": "...", "description": "...", '
            '"event_date": "YYYY-MM-DD or null", "entity_name": "... or null", '
            '"event_type": "... or null", "impact_description": "... or null", '
            '"source_urls": ["url1"]}]}\n\n'
            "event_date is the date the incident happened as the sources state it, not an "
            "article's publication date (null if not stated). "
            "Only include real cyber security incidents related to Australia. "
            'If no relevant incidents are found, return: {"events": []}'
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query},
        ]

        try:
            content, finish_reason = await self._ask(messages, 0.0, "perplexity_strict_json")
            if not content:
                return None
            # Still strip markdown in case the model ignores the instruction
            return self._read_answer(content, finish_reason)
        except Exception as exc:
            self.logger.warning("Strict JSON retry failed with error: %s", exc)

        return None

    def _extract_partial_events(self, content: str) -> List[Dict[str, Any]]:
        """The incident objects recoverable from a malformed answer, each read from inside itself.

        See :func:`recover_event_objects`: a field is never taken from another
        incident's object, so a missing field leaves a gap instead of moving
        every later field onto the wrong incident.
        """
        return recover_event_objects(self._strip_markdown_json(content))

    async def _complete_partial_events(
        self,
        partial_events: List[Dict[str, Any]],
        date_range: DateRange,
    ) -> Optional[PerplexitySearchResults]:
        """Send partial event data to Perplexity and ask it to complete the details."""
        if not self.openai_client or not partial_events:
            return None

        # Build a summary of the partial events we extracted
        event_summaries = []
        for i, event in enumerate(partial_events[:10], 1):  # cap at 10
            parts = [f"{i}. Title: {event.get('title', 'Unknown')}"]
            if event.get("description"):
                parts.append(f"   Description: {str(event['description'])[:200]}")
            if event.get("entity_name"):
                parts.append(f"   Entity: {event['entity_name']}")
            if event.get("event_date"):
                parts.append(f"   Date: {event['event_date']}")
            event_summaries.append("\n".join(parts))

        events_text = "\n\n".join(event_summaries)

        system_prompt = (
            "You are a cybersecurity analyst. I have partial data about Australian "
            "cyber security incidents that needs to be completed.\n\n"
            "CRITICAL: Return ONLY raw JSON. Do NOT wrap it in markdown code blocks. "
            "Do NOT use ```json or ``` delimiters. Return the JSON object directly.\n\n"
            "Return this exact JSON structure:\n"
            '{"events": [{"title": "...", "description": "...", '
            '"event_date": "YYYY-MM-DD or null", "entity_name": "... or null", '
            '"event_type": "... or null", "impact_description": "... or null", '
            '"source_urls": ["url1"]}]}\n\n'
            "For each event below, verify the details and fill in any missing fields "
            "(event_type, impact_description, source_urls). "
            "Correct any obviously wrong information."
        )

        user_prompt = (
            f"Complete and verify these Australian cyber incidents:\n\n{events_text}"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        try:
            content, finish_reason = await self._ask(messages, 0.0, "perplexity_partial_complete")
            if not content:
                return None
            outcome = self._read_answer(content, finish_reason)
            if outcome is not None:
                return outcome.results
        except Exception as exc:
            self.logger.warning("Partial event completion failed: %s", exc)

        return None

    @staticmethod
    def _is_article_url(url: str) -> bool:
        """An http(s) URL with a path. The model writes ``source_urls`` itself, and a bare site address
        ("https://cybertrackerau.com/") is a home page: scraped, it became the record's "article" and its
        text was judged as the incident (26 live records). Such a URL is not a source for one incident."""
        parsed = urlparse((url or "").strip())
        return parsed.scheme in ("http", "https") and bool(parsed.netloc) and any(
            segment for segment in parsed.path.split("/"))

    def _convert_results_to_events(self, results: PerplexitySearchResults,
                                   query: Optional[str] = None) -> List[CyberEvent]:
        """One record per article URL an incident cites, each carrying the answer item's provenance.

        A Perplexity answer item is the model's account of one incident with
        the articles it cites. Each article becomes its own record - the same
        title, description and answer fields, its own URL - so its page is
        scraped and judged on its own; an item that cites no article address
        becomes one record with no URL. ``source_provenance`` (stored as
        ``RawEvents.source_metadata.provenance``) names the answer item every
        sibling shares (:func:`answer_item_id`), the query, the URL's place
        among the siblings, the URLs that are not article addresses and the
        answer's own fields.
        """
        events: List[CyberEvent] = []
        for item in results.events:
            try:
                article_urls: List[str] = []
                skipped: List[str] = []
                for raw_url in item.source_urls:
                    url = (raw_url or "").strip()
                    if not self._is_article_url(url):
                        skipped.append(url)
                        continue
                    url = clean_url(url) or url
                    if url not in article_urls:
                        article_urls.append(url)
                if skipped:
                    self.logger.info("Perplexity event %r: %d source URL(s) are not article addresses and are not "
                                     "stored as its source: %s", item.title[:60], len(skipped), ", ".join(skipped[:3]))

                item_id = answer_item_id(item)
                event_date = _incident_date(item.event_date)
                answer = item.model_dump(exclude={"recovery"})
                entity = None
                if item.entity_name:
                    # A hint for entity extraction, which replaces it. The answer does not say whether the
                    # organisation itself is Australian (an incident can reach Australians through a foreign one).
                    entity = AffectedEntity(name=item.entity_name, entity_type=EntityType.OTHER,
                                            australian_entity=False, confidence_score=0.6)

                for index, url in enumerate(article_urls or [None]):
                    provenance = {
                        "origin": PROVENANCE_ORIGIN,
                        "version": PROVENANCE_VERSION,
                        "query": query,
                        "answer_item_id": item_id,
                        "url_index": index if url else None,
                        "url_count": len(article_urls),
                        "sibling_urls": article_urls,
                        "skipped_urls": skipped,
                        "recovery": item.recovery,
                        "answer": answer,
                    }
                    data_source = EventSource(
                        source_id=f"perplexity_{hashlib.md5((url or item_id).encode()).hexdigest()[:16]}",
                        source_type="Perplexity Result",
                        url=url,
                        title=item.title,
                        content_snippet=item.description or None,
                        credibility_score=0.6,
                        relevance_score=0.7,
                    )
                    confidence = ConfidenceScore(
                        overall=0.65,
                        source_reliability=0.6,
                        data_completeness=0.6,
                        temporal_accuracy=0.6,
                        geographic_accuracy=0.7,
                    )
                    events.append(
                        CyberEvent(
                            external_ids={"perplexity_answer_item": item_id},
                            title=item.title,
                            description=item.description,
                            event_type=self._infer_event_type(item.event_type),
                            severity=EventSeverity.MEDIUM,
                            event_date=event_date,
                            event_date_precision=None,
                            event_date_basis=BASIS_PERPLEXITY_ANSWER if event_date else None,
                            primary_entity=entity,
                            affected_entities=[entity] if entity else [],
                            australian_relevance=True,
                            data_sources=[data_source],
                            confidence=confidence,
                            source_provenance=provenance,
                        )
                    )
            except Exception as exc:
                # A converter error is a failure that loses an incident, not a skipped item.
                self.note_failure(f"Perplexity incident {item.title[:60]!r} could not be converted", exc)
        return events

    def _infer_event_type(self, event_type: Optional[str]) -> CyberEventType:
        if not event_type:
            return CyberEventType.OTHER

        value = event_type.lower()
        if "ransomware" in value:
            return CyberEventType.RANSOMWARE
        if "breach" in value or "data" in value:
            return CyberEventType.DATA_BREACH
        if "phish" in value:
            return CyberEventType.PHISHING
        return CyberEventType.OTHER

    def get_source_info(self) -> Dict[str, Any]:
        return {
            "name": "Perplexity Search API",
            "description": "AI-powered web search with real-time information",
            "update_frequency": "Real-time",
            "coverage": "Global web content with Australian focus",
            "data_types": ["Web search results", "News articles", "Reports"],
        }

    def _is_auth_error(self, exc: Exception) -> bool:
        """Check if the error is an authentication/authorization error."""
        error_str = str(exc).lower()
        return (
            "401" in error_str or
            "403" in error_str or
            "authorization" in error_str or
            "unauthorized" in error_str or
            "forbidden" in error_str or
            "invalid api key" in error_str or
            "api key" in error_str
        )

    def _is_rate_limit_error(self, exc: Exception) -> bool:
        """Check if the error is a rate limiting error."""
        error_str = str(exc).lower()
        return (
            "429" in error_str or
            "rate limit" in error_str or
            "too many requests" in error_str or
            "quota exceeded" in error_str
        )

    def _is_server_error(self, exc: Exception) -> bool:
        """Check if the error is a server-side error (5xx)."""
        error_str = str(exc).lower()
        return (
            "500" in error_str or
            "502" in error_str or
            "503" in error_str or
            "504" in error_str or
            "internal server error" in error_str or
            "bad gateway" in error_str or
            "service unavailable" in error_str or
            "gateway timeout" in error_str
        )

    def _is_client_error(self, exc: Exception) -> bool:
        """Check if the error is a client-side error (4xx)."""
        error_str = str(exc).lower()
        return (
            "400" in error_str or
            "401" in error_str or
            "403" in error_str or
            "404" in error_str or
            "405" in error_str or
            "408" in error_str or
            "409" in error_str or
            "410" in error_str or
            "422" in error_str or
            "429" in error_str or
            "bad request" in error_str or
            "not found" in error_str or
            "method not allowed" in error_str
        )

    def _is_network_error(self, exc: Exception) -> bool:
        """Check if the error is a network/connectivity error."""
        error_str = str(exc).lower()
        exc_type = type(exc).__name__.lower()
        return (
            "timeout" in error_str or
            "connection" in error_str or
            "network" in error_str or
            "dns" in error_str or
            "resolve" in error_str or
            exc_type in ["connectionerror", "timeout", "connecttimeout", "readtimeout"]
        )

    def _should_skip_due_to_circuit_breaker(self) -> bool:
        """Check if we should skip API calls due to circuit breaker."""
        if self.consecutive_failures >= self.circuit_breaker_threshold:
            # Back off for 5 minutes after circuit breaker threshold
            time_since_last_success = time.time() - self.last_success_time
            return time_since_last_success < 300  # 5 minutes
        return False

    def _record_success(self) -> None:
        """Record a successful API call."""
        self.consecutive_failures = 0
        self.last_success_time = time.time()

    def _record_failure(self) -> None:
        """Record a failed API call."""
        self.consecutive_failures += 1
