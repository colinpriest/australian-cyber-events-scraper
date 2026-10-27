"""Labelled-real events left with no active record get the report their label cites."""

from __future__ import annotations

import json
import sqlite3

import pytest

from cyber_data_collector.dedup import label_evidence as le
from cyber_data_collector.quality.regression import EventResolver, check_event_reality, check_source_gate
from cyber_data_collector.storage.schema_init import initialize_database

URL = "https://cyberdaily.au/security/11281-home-affairs-personal-data-compromised-in-zircodata-hack"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "instance" / "t.db"
    initialize_database(str(path))
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    # The Home Affairs shape: a labelled-real event whose only record, a junk page, was deactivated.
    _record(c, "junk", "https://www.cyber.gov.au/ransomware-playbook", "Inactive", "ev-real")
    # An ordinary event with live evidence, labelled real and citing a URL: nothing to do.
    _record(c, "fine", "https://news.example/fine", "Active", "ev-fine")
    c.commit()
    yield c
    c.close()


def _record(c, rid, url, status, event):
    c.execute("INSERT INTO RawEvents (raw_event_id, source_type, raw_title, source_url, is_processed) "
              "VALUES (?, 'GoogleSearch', ?, ?, 1)", (rid, rid, url))
    c.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, status, is_australian_event, "
              "is_specific_event) VALUES (?, ?, ?, ?, 1, 1)", (rid, rid, rid, status))
    if not c.execute("SELECT 1 FROM DeduplicatedEvents WHERE deduplicated_event_id = ?", (event,)).fetchone():
        c.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, master_enriched_event_id, title, "
                  "event_date, victim_organization_name, status, is_australian_event, is_specific_event) "
                  "VALUES (?, ?, ?, '2024-10-01', 'Department of Home Affairs', 'Active', 1, 1)",
                  (event, rid, f"{event} title"))
    c.execute("INSERT INTO EventDeduplicationMap (map_id, raw_event_id, enriched_event_id, deduplicated_event_id, "
              "contribution_type, similarity_score) VALUES (?, ?, ?, ?, 'primary', 1.0)",
              (f"map-{rid}", rid, rid, event))


@pytest.fixture
def labels(tmp_path):
    path = tmp_path / "event_labels.csv"
    path.write_text("deduplicated_event_id,label,rationale,corroboration,labeller\n"
                    f"ev-real,real,Home Affairs data compromised in the ZircoDATA hack.,{URL},t\n"
                    "ev-fine,real,Reported.,https://news.example/fine,t\n"
                    "ev-packet,real,Reported.,packet,t\n", encoding="utf-8")
    return path


def _queued(conn):
    return [dict(r) for r in conn.execute(
        "SELECT source_type, source_url, is_processed, source_metadata FROM RawEvents WHERE source_type = ?",
        (le.SOURCE_TYPE,))]


def test_the_cited_report_is_queued_once_for_an_event_with_no_active_record(conn, labels):
    first = le.run(conn, labels)
    assert [q["event"] for q in first["queued"]] == ["ev-real"]
    rows = _queued(conn)
    assert len(rows) == 1 and rows[0]["source_url"] == URL and rows[0]["is_processed"] == 0
    assert json.loads(rows[0]["source_metadata"])["label_evidence"]["event"] == "ev-real"
    # Idempotent; the unprocessed record is reported as waiting.
    second = le.run(conn, labels)
    assert second["queued"] == [] and len(_queued(conn)) == 1
    assert second["attached"][0]["action"].startswith("waiting")


def test_dry_run_changes_nothing(conn, labels):
    assert [q["event"] for q in le.run(conn, labels, dry_run=True)["queued"]] == ["ev-real"]
    assert _queued(conn) == []


def test_once_processed_the_report_joins_its_event_and_the_junk_page_leaves(conn, labels, tmp_path):
    le.run(conn, labels)
    raw_id = conn.execute("SELECT raw_event_id FROM RawEvents WHERE source_type = ?", (le.SOURCE_TYPE,)).fetchone()[0]
    # The refresh scrapes, enriches and deduplicates it into an event of its own.
    conn.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, status, is_australian_event, "
                 "is_specific_event) VALUES ('report', ?, 'Home Affairs data in ZircoDATA hack', 'Active', 1, 1)",
                 (raw_id,))
    conn.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, master_enriched_event_id, title, event_date, "
                 "status, is_australian_event, is_specific_event) "
                 "VALUES ('ev-new', 'report', 'Home Affairs data in ZircoDATA hack', '2024-02-01', 'Active', 1, 1)")
    conn.execute("INSERT INTO EventDeduplicationMap (map_id, raw_event_id, enriched_event_id, deduplicated_event_id, "
                 "contribution_type, similarity_score) VALUES ('map-report', ?, 'report', 'ev-new', 'primary', 1.0)",
                 (raw_id,))
    conn.commit()

    result = le.run(conn, labels)
    attached = result["attached"][0]
    assert attached["action"] == "attached" and attached["merged"] == ["ev-new"]
    assert attached["inactive_split_out"] == ["junk"]
    status = dict(conn.execute("SELECT deduplicated_event_id, status FROM DeduplicatedEvents"))
    assert status["ev-real"] == "Active" and status["ev-new"] == "Merged"
    assert status[attached["rejected_split"]] == "Rejected"
    members = {r[0] for r in conn.execute(
        "SELECT enriched_event_id FROM EventDeduplicationMap WHERE deduplicated_event_id = 'ev-real'")}
    assert members == {"report"}

    # Neither label set is contradicted any more.
    gate_labels = tmp_path / "gate.csv"
    gate_labels.write_text("enriched_event_id,label,rationale,labeller\njunk,not_incident,playbook,t\n",
                           encoding="utf-8")
    resolver = EventResolver(conn)
    assert check_source_gate(conn, resolver, gate_labels)["labels.source_gate.contradictions"].value == 0
    assert check_event_reality(conn, resolver, labels)["labels.event_reality.contradictions"].value == 0
    # Running again changes nothing.
    again = le.run(conn, labels)
    assert again["queued"] == [] and again["attached"][0]["merged"] == []
    assert again["attached"][0]["inactive_split_out"] == []


def test_out_of_scope_label_must_not_stay_active(conn, tmp_path):
    path = tmp_path / "scope.csv"
    path.write_text("deduplicated_event_id,label,rationale,corroboration,labeller\n"
                    "ev-fine,out_of_scope,Scottish,packet,t\n", encoding="utf-8")
    result = check_event_reality(conn, EventResolver(conn), path)
    assert result["labels.event_reality.contradictions"].value == 1
    conn.execute("UPDATE DeduplicatedEvents SET status = 'Rejected' WHERE deduplicated_event_id = 'ev-fine'")
    assert check_event_reality(conn, EventResolver(conn), path)["labels.event_reality.contradictions"].value == 0


def test_a_stored_report_that_never_became_a_record_is_processed_again_not_duplicated(conn, labels):
    """Cyber Daily's ZircoDATA article was stored in 2025 (behind its registration wall) and dropped."""
    conn.execute("INSERT INTO RawEvents (raw_event_id, source_type, raw_title, source_url, source_metadata, "
                 "is_processed) VALUES ('old', 'GoogleSearch', 'Home Affairs ... ZircoDATA', ?, ?, 1)",
                 (URL.replace("https://", "https://www."), json.dumps({"severity": "Medium"})))
    conn.commit()
    result = le.run(conn, labels)
    assert [(q["event"], q["raw_event_id"]) for q in result["queued"]] == [("ev-real", "old")]
    assert _queued(conn) == []                       # no LabelledEvidence copy stored
    row = conn.execute("SELECT is_processed, source_metadata FROM RawEvents WHERE raw_event_id = 'old'").fetchone()
    meta = json.loads(row["source_metadata"])
    assert row["is_processed"] == 0 and meta["severity"] == "Medium"
    assert meta["label_evidence"]["event"] == "ev-real"
    assert le.run(conn, labels)["queued"] == []
    # Processed again and rejected: reported, not left waiting silently.
    conn.execute("UPDATE RawEvents SET is_processed = 1, processing_error = 'Rejected at classification' "
                 "WHERE raw_event_id = 'old'")
    conn.commit()
    assert le.run(conn, labels)["attached"][0]["action"].startswith("REJECTED")
