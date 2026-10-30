"""Event scope: a calibrated Jev judgement of whether each event affects Australia.

All offline: Jev and the reasoning model are fakes, and an autouse fixture sets bogus keys so a
test that forgets its fake fails instead of spending.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("typesafe_sdk")

from cyber_data_collector.dedup import calibration_guard  # noqa: E402
from cyber_data_collector.dedup import event_reality as er  # noqa: E402
from cyber_data_collector.dedup import event_scope as es  # noqa: E402
from cyber_data_collector.dedup import recalibration as rc  # noqa: E402
from cyber_data_collector.dedup.ledger import DedupLedger  # noqa: E402
from cyber_data_collector.quality import regression  # noqa: E402
from cyber_data_collector.utils.jev import JevJudge  # noqa: E402
from scripts import calibrate_event_scope as cal  # noqa: E402
from scripts import dedup_admin  # noqa: E402

SCHEMA = """
    CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, title TEXT, event_date TEXT,
        victim_organization_name TEXT, status TEXT, updated_at TEXT, description TEXT, summary TEXT,
        vendor_organization_name TEXT);
    CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
    CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT);
    CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
    CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title TEXT,
        perplexity_enrichment_data TEXT, status TEXT, updated_at TEXT);
    CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_url TEXT, raw_content TEXT, raw_description TEXT);
"""
#: id -> (title, P(in scope) Jev gives it)
EVENTS = {
    "us": ("US agencies hit by AI agents", 0.03),
    "uk": ("NHS trust ransomware", 0.08),
    "au": ("Optus data breach", 0.97),
    "supplier": ("Global vendor breach reaches Australian universities", 0.9),
}
NO_ESCALATION = object()    # never called: the fake Jev scores sit outside the escalation band


@pytest.fixture(autouse=True)
def _models(monkeypatch):
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    # A test that forgets to pass a fake judge or escalation client must fail, not make a paid call.
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")


class FakeTypeSafe:
    """P(in scope) by event title; records what it was shown."""

    def __init__(self, by_title=None, fail_titles=()):
        self.by_title = by_title or {t: p for t, p in EVENTS.values()}
        self.fail_titles = set(fail_titles)
        self.states = []
        self.calls = 0

    def system_one(self, state, questions, model=None):
        self.calls += 1
        self.states.append(state)
        title = state["event"]["title"]
        if title in self.fail_titles:
            raise RuntimeError("Jev unavailable")
        return SimpleNamespace(model="jev-1.14.0", usage=None,
                               answers={qid: SimpleNamespace(type="noul", noul=self.by_title[title])
                                        for qid in questions})


class FakeReasoner:
    def __init__(self, probability=None, fail=False):
        self.probability, self.fail, self.calls = probability, fail, 0
        self.chat = self
        self.completions = self

    def create_with_completion(self, response_model, messages, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("reasoning model unavailable")
        return (response_model(rationale="Victims are in Australia.", probability=self.probability),
                SimpleNamespace(model="gpt-5.5-2026-04-23", usage=None))


def _build(path, events=None):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for did, (title, _) in (events or EVENTS).items():
        conn.execute("INSERT INTO DeduplicatedEvents VALUES (?, ?, '2026-09-25', 'Victim', 'Active', NULL, "
                     "'desc', 'summary', NULL)", (did, title))
        conn.execute("INSERT INTO EnrichedEvents VALUES (?, ?, ?, ?, 'Active', NULL)",
                     (f"{did}-r", f"{did}-r", title, json.dumps({"formal_entity_name": f"{title} Pty"})))
        conn.execute("INSERT INTO RawEvents VALUES (?, 'https://example.com', ?, NULL)", (f"{did}-r", f"{title} page"))
        conn.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?)", (f"{did}-r", did))
    conn.commit()
    return conn


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setattr(DedupLedger, "snapshot_event", lambda self, batch, did: batches.append(batch))
    batches.clear()
    return _build(":memory:")


batches = []


def _cal(tmp_path, cutoff, **override):
    path = tmp_path / "cal.json"
    path.write_text(json.dumps({"cutoff": cutoff, **es.calibration_identity(), **override}))
    return path


def _labels(tmp_path, rows=(), reality=()):
    scope = tmp_path / "scope_labels.csv"
    scope.write_text("deduplicated_event_id,label,rationale,labeller\n"
                     + "".join(f"{d},{label},why,test\n" for d, label in rows), encoding="utf-8")
    real = tmp_path / "reality_labels.csv"
    real.write_text("deduplicated_event_id,label,rationale,corroboration,labeller\n"
                    + "".join(f"{d},{label},why,,test\n" for d, label in reality), encoding="utf-8")
    return scope, real


def _check(conn, tmp_path, cutoff=0.3, rows=(("au", "in_scope"),), reality=(), judge=None, **kwargs):
    scope, real = _labels(tmp_path, rows, reality)
    kwargs.setdefault("calibration_path", _cal(tmp_path, cutoff) if cutoff is not None else tmp_path / "none.json")
    return es.check_scope(conn, judge=judge or JevJudge(conn, client=FakeTypeSafe()),
                          labels_path=scope, reality_labels_path=real,
                          escalation_client=kwargs.pop("escalation_client", NO_ESCALATION), **kwargs)


def _status(conn):
    return dict(conn.execute("SELECT deduplicated_event_id, status FROM DeduplicatedEvents"))


# --- the question and its evidence ------------------------------------------------------------

def test_the_question_states_the_scope_rules():
    text = es.INSTRUCTIONS
    for needle in ("Australian arm", "supplier breach", "Australian customers", "none of them is in Australia",
                   "A reaction is not a victim", "separate Australian incident mentioned as background", "Australian outlet", "Australian regulator", "speculation",
                   "not by `event.victim`", "attacker", "an Australian victim is enough", "too thin"):
        assert needle in text, needle
    # The decision is the model's: the question names no country, agency, outlet or organisation to match on.
    for name in ("United States", "US ", "UK ", "SEC", "Census", "Education Department", "BBC", "Grok", "OpenAI"):
        assert name not in text, name
    assert es.question().model_dump()["criteria"] and es.question().instructions.startswith("This database")
    assert "within the scope" in es.escalation_question() and es.CRITERIA["false"] in es.escalation_question()


def test_the_state_is_the_event_evidence_without_a_corroboration_search(conn):
    state = es.build_state(conn, "us")
    assert state["records"][0]["page_text"].startswith("US agencies hit")
    assert state["records"][0]["perplexity"]["formal_entity_name"] == "US agencies hit by AI agents Pty"
    assert "corroboration" not in state


# --- judging, storage and escalation ------------------------------------------------------------

def test_every_active_event_is_judged_and_the_answers_are_stored_and_reused(conn, tmp_path):
    fake = FakeTypeSafe()
    result = _check(conn, tmp_path, cutoff=None, judge=JevJudge(conn, client=fake))
    assert result["judged"] == 4 and fake.calls == 4 and not result["calibrated"]
    stored = {r[0]: r[1] for r in conn.execute(
        "SELECT subject_id, question_version FROM ModelJudgements WHERE question_id = ?", (es.QUESTION_ID,))}
    assert set(stored.values()) == {es.QUESTION_VERSION} and len(stored) == 4
    again = FakeTypeSafe()
    _check(conn, tmp_path, cutoff=None, judge=JevJudge(conn, client=again))
    assert again.calls == 0


def test_inactive_events_are_not_judged(conn, tmp_path):
    conn.execute("UPDATE DeduplicatedEvents SET status = 'Rejected' WHERE deduplicated_event_id = 'uk'")
    assert _check(conn, tmp_path, cutoff=None)["judged"] == 3


def test_without_a_calibration_nothing_is_rejected_and_the_lowest_are_reported(conn, tmp_path):
    result = _check(conn, tmp_path, cutoff=None, apply=True)
    # Nothing is rejected on a score; only what a label decides (none of these events is labelled out of scope).
    assert not result["calibrated"] and result["rejections"] == result["labelled_rejections"]
    assert [f["deduplicated_event_id"] for f in result["lowest_scoring"]][:2] == ["us", "uk"]
    # An event labelled in scope is settled by its label and not scored.
    assert result["distribution"]["0.0-0.1"] == 2 and sum(result["distribution"].values()) == result["judged"]
    assert result["judged"] + result["settled_by_label"] == result["events"]
    assert _status(conn)["us"] == "Active" and _status(conn)["uk"] == "Active"


def test_a_missing_calibration_file_is_report_only(conn, tmp_path):
    assert es.active_calibration(tmp_path / "gone.json") is None


@pytest.mark.parametrize("override", [{"model": "jev-1.13.0+gpt-5.5-2026-04-23"},
                                      {"question_version": "event-scope-v0+x"},
                                      {"evidence": "event-scope:0000000000000000"}])
def test_a_calibration_for_another_model_question_or_evidence_is_ignored(tmp_path, override):
    assert es.active_calibration(_cal(tmp_path, 0.3, **override)) is None
    assert es.active_calibration(_cal(tmp_path, 0.3))["cutoff"] == 0.3


def test_a_calibration_without_a_cutoff_is_report_only(tmp_path):
    assert es.active_calibration(_cal(tmp_path, None)) is None


def test_the_calibrated_cutoff_rejects_only_below_it_through_the_ledger(conn, tmp_path):
    result = _check(conn, tmp_path, cutoff=0.3, apply=True)
    assert result["applied"] and {r["deduplicated_event_id"] for r in result["rejections"]} == {"us", "uk"}
    assert _status(conn) == {"us": "Rejected", "uk": "Rejected", "au": "Active", "supplier": "Active"}
    assert conn.execute("SELECT status FROM EnrichedEvents WHERE enriched_event_id = 'us-r'").fetchone()[0] == "Inactive"
    assert batches and all(b.startswith("reject-event-scope-") for b in batches)


def test_without_apply_it_only_reports(conn, tmp_path):
    result = _check(conn, tmp_path, cutoff=0.3, apply=False)
    assert len(result["rejections"]) == 2 and not result["applied"]
    assert set(_status(conn).values()) == {"Active"}


def test_an_event_labelled_in_scope_is_never_rejected(conn, tmp_path):
    result = _check(conn, tmp_path, cutoff=0.3, apply=True, rows=[("us", "in_scope")])
    assert [r["deduplicated_event_id"] for r in result["rejections"]] == ["uk"]
    assert _status(conn)["us"] == "Active"


def test_a_real_label_says_nothing_about_scope(conn, tmp_path):
    """``real`` is a real incident, not an Australian one: a real foreign incident is still rejected."""
    result = _check(conn, tmp_path, cutoff=0.3, apply=True, rows=[("au", "in_scope")], reality=[("us", "real")])
    assert {r["deduplicated_event_id"] for r in result["rejections"]} == {"us", "uk"}


def test_an_explicit_scope_label_overrides_the_reality_label(conn, tmp_path):
    result = _check(conn, tmp_path, cutoff=0.3, apply=True, rows=[("us", "out_of_scope")], reality=[("us", "real")])
    assert {r["deduplicated_event_id"] for r in result["rejections"]} == {"us", "uk"}
    assert [r["deduplicated_event_id"] for r in result["labelled_rejections"]] == ["us"]


def test_an_event_labelled_out_of_scope_is_rejected_whatever_it_scores(conn, tmp_path):
    result = _check(conn, tmp_path, cutoff=0.3, apply=True, rows=[("supplier", "out_of_scope")])
    assert [r["deduplicated_event_id"] for r in result["labelled_rejections"]] == ["supplier"]
    assert _status(conn)["supplier"] == "Rejected" and _status(conn)["au"] == "Active"


def test_an_event_labelled_out_of_scope_in_the_reality_labels_is_rejected_too(conn, tmp_path):
    result = _check(conn, tmp_path, cutoff=0.3, apply=True, rows=[], reality=[("au", "out_of_scope")])
    assert _status(conn)["au"] == "Rejected" and result["applied"]


def test_labels_are_acted_on_even_without_a_calibration(conn, tmp_path):
    # A labelled rejection rests on the label, not on the model's precision: a report-only step still applies it.
    result = _check(conn, tmp_path, cutoff=None, apply=True, rows=[("supplier", "out_of_scope")])
    assert result["applied"] and _status(conn)["supplier"] == "Rejected"
    assert [f["deduplicated_event_id"] for f in result["labelled_rejections"]] == ["supplier"]


def test_acting_without_readable_scope_labels_refuses(conn, tmp_path):
    scope, real = _labels(tmp_path)           # both header only: no label anywhere
    with pytest.raises(es.ScopeConfigError, match="empty"):
        es.check_scope(conn, apply=True, judge=JevJudge(conn, client=FakeTypeSafe()), labels_path=scope,
                       reality_labels_path=real, calibration_path=_cal(tmp_path, 0.3), escalation_client=NO_ESCALATION)
    with pytest.raises(es.ScopeConfigError, match="unreadable"):
        es.scope_labels(tmp_path / "gone.csv")
    assert set(_status(conn).values()) == {"Active"}


def test_an_empty_scope_file_is_enough_when_the_reality_labels_imply_scope(tmp_path):
    scope, real = _labels(tmp_path, reality=[("a", "out_of_scope")])
    assert es.labels(scope, real) == {"a": "out_of_scope"}


def test_reporting_does_not_need_the_labels(conn, tmp_path):
    scope, real = _labels(tmp_path)
    result = es.check_scope(conn, apply=False, judge=JevJudge(conn, client=FakeTypeSafe()), labels_path=scope,
                            reality_labels_path=real, calibration_path=_cal(tmp_path, 0.3),
                            escalation_client=NO_ESCALATION)
    assert len(result["rejections"]) == 2


def test_an_unknown_label_is_an_error(tmp_path):
    scope, _ = _labels(tmp_path, [("a", "maybe")])
    with pytest.raises(es.ScopeConfigError, match="'maybe'"):
        es.scope_labels(scope)


def test_reality_labels_map_to_scope(tmp_path):
    _, real = _labels(tmp_path, reality=[("a", "real"), ("b", "out_of_scope"), ("c", "not_incident"), ("d", "unsure")])
    assert es.reality_scope_labels(real) == {"b": "out_of_scope"}


def test_the_middle_band_is_escalated_and_the_escalation_decides(conn, tmp_path):
    fake = FakeTypeSafe({**{t: p for t, p in EVENTS.values()}, "Optus data breach": 0.4})
    reasoner = FakeReasoner(0.02)
    result = _check(conn, tmp_path, cutoff=0.3, apply=True, judge=JevJudge(conn, client=fake),
                    escalation_client=reasoner, rows=[("supplier", "in_scope")])
    assert reasoner.calls == 1 and result["escalated"] == 1
    assert "au" in {r["deduplicated_event_id"] for r in result["rejections"]}
    assert dict(conn.execute("SELECT subject_id, value FROM ModelJudgements WHERE question_id = ?",
                             (es.ESCALATION_QUESTION_ID,)))["au"] == pytest.approx(0.02)
    decided = es.final_decisions.latest(conn, es.STEP, ["au", "us"])
    assert {k: v[0] for k, v in decided.items()} == pytest.approx({"au": 0.02, "us": 0.03})
    # Both were rejected, so their records are inactive and their evidence is no longer what they were judged on:
    # neither has a decision on its current evidence (a reader must judge before it acts or calibrates).
    assert es.stored_final_probabilities(conn, ["au", "us"]) == {}


def test_a_failed_escalation_leaves_the_event_undecided(conn, tmp_path):
    fake = FakeTypeSafe({**{t: p for t, p in EVENTS.values()}, "Optus data breach": 0.4})
    result = _check(conn, tmp_path, cutoff=0.5, apply=True, judge=JevJudge(conn, client=fake),
                    escalation_client=FakeReasoner(fail=True), rows=[("supplier", "in_scope")])
    assert result["escalation_failed"] == ["au"]
    assert _status(conn)["au"] == "Active"        # 0.4 is below the cut-off, but it was not decided on Jev's mid-band score
    assert "au" not in {r["deduplicated_event_id"] for r in result["rejections"]}


def test_one_jev_failure_leaves_that_event_undecided_and_the_rest_judged(conn, tmp_path):
    result = _check(conn, tmp_path, cutoff=0.3, apply=True,
                    judge=JevJudge(conn, client=FakeTypeSafe(fail_titles=["NHS trust ransomware"])))
    # "au" is labelled in scope: the label decides, so the model is not asked about it (see the next test).
    assert result["judgement_failed"] == ["uk"] and result["judged"] == 2 and result["settled_by_label"] == 1
    assert _status(conn) == {"us": "Rejected", "uk": "Active", "au": "Active", "supplier": "Active"}


def test_repeated_jev_failures_stop_the_run_before_anything_is_rejected(tmp_path):
    many = {f"e{i}": (f"Event {i}", 0.01) for i in range(es.MAX_CONSECUTIVE_FAILURES + 2)}
    conn = _build(":memory:", many)
    fake = FakeTypeSafe({t: p for t, p in many.values()}, fail_titles=[t for t, _ in many.values()])
    with pytest.raises(es.ScopeJudgementError, match="consecutive Jev failures"):
        es.judge_events(conn, list(many), judge=JevJudge(conn, client=fake))
    assert fake.calls == es.MAX_CONSECUTIVE_FAILURES


def test_only_filters_events_by_prefix(conn, tmp_path):
    assert _check(conn, tmp_path, cutoff=None, only=["s"])["events"] == 1


# --- the evidence fingerprint -------------------------------------------------------------------

def test_the_fingerprint_moves_with_the_event_evidence_and_with_its_own_version(monkeypatch):
    before = es.evidence_fingerprint()
    monkeypatch.setattr(er, "PAGE_CHARS", er.PAGE_CHARS * 10)
    assert es.evidence_fingerprint() != before
    monkeypatch.undo()
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    assert es.evidence_fingerprint() == before
    monkeypatch.setattr(es, "EVIDENCE_VERSION", "event-scope-evidence-next")
    assert es.evidence_fingerprint() != before
    monkeypatch.undo()
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    monkeypatch.setattr(es, "ESCALATE_BAND", (0.2, 0.8))
    assert es.evidence_fingerprint() != before


def test_the_identity_names_both_models_and_both_question_versions():
    identity = es.calibration_identity()
    assert identity["question_version"] == f"{es.QUESTION_VERSION}+{es.ESCALATION_VERSION}"
    assert identity["model"].startswith("jev-1.14.0+") and identity["evidence"].startswith("event-scope:")
    assert calibration_guard.mismatch(identity, identity) is None


# --- the quality gate ---------------------------------------------------------------------------

def test_the_gate_checks_scope_labels_stay_honoured(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, status TEXT,
            master_enriched_event_id TEXT);
        CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);""")
    conn.executemany("INSERT INTO DeduplicatedEvents VALUES (?, ?, ?)", [
        ("au", "Rejected", "au-r"), ("us", "Active", "us-r"), ("uk", "Rejected", "uk-r"), ("supplier", "Active", "s-r")])
    path = tmp_path / "event_scope_labels.csv"
    path.write_text("deduplicated_event_id,label,rationale,labeller\n"
                    "au,in_scope,Australian,t\nus,out_of_scope,US only,t\nuk,out_of_scope,UK,t\n"
                    "supplier,unsure,x,t\ngone,in_scope,x,t\n", encoding="utf-8")
    found = regression.check_event_scope(conn, regression.EventResolver(conn), path)
    contradictions = found["labels.event_scope.contradictions"]
    assert contradictions.value == 2 and {e.split(":")[0] for e in contradictions.examples} == {"au", "us"}
    assert found["labels.event_scope.unchecked"].value == 2           # 'supplier' (unsure) and 'gone'


# --- the command --------------------------------------------------------------------------------

def _result(**override):
    base = {"events": 3, "judged": 3, "escalated": 0, "judgement_failed": [], "escalation_failed": [],
            "cutoff": None, "calibrated": False, "applied": False, "rejections": [], "labelled_rejections": [],
            "restored_protected": [], "distribution": {},
            "lowest_scoring": [{"deduplicated_event_id": "abcdef012", "title": "US agencies", "p_in_scope": 0.03}]}
    return {**base, **override}


def test_the_command_exits_2_without_jev_or_the_escalation_key(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    assert dedup_admin.main(["--db", str(tmp_path / "x.db"), "check-scope"]) == 2
    assert "TYPESAFE_API_KEY" in capsys.readouterr().err
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    monkeypatch.delenv("OPENAI_API_KEY")
    assert dedup_admin.main(["--db", str(tmp_path / "x.db"), "check-scope"]) == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "x.db").exists()


def test_the_command_writes_findings_and_exits_1_when_something_is_undecided(monkeypatch, tmp_path, capsys):
    db = tmp_path / "x.db"
    _build(db).close()
    monkeypatch.setattr(es, "check_scope", lambda conn, apply=False, only=None: _result(judgement_failed=["us"]))
    assert dedup_admin.main(["--db", str(db), "check-scope", "--apply"]) == 1
    findings = json.loads((tmp_path / "event_scope_findings.json").read_text(encoding="utf-8"))
    assert findings["apply"] is True and findings["judgement_failed"] == ["us"]
    out = capsys.readouterr()
    assert "REPORT ONLY" in out.out and "UNDECIDED" in out.err


def test_the_command_exits_1_when_the_run_cannot_act_safely(monkeypatch, tmp_path, capsys):
    db = tmp_path / "x.db"
    _build(db).close()

    def refuse(conn, apply=False, only=None):
        raise es.ScopeConfigError("event scope labels empty")

    monkeypatch.setattr(es, "check_scope", refuse)
    assert dedup_admin.main(["--db", str(db), "check-scope", "--apply"]) == 1
    assert "Nothing rejected" in capsys.readouterr().err


def test_the_command_reports_what_it_rejected(monkeypatch, tmp_path, capsys):
    db = tmp_path / "x.db"
    _build(db).close()
    rejected = [{"deduplicated_event_id": "abcdef012", "title": "US agencies", "p_in_scope": 0.03}]
    monkeypatch.setattr(es, "check_scope", lambda conn, apply=False, only=None: _result(
        calibrated=True, applied=True, cutoff=0.3, rejections=rejected))
    assert dedup_admin.main(["--db", str(db), "check-scope", "--apply", "--event", "abc"]) == 0
    assert "Rejected 1 of 3 event(s) as out of scope" in capsys.readouterr().out


def test_the_refresh_runs_check_scope_right_after_check_events():
    source = (Path(__file__).resolve().parents[2] / "run_full_pipeline.py").read_text(encoding="utf-8")
    hygiene = source[source.index("def run_data_hygiene"):source.index("def run_recurrence_check")]
    assert 'scope = ["check-scope"]' in hygiene and 'scope.append("--apply")' in hygiene
    assert "sources, check, scope," in hygiene


# --- calibration --------------------------------------------------------------------------------

@pytest.fixture
def world(tmp_path, monkeypatch):
    """A database whose scores are stored, a sample, labels and the reality labels, with paths redirected."""
    foreign = {f"f{i}": (f"Foreign incident {i}", 0.005 * (i + 1)) for i in range(12)}      # 0.005 .. 0.06, below the escalation band
    home = {f"a{i}": (f"Australian incident {i}", 0.8 + 0.01 * i) for i in range(8)}
    events = {**foreign, **home, "x1": ("Extra real incident", 0.95)}
    instance = tmp_path / "instance"
    instance.mkdir()
    db = instance / "labelling.db"
    conn = _build(db, events)
    fake = FakeTypeSafe({t: p for t, p in events.values()})
    es.check_scope(conn, judge=JevJudge(conn, client=fake), calibration_path=tmp_path / "none.json",
                   escalation_client=NO_ESCALATION)
    conn.close()
    sample = tmp_path / "sample.csv"
    sample.write_text("deduplicated_event_id,stratum,weight\n"
                      + "".join(f"{d},A,1\n" for d in foreign) + "".join(f"{d},B,2.5\n" for d in home),
                      encoding="utf-8")
    scope = tmp_path / "scope_labels.csv"
    scope.write_text("deduplicated_event_id,label,rationale,labeller\n"
                     + "".join(f"{d},out_of_scope,foreign,t\n" for d in foreign if d != "f11")
                     + "f11,unsure,unclear,t\n" + "".join(f"{d},in_scope,au,t\n" for d in home), encoding="utf-8")
    reality = tmp_path / "reality_labels.csv"
    reality.write_text("deduplicated_event_id,label,rationale,corroboration,labeller\nx1,real,r,,t\n"
                       "a0,not_incident,r,,t\n", encoding="utf-8")
    calibration = tmp_path / "calibration.json"
    monkeypatch.setattr(es, "ROOT", tmp_path)
    monkeypatch.setattr(es, "LABELS_PATH", scope)
    monkeypatch.setattr(er, "LABELS_PATH", reality)
    monkeypatch.setattr(es, "CALIBRATION_PATH", calibration)
    monkeypatch.setattr(cal, "SAMPLE_PATH", sample)
    return SimpleNamespace(db=db, tmp=tmp_path, calibration=calibration, sample=sample, scope=scope,
                           events=events, fake=fake)


def test_calibrate_chooses_the_largest_cutoff_meeting_the_precision(world, capsys):
    assert cal.main(["--db", str(world.db), "calibrate", "--min-rejections", "1"]) == 0
    result = json.loads(world.calibration.read_text(encoding="utf-8"))
    # 11 labelled out of scope (0.005-0.055), all below the Australian events (0.80 and up).
    # Rejecting also the lowest-scoring Australian event (0.80) would drop the precision to 11/13.5.
    assert 0.055 < result["cutoff"] < 0.06 and result["at_cutoff"]["weighted_precision"] == 1.0
    assert result["labelled_out_of_scope"] == 11 and result["unsure_excluded"] == 1
    # x1 is labelled 'real' in the reality labels: a real incident says nothing about scope, so it is not used.
    assert result["extra_labelled_outside_sample"] == 0
    assert result["labelled_used"] == 11 + 8
    assert result["evidence"] == es.evidence_fingerprint() and result["calibrated_on_database"] == "labelling.db"
    assert es.active_calibration(world.calibration)["cutoff"] == result["cutoff"]


def test_a_labelled_australian_event_the_model_scores_low_stops_the_cutoff(world):
    # The labellers say a0 is in scope, but the model scored it 0.03, among the foreign events.
    conn = sqlite3.connect(world.db)
    conn.execute("UPDATE ModelJudgements SET value = 0.03 WHERE subject_id = 'a0' AND question_id = ?",
                 (es.QUESTION_ID,))
    conn.execute("UPDATE FinalDecisions SET final_score = 0.03 WHERE subject_id = 'a0' AND step = ?", (es.STEP.name,))
    conn.commit()
    conn.close()
    cal.main(["--db", str(world.db), "calibrate", "--min-rejections", "1"])
    result = json.loads(world.calibration.read_text(encoding="utf-8"))
    assert 0.025 < result["cutoff"] < 0.03          # rejecting a0 (weight 2.5) would cost precision


def test_calibrate_reports_no_cutoff_when_the_precision_is_never_met(world):
    cal.main(["--db", str(world.db), "calibrate", "--min-rejections", "50"])
    result = json.loads(world.calibration.read_text(encoding="utf-8"))
    assert result["cutoff"] is None and es.active_calibration(world.calibration) is None


def test_sample_stratifies_on_the_stored_score_with_a_census_of_the_low_band(world):
    assert cal.main(["--db", str(world.db), "sample", "--census-below", "0.5", "--sample-mid", "3",
                     "--sample-high", "2", "--seed", "1"]) == 0
    rows = list(csv.DictReader(world.sample.open(encoding="utf-8")))
    by = {s: [r for r in rows if r["stratum"] == s] for s in "ABC"}
    assert len(by["A"]) == 12 and {r["weight"] for r in by["A"]} == {"1.0"}          # every event below 0.5
    assert len(by["B"]) == 3 and float(by["B"][0]["weight"]) == pytest.approx(8 / 3, abs=1e-3)   # 8 in 0.5-0.9
    assert len(by["C"]) == 1 and by["C"][0]["deduplicated_event_id"] == "x1"                     # 1 above 0.9
    again = cal.main(["--db", str(world.db), "sample", "--sample-mid", "3", "--sample-high", "2", "--seed", "1"])
    assert again == 0 and list(csv.DictReader(world.sample.open(encoding="utf-8"))) == rows     # seeded


def test_extend_raises_the_census_without_redrawing_and_reweights_the_rest(world):
    # Scores: f0-f11 0.005-0.06, a0-a7 0.80-0.87, x1 0.95. The existing sample: f* census (A), a* sample (B).
    rows = list(csv.DictReader(world.sample.open(encoding="utf-8")))
    world.sample.write_text("deduplicated_event_id,stratum,weight\n"
                            + "".join(f"{r['deduplicated_event_id']},{r['stratum']},{r['weight']}\n"
                                      for r in rows if r["deduplicated_event_id"] not in ("a1", "a4"))
                            + "x1,C,1.0\n", encoding="utf-8")
    assert cal.main(["--db", str(world.db), "sample", "--extend", "--census-below", "0.845"]) == 0
    out = {r["deduplicated_event_id"]: r for r in csv.DictReader(world.sample.open(encoding="utf-8"))}
    # Census below 0.845: every f*, the sampled a0/a2/a3, and the unsampled a1/a4 - all weight 1.
    census = {d for d, r in out.items() if r["stratum"] == "A"}
    assert census == {f"f{i}" for i in range(12)} | {"a0", "a1", "a2", "a3", "a4"}
    assert {out[d]["weight"] for d in census} == {"1.0"}
    # B is what is left of the old sample above it (a5-a7), standing for the 3 events of 0.845-0.9; C unchanged.
    assert {d for d, r in out.items() if r["stratum"] == "B"} == {"a5", "a6", "a7"}
    assert {float(out[d]["weight"]) for d in ("a5", "a6", "a7")} == {1.0}
    assert out["x1"]["stratum"] == "C"


def test_extend_reweights_a_partly_sampled_band(world):
    world.sample.write_text("deduplicated_event_id,stratum,weight\n"
                            + "".join(f"f{i},A,1.0\n" for i in range(12)) + "a0,B,4\na6,B,4\nx1,C,1\n",
                            encoding="utf-8")
    assert cal.main(["--db", str(world.db), "sample", "--extend", "--census-below", "0.825"]) == 0
    out = {r["deduplicated_event_id"]: r for r in csv.DictReader(world.sample.open(encoding="utf-8"))}
    assert {d for d, r in out.items() if r["stratum"] == "A"} == {f"f{i}" for i in range(12)} | {"a0", "a1", "a2"}
    assert out["a6"]["stratum"] == "B" and float(out["a6"]["weight"]) == pytest.approx(5.0)   # a3-a7: 5 events


@pytest.mark.parametrize("sample, census, message", [
    ("f0,A,1\nx1,C,1\na0,B,1\n", 0.03, "a census cannot be narrowed"),            # f5+ score above 0.03 ...
    ("f0,A,1\nx1,B,1\n", 0.5, "above the band it was drawn from"),               # x1 scores 0.95: not a B row
    ("f0,A,1\na0,C,1\na1,B,1\n", 0.5, "below the band it was drawn from"),
    ("f0,A,1\nghost,B,1\nx1,C,1\n", 0.5, "no score on its current evidence"),
    ("f0,A,1\na0,B,1\n", 0.5, "no sampled row is left"),                         # C holds x1 and no row
])
def test_extend_refuses_a_sample_it_cannot_extend_soundly(world, capsys, sample, census, message):
    rows = "deduplicated_event_id,stratum,weight\n" + sample
    if message == "a census cannot be narrowed":
        rows += "f9,A,1\n"
    world.sample.write_text(rows, encoding="utf-8")
    assert cal.main(["--db", str(world.db), "sample", "--extend", "--census-below", str(census)]) == 2
    assert message in capsys.readouterr().err
    assert world.sample.read_text(encoding="utf-8") == rows                      # left as it was


def test_extend_needs_an_existing_sample(world, capsys):
    world.sample.unlink()
    assert cal.main(["--db", str(world.db), "sample", "--extend"]) == 2
    assert "needs an existing sample" in capsys.readouterr().err


def test_sample_needs_stored_judgements(tmp_path, monkeypatch, capsys):
    db = tmp_path / "empty.db"
    _build(db).close()
    sqlite3.connect(db).executescript(
        "CREATE TABLE IF NOT EXISTS ModelJudgements (judgement_id TEXT, subject_type TEXT, subject_id TEXT, "
        "question_id TEXT, question_version TEXT, primitive TEXT, answer TEXT, value REAL, confidence REAL, "
        "probabilities_json TEXT, question_json TEXT, input_hash TEXT, model_requested TEXT, model_returned TEXT, "
        "input_tokens INTEGER, output_tokens INTEGER, created_at TEXT)")
    monkeypatch.setattr(cal, "SAMPLE_PATH", tmp_path / "sample.csv")
    assert cal.main(["--db", str(db), "sample"]) == 2
    assert "check-scope" in capsys.readouterr().err


def test_packets_carry_no_score_and_skip_events_already_labelled(world):
    out = world.tmp / "packets"
    assert cal.main(["--db", str(world.db), "packet", "--out", str(out)]) == 0
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    # every sampled event except f11 (unsure is still a label) is labelled, so only none are left to label ...
    assert index == []
    assert cal.main(["--db", str(world.db), "packet", "--out", str(out), "--include-labelled"]) == 0
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    assert len(index) == 20
    text = (out / index[0]["packet"]).read_text(encoding="utf-8")
    packet = json.loads(text)
    assert packet["deduplicated_event_id"] == index[0]["deduplicated_event_id"]
    assert "records" in packet and packet["definition"] == es.INSTRUCTIONS and packet["event"]["title"]
    for token in ("p_in_scope", "probab", "stratum", "weight", "0.02"):
        assert token not in text.lower().replace("probability that", "")


def test_packets_alias_still_works(world):
    assert cal.main(["--db", str(world.db), "packets", "--out", str(world.tmp / "p2"), "--include-labelled"]) == 0


# --- recalibration ------------------------------------------------------------------------------

def test_event_scope_is_a_recalibration_step(world, monkeypatch):
    spec = rc.SPECS["event-scope"]
    assert spec.automatic and spec.escalates and rc.PLANS["event-scope"] and rc.CALIBRATE["event-scope"][0]
    monkeypatch.setattr(es, "CALIBRATION_PATH", world.tmp / "absent.json")
    assert rc.calibration_state(spec)["state"] == "missing"
    world.calibration.write_text(json.dumps({**es.calibration_identity(), "evidence": "event-scope:0000", "cutoff": 0.3}))
    monkeypatch.setattr(es, "CALIBRATION_PATH", world.calibration)
    state = rc.calibration_state(spec)
    assert state["state"] == "stale" and rc.needs_recalibration(state)


def test_recalibrating_rescores_the_labelled_cases_and_writes_a_matching_calibration(world, monkeypatch):
    world.calibration.write_text(json.dumps({**es.calibration_identity(), "evidence": "event-scope:0000",
                                             "cutoff": 0.3, "labelled_used": 20}))
    live = world.db.parent / "live.db"
    _build(live, {}).close()
    monkeypatch.setattr(rc, "work_database", lambda step, live_db, snapshot, fresh=False, extra_tables=(): _copy(snapshot, live_db))
    fake = FakeTypeSafe({t: p for t, p in world.events.values()})
    result = rc.recalibrate_step("event-scope", live, world.db, judge_factory=lambda c: JevJudge(c, client=fake),
                                 escalation_client=NO_ESCALATION, verify_keys=False,
                                 calibrate_args=["--min-rejections", "1"])
    new = json.loads(world.calibration.read_text(encoding="utf-8"))
    assert new["evidence"] == es.evidence_fingerprint() and new["labelled_used"] == 19   # the reality label x1 (real) says nothing about scope
    assert new["recalibration"]["previous"]["evidence"] == "event-scope:0000"
    assert result["after"]["state"] == "current" and result["cases_scored"] == 19


def _copy(snapshot, live_db):
    work = Path(live_db).parent / "work.db"
    work.write_bytes(Path(snapshot).read_bytes())
    return work


def test_the_snapshot_registry_names_the_event_scope_snapshot():
    registered = json.loads(rc.SNAPSHOT_REGISTRY.read_text(encoding="utf-8"))["snapshots"]
    assert "event-scope" in registered


def test_a_real_event_rejected_as_out_of_scope_is_not_a_reality_label_contradiction(tmp_path):
    """``real`` says the incident happened; the quality gate excuses a scope rejection, not any other."""
    from cyber_data_collector.quality import regression as rg

    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT, status TEXT, master_enriched_event_id TEXT);
        CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
        CREATE TABLE DedupSnapshots (snapshot_id TEXT, batch_id TEXT, deduplicated_event_id TEXT, payload_json TEXT);
        INSERT INTO DeduplicatedEvents VALUES ('mazda', 'Rejected', 'e1'), ('junk', 'Rejected', 'e2');
        INSERT INTO DedupSnapshots VALUES ('s1', 'reject-event-scope-20261029', 'mazda', '{}'),
                                          ('s2', 'reject-event-reality-20261029', 'junk', '{}');
    """)
    labels = tmp_path / "reality.csv"
    labels.write_text("deduplicated_event_id,label,rationale,corroboration,labeller\n"
                      "mazda,real,Mazda breach,,t\njunk,real,a real one wrongly rejected,,t\n", encoding="utf-8")
    result = rg.check_event_reality(c, rg.EventResolver(c), labels)
    contradictions = [m for k, m in result.items() if k.endswith("contradictions")][0]
    assert contradictions.value == 1
    assert any("junk" in e for e in contradictions.examples) and not any("mazda" in e for e in contradictions.examples)


def test_labels_decide_first_so_a_labelled_event_is_not_asked_about(conn, tmp_path):
    """Labelled in scope is never rejected and labelled out of scope always is: neither needs a (re-)ask."""
    jev = FakeTypeSafe()
    result = _check(conn, tmp_path, cutoff=0.3, apply=True, judge=JevJudge(conn, client=jev),
                    rows=[("au", "in_scope"), ("us", "out_of_scope")])
    asked = {state["event"]["title"] for state in jev.states}
    assert asked.isdisjoint({EVENTS["au"][0], EVENTS["us"][0]}) and result["settled_by_label"] == 2
    assert _status(conn)["us"] == "Rejected" and _status(conn)["au"] == "Active"
    # Without --apply nothing is settled by a label: every event is judged and reported.
    report = _check(conn, tmp_path, cutoff=0.3, apply=False, judge=JevJudge(conn, client=FakeTypeSafe()))
    assert report["settled_by_label"] == 0
