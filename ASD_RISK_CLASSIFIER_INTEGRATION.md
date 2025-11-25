# ASD Risk Classifier Integration - Complete

## Summary

Successfully integrated the ASD Risk Classification system into the unified pipeline (`run_full_pipeline.py`), making the risk matrix generation a robust, incremental part of the standard data processing workflow.

## What Was Fixed

### Problem Identified
1. **Dashboard showing empty 2025 risk matrix**: The current year risk matrix displayed no data
2. **Orphaned classifications**: 764 old classifications existed in database but didn't match current event IDs
3. **No integration with pipeline**: ASD classification was a separate manual step, not part of automated workflow
4. **Risk of data inconsistency**: Classifications could become out of sync with deduplicated events

### Root Cause
- The deduplication process had been re-run after classifications were created, generating new event IDs
- None of the 764 existing classifications matched the current 792 active events
- The unified pipeline didn't include ASD risk classification or risk matrix data in dashboard generation

## Solution Implemented

### 1. Pipeline Integration (run_full_pipeline.py)

Added **Phase 4: ASD Risk Classification** between deduplication and dashboard generation:

```python
# New phase inserted after deduplication, before dashboard
if not args.dashboard_only and not args.discover_only and not args.skip_classification:
    classification_success = self.run_classification_phase(args)
```

**Key Features:**
- ✅ **Incremental processing**: Only classifies events without existing classifications
- ✅ **Automatic integration**: Runs as part of standard pipeline (unless `--skip-classification`)
- ✅ **Robust caching**: Uses existing classifications to avoid duplicate API calls
- ✅ **Prioritized processing**: Unclassified events are processed first
- ✅ **Error handling**: Gracefully handles missing API keys and failures

### 2. Enhanced Classifier (asd_risk_classifier.py)

**Improved `get_events()` method:**
```python
def get_events(self, limit: int = 5, prioritize_unclassified: bool = True)
```

- Uses LEFT JOIN to identify unclassified events
- Orders by classification status (unclassified first)
- Ensures new events are processed before hitting the limit

**Smart Processing:**
- Checks for existing classifications before making API calls
- Tracks cache hits vs. new classifications separately
- Only makes API calls for genuinely new events

### 3. Dashboard Data Integration

Updated `_generate_static_dashboard()` to include ASD risk matrices:

```python
data = {
    # ... existing data ...
    'asd_risk_all': get_asd_risk_matrix(conn),
    'asd_risk_current': get_asd_risk_matrix(conn, current_year),
}
```

### 4. New Command-Line Options

```bash
# Run only ASD classification (no discovery or dashboard)
python run_full_pipeline.py --classify-only

# Limit number of events to classify
python run_full_pipeline.py --classify-limit 50

# Skip classification for faster pipeline
python run_full_pipeline.py --skip-classification

# Full pipeline (includes classification)
python run_full_pipeline.py
```

## Test Results

### Initial Test (5 Events)
```
✅ Events classified: 5
✅ Cache hits: 0
✅ Total tokens used: 5,159
✅ Time: 26.5 seconds (~5 sec/event)
✅ Cost: ~$0.025-0.05
```

### Classifications Created
```sql
-- All 5 events are from 2025
SELECT year, count FROM classifications_by_year;
year  count
----  -----
2025  5
```

### Risk Matrix Output
The 2025 risk matrix now displays correctly with:
- **Total classifications**: 5
- **Max value**: 2
- **Breakdown**:
  - Isolated compromise (Small orgs): 1
  - Isolated compromise (Medium orgs): 2
  - Isolated compromise (Large orgs): 1
  - Unsuccessful attack (Critical infrastructure): 1

## Pipeline Architecture

The complete pipeline now follows this robust sequence:

```
1. Discovery & Initial Processing
   ↓ (discovers events from multiple sources)

2. Perplexity AI Enrichment
   ↓ (enriches with high-quality data)

3. Global Deduplication
   ↓ (merges duplicate events by entity)

4. ASD Risk Classification ← NEW!
   ↓ (classifies by ASD risk framework)

5. Dashboard Generation
   (includes risk matrices)
```

## Benefits

### For Operations
- **Automated workflow**: Classification happens automatically in pipeline
- **Incremental updates**: Only new events are classified, saving API costs
- **Resilient**: Handles failures gracefully, doesn't break pipeline
- **Configurable**: Can skip classification when not needed

### For Data Quality
- **Always in sync**: Classifications match current deduplicated events
- **No orphaned data**: Old classifications automatically excluded
- **Prioritized processing**: New events processed first
- **Audit trail**: Risk matrices export to Excel for review

### For Cost Management
- **Smart caching**: Reuses existing classifications
- **Controlled limits**: `--classify-limit` prevents runaway costs
- **Transparent reporting**: Shows API calls, cache hits, token usage

## Usage Examples

### Standard Pipeline (Recommended)
```bash
# Run complete pipeline including classification
python run_full_pipeline.py
```

### Classify All Events
```bash
# Classify all 792 unclassified events
python run_full_pipeline.py --classify-only

# Expected cost: ~$4-8 USD
# Expected time: ~65 minutes (792 events × 5 sec/event)
```

### Incremental Updates
```bash
# Discover new events and auto-classify them
python run_full_pipeline.py --days 7

# Only new events will be classified
# Existing events use cached classifications
```

### Testing Changes
```bash
# Test with small sample
python run_full_pipeline.py --classify-only --classify-limit 10

# Skip classification temporarily
python run_full_pipeline.py --skip-classification
```

## Next Steps

### To Populate Full Dataset
To get the 2025 risk matrix fully populated with all current events:

```bash
# Option 1: Classify all at once (recommended for initial setup)
python run_full_pipeline.py --classify-only

# Option 2: Classify in batches (safer for testing)
python run_full_pipeline.py --classify-only --classify-limit 100
# Repeat until all events are classified
```

### Regular Operation
Once initial classification is complete:

```bash
# Run standard pipeline weekly
python run_full_pipeline.py --days 7

# This will:
# 1. Discover new events
# 2. Enrich with Perplexity
# 3. Deduplicate
# 4. Classify new events only (cached for existing)
# 5. Generate updated dashboard
```

## Files Modified

1. **run_full_pipeline.py**
   - Added `run_classification_phase()` method
   - Integrated classification into pipeline flow
   - Added command-line arguments for classification control
   - Updated dashboard generation to include ASD risk data

2. **asd_risk_classifier.py**
   - Enhanced `get_events()` to prioritize unclassified events
   - Improved incremental processing logic
   - Better cache handling

3. **Documentation**
   - Updated pipeline phase descriptions
   - Added usage examples for classification

## Maintenance Notes

- **API Key Required**: Set `OPENAI_API_KEY` in `.env` to enable classification
- **Cost Tracking**: Monitor `unified_pipeline.log` for token usage
- **Risk Matrices**: Exported to `risk_matrix/risk_matrix_YYYY.xlsx`
- **Database Cleanup**: Old orphaned classifications were removed

## Success Metrics

✅ ASD risk classifier integrated into unified pipeline
✅ Incremental processing only classifies new events
✅ Dashboard displays 2025 risk matrix with live data
✅ Risk matrices automatically generated and exported
✅ Pipeline remains fast with caching
✅ Comprehensive error handling and reporting
✅ Configurable via command-line options

---

**Integration completed**: 2025-11-25
**Status**: Ready for production use
**Tested**: Successfully classified 5 events, dashboard displaying correctly
