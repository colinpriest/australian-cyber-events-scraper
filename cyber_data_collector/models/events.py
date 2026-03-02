from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
import uuid

from pydantic import BaseModel, ConfigDict, Field


class CyberEventType(str, Enum):
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


class EventSeverity(str, Enum):
    """Event severity levels."""

    CRITICAL = "Critical"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"
    UNKNOWN = "Unknown"


class EntityType(str, Enum):
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


class ConfidenceScore(BaseModel):
    """Confidence scoring for data quality."""

    overall: float = Field(..., ge=0.0, le=1.0, description="Overall confidence score")
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
    confidence: ConfidenceScore = Field(..., description="Data quality confidence scores")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Record creation timestamp")
    processed_at: Optional[datetime] = Field(None, description="Processing completion timestamp")
    duplicate_of: Optional[str] = Field(None, description="ID of master event if duplicate")
    merged_events: List[str] = Field(default_factory=list, description="IDs of events merged into this one")

    model_config = ConfigDict(json_encoders={datetime: lambda value: value.isoformat()})

