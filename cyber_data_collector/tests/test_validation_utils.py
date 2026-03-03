from __future__ import annotations

import pytest

import cyber_data_collector.utils.validation as _val_module
from cyber_data_collector.utils.validation import (
    safe_json_dumps,
    validate_and_correct_enrichment_data,
    validate_enrichment_data_for_storage,
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


def test_validate_enrichment_data_rejects_event_when_perplexity_says_not_cyber(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Perplexity returns is_cyber=False, the event must be marked as rejected.

    The Tabcorp in-play betting case is the canonical example: records_affected=37
    triggered the low-value heuristic, Perplexity confirmed the event was a
    wagering compliance issue (not a cyber incident), and the event should come
    out of validate_enrichment_data_for_storage with is_australian_event=False
    and is_specific_event=False.
    """

    def _mock_llm_validate(
        value,
        event_title="",
        org_name=None,
        description=None,
        perplexity_api_key=None,
    ):
        # Simulate Perplexity explicitly saying: not a cyber incident
        return None, False

    monkeypatch.setattr(_val_module, "llm_validate_records_affected", _mock_llm_validate)

    data = {
        "title": "Tabcorp says bad data, technical error behind online betting breach",
        "records_affected": 37,
        "is_australian_event": True,
        "is_specific_event": True,
        "confidence_score": 0.8,
    }

    result = validate_enrichment_data_for_storage(
        data,
        event_title="Tabcorp says bad data, technical error behind online betting breach",
        perplexity_api_key="fake-key",
    )

    assert result["records_affected"] is None
    assert result["is_australian_event"] is False
    assert result["is_specific_event"] is False


def test_validate_enrichment_data_preserves_event_when_perplexity_confirms_cyber(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Perplexity confirms is_cyber=True, is_australian_event must not be overwritten."""

    def _mock_llm_validate(
        value,
        event_title="",
        org_name=None,
        description=None,
        perplexity_api_key=None,
    ):
        return 50_000, True

    monkeypatch.setattr(_val_module, "llm_validate_records_affected", _mock_llm_validate)

    data = {
        "title": "Medibank data breach",
        "records_affected": 37,
        "is_australian_event": True,
        "is_specific_event": True,
        "confidence_score": 0.9,
    }

    result = validate_enrichment_data_for_storage(
        data,
        event_title="Medibank data breach",
        perplexity_api_key="fake-key",
    )

    assert result["records_affected"] == 50_000
    assert result["is_australian_event"] is True
    assert result["is_specific_event"] is True


def test_safe_json_dumps_validates_context_and_serialization() -> None:
    assert safe_json_dumps({"ok": True}, "test payload") == '{"ok": true}'

    with pytest.raises(TypeError):
        safe_json_dumps({1, 2, 3}, "bad payload")

    with pytest.raises(TypeError):
        safe_json_dumps({"ok": True}, context=123)  # type: ignore[arg-type]
