"""Tests for the safety net: SafetyVault snapshot/restore around destructive
operations, and the integrity checks scripts/check_data_integrity.py runs.

The integrity-check tests build small in-memory test DBs that exercise each
failure mode the checker is designed to catch (truncated severities, qualified
event_type names, ASD orphans, etc.) and assert the checker fails on them.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from cyber_data_collector.utils.safety_vault import SafetyVault


# --------------------------------------------------------------------------
# Helpers: create a minimal test DB matching the project schema
# --------------------------------------------------------------------------

DEDUP_DDL = """
CREATE TABLE DeduplicatedEvents (
    deduplicated_event_id      TEXT PRIMARY KEY,
    master_enriched_event_id   TEXT NOT NULL,
    title                      VARCHAR(255) NOT NULL,
    description                TEXT,
    summary                    TEXT,
    event_type                 VARCHAR(50),
    severity                   VARCHAR(20),
    event_date                 DATE,
    records_affected           BIGINT,
    is_australian_event        BOOLEAN NOT NULL,
    is_specific_event          BOOLEAN NOT NULL,
    confidence_score           REAL,
    australian_relevance_score REAL,
    status                     VARCHAR(20) DEFAULT 'Active',
    created_at                 TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at                 TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    victim_organization_name   VARCHAR(255),
    victim_organization_industry VARCHAR(100)
);
"""

ENRICHED_DDL = """
CREATE TABLE EnrichedEvents (
    enriched_event_id          TEXT PRIMARY KEY,
    raw_event_id               TEXT NOT NULL,
    title                      VARCHAR(255) NOT NULL,
    description                TEXT,
    summary                    TEXT,
    event_type                 VARCHAR(50),
    severity                   VARCHAR(20),
    event_date                 DATE,
    records_affected           BIGINT,
    is_australian_event        BOOLEAN NOT NULL,
    is_specific_event          BOOLEAN NOT NULL,
    confidence_score           REAL,
    status                     VARCHAR(20) DEFAULT 'Active',
    perplexity_enrichment_data TEXT
);
"""

ASD_DDL = """
CREATE TABLE ASDRiskClassifications (
    classification_id            TEXT PRIMARY KEY,
    deduplicated_event_id        TEXT NOT NULL,
    severity_category            VARCHAR(10),
    primary_stakeholder_category VARCHAR(50),
    impact_type                  VARCHAR(100),
    reasoning_json               TEXT,
    confidence_score             REAL,
    model_used                   VARCHAR(50),
    created_at                   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at                   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _make_test_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(DEDUP_DDL + ENRICHED_DDL + ASD_DDL)

    # Enriched events (3 of them)
    conn.executemany(
        "INSERT INTO EnrichedEvents "
        "(enriched_event_id, raw_event_id, title, severity, event_type, "
        " is_australian_event, is_specific_event) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("e1", "r1", "Event 1", "High", "Data Breach", 1, 1),
            ("e2", "r2", "Event 2", "Critical", "Ransomware", 1, 1),
            ("e3", "r3", "Event 3", "Medium", "Phishing", 1, 1),
        ],
    )

    # Dedup events (3 active)
    conn.executemany(
        "INSERT INTO DeduplicatedEvents "
        "(deduplicated_event_id, master_enriched_event_id, title, severity, "
        " event_type, is_australian_event, is_specific_event, "
        " victim_organization_name, victim_organization_industry, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Active')",
        [
            ("d1", "e1", "Event 1", "High",     "Data Breach", 1, 1, "Org A", "Healthcare"),
            ("d2", "e2", "Event 2", "Critical", "Ransomware",  1, 1, "Org B", "Government"),
            ("d3", "e3", "Event 3", "Medium",   "Phishing",    1, 1, "Org C", "Finance"),
        ],
    )

    # ASD classifications (3)
    conn.executemany(
        "INSERT INTO ASDRiskClassifications "
        "(classification_id, deduplicated_event_id, severity_category) "
        "VALUES (?, ?, ?)",
        [("a1", "d1", "C3"), ("a2", "d2", "C1"), ("a3", "d3", "C4")],
    )

    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# SafetyVault tests
# --------------------------------------------------------------------------

class TestSafetyVault:
    def test_snapshot_writes_files_and_pointers(self, tmp_path):
        db = tmp_path / "test.db"
        _make_test_db(db)

        vault = SafetyVault(str(db), vault_dir=tmp_path / "vault")
        result = vault.snapshot(label="t1")

        assert result["asd"] == 3
        assert result["industry_overrides"] == 3
        # Pointer files written
        assert (tmp_path / "vault" / "latest_asd_classifications.json").exists()
        assert (tmp_path / "vault" / "latest_industry_overrides.json").exists()

    def test_restore_after_dedup_rebuild_reconnects_via_master_id(self, tmp_path):
        """The killer-feature test: after a force-dedup rebuilds DeduplicatedEvents
        with NEW dedup IDs, the vault should restore ASD classifications via the
        stable master_enriched_event_id rather than the obsolete dedup ID.
        """
        db = tmp_path / "test.db"
        _make_test_db(db)
        vault = SafetyVault(str(db), vault_dir=tmp_path / "vault")
        vault.snapshot(label="pre_dedup")

        # Simulate a force-dedup: drop+recreate dedup events with NEW IDs.
        # Critically, master_enriched_event_id stays the same.
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM ASDRiskClassifications")
        conn.execute("DELETE FROM DeduplicatedEvents")
        conn.executemany(
            "INSERT INTO DeduplicatedEvents "
            "(deduplicated_event_id, master_enriched_event_id, title, severity, "
            " event_type, is_australian_event, is_specific_event) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("NEW-d1", "e1", "Event 1", "High",     "Data Breach", 1, 1),
                ("NEW-d2", "e2", "Event 2", "Critical", "Ransomware",  1, 1),
                ("NEW-d3", "e3", "Event 3", "Medium",   "Phishing",    1, 1),
            ],
        )
        conn.commit()
        conn.close()

        # Restore should map old ASD rows to new dedup IDs via master_enriched_event_id
        n_asd, n_ind = vault.restore()
        assert n_asd == 3
        assert n_ind == 3

        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT arc.deduplicated_event_id, arc.severity_category "
            "FROM ASDRiskClassifications arc "
            "JOIN DeduplicatedEvents de "
            "  ON arc.deduplicated_event_id = de.deduplicated_event_id "
            "ORDER BY arc.severity_category"
        ).fetchall()
        conn.close()
        assert rows == [("NEW-d2", "C1"), ("NEW-d1", "C3"), ("NEW-d3", "C4")]

    def test_restore_skips_dedup_rows_that_disappeared(self, tmp_path):
        """If a master_enriched_event_id is gone after rebuild (its enriched
        event was removed), the restore should silently skip - not crash."""
        db = tmp_path / "test.db"
        _make_test_db(db)
        vault = SafetyVault(str(db), vault_dir=tmp_path / "vault")
        vault.snapshot()

        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM ASDRiskClassifications")
        conn.execute("DELETE FROM DeduplicatedEvents")
        # Only re-create d1 and d2 - d3's enriched row was 'lost'
        conn.executemany(
            "INSERT INTO DeduplicatedEvents "
            "(deduplicated_event_id, master_enriched_event_id, title, severity, "
            " event_type, is_australian_event, is_specific_event) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("NEW-d1", "e1", "Event 1", "High",     "Data Breach", 1, 1),
                ("NEW-d2", "e2", "Event 2", "Critical", "Ransomware",  1, 1),
            ],
        )
        conn.commit()
        conn.close()

        n_asd, _ = vault.restore()
        assert n_asd == 2  # d3's classification skipped

    def test_restore_keeps_most_severe_when_multiple_old_rows_collapse(self, tmp_path):
        """If multiple OLD ASD records share a master_enriched_event_id - as
        happens when several old dedup records had the same master and
        post-rebuild they all collapse to one new dedup row - the vault must
        keep only the MOST SEVERE classification.
        """
        db = tmp_path / "test.db"
        _make_test_db(db)

        # Set up the unusual but realistic situation: three OLD dedup records
        # all pointing to the SAME master_enriched_event_id (e1). Their ASD
        # severities differ (C3 / C1 / C4); the vault must keep C1.
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE DeduplicatedEvents SET master_enriched_event_id = 'e1' "
            "WHERE deduplicated_event_id IN ('d2', 'd3')"
        )
        conn.commit()
        conn.close()

        vault = SafetyVault(str(db), vault_dir=tmp_path / "vault")
        vault.snapshot()

        # Simulate force-dedup: ONE new dedup row that maps to e1
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM ASDRiskClassifications")
        conn.execute("DELETE FROM DeduplicatedEvents")
        conn.execute(
            "INSERT INTO DeduplicatedEvents "
            "(deduplicated_event_id, master_enriched_event_id, title, severity, "
            " event_type, is_australian_event, is_specific_event) "
            "VALUES ('NEW-d', 'e1', 'merged', 'Critical', 'Data Breach', 1, 1)"
        )
        conn.commit()
        conn.close()

        n_asd, _ = vault.restore()
        conn = sqlite3.connect(db)
        sevs = [r[0] for r in conn.execute(
            "SELECT severity_category FROM ASDRiskClassifications"
        )]
        conn.close()
        assert n_asd == 1
        assert sevs == ["C1"], f"Should keep most severe (C1) on collision; got {sevs}"


# --------------------------------------------------------------------------
# Integrity-checker tests - exercise each detector with a known-bad fixture
# --------------------------------------------------------------------------

INTEGRITY_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_data_integrity.py"


def _run_integrity_check(db_path: Path) -> tuple[int, str]:
    """Run the integrity-check script against `db_path` and return (rc, stdout+stderr).

    Runs with the CWD set to the test's own directory (where `db_path` lives)
    rather than the project root. The JSON checks glob `oaic_cyber_statistics_*`
    relative to the CWD, so running from the project root would let real scrape
    outputs pollute these DB-focused tests. Isolating the CWD means the JSON
    checks see no files (and pass vacuously), so each test exercises only the
    DB invariant it targets.
    """
    proc = subprocess.run(
        [sys.executable, str(INTEGRITY_SCRIPT), "--db", str(db_path), "--quiet"],
        capture_output=True, text=True, cwd=str(db_path.parent),
    )
    return proc.returncode, proc.stdout + proc.stderr


class TestIntegrityChecker:
    def test_clean_db_passes(self, tmp_path):
        db = tmp_path / "clean.db"
        _make_test_db(db)
        rc, out = _run_integrity_check(db)
        assert rc == 0, f"Clean DB should pass; got:\n{out}"

    def test_truncated_severity_detected(self, tmp_path):
        db = tmp_path / "truncated.db"
        _make_test_db(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE DeduplicatedEvents SET severity = ? WHERE deduplicated_event_id = ?",
            ("EventSeverity.UNKNOW", "d1"),
        )
        conn.commit(); conn.close()
        rc, out = _run_integrity_check(db)
        assert rc != 0
        assert "severity-no-qualified-or-truncated" in out

    def test_qualified_event_type_detected(self, tmp_path):
        db = tmp_path / "qualified.db"
        _make_test_db(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE DeduplicatedEvents SET event_type = ? WHERE deduplicated_event_id = ?",
            ("CyberEventType.RANSOMWARE", "d1"),
        )
        conn.commit(); conn.close()
        rc, out = _run_integrity_check(db)
        assert rc != 0
        assert "event_type-no-qualified" in out

    def test_unknown_severity_value_detected(self, tmp_path):
        db = tmp_path / "weird.db"
        _make_test_db(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE DeduplicatedEvents SET severity = ? WHERE deduplicated_event_id = ?",
            ("Severe", "d1"),  # not in enum set
        )
        conn.commit(); conn.close()
        rc, out = _run_integrity_check(db)
        assert rc != 0
        assert "severity-in-enum-set" in out

    def test_asd_orphan_detected(self, tmp_path):
        db = tmp_path / "orphan.db"
        _make_test_db(db)
        conn = sqlite3.connect(db)
        # Drop a dedup row but leave its ASD classification
        conn.execute("DELETE FROM DeduplicatedEvents WHERE deduplicated_event_id = 'd2'")
        conn.commit(); conn.close()
        rc, out = _run_integrity_check(db)
        assert rc != 0
        assert "asd-no-orphans" in out

    def test_records_affected_outlier_detected(self, tmp_path):
        db = tmp_path / "outlier.db"
        _make_test_db(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE DeduplicatedEvents SET records_affected = ? WHERE deduplicated_event_id = ?",
            (5_000_000_000, "d1"),  # 5B - way above the LinkedIn 700M ceiling
        )
        conn.commit(); conn.close()
        rc, out = _run_integrity_check(db)
        assert rc != 0
        assert "records-affected-in-range" in out

    def test_known_vendor_misclassified_detected(self, tmp_path):
        db = tmp_path / "vendor.db"
        _make_test_db(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO DeduplicatedEvents "
            "(deduplicated_event_id, master_enriched_event_id, title, severity, "
            " event_type, is_australian_event, is_specific_event, "
            " victim_organization_name, victim_organization_industry, status) "
            "VALUES ('d4', 'e1', 't', 'High', 'Data Breach', 1, 1, "
            "        'HWL Ebsworth Lawyers', 'Government', 'Active')"
        )
        conn.commit(); conn.close()
        rc, out = _run_integrity_check(db)
        assert rc != 0
        assert "industry-known-vendors-correct" in out

    def test_dedup_status_drift_detected(self, tmp_path):
        db = tmp_path / "status.db"
        _make_test_db(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE DeduplicatedEvents SET status = 'Pending' WHERE deduplicated_event_id = 'd1'"
        )
        conn.commit(); conn.close()
        rc, out = _run_integrity_check(db)
        assert rc != 0
        assert "dedup-status-canonical" in out

    def test_master_link_orphan_detected(self, tmp_path):
        db = tmp_path / "master.db"
        _make_test_db(db)
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM EnrichedEvents WHERE enriched_event_id = 'e1'")
        conn.commit(); conn.close()
        rc, out = _run_integrity_check(db)
        assert rc != 0
        assert "dedup-master-links-exist" in out
