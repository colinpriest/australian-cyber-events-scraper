#!/usr/bin/env python3
"""
Test script for the new separated raw/enriched events schema.

This script tests the V2 database functionality and pipeline components
without running the full discovery process.
"""

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Handle Windows encoding issues
if sys.platform == "win32" and sys.stdout.encoding != "utf-8":
    import codecs
    sys.stdout = codecs.getwriter("utf-8")(sys.stdout.buffer)

# Add current directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from cyber_event_data_v2 import CyberEventDataV2


def test_database_v2_functionality():
    """Test basic V2 database operations"""
    print("🧪 Testing Database V2 functionality...")

    # Test database initialization
    test_db_path = "test_cyber_events_v2.db"

    try:
        # Clean up any existing test database
        test_db_file = Path(test_db_path)
        if test_db_file.exists():
            test_db_file.unlink()

        print("❌ Error: V2 schema not found in test database")
        print("📋 You need to run the migration script first on the main database")
        print("   python database_migration_v2.py")
        return False

    except Exception as e:
        print(f"❌ Database V2 test failed: {e}")
        return False


def test_existing_database_v2():
    """Test with existing migrated database"""
    print("\n🧪 Testing existing Database V2...")

    try:
        with CyberEventDataV2() as db:
            # Test basic operations
            print("✅ Database V2 connection successful")

            # Get statistics
            stats = db.get_summary_statistics()
            print(f"📊 Raw events: {stats.get('raw_events_total', 0)}")
            print(f"📊 Enriched events: {stats.get('enriched_events_total', 0)}")

            # Get processing queue status
            queue_stats = db.get_processing_queue_status()
            print(f"🔄 Events needing processing: {queue_stats.get('unprocessed_total', 0)}")

            print("✅ Database V2 operations successful")
            return True

    except RuntimeError as e:
        if "V2 schema not found" in str(e):
            print("❌ V2 schema not found in main database")
            print("📋 Please run the migration script first:")
            print("   python database_migration_v2.py")
            return False
        else:
            print(f"❌ Database V2 test failed: {e}")
            return False
    except Exception as e:
        print(f"❌ Database V2 test failed: {e}")
        return False


def test_sample_raw_event():
    """Test adding a sample raw event"""
    print("\n🧪 Testing raw event creation...")

    try:
        with CyberEventDataV2() as db:
            # Create sample raw event
            raw_data = {
                'source_event_id': 'test_123',
                'title': 'Test Australian Cyber Incident',
                'description': 'Sample cyber incident for testing',
                'content': 'This is a test content for a cyber security incident affecting an Australian company.',
                'event_date': datetime.now().date(),
                'source_url': 'https://example.com/test-incident',
                'metadata': {
                    'test': True,
                    'confidence': 0.8
                }
            }

            raw_event_id = db.add_raw_event('GDELT', raw_data)
            print(f"✅ Created test raw event: {raw_event_id}")

            # Test retrieval
            unprocessed = db.get_unprocessed_raw_events(limit=1)
            if unprocessed:
                print("✅ Raw event retrieval successful")
                return True
            else:
                print("❌ No unprocessed events found")
                return False

    except Exception as e:
        print(f"❌ Raw event test failed: {e}")
        return False


def main():
    """Run all tests"""
    print("🚀 Starting Database V2 Schema Tests\n")

    tests_passed = 0
    total_tests = 3

    # Test 1: V2 database functionality
    if test_existing_database_v2():
        tests_passed += 1

    # Test 2: Sample raw event creation
    if test_sample_raw_event():
        tests_passed += 1

    # Test 3: Pipeline import test
    try:
        print("\n🧪 Testing pipeline imports...")
        from discover_enrich_events import EventDiscoveryEnrichmentPipeline
        print("✅ Pipeline import successful")
        tests_passed += 1
    except Exception as e:
        print(f"❌ Pipeline import failed: {e}")

    # Summary
    print(f"\n{'='*50}")
    print(f"🎯 Test Results: {tests_passed}/{total_tests} tests passed")

    if tests_passed == total_tests:
        print("✅ All tests passed! V2 schema is ready.")
        print("\n📋 Next steps:")
        print("1. Run migration: python database_migration_v2.py")
        print("2. Test pipeline: python discover_enrich_events.py --discover --max-events 10")
        return 0
    else:
        print("❌ Some tests failed. Please check the setup.")
        if tests_passed == 0:
            print("\n📋 Required setup:")
            print("1. Run migration script: python database_migration_v2.py")
            print("2. Ensure all dependencies are installed")
        return 1


if __name__ == "__main__":
    sys.exit(main())