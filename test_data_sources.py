#!/usr/bin/env python3
"""Australian Cyber Events Data Sources Test Script.

This script validates the configured cyber event data sources for June 2025,
providing detailed console reporting, performance insights, and data quality
metrics. Run with ``python test_data_sources.py``. See ``--help`` for options.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from enum import Enum

if sys.platform == "win32" and sys.stdout.encoding != "utf-8":
    import codecs
    sys.stdout = codecs.getwriter("utf-8")(sys.stdout.buffer)

try:  # pragma: no cover - optional dependency
    from google.oauth2.credentials import Credentials  # type: ignore
except ImportError:  # pragma: no cover
    Credentials = None

try:  # pragma: no cover - optional dependency
    import psutil  # type: ignore
except ImportError:  # pragma: no cover
    psutil = None

try:  # pragma: no cover - optional dependency
    from google.cloud import bigquery  # type: ignore
except ImportError:  # pragma: no cover
    bigquery = None

import requests
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from cyber_data_collector import (
    CollectionConfig,
    CyberDataCollector,
    DataSourceConfig,
    DateRange,
)
from cyber_data_collector.datasources import (
    GDELTDataSource,
    GoogleSearchDataSource,
    PerplexityDataSource,
    WebberInsuranceDataSource,
)
from cyber_data_collector.models.config import DataSourceConfig as CollectorDataSourceConfig
from cyber_data_collector.models.events import CyberEvent
from cyber_data_collector.storage import CacheManager, DatabaseManager
from cyber_data_collector.utils import ConfigManager, RateLimiter
from cyber_event_data import CyberEventData


# --------------------------------------------------------------------------- 
# Test configuration models
# --------------------------------------------------------------------------- 


class TestSeverity(str, Enum):
    """Test result severity levels."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class TestStatus(str, Enum):
    """Test execution status."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"


class DataSourceTestConfig(BaseModel):
    """Configuration for individual data source tests."""

    enabled: bool = Field(True, description="Whether to test this data source")
    timeout_seconds: int = Field(300, description="Maximum time to wait for results")
    max_events_expected: int = Field(1000, description="Maximum events expected")
    min_events_expected: int = Field(1, description="Minimum events expected for pass")
    retry_attempts: int = Field(3, description="Number of retry attempts on failure")
    validate_data_quality: bool = Field(True, description="Perform data quality validation")
    test_australian_relevance: bool = Field(True, description="Verify Australian relevance")
    performance_monitoring: bool = Field(True, description="Enable performance monitoring")
    rate_limit_per_minute: Optional[int] = Field(
        None, description="Override rate limit for this data source (requests/minute)"
    )


class TestConfig(BaseModel):
    """Main test configuration."""

    test_start_date: datetime = Field(default=datetime(2025, 6, 1))
    test_end_date: datetime = Field(default=datetime(2025, 6, 30))

    console_output_level: TestSeverity = Field(default=TestSeverity.INFO)
    detailed_reporting: bool = Field(True)
    show_progress_indicators: bool = Field(True)
    colored_output: bool = Field(True)

    fail_fast: bool = Field(False)
    parallel_testing: bool = Field(True)
    max_parallel_tests: int = Field(4)

    gdelt_config: DataSourceTestConfig = Field(default_factory=DataSourceTestConfig)
    perplexity_config: DataSourceTestConfig = Field(default_factory=DataSourceTestConfig)
    google_search_config: DataSourceTestConfig = Field(default_factory=DataSourceTestConfig)
    webber_config: DataSourceTestConfig = Field(default_factory=DataSourceTestConfig)

    min_australian_relevance_percentage: float = Field(0.80)
    min_data_quality_score: float = Field(0.70)
    max_duplicate_percentage: float = Field(0.20)


class PerformanceMetrics(BaseModel):
    """Performance measurement metrics."""

    response_time_seconds: float = Field(...)
    requests_per_minute: float = Field(0.0)
    rate_limit_hits: int = Field(0)
    timeout_occurrences: int = Field(0)
    retry_count: int = Field(0)
    memory_usage_mb: Optional[float] = Field(None)
    cpu_usage_percent: Optional[float] = Field(None)
    peak_memory_mb: Optional[float] = Field(None)


class DataQualityMetrics(BaseModel):
    """Data quality assessment metrics."""

    completeness_score: float = Field(...)
    accuracy_score: float = Field(...)
    consistency_score: float = Field(...)
    relevance_score: float = Field(...)
    duplicate_percentage: float = Field(...)
    missing_fields_count: int = Field(0)
    invalid_dates_count: int = Field(0)
    invalid_entities_count: int = Field(0)
    confidence_scores: List[float] = Field(default_factory=list)


class TestResult(BaseModel):
    """Individual test result."""

    test_name: str
    data_source: str
    status: TestStatus
    severity: TestSeverity
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    events_collected: int = 0
    events_valid: int = 0
    events_australian: int = 0
    error_message: Optional[str] = None
    error_details: Optional[Dict[str, Any]] = None
    performance_metrics: Optional[Dict[str, Any]] = None
    data_quality_metrics: Optional[Dict[str, Any]] = None
    warnings: List[str] = Field(default_factory=list)


class TestSummary(BaseModel):
    """Overall test execution summary."""

    total_tests: int
    passed_tests: int
    failed_tests: int
    skipped_tests: int
    error_tests: int
    total_duration_seconds: float
    total_events_collected: int
    overall_success_rate: float
    critical_failures: List[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- 
# Console reporting helpers
# --------------------------------------------------------------------------- 


class ColorCodes:
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    END = "\033[0m"


class ConsoleReporter:
    """Console reporter with optional colored output."""

    def __init__(self, use_colors: bool = True):
        self.use_colors = use_colors and sys.stdout.isatty()
        self.start_time = datetime.now()

    def _colorize(self, text: str, color: str) -> str:
        if self.use_colors:
            return f"{color}{text}{ColorCodes.END}"
        return text

    def print_header(self, text: str) -> None:
        separator = "=" * 60
        print(f"\n{self._colorize(separator, ColorCodes.CYAN)}")
        print(self._colorize(text.center(60), ColorCodes.CYAN + ColorCodes.BOLD))
        print(f"{self._colorize(separator, ColorCodes.CYAN)}\n")

    def print_subheader(self, text: str) -> None:
        separator = "─" * 40
        print(f"\n{self._colorize(separator, ColorCodes.BLUE)}")
        print(self._colorize(text, ColorCodes.BLUE + ColorCodes.BOLD))
        print(self._colorize(separator, ColorCodes.BLUE))

    def print_test_start(self, test_name: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(self._colorize(f"\n🚀 [{timestamp}] Starting: {test_name}", ColorCodes.BLUE))

    def print_success(self, message: str) -> None:
        print(f"   {self._colorize(message, ColorCodes.GREEN)}")

    def print_error(self, message: str) -> None:
        print(f"   {self._colorize(message, ColorCodes.RED)}")

    def print_warning(self, message: str) -> None:
        print(f"   {self._colorize(message, ColorCodes.YELLOW)}")

    def print_info(self, message: str) -> None:
        print(f"   {self._colorize(message, ColorCodes.WHITE)}")

    def print_separator(self) -> None:
        print(self._colorize("─" * 60, ColorCodes.BLUE))

    def print_progress(self, current: int, total: int, description: str = "") -> None:
        if total <= 0:
            return
        percentage = (current / total) * 100
        bar_length = 20
        filled = int(bar_length * current // total)
        bar = "█" * filled + "░" * (bar_length - filled)
        progress_text = f"📊 Progress: [{bar}] {percentage:.1f}% ({current}/{total})"
        if description:
            progress_text += f" - {description}"
        print(f"\r   {self._colorize(progress_text, ColorCodes.CYAN)}", end="", flush=True)
        if current >= total:
            print()

    def print_final_summary(self, summary: TestSummary) -> None:
        self.print_header("🎯 FINAL TEST SUMMARY")
        total_tests = summary.total_tests
        success_rate = summary.overall_success_rate * 100
        print(f"📊 {self._colorize('Test Results Overview:', ColorCodes.BOLD)}")
        print(f"   Total Tests: {summary.total_tests}")
        print(f"   ✅ Passed: {self._colorize(str(summary.passed_tests), ColorCodes.GREEN)}")
        print(f"   ❌ Failed: {self._colorize(str(summary.failed_tests), ColorCodes.RED)}")
        print(f"   🚫 Errors: {self._colorize(str(summary.error_tests), ColorCodes.RED)}")
        print(f"   ⏭️  Skipped: {self._colorize(str(summary.skipped_tests), ColorCodes.YELLOW)}")
        color = ColorCodes.GREEN if success_rate >= 80 else (
            ColorCodes.YELLOW if success_rate >= 60 else ColorCodes.RED
        )
        print(
            f"   🎯 Success Rate: {self._colorize(f'{success_rate:.1f}%', color + ColorCodes.BOLD)}"
        )
        duration_minutes = summary.total_duration_seconds / 60
        print(f"\n⏱️  Total Duration: {duration_minutes:.1f} minutes")
        print(f"📦 Total Events Collected: {summary.total_events_collected}")
        if summary.critical_failures:
            self.print_warning("⚠️  Critical Issues Detected:")
            for failure in summary.critical_failures:
                self.print_error(f"   💥 {failure}")
        final_separator = "=" * 60
        print(self._colorize(f"\n{final_separator}", ColorCodes.CYAN))
        if success_rate >= 80:
            message = "🎉 TESTS COMPLETED SUCCESSFULLY"
            final_color = ColorCodes.GREEN + ColorCodes.BOLD
        elif success_rate >= 60:
            message = "⚠️  TESTS COMPLETED WITH WARNINGS"
            final_color = ColorCodes.YELLOW + ColorCodes.BOLD
        else:
            message = "❌ TESTS COMPLETED WITH FAILURES"
            final_color = ColorCodes.RED + ColorCodes.BOLD
        print(self._colorize(message.center(60), final_color))
        print(self._colorize(final_separator, ColorCodes.CYAN))

    def print_db_summary(self, stats: Dict[str, Any]) -> None:
        """Prints the database summary statistics."""
        self.print_header("💾 DATABASE SUMMARY")
        if not stats:
            self.print_warning("Statistics could not be generated.")
            return

        self.print_info(f"Total Unique Events: {stats.get('unique_event_count', 0)}")
        self.print_info(f"Total Unique Entities: {stats.get('unique_entity_count', 0)}")

        if stats.get("events_by_category"):
            self.print_subheader("Events by Category")
            for category, count in sorted(stats["events_by_category"].items()):
                self.print_info(f"  - {category}: {count}")

        if stats.get("events_by_industry"):
            self.print_subheader("Events by Entity Industry")
            for industry, count in sorted(stats["events_by_industry"].items()):
                self.print_info(f"  - {industry}: {count}")
        
        if stats.get("events_by_source"):
            self.print_subheader("Events by Data Source")
            for source, count in sorted(stats["events_by_source"].items()):
                self.print_info(f"  - {source}: {count}")


# --------------------------------------------------------------------------- 
# Utility helpers
# --------------------------------------------------------------------------- 


class TestTimer:
    def __init__(self) -> None:
        self.start_time: Optional[float] = None

    def start(self) -> None:
        self.start_time = time.time()

    def stop(self) -> float:
        if self.start_time is None:
            return 0.0
        return time.time() - self.start_time


class ErrorCollector:
    def __init__(self) -> None:
        self.errors: List[Dict[str, Any]] = []

    def add_error(self, error_type: str, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        self.errors.append({
            "type": error_type,
            "message": message,
            "details": details or {},
            "timestamp": datetime.now(),
        })

    def has_critical_errors(self) -> bool:
        return any(error["type"] == "CRITICAL" for error in self.errors)


class StatisticsCalculator:
    @staticmethod
    def calculate_success_rate(results: Sequence[TestResult]) -> float:
        if not results:
            return 0.0
        passed = sum(1 for result in results if result.status == TestStatus.PASSED)
        return passed / len(results)

    @staticmethod
    def calculate_average_duration(results: Sequence[TestResult]) -> float:
        durations = [result.duration_seconds for result in results if result.duration_seconds]
        if not durations:
            return 0.0
        return sum(durations) / len(durations)


# --------------------------------------------------------------------------- 
# Base test runner
# --------------------------------------------------------------------------- 


class BaseTestRunner:
    """Abstract base class encapsulating shared testing behaviour."""

    name: str = "BaseTestRunner"

    def __init__(self, config: DataSourceTestConfig, test_config: TestConfig):
        self.config = config
        self.test_config = test_config
        self.reporter = ConsoleReporter(test_config.colored_output)
        self.error_collector = ErrorCollector()
        self.timer = TestTimer()
        self.last_collected_events: List[CyberEvent] = []

    async def run_all_tests(self) -> List[TestResult]:
        if not self.config.enabled:
            self.reporter.print_warning(f"❌ {self.name} tests are disabled")
            return []

        self.reporter.print_header(f"🚀 Starting {self.name} Tests")
        self.reporter.print_info(
            f"📅 Testing period: {self.test_config.test_start_date.date()} to {self.test_config.test_end_date.date()}"
        )

        results: List[TestResult] = []

        connection_result = await self._run_with_monitoring(self.test_connection, "Connection Test")
        results.append(connection_result)
        self._report_result(connection_result)
        if connection_result.status in {TestStatus.FAILED, TestStatus.ERROR} and self.test_config.fail_fast:
            self.reporter.print_error("❌ Connection test failed, skipping remaining tests")
            self._report_summary(results)
            return results

        collection_result = await self._run_with_monitoring(self.test_data_collection, "Data Collection Test")
        results.append(collection_result)
        self._report_result(collection_result)

        if (
            collection_result.status == TestStatus.PASSED
            and self.config.validate_data_quality
        ):
            quality_result = await self._run_quality_validation(collection_result)
            results.append(quality_result)
            self._report_result(quality_result)

        self._report_summary(results)
        return results

    async def _run_with_monitoring(self, func, label: str) -> TestResult:
        self.reporter.print_test_start(label)
        start_time = datetime.now()
        process = psutil.Process() if psutil else None
        memory_before = process.memory_info().rss / (1024 * 1024) if process else None
        cpu_before = process.cpu_percent() if process else None
        try:
            result: TestResult = await asyncio.wait_for(func(), timeout=self.config.timeout_seconds)
        except asyncio.TimeoutError:
            return TestResult(
                test_name=label,
                data_source=self.name,
                status=TestStatus.FAILED,
                severity=TestSeverity.HIGH,
                start_time=start_time,
                end_time=datetime.now(),
                error_message=f"Test timed out after {self.config.timeout_seconds} seconds",
            )
        except Exception as exc:  # pragma: no cover - defensive
            return TestResult(
                test_name=label,
                data_source=self.name,
                status=TestStatus.ERROR,
                severity=TestSeverity.CRITICAL,
                start_time=start_time,
                end_time=datetime.now(),
                error_message=str(exc),
                error_details={"exception_type": type(exc).__name__},
            )

        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        metrics: Dict[str, Any] = {"response_time_seconds": duration}
        if process and self.config.performance_monitoring:
            memory_after = process.memory_info().rss / (1024 * 1024)
            cpu_after = process.cpu_percent()
            metrics["memory_usage_mb"] = (memory_after - memory_before) if memory_before is not None else None
            metrics["peak_memory_mb"] = memory_after
            metrics["cpu_usage_percent"] = max(cpu_before or 0.0, cpu_after)
        result.start_time = start_time
        result.end_time = end_time
        result.duration_seconds = duration
        if self.config.performance_monitoring:
            result.performance_metrics = metrics
        return result

    async def _run_quality_validation(self, collection_result: TestResult) -> TestResult:
        try:
            metrics = await self.validate_data_quality(self.last_collected_events)
        except Exception as exc:
            return TestResult(
                test_name="Data Quality Validation",
                data_source=self.name,
                status=TestStatus.ERROR,
                severity=TestSeverity.MEDIUM,
                start_time=datetime.now(),
                end_time=datetime.now(),
                error_message=f"Quality validation failed: {exc}",
            )

        passes_quality = (
            metrics.relevance_score >= self.test_config.min_australian_relevance_percentage
            and metrics.duplicate_percentage <= self.test_config.max_duplicate_percentage
        )
        return TestResult(
            test_name="Data Quality Validation",
            data_source=self.name,
            status=TestStatus.PASSED if passes_quality else TestStatus.FAILED,
            severity=TestSeverity.MEDIUM if passes_quality else TestSeverity.HIGH,
            start_time=datetime.now(),
            end_time=datetime.now(),
            data_quality_metrics=metrics.model_dump(),
        )

    def _report_result(self, result: TestResult) -> None:
        if result.status == TestStatus.PASSED:
            self.reporter.print_success(f"✅ {result.test_name} PASSED")
            if result.duration_seconds is not None:
                self.reporter.print_info(f"   ⏱️  Duration: {result.duration_seconds:.2f}s")
            if result.events_collected:
                self.reporter.print_info(
                    f"   📊 Events: {result.events_collected} collected, {result.events_australian} Australian"
                )
        elif result.status == TestStatus.FAILED:
            self.reporter.print_error(f"❌ {result.test_name} FAILED")
            if result.error_message:
                self.reporter.print_error(f"   💥 Error: {result.error_message}")
        elif result.status == TestStatus.ERROR:
            self.reporter.print_error(f"🚫 {result.test_name} ERROR")
            if result.error_message:
                self.reporter.print_error(f"   💥 Error: {result.error_message}")
        if result.performance_metrics and self.config.performance_monitoring:
            metrics = result.performance_metrics
            self.reporter.print_info(
                f"   🚀 Performance: {metrics.get('response_time_seconds', 0):.2f}s, "
                f"{metrics.get('memory_usage_mb', 0) or 0:.1f}MB"
            )

    def _report_summary(self, results: Sequence[TestResult]) -> None:
        self.reporter.print_separator()
        passed = sum(1 for r in results if r.status == TestStatus.PASSED)
        total = len(results)
        self.reporter.print_info(f"📈 {self.name} Summary: {passed}/{total} tests passed")
        total_events = sum(result.events_collected for result in results)
        if total_events:
            total_australian = sum(result.events_australian for result in results)
            relevance_pct = (total_australian / total_events) * 100 if total_events > 0 else 0
            self.reporter.print_info(
                f"📊 Data: {total_events} events, {total_australian} Australian ({relevance_pct:.1f}%)"
            )

    # Abstract methods
    async def test_connection(self) -> TestResult:  # pragma: no cover - overridden
        raise NotImplementedError

    async def test_data_collection(self) -> TestResult:  # pragma: no cover - overridden
        raise NotImplementedError

    async def validate_data_quality(self, events: List[CyberEvent]) -> DataQualityMetrics:  # pragma: no cover - overridden
        raise NotImplementedError


# --------------------------------------------------------------------------- 
# Data source specific test runners
# --------------------------------------------------------------------------- 


class GDELTTestRunner(BaseTestRunner):
    name = "GDELTTestRunner"

    def __init__(self, config: DataSourceTestConfig, test_config: TestConfig):
        super().__init__(config, test_config)
        self.data_source: Optional[GDELTDataSource] = None
        self.rate_limiter = RateLimiter()
        if config.rate_limit_per_minute:
            self.rate_limiter.set_limit("gdelt", per_minute=config.rate_limit_per_minute)
        self.env_config = ConfigManager().load()
        self.bigquery_available = False

    def _build_data_source(self) -> None:
        if self.data_source is not None:
            return
        collector_config = CollectorDataSourceConfig(
            enabled=True,
            custom_config={
                "max_records": 250,
            },
        )
        self.data_source = GDELTDataSource(collector_config, self.rate_limiter, self.env_config)
        initialized = self.data_source.validate_config()
        if not initialized:
            raise RuntimeError("Failed to initialize GDELT BigQuery data source")

    async def test_connection(self) -> TestResult:
        project_id = self.env_config.get("GDELT_PROJECT_ID") or self.env_config.get("GOOGLE_CLOUD_PROJECT")
        credentials_path = self.env_config.get("GOOGLE_APPLICATION_CREDENTIALS")

        if not project_id:
            return TestResult(
                test_name="GDELT Connection Test",
                data_source="GDELT",
                status=TestStatus.SKIPPED,
                severity=TestSeverity.LOW,
                start_time=datetime.now(),
                error_message="GDELT_PROJECT_ID or GOOGLE_CLOUD_PROJECT not configured",
            )

        if bigquery is None:
            return TestResult(
                test_name="GDELT Connection Test",
                data_source="GDELT",
                status=TestStatus.SKIPPED,
                severity=TestSeverity.LOW,
                start_time=datetime.now(),
                error_message="google-cloud-bigquery package not installed",
            )

        token_path = credentials_path or "bigquery_token.json"
        if Credentials is None:
            return TestResult(
                test_name="GDELT Connection Test",
                data_source="GDELT",
                status=TestStatus.SKIPPED,
                severity=TestSeverity.MEDIUM,
                start_time=datetime.now(),
                error_message="google-auth dependencies not installed",
            )

        if not os.path.exists(token_path):
            return TestResult(
                test_name="GDELT Connection Test",
                data_source="GDELT",
                status=TestStatus.ERROR,
                severity=TestSeverity.CRITICAL,
                start_time=datetime.now(),
                error_message=f"BigQuery token file not found at {token_path}",
            )

        try:
            with open(token_path, "r", encoding="utf-8") as file:
                token_data = json.load(file)
            creds = Credentials(
                token=token_data.get("token"),
                refresh_token=token_data.get("refresh_token"),
                token_uri=token_data.get("token_uri"),
                client_id=token_data.get("client_id"),
                client_secret=token_data.get("client_secret"),
            )
        except Exception as exc:
            return TestResult(
                test_name="GDELT Connection Test",
                data_source="GDELT",
                status=TestStatus.ERROR,
                severity=TestSeverity.CRITICAL,
                start_time=datetime.now(),
                error_message=f"Failed to load BigQuery credentials: {exc}",
            )

        try:
            client = bigquery.Client(project=project_id, credentials=creds)
            query = """
                SELECT COUNT(*) AS total_events
                FROM `gdelt-bq.gdeltv2.events`
                WHERE SQLDATE = 20250601
                LIMIT 1
            """
            results = list(client.query(query).result())
            self.bigquery_available = True
        except Exception as exc:
            return TestResult(
                test_name="GDELT Connection Test",
                data_source="GDELT",
                status=TestStatus.ERROR,
                severity=TestSeverity.CRITICAL,
                start_time=datetime.now(),
                error_message=f"BigQuery connection failed: {exc}",
            )

        return TestResult(
            test_name="GDELT Connection Test",
            data_source="GDELT",
            status=TestStatus.PASSED,
            severity=TestSeverity.INFO,
            start_time=datetime.now(),
            events_collected=results[0].total_events if results else 0,
        )

    async def test_data_collection(self) -> TestResult:
        self._build_data_source()
        assert self.data_source is not None
        date_range = DateRange(
            start_date=self.test_config.test_start_date,
            end_date=self.test_config.test_end_date,
        )
        try:
            events = await self.data_source.collect_events(date_range)
        except Exception as exc:
            return TestResult(
                test_name="GDELT Data Collection Test",
                data_source="GDELT",
                status=TestStatus.ERROR,
                severity=TestSeverity.HIGH,
                start_time=datetime.now(),
                error_message=f"GDELT collection failed: {exc}",
            )
        self.last_collected_events = events
        australian_events = sum(1 for event in events if event.australian_relevance)
        meets_minimum = len(events) >= self.config.min_events_expected
        status = TestStatus.PASSED if meets_minimum else TestStatus.FAILED
        severity = TestSeverity.INFO if meets_minimum else TestSeverity.MEDIUM
        warnings = [] if meets_minimum else [
            f"Expected at least {self.config.min_events_expected} events, got {len(events)}"
        ]
        return TestResult(
            test_name="GDELT Data Collection Test",
            data_source="GDELT",
            status=status,
            severity=severity,
            start_time=datetime.now(),
            events_collected=len(events),
            events_valid=len(events),
            events_australian=australian_events,
            warnings=warnings,
        )

    async def validate_data_quality(self, events: List[CyberEvent]) -> DataQualityMetrics:
        if not events:
            return DataQualityMetrics(
                completeness_score=0.0,
                accuracy_score=0.0,
                consistency_score=0.0,
                relevance_score=0.0,
                duplicate_percentage=0.0,
            )
        complete = sum(1 for event in events if event.title and event.event_type and event.event_date)
        australian = sum(1 for event in events if event.australian_relevance)
        duplicates = len(events) - len({event.event_id for event in events})
        average_confidence = (
            sum(event.confidence.overall for event in events if event.confidence) / len(events)
        ) if any(event.confidence for event in events) else 0.0
        return DataQualityMetrics(
            completeness_score=complete / len(events) if events else 0.0,
            accuracy_score=average_confidence,
            consistency_score=0.85,
            relevance_score=australian / len(events) if events else 0.0,
            duplicate_percentage=duplicates / len(events) if events else 0.0,
        )


class PerplexityTestRunner(BaseTestRunner):
    name = "PerplexityTestRunner"

    def __init__(self, config: DataSourceTestConfig, test_config: TestConfig):
        super().__init__(config, test_config)
        self.data_source: Optional[PerplexityDataSource] = None
        self.rate_limiter = RateLimiter()
        if config.rate_limit_per_minute:
            self.rate_limiter.set_limit("perplexity", per_minute=config.rate_limit_per_minute)

    def _build_data_source(self) -> Optional[PerplexityDataSource]:
        if self.data_source is not None:
            return self.data_source
        env_config = ConfigManager().load()
        if not env_config.get("PERPLEXITY_API_KEY"):
            return None
        collector_config = CollectorDataSourceConfig(enabled=True)
        try:
            self.data_source = PerplexityDataSource(collector_config, self.rate_limiter, env_config)
        except Exception:
            self.data_source = None
        return self.data_source

    async def test_connection(self) -> TestResult:
        data_source = self._build_data_source()
        if data_source is None or not data_source.validate_config():
            return TestResult(
                test_name="Perplexity Connection Test",
                data_source="Perplexity",
                status=TestStatus.SKIPPED,
                severity=TestSeverity.LOW,
                start_time=datetime.now(),
                error_message="Perplexity configuration not available",
            )
        try:
            # Make a lightweight request
            response = await data_source._search("Australian cyber security", DateRange(
                start_date=self.test_config.test_start_date,
                end_date=self.test_config.test_end_date,
            ))
            dummy = len(response.events)
        except Exception as exc:
            return TestResult(
                test_name="Perplexity Connection Test",
                data_source="Perplexity",
                status=TestStatus.ERROR,
                severity=TestSeverity.CRITICAL,
                start_time=datetime.now(),
                error_message=f"Perplexity API call failed: {exc}",
            )
        return TestResult(
            test_name="Perplexity Connection Test",
            data_source="Perplexity",
            status=TestStatus.PASSED,
            severity=TestSeverity.INFO,
            start_time=datetime.now(),
            events_collected=dummy,
        )

    async def test_data_collection(self) -> TestResult:
        data_source = self._build_data_source()
        if data_source is None:
            return TestResult(
                test_name="Perplexity Data Collection Test",
                data_source="Perplexity",
                status=TestStatus.SKIPPED,
                severity=TestSeverity.LOW,
                start_time=datetime.now(),
                error_message="Perplexity API unavailable",
            )
        date_range = DateRange(
            start_date=self.test_config.test_start_date,
            end_date=self.test_config.test_end_date,
        )
        try:
            events = await data_source.collect_events(date_range)
        except Exception as exc:
            return TestResult(
                test_name="Perplexity Data Collection Test",
                data_source="Perplexity",
                status=TestStatus.ERROR,
                severity=TestSeverity.HIGH,
                start_time=datetime.now(),
                error_message=f"Perplexity data collection failed: {exc}",
            )
        self.last_collected_events = events
        australian_events = sum(1 for event in events if event.australian_relevance)
        meets_minimum = len(events) >= self.config.min_events_expected
        status = TestStatus.PASSED if meets_minimum else TestStatus.FAILED
        severity = TestSeverity.INFO if meets_minimum else TestSeverity.MEDIUM
        warnings = [] if meets_minimum else [
            f"Expected at least {self.config.min_events_expected} events, got {len(events)}"
        ]
        return TestResult(
            test_name="Perplexity Data Collection Test",
            data_source="Perplexity",
            status=status,
            severity=severity,
            start_time=datetime.now(),
            events_collected=len(events),
            events_valid=len(events),
            events_australian=australian_events,
            warnings=warnings,
        )

    async def validate_data_quality(self, events: List[CyberEvent]) -> DataQualityMetrics:
        if not events:
            return DataQualityMetrics(
                completeness_score=0.0,
                accuracy_score=0.0,
                consistency_score=0.0,
                relevance_score=0.0,
                duplicate_percentage=0.0,
            )
        complete = sum(1 for event in events if event.title and event.description)
        australian = sum(1 for event in events if event.australian_relevance)
        duplicates = len(events) - len({event.title for event in events})
        average_confidence = (
            sum(event.confidence.overall for event in events if event.confidence) / len(events)
        ) if any(event.confidence for event in events) else 0.6
        return DataQualityMetrics(
            completeness_score=complete / len(events) if events else 0.0,
            accuracy_score=average_confidence,
            consistency_score=0.75,
            relevance_score=australian / len(events) if events else 0.0,
            duplicate_percentage=duplicates / len(events) if events else 0.0,
        )


class GoogleSearchTestRunner(BaseTestRunner):
    name = "GoogleSearchTestRunner"

    def __init__(self, config: DataSourceTestConfig, test_config: TestConfig):
        super().__init__(config, test_config)
        self.data_source: Optional[GoogleSearchDataSource] = None
        self.rate_limiter = RateLimiter()
        if config.rate_limit_per_minute:
            self.rate_limiter.set_limit("google_search", per_minute=config.rate_limit_per_minute)

    def _build_data_source(self) -> Optional[GoogleSearchDataSource]:
        if self.data_source is not None:
            return self.data_source
        env_config = ConfigManager().load()
        if not (env_config.get("GOOGLE_CUSTOMSEARCH_API_KEY") and env_config.get("GOOGLE_CUSTOMSEARCH_CX_KEY")):
            return None
        collector_config = CollectorDataSourceConfig(enabled=True)
        self.data_source = GoogleSearchDataSource(collector_config, self.rate_limiter, env_config)
        return self.data_source

    async def test_connection(self) -> TestResult:
        data_source = self._build_data_source()
        if data_source is None or not data_source.validate_config():
            return TestResult(
                test_name="Google Search Connection Test",
                data_source="GoogleSearch",
                status=TestStatus.SKIPPED,
                severity=TestSeverity.LOW,
                start_time=datetime.now(),
                error_message="Google Custom Search configuration missing",
            )
        params = {
            "key": data_source.api_key,
            "cx": data_source.cx_key,
            "q": "Australian cyber security",
            "num": 1,
        }
        try:
            response = requests.get("https://www.googleapis.com/customsearch/v1", params=params, timeout=15)
            response.raise_for_status()
        except Exception as exc:
            return TestResult(
                test_name="Google Search Connection Test",
                data_source="GoogleSearch",
                status=TestStatus.ERROR,
                severity=TestSeverity.CRITICAL,
                start_time=datetime.now(),
                error_message=f"Google API request failed: {exc}",
            )
        return TestResult(
            test_name="Google Search Connection Test",
            data_source="GoogleSearch",
            status=TestStatus.PASSED,
            severity=TestSeverity.INFO,
            start_time=datetime.now(),
        )

    async def test_data_collection(self) -> TestResult:
        data_source = self._build_data_source()
        if data_source is None:
            return TestResult(
                test_name="Google Search Data Collection Test",
                data_source="GoogleSearch",
                status=TestStatus.SKIPPED,
                severity=TestSeverity.LOW,
                start_time=datetime.now(),
                error_message="Google Custom Search configuration missing",
            )
        date_range = DateRange(
            start_date=self.test_config.test_start_date,
            end_date=self.test_config.test_end_date,
        )
        try:
            events = await data_source.collect_events(date_range)
        except Exception as exc:
            return TestResult(
                test_name="Google Search Data Collection Test",
                data_source="GoogleSearch",
                status=TestStatus.ERROR,
                severity=TestSeverity.HIGH,
                start_time=datetime.now(),
                error_message=f"Google Search data collection failed: {exc}",
            )
        self.last_collected_events = events
        australian_events = sum(1 for event in events if event.australian_relevance)
        meets_minimum = len(events) >= self.config.min_events_expected
        status = TestStatus.PASSED if meets_minimum else TestStatus.FAILED
        severity = TestSeverity.INFO if meets_minimum else TestSeverity.MEDIUM
        warnings = [] if meets_minimum else [
            f"Expected at least {self.config.min_events_expected} events, got {len(events)}"
        ]
        return TestResult(
            test_name="Google Search Data Collection Test",
            data_source="GoogleSearch",
            status=status,
            severity=severity,
            start_time=datetime.now(),
            events_collected=len(events),
            events_valid=len(events),
            events_australian=australian_events,
            warnings=warnings,
        )

    async def validate_data_quality(self, events: List[CyberEvent]) -> DataQualityMetrics:
        if not events:
            return DataQualityMetrics(
                completeness_score=0.0,
                accuracy_score=0.0,
                consistency_score=0.0,
                relevance_score=0.0,
                duplicate_percentage=0.0,
            )
        complete = sum(1 for event in events if event.title and event.data_sources)
        australian = sum(1 for event in events if event.australian_relevance)
        duplicates = len(events) - len({event.title for event in events})
        average_confidence = (
            sum(event.confidence.overall for event in events if event.confidence) / len(events)
        ) if any(event.confidence for event in events) else 0.5
        return DataQualityMetrics(
            completeness_score=complete / len(events) if events else 0.0,
            accuracy_score=average_confidence,
            consistency_score=0.7,
            relevance_score=australian / len(events) if events else 0.0,
            duplicate_percentage=duplicates / len(events) if events else 0.0,
        )


class WebberInsuranceTestRunner(BaseTestRunner):
    name = "WebberInsuranceTestRunner"

    def __init__(self, config: DataSourceTestConfig, test_config: TestConfig):
        super().__init__(config, test_config)
        self.data_source = WebberInsuranceDataSource(
            CollectorDataSourceConfig(enabled=True),
            RateLimiter(),
            {},
        )

    async def test_connection(self) -> TestResult:
        try:
            response = requests.get(self.data_source.base_url, timeout=20)
            response.raise_for_status()
        except Exception as exc:
            return TestResult(
                test_name="Webber Insurance Connection Test",
                data_source="WebberInsurance",
                status=TestStatus.ERROR,
                severity=TestSeverity.CRITICAL,
                start_time=datetime.now(),
                error_message=f"Webber Insurance request failed: {exc}",
            )
        return TestResult(
            test_name="Webber Insurance Connection Test",
            data_source="WebberInsurance",
            status=TestStatus.PASSED,
            severity=TestSeverity.INFO,
            start_time=datetime.now(),
        )

    async def test_data_collection(self) -> TestResult:
        date_range = DateRange(
            start_date=self.test_config.test_start_date,
            end_date=self.test_config.test_end_date,
        )
        try:
            events = await self.data_source.collect_events(date_range)
        except Exception as exc:
            return TestResult(
                test_name="Webber Insurance Data Collection Test",
                data_source="WebberInsurance",
                status=TestStatus.ERROR,
                severity=TestSeverity.HIGH,
                start_time=datetime.now(),
                error_message=f"Webber Insurance data collection failed: {exc}",
            )
        self.last_collected_events = events
        australian_events = sum(1 for event in events if event.australian_relevance)
        meets_minimum = len(events) >= self.config.min_events_expected
        status = TestStatus.PASSED if meets_minimum else TestStatus.FAILED
        severity = TestSeverity.INFO if meets_minimum else TestSeverity.MEDIUM
        warnings = [] if meets_minimum else [
            "Webber Insurance archives may not yet include future dates",
        ]
        return TestResult(
            test_name="Webber Insurance Data Collection Test",
            data_source="WebberInsurance",
            status=status,
            severity=severity,
            start_time=datetime.now(),
            events_collected=len(events),
            events_valid=len(events),
            events_australian=australian_events,
            warnings=warnings,
        )

    async def validate_data_quality(self, events: List[CyberEvent]) -> DataQualityMetrics:
        if not events:
            return DataQualityMetrics(
                completeness_score=0.0,
                accuracy_score=0.0,
                consistency_score=0.0,
                relevance_score=1.0,
                duplicate_percentage=0.0,
            )
        complete = sum(1 for event in events if event.primary_entity and event.title)
        duplicates = len(events) - len({event.title for event in events})
        average_confidence = (
            sum(event.confidence.overall for event in events if event.confidence) / len(events)
        ) if any(event.confidence for event in events) else 0.85
        return DataQualityMetrics(
            completeness_score=complete / len(events) if events else 0.0,
            accuracy_score=average_confidence,
            consistency_score=0.85,
            relevance_score=1.0,
            duplicate_percentage=duplicates / len(events) if events else 0.0,
        )


# --------------------------------------------------------------------------- 
# Test orchestrator
# --------------------------------------------------------------------------- 


class TestOrchestrator:
    """Main orchestrator coordinating all data source tests."""

    def __init__(self, config: TestConfig) -> None:
        self.config = config
        self.reporter = ConsoleReporter(config.colored_output)
        self.test_runners: List[BaseTestRunner] = []
        self.all_results: List[TestResult] = []
        self.all_collected_events: List[CyberEvent] = []

    def initialize_test_runners(self) -> None:
        if self.config.gdelt_config.enabled:
            self.test_runners.append(GDELTTestRunner(self.config.gdelt_config, self.config))
        if self.config.perplexity_config.enabled:
            self.test_runners.append(PerplexityTestRunner(self.config.perplexity_config, self.config))
        if self.config.google_search_config.enabled:
            self.test_runners.append(GoogleSearchTestRunner(self.config.google_search_config, self.config))
        if self.config.webber_config.enabled:
            self.test_runners.append(WebberInsuranceTestRunner(self.config.webber_config, self.config))

    async def run_all_tests(self) -> TestSummary:
        self.reporter.print_header("🔍 AUSTRALIAN CYBER EVENTS DATA SOURCE TESTS")
        self.reporter.print_info(
            f"🗓️  Test Period: {self.config.test_start_date.date()} to {self.config.test_end_date.date()}"
        )
        self.reporter.print_info(f"🔧 Data Sources Enabled: {len(self.test_runners)}")
        start = time.time()
        if self.config.parallel_testing:
            await self._run_tests_parallel()
        else:
            await self._run_tests_sequential()
        duration = time.time() - start
        summary = self._generate_summary(duration)
        self.reporter.print_final_summary(summary)
        return summary

    async def _run_tests_parallel(self) -> None:
        tasks = [asyncio.create_task(runner.run_all_tests()) for runner in self.test_runners]
        results_lists = await asyncio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(results_lists):
            if isinstance(result, Exception):
                self.reporter.print_error(f"Test runner failed: {result}")
            else:
                self.all_results.extend(result)
                self.all_collected_events.extend(self.test_runners[i].last_collected_events)

    async def _run_tests_sequential(self) -> None:
        total = len(self.test_runners)
        for index, runner in enumerate(self.test_runners, start=1):
            if self.config.show_progress_indicators:
                self.reporter.print_progress(index - 1, total, f"Starting {runner.name}")
            try:
                results = await runner.run_all_tests()
                self.all_results.extend(results)
                self.all_collected_events.extend(runner.last_collected_events)
            except Exception as exc:
                self.reporter.print_error(f"Test runner {runner.name} failed: {exc}")
            if self.config.show_progress_indicators:
                self.reporter.print_progress(index, total, f"Completed {runner.name}")

    def _generate_summary(self, duration: float) -> TestSummary:
        total_tests = len(self.all_results)
        passed = sum(1 for result in self.all_results if result.status == TestStatus.PASSED)
        failed = sum(1 for result in self.all_results if result.status == TestStatus.FAILED)
        errors = sum(1 for result in self.all_results if result.status == TestStatus.ERROR)
        skipped = sum(1 for result in self.all_results if result.status == TestStatus.SKIPPED)
        total_events = sum(result.events_collected for result in self.all_results)
        success_rate = passed / total_tests if total_tests else 0.0
        critical_failures = [
            f"{result.data_source}: {result.error_message}"
            for result in self.all_results
            if result.severity == TestSeverity.CRITICAL and result.error_message
        ]
        return TestSummary(
            total_tests=total_tests,
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
            error_tests=errors,
            total_duration_seconds=duration,
            total_events_collected=total_events,
            overall_success_rate=success_rate,
            critical_failures=critical_failures,
        )


# --------------------------------------------------------------------------- 
# Configuration and CLI helpers
# --------------------------------------------------------------------------- 


def load_test_config(config_path: Optional[str]) -> TestConfig:
    if config_path and Path(config_path).exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return TestConfig(**data)
        except Exception as exc:
            print(f"⚠️  Failed to load config from {config_path}: {exc}")
            print("🔄 Using default configuration")
    return TestConfig(
        gdelt_config=DataSourceTestConfig(
            enabled=True,
            timeout_seconds=300,
            max_events_expected=500,
            min_events_expected=5,
            retry_attempts=3,
            rate_limit_per_minute=60,
        ),
        perplexity_config=DataSourceTestConfig(
            enabled=True,
            timeout_seconds=240,
            max_events_expected=200,
            min_events_expected=2,
            retry_attempts=2,
            rate_limit_per_minute=40,
        ),
        google_search_config=DataSourceTestConfig(
            enabled=True,
            timeout_seconds=180,
            max_events_expected=300,
            min_events_expected=3,
            retry_attempts=3,
            rate_limit_per_minute=100,
        ),
        webber_config=DataSourceTestConfig(
            enabled=True,
            timeout_seconds=120,
            max_events_expected=50,
            min_events_expected=0,
            retry_attempts=2,
        ),
    )


def check_environment_variables(required: Iterable[str]) -> bool:
    missing = [var for var in required if not os.getenv(var)]
    if missing:
        reporter = ConsoleReporter()
        reporter.print_error("❌ Missing required environment variables:")
        for var in missing:
            reporter.print_error(f"   - {var}")
        reporter.print_info("💡 Please update your environment configuration before retrying")
        return False
    return True


def print_test_configuration(config: TestConfig) -> None:
    reporter = ConsoleReporter(config.colored_output)
    reporter.print_subheader("🔧 Test Configuration")
    reporter.print_info(f"📅 Test Period: {config.test_start_date.date()} to {config.test_end_date.date()}")
    reporter.print_info(f"🔀 Parallel Testing: {'Enabled' if config.parallel_testing else 'Disabled'}")
    reporter.print_info(f"🎯 Fail Fast: {'Enabled' if config.fail_fast else 'Disabled'}")
    reporter.print_info(f"🎨 Colored Output: {'Enabled' if config.colored_output else 'Disabled'}")
    enabled_sources = []
    if config.gdelt_config.enabled:
        enabled_sources.append("GDELT")
    if config.perplexity_config.enabled:
        enabled_sources.append("Perplexity")
    if config.google_search_config.enabled:
        enabled_sources.append("Google Search")
    if config.webber_config.enabled:
        enabled_sources.append("Webber Insurance")
    reporter.print_info(f"🔗 Data Sources: {', '.join(enabled_sources) or 'None'}")


# --------------------------------------------------------------------------- 
# Entry point
# --------------------------------------------------------------------------- 


async def main() -> None:
    parser = argparse.ArgumentParser(description="Test Australian cyber events data sources for June 2025")
    parser.add_argument("--config", type=str, help="Path to test configuration JSON file")
    parser.add_argument("--env", type=str, default=".env", help="Path to environment variables file")
    parser.add_argument("--no-colors", action="store_true", help="Disable colored output")
    parser.add_argument("--sequential", action="store_true", help="Run tests sequentially")
    parser.add_argument("--fail-fast", action="store_true", help="Enable fail-fast mode")
    args = parser.parse_args()

    if Path(args.env).exists():
        load_dotenv(args.env)
        print(f"✅ Loaded environment variables from {args.env}")
    else:
        print(f"⚠️  Environment file {args.env} not found; using system environment")

    config = load_test_config(args.config)
    if args.no_colors:
        config.colored_output = False
    if args.sequential:
        config.parallel_testing = False
    if args.fail_fast:
        config.fail_fast = True

    required_env = [
        "GDELT_PROJECT_ID",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "PERPLEXITY_API_KEY",
        "GOOGLE_CUSTOMSEARCH_API_KEY",
        "GOOGLE_CUSTOMSEARCH_CX_KEY",
        "OPENAI_API_KEY",
    ]
    if not os.getenv("GDELT_PROJECT_ID") and not os.getenv("GOOGLE_CLOUD_PROJECT"):
        required_env.append("GOOGLE_CLOUD_PROJECT")
    check_environment_variables(required_env)

    print_test_configuration(config)

    orchestrator = TestOrchestrator(config)
    orchestrator.initialize_test_runners()
    if not orchestrator.test_runners:
        reporter = ConsoleReporter(config.colored_output)
        reporter.print_error("❌ No data sources enabled for testing")
        sys.exit(1)

    summary = await orchestrator.run_all_tests()

    # --- NEW DATABASE INTEGRATION ---
    reporter = ConsoleReporter(config.colored_output)
    try:
        reporter.print_header("💾 SAVING EVENTS TO DATABASE")
        db = CyberEventData()
        
        total_events_to_save = len(orchestrator.all_collected_events)
        reporter.print_info(f"Found {total_events_to_save} events to process.")

        for i, event in enumerate(orchestrator.all_collected_events):
            try:
                db.add_event(event.model_dump())
                if config.show_progress_indicators:
                    reporter.print_progress(i + 1, total_events_to_save, "Saving events")
            except Exception as e:
                reporter.print_warning(f"Could not save event {getattr(event, 'title', 'N/A')}: {e}")
        
        if config.show_progress_indicators and total_events_to_save > 0:
            print() # for the progress bar

        stats = db.get_summary_statistics()
        reporter.print_db_summary(stats)
        
        db.close()

    except Exception as e:
        reporter.print_error(f"❌ Database operations failed: {e}")
    # --- END OF NEW SECTION ---

    if summary.overall_success_rate >= 0.8:
        sys.exit(0)
    if summary.critical_failures:
        sys.exit(2)
    sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        reporter = ConsoleReporter()
        reporter.print_warning("\n⚠️  Test execution interrupted by user")
        sys.exit(130)
    except Exception as exc:  # pragma: no cover - top level safety
        reporter = ConsoleReporter()
        reporter.print_error(f"❌ Test execution failed: {exc}")
        sys.exit(1)