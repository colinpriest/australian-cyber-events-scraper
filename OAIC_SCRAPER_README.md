# OAIC Data Breach Statistics Scraper

## Overview

This script automatically scrapes official cyber attack and data breach statistics from the Office of the Australian Information Commissioner (OAIC) website. The OAIC publishes semi-annual reports containing detailed statistics on notifiable data breaches in Australia, including cyber security incidents, ransomware attacks, phishing attempts, and sector breakdowns.

## Data Source

**Website**: https://www.oaic.gov.au/privacy/notifiable-data-breaches/notifiable-data-breaches-publications

**Publication Schedule**: Semi-annual reports (January-June and July-December)

**Data Coverage**: 2018 - Present

**Report Types**:
- Semi-annual reports (most common): January-June, July-December
- Quarterly reports (2018-2019): Q1, Q2, Q3, Q4
- Annual insight reports

## Installation

### Prerequisites

```bash
pip install requests beautifulsoup4
```

### Script Usage

```bash
# Basic usage - scrape all available data
python oaic_data_scraper.py

# Scrape specific year range
python oaic_data_scraper.py --start-year 2020 --end-year 2024

# Output formats
python oaic_data_scraper.py --output csv      # CSV only
python oaic_data_scraper.py --output json     # JSON only
python oaic_data_scraper.py --output both     # Both formats (default)

# Custom filename
python oaic_data_scraper.py --filename my_oaic_data

# Skip summary display
python oaic_data_scraper.py --no-summary
```

## Data Fields Extracted

### Core Statistics
- **total_notifications**: Total number of data breach notifications
- **cyber_incidents_total**: Number of cyber security related incidents
- **cyber_incidents_percentage**: Percentage of all breaches that were cyber incidents
- **malicious_attacks**: Total malicious/criminal attacks
- **human_error**: Breaches caused by human error
- **system_faults**: Breaches caused by system faults

### Cyber Attack Types
- **ransomware**: Number of ransomware incidents
- **phishing**: Number of phishing incidents
- **hacking**: Number of hacking incidents
- **brute_force**: Number of brute force attacks
- **malware**: Number of malware incidents
- **compromised_credentials**: Number of compromised credential incidents

### Metadata
- **year**: Report year
- **period**: Reporting period (H1, H2, Q1, Q2, Q3, Q4)
- **quarter**: Quarter designation
- **start_month**: Starting month of period
- **end_month**: Ending month of period
- **title**: Full report title
- **url**: Direct link to OAIC report

### Sector Data
- **top_sectors**: List of most affected industry sectors with notification counts

## Sample Output

### CSV Format
```csv
year,period,quarter,start_month,end_month,total_notifications,cyber_incidents_total,cyber_incidents_percentage,ransomware,phishing,hacking
2024,H1,H1,1,6,527,200,38,49,63,14
2024,H2,H2,7,12,547,210,39,52,68,16
```

### JSON Format
```json
[
  {
    "title": "Notifiable Data Breaches Report: January to June 2024",
    "year": 2024,
    "period": "H1",
    "total_notifications": 527,
    "cyber_incidents_total": 200,
    "cyber_incidents_percentage": 38,
    "ransomware": 49,
    "phishing": 63,
    "hacking": 14,
    "top_sectors": [
      {"sector": "Health", "notifications": 102},
      {"sector": "Finance", "notifications": 58}
    ]
  }
]
```

## Example Usage Scenarios

### 1. Annual Trend Analysis
```bash
# Get 5 years of data for trend analysis
python oaic_data_scraper.py --start-year 2020 --end-year 2024 --output csv
```

### 2. Recent Period Focus
```bash
# Get latest 2 years only
python oaic_data_scraper.py --start-year 2023 --output json
```

### 3. Integration with Other Tools
```bash
# Export for dashboard integration
python oaic_data_scraper.py --filename dashboard_oaic --output json --no-summary
```

## Key Statistics Available

### Historical Coverage
- **2018**: Quarterly reports (when NDB scheme began)
- **2019**: Mixed quarterly and semi-annual reports
- **2020-2024**: Semi-annual reports (January-June, July-December)

### Typical Data Points per Report
- Total notifications: 400-600 per period
- Cyber incidents: 35-45% of all breaches
- Sector breakdown: Health, Government, Finance typically top 3
- Attack methods: Ransomware, phishing, compromised credentials most common

## Data Quality Notes

### Strengths
- ✅ **Official government source** - highest reliability
- ✅ **Consistent reporting** - regular semi-annual publication
- ✅ **Detailed breakdown** - by attack type, sector, impact
- ✅ **Long history** - data back to 2018
- ✅ **Standardized format** - comparable across periods

### Limitations
- ⚠️ **Semi-annual frequency** - not monthly/quarterly granularity for recent years
- ⚠️ **Reporting lag** - 6-month delay between period end and publication
- ⚠️ **Voluntary reporting** - based on notifications received, may underrepresent
- ⚠️ **Definition changes** - methodology may evolve over time

## Integration with Dashboard

The scraped OAIC data can be integrated with the dashboard system:

```python
# Example integration
from oaic_data_scraper import OAICDataScraper

scraper = OAICDataScraper()
oaic_data = scraper.scrape_all_reports(2020, 2024)

# Convert to dashboard format
for record in oaic_data:
    # Insert into dashboard database
    # Compare with other data sources
    # Generate trend analysis
```

## Error Handling

The script includes robust error handling for:
- **Network issues**: Automatic retries with backoff
- **Website changes**: Graceful degradation if structure changes
- **Missing data**: Continues processing other reports
- **Rate limiting**: Respects website access patterns

## Output Files

Files are automatically timestamped to prevent overwrites:
- `oaic_cyber_statistics_YYYYMMDD_HHMMSS.csv`
- `oaic_cyber_statistics_YYYYMMDD_HHMMSS.json`

## Monitoring and Updates

### Regular Updates
Recommended to run monthly to capture new reports:
```bash
# Cron job example (monthly on 15th)
0 0 15 * * /path/to/python /path/to/oaic_data_scraper.py --start-year 2024
```

### Data Validation
Key validation checks:
- Total notifications should be 300-800 per period
- Cyber incidents should be 30-50% of total
- Check for missing periods in recent years

## Contact and Support

For issues with:
- **Script functionality**: Check error messages and network connectivity
- **Data accuracy**: Cross-reference with official OAIC reports
- **Missing reports**: Verify publication schedule on OAIC website

## Legal and Ethical Notes

- ✅ **Public data**: All scraped data is publicly available
- ✅ **Respectful access**: Reasonable delays between requests
- ✅ **Attribution**: Data credited to OAIC
- ⚠️ **Terms of use**: Comply with OAIC website terms
- ⚠️ **Fair use**: Academic/research purposes recommended

---

**Last Updated**: September 2024
**Version**: 1.0
**Compatibility**: Python 3.7+