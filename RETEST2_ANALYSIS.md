# Phase 1 Retest #2 Analysis - Critical Findings

**Date:** 2025-10-28
**Status:** ⚠️ UNEXPECTED RESULTS - Improved prompt not working as intended

---

## Executive Summary

Phase 1 Retest #2 with the improved specificity prompt showed **mixed results** - better than Retest #1, but still worse than the original:

| Test | AUTO_ACCEPT | WARNING | REJECT | Total Accept |
|------|-------------|---------|--------|--------------|
| **Original (No Penalties)** | 52% | 27% | 21% | **79%** ✅ Best |
| **Retest #1 (Old Prompt)** | 43% | 4% | 53% | **47%** ❌ Worst |
| **Retest #2 (New Prompt)** | 37% | 23% | 40% | **60%** ⚠️ Mediocre |

**Key Finding:** The improved prompt did NOT solve the problem as expected.

---

## Critical Discovery: GPT-4o Still Marking Specific Incidents as Non-Specific

### Event #9 Case Study: TPG/iiNet Cyber Incident

**Title:** "Australia's TPG Telecom flags cyber incident in its iiNet system"

**Results Across Tests:**
| Test | Decision | Confidence | is_specific |
|------|----------|------------|-------------|
| Original (No Penalties) | AUTO_ACCEPT | 0.86 | Likely True |
| Retest #1 (Old Prompt + Penalties) | REJECT | 0.41 | False |
| **Retest #2 (New Prompt + Penalties)** | **REJECT** | **0.40** | **False** ❌ |

**Analysis:** Even with the improved prompt, GPT-4o STILL classified this legitimate news article about a specific iiNet cyber incident as "non-specific". The new prompt failed to fix this misclassification.

---

## Validation Override Performance

**Total Validation Overrides Applied:** 40 out of 100 events (40%)

**What This Means:**
- The validation layer is working (catching 40% of misclassifications)
- BUT GPT-4o is still making mistakes on 40% of events
- The improved prompt did NOT reduce the misclassification rate significantly

**Why Event #9 Wasn't Saved:**
- Event #9 was marked `is_specific = False` by GPT-4o
- The validation layer apparently didn't override it
- Possible reasons:
  - Australian relevance score < 0.5 (didn't trigger Rule 2)
  - No concrete details extracted (didn't trigger Rule 1)
  - Title truncation issue ("..." at end)
  - Validation rule thresholds too strict

---

## Comparison: What Changed Between Tests?

### From Retest #1 to Retest #2

**Positive Changes:**
- ✅ REJECT rate improved: 53% → 40% (-13 events)
- ✅ ACCEPT_WITH_WARNING recovered: 4% → 23% (+19 events)
- ✅ Validation overrides working (40% of events)

**Negative Changes:**
- ❌ AUTO_ACCEPT dropped: 43% → 37% (-6 events)
- ❌ Overall acceptance still low: 60% (vs 79% original)
- ❌ Improved prompt didn't reduce misclassifications significantly

### Event Movement Analysis

| From → To | Count | Explanation |
|-----------|-------|-------------|
| REJECT → ACCEPT_WITH_WARNING | ~19 | Validation overrides rescued some events |
| AUTO_ACCEPT → ACCEPT_WITH_WARNING | ~6 | Some events downgraded (possibly due to other penalties) |
| REJECT → REJECT | ~21 | Still rejected (non-Australian, truly non-specific) |

---

## Why the Improved Prompt Didn't Work

### Hypothesis 1: GPT-4o Ignoring Examples

The improved prompt includes explicit examples like:
- ✓ "Experts analyze the iiNet credential stuffing attack" (expert commentary on specific incident)

Yet GPT-4o still marked "Australia's TPG Telecom flags cyber incident in its iiNet system" as non-specific.

**Possible Reasons:**
- GPT-4o not reading/following the examples carefully
- The prompt is too long (may be truncated or ignored)
- GPT-4o's internal biases override the prompt

### Hypothesis 2: Prompt Ambiguity

The prompt says:
> "Focus on WHETHER an incident is described, NOT on the article's style or purpose"

But GPT-4o may still be:
- Focusing on the word "flags" (future tense, not concrete)
- Seeing "TPG Telecom" as the primary subject, not "iiNet"
- Interpreting "flags" as "announces" (non-specific announcement)

### Hypothesis 3: Content vs Title Mismatch

Event #9 might have:
- Title mentions "TPG Telecom flags cyber incident"
- Content discusses multiple incidents or general cybersecurity
- GPT-4o correctly identifying content as non-specific, but we're judging by title

---

## Validation Layer Analysis

### Why Validation Saved Some Events (40%)

Validation Rule 2 triggers when:
- `is_specific == False` AND
- Title contains incident keywords ("breach", "attack", "incident", etc.) AND
- Victim name in title AND
- NOT aggregate URL AND
- Australian relevance > 0.5

**Events Saved by Validation:**
- Likely had clear incident keywords in title
- Victim clearly mentioned
- High Australian relevance (> 0.5)

### Why Validation Didn't Save Event #9

Event #9 should have triggered Rule 2:
- Title: "Australia's TPG Telecom flags cyber incident in its iiNet system"
- Contains "incident" keyword ✓
- Contains victim "iiNet" ✓
- Likely high Australian relevance ✓

**Possible Failures:**
1. **Australian relevance < 0.5:** GPT-4o may have extracted low relevance
2. **Victim mismatch:** Validation looked for "iiNet" but title has "TPG Telecom" first
3. **Aggregate URL detection:** URL might have matched aggregate patterns
4. **Title truncation:** Title shows "..." which might have broken matching

---

## Root Cause Analysis

The fundamental problem remains:
1. **GPT-4o's classification is still unreliable** despite improved prompt
2. **Validation layer catches ~40%** but misses critical cases like Event #9
3. **Penalties are working correctly** but penalizing events that shouldn't be marked non-specific

**The Issue:** We're treating the symptom (adding validation overrides) instead of the root cause (GPT-4o misclassifying incidents).

---

## Recommendations

### Option 1: Remove Non-Specific Penalty (Immediate Fix)

**Action:** Disable the `is_specific_incident = False` penalty (0.5x multiplier)

**Rationale:**
- The flag is unreliable (40% misclassification rate)
- Even with improved prompt + validation, it's still catching legitimate events
- Better to accept some non-specific events than reject legitimate incidents

**Expected Impact:**
- AUTO_ACCEPT would increase from 37% → ~50-55%
- ACCEPT_WITH_WARNING would decrease slightly
- REJECT would drop from 40% → ~25-30%
- Close to original 79% acceptance rate

**Tradeoff:** Will accept more general discussions/analysis articles that aren't about specific incidents.

---

### Option 2: Soften Non-Specific Penalty

**Action:** Change penalty from 0.5x to 0.8x

**Rationale:**
- Still penalize non-specific events, but less aggressively
- Allows marginally non-specific events to survive if they have other strong signals

**Expected Impact:**
- AUTO_ACCEPT: 37% → ~45%
- ACCEPT_WITH_WARNING: 23% → ~18%
- REJECT: 40% → ~37%

**Code Change:**
```python
# BEFORE:
if is_specific_incident == False:
    final_confidence *= 0.5  # 50% penalty

# AFTER:
if is_specific_incident == False:
    final_confidence *= 0.8  # 20% penalty
```

---

### Option 3: Improve Validation Rules

**Action:** Make validation rules more aggressive to catch more misclassifications

**Possible Changes:**
1. Lower Australian relevance threshold for Rule 2: 0.5 → 0.3
2. Add more incident keywords: "flags", "reports", "confirms", "discloses"
3. Check both victim and attacker names in title
4. Allow partial victim name matching ("TPG" should match "iiNet" if both are in same org)

**Expected Impact:**
- Validation overrides: 40% → 50-60%
- Would save more events like Event #9
- But relies on heuristics (fragile)

---

### Option 4: Use Different LLM Model

**Action:** Test with GPT-4 Turbo or Claude 3.5 Sonnet for specificity classification

**Rationale:**
- GPT-4o may not be the best model for this classification task
- Different models have different biases
- Claude 3.5 Sonnet might better understand "analysis OF specific incident = specific"

**Implementation:**
- Add model parameter to `gpt4o_enricher.py`
- Test same 100 events with different model
- Compare is_specific classifications

---

### Option 5: Two-Stage Classification

**Action:**
1. First, ask: "What is the name of the specific organization/incident discussed?"
2. Then ask: "Is this a specific incident or general discussion?"

**Rationale:**
- Forces GPT-4o to identify the concrete subject first
- Makes it harder to misclassify articles about specific incidents

**Example Prompt:**
```
STAGE 1: Identify the primary subject
Q: "What is the main organization or incident this article discusses?"
A: [GPT-4o extracts]

STAGE 2: Classify specificity
Q: "Given that this article discusses [ORG/INCIDENT], does it describe a specific cyber incident?"
A: [GPT-4o classifies]
```

---

## Next Steps

### Immediate Action Required

**Decision Point:** Which option to pursue?

1. **Quick Fix (Option 1):** Remove non-specific penalty
   - Pros: Immediate ~79% acceptance rate, no code changes
   - Cons: Accept some non-specific events

2. **Balanced Fix (Option 2):** Soften penalty to 0.8x
   - Pros: Still filter most non-specific, less aggressive
   - Cons: Still misses some legitimate events

3. **Validation Fix (Option 3):** Improve validation rules
   - Pros: Catches more misclassifications
   - Cons: Maintenance burden, fragile heuristics

4. **Model Fix (Option 4):** Test different LLM
   - Pros: May fix root cause
   - Cons: Cost, API changes, uncertain outcome

5. **Prompt Fix (Option 5):** Two-stage classification
   - Pros: Forces clearer reasoning
   - Cons: Double API calls, higher cost

### Recommended Approach

**Short-term:** Option 2 (Soften Penalty) + Option 3 (Improve Validation)
- Change penalty from 0.5x → 0.8x
- Lower Australian relevance threshold from 0.5 → 0.3 in Rule 2
- Add more incident keywords to validation

**Medium-term:** Option 5 (Two-Stage Classification)
- Test on subset of events
- Measure improvement in is_specific accuracy
- Deploy if results are significantly better

**Long-term:** Option 4 (Test Different Models)
- Evaluate Claude 3.5 Sonnet for enrichment
- Compare cost/quality tradeoffs
- Consider model ensemble (use multiple models for critical decisions)

---

## Success Criteria for Next Iteration

**Minimum Acceptable Results:**
- AUTO_ACCEPT: ≥ 45%
- ACCEPT_WITH_WARNING: 10-20%
- REJECT: ≤ 40%
- Total Acceptance: ≥ 65%

**Target Results:**
- AUTO_ACCEPT: 50-52%
- ACCEPT_WITH_WARNING: 10-12%
- REJECT: 36-38%
- Total Acceptance: 60-64%

**Ideal Results:**
- AUTO_ACCEPT: 50-55%
- ACCEPT_WITH_WARNING: 20-25%
- REJECT: 20-30%
- Total Acceptance: 70-80%

---

## Appendix: Sample Events Analysis

### Events That Improved

**Event #1 (iiNet):**
- Retest #1: REJECT (0.41)
- Retest #2: AUTO_ACCEPT (0.88) ✅
- Validation override applied successfully

**Event #8 (iiNet):**
- Retest #1: REJECT (unknown)
- Retest #2: ACCEPT_WITH_WARNING (0.74) ✅
- Moved from reject to warning zone

### Events That Stayed Rejected

**Event #2 (Hanson Chambers - UK):**
- Retest #1: REJECT (0.32)
- Retest #2: REJECT (0.15) ✅
- Correctly rejected (non-Australian)

**Event #9 (TPG/iiNet):**
- Retest #1: REJECT (0.41)
- Retest #2: REJECT (0.40) ❌
- Should be AUTO_ACCEPT (legitimate Australian incident)

### Events That Regressed

**Event #5 (Optus):**
- Original: AUTO_ACCEPT (likely 0.85+)
- Retest #2: AUTO_ACCEPT (0.99) ✅
- Actually improved (stayed high)

---

**Analysis Complete:** 2025-10-28 19:53
**Status:** Awaiting decision on next steps
**Recommendation:** Implement Option 2 + Option 3 for Phase 1 Retest #3
