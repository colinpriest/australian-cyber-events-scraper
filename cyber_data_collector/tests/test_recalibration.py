"""Recalibration: labelled cases re-scored on current evidence, the cut-off recomputed, nothing
spent or overwritten when a prerequisite is missing, the old calibration kept on any failure."""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("typesafe_sdk")

from cyber_data_collector.dedup import calibration_guard  # noqa: E402
from cyber_data_collector.dedup import corroboration, org_timeline  # noqa: E402
from cyber_data_collector.dedup import event_reality as er  # noqa: E402
from cyber_data_collector.dedup import recalibration as rc  # noqa: E402
from cyber_data_collector.utils.jev import JevJudge  # noqa: E402
from scripts import calibrate_event_reality as cal  # noqa: E402

SCHEMA = """
    CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, title TEXT, event_date TEXT,
        victim_organization_name TEXT, status TEXT, updated_at TEXT);
    CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
    CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT);
    CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
    CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title TEXT,
        perplexity_enrichment_data TEXT, status TEXT, updated_at TEXT);
    CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_url TEXT, raw_content TEXT, raw_description TEXT);
"""
#: id -> (title, label, P(real) Jev gives it)
EVENTS = {"n1": ("Menu page one", "not_incident", 0.02), "n2": ("Menu page two", "not_incident", 0.05),
          "r1": ("Real breach one", "real", 0.9), "r2": ("Real breach two", "real", 0.8)}


class FakeTypeSafe:
    def __init__(self, fail_title=None):
        self.calls = 0
        self.fail_title = fail_title

    def system_one(self, state, questions, model=None):
        self.calls += 1
        title = state["event"]["title"]
        if title == self.fail_title:
            raise RuntimeError("Jev unavailable")
        p = next(v[2] for v in EVENTS.values() if v[0] == title)
        return SimpleNamespace(model="jev-1.14.0", usage=None,
                               answers={qid: SimpleNamespace(type="noul", noul=p) for qid in questions})


def _build(path: Path, with_events: bool = True) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    org_timeline.ensure_schema(conn)
    corroboration.ensure_schema(conn)
    if with_events:
        for did, (title, _, _) in EVENTS.items():
            conn.execute("INSERT INTO DeduplicatedEvents VALUES (?, ?, '2024-01-26', NULL, 'Active', NULL)", (did, title))
            conn.execute("INSERT INTO EnrichedEvents VALUES (?, ?, ?, ?, 'Active', NULL)",
                         (f"{did}-r", f"{did}-r", title, json.dumps({"formal_entity_name": title})))
            conn.execute("INSERT INTO RawEvents VALUES (?, 'https://example.com', ?, NULL)", (f"{did}-r", f"{title} page"))
            conn.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?)", (f"{did}-r", did))
    conn.commit()
    conn.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")     # a forgotten fake must fail, not spend
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    instance = tmp_path / "instance"
    instance.mkdir()
    live = instance / "live.db"
    _build(live, with_events=False)
    snapshot = instance / "labelling.db"
    _build(snapshot)
    labels = tmp_path / "labels.csv"
    labels.write_text("deduplicated_event_id,label,rationale,corroboration,labeller\n"
                      + "".join(f"{d},{v[1]},why,,test\n" for d, v in EVENTS.items()), encoding="utf-8")
    sample = tmp_path / "sample.csv"
    sample.write_text("deduplicated_event_id,stratum,weight\n" + "".join(f"{d},A,1\n" for d in EVENTS), encoding="utf-8")
    calibration = tmp_path / "calibration.json"
    old = {**er.calibration_identity(), "evidence": "event-reality:0000000000000000", "cutoff": 0.28,
           "labelled_used": 4, "generated": "2026-10-27T20:18:16"}
    calibration.write_text(json.dumps(old), encoding="utf-8")
    monkeypatch.setattr(er, "ROOT", tmp_path)      # the script records its files relative to the root
    monkeypatch.setattr(er, "LABELS_PATH", labels)
    monkeypatch.setattr(er, "CALIBRATION_PATH", calibration)
    monkeypatch.setattr(cal, "SAMPLE_PATH", sample)
    return SimpleNamespace(live=live, snapshot=snapshot, calibration=calibration, old=old, tmp=tmp_path)


def _run(world, fake=None, **kwargs):
    fake = fake or FakeTypeSafe()
    kwargs.setdefault("calibrate_args", ["--min-rejections", "1"])
    result = rc.recalibrate_step("event-reality", world.live, world.snapshot,
                                 judge_factory=lambda conn: JevJudge(conn, client=fake), escalation_client=object(),
                                 verify_keys=False, **kwargs)
    return result, fake


def test_a_stale_calibration_is_found_and_a_current_one_is_not(world):
    state = rc.calibration_state(rc.SPECS["event-reality"])
    assert state["state"] == "stale" and "evidence was" in state["reason"] and rc.needs_recalibration(state)
    _run(world)
    assert rc.calibration_state(rc.SPECS["event-reality"])["state"] == "current"


def test_recalibration_rescores_labelled_cases_and_records_the_fingerprint(world):
    result, fake = _run(world)
    new = json.loads(world.calibration.read_text(encoding="utf-8"))
    assert new["evidence"] == er.evidence_fingerprint() and new["labelled_used"] == 4
    # rejects the two menu pages (0.02, 0.05) and keeps the two real breaches (0.8, 0.9)
    assert 0.05 < new["cutoff"] < 0.8
    assert new["recalibration"]["previous"]["evidence"] == "event-reality:0000000000000000"
    assert new["recalibration"]["labelling_snapshot"] == "labelling.db" and new["recalibration"]["cases_scored"] == 4
    assert er.active_calibration(world.calibration)["cutoff"] == new["cutoff"]
    assert result["after"]["state"] == "current" and fake.calls == 4


def test_a_repeat_run_reuses_the_stored_answers(world):
    _, first = _run(world)
    _, second = _run(world)
    assert first.calls == 4 and second.calls == 0


def test_the_live_database_is_never_written(world):
    before = world.live.read_bytes()
    _run(world)
    assert world.live.read_bytes() == before
    assert (world.live.parent / rc.WORK_DIR_NAME / "event-reality.db").exists()


def test_the_labelling_snapshot_is_not_modified(world):
    before = world.snapshot.read_bytes()
    _run(world)
    assert world.snapshot.read_bytes() == before


def test_stored_timelines_are_carried_into_the_copy(world):
    conn = sqlite3.connect(world.live)
    conn.execute("INSERT INTO OrganisationTimelines VALUES (1, 'Menu page one', 'h', 'q', 'm', '1. 2024 | x', '[]', '2026-10-28')")
    conn.commit()
    conn.close()
    _run(world)
    work = sqlite3.connect(world.live.parent / rc.WORK_DIR_NAME / "event-reality.db")
    assert work.execute("SELECT COUNT(*) FROM OrganisationTimelines").fetchone()[0] == 1


def test_a_case_that_cannot_be_scored_stops_the_run_and_keeps_the_old_calibration(world):
    before = world.calibration.read_text(encoding="utf-8")
    with pytest.raises(rc.RecalibrationError, match="could not be scored"):
        _run(world, fake=FakeTypeSafe(fail_title="Menu page two"))
    assert world.calibration.read_text(encoding="utf-8") == before


def test_far_fewer_usable_cases_than_before_keeps_the_old_calibration(world):
    world.calibration.write_text(json.dumps({**world.old, "labelled_used": 100}), encoding="utf-8")
    before = world.calibration.read_text(encoding="utf-8")
    with pytest.raises(rc.RecalibrationError, match="only 4 labelled case"):
        _run(world)
    assert world.calibration.read_text(encoding="utf-8") == before


def test_a_missing_snapshot_spends_nothing(world):
    fake = FakeTypeSafe()
    with pytest.raises(rc.PrerequisiteMissing, match="snapshot for event-reality not found"):
        rc.registered_snapshot("event-reality", world.live, {"event-reality": str(world.tmp / "gone.db")})
    assert fake.calls == 0


def test_the_registry_resolves_beside_the_live_database(world, tmp_path):
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"snapshots": {"event-reality": "labelling.db"}}))
    assert rc.registered_snapshot("event-reality", world.live, registry=registry) == world.snapshot.resolve()
    with pytest.raises(rc.PrerequisiteMissing, match="names no labelling snapshot"):
        rc.registered_snapshot("duplicates", world.live, registry=registry)


def test_the_committed_registry_names_a_snapshot_for_every_calibrated_step():
    registered = json.loads(rc.SNAPSHOT_REGISTRY.read_text(encoding="utf-8"))["snapshots"]
    for step, spec in rc.SPECS.items():
        if spec.automatic and step not in ("coherence-same-organisation", "victim-fill"):      # not labelled yet
            assert step in registered, step


def test_missing_keys_stop_the_run_before_anything_is_spent(world, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(rc.PrerequisiteMissing, match="TYPESAFE_API_KEY"):
        rc.recalibrate_step("event-reality", world.live, world.snapshot, verify_keys=True)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(rc.PrerequisiteMissing, match="OPENAI_API_KEY"):
        rc.recalibrate_step("event-reality", world.live, world.snapshot, verify_keys=True)
    assert not (world.live.parent / rc.WORK_DIR_NAME).exists()


def test_dry_run_counts_the_cases_and_writes_nothing(world):
    before = world.calibration.read_text(encoding="utf-8")
    result = rc.recalibrate_step("event-reality", world.live, world.snapshot, dry_run=True)
    assert result["dry_run"] and result["labelled"] == 4 and result["present_in_snapshot"] == 4
    assert result["would_become"] == er.evidence_fingerprint()
    assert world.calibration.read_text(encoding="utf-8") == before
    assert not (world.live.parent / rc.WORK_DIR_NAME).exists()


def test_a_snapshot_missing_the_labelled_cases_is_refused(world):
    empty = world.live.parent / "empty.db"
    _build(empty, with_events=False)
    with pytest.raises(rc.RecalibrationError, match="none of the 4 labelled cases"):
        rc.recalibrate_step("event-reality", world.live, empty, judge_factory=lambda c: JevJudge(c, client=FakeTypeSafe()),
                            verify_keys=False)


def test_supplier_victims_are_a_registered_automatic_step():
    """Its recalibration is tested with the step's own fixtures in test_supplier_victims.py."""
    spec = rc.SPECS["supplier-victims"]
    assert spec.automatic and spec.carries == ("SupplierIncidentSearches",)
    assert "supplier-victims" in rc.PLANS and "supplier-victims" in rc.CALIBRATE
    registry = json.loads(rc.SNAPSHOT_REGISTRY.read_text(encoding="utf-8"))["snapshots"]
    assert registry["supplier-victims"] == "calibration_supplier_victims.db"


def test_status_lists_every_step_and_spends_nothing(world, capsys):
    from scripts import dedup_admin

    assert dedup_admin.main(["--db", str(world.live), "recalibrate", "--status", "--step", "event-reality"]) == 0
    out = capsys.readouterr().out
    assert "event-reality" in out and "stale" in out


def test_if_stale_does_nothing_when_everything_matches(world, capsys):
    from scripts import dedup_admin

    _run(world)
    assert dedup_admin.main(["--db", str(world.live), "recalibrate", "--if-stale", "--step", "event-reality"]) == 0
    assert "matches the current model, question and evidence processing" in capsys.readouterr().out


def test_the_cli_reports_a_missing_snapshot_as_exit_2(world, capsys):
    from scripts import dedup_admin

    code = dedup_admin.main(["--db", str(world.live), "recalibrate", "--step", "event-reality", "--dry-run",
                             "--snapshot", f"event-reality={world.tmp / 'gone.db'}"])
    assert code == 2 and "labelling snapshot for event-reality not found" in capsys.readouterr().err


def test_the_refresh_recalibrates_first_and_resolves_contradictions_after_the_merging_steps():
    source = (Path(__file__).resolve().parents[2] / "run_full_pipeline.py").read_text(encoding="utf-8")
    hygiene = source[source.index("def run_data_hygiene"):source.index("def run_recurrence_check")]
    assert 'recalibration = ["recalibrate", "--if-stale"]' in hygiene and "steps.insert(0, recalibration)" in hygiene
    assert "skip_recalibration" in hygiene and "--skip-recalibration" in source
    order = [hygiene.index(token) for token in ("coherence, same_org, duplicates, suppliers, label_contradictions",
                                                 '["classify-entities", "--stale-only"],\n')]
    assert order == sorted(order)
    assert 'label_contradictions.append("--dry-run")' in hygiene       # --report-event-rejections only lists
