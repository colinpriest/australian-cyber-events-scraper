#!/usr/bin/env python3
"""
Analyze OAIC PDF content to understand structure and improve extraction patterns.

This script examines downloaded OAIC PDFs to:
1. Identify where key data is located (which pages)
2. Understand table structures
3. Find text patterns for median/average statistics
4. Determine if OCR is needed
"""

import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Any

try:
    import pdfplumber
except ImportError:
    print("Error: pdfplumber not installed. Run: pip install pdfplumber")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def analyze_pdf_structure(pdf_path: str) -> Dict[str, Any]:
    """Analyze PDF structure and content."""

    analysis = {
        'filename': Path(pdf_path).name,
        'total_pages': 0,
        'pages_with_text': 0,
        'pages_with_tables': 0,
        'total_text_chars': 0,
        'tables_found': [],
        'text_samples': {},
        'key_patterns_found': {}
    }

    try:
        with pdfplumber.open(pdf_path) as pdf:
            analysis['total_pages'] = len(pdf.pages)

            for page_num, page in enumerate(pdf.pages, start=1):
                # Extract text
                text = page.extract_text()
                if text and len(text.strip()) > 10:
                    analysis['pages_with_text'] += 1
                    analysis['total_text_chars'] += len(text)

                    # Store sample text from first few content pages
                    if len(analysis['text_samples']) < 5:
                        preview = text[:500].replace('\n', ' ')
                        analysis['text_samples'][f'page_{page_num}'] = preview

                    # Look for key patterns
                    text_lower = text.lower()

                    if 'individual' in text_lower and 'affected' in text_lower:
                        if 'individuals_affected' not in analysis['key_patterns_found']:
                            analysis['key_patterns_found']['individuals_affected'] = []
                        analysis['key_patterns_found']['individuals_affected'].append({
                            'page': page_num,
                            'snippet': text[max(0, text_lower.index('individual')-50):
                                          text_lower.index('individual')+200]
                        })

                    if 'median' in text_lower:
                        if 'median' not in analysis['key_patterns_found']:
                            analysis['key_patterns_found']['median'] = []
                        analysis['key_patterns_found']['median'].append({
                            'page': page_num,
                            'snippet': text[max(0, text_lower.index('median')-50):
                                          text_lower.index('median')+150]
                        })

                    if 'average' in text_lower or 'mean' in text_lower:
                        if 'average' not in analysis['key_patterns_found']:
                            analysis['key_patterns_found']['average'] = []
                        keyword = 'average' if 'average' in text_lower else 'mean'
                        analysis['key_patterns_found']['average'].append({
                            'page': page_num,
                            'snippet': text[max(0, text_lower.index(keyword)-50):
                                          text_lower.index(keyword)+150]
                        })

                    if 'sector' in text_lower or 'industry' in text_lower or 'entity type' in text_lower:
                        if 'sectors' not in analysis['key_patterns_found']:
                            analysis['key_patterns_found']['sectors'] = []
                        keyword = 'sector' if 'sector' in text_lower else ('industry' if 'industry' in text_lower else 'entity type')
                        analysis['key_patterns_found']['sectors'].append({
                            'page': page_num,
                            'snippet': text[max(0, text_lower.index(keyword)-50):
                                          text_lower.index(keyword)+200]
                        })

                # Extract tables
                tables = page.extract_tables()
                if tables:
                    analysis['pages_with_tables'] += 1

                    for table_idx, table in enumerate(tables):
                        if not table or len(table) < 2:
                            continue

                        # Analyze table structure
                        header = table[0] if table else []
                        table_info = {
                            'page': page_num,
                            'table_index': table_idx,
                            'rows': len(table),
                            'cols': len(header) if header else 0,
                            'header': [str(cell) if cell else '' for cell in header],
                            'sample_rows': []
                        }

                        # Get a few sample rows
                        for row in table[1:min(4, len(table))]:
                            table_info['sample_rows'].append([str(cell) if cell else '' for cell in row])

                        analysis['tables_found'].append(table_info)

        return analysis

    except Exception as e:
        logger.error(f"Error analyzing PDF: {e}")
        return analysis


def print_analysis_report(analysis: Dict[str, Any]):
    """Print a formatted analysis report."""

    print("\n" + "="*80)
    print(f"PDF ANALYSIS REPORT: {analysis['filename']}")
    print("="*80)

    print(f"\n[DOCUMENT OVERVIEW]")
    print(f"  Total pages: {analysis['total_pages']}")
    print(f"  Pages with text: {analysis['pages_with_text']}")
    print(f"  Pages with tables: {analysis['pages_with_tables']}")
    print(f"  Total text characters: {analysis['total_text_chars']:,}")
    print(f"  Average chars per page: {analysis['total_text_chars'] // max(1, analysis['pages_with_text']):,}")

    if analysis['total_text_chars'] < 1000:
        print("\n  WARNING: Very little text found - PDF may be image-based")
        print("     Consider using OCR (pytesseract) for text extraction")

    print(f"\n[TABLES FOUND]: {len(analysis['tables_found'])} total")
    for table in analysis['tables_found']:
        print(f"\n  Table on page {table['page']} (table #{table['table_index'] + 1}):")
        print(f"    Size: {table['rows']} rows × {table['cols']} columns")
        print(f"    Header: {table['header']}")
        if table['sample_rows']:
            print(f"    Sample rows:")
            for i, row in enumerate(table['sample_rows'][:2], 1):
                print(f"      Row {i}: {row}")

    print(f"\n[KEY PATTERNS FOUND]:")
    if analysis['key_patterns_found']:
        for pattern_name, occurrences in analysis['key_patterns_found'].items():
            print(f"\n  {pattern_name.upper()}: Found on {len(occurrences)} page(s)")
            for occ in occurrences[:2]:  # Show first 2 occurrences
                print(f"    Page {occ['page']}: {occ['snippet'][:150]}...")
    else:
        print("  WARNING: No key patterns found in text")

    print(f"\n[TEXT SAMPLES]:")
    for page_label, sample in list(analysis['text_samples'].items())[:3]:
        print(f"\n  {page_label}: {sample[:200]}...")

    print("\n" + "="*80)


def analyze_all_pdfs(pdf_dir: str = "oaic_pdfs"):
    """Analyze all PDFs in the directory."""

    pdf_path = Path(pdf_dir)
    if not pdf_path.exists():
        logger.error(f"PDF directory not found: {pdf_dir}")
        return

    pdf_files = list(pdf_path.glob("*.pdf"))

    if not pdf_files:
        logger.warning(f"No PDF files found in {pdf_dir}")
        return

    logger.info(f"Found {len(pdf_files)} PDF file(s) to analyze\n")

    all_analyses = []

    for pdf_file in pdf_files:
        logger.info(f"Analyzing: {pdf_file.name}")
        analysis = analyze_pdf_structure(str(pdf_file))
        print_analysis_report(analysis)
        all_analyses.append(analysis)

    # Save detailed analysis to JSON
    output_file = "pdf_analysis_results.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(all_analyses, f, indent=2, ensure_ascii=False)

    logger.info(f"\nDetailed analysis saved to: {output_file}")

    # Print summary recommendations
    print("\n" + "="*80)
    print("RECOMMENDATIONS FOR PATTERN IMPROVEMENT")
    print("="*80)

    avg_text = sum(a['total_text_chars'] for a in all_analyses) / len(all_analyses)
    total_tables = sum(len(a['tables_found']) for a in all_analyses)

    print(f"\n[SUMMARY] Across {len(all_analyses)} PDF(s):")
    print(f"  Average text per PDF: {avg_text:,.0f} characters")
    print(f"  Total tables found: {total_tables}")

    if avg_text < 5000:
        print("\n[WARNING] LOW TEXT EXTRACTION DETECTED")
        print("  Recommendation: PDFs appear to be image-heavy or have complex layouts")
        print("  Options:")
        print("    1. Add OCR support (pip install pytesseract)")
        print("    2. Use alternative data source (HTML pages, API)")
        print("    3. Manual data entry for key statistics")
    else:
        print("\n[SUCCESS] Good text extraction - patterns can be refined")

    # Check for key data
    has_individuals = any('individuals_affected' in a.get('key_patterns_found', {}) for a in all_analyses)
    has_median = any('median' in a.get('key_patterns_found', {}) for a in all_analyses)
    has_sectors = any('sectors' in a.get('key_patterns_found', {}) for a in all_analyses)

    print("\n[DATA AVAILABILITY]:")
    print(f"  Individuals affected data: {'[FOUND]' if has_individuals else '[NOT FOUND]'}")
    print(f"  Median/average statistics: {'[FOUND]' if has_median else '[NOT FOUND]'}")
    print(f"  Sector/industry data: {'[FOUND]' if has_sectors else '[NOT FOUND]'}")

    if total_tables > 0:
        print("\n[SUCCESS] Tables detected - extraction functions should work with pattern adjustments")
        print("  Next steps:")
        print("    1. Examine table headers in analysis output")
        print("    2. Update header matching patterns in extraction functions")
        print("    3. Test on specific pages where tables were found")

    print("="*80)


def main():
    """Main function."""
    import argparse

    parser = argparse.ArgumentParser(
        description='Analyze OAIC PDF content structure'
    )
    parser.add_argument('--pdf-dir', default='oaic_pdfs',
                       help='Directory containing PDFs (default: oaic_pdfs)')
    parser.add_argument('--pdf-file', help='Analyze a specific PDF file')

    args = parser.parse_args()

    if args.pdf_file:
        logger.info(f"Analyzing single PDF: {args.pdf_file}")
        analysis = analyze_pdf_structure(args.pdf_file)
        print_analysis_report(analysis)

        # Save to JSON
        output_file = "pdf_analysis_results.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump([analysis], f, indent=2, ensure_ascii=False)
        logger.info(f"\nDetailed analysis saved to: {output_file}")
    else:
        analyze_all_pdfs(args.pdf_dir)


if __name__ == "__main__":
    main()
