import asyncio
import json
import random
import re
import time
from typing import Optional, List, Dict, Any
import logging

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

try:
    import openai
except ImportError:
    openai = None

try:
    from cyber_data_collector.utils.pdf_extractor import PDFExtractor
except ImportError:
    PDFExtractor = None

class PlaywrightScraper:
    """
    A robust Playwright-based scraper designed to fetch web page content while
    avoiding common detection mechanisms.
    """

    def __init__(self, headless=True):
        """Initializes the scraper."""
        self.headless = headless
        self.playwright = None
        self.browser = None
        self.pdf_extractor = PDFExtractor() if PDFExtractor else None
        self.logger = logging.getLogger(__name__)

    async def __aenter__(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--disable-infobars',
                '--disable-background-networking',
                '--disable-default-apps',
                '--disable-extensions',
                '--disable-sync',
                '--disable-translate',
                '--hide-scrollbars',
                '--metrics-recording-only',
                '--mute-audio',
                '--no-first-run',
                '--safebrowsing-disable-auto-update',
                '--ignore-certificate-errors',
                '--ignore-ssl-errors',
                '--ignore-certificate-errors-spki-list',
                '--no-sandbox',
                '--disable-setuid-sandbox',
            ]
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def _get_random_user_agent(self):
        """Returns a random user agent with current 2025 versions."""
        user_agents = [
            # Current Chrome versions (Nov 2025)
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",

            # Current Firefox versions
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0",

            # Current Safari
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",

            # Current Edge
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
        ]
        return random.choice(user_agents)

    async def get_page_text(self, url: str, timeout: int = 30, event_date: str = None) -> Optional[str]:
        """
        Visits a URL and extracts the primary text content from the page.
        Supports both HTML pages and PDF files.

        Args:
            url: The URL to scrape.
            timeout: The maximum time to wait for the page to load in seconds.

        Returns:
            The extracted text content of the page, or None if scraping fails.
        """
        # Check if URL points to a PDF file
        if self.pdf_extractor and self.pdf_extractor.is_pdf_url(url):
            self.logger.info(f"Detected PDF URL, using PDF extractor: {url}")
            try:
                result = self.pdf_extractor.extract_from_url(url, timeout=timeout)
                if result and result['success']:
                    self.logger.info(f"Successfully extracted {len(result['text'])} chars from PDF using {result['extraction_method']}")
                    return result['text']
                else:
                    self.logger.warning(f"PDF extraction failed: {result.get('error')}")
                    return None
            except Exception as e:
                self.logger.error(f"PDF extraction error: {e}")
                return None

        # For sites known to heavily block scrapers, try Perplexity first
        if self._is_stubborn_site(url):
            self.logger.info(f"Stubborn site detected, trying Perplexity first: {url}")
            perplexity_content = await self._perplexity_fallback(url, event_date)
            if perplexity_content and len(perplexity_content) > 200:
                return perplexity_content
            # If Perplexity fails, still try direct scraping

        # Enhanced context with more realistic browser fingerprint
        context = await self.browser.new_context(
            user_agent=self._get_random_user_agent(),
            java_script_enabled=True,
            accept_downloads=False,
            viewport={'width': random.choice([1920, 1366, 1440]), 'height': random.choice([1080, 768, 900])},
            screen={'width': 1920, 'height': 1080},
            locale='en-AU',
            timezone_id='Australia/Sydney',
            extra_http_headers={
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-AU,en-US;q=0.9,en;q=0.8',
                'Accept-Encoding': 'gzip, deflate, br',
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'cross-site',
                'Sec-Fetch-User': '?1',
                'Cache-Control': 'max-age=0'
            }
        )

        page = await context.new_page()

        # Set additional stealth measures
        await page.add_init_script("""
            // Override webdriver property
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

            // Delete automation-related properties
            delete navigator.__proto__.webdriver;

            // Override chrome property to look like real Chrome
            window.chrome = {
                runtime: {},
                loadTimes: function() {},
                csi: function() {},
                app: {}
            };

            // Override plugins to look realistic
            Object.defineProperty(navigator, 'plugins', {
                get: () => {
                    const plugins = [
                        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                        { name: 'Native Client', filename: 'internal-nacl-plugin' }
                    ];
                    plugins.length = 3;
                    return plugins;
                }
            });

            // Override languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-AU', 'en-US', 'en']
            });

            // Override permissions query
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );

            // Override hardwareConcurrency
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });

            // Override deviceMemory
            Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

            // Override platform
            Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });

            // Override connection
            Object.defineProperty(navigator, 'connection', {
                get: () => ({
                    effectiveType: '4g',
                    rtt: 50,
                    downlink: 10,
                    saveData: false
                })
            });

            // Fix iframe contentWindow
            Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
                get: function() {
                    return window;
                }
            });
        """)

        try:
            # Special handling for Australian news sites
            if self._is_australian_news_site(url):
                await self._apply_australian_site_strategies(page, url)

            # More realistic navigation with retries for 403 errors
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    # Add referrer to appear more legitimate
                    response = await page.goto(
                        url,
                        timeout=timeout * 1000,
                        wait_until="domcontentloaded",
                        referer="https://www.google.com/"
                    )

                    # Check for 403 or other access denied responses
                    if response and response.status == 403:
                        if attempt < max_retries - 1:
                            # Wait longer between retries for 403s and try different approach
                            await asyncio.sleep(random.uniform(8, 15))
                            # Try again with different headers or user agent
                            await context.close()
                            return await self._retry_with_different_approach(url, timeout, event_date)
                        else:
                            return None
                    elif response and response.status >= 400:
                        return None
                    else:
                        break  # Success

                except PlaywrightTimeoutError:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(random.uniform(3, 8))
                        continue
                    else:
                        return None

            # Random delay to appear more human
            await asyncio.sleep(random.uniform(3, 7))

            await self._human_like_scroll(page)

            # Extended content selectors for various site types
            content_selectors = [
                # Standard article selectors
                "article", "main", ".post-content", ".entry-content", ".content",
                "div[role='main']", "#main-content", "#content", ".article-content",
                # News site specific
                ".story-content", ".article-body", ".article__body", ".story__body",
                ".news-article", ".article-text", "#article-body", ".body-content",
                # Blog/corporate site specific
                ".blog-post", ".post-body", ".page-content", "#page-content",
                ".single-post-content", ".wysiwyg-content", ".rich-text",
                # Government site specific
                ".publication-content", ".page-body", ".gov-content", "#main",
                ".rte-content", ".field-body", ".body-field",
                # Generic fallbacks
                "[itemprop='articleBody']", "[class*='article']", "[class*='content']"
            ]
            main_content_handle = None
            for selector in content_selectors:
                try:
                    main_content_handle = await page.query_selector(selector)
                    if main_content_handle:
                        inner = await main_content_handle.inner_text()
                        if inner and len(inner.strip()) > 200:  # Only use if substantial content
                            break
                        main_content_handle = None  # Reset if content too short
                except:
                    continue

            if main_content_handle:
                text = await main_content_handle.inner_text()
            else:
                text = await page.locator('body').inner_text()

            cleaned_text = self._clean_text(text)
            return cleaned_text

        except PlaywrightTimeoutError:
            # Try Perplexity fallback for timeouts
            return await self._perplexity_fallback(url, event_date)
        except Exception as e:
            # Check if it's a 403/404 error and try Perplexity fallback
            if "403" in str(e) or "404" in str(e) or "Forbidden" in str(e) or "Not Found" in str(e):
                return await self._perplexity_fallback(url, event_date)
            return None
        finally:
            await page.close()
            await context.close()

    async def _human_like_scroll(self, page):
        """Simulates human-like scrolling."""
        total_height = await page.evaluate("document.body.scrollHeight")
        for i in range(0, total_height, random.randint(300, 500)):
            await page.evaluate(f"window.scrollTo(0, {i});")
            await asyncio.sleep(random.uniform(0.2, 0.6))

    def _clean_text(self, text: str) -> str:
        """Cleans the extracted text by removing excessive whitespace and non-printable chars."""
        if not text:
            return ""
        text = re.sub(r'[ \t\r\f\v]+', ' ', text)
        text = re.sub(r'(\n ?)+', '\n', text)
        return "".join(char for char in text if char.isprintable() or char in '\n\t')

    async def close(self):
        """Closes the browser with timeout to prevent hanging on Windows."""
        try:
            if self.browser:
                try:
                    await asyncio.wait_for(self.browser.close(), timeout=10.0)
                except asyncio.TimeoutError:
                    self.logger.warning("Browser close timed out after 10s, forcing cleanup")
                except Exception as e:
                    self.logger.debug(f"Browser close error (non-fatal): {e}")

            if self.playwright:
                try:
                    await asyncio.wait_for(self.playwright.stop(), timeout=10.0)
                except asyncio.TimeoutError:
                    self.logger.warning("Playwright stop timed out after 10s")
                except Exception as e:
                    self.logger.debug(f"Playwright stop error (non-fatal): {e}")
        except Exception as e:
            self.logger.warning(f"Error during browser cleanup: {e}")

    def _is_australian_news_site(self, url: str) -> bool:
        """Check if the URL is from a known Australian news site."""
        australian_domains = [
            'abc.net.au', 'news.com.au', 'theage.com.au', 'smh.com.au',
            'theaustralian.com.au', 'theguardian.com/australia-news',
            'thenewdaily.com.au', 'canberratimes.com.au', 'adelaidenow.com.au',
            'heraldsun.com.au', 'couriermail.com.au', 'perthnow.com.au',
            'ntnews.com.au', 'themercury.com.au', 'thewest.com.au'
        ]
        return any(domain in url.lower() for domain in australian_domains)

    def _is_stubborn_site(self, url: str) -> bool:
        """Check if URL is from a site known to block scrapers."""
        stubborn_domains = [
            'nytimes.com', 'reuters.com', 'facebook.com', 'twitter.com',
            'linkedin.com', 'news.com.au', 'theaustralian.com.au',
            'afr.com', 'wsj.com', 'ft.com', 'bloomberg.com'
        ]
        return any(domain in url.lower() for domain in stubborn_domains)

    async def _apply_australian_site_strategies(self, page, url: str):
        """Apply specific strategies for Australian news sites."""
        # Some Australian sites detect automation, so add more realistic behavior
        if 'thenewdaily.com.au' in url:
            # The New Daily is particularly strict
            await page.set_extra_http_headers({
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-AU,en-US;q=0.7,en;q=0.3',
                'Sec-GPC': '1'
            })
        elif 'abc.net.au' in url:
            # ABC can be sensitive to rapid requests
            await asyncio.sleep(random.uniform(2, 4))

    async def _retry_with_different_approach(self, url: str, timeout: int, event_date: str = None) -> Optional[str]:
        """Retry scraping with a completely different browser context."""
        # Create a new context with different fingerprint
        context = await self.browser.new_context(
            user_agent=self._get_random_user_agent(),
            java_script_enabled=True,
            accept_downloads=False,
            viewport={'width': 1366, 'height': 768},  # Different viewport
            locale='en-AU',  # Australian locale for Australian sites
            timezone_id='Australia/Sydney',
            extra_http_headers={
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-AU,en;q=0.5',
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache'
            }
        )

        page = await context.new_page()
        try:
            # More aggressive wait and different approach
            await asyncio.sleep(random.uniform(5, 10))

            response = await page.goto(
                url,
                timeout=timeout * 1000,
                wait_until="networkidle",  # Wait for network to be idle
                referer="https://www.google.com.au/"  # Australian Google
            )

            if response and response.status >= 400:
                # Try Perplexity fallback for HTTP errors
                await page.close()
                await context.close()
                return await self._perplexity_fallback(url, event_date)

            # Longer wait and scroll
            await asyncio.sleep(random.uniform(5, 8))
            await self._human_like_scroll(page)

            # Try to extract content
            content_selectors = ["article", "main", ".post-content", ".entry-content", ".content"]
            main_content_handle = None
            for selector in content_selectors:
                main_content_handle = await page.query_selector(selector)
                if main_content_handle:
                    break

            if main_content_handle:
                text = await main_content_handle.inner_text()
            else:
                text = await page.locator('body').inner_text()

            return self._clean_text(text)

        except Exception:
            return None
        finally:
            await page.close()
            await context.close()

    async def _perplexity_fallback(self, failed_url: str, event_date: str = None) -> Optional[str]:
        """Use Perplexity to find alternative URLs when original URL fails."""
        if not openai:
            return None

        try:
            # Get Perplexity API key from environment
            import os
            api_key = os.environ.get('PERPLEXITY_API_KEY')
            if not api_key:
                return None

            client = openai.OpenAI(
                api_key=api_key,
                base_url="https://api.perplexity.ai"
            )

            # Extract date context from the failed URL or use provided date
            date_context = self._extract_date_context(failed_url, event_date)

            # Create a comprehensive query with exact URL and date context
            query = f"""Find alternative URLs for the exact same news story that was originally published at this URL: {failed_url}

Date context: {date_context}

I need you to:
1. Identify what specific cybersecurity/data breach story this URL was about
2. Find the SAME story covered by other reputable news sources
3. Return working URLs that contain the same story content
4. Focus on major news outlets like Reuters, Guardian, BBC, ABC News, ZDNet, etc.

The original URL is broken/inaccessible, so I need alternative sources covering the identical incident."""

            response = client.chat.completions.create(
                model="sonar-pro",
                messages=[
                    {
                        "role": "system",
                        "content": """You are a news research specialist. When given a broken/inaccessible URL, you find working alternative URLs that cover the exact same news story.

IMPORTANT:
- Analyze the provided URL carefully to understand what story it was about
- Use the date context to narrow down the timeframe
- Find the SAME story from different reputable news sources
- Return actual working URLs, not the broken one
- Focus on cybersecurity/data breach stories if indicated

Return your response as a simple list of alternative URLs, one per line. Only include URLs that you're confident cover the same story."""
                    },
                    {
                        "role": "user",
                        "content": query
                    }
                ],
                temperature=0.1,
                max_tokens=1500,
            )

            content = response.choices[0].message.content
            if not content:
                return None

            # Try to extract URLs from the response
            alternative_urls = self._extract_urls_from_response(content)

            # Try each alternative URL
            for alt_url in alternative_urls[:3]:  # Try up to 3 alternatives
                try:
                    # Simple attempt with basic headers to test if URL works
                    alt_content = await self._simple_url_test(alt_url)
                    if alt_content and len(alt_content) > 500:  # Valid content
                        # Update the database to record the alternative URL
                        await self._record_alternative_url(failed_url, alt_url)
                        return alt_content
                except Exception:
                    continue

            return None

        except Exception as e:
            logger.debug(f"Perplexity fallback failed: {e}")
            return None

    def _extract_story_hint_from_url(self, url: str) -> str:
        """Extract story hints from URL for Perplexity search."""
        # Extract meaningful parts from URL
        import urllib.parse
        parsed = urllib.parse.urlparse(url)
        path_parts = parsed.path.split('/')

        # Look for story identifiers in URL
        story_parts = []
        for part in path_parts:
            if part and not part.isdigit() and len(part) > 3:
                # Clean up common URL patterns
                cleaned = part.replace('-', ' ').replace('_', ' ')
                if any(keyword in cleaned.lower() for keyword in ['breach', 'hack', 'cyber', 'data', 'security']):
                    story_parts.append(cleaned)

        if story_parts:
            return ' '.join(story_parts[:3])  # First 3 relevant parts

        # Fallback to domain for context
        domain = parsed.netloc.replace('www.', '')
        return f"cybersecurity story from {domain}"

    def _extract_date_context(self, url: str, event_date: str = None) -> str:
        """Extract or construct date context for Perplexity search."""
        if event_date:
            return f"Article published around {event_date}"

        # Try to extract date from URL
        import re
        from datetime import datetime

        # Look for date patterns in URL
        date_patterns = [
            r'/(\d{4})/(\d{1,2})/(\d{1,2})/',  # /2020/02/11/
            r'/(\d{4})-(\d{1,2})-(\d{1,2})',   # /2020-02-11
            r'(\d{4})(\d{2})(\d{2})',          # 20200211
        ]

        for pattern in date_patterns:
            match = re.search(pattern, url)
            if match:
                try:
                    year, month, day = match.groups()
                    date_obj = datetime(int(year), int(month), int(day))
                    return f"Article published on or around {date_obj.strftime('%B %Y')} ({date_obj.strftime('%Y-%m-%d')})"
                except (ValueError, TypeError):
                    continue

        # Look for year in URL as fallback
        year_match = re.search(r'/(20\d{2})/', url)
        if year_match:
            year = year_match.group(1)
            return f"Article published sometime in {year}"

        # Generic fallback
        return "Article published in recent years (exact date unknown)"

    def _extract_urls_from_response(self, content: str) -> List[str]:
        """Extract URLs from Perplexity response."""
        import re

        # Look for URLs in the response
        url_pattern = r'https?://[^\s\[\]<>"]+[^\s\[\]<>".,;!?]'
        urls = re.findall(url_pattern, content)

        # Filter for relevant domains (news sites)
        news_domains = [
            'abc.net.au', 'news.com.au', 'theage.com.au', 'smh.com.au',
            'theguardian.com', 'reuters.com', 'bbc.com', 'cnn.com',
            'zdnet.com', 'techcrunch.com', 'ars-technica.com', 'wired.com',
            'cybersecuritydive.com', 'securityweek.com', 'darkreading.com'
        ]

        relevant_urls = []
        for url in urls:
            if any(domain in url.lower() for domain in news_domains):
                relevant_urls.append(url)

        return relevant_urls

    async def _simple_url_test(self, url: str) -> Optional[str]:
        """Simple test to see if a URL returns valid content."""
        try:
            context = await self.browser.new_context(
                user_agent=self._get_random_user_agent()
            )
            page = await context.new_page()

            response = await page.goto(url, timeout=15000, wait_until="domcontentloaded")

            if response and response.status < 400:
                # Quick content extraction
                text = await page.locator('body').inner_text()
                await page.close()
                await context.close()
                return self._clean_text(text)

            await page.close()
            await context.close()
            return None

        except Exception:
            return None

    async def _record_alternative_url(self, original_url: str, alternative_url: str):
        """Record the successful alternative URL for future reference."""
        try:
            # This could be logged to a file or database for future use
            logger.debug(f"ALTERNATIVE URL FOUND: {original_url} -> {alternative_url}")

            # Could implement database storage here:
            # - Store mapping of failed URLs to working alternatives
            # - Use for future scraping attempts
            # - Track success rates of different news sources

        except Exception:
            pass