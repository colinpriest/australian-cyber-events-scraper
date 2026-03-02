"""
PDF Extraction Utility - Extract text from PDF files and URLs.

Supports multiple extraction methods with automatic fallback:
1. pdfplumber (primary) - Best for modern PDFs with proper text layers
2. PyPDF2 (fallback) - Lightweight alternative for simple PDFs
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any, Dict, Optional
from pathlib import Path

import requests


class PDFExtractor:
    """Extract text content from PDF files and URLs"""

    def __init__(self) -> None:
        self.logger = logging.getLogger(__name__)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })

    def close(self) -> None:
        """Close the HTTP session."""
        self.session.close()

    def __enter__(self) -> PDFExtractor:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def is_pdf_url(self, url: str) -> bool:
        """Check if URL points to a PDF file"""
        url_lower = url.lower()
        return (
            url_lower.endswith('.pdf') or
            '/pdf/' in url_lower or
            'contenttype=application/pdf' in url_lower or
            'filetype=pdf' in url_lower
        )

    def extract_from_url(self, url: str, timeout: int = 30) -> Optional[Dict[str, Any]]:
        """
        Download and extract text from a PDF URL.

        Args:
            url: URL to PDF file
            timeout: Request timeout in seconds

        Returns:
            {
                'text': str,           # Extracted text content
                'pages': int,          # Number of pages
                'extraction_method': str,  # Method used
                'success': bool,
                'error': str or None
            }
        """
        try:
            # Download PDF to temporary file
            self.logger.info(f"Downloading PDF from {url}")
            response = self.session.get(url, timeout=timeout, stream=True)
            response.raise_for_status()

            # Verify content type
            content_type = response.headers.get('content-type', '').lower()
            if 'pdf' not in content_type and not self.is_pdf_url(url):
                return self._error_result(f"URL does not appear to be a PDF (content-type: {content_type})")

            # Save to temporary file
            with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp_file:
                for chunk in response.iter_content(chunk_size=8192):
                    tmp_file.write(chunk)
                tmp_path = tmp_file.name

            try:
                # Extract text from temporary file
                result = self.extract_from_file(tmp_path)
                return result
            finally:
                # Clean up temporary file
                try:
                    os.unlink(tmp_path)
                except Exception as exc:
                    self.logger.warning("Failed to delete temporary PDF %s: %s", tmp_path, exc)

        except requests.RequestException as e:
            return self._error_result(f"Failed to download PDF: {e}")
        except Exception as e:
            return self._error_result(f"Unexpected error: {e}")

    def extract_from_file(self, file_path: str) -> Dict[str, Any]:
        """
        Extract text from a local PDF file.

        Args:
            file_path: Path to PDF file

        Returns:
            Same format as extract_from_url()
        """
        if not os.path.exists(file_path):
            return self._error_result(f"File not found: {file_path}")

        # Try pdfplumber first (best quality)
        result = self._extract_with_pdfplumber(file_path)
        if result and result['success'] and len(result['text']) > 100:
            return result

        # Fallback to PyPDF2
        result = self._extract_with_pypdf2(file_path)
        if result and result['success']:
            return result

        return self._error_result("All PDF extraction methods failed")

    def _extract_with_pdfplumber(self, file_path: str) -> Optional[Dict[str, Any]]:
        """Extract text using pdfplumber (primary method)"""
        try:
            import pdfplumber
        except ImportError:
            self.logger.debug("pdfplumber not installed, skipping")
            return None

        try:
            text_parts = []
            page_count = 0

            with pdfplumber.open(file_path) as pdf:
                page_count = len(pdf.pages)

                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)

            full_text = '\n\n'.join(text_parts)

            if len(full_text.strip()) < 50:
                return self._error_result("Extracted text too short (possible image-based PDF)")

            self.logger.info(f"Extracted {len(full_text)} chars from {page_count} pages using pdfplumber")

            return {
                'text': full_text,
                'pages': page_count,
                'extraction_method': 'pdfplumber',
                'success': True,
                'error': None
            }

        except Exception as e:
            self.logger.warning(f"pdfplumber extraction failed: {e}")
            return None

    def _extract_with_pypdf2(self, file_path: str) -> Optional[Dict[str, Any]]:
        """Extract text using PyPDF2 (fallback method)"""
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            self.logger.debug("PyPDF2 not installed, skipping")
            return None

        try:
            text_parts = []

            reader = PdfReader(file_path)
            page_count = len(reader.pages)

            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)

            full_text = '\n\n'.join(text_parts)

            if len(full_text.strip()) < 50:
                return self._error_result("Extracted text too short (possible image-based PDF)")

            self.logger.info(f"Extracted {len(full_text)} chars from {page_count} pages using PyPDF2")

            return {
                'text': full_text,
                'pages': page_count,
                'extraction_method': 'PyPDF2',
                'success': True,
                'error': None
            }

        except Exception as e:
            self.logger.warning(f"PyPDF2 extraction failed: {e}")
            return None

    def _error_result(self, error_message: str) -> Dict[str, Any]:
        """Return error result structure"""
        self.logger.error(error_message)
        return {
            'text': '',
            'pages': 0,
            'extraction_method': 'none',
            'success': False,
            'error': error_message
        }


def test_pdf_extractor():
    """Test the PDF extractor with sample URLs"""
    extractor = PDFExtractor()

    # Test URL detection
    test_urls = [
        'https://www.qld.gov.au/file.pdf',
        'https://example.com/document?filetype=pdf',
        'https://example.com/article.html',
    ]

    print("PDF URL Detection:")
    for url in test_urls:
        is_pdf = extractor.is_pdf_url(url)
        print(f"  {url}: {'PDF' if is_pdf else 'Not PDF'}")

    # Test actual extraction (would need a real PDF URL)
    print("\nTo test extraction, provide a PDF URL:")
    print("  result = extractor.extract_from_url('https://example.com/sample.pdf')")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    test_pdf_extractor()
