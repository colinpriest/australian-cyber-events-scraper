"""Regression tests for PerplexityEnrichmentEngine JSON parsing.

Perplexity frequently truncates its response at ``max_tokens``, cutting off the
trailing ``reasoning``/``sources_consulted`` fields mid-string. This produced
dozens of "Failed to parse Perplexity response: Unterminated string ..."
warnings and discarded the entire (otherwise useful) enrichment. The repair
path salvages every field that completed before the truncation point.
"""

from cyber_data_collector.processing.perplexity_enrichment import (
    PerplexityEnrichmentEngine,
    PerplexityEventEnrichment,
    PerplexityDuplicateCheck,
)


# A realistic response cut off mid-string in the trailing reasoning field —
# the exact "Unterminated string" failure mode seen in production logs.
TRUNCATED_ENRICHMENT = '''{
  "earliest_event_date": "2026-02-01",
  "date_confidence": 0.9,
  "formal_entity_name": "youX FinTech Pty Ltd",
  "entity_confidence": 0.85,
  "victim_industry": "Finance",
  "threat_actor": "Unknown",
  "attack_method": "data breach",
  "attack_method_confidence": 0.7,
  "victim_count": 12000,
  "sources_consulted": [
    "https://example.com/a",
    "https://example.com/b"
  ],
  "overall_confidence": 0.78,
  "reasoning": "The incident was first reported on Feb 1 2026 and the data exposed inclu'''


class TestRepairTruncatedJson:
    def test_repair_recovers_completed_fields(self):
        data = PerplexityEnrichmentEngine._repair_truncated_json(TRUNCATED_ENRICHMENT)
        assert data is not None
        # Every field that completed before the cut is recovered...
        assert data["formal_entity_name"] == "youX FinTech Pty Ltd"
        assert data["earliest_event_date"] == "2026-02-01"
        assert data["attack_method"] == "data breach"
        assert data["victim_count"] == 12000
        assert data["overall_confidence"] == 0.78
        assert data["sources_consulted"] == [
            "https://example.com/a",
            "https://example.com/b",
        ]
        # ...and the incomplete trailing field is dropped, not corrupted.
        assert "reasoning" not in data

    def test_repaired_data_builds_valid_model(self):
        data = PerplexityEnrichmentEngine._repair_truncated_json(TRUNCATED_ENRICHMENT)
        enrichment = PerplexityEventEnrichment(**data)
        assert enrichment.formal_entity_name == "youX FinTech Pty Ltd"
        assert enrichment.overall_confidence == 0.78

    def test_well_formed_json_is_unaffected_by_parse_path(self):
        engine = PerplexityEnrichmentEngine.__new__(PerplexityEnrichmentEngine)
        import logging
        engine.logger = logging.getLogger("test")
        good = (
            '{"formal_entity_name": "Optus", "overall_confidence": 0.9, '
            '"reasoning": "clear"}'
        )
        result = engine._parse_enrichment_response(good)
        assert result.formal_entity_name == "Optus"
        assert result.overall_confidence == 0.9

    def test_truncated_response_yields_partial_not_empty(self):
        engine = PerplexityEnrichmentEngine.__new__(PerplexityEnrichmentEngine)
        import logging
        engine.logger = logging.getLogger("test")
        result = engine._parse_enrichment_response(TRUNCATED_ENRICHMENT)
        # Before the fix this returned overall_confidence=0.0 with no fields.
        assert result.formal_entity_name == "youX FinTech Pty Ltd"
        assert result.overall_confidence == 0.78

    def test_unrepairable_response_returns_empty_enrichment(self):
        engine = PerplexityEnrichmentEngine.__new__(PerplexityEnrichmentEngine)
        import logging
        engine.logger = logging.getLogger("test")
        result = engine._parse_enrichment_response("this is not json at all")
        assert result.overall_confidence == 0.0
        assert result.formal_entity_name is None

    def test_duplicate_check_backfills_truncated_reasoning(self):
        engine = PerplexityEnrichmentEngine.__new__(PerplexityEnrichmentEngine)
        import logging
        engine.logger = logging.getLogger("test")
        truncated = (
            '{"are_same_incident": true, "confidence": 0.88, '
            '"reasoning": "both describe the youX FinTech breach on the same da'
        )
        result = engine._parse_duplicate_check_response(truncated)
        assert isinstance(result, PerplexityDuplicateCheck)
        assert result.are_same_incident is True
        assert result.confidence == 0.88
        assert result.reasoning  # backfilled, never empty
