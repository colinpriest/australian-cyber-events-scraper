# Database Record Wiper Script

## Overview

The `wipe_database.py` script safely clears all **RECORDS** from the Australian Cyber Events Scraper project's database tables while **preserving the database structure and schema**. This is perfect for resetting data without losing table definitions.

## Key Features

- **Record-Only Deletion**: Clears data but preserves database structure
- **Project-Specific**: Targets only this project's known tables
- **Safe Operation**: Multiple confirmation prompts and dry-run mode
- **Selective Wiping**: Target specific tables or all tables
- **Verification**: Confirms successful deletion after operation
- **Database Integrity**: Handles foreign keys and auto-increment counters properly

## Database Tables Managed

The script handles these project-specific tables:

- **UniqueEvents**: 42 records - Main cyber events
- **Entities**: 262 records - Affected entities (companies, organizations)
- **EventEntities**: 225 records - Links between events and entities
- **EventSources**: 46 records - Source URLs and metadata
- **DataSources**: 3 records - Data source definitions
- **EventAttributeHistory**: 0 records - Change tracking

**Total Current Records: 578**

## Usage

### Basic Usage

```bash
# Preview what records would be deleted (SAFE)
python wipe_database.py --dry-run

# Wipe all records from all project tables (with confirmation)
python wipe_database.py

# Wipe specific tables only
python wipe_database.py --tables "EventSources,EventEntities"

# Force wipe without confirmations (DANGEROUS!)
python wipe_database.py --force
```

### Command Line Options

- `--dry-run`: Preview mode - shows what would be deleted without actually deleting
- `--force`: Skip all confirmation prompts (use with extreme caution!)
- `--tables`: Comma-separated list of specific tables to wipe (e.g., "UniqueEvents,Entities")

## What Gets Wiped vs Preserved

### ✅ **WIPED (Deleted)**
- All record data from project tables
- Auto-increment counters (reset to 0)
- Foreign key relationships between records

### ✅ **PRESERVED (Kept)**
- Database file (`instance/cyber_events.db`)
- Table structure and schema
- Column definitions and constraints
- Indexes and foreign key definitions
- Database integrity and relationships

## Safety Features

### Multiple Confirmations
1. **Initial Warning**: Clear explanation of what will happen
2. **Record Summary**: Shows exactly how many records from which tables
3. **Final Confirmation**: Must type "YES" to proceed

### Dry Run Mode
```bash
python wipe_database.py --dry-run
```

Example output:
```
[2025-09-26 17:04:49] [DRY-RUN] INFO: Tables to wipe:
[2025-09-26 17:04:49] [DRY-RUN] INFO:   - EventSources: 46 records
[2025-09-26 17:04:49] [DRY-RUN] INFO:   - EventEntities: 225 records
[2025-09-26 17:04:49] [DRY-RUN] INFO:   - UniqueEvents: 42 records
[2025-09-26 17:04:49] [DRY-RUN] INFO:   - Entities: 262 records
[2025-09-26 17:04:49] [DRY-RUN] INFO:   - DataSources: 3 records
[2025-09-26 17:04:49] [DRY-RUN] INFO: Total records to delete: 578
```

### Post-Operation Verification
- Confirms all targeted tables are empty
- Reports any remaining records
- Validates operation success

## Examples

### 1. Safe Preview of All Records
```bash
python wipe_database.py --dry-run
```
Shows exactly what would be deleted without making any changes.

### 2. Clear Only Event-Related Data
```bash
python wipe_database.py --tables "UniqueEvents,EventSources,EventEntities"
```
Preserves entities and data sources, only clears events.

### 3. Complete Database Reset
```bash
python wipe_database.py
```
Clears all records from all project tables after confirmation.

### 4. Emergency Automated Wipe
```bash
python wipe_database.py --force
```
**⚠️ WARNING**: No confirmations - use only in scripts!

## Database Support

### SQLite (Primary)
- **File**: `instance/cyber_events.db`
- **Method**: `DELETE FROM table` with foreign key handling
- **Auto-increment**: Resets counters via `sqlite_sequence`

### PostgreSQL (Optional)
- **Method**: `TRUNCATE TABLE ... RESTART IDENTITY CASCADE`
- **Config**: Via environment variables (`POSTGRES_*`)
- **Safety**: Temporarily disables foreign key constraints

## Technical Details

### Foreign Key Handling
```sql
-- Temporarily disable constraints
PRAGMA foreign_keys = OFF;

-- Delete records from tables
DELETE FROM EventSources;
DELETE FROM EventEntities;
-- ... etc

-- Reset auto-increment
DELETE FROM sqlite_sequence WHERE name IN (...);

-- Re-enable constraints
PRAGMA foreign_keys = ON;
```

### Table Processing Order
Tables are processed in dependency order to avoid foreign key conflicts:

1. `EventAttributeHistory` (no dependencies)
2. `EventSources` (references UniqueEvents, DataSources)
3. `EventEntities` (references UniqueEvents, Entities)
4. `UniqueEvents` (main events table)
5. `Entities` (entity definitions)
6. `DataSources` (source definitions)

## Error Handling

- **Missing Database**: Gracefully handles if database doesn't exist
- **Missing Tables**: Skips tables that don't exist in the database
- **Foreign Key Violations**: Temporarily disables constraints
- **Operation Verification**: Confirms successful deletion
- **Detailed Logging**: Timestamps and clear error messages

## Return Codes

- **0**: Success - all records deleted successfully
- **1**: Failure - errors occurred during operation
- **1**: User Cancellation - operation cancelled by user

## Best Practices

### Before Running
1. **Always use `--dry-run` first** to see what will be deleted
2. **Backup your database** if you want to keep the data
3. **Stop the application** to avoid data corruption during deletion

### After Running
1. **Verify results** - script automatically checks for remaining records
2. **Restart your application** to begin with fresh, empty tables
3. **Check logs** for any warnings or errors

## Real-World Scenarios

### Development Reset
```bash
# Clear all test data and start fresh
python wipe_database.py --dry-run  # Preview
python wipe_database.py            # Execute
```

### Selective Cleanup
```bash
# Keep entities but clear events
python wipe_database.py --tables "UniqueEvents,EventSources,EventEntities"
```

### Production Migration
```bash
# Use in deployment scripts with force mode
python wipe_database.py --force
```

---

## ⚠️ **Important Notes**

- **Data Loss**: Records are permanently deleted and cannot be recovered
- **Schema Preserved**: Database structure remains intact
- **No Backup**: Script doesn't create backups - do this manually if needed
- **Single Database**: Currently designed for the project's SQLite database

**Always test with `--dry-run` first!**