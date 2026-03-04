# Event Filtering Stage

This document provides a detailed specification of the event filtering stage of the Australian Cyber Events pipeline. It covers the three distinct filtering mechanisms, their integration points, the machine learning model's training data and feature engineering, the full LLM prompts used for classification, the confidence-based thresholding system, and the rationale behind every design decision. The level of detail is sufficient for an independent reviewer to (a) assess whether these processes are appropriate for this use case and (b) replicate them from scratch using only this documentation.

---

## Table of Contents

1. [Purpose and Scope](#1-purpose-and-scope)
2. [Filtering Architecture Overview](#2-filtering-architecture-overview)
   - 2.1 [Why Multiple Filtering Stages Are Needed](#21-why-multiple-filtering-stages-are-needed)
   - 2.2 [Order of Operations and Justification](#22-order-of-operations-and-justification)
   - 2.3 [High-Level Data Flow](#23-high-level-data-flow)
3. [Stage 1: Random Forest ML Filter (Post-Scrape)](#3-stage-1-random-forest-ml-filter-post-scrape)
   - 3.1 [Purpose](#31-purpose)
   - 3.2 [Training Data](#32-training-data)
   - 3.3 [Feature Engineering](#33-feature-engineering)
   - 3.4 [Model Architecture and Hyperparameters](#34-model-architecture-and-hyperparameters)
   - 3.5 [Text Vectoriser Configuration](#35-text-vectoriser-configuration)
   - 3.6 [Source Type Encoder](#36-source-type-encoder)
   - 3.7 [Prediction and Decision Logic](#37-prediction-and-decision-logic)
   - 3.8 [Error Handling and Conservative Defaults](#38-error-handling-and-conservative-defaults)
   - 3.9 [Metadata Storage](#39-metadata-storage)
   - 3.10 [Model Artifacts and Versioning](#310-model-artifacts-and-versioning)
   - 3.11 [Top Feature Importances](#311-top-feature-importances)
   - 3.12 [Retraining the Model](#312-retraining-the-model)
4. [Stage 2: LLM Classification (GPT-4o-mini)](#4-stage-2-llm-classification-gpt-4o-mini)
   - 4.1 [Purpose](#41-purpose)
   - 4.2 [Two LLM Classification Contexts](#42-two-llm-classification-contexts)
   - 4.3 [Fast-Pass LLM Extraction (llm_extractor)](#43-fast-pass-llm-extraction-llm_extractor)
   - 4.4 [Full LLM Classification (llm_classifier)](#44-full-llm-classification-llm_classifier)
   - 4.5 [LLM Decision Criteria](#45-llm-decision-criteria)
   - 4.6 [Structured Output Enforcement](#46-structured-output-enforcement)
   - 4.7 [Error Handling](#47-error-handling)
5. [Stage 3: Final Random Forest Filter (Post-LLM)](#5-stage-3-final-random-forest-filter-post-llm)
   - 5.1 [Purpose](#51-purpose)
   - 5.2 [How It Differs from Stage 1](#52-how-it-differs-from-stage-1)
   - 5.3 [Decision Logic](#53-decision-logic)
6. [Confidence-Based Thresholding](#6-confidence-based-thresholding)
   - 6.1 [Confidence Score Threshold](#61-confidence-score-threshold)
   - 6.2 [Risk Level Assignment](#62-risk-level-assignment)
7. [Legacy Progressive Filter System](#7-legacy-progressive-filter-system)
   - 7.1 [Three-Stage Progressive Filter](#71-three-stage-progressive-filter)
   - 7.2 [Keyword-Based Confidence Filter](#72-keyword-based-confidence-filter)
   - 7.3 [Current Status](#73-current-status)
8. [Statistics and Audit Trail](#8-statistics-and-audit-trail)
9. [Justification of Design Decisions](#9-justification-of-design-decisions)
10. [Known Limitations](#10-known-limitations)
11. [Replication Guide](#11-replication-guide)

---

## 1. Purpose and Scope

The event filtering stage determines which discovered events are genuine, specific, Australian cyber security incidents worth enriching and including in the final dataset. It sits between content scraping (Phase 1: Discovery) and enrichment (Phase 2: Perplexity Enrichment) in the pipeline.

### The Problem Being Solved

The pipeline's data sources (Perplexity AI search, Google Custom Search, GDELT, Webber Insurance, OAIC) return a wide range of results that include:

- **True positives:** Specific cyber incidents affecting named Australian organisations (e.g., "Toll Group Ransomware Attack", "Canva data breach").
- **False positives:** General cyber security news, trend reports, OAIC regulatory summaries, policy documents, educational content, international events without Australian impact, and completely unrelated content (sports, weather, entertainment) that happens to mention keywords like "attack" or "breach".

Without filtering, false positives would overwhelm the dataset, consume expensive LLM API calls for enrichment, and degrade the quality of the final event database.

### What This Stage Does

1. **ML-based pre-screening** — Uses a trained Random Forest classifier to discard clearly non-cyber content before any LLM API calls are made.
2. **LLM-based semantic classification** — Uses GPT-4o-mini to determine whether an article describes a specific, concrete Australian cyber incident.
3. **Post-LLM ML confirmation** — Re-applies the Random Forest filter after LLM analysis as a final gate before creating an enriched event record.
4. **Confidence-based thresholding** — Discards events below a minimum confidence score.

### What This Stage Does NOT Do

- Event discovery or data source querying (covered in [02-discovery-collection.md](02-discovery-collection.md))
- Content scraping (covered in [03-content-scraping.md](03-content-scraping.md))
- Perplexity AI enrichment (Phase 2)
- Global deduplication (Phase 3)
- ASD risk classification (covered in [07-ASD-risk-classification.md](07-ASD-risk-classification.md))

### Source Files

| File | Purpose |
|------|---------|
| `cyber_data_collector/filtering/rf_event_filter.py` | Random Forest ML filter: feature engineering, prediction, statistics |
| `cyber_data_collector/utils/llm_extractor.py` | Fast-pass GPT-4o-mini extraction and classification |
| `cyber_data_collector/processing/llm_classifier.py` | Full GPT-4o-mini classification with structured output |
| `cyber_data_collector/filtering/progressive_filter.py` | Legacy progressive filter system (superseded by RF filter) |
| `cyber_data_collector/filtering/confidence_filter.py` | Legacy keyword-based confidence filter (superseded by RF filter) |
| `cyber_data_collector/pipelines/discovery.py` | Pipeline orchestration: `_apply_rf_content_filter()`, `_enrich_single_event()` |
| `machine_learning_filter/` | Trained model artifacts and training data |

---

## 2. Filtering Architecture Overview

### 2.1 Why Multiple Filtering Stages Are Needed

A single filtering pass is insufficient because:

1. **Cost containment:** LLM API calls cost money. Running GPT-4o-mini on every scraped article (including obvious non-cyber content) would be wasteful. The ML pre-filter removes ~70% of irrelevant content before any LLM call is made.

2. **Semantic understanding:** The ML classifier operates on statistical text features (TF-IDF). It can detect whether an article is broadly about cyber security, but it cannot reliably determine whether the article describes a *specific incident* affecting a *named Australian organisation* versus a general advisory or trend report. That semantic distinction requires LLM reasoning.

3. **Defence in depth:** No single classifier is perfect. By combining a fast statistical filter with a slower semantic filter, the pipeline reduces both false positives (non-cyber content reaching enrichment) and false negatives (genuine incidents being discarded). The ML filter is biased toward inclusion (conservative), while the LLM provides precision.

### 2.2 Order of Operations and Justification

The filtering stages execute in this order:

```
Scraped Content → [RF Pre-Filter] → [LLM Classification] → [RF Post-Filter] → [Confidence Threshold] → Enriched Event
```

**Why the ML classifier runs before the LLM:**

| Consideration | ML Filter (Random Forest) | LLM Filter (GPT-4o-mini) |
|---------------|--------------------------|--------------------------|
| **Cost per event** | ~0 (local CPU inference) | ~$0.001–$0.005 per API call |
| **Latency** | <10 ms | 500–3000 ms |
| **Throughput** | Thousands per second | ~10 concurrent (rate-limited) |
| **Capability** | Binary text relevance classification | Semantic understanding of incident specificity |

Running the free, fast ML filter first eliminates obviously irrelevant content (entertainment articles, sports news, weather reports, product announcements) without spending API credits. Only articles that the ML model considers potentially cyber-relevant are sent to the LLM for deeper analysis. This reduces LLM API costs by approximately 70% compared to sending all scraped content to the LLM.

**Why there is a second RF filter after the LLM:**

The post-LLM RF filter serves as a safety net. It re-evaluates the combined text features now that the full content has been assembled for LLM analysis. In practice, this second pass catches edge cases where the LLM accepted an event that the RF model's broader pattern recognition flags as unlikely to be a genuine cyber incident (e.g., articles where the LLM was overly permissive due to its "bias toward inclusion" instruction). The LLM analysis metadata is passed to this call but is not currently used by the RF model's feature vector — the metadata parameter exists for future integration.

### 2.3 High-Level Data Flow

```
                          ┌─────────────────────────────────────────────┐
                          │           Raw Events (scraped)              │
                          │  Title + Description + Content + URL        │
                          └─────────────────┬───────────────────────────┘
                                            │
                                            ▼
                          ┌─────────────────────────────────────────────┐
                          │  STAGE 1: Random Forest Pre-Filter          │
                          │  _apply_rf_content_filter()                 │
                          │                                             │
                          │  • Concatenate title+desc+content+URL       │
                          │  • TF-IDF vectorise (1000 features)         │
                          │  • Encode source type                       │
                          │  • RF prediction (keep/filter)              │
                          │                                             │
                          │  Threshold: RF binary prediction (0.5)      │
                          │  Error default: KEEP                        │
                          ├─────────────────┬───────────────────────────┤
                          │     KEEP        │         FILTER            │
                          └────────┬────────┴────────────┬──────────────┘
                                   │                     │
                                   ▼                     ▼
                          ┌────────────────┐    ┌────────────────────┐
                          │  Proceed to    │    │  Discarded         │
                          │  LLM analysis  │    │  (logged as        │
                          │                │    │  "Content filtered  │
                          │                │    │   out as non-cyber")│
                          └───────┬────────┘    └────────────────────┘
                                  │
                                  ▼
                          ┌─────────────────────────────────────────────┐
                          │  STAGE 2: LLM Classification                │
                          │  extract_event_details_with_llm() OR        │
                          │  LLMClassifier.classify_events()            │
                          │                                             │
                          │  • GPT-4o-mini with structured output       │
                          │  • Determines: is_australian_event?         │
                          │  • Determines: is_specific_event?           │
                          │  • Determines: is_cybersecurity_event?      │
                          │  • Extracts entity names, dates, records    │
                          │                                             │
                          │  Decision: Both flags must be true          │
                          │  Error default: Event dropped               │
                          └─────────────────┬───────────────────────────┘
                                            │
                                            ▼
                          ┌─────────────────────────────────────────────┐
                          │  STAGE 3: Random Forest Post-Filter         │
                          │  filter_system.should_keep_event()          │
                          │                                             │
                          │  Same RF model, same features               │
                          │  LLM metadata passed but not yet used       │
                          │                                             │
                          │  Threshold: RF binary prediction (0.5)      │
                          │  Error default: KEEP                        │
                          └─────────────────┬───────────────────────────┘
                                            │
                                            ▼
                          ┌─────────────────────────────────────────────┐
                          │  STAGE 4: Confidence Threshold              │
                          │                                             │
                          │  event.confidence.overall >= 0.6            │
                          │  (set in collection config)                 │
                          └─────────────────┬───────────────────────────┘
                                            │
                                            ▼
                          ┌─────────────────────────────────────────────┐
                          │  Enriched Event Created                     │
                          │  Stored in EnrichedEvents table             │
                          └─────────────────────────────────────────────┘
```

---

## 3. Stage 1: Random Forest ML Filter (Post-Scrape)

**Source file:** `cyber_data_collector/filtering/rf_event_filter.py`
**Integration point:** `cyber_data_collector/pipelines/discovery.py`, method `_apply_rf_content_filter()`

### 3.1 Purpose

The Random Forest filter is a binary classifier that predicts whether a raw event's text content relates to a genuine cyber security incident. It runs immediately after content scraping and before any LLM API calls, serving as a cost-saving pre-filter that discards obviously irrelevant content at near-zero computational cost.

### 3.2 Training Data

**Location:** `machine_learning_filter/event_training_data.csv` (also available as `.xlsx`)

The training data was derived from actual pipeline runs. Events were collected from all data sources, scraped, and then manually labelled.

**Dataset statistics:**

| Metric | Value |
|--------|-------|
| Total rows in file | 203 |
| Rows with labels (`filter_keep` not null) | 119 |
| Rows without labels (unlabelled) | 84 |
| Positive class (`filter_keep = 1.0`, keep) | 34 (28.6% of labelled) |
| Negative class (`filter_keep = 0.0`, discard) | 85 (71.4% of labelled) |
| Rows with scraped content | 81 of 119 labelled |
| Average content length (where available) | ~4,426 characters |
| Average title length | ~34 characters |

**Label distribution by source type (labelled rows only):**

| Source Type | Count |
|-------------|-------|
| GDELT | 67 |
| WebberInsurance | 21 |
| GoogleSearch | 20 |
| Perplexity | 11 |

**Training data columns:**

| Column | Type | Description |
|--------|------|-------------|
| `raw_event_id` | TEXT | Unique event identifier |
| `source_type` | VARCHAR | Data source: GDELT, GoogleSearch, Perplexity, WebberInsurance |
| `source_event_id` | TEXT | Source-specific identifier |
| `raw_title` | TEXT | Event title as returned by the source |
| `raw_description` | TEXT | Event description or snippet |
| `raw_content` | TEXT | Full scraped article content |
| `event_date` | DATE | Event date |
| `source_url` | VARCHAR | Source article URL |
| `source_metadata` | TEXT | JSON metadata from the source |
| `discovered_at` | TIMESTAMP | When the event was discovered |
| `is_processed` | BOOLEAN | Processing status flag |
| `processing_attempted_at` | TIMESTAMP | Last processing attempt |
| `processing_error` | TEXT | Error message if processing failed |
| `created_at` | TIMESTAMP | Record creation timestamp |
| **`filter_keep`** | **FLOAT** | **Label: 1.0 = genuine cyber event (keep), 0.0 = non-relevant (discard)** |

**Class imbalance:** The dataset is imbalanced with a 71:29 negative-to-positive ratio. This reflects the real-world distribution where most search results are not specific cyber incidents. The imbalance biases the model toward the negative class, which is acceptable for this use case because false negatives (missing a genuine event) are less costly than false positives (wasting LLM API calls on irrelevant content). Genuine events that are incorrectly filtered out at this stage may still be rediscovered in future pipeline runs from different sources.

**Labelling criteria:** An event was labelled `filter_keep = 1.0` if it described a specific, concrete cyber security incident involving a named entity. Events labelled `filter_keep = 0.0` include:

- Summary reports covering multiple incidents (e.g., "Multiple Cyber Incidents Reported in Australia")
- OAIC regulatory reports (e.g., "OAIC Notifiable Data Breaches: January–June 2020")
- General trend articles, policy documents, educational content
- Non-cyber content that matched search keywords incidentally

### 3.3 Feature Engineering

The model uses two types of features, combined into a single feature vector of 1,001 dimensions:

#### 3.3.1 Text Features (1,000 dimensions)

All text fields are concatenated into a single string, preprocessed, and vectorised:

```
combined_text = preprocess(title) + ' ' + preprocess(description) + ' ' + preprocess(content) + ' ' + preprocess(url)
```

**Text preprocessing (`preprocess_text`):**
1. Convert to lowercase.
2. Collapse multiple whitespace to single spaces.
3. Strip leading/trailing whitespace.
4. Return empty string for null/NaN inputs.

**URL preprocessing (`preprocess_url`):**
1. Remove protocol prefix (`https?://`).
2. Replace hyphens and underscores with spaces.
3. Remove common TLD patterns (`.com`, `.org`, `.net`, `.edu`, `.gov`, `.au`, `.uk`, `.us`).
4. Remove `www.` prefix.
5. Collapse multiple whitespace.

The URL is preprocessed separately to extract meaningful tokens from the URL path and domain that may indicate cyber-relevance (e.g., "security-breach-company-name" becomes "security breach company name").

#### 3.3.2 Source Type Feature (1 dimension)

The data source type (GDELT, GoogleSearch, Perplexity, WebberInsurance) is encoded as a single integer using scikit-learn's `LabelEncoder`. This feature captures the prior probability that events from a given source are relevant — for example, Perplexity results are more likely to be relevant than GDELT results because the Perplexity search queries are specifically crafted for Australian cyber events.

Unknown source types encountered at inference time default to encoding value `0`.

#### 3.3.3 Feature Vector Assembly

The final feature vector is constructed by horizontal concatenation:

```python
features = np.hstack([
    np.array([[source_type_encoded]]),    # Shape: (1, 1)
    text_features.toarray()               # Shape: (1, 1000)
])
# Result shape: (1, 1001)
```

### 3.4 Model Architecture and Hyperparameters

The model is a scikit-learn `RandomForestClassifier` with the following configuration:

| Hyperparameter | Value | Rationale |
|----------------|-------|-----------|
| `n_estimators` | 100 | Default value; provides sufficient ensemble diversity for a small dataset |
| `max_depth` | 5 | Limits tree depth to prevent overfitting on the small (119-sample) training set |
| `n_features_in_` | 1,001 | 1 source type + 1,000 TF-IDF features |
| `classes_` | `[0, 1]` | Binary: 0 = discard, 1 = keep |

**Why Random Forest:**

- **Interpretable:** Feature importances are directly available, allowing inspection of which text patterns drive decisions.
- **Robust to overfitting:** The ensemble of 100 shallow trees (max depth 5) is less prone to overfitting than a single deep tree, which is important given the small training set (119 labelled examples).
- **Handles mixed feature types:** Naturally handles the combination of a categorical feature (source type) with continuous TF-IDF features.
- **No feature scaling required:** Unlike SVM or logistic regression, Random Forest does not require feature normalisation.
- **Fast inference:** Sub-millisecond prediction time, essential for a pre-filter that processes every scraped event.
- **Probability calibration:** `predict_proba()` provides confidence scores that can be used for risk-level assignment and audit logging.

### 3.5 Text Vectoriser Configuration

The text vectoriser is a scikit-learn `TfidfVectorizer` with the following configuration:

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| `max_features` | 1,000 | Limits vocabulary to the 1,000 most informative terms; prevents curse of dimensionality on a small dataset |
| `ngram_range` | (1, 2) | Captures both single words ("ransomware") and bigrams ("data breach", "cyber attack") |
| `max_df` | 0.95 | Ignores terms appearing in >95% of documents (too common to be discriminative) |
| `min_df` | 2 | Ignores terms appearing in fewer than 2 documents (too rare to generalise) |
| `sublinear_tf` | False | Standard TF-IDF weighting (not sublinear) |
| Vocabulary size | 1,000 | Learned from training data |

**Why TF-IDF over alternatives:**

- **Simplicity:** TF-IDF is a well-understood, deterministic transformation with no hyperparameter tuning beyond what is listed above.
- **Interpretable:** Each dimension corresponds to a specific term or bigram, making the feature vector human-readable.
- **Efficiency:** Sparse matrix representation keeps memory usage low even for long articles.
- **Sufficient for this task:** The classifier's job is to distinguish "cyber security article" from "non-cyber article" — a relatively easy binary classification that does not require the semantic understanding of embedding-based approaches.

### 3.6 Source Type Encoder

The source type encoder is a scikit-learn `LabelEncoder` trained on the four source types present in the training data:

| Encoded Value | Source Type |
|---------------|-------------|
| 0 | GDELT |
| 1 | GoogleSearch |
| 2 | Perplexity |
| 3 | WebberInsurance |

Unknown source types at inference time are encoded as `0` (the default fallback). This means OAIC events (not present in the training data) are treated as GDELT events for the purpose of the source type feature. Since source type contributes only ~4% of the model's total feature importance, this has a minor impact on predictions.

### 3.7 Prediction and Decision Logic

For each event, the model produces two outputs:

1. **Binary prediction** (`model.predict()`): 0 (discard) or 1 (keep).
2. **Class probabilities** (`model.predict_proba()`): Probability of class 0 and class 1.

The **confidence score** is defined as the probability of class 1 (keep):

```python
confidence_score = float(prediction_proba[1])
```

The **decision** is the binary prediction directly:

```python
is_cyber_relevant = bool(prediction)  # True if prediction == 1
```

This means the effective decision threshold is 0.5 on the predicted probability. Events with P(keep) >= 0.5 are retained; events with P(keep) < 0.5 are discarded.

### 3.8 Error Handling and Conservative Defaults

The filter is designed to fail open (conservative — keep the event) in all error cases:

| Error Scenario | Behaviour |
|----------------|-----------|
| Model files not found | `RuntimeError` raised during `__init__`, preventing pipeline startup |
| Models not loaded (`is_loaded = False`) | Returns `is_cyber_relevant=True`, `confidence_score=0.5` with reasoning "Models not loaded - defaulting to keep" |
| Prediction error (any exception during `should_keep_event`) | Returns `is_cyber_relevant=True`, `confidence_score=0.3` with reasoning including the error message |
| Unknown source type | Encodes as `0` and proceeds with prediction |
| Null/empty text fields | Preprocessed to empty strings; prediction proceeds on available features |

**Justification:** In an event collection pipeline, false negatives (missing genuine events) are more costly than false positives (sending non-cyber content to the LLM). A false positive costs a few cents in API calls; a false negative means a genuine incident is permanently missing from the dataset. Therefore, all error paths default to keeping the event.

### 3.9 Metadata Storage

After each prediction, the filter's results are stored in the raw event's `source_metadata` JSON field via `_update_raw_event_confidence()`:

```json
{
  "content_filter_confidence": 0.87,
  "content_filter_reasoning": [
    "Random Forest prediction: KEEP",
    "Confidence score: 0.870",
    "Source type: Perplexity",
    "Text length: 4521"
  ],
  "content_filter_stage": "rf_filter",
  "content_filter_risk_level": "low"
}
```

This metadata enables post-hoc auditing of filtering decisions without re-running the model.

### 3.10 Model Artifacts and Versioning

All model artifacts are stored in `machine_learning_filter/`:

| File | Size | Contents |
|------|------|----------|
| `random_forest_filter.pkl` | ~117 KB | Serialised `RandomForestClassifier` (100 trees, max depth 5) |
| `text_vectorizer.pkl` | ~38 KB | Serialised `TfidfVectorizer` (1,000-term vocabulary) |
| `source_type_encoder.pkl` | ~0.3 KB | Serialised `LabelEncoder` (4 classes) |
| `event_training_data.csv` | ~492 KB | Training data with labels (203 rows, 119 labelled) |
| `event_training_data.xlsx` | ~234 KB | Same training data in Excel format |

**Version compatibility:** The models were trained with scikit-learn 1.8.0. The `_load_pickle()` method handles version mismatches by:

1. Loading the pickle file while capturing any `InconsistentVersionWarning`.
2. If a version warning is raised, validating the loaded object (checking for required methods like `predict`, `predict_proba`, `transform` and required attributes like `estimators_`, `classes_`, `vocabulary_`).
3. If validation passes, re-pickling the object with the current scikit-learn version to silence future warnings.
4. If validation fails, raising a `RuntimeError` instructing the user to retrain the model.

### 3.11 Top Feature Importances

The 20 most important features in the trained Random Forest model, as measured by Gini importance:

| Rank | Feature | Importance |
|------|---------|------------|
| 1 | `bigquery code` (bigram) | 0.0647 |
| 2 | `event detected` (bigram) | 0.0614 |
| 3 | `gdelt bigquery` (bigram) | 0.0489 |
| 4 | `source_type` | 0.0412 |
| 5 | `company` | 0.0392 |
| 6 | `bigquery` | 0.0392 |
| 7 | `event` | 0.0326 |
| 8 | `code` | 0.0263 |
| 9 | `cyber event` (bigram) | 0.0226 |
| 10 | `data` | 0.0201 |
| 11 | `information` | 0.0188 |
| 12 | `breach` | 0.0184 |
| 13 | `ransomware` | 0.0177 |
| 14 | `security` | 0.0174 |
| 15 | `affected` | 0.0171 |
| 16 | `incident` | 0.0166 |
| 17 | `detected gdelt` (bigram) | 0.0151 |
| 18 | `gdelt` | 0.0143 |
| 19 | `infrastructure` | 0.0142 |
| 20 | `code 172` (bigram) | 0.0133 |

**Interpretation:** The top features are heavily influenced by the GDELT source, which dominates the training data (67 of 119 labelled examples). Features like "bigquery code", "event detected", and "gdelt bigquery" are artefacts of GDELT's generic event descriptions (e.g., "Cyber event detected via GDELT BigQuery, code 172"). These features help the model identify and filter out GDELT events that lack specific incident details. The remaining high-importance features ("company", "breach", "ransomware", "security", "affected", "incident") capture genuine cyber-relevance signals.

### 3.12 Retraining the Model

No training script is included in the repository — the model was trained externally and the serialised artifacts were committed. To retrain the model:

1. **Prepare training data:** Export raw events from the database with manual `filter_keep` labels into `machine_learning_filter/event_training_data.csv`. The CSV must contain at minimum: `source_type`, `raw_title`, `raw_description`, `raw_content`, `source_url`, and `filter_keep` (1.0 or 0.0).

2. **Train the model:**

```python
import pandas as pd
import pickle
from sklearn.ensemble import RandomForestClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import LabelEncoder
import numpy as np
import re

# Load data
df = pd.read_csv('machine_learning_filter/event_training_data.csv')
df = df[df['filter_keep'].notna()]  # Only labelled rows

# Preprocess text
def preprocess_text(text):
    if pd.isna(text) or not text:
        return ""
    return re.sub(r'\s+', ' ', str(text).lower()).strip()

def preprocess_url(url):
    if pd.isna(url) or not url:
        return ""
    url = re.sub(r'^https?://', '', str(url))
    url = re.sub(r'[-_]', ' ', url)
    url = re.sub(r'\.(com|org|net|edu|gov|au|uk|us)/?', ' ', url)
    url = re.sub(r'www\.', '', url)
    return re.sub(r'\s+', ' ', url).strip()

# Combine text features
df['combined_text'] = (
    df['raw_title'].apply(preprocess_text) + ' ' +
    df['raw_description'].apply(preprocess_text) + ' ' +
    df['raw_content'].apply(preprocess_text) + ' ' +
    df['source_url'].apply(preprocess_url)
)

# Encode source type
encoder = LabelEncoder()
df['source_type_encoded'] = encoder.fit_transform(df['source_type'])

# Vectorise text
vectorizer = TfidfVectorizer(max_features=1000, ngram_range=(1, 2), max_df=0.95, min_df=2)
text_features = vectorizer.fit_transform(df['combined_text'])

# Assemble feature matrix
X = np.hstack([df[['source_type_encoded']].values, text_features.toarray()])
y = df['filter_keep'].values

# Train model
model = RandomForestClassifier(n_estimators=100, max_depth=5, random_state=42)
model.fit(X, y)

# Save artifacts
with open('machine_learning_filter/random_forest_filter.pkl', 'wb') as f:
    pickle.dump(model, f)
with open('machine_learning_filter/text_vectorizer.pkl', 'wb') as f:
    pickle.dump(vectorizer, f)
with open('machine_learning_filter/source_type_encoder.pkl', 'wb') as f:
    pickle.dump(encoder, f)
```

3. **Validate:** Run the pipeline on a held-out set of events and verify that the filter keeps genuine cyber events while discarding irrelevant content.

---

## 4. Stage 2: LLM Classification (GPT-4o-mini)

### 4.1 Purpose

The LLM classification stage provides semantic understanding that the ML filter cannot achieve. While the Random Forest can determine "this text is about cyber security", it cannot distinguish between:

- A *specific incident* report ("Toll Group was hit by ransomware on 31 January 2020")
- A *general summary* report ("Multiple cyber incidents were reported in January 2020")
- A *policy document* ("Australian Data Breach Action Plan 2020")

All three contain high-confidence cyber security terms and would pass the RF filter. The LLM reads the article content and makes a semantic judgment about whether a specific, concrete incident is being described.

### 4.2 Two LLM Classification Contexts

The pipeline uses GPT-4o-mini for classification in two different contexts, depending on which code path processes the event:

| Context | Module | When Used | Model |
|---------|--------|-----------|-------|
| **Fast-pass extraction** | `llm_extractor.py` | Individual event enrichment in `_enrich_single_event()` | `gpt-4o-mini` |
| **Full classification** | `llm_classifier.py` | Batch processing in `CyberDataCollector._process_events()` | `gpt-4o-mini` |

Both contexts make the same core determination (is this a specific Australian cyber incident?) but with different prompts and output schemas.

### 4.3 Fast-Pass LLM Extraction (llm_extractor)

**Source file:** `cyber_data_collector/utils/llm_extractor.py`
**Function:** `extract_event_details_with_llm(text_content, model="gpt-4o-mini")`

This is called during individual event enrichment (`_enrich_single_event()` in the discovery pipeline). It receives the combined text content (title + description + scraped content + metadata, up to 12,000 characters) and returns structured event details.

#### System Prompt (Verbatim)

```
You are an expert cybersecurity analyst. Your task is to analyze the provided news article text
and extract key information with high accuracy. Adhere strictly to the response format.
```

#### User Prompt (Verbatim)

```
Please analyze the following article text and extract the required details.

Key Instructions:
1. `is_australian_event`: Set to `true` if the event involves Australia, an Australian company,
   or Australian citizens. Otherwise, `false`.
2. `is_specific_event`: This is the most important instruction. Set to `true` if the article
   describes a specific, concrete cyber incident that has already happened (e.g., a data breach
   at a named company, a ransomware attack on a specific date). Set to `false` if the article
   is about a potential future threat, a general security warning, a report on cyber trends,
   or an opinion piece about cybersecurity. Focus on whether a specific event is the main
   subject of the article.
3. `primary_entity`: Identify the main organization that was the target of the attack. If no
   single primary entity is clear, leave it as null.
4. `affected_entities`: List any other named organizations or groups mentioned as being affected.
5. `summary`: Provide a brief, neutral summary of the incident described.
6. `event_date`: Extract the actual date when the cyber incident occurred (NOT the publication
   date). Look for phrases like 'in June 2025', 'last month', 'on June 5th', etc. Format as
   YYYY-MM-DD. If no specific date is found, set to null.
7. `records_affected`: Extract the specific number of people, customers, or data records affected.
   If a number is mentioned (e.g., '2 million customers', '50,000 records'), extract only the
   integer value. If no number is specified, set to null.

--- ARTICLE TEXT ---
{truncated_content}
```

#### Configuration

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Model | `gpt-4o-mini` | Lowest-cost OpenAI model with sufficient reasoning for this task |
| Temperature | `0.0` | Deterministic output for reproducibility |
| Max retries | `2` | Retries on transient API errors |
| Max input characters | `12,000` | Truncation limit to fit within model context window |
| Structured output | `instructor` library with Pydantic model | Ensures type-safe, parseable responses |

#### Structured Output Schema

```python
class ExtractedEventDetails(BaseModel):
    is_australian_event: bool    # Key filtering field
    is_specific_event: bool      # Key filtering field
    primary_entity: Optional[str]
    affected_entities: List[str]
    summary: str
    event_date: Optional[date]
    records_affected: Optional[int]
```

#### Filtering Decision

The LLM output is used for filtering in the final stage (`evaluate_final_stage()` in `confidence_filter.py`): an event is accepted only if **both** `is_australian_event` and `is_specific_event` are `true`. If either is `false`, the event is rejected.

### 4.4 Full LLM Classification (llm_classifier)

**Source file:** `cyber_data_collector/processing/llm_classifier.py`
**Class:** `LLMClassifier`

This is used during batch processing in `CyberDataCollector._process_events()`. It receives the event title, description, entity names, and raw data source snippets, and returns a comprehensive classification.

#### System Prompt (Verbatim)

```
You are a cybersecurity incident analyst focused on identifying SPECIFIC cybersecurity
incidents affecting Australian organizations. ACCEPT events that describe specific incidents
affecting named organizations, even if details are limited. Examples to ACCEPT: 'Toll Group
ransomware attack', 'Perth Mint data breach', 'ANU cyber attack', 'Canva security incident',
'Travelex ransomware'. Examples to REJECT: 'Multiple Cyber Incidents Reported', 'OAIC
Notifiable Data Breaches Report', 'What is a cyber attack?', policy documents. REJECT obvious
summaries, regulatory reports, and policy documents, but ACCEPT specific incidents. When in
doubt about whether something is a specific incident affecting a named organization, ACCEPT
it rather than reject it. Bias toward inclusion of potential incidents for further analysis.
```

#### User Prompt (Verbatim)

```
FIRST, determine if this is actually a cybersecurity INCIDENT and if it's Australian-relevant.

Event Title: {request.title}
Event Description: {request.description}
Affected Entities: {', '.join(request.entity_names)}
Raw Data Snippets: {' '.join(request.raw_data_sources)}

STEP 1 - VALIDATION (CRITICAL):
- `is_cybersecurity_event`: Is this genuinely about ONE SPECIFIC cybersecurity INCIDENT that
  actually happened to a named organization?
  - Return TRUE for: specific incidents affecting named organizations (e.g., "Toll Group
    Ransomware Attack", "Perth Mint data breach", "ANU cyber attack")
  - Return TRUE for: actual data breaches, cyber attacks, malware infections, ransomware
    attacks, phishing campaigns that OCCURRED to ONE specific named company/organization
  - Return FALSE for:
    * General summaries with words: "Multiple", "Several", "Various", "incidents"
    * Time-period reports: "January 2020", "Q1 2020", "2020 breaches"
    * OAIC regulatory reports and summaries
    * Policy documents: "action plan", "framework", "guidance", "guidelines", "recommendation"
    * Educational content: "What is a cyber attack?", training materials
    * General trend analyses or market reports
    * Regulatory guidance, compliance documents, privacy principles
    * Training materials, educational content, best practices
    * Celebrations, fireworks, New Year events, holidays, festivals, concerts, sports,
      elections, entertainment

- `is_australian_relevant`: Does this SPECIFIC INCIDENT affect Australian organizations,
  systems, or citizens?
  - Return TRUE for incidents affecting Australian entities
  - Return FALSE for: generic global events, events in other countries without Australian impact
- `rejection_reason`: If either above is false, explain why this should be rejected

EXAMPLES TO ACCEPT:
- "Toll Group Ransomware Attack" ✓ (specific incident, named organization)
- "Perth Mint visitor data stolen" ✓ (specific breach, named organization)
- "Australian National University cyber attack" ✓ (specific incident, named organization)
- "Canva Security Incident" ✓ (specific incident, named organization)
- "Travelex website hit by ransomware" ✓ (specific incident, named organization)

EXAMPLES TO REJECT:
- "Multiple Cyber Incidents Reported in Australia (January 2020)" ✗ (summary of multiple)
- "OAIC Notifiable Data Breaches: January–June 2020" ✗ (regulatory report)
- "What is a cyber attack?" ✗ (educational content)
- "Australian Data Breach Action Plan" ✗ (policy document)

STEP 2 - CLASSIFICATION:
- `event_type`: If cybersecurity incident, classify into appropriate category. If rejected,
  use "Other".
- `secondary_types`: List any other relevant event categories (empty list if rejected).
- `severity`: If cybersecurity incident, assess severity. If rejected, use "Unknown".
- `detailed_description`: If cybersecurity incident, provide detailed description. If rejected,
  can be empty.
- `technical_details`: If cybersecurity incident, provide technical details. If rejected,
  leave empty.
- `estimated_customers_affected`: If cybersecurity incident and mentioned, extract number.
  Otherwise null.
- `estimated_financial_impact`: If cybersecurity incident and mentioned, extract amount.
  Otherwise null.
- `regulatory_fine`: If mentioned, extract amount. Otherwise null.
- `regulatory_undertaking`: If mentioned, describe. Otherwise null.
- `response_actions`: If cybersecurity incident, list response actions. Otherwise empty list.
- `attribution`: If cybersecurity incident and mentioned, identify threat actor. Otherwise null.

CRITICAL REQUIREMENTS:
- ACCEPT specific incidents affecting named organizations, even if details are limited
- REJECT obvious summaries, reports, and policy documents
- Focus on the organization name and incident specificity
- When in doubt about whether something is a specific incident, ACCEPT it rather than reject it
- Always provide all fields even for rejected events (use defaults for rejected events).
```

#### Configuration

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Model | `gpt-4o-mini` | Cost-effective for binary classification + metadata extraction |
| Max retries | `2` | Handles transient API failures |
| Timeout | `60` seconds | Prevents indefinite hangs on slow API responses |
| Concurrency | `asyncio.Semaphore(10)` | Limits to 10 concurrent API calls to avoid rate limits |
| Structured output | `instructor` library with Pydantic model | Ensures type-safe responses |

#### Structured Output Schema

```python
class EventEnhancement(BaseModel):
    is_cybersecurity_event: bool          # Key filtering field
    is_australian_relevant: bool          # Key filtering field
    rejection_reason: Optional[str]
    event_type: CyberEventType
    secondary_types: List[CyberEventType]
    severity: EventSeverity
    detailed_description: str
    technical_details: str
    estimated_customers_affected: Optional[int]
    estimated_financial_impact: Optional[float]
    regulatory_fine: Optional[float]
    regulatory_undertaking: Optional[str]
    response_actions: List[str]
    attribution: Optional[str]
    confidence_adjustments: Dict[str, float]
```

#### Filtering Decision

An event is **rejected** (returns `None`, dropping it from the pipeline) if either:
- `is_cybersecurity_event` is `false`, OR
- `is_australian_relevant` is `false`

### 4.5 LLM Decision Criteria

Both LLM contexts use the same conceptual criteria, summarised here:

**Accept if:**
- The article describes a specific, concrete cyber security incident
- The incident has already occurred (not a future threat or advisory)
- A named organisation is identified as the target or victim
- The incident has Australian relevance (affects Australian organisations, systems, or citizens)

**Reject if:**
- The article is a summary of multiple incidents
- The article is a regulatory report, policy document, or guidance
- The article is educational, advisory, or opinion content
- The article covers non-cyber topics (entertainment, sports, weather)
- The event is not specific to a named organisation
- The event has no Australian connection

**Bias direction:** Both prompts explicitly instruct the LLM to "bias toward inclusion" and "when in doubt, ACCEPT". This is intentional — the pipeline prefers to include borderline events for subsequent deduplication and enrichment rather than risk losing genuine incidents.

### 4.6 Structured Output Enforcement

Both LLM contexts use the `instructor` library (a wrapper around OpenAI's API that enforces Pydantic model schemas on LLM responses). This means:

1. The LLM's response is automatically validated against the Pydantic schema.
2. Type mismatches (e.g., returning a string where a boolean is expected) trigger automatic retries.
3. Missing required fields trigger automatic retries.
4. The pipeline always receives a well-typed Python object or an exception — never malformed JSON.

### 4.7 Error Handling

| Error Scenario | Fast-Pass (`llm_extractor`) | Full Classification (`llm_classifier`) |
|----------------|----------------------------|----------------------------------------|
| API error | Returns `None`; event is dropped from enrichment | Returns original event unchanged |
| Timeout | Returns `None` | Raises `asyncio.TimeoutError`; event counts as rejected |
| Parsing error | `instructor` retries up to 2 times, then returns `None` | `instructor` retries up to 2 times, then raises exception |
| Empty input | Returns `None` immediately (checked before API call) | Not applicable (input comes from prior stages) |

---

## 5. Stage 3: Final Random Forest Filter (Post-LLM)

### 5.1 Purpose

After the LLM has analysed the event and determined it is a specific Australian cyber incident, the pipeline applies the Random Forest filter one more time before creating the enriched event record. This serves as a final safety net.

**Source:** `cyber_data_collector/pipelines/discovery.py`, lines 1805–1821 within `_enrich_single_event()`

### 5.2 How It Differs from Stage 1

The post-LLM filter call uses the **same model** and **same features** as the pre-filter. The only difference is that LLM analysis metadata is passed in the `metadata` parameter:

```python
final_filter_result = self.filter_system.should_keep_event(
    source_type=raw_event.get('source_type', ''),
    title=raw_event.get('raw_title', ''),
    description=raw_event.get('raw_description', ''),
    content=content or '',
    url=raw_event.get('source_url', ''),
    metadata={
        'llm_analysis': {
            'is_australian_event': enriched_data.is_australian_event,
            'is_specific_event': enriched_data.is_specific_event,
            'confidence_score': getattr(enriched_data, 'confidence_score', 0.7),
            'primary_entity': getattr(enriched_data, 'primary_entity', None)
        }
    }
)
```

However, the current `RfEventFilter.should_keep_event()` implementation does **not** use the `metadata` parameter in its feature engineering — the `metadata` argument is accepted but ignored. The feature vector is constructed from the same text and source type features as Stage 1. The metadata parameter exists as an extension point for future versions that may incorporate LLM signals into the ML model's feature vector.

### 5.3 Decision Logic

The decision follows the same logic as Stage 1:

- If the RF model predicts "keep" (`prediction == 1`): the enriched event is created and stored.
- If the RF model predicts "filter" (`prediction == 0`): the event is discarded even though the LLM approved it. A log entry is created with `final_filtering` status `filtered_out`, including both the RF confidence and the LLM's `is_australian_event` / `is_specific_event` flags for audit purposes.
- On error: the event is conservatively kept.

The final filter's confidence score and reasoning are stored in the enriched event data as `final_filter_confidence` and `final_filter_reasoning`.

---

## 6. Confidence-Based Thresholding

### 6.1 Confidence Score Threshold

After all filtering stages, a final confidence threshold is applied as part of the discovery pipeline's month processing:

```python
high_confidence_events = [
    event for event in deduplicated_events
    if event.confidence.overall >= collector.config.confidence_threshold
]
```

**Threshold value:** `0.6` (set in `_build_collection_config()` in `discovery.py`)
**Default in config model:** `0.7` (defined in `CollectionConfig`, overridden to `0.6` by the pipeline)

The `confidence.overall` score on each event is set during the LLM classification stage and reflects the LLM's assessment of how confident it is that this is a genuine cyber event. Events below 0.6 overall confidence are filtered out at this final stage.

### 6.2 Risk Level Assignment

The Random Forest filter also assigns a risk level based on its confidence score:

| Confidence Score | Risk Level | Meaning |
|-----------------|------------|---------|
| >= 0.8 | `low` | High confidence in the prediction; low risk of error |
| 0.4 – 0.8 | `medium` | Moderate confidence; the event is borderline |
| < 0.4 | `high` | Low confidence in the prediction; high risk of error |

This risk level is stored in the event metadata for audit purposes. It does not directly affect the filtering decision (which uses the binary prediction), but it provides useful context for human review.

---

## 7. Legacy Progressive Filter System

The repository contains a legacy filtering system that has been superseded by the Random Forest filter but remains in the codebase. This section documents it for completeness.

### 7.1 Three-Stage Progressive Filter

**Source file:** `cyber_data_collector/filtering/progressive_filter.py`

The `ProgressiveFilterSystem` implements a three-stage filtering approach:

| Stage | Method | Threshold | Behaviour |
|-------|--------|-----------|-----------|
| 1. Discovery | `should_discover_event()` | 0.2 (very permissive) | Minimal false negatives |
| 2. Content | `should_process_content()` | 0.4 (balanced) | After scraping, balanced precision/recall |
| 3. Final | `should_enrich_event()` | 0.6 (high precision) | Based on LLM analysis results |

Each stage delegates to the `ConfidenceBasedFilter` class.

### 7.2 Keyword-Based Confidence Filter

**Source file:** `cyber_data_collector/filtering/confidence_filter.py`

The `ConfidenceBasedFilter` assigns confidence scores using keyword matching rather than ML:

**Positive indicators (boost score):**
- **High-confidence terms** (+0.2 per match, max +0.4 in discovery / +0.5 in content): `cyber attack`, `cyberattack`, `data breach`, `ransomware`, `malware`, `phishing`, `ddos`, `hacking`, `security breach`, `data leak`, `vulnerability`, `exploit`, `zero-day`, `botnet`, `trojan`, `spyware`, `rootkit`, `keylogger`, `backdoor`, and more.
- **Medium-confidence terms** (+0.1 per match, max +0.3): `virus`, `worm`, `firewall`, `antivirus`, `encryption`, `authentication`, `intrusion`, `social engineering`, `identity theft`, `fraud`, `credential`, `password`, and more.
- **Context terms** (+0.05 per match, max +0.2, only if high/medium terms also present): `security`, `attack`, `breach`, `incident`, `threat`, `risk`, `compromise`, `unauthorized`, `malicious`, `investigation`, `forensics`, `mitigation`, `patch`.
- **Australian terms** (+0.05 per match, max +0.15 in discovery / +0.2 in content): `australia`, `australian`, `sydney`, `melbourne`, `nsw`, `ato`, `centrelink`, `acsc`, `asd`, `asio`, and others.
- **Narrative patterns** (+0.2): Regex patterns detecting incident language like "attacked", "breached", "compromised", "stolen data", "security incident", "cyber attack".
- **Technical indicators** (+0.15): Regex patterns for "IP address", "vulnerability", "CVE-\d+", "encryption", "authentication", "endpoint".
- **Incident language** (+0.15): Regex patterns for "incident response", "forensic analysis", "containment", "affected customers", "law enforcement", "privacy commissioner".
- **Content length bonuses** (+0.05 for >500 chars, +0.05 for >2,000 chars).
- **Cyber-relevant URL** (+0.1): URL contains "security", "cyber", "hack", or "breach".

**Negative indicators (reduce score):**
- **Strong negative terms** (-0.2 per match, max -0.6 in discovery / -0.5 in content): `wedding`, `birthday`, `graduation`, `football`, `cricket`, `olympics`, `movie`, `concert`, `bushfire`, `flood`, `earthquake`, `tsunami`, and more. In content stage, penalty is halved if high-confidence cyber terms are also present.
- **Moderate negative terms** (-0.1 per match, max -0.3): `covid`, `pandemic`, `hospital`, `education`, `school`, `university`, `transport`, `airport`, `airline`.

**Final stage (LLM-based):** The final stage ignores all keyword scoring and relies entirely on the LLM's `is_australian_event` and `is_specific_event` flags. If both are true, the score is set to 0.9; otherwise the event is rejected.

### 7.3 Current Status

The progressive filter and confidence filter classes remain in the codebase but are **not used in the active pipeline**. The `EventDiscoveryEnrichmentPipeline.__init__()` initialises `self.filter_system = RfEventFilter()`, not `ProgressiveFilterSystem()`. The legacy system is retained for potential future use as a fallback or for comparison testing.

---

## 8. Statistics and Audit Trail

### Run-Level Statistics

The `RfEventFilter` tracks per-run statistics:

| Statistic | Description |
|-----------|-------------|
| `events_processed` | Total events evaluated |
| `events_kept` | Events that passed the filter |
| `events_filtered` | Events that were discarded |
| `prediction_errors` | Events where prediction failed (conservatively kept) |
| `keep_rate` | `events_kept / events_processed` |
| `filter_rate` | `events_filtered / events_processed` |
| `error_rate` | `prediction_errors / events_processed` |

Statistics are reset at the start of each pipeline run via `reset_statistics()` and logged at pipeline close via `log_filtering_summary()`.

### Per-Event Audit Trail

Each filtering decision is recorded in the database:

1. **Pre-filter (Stage 1):** Confidence score, reasoning, stage, and risk level stored in `RawEvents.source_metadata` JSON field.
2. **LLM analysis:** Full LLM response stored via `log_processing_attempt()` with stage `llm_analysis` and status `success` or `failed`.
3. **Post-filter (Stage 3):** Logged via `log_processing_attempt()` with stage `final_filtering` and status `success` or `filtered_out`. Includes final confidence, reasoning, risk level, and (for filtered events) the LLM's `is_australian_event` and `is_specific_event` values.
4. **Enriched event:** The `final_filter_confidence` and `final_filter_reasoning` are stored directly in the enriched event record.

---

## 9. Justification of Design Decisions

### Why use ML before LLM?

The ML filter is a cost optimisation. At ~$0.001–$0.005 per GPT-4o-mini API call, processing 1,000 scraped articles would cost $1–$5. If 70% of those articles are irrelevant (consistent with the training data distribution of 71.4% negative class), the ML pre-filter saves $0.70–$3.50 per batch while adding negligible latency (~10 ms per event). Over hundreds of pipeline runs, this compounds into significant savings.

### Why use a Random Forest instead of a neural network?

1. **Small training set (119 labelled examples):** Neural networks require orders of magnitude more data to generalise. A Random Forest with shallow trees is one of the best-performing algorithms for small datasets.
2. **Interpretability:** Feature importances are directly inspectable, enabling quick diagnosis of filtering errors.
3. **No GPU required:** Inference runs on CPU in sub-millisecond time.
4. **No training infrastructure:** The model can be retrained in a single Python script without deep learning frameworks.

### Why use GPT-4o-mini instead of GPT-4o for classification?

The classification task (is this a specific Australian cyber incident?) requires basic reading comprehension, not complex reasoning. GPT-4o-mini is ~20x cheaper than GPT-4o while achieving comparable accuracy on straightforward binary classification tasks. The more expensive GPT-4o is reserved for the later enrichment phase where nuanced extraction is needed.

### Why bias the LLM toward inclusion?

Both LLM prompts instruct the model to "bias toward inclusion" and "when in doubt, ACCEPT". This reflects the asymmetric cost structure:

- **False positive cost:** A non-incident that passes filtering will be enriched (costing ~$0.01 in API calls) and later deduplicated or identified as low-quality during manual review. Cost: small.
- **False negative cost:** A genuine incident that is filtered out is permanently missing from the dataset. It may never be rediscovered, especially for older events. Cost: potentially large.

### Why apply the RF filter twice (pre-LLM and post-LLM)?

The pre-LLM application saves LLM API costs. The post-LLM application catches edge cases where the LLM's "bias toward inclusion" instruction causes it to accept borderline events that the RF model's pattern recognition correctly identifies as non-incidents. The double application is a "belt and suspenders" approach that slightly increases processing time but reduces both false positives and wasted API spend.

### Why keep the legacy progressive filter in the codebase?

The legacy system provides:
1. A fallback if the ML model artifacts are corrupted or incompatible.
2. A reference implementation for the keyword-based scoring logic, which may be useful for debugging or comparison.
3. The keyword lists serve as documentation of what constitutes positive and negative signals for cyber event relevance.

---

## 10. Known Limitations

1. **Small training set:** The model was trained on 119 labelled examples, which is small by ML standards. Performance may degrade on event types or source types not well-represented in the training data (e.g., OAIC events, which are absent from the training set).

2. **GDELT bias in training data:** 56% of labelled examples (67/119) are from GDELT. The model has learned GDELT-specific patterns ("bigquery code", "event detected") that are not generalisable. Events from non-GDELT sources may not be filtered as accurately.

3. **No cross-validation metrics:** The repository does not include evaluation metrics (accuracy, precision, recall, F1, AUC-ROC) from the model training. An independent reviewer cannot assess the model's generalisation performance without retraining and evaluating.

4. **Post-LLM RF filter uses same features:** The Stage 3 post-LLM filter does not incorporate the LLM's analysis into its feature vector. The `metadata` parameter is passed but ignored. This means Stage 3 will always agree with Stage 1 if the same text is presented — it provides redundancy rather than complementary signal.

5. **Unlabelled training data:** 84 of 203 rows in the training CSV lack labels. These rows are excluded during training but their presence suggests incomplete labelling. Labelling these rows could improve model performance.

6. **No OAIC source type in training data:** OAIC events are encoded as source type `0` (same as GDELT) by the label encoder fallback. This may affect prediction accuracy for OAIC-sourced events.

7. **Confidence threshold interaction:** The overall confidence threshold (0.6) operates on a different confidence score (the event's `confidence.overall` from LLM classification) than the RF filter's confidence score. These two scores measure different things and are not directly comparable.

---

## 11. Replication Guide

To replicate the complete filtering pipeline from scratch:

### Prerequisites

- Python 3.8+
- scikit-learn >= 1.3
- OpenAI API key (for GPT-4o-mini)
- `instructor` library (for structured LLM output)
- `pydantic` >= 2.0

### Step 1: Prepare Training Data

Export raw events from a database or create a CSV with columns: `source_type`, `raw_title`, `raw_description`, `raw_content`, `source_url`, `filter_keep` (1.0 or 0.0).

Label events as follows:
- `1.0` — Specific, concrete cyber security incident affecting a named entity
- `0.0` — Everything else (summaries, reports, advisories, non-cyber content)

Aim for at least 100 labelled examples with representation from all data sources.

### Step 2: Train the Random Forest Model

Follow the training script in [Section 3.12](#312-retraining-the-model). Key parameters:
- TF-IDF: `max_features=1000`, `ngram_range=(1,2)`, `max_df=0.95`, `min_df=2`
- Random Forest: `n_estimators=100`, `max_depth=5`
- Feature vector: 1 encoded source type + 1,000 TF-IDF features = 1,001 dimensions

### Step 3: Deploy Model Artifacts

Save three pickle files:
- `random_forest_filter.pkl` — the trained `RandomForestClassifier`
- `text_vectorizer.pkl` — the fitted `TfidfVectorizer`
- `source_type_encoder.pkl` — the fitted `LabelEncoder`

### Step 4: Implement the Filtering Pipeline

1. **Pre-filter:** After scraping, load the event's title, description, content, URL, and source type. Concatenate all text fields (lowercase, whitespace-normalised), vectorise with the saved TF-IDF, encode the source type, and predict with the saved Random Forest. Keep events where `prediction == 1`.

2. **LLM classification:** For events that pass the pre-filter, send the combined text (up to 12,000 characters) to GPT-4o-mini with the prompts documented in [Section 4.3](#43-fast-pass-llm-extraction-llm_extractor) or [Section 4.4](#44-full-llm-classification-llm_classifier). Use `instructor` or equivalent library to enforce the structured output schema. Accept events where both Australian relevance and incident specificity flags are true.

3. **Post-filter:** Re-apply the same Random Forest model to the event text as a confirmation step.

4. **Confidence threshold:** Discard events with overall confidence below 0.6.

### Step 5: Validate

Run the pipeline on a held-out set of events and manually verify:
- Genuine incidents are retained (recall)
- Non-incidents are discarded (precision)
- Error paths default to keeping events (conservative failure)

---

*Last updated: March 2026*
*Source files: `cyber_data_collector/filtering/`, `cyber_data_collector/utils/llm_extractor.py`, `cyber_data_collector/processing/llm_classifier.py`, `cyber_data_collector/pipelines/discovery.py`*
