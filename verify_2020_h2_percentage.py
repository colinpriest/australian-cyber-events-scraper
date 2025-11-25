#!/usr/bin/env python3
"""
Check if there's a percentage breakdown for malicious attacks in 2020 H2.
"""
import pdfplumber
from pathlib import Path

pdf_path = Path("D:/dev/australian-cyber-events-scraper/oaic_pdfs/notifiable-data-breaches-report-july-dec-2020.pdf")

print("Looking for malicious/criminal attack percentage and breakdown...")
print("="*80)

with pdfplumber.open(pdf_path) as pdf:
    for page_num, page in enumerate(pdf.pages, 1):
        text = page.extract_text()

        # Look for malicious attack breakdown
        if "malicious" in text.lower() and "58%" in text:
            lines = text.split('\n')
            for i, line in enumerate(lines):
                if "58%" in line or ("malicious" in line.lower() and "attack" in line.lower()):
                    start = max(0, i-3)
                    end = min(len(lines), i+4)
                    print(f"\nPage {page_num}:")
                    print('\n'.join(lines[start:end]))
                    print("-" * 60)

print("\n\nLooking for specific breakdown of malicious attacks...")
print("="*80)

with pdfplumber.open(pdf_path) as pdf:
    for page_num, page in enumerate(pdf.pages, 1):
        text = page.extract_text()

        # Look for table or chart about malicious attacks
        if ("malicious or criminal attack" in text.lower() and
            ("chart" in text.lower() or "table" in text.lower())):
            print(f"\nFound on Page {page_num}")
            # Print more context
            lines = text.split('\n')
            for i, line in enumerate(lines):
                if "malicious or criminal attack" in line.lower():
                    start = max(0, i-1)
                    end = min(len(lines), i+15)
                    print('\n'.join(lines[start:end]))
                    print("-" * 60)
                    break
