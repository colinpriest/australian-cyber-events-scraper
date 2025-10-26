"""
Comprehensive test suite for the new deduplication system.

This module tests all aspects of the new deduplication system:
- Exact duplicate detection
- Similar event detection  
- Edge cases and error handling
- Validation and integrity checks
- Database constraints
- Merge lineage tracking
- Idempotency
"""

import pytest
import sqlite3
import tempfile
import os
from datetime import datetime, date, timedelta
from typing import List, Dict, Any

from cyber_data_collector.processing.deduplication_v2 import (
    DeduplicationEngine, SimilarityCalculator, LLMArbiter, DeduplicationValidator,
    DeduplicationResult, MergeGroup, ValidationError, SimilarityScore
)
from cyber_data_collector.storage.deduplication_storage import DeduplicationStorage
from cyber_data_collector.processing.deduplication_v2 import CyberEvent


class TestDeduplicationValidator:
    """Test the DeduplicationValidator class"""
    
    def test_validate_inputs_empty(self):
        """Test validation with empty input"""
        validator = DeduplicationValidator()
        errors = validator.validate_inputs([])
        
        assert len(errors) == 1
        assert errors[0].error_type == "EMPTY_INPUT"
    
    def test_validate_inputs_duplicate_ids(self):
        """Test validation with duplicate event IDs"""
        validator = DeduplicationValidator()
        
        events = [
            CyberEvent(event_id="1", title="Event 1", event_date=date.today()),
            CyberEvent(event_id="1", title="Event 2", event_date=date.today()),
            CyberEvent(event_id="2", title="Event 3", event_date=date.today())
        ]
        
        errors = validator.validate_inputs(events)
        
        assert len(errors) == 1
        assert errors[0].error_type == "DUPLICATE_EVENT_IDS"
        assert "1" in errors[0].context["duplicate_ids"]
    
    def test_validate_inputs_missing_titles(self):
        """Test validation with missing titles"""
        validator = DeduplicationValidator()
        
        events = [
            CyberEvent(event_id="1", title="", event_date=date.today()),
            CyberEvent(event_id="2", title=None, event_date=date.today()),
            CyberEvent(event_id="3", title="Valid Title", event_date=date.today())
        ]
        
        errors = validator.validate_inputs(events)
        
        assert len(errors) == 2
        assert all(error.error_type == "MISSING_TITLE" for error in errors)
    
    def test_validate_no_duplicates_exact_duplicates(self):
        """Test detection of exact duplicates"""
        validator = DeduplicationValidator()
        
        events = [
            CyberEvent(event_id="1", title="Data Breach", event_date=date(2023, 1, 1)),
            CyberEvent(event_id="2", title="Data Breach", event_date=date(2023, 1, 1)),
            CyberEvent(event_id="3", title="Different Event", event_date=date(2023, 1, 2))
        ]
        
        errors = validator.validate_no_duplicates(events)
        
        assert len(errors) == 1
        assert errors[0].error_type == "DUPLICATE_EVENT"
    
    def test_validate_no_duplicates_similar_titles(self):
        """Test detection of very similar titles"""
        validator = DeduplicationValidator()
        
        events = [
            CyberEvent(event_id="1", title="Optus Data Breach", event_date=date(2023, 1, 1)),
            CyberEvent(event_id="2", title="Optus Data Breach Incident", event_date=date(2023, 1, 1)),
            CyberEvent(event_id="3", title="Completely Different", event_date=date(2023, 1, 1))
        ]
        
        errors = validator.validate_no_duplicates(events)
        
        assert len(errors) == 1
        assert errors[0].error_type == "SIMILAR_EVENT"
    
    def test_validate_data_integrity_future_dates(self):
        """Test validation of future dates"""
        validator = DeduplicationValidator()
        
        future_date = date.today() + timedelta(days=1)
        events = [
            CyberEvent(event_id="1", title="Future Event", event_date=future_date),
            CyberEvent(event_id="2", title="Past Event", event_date=date(2023, 1, 1))
        ]
        
        errors = validator.validate_data_integrity(events)
        
        assert len(errors) == 1
        assert errors[0].error_type == "FUTURE_DATE"
    
    def test_validate_data_integrity_negative_records(self):
        """Test validation of negative records affected"""
        validator = DeduplicationValidator()
        
        events = [
            CyberEvent(event_id="1", title="Negative Records", records_affected=-100),
            CyberEvent(event_id="2", title="Valid Records", records_affected=1000)
        ]
        
        errors = validator.validate_data_integrity(events)
        
        assert len(errors) == 1
        assert errors[0].error_type == "NEGATIVE_RECORDS"


class TestSimilarityCalculator:
    """Test the SimilarityCalculator class"""
    
    def test_title_similarity_exact_match(self):
        """Test exact title matching"""
        calculator = SimilarityCalculator()
        
        event1 = CyberEvent(event_id="1", title="Data Breach", event_date=date.today())
        event2 = CyberEvent(event_id="2", title="Data Breach", event_date=date.today())
        
        similarity = calculator._title_similarity(event1, event2)
        assert similarity == 1.0
    
    def test_title_similarity_substring_match(self):
        """Test substring title matching"""
        calculator = SimilarityCalculator()
        
        event1 = CyberEvent(event_id="1", title="Optus Data Breach", event_date=date.today())
        event2 = CyberEvent(event_id="2", title="Optus Data Breach Incident", event_date=date.today())
        
        similarity = calculator._title_similarity(event1, event2)
        assert similarity >= 0.8
    
    def test_entity_similarity_common_entities(self):
        """Test entity similarity with common entities"""
        calculator = SimilarityCalculator()
        
        event1 = CyberEvent(event_id="1", title="Optus Data Breach", event_date=date.today())
        event2 = CyberEvent(event_id="2", title="Optus Security Incident", event_date=date.today())
        
        similarity = calculator._entity_similarity(event1, event2)
        # Should have some similarity due to "Optus"
        assert similarity > 0.0
    
    def test_temporal_similarity_same_date(self):
        """Test temporal similarity with same date"""
        calculator = SimilarityCalculator()
        
        same_date = date.today()
        event1 = CyberEvent(event_id="1", title="Event 1", event_date=same_date)
        event2 = CyberEvent(event_id="2", title="Event 2", event_date=same_date)
        
        similarity = calculator._temporal_similarity(event1, event2)
        assert similarity == 1.0
    
    def test_temporal_similarity_different_dates(self):
        """Test temporal similarity with different dates"""
        calculator = SimilarityCalculator()
        
        event1 = CyberEvent(event_id="1", title="Event 1", event_date=date(2023, 1, 1))
        event2 = CyberEvent(event_id="2", title="Event 2", event_date=date(2023, 1, 2))
        
        similarity = calculator._temporal_similarity(event1, event2)
        assert similarity > 0.0  # Should have some similarity for close dates
    
    def test_calculate_similarity_comprehensive(self):
        """Test comprehensive similarity calculation"""
        calculator = SimilarityCalculator()
        
        event1 = CyberEvent(
            event_id="1", 
            title="Optus Data Breach", 
            summary="Optus experienced a data breach",
            event_date=date(2023, 1, 1)
        )
        event2 = CyberEvent(
            event_id="2", 
            title="Optus Security Incident", 
            summary="Optus had a security incident",
            event_date=date(2023, 1, 2)
        )
        
        similarity = calculator.calculate_similarity(event1, event2)
        
        assert isinstance(similarity, SimilarityScore)
        assert 0.0 <= similarity.overall_score <= 1.0
        assert 0.0 <= similarity.title_similarity <= 1.0
        assert 0.0 <= similarity.entity_similarity <= 1.0
        assert 0.0 <= similarity.content_similarity <= 1.0
        assert 0.0 <= similarity.temporal_similarity <= 1.0
        assert similarity.reasoning is not None


class TestLLMArbiter:
    """Test the LLMArbiter class"""
    
    def test_should_use_arbiter_uncertain_range(self):
        """Test that arbiter is used for uncertain scores"""
        arbiter = LLMArbiter()
        
        # Should use arbiter for uncertain scores
        assert arbiter._should_use_arbiter(0.4) == True
        assert arbiter._should_use_arbiter(0.6) == True
        
        # Should not use arbiter for confident scores
        assert arbiter._should_use_arbiter(0.2) == False
        assert arbiter._should_use_arbiter(0.8) == False
    
    def test_decide_similarity_no_api_key(self):
        """Test arbiter fallback when no API key"""
        arbiter = LLMArbiter(api_key=None)
        
        event1 = CyberEvent(event_id="1", title="Event 1", event_date=date.today())
        event2 = CyberEvent(event_id="2", title="Event 2", event_date=date.today())
        
        decision = arbiter.decide_similarity(event1, event2, 0.5)
        
        assert decision.is_similar == True  # Should use algorithmic score
        assert decision.confidence == 0.5
        assert "No LLM API key" in decision.reasoning
    
    def test_format_prompt(self):
        """Test prompt formatting"""
        arbiter = LLMArbiter()
        
        event1 = CyberEvent(
            event_id="1", 
            title="Data Breach", 
            summary="A data breach occurred",
            event_date=date(2023, 1, 1),
            event_type="Data Breach"
        )
        event2 = CyberEvent(
            event_id="2", 
            title="Security Incident", 
            summary="A security incident occurred",
            event_date=date(2023, 1, 2),
            event_type="Security Incident"
        )
        
        prompt = arbiter._format_prompt(event1, event2, 0.5)
        
        assert "Data Breach" in prompt
        assert "Security Incident" in prompt
        assert "0.5" in prompt
        assert "JSON" in prompt


class TestDeduplicationEngine:
    """Test the DeduplicationEngine class"""
    
    def test_deduplicate_empty_input(self):
        """Test deduplication with empty input"""
        engine = DeduplicationEngine()
        
        result = engine.deduplicate([])
        
        assert len(result.unique_events) == 0
        assert len(result.merge_groups) == 0
        assert len(result.validation_errors) > 0
        assert result.validation_errors[0].error_type == "EMPTY_INPUT"
    
    def test_deduplicate_single_event(self):
        """Test deduplication with single event"""
        engine = DeduplicationEngine()
        
        event = CyberEvent(event_id="1", title="Single Event", event_date=date.today())
        result = engine.deduplicate([event])
        
        assert len(result.unique_events) == 1
        assert len(result.merge_groups) == 0
        assert result.unique_events[0].event_id == "1"
    
    def test_deduplicate_exact_duplicates(self):
        """Test deduplication with exact duplicates"""
        engine = DeduplicationEngine(similarity_threshold=0.9)
        
        events = [
            CyberEvent(event_id="1", title="Data Breach", event_date=date(2023, 1, 1)),
            CyberEvent(event_id="2", title="Data Breach", event_date=date(2023, 1, 1)),
            CyberEvent(event_id="3", title="Different Event", event_date=date(2023, 1, 2))
        ]
        
        result = engine.deduplicate(events)
        
        # Should merge the two identical events
        assert len(result.unique_events) == 2
        assert len(result.merge_groups) == 1
        assert result.merge_groups[0].confidence > 0.0
    
    def test_deduplicate_similar_events(self):
        """Test deduplication with similar events"""
        engine = DeduplicationEngine(similarity_threshold=0.7)
        
        events = [
            CyberEvent(
                event_id="1", 
                title="Optus Data Breach", 
                summary="Optus experienced a data breach",
                event_date=date(2023, 1, 1)
            ),
            CyberEvent(
                event_id="2", 
                title="Optus Security Incident", 
                summary="Optus had a security incident",
                event_date=date(2023, 1, 2)
            ),
            CyberEvent(
                event_id="3", 
                title="Completely Different", 
                summary="A different company had an issue",
                event_date=date(2023, 1, 3)
            )
        ]
        
        result = engine.deduplicate(events)
        
        # Should merge the two Optus events
        assert len(result.unique_events) == 2
        assert len(result.merge_groups) == 1
    
    def test_deduplicate_different_events(self):
        """Test deduplication with completely different events"""
        engine = DeduplicationEngine(similarity_threshold=0.8)
        
        events = [
            CyberEvent(event_id="1", title="Company A Breach", event_date=date(2023, 1, 1)),
            CyberEvent(event_id="2", title="Company B Incident", event_date=date(2023, 1, 2)),
            CyberEvent(event_id="3", title="Company C Attack", event_date=date(2023, 1, 3))
        ]
        
        result = engine.deduplicate(events)
        
        # Should not merge any events
        assert len(result.unique_events) == 3
        assert len(result.merge_groups) == 0
    
    def test_select_master_event(self):
        """Test master event selection logic"""
        engine = DeduplicationEngine()
        
        events = [
            CyberEvent(
                event_id="1", 
                title="Event 1", 
                summary="Short summary",
                event_date=date(2023, 1, 1)
            ),
            CyberEvent(
                event_id="2", 
                title="Event 2", 
                summary="This is a much longer and more detailed summary that provides more information about the event",
                event_date=date(2023, 1, 2),
                records_affected=1000
            )
        ]
        
        master = engine._select_master_event(events)
        
        # Should select the event with more complete data
        assert master.event_id == "2"
    
    def test_merge_event_data(self):
        """Test merging data from multiple events"""
        engine = DeduplicationEngine()
        
        events = [
            CyberEvent(
                event_id="1", 
                title="Event 1", 
                summary="Short summary",
                records_affected=100,
                data_sources=["source1"],
                urls=["url1"]
            ),
            CyberEvent(
                event_id="2", 
                title="Event 2", 
                summary="Longer summary with more details",
                records_affected=500,
                data_sources=["source2"],
                urls=["url2"]
            )
        ]
        
        merged = engine._merge_event_data(events)
        
        # Should use the best data from all events
        assert merged.event_id == "1"  # Keeps original ID
        assert len(merged.summary) > len(events[0].summary)  # Uses longer summary
        assert merged.records_affected == 500  # Uses higher count
        assert len(merged.data_sources) == 2  # Combines all sources
        assert len(merged.urls) == 2  # Combines all URLs


class TestDeduplicationStorage:
    """Test the DeduplicationStorage class"""
    
    @pytest.fixture
    def temp_db(self):
        """Create a temporary database for testing"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        # Create database with required tables
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Create DeduplicatedEvents table
        cursor.execute("""
            CREATE TABLE DeduplicatedEvents (
                deduplicated_event_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT,
                event_date DATE,
                event_type TEXT,
                severity TEXT,
                records_affected INTEGER,
                data_sources TEXT,
                urls TEXT,
                status TEXT DEFAULT 'Active',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create EventDeduplicationMap table
        cursor.execute("""
            CREATE TABLE EventDeduplicationMap (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deduplicated_event_id TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                similarity_score REAL,
                merge_reason TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create DeduplicationClusters table
        cursor.execute("""
            CREATE TABLE DeduplicationClusters (
                cluster_id TEXT PRIMARY KEY,
                master_event_id TEXT NOT NULL,
                merge_timestamp DATETIME NOT NULL,
                merge_reason TEXT,
                confidence REAL,
                similarity_scores TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()
        conn.close()
        
        yield db_path
        
        # Cleanup
        os.unlink(db_path)
    
    def test_clear_existing_deduplications(self, temp_db):
        """Test clearing existing deduplications"""
        conn = sqlite3.connect(temp_db)
        storage = DeduplicationStorage(conn)
        
        # Add some test data
        cursor = conn.cursor()
        cursor.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, title) VALUES (?, ?)", 
                      ("test1", "Test Event"))
        conn.commit()
        
        # Clear deduplications
        storage.clear_existing_deduplications()
        
        # Verify data is cleared
        cursor.execute("SELECT COUNT(*) FROM DeduplicatedEvents")
        count = cursor.fetchone()[0]
        assert count == 0
        
        conn.close()
    
    def test_validate_storage_integrity_no_issues(self, temp_db):
        """Test storage integrity validation with no issues"""
        conn = sqlite3.connect(temp_db)
        storage = DeduplicationStorage(conn)
        
        errors = storage.validate_storage_integrity()
        
        # Should have no errors for empty database
        assert len(errors) == 0
        
        conn.close()
    
    def test_validate_storage_integrity_duplicate_events(self, temp_db):
        """Test storage integrity validation with duplicate events"""
        conn = sqlite3.connect(temp_db)
        storage = DeduplicationStorage(conn)
        
        # Add duplicate events
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO DeduplicatedEvents (deduplicated_event_id, title, event_date, status)
            VALUES (?, ?, ?, ?)
        """, ("id1", "Duplicate Event", "2023-01-01", "Active"))
        cursor.execute("""
            INSERT INTO DeduplicatedEvents (deduplicated_event_id, title, event_date, status)
            VALUES (?, ?, ?, ?)
        """, ("id2", "Duplicate Event", "2023-01-01", "Active"))
        conn.commit()
        
        errors = storage.validate_storage_integrity()
        
        # Should detect duplicate events
        assert len(errors) > 0
        assert any(error.error_type == "DUPLICATE_EVENT" for error in errors)
        
        conn.close()
    
    def test_get_deduplication_statistics(self, temp_db):
        """Test getting deduplication statistics"""
        conn = sqlite3.connect(temp_db)
        storage = DeduplicationStorage(conn)
        
        # Add some test data
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO DeduplicatedEvents (deduplicated_event_id, title, event_date, status, event_type, severity)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("id1", "Event 1", "2023-01-01", "Active", "Data Breach", "High"))
        cursor.execute("""
            INSERT INTO DeduplicationClusters (cluster_id, master_event_id, merge_timestamp, confidence)
            VALUES (?, ?, ?, ?)
        """, ("cluster1", "id1", "2023-01-01", 0.8))
        conn.commit()
        
        stats = storage.get_deduplication_statistics()
        
        assert stats['active_events'] == 1
        assert stats['merge_groups'] == 1
        assert 'date_range' in stats
        assert 'event_types' in stats
        assert 'severity_distribution' in stats
        
        conn.close()


class TestIntegration:
    """Integration tests for the complete deduplication system"""
    
    @pytest.fixture
    def temp_db(self):
        """Create a temporary database for testing"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        # Create database with required tables
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Create DeduplicatedEvents table
        cursor.execute("""
            CREATE TABLE DeduplicatedEvents (
                deduplicated_event_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT,
                event_date DATE,
                event_type TEXT,
                severity TEXT,
                records_affected INTEGER,
                data_sources TEXT,
                urls TEXT,
                status TEXT DEFAULT 'Active',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create EventDeduplicationMap table
        cursor.execute("""
            CREATE TABLE EventDeduplicationMap (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deduplicated_event_id TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                similarity_score REAL,
                merge_reason TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create DeduplicationClusters table
        cursor.execute("""
            CREATE TABLE DeduplicationClusters (
                cluster_id TEXT PRIMARY KEY,
                master_event_id TEXT NOT NULL,
                merge_timestamp DATETIME NOT NULL,
                merge_reason TEXT,
                confidence REAL,
                similarity_scores TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()
        conn.close()
        
        yield db_path
        
        # Cleanup
        os.unlink(db_path)
    
    def test_end_to_end_deduplication(self, temp_db):
        """Test complete end-to-end deduplication process"""
        conn = sqlite3.connect(temp_db)
        storage = DeduplicationStorage(conn)
        
        # Create test events with duplicates
        events = [
            CyberEvent(
                event_id="1",
                title="Optus Data Breach",
                summary="Optus experienced a major data breach",
                event_date=date(2023, 1, 1),
                event_type="Data Breach",
                severity="High",
                records_affected=1000000
            ),
            CyberEvent(
                event_id="2",
                title="Optus Security Incident",
                summary="Optus had a security incident affecting customers",
                event_date=date(2023, 1, 2),
                event_type="Security Incident",
                severity="High",
                records_affected=2000000
            ),
            CyberEvent(
                event_id="3",
                title="Telstra Network Outage",
                summary="Telstra experienced a network outage",
                event_date=date(2023, 1, 3),
                event_type="Network Outage",
                severity="Medium",
                records_affected=50000
            )
        ]
        
        # Run deduplication
        engine = DeduplicationEngine(similarity_threshold=0.7)
        result = engine.deduplicate(events)
        
        # Should merge the two Optus events
        assert len(result.unique_events) == 2
        assert len(result.merge_groups) == 1
        assert result.statistics.input_events == 3
        assert result.statistics.output_events == 2
        assert result.statistics.merge_groups == 1
        
        # Store results
        storage_result = storage.store_deduplication_result(result)
        assert storage_result.success == True
        assert storage_result.stored_events == 2
        assert storage_result.merge_groups_created == 1
        
        # Verify no duplicates in database
        integrity_errors = storage.validate_storage_integrity()
        assert len(integrity_errors) == 0
        
        conn.close()
    
    def test_idempotency(self, temp_db):
        """Test that running deduplication multiple times produces same result"""
        conn = sqlite3.connect(temp_db)
        storage = DeduplicationStorage(conn)
        
        events = [
            CyberEvent(event_id="1", title="Event 1", event_date=date(2023, 1, 1)),
            CyberEvent(event_id="2", title="Event 2", event_date=date(2023, 1, 2))
        ]
        
        engine = DeduplicationEngine()
        
        # Run deduplication twice
        result1 = engine.deduplicate(events)
        result2 = engine.deduplicate(events)
        
        # Results should be identical
        assert len(result1.unique_events) == len(result2.unique_events)
        assert len(result1.merge_groups) == len(result2.merge_groups)
        assert result1.statistics.input_events == result2.statistics.input_events
        assert result1.statistics.output_events == result2.statistics.output_events
        
        conn.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
