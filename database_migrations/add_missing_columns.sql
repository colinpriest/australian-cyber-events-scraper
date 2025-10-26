-- Add missing data quality columns to EnrichedEvents table
-- This migration adds columns for better data quality tracking

BEGIN TRANSACTION;

-- Add attack victim entity column
ALTER TABLE EnrichedEvents 
ADD COLUMN attack_victim_entity VARCHAR(255);

-- Add vulnerability/attack method details column  
ALTER TABLE EnrichedEvents 
ADD COLUMN vulnerability_details TEXT;

-- Add data source reliability score
ALTER TABLE EnrichedEvents 
ADD COLUMN data_source_reliability_score REAL DEFAULT 0.5;

-- Add data completeness score
ALTER TABLE EnrichedEvents 
ADD COLUMN data_completeness_score REAL DEFAULT 0.5;

-- Add data quality flags
ALTER TABLE EnrichedEvents 
ADD COLUMN has_complete_date BOOLEAN DEFAULT FALSE;

ALTER TABLE EnrichedEvents 
ADD COLUMN has_complete_entity BOOLEAN DEFAULT FALSE;

ALTER TABLE EnrichedEvents 
ADD COLUMN has_complete_vulnerability BOOLEAN DEFAULT FALSE;

ALTER TABLE EnrichedEvents 
ADD COLUMN has_complete_records_count BOOLEAN DEFAULT FALSE;

-- Add data quality notes
ALTER TABLE EnrichedEvents 
ADD COLUMN data_quality_notes TEXT;

-- Add indexes for new columns
CREATE INDEX IF NOT EXISTS idx_enriched_attack_victim_entity 
ON EnrichedEvents(attack_victim_entity) 
WHERE attack_victim_entity IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_enriched_vulnerability 
ON EnrichedEvents(vulnerability_details) 
WHERE vulnerability_details IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_enriched_data_quality 
ON EnrichedEvents(data_completeness_score, data_source_reliability_score);

COMMIT;
