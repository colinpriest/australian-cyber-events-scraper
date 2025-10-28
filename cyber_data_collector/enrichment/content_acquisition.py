"""
Content Acquisition Service - Fetch and clean full article text from URLs.

This module uses multiple extraction methods to get complete article content
instead of relying on title/summary only.
"""

import logging
from typing import Dict, Any, Optional
from datetime import datetime
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
import time


class ContentAcquisitionService:
    """Fetch and clean article content from URLs using multiple extraction methods"""

    TRUSTED_SOURCES = {
        # Australian news sources
        'abc.net.au': 1.0,
        'smh.com.au': 0.95,
        'theage.com.au': 0.95,
        'afr.com': 0.95,
        'news.com.au': 0.85,
        'theaustralian.com.au': 0.9,
        '9news.com.au': 0.85,
        '7news.com.au': 0.85,

        # Tech news sources
        'zdnet.com': 0.9,
        'arstechnica.com': 0.9,
        'techcrunch.com': 0.85,
        'theverge.com': 0.85,

        # Cybersecurity sources
        'itnews.com.au': 0.9,
        'cyberdaily.au': 0.85,
        'bleepingcomputer.com': 0.85,
        'cyberscoop.com': 0.85,
        'krebsonsecurity.com': 0.95,
        'threatpost.com': 0.85,
        'darkreading.com': 0.85,

        # Government sources
        'oaic.gov.au': 1.0,
        'cyber.gov.au': 1.0,
        'acsc.gov.au': 1.0,
    }

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })

    def acquire_content(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """
        Fetch full article text and extract clean content.

        Args:
            event: Event dict with at minimum 'url' field

        Returns:
            {
                'full_text': str,          # Complete article text
                'clean_summary': str,      # 2-3 paragraph summary
                'publication_date': str,   # Article publication date
                'source_domain': str,      # Domain of source
                'source_reliability': float, # Source credibility score
                'content_length': int,     # Word count
                'extraction_method': str,  # How content was extracted
                'extraction_success': bool,
                'error': str or None
            }
        """
        url = event.get('url')

        if not url:
            return self._error_result("No URL provided")

        # Try extraction methods in order
        extraction_result = None

        # Method 1: Try newspaper3k (best for news articles)
        try:
            extraction_result = self._extract_with_newspaper3k(url)
            if extraction_result and len(extraction_result.get('full_text', '')) > 200:
                extraction_result['extraction_method'] = 'newspaper3k'
                self.logger.info(f"Extracted {len(extraction_result['full_text'])} chars using newspaper3k")
        except Exception as e:
            self.logger.warning(f"newspaper3k failed for {url}: {e}")

        # Method 2: Try trafilatura (fallback for difficult sites)
        if not extraction_result or len(extraction_result.get('full_text', '')) < 200:
            try:
                extraction_result = self._extract_with_trafilatura(url)
                if extraction_result and len(extraction_result.get('full_text', '')) > 200:
                    extraction_result['extraction_method'] = 'trafilatura'
                    self.logger.info(f"Extracted {len(extraction_result['full_text'])} chars using trafilatura")
            except Exception as e:
                self.logger.warning(f"trafilatura failed for {url}: {e}")

        # Method 3: BeautifulSoup (last resort)
        if not extraction_result or len(extraction_result.get('full_text', '')) < 200:
            try:
                extraction_result = self._extract_with_beautifulsoup(url)
                if extraction_result:
                    extraction_result['extraction_method'] = 'beautifulsoup'
                    self.logger.info(f"Extracted {len(extraction_result['full_text'])} chars using beautifulsoup")
            except Exception as e:
                self.logger.warning(f"beautifulsoup failed for {url}: {e}")

        if not extraction_result or len(extraction_result.get('full_text', '')) < 100:
            return self._error_result(f"Failed to extract sufficient content from {url}")

        # Assess source reliability
        domain = self._extract_domain(url)
        reliability = self.TRUSTED_SOURCES.get(domain, 0.6)  # Default moderate trust

        # Calculate content quality metrics
        full_text = extraction_result['full_text']
        word_count = len(full_text.split())

        result = {
            'full_text': full_text,
            'clean_summary': extraction_result.get('summary') or event.get('summary') or self._generate_summary(full_text),
            'publication_date': extraction_result.get('publication_date'),
            'source_domain': domain,
            'source_reliability': reliability,
            'content_length': word_count,
            'extraction_method': extraction_result['extraction_method'],
            'extraction_success': True,
            'error': None
        }

        return result

    def _extract_with_newspaper3k(self, url: str) -> Optional[Dict[str, Any]]:
        """Extract content using newspaper3k library"""
        try:
            from newspaper import Article
        except ImportError:
            self.logger.warning("newspaper3k not installed, skipping this method")
            return None

        article = Article(url)
        article.download()
        article.parse()

        if not article.text or len(article.text) < 100:
            return None

        return {
            'full_text': article.text,
            'summary': article.summary if hasattr(article, 'summary') else None,
            'publication_date': article.publish_date.isoformat() if article.publish_date else None,
        }

    def _extract_with_trafilatura(self, url: str) -> Optional[Dict[str, Any]]:
        """Extract content using trafilatura library"""
        try:
            import trafilatura
        except ImportError:
            self.logger.warning("trafilatura not installed, skipping this method")
            return None

        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None

        text = trafilatura.extract(downloaded, include_comments=False)

        if not text or len(text) < 100:
            return None

        # Try to extract metadata
        metadata = trafilatura.extract_metadata(downloaded)

        return {
            'full_text': text,
            'summary': None,  # trafilatura doesn't generate summaries
            'publication_date': metadata.date if metadata and metadata.date else None,
        }

    def _extract_with_beautifulsoup(self, url: str) -> Optional[Dict[str, Any]]:
        """Extract content using BeautifulSoup (last resort)"""

        response = self.session.get(url, timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, 'html.parser')

        # Remove script and style elements
        for script in soup(['script', 'style', 'nav', 'header', 'footer', 'aside']):
            script.decompose()

        # Try to find article content
        article_content = None

        # Common article selectors
        article_selectors = [
            'article',
            '.article-content',
            '.post-content',
            '.entry-content',
            '#content',
            '.content',
            'main',
        ]

        for selector in article_selectors:
            elements = soup.select(selector)
            if elements:
                article_content = elements[0]
                break

        if not article_content:
            # Fallback: get all paragraph text
            article_content = soup

        # Extract text from paragraphs
        paragraphs = article_content.find_all('p')
        text = '\n\n'.join(p.get_text().strip() for p in paragraphs if p.get_text().strip())

        if len(text) < 100:
            return None

        return {
            'full_text': text,
            'summary': None,
            'publication_date': None,
        }

    def _generate_summary(self, full_text: str, max_length: int = 500) -> str:
        """Generate a simple summary by taking first N characters"""
        if len(full_text) <= max_length:
            return full_text

        # Try to break at sentence boundary
        summary = full_text[:max_length]
        last_period = summary.rfind('.')

        if last_period > max_length * 0.7:  # If we can break at a sentence within 70% of max_length
            summary = summary[:last_period + 1]

        return summary.strip()

    def _extract_domain(self, url: str) -> str:
        """Extract domain from URL"""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc
            # Remove www. prefix
            if domain.startswith('www.'):
                domain = domain[4:]
            return domain
        except:
            return 'unknown'

    def _error_result(self, error_message: str) -> Dict[str, Any]:
        """Return error result structure"""
        self.logger.error(error_message)
        return {
            'full_text': '',
            'clean_summary': '',
            'publication_date': None,
            'source_domain': 'unknown',
            'source_reliability': 0.0,
            'content_length': 0,
            'extraction_method': 'none',
            'extraction_success': False,
            'error': error_message
        }


def test_content_acquisition():
    """Test the content acquisition service"""
    service = ContentAcquisitionService()

    # Test URLs
    test_urls = [
        'https://www.abc.net.au/news/2022-09-22/optus-cyber-attack-personal-information-stolen/101465662',
        'https://www.itnews.com.au/news/medibank-confirms-data-breach-affecting-all-customers-583593',
    ]

    for url in test_urls:
        print(f"\n{'='*80}")
        print(f"Testing: {url}")
        print(f"{'='*80}\n")

        result = service.acquire_content({'url': url})

        if result['extraction_success']:
            print(f"✓ Extraction successful")
            print(f"  Method: {result['extraction_method']}")
            print(f"  Domain: {result['source_domain']}")
            print(f"  Reliability: {result['source_reliability']}")
            print(f"  Content length: {result['content_length']} words")
            print(f"  Publication date: {result['publication_date']}")
            print(f"\nFirst 300 chars:")
            print(result['full_text'][:300])
        else:
            print(f"✗ Extraction failed: {result['error']}")


if __name__ == '__main__':
    # Set up logging
    logging.basicConfig(level=logging.INFO)
    test_content_acquisition()
