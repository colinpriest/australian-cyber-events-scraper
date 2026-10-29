"""Retained database snapshots and all-or-nothing publication of every output built from one.

    from cyber_data_collector.publication import publish
    result = publish(db_path, Path("dashboard"), Path("risk_matrix"))

See ``snapshot.py`` (identity, retention, reproduction) and ``outputs.py``
(gate, build, stamp, install) and ``docs/08-dashboard-generation.md``.
"""

from cyber_data_collector.publication.outputs import (
    PublicationBlocked,
    PublicationError,
    PublicationResult,
    publish,
)
from cyber_data_collector.publication.snapshot import AnalysisInput, SnapshotError, SnapshotStore, store_for

__all__ = ["AnalysisInput", "PublicationBlocked", "PublicationError", "PublicationResult", "SnapshotError",
           "SnapshotStore", "publish", "store_for"]
