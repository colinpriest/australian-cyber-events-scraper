# OAIC Enhancement Implementation Plan

## Executive Summary

This plan details the implementation of three major enhancements to extract and visualize detailed data from OAIC PDF reports:

1. **Individuals Affected Distribution**: Extract histogram data from PDFs and create comparative visualizations with database records
2. **Median/Average Statistics**: Extract statistical measures and create trend comparison plots
3. **Complete Sector Rankings**: Extract full Top 5 sector data and compare with database industry distribution

**Current Limitation**: The existing `oaic_data_scraper.py` only parses HTML summary pages. The detailed data we need (distributions, statistical measures, complete sector rankings) exists in **PDF reports with tables and charts**.

**Solution**: Implement PDF parsing to extract structured table data from OAIC reports.

---

## Phase 1: PDF Parsing Infrastructure

### 1.1 Dependencies
**Add to `requirements.txt`:**
```
pdfplumber>=0.10.0
tabula-py>=2.8.0
```

**Rationale**:
- `pdfplumber` excels at extracting text from specific regions and can identify table structures
- `tabula-py` is excellent for extracting tabular data directly to pandas DataFrames
- We'll use both: `pdfplumber` for initial exploration/text extraction, `tabula-py` for table extraction

### 1.2 PDF Download Functionality
**Modify `oaic_data_scraper.py`:**

```python
def download_pdf_report(report_url: str, output_dir: str = "oaic_pdfs") -> Optional[str]:
    """Download PDF report and return local file path."""
    import os
    import requests
    from urllib.parse import urlparse, unquote

    os.makedirs(output_dir, exist_ok=True)

    # Extract filename from URL
    parsed = urlparse(report_url)
    filename = os.path.basename(unquote(parsed.path))

    if not filename.endswith('.pdf'):
        filename += '.pdf'

    filepath = os.path.join(output_dir, filename)

    # Skip if already downloaded
    if os.path.exists(filepath):
        logger.info(f"PDF already exists: {filepath}")
        return filepath

    # Download PDF
    try:
        response = requests.get(report_url, timeout=30)
        response.raise_for_status()

        with open(filepath, 'wb') as f:
            f.write(response.content)

        logger.info(f"Downloaded PDF: {filepath}")
        return filepath
    except Exception as e:
        logger.error(f"Failed to download PDF: {e}")
        return None
```

### 1.3 PDF Table Extraction
**Add to `oaic_data_scraper.py`:**

```python
import tabula
import pdfplumber

def extract_individuals_affected_distribution(pdf_path: str) -> Optional[List[Dict[str, Any]]]:
    """
    Extract individuals affected distribution from PDF.

    Expected table format:
    | Number of Individuals | Count |
    |----------------------|-------|
    | 1-100                | XX    |
    | 101-1,000            | XX    |
    | 1,001-10,000         | XX    |
    | 10,001-100,000       | XX    |
    | 100,001+             | XX    |
    """
    try:
        # Try tabula first for structured table extraction
        tables = tabula.read_pdf(pdf_path, pages='all', multiple_tables=True)

        for df in tables:
            # Look for tables with columns like "Number of Individuals" or "Individuals affected"
            if any('individual' in str(col).lower() for col in df.columns):
                # Parse the table into bins
                bins = []
                for _, row in df.iterrows():
                    # Extract range and count
                    # Handle various formats: "1-100", "101-1,000", "100,001+"
                    range_str = str(row[0]).strip()
                    count_str = str(row[1]).strip()

                    # Parse count (remove commas)
                    count = int(count_str.replace(',', ''))

                    bins.append({
                        'range': range_str,
                        'count': count
                    })

                return bins if bins else None

        return None
    except Exception as e:
        logger.error(f"Failed to extract individuals affected distribution: {e}")
        return None


def extract_median_average_statistics(pdf_path: str) -> Optional[Dict[str, Any]]:
    """
    Extract median and average affected individuals statistics.

    Expected data:
    - Median number of individuals affected
    - Average number of individuals affected
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = ""
            for page in pdf.pages:
                text += page.extract_text() + "\n"

            # Regex patterns for median and average
            import re

            median_pattern = r'median.*?(\d+(?:,\d+)*)\s*(?:individual|people|record)'
            average_pattern = r'average.*?(\d+(?:,\d+)*)\s*(?:individual|people|record)'
            mean_pattern = r'mean.*?(\d+(?:,\d+)*)\s*(?:individual|people|record)'

            median = None
            average = None

            # Search for median
            match = re.search(median_pattern, text, re.IGNORECASE)
            if match:
                median = int(match.group(1).replace(',', ''))

            # Search for average or mean
            match = re.search(average_pattern, text, re.IGNORECASE)
            if match:
                average = int(match.group(1).replace(',', ''))
            else:
                match = re.search(mean_pattern, text, re.IGNORECASE)
                if match:
                    average = int(match.group(1).replace(',', ''))

            if median is not None or average is not None:
                return {
                    'median': median,
                    'average': average
                }

            return None
    except Exception as e:
        logger.error(f"Failed to extract median/average statistics: {e}")
        return None


def extract_complete_sector_rankings(pdf_path: str) -> Optional<List[Dict[str, Any]]]:
    """
    Extract complete Top 5 (or more) sector rankings from PDF.

    Expected table format:
    | Sector                    | Notifications |
    |--------------------------|---------------|
    | Health service providers  | XX            |
    | Finance                  | XX            |
    | Education                | XX            |
    | Retail                   | XX            |
    | Legal, accounting        | XX            |
    """
    try:
        tables = tabula.read_pdf(pdf_path, pages='all', multiple_tables=True)

        for df in tables:
            # Look for tables with sector/industry columns
            if any(keyword in str(col).lower() for keyword in ['sector', 'industry'] for col in df.columns):
                sectors = []
                for _, row in df.iterrows():
                    sector = str(row[0]).strip()

                    # Parse notification count from second column
                    try:
                        notifications = int(str(row[1]).strip().replace(',', ''))
                        sectors.append({
                            'sector': sector,
                            'notifications': notifications
                        })
                    except:
                        continue

                # Return if we got at least 3 sectors (likely valid data)
                if len(sectors) >= 3:
                    return sectors

        return None
    except Exception as e:
        logger.error(f"Failed to extract sector rankings: {e}")
        return None
```

---

## Phase 2: Data Schema Updates

### 2.1 OAIC Data Structure Enhancement
**Modify the data structure in `oaic_data_scraper.py`:**

Current structure has:
```python
{
    "title": str,
    "year": int,
    "period": str,
    "total_notifications": int,
    "top_sectors": List[Dict],  # Currently only 1-4 sectors
    ...
}
```

**Enhanced structure:**
```python
{
    "title": str,
    "year": int,
    "period": str,
    "total_notifications": int,
    "top_sectors": List[Dict],  # Full Top 5+ sectors

    # NEW: Individuals affected distribution
    "individuals_affected_distribution": [
        {"range": "1-100", "count": 45},
        {"range": "101-1,000", "count": 78},
        {"range": "1,001-10,000", "count": 52},
        {"range": "10,001-100,000", "count": 18},
        {"range": "100,001+", "count": 7}
    ],

    # NEW: Statistical measures
    "individuals_affected_median": int,
    "individuals_affected_average": int,

    # Metadata
    "pdf_url": str,
    "pdf_parsed": bool,
    "pdf_parsing_errors": List[str]
}
```

### 2.2 Database Comparison Queries
**Add to `build_static_dashboard.py`:**

```python
def get_records_affected_distribution(conn: sqlite3.Connection, start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """Get distribution of records affected from database events."""
    # Define bins matching OAIC structure
    bins = [
        {"range": "1-100", "min": 1, "max": 100},
        {"range": "101-1,000", "min": 101, "max": 1000},
        {"range": "1,001-10,000", "min": 1001, "max": 10000},
        {"range": "10,001-100,000", "min": 10001, "max": 100000},
        {"range": "100,001-1,000,000", "min": 100001, "max": 1000000},
        {"range": "1,000,001+", "min": 1000001, "max": None}
    ]

    distribution = []
    for bin_def in bins:
        if bin_def["max"] is None:
            query = f"""
                SELECT COUNT(*) as count
                FROM DeduplicatedEvents
                WHERE status = 'Active'
                    AND records_affected IS NOT NULL
                    AND CAST(records_affected AS INTEGER) >= {bin_def["min"]}
                    AND CAST(records_affected AS INTEGER) <= 1000000000
                    AND event_date >= ?
                    AND event_date <= ?
            """
        else:
            query = f"""
                SELECT COUNT(*) as count
                FROM DeduplicatedEvents
                WHERE status = 'Active'
                    AND records_affected IS NOT NULL
                    AND CAST(records_affected AS INTEGER) >= {bin_def["min"]}
                    AND CAST(records_affected AS INTEGER) <= {bin_def["max"]}
                    AND event_date >= ?
                    AND event_date <= ?
            """

        result = conn.execute(query, (start_date, end_date)).fetchone()
        distribution.append({
            "range": bin_def["range"],
            "count": result[0]
        })

    return distribution


def get_median_average_records_affected(conn: sqlite3.Connection, period_start: str, period_end: str) -> Dict[str, Any]:
    """Calculate median and average records affected for a period."""
    query = """
        SELECT records_affected
        FROM DeduplicatedEvents
        WHERE status = 'Active'
            AND records_affected IS NOT NULL
            AND CAST(records_affected AS INTEGER) > 0
            AND CAST(records_affected AS INTEGER) <= 1000000000
            AND event_date >= ?
            AND event_date <= ?
        ORDER BY CAST(records_affected AS INTEGER)
    """

    results = conn.execute(query, (period_start, period_end)).fetchall()
    values = [int(row[0]) for row in results]

    if not values:
        return {"median": None, "average": None, "count": 0}

    median = values[len(values) // 2]
    average = sum(values) // len(values)

    return {
        "median": median,
        "average": average,
        "count": len(values)
    }


def get_industry_distribution(conn: sqlite3.Connection, start_date: str, end_date: str, top_n: int = 5) -> List[Dict[str, Any]]:
    """Get top N industries by event count."""
    query = """
        SELECT
            COALESCE(victim_industry, 'Unknown') as industry,
            COUNT(*) as event_count
        FROM DeduplicatedEvents
        WHERE status = 'Active'
            AND event_date >= ?
            AND event_date <= ?
        GROUP BY industry
        ORDER BY event_count DESC
        LIMIT ?
    """

    results = conn.execute(query, (start_date, end_date, top_n)).fetchall()
    return [{"industry": row[0], "count": row[1]} for row in results]
```

---

## Phase 3: Dashboard Visualization Updates

### 3.1 Comparative Histogram: Individuals Affected
**Add to `build_static_dashboard.py` HTML template:**

```html
<!-- Individuals Affected Distribution Comparison -->
<div class="col-lg-6 col-md-12">
  <div class="chart-container" style="height: 450px;">
    <div class="chart-title">Individuals Affected Distribution: OAIC vs Database</div>
    <div class="chart-subtitle">Comparing notification ranges from OAIC reports to database records affected</div>
    <canvas id="individualsAffectedHistChart"></canvas>
    <div id="individualsAffectedStats" class="mt-2" style="font-size: 0.9rem; color: #374151;"></div>
  </div>
</div>

<script>
const individualsAffectedCtx = document.getElementById('individualsAffectedHistChart').getContext('2d');

// Prepare data
const oaicDistribution = {{ oaic_individuals_distribution | tojson }};
const dbDistribution = {{ db_individuals_distribution | tojson }};

// Extract labels and values
const ranges = oaicDistribution.map(d => d.range);
const oaicCounts = oaicDistribution.map(d => d.count);
const dbCounts = dbDistribution.map(d => d.count);

new Chart(individualsAffectedCtx, {
  type: 'bar',
  data: {
    labels: ranges,
    datasets: [
      {
        label: 'OAIC Reports (Notifications)',
        data: oaicCounts,
        backgroundColor: 'rgba(59, 130, 246, 0.7)',
        borderColor: 'rgb(59, 130, 246)',
        borderWidth: 1
      },
      {
        label: 'Database (Events)',
        data: dbCounts,
        backgroundColor: 'rgba(16, 185, 129, 0.7)',
        borderColor: 'rgb(16, 185, 129)',
        borderWidth: 1
      }
    ]
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    scales: {
      y: {
        beginAtZero: true,
        title: {
          display: true,
          text: 'Count'
        }
      },
      x: {
        title: {
          display: true,
          text: 'Number of Individuals/Records Affected'
        }
      }
    },
    plugins: {
      legend: {
        position: 'top'
      },
      tooltip: {
        callbacks: {
          label: function(context) {
            return context.dataset.label + ': ' + context.parsed.y.toLocaleString();
          }
        }
      }
    }
  }
});

// Statistics
const oaicTotal = oaicCounts.reduce((a, b) => a + b, 0);
const dbTotal = dbCounts.reduce((a, b) => a + b, 0);
document.getElementById('individualsAffectedStats').innerHTML = `
  <strong>OAIC:</strong> ${oaicTotal.toLocaleString()} notifications |
  <strong>Database:</strong> ${dbTotal.toLocaleString()} events
`;
</script>
```

### 3.2 Median/Average Trends Over Time
**Add to `build_static_dashboard.py` HTML template:**

```html
<!-- Median/Average Comparison Over Time -->
<div class="col-lg-12 col-md-12">
  <div class="chart-container" style="height: 400px;">
    <div class="chart-title">Median & Average Records Affected: OAIC vs Database</div>
    <div class="chart-subtitle">Trends over half-yearly periods</div>
    <canvas id="medianAverageTrendsChart"></canvas>
  </div>
</div>

<script>
const medianAvgCtx = document.getElementById('medianAverageTrendsChart').getContext('2d');

// Prepare data
const periods = {{ periods | tojson }};
const oaicMedian = {{ oaic_median_values | tojson }};
const oaicAverage = {{ oaic_average_values | tojson }};
const dbMedian = {{ db_median_values | tojson }};
const dbAverage = {{ db_average_values | tojson }};

new Chart(medianAvgCtx, {
  type: 'line',
  data: {
    labels: periods,
    datasets: [
      {
        label: 'OAIC Median',
        data: oaicMedian,
        borderColor: 'rgb(59, 130, 246)',
        backgroundColor: 'rgba(59, 130, 246, 0.1)',
        borderWidth: 2,
        pointRadius: 4,
        fill: false
      },
      {
        label: 'OAIC Average',
        data: oaicAverage,
        borderColor: 'rgb(99, 102, 241)',
        backgroundColor: 'rgba(99, 102, 241, 0.1)',
        borderWidth: 2,
        borderDash: [5, 5],
        pointRadius: 4,
        fill: false
      },
      {
        label: 'Database Median',
        data: dbMedian,
        borderColor: 'rgb(16, 185, 129)',
        backgroundColor: 'rgba(16, 185, 129, 0.1)',
        borderWidth: 2,
        pointRadius: 4,
        fill: false
      },
      {
        label: 'Database Average',
        data: dbAverage,
        borderColor: 'rgb(5, 150, 105)',
        backgroundColor: 'rgba(5, 150, 105, 0.1)',
        borderWidth: 2,
        borderDash: [5, 5],
        pointRadius: 4,
        fill: false
      }
    ]
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    scales: {
      y: {
        beginAtZero: true,
        title: {
          display: true,
          text: 'Number of Records/Individuals'
        },
        ticks: {
          callback: function(value) {
            return value.toLocaleString();
          }
        }
      }
    },
    plugins: {
      legend: {
        position: 'top'
      },
      tooltip: {
        callbacks: {
          label: function(context) {
            return context.dataset.label + ': ' + context.parsed.y.toLocaleString();
          }
        }
      }
    }
  }
});
</script>
```

### 3.3 Sector/Industry Distribution Comparison
**Add to `build_static_dashboard.py` HTML template:**

```html
<!-- Sector Distribution Comparison -->
<div class="col-lg-6 col-md-12">
  <div class="chart-container" style="height: 450px;">
    <div class="chart-title">Top 5 Sectors/Industries: OAIC vs Database</div>
    <div class="chart-subtitle">Latest available period comparison</div>
    <canvas id="sectorComparisonChart"></canvas>
  </div>
</div>

<script>
const sectorCtx = document.getElementById('sectorComparisonChart').getContext('2d');

// Prepare data
const oaicSectors = {{ oaic_top_sectors | tojson }};
const dbIndustries = {{ db_top_industries | tojson }};

// Merge and align sectors (create unified list)
const allSectors = new Set([
  ...oaicSectors.map(s => s.sector),
  ...dbIndustries.map(s => s.industry)
]);

const sectorLabels = Array.from(allSectors);
const oaicValues = sectorLabels.map(label => {
  const match = oaicSectors.find(s => s.sector === label);
  return match ? match.notifications : 0;
});
const dbValues = sectorLabels.map(label => {
  const match = dbIndustries.find(s => s.industry === label);
  return match ? match.count : 0;
});

new Chart(sectorCtx, {
  type: 'bar',
  data: {
    labels: sectorLabels,
    datasets: [
      {
        label: 'OAIC Reports',
        data: oaicValues,
        backgroundColor: 'rgba(59, 130, 246, 0.7)',
        borderColor: 'rgb(59, 130, 246)',
        borderWidth: 1
      },
      {
        label: 'Database Events',
        data: dbValues,
        backgroundColor: 'rgba(16, 185, 129, 0.7)',
        borderColor: 'rgb(16, 185, 129)',
        borderWidth: 1
      }
    ]
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    indexAxis: 'y',  // Horizontal bar chart
    scales: {
      x: {
        beginAtZero: true,
        title: {
          display: true,
          text: 'Count'
        }
      }
    },
    plugins: {
      legend: {
        position: 'top'
      },
      tooltip: {
        callbacks: {
          label: function(context) {
            return context.dataset.label + ': ' + context.parsed.x.toLocaleString();
          }
        }
      }
    }
  }
});
</script>
```

---

## Phase 4: Integration and Data Flow

### 4.1 Modified Scraper Main Function
**Update `oaic_data_scraper.py`:**

```python
def scrape_oaic_reports_with_pdfs():
    """Main function to scrape OAIC reports including PDF parsing."""

    # Get all report links
    report_links = get_report_links()
    logger.info(f"Found {len(report_links)} OAIC reports")

    results = []

    for report_data in report_links:
        logger.info(f"Processing: {report_data['title']}")

        # Scrape HTML statistics (existing functionality)
        html_stats = scrape_report_statistics(report_data['url'])

        # Merge HTML stats with report data
        report_entry = {**report_data, **html_stats}

        # Try to find and download PDF
        pdf_url = find_pdf_link(report_data['url'])

        if pdf_url:
            report_entry['pdf_url'] = pdf_url
            pdf_path = download_pdf_report(pdf_url)

            if pdf_path:
                # Extract enhanced data from PDF
                individuals_dist = extract_individuals_affected_distribution(pdf_path)
                median_avg = extract_median_average_statistics(pdf_path)
                complete_sectors = extract_complete_sector_rankings(pdf_path)

                # Add to report entry
                if individuals_dist:
                    report_entry['individuals_affected_distribution'] = individuals_dist

                if median_avg:
                    report_entry['individuals_affected_median'] = median_avg['median']
                    report_entry['individuals_affected_average'] = median_avg['average']

                if complete_sectors:
                    # Replace the partial top_sectors from HTML with complete PDF data
                    report_entry['top_sectors'] = complete_sectors

                report_entry['pdf_parsed'] = True
            else:
                report_entry['pdf_parsed'] = False
        else:
            report_entry['pdf_parsed'] = False

        results.append(report_entry)

    # Save to JSON
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = f'oaic_cyber_statistics_{timestamp}.json'

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    logger.info(f"Saved {len(results)} reports to {output_file}")
    return output_file


def find_pdf_link(report_url: str) -> Optional[str]:
    """Find PDF download link on the report page."""
    try:
        response = requests.get(report_url, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')

        # Look for PDF links
        for link in soup.find_all('a', href=True):
            href = link['href']
            if href.endswith('.pdf') or 'download' in href.lower():
                # Make absolute URL
                if href.startswith('http'):
                    return href
                else:
                    from urllib.parse import urljoin
                    return urljoin(report_url, href)

        return None
    except Exception as e:
        logger.error(f"Failed to find PDF link: {e}")
        return None
```

### 4.2 Dashboard Generation Updates
**Update `build_static_dashboard.py` main function:**

```python
def main():
    # ... existing code ...

    # Load OAIC data
    oaic_file = find_latest_oaic_data()
    oaic_data = load_oaic_data(oaic_file)

    # NEW: Extract individuals affected distribution for latest period
    latest_oaic = [r for r in oaic_data if r.get('individuals_affected_distribution')][-1] if oaic_data else None
    oaic_individuals_distribution = latest_oaic['individuals_affected_distribution'] if latest_oaic else []

    # Get database distribution for same period
    if latest_oaic:
        period_start, period_end = get_period_dates(latest_oaic['year'], latest_oaic['period'])
        db_individuals_distribution = get_records_affected_distribution(conn, period_start, period_end)
    else:
        db_individuals_distribution = []

    # NEW: Extract median/average trends
    periods = []
    oaic_median_values = []
    oaic_average_values = []
    db_median_values = []
    db_average_values = []

    for report in oaic_data:
        if report.get('individuals_affected_median') is not None:
            period_label = f"{report['year']} {report['period']}"
            periods.append(period_label)
            oaic_median_values.append(report.get('individuals_affected_median'))
            oaic_average_values.append(report.get('individuals_affected_average'))

            # Get database stats for same period
            period_start, period_end = get_period_dates(report['year'], report['period'])
            db_stats = get_median_average_records_affected(conn, period_start, period_end)
            db_median_values.append(db_stats['median'])
            db_average_values.append(db_stats['average'])

    # NEW: Extract sector comparison for latest period
    if latest_oaic:
        oaic_top_sectors = latest_oaic.get('top_sectors', [])[:5]
        db_top_industries = get_industry_distribution(conn, period_start, period_end, top_n=5)
    else:
        oaic_top_sectors = []
        db_top_industries = []

    # Pass all new data to template
    html_content = template.render(
        # ... existing variables ...
        oaic_individuals_distribution=oaic_individuals_distribution,
        db_individuals_distribution=db_individuals_distribution,
        periods=periods,
        oaic_median_values=oaic_median_values,
        oaic_average_values=oaic_average_values,
        db_median_values=db_median_values,
        db_average_values=db_average_values,
        oaic_top_sectors=oaic_top_sectors,
        db_top_industries=db_top_industries
    )

    # ... rest of existing code ...
```

---

## Phase 5: Implementation Steps

### Step 1: Setup and Dependencies (Day 1)
1. Add `pdfplumber` and `tabula-py` to `requirements.txt`
2. Install dependencies: `pip install -r requirements.txt`
3. Install Java (required for tabula-py): https://www.java.com/en/download/
4. Test PDF parsing on a single report manually

### Step 2: PDF Extraction Functions (Days 2-3)
1. Implement `download_pdf_report()` function
2. Implement `find_pdf_link()` function
3. Implement `extract_individuals_affected_distribution()` function
4. Implement `extract_median_average_statistics()` function
5. Implement `extract_complete_sector_rankings()` function
6. Test each function individually on 2-3 sample reports

### Step 3: Scraper Integration (Day 4)
1. Modify `scrape_oaic_reports_with_pdfs()` main function
2. Test full scraper on all reports
3. Validate JSON output structure
4. Handle edge cases (missing PDFs, parsing failures)

### Step 4: Database Query Functions (Day 5)
1. Implement `get_records_affected_distribution()` in dashboard script
2. Implement `get_median_average_records_affected()` in dashboard script
3. Implement `get_industry_distribution()` in dashboard script
4. Test queries against current database

### Step 5: Dashboard Visualizations (Days 6-7)
1. Add individuals affected histogram HTML/JS
2. Add median/average trends line chart HTML/JS
3. Add sector comparison horizontal bar chart HTML/JS
4. Test with mock data first
5. Integrate with real data

### Step 6: Integration Testing (Day 8)
1. Run full pipeline: scraper → dashboard generation
2. Verify all charts render correctly
3. Validate data accuracy by spot-checking against OAIC PDFs
4. Test with different date ranges

### Step 7: Documentation and Cleanup (Day 9)
1. Update README with new features
2. Document PDF parsing approach
3. Add error handling and logging improvements
4. Clean up temporary files and backups

---

## Phase 6: Testing Approach

### 6.1 Unit Tests
Create `test_pdf_extraction.py`:

```python
import pytest
from oaic_data_scraper import (
    extract_individuals_affected_distribution,
    extract_median_average_statistics,
    extract_complete_sector_rankings
)

def test_individuals_distribution():
    """Test extraction of individuals affected distribution."""
    # Use a known PDF
    pdf_path = "oaic_pdfs/sample_report.pdf"
    result = extract_individuals_affected_distribution(pdf_path)

    assert result is not None
    assert len(result) >= 5  # Should have at least 5 bins
    assert all('range' in item and 'count' in item for item in result)


def test_median_average():
    """Test extraction of median/average statistics."""
    pdf_path = "oaic_pdfs/sample_report.pdf"
    result = extract_median_average_statistics(pdf_path)

    assert result is not None
    assert 'median' in result or 'average' in result


def test_sector_rankings():
    """Test extraction of complete sector rankings."""
    pdf_path = "oaic_pdfs/sample_report.pdf"
    result = extract_complete_sector_rankings(pdf_path)

    assert result is not None
    assert len(result) >= 5  # Should have Top 5
    assert all('sector' in item and 'notifications' in item for item in result)
```

### 6.2 Integration Tests
1. Run scraper on 3 recent reports
2. Verify JSON structure matches schema
3. Run dashboard generation
4. Verify charts appear and contain data
5. Spot-check accuracy against source PDFs

### 6.3 Validation Criteria
- [ ] All OAIC PDFs successfully downloaded
- [ ] At least 80% of reports have individuals_affected_distribution parsed
- [ ] At least 70% of reports have median/average statistics extracted
- [ ] 100% of reports have complete top_sectors (Top 5)
- [ ] All three new dashboard charts render without errors
- [ ] Data accuracy spot-checked on 5 random reports (within 5% tolerance)

---

## Phase 7: Rollback Plan

### If PDF Parsing Fails
1. **Fallback**: Keep existing HTML-based scraping
2. **Manual entry**: Create `oaic_manual_data.json` for key missing fields
3. **Merge approach**: Combine automated HTML + manual PDF data

### If Dashboard Fails to Render
1. **Isolation**: Comment out new charts, keep existing dashboard functional
2. **Debug**: Test with mock data to identify JavaScript/rendering issues
3. **Progressive rollout**: Enable charts one at a time

---

## Phase 8: Future Enhancements

### Post-Implementation Improvements
1. **OCR fallback**: Use `pytesseract` if PDF text extraction fails
2. **Machine learning**: Train model to identify table regions automatically
3. **Automated validation**: Cross-reference extracted numbers with HTML summaries
4. **Historical backfill**: Parse all historical PDFs (back to 2018)
5. **Real-time alerts**: Detect when new OAIC reports published

---

## Success Metrics

### Quantitative
- **Coverage**: >80% of OAIC reports with full PDF data extracted
- **Accuracy**: >95% accuracy vs manual spot-checks
- **Performance**: Dashboard generation completes in <60 seconds
- **Reliability**: PDF parsing success rate >85%

### Qualitative
- Dashboard provides clear, actionable comparison between OAIC and database
- Visualizations help identify gaps in database coverage
- Data quality issues become immediately visible
- Users can quickly understand trends in individuals affected

---

## Risk Assessment

### High Risk
- **Java dependency for tabula-py**: Requires users to install Java
  - **Mitigation**: Add clear installation instructions, consider pure-Python alternatives

### Medium Risk
- **PDF format changes**: OAIC might change PDF layouts
  - **Mitigation**: Build flexible parsers with multiple fallback patterns

- **Table extraction accuracy**: Complex tables may parse incorrectly
  - **Mitigation**: Validate extracted data, provide manual override mechanism

### Low Risk
- **Performance**: PDF parsing slower than HTML
  - **Mitigation**: Cache parsed PDFs, only re-parse when changed

---

## Timeline Summary

| Phase | Duration | Key Deliverables |
|-------|----------|------------------|
| 1. PDF Infrastructure | 1 day | PDF download, dependencies installed |
| 2. Extraction Functions | 2 days | All 3 extraction functions working |
| 3. Scraper Integration | 1 day | Modified scraper with PDF support |
| 4. Database Queries | 1 day | 3 new query functions |
| 5. Dashboard Viz | 2 days | 3 new charts added |
| 6. Integration Testing | 1 day | End-to-end validation |
| 7. Documentation | 1 day | Complete documentation |
| **Total** | **9 days** | **Fully functional enhanced system** |

---

## Appendix A: Sample Data Structures

### OAIC Report with Full PDF Data
```json
{
  "title": "Notifiable Data Breaches Report: January to June 2024",
  "url": "https://www.oaic.gov.au/privacy/notifiable-data-breaches/...",
  "pdf_url": "https://www.oaic.gov.au/.../report.pdf",
  "year": 2024,
  "period": "H1",
  "quarter": "H1",
  "start_month": 1,
  "end_month": 6,
  "total_notifications": 527,
  "cyber_incidents_percentage": 38,
  "ransomware": 49,
  "phishing": 63,
  "hacking": 14,
  "malware": 105,

  "individuals_affected_distribution": [
    {"range": "1-100", "count": 145},
    {"range": "101-1,000", "count": 178},
    {"range": "1,001-10,000", "count": 102},
    {"range": "10,001-100,000", "count": 48},
    {"range": "100,001+", "count": 54}
  ],

  "individuals_affected_median": 1250,
  "individuals_affected_average": 15500,

  "top_sectors": [
    {"sector": "Health service providers", "notifications": 102},
    {"sector": "Finance", "notifications": 54},
    {"sector": "Education", "notifications": 44},
    {"sector": "Retail", "notifications": 29},
    {"sector": "Legal, accounting and management services", "notifications": 26}
  ],

  "pdf_parsed": true,
  "pdf_parsing_errors": []
}
```

---

## Appendix B: Key Files Modified

| File | Lines Changed | Description |
|------|---------------|-------------|
| `requirements.txt` | +2 | Add pdfplumber, tabula-py |
| `oaic_data_scraper.py` | +350 | Add PDF parsing functions |
| `build_static_dashboard.py` | +400 | Add 3 queries, 3 chart sections |
| `dashboard/index.html` | Auto-generated | New charts embedded |

---

## Conclusion

This plan provides a comprehensive, phased approach to implementing OAIC PDF data extraction and enhanced dashboard visualizations. The modular design allows for incremental testing and rollback if needed. With 9 days of focused development, we can deliver three powerful new comparison visualizations that provide deep insights into the relationship between OAIC reported breaches and our database events.
