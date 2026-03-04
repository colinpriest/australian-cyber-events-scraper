# Deduplication Stage

This document provides a detailed specification of the deduplication stage (Phase 3) of the Australian Cyber Events pipeline. It covers the matching rules, similarity algorithms, entity resolution, merge logic, LLM arbiter prompts, records validation, incremental processing, and database schema in sufficient detail for independent review and replication.

---

## Table of Contents

1. [Purpose and Scope](#1-purpose-and-scope)
2. [High-Level Data Flow](#2-high-level-data-flow)
3. [Core Design Principle](#3-core-design-principle)
4. [Matching Rules](#4-matching-rules)
   - 4.1 [Rule 1: Same Entity + Same Date](#41-rule-1-same-entity--same-date)
   - 4.2 [Exact Title + Date Match](#42-exact-title--date-match)
   - 4.3 [Rule 2a: Same Entity + Similar Titles](#43-rule-2a-same-entity--similar-titles)
   - 4.4 [Rule 2b: Same Entity + Similar Descriptions](#44-rule-2b-same-entity--similar-descriptions)
   - 4.5 [Rule 3: High Description Similarity Without Entity Match](#45-rule-3-high-description-similarity-without-entity-match)
   - 4.6 [Fallback: Weighted Composite Similarity](#46-fallback-weighted-composite-similarity)
   - 4.7 [Pre-Filters](#47-pre-filters)
5. [Entity Resolution](#5-entity-resolution)
   - 5.1 [Entity Name Normalization](#51-entity-name-normalization)
   - 5.2 [Organization Name Matching](#52-organization-name-matching)
   - 5.3 [Related Companies](#53-related-companies)
   - 5.4 [Title Keyword Fallback](#54-title-keyword-fallback)
6. [Similarity Functions](#6-similarity-functions)
   - 6.1 [Quick Title Similarity (Jaccard)](#61-quick-title-similarity-jaccard)
   - 6.2 [Weighted Composite Similarity](#62-weighted-composite-similarity)
   - 6.3 [Title Similarity (Detailed)](#63-title-similarity-detailed)
   - 6.4 [Entity Similarity](#64-entity-similarity)
   - 6.5 [Content Similarity](#65-content-similarity)
   - 6.6 [Temporal Similarity](#66-temporal-similarity)
7. [LLM Arbiter](#7-llm-arbiter)
   - 7.1 [Activation Criteria](#71-activation-criteria)
   - 7.2 [Prompt](#72-prompt)
   - 7.3 [Score Adjustment](#73-score-adjustment)
   - 7.4 [Fallback Behaviour](#74-fallback-behaviour)
8. [Merge Logic](#8-merge-logic)
   - 8.1 [Master Event Selection](#81-master-event-selection)
   - 8.2 [Field Merging Strategy](#82-field-merging-strategy)
   - 8.3 [Records Affected Validation](#83-records-affected-validation)
9. [Grouping Algorithm](#9-grouping-algorithm)
10. [Execution Modes](#10-execution-modes)
    - 10.1 [Full Rebuild](#101-full-rebuild)
    - 10.2 [Incremental Deduplication](#102-incremental-deduplication)
    - 10.3 [Mode Selection Logic](#103-mode-selection-logic)
11. [Input Validation](#11-input-validation)
12. [Database Schema](#12-database-schema)
    - 12.1 [DeduplicatedEvents Table](#121-deduplicatedevents-table)
    - 12.2 [EventDeduplicationMap Table](#122-eventdeduplicationmap-table)
    - 12.3 [DeduplicationClusters Table](#123-deduplicationclusters-table)
    - 12.4 [EntityMappings Table](#124-entitymappings-table)
13. [Storage Integrity Checks](#13-storage-integrity-checks)
14. [Configuration and Thresholds](#14-configuration-and-thresholds)
15. [Error Handling](#15-error-handling)
16. [Justification of Design Decisions](#16-justification-of-design-decisions)
17. [Replication Guide](#17-replication-guide)

---

## 1. Purpose and Scope

The deduplication stage is responsible for:

- **Identifying** enriched events that describe the same real-world cyber security incident.
- **Merging** duplicate event records into a single canonical record per incident.
- **Preserving** the most complete metadata (longest summary, earliest date, highest validated records count).
- **Tracking** merge lineage so that every source record can be traced to its deduplicated output.
- **Validating** records_affected values against organisation-specific plausibility rules.

This stage corresponds to **Phase 3** of the five-phase pipeline. It is invoked by `run_full_pipeline.py` and orchestrated by the `DeduplicationMigration` class in `scripts/run_global_deduplication.py`.

### What This Stage Does NOT Do

This stage does not perform event discovery (Phase 1), Perplexity AI enrichment (Phase 2), ASD risk classification (Phase 4), or dashboard generation (Phase 5). Those are separate pipeline phases documented elsewhere.

### Source Files

| File | Purpose |
|------|---------|
| `cyber_data_collector/processing/deduplication_v2.py` | Core engine: matching rules, similarity calculations, merging, LLM arbiter |
| `cyber_data_collector/storage/deduplication_storage.py` | Database persistence: stores deduplicated events, merge lineage, integrity checks |
| `scripts/run_global_deduplication.py` | Runner script: orchestrates full/incremental deduplication runs |
| `cyber_data_collector/utils/validation.py` | Records-affected validation with Perplexity LLM fallback |
| `run_full_pipeline.py` (Phase 3 invocation) | Unified pipeline integration |

---

## 2. High-Level Data Flow

```
┌──────────────────────────────────────────────────────────────────────┐
│                     Deduplication Phase                              │
│                                                                      │
│  ┌──────────────┐     ┌─────────────┐     ┌─────────────────────┐   │
│  │ EnrichedEvents│────▶│  Grouping   │────▶│ Merge Groups        │   │
│  │ (input)       │     │  Algorithm  │     │ (master + merged)   │   │
│  └──────────────┘     └─────────────┘     └─────────────────────┘   │
│                              │                       │               │
│                              ▼                       ▼               │
│                     ┌─────────────┐     ┌─────────────────────┐     │
│                     │Entity Match │     │ Records Validation  │     │
│                     │Rules 1-3    │     │ (rule + Perplexity) │     │
│                     └─────────────┘     └─────────────────────┘     │
│                              │                       │               │
│                              ▼                       ▼               │
│                     ┌─────────────┐     ┌─────────────────────┐     │
│                     │LLM Arbiter  │     │DeduplicatedEvents   │     │
│                     │(uncertain   │     │EventDeduplicationMap│     │
│                     │ cases only) │     │DeduplicationClusters│     │
│                     └─────────────┘     └─────────────────────┘     │
└──────────────────────────────────────────────────────────────────────┘
```

**Input:** All active `EnrichedEvents` rows, joined with `RawEvents` for description text and `perplexity_enrichment_data` JSON for entity names.

**Output:** `DeduplicatedEvents` (one row per unique incident), `EventDeduplicationMap` (merge lineage), and `DeduplicationClusters` (group metadata).

---

## 3. Core Design Principle

The pipeline tracks **cyber security incidents**, not news articles. Multiple articles, blog posts, or reports about the same breach must be merged into a single event record.

The guiding question is:

> "How many distinct security incidents occurred at each organisation?"
> — **Not** "How many news articles were published?"

**Example:** An article titled "Ticketmaster hacked" published on April 15 and another titled "Live Nation breach" published on May 20 describe the same incident. They must be merged into one event with the date April 15 (the earliest reporting date).

This principle drives several design choices:

1. Entity identity takes priority over textual similarity — two articles about the same organisation are presumed to describe the same incident unless clearly distinguishable.
2. Different publication dates do not imply different incidents.
3. The earliest date is always preserved as the canonical incident date.

---

## 4. Matching Rules

Rules are applied in strict priority order during pairwise event comparison. When a rule triggers, the pair is merged immediately and no further rules are evaluated for that pair.

### 4.1 Rule 1: Same Entity + Same Date

**Condition:** Both events have the same victim organisation (per entity resolution in Section 5) AND the same `event_date`.

**Action:** Always merge, unconditionally.

**Threshold:** None — entity match + exact date match is sufficient.

**Justification:** If the same organisation experienced a reported incident on the same date, it is overwhelmingly likely to be a single incident reported by multiple sources. False positives (two genuinely distinct incidents at the same organisation on the same date) are extremely rare in the Australian cyber event landscape.

### 4.2 Exact Title + Date Match

**Condition:** Both events have identical titles (case-insensitive, whitespace-trimmed) AND the same `event_date`.

**Action:** Always merge.

**Justification:** Identical titles with the same date are direct duplicates, typically from the same article discovered by different data sources.

### 4.3 Rule 2a: Same Entity + Similar Titles

**Condition:** Both events have the same victim organisation AND a Jaccard word similarity of their titles >= **0.15**.

**Action:** Merge.

**Threshold:** 0.15 (Jaccard word overlap).

**Justification:** Once entity identity is established, titles need only share minimal lexical overlap to confirm they describe the same incident. The threshold of 0.15 is deliberately low because:

- Entity matching has already provided strong signal that these events relate to the same organisation.
- Titles for the same incident vary significantly across sources (e.g., "Ticketmaster hacked" vs "Live Nation data breach affects millions").
- A Jaccard score of 0.15 requires at least one shared word between titles, filtering out completely unrelated incidents.
- The risk of false merges is mitigated by the entity match prerequisite — two events about the same organisation rarely describe different incidents unless they are months apart.

### 4.4 Rule 2b: Same Entity + Similar Descriptions

**Condition:** Both events have the same victim organisation, both have non-null `description` fields, AND a Jaccard word similarity of their descriptions >= **0.20**.

**Action:** Merge.

**Threshold:** 0.20 (Jaccard word overlap on descriptions).

**Justification:** This catches cases where titles are completely different but the full scraped article content clearly describes the same incident. A slightly higher threshold than Rule 2a is used because descriptions are longer and noisier than titles. The `description` field contains the raw scraped text from the source article, providing richer content for comparison.

### 4.5 Rule 3: High Description Similarity Without Entity Match

**Condition:** Both events have non-null `description` fields, Jaccard word similarity of descriptions >= **0.35**, AND events are within **90 days** of each other.

**Action:** Merge.

**Threshold:** 0.35 (Jaccard word overlap on descriptions) + 90-day window.

**Justification:** This rule handles cases where entity extraction failed for one or both events (e.g., `victim_organization_name` is NULL) but the scraped content clearly describes the same incident. The threshold is more than double the same-entity threshold (0.35 vs 0.15/0.20) because without entity confirmation there is higher false-positive risk. The 90-day temporal constraint provides additional protection against merging unrelated events that happen to share vocabulary.

### 4.6 Fallback: Weighted Composite Similarity

**Condition:** None of the above rules triggered, the title Jaccard similarity is >= 0.3, dates are within 365 days, AND the weighted composite similarity score is >= **0.75**.

**Action:** Merge.

**Threshold:** 0.75 (composite score, see Section 6.2 for weights).

**Justification:** This is the general-purpose fallback for event pairs that do not trigger the entity-based rules. The 0.75 threshold is conservative, requiring strong agreement across multiple similarity dimensions. In practice, most merges are handled by Rules 1-3; this fallback catches the remaining edge cases.

### 4.7 Pre-Filters

Before evaluating the fallback composite score, two cheap pre-filters avoid unnecessary computation:

| Pre-filter | Condition | Action |
|------------|-----------|--------|
| Date distance | Events > 365 days apart AND different entities | Skip comparison |
| Title dissimilarity | Jaccard title similarity < 0.3 | Skip detailed comparison |

---

## 5. Entity Resolution

Entity resolution determines whether two events refer to the same victim organisation. It is the most important signal for deduplication.

### 5.1 Entity Name Normalization

Before comparison, entity names are normalized through the `EntityMappings` database table, which maps subsidiary/brand/alias names to canonical parent entities.

**Lookup process:**

1. **Exact match:** `entity_name.lower().strip()` is looked up in the mappings dictionary.
2. **Partial match:** If no exact match, iterates all mappings. If the source key contains the entity name or vice versa, the mapping is applied.
3. **No match:** Returns the original name unchanged.

**Example mapping:** `"ticketmaster llc"` → `"Live Nation Entertainment"`

The `EntityMappings` table is maintained manually and loaded at engine initialization. When invoked from the discovery pipeline (`discovery.py`), these mappings are loaded from the database. When invoked from the standalone script (`run_global_deduplication.py`), entity mappings are not loaded (the script passes `entity_mappings=None`), so normalization relies only on the direct comparison methods below.

### 5.2 Organization Name Matching

When both events have non-null `victim_organization_name` fields (after normalization):

1. **Exact case-insensitive match:** `name1.lower().strip() == name2.lower().strip()`
2. **Substring containment:** `name1 in name2` or `name2 in name1`, but only if **both** names are at least 5 characters long. The 5-character minimum prevents false positives like "Bank" matching "Commonwealth Bank".

### 5.3 Related Companies

A hardcoded list of known parent-subsidiary/related company relationships is checked:

```python
related_companies = [
    {'ticketmaster', 'live nation'},
]
```

If both organisation names match entries in the same set (via substring check), they are considered the same entity.

**Justification:** Some high-profile incidents involve companies known by different names. The Ticketmaster/Live Nation case is the canonical example in this dataset — both names refer to the same parent company, and the 2024 data breach was reported under both names.

### 5.4 Title Keyword Fallback

If organisation names are null or do not match via the methods above, both event titles are checked for shared company keywords:

```python
company_keywords = [
    'ticketmaster', 'live nation', 'medibank', 'optus', 'singtel',
    'latitude', 'myob', 'canva', 'woolworths', 'coles',
    'nitro', 'nitro pdf', 'nitro software',
    'qantas', 'telstra', 'commonwealth bank', 'westpac', 'nab', 'anz',
    'bunnings', 'kmart', 'target', 'myer', 'david jones',
    'toyota', 'mazda', 'ford', 'holden',
    'university', 'council', 'hospital', 'health',
]
```

If both titles contain the same keyword (case-insensitive substring match), they are considered the same entity.

**Important:** This fallback runs even when one or both events have null `victim_organization_name`, handling cases where the enrichment stage failed to extract the entity but the title clearly references a known organisation.

**Justification:** Entity extraction via LLMs is imperfect. Some articles may have their organisation name extracted correctly while others from the same source do not. The keyword list covers the most frequently reported Australian organisations and generic institutional types (university, council, hospital) that appear frequently in cyber incident reporting.

---

## 6. Similarity Functions

### 6.1 Quick Title Similarity (Jaccard)

Used by Rules 2a, 2b, 3, and the pre-filter. This is a fast, word-level Jaccard similarity:

```
Jaccard(A, B) = |A ∩ B| / |A ∪ B|
```

Where A and B are the sets of whitespace-delimited words from each title (lowercased, stripped).

- Returns 1.0 for exact matches.
- Returns 0.0 if either title is null/empty.

**Complexity:** O(n) where n is the total word count.

### 6.2 Weighted Composite Similarity

Used by the fallback rule (Section 4.6). Calculates four component scores and combines them:

```
overall_score = title × 0.4 + entity × 0.3 + content × 0.2 + temporal × 0.1
```

| Component | Weight | Method |
|-----------|--------|--------|
| Title | 0.4 | SequenceMatcher ratio with substring and word overlap bonuses |
| Entity | 0.3 | Jaccard similarity on regex-extracted capitalised words |
| Content | 0.2 | SequenceMatcher ratio on normalized summary/description text |
| Temporal | 0.1 | Step function based on date distance |

**Confidence** is calculated as `1.0 - (max(scores) - min(scores))`, giving higher confidence when all component scores agree.

**Justification for weights:** Title similarity receives the highest weight (0.4) because incident titles are the most distinctive identifier. Entity overlap (0.3) captures shared organisation/people names. Content (0.2) provides broader semantic signal. Temporal proximity (0.1) receives the lowest weight because the same incident can be reported weeks apart.

### 6.3 Title Similarity (Detailed)

Used within the composite similarity calculator. More sophisticated than the quick Jaccard:

1. **Exact match** → 1.0
2. **Very similar titles** (after removing ` - `, ` | `, `...`, double spaces, the SequenceMatcher ratio exceeds 0.9) → 0.95
3. **Sequence similarity:** `difflib.SequenceMatcher(None, title1, title2).ratio()`
4. **Substring bonus:** If one title is a substring of the other → `max(score, 0.8)`
5. **Word overlap floor:** Jaccard word overlap is computed and used as a floor for the final score: `max(seq_sim, word_overlap)`

### 6.4 Entity Similarity

Extracts potential entity names from titles using a regex pattern for capitalised word sequences (`\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b`), then filters out cyber security stopwords:

```
Data, Breach, Incident, Security, Attack, Cyber, Cybersecurity,
Ransomware, Malware, Hack, Hackers, Outage, Leak, Exposure, Network
```

Calculates Jaccard set similarity on the remaining entity tokens.

### 6.5 Content Similarity

Uses the event's `summary` field (falls back to `description` if summary is null). Normalizes text by lowercasing, collapsing whitespace, and removing non-word characters except spaces. Calculates `difflib.SequenceMatcher.ratio()`.

### 6.6 Temporal Similarity

A step function based on the absolute date difference:

| Date Difference | Score |
|----------------|-------|
| Same day | 1.0 |
| 1-7 days | 0.8 |
| 8-30 days | 0.6 |
| 31-90 days | 0.4 |
| 91-365 days | 0.2 |
| > 365 days | 0.0 |

If either event has no date, returns 0.0.

---

## 7. LLM Arbiter

The LLM arbiter is an optional GPT-4o-mini integration that resolves uncertain similarity decisions in the composite fallback path (Section 4.6).

### 7.1 Activation Criteria

The arbiter is invoked only when the algorithmic composite score falls in the **uncertain range of 0.3 to 0.7** (inclusive). Scores outside this range are considered decisive enough to accept or reject without LLM assistance.

### 7.2 Prompt

**System message:**

```
You are a cybersecurity analyst comparing breach reports. Respond only with valid JSON.
```

**User message:**

```
You are analyzing two cybersecurity events to determine if they are duplicates
or different incidents.

Event 1:
- Title: {event1.title}
- Date: {event1.event_date}
- Summary: {event1.summary or 'No summary available'}
- Type: {event1.event_type or 'Unknown'}

Event 2:
- Title: {event2.title}
- Date: {event2.event_date}
- Summary: {event2.summary or 'No summary available'}
- Type: {event2.event_type or 'Unknown'}

Algorithmic similarity score: {algo_score:.2f}

Are these the same cybersecurity incident? Consider:
1. Are they about the same breach/attack?
2. Do they involve the same organization?
3. Are the dates consistent with the same incident?
4. Are the details describing the same event?

Respond with JSON:
{
    "is_similar": true/false,
    "confidence": 0.0-1.0,
    "reasoning": "Brief explanation of your decision"
}
```

**Model parameters:**

| Parameter | Value |
|-----------|-------|
| Model | `gpt-4o-mini` |
| Temperature | 0.1 |
| Max tokens | 200 |

### 7.3 Score Adjustment

Based on the arbiter's decision:

- **`is_similar: true`** → Overall score is raised to `max(original_score, 0.8)`, ensuring it exceeds the 0.75 merge threshold.
- **`is_similar: false`** → Overall score is lowered to `min(original_score, 0.3)`, ensuring it falls below the threshold.

The confidence value from the arbiter replaces the algorithmic confidence, and the reasoning is recorded.

### 7.4 Fallback Behaviour

- **No API key:** The arbiter is not instantiated; algorithmic scores are used directly.
- **API error or unparseable response:** Falls back to `is_similar = (original_score > 0.5)` with confidence 0.3.
- **Score outside 0.3-0.7 range:** Arbiter is skipped; algorithmic score is used.

---

## 8. Merge Logic

When a group of events is identified as duplicates, they are merged into a single output record.

### 8.1 Master Event Selection

Each event in the group is scored on completeness:

| Criterion | Points |
|-----------|--------|
| Summary longer than 50 characters | +2 |
| Has `records_affected` > 0 | +1 |
| Has `severity` value | +1 |
| Has `event_type` value | +1 |
| Recency bonus | +`max(0, 1 - (days_ago / 365))` |

The highest-scoring event is selected as the **master event** — it provides the primary title, event type, and severity for the merged record.

**Justification:** The master event selection prefers records with richer metadata (higher completeness score) because these are more useful for downstream analysis. The recency bonus is small (max 1.0 point) and decays linearly, slightly favouring newer reports which tend to have more complete information.

### 8.2 Field Merging Strategy

The merged event inherits fields as follows:

| Field | Strategy | Justification |
|-------|----------|---------------|
| `event_id` | From first event in group | Preserved for referential integrity |
| `title` | From first event in group | Maintains a stable identifier |
| `summary` | Longest summary across all events | Maximises information content |
| `event_date` | **Earliest** date across all events | Reflects the actual incident date, not publication date |
| `event_type` | From first event | Consistent with initial classification |
| `severity` | From first event | Consistent with initial classification |
| `records_affected` | Highest **validated** value (see Section 8.3) | Maximises accuracy while preventing implausible values |
| `victim_organization_name` | First non-null value, normalized through entity mappings | Ensures entity name is always populated when available |
| `victim_organization_industry` | First non-null value | Ensures industry is always populated when available |
| `data_sources` | Union of all sources across events | Preserves provenance information |
| `urls` | Union of all URLs across events | Preserves all source references |

### 8.3 Records Affected Validation

The highest `records_affected` value across merged events undergoes a two-tier validation process:

#### Tier 1: Rule-Based Validation

Applied first. Uses organisation-specific plausibility thresholds:

| Rule | Threshold | Justification |
|------|-----------|---------------|
| Reject negative values | < 0 | Invalid data |
| Reject zero | = 0 | Treated as missing (NULL) |
| Reject suspiciously low | < 50 | Likely parsing errors where units ("thousand", "million") were missed |
| Small/unknown org cap | > 20,000,000 | Local/regional organisations rarely have >20M records |
| Major Australian org cap | > 30,000,000 | Based on Australia's population of ~26M |
| Absolute maximum | > 1,000,000,000 | No single breach can realistically affect >1B records |

Organisation classification is determined by checking the event title against three hardcoded sets:
- **Major international organisations** (~50 entries: Facebook/Meta, Google, Microsoft, etc.) — allows up to 1 billion
- **Major Australian organisations** (~30 entries: Commonwealth Bank, Telstra, Optus, etc.) — allows up to 30 million
- **Australian government identifiers** (~30 entries: government, department, ATO, Medicare, etc.) — allows up to 30 million

#### Tier 2: Perplexity LLM Fallback

If the rule-based check **rejects** the value AND a Perplexity API key is available, the system calls Perplexity's `sonar-pro` model for verification.

**Prompt:**

```
You are verifying a data point from a cyber security breach report.

Event: "{event_title}"
Organization: "{org_name or event_title}"
Claimed records affected: {value:,}
Context: "{description[:500]}"

Questions:
1. Is this event genuinely about a CYBER SECURITY incident
(data breach, ransomware, hacking, phishing, etc.)?
Or is it about physical security, airline safety, border security,
workplace safety, or some other non-cyber topic?
The word 'security' alone does NOT make it a cyber incident.
2. Is "{org_name}" a major organization? Estimate their customer/user base size.
3. Is {value:,} records affected plausible for this organization and this incident?
4. Could this number be a parsing error (e.g. '20' when the article said
'20,000' or '20 million')?

Respond with JSON only:
{
  "is_cyber_incident": true or false,
  "is_plausible": true or false,
  "corrected_value": null or integer (if the number was likely misread),
  "org_size_category": "major_international" | "major_australian" | "small_regional" | "unknown",
  "estimated_customer_base": integer or null,
  "reasoning": "brief explanation"
}
```

**Model parameters:**

| Parameter | Value |
|-----------|-------|
| Model | `sonar-pro` (Perplexity) |
| Temperature | 0.1 |
| Max tokens | 500 |
| Timeout | 30 seconds |

**Outcomes:**

- If `is_cyber_incident` is false → the event is flagged for rejection (records set to NULL, and a `False` flag is returned to the caller).
- If `corrected_value` is provided → the corrected value is re-validated through the same rule-based checks before acceptance.
- If `org_size_category` is `"major_australian"` or `"major_international"` → the organisation is **dynamically added** to the module-level organisation sets, so future checks for the same org pass without an API call.

**Graceful degradation:** If no Perplexity API key is configured, the rule-based rejection stands. The event retains `records_affected = NULL` rather than the unvalidated value.

---

## 9. Grouping Algorithm

The core grouping uses a **single-pass greedy algorithm** with O(n²) pairwise comparisons:

```
For each event i (not yet processed):
    Create a new group containing event i
    Mark i as processed

    For each subsequent event j (not yet processed):
        Apply matching rules in priority order (Section 4)
        If any rule triggers:
            Add event j to the group
            Mark j as processed
            Continue to next j

    Add the completed group to the output
```

**Characteristics:**

- **Greedy:** Once an event is assigned to a group, it cannot be reassigned.
- **Order-dependent:** The first event in a group becomes the seed; subsequent events are compared against the seed only.
- **Single-pass:** Each event pair is compared at most once.

**Justification:** The greedy approach is chosen for its simplicity and predictability. The entity-based matching rules (which handle the vast majority of merges) are deterministic and order-independent for the cases they cover. The O(n²) complexity is acceptable for the dataset size (typically 500-2,000 enriched events).

---

## 10. Execution Modes

### 10.1 Full Rebuild

Triggered when `force=True` or no existing deduplicated data exists.

**Steps:**

1. Back up the database (binary copy + SQL dump of dedup tables).
2. Clear all deduplication tables (`DeduplicatedEvents`, `EventDeduplicationMap`, `DeduplicationClusters`), along with downstream tables that have foreign key dependencies (`ASDRiskClassifications`, `DeduplicatedEventEntities`, `DeduplicatedEventSources`).
3. Load **all** active enriched events from `EnrichedEvents`, joined with `RawEvents` for description text. Entity names are extracted from the `perplexity_enrichment_data` JSON column (`formal_entity_name` → `victim_organization_name`, `victim_industry` → `victim_organization_industry`).
4. Run the full `DeduplicationEngine.deduplicate()` method.
5. Store results via `DeduplicationStorage`.
6. Validate storage integrity.

**SQL for loading enriched events:**

```sql
SELECT e.enriched_event_id, e.title, e.summary, e.event_date,
       e.event_type, e.severity, e.records_affected, e.confidence_score,
       e.perplexity_enrichment_data, r.raw_description
FROM EnrichedEvents e
LEFT JOIN RawEvents r ON e.raw_event_id = r.raw_event_id
WHERE e.status = 'Active'
ORDER BY e.event_date DESC
```

### 10.2 Incremental Deduplication

Triggered when new unprocessed enriched events exist but existing deduplicated data is present.

**Steps:**

1. Back up the database.
2. Detect new events (enriched events not yet in any deduplication output).
3. Load only the new events from `EnrichedEvents`.
4. Load existing `DeduplicatedEvents` for comparison.
5. **Step 1/2:** Deduplicate the new events among themselves using the full engine.
6. **Step 2/2:** Match each new unique event against all existing deduplicated events using the same matching rules (Rules 1, 2a, 2b). Note: Rule 3 (description-only matching) and the composite fallback are not applied during incremental matching — only entity-based rules are used.
7. For matches → create an `EventDeduplicationMap` entry with `contribution_type='incremental_merge'` and update the existing event's date to the earlier of the two dates.
8. For non-matches → store as new deduplicated events.

**SQL for detecting new events:**

```sql
SELECT e.enriched_event_id
FROM EnrichedEvents e
WHERE e.status = 'Active'
  AND NOT EXISTS (
      SELECT 1 FROM EventDeduplicationMap edm
      WHERE edm.enriched_event_id = e.enriched_event_id
  )
  AND NOT EXISTS (
      SELECT 1 FROM DeduplicatedEvents de
      WHERE de.master_enriched_event_id = e.enriched_event_id
        AND de.status = 'Active'
  )
```

### 10.3 Mode Selection Logic

```
if force=True OR no existing DeduplicatedEvents:
    → Full Rebuild (Path A)
elif no new events detected:
    → Skip (Path B)
else:
    → Incremental (Path C)
```

---

## 11. Input Validation

Before deduplication begins, the `DeduplicationValidator` runs three categories of checks:

**Input validation:**
- Empty input (no events provided).
- Duplicate event IDs within the input.
- Events with missing or empty titles.

**Output validation** (after grouping and merging):
- Exact `(title.lower().strip(), event_date)` duplicates remaining in the output.
- Merge groups with a missing master event.

**Data integrity validation:**
- Events with future dates.
- Events with negative `records_affected` values.

All validation errors are logged as warnings but are **non-fatal** — processing continues, and errors are included in the `DeduplicationResult` for review.

---

## 12. Database Schema

### 12.1 DeduplicatedEvents Table

| Column | Type | Description |
|--------|------|-------------|
| `deduplicated_event_id` | TEXT PK | UUID generated during deduplication |
| `master_enriched_event_id` | TEXT NOT NULL | FK to `EnrichedEvents.enriched_event_id` |
| `title` | VARCHAR(255) NOT NULL | Final event title |
| `description` | TEXT | Final description |
| `summary` | TEXT | Final summary (longest across merged events) |
| `event_type` | VARCHAR(50) | CyberEventType enum value |
| `severity` | VARCHAR(20) | EventSeverity enum value |
| `event_date` | DATE | Earliest date across merged sources |
| `records_affected` | BIGINT | Highest validated value |
| `is_australian_event` | BOOLEAN NOT NULL | Always TRUE for this pipeline |
| `is_specific_event` | BOOLEAN NOT NULL | Always TRUE for this pipeline |
| `confidence_score` | REAL | 0.0-1.0 confidence |
| `victim_organization_name` | VARCHAR(255) | Victim organisation (first non-null, normalized) |
| `victim_organization_industry` | VARCHAR(100) | Industry classification |
| `status` | VARCHAR(20) DEFAULT 'Active' | Active / Merged / Deleted |
| `similarity_score` | REAL | Average pairwise similarity within merge group |
| `deduplication_method` | VARCHAR(50) | Method identifier |
| `created_at` | TIMESTAMP | Record creation time |
| `updated_at` | TIMESTAMP | Last modification time |
| `attacking_entity_name` | VARCHAR(255) | Threat actor name |
| `attack_method` | VARCHAR(100) | Attack method classification |
| `perplexity_validated` | BOOLEAN DEFAULT FALSE | Whether Perplexity enrichment was applied |
| `perplexity_enrichment_data` | TEXT | JSON blob of Perplexity enrichment data |

### 12.2 EventDeduplicationMap Table

Tracks which enriched events were merged into which deduplicated events.

| Column | Type | Description |
|--------|------|-------------|
| `map_id` | TEXT PK | UUID |
| `raw_event_id` | TEXT NOT NULL | Source enriched event ID |
| `enriched_event_id` | TEXT | Same as `raw_event_id` in practice |
| `deduplicated_event_id` | TEXT NOT NULL | Master enriched event ID |
| `contribution_type` | VARCHAR(50) | `'merged'` (full rebuild) or `'incremental_merge'` |
| `similarity_score` | REAL | Similarity to master event |
| `data_source_weight` | REAL DEFAULT 1.0 | Source weighting |
| `created_at` | TIMESTAMP | Record creation time |

**Constraint:** `UNIQUE(raw_event_id, deduplicated_event_id)` — prevents duplicate mapping entries.

### 12.3 DeduplicationClusters Table

| Column | Type | Description |
|--------|------|-------------|
| `cluster_id` | TEXT PK | UUID |
| `deduplicated_event_id` | TEXT NOT NULL | FK to `DeduplicatedEvents` |
| `cluster_size` | INTEGER | Count of events in group (including master) |
| `average_similarity` | REAL | Average pairwise similarity within cluster |
| `deduplication_timestamp` | TIMESTAMP | When deduplication was performed |
| `algorithm_version` | VARCHAR(20) | Always `'v2.0'` |

### 12.4 EntityMappings Table

Manually maintained table for entity name normalization.

| Column | Type | Description |
|--------|------|-------------|
| `entity_mapping_id` | INTEGER PK AUTOINCREMENT | Row ID |
| `source_entity` | TEXT NOT NULL UNIQUE | Entity variant (e.g., "ticketmaster llc") |
| `canonical_entity` | TEXT NOT NULL | Canonical name (e.g., "Live Nation Entertainment") |
| `relationship_type` | TEXT DEFAULT 'subsidiary' | subsidiary / brand / division / alias |
| `notes` | TEXT | Free-text notes |
| `created_at` | TEXT | Creation timestamp |
| `updated_at` | TEXT | Last update timestamp |

---

## 13. Storage Integrity Checks

After deduplication results are stored, the following integrity checks are performed:

1. **Duplicate IDs:** No duplicate `deduplicated_event_id` values in `DeduplicatedEvents`.
2. **Duplicate title+date:** No duplicate `(title, event_date)` combinations where `status='Active'`.
3. **Orphaned mappings:** No `EventDeduplicationMap` entries pointing to non-existent `DeduplicatedEvents`.
4. **Orphaned clusters:** No `DeduplicationClusters` entries pointing to non-existent `DeduplicatedEvents`.

Additionally, before storing, events with identical `(title.lower().strip(), event_date)` combinations are de-duplicated, keeping the first occurrence.

All database operations use `threading.RLock()` for thread safety.

---

## 14. Configuration and Thresholds

Complete reference of all configurable parameters and hardcoded thresholds:

| Parameter | Value | Location | Description |
|-----------|-------|----------|-------------|
| `similarity_threshold` | 0.75 | Engine constructor | Composite score threshold for fallback matching |
| Rule 2a title similarity | 0.15 | `_group_similar_events` | Jaccard threshold for same-entity title matching |
| Rule 2b description similarity | 0.20 | `_group_similar_events` | Jaccard threshold for same-entity description matching |
| Rule 3 description similarity | 0.35 | `_group_similar_events` | Jaccard threshold for description-only matching |
| Rule 3 date window | 90 days | `_group_similar_events` | Maximum date distance for description-only matching |
| Pre-filter: date cutoff | 365 days | `_group_similar_events` | Skip comparisons for distant events (different entities) |
| Pre-filter: title similarity floor | 0.30 | `_group_similar_events` | Skip detailed comparison for dissimilar titles |
| Entity name min length | 5 chars | `_same_entity` | Minimum for substring matching (prevents "Bank" ⊂ "Commonwealth Bank") |
| LLM arbiter activation range | 0.3-0.7 | `LLMArbiter._should_use_arbiter` | Composite score range triggering LLM review |
| LLM arbiter boost | max(score, 0.8) | `_calculate_event_similarity` | Score adjustment when LLM says "similar" |
| LLM arbiter suppress | min(score, 0.3) | `_calculate_event_similarity` | Score adjustment when LLM says "not similar" |
| LLM arbiter model | gpt-4o-mini | `LLMArbiter.__init__` | OpenAI model used |
| LLM arbiter temperature | 0.1 | `LLMArbiter._call_llm` | Low temperature for deterministic responses |
| LLM arbiter max tokens | 200 | `LLMArbiter._call_llm` | Response size limit |
| Composite weights | 0.4 / 0.3 / 0.2 / 0.1 | `SimilarityCalculator` | title / entity / content / temporal |
| Very similar title threshold | 0.9 | `_are_titles_very_similar` | SequenceMatcher ratio for near-identical titles |
| MIN_REALISTIC_RECORDS | 50 | `validation.py` | Minimum plausible records affected |
| SMALL_ORG_MAX | 20,000,000 | `validation.py` | Cap for unknown/small organisations |
| LARGE_AU_ORG_MAX | 30,000,000 | `validation.py` | Cap for major Australian orgs (~AU population) |
| MAX_RECORDS | 1,000,000,000 | `validation.py` | Absolute maximum for any organisation |
| Perplexity validation model | sonar-pro | `validation.py` | Perplexity model for records verification |
| Algorithm version | v2.0 | `DeduplicationStorage` | Version tag stored in cluster records |

---

## 15. Error Handling

| Scenario | Behaviour |
|----------|-----------|
| Validation errors (input/output) | Non-fatal: logged as warnings, included in result, processing continues |
| Storage failures | Transaction rollback, exception raised |
| LLM arbiter API error | Falls back to algorithmic score with confidence 0.3 |
| LLM arbiter unparseable response | Falls back to `is_similar = (score > 0.5)` with confidence 0.3 |
| No OpenAI API key | Arbiter not instantiated; algorithmic scores only |
| Perplexity validation API error | Returns `(None, True)` — rejects the records value but keeps the event |
| No Perplexity API key | Rule-based rejection stands |
| Full migration failure | Error logged; database backup can be restored |
| Date parsing failure | Partial dates handled: `YYYY-MM` → first of month, `YYYY` → January 1st |

---

## 16. Justification of Design Decisions

### Why entity-based matching over pure text similarity?

Pure text similarity (e.g., TF-IDF, cosine similarity) fails when articles about the same incident use different language, framing, or emphasis. Entity-based matching leverages the strongest signal available: if two events name the same victim organisation, they are very likely about the same incident. Text similarity is reserved as a fallback for cases where entity extraction failed.

### Why a 0.15 Jaccard threshold for same-entity title matching?

This threshold is deliberately low because entity identity has already been established. A Jaccard score of 0.15 on typical news article titles (8-15 words) requires approximately 1-2 shared words. This catches cases like "Ticketmaster hacked in massive breach" vs "Live Nation data breach exposes customer records" where entity matching (via the related companies list) confirms the same organisation but titles share few words. The false-positive risk is minimal because the entity match prerequisite eliminates the vast majority of unrelated event pairs.

### Why use earliest date instead of most common/most recent?

Multiple news articles about the same incident are published on different dates (initial disclosure, follow-up reporting, regulatory announcements). The earliest date best approximates when the incident was first reported or disclosed, which is the most useful date for incident timeline analysis. Using the most common or most recent date would bias toward publication patterns rather than incident timing.

### Why greedy grouping instead of hierarchical clustering?

Greedy grouping is simpler to implement, debug, and explain. For this dataset (typically 500-2,000 events), the entity-based matching rules handle the vast majority of merges deterministically — the grouping order rarely matters because entity matches are symmetric and transitive for practical purposes. Hierarchical clustering would add complexity without meaningful accuracy improvement.

### Why a separate LLM arbiter instead of an LLM-only approach?

The hybrid approach (algorithmic rules + LLM fallback) balances accuracy, cost, and latency:
- Rules 1-3 handle ~95% of merges without any LLM call.
- The LLM arbiter is invoked only for the small fraction of event pairs with uncertain composite scores (0.3-0.7 range).
- Using GPT-4o-mini with 200 max tokens keeps costs minimal per invocation.
- The system degrades gracefully when no API key is available.

### Why validate records_affected during merge?

LLM extraction of numeric values is error-prone. Common failures include:
- Extracting "20" when the article said "20,000" (missing units).
- Confusing customer count with records count.
- Extracting total company customers rather than breach-specific counts.

The tiered validation (rule-based caps → Perplexity verification) catches these errors at the point where the highest value is selected during merge, preventing implausible figures from propagating to the dashboard.

### Why dynamic organisation set updates during Perplexity validation?

When Perplexity confirms an organisation is a "major Australian" or "major international" company, adding it to the module-level sets avoids redundant API calls for subsequent events involving the same organisation. This is both a cost optimisation and a consistency measure.

---

## 17. Replication Guide

To independently replicate the deduplication process:

### Prerequisites

- Python 3.8+
- SQLite database with `EnrichedEvents` and `RawEvents` tables populated
- OpenAI API key (optional, for LLM arbiter)
- Perplexity API key (optional, for records_affected validation)

### Step-by-Step Process

1. **Load enriched events** from the database using the SQL in Section 10.1. For each event, extract `formal_entity_name` and `victim_industry` from the `perplexity_enrichment_data` JSON column. Use `raw_description` from `RawEvents` as the `description` field.

2. **Optionally load entity mappings** from the `EntityMappings` table. Build a dictionary mapping `source_entity.lower()` → `canonical_entity`.

3. **For each event pair (i, j) where i < j**, apply the matching rules in Section 4 in strict priority order. If any rule triggers, the pair should be merged.

4. **Group events** using the greedy algorithm in Section 9. Process events in descending date order (as loaded from the database query).

5. **For each group with >1 event**, merge fields according to Section 8.2:
   - Select the master event by scoring completeness (Section 8.1).
   - Use the earliest date.
   - Use the longest summary.
   - Validate the highest records_affected value (Section 8.3).
   - Take the first non-null organisation name and industry.
   - Union all data sources and URLs.

6. **Store results** in `DeduplicatedEvents`, `EventDeduplicationMap`, and `DeduplicationClusters` tables using the schemas in Section 12.

7. **Validate** using the integrity checks in Section 13.

### Running the Implementation

```bash
# Full rebuild
python scripts/run_global_deduplication.py --force

# Incremental (default)
python scripts/run_global_deduplication.py

# Dry run (preview without changes)
python scripts/run_global_deduplication.py --dry-run

# As part of the full pipeline
python run_full_pipeline.py
```

### Key Implementation Notes

- The `_quick_title_similarity` function uses **Jaccard word-level similarity**, not character-level. Words are obtained by splitting on whitespace after lowercasing.
- Entity matching via `_same_entity` is **not symmetric in all cases** — the keyword list check depends on title content, not just organisation names. However, this asymmetry does not affect correctness because the greedy grouping compares all pairs within each group.
- The incremental path (Section 10.2) uses only Rules 1, 2a, and 2b for matching against existing deduplicated events. It does **not** apply Rule 3 or the composite fallback, making it more conservative than a full rebuild.
- During storage, events with duplicate `(title.lower().strip(), event_date)` combinations are pre-filtered, keeping the first occurrence. This is a safety net that catches any duplicates missed by the grouping algorithm.
