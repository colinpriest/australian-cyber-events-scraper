"""
Enrichment Audit Storage - Store enrichment audit trails in database.

This module handles persisting enrichment audit trails to the database for
tracking, monitoring, and quality analysis.
"""

import sqlite3
import json
import logging
import uuid
from typing import Dict, Any
from datetime import datetime


class EnrichmentAuditStorage:
    """Store and retrieve enrichment audit trails"""

    def __init__(self, db_path: str):
        """Initialize audit storage"""
        self.db_path = db_path
        self.logger = logging.getLogger(__name__)

    def save_audit_trail(self, pipeline_result: Dict[str, Any]) -> str:
        """
        Save enrichment audit trail to database.

        Args:
            pipeline_result: Complete result from HighQualityEnrichmentPipeline.enrich_event()

        Returns:
            audit_id: UUID of created audit trail record
        """

        audit_trail = pipeline_result.get('audit_trail', {})
        enrichment_result = pipeline_result.get('enrichment_result', {})
        content_acquisition = pipeline_result.get('content_acquisition', {})
        fact_check_result = pipeline_result.get('fact_check_result', {})
        validation_result = pipeline_result.get('validation_result', {})
        final_decision = pipeline_result.get('final_decision', {})

        # Generate audit ID
        audit_id = str(uuid.uuid4())

        # Extract key fields from pipeline result
        enriched_event_id = audit_trail.get('event_id')
        started_at = audit_trail.get('started_at')
        completed_at = audit_trail.get('completed_at')
        total_time = audit_trail.get('total_time_seconds')

        # Stage 1: Content Acquisition
        stage1_success = content_acquisition.get('extraction_success', False)
        stage1_method = content_acquisition.get('extraction_method')
        stage1_content_length = content_acquisition.get('content_length', 0)
        stage1_source_reliability = content_acquisition.get('source_reliability', 0.0)
        stage1_details = json.dumps({
            'title': content_acquisition.get('title'),
            'url': content_acquisition.get('url'),
            'source_domain': content_acquisition.get('source_domain'),
            'publication_date': content_acquisition.get('publication_date'),
            'error': content_acquisition.get('error')
        })

        # Stage 2: GPT-4o Extraction
        stage2_success = enrichment_result.get('overall_confidence', 0) > 0
        stage2_victim = enrichment_result.get('victim', {}).get('organization')
        stage2_confidence = enrichment_result.get('overall_confidence', 0.0)
        stage2_is_specific = enrichment_result.get('specificity', {}).get('is_specific_incident')
        stage2_australian_relevance = enrichment_result.get('australian_relevance', {}).get('relevance_score', 0.0)
        stage2_tokens = enrichment_result.get('extraction_metadata', {}).get('tokens_used', 0)
        stage2_details = json.dumps({
            'victim': enrichment_result.get('victim', {}),
            'attacker': enrichment_result.get('attacker', {}),
            'incident': enrichment_result.get('incident', {}),
            'australian_relevance': enrichment_result.get('australian_relevance', {}),
            'specificity': enrichment_result.get('specificity', {}),
            'multi_victim': enrichment_result.get('multi_victim', {}),
            'extraction_notes': enrichment_result.get('extraction_notes', ''),
            'extraction_metadata': enrichment_result.get('extraction_metadata', {})
        })

        # Stage 3: Perplexity Fact-Checking
        stage3_checks_performed = fact_check_result.get('checks_performed', 0)
        stage3_checks_passed = fact_check_result.get('checks_passed', 0)
        stage3_checks_failed = fact_check_result.get('checks_failed', 0)
        stage3_verification_confidence = fact_check_result.get('overall_verification_confidence', 0.0)
        stage3_details = json.dumps({
            'details': fact_check_result.get('details', []),
            'timestamp': fact_check_result.get('timestamp')
        })

        # Stage 4: Validation
        stage4_is_valid = validation_result.get('is_valid', False)
        stage4_error_count = len(validation_result.get('errors', []))
        stage4_warning_count = len(validation_result.get('warnings', []))
        stage4_validation_confidence = validation_result.get('validation_confidence', 0.0)
        stage4_details = json.dumps({
            'errors': validation_result.get('errors', []),
            'warnings': validation_result.get('warnings', [])
        })

        # Stage 5: Final Decision
        decision = final_decision.get('decision')
        confidence = final_decision.get('final_confidence', 0.0)
        stage5_confidences = json.dumps(final_decision.get('stage_confidences', {}))
        stage5_penalties = json.dumps(final_decision.get('applied_penalties', {}))

        # Error handling
        error_message = audit_trail.get('error')
        error_stage = None
        if error_message:
            # Try to determine which stage failed
            for stage_info in audit_trail.get('stages', []):
                if not stage_info.get('success', True):
                    error_stage = stage_info.get('name')
                    break

        # Insert into database
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("""
                INSERT INTO EnrichmentAuditTrail (
                    audit_id,
                    enriched_event_id,
                    pipeline_version,
                    started_at,
                    completed_at,
                    total_time_seconds,
                    final_decision,
                    final_confidence,
                    stage1_success,
                    stage1_extraction_method,
                    stage1_content_length,
                    stage1_source_reliability,
                    stage1_details,
                    stage2_success,
                    stage2_victim_organization,
                    stage2_confidence,
                    stage2_is_specific_incident,
                    stage2_australian_relevance,
                    stage2_tokens_used,
                    stage2_details,
                    stage3_checks_performed,
                    stage3_checks_passed,
                    stage3_checks_failed,
                    stage3_verification_confidence,
                    stage3_details,
                    stage4_is_valid,
                    stage4_error_count,
                    stage4_warning_count,
                    stage4_validation_confidence,
                    stage4_details,
                    stage5_stage_confidences,
                    stage5_penalties_applied,
                    error_message,
                    error_stage
                ) VALUES (
                    ?, ?, '1.0', ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?
                )
            """, (
                audit_id,
                enriched_event_id,
                started_at,
                completed_at,
                total_time,
                decision,
                confidence,
                stage1_success,
                stage1_method,
                stage1_content_length,
                stage1_source_reliability,
                stage1_details,
                stage2_success,
                stage2_victim,
                stage2_confidence,
                stage2_is_specific,
                stage2_australian_relevance,
                stage2_tokens,
                stage2_details,
                stage3_checks_performed,
                stage3_checks_passed,
                stage3_checks_failed,
                stage3_verification_confidence,
                stage3_details,
                stage4_is_valid,
                stage4_error_count,
                stage4_warning_count,
                stage4_validation_confidence,
                stage4_details,
                stage5_confidences,
                stage5_penalties,
                error_message,
                error_stage
            ))

            conn.commit()
            self.logger.info(f"✓ Saved audit trail: {audit_id}")

            return audit_id

        except Exception as e:
            conn.rollback()
            self.logger.error(f"Failed to save audit trail: {e}")
            raise

        finally:
            conn.close()

    def get_audit_trail(self, audit_id: str) -> Dict[str, Any]:
        """Retrieve an audit trail by ID"""

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        try:
            cursor.execute("""
                SELECT * FROM EnrichmentAuditTrail
                WHERE audit_id = ?
            """, (audit_id,))

            row = cursor.fetchone()

            if not row:
                return None

            # Convert to dict and parse JSON fields
            audit = dict(row)

            # Parse JSON fields
            json_fields = [
                'stage1_details', 'stage2_details', 'stage3_details',
                'stage4_details', 'stage5_stage_confidences', 'stage5_penalties_applied'
            ]

            for field in json_fields:
                if audit.get(field):
                    try:
                        audit[field] = json.loads(audit[field])
                    except:
                        pass

            return audit

        finally:
            conn.close()

    def get_quality_report(self, pipeline_version: str = '1.0') -> Dict[str, Any]:
        """Get quality report for a pipeline version"""

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        try:
            cursor.execute("""
                SELECT * FROM EnrichmentQualityReport
                WHERE pipeline_version = ?
            """, (pipeline_version,))

            row = cursor.fetchone()

            if not row:
                return None

            return dict(row)

        finally:
            conn.close()

    def get_recent_audits(self, limit: int = 10) -> list:
        """Get recent audit trails"""

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        try:
            cursor.execute("""
                SELECT
                    audit_id,
                    enriched_event_id,
                    pipeline_version,
                    final_decision,
                    final_confidence,
                    stage2_victim_organization,
                    total_time_seconds,
                    created_at
                FROM EnrichmentAuditTrail
                ORDER BY created_at DESC
                LIMIT ?
            """, (limit,))

            rows = cursor.fetchall()
            return [dict(row) for row in rows]

        finally:
            conn.close()


def test_audit_storage():
    """Test audit storage"""

    import logging
    logging.basicConfig(level=logging.INFO)

    storage = EnrichmentAuditStorage('instance/cyber_events.db')

    # Create mock pipeline result
    mock_result = {
        'audit_trail': {
            'event_id': 'test-event-123',
            'url': 'https://example.com/test',
            'started_at': datetime.now().isoformat(),
            'completed_at': datetime.now().isoformat(),
            'total_time_seconds': 45.2,
            'stages': []
        },
        'enrichment_result': {
            'victim': {'organization': 'Test Corp'},
            'overall_confidence': 0.85,
            'specificity': {'is_specific_incident': True},
            'australian_relevance': {'relevance_score': 0.9},
            'extraction_metadata': {'tokens_used': 1500}
        },
        'content_acquisition': {
            'extraction_success': True,
            'extraction_method': 'newspaper3k',
            'content_length': 500,
            'source_reliability': 0.8,
            'source_domain': 'example.com'
        },
        'fact_check_result': {
            'checks_performed': 2,
            'checks_passed': 2,
            'checks_failed': 0,
            'overall_verification_confidence': 0.95,
            'details': []
        },
        'validation_result': {
            'is_valid': True,
            'errors': [],
            'warnings': [],
            'validation_confidence': 1.0
        },
        'final_decision': {
            'decision': 'AUTO_ACCEPT',
            'final_confidence': 0.88,
            'stage_confidences': {
                'gpt4o_extraction': 0.85,
                'perplexity_fact_check': 0.95,
                'validation': 1.0,
                'source_reliability': 0.8
            },
            'applied_penalties': {}
        }
    }

    # Save audit trail
    audit_id = storage.save_audit_trail(mock_result)
    print(f"Saved audit trail: {audit_id}")

    # Retrieve it
    audit = storage.get_audit_trail(audit_id)
    print(f"\nRetrieved audit trail:")
    print(f"  Decision: {audit['final_decision']}")
    print(f"  Confidence: {audit['final_confidence']}")
    print(f"  Victim: {audit['stage2_victim_organization']}")

    # Get quality report
    report = storage.get_quality_report('1.0')
    if report:
        print(f"\nQuality Report:")
        print(f"  Total events: {report['total_events']}")
        print(f"  Auto-accept: {report['auto_accept_count']}")
        print(f"  Avg confidence: {report['avg_confidence']:.2f}")


if __name__ == '__main__':
    test_audit_storage()
