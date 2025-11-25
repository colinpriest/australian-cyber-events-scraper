#!/usr/bin/env python3
"""
Extract specific numerical values from OAIC PDFs.
"""
import pdfplumber
import re
from pathlib import Path

def extract_2024_h1_table4(pdf_path):
    """Extract Table 4 values from 2024 H1 PDF."""
    print("\n" + "="*80)
    print("2024 H1 - Table 4: Cyber incident breakdown")
    print("="*80)

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            text = page.extract_text()

            if "Table 4:" in text and "Cyber incident breakdown" in text:
                print(f"\nFound on Page {page_num}")

                # Extract table data
                lines = text.split('\n')
                table_started = False
                for i, line in enumerate(lines):
                    if "Table 4:" in line:
                        table_started = True
                        # Print next 15 lines
                        for j in range(20):
                            if i+j < len(lines):
                                print(lines[i+j])
                        break

def extract_2024_h2_table2_and_3(pdf_path):
    """Extract Table 2 and 3 from 2024 H2 PDF."""
    print("\n" + "="*80)
    print("2024 H2 - Table 2 (Malicious attack) and Table 3 (Cyber incident)")
    print("="*80)

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            text = page.extract_text()

            if "Table 2" in text and "Malicious or criminal attack breakdown" in text:
                print(f"\nTable 2 found on Page {page_num}")
                lines = text.split('\n')
                for i, line in enumerate(lines):
                    if "Table 2" in line:
                        # Print next 20 lines
                        for j in range(25):
                            if i+j < len(lines):
                                print(lines[i+j])
                        break

            if "Table 3" in text and "Cyber incident breakdown" in text:
                print(f"\n\nTable 3 found on Page {page_num}")
                lines = text.split('\n')
                for i, line in enumerate(lines):
                    if "Table 3" in line:
                        # Print next 20 lines
                        for j in range(25):
                            if i+j < len(lines):
                                print(lines[i+j])
                        break

def extract_2020_h2_cyber_count(pdf_path):
    """Extract cyber incident count from 2020 H2 PDF."""
    print("\n" + "="*80)
    print("2020 H2 - Cyber Incidents Count")
    print("="*80)

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            text = page.extract_text()

            # Look for the key statistics on cyber incidents
            if "cyber security incidents" in text.lower() and ("212" in text or "218" in text):
                print(f"\nFound on Page {page_num}")
                lines = text.split('\n')
                for i, line in enumerate(lines):
                    if "cyber security incidents" in line.lower() or "cyber incident" in line.lower():
                        # Print context
                        start = max(0, i-2)
                        end = min(len(lines), i+3)
                        for j in range(start, end):
                            print(lines[j])
                        print("-" * 40)

def extract_2023_h2_malware_count(pdf_path):
    """Extract malware notification count from 2023 H2 PDF."""
    print("\n" + "="*80)
    print("2023 H2 - Malware Notification Count")
    print("="*80)

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            text = page.extract_text()

            # Look for cyber incident table with malware breakdown
            if "malware" in text.lower() and ("Table" in text or "Chart" in text):
                lines = text.split('\n')
                for i, line in enumerate(lines):
                    if "malware" in line.lower():
                        # Print context
                        start = max(0, i-2)
                        end = min(len(lines), i+3)
                        context = '\n'.join(lines[start:end])

                        # Only print if it has numbers
                        if re.search(r'\d+', context):
                            print(f"\nPage {page_num}:")
                            print(context)
                            print("-" * 40)

def extract_2022_h2_ransomware_count(pdf_path):
    """Extract ransomware notification count from 2022 H2 PDF."""
    print("\n" + "="*80)
    print("2022 H2 - Ransomware Notification Count")
    print("="*80)

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            text = page.extract_text()

            # Look for cyber incident table with ransomware breakdown
            if "ransomware" in text.lower() and ("Table" in text or "Chart" in text):
                lines = text.split('\n')
                for i, line in enumerate(lines):
                    if "ransomware" in line.lower():
                        # Print context
                        start = max(0, i-2)
                        end = min(len(lines), i+3)
                        context = '\n'.join(lines[start:end])

                        # Only print if it has numbers
                        if re.search(r'\d+', context):
                            print(f"\nPage {page_num}:")
                            print(context)
                            print("-" * 40)

def main():
    base_path = Path("D:/dev/australian-cyber-events-scraper/oaic_pdfs")

    # 1. 2024 H1
    print("\n\n" + "#"*80)
    print("# TASK 1: 2024 H1 - Average and Median")
    print("#"*80)
    pdf_2024_h1 = base_path / "Notifiable-data-breaches-report-January-to-June-2024.pdf"
    if pdf_2024_h1.exists():
        extract_2024_h1_table4(pdf_2024_h1)

    # 2. 2024 H2
    print("\n\n" + "#"*80)
    print("# TASK 2 & 4: 2024 H2 - Average, Median, and Cyber Incidents")
    print("#"*80)
    pdf_2024_h2 = base_path / "Notifiable-data-breaches-report-July-to-December-2024.pdf"
    if pdf_2024_h2.exists():
        extract_2024_h2_table2_and_3(pdf_2024_h2)

    # 3. 2020 H2
    print("\n\n" + "#"*80)
    print("# TASK 3: 2020 H2 - Cyber Incidents Count")
    print("#"*80)
    pdf_2020_h2 = base_path / "notifiable-data-breaches-report-july-dec-2020.pdf"
    if pdf_2020_h2.exists():
        extract_2020_h2_cyber_count(pdf_2020_h2)

    # 4. 2023 H2
    print("\n\n" + "#"*80)
    print("# TASK 5: 2023 H2 - Malware Notification Count")
    print("#"*80)
    pdf_2023_h2 = base_path / "Notifiable-data-breaches-report-July-to-December-2023.pdf"
    if pdf_2023_h2.exists():
        extract_2023_h2_malware_count(pdf_2023_h2)

    # 5. 2022 H2
    print("\n\n" + "#"*80)
    print("# TASK 6: 2022 H2 - Ransomware Notification Count")
    print("#"*80)
    pdf_2022_h2 = base_path / "OAIC-Notifiable-data-breaches-report-July-December-2022.pdf"
    if pdf_2022_h2.exists():
        extract_2022_h2_ransomware_count(pdf_2022_h2)

if __name__ == "__main__":
    main()
