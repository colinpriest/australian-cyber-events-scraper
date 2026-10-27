-- Base schema: every table, index and view the pipeline uses, as of 2026-10-27.
-- Generated once from the production database's structure (no data).
-- Applied by `python pipeline.py init`; later changes are new numbered files.
-- Columns that modules add at run time (ALTER TABLE ... ADD COLUMN guarded by
-- PRAGMA table_info) are already present here, so those guards are no-ops.

CREATE TABLE IF NOT EXISTS ASDRiskClassifications (
            classification_id TEXT PRIMARY KEY,
            deduplicated_event_id TEXT UNIQUE NOT NULL,
            severity_category VARCHAR(2) NOT NULL CHECK(severity_category IN ('C1', 'C2', 'C3', 'C4', 'C5', 'C6')),
            primary_stakeholder_category VARCHAR(255) NOT NULL,
            impact_type VARCHAR(100) NOT NULL,
            reasoning_json TEXT NOT NULL,
            confidence_score REAL NOT NULL CHECK(confidence_score >= 0.0 AND confidence_score <= 1.0),
            model_used VARCHAR(50) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (deduplicated_event_id) REFERENCES DeduplicatedEvents(deduplicated_event_id) ON DELETE CASCADE
        );

CREATE TABLE IF NOT EXISTS AuditLog (
            audit_id TEXT PRIMARY KEY,
            table_name VARCHAR(50) NOT NULL,
            record_id TEXT NOT NULL,
            action VARCHAR(20) NOT NULL,  -- 'INSERT', 'UPDATE', 'DELETE'
            old_values TEXT,              -- JSON
            new_values TEXT,              -- JSON
            changed_by TEXT,              -- System/User identifier
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE IF NOT EXISTS DataSourcesV2 (
            source_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name VARCHAR(100) UNIQUE NOT NULL,
            source_type VARCHAR(50),
            base_url TEXT,
            api_endpoint TEXT,
            credibility_score REAL,
            last_accessed TIMESTAMP,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE IF NOT EXISTS DedupCalibration (
        calibration_id  TEXT PRIMARY KEY,
        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        sample_size     INTEGER NOT NULL,
        false_merges    INTEGER NOT NULL,
        missed_merges   INTEGER NOT NULL,
        merge_threshold REAL NOT NULL,
        notes           TEXT
    );

CREATE TABLE IF NOT EXISTS DedupDecisions (
        decision_id            TEXT PRIMARY KEY,
        batch_id               TEXT NOT NULL,
        enriched_event_id      TEXT NOT NULL,
        deduplicated_event_id  TEXT,
        cluster_key            TEXT,
        action                 TEXT NOT NULL CHECK (action IN ('merge','keep_separate','revert')),
        decided_by             TEXT NOT NULL CHECK (decided_by IN ('rule','embedding','llm','human')),
        certainty              REAL CHECK (certainty IS NULL OR (certainty >= 0.0 AND certainty <= 1.0)),
        method                 TEXT,
        reasoning              TEXT,
        evidence_json          TEXT,
        superseded_by          TEXT,
        created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

CREATE TABLE IF NOT EXISTS DedupOverrides (
        override_id              TEXT PRIMARY KEY,
        pair_key                 TEXT NOT NULL UNIQUE,
        left_enriched_event_id   TEXT NOT NULL,
        right_enriched_event_id  TEXT NOT NULL,
        verdict                  TEXT NOT NULL CHECK (verdict IN ('same','different')),
        reason                   TEXT,
        created_by               TEXT DEFAULT 'human',
        created_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        active                   INTEGER NOT NULL DEFAULT 1
    );

CREATE TABLE IF NOT EXISTS DedupSnapshots (
        snapshot_id            TEXT PRIMARY KEY,
        batch_id               TEXT NOT NULL,
        deduplicated_event_id  TEXT NOT NULL,
        payload_json           TEXT NOT NULL,
        created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

CREATE TABLE IF NOT EXISTS DeduplicatedEventEntities (
            deduplicated_event_id TEXT,
            entity_id INTEGER,
            relationship_type VARCHAR(50) DEFAULT 'affected',
            confidence_score REAL,
            source_count INTEGER DEFAULT 1,            -- How many sources mentioned this entity

            PRIMARY KEY (deduplicated_event_id, entity_id),
            FOREIGN KEY (deduplicated_event_id) REFERENCES DeduplicatedEvents(deduplicated_event_id) ON DELETE CASCADE,
            FOREIGN KEY (entity_id) REFERENCES EntitiesV2(entity_id) ON DELETE CASCADE
        );

CREATE TABLE IF NOT EXISTS DeduplicatedEventSources (
            deduplicated_event_id TEXT,
            source_url VARCHAR(2048),
            source_type VARCHAR(50),
            credibility_score REAL,
            content_snippet TEXT,
            discovered_at TIMESTAMP,

            PRIMARY KEY (deduplicated_event_id, source_url),
            FOREIGN KEY (deduplicated_event_id) REFERENCES DeduplicatedEvents(deduplicated_event_id) ON DELETE CASCADE
        );

CREATE TABLE IF NOT EXISTS DeduplicatedEvents (
            deduplicated_event_id TEXT PRIMARY KEY,
            master_enriched_event_id TEXT NOT NULL,  -- References the "best" enriched event
            title VARCHAR(255) NOT NULL,
            description TEXT,
            summary TEXT,
            event_type VARCHAR(50),
            severity VARCHAR(20),
            event_date DATE,
            records_affected BIGINT,
            is_australian_event BOOLEAN NOT NULL,
            is_specific_event BOOLEAN NOT NULL,
            confidence_score REAL,
            australian_relevance_score REAL,

            -- Aggregated data from all contributing events
            total_data_sources INTEGER DEFAULT 0,      -- Number of contributing data sources
            contributing_raw_events INTEGER DEFAULT 0,  -- Number of raw events that contributed
            contributing_enriched_events INTEGER DEFAULT 0, -- Number of enriched events merged

            -- Deduplication metadata
            similarity_score REAL,                     -- Average similarity of merged events
            deduplication_method VARCHAR(50),          -- 'title_similarity', 'entity_match', 'manual'

            status VARCHAR(20) DEFAULT 'Active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, attacking_entity_id INTEGER, attacking_entity_name VARCHAR(255), attack_method VARCHAR(100), victim_organization_name VARCHAR(255), victim_organization_industry VARCHAR(100), perplexity_validated BOOLEAN DEFAULT FALSE, perplexity_validated_at TIMESTAMP, perplexity_enrichment_data TEXT, date_confidence REAL, entity_confidence REAL, data_source_reliability REAL, personal_info_types_json TEXT, breach_source_category VARCHAR(40), dedup_certainty REAL, dedup_method TEXT, dedup_batch_id TEXT, has_human_override INTEGER NOT NULL DEFAULT 0, vendor_organization_name TEXT, vendor_organization_industry TEXT, roles_member_signature TEXT, roles_classified_at TIMESTAMP,

            FOREIGN KEY (master_enriched_event_id) REFERENCES EnrichedEvents(enriched_event_id) ON DELETE CASCADE
        );

CREATE TABLE IF NOT EXISTS DeduplicationClusters (
            cluster_id TEXT PRIMARY KEY,
            deduplicated_event_id TEXT NOT NULL,
            cluster_size INTEGER NOT NULL,             -- Number of events in this cluster
            average_similarity REAL,                   -- Average pairwise similarity
            deduplication_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            algorithm_version VARCHAR(20),             -- Version of deduplication algorithm used

            FOREIGN KEY (deduplicated_event_id) REFERENCES DeduplicatedEvents(deduplicated_event_id) ON DELETE CASCADE
        );

CREATE TABLE IF NOT EXISTS EnrichedEventEntities (
            enriched_event_id TEXT,
            entity_id INTEGER,
            relationship_type VARCHAR(50) DEFAULT 'affected',  -- 'primary_target', 'affected', 'mentioned'
            confidence_score REAL,
            PRIMARY KEY (enriched_event_id, entity_id),
            FOREIGN KEY (enriched_event_id) REFERENCES EnrichedEvents(enriched_event_id) ON DELETE CASCADE,
            FOREIGN KEY (entity_id) REFERENCES EntitiesV2(entity_id) ON DELETE CASCADE
        );

CREATE TABLE IF NOT EXISTS EnrichedEvents (
            enriched_event_id TEXT PRIMARY KEY,
            raw_event_id TEXT NOT NULL,
            title VARCHAR(255) NOT NULL,
            description TEXT,
            summary TEXT,
            event_type VARCHAR(50),
            severity VARCHAR(20),
            event_date DATE,                    -- Now nullable!
            records_affected BIGINT,
            is_australian_event BOOLEAN NOT NULL,
            is_specific_event BOOLEAN NOT NULL,
            confidence_score REAL,
            australian_relevance_score REAL,
            status VARCHAR(20) DEFAULT 'Active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, perplexity_validated BOOLEAN DEFAULT FALSE, perplexity_validated_at TIMESTAMP, perplexity_enrichment_data TEXT, date_confidence REAL, entity_confidence REAL, data_source_reliability REAL, attacking_entity_id INTEGER, attacking_entity_name VARCHAR(255), attack_method VARCHAR(100), enrichment_pipeline_version VARCHAR(20) DEFAULT 'v1_regex', enrichment_confidence REAL, enrichment_method VARCHAR(50) DEFAULT 'regex', last_enrichment_audit_id TEXT,
            FOREIGN KEY (raw_event_id) REFERENCES RawEvents(raw_event_id) ON DELETE CASCADE
        );

CREATE TABLE IF NOT EXISTS EnrichmentAuditTrail (
    audit_id TEXT PRIMARY KEY,  -- UUID for audit trail
    enriched_event_id TEXT NOT NULL,  -- Reference to EnrichedEvents (may not exist yet for new events)
    raw_event_id TEXT,  -- Reference to RawEvents
    pipeline_version VARCHAR(20) NOT NULL DEFAULT '1.0',  -- Pipeline version (e.g., "1.0", "1.1")

    -- Timing
    started_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP,
    total_time_seconds REAL,

    -- Final Decision
    final_decision VARCHAR(30),  -- AUTO_ACCEPT, ACCEPT_WITH_WARNING, REJECT, ERROR
    final_confidence REAL,  -- 0.0 to 1.0

    -- Stage 1: Content Acquisition
    stage1_success BOOLEAN,
    stage1_extraction_method VARCHAR(50),  -- newspaper3k, trafilatura, beautifulsoup
    stage1_content_length INTEGER,  -- word count
    stage1_source_reliability REAL,  -- 0.0 to 1.0
    stage1_details TEXT,  -- JSON

    -- Stage 2: GPT-4o Extraction
    stage2_success BOOLEAN,
    stage2_victim_organization TEXT,
    stage2_confidence REAL,
    stage2_is_specific_incident BOOLEAN,
    stage2_australian_relevance REAL,
    stage2_tokens_used INTEGER,
    stage2_details TEXT,  -- JSON

    -- Stage 3: Perplexity Fact-Checking
    stage3_checks_performed INTEGER DEFAULT 0,
    stage3_checks_passed INTEGER DEFAULT 0,
    stage3_checks_failed INTEGER DEFAULT 0,
    stage3_verification_confidence REAL,
    stage3_details TEXT,  -- JSON

    -- Stage 4: Validation
    stage4_is_valid BOOLEAN,
    stage4_error_count INTEGER DEFAULT 0,
    stage4_warning_count INTEGER DEFAULT 0,
    stage4_validation_confidence REAL,
    stage4_details TEXT,  -- JSON

    -- Stage 5: Decision details are captured in final_decision/final_confidence above
    stage5_stage_confidences TEXT,  -- JSON with breakdown
    stage5_penalties_applied TEXT,  -- JSON

    -- Error handling
    error_message TEXT,
    error_stage VARCHAR(50),  -- Which stage failed

    -- Metadata
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (raw_event_id) REFERENCES RawEvents(raw_event_id)
);

CREATE TABLE IF NOT EXISTS EntitiesV2 (
            entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_name VARCHAR(255) UNIQUE NOT NULL,
            entity_type VARCHAR(50),
            industry VARCHAR(100),
            turnover VARCHAR(50),
            employee_count INTEGER,
            is_australian BOOLEAN,
            headquarters_location TEXT,
            website_url TEXT,
            confidence_score REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        , entity_kind TEXT, size_estimate TEXT, size_confidence REAL, size_employees INTEGER, size_revenue_aud REAL, size_basis TEXT, size_method TEXT, size_sources TEXT, size_estimated_at TIMESTAMP);

CREATE TABLE IF NOT EXISTS EntityAliases (
        alias_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        raw_name       TEXT NOT NULL UNIQUE,
        canonical_name TEXT NOT NULL,
        canonical_key  TEXT NOT NULL,
        source         TEXT NOT NULL DEFAULT 'rule' CHECK (source IN ('rule','llm','human','learned')),
        confidence     REAL,
        created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

CREATE TABLE IF NOT EXISTS EntityMappings (
    entity_mapping_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_entity TEXT NOT NULL,           -- The entity name to map from (e.g., "Ticketmaster LLC")
    canonical_entity TEXT NOT NULL,        -- The canonical entity name to map to (e.g., "Live Nation Entertainment, Inc")
    relationship_type TEXT DEFAULT 'subsidiary',  -- Type: 'subsidiary', 'brand', 'division', 'alias'
    notes TEXT,                            -- Optional notes about the relationship
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_entity)                  -- Each source entity can only map to one canonical entity
);

CREATE TABLE IF NOT EXISTS EventCorroborations (
    deduplicated_event_id TEXT NOT NULL,
    query_hash            TEXT NOT NULL,
    query                 TEXT NOT NULL,
    model                 TEXT NOT NULL,
    answer                TEXT,
    citations_json        TEXT,
    created_at            TEXT NOT NULL,
    PRIMARY KEY (deduplicated_event_id, query_hash)
);

CREATE TABLE IF NOT EXISTS EventDeduplicationMap (
            map_id TEXT PRIMARY KEY,
            raw_event_id TEXT NOT NULL,
            enriched_event_id TEXT,                    -- May be NULL if raw event wasn't enriched
            deduplicated_event_id TEXT NOT NULL,
            contribution_type VARCHAR(50),             -- 'primary', 'supporting', 'duplicate'
            similarity_score REAL,                     -- How similar this event was to the master
            data_source_weight REAL DEFAULT 1.0,      -- Importance weighting of this source
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            UNIQUE(raw_event_id, deduplicated_event_id),
            FOREIGN KEY (raw_event_id) REFERENCES RawEvents(raw_event_id) ON DELETE CASCADE,
            FOREIGN KEY (enriched_event_id) REFERENCES EnrichedEvents(enriched_event_id) ON DELETE CASCADE,
            FOREIGN KEY (deduplicated_event_id) REFERENCES DeduplicatedEvents(deduplicated_event_id) ON DELETE CASCADE
        );

CREATE TABLE IF NOT EXISTS IndustryGroupings (
    industry_group_id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_name TEXT NOT NULL UNIQUE,
    keywords TEXT NOT NULL,  -- JSON array of keywords to match against detailed industries
    display_order INTEGER NOT NULL,  -- Order for display in visualizations
    description TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ModelJudgements (
    judgement_id      TEXT PRIMARY KEY,
    subject_type      TEXT NOT NULL,   -- enriched_event, event_pair, deduplicated_event, ...
    subject_id        TEXT NOT NULL,   -- for a pair: the two ids joined by '|', sorted
    question_id       TEXT NOT NULL,   -- stable name, e.g. 'page_kind'
    question_version  TEXT NOT NULL,   -- bumped whenever the wording or the input changes
    primitive         TEXT NOT NULL,   -- choice / noul / score
    answer            TEXT,            -- chosen option; 'yes'/'no' for a noul at 0.5
    value             REAL,            -- noul probability, score value, or chosen option's probability
    confidence        REAL,            -- choice/score concentration (NULL for a noul)
    probabilities_json TEXT,           -- full distribution as returned
    question_json     TEXT NOT NULL,   -- the question exactly as sent
    input_hash        TEXT NOT NULL,   -- sha256 of state + question + requested model
    model_requested   TEXT NOT NULL,
    model             TEXT,            -- the version that answered
    input_tokens      INTEGER,         -- for the whole request, repeated on each answer
    output_tokens     INTEGER,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS MonthProcessed (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                is_processed BOOLEAN DEFAULT FALSE,
                processed_at TIMESTAMP NULL,
                total_raw_events INTEGER DEFAULT 0,
                total_enriched_events INTEGER DEFAULT 0,
                processing_notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(year, month)
            );

CREATE TABLE IF NOT EXISTS OrganisationTimelines (
    entity_id      INTEGER NOT NULL,
    entity_name    TEXT NOT NULL,
    query_hash     TEXT NOT NULL,
    query          TEXT NOT NULL,
    model          TEXT NOT NULL,
    answer         TEXT,
    citations_json TEXT,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (entity_id, query_hash)
);

CREATE TABLE IF NOT EXISTS PageClassifications (
    enriched_event_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    confidence REAL NOT NULL,
    reasoning TEXT,
    model TEXT,
    created_at TEXT NOT NULL
, question_version TEXT);

CREATE TABLE IF NOT EXISTS ProcessingLog (
            log_id TEXT PRIMARY KEY,
            raw_event_id TEXT NOT NULL,
            processing_stage VARCHAR(50) NOT NULL,  -- 'url_scraping', 'llm_analysis', 'entity_extraction'
            status VARCHAR(20) NOT NULL,            -- 'success', 'failed', 'skipped'
            result_data TEXT,                       -- JSON result data
            error_message TEXT,
            processing_time_ms INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (raw_event_id) REFERENCES RawEvents(raw_event_id) ON DELETE CASCADE
        );

CREATE TABLE IF NOT EXISTS RawEvents (
            raw_event_id TEXT PRIMARY KEY,
            source_type VARCHAR(50) NOT NULL,  -- 'GDELT', 'Perplexity', 'GoogleSearch', 'WebberInsurance'
            source_event_id TEXT,              -- External ID from source system
            raw_title TEXT,
            raw_description TEXT,
            raw_content TEXT,                  -- Full scraped content if applicable
            event_date DATE,
            source_url VARCHAR(2048),
            source_metadata TEXT,              -- JSON for additional source-specific data
            discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_processed BOOLEAN DEFAULT FALSE,
            processing_attempted_at TIMESTAMP NULL,
            processing_error TEXT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE IF NOT EXISTS SourceReplacements (
    enriched_event_id TEXT PRIMARY KEY,
    query             TEXT NOT NULL,
    model             TEXT NOT NULL,
    answer            TEXT,
    citations_json    TEXT,
    raw_event_id      TEXT,          -- the replacement record queued, if any
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_asd_risk_dedup_event_id 
        ON ASDRiskClassifications(deduplicated_event_id);
CREATE INDEX IF NOT EXISTS idx_asd_risk_severity 
        ON ASDRiskClassifications(severity_category);
CREATE INDEX IF NOT EXISTS idx_asd_risk_stakeholder 
        ON ASDRiskClassifications(primary_stakeholder_category);
CREATE INDEX IF NOT EXISTS idx_dd_batch ON DedupDecisions(batch_id);
CREATE INDEX IF NOT EXISTS idx_dd_dedup ON DedupDecisions(deduplicated_event_id);
CREATE INDEX IF NOT EXISTS idx_dd_enriched ON DedupDecisions(enriched_event_id);
CREATE INDEX IF NOT EXISTS idx_dd_live ON DedupDecisions(enriched_event_id, superseded_by);
CREATE INDEX IF NOT EXISTS idx_do_active ON DedupOverrides(active);
CREATE INDEX IF NOT EXISTS idx_do_left ON DedupOverrides(left_enriched_event_id);
CREATE INDEX IF NOT EXISTS idx_do_right ON DedupOverrides(right_enriched_event_id);
CREATE INDEX IF NOT EXISTS idx_ds_batch ON DedupSnapshots(batch_id);
CREATE INDEX IF NOT EXISTS idx_ds_dedup ON DedupSnapshots(deduplicated_event_id);
CREATE INDEX IF NOT EXISTS idx_dedup_sources_type ON DeduplicatedEventSources(source_type);
CREATE INDEX IF NOT EXISTS idx_dedup_attacking_entity
                ON DeduplicatedEvents(attacking_entity_id);
CREATE INDEX IF NOT EXISTS idx_dedup_event_type 
ON DeduplicatedEvents(event_type) 
WHERE event_type IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_dedup_events_australian ON DeduplicatedEvents(is_australian_event);
CREATE INDEX IF NOT EXISTS idx_dedup_events_date ON DeduplicatedEvents(event_date);
CREATE INDEX IF NOT EXISTS idx_dedup_events_type ON DeduplicatedEvents(event_type);
CREATE INDEX IF NOT EXISTS idx_dedup_perplexity_validated
                ON DeduplicatedEvents(perplexity_validated);
CREATE INDEX IF NOT EXISTS idx_dedup_severity 
ON DeduplicatedEvents(severity) 
WHERE severity IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_dedup_status_date 
ON DeduplicatedEvents(status, event_date);
CREATE INDEX IF NOT EXISTS idx_dedup_title ON DeduplicatedEvents(title);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dedup_unique_master
        ON DeduplicatedEvents(master_enriched_event_id)
        WHERE status = 'Active';
CREATE INDEX IF NOT EXISTS idx_eee_role ON EnrichedEventEntities(relationship_type);
CREATE INDEX IF NOT EXISTS idx_enriched_attacking_entity
                ON EnrichedEvents(attacking_entity_id);
CREATE INDEX IF NOT EXISTS idx_enriched_perplexity_validated
                ON EnrichedEvents(perplexity_validated);
CREATE INDEX IF NOT EXISTS idx_enrichedevents_audit
    ON EnrichedEvents(last_enrichment_audit_id);
CREATE INDEX IF NOT EXISTS idx_audit_confidence
    ON EnrichmentAuditTrail(final_confidence);
CREATE INDEX IF NOT EXISTS idx_audit_created
    ON EnrichmentAuditTrail(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_decision
    ON EnrichmentAuditTrail(final_decision);
CREATE INDEX IF NOT EXISTS idx_audit_enriched_event
    ON EnrichmentAuditTrail(enriched_event_id);
CREATE INDEX IF NOT EXISTS idx_audit_pipeline_version
    ON EnrichmentAuditTrail(pipeline_version);
CREATE INDEX IF NOT EXISTS idx_audit_raw_event
    ON EnrichmentAuditTrail(raw_event_id);
CREATE INDEX IF NOT EXISTS idx_audit_specific_incident
    ON EnrichmentAuditTrail(stage2_is_specific_incident);
CREATE INDEX IF NOT EXISTS idx_audit_stage2_victim
    ON EnrichmentAuditTrail(stage2_victim_organization);
CREATE INDEX IF NOT EXISTS idx_entities_size ON EntitiesV2(size_estimate);
CREATE INDEX IF NOT EXISTS idx_ea_key ON EntityAliases(canonical_key);
CREATE INDEX IF NOT EXISTS idx_entity_mappings_canonical
ON EntityMappings(canonical_entity);
CREATE INDEX IF NOT EXISTS idx_entity_mappings_source
ON EntityMappings(source_entity);
CREATE INDEX IF NOT EXISTS idx_dedup_map_dedup_event ON EventDeduplicationMap(deduplicated_event_id);
CREATE INDEX IF NOT EXISTS idx_dedup_map_raw_event ON EventDeduplicationMap(raw_event_id);
CREATE INDEX IF NOT EXISTS idx_industry_groupings_display_order
ON IndustryGroupings(display_order);
CREATE INDEX IF NOT EXISTS idx_judgements_lookup
    ON ModelJudgements (question_id, input_hash);
CREATE INDEX IF NOT EXISTS idx_judgements_subject
    ON ModelJudgements (subject_type, subject_id, question_id);
CREATE INDEX IF NOT EXISTS idx_month_processed_year_month
            ON MonthProcessed(year, month);
CREATE INDEX IF NOT EXISTS idx_processing_log_status ON ProcessingLog(status);
CREATE INDEX IF NOT EXISTS idx_raw_events_discovered_at ON RawEvents(discovered_at);
CREATE INDEX IF NOT EXISTS idx_raw_events_is_processed ON RawEvents(is_processed);
CREATE INDEX IF NOT EXISTS idx_raw_events_source_type ON RawEvents(source_type);

CREATE VIEW IF NOT EXISTS EnrichmentMetrics AS
SELECT
    DATE(created_at) as date,
    pipeline_version,
    final_decision,
    COUNT(*) as event_count,
    AVG(final_confidence) as avg_confidence,
    AVG(total_time_seconds) as avg_processing_time,
    AVG(stage2_confidence) as avg_gpt4o_confidence,
    AVG(stage3_verification_confidence) as avg_perplexity_confidence,
    AVG(stage4_validation_confidence) as avg_validation_confidence,
    SUM(CASE WHEN stage4_error_count > 0 THEN 1 ELSE 0 END) as events_with_errors,
    SUM(CASE WHEN stage4_warning_count > 0 THEN 1 ELSE 0 END) as events_with_warnings,
    AVG(CAST(stage3_checks_passed AS REAL) / NULLIF(stage3_checks_performed, 0)) as avg_factcheck_pass_rate
FROM EnrichmentAuditTrail
WHERE final_decision IS NOT NULL
GROUP BY DATE(created_at), pipeline_version, final_decision
ORDER BY date DESC, pipeline_version, final_decision;

CREATE VIEW IF NOT EXISTS EnrichmentQualityReport AS
SELECT
    pipeline_version,
    COUNT(*) as total_events,

    -- Decision breakdown
    SUM(CASE WHEN final_decision = 'AUTO_ACCEPT' THEN 1 ELSE 0 END) as auto_accept_count,
    SUM(CASE WHEN final_decision = 'ACCEPT_WITH_WARNING' THEN 1 ELSE 0 END) as accept_warning_count,
    SUM(CASE WHEN final_decision = 'REJECT' THEN 1 ELSE 0 END) as reject_count,
    SUM(CASE WHEN final_decision = 'ERROR' THEN 1 ELSE 0 END) as error_count,

    -- Confidence statistics
    AVG(final_confidence) as avg_confidence,
    MIN(final_confidence) as min_confidence,
    MAX(final_confidence) as max_confidence,

    -- Stage success rates
    SUM(CASE WHEN stage1_success = 1 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as stage1_success_rate,
    SUM(CASE WHEN stage2_success = 1 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as stage2_success_rate,
    SUM(CASE WHEN stage4_is_valid = 1 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as stage4_success_rate,

    -- Fact-checking statistics
    AVG(CAST(stage3_checks_passed AS REAL) / NULLIF(stage3_checks_performed, 0) * 100) as avg_factcheck_pass_rate,
    SUM(stage3_checks_performed) as total_factchecks,
    SUM(stage3_checks_passed) as total_factchecks_passed,

    -- Validation statistics
    SUM(stage4_error_count) as total_validation_errors,
    SUM(stage4_warning_count) as total_validation_warnings,

    -- Performance statistics
    AVG(total_time_seconds) as avg_processing_time,
    MIN(total_time_seconds) as min_processing_time,
    MAX(total_time_seconds) as max_processing_time,

    -- Victim identification
    SUM(CASE WHEN stage2_victim_organization IS NOT NULL
             AND stage2_victim_organization != 'None'
             AND stage2_victim_organization != 'Unknown'
        THEN 1 ELSE 0 END) as events_with_victim,

    -- Specific incident rate
    SUM(CASE WHEN stage2_is_specific_incident = 1 THEN 1 ELSE 0 END) as specific_incident_count,

    -- Date range
    MIN(created_at) as first_event,
    MAX(created_at) as last_event

FROM EnrichmentAuditTrail
GROUP BY pipeline_version
ORDER BY first_event DESC;

CREATE TRIGGER IF NOT EXISTS update_entity_mappings_timestamp
AFTER UPDATE ON EntityMappings
BEGIN
    UPDATE EntityMappings
    SET updated_at = CURRENT_TIMESTAMP
    WHERE entity_mapping_id = NEW.entity_mapping_id;
END;

CREATE TRIGGER IF NOT EXISTS update_industry_groupings_timestamp
AFTER UPDATE ON IndustryGroupings
BEGIN
    UPDATE IndustryGroupings
    SET updated_at = CURRENT_TIMESTAMP
    WHERE industry_group_id = NEW.industry_group_id;
END;
