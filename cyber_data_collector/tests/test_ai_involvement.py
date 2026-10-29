"""AI involvement: Jev required, all four probabilities stored, reused; chart and report read it."""

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
    MEDICARE: {"ai_attacker": 0.91, "ai_vulnerability": 0.05, "ai_misuse": 0.0, "no_ai": 0.04},
    CHATBOT: {"ai_attacker": 0.10, "ai_vulnerability": 0.85, "ai_misuse": 0.0, "no_ai": 0.05},
    RANSOM: {"ai_attacker": 0.02, "ai_vulnerability": 0.01, "ai_misuse": 0.0, "no_ai": 0.97},
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
    # No committed labels or calibration leak in: each test sets its own.
    monkeypatch.setattr(ai, "CALIBRATION_PATH", tmp_path / "no_calibration.json")
    monkeypatch.setattr(ai, "LABELS_PATH", tmp_path / "no_labels.csv")
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


def test_label_and_all_four_probabilities_are_stored(conn):
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
    conn.execute("UPDATE RawEvents SET raw_content = 'Services Australia: new text' WHERE raw_event_id = 'r0'")
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
    assert "P(AI) = 0.95" in page and "P(AI) = 0.96" in page                   # 1 - P(no AI)
    assert "NOT CALIBRATED" in page                                            # no calibration in the test
    assert "&lt;b&gt;bold&lt;/b&gt;" in page and "<b>bold</b>" not in page     # escaped
    assert NO_DESCRIPTION in page                                             # missing, and says so
    assert "2 of 3 incidents" in page


def test_uncertain_incidents_are_listed_but_not_in_the_headline_total(conn):
    from scripts.build_ai_incident_report import build_report_html

    _run(conn)
    conn.execute("UPDATE DeduplicatedEvents SET ai_involvement = 'AI-uncertain' WHERE deduplicated_event_id = 'd1'")
    page = build_report_html(conn, PERIOD)
    assert "1 of 3 incidents" in page and "A further 1 are listed" in page and "not counted in the 1" in page
    assert 'data-category="uncertain"' in page


def test_labels_and_descriptions_go_stale_when_evidence_changes(conn):
    import scripts.build_static_dashboard as sd
    from scripts.build_ai_incident_report import build_report_html

    client, describer = FakeClient(SCRIPT), FakeDescriber()
    _run(conn, client, describer)
    assert ai.stale_judgements(conn, PERIOD) == {}
    assert "Stale:" not in build_report_html(conn, PERIOD)
    # New evidence for a listed AI incident and for an unlisted No-AI one, not yet re-judged.
    conn.execute("UPDATE RawEvents SET raw_content = 'Services Australia: new text' WHERE raw_event_id = 'r0'")
    conn.execute("UPDATE RawEvents SET raw_content = 'Acme: new text' WHERE raw_event_id = 'r2'")
    assert ai.stale_judgements(conn, PERIOD) == {"d0": ai.STALE_CHANGED, "d2": ai.STALE_CHANGED}
    page = build_report_html(conn, PERIOD)
    assert f"Stale: {ai.STALE_CHANGED}" in page and "Written from older evidence" in page
    assert "1 listed incident(s) have a stale label" in page
    assert "1 incident(s) not listed have a stale label" in page
    assert ai.ai_incidents(conn, PERIOD)[1]["description_stale"]
    assert sd.get_ai_involvement_by_half_year(conn, PERIOD.start_iso, PERIOD.end_iso)["stale"] == 2
    # Re-judging brings both back up to date and rewrites the description.
    _run(conn, client, describer)
    assert ai.stale_judgements(conn, PERIOD) == {}
    assert not any(i["description_stale"] for i in ai.ai_incidents(conn, PERIOD))
    # A changed cut-off makes every label stale too.
    monkey_cal = {"question_version": ai.calibration_identity()["question_version"],
                  "model": ai.calibration_identity()["model"], "cutoff": 0.5}
    Path(ai.CALIBRATION_PATH).write_text(json.dumps(monkey_cal), encoding="utf-8")
    assert set(ai.stale_judgements(conn, PERIOD)) == {"d0", "d1", "d2"}


def test_a_failed_rejudgement_keeps_the_label_but_marks_it_stale(conn):
    from scripts.build_ai_incident_report import build_report_html

    _run(conn)
    conn.execute("UPDATE RawEvents SET raw_content = 'Services Australia: new text' WHERE raw_event_id = 'r0'")
    with pytest.raises(ai.AIInvolvementIncomplete):
        _run(conn, FakeClient(dict(SCRIPT, **{MEDICARE: RuntimeError("Jev down")})))
    assert _values(conn)["d0"][0] == "AI-attacker"                      # kept, not defaulted
    reason = ai.stale_judgements(conn, PERIOD)["d0"]
    assert reason.startswith(ai.STALE_FAILED) and "Jev down" in reason
    assert "Stale: last re-judgement failed" in build_report_html(conn, PERIOD)
    _run(conn)
    assert ai.stale_judgements(conn, PERIOD) == {}


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


def _choice(attacker, vulnerability, no_ai, misuse=0.0):
    probs = {"ai_attacker": attacker, "ai_vulnerability": vulnerability, "ai_misuse": misuse, "no_ai": no_ai}
    return SimpleNamespace(answer=max(probs, key=probs.get), probabilities=probs)


def test_close_scores_never_become_a_definite_category():
    """Vercel on 2026-10-28: attacker 0.48, vulnerability 0.42, no AI 0.10 was published as AI-attacker."""
    vercel = {"ai_attacker": 0.48, "ai_vulnerability": 0.42, "ai_misuse": 0.0, "no_ai": 0.10}
    assert not ai.is_settled(vercel)
    label, basis = ai.decide(vercel)
    assert label == ai.AI_UNCERTAIN and "category unsettled" in basis
    # Settled either way.
    assert ai.decide(_choice(0.9, 0.05, 0.05).probabilities)[0] == ai.AI_ATTACKER
    assert ai.decide(_choice(0.03, 0.02, 0.95).probabilities)[0] == ai.NO_AI
    # Involvement itself unsettled (P(AI) 0.5) without a calibration.
    assert ai.decide(_choice(0.45, 0.05, 0.5).probabilities)[0] == ai.AI_UNCERTAIN


def test_an_organisations_own_use_of_ai_is_misuse():
    """Kmart's facial recognition: no attacker, the organisation's own AI use was the breach."""
    label, basis = ai.decide(_choice(0.05, 0.05, 0.02, misuse=0.88).probabilities)
    assert label == ai.AI_MISUSE and "misuse 90% of it" in basis
    # Split three ways, no category holds three quarters: unsettled, never a guess.
    label, basis = ai.decide(_choice(0.3, 0.3, 0.05, misuse=0.35).probabilities)
    assert label == ai.AI_UNCERTAIN and "misuse 37%" in basis
    values = ai.values_from_judgement(_choice(0.05, 0.05, 0.02, misuse=0.88), cutoff=0.67)
    assert values["ai_involvement"] == ai.AI_MISUSE and values["ai_misuse_probability"] == 0.88


def test_misuse_is_charted_and_listed(conn, monkeypatch):
    import scripts.build_static_dashboard as sd
    from scripts.build_ai_incident_report import build_report_html

    monkeypatch.setitem(SCRIPT, RANSOM, {"ai_attacker": 0.02, "ai_vulnerability": 0.03, "ai_misuse": 0.9,
                                         "no_ai": 0.05})
    _run(conn)
    assert ai.stored_probabilities(conn)["d2"]["category"] == "ai_misuse"
    chart = sd.get_ai_involvement_by_half_year(conn, PERIOD.start_iso, PERIOD.end_iso)
    assert dict(zip(chart["periods"], chart["misuse"]))["2025 H2"] == 1 and sum(chart["misuse"]) == 1
    check = [c for c in sd.build_reconciliation_checks(
        conn, _minimal_payload(conn, sd, chart), PERIOD, PERIOD, {"counts": [3]})
        if c.name == "AI involvement per half-year"][0]
    assert check.is_ok
    page = build_report_html(conn, PERIOD)
    assert '<tr data-category="misuse">' in page and "1 where the organisation's own use of AI" in page
    assert [i["probability"] for i in ai.ai_incidents(conn, PERIOD) if i["deduplicated_event_id"] == "d2"] == [0.9]


def test_a_calibrated_cutoff_decides_involvement():
    weak = ai.values_from_judgement(_choice(0.45, 0.10, 0.45), cutoff=0.8)
    assert weak["ai_involvement"] == ai.NO_AI and weak["ai_attacker_probability"] == 0.45
    strong = ai.values_from_judgement(_choice(0.85, 0.05, 0.10), cutoff=0.8)
    assert strong["ai_involvement"] == ai.AI_ATTACKER and "calibrated cut-off 0.800" in strong["ai_involvement_basis"]


def test_an_unsettled_answer_is_escalated_and_the_reasoned_answer_stored(conn, monkeypatch):
    """Jev's unsettled distribution goes to the reasoning model; its answer decides and is kept."""
    from cyber_data_collector.utils import escalation

    calls = []

    def fake_escalate(conn_, subject_type, subject_id, question_id, version, state, question, options, client=None):
        calls.append(subject_id)
        assert set(options) == set(ai.LABELS)
        return {"ai_attacker": 0.05, "ai_vulnerability": 0.9, "ai_misuse": 0.0, "no_ai": 0.05}

    monkeypatch.setattr(escalation, "escalate_choice", fake_escalate)
    monkeypatch.setitem(SCRIPT, MEDICARE, _choice(0.48, 0.42, 0.10).probabilities)
    report = _run(conn)
    row = conn.execute("SELECT ai_involvement, ai_involvement_escalated, ai_vulnerability_probability "
                       "FROM DeduplicatedEvents WHERE deduplicated_event_id = 'd0'").fetchone()
    assert calls == ["d0"] and report.escalated == 1
    assert tuple(row) == (ai.AI_VULNERABILITY, 1, 0.9)


def test_a_failed_escalation_leaves_the_event_undecided(conn, monkeypatch):
    from cyber_data_collector.utils import escalation

    monkeypatch.setattr(escalation, "escalate_choice", lambda *a, **k: None)
    monkeypatch.setitem(SCRIPT, MEDICARE, _choice(0.48, 0.42, 0.10).probabilities)
    with pytest.raises(ai.AIInvolvementIncomplete):
        _run(conn)
    assert conn.execute("SELECT ai_involvement FROM DeduplicatedEvents WHERE deduplicated_event_id = 'd0'"
                        ).fetchone()[0] is None


def test_a_label_is_ground_truth_whatever_jev_says():
    values = ai.values_from_judgement(_choice(0.95, 0.03, 0.02), cutoff=0.8, label="no_ai")
    assert (values["ai_involvement"], values["ai_involvement_basis"]) == (ai.NO_AI, ai.BASIS_LABEL)


def test_a_calibration_for_another_model_or_question_is_not_applied(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    path = tmp_path / "cal.json"
    path.write_text(json.dumps({"cutoff": 0.7, **ai.calibration_identity()}))
    assert ai.active_calibration(path)["cutoff"] == 0.7
    path.write_text(json.dumps({"cutoff": 0.7, "model": "jev-1.14.0", "question_version": "ai-involvement-v1"}))
    assert ai.active_calibration(path) is None
    assert ai.active_calibration(tmp_path / "missing.json") is None


def test_cutoff_is_the_smallest_meeting_precision():
    from scripts.calibrate_ai_involvement import choose_cutoff

    rows = [{"p": p, "weight": 1.0, "is_ai": is_ai, "category_right": True, "stratum": "A"}
            for p, is_ai in [(0.99, True), (0.97, True), (0.95, True), (0.9, True), (0.8, True),
                             (0.7, False), (0.6, True), (0.1, False)]]
    chosen, _ = choose_cutoff(rows, precision=0.95, min_flags=3)
    # Flagging down to 0.8 is all right; reaching 0.7 lets a non-AI event in (5/6 < 0.95).
    assert chosen["cutoff"] == 0.8 and chosen["flagged_labelled"] == 5 and chosen["weighted_precision"] == 1.0
