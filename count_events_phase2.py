import sqlite3

conn = sqlite3.connect('instance/cyber_events.db')
cursor = conn.cursor()

# Count events without victims (for enrichment)
cursor.execute("""
    SELECT COUNT(*)
    FROM EnrichedEvents e
    JOIN RawEvents r ON e.raw_event_id = r.raw_event_id
    LEFT JOIN EnrichedEventEntities ee ON e.enriched_event_id = ee.enriched_event_id
        AND ee.relationship_type = "victim"
    WHERE ee.entity_id IS NULL
    AND e.status = 'Active'
    AND r.source_url IS NOT NULL
    AND r.source_url != ''
""")
no_victims = cursor.fetchone()[0]

# Count total active events
cursor.execute("SELECT COUNT(*) FROM EnrichedEvents WHERE status = 'Active'")
total = cursor.fetchone()[0]

print(f"Events without victims: {no_victims}")
print(f"Total active events: {total}")
print(f"\nPhase 2 will enrich {no_victims} events")
print(f"Estimated cost: ${no_victims * 0.14:.2f}")
print(f"Estimated time: {no_victims * 35 / 60:.1f} minutes ({no_victims * 35 / 3600:.1f} hours)")

conn.close()
