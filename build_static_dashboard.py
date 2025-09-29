#!/usr/bin/env python3
"""
Build a static Australian Cyber Security Events Dashboard as a single HTML file.

- Connects directly to SQLite database
- Queries data for the fixed date range: 2020-01-01 to today (local system date)
- Renders charts with Chart.js via CDN (no Flask, no API calls)
- Outputs to dashboard/index.html (creates folder if needed)

Usage:
    python build_static_dashboard.py --db-path instance/cyber_events.db --out-dir dashboard
"""

import argparse
import os
import sqlite3
import json
import glob
from datetime import date
from pathlib import Path
from typing import Dict, Any, List, Optional
import pandas as pd


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def get_monthly_event_counts(conn: sqlite3.Connection, start_date: str, end_date: str) -> Dict[str, Any]:
    query = """
        SELECT
            strftime('%Y-%m', event_date) as month,
            COUNT(DISTINCT deduplicated_event_id) as unique_events
        FROM DeduplicatedEvents
        WHERE status = 'Active'
            AND event_date >= ?
            AND event_date <= ?
        GROUP BY strftime('%Y-%m', event_date)
        ORDER BY month
    """
    rows = conn.execute(query, (start_date, end_date)).fetchall()
    months = [r['month'] for r in rows if r['month']]
    counts = [r['unique_events'] for r in rows if r['month']]
    return {
        'months': months,
        'counts': counts,
        'total_events': sum(counts),
        'avg_per_month': (sum(counts) / len(counts)) if counts else 0,
    }


def get_monthly_severity_trends(conn: sqlite3.Connection, start_date: str, end_date: str) -> Dict[str, Any]:
    query = """
        SELECT
            strftime('%Y-%m', event_date) as month,
            severity,
            COUNT(DISTINCT deduplicated_event_id) as event_count
        FROM DeduplicatedEvents
        WHERE status = 'Active'
            AND event_date >= ?
            AND event_date <= ?
        GROUP BY strftime('%Y-%m', event_date), severity
        ORDER BY month,
            CASE severity
                WHEN 'Critical' THEN 1
                WHEN 'High' THEN 2
                WHEN 'Medium' THEN 3
                WHEN 'Low' THEN 4
                ELSE 5
            END
    """
    rows = conn.execute(query, (start_date, end_date)).fetchall()
    months: List[str] = []
    severity_data: Dict[str, List[int]] = {}
    for r in rows:
        if not r['month']:
            continue
        month = r['month']
        sev_raw = r['severity'] or 'Unknown'
        severity = sev_raw.replace('EventSeverity.', '').title() if sev_raw.startswith('EventSeverity.') else sev_raw
        count = r['event_count']
        if month not in months:
            months.append(month)
        if severity not in severity_data:
            severity_data[severity] = []
        while len(severity_data[severity]) < len(months):
            severity_data[severity].append(0)
        severity_data[severity][-1] = count
    for sev in list(severity_data.keys()):
        while len(severity_data[sev]) < len(months):
            severity_data[sev].append(0)
    return {'months': months, 'data': severity_data}


def get_monthly_records_affected(conn: sqlite3.Connection, start_date: str, end_date: str) -> Dict[str, Any]:
    query = """
        SELECT
            strftime('%Y-%m', event_date) as month,
            AVG(CAST(records_affected AS FLOAT)) as avg_records,
            COUNT(DISTINCT deduplicated_event_id) as sample_size,
            MIN(CAST(records_affected AS FLOAT)) as min_records,
            MAX(CAST(records_affected AS FLOAT)) as max_records
        FROM DeduplicatedEvents
        WHERE status = 'Active'
            AND records_affected IS NOT NULL
            AND CAST(records_affected AS INTEGER) > 0
            AND event_date >= ?
            AND event_date <= ?
        GROUP BY strftime('%Y-%m', event_date)
        ORDER BY month
    """
    rows = conn.execute(query, (start_date, end_date)).fetchall()
    months, averages, ci, sample_sizes = [], [], [], []
    for r in rows:
        if not r['month']:
            continue
        months.append(r['month'])
        avg = r['avg_records'] or 0
        averages.append(avg)
        sample_sizes.append(r['sample_size'] or 0)
        margin = avg * 0.2
        ci.append([max(0, avg - margin), avg + margin])
    return {'months': months, 'averages': averages, 'confidence_intervals': ci, 'sample_sizes': sample_sizes}


def get_monthly_event_type_mix(conn: sqlite3.Connection, start_date: str, end_date: str) -> Dict[str, Any]:
    query = """
        SELECT
            strftime('%Y-%m', event_date) as month,
            event_type,
            COUNT(DISTINCT deduplicated_event_id) as event_count
        FROM DeduplicatedEvents
        WHERE status = 'Active'
            AND event_date >= ?
            AND event_date <= ?
        GROUP BY strftime('%Y-%m', event_date), event_type
        ORDER BY month, event_type
    """
    rows = conn.execute(query, (start_date, end_date)).fetchall()
    months: List[str] = []
    event_types: Dict[str, List[int]] = {}
    for r in rows:
        if not r['month']:
            continue
        month = r['month']
        et_raw = r['event_type'] or 'Unknown'
        event_type = et_raw.replace('CyberEventType.', '').replace('_', ' ').title() if et_raw.startswith('CyberEventType.') else et_raw
        count = r['event_count']
        if month not in months:
            months.append(month)
        if event_type not in event_types:
            event_types[event_type] = []
        while len(event_types[event_type]) < len(months):
            event_types[event_type].append(0)
        event_types[event_type][-1] = count
    for et in list(event_types.keys()):
        while len(event_types[et]) < len(months):
            event_types[et].append(0)
    return {'months': months, 'types': event_types}


def get_overall_event_type_mix(conn: sqlite3.Connection, start_date: str, end_date: str) -> Dict[str, Any]:
    """Get overall event type distribution across the full period."""
    query = """
        SELECT
            COALESCE(event_type, 'Unknown') as event_type,
            COUNT(DISTINCT deduplicated_event_id) as event_count
        FROM DeduplicatedEvents
        WHERE status = 'Active'
            AND event_date >= ?
            AND event_date <= ?
        GROUP BY event_type
        ORDER BY event_count DESC
    """
    rows = conn.execute(query, (start_date, end_date)).fetchall()
    labels: List[str] = []
    counts: List[int] = []
    for r in rows:
        et_raw = r['event_type'] or 'Unknown'
        label = et_raw.replace('CyberEventType.', '').replace('_', ' ').title() if et_raw.startswith('CyberEventType.') else et_raw
        labels.append(label)
        counts.append(r['event_count'])
    return {'labels': labels, 'counts': counts}


def get_entity_type_distribution(conn: sqlite3.Connection, start_date: str, end_date: str) -> Dict[str, Any]:
    query = """
        SELECT
            COALESCE(e.entity_type, 'Unknown') as entity_type,
            COUNT(DISTINCT de.deduplicated_event_id) as linked_events,
            COUNT(DISTINCT e.entity_id) as unique_entities
        FROM EntitiesV2 e
        INNER JOIN DeduplicatedEventEntities dee ON e.entity_id = dee.entity_id
        INNER JOIN DeduplicatedEvents de ON dee.deduplicated_event_id = de.deduplicated_event_id
        WHERE de.status = 'Active'
            AND de.event_date >= ?
            AND de.event_date <= ?
        GROUP BY e.entity_type
        ORDER BY linked_events DESC
    """
    rows = conn.execute(query, (start_date, end_date)).fetchall()
    types = [{'type': r['entity_type'], 'events': r['linked_events'], 'entities': r['unique_entities']} for r in rows]
    return {'types': types}


def get_records_affected_histogram(conn: sqlite3.Connection, start_date: str, end_date: str) -> Dict[str, Any]:
    query = """
        SELECT
            CAST(records_affected AS INTEGER) as records_affected,
            event_type,
            severity,
            COUNT(*) as frequency
        FROM DeduplicatedEvents
        WHERE status = 'Active'
            AND records_affected IS NOT NULL
            AND CAST(records_affected AS INTEGER) > 0
            AND event_date >= ?
            AND event_date <= ?
        GROUP BY CAST(records_affected AS INTEGER), event_type, severity
        ORDER BY CAST(records_affected AS INTEGER)
    """
    rows = conn.execute(query, (start_date, end_date)).fetchall()
    bins = ['1-10', '11-100', '101-1K', '1K-10K', '10K-100K', '100K-1M', '1M-10M', '10M+']
    freqs = [0] * len(bins)

    def bin_idx(value: int) -> int:
        if value <= 10:
            return 0
        elif value <= 100:
            return 1
        elif value <= 1000:
            return 2
        elif value <= 10000:
            return 3
        elif value <= 100000:
            return 4
        elif value <= 1000000:
            return 5
        elif value <= 10000000:
            return 6
        else:
            return 7

    for r in rows:
        records = r['records_affected']
        freq = r['frequency']
        freqs[bin_idx(records)] += freq
    return {'bins': bins, 'frequencies': freqs}


def load_oaic_data() -> List[Dict[str, Any]]:
    """Load OAIC data from JSON files created by oaic_data_scraper.py"""
    oaic_files = glob.glob('oaic_cyber_statistics_*.json')

    if not oaic_files:
        print("Warning: No OAIC data files found. Run oaic_data_scraper.py first.")
        return []

    # Get the most recent OAIC data file
    latest_file = max(oaic_files, key=os.path.getctime)

    try:
        with open(latest_file, 'r', encoding='utf-8') as f:
            oaic_data = json.load(f)
        print(f"Loaded OAIC data from: {latest_file}")
        return oaic_data
    except Exception as e:
        print(f"Error loading OAIC data: {e}")
        return []


def get_half_yearly_database_counts(conn: sqlite3.Connection, start_date: str, end_date: str) -> Dict[str, Any]:
    """Get half-yearly event counts from the database to compare with OAIC data."""
    query = """
        SELECT
            CASE
                WHEN CAST(strftime('%m', event_date) AS INTEGER) BETWEEN 1 AND 6 THEN 'H1'
                ELSE 'H2'
            END as half_year,
            strftime('%Y', event_date) as year,
            COUNT(DISTINCT deduplicated_event_id) as unique_events
        FROM DeduplicatedEvents
        WHERE status = 'Active'
            AND event_date >= ?
            AND event_date <= ?
            AND CAST(strftime('%Y', event_date) AS INTEGER) >= 2020
        GROUP BY strftime('%Y', event_date),
            CASE
                WHEN CAST(strftime('%m', event_date) AS INTEGER) BETWEEN 1 AND 6 THEN 'H1'
                ELSE 'H2'
            END
        ORDER BY year, half_year
    """

    rows = conn.execute(query, (start_date, end_date)).fetchall()

    periods = []
    database_counts = []

    for row in rows:
        year = row['year']
        half_year = row['half_year']
        count = row['unique_events']

        period_label = f"{year} {half_year}"
        periods.append(period_label)
        database_counts.append(count)

    return {
        'periods': periods,
        'database_counts': database_counts
    }


def prepare_oaic_comparison_data(database_data: Dict[str, Any], oaic_data: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Prepare data for OAIC vs Database comparison chart."""
    # Create a mapping of OAIC data by period
    oaic_lookup = {}
    for record in oaic_data:
        year = record.get('year')
        period = record.get('period')  # H1 or H2
        total_notifications = record.get('total_notifications')

        if year and period and total_notifications:
            period_key = f"{year} {period}"
            oaic_lookup[period_key] = total_notifications

    # Align database and OAIC data
    periods = database_data['periods']
    database_counts = database_data['database_counts']
    oaic_counts = []

    for period in periods:
        oaic_count = oaic_lookup.get(period, None)
        oaic_counts.append(oaic_count)

    # Filter to only include periods where we have both datasets (from 2020 H1 onwards)
    filtered_periods = []
    filtered_database = []
    filtered_oaic = []

    for i, period in enumerate(periods):
        # Only include periods from 2020 H1 onwards
        year_str = period.split()[0]
        if int(year_str) >= 2020:
            filtered_periods.append(period)
            filtered_database.append(database_counts[i])
            filtered_oaic.append(oaic_counts[i])

    return {
        'periods': filtered_periods,
        'database_counts': filtered_database,
        'oaic_counts': filtered_oaic,
        'oaic_available': len([x for x in filtered_oaic if x is not None]),
        'database_available': len([x for x in filtered_database if x is not None])
    }


def compute_event_type_correlation_matrix(event_type_mix: Dict[str, Any]) -> Dict[str, Any]:
    """Calculate correlation matrix between event types based on monthly counts."""
    types = event_type_mix['types']
    
    # Get all event types that have sufficient data (at least 5 non-zero months)
    valid_types = []
    for event_type, counts in types.items():
        non_zero_count = sum(1 for count in counts if count > 0)
        if non_zero_count >= 5:  # Require at least 5 months with events
            valid_types.append(event_type)
    
    if len(valid_types) < 2:
        return {
            'labels': [],
            'correlation_matrix': [],
            'min_correlation': 0,
            'max_correlation': 0
        }
    
    # Create DataFrame with monthly counts for valid event types
    data_dict = {}
    for event_type in valid_types:
        data_dict[event_type] = types[event_type]
    
    df = pd.DataFrame(data_dict)
    
    # Calculate correlation matrix
    correlation_matrix = df.corr()
    
    # Handle NaN values (can occur if a type has zero variance)
    correlation_matrix = correlation_matrix.fillna(0.0)
    
    # Round to 3 decimal places for cleaner display
    correlation_matrix = correlation_matrix.round(3)
    
    return {
        'labels': valid_types,
        'correlation_matrix': correlation_matrix.values.tolist(),
        'min_correlation': float(correlation_matrix.min().min()),
        'max_correlation': float(correlation_matrix.max().max())
    }


def compute_monthly_counts_stats(monthly_counts: Dict[str, Any]) -> Dict[str, Any]:
    """Compute histogram and dispersion estimate for monthly unique event counts.
    - Uses 10 equal-width bins from min to max (inclusive).
    - Estimates Negative Binomial dispersion k = mean^2 / (var - mean) when var > mean; else k = None.
    - Provides a model suggestion string.
    """
    counts = [int(x) for x in monthly_counts.get('counts', []) if x is not None]
    n = len(counts)
    if n == 0:
        return {'bins': [], 'frequencies': [], 'mean': 0, 'variance': 0, 'k_estimate': None, 'model': 'No data'}

    mean = sum(counts) / n
    variance = sum((c - mean) ** 2 for c in counts) / n if n > 0 else 0

    cmin, cmax = min(counts), max(counts)
    if cmin == cmax:
        # Single-value case: one bin
        bins = [f"{cmin}"]
        freqs = [n]
    else:
        num_bins = 10
        width = (cmax - cmin) / num_bins
        edges = [cmin + i * width for i in range(num_bins)] + [cmax]
        bins = [f"{int(edges[i])} - {int(edges[i+1])}" for i in range(num_bins)]
        freqs = [0] * num_bins
        for v in counts:
            # Find bin index
            if v == cmax:
                idx = num_bins - 1
            else:
                idx = int((v - cmin) / width)
                idx = max(0, min(num_bins - 1, idx))
            freqs[idx] += 1

    if variance > mean:
        denom = (variance - mean)
        k_estimate = (mean * mean / denom) if denom > 0 else None
        model = 'Over-dispersed (likely Negative Binomial)'
    elif abs(variance - mean) / (mean + 1e-9) < 0.1:
        k_estimate = None
        model = 'Variance ≈ Mean (close to Poisson)'
    else:
        k_estimate = None
        model = 'Under-dispersed (not Poisson/NB)'

    return {
        'bins': bins,
        'frequencies': freqs,
        'mean': mean,
        'variance': variance,
        'k_estimate': k_estimate,
        'model': model,
    }


def build_html(data: Dict[str, Any], start_date: str, end_date: str) -> str:
    """Return full static HTML content embedding data and rendering charts."""
    mc = json.dumps(data['monthly_counts'])
    sev = json.dumps(data['severity_trends'])
    ra = json.dumps(data['records_affected'])
    etm = json.dumps(data['event_type_mix'])
    oetm = json.dumps(data['overall_event_type_mix'])
    ent = json.dumps(data['entity_types'])
    rh = json.dumps(data['records_histogram'])
    mcs = json.dumps(data['monthly_counts_stats'])
    etc = json.dumps(data['event_type_correlation'])
    oaic_comp = json.dumps(data.get('oaic_comparison', {'periods': [], 'database_counts': [], 'oaic_counts': []}))

    template = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Australian Cyber Security Events Dashboard</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet" />
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><text y='14' font-size='14'>🛡️</text></svg>" />
  <style>
    body { background-color: #f8f9fa; }
    .dashboard-header { background: linear-gradient(135deg, #2563eb, #1e40af); color: white; padding: 2rem 0; margin-bottom: 2rem; }
    .chart-container { background: white; border-radius: 12px; padding: 1.25rem; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 2rem; height: 380px; }
    .chart-title { font-size: 1.1rem; font-weight: 600; color: #374151; margin-bottom: 1rem; text-align: center; }
    .last-updated { color: #e5e7eb; font-size: 0.9rem; }
  </style>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
</head>
<body>
  <div class="dashboard-header">
    <div class="container">
      <div class="row align-items-center">
        <div class="col-md-8">
          <h1 class="mb-0">Australian Cyber Security Events Dashboard</h1>
          <p class="mb-0 last-updated">Date Range: __START__ to __END__</p>
        </div>
      </div>
    </div>
  </div>

  <div class="container">
    <div class="row">
      <div class="col-lg-6 col-md-12">
        <div class="chart-container">
          <div class="chart-title">Monthly Trends in Unique Event Count</div>
          <canvas id="monthlyTrendsChart"></canvas>
        </div>
      </div>
      <div class="col-lg-6 col-md-12">
        <div class="chart-container">
          <div class="chart-title">Monthly Trends in Severity</div>
          <canvas id="severityTrendsChart"></canvas>
        </div>
      </div>
      <div class="col-lg-6 col-md-12">
        <div class="chart-container">
          <div class="chart-title">Monthly Trends in Average Records Affected</div>
          <canvas id="recordsAffectedChart"></canvas>
        </div>
      </div>
      <div class="col-lg-6 col-md-12">
        <div class="chart-container">
          <div class="chart-title">Monthly Trends in Event Type Mix</div>
          <canvas id="eventTypeMixChart"></canvas>
        </div>
      </div>
      <div class="col-lg-6 col-md-12">
        <div class="chart-container">
          <div class="chart-title">Overall Mix of Entity Types</div>
          <canvas id="entityTypeChart"></canvas>
        </div>
      </div>
      <div class="col-lg-6 col-md-12">
        <div class="chart-container">
          <div class="chart-title">Overall Event Type Mix (Pie)</div>
          <canvas id="overallEventTypePieChart"></canvas>
        </div>
      </div>
      <div class="col-lg-6 col-md-12">
        <div class="chart-container">
          <div class="chart-title">Records Affected Histogram</div>
          <canvas id="recordsHistogramChart"></canvas>
        </div>
      </div>
      <div class="col-lg-6 col-md-12">
        <div class="chart-container">
          <div class="chart-title">Histogram of Monthly Unique Event Counts</div>
          <canvas id="monthlyCountsHistChart"></canvas>
          <div id="monthlyCountsStats" class="mt-2" style="font-size: 0.9rem; color: #374151;"></div>
        </div>
      </div>
      <div class="col-lg-6 col-md-12">
        <div class="chart-container">
          <div class="chart-title">Database vs OAIC Official Notifications (Half-Yearly)</div>
          <canvas id="oaicComparisonChart"></canvas>
          <div id="oaicComparisonStats" class="mt-2" style="font-size: 0.9rem; color: #374151;"></div>
        </div>
      </div>
      <div class="col-lg-12 col-md-12">
        <div class="chart-container" style="height: 500px;">
          <div class="chart-title">Event Type Correlation Matrix (Monthly Counts)</div>
          <canvas id="correlationMatrixChart"></canvas>
          <div id="correlationMatrixStats" class="mt-2" style="font-size: 0.9rem; color: #374151;"></div>
        </div>
      </div>
    </div>
  </div>

  <script>
    // Embedded data (static)
    const monthlyCounts = __MC__;
    const severityTrends = __SEV__;
    const recordsAffected = __RA__;
    const eventTypeMix = __ETM__;
    const overallEventTypeMix = __OETM__;
    const entityTypes = __ENT__;
    const recordsHistogram = __RH__;
    const monthlyCountsStats = __MCS__;
    const eventTypeCorrelation = __ETC__;
    const oaicComparison = __OAIC_COMP__;

    const colors = {
      primary: '#2563eb',
      secondary: '#10b981',
      warning: '#f59e0b',
      danger: '#ef4444',
      info: '#06b6d4',
      severity: {
        'Critical': '#dc2626', 'High': '#ea580c', 'Medium': '#ca8a04', 'Low': '#16a34a', 'Unknown': '#6b7280'
      }
    };

    // 1) Monthly Trends (line)
    new Chart(document.getElementById('monthlyTrendsChart').getContext('2d'), {
      type: 'line',
      data: {
        labels: monthlyCounts.months,
        datasets: [{
          label: 'Unique Events',
          data: monthlyCounts.counts,
          borderColor: colors.primary,
          backgroundColor: '#2563eb20',
          fill: true,
          tension: 0.4,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false }},
        scales: { y: { beginAtZero: true }, x: { title: { display: true, text: 'Month' }}}
      }
    });

    // 2) Severity Trends (stacked bar)
    const sevDatasets = Object.keys(severityTrends.data).map(sev => ({
      label: sev,
      data: severityTrends.data[sev],
      backgroundColor: colors.severity[sev] || '#6b7280',
      borderColor: colors.severity[sev] || '#6b7280',
    }));
    new Chart(document.getElementById('severityTrendsChart').getContext('2d'), {
      type: 'bar',
      data: { labels: severityTrends.months, datasets: sevDatasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'top' } },
        scales: {
          x: { stacked: true, title: { display: true, text: 'Month' }},
          y: { stacked: true, beginAtZero: true, title: { display: true, text: 'Events' }}
        }
      }
    });

    // 3) Records Affected (line, log scale)
    new Chart(document.getElementById('recordsAffectedChart').getContext('2d'), {
      type: 'line',
      data: { labels: recordsAffected.months, datasets: [{
        label: 'Average Records Affected', data: recordsAffected.averages,
        borderColor: colors.secondary, backgroundColor: '#10b98120', fill: false, tension: 0.4
      }]},
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false }},
        scales: {
          y: { type: 'logarithmic', beginAtZero: true, title: { display: true, text: 'Records (log)' }},
          x: { title: { display: true, text: 'Month' }}
        }
      }
    });

    // 4) Event Type Mix (stacked bar)
    function generateUniqueColors(n) {
      // Golden angle based distinct colors
      const colors = [];
      const golden = 137.508;
      for (let i = 0; i < n; i++) {
        const hue = (i * golden) % 360;
        const sat = 70;
        const light = 50;
        colors.push(`hsl(${hue}, ${sat}%, ${light}%)`);
      }
      return colors;
    }
    const etKeys = Object.keys(eventTypeMix.types);
    const uniqueColors = generateUniqueColors(etKeys.length);
    const etDatasets = etKeys.map((t, i) => ({
      label: t,
      data: eventTypeMix.types[t],
      backgroundColor: uniqueColors[i],
      borderColor: uniqueColors[i]
    }));
    new Chart(document.getElementById('eventTypeMixChart').getContext('2d'), {
      type: 'bar',
      data: { labels: eventTypeMix.months, datasets: etDatasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'top' }},
        scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true }}
      }
    });

    // 5) Entity Type Distribution (doughnut)
    const entityLabels = entityTypes.types.map(t => t.type);
    const entityValues = entityTypes.types.map(t => t.events);
    new Chart(document.getElementById('entityTypeChart').getContext('2d'), {
      type: 'doughnut',
      data: { labels: entityLabels, datasets: [{ data: entityValues, backgroundColor: entityLabels.map((_, i) => `hsl(${(i*360/entityLabels.length)||0},70%,50%)`) }]},
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'right' }}}
    });

    // 6) Overall Event Type Mix (pie)
    (function(){
      const labels = overallEventTypeMix.labels || [];
      const counts = overallEventTypeMix.counts || [];
      const colors = generateUniqueColors(labels.length);
      new Chart(document.getElementById('overallEventTypePieChart').getContext('2d'), {
        type: 'pie',
        data: { labels, datasets: [{ data: counts, backgroundColor: colors, borderColor: colors }]},
        options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'right' }}}
      });
    })();

    // 6) Records Histogram (bar)
    new Chart(document.getElementById('recordsHistogramChart').getContext('2d'), {
      type: 'bar',
      data: { labels: recordsHistogram.bins, datasets: [{ label: 'Frequency', data: recordsHistogram.frequencies, backgroundColor: '#06b6d480', borderColor: '#06b6d4' }]},
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false }}, scales: { y: { beginAtZero: true }}}
    });

    // 7) Histogram of Monthly Unique Event Counts + dispersion stats
    new Chart(document.getElementById('monthlyCountsHistChart').getContext('2d'), {
      type: 'bar',
      data: { labels: monthlyCountsStats.bins, datasets: [{ label: 'Months', data: monthlyCountsStats.frequencies, backgroundColor: '#64748b80', borderColor: '#64748b' }]},
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false }}, scales: { y: { beginAtZero: true }}}
    });
    (function(){
      const s = monthlyCountsStats;
      const lines = [];
      lines.push(`Mean: ${s.mean?.toFixed ? s.mean.toFixed(2) : s.mean}`);
      lines.push(`Variance: ${s.variance?.toFixed ? s.variance.toFixed(2) : s.variance}`);
      if (s.k_estimate) lines.push(`NB dispersion k (est.): ${s.k_estimate.toFixed ? s.k_estimate.toFixed(2) : s.k_estimate}`);
      lines.push(`Model: ${s.model}`);
      document.getElementById('monthlyCountsStats').textContent = lines.join(' • ');
    })();

    // 9) OAIC Comparison Chart
    (function(){
      const oaic = oaicComparison;
      if (!oaic.periods || oaic.periods.length === 0) {
        document.getElementById('oaicComparisonChart').parentElement.innerHTML =
          '<div class="chart-title">Database vs OAIC Official Notifications (Half-Yearly)</div>' +
          '<div class="text-center text-muted mt-5"><p>No OAIC data available.<br>Run <code>python oaic_data_scraper.py</code> first.</p></div>';
        return;
      }

      const datasets = [
        {
          label: 'Database Events',
          data: oaic.database_counts,
          borderColor: colors.primary,
          backgroundColor: colors.primary + '20',
          fill: false,
          tension: 0.1,
          pointRadius: 5
        }
      ];

      // Only add OAIC dataset if we have OAIC data
      if (oaic.oaic_available > 0) {
        datasets.push({
          label: 'OAIC Official Notifications',
          data: oaic.oaic_counts,
          borderColor: colors.danger,
          backgroundColor: colors.danger + '20',
          fill: false,
          tension: 0.1,
          pointRadius: 5,
          spanGaps: false  // Don't connect points where data is missing
        });
      }

      new Chart(document.getElementById('oaicComparisonChart').getContext('2d'), {
        type: 'line',
        data: {
          labels: oaic.periods,
          datasets: datasets
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: {
              position: 'top'
            },
            tooltip: {
              callbacks: {
                footer: function(tooltipItems) {
                  const dbCount = tooltipItems.find(item => item.datasetIndex === 0);
                  const oaicCount = tooltipItems.find(item => item.datasetIndex === 1);

                  if (dbCount && oaicCount && oaicCount.raw !== null) {
                    const ratio = (dbCount.raw / oaicCount.raw).toFixed(2);
                    return `Ratio (DB/OAIC): ${ratio}x`;
                  }
                  return '';
                }
              }
            }
          },
          scales: {
            y: {
              beginAtZero: true,
              title: {
                display: true,
                text: 'Number of Events/Notifications'
              }
            },
            x: {
              title: {
                display: true,
                text: 'Half-Year Period'
              }
            }
          }
        }
      });

      // Add summary statistics
      const stats = [];
      stats.push(`Database periods: ${oaic.database_available}`);
      if (oaic.oaic_available > 0) {
        stats.push(`OAIC periods: ${oaic.oaic_available}`);

        // Calculate ratios for overlapping periods
        const ratios = [];
        for (let i = 0; i < oaic.periods.length; i++) {
          const dbCount = oaic.database_counts[i];
          const oaicCount = oaic.oaic_counts[i];
          if (dbCount && oaicCount) {
            ratios.push(dbCount / oaicCount);
          }
        }

        if (ratios.length > 0) {
          const avgRatio = ratios.reduce((a, b) => a + b, 0) / ratios.length;
          stats.push(`Avg ratio (DB/OAIC): ${avgRatio.toFixed(2)}x`);
        }
      } else {
        stats.push('OAIC: No data available');
      }

      document.getElementById('oaicComparisonStats').textContent = stats.join(' • ');
    })();

    // 10) Event Type Correlation Matrix
    (function(){
      const corr = eventTypeCorrelation;
      if (!corr.labels || corr.labels.length === 0) {
        document.getElementById('correlationMatrixChart').parentElement.innerHTML =
          '<div class="chart-title">Event Type Correlation Matrix (Monthly Counts)</div>' +
          '<div class="text-center text-muted mt-5"><p>Insufficient data for correlation analysis.<br>Need at least 2 event types with 5+ months of data.</p></div>';
        return;
      }

      const labels = corr.labels;
      const matrix = corr.correlation_matrix;
      
      // Create a custom heatmap using HTML table since Chart.js doesn't have a native heatmap
      const canvas = document.getElementById('correlationMatrixChart');
      const ctx = canvas.getContext('2d');
      const container = canvas.parentElement;
      
      // Create table-based heatmap
      const table = document.createElement('table');
      table.style.width = '100%';
      table.style.height = '100%';
      table.style.borderCollapse = 'collapse';
      table.style.fontSize = '12px';
      
      // Create header row
      const headerRow = document.createElement('tr');
      const emptyCell = document.createElement('th');
      emptyCell.style.border = '1px solid #ddd';
      emptyCell.style.padding = '8px';
      emptyCell.style.backgroundColor = '#f8f9fa';
      headerRow.appendChild(emptyCell);
      
      labels.forEach(label => {
        const th = document.createElement('th');
        th.textContent = label;
        th.style.border = '1px solid #ddd';
        th.style.padding = '8px';
        th.style.backgroundColor = '#f8f9fa';
        th.style.fontSize = '11px';
        th.style.transform = 'rotate(-45deg)';
        th.style.transformOrigin = 'center';
        th.style.whiteSpace = 'nowrap';
        headerRow.appendChild(th);
      });
      table.appendChild(headerRow);
      
      // Create data rows
      labels.forEach((label, i) => {
        const row = document.createElement('tr');
        
        // Row label
        const rowLabel = document.createElement('td');
        rowLabel.textContent = label;
        rowLabel.style.border = '1px solid #ddd';
        rowLabel.style.padding = '8px';
        rowLabel.style.backgroundColor = '#f8f9fa';
        rowLabel.style.fontSize = '11px';
        rowLabel.style.fontWeight = 'bold';
        row.appendChild(rowLabel);
        
        // Data cells
        labels.forEach((_, j) => {
          const cell = document.createElement('td');
          const value = matrix[i][j];
          cell.textContent = value.toFixed(3);
          cell.style.border = '1px solid #ddd';
          cell.style.padding = '8px';
          cell.style.textAlign = 'center';
          cell.style.fontSize = '11px';
          cell.style.fontWeight = 'bold';
          
          // Color coding
          if (value < 0) {
            const intensity = Math.abs(value);
            const alpha = 0.3 + intensity * 0.7;
            cell.style.backgroundColor = `rgba(239, 68, 68, ${alpha})`;
            cell.style.color = intensity > 0.5 ? 'white' : 'black';
          } else if (value > 0) {
            const intensity = value;
            const alpha = 0.3 + intensity * 0.7;
            cell.style.backgroundColor = `rgba(59, 130, 246, ${alpha})`;
            cell.style.color = intensity > 0.5 ? 'white' : 'black';
          } else {
            cell.style.backgroundColor = 'rgba(156, 163, 175, 0.3)';
            cell.style.color = 'black';
          }
          
          // Tooltip
          cell.title = `${labels[i]} vs ${labels[j]}: ${value.toFixed(3)}`;
          row.appendChild(cell);
        });
        
        table.appendChild(row);
      });
      
      // Replace canvas with table
      container.replaceChild(table, canvas);
      
      // Add correlation statistics
      const stats = [];
      stats.push(`Event types: ${labels.length}`);
      stats.push(`Min correlation: ${corr.min_correlation.toFixed(3)}`);
      stats.push(`Max correlation: ${corr.max_correlation.toFixed(3)}`);
      
      // Find strongest positive and negative correlations
      let maxPos = -1, maxNeg = 1;
      let maxPosPair = '', maxNegPair = '';
      
      for (let i = 0; i < labels.length; i++) {
        for (let j = i + 1; j < labels.length; j++) {
          const corrValue = matrix[i][j];
          if (corrValue > maxPos) {
            maxPos = corrValue;
            maxPosPair = `${labels[i]} & ${labels[j]}`;
          }
          if (corrValue < maxNeg) {
            maxNeg = corrValue;
            maxNegPair = `${labels[i]} & ${labels[j]}`;
          }
        }
      }
      
      if (maxPosPair) stats.push(`Strongest positive: ${maxPosPair} (${maxPos.toFixed(3)})`);
      if (maxNegPair) stats.push(`Strongest negative: ${maxNegPair} (${maxNeg.toFixed(3)})`);

      document.getElementById('correlationMatrixStats').textContent = stats.join(' • ');
    })();
  </script>
</body>
</html>
"""

    return (template
            .replace('__START__', start_date)
            .replace('__END__', end_date)
            .replace('__MC__', mc)
            .replace('__SEV__', sev)
            .replace('__RA__', ra)
            .replace('__ETM__', etm)
            .replace('__OETM__', oetm)
            .replace('__ENT__', ent)
            .replace('__RH__', rh)
            .replace('__MCS__', mcs)
            .replace('__ETC__', etc)
            .replace('__OAIC_COMP__', oaic_comp)
           )


def main():
    parser = argparse.ArgumentParser(description='Build a static dashboard HTML (no Flask).')
    parser.add_argument('--db-path', default='instance/cyber_events.db', help='Path to SQLite database file')
    parser.add_argument('--out-dir', default='dashboard', help='Output directory for static HTML')
    args = parser.parse_args()

    start_date = '2020-01-01'
    end_date = date.today().strftime('%Y-%m-%d')

    if not os.path.exists(args.db_path):
        raise FileNotFoundError(f'Database not found: {args.db_path}')

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / 'index.html'

    # Load OAIC data
    oaic_data = load_oaic_data()

    with get_connection(args.db_path) as conn:
        monthly_counts = get_monthly_event_counts(conn, start_date, end_date)

        # Get half-yearly database counts for OAIC comparison
        database_half_yearly = get_half_yearly_database_counts(conn, start_date, end_date)
        oaic_comparison = prepare_oaic_comparison_data(database_half_yearly, oaic_data)

        event_type_mix = get_monthly_event_type_mix(conn, start_date, end_date)
        
        data = {
            'monthly_counts': monthly_counts,
            'severity_trends': get_monthly_severity_trends(conn, start_date, end_date),
            'records_affected': get_monthly_records_affected(conn, start_date, end_date),
            'event_type_mix': event_type_mix,
            'overall_event_type_mix': get_overall_event_type_mix(conn, start_date, end_date),
            'entity_types': get_entity_type_distribution(conn, start_date, end_date),
            'records_histogram': get_records_affected_histogram(conn, start_date, end_date),
            'monthly_counts_stats': compute_monthly_counts_stats(monthly_counts),
            'event_type_correlation': compute_event_type_correlation_matrix(event_type_mix),
            'oaic_comparison': oaic_comparison,
        }

    html = build_html(data, start_date, end_date)
    out_file.write_text(html, encoding='utf-8')
    print(f'Static dashboard generated: {out_file}')


if __name__ == '__main__':
    main()
