from __future__ import annotations

import pytest

from cyber_data_collector.utils.validation import (
    safe_json_dumps,
    validate_and_correct_enrichment_data,
    validate_records_affected,
)


def test_validate_records_affected_rejects_bad_event_title_type() -> None:
    with pytest.raises(TypeError):
        validate_records_affected(100, event_title=None)  # type: ignore[arg-type]


def test_validate_records_affected_rejects_invalid_values() -> None:
    assert validate_records_affected(None) is None
    assert validate_records_affected(-10, event_title="Test Event") is None
    assert validate_records_affected(0, event_title="Test Event") is None
    assert validate_records_affected(10, event_title="Test Event") is None


def test_validate_records_affected_honors_org_thresholds() -> None:
    assert validate_records_affected(25_000_000, event_title="Small Local Org") is None
    assert validate_records_affected(25_000_000, event_title="Telstra data breach") == 25_000_000
    assert validate_records_affected(900_000_000, event_title="Google incident") == 900_000_000


def test_validate_and_correct_enrichment_data_validates_input_type() -> None:
    with pytest.raises(TypeError):
        validate_and_correct_enrichment_data("not-a-dict")  # type: ignore[arg-type]


def test_validate_and_correct_enrichment_data_updates_records_affected() -> None:
    payload = {"records_affected": 25}
    result = validate_and_correct_enrichment_data(payload, event_title="Example Event")
    assert result["records_affected"] is None


def test_safe_json_dumps_validates_context_and_serialization() -> None:
    assert safe_json_dumps({"ok": True}, "test payload") == '{"ok": true}'

    with pytest.raises(TypeError):
        safe_json_dumps({1, 2, 3}, "bad payload")

    with pytest.raises(TypeError):
        safe_json_dumps({"ok": True}, context=123)  # type: ignore[arg-type]
