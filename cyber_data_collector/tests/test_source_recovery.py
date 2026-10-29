"""Abandoned discovery searches recover by themselves; unrecognised failures are not "permanent".

Regression for the reviewer finding: a source-month search was marked ``gave_up``
after a fixed number of failures - temporary ones included - and skipped until a
person ran ``discovery_queue.py release``; and an exception the record
classifier did not recognise was treated as the record's own, permanent fault.

Every external service is a fake (the autouse fixture sets bogus keys, so a call
that reached one would fail instead of being made).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from importlib import metadata

import pytest

from cyber_data_collector.processing import fetch_errors as fe
from cyber_data_collector.processing import retry_queue as rq
from cyber_data_collector.processing import source_config
from cyber_data_collector.quality import metrics as qm
from cyber_data_collector.quality.model import load_thresholds
from cyber_data_collector.storage.schema_init import initialize_database
from cyber_data_collector.tests.test_cyber_daily_monitoring import (  # noqa: F401 - switch is a fixture
    _http_error,
    _processed_month,
    switch,
)
from cyber_data_collector.tests.test_cyber_daily_source import env  # noqa: F401 - env is a fixture


@pytest.fixture(autouse=True)
def bogus_keys(monkeypatch):
    for name in ("OPENAI_API_KEY", "PERPLEXITY_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.setenv(name, "bogus-not-a-key")


START = datetime(2026, 10, 1, 9, 0)


class Config:
    """A controllable source configuration: ``components`` change when a key, query or client changes."""

    def __init__(self):
        self.components = {"secret:KEY": "aaaaaaaaaaaa", "query": "1", "client:requests": "2.31.0"}

    def __call__(self, source_type):
        return dict(self.components)


def _queue(clock, config=None):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_type TEXT, event_date TEXT, "
                 "discovered_at TEXT, is_processed INTEGER, processing_error TEXT, processing_attempted_at TEXT, "
                 "raw_title TEXT, raw_description TEXT, raw_content TEXT, source_url TEXT, source_metadata TEXT)")
    config = config or Config()
    return conn, rq.RetryQueue(conn, clock=lambda: clock[0], configuration=config), config


def _fail(queue, clock, error_type, n=rq.MAX_SOURCE_MONTH_ATTEMPTS, source="CyberDaily", year=2021, month=3):
    """``n`` consecutive failed searches, each after the back-off of the previous one."""
    for _ in range(n):
        queue.record_discovery(source, year, month, 0, ["it failed"], error_types=[error_type])
        row = [r for r in queue.failed_discoveries() if (r["source_type"], r["year"], r["month"]) ==
               (source, year, month)][0]
        if row["next_attempt_at"]:
            clock[0] = max(clock[0], datetime.fromisoformat(row["next_attempt_at"]) + timedelta(seconds=1))


# ---------------------------------------------------------------------------
# Failure classes
# ---------------------------------------------------------------------------

class _Weird(Exception):
    pass


@pytest.mark.parametrize("exc,kind", [
    (RuntimeError("boom"), rq.KIND_UNKNOWN),
    (KeyError("missing"), rq.KIND_UNKNOWN),
    (_Weird("nobody has seen this before"), rq.KIND_UNKNOWN),
    (AttributeError("'NoneType' object has no attribute 'x'"), rq.KIND_UNKNOWN),
    (sqlite3.ProgrammingError("Cannot operate on a closed database."), rq.KIND_UNKNOWN),
    (type("AuthenticationError", (Exception,), {"status_code": 401})("bad key"), rq.KIND_UNKNOWN),
    (type("PermissionDeniedError", (Exception,), {"status_code": 403})("blocked"), rq.KIND_UNKNOWN),
    (ValueError("title is empty"), rq.KIND_PERMANENT),
    (sqlite3.IntegrityError("CHECK constraint failed"), rq.KIND_PERMANENT),
    (sqlite3.ProgrammingError("Error binding parameter 3: type 'dict' is not supported"), rq.KIND_PERMANENT),
    (type("BadRequestError", (Exception,), {"status_code": 400})("context length exceeded"), rq.KIND_PERMANENT),
    (type("UnprocessableEntityError", (Exception,), {"status_code": 422})("cannot"), rq.KIND_PERMANENT),
    (asyncio.TimeoutError(), rq.KIND_TEMPORARY),
])
def test_only_recognised_record_faults_are_permanent(exc, kind):
    assert rq.classify_failure(exc) == kind


def test_a_provider_failure_behind_a_validation_error_is_still_temporary():
    try:
        try:
            raise TimeoutError("timed out")
        except TimeoutError as inner:
            raise ValueError("retries exhausted") from inner
    except ValueError as outer:
        assert rq.classify_failure(outer) == rq.KIND_TEMPORARY


def test_an_unrecognised_error_behind_a_recognised_one_keeps_the_record_s_fault():
    try:
        try:
            raise RuntimeError("deep")
        except RuntimeError as inner:
            raise ValueError("model output failed validation") from inner
    except ValueError as outer:
        assert rq.classify_failure(outer) == rq.KIND_PERMANENT


@pytest.mark.parametrize("error_type,failure", [
    (fe.THROTTLED, "temporary"), (fe.NETWORK, "temporary"), (fe.PARSE, "temporary"), (fe.INCOMPLETE, "temporary"),
    (fe.MISSING, "permanent"), (fe.OTHER, "unknown"), (fe.UNKNOWN, "unknown"), ("never-heard-of-it", "unknown"),
    (None, "unknown"),
])
def test_fetch_error_types_have_three_classes(error_type, failure):
    assert fe.failure_class(error_type) == failure


def test_a_message_that_says_nothing_is_unknown_not_other():
    assert fe.error_type_from_text("something nobody anticipated") == fe.UNKNOWN
    assert fe.error_type_from_text("HTTP 403") == fe.OTHER        # a recognised refusal
    assert fe.deciding_type(["not-a-type"]) == fe.UNKNOWN


# ---------------------------------------------------------------------------
# Records: unknown failures wait long and are never quarantined
# ---------------------------------------------------------------------------

def _record_queue(tmp_path):
    conn = sqlite3.connect(tmp_path / "q.db")
    conn.execute("CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_type TEXT, raw_title TEXT, "
                 "raw_description TEXT, raw_content TEXT, event_date TEXT, source_url TEXT, source_metadata TEXT, "
                 "discovered_at TEXT, is_processed INTEGER DEFAULT 0, processing_error TEXT, "
                 "processing_attempted_at TEXT)")
    conn.execute("INSERT INTO RawEvents (raw_event_id, source_type, event_date) VALUES ('r', 'OAIC', '2025-03-14')")
    return conn, rq.RetryQueue(conn, clock=lambda: START)


def test_unknown_record_failures_never_quarantine_and_back_off_for_days(tmp_path, caplog):
    conn, queue = _record_queue(tmp_path)
    gaps = []
    with caplog.at_level(logging.ERROR):
        for _ in range(rq.MAX_PERMANENT_ATTEMPTS + 6):
            assert queue.record_failure("r", "classification", "RuntimeError: boom", rq.KIND_UNKNOWN) == "retry"
            nxt = conn.execute("SELECT next_attempt_at FROM DiscoveryRecordState").fetchone()[0]
            gaps.append(datetime.fromisoformat(nxt) - START)
    assert conn.execute("SELECT is_processed FROM RawEvents").fetchone()[0] == 0
    state = conn.execute("SELECT attempts, unknown_failures, temporary_failures, last_error_kind, quarantined_at "
                         "FROM DiscoveryRecordState").fetchone()
    assert state == (0, rq.MAX_PERMANENT_ATTEMPTS + 6, 0, "unknown", None)
    assert gaps[:3] == [timedelta(hours=6), timedelta(hours=12), timedelta(days=1)]
    assert max(gaps) == rq.UNKNOWN_BACKOFF_CAP == timedelta(days=7)
    assert any("Unrecognised failure" in r.message for r in caplog.records), "each one is logged at ERROR"
    # It is still in the queue: due once its back-off passes.
    assert "r" in {row["raw_event_id"] for row in queue.due(now=START + timedelta(days=8))}


def test_permanent_record_failures_still_quarantine(tmp_path):
    conn, queue = _record_queue(tmp_path)
    outcomes = [queue.record_failure("r", "classification", "invalid", rq.KIND_PERMANENT)
                for _ in range(rq.MAX_PERMANENT_ATTEMPTS)]
    assert outcomes[-1] == "quarantined"


def test_unknown_failures_count_as_failing_in_the_completion_view(tmp_path):
    conn, queue = _record_queue(tmp_path)
    queue.register(["r"], {"r": "OAIC"}, 2025, 3)
    queue.record_failure("r", "classification", "RuntimeError: boom", rq.KIND_UNKNOWN)
    [row] = queue.completion()
    assert row["pending"] == 1 and row["failing"] == 1 and row["quarantined"] == 0


# ---------------------------------------------------------------------------
# Source-months: permanent, parked, probed, reopened
# ---------------------------------------------------------------------------

def test_a_permanent_failure_is_given_up_at_once_with_no_configuration():
    clock = [START]
    _, queue, _ = _queue(clock)
    queue.record_discovery("CyberDaily", 2020, 2, 0, ["HTTP 404"], error_types=[fe.MISSING])
    [row] = queue.failed_discoveries()
    assert row["gave_up_at"] and row["give_up_reason"] == "permanent" and row["give_up_config"] is None
    assert queue.source_month_retry("CyberDaily", 2020, 2) == rq.MONTH_GAVE_UP
    assert [g["month"] for g in queue.given_up_months()] == [2] and queue.parked_months() == []


def test_repeated_temporary_failures_park_the_month_and_store_the_configuration():
    clock = [START]
    _, queue, config = _queue(clock)
    _fail(queue, clock, fe.THROTTLED)
    [row] = queue.failed_discoveries()
    assert row["give_up_reason"] == "temporary" and row["gave_up_at"] and row["next_attempt_at"] is None
    assert queue.source_month_retry("CyberDaily", 2021, 3) == rq.MONTH_PARKED
    assert source_config.decode(row["give_up_config"]) == config.components
    assert "aaaaaaaaaaaa" in row["give_up_config"]            # the hash of the key, as given by the provider
    assert [g["month"] for g in queue.parked_months()] == [3] and queue.given_up_months() == []


def test_unknown_failures_back_off_for_hours_then_days_and_then_park():
    clock = [START]
    _, queue, _ = _queue(clock)
    gaps = []
    for _ in range(rq.MAX_SOURCE_MONTH_ATTEMPTS - 1):
        before = clock[0]
        queue.record_discovery("Perplexity", 2025, 1, 0, ["???"], error_types=[fe.UNKNOWN])
        row = queue.failed_discoveries()[0]
        gaps.append(datetime.fromisoformat(row["next_attempt_at"]) - START if not gaps else
                    datetime.fromisoformat(row["next_attempt_at"]) - before)
        clock[0] = datetime.fromisoformat(row["next_attempt_at"]) + timedelta(seconds=1)
    assert gaps[:3] == [timedelta(hours=6), timedelta(hours=12), timedelta(days=1)] or gaps[0] == timedelta(hours=6)
    queue.record_discovery("Perplexity", 2025, 1, 0, ["???"], error_types=[fe.UNKNOWN])
    assert queue.source_month_retry("Perplexity", 2025, 1) == rq.MONTH_PARKED
    assert queue.failed_discoveries()[0]["give_up_reason"] == "unknown"


def test_a_probe_is_not_due_until_a_week_after_the_first_month_was_parked():
    clock = [START]
    _, queue, _ = _queue(clock)
    _fail(queue, clock, fe.NETWORK, month=3)
    parked_at = clock[0]
    assert queue.probe_due("CyberDaily", now=parked_at + timedelta(days=6, hours=23)) is None
    assert queue.probe_due("CyberDaily", now=parked_at + timedelta(days=7, hours=1)) == (2021, 3)
    assert queue.probe_due("Perplexity", now=parked_at + timedelta(days=30)) is None      # another source


def test_the_probe_is_the_most_recent_parked_month_among_those_asked_about():
    clock = [START]
    _, queue, _ = _queue(clock)
    for month in (3, 4, 5):
        _fail(queue, clock, fe.NETWORK, month=month)
    later = clock[0] + timedelta(days=8)
    assert queue.probe_due("CyberDaily", now=later) == (2021, 5)
    assert queue.probe_due("CyberDaily", months=[(2021, 3), (2021, 4)], now=later) == (2021, 4)


def test_a_successful_probe_reopens_every_parked_month_of_the_source_only():
    clock = [START]
    _, queue, _ = _queue(clock)
    for month in (3, 4):
        _fail(queue, clock, fe.THROTTLED, month=month)
    _fail(queue, clock, fe.THROTTLED, source="Perplexity", month=3)                 # another source stays parked
    queue.record_discovery("CyberDaily", 2020, 2, 0, ["HTTP 404"], error_types=[fe.MISSING])   # stays given up
    later = clock[0] + timedelta(days=8)
    # The probe month completes (as record_discovery records it), then the outcome reopens the rest.
    queue.record_discovery("CyberDaily", 2021, 4, 10, [])
    reopened = queue.record_probe("CyberDaily", True, now=later)
    assert reopened == [(2021, 3)]
    assert queue.source_month_retry("CyberDaily", 2021, 3, now=later) == rq.MONTH_DUE
    assert queue.source_month_retry("Perplexity", 2021, 3, now=later) == rq.MONTH_PARKED
    assert queue.source_month_retry("CyberDaily", 2020, 2, now=later) == rq.MONTH_GAVE_UP
    row = [r for r in queue.failed_discoveries() if (r["source_type"], r["month"]) == ("CyberDaily", 3)][0]
    assert row["reopen_reason"] == "probe succeeded" and row["reopened_at"] and row["failed_attempts"] == 0
    assert queue.probe_due("CyberDaily", now=later + timedelta(days=365)) is None       # nothing left parked


def test_a_failed_probe_doubles_the_wait_up_to_four_weeks_and_keeps_the_month_parked():
    clock = [START]
    _, queue, _ = _queue(clock)
    _fail(queue, clock, fe.THROTTLED)
    now = clock[0] + timedelta(days=8)
    waits = []
    for _ in range(4):
        assert queue.probe_due("CyberDaily", now=now) == (2021, 3)
        queue.record_discovery("CyberDaily", 2021, 3, 0, ["still down"], error_types=[fe.THROTTLED], now=now)
        assert queue.record_probe("CyberDaily", False, error_type=fe.THROTTLED, now=now) == []
        [probe] = [p for p in queue.health(now)["probes"]]
        waits.append(datetime.fromisoformat(probe["next_probe_at"]) - now)
        assert queue.source_month_retry("CyberDaily", 2021, 3, now=now) == rq.MONTH_PARKED
        assert queue.probe_due("CyberDaily", now=now + waits[-1] - timedelta(hours=1)) is None
        now = now + waits[-1]
    assert waits == [timedelta(days=14), timedelta(days=28), timedelta(days=28), timedelta(days=28)]
    assert queue.health(now)["probes"][0]["consecutive_failures"] == 4


def test_a_permanent_give_up_is_never_probed_or_reopened_by_a_configuration_change():
    clock = [START]
    _, queue, config = _queue(clock)
    queue.record_discovery("CyberDaily", 2020, 2, 0, ["HTTP 404"], error_types=[fe.MISSING])
    config.components["secret:KEY"] = "bbbbbbbbbbbb"
    assert queue.probe_due("CyberDaily", now=START + timedelta(days=400)) is None
    assert queue.reopen_changed_config() == []
    assert queue.source_month_retry("CyberDaily", 2020, 2) == rq.MONTH_GAVE_UP
    assert queue.release_source_months() == 1                                    # only a person reopens it
    assert queue.source_month_retry("CyberDaily", 2020, 2) == rq.MONTH_DUE


# ---------------------------------------------------------------------------
# Configuration changes
# ---------------------------------------------------------------------------

def test_a_parked_month_reopens_when_the_source_configuration_changes():
    clock = [START]
    conn, queue, config = _queue(clock)
    _fail(queue, clock, fe.OTHER, source="Perplexity")                     # e.g. a 401 under an expired key
    assert queue.reopen_changed_config() == []                             # same configuration: stays parked
    config.components["secret:KEY"] = "bbbbbbbbbbbb"                       # a new key
    config.components["client:requests"] = "2.32.0"                        # and a newer client
    preview = queue.reopen_changed_config(dry_run=True)
    assert preview == [{"source_type": "Perplexity", "year": 2021, "month": 3,
                        "changed": ["client:requests", "secret:KEY"]}]
    assert queue.source_month_retry("Perplexity", 2021, 3) == rq.MONTH_PARKED          # a dry run changes nothing
    found = queue.reopen_changed_config()
    assert [(e["source_type"], e["month"]) for e in found] == [("Perplexity", 3)]
    assert queue.source_month_retry("Perplexity", 2021, 3) == rq.MONTH_DUE
    row = queue.failed_discoveries()[0]
    assert row["reopen_reason"] == "configuration changed: client:requests, secret:KEY" and row["reopened_at"]
    assert queue.reopen_changed_config() == []                             # idempotent
    # The secret itself is never stored.
    assert "bbbbbbbbbbbb" not in json.dumps(conn.execute("SELECT * FROM SourceMonthDiscovery").fetchall())


def test_a_failure_under_the_new_configuration_records_it_and_does_not_reopen_again():
    clock = [START]
    _, queue, config = _queue(clock)
    _fail(queue, clock, fe.THROTTLED)
    config.components["query"] = "2"
    assert len(queue.reopen_changed_config()) == 1
    _fail(queue, clock, fe.THROTTLED)                                      # fails again under query version 2
    assert source_config.decode(queue.failed_discoveries()[0]["give_up_config"])["query"] == "2"
    assert queue.reopen_changed_config() == []


def test_a_month_parked_before_configurations_were_recorded_is_searched_once_more():
    clock = [START]
    conn, queue, _ = _queue(clock)
    _fail(queue, clock, fe.THROTTLED)
    conn.execute("UPDATE SourceMonthDiscovery SET give_up_config = NULL, give_up_reason = NULL")
    rq.ensure_schema(conn)                                                 # the upgrade assigns the class
    assert conn.execute("SELECT give_up_reason FROM SourceMonthDiscovery").fetchone()[0] == "temporary"
    assert queue.reopen_changed_config()[0]["changed"] == ["(not recorded)"]


def test_the_stored_configuration_names_secrets_by_hash_and_includes_versions(monkeypatch):
    monkeypatch.setattr(metadata, "version", lambda name: "9.9.9")
    env_a = {"PERPLEXITY_API_KEY": "pplx-secret-one"}
    a = source_config.current_components("Perplexity", env_a)
    assert "pplx-secret-one" not in source_config.encode(a)
    assert a["query"] == source_config.QUERY_VERSIONS["Perplexity"] and a["client:openai"] == "9.9.9"
    b = source_config.current_components("Perplexity", {"PERPLEXITY_API_KEY": "pplx-secret-two"})
    assert source_config.changed_components(a, b) == ["secret:PERPLEXITY_API_KEY"]
    unset = source_config.current_components("Perplexity", {})
    assert unset["secret:PERPLEXITY_API_KEY"] == source_config.UNSET
    assert source_config.changed_components(unset, a) == ["secret:PERPLEXITY_API_KEY"]    # adding a key is a change
    assert source_config.current_components("NoSuchSource", {}) == {"source": source_config.UNREGISTERED}


def test_the_default_configuration_reads_dotenv_like_the_pipeline_so_a_report_does_not_see_every_key_unset(
        tmp_path, monkeypatch):
    import os

    (tmp_path / ".env").write_text("PERPLEXITY_API_KEY=pplx-from-dotenv\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {k: v for k, v in os.environ.items() if k != "PERPLEXITY_API_KEY"})
    monkeypatch.setattr(source_config, "_DOTENV_LOADED", False)
    loaded = source_config.current_components("Perplexity")
    assert loaded["secret:PERPLEXITY_API_KEY"] != source_config.UNSET
    assert "pplx-from-dotenv" not in source_config.encode(loaded)
    assert loaded == source_config.current_components("Perplexity", {"PERPLEXITY_API_KEY": "pplx-from-dotenv"})


def test_every_known_source_has_a_configuration():
    assert set(source_config.SOURCE_SECRETS) == set(source_config.QUERY_VERSIONS) == set(source_config.CLIENT_PACKAGES)
    for source in source_config.SOURCES:
        assert "query" in source_config.current_components(source, {})


# ---------------------------------------------------------------------------
# The whole flow through the backfill
# ---------------------------------------------------------------------------

def _run(pipeline, now, *months):
    pipeline.retry_queue.clock = lambda: now
    return asyncio.run(pipeline.backfill_source("CyberDaily", list(months)))


def test_backfill_recovers_parked_months_without_anyone_releasing_them(env, switch):
    first, second = _processed_month(env, 5), _processed_month(env, 6)
    pipeline = env.make()
    switch.failure = _http_error(429)
    now = datetime.now()
    for _ in range(rq.MAX_SOURCE_MONTH_ATTEMPTS):
        now += timedelta(days=2)
        result = _run(pipeline, now, first, second)
        assert sorted(result["failed"]) == sorted([first, second])
    now += timedelta(days=2)
    assert sorted(_run(pipeline, now, first, second)["parked"]) == sorted([first, second])

    calls = switch.calls
    now += timedelta(days=2)
    assert _run(pipeline, now, first, second)["probed"] == []                  # a week has not passed
    assert switch.calls == calls                                              # and nothing was asked of the site

    # A week on, the site still throttles: one month is probed, the other is left alone.
    now += timedelta(days=6)
    result = _run(pipeline, now, first, second)
    assert result["probed"] == [max(first, second)] and sorted(result["parked"]) == sorted([first, second])
    probe_calls = switch.calls - calls
    assert 0 < probe_calls
    assert pipeline.retry_queue.health(now)["probes"][0]["consecutive_failures"] == 1

    # The site recovers. When the (longer) wait is over, the probe succeeds and everything is searched.
    switch.failure = None
    now += timedelta(days=15)
    result = _run(pipeline, now, first, second)
    assert result["probed"] == [max(first, second)]
    assert result["reopened"] == [min(first, second)]
    assert sorted(result["searched"] + result["already_done"]) == sorted([first, second])
    assert result["failed"] == [] and result["parked"] == [] and result["gave_up"] == []
    assert pipeline.retry_queue.parked_months() == []
    rows = [pipeline.retry_queue.discovery_status("CyberDaily", *m) for m in (first, second)]
    pipeline.close()
    assert rows == ["complete", "complete"]


def test_backfill_reopens_on_a_configuration_change_before_any_probe_is_due(env, switch):
    month = _processed_month(env, 5)
    pipeline = env.make()
    config = Config()
    pipeline.retry_queue.configuration = config
    switch.failure = _http_error(403)                                          # e.g. the key was revoked
    now = datetime.now()
    for _ in range(rq.MAX_SOURCE_MONTH_ATTEMPTS):
        now += timedelta(days=8)
        _run(pipeline, now, month)
    assert pipeline.retry_queue.source_month_retry("CyberDaily", *month, now=now) == rq.MONTH_PARKED
    assert pipeline.retry_queue.parked_months()[0]["error_type"] == fe.OTHER

    switch.failure = None
    now += timedelta(days=1)
    assert _run(pipeline, now, month)["parked"] == [month]                     # nothing changed: still parked
    config.components["secret:KEY"] = "cccccccccccc"                           # the key is replaced
    result = _run(pipeline, now, month)
    assert result["reopened"] == [month] and result["searched"] == [month] and result["parked"] == []
    pipeline.close()


def test_backfill_never_probes_a_permanently_given_up_month(env, switch):
    month = _processed_month(env, 5)
    pipeline = env.make()
    switch.failure = _http_error(404)
    now = datetime.now()
    _run(pipeline, now, month)
    switch.failure = None
    calls = switch.calls
    result = _run(pipeline, now + timedelta(days=365), month)
    assert result["gave_up"] == [month] and result["probed"] == [] and switch.calls == calls
    pipeline.close()


def test_force_still_searches_a_parked_month(env, switch):
    month = _processed_month(env, 5)
    pipeline = env.make()
    switch.failure = _http_error(429)
    now = datetime.now()
    for _ in range(rq.MAX_SOURCE_MONTH_ATTEMPTS):
        now += timedelta(days=2)
        _run(pipeline, now, month)
    switch.failure = None
    pipeline.retry_queue.clock = lambda: now
    assert asyncio.run(pipeline.backfill_source("CyberDaily", [month], force=True))["searched"] == [month]
    pipeline.close()


# ---------------------------------------------------------------------------
# Reporting: the status command, the health summary and the quality report
# ---------------------------------------------------------------------------

def test_health_separates_given_up_parked_probes_and_unknown_records(tmp_path):
    clock = [START]
    conn, queue, config = _queue(clock)
    conn.execute("INSERT INTO RawEvents (raw_event_id, source_type, is_processed) VALUES ('u', 'OAIC', 0)")
    queue.record_failure("u", "classification", "RuntimeError: boom", rq.KIND_UNKNOWN, now=START)
    queue.record_discovery("CyberDaily", 2020, 2, 0, ["HTTP 404"], error_types=[fe.MISSING])
    _fail(queue, clock, fe.THROTTLED, month=7)
    config.components["query"] = "2"
    health = queue.health(clock[0] + timedelta(days=30))
    assert [g["month"] for g in health["given_up"]] == [2]
    [parked] = health["parked"]
    assert (parked["month"], parked["reason"], parked["would_reopen"]) == (7, "temporary", ["query"])
    [probe] = health["probes"]
    assert probe["source_type"] == "CyberDaily" and probe["parked"] == 1 and probe["overdue"] is True
    assert health["unknown_records"] == {"count": 1, "by_stage": {"classification": 1}}


def test_status_command_reports_what_is_parked_and_what_would_reopen(tmp_path, capsys):
    from scripts import discovery_queue as cli

    path = tmp_path / "instance" / "t.db"
    initialize_database(str(path))
    conn = sqlite3.connect(path)
    queue = rq.RetryQueue(conn, configuration=Config())
    queue.record_discovery("CyberDaily", 2020, 2, 0, ["HTTP 404"], error_types=[fe.MISSING])
    for _ in range(rq.MAX_SOURCE_MONTH_ATTEMPTS):
        queue.record_discovery("CyberDaily", 2021, 3, 0, ["timed out"], error_types=[fe.NETWORK])
    conn.close()
    assert cli._status(str(path), show_all=False) == 0
    out = capsys.readouterr().out
    assert "Given up on a permanent failure" in out and "CyberDaily" in out and "2020-02" in out
    assert "Parked after repeated temporary/unknown failures" in out and "2021-03" in out
    assert "next probe" in out and "reopens on" in out
    # The default configuration provider reads the real environment, so the stored (test) one differs.
    assert cli._reopen(str(path), dry_run=True) == 0
    assert "Would reopen CyberDaily 2021-03" in capsys.readouterr().out
    assert cli._reopen(str(path), dry_run=False) == 0
    assert "Reopened CyberDaily 2021-03" in capsys.readouterr().out
    conn = sqlite3.connect(path)
    assert rq.RetryQueue(conn).source_month_retry("CyberDaily", 2021, 3) == rq.MONTH_DUE
    assert rq.RetryQueue(conn).source_month_retry("CyberDaily", 2020, 2) == rq.MONTH_GAVE_UP
    conn.close()


def test_quality_report_counts_parked_months_probes_and_unknown_failures(tmp_path):
    path = tmp_path / "instance" / "t.db"
    initialize_database(str(path))
    conn = sqlite3.connect(path)
    clock = [START]
    queue = rq.RetryQueue(conn, clock=lambda: clock[0], configuration=Config())
    conn.execute("INSERT INTO RawEvents (raw_event_id, source_type, is_processed) VALUES ('u', 'OAIC', 0)")
    queue.record_failure("u", "classification", "RuntimeError: boom", rq.KIND_UNKNOWN, now=START)
    queue.record_discovery("CyberDaily", 2020, 2, 0, ["HTTP 404"], error_types=[fe.MISSING])
    _fail(queue, clock, fe.THROTTLED, month=7)
    thresholds = load_thresholds()
    measured = qm.measure_discovery_recovery(conn, thresholds, (clock[0] + timedelta(days=30)).date())
    assert measured["discovery.source_months_given_up"].value == 1
    assert measured["discovery.source_months_parked"].value == 1
    assert measured["discovery.parked_config_changed"].value == 1       # stored under the test config, now the env's
    assert measured["discovery.source_probes_overdue"].value == 1
    assert measured["discovery.records_unknown_failures"].value == 1
    for name in qm.DISCOVERY_RECOVERY_METRICS:
        assert thresholds.rule_for(name) is not None, name
    # Cyber Daily's own blocking measure counts only the permanent give-up; the parked month is its own number.
    cd = qm.measure_cyberdaily(conn, thresholds, (clock[0] + timedelta(days=30)).date())
    assert cd["coverage.cyberdaily.backfill_months_failed"].value == 1
    assert cd["coverage.cyberdaily.backfill_months_parked"].value == 1
    conn.close()


def test_quality_report_says_not_measured_on_a_schema_that_predates_parking():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE SourceMonthDiscovery (source_type TEXT, year INTEGER, month INTEGER)")
    measured = qm.measure_discovery_recovery(conn, load_thresholds(), datetime(2026, 10, 29).date())
    assert all(m.value is None for m in measured.values())
