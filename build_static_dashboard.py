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

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import json
import glob
from datetime import date
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import pandas as pd
import numpy as np
from scipy import stats

logger = logging.getLogger(__name__)

ASD_VALID_STAKEHOLDER_CATEGORIES = [
    "Member(s) of the public",
    "Small organisation(s)",
    "Sole traders",
    "Medium-sized organisation(s)",
    "Schools",
    "Local government",
    "State government",
    "Academia/R&D",
    "Large organisation(s)",
    "Supply chain",
    "Federal government",
    "Government shared services",
    "Regulated critical infrastructure",
    "National security",
    "Systems of National Significance"
]

ASD_STAKEHOLDER_GROUPS = {
    "Member(s) of the public": ["Member(s) of the public"],
    "Small organisation(s) / Sole traders": [
        "Small organisation(s)",
        "Sole traders"
    ],
    "Medium-sized organisation(s) / Schools / Local government": [
        "Medium-sized organisation(s)",
        "Schools",
        "Local government"
    ],
    "State government / Academia/R&D / Large organisation(s) / Supply chain": [
        "State government",
        "Academia/R&D",
        "Large organisation(s)",
        "Supply chain"
    ],
    "Federal government / Government shared services / Regulated critical infrastructure": [
        "Federal government",
        "Government shared services",
        "Regulated critical infrastructure"
    ],
    "National security / Systems of National Significance": [
        "National security",
        "Systems of National Significance"
    ]
}

ASD_VALID_IMPACT_TYPES = [
    "Sustained disruption of essential systems and associated services",
    "Extensive compromise",
    "Isolated compromise",
    "Coordinated low-level malicious attack",
    "Low-level malicious attack",
    "Unsuccessful low-level malicious attack"
]


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

    # Calculate overdispersion statistics
    overdispersion_stats = {}
    if len(counts) > 1:
        counts_arr = np.array(counts)
        mean_count = np.mean(counts_arr)
        var_count = np.var(counts_arr, ddof=1)  # Sample variance

        # Overdispersion parameter: φ = variance / mean
        # φ ≈ 1 suggests Poisson, φ > 1 suggests Negative Binomial
        overdispersion_param = var_count / mean_count if mean_count > 0 else 0

        # Fit Poisson distribution (single parameter: λ = mean)
        poisson_lambda = mean_count

        # Fit Negative Binomial distribution
        # Using method of moments: n = mean²/(variance - mean)
        if var_count > mean_count:
            nb_n = (mean_count ** 2) / (var_count - mean_count)
            nb_p = mean_count / var_count
        else:
            # Fall back if variance <= mean (unusual for overdispersion)
            nb_n = mean_count
            nb_p = 0.5

        # Goodness of fit test (chi-square)
        # Create histogram bins
        observed_freq, bin_edges = np.histogram(counts_arr, bins=min(10, len(counts)//2 + 1))

        # Calculate expected frequencies for Poisson
        poisson_expected = []
        for i in range(len(bin_edges) - 1):
            bin_center = (bin_edges[i] + bin_edges[i+1]) / 2
            poisson_expected.append(len(counts) * stats.poisson.pmf(int(bin_center), poisson_lambda))

        # Calculate expected frequencies for Negative Binomial
        nb_expected = []
        for i in range(len(bin_edges) - 1):
            bin_center = (bin_edges[i] + bin_edges[i+1]) / 2
            nb_expected.append(len(counts) * stats.nbinom.pmf(int(bin_center), nb_n, nb_p))

        overdispersion_stats = {
            'mean': round(mean_count, 2),
            'variance': round(var_count, 2),
            'overdispersion_param': round(overdispersion_param, 3),
            'poisson_lambda': round(poisson_lambda, 2),
            'nb_n': round(nb_n, 2),
            'nb_p': round(nb_p, 3),
            'interpretation': 'Negative Binomial' if overdispersion_param > 1.5 else 'Poisson' if overdispersion_param < 1.2 else 'Mixed'
        }

    return {
        'months': months,
        'counts': counts,
        'total_events': sum(counts),
        'avg_per_month': (sum(counts) / len(counts)) if counts else 0,
        'overdispersion': overdispersion_stats
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
    # First, get all records_affected values grouped by month for median calculation
    detail_query = """
        SELECT
            strftime('%Y-%m', event_date) as month,
            CAST(records_affected AS FLOAT) as records_affected
        FROM DeduplicatedEvents
        WHERE status = 'Active'
            AND records_affected IS NOT NULL
            AND CAST(records_affected AS INTEGER) > 0
            AND CAST(records_affected AS INTEGER) <= 1000000000
            AND event_date >= ?
            AND event_date <= ?
        ORDER BY month, records_affected
    """
    detail_rows = conn.execute(detail_query, (start_date, end_date)).fetchall()

    # Group by month and calculate median
    from collections import defaultdict
    monthly_values = defaultdict(list)
    for r in detail_rows:
        if r['month']:
            monthly_values[r['month']].append(r['records_affected'])

    # Get aggregate stats per month
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
            AND CAST(records_affected AS INTEGER) <= 1000000000
            AND event_date >= ?
            AND event_date <= ?
        GROUP BY strftime('%Y-%m', event_date)
        ORDER BY month
    """
    rows = conn.execute(query, (start_date, end_date)).fetchall()
    months, averages, medians, ci, sample_sizes = [], [], [], [], []
    for r in rows:
        if not r['month']:
            continue
        month = r['month']
        months.append(month)
        avg = r['avg_records'] or 0
        averages.append(avg)
        sample_sizes.append(r['sample_size'] or 0)
        margin = avg * 0.2
        ci.append([max(0, avg - margin), avg + margin])

        # Calculate median for this month
        values = sorted(monthly_values[month])
        n = len(values)
        if n > 0:
            if n % 2 == 0:
                median = (values[n//2 - 1] + values[n//2]) / 2
            else:
                median = values[n//2]
            medians.append(median)
        else:
            medians.append(0)

    return {
        'months': months,
        'averages': averages,
        'medians': medians,
        'confidence_intervals': ci,
        'sample_sizes': sample_sizes
    }


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
    """
    Get entity type distribution based on victim_organization_industry field.
    Maps detailed industry values to broader categories using the IndustryGroupings table.
    """

    # Load industry groupings from database
    groupings_query = """
        SELECT group_name, keywords, display_order
        FROM IndustryGroupings
        WHERE group_name != 'Others'
        ORDER BY display_order
    """
    try:
        groupings = conn.execute(groupings_query).fetchall()
    except sqlite3.OperationalError:
        logger.warning("IndustryGroupings table not found, using empty groupings")
        groupings = []

    # Parse keywords from JSON and create mapping
    category_mapping = {}
    for group in groupings:
        group_name = group['group_name']
        keywords_json = group['keywords']
        keywords = json.loads(keywords_json) if keywords_json else []
        category_mapping[group_name] = keywords

    # Query for industry event counts
    query = """
        SELECT
            COALESCE(victim_organization_industry, 'Unknown') as industry,
            COUNT(DISTINCT deduplicated_event_id) as event_count
        FROM DeduplicatedEvents
        WHERE status = 'Active'
            AND event_date >= ?
            AND event_date <= ?
        GROUP BY victim_organization_industry
        ORDER BY event_count DESC
    """
    rows = conn.execute(query, (start_date, end_date)).fetchall()

    # Count events by category
    category_counts = {}
    unknown_count = 0

    for row in rows:
        industry = row['industry']
        count = row['event_count']

        if industry == 'Unknown' or not industry:
            unknown_count += count
            continue

        # Find matching category
        matched = False
        for category, keywords in category_mapping.items():
            if any(keyword.lower() in industry.lower() for keyword in keywords):
                category_counts[category] = category_counts.get(category, 0) + count
                matched = True
                break

        if not matched:
            # Add to "Others" category
            category_counts['Others'] = category_counts.get('Others', 0) + count

    # If there are unknown values, add them
    if unknown_count > 0:
        category_counts['Unknown'] = unknown_count

    # Convert to list format expected by dashboard, sorted by count
    types = [{'type': cat, 'events': count, 'entities': count}
             for cat, count in sorted(category_counts.items(), key=lambda x: x[1], reverse=True)]

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


def _severity_to_numeric(severity: str) -> int:
    """Convert severity string to numeric value."""
    mapping = {
        'Critical': 4, 'EventSeverity.CRITICAL': 4,
        'High': 3, 'EventSeverity.HIGH': 3,
        'Medium': 2, 'EventSeverity.MEDIUM': 2,
        'Low': 1, 'EventSeverity.LOW': 1,
    }
    return mapping.get(severity, 0)


def get_maximum_severity_per_month(conn: sqlite3.Connection, start_date: str, end_date: str) -> Dict[str, Any]:
    """Get maximum and average severity per month with entity details."""
    # Query for maximum severity
    query_max = """
        SELECT
            strftime('%Y-%m', de.event_date) as month,
            de.severity,
            de.title,
            e.entity_name,
            de.deduplicated_event_id
        FROM DeduplicatedEvents de
        LEFT JOIN DeduplicatedEventEntities dee ON de.deduplicated_event_id = dee.deduplicated_event_id
        LEFT JOIN EntitiesV2 e ON dee.entity_id = e.entity_id
        WHERE de.status = 'Active'
            AND de.severity IS NOT NULL
            AND de.event_date >= ?
            AND de.event_date <= ?
        ORDER BY strftime('%Y-%m', de.event_date),
            CASE de.severity
                WHEN 'Critical' THEN 1
                WHEN 'EventSeverity.CRITICAL' THEN 1
                WHEN 'High' THEN 2
                WHEN 'EventSeverity.HIGH' THEN 2
                WHEN 'Medium' THEN 3
                WHEN 'EventSeverity.MEDIUM' THEN 3
                WHEN 'Low' THEN 4
                WHEN 'EventSeverity.LOW' THEN 4
                ELSE 5
            END
    """

    # Query for average severity
    query_avg = """
        SELECT
            strftime('%Y-%m', event_date) as month,
            AVG(CASE severity
                WHEN 'Critical' THEN 4
                WHEN 'EventSeverity.CRITICAL' THEN 4
                WHEN 'High' THEN 3
                WHEN 'EventSeverity.HIGH' THEN 3
                WHEN 'Medium' THEN 2
                WHEN 'EventSeverity.MEDIUM' THEN 2
                WHEN 'Low' THEN 1
                WHEN 'EventSeverity.LOW' THEN 1
                ELSE 0
            END) as avg_severity_numeric
        FROM DeduplicatedEvents
        WHERE status = 'Active'
            AND severity IS NOT NULL
            AND event_date >= ?
            AND event_date <= ?
        GROUP BY month
        ORDER BY month
    """

    # Get maximum severity per month
    rows = conn.execute(query_max, (start_date, end_date)).fetchall()

    # Group by month and get the first (highest severity) event
    monthly_max = {}
    for row in rows:
        month = row['month']
        if not month:
            continue

        if month not in monthly_max:
            monthly_max[month] = {
                'severity': row['severity'],
                'title': row['title'],
                'entity_name': row['entity_name'] or 'Unknown Entity',
                'severity_numeric': _severity_to_numeric(row['severity'])
            }

    # Get average severity per month
    avg_rows = conn.execute(query_avg, (start_date, end_date)).fetchall()
    monthly_avg = {}
    for row in avg_rows:
        month = row['month']
        if month:
            monthly_avg[month] = round(row['avg_severity_numeric'], 2)

    months = sorted(monthly_max.keys())
    severities = [monthly_max[month]['severity'] for month in months]
    titles = [monthly_max[month]['title'] for month in months]
    entities = [monthly_max[month]['entity_name'] for month in months]
    severity_numeric = [monthly_max[month]['severity_numeric'] for month in months]
    avg_severity_numeric = [monthly_avg.get(month, 0) for month in months]

    return {
        'months': months,
        'severities': severities,
        'titles': titles,
        'entities': entities,
        'severity_numeric': severity_numeric,
        'avg_severity_numeric': avg_severity_numeric
    }


def get_median_severity_per_month(conn: sqlite3.Connection, start_date: str, end_date: str) -> Dict[str, Any]:
    """Get median severity per month."""
    query = """
        SELECT
            strftime('%Y-%m', event_date) as month,
            severity
        FROM DeduplicatedEvents
        WHERE status = 'Active'
            AND severity IS NOT NULL
            AND event_date >= ?
            AND event_date <= ?
        ORDER BY month, event_date
    """
    rows = conn.execute(query, (start_date, end_date)).fetchall()

    # Group by month and calculate median
    monthly_severities = {}
    for row in rows:
        month = row['month']
        if not month:
            continue
        
        if month not in monthly_severities:
            monthly_severities[month] = []
        
        severity_numeric = _severity_to_numeric(row['severity'])
        monthly_severities[month].append(severity_numeric)

    months = sorted(monthly_severities.keys())
    median_severities = []
    
    for month in months:
        severities = monthly_severities[month]
        if severities:
            # Calculate median
            sorted_severities = sorted(severities)
            n = len(sorted_severities)
            if n % 2 == 0:
                median = (sorted_severities[n//2 - 1] + sorted_severities[n//2]) / 2
            else:
                median = sorted_severities[n//2]
            median_severities.append(median)
        else:
            median_severities.append(0)

    return {
        'months': months,
        'median_severities': median_severities
    }


def get_maximum_records_affected_per_month(conn: sqlite3.Connection, start_date: str, end_date: str) -> Dict[str, Any]:
    """Get maximum records affected per month with event details.

    Filters out unrealistic values (> 1 billion) to exclude data quality issues.
    """
    query = """
        SELECT
            strftime('%Y-%m', de.event_date) as month,
            de.title,
            COALESCE(
                e.entity_name,
                JSON_EXTRACT(me.perplexity_enrichment_data, '$.formal_entity_name'),
                'Unknown'
            ) as entity_name,
            CAST(de.records_affected AS INTEGER) as records_affected,
            de.deduplicated_event_id
        FROM DeduplicatedEvents de
        LEFT JOIN DeduplicatedEventEntities dee ON de.deduplicated_event_id = dee.deduplicated_event_id
        LEFT JOIN EntitiesV2 e ON dee.entity_id = e.entity_id
        LEFT JOIN EnrichedEvents me ON de.master_enriched_event_id = me.enriched_event_id
        WHERE de.status = 'Active'
            AND de.records_affected IS NOT NULL
            AND CAST(de.records_affected AS INTEGER) > 0
            AND CAST(de.records_affected AS INTEGER) <= 1000000000
            AND de.event_date >= ?
            AND de.event_date <= ?
        ORDER BY de.event_date, CAST(de.records_affected AS INTEGER) DESC
    """
    rows = conn.execute(query, (start_date, end_date)).fetchall()

    # Get maximum record for each month
    monthly_max = {}
    for row in rows:
        month = row['month']
        if not month:
            continue

        if month not in monthly_max:
            monthly_max[month] = {
                'max_records': row['records_affected'],
                'title': row['title'],
                'entity_name': row['entity_name']
            }
        elif row['records_affected'] > monthly_max[month]['max_records']:
            monthly_max[month] = {
                'max_records': row['records_affected'],
                'title': row['title'],
                'entity_name': row['entity_name']
            }

    months = sorted(monthly_max.keys())
    max_records = [monthly_max[month]['max_records'] for month in months]
    titles = [monthly_max[month]['title'] or 'Unknown Event' for month in months]
    entities = [monthly_max[month]['entity_name'] for month in months]

    return {
        'months': months,
        'max_records': max_records,
        'titles': titles,
        'entities': entities
    }


def get_severity_by_industry(conn: sqlite3.Connection, start_date: str, end_date: str) -> Dict[str, Any]:
    """Get average severity by industry grouped into 12 higher-level categories."""

    # Load industry groupings from database
    groupings_query = """
        SELECT group_name, keywords, display_order
        FROM IndustryGroupings
        WHERE group_name != 'Others'
        ORDER BY display_order
    """
    try:
        groupings = conn.execute(groupings_query).fetchall()
    except sqlite3.OperationalError:
        logger.warning("IndustryGroupings table not found, using empty groupings")
        groupings = []

    # Parse keywords from JSON and create mapping
    category_mapping = {}
    for group in groupings:
        group_name = group['group_name']
        keywords_json = group['keywords']
        keywords = json.loads(keywords_json) if keywords_json else []
        category_mapping[group_name] = keywords

    # Query for severity data by raw industry
    query = """
        SELECT
            COALESCE(victim_organization_industry, 'Unknown') as industry,
            severity,
            COUNT(*) as event_count
        FROM DeduplicatedEvents
        WHERE status = 'Active'
            AND severity IS NOT NULL
            AND event_date >= ?
            AND event_date <= ?
        GROUP BY victim_organization_industry, severity
        ORDER BY victim_organization_industry
    """
    rows = conn.execute(query, (start_date, end_date)).fetchall()

    # Map raw industries to categories and calculate weighted average severity
    category_data = {}
    unknown_count = 0

    for row in rows:
        industry = row['industry']
        severity_numeric = _severity_to_numeric(row['severity'])
        count = row['event_count']

        if industry == 'Unknown' or not industry:
            unknown_count += count
            continue

        # Find matching category
        matched = False
        for category, keywords in category_mapping.items():
            if any(keyword.lower() in industry.lower() for keyword in keywords):
                if category not in category_data:
                    category_data[category] = {'total_severity': 0, 'total_count': 0}

                category_data[category]['total_severity'] += severity_numeric * count
                category_data[category]['total_count'] += count
                matched = True
                break

        if not matched:
            # Add to "Others" category
            if 'Others' not in category_data:
                category_data['Others'] = {'total_severity': 0, 'total_count': 0}
            category_data['Others']['total_severity'] += severity_numeric * count
            category_data['Others']['total_count'] += count

    # If all data is "Unknown", return empty lists to indicate no data
    if len(category_data) == 0 and unknown_count > 0:
        return {
            'industries': [],
            'avg_severities': [],
            'no_data': True
        }

    industries = []
    avg_severities = []

    # Calculate average severity for each category
    for category, data in sorted(category_data.items(), key=lambda x: x[1]['total_count'], reverse=True):
        if data['total_count'] > 0:
            avg_severity = data['total_severity'] / data['total_count']
            industries.append(category)
            avg_severities.append(avg_severity)

    return {
        'industries': industries,
        'avg_severities': avg_severities,
        'no_data': len(industries) == 0
    }


def get_severity_by_attack_type(conn: sqlite3.Connection, start_date: str, end_date: str) -> Dict[str, Any]:
    """Get average severity by attack type."""
    query = """
        SELECT
            COALESCE(event_type, 'Unknown') as attack_type,
            severity,
            COUNT(*) as event_count
        FROM DeduplicatedEvents
        WHERE status = 'Active'
            AND severity IS NOT NULL
            AND event_date >= ?
            AND event_date <= ?
        GROUP BY event_type, severity
        ORDER BY event_type
    """
    rows = conn.execute(query, (start_date, end_date)).fetchall()

    # Calculate weighted average severity by attack type
    attack_type_data = {}
    for row in rows:
        attack_type = row['attack_type']
        # Clean up enum strings
        if attack_type.startswith('CyberEventType.'):
            attack_type = attack_type.replace('CyberEventType.', '').replace('_', ' ').title()
        
        severity_numeric = _severity_to_numeric(row['severity'])
        count = row['event_count']
        
        if attack_type not in attack_type_data:
            attack_type_data[attack_type] = {'total_severity': 0, 'total_count': 0}
        
        attack_type_data[attack_type]['total_severity'] += severity_numeric * count
        attack_type_data[attack_type]['total_count'] += count

    attack_types = []
    avg_severities = []
    
    for attack_type, data in attack_type_data.items():
        if data['total_count'] > 0:
            avg_severity = data['total_severity'] / data['total_count']
            attack_types.append(attack_type)
            avg_severities.append(avg_severity)

    return {
        'attack_types': attack_types,
        'avg_severities': avg_severities
    }


def get_records_affected_by_attack_type(conn: sqlite3.Connection, start_date: str, end_date: str) -> Dict[str, Any]:
    """Get average records affected by attack type.

    Filters out unrealistic values (> 1 billion) to exclude data quality issues.
    """
    query = """
        SELECT
            COALESCE(event_type, 'Unknown') as attack_type,
            CAST(records_affected AS FLOAT) as records_affected,
            COUNT(*) as event_count,
            SUM(CAST(records_affected AS FLOAT)) as total_records
        FROM DeduplicatedEvents
        WHERE status = 'Active'
            AND records_affected IS NOT NULL
            AND CAST(records_affected AS INTEGER) > 0
            AND CAST(records_affected AS INTEGER) <= 1000000000
            AND event_date >= ?
            AND event_date <= ?
        GROUP BY event_type
        ORDER BY total_records DESC
    """
    rows = conn.execute(query, (start_date, end_date)).fetchall()

    attack_types = []
    avg_records = []

    for row in rows:
        attack_type = row['attack_type']
        # Clean up enum strings
        if attack_type.startswith('CyberEventType.'):
            attack_type = attack_type.replace('CyberEventType.', '').replace('_', ' ').title()

        if row['event_count'] > 0:
            avg = row['total_records'] / row['event_count']
            attack_types.append(attack_type)
            avg_records.append(avg)

    return {
        'attack_types': attack_types,
        'avg_records': avg_records
    }


def _validate_oaic_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and clean OAIC record, nullifying obviously incorrect values.

    Rules:
    - Sector notification counts should not exceed total_notifications
    - Attack type counts should not exceed total_notifications
    - Percentages should be between 0 and 100
    """
    total = record.get('total_notifications') or 0

    # Validate top_sectors - remove entries with counts > total_notifications
    if record.get('top_sectors') and total > 0:
        valid_sectors = []
        for sector in record['top_sectors']:
            count = sector.get('notifications') or 0
            if count <= total * 1.1:  # Allow 10% margin for rounding
                valid_sectors.append(sector)
        record['top_sectors'] = valid_sectors if valid_sectors else None

    # Validate attack type counts - should not exceed total_notifications
    attack_fields = ['phishing', 'ransomware', 'hacking', 'brute_force', 'malware', 'compromised_credentials']
    for field in attack_fields:
        value = record.get(field)
        if value is not None and total > 0 and value > total * 1.1:
            record[field] = None  # Nullify impossible values

    return record


def load_oaic_data() -> List[Dict[str, Any]]:
    """Load OAIC data from JSON files, merging data from multiple sources.

    Combines PDF-scraped data (has individuals_affected_average/median) with
    dashboard-scraped data (has individuals_affected_distribution) to get the
    most complete dataset. Validates data to filter out obviously incorrect values.
    """
    oaic_files = glob.glob('oaic_cyber_statistics_*.json')

    if not oaic_files:
        logger.warning("No OAIC data files found. Run oaic_data_scraper.py first.")
        return []

    # Load all OAIC files and merge by period
    all_records: Dict[str, Dict[str, Any]] = {}  # key: "year period" -> record

    for filepath in oaic_files:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                file_data = json.load(f)

            for record in file_data:
                year = record.get('year')
                period = record.get('period')
                if not year or not period:
                    continue

                # Validate the record
                record = _validate_oaic_record(record)
                period_key = f"{year} {period}"

                if period_key not in all_records:
                    all_records[period_key] = record
                else:
                    # Merge: keep fields with actual values
                    existing = all_records[period_key]

                    # Prefer records with individuals_affected_average/median (PDF-scraped)
                    if record.get('individuals_affected_average') and not existing.get('individuals_affected_average'):
                        existing['individuals_affected_average'] = record['individuals_affected_average']
                    if record.get('individuals_affected_median') and not existing.get('individuals_affected_median'):
                        existing['individuals_affected_median'] = record['individuals_affected_median']

                    # Prefer records with individuals_affected_distribution (dashboard-scraped)
                    if record.get('individuals_affected_distribution') and not existing.get('individuals_affected_distribution'):
                        existing['individuals_affected_distribution'] = record['individuals_affected_distribution']

                    # For top_sectors, prefer records with valid sectors over empty/null
                    if record.get('top_sectors') and not existing.get('top_sectors'):
                        existing['top_sectors'] = record['top_sectors']

                    # For attack types, prefer valid (non-null) values
                    for field in ['phishing', 'ransomware', 'hacking', 'brute_force', 'malware', 'compromised_credentials']:
                        if record.get(field) is not None and existing.get(field) is None:
                            existing[field] = record[field]

                    # For other fields, prefer non-null values
                    for key, value in record.items():
                        if key not in existing or existing[key] is None:
                            existing[key] = value

        except Exception as e:
            logger.warning("Could not load %s: %s", filepath, e)
            continue

    # Sort by year and period
    merged_data = sorted(
        all_records.values(),
        key=lambda x: (x.get('year', 0), 0 if x.get('period') == 'H1' else 1)
    )

    logger.info("Loaded and merged OAIC data from %d files, %d periods", len(oaic_files), len(merged_data))
    return merged_data


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


def prepare_oaic_comparison_data(database_data: Dict[str, Any], oaic_data: List[Dict[str, Any]], end_date: str = None) -> Dict[str, Any]:
    """Prepare data for OAIC vs Database comparison chart.

    If end_date is provided and the latest period is partial, calculates a pro-rata estimate
    for the full 6-month period.
    """
    from datetime import datetime

    # Create a mapping of OAIC data by period
    oaic_lookup = {}
    for record in oaic_data:
        year = record.get('year')
        period = record.get('period')  # H1 or H2
        total_notifications = record.get('total_notifications')

        if year and period and total_notifications:
            period_key = f"{year} {period}"
            oaic_lookup[period_key] = total_notifications

    # Align database and OAIC data (use union so OAIC-only periods still render)
    db_periods = database_data.get('periods', [])
    db_counts_map = {p: c for p, c in zip(db_periods, database_data.get('database_counts', []))}

    all_periods = sorted(set(list(db_periods) + list(oaic_lookup.keys())))
    periods = []
    database_counts = []
    oaic_counts = []

    for period in all_periods:
        periods.append(period)
        database_counts.append(db_counts_map.get(period))
        oaic_counts.append(oaic_lookup.get(period))

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

    # Calculate pro-rata estimate for partial periods
    prorata_period = None
    prorata_estimate = None
    prorata_actual = None

    if end_date and filtered_periods:
        # Get the last period
        last_period = filtered_periods[-1]
        last_count = filtered_database[-1]

        # Parse the period (e.g., "2025 H1")
        parts = last_period.split()
        if len(parts) == 2:
            year = int(parts[0])
            half = parts[1]  # H1 or H2

            # Determine the period end date
            if half == 'H1':
                period_end = datetime(year, 6, 30)
                period_start = datetime(year, 1, 1)
            else:  # H2
                period_end = datetime(year, 12, 31)
                period_start = datetime(year, 7, 1)

            # Parse the actual end date
            actual_end = datetime.strptime(end_date, '%Y-%m-%d')

            # Check if this is a partial period
            if actual_end < period_end and actual_end >= period_start:
                # Calculate months elapsed (as a fraction)
                days_elapsed = (actual_end - period_start).days + 1
                days_in_period = (period_end - period_start).days + 1
                months_elapsed = (days_elapsed / days_in_period) * 6

                # Calculate pro-rata estimate
                if months_elapsed > 0 and last_count:
                    prorata_factor = 6.0 / months_elapsed
                    prorata_estimate = round(last_count * prorata_factor)
                    prorata_actual = last_count
                    prorata_period = last_period

    return {
        'periods': filtered_periods,
        'database_counts': filtered_database,
        'oaic_counts': filtered_oaic,
        'oaic_available': len([x for x in filtered_oaic if x is not None]),
        'database_available': len([x for x in filtered_database if x is not None]),
        'prorata_period': prorata_period,
        'prorata_estimate': prorata_estimate,
        'prorata_actual': prorata_actual
    }


def prepare_oaic_cyber_incidents_data(oaic_data: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Prepare OAIC cyber incidents trends over time."""
    from collections import defaultdict

    # Group by year-period
    periods = []
    cyber_incidents = []
    total_notifications = []

    for record in sorted(oaic_data, key=lambda x: (x.get('year', 0), x.get('start_month', 0))):
        year = record.get('year')
        period = record.get('period')

        if year and period:
            period_key = f"{year} {period}"
            periods.append(period_key)

            # Get cyber incidents (prefer total over percentage calculation)
            cyber_inc = record.get('cyber_incidents_total')
            if not cyber_inc and record.get('cyber_incidents_percentage') and record.get('total_notifications'):
                cyber_inc = round((record['cyber_incidents_percentage'] / 100) * record['total_notifications'])

            cyber_incidents.append(cyber_inc)
            total_notifications.append(record.get('total_notifications'))

    return {
        'periods': periods,
        'cyber_incidents': cyber_incidents,
        'total_notifications': total_notifications
    }


def prepare_oaic_attack_types_data(oaic_data: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Prepare OAIC attack types breakdown over time."""
    periods = []
    attack_types = {
        'ransomware': [],
        'phishing': [],
        'hacking': [],
        'malware': [],
        'brute_force': [],
        'compromised_credentials': []
    }

    for record in sorted(oaic_data, key=lambda x: (x.get('year', 0), x.get('start_month', 0))):
        year = record.get('year')
        period = record.get('period')

        if year and period:
            period_key = f"{year} {period}"
            periods.append(period_key)

            for attack_type in attack_types.keys():
                attack_types[attack_type].append(record.get(attack_type))

    return {
        'periods': periods,
        'attack_types': attack_types
    }


def normalize_sector_name(sector: str) -> str:
    """Normalize sector names to canonical form for consistent aggregation."""
    if not sector:
        return sector

    # Mapping of variations to canonical names
    sector_mappings = {
        'Australian': 'Australian Government',
        'Australian government': 'Australian Government',
        'Government': 'Australian Government',
        'Finance (incl. superannuation)': 'Finance',
        'Finance (including superannuation)': 'Finance',
        'Health service providers': 'Health',
        'Health services': 'Health',
        'Healthcare': 'Health',
    }

    # Check for exact match first
    if sector in sector_mappings:
        return sector_mappings[sector]

    # Check for case-insensitive match
    sector_lower = sector.lower()
    for key, value in sector_mappings.items():
        if key.lower() == sector_lower:
            return value

    return sector


def prepare_oaic_sectors_data(oaic_data: List[Dict[str, Any]], db_path: str = 'instance/cyber_events.db') -> Dict[str, Any]:
    """Prepare OAIC top sectors affected with database comparison (aggregated 2019-2024)."""
    from collections import defaultdict
    import sqlite3

    sector_totals = defaultdict(int)

    for record in oaic_data:
        top_sectors = record.get('top_sectors') or []
        for sector_entry in top_sectors:
            sector = sector_entry.get('sector')
            notifications = sector_entry.get('notifications', 0)
            if sector and notifications:
                # Normalize sector name before aggregating
                normalized_sector = normalize_sector_name(sector)
                sector_totals[normalized_sector] += notifications

    # Sort by total notifications
    sorted_sectors = sorted(sector_totals.items(), key=lambda x: x[1], reverse=True)

    # Take up to top 10 (may be fewer if less data available)
    top_sectors_list = sorted_sectors[:10]
    sector_count = len(top_sectors_list)

    # Get database counts for 2019-2024
    db_counts = {}
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT victim_organization_industry, COUNT(*) as count
            FROM DeduplicatedEvents
            WHERE event_date >= '2019-01-01' AND event_date <= '2024-12-31'
            AND victim_organization_industry IS NOT NULL
            GROUP BY victim_organization_industry
        """)
        for row in cursor.fetchall():
            db_counts[row[0]] = row[1]
        conn.close()
    except Exception as e:
        logger.warning("Could not query database for sector counts: %s", e)

    # Map database industry names to OAIC sector names (using normalized names)
    industry_mapping = {
        'Healthcare': 'Health',
        'Government': 'Australian Government',
        'Finance': 'Finance',
        'Retail': 'Retail',
        'Education': 'Education'
    }

    # Build comparison data
    sectors = [s[0] for s in top_sectors_list]
    oaic_counts = [s[1] for s in top_sectors_list]
    database_counts = []
    ratios = []

    for sector, oaic_count in zip(sectors, oaic_counts):
        # Find matching database count
        db_count = 0
        for db_industry, oaic_sector in industry_mapping.items():
            if sector == oaic_sector and db_industry in db_counts:
                db_count = db_counts[db_industry]
                break
        database_counts.append(db_count)
        # Calculate ratio (database / OAIC), handle division by zero
        ratio = round(db_count / oaic_count, 2) if oaic_count > 0 else 0
        ratios.append(ratio)

    return {
        'sectors': sectors,
        'oaic_counts': oaic_counts,
        'database_counts': database_counts,
        'ratios': ratios,
        'sector_count': sector_count
    }


def calculate_stats_from_distribution(distribution: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
    """
    Calculate average and median from individuals_affected_distribution data.

    Returns:
        Tuple of (average, median) or (None, None) if calculation not possible
    """
    if not distribution:
        return None, None

    # Define midpoints for each range
    range_midpoints = {
        '1': 1,
        '2-10': 6,
        '11-100': 55,
        '101-1,000': 550,
        '101-1000': 550,
        '1,001-5,000': 3000,
        '1001-5000': 3000,
        '5,001-10,000': 7500,
        '5001-10000': 7500,
        '10,001-50,000': 30000,
        '10001-50000': 30000,
        '50,001-100,000': 75000,
        '50001-100000': 75000,
        '100,001-250,000': 175000,
        '100001-250000': 175000,
        '250,001-500,000': 375000,
        '250001-500000': 375000,
        '500,001-1,000,000': 750000,
        '500001-1000000': 750000,
        '1,000,001-5,000,000': 3000000,
        '1000001-5000000': 3000000,
        '5,000,001+': 7500000,
        '5000001+': 7500000,
    }

    # Build a list of estimated values
    values = []
    for item in distribution:
        range_str = item.get('range', '')
        count = item.get('count', 0)

        if range_str.lower() == 'unknown' or count == 0:
            continue

        # Normalize range string (remove spaces, handle variations)
        range_normalized = range_str.replace(' ', '').replace(',', '')

        midpoint = range_midpoints.get(range_str) or range_midpoints.get(range_normalized)

        if midpoint:
            values.extend([midpoint] * count)

    if not values:
        return None, None

    # Calculate average and median
    avg = sum(values) / len(values)
    sorted_values = sorted(values)
    n = len(sorted_values)
    if n % 2 == 0:
        median = (sorted_values[n//2 - 1] + sorted_values[n//2]) / 2
    else:
        median = sorted_values[n//2]

    return avg, median


def prepare_oaic_individuals_affected_data(oaic_data: List[Dict[str, Any]], db_path: str = 'instance/cyber_events.db') -> Dict[str, Any]:
    """Prepare OAIC individuals affected trends with database comparison."""
    import sqlite3

    periods = []
    averages = []
    medians = []
    db_averages = []

    # Get database averages by half-year period
    db_data = {}
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                strftime('%Y', event_date) as year,
                CASE WHEN CAST(strftime('%m', event_date) AS INTEGER) <= 6 THEN 'H1' ELSE 'H2' END as half,
                AVG(records_affected) as avg_records
            FROM DeduplicatedEvents
            WHERE event_date >= '2019-01-01' AND event_date <= '2025-12-31'
            AND records_affected IS NOT NULL
            GROUP BY year, half
            ORDER BY year, half
        """)
        for row in cursor.fetchall():
            period_key = f"{row[0]} {row[1]}"
            db_data[period_key] = row[2]
        conn.close()
    except Exception as e:
        logger.warning("Could not query database for records affected: %s", e)

    for record in sorted(oaic_data, key=lambda x: (x.get('year', 0), x.get('start_month', 0))):
        year = record.get('year')
        period = record.get('period')

        if year and period:
            period_key = f"{year} {period}"
            periods.append(period_key)

            # Get average and median, or calculate from distribution if not available
            avg = record.get('individuals_affected_average')
            med = record.get('individuals_affected_median')

            # If missing, try to calculate from distribution data
            if (avg is None or med is None) and record.get('individuals_affected_distribution'):
                calc_avg, calc_med = calculate_stats_from_distribution(record.get('individuals_affected_distribution'))
                if avg is None:
                    avg = calc_avg
                if med is None:
                    med = calc_med

            averages.append(avg)
            medians.append(med)

            # Add database average for this period (or None if not available)
            db_averages.append(db_data.get(period_key))

    return {
        'periods': periods,
        'averages': averages,
        'medians': medians,
        'db_averages': db_averages
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
    - Includes overdispersion analysis from monthly_counts if available.
    """
    counts = [int(x) for x in monthly_counts.get('counts', []) if x is not None]
    n = len(counts)
    if n == 0:
        return {'bins': [], 'frequencies': [], 'mean': 0, 'variance': 0, 'k_estimate': None, 'model': 'No data', 'overdispersion': {}}

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

    # Include overdispersion statistics if available from get_monthly_event_counts
    overdispersion = monthly_counts.get('overdispersion', {})

    return {
        'bins': bins,
        'frequencies': freqs,
        'mean': mean,
        'variance': variance,
        'k_estimate': k_estimate,
        'model': model,
        'overdispersion': overdispersion,
    }


def get_asd_risk_matrix(conn: sqlite3.Connection, year: Optional[int] = None) -> Dict[str, Any]:
    """Aggregate ASD risk classifications into a matrix by impact type and stakeholder groups.

    NOTE: ASDRiskClassifications.deduplicated_event_id references DeduplicatedEvents.deduplicated_event_id
    (NOT EnrichedEvents). Always join to DeduplicatedEvents for date filtering.
    """
    stakeholder_groups = list(ASD_STAKEHOLDER_GROUPS.keys())

    def empty_matrix() -> Dict[str, Any]:
        return {
            'impact_types': ASD_VALID_IMPACT_TYPES,
            'stakeholder_groups': stakeholder_groups,
            'matrix': [
                {'impact_type': impact, 'counts': {group: 0 for group in stakeholder_groups}}
                for impact in ASD_VALID_IMPACT_TYPES
            ],
            'total_classifications': 0,
            'max_value': 0,
            'year': year,
        }

    try:
        cursor = conn.cursor()
        params: List[Any] = []
        # IMPORTANT: Join to DeduplicatedEvents (not EnrichedEvents) - that's where the FK points
        base_sql = """
            SELECT arc.impact_type, arc.primary_stakeholder_category, COUNT(*) as count
            FROM ASDRiskClassifications arc
            LEFT JOIN DeduplicatedEvents de ON arc.deduplicated_event_id = de.deduplicated_event_id
        """
        where_clauses = []

        if year:
            where_clauses.append("de.event_date IS NOT NULL")
            where_clauses.append("CAST(strftime('%Y', de.event_date) AS INTEGER) = ?")
            params.append(year)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        query = f"{base_sql} {where_sql} GROUP BY arc.impact_type, arc.primary_stakeholder_category"
        rows = cursor.execute(query, params).fetchall()
    except Exception as e:
        logger.error("Error in get_asd_risk_matrix: %s", e)
        return empty_matrix()

    if not rows:
        return empty_matrix()

    valid_stakeholders = {cat for cats in ASD_STAKEHOLDER_GROUPS.values() for cat in cats}
    count_map: Dict[tuple, int] = {}
    for row in rows:
        impact = row['impact_type']
        stakeholder = row['primary_stakeholder_category']
        count = int(row['count'])
        if impact not in ASD_VALID_IMPACT_TYPES or stakeholder not in valid_stakeholders:
            continue
        count_map[(impact, stakeholder)] = count

    matrix = []
    max_value = 0
    total_classifications = 0

    for impact in ASD_VALID_IMPACT_TYPES:
        row_counts: Dict[str, int] = {}
        for group_name, categories in ASD_STAKEHOLDER_GROUPS.items():
            group_total = sum(count_map.get((impact, category), 0) for category in categories)
            row_counts[group_name] = group_total
            max_value = max(max_value, group_total)
            total_classifications += group_total
        matrix.append({'impact_type': impact, 'counts': row_counts})

    return {
        'impact_types': ASD_VALID_IMPACT_TYPES,
        'stakeholder_groups': stakeholder_groups,
        'matrix': matrix,
        'total_classifications': total_classifications,
        'max_value': max_value,
        'year': year,
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
    mspm = json.dumps(data['max_severity_per_month'])
    medspm = json.dumps(data['median_severity_per_month'])
    mrpm = json.dumps(data['max_records_per_month'])
    sbi = json.dumps(data['severity_by_industry'])
    sbat = json.dumps(data['severity_by_attack_type'])
    rbat = json.dumps(data['records_by_attack_type'])
    mcs = json.dumps(data['monthly_counts_stats'])
    etc = json.dumps(data['event_type_correlation'])
    oaic_comp = json.dumps(data.get('oaic_comparison', {'periods': [], 'database_counts': [], 'oaic_counts': []}))
    oaic_ci = json.dumps(data.get('oaic_cyber_incidents', {'periods': [], 'cyber_incidents': [], 'total_notifications': []}))
    oaic_at = json.dumps(data.get('oaic_attack_types', {'periods': [], 'attack_types': {}}))
    oaic_sec = json.dumps(data.get('oaic_sectors', {'sectors': [], 'oaic_counts': [], 'database_counts': []}))
    oaic_ind_aff = json.dumps(data.get('oaic_individuals_affected', {'periods': [], 'averages': [], 'medians': [], 'db_averages': []}))
    asd_all = json.dumps(data.get('asd_risk_all', {}))
    asd_current = json.dumps(data.get('asd_risk_current', {}))
    asd_previous = json.dumps(data.get('asd_risk_previous', {}))

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
    .risk-matrix table { font-size: 0.9rem; }
    .risk-matrix .impact-label { width: 260px; text-align: left; font-weight: 600; }
    .risk-matrix .group-header { white-space: normal; }
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
          <div class="chart-title">Severity per Month</div>
          <canvas id="maxSeverityChart"></canvas>
        </div>
      </div>
      <div class="col-lg-6 col-md-12">
        <div class="chart-container">
          <div class="chart-title">Maximum Records Affected Per Month</div>
          <canvas id="maxRecordsChart"></canvas>
        </div>
      </div>
      <div class="col-lg-6 col-md-12">
        <div class="chart-container">
          <div class="chart-title">Average Severity by Industry</div>
          <canvas id="severityByIndustryChart"></canvas>
        </div>
      </div>
      <div class="col-lg-6 col-md-12">
        <div class="chart-container">
          <div class="chart-title">Average Severity by Attack Type</div>
          <canvas id="severityByAttackTypeChart"></canvas>
        </div>
      </div>
      <div class="col-lg-6 col-md-12">
        <div class="chart-container">
          <div class="chart-title">Average Records Affected by Attack Type</div>
          <canvas id="recordsByAttackTypeChart"></canvas>
        </div>
      </div>
      <div class="col-lg-6 col-md-12">
        <div class="chart-container" style="height: 440px; overflow: hidden;">
          <div class="chart-title">Histogram of Monthly Unique Event Counts</div>
          <div id="monthlyCountsSubtitle" class="chart-subtitle" style="font-size: 0.95rem; color: #6b7280; margin-top: -8px; margin-bottom: 8px;"></div>
          <div style="height: 300px; position: relative;">
            <canvas id="monthlyCountsHistChart"></canvas>
          </div>
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
      <div class="col-lg-6 col-md-12">
        <div class="chart-container">
          <div class="chart-title">OAIC Cyber Incidents vs Total Notifications</div>
          <canvas id="oaicCyberIncidentsChart"></canvas>
        </div>
      </div>
      <div class="col-lg-6 col-md-12">
        <div class="chart-container">
          <div class="chart-title">OAIC Attack Types Over Time</div>
          <canvas id="oaicAttackTypesChart"></canvas>
        </div>
      </div>
      <div class="col-lg-6 col-md-12">
        <div class="chart-container">
          <div class="chart-title" id="oaicSectorsTitle">OAIC Top Affected Sectors (2020-2025)</div>
          <canvas id="oaicSectorsChart"></canvas>
        </div>
      </div>
      <div class="col-lg-6 col-md-12">
        <div class="chart-container">
          <div class="chart-title">Database/OAIC Ratio by Sector</div>
          <canvas id="oaicSectorRatioChart"></canvas>
        </div>
      </div>
      <div class="col-lg-12 col-md-12">
        <div class="chart-container">
          <div class="chart-title">OAIC Individuals Affected Trends</div>
          <canvas id="oaicIndividualsAffectedChart"></canvas>
        </div>
      </div>
      <div class="col-lg-12 col-md-12">
        <div class="chart-container risk-matrix" style="height: auto;">
          <div class="chart-title d-flex justify-content-between align-items-center">
            <span>ASD Risk Matrix (All Years)</span>
            <span class="badge bg-secondary">Classifications: <span id="asdAllTotal">0</span></span>
          </div>
          <div id="asdRiskMatrixAll"></div>
        </div>
      </div>
      <div class="col-lg-12 col-md-12">
        <div class="chart-container risk-matrix" style="height: auto;">
          <div class="chart-title d-flex justify-content-between align-items-center">
            <span>ASD Risk Matrix (Current Year)</span>
            <span class="badge bg-secondary">Classifications: <span id="asdCurrentTotal">0</span></span>
          </div>
          <div id="asdRiskMatrixCurrent"></div>
        </div>
      </div>
      <div class="col-lg-12 col-md-12">
        <div class="chart-container risk-matrix" style="height: auto;">
          <div class="chart-title d-flex justify-content-between align-items-center">
            <span>ASD Risk Matrix (Previous Year)</span>
            <span class="badge bg-secondary">Classifications: <span id="asdPreviousTotal">0</span></span>
          </div>
          <div id="asdRiskMatrixPrevious"></div>
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
    const maxRecordsPerMonth = __MRP__;
    const recordsByAttackType = __RBAT__;
    const monthlyCountsStats = __MCS__;
    const eventTypeCorrelation = __ETC__;
    const oaicComparison = __OAIC_COMP__;
    const oaicCyberIncidents = __OAIC_CI__;
    const oaicAttackTypes = __OAIC_AT__;
    const oaicSectors = __OAIC_SEC__;
    const oaicIndividualsAffected = __OAIC_IND_AFF__;
    const asdRiskAll = __ASD_ALL__;
    const asdRiskCurrent = __ASD_CURR__;
    const asdRiskPrevious = __ASD_PREV__;

    const colors = {
      primary: '#2563eb',
      secondary: '#10b981',
      success: '#10b981',
      warning: '#f59e0b',
      danger: '#ef4444',
      info: '#06b6d4',
      severity: {
        'Critical': '#dc2626', 'High': '#ea580c', 'Medium': '#ca8a04', 'Low': '#16a34a', 'Unknown': '#6b7280'
      }
    };

    function renderRiskMatrix(targetId, matrixData, totalId) {
      const container = document.getElementById(targetId);
      const totalElement = totalId ? document.getElementById(totalId) : null;
      if (!container) return;

      if (!matrixData || !Array.isArray(matrixData.matrix) || matrixData.matrix.length === 0) {
        container.innerHTML = '<p class="text-muted mb-0">No ASD risk classifications available.</p>';
        if (totalElement) totalElement.textContent = '0';
        return;
      }

      const groups = matrixData.stakeholder_groups || [];
      const matrixRows = matrixData.matrix;
      const maxValue = matrixData.max_value || 0;

      const colorForValue = (value) => {
        if (!value || !maxValue) return '';
        const opacity = 0.18 + 0.72 * (value / maxValue);
        const textColor = opacity > 0.45 ? '#ffffff' : '#111827';
        return `background-color: rgba(37, 99, 235, ${opacity.toFixed(3)}); color: ${textColor};`;
      };

      let html = '<div class="table-responsive"><table class="table table-sm table-bordered align-middle text-center mb-1">';
      html += '<thead><tr><th class="text-start">Impact Type</th>';
      groups.forEach(group => { html += `<th class="group-header">${group}</th>`; });
      html += '</tr></thead><tbody>';

      matrixRows.forEach(row => {
        html += `<tr><th scope="row" class="text-start impact-label">${row.impact_type}</th>`;
        groups.forEach(group => {
          const value = (row.counts && row.counts[group]) ? row.counts[group] : 0;
          const display = value > 0 ? value : '';
          const style = colorForValue(value);
          html += `<td style="${style}">${display}</td>`;
        });
        html += '</tr>';
      });

      html += '</tbody></table></div>';
      container.innerHTML = html;

      if (totalElement) {
        totalElement.textContent = (matrixData.total_classifications || 0).toLocaleString();
      }
    }

    renderRiskMatrix('asdRiskMatrixAll', asdRiskAll, 'asdAllTotal');
    renderRiskMatrix('asdRiskMatrixCurrent', asdRiskCurrent, 'asdCurrentTotal');
    renderRiskMatrix('asdRiskMatrixPrevious', asdRiskPrevious, 'asdPreviousTotal');

    // 1) Monthly Trends (line) with trend line
    (function() {
      const counts = monthlyCounts.counts;
      const n = counts.length;

      // Calculate linear regression for trend line
      let sumX = 0, sumY = 0, sumXY = 0, sumXX = 0;
      for (let i = 0; i < n; i++) {
        sumX += i;
        sumY += counts[i];
        sumXY += i * counts[i];
        sumXX += i * i;
      }

      const slope = (n * sumXY - sumX * sumY) / (n * sumXX - sumX * sumX);
      const intercept = (sumY - slope * sumX) / n;

      // Generate trend line data
      const trendLine = [];
      for (let i = 0; i < n; i++) {
        trendLine.push(slope * i + intercept);
      }

      new Chart(document.getElementById('monthlyTrendsChart').getContext('2d'), {
        type: 'line',
        data: {
          labels: monthlyCounts.months,
          datasets: [
            {
              label: 'Unique Events',
              data: counts,
              borderColor: colors.primary,
              backgroundColor: '#2563eb20',
              fill: true,
              tension: 0.4,
              order: 2
            },
            {
              label: 'Trend',
              data: trendLine,
              borderColor: '#dc2626',
              backgroundColor: 'transparent',
              borderWidth: 2,
              borderDash: [5, 5],
              fill: false,
              tension: 0,
              pointRadius: 0,
              order: 1
            }
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: {
              display: true,
              position: 'top'
            }
          },
          scales: {
            y: { beginAtZero: true },
            x: { title: { display: true, text: 'Month' }}
          }
        }
      });
    })();

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

    // 3) Records Affected (line, log scale) with Average and Median
    new Chart(document.getElementById('recordsAffectedChart').getContext('2d'), {
      type: 'line',
      data: {
        labels: recordsAffected.months,
        datasets: [
          {
            label: 'Average Records Affected',
            data: recordsAffected.averages,
            borderColor: colors.secondary,
            backgroundColor: '#10b98120',
            fill: false,
            tension: 0.4,
            borderWidth: 2
          },
          {
            label: 'Median Records Affected',
            data: recordsAffected.medians,
            borderColor: colors.primary,
            backgroundColor: '#2563eb20',
            fill: false,
            tension: 0.4,
            borderWidth: 2,
            borderDash: [5, 5]
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            display: true,
            position: 'top'
          }
        },
        scales: {
          y: {
            type: 'logarithmic',
            beginAtZero: true,
            title: { display: true, text: 'Records (log)' }
          },
          x: {
            title: { display: true, text: 'Month' }
          }
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
      const od = s.overdispersion || {};
      const statsEl = document.getElementById('monthlyCountsStats');
      const subtitleEl = document.getElementById('monthlyCountsSubtitle');

      // Calculate and display overdispersion percentage in subtitle
      const mean = od.mean || s.mean;
      const variance = od.variance || s.variance;
      if (mean && variance && mean > 0) {
        const overdispersionPct = ((variance - mean) / mean * 100).toFixed(1);
        if (variance > mean) {
          subtitleEl.textContent = `Overdispersion: +${overdispersionPct}% vs Poisson`;
        } else if (variance < mean) {
          subtitleEl.textContent = `Underdispersion: ${overdispersionPct}% vs Poisson`;
        } else {
          subtitleEl.textContent = `Dispersion: 0% vs Poisson (perfect match)`;
        }
      }

      // Create formatted display with overdispersion analysis
      if (od.overdispersion_param) {
        const phi = od.overdispersion_param;
        const interpretation = od.interpretation || s.model;

        statsEl.innerHTML = `
          <div style="font-weight: 600; margin-bottom: 4px;">
            Overdispersion Analysis: φ = ${phi} (${interpretation})
          </div>
          <div style="font-size: 0.85rem;">
            Mean: ${od.mean} • Variance: ${od.variance} •
            ${phi > 1.5 ? `Negative Binomial (n=${od.nb_n}, p=${od.nb_p})` :
              phi < 1.2 ? `Poisson (λ=${od.poisson_lambda})` :
              'Mixed distribution'}
          </div>
        `;
      } else {
        // Fallback to old format if overdispersion not available
        const lines = [];
        lines.push(`Mean: ${s.mean?.toFixed ? s.mean.toFixed(2) : s.mean}`);
        lines.push(`Variance: ${s.variance?.toFixed ? s.variance.toFixed(2) : s.variance}`);
        if (s.k_estimate) lines.push(`NB dispersion k (est.): ${s.k_estimate.toFixed ? s.k_estimate.toFixed(2) : s.k_estimate}`);
        lines.push(`Model: ${s.model}`);
        statsEl.textContent = lines.join(' • ');
      }
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

      // Add pro-rata estimate if available
      if (oaic.prorata_estimate && oaic.prorata_period) {
        // Create array with null values except for the last period
        const prorataData = oaic.periods.map((p, idx) =>
          (p === oaic.prorata_period && idx === oaic.periods.length - 1) ? oaic.prorata_estimate : null
        );

        datasets.push({
          label: 'Estimated Full Period (Pro-rata)',
          data: prorataData,
          borderColor: colors.secondary,
          backgroundColor: colors.secondary,
          fill: false,
          tension: 0,
          pointRadius: 8,
          pointStyle: 'triangle',
          showLine: false  // Don't draw lines between points
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

      // Add pro-rata estimate information if available
      if (oaic.prorata_estimate && oaic.prorata_period) {
        stats.push(`${oaic.prorata_period} (partial): ${oaic.prorata_actual} → Est. ${oaic.prorata_estimate}`);
      }

      document.getElementById('oaicComparisonStats').textContent = stats.join(' • ');
    })();

    // 10) OAIC Cyber Incidents Chart
    (function(){
      const data = oaicCyberIncidents;
      if (!data.periods || data.periods.length === 0) {
        document.getElementById('oaicCyberIncidentsChart').parentElement.innerHTML =
          '<div class="chart-title">OAIC Cyber Incidents vs Total Notifications</div>' +
          '<div class="text-center text-muted mt-5"><p>No OAIC data available.</p></div>';
        return;
      }

      new Chart(document.getElementById('oaicCyberIncidentsChart').getContext('2d'), {
        type: 'line',
        data: {
          labels: data.periods,
          datasets: [
            {
              label: 'Cyber Incidents',
              data: data.cyber_incidents,
              borderColor: colors.danger,
              backgroundColor: colors.danger + '20',
              fill: true,
              tension: 0.3
            },
            {
              label: 'Total Notifications',
              data: data.total_notifications,
              borderColor: colors.primary,
              backgroundColor: colors.primary + '20',
              fill: true,
              tension: 0.3
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: {
            mode: 'index',
            intersect: false
          },
          scales: {
            y: {
              type: 'linear',
              position: 'left',
              beginAtZero: true,
              title: {
                display: true,
                text: 'Count'
              }
            }
          }
        }
      });
    })();

    // 11) OAIC Attack Types Chart
    (function(){
      const data = oaicAttackTypes;
      if (!data.periods || data.periods.length === 0) {
        document.getElementById('oaicAttackTypesChart').parentElement.innerHTML =
          '<div class="chart-title">OAIC Attack Types Over Time</div>' +
          '<div class="text-center text-muted mt-5"><p>No OAIC data available.</p></div>';
        return;
      }

      const datasets = [];
      const attackColors = {
        'ransomware': '#dc2626',
        'phishing': '#ea580c',
        'hacking': '#ca8a04',
        'malware': '#16a34a',
        'brute_force': '#0891b2',
        'compromised_credentials': '#7c3aed'
      };

      for (const [attackType, values] of Object.entries(data.attack_types)) {
        datasets.push({
          label: attackType.replace(/_/g, ' ').replace(/\\b\\w/g, l => l.toUpperCase()),
          data: values,
          borderColor: attackColors[attackType] || colors.primary,
          backgroundColor: (attackColors[attackType] || colors.primary) + '20',
          fill: false,
          tension: 0.3
        });
      }

      new Chart(document.getElementById('oaicAttackTypesChart').getContext('2d'), {
        type: 'line',
        data: {
          labels: data.periods,
          datasets: datasets
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: {
            mode: 'index',
            intersect: false
          },
          plugins: {
            legend: {
              position: 'top'
            }
          }
        }
      });
    })();

    // 12) OAIC Top Sectors Chart
    (function(){
      const data = oaicSectors;
      if (!data.sectors || data.sectors.length === 0) {
        document.getElementById('oaicSectorsChart').parentElement.innerHTML =
          '<div class="chart-title">OAIC Top Affected Sectors (2020-2025)</div>' +
          '<div class="text-center text-muted mt-5"><p>No OAIC data available.</p></div>';
        return;
      }

      // Update title based on actual sector count
      const sectorCount = data.sector_count || data.sectors.length;
      document.getElementById('oaicSectorsTitle').textContent =
        `OAIC Top ${sectorCount} Affected Sectors (2020-2025)`;

      new Chart(document.getElementById('oaicSectorsChart').getContext('2d'), {
        type: 'bar',
        data: {
          labels: data.sectors,
          datasets: [
            {
              label: 'OAIC Official Count',
              data: data.oaic_counts,
              backgroundColor: colors.primary,
              borderColor: colors.primary,
              borderWidth: 1
            },
            {
              label: 'Database Count',
              data: data.database_counts,
              backgroundColor: colors.success,
              borderColor: colors.success,
              borderWidth: 1
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          indexAxis: 'y',
          plugins: {
            legend: {
              display: true,
              position: 'top'
            }
          },
          scales: {
            x: {
              title: {
                display: true,
                text: 'Total Notifications (2020-2025)'
              }
            }
          }
        }
      });
    })();

    // 12b) OAIC Sector Ratio Chart (Database/OAIC)
    (function(){
      const data = oaicSectors;
      if (!data.sectors || data.sectors.length === 0 || !data.ratios) {
        document.getElementById('oaicSectorRatioChart').parentElement.innerHTML =
          '<div class="chart-title">Database/OAIC Ratio by Sector</div>' +
          '<div class="text-center text-muted mt-5"><p>No ratio data available.</p></div>';
        return;
      }

      // Convert ratios to percentages and color bars (green if >= 100%, orange if < 100%)
      const ratiosPct = data.ratios.map(r => Math.round(r * 100));
      const barColors = ratiosPct.map(r => r >= 100 ? colors.success : colors.warning);

      new Chart(document.getElementById('oaicSectorRatioChart').getContext('2d'), {
        type: 'bar',
        data: {
          labels: data.sectors,
          datasets: [
            {
              label: 'Database/OAIC %',
              data: ratiosPct,
              backgroundColor: barColors,
              borderColor: barColors,
              borderWidth: 1
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          indexAxis: 'y',
          plugins: {
            legend: {
              display: false
            },
            tooltip: {
              callbacks: {
                label: function(context) {
                  const pct = context.raw;
                  const idx = context.dataIndex;
                  const dbCount = data.database_counts[idx];
                  const oaicCount = data.oaic_counts[idx];
                  return [
                    `Coverage: ${pct}%`,
                    `Database: ${dbCount}`,
                    `OAIC: ${oaicCount}`
                  ];
                }
              }
            },
            annotation: {
              annotations: {
                line1: {
                  type: 'line',
                  xMin: 100,
                  xMax: 100,
                  borderColor: '#ef4444',
                  borderWidth: 2,
                  borderDash: [5, 5],
                  label: {
                    display: true,
                    content: '100%',
                    position: 'end'
                  }
                }
              }
            }
          },
          scales: {
            x: {
              title: {
                display: true,
                text: 'Database Coverage of OAIC Count (%)'
              },
              beginAtZero: true,
              ticks: {
                callback: function(value) {
                  return value + '%';
                }
              }
            }
          }
        }
      });
    })();

    // 13) OAIC Individuals Affected Chart
    (function(){
      const data = oaicIndividualsAffected;
      if (!data.periods || data.periods.length === 0) {
        document.getElementById('oaicIndividualsAffectedChart').parentElement.innerHTML =
          '<div class="chart-title">OAIC Individuals Affected Trends</div>' +
          '<div class="text-center text-muted mt-5"><p>No OAIC data available.</p></div>';
        return;
      }

      new Chart(document.getElementById('oaicIndividualsAffectedChart').getContext('2d'), {
        type: 'line',
        data: {
          labels: data.periods,
          datasets: [
            {
              label: 'OAIC Average Individuals Affected',
              data: data.averages,
              borderColor: colors.danger,
              backgroundColor: colors.danger + '20',
              fill: true,
              tension: 0.3
            },
            {
              label: 'OAIC Median Individuals Affected',
              data: data.medians,
              borderColor: colors.primary,
              backgroundColor: colors.primary + '20',
              fill: true,
              tension: 0.3
            },
            {
              label: 'Database Average Records Affected',
              data: data.db_averages,
              borderColor: colors.success,
              backgroundColor: colors.success + '20',
              fill: true,
              tension: 0.3
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: {
            mode: 'index',
            intersect: false
          },
          plugins: {
            legend: {
              position: 'top'
            },
            tooltip: {
              callbacks: {
                label: function(context) {
                  let label = context.dataset.label || '';
                  if (label) {
                    label += ': ';
                  }
                  if (context.parsed.y !== null) {
                    label += new Intl.NumberFormat('en-US').format(context.parsed.y);
                  }
                  return label;
                }
              }
            }
          },
          scales: {
            y: {
              type: 'logarithmic',
              title: {
                display: true,
                text: 'Number of Individuals (log scale)'
              },
              ticks: {
                callback: function(value) {
                  return new Intl.NumberFormat('en-US', { notation: 'compact' }).format(value);
                }
              }
            }
          }
        }
      });
    })();

    // 14) Event Type Correlation Matrix
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
      table.style.tableLayout = 'fixed';
      
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
        th.style.padding = '6px 8px';
        th.style.backgroundColor = '#f8f9fa';
        th.style.fontSize = '11px';
        th.style.textAlign = 'center';
        th.style.whiteSpace = 'normal';
        th.style.wordBreak = 'break-word';
        th.style.lineHeight = '1.2';
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

    // Maximum Severity Per Month Chart
    (() => {
      const maxSeverityData = __MSPM__;
      new Chart(document.getElementById('maxSeverityChart').getContext('2d'), {
        type: 'line',
        data: {
          labels: maxSeverityData.months,
          datasets: [{
            label: 'Maximum Severity',
            data: maxSeverityData.severity_numeric,
            borderColor: '#dc2626',
            backgroundColor: '#dc262620',
            fill: false,
            tension: 0.4,
            pointRadius: 6,
            pointHoverRadius: 8,
            borderWidth: 3
          }, {
            label: 'Average Severity',
            data: maxSeverityData.avg_severity_numeric,
            borderColor: '#f59e0b',
            backgroundColor: '#f59e0b20',
            fill: false,
            tension: 0.4,
            pointRadius: 4,
            pointHoverRadius: 6,
            borderWidth: 2,
            borderDash: [5, 5]
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: true, position: 'top' },
            tooltip: {
              callbacks: {
                title: function(tooltipItems) {
                  const index = tooltipItems[0].dataIndex;
                  return maxSeverityData.months[index];
                },
                label: function(tooltipItem) {
                  const index = tooltipItem.dataIndex;
                  const datasetLabel = tooltipItem.dataset.label;
                  if (datasetLabel === 'Maximum Severity') {
                    return `Maximum: ${maxSeverityData.severities[index]}`;
                  } else {
                    const avgValue = maxSeverityData.avg_severity_numeric[index];
                    const severityLabels = ['Unknown', 'Low', 'Medium', 'High', 'Critical'];
                    const nearestLabel = severityLabels[Math.round(avgValue)] || 'Unknown';
                    return `Average: ${nearestLabel} (${avgValue.toFixed(2)})`;
                  }
                }
              }
            }
          },
          scales: {
            y: {
              beginAtZero: true,
              max: 4,
              ticks: {
                callback: function(value) {
                  const severityMap = {0: 'Unknown', 1: 'Low', 2: 'Medium', 3: 'High', 4: 'Critical'};
                  return severityMap[value] || value;
                }
              },
              title: { display: true, text: 'Severity Level' }
            },
            x: { title: { display: true, text: 'Month' } }
          }
        }
      });
    })();

    // 12) Maximum Records Affected Per Month
    (function() {
      const maxRecordsData = maxRecordsPerMonth;
      new Chart(document.getElementById('maxRecordsChart').getContext('2d'), {
        type: 'line',
        data: {
          labels: maxRecordsData.months,
          datasets: [{
            label: 'Maximum Records Affected',
            data: maxRecordsData.max_records,
            borderColor: '#dc2626',
            backgroundColor: '#dc262620',
            fill: false,
            tension: 0.4,
            pointRadius: 6,
            pointHoverRadius: 8
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                title: function(tooltipItems) {
                  const index = tooltipItems[0].dataIndex;
                  return maxRecordsData.titles[index] || 'Unknown Event';
                },
                label: function(tooltipItem) {
                  const index = tooltipItem.dataIndex;
                  const records = maxRecordsData.max_records[index];
                  return [
                    `Entity: ${maxRecordsData.entities[index]}`,
                    `Records: ${records.toLocaleString()}`
                  ];
                }
              }
            }
          },
          scales: {
            y: {
              type: 'logarithmic',
              beginAtZero: true,
              title: { display: true, text: 'Records Affected (log scale)' }
            },
            x: {
              title: { display: true, text: 'Month' }
            }
          }
        }
      });
    })();

    // Severity by Industry Radar Chart
    (() => {
      const severityByIndustry = __SBI__;

      // Check if there's no data (all Unknown industries)
      if (severityByIndustry.no_data || !severityByIndustry.industries || severityByIndustry.industries.length === 0) {
        const container = document.getElementById('severityByIndustryChart').parentElement;
        container.innerHTML =
          '<div class="chart-title">Average Severity by Industry</div>' +
          '<div class="text-center text-muted mt-5">' +
          '<p>No industry data available.</p>' +
          '<p style="font-size: 0.9rem;">The enrichment process has not yet populated industry information for events.</p>' +
          '</div>';
        return;
      }

      new Chart(document.getElementById('severityByIndustryChart').getContext('2d'), {
        type: 'radar',
        data: {
          labels: severityByIndustry.industries,
          datasets: [{
            label: 'Average Severity',
            data: severityByIndustry.avg_severities,
            backgroundColor: '#2563eb20',
            borderColor: '#2563eb',
            borderWidth: 2,
            pointBackgroundColor: '#2563eb',
            pointBorderColor: '#2563eb'
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            r: {
              beginAtZero: true,
              max: 4,
              ticks: {
                callback: function(value) {
                  const severityMap = {0: 'Unknown', 1: 'Low', 2: 'Medium', 3: 'High', 4: 'Critical'};
                  return severityMap[value] || value;
                }
              }
            }
          }
        }
      });
    })();

    // Severity by Attack Type Radar Chart
    (() => {
      const severityByAttackType = __SBAT__;
      new Chart(document.getElementById('severityByAttackTypeChart').getContext('2d'), {
        type: 'radar',
        data: {
          labels: severityByAttackType.attack_types,
          datasets: [{
            label: 'Average Severity',
            data: severityByAttackType.avg_severities,
            backgroundColor: '#05966920',
            borderColor: '#059669',
            borderWidth: 2,
            pointBackgroundColor: '#059669',
            pointBorderColor: '#059669'
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            r: {
              beginAtZero: true,
              max: 4,
              ticks: {
                callback: function(value) {
                  const severityMap = {0: 'Unknown', 1: 'Low', 2: 'Medium', 3: 'High', 4: 'Critical'};
                  return severityMap[value] || value;
                }
              }
            }
          }
        }
      });
    })();

    // 13) Average Records Affected by Attack Type
    (function() {
      const recordsData = recordsByAttackType;
      new Chart(document.getElementById('recordsByAttackTypeChart').getContext('2d'), {
        type: 'bar',
        data: {
          labels: recordsData.attack_types,
          datasets: [{
            label: 'Average Records Affected',
            data: recordsData.avg_records,
            backgroundColor: '#7c3aed',
            borderColor: '#7c3aed',
            borderWidth: 1
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          indexAxis: 'y',
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                label: function(tooltipItem) {
                  const value = tooltipItem.raw;
                  return `Average: ${value.toLocaleString(undefined, {maximumFractionDigits: 0})} records`;
                }
              }
            }
          },
          scales: {
            x: {
              type: 'logarithmic',
              beginAtZero: true,
              title: { display: true, text: 'Average Records Affected (log scale)' },
              ticks: {
                callback: function(value) {
                  return value.toLocaleString();
                }
              }
            },
            y: {
              title: { display: true, text: 'Attack Type' }
            }
          }
        }
      });
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
            .replace('__MSPM__', mspm)
            .replace('__MEDSPM__', medspm)
            .replace('__MRP__', mrpm)
            .replace('__SBI__', sbi)
            .replace('__SBAT__', sbat)
            .replace('__RBAT__', rbat)
            .replace('__MCS__', mcs)
            .replace('__ETC__', etc)
            .replace('__OAIC_COMP__', oaic_comp)
            .replace('__OAIC_CI__', oaic_ci)
            .replace('__OAIC_AT__', oaic_at)
            .replace('__OAIC_SEC__', oaic_sec)
            .replace('__OAIC_IND_AFF__', oaic_ind_aff)
            .replace('__ASD_ALL__', asd_all)
            .replace('__ASD_CURR__', asd_current)
            .replace('__ASD_PREV__', asd_previous)
           )


def main():
    parser = argparse.ArgumentParser(description='Build a static dashboard HTML (no Flask).')
    parser.add_argument('--db-path', default='instance/cyber_events.db', help='Path to SQLite database file')
    parser.add_argument('--out-dir', default='dashboard', help='Output directory for static HTML')
    args = parser.parse_args()

    start_date = '2020-01-01'
    end_date = date.today().strftime('%Y-%m-%d')
    current_year = date.today().year

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
        oaic_comparison = prepare_oaic_comparison_data(database_half_yearly, oaic_data, end_date)

        # Prepare additional OAIC data for new charts
        oaic_cyber_incidents = prepare_oaic_cyber_incidents_data(oaic_data)
        oaic_attack_types = prepare_oaic_attack_types_data(oaic_data)
        oaic_sectors = prepare_oaic_sectors_data(oaic_data)
        oaic_individuals_affected = prepare_oaic_individuals_affected_data(oaic_data)

        event_type_mix = get_monthly_event_type_mix(conn, start_date, end_date)

        data = {
            'monthly_counts': monthly_counts,
            'severity_trends': get_monthly_severity_trends(conn, start_date, end_date),
            'records_affected': get_monthly_records_affected(conn, start_date, end_date),
            'event_type_mix': event_type_mix,
            'overall_event_type_mix': get_overall_event_type_mix(conn, start_date, end_date),
            'entity_types': get_entity_type_distribution(conn, start_date, end_date),
            'records_histogram': get_records_affected_histogram(conn, start_date, end_date),
            'max_severity_per_month': get_maximum_severity_per_month(conn, start_date, end_date),
            'median_severity_per_month': get_median_severity_per_month(conn, start_date, end_date),
            'max_records_per_month': get_maximum_records_affected_per_month(conn, start_date, end_date),
            'severity_by_industry': get_severity_by_industry(conn, start_date, end_date),
            'severity_by_attack_type': get_severity_by_attack_type(conn, start_date, end_date),
            'records_by_attack_type': get_records_affected_by_attack_type(conn, start_date, end_date),
            'monthly_counts_stats': compute_monthly_counts_stats(monthly_counts),
            'event_type_correlation': compute_event_type_correlation_matrix(event_type_mix),
            'oaic_comparison': oaic_comparison,
            'oaic_cyber_incidents': oaic_cyber_incidents,
            'oaic_attack_types': oaic_attack_types,
            'oaic_sectors': oaic_sectors,
            'oaic_individuals_affected': oaic_individuals_affected,
            'asd_risk_all': get_asd_risk_matrix(conn),
            'asd_risk_current': get_asd_risk_matrix(conn, current_year),
            'asd_risk_previous': get_asd_risk_matrix(conn, current_year - 1),
        }

    html = build_html(data, start_date, end_date)
    out_file.write_text(html, encoding='utf-8')
    print(f'Static dashboard generated: {out_file}')


if __name__ == '__main__':
    main()
