#!/usr/bin/env python3
import sqlite3

conn = sqlite3.connect('instance/cyber_events.db')
cursor = conn.cursor()

# Check the actual date range of events
cursor.execute('SELECT MIN(created_at) as earliest, MAX(created_at) as latest, COUNT(*) as total FROM EnrichedEvents WHERE status = "Active"')
result = cursor.fetchone()
print(f'All active events: {result[2]} total')
print(f'Date range: {result[0]} to {result[1]}')

# Check what the 3-month filter would return
cursor.execute('SELECT MIN(created_at) as earliest, MAX(created_at) as latest, COUNT(*) as total FROM EnrichedEvents WHERE status = "Active" AND created_at >= datetime("now", "-3 months")')
result = cursor.fetchone()
print(f'Last 3 months filter: {result[2]} total')
print(f'Date range: {result[0]} to {result[1]}')

# Check what datetime('now', '-3 months') evaluates to
cursor.execute('SELECT datetime("now", "-3 months") as cutoff')
cutoff = cursor.fetchone()[0]
print(f'3-month cutoff date: {cutoff}')

conn.close()
