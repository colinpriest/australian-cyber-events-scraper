#!/usr/bin/env python3
"""
Test script for PDF parsing functionality.

This script validates that PDF parsing libraries are working correctly
and can extract data from OAIC reports.

Requirements:
- pdfplumber: Required (no Java needed)
- tabula-py: Optional (requires Java)
"""

import logging
import sys
import re
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def test_pdfplumber():
    """Test pdfplumber import and basic functionality."""
    try:
        import pdfplumber
        logger.info("✓ pdfplumber is installed and working")
        logger.info(f"  Version: {pdfplumber.__version__}")
        return True
    except ImportError as e:
        logger.error(f"✗ pdfplumber import failed: {e}")
        return False


def test_tabula():
    """Test tabula-py import and Java availability."""
    try:
        import tabula
        logger.info("✓ tabula-py is installed")

        # Try to check if Java is available
        try:
            import subprocess
            result = subprocess.run(['java', '-version'],
                                  capture_output=True,
                                  text=True,
                                  timeout=5)
            logger.info("✓ Java is available")
            logger.info(f"  {result.stderr.split()[2] if result.stderr else 'Unknown version'}")
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            logger.warning("✗ Java not found - tabula-py will not work")
            logger.warning("  Install Java from: https://www.java.com/en/download/")
            logger.warning("  Note: pdfplumber can be used as alternative (no Java required)")
            return False

    except ImportError as e:
        logger.error(f"✗ tabula-py import failed: {e}")
        return False


def test_pdf_text_extraction(pdf_path: str = None):
    """Test extracting text from a sample PDF."""
    import pdfplumber

    if not pdf_path:
        logger.info("\nNo test PDF provided - skipping text extraction test")
        logger.info("  To test with an OAIC PDF, run: python test_pdf_parsing.py path/to/report.pdf")
        return True

    pdf_file = Path(pdf_path)
    if not pdf_file.exists():
        logger.error(f"✗ Test PDF not found: {pdf_path}")
        return False

    try:
        logger.info(f"\n--- Testing PDF text extraction: {pdf_file.name} ---")

        with pdfplumber.open(pdf_file) as pdf:
            logger.info(f"✓ PDF opened successfully")
            logger.info(f"  Pages: {len(pdf.pages)}")

            # Extract text from first page
            first_page = pdf.pages[0]
            text = first_page.extract_text()

            if text:
                logger.info(f"✓ Text extraction successful")
                logger.info(f"  Characters extracted: {len(text)}")

                # Show first 200 characters
                preview = text[:200].replace('\n', ' ')
                logger.info(f"  Preview: {preview}...")

                # Try to find some common OAIC report patterns
                patterns = {
                    'notifications': r'(\d+)\s*notification',
                    'cyber': r'cyber\s+incident',
                    'malicious': r'malicious\s+attack',
                    'individuals': r'(\d+(?:,\d+)*)\s*individual'
                }

                logger.info("\n  Searching for OAIC data patterns:")
                for name, pattern in patterns.items():
                    matches = re.findall(pattern, text, re.IGNORECASE)
                    if matches:
                        logger.info(f"    ✓ Found {name}: {matches[:3]}")
                    else:
                        logger.info(f"    ✗ Pattern '{name}' not found")

                return True
            else:
                logger.warning("✗ No text extracted from first page")
                return False

    except Exception as e:
        logger.error(f"✗ PDF text extraction failed: {e}")
        return False


def test_pdf_table_extraction(pdf_path: str = None):
    """Test extracting tables from a sample PDF."""
    import pdfplumber

    if not pdf_path:
        logger.info("\nSkipping table extraction test (no PDF provided)")
        return True

    pdf_file = Path(pdf_path)
    if not pdf_file.exists():
        return False

    try:
        logger.info(f"\n--- Testing PDF table extraction: {pdf_file.name} ---")

        with pdfplumber.open(pdf_file) as pdf:
            tables_found = 0

            for i, page in enumerate(pdf.pages[:5]):  # Check first 5 pages
                tables = page.extract_tables()
                if tables:
                    tables_found += len(tables)
                    logger.info(f"  Page {i+1}: Found {len(tables)} table(s)")

                    # Show first table structure
                    if i == 0 and tables:
                        table = tables[0]
                        logger.info(f"    First table has {len(table)} rows")
                        if table:
                            logger.info(f"    First row: {table[0][:3]}")

            if tables_found > 0:
                logger.info(f"✓ Table extraction successful - found {tables_found} table(s)")
                return True
            else:
                logger.warning("✗ No tables found in first 5 pages")
                logger.info("  Note: OAIC reports may have tables in later pages")
                return False

    except Exception as e:
        logger.error(f"✗ PDF table extraction failed: {e}")
        return False


def download_sample_oaic_report():
    """Download a sample OAIC report for testing."""
    import requests

    # Latest report URL (as of implementation)
    sample_url = "https://www.oaic.gov.au/privacy/notifiable-data-breaches/notifiable-data-breaches-publications/notifiable-data-breaches-report-july-to-december-2024"

    logger.info("\n--- Sample OAIC Report Download ---")
    logger.info(f"To test with a real OAIC report:")
    logger.info(f"1. Visit: {sample_url}")
    logger.info(f"2. Download the PDF report")
    logger.info(f"3. Run: python test_pdf_parsing.py path/to/downloaded.pdf")
    logger.info("")
    logger.info("Or use the oaic_data_scraper.py to automatically download PDFs")


def main():
    """Run all PDF parsing tests."""
    logger.info("="*60)
    logger.info("PDF PARSING CAPABILITY TEST")
    logger.info("="*60)

    results = []

    # Test 1: pdfplumber
    results.append(("pdfplumber", test_pdfplumber()))

    # Test 2: tabula-py + Java
    results.append(("tabula-py + Java", test_tabula()))

    # Test 3: Text extraction
    pdf_path = sys.argv[1] if len(sys.argv) > 1 else None
    results.append(("Text extraction", test_pdf_text_extraction(pdf_path)))

    # Test 4: Table extraction
    results.append(("Table extraction", test_pdf_table_extraction(pdf_path)))

    # Summary
    logger.info("\n" + "="*60)
    logger.info("TEST SUMMARY")
    logger.info("="*60)

    for test_name, success in results:
        status = "✓ PASS" if success else "✗ FAIL/WARN"
        logger.info(f"{status}: {test_name}")

    passed = sum(1 for _, s in results if s)
    total = len(results)

    logger.info(f"\nTests passed: {passed}/{total}")

    if not pdf_path:
        download_sample_oaic_report()

    # Critical checks
    if not results[0][1]:  # pdfplumber must work
        logger.error("\n✗ CRITICAL: pdfplumber is not working")
        logger.error("  Run: pip install pdfplumber")
        return 1

    if not results[1][1]:  # tabula is optional
        logger.warning("\n⚠ WARNING: tabula-py/Java not available")
        logger.warning("  This is optional - pdfplumber will be used instead")
        logger.warning("  To enable tabula-py: install Java and restart terminal")

    logger.info("\n✓ PDF parsing infrastructure is ready!")
    logger.info("  Primary method: pdfplumber (no Java required)")
    logger.info("  Fallback method: tabula-py (requires Java)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
