# Australian Cyber Events Dashboard - Usage Instructions

## Overview

The Australian Cyber Events Dashboard is a web-based business intelligence interface that provides comprehensive insights into cyber security incidents affecting Australian organizations. This document provides detailed instructions on how to use all dashboard features effectively.

## Getting Started

### Prerequisites

1. **Database**: Ensure the cyber events database is populated with data
2. **Python Dependencies**: Flask and Flask-CORS installed
3. **Web Browser**: Modern browser (Chrome 90+, Firefox 88+, Safari 14+, Edge 90+)

### Starting the Dashboard

```bash
# Basic startup
python generate_dashboard.py

# Custom configuration
python generate_dashboard.py --port 8080 --debug --db-path /path/to/cyber_events.db
```

### Accessing the Dashboard

Open your web browser and navigate to:
- **Default**: `http://localhost:5000`
- **Custom port**: `http://localhost:[PORT]`

## Dashboard Layout

### Header Section
- **Title**: "Australian Cyber Security Events Dashboard"
- **Last Updated**: Shows when data was last refreshed
- **Export Buttons**: PDF and CSV export options

### Control Panel
- **Date Range Selector**: Filter data by custom date ranges
- **Refresh Data Button**: Manually refresh all charts
- **Reset Filters Button**: Clear all applied filters

### Charts Grid (2x3 Layout)
```
┌─────────────────┬─────────────────┐
│   Event Trends  │   Severity      │
│   (Monthly)     │   Trends        │
├─────────────────┼─────────────────┤
│   Records       │   Event Type    │
│   Affected      │   Mix           │
├─────────────────┼─────────────────┤
│   Entity Types  │   Records       │
│   Distribution  │   Histogram     │
└─────────────────┴─────────────────┘
```

## Chart-by-Chart Usage Guide

### 1. Monthly Trends in Unique Event Count

**Purpose**: Shows the volume of unique cyber events over time

**Chart Type**: Line chart with area fill

**How to Read**:
- **X-Axis**: Months in YYYY-MM format
- **Y-Axis**: Number of unique events
- **Blue line**: Trend of event counts
- **Light blue fill**: Visual emphasis of the trend area

**Interactive Features**:
- **Hover**: Shows exact event count for each month
- **Tooltip**: Displays total events across all months

**Key Insights**:
- Identify seasonal patterns in cyber incidents
- Spot sudden spikes or drops in activity
- Track overall trend direction (increasing/decreasing)

### 2. Monthly Trends in Severity

**Purpose**: Displays the distribution of event severity levels over time

**Chart Type**: Stacked bar chart

**How to Read**:
- **X-Axis**: Months in YYYY-MM format
- **Y-Axis**: Number of events (stacked by severity)
- **Color Coding**:
  - 🔴 **Critical**: Red (#dc2626)
  - 🟠 **High**: Orange (#ea580c)
  - 🟡 **Medium**: Yellow (#ca8a04)
  - 🟢 **Low**: Green (#16a34a)
  - ⚪ **Unknown**: Gray (#6b7280)

**Interactive Features**:
- **Legend**: Click to toggle severity levels on/off
- **Hover**: Shows exact count for each severity level
- **Stacked View**: See total events and breakdown simultaneously

**Key Insights**:
- Monitor changes in threat severity over time
- Identify periods of high-severity incident clusters
- Track improvement or deterioration in security posture

### 3. Monthly Trends in Average Records Affected

**Purpose**: Shows the scale of impact in terms of records compromised

**Chart Type**: Line chart with logarithmic scale

**How to Read**:
- **X-Axis**: Months in YYYY-MM format
- **Y-Axis**: Average records affected (logarithmic scale)
- **Green line**: Average impact per month
- **Tooltip**: Includes sample size information

**Interactive Features**:
- **Logarithmic Scale**: Better visualization of wide-ranging values
- **Hover**: Shows average and sample size
- **Trend Analysis**: Identify impact magnitude changes

**Key Insights**:
- Assess the increasing or decreasing scale of breaches
- Identify months with particularly large-scale incidents
- Understand the typical scope of cyber incidents

### 4. Monthly Trends in Event Type Mix

**Purpose**: Shows the composition of different types of cyber events

**Chart Type**: Stacked bar chart

**How to Read**:
- **X-Axis**: Months in YYYY-MM format
- **Y-Axis**: Number of events (stacked by type)
- **Color Coding**:
  - 🔴 **Ransomware**: Red
  - 🔵 **Data Breach**: Blue
  - 🟣 **Phishing**: Purple
  - 🟠 **Malware**: Orange
  - 🟢 **Other**: Green

**Interactive Features**:
- **Legend**: Toggle event types on/off
- **Stacked View**: See proportional changes over time
- **Hover**: Exact counts for each event type

**Key Insights**:
- Track evolution of threat landscape
- Identify emerging or declining attack methods
- Plan targeted security measures based on trends

### 5. Overall Mix of Entity Types

**Purpose**: Shows which types of organizations are most affected

**Chart Type**: Donut chart

**How to Read**:
- **Segments**: Each represents a different entity type
- **Size**: Proportional to number of linked events
- **Colors**: Distinct colors for each entity type
- **Center**: Total statistics

**Interactive Features**:
- **Hover**: Shows detailed statistics (events + entities)
- **Legend**: Lists all entity types
- **Click**: Future drill-down capability

**Key Insights**:
- Identify most targeted industry sectors
- Understand distribution of cyber risk
- Plan sector-specific security initiatives

### 6. Records Affected Histogram

**Purpose**: Shows the distribution of incident sizes by records affected

**Chart Type**: Histogram with logarithmic bins

**How to Read**:
- **X-Axis**: Ranges of records affected (1-10, 11-100, etc.)
- **Y-Axis**: Number of events (frequency)
- **Bars**: Height indicates how many events fall in each range

**Interactive Features**:
- **Hover**: Shows exact frequency for each bin
- **Logarithmic Bins**: Better visualization of wide range

**Key Insights**:
- Understand typical breach sizes
- Identify most common impact scales
- Plan incident response based on likely scenarios

## Navigation and Controls

### Date Range Filtering

1. **Click** the date range input field
2. **Select** start date from calendar
3. **Select** end date from calendar
4. **Charts automatically update** with filtered data
5. **Clear filter** using "Reset Filters" button

### Data Refresh

- **Manual Refresh**: Click "🔄 Refresh Data" button
- **Automatic**: Charts update when date filters change
- **Loading Indicators**: Show during data loading
- **Error Messages**: Display if data loading fails

### Responsive Behavior

#### Desktop (1200px+)
- Full 2x3 grid layout
- Large charts with detailed legends
- Complete tooltips and interactions

#### Tablet (768px - 1199px)
- Single column (1x6) vertical layout
- Medium-sized charts
- Maintained interactivity

#### Mobile (<768px)
- Single column layout
- Simplified charts optimized for touch
- Essential information preserved

## Export Features

### PDF Export
1. Click "📊 Export PDF" button
2. Dashboard generates comprehensive PDF report
3. Includes all current charts and date range
4. Suitable for presentations and reports

### CSV Export
1. Click "📋 Export CSV" button
2. Downloads aggregated data from all charts
3. Suitable for further analysis in Excel/other tools
4. Includes metadata about date ranges and filters

## Accessibility Features

### Keyboard Navigation
- **Tab**: Navigate between interactive elements
- **Enter/Space**: Activate charts and controls
- **Arrow Keys**: Navigate within charts (where supported)

### Screen Reader Support
- **Chart Labels**: Descriptive aria-labels for all charts
- **Live Announcements**: Status updates announced automatically
- **Alternative Text**: Charts include alternative descriptions

### Visual Accessibility
- **High Contrast**: Automatic support for high contrast mode
- **Color Blind Friendly**: Color palettes designed for accessibility
- **Large Text**: Scalable fonts that work with browser zoom
- **Focus Indicators**: Clear visual focus for keyboard users

## Troubleshooting

### Common Issues

#### Charts Not Loading
- **Check Console**: Press F12 and look for error messages
- **Database Connection**: Ensure database file exists and is accessible
- **Network Issues**: Check if API endpoints are responding

#### Data Appears Incomplete
- **Date Range**: Verify date filter includes expected time period
- **Database Content**: Ensure database has been populated with events
- **Filters**: Check if any filters are accidentally applied

#### Performance Issues
- **Large Date Ranges**: Try shorter time periods for better performance
- **Browser Memory**: Refresh page if charts become sluggish
- **Database Size**: Large databases may require optimization

#### Mobile Display Issues
- **Orientation**: Try both portrait and landscape modes
- **Browser**: Ensure you're using a supported mobile browser
- **Zoom Level**: Reset browser zoom to default

### Getting Help

1. **Check Browser Console**: Press F12 for detailed error messages
2. **Verify Database**: Ensure all required tables exist and contain data
3. **Test API Endpoints**: Visit endpoints directly (e.g., `/api/v1/dashboard/monthly-event-counts`)
4. **Review Logs**: Check server console for backend errors

## Advanced Usage Tips

### Analyzing Trends
1. **Use multiple time ranges** to identify short-term vs long-term patterns
2. **Compare severity trends** with event volume for context
3. **Cross-reference entity types** with event types for sector analysis

### Creating Reports
1. **Set relevant date range** before exporting
2. **Take screenshots** of specific insights for presentations
3. **Export raw data** for detailed statistical analysis
4. **Document key findings** with specific time periods

### Performance Optimization
1. **Use shorter date ranges** for faster loading
2. **Refresh data regularly** to ensure accuracy
3. **Close unused browser tabs** to free memory
4. **Clear browser cache** if experiencing issues

## Security Considerations

### Data Access
- Dashboard shows **aggregated data only**
- **No personal information** is displayed
- Access should be **restricted to authorized users**

### Network Security
- Use **HTTPS in production** environments
- Implement **proper authentication** for sensitive deployments
- Consider **IP whitelisting** for restricted access

## Version Information

- **Dashboard Version**: 1.0
- **Last Updated**: 2024-09-28
- **Compatible Browsers**: Chrome 90+, Firefox 88+, Safari 14+, Edge 90+
- **Required Dependencies**: Flask, Flask-CORS, Chart.js, Bootstrap 5

---

For technical support or feature requests, please refer to the project documentation or contact the development team.