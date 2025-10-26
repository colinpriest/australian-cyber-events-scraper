-- Database migration to add constraints for the new deduplication system
-- This migration adds UNIQUE constraints to prevent duplicates

-- Migration: Add Deduplication Constraints
-- Version: 2.1
-- Date: 2024-01-XX
-- Description: Add constraints to prevent duplicate deduplicated events

BEGIN TRANSACTION;

-- 1. Add UNIQUE constraint to prevent duplicate title+date combinations
-- This is the most important constraint to prevent the current issue where
-- we have more deduplicated events than raw events
CREATE UNIQUE INDEX IF NOT EXISTS idx_dedup_unique_event 
ON DeduplicatedEvents(title, event_date) 
WHERE status = 'Active';

-- 2. Add index for better query performance on common lookups
CREATE INDEX IF NOT EXISTS idx_dedup_status_date 
ON DeduplicatedEvents(status, event_date);

CREATE INDEX IF NOT EXISTS idx_dedup_event_type 
ON DeduplicatedEvents(event_type) 
WHERE event_type IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_dedup_severity 
ON DeduplicatedEvents(severity) 
WHERE severity IS NOT NULL;

COMMIT;

-- Verification queries to check the migration worked
-- Run these after the migration to verify constraints are in place

-- Check that the unique constraint exists
-- SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_dedup_unique_event';
