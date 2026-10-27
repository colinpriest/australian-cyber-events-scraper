"""Continuous data-quality checks and the publication gate.

    from cyber_data_collector.quality import publication_allowed
    allowed, reasons = publication_allowed(conn)

See ``scripts/quality_report.py`` and ``docs/10-quality-gate.md``.
"""

from cyber_data_collector.quality.gate import (
    QualityReport,
    QualityStoreError,
    publication_allowed,
    run_quality_checks,
    store_report,
    write_outputs,
)
from cyber_data_collector.quality.model import load_thresholds

__all__ = [
    "QualityReport",
    "QualityStoreError",
    "load_thresholds",
    "publication_allowed",
    "run_quality_checks",
    "store_report",
    "write_outputs",
]
