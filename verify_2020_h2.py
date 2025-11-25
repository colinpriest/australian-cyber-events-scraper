#!/usr/bin/env python3
"""
Verify 2020 H2 total notifications.
"""
import pdfplumber
from pathlib import Path

pdf_path = Path("D:/dev/australian-cyber-events-scraper/oaic_pdfs/notifiable-data-breaches-report-july-dec-2020.pdf")

with pdfplumber.open(pdf_path) as pdf:
    for page_num, page in enumerate(pdf.pages, 1):
        text = page.extract_text()

        # Look for total notifications mention
        if "539" in text or "512" in text:
            lines = text.split('\n')
            for i, line in enumerate(lines):
                if "539" in line or "512" in line:
                    start = max(0, i-2)
                    end = min(len(lines), i+3)
                    print(f"Page {page_num}:")
                    print('\n'.join(lines[start:end]))
                    print("-" * 60)
