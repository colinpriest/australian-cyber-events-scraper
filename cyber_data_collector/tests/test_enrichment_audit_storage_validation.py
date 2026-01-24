from __future__ import annotations

import pytest

from cyber_data_collector.enrichment.enrichment_audit_storage import EnrichmentAuditStorage


def test_enrichment_audit_storage_requires_valid_db_path() -> None:
    with pytest.raises(TypeError):
        EnrichmentAuditStorage(None)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        EnrichmentAuditStorage("")


def test_save_audit_trail_rejects_invalid_payload_type() -> None:
    storage = EnrichmentAuditStorage(":memory:")

    with pytest.raises(TypeError):
        storage.save_audit_trail("not-a-dict")  # type: ignore[arg-type]


def test_get_audit_trail_validates_audit_id() -> None:
    storage = EnrichmentAuditStorage(":memory:")

    with pytest.raises(TypeError):
        storage.get_audit_trail(123)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        storage.get_audit_trail("")


def test_get_quality_report_validates_pipeline_version() -> None:
    storage = EnrichmentAuditStorage(":memory:")

    with pytest.raises(TypeError):
        storage.get_quality_report(1.0)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        storage.get_quality_report("")


def test_get_recent_audits_validates_limit() -> None:
    storage = EnrichmentAuditStorage(":memory:")

    with pytest.raises(TypeError):
        storage.get_recent_audits("10")  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        storage.get_recent_audits(0)
