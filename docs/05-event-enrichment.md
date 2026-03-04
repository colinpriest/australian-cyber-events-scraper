# Event Enrichment Stage

This document provides a detailed specification of the event enrichment stage of the Australian Cyber Events pipeline. It covers the enrichment architecture, content acquisition methods, LLM extraction prompts, fact-checking processes, validation rules, confidence scoring, audit trail storage, and design justifications in sufficient detail for an independent reviewer to assess suitability and for independent replication.

---

## Table of Contents

1. [Purpose and Scope](#1-purpose-and-scope)
2. [Enrichment Architecture Overview](#2-enrichment-architecture-overview)
   - 2.1 [Three Enrichment Pathways](#21-three-enrichment-pathways)
   - 2.2 [End-to-End Data Flow](#22-end-to-end-data-flow)
   - 2.3 [When Each Pathway Runs](#23-when-each-pathway-runs)
3. [Pathway 1: Initial GPT-4o-mini Extraction (Discovery Phase)](#3-pathway-1-initial-gpt-4o-mini-extraction-discovery-phase)
   - 3.1 [Purpose](#31-purpose)
   - 3.2 [Model and Configuration](#32-model-and-configuration)
   - 3.3 [LLM Prompts](#33-llm-prompts)
   - 3.4 [Output Schema](#34-output-schema)
   - 3.5 [Justification](#35-justification)
4. [Pathway 2: Perplexity AI Enrichment (Post-Discovery)](#4-pathway-2-perplexity-ai-enrichment-post-discovery)
   - 4.1 [Purpose](#41-purpose)
   - 4.2 [Event Selection Criteria](#42-event-selection-criteria)
   - 4.3 [Model and Configuration](#43-model-and-configuration)
   - 4.4 [LLM Prompt](#44-llm-prompt)
   - 4.5 [Output Schema (Pydantic)](#45-output-schema-pydantic)
   - 4.6 [Field Update Rules](#46-field-update-rules)
   - 4.7 [Rate Limiting and Retry Logic](#47-rate-limiting-and-retry-logic)
   - 4.8 [Justification](#48-justification)
5. [Pathway 3: High-Quality 5-Stage Enrichment Pipeline](#5-pathway-3-high-quality-5-stage-enrichment-pipeline)
   - 5.1 [Purpose and Architecture](#51-purpose-and-architecture)
   - 5.2 [Stage 1: Content Acquisition](#52-stage-1-content-acquisition)
   - 5.3 [Stage 2: GPT-4o Primary Extraction](#53-stage-2-gpt-4o-primary-extraction)
   - 5.4 [Stage 3: Perplexity Fact-Checking](#54-stage-3-perplexity-fact-checking)
   - 5.5 [Stage 4: Validation and Sensibility Checks](#55-stage-4-validation-and-sensibility-checks)
   - 5.6 [Stage 5: Confidence Aggregation and Decision](#56-stage-5-confidence-aggregation-and-decision)
   - 5.7 [Audit Trail Storage](#57-audit-trail-storage)
6. [Supplementary Enrichment: Missing Detail Fill-In](#6-supplementary-enrichment-missing-detail-fill-in)
   - 6.1 [Purpose](#61-purpose)
   - 6.2 [Fields Checked](#62-fields-checked)
   - 6.3 [Query Construction](#63-query-construction)
   - 6.4 [Response Parsing](#64-response-parsing)
7. [Records Affected Validation](#7-records-affected-validation)
   - 7.1 [Rule-Based Validation](#71-rule-based-validation)
   - 7.2 [LLM Fallback Validation](#72-llm-fallback-validation)
   - 7.3 [Dynamic Organization List Updates](#73-dynamic-organization-list-updates)
8. [Token Usage Tracking](#8-token-usage-tracking)
9. [Configuration and Thresholds Summary](#9-configuration-and-thresholds-summary)
10. [Error Handling and Resilience](#10-error-handling-and-resilience)
11. [Justification of Design Decisions](#11-justification-of-design-decisions)
12. [Replication Guide](#12-replication-guide)

---

## 1. Purpose and Scope

The event enrichment stage is responsible for transforming raw discovered events into structured, validated, and confidence-scored records suitable for analysis and reporting. It performs:

- **Content acquisition** from source URLs using multiple extraction methods.
- **Structured data extraction** using large language models (GPT-4o and GPT-4o-mini) to identify victim organizations, attacker information, incident details, and Australian relevance.
- **Fact-checking** via Perplexity AI's real-time web search to verify extracted claims against authoritative sources.
- **Validation** through rule-based sensibility checks on dates, organization names, cross-field consistency, and specificity classification.
- **Confidence scoring** that aggregates signals from extraction, fact-checking, validation, and source reliability into an accept/reject decision.
- **Audit trail storage** that records the complete processing history for every enriched event.

This stage corresponds to Phase 2 of the five-phase pipeline (and includes enrichment logic embedded in Phase 1's discovery process). It is invoked by `run_full_pipeline.py` and uses modules in `cyber_data_collector/enrichment/`, `cyber_data_collector/processing/`, and `cyber_data_collector/utils/`.

### What This Stage Does NOT Do

This stage does not perform initial event discovery or source scraping (Phase 1), global deduplication (Phase 3), ASD risk classification (Phase 4), or dashboard generation (Phase 5). Those are separate pipeline phases documented elsewhere.

### Source Code Locations

| Module | File | Purpose |
|--------|------|---------|
| HighQualityEnrichmentPipeline | `cyber_data_collector/enrichment/high_quality_enrichment_pipeline.py` | 5-stage orchestrator |
| ContentAcquisitionService | `cyber_data_collector/enrichment/content_acquisition.py` | URL content fetching |
| GPT4oEnricher | `cyber_data_collector/enrichment/gpt4o_enricher.py` | Primary GPT-4o extraction |
| PerplexityFactChecker | `cyber_data_collector/enrichment/perplexity_fact_checker.py` | Fact verification |
| EnrichmentValidator | `cyber_data_collector/enrichment/enrichment_validator.py` | Sensibility checks |
| EnrichmentAuditStorage | `cyber_data_collector/enrichment/enrichment_audit_storage.py` | Audit persistence |
| PerplexityEnrichmentEngine | `cyber_data_collector/processing/perplexity_enrichment.py` | Perplexity backfill enrichment |
| PerplexityEventEnricher | `cyber_data_collector/processing/perplexity_enricher.py` | Missing-detail enrichment |
| PerplexityBackfillProcessor | `scripts/perplexity_backfill_events.py` | Backfill orchestrator |
| extract_event_details_with_llm | `cyber_data_collector/utils/llm_extractor.py` | Fast initial extraction |
| validate_records_affected | `cyber_data_collector/utils/validation.py` | Records count validation |
| llm_validate_records_affected | `cyber_data_collector/utils/validation.py` | LLM-assisted validation |

---

## 2. Enrichment Architecture Overview

### 2.1 Three Enrichment Pathways

The pipeline has three distinct enrichment pathways that operate at different stages and with different cost/quality trade-offs:

| Pathway | Model | Purpose | Cost per Event | Quality |
|---------|-------|---------|----------------|---------|
| **1. Initial Extraction** | GPT-4o-mini | Fast filtering during discovery | ~$0.001 | Moderate |
| **2. Perplexity Backfill** | Perplexity sonar-pro | Post-discovery validation and entity resolution | ~$0.005 | High |
| **3. High-Quality 5-Stage** | GPT-4o + Perplexity sonar-pro | Full re-enrichment with fact-checking | ~$0.02 | Very High |

### 2.2 End-to-End Data Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        EVENT ENRICHMENT PIPELINE                            │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │ PATHWAY 1: Initial Extraction (During Discovery - Phase 1)          │   │
│  │                                                                      │   │
│  │  Raw Event ──► GPT-4o-mini ──► ExtractedEventDetails                │   │
│  │               (fast pass)     • is_australian_event                  │   │
│  │                               • is_specific_event                   │   │
│  │                               • primary_entity                      │   │
│  │                               • summary, event_date                 │   │
│  │                               • records_affected                    │   │
│  │                                      │                              │   │
│  │                                      ▼                              │   │
│  │                          Records Affected Validation                │   │
│  │                          (rule-based + Perplexity LLM fallback)     │   │
│  │                                      │                              │   │
│  │                                      ▼                              │   │
│  │                          Store as EnrichedEvent                     │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                      │                                      │
│                                      ▼                                      │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │ PATHWAY 2: Perplexity Backfill (Automatic Post-Discovery)           │   │
│  │                                                                      │   │
│  │  Events where perplexity_validated IS NULL                          │   │
│  │         │                                                            │   │
│  │         ▼                                                            │   │
│  │  Perplexity sonar-pro ──► PerplexityEventEnrichment                 │   │
│  │  (real-time web search)   • formal_entity_name                      │   │
│  │                           • earliest_event_date                     │   │
│  │                           • threat_actor, attack_method             │   │
│  │                           • victim_count                            │   │
│  │                           • per-field confidence scores             │   │
│  │                                      │                              │   │
│  │                                      ▼                              │   │
│  │                   Update EnrichedEvent (if confidence >= 0.6)       │   │
│  │                   Set perplexity_validated = TRUE                   │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                      │                                      │
│                                      ▼                                      │
│                              Global Deduplication                           │
│                                      │                                      │
│                                      ▼                                      │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │ PATHWAY 3: High-Quality 5-Stage Pipeline (On-Demand Re-Enrichment)  │   │
│  │                                                                      │   │
│  │  Stage 1: Content Acquisition                                       │   │
│  │     newspaper3k → trafilatura → BeautifulSoup → Playwright          │   │
│  │                          │                                          │   │
│  │  Stage 2: GPT-4o Primary Extraction                                 │   │
│  │     Ultra-specific prompt → 6-section structured JSON               │   │
│  │                          │                                          │   │
│  │  Stage 3: Perplexity Fact-Checking                                  │   │
│  │     4 verification checks → weighted confidence                     │   │
│  │                          │                                          │   │
│  │  Stage 4: Validation & Sensibility Checks                           │   │
│  │     6 validation checks → specificity overrides                     │   │
│  │                          │                                          │   │
│  │  Stage 5: Confidence Aggregation                                    │   │
│  │     Weighted average → penalties → AUTO_ACCEPT / ACCEPT / REJECT    │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │ SUPPLEMENTARY: Missing Detail Fill-In (Post-Deduplication)          │   │
│  │                                                                      │   │
│  │  DeduplicatedEvents with missing fields (attacker, vulnerability,   │   │
│  │  severity, records_affected, regulatory fines)                      │   │
│  │         │                                                            │   │
│  │         ▼                                                            │   │
│  │  Perplexity sonar-pro with field-specific queries                   │   │
│  │         │                                                            │   │
│  │         ▼                                                            │   │
│  │  Regex-based response parsing → Update DeduplicatedEvents           │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.3 When Each Pathway Runs

| Pathway | Trigger | Frequency |
|---------|---------|-----------|
| **1. Initial Extraction** | Automatically during `run_full_pipeline.py` discovery | Every pipeline run |
| **2. Perplexity Backfill** | Automatically after discovery completes | Every pipeline run |
| **3. High-Quality 5-Stage** | On-demand via `--re-enrich` flag | Manual or scheduled |
| **Supplementary Fill-In** | On-demand via `PerplexityEventEnricher` | Manual or post-deduplication |

---

## 3. Pathway 1: Initial GPT-4o-mini Extraction (Discovery Phase)

### 3.1 Purpose

Provides a fast, low-cost first pass over discovered events to extract basic structured metadata. This enables immediate filtering (removing non-cyber and non-Australian events) before the more expensive enrichment stages run.

### 3.2 Model and Configuration

| Parameter | Value |
|-----------|-------|
| Model | `gpt-4o-mini` |
| Temperature | `0.0` |
| Max retries | `2` |
| Content truncation | 12,000 characters |
| Library | `instructor` (structured output via Pydantic) |

**Source file:** `cyber_data_collector/utils/llm_extractor.py`

### 3.3 LLM Prompts

**System prompt:**

```
You are an expert cybersecurity analyst. Your task is to analyze the provided news
article text and extract key information with high accuracy. Adhere strictly to the
response format.
```

**User prompt:**

```
Please analyze the following article text and extract the required details.

Key Instructions:
1. `is_australian_event`: Set to `true` if the event involves Australia, an Australian
   company, or Australian citizens. Otherwise, `false`.
2. `is_specific_event`: This is the most important instruction. Set to `true` if the
   article describes a specific, concrete cyber incident that has already happened
   (e.g., a data breach at a named company, a ransomware attack on a specific date).
   Set to `false` if the article is about a potential future threat, a general security
   warning, a report on cyber trends, or an opinion piece about cybersecurity. Focus on
   whether a specific event is the main subject of the article.
3. `primary_entity`: Identify the main organization that was the target of the attack.
   If no single primary entity is clear, leave it as null.
4. `affected_entities`: List any other named organizations or groups mentioned as being
   affected.
5. `summary`: Provide a brief, neutral summary of the incident described.
6. `event_date`: Extract the actual date when the cyber incident occurred (NOT the
   publication date). Look for phrases like 'in June 2025', 'last month',
   'on June 5th', etc. Format as YYYY-MM-DD. If no specific date is found, set to null.
7. `records_affected`: Extract the specific number of people, customers, or data records
   affected. If a number is mentioned (e.g., '2 million customers', '50,000 records'),
   extract only the integer value. If no number is specified, set to null.

--- ARTICLE TEXT ---
{truncated_content}
```

### 3.4 Output Schema

The response is validated against the `ExtractedEventDetails` Pydantic model:

| Field | Type | Description |
|-------|------|-------------|
| `is_australian_event` | `bool` | Whether the event involves Australia |
| `is_specific_event` | `bool` | Whether the article describes a concrete incident |
| `primary_entity` | `Optional[str]` | Main affected organization |
| `affected_entities` | `List[str]` | Other affected organizations |
| `summary` | `str` | One-paragraph summary |
| `event_date` | `Optional[date]` | When the incident occurred (YYYY-MM-DD) |
| `records_affected` | `Optional[int]` | Number of affected individuals/records |

### 3.5 Justification

- **GPT-4o-mini** is used instead of GPT-4o because this is a high-volume, low-precision pass. The pipeline processes hundreds to thousands of candidate events, and the cost of GPT-4o would be prohibitive at this scale. GPT-4o-mini provides sufficient accuracy for binary classification (is_australian, is_specific) and basic entity extraction.
- **Temperature 0.0** ensures deterministic, reproducible outputs for the same input, which is important for consistency across pipeline runs.
- **Content truncation at 12,000 characters** balances the need to capture the full article context against the model's context window limits and cost. Most news articles are under this threshold.
- **The `instructor` library** provides automatic Pydantic model validation of LLM outputs, eliminating the need for manual JSON parsing and reducing extraction errors.

---

## 4. Pathway 2: Perplexity AI Enrichment (Post-Discovery)

### 4.1 Purpose

After discovery, events are enriched with Perplexity AI's real-time web search capability to validate and improve the initial extraction. Perplexity has access to current web content and can cross-reference claims against multiple authoritative sources, producing higher-quality entity names, more accurate dates, and verified threat actor attributions.

### 4.2 Event Selection Criteria

Events are selected for Perplexity enrichment based on:

```sql
SELECT ... FROM EnrichedEvents ee
WHERE ee.status = 'Active'
  AND (ee.perplexity_validated IS NULL OR ee.perplexity_validated = FALSE)
  AND ee.is_specific_event = TRUE
ORDER BY
    CASE WHEN CAST(strftime('%d', ee.event_date) AS INTEGER) = 1
         THEN 0 ELSE 1 END,   -- Placeholder dates prioritized
    ee.event_date DESC,         -- Recent events first
    ee.confidence_score ASC     -- Low-confidence events first
```

**Priority ordering rationale:**
1. Events with placeholder dates (1st of month) are prioritized because these dates are known to be imprecise approximations that Perplexity can often resolve to exact dates.
2. Recent events are processed before older ones because Perplexity's web search is more likely to find current source material.
3. Low-confidence events are processed before high-confidence ones because they have the most to gain from enrichment.

### 4.3 Model and Configuration

| Parameter | Value |
|-----------|-------|
| Model | `sonar-pro` (Perplexity) |
| Temperature | `0.1` |
| Max tokens | `1,000` |
| Rate limit | 2.0 seconds minimum between requests |
| Max retries | `3` (with exponential backoff: base 2s, max 60s) |
| API endpoint | `https://api.perplexity.ai` (via OpenAI Python client) |

**Source file:** `cyber_data_collector/processing/perplexity_enrichment.py`

### 4.4 LLM Prompt

**System message:**

```
You are a precise cybersecurity analyst. Provide accurate, well-researched
information in JSON format.
```

**User prompt:**

```
You are a cybersecurity incident analyst. Analyze the following Australian cyber
security incident and provide validated, accurate information.

INCIDENT INFORMATION:
Title: {title}
Description: {description[:500]}...

{Currently assigned date: {current_date} | No date currently assigned}
{Currently identified entity: {current_entity} | No entity currently identified}

TASK:
Research this incident using authoritative sources and provide:

1. **Earliest Event Date**: When did the incident actually occur (not when it was
   reported)?
   - Provide in YYYY-MM-DD format
   - If only month/year known, use YYYY-MM-01 or YYYY-01-01
   - Include confidence (0.0-1.0)

2. **Formal Entity Name**: What is the official legal name of the affected
   organization?
   - Not a brand name or abbreviation
   - As registered with government/regulators
   - Include confidence (0.0-1.0)

3. **Threat Actor**: Who was responsible (if known)?
   - Ransomware group, nation-state actor, individual, etc.
   - Only include if confirmed by reliable sources
   - Include confidence (0.0-1.0)

4. **Attack Method**: What was the primary attack vector?
   - Examples: ransomware, phishing, data breach, SQL injection, DDoS, etc.
   - Include confidence (0.0-1.0)

5. **Victim Count**: How many individuals/records were affected?
   - Numerical value only
   - Include confidence (0.0-1.0)

6. **Sources**: List the URLs of authoritative sources you consulted

7. **Reasoning**: Briefly explain how you arrived at these conclusions

Return your response as a JSON object with this exact structure:
{
    "earliest_event_date": "YYYY-MM-DD or null",
    "date_confidence": 0.0-1.0,
    "formal_entity_name": "Official name or null",
    "entity_confidence": 0.0-1.0,
    "victim_industry": "Industry sector or null",
    "threat_actor": "Actor name or null",
    "threat_actor_confidence": 0.0-1.0,
    "attack_method": "Method or null",
    "attack_method_confidence": 0.0-1.0,
    "victim_count": number or null,
    "victim_count_confidence": 0.0-1.0,
    "sources_consulted": ["url1", "url2", ...],
    "overall_confidence": 0.0-1.0,
    "reasoning": "Brief explanation"
}

If you cannot find reliable information for a field, set it to null and confidence
to 0.0. Only provide information you are confident about from authoritative sources.
```

### 4.5 Output Schema (Pydantic)

The response is parsed into the `PerplexityEventEnrichment` Pydantic model:

| Field | Type | Description |
|-------|------|-------------|
| `earliest_event_date` | `Optional[str]` | Earliest confirmed date (YYYY-MM-DD) |
| `date_confidence` | `Optional[float]` | Confidence in date (0.0-1.0) |
| `formal_entity_name` | `Optional[str]` | Official legal entity name |
| `entity_confidence` | `Optional[float]` | Confidence in entity (0.0-1.0) |
| `victim_industry` | `Optional[str]` | Industry sector of victim |
| `threat_actor` | `Optional[str]` | Threat actor or group name |
| `threat_actor_confidence` | `Optional[float]` | Confidence in attribution (0.0-1.0) |
| `attack_method` | `Optional[str]` | Primary attack vector |
| `attack_method_confidence` | `Optional[float]` | Confidence in method (0.0-1.0) |
| `victim_count` | `Optional[int]` | Number of affected individuals |
| `victim_count_confidence` | `Optional[float]` | Confidence in count (0.0-1.0) |
| `sources_consulted` | `List[str]` | URLs of consulted sources |
| `overall_confidence` | `float` | Overall enrichment confidence |
| `reasoning` | `Optional[str]` | Reasoning for conclusions |

### 4.6 Field Update Rules

Fields from Perplexity enrichment are only applied to the database when the per-field confidence meets or exceeds **0.6** (60%). This threshold was chosen to balance between accepting useful enrichment data and rejecting uncertain claims.

| Field | Database Column Updated | Confidence Threshold |
|-------|------------------------|---------------------|
| `earliest_event_date` | `event_date`, `date_confidence` | >= 0.6 |
| `formal_entity_name` | `entity_confidence` (logged) | >= 0.6 |
| `threat_actor` | `attacking_entity_name` | >= 0.6 |
| `attack_method` | `attack_method` | >= 0.6 |
| `victim_count` | `records_affected` | >= 0.6 |

Additionally, regardless of field-level confidence, every processed event receives:

| Column | Value |
|--------|-------|
| `perplexity_validated` | `TRUE` |
| `perplexity_validated_at` | Current ISO timestamp |
| `perplexity_enrichment_data` | Full JSON dump of enrichment result |
| `data_source_reliability` | `0.85` |

### 4.7 Rate Limiting and Retry Logic

- **Minimum request interval:** 2.0 seconds between Perplexity API calls, enforced by an `asyncio.Lock`.
- **Retry strategy:** Up to 3 retries with exponential backoff (base delay 2s, max delay 60s).
- **Authentication errors** (HTTP 401, 403) are not retried and raise immediately.
- **Between-event delay:** 2.0 seconds sleep between processing separate events.

### 4.8 Justification

- **Perplexity sonar-pro** is chosen because it combines LLM reasoning with real-time web search, enabling it to verify claims against current authoritative sources. Unlike GPT-4o which relies on training data, Perplexity can access the latest articles and official statements.
- **Per-field confidence thresholds** prevent low-quality data from overwriting existing (potentially correct) values. The 0.6 threshold means only data that Perplexity reports as moderately-to-highly confident is used.
- **Storing the full enrichment JSON** (`perplexity_enrichment_data`) preserves all information for later re-analysis without requiring another API call.
- **The 0.85 reliability score** for Perplexity-validated events reflects the high quality of multi-source-verified data while acknowledging that automated enrichment is not equivalent to manual expert review (which would score 1.0).

---

## 5. Pathway 3: High-Quality 5-Stage Enrichment Pipeline

### 5.1 Purpose and Architecture

The high-quality pipeline provides the most thorough enrichment, combining GPT-4o's superior reasoning with Perplexity's real-time verification and rule-based validation. It is designed for re-enrichment of existing events when higher-quality metadata is needed.

**Source file:** `cyber_data_collector/enrichment/high_quality_enrichment_pipeline.py`

The pipeline orchestrates five sequential stages:

```
Stage 1: Content Acquisition
    │ Fetches full article text from URL
    │ Assesses source reliability
    ▼
Stage 2: GPT-4o Primary Extraction
    │ Extracts 6 categories of structured data
    │ Applies records_affected validation
    ▼
Stage 3: Perplexity Fact-Checking
    │ Up to 4 verification checks
    │ Calculates verification confidence
    ▼
Stage 4: Validation & Sensibility Checks
    │ 6 rule-based validation checks
    │ Specificity override system
    ▼
Stage 5: Confidence Aggregation
    │ Weighted confidence from all stages
    │ Penalty system for quality issues
    │ Accept/reject decision
    ▼
    Output: enrichment_result + fact_check + validation + decision + audit_trail
```

The pipeline requires at minimum an event with a `url` field. It returns a comprehensive result dictionary containing the outputs from all five stages plus a complete audit trail.

---

### 5.2 Stage 1: Content Acquisition

**Source file:** `cyber_data_collector/enrichment/content_acquisition.py`

#### 5.2.1 Purpose

Fetches the full article text from a URL to provide GPT-4o with complete context for extraction. Relying only on titles and summaries would result in incomplete or inaccurate metadata extraction.

#### 5.2.2 Extraction Cascade

The service attempts multiple extraction methods in sequence, falling through to the next method if the current one fails to produce at least 200 characters of content (100 characters for the final acceptance threshold):

| Order | Method | Library | Best For | Min Content |
|-------|--------|---------|----------|-------------|
| 0 | Cached content | (in-memory) | Events with pre-fetched content | 200 chars |
| 0b | PDF extraction | pdfplumber / PyPDF2 | PDF URLs (detected by extension) | 200 chars |
| 1 | newspaper3k | `newspaper` | Standard news articles | 200 chars |
| 2 | trafilatura | `trafilatura` | Difficult sites, blogs | 200 chars |
| 3 | BeautifulSoup | `beautifulsoup4` | Basic HTML parsing | 200 chars |
| 4 | Playwright | `playwright` | JavaScript-heavy sites | 200 chars |

**Cascade justification:**
- **newspaper3k** is tried first because it is purpose-built for news article extraction and handles most standard news sites well, including metadata extraction (publication date).
- **trafilatura** is the second choice because it uses more aggressive content extraction heuristics that work on non-standard site layouts where newspaper3k fails.
- **BeautifulSoup** as a third fallback provides direct HTML parsing with selectors targeting common article container elements: `article`, `.article-content`, `.post-content`, `.entry-content`, `#content`, `.content`, `main`.
- **Playwright** is the last resort because it requires launching a full browser process (slower, more resource-intensive), but it can handle JavaScript-rendered content that all other methods miss.

#### 5.2.3 BeautifulSoup Content Selectors

When using BeautifulSoup, the following HTML elements are first removed (noise reduction): `script`, `style`, `nav`, `header`, `footer`, `aside`.

Then article content is sought using these CSS selectors in order:

1. `article`
2. `.article-content`
3. `.post-content`
4. `.entry-content`
5. `#content`
6. `.content`
7. `main`

If no selector matches, all `<p>` tags in the document are concatenated.

#### 5.2.4 Source Reliability Scoring

Each source domain is assigned a reliability score (0.0-1.0) based on a hardcoded lookup table. The reliability score is later used in confidence aggregation (Stage 5).

**Trusted source scores:**

| Category | Domain | Score |
|----------|--------|-------|
| **Government (highest trust)** | `oaic.gov.au` | 1.0 |
| | `cyber.gov.au` | 1.0 |
| | `acsc.gov.au` | 1.0 |
| **Australian quality news** | `abc.net.au` | 1.0 |
| | `smh.com.au` | 0.95 |
| | `theage.com.au` | 0.95 |
| | `afr.com` | 0.95 |
| | `theaustralian.com.au` | 0.9 |
| **Cybersecurity investigative** | `krebsonsecurity.com` | 0.95 |
| | `zdnet.com` | 0.9 |
| | `arstechnica.com` | 0.9 |
| | `itnews.com.au` | 0.9 |
| **Cybersecurity trade** | `cyberdaily.au` | 0.85 |
| | `bleepingcomputer.com` | 0.85 |
| | `cyberscoop.com` | 0.85 |
| | `darkreading.com` | 0.85 |
| | `threatpost.com` | 0.85 |
| **Australian commercial news** | `news.com.au` | 0.85 |
| | `9news.com.au` | 0.85 |
| | `7news.com.au` | 0.85 |
| **Tech news** | `techcrunch.com` | 0.85 |
| | `theverge.com` | 0.85 |
| **Unknown domains** | *(default)* | 0.6 |
| **Cached/PDF content** | *(default)* | 0.7 |

**Scoring justification:**
- Government sources receive 1.0 because they are official primary sources with legal obligations for accuracy.
- ABC News receives 1.0 as Australia's publicly-funded national broadcaster with strong editorial standards.
- Major broadsheets (SMH, Age, AFR) receive 0.95 as respected outlets with dedicated technology/security reporters.
- Cybersecurity specialist outlets receive 0.85-0.95 based on their track record for accurate technical reporting.
- The default of 0.6 for unknown domains reflects that unrecognized sources may be legitimate but cannot be verified programmatically.

#### 5.2.5 Output Format

```python
{
    'title': str,               # Event title (preferred) or URL
    'url': str,                 # Source URL
    'full_text': str,           # Complete extracted article text
    'clean_summary': str,       # First 500 chars or sentence-boundary truncation
    'publication_date': str,    # ISO date if extractable, else None
    'source_domain': str,       # Extracted domain (www. prefix removed)
    'source_reliability': float, # 0.0-1.0 from lookup table
    'content_length': int,      # Word count of full_text
    'extraction_method': str,   # Which method succeeded
    'extraction_success': bool, # True if content acquired
    'error': str or None        # Error message if failed
}
```

---

### 5.3 Stage 2: GPT-4o Primary Extraction

**Source file:** `cyber_data_collector/enrichment/gpt4o_enricher.py`

#### 5.3.1 Model and Configuration

| Parameter | Value |
|-----------|-------|
| Model | `gpt-4o` |
| Temperature | `0.1` |
| Response format | `{"type": "json_object"}` (forced JSON output) |
| Content truncation | 8,000 characters |

#### 5.3.2 System Message

```
You are a precise cybersecurity incident analyst. Always respond with valid JSON only.
Be extremely conservative and accurate in your extractions.
```

#### 5.3.3 Complete Extraction Prompt

The extraction prompt is constructed dynamically and contains six major extraction sections. The full prompt is approximately 5,000 tokens and includes article content, extraction rules, few-shot examples, and output format specification.

**Prompt preamble (article context):**

```
You are a cybersecurity incident analyst extracting structured data from news
articles about cyber attacks.

ARTICLE CONTENT:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Title: {title}
URL: {url}
Publication Date: {publication_date}
Source Reliability: {source_reliability}

Full Article Text:
{full_text (truncated to 8,000 chars)}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EXTRACTION TASK:
Extract the following information about this cyber security incident. Be
EXTREMELY precise and conservative.
```

---

#### 5.3.4 Section 1: Victim Organization Extraction

**Title prioritization rule:** The prompt instructs the model to check the article TITLE first to identify the primary victim. This is the most critical rule because aggregate blog posts and news roundups often mention multiple organizations, but the title indicates the article's primary focus.

**Few-shot examples included in the prompt:**

The prompt includes three worked examples that demonstrate common error cases:

1. **Aggregate blog post with specific title** -- An article from `fortian.com.au/blog/august-2025-cyber-update.html` with title "iiNet Data Breach Exposes 280,000 Customers" that also mentions Optus, Dutch telecoms, and universities. The correct extraction is "iiNet" (from the title), not other mentioned organizations.

2. **Aggregate blog with multiple mentions** -- An article mentioning iiNet, Optus, Medibank, Salt Typhoon, and Qantas. The correct extraction is "iiNet" (matches the title), not any contextually-mentioned organization.

3. **Parent company vs subsidiary** -- An article about "TPG Telecom flags cyber incident in its iiNet system". Either "iiNet" or "TPG Telecom" is acceptable because both appear in the title.

**Extraction rules (from the prompt):**

- Extract the SPECIFIC organization that was DIRECTLY attacked/breached
- Use the exact legal name when mentioned (e.g., "Medibank Private Limited" or "Medibank")
- For government agencies, include full name (e.g., "Department of Home Affairs")
- For universities, use full official name (e.g., "Western Sydney University" not "WSU")

**Do NOT extract:**
- Organizations mentioned ONLY for comparison
- Organizations mentioned as clients/customers of the actual victim
- Security researchers, experts, or consultants quoted in the article
- Parent companies unless they were also directly breached
- Generic references ("an Australian company", "a healthcare provider")

**Industry categories** (NIST-based):

`GOVERNMENT`, `FINANCIAL_SERVICES`, `HEALTHCARE`, `EDUCATION`, `TECHNOLOGY`, `MEDIA_COMMUNICATIONS`, `RETAIL_COMMERCE`, `TELECOMMUNICATIONS`, `TRANSPORTATION`, `ENERGY_UTILITIES`, `MANUFACTURING`, `NON_PROFIT`, `LEGAL_SERVICES`, `REAL_ESTATE`, `AGRICULTURE`, `MINING`, `CONSTRUCTION`, `HOSPITALITY`, `ENTERTAINMENT`, `OTHER`

**Output fields:**

| Field | Type | Description |
|-------|------|-------------|
| `victim.organization` | `str` or `null` | Exact organization name |
| `victim.industry` | `str` or `null` | One of the NIST categories |
| `victim.is_australian` | `bool` | Whether the victim is Australian-based |
| `victim.confidence` | `float` | 0.0-1.0 extraction confidence |
| `victim.reasoning` | `str` | 1-2 sentence justification citing article text |

---

#### 5.3.5 Section 2: Attacker Information Extraction

**Rules:**
- Extract threat actor name only if EXPLICITLY mentioned (e.g., "LockBit", "APT29", "REvil")
- Extract ransomware/malware name if mentioned
- Classify attacker type based on evidence
- Use "Unknown" for unidentified attackers; do NOT extract speculation

**Attacker type taxonomy:**

| Type | Description | Examples |
|------|-------------|---------|
| `nation_state` | State-sponsored actors | APT29, Fancy Bear |
| `ransomware_gang` | Ransomware groups | LockBit, REvil |
| `hacktivist` | Ideologically motivated | Anonymous |
| `cybercriminal` | Financially motivated criminals | — |
| `insider` | Internal threat actor | — |
| `unknown` | Cannot determine from article | — |

**Output fields:**

| Field | Type | Description |
|-------|------|-------------|
| `attacker.name` | `str` | Threat actor name or "Unknown" |
| `attacker.type` | `str` | One of the attacker types above |
| `attacker.malware` | `str` or `null` | Malware/ransomware name |
| `attacker.attack_vector` | `str` or `null` | How the attack was conducted |
| `attacker.confidence` | `float` | 0.0-1.0 |
| `attacker.reasoning` | `str` | Evidence cited from article |

---

#### 5.3.6 Section 3: Incident Details Extraction

**Date extraction rules:**
- `incident_date`: When the attack/breach occurred (YYYY-MM-DD)
- `discovery_date`: When the breach was discovered (YYYY-MM-DD)
- `disclosure_date`: When publicly announced (YYYY-MM-DD)

**Severity levels:**

| Level | Criteria |
|-------|----------|
| `critical` | National security impact, millions affected, critical infrastructure |
| `high` | Significant impact, thousands affected, sensitive data (health, financial) |
| `medium` | Moderate impact, hundreds affected, standard personal data |
| `low` | Minor impact, minimal data exposure |
| `unknown` | Cannot determine from article |

**Records affected extraction rules (from the prompt):**

This is the most extensively documented field in the prompt due to the high error rate in LLM number parsing. The prompt includes:

**Definition:** Number of distinct individuals/customers/users whose personal data was compromised. This is NOT transaction counts, database records, files, bytes, dollars, or financial figures.

**Unit parsing rules (critical):**
- "6 million" means 6,000,000 (NOT 6)
- "280 thousand" means 280,000 (NOT 280)
- "1.5 million" means 1,500,000 (NOT 15 or 1500)
- ALWAYS multiply by the stated unit (thousand = x1,000, million = x1,000,000)
- Minimum realistic value: 50 records (anything smaller indicates missed units)

**What to extract (with examples):**
- "280,000 customers affected" → 280000
- "9.7 million people" → 9700000
- "500,000 individuals" → 500000
- "6 million records" → 6000000

**What NOT to extract (use null instead):**
- "3 million transactions processed" → null (transactions ≠ customers)
- "500 GB of data stolen" → null (bytes ≠ customers)
- "$2.5 million fine" → null (dollars ≠ customers)
- Revenue, turnover, or financial figures → null

**Validation constraints:**
- Maximum realistic value: 1,000,000,000 (1 billion)
- Minimum realistic value: 50
- If unsure whether the number refers to people, use null

**Output fields:**

| Field | Type | Description |
|-------|------|-------------|
| `incident.date` | `str` or `null` | YYYY-MM-DD |
| `incident.discovery_date` | `str` or `null` | YYYY-MM-DD |
| `incident.disclosure_date` | `str` or `null` | YYYY-MM-DD |
| `incident.severity` | `str` | One of the severity levels |
| `incident.records_affected` | `int` or `null` | Number of people affected |
| `incident.data_types` | `List[str]` | Types of data compromised |
| `incident.confidence` | `float` | 0.0-1.0 |
| `incident.reasoning` | `str` | Justification for severity and data support |

---

#### 5.3.7 Section 4: Australian Relevance Assessment

**Criteria for Australian relevance:**
- Australian organization was the victim
- Australian citizens' data was compromised
- Attack originated from or targeted Australian infrastructure
- Significant impact on Australian critical infrastructure

**Output fields:**

| Field | Type | Description |
|-------|------|-------------|
| `australian_relevance.is_australian_event` | `bool` | Binary relevance flag |
| `australian_relevance.relevance_score` | `float` | 0.0-1.0 continuous score |
| `australian_relevance.reasoning` | `str` | Justification |

---

#### 5.3.8 Section 5: Event Specificity Assessment

This section determines whether the article describes a CONCRETE cyber incident (useful for the dataset) versus general commentary, advice, or trend reporting (not useful).

**Decision rule from the prompt:**

> Ask yourself: "After reading this article, can I name:
> 1. Which specific organization was attacked?
> 2. What type of attack it was?
> 3. When it happened (approximately)?"
>
> If YES to all 3 → `is_specific_incident: TRUE`
> If NO to any → `is_specific_incident: FALSE`

**Specific incident examples (true):**
- Official breach announcements
- Investigation or analysis of a specific incident
- Timeline or retrospective of a known incident
- Expert commentary on a specific named incident
- Lessons from a specific incident used as case study

**Not specific (false):**
- General cybersecurity news without a specific victim
- Educational content ("How to protect your business")
- Industry trend reporting
- Opinion pieces about cybersecurity policy
- Multiple separate incidents in one article (unless title focuses on one)

**Output fields:**

| Field | Type | Description |
|-------|------|-------------|
| `specificity.is_specific_incident` | `bool` | Whether a concrete incident is described |
| `specificity.reasoning` | `str` | Justification citing article details |

---

#### 5.3.9 Section 6: Multi-Victim Detection

Distinguishes between a single attack affecting multiple organizations versus one organization being breached with downstream impact on clients.

**Multi-victim (true):**
- A SINGLE attack/campaign simultaneously affecting multiple distinct organizations
- Example: SolarWinds supply chain attack affecting Microsoft, FireEye, and government agencies

**NOT multi-victim (false):**
- One organization breached, affecting its clients (e.g., HWL Ebsworth breached, exposing government department data)
- Article mentions past breaches for context
- Aggregate news roundups covering separate incidents

**Output fields:**

| Field | Type | Description |
|-------|------|-------------|
| `multi_victim.has_multiple_victims` | `bool` | True only for multi-org campaigns |
| `multi_victim.victim_organizations` | `List[str]` or `null` | Names if multi-victim |
| `multi_victim.reasoning` | `str` | Justification |

---

#### 5.3.10 Complete JSON Output Format

```json
{
  "victim": {
    "organization": "exact organization name or null",
    "industry": "NIST category or null",
    "is_australian": true,
    "confidence": 0.85,
    "reasoning": "explanation citing article text"
  },
  "attacker": {
    "name": "threat actor name or Unknown",
    "type": "nation_state|ransomware_gang|hacktivist|cybercriminal|insider|unknown",
    "malware": "malware name or null",
    "attack_vector": "vector description or null",
    "confidence": 0.75,
    "reasoning": "evidence citation"
  },
  "incident": {
    "date": "YYYY-MM-DD or null",
    "discovery_date": "YYYY-MM-DD or null",
    "disclosure_date": "YYYY-MM-DD or null",
    "severity": "critical|high|medium|low|unknown",
    "records_affected": 280000,
    "data_types": ["personal_information", "financial_data"],
    "confidence": 0.8,
    "reasoning": "severity and data justification"
  },
  "australian_relevance": {
    "is_australian_event": true,
    "relevance_score": 0.95,
    "reasoning": "explanation"
  },
  "specificity": {
    "is_specific_incident": true,
    "reasoning": "explanation"
  },
  "multi_victim": {
    "has_multiple_victims": false,
    "victim_organizations": null,
    "reasoning": "explanation"
  },
  "overall_confidence": 0.82,
  "extraction_notes": "any caveats"
}
```

#### 5.3.11 Post-Extraction Processing

After GPT-4o returns the JSON, the `records_affected` value undergoes additional validation via `llm_validate_records_affected()` (see [Section 7](#7-records-affected-validation)). This may adjust the value or set it to `null` if it fails plausibility checks.

Token usage is recorded via the token tracker for cost monitoring.

#### 5.3.12 Justification

- **GPT-4o** (not GPT-4o-mini) is used because the extraction task requires nuanced reasoning about victim identification (especially in aggregate articles), multi-victim detection, and specificity assessment. GPT-4o's larger model size provides materially better accuracy on these tasks.
- **Temperature 0.1** provides near-deterministic output while allowing slight variation for edge cases where the model might otherwise default to a less-optimal deterministic answer.
- **Forced JSON output** (`response_format: json_object`) eliminates JSON parsing errors that commonly occur with free-text LLM responses.
- **The extensive few-shot examples** address the most common extraction failure mode: incorrectly extracting contextually-mentioned organizations instead of the primary victim from aggregate articles and blog posts.
- **The detailed records_affected instructions** address the second most common failure mode: LLMs misinterpreting "6 million" as the integer 6, or extracting financial figures as record counts.

---

### 5.4 Stage 3: Perplexity Fact-Checking

**Source file:** `cyber_data_collector/enrichment/perplexity_fact_checker.py`

#### 5.4.1 Purpose

Cross-validates the claims extracted by GPT-4o in Stage 2 using Perplexity's real-time web search. This provides independent verification from current authoritative sources rather than relying solely on a single LLM's extraction.

#### 5.4.2 Model and Configuration

| Parameter | Value |
|-----------|-------|
| Model | `sonar-pro` (Perplexity) |
| Temperature | `0.1` |
| Max tokens | `1,000` |
| Max retries | `3` (exponential backoff: 1s, 2s, 4s) |
| Timeout | 60 seconds per request |

#### 5.4.3 Verification Checks

Up to four verification checks are performed, each conditionally triggered based on the available extracted data:

---

**Check 1: Organization Exists** (triggered when `victim.organization` is present)

*Prompt:*
```
Is "{org_name}" a real, specific organization (not a generic description)?

Search for information about this organization and answer with JSON:

{
  "is_real_organization": true or false,
  "organization_type": "company|government|nonprofit|university|other|generic",
  "country": "primary country of operation or null",
  "confidence": 0.0 to 1.0,
  "reasoning": "brief explanation with sources if possible",
  "sources": ["URL1", "URL2"] or []
}

CRITICAL RULES:
- is_real_organization: FALSE if it's a generic description like "Australian IT
  company" or "healthcare provider"
- is_real_organization: TRUE only if you can confirm it's a specific legal entity
  with web presence
- Provide sources (URLs) if you find them
- If you cannot find ANY information about this organization, confidence should
  be < 0.3

Examples:
- "Medibank Private" → is_real_organization: true (major Australian health insurer)
- "Australian IT company" → is_real_organization: false (generic description)
- "John Smith" → is_real_organization: false (person's name, not organization)
```

---

**Check 2: Incident Occurred** (triggered when both `victim.organization` AND `incident.date` are present)

*Prompt:*
```
Did "{org_name}" experience a cybersecurity incident (data breach, ransomware
attack, cyber attack, etc.) on or around {incident_date}?

Search for reliable news sources and answer with JSON:

{
  "incident_confirmed": true or false,
  "actual_date": "YYYY-MM-DD if different from provided date, or null",
  "incident_type": "type of incident or null",
  "sources": ["URL1", "URL2"],
  "confidence": 0.0 to 1.0,
  "reasoning": "brief explanation citing sources"
}

CRITICAL RULES:
- incident_confirmed: TRUE only if you find credible news sources confirming
  the incident
- If date is slightly different (within 30 days), still confirm but provide
  actual_date
- Prefer Australian news sources (ABC, SMH, AFR) and cybersecurity sources
  (iTnews, ZDNet)
- If you cannot find ANY sources, confidence should be < 0.3
- Include at least 2 sources if possible
```

---

**Check 3: Attacker Attribution** (triggered when `attacker.name` is not "Unknown" or null)

*Prompt:*
```
Has the threat actor "{attacker}" been credibly linked to a cyber attack on
"{victim}"?

Search for attribution information and answer with JSON:

{
  "attribution_confirmed": true or false,
  "attribution_confidence": "confirmed|suspected|unverified",
  "sources": ["URL1", "URL2"],
  "confidence": 0.0 to 1.0,
  "reasoning": "brief explanation"
}

CRITICAL RULES:
- attribution_confirmed: TRUE only if multiple credible sources confirm the
  attribution
- attribution_confidence:
  * "confirmed": Official attribution by government/cybersecurity firms
  * "suspected": Multiple sources suggest link but not officially confirmed
  * "unverified": Single source or speculation only
- Consider attribution claims carefully - they are often uncertain
- If no sources found, confidence < 0.3
```

---

**Check 4: Records Count** (triggered when `incident.records_affected` is present)

*Prompt:*
```
In the "{org_name}" cyber incident, were approximately {records_count:,}
records/people affected?

Search for reliable sources and answer with JSON:

{
  "count_confirmed": true or false,
  "actual_count": integer or null,
  "count_range": "e.g., 9-10 million, or 50,000-100,000",
  "sources": ["URL1", "URL2"],
  "confidence": 0.0 to 1.0,
  "reasoning": "brief explanation citing sources"
}

CRITICAL RULES:
- count_confirmed: TRUE if the number is within ±20% of the stated number
- If you find a significantly different number (>20% variance), set
  count_confirmed: FALSE and provide actual_count
- Provide count_range if sources give a range rather than exact number
- Prefer official statements from the organization or regulatory filings
- If no sources found, confidence < 0.3
```

Records count verification uses a **±20% variance tolerance**: if Perplexity reports an `actual_count`, the check passes if `abs(extracted - actual) / extracted <= 0.20`.

---

#### 5.4.4 Verification Confidence Calculation

The overall verification confidence is calculated as a weighted average of individual check results:

| Check Type | Weight | Rationale |
|------------|--------|-----------|
| `organization_exists` | 0.40 | Most fundamental -- if the org isn't real, nothing else matters |
| `incident_occurred` | 0.40 | Second most important -- confirms the event actually happened |
| `attacker_attribution` | 0.10 | Useful but often uncertain, even for real incidents |
| `records_affected` | 0.10 | Useful but frequently estimated/revised over time |

**Scoring formula for each check:**

```python
if check.verified:
    contribution = weight * check.confidence
else:
    contribution = weight * (1.0 - check.confidence) * 0.5  # 50% penalty
```

The 50% penalty on failed checks means that even a confidently-failed check does not reduce the score to zero, acknowledging that Perplexity may not have access to all relevant sources.

**Final confidence:** `sum(contributions) / sum(weights)`

#### 5.4.5 Justification

- **Four separate checks** rather than a single omnibus query allow targeted verification of distinct claims. A failed records count check should not invalidate a confirmed organization identity.
- **The 40/40/10/10 weighting** reflects the relative importance: confirming the organization exists and the incident occurred are foundational requirements; attacker attribution and exact record counts are supplementary details that are often uncertain even in authoritative sources.
- **The ±20% tolerance** on records counts acknowledges that breach counts are frequently revised upward or reported as ranges in different sources.
- **Preferring Australian and cybersecurity sources** in the incident verification prompt reduces the risk of false negatives for Australian-specific events that may not appear in international media.

---

### 5.5 Stage 4: Validation and Sensibility Checks

**Source file:** `cyber_data_collector/enrichment/enrichment_validator.py`

#### 5.5.1 Purpose

Performs rule-based validation on the GPT-4o extraction results to catch common LLM errors, logical impossibilities, and classification mistakes before the data enters the database.

#### 5.5.2 Validation Checks

**Check 1: Organization Name Validation**

Rejects or warns about organization names matching known error patterns:

*Generic description patterns (ERROR -- extraction rejected):*

| Pattern | Example Caught |
|---------|---------------|
| `australian\s+\w+\s+(company\|firm\|organization)` | "Australian IT company" |
| `new\s+zealand\s+\w+\s+(company\|firm)` | "New Zealand tech firm" |
| `\w+\s+sector\s+organization` | "healthcare sector organization" |
| `\w+\s+(companies\|organizations\|firms)$` | "multiple companies" |
| `^(dutch\|german\|...)\s+\w+\s+companies` | "Dutch telecom companies" |
| `^an?\s+\w+\s+company` | "a healthcare company" |
| `approximately\s+\d+\s+organizations` | "approximately 50 organizations" |

*Person name patterns (ERROR):*

| Pattern | Example Caught |
|---------|---------------|
| `^(mr\|mrs\|ms\|dr\|prof)\.\s+\w+\s+\w+` | "Dr. Jane Smith" |
| `^\w+\s+\w+\s+(ceo\|cto\|ciso\|expert)$` | "John Smith CEO" |

*Other validation rules:*
- Names shorter than 2 characters → ERROR
- Names longer than 150 characters → WARNING
- Reserved words ("unknown", "n/a", "null", "none", "not specified") → ERROR

---

**Check 1b: Title Matching Validation**

Verifies that the extracted victim organization appears in the article title. If the victim name (or a significant word from it) does not appear in the title, this indicates a potential error where GPT-4o extracted a contextually-mentioned organization instead of the primary subject.

The check splits the organization name into key words (filtering out corporate suffixes like "limited", "ltd", "inc", "corp", "pty", "australia", "group", "holdings") and checks if any appear in the title.

If no match is found, a warning is generated. If the URL also matches aggregate content patterns (`blog/`, `weekly`, `monthly`, `roundup`, `digest`, `update`, `news-feed`, `bulletin`, `newsletter`, `recap`), the warning notes this as likely aggregate content.

---

**Check 2: Date Plausibility**

| Rule | Result |
|------|--------|
| Incident date in the future | ERROR |
| Incident date before 1990 | ERROR (predates modern cybersecurity era) |
| Discovery date before incident date | ERROR (logically impossible) |
| Disclosure date before discovery date | ERROR (unusual ordering) |
| Invalid date format | ERROR |

---

**Check 3: Cross-Field Consistency**

| Rule | Result |
|------|--------|
| Severity "critical" but records_affected < 1,000 | WARNING |
| Severity "low" but records_affected > 100,000 | WARNING |
| Known organization misclassified industry | WARNING |

Known organization-to-industry mappings used for consistency checking:

| Organization | Expected Industry |
|-------------|-------------------|
| Medibank | HEALTHCARE |
| Qantas, Virgin Australia | TRANSPORTATION |
| Telstra, Optus, TPG, iiNet | TELECOMMUNICATIONS |
| Commonwealth Bank, Westpac, ANZ, NAB | FINANCIAL_SERVICES |
| Woolworths, Coles | RETAIL_COMMERCE |
| BHP, Rio Tinto, Fortescue | MINING |

---

**Check 4: Fact-Check Alignment**

Any failed fact-checks from Stage 3 are surfaced as warnings in the validation result. Each failed Perplexity check generates a warning message: `"Failed fact-check ({check_type}): {query}"`.

---

**Check 5: Duplicate Detection**

Queries the `EnrichedEvents` table for existing active events with a matching organization name in the title and the same event date:

```sql
SELECT enriched_event_id, title
FROM EnrichedEvents
WHERE title LIKE '%{org_name}%'
  AND event_date = '{event_date}'
  AND status = 'Active'
LIMIT 5
```

If matches are found, a warning is generated with the ID and title of the potential duplicate.

---

**Check 6: Specificity Override System**

This check corrects GPT-4o's specificity classification using three heuristic rules. This addresses observed systematic errors where GPT-4o marks specific incidents as non-specific (missing real events) or marks educational content as specific (admitting noise).

**Rule 1: Override False → True (concrete incident indicators)**

*Condition:* `is_specific == False` AND has named victim AND `australian_relevance > 0.7` AND has at least one of:
- `records_affected > 0`
- `incident_date` is not null
- `attack_type` is a known specific type (not "cyber incident", "unknown", or "not specified")

*Action:* Override to `is_specific = True` with logged warning.

*Rationale:* If an event has a named Australian victim AND concrete incident details (date, records, attack type), it is almost certainly a specific incident regardless of how the article frames it.

**Rule 2: Override False → True (title-based detection)**

*Condition:* `is_specific == False` AND title contains incident keywords AND victim name appears in title AND URL is not aggregate AND `australian_relevance > 0.3`

*Incident keywords:* `breach`, `attack`, `hack`, `ransomware`, `incident`, `compromised`, `exposed`, `hit by`, `flags`, `reports`, `confirms`, `discloses`, `reveals`, `data leak`, `cyberattack`

*Aggregate URL patterns:* `blog/`, `weekly`, `monthly`, `roundup`, `digest`, `update`

*Action:* Override to `is_specific = True`.

*Rationale:* Article titles containing both an organization name and incident-related keywords strongly indicate a specific incident report.

**Rule 3: Override True → False (educational content)**

*Condition:* `is_specific == True` AND title starts with educational prefix AND no victim identified

*Educational prefixes:* `how to`, `guide to`, `best practices`, `tips for`, `5 ways`, `10 steps`

*Action:* Override to `is_specific = False`.

*Rationale:* Articles beginning with instructional phrases without identifying a specific victim are educational content, not incident reports.

---

#### 5.5.3 Validation Confidence Calculation

```python
confidence = 1.0
confidence -= len(errors) * 0.3      # -0.3 per error
confidence -= len(warnings) * 0.1    # -0.1 per warning

if fact_check_performed:
    fact_check_pass_rate = checks_passed / checks_performed
    confidence = (confidence + fact_check_pass_rate) / 2

confidence = max(0.0, min(1.0, confidence))  # Clamp to [0, 1]
```

---

### 5.6 Stage 5: Confidence Aggregation and Decision

**Source file:** `cyber_data_collector/enrichment/high_quality_enrichment_pipeline.py` (method `_aggregate_confidence`)

#### 5.6.1 Weighted Average

The final confidence score combines four input signals:

| Signal | Weight | Source |
|--------|--------|--------|
| GPT-4o extraction confidence | 30% | `overall_confidence` from Stage 2 output |
| Perplexity verification confidence | 30% | `overall_verification_confidence` from Stage 3 |
| Validation confidence | 20% | `validation_confidence` from Stage 4 |
| Source reliability | 20% | `source_reliability` from Stage 1 |

```python
final_confidence = (
    0.30 * gpt_conf +
    0.30 * fact_conf +
    0.20 * val_conf +
    0.20 * source_conf
)
```

**Weight justification:** GPT-4o extraction and Perplexity fact-checking are weighted equally (30% each) because they provide complementary signals: GPT-4o assesses internal coherence of the article while Perplexity provides external verification. Validation and source reliability are weighted lower (20% each) because they serve as corrective signals rather than primary quality indicators.

#### 5.6.2 Penalty System

After computing the weighted average, multiplicative penalties are applied for detected quality issues. Penalties compound when multiple issues are present.

| Condition | Penalty Multiplier | Effect |
|-----------|--------------------|--------|
| Validation errors present | × 0.3 | 70% reduction (severe) |
| More than 3 warnings | × 0.8 | 20% reduction |
| Non-specific incident (`is_specific == False`) | × 0.8 | 20% reduction |
| Low Australian relevance (< 0.3) | × 0.4 | 60% reduction |
| Low fact-check pass rate (< 50%) | × 0.5 | 50% reduction |
| Geographic mismatch (title says "Australian" but relevance < 0.3) | × 0.3 | 70% reduction |

**Example penalty application:**

For an event with validation errors AND low Australian relevance:
```
final = weighted_average × 0.3 × 0.4 = weighted_average × 0.12
```
This aggressive compounding ensures that events with multiple quality issues are nearly always rejected.

#### 5.6.3 Decision Thresholds

| Final Confidence | Decision | Action |
|-----------------|----------|--------|
| >= 0.8 | `AUTO_ACCEPT` | Store in database with high confidence |
| >= 0.5 | `ACCEPT_WITH_WARNING` | Store in database with flag for manual review |
| < 0.5 | `REJECT` | Do not store; flag for manual review |

**Threshold justification:**
- **0.8 for AUTO_ACCEPT** is deliberately high to ensure only well-verified events enter the dataset without review. An event needs strong signals from extraction, fact-checking, validation, AND source reliability to achieve this threshold.
- **0.5 for ACCEPT_WITH_WARNING** allows borderline events to be stored while flagging them. This prevents losing real events that may have insufficient web coverage for full verification.
- **Below 0.5 REJECT** removes events where the pipeline has more doubt than confidence. These may be generic articles, non-Australian events, or fictional/speculative content.

#### 5.6.4 Complete Decision Output

```python
{
    'final_confidence': 0.73,
    'decision': 'ACCEPT_WITH_WARNING',
    'action': 'Store in database with flag for manual review',
    'stage_confidences': {
        'gpt4o_extraction': 0.85,
        'perplexity_fact_check': 0.72,
        'validation': 0.80,
        'source_reliability': 0.90
    },
    'applied_penalties': {
        'non_specific_incident': 0.8
    },
    'fact_check_summary': {
        'checks_performed': 3,
        'checks_passed': 2,
        'checks_failed': 1
    }
}
```

---

### 5.7 Audit Trail Storage

**Source file:** `cyber_data_collector/enrichment/enrichment_audit_storage.py`

#### 5.7.1 Purpose

Every event processed through the 5-stage pipeline has its complete processing history persisted to the `EnrichmentAuditTrail` database table. This enables:
- Post-hoc analysis of enrichment quality
- Identification of systematic extraction errors
- Auditing of accept/reject decisions
- Debugging of pipeline failures

#### 5.7.2 Database Schema

The `EnrichmentAuditTrail` table stores 34 columns covering all five stages:

| Column Group | Columns | Description |
|-------------|---------|-------------|
| **Identity** | `audit_id` (UUID PK), `enriched_event_id`, `pipeline_version` | Event linkage |
| **Timing** | `started_at`, `completed_at`, `total_time_seconds` | Processing duration |
| **Final** | `final_decision`, `final_confidence` | Accept/reject outcome |
| **Stage 1** | `stage1_success`, `stage1_extraction_method`, `stage1_content_length`, `stage1_source_reliability`, `stage1_details` (JSON) | Content acquisition results |
| **Stage 2** | `stage2_success`, `stage2_victim_organization`, `stage2_confidence`, `stage2_is_specific_incident`, `stage2_australian_relevance`, `stage2_tokens_used`, `stage2_details` (JSON) | GPT-4o extraction results |
| **Stage 3** | `stage3_checks_performed`, `stage3_checks_passed`, `stage3_checks_failed`, `stage3_verification_confidence`, `stage3_details` (JSON) | Fact-check results |
| **Stage 4** | `stage4_is_valid`, `stage4_error_count`, `stage4_warning_count`, `stage4_validation_confidence`, `stage4_details` (JSON) | Validation results |
| **Stage 5** | `stage5_stage_confidences` (JSON), `stage5_penalties_applied` (JSON) | Confidence aggregation details |
| **Errors** | `error_message`, `error_stage` | Failure information |

The JSON detail columns store the full structured output from each stage, enabling complete reconstruction of the enrichment decision without re-running the pipeline.

---

## 6. Supplementary Enrichment: Missing Detail Fill-In

### 6.1 Purpose

After deduplication, some events may still have missing fields (attacker unknown, no vulnerability details, etc.). The `PerplexityEventEnricher` fills these gaps by constructing targeted queries for each missing field.

**Source file:** `cyber_data_collector/processing/perplexity_enricher.py`

### 6.2 Fields Checked

| Field | Missing Condition |
|-------|-------------------|
| `attacker` | `threat_actor` is null, empty, or "Unknown" |
| `vulnerability` | `vulnerability_details` is null or shorter than 50 characters |
| `vulnerability_category` | `vulnerability_category` is null |
| `regulatory_fines` | `regulatory_fine_amount` is null |
| `severity` | `severity` is null or "Unknown" |
| `records_affected` | `records_affected` is null or 0 |

Events enriched within the last 30 days are skipped (unless `force=True`), preventing unnecessary API calls for recently-processed events.

### 6.3 Query Construction

For each missing field, a natural-language question is generated and concatenated into a single Perplexity query:

| Missing Field | Query Template |
|---------------|---------------|
| `attacker` | "Who was responsible for the {title} cyber attack on {entity} in {date}? What threat actor or group claimed responsibility?" |
| `vulnerability` | "What security vulnerability or weakness allowed the {title} attack on {entity} in {date}? What was the root cause or security flaw exploited?" |
| `vulnerability_category` | "Classify the vulnerability into one of these categories: {17 categories}. Provide the category and explain why." |
| `regulatory_fines` | "Were any regulatory fines or penalties imposed after the {title} incident involving {entity} in {date}? If so, what was the amount and which regulator imposed it?" |
| `severity` | "What was the severity and impact of the {title} cyber incident involving {entity} in {date}? Was it classified as critical, high, medium, or low severity?" |
| `records_affected` | "How many records or accounts were affected in the {title} incident involving {entity} in {date}? What was the scale of the data breach?" |

### 6.4 Response Parsing

Perplexity's free-text response is parsed using regex-based extractors for each field type:

**Attacker extraction:** Matches patterns like "attributed to {Name}", "claimed by {Name}", "threat actor {Name}". Falls back to "Unknown (not publicly disclosed)" if the response contains "unknown", "not disclosed", or "not publicly identified".

**Vulnerability category classification:** Uses keyword matching against 17 vulnerability categories:

| Category | Keywords |
|----------|----------|
| Authentication Weakness | password, credential, authentication, login |
| Access Control Failure | access control, unauthorized access, privilege |
| Injection Attacks | injection, sql injection, command injection |
| Phishing/Social Engineering | phishing, social engineering, email |
| Ransomware | ransomware, encryption, ransom |
| Malware | malware, trojan, virus, backdoor |
| Configuration Error | misconfigured, configuration, exposed |
| Unpatched Software | unpatched, outdated, cve, vulnerability |
| Supply Chain Attack | supply chain, third party, vendor |
| Zero-Day Exploit | zero-day, zero day, unknown vulnerability |
| DDoS Attack | ddos, denial of service, overwhelm |
| Insider Threat | insider, employee, internal |
| Physical Security | physical, stolen, device |
| API Vulnerability | api, endpoint, interface |
| Cross-Site Scripting (XSS) | xss, cross-site scripting |
| Business Logic Flaw | business logic, application logic |
| Cryptographic Failure | encryption, cryptographic, cipher |

The category with the most keyword matches wins. Exact category name matches in the response take precedence.

**Regulatory fine extraction:** Uses regex to find monetary amounts, identifies currency (USD/AUD/EUR), and extracts the regulatory authority (ACMA, OAIC, ASIC, FTC, GDPR).

**Severity extraction:** Maps keywords to severity levels: critical/severe/catastrophic → Critical; high/significant/major → High; medium/moderate/modest → Medium; low/minor/minimal → Low.

**Records affected extraction:** Matches patterns like "{number} records/accounts/customers/users" and applies unit multipliers (thousand, million, billion). The extracted value is then passed through `validate_and_correct_enrichment_data()` for plausibility checking.

---

## 7. Records Affected Validation

**Source file:** `cyber_data_collector/utils/validation.py`

### 7.1 Rule-Based Validation

The `validate_records_affected()` function applies tiered plausibility checks based on organization size:

| Rule | Value | Action |
|------|-------|--------|
| Negative values | < 0 | Reject (return null) |
| Zero values | 0 | Reject (return null) |
| Suspiciously low | < 50 | Reject (likely missed unit multiplier) |
| Small/unknown org | > 20,000,000 | Reject (exceeds small org cap) |
| Major Australian org | > 30,000,000 | Reject (exceeds Australia's ~26M population) |
| Major international org | > 1,000,000,000 | Reject (exceeds global plausibility) |

**Organization size tiers:**

| Tier | Max Records | Examples |
|------|-------------|---------|
| Major International | 1,000,000,000 | Facebook, Google, Microsoft, Amazon, Apple, Yahoo, Alibaba, Uber, PayPal, Visa, Mastercard, Equifax, Marriott, Adobe, eBay, Spotify, Netflix, Samsung, Canva, Ticketmaster, Internet Archive |
| Major Australian | 30,000,000 | Commonwealth Bank, Westpac, NAB, ANZ, Telstra, Optus, Medibank, Woolworths, Coles, Qantas, Australia Post, Latitude Financial |
| Australian Government | 30,000,000 | Department of Home Affairs, ATO, Medicare, Centrelink, Services Australia |
| Small/Unknown | 20,000,000 | All others |

### 7.2 LLM Fallback Validation

When rule-based validation rejects a value AND a Perplexity API key is available, `llm_validate_records_affected()` makes a targeted Perplexity call to determine whether the rejection was a false positive.

**LLM validation prompt:**

```
You are verifying a data point from a cyber security breach report.

Event: "{event_title}"
Organization: "{org_name}"
Claimed records affected: {value:,}
Context: "{description[:500]}"

Questions:
1. Is this event genuinely about a CYBER SECURITY incident (data breach,
   ransomware, hacking, phishing, etc.)? Or is it about physical security,
   airline safety, border security, workplace safety, or some other non-cyber
   topic? The word 'security' alone does NOT make it a cyber incident.
2. Is "{org_name}" a major organization? Estimate their customer/user base size.
3. Is {value:,} records affected plausible for this organization and this
   incident?
4. Could this number be a parsing error (e.g. '20' when the article said
   '20,000' or '20 million')?

Respond with JSON only:
{
  "is_cyber_incident": true or false,
  "is_plausible": true or false,
  "corrected_value": null or integer,
  "org_size_category": "major_international" | "major_australian" |
                       "small_regional" | "unknown",
  "estimated_customer_base": integer or null,
  "reasoning": "brief explanation"
}
```

**Decision logic:**

1. If `is_cyber_incident == false`: reject the entire event (not just the record count). This catches events about physical security, airline safety, etc. that were incorrectly classified as cyber events.
2. If `corrected_value` is provided (e.g., "20" was actually "20,000"): the corrected value is re-validated through rule-based checks before acceptance.
3. If `is_plausible == true`: accept the original value, overriding the rule-based rejection.
4. If `is_plausible == false`: confirm the rule-based rejection (return null).

### 7.3 Dynamic Organization List Updates

When Perplexity confirms an organization's size category, the module-level organization sets are dynamically updated:

- If `org_size_category == "major_australian"`: add to `MAJOR_AUSTRALIAN_ORGANIZATIONS`
- If `org_size_category == "major_international"`: add to `MAJOR_INTERNATIONAL_ORGANIZATIONS`

This means that subsequent events mentioning the same organization will pass rule-based validation without requiring another Perplexity API call, reducing costs over time.

---

## 8. Token Usage Tracking

**Source file:** `cyber_data_collector/utils/token_tracker.py`

All LLM API calls across the enrichment pipeline are tracked by a thread-safe singleton tracker that records model, token counts, and context.

**Pricing model used for cost estimation (per 1 million tokens, USD):**

| Model | Input | Output |
|-------|-------|--------|
| `gpt-4o` | $2.50 | $10.00 |
| `gpt-4o-mini` | $0.15 | $0.60 |
| `sonar-pro` (Perplexity) | $3.00 | $15.00 |

**Tracking contexts:**

| Context | Module |
|---------|--------|
| `gpt4o_enrichment` | GPT-4o primary extraction (Stage 2) |
| `perplexity_fact_check` | Perplexity fact-checking (Stage 3) |
| `perplexity_enrichment` | Perplexity backfill enrichment (Pathway 2) |
| `records_affected_validation` | LLM records count validation |

---

## 9. Configuration and Thresholds Summary

| Parameter | Value | Location |
|-----------|-------|----------|
| GPT-4o-mini temperature | 0.0 | `llm_extractor.py` |
| GPT-4o-mini content truncation | 12,000 chars | `llm_extractor.py` |
| GPT-4o temperature | 0.1 | `gpt4o_enricher.py` |
| GPT-4o content truncation | 8,000 chars | `gpt4o_enricher.py` |
| Perplexity temperature | 0.1 | `perplexity_fact_checker.py`, `perplexity_enrichment.py` |
| Perplexity max tokens | 1,000 | `perplexity_fact_checker.py`, `perplexity_enrichment.py` |
| Perplexity rate limit | 2.0 seconds | `perplexity_enrichment.py` |
| Perplexity max retries | 3 | `perplexity_fact_checker.py`, `perplexity_enrichment.py` |
| Field update confidence threshold | 0.6 | `perplexity_backfill_events.py` |
| Records affected minimum | 50 | `validation.py` |
| Records affected maximum | 1,000,000,000 | `validation.py` |
| Small org records cap | 20,000,000 | `validation.py` |
| Major AU org records cap | 30,000,000 | `validation.py` |
| Fact-check records tolerance | ±20% | `perplexity_fact_checker.py` |
| Auto-accept confidence | >= 0.8 | `high_quality_enrichment_pipeline.py` |
| Accept-with-warning confidence | >= 0.5 | `high_quality_enrichment_pipeline.py` |
| Reject confidence | < 0.5 | `high_quality_enrichment_pipeline.py` |
| Validation error penalty | -0.3 per error | `enrichment_validator.py` |
| Validation warning penalty | -0.1 per warning | `enrichment_validator.py` |
| Stage weights (extraction) | 30% | `high_quality_enrichment_pipeline.py` |
| Stage weights (fact-check) | 30% | `high_quality_enrichment_pipeline.py` |
| Stage weights (validation) | 20% | `high_quality_enrichment_pipeline.py` |
| Stage weights (source reliability) | 20% | `high_quality_enrichment_pipeline.py` |
| Detail enrichment cooldown | 30 days | `perplexity_enricher.py` |

---

## 10. Error Handling and Resilience

### LLM API Failures

- All Perplexity calls use retry logic with exponential backoff (3 retries, delays of 1-4 seconds for fact-checker, 2-60 seconds for enrichment engine).
- Authentication errors (HTTP 401/403) are not retried and propagate immediately.
- JSON parsing failures trigger retries (the LLM may return valid JSON on retry).
- If all retries fail, the check/enrichment returns a null/error result rather than crashing the pipeline.

### GPT-4o Failures

- JSON parsing errors return a structured error result with zero confidence, allowing the pipeline to continue with degraded quality rather than failing.
- The `response_format: json_object` parameter ensures GPT-4o produces valid JSON in nearly all cases.

### Content Acquisition Failures

- If all four extraction methods fail (newspaper3k, trafilatura, BeautifulSoup, Playwright), the pipeline returns an error result with `extraction_success: False`.
- The pipeline short-circuits: Stage 2-5 are skipped and the event receives a REJECT decision.

### Database Errors

- The audit trail storage uses explicit transaction management with `conn.commit()` and `conn.rollback()`.
- Duplicate detection queries use try/except to gracefully handle database lock or connection errors.
- The backfill processor uses the database's `_lock` for thread safety.

### Graceful Degradation

- If Perplexity API key is not configured, the pipeline skips fact-checking (Stage 3) and uses default confidence values.
- If `newspaper3k` or `trafilatura` are not installed, their extraction methods are skipped.
- If Playwright is not installed, JavaScript-heavy sites fall back to BeautifulSoup's basic parsing.
- The `llm_validate_records_affected` function works without a Perplexity key (pure rule-based validation only).

---

## 11. Justification of Design Decisions

### Why Use Multiple LLMs?

The pipeline uses three different LLM services (GPT-4o-mini, GPT-4o, Perplexity sonar-pro) because they serve complementary purposes:

1. **GPT-4o-mini** provides low-cost, high-throughput initial classification. At ~$0.001 per event, it can process thousands of candidates economically. Its accuracy is sufficient for binary yes/no decisions (is this Australian? is this specific?) but insufficient for precise entity extraction.

2. **GPT-4o** provides high-accuracy structured extraction. Its larger model size enables better reasoning about complex cases (aggregate articles, parent-vs-subsidiary organizations, multi-victim scenarios). The ~$0.01 cost per event is justified by the much higher extraction quality.

3. **Perplexity sonar-pro** provides independent verification through real-time web search. Unlike GPT-4o which reasons only from the article text, Perplexity can access current web sources to verify claims. This cross-validation catches extraction errors and hallucinations.

### Why Not Use a Single LLM?

A single LLM approach would be simpler but has critical weaknesses:
- A single LLM cannot independently verify its own extractions against external sources.
- Using GPT-4o for all events would be cost-prohibitive at discovery scale.
- Using only GPT-4o-mini would sacrifice extraction quality for critical metadata fields.

### Why Rule-Based Validation After LLM Extraction?

LLMs systematically make certain types of errors that simple rules can catch:
- Extracting "6" instead of "6,000,000" for records affected (missed unit multiplier)
- Extracting generic descriptions as organization names
- Classifying educational articles as specific incidents
- Producing future dates or logically impossible date orderings

Rule-based validation is deterministic, fast, and catches these patterns reliably without additional API costs.

### Why the Specificity Override System?

GPT-4o's specificity classification (is this a specific incident?) is the single most impactful determination in the pipeline. A false negative (marking a real incident as non-specific) causes the event to be penalized or rejected, losing real data. The override system uses high-confidence heuristics (named victim + concrete details + Australian relevance) to catch and correct these false negatives.

The system was designed after observing that GPT-4o systematically under-classifies events as specific when:
- The article is a retrospective or analysis of a known incident
- The article is from an aggregate blog but has a specific title
- The article describes an incident with limited public details

### Why 30/30/20/20 Weighting?

The confidence aggregation weights were chosen based on:
- **Extraction (30%) and Fact-checking (30%)** are equally weighted because they provide independent, complementary signals. Neither is sufficient alone.
- **Validation (20%)** is a secondary corrective signal -- it catches errors but doesn't contribute positive evidence.
- **Source reliability (20%)** provides a prior probability -- articles from trusted sources are more likely to contain accurate, specific incident information.

### Why Accept Events Below 0.8 Confidence?

The 0.5-0.8 "accept with warning" band exists because:
- Many real Australian cyber incidents receive limited media coverage, making fact-checking difficult (Perplexity may not find corroborating sources).
- New/emerging incidents may not yet have multiple sources.
- Rejecting all events below 0.8 would create significant gaps in coverage.
- The "warning" flag enables later manual review of borderline cases.

---

## 12. Replication Guide

To independently replicate the enrichment processes described in this document:

### Prerequisites

- Python 3.8+
- OpenAI API key (for GPT-4o and GPT-4o-mini)
- Perplexity API key (for fact-checking and enrichment)
- Python packages: `openai`, `pydantic`, `instructor`, `requests`, `beautifulsoup4`, `newspaper3k` (optional), `trafilatura` (optional), `playwright` (optional), `pdfplumber` (optional)

### Step 1: Initial Extraction (Pathway 1)

1. For each discovered event URL, fetch the full article content using the extraction cascade (Section 5.2).
2. Truncate the content to 12,000 characters.
3. Send to GPT-4o-mini with the system prompt and user prompt from Section 3.3.
4. Parse the response into the `ExtractedEventDetails` schema (Section 3.4).
5. Validate `records_affected` using the rules in Section 7.1.
6. Store the result with a confidence score.

### Step 2: Perplexity Backfill (Pathway 2)

1. Query the database for events where `perplexity_validated IS NULL AND is_specific_event = TRUE`, prioritized by placeholder dates, recency, and low confidence.
2. For each event, construct the enrichment prompt from Section 4.4.
3. Send to Perplexity sonar-pro with temperature 0.1 and max_tokens 1000.
4. Parse the JSON response into the `PerplexityEventEnrichment` schema (Section 4.5).
5. Apply field updates only when per-field confidence >= 0.6 (Section 4.6).
6. Mark the event as `perplexity_validated = TRUE`.
7. Wait at least 2 seconds between API calls.

### Step 3: High-Quality Re-Enrichment (Pathway 3)

1. **Content Acquisition:** Fetch full article text using the cascade in Section 5.2. Assign source reliability from the lookup table in Section 5.2.4.
2. **GPT-4o Extraction:** Truncate content to 8,000 characters. Construct the 6-section prompt from Section 5.3.3-5.3.9. Send to GPT-4o with temperature 0.1 and `response_format: json_object`. Parse the JSON response. Apply `llm_validate_records_affected` to the records count.
3. **Perplexity Fact-Checking:** Run up to 4 verification checks (Section 5.4.3) based on available extracted data. Calculate verification confidence using the 40/40/10/10 weights (Section 5.4.4).
4. **Validation:** Apply all 6 validation checks from Section 5.5.2. Apply specificity overrides where triggered. Calculate validation confidence (Section 5.5.3).
5. **Decision:** Compute the weighted confidence (Section 5.6.1), apply penalties (Section 5.6.2), and classify as AUTO_ACCEPT / ACCEPT_WITH_WARNING / REJECT using the thresholds in Section 5.6.3.
6. Store the complete audit trail (Section 5.7).

### Step 4: Missing Detail Fill-In (Supplementary)

1. Query `DeduplicatedEvents` for events with missing fields (Section 6.2).
2. Construct targeted Perplexity queries per missing field (Section 6.3).
3. Parse responses using regex extractors (Section 6.4).
4. Validate all extracted data before database insertion.

### Key Implementation Notes

- All Perplexity calls use the `sonar-pro` model via the OpenAI-compatible API at `https://api.perplexity.ai`.
- JSON responses from Perplexity may be wrapped in markdown code fences (` ```json ... ``` `) -- strip these before parsing.
- The GPT-4o `response_format: json_object` parameter eliminates most JSON parsing issues but is not available for GPT-4o-mini via the instructor library.
- Records affected validation should be applied at every entry point (initial extraction, Perplexity enrichment, detail fill-in) to prevent implausible values from entering the database.
- Rate limiting is critical for Perplexity API stability: enforce a minimum of 2 seconds between requests.
