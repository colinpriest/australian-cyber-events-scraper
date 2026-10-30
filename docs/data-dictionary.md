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
| `event_date_basis` | TEXT | Yes | NULL | Where the source's `event_date` comes from (e.g. `Webber list heading (month)`, `stated in the source page`) |
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
| SourceMonthDiscovery | `source_type`, `year`, `month` | **PK** | One source's search of one month |
| SourceMonthDiscovery | `status` | TEXT | `complete` or `failed` |
| SourceMonthDiscovery | `error_type`, `error_types_json` | TEXT | The deciding type and the type of each failure (`throttled`, `missing`, `network`, `parse`, `incomplete`, `other`, `unknown`) |
| SourceMonthDiscovery | `failed_attempts`, `next_attempt_at` | | Consecutive failures and the back-off (30 min doubling to 1 day; 6 h doubling to 7 days for `other` / `unknown`) |
| SourceMonthDiscovery | `gave_up_at` | TEXT | Set when the month is **given up** (permanent: `missing`, at once) or **parked** (temporary or unknown failures that reached 5 attempts) |
| SourceMonthDiscovery | `give_up_reason` | TEXT | `permanent`, `temporary` or `unknown`: only `permanent` waits for `discovery_queue.py release` |
| SourceMonthDiscovery | `give_up_config` | TEXT | JSON of the source configuration it failed under: hashed secrets (`secret:<ENV>`), `query` version, `client:<package>` versions (`processing/source_config.py`). A parked month reopens when this differs from the current configuration |
| SourceMonthDiscovery | `reopened_at`, `reopen_reason` | TEXT | Last automatic reopening: `probe succeeded` or `configuration changed: <components>` |
| SourceMonthDiscovery | `items_seen` | INTEGER | Items the source listed for the month (Cyber Daily: feed articles) |
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
earlier rows; the reconstruction uses the current value for them and reports it).

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
