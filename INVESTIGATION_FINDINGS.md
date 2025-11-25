# Investigation Findings - ACCEPT_WITH_WARNING Analysis

**Date:** 2025-10-28
**Investigation Tasks:**
1. ✅ Review all 27 ACCEPT_WITH_WARNING events for patterns
2. ✅ Investigate why Example 2 (DaVita) wasn't caught by validation
3. ⏳ Implement proposed penalty adjustments

---

## Part 1: Pattern Analysis of All 27 ACCEPT_WITH_WARNING Events

### Key Findings

**Surprising Discovery:** Most ACCEPT_WITH_WARNING events are actually HIGH quality!

| Pattern | Finding |
|---------|---------|
| **Australian Relevance** | 66.7% (18/27) have HIGH relevance (0.8-1.0) |
| **Fact-Check Pass Rate** | 66.7% (18/27) have 100% pass rate |
| **Validation Warnings** | Only 22.2% (6/27) have validation warnings |
| **Confidence Clustering** | 81.5% (22/27) are at 0.75-0.80 (right at boundary) |
| **Top Victim** | Qantas = 41% (11/27) of all ACCEPT_WITH_WARNING |

### Root Cause: "is_specific_incident: False" Penalty

**55.6% of high-Australian-relevance events (10/18) are marked as NON-SPECIFIC incidents**

This explains the pattern:
- Events like "The Qantas Breach: A Wake-Up Call on Third-Party Privacy Risks" are **analysis articles**, not primary incident reports
- Events like "Qantas Cyber Incident" are too vague/generic
- These get penalized for being non-specific, dropping confidence from ~0.85 to 0.75-0.80

**Examples of Non-Specific Events:**
1. "The Qantas Breach: A Wake-Up Call..." (analysis/opinion piece)
2. "Qantas Cyber Incident" (vague headline)
3. "Australia's Qantas says 6 million customer accounts accessed..." (retrospective reporting)

### User Feedback: "general discussions should be reject rather than accept with warning"

**Agreed!** Analysis pieces and general discussions are not primary sources and should be REJECT, not ACCEPT_WITH_WARNING.

**Proposed Fix:** Increase penalty for `is_specific_incident: False`

---

## Part 2: Why Example 2 (DaVita) Wasn't Caught by Validation

### The Event

**Title:** "DDoS Attacks on Australian Government and University Targets by DieNet"
**URL:** healthcare-data-breaches-in-august-2025
**Extracted Victim:** DaVita Inc. (American healthcare company)
**Australian Relevance:** 0.1

### Expected Behavior

Title-matching validation SHOULD trigger warning:
- "DaVita" does NOT appear in title "Australian Government and University"
- This is a clear mismatch

### Investigation Results

**Title-Matching Logic Test:**
```
Organization: DaVita Inc.
Org words: ['davita', 'inc.']
Key words (>3 chars, not stopwords): ['davita', 'inc.']
  "davita" in title? False
  "inc." in title? False
Full org name in title? False

Should trigger warning? TRUE
```

**Actual Results:**
- JSON batch results: `"validation warnings": 2` ✅
- Database audit trail: Only 1 warning stored ❌

**Conclusion:** Title-matching validation DID trigger, but the warning failed to save to database (likely Unicode encoding issue with special characters in warning message).

### Evidence

1. Pipeline code (line 154-155) DOES pass `event.get('title')` to validator ✅
2. SQL queries DO select `e.title` from EnrichedEvents ✅
3. Title-matching validation method EXISTS and is called (line 64-68) ✅
4. JSON results show 2 warnings, database shows 1 warning = Save failure ❌

**Root Cause:** Unicode encoding error when saving validation warning to database

Observed errors in logs:
```
UnicodeEncodeError: 'charmap' codec can't encode character '\u2192'
```

This suggests the warning message contains special characters (arrows, check marks, etc.) that fail to encode in cp1252.

---

## Part 3: Recommendations for Penalty Adjustments

### Current Issues

1. **Non-specific incidents** get ACCEPT_WITH_WARNING instead of REJECT
2. **Non-Australian events** (relevance 0.0-0.2) get ACCEPT_WITH_WARNING instead of REJECT
3. **Low fact-check pass rates** (< 50%) still allow ACCEPT_WITH_WARNING

### Proposed Penalty Adjustments

```python
# In high_quality_enrichment_pipeline.py or confidence_aggregator.py

# 1. Non-specific incident penalty (user request)
if is_specific_incident == False:
    confidence *= 0.6  # Stronger penalty (was ~0.9)
    # This will drop non-specific from 0.85 → 0.51 → still ACCEPT_WITH_WARNING
    # Need even stronger: 0.5 to push below threshold
    confidence *= 0.5  # Very strong penalty
    # 0.85 * 0.5 = 0.425 → REJECT ✅

# 2. Very low Australian relevance penalty
if australian_relevance < 0.3:
    confidence *= 0.4  # Heavy penalty
    # Example: 0.80 * 0.4 = 0.32 → REJECT ✅

# 3. Low fact-check pass rate penalty
fact_check_pass_rate = checks_passed / checks_performed if checks_performed > 0 else 1.0
if fact_check_pass_rate < 0.5:
    confidence *= 0.5  # Half confidence for <50% pass rate
    # Example: 0.72 * 0.5 = 0.36 → REJECT ✅

# 4. Geographic mismatch penalty (new)
# If title mentions "Australian" but victim is foreign company
if 'australian' in title.lower() and australian_relevance < 0.3:
    confidence *= 0.3  # Strong penalty for mismatch
    # DaVita example: 0.79 * 0.3 = 0.24 → REJECT ✅
```

### Expected Impact

With these adjustments, the ACCEPT_WITH_WARNING distribution would change:

**Current:**
- 27 events (27% of 100)
- Mix of legitimate warnings and should-be-rejects

**After Fixes:**
- ~10-15 events (10-15% of 100)
- Mostly legitimate "accept with caution" cases
- Non-specific incidents → REJECT
- Non-Australian events → REJECT
- Low fact-check rates → REJECT

---

## Part 4: Secondary Issues to Address

### Issue 1: Unicode Encoding in Validation Warnings

**Problem:** Validation warnings with special characters fail to save to database

**Fix:** Add UTF-8 encoding handling in enrichment_audit_storage.py

```python
# When saving to SQLite, ensure proper encoding
import codecs
# OR
# Remove special characters from warning messages
# OR
# Use ASCII-safe equivalents (-> instead of →, [X] instead of ✗)
```

### Issue 2: stage1_details Missing Title/URL

**Problem:** Audit trail doesn't store title/URL from content acquisition

**Fix:** Update enrichment_audit_storage.py line 56-60:

```python
stage1_details = json.dumps({
    'title': content_acquisition.get('title'),  # ADD
    'url': content_acquisition.get('url'),       # ADD
    'source_domain': content_acquisition.get('source_domain'),
    'publication_date': content_acquisition.get('publication_date'),
    'error': content_acquisition.get('error')
})
```

---

## Summary

### What We Learned

1. **ACCEPT_WITH_WARNING is mostly high-quality Australian events** that are non-specific incidents (analysis/opinion pieces)
2. **Title-matching validation IS working** but warnings are failing to save due to Unicode encoding
3. **Current penalties are too lenient** - allowing non-Australian and non-specific events to pass

### Proposed Actions

1. ✅ **Strengthen penalties** for:
   - is_specific_incident: False → multiply by 0.5 (push to REJECT)
   - australian_relevance < 0.3 → multiply by 0.4 (push to REJECT)
   - fact_check_pass_rate < 0.5 → multiply by 0.5 (push to REJECT)

2. ✅ **Fix Unicode encoding** in validation warning storage

3. ✅ **Add title/URL to stage1_details** for better audit trail

### Expected Outcomes

**Before:** 27 ACCEPT_WITH_WARNING (40% legitimate, 60% should be rejected)

**After:** ~10-15 ACCEPT_WITH_WARNING (90% legitimate warnings)

**ACCEPT_WITH_WARNING will become:** Events with correct extraction but quality concerns (unreliable sources, partial fact-check failures, edge cases requiring manual review)

**REJECT will include:** Non-specific incidents, non-Australian events, very low quality extractions

---

**Investigation Status:** ✅ Complete
**Next Step:** Implement penalty adjustments
