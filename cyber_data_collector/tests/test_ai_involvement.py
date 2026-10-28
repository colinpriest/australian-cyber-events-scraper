"""AI involvement: Jev required, all three probabilities stored, reused; chart and report read it."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from cyber_data_collector.processing import ai_involvement as ai
from cyber_data_collector.reporting.population import ReportingPeriod
from cyber_data_collector.storage.schema_init import initialize_database
from cyber_data_collector.utils import jev
from cyber_data_collector.utils.jev import JevJudge

MEDICARE = "OpenAI agent accessed Medicare statistics portal"
CHATBOT = "Retailer chatbot leaks orders after prompt injection"
RANSOM = "Acme ransomware attack"


class FakeClient:
    """Stands in for TypeSafeClient: a per-title distribution (or an exception)."""

    def __init__(self, by_title):
        self.by_title = by_title
        self.requests = []

    def system_one(self, state, questions, model):
        self.requests.append(state)
        probs = self.by_title[state["event"]["title"]]
        if isinstance(probs, Exception):
            raise probs
        choice = max(probs, key=probs.get)
        return SimpleNamespace(answers={ai.QUESTION_ID: SimpleNamespace(
            type="choice", choice=choice, confidence=max(probs.values()), probabilities=probs)},
            model="jev-test", usage=None)

    def close(self):
        pass


SCRIPT = {
    MEDICARE: {"ai_attacker": 0.91, "ai_vulnerability": 0.05, "no_ai": 0.04},
    CHATBOT: {"ai_attacker": 0.10, "ai_vulnerability": 0.85, "no_ai": 0.05},
    RANSOM: {"ai_attacker": 0.02, "ai_vulnerability": 0.01, "no_ai": 0.97},
}


class FakeDescriber:
    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = set(fail_on)

    def __call__(self, state):
        title = state["event"]["title"]
        self.calls.append(title)
        if title in self.fail_on:
            raise RuntimeError("OpenAI down")
        return f"{title} happened. It involved AI. <b>bold</b>"


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = tmp_path / "instance" / "t.db"
    initialize_database(str(path))
    c = sqlite3.connect(path)
    rows = [(MEDICARE, "Active", "2026-06-15", "Services Australia"),
            (CHATBOT, "Active", "2025-03-02", "Retailer"),
            (RANSOM, "Active", "2025-08-20", "Acme"),
            ("Old merged", "Merged", "2025-01-01", None)]
    for n, (title, status, when, victim) in enumerate(rows):
        c.execute("INSERT INTO RawEvents (raw_event_id, source_type, raw_content) VALUES (?, 'GoogleSearch', ?)",
                  (f"r{n}", f"{title}: page text"))
        c.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, description, status, "
                  "is_australian_event, is_specific_event, perplexity_enrichment_data) "
                  "VALUES (?, ?, ?, ?, 'Active', 1, 1, ?)",
                  (f"e{n}", f"r{n}", title, f"{title} snippet", json.dumps({"attack_method": "x"})))
        c.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, master_enriched_event_id, title, status, "
                  "is_australian_event, is_specific_event, event_date, victim_organization_name) "
                  "VALUES (?, ?, ?, ?, 1, 1, ?, ?)", (f"d{n}", f"e{n}", title, status, when, victim))
        c.execute("INSERT INTO EventDeduplicationMap (map_id, raw_event_id, enriched_event_id, deduplicated_event_id) "
                  "VALUES (?, ?, ?, ?)", (f"m{n}", f"r{n}", f"e{n}", f"d{n}"))
    c.commit()
    c.close()
    return path


@pytest.fixture
def conn(db_path):
    c = sqlite3.connect(db_path)
    yield c
    c.close()


def _values(conn):
    return {r[0]: r[1:] for r in conn.execute(
        "SELECT deduplicated_event_id, ai_involvement, ai_attacker_probability, ai_vulnerability_probability, "
        "no_ai_probability FROM DeduplicatedEvents")}


def _run(conn, client=None, describer=None, **kw):
    return ai.classify_events(conn, judge=JevJudge(conn, client=client or FakeClient(SCRIPT)),
                              describer=describer or FakeDescriber(), **kw)


def test_schema_comes_from_init(conn):
    assert ai.has_schema(conn)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'EventDescriptions'").fetchone()


def test_label_and_all_three_probabilities_are_stored(conn):
    describer = FakeDescriber()
    report = _run(conn, describer=describer)
    values = _values(conn)
    assert values["d0"] == ("AI-attacker", 0.91, 0.05, 0.04)
    assert values["d1"] == ("AI-vulnerability", 0.10, 0.85, 0.05)
    assert values["d2"] == ("No-AI", 0.02, 0.01, 0.97)
    assert values["d3"] == (None, None, None, None)          # merged events are not judged
    assert report.distribution == {"AI-attacker": 1, "AI-vulnerability": 1, "No-AI": 1}
    # Only the AI incidents are described.
    assert sorted(describer.calls) == sorted([MEDICARE, CHATBOT]) and report.described == 2
    stored = json.loads(conn.execute(
        "SELECT probabilities_json FROM ModelJudgements WHERE subject_id = 'd0'").fetchone()[0])
    assert stored == SCRIPT[MEDICARE]


def test_rerun_reuses_answers_and_descriptions_and_reasks_changed_events(conn):
    client, describer = FakeClient(SCRIPT), FakeDescriber()
    _run(conn, client, describer)
    again = _run(conn, client, describer)
    assert len(client.requests) == 3 and again.reused == 3 and again.updated == 0
    assert len(describer.calls) == 2 and again.described == 0
    # A rebuilt dedup table is refilled from the stored answers at no cost.
    conn.execute("UPDATE DeduplicatedEvents SET ai_involvement = NULL, ai_attacker_probability = NULL")
    refill = _run(conn, client, describer)
    assert len(client.requests) == 3 and refill.updated == 3
    # Changed evidence is asked again, and its description rewritten.
    conn.execute("UPDATE RawEvents SET raw_content = 'new text' WHERE raw_event_id = 'r0'")
    changed = _run(conn, client, describer)
    assert len(client.requests) == 4 and changed.asked == 1 and changed.described == 1


def test_failures_are_not_defaulted_and_raise_after_storing_the_rest(conn):
    script = dict(SCRIPT, **{MEDICARE: RuntimeError("Jev down")})
    with pytest.raises(ai.AIInvolvementIncomplete) as info:
        _run(conn, FakeClient(script))
    assert list(info.value.report.failed) == ["d0"]
    values = _values(conn)
    assert values["d0"] == (None, None, None, None)          # not No-AI
    assert values["d1"][0] == "AI-vulnerability"


def test_unknown_option_or_missing_probability_is_a_failure(conn):
    script = dict(SCRIPT, **{MEDICARE: {"ai_attacker": 0.9, "no_ai": 0.1},
                             CHATBOT: {"ai_agent": 0.9, "ai_attacker": 0.05, "ai_vulnerability": 0.03,
                                       "no_ai": 0.02}})
    with pytest.raises(ai.AIInvolvementIncomplete) as info:
        _run(conn, FakeClient(script))
    assert "no probability for ai_vulnerability" in info.value.report.failed["d0"]
    assert "unknown AI-involvement option" in info.value.report.failed["d1"]
    assert _values(conn)["d0"][0] is None and _values(conn)["d1"][0] is None


def test_a_failed_description_fails_the_step_but_keeps_the_label(conn):
    with pytest.raises(ai.AIInvolvementIncomplete) as info:
        _run(conn, describer=FakeDescriber(fail_on=[CHATBOT]))
    assert info.value.report.failed["d1"].startswith("description:")
    assert _values(conn)["d1"][0] == "AI-vulnerability"
    assert ai.stored_description(conn, "d1") is None and ai.stored_description(conn, "d0")


def test_dry_run_stores_judgements_but_not_columns_or_descriptions(conn):
    describer = FakeDescriber()
    report = _run(conn, describer=describer, dry_run=True)
    assert report.updated == 3 and _values(conn)["d0"] == (None, None, None, None)
    assert describer.calls == []


def test_jev_is_required(conn, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(jev.JevUnavailable):
        ai.classify_events(conn)
    assert _values(conn)["d0"] == (None, None, None, None)


# --------------------------------------------------------------------------
# Report and dashboard
# --------------------------------------------------------------------------

PERIOD = ReportingPeriod.from_iso("2020-01-01", "2026-10-28")


def test_report_lists_only_ai_incidents_oldest_first_with_descriptions(conn):
    from scripts.build_ai_incident_report import NO_DESCRIPTION, build_report_html

    _run(conn)
    conn.execute("DELETE FROM EventDescriptions WHERE deduplicated_event_id = 'd0'")
    page = build_report_html(conn, PERIOD)
    rows = re.findall(r'<tr data-category="(\w+)"><td class="nowrap">([\d-]+)</td><td>([^<]+)</td>', page)
    assert rows == [("vulnerability", "2025-03-02", "Retailer"), ("attacker", "2026-06-15", "Services Australia")]
    assert RANSOM not in page
    assert "p = 0.85" in page and "p = 0.91" in page
    assert "&lt;b&gt;bold&lt;/b&gt;" in page and "<b>bold</b>" not in page     # escaped
    assert NO_DESCRIPTION in page                                             # missing, and says so
    assert "2 of 3 incidents" in page


def test_dashboard_chart_counts_ai_incidents_per_half_year(conn):
    import scripts.build_static_dashboard as sd

    _run(conn)
    conn.execute("UPDATE DeduplicatedEvents SET ai_involvement = NULL WHERE deduplicated_event_id = 'd2'")
    chart = sd.get_ai_involvement_by_half_year(conn, PERIOD.start_iso, PERIOD.end_iso)
    by_period = {p: (a, v) for p, a, v in zip(chart["periods"], chart["attacker"], chart["vulnerability"])}
    assert chart["periods"][0] == "2020 H1" and chart["periods"][-1] == "2026 H2"
    assert by_period["2026 H1"] == (1, 0) and by_period["2025 H1"] == (0, 1)
    assert sum(chart["attacker"]) + sum(chart["vulnerability"]) == 2
    assert chart["unjudged"] == 1 and chart["available"]
    check = [c for c in sd.build_reconciliation_checks(
        conn, _minimal_payload(conn, sd, chart), PERIOD, PERIOD, {"counts": [3]})
        if c.name == "AI involvement per half-year"][0]
    assert check.is_ok
    wrong = dict(chart, attacker=[0] * len(chart["periods"]))
    check = [c for c in sd.build_reconciliation_checks(
        conn, _minimal_payload(conn, sd, wrong), PERIOD, PERIOD, {"counts": [3]})
        if c.name == "AI involvement per half-year"][0]
    assert not check.is_ok


def _minimal_payload(conn, sd, chart):
    return {"monthly_counts": {"counts": [3]}, "oaic_comparison": {"database_counts": [3]},
            "overall_event_type_mix": {"counts": [3]}, "entity_types": {"types": []},
            "severity_trends": {"data": {}}, "event_type_mix": {"types": {}},
            "records_histogram": {"frequencies": []}, "ai_involvement": chart}


def test_chart_without_the_columns_says_not_classified():
    import scripts.build_static_dashboard as sd

    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT, status TEXT, event_date TEXT)")
    c.execute("INSERT INTO DeduplicatedEvents VALUES ('a', 'Active', '2025-01-01')")
    chart = sd.get_ai_involvement_by_half_year(c, PERIOD.start_iso, PERIOD.end_iso)
    assert not chart["available"] and chart["unjudged"] == 1


def test_dashboard_build_writes_the_chart_and_the_report(db_path, tmp_path, monkeypatch):
    import scripts.build_static_dashboard as sd

    c = sqlite3.connect(db_path)
    _run(c)
    c.close()
    monkeypatch.setattr(sd, "load_oaic_data", lambda: [])
    out = Path(sd.build_dashboard_file(str(db_path), str(tmp_path / "out")))
    page = out.read_text(encoding="utf-8")
    assert 'id="aiInvolvementChart"' in page and "__AI_INV__" not in page
    report = (out.parent / "ai_incidents.html").read_text(encoding="utf-8")
    assert "Services Australia" in report and "Retailer" in report
