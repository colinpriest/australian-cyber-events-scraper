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


def test_invalid_dashboard_is_not_published(pipeline, monkeypatch, tmp_path):
    out = tmp_path / "dashboard"
    out.mkdir()
    (out / "index.html").write_text("<html>last good</html>", encoding="utf-8")
    (tmp_path / "db.sqlite").write_bytes(b"")

    def tiny(db_path, out_dir):
        path = Path(out_dir) / "index.html"
        path.write_text("<html>stub</html>", encoding="utf-8")   # far under 50 KB
        return str(path)

    monkeypatch.setattr(rfp, "build_dashboard_file", tiny)
    monkeypatch.setattr(rfp, "get_connection", _fake_connection)
    assert pipeline.run_dashboard_phase(_args(out_dir=str(out))) is False
    assert (out / "index.html").read_text(encoding="utf-8") == "<html>last good</html>"


def test_valid_dashboard_is_published(pipeline, monkeypatch, tmp_path):
    out = tmp_path / "dashboard"
    (tmp_path / "db.sqlite").write_bytes(b"")

    def full(db_path, out_dir):
        path = Path(out_dir) / "index.html"
        path.write_text("<html>" + "x" * 60_000 + "</html>", encoding="utf-8")
        return str(path)

    monkeypatch.setattr(rfp, "build_dashboard_file", full)
    monkeypatch.setattr(rfp, "get_connection", _fake_connection)
    assert pipeline.run_dashboard_phase(_args(out_dir=str(out))) is True
    assert (out / "index.html").stat().st_size > 50_000


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
