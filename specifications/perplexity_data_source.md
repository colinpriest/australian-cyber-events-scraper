# Perplexity Data Source Specification for Australian Cyber Events

## Overview

This specification defines the requirements and implementation approach for extracting detailed Australian cyber event data from the Perplexity Search API. The system will leverage Perplexity's real-time web search capabilities with advanced filtering to identify, extract, and process cyber security events affecting Australia from January 1, 2020 onwards.

## 1. Project Background

### 1.1 Perplexity API Overview
- Real-time web search with AI-powered summarization and citation
- Advanced filtering capabilities including date ranges, domains, and academic sources
- OpenAI-compatible API for easy integration
- Streaming response support for real-time processing
- Rate-limited access with usage tier management

### 1.2 Data Scope
- **Temporal Range**: January 1, 2020 onwards
- **Geographic Focus**: Australia and Australian entities
- **Event Type**: Cyber security incidents, breaches, attacks, and related activities
- **Update Frequency**: Daily searches for new events
- **Languages**: Primary focus on English content with multi-language support

## 2. Technical Requirements

### 2.1 API Access Configuration

#### 2.1.1 Authentication
- **Method**: Bearer token authentication
- **Header**: `Authorization: Bearer {API_KEY}`
- **Key Management**: Secure environment variable storage
- **Billing**: Credit-based system requiring valid payment method

#### 2.1.2 Base Endpoint
- **Primary Endpoint**: `https://api.perplexity.ai/chat/completions`
- **Protocol**: HTTPS REST API
- **Compatibility**: OpenAI SDK compatible

#### 2.1.3 Rate Limits
- **Standard Tier**: 50 requests per minute
- **Pro Tier**: Higher limits available
- **Credits**: $5 monthly for Pro subscribers
- **Retry Strategy**: Exponential backoff for 429 errors

### 2.2 Search Models and Capabilities

#### 2.2.1 Available Models
- **sonar-pro**: Flagship model with advanced search capabilities
- **sonar-medium**: Balanced performance and cost
- **sonar-small**: Efficient for simple queries
- **Online Models**: Real-time web search integration

#### 2.2.2 Search Features
- Real-time web search results with citations
- Domain-specific filtering
- Academic source filtering
- Date range filtering
- Image and video result support
- Structured output options

### 2.3 Date Filtering Parameters

#### 2.3.1 Publication Date Filters
- **search_after_date_filter**: Content published after specified date
- **search_before_date_filter**: Content published before specified date
- **Format**: "%m/%d/%Y" (e.g., "1/1/2020")

#### 2.3.2 Last Updated Filters
- **last_updated_after_filter**: Content updated after specified date
- **last_updated_before_filter**: Content updated before specified date
- **Format**: "%m/%d/%Y" (e.g., "3/1/2025")

#### 2.3.3 Recency Filter
- **search_recency_filter**: Predefined time periods
- **Values**: "day", "week", "month", "year"

## 3. Implementation Architecture

### 3.1 System Components

#### 3.1.1 Query Generation Service
- **Technology**: Python 3.8+
- **Libraries**:
  - `openai` for API communication
  - `requests` for HTTP requests
  - `schedule` for periodic execution
  - `pydantic` for data validation
- **Functionality**: Generate targeted search queries for cyber events

#### 3.1.2 Data Extraction Pipeline
- **Search Orchestration**: Manage multiple concurrent queries
- **Response Processing**: Parse and extract relevant information
- **Citation Management**: Track and validate source URLs
- **Deduplication**: Identify and merge duplicate events

#### 3.1.3 Event Classification System
- **NLP Processing**: Extract event details from search results
- **Classification**: Categorize event types and severity
- **Geographic Validation**: Confirm Australian relevance
- **Temporal Analysis**: Extract event timing and duration

#### 3.1.4 Data Storage and Management
- **Primary Storage**: PostgreSQL database
- **Cache Layer**: Redis for API response caching
- **File Storage**: Raw response data in JSON format
- **Backup**: Daily incremental backups

### 3.2 Data Models

#### 3.2.1 CyberEvent Entity
```python
class CyberEvent:
    event_id: str
    event_date: date
    discovery_date: date
    title: str
    description: str
    event_type: str
    severity_level: str
    affected_entity: str
    entity_type: str
    location: str
    coordinates: Optional[Tuple[float, float]]
    industry_sector: str
    impact_assessment: str
    response_actions: List[str]
    attribution: Optional[str]
    source_urls: List[str]
    confidence_score: float
    created_at: datetime
    updated_at: datetime
```

#### 3.2.2 SearchQuery Entity
```python
class SearchQuery:
    query_id: str
    query_text: str
    search_parameters: dict
    execution_date: date
    results_count: int
    execution_time: float
    model_used: str
    cost_credits: float
    status: str
```

#### 3.2.3 Source Citation Entity
```python
class SourceCitation:
    citation_id: str
    event_id: str
    url: str
    title: str
    publication_date: date
    domain: str
    relevance_score: float
    content_snippet: str
    verified: bool
```

### 3.3 Search Query Templates

#### 3.3.1 Primary Cyber Event Queries
```python
CYBER_EVENT_QUERIES = [
    "Australian cyber attack {date_range} data breach security incident",
    "Australia cybersecurity breach {date_range} ransomware malware",
    "Australian company cyber incident {date_range} hacking data leak",
    "Australia government cyber attack {date_range} infrastructure security",
    "Australian bank financial cyber breach {date_range} fraud security",
    "Australia healthcare cyber attack {date_range} medical data breach",
    "Australian university cyber incident {date_range} education security"
]
```

#### 3.3.2 Entity-Specific Queries
```python
ENTITY_QUERIES = [
    "Commonwealth Bank Australia cyber breach {date_range}",
    "Telstra cyber attack {date_range} security incident",
    "Australian Department of Defence cyber {date_range}",
    "Services Australia cyber breach {date_range}",
    "Australian Taxation Office cyber attack {date_range}"
]
```

#### 3.3.3 Sector-Specific Queries
```python
SECTOR_QUERIES = [
    "Australian mining cyber attack {date_range} Rio Tinto BHP",
    "Australia energy cyber security {date_range} power grid utility",
    "Australian telecommunications cyber {date_range} network security",
    "Australia retail cyber breach {date_range} customer data"
]
```

### 3.4 API Request Configuration

#### 3.4.1 Request Structure
```python
request_payload = {
    "model": "sonar-pro",
    "messages": [
        {
            "role": "system",
            "content": "You are a cybersecurity analyst. Provide detailed, factual information about cyber security incidents in Australia with specific dates, entities involved, and impact assessments. Include source citations."
        },
        {
            "role": "user",
            "content": query_text
        }
    ],
    "search_after_date_filter": "1/1/2020",
    "search_before_date_filter": current_date,
    "search_recency_filter": "day",
    "temperature": 0.1,
    "max_tokens": 2000,
    "stream": False
}
```

## 4. Data Processing Requirements

### 4.1 Query Execution Strategy

#### 4.1.1 Temporal Segmentation
- **Historical Backfill**: Monthly chunks from January 2020
- **Recent Events**: Daily queries for last 30 days
- **Real-time Monitoring**: Weekly queries for emerging threats
- **Date Format**: Consistent "%m/%d/%Y" formatting

#### 4.1.2 Query Rotation
- **Primary Queries**: 20 core cyber event search terms
- **Entity Queries**: 50 specific Australian organizations
- **Sector Queries**: 15 industry-focused searches
- **Rotation Schedule**: Distribute queries across rate limits

#### 4.1.3 Response Processing
- **Content Extraction**: Parse AI-generated summaries
- **Citation Validation**: Verify and categorize source URLs
- **Fact Checking**: Cross-reference multiple sources
- **Confidence Scoring**: Assess information reliability

### 4.2 Data Quality Assurance

#### 4.2.1 Geographic Validation
- **Entity Verification**: Confirm Australian connection
- **Location Extraction**: Parse geographic references
- **Address Normalization**: Standardize location formats
- **Coordinate Lookup**: Geocode locations when possible

#### 4.2.2 Temporal Validation
- **Date Parsing**: Extract event dates from content
- **Timeline Verification**: Validate chronological consistency
- **Duration Estimation**: Calculate incident timeframes
- **Discovery vs Occurrence**: Distinguish between dates

#### 4.2.3 Content Verification
- **Source Credibility**: Assess source reliability
- **Fact Consistency**: Check across multiple sources
- **Duplicate Detection**: Identify redundant information
- **Completeness Check**: Ensure required fields populated

### 4.3 Error Handling and Resilience

#### 4.3.1 API Error Management
- **Rate Limit Handling**: Implement exponential backoff
- **Authentication Errors**: Automatic token refresh
- **Network Timeouts**: Retry with progressive delays
- **Quota Exhaustion**: Queue management and prioritization

#### 4.3.2 Data Processing Errors
- **Parse Failures**: Log and continue processing
- **Validation Errors**: Flag for manual review
- **Classification Errors**: Use fallback categories
- **Storage Failures**: Implement transaction rollback

## 5. Security and Compliance

### 5.1 API Security
- **Key Protection**: Secure environment variable storage
- **Request Encryption**: HTTPS-only communications
- **Audit Logging**: Complete request/response logging
- **Access Control**: Role-based API key management

### 5.2 Data Privacy
- **PII Handling**: Avoid collecting personal information
- **Source Attribution**: Maintain citation integrity
- **Data Retention**: Configurable retention policies
- **Anonymization**: Remove identifying details when required

### 5.3 Compliance Requirements
- **Attribution**: Proper source citation for all data
- **Rate Limiting**: Respect API terms of service
- **Data Usage**: Comply with content licensing terms
- **Storage Security**: Encrypted data at rest

## 6. Configuration Management

### 6.1 Environment Variables
```bash
# API Configuration
PERPLEXITY_API_KEY=your_api_key_here
PERPLEXITY_BASE_URL=https://api.perplexity.ai
PERPLEXITY_MODEL=sonar-pro

# Search Configuration
SEARCH_START_DATE=1/1/2020
SEARCH_END_DATE=auto
SEARCH_FREQUENCY=daily
MAX_QUERIES_PER_HOUR=45

# Database Configuration
DATABASE_URL=postgresql://user:pass@host:port/db
REDIS_URL=redis://host:port/db

# Processing Configuration
MAX_CONCURRENT_REQUESTS=5
REQUEST_TIMEOUT=30
RETRY_ATTEMPTS=3
BACKOFF_FACTOR=2

# Logging Configuration
LOG_LEVEL=INFO
LOG_FILE_PATH=/var/log/perplexity_scraper.log
```

### 6.2 Query Configuration
```json
{
  "search_parameters": {
    "temperature": 0.1,
    "max_tokens": 2000,
    "stream": false,
    "search_recency_filter": "day"
  },
  "query_categories": {
    "general_cyber": ["cyber attack", "data breach", "ransomware"],
    "sector_specific": ["banking", "healthcare", "government"],
    "threat_types": ["malware", "phishing", "ddos"]
  },
  "australian_entities": [
    "Commonwealth Bank", "Westpac", "ANZ", "NAB",
    "Telstra", "Optus", "Vodafone",
    "Woolworths", "Coles", "JB Hi-Fi"
  ]
}
```

## 7. Monitoring and Alerting

### 7.1 Performance Metrics
- **API Response Time**: Average and 95th percentile
- **Success Rate**: Percentage of successful requests
- **Query Coverage**: Completion rate for scheduled queries
- **Data Quality Score**: Percentage of validated events

### 7.2 Cost Monitoring
- **Credit Usage**: Daily and monthly consumption tracking
- **Cost per Event**: Calculate extraction efficiency
- **Budget Alerts**: Notify when approaching limits
- **Usage Optimization**: Identify high-cost, low-value queries

### 7.3 Data Quality Alerts
- **Low Confidence Events**: Flag events below threshold
- **Source Validation Failures**: Identify unreliable sources
- **Duplicate Detection**: Alert on high duplication rates
- **Coverage Gaps**: Identify missing temporal periods

## 8. Testing Strategy

### 8.1 Unit Tests
- **Query Generation**: Validate search query construction
- **Response Parsing**: Test data extraction logic
- **Date Filtering**: Verify temporal parameter handling
- **Error Handling**: Test failure scenarios

### 8.2 Integration Tests
- **API Connectivity**: Live API endpoint testing
- **Database Operations**: CRUD functionality validation
- **End-to-End Pipeline**: Complete workflow testing
- **Rate Limit Handling**: Throttling mechanism validation

### 8.3 Data Quality Tests
- **Geographic Accuracy**: Verify Australian relevance
- **Temporal Consistency**: Check date range compliance
- **Citation Validity**: Verify source URL accessibility
- **Content Relevance**: Assess cyber event classification

## 9. Deployment Architecture

### 9.1 Infrastructure Requirements
- **Compute**: 4 CPU cores, 8GB RAM minimum
- **Storage**: 100GB for data and logs
- **Network**: Reliable internet with failover
- **Operating System**: Linux (Ubuntu 20.04+)

### 9.2 Container Configuration
```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
EXPOSE 8000

CMD ["python", "main.py"]
```

### 9.3 Orchestration
- **Container Platform**: Docker with Docker Compose
- **Scheduling**: Cron-based query execution
- **Scaling**: Horizontal scaling for high-volume periods
- **Load Balancing**: Round-robin request distribution

## 10. Success Criteria

### 10.1 Data Coverage
- **Temporal Completeness**: 95% coverage from January 2020
- **Event Capture Rate**: 90% of publicly reported incidents
- **Geographic Accuracy**: 99% Australian relevance
- **Source Diversity**: Minimum 100 unique domains

### 10.2 System Performance
- **Availability**: 99% uptime
- **Response Time**: Average 5 seconds per query
- **Throughput**: 500 queries per day
- **Error Rate**: Less than 5% failed requests

### 10.3 Data Quality
- **Accuracy**: 95% verified event details
- **Completeness**: 90% of events with full metadata
- **Timeliness**: Events detected within 48 hours
- **Uniqueness**: Less than 5% duplicate events

## 11. Cost Optimization

### 11.1 Query Efficiency
- **Smart Caching**: Cache results for 24 hours
- **Query Deduplication**: Avoid redundant searches
- **Targeted Searches**: Focus on high-value queries
- **Batch Processing**: Group similar queries

### 11.2 Credit Management
- **Budget Allocation**: Monthly spending limits
- **Priority Queuing**: High-value queries first
- **Cost Tracking**: Per-query cost analysis
- **Usage Optimization**: Eliminate low-value searches

## 12. Future Enhancements

### 12.1 Advanced Analytics
- **Trend Analysis**: Identify emerging threat patterns
- **Predictive Modeling**: Forecast potential incidents
- **Network Analysis**: Map attack relationships
- **Sentiment Analysis**: Assess public impact

### 12.2 Integration Capabilities
- **SIEM Integration**: Real-time alert forwarding
- **Threat Intelligence**: Enrich with external feeds
- **Visualization**: Interactive dashboards
- **API Endpoints**: Provide data access APIs

### 12.3 Machine Learning Enhancements
- **Auto-Classification**: Improve event categorization
- **Relevance Scoring**: Enhance content filtering
- **Anomaly Detection**: Identify unusual patterns
- **Natural Language Processing**: Better content extraction

## Appendices

### Appendix A: Search Query Examples
- Complete list of cyber event search patterns
- Entity-specific query templates
- Sector-focused search strategies
- Geographic refinement queries

### Appendix B: Response Format Examples
- Sample API responses with annotations
- Citation format specifications
- Error response handling examples
- Data extraction patterns

### Appendix C: Error Codes and Handling
- API error code reference
- Recommended retry strategies
- Escalation procedures
- Recovery mechanisms

### Appendix D: Performance Benchmarks
- Expected response times by query type
- Throughput metrics and scaling factors
- Cost analysis by search category
- Quality metrics and thresholds