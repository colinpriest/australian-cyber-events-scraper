from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Any
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from dateutil.parser import parse as dateutil_parse

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
from cyber_data_collector.utils import RateLimiter


class OAICDataSource(DataSource):
    """Australian Information Commissioner's Office (OAIC) media centre scraper for cyber-related regulatory actions."""

    def __init__(self, config: DataSourceConfig, rate_limiter: RateLimiter, env_config: Dict[str, str | None]):
        super().__init__(config, rate_limiter)
        self.base_url = "https://www.oaic.gov.au/news/media-centre"
        self.search_url = "https://www.oaic.gov.au/news/media-centre?query=&sort=dmetapublishedDateISO&num_ranks=1000"

    def validate_config(self) -> bool:
        return True

    async def collect_events(self, date_range: DateRange) -> List[CyberEvent]:
        """
        Collects cyber-related regulatory events from OAIC media centre.
        """
        try:
            await self.rate_limiter.wait("oaic_search")
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            response = requests.get(self.search_url, headers=headers, timeout=self.config.timeout)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser")

            # Extract article links from search results
            article_links = self._extract_article_links(soup)
            self.logger.info(f"Found {len(article_links)} potential article links from OAIC")

            all_events: List[CyberEvent] = []
            for link_info in article_links:
                # First check if we have a publication date from search results
                pub_date_str = link_info.get('publication_date')
                if pub_date_str:
                    try:
                        from dateutil.parser import parse as dateutil_parse
                        from dateutil.relativedelta import relativedelta
                        pub_date = dateutil_parse(pub_date_str)
                        pub_date_only = pub_date.date()
                        # Expand to 3-month window to catch late-reported events
                        range_start_raw = date_range.start_date.date() if hasattr(date_range.start_date, 'date') else date_range.start_date
                        range_start = range_start_raw - relativedelta(months=2)
                        range_end = date_range.end_date.date() if hasattr(date_range.end_date, 'date') else date_range.end_date

                        if not (range_start <= pub_date_only <= range_end):
                            self.logger.debug(f"OAIC article outside 3-month range ({pub_date_only}): {link_info['text'][:50]}...")
                            continue
                    except:
                        pass  # Fall back to scraping for date

                await self.rate_limiter.wait("oaic_detail")

                # Get the actual article URL (may need to resolve redirects)
                actual_url = self._resolve_article_url(link_info['url'])
                if not actual_url:
                    continue

                # Pass the publication date if we found one
                publication_date = None
                if pub_date_str:
                    try:
                        publication_date = dateutil_parse(pub_date_str)
                    except:
                        pass

                event = self._scrape_article_page(actual_url, link_info['text'], publication_date)
                if event:
                    # Include all events from articles published in the date range
                    # Do NOT filter by event_date - late-reported incidents may have
                    # event_date outside the processing month (e.g., incident in Sept, reported in Nov)
                    self.logger.info(f"OAIC event: {event.title[:50]}...")
                    all_events.append(event)

            self.logger.info(f"Collected {len(all_events)} OAIC events within the date range")
            return all_events

        except Exception as exc:
            self.logger.error(f"OAIC scraping failed: {exc}")
            return []

    def _extract_article_links(self, soup: BeautifulSoup) -> List[Dict[str, str]]:
        """Extract article links with publication dates from OAIC search results page."""
        links = []

        # First, try to find structured search results with dates
        search_results = self._extract_structured_results(soup)
        if search_results:
            self.logger.info(f"Found {len(search_results)} structured search results")
            return search_results

        # Fallback to original method if structured extraction fails
        all_links = soup.find_all('a', href=True)

        for link in all_links:
            href = link.get('href', '')
            text = link.get_text(strip=True)

            # Filter for cyber/privacy related content
            if (len(text) > 20 and
                any(keyword in text.lower() for keyword in [
                    'cyber', 'data breach', 'privacy', 'security', 'hack', 'attack',
                    'civil penalty', 'enforcement', 'investigation', 'determination',
                    'enforceable undertaking', 'compliance', 'breach', 'incident'
                ]) and
                # Exclude navigation/generic links
                not any(exclude in text.lower() for exclude in [
                    'privacy policy', 'your privacy rights', 'privacy complaints',
                    'australian privacy principles', 'privacy guidance', 'privacy legislation'
                ]) and
                # Must be OAIC article link
                ('/news/' in href or '/media-centre/' in href or 's/redirect' in href)):

                # Try to find publication date near this link
                pub_date = self._find_publication_date_near_link(link)

                links.append({
                    "url": href,
                    "text": text,
                    "publication_date": pub_date
                })
                self.logger.debug(f"Found OAIC article: {text[:60]}... (Date: {pub_date})")

        self.logger.info(f"Extracted {len(links)} potential OAIC article links")
        return links

    def _extract_structured_results(self, soup: BeautifulSoup) -> List[Dict[str, str]]:
        """Extract search results from structured containers with dates."""
        results = []

        # Look for containers that might hold search results with dates
        containers = soup.find_all(['div', 'li', 'article'])

        for container in containers:
            # Check if this container has both a link and a date
            links = container.find_all('a', href=True)
            text_content = container.get_text()

            # Look for date patterns in this container
            import re
            date_matches = re.findall(r'\\b\\d{1,2}\\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\\s+\\d{4}\\b', text_content, re.IGNORECASE)

            if links and date_matches:
                for link in links:
                    link_text = link.get_text(strip=True)
                    href = link.get('href', '')

                    # Filter for actual article links
                    if (len(link_text) > 20 and
                        ('oaic.gov.au' in href or href.startswith('/news/') or '/s/redirect' in href) and
                        any(keyword in link_text.lower() for keyword in [
                            'cyber', 'data breach', 'privacy', 'security', 'hack', 'attack',
                            'civil penalty', 'enforcement', 'investigation', 'determination',
                            'enforceable undertaking', 'compliance', 'breach', 'incident'
                        ]) and
                        not any(exclude in link_text.lower() for exclude in [
                            'privacy policy', 'your privacy rights', 'privacy complaints',
                            'australian privacy principles', 'privacy guidance', 'privacy legislation'
                        ])):

                        results.append({
                            'url': href,
                            'text': link_text,
                            'publication_date': date_matches[0]  # Use first date found
                        })

        return results

    def _find_publication_date_near_link(self, link) -> Optional[str]:
        """Find publication date near a link element."""
        import re

        # Check the link itself and nearby elements for dates
        elements_to_check = [link]

        # Add parent and siblings
        if link.parent:
            elements_to_check.append(link.parent)
            # Get previous and next siblings safely
            for sibling in link.parent.previous_siblings:
                if len(elements_to_check) > 10:  # Limit to avoid excessive checking
                    break
                if sibling and hasattr(sibling, 'get_text'):
                    elements_to_check.append(sibling)

            for sibling in link.parent.next_siblings:
                if len(elements_to_check) > 15:  # Limit to avoid excessive checking
                    break
                if sibling and hasattr(sibling, 'get_text'):
                    elements_to_check.append(sibling)

        for elem in elements_to_check:
            if elem and hasattr(elem, 'get_text'):
                text = elem.get_text()
                date_matches = re.findall(r'\\b\\d{1,2}\\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\\s+\\d{4}\\b', text, re.IGNORECASE)
                if date_matches:
                    return date_matches[0]

        return None

    def _resolve_article_url(self, url: str) -> Optional[str]:
        """Resolve redirect URLs to get actual article URLs."""
        try:
            # Handle relative URLs
            if url.startswith('/'):
                url = urljoin("https://www.oaic.gov.au", url)

            # Handle OAIC redirect URLs
            if 's/redirect' in url:
                # Extract the actual URL from redirect parameters
                parsed = urlparse(url)
                if parsed.path == '/s/redirect':
                    query_params = parse_qs(parsed.query)
                    actual_url = query_params.get('url', [None])[0]
                    if actual_url:
                        # URL decode
                        import urllib.parse
                        return urllib.parse.unquote(actual_url)

            # Already a direct URL
            if url.startswith('https://www.oaic.gov.au/news/'):
                return url

            return None

        except Exception as e:
            self.logger.warning(f"Failed to resolve OAIC URL {url}: {e}")
            return None

    def _scrape_article_page(self, url: str, title_hint: str, publication_date: Optional[datetime] = None) -> Optional[CyberEvent]:
        """Scrape an OAIC article page for event details."""
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            response = requests.get(url, headers=headers, timeout=self.config.timeout)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser")

            # Extract title
            title_tag = soup.find('h1') or soup.find('title')
            title = title_tag.get_text(strip=True) if title_tag else title_hint

            # Extract main content
            content_selectors = [
                'main', 'article', '.content', '.main-content',
                '.article-content', '.news-content'
            ]
            content_block = None
            for selector in content_selectors:
                content_block = soup.select_one(selector)
                if content_block:
                    break

            if not content_block:
                content_block = soup

            description = content_block.get_text(strip=True, separator="\n")

            # Use provided publication date or parse from content
            event_date = publication_date or self._parse_article_date(content_block, soup)

            # Extract affected entity (usually in the title or content)
            entity_name = self._extract_entity_name(title, description)

            return self._convert_to_cyber_event(url, title, description, event_date, entity_name)

        except Exception as e:
            self.logger.warning(f"Failed to scrape OAIC article {url}: {e}")
            return None

    def _parse_article_date(self, content_block: BeautifulSoup, soup: BeautifulSoup) -> Optional[datetime]:
        """Parse the publication date from OAIC article."""
        try:
            # Look for date meta tags first
            date_meta = soup.find('meta', {'name': 'DC.Date'}) or soup.find('meta', {'property': 'article:published_time'})
            if date_meta:
                date_str = date_meta.get('content', '')
                if date_str:
                    return dateutil_parse(date_str)

            # Look for date patterns in the content
            text = content_block.get_text()
            date_patterns = [
                r'\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}\b',
                r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}\b',
                r'\b\d{4}-\d{2}-\d{2}\b'
            ]

            for pattern in date_patterns:
                matches = re.findall(pattern, text, re.IGNORECASE)
                if matches:
                    for match in matches:
                        try:
                            return dateutil_parse(match)
                        except:
                            continue

            return None
        except Exception:
            return None

    def _extract_entity_name(self, title: str, description: str) -> str:
        """Extract the affected entity name from title and content."""
        # Common patterns for entity extraction
        patterns = [
            r'(?:action against|penalty.*against|investigation.*into|determination.*against)\s+([A-Za-z][A-Za-z0-9\s&.-]+?)(?:\s|$|,|\.|;)',
            r'([A-Z][A-Za-z0-9\s&.-]+?)\s+(?:cyber incident|data breach|privacy breach|breach|hack)',
            r'([A-Z][A-Za-z0-9\s&.-]+?)(?:\s+–|\s+privacy|\s+security|\s+data)'
        ]

        for pattern in patterns:
            match = re.search(pattern, title, re.IGNORECASE)
            if match:
                entity = match.group(1).strip()
                # Clean up common suffixes
                entity = re.sub(r'\s+(Pty|Ltd|Limited|Inc|Corporation|Corp|Company)\.?$', '', entity, flags=re.IGNORECASE)
                return entity

        # Fallback: extract first capitalized word/phrase
        words = title.split()
        for i, word in enumerate(words):
            if word[0].isupper() and len(word) > 2:
                # Try to get a reasonable entity name
                entity_words = [word]
                for j in range(i+1, min(i+3, len(words))):
                    if words[j][0].isupper() or words[j].lower() in ['and', '&', 'of']:
                        entity_words.append(words[j])
                    else:
                        break
                return ' '.join(entity_words)

        return "Unknown Entity"

    def _convert_to_cyber_event(self, url: str, title: str, description: str, event_date: Optional[datetime], entity_name: str) -> CyberEvent:
        """Convert scraped data to CyberEvent object."""
        entity = AffectedEntity(
            name=entity_name,
            entity_type=EntityType.OTHER,  # Use OTHER since ORGANIZATION doesn't exist
            australian_entity=True,  # OAIC only regulates Australian entities
            confidence_score=0.9,
        )

        data_source = EventSource(
            source_id=f"oaic_{hash(url)}",
            source_type="OAIC",
            url=url,
            title=title,
            content_snippet=description[:500],
            domain="oaic.gov.au",
            credibility_score=0.95,  # High credibility for government source
            relevance_score=1.0,
            publication_date=event_date
        )

        confidence = ConfidenceScore(
            overall=0.9, source_reliability=0.95, data_completeness=0.8,
            temporal_accuracy=0.85, geographic_accuracy=1.0,
        )

        # Determine event type based on content
        event_type = CyberEventType.OTHER  # Default to OTHER for regulatory actions
        if any(term in title.lower() for term in ['data breach', 'cyber incident', 'hack']):
            event_type = CyberEventType.DATA_BREACH

        # Determine severity based on content
        severity = EventSeverity.HIGH  # OAIC actions are typically significant
        if 'civil penalty' in title.lower():
            severity = EventSeverity.CRITICAL

        return CyberEvent(
            external_ids={"oaic_url": url},
            title=title,
            description=description,
            event_type=event_type,
            severity=severity,
            event_date=event_date,
            primary_entity=entity,
            affected_entities=[entity],
            australian_relevance=True,  # OAIC is Australian regulator
            data_sources=[data_source],
            confidence=confidence,
        )

    def get_source_info(self) -> Dict[str, Any]:
        return {
            "name": "Australian Information Commissioner's Office (OAIC)",
            "description": "Regulatory actions, investigations, and enforcement related to privacy and data breaches",
            "update_frequency": "As published",
            "coverage": "Australian privacy and data protection regulatory actions",
            "data_types": ["Regulatory actions", "Civil penalties", "Investigations", "Privacy determinations"],
        }