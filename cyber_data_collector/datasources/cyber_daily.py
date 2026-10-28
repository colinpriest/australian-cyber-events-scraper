"""Cyber Daily (cyberdaily.au, Momentum Media) section feeds.

Cyber Daily reports Australian incidents that no other source carries, most of
them ransomware leak-site claims against small and medium organisations
("Exclusive: Qilin claims breach of Victoria-based flooring firm"). Measured on
2026-10-28: of 1,001 articles in its Security section over the previous 12
months, 68 were already in the database (through Webber Insurance, Google or
Perplexity); a seeded sample of the rest put roughly 80 further Australian
incidents a year outside it, against 146 active events in the same window.

Discovery reads the section's public RSS feed (Joomla ``?format=feed&type=rss``),
which gives each article's title, standfirst, publication date and URL - 13
items a page, newest first, paged with ``limitstart`` back to 2018. The site's
terms name RSS as a way of accessing it and robots.txt does not exclude the
sections. The article itself is fetched by the normal discovery scrape, like
every other source's page; nothing here logs in, and no page text is written by
another model.

The feed is ordered by publication date only approximately (an article can
appear up to about a week out of order), so a month is read with a 14-day
margin either side and filtered on the publication date itself.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, List, Optional, Set

import requests
from bs4 import BeautifulSoup

from cyber_data_collector.datasources.base import DataSource
from cyber_data_collector.models.config import DataSourceConfig, DateRange
from cyber_data_collector.models.events import (
    ConfidenceScore,
    CyberEvent,
    CyberEventType,
    EventSeverity,
    EventSource,
)
from cyber_data_collector.processing.fetch_errors import PARSE, classify_error
from cyber_data_collector.utils import RateLimiter

BASE_URL = "https://www.cyberdaily.au"
DEFAULT_SECTIONS = ("security",)
#: How far out of publication order an article can appear in the feed (seen: 7.1 days).
ORDER_SLACK = timedelta(days=14)
#: A section has ~5,300 articles (2018 onward), 13 a page; a month never needs more.
MAX_PAGES_PER_MONTH = 60
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
#: Waits (seconds) before each retry of a feed page the site answered with
#: 429 Too Many Requests or 503. The 2026-10-28 backfill lost 2021-03 and
#: 2021-05 to single 429s deep in the feed; a Retry-After header wins.
THROTTLE_WAITS = (15, 60, 180, 300)
THROTTLED = (429, 503)

_ARTICLE_ID = re.compile(r"cyberdaily\.au/(?:[\w-]+/)+(\d+)-", re.IGNORECASE)
_AUSTRALIAN = re.compile(
    r"australi|aussie|\b(?:nsw|qld|act|wa|sa|nt|tas|vic)\b|new south wales|victoria|queensland|tasmania|"
    r"canberra|sydney|melbourne|brisbane|perth|adelaide|hobart|darwin", re.IGNORECASE)


def _retry_after(value: Optional[str]) -> float:
    """Seconds from a Retry-After header (delta-seconds or HTTP date); 0 if absent or unreadable."""
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (when - datetime.now(when.tzinfo)).total_seconds())


class CyberDailyFeedError(RuntimeError):
    """A feed page could not be read or did not parse as the feed.

    ``error_type`` (:mod:`~cyber_data_collector.processing.fetch_errors`) says
    which: ``parse`` for anything malformed, else the type of the fetch failure
    (``throttled`` after every :data:`THROTTLE_WAITS`, ``missing``, ``network``).
    """

    def __init__(self, message: str, error_type: str = PARSE):
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True)
class FeedItem:
    """One article as the section feed lists it."""

    url: str
    title: str
    summary: str
    published: datetime  # aware, in the publisher's own (Australian) offset
    section: str

    @property
    def published_on(self) -> date:
        """The publication date as the publisher dated it (its local day)."""
        return self.published.date()


def article_id(url: Optional[str]) -> Optional[str]:
    """Cyber Daily's numeric article id (``/security/11281-slug`` -> ``'11281'``)."""
    match = _ARTICLE_ID.search(url or "")
    return match.group(1) if match else None


def parse_feed(content: bytes, section: str) -> List[FeedItem]:
    """Parse one feed page. Raises :class:`CyberDailyFeedError` on anything malformed."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise CyberDailyFeedError(f"feed did not parse as XML: {exc}") from exc
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise CyberDailyFeedError(f"not an RSS feed (root element <{root.tag}>)")
    items = []
    for node in channel.findall("item"):
        url = (node.findtext("link") or "").strip()
        title = (node.findtext("title") or "").strip()
        stamp = (node.findtext("pubDate") or "").strip()
        if not url or not title or not stamp:
            raise CyberDailyFeedError(f"feed item without link, title or pubDate: {url or title or '?'}")
        try:
            published = parsedate_to_datetime(stamp)
        except (TypeError, ValueError) as exc:
            raise CyberDailyFeedError(f"unparseable pubDate {stamp!r} for {url}") from exc
        if published.tzinfo is None:
            raise CyberDailyFeedError(f"pubDate {stamp!r} for {url} has no UTC offset")
        summary = BeautifulSoup(node.findtext("description") or "", "html.parser").get_text(" ", strip=True)
        items.append(FeedItem(url=url, title=title, summary=summary, published=published, section=section))
    return items


class CyberDailyDataSource(DataSource):
    """Articles from Cyber Daily's section RSS feeds, one month at a time."""

    def __init__(self, config: DataSourceConfig, rate_limiter: RateLimiter,
                 env_config: Optional[Dict[str, Optional[str]]] = None,
                 fetch: Optional[Callable[[str, Dict[str, Any]], bytes]] = None):
        super().__init__(config, rate_limiter)
        self.env_config = env_config or {}
        self.sections: List[str] = list(config.custom_config.get("sections") or DEFAULT_SECTIONS)
        self._fetch = fetch or self._http_get
        #: URLs already stored under any source; their articles are not stored again.
        self.known_urls: Set[str] = set()
        self.skipped_known = 0
        #: Articles the feed listed for the last collected range (stored or not);
        #: None until a collection has read every section completely.
        self.items_seen: Optional[int] = None

    def validate_config(self) -> bool:
        return bool(self.sections)

    # ------------------------------------------------------------------
    # Feed access
    # ------------------------------------------------------------------

    def _http_get(self, url: str, params: Dict[str, Any], sleep: Callable[[float], None] = time.sleep) -> bytes:
        """One feed page. A throttled answer (429/503) is waited out and asked
        again (:data:`THROTTLE_WAITS`); still throttled after that, it raises,
        and the month is recorded as incomplete and searched again next run."""
        for wait in (*THROTTLE_WAITS, None):
            response = requests.get(url, params=params, headers={"User-Agent": USER_AGENT},
                                    timeout=self.config.timeout)
            if response.status_code not in THROTTLED or wait is None:
                break
            wait = max(wait, _retry_after(response.headers.get("Retry-After")))
            self.logger.warning("Cyber Daily answered %s for %s (offset %s); waiting %ss",
                                response.status_code, url, params.get("limitstart"), wait)
            sleep(wait)
        response.raise_for_status()
        return response.content

    async def _page(self, section: str, offset: int, cache: Dict[int, List[FeedItem]]) -> List[FeedItem]:
        if offset not in cache:
            await self.rate_limiter.wait("cyberdaily")
            params = {"format": "feed", "type": "rss", "limitstart": offset}
            loop = asyncio.get_running_loop()
            try:
                content = await loop.run_in_executor(None, self._fetch, f"{BASE_URL}/{section}", params)
            except Exception as exc:
                raise CyberDailyFeedError(f"{section} feed at offset {offset} could not be fetched: {exc}",
                                          classify_error(exc)) from exc
            cache[offset] = parse_feed(content, section)
        return cache[offset]

    async def _first_page_reaching(self, section: str, newest_wanted: datetime, page_size: int,
                                   cache: Dict[int, List[FeedItem]]) -> int:
        """Index of the first page holding anything at or before ``newest_wanted``.

        The pages before it are entirely newer. Found by doubling then
        bisecting, so an old month costs ~20 requests, not a walk from today.
        """
        async def reaches(page: int) -> bool:
            items = await self._page(section, page * page_size, cache)
            return not items or min(i.published for i in items) <= newest_wanted

        if await reaches(0):
            return 0
        low, high = 0, 1
        while not await reaches(high):
            low, high = high, high * 2
        while high - low > 1:
            middle = (low + high) // 2
            if await reaches(middle):
                high = middle
            else:
                low = middle
        return high

    async def items_between(self, section: str, start: date, end: date) -> List[FeedItem]:
        """Every article in ``section`` published from ``start`` to ``end`` (inclusive).

        Raises:
            CyberDailyFeedError: a page could not be fetched or parsed, the feed
                is empty, or the month did not end within ``MAX_PAGES_PER_MONTH``.
        """
        cache: Dict[int, List[FeedItem]] = {}
        first = await self._page(section, 0, cache)
        if not first:
            raise CyberDailyFeedError(f"{section} feed returned no items")
        page_size = len(first)
        tz = first[0].published.tzinfo
        newest_wanted = datetime.combine(end, datetime.max.time(), tzinfo=tz) + ORDER_SLACK
        oldest_wanted = datetime.combine(start, datetime.min.time(), tzinfo=tz) - ORDER_SLACK

        page = await self._first_page_reaching(section, newest_wanted, page_size, cache)
        found: Dict[str, FeedItem] = {}
        for _ in range(MAX_PAGES_PER_MONTH):
            items = await self._page(section, page * page_size, cache)
            if not items:
                break
            for item in items:
                if start <= item.published_on <= end:
                    found.setdefault(item.url, item)
            if max(i.published for i in items) < oldest_wanted:
                break
            page += 1
        else:
            raise CyberDailyFeedError(
                f"{section} feed: {start}..{end} not finished after {MAX_PAGES_PER_MONTH} pages")
        return sorted(found.values(), key=lambda i: i.published)

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------

    async def collect_events(self, date_range: DateRange) -> List[CyberEvent]:
        start = _as_date(date_range.start_date)
        end = _as_date(date_range.end_date or datetime.now())
        known_ids = {aid for aid in (article_id(u) for u in self.known_urls) if aid}
        events: List[CyberEvent] = []
        seen: Set[str] = set()
        self.skipped_known = 0
        self.items_seen = None
        complete = True
        for section in self.sections:
            try:
                items = await self.items_between(section, start, end)
            except CyberDailyFeedError as exc:
                self.note_failure(f"Cyber Daily {section} feed for {start}..{end} incomplete", exc)
                complete = False
                continue
            for item in items:
                key = article_id(item.url) or item.url
                if key in seen:
                    continue
                seen.add(key)
                if key in known_ids:
                    self.skipped_known += 1
                    continue
                events.append(self._to_event(item))
        if complete:
            self.items_seen = len(seen)
        self.logger.info("Cyber Daily %s..%s: %d new article(s), %d already stored by another source",
                         start, end, len(events), self.skipped_known)
        return events

    @staticmethod
    def _to_event(item: FeedItem) -> CyberEvent:
        aid = article_id(item.url)
        # Publication date, not incident date: event_date stays empty and the
        # raw record takes the publication date until enrichment finds the
        # incident's own date.
        published = item.published.replace(tzinfo=None)
        source = EventSource(
            source_id=f"cyberdaily_{aid or hashlib.md5(item.url.encode()).hexdigest()[:16]}",
            source_type="Cyber Daily",
            url=item.url,
            title=item.title,
            content_snippet=item.summary[:500],
            domain="cyberdaily.au",
            credibility_score=0.75,
            relevance_score=0.6,
            publication_date=published,
        )
        return CyberEvent(
            external_ids={"cyberdaily_url": item.url, **({"cyberdaily_id": aid} if aid else {})},
            title=item.title,
            description=item.summary,
            event_type=CyberEventType.OTHER,
            severity=EventSeverity.MEDIUM,
            australian_relevance=bool(_AUSTRALIAN.search(f"{item.title} {item.summary}")),
            data_sources=[source],
            confidence=ConfidenceScore(overall=0.6, source_reliability=0.75, data_completeness=0.4,
                                       temporal_accuracy=0.5, geographic_accuracy=0.6),
        )

    def get_source_info(self) -> Dict[str, Any]:
        return {
            "name": "Cyber Daily",
            "description": "Australian cyber security news (Momentum Media); section RSS feeds",
            "update_frequency": "Daily",
            "coverage": "Australian incidents, including ransomware leak-site claims",
            "data_types": ["News articles"],
            "sections": list(self.sections),
        }


def _as_date(value: Any) -> date:
    return value.date() if isinstance(value, datetime) else value

