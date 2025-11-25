# Phase 1 Retest Comparison Plan

**Date:** 2025-10-28
**Status:** ✅ **COMPLETE** (Retest successful - see PHASE1_RETEST_COMPARISON_REPORT.md)

---

## Purpose

Re-run the original Phase 1 test (100 events) with all victim misidentification fixes applied to measure concrete improvement metrics.

---

## What Changed Between Phase 1 Original and Retest

### Fixes Applied (4 components)

1. **Content Acquisition Enhancement** (content_acquisition.py:128-129)
   - Now passes event title from database to GPT-4o instead of webpage-extracted title
   - Ensures GPT-4o sees the correct title for aggregate blog posts

2. **GPT-4o Prompt - Few-Shot Examples** (gpt4o_enricher.py:69-170)
   - Added 3 concrete examples showing correct extraction for aggregate articles
   - Examples directly mirror the problematic cases from Phase 1
   - Teaches GPT-4o to prioritize title over article body

3. **Validation - Title Matching** (enrichment_validator.py:171-242)
   - Added validation to flag when extracted victim doesn't appear in title
   - Detects aggregate URLs (blog/, update, roundup, etc.)
   - Provides detailed warnings for manual review

4. **Validation - Generic Name Detection** (enrichment_validator.py:127-128)
   - Enhanced patterns to catch "Dutch telecommunications companies" and similar plurals
   - Rejects generic organization names as errors

**Total Changes:** 180 lines added/modified across 4 files

---

## Metrics to Compare

### Primary Metrics

| Metric | Phase 1 Original | Phase 1 Retest | Expected Improvement |
|--------|------------------|----------------|----------------------|
| **Victims Identified** | 52 (52%) | ? | ≥52% (same or better) |
| **Victim Misidentification Rate** | ~10% (10 events) | ? | <5% (50% reduction) |
| **Generic Names Passed** | 2 ("Dutch telecoms") | ? | 0 (100% reduction) |
| **Title Mismatches Detected** | 0 (no validation) | ? | 100% detection |
| **AUTO_ACCEPT** | 40 (40%) | ? | ≥40% (maintain quality) |
| **ACCEPT_WITH_WARNING** | 27 (27%) | ? | Similar or less |
| **REJECT** | 33 (33%) | ? | Similar or more |

### Quality Metrics

| Metric | Phase 1 Original | Phase 1 Retest | Expected |
|--------|------------------|----------------|----------|
| **Average Confidence** | 0.59 | ? | ≥0.59 |
| **Fact-Check Pass Rate** | 82.5% | ? | ≥80% |
| **Clean Events (no issues)** | 67 (67%) | ? | ≥67% |
| **Validation Errors** | 14 | ? | ≤14 (ideally lower) |
| **Validation Warnings** | 21 | ? | May increase (better detection) |

### Performance Metrics

| Metric | Phase 1 Original | Phase 1 Retest | Expected |
|--------|------------------|----------------|----------|
| **Processing Time** | 39.3 minutes | ? | ~40 minutes (similar) |
| **Average Time/Event** | 23.6 seconds | ? | ~24 seconds |
| **Cost** | $14.00 | ? | ~$14 (same) |
| **Success Rate** | 100% | ? | 100% |

---

## Specific Cases to Verify Fixed

### Known Problematic Events from Phase 1

| Event # | Title | Original Result | Expected Retest Result |
|---------|-------|-----------------|------------------------|
| **Event 5** | "Casino gaming company Bragg..." | Extracted: Optus ❌ | Extract: Bragg OR warn about mismatch ✅ |
| **Event 7** | "Aussie ISP iiNet confirms..." | Extracted: iiNet ✅ | Extract: iiNet ✅ (verify consistency) |
| **Event 8** | "Australia's TPG Telecom... iiNet..." | Extracted: Dutch telecoms ❌ | Extract: iiNet OR TPG ✅ |
| **Event 9** | "iiNet Data Breach..." | Extracted: Dutch telecoms ❌ | Extract: iiNet ✅ |
| **Event 10** | "iiNet Data Breach Exposes..." | Extracted: Optus ❌ | Extract: iiNet ✅ |

**Expected:** 4/5 of these problematic events should now extract correctly

---

## Top Victims - Consistency Check

### Phase 1 Original Top Victims

| Victim | Count | Notes |
|--------|-------|-------|
| Qantas | 10 | Should remain consistent |
| Optus | 6 | May decrease if misidentifications fixed |
| iiNet | 6 | Should remain or increase |
| Qantas Airways | 2 | Variant of Qantas |
| Colt | 2 | Should remain |

**Expected:** Qantas and iiNet counts should remain stable or increase. Optus count may decrease if Event 5 and Event 10 misidentifications are fixed.

---

## Validation Improvements to Verify

### Title Matching Validation

**Test:** Events where victim doesn't match title should get warnings

Expected warnings for:
- Event 5: "Bragg" in title but extracted different victim
- Any aggregate articles where extraction differs from title

### Generic Name Detection

**Test:** "Dutch telecommunications companies" should be rejected

Expected:
- Events 8-9 from original Phase 1 should now either:
  - Extract "iiNet" (correct) OR
  - Get ERROR/REJECT decision for generic name

---

## Success Criteria for Retest

### Minimum Requirements (Must Pass)

- ✅ **≥52% victim identification rate** (same as Phase 1)
- ✅ **Zero crashes** (100% processing success)
- ✅ **Cost ≤$15** (same budget)
- ✅ **Generic names rejected** (0 instances of "Dutch telecommunications companies" in AUTO_ACCEPT)

### Target Improvements (Should Achieve)

- 🎯 **<5% victim misidentification** (down from ~10%)
- 🎯 **Title mismatch detection** (100% of mismatches flagged)
- 🎯 **Validation warnings** (correctly identify problematic extractions)

### Stretch Goals (Nice to Have)

- 🌟 **>55% victim identification** (better than Phase 1)
- 🌟 **>45% AUTO_ACCEPT** (higher confidence)
- 🌟 **<30% REJECT** (fewer events discarded)

---

## Analysis Plan

Once retest completes, we will:

1. **Generate comparison report** comparing all metrics side-by-side
2. **Analyze specific problematic events** (Events 5, 7, 8, 9, 10) to verify fixes
3. **Review validation warnings** to ensure quality checks are working
4. **Check top victims list** for unexpected changes
5. **Calculate improvement percentages** for all key metrics

---

## Files to Compare

### Original Phase 1 Files
- `PHASE1_RESULTS_SUMMARY.md` - Comprehensive summary
- `batch_enrichment_results_20251028_105131.json` - Detailed JSON results
- `PHASE1_TEST_REPORT_20251028_105131.txt` - Text report

### Retest Files (Will be generated)
- `PHASE1_RETEST_COMPARISON_REPORT.md` - New comprehensive comparison
- `batch_enrichment_results_[timestamp].json` - New JSON results
- `PHASE1_TEST_REPORT_[timestamp].txt` - New text report

---

## Timeline

- **Retest Start:** 2025-10-28 14:47 (running now)
- **Estimated Completion:** ~15:27 (40 minutes)
- **Analysis Time:** ~15 minutes
- **Report Generation:** ~10 minutes
- **Total:** ~65 minutes from start to final report

---

## Expected Outcome

Based on the targeted test fixes (3/3 passed), we expect:

**Best Case:**
- 10% misidentification rate → <2% (80% reduction)
- 0 generic names in AUTO_ACCEPT
- All title mismatches flagged
- ≥55% victim identification

**Realistic Case:**
- 10% misidentification rate → ~5% (50% reduction)
- 0-1 generic names in AUTO_ACCEPT
- 90%+ title mismatches flagged
- ~52% victim identification (maintained)

**Minimum Acceptable:**
- 10% misidentification rate → ≤8% (20% reduction)
- Validation catches misidentifications (even if extraction isn't perfect)
- No regression in victim identification rate

---

**Status:** ✅ COMPLETE (100/100 events processed)

**Results Summary:**
- AUTO_ACCEPT: 52 (52%) - up from 40 (40%) = **+30% improvement**
- REJECT: 21 (21%) - down from 33 (33%) = **-36% improvement**
- Average confidence: 0.68 - up from 0.59 = **+15% improvement**
- Victim identification: 79% - up from 52% = **+52% improvement**
- Generic names eliminated: 0 instances (down from 2) = **100% success**
- Problematic events fixed: 3/4 = **75% success rate**

**See:** PHASE1_RETEST_COMPARISON_REPORT.md for full analysis
