#!/usr/bin/env python3
"""
Clean up and consolidate OAIC data files.

This script:
1. Uses the cleanest historical data file as the base
2. Merges in individuals_affected_average/median from PDF scraping
3. Adds 2025 H1 data from dashboard scraping
4. Validates all data to ensure no corrupted values
5. Outputs a single clean OAIC data file
"""

import json
import glob
import os
from datetime import datetime
from typing import Dict, Any, List, Optional


def validate_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and clean a single OAIC record."""
    total = record.get('total_notifications') or 0

    # Validate top_sectors
    if record.get('top_sectors') and total > 0:
        valid_sectors = []
        for sector in record['top_sectors']:
            count = sector.get('notifications') or 0
            if count <= total * 1.1:  # Allow 10% margin
                valid_sectors.append(sector)
            else:
                print(f"  Removing invalid sector: {sector.get('sector')}={count} (total={total})")
        record['top_sectors'] = valid_sectors if valid_sectors else []

    # Validate attack type counts
    attack_fields = ['phishing', 'ransomware', 'hacking', 'brute_force', 'malware', 'compromised_credentials']
    for field in attack_fields:
        value = record.get(field)
        if value is not None and total > 0 and value > total * 1.1:
            print(f"  Nullifying invalid {field}={value} (total={total})")
            record[field] = None

    return record


def merge_oaic_data() -> List[Dict[str, Any]]:
    """Merge OAIC data from multiple sources to produce clean output."""

    # Source 1: Clean historical data (2019-2024)
    base_file = 'oaic_cyber_statistics_20251030_194752.json'
    print(f"Loading base historical data from: {base_file}")
    with open(base_file, 'r', encoding='utf-8') as f:
        base_data = json.load(f)

    # Source 2: PDF-scraped data with individuals_affected stats (2024)
    pdf_file = 'oaic_cyber_statistics_20260116_085731.json'
    print(f"Loading PDF data (for individuals_affected stats) from: {pdf_file}")
    with open(pdf_file, 'r', encoding='utf-8') as f:
        pdf_data = json.load(f)

    # Source 3: Dashboard-scraped data (2025 H1)
    dashboard_file = 'oaic_cyber_statistics_20260116_112846.json'
    print(f"Loading dashboard data (for 2025 H1) from: {dashboard_file}")
    with open(dashboard_file, 'r', encoding='utf-8') as f:
        dashboard_data = json.load(f)

    # Index data by period
    records_by_period: Dict[str, Dict[str, Any]] = {}

    # Process base data (filter out Q1/Q2 periods, keep only H1/H2)
    print("\nProcessing base historical data...")
    for record in base_data:
        year = record.get('year')
        period = record.get('period')

        # Skip quarterly data (Q1, Q2) - we only want half-yearly (H1, H2)
        if period not in ['H1', 'H2']:
            print(f"  Skipping {year} {period} (not half-yearly)")
            continue

        period_key = f"{year} {period}"
        record = validate_record(record)
        records_by_period[period_key] = record
        print(f"  Added {period_key}")

    # Merge individuals_affected stats from PDF data
    print("\nMerging individuals_affected stats from PDF data...")
    for record in pdf_data:
        year = record.get('year')
        period = record.get('period')
        period_key = f"{year} {period}"

        if period_key in records_by_period:
            # Add individuals_affected stats if present
            if record.get('individuals_affected_average'):
                records_by_period[period_key]['individuals_affected_average'] = record['individuals_affected_average']
                print(f"  Added avg to {period_key}: {record['individuals_affected_average']}")
            if record.get('individuals_affected_median'):
                records_by_period[period_key]['individuals_affected_median'] = record['individuals_affected_median']
                print(f"  Added median to {period_key}: {record['individuals_affected_median']}")

    # Add 2025 H1 from dashboard data
    print("\nAdding 2025 H1 from dashboard data...")
    for record in dashboard_data:
        year = record.get('year')
        period = record.get('period')

        if year == 2025 and period == 'H1':
            period_key = f"{year} {period}"
            record = validate_record(record)
            records_by_period[period_key] = record
            print(f"  Added {period_key} from dashboard")

    # Sort by year and period
    sorted_periods = sorted(records_by_period.keys(),
                           key=lambda x: (int(x.split()[0]), 0 if x.split()[1] == 'H1' else 1))

    result = [records_by_period[p] for p in sorted_periods]

    print(f"\n=== Final merged data: {len(result)} periods ===")
    for record in result:
        year = record.get('year')
        period = record.get('period')
        total = record.get('total_notifications')
        sectors = len(record.get('top_sectors', []))
        avg = record.get('individuals_affected_average')
        print(f"  {year} {period}: total={total}, sectors={sectors}, avg={avg}")

    return result


def main():
    """Main function to clean and consolidate OAIC data."""
    print("=" * 60)
    print("OAIC Data Cleanup Script")
    print("=" * 60)
    print()

    # Merge data
    clean_data = merge_oaic_data()

    # Save output
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = f'oaic_cyber_statistics_cleaned_{timestamp}.json'

    print(f"\nSaving cleaned data to: {output_file}")
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(clean_data, f, indent=2)

    print(f"\nDone! {len(clean_data)} periods saved.")
    print("\nTo use this data, run:")
    print(f"  python build_static_dashboard.py")
    print("\nThe dashboard builder will automatically use the most recent OAIC file.")


if __name__ == '__main__':
    main()
