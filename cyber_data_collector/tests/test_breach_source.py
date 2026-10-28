"""Breach source and personal-information types: Jev required, stored, reused, never defaulted."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from cyber_data_collector.processing import breach_source as bs
from cyber_data_collector.storage.schema_init import initialize_database
from cyber_data_collector.utils.jev import JevJudge


class FakeClient:
    """Stands in for TypeSafeClient: answers from a per-title script."""

    def __init__(self, source_by_title, pii_by_title=None):
        self.source_by_title = source_by_title
        self.pii_by_title = pii_by_title or {}
        self.requests = []

    def system_one(self, state, questions, model):
        self.requests.append(state)
        title = state["event"]["title"]
        answers = {}
        for qid in questions:
            if qid == bs.SOURCE_QUESTION_ID:
                choice = self.source_by_title[title]
                if isinstance(choice, Exception):
                    raise choice
                answers[qid] = SimpleNamespace(type="choice", choice=choice, confidence=0.9,
                                               probabilities={choice: 0.9, "cannot_tell": 0.1})
            else:
                key = qid[len(bs.PII_QUESTION_PREFIX):]
                answers[qid] = SimpleNamespace(type="noul", noul=self.pii_by_title.get(title, {}).get(key, 0.1))
        return SimpleNamespace(answers=answers, model="jev-test", usage=None)

    def close(self):
        pass


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = tmp_path / "instance" / "t.db"
    initialize_database(str(path))
    c = sqlite3.connect(path)
    for n, (title, status) in enumerate([("Acme ransomware", "Active"), ("Bank emailed wrong customer", "Active"),
                                         ("Old merged", "Merged")]):
        c.execute("INSERT INTO RawEvents (raw_event_id, source_type, raw_content) VALUES (?, 'GoogleSearch', ?)",
                  (f"r{n}", f"{title}: page text"))
        c.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, description, status, "
                  "is_australian_event, is_specific_event, perplexity_enrichment_data) "
                  "VALUES (?, ?, ?, ?, 'Active', 1, 1, ?)",
                  (f"e{n}", f"r{n}", title, f"{title} snippet",
                   json.dumps({"attack_method": "ransomware", "threat_actor": "None", "reasoning": "x"})))
        c.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, master_enriched_event_id, title, status, "
                  "is_australian_event, is_specific_event, event_date) VALUES (?, ?, ?, ?, 1, 1, '2025-01-01')",
                  (f"d{n}", f"e{n}", title, status))
        c.execute("INSERT INTO EventDeduplicationMap (map_id, raw_event_id, enriched_event_id, deduplicated_event_id) "
                  "VALUES (?, ?, ?, ?)", (f"m{n}", f"r{n}", f"e{n}", f"d{n}"))
    c.commit()
    yield c
    c.close()


def _values(conn):
    return {r[0]: (r[1], json.loads(r[2]) if r[2] else None) for r in conn.execute(
        "SELECT deduplicated_event_id, breach_source_category, personal_info_types_json FROM DeduplicatedEvents")}


def test_fills_active_events_from_member_evidence(conn):
    client = FakeClient({"Acme ransomware": "malicious_or_criminal_attack",
                         "Bank emailed wrong customer": "human_error"},
                        {"Bank emailed wrong customer": {"contact_information": 0.92, "financial_details": 0.7}})
    report = bs.classify_events(conn, judge=JevJudge(conn, client=client))
    values = _values(conn)
    assert values["d0"][0] == "Malicious or Criminal Attack"
    assert values["d1"][0] == "Human Error"
    assert values["d1"][1]["contact_information"] and values["d1"][1]["financial_details"]
    assert not values["d1"][1]["health_information"]
    assert values["d2"] == (None, None)                       # merged events are not classified
    assert report.distribution == {"Malicious or Criminal Attack": 1, "Human Error": 1}
    # The question saw the member record, not just the (empty) event row.
    record = client.requests[0]["records"][0]
    assert record["page_text"].endswith("page text") and record["research"]["attack_method"] == "ransomware"
    assert "threat_actor" not in record["research"]           # "None" is not evidence
    stored = conn.execute("SELECT COUNT(*), COUNT(DISTINCT question_id) FROM ModelJudgements").fetchone()
    assert stored == (2 * (1 + len(bs.PII_CATEGORIES)), 1 + len(bs.PII_CATEGORIES))


def test_rerun_reuses_stored_answers_and_reasks_changed_events(conn):
    client = FakeClient({"Acme ransomware": "malicious_or_criminal_attack",
                         "Bank emailed wrong customer": "human_error"})
    bs.classify_events(conn, judge=JevJudge(conn, client=client))
    assert len(client.requests) == 2
    again = bs.classify_events(conn, judge=JevJudge(conn, client=client))
    assert len(client.requests) == 2 and again.reused == 2 and again.updated == 0
    # A rebuilt dedup table (columns lost) is refilled from stored answers at no cost.
    conn.execute("UPDATE DeduplicatedEvents SET breach_source_category = NULL, personal_info_types_json = NULL")
    refill = bs.classify_events(conn, judge=JevJudge(conn, client=client))
    assert len(client.requests) == 2 and refill.updated == 2
    # Changed evidence is asked again.
    conn.execute("UPDATE RawEvents SET raw_content = 'new text' WHERE raw_event_id = 'r0'")
    changed = bs.classify_events(conn, judge=JevJudge(conn, client=client))
    assert len(client.requests) == 3 and changed.asked == 1


def test_failures_are_not_defaulted_and_raise_after_storing_the_rest(conn):
    client = FakeClient({"Acme ransomware": RuntimeError("Jev down"),
                         "Bank emailed wrong customer": "human_error"})
    with pytest.raises(bs.BreachSourceIncomplete) as info:
        bs.classify_events(conn, judge=JevJudge(conn, client=client))
    assert list(info.value.report.failed) == ["d0"]
    values = _values(conn)
    assert values["d0"] == (None, None)                       # not 'Other', not False flags
    assert values["d1"][0] == "Human Error"                   # the rest is stored


def test_unknown_option_is_a_failure_not_a_category(conn):
    client = FakeClient({"Acme ransomware": "cyber_incident", "Bank emailed wrong customer": "human_error"})
    with pytest.raises(bs.BreachSourceIncomplete) as info:
        bs.classify_events(conn, judge=JevJudge(conn, client=client))
    assert "unknown breach-source option" in info.value.report.failed["d0"]
    assert _values(conn)["d0"] == (None, None)


def test_cannot_tell_is_stored_as_unknown(conn):
    client = FakeClient({"Acme ransomware": "cannot_tell", "Bank emailed wrong customer": "system_fault"})
    bs.classify_events(conn, judge=JevJudge(conn, client=client))
    assert _values(conn)["d0"][0] == "Unknown" and _values(conn)["d1"][0] == "System Fault"


def test_dry_run_stores_judgements_but_not_columns(conn):
    client = FakeClient({"Acme ransomware": "malicious_or_criminal_attack",
                         "Bank emailed wrong customer": "human_error"})
    report = bs.classify_events(conn, dry_run=True, judge=JevJudge(conn, client=client))
    assert report.updated == 2 and _values(conn)["d0"] == (None, None)


def test_requires_jev(conn, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(bs.BreachSourceUnavailable, match="TYPESAFE_API_KEY"):
        bs.classify_events(conn)


def test_script_exits_nonzero_without_jev(tmp_path, monkeypatch, capsys):
    from scripts import enrich_pii_and_source_category as script

    monkeypatch.setattr(script, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "instance" / "t.db"
    initialize_database(str(path))
    assert script.main(["--db", str(path)]) == 2
    assert "TYPESAFE_API_KEY" in capsys.readouterr().err


def test_dashboard_counts_classified_events(conn, tmp_path):
    """The OAIC source-split chart reads the labels this module stores."""
    from scripts.build_static_dashboard import prepare_source_split_comparison

    client = FakeClient({"Acme ransomware": "malicious_or_criminal_attack",
                         "Bank emailed wrong customer": "human_error"})
    bs.classify_events(conn, judge=JevJudge(conn, client=client))
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    oaic = [{"year": 2025, "period": "H1", "breach_sources": {
        "human_error": {"current_period": 100}, "malicious_attack": {"current_period": 200},
        "system_fault": {"current_period": 10}}}]
    series = prepare_source_split_comparison(oaic, db_path)
    assert series["periods"] == ["2025 H1"]
    assert (series["db_malicious"], series["db_human"], series["db_system"]) == ([1], [1], [0])


def test_evidence_is_the_whole_page_within_the_budget_and_any_cut_is_stated(conn):
    """2026-10-28: pages were cut to their first 1,200 characters, mostly site chrome;
    80% of pages mentioning AI first did so after that point."""
    menu = "Home | News | Subscribe | " * 60                             # ~1,560 characters of chrome
    conn.execute("UPDATE RawEvents SET raw_content = ? WHERE raw_event_id = 'r0'",
                 (menu + "The attackers used an AI agent to reach the portal.",))
    record = bs.event_state(conn, "d0")["records"][0]
    assert record["page_text"].endswith("AI agent to reach the portal.") and "page_text_cut" not in record
    conn.execute("UPDATE RawEvents SET raw_content = ? WHERE raw_event_id = 'r0'", ("x" * (bs.PAGE_CHARS + 500),))
    record = bs.event_state(conn, "d0")["records"][0]
    assert len(record["page_text"]) == bs.PAGE_CHARS
    assert record["page_text_cut"] == f"first {bs.PAGE_CHARS} of {bs.PAGE_CHARS + 500} characters"


def test_records_past_the_event_budget_are_named_not_hidden(conn, monkeypatch):
    monkeypatch.setattr(bs, "EVENT_PAGE_CHARS", 30)
    monkeypatch.setattr(bs, "MAX_RECORDS", 2)
    for n in range(1, 3):
        conn.execute("INSERT INTO RawEvents (raw_event_id, source_type, raw_content) VALUES (?, 'GoogleSearch', ?)",
                     (f"extra{n}", "y" * 50))
        conn.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, status, is_australian_event, "
                     "is_specific_event) VALUES (?, ?, ?, 'Active', 1, 1)", (f"ex{n}", f"extra{n}", f"Report {n}"))
        conn.execute("INSERT INTO EventDeduplicationMap (map_id, raw_event_id, enriched_event_id, deduplicated_event_id) "
                     "VALUES (?, ?, ?, 'd0')", (f"mx{n}", f"extra{n}", f"ex{n}"))
    state = bs.event_state(conn, "d0")
    first, second = state["records"]
    assert first["page_text"] == "Acme ransomware: page text"                 # 26 of the 30 characters
    assert second["page_text"] == "yyyy" and second["page_text_cut"] == "first 4 of 50 characters"
    assert state["event"]["record_count"] == 3 and state["other_record_titles"] == ["Report 2"]
