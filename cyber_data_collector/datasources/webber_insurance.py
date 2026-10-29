from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

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
from cyber_data_collector.processing.active_evidence import shares_distinctive_words
from cyber_data_collector.processing.incident_dates import (
    BASIS_STATED_IN_SOURCE,
    BASIS_WEBBER_HEADING,
    incident_date_from_text,
)
from cyber_data_collector.utils import RateLimiter
from cyber_data_collector.utils.llm_models import perplexity_model
from cyber_data_collector.utils.url_clean import clean_url


class WebberInsuranceDataSource(DataSource):
    """Webber Insurance data breaches list scraper."""

    def __init__(self, config: DataSourceConfig, rate_limiter: RateLimiter, env_config: Dict[str, Optional[str]]):
        super().__init__(config, rate_limiter)
        self.env_config = env_config
        # Detail pages that could not be scraped in the last collect_events run.
        self.failed_detail_pages: List[Dict[str, str]] = []
        self.base_url = "https://www.webberinsurance.com.au/data-breaches-list"

    def validate_config(self) -> bool:
        return True

    async def collect_events(self, date_range: DateRange) -> List[CyberEvent]:
        """
        Collects events by scraping all links from the main list page and then
        scraping the detail page for each event, filtering by date afterwards.
        """
        try:
            await self.rate_limiter.wait("webber_list")
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: requests.get(self.base_url, headers=headers, timeout=self.config.timeout),
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser")

            event_links = self._extract_all_event_links(soup)
            self.logger.info(f"Found {len(event_links)} total potential event links.")

            all_events: List[CyberEvent] = []
            self.failed_detail_pages = []
            for link_info in event_links:
                # Check if the section date is within our range first (much more efficient)
                section_date = link_info.get('section_date')
                if section_date:
                    section_date_only = section_date.date()
                    range_start = date_range.start_date.date() if hasattr(date_range.start_date, 'date') else date_range.start_date
                    range_end = date_range.end_date.date() if hasattr(date_range.end_date, 'date') else date_range.end_date

                    if not (range_start <= section_date_only <= range_end):
                        self.logger.debug(f"Section {link_info.get('section_header', '')} ({section_date_only}) outside date range {range_start} to {range_end} - skipping")
                        continue

                # Section is within range, scrape the event details
                await self.rate_limiter.wait("webber_detail")
                event = self._scrape_detail_page(link_info['url'], section_date, item=link_info)
                if event:
                    self.logger.info(f"Found event in {link_info.get('section_header', '')}: {event.title[:50]}... with date: {event.event_date}")
                    all_events.append(event)

            self.logger.info(f"Collected {len(all_events)} events from Webber Insurance within the date range.")
            if self.failed_detail_pages:
                self.logger.warning(
                    "Webber Insurance: %d detail page(s) could not be scraped and were skipped: %s",
                    len(self.failed_detail_pages),
                    ", ".join(f["url"] for f in self.failed_detail_pages[:10]))
            return all_events

        except Exception as exc:
            self.note_failure("Webber Insurance scraping failed", exc)
            return []

    def _extract_all_event_links(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        """Extracts the list's items: each dated heading with the link of *its own* entries.

        An item is a heading ("Vertel - June 2025") and the ``<li>`` entries
        that follow it up to the next ``<h3>``; the link belongs to the entry
        that holds it, and the entry's text and the heading's organisation
        travel with it so the page that link leads to can be checked against
        what the list says it is. (The old filter kept only links whose text
        contained "breach", "cyber", "hack", "attack", "ransomware" or
        "incident": 203 of 1,282 entries on 2026-10-29, such as "Almost 2m
        impacted, almost 50k credit cards compromised", were never stored.)
        """
        events: List[Dict[str, Any]] = []
        sections = 0

        for header in soup.find_all('h3'):
            header_text = header.get_text(strip=True)

            # Parse date from section header (e.g., "Vertel – June 2025")
            section_date = self._parse_section_header_date(header_text)
            if not section_date:
                continue
            sections += 1
            organisation = self._organisation_from_heading(header_text)
            self.logger.debug(f"Processing section: {header_text} -> {section_date}")

            seen: set = set()
            current = header.next_sibling
            while current:
                if current.name == 'h3':
                    break
                if hasattr(current, 'find_all'):
                    for entry in self._entries(current):
                        link = next((a for a in entry.find_all('a', href=True) if self._is_article_link(a)), None)
                        if link is None:
                            continue
                        # A newsletter link carries utm_* tags and, for some, a subscriber token: the record's
                        # URL is the article's address without them. The newsletter date in the tag is kept.
                        linked = urljoin(self.base_url, link.get('href', ''))
                        full_url = clean_url(linked)
                        if full_url in seen:
                            continue
                        seen.add(full_url)
                        link_text = link.get_text(strip=True)
                        events.append({
                            "url": full_url,
                            "url_date": self._parse_url_date(linked),
                            "text": link_text,
                            "summary": entry.get_text(" ", strip=True),
                            "organisation": organisation,
                            "section_date": section_date,
                            "section_header": header_text,
                        })
                        self.logger.debug(f"Found event in {header_text}: {link_text[:50]}...")
                current = current.next_sibling

        self.logger.info(f"Extracted {len(events)} events from {sections} dated sections")
        return events

    @staticmethod
    def _entries(block: Any) -> List[Any]:
        """The list entries (``<li>``) in a block of the page; the block itself when it is an entry or a bare link."""
        if block.name == 'li':
            return [block]
        entries = block.find_all('li')
        if entries:
            return entries
        # A link outside any list: kept only when its own text reads as a breach report, as before.
        text = block.get_text(" ", strip=True).lower()
        looks_like_report = len(text) > 20 and any(
            word in text for word in ('breach', 'cyber', 'hack', 'attack', 'ransomware', 'incident'))
        return [block] if looks_like_report and block.find('a', href=True) else []

    def _is_article_link(self, anchor: Any) -> bool:
        """An outside article, not Webber's own guides or an in-page anchor."""
        href = (anchor.get('href') or '').strip()
        if not href or href.startswith(('#', 'mailto:', 'tel:', 'javascript:')):
            return False
        text = anchor.get_text(strip=True).lower()
        if any(exclude in text for exclude in ('guide', 'ultimate', 'notification laws', 'essentials')):
            return False
        host = urlparse(urljoin(self.base_url, href)).netloc.lower()
        return bool(text) and not host.endswith('webberinsurance.com.au')

    @staticmethod
    def _organisation_from_heading(header_text: str) -> str:
        """The organisation a heading names: "Vertel – June 2025" -> "Vertel"."""
        name = re.sub(r'\s*[-–—�]*\s*(?:January|February|March|April|May|June|July|August|'
                      r'September|October|November|December)\s+\d{4}\b.*$', '', header_text, flags=re.I)
        return name.strip(' -–—� ')

    def _parse_section_header_date(self, header_text: str) -> Optional[datetime]:
        """Parse date from section header like 'Vertel – June 2025' or 'BMW – September 2025'."""
        try:
            # Look for month-year patterns in the header
            month_year_pattern = r'(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})'
            match = re.search(month_year_pattern, header_text, re.IGNORECASE)

            if match:
                year = int(match.group(1))
                month_name = re.search(r'(January|February|March|April|May|June|July|August|September|October|November|December)', header_text, re.IGNORECASE).group(1)

                # Convert month name to number
                month_map = {
                    'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5, 'june': 6,
                    'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11, 'december': 12
                }
                month = month_map.get(month_name.lower())

                if month:
                    # A month, not a day: stored as its 1st with precision "month".
                    # (It used to become the 15th - a day nobody reported.)
                    return datetime(year, month, 1)

            return None
        except (ValueError, AttributeError) as exc:
            self.logger.debug("Failed to parse section header date from '%s': %s", header_text, exc)
            return None

    @staticmethod
    def _parse_url_date(url: str) -> Optional[datetime]:
        """The newsletter date in a link ("campaign=20_06_2025"): a publication date, never the incident's."""
        match = re.search(r'(\d{2})_(\d{2})_(\d{4})', url or "")
        if not match:
            return None
        day, month, year = match.groups()
        try:
            return datetime(int(year), int(month), int(day))
        except ValueError:
            return None

    def _scrape_detail_page(self, url: str, section_date: Optional[datetime] = None,
                            item: Optional[Dict[str, Any]] = None) -> Optional[CyberEvent]:
        """Scrapes a single event detail page for structured information.

        Args:
            item: The list entry that led here (``text``, ``organisation``). What the
                list says the link is travels with the record: when the page's own
                heading is about something else (a site name, a disclaimer, another
                article), the record keeps the list's title, so the identity check
                after scraping sees the mismatch instead of a title copied from the page.
        """
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            response = requests.get(url, headers=headers, timeout=self.config.timeout)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser")

            title_tag = soup.find('h1')
            title = title_tag.get_text(strip=True) if title_tag else ""
            item = item or {}
            claimed = str(item.get("text") or "").strip()
            organisation = str(item.get("organisation") or "").strip()
            if claimed and not (title and shares_distinctive_words(title, claimed, organisation)):
                title = claimed

            content_block = soup.find('article') or soup.find('div', class_="content") or soup
            description = content_block.get_text(strip=True, separator="\n")

            # A day counts as the incident's only when the wording ties it to the
            # breach ("occurred on", "discovered", "between X and Y"). The first date
            # in an article is its byline or publication date. The list heading gives
            # the month; the date in the link is the newsletter's (a publication date).
            near = section_date.date() if section_date else None
            stated = incident_date_from_text(content_block.get_text(" "), near=near)
            published = self._parse_url_date(url) or (item or {}).get("url_date")
            if stated:
                event_date = datetime(stated.day.year, stated.day.month, stated.day.day)
                precision, basis = "day", BASIS_STATED_IN_SOURCE
            elif section_date:
                event_date, precision, basis = section_date, "month", BASIS_WEBBER_HEADING
            else:
                event_date, precision, basis = None, None, None

            if organisation:
                entity_name = organisation
            else:
                entity_name_match = re.match(r'([^-–]+)', title)
                entity_name = entity_name_match.group(1).strip() if entity_name_match else title

            event = self._convert_details_to_event(url, title, description, event_date, entity_name,
                                                   published=published)
            event.event_date_precision, event.event_date_basis = precision, basis
            return event

        except Exception as e:
            # A failed detail page is a failure. It used to be replaced by
            # Perplexity-written prose about whatever the URL seemed to be, stored
            # as if it were Webber's page with Webber's credibility.
            self.logger.warning(f"Failed to scrape detail page {url}: {e}")
            self.failed_detail_pages.append({"url": url, "error": f"{type(e).__name__}: {e}"})
            self.note_failure(f"Webber Insurance detail page could not be read: {url}", e)
            return None

    def _convert_details_to_event(self, url: str, title: str, description: str, event_date: Optional[datetime],
                                  entity_name: str, published: Optional[datetime] = None) -> CyberEvent:
        """Converts the scraped details into a CyberEvent object."""
        entity = AffectedEntity(
            name=entity_name,
            entity_type=EntityType.OTHER,
            australian_entity=True,
            confidence_score=0.9,
        )

        data_source = EventSource(
            source_id=f"webber_{hashlib.md5(url.encode()).hexdigest()[:16]}",
            source_type="Webber Insurance",
            url=url,
            title=title,
            content_snippet=description[:500],
            domain="webberinsurance.com.au",
            credibility_score=0.8,
            relevance_score=1.0,
            publication_date=published
        )

        confidence = ConfidenceScore(
            overall=0.85, source_reliability=0.8, data_completeness=0.7,
            temporal_accuracy=0.9, geographic_accuracy=1.0,
        )

        return CyberEvent(
            external_ids={"webber_url": url},
            title=title, description=description,
            event_type=CyberEventType.DATA_BREACH,
            severity=EventSeverity.MEDIUM,
            event_date=event_date,
            primary_entity=entity,
            affected_entities=[entity],
            australian_relevance=True,
            data_sources=[data_source],
            confidence=confidence,
        )

    def get_source_info(self) -> Dict[str, Any]:
        return {
            "name": "Webber Insurance Data Breaches List",
            "description": "Curated list of Australian data breaches with detailed event pages",
            "update_frequency": "Periodic",
            "coverage": "Australian entities",
            "data_types": ["Data breaches", "Privacy incidents"],
        }















