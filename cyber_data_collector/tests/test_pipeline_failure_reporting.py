"""The pipeline must report failure when a required stage fails, and must not
publish a dashboard that fails validation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import run_full_pipeline as rfp


def _args(**over):
    base = dict(dashboard_only=False, re_enrich=False, classify_only=False, discover_only=False,
                skip_classification=True, continue_on_error=False, out_dir="dashboard")
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    p = rfp.UnifiedPipeline(str(tmp_path / "db.sqlite"))
    monkeypatch.setattr(p, "print_summary", lambda: None)
    return p


def _phases(pipeline, monkeypatch, discovery=True, dashboard=True):
    async def disc(args):
        return discovery

    monkeypatch.setattr(pipeline, "run_discovery_phase", disc)
    monkeypatch.setattr(pipeline, "run_dashboard_phase", lambda args: dashboard)


def test_failed_required_phase_fails_the_run_even_when_continuing(pipeline, monkeypatch):
    _phases(pipeline, monkeypatch, discovery=False)
    assert asyncio.run(pipeline.run_pipeline(_args(continue_on_error=True))) == "failed"


def test_failed_phase_stops_the_run(pipeline, monkeypatch):
    _phases(pipeline, monkeypatch, discovery=False)
    assert asyncio.run(pipeline.run_pipeline(_args())) == "failed"


def test_a_failed_step_inside_a_phase_is_degraded(pipeline, monkeypatch):
    _phases(pipeline, monkeypatch)
    pipeline.results['deduplication']['errors'].append("check-duplicates: boom")
    assert asyncio.run(pipeline.run_pipeline(_args())) == "degraded"


def test_clean_run_is_ok(pipeline, monkeypatch):
    _phases(pipeline, monkeypatch)
    assert asyncio.run(pipeline.run_pipeline(_args())) == "ok"


def _publication(monkeypatch, builder, allowed=True, reasons=()):
    """Run the real publication step with one fake builder and a fake gate verdict."""
    import cyber_data_collector.publication.outputs as outputs
    import cyber_data_collector.quality.gate as gate

    monkeypatch.setattr(outputs, "DEFAULT_BUILDERS", {"dashboard": builder})

    def fake_checks(conn, thresholds=None, today=None, evaluation_dir=None, snapshot=None, db_path=None):
        from cyber_data_collector.quality.model import load_thresholds
        return gate.QualityReport(run_id="qr-test", created_at="2026-10-29T00:00:00", db_path=str(db_path),
                                  thresholds=load_thresholds(), metrics=[], allowed=allowed,
                                  reasons=list(reasons), snapshot_id=snapshot.snapshot_id,
                                  snapshot_sha256=snapshot.sha256)

    monkeypatch.setattr(gate, "run_quality_checks", fake_checks)
    monkeypatch.setattr(gate, "store_report", lambda conn, report: report.run_id)


def _live_db(tmp_path):
    import sqlite3
    path = tmp_path / "db.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE t (x)")
    return path


def test_invalid_dashboard_is_not_published(pipeline, monkeypatch, tmp_path):
    out = tmp_path / "dashboard"
    out.mkdir()
    (out / "index.html").write_text("<html>last good</html>", encoding="utf-8")
    _live_db(tmp_path)

    def tiny(ctx):
        path = ctx.dashboard_dir / "index.html"
        path.write_text("<html>stub</html>", encoding="utf-8")   # far under 50 KB
        from cyber_data_collector.publication.outputs import PublicationError, validate_dashboard
        problems = validate_dashboard(str(path))
        if problems:
            raise PublicationError("; ".join(problems))
        return [path]

    _publication(monkeypatch, tiny)
    monkeypatch.setattr(rfp, "get_connection", _fake_connection)
    assert pipeline.run_dashboard_phase(_args(out_dir=str(out), risk_matrix_dir=str(tmp_path / "risk"))) is False
    assert (out / "index.html").read_text(encoding="utf-8") == "<html>last good</html>"


def test_quality_gate_blocks_publication(pipeline, monkeypatch, tmp_path):
    out = tmp_path / "dashboard"
    out.mkdir()
    (out / "index.html").write_text("<html>last good</html>", encoding="utf-8")
    _live_db(tmp_path)

    def full(ctx):
        path = ctx.dashboard_dir / "index.html"
        path.write_text("<html>" + "x" * 60_000 + "</html>", encoding="utf-8")
        return [path]

    _publication(monkeypatch, full, allowed=False, reasons=["labels.contradictions_total"])
    monkeypatch.setattr(rfp, "get_connection", _fake_connection)
    assert pipeline.run_dashboard_phase(_args(out_dir=str(out), risk_matrix_dir=str(tmp_path / "risk"))) is False
    assert (out / "index.html").read_text(encoding="utf-8") == "<html>last good</html>"


def test_valid_dashboard_is_published(pipeline, monkeypatch, tmp_path):
    out = tmp_path / "dashboard"
    _live_db(tmp_path)

    def full(ctx):
        path = ctx.dashboard_dir / "index.html"
        path.write_text("<html>" + "x" * 60_000 + "</html>", encoding="utf-8")
        return [path]

    _publication(monkeypatch, full)
    monkeypatch.setattr(rfp, "get_connection", _fake_connection)
    assert pipeline.run_dashboard_phase(_args(out_dir=str(out), risk_matrix_dir=str(tmp_path / "risk"))) is True
    assert (out / "index.html").stat().st_size > 50_000
    assert "data-snapshot" in (out / "index.html").read_text(encoding="utf-8")
    assert (out / "publication.json").is_file()


class _fake_connection:
    """Schema check passes: the required tables exist."""

    def __init__(self, path):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        return SimpleNamespace(fetchall=lambda: [("DeduplicatedEvents",), ("EntitiesV2",),
                                                 ("DeduplicatedEventEntities",)])
