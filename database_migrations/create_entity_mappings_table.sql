-- Create EntityMappings table for entity normalization during deduplication
-- This table maps entity names to their canonical parent/group entity
-- Used to merge events from related entities (e.g., subsidiaries to parent company)

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

-- Insert known entity mappings
INSERT INTO EntityMappings (source_entity, canonical_entity, relationship_type, notes) VALUES
(
    'Ticketmaster LLC',
    'Live Nation Entertainment, Inc',
    'subsidiary',
    'Ticketmaster is a subsidiary of Live Nation Entertainment'
);

-- Create indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_entity_mappings_source
ON EntityMappings(source_entity);

CREATE INDEX IF NOT EXISTS idx_entity_mappings_canonical
ON EntityMappings(canonical_entity);

-- Create trigger to update updated_at timestamp
CREATE TRIGGER IF NOT EXISTS update_entity_mappings_timestamp
AFTER UPDATE ON EntityMappings
BEGIN
    UPDATE EntityMappings
    SET updated_at = CURRENT_TIMESTAMP
    WHERE entity_mapping_id = NEW.entity_mapping_id;
END;
