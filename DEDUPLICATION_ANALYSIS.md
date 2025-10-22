# Australian Cyber Events Scraper - Deduplication Implementation Analysis

## 1. CURRENT DEDUPLICATION LOGIC

### Overview
The deduplication system uses a sophisticated multi-stage approach implemented in `DeduplicationEngine` that identifies and merges similar events while preserving source information.

### Key Components

#### 1.1 Similarity Threshold & Tolerance
- **Similarity Threshold**: 0.8 (80% match required)
- **Date Tolerance**: 30 days (1 month tolerance for event date differences)
- **Location**: `/cyber_data_collector/processing/deduplication.py` lines 14-17

#### 1.2 Deduplication Algorithm (3-Stage Approach)

**Stage 1: Entity-Based Grouping** (`_group_similar_events`, lines 63-84)
- Groups events by similarity before detailed comparison
- Tracks processed indices to avoid duplicate comparisons
- Returns list of event groups for merging

**Stage 2: Entity Similarity Checking** (`_are_events_similar`, lines 86-131)
- **CRITICAL GATE**: Entity name matching is the hard gate
- If entities differ by >20% similarity (< 0.8 threshold), events are rejected as different
- Special handling for identical titles - assumes entity match (line 112-114)
- Entity extraction from titles using 17+ regex patterns (lines 133-199)
- Normalized entity comparison to handle common suffixes:
  - Removes: 'group', 'company', 'corp', 'corporation', 'inc', 'incorporated', 'ltd', 'limited', 'llc', 'pty', 'bank', 'insurance'
  - Handles subset matches (e.g., 'Toll' vs 'Toll Group') with 0.95 similarity score (line 233)

**Stage 3: Content-Based Similarity** (`_check_cyber_event_similarity`, lines 389-517)
- Uses weighted scoring combining:
  - Title similarity (0.3 weight in standard mode, 0.2 with strong indicators)
  - Description similarity (0.2 weight)
  - Key terms similarity (0.4 weight)
  - Event type matching (0.1 weight)
  - Date proximity factor (multiplier)
  - Strong incident indicators (lines 519-588)

### Enhanced Similarity Detection Features

#### Truncation Detection (lines 404-414)
- Detects when one title is a truncated version of another
- Boosts similarity to 0.9 if >70% of longer title is in shorter

#### Strong Incident Indicators (lines 519-588)
- Specific platform/system matches (contact centre, third-party platform, etc.)
- Same date mentions in content
- Common data types compromised (email, phone, frequent flyer, etc.)
- Detection method similarities
- Threat actor/attack method matches (scattered spider, phishing, MFA bombing, etc.)

#### Generic Summary Matching (lines 248-307)
- Detects when both events are generic summaries of the same period/type
- Uses 14+ generic patterns (months, years, OAIC reports, COVID-themed, phishing campaigns)
- Requires 3+ common terms for match

#### Incident Update Detection (lines 348-387)
- Detects when events are updates to same incident with revised impact numbers
- Looks for customer count differences with common incident indicators
- Reasonable update range: 2x to 50x customer count ratio
- Boosts similarity to 0.9 for incident updates

#### Different Incidents Detection (lines 309-346)
- Detects genuinely different incidents for same company
- Requires >10x difference in customer count + different attack methods AND different timeframes
- Returns False (different events) to prevent merging

### Threshold Decisions

**With Strong Indicators** (>=0.8 score):
- Weighted similarity formula emphasizes key terms and indicators
- Threshold: 0.6 (more lenient)

**Without Strong Indicators**:
- Standard weighted similarity formula
- Threshold: 0.7 (stricter)

**Borderline Cases** (0.5 <= similarity < threshold):
- LLM arbiter used for final decision (OpenAI GPT-3.5-turbo)
- Conservative approach: "if not clearly the same incident, answer NO"

---

## 2. DATA STRUCTURES

### 2.1 CyberEvent Model
**Location**: `/cyber_data_collector/models/events.py` lines 106-142

```python
class CyberEvent(BaseModel):
    event_id: str                              # UUID
    enriched_event_id: Optional[str]           # Database ID if enriched
    external_ids: Dict[str, str]               # External system IDs
    title: str                                 # Event title/headline
    description: str                           # Detailed description
    contributing_raw_events: int = 1           # **NEW FIELD** - # raw events merged
    contributing_enriched_events: int = 1      # **NEW FIELD** - # enriched events merged
    event_type: CyberEventType                 # Enum: Ransomware, Data Breach, etc.
    secondary_types: List[CyberEventType]      # Secondary categories
    severity: EventSeverity                    # Critical, High, Medium, Low
    event_date: Optional[datetime]             # When event occurred
    discovery_date: Optional[datetime]         # When discovered
    publication_date: Optional[datetime]       # When first reported
    last_updated: datetime                     # Last update timestamp
    primary_entity: Optional[AffectedEntity]   # Primary victim
    affected_entities: List[AffectedEntity]    # All affected entities
    location: Optional[str]                    # Event location
    coordinates: Optional[Tuple[float, float]] # Lat/long
    australian_relevance: bool                 # Australian relevance flag
    financial_impact: Optional[FinancialImpact]
    technical_details: Optional[Dict[str, Any]]
    response_actions: List[str]
    attribution: Optional[str]
    data_sources: List[EventSource]
    confidence: ConfidenceScore
    created_at: datetime
    processed_at: Optional[datetime]
    duplicate_of: Optional[str]                # ID of master if duplicate
    merged_events: List[str]                   # IDs of merged events
```

### 2.2 RawEvents Table
**Location**: `/database_migration_v2.py` lines 39-54

```sql
CREATE TABLE RawEvents (
    raw_event_id TEXT PRIMARY KEY,
    source_type VARCHAR(50) NOT NULL,         -- 'GDELT', 'Perplexity', 'GoogleSearch', 'WebberInsurance'
    source_event_id TEXT,                     -- External ID
    raw_title TEXT,
    raw_description TEXT,
    raw_content TEXT,                         -- Full scraped content
    event_date DATE,                          -- Event date from source
    source_url VARCHAR(2048),
    source_metadata TEXT,                     -- JSON metadata
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_processed BOOLEAN DEFAULT FALSE,
    processing_attempted_at TIMESTAMP NULL,
    processing_error TEXT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 2.3 EnrichedEvents Table
**Location**: `/database_migration_v2.py` lines 57-75

```sql
CREATE TABLE EnrichedEvents (
    enriched_event_id TEXT PRIMARY KEY,
    raw_event_id TEXT NOT NULL,               -- FK to RawEvents
    title VARCHAR(255) NOT NULL,
    description TEXT,
    summary TEXT,                             -- LLM-generated
    event_type VARCHAR(50),
    severity VARCHAR(20),
    event_date DATE,                          -- Can be NULL
    records_affected BIGINT,
    is_australian_event BOOLEAN NOT NULL,
    is_specific_event BOOLEAN NOT NULL,
    confidence_score REAL,
    australian_relevance_score REAL,
    status VARCHAR(20) DEFAULT 'Active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- Added columns from add_threat_actor_column.py:
    attacking_entity_id INTEGER,              -- **NEW FIELD**
    attacking_entity_name VARCHAR(255),       -- **NEW FIELD**
    attack_method VARCHAR(100),               -- **NEW FIELD**
    FOREIGN KEY (raw_event_id) REFERENCES RawEvents(raw_event_id) ON DELETE CASCADE
);
```

### 2.4 DeduplicatedEvents Table
**Location**: `/add_deduplication_schema.py` lines 31-61

```sql
CREATE TABLE DeduplicatedEvents (
    deduplicated_event_id TEXT PRIMARY KEY,
    master_enriched_event_id TEXT NOT NULL,   -- Best enriched event
    title VARCHAR(255) NOT NULL,
    description TEXT,
    summary TEXT,
    event_type VARCHAR(50),
    severity VARCHAR(20),
    event_date DATE,                          -- Chosen date
    records_affected BIGINT,
    is_australian_event BOOLEAN NOT NULL,
    is_specific_event BOOLEAN NOT NULL,
    confidence_score REAL,
    australian_relevance_score REAL,
    
    -- Aggregated deduplication data:
    total_data_sources INTEGER DEFAULT 0,     -- Contributing sources
    contributing_raw_events INTEGER DEFAULT 0, -- **NEW FIELD** - raw events merged
    contributing_enriched_events INTEGER DEFAULT 0, -- **NEW FIELD** - enriched events merged
    
    -- Deduplication metadata:
    similarity_score REAL,                    -- Avg similarity
    deduplication_method VARCHAR(50),         -- 'title_similarity', 'entity_match', 'manual'
    
    -- Added columns from add_threat_actor_column.py:
    attacking_entity_id INTEGER,              -- **NEW FIELD**
    attacking_entity_name VARCHAR(255),       -- **NEW FIELD**
    attack_method VARCHAR(100),               -- **NEW FIELD**
    
    status VARCHAR(20) DEFAULT 'Active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    FOREIGN KEY (master_enriched_event_id) REFERENCES EnrichedEvents(enriched_event_id) ON DELETE CASCADE
);
```

### 2.5 Support Tables

**EventDeduplicationMap** (lines 64-78)
- Maps raw/enriched events to deduplicated counterparts
- Tracks contribution type: 'primary', 'supporting', 'duplicate'
- Records similarity_score for each mapping
- Allows weighted importance via data_source_weight

**DeduplicationClusters** (lines 81-90)
- Tracks groups of merged events
- Records cluster_size and average_similarity
- Algorithm version tracking

**DeduplicatedEventEntities** (lines 93-103)
- Links deduplicated events to affected entities
- Tracks source_count (how many sources mentioned each entity)

**DeduplicatedEventSources** (lines 106-116)
- Consolidates data sources at deduplicated event level
- Each source has credibility_score and relevance_score

---

## 3. DATE ASSIGNMENT LOGIC

### 3.1 Date Extraction
**Location**: `/llm_extractor.py` lines 23, 58

LLM-based extraction via ExtractedEventDetails:
```python
event_date: Optional[date] = Field(
    None, 
    description="The actual date when the cyber event occurred (not when it was reported). 
                 Extract from article text if available, format as YYYY-MM-DD."
)
```

Key instruction to LLM:
- Extract actual incident date, NOT publication date
- Look for phrases like 'in June 2025', 'last month', 'on June 5th'
- Format as YYYY-MM-DD

### 3.2 Date Assignment During Raw Event Creation
**Location**: `/discover_enrich_events.py` lines 487-496

Three-tiered fallback approach:

1. **Use extracted event_date** (lines 550-551)
   - If available from LLM extraction, use directly

2. **Fall back to publication_date** (lines 558-559)
   - If event_date unavailable, try source publication date

3. **Fall back to processing month's 1st day** (lines 565-566)
   - If no other date, use: `date(processing_year, processing_month, 1)`
   - This is the DEFAULT for events without dates

### 3.3 Date Selection During Merge
**Location**: `/cyber_data_collector/processing/deduplication.py` lines 670-706

Strategy: **Prefer earliest specific date, avoid 1st of month**

```python
def _merge_event_group(self, events: List[CyberEvent]) -> CyberEvent:
    # Separate dates into two categories:
    specific_dates = [date for date in all_dates if date.day != 1]  # Not 1st of month
    fallback_dates = [date for date in all_dates if date.day == 1]   # 1st of month
    
    if specific_dates:
        best_date = min(specific_dates)  # Earliest specific date
    elif fallback_dates:
        best_date = min(fallback_dates)   # Earliest fallback date
    else:
        best_date = all_dates[0]          # First available
```

**Rationale**: 
- Dates on 1st of month are often placeholders when actual date unknown
- Earliest specific date is likely most accurate actual incident date
- Dates with confidence > 0.8 in title matching are preferred

### 3.4 Date Tolerance in Deduplication
**Location**: `/cyber_data_collector/processing/deduplication.py` lines 628-644

Date proximity factor scales based on difference:
- Same date (0 days): 1.0 multiplier
- Within week (≤7 days): 0.95 multiplier
- Within month (≤30 days): 0.85 multiplier
- Within 3 months (≤90 days): 0.70 multiplier
- Beyond 3 months: 0.50 multiplier
- Missing dates: 0.80 multiplier (neutral, doesn't penalize)

Special handling for identical titles:
- Boosts date_factor to ≥0.95 (line 461)
- Rationale: identical titles should not be penalized for date mismatches

---

## 4. PERPLEXITY INTEGRATION

### 4.1 Perplexity API Configuration
**Location**: `/cyber_data_collector/datasources/perplexity.py`

**API Details**:
- Base URL: `https://api.perplexity.ai`
- Model: `sonar-pro` (lines 223)
- Auth: API key via `PERPLEXITY_API_KEY` environment variable

### 4.2 Search Query Generation
**Location**: Lines 133-148

Seven base queries targeting different sectors:
1. Australian cyber attack data breach security incident
2. Australia cybersecurity breach ransomware malware
3. Australian company cyber incident hacking data leak
4. Australia government cyber attack infrastructure security
5. Australian bank financial cyber breach fraud security
6. Australia healthcare cyber attack medical data breach
7. Australian university cyber incident education security

Date range insertion: `after:MM/DD/YYYY before:MM/DD/YYYY` format

### 4.3 Response Parsing
**Location**: Lines 198-254

System prompt directs Perplexity to return JSON with structure:
```json
{
    "events": [
        {
            "title": "Event title",
            "description": "Description",
            "event_date": "YYYY-MM-DD or null",
            "entity_name": "Affected entity or null",
            "event_type": "Type or null",
            "impact_description": "Impact or null",
            "source_urls": ["url1", "url2"]
        }
    ]
}
```

### 4.4 Event Conversion
**Location**: Lines 256-292

Converts PerplexityEvent to CyberEvent:
- Credibility scores: 0.6 source reliability, 0.7 relevance
- Overall confidence: 0.65 (lowest confidence of all sources)
- Event type inference (lines 294-305):
  - "ransomware" → CyberEventType.RANSOMWARE
  - "breach" or "data" → CyberEventType.DATA_BREACH
  - "phish" → CyberEventType.PHISHING
  - else → CyberEventType.OTHER

### 4.5 Error Handling & Circuit Breaker
**Location**: Lines 54-62, 86-88, 385-400

Circuit breaker pattern:
- Max retries: 3 attempts with exponential backoff
- Base delay: 2 seconds
- Max delay: 60 seconds
- Backoff multiplier: 2.0 (doubles each retry)
- Circuit breaker threshold: 5 consecutive failures
- Cool-off period: 5 minutes after threshold

Error classification:
- **Auth errors** (401, 403, invalid API key): No retry, raises immediately
- **Rate limit errors** (429): Retries with longer delay (30s extra)
- **Server errors** (5xx): Retries with exponential backoff
- **Client errors** (4xx except 429): No retry, raises immediately
- **Network errors**: Retries with exponential backoff

---

## 5. ENTITY NAME HANDLING & MATCHING LOGIC

### 5.1 Entity Extraction from Titles
**Location**: `/cyber_data_collector/processing/deduplication.py` lines 133-199

17+ regex patterns for extraction, including:

**Indicator-based patterns**:
- Organization suffixes: Inc, Corp, Ltd, Limited, Company, LLC, Pty, Group, Bank, Insurance, University, College, Hospital, Health, Airways, Telecom

**Action-based patterns**:
- "X suffers/confirms/experiences/reports/admits/reveals/discloses/investigates cyber/data breach/hack/attack"
- "X hit/struck/targeted/affected/impacted"
- "X held to ransom"
- "X tight-lipped"
- "X shuts"

**Attack format patterns**:
- "Ransomware Attack on X"
- "The X hack"
- "X members compromised"
- "data breach involving X"
- "X facing class action"
- "X Contact Centre"

**Fallback heuristics**:
- Conservative approach: requires either org indicator or two proper nouns
- Only uses first 2 words if both are capitalized and >2 characters each

### 5.2 Entity Similarity Calculation
**Location**: Lines 201-246

**Strategy**: Normalize then compare

Normalization removes suffixes:
- 'group', 'company', 'corp', 'corporation', 'inc', 'incorporated', 'ltd', 'limited', 'llc', 'pty', 'bank', 'insurance'

Similarity scoring:

1. **Exact match**: 1.0
2. **Normalized exact match**: 0.95
3. **Subset match** (e.g., "Toll" ⊂ "Toll Group"): 0.95
4. **Sequence similarity**: SequenceMatcher ratio on both normalized and original names
5. **Return max of normalized and original similarity**

### 5.3 Entity Handling During Merge
**Location**: Lines 658-668

When merging event groups:
```python
# Merge affected entities using name-based deduplication
entity_map: Dict[str, Dict[str, Any]] = {}
for event in events:
    for entity in event.affected_entities:
        key = entity.name.lower()  # Case-insensitive deduplication
        if key not in entity_map:
            entity_map[key] = entity.dict()

merged_event.affected_entities = [AffectedEntity(**data) for data in entity_map.values()]

# Set primary entity to first merged entity
if merged_event.affected_entities:
    merged_event.primary_entity = merged_event.affected_entities[0]
```

### 5.4 Primary Entity vs Affected Entities
**Location**: `/cyber_data_collector/models/events.py` lines 123-124

- **primary_entity**: Single most important victim (AffectedEntity object)
- **affected_entities**: List of all entities affected by the event

During deduplication merge, only the first deduplicated entity becomes primary.

---

## 6. TIME SPAN LIMITATIONS

### 6.1 Data Collection Time Span
**Location**: `/discover_enrich_events.py` lines 179-185, 232

Parameter mentions:
```python
async def discover_events(
    self, 
    source_types: List[str] = None, 
    date_range_days: int = 7,  # Now largely ignored
    max_events: int = 1000
):
```

**Note**: `date_range_days` parameter is marked as "ignored - now processes all months"

### 6.2 Historical Data Coverage
**Location**: `/cyber_event_data_v2.py` lines defined in `get_unprocessed_months`

Default range mentioned in comments: **Jan 2020 - Aug 2025** (68 months total)

Processing is done on monthly basis:
- System tracks which months have been processed
- Generates completion statistics
- Processes unprocessed months iteratively

### 6.3 No Explicit Time Limit Enforcement
**Key Finding**: No hardcoded time span limitation found in deduplication logic itself. The system:
- Processes all available months from historical data (back to 2020)
- Uses 30-day tolerance for event date matching during deduplication
- No maximum lookback period enforced at deduplication level

### 6.4 Monthly Processing Window
**Location**: Implicit throughout `discover_enrich_events.py`

Events are collected and deduplicated on a per-month basis:
- Each month's events are deduplicated separately
- Month is used as processing unit for tracking and statistics
- No cross-month deduplication overlap mentioned

---

## 7. NEW/RECENTLY ADDED FIELDS

### 7.1 Contributing Event Count Fields

**Location**: 
- `/cyber_data_collector/models/events.py` lines 114-115
- `/add_deduplication_schema.py` lines 49-50

**Fields added to support duplicate tracking**:

```python
# In CyberEvent model:
contributing_raw_events: int = Field(
    default=1, 
    description="Number of raw events that contributed to this deduplicated event"
)
contributing_enriched_events: int = Field(
    default=1,
    description="Number of enriched events that contributed to this deduplicated event"
)

# In DeduplicatedEvents table:
contributing_raw_events INTEGER DEFAULT 0,      -- # raw events merged
contributing_enriched_events INTEGER DEFAULT 0, -- # enriched events merged
```

**Purpose**: Track deduplication scope and event consolidation

**Set during merge** (`_merge_event_group`, lines 651-652):
```python
merged_event.contributing_raw_events = len(events)
merged_event.contributing_enriched_events = len(events)
```

### 7.2 Threat Actor Tracking Fields

**Location**: `/add_threat_actor_column.py` lines 25-41, 48-62

**Fields added to EnrichedEvents and DeduplicatedEvents**:

```sql
-- Added columns (lines 31-40, 53-62):
ALTER TABLE EnrichedEvents
ADD COLUMN attacking_entity_id INTEGER;                    -- **NEW**
ADD COLUMN attacking_entity_name VARCHAR(255);             -- **NEW**
ADD COLUMN attack_method VARCHAR(100);                     -- **NEW**

-- Similar additions to DeduplicatedEvents
```

**Purpose**: Distinguish between victim entities and attacking threat actors

**Population logic** (lines 119-150):
- Finds threat actors from EntitiesV2 where `entity_type = 'threat-actor'`
- Updates events where actor is mentioned in title/summary
- Infers attack method from actor name (e.g., ransomware operators)
- Cascades to deduplicated events from their master enriched events

**Indexes created**:
- `idx_enriched_attacking_entity` on EnrichedEvents(attacking_entity_id)
- `idx_dedup_attacking_entity` on DeduplicatedEvents(attacking_entity_id)

---

## 8. INTEGRATION FLOW

### 8.1 Data Flow Pipeline
```
RawEvents (multiple sources)
    ↓
[Scraping & LLM Analysis]
    ↓
EnrichedEvents + Threat Actor Assignment
    ↓
[Deduplication by Entity + Content]
    ↓
DeduplicatedEvents (final unique events)
    ↓
[Entity & Data Source Storage]
    ↓
[Deduplication Cluster Tracking]
```

### 8.2 Orchestration
**Location**: `/discover_enrich_events.py` lines 334-394

Monthly processing loop:
1. Collect raw events from all sources
2. Scrape URLs for content
3. Enrich via LLM (extract dates, entities, etc.)
4. Filter for Australian cyber events
5. Store as EnrichedEvents
6. **Load enriched events for deduplication**
7. **Run deduplication engine** (merge similar events)
8. **Store deduplicated results**
9. **Store entity relationships**
10. **Store data sources**
11. **Track deduplication clusters**

---

## 9. KEY TAKEAWAYS

1. **Entity matching is the hard gate** - events must have similar entities to be considered duplicates

2. **Multi-layered similarity** - title, description, key terms, date proximity all weighted differently

3. **Smart defaults for dates** - 1st of month is avoided during merge as likely placeholder

4. **Comprehensive tracking** - full traceability from raw → enriched → deduplicated with contribution counts

5. **Threat actor distinction** - newly added fields separate victim entities from attacking entities

6. **LLM as arbiter** - borderline cases use OpenAI with conservative approach to avoid false positives

7. **Time tolerance reasonable** - 30 days for deduplication, ~3 months for date proximity scoring

8. **Monthly processing** - system processes data on per-month basis for tracking and efficiency
