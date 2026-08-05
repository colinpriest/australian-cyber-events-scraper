"""Pydantic models for deduplication decisions, evidence and verdicts.

Everything the pipeline decides is represented here so it can be serialised
into the audit log verbatim. The guiding rule: a decision is only valid if it
carries enough evidence for a human to independently agree or disagree with it.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class DedupAction(str, Enum):
    """What was done with a member event."""

    MERGE = "merge"
    KEEP_SEPARATE = "keep_separate"
    REVERT = "revert"


class DecidedBy(str, Enum):
    """Which layer of the pipeline made the call.

    Ordered weakest to strongest; a later layer may override an earlier one,
    and HUMAN always wins.
    """

    RULE = "rule"
    EMBEDDING = "embedding"
    LLM = "llm"
    HUMAN = "human"


class MatchEvidence(BaseModel):
    """The observable facts behind a pair decision.

    Stored as JSON on every decision so the dashboard can show a reviewer
    exactly what the pipeline saw, rather than just a similarity number.
    """

    entity_left: Optional[str] = None
    entity_right: Optional[str] = None
    entity_canonical_left: Optional[str] = None
    entity_canonical_right: Optional[str] = None
    entity_match: Optional[bool] = None

    date_left: Optional[str] = None
    date_right: Optional[str] = None
    date_delta_days: Optional[int] = None

    title_left: Optional[str] = None
    title_right: Optional[str] = None

    embedding_similarity: Optional[float] = Field(None, ge=-1.0, le=1.0)
    title_similarity: Optional[float] = Field(None, ge=0.0, le=1.0)

    shared_urls: List[str] = Field(default_factory=list)
    distinguishing_facts: List[str] = Field(default_factory=list)
    supporting_facts: List[str] = Field(default_factory=list)

    def summary_line(self) -> str:
        """One-line human summary used in dashboard tables."""
        bits: List[str] = []
        if self.entity_canonical_left and self.entity_canonical_right:
            same = "=" if self.entity_match else "!="
            bits.append(
                f"entity {self.entity_canonical_left} {same} {self.entity_canonical_right}"
            )
        if self.date_delta_days is not None:
            bits.append(f"dates {self.date_delta_days}d apart")
        if self.embedding_similarity is not None:
            bits.append(f"embed {self.embedding_similarity:.2f}")
        if self.shared_urls:
            bits.append(f"{len(self.shared_urls)} shared url(s)")
        return "; ".join(bits) or "no evidence recorded"


class PairVerdict(BaseModel):
    """An adjudicator's answer on whether two events are the same incident."""

    is_same_event: bool
    certainty: float = Field(..., ge=0.0, le=1.0)
    reasoning: str = Field(..., min_length=1)
    decided_by: DecidedBy = DecidedBy.LLM
    evidence: MatchEvidence = Field(default_factory=MatchEvidence)

    @field_validator("reasoning")
    @classmethod
    def _reasoning_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("reasoning must explain the verdict")
        return v.strip()

    @property
    def is_confident(self) -> bool:
        """True when the verdict is strong enough to apply without review."""
        return self.certainty >= 0.85


class LLMPairAdjudication(BaseModel):
    """Structured output schema for the LLM adjudicator.

    Deliberately small and unambiguous: the model returns a judgement plus the
    facts that drove it, and we build the richer PairVerdict around that.
    """

    is_same_event: bool = Field(
        description=(
            "True only if both records describe the SAME real-world security "
            "incident at the SAME organisation. Coverage of a different "
            "incident at the same organisation is NOT the same event."
        )
    )
    certainty: float = Field(
        ge=0.0, le=1.0,
        description="0.0-1.0 confidence in the judgement. Use <0.7 when the "
                    "evidence is genuinely ambiguous.",
    )
    reasoning: str = Field(
        description="One or two sentences a reviewer can check, naming the "
                    "decisive facts."
    )
    supporting_facts: List[str] = Field(
        default_factory=list,
        description="Concrete facts shared by both records (same victim, same "
                    "breach vector, same record count, same dates).",
    )
    distinguishing_facts: List[str] = Field(
        default_factory=list,
        description="Concrete facts that differ and argue against merging.",
    )


class DedupDecision(BaseModel):
    """One append-only row in the decision ledger."""

    decision_id: str
    batch_id: str
    enriched_event_id: str
    deduplicated_event_id: Optional[str] = None
    cluster_key: Optional[str] = None
    action: DedupAction
    decided_by: DecidedBy
    certainty: Optional[float] = Field(None, ge=0.0, le=1.0)
    method: Optional[str] = None
    reasoning: Optional[str] = None
    evidence: MatchEvidence = Field(default_factory=MatchEvidence)
    superseded_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)

    def to_row(self) -> Dict[str, Any]:
        """Flatten to the DedupDecisions column layout."""
        return {
            "decision_id": self.decision_id,
            "batch_id": self.batch_id,
            "enriched_event_id": self.enriched_event_id,
            "deduplicated_event_id": self.deduplicated_event_id,
            "cluster_key": self.cluster_key,
            "action": self.action.value,
            "decided_by": self.decided_by.value,
            "certainty": self.certainty,
            "method": self.method,
            "reasoning": self.reasoning,
            "evidence_json": self.evidence.model_dump_json(),
            "superseded_by": self.superseded_by,
            "created_at": self.created_at.isoformat(),
        }


class OverrideVerdict(str, Enum):
    """A human's ruling on a pair."""

    SAME = "same"
    DIFFERENT = "different"
