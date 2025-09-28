from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Any
from urllib.parse import urljoin

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


class WebberInsuranceDataSource(DataSource):
    """Webber Insurance data breaches list scraper."""

    def __init__(self, config: DataSourceConfig, rate_limiter: RateLimiter, env_config: Dict[str, str | None]):
        super().__init__(config, rate_limiter)
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
            response = requests.get(self.base_url, headers=headers, timeout=self.config.timeout)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser")

            event_links = self._extract_all_event_links(soup)
            self.logger.info(f"Found {len(event_links)} total potential event links.")

            all_events: List[CyberEvent] = []
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
                event = self._scrape_detail_page(link_info['url'], section_date)
                if event:
                    self.logger.info(f"Found event in {link_info.get('section_header', '')}: {event.title[:50]}... with date: {event.event_date}")
                    all_events.append(event)

            self.logger.info(f"Collected {len(all_events)} events from Webber Insurance within the date range.")
            return all_events

        except Exception as exc:
            self.logger.error(f"Webber Insurance scraping failed: {exc}")
            return []

    def _extract_all_event_links(self, soup: BeautifulSoup) -> List[Dict[str, str]]:
        """Extracts events organized by section headers with dates."""
        events = []

        # Find all h3 headers that contain date information
        section_headers = soup.find_all('h3')

        for header in section_headers:
            header_text = header.get_text(strip=True)

            # Parse date from section header (e.g., "Vertel – June 2025")
            section_date = self._parse_section_header_date(header_text)
            if not section_date:
                continue

            self.logger.debug(f"Processing section: {header_text} -> {section_date}")

            # Find events after this header until the next header
            current = header.next_sibling
            events_in_section = []

            while current:
                # Stop if we hit another h3 header
                if current.name == 'h3':
                    break

                # Look for links in this section
                if hasattr(current, 'find_all'):
                    links = current.find_all('a', href=True)
                    for link in links:
                        link_text = link.get_text(strip=True)
                        href = link.get('href', '')

                        # Filter for actual breach event links
                        if (len(link_text) > 20 and
                            any(keyword in link_text.lower() for keyword in ['breach', 'cyber', 'hack', 'attack', 'ransomware', 'incident']) and
                            not any(exclude in link_text.lower() for exclude in ['guide', 'ultimate', 'notification laws', 'essentials'])):

                            # Use absolute URL if it starts with http, otherwise join with base
                            if href.startswith(('http://', 'https://')):
                                full_url = href
                            else:
                                full_url = urljoin(self.base_url, href)

                            events_in_section.append({
                                "url": full_url,
                                "text": link_text,
                                "section_date": section_date,
                                "section_header": header_text
                            })
                            self.logger.debug(f"Found event in {header_text}: {link_text[:50]}...")

                current = current.next_sibling

            events.extend(events_in_section)

        self.logger.info(f"Extracted {len(events)} events from {len([h for h in section_headers if self._parse_section_header_date(h.get_text(strip=True))])} dated sections")
        return events

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
                    # Use the 15th as a default day for the month
                    return datetime(year, month, 15)

            return None
        except (ValueError, AttributeError):
            return None

    def _parse_date(self, text: str, url: str = "") -> Optional[datetime]:
        """Robustly parse date strings from text and URL."""
        try:
            # First try to extract specific date patterns from text
            date_patterns = [
                r'\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}\b',  # "19 Jun 2025"
                r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}\b',  # "June 19, 2025"
                r'\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b',  # "2025-06-19"
                r'\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b'   # "19/06/2025"
            ]

            # Search for date patterns in text
            for pattern in date_patterns:
                matches = re.findall(pattern, text, re.IGNORECASE)
                if matches:
                    for match in matches:
                        try:
                            # Remove ordinal suffixes
                            cleaned = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', match)
                            return dateutil_parse(cleaned)
                        except:
                            continue

            # Try extracting date from URL if text parsing fails
            if url:
                # Look for patterns like "20_06_2025" in URL
                url_date_match = re.search(r'(\d{2})_(\d{2})_(\d{4})', url)
                if url_date_match:
                    day, month, year = url_date_match.groups()
                    try:
                        return datetime(int(year), int(month), int(day))
                    except:
                        pass

                # Look for "campaign=20_06_2025" style patterns
                campaign_match = re.search(r'campaign=(\d{2}_\d{2}_\d{4})', url)
                if campaign_match:
                    date_str = campaign_match.group(1).replace('_', '-')
                    try:
                        return dateutil_parse(date_str)
                    except:
                        pass

            return None
        except (ValueError, TypeError, OverflowError):
            return None

    def _scrape_detail_page(self, url: str, section_date: Optional[datetime] = None) -> Optional[CyberEvent]:
        """Scrapes a single event detail page for structured information."""
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            response = requests.get(url, headers=headers, timeout=self.config.timeout)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser")

            title_tag = soup.find('h1')
            title = title_tag.get_text(strip=True) if title_tag else ""

            content_block = soup.find('article') or soup.find('div', class_="content") or soup
            description = content_block.get_text(strip=True, separator="\n")

            # Use section date as primary source, fallback to content/URL parsing
            event_date = section_date or self._parse_date(content_block.get_text(), url)

            entity_name_match = re.match(r'([^-–]+)', title)
            entity_name = entity_name_match.group(1).strip() if entity_name_match else title

            return self._convert_details_to_event(url, title, description, event_date, entity_name)

        except Exception as e:
            self.logger.warning(f"Failed to scrape detail page {url}: {e}")

            # Try Perplexity fallback for URL errors
            try:
                alternative_content = self._perplexity_fallback(url, section_date)
                if alternative_content:
                    self.logger.info(f"Perplexity fallback successful for {url}")
                    # Extract basic info from alternative content
                    lines = alternative_content.split('\n')
                    title = lines[0] if lines else url.split('/')[-1]
                    description = alternative_content[:500] + "..." if len(alternative_content) > 500 else alternative_content

                    # Extract entity name from title
                    entity_name_match = re.match(r'([^-–]+)', title)
                    entity_name = entity_name_match.group(1).strip() if entity_name_match else title

                    return self._convert_details_to_event(url, title, description, section_date, entity_name)
            except Exception as fallback_e:
                self.logger.warning(f"Perplexity fallback also failed for {url}: {fallback_e}")

            return None

    def _perplexity_fallback(self, failed_url: str, section_date: Optional[datetime] = None) -> Optional[str]:
        """Use Perplexity to find alternative content when original URL fails."""
        try:
            import os
            import openai

            # Get Perplexity API key from environment
            api_key = os.environ.get('PERPLEXITY_API_KEY')
            if not api_key:
                self.logger.debug("Perplexity API key not found, skipping fallback")
                return None

            client = openai.OpenAI(
                api_key=api_key,
                base_url="https://api.perplexity.ai"
            )

            # Format date context from section_date
            date_context = ""
            if section_date:
                date_context = f"This incident occurred around {section_date.strftime('%B %Y')}"

            # Create a comprehensive query with exact URL and date context
            query = f"""Find information about the cybersecurity incident that was originally reported at this URL: {failed_url}

{date_context}

I need you to:
1. Identify what specific cybersecurity/data breach story this URL was about
2. Find information about the SAME incident from reliable sources
3. Provide a summary of the incident including company name, what happened, and impact
4. Focus on factual details about the breach/incident

The original URL is inaccessible, so I need the actual story content."""

            response = client.chat.completions.create(
                model="sonar-pro",
                messages=[
                    {
                        "role": "system",
                        "content": """You are a cybersecurity news specialist. When given an inaccessible URL about a cybersecurity incident, you research and provide detailed information about that incident.

IMPORTANT:
- Analyze the URL to understand what incident it was covering
- Use any date context provided to help identify the specific incident
- Provide factual information about the cybersecurity incident
- Include company name, type of breach, impact, and timeline if available
- Focus on verified information from reputable sources

Return a comprehensive summary of the incident in paragraph form."""
                    },
                    {
                        "role": "user",
                        "content": query
                    }
                ],
                max_tokens=800,
                temperature=0.1
            )

            if response.choices and response.choices[0].message.content:
                content = response.choices[0].message.content.strip()
                self.logger.debug(f"Perplexity fallback provided content for {failed_url}")
                return content

            return None

        except ImportError:
            self.logger.debug("OpenAI library not available for Perplexity fallback")
            return None
        except Exception as e:
            self.logger.warning(f"Perplexity fallback failed for {failed_url}: {e}")
            return None

    def _convert_details_to_event(self, url: str, title: str, description: str, event_date: Optional[datetime], entity_name: str) -> CyberEvent:
        """Converts the scraped details into a CyberEvent object."""
        entity = AffectedEntity(
            name=entity_name,
            entity_type=EntityType.OTHER,
            australian_entity=True,
            confidence_score=0.9,
        )

        data_source = EventSource(
            source_id=f"webber_{hash(url)}",
            source_type="Webber Insurance",
            url=url,
            title=title,
            content_snippet=description[:500],
            domain="webberinsurance.com.au",
            credibility_score=0.8,
            relevance_score=1.0,
            publication_date=event_date
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
