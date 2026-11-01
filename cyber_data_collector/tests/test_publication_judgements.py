"""The publication step brings stored judgements up to date before its snapshot (owner, 2026-11-02: the AI
incident report was published saying its labels were stale after the supplier steps had merged events)."""
from __future__ import annotations

from pathlib import Path

import pytest

from cyber_data_collector.publication import judgements
from scripts import publish_outputs


def test_every_step_runs_and_a_failure_is_raised_after_the_rest():
    calls = []

    def ok(argv):
        calls.append(("ok", list(argv)))
        return 0

    def broken(argv):
        calls.append(("broken", list(argv)))
        return 2

    with pytest.raises(judgements.JudgementRefreshFailed, match="AI involvement \\(exit 2\\)"):
        judgements.refresh_judgements(Path("x.db"), steps=[("AI involvement", broken), ("breach source", ok)])
    assert calls == [("broken", ["--db", "x.db"]), ("ok", ["--db", "x.db"])]
    assert judgements.refresh_judgements(Path("x.db"), steps=[("breach source", ok)],
                                         run=lambda main, argv: main(argv)) == ["breach source"]


def test_the_default_steps_are_the_ai_and_breach_source_scripts():
    from scripts import classify_ai_involvement, enrich_pii_and_source_category

    assert [main for _name, main in judgements._steps()] == [enrich_pii_and_source_category.main,
                                                            classify_ai_involvement.main]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "events.db"
    path.write_bytes(b"")
    return path


def test_publication_refreshes_first_and_publishes_nothing_when_that_fails(monkeypatch, db, tmp_path):
    order = []
    monkeypatch.setattr(publish_outputs, "refresh_judgements", lambda path: order.append(("refresh", path)))
    monkeypatch.setattr(publish_outputs, "publish", lambda *a, **k: order.append(("publish",)) or
                        type("R", (), {"files": [], "snapshot_id": "s", "gate": "pass", "manifest": "m",
                                       "warnings": []})())
    assert publish_outputs.main(["--db-path", str(db), "--out-dir", str(tmp_path / "d")]) == 0
    assert order == [("refresh", db), ("publish",)]

    order.clear()

    def failing(path):
        raise judgements.JudgementRefreshFailed("AI involvement (exit 2)")

    monkeypatch.setattr(publish_outputs, "refresh_judgements", failing)
    assert publish_outputs.main(["--db-path", str(db), "--out-dir", str(tmp_path / "d")]) == 2
    assert order == []
    # Explicitly opted out, or reproducing a retained snapshot: no refresh.
    assert publish_outputs.main(["--db-path", str(db), "--out-dir", str(tmp_path / "d"),
                                 "--no-refresh-judgements"]) == 0
    assert publish_outputs.main(["--db-path", str(db), "--out-dir", str(tmp_path / "d"),
                                 "--snapshot-id", "latest-published"]) == 0
    assert order == [("publish",), ("publish",)]


def test_the_pipeline_dashboard_phase_refreshes_first_and_fails_when_that_fails(monkeypatch, tmp_path):
    import run_full_pipeline as rfp
    from cyber_data_collector.tests.test_pipeline_failure_reporting import _args

    calls = []
    monkeypatch.setattr(judgements, "refresh_judgements", lambda path, run=None: calls.append(path) or
                        (_ for _ in ()).throw(judgements.JudgementRefreshFailed("AI involvement (exit 2)")))
    db = tmp_path / "e.db"
    import sqlite3
    sqlite3.connect(db).executescript("CREATE TABLE DeduplicatedEvents (x); CREATE TABLE EntitiesV2 (x); "
                                      "CREATE TABLE DeduplicatedEventEntities (x);")
    pipeline = rfp.UnifiedPipeline(str(db))
    assert pipeline.run_dashboard_phase(_args(no_refresh_judgements=False, out_dir=str(tmp_path / "d"))) is False
    assert calls == [db]
    assert "AI involvement (exit 2)" in pipeline.results['dashboard']['errors'][0]
