"""Check if failed events have cached raw_content"""
import sqlite3

conn = sqlite3.connect('instance/cyber_events.db')
cursor = conn.cursor()

# Check failed events for cached content
query = """
SELECT
    COUNT(*) as total,
    COUNT(CASE WHEN r.raw_content IS NOT NULL AND LENGTH(r.raw_content) > 0 THEN 1 END) as with_content,
    COUNT(CASE WHEN r.raw_content IS NULL OR LENGTH(r.raw_content) = 0 THEN 1 END) as without_content,
    AVG(CASE WHEN r.raw_content IS NOT NULL THEN LENGTH(r.raw_content) ELSE 0 END) as avg_content_length
FROM RawEvents r
JOIN EnrichedEvents e ON r.raw_event_id = e.raw_event_id
JOIN EnrichmentAuditTrail a ON e.enriched_event_id = a.enriched_event_id
WHERE a.final_confidence = 0.0
    AND a.error_message LIKE '%Failed to extract sufficient content%'
    AND e.status = 'Active'
"""

cursor.execute(query)
result = cursor.fetchone()

print(f"\n{'='*80}")
print("CACHED CONTENT ANALYSIS FOR FAILED EVENTS")
print(f"{'='*80}")
print(f"Total failed events: {result[0]}")
print(f"Events WITH cached content: {result[1]} ({result[1]/result[0]*100:.1f}%)")
print(f"Events WITHOUT cached content: {result[2]} ({result[2]/result[0]*100:.1f}%)")
print(f"Average content length: {result[3]:.0f} characters")
print()

# Sample of events with content
print(f"\n{'='*80}")
print("SAMPLE EVENTS WITH CACHED CONTENT (first 5)")
print(f"{'='*80}")

query2 = """
SELECT
    e.enriched_event_id,
    r.raw_title,
    LENGTH(r.raw_content) as content_length,
    r.source_url
FROM RawEvents r
JOIN EnrichedEvents e ON r.raw_event_id = e.raw_event_id
JOIN EnrichmentAuditTrail a ON e.enriched_event_id = a.enriched_event_id
WHERE a.final_confidence = 0.0
    AND a.error_message LIKE '%Failed to extract sufficient content%'
    AND e.status = 'Active'
    AND r.raw_content IS NOT NULL
    AND LENGTH(r.raw_content) > 0
ORDER BY LENGTH(r.raw_content) DESC
LIMIT 5
"""

cursor.execute(query2)
for row in cursor.fetchall():
    print(f"\nID: {row[0]}")
    print(f"Title: {row[1][:70]}")
    print(f"Cached content: {row[2]:,} characters")
    print(f"URL: {row[3][:80]}")

# Sample of events without content
print(f"\n{'='*80}")
print("SAMPLE EVENTS WITHOUT CACHED CONTENT (first 5)")
print(f"{'='*80}")

query3 = """
SELECT
    e.enriched_event_id,
    r.raw_title,
    r.source_url
FROM RawEvents r
JOIN EnrichedEvents e ON r.raw_event_id = e.raw_event_id
JOIN EnrichmentAuditTrail a ON e.enriched_event_id = a.enriched_event_id
WHERE a.final_confidence = 0.0
    AND a.error_message LIKE '%Failed to extract sufficient content%'
    AND e.status = 'Active'
    AND (r.raw_content IS NULL OR LENGTH(r.raw_content) = 0)
LIMIT 5
"""

cursor.execute(query3)
for row in cursor.fetchall():
    print(f"\nID: {row[0]}")
    print(f"Title: {row[1][:70]}")
    print(f"URL: {row[2][:80]}")

conn.close()
