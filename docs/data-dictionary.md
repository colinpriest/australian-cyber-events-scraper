# Data Dictionary

Comprehensive reference for all data structures in the Australian Cyber Events Discovery and Enrichment Pipeline, covering the SQLite database schema, JSON/CSV data files, ML model artifacts, and the relationships between them.

---

## Table of Contents

1. [Data Flow Overview](#data-flow-overview)
2. [Database Schema](#database-schema)
   - [Core Tables](#core-tables)
   - [Junction and Mapping Tables](#junction-and-mapping-tables)
   - [Audit and Tracking Tables](#audit-and-tracking-tables)
   - [Configuration Tables](#configuration-tables)
   - [Views](#views)
   - [Legacy Tables](#legacy-tables)
   - [Further tables](#further-tables)
3. [Database Relationships (ERD)](#database-relationships-erd)
4. [JSON Columns (Embedded JSON)](#json-columns-embedded-json)
5. [Data Files](#data-files)
   - [OAIC Statistics Files](#oaic-statistics-files)
   - [ASD Risk Classification Exports](#asd-risk-classification-exports)
   - [Machine Learning Artifacts](#machine-learning-artifacts)
   - [Authentication Files](#authentication-files)
   - [Dashboard Output](#dashboard-output)
   - [Export Outputs](#export-outputs)
6. [Enumerations and Controlled Vocabularies](#enumerations-and-controlled-vocabularies)
7. [Data Source Mapping](#data-source-mapping)

---

## Data Flow Overview

```
                    ┌──────────────────────────────────────┐
                    │         External Data Sources         │
                    │  Perplexity │ OAIC │ Google │ Webber  │
                    │             │      │ Search │ Ins.    │
                    └──────┬──────┴──┬───┴───┬────┴───┬────┘
                           │        │       │        │
                           ▼        ▼       ▼        ▼
                    ┌──────────────────────────────────────┐
                    │           RawEvents (DB)              │
                    │  + source_metadata (JSON column)      │
                    └──────────────┬───────────────────────┘
                                   │
                     ┌─────────────┼─────────────┐
                     ▼             ▼              ▼
              ┌────────────┐ ┌──────────┐ ┌───────────────┐
              │ProcessingLog│ │EntitiesV2│ │EnrichedEvents │
              └────────────┘ └────┬─────┘ │+ perplexity_  │
                                  │       │  enrichment_   │
                                  │       │  data (JSON)   │
                                  │       └───────┬────────┘
                                  │               │
                                  ▼               ▼
                         ┌──────────────────────────────────┐
                         │     EnrichedEventEntities         │
                         │     (junction table)              │
                         └──────────────────────────────────┘
                                        │
                                        ▼
                         ┌──────────────────────────────────┐
                         │      DeduplicatedEvents (DB)     │
                         │  + perplexity_enrichment_data    │
                         └──────────────┬───────────────────┘
                                        │
                     ┌──────────────────┼──────────────────┐
                     ▼                  ▼                  ▼
          ┌────────────────┐ ┌──────────────────┐ ┌──────────────────┐
          │EventDedup      │ │DeduplicatedEvent │ │DeduplicatedEvent │
          │  Map           │ │  Entities        │ │  Sources         │
          └────────────────┘ └──────────────────┘ └──────────────────┘
                                        │
                                        ▼
                         ┌──────────────────────────────────┐
                         │     ASDRiskClassifications        │
                         │  + reasoning_json (JSON column)   │
                         └──────────────────────────────────┘
                                        │
                    ┌───────────────────┼───────────────────┐
                    ▼                   ▼                   ▼
             ┌────────────┐   ┌──────────────┐   ┌───────────────┐
             │ risk_matrix/│   │ dashboard/   │   │  CSV/Excel    │
             │ *.xlsx      │   │ index.html   │   │  exports      │
             └────────────┘   └──────────────┘   └───────────────┘
```

### Data Sources → File/Table Mapping

| External Source | Ingested Into | Intermediate Files | Final Outputs |
|---|---|---|---|
| Perplexity AI | `RawEvents` → `EnrichedEvents` → `DeduplicatedEvents` | None | Dashboard, Excel exports |
| OAIC PDF Reports | `oaic_cyber_statistics_*.json` | `oaic_cyber_statistics_cleaned_*.json` | Dashboard OAIC trends |
| OAIC Power BI Dashboard | `oaic_cyber_statistics_*.json` | `oaic_cyber_statistics_cleaned_*.json` | Dashboard OAIC trends |
| Google Custom Search | `RawEvents` → `EnrichedEvents` → `DeduplicatedEvents` | None | Dashboard, Excel exports |
| Webber Insurance | `RawEvents` → `EnrichedEvents` → `DeduplicatedEvents` | None | Dashboard, Excel exports |
| GDELT (BigQuery) | `RawEvents` → `EnrichedEvents` → `DeduplicatedEvents` | None | Dashboard, Excel exports |

---

## Database Schema

**Database:** SQLite 3.x at `instance/cyber_events.db`

**Runtime PRAGMA settings** (set per-connection in `cyber_event_data_v2.py`):

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 30000;  -- 30 seconds
```

---

### Core Tables

#### RawEvents

Initial discovered events before any LLM processing. One row per discovered article/report.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `raw_event_id` | TEXT | **PK** | — | UUID primary key |
| `source_type` | VARCHAR(50) | NOT NULL | — | Data source identifier. Values: `Perplexity`, `OAIC`, `GoogleSearch`, `WebberInsurance`, `CyberDaily`, `GDELT` |
| `source_event_id` | TEXT | Yes | NULL | External identifier from the source system (e.g., GDELT event ID) |
| `raw_title` | TEXT | Yes | NULL | Original article/event title as scraped |
| `raw_description` | TEXT | Yes | NULL | Original description or snippet from the source |
| `raw_content` | TEXT | Yes | NULL | Full scraped article content (via Playwright or Perplexity fallback) |
| `event_date` | DATE | Yes | NULL | Date of the event as reported by the source |
| `source_url` | VARCHAR(2048) | Yes | NULL | URL of the original article or source page |
| `source_metadata` | TEXT | Yes | NULL | JSON blob with source-specific metadata. See [source_metadata JSON](#rawevents-source_metadata) |
| `publication_date` | TEXT | Yes | NULL | Day the source was published (`YYYY-MM-DD`): the source's own date, else the page's metadata, else a full date in the URL path. Never the incident date. See `processing/publication_dates.py` |
| `publication_date_source` | TEXT | Yes | NULL | Where `publication_date` came from: `source`, `page metadata: <field>` or `URL path` |
| `event_date_precision` | TEXT | Yes | NULL | How precisely the source states `event_date`: `day`, `month` or `year`; NULL = not stated |
| `event_date_basis` | TEXT | Yes | NULL | Where the source's `event_date` comes from (e.g. `Webber list heading (month)`, `stated in the source page`, `stated in the Perplexity discovery answer (unverified)` - no precision is recorded for the last) |
| `discovered_at` | TIMESTAMP | No | `CURRENT_TIMESTAMP` | When the pipeline discovered this event |
| `is_processed` | BOOLEAN | No | `FALSE` | Whether this event has been through LLM enrichment |
| `processing_attempted_at` | TIMESTAMP | Yes | NULL | Timestamp of last enrichment attempt |
| `processing_error` | TEXT | Yes | NULL | Error message if enrichment failed |
| `created_at` | TIMESTAMP | No | `CURRENT_TIMESTAMP` | Row creation timestamp |

**Indexes:**
- `idx_raw_events_source_type` on `(source_type)`
- `idx_raw_events_is_processed` on `(is_processed)`
- `idx_raw_events_discovered_at` on `(discovered_at)`

---

#### EnrichedEvents

LLM-processed events with extracted metadata, entity names, and confidence scores. Created by the enrichment pipeline from RawEvents.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `enriched_event_id` | TEXT | **PK** | — | UUID primary key |
| `raw_event_id` | TEXT | NOT NULL | — | **FK → RawEvents.raw_event_id** |
| `title` | VARCHAR(255) | NOT NULL | — | Enriched event title (may be improved from raw title) |
| `description` | TEXT | Yes | NULL | Enriched description |
| `summary` | TEXT | Yes | NULL | LLM-generated summary of the event |
| `event_type` | VARCHAR(50) | Yes | NULL | Classified event type. See [CyberEventType enum](#cybereventtype) |
| `severity` | VARCHAR(20) | Yes | NULL | Severity level. See [EventSeverity enum](#eventseverity) |
| `event_date` | DATE | Yes | NULL | Best-determined event date (LLM-extracted preferred over raw) |
| `records_affected` | BIGINT | Yes | NULL | Number of records/individuals affected |
| `is_australian_event` | BOOLEAN | NOT NULL | — | Whether the event is relevant to Australia |
| `is_specific_event` | BOOLEAN | NOT NULL | — | Whether this is a specific incident (vs. general news) |
| `confidence_score` | REAL | Yes | NULL | Overall confidence score (0.0–1.0); NULL = nothing measured one (never a default; a stored 0.7 / 0.8 that provenance proves was a default is cleared by `scripts/repair_confidence_defaults.py`) |
| `australian_relevance_score` | REAL | Yes | NULL | Australian relevance score (0.0–1.0) |
| `status` | VARCHAR(20) | No | `'Active'` | Event status: `Active`, `Merged`, `Deleted` |
| `created_at` | TIMESTAMP | No | `CURRENT_TIMESTAMP` | Row creation timestamp |
| `updated_at` | TIMESTAMP | No | `CURRENT_TIMESTAMP` | Last update timestamp |
| `perplexity_validated` | BOOLEAN | No | `FALSE` | Whether Perplexity fact-checking has been run |
| `perplexity_validated_at` | TIMESTAMP | Yes | NULL | When Perplexity validation was performed |
| `perplexity_enrichment_data` | TEXT | Yes | NULL | JSON blob with Perplexity enrichment results. See [perplexity_enrichment_data JSON](#perplexity_enrichment_data) |
| `date_confidence` | REAL | Yes | NULL | Confidence in the event date (0.0–1.0) |
| `entity_confidence` | REAL | Yes | NULL | Confidence in the entity identification (0.0–1.0) |
| `data_source_reliability` | REAL | Yes | NULL | Reliability score for the data source (0.0–1.0) |
| `attacking_entity_id` | INTEGER | Yes | NULL | ID of the threat actor entity (if known) |
| `attacking_entity_name` | VARCHAR(255) | Yes | NULL | Name of the threat actor (e.g., "LockBit", "APT28") |
| `attack_method` | VARCHAR(100) | Yes | NULL | Attack method used (e.g., "ransomware", "phishing") |
| `enrichment_pipeline_version` | VARCHAR(20) | No | `'v1_regex'` | Version of the enrichment pipeline used |
| `enrichment_confidence` | REAL | Yes | NULL | Confidence from the enrichment pipeline (0.0–1.0) |
| `enrichment_method` | VARCHAR(50) | No | `'regex'` | Enrichment method: `regex`, `gpt4o`, `perplexity` |
| `last_enrichment_audit_id` | TEXT | Yes | NULL | Reference to EnrichmentAuditTrail.audit_id |
| `perplexity_attempt_count` | INTEGER | No | `0` | Perplexity enrichment attempts so far (added idempotently by `CyberEventDataV2` on connect) |
| `perplexity_last_attempt_at` | TEXT | Yes | NULL | ISO timestamp of the last attempt |
| `perplexity_last_error` | TEXT | Yes | NULL | Why the last attempt was not accepted; cleared on success |
| `perplexity_status` | TEXT | Yes | NULL | `retrying`, `validated`, or `quarantined` (3 failed attempts; no longer retried) |
| `confidence_model` | TEXT | Yes | NULL | The model that adjusted `confidence_score` (the fast OpenAI model); NULL for a data source's fixed prior and whenever the score is NULL |
| `confidence_version` | TEXT | Yes | NULL | The rule that produced the score: `source-prior-v1` (the source's own prior) or `classifier-adjusted-v1`; NULL when the score is NULL |
| `records_affected_unit` | TEXT | Yes | — | What `records_affected` counts, read from the supporting source's words after the number: `records`, `accounts`, `customers` or `people`; NULL when unclear. Written by `processing/enrichment_gate.py`. |
| `records_affected_basis` | TEXT | Yes | — | How the source states the count: `stated`, `estimate` or `at_least`; NULL when unclear. Written by `processing/enrichment_gate.py`. |
| `records_affected_scope` | TEXT | Yes | — | Whether the count is `australian` or `global`; NULL when the source does not say. Written by `processing/enrichment_gate.py`. |
| `records_affected_source` | TEXT | Yes | — | Where the accepted count was found: the cited page's URL, or `record` for the record's own text. Written by `processing/enrichment_gate.py`. |

`data_source_reliability` is computed from the accepted Perplexity facts and is `NULL` when none carries a confidence.

**Triggers:**
- `trg_enriched_one_active_per_record` (`storage/enrichment_uniqueness.py`): `BEFORE INSERT` aborts with `record already has an active enrichment` when the new row is `Active` and `raw_event_id` already has an active row. Creation is also one `INSERT ... WHERE NOT EXISTS`, so `create_enriched_event` returns the existing id instead of failing. It is a trigger and not a unique index on `(raw_event_id) WHERE status = 'Active'` because `processing/record_identity.py` moves enrichments with an `UPDATE` of `raw_event_id` and may leave a record holding two (reported in `RecordIdentityChecks`); `scripts/check_data_integrity.py` (`active-enrichment-unique-per-record`) fails for any pair that finding does not explain.

**Indexes:**
- `idx_enriched_perplexity_validated` on `(perplexity_validated)`
- `idx_enriched_attacking_entity` on `(attacking_entity_id)`
- `idx_enrichedevents_audit` on `(last_enrichment_audit_id)`

**Foreign Keys:**
- `raw_event_id` → `RawEvents(raw_event_id)` ON DELETE CASCADE

---

#### EntitiesV2

Organizations, government agencies, and other entities mentioned in cyber events. Deduplicated by name.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `entity_id` | INTEGER | **PK** | AUTOINCREMENT | Auto-incrementing primary key |
| `entity_name` | VARCHAR(255) | NOT NULL, UNIQUE | — | Formal entity name (e.g., "Medibank Private Limited") |
| `entity_type` | VARCHAR(50) | Yes | NULL | Entity category. See [EntityType enum](#entitytype) |
| `industry` | VARCHAR(100) | Yes | NULL | Industry sector (e.g., "Healthcare", "Finance") |
| `turnover` | VARCHAR(50) | Yes | NULL | Annual turnover range (if known) |
| `employee_count` | INTEGER | Yes | NULL | Number of employees (if known) |
| `is_australian` | BOOLEAN | Yes | NULL | Whether the entity is Australian |
| `headquarters_location` | TEXT | Yes | NULL | Location of headquarters |
| `website_url` | TEXT | Yes | NULL | Entity's website URL |
| `confidence_score` | REAL | Yes | NULL | Confidence in entity identification (0.0–1.0); NULL = not scored (taken from the first link that created the entity) |
| `created_at` | TIMESTAMP | No | `CURRENT_TIMESTAMP` | Row creation timestamp |
| `updated_at` | TIMESTAMP | No | `CURRENT_TIMESTAMP` | Last update timestamp |
| `entity_kind` | TEXT | Yes | NULL | What the entity **is**, independent of any event: `organisation`, `government_body`, `product`, `person`, `threat_actor`, `other`. Distinct from the per-event role on `EnrichedEventEntities.relationship_type` |
| `size_estimate` | TEXT | Yes | NULL | Ordinal size band: `SMALL`, `MEDIUM`, `LARGE`, `HUGE`, `UNKNOWN`. See [Entity size bands](#entity-size-bands) |
| `size_confidence` | REAL | Yes | NULL | Confidence in the band (0.0–1.0) |
| `size_employees` | INTEGER | Yes | NULL | Best point estimate of headcount behind the band |
| `size_revenue_aud` | REAL | Yes | NULL | Approximate annual revenue, turnover or budget, in AUD |
| `size_basis` | TEXT | Yes | NULL | One or two sentences naming the decisive facts |
| `size_method` | TEXT | Yes | NULL | `perplexity` (researched online), `rule` (not a sizeable entity), `human` (never overwritten), `unavailable` (research unreachable — retried next run) |
| `size_sources` | TEXT | Yes | NULL | JSON array of URLs the research cited |
| `size_estimated_at` | TIMESTAMP | Yes | NULL | When the band was established |

**Note.** `turnover` and `employee_count` predate the size columns and were NULL
on every row; `size_employees` now also writes through to `employee_count` so
existing readers see data without changing. Populated by
`python scripts/dedup_admin.py size-entities`.

##### Entity size bands

| Band | Definition |
|---|---|
| `SMALL` | fewer than 20 employees, or under A$10m annual revenue |
| `MEDIUM` | 20–199 employees, or A$10m–100m |
| `LARGE` | 200–4,999 employees, or A$100m–1b |
| `HUGE` | 5,000+ employees or over A$1b; also national/state government departments, the major universities, ASX-100 companies |
| `UNKNOWN` | the organisation cannot be identified, or is not an organisation (a product, a person, a threat actor, a collective noun) |

The three-way cut follows the ABS definition of business size (small 0–19,
medium 20–199, large 200+); `LARGE` is split at 5,000 because that band
otherwise spans a mid-sized firm through to Telstra. Employee count is the
primary axis and revenue the tiebreaker, since government bodies and
universities have headcounts but no commercial turnover.

---

#### DeduplicatedEvents

Final unique events after entity-based deduplication. The primary table used for dashboard generation, exports, and risk classification.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `deduplicated_event_id` | TEXT | **PK** | — | UUID primary key |
| `master_enriched_event_id` | TEXT | NOT NULL | — | **FK → EnrichedEvents.enriched_event_id** (the "best" enriched event in the merge group) |
| `title` | VARCHAR(255) | NOT NULL | — | Final event title |
| `description` | TEXT | Yes | NULL | Final event description |
| `summary` | TEXT | Yes | NULL | Final event summary |
| `event_type` | VARCHAR(50) | Yes | NULL | Event type. See [CyberEventType enum](#cybereventtype) |
| `severity` | VARCHAR(20) | Yes | NULL | Severity level. See [EventSeverity enum](#eventseverity) |
| `event_date` | DATE | Yes | NULL | Earliest known event date across all merged sources |
| `records_affected` | BIGINT | Yes | NULL | Number of records/individuals affected |
| `is_australian_event` | BOOLEAN | NOT NULL | — | Australian relevance flag |
| `is_specific_event` | BOOLEAN | NOT NULL | — | Whether this is a specific incident |
| `confidence_score` | REAL | Yes | NULL | Overall confidence (0.0–1.0), copied from the master enriched event; NULL when the master has none |
| `australian_relevance_score` | REAL | Yes | NULL | Australian relevance (0.0–1.0) |
| `total_data_sources` | INTEGER | No | `0` | Count of distinct data sources that reported this event |
| `contributing_raw_events` | INTEGER | No | `0` | Count of merged raw events |
| `contributing_enriched_events` | INTEGER | No | `0` | Count of merged enriched events |
| `similarity_score` | REAL | Yes | NULL | Average pairwise similarity of merged events |
| `deduplication_method` | VARCHAR(50) | Yes | NULL | Method used: `title_similarity`, `entity_match`, `manual` |
| `status` | VARCHAR(20) | No | `'Active'` | Event status: `Active`, `Merged`, `Deleted` |
| `created_at` | TIMESTAMP | No | `CURRENT_TIMESTAMP` | Row creation timestamp |
| `updated_at` | TIMESTAMP | No | `CURRENT_TIMESTAMP` | Last update timestamp |
| `attacking_entity_id` | INTEGER | Yes | NULL | Threat actor entity ID |
| `attacking_entity_name` | VARCHAR(255) | Yes | NULL | Threat actor name |
| `attack_method` | VARCHAR(100) | Yes | NULL | Attack method used |
| `victim_organization_name` | VARCHAR(255) | Yes | NULL | Name of the victim organization |
| `victim_organization_industry` | VARCHAR(100) | Yes | NULL | Industry of the victim organization |
| `perplexity_validated` | BOOLEAN | No | `FALSE` | Whether Perplexity validation was performed |
| `perplexity_validated_at` | TIMESTAMP | Yes | NULL | When Perplexity validation occurred |
| `perplexity_enrichment_data` | TEXT | Yes | NULL | JSON blob with Perplexity enrichment. See [perplexity_enrichment_data JSON](#perplexity_enrichment_data) |
| `date_confidence` | REAL | Yes | NULL | Confidence in the event date (0.0–1.0) |
| `entity_confidence` | REAL | Yes | NULL | Confidence in entity identification (0.0–1.0) |
| `data_source_reliability` | REAL | Yes | NULL | Reliability score for data source (0.0–1.0) |
| `ai_involvement` | VARCHAR(20) | Yes | NULL | `AI-attacker`, `AI-vulnerability`, `AI-misuse`, `AI-uncertain` or `No-AI` (`scripts/classify_ai_involvement.py`): the label if the event is labelled, else AI-involved when P(AI) = 1 - `no_ai_probability` reaches the calibrated cut-off, else (no matching calibration) Jev's most probable option. NULL = not judged yet |
| `ai_involvement_basis` | TEXT | Yes | NULL | How `ai_involvement` was decided: `labelled`, `calibrated: P(AI) >= <cut-off>` / `< <cut-off>`, or `most probable option (not calibrated)` |
| `ai_attacker_probability` | REAL | Yes | NULL | Jev's probability that AI was the attacker or its instrument |
| `ai_vulnerability_probability` | REAL | Yes | NULL | Jev's probability that the victim's AI was attacked, manipulated or leaked data through a flaw |
| `ai_misuse_probability` | REAL | Yes | NULL | Jev's probability that there was no attack and the organisation's own use of AI was the breach (facial recognition, scraping into its own model, staff entering data into an AI tool). Added with question `ai-involvement-v4` |
| `no_ai_probability` | REAL | Yes | NULL | Jev's probability that no AI involvement is reported (the four sum to 1) |
| `ai_involvement_input_hash` | TEXT | Yes | NULL | sha256 of what the label was decided on: the evidence, question and escalation versions, both models, the cut-off and any label. A label whose current hash differs is stale (`ai_involvement.stale_judgements`; the AI report and dashboard count and mark it). NULL = judged before hashes were kept (stale until the step runs again) |
| `ai_involvement_error` | TEXT | Yes | NULL | Why the last re-judgement failed; the previous label is kept and reported stale. Cleared by the next successful judgement |
| `first_reported_date` | TEXT | Yes | NULL | Earliest `RawEvents.publication_date` among the event's records |
| `event_date_precision` | TEXT | Yes | NULL | Verified precision of `event_date` (`day` / `month` / `year`). Verified sources that give compatible evidence (a month and a day inside it) reconcile to the narrower; verified evidence for a different date (another month, or an interval that excludes `event_date`) leaves it NULL (`conflicting`); NULL = unverified or conflicting. `processing/incident_dates.reconcile_precision` |
| `event_date_earliest` | TEXT | Yes | NULL | First day of the incident-date interval; always contains `event_date`. NULL = no interval is known - the date is unknown, not a single day. The recurrence check and analyses measure gaps between these intervals (0 when they overlap) |
| `event_date_latest` | TEXT | Yes | NULL | Last day of the incident-date interval (NULL as above) |
| `reporting_lag_days` | INTEGER | Yes | NULL | `first_reported_date` minus `event_date`, only when the event date is not itself a publication or discovery date (see `date_basis`, `date_precision`) |
| `evidence_status` | TEXT | Yes | NULL | `supported` (an active record holds its own page text, >= 300 chars, and one has validated Perplexity research), `partial` (one of them, or only another page's text) or `unsupported` (neither). Written by `dedup_admin.py recover-evidence` (`processing/evidence_recovery.py`); NULL = not assessed yet, shown as `not assessed` |
| `evidence_status_reason` | TEXT | Yes | NULL | What the evidence lacks, record by record, and where retrieval stands (re-fetch queued / given up, research retrying / quarantined, labelled report or replacement queued) |
| `evidence_uncertain_fields` | TEXT (JSON) | Yes | NULL | For an `unsupported` event, the set fields nothing supports (`title`, `event_date`, `event_type`, `severity`, `victim_organization_name`, `attacking_entity_name`, `attack_method`, `records_affected`); `[]` otherwise |
| `evidence_status_at` | TEXT | Yes | NULL | When `evidence_status` last changed |
| `personal_info_types_json` | TEXT | Yes | — | JSON object mapping eight OAIC personal-information types (`contact_information`, `identity_information`, `financial_details`, `health_information`, `tax_file_numbers`, `consumer_data_right`, `digital_id`, `other_sensitive_information`) to `true`/`false`, from Jev judgements; written by `processing/breach_source.py` (`scripts/enrich_pii_and_source_category.py`). |
| `breach_source_category` | VARCHAR(40) | Yes | — | OAIC source of breach from a Jev Choice: `Malicious or Criminal Attack`, `Human Error`, `System Fault` or `Unknown` (cannot tell); written by `processing/breach_source.py` (`scripts/enrich_pii_and_source_category.py`). |
| `dedup_certainty` | REAL | Yes | — | Certainty (0-1) of the decision that formed the event; read by `scripts/build_dedup_dashboard.py` (not written by current code). |
| `dedup_method` | TEXT | Yes | — | How the dedup ledger created the event row: `manual-split` for an event split out by `dedup/ledger.py`; NULL for events made by the deduplication run. |
| `dedup_batch_id` | TEXT | Yes | — | Ledger batch that created the event (a split's batch, e.g. `revert-<timestamp>`), matching `DedupDecisions` and `DedupSnapshots`; written by `dedup/ledger.py`. |
| `has_human_override` | INTEGER | NOT NULL | `0` | `1` when the event was the target of a ledger merge or was created by a split, `0` otherwise; `dedup/ledger.py` sets it for automated passes too, so it does not mean a person decided. |
| `vendor_organization_name` | TEXT | Yes | — | The supplier through which the victim was compromised (the event's chosen `vendor` role); NULL when none. Written by `dedup/entity_classification.py` (`dedup_admin.py classify-entities`) and cleared by `dedup/supplier_role_labels.py`. |
| `vendor_organization_industry` | TEXT | Yes | — | Industry of the vendor in `vendor_organization_name`; written and cleared with it by `dedup/entity_classification.py` and `dedup/supplier_role_labels.py`. |
| `roles_member_signature` | TEXT | Yes | — | Hash of the event's member records when its entity roles were last classified; roles are stale when it differs from the current membership, is NULL, or is `record-content-changed` (set by `dedup/record_fingerprints.py` when a member's research changed). Written by `dedup/role_maintenance.py`. |
| `roles_classified_at` | TIMESTAMP | Yes | — | When the event's entity roles were last classified (ISO timestamp, local time); written by `dedup/role_maintenance.py`. |
| `facts_member_signature` | TEXT | Yes | — | SHA-256 of the sorted member record ids the event's facts (`records_affected`, `event_type`, `severity`, `attack_method`, `attacking_entity_name`) were last computed from; written by `dedup/event_facts.py` (`dedup_admin.py refresh-facts`). |
| `date_basis` | TEXT | Yes | — | How the event's stored date was determined, e.g. `Perplexity research`, `stated in the record`, `publication date (may lag the incident)`, `date the record was found (may lag the incident)`, `set by a repair pass (no record carries it)`, `owner label`, `unknown (...)` or `no date`; written by `dedup/date_provenance.py` and `processing/publication_dates.py`. |
| `date_precision` | TEXT | Yes | — | Displayed precision of the stored date: `day`, `month only, or the 1st (stored as the 1st)`, `year only (stored as 1 January)` or `unknown`; written by `dedup/date_provenance.py` (the verified precision is `event_date_precision`). |
| `ai_involvement_escalated` | INTEGER | Yes | — | `1` when the Jev AI-involvement answer was unsettled and the reasoning model re-judged it, `0` when Jev decided, NULL when not judged; written by `processing/ai_involvement.py` (`scripts/classify_ai_involvement.py`). |

Evidence retrieval state ([11-evidence-recovery.md](11-evidence-recovery.md))
lives in the tables of the mechanisms it reuses:
`PageRescrapes` (`status` `queued` = asked for by evidence recovery for a
record of any source; `retry` / `stored` / `gave_up` as before),
`CyberDailyRefetches`, and `SourceReplacements` (`purpose` NULL = a source-gate
replacement for a removed junk record, `evidence_recovery` = a search for an
incident with no usable evidence, with its `deduplicated_event_id`). A
replacement record is a new `RawEvents` row with its own URL and
`source_metadata.evidence_for` naming the incident.

The descriptions shown in `dashboard/ai_incidents.html` are in `EventDescriptions`
(`deduplicated_event_id`, `purpose = 'ai_incident_report'`, `prompt_version`,
`input_hash` of evidence + prompt + model, `model`, `description`, `created_at`);
a new row is written only when the evidence changes.

`PublicationDateFetches` (`raw_event_id`, `attempts`, `status`: stored / retry / no date on page / gave up, `last_error`, `last_attempt_at`) records each page fetched for its publication date.

`ContextualEntityAliases` (`raw_name`, `canonical_name`, `source`, `created_at`; key `raw_name` + `canonical_name`) holds acronym aliases that apply only where a record's text resolves them (`dedup/contextual_aliases.py`); one acronym may list several organisations. `EntityMentionResolutions` (`link_table`, `subject_id`, `raw_name`, `canonical_name` - NULL when unresolved, `method`: named in the text / jev / escalated / unresolved / escalation failed, `probability`, `moved_from`, `decided_at`) records where each link of an acronym was placed and why.

`EntityAliasDecisions` (`name`, `other_name`, `verdict`: same / parent_subsidiary / related / different / unsure, `canonical_name`, `reason`, `citations_json`, `model`, `prompt_version`, `decided_at`) holds Perplexity's verdict on every entity fold the name-structure rules proposed; only `same` folds, and accepted folds are written to `EntityAliases` with source `llm`.

`EntityAntiAliases` (`name_key_a`, `name_key_b` - the two names normalised and sorted, the key; `name_a`, `name_b` as written; `relation`: different / related / parent_subsidiary; `source`: human / llm; `reason`; `citations_json`; `model`; `created_at`, UTC with offset) lists names that look alike but are different entities ("Optus" / "Optus Stadium"). `llm` rows list every reusable not-the-same verdict of `EntityAliasDecisions` (removed by a later `same`), including those asked about the pairs victim counting joined on name shape alone; `human` rows come from `dedup_admin.py anti-alias add --reason` and are never replaced by a model. Victim counting, fold verification, stored aliases and the reversal of model folds all honour it (`dedup/anti_aliases.py`).

`EntityMerges` (`dedup/entity_merge_ledger.py`; `merge_id`, `kept_entity_id`, `kept_name`, `dropped_entity_id`, `dropped_name`, `source`: human / llm / structure, `decision_json` the alias verdict the fold rested on, `entity_row_json` and `kept_before_json` the two `EntitiesV2` rows before the fold, `inherited_json` the attributes filled on the kept row, `links_json` every link the dropped entity held with its outcome `moved` / `collided` and both original rows, `scalar_json` events whose victim or vendor name was renamed, `mentions_json`, `roles_json`, `source_ids_json` enriched / raw / deduplicated event ids, `references_json` rows of other tables still keyed by the dropped id, `alias_json`, `status`: applied / reversed, `merged_at`, `reversed_at`, `reversed_reason`) is the ledger of entity folds. `dedup_admin.py unmerge-entity <merge_id>` restores one exactly, and `canonicalise-entities` reverses a model-decided fold whose pair verdict later changes to anything but `same`. On a collision (one record or event linking both spellings) the surviving link keeps the stronger role in the order victim, vendor, affected_customer, primary_target, affected, threat_actor, regulator, product, mentioned, bystander, the higher `confidence_score` and the summed `source_count`.

**Indexes:**
- `idx_dedup_events_australian` on `(is_australian_event)`
- `idx_dedup_events_date` on `(event_date)`
- `idx_dedup_events_type` on `(event_type)`
- `idx_dedup_perplexity_validated` on `(perplexity_validated)`
- `idx_dedup_attacking_entity` on `(attacking_entity_id)`
- `idx_dedup_unique_event` UNIQUE on `(title, event_date)` WHERE `status = 'Active'`
- `idx_dedup_status_date` on `(status, event_date)`
- `idx_dedup_event_type` on `(event_type)` WHERE `event_type IS NOT NULL`
- `idx_dedup_severity` on `(severity)` WHERE `severity IS NOT NULL`

**Foreign Keys:**
- `master_enriched_event_id` → `EnrichedEvents(enriched_event_id)` ON DELETE CASCADE

---

#### ASDRiskClassifications

ASD (Australian Signals Directorate) C1–C6 risk matrix assignments for deduplicated events. Classified by GPT-4o.

| Column | Type | Nullable | Default | Constraints | Description |
|--------|------|----------|---------|-------------|-------------|
| `classification_id` | TEXT | **PK** | — | — | UUID primary key |
| `deduplicated_event_id` | TEXT | NOT NULL, UNIQUE | — | — | **FK → DeduplicatedEvents.deduplicated_event_id** |
| `severity_category` | VARCHAR(2) | NOT NULL | — | CHECK IN (`C1`..`C6`) | ASD severity: C1 (most severe) to C6 (least) |
| `primary_stakeholder_category` | VARCHAR(255) | NOT NULL | — | — | Affected stakeholder group. See [StakeholderCategory](#stakeholdercategory) |
| `impact_type` | VARCHAR(100) | NOT NULL | — | — | Type of impact. See [ImpactType](#impacttype) |
| `reasoning_json` | TEXT | NOT NULL | — | — | JSON with GPT-4o reasoning. See [reasoning_json JSON](#asdclassifications-reasoning_json) |
| `confidence_score` | REAL | NOT NULL | — | CHECK 0.0–1.0 | Classification confidence (0.0–1.0) |
| `model_used` | VARCHAR(50) | NOT NULL | — | — | LLM model used (e.g., `gpt-4o`) |
| `created_at` | TIMESTAMP | No | `CURRENT_TIMESTAMP` | — | Row creation timestamp |
| `updated_at` | TIMESTAMP | No | `CURRENT_TIMESTAMP` | — | Last update timestamp |
| `input_fingerprint` | TEXT | Yes | — | — | SHA-256 of the ASD prompt version and the prompt built from the event's current fields and the model; a stored classification is reused only while this matches, and NULL (pre-fingerprint rows) counts as stale. Written by `scripts/asd_risk_classifier.py`. |

**Indexes:**
- `idx_asd_risk_dedup_event_id` on `(deduplicated_event_id)`
- `idx_asd_risk_severity` on `(severity_category)`
- `idx_asd_risk_stakeholder` on `(primary_stakeholder_category)`

**Foreign Keys:**
- `deduplicated_event_id` → `DeduplicatedEvents(deduplicated_event_id)` ON DELETE CASCADE

---

### Junction and Mapping Tables

#### EnrichedEventEntities

Many-to-many relationship between enriched events and entities.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `enriched_event_id` | TEXT | **PK** (composite) | — | **FK → EnrichedEvents.enriched_event_id** |
| `entity_id` | INTEGER | **PK** (composite) | — | **FK → EntitiesV2.entity_id** |
| `relationship_type` | VARCHAR(50) | No | `'affected'` | Relationship: `primary_target`, `affected`, `mentioned` |
| `confidence_score` | REAL | Yes | NULL | Confidence in this entity–event relationship (0.0–1.0) |

**Foreign Keys:**
- `enriched_event_id` → `EnrichedEvents(enriched_event_id)` ON DELETE CASCADE
- `entity_id` → `EntitiesV2(entity_id)` ON DELETE CASCADE

---

#### DeduplicatedEventEntities

Many-to-many relationship between deduplicated events and entities, with cross-source counts.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `deduplicated_event_id` | TEXT | **PK** (composite) | — | **FK → DeduplicatedEvents.deduplicated_event_id** |
| `entity_id` | INTEGER | **PK** (composite) | — | **FK → EntitiesV2.entity_id** |
| `relationship_type` | VARCHAR(50) | No | `'affected'` | Relationship: `primary_target`, `affected`, `mentioned` |
| `confidence_score` | REAL | Yes | NULL | Confidence in this entity–event relationship (0.0–1.0) |
| `source_count` | INTEGER | No | `1` | Number of independent sources that mentioned this entity |

**Foreign Keys:**
- `deduplicated_event_id` → `DeduplicatedEvents(deduplicated_event_id)` ON DELETE CASCADE
- `entity_id` → `EntitiesV2(entity_id)` ON DELETE CASCADE

---

#### DeduplicatedEventSources

Source URLs and metadata for each deduplicated event.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `deduplicated_event_id` | TEXT | **PK** (composite) | — | **FK → DeduplicatedEvents.deduplicated_event_id** |
| `source_url` | VARCHAR(2048) | **PK** (composite) | — | URL of the source article |
| `source_type` | VARCHAR(50) | Yes | NULL | Data source type (`Perplexity`, `GoogleSearch`, etc.) |
| `credibility_score` | REAL | Yes | NULL | Source credibility score (0.0–1.0) |
| `content_snippet` | TEXT | Yes | NULL | Brief excerpt from the source |
| `discovered_at` | TIMESTAMP | Yes | NULL | When this source was first discovered |

**Indexes:**
- `idx_dedup_sources_type` on `(source_type)`

**Foreign Keys:**
- `deduplicated_event_id` → `DeduplicatedEvents(deduplicated_event_id)` ON DELETE CASCADE

---

#### EventDeduplicationMap

Tracks the lineage of which raw/enriched events were merged into each deduplicated event.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `map_id` | TEXT | **PK** | — | UUID primary key |
| `raw_event_id` | TEXT | NOT NULL | — | **FK → RawEvents.raw_event_id** |
| `enriched_event_id` | TEXT | Yes | NULL | **FK → EnrichedEvents.enriched_event_id** (NULL if raw wasn't enriched) |
| `deduplicated_event_id` | TEXT | NOT NULL | — | **FK → DeduplicatedEvents.deduplicated_event_id** |
| `contribution_type` | VARCHAR(50) | Yes | NULL | Role in the merge: `primary`, `supporting`, `duplicate` |
| `similarity_score` | REAL | Yes | NULL | Similarity to the master event (0.0–1.0) |
| `data_source_weight` | REAL | No | `1.0` | Importance weighting of this source |
| `created_at` | TIMESTAMP | No | `CURRENT_TIMESTAMP` | Row creation timestamp |

**Constraints:**
- UNIQUE on `(raw_event_id, deduplicated_event_id)`

**Indexes:**
- `idx_dedup_map_raw_event` on `(raw_event_id)`
- `idx_dedup_map_dedup_event` on `(deduplicated_event_id)`

**Foreign Keys:**
- `raw_event_id` → `RawEvents(raw_event_id)` ON DELETE CASCADE
- `enriched_event_id` → `EnrichedEvents(enriched_event_id)` ON DELETE CASCADE
- `deduplicated_event_id` → `DeduplicatedEvents(deduplicated_event_id)` ON DELETE CASCADE

---

#### EntityMappings

Maps variant entity names (subsidiaries, brands, aliases) to canonical names for deduplication.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `entity_mapping_id` | INTEGER | **PK** | AUTOINCREMENT | Auto-incrementing primary key |
| `source_entity` | TEXT | NOT NULL, UNIQUE | — | Entity name to map from (e.g., "Ticketmaster LLC") |
| `canonical_entity` | TEXT | NOT NULL | — | Canonical entity name to map to (e.g., "Live Nation Entertainment") |
| `relationship_type` | TEXT | No | `'subsidiary'` | Relationship: `subsidiary`, `brand`, `division`, `alias` |
| `notes` | TEXT | Yes | NULL | Free-text notes about the mapping |
| `created_at` | TEXT | No | `CURRENT_TIMESTAMP` | Row creation timestamp |
| `updated_at` | TEXT | No | `CURRENT_TIMESTAMP` | Last update timestamp (auto-updated via trigger) |

**Indexes:**
- `idx_entity_mappings_source` on `(source_entity)`
- `idx_entity_mappings_canonical` on `(canonical_entity)`

**Triggers:**
- `update_entity_mappings_timestamp` — updates `updated_at` on UPDATE

---

### Audit and Tracking Tables

#### Discovery retry queue: DiscoveryRecordState, SourceMonthDiscovery, SourceProbes

Written by `processing/retry_queue.py` (`ensure_schema` is idempotent and adds the columns below to an older database; the view `SourceMonthCompletion` is recreated each time). See [02-discovery-collection.md](02-discovery-collection.md) section 7.

| Table | Column | Type | Description |
|-------|--------|------|-------------|
| DiscoveryRecordState | `raw_event_id` | TEXT **PK** | The stored record |
| DiscoveryRecordState | `source_type`, `discovery_year`, `discovery_month` | | Where and when it was found |
| DiscoveryRecordState | `attempts` | INTEGER | *Recognised* permanent failures; the 3rd quarantines the record |
| DiscoveryRecordState | `temporary_failures` | INTEGER | Provider failures; never count toward quarantine |
| DiscoveryRecordState | `unknown_failures` | INTEGER | Failures nothing recognised as the provider's or the record's fault; retried with a 6 h to 7 day back-off, never quarantined |
| DiscoveryRecordState | `last_stage`, `last_error`, `last_error_kind`, `last_error_type` | TEXT | The latest failure; kind is `temporary` / `permanent` / `unknown`, type is a `fetch_errors` type |
| DiscoveryRecordState | `next_attempt_at`, `quarantined_at`, `completed_at`, `updated_at` | TEXT | Back-off and outcome |
| DiscoveryRecordState | `first_failed_at` | TEXT | When the record first failed processing (ISO timestamp, local time, to the second), kept across later failures; written by `RetryQueue.record_failure` in `processing/retry_queue.py`. |
| DiscoveryRecordState | `last_attempt_at` | TEXT | When the record last failed processing (ISO timestamp, local time, to the second); written by `RetryQueue.record_failure`. |
| SourceMonthDiscovery | `source_type`, `year`, `month` | **PK** | One source's search of one month |
| SourceMonthDiscovery | `status` | TEXT | `complete` or `failed` |
| SourceMonthDiscovery | `error_type`, `error_types_json` | TEXT | The deciding type and the type of each failure (`throttled`, `missing`, `network`, `parse`, `incomplete`, `other`, `unknown`) |
| SourceMonthDiscovery | `failed_attempts`, `next_attempt_at` | | Consecutive failures and the back-off (30 min doubling to 1 day; 6 h doubling to 7 days for `other` / `unknown`) |
| SourceMonthDiscovery | `gave_up_at` | TEXT | Set when the month is **given up** (permanent: `missing`, at once) or **parked** (temporary or unknown failures that reached 5 attempts) |
| SourceMonthDiscovery | `give_up_reason` | TEXT | `permanent`, `temporary` or `unknown`: only `permanent` waits for `discovery_queue.py release` |
| SourceMonthDiscovery | `give_up_config` | TEXT | JSON of the source configuration it failed under: hashed secrets (`secret:<ENV>`), `query` version, `client:<package>` versions (`processing/source_config.py`). A parked month reopens when this differs from the current configuration |
| SourceMonthDiscovery | `reopened_at`, `reopen_reason` | TEXT | Last automatic reopening: `probe succeeded` or `configuration changed: <components>` |
| SourceMonthDiscovery | `items_seen` | INTEGER | Items the source listed for the month (Cyber Daily: feed articles) |
| SourceMonthDiscovery | `events_found` | INTEGER | Records the source's last search of the month returned. |
| SourceMonthDiscovery | `failures_json` | TEXT | JSON list (at most 50) of the failures the data source noted on the last search; NULL when it was complete. |
| SourceMonthDiscovery | `attempted_at` | TEXT | When the month was last searched (ISO timestamp, local time, to the second); written by `RetryQueue.record_discovery` in `processing/retry_queue.py`. |
| SourceMonthDiscovery | `completed_at` | TEXT | Last time a search of the month finished without failure (ISO timestamp, local time); kept through later failed searches. |
| SourceProbes | `source_type` | TEXT **PK** | A source with parked months |
| SourceProbes | `last_probe_at`, `next_probe_at` | TEXT | One parked month is searched when `next_probe_at` passes (a week after the first month was parked; then 14 and 28 days while probes fail) |
| SourceProbes | `consecutive_failures`, `last_error_type`, `last_success_at` | | Probe history; a successful probe reopens every parked month of the source |

#### ConfidenceRepairs

Written by `scripts/repair_confidence_defaults.py`: one row per confidence value cleared because its provenance proved it a default (`table_name`, `row_key`, `column_name`, `old_value`, `rule`, `repaired_at`; primary key the first three). The old value is kept so the repair can be audited or reversed.

#### QualityRuns / QualityMetrics

Written by `scripts/quality_report.py` and `cyber_data_collector.quality.publication_allowed`
(see [10-quality-gate.md](10-quality-gate.md)). One `QualityRuns` row per quality run; one
`QualityMetrics` row per metric per run.

| Table | Column | Type | Description |
|-------|--------|------|-------------|
| QualityRuns | `run_id` | TEXT **PK** | `qr-<timestamp>-<hex>` |
| QualityRuns | `created_at` | TEXT | ISO timestamp of the run |
| QualityRuns | `db_path` | TEXT | The live database the run is about |
| QualityRuns | `snapshot_id`, `snapshot_sha256` | TEXT | The retained snapshot the run measured (`instance/snapshots/<id>.db.gz`, `publication/snapshot.py`); NULL when the live database was measured directly. Added by `ALTER TABLE` when a run is first stored |
| QualityRuns | `thresholds_version`, `thresholds_sha256` | TEXT | Which `thresholds.json` judged it |
| QualityRuns | `publication_allowed` | INTEGER | 1 when no blocking metric failed or went unmeasured |
| QualityRuns | `blocking_failures`, `failed`, `warned`, `not_measured` | INTEGER | Counts by status |
| QualityRuns | `reasons_json` | TEXT | JSON list of blocking reasons |
| QualityMetrics | `run_id`, `metric` | TEXT **PK** | Run and metric name (e.g. `dates.future_active`) |

#### EmbeddingCache / AdjudicationCache

Written by the deduplication adjudicators (`cyber_data_collector/dedup/adjudication_cache.py`),
created on first use. They let a rerun ask only what changed; nothing reads them but the adjudicators
and `scripts/evaluate_dedup.py`.

| Table | Column | Type | Description |
|-------|--------|------|-------------|
| EmbeddingCache | `content_hash`, `model`, `version` | TEXT **PK** | sha256 of the exact text embedded; the embedding model requested; `EMBED_TEXT_VERSION` (how the text is built) |
| EmbeddingCache | `dimensions` | INTEGER | Vector length |
| EmbeddingCache | `vector` | BLOB | Little-endian float32 |
| EmbeddingCache | `created_at` | TEXT | ISO timestamp |
| AdjudicationCache | `cache_key` | TEXT **PK** | sha256 of the kind, prompt and evidence versions, the model and the exact messages sent |
| AdjudicationCache | `kind` | TEXT | `pair` or `cluster` |
| AdjudicationCache | `prompt_version`, `evidence_version`, `model` | TEXT | What produced the answer (`pair-adjudication-v2` / `cluster-partition-v2`, `excerpts-v1`, the model) |
| AdjudicationCache | `subject_ids` | TEXT | JSON list of the ids judged, in the order shown |
| AdjudicationCache | `response_json` | TEXT | The validated answer (`LLMPairAdjudication` or `ClusterPartition`); only valid answers are stored |
| AdjudicationCache | `created_at` | TEXT | ISO timestamp |
| QualityMetrics | `metric_group` | TEXT | coverage / evidence / backlog / duplicates / dates / staleness / integrity / labels / synthetic |
| QualityMetrics | `value` | REAL | NULL when the check has a verdict instead of a value, or was not measured |
| QualityMetrics | `threshold` | TEXT | e.g. `<= 0.05`, `must pass`; empty for info metrics |
| QualityMetrics | `status` | TEXT | `pass` / `fail` / `warn` / `not_measured` / `info` |
| QualityMetrics | `blocking` | INTEGER | 1 when a failure blocks publication |
| QualityMetrics | `detail`, `examples_json` | TEXT | What was found (or why it could not be measured), and up to 10 examples |

#### CitedDocuments / CitedDocumentVersions

The pages a Perplexity answer cites, read so each fact can be checked against what its sources say
(`cyber_data_collector/processing/cited_documents.py`; created on first use, older tables gain the version columns
by `ALTER TABLE`). `CitedDocuments` holds the current version of each URL; `CitedDocumentVersions` the superseded
ones. `refresh-cited-documents` writes both (`processing/cited_refresh.py`).

| Table | Column | Type | Description |
|-------|--------|------|-------------|
| CitedDocuments | `url` | TEXT **PK** | The URL as cited |
| CitedDocuments | `fetched_at` | TEXT | When the current version was fetched |
| CitedDocuments | `status` | TEXT | `ok`, `http_error`, `fetch_error`, `unsupported_type`, `empty` |
| CitedDocuments | `http_status`, `content_type`, `final_url`, `error` | | The response, or why it failed |
| CitedDocuments | `text` | TEXT | Visible text of the page (at most 200,000 characters) |
| CitedDocuments | `published_date` | TEXT | The page's own publication date from its metadata |
| CitedDocuments | `etag`, `last_modified` | TEXT | The server's validators, sent back as `If-None-Match` / `If-Modified-Since` |
| CitedDocuments | `content_hash` | TEXT | SHA-256 of the whitespace-normalised text; backfilled for older rows |
| CitedDocuments | `version` | INTEGER | Which content version this row holds (1 on first read) |
| CitedDocuments | `first_fetched_at`, `last_checked_at`, `last_changed_at` | TEXT | First read; last fetch or validation of any kind; when the content last differed |
| CitedDocuments | `change_count`, `unchanged_streak` | INTEGER | Versions after the first; consecutive checks that found nothing new (lengthens the check interval) |
| CitedDocuments | `last_check_status`, `last_check_error` | TEXT | Latest check: `changed`, `unchanged`, `not_modified` (HTTP 304) or `failed` (the page already read is kept) |
| CitedDocumentVersions | `url`, `version` | TEXT, INTEGER **PK** | A superseded version of a URL |
| CitedDocumentVersions | `fetched_at`, `content_hash`, `etag`, `last_modified` | | When it was fetched and what identified it |
| CitedDocumentVersions | `http_status`, `content_type`, `final_url`, `text`, `published_date` | | The version as read |
| CitedDocumentVersions | `superseded_at` | TEXT | When a check found the content had changed |

#### EnrichmentAuditTrail

Complete audit trail for the 5-stage enrichment pipeline. One row per enrichment attempt.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `audit_id` | TEXT | **PK** | — | UUID primary key |
| `enriched_event_id` | TEXT | NOT NULL | — | Reference to EnrichedEvents (not a formal FK) |
| `raw_event_id` | TEXT | Yes | NULL | **FK → RawEvents.raw_event_id** |
| `pipeline_version` | VARCHAR(20) | NOT NULL | `'1.0'` | Pipeline version used |
| `started_at` | TIMESTAMP | NOT NULL | — | When enrichment started |
| `completed_at` | TIMESTAMP | Yes | NULL | When enrichment completed |
| `total_time_seconds` | REAL | Yes | NULL | Total processing time |
| `final_decision` | VARCHAR(30) | Yes | NULL | Final outcome: `AUTO_ACCEPT`, `ACCEPT_WITH_WARNING`, `REJECT`, `ERROR` |
| `final_confidence` | REAL | Yes | NULL | Overall confidence of enrichment (0.0–1.0) |
| **Stage 1: Content Acquisition** | | | | |
| `stage1_success` | BOOLEAN | Yes | NULL | Whether content was successfully acquired |
| `stage1_extraction_method` | VARCHAR(50) | Yes | NULL | Method used: `newspaper3k`, `trafilatura`, `beautifulsoup` |
| `stage1_content_length` | INTEGER | Yes | NULL | Word count of extracted content |
| `stage1_source_reliability` | REAL | Yes | NULL | Source reliability score (0.0–1.0) |
| `stage1_details` | TEXT | Yes | NULL | JSON with detailed stage 1 results |
| **Stage 2: GPT-4o Extraction** | | | | |
| `stage2_success` | BOOLEAN | Yes | NULL | Whether GPT-4o extraction succeeded |
| `stage2_victim_organization` | TEXT | Yes | NULL | Victim organization name extracted by GPT-4o |
| `stage2_confidence` | REAL | Yes | NULL | GPT-4o extraction confidence (0.0–1.0) |
| `stage2_is_specific_incident` | BOOLEAN | Yes | NULL | Whether GPT-4o identified a specific incident |
| `stage2_australian_relevance` | REAL | Yes | NULL | Australian relevance score from GPT-4o (0.0–1.0) |
| `stage2_tokens_used` | INTEGER | Yes | NULL | LLM tokens consumed |
| `stage2_details` | TEXT | Yes | NULL | JSON with detailed stage 2 results |
| **Stage 3: Perplexity Fact-Checking** | | | | |
| `stage3_checks_performed` | INTEGER | No | `0` | Number of fact checks attempted |
| `stage3_checks_passed` | INTEGER | No | `0` | Number of fact checks passed |
| `stage3_checks_failed` | INTEGER | No | `0` | Number of fact checks failed |
| `stage3_verification_confidence` | REAL | Yes | NULL | Verification confidence (0.0–1.0) |
| `stage3_details` | TEXT | Yes | NULL | JSON with detailed stage 3 results |
| **Stage 4: Validation** | | | | |
| `stage4_is_valid` | BOOLEAN | Yes | NULL | Whether validation passed |
| `stage4_error_count` | INTEGER | No | `0` | Number of validation errors |
| `stage4_warning_count` | INTEGER | No | `0` | Number of validation warnings |
| `stage4_validation_confidence` | REAL | Yes | NULL | Validation confidence (0.0–1.0) |
| `stage4_details` | TEXT | Yes | NULL | JSON with detailed stage 4 results |
| **Stage 5: Decision** | | | | |
| `stage5_stage_confidences` | TEXT | Yes | NULL | JSON with per-stage confidence breakdown |
| `stage5_penalties_applied` | TEXT | Yes | NULL | JSON with applied penalties |
| **Error Handling** | | | | |
| `error_message` | TEXT | Yes | NULL | Error message if enrichment failed |
| `error_stage` | VARCHAR(50) | Yes | NULL | Which stage failed |
| `created_at` | TIMESTAMP | No | `CURRENT_TIMESTAMP` | Row creation timestamp |

**Indexes:**
- `idx_audit_enriched_event` on `(enriched_event_id)`
- `idx_audit_raw_event` on `(raw_event_id)`
- `idx_audit_decision` on `(final_decision)`
- `idx_audit_confidence` on `(final_confidence)`
- `idx_audit_created` on `(created_at)`
- `idx_audit_pipeline_version` on `(pipeline_version)`
- `idx_audit_stage2_victim` on `(stage2_victim_organization)`
- `idx_audit_specific_incident` on `(stage2_is_specific_incident)`

**Foreign Keys:**
- `raw_event_id` → `RawEvents(raw_event_id)` (no cascade)

---

#### ProcessingLog

Log of individual processing stages for each raw event.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `log_id` | TEXT | **PK** | — | UUID primary key |
| `raw_event_id` | TEXT | NOT NULL | — | **FK → RawEvents.raw_event_id** |
| `processing_stage` | VARCHAR(50) | NOT NULL | — | Stage name: `url_scraping`, `llm_analysis`, `entity_extraction` |
| `status` | VARCHAR(20) | NOT NULL | — | Result: `success`, `failed`, `skipped` |
| `result_data` | TEXT | Yes | NULL | JSON with stage result data |
| `error_message` | TEXT | Yes | NULL | Error message if stage failed |
| `processing_time_ms` | INTEGER | Yes | NULL | Processing time in milliseconds |
| `created_at` | TIMESTAMP | No | `CURRENT_TIMESTAMP` | Row creation timestamp |

**Indexes:**
- `idx_processing_log_status` on `(status)`

**Foreign Keys:**
- `raw_event_id` → `RawEvents(raw_event_id)` ON DELETE CASCADE

---

#### RecordIdentityChecks

Does a record's stored page, and the enrichment attached to it, describe the record's own article? One row per
checked pairing; written by `processing/record_identity.py` (at ingest, and by `dedup_admin.py
check-record-identity`). See [README: records linked to the wrong article](../README.md#records-linked-to-the-wrong-article).

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `raw_event_id` | TEXT | NOT NULL | — | The record. **PK part 1** |
| `enriched_event_id` | TEXT | NOT NULL | `''` | The enrichment checked; `''` = a check of the record's page alone (before enrichment). **PK part 2** |
| `stage` | TEXT | NOT NULL | — | `ingest` (after scraping / as the enrichment was stored) or `audit` (`check-record-identity`) |
| `verdict` | TEXT | NOT NULL | — | `match` or `mismatch` |
| `kind` | TEXT | Yes | NULL | `page_other_article` (the stored page is not the record's article) or `enrichment_other_article` (the enrichment describes another record) |
| `reason` | TEXT | Yes | NULL | Why, in words (shared-word and page-identity findings) |
| `owner_raw_event_id` | TEXT | Yes | NULL | The one stored record whose title is exactly the enrichment's title, when found |
| `previous_raw_event_id` | TEXT | Yes | NULL | On a `repointed` match row: the record the enrichment was moved from (`check-record-identity --reverse` puts it back) |
| `action` | TEXT | Yes | NULL | `repointed`, `requeued_for_enrichment`, `rescrape_queued`, `rescraped_still_mismatched`, `replacement_searched`, `covered_by_another_event`, `needs_review`, `reversed_by_hand` |
| `action_detail` | TEXT | Yes | NULL | What was queued or why nothing was |
| `checked_at` | TEXT | NOT NULL | — | When last checked |
| `resolved_at` | TEXT | Yes | NULL | When the finding closed (re-attached, the page now belongs, or the pairing is sound); NULL = open |

**Indexes:**
- `idx_identity_open` on `(verdict, resolved_at)`

`check-record-identity` also removes tracking and identity query parameters (`utm_*`, subscriber `token`s, mail ids)
from every stored URL and from URLs quoted in stored text (`utils/url_clean.py`), and `RawEvents.source_url` is stored
without them from ingest on.

Open `page_other_article` rows with `action = 'rescrape_queued'` make the record a `rescrape-short-pages`
candidate whatever the length of its stored text (`PageRescrapes` holds the attempts).

---

#### AuditLog

General-purpose audit log for INSERT/UPDATE/DELETE operations across all tables.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `audit_id` | TEXT | **PK** | — | UUID primary key |
| `table_name` | VARCHAR(50) | NOT NULL | — | Name of the table modified |
| `record_id` | TEXT | NOT NULL | — | Primary key of the modified record |
| `action` | VARCHAR(20) | NOT NULL | — | Action type: `INSERT`, `UPDATE`, `DELETE` |
| `old_values` | TEXT | Yes | NULL | JSON of previous field values (for UPDATE/DELETE) |
| `new_values` | TEXT | Yes | NULL | JSON of new field values (for INSERT/UPDATE) |
| `changed_by` | TEXT | Yes | NULL | System or user identifier that made the change |
| `created_at` | TIMESTAMP | No | `CURRENT_TIMESTAMP` | When the change occurred |

---

#### DeduplicationClusters

Metadata about each group of events merged during deduplication.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `cluster_id` | TEXT | **PK** | — | UUID primary key |
| `deduplicated_event_id` | TEXT | NOT NULL | — | **FK → DeduplicatedEvents.deduplicated_event_id** |
| `cluster_size` | INTEGER | NOT NULL | — | Number of events merged into this cluster |
| `average_similarity` | REAL | Yes | NULL | Average pairwise similarity within the cluster |
| `deduplication_timestamp` | TIMESTAMP | No | `CURRENT_TIMESTAMP` | When deduplication was performed |
| `algorithm_version` | VARCHAR(20) | Yes | NULL | Version of the deduplication algorithm |

**Foreign Keys:**
- `deduplicated_event_id` → `DeduplicatedEvents(deduplicated_event_id)` ON DELETE CASCADE

---

#### MonthProcessed

Tracks which year/month combinations have been processed by the pipeline.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `id` | INTEGER | **PK** | AUTOINCREMENT | Auto-incrementing primary key |
| `year` | INTEGER | NOT NULL | — | Calendar year |
| `month` | INTEGER | NOT NULL | — | Calendar month (1–12) |
| `is_processed` | BOOLEAN | No | `FALSE` | Whether this month has been fully processed |
| `processed_at` | TIMESTAMP | Yes | NULL | When processing completed |
| `total_raw_events` | INTEGER | No | `0` | Raw events discovered for this month |
| `total_enriched_events` | INTEGER | No | `0` | Enriched events produced for this month |
| `processing_notes` | TEXT | Yes | NULL | Free-text notes about the processing run |
| `created_at` | TIMESTAMP | No | `CURRENT_TIMESTAMP` | Row creation timestamp |

**Constraints:**
- UNIQUE on `(year, month)`

**Indexes:**
- `idx_month_processed_year_month` on `(year, month)`

---

### Victim Filling, Proper Names and Entity Profiles

#### VictimFillActions (`dedup/victim_fill.py`)

One row per event given a victim by `fill-victims --apply`. Columns: `action_id`, `batch_id`, `deduplicated_event_id`,
`action` (`filled`), `new_victim`, `choice_probability` (the best candidate's final probability), `cutoff`,
`candidates_json` (name, support, sources), `replaced_json` (unusable victim roles demoted), `calibration_json`,
`reason`, `roles_before_json` (for reversal; the ledger snapshot holds the event), `member_signature`, `created_at`.

#### EntityProfiles / EntityProfileResearch (`dedup/entity_profiles.py`)

`EntityProfiles(entity_id, kind, value, source, evidence, created_at)`, unique on `(entity_id, kind, value)`.
`kind`: `jurisdiction`, `sector`, `type`, `administered_by`, `runs`, `holds_data`, `known_as`, `website`. `source`:
`human` (entered with `entity-profile set --reason`; never replaced by research) or `llm` (Perplexity; `evidence` holds
the citations). `EntityProfileResearch(entity_id, evidence_hash, prompt_version, model, status, citations_json,
researched_at)`: one lookup per organisation, again when `evidence_hash` (its incident titles) changes.

#### Other additions (2026-10-29)

* `DeduplicatedEvents.roles_prompt_version` - the role prompt that decided the event's roles (`entity-roles-v2`);
  an event on an older version with no victim is classified again.
* `EntityAliasDecisions.evidence_hash` - a hash of the incidents and profiles an alias question showed. A decision by
  another model is not reused. Since 2026-10-29 a model's decision of any verdict is asked again when the hash changes
  and the pair matters now (proposed for a fold, or barring one); a `human` decision never is; a row with a NULL hash
  counts as changed the first time it matters. The row it replaces goes to `EntityAliasDecisionHistory`.
* `ModelJudgements` rows with `question_id = 'is_proper_name'` (subject = the name; Noul) and
  `'event_named_organisations'` (`primitive = 'extraction'`; `answer` = the fast model's raw JSON list of possible
  victims and actors, validated against the text on every read), and the choices `fill_victim` /
  `fill_victim_escalated`.
* Calibration files `name_calibration.json` and `victim_fill_calibration.json` (same fields as above; the fill
  cut-off is the lowest best-candidate probability at which weighted precision met the target).
* Label files `name_labels.csv` (`name,label,rationale,labeller`; `named` / `descriptive` / `unsure`) and
  `victim_fill_labels.csv` (`deduplicated_event_id,label,correct_victim,rationale,labeller`; `named` /
  `none_named` / `unsure`); `review_acceptance_cases.csv` (`deduplicated_event_id,expectation,value,note,source`).

### Input History (what was known when) (`dedup/input_history.py`)

Append-only, written by SQLite triggers on the four watched tables (so every writer is covered), read by
`--as-known-at` (README, "Inputs as known at a date"). `known_at` is UTC `YYYY-MM-DD HH:MM:SS` from the database
clock. Column types are undeclared: a value is stored exactly as the watched table held it.

| Table | Watches | Key columns | Tracked columns |
|---|---|---|---|
| `EventStateHistory` | `DeduplicatedEvents` | `deduplicated_event_id` | `master_enriched_event_id`, `title`, `status`, `event_date`, `event_date_precision`, `event_date_earliest`, `event_date_latest`, `date_precision`, `date_basis`, `date_confidence`, `is_australian_event`, `is_specific_event`, `event_type`, `records_affected`, `victim_organization_name`, `vendor_organization_name`, `victim_organization_industry`, `confidence_score`, `total_data_sources` |
| `EventMembershipHistory` | `EventDeduplicationMap` | `map_id` | `raw_event_id`, `enriched_event_id`, `deduplicated_event_id` |
| `EventEntityLinkHistory` | `DeduplicatedEventEntities` | `deduplicated_event_id`, `entity_id` | `relationship_type`, `confidence_score`, `source_count` |
| `EntityStateHistory` | `EntitiesV2` | `entity_id` | `entity_name`, `entity_type`, `entity_kind`, `industry`, `is_australian`, `employee_count`, `turnover`, `size_estimate`, `size_confidence`, `size_employees`, `size_revenue_aud` |

Every history table also has `history_id` (INTEGER PK AUTOINCREMENT; the order of events), `change`
(`baseline` / `insert` / `update` / `delete`), `known_at` and `backfilled` (1 for `baseline` rows: the state was
already there when tracking began, so when it began to hold is unknown). A key's state at a time is its last row
with `known_at` at or before it; a `delete` row means it did not exist. A key that existed before tracking and is
first changed or deleted has its old state written as a `baseline` row first. Changing a key column (a link moved
to another entity) is a `delete` of the old key and an `insert` of the new.

`InputHistoryStatus(source PK, table_name, tracking_since, baselined_at, columns_json)`: one row per watched table;
`tracking_since` is when the triggers were first installed (a cutoff before it is reconstructed from the baseline),
`columns_json` maps each tracked column to when it began to be tracked (a column added later has no value for
earlier rows; the reconstruction uses the current value for them and marks it unknown).

`InputHistoryUnknown(unknown_id PK, source, table_name, key_json, columns_json, reason)`: written by the
reconstruction into its **working copy only** (never the live database): what cannot be placed at the cutoff.
`source` is the history source (`events`, `membership`, `links`, `entities`) or `ledger`; `key_json` the row's key
as `{column: value}` (NULL: the table as a whole, e.g. a ledger whose first row is after the cutoff);
`columns_json` the columns whose value is unknown (NULL: the row's existence and state). `reason` is one of the
`REASON_*` constants in `input_history.py`. `unknown_victim_rows()` turns it into the victim rows an analysis can
flag or exclude.

Ledger timestamps: written as UTC with an explicit offset (`utils/timestamps.utc_now_iso()`); rows written before
2026-10-30 have no offset and follow their writer's convention, declared in `input_history.LEDGERS` (UTC for
`EntityAliases.created_at` and `DedupOverrides.created_at`, local time for `ModelJudgements`,
`VictimAttributionActions`, `VictimFillActions`, `ContextualEntityAliases` and `EntityMentionResolutions`).

### Configuration Tables

#### DataSourcesV2

Registry of data sources with metadata and credibility scores.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `source_id` | INTEGER | **PK** | AUTOINCREMENT | Auto-incrementing primary key |
| `source_name` | VARCHAR(100) | NOT NULL, UNIQUE | — | Source name (e.g., "Perplexity AI Search") |
| `source_type` | VARCHAR(50) | Yes | NULL | Source category (e.g., "AI Search", "News Archive") |
| `base_url` | TEXT | Yes | NULL | Base URL of the source |
| `api_endpoint` | TEXT | Yes | NULL | API endpoint (if applicable) |
| `credibility_score` | REAL | Yes | NULL | Default credibility score (0.0–1.0) |
| `last_accessed` | TIMESTAMP | Yes | NULL | Last time this source was queried |
| `is_active` | BOOLEAN | No | `TRUE` | Whether this source is currently enabled |
| `created_at` | TIMESTAMP | No | `CURRENT_TIMESTAMP` | Row creation timestamp |

---

#### IndustryGroupings

Configurable groupings for categorizing entities by industry in visualizations.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `industry_group_id` | INTEGER | **PK** | AUTOINCREMENT | Auto-incrementing primary key |
| `group_name` | TEXT | NOT NULL, UNIQUE | — | Display name for the industry group |
| `keywords` | TEXT | NOT NULL | — | JSON array of keywords that map to this group |
| `display_order` | INTEGER | NOT NULL | — | Sort order for visualization display |
| `description` | TEXT | Yes | NULL | Description of the industry group |
| `created_at` | TEXT | No | `CURRENT_TIMESTAMP` | Row creation timestamp |
| `updated_at` | TEXT | No | `CURRENT_TIMESTAMP` | Last update timestamp (auto-updated via trigger) |

**Indexes:**
- `idx_industry_groupings_display_order` on `(display_order)`

**Triggers:**
- `update_industry_groupings_timestamp` — updates `updated_at` on UPDATE

---

### Views

#### EnrichmentMetrics

Daily aggregated enrichment pipeline performance metrics, grouped by date, pipeline version, and decision.

| Column | Source | Description |
|--------|--------|-------------|
| `date` | `DATE(created_at)` | Processing date |
| `pipeline_version` | `pipeline_version` | Pipeline version |
| `final_decision` | `final_decision` | Decision outcome |
| `event_count` | `COUNT(*)` | Number of events processed |
| `avg_confidence` | `AVG(final_confidence)` | Average confidence score |
| `avg_processing_time` | `AVG(total_time_seconds)` | Average processing time (seconds) |
| `avg_gpt4o_confidence` | `AVG(stage2_confidence)` | Average GPT-4o confidence |
| `avg_perplexity_confidence` | `AVG(stage3_verification_confidence)` | Average Perplexity verification confidence |
| `avg_validation_confidence` | `AVG(stage4_validation_confidence)` | Average validation confidence |
| `events_with_errors` | `SUM(CASE...)` | Count of events with validation errors |
| `events_with_warnings` | `SUM(CASE...)` | Count of events with validation warnings |
| `avg_factcheck_pass_rate` | `AVG(...)` | Average fact-check pass rate |

---

#### EnrichmentQualityReport

Summary report of enrichment quality metrics per pipeline version.

| Column | Source | Description |
|--------|--------|-------------|
| `pipeline_version` | — | Pipeline version |
| `total_events` | `COUNT(*)` | Total events processed |
| `auto_accept_count` | — | Events auto-accepted |
| `accept_warning_count` | — | Events accepted with warnings |
| `reject_count` | — | Events rejected |
| `error_count` | — | Events with errors |
| `avg_confidence` / `min_confidence` / `max_confidence` | — | Confidence distribution |
| `stage1_success_rate` | — | Content acquisition success % |
| `stage2_success_rate` | — | GPT-4o extraction success % |
| `stage4_success_rate` | — | Validation success % |
| `avg_factcheck_pass_rate` | — | Average fact-check pass % |
| `total_factchecks` / `total_factchecks_passed` | — | Fact-check totals |
| `total_validation_errors` / `total_validation_warnings` | — | Validation issue totals |
| `avg_processing_time` / `min_processing_time` / `max_processing_time` | — | Processing time distribution |
| `events_with_victim` | — | Events with identified victim organization |
| `specific_incident_count` | — | Events identified as specific incidents |
| `first_event` / `last_event` | — | Date range of processed events |

---

### Legacy Tables

These tables exist in the database from the V1 schema. They are renamed with `_v1_backup` suffix and are not used by current code.

| Table | Description |
|-------|-------------|
| `UniqueEvents_v1_backup` | V1 unique events table |
| `Entities_v1_backup` | V1 entities table |
| `EventEntities_v1_backup` | V1 event-entity junction |
| `DataSources_v1_backup` | V1 data sources registry |
| `EventSources_v1_backup` | V1 event-source mapping |
| `EventAttributeHistory_v1_backup` | V1 attribute change history |
| `EnrichedEvents_backup_20250927_211831` | Point-in-time backup of EnrichedEvents |

---

### Further tables

Tables and columns not described above, written from the code that creates and fills them. `scripts/build_data_dictionary.py` reads this file for the published `dashboard/data_dictionary.html`.

#### ConfidenceRepairs

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `table_name` | TEXT | **PK** | — | Table whose confidence value was cleared: `EnrichedEvents`, `EnrichedEventEntities` or `DeduplicatedEvents`. |
| `row_key` | TEXT | **PK** | — | Id of the cleared row in that table: the `enriched_event_id` or `deduplicated_event_id`, or `<enriched_event_id>\|<entity_id>` for an entity link. |
| `column_name` | TEXT | **PK** | — | Column that was set to NULL (`confidence_score`). |
| `old_value` | REAL | NOT NULL | — | The defaulted value that was cleared (`0.7`, `0.8` or `0.6`, or a cleared master's value copied to its event). |
| `rule` | TEXT | NOT NULL | — | The proof that the value was a code default, one of the fixed rule texts in `processing/confidence_defaults.py` (event, entity-link or deduplicated-event rule). |
| `repaired_at` | TEXT | NOT NULL | — | When the value was cleared (ISO timestamp, local time, to the second); written by `scripts/repair_confidence_defaults.py`. |

#### ContextualEntityAliases

One row per acronym and one organisation it can stand for: an alias that holds only where a record's own text supports it, so one acronym can list several organisations. Written by `dedup/contextual_aliases.py` (from `canonicalise-entities`, `dedup_admin.py alias add --contextual` and `EntityResolver.learn_alias`) and read when links are placed record by record (`resolve_links`) and at ingest.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `raw_name` | TEXT | **PK** | — | The acronym or initialism as written (every token upper case, at most 8 letters and digits). |
| `canonical_name` | TEXT | **PK** | — | Full name of one organisation the acronym can mean; a link moves to it only when the record's text names it or a model resolves the mention to it (`EntityMentionResolutions`). |
| `source` | TEXT | NOT NULL | — | Who made the alias: `human` (`dedup_admin.py alias add --contextual`), `llm` (a Perplexity-confirmed fold), or `rule` / `learned` when carried over from the `EntityAliases` row it replaced; written by `add_contextual_alias`. |
| `created_at` | TEXT | NOT NULL | — | When the row was written, ISO 8601: UTC with offset (`utc_now_iso`); older rows have no offset and are local time. |

#### CyberDailyRefetches

One row per stored Cyber Daily story re-fetched by `processing/cyberdaily_refetch.py` (`discovery_queue.py refetch-cyberdaily`, and every run with CyberDaily among the sources): the outcome of its last fetch and its retry state. Read by the same module to choose stories and back off, and by `discovery_queue.py status` and the quality report.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `raw_event_id` | TEXT | **PK** | — | The re-fetched record (`RawEvents.raw_event_id`). |
| `url` | TEXT | NOT NULL | — | Article URL fetched, with its query string and fragment removed (no tracking or subscriber token). |
| `status` | TEXT | NOT NULL | — | Outcome of the last fetch: `unchanged`, `updated` (new text stored), `kept_stored` (fetched page cut off by the free-article meter, stored article kept), `failed` or `missing` (HTTP 404/410). |
| `content_hash` | TEXT | Yes | — | SHA-256 of the fetched text with the free-article banner removed and whitespace collapsed; NULL when nothing was fetched. |
| `chars` | INTEGER | Yes | — | Length of the fetched text in characters; NULL when nothing was fetched. |
| `last_error` | TEXT | Yes | — | Message of the last failed or still-incomplete fetch; NULL after a clean fetch. |
| `failures` | INTEGER | NOT NULL | `0` | Consecutive failed or still-incomplete fetches; a throttled answer does not count, and the story is given up at 5. |
| `fetched_at` | TEXT | NOT NULL | — | When the story was last fetched (ISO timestamp, local time). |
| `error_type` | TEXT | Yes | — | Failure type of the last fetch from `processing/fetch_errors.py` (`throttled`, `missing`, `network`, `parse`, `incomplete`, `other`, `unknown`); NULL once complete. |
| `next_attempt_at` | TEXT | Yes | — | The story is not fetched again before this (ISO timestamp, local time): back-off of 1 day doubling to 30 days, or 2 hours after a throttled answer. |
| `gave_up_at` | TEXT | Yes | — | When the story was given up (ISO timestamp, local time): on a 404/410 or after 5 consecutive failures; `discovery_queue.py release` returns it to the queue. |

#### DataSources_v1_backup

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `source_id` | INTEGER | **PK** | — | Integer key of a V1 data source, referenced by `EventSources_v1_backup.source_id` (V1 schema, not used by current code). |
| `source_name` | VARCHAR(100) | NOT NULL | — | Name of the data source, unique; read by the V1 summary statistics, written by no V1 code in git history (V1 schema, not used by current code). |
| `source_type` | VARCHAR(50) | Yes | — | Category of the data source; its current counterpart is `RawEvents.source_type` (V1 schema, not used by current code). |

#### DedupCalibration

One row per run of `dedup_admin.py learn` over at least 5 active overrides: the auto-merge threshold nudged towards the error people corrected more often. Written by `DedupLedger.learn_from_overrides` (`dedup/ledger.py`), which reads the newest row as the current threshold (0.85 when the table is empty).

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `calibration_id` | TEXT | **PK** | — | Row identifier (UUID); written by `DedupLedger.learn_from_overrides`. |
| `created_at` | TIMESTAMP | Yes | `CURRENT_TIMESTAMP` | When the calibration was recorded (SQLite `CURRENT_TIMESTAMP`, UTC); the newest row is the current threshold. |
| `sample_size` | INTEGER | NOT NULL | — | Number of active rows in `DedupOverrides` the calibration was computed from (written only when at least 5). |
| `false_merges` | INTEGER | NOT NULL | — | Active overrides with verdict `different`: pairs a person said the pipeline should not have joined. |
| `missed_merges` | INTEGER | NOT NULL | — | Active overrides with verdict `same`: pairs a person said the pipeline should have joined. |
| `merge_threshold` | REAL | NOT NULL | — | The resulting auto-merge certainty threshold (0-1): the previous one plus 0.02 when false merges outnumber missed ones (at most 0.97), minus 0.02 when fewer (at least 0.60). |
| `notes` | TEXT | Yes | — | Free-text note; currently always `auto-calibrated from human overrides`. |

#### DedupDecisions

Append-only ledger of deduplication decisions: one row per record per merge, split or pair verdict, with who decided, how certain and why. Written by `DedupLedger.record_decision` (`dedup/ledger.py`) for `dedup_admin.py` merges, splits and adjudications; read by `DedupLedger.ancestry`, `scripts/build_dedup_dashboard.py` and the supplier steps.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `decision_id` | TEXT | **PK** | — | Row identifier (UUID). |
| `batch_id` | TEXT | NOT NULL | — | Identifier of the run or command that made the decision, e.g. `merge-YYYYMMDDHHMMSS` or `revert-YYYYMMDDHHMMSS`; rows of one operation share it. |
| `enriched_event_id` | TEXT | NOT NULL | — | The record (`EnrichedEvents`) the decision is about: the record that joined or left an event, or the right-hand record of a pair verdict. |
| `deduplicated_event_id` | TEXT | Yes | — | The event (`DeduplicatedEvents`) the record was placed in or left; NULL for a pair verdict recorded without one. |
| `cluster_key` | TEXT | Yes | — | For a pair verdict, the two enriched event ids sorted and joined by `\|` (`ledger.pair_key`); NULL for merges and splits. |
| `action` | TEXT | NOT NULL | — | What was decided: `merge`, `keep_separate` or `revert` (a split that undid an earlier merge). |
| `decided_by` | TEXT | NOT NULL | — | Who decided: `rule`, `embedding`, `llm` or `human`. |
| `certainty` | REAL | Yes | — | The decider's certainty (0-1), e.g. the adjudicator's probability; NULL when the decider gave none. |
| `method` | TEXT | Yes | — | The procedure that produced the decision, e.g. `manual-merge`, `manual-split`, `manual-split-reattach`, `legacy-review`, `band-resolve (...)` or `<decided_by>-adjudication`. |
| `reasoning` | TEXT | Yes | — | The decider's written reason (the adjudicator's rationale or the command's `--reason`). |
| `evidence_json` | TEXT | Yes | — | The `MatchEvidence` the decision rested on, as JSON (`dedup/models.py`): entity names and canonical keys, dates and gap in days, titles, embedding and title similarity, shared URLs, distinguishing facts. |
| `superseded_by` | TEXT | Yes | — | NULL while the decision applies; set to a fresh marker UUID by `DedupLedger._supersede_decisions` when a later merge or split replaces it (it does not point to another row). |
| `created_at` | TIMESTAMP | Yes | `CURRENT_TIMESTAMP` | When the decision was recorded (ISO 8601 local time from `datetime.now()`, no offset). |

#### DedupOverrides

One row per ruling that two records are, or are not, the same incident, keyed on the stable enriched event ids so it survives rebuilds. Written by `DedupLedger.add_override` (`dedup_admin.py override`, and every split, which rules the departing records `different` from those left); read by `merge_events`, which refuses to join events across an active `different` ruling, and by the duplicate, supplier and label-repair steps.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `override_id` | TEXT | **PK** | — | Row identifier (UUID). |
| `pair_key` | TEXT | NOT NULL | — | The two enriched event ids sorted and joined by `\|` (`ledger.pair_key`); unique, so a new ruling on a pair replaces the old one. |
| `left_enriched_event_id` | TEXT | NOT NULL | — | One record of the pair (`EnrichedEvents.enriched_event_id`). |
| `right_enriched_event_id` | TEXT | NOT NULL | — | The other record of the pair (`EnrichedEvents.enriched_event_id`). |
| `verdict` | TEXT | NOT NULL | — | The ruling: `same` (one incident) or `different` (separate incidents). |
| `reason` | TEXT | Yes | — | Why the ruling was made (the command's `--reason`, prefixed `split:` for a split); a deactivation appends a `[void: ...]` note saying why. |
| `created_by` | TEXT | Yes | `'human'` | Who made it: `human` (a person's command) or `rule` (an automated split, e.g. by the coherence or supplier steps). |
| `created_at` | TIMESTAMP | Yes | `CURRENT_TIMESTAMP` | When the ruling was made or last replaced (UTC; `utils/timestamps.utc_now_iso` with offset, older rows SQLite `CURRENT_TIMESTAMP`). |
| `active` | INTEGER | NOT NULL | `1` | 1 while the ruling applies, 0 once deactivated by `dedup/split_rulings.py` or `dedup/label_contradictions.py` because its basis no longer holds; inactive rows are kept for audit. |

#### DedupSnapshots

One row per event captured just before a ledger operation changed it (merge, split, rejection, victim change), so the change can be undone without a rebuild. Written by `DedupLedger.snapshot_event` (`dedup/ledger.py`) and read by `DedupLedger.restore_snapshot`, the victim-fill reversal and the quality regression checks.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `snapshot_id` | TEXT | **PK** | — | Row identifier (UUID). |
| `batch_id` | TEXT | NOT NULL | — | The operation the snapshot was taken for, e.g. `merge-...`, `revert-...`, `victim-attribution-...`, `victim-fill-...`, `reject-event-reality-...`; shared by every event that operation changed. |
| `deduplicated_event_id` | TEXT | NOT NULL | — | The event (`DeduplicatedEvents`) that was captured. |
| `payload_json` | TEXT | NOT NULL | — | JSON `{"event": <the DeduplicatedEvents row>, "members": [<its EventDeduplicationMap rows>]}` as they were before the change. |
| `created_at` | TIMESTAMP | Yes | `CURRENT_TIMESTAMP` | When the snapshot was taken (SQLite `CURRENT_TIMESTAMP`, UTC). |

#### DescriptiveNames

One row per name that describes an organisation instead of naming one; a listed name is never a victim, a fold target or a victim candidate. Written by `dedup/descriptive_names.py` (`dedup_admin.py descriptive-name add` and `descriptive-name sync`) and read through `name_judgement.is_descriptive_name` by role classification, victim fill, alias learning and victim counting.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `name_key` | TEXT | **PK** | — | The name in lower case with single spaces (`descriptive_names.name_key`); names are matched on it. |
| `name` | TEXT | NOT NULL | — | The name as written. |
| `source` | TEXT | NOT NULL | — | Where the entry came from: `human` (`dedup_admin.py descriptive-name add`; never replaced or removed by a model), `label` (labelled `descriptive` in `evaluation/name_labels.csv`) or `llm` (scored below the calibrated proper-name cut-off); `sync` removes `label` and `llm` rows no longer supported. |
| `reason` | TEXT | Yes | — | Why the name is listed: the person's reason, the label's rationale, or for `llm` the probability against the calibrated cut-off. |
| `probability` | REAL | Yes | — | For `llm` rows, Jev's P(the name identifies one specific organisation) from the proper-name judgement (`dedup/name_judgement.py`); NULL otherwise. |
| `model` | TEXT | Yes | — | For `llm` rows, the TypeSafe model that judged the name (e.g. `jev-1.14.0`); NULL otherwise. |
| `created_at` | TEXT | NOT NULL | — | When the entry was added, ISO 8601, UTC with offset. |

#### DescriptiveVictimDemotions

Audit of victims taken off Active events because the name describes an organisation instead of naming one (listed in `DescriptiveNames`). Written by `descriptive_names.demote_victims`, run by `dedup_admin.py fill-victims` before it fills the event with the organisation its evidence names.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `demotion_id` | INTEGER | **PK** | — | Row identifier (auto-increment integer). |
| `deduplicated_event_id` | TEXT | NOT NULL | — | The event the description was a victim of. |
| `entity_id` | INTEGER | Yes | — | The `EntitiesV2` entity whose `victim` role on the event was changed to `affected`; NULL when only the scalar victim column held the description. |
| `name` | TEXT | NOT NULL | — | The descriptive name that was demoted, as stored. |
| `scalar_cleared` | INTEGER | NOT NULL | `0` | 1 when `DeduplicatedEvents.victim_organization_name` was the description and was set to NULL, else 0. |
| `reason` | TEXT | Yes | — | Why it was demoted; currently always `describes an organisation instead of naming it (DescriptiveNames)`. |
| `created_at` | TEXT | NOT NULL | — | When the demotion was made (UTC, ISO 8601 with offset, `utils/timestamps.utc_now_iso`). |

#### EnrichmentFactSupport

One row per record and Perplexity enrichment fact: whether the acceptance gate stored the fact because a source supports it, or withheld it, with the source and the reason. Written by `processing/enrichment_gate.py` through `storage/enrichment_support.py` on enrichment, and by `perplexity_backfill_events.py --recompute-reliability` and `refresh-cited-documents`; read by those re-checks and the retry queue.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `enriched_event_id` | TEXT | **PK** | — | The enriched record the fact belongs to (`EnrichedEvents.enriched_event_id`). |
| `fact` | TEXT | **PK** | — | Fact group: `date`, `entity`, `threat_actor`, `attack_method`, `victim_count` or `industry`. |
| `status` | TEXT | NOT NULL | — | `supported` (stored in the typed columns and enrichment JSON) or `withheld` (no source supports it; kept only here). |
| `value_json` | TEXT | Yes | — | The value Perplexity claimed, as JSON (dates as ISO text). |
| `confidence` | REAL | Yes | — | Perplexity's confidence for the fact (0-1), e.g. `date_confidence`; NULL for `industry`, which has none. |
| `source_kind` | TEXT | Yes | — | Where a supported fact was found: `record` (the record's own text), `cited_document` (a cited page) or `entity` (industry, supported through the supported victim); NULL when withheld with no source. |
| `source_url` | TEXT | Yes | — | URL of the cited page that supports the fact; NULL for record support or when withheld. |
| `reason` | TEXT | Yes | — | Why the fact was withheld (e.g. no source states it); NULL when supported. |
| `details_json` | TEXT | Yes | — | How the source states the fact, as JSON: a date's `precision` and `basis`, a count's `unit`, `basis` and `scope`; NULL for other facts. |
| `checked_at` | TEXT | NOT NULL | — | When the fact was last judged (ISO timestamp, local time). |

#### Entities_v1_backup

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `entity_id` | INTEGER | **PK** | — | Integer key of a V1 entity, referenced by `EventEntities_v1_backup.entity_id` (V1 schema, not used by current code). |
| `entity_name` | VARCHAR(255) | NOT NULL | — | Organisation name extracted by V1 enrichment, unique; inserted by the V1 `cyber_event_data/library.py` (V1 schema, not used by current code). |
| `entity_type` | VARCHAR(50) | Yes | — | Kind of entity; written by no V1 code in git history, its current counterpart is `EntitiesV2.entity_type` (V1 schema, not used by current code). |
| `industry` | VARCHAR(100) | Yes | — | Industry of the entity, read by the V1 summary statistics; written by no V1 code in git history (V1 schema, not used by current code). |
| `turnover` | VARCHAR(50) | Yes | — | Annual turnover as text; written by no V1 code in git history (V1 schema, not used by current code). |
| `employee_count` | INT | Yes | — | Number of employees; written by no V1 code in git history (V1 schema, not used by current code). |

#### EntityAliasDecisionHistory

Alias decisions that a re-ask on changed evidence replaced: one row per superseded `EntityAliasDecisions` row, with `superseded_at` and `superseded_by` (the new verdict). Written by `dedup/entity_alias_verification.py`.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `name` | TEXT | NOT NULL | — | Alphabetically first name of the pair, as in `EntityAliasDecisions.name`. |
| `other_name` | TEXT | NOT NULL | — | The other name of the pair. |
| `verdict` | TEXT | NOT NULL | — | The replaced verdict: `same`, `parent_subsidiary`, `related`, `different` or `unsure`. |
| `canonical_name` | TEXT | Yes | — | The organisation's usual name given by the replaced decision, when its verdict was `same`; NULL otherwise. |
| `reason` | TEXT | Yes | — | The reason given for the replaced verdict. |
| `citations_json` | TEXT | Yes | — | JSON list of the URLs cited for the replaced verdict. |
| `model` | TEXT | NOT NULL | — | Who made the replaced decision: a Perplexity model (e.g. `sonar-pro`) or `rule`; a person's (`human`) decision is never replaced. |
| `prompt_version` | TEXT | NOT NULL | — | Question version of the replaced decision (e.g. `entity-alias-v1`). |
| `decided_at` | TEXT | NOT NULL | — | When the replaced decision was made, ISO 8601 local time. |
| `superseded_at` | TEXT | NOT NULL | — | When `entity_alias_verification.verify_pair` asked the pair again on changed evidence and replaced the decision, ISO 8601 local time. |
| `superseded_by` | TEXT | Yes | — | The new verdict that replaced it. |

#### EntityAliasDecisions

One row per pair of entity names and question version: the current verdict on whether the two names are one organisation, from Perplexity, a person or the unmerge rule. Written by `dedup/entity_alias_verification.py` (`canonicalise-entities`, alias learning, anti-alias learning, supplier customer pairs), `dedup_admin.py alias add` and `entity_merge_ledger.unmerge_entity`; read before every fold, by `anti_aliases.sync_from_verdicts` and by victim counting.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `name` | TEXT | **PK** | — | One name of the pair, the alphabetically first, so each pair is stored once. |
| `other_name` | TEXT | **PK** | — | The other name of the pair. |
| `verdict` | TEXT | NOT NULL | — | Whether the names are one organisation: `same` (one legal entity, or its trading name, abbreviation, acronym or former name; only this folds), `parent_subsidiary`, `related`, `different` or `unsure`. |
| `canonical_name` | TEXT | Yes | — | The organisation's usual name when the verdict is `same`; NULL otherwise. |
| `reason` | TEXT | Yes | — | Why: Perplexity's explanation, the reason a person entered, or `verdict changed to ...` from an unmerge. |
| `citations_json` | TEXT | Yes | — | JSON list of the URLs Perplexity cited; `[]` for an unmerge's verdict, NULL for a person's alias. |
| `model` | TEXT | NOT NULL | — | Who decided: the Perplexity model (e.g. `sonar-pro`), `human` (`dedup_admin.py alias add` or `unmerge-entity`; never re-asked, outranks any model) or `rule` (the `different` recorded when a fold is reversed automatically, which a later verification may overturn). |
| `prompt_version` | TEXT | **PK** | — | Version of the verification question (`entity-alias-v1`), or `human` for a person's alias. |
| `decided_at` | TEXT | NOT NULL | — | When the decision was made, ISO 8601 (local time for model and rule decisions, UTC with offset for `alias add`). |

#### EntityAliases

One row per name variant that resolves to an organisation's canonical name everywhere (a global alias). Written by `EntityResolver.learn_alias` (`dedup/entity_resolution.py`, from `canonicalise-entities`, `dedup_admin.py alias add` and `dedup_admin.py learn`) and `entity_alias_verification.py`; read by entity resolution at ingest, victim counting and fold proposals.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `alias_id` | INTEGER | **PK** | — | Integer key. |
| `raw_name` | TEXT | NOT NULL | — | The variant name as written, unique. |
| `canonical_name` | TEXT | NOT NULL | — | The organisation's canonical name the variant resolves to. |
| `canonical_key` | TEXT | NOT NULL | — | `entity_resolution.canonical_key(canonical_name)`: normalised (lower case, no punctuation or accents) with trailing legal suffixes removed. |
| `source` | TEXT | NOT NULL | `'rule'` | Who decided: `human` (`dedup_admin.py alias add` or a pair in `EXPLICIT_ALIASES`), `llm` (confirmed `same` by Perplexity), `learned` (a person's `same` ruling between two records, `dedup_admin.py learn`) or `rule` (from name structure alone; `canonicalise-entities` puts each to Perplexity, making it `llm` or deleting it, and keeps pairs that differ only in spelling). |
| `confidence` | REAL | Yes | — | Confidence in the alias, 0 to 1; written as `1.0` by every current path. |
| `created_at` | TIMESTAMP | Yes | `CURRENT_TIMESTAMP` | When the alias was added or last changed target or source, ISO 8601: UTC with offset; older rows are `CURRENT_TIMESTAMP` (UTC, no offset). |

#### EntityAntiAliases

One row per pair of names that look alike but are different entities; every path that joins two names (victim counting, fold verification, alias lookup, fold reversal) checks it first. Written by `dedup/anti_aliases.py` (`sync_from_verdicts` and `learn_from_counting` in `canonicalise-entities`, and `dedup_admin.py anti-alias add`).

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `name_key_a` | TEXT | **PK** | — | `normalize_name` of the first name; the pair is stored sorted on these keys. |
| `name_key_b` | TEXT | **PK** | — | `normalize_name` of the second name. |
| `name_a` | TEXT | NOT NULL | — | The first name as written. |
| `name_b` | TEXT | NOT NULL | — | The second name as written. |
| `relation` | TEXT | NOT NULL | — | How the two differ: `different`, `related` or `parent_subsidiary`. |
| `source` | TEXT | NOT NULL | — | Who listed the pair: `human` (`dedup_admin.py anti-alias add`; never replaced or removed by a model) or `llm` (a stored Perplexity verdict in `EntityAliasDecisions`; removed if a later verdict is `same`). |
| `reason` | TEXT | Yes | — | The person's reason, or the reason of the verdict listed. |
| `citations_json` | TEXT | Yes | — | JSON list of cited URLs; `[]` for entries listed from stored verdicts, whose citations stay in `EntityAliasDecisions`. |
| `model` | TEXT | Yes | — | For `llm` rows, the model whose verdict is listed (e.g. `sonar-pro`); NULL for a person's entry. |
| `created_at` | TEXT | NOT NULL | — | When the entry was written, ISO 8601, UTC with offset. |

#### EntityMentionResolutions

One row per entity link and acronym: which organisation a contextual acronym on one record or event resolves to, and how. Written by `contextual_aliases.resolve_links` (run by `canonicalise-entities`) and read by later runs, victim counting and `EntityMerges` (which copies the rows naming a dropped entity).

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `link_table` | TEXT | **PK** | — | The link table the mention belongs to: `EnrichedEventEntities` or `DeduplicatedEventEntities`. |
| `subject_id` | TEXT | **PK** | — | The record or event the link belongs to: an `enriched_event_id` or a `deduplicated_event_id`, according to `link_table`. |
| `raw_name` | TEXT | **PK** | — | The acronym as written (a `ContextualEntityAliases.raw_name`). |
| `canonical_name` | TEXT | Yes | — | The organisation the mention resolved to; NULL when unresolved or when a model was confident it means none of the candidates. |
| `method` | TEXT | NOT NULL | — | How it was decided: `named in the text` (the record names one candidate in full, no model), `jev` (Jev chose at 0.9 or above), `escalated` (the reasoning OpenAI model chose at 0.9 or above) or `unresolved`. |
| `probability` | REAL | Yes | — | Probability of the leading answer (1.0 for `named in the text`); for `unresolved`, the escalation's best probability, below 0.9. |
| `moved_from` | TEXT | Yes | — | Name of the entity the link was on before it moved; NULL when the link did not move. |
| `decided_at` | TEXT | NOT NULL | — | When the decision was recorded, ISO 8601: UTC with offset; older rows have no offset and are local time. |

#### EntityMerges

One row per fold of one `EntitiesV2` entity into another, holding everything needed to undo it. Written by `entity_merge_ledger.merge_pair` (from `merge_entities` in `canonicalise-entities`) and read by `dedup_admin.py entity-merges` and `unmerge-entity` and by `reverse_overturned_merges`, which undoes model-made folds whose verdict has changed.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `merge_id` | TEXT | **PK** | — | Key: `em-` followed by 12 hex characters. |
| `kept_entity_id` | INTEGER | NOT NULL | — | `EntitiesV2.entity_id` of the entity that remains. |
| `kept_name` | TEXT | NOT NULL | — | Name of the kept entity. |
| `dropped_entity_id` | INTEGER | NOT NULL | — | `EntitiesV2.entity_id` of the entity folded away; `unmerge-entity` restores it with this id. |
| `dropped_name` | TEXT | NOT NULL | — | Name of the dropped entity. |
| `source` | TEXT | NOT NULL | — | Who decided the alias the fold applied: `human` (a reviewed alias), `llm` (Perplexity-confirmed) or `rule` (a stored structural alias); `human` folds are never reversed by a model. |
| `decision_json` | TEXT | Yes | — | The `EntityAliasDecisions` verdict the fold rested on, as JSON. |
| `entity_row_json` | TEXT | NOT NULL | — | The dropped `EntitiesV2` row, every column, as JSON. |
| `kept_before_json` | TEXT | NOT NULL | — | The kept `EntitiesV2` row before the fold, as JSON. |
| `inherited_json` | TEXT | Yes | — | Attributes the kept row took from the dropped row (its blanks only): `{column: {"before": ..., "after": ...}}`. |
| `links_json` | TEXT | NOT NULL | — | Every link the dropped entity held in `EnrichedEventEntities` and `DeduplicatedEventEntities`, with what became of it: `moved`, or `collided` with a link the kept entity already held (both original rows kept). |
| `scalar_json` | TEXT | Yes | — | Events whose `victim_organization_name` or `vendor_organization_name` was renamed to the kept name. |
| `mentions_json` | TEXT | Yes | — | The `EntityMentionResolutions` rows naming the dropped entity. |
| `roles_json` | TEXT | Yes | — | Roles the dropped entity held: `{table: {role: count}}`. |
| `source_ids_json` | TEXT | Yes | — | The enriched, raw and deduplicated event ids of the dropped entity's links. |
| `references_json` | TEXT | Yes | — | Other tables still keyed by the dropped id, left in place: `{table: row count}`. |
| `alias_json` | TEXT | Yes | — | The `EntityAliases` row for `dropped_name` at fold time. |
| `status` | TEXT | NOT NULL | `'applied'` | `applied`, or `reversed` once `unmerge-entity` or an automatic reversal has undone it. |
| `merged_at` | TEXT | NOT NULL | — | When the fold was made, ISO 8601 local time. |
| `reversed_at` | TEXT | Yes | — | When the fold was undone, ISO 8601 local time; NULL while applied. |
| `reversed_reason` | TEXT | Yes | — | Why it was undone: the reason given to `unmerge-entity`, or `verdict changed to ...` for an automatic reversal. |

#### EntityProfileResearch

One row per entity: when its profile was last looked up and on what evidence, so a lookup is repeated only when that evidence changes. Written by `entity_profiles.research_profile` (`dedup_admin.py entity-profile research`, `find-supplier-victims --research`) and read by `entity_profiles.pending`.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `entity_id` | INTEGER | **PK** | — | `EntitiesV2.entity_id` of the organisation looked up. |
| `evidence_hash` | TEXT | NOT NULL | — | First 16 hex characters of a SHA-256 of up to three incident titles the organisation is linked to, the prompt version and the model (`entity_profiles.evidence_hash`). |
| `prompt_version` | TEXT | NOT NULL | — | Version of the lookup question (`entity-profile-v1`). |
| `model` | TEXT | NOT NULL | — | The Perplexity model that answered (e.g. `sonar-pro`). |
| `status` | TEXT | NOT NULL | — | `researched` (attributes stored in `EntityProfiles`) or `unavailable` (no readable answer; looked up again next run). |
| `citations_json` | TEXT | Yes | — | JSON list of the URLs Perplexity cited; NULL when `unavailable`. |
| `researched_at` | TEXT | NOT NULL | — | When the lookup ran, ISO 8601 local time. |

#### EntityProfiles

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `profile_id` | INTEGER | **PK** | — | Integer key. |
| `entity_id` | INTEGER | NOT NULL | — | `EntitiesV2.entity_id` the attribute describes. |
| `kind` | TEXT | NOT NULL | — | The attribute: `jurisdiction`, `sector`, `type`, `administered_by`, `runs`, `holds_data`, `known_as` or `website`; `jurisdiction`, `type`, `administered_by` and `website` hold one value per entity when set by a person. |
| `value` | TEXT | NOT NULL | — | The attribute's value as text, e.g. a country and state, an industry, or a service the organisation runs. |
| `source` | TEXT | NOT NULL | — | `llm` (from a Perplexity lookup, `entity_profiles.research_profile`) or `human` (`dedup_admin.py entity-profile set`; never replaced or removed by a lookup). |
| `evidence` | TEXT | Yes | — | For `llm`, the JSON list of URLs the lookup cited; for `human`, the reason entered. |
| `created_at` | TEXT | NOT NULL | — | When the attribute was stored, ISO 8601 local time. |

#### EntityStateHistory

Append-only history of `EntitiesV2`: one row per change to a tracked column, written by SQLite triggers (`dedup/input_history.py`) so every writer is covered. Read by the analyses' `--as-known-at` reconstruction.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `history_id` | INTEGER | **PK** | — | Order of the changes (INTEGER, autoincrement); the row with the highest id at or before a time is the state then. |
| `change` | TEXT | NOT NULL | — | `baseline` (the state when tracking began, its start unknown), `insert`, `update` or `delete` (the key did not exist from then on). |
| `known_at` | TEXT | NOT NULL | — | When the change was recorded, UTC `YYYY-MM-DD HH:MM:SS` from the database clock; written by the trigger. |
| `backfilled` | INTEGER | NOT NULL | `0` | 1 for `baseline` rows: the state already held when tracking began, so when it began to hold is unknown; 0 otherwise. |

#### EventAttributeHistory_v1_backup

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `history_id` | TEXT | **PK** | — | Text key of one recorded change to a V1 event's attribute; no code in git history writes the table (V1 schema, not used by current code). |
| `event_id` | TEXT | Yes | — | The V1 event changed (`UniqueEvents_v1_backup.event_id`) (V1 schema, not used by current code). |
| `event_source_id` | TEXT | Yes | — | The V1 source behind the change (`EventSources_v1_backup.event_source_id`); NULL when none (V1 schema, not used by current code). |
| `attribute_name` | VARCHAR(50) | NOT NULL | — | Name of the event column that changed (V1 schema, not used by current code). |
| `old_value` | TEXT | Yes | — | Value before the change, as text (V1 schema, not used by current code). |
| `new_value` | TEXT | Yes | — | Value after the change, as text (V1 schema, not used by current code). |
| `change_date` | TIMESTAMP | Yes | `CURRENT_TIMESTAMP` | When the change was recorded; defaults to `CURRENT_TIMESTAMP` (UTC) (V1 schema, not used by current code). |

#### EventCorroborations

One row per event and Perplexity corroboration search asking whether credible public reporting of the incident exists, stored because a search cannot be replayed. Written by `dedup/corroboration.py` during `dedup_admin.py check-events` for events the screen scores below 0.5; read by the event-reality judgement.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `deduplicated_event_id` | TEXT | **PK** | — | The event searched for (`DeduplicatedEvents.deduplicated_event_id`). |
| `query_hash` | TEXT | **PK** | — | SHA-256 of the model and the query; a new hash means a new search. |
| `query` | TEXT | NOT NULL | — | The prompt sent to Perplexity, built from the event's title, victim, date and records. |
| `model` | TEXT | NOT NULL | — | Perplexity model that answered (`PERPLEXITY_MODEL`, default `sonar-pro`). |
| `answer` | TEXT | Yes | — | Perplexity's answer in the `FOUND` / `INCIDENT` / `SOURCES` format. |
| `citations_json` | TEXT | Yes | — | JSON list of the citation URLs Perplexity returned. |
| `created_at` | TEXT | NOT NULL | — | When the search was made (ISO timestamp, local time). |

#### EventDescriptions

One row per generated description of an incident and the evidence it was written from: AI incidents for the AI incident report, and supplier incidents for the supplier report. Written by `processing/ai_involvement.py` (`scripts/classify_ai_involvement.py`) and `dedup/multi_customer_incidents.py`; read by `build_ai_incident_report.py`, `build_supplier_incidents_report.py` and the main dashboard.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `deduplicated_event_id` | TEXT | **PK** | — | The described event (`DeduplicatedEvents.deduplicated_event_id`), or the supplier incident id (`si-...`, `SupplierIncidents`) for purpose `supplier-incident`. |
| `purpose` | TEXT | **PK** | — | What the description is for: `ai_incident_report` or `supplier-incident`. |
| `prompt_version` | TEXT | NOT NULL | — | Version of the description prompt, e.g. `ai-description-v1` or `supplier-incident-description-v1`. |
| `input_hash` | TEXT | **PK** | — | SHA-256 of the evidence, prompt and model; a description is current only while its evidence gives the same hash. |
| `model` | TEXT | NOT NULL | — | OpenAI model that wrote it (the fast model, `OPENAI_FAST_MODEL`). |
| `description` | TEXT | NOT NULL | — | The 2-4 sentence description. |
| `created_at` | TEXT | NOT NULL | — | When it was written (ISO timestamp, local time). |

#### EventEntities_v1_backup

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `event_id` | TEXT | **PK** | — | V1 event the entity was named in (`UniqueEvents_v1_backup.event_id`); written by the V1 `update_event_with_enriched_data` (V1 schema, not used by current code). |
| `entity_id` | INTEGER | **PK** | — | The V1 entity (`Entities_v1_backup.entity_id`) (V1 schema, not used by current code). |

#### EventEntityLinkHistory

Append-only history of `DeduplicatedEventEntities`: one row per change to a tracked column, written by SQLite triggers (`dedup/input_history.py`) so every writer is covered. Read by the analyses' `--as-known-at` reconstruction.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `history_id` | INTEGER | **PK** | — | Order of the changes (INTEGER, autoincrement); the row with the highest id at or before a time is the state then. |
| `change` | TEXT | NOT NULL | — | `baseline` (the state when tracking began, its start unknown), `insert`, `update` or `delete` (the key did not exist from then on). |
| `known_at` | TEXT | NOT NULL | — | When the change was recorded, UTC `YYYY-MM-DD HH:MM:SS` from the database clock; written by the trigger. |
| `backfilled` | INTEGER | NOT NULL | `0` | 1 for `baseline` rows: the state already held when tracking began, so when it began to hold is unknown; 0 otherwise. |

#### EventFactOverrides

One row per explicit correction of an event fact, applied instead of the value computed from the event's members. Written by `set_fact` in `dedup/event_facts.py` (`dedup_admin.py set-fact`); read by `refresh_facts` whenever facts are recomputed.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `deduplicated_event_id` | TEXT | **PK** | — | The corrected event (`DeduplicatedEvents.deduplicated_event_id`). |
| `field` | TEXT | **PK** | — | Corrected fact: `records_affected`, `event_type`, `severity`, `attack_method` or `attacking_entity_name`. |
| `value` | TEXT | Yes | — | The value to use, as text; NULL clears the fact. |
| `reason` | TEXT | NOT NULL | — | Why the correction was made. |
| `created_by` | TEXT | NOT NULL | `'human'` | Who made it (default `human`). |
| `created_at` | TEXT | NOT NULL | — | When it was recorded (ISO timestamp, local time). |

#### EventMembershipHistory

Append-only history of `EventDeduplicationMap`: one row per change to a tracked column, written by SQLite triggers (`dedup/input_history.py`) so every writer is covered. Read by the analyses' `--as-known-at` reconstruction.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `history_id` | INTEGER | **PK** | — | Order of the changes (INTEGER, autoincrement); the row with the highest id at or before a time is the state then. |
| `change` | TEXT | NOT NULL | — | `baseline` (the state when tracking began, its start unknown), `insert`, `update` or `delete` (the key did not exist from then on). |
| `known_at` | TEXT | NOT NULL | — | When the change was recorded, UTC `YYYY-MM-DD HH:MM:SS` from the database clock; written by the trigger. |
| `backfilled` | INTEGER | NOT NULL | `0` | 1 for `baseline` rows: the state already held when tracking began, so when it began to hold is unknown; 0 otherwise. |

#### EventSources_v1_backup

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `event_source_id` | TEXT | **PK** | — | UUID key of one source URL of a V1 event (V1 schema, not used by current code). |
| `event_id` | TEXT | Yes | — | The V1 event the URL reports (`UniqueEvents_v1_backup.event_id`) (V1 schema, not used by current code). |
| `source_id` | INTEGER | Yes | — | The V1 data source (`DataSources_v1_backup.source_id`); never set by the V1 library (V1 schema, not used by current code). |
| `url` | VARCHAR(2048) | Yes | — | Article URL, unique; written by the V1 `add_event` (V1 schema, not used by current code). |
| `published_date` | TIMESTAMP | Yes | — | Publication date of the article; not written by the V1 library (V1 schema, not used by current code). |
| `retrieved_date` | TIMESTAMP | Yes | `CURRENT_TIMESTAMP` | When the URL was stored; defaults to `CURRENT_TIMESTAMP` (UTC) (V1 schema, not used by current code). |
| `raw_title` | TEXT | Yes | — | Article title as scraped; carried into `RawEvents.raw_title` by the V1-to-V2 migration (`database_migration_v2.py`) (V1 schema, not used by current code). |
| `raw_content` | TEXT | Yes | — | Scraped article text (V1 schema, not used by current code). |
| `is_enriched` | BOOLEAN | Yes | `FALSE` | 1 once V1 enrichment had processed the URL (`mark_url_as_processed`); migrated to `RawEvents.is_processed` (V1 schema, not used by current code). |

#### EventStateHistory

Append-only history of `DeduplicatedEvents`: one row per change to a tracked column, written by SQLite triggers (`dedup/input_history.py`) so every writer is covered. Read by the analyses' `--as-known-at` reconstruction.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `history_id` | INTEGER | **PK** | — | Order of the changes (INTEGER, autoincrement); the row with the highest id at or before a time is the state then. |
| `change` | TEXT | NOT NULL | — | `baseline` (the state when tracking began, its start unknown), `insert`, `update` or `delete` (the key did not exist from then on). |
| `known_at` | TEXT | NOT NULL | — | When the change was recorded, UTC `YYYY-MM-DD HH:MM:SS` from the database clock; written by the trigger. |
| `backfilled` | INTEGER | NOT NULL | `0` | 1 for `baseline` rows: the state already held when tracking began, so when it began to hold is unknown; 0 otherwise. |

#### EventVictimCoreference

One row per pair of victim names on one event, with the verdict on whether they are the same organisation in that incident's reporting. Written by `dedup/covictim_coreference.py` (`dedup_admin.py check-covictims`) from a Perplexity question or a person's alias record; a `same` verdict makes victim counting and the entity dashboard count the two names once on that event only.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `deduplicated_event_id` | TEXT | **PK** | — | The event both names are recorded victims of. |
| `name` | TEXT | **PK** | — | The first of the two victim names (the pair is stored in sorted order). |
| `other_name` | TEXT | **PK** | — | The second of the two victim names. |
| `verdict` | TEXT | NOT NULL | — | The answer: `same`, `parent_subsidiary`, `related`, `different` or `unsure`; only `same` joins the names, and only on this event. |
| `canonical_name` | TEXT | Yes | — | The organisation's usual full name when the verdict is `same`, as the model gave it; NULL otherwise and for a person's record. |
| `reason` | TEXT | Yes | — | The model's one-line reason, or `a person's alias or alias decision` when a person's record decided. |
| `citations_json` | TEXT | Yes | — | JSON list of URLs the Perplexity answer cited; `[]` for a person's record. |
| `model` | TEXT | NOT NULL | — | The Perplexity model that answered, or `human` when a person's alias or alias decision decided without a question. |
| `prompt_version` | TEXT | **PK** | — | Version of the question (`covictim_coreference.PROMPT_VERSION`, e.g. `covictim-coreference-v1`); part of the primary key. |
| `evidence_hash` | TEXT | NOT NULL | — | SHA-256 of the question exactly as sent (`human` for a person's record); a verdict counts only while the event's question still hashes to it. |
| `decided_at` | TEXT | NOT NULL | — | When the verdict was stored (UTC, ISO 8601 with offset). |

#### ImpossibleIncidentDates

One row per incident date removed because it fell more than 7 days after the date the source gave for the item itself. Written by `remove_dates_after_publication` in `processing/incident_dates.py` (`dedup_admin.py publication-dates`) as an audit record.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `raw_event_id` | TEXT | NOT NULL | — | The record whose date was removed (`RawEvents.raw_event_id`). |
| `enriched_event_id` | TEXT | Yes | — | The record's enrichment, whose research date was removed when `field` is the research date. |
| `field` | TEXT | NOT NULL | — | What was removed: `raw incident date` (`RawEvents.event_date`) or `research date and count` (the Perplexity date with its records count, withheld and the record re-queued for research). |
| `removed_value` | TEXT | Yes | — | The removed value: the date, or `<date> / <count>` for research. |
| `publication_date` | TEXT | NOT NULL | — | The source's own date for the item, which the incident date followed (a Webber date counts to the end of its month). |
| `removed_at` | TEXT | NOT NULL | — | When it was removed (ISO timestamp, local time). |

#### InputHistoryStatus

One row per watched table of the input history (`dedup/input_history.py`): when tracking began and which columns are tracked.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `source` | TEXT | **PK** | — | The history source: `events`, `membership`, `links` or `entities`. |
| `table_name` | TEXT | NOT NULL | — | The watched table (`DeduplicatedEvents`, `EventDeduplicationMap`, `DeduplicatedEventEntities` or `EntitiesV2`). |
| `tracking_since` | TEXT | NOT NULL | — | When the triggers were first installed (UTC); a cutoff before it is reconstructed from the baseline rows. |
| `baselined_at` | TEXT | NOT NULL | — | When the baseline rows (the state at that moment) were written (UTC). |
| `columns_json` | TEXT | NOT NULL | — | JSON object mapping each tracked column to when it began to be tracked; a column added later has no value for earlier rows. |

#### ModelJudgements

One row per stored model answer: every Jev judgement (`utils/jev.py`), every reasoning-model escalation (`utils/escalation.py`) and the fast model's organisation extractions (`dedup/event_organisations.py`), each keyed by a hash of its input so a re-run reuses it. Read by every calibrated step (event reality, scope, coherence, duplicates, victim attribution and fill, names, pages, AI involvement, breach source) and by the calibration scripts.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `judgement_id` | TEXT | **PK** | — | Row identifier (UUID). |
| `subject_type` | TEXT | NOT NULL | — | What was judged, e.g. `deduplicated_event`, `event_pair`, `event_victim`, `event_victim_fill`, `event_member`, `event_member_same_org`, `event_organisation`, `organisation_name`, `entity_mention`, `raw_event`, `enriched_event`. |
| `subject_id` | TEXT | NOT NULL | — | Id of the subject; for a pair, the two ids sorted and joined by `\|`. |
| `question_id` | TEXT | NOT NULL | — | Stable name of the question, e.g. `page_kind`; with `input_hash` it is the reuse key. |
| `question_version` | TEXT | NOT NULL | — | Version of the question's wording and evidence, bumped whenever either changes. |
| `primitive` | TEXT | NOT NULL | — | Kind of answer: `choice`, `noul` or `score` (Jev), `escalation` or `escalation_choice` (reasoning model), `extraction` (fast-model organisation list). |
| `answer` | TEXT | Yes | — | The chosen option for a choice; `yes`/`no` at 0.5 for a noul or escalation; NULL for a score; the extracted names as JSON for an extraction. |
| `value` | REAL | Yes | — | Noul or escalation probability, score value, or the chosen option's probability (0-1); NULL for an extraction. |
| `confidence` | REAL | Yes | — | Jev's concentration of the distribution for a choice or score; NULL for a noul, escalation or extraction. |
| `probabilities_json` | TEXT | Yes | — | The full distribution as returned, as JSON (option to probability); escalations add the model's `rationale`; `{}` for an extraction. |
| `question_json` | TEXT | NOT NULL | — | The question exactly as sent, as JSON (escalations also record the reasoning effort). |
| `input_hash` | TEXT | NOT NULL | — | SHA-256 of the evidence state, the question and the requested model (`jev.input_hash`; escalations also hash the reasoning effort); an answer is reused only for the same `question_id` and hash. |
| `model_requested` | TEXT | NOT NULL | — | The model asked, e.g. `jev-1.14.0` (`TYPESAFE_MODEL`), `gpt-5.5-2026-04-23` (`OPENAI_MODEL`) or `gpt-5.4-mini-2026-03-17` (`OPENAI_FAST_MODEL`). |
| `model` | TEXT | Yes | — | The model version the response reported as having answered. |
| `input_tokens` | INTEGER | Yes | — | Input tokens of the whole request, repeated on each answer of a multi-question Jev request; NULL when not reported. |
| `output_tokens` | INTEGER | Yes | — | Output tokens of the whole request, repeated on each answer; NULL when not reported. |
| `created_at` | TEXT | NOT NULL | — | When the answer was stored (UTC, ISO 8601 with offset; rows before 2026-10-30 are naive local time). |

#### OrganisationTimelineFailures

One row per failed Perplexity search for an organisation's incident history. Written by `org_timeline._search_once` (`dedup/org_timeline.py`) before it raises; read by `org_timeline.limitations`, which warns the judgements that a timeline older than the failure may be incomplete.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `entity_id` | INTEGER | NOT NULL | — | The organisation researched (`EntitiesV2.entity_id`). |
| `entity_name` | TEXT | NOT NULL | — | The organisation's name as researched. |
| `query` | TEXT | NOT NULL | — | The question sent to Perplexity. |
| `model` | TEXT | NOT NULL | — | The Perplexity model asked (`PERPLEXITY_MODEL`, e.g. `sonar-pro`). |
| `error` | TEXT | NOT NULL | — | The exception type and message, cut to 500 characters. |
| `failed_at` | TEXT | NOT NULL | — | When the search failed (ISO 8601 local time from `datetime.now()`, no offset). |

#### OrganisationTimelines

One row per Perplexity search for an organisation's full incident history (every version kept; the latest is used). Written by `org_timeline._search_once` (`dedup_admin.py research-timelines` and the hygiene steps); read as evidence by event reality, duplicates, coherence and victim judgements, by `reconcile-timelines` and by `scripts/build_timeline_dashboard.py`.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `entity_id` | INTEGER | **PK** | — | The organisation researched (`EntitiesV2.entity_id`). |
| `entity_name` | TEXT | NOT NULL | — | The organisation's name as researched. |
| `query_hash` | TEXT | **PK** | — | SHA-256 of the model, the query and the discovery time of the newest record about the organisation (`org_timeline.latest_evidence`), so a new record leads to a new search. |
| `query` | TEXT | NOT NULL | — | The question sent to Perplexity (prompt `timeline-v2`). |
| `model` | TEXT | NOT NULL | — | The Perplexity model that answered (`PERPLEXITY_MODEL`, e.g. `sonar-pro`). |
| `answer` | TEXT | Yes | — | Perplexity's answer text: the dated list of incidents, or the answer as given when it lists none. |
| `citations_json` | TEXT | Yes | — | JSON list of URLs the answer cited. |
| `created_at` | TEXT | NOT NULL | — | When the search was made (ISO 8601 local time from `datetime.now()`, no offset). |

#### PageClassifications

One row per enriched record: what kind of page its scraped source is, so roundups, guidance, profiles and index pages can be treated as non-incidents. Written by `dedup/page_classifier.py` (`dedup_admin.py classify-pages`); read by `reject-non-incidents` and the dedup adjudicators.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `enriched_event_id` | TEXT | **PK** | — | The classified record (`EnrichedEvents.enriched_event_id`). |
| `kind` | TEXT | NOT NULL | — | Page kind: `specific_incident`, `roundup_or_list`, `guidance_or_marketing`, `profile_or_personal` or `index_or_homepage`. |
| `confidence` | REAL | NOT NULL | — | Probability (0-1) of the chosen side, incident or non-incident, summed over the Jev distribution; on older OpenAI rows, the model's own confidence. |
| `reasoning` | TEXT | Yes | — | For Jev, `jev: ` followed by each kind's probability as JSON; for OpenAI, one sentence naming the decisive evidence. |
| `model` | TEXT | Yes | — | Model that classified the page: the Jev version (e.g. `jev-1.14.0`) or the OpenAI fast model. |
| `created_at` | TEXT | NOT NULL | — | When the page was classified (ISO timestamp, local time). |
| `question_version` | TEXT | Yes | — | Version of the page-kind question (e.g. `page-kind-v2-source-text`); a verdict under another version is asked again. NULL on rows from before versions were stored. |

#### PageRescrapes

One row per record whose page is queued for, or has had, a Playwright re-fetch because its stored text is too short or is not the record's article. Written by `processing/page_rescrape.py` (`dedup_admin.py rescrape-short-pages`) and queued also by `processing/record_identity.py` and `processing/evidence_recovery.py`; read by those and the quality report.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `raw_event_id` | TEXT | **PK** | — | The record whose page is re-fetched (`RawEvents.raw_event_id`). |
| `url` | TEXT | Yes | — | The page URL fetched. |
| `attempts` | INTEGER | NOT NULL | `0` | Failed fetch attempts; the page is given up at 3. |
| `status` | TEXT | NOT NULL | — | `queued` (asked for, not tried yet), `stored` (new text stored), `retry` or `gave_up`. |
| `last_error` | TEXT | Yes | — | Why the last attempt failed (e.g. too short, or not longer than the stored text); NULL after success. |
| `last_chars` | INTEGER | Yes | — | Length in characters of the text fetched last time. |
| `last_attempt_at` | TEXT | NOT NULL | — | When the page was last tried, or queued (ISO timestamp, local time). |

#### PublicationDateFetches

One row per record whose page was fetched (or skipped) to read its publication date from page metadata or the URL. Written by `processing/publication_dates.py` (`dedup_admin.py publication-dates`), which reads it to skip settled pages and retry failures.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `raw_event_id` | TEXT | **PK** | — | The record (`RawEvents.raw_event_id`). |
| `attempts` | INTEGER | NOT NULL | `0` | Fetch attempts made; given up at 3, and `0` for listing pages, which are not fetched. |
| `status` | TEXT | NOT NULL | — | `stored` (date found), `no date on page`, `retry`, `gave up` or `listing page` (a page cited by records of more than 3 events, whose date is nobody's publication date). |
| `last_error` | TEXT | Yes | — | Error of the last failed fetch (e.g. `HTTP 404`); NULL otherwise. |
| `last_attempt_at` | TEXT | NOT NULL | — | When the page was last tried (ISO timestamp, local time, to the second). |

#### ReconciliationGaps

One row per incident in an organisation's researched timeline that no event covered, with the search made for proper reporting of it. Written by `timeline_reconciliation.queue_gap` (`dedup_admin.py reconcile-timelines --queue-gaps`), which also queues a new `RawEvents` record when the search finds reporting; a gap already stored is not searched again.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `entity_id` | INTEGER | **PK** | — | The organisation whose timeline lists the incident (`EntitiesV2.entity_id`). |
| `incident_key` | TEXT | **PK** | — | The incident's date plus the first eight words of its description, cut to 200 characters; with `entity_id` the primary key. |
| `incident_text` | TEXT | NOT NULL | — | The timeline incident as JSON (`date`, `what`, `source` and the other fields the timeline gave). |
| `query` | TEXT | Yes | — | The question sent to Perplexity for reporting of the incident. |
| `model` | TEXT | Yes | — | The Perplexity model asked (`PERPLEXITY_MODEL`, e.g. `sonar-pro`). |
| `answer` | TEXT | Yes | — | Perplexity's answer; one beginning `FOUND: YES` with citations queues a record. |
| `citations_json` | TEXT | Yes | — | JSON list of URLs the answer cited, or the timeline's own source when it cited none. |
| `raw_event_id` | TEXT | Yes | — | The `RawEvents` record queued for normal ingestion when reporting was found; NULL when none was. |
| `created_at` | TEXT | NOT NULL | — | When the gap was searched (ISO 8601 local time from `datetime.now()`, no offset). |

#### RecordFingerprints

One row per enriched record placed in an event: the values of the fields events are built from, as last seen, so a re-enriched record refreshes its events with their ids kept. Written and read by `refresh_changed_events` in `dedup/record_fingerprints.py` (run by `run_global_deduplication.py` and `refresh-cited-documents`).

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `enriched_event_id` | TEXT | **PK** | — | The record (`EnrichedEvents.enriched_event_id`). |
| `fingerprint` | TEXT | NOT NULL | — | SHA-256 of `values_json`. |
| `values_json` | TEXT | NOT NULL | — | JSON of the tracked `EnrichedEvents` fields (`title`, `description`, `summary`, `event_type`, `severity`, `event_date`, `records_affected`, `attacking_entity_name`, `attack_method`, `is_australian_event`, `is_specific_event`, `perplexity_enrichment_data`). |
| `recorded_at` | TEXT | NOT NULL | — | When the fingerprint was stored (ISO timestamp, local time). |

#### SchemaMigrations

One row per numbered SQL migration in `cyber_data_collector/storage/migrations/` applied to the database. Written and read by `storage/schema_init.initialize_database` (`pipeline.py init`), which skips applied versions and reports any whose file has changed since.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `version` | TEXT | **PK** | — | File name of the migration without `.sql`, e.g. `0001_base_schema`. |
| `checksum` | TEXT | NOT NULL | — | SHA-256 (hex) of the migration file with CRLF line endings read as LF, at the time it was applied. |
| `applied_at` | TEXT | NOT NULL | — | When the migration was applied, ISO 8601 local time to the second. |

#### SourceMonthCompletion

View with one row per source and discovery month: how many of the records discovery stored are processed, rejected, pending, failing or quarantined. Recreated by `processing/retry_queue.py` from `DiscoveryRecordState` joined to `RawEvents`; read by `RetryQueue.completion` (`discovery_queue.py status`).

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `source_type` | TEXT | Yes | — | Data source (`Perplexity`, `OAIC`, `GoogleSearch`, `WebberInsurance`, `CyberDaily`, `GDELT`). |
| `discovery_year` | INTEGER | Yes | — | Year of the source-month the records were first found under. |
| `discovery_month` | INTEGER | Yes | — | Month (1-12) of that source-month. |
| `stored` | — | Yes | — | Records stored for the source-month. |
| `processed` | — | Yes | — | Records processed and not rejected. |
| `rejected` | — | Yes | — | Records rejected at a filter stage (`Rejected at ...`), quarantines excluded. |
| `pending` | — | Yes | — | Records not yet processed. |
| `failing` | — | Yes | — | Pending records with at least one failed attempt (permanent, temporary or unknown). |
| `quarantined` | — | Yes | — | Records quarantined after repeated permanent failures (`Rejected at retry_quarantine`). |

#### SourceReplacements

One row per Perplexity search for replacement reporting whose best citation is queued as a new record with its own URL: for a removed junk record (source gate), an incident with no usable evidence, or a supplier's customer with no event of its own. Written by `search_and_queue` in `dedup/source_gate.py`, called from `check-sources`, `check-record-identity`, `recover-evidence` and `find-supplier-victims --search-customers`, which read it to skip or retry searches.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `enriched_event_id` | TEXT | **PK** | — | Search key: the removed or evidence-less record's `enriched_event_id`, or `customer:<supplier_incident_id>:<customer name>` for purpose `supplier_customer`. |
| `query` | TEXT | NOT NULL | — | The prompt sent to Perplexity. |
| `model` | TEXT | NOT NULL | — | Perplexity model used. |
| `answer` | TEXT | Yes | — | Perplexity's answer; NULL when the search failed. |
| `citations_json` | TEXT | Yes | — | JSON list of the citation URLs returned. |
| `raw_event_id` | TEXT | Yes | — | The new record queued from the best citation (`RawEvents.raw_event_id`); NULL when none was queued or it was pruned as already covered. |
| `created_at` | TEXT | NOT NULL | — | When the key was first searched (ISO timestamp, local time). |
| `status` | TEXT | Yes | — | `searched` (whatever it found) or `pending` (the search failed and is retried); NULL on older rows means `searched`. |
| `attempts` | INTEGER | NOT NULL | `0` | Searches made for the key. |
| `last_error` | TEXT | Yes | — | Error of the last failed search; NULL otherwise. |
| `updated_at` | TEXT | Yes | — | When the row last changed (ISO timestamp, local time). |
| `purpose` | TEXT | Yes | — | Who asked: NULL or `source_gate` (a removed junk record), `evidence_recovery` (`processing/evidence_recovery.py`) or `supplier_customer` (`dedup/multi_customer_incidents.py`). |
| `deduplicated_event_id` | TEXT | Yes | — | The event the search was for, set for `evidence_recovery`; NULL otherwise. |

#### SupplierIncidentLinks

One row per affected customer of a supplier incident: the customer, its own event when it has one, and the basis and judgement that linked it. Written by `supplier_links.Registry.write`, `supplier_links.convert` and `repair_registry` (`dedup_admin.py find-supplier-victims --apply`); read by `multi_customer_incidents` and the supplier incidents report.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `supplier_incident_id` | TEXT | **PK** | — | The `SupplierIncidents` row the customer belongs to; part of the primary key. |
| `customer_entity_id` | INTEGER | **PK** | — | `EntitiesV2.entity_id` of the customer; part of the primary key, so a customer is linked once per incident. |
| `customer_name` | TEXT | NOT NULL | — | The customer's name as the link was made (the event's recorded victim, or the name the reporting gives). |
| `customer_event_id` | TEXT | Yes | — | `DeduplicatedEvents` id of the customer's own event of the breach; NULL for a customer the reporting names that has no event of its own. A known event is never overwritten with NULL; readers resolve a Merged id with `resolve_event`. |
| `basis` | TEXT | NOT NULL | — | Why the customer is linked: `vendor-recorded` (its event records the supplier as vendor), `label` (a `same` pair label), `judged-same-incident` (the calibrated same-incident judgement), `customer-named` (named in the reporting and accepted by the victim judgement), `split-customer` (its records were split out of a multi-customer event) or `converted-attachment` (a co-victim attachment of the earlier convention). |
| `probability` | REAL | Yes | — | Final probability of the judgement behind the link: the same-incident pair score for `judged-same-incident`, the victim score for `customer-named`, the attachment's probability for `converted-attachment`; NULL for rule-based links. |
| `decided_by` | TEXT | NOT NULL | — | Who decided the link: `rule`, `label`, `jev` or `escalation`; for `converted-attachment`, the attachment's own decider (`jev`, `escalation`, `label`, `merge`) or `attachment`. |
| `created_at` | TEXT | NOT NULL | — | Local ISO timestamp when the link was first written. |
| `updated_at` | TEXT | NOT NULL | — | Local ISO timestamp of the last change to the link (stronger basis, event found, or moved by an incident merge). |

#### SupplierIncidents

One row per supplier incident: one attack on a supplier, with the supplier's own event when one exists and the date span of the events linked to it. Written by `supplier_links.Registry.write` and repaired by `supplier_links.repair_registry` (both run by `dedup_admin.py find-supplier-victims`); read by `multi_customer_incidents` for `dashboard/supplier_incidents.html` and `dedup_admin.py supplier-incidents`.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `supplier_incident_id` | TEXT | **PK** | — | Primary key, `si-` plus the first 16 hex digits of a SHA-1 of the supplier key and the anchoring event id (`supplier_links.Registry.incident`). |
| `supplier_key` | TEXT | NOT NULL | — | Canonical key of the supplier's name (`EntityResolver.key_for`); spellings of one supplier share one key, so their incidents can be merged by `repair_registry`. |
| `supplier_entity_id` | INTEGER | Yes | — | `EntitiesV2.entity_id` of the supplier; NULL when no entity row holds its name. |
| `supplier_name` | TEXT | NOT NULL | — | The supplier's name as first recorded for the incident. |
| `supplier_event_id` | TEXT | Yes | — | `DeduplicatedEvents` id of the supplier's own event (its report, or an event recording it as vendor) that anchors the incident; NULL when none exists. Stored as written; readers resolve a Merged id with `supplier_links.resolve_event`. |
| `first_date` | TEXT | Yes | — | Earliest `event_date` (YYYY-MM-DD) among the events linked to the incident; recomputed from the current links by `repair_registry`. |
| `last_date` | TEXT | Yes | — | Latest `event_date` (YYYY-MM-DD) among the events linked to the incident. |
| `created_at` | TEXT | NOT NULL | — | Local ISO timestamp when the incident row was first written. |
| `updated_at` | TEXT | NOT NULL | — | Local ISO timestamp of the last change to the row (anchor, key or date span). |

#### SupplierIncidentSearches

One row per Perplexity search for the organisations affected by a supplier incident event, stored so the search is asked once and can be replayed in calibration. Written by `supplier_victims.research_incident` (`dedup_admin.py find-supplier-victims --research`); read by `supplier_victims.stored_search` and carried into recalibration copies by `recalibration` and `scripts/prepare_calibration_snapshot.py`.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `deduplicated_event_id` | TEXT | **PK** | — | The supplier incident event searched about; part of the primary key. |
| `query_hash` | TEXT | **PK** | — | SHA-256 of the model name and the query text (`supplier_victims._query_hash`); part of the primary key, so a changed query or model is asked again. |
| `query` | TEXT | NOT NULL | — | The full prompt sent (`supplier_victims.build_search_query`: the event's title, date and recorded vendors). |
| `model` | TEXT | NOT NULL | — | Perplexity model that answered (`llm_models.perplexity_model`, e.g. `sonar-pro`). |
| `answer` | TEXT | Yes | — | Perplexity's answer text listing the affected organisations. |
| `citations_json` | TEXT | Yes | — | JSON list of the URLs Perplexity cited. |
| `created_at` | TEXT | NOT NULL | — | Local ISO timestamp of the search. |

#### SupplierLinkActions

Audit row for each change the supplier-incident rules made to events: a split of a customer's records, a customer demoted on another event, or an event made (or unmade) a supplier's report. Written by `supplier_links.convert`, `double_counted_customers`, `reverse_name_counted_reports` and the customer-records split (`dedup_admin.py find-supplier-victims --apply`, and after role classification); read by `departed_customers`, `demote_split_customers` and the `split-customer` link repairs.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `action_id` | TEXT | **PK** | — | Primary key, a random UUID. |
| `action` | TEXT | NOT NULL | — | What was done: `split` (records left the event as a new event), `own-event-customer` (a customer with its own event was demoted to `affected_customer` here), `supplier-report` (the event became the supplier's report, the supplier its victim) or `supplier-report-reversed` (a name-counted `supplier-report` conversion undone). |
| `deduplicated_event_id` | TEXT | NOT NULL | — | The event acted on: the event split, the event the customer was demoted on, or the event converted or reverted. |
| `new_event_id` | TEXT | Yes | — | For `split`, the new event the records left as; for `own-event-customer`, the customer's own event; for `supplier-report-reversed`, the event split back out of a later merge; NULL otherwise. |
| `entity_id` | INTEGER | Yes | — | `EntitiesV2.entity_id` of the organisation concerned: the demoted customer for `own-event-customer`, the supplier for `supplier-report` and `supplier-report-reversed`; NULL for `split`. |
| `detail_json` | TEXT | Yes | — | JSON details of the action, e.g. the customer, record ids, own event and part that stays for `split`; supplier and customers for the report actions; the undone entry for `supplier-report-reversed`. |
| `reason` | TEXT | NOT NULL | — | The owner decision the rule applies, as a fixed text constant (`OWNER_DECISION`, `OWN_EVENT_DECISION`, `SUPPLIER_REPORT_DECISION`, `REPORT_REVERSAL_DECISION`). |
| `created_at` | TEXT | NOT NULL | — | Local ISO timestamp of the action. |

#### SupplierVictimAttachments

One row per organisation made a `victim` of an event by the supplier-victim step: a customer attached as a co-victim of a supplier incident, or a victim carried across a merge. Written by `supplier_victims.attach_victim` (now only through `carry_victims`, called by supplier merges and `dedup_admin.py` neighbourhood merges; the `jev` / `escalation` / `label` rows come from `find-supplier-victims --apply` before the 2026-10-31 owner decision); read by `reverse_attachments` and `supplier_links.convert`.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `attachment_id` | TEXT | **PK** | — | Primary key, a random UUID. |
| `deduplicated_event_id` | TEXT | NOT NULL | — | The event the organisation was made a victim of. |
| `entity_id` | INTEGER | NOT NULL | — | `EntitiesV2.entity_id` of the organisation attached. |
| `entity_name` | TEXT | NOT NULL | — | The organisation's `EntitiesV2.entity_name` at attachment time. |
| `previous_role` | TEXT | Yes | — | The organisation's `DeduplicatedEventEntities.relationship_type` on the event before the attachment (e.g. `affected_customer`, `bystander`); NULL when the event had no link to it. |
| `decided_by` | TEXT | NOT NULL | — | What decided the attachment: `jev`, `escalation` or `label` (victim judgement of the earlier convention), or `merge` (carried from a merged-away event). |
| `subject_id` | TEXT | Yes | — | The judgement subject: `event_id\|entity_id` (or `event_id\|name:key`) for a victim judgement, or the two event ids joined by `\|` for a merge. |
| `probability` | REAL | Yes | — | Final probability of the judgement or merge certainty behind the attachment; NULL when none was given. |
| `cutoff` | REAL | Yes | — | The calibrated cut-off in force when the attachment was made. |
| `question_version` | TEXT | Yes | — | Question version of the judgement: `supplier-victim-v1` for victim judgements, `same-supplier-incident-v1` (or the merge path's version) for carried victims. |
| `merged_from` | TEXT | Yes | — | For `merge` rows, the event merged away that the victim was carried from; NULL otherwise. |
| `snapshot_batch` | TEXT | Yes | — | `DedupSnapshots` batch id (`supplier-victims-YYYYMMDDHHMMSS-<event prefix>`) of the event's snapshot taken before the change. |
| `created_at` | TEXT | NOT NULL | — | Local ISO timestamp of the attachment. |

#### SupplierVictimReversals

One row per `SupplierVictimAttachments` row undone after the owner decision of 2026-10-31 (one event per customer, no co-victims). Written by `supplier_victims.reverse_attachments` and updated by `restore_own_victims` (both run by `supplier_links.convert`, `dedup_admin.py find-supplier-victims --apply`); read by them and by `supplier_links` to find customers that were reversed.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `attachment_id` | TEXT | **PK** | — | Primary key; the `SupplierVictimAttachments` row undone. |
| `deduplicated_event_id` | TEXT | NOT NULL | — | The event the victim role was taken from. |
| `entity_id` | INTEGER | NOT NULL | — | `EntitiesV2.entity_id` of the organisation whose victim role was reversed. |
| `entity_name` | TEXT | NOT NULL | — | The organisation's name, as in the attachment. |
| `action` | TEXT | NOT NULL | — | `reversed` (the role was undone) or `restored` (the event's own victim, given its role back by `restore_own_victims`; set back to `reversed` when the event's victim later becomes another organisation). |
| `restored_role` | TEXT | Yes | — | The event-level role the organisation was given back (the attachment's `previous_role`, e.g. `affected_customer`); NULL when the event link was removed. |
| `own_events_json` | TEXT | NOT NULL | — | JSON list of the organisation's other Active events where it is the victim, at reversal time. |
| `reason` | TEXT | NOT NULL | — | The owner decision applied (`supplier_victims.OWNER_DECISION`). |
| `snapshot_batch` | TEXT | Yes | — | `DedupSnapshots` batch id (`supplier-victims-reverse-YYYYMMDDHHMMSS-<event prefix>`) of the event's snapshot taken before the reversal. |
| `created_at` | TEXT | NOT NULL | — | Local ISO timestamp of the reversal. |

#### UniqueEvents_v1_backup

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `event_id` | TEXT | **PK** | — | UUID key of a V1 event (V1 schema, not used by current code). |
| `unique_hash` | VARCHAR(64) | NOT NULL | — | SHA-256 of the title (letters and digits only, lower case) followed by the event date `YYYY-MM-DD`; the V1 `add_event` deduplicated on it (V1 schema, not used by current code). |
| `title` | VARCHAR(255) | NOT NULL | — | Event title (V1 schema, not used by current code). |
| `description` | TEXT | Yes | — | Event description, replaced by the V1 enrichment summary (V1 schema, not used by current code). |
| `event_date` | DATE | NOT NULL | — | Event date, `YYYY-MM-DD` (V1 schema, not used by current code). |
| `event_type` | VARCHAR(50) | Yes | — | Event category; counted by the V1 summary statistics, written by no V1 code in git history (V1 schema, not used by current code). |
| `severity` | VARCHAR(20) | Yes | — | Severity of the event (V1 schema, not used by current code). |
| `records_affected` | BIGINT | Yes | — | Number of records affected (V1 schema, not used by current code). |
| `status` | VARCHAR(20) | Yes | `'Active'` | Event status, default `Active` (V1 schema, not used by current code). |
| `created_at` | TIMESTAMP | Yes | `CURRENT_TIMESTAMP` | When the row was created; defaults to `CURRENT_TIMESTAMP` (UTC) (V1 schema, not used by current code). |
| `updated_at` | TIMESTAMP | Yes | `CURRENT_TIMESTAMP` | When the row was last updated; defaults to `CURRENT_TIMESTAMP` (UTC) (V1 schema, not used by current code). |
| `is_australian_event` | BOOLEAN | Yes | `FALSE` | 1 when V1 enrichment judged the event Australian (V1 schema, not used by current code). |
| `is_specific_event` | BOOLEAN | Yes | `FALSE` | 1 when V1 enrichment judged it a specific incident rather than general news (V1 schema, not used by current code). |
| `is_reviewed` | BOOLEAN | Yes | `FALSE` | 1 once a person had reviewed the event in the V1 review flow (`mark_event_as_reviewed`) (V1 schema, not used by current code). |

#### ValidationExceptions

One row per wrong result the owner reported, kept as an acceptance test the data must pass from then on. Written by `dedup/validation_exceptions.py` (`dedup_admin.py validation-exception add`, `check`, `root-cause`); `check` re-evaluates every row and the quality report counts the failures, and no pipeline step reads it to decide anything.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `exception_id` | TEXT | **PK** | — | Random UUID of the exception. |
| `reported_at` | TEXT | NOT NULL | — | When it was recorded (ISO timestamp, UTC). |
| `reported_by` | TEXT | NOT NULL | — | Who reported it (`owner`). |
| `kind` | TEXT | NOT NULL | — | What must hold: `victim`, `not_victim`, `same_incident_as`, `different_incident_from`, `incident_month`, `same_supplier_incident_as`, `not_supplier_customer`, `supplier_customers_once`, `not_vendor`, `victim_size`, `not_supplier_incident` or `ai_label_current`. |
| `deduplicated_event_id` | TEXT | NOT NULL | — | The event as reported; at check time the event is the Active one holding most of `record_ids_json`. |
| `record_ids_json` | TEXT | NOT NULL | — | JSON list of the event's Active record ids when reported, which anchor the exception across merges and splits. |
| `value` | TEXT | NOT NULL | — | What the kind is checked against: an organisation name, the other event's id, a month `YYYY-MM`, `A\|B` for two names of one customer, `NAME=BAND` for `victim_size`, or `current`. |
| `value_record_ids_json` | TEXT | Yes | — | JSON list of the other event's record ids for the pair kinds; NULL otherwise. |
| `observed` | TEXT | Yes | — | What was wrong when it was reported. |
| `note` | TEXT | NOT NULL | — | The owner's words. |
| `root_cause` | TEXT | Yes | — | What was found and fixed; set by `validation-exception root-cause`. |
| `last_checked_at` | TEXT | Yes | — | When `check` last evaluated it (ISO timestamp, UTC). |
| `last_result` | TEXT | Yes | — | Result of the last check: `pass`, `fail` or `unchecked` (no single Active event holds the records, or the case cannot be evaluated yet). |
| `last_detail` | TEXT | Yes | — | What the last check found, e.g. which event holds the records or which victims it records. |

#### VictimAttributionActions

Audit of `dedup_admin.py check-victim-attribution --apply`: one row per recorded event-victim pair acted on or reviewed (demoted, re-attributed, flagged or confirmed), with the scores, cut-offs and calibration behind it. Written by `victim_judgement.apply_actions` (`dedup/victim_judgement.py`); the latest row per pair decides whether the attribution is unresolved for victim counting, the by-entity dashboard and the recurrence analysis.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `action_id` | INTEGER | **PK** | — | Row identifier (auto-increment integer); the highest per event and entity is the pair's current state. |
| `batch_id` | TEXT | NOT NULL | — | The run that wrote the row, `victim-attribution-YYYYMMDDHHMMSS`; also the `DedupSnapshots` batch for the events it changed. |
| `deduplicated_event_id` | TEXT | NOT NULL | — | The event whose recorded victim was judged. |
| `entity_id` | INTEGER | NOT NULL | — | The recorded victim judged (`EntitiesV2.entity_id`). |
| `old_victim` | TEXT | NOT NULL | — | The recorded victim's name at the time. |
| `action` | TEXT | NOT NULL | — | What was done: `demoted` (victim role removed, another victim kept), `reattributed` (replaced by `new_victim`), `flagged` (left unchanged, unresolved) or `confirmed` (judged at or above the cut-off, or labelled correct; clears a flag). |
| `new_victim` | TEXT | Yes | — | The organisation that became the victim when `reattributed`; NULL otherwise. |
| `p_recorded_victim` | REAL | NOT NULL | — | Probability (0-1) that the recorded victim is the affected organisation, from Jev or the escalation; a fixed score when a label decided. |
| `replacement_probability` | REAL | Yes | — | Probability (0-1) of the chosen replacement candidate; NULL when no replacement was chosen or a label decided. |
| `cutoff` | REAL | Yes | — | The calibrated cut-off `p_recorded_victim` was compared against (act below it). |
| `replacement_threshold` | REAL | Yes | — | The calibrated threshold a replacement's probability had to reach; NULL when the calibration has none. |
| `calibration_json` | TEXT | Yes | — | The calibration used, as JSON: `question_version`, `model`, `cutoff`, `replacement_threshold`, `calibrated_on_database`, `generated`. |
| `reason` | TEXT | Yes | — | Why this action was taken or the pair was flagged, e.g. other victims kept, replacement below threshold, replacement not named in the records' text. |
| `roles_before_json` | TEXT | Yes | — | The event's entity roles before the change, as JSON `[[entity_id, relationship_type], ...]`, so it can be reversed. |
| `created_at` | TEXT | NOT NULL | — | When the row was written (UTC, ISO 8601 with offset; older rows naive local time). |
| `input_hash` | TEXT | Yes | — | Hash of the judged input (evidence, timeline, membership and other victims); a flag is written again only when this, the score or the reason changes. |
| `member_signature` | TEXT | Yes | — | Signature of the event's member records when judged (`role_maintenance.member_signature`). |

#### VictimFillActions

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `action_id` | INTEGER | **PK** | — | Row identifier (auto-increment integer). |
| `batch_id` | TEXT | NOT NULL | — | The run that wrote the row, `victim-fill-YYYYMMDDHHMMSS`; also the `DedupSnapshots` batch for the events it filled. |
| `deduplicated_event_id` | TEXT | NOT NULL | — | The event that had no victim and was filled (or whose fill was reversed). |
| `action` | TEXT | NOT NULL | — | `filled` (a victim was given to the event) or `reversed` (an earlier fill undone because its victim is now judged a description). |
| `new_victim` | TEXT | NOT NULL | — | The victim stored on the event; several co-equal victims are joined by `; `. For `reversed`, the victim that was removed. |
| `choice_probability` | REAL | Yes | — | Probability (0-1) of the chosen candidate from the Jev `fill_victim` choice or its escalation; NULL when a label decided or for `reversed`. |
| `cutoff` | REAL | Yes | — | The calibrated cut-off the choice was compared against; NULL without a calibration (label-only fills) and for `reversed`. |
| `candidates_json` | TEXT | Yes | — | The candidates offered, as JSON `[{"name", "support", "sources"}, ...]`. |
| `replaced_json` | TEXT | Yes | — | Unusable victims demoted before the fill (placeholders or descriptions), as JSON `[[entity_id, name], ...]`. |
| `calibration_json` | TEXT | Yes | — | The calibration used, as JSON: `question_version`, `model`, `cutoff`, `calibrated_on_database`, `generated`. |
| `reason` | TEXT | Yes | — | Why the event was filled, or why a fill was reversed. |
| `roles_before_json` | TEXT | Yes | — | The event's entity roles before the fill, as JSON `[[entity_id, relationship_type], ...]`; read to reverse it. |
| `member_signature` | TEXT | Yes | — | Signature of the event's member records when filled (`role_maintenance.member_signature`). |
| `created_at` | TEXT | NOT NULL | — | When the row was written (UTC, ISO 8601 with offset). |

---

## Database Relationships (ERD)

```
┌──────────────┐
│  RawEvents   │
│  (raw_event_ │
│   id PK)     │
└──────┬───────┘
       │ 1
       │
       │ N
┌──────┴───────┐        ┌──────────────────┐
│ProcessingLog │        │ EnrichmentAudit  │
│(log_id PK)   │        │ Trail            │
│FK: raw_event │        │ (audit_id PK)    │
│    _id       │        │ FK: raw_event_id │
└──────────────┘        └──────────────────┘
       │ 1                        │
       │                          │
       │ N                        │
┌──────┴───────┐                  │
│EnrichedEvents│◄─────────────────┘
│(enriched_    │     last_enrichment_audit_id
│ event_id PK) │
│FK: raw_event │
│    _id       │
└──────┬───────┘
       │
       ├────────────────────────────────────┐
       │ N                                  │ N
       │                                    │
┌──────┴────────────┐              ┌────────┴────────┐
│EnrichedEvent      │              │EventDedup       │
│  Entities         │              │  Map            │
│PK: (enriched_     │              │(map_id PK)      │
│  event_id,        │              │FK: raw_event_id │
│  entity_id)       │              │FK: enriched_    │
│FK: enriched_      │              │    event_id     │
│    event_id       │              │FK: deduplicated │
│FK: entity_id ─────┤              │    _event_id    │
└───────────────────┘              └────────┬────────┘
       │                                    │
       │                                    │
       ▼                                    ▼
┌──────────────┐               ┌─────────────────────┐
│ EntitiesV2   │               │DeduplicatedEvents   │
│ (entity_id   │               │(deduplicated_event_ │
│  PK)         │               │ id PK)              │
└──────┬───────┘               │FK: master_enriched_ │
       │                       │    event_id          │
       │                       └──────────┬───────────┘
       │                                  │
       │              ┌───────────────────┼───────────────────┐
       │              │                   │                   │
       │     ┌────────┴────────┐  ┌───────┴────────┐  ┌──────┴──────────┐
       │     │DeduplicatedEvent│  │DeduplicatedEvent│  │ASDRisk          │
       │     │  Entities       │  │  Sources        │  │Classifications  │
       └────►│PK: (dedup_      │  │PK: (dedup_      │  │(classification_ │
             │  event_id,      │  │  event_id,      │  │ id PK)          │
             │  entity_id)     │  │  source_url)    │  │FK: deduplicated │
             │FK: dedup_       │  │FK: dedup_       │  │    _event_id    │
             │    event_id     │  │    event_id     │  └─────────────────┘
             │FK: entity_id    │  └─────────────────┘
             └─────────────────┘

┌──────────────────┐        ┌──────────────────┐
│DeduplicationClust│        │EntityMappings    │
│  ers             │        │(entity_mapping_  │
│(cluster_id PK)   │        │ id PK)           │
│FK: dedup_event_id│        │source_entity →   │
└──────────────────┘        │canonical_entity  │
                            └──────────────────┘

┌──────────────────┐        ┌──────────────────┐
│DataSourcesV2     │        │IndustryGroupings │
│(source_id PK)    │        │(industry_group_  │
│                  │        │ id PK)           │
└──────────────────┘        └──────────────────┘

┌──────────────────┐        ┌──────────────────┐
│MonthProcessed    │        │AuditLog          │
│(id PK)           │        │(audit_id PK)     │
│UNIQUE(year,month)│        │                  │
└──────────────────┘        └──────────────────┘
```

### Key Relationships Summary

| From Table | To Table | Cardinality | FK Column | Cascade |
|---|---|---|---|---|
| RawEvents | EnrichedEvents | 1:N | `raw_event_id` | CASCADE |
| RawEvents | ProcessingLog | 1:N | `raw_event_id` | CASCADE |
| RawEvents | EnrichmentAuditTrail | 1:N | `raw_event_id` | No cascade |
| RawEvents | EventDeduplicationMap | 1:N | `raw_event_id` | CASCADE |
| EnrichedEvents | EnrichedEventEntities | 1:N | `enriched_event_id` | CASCADE |
| EnrichedEvents | EventDeduplicationMap | 1:N | `enriched_event_id` | CASCADE |
| EnrichedEvents | DeduplicatedEvents | 1:N | `master_enriched_event_id` | CASCADE |
| EntitiesV2 | EnrichedEventEntities | 1:N | `entity_id` | CASCADE |
| EntitiesV2 | DeduplicatedEventEntities | 1:N | `entity_id` | CASCADE |
| DeduplicatedEvents | DeduplicatedEventEntities | 1:N | `deduplicated_event_id` | CASCADE |
| DeduplicatedEvents | DeduplicatedEventSources | 1:N | `deduplicated_event_id` | CASCADE |
| DeduplicatedEvents | EventDeduplicationMap | 1:N | `deduplicated_event_id` | CASCADE |
| DeduplicatedEvents | DeduplicationClusters | 1:1 | `deduplicated_event_id` | CASCADE |
| DeduplicatedEvents | ASDRiskClassifications | 1:1 | `deduplicated_event_id` | CASCADE |

---

## JSON Columns (Embedded JSON)

Several database columns store serialized JSON. These are the documented schemas for each.

### RawEvents: source_metadata

Stored in `RawEvents.source_metadata`. Written by the discovery pipeline during event ingestion and updated during content filtering.

```json
{
  "event_type": "Data Breach",
  "severity": "High",
  "confidence": 0.85,
  "australian_relevance": 0.95,
  "data_sources_count": 3,
  "publication_date": "2024-03-15",
  "content_filter_confidence": 0.92,
  "content_filter_reasoning": "Specific Australian cyber incident with named entity",
  "content_filter_stage": "llm_pass",
  "content_filter_risk_level": "low"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `event_type` | string | Initial event classification |
| `severity` | string | Initial severity assessment |
| `confidence` | float | Source confidence score (0.0–1.0) |
| `australian_relevance` | float | Australian relevance score (0.0–1.0) |
| `data_sources_count` | integer | Number of sources reporting this event |
| `publication_date` | string | ISO date of the original publication |
| `content_filter_confidence` | float | ML/LLM filter confidence (0.0–1.0) |
| `content_filter_reasoning` | string | Reasoning for the filter decision |
| `content_filter_stage` | string | Filter stage that processed it |
| `content_filter_risk_level` | string | Risk level assessment |
| `provenance` | object | Where the source got the record, when it says (Perplexity discovery; below) |

**`provenance` of a Perplexity record** (`datasources/perplexity.py`, version `perplexity-children-v1`). Each
article URL a Perplexity answer item cites is its own record; the siblings share `answer_item_id`.

```json
"provenance": {
  "origin": "perplexity_answer",
  "version": "perplexity-children-v1",
  "query": "Australian cyber attack after:01/01/2025 before:03/31/2025 data breach security incident",
  "answer_item_id": "3f1c0a9e5b7d2c41",
  "url_index": 0,
  "url_count": 2,
  "sibling_urls": ["https://news.example/acme-breach", "https://other.example/acme"],
  "skipped_urls": ["https://cybertrackerau.com/"],
  "recovery": null,
  "answer": {"title": "...", "description": "...", "event_date": "2025-03-14", "entity_name": "Acme",
             "event_type": "Data Breach", "impact_description": null, "source_urls": ["..."]}
}
```

| Field | Description |
|-------|-------------|
| `query` | the query as sent, with its `after:/before:` window (a half window when a truncated answer was searched again) |
| `answer_item_id` | first 16 hex of SHA-256 over the item's fields (URLs cleaned); the same for every sibling and for the same item in another answer |
| `url_index`, `url_count` | this record's place among the item's article URLs (`null` / `0` for an item citing no article) |
| `sibling_urls` | every article URL of the item, cleaned of tracking parameters, in answer order |
| `skipped_urls` | URLs the item cites that are not article addresses (site roots, non-URLs); not stored as sources |
| `recovery` | how the item was read: `null` (whole answer), `repaired`, `object`, `truncated_object`, `completed` |
| `answer` | the item's own fields as the answer gave them (`event_date` may be a month or year; only a full day becomes `RawEvents.event_date`) |

---

### perplexity_enrichment_data

Stored in both `EnrichedEvents.perplexity_enrichment_data` and `DeduplicatedEvents.perplexity_enrichment_data`. Written by the Perplexity enrichment stage. Corresponds to the `PerplexityEventEnrichment` Pydantic model.

```json
{
  "earliest_event_date": "2024-02-15",
  "date_confidence": 0.95,
  "formal_entity_name": "Medibank Private Limited",
  "entity_confidence": 0.98,
  "victim_industry": "Healthcare",
  "threat_actor": "REvil",
  "threat_actor_confidence": 0.85,
  "attack_method": "ransomware",
  "attack_method_confidence": 0.92,
  "victim_count": 9700000,
  "victim_count_confidence": 0.80,
  "sources_consulted": [
    "https://example.com/article1",
    "https://example.com/article2"
  ],
  "overall_confidence": 0.90,
  "reasoning": "Multiple authoritative sources confirm..."
}
```

| Field | Type | Description |
|-------|------|-------------|
| `earliest_event_date` | string\|null | Earliest confirmed date (YYYY-MM-DD) |
| `date_confidence` | float\|null | Confidence in the date (0.0–1.0) |
| `formal_entity_name` | string\|null | Official name of the victim organization |
| `entity_confidence` | float\|null | Confidence in entity identification (0.0–1.0) |
| `victim_industry` | string\|null | Industry sector of the victim |
| `threat_actor` | string\|null | Name of the threat actor or group |
| `threat_actor_confidence` | float\|null | Confidence in threat actor attribution (0.0–1.0) |
| `attack_method` | string\|null | Type of attack (e.g., "ransomware", "phishing") |
| `attack_method_confidence` | float\|null | Confidence in attack method identification (0.0–1.0) |
| `victim_count` | integer\|null | Number of individuals affected |
| `victim_count_confidence` | float\|null | Confidence in victim count (0.0–1.0) |
| `sources_consulted` | string[] | URLs of sources used for verification |
| `overall_confidence` | float | Overall enrichment confidence (0.0–1.0) |
| `reasoning` | string\|null | Free-text reasoning for the enrichment decisions |

---

### ASDClassifications: reasoning_json

Stored in `ASDRiskClassifications.reasoning_json`. Contains GPT-4o's detailed reasoning for the risk classification. Corresponds to the `ClassificationReasoning` Pydantic model.

```json
{
  "severity_reasoning": "The breach affected 9.7 million customers...",
  "stakeholder_reasoning": "Medibank is a large regulated health insurer...",
  "impact_reasoning": "Sustained disruption to healthcare services...",
  "information_quality": "Multiple authoritative sources confirm the breach scope..."
}
```

| Field | Type | Description |
|-------|------|-------------|
| `severity_reasoning` | string | Justification for the C1–C6 severity category |
| `stakeholder_reasoning` | string | Justification for the stakeholder category |
| `impact_reasoning` | string | Justification for the impact type |
| `information_quality` | string | Assessment of the quality of available information |

---

### IndustryGroupings: keywords

Stored in `IndustryGroupings.keywords`. JSON array of keyword strings that map entities to this industry group.

```json
["healthcare", "hospital", "medical", "pharmaceutical", "clinic"]
```

---

## Data Files

### OAIC Statistics Files

#### `oaic_cyber_statistics_<timestamp>.json`

**Location:** Project root
**Role:** Intermediate artifact — pipeline between OAIC scraping and dashboard generation
**Created by:** `scripts/oaic/oaic_data_scraper.py`, `scripts/oaic/OAIC_dashboard_scraper.py`
**Consumed by:** `scripts/oaic/cleanup_oaic_data.py`, `scripts/oaic/OAIC_dashboard_scraper.py`, `scripts/build_static_dashboard.py`

Each file contains an array of OAIC reporting period records:

| Field | Type | Nullable | Description |
|-------|------|----------|-------------|
| `title` | string | No | Report title (e.g., "Notifiable Data Breaches Report: July–December 2019") |
| `url` | string | No | URL of the OAIC report page |
| `year` | integer | No | Reporting year |
| `period` | string | No | Reporting period: `H1` (Jan–Jun) or `H2` (Jul–Dec) |
| `quarter` | string | No | Same as `period` (legacy field) |
| `start_month` | integer | No | Start month of reporting period (1 or 7) |
| `end_month` | integer | No | End month of reporting period (6 or 12) |
| `total_notifications` | integer | Yes | Total breach notifications in the period |
| `cyber_incidents_total` | integer | Yes | Total cyber-related incidents |
| `cyber_incidents_percentage` | integer | Yes | Percentage that were cyber incidents |
| `malicious_attacks` | integer | Yes | Count or percentage of malicious attacks |
| `human_error` | integer | Yes | Count or percentage of human error breaches |
| `system_faults` | integer | Yes | Count or percentage of system fault breaches |
| `ransomware` | integer | Yes | Count or percentage of ransomware incidents |
| `phishing` | integer | Yes | Count or percentage of phishing incidents |
| `hacking` | integer | Yes | Count or percentage of hacking incidents |
| `brute_force` | integer | Yes | Count or percentage of brute force attacks |
| `malware` | integer | Yes | Count or percentage of malware incidents |
| `compromised_credentials` | integer | Yes | Count or percentage of credential compromises |
| `top_sectors` | object[] | Yes | Array of `{sector: string, notifications: integer}` |
| `key_findings` | string[] | Yes | Array of key finding text strings |
| `pdf_url` | string | Yes | URL of the original OAIC PDF report |
| `individuals_affected_median` | integer | Yes | Median individuals affected per breach |
| `individuals_affected_average` | integer | Yes | Average individuals affected per breach |
| `pdf_parsed` | boolean | Yes | Whether the PDF was successfully parsed |
| `pdf_parsing_errors` | string[] | Yes | Array of parsing error messages |

---

#### `oaic_cyber_statistics_<timestamp>.csv`

**Location:** Project root
**Role:** Output artifact (human review)
**Created by:** `scripts/oaic/oaic_data_scraper.py` (with `--output csv` or `--output both`)

Flat version of the JSON with these columns:

`year, period, quarter, start_month, end_month, total_notifications, cyber_incidents_total, cyber_incidents_percentage, malicious_attacks, human_error, system_faults, ransomware, phishing, hacking, brute_force, malware, compromised_credentials, title, url`

---

#### `oaic_cyber_statistics_cleaned_<timestamp>.json`

**Location:** Project root
**Role:** Authoritative merged OAIC dataset
**Created by:** `scripts/oaic/cleanup_oaic_data.py`
**Consumed by:** `scripts/build_static_dashboard.py` (selected by glob as most recent)

Same schema as `oaic_cyber_statistics_<timestamp>.json`, but filtered to H1/H2 periods only (quarterly data dropped), with data quality corrections applied and multiple sources merged.

---

#### `oaic_pdf_vs_dashboard_comparison_<timestamp>.json`

**Location:** Project root
**Role:** Validation artifact (human review)
**Created by:** `scripts/oaic/OAIC_dashboard_scraper.py`

Side-by-side comparison of PDF-scraped vs. Power BI dashboard-scraped data:

```json
[
  {
    "period": "Jul-Dec 2024",
    "pdf_data": { "...full OAIC record..." },
    "dashboard_data": { "...full OAIC record..." }
  }
]
```

---

#### `oaic_screenshots/<timestamp>/metadata.json`

**Location:** `oaic_screenshots/` subdirectory
**Role:** Audit trail for dashboard scraping sessions
**Created by:** `scripts/oaic/OAIC_dashboard_scraper.py`

| Field | Type | Description |
|-------|------|-------------|
| `scraped_at` | string | ISO timestamp of the scraping session |
| `semesters` | string[] | Semesters scraped (e.g., `["Jan-Jun 2024", "Jul-Dec 2024"]`) |
| `output_file` | string | Path to the output JSON file |
| `comparison_file` | string | Path to the comparison JSON file (if generated) |

---

### Calibration Files (`evaluation/*_calibration.json`)

**Role:** the measured cut-off a data-quality step acts at, and what it was measured on
**Created by:** `scripts/calibrate_*.py`, `scripts/dedup_admin.py recalibrate`
**Consumed by:** each step's `active_calibration()`; a step acts only when the file matches the current identity

| Field | Type | Description |
|-------|------|-------------|
| `question_version` | string | Question and escalation versions the cases were scored with |
| `model` | string | `<Jev model>+<escalation model>` (the Jev model alone for the source gate) |
| `evidence` | string | **Evidence fingerprint** (`<reader>:<16 hex>`, `dedup/calibration_guard.py`): a hash of the reader's `EVIDENCE_VERSION` and the parameters that shape what a model is shown. Absent in calibrations measured before 2026-10-29, which therefore do not apply |
| `evidence_attestation` | string | Only where the evidence was attested unchanged instead of re-measured (the source gate) |
| `cutoff` | number or null | Null: no cut-off met the precision target, the step reports only |
| `calibrated_on_database` | string | The labelling-time snapshot (or recalibration copy) scored |
| `recalibration` | object | Present when `dedup_admin.py recalibrate` wrote the file: reason, labelling snapshot, the previous evidence, cut-off and `labelled_used`, cases scored |
| `labelled_used`, `curve`, `bootstrap_out_of_sample_precision`, ... | | The measurement (see the calibration script's docstring) |
| `labelled_rows` | object | Per automatic action (`reject`, `merge`, `split`, `fill`, ...), the labelled rows the cut-off was chosen on: `{id, stratum, weight, p, positive}` (`positive` = the action would be right; `p` null = never acted on). Written since 2026-10-30; read by `scripts/evaluate_calibrations.py` |

`evaluation/calibration_snapshots.json` maps each step to the labelling-time backup it is recalibrated on.

**Deployment rule.** A current calibration acts only when every automatic action passes
`evaluation/deployment_policy.json` (`confidence`, `precision_floor`, `min_labelled_actions`,
`require_heldout_evaluation`, `heldout_folds`, with a `rationale` per key; `dedup/deployment.py`).
`evaluation/<step>_heldout.json` (written by `scripts/evaluate_calibrations.py` and by every calibration script)
holds the held-out evaluation of one calibration: `calibration` (its `question_version`, `model`, `evidence`,
`generated` - the rule uses the file only when all four match), `rows_source`, and per action `cutoff`, `full`
(counts, `weighted_precision`, Kish `effective_n` and the exact lower bounds at the committed cut-off),
`reproduces` / `reproduction_note` (the rows re-choose the committed cut-off and give its counts), and `heldout`
(pooled counts over `folds` folds keyed by `sha256(step|subject id)`, `per_fold` cut-offs, `folds_without_cutoff`).
`rows` holds rows extracted from a calibration database; `seeded` (duplicates, with `--dedup-db`) the
`evaluate_dedup.py` results, reported only.
`ModelJudgements.subject_type` also takes `event_member_same_org` (`check-same-organisation`).

`evaluation/event_scope_calibration.json` (`check-scope`) is calibrated on `evaluation/event_scope_labels.csv`
(`deduplicated_event_id, label, rationale, labeller`; label `in_scope` / `out_of_scope` / `unsure`) and
`evaluation/event_scope_sample.csv` (`deduplicated_event_id, stratum, weight`). Besides the fields above it records
`labelled_out_of_scope` and `extra_labelled_outside_sample` (event-reality labels folded in as stratum `X`).
`ModelJudgements` rows for scope have `question_id` `is_in_scope` (`event-scope-v1`, Jev) and `is_in_scope_escalated`
(`event-scope-escalation-v1`, reasoning model), subject type `deduplicated_event`; value is P(in scope).
Rejections are ledger batches `reject-event-scope-<timestamp>`.

`evaluation/duplicate_calibration.json` and `evaluation/ai_duplicate_calibration.json` also carry `different_cutoff`
(and `different_at_cutoff`, `different_bootstrap_out_of_sample_precision`): the largest final score below which the labelled
pairs are, weighted, at least 95% labelled `different` and none is labelled `same`. It is the only way a stored low score
vetoes a link (`duplicate_judgement.partition_without_conflicts`); a calibration without it vetoes nothing.

### AI duplicate pairs (`evaluation/ai_duplicate_*`)

**Role:** duplicate candidates among AI-involved events whatever their victim, with a cut-off of their own
**Created by:** `scripts/calibrate_ai_duplicates.py` (`score`, `sample`, `packet`, `calibrate`); labels by blind labellers
**Consumed by:** `dedup/ai_duplicates.py` (`check-duplicates`), `dedup/label_contradictions.py` (the ledger refuses a merge across a `different` label), `dedup_admin.py recalibrate --step ai-duplicates`

| File | Columns / fields |
|---|---|
| `ai_duplicate_sample.csv` | `pair_id` (the two event ids, sorted, joined by `\|`), `stratum` (`low` below 0.30, `census` 0.30-0.90, `high` 0.90 and above, on the stored final score), `weight` (stratum size / number sampled) |
| `ai_duplicate_labels.csv` | `pair_id,label,rationale,corroboration,labeller`; label `same`, `different` or `unsure` (the definitions of `duplicate_labels.csv`; see `ai_duplicate_labelling_guide.md`) |
| `ai_duplicate_calibration.json` | the calibration-file fields above, plus `window_days`, `labels_sha256`, `sample_file`, `at_cutoff`, `min_merges`, `organisation_route_cutoff` (the organisation route's, for comparison) |

#### `FinalDecisions`, `EntityAliasDecisionHistory`

`FinalDecisions` is the final score a judging step acts on, tied to the evidence it was made on (`dedup/final_decisions.py`).
Primary key (`step`, `subject_id`, `evidence_hash`, `question_version`, `jev_model`, `escalation_model`).

| Column | Type | Description |
|---|---|---|
| `step` | TEXT | `event-reality`, `event-scope`, `coherence`, `coherence-same-organisation`, `victim-attribution`, `duplicates-organisation` or `duplicates-ai` |
| `subject_id` | TEXT | the event id; `event\|record` for a member; `event\|entity` for an event-victim pair; the two event ids, sorted, joined by `\|`, for a duplicate pair |
| `evidence_hash` | TEXT | SHA-256 of the state exactly as Jev and the escalation were shown it (the subject's records, research, page text, timeline and whatever else the step shows) |
| `question_version` | TEXT | Jev question version + `+` + escalation question version |
| `jev_model`, `escalation_model` | TEXT | the requested models |
| `final_score` | REAL | the probability acted on: the escalation's inside the step's escalation band, else Jev's |
| `basis` | TEXT | `jev` or `escalated` |
| `jev_input_hash`, `escalation_input_hash` | TEXT | the `ModelJudgements.input_hash` of the answers it rests on (the second NULL when `basis = 'jev'`) |
| `decided_at` | TEXT | ISO time |

A subject whose escalation failed has no row. A row is a subject's score only while its `evidence_hash` equals the hash of the
subject's current state; otherwise it is stale: every `stored_final_probabilities` ignores it, and the duplicate check asks a
stale low pair again only where it could block a proposed merge (`duplicate_judgement.calibrated_negatives`). Replaces
`PairDecisions` (introduced and replaced within 2026-10-29, never in a released database).
`EntityAliasDecisionHistory` has `EntityAliasDecisions`'s columns plus
`superseded_at` and `superseded_by` (the new verdict): the decision a re-ask on changed evidence replaced.

`ModelJudgements` rows for these pairs: `subject_type = 'event_pair'`, `question_id = 'same_incident_ai'`
(`same-incident-ai-v1`) and, for scores Jev puts in 0.2-0.8, `same_incident_ai_escalated` (`primitive = 'escalation'`).
The organisation route's rows use `same_incident` / `same_incident_escalated`. `DedupOverrides` rulings and the label
files of both routes protect AI pairs the same way.

### ASD Risk Classification Exports

All files in the `risk_matrix/` directory. Created by `scripts/asd_risk_classifier.py`.

#### `risk_matrix/asd_risk_classifications_<timestamp>.csv`

**Role:** Output artifact (human review)

| Column | Type | Description |
|--------|------|-------------|
| `deduplicated_event_id` | string | FK to DeduplicatedEvents |
| `title` | string | Event title |
| `event_date` | string | Event date (YYYY-MM-DD) |
| `event_type` | string | Event type classification |
| `victim_organization_name` | string | Victim organization name |
| `victim_organization_industry` | string | Victim industry |
| `records_affected` | integer | Number of records affected |
| `severity_category` | string | ASD severity: C1–C6 |
| `primary_stakeholder_category` | string | Affected stakeholder group |
| `impact_type` | string | Type of impact |
| `confidence_score` | float | Classification confidence (0.0–1.0) |
| `model_used` | string | LLM model used |
| `created_at` | string | Classification timestamp |

---

#### `risk_matrix/asd_risk_classifications_<timestamp>.json`

**Role:** Output artifact (audit/human review)

Same fields as the CSV above, plus a nested `reasoning` object:

```json
{
  "reasoning": {
    "severity_reasoning": "...",
    "stakeholder_reasoning": "...",
    "impact_reasoning": "...",
    "information_quality": "..."
  }
}
```

---

#### `risk_matrix/risk_matrix.xlsx` and `risk_matrix/risk_matrix_<year>.xlsx`

**Role:** Output artifact (human analysis/reporting)
**Sheet name:** `Risk Matrix`

Pivot table of event counts:

| Column | Description |
|--------|-------------|
| `Impact Type` | Row label — type of cyber impact |
| `Member(s) of the public` | Count of events affecting public members |
| `Small organisation(s) / Sole traders` | Count of events affecting small orgs |
| `Medium-sized organisation(s) / Schools / Local government` | Count for medium orgs |
| `State government / Academia/R&D / Large organisation(s) / Supply chain` | Count for large orgs |
| `Federal government / Government shared services / Regulated critical infrastructure` | Count for federal/CI |
| `National security / Systems of National Significance` | Count for national security |

One file per year (2020–2026) plus one combined `risk_matrix.xlsx`.

---

### Machine Learning Artifacts

All files in the `machine_learning_filter/` directory.

#### `event_training_data.csv` / `event_training_data.xlsx`

**Role:** Input artifact — labeled training data for the Random Forest classifier
**Created by:** Manually labeled offline (exported from RawEvents with human labels)

| Column | Type | Description |
|--------|------|-------------|
| `raw_event_id` | string | Event identifier |
| `source_type` | string | Data source type |
| `source_event_id` | string | External source ID |
| `raw_title` | string | Event title |
| `raw_description` | string | Event description |
| `raw_content` | string | Full article content |
| `event_date` | string | Event date |
| `source_url` | string | Source URL |
| `source_metadata` | string | JSON metadata |
| `discovered_at` | string | Discovery timestamp |
| `is_processed` | boolean | Processing flag |
| `processing_attempted_at` | string | Processing timestamp |
| `processing_error` | string | Error message |
| `created_at` | string | Creation timestamp |
| `filter_keep` | integer | **Label column:** 0 = discard, 1 = keep as relevant cyber event |

---

#### `random_forest_filter.pkl`

**Role:** Input artifact — deployed ML model
**Format:** Python pickle (scikit-learn `RandomForestClassifier`)
**Consumed by:** `cyber_data_collector/filtering/rf_event_filter.py`

Serialized Random Forest model trained on the labeled training data. Predicts whether a raw event is a relevant Australian cyber event.

---

#### `source_type_encoder.pkl`

**Role:** Input artifact — ML preprocessor
**Format:** Python pickle (scikit-learn `LabelEncoder`)
**Consumed by:** `cyber_data_collector/filtering/rf_event_filter.py`

Encodes the `source_type` categorical field into numeric features for the Random Forest.

---

#### `text_vectorizer.pkl`

**Role:** Input artifact — ML preprocessor
**Format:** Python pickle (scikit-learn `TfidfVectorizer`)
**Consumed by:** `cyber_data_collector/filtering/rf_event_filter.py`

Converts raw event text (title + description) into TF-IDF feature vectors for the Random Forest.

---

### Authentication Files

#### `client_secrets.json`

**Location:** Project root
**Role:** Input credential file (do not commit)
**Created by:** Downloaded from Google Cloud Console
**Consumed by:** `scripts/setup/setup_bigquery_auth.py`

Google OAuth2 client secrets for BigQuery/GDELT authentication. Contains `client_id`, `client_secret`, `redirect_uris`.

---

#### `bigquery_token.json`

**Location:** Project root
**Role:** Input/output credential file (do not commit)
**Created by:** `scripts/setup/setup_bigquery_auth.py` after OAuth flow
**Consumed by:** `scripts/setup/setup_bigquery_auth.py`, `cyber_data_collector/datasources/gdelt.py`

OAuth access/refresh token for BigQuery in Google's `authorized_user` format.

---

### Dashboard Output

#### `dashboard/index.html`

**Location:** `dashboard/`
**Role:** Output artifact — self-contained interactive dashboard
**Created by:** `scripts/build_static_dashboard.py`
**Data sources:** SQLite database + `oaic_cyber_statistics_*.json` files

Fully self-contained HTML file with:
- All event data embedded as inline JavaScript variables
- Chart.js visualizations (event trends, type distribution, severity breakdown)
- OAIC breach notification trends
- ASD risk matrices for all years
- Interactive filtering and search

---

### Export Outputs

#### On-demand CSV/Excel exports

**Created by:** `scripts/export/export_cyber_events.py`, `scripts/export/export_events_excel.py`
**Role:** Output artifacts (user-specified filenames)

**Full export** (`export_cyber_events.py`):
- All columns from any table, or a detailed join of DeduplicatedEvents + entities + sources
- Entities and sources serialized as JSON strings within CSV cells
- Supports `--format csv` or `--format excel`, `--detailed`, `--anonymize`

**Clean export** (`export_events_excel.py`):
7-column Excel with LLM-summarized and optionally anonymized descriptions:

| Column | Description |
|--------|-------------|
| `Event Date` | Date of the cyber event |
| `Event Title` | Event title |
| `Event Description` | LLM-summarized description |
| `Anonymised Description` | Description with entity names removed |
| `Records Affected` | Number of records affected |
| `Entity Type` | Type of affected entity |
| `Attack Type` | Type of cyber attack |

---

## Enumerations and Controlled Vocabularies

### CyberEventType

Defined in `cyber_data_collector/models/events.py`. Used in `EnrichedEvents.event_type` and `DeduplicatedEvents.event_type`.

| Value | Description |
|-------|-------------|
| `Ransomware` | Ransomware attack |
| `Data Breach` | Unauthorized data exposure |
| `Phishing` | Phishing/social engineering attack |
| `Malware` | Malware infection |
| `Vulnerability Exploit` | Exploitation of a known/unknown vulnerability |
| `State-Sponsored Attack` | Nation-state cyber operation |
| `Supply Chain Attack` | Attack via supply chain compromise |
| `Insider Threat` | Malicious or negligent insider |
| `Denial of Service` | DDoS or DoS attack |
| `Financial Fraud` | Cyber-enabled financial fraud |
| `Identity Theft` | Identity theft via cyber means |
| `Infrastructure Attack` | Attack on critical infrastructure |
| `Other` | Uncategorized cyber event |

---

### EventSeverity

Defined in `cyber_data_collector/models/events.py`. Used in `EnrichedEvents.severity` and `DeduplicatedEvents.severity`.

| Value | Description |
|-------|-------------|
| `Critical` | Severe, widespread impact |
| `High` | Significant impact |
| `Medium` | Moderate impact |
| `Low` | Minor impact |
| `Unknown` | Severity not determined |

---

### EntityType

Defined in `cyber_data_collector/models/events.py`. Used in `EntitiesV2.entity_type`.

| Value | Description |
|-------|-------------|
| `Government` | Government agency |
| `Financial Institution` | Bank, insurer, financial services |
| `Healthcare` | Hospital, health insurer, medical provider |
| `Education` | University, school, education provider |
| `Technology` | Technology company |
| `Retail` | Retail business |
| `Energy/Utilities` | Energy or utilities provider |
| `Telecommunications` | Telco provider |
| `Manufacturing` | Manufacturing company |
| `Transportation` | Transport or logistics |
| `Media` | Media or publishing |
| `Non-Profit` | Non-profit organization |
| `Individual` | Individual person |
| `Other` | Other entity type |

---

### SeverityCategory (ASD Risk Matrix)

Defined in `scripts/asd_risk_classifier.py`. Used in `ASDRiskClassifications.severity_category`.

| Value | Description |
|-------|-------------|
| `C1` | Category 1 — most severe (e.g., sustained disruption to national security) |
| `C2` | Category 2 — very high severity |
| `C3` | Category 3 — high severity |
| `C4` | Category 4 — moderate severity |
| `C5` | Category 5 — low severity |
| `C6` | Category 6 — least severe (e.g., unsuccessful low-level attack) |

---

### StakeholderCategory

Defined in `scripts/asd_risk_classifier.py`. Used in `ASDRiskClassifications.primary_stakeholder_category`.

| Value |
|-------|
| `Member(s) of the public` |
| `Small organisation(s)` |
| `Sole traders` |
| `Medium-sized organisation(s)` |
| `Schools` |
| `Local government` |
| `State government` |
| `Academia/R&D` |
| `Large organisation(s)` |
| `Supply chain` |
| `Federal government` |
| `Government shared services` |
| `Regulated critical infrastructure` |
| `National security` |
| `Systems of National Significance` |

---

### ImpactType

Defined in `scripts/asd_risk_classifier.py`. Used in `ASDRiskClassifications.impact_type`.

| Value |
|-------|
| `Sustained disruption of essential systems and associated services` |
| `Extensive compromise` |
| `Isolated compromise` |
| `Coordinated low-level malicious attack` |
| `Low-level malicious attack` |
| `Unsuccessful low-level malicious attack` |

---

### Vulnerability Categories

Defined in `cyber_data_collector/models/vulnerability_taxonomy.py`. Used as a controlled vocabulary for attack classification.

`Authentication Weakness`, `Access Control Failure`, `Injection Attacks`, `Phishing/Social Engineering`, `Ransomware`, `Malware`, `Configuration Error`, `Unpatched Software`, `Supply Chain Attack`, `Zero-Day Exploit`, `DDoS Attack`, `Insider Threat`, `Physical Security`, `API Vulnerability`, `Cross-Site Scripting (XSS)`, `Business Logic Flaw`, `Cryptographic Failure`, `Unknown/Other`

---

## Data Source Mapping

How each external data source flows through the system.

### Perplexity AI

```
Perplexity API → PerplexityEvent (Pydantic)
  → CyberEvent (Pydantic) → RawEvents (DB)
  → [ML Filter + LLM Filter]
  → EnrichedEvents (DB) + EntitiesV2 (DB)
  → [Perplexity Enrichment] → perplexity_enrichment_data (JSON column)
  → DeduplicatedEvents (DB)
  → ASDRiskClassifications (DB)
```

### OAIC (Office of the Australian Information Commissioner)

OAIC data follows **two parallel paths**:

**Path 1 — Aggregate statistics (JSON files):**
```
OAIC PDF Reports → oaic_data_scraper.py → oaic_cyber_statistics_*.json
OAIC Power BI Dashboard → OAIC_dashboard_scraper.py → oaic_cyber_statistics_*.json
  → cleanup_oaic_data.py → oaic_cyber_statistics_cleaned_*.json
  → build_static_dashboard.py → dashboard/index.html (OAIC trends section)
```

**Path 2 — Individual breach events (database):**
```
OAIC breach listings → RawEvents (source_type='OAIC')
  → EnrichedEvents → DeduplicatedEvents → ASDRiskClassifications
```

### Google Custom Search

```
Google Search API → RawEvents (source_type='GoogleSearch')
  → [Playwright scraping for full content]
  → [ML Filter + LLM Filter]
  → EnrichedEvents → DeduplicatedEvents → ASDRiskClassifications
```

### Webber Insurance

```
Webber Insurance timeline (web scraping) → RawEvents (source_type='WebberInsurance')
  → [ML Filter + LLM Filter]
  → EnrichedEvents → DeduplicatedEvents → ASDRiskClassifications
```

### GDELT (via BigQuery)

```
BigQuery GDELT tables → RawEvents (source_type='GDELT')
  → [Playwright scraping for full content]
  → [ML Filter + LLM Filter]
  → EnrichedEvents → DeduplicatedEvents → ASDRiskClassifications
```

---

*Last updated: March 2026*
