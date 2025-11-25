-- Add enrichment fields to DeduplicatedEvents table
-- Run this script to add the new fields required for the enhanced dashboard

-- Add threat actor information
ALTER TABLE DeduplicatedEvents ADD COLUMN threat_actor TEXT;

-- Add vulnerability details
ALTER TABLE DeduplicatedEvents ADD COLUMN vulnerability_details TEXT;
ALTER TABLE DeduplicatedEvents ADD COLUMN vulnerability_category TEXT;
ALTER TABLE DeduplicatedEvents ADD COLUMN vulnerability_cve TEXT;

-- Add regulatory fine information
ALTER TABLE DeduplicatedEvents ADD COLUMN regulatory_fine_amount REAL;
ALTER TABLE DeduplicatedEvents ADD COLUMN regulatory_fine_currency TEXT;
ALTER TABLE DeduplicatedEvents ADD COLUMN regulatory_authority TEXT;

-- Add enrichment metadata
ALTER TABLE DeduplicatedEvents ADD COLUMN enrichment_source TEXT;
ALTER TABLE DeduplicatedEvents ADD COLUMN last_enrichment_date TIMESTAMP;

-- Create index on vulnerability_category for faster queries
CREATE INDEX IF NOT EXISTS idx_vulnerability_category ON DeduplicatedEvents(vulnerability_category);

-- Create index on threat_actor for faster queries
CREATE INDEX IF NOT EXISTS idx_threat_actor ON DeduplicatedEvents(threat_actor);

-- Create index on regulatory_fine_amount for faster queries
CREATE INDEX IF NOT EXISTS idx_regulatory_fine_amount ON DeduplicatedEvents(regulatory_fine_amount);








