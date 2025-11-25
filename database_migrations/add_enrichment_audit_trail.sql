-- Migration: Add Enrichment Audit Trail Schema
-- Description: Add tables and columns to store comprehensive audit trails for the high-quality enrichment pipeline
-- Version: 1.0
-- Date: 2025-10-28

-- ============================================================================
-- 1. Create EnrichmentAuditTrail table
-- ============================================================================

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

-- Index for querying by event
CREATE INDEX IF NOT EXISTS idx_audit_enriched_event
    ON EnrichmentAuditTrail(enriched_event_id);

-- Index for querying by raw event
CREATE INDEX IF NOT EXISTS idx_audit_raw_event
    ON EnrichmentAuditTrail(raw_event_id);

-- Index for querying by decision
CREATE INDEX IF NOT EXISTS idx_audit_decision
    ON EnrichmentAuditTrail(final_decision);

-- Index for querying by confidence
CREATE INDEX IF NOT EXISTS idx_audit_confidence
    ON EnrichmentAuditTrail(final_confidence);

-- Index for querying by date
CREATE INDEX IF NOT EXISTS idx_audit_created
    ON EnrichmentAuditTrail(created_at);


-- ============================================================================
-- 2. Add enrichment tracking columns to EnrichedEvents
-- ============================================================================

-- Add pipeline version tracking
ALTER TABLE EnrichedEvents
ADD COLUMN enrichment_pipeline_version VARCHAR(20) DEFAULT 'v1_regex';

-- Add enrichment confidence
ALTER TABLE EnrichedEvents
ADD COLUMN enrichment_confidence REAL;

-- Add enrichment method
ALTER TABLE EnrichedEvents
ADD COLUMN enrichment_method VARCHAR(50) DEFAULT 'regex';

-- Add reference to latest audit trail
ALTER TABLE EnrichedEvents
ADD COLUMN last_enrichment_audit_id TEXT;

-- Add foreign key constraint
-- Note: SQLite doesn't support ADD CONSTRAINT on existing tables,
-- so this would need to be done in a table recreation if strict FK enforcement is needed

-- Index for querying by audit trail
CREATE INDEX IF NOT EXISTS idx_enrichedevents_audit
    ON EnrichedEvents(last_enrichment_audit_id);


-- ============================================================================
-- 3. Create EnrichmentMetrics view for easy analysis
-- ============================================================================

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


-- ============================================================================
-- 4. Create EnrichmentQualityReport view for monitoring
-- ============================================================================

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


-- ============================================================================
-- 5. Create indexes for analytics queries
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_audit_pipeline_version
    ON EnrichmentAuditTrail(pipeline_version);

CREATE INDEX IF NOT EXISTS idx_audit_stage2_victim
    ON EnrichmentAuditTrail(stage2_victim_organization);

CREATE INDEX IF NOT EXISTS idx_audit_specific_incident
    ON EnrichmentAuditTrail(stage2_is_specific_incident);


-- ============================================================================
-- Migration Complete
-- ============================================================================

-- Verification query (run after migration)
-- SELECT
--     'EnrichmentAuditTrail' as table_name,
--     COUNT(*) as column_count
-- FROM pragma_table_info('EnrichmentAuditTrail')
-- UNION ALL
-- SELECT
--     'EnrichedEvents enrichment columns' as table_name,
--     COUNT(*) as column_count
-- FROM pragma_table_info('EnrichedEvents')
-- WHERE name LIKE 'enrichment%';
