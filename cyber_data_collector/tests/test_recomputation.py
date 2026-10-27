"""Changed inputs reach downstream results: re-enriched records refresh their
events (ids kept), and ASD classifications are redone when their inputs change."""

from __future__ import annotations

import sqlite3

import pytest

from cyber_data_collector.dedup.record_fingerprints import refresh_changed_events

EVENT_COLUMNS = ("deduplicated_event_id TEXT PRIMARY KEY, master_enriched_event_id TEXT, title TEXT, "
                 "description TEXT, summary TEXT, event_type TEXT, severity TEXT, event_date TEXT, "
                 "records_affected INTEGER, victim_organization_name TEXT, victim_organization_industry TEXT, "
                 "attacking_entity_name TEXT, attack_method TEXT, is_australian_event BOOLEAN, "
                 "is_specific_event BOOLEAN, australian_relevance_score REAL, status TEXT, "
                 "roles_member_signature TEXT, created_at TEXT, updated_at TEXT")
RECORD_COLUMNS = ("enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title TEXT, description TEXT, "
                  "summary TEXT, event_type TEXT, severity TEXT, event_date TEXT, records_affected INTEGER, "
                  "attacking_entity_name TEXT, attack_method TEXT, is_australian_event BOOLEAN, "
                  "is_specific_event BOOLEAN, perplexity_enrichment_data TEXT, status TEXT")


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(f"""
        CREATE TABLE DeduplicatedEvents ({EVENT_COLUMNS});
        CREATE TABLE EnrichedEvents ({RECORD_COLUMNS});
        CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
        CREATE TABLE DedupDecisions (deduplicated_event_id TEXT, reasoning TEXT, created_at TEXT);
        INSERT INTO DeduplicatedEvents (deduplicated_event_id, master_enriched_event_id, title, event_date,
            records_affected, victim_organization_name, status, roles_member_signature)
            VALUES ('d1', 'r1', 'Acme data breach', '2024-03-12', 5000, 'Acme', 'Active', 'sig');
    """)
    for rid in ("r1", "r2"):
        c.execute("INSERT INTO EnrichedEvents (enriched_event_id, title, event_date, records_affected, "
                  "perplexity_enrichment_data, status) VALUES (?, 'Acme data breach', '2024-03-12', 5000, '{}', 'Active')",
                  (rid,))
        c.execute("INSERT INTO EventDeduplicationMap VALUES (?, 'd1')", (rid,))
    return c


def _event(conn):
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM DeduplicatedEvents WHERE deduplicated_event_id = 'd1'").fetchone()
    conn.row_factory = None
    return row


def test_first_sight_is_a_baseline(conn):
    report = refresh_changed_events(conn)
    assert report.baselined == 2 and report.changed_records == 0 and not report.refreshed_events


def test_corrected_dates_redate_the_event(conn):
    refresh_changed_events(conn)
    conn.execute("UPDATE EnrichedEvents SET event_date = '2024-02-20'")   # both records corrected
    report = refresh_changed_events(conn)
    assert report.refreshed_events == ["d1"]
    assert _event(conn)["event_date"] == "2024-02-20"
    assert _event(conn)["deduplicated_event_id"] == "d1"   # id kept


def test_master_correction_reaches_the_event(conn):
    refresh_changed_events(conn)
    conn.execute("UPDATE EnrichedEvents SET attack_method = 'Phishing' WHERE enriched_event_id = 'r1'")
    refresh_changed_events(conn)
    assert _event(conn)["attack_method"] == "Phishing"


def test_a_larger_supported_count_from_any_report_reaches_the_event(conn):
    """The reviewer's case: an incident at 100 records gets a report confirming 10,000."""
    refresh_changed_events(conn)
    conn.execute("UPDATE EnrichedEvents SET records_affected = 10000 WHERE enriched_event_id = 'r2'")
    refresh_changed_events(conn)
    assert _event(conn)["records_affected"] == 10000


def test_explicit_correction_is_kept(conn):
    from cyber_data_collector.dedup.event_facts import set_fact
    refresh_changed_events(conn)
    set_fact(conn, "d1", "records_affected", "900", reason="regulator figure")
    conn.execute("UPDATE EnrichedEvents SET records_affected = 12000 WHERE enriched_event_id = 'r1'")
    refresh_changed_events(conn)
    assert _event(conn)["records_affected"] == 900


def test_a_new_member_refreshes_the_facts(conn):
    """Membership change, not only content change, triggers recomputation."""
    refresh_changed_events(conn)
    conn.execute("INSERT INTO EnrichedEvents (enriched_event_id, title, event_date, records_affected, "
                 "perplexity_enrichment_data, status) VALUES ('r3', 'Acme', '2024-03-12', 10000, '{}', 'Active')")
    conn.execute("INSERT INTO EventDeduplicationMap VALUES ('r3', 'd1')")
    report = refresh_changed_events(conn)
    assert report.baselined == 1 and _event(conn)["records_affected"] == 10000


def test_implausible_counts_are_not_promoted(conn):
    refresh_changed_events(conn)
    conn.execute("UPDATE EnrichedEvents SET records_affected = 5000000000 WHERE enriched_event_id = 'r2'")
    refresh_changed_events(conn)
    assert _event(conn)["records_affected"] == 5000


def test_non_master_change_does_not_copy_master_fields(conn):
    refresh_changed_events(conn)
    conn.execute("UPDATE EnrichedEvents SET attack_method = 'Phishing' WHERE enriched_event_id = 'r2'")
    conn.execute("UPDATE DeduplicatedEvents SET attack_method = 'Ransomware'")
    refresh_changed_events(conn)
    assert _event(conn)["attack_method"] == "Ransomware"


def test_new_research_marks_roles_outdated(conn):
    refresh_changed_events(conn)
    conn.execute("UPDATE EnrichedEvents SET perplexity_enrichment_data = '{\"victim\": \"Acme Pty Ltd\"}' "
                 "WHERE enriched_event_id = 'r2'")
    refresh_changed_events(conn)
    assert _event(conn)["roles_member_signature"] == "record-content-changed"


def test_unchanged_records_refresh_nothing(conn):
    refresh_changed_events(conn)
    assert not refresh_changed_events(conn).refreshed_events


# --- ASD classifications --------------------------------------------------


@pytest.fixture
def classifier(tmp_path, monkeypatch):
    from scripts.asd_risk_classifier import ASDRiskClassifier
    db = tmp_path / "asd.db"
    c = sqlite3.connect(db)
    c.executescript(f"""
        CREATE TABLE DeduplicatedEvents ({EVENT_COLUMNS});
        CREATE TABLE ASDRiskClassifications (classification_id TEXT PRIMARY KEY,
            deduplicated_event_id TEXT UNIQUE NOT NULL, severity_category TEXT, primary_stakeholder_category TEXT,
            impact_type TEXT, reasoning_json TEXT, confidence_score REAL, model_used TEXT,
            created_at TEXT, updated_at TEXT);
        INSERT INTO DeduplicatedEvents (deduplicated_event_id, title, event_date, victim_organization_name, status)
            VALUES ('d1', 'Acme data breach', '2024-03-12', 'Acme', 'Active');
    """)
    c.commit()
    c.close()
    clf = ASDRiskClassifier(str(db), model="test-model", api_key="test")
    yield clf
    clf.close()


CLASSIFICATION = {"severity_category": "C4", "primary_stakeholder_category": "Small organisation",
                  "impact_type": "Data breach", "confidence": 0.8,
                  "reasoning": {"severity_reasoning": "", "stakeholder_reasoning": "",
                                "impact_reasoning": "", "information_quality": ""}}


def test_classification_from_current_inputs_is_reused(classifier):
    event = classifier.get_events(limit=10)[0]
    assert event["_is_stale"]
    classifier.save_classification("d1", CLASSIFICATION, event["_fingerprint"])
    assert classifier.stale_count() == 0
    assert classifier.get_cached_classification("d1", event["_fingerprint"]) is not None


def test_changed_event_is_reclassified(classifier):
    event = classifier.get_events(limit=10)[0]
    classifier.save_classification("d1", CLASSIFICATION, event["_fingerprint"])
    classifier.conn.execute("UPDATE DeduplicatedEvents SET victim_organization_name = 'Acme Holdings'")
    classifier.conn.commit()
    assert classifier.stale_count() == 1
    fresh = classifier.get_events(limit=10)[0]
    assert classifier.get_cached_classification("d1", fresh["_fingerprint"]) is None


def test_legacy_classification_without_fingerprint_is_stale(classifier):
    classifier.save_classification("d1", CLASSIFICATION)   # no fingerprint, as before this change
    assert classifier.stale_count() == 1


def test_new_model_reclassifies(classifier):
    event = classifier.get_events(limit=10)[0]
    classifier.save_classification("d1", CLASSIFICATION, event["_fingerprint"])
    classifier.model = "another-model"
    assert classifier.stale_count() == 1
