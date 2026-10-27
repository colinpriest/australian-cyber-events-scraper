"""ASDRiskClassifications accepts "Insufficient information" after the schema step."""

from __future__ import annotations

import sqlite3

import pytest

from cyber_data_collector.storage import asd_schema

OLD_TABLE = """
CREATE TABLE ASDRiskClassifications (
    classification_id TEXT PRIMARY KEY,
    deduplicated_event_id TEXT UNIQUE NOT NULL,
    severity_category VARCHAR(2) NOT NULL CHECK(severity_category IN ('C1', 'C2', 'C3', 'C4', 'C5', 'C6')),
    primary_stakeholder_category VARCHAR(255) NOT NULL,
    impact_type VARCHAR(100) NOT NULL,
    reasoning_json TEXT NOT NULL,
    confidence_score REAL NOT NULL CHECK(confidence_score >= 0.0 AND confidence_score <= 1.0),
    model_used VARCHAR(50) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)"""


def _insert(conn, cid, severity):
    conn.execute(
        "INSERT INTO ASDRiskClassifications (classification_id, deduplicated_event_id, severity_category,"
        " primary_stakeholder_category, impact_type, reasoning_json, confidence_score, model_used)"
        " VALUES (?, ?, ?, 'Member(s) of the public', 'Low', '{}', 0.8, 'm')", (cid, "ev-" + cid, severity))


@pytest.mark.parametrize("with_fingerprint", [False, True])
def test_old_table_is_rebuilt_keeping_rows(with_fingerprint):
    conn = sqlite3.connect(":memory:")
    conn.execute(OLD_TABLE)
    if with_fingerprint:
        conn.execute("ALTER TABLE ASDRiskClassifications ADD COLUMN input_fingerprint TEXT")
    _insert(conn, "a", "C4")
    if with_fingerprint:
        conn.execute("UPDATE ASDRiskClassifications SET input_fingerprint = 'fp'")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, "b", "Insufficient information")
    conn.rollback()

    asd_schema.ensure_schema(conn)

    _insert(conn, "b", "Insufficient information")
    rows = conn.execute("SELECT classification_id, severity_category, input_fingerprint"
                        " FROM ASDRiskClassifications ORDER BY 1").fetchall()
    assert rows == [("a", "C4", "fp" if with_fingerprint else None), ("b", "Insufficient information", None)]
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, "c", "C7")
    indexes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='ASDRiskClassifications'"
        " AND name LIKE 'idx_%'")}
    assert indexes == {"idx_asd_risk_dedup_event_id", "idx_asd_risk_severity", "idx_asd_risk_stakeholder"}


def test_idempotent_and_missing_table_is_left_alone():
    conn = sqlite3.connect(":memory:")
    asd_schema.ensure_schema(conn)  # no table: nothing to do
    assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []
    conn.execute(OLD_TABLE)
    asd_schema.ensure_schema(conn)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='ASDRiskClassifications'").fetchone()[0]
    asd_schema.ensure_schema(conn)
    assert conn.execute("SELECT sql FROM sqlite_master WHERE name='ASDRiskClassifications'").fetchone()[0] == sql
