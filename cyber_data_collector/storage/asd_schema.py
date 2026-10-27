"""Schema of ``ASDRiskClassifications`` beyond the base migration.

The base schema allows only ``C1``-``C6`` as a severity. Prompt ``asd-v2``
answers ``Insufficient information`` when the evidence cannot decide one, so an
older table refuses every such answer (CHECK constraint failed) and the
classifier fails the event. SQLite cannot alter a CHECK in place, so the table
is rebuilt with the wider constraint, keeping every column and row. Run by
``pipeline.py init`` and by the classifier itself; idempotent.
"""

from __future__ import annotations

import sqlite3

INSUFFICIENT_INFORMATION = "Insufficient information"

_TABLE_SQL = """
CREATE TABLE {name} (
    classification_id TEXT PRIMARY KEY,
    deduplicated_event_id TEXT UNIQUE NOT NULL,
    severity_category VARCHAR(30) NOT NULL CHECK(severity_category IN
        ('C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'Insufficient information')),
    primary_stakeholder_category VARCHAR(255) NOT NULL,
    impact_type VARCHAR(100) NOT NULL,
    reasoning_json TEXT NOT NULL,
    confidence_score REAL NOT NULL CHECK(confidence_score >= 0.0 AND confidence_score <= 1.0),
    model_used VARCHAR(50) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    input_fingerprint TEXT,
    FOREIGN KEY (deduplicated_event_id) REFERENCES DeduplicatedEvents(deduplicated_event_id) ON DELETE CASCADE
)"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_asd_risk_dedup_event_id ON ASDRiskClassifications(deduplicated_event_id)",
    "CREATE INDEX IF NOT EXISTS idx_asd_risk_severity ON ASDRiskClassifications(severity_category)",
    "CREATE INDEX IF NOT EXISTS idx_asd_risk_stakeholder ON ASDRiskClassifications(primary_stakeholder_category)",
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Give ``ASDRiskClassifications`` the ``input_fingerprint`` column and a
    severity CHECK that accepts ``Insufficient information``.

    Does nothing when the table is missing (the base migration creates it) or
    already current. The rebuild runs in one transaction; a failure leaves the
    old table as it was.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ASDRiskClassifications'"
    ).fetchone()
    if row is None:
        return
    columns = [r[1] for r in conn.execute("PRAGMA table_info(ASDRiskClassifications)")]
    if INSUFFICIENT_INFORMATION in (row[0] or ""):
        if "input_fingerprint" not in columns:
            conn.execute("ALTER TABLE ASDRiskClassifications ADD COLUMN input_fingerprint TEXT")
            conn.commit()
        return

    conn.commit()
    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")  # no effect inside a transaction, so set first
    try:
        conn.execute("BEGIN")
        conn.execute(_TABLE_SQL.format(name="ASDRiskClassifications_new"))
        keep = [c for c in columns if c in _new_columns()]
        listed = ", ".join(keep)
        conn.execute(f"INSERT INTO ASDRiskClassifications_new ({listed}) "
                     f"SELECT {listed} FROM ASDRiskClassifications")
        conn.execute("DROP TABLE ASDRiskClassifications")
        conn.execute("ALTER TABLE ASDRiskClassifications_new RENAME TO ASDRiskClassifications")
        for statement in _INDEXES:
            conn.execute(statement)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")


def _new_columns() -> set:
    probe = sqlite3.connect(":memory:")
    try:
        probe.execute(_TABLE_SQL.format(name="probe"))
        return {r[1] for r in probe.execute("PRAGMA table_info(probe)")}
    finally:
        probe.close()
