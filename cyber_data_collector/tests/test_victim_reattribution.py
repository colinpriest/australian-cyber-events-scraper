"""Timeline v2 questions, calibrated victim re-attribution, and the no-silent-fallback rules.

All offline: Perplexity, Jev and the reasoning model are fakes.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from types import SimpleNamespace

import pytest

pytest.importorskip("typesafe_sdk")

from cyber_data_collector.dedup import org_timeline as ot  # noqa: E402
from cyber_data_collector.dedup import victim_judgement as vj  # noqa: E402
from cyber_data_collector.utils.jev import JevJudge  # noqa: E402
from cyber_data_collector.utils.llm_models import openai_model, typesafe_model  # noqa: E402


@pytest.fixture(autouse=True)
def _no_text_extraction(monkeypatch):
    """The victim candidates include organisations the text names (an extraction by the fast model).
    These tests are about something else: the fake finds none, and bogus keys make a forgotten fake fail."""
    from cyber_data_collector.dedup import event_organisations

    for var in ("OPENAI_API_KEY", "TYPESAFE_API_KEY", "PERPLEXITY_API_KEY"):
        monkeypatch.setenv(var, "bogus-not-a-real-key")
    monkeypatch.setattr(event_organisations, "default_extractor",
                        lambda: (lambda text: event_organisations.NamedOrganisations()))


TIMELINE = "1. 16 October 2025 | Unrelated AFP matter | statement | https://x"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT, entity_kind TEXT,
            entity_type TEXT, industry TEXT, is_australian INTEGER);
        CREATE TABLE EntityAliases (raw_name TEXT, canonical_name TEXT, canonical_key TEXT);
        CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_url TEXT, raw_content TEXT,
            raw_description TEXT, event_date TEXT, source_metadata TEXT, discovered_at TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title TEXT,
            event_date TEXT, summary TEXT, description TEXT, perplexity_enrichment_data TEXT,
            status TEXT, updated_at TEXT);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, title TEXT, event_date TEXT,
            status TEXT, victim_organization_name TEXT, victim_organization_industry TEXT,
            vendor_organization_name TEXT, updated_at TEXT);
        CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
        CREATE TABLE EnrichedEventEntities (enriched_event_id TEXT, entity_id INTEGER,
            relationship_type TEXT, confidence_score REAL);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER,
            relationship_type TEXT, confidence_score REAL, source_count INTEGER);
        CREATE TABLE DedupSnapshots (snapshot_id TEXT PRIMARY KEY, batch_id TEXT, deduplicated_event_id TEXT,
            payload_json TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
    """)
    return c


def _entity(conn, name, australian=1):
    row = conn.execute("SELECT entity_id FROM EntitiesV2 WHERE entity_name = ?", (name,)).fetchone()
    if row:
        return row[0]
    return conn.execute("INSERT INTO EntitiesV2 (entity_name, entity_kind, is_australian) VALUES (?, 'organisation', ?)",
                        (name, australian)).lastrowid


def _event(conn, did, victims, records, links=()):
    """victims: recorded victim names; records: [(title, body, perplexity victim)]."""
    conn.execute("INSERT INTO DeduplicatedEvents VALUES (?, ?, '2025-10-16', 'Active', ?, NULL, NULL, NULL)",
                 (did, records[0][0], victims[0]))
    roles = [(v, "victim") for v in victims] + list(links)
    for i, (title, body, org) in enumerate(records):
        eid = f"{did}-{i}"
        conn.execute("INSERT INTO RawEvents VALUES (?, 'https://news/x', ?, NULL, NULL, NULL, '2026-06-01T00:00:00')",
                     (eid, body))
        conn.execute("INSERT INTO EnrichedEvents VALUES (?, ?, ?, '2025-10-16', ?, NULL, ?, 'Active', NULL)",
                     (eid, eid, title, body, json.dumps({"formal_entity_name": org})))
        conn.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?)", (eid, did))
        for name, role in roles:
            conn.execute("INSERT INTO EnrichedEventEntities VALUES (?, ?, ?, NULL)", (eid, _entity(conn, name), role))
    for name, role in roles:
        conn.execute("INSERT INTO DeduplicatedEventEntities VALUES (?, ?, ?, NULL, ?)",
                     (did, _entity(conn, name), role, len(records)))


def _role(conn, did, name):
    row = conn.execute("""SELECT relationship_type FROM DeduplicatedEventEntities dee JOIN EntitiesV2 v
                          ON v.entity_id = dee.entity_id WHERE deduplicated_event_id = ? AND entity_name = ?""",
                       (did, name)).fetchone()
    return row[0] if row else None


# ------------------------------------------------------------------ timelines

def test_aliases_are_spellings_never_similar_organisations():
    assert ot.is_same_name("Optus", "Singtel Optus Pty Limited")
    assert ot.is_same_name("Accenture", "Accenture plc")
    assert not ot.is_same_name("Australian Federal Police", "Aussie Fluid Power Pty Ltd")
    assert not ot.is_same_name("University of Western Australia", "UN Women Australia")
    assert not ot.is_same_name("Ambulance Victoria", "Ambulance Tasmania")
    assert not ot.is_same_name("Tasmanian Department of Education", "Department of Education")


def test_a_misfiled_record_never_enters_the_question(conn):
    _event(conn, "afp1", ["Australian Federal Police"],
           [("Aussie Fluid Power ransomware attack", "Anubis claimed Aussie Fluid Power", "Aussie Fluid Power Pty Ltd")])
    _event(conn, "afp2", ["Australian Federal Police"],
           [("AFP officers' data leaked", "AFP data leak", "Australian Federal Police")])
    query = ot.build_query(conn, _entity(conn, "Australian Federal Police"), "Australian Federal Police")
    assert "Aussie Fluid Power" not in query
    assert "supplier" in query and ot.AUSTRALIAN_SCOPE not in query


def test_multinationals_are_asked_about_australia(conn):
    _event(conn, "m1", ["Microsoft"], [("Microsoft zero-day", "exploited", "Microsoft Corporation")])
    conn.execute("UPDATE EntitiesV2 SET is_australian = 0 WHERE entity_name = 'Microsoft'")
    assert ot.AUSTRALIAN_SCOPE in ot.build_query(conn, _entity(conn, "Microsoft"), "Microsoft")


def test_a_refusal_is_asked_again_about_australia_and_then_kept(conn):
    _event(conn, "w1", ["Westpac"], [("Westpac breach", "PayID", "Westpac Banking Corporation")])
    calls = []

    def search(query, model):
        calls.append(query)
        if ot.AUSTRALIAN_SCOPE in query:
            return {"answer": "1. 2019 | PayID enumeration | June 2019 | https://x", "citations": []}
        return {"answer": "I can't reliably produce an exhaustive list.\n\nNONE FOUND.", "citations": []}

    entity = _entity(conn, "Westpac")
    timeline = ot.research(conn, entity, "Westpac", search=search)
    assert len(calls) == 2 and timeline["australia_only"] and "PayID" in timeline["timeline"]
    assert not ot.is_stale(conn, entity, "Westpac")
    ot.research(conn, entity, "Westpac", search=search)
    assert len(calls) == 2


def test_a_new_prompt_version_makes_a_timeline_stale(conn):
    _event(conn, "o1", ["Optus"], [("Optus breach", "API", "Optus")])
    entity = _entity(conn, "Optus")
    ot.research(conn, entity, "Optus", search=lambda q, m: {"answer": TIMELINE, "citations": []})
    assert not ot.is_stale(conn, entity)
    conn.execute("UPDATE OrganisationTimelines SET query = 'List every incident (v1)'")
    assert ot.is_stale(conn, entity)


def test_research_without_a_key_or_with_a_failed_search_fails_loudly(conn, monkeypatch):
    _event(conn, "o1", ["Optus"], [("Optus breach", "API", "Optus")])
    entity = _entity(conn, "Optus")
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="PERPLEXITY_API_KEY"):
        ot.research(conn, entity, "Optus")

    def broken(query, model):
        raise ConnectionError("503")

    with pytest.raises(RuntimeError, match="503"):
        ot.research(conn, entity, "Optus", search=broken)
    assert conn.execute("SELECT COUNT(*) FROM OrganisationTimelineFailures").fetchone()[0] == 1


def test_judgements_are_told_when_a_timeline_may_be_incomplete(conn):
    _event(conn, "o1", ["Optus"], [("Optus breach", "API", "Optus")])
    entity = _entity(conn, "Optus")
    ot.research(conn, entity, "Optus", search=lambda q, m: {"answer": TIMELINE, "citations": []})
    assert "warning" not in ot.for_judgement(conn, entity)          # current: input unchanged

    def broken(query, model):
        raise ConnectionError("timeout")

    # A new record arrives after the research; the refresh then fails.
    conn.execute("INSERT INTO RawEvents VALUES ('n1', 'https://z', 'Optus notice', NULL, NULL, NULL, '2099-01-01')")
    conn.execute("INSERT INTO EnrichedEvents VALUES ('n1', 'n1', 'Optus notice', NULL, '', NULL, '{}', 'Active', NULL)")
    conn.execute("INSERT INTO EventDeduplicationMap VALUES ('n1', 'o1')")
    researched = ot.stored(conn, entity)["researched_at"][:10]
    with pytest.raises(RuntimeError):
        ot.research(conn, entity, "Optus", search=broken)
    warning = ot.for_judgement(conn, entity)["warning"]
    assert "refreshing it failed" in warning and "records about the organisation have arrived" in warning
    assert f"Last successful research {researched}" in warning and "NOT evidence against" in warning


def test_unnumbered_timeline_lines_are_parsed():
    text = "2016-02 | DDoS on CommBank | Sept 2016 | https://a\n2016-2017 | Misaddressed emails | 2018 | https://b"
    items = ot.parse_timeline(text)
    assert [i["number"] for i in items] == ["1", "2"] and items[1]["date"] == "2016-2017"


# ------------------------------------------------------------------ victim attribution

class FakeJev:
    """Noul by recorded victim; the replacement Choice picks the named organisation."""

    def __init__(self, p_by_victim, pick=None, pick_p=0.9):
        self.p_by_victim, self.pick, self.pick_p = p_by_victim, pick, pick_p

    def system_one(self, state, questions, model=None):
        answers = {}
        for qid, q in questions.items():
            if qid == vj.QUESTION_ID:
                answers[qid] = SimpleNamespace(type="noul", noul=self.p_by_victim[state["recorded_victim"]])
            else:
                key = next((k for k, v in q.criteria.items() if v == self.pick), "none")
                probs = {k: (self.pick_p if k == key else (1 - self.pick_p) / (len(q.criteria) - 1))
                         for k in q.criteria}
                answers[qid] = SimpleNamespace(type="choice", choice=key, confidence=0.9, probabilities=probs)
        return SimpleNamespace(model="jev-1.14.0", usage=None, answers=answers)


def _calibration(tmp_path, cutoff=0.3, threshold=0.8):
    path = tmp_path / "cal.json"
    path.write_text(json.dumps({**vj.calibration_identity(), "cutoff": cutoff, "replacement_threshold": threshold}),
                    encoding="utf-8")
    return path


def _labels(tmp_path, rows=()):
    path = tmp_path / "labels.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["pair_id", "deduplicated_event_id", "recorded_victim", "label", "correct_victim", "rationale",
                    "labeller"])
        w.writerows(rows or [("other|1", "other", "x", "correct", "", "placeholder", "test")])
    return path


def _afp(conn):
    _event(conn, "afp1", ["Australian Federal Police"],
           [("Aussie Fluid Power ransomware attack", "Anubis listed Aussie Fluid Power", "Aussie Fluid Power"),
            ("Anubis claims Aussie Fluid Power", "Aussie Fluid Power data posted", "Aussie Fluid Power")])


def _model_env(monkeypatch):
    monkeypatch.setenv("TYPESAFE_MODEL", typesafe_model())
    monkeypatch.setenv("OPENAI_MODEL", openai_model())


def test_without_calibration_nothing_changes(conn, tmp_path):
    _afp(conn)
    result = vj.check_victim_attribution(conn, judge=JevJudge(conn, client=FakeJev({"Australian Federal Police": 0.03})),
                                         calibration_path=tmp_path / "none.json", labels_path=_labels(tmp_path))
    assert result["judged"] == 1 and not result["calibrated"] and len(result["reported"]) == 1
    assert _role(conn, "afp1", "Australian Federal Police") == "victim"


def test_apply_refuses_without_labels(conn, tmp_path):
    _afp(conn)
    for missing in (tmp_path / "absent.csv", tmp_path / "empty.csv"):
        if missing.name == "empty.csv":
            missing.write_text("pair_id,label\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="refusing"):
            vj.check_victim_attribution(conn, apply=True, judge=JevJudge(conn, client=FakeJev({})),
                                        calibration_path=_calibration(tmp_path), labels_path=missing)


def test_a_wrong_sole_victim_is_replaced_from_the_evidence_and_audited(conn, tmp_path):
    _afp(conn)
    judge = JevJudge(conn, client=FakeJev({"Australian Federal Police": 0.03}, pick="Aussie Fluid Power"))
    result = vj.check_victim_attribution(conn, apply=True, judge=judge, calibration_path=_calibration(tmp_path),
                                         labels_path=_labels(tmp_path))
    assert result["counts"] == {"reattributed": 1} and result["applied"]
    assert _role(conn, "afp1", "Aussie Fluid Power") == "victim"
    assert _role(conn, "afp1", "Australian Federal Police") == "bystander"
    assert conn.execute("SELECT victim_organization_name FROM DeduplicatedEvents").fetchone()[0] == "Aussie Fluid Power"
    audit = conn.execute("SELECT action, old_victim, new_victim, roles_before_json FROM VictimAttributionActions").fetchone()
    assert tuple(audit[:3]) == ("reattributed", "Australian Federal Police", "Aussie Fluid Power")
    assert json.loads(audit[3])
    assert conn.execute("SELECT COUNT(*) FROM DedupSnapshots").fetchone()[0] == 1
    # Idempotent: the new victim is judged right, nothing more happens.
    again = vj.check_victim_attribution(
        conn, apply=True, calibration_path=_calibration(tmp_path), labels_path=_labels(tmp_path),
        judge=JevJudge(conn, client=FakeJev({"Aussie Fluid Power": 0.97})))
    assert again["counts"] == {}


def test_the_replacement_entity_is_never_the_old_victim(conn, tmp_path):
    """An acronym clash must not resolve the replacement back to the victim being corrected."""
    _event(conn, "unw", ["University of Western Australia"],
           [("UN Women Australia thwarts cyber attack", "UN Women Australia used Cloudflare", "UN Women Australia")],
           links=[("UN Women Australia", "bystander")])
    old = _entity(conn, "University of Western Australia")
    assert vj.replacement_entity(conn, "unw", "UN Women Australia", old) == _entity(conn, "UN Women Australia")
    judge = JevJudge(conn, client=FakeJev({"University of Western Australia": 0.02}, pick="UN Women Australia"))
    vj.check_victim_attribution(conn, apply=True, judge=judge, calibration_path=_calibration(tmp_path),
                                labels_path=_labels(tmp_path))
    assert _role(conn, "unw", "UN Women Australia") == "victim"
    assert _role(conn, "unw", "University of Western Australia") == "bystander"


def test_no_confident_replacement_is_flagged_not_guessed(conn, tmp_path):
    _afp(conn)
    judge = JevJudge(conn, client=FakeJev({"Australian Federal Police": 0.03}, pick="Aussie Fluid Power", pick_p=0.5))
    result = vj.check_victim_attribution(conn, apply=True, judge=judge, calibration_path=_calibration(tmp_path),
                                         labels_path=_labels(tmp_path))
    assert result["counts"] == {"flagged": 1}
    assert _role(conn, "afp1", "Australian Federal Police") == "victim"


def test_a_wrong_co_victim_is_demoted_and_the_others_kept(conn, tmp_path):
    _event(conn, "bank", ["Westpac", "NAB"],
           [("Callback scams impersonate Westpac", "scam texts", "Westpac Banking Corporation")])
    judge = JevJudge(conn, client=FakeJev({"Westpac": 0.9, "NAB": 0.05}))
    result = vj.check_victim_attribution(conn, apply=True, judge=judge, calibration_path=_calibration(tmp_path),
                                         labels_path=_labels(tmp_path))
    assert result["counts"] == {"demoted": 1}
    assert _role(conn, "bank", "NAB") == "bystander" and _role(conn, "bank", "Westpac") == "victim"


def test_labels_are_ground_truth(conn, tmp_path):
    _afp(conn)
    pid = f"afp1|{_entity(conn, 'Australian Federal Police')}"
    judge = JevJudge(conn, client=FakeJev({"Australian Federal Police": 0.03}, pick="Aussie Fluid Power"))
    kept = vj.check_victim_attribution(
        conn, apply=True, judge=judge, calibration_path=_calibration(tmp_path),
        labels_path=_labels(tmp_path, [(pid, "afp1", "Australian Federal Police", "correct", "", "r", "t")]))
    assert kept["counts"] == {}
    other = vj.check_victim_attribution(
        conn, apply=True, judge=judge, calibration_path=_calibration(tmp_path),
        labels_path=_labels(tmp_path, [(pid, "afp1", "Australian Federal Police", "wrong", "Anubis Victim Pty", "r", "t")]))
    assert other["counts"] == {"flagged": 1}
    assert _role(conn, "afp1", "Australian Federal Police") == "victim"


def test_a_calibration_for_another_question_is_report_only(conn, tmp_path, caplog):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"question_version": "victim-attribution-v1", "model": "x", "cutoff": 0.3}),
                    encoding="utf-8")
    assert vj.active_calibration(path) is None
    assert "REPORT ONLY" in caplog.text


def test_a_failed_escalation_fails_loudly(conn, tmp_path):
    _afp(conn)

    class Broken:
        chat = completions = None

    broken = Broken()
    broken.chat = broken
    broken.completions = broken

    def create_with_completion(**kwargs):
        raise TimeoutError("openai down")

    broken.create_with_completion = create_with_completion
    with pytest.raises(RuntimeError, match="Escalation"):
        vj.judge_pairs(conn, vj.pairs_to_check(conn),
                       judge=JevJudge(conn, client=FakeJev({"Australian Federal Police": 0.4})),
                       escalation_client=broken)


class FakeReasoner:
    """Stands in for the reasoning model: ``p`` for every case."""

    def __init__(self, p):
        from types import SimpleNamespace as NS
        from cyber_data_collector.utils.escalation import EscalatedJudgement

        self.chat = NS(completions=NS(create_with_completion=lambda **kw: (
            EscalatedJudgement(rationale="r", probability=p), NS(usage=None, model="gpt-5.5"))))


def test_a_victim_score_is_read_only_from_a_decision_on_the_pairs_current_evidence(conn):
    """The final score was the newest Jev answer and the newest escalation, each chosen alone. It is now stored
    against the evidence both were given, and a pair whose records changed has no score until it is judged again."""
    from collections import defaultdict

    _afp(conn)
    _event(conn, "wbc1", ["Westpac"], [("Westpac breach", "PayID", "Westpac Banking Corporation")])
    pairs = vj.pairs_to_check(conn)
    afp = next(p for p in pairs if p["victim"] == "Australian Federal Police")
    p_by_victim = defaultdict(lambda: 0.9, {"Australian Federal Police": 0.4})     # 0.4 is mid-band: escalated
    scores = vj.judge_pairs(conn, pairs, judge=JevJudge(conn, client=FakeJev(p_by_victim)),
                            escalation_client=FakeReasoner(0.07))
    assert scores[afp["pair_id"]] == pytest.approx(0.07)
    assert vj.stored_final_probabilities(conn, [afp])[afp["pair_id"]] == pytest.approx(0.07)
    _add_record(conn, afp["deduplicated_event_id"], "A later report", "Later report text.", "Someone Else", "Someone Else")
    assert vj.stored_final_probabilities(conn, [afp]) == {}                    # the old 0.07 is not this pair's score
    others = [p for p in pairs if p["deduplicated_event_id"] != afp["deduplicated_event_id"]]
    assert vj.stored_final_probabilities(conn, others)                         # unchanged pairs keep theirs


# ------------------------------------------------------------------ flags are reassessed

def _add_record(conn, did, title, body, org, victim):
    """A new member for an event: the membership, and so the judged input, changes."""
    eid = f"{did}-new-{title[:8]}"
    conn.execute("INSERT INTO RawEvents VALUES (?, 'https://news/y', ?, NULL, NULL, NULL, '2026-06-02T00:00:00')",
                 (eid, body))
    conn.execute("INSERT INTO EnrichedEvents VALUES (?, ?, ?, '2025-10-16', ?, NULL, ?, 'Active', NULL)",
                 (eid, eid, title, body, json.dumps({"formal_entity_name": org})))
    conn.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?)", (eid, did))
    conn.execute("INSERT INTO EnrichedEventEntities VALUES (?, ?, 'victim', NULL)", (eid, _entity(conn, victim)))


def _flag_afp(conn, tmp_path):
    _afp(conn)
    judge =JevJudge(conn, client=FakeJev({"Australian Federal Police": 0.03}, pick="Aussie Fluid Power", pick_p=0.5))
    result = vj.check_victim_attribution(conn, apply=True, judge=judge, calibration_path=_calibration(tmp_path),
                                         labels_path=_labels(tmp_path))
    assert result["counts"] == {"flagged": 1}
    return ("afp1", _entity(conn, "Australian Federal Police"))


def test_a_flag_records_its_input_and_is_unresolved(conn, tmp_path):
    key = _flag_afp(conn, tmp_path)
    flag = vj.unresolved_attributions(conn)[key]
    assert flag["reason"].startswith("replacement 'Aussie Fluid Power' below threshold")
    assert flag["input_hash"] and flag["member_signature"]


def test_an_unchanged_flag_stays_unresolved_and_is_not_rewritten(conn, tmp_path):
    key = _flag_afp(conn, tmp_path)
    again = vj.check_victim_attribution(
        conn, apply=True, calibration_path=_calibration(tmp_path), labels_path=_labels(tmp_path),
        judge=JevJudge(conn, client=FakeJev({"Australian Federal Police": 0.03}, pick="Aussie Fluid Power",
                                            pick_p=0.5)))
    assert again["flag_review"] == {"flagged_before": 1, "confirmed": 0, "demoted": 0, "reattributed": 0,
                                    "still_flagged": 1, "inputs_changed": 0, "unresolved_now": 1}
    assert conn.execute("SELECT COUNT(*) FROM VictimAttributionActions").fetchone()[0] == 1
    assert key in vj.unresolved_attributions(conn)


def test_a_flag_is_confirmed_when_new_membership_vindicates_the_victim(conn, tmp_path):
    key = _flag_afp(conn, tmp_path)
    _add_record(conn, "afp1", "AFP confirms officers' data stolen", "The AFP said its systems were breached",
                "Australian Federal Police", "Australian Federal Police")
    result = vj.check_victim_attribution(
        conn, apply=True, calibration_path=_calibration(tmp_path), labels_path=_labels(tmp_path),
        judge=JevJudge(conn, client=FakeJev({"Australian Federal Police": 0.92})))
    assert result["counts"] == {"confirmed": 1}
    assert result["flag_review"]["confirmed"] == 1 and result["flag_review"]["inputs_changed"] == 1
    assert result["flag_review"]["unresolved_now"] == 0 and key not in vj.unresolved_attributions(conn)
    action, reason = conn.execute("SELECT action, reason FROM VictimAttributionActions "
                                  "ORDER BY action_id DESC LIMIT 1").fetchone()
    assert action == "confirmed" and "event membership changed" in reason
    assert _role(conn, "afp1", "Australian Federal Police") == "victim"
    # Idempotent: a confirmed pair is not confirmed again.
    again = vj.check_victim_attribution(
        conn, apply=True, calibration_path=_calibration(tmp_path), labels_path=_labels(tmp_path),
        judge=JevJudge(conn, client=FakeJev({"Australian Federal Police": 0.92})))
    assert again["counts"] == {} and again["flag_review"]["flagged_before"] == 0


def test_a_flag_is_replaced_once_the_evidence_supports_a_replacement(conn, tmp_path):
    key = _flag_afp(conn, tmp_path)
    _add_record(conn, "afp1", "Aussie Fluid Power confirms ransomware", "Aussie Fluid Power notified customers",
                "Aussie Fluid Power", "Aussie Fluid Power")
    result = vj.check_victim_attribution(
        conn, apply=True, calibration_path=_calibration(tmp_path), labels_path=_labels(tmp_path),
        judge=JevJudge(conn, client=FakeJev({"Australian Federal Police": 0.03}, pick="Aussie Fluid Power")))
    assert result["counts"] == {"reattributed": 1} and result["flag_review"]["reattributed"] == 1
    assert key not in vj.unresolved_attributions(conn)
    assert _role(conn, "afp1", "Aussie Fluid Power") == "victim"


def test_a_flag_on_a_pair_labelled_correct_is_confirmed(conn, tmp_path):
    key = _flag_afp(conn, tmp_path)
    pid = f"{key[0]}|{key[1]}"
    result = vj.check_victim_attribution(
        conn, apply=True, calibration_path=_calibration(tmp_path),
        labels_path=_labels(tmp_path, [(pid, "afp1", "Australian Federal Police", "correct", "", "r", "t")]),
        judge=JevJudge(conn, client=FakeJev({"Australian Federal Police": 0.03})))
    assert result["counts"] == {"confirmed": 1} and not vj.unresolved_attributions(conn)
    assert "labelled correct (input unchanged)" in result["actions"][0]["reason"]


def test_a_flag_without_a_calibration_is_left_unresolved(conn, tmp_path):
    key = _flag_afp(conn, tmp_path)
    result = vj.check_victim_attribution(
        conn, judge=JevJudge(conn, client=FakeJev({"Australian Federal Police": 0.92})),
        calibration_path=tmp_path / "none.json", labels_path=_labels(tmp_path))
    assert not result["calibrated"] and result["counts"] == {}
    assert result["flag_review"]["still_flagged"] == 1 and key in vj.unresolved_attributions(conn)


def test_flags_written_before_input_tracking_are_reassessed(conn, tmp_path):
    """The table as first created had no input columns; it is upgraded and the old flag re-judged."""
    conn.executescript(vj.ACTIONS_SCHEMA.replace(
        ",\n    input_hash             TEXT,              -- hash of the judged input (evidence, timeline, membership)\n"
        "    member_signature       TEXT               -- the event's members when judged (role_maintenance)", ""))
    assert "input_hash" not in {r[1] for r in conn.execute("PRAGMA table_info(VictimAttributionActions)")}
    _afp(conn)
    entity = _entity(conn, "Australian Federal Police")
    conn.execute("INSERT INTO VictimAttributionActions (batch_id, deduplicated_event_id, entity_id, old_victim, "
                 "action, p_recorded_victim, reason, created_at) VALUES ('old', 'afp1', ?, 'AFP', 'flagged', "
                 "0.03, 'no candidate chosen', '2026-10-28')", (entity,))
    assert ("afp1", entity) in vj.unresolved_attributions(conn)
    result = vj.check_victim_attribution(
        conn, apply=True, calibration_path=_calibration(tmp_path), labels_path=_labels(tmp_path),
        judge=JevJudge(conn, client=FakeJev({"Australian Federal Police": 0.92})))
    assert result["counts"] == {"confirmed": 1}
    assert "flag predates input tracking" in result["actions"][0]["reason"]
    assert conn.execute("SELECT input_hash FROM VictimAttributionActions ORDER BY action_id DESC").fetchone()[0]
