from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, model_validator


class DateRange(BaseModel):
    """Date range for data collection."""

    start_date: datetime = Field(..., description="Start date for data collection")
    end_date: Optional[datetime] = Field(None, description="End date for data collection")

    @model_validator(mode="after")
    def validate_date_order(self) -> DateRange:
        """Ensure start_date is not after end_date."""
        if self.end_date is not None and self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        return self


class DataSourceConfig(BaseModel):
    """Configuration for individual data sources."""

    name: str = Field("", description="Data source name")
    enabled: bool = Field(True, description="Whether this data source is enabled")
    priority: int = Field(1, ge=1, le=10, description="Processing priority (1-10)")
    rate_limit: int = Field(60, ge=1, description="Requests per minute")
    timeout: int = Field(30, ge=1, description="Request timeout in seconds")
    retry_attempts: int = Field(3, description="Number of retry attempts")
    custom_config: Dict[str, Any] = Field(default_factory=dict, description="Source-specific configuration")


class CollectionConfig(BaseModel):
    """Main collection configuration."""

    date_range: DateRange = Field(..., description="Date range for collection")
    max_threads: int = Field(10, ge=1, description="Maximum concurrent threads")
    batch_size: int = Field(20, ge=1, description="Batch size for processing")
    enable_deduplication: bool = Field(True, description="Enable event deduplication")
    confidence_threshold: float = Field(0.7, ge=0.0, le=1.0, description="Minimum confidence threshold")
    gdelt_config: DataSourceConfig = Field(default_factory=DataSourceConfig)
    perplexity_config: DataSourceConfig = Field(default_factory=DataSourceConfig)
    google_search_config: DataSourceConfig = Field(default_factory=DataSourceConfig)
    webber_config: DataSourceConfig = Field(default_factory=DataSourceConfig)
    oaic_config: DataSourceConfig = Field(default_factory=DataSourceConfig)
