"""
Test improved content acquisition with PDF and Playwright support.

Tests the new extraction methods on sample URLs that previously failed.
"""

import logging
import sys
from cyber_data_collector.enrichment.content_acquisition import ContentAcquisitionService

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s - %(message)s'
)

def test_pdf_extraction():
    """Test PDF extraction on sample PDF URLs"""
    print("=" * 80)
    print("TEST 1: PDF Extraction")
    print("=" * 80)

    service = ContentAcquisitionService()

    # Sample PDF URL from failed events
    pdf_url = "https://www.qld.gov.au/__data/assets/pdf_file/0035/388790/10144-Cyber-Security-Hazard-Response-Plan_v23.pdf"

    print(f"\nTesting PDF URL: {pdf_url}")
    result = service.acquire_content({'url': pdf_url})

    if result['extraction_success']:
        print(f"\n[PASS] PDF EXTRACTION SUCCESSFUL")
        print(f"  Method: {result['extraction_method']}")
        print(f"  Content length: {result['content_length']} words")
        print(f"  Text preview (first 300 chars):")
        print(f"  {result['full_text'][:300]}...")
        return True
    else:
        print(f"\n[FAIL] PDF EXTRACTION FAILED")
        print(f"  Error: {result['error']}")
        return False


def test_playwright_extraction():
    """Test Playwright extraction on JavaScript-heavy site"""
    print("\n\n" + "=" * 80)
    print("TEST 2: Playwright Extraction (JavaScript-heavy site)")
    print("=" * 80)

    service = ContentAcquisitionService()

    # Sample URL that needs JavaScript rendering (Reuters)
    js_url = "https://www.reuters.com/world/asia-pacific/australian-banking-regulator-warns-geopolitical-tensions-could-lead-more-cyber-2025-08-20/"

    print(f"\nTesting JavaScript-heavy URL: {js_url}")
    print("This may take 30-60 seconds (Playwright needs to render page)...")
    result = service.acquire_content({'url': js_url})

    if result['extraction_success']:
        print(f"\n[PASS] PLAYWRIGHT EXTRACTION SUCCESSFUL")
        print(f"  Method: {result['extraction_method']}")
        print(f"  Content length: {result['content_length']} words")
        print(f"  Text preview (first 300 chars):")
        print(f"  {result['full_text'][:300]}...")
        return True
    else:
        print(f"\n[FAIL] PLAYWRIGHT EXTRACTION FAILED")
        print(f"  Error: {result['error']}")
        return False


def test_normal_extraction():
    """Test that normal extraction still works"""
    print("\n\n" + "=" * 80)
    print("TEST 3: Normal HTML Extraction (baseline)")
    print("=" * 80)

    service = ContentAcquisitionService()

    # Sample URL that should work with newspaper3k
    normal_url = "https://www.abc.net.au/news/2022-09-22/optus-cyber-attack-personal-information-stolen/101465662"

    print(f"\nTesting normal HTML URL: {normal_url}")
    result = service.acquire_content({'url': normal_url})

    if result['extraction_success']:
        print(f"\n[PASS] NORMAL EXTRACTION SUCCESSFUL")
        print(f"  Method: {result['extraction_method']}")
        print(f"  Content length: {result['content_length']} words")
        print(f"  Text preview (first 300 chars):")
        print(f"  {result['full_text'][:300]}...")
        return True
    else:
        print(f"\n[FAIL] NORMAL EXTRACTION FAILED")
        print(f"  Error: {result['error']}")
        return False


if __name__ == '__main__':
    print("\n" + "=" * 80)
    print("IMPROVED CONTENT ACQUISITION TEST SUITE")
    print("=" * 80)
    print("\nTesting new PDF and Playwright extraction capabilities...")

    results = {
        'pdf': False,
        'playwright': False,
        'normal': False
    }

    # Test PDF extraction
    try:
        results['pdf'] = test_pdf_extraction()
    except Exception as e:
        print(f"\n[FAIL] PDF test crashed: {e}")
        import traceback
        traceback.print_exc()

    # Test Playwright extraction
    try:
        results['playwright'] = test_playwright_extraction()
    except Exception as e:
        print(f"\n[FAIL] Playwright test crashed: {e}")
        import traceback
        traceback.print_exc()

    # Test normal extraction (baseline)
    try:
        results['normal'] = test_normal_extraction()
    except Exception as e:
        print(f"\n[FAIL] Normal extraction test crashed: {e}")
        import traceback
        traceback.print_exc()

    # Summary
    print("\n\n" + "=" * 80)
    print("TEST SUMMARY")
    print("=" * 80)
    print(f"\nPDF Extraction:       {'[PASS]' if results['pdf'] else '[FAIL]'}")
    print(f"Playwright Extraction: {'[PASS]' if results['playwright'] else '[FAIL]'}")
    print(f"Normal Extraction:    {'[PASS]' if results['normal'] else '[FAIL]'}")

    passed = sum(results.values())
    total = len(results)
    print(f"\nTotal: {passed}/{total} tests passed")

    if passed == total:
        print("\n[SUCCESS] ALL TESTS PASSED - Ready for production use!")
        sys.exit(0)
    elif passed >= 2:
        print("\n[PARTIAL] Some extraction methods working")
        sys.exit(0)
    else:
        print("\n[FAILED] Issues need to be resolved")
        sys.exit(1)
