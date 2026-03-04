# Dashboard Generation Stage

This document provides a detailed specification of the dashboard generation stage (Phase 5) of the Australian Cyber Events pipeline. It covers the data extraction queries, statistical computations, chart configurations, OAIC comparison methodology, ASD risk matrix rendering, and HTML assembly process in sufficient detail for independent review and replication.

---

## Table of Contents

1. [Purpose and Scope](#1-purpose-and-scope)
2. [High-Level Data Flow](#2-high-level-data-flow)
3. [Invocation and Configuration](#3-invocation-and-configuration)
4. [Data Extraction (SQL Queries)](#4-data-extraction-sql-queries)
   - 4.1 [Monthly Event Counts](#41-monthly-event-counts)
   - 4.2 [Monthly Severity Trends](#42-monthly-severity-trends)
   - 4.3 [Monthly Records Affected](#43-monthly-records-affected)
   - 4.4 [Monthly Event Type Mix](#44-monthly-event-type-mix)
   - 4.5 [Overall Event Type Mix](#45-overall-event-type-mix)
   - 4.6 [Entity Type Distribution](#46-entity-type-distribution)
   - 4.7 [Records Affected Histogram](#47-records-affected-histogram)
   - 4.8 [Maximum and Average Severity Per Month](#48-maximum-and-average-severity-per-month)
   - 4.9 [Median Severity Per Month](#49-median-severity-per-month)
   - 4.10 [Maximum Records Affected Per Month](#410-maximum-records-affected-per-month)
   - 4.11 [Severity by Industry](#411-severity-by-industry)
   - 4.12 [Severity by Attack Type](#412-severity-by-attack-type)
   - 4.13 [Records Affected by Attack Type](#413-records-affected-by-attack-type)
   - 4.14 [Half-Yearly Database Counts](#414-half-yearly-database-counts)
   - 4.15 [ASD Risk Matrix](#415-asd-risk-matrix)
5. [OAIC Data Integration](#5-oaic-data-integration)
   - 5.1 [Loading OAIC JSON Files](#51-loading-oaic-json-files)
   - 5.2 [OAIC vs Database Comparison](#52-oaic-vs-database-comparison)
   - 5.3 [OAIC Cyber Incidents](#53-oaic-cyber-incidents)
   - 5.4 [OAIC Attack Types](#54-oaic-attack-types)
   - 5.5 [OAIC Top Sectors](#55-oaic-top-sectors)
   - 5.6 [OAIC Individuals Affected](#56-oaic-individuals-affected)
6. [Statistical Computations](#6-statistical-computations)
   - 6.1 [Overdispersion Analysis](#61-overdispersion-analysis)
   - 6.2 [Event Type Correlation Matrix](#62-event-type-correlation-matrix)
   - 6.3 [Monthly Counts Histogram and Dispersion Estimate](#63-monthly-counts-histogram-and-dispersion-estimate)
   - 6.4 [Distribution-Based Statistics (OAIC)](#64-distribution-based-statistics-oaic)
7. [HTML Assembly and Rendering](#7-html-assembly-and-rendering)
   - 7.1 [Template Architecture](#71-template-architecture)
   - 7.2 [External Dependencies (CDN)](#72-external-dependencies-cdn)
   - 7.3 [CSS Styling](#73-css-styling)
   - 7.4 [Data Injection Mechanism](#74-data-injection-mechanism)
8. [Chart Specifications](#8-chart-specifications)
   - 8.1 [Monthly Event Count (Line Chart)](#81-monthly-event-count-line-chart)
   - 8.2 [Severity Trends (Stacked Bar Chart)](#82-severity-trends-stacked-bar-chart)
   - 8.3 [Records Affected (Line Chart, Log Scale)](#83-records-affected-line-chart-log-scale)
   - 8.4 [Event Type Mix (Stacked Bar Chart)](#84-event-type-mix-stacked-bar-chart)
   - 8.5 [Entity Type Distribution (Doughnut Chart)](#85-entity-type-distribution-doughnut-chart)
   - 8.6 [Overall Event Type Mix (Pie Chart)](#86-overall-event-type-mix-pie-chart)
   - 8.7 [Records Affected Histogram (Bar Chart)](#87-records-affected-histogram-bar-chart)
   - 8.8 [Severity Per Month (Line Chart)](#88-severity-per-month-line-chart)
   - 8.9 [Maximum Records Affected Per Month (Line Chart)](#89-maximum-records-affected-per-month-line-chart)
   - 8.10 [Severity by Industry (Radar Chart)](#810-severity-by-industry-radar-chart)
   - 8.11 [Severity by Attack Type (Radar Chart)](#811-severity-by-attack-type-radar-chart)
   - 8.12 [Records Affected by Attack Type (Horizontal Bar Chart)](#812-records-affected-by-attack-type-horizontal-bar-chart)
   - 8.13 [Monthly Counts Histogram (Bar Chart)](#813-monthly-counts-histogram-bar-chart)
   - 8.14 [Database vs OAIC Comparison (Line Chart)](#814-database-vs-oaic-comparison-line-chart)
   - 8.15 [OAIC Cyber Incidents (Line Chart)](#815-oaic-cyber-incidents-line-chart)
   - 8.16 [OAIC Attack Types (Multi-Line Chart)](#816-oaic-attack-types-multi-line-chart)
   - 8.17 [OAIC Top Sectors (Horizontal Grouped Bar Chart)](#817-oaic-top-sectors-horizontal-grouped-bar-chart)
   - 8.18 [Database/OAIC Ratio by Sector (Horizontal Bar Chart)](#818-databaseoaic-ratio-by-sector-horizontal-bar-chart)
   - 8.19 [OAIC Individuals Affected (Multi-Line Chart)](#819-oaic-individuals-affected-multi-line-chart)
   - 8.20 [ASD Risk Matrices (HTML Tables)](#820-asd-risk-matrices-html-tables)
   - 8.21 [Event Type Correlation Matrix (HTML Heatmap Table)](#821-event-type-correlation-matrix-html-heatmap-table)
9. [ASD Risk Matrix Framework](#9-asd-risk-matrix-framework)
10. [Colour Schemes and Visual Conventions](#10-colour-schemes-and-visual-conventions)
11. [Empty Data Handling](#11-empty-data-handling)
12. [LLM Prompts](#12-llm-prompts)
13. [Output Files](#13-output-files)
14. [Justification of Design Decisions](#14-justification-of-design-decisions)
15. [Replication Guide](#15-replication-guide)

---

## 1. Purpose and Scope

The dashboard generation stage produces a self-contained, interactive HTML dashboard summarising all cyber events in the pipeline database. It serves three purposes:

- **Trend Analysis** — Visualise monthly trends in event volume, severity, records affected, and event types over the full date range (2020 to present).
- **OAIC Benchmarking** — Compare pipeline-discovered events against official OAIC Notifiable Data Breach statistics to assess coverage and identify gaps.
- **ASD Risk Classification Display** — Present the Australian Signals Directorate risk matrix showing how events distribute across impact severity and stakeholder categories.

This stage corresponds to Phase 5 of the five-phase pipeline. It is invoked by `run_full_pipeline.py` and the core logic resides in `scripts/build_static_dashboard.py`.

### What This Stage Does NOT Do

This stage does not discover events (Phase 1), enrich events with Perplexity AI (Phase 2), deduplicate events (Phase 3), or classify events against the ASD risk matrix (Phase 4). It reads the outputs of those phases from the SQLite database and produces read-only visualisations.

### Why a Static Dashboard?

The dashboard is generated as a single self-contained HTML file rather than a server-based application. This design was chosen because:

1. **Portability** — The file can be shared via email, uploaded to a static host, or opened directly in a browser without requiring a web server.
2. **No runtime dependencies** — No Python, Node.js, or database server needed to view the dashboard.
3. **Reproducibility** — Each generation produces a point-in-time snapshot tied to the data in the database at that moment.
4. **Security** — No API endpoints to secure; no server to maintain or patch.

---

## 2. High-Level Data Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    Dashboard Generation (Phase 5)                       │
│                                                                         │
│  ┌──────────────────────┐     ┌───────────────────────────┐             │
│  │ OAIC JSON Files      │     │ SQLite Database            │             │
│  │ (oaic_cyber_         │     │ (instance/cyber_events.db) │             │
│  │  statistics_*.json)  │     │                            │             │
│  └──────────┬───────────┘     └──────────────┬────────────┘             │
│             │                                │                          │
│             ▼                                ▼                          │
│  ┌──────────────────┐         ┌──────────────────────────┐              │
│  │ load_oaic_data() │         │ 15 SQL query functions    │              │
│  │ Merge & validate │         │ (get_monthly_event_counts │              │
│  │ OAIC periods     │         │  get_severity_trends ...  │              │
│  └────────┬─────────┘         │  get_asd_risk_matrix)     │              │
│           │                   └──────────────┬───────────┘              │
│           │                                  │                          │
│           └──────────────┬───────────────────┘                          │
│                          ▼                                              │
│              ┌───────────────────────┐                                   │
│              │ Statistical Transforms │                                   │
│              │ - Overdispersion       │                                   │
│              │ - Correlation matrix   │                                   │
│              │ - Pro-rata estimates   │                                   │
│              │ - Sector normalisation │                                   │
│              │ - Midpoint statistics  │                                   │
│              └───────────┬───────────┘                                   │
│                          │                                              │
│                          ▼                                              │
│              ┌───────────────────────┐                                   │
│              │ JSON encode 23 data   │                                   │
│              │ structures + inject   │                                   │
│              │ into HTML template    │                                   │
│              └───────────┬───────────┘                                   │
│                          │                                              │
│                          ▼                                              │
│              ┌───────────────────────┐                                   │
│              │ build_html()          │                                   │
│              │ → dashboard/index.html│                                   │
│              └───────────────────────┘                                   │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Invocation and Configuration

### Entry Points

The dashboard can be generated via three entry points:

| Command | Description |
|---------|-------------|
| `python run_full_pipeline.py` | Full pipeline — dashboard is Phase 5 (runs after classification) |
| `python run_full_pipeline.py --dashboard-only` | Dashboard generation only (skip Phases 1–4) |
| `python scripts/build_static_dashboard.py` | Standalone dashboard generation script |

### Pipeline Invocation

When run as part of the unified pipeline (`run_full_pipeline.py`), the dashboard phase is invoked by `UnifiedPipeline.run_dashboard_phase()` (line 512). Before generating the dashboard, the pipeline validates that the database exists and contains the required tables:

- `DeduplicatedEvents`
- `EntitiesV2`
- `DeduplicatedEventEntities`

If any required table is missing, the phase fails with a `RuntimeError`.

### Configuration Parameters

| Parameter | Default | Source | Description |
|-----------|---------|--------|-------------|
| `start_date` | `2020-01-01` | Hardcoded | Earliest date for dashboard data |
| `end_date` | System date (`date.today()`) | Runtime | Latest date for dashboard data |
| `--db-path` | `instance/cyber_events.db` | CLI argument | SQLite database file path |
| `--out-dir` | `dashboard` | CLI argument | Output directory for generated HTML |

**Source file:** `scripts/build_static_dashboard.py`

**Justification for fixed start date:** The hardcoded `2020-01-01` start date aligns with the availability of OAIC Notifiable Data Breach scheme data (reporting began in 2018, but consistent half-yearly reports with comparable formatting began from 2020). This ensures all OAIC comparison periods have matching data on both sides.

---

## 4. Data Extraction (SQL Queries)

The dashboard extracts data from the SQLite database using 15 distinct query functions. All queries target the `DeduplicatedEvents` table (the final deduplicated and merged event set) and filter on `status = 'Active'` to exclude soft-deleted or superseded events.

### Common Query Patterns

All date-filtered queries use parameterised placeholders (`?`) for `start_date` and `end_date`:

```sql
WHERE status = 'Active'
    AND event_date >= ?
    AND event_date <= ?
```

Month grouping uses SQLite's `strftime('%Y-%m', event_date)` function, producing keys like `2024-03`.

### 4.1 Monthly Event Counts

**Function:** `get_monthly_event_counts()`

**Purpose:** Count distinct deduplicated events per calendar month for the primary trend chart.

```sql
SELECT
    strftime('%Y-%m', event_date) as month,
    COUNT(DISTINCT deduplicated_event_id) as unique_events
FROM DeduplicatedEvents
WHERE status = 'Active'
    AND event_date >= ?
    AND event_date <= ?
GROUP BY strftime('%Y-%m', event_date)
ORDER BY month
```

**Post-processing:** After retrieving counts, the function calculates overdispersion statistics using NumPy and SciPy (see [Section 6.1](#61-overdispersion-analysis)).

**Returns:**

| Field | Type | Description |
|-------|------|-------------|
| `months` | `List[str]` | Month labels (`YYYY-MM`) |
| `counts` | `List[int]` | Event count per month |
| `total_events` | `int` | Sum of all counts |
| `avg_per_month` | `float` | Mean events per month |
| `overdispersion` | `Dict` | Overdispersion statistics (see [Section 6.1](#61-overdispersion-analysis)) |

### 4.2 Monthly Severity Trends

**Function:** `get_monthly_severity_trends()`

**Purpose:** Count events by severity level per month for the stacked severity bar chart.

```sql
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
```

**Post-processing:** Severity strings are normalised by stripping the `EventSeverity.` prefix if present (a legacy artefact from enum serialisation) and applying `.title()` capitalisation. The result is restructured into a dictionary mapping severity names to arrays of per-month counts, padded with zeros where a severity level had no events in a given month.

**Severity levels (in display order):** Critical, High, Medium, Low, Unknown.

### 4.3 Monthly Records Affected

**Function:** `get_monthly_records_affected()`

**Purpose:** Calculate average and median records affected per month for trend analysis.

This function executes two queries:

**Query 1 — Detail values for median calculation:**

```sql
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
```

**Query 2 — Aggregate statistics:**

```sql
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
```

**Filtering rationale:** Records affected values are filtered to the range `(0, 1,000,000,000]`. The lower bound excludes zero and null values (events where records affected is unknown). The upper bound of 1 billion excludes data quality outliers that would distort averages.

**Median calculation:** The median is computed programmatically in Python from sorted per-month values (SQLite has no built-in `MEDIAN` function). For even-length lists, the median is the average of the two middle values.

**Confidence intervals:** A simple ±20% margin around the mean is calculated as an approximate confidence band: `[max(0, avg × 0.8), avg × 1.2]`. This is a heuristic rather than a formal statistical confidence interval.

### 4.4 Monthly Event Type Mix

**Function:** `get_monthly_event_type_mix()`

**Purpose:** Count events by type per month for the stacked event type bar chart.

```sql
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
```

**Post-processing:** Event type strings are cleaned: the `CyberEventType.` prefix is stripped, underscores are replaced with spaces, and the result is title-cased (e.g., `CyberEventType.DATA_BREACH` → `Data Breach`).

### 4.5 Overall Event Type Mix

**Function:** `get_overall_event_type_mix()`

**Purpose:** Aggregate event type distribution across the entire date range for the pie chart.

```sql
SELECT
    COALESCE(event_type, 'Unknown') as event_type,
    COUNT(DISTINCT deduplicated_event_id) as event_count
FROM DeduplicatedEvents
WHERE status = 'Active'
    AND event_date >= ?
    AND event_date <= ?
GROUP BY event_type
ORDER BY event_count DESC
```

**Post-processing:** Same string normalisation as [Section 4.4](#44-monthly-event-type-mix). Null event types default to `'Unknown'` via `COALESCE`.

### 4.6 Entity Type Distribution

**Function:** `get_entity_type_distribution()`

**Purpose:** Categorise events by victim organisation industry using the `IndustryGroupings` lookup table.

**Step 1 — Load industry groupings:**

```sql
SELECT group_name, keywords, display_order
FROM IndustryGroupings
WHERE group_name != 'Others'
ORDER BY display_order
```

The `IndustryGroupings` table maps broad category names (e.g., `Healthcare`, `Finance`) to JSON arrays of keyword strings. Each event's `victim_organization_industry` field is matched against these keywords using case-insensitive substring matching.

**Step 2 — Count events by raw industry:**

```sql
SELECT
    COALESCE(victim_organization_industry, 'Unknown') as industry,
    COUNT(DISTINCT deduplicated_event_id) as event_count
FROM DeduplicatedEvents
WHERE status = 'Active'
    AND event_date >= ?
    AND event_date <= ?
GROUP BY victim_organization_industry
ORDER BY event_count DESC
```

**Mapping logic:** For each event's industry value, the function iterates through the `IndustryGroupings` categories and matches using `any(keyword.lower() in industry.lower() for keyword in keywords)`. First match wins. Unmatched industries are assigned to `Others`. Events with null or `Unknown` industry are counted separately.

**Justification:** Raw industry values from LLM enrichment are inconsistent (e.g., "healthcare", "Health services", "Medical sector" could all refer to the same sector). The `IndustryGroupings` lookup table normalises these into consistent categories for meaningful aggregation.

### 4.7 Records Affected Histogram

**Function:** `get_records_affected_histogram()`

**Purpose:** Create a logarithmic histogram of records affected across all events.

```sql
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
```

**Binning:** Events are assigned to 8 logarithmic bins:

| Bin | Range |
|-----|-------|
| 1 | 1 – 10 |
| 2 | 11 – 100 |
| 3 | 101 – 1K |
| 4 | 1K – 10K |
| 5 | 10K – 100K |
| 6 | 100K – 1M |
| 7 | 1M – 10M |
| 8 | 10M+ |

**Justification for logarithmic bins:** Records affected spans many orders of magnitude — from a handful of records in a small business breach to millions in a major data exposure. Equal-width bins would compress all variation into the first bin. Logarithmic bins provide meaningful granularity across the full range.

### 4.8 Maximum and Average Severity Per Month

**Function:** `get_maximum_severity_per_month()`

**Purpose:** Track the highest-severity event and average severity level per month.

**Query 1 — Maximum severity with entity details:**

```sql
SELECT
    strftime('%Y-%m', de.event_date) as month,
    de.severity,
    de.title,
    e.entity_name,
    de.deduplicated_event_id
FROM DeduplicatedEvents de
LEFT JOIN DeduplicatedEventEntities dee
    ON de.deduplicated_event_id = dee.deduplicated_event_id
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
```

**Query 2 — Average severity (numeric):**

```sql
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
```

**Severity numeric encoding:**

| Severity | Numeric Value |
|----------|---------------|
| Critical | 4 |
| High | 3 |
| Medium | 2 |
| Low | 1 |
| Unknown | 0 |

**Post-processing:** The maximum severity query returns all events ordered by severity within each month. The function takes the first (highest severity) row per month. The average is computed by the database using the `CASE`-based numeric mapping.

### 4.9 Median Severity Per Month

**Function:** `get_median_severity_per_month()`

**Purpose:** Calculate the median severity per month (more robust to outliers than the mean).

```sql
SELECT
    strftime('%Y-%m', event_date) as month,
    severity
FROM DeduplicatedEvents
WHERE status = 'Active'
    AND severity IS NOT NULL
    AND event_date >= ?
    AND event_date <= ?
ORDER BY month, event_date
```

**Post-processing:** Severity strings are converted to numeric values using the same encoding as [Section 4.8](#48-maximum-and-average-severity-per-month). The median is computed in Python from sorted per-month numeric values.

### 4.10 Maximum Records Affected Per Month

**Function:** `get_maximum_records_affected_per_month()`

**Purpose:** Identify the largest data breach in each month, with event title and entity name.

```sql
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
LEFT JOIN DeduplicatedEventEntities dee
    ON de.deduplicated_event_id = dee.deduplicated_event_id
LEFT JOIN EntitiesV2 e ON dee.entity_id = e.entity_id
LEFT JOIN EnrichedEvents me
    ON de.master_enriched_event_id = me.enriched_event_id
WHERE de.status = 'Active'
    AND de.records_affected IS NOT NULL
    AND CAST(de.records_affected AS INTEGER) > 0
    AND CAST(de.records_affected AS INTEGER) <= 1000000000
    AND de.event_date >= ?
    AND de.event_date <= ?
ORDER BY de.event_date, CAST(de.records_affected AS INTEGER) DESC
```

**Entity name resolution:** The entity name uses a three-level fallback: (1) `EntitiesV2.entity_name` via the join, (2) `formal_entity_name` extracted from the Perplexity enrichment JSON stored on the `EnrichedEvents` row, (3) the literal string `'Unknown'`.

**Post-processing:** The function iterates through rows (already ordered by records descending within each month) and retains only the maximum-records event per month.

### 4.11 Severity by Industry

**Function:** `get_severity_by_industry()`

**Purpose:** Calculate weighted average severity per industry category for the radar chart.

Uses the same `IndustryGroupings` lookup table as [Section 4.6](#46-entity-type-distribution).

```sql
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
```

**Weighted average calculation:** For each industry category, the average severity is computed as: `sum(severity_numeric × event_count) / sum(event_count)`. This weights each severity level by the number of events at that level, producing a single 0–4 score per industry.

### 4.12 Severity by Attack Type

**Function:** `get_severity_by_attack_type()`

**Purpose:** Calculate weighted average severity per attack method for the radar chart.

```sql
SELECT
    COALESCE(attack_method, 'Unknown') as attack_type,
    severity,
    COUNT(*) as event_count
FROM DeduplicatedEvents
WHERE status = 'Active'
    AND severity IS NOT NULL
    AND event_date >= ?
    AND event_date <= ?
GROUP BY COALESCE(attack_method, 'Unknown'), severity
ORDER BY COALESCE(attack_method, 'Unknown')
```

**Post-processing:** Same weighted average calculation as [Section 4.11](#411-severity-by-industry), but grouped by `attack_method` rather than industry.

### 4.13 Records Affected by Attack Type

**Function:** `get_records_affected_by_attack_type()`

**Purpose:** Calculate average records affected per attack type for the horizontal bar chart.

```sql
SELECT
    COALESCE(attack_method, 'Unknown') as attack_type,
    AVG(CAST(records_affected AS FLOAT)) as avg_records,
    COUNT(DISTINCT deduplicated_event_id) as event_count
FROM DeduplicatedEvents
WHERE status = 'Active'
    AND records_affected IS NOT NULL
    AND CAST(records_affected AS INTEGER) > 0
    AND CAST(records_affected AS INTEGER) <= 1000000000
    AND event_date >= ?
    AND event_date <= ?
GROUP BY COALESCE(attack_method, 'Unknown')
ORDER BY avg_records DESC
```

### 4.14 Half-Yearly Database Counts

**Function:** `get_half_yearly_database_counts()`

**Purpose:** Count events in half-year periods (H1: Jan–Jun, H2: Jul–Dec) for comparison with OAIC reporting periods.

```sql
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
```

**Period labels:** Formatted as `"2024 H1"`, `"2024 H2"`, etc. These align directly with OAIC reporting periods.

**Justification:** OAIC publishes its Notifiable Data Breaches reports on a half-yearly cycle (January–June and July–December). Using the same period boundaries enables direct comparison between pipeline discoveries and official notification counts.

### 4.15 ASD Risk Matrix

**Function:** `get_asd_risk_matrix()`

**Purpose:** Aggregate ASD risk classifications into a 6×6 matrix (impact type × stakeholder group).

```sql
SELECT arc.impact_type, arc.primary_stakeholder_category, COUNT(*) as count
FROM ASDRiskClassifications arc
LEFT JOIN DeduplicatedEvents de
    ON arc.deduplicated_event_id = de.deduplicated_event_id
[WHERE de.event_date IS NOT NULL
    AND CAST(strftime('%Y', de.event_date) AS INTEGER) = ?]
GROUP BY arc.impact_type, arc.primary_stakeholder_category
```

The `WHERE` clause is conditionally included only when filtering by year. Three instances of this query are executed:

1. **All years** — No year filter
2. **Current year** — `year = date.today().year`
3. **Previous year** — `year = date.today().year - 1`

**Stakeholder grouping:** The ASD framework defines 15 individual stakeholder categories. For display in the matrix, these are consolidated into 6 groups (see [Section 9](#9-asd-risk-matrix-framework)). The function sums counts across all individual categories within each group.

**Validation:** Impact types and stakeholder categories are validated against predefined lists. Rows with unrecognised values are silently discarded to prevent rendering errors from unexpected classification outputs.

---

## 5. OAIC Data Integration

The dashboard integrates data from OAIC (Office of the Australian Information Commissioner) Notifiable Data Breach reports. OAIC data is loaded from pre-scraped JSON files rather than queried from the database, because OAIC statistics (aggregate industry counts, individuals affected distributions) are not stored as events in the pipeline database.

### 5.1 Loading OAIC JSON Files

**Function:** `load_oaic_data()`

**Source files:** `oaic_cyber_statistics_*.json` (glob pattern in project root)

These JSON files are produced by the OAIC scraping scripts (`scripts/oaic/oaic_data_scraper.py` and `scripts/oaic/OAIC_dashboard_scraper.py`). Two scraping methods exist because OAIC publishes data in two forms:

1. **PDF reports** — Semi-annual reports with tables (scraped by `oaic_data_scraper.py`). These contain `individuals_affected_average` and `individuals_affected_median` values.
2. **Power BI dashboard** — Interactive online dashboard (scraped by `OAIC_dashboard_scraper.py`). This contains `individuals_affected_distribution` data (range → count pairs).

**Merge logic:** When multiple JSON files contain data for the same period (identified by `"{year} {period}"` key, e.g., `"2024 H1"`):

| Field | Merge Strategy | Reason |
|-------|----------------|--------|
| `individuals_affected_average` | Prefer PDF-scraped | More precise than calculated from distribution |
| `individuals_affected_median` | Prefer PDF-scraped | More precise than calculated from distribution |
| `individuals_affected_distribution` | Prefer dashboard-scraped | Only available from Power BI dashboard |
| `top_sectors` | Prefer non-null | Either source is acceptable |
| Attack types (`phishing`, `ransomware`, etc.) | Prefer non-null | Fill gaps from whichever source has data |
| All other fields | Prefer non-null | Keep earliest available value |

**Validation:** Each record is validated before merging. Attack type values that exceed `total_notifications × 1.1` are nullified (set to `None`) as they are likely scraping artefacts.

**Sorting:** Merged data is sorted chronologically by `(year, period)` where H1 sorts before H2.

### 5.2 OAIC vs Database Comparison

**Function:** `prepare_oaic_comparison_data()`

**Purpose:** Align database half-yearly event counts with OAIC official notification counts for side-by-side comparison.

**Logic:**

1. Create lookup from OAIC data: `{period_key: total_notifications}`.
2. Create lookup from database data: `{period_key: unique_events}`.
3. Form a union of all periods from both sources.
4. Filter to periods from `2020 H1` onwards.
5. For each period, include both the database count and the OAIC count (either may be `null` if data is missing for that period).

**Pro-rata estimation:** If the current date falls within an incomplete half-year period (e.g., the dashboard is generated in March 2026 during the 2026 H1 period), a pro-rata estimate is calculated:

```
days_elapsed = (actual_end - period_start).days + 1
days_in_period = (period_end - period_start).days + 1
months_elapsed = (days_elapsed / days_in_period) × 6
prorata_estimate = actual_count × (6.0 / months_elapsed)
```

This extrapolates the partial period's count to a full 6-month estimate, providing a forward-looking indicator.

### 5.3 OAIC Cyber Incidents

**Function:** `prepare_oaic_cyber_incidents_data()`

**Purpose:** Extract cyber incident counts versus total notifications from OAIC data.

**Logic:** For each OAIC period, extracts `cyber_incidents_total`. If this field is missing, it is calculated from `cyber_incidents_percentage × total_notifications / 100`. Both cyber incident count and total notification count are returned per period.

### 5.4 OAIC Attack Types

**Function:** `prepare_oaic_attack_types_data()`

**Purpose:** Extract attack type breakdown over time from OAIC data.

**Attack types tracked:** `ransomware`, `phishing`, `hacking`, `malware`, `brute_force`, `compromised_credentials`.

Values are taken directly from each OAIC period record (pre-scraped from OAIC reports). Null values are preserved to indicate periods where that attack type was not reported.

### 5.5 OAIC Top Sectors

**Function:** `prepare_oaic_sectors_data()`

**Purpose:** Aggregate OAIC's top affected sectors across all periods and compare with database industry counts.

**OAIC aggregation:**

1. Iterate all OAIC period records and sum `notifications` by `sector` from the `top_sectors` field.
2. Normalise sector names to canonical forms (see mapping below).
3. Sort by total notifications descending and take the top 10.

**Sector name normalisation:**

| Raw Variant | Canonical Name |
|-------------|----------------|
| `Australian government` | `Australian Government` |
| `Government` | `Australian Government` |
| `Finance (incl. superannuation)` | `Finance` |
| `Finance (including superannuation)` | `Finance` |
| `Health service providers` | `Health` |
| `Health services` | `Health` |
| `Healthcare` | `Health` |

**Database comparison query:**

```sql
SELECT victim_organization_industry, COUNT(*) as count
FROM DeduplicatedEvents
WHERE event_date >= '2019-01-01' AND event_date <= '2024-12-31'
AND victim_organization_industry IS NOT NULL
GROUP BY victim_organization_industry
```

**Industry mapping (database → OAIC):**

| Database Industry | OAIC Sector |
|-------------------|-------------|
| `Healthcare` | `Health` |
| `Government` | `Australian Government` |
| `Finance` | `Finance` |
| `Retail` | `Retail` |
| `Education` | `Education` |

**Coverage ratio:** For each sector: `ratio = database_count / oaic_count`. A ratio of 1.0 means the database has discovered exactly as many events as OAIC reported for that sector. Values above 1.0 indicate the database has broader coverage (including non-notifiable incidents); values below 1.0 indicate gaps in discovery.

### 5.6 OAIC Individuals Affected

**Function:** `prepare_oaic_individuals_affected_data()`

**Purpose:** Compare OAIC individuals-affected statistics with database records-affected averages.

**OAIC data sources (per period):**

1. `individuals_affected_average` — from PDF reports (preferred)
2. `individuals_affected_median` — from PDF reports (preferred)
3. `individuals_affected_distribution` — from Power BI dashboard (used as fallback for calculating average/median when PDF values are missing)

**Fallback calculation:** When average or median is missing, they are calculated from the distribution data using midpoint estimation (see [Section 6.4](#64-distribution-based-statistics-oaic)).

**Database comparison query:**

```sql
SELECT
    strftime('%Y', event_date) as year,
    CASE WHEN CAST(strftime('%m', event_date) AS INTEGER) <= 6
         THEN 'H1' ELSE 'H2' END as half,
    AVG(records_affected) as avg_records
FROM DeduplicatedEvents
WHERE event_date >= '2019-01-01' AND event_date <= '2025-12-31'
AND records_affected IS NOT NULL
GROUP BY year, half
ORDER BY year, half
```

---

## 6. Statistical Computations

### 6.1 Overdispersion Analysis

**Computed in:** `get_monthly_event_counts()` (post-query processing)

**Purpose:** Assess whether the distribution of monthly event counts follows a Poisson or Negative Binomial distribution. This is relevant for determining appropriate statistical models for event arrival rates.

**Method:**

1. **Overdispersion parameter (φ):** `φ = sample_variance / mean`. A Poisson distribution has φ ≈ 1. Values significantly above 1 indicate overdispersion (more variability than Poisson predicts), suggesting a Negative Binomial model is more appropriate.

2. **Poisson fit:** Single parameter `λ = mean`.

3. **Negative Binomial fit (method of moments):** When `variance > mean`:
   - `n = mean² / (variance - mean)`
   - `p = mean / variance`

4. **Interpretation thresholds:**
   - `φ < 1.2` → Poisson
   - `φ > 1.5` → Negative Binomial
   - `1.2 ≤ φ ≤ 1.5` → Mixed

**Justification:** Cyber incidents are not uniformly distributed over time — they tend to cluster (e.g., multiple incidents following a widely-exploited vulnerability). Overdispersion analysis quantifies this clustering tendency and informs whether simple Poisson-based forecasting is appropriate or whether a more flexible model is needed.

### 6.2 Event Type Correlation Matrix

**Function:** `compute_event_type_correlation_matrix()`

**Purpose:** Calculate Pearson correlation coefficients between event types based on monthly co-occurrence patterns.

**Method:**

1. Filter event types to those with at least 5 non-zero months (minimum data requirement for meaningful correlation).
2. Construct a Pandas DataFrame with monthly counts per event type.
3. Compute the Pearson correlation matrix using `DataFrame.corr()`.
4. Fill NaN values with 0.0 (can occur when an event type has zero variance).
5. Round to 3 decimal places.

**Interpretation:** A positive correlation between two event types indicates they tend to increase and decrease together month-over-month. A negative correlation indicates an inverse relationship. This can reveal patterns such as ransomware and data breach events co-occurring, or certain attack types displacing others.

**Minimum data threshold (5 months):** Correlations computed from fewer than 5 data points are unreliable and likely to show spurious strong correlations. The threshold of 5 is conservative but prevents misleading results.

### 6.3 Monthly Counts Histogram and Dispersion Estimate

**Function:** `compute_monthly_counts_stats()`

**Purpose:** Create a histogram of monthly event counts and estimate the dispersion parameter for distribution fitting.

**Binning:** 10 equal-width bins from `min(counts)` to `max(counts)`.

**Dispersion estimate:** Uses the same method-of-moments approach as [Section 6.1](#61-overdispersion-analysis), but with the simpler population variance formula (`/ n` rather than `/ (n-1)`):

- If `variance > mean`: `k = mean² / (variance - mean)` → Over-dispersed (Negative Binomial)
- If `|variance - mean| / mean < 0.1`: Variance ≈ Mean → Poisson
- Otherwise: Under-dispersed

### 6.4 Distribution-Based Statistics (OAIC)

**Function:** `calculate_stats_from_distribution()`

**Purpose:** Estimate average and median from OAIC individuals-affected distribution data (range → count pairs).

**Midpoint mapping:**

| Range | Midpoint |
|-------|----------|
| `1` | 1 |
| `2-10` | 6 |
| `11-100` | 55 |
| `101-1,000` | 550 |
| `1,001-5,000` | 3,000 |
| `5,001-10,000` | 7,500 |
| `10,001-50,000` | 30,000 |
| `50,001-100,000` | 75,000 |
| `100,001-250,000` | 175,000 |
| `250,001-500,000` | 375,000 |
| `500,001-1,000,000` | 750,000 |
| `1,000,001-5,000,000` | 3,000,000 |
| `5,000,001+` | 7,500,000 |

**Method:** Each range's midpoint is repeated `count` times to build a synthetic value list. The average and median are then computed from this expanded list.

**Justification:** OAIC publishes individuals-affected data as histograms (count of breaches in each range), not as individual values. Midpoint estimation is the standard technique for recovering summary statistics from grouped data. The midpoints are chosen as the geometric midpoint of each range, which is appropriate for right-skewed distributions.

**Limitation:** The open-ended top range (`5,000,001+`) uses an assumed midpoint of 7,500,000, which could underestimate or overestimate the true average for extremely large breaches.

---

## 7. HTML Assembly and Rendering

### 7.1 Template Architecture

**Function:** `build_html()`

The dashboard is assembled using a single Python template string containing the complete HTML document (~1,400 lines). Data is injected by replacing placeholder tokens with JSON-serialised data structures.

This approach was chosen over a template engine (Jinja2, Mako) because:

1. **Zero additional dependencies** — No template library is needed beyond Python's built-in string operations.
2. **Single-file output** — All CSS, JavaScript, and data are embedded in one HTML file.
3. **Transparency** — The complete HTML structure is visible in a single location (`build_html()` in `scripts/build_static_dashboard.py`), making it easy to audit and modify.

### 7.2 External Dependencies (CDN)

The generated HTML loads two external libraries via CDN:

| Library | Version | CDN URL | Purpose |
|---------|---------|---------|---------|
| Bootstrap | 5.3.0 | `cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css` | Responsive grid layout and table styling |
| Chart.js | 4.4.0 | `cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js` | Interactive chart rendering |

**Justification for CDN loading:** Loading from CDN reduces the HTML file size significantly (Bootstrap CSS alone is ~190KB minified). Since the dashboard is not intended for air-gapped environments, CDN loading is an acceptable trade-off.

**Limitation:** The dashboard requires an internet connection to load CSS and JavaScript on first view. Browsers that cache these resources will work offline on subsequent views.

### 7.3 CSS Styling

All custom CSS is inlined in a `<style>` block within the HTML `<head>`:

```css
body { background-color: #f8f9fa; }

.dashboard-header {
    background: linear-gradient(135deg, #2563eb, #1e40af);
    color: white;
    padding: 2rem 0;
    margin-bottom: 2rem;
}

.chart-container {
    background: white;
    border-radius: 12px;
    padding: 1.25rem;
    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    margin-bottom: 2rem;
    height: 380px;
}

.chart-title {
    font-size: 1.1rem;
    font-weight: 600;
    color: #374151;
    margin-bottom: 1rem;
    text-align: center;
}

.last-updated { color: #e5e7eb; font-size: 0.9rem; }
.risk-matrix table { font-size: 0.9rem; }
.risk-matrix .impact-label { width: 260px; text-align: left; font-weight: 600; }
.risk-matrix .group-header { white-space: normal; }
```

### 7.4 Data Injection Mechanism

The `build_html()` function (line 1613) serialises 23 data structures to JSON and replaces placeholder tokens in the HTML template:

| Placeholder | Data Source | Description |
|-------------|-------------|-------------|
| `__MC__` | `monthly_counts` | Monthly event counts + overdispersion |
| `__SEV__` | `severity_trends` | Monthly severity breakdown |
| `__RA__` | `records_affected` | Monthly avg/median records affected |
| `__ETM__` | `event_type_mix` | Monthly event type breakdown |
| `__OETM__` | `overall_event_type_mix` | Aggregate event type distribution |
| `__ENT__` | `entity_types` | Entity/industry type distribution |
| `__RH__` | `records_histogram` | Records affected histogram bins |
| `__MSPM__` | `max_severity_per_month` | Max + avg severity per month |
| `__MEDSPM__` | `median_severity_per_month` | Median severity per month |
| `__MRP__` | `max_records_per_month` | Max records affected per month |
| `__SBI__` | `severity_by_industry` | Avg severity by industry |
| `__SBAT__` | `severity_by_attack_type` | Avg severity by attack type |
| `__RBAT__` | `records_by_attack_type` | Avg records by attack type |
| `__MCS__` | `monthly_counts_stats` | Histogram + dispersion statistics |
| `__ETC__` | `event_type_correlation` | Correlation matrix |
| `__OAIC_COMP__` | `oaic_comparison` | DB vs OAIC half-yearly |
| `__OAIC_CI__` | `oaic_cyber_incidents` | OAIC cyber vs total notifications |
| `__OAIC_AT__` | `oaic_attack_types` | OAIC attack type breakdown |
| `__OAIC_SEC__` | `oaic_sectors` | OAIC sector comparison |
| `__OAIC_IND_AFF__` | `oaic_individuals_affected` | OAIC individuals affected trends |
| `__ASD_ALL__` | `asd_risk_all` | ASD risk matrix (all years) |
| `__ASD_CURR__` | `asd_risk_current` | ASD risk matrix (current year) |
| `__ASD_PREV__` | `asd_risk_previous` | ASD risk matrix (previous year) |
| `__START__` | `start_date` | Dashboard start date label |
| `__END__` | `end_date` | Dashboard end date label |

In the generated HTML, these placeholders appear inside JavaScript variable declarations:

```javascript
const monthlyCounts = __MC__;
const severityTrends = __SEV__;
// ... etc.
```

After replacement, each becomes a valid JavaScript object literal (the JSON output of `json.dumps()`).

---

## 8. Chart Specifications

All charts use Chart.js 4.4.0 and share common options: `responsive: true`, `maintainAspectRatio: false`. Each chart renders inside a `<canvas>` element within a 380px-tall container (unless otherwise noted).

### 8.1 Monthly Event Count (Line Chart)

**Chart type:** `line`
**Data:** `monthlyCounts.months` (x-axis) vs `monthlyCounts.counts` (y-axis)

**Features:**
- Filled area under the line (light blue, `#2563eb20`)
- Linear regression trend line (red, dashed) computed in JavaScript:
  - Slope and intercept calculated using ordinary least squares
  - Rendered as a second dataset with `borderDash: [5, 5]`, `pointRadius: 0`
- Y-axis begins at zero

**Justification:** A line chart with trend line shows both the raw monthly variation and the underlying trajectory. The linear regression provides a simple, interpretable measure of whether event discovery is increasing or decreasing over time.

### 8.2 Severity Trends (Stacked Bar Chart)

**Chart type:** `bar` with `stacked: true` on both axes
**Data:** One dataset per severity level (Critical, High, Medium, Low, Unknown)

**Severity colours:**

| Level | Colour |
|-------|--------|
| Critical | `#dc2626` (red) |
| High | `#ea580c` (orange) |
| Medium | `#ca8a04` (amber) |
| Low | `#16a34a` (green) |
| Unknown | `#6b7280` (grey) |

**Justification:** Stacked bars show both the total event count and the severity composition per month. The colour scheme uses a red-to-green progression that matches intuitive severity associations.

### 8.3 Records Affected (Line Chart, Log Scale)

**Chart type:** `line`
**Y-axis:** Logarithmic scale

**Datasets:**
1. Average Records Affected — solid line (green, `#10b981`)
2. Median Records Affected — dashed line (blue, `#2563eb`)

**Justification:** Records affected spans many orders of magnitude, making a linear Y-axis impractical. The logarithmic scale allows meaningful comparison of trends across months with very different magnitudes. Showing both mean and median reveals skewness — when the mean is much higher than the median, a few very large breaches are pulling the average up.

### 8.4 Event Type Mix (Stacked Bar Chart)

**Chart type:** `bar` with `stacked: true`

**Colour generation:** Colours are dynamically generated using the golden angle algorithm:

```javascript
hue = (index × 137.508) % 360
saturation = 70%
lightness = 50%
```

**Justification:** The golden angle (137.508°) produces maximally separated hues regardless of how many event types exist. This ensures that adjacent event types in the chart always have visually distinct colours, even when there are 10+ types.

### 8.5 Entity Type Distribution (Doughnut Chart)

**Chart type:** `doughnut`
**Legend position:** Right

**Colour generation:** Evenly distributed hues: `hue = (index × 360 / total_types)`.

### 8.6 Overall Event Type Mix (Pie Chart)

**Chart type:** `pie`
**Legend position:** Right
**Colour generation:** Same golden angle method as [Section 8.4](#84-event-type-mix-stacked-bar-chart).

### 8.7 Records Affected Histogram (Bar Chart)

**Chart type:** `bar`
**Colour:** Cyan (`#06b6d4`) with alpha transparency
**Legend:** Hidden (single dataset)
**Y-axis:** Begins at zero (frequency count)

### 8.8 Severity Per Month (Line Chart)

**Chart type:** `line`

**Datasets:**
1. Maximum Severity — solid line (red, `#dc2626`), `pointRadius: 6`, `borderWidth: 3`
2. Average Severity — dashed line (amber, `#f59e0b`), `pointRadius: 4`, `borderWidth: 2`

**Y-axis:** Fixed range 0–4 with custom tick labels:

| Value | Label |
|-------|-------|
| 0 | Unknown |
| 1 | Low |
| 2 | Medium |
| 3 | High |
| 4 | Critical |

**Custom tooltip:** Displays the severity label name and, for the average, shows both the label and the numeric value to two decimal places.

### 8.9 Maximum Records Affected Per Month (Line Chart)

**Chart type:** `line`
**Y-axis:** Logarithmic scale
**Legend:** Hidden (single dataset)

**Custom tooltip:** Shows the event title, entity name, and formatted record count for each data point.

### 8.10 Severity by Industry (Radar Chart)

**Chart type:** `radar`
**Radial axis:** 0–4 range with severity labels

**Colour:** Blue (`#2563eb`) with translucent fill.

**Empty data handling:** If no industry data is available (all events have `Unknown` industry), a text message is displayed instead of the chart.

### 8.11 Severity by Attack Type (Radar Chart)

**Chart type:** `radar`
**Radial axis:** 0–4 range with severity labels
**Colour:** Green (`#059669`) with translucent fill.

### 8.12 Records Affected by Attack Type (Horizontal Bar Chart)

**Chart type:** `bar` with `indexAxis: 'y'` (horizontal)
**X-axis:** Logarithmic scale
**Colour:** Purple (`#7c3aed`)
**Legend:** Hidden (single dataset)

**Justification:** Horizontal orientation accommodates long attack type labels. The logarithmic X-axis handles the wide range of average records affected across different attack types.

### 8.13 Monthly Counts Histogram (Bar Chart)

**Chart type:** `bar`
**Container height:** 440px (taller than standard to accommodate statistics text below)
**Colour:** Slate grey (`#64748b`) with alpha

**Below the chart:** A text element displays the overdispersion analysis results:
- φ value and interpretation (Poisson / Negative Binomial / Mixed)
- Mean and variance values
- Distribution parameters (λ for Poisson, n and p for Negative Binomial)

**Subtitle:** Displays the overdispersion percentage relative to Poisson: `"Overdispersion: +X% vs Poisson"` or `"Underdispersion: -X% vs Poisson"`.

### 8.14 Database vs OAIC Comparison (Line Chart)

**Chart type:** `line`

**Datasets:**
1. Database Events — blue (`#2563eb`), `pointRadius: 5`
2. OAIC Official Notifications — red (`#ef4444`), `pointRadius: 5`, `spanGaps: false`
3. Estimated Full Period (Pro-rata) — green (`#10b981`), `pointStyle: 'triangle'`, `pointRadius: 8`, shown only on the last partial period

**Custom tooltip footer:** Calculates and displays the ratio `DB/OAIC` when both values are available.

**Summary statistics:** Displayed below the chart as text, including: database period count, OAIC period count, average ratio, and pro-rata estimate details if applicable.

### 8.15 OAIC Cyber Incidents (Line Chart)

**Chart type:** `line`

**Datasets:**
1. Cyber Incidents — red (`#ef4444`), filled area
2. Total Notifications — blue (`#2563eb`), filled area

**Interaction mode:** `mode: 'index', intersect: false` — hovering shows values for both datasets at the same x-position.

### 8.16 OAIC Attack Types (Multi-Line Chart)

**Chart type:** `line`

**Attack type colours:**

| Attack Type | Colour |
|-------------|--------|
| Ransomware | `#dc2626` (red) |
| Phishing | `#ea580c` (orange) |
| Hacking | `#ca8a04` (amber) |
| Malware | `#16a34a` (green) |
| Brute Force | `#0891b2` (teal) |
| Compromised Credentials | `#7c3aed` (purple) |

### 8.17 OAIC Top Sectors (Horizontal Grouped Bar Chart)

**Chart type:** `bar` with `indexAxis: 'y'`

**Datasets:**
1. OAIC Official Count — blue (`#2563eb`)
2. Database Count — green (`#10b981`)

**Dynamic title:** The chart title updates to show the actual number of sectors displayed (e.g., "OAIC Top 8 Affected Sectors").

### 8.18 Database/OAIC Ratio by Sector (Horizontal Bar Chart)

**Chart type:** `bar` with `indexAxis: 'y'`

**Values:** Ratios converted to percentages (`ratio × 100`).

**Bar colours:** Conditional — green (`#10b981`) if coverage ≥ 100%, orange/amber (`#f59e0b`) if < 100%.

**Custom tooltip:** Shows coverage percentage, raw database count, and raw OAIC count.

**X-axis ticks:** Formatted with `%` suffix.

### 8.19 OAIC Individuals Affected (Multi-Line Chart)

**Chart type:** `line`
**Container width:** Full width (`col-lg-12`)
**Y-axis:** Logarithmic scale with compact number formatting (`Intl.NumberFormat` with `notation: 'compact'`)

**Datasets:**
1. OAIC Average Individuals Affected — red, filled
2. OAIC Median Individuals Affected — blue, filled
3. Database Average Records Affected — green, filled

**Custom tooltip:** Large numbers formatted with `Intl.NumberFormat('en-US')`.

### 8.20 ASD Risk Matrices (HTML Tables)

**Rendering function:** `renderRiskMatrix()` (JavaScript)

Three instances are rendered:
1. ASD Risk Matrix (All Years)
2. ASD Risk Matrix (Last Year)
3. ASD Risk Matrix (Current Year)

**Table structure:** Bootstrap-styled table (`table table-sm table-bordered`) with:
- First column: Impact type labels (bold, left-aligned, 260px width)
- Remaining columns: One per stakeholder group
- Cells contain event counts (empty when zero)

**Cell colour:** Blue (`rgba(37, 99, 235, opacity)`) where opacity scales linearly with the cell value:

```
opacity = 0.18 + 0.72 × (value / max_value)
```

- Minimum opacity: 0.18 (lightest cells still visibly tinted)
- Maximum opacity: 0.90 (for the cell with the highest count)
- Text colour: White when `opacity > 0.45`, black otherwise (ensures readability)

**Badge:** Each matrix displays a total classification count badge.

**Container height:** `auto` (not fixed at 380px) to accommodate the full table.

### 8.21 Event Type Correlation Matrix (HTML Heatmap Table)

**Rendering:** JavaScript-generated HTML table (replaces the `<canvas>` element)
**Container height:** 500px

**Cell colour coding:**
- Positive correlations: Blue (`rgba(59, 130, 246, alpha)`), alpha = `0.3 + value × 0.7`
- Negative correlations: Red (`rgba(239, 68, 68, alpha)`), alpha = `0.3 + |value| × 0.7`
- Zero: Grey (`rgba(156, 163, 175, 0.3)`)
- Text colour: White when alpha intensity > 0.5, black otherwise

**Cell tooltips:** `"Type A vs Type B: 0.xxx"` on hover.

**Statistics below:** Event type count, min correlation, max correlation, strongest positive pair, strongest negative pair.

---

## 9. ASD Risk Matrix Framework

The ASD (Australian Signals Directorate) risk matrix classifies events along two dimensions:

### Impact Types (6 levels, from most severe to least severe)

| # | Impact Type |
|---|-------------|
| 1 | Sustained disruption of essential systems and associated services |
| 2 | Extensive compromise |
| 3 | Isolated compromise |
| 4 | Coordinated low-level malicious attack |
| 5 | Low-level malicious attack |
| 6 | Unsuccessful low-level malicious attack |

### Stakeholder Categories (15 individual categories, grouped into 6 display groups)

| Display Group | Individual Categories |
|---------------|----------------------|
| Member(s) of the public | Member(s) of the public |
| Small organisation(s) / Sole traders | Small organisation(s), Sole traders |
| Medium-sized organisation(s) / Schools / Local government | Medium-sized organisation(s), Schools, Local government |
| State government / Academia/R&D / Large organisation(s) / Supply chain | State government, Academia/R&D, Large organisation(s), Supply chain |
| Federal government / Government shared services / Regulated critical infrastructure | Federal government, Government shared services, Regulated critical infrastructure |
| National security / Systems of National Significance | National security, Systems of National Significance |

**Grouping justification:** The 15 individual stakeholder categories are grouped into 6 columns to keep the matrix table readable. The groupings follow the ASD's own framework where categories within a group represent similar levels of national importance and impact scope.

**Cell values:** Each cell in the matrix contains the count of events classified with that specific (impact type, stakeholder group) combination. Higher counts indicate more frequent types of incidents at that intersection.

---

## 10. Colour Schemes and Visual Conventions

### Primary Colour Palette

| Role | Colour | Hex |
|------|--------|-----|
| Primary | Blue | `#2563eb` |
| Secondary / Success | Green | `#10b981` |
| Warning | Amber | `#f59e0b` |
| Danger | Red | `#ef4444` |
| Info | Cyan | `#06b6d4` |

### Severity Colour Mapping

| Severity | Colour | Hex |
|----------|--------|-----|
| Critical | Red | `#dc2626` |
| High | Orange | `#ea580c` |
| Medium | Amber | `#ca8a04` |
| Low | Green | `#16a34a` |
| Unknown | Grey | `#6b7280` |

### Transparency Convention

Semi-transparent fills use a `20` hex suffix (12.5% opacity), e.g., `#2563eb20`. This provides visual weight for filled areas without obscuring overlapping datasets.

---

## 11. Empty Data Handling

When data is absent for a particular chart or section, the dashboard displays a descriptive message rather than rendering a broken or empty chart:

| Condition | Displayed Message |
|-----------|-------------------|
| No OAIC data files found | "No OAIC data available. Run `python oaic_data_scraper.py` first." |
| No industry data (all Unknown) | "No industry data available." + "The enrichment process has not yet populated industry information for events." |
| Insufficient correlation data (<2 event types with 5+ months) | "Insufficient data for correlation analysis. Need at least 2 event types with 5+ months of data." |
| No ASD risk classifications | "No ASD risk classifications available." |
| No OAIC sector data | "No OAIC data available." |
| No OAIC individuals affected data | "No OAIC data available." |

These messages are rendered by replacing the chart's canvas element or container with styled HTML text.

---

## 12. LLM Prompts

The dashboard generation stage does **not** use any LLM prompts. All data displayed in the dashboard is extracted from the database (where it was placed by earlier pipeline phases that did use LLMs) or loaded from pre-scraped OAIC JSON files.

The pipeline phases that use LLM prompts are:

- **Phase 1 (Discovery)** — GPT-4o-mini for event extraction and classification
- **Phase 2 (Enrichment)** — Perplexity AI for entity name extraction and fact-checking
- **Phase 4 (ASD Classification)** — GPT-4o for risk matrix assignment

Dashboard generation (Phase 5) is purely a data retrieval and visualisation step.

---

## 13. Output Files

### Primary Output

| File | Path | Description |
|------|------|-------------|
| `index.html` | `dashboard/index.html` | Self-contained interactive HTML dashboard |

**File characteristics:**
- Single HTML file with embedded CSS, JavaScript, and JSON data
- Approximately 3,000+ lines (varies with data volume)
- Can be opened directly in any modern web browser
- Requires internet connection for Bootstrap CSS and Chart.js JavaScript (CDN-loaded)

### Related Output (from Phase 4)

The ASD risk classification phase (Phase 4) also generates Excel files that present the same risk matrix data in spreadsheet format:

| File | Path | Description |
|------|------|-------------|
| `risk_matrix.xlsx` | `risk_matrix/risk_matrix.xlsx` | All years aggregate |
| `risk_matrix_YYYY.xlsx` | `risk_matrix/risk_matrix_2020.xlsx` ... `risk_matrix_2026.xlsx` | Per-year matrices |

These Excel files are generated by `scripts/asd_risk_classifier.py` using openpyxl, not by the dashboard generation stage. They are mentioned here because the dashboard's ASD risk matrix tables display the same data in HTML form.

---

## 14. Justification of Design Decisions

### Why a Static HTML Dashboard?

| Alternative | Why Rejected |
|-------------|--------------|
| Flask/Django web app | Requires a running server, more complex deployment, unnecessary for a single-user analysis tool |
| Jupyter Notebook | Less portable, requires Python installation to view, harder to share with non-technical stakeholders |
| Tableau/Power BI | Requires commercial licences, introduces vendor dependency |
| PDF report | No interactivity (no hover tooltips, no zooming) |

A static HTML file with Chart.js provides interactivity (tooltips, zoom) with zero infrastructure requirements.

### Why Chart.js?

| Alternative | Why Chart.js Was Preferred |
|-------------|---------------------------|
| D3.js | More powerful but significantly more complex; Chart.js provides sufficient chart types for this use case |
| Plotly | Larger library size; Python integration adds complexity for static file generation |
| Google Charts | Requires internet connection and Google API loading; Chart.js CDN is smaller and faster |
| Matplotlib (rendered as images) | No interactivity; images increase file size; blurry on high-DPI screens |

Chart.js offers a good balance of simplicity, interactivity, small library size, and chart type coverage (line, bar, pie, doughnut, radar).

### Why Bootstrap?

Bootstrap provides a responsive grid system that makes the dashboard readable on both desktop and tablet screens without custom CSS media queries. The `col-lg-6 col-md-12` grid layout displays charts in a 2-column grid on large screens and full-width on medium screens.

### Why Logarithmic Scales?

Records affected values span from single digits to millions. The records affected histogram ([Section 4.7](#47-records-affected-histogram)), monthly records affected trend ([Section 8.3](#83-records-affected-line-chart-log-scale)), and maximum records per month ([Section 8.9](#89-maximum-records-affected-per-month-line-chart)) all use logarithmic Y-axes. On a linear scale, months with large breaches (100K+ records) would compress all smaller-breach months to an indistinguishable line near zero.

### Why Radar Charts for Severity Analysis?

Radar charts ([Section 8.10](#810-severity-by-industry-radar-chart), [Section 8.11](#811-severity-by-attack-type-radar-chart)) display severity across multiple categories on a common 0–4 scale. This makes it easy to identify which industries or attack types consistently produce higher-severity events. The shape of the radar polygon provides an at-a-glance severity profile.

### Why Half-Yearly Periods for OAIC Comparison?

OAIC publishes Notifiable Data Breaches reports on a half-yearly cycle (H1: January–June, H2: July–December). Using the same period boundaries ensures direct comparability and eliminates temporal misalignment that would occur with monthly or quarterly aggregation.

### Why Pro-Rata Estimation?

When the dashboard is generated mid-period (e.g., in March during H1), the current half-year period has incomplete data. Without pro-rata estimation, the chart would show a misleadingly low bar for the current period. The pro-rata extrapolation `(actual_count × 6 / months_elapsed)` provides a simple linear estimate of where the full period is likely to end, displayed as a distinct triangle marker so it is visually distinct from observed data.

---

## 15. Replication Guide

To replicate the dashboard generation process independently:

### Prerequisites

- Python 3.8+ with packages: `numpy`, `scipy`, `pandas`, `json` (stdlib), `sqlite3` (stdlib)
- A populated SQLite database with at minimum: `DeduplicatedEvents`, `EntitiesV2`, `DeduplicatedEventEntities` tables
- (Optional) OAIC JSON data files (`oaic_cyber_statistics_*.json`) in the project root
- (Optional) `ASDRiskClassifications` table for risk matrix display
- Internet connection (for CDN-hosted Bootstrap and Chart.js in the generated HTML)

### Step-by-Step

1. **Set the date range:** `start_date = '2020-01-01'`, `end_date = today's date in YYYY-MM-DD format`.

2. **Load OAIC data:** Glob for `oaic_cyber_statistics_*.json` files. Parse each as JSON. Merge records by `"{year} {period}"` key using the merge strategy in [Section 5.1](#51-loading-oaic-json-files).

3. **Open a SQLite connection** to the database file.

4. **Execute the 15 SQL query functions** from [Section 4](#4-data-extraction-sql-queries), passing `start_date` and `end_date` as parameters. Each function returns a Python dictionary.

5. **Compute statistical transforms:**
   - Overdispersion analysis on monthly counts ([Section 6.1](#61-overdispersion-analysis))
   - Correlation matrix from event type mix data ([Section 6.2](#62-event-type-correlation-matrix))
   - Monthly counts histogram and dispersion estimate ([Section 6.3](#63-monthly-counts-histogram-and-dispersion-estimate))
   - OAIC comparison alignment and pro-rata estimation ([Section 5.2](#52-oaic-vs-database-comparison))

6. **Assemble a data dictionary** with all 23 data structures (see [Section 7.4](#74-data-injection-mechanism) for the complete key list).

7. **Serialise each data structure to JSON** using `json.dumps()`.

8. **Generate the HTML document:** Construct an HTML document with:
   - Bootstrap 5.3.0 CSS (CDN link)
   - Chart.js 4.4.0 (CDN script)
   - Inline CSS as specified in [Section 7.3](#73-css-styling)
   - `<canvas>` elements for each chart (see [Section 8](#8-chart-specifications) for IDs and types)
   - `<div>` containers for ASD risk matrices and correlation matrix
   - A `<script>` block containing the JSON data as JavaScript variables and Chart.js initialisation code per chart specification

9. **Write the HTML** to `dashboard/index.html` (create the directory if needed).

### Verification

After generation, the dashboard should:
- Open in any modern browser (Chrome, Firefox, Edge, Safari)
- Display 21 visualisations (18 Chart.js charts, 3 ASD risk matrix tables) plus 1 correlation heatmap table
- Show no JavaScript console errors
- Respond to hover interactions on all charts (tooltips with detailed data)
- Display OAIC comparison data (if JSON files are present)
- Display ASD risk matrices (if classification data exists in the database)

---

**Source file:** `scripts/build_static_dashboard.py`
**Pipeline integration:** `run_full_pipeline.py` (Phase 5, `run_dashboard_phase()` method)
**Last updated:** March 2026
