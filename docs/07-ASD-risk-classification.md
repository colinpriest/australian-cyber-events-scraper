# ASD Risk Classification Stage

This document provides a detailed specification of the ASD (Australian Signals Directorate) risk classification stage of the Australian Cyber Events pipeline. It describes the classification framework, LLM prompts, validation logic, output formats, and design rationale in sufficient detail for an independent reviewer to assess suitability and to replicate the process using only this documentation.

---

## Table of Contents

1. [Purpose and Scope](#1-purpose-and-scope)
2. [ASD Risk Matrix Framework](#2-asd-risk-matrix-framework)
   - 2.1 [Severity Categories (C1–C6)](#21-severity-categories-c1c6)
   - 2.2 [Stakeholder Categories](#22-stakeholder-categories)
   - 2.3 [Impact Types](#23-impact-types)
   - 2.4 [Two-Dimensional Risk Matrix Structure](#24-two-dimensional-risk-matrix-structure)
3. [Classification Process](#3-classification-process)
   - 3.1 [Input Data](#31-input-data)
   - 3.2 [LLM Prompt Construction](#32-llm-prompt-construction)
   - 3.3 [Full System Prompt](#33-full-system-prompt)
   - 3.4 [Full User Prompt Template](#34-full-user-prompt-template)
   - 3.5 [Structured Output Schema](#35-structured-output-schema)
4. [Validation and Quality Controls](#4-validation-and-quality-controls)
   - 4.1 [Type-Safe Output Enforcement](#41-type-safe-output-enforcement)
   - 4.2 [Post-Classification Validation](#42-post-classification-validation)
   - 4.3 [Default Behaviour for Insufficient Information](#43-default-behaviour-for-insufficient-information)
5. [Processing Architecture](#5-processing-architecture)
   - 5.1 [Incremental Processing](#51-incremental-processing)
   - 5.2 [Concurrency Model](#52-concurrency-model)
   - 5.3 [Caching Strategy](#53-caching-strategy)
   - 5.4 [Retry Logic](#54-retry-logic)
6. [Pipeline Integration](#6-pipeline-integration)
   - 6.1 [Where Classification Fits in the Pipeline](#61-where-classification-fits-in-the-pipeline)
   - 6.2 [Triggering Classification](#62-triggering-classification)
   - 6.3 [Standalone Execution](#63-standalone-execution)
7. [Database Storage](#7-database-storage)
   - 7.1 [ASDRiskClassifications Table Schema](#71-asdriskclassifications-table-schema)
   - 7.2 [Reasoning JSON Structure](#72-reasoning-json-structure)
8. [Output Formats](#8-output-formats)
   - 8.1 [Risk Matrix Excel Files](#81-risk-matrix-excel-files)
   - 8.2 [CSV and JSON Exports](#82-csv-and-json-exports)
   - 8.3 [Dashboard Integration](#83-dashboard-integration)
9. [Justification of Design Decisions](#9-justification-of-design-decisions)
10. [Limitations and Caveats](#10-limitations-and-caveats)
11. [Replication Guide](#11-replication-guide)

---

## 1. Purpose and Scope

The ASD risk classification stage assigns each deduplicated cyber event a structured risk assessment based on the Australian Signals Directorate's cyber incident categorisation framework. This is Phase 4 of the five-phase pipeline, running after global deduplication (Phase 3) and before dashboard generation (Phase 5).

For each event, the classifier determines three outputs:

1. **Severity category** (C1–C6) — how severe the incident is.
2. **Primary stakeholder category** — which type of entity was most affected.
3. **Impact type** — what form the impact took.

These three dimensions correspond to the ASD's published risk matrix, which plots impact type (rows) against stakeholder category (columns) to produce a grid of incident counts. The severity category C1–C6 is a supplementary classification that summarises overall severity into a single ordinal value.

### What This Stage Does

- Reads deduplicated events from the `DeduplicatedEvents` table.
- Sends each event's metadata to GPT-4o for classification.
- Validates the response against a strict schema.
- Stores the classification and reasoning in the `ASDRiskClassifications` table.
- Compiles aggregated risk matrices as formatted Excel workbooks.

### What This Stage Does NOT Do

- It does not discover, scrape, enrich, or deduplicate events (Phases 1–3).
- It does not generate the HTML dashboard (Phase 5), although the dashboard consumes classification data.
- It does not modify the underlying event data — classification is an additive annotation.

---

## 2. ASD Risk Matrix Framework

### 2.1 Severity Categories (C1–C6)

The severity scale runs from C1 (most severe) to C6 (least severe). The definitions used in the LLM prompt are:

| Category | Label | Description | Records Affected Guidance |
|----------|-------|-------------|---------------------------|
| **C1** | Most severe | Sustained disruption of critical national infrastructure, extensive data breaches affecting millions, national security implications | > 1,000,000 records |
| **C2** | Very severe | Major disruption to essential services, large-scale data breaches (hundreds of thousands to millions), significant financial impact | 100,000–1,000,000 records |
| **C3** | Severe | Substantial impact on organisations, moderate data breaches (tens of thousands to hundreds of thousands), notable service disruption | 10,000–100,000 records |
| **C4** | Moderate | Limited impact on organisations, smaller data breaches (thousands to tens of thousands), minor service disruption | 1,000–10,000 records |
| **C5** | Low | Minimal impact, small data breaches (hundreds to thousands), isolated incidents | < 1,000 records |
| **C6** | Least severe | Unsuccessful attacks, very small or no data breaches, minimal to no impact | Unknown / no data |

The records-affected guidance is explicitly provided to the LLM as a heuristic. When the number of records affected is unknown, the default classification is C6 (least severe). This conservative default avoids over-stating the severity of poorly-documented incidents.

### 2.2 Stakeholder Categories

There are 15 stakeholder categories, ordered from lowest to highest criticality. These are the exact string values used in the classification and stored in the database:

| # | Category |
|---|----------|
| 1 | Member(s) of the public |
| 2 | Small organisation(s) |
| 3 | Sole traders |
| 4 | Medium-sized organisation(s) |
| 5 | Schools |
| 6 | Local government |
| 7 | State government |
| 8 | Academia/R&D |
| 9 | Large organisation(s) |
| 10 | Supply chain |
| 11 | Federal government |
| 12 | Government shared services |
| 13 | Regulated critical infrastructure |
| 14 | National security |
| 15 | Systems of National Significance |

For risk matrix display purposes, these 15 categories are grouped into 6 columns:

| Group Label (Column Header) | Member Categories |
|-----------------------------|-------------------|
| Member(s) of the public | Member(s) of the public |
| Small organisation(s) / Sole traders | Small organisation(s), Sole traders |
| Medium-sized organisation(s) / Schools / Local government | Medium-sized organisation(s), Schools, Local government |
| State government / Academia/R&D / Large organisation(s) / Supply chain | State government, Academia/R&D, Large organisation(s), Supply chain |
| Federal government / Government shared services / Regulated critical infrastructure | Federal government, Government shared services, Regulated critical infrastructure |
| National security / Systems of National Significance | National security, Systems of National Significance |

This grouping is used when compiling the risk matrix into Excel and the HTML dashboard. Counts for all member categories within a group are summed into a single cell.

### 2.3 Impact Types

There are 6 impact types, ordered from most severe to least severe. These form the rows of the risk matrix:

| # | Impact Type |
|---|-------------|
| 1 | Sustained disruption of essential systems and associated services |
| 2 | Extensive compromise |
| 3 | Isolated compromise |
| 4 | Coordinated low-level malicious attack |
| 5 | Low-level malicious attack |
| 6 | Unsuccessful low-level malicious attack |

### 2.4 Two-Dimensional Risk Matrix Structure

The final risk matrix is a 6 × 6 grid:

- **Rows:** The 6 impact types (most severe at top).
- **Columns:** The 6 grouped stakeholder categories (lowest criticality at left, highest at right).
- **Cell values:** Count of classified events that fall into that (impact type, stakeholder group) combination.

This matrix is generated for three views: all years combined, the current year, and the previous year.

---

## 3. Classification Process

### 3.1 Input Data

Each event submitted for classification is a deduplicated event from the `DeduplicatedEvents` table with `status = 'Active'`. The following fields are provided to the LLM:

| Field | Source Column | Description |
|-------|---------------|-------------|
| Title | `title` | Event headline |
| Event Type | `event_type` | CyberEventType enum value (e.g., RANSOMWARE, DATA_BREACH) |
| Current Severity | `severity` | Pre-existing severity from enrichment (Critical/High/Medium/Low) |
| Event Date | `event_date` | Date of the incident |
| Records Affected | `records_affected` | Number of records/individuals affected (may be null) |
| Victim Organization | `victim_organization_name` | Name of the affected organisation |
| Victim Industry | `victim_organization_industry` | Industry sector of the victim |
| Attacking Entity | `attacking_entity_name` | Name of the threat actor (if known) |
| Attack Method | `attack_method` | Method of attack (if known) |
| Australian Event | `is_australian_event` | Boolean flag for Australian relevance |
| Australian Relevance Score | `australian_relevance_score` | Numeric relevance score |
| Description | `description` | Event description text |
| Summary | `summary` | Event summary text |

### 3.2 LLM Prompt Construction

The classification uses a two-message conversation with GPT-4o:

1. **System message** — establishes the LLM's role and constraints.
2. **User message** — contains the event data, framework definitions, and instructions.

The LLM model used is `gpt-4o` with a temperature of `0.3`. The low temperature promotes consistency and determinism in classifications — the same event should receive the same classification across runs.

### 3.3 Full System Prompt

The system prompt is a single string sent as the `system` role message:

```
You are an expert cybersecurity analyst specializing in ASD risk classification. You must classify cyber incidents according to the Australian Signals Directorate risk matrix framework using ONLY the valid categories provided.
```

This prompt constrains the LLM to use only the categories defined in the user message, preventing hallucination of invalid categories.

### 3.4 Full User Prompt Template

The user prompt is constructed dynamically for each event. Below is the complete template with placeholders shown in `{braces}`:

```
You are an expert cybersecurity analyst classifying cyber incidents according to the Australian Signals Directorate (ASD) risk matrix framework.

Analyze the following cyber event and provide a classification:

Event Details:
- Title: {title}
- Event Type: {event_type}
- Current Severity: {severity}
- Event Date: {event_date}
- Records Affected: {records_affected (formatted with commas, or "Unknown")}
- Victim Organization: {victim_organization_name}
- Victim Industry: {victim_organization_industry}
- Attacking Entity: {attacking_entity_name}
- Attack Method: {attack_method}
- Australian Event: {is_australian_event}
- Australian Relevance Score: {australian_relevance_score}

Description: {description}

Summary: {summary}

ASD Severity Categories (C1 = most severe, C6 = least severe):
- C1: Most severe - Sustained disruption of critical national infrastructure, extensive data breaches affecting millions, national security implications
- C2: Very severe - Major disruption to essential services, large-scale data breaches (hundreds of thousands to millions), significant financial impact
- C3: Severe - Substantial impact on organizations, moderate data breaches (tens of thousands to hundreds of thousands), notable service disruption
- C4: Moderate - Limited impact on organizations, smaller data breaches (thousands to tens of thousands), minor service disruption
- C5: Low - Minimal impact, small data breaches (hundreds to thousands), isolated incidents
- C6: Least severe - Unsuccessful attacks, very small or no data breaches, minimal to no impact

IMPORTANT: Use the "Records Affected" field to inform severity:
- >1,000,000 records → Consider C1-C2
- 100,000-1,000,000 records → Consider C2-C3
- 10,000-100,000 records → Consider C3-C4
- 1,000-10,000 records → Consider C4-C5
- <1,000 records → Consider C5-C6
- Unknown/No data → Default to C6 (least severe)

Stakeholder Categories (select ONE primary category):
- Member(s) of the public
- Small organisation(s)
- Sole traders
- Medium-sized organisation(s)
- Schools
- Local government
- State government
- Academia/R&D
- Large organisation(s)
- Supply chain
- Federal government
- Government shared services
- Regulated critical infrastructure
- National security
- Systems of National Significance

Impact Types:
- Sustained disruption of essential systems and associated services
- Extensive compromise
- Isolated compromise
- Coordinated low-level malicious attack
- Low-level malicious attack
- Unsuccessful low-level malicious attack

Instructions:
1. Analyze the event details, especially the number of records affected
2. Assign a severity category (C1-C6) based on impact and records affected
3. Select ONE primary stakeholder category that best represents the victim
4. Classify the impact type
5. Provide detailed reasoning for each classification
6. If there is insufficient information, default to C6 (least severe) and explain why

Return your response as a JSON object with this exact structure:
{
    "severity_category": "C3",
    "primary_stakeholder_category": "Medium-sized organisation(s)",
    "impact_type": "Isolated compromise",
    "reasoning": {
        "severity_reasoning": "Detailed explanation of why this severity was chosen, including consideration of records affected",
        "stakeholder_reasoning": "Explanation of why this stakeholder category was selected",
        "impact_reasoning": "Explanation of the impact type classification",
        "information_quality": "Assessment of available information quality and any limitations"
    },
    "confidence": 0.85
}
```

Key design aspects of the prompt:

- **Records-affected heuristic:** The prompt provides explicit numeric thresholds for mapping record counts to severity levels. This ensures consistent treatment of the most objective data point available.
- **Conservative default:** The instruction to default to C6 when records affected is unknown prevents inflation of severity scores for poorly-documented events.
- **Mandatory reasoning:** Four separate reasoning fields force the LLM to justify each dimension of its classification, providing an audit trail.
- **Closed enumeration:** All valid categories are listed inline so the LLM cannot invent new ones.

### 3.5 Structured Output Schema

The API call uses OpenAI's structured outputs feature (`response_format=ASDRiskClassification`) to enforce type-safe responses. The Pydantic model used is:

```python
class ClassificationReasoning(BaseModel):
    severity_reasoning: str = Field(..., min_length=10)
    stakeholder_reasoning: str = Field(..., min_length=10)
    impact_reasoning: str = Field(..., min_length=10)
    information_quality: str = Field(..., min_length=10)

class ASDRiskClassification(BaseModel):
    severity_category: Literal["C1", "C2", "C3", "C4", "C5", "C6"]
    primary_stakeholder_category: Literal[
        "Member(s) of the public",
        "Small organisation(s)",
        "Sole traders",
        "Medium-sized organisation(s)",
        "Schools",
        "Local government",
        "State government",
        "Academia/R&D",
        "Large organisation(s)",
        "Supply chain",
        "Federal government",
        "Government shared services",
        "Regulated critical infrastructure",
        "National security",
        "Systems of National Significance"
    ]
    impact_type: Literal[
        "Sustained disruption of essential systems and associated services",
        "Extensive compromise",
        "Isolated compromise",
        "Coordinated low-level malicious attack",
        "Low-level malicious attack",
        "Unsuccessful low-level malicious attack"
    ]
    reasoning: ClassificationReasoning
    confidence: float = Field(..., ge=0.0, le=1.0)
```

The use of `Literal` types ensures that the API response can only contain exact matches from the predefined enumerations. If the LLM returns an invalid value, the structured output parser rejects it and the request is retried.

---

## 4. Validation and Quality Controls

### 4.1 Type-Safe Output Enforcement

The primary validation mechanism is OpenAI's structured outputs API (`client.beta.chat.completions.parse()`). This provides schema-level enforcement at the API layer:

- `severity_category` must be one of exactly 6 values: C1, C2, C3, C4, C5, C6.
- `primary_stakeholder_category` must be one of exactly 15 predefined strings.
- `impact_type` must be one of exactly 6 predefined strings.
- `confidence` must be a float between 0.0 and 1.0 (enforced by `ge=0.0, le=1.0` constraints).
- All four reasoning fields must be strings with a minimum length of 10 characters.

If the API returns a response that does not conform to this schema, `response.choices[0].message.parsed` returns `None` and the request is retried.

### 4.2 Post-Classification Validation

After parsing, a secondary validation function checks:

1. All 5 required top-level fields are present: `severity_category`, `primary_stakeholder_category`, `impact_type`, `reasoning`, `confidence`.
2. `severity_category` is one of C1–C6.
3. `primary_stakeholder_category` matches the valid stakeholder list.
4. `impact_type` matches the valid impact type list.
5. `confidence` is a number between 0.0 and 1.0.
6. `reasoning` is a dictionary containing all 4 required sub-fields: `severity_reasoning`, `stakeholder_reasoning`, `impact_reasoning`, `information_quality`.

This defence-in-depth approach ensures robustness even if the structured output API behaviour changes.

### 4.3 Default Behaviour for Insufficient Information

When event data is sparse (e.g., missing description, unknown records affected, no identified victim):

- The prompt explicitly instructs the LLM to default to **C6** (least severe).
- The LLM must explain why in the `information_quality` reasoning field.
- The `confidence` score will typically be lower (0.5–0.7) for poorly-documented events.

This conservative approach ensures that data gaps do not lead to inflated severity assessments.

---

## 5. Processing Architecture

### 5.1 Incremental Processing

The classifier is incremental — it only processes events that do not already have a classification:

1. Query `DeduplicatedEvents` with a `LEFT JOIN` to `ASDRiskClassifications`.
2. Order results so unclassified events (`has_classification = 0`) come first.
3. For each event, check the database cache before making an API call.
4. If a classification already exists and `force_reclassify` is `False`, skip the event.

This means re-running the classification phase only processes newly added events since the last run.

### 5.2 Concurrency Model

Classification uses a `ThreadPoolExecutor` with 10 worker threads, rate-limited by a semaphore:

- **Thread pool:** `max_workers=10` — up to 10 events are classified in parallel.
- **Semaphore:** `threading.Semaphore(10)` — limits concurrent API calls to 10.
- **Database lock:** `threading.Lock()` — all database reads and writes are serialised through a single lock to prevent SQLite concurrency issues.
- **Stats lock:** `threading.Lock()` — separate lock for updating API call counters.

Progress is displayed via a `tqdm` progress bar showing events processed, API calls made, and cache hits.

### 5.3 Caching Strategy

There are two layers of caching:

1. **Database cache:** Before calling the API, the classifier checks if a classification already exists in the `ASDRiskClassifications` table for the given `deduplicated_event_id`. If found, the cached result is returned immediately.

2. **Prompt-level cache:** A SHA-256 hash is computed from `model + temperature + prompt_text`. If an identical prompt has already been sent during the current session, the cached result is returned without an API call. This handles edge cases where multiple events might produce identical prompts (e.g., after deduplication changes).

### 5.4 Retry Logic

Each API call has retry logic with exponential backoff:

- **Maximum retries:** 3 attempts.
- **Backoff:** `2^attempt` seconds (1s, 2s, 4s).
- **Retry conditions:** Any exception from the API call, or a `None` parsed result (schema validation failure).
- **Failure handling:** After 3 failed attempts, the event is skipped and logged as a warning. The pipeline continues processing remaining events.

---

## 6. Pipeline Integration

### 6.1 Where Classification Fits in the Pipeline

```
Phase 1: Discovery & Initial Processing
    ↓
Phase 2: Perplexity AI Enrichment
    ↓
Phase 3: Global Deduplication
    ↓
Phase 4: ASD Risk Classification  ← THIS STAGE
    ↓
Phase 5: Dashboard Generation
```

Classification operates on `DeduplicatedEvents` — the final, merged representation of each unique cyber incident. This ensures each real-world incident receives exactly one classification, regardless of how many source articles originally reported it.

### 6.2 Triggering Classification

Classification is triggered in three ways:

**1. Full pipeline run (default):**
```bash
python run_full_pipeline.py
```
Runs all 5 phases. Classification runs after deduplication completes.

**2. Classification-only mode:**
```bash
python run_full_pipeline.py --classify-only
python run_full_pipeline.py --classify-only --classify-limit 50
```
Runs only the classification phase. Useful for classifying newly discovered events without re-running discovery or deduplication.

**3. Simplified CLI:**
```bash
python pipeline.py refresh
```
Runs the full pipeline including classification. Use `--skip-classification` to omit this phase for faster execution.

The classification phase is skipped when:
- `--skip-classification` is specified.
- `--discover-only` or `--dashboard-only` is specified.
- The `OPENAI_API_KEY` environment variable is not set (logged as a warning, not an error).
- All active events already have classifications.

### 6.3 Standalone Execution

The classifier can also be run as a standalone script:

```bash
python scripts/asd_risk_classifier.py --limit 100
python scripts/asd_risk_classifier.py --force-reclassify --output-dir risk_matrix
python scripts/asd_risk_classifier.py --model gpt-4o --limit 500
```

| Option | Default | Description |
|--------|---------|-------------|
| `--db-path` | `instance/cyber_events.db` | Path to the SQLite database |
| `--model` | `gpt-4o` | OpenAI model to use |
| `--limit` | `5` | Maximum number of events to process |
| `--force-reclassify` | `False` | Re-classify events even if cached |
| `--output-dir` | `risk_matrix` | Directory for export files |

---

## 7. Database Storage

### 7.1 ASDRiskClassifications Table Schema

```sql
CREATE TABLE ASDRiskClassifications (
    classification_id TEXT PRIMARY KEY,
    deduplicated_event_id TEXT UNIQUE NOT NULL,
    severity_category VARCHAR(2) NOT NULL
        CHECK(severity_category IN ('C1', 'C2', 'C3', 'C4', 'C5', 'C6')),
    primary_stakeholder_category VARCHAR(255) NOT NULL,
    impact_type VARCHAR(100) NOT NULL,
    reasoning_json TEXT NOT NULL,
    confidence_score REAL NOT NULL
        CHECK(confidence_score >= 0.0 AND confidence_score <= 1.0),
    model_used VARCHAR(50) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (deduplicated_event_id)
        REFERENCES DeduplicatedEvents(deduplicated_event_id) ON DELETE CASCADE
);
```

Key constraints:

- **One-to-one relationship:** `deduplicated_event_id` has a `UNIQUE` constraint, ensuring exactly one classification per deduplicated event.
- **Severity CHECK:** The database enforces that `severity_category` is one of C1–C6 at the storage layer.
- **Confidence CHECK:** The database enforces that `confidence_score` is between 0.0 and 1.0.
- **CASCADE delete:** If a deduplicated event is removed (e.g., during a re-deduplication), its classification is automatically deleted.
- **INSERT OR REPLACE:** The save operation uses `INSERT OR REPLACE`, so re-classification of an event overwrites the previous classification.

### 7.2 Reasoning JSON Structure

The `reasoning_json` column stores the LLM's justification as a JSON object:

```json
{
    "severity_reasoning": "The event affected 80,000 records, which falls into the 10,000-100,000 range, suggesting a C3-C4 severity. Given the involvement of the ATO, a federal entity, C3 is appropriate.",
    "stakeholder_reasoning": "The primary stakeholder affected is the Federal government, as the ATO is a federal entity.",
    "impact_reasoning": "The locking out of tens of thousands of accounts indicates an extensive compromise.",
    "information_quality": "The information is limited in detail, lacking specifics about the attack method. However, the number of affected records and the involvement of the ATO provide sufficient context."
}
```

Each field serves a specific audit purpose:

| Field | Purpose |
|-------|---------|
| `severity_reasoning` | Justifies the C1–C6 rating, with explicit reference to records affected |
| `stakeholder_reasoning` | Explains why the selected stakeholder category best represents the victim |
| `impact_reasoning` | Explains the impact type classification |
| `information_quality` | Assesses completeness of available data and flags any gaps that affected classification confidence |

---

## 8. Output Formats

### 8.1 Risk Matrix Excel Files

After classification, risk matrices are compiled into formatted Excel workbooks in the `risk_matrix/` directory:

| File | Contents |
|------|----------|
| `risk_matrix.xlsx` | All years combined |
| `risk_matrix_2020.xlsx` | Events from 2020 only |
| `risk_matrix_2021.xlsx` | Events from 2021 only |
| ... | One file per year through current year |
| `risk_matrix_{current_year}.xlsx` | Events from the current year |

Each workbook contains a single sheet named "Risk Matrix" with:

- **Rows:** 6 impact types (most severe at top).
- **Columns:** Column A = impact type label; Columns B–G = the 6 grouped stakeholder categories.
- **Cell values:** Integer count of events, or empty if zero.
- **Formatting:** Blue header row with white text, bold impact type labels, thin cell borders, centered numeric cells, frozen first row and column.

If no classifications exist for a given year, an empty matrix template is generated with the correct structure but no counts.

### 8.2 CSV and JSON Exports

When the classifier runs in standalone mode, it also produces timestamped CSV and JSON exports:

**CSV file** (`asd_risk_classifications_{timestamp}.csv`):
Contains one row per classified event with columns: `deduplicated_event_id`, `title`, `event_date`, `event_type`, `victim_organization_name`, `victim_organization_industry`, `records_affected`, `severity_category`, `primary_stakeholder_category`, `impact_type`, `confidence_score`, `model_used`, `created_at`.

**JSON file** (`asd_risk_classifications_{timestamp}.json`):
Same data as CSV but also includes the full `reasoning` object for each classification.

### 8.3 Dashboard Integration

The HTML dashboard (Phase 5) reads classification data by querying the `ASDRiskClassifications` table joined to `DeduplicatedEvents`. It generates three risk matrix views:

1. **All years** — aggregates all classifications.
2. **Previous year** — filters by the previous calendar year.
3. **Current year** — filters by the current calendar year.

Each matrix is rendered as an HTML table in the dashboard with heat-map-style colouring based on cell values.

---

## 9. Justification of Design Decisions

### Why the ASD Framework?

The Australian Signals Directorate's cyber incident categorisation framework is the de facto standard for assessing cyber incidents within the Australian government and critical infrastructure context. Using this framework:

- **Aligns with regulatory expectations:** Australian organisations report incidents using ASD categories. Using the same framework makes the pipeline's output directly comparable with official reporting.
- **Provides structured severity assessment:** The C1–C6 scale offers a clear ordinal ranking that is more nuanced than simple High/Medium/Low labels.
- **Incorporates stakeholder context:** The stakeholder dimension captures who was affected, not just how badly — a critical distinction for policy and response prioritisation.
- **Is well-defined and stable:** The framework has published definitions, reducing ambiguity in classification criteria.

### Why GPT-4o for Classification?

- **Nuanced judgement required:** Mapping free-text event descriptions to structured categories requires understanding context, implied severity, and organisational significance. This is a natural-language reasoning task well suited to LLMs.
- **Consistency at scale:** With structured outputs and a low temperature (0.3), GPT-4o produces consistent classifications across events. The same event classified multiple times will typically receive the same result.
- **Reasoning capture:** The LLM provides natural-language justifications for each classification, creating an audit trail that a human reviewer can assess.
- **GPT-4o over GPT-4o-mini:** The full GPT-4o model is used (rather than the mini variant used elsewhere in the pipeline for fast filtering) because classification requires deeper reasoning about severity, stakeholder identification, and impact assessment.

### Why a Conservative C6 Default?

When the records-affected field is null or the event description is sparse, the classifier defaults to C6 (least severe). This prevents:

- **Severity inflation:** Poorly-documented events would otherwise be classified based on speculative reasoning.
- **False urgency:** Over-classifying events with incomplete information could mislead downstream consumers of the risk matrix.
- **Bias towards recent events:** Newer events typically have more detail, so without a conservative default, historical events would be systematically under-classified relative to recent ones.

The confidence score is lowered for such events, signalling to reviewers that the classification has limited evidential support.

### Why Records Affected as a Primary Heuristic?

The prompt gives records-affected count special prominence because:

- **It is the most objective metric available.** Unlike event descriptions (which vary in tone and completeness), record counts are quantitative.
- **It directly measures impact scale.** The number of affected individuals or records is a reasonable proxy for the breadth of harm.
- **It anchors the LLM's judgement.** Without explicit thresholds, the LLM might over- or under-weight qualitative descriptions. The numeric thresholds provide a stable baseline.

However, the thresholds are advisory ("Consider C3-C4"), not mandatory — the LLM can override them when other factors (e.g., critical infrastructure involvement, national security implications) warrant a different rating.

### Why Structured Outputs?

Using OpenAI's structured outputs (`response_format=ASDRiskClassification`) rather than free-text JSON parsing:

- **Eliminates parsing failures.** The API guarantees the response conforms to the Pydantic schema.
- **Prevents invalid categories.** `Literal` types ensure only valid values are returned.
- **Reduces retry rates.** Schema validation happens at the API layer, not after receiving a response.

### Why One Classification per Deduplicated Event?

Classification operates on `DeduplicatedEvents` rather than `EnrichedEvents` or `RawEvents` because:

- **One incident = one classification.** Multiple news articles about the same breach should not produce multiple risk ratings.
- **Best available data.** Deduplicated events have the most complete metadata, having been enriched and merged from multiple sources.
- **Prevents double-counting.** The risk matrix counts would be inflated if each source article received its own classification.

---

## 10. Limitations and Caveats

### LLM Judgement Variability

While the low temperature (0.3) promotes consistency, LLM classifications are inherently probabilistic. The same event may occasionally receive different classifications across runs, particularly when:

- The event description is ambiguous.
- The records-affected count is near a threshold boundary (e.g., 9,500 records could be C4 or C5).
- Multiple stakeholder categories are equally applicable.

### Dependence on Upstream Data Quality

Classification quality is bounded by the quality of the enriched event data:

- Missing `records_affected` → defaults to C6.
- Missing `victim_organization_name` → stakeholder category may be less precise.
- Inaccurate `event_type` from earlier processing → may influence classification.

### No Human Review Loop

Classifications are generated entirely by the LLM without human review. The reasoning fields enable post-hoc auditing, but there is no built-in mechanism for human override or correction.

### Temporal Bias

Events from different years may have different levels of available detail. More recent events tend to have richer descriptions, potentially leading to higher severity classifications for recent events compared to historical ones with sparser documentation.

### Framework Interpretation

The ASD risk matrix framework was designed for real-time incident triage by human analysts. This pipeline applies it retrospectively to media-reported events, which may lack operational details (e.g., duration of disruption, remediation status) that would inform a real-time classification.

---

## 11. Replication Guide

To independently replicate the ASD risk classification process:

### Prerequisites

- OpenAI API key with access to GPT-4o.
- A SQLite database with populated `DeduplicatedEvents` table following the schema described in the data dictionary.
- Python 3.8+ with packages: `openai`, `pydantic`, `pandas`, `openpyxl`, `python-dotenv`, `tqdm`.

### Step-by-Step Process

1. **For each active deduplicated event**, construct the LLM prompt as described in [Section 3.4](#34-full-user-prompt-template), substituting the event's metadata into the template.

2. **Send the prompt to GPT-4o** using the OpenAI chat completions API:
   - Model: `gpt-4o`
   - Temperature: `0.3`
   - System message: as described in [Section 3.3](#33-full-system-prompt)
   - Response format: Structured output matching the Pydantic schema in [Section 3.5](#35-structured-output-schema)

3. **Validate the response:**
   - `severity_category` is one of C1–C6.
   - `primary_stakeholder_category` is one of the 15 valid categories.
   - `impact_type` is one of the 6 valid types.
   - `confidence` is between 0.0 and 1.0.
   - All 4 reasoning fields are present and non-trivial (≥ 10 characters).

4. **Store the classification** with a unique ID, the event ID, all classification fields, the reasoning as JSON, the model name, and a timestamp.

5. **Compile risk matrices** by aggregating counts:
   - Group `primary_stakeholder_category` values into the 6 groups defined in [Section 2.2](#22-stakeholder-categories).
   - Create a pivot table with impact types as rows and stakeholder groups as columns.
   - Count events in each cell.
   - Generate separate matrices for all years combined and for each individual year.

### Expected Behaviour for Edge Cases

| Scenario | Expected Outcome |
|----------|------------------|
| Records affected = `null` or unknown | Severity defaults to C6; confidence is lower (0.5–0.7) |
| All fields are "N/A" | Severity = C6, confidence ≈ 0.3–0.5, information_quality explains limitations |
| Records affected near a threshold (e.g., 10,000) | LLM may choose either adjacent category; reasoning should justify the choice |
| Same event classified twice (force reclassify) | INSERT OR REPLACE overwrites the previous classification |
| API call fails after 3 retries | Event is skipped; logged as warning; pipeline continues |
