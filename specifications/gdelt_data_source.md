# GDELT Data Source Specification for Australian Cyber Events

## Overview

This specification defines the requirements and implementation approach for extracting detailed Australian cyber event data from the Global Database of Events, Language, and Tone (GDELT) Project at gdeltproject.org. The system will focus on cyber security events affecting Australia from January 1, 2020 onwards.

## 1. Project Background

### 1.1 GDELT Project
- Comprehensive global human society database spanning 215 years
- Updates every 15 minutes for GDELT 2.0 data
- Event data structured as (Actor1, Event, Actor2) triples using CAMEO taxonomy
- Supports 65 live translated languages
- Available through multiple access methods: BigQuery, Analysis Service, raw CSV downloads

### 1.2 Data Scope
- **Temporal Range**: January 1, 2020 onwards
- **Geographic Focus**: Australia (country code: AS)
- **Event Type**: Cyber security events (CAMEO codes 172, 210, and related)
- **Update Frequency**: 15-minute intervals for real-time monitoring
- **Languages**: All 65 supported languages with focus on English

## 2. Technical Requirements

### 2.1 Data Access Methods

#### 2.1.1 Primary Method: Google BigQuery
- **Dataset**: `gdelt-bq.gdeltv2.events`
- **Advantages**: Near-instantaneous SQL queries, sophisticated analysis capabilities
- **Authentication**: Google Cloud Service Account with BigQuery access
- **Rate Limits**: Follow Google Cloud quotas and billing limits

#### 2.1.2 Secondary Method: Raw CSV Downloads
- **Source**: GDELT 2.0 Event Database files
- **Format**: Tab-delimited CSV files
- **Update Frequency**: Every 15 minutes
- **File Naming**: `YYYYMMDDHHMMSS.export.CSV.zip`

#### 2.1.3 Tertiary Method: GDELT Analysis Service
- **Purpose**: Data validation and exploration
- **Format**: Multiple export formats available
- **Limitations**: Browser-based, not suitable for automated processing

### 2.2 Data Schema

#### 2.2.1 GDELT 2.0 Event Database Fields
The following fields are required for Australian cyber event extraction:

| Field Name | Data Type | Description | Required |
|------------|-----------|-------------|----------|
| GLOBALEVENTID | String | Unique event identifier | Yes |
| SQLDATE | Integer | Event date (YYYYMMDD format) | Yes |
| MonthYear | Integer | Month/Year (YYYYMM format) | Yes |
| Year | Integer | Year | Yes |
| FractionDate | Float | Fractional date representation | Yes |
| Actor1Code | String | CAMEO actor code for Actor1 | Yes |
| Actor1Name | String | Actor1 name | Yes |
| Actor1CountryCode | String | Actor1 country code | Yes |
| Actor1KnownGroupCode | String | Actor1 known group classification | No |
| Actor1EthnicCode | String | Actor1 ethnic code | No |
| Actor1Religion1Code | String | Actor1 primary religion code | No |
| Actor1Religion2Code | String | Actor1 secondary religion code | No |
| Actor1Type1Code | String | Actor1 primary type code | No |
| Actor1Type2Code | String | Actor1 secondary type code | No |
| Actor1Type3Code | String | Actor1 tertiary type code | No |
| Actor2Code | String | CAMEO actor code for Actor2 | Yes |
| Actor2Name | String | Actor2 name | Yes |
| Actor2CountryCode | String | Actor2 country code | Yes |
| Actor2KnownGroupCode | String | Actor2 known group classification | No |
| Actor2EthnicCode | String | Actor2 ethnic code | No |
| Actor2Religion1Code | String | Actor2 primary religion code | No |
| Actor2Religion2Code | String | Actor2 secondary religion code | No |
| Actor2Type1Code | String | Actor2 primary type code | No |
| Actor2Type2Code | String | Actor2 secondary type code | No |
| Actor2Type3Code | String | Actor2 tertiary type code | No |
| IsRootEvent | Integer | Root event flag (1/0) | Yes |
| EventCode | String | CAMEO event code | Yes |
| EventBaseCode | String | CAMEO base event code | Yes |
| EventRootCode | String | CAMEO root event code | Yes |
| QuadClass | Integer | Event quadrant classification | Yes |
| GoldsteinScale | Float | Goldstein conflict-cooperation score | Yes |
| NumMentions | Integer | Number of source documents | Yes |
| NumSources | Integer | Number of information sources | Yes |
| NumArticles | Integer | Number of source articles | Yes |
| AvgTone | Float | Average tone of coverage | Yes |
| Actor1Geo_Type | Integer | Actor1 geographic resolution | No |
| Actor1Geo_FullName | String | Actor1 full geographic name | No |
| Actor1Geo_CountryCode | String | Actor1 geographic country code | No |
| Actor1Geo_ADM1Code | String | Actor1 administrative division 1 | No |
| Actor1Geo_ADM2Code | String | Actor1 administrative division 2 | No |
| Actor1Geo_Lat | Float | Actor1 latitude | No |
| Actor1Geo_Long | Float | Actor1 longitude | No |
| Actor1Geo_FeatureID | String | Actor1 geographic feature ID | No |
| Actor2Geo_Type | Integer | Actor2 geographic resolution | No |
| Actor2Geo_FullName | String | Actor2 full geographic name | No |
| Actor2Geo_CountryCode | String | Actor2 geographic country code | No |
| Actor2Geo_ADM1Code | String | Actor2 administrative division 1 | No |
| Actor2Geo_ADM2Code | String | Actor2 administrative division 2 | No |
| Actor2Geo_Lat | Float | Actor2 latitude | No |
| Actor2Geo_Long | Float | Actor2 longitude | No |
| Actor2Geo_FeatureID | String | Actor2 geographic feature ID | No |
| ActionGeo_Type | Integer | Action geographic resolution | Yes |
| ActionGeo_FullName | String | Action full geographic name | Yes |
| ActionGeo_CountryCode | String | Action geographic country code | Yes |
| ActionGeo_ADM1Code | String | Action administrative division 1 | No |
| ActionGeo_ADM2Code | String | Action administrative division 2 | No |
| ActionGeo_Lat | Float | Action latitude | Yes |
| ActionGeo_Long | Float | Action longitude | Yes |
| ActionGeo_FeatureID | String | Action geographic feature ID | No |
| DATEADDED | Integer | Date record was added (YYYYMMDD) | Yes |
| SOURCEURL | String | Source URL | Yes |

### 2.3 Filtering Criteria

#### 2.3.1 Geographic Filters
- **Primary**: `ActionGeo_CountryCode = 'AS'` (Events occurring in Australia)
- **Secondary**: `Actor1CountryCode = 'AS' OR Actor2CountryCode = 'AS'` (Events involving Australian actors)

#### 2.3.2 Cyber Event Filters (CAMEO Codes)
- **172**: Coerce cybernetically
- **210**: Use conventional military force
- **Additional Codes**: Events with cyber-related keywords in source articles

#### 2.3.3 Temporal Filters
- **Start Date**: `SQLDATE >= 20200101`
- **End Date**: Current date or specified end date

#### 2.3.4 Quality Filters
- **NumSources >= 2**: Ensure multiple source validation
- **NumArticles >= 1**: At least one source article
- **IsRootEvent = 1**: Focus on root events to avoid duplicates

## 3. Implementation Architecture

### 3.1 System Components

#### 3.1.1 Data Extraction Service
- **Technology**: Python 3.8+
- **Libraries**:
  - `google-cloud-bigquery` for BigQuery access
  - `pandas` for data manipulation
  - `requests` for CSV downloads
  - `schedule` for periodic updates
- **Configuration**: Environment variables for credentials and parameters

#### 3.1.2 Data Processing Pipeline
- **Input Validation**: Schema validation and data quality checks
- **Transformation**: Normalize and enrich cyber event data
- **Deduplication**: Remove duplicate events based on GLOBALEVENTID
- **Classification**: Enhanced cyber event categorization

#### 3.1.3 Data Storage
- **Primary Storage**: PostgreSQL database
- **Backup Storage**: JSON/CSV exports
- **Indexing**: Geographic and temporal indices for performance

#### 3.1.4 Monitoring and Alerting
- **Health Checks**: Service availability monitoring
- **Data Quality**: Anomaly detection for data patterns
- **Error Handling**: Comprehensive logging and error recovery

### 3.2 Data Models

#### 3.2.1 CyberEvent Entity
```python
class CyberEvent:
    global_event_id: str
    event_date: date
    event_code: str
    event_description: str
    actor1_name: str
    actor1_country: str
    actor2_name: str
    actor2_country: str
    action_location: str
    action_country: str
    action_coordinates: Tuple[float, float]
    goldstein_score: float
    num_mentions: int
    num_sources: int
    avg_tone: float
    source_urls: List[str]
    created_at: datetime
    updated_at: datetime
```

### 3.3 API Queries

#### 3.3.1 BigQuery SQL Template
```sql
SELECT
    GLOBALEVENTID,
    SQLDATE,
    EventCode,
    EventBaseCode,
    EventRootCode,
    Actor1Name,
    Actor1CountryCode,
    Actor2Name,
    Actor2CountryCode,
    ActionGeo_CountryCode,
    ActionGeo_FullName,
    ActionGeo_Lat,
    ActionGeo_Long,
    GoldsteinScale,
    NumMentions,
    NumSources,
    NumArticles,
    AvgTone,
    SOURCEURL,
    DATEADDED
FROM `gdelt-bq.gdeltv2.events`
WHERE
    SQLDATE >= 20200101
    AND (ActionGeo_CountryCode = 'AS'
         OR Actor1CountryCode = 'AS'
         OR Actor2CountryCode = 'AS')
    AND (EventCode LIKE '172%'
         OR EventCode LIKE '210%'
         OR EventCode IN ('172', '210'))
    AND NumSources >= 2
    AND IsRootEvent = 1
ORDER BY SQLDATE DESC, DATEADDED DESC
```

## 4. Data Processing Requirements

### 4.1 Real-time Processing
- **Frequency**: Every 15 minutes
- **Batch Size**: 1000 records per batch
- **Timeout**: 30 seconds per BigQuery request
- **Retry Logic**: Exponential backoff with maximum 3 retries

### 4.2 Historical Data Backfill
- **Approach**: Monthly batches from January 2020
- **Rate Limiting**: Maximum 100 requests per hour
- **Progress Tracking**: Checkpoint mechanism for resume capability
- **Validation**: Cross-reference with existing data

### 4.3 Data Quality Assurance
- **Schema Validation**: Enforce required field presence
- **Geographic Validation**: Verify Australian location data
- **Duplicate Detection**: GLOBALEVENTID uniqueness
- **Completeness Checks**: Minimum required fields validation

### 4.4 Error Handling
- **Network Errors**: Retry with exponential backoff
- **API Quota Errors**: Implement rate limiting and queuing
- **Data Format Errors**: Log and skip malformed records
- **Authentication Errors**: Refresh credentials automatically

## 5. Security and Compliance

### 5.1 Authentication
- **Google Cloud**: Service Account with minimal required permissions
- **API Keys**: Secure storage in environment variables or key vault
- **Access Control**: Role-based access to data and systems

### 5.2 Data Privacy
- **PII Handling**: No collection of personally identifiable information
- **Source Attribution**: Maintain source URL references
- **Data Retention**: Configurable retention policies

### 5.3 Rate Limiting
- **BigQuery**: Respect Google Cloud quotas
- **CSV Downloads**: Maximum 4 requests per minute
- **Graceful Degradation**: Fallback to alternative methods

## 6. Deployment and Infrastructure

### 6.1 Environment Requirements
- **Python**: 3.8 or higher
- **Memory**: Minimum 2GB RAM
- **Storage**: 50GB for historical data
- **Network**: Reliable internet connection

### 6.2 Configuration Management
- **Environment Variables**:
  - `GOOGLE_APPLICATION_CREDENTIALS`: Service account key path
  - `GDELT_START_DATE`: Data extraction start date
  - `GDELT_END_DATE`: Data extraction end date (optional)
  - `GDELT_UPDATE_FREQUENCY`: Update interval in minutes
  - `DATABASE_URL`: Database connection string
  - `LOG_LEVEL`: Logging verbosity level

### 6.3 Monitoring
- **Metrics**: Data volume, processing time, error rates
- **Alerts**: System failures, data quality issues
- **Dashboards**: Real-time monitoring and historical trends

## 7. Testing Strategy

### 7.1 Unit Tests
- **Data Validation**: Schema validation functions
- **API Integration**: Mock BigQuery responses
- **Processing Logic**: Transformation and filtering logic

### 7.2 Integration Tests
- **BigQuery Connectivity**: Live API connection tests
- **Database Operations**: CRUD operation validation
- **End-to-End**: Complete data pipeline testing

### 7.3 Performance Tests
- **Load Testing**: High-volume data processing
- **Stress Testing**: System behavior under extreme loads
- **Scalability Testing**: Horizontal and vertical scaling

## 8. Documentation Requirements

### 8.1 Technical Documentation
- **API Documentation**: BigQuery schema and queries
- **Setup Guide**: Installation and configuration instructions
- **Troubleshooting Guide**: Common issues and solutions

### 8.2 User Documentation
- **Usage Examples**: Common query patterns
- **Data Dictionary**: Field definitions and examples
- **Best Practices**: Recommended implementation approaches

## 9. Success Criteria

### 9.1 Data Quality
- **Completeness**: 95% of available cyber events captured
- **Accuracy**: 99% correct geographic and temporal classification
- **Freshness**: Data updated within 30 minutes of GDELT updates

### 9.2 System Performance
- **Availability**: 99.5% uptime
- **Latency**: Average response time under 5 seconds
- **Throughput**: Process 10,000 events per hour

### 9.3 Coverage Metrics
- **Geographic**: All Australian states and territories represented
- **Temporal**: No gaps in daily coverage from January 1, 2020
- **Source Diversity**: Minimum 50 unique source domains

## 10. Future Enhancements

### 10.1 Advanced Analytics
- **Trend Analysis**: Temporal pattern identification
- **Sentiment Analysis**: Enhanced tone analysis beyond AvgTone
- **Network Analysis**: Actor relationship mapping

### 10.2 Real-time Alerting
- **Critical Event Detection**: Immediate notification system
- **Threshold Monitoring**: Automated anomaly detection
- **Integration**: Connection with external security systems

### 10.3 Data Enrichment
- **External Sources**: Cross-reference with other cyber threat databases
- **Machine Learning**: Automated event classification improvement
- **Geospatial Analysis**: Enhanced location-based insights

## Appendices

### Appendix A: CAMEO Code Reference
- Complete list of cyber-related CAMEO codes
- Event type classifications and descriptions
- Actor type codes relevant to cyber events

### Appendix B: Australian Geographic Codes
- State and territory codes
- Major city identifiers
- Administrative division mappings

### Appendix C: Sample Data Records
- Representative cyber event records
- Data format examples
- Edge case scenarios

### Appendix D: Error Codes and Messages
- System error classifications
- Recommended actions for each error type
- Escalation procedures