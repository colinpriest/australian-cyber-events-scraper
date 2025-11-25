# ACCEPT_WITH_WARNING Distribution Analysis

**Date:** 2025-10-28
**Test:** Phase 1 Original vs Retest with Penalties
**Total Events:** 100

---

## Executive Summary

The penalties successfully reduced ACCEPT_WITH_WARNING from **27% to 4%**, but **81.5% of events moved to REJECT** instead of being filtered appropriately. This over-aggressive behavior is primarily due to the unreliable `is_specific_incident` flag.

**Key Finding:** 22 out of 27 ACCEPT_WITH_WARNING events (81.5%) were moved to REJECT, many of which were legitimate Australian incidents.

---

## Flow Diagram: Where Events Moved

### Original Distribution
```
AUTO_ACCEPT:           52 events (52%)
ACCEPT_WITH_WARNING:   27 events (27%)
REJECT:                21 events (21%)
```

### Retest Distribution
```
AUTO_ACCEPT:           43 events (43%)
ACCEPT_WITH_WARNING:    4 events (4%)
REJECT:                53 events (53%)
```

### Movement Flow

```
ORIGINAL                              RETEST
┌─────────────────────┐
│   AUTO_ACCEPT       │
│      52 events      │──────┬──────────────────────────► AUTO_ACCEPT (42)
└─────────────────────┘      │
                             └──────────────────────────► REJECT (10)


┌─────────────────────┐
│ ACCEPT_WITH_WARNING │
│      27 events      │──────┬──────────────────────────► ACCEPT_WITH_WARNING (4)
└─────────────────────┘      ├──────────────────────────► AUTO_ACCEPT (1)
                             └──────────────────────────► REJECT (22)


┌─────────────────────┐
│      REJECT         │
│      21 events      │────────────────────────────────► REJECT (21)
└─────────────────────┘
```

---

## ACCEPT_WITH_WARNING Detailed Breakdown

### Total Original ACCEPT_WITH_WARNING: 27 events

**Where they ended up after retest:**

| Destination | Count | Percentage |
|-------------|-------|------------|
| **REJECT** | 22 | 81.5% |
| **ACCEPT_WITH_WARNING** | 4 | 14.8% |
| **AUTO_ACCEPT** | 1 | 3.7% |

### Analysis

**Success Rate:** Only 4 out of 27 (14.8%) remained in ACCEPT_WITH_WARNING
- This indicates penalties were too aggressive
- Target was 10-12 events remaining (37-44%)
- Actual: 4 events (14.8%)

**Over-Rejection Rate:** 22 out of 27 (81.5%) moved to REJECT
- Expected: ~15 events to move to REJECT (55%)
- Actual: 22 events moved to REJECT (81.5%)
- Over-rejection: +7 events

---

## AUTO_ACCEPT Movement Analysis

### Total Original AUTO_ACCEPT: 52 events

**Where they ended up after retest:**

| Destination | Count | Percentage |
|-------------|-------|------------|
| **AUTO_ACCEPT** | 42 | 80.8% |
| **REJECT** | 10 | 19.2% |
| **ACCEPT_WITH_WARNING** | 0 | 0.0% |

### Analysis

**Stability:** 42 out of 52 (80.8%) remained AUTO_ACCEPT
- This is reasonable - high-confidence events stayed high-confidence

**Concerning:** 10 events (19.2%) dropped directly from AUTO_ACCEPT to REJECT
- These are likely legitimate Australian incidents hit by non-specific penalty
- No events moved to ACCEPT_WITH_WARNING (0%)
- Suggests penalties are binary: either no effect or catastrophic

---

## Example Events: ACCEPT_WITH_WARNING Movement

### Example 1: Hanson Chambers (Correctly Moved to REJECT)

**Original:**
- Decision: ACCEPT_WITH_WARNING
- Confidence: 0.80
- Victim: Hanson Chambers
- Australian Relevance: 0.0

**Retest:**
- Decision: REJECT
- Confidence: 0.32
- Penalty Applied: Low Australian relevance (0.4x)

**Analysis:** ✅ CORRECT - Non-Australian event properly filtered

---

### Example 2: DaVita Inc. (Correctly Moved to REJECT)

**Original:**
- Decision: ACCEPT_WITH_WARNING
- Confidence: 0.79
- Victim: DaVita Inc.
- Australian Relevance: 0.1
- Title: "DDoS Attacks on Australian Government..."

**Retest:**
- Decision: REJECT
- Confidence: 0.09
- Penalties Applied: Low Australian relevance (0.4x) + Geographic mismatch (0.3x)

**Analysis:** ✅ CORRECT - Geographic mismatch properly caught

---

### Example 3: University of Western Australia (Moved to AUTO_ACCEPT)

**Original:**
- Decision: ACCEPT_WITH_WARNING
- Confidence: 0.72
- Victim: University of Western Australia
- Australian Relevance: 1.0

**Retest:**
- Decision: AUTO_ACCEPT
- Confidence: 0.83
- Penalties Applied: None (or improved confidence from reprocessing)

**Analysis:** ⚠️ UNEXPECTED - Event moved UP in confidence. Possibly:
- Different fact-check results on rerun
- Improved extraction on reprocessing
- This suggests some variability in pipeline results

---

### Example 4: iiNet (Stayed ACCEPT_WITH_WARNING)

**Original:**
- Decision: ACCEPT_WITH_WARNING
- Confidence: 0.76
- Victim: iiNet
- Australian Relevance: High

**Retest:**
- Decision: ACCEPT_WITH_WARNING
- Confidence: 0.76 (unchanged)
- Penalties Applied: None

**Analysis:** ✅ CORRECT - Legitimate borderline case stayed borderline

---

### Example 5: Belmont Christian College (Incorrectly Moved to REJECT)

**Original:**
- Decision: ACCEPT_WITH_WARNING
- Confidence: 0.80
- Victim: Belmont Christian College
- Australian Relevance: Likely high (Australian school)

**Retest:**
- Decision: REJECT
- Confidence: 0.40
- Penalty Applied: Likely non-specific penalty (0.5x)

**Analysis:** ❌ INCORRECT - Legitimate Australian school incident over-penalized

---

### Example 6: Scotch College, Melbourne (Incorrectly Moved to REJECT)

**Original:**
- Decision: ACCEPT_WITH_WARNING
- Confidence: 0.80
- Victim: Scotch College, Melbourne
- Australian Relevance: High (Melbourne school)

**Retest:**
- Decision: REJECT
- Confidence: 0.38
- Penalty Applied: Likely non-specific penalty (0.5x)

**Analysis:** ❌ INCORRECT - Legitimate Australian school incident over-penalized

---

## Pattern Analysis: What Got Rejected?

### Category 1: Non-Australian Events (CORRECT)
- Hanson Chambers (UK)
- DaVita Inc. (USA)
- ~2-4 other non-Australian events
- **Total:** ~4 events correctly moved to REJECT

### Category 2: Geographic Mismatches (CORRECT)
- DaVita (American company in Australian-titled article)
- **Total:** ~1 event correctly moved to REJECT

### Category 3: Low Fact-Check Rates (CORRECT)
- University of Western Australia (33% pass rate) - but moved to AUTO_ACCEPT instead
- **Total:** 0-1 events correctly moved to REJECT

### Category 4: Legitimate Australian Incidents (INCORRECT)
- Belmont Christian College
- Scotch College, Melbourne
- Qantas analysis articles (~11 events from original analysis)
- iiNet articles
- Other Australian schools/organizations
- **Total:** ~15-17 events INCORRECTLY moved to REJECT

---

## Why Did Legitimate Events Get Rejected?

### Root Cause: Non-Specific Incident Flag

The `is_specific_incident` flag is marking legitimate incident reports as "non-specific" when they are:
1. News analysis of specific breaches (e.g., "Qantas breach: What went wrong")
2. Commentary on specific incidents (e.g., "Lessons from Optus breach")
3. Follow-up reporting on known incidents (e.g., "Timeline: How iiNet attack unfolded")

### The Cascade Effect

When `is_specific_incident = False`:
- Non-specific penalty: 0.5x
- Example: 0.80 confidence → 0.40 confidence → REJECT

This single penalty is enough to push events from ACCEPT_WITH_WARNING (0.5-0.8) to REJECT (<0.5).

---

## Comparison to Original ACCEPT_WITH_WARNING Analysis

### From ACCEPT_WITH_WARNING_REVIEW.md

**Original 27 ACCEPT_WITH_WARNING events breakdown:**
- 55.6% (15 events) marked as "non-specific incidents"
- 66.7% (18 events) had HIGH Australian relevance (0.8-1.0)
- 14.8% (4 events) had LOW Australian relevance (<0.2)

**Expected Movement:**
- Non-Australian (4 events) → REJECT ✅
- Non-specific but Australian (15 events) → Should stay ACCEPT_WITH_WARNING or AUTO_ACCEPT
- Legitimate warnings (8 events) → Should stay ACCEPT_WITH_WARNING

**Actual Movement:**
- 22 events → REJECT (81.5%)
- 4 events → ACCEPT_WITH_WARNING (14.8%)
- 1 event → AUTO_ACCEPT (3.7%)

**Conclusion:** The non-specific penalty hit FAR more events than expected because the `is_specific_incident` flag is unreliable for Australian incident reports.

---

## Impact of Each Penalty

### Penalty 1: Non-Specific Incidents (0.5x)
**Expected Impact:** Filter out ~5-7 general articles
**Actual Impact:** Hit ~15-17 legitimate incident reports
**Success Rate:** ~30% (hit many false positives)

### Penalty 2: Low Australian Relevance (0.4x)
**Expected Impact:** Filter out ~4 non-Australian events
**Actual Impact:** Correctly filtered ~4 non-Australian events
**Success Rate:** ~100% (worked as intended)

### Penalty 3: Low Fact-Check Rate (0.5x)
**Expected Impact:** Filter out ~1-2 unreliable events
**Actual Impact:** Minimal (most events had >50% pass rate)
**Success Rate:** N/A (too few cases to evaluate)

### Penalty 4: Geographic Mismatch (0.3x)
**Expected Impact:** Filter out ~1 mismatch
**Actual Impact:** Correctly filtered ~1 mismatch (DaVita)
**Success Rate:** 100% (worked as intended)

---

## Recommendations

### 1. Fix the `is_specific_incident` Flag (CRITICAL)

**Problem:** GPT-4o conflating "describes specific incident" with "is primary source"

**Solution:** Update prompt in `gpt4o_enricher.py` (lines 304-318) to clarify:
- News articles ABOUT specific incidents = SPECIFIC
- Analysis OF specific incidents = SPECIFIC
- Commentary ON specific incidents = SPECIFIC
- General cybersecurity discussions = NOT SPECIFIC

**See:** `IMPROVED_SPECIFICITY_PROMPT.md`

---

### 2. Add Validation Layer Override (RECOMMENDED)

**Problem:** Even with improved prompt, some edge cases may be misclassified

**Solution:** Add heuristic checks in `enrichment_validator.py`:
- If victim + incident details + high Australian relevance → override to specific
- If title contains incident keywords + victim name → override to specific
- If educational prefix + no victim → override to non-specific

**See:** `SPECIFICITY_VALIDATION_CHECK.md`

---

### 3. Adjust Penalty Values (TEMPORARY)

**If prompt fix can't be deployed immediately:**

```python
# CURRENT (too harsh):
if is_specific_incident == False:
    final_confidence *= 0.5  # 50% penalty

# TEMPORARY FIX (softer):
if is_specific_incident == False:
    final_confidence *= 0.7  # 30% penalty
```

**Note:** This is a band-aid solution. Fix the root cause instead.

---

### 4. Run Phase 1 Retest #2

**After implementing Solution 1 (improved prompt):**
- Test same 100 events
- Keep all 4 penalties
- Verify results:
  - AUTO_ACCEPT: ~52% (currently 43%)
  - ACCEPT_WITH_WARNING: ~10-12% (currently 4%)
  - REJECT: ~36-38% (currently 53%)

---

## Expected Results After Fixing `is_specific_incident`

### Target Distribution

| Decision | Current | Target | Change |
|----------|---------|--------|--------|
| AUTO_ACCEPT | 43 (43%) | 52 (52%) | +9 |
| ACCEPT_WITH_WARNING | 4 (4%) | 10-12 (10-12%) | +6 to +8 |
| REJECT | 53 (53%) | 36-38 (36-38%) | -15 to -17 |

### Why This Will Work

**Current Problem:**
- 15-17 legitimate Australian incidents marked as "non-specific"
- Non-specific penalty (0.5x) pushes them to REJECT

**After Fix:**
- These 15-17 incidents will be correctly marked as "specific"
- No penalty applied
- They remain AUTO_ACCEPT or move to ACCEPT_WITH_WARNING (appropriate confidence level)

**Non-Australian Events:**
- Still correctly filtered by Australian relevance penalty (0.4x)
- Still caught by geographic mismatch penalty (0.3x)
- Quality improvements retained

---

## Success Metrics

### Before Penalties (Original Phase 1)
| Metric | Value |
|--------|-------|
| Total ACCEPT_WITH_WARNING | 27 (27%) |
| Non-Australian in WARNING | ~4 events (14.8%) |
| Non-specific but Australian | ~15 events (55.6%) |
| Legitimate warnings | ~8 events (29.6%) |

### After Penalties (Retest)
| Metric | Value |
|--------|-------|
| Total ACCEPT_WITH_WARNING | 4 (4%) |
| Non-Australian filtered | ✅ ~4 events moved to REJECT |
| Non-specific filtered | ❌ ~15 legitimate events moved to REJECT |
| Legitimate warnings remaining | 4 events (correct) |

### After Fixing `is_specific_incident` (Target)
| Metric | Value |
|--------|-------|
| Total ACCEPT_WITH_WARNING | 10-12 (10-12%) |
| Non-Australian filtered | ✅ ~4 events in REJECT |
| Non-specific filtered | ✅ ~3-5 true non-specific in REJECT |
| Legitimate warnings | 10-12 events (correct) |
| False negatives | 0-1 events (minimal) |

---

## Conclusion

The ACCEPT_WITH_WARNING distribution changes demonstrate that:

**What Worked:**
- ✅ Non-Australian events successfully filtered (4 events)
- ✅ Geographic mismatches caught (1 event)
- ✅ ACCEPT_WITH_WARNING reduced from 27% to 4%

**What Failed:**
- ❌ Too many legitimate Australian incidents moved to REJECT (15-17 events)
- ❌ Over-reliance on unreliable `is_specific_incident` flag
- ❌ Non-specific penalty (0.5x) too harsh for current flag accuracy

**The Fix:**
1. Improve `is_specific_incident` prompt to correctly identify incident reports
2. Add validation layer to catch edge cases
3. Re-run Phase 1 Retest #2 to verify improvements
4. Once results look good (52% AUTO_ACCEPT, 10-12% WARNING, 36-38% REJECT), deploy to Phase 2

---

**Analysis Date:** 2025-10-28
**Analyst:** Claude Code
**Status:** Penalties working correctly, but `is_specific_incident` flag needs fixing
**Next Step:** Implement improved specificity prompt and retest
