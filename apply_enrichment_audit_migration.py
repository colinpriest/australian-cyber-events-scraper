"""
Apply Enrichment Audit Trail Migration

This script applies the database schema changes to support audit trail storage
for the high-quality enrichment pipeline.
"""

import sqlite3
import sys
from pathlib import Path


def apply_migration(db_path: str = "instance/cyber_events.db"):
    """Apply the enrichment audit trail migration"""

    migration_file = Path("database_migrations/add_enrichment_audit_trail.sql")

    if not migration_file.exists():
        print(f"ERROR: Migration file not found: {migration_file}")
        return 1

    print(f"Applying migration: {migration_file}")
    print(f"Target database: {db_path}")
    print("-" * 80)

    # Read migration SQL
    with open(migration_file, 'r', encoding='utf-8') as f:
        migration_sql = f.read()

    # Connect to database
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # Execute migration (split by semicolons to handle multiple statements)
        statements = [s.strip() for s in migration_sql.split(';') if s.strip() and not s.strip().startswith('--')]

        for i, statement in enumerate(statements):
            # Skip empty statements and comments
            if not statement or statement.startswith('--'):
                continue

            try:
                cursor.execute(statement)
                print(f"✓ Executed statement {i+1}/{len(statements)}")
            except sqlite3.OperationalError as e:
                # Ignore "already exists" errors (idempotent migration)
                if 'already exists' in str(e).lower():
                    print(f"  (skipped - already exists)")
                else:
                    raise

        conn.commit()
        print("\n" + "=" * 80)
        print("✓ Migration applied successfully!")
        print("=" * 80)

        # Verify migration
        print("\nVerifying migration...")
        print("-" * 80)

        # Check EnrichmentAuditTrail table
        cursor.execute("SELECT COUNT(*) FROM pragma_table_info('EnrichmentAuditTrail')")
        audit_col_count = cursor.fetchone()[0]
        print(f"✓ EnrichmentAuditTrail table: {audit_col_count} columns")

        # Check EnrichedEvents new columns
        cursor.execute("""
            SELECT COUNT(*)
            FROM pragma_table_info('EnrichedEvents')
            WHERE name LIKE 'enrichment%'
        """)
        enrichment_col_count = cursor.fetchone()[0]
        print(f"✓ EnrichedEvents enrichment columns: {enrichment_col_count} added")

        # Check views
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type = 'view'
            AND name IN ('EnrichmentMetrics', 'EnrichmentQualityReport')
        """)
        views = cursor.fetchall()
        print(f"✓ Analytics views created: {len(views)}")
        for view in views:
            print(f"  - {view[0]}")

        # Check indexes
        cursor.execute("""
            SELECT COUNT(*)
            FROM sqlite_master
            WHERE type = 'index'
            AND name LIKE 'idx_audit%'
        """)
        index_count = cursor.fetchone()[0]
        print(f"✓ Indexes created: {index_count}")

        print("\n" + "=" * 80)
        print("Migration verification complete!")
        print("=" * 80)

        # Show quick stats
        print("\nCurrent database statistics:")
        print("-" * 80)

        cursor.execute("SELECT COUNT(*) FROM EnrichedEvents")
        enriched_count = cursor.fetchone()[0]
        print(f"EnrichedEvents: {enriched_count:,} records")

        cursor.execute("SELECT COUNT(*) FROM EnrichmentAuditTrail")
        audit_count = cursor.fetchone()[0]
        print(f"EnrichmentAuditTrail: {audit_count:,} records")

        if audit_count > 0:
            cursor.execute("""
                SELECT pipeline_version, COUNT(*)
                FROM EnrichmentAuditTrail
                GROUP BY pipeline_version
            """)
            versions = cursor.fetchall()
            print(f"\nAudit trails by pipeline version:")
            for version, count in versions:
                print(f"  - {version}: {count:,} trails")

        return 0

    except Exception as e:
        conn.rollback()
        print(f"\n✗ ERROR: Migration failed: {e}")
        import traceback
        traceback.print_exc()
        return 1

    finally:
        conn.close()


def main():
    """Main execution"""

    db_path = "instance/cyber_events.db"

    # Check if database exists
    if not Path(db_path).exists():
        print(f"ERROR: Database not found: {db_path}")
        return 1

    print("\n" + "=" * 80)
    print("ENRICHMENT AUDIT TRAIL MIGRATION")
    print("=" * 80)
    print("\nThis migration will:")
    print("  1. Create EnrichmentAuditTrail table")
    print("  2. Add enrichment tracking columns to EnrichedEvents")
    print("  3. Create analytics views (EnrichmentMetrics, EnrichmentQualityReport)")
    print("  4. Create indexes for efficient querying")
    print("\nThe migration is idempotent - safe to run multiple times.")
    print("=" * 80)

    response = input("\nProceed with migration? (y/n): ").strip().lower()

    if response != 'y':
        print("Migration cancelled.")
        return 0

    return apply_migration(db_path)


if __name__ == '__main__':
    sys.exit(main())
