#!/usr/bin/env python3
"""
Extract specific data points from OAIC PDF files using pdfplumber.
"""
import pdfplumber
import re
from pathlib import Path

def extract_text_from_pdf(pdf_path):
    """Extract all text from PDF with page numbers."""
    pages_text = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            text = page.extract_text()
            pages_text.append((i, text))
    return pages_text

def search_for_patterns(pages_text, patterns, context_words=10):
    """Search for patterns and return matches with context."""
    results = []
    for page_num, text in pages_text:
        lines = text.split('\n')
        for line_idx, line in enumerate(lines):
            for pattern_name, pattern in patterns.items():
                matches = re.finditer(pattern, line, re.IGNORECASE)
                for match in matches:
                    # Get context (surrounding lines)
                    start_idx = max(0, line_idx - 2)
                    end_idx = min(len(lines), line_idx + 3)
                    context = '\n'.join(lines[start_idx:end_idx])

                    results.append({
                        'pattern': pattern_name,
                        'match': match.group(0),
                        'page': page_num,
                        'line': line,
                        'context': context
                    })
    return results

def extract_2024_h1_stats(pdf_path):
    """Extract average and median from 2024 H1 PDF."""
    print("\n" + "="*80)
    print("2024 H1 PDF - Average and Median Individuals Affected")
    print("="*80)

    pages_text = extract_text_from_pdf(pdf_path)

    # Search for average and median patterns
    patterns = {
        'average': r'\baverage\b.*?\d+[,\d]*',
        'mean': r'\bmean\b.*?\d+[,\d]*',
        'median': r'\bmedian\b.*?\d+[,\d]*',
        'individuals_avg': r'\d+[,\d]*.*?average.*?individuals',
        'individuals_median': r'\d+[,\d]*.*?median.*?individuals',
    }

    results = search_for_patterns(pages_text, patterns)

    for result in results:
        print(f"\nPage {result['page']} - Pattern: {result['pattern']}")
        print(f"Match: {result['match']}")
        print(f"Context:\n{result['context']}")
        print("-" * 40)

    # Also search for sections about individuals affected
    print("\n\nSearching for 'individuals affected' sections:")
    for page_num, text in pages_text:
        if 'individuals affected' in text.lower():
            # Extract the relevant paragraph
            paragraphs = text.split('\n\n')
            for para in paragraphs:
                if 'individuals affected' in para.lower() and any(keyword in para.lower() for keyword in ['average', 'median', 'mean']):
                    print(f"\nPage {page_num}:")
                    print(para)
                    print("-" * 40)

def extract_2024_h2_stats(pdf_path):
    """Extract average, median, and cyber incidents from 2024 H2 PDF."""
    print("\n" + "="*80)
    print("2024 H2 PDF - Average, Median, and Cyber Incidents")
    print("="*80)

    pages_text = extract_text_from_pdf(pdf_path)

    # Search for average and median patterns
    patterns = {
        'average': r'\baverage\b.*?\d+[,\d]*',
        'mean': r'\bmean\b.*?\d+[,\d]*',
        'median': r'\bmedian\b.*?\d+[,\d]*',
        'cyber_security': r'\bcyber[\s-]?security.*?\d+[,\d]*',
        'malicious': r'\bmalicious.*?\d+[,\d]*',
        'cyber_incident': r'\bcyber.*?incident.*?\d+[,\d]*',
    }

    results = search_for_patterns(pages_text, patterns)

    for result in results:
        print(f"\nPage {result['page']} - Pattern: {result['pattern']}")
        print(f"Match: {result['match']}")
        print(f"Context:\n{result['context']}")
        print("-" * 40)

    # Search for cyber-related sections
    print("\n\nSearching for cyber/malicious attack sections:")
    for page_num, text in pages_text:
        if any(keyword in text.lower() for keyword in ['cyber security', 'malicious', 'cyber incident', 'cyber-related']):
            lines = text.split('\n')
            for i, line in enumerate(lines):
                if any(keyword in line.lower() for keyword in ['cyber security', 'malicious', 'cyber incident']):
                    # Print surrounding lines
                    start = max(0, i-2)
                    end = min(len(lines), i+3)
                    print(f"\nPage {page_num}:")
                    print('\n'.join(lines[start:end]))
                    print("-" * 40)

def extract_2020_h2_cyber(pdf_path):
    """Extract cyber incidents from 2020 H2 PDF (total: 512)."""
    print("\n" + "="*80)
    print("2020 H2 PDF - Cyber Incidents (Total notifications: 512)")
    print("="*80)

    pages_text = extract_text_from_pdf(pdf_path)

    patterns = {
        'cyber_security': r'\bcyber[\s-]?security.*?\d+[,\d]*',
        'malicious': r'\bmalicious.*?\d+[,\d]*',
        'cyber_incident': r'\bcyber.*?incident.*?\d+[,\d]*',
        'percentage': r'\d+[,\d]*\.?\d*\s*%',
    }

    results = search_for_patterns(pages_text, patterns)

    for result in results:
        print(f"\nPage {result['page']} - Pattern: {result['pattern']}")
        print(f"Match: {result['match']}")
        print(f"Context:\n{result['context']}")
        print("-" * 40)

def extract_2023_h2_malware(pdf_path):
    """Extract correct malware notification count from 2023 H2 PDF."""
    print("\n" + "="*80)
    print("2023 H2 PDF - Malware Notifications (NOT individuals)")
    print("="*80)

    pages_text = extract_text_from_pdf(pdf_path)

    patterns = {
        'malware': r'\bmalware\b.*?\d+[,\d]*',
        'malware_notifications': r'\d+[,\d]*.*?malware',
    }

    results = search_for_patterns(pages_text, patterns)

    for result in results:
        print(f"\nPage {result['page']} - Pattern: {result['pattern']}")
        print(f"Match: {result['match']}")
        print(f"Context:\n{result['context']}")
        print("-" * 40)

    # Look for tables or charts about source of breach
    print("\n\nSearching for 'source of breach' or 'type of breach' sections:")
    for page_num, text in pages_text:
        if any(keyword in text.lower() for keyword in ['source of breach', 'type of breach', 'malware']):
            lines = text.split('\n')
            for i, line in enumerate(lines):
                if 'malware' in line.lower():
                    start = max(0, i-3)
                    end = min(len(lines), i+4)
                    print(f"\nPage {page_num}:")
                    print('\n'.join(lines[start:end]))
                    print("-" * 40)

def extract_2022_h2_ransomware(pdf_path):
    """Extract correct ransomware notification count from 2022 H2 PDF."""
    print("\n" + "="*80)
    print("2022 H2 PDF - Ransomware Notifications (NOT individuals)")
    print("="*80)

    pages_text = extract_text_from_pdf(pdf_path)

    patterns = {
        'ransomware': r'\bransomware\b.*?\d+[,\d]*',
        'ransomware_notifications': r'\d+[,\d]*.*?ransomware',
    }

    results = search_for_patterns(pages_text, patterns)

    for result in results:
        print(f"\nPage {result['page']} - Pattern: {result['pattern']}")
        print(f"Match: {result['match']}")
        print(f"Context:\n{result['context']}")
        print("-" * 40)

    # Look for cyber security incidents section
    print("\n\nSearching for cyber security incidents sections:")
    for page_num, text in pages_text:
        if 'ransomware' in text.lower():
            lines = text.split('\n')
            for i, line in enumerate(lines):
                if 'ransomware' in line.lower():
                    start = max(0, i-3)
                    end = min(len(lines), i+4)
                    print(f"\nPage {page_num}:")
                    print('\n'.join(lines[start:end]))
                    print("-" * 40)

def main():
    base_path = Path("D:/dev/australian-cyber-events-scraper/oaic_pdfs")

    # 1. 2024 H1 - Average and Median
    pdf_2024_h1 = base_path / "Notifiable-data-breaches-report-January-to-June-2024.pdf"
    if pdf_2024_h1.exists():
        extract_2024_h1_stats(pdf_2024_h1)

    # 2. 2024 H2 - Average, Median, and Cyber Incidents
    pdf_2024_h2 = base_path / "Notifiable-data-breaches-report-July-to-December-2024.pdf"
    if pdf_2024_h2.exists():
        extract_2024_h2_stats(pdf_2024_h2)

    # 3. 2020 H2 - Cyber Incidents
    pdf_2020_h2 = base_path / "notifiable-data-breaches-report-july-dec-2020.pdf"
    if pdf_2020_h2.exists():
        extract_2020_h2_cyber(pdf_2020_h2)

    # 4. 2023 H2 - Malware Notifications
    pdf_2023_h2 = base_path / "Notifiable-data-breaches-report-July-to-December-2023.pdf"
    if pdf_2023_h2.exists():
        extract_2023_h2_malware(pdf_2023_h2)

    # 5. 2022 H2 - Ransomware Notifications
    pdf_2022_h2 = base_path / "OAIC-Notifiable-data-breaches-report-July-December-2022.pdf"
    if pdf_2022_h2.exists():
        extract_2022_h2_ransomware(pdf_2022_h2)

if __name__ == "__main__":
    main()
