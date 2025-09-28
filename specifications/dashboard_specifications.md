# Australian Cyber Events Dashboard Specifications

## Overview
This document specifies the design and requirements for an HTML business intelligence dashboard that visualizes cyber security events data from the Australian Cyber Events database. The dashboard provides insights into trends, patterns, and distributions of cyber security incidents affecting Australian organizations.

## Technical Architecture

### Frontend Stack
- **Framework**: HTML5 with JavaScript
- **Charting Library**: Chart.js or D3.js for interactive visualizations
- **Styling**: CSS3 with responsive design (Bootstrap or similar)
- **Data Fetching**: REST API endpoints for real-time data

### Backend API Requirements
- **Endpoint Base**: `/api/v1/dashboard/`
- **Response Format**: JSON
- **Date Range Filtering**: Support for custom date ranges via query parameters
- **Caching**: Implement caching for performance optimization

## Dashboard Layout

### Header Section
- **Title**: "Australian Cyber Security Events Dashboard"
- **Date Range Selector**: Dropdown/picker for custom date ranges
- **Last Updated**: Timestamp of last data refresh
- **Export Options**: PDF/CSV export buttons

### Main Content Grid (2x3 Layout)
```
┌─────────────────┬─────────────────┐
│   Trend Chart   │   Severity      │
│   (Events/Mo)   │   Trends        │
├─────────────────┼─────────────────┤
│   Records       │   Event Type    │
│   Affected      │   Mix Trends    │
├─────────────────┼─────────────────┤
│   Entity Types  │   Records       │
│   Distribution  │   Histogram     │
└─────────────────┴─────────────────┘
```

## Visualization Specifications

### 1. Monthly Trends in Unique Event Count

**Chart Type**: Line Chart with Area Fill
**API Endpoint**: `GET /api/v1/dashboard/monthly-event-counts?start_date=YYYY-MM&end_date=YYYY-MM`

**Data Source Query**:
```sql
SELECT
    DATE_FORMAT(event_date, '%Y-%m') as month,
    COUNT(DISTINCT deduplicated_event_id) as unique_events
FROM DeduplicatedEvents
WHERE status = 'Active'
    AND event_date >= ?
    AND event_date <= ?
GROUP BY DATE_FORMAT(event_date, '%Y-%m')
ORDER BY month;
```

**Visual Properties**:
- **X-Axis**: Month (YYYY-MM format)
- **Y-Axis**: Number of unique events
- **Colors**: Primary blue (#2563eb) with light blue fill
- **Interactivity**: Hover tooltips showing exact counts
- **Trend Line**: Optional moving average overlay

**Features**:
- Year-over-year comparison toggle
- Drill-down capability to view events for specific months

### 2. Monthly Trends in Severity

**Chart Type**: Stacked Area Chart
**API Endpoint**: `GET /api/v1/dashboard/monthly-severity-trends?start_date=YYYY-MM&end_date=YYYY-MM`

**Data Source Query**:
```sql
SELECT
    DATE_FORMAT(event_date, '%Y-%m') as month,
    severity,
    COUNT(DISTINCT deduplicated_event_id) as event_count
FROM DeduplicatedEvents
WHERE status = 'Active'
    AND event_date >= ?
    AND event_date <= ?
GROUP BY DATE_FORMAT(event_date, '%Y-%m'), severity
ORDER BY month,
    CASE severity
        WHEN 'Critical' THEN 1
        WHEN 'High' THEN 2
        WHEN 'Medium' THEN 3
        WHEN 'Low' THEN 4
        WHEN 'Unknown' THEN 5
    END;
```

**Visual Properties**:
- **X-Axis**: Month (YYYY-MM format)
- **Y-Axis**: Number of events (stacked)
- **Colors**:
  - Critical: #dc2626 (red)
  - High: #ea580c (orange)
  - Medium: #ca8a04 (yellow)
  - Low: #16a34a (green)
  - Unknown: #6b7280 (gray)
- **Legend**: Interactive legend with toggle capability

**Features**:
- Toggle between absolute numbers and percentages
- Severity trend indicators (arrows showing increase/decrease)

### 3. Monthly Trends in Average Number of Records Affected

**Chart Type**: Line Chart with Confidence Intervals
**API Endpoint**: `GET /api/v1/dashboard/monthly-records-affected?start_date=YYYY-MM&end_date=YYYY-MM`

**Data Source Query**:
```sql
SELECT
    DATE_FORMAT(event_date, '%Y-%m') as month,
    AVG(records_affected) as avg_records,
    STDDEV(records_affected) as std_records,
    COUNT(DISTINCT deduplicated_event_id) as sample_size,
    MIN(records_affected) as min_records,
    MAX(records_affected) as max_records
FROM DeduplicatedEvents
WHERE status = 'Active'
    AND event_date >= ?
    AND event_date <= ?
    AND records_affected IS NOT NULL
    AND records_affected > 0
GROUP BY DATE_FORMAT(event_date, '%Y-%m')
ORDER BY month;
```

**Visual Properties**:
- **X-Axis**: Month (YYYY-MM format)
- **Y-Axis**: Average records affected (logarithmic scale option)
- **Colors**: Primary green (#16a34a) with confidence band in light green
- **Error Bars**: Standard deviation or confidence intervals
- **Secondary Line**: Median values for comparison

**Features**:
- Toggle between arithmetic and logarithmic Y-axis
- Outlier detection and highlighting
- Sample size indicator in tooltips

### 4. Monthly Trends in Mix of Events by Event Type

**Chart Type**: Stacked Bar Chart with Percentage Option
**API Endpoint**: `GET /api/v1/dashboard/monthly-event-type-mix?start_date=YYYY-MM&end_date=YYYY-MM`

**Data Source Query**:
```sql
SELECT
    DATE_FORMAT(event_date, '%Y-%m') as month,
    event_type,
    COUNT(DISTINCT deduplicated_event_id) as event_count
FROM DeduplicatedEvents
WHERE status = 'Active'
    AND event_date >= ?
    AND event_date <= ?
GROUP BY DATE_FORMAT(event_date, '%Y-%m'), event_type
ORDER BY month, event_type;
```

**Visual Properties**:
- **X-Axis**: Month (YYYY-MM format)
- **Y-Axis**: Number of events (absolute or percentage)
- **Colors**: Distinct colors for each event type
  - Ransomware: #dc2626
  - Data Breach: #2563eb
  - Phishing: #7c3aed
  - Malware: #ea580c
  - Other types: Additional distinct colors
- **Legend**: Sortable by frequency

**Features**:
- Toggle between absolute counts and percentages
- Filter to show/hide specific event types
- Trend indicators for each event type

### 5. Overall Mix of Entity Types

**Chart Type**: Donut Chart with Detailed Breakdown
**API Endpoint**: `GET /api/v1/dashboard/entity-type-distribution?start_date=YYYY-MM&end_date=YYYY-MM`

**Data Source Query**:
```sql
SELECT
    e.entity_type,
    COUNT(DISTINCT de.deduplicated_event_id) as linked_events,
    COUNT(DISTINCT e.entity_id) as unique_entities
FROM EntitiesV2 e
INNER JOIN DeduplicatedEventEntities dee ON e.entity_id = dee.entity_id
INNER JOIN DeduplicatedEvents de ON dee.deduplicated_event_id = de.deduplicated_event_id
WHERE de.status = 'Active'
    AND de.event_date >= ?
    AND de.event_date <= ?
GROUP BY e.entity_type
ORDER BY linked_events DESC;
```

**Visual Properties**:
- **Chart Type**: Donut chart with center statistics
- **Colors**: Professional color palette with good contrast
- **Center Display**: Total entities count and events linked
- **Labels**: Percentage and absolute counts

**Features**:
- Hover effects with detailed statistics
- Click to drill down to entity list
- Option to switch between events linked vs unique entities

### 6. Overall Histogram of Number of Records Affected

**Chart Type**: Histogram with Log-Normal Overlay
**API Endpoint**: `GET /api/v1/dashboard/records-affected-histogram?start_date=YYYY-MM&end_date=YYYY-MM`

**Data Source Query**:
```sql
SELECT
    records_affected,
    event_type,
    severity,
    COUNT(*) as frequency
FROM DeduplicatedEvents
WHERE status = 'Active'
    AND event_date >= ?
    AND event_date <= ?
    AND records_affected IS NOT NULL
    AND records_affected > 0
GROUP BY records_affected, event_type, severity
ORDER BY records_affected;
```

**Visual Properties**:
- **X-Axis**: Records affected (logarithmic bins)
- **Y-Axis**: Frequency (number of events)
- **Bins**: Logarithmic scaling (1-10, 11-100, 101-1K, 1K-10K, etc.)
- **Colors**: Gradient from light to dark blue
- **Overlay**: Statistical distribution curve

**Features**:
- Configurable bin sizes
- Filter by event type and severity
- Statistical summary (mean, median, mode, percentiles)
- Outlier identification

## Responsive Design Requirements

### Desktop (1200px+)
- Full 2x3 grid layout
- Large charts with detailed legends
- Comprehensive tooltips

### Tablet (768px - 1199px)
- 1x6 vertical layout
- Medium-sized charts
- Collapsible legends

### Mobile (< 768px)
- Single column layout
- Simplified charts
- Swipe navigation between charts

## Performance Requirements

### Loading Times
- Initial page load: < 3 seconds
- Chart updates: < 1 second
- Data refresh: < 5 seconds

### Data Optimization
- Implement data caching with 15-minute expiry
- Use data aggregation for large datasets
- Progressive loading for detailed views

## Interactive Features

### Global Filters
- **Date Range Picker**: Custom start and end dates
- **Severity Filter**: Multi-select dropdown
- **Event Type Filter**: Multi-select dropdown
- **Entity Type Filter**: Multi-select dropdown

### Chart Interactions
- **Zoom**: Mouse wheel zoom on time-series charts
- **Pan**: Click and drag on charts
- **Drill Down**: Click chart elements to view details
- **Cross-Filtering**: Selecting data in one chart filters others

### Export Features
- **PNG/SVG**: Individual chart export
- **PDF**: Full dashboard export
- **CSV**: Raw data export for each chart
- **Share Links**: Generate shareable URLs with current filters

## API Endpoints Specification

### Base URL
```
/api/v1/dashboard/
```

### Common Query Parameters
- `start_date`: YYYY-MM-DD format
- `end_date`: YYYY-MM-DD format
- `severity[]`: Array of severity levels
- `event_type[]`: Array of event types
- `entity_type[]`: Array of entity types

### Endpoint Details

1. **Monthly Event Counts**
   ```
   GET /monthly-event-counts
   Response: { "months": ["2023-01", "2023-02"], "counts": [15, 23] }
   ```

2. **Monthly Severity Trends**
   ```
   GET /monthly-severity-trends
   Response: { "months": ["2023-01"], "data": [{"severity": "High", "count": 10}] }
   ```

3. **Monthly Records Affected**
   ```
   GET /monthly-records-affected
   Response: { "months": ["2023-01"], "averages": [1250], "confidence": [[1100, 1400]] }
   ```

4. **Monthly Event Type Mix**
   ```
   GET /monthly-event-type-mix
   Response: { "months": ["2023-01"], "types": {"Ransomware": [5], "Data Breach": [8]} }
   ```

5. **Entity Type Distribution**
   ```
   GET /entity-type-distribution
   Response: { "types": [{"type": "Financial", "events": 45, "entities": 12}] }
   ```

6. **Records Affected Histogram**
   ```
   GET /records-affected-histogram
   Response: { "bins": ["1-10", "11-100"], "frequencies": [25, 15] }
   ```

## Security Considerations

### Data Protection
- No personally identifiable information in charts
- Aggregate data only
- Secure API authentication

### Access Control
- Role-based access (view-only, admin)
- IP whitelist for sensitive data
- Rate limiting on API endpoints

## Accessibility Requirements

### WCAG 2.1 AA Compliance
- Color contrast ratios ≥ 4.5:1
- Keyboard navigation support
- Screen reader compatibility
- Alternative text for all visualizations

### Color Accessibility
- Colorblind-friendly palettes
- Pattern/texture alternatives to color coding
- High contrast mode option

## Testing Requirements

### Browser Compatibility
- Chrome 90+
- Firefox 88+
- Safari 14+
- Edge 90+

### Performance Testing
- Load testing with 1000+ concurrent users
- Data volume testing with 50K+ events
- Mobile performance testing

## Deployment Specifications

### Environment Requirements
- **Development**: Local development with sample data
- **Staging**: Production-like environment for testing
- **Production**: High-availability setup with monitoring

### Monitoring
- Application performance monitoring
- Error tracking and alerting
- User analytics and usage patterns

## Future Enhancements

### Phase 2 Features
- Real-time data streaming
- Predictive analytics
- Custom alert configuration
- Advanced filtering and search

### Integration Possibilities
- Export to BI tools (Tableau, Power BI)
- API for third-party integrations
- Automated report generation
- Mobile application

---

**Document Version**: 1.0
**Last Updated**: 2024-09-28
**Reviewed By**: [To be filled]
**Approved By**: [To be filled]