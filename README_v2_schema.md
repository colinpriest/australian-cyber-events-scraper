# Australian Cyber Events Database V2 - Separated Raw/Enriched Schema

This document describes the new Version 2 database schema that separates raw event discovery from enriched event processing, along with the new `discover_enrich_events.py` pipeline.

## Overview

The V2 system addresses key limitations of the original mixed schema by:

- **Separating raw and enriched data** - Raw events from sources are stored separately from processed/validated events
- **Adding processing flags** - Track which events have been processed to avoid duplicate work
- **Intelligent filtering** - Only enrich events that are Australian-specific cyber incidents
- **GDELT URL scraping** - Extract full content from GDELT URLs to create meaningful event details
- **Comprehensive pipeline** - Single script combining discovery, scraping, and enrichment

## Database Schema V2

### Core Tables

#### RawEvents
Stores all discovered events before processing:
```sql
CREATE TABLE RawEvents (
    raw_event_id TEXT PRIMARY KEY,
    source_type VARCHAR(50) NOT NULL,     -- 'GDELT', 'Perplexity', etc.
    source_event_id TEXT,                 -- External ID from source
    raw_title TEXT,
    raw_description TEXT,
    raw_content TEXT,                     -- Scraped content
    event_date DATE,
    source_url VARCHAR(2048),
    source_metadata TEXT,                 -- JSON metadata
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_processed BOOLEAN DEFAULT FALSE,
    processing_attempted_at TIMESTAMP NULL,
    processing_error TEXT NULL
);
```

#### EnrichedEvents
Stores processed and validated Australian cyber events:
```sql
CREATE TABLE EnrichedEvents (
    enriched_event_id TEXT PRIMARY KEY,
    raw_event_id TEXT NOT NULL,           -- Links to RawEvents
    title VARCHAR(255) NOT NULL,
    description TEXT,
    summary TEXT,                         -- LLM-generated summary
    event_type VARCHAR(50),
    severity VARCHAR(20),
    event_date DATE NOT NULL,
    is_australian_event BOOLEAN NOT NULL,
    is_specific_event BOOLEAN NOT NULL,
    confidence_score REAL,
    australian_relevance_score REAL,
    status VARCHAR(20) DEFAULT 'Active',
    FOREIGN KEY (raw_event_id) REFERENCES RawEvents(raw_event_id)
);
```

#### ProcessingLog
Tracks processing attempts and results:
```sql
CREATE TABLE ProcessingLog (
    log_id TEXT PRIMARY KEY,
    raw_event_id TEXT NOT NULL,
    processing_stage VARCHAR(50) NOT NULL, -- 'url_scraping', 'llm_analysis', etc.
    status VARCHAR(20) NOT NULL,           -- 'success', 'failed', 'skipped'
    result_data TEXT,                      -- JSON result data
    error_message TEXT,
    processing_time_ms INTEGER,
    FOREIGN KEY (raw_event_id) REFERENCES RawEvents(raw_event_id)
);
```

### Enhanced Tables

- **EntitiesV2** - Enhanced entity management with Australian flags and confidence scores
- **EnrichedEventEntities** - Links enriched events to entities with relationship types
- **DataSourcesV2** - Enhanced data source tracking with credibility scores
- **AuditLog** - Track all changes for accountability

## Migration Process

### 1. Run Database Migration

```bash
python database_migration_v2.py
```

This script will:
- ✅ Create backup of existing database
- ✅ Create V2 schema tables
- ✅ Migrate existing data to new structure
- ✅ Rename old tables with `_v1_backup` suffix
- ✅ Insert default data sources

### 2. Verify Migration

```bash
python test_new_schema.py
```

## New Pipeline: discover_enrich_events.py

The new pipeline combines all event processing into a single comprehensive script.

### Key Features

1. **Multi-source Discovery** - Collects from GDELT, Perplexity, Google Search, Webber Insurance
2. **URL Scraping** - Extracts full content from event URLs (crucial for GDELT)
3. **LLM Analysis** - Uses GPT-4o-mini to validate Australian relevance and event specificity
4. **Intelligent Filtering** - Only enriches events that are:
   - Australian-related (companies, locations, or .au domains)
   - Specific cyber incidents (not general news or reports)
5. **Processing Tracking** - Prevents duplicate processing with detailed logs
6. **Error Handling** - Comprehensive error tracking and recovery

### Usage Examples

#### Full Pipeline (Recommended)
```bash
# Discover, scrape, and enrich events from all sources
python discover_enrich_events.py
```

#### Discovery Only
```bash
# Discover new events from all sources (last 7 days, max 100 each)
python discover_enrich_events.py --discover --max-events 100

# Discover from specific source with extended date range
python discover_enrich_events.py --discover --source GDELT --days 14 --max-events 200
```

#### Scraping Only
```bash
# Scrape URLs from discovered events to get full content
python discover_enrich_events.py --scrape --max-events 50
```

#### Enrichment Only
```bash
# Analyze and enrich raw events using LLM
python discover_enrich_events.py --enrich --max-events 30
```

#### Source-Specific Processing
```bash
# Process only GDELT events (all phases)
python discover_enrich_events.py --source GDELT --max-events 150

# Process multiple specific sources
python discover_enrich_events.py --source GDELT --source Perplexity --max-events 100
```

### Pipeline Phases

#### Phase 1: Discovery
- Connects to configured data sources (GDELT BigQuery, Perplexity API, etc.)
- Searches for cyber security events in specified date range
- Stores all discovered events as **RawEvents** regardless of quality
- No filtering at this stage - capture everything

#### Phase 2: URL Scraping
- Identifies raw events with URLs that haven't been scraped
- Uses Selenium to extract full webpage content
- **Critical for GDELT events** which often only have titles from BigQuery
- Updates `raw_content` field with scraped text
- Logs scraping attempts and results

#### Phase 3: LLM Analysis & Enrichment
- Identifies raw events ready for processing (preferring Australian keywords)
- Combines title, description, and scraped content for LLM analysis
- Uses GPT-4o-mini to determine:
  - `is_australian_event` - Related to Australia/Australian entities
  - `is_specific_event` - Actual incident vs general news/reports
  - `primary_entity` - Main affected organization
  - `affected_entities` - Other impacted parties
  - `summary` - Concise incident description
- **Only creates EnrichedEvents** for Australian-specific cyber incidents
- Links entities with relationship types and confidence scores

### Configuration

The pipeline uses existing environment variables:
```bash
# Required for GDELT BigQuery
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_APPLICATION_CREDENTIALS=path/to/bigquery_token.json

# Required for Perplexity
PERPLEXITY_API_KEY=your-perplexity-key

# Required for Google Custom Search
GOOGLE_CUSTOMSEARCH_API_KEY=your-google-api-key
GOOGLE_CUSTOMSEARCH_CX_KEY=your-search-engine-id

# Required for LLM analysis
OPENAI_API_KEY=your-openai-key
```

## Filtering Logic

### Australian Event Detection
Events are considered Australian if they have:
- Australia/Australian in title or description
- .au domain in source URL
- Australian company names or locations
- References to Australian government/infrastructure

### Specific Event Detection
Events are considered specific cyber incidents if they describe:
- Actual data breaches with named victims
- Confirmed cyber attacks on organizations
- Specific ransomware or malware incidents
- Infrastructure compromises with identified targets

Events are **excluded** if they are:
- General industry reports or trends
- Security warnings or advisories
- Opinion pieces or analysis
- Vendor announcements or marketing

## Database Operations

### CyberEventDataV2 Class

The new `CyberEventDataV2` class provides methods for:

```python
# Raw event operations
db.add_raw_event(source_type, raw_data)
db.get_unprocessed_raw_events(source_types, limit)
db.get_raw_events_for_processing(australian_only, limit)
db.mark_raw_event_processed(raw_event_id)

# Enriched event operations
db.create_enriched_event(raw_event_id, enriched_data)
db.get_enriched_events(australian_only, specific_only, limit)

# Processing tracking
db.log_processing_attempt(raw_event_id, stage, status, result_data)

# Statistics and reporting
db.get_summary_statistics()
db.get_processing_queue_status()
```

### Backward Compatibility

The original `CyberEventData` class remains functional for existing scripts like `test_data_sources.py`. A `get_summary_statistics()` method has been added for compatibility.

## Key Benefits

### 1. **No Data Loss**
- All discovered events preserved as raw events
- Failed processing attempts logged with error details
- Original data always available for reprocessing

### 2. **Intelligent Processing**
- Only Australian cyber incidents get enriched
- Avoids wasting resources on irrelevant events
- LLM analysis ensures high-quality results

### 3. **GDELT Enhancement**
- Full content extraction from GDELT URLs
- Creates meaningful titles and descriptions
- Transforms generic GDELT entries into rich event records

### 4. **Scalable Architecture**
- Separate processing stages allow targeted optimization
- Failed events can be reprocessed without losing progress
- Processing queue status provides operational visibility

### 5. **Comprehensive Tracking**
- Detailed logs of all processing attempts
- Performance metrics for optimization
- Clear audit trail for data quality

## Monitoring and Maintenance

### Check Processing Status
```bash
python -c "
from cyber_event_data_v2 import CyberEventDataV2
with CyberEventDataV2() as db:
    stats = db.get_processing_queue_status()
    print(f'Events needing scraping: {stats[\"needs_scraping\"]}')
    print(f'Events ready for analysis: {stats[\"needs_analysis\"]}')
"
```

### View Statistics
```bash
python -c "
from cyber_event_data_v2 import CyberEventDataV2
with CyberEventDataV2() as db:
    stats = db.get_summary_statistics()
    print(f'Raw events: {stats[\"raw_events_total\"]}')
    print(f'Enriched Australian events: {stats[\"enriched_events_australian\"]}')
"
```

### Clear Processing Flags (if needed)
```sql
-- Reset processing flags to reprocess events
UPDATE RawEvents SET is_processed = FALSE WHERE processing_error IS NOT NULL;
```

## Troubleshooting

### Common Issues

#### "V2 schema not found"
**Solution**: Run the migration script first
```bash
python database_migration_v2.py
```

#### "No events ready for enrichment"
**Causes**:
- All raw events already processed
- No events match Australian filtering criteria
- URL scraping not completed

**Solutions**:
```bash
# Check raw events status
python discover_enrich_events.py --scrape --max-events 50

# Disable Australian filtering temporarily
python discover_enrich_events.py --enrich --no-australian-filter --max-events 10
```

#### LLM Analysis Failures
**Common causes**:
- OpenAI API key issues
- Rate limiting
- Insufficient content for analysis

**Check logs**: `discover_enrich_events.log`

### Recovery Procedures

#### Reprocess Failed Events
```python
from cyber_event_data_v2 import CyberEventDataV2

with CyberEventDataV2() as db:
    # Reset failed processing attempts
    cursor = db._conn.cursor()
    cursor.execute("UPDATE RawEvents SET is_processed = FALSE WHERE processing_error IS NOT NULL")
    db._conn.commit()
```

#### Emergency Data Recovery
Original V1 data is preserved in `*_v1_backup` tables and can be restored if needed.

## Migration Checklist

- [ ] Backup current database
- [ ] Run `python database_migration_v2.py`
- [ ] Verify with `python test_new_schema.py`
- [ ] Test discovery: `python discover_enrich_events.py --discover --max-events 10`
- [ ] Test scraping: `python discover_enrich_events.py --scrape --max-events 5`
- [ ] Test enrichment: `python discover_enrich_events.py --enrich --max-events 5`
- [ ] Run full pipeline: `python discover_enrich_events.py --max-events 50`
- [ ] Verify enriched events in database
- [ ] Update any custom scripts to use V2 classes if needed

The new V2 system provides a robust, scalable foundation for Australian cyber event intelligence with clear separation of concerns and comprehensive processing tracking.