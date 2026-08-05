"""Deduplication v3: auditable, reversible, learning deduplication.

This package replaces the ad-hoc regex/threshold matching in
``processing/deduplication_v2.py`` with a pipeline that records *why* every
merge happened, lets a human reverse any of it without re-running the
pipeline, and folds those human corrections back into later runs.

Modules:
    schema          - additive migration for the audit/override/alias tables
    models          - Pydantic models for decisions, evidence and verdicts
    entity_resolution - canonical entity naming (fixes variant under-merging)
    adjudicator     - embedding recall + LLM precision, with certainty scores
    ledger          - append-only decision log, apply/revert, overrides
    backfill        - repair provenance and lineage for existing rows
"""

from cyber_data_collector.dedup.models import (
    DedupAction,
    DedupDecision,
    DecidedBy,
    MatchEvidence,
    PairVerdict,
)

__all__ = [
    "DedupAction",
    "DedupDecision",
    "DecidedBy",
    "MatchEvidence",
    "PairVerdict",
]
