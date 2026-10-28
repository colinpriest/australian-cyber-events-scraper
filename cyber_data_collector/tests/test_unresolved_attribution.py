"""Unresolved victim attributions: kept and counted, but marked; and roles refreshed last.

A pair the calibrated victim-attribution step flagged (recorded victim
probably wrong, no trusted replacement) is still a real incident. Counting,
the by-entity dashboard and the recurrence analysis keep it, mark it and
report the analysis with and without it. All offline.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytest.importorskip("typesafe_sdk")

from cyber_data_collector.dedup import victim_judgement as vj  # noqa: E402
from cyber_data_collector.dedup.victim_counting import (  # noqa: E402
    ATTRIBUTION_UNRESOLVED, unresolved_rows, within_event_duplicates,
)


def _db(path: Path) -> None:
    """ProctorU (two co-equal victims), Medibank under two names, and a later Medibank event."""
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT, employee_count INTEGER,
            turnover TEXT, entity_kind TEXT, size_estimate TEXT, size_confidence REAL, size_employees INTEGER,
            size_revenue_aud REAL, size_basis TEXT, industry TEXT);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, master_enriched_event_id TEXT,
            title TEXT, summary TEXT, description TEXT, event_date TEXT, event_type TEXT, records_affected INTEGER,
            vendor_organization_name TEXT, date_confidence REAL, confidence_score REAL, total_data_sources INTEGER,
            victim_organization_industry TEXT, is_australian_event INTEGER, is_specific_event INTEGER,
            status TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        CREATE TABLE EventDeduplicationMap (deduplicated_event_id TEXT, enriched_event_id TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, summary TEXT, description TEXT);
    """)
    c.executemany("INSERT INTO EntitiesV2 (entity_id, entity_name, entity_kind, size_estimate) VALUES (?,?,?,?)",
                  [(1, "University of Sydney", "organisation", "HUGE"),
                   (2, "University of Tasmania", "organisation", "LARGE"),
                   (3, "Medibank", "organisation", "HUGE"),
                   (4, "Medibank Private Limited", "organisation", None),
                   (5, "ProctorU", "organisation", None)])
    for did, title, when, status in [("p1", "ProctorU breach", "2020-08-07", "Active"),
                                     ("m1", "Medibank breach", "2022-10-13", "Active"),
                                     ("m2", "Medibank follow-up attack", "2023-06-01", "Active"),
                                     ("r1", "Rejected page", "2023-07-01", "Rejected")]:
        c.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, master_enriched_event_id, title, "
                  "event_date, status) VALUES (?, ?, ?, ?, ?)", (did, did + "-m", title, when, status))
    c.executemany("INSERT INTO DeduplicatedEventEntities VALUES (?,?,?)", [
        ("p1", 1, "victim"), ("p1", 2, "victim"), ("p1", 5, "vendor"),
        ("m1", 3, "victim"), ("m1", 4, "victim"),
        ("m2", 3, "victim"),
        ("r1", 1, "victim"),
    ])
    vj.ensure_actions_schema(c)
    c.commit()
    c.close()


def _act(conn, did, entity_id, action, reason="no candidate chosen"):
    conn.execute("INSERT INTO VictimAttributionActions (batch_id, deduplicated_event_id, entity_id, old_victim, "
                 "action, p_recorded_victim, reason, created_at) VALUES ('b', ?, ?, 'x', ?, 0.05, ?, '2026-10-28')",
                 (did, entity_id, action, reason))
    conn.commit()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "attr.db"
    _db(path)
    return path


@pytest.fixture
def conn(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


# ------------------------------------------------------------------ which pairs are unresolved

def test_only_the_latest_flag_on_an_active_victim_is_unresolved(conn):
    _act(conn, "p1", 2, "flagged")                      # unresolved
    _act(conn, "p1", 1, "flagged")
    _act(conn, "p1", 1, "confirmed", "re-judged")       # later confirmed: resolved
    _act(conn, "p1", 5, "flagged")                      # no longer a victim role
    _act(conn, "r1", 1, "flagged")                      # rejected event
    assert set(vj.unresolved_attributions(conn)) == {("p1", 2)}
    assert vj.unresolved_attributions(conn)[("p1", 2)]["reason"] == "no candidate chosen"


def test_no_table_means_nothing_unresolved(tmp_path):
    c = sqlite3.connect(tmp_path / "empty.db")
    c.execute("CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, "
              "relationship_type TEXT)")
    assert vj.unresolved_attributions(c) == {} and vj.latest_actions(c) == {}


def test_a_flagged_variant_dropped_within_the_event_marks_the_kept_name(conn):
    """Medibank Private Limited (flagged) is listed under Medibank on m1, so Medibank's m1 row is unresolved."""
    _act(conn, "m1", 4, "flagged", "replacement below threshold")
    rows = [(3, "Medibank", "m1"), (4, "Medibank Private Limited", "m1"), (3, "Medibank", "m2")]
    drop = within_event_duplicates(conn, rows)
    assert drop == {(4, "m1")}
    assert unresolved_rows(conn, rows, drop) == {(3, "m1"): "replacement below threshold"}


# ------------------------------------------------------------------ dashboard

def test_dashboard_keeps_and_marks_unresolved_rows(conn):
    from scripts.build_entity_dashboard import build_html, load_rows, summarise

    _act(conn, "p1", 2, "flagged")
    rows = load_rows(conn)
    tas = [r for r in rows if r["entity"] == "University of Tasmania"]
    assert len(tas) == 1 and tas[0]["attribution"] == ATTRIBUTION_UNRESOLVED
    assert tas[0]["attribution_reason"] == "no candidate chosen"
    assert all(not r["attribution"] for r in rows if r["entity"] != "University of Tasmania")
    stats = summarise(rows)
    assert stats["unresolved_rows"] == 1 and stats["unresolved_events"] == 1 and stats["events"] == 3
    page = build_html(rows, stats)
    assert 'id="unres"' in page and "unresolved victim attribution" in page


# ------------------------------------------------------------------ recurrence inputs

def test_recurrence_reports_unresolved_attributions_included_and_excluded(db, conn):
    from scripts.analyze_recurrent_timing import (
        DEFAULT_ELAPSED_BOUNDS, apply_attribution_policy, attribution_sensitivity, load_victim_events,
    )

    _act(conn, "m1", 4, "flagged")
    events = load_victim_events(db)
    flagged = events[events["attribution_unresolved"]]
    assert list(zip(flagged["entity_name"], flagged["deduplicated_event_id"])) == [("Medibank", "m1")]

    kept, info = apply_attribution_policy(events, "include")
    assert len(kept) == len(events) == 4
    assert info["victim-event attributions unresolved"] == 1 and info["victim-event attributions analysed"] == 4
    dropped, info = apply_attribution_policy(events, "exclude")
    assert len(dropped) == 3 and info["policy"] == "exclude"
    assert info["events with an unresolved attribution"] == 1
    with pytest.raises(ValueError):
        apply_attribution_policy(events, "ignore")

    sensitivity = attribution_sensitivity(events, date(2024, 1, 1), DEFAULT_ELAPSED_BOUNDS)
    assert sensitivity["included"]["observed repeats"] == 1          # Medibank m1 -> m2
    assert sensitivity["excluded"]["observed repeats"] == 0
    assert sensitivity["repeats involving an unresolved attribution"] == 1


def test_recurrence_without_flags_is_unchanged(db):
    from scripts.analyze_recurrent_timing import apply_attribution_policy, load_victim_events

    events = load_victim_events(db)
    assert not events["attribution_unresolved"].any()
    kept, info = apply_attribution_policy(events, "exclude")
    assert len(kept) == len(events) and info["victim-event attributions unresolved"] == 0


# ------------------------------------------------------------------ roles refreshed after the last merge

def test_pipeline_refreshes_roles_after_the_last_membership_change():
    source = (Path(__file__).resolve().parents[2] / "run_full_pipeline.py").read_text(encoding="utf-8")
    order = [source.index(s) for s in ("self.run_data_hygiene(args)", "self.run_recurrence_check(args)",
                                       "self.run_role_refresh(args)")]
    assert order == sorted(order)
    # ...and again inside hygiene after the steps that merge and split.
    hygiene = source[source.index("def run_data_hygiene"):source.index("def run_recurrence_check")]
    steps = hygiene[hygiene.index("steps = ["):]
    assert steps.index("suppliers") < steps.rindex('["classify-entities", "--stale-only"]') < steps.index(
        '"reconcile-timelines"')


def _role_db(path: Path, outdated: bool) -> None:
    from cyber_data_collector.dedup import schema
    from cyber_data_collector.dedup.role_maintenance import mark_classified
    from cyber_data_collector.tests.test_role_maintenance import BASE_DDL

    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.executescript(BASE_DDL)
    schema.migrate(c)
    c.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, master_enriched_event_id, status) "
              "VALUES ('d1', 'e1', 'Active')")
    c.execute("INSERT INTO EventDeduplicationMap (map_id, enriched_event_id, deduplicated_event_id) "
              "VALUES ('x1', 'e1', 'd1')")
    mark_classified(c, "d1")
    if outdated:   # a merge after classification
        c.execute("INSERT INTO EventDeduplicationMap (map_id, enriched_event_id, deduplicated_event_id) "
                  "VALUES ('x2', 'e2', 'd1')")
    c.commit()
    c.close()


@pytest.mark.parametrize("outdated", [False, True])
def test_role_refresh_runs_its_steps_and_fails_loudly_on_outdated_roles(tmp_path, monkeypatch, outdated):
    import run_full_pipeline as rfp

    path = tmp_path / "roles.db"
    _role_db(path, outdated)
    calls = []
    monkeypatch.setattr(rfp, "run_step", lambda main, argv: calls.append(argv[2:]) or 0)
    pipeline = SimpleNamespace(db_path=str(path), results={"deduplication": {"errors": []}})
    rfp.UnifiedPipeline.run_role_refresh(pipeline, SimpleNamespace())
    assert calls == [["classify-entities", "--stale-only"], ["check-victims"],
                     ["check-victim-attribution", "--apply"]]
    errors = pipeline.results["deduplication"]["errors"]
    assert (any(e.startswith("roles:") for e in errors)) is outdated
    assert pipeline.results["deduplication"]["roles"]["outdated"] == int(outdated)


def test_role_refresh_skips_attribution_with_hygiene_and_reports_only_when_asked(tmp_path, monkeypatch):
    import run_full_pipeline as rfp

    path = tmp_path / "roles.db"
    _role_db(path, False)
    calls = []
    monkeypatch.setattr(rfp, "run_step", lambda main, argv: calls.append(argv[2:]) or 0)
    pipeline = SimpleNamespace(db_path=str(path), results={"deduplication": {"errors": []}})
    rfp.UnifiedPipeline.run_role_refresh(pipeline, SimpleNamespace(skip_data_hygiene=True))
    assert calls == [["classify-entities", "--stale-only"]]
    calls.clear()
    rfp.UnifiedPipeline.run_role_refresh(pipeline, SimpleNamespace(report_event_rejections=True))
    assert calls[-1] == ["check-victim-attribution"]
