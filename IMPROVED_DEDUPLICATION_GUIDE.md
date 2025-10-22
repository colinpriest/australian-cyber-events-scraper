# Improved Deduplication System - Implementation Guide

## Overview

This document describes the enhanced deduplication system that addresses duplicate event issues through:

1. **Perplexity-based event validation** for accurate dates and entity names
2. **Relaxed matching thresholds** for better fuzzy matching
3. **Intelligent merge logic** with field-level confidence scoring
4. **Automated re-deduplication** after enrichment

## Problem Statement

The original deduplication system had several issues:

- **Placeholder dates** (defaulting to 1st of month) causing artificial date mismatches
- **30-day hard limit** missing late-reported incidents
- **0.8 entity similarity threshold** too strict, missing valid variations
- **No validation** of extracted entity names and dates
- **Lack of acronym matching** (e.g., "NAB" vs "National Australia Bank")

## Solution Architecture

### 1. Perplexity Enrichment Module

**File:** `cyber_data_collector/processing/perplexity_enrichment.py`

**Purpose:** Validates and enriches individual events with authoritative data

**Key Features:**
- Queries Perplexity AI to find earliest reported event dates
- Validates formal/official entity names
- Identifies threat actors and attack methods
- Provides victim count validation
- Returns confidence scores for all fields

**Usage:**
```python
from cyber_data_collector.processing.perplexity_enrichment import PerplexityEnrichmentEngine

engine = PerplexityEnrichmentEngine()

# Enrich a single event
enrichment = await engine.enrich_event(
    title="Medibank data breach affects millions",
    description="Medibank confirms customer data stolen...",
    current_date="2022-11-01",  # Placeholder date
    current_entity="Medibank"
)

# Results include:
# - earliest_event_date: "2022-10-13" (with date_confidence: 0.9)
# - formal_entity_name: "Medibank Private Limited" (with entity_confidence: 0.95)
# - threat_actor: "REvil" (with threat_actor_confidence: 0.7)
# - attack_method: "ransomware" (with attack_method_confidence: 0.85)
```

**Duplicate Checking:**
```python
# Check if two events are the same incident
check = await engine.check_duplicate(
    event1_title="Optus breach",
    event1_description="...",
    event1_date="2022-09-01",
    event1_entity="Optus",
    event2_title="Optus data leak",
    event2_description="...",
    event2_date="2022-09-22",
    event2_entity="Optus Australia"
)

# Returns: are_same_incident=True, confidence=0.92, reasoning="..."
```

### 2. Improved Deduplication Logic

**File:** `cyber_data_collector/processing/deduplication.py`

**Changes Made:**

#### a) Relaxed Entity Similarity Threshold
```python
# Before:
self.similarity_threshold = 0.8  # Too strict

# After:
self.entity_similarity_threshold = 0.7  # Better fuzzy matching
```

#### b) Removed 30-Day Hard Limit
```python
# Before:
self.date_tolerance_days = 30  # Hard cutoff

# After:
self.date_tolerance_days = None  # Use as scoring factor
```

Date differences now contribute to a **sliding scale** rather than hard rejection:
- 0-7 days: 0.98 factor
- 8-30 days: 0.90 factor
- 31-90 days: 0.80 factor
- 91-180 days: 0.70 factor
- 181-365 days: 0.60 factor
- >365 days: 0.40-0.60 factor (graduated)

#### c) Enhanced Entity Matching

**Acronym Matching:**
```python
def _check_acronym_match(self, name1, name2):
    # Matches "NAB" with "National Australia Bank"
    # Matches "FBI" with "Federal Bureau of Investigation"
```

**Common Variations:**
```python
common_variations = {
    'anz': 'australia and new zealand banking group',
    'nab': 'national australia bank',
    'cba': 'commonwealth bank',
    # ... more
}
```

**Subset Matching:**
- "Toll" matches "Toll Group" with 0.95 similarity
- Normalized entity names ignore suffixes (Corp, Ltd, Inc, etc.)

#### d) Perplexity Arbiter for Uncertain Matches

For similarity scores between **0.50 and 0.85** (uncertain range):
1. **Try Perplexity first** (if available) for authoritative decision
2. **Fallback to LLM** (GPT-3.5) if Perplexity unavailable
3. **Use conservative threshold** if both fail

```python
if 0.50 <= weighted_similarity < 0.85:
    # Use Perplexity for final decision
    perplexity_check = await perplexity_arbiter.check_duplicate(...)
    if perplexity_check.confidence >= 0.7:
        return perplexity_check.are_same_incident
```

### 3. Database Schema Updates

**File:** `add_perplexity_validation_fields.py`

**New Fields Added:**

#### EnrichedEvents & DeduplicatedEvents:
- `perplexity_validated` (BOOLEAN): Whether event has been Perplexity-validated
- `perplexity_validated_at` (TIMESTAMP): When validation occurred
- `perplexity_enrichment_data` (TEXT/JSON): Full enrichment response
- `date_confidence` (REAL): Confidence in event_date (0.0-1.0)
- `entity_confidence` (REAL): Confidence in entity identification (0.0-1.0)
- `data_source_reliability` (REAL): Overall source reliability score (0.0-1.0)

**Run Migration:**
```bash
python add_perplexity_validation_fields.py
```

### 4. Backfill Script for Existing Events

**File:** `perplexity_backfill_events.py`

**Purpose:** Enrich existing events with Perplexity validation

**Priority System:**
1. **High Priority:** Events with placeholder dates (1st of month)
2. **Medium Priority:** Events with low confidence scores
3. **Low Priority:** All other events

**Usage:**
```bash
# Dry run to see what would be changed
python perplexity_backfill_events.py --dry-run --limit 10

# Process only high-priority events
python perplexity_backfill_events.py --priority-only --limit 50

# Process all events (expensive!)
python perplexity_backfill_events.py --limit 1000
```

**Rate Limiting:**
- 2 seconds between Perplexity requests
- Configurable retry logic with exponential backoff

### 5. Iterative Re-Deduplication

After Perplexity enrichment, events need to be re-deduplicated with updated:
- More accurate event dates
- Validated entity names
- Improved confidence scores

**Workflow:**
1. **Backfill** existing DeduplicatedEvents with Perplexity
2. **Mark** DeduplicatedEvents as needing re-processing
3. **Re-run** deduplication with new data
4. **Second pass** catches duplicates missed in first pass

## Implementation Workflow

### Phase 1: Database Setup
```bash
# Add Perplexity validation fields
python add_perplexity_validation_fields.py
```

### Phase 2: Backfill High-Priority Events
```bash
# Start with placeholder dates (dry run first)
python perplexity_backfill_events.py --dry-run --priority-only --limit 10

# If results look good, run for real
python perplexity_backfill_events.py --priority-only --limit 100
```

### Phase 3: Re-Deduplicate
```bash
# Run deduplication processor with updated data
python deduplication_processor.py
```

### Phase 4: Backfill Remaining Events (Optional)
```bash
# Process remaining events in batches
python perplexity_backfill_events.py --limit 500
```

### Phase 5: Monitor and Validate
```sql
-- Check Perplexity validation statistics
SELECT
    COUNT(*) as total_events,
    SUM(CASE WHEN perplexity_validated = TRUE THEN 1 ELSE 0 END) as validated,
    AVG(date_confidence) as avg_date_confidence,
    AVG(entity_confidence) as avg_entity_confidence
FROM DeduplicatedEvents;

-- Find events with improved dates
SELECT
    title,
    event_date,
    date_confidence,
    perplexity_validated_at
FROM DeduplicatedEvents
WHERE perplexity_validated = TRUE
  AND date_confidence >= 0.8
ORDER BY perplexity_validated_at DESC
LIMIT 20;
```

## Configuration Options

### Environment Variables

```bash
# Required for Perplexity enrichment
export PERPLEXITY_API_KEY="your-api-key-here"

# Optional for LLM arbiter fallback
export OPENAI_API_KEY="your-openai-key-here"
```

### Cost Control

**Perplexity API Costs:**
- ~$0.001 per request (sonar-pro model)
- Enriching 1,000 events ≈ $1.00
- Duplicate checking ≈ $0.0005 per comparison

**Recommendations:**
1. Start with `--priority-only` to target high-value events
2. Use `--limit` to control batch sizes
3. Monitor API costs in Perplexity dashboard
4. Consider caching enrichment results

### Confidence Thresholds

**Adjustable in code:**

```python
# perplexity_backfill_events.py
MIN_DATE_CONFIDENCE = 0.6  # Only update if confidence >= 0.6
MIN_ENTITY_CONFIDENCE = 0.6
MIN_THREAT_ACTOR_CONFIDENCE = 0.6

# deduplication.py
PERPLEXITY_ARBITER_MIN_CONFIDENCE = 0.7  # Use decision if >= 0.7
```

## Expected Results

### Duplicate Reduction

Based on the improvements, we expect:
- **10-20% reduction** in total deduplicated events (finding more duplicates)
- **30-50% improvement** in catching late-reported incidents
- **90%+ accuracy** for entity name matching

### Data Quality Improvements

- **Event dates:** 70-80% of placeholder dates replaced with accurate dates
- **Entity names:** 80-90% validated with formal names
- **Threat actors:** 40-50% of events get threat actor attribution
- **Attack methods:** 60-70% of events get accurate attack method classification

## Troubleshooting

### Issue: Perplexity API Rate Limiting

**Solution:** Increase delay between requests
```python
# In PerplexityEnrichmentEngine.__init__()
self.min_request_interval = 3.0  # Increase from 2.0 to 3.0 seconds
```

### Issue: Low Confidence Scores

**Diagnosis:** Check `perplexity_enrichment_data` field:
```sql
SELECT
    title,
    json_extract(perplexity_enrichment_data, '$.overall_confidence') as confidence,
    json_extract(perplexity_enrichment_data, '$.reasoning') as reasoning
FROM DeduplicatedEvents
WHERE perplexity_validated = TRUE
  AND json_extract(perplexity_enrichment_data, '$.overall_confidence') < 0.5
LIMIT 10;
```

**Solution:** Lower confidence thresholds for updates, or investigate event titles/descriptions

### Issue: Too Many False Positives

**Diagnosis:** Events being merged that shouldn't be

**Solution:** Increase entity similarity threshold:
```python
# In DeduplicationEngine.__init__()
self.entity_similarity_threshold = 0.75  # Increase from 0.7
```

### Issue: Too Many False Negatives

**Diagnosis:** Duplicates not being caught

**Solution:** Lower similarity thresholds or check Perplexity arbiter is enabled:
```python
# When initializing DeduplicationEngine
from cyber_data_collector.processing.perplexity_enrichment import PerplexityEnrichmentEngine

perplexity_engine = PerplexityEnrichmentEngine()
dedup_engine = DeduplicationEngine(perplexity_arbiter=perplexity_engine)
```

## Monitoring and Metrics

### Key Metrics to Track

1. **Deduplication Rate:**
   ```sql
   SELECT
       (SELECT COUNT(*) FROM EnrichedEvents) as total_enriched,
       (SELECT COUNT(*) FROM DeduplicatedEvents) as total_deduplicated,
       ROUND(100.0 * (SELECT COUNT(*) FROM DeduplicatedEvents) /
             (SELECT COUNT(*) FROM EnrichedEvents), 2) as dedup_percentage;
   ```

2. **Perplexity Validation Coverage:**
   ```sql
   SELECT
       COUNT(*) as total,
       SUM(CASE WHEN perplexity_validated = TRUE THEN 1 ELSE 0 END) as validated,
       ROUND(100.0 * SUM(CASE WHEN perplexity_validated = TRUE THEN 1 ELSE 0 END) /
             COUNT(*), 2) as validation_percentage
   FROM DeduplicatedEvents;
   ```

3. **Confidence Score Distribution:**
   ```sql
   SELECT
       CASE
           WHEN date_confidence >= 0.9 THEN 'Very High (0.9+)'
           WHEN date_confidence >= 0.7 THEN 'High (0.7-0.9)'
           WHEN date_confidence >= 0.5 THEN 'Medium (0.5-0.7)'
           ELSE 'Low (<0.5)'
       END as confidence_level,
       COUNT(*) as count
   FROM DeduplicatedEvents
   WHERE perplexity_validated = TRUE
   GROUP BY confidence_level
   ORDER BY MIN(date_confidence) DESC;
   ```

## Future Enhancements

1. **Machine Learning Integration:**
   - Train ML model on Perplexity-validated data
   - Use model for faster validation without API calls

2. **Automated Re-Deduplication:**
   - Trigger re-deduplication automatically after batch enrichment
   - Incremental deduplication for new events only

3. **Enhanced Entity Resolution:**
   - Build entity alias database from Perplexity responses
   - Cross-reference with external entity databases (companies house, etc.)

4. **Multi-Source Validation:**
   - Combine Perplexity with other sources (CVE databases, threat intel feeds)
   - Use voting/consensus for higher confidence

## Conclusion

The improved deduplication system provides:
- ✅ More accurate event dates (Perplexity validation)
- ✅ Better entity matching (fuzzy matching, acronyms, variations)
- ✅ Fewer false negatives (relaxed date limits)
- ✅ Automated duplicate checking (Perplexity arbiter)
- ✅ Full traceability (confidence scores, enrichment data)

**Next Steps:**
1. Run database migration: `python add_perplexity_validation_fields.py`
2. Start with small batch: `python perplexity_backfill_events.py --dry-run --limit 10`
3. Review results and adjust thresholds as needed
4. Scale up to full backfill
5. Monitor metrics and iterate

---

**Questions or Issues?**
- Check the troubleshooting section above
- Review the code documentation in each module
- Check logs for detailed debugging information
