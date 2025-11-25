# Australian Cyber Events Pipeline Architecture

**Last Updated:** 2025-10-30

## Single Source of Truth

**`run_full_pipeline.py`** is the **ONLY** entry point for all operations.

### Why This Matters

Previously, the project had multiple scattered scripts (`discover_enrich_events.py`, `perplexity_backfill_events.py`, `batch_enrichment.py`, etc.) using different enrichment algorithms, leading to inconsistent data quality.

**The unified pipeline ensures:**
- ✅ Consistent Perplexity AI enrichment for ALL events
- ✅ Advanced entity-based deduplication
- ✅ Single event definition (one real-world incident = one event)
- ✅ Automated end-to-end processing

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    run_full_pipeline.py                          │
│              (SINGLE SOURCE OF TRUTH)                            │
└─────────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
┌────────────────┐   ┌─────────────────┐   ┌────────────────┐
│  PHASE 1:      │   │  PHASE 2:       │   │  PHASE 3:      │
│  Discovery +   │──▶│  Deduplication  │──▶│  Dashboard     │
│  Enrichment    │   │                 │   │                │
└────────────────┘   └─────────────────┘   └────────────────┘
```

## Phase 1: Discovery & Enrichment

### Step 1: Raw Discovery (Fast Pass)
- **Sources:** GDELT, Perplexity API, Google Search, Webber Insurance
- **Filtering:** Initial GPT-4o-mini filtering (fast, basic quality check)
- **Purpose:** Quickly gather raw events, filter out obvious non-Australian/non-specific content

**Historical Issue:** This used to be the ONLY enrichment step, resulting in 70% rejection rate.

### Step 2: Perplexity AI Enrichment (High Quality)
**AUTOMATICALLY runs after discovery** to upgrade all events with sophisticated enrichment:

- **Formal entity names** (e.g., "Qantas" → "Qantas Airways Limited")
- **Threat actor identification** with confidence scores
- **Attack method classification** (ransomware, phishing, data breach, etc.)
- **Industry categorization**
- **Victim count validation**
- **Multi-source verification**

**Why This Matters:** Perplexity AI uses web search + reasoning to verify facts across multiple sources, dramatically improving accuracy over simple GPT-4o-mini extraction.

**Result:** Rejection rate drops from 70% to ~30%, with much higher quality data.

### Step 3: Global Deduplication

Runs after enrichment using advanced entity-based matching:

**Deduplication Rules:**

1. **RULE 1:** Same entity + same date → ALWAYS merge
2. **RULE 2:** Same entity + similar titles (Jaccard similarity ≥ 0.15) → MERGE
3. **RULE 3:** Use EARLIEST date from merged events as canonical incident date

**Why 0.15 threshold?**
- For same entity, we're already confident they might be the same incident
- Lower threshold catches variations like "Ticketmaster hack" vs "Live Nation probing Ticketmaster hack"
- Prevents false positives for truly different incidents

**Example:**
```
Input:
- "Ticketmaster hack" (April 15, 2024)
- "Live Nation probing Ticketmaster hack" (May 20, 2024)
- "Ticketmaster data breach disclosed" (May 21, 2024)

Output:
- "Live Nation probing Ticketmaster hack" (April 15, 2024)
  [merged from 3 sources, using earliest date]
```

## Phase 2: Dashboard Generation

Generates static HTML dashboard with:
- Monthly event trends
- Severity analysis
- Industry distribution
- Attack type classification
- Records affected histograms
- OAIC comparison metrics
- Event type correlation analysis

## Critical Concepts

### Event Definition

**AN EVENT = ONE REAL-WORLD CYBER SECURITY INCIDENT**

NOT multiple news articles about the same incident!

This is enforced through:
1. Entity-based deduplication
2. Perplexity AI fact-checking across sources
3. Earliest date selection for merged events

### Data Quality Evolution

```
2079 raw events discovered
   ↓ (OLD: GPT-4o-mini only)
   ↓ → 938 enriched (55% retention) ❌ LOW QUALITY
   ↓
   ↓ (NEW: GPT-4o-mini + Perplexity)
   ↓ → 2045 enriched (98% retention) ✅ HIGH QUALITY
   ↓
   ↓ (Deduplication with entity matching)
   ↓ → 900 deduplicated events (43% reduction)
```

## Usage

### Normal Operations (Recommended)

```bash
# Run everything (discovery + enrichment + deduplication + dashboard)
python run_full_pipeline.py

# Discover new months only (auto-enriches with Perplexity)
python run_full_pipeline.py --discover-only

# Re-enrich existing events with updated Perplexity prompts
python run_full_pipeline.py --re-enrich

# Dashboard only
python run_full_pipeline.py --dashboard-only
```

### Advanced Options

```bash
# Control discovery sources
python run_full_pipeline.py --source Perplexity OAIC

# Limit events for testing
python run_full_pipeline.py --max-events 100

# Dashboard variants
python run_full_pipeline.py --dashboard-type flask --launch-server
```

## Deprecated Scripts

These scripts are **DEPRECATED** and should NOT be used:

- ❌ `discover_enrich_events.py` - Uses old GPT-4o-mini enrichment (70% rejection rate)
- ❌ `perplexity_backfill_events.py` - Now integrated into unified pipeline
- ❌ `batch_enrichment.py` - Replaced by automatic Perplexity enrichment
- ❌ `perplexity_data_enrichment.py` - Legacy enrichment script

**Always use `run_full_pipeline.py` instead.**

## Key Files

### Core Pipeline
- `run_full_pipeline.py` - **SINGLE SOURCE OF TRUTH** entry point
- `build_static_dashboard.py` - Static HTML dashboard generator (called by run_full_pipeline.py)
- `cyber_data_collector/processing/perplexity_enrichment.py` - Perplexity AI enrichment engine
- `cyber_data_collector/processing/deduplication_v2.py` - Advanced deduplication with entity matching

### Supporting Components
- `cyber_data_collector/datasources/` - Data source implementations (GDELT, Perplexity, Google, Webber)
- `cyber_data_collector/storage/database.py` - SQLite database interface
- `cyber_event_data_v2.py` - Database ORM and queries
- `migrate_to_global_deduplication.py` - Deduplication execution script

### Legacy (Do Not Use)
- `discover_enrich_events.py` - ⚠️ DEPRECATED
- `llm_extractor.py` - Old GPT-4o-mini enrichment
- `perplexity_backfill_events.py` - ⚠️ DEPRECATED

## Database Schema

### Key Tables

**RawEvents** → **EnrichedEvents** → **DeduplicatedEvents**

```
RawEvents
├── raw_event_id (PK)
├── source_type
├── raw_title
├── raw_description
├── event_date
└── is_processed

EnrichedEvents
├── enriched_event_id (PK)
├── raw_event_id (FK)
├── title
├── summary
├── event_date
├── victim_organization_name
├── perplexity_validated
├── perplexity_enrichment_data (JSON)
└── is_australian_event

DeduplicatedEvents
├── deduplicated_event_id (PK)
├── master_enriched_event_id (FK)
├── victim_organization_name
├── event_date (EARLIEST from merged events)
├── records_affected
└── status (Active/Superseded)
```

## Troubleshooting

### Low Event Counts

**Problem:** Sep-25 and Oct-25 had only 5-6 events vs expected 20-25.

**Root Cause:** Discovery used old GPT-4o-mini enrichment (70% rejection rate).

**Solution:** Run re-enrichment to upgrade with Perplexity:
```bash
python run_full_pipeline.py --re-enrich
```

### Duplicate Events

**Problem:** Same incident appearing multiple times (e.g., Ticketmaster showing 5 times).

**Root Cause:** Deduplication not run or entity names don't match.

**Solution:**
1. Ensure Perplexity enrichment extracted proper entity names
2. Run deduplication: `python migrate_to_global_deduplication.py`
3. Check deduplication rules in `deduplication_v2.py:718-849`

### Event Count Verification

```bash
# Check monthly counts
sqlite3 instance/cyber_events.db "
  SELECT strftime('%Y-%m', event_date) as month, COUNT(*)
  FROM DeduplicatedEvents
  WHERE status = 'Active'
  GROUP BY month
  ORDER BY month DESC
  LIMIT 12;
"

# Check enrichment status
sqlite3 instance/cyber_events.db "
  SELECT
    COUNT(*) as total,
    SUM(CASE WHEN perplexity_validated = 1 THEN 1 ELSE 0 END) as perplexity_enriched
  FROM EnrichedEvents;
"
```

## Future Improvements

- [ ] Migrate discovery to use Perplexity enrichment directly (bypass GPT-4o-mini fast pass)
- [ ] Implement parallel enrichment for faster processing
- [ ] Add confidence-based filtering options
- [ ] Create automated quality metrics dashboard

## Conclusion

**Always use `run_full_pipeline.py` for all operations.** This ensures consistent, high-quality data processing with the latest algorithms.

For questions or issues, check the logs in `unified_pipeline.log`.
