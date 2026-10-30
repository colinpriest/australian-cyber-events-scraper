from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
import uuid

from pydantic import BaseModel, ConfigDict, Field


class _ValueEnum(str, Enum):
    """A string enum that stringifies to its value.

    On Python 3.11+ ``str()`` and ``format()`` of a ``(str, Enum)`` member give
    the qualified name (``"EventSeverity.MEDIUM"``), and every writer that
    stringified a member stored that name instead of ``"Medium"``. Stringifying
    to the value makes ``str(member)``, ``f"{member}"`` and the member itself
    store the same text. See :mod:`cyber_data_collector.utils.enum_values`.
    """

    def __str__(self) -> str:
        return str(self.value)

    def __format__(self, format_spec: str) -> str:
        return format(str(self.value), format_spec)


class CyberEventType(_ValueEnum):
    """Standardized cyber event categories."""

    RANSOMWARE = "Ransomware"
    DATA_BREACH = "Data Breach"
    PHISHING = "Phishing"
    MALWARE = "Malware"
    VULNERABILITY_EXPLOIT = "Vulnerability Exploit"
    STATE_SPONSORED_ATTACK = "State-Sponsored Attack"
    SUPPLY_CHAIN_ATTACK = "Supply Chain Attack"
    INSIDER_THREAT = "Insider Threat"
    DENIAL_OF_SERVICE = "Denial of Service"
    FINANCIAL_FRAUD = "Financial Fraud"
    IDENTITY_THEFT = "Identity Theft"
    INFRASTRUCTURE_ATTACK = "Infrastructure Attack"
    OTHER = "Other"


class EventSeverity(_ValueEnum):
    """Event severity levels."""

    CRITICAL = "Critical"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"
    UNKNOWN = "Unknown"


class EntityType(_ValueEnum):
    """Types of entities affected by cyber events."""

    GOVERNMENT = "Government"
    FINANCIAL = "Financial Institution"
    HEALTHCARE = "Healthcare"
    EDUCATION = "Education"
    TECHNOLOGY = "Technology"
    RETAIL = "Retail"
    ENERGY = "Energy/Utilities"
    TELECOMMUNICATIONS = "Telecommunications"
    MANUFACTURING = "Manufacturing"
    TRANSPORTATION = "Transportation"
    MEDIA = "Media"
    NON_PROFIT = "Non-Profit"
    INDIVIDUAL = "Individual"
    OTHER = "Other"


#: ``ConfidenceScore.score_version`` of a score that is a fixed prior of the data source that found the record.
SOURCE_PRIOR_VERSION = "source-prior-v1"
#: ``ConfidenceScore.score_version`` of a prior the classification model then adjusted.
CLASSIFIER_ADJUSTED_VERSION = "classifier-adjusted-v1"


class ConfidenceScore(BaseModel):
    """Confidence scoring for data quality.

    ``overall`` is ``None`` when nothing measured it. It is stored as NULL and
    every reader treats NULL as unknown - it is never replaced by a default
    that would read as model output (0.7 and 0.8 were).

    ``model`` is the model that produced or adjusted the score (None for a
    data source's fixed prior); ``score_version`` names the rule that produced
    it (:data:`SOURCE_PRIOR_VERSION`, :data:`CLASSIFIER_ADJUSTED_VERSION`).
    """

    overall: Optional[float] = Field(None, ge=0.0, le=1.0,
                                     description="Overall confidence score; None = not measured")
    model: Optional[str] = Field(None, description="Model that produced or adjusted the score; None = a fixed prior")
    score_version: Optional[str] = Field(SOURCE_PRIOR_VERSION, description="Version of the rule that produced it")
    source_reliability: float = Field(..., ge=0.0, le=1.0, description="Source reliability score")
    data_completeness: float = Field(..., ge=0.0, le=1.0, description="Data completeness score")
    temporal_accuracy: float = Field(..., ge=0.0, le=1.0, description="Temporal accuracy score")
    geographic_accuracy: float = Field(..., ge=0.0, le=1.0, description="Geographic accuracy score")


class AffectedEntity(BaseModel):
    """Entity affected by a cyber event."""

    name: str = Field(..., description="Entity name")
    entity_type: EntityType = Field(..., description="Type of entity")
    industry_sector: Optional[str] = Field(None, description="Specific industry sector")
    location: Optional[str] = Field(None, description="Geographic location")
    coordinates: Optional[Tuple[float, float]] = Field(None, description="Latitude, longitude")
    australian_entity: bool = Field(..., description="Whether entity is Australian")
    confidence_score: float = Field(..., ge=0.0, le=1.0, description="Confidence in entity identification")


class FinancialImpact(BaseModel):
    """Financial impact information."""

    estimated_cost: Optional[float] = Field(None, ge=0, description="Estimated financial cost in AUD")
    regulatory_fine: Optional[float] = Field(None, ge=0, description="Regulatory fine amount in AUD")
    regulatory_fine_currency: Optional[str] = Field("AUD", description="Currency of regulatory fine")
    regulatory_undertaking: Optional[str] = Field(None, description="Description of regulatory undertaking")
    customers_affected: Optional[int] = Field(None, ge=0, description="Number of customers/records affected")
    revenue_impact: Optional[float] = Field(None, ge=0, description="Revenue impact in AUD")


class EventSource(BaseModel):
    """Information about a data source associated with an event."""

    source_id: str = Field(..., description="Unique source identifier")
    source_type: str = Field(..., description="Type of data source")
    url: Optional[str] = Field(None, description="Source URL")
    title: Optional[str] = Field(None, description="Source title")
    content_snippet: Optional[str] = Field(None, description="Content excerpt")
    publication_date: Optional[datetime] = Field(None, description="Publication date")
    domain: Optional[str] = Field(None, description="Source domain")
    credibility_score: float = Field(..., ge=0.0, le=1.0, description="Source credibility score")
    relevance_score: float = Field(..., ge=0.0, le=1.0, description="Content relevance score")
    language: str = Field("en", description="Content language")


class CyberEvent(BaseModel):
    """Comprehensive cyber event model."""

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Unique event identifier")
    raw_event_id: Optional[str] = Field(None, description="Database raw event ID")
    enriched_event_id: Optional[str] = Field(None, description="Database enriched event ID")
    external_ids: Dict[str, str] = Field(default_factory=dict, description="External system IDs")
    title: str = Field(..., description="Event title/headline")
    description: str = Field(..., description="Detailed event description")
    contributing_raw_events: int = Field(default=1, description="Number of raw events that contributed to this deduplicated event")
    contributing_enriched_events: int = Field(default=1, description="Number of enriched events that contributed to this deduplicated event")
    event_type: CyberEventType = Field(..., description="Primary event category")
    secondary_types: List[CyberEventType] = Field(default_factory=list, description="Secondary event categories")
    severity: EventSeverity = Field(..., description="Event severity level")
    event_date: Optional[datetime] = Field(None, description="When the event occurred")
    event_date_precision: Optional[str] = Field(
        None, description="How precisely the source states event_date: day, month or year (None = not stated)")
    event_date_basis: Optional[str] = Field(None, description="Where the source's event_date comes from")
    discovery_date: Optional[datetime] = Field(None, description="When the event was discovered")
    publication_date: Optional[datetime] = Field(None, description="When the event was first reported")
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Last update timestamp")
    primary_entity: Optional[AffectedEntity] = Field(None, description="Primary affected entity")
    affected_entities: List[AffectedEntity] = Field(default_factory=list, description="All affected entities")
    location: Optional[str] = Field(None, description="Primary event location")
    coordinates: Optional[Tuple[float, float]] = Field(None, description="Event coordinates")
    australian_relevance: bool = Field(..., description="Whether event has Australian relevance")
    financial_impact: Optional[FinancialImpact] = Field(None, description="Financial impact details")
    technical_details: Optional[Dict[str, Any]] = Field(None, description="Technical attack details")
    response_actions: List[str] = Field(default_factory=list, description="Response and remediation actions")
    attribution: Optional[str] = Field(None, description="Attack attribution if known")
    data_sources: List[EventSource] = Field(default_factory=list, description="All data sources for this event")
    source_provenance: Optional[Dict[str, Any]] = Field(
        None, description="Where the source got this record (e.g. the Perplexity answer item and its sibling "
                          "citations); stored as RawEvents.source_metadata.provenance")
    confidence: ConfidenceScore = Field(..., description="Data quality confidence scores")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Record creation timestamp")
    processed_at: Optional[datetime] = Field(None, description="Processing completion timestamp")
    duplicate_of: Optional[str] = Field(None, description="ID of master event if duplicate")
    merged_events: List[str] = Field(default_factory=list, description="IDs of events merged into this one")
    evidence_basis: str = Field(
        "unscraped",
        description="What the persisted record holds: 'article' (scraped text), 'snippet_only', 'none', or "
                    "'unscraped' if the event never went through the persisted-evidence path",
    )
    evidence_text: Optional[str] = Field(None, description="Persisted scraped article text, when there is any")
    classification_status: str = Field(
        "unclassified",
        description="'classified' once the LLM judged it, 'failed' if the LLM call errored (not an acceptance)",
    )
    classification_error: Optional[str] = Field(None, description="Error text when classification_status is 'failed'")
    classification_error_kind: Optional[str] = Field(
        None,
        description="'temporary' (provider failure: timeout, 429/5xx, connection, budget), 'permanent' "
                    "(recognised as the record's own: invalid content, a refused request) or 'unknown' "
                    "(not recognised either way: retried with a long back-off) when classification_status is 'failed'",
    )

    model_config = ConfigDict(json_encoders={datetime: lambda value: value.isoformat()})

