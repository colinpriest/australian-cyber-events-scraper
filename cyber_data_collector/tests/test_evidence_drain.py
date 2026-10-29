"""Draining the evidence-recovery queues: throttle-aware Cyber Daily re-fetch, route fall-through, per-route progress.

Every fetch and search is a fake; no network, no paid API.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from cyber_data_collector.dedup import source_gate
from cyber_data_collector.processing import cyberdaily_refetch as cr
from cyber_data_collector.processing import evidence_recovery as er
from cyber_data_collector.processing import fetch_errors as fe
from cyber_data_collector.processing import recovery_progress as rp
from cyber_data_collector.quality import metrics as qm
from cyber_data_collector.quality.gate import render_markdown, run_quality_checks
from cyber_data_collector.quality.model import load_thresholds
from cyber_data_collector.tests.test_cyber_daily_source import (  # noqa: F401 - env is a fixture
    ARTICLE,
    CUT_OFF,
    _stored_story,
    env,
)
from cyber_data_collector.tests.test_evidence_recovery import (  # noqa: F401 - conn is a fixture
    FOUND,
    _event,
    _member,
    conn,
)

CD = "https://www.cyberdaily.au/security/{}-story"


def _story_url(n: int) -> str:
    return CD.format(14000 + n)


def _fetcher(results):
    """A fetch returning ``results`` in order (the last repeats); records every URL it was given."""
    seen = []

    async def fetch(url):
        seen.append(url)
        result = results[min(len(seen) - 1, len(results) - 1)]
        if isinstance(result, BaseException):
            raise result
        return result

    fetch.seen = seen
    return fetch


def _age(conn, days=3):
    """As if the 24-hour rule and every back-off had passed."""
    past = (datetime.now() - timedelta(days=days)).isoformat()
    conn.execute("UPDATE CyberDailyRefetches SET fetched_at = ?, next_attempt_at = CASE WHEN next_attempt_at "
                 "IS NULL THEN NULL ELSE ? END", (past, past))
    conn.commit()


def _stories(env, n):
    ids = [_stored_story(env.path, _story_url(i), CUT_OFF, env.now.date().isoformat()) for i in range(n)]
    return ids


# --- Cyber Daily re-fetch: URLs, throttling, drain -------------------------------------------------

def test_fetch_url_drops_the_query_and_fragment():
    token = "https://www.cyberdaily.au/security/14158-story?utm_source=newsletter&token=abc.def.ghi#top"
    assert cr.fetch_url(token) == "https://www.cyberdaily.au/security/14158-story"
    assert cr.fetch_url("https://www.cyberdaily.au/security/1-x") == "https://www.cyberdaily.au/security/1-x"
    assert cr.fetch_url("not a url") == "not a url"


def test_a_subscribers_token_is_never_fetched_logged_or_recorded(env, caplog):
    url = _story_url(1) + "?utm_source=newsletter&token=SECRET-JWT"
    raw_id = _stored_story(env.path, url, CUT_OFF, env.now.date().isoformat())
    fetch = _fetcher([(ARTICLE, {"content_origin": "page"})])
    c = sqlite3.connect(env.path)
    with caplog.at_level("INFO"):
        report = cr.refetch_stories(c, fetch=fetch, delay_seconds=0)
    assert report.outcomes[cr.UPDATED] == 1
    assert fetch.seen == [_story_url(1)]
    assert "SECRET-JWT" not in caplog.text
    assert c.execute("SELECT url FROM CyberDailyRefetches WHERE raw_event_id = ?", (raw_id,)).fetchone()[0] == \
        _story_url(1)
    # Rows recorded by an earlier version with the token are scrubbed when the schema is ensured.
    c.execute("UPDATE CyberDailyRefetches SET url = ?", (url,))
    c.commit()
    cr.ensure_schema(c)
    assert "SECRET" not in c.execute("SELECT url FROM CyberDailyRefetches").fetchone()[0]
    c.close()


def test_a_throttled_story_is_not_counted_towards_giving_it_up_and_is_retried_soon(env):
    _stories(env, 1)
    c = sqlite3.connect(env.path)
    for _ in range(cr.MAX_REFETCH_FAILURES + 2):
        report = cr.refetch_stories(c, fetch=_fetcher([(None, {"error": "HTTP 429"})]), delay_seconds=0,
                                    stop_after_throttled=0)
        assert report.candidates == 1 and report.throttled == 1
        failures, next_at, gave_up, error_type = c.execute(
            "SELECT failures, next_attempt_at, gave_up_at, error_type FROM CyberDailyRefetches").fetchone()
        assert (failures, gave_up, error_type) == (0, None, fe.THROTTLED)
        assert cr.stored_stories(c, days=90) == []                       # inside the pause
        past = (datetime.now() - timedelta(hours=3)).isoformat()         # the pause is hours, not a day
        c.execute("UPDATE CyberDailyRefetches SET next_attempt_at = ?", (past,))
        c.commit()
        assert len(cr.stored_stories(c, days=90)) == 1                   # fetched an hour ago, still retried
    c.close()


def test_consecutive_throttling_ends_the_run_and_leaves_the_rest_queued(env):
    _stories(env, 6)
    c = sqlite3.connect(env.path)
    fetch = _fetcher([(None, {"error": "HTTP 429"})])
    report = cr.refetch_stories(c, fetch=fetch, delay_seconds=0)
    assert report.stopped_on_throttle and len(fetch.seen) == cr.STOP_AFTER_THROTTLED
    assert report.not_fetched == 6 - cr.STOP_AFTER_THROTTLED
    assert cr.waiting_count(c) == 6                                     # nothing given up, nothing lost
    c.close()


def test_a_success_between_throttles_resets_the_brake(env):
    _stories(env, 5)
    c = sqlite3.connect(env.path)
    throttled, ok = (None, {"error": "HTTP 429"}), (ARTICLE, {"content_origin": "page"})
    fetch = _fetcher([throttled, throttled, ok, throttled, throttled])
    report = cr.refetch_stories(c, fetch=fetch, delay_seconds=0)
    assert not report.stopped_on_throttle and len(fetch.seen) == 5
    c.close()


def test_the_throttle_pause_doubles_for_each_throttled_answer_in_a_row():
    pace = cr._Pace(delay=2.0, stop_after_throttled=5)
    report = cr.RefetchReport()
    assert pace.current_delay() == 2.0
    pace.after(cr.FAILED, fe.THROTTLED, report)
    pace.after(cr.FAILED, fe.THROTTLED, report)
    assert pace.current_delay() == 8.0
    pace.after(cr.UPDATED, None, report)
    assert pace.current_delay() == 2.0


def test_drain_works_through_the_queue_in_batches_and_is_resumable(env):
    ids = _stories(env, 7)
    c = sqlite3.connect(env.path)
    fetch = _fetcher([(ARTICLE, {"content_origin": "page"})])
    report = cr.drain(c, batch_size=3, fetch=fetch, delay_seconds=0, max_minutes=None)
    assert report.ended == cr.DRAINED and report.complete
    assert report.batches == 3 and report.fetched == 7 and report.outcomes[cr.UPDATED] == 7
    assert report.remaining == 0 and len(set(fetch.seen)) == 7
    assert cr.drain(c, batch_size=3, fetch=fetch, delay_seconds=0).fetched == 0       # nothing left; no refetch
    assert len(fetch.seen) == 7
    c.close()
    assert len(ids) == 7


def test_drain_stops_on_throttling_and_resumes_where_it_stopped(env):
    _stories(env, 8)
    c = sqlite3.connect(env.path)
    ok = (ARTICLE, {"content_origin": "page"})
    throttled = (None, {"error": "HTTP 429"})
    first = cr.drain(c, batch_size=4, fetch=_fetcher([ok, ok, throttled, throttled, throttled]), delay_seconds=0)
    assert first.ended == cr.THROTTLED_STOP and not first.complete
    assert first.outcomes[cr.UPDATED] == 2 and first.throttled == 3
    assert first.remaining == 6                                          # 2 fixed; the throttled ones wait
    _age(c, days=0)
    c.execute("UPDATE CyberDailyRefetches SET next_attempt_at = ? WHERE next_attempt_at IS NOT NULL",
              ((datetime.now() - timedelta(hours=1)).isoformat(),))
    c.commit()
    second = cr.drain(c, batch_size=4, fetch=_fetcher([ok]), delay_seconds=0)
    assert second.ended == cr.DRAINED and second.remaining == 0
    assert second.outcomes[cr.UPDATED] == 6
    c.close()


def test_drain_stops_at_its_time_budget(env):
    _stories(env, 4)
    c = sqlite3.connect(env.path)

    async def slow(url):
        await asyncio.sleep(0.05)
        return ARTICLE, {"content_origin": "page"}

    report = cr.drain(c, batch_size=1, fetch=slow, delay_seconds=0, max_minutes=0.0005)   # 30 ms
    assert report.ended == cr.TIME_STOP and 0 < report.fetched < 4 and report.remaining == 4 - report.fetched
    c.close()


def test_drain_can_be_restricted_to_chosen_stories_first(env):
    ids = _stories(env, 5)
    c = sqlite3.connect(env.path)
    fetch = _fetcher([(ARTICLE, {"content_origin": "page"})])
    report = cr.drain(c, batch_size=10, fetch=fetch, delay_seconds=0, only=[ids[3], ids[1]], first=[ids[3]])
    assert report.fetched == 2 and fetch.seen == [_story_url(3), _story_url(1)]
    assert cr.waiting_count(c) == 3
    c.close()


def test_workers_fetch_concurrently_up_to_the_limit(env):
    _stories(env, 6)
    c = sqlite3.connect(env.path)
    state = {"now": 0, "peak": 0}

    async def fetch(url):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.01)
        state["now"] -= 1
        return ARTICLE, {"content_origin": "page"}

    report = cr.drain(c, batch_size=6, fetch=fetch, delay_seconds=0, concurrency=2)
    assert report.fetched == 6 and state["peak"] == 2
    c.close()


# --- the scraper limits requests per host ----------------------------------------------------------

def test_scraper_limits_requests_in_flight_per_host(monkeypatch):
    from cyber_data_collector.utils import entity_scraper

    monkeypatch.delenv("SCRAPER_HOST_CONCURRENCY", raising=False)
    scraper = entity_scraper.PlaywrightScraper()
    flight = {"now": 0, "peak": {}, "by_host": {}}

    async def fake(url, *args, **kwargs):
        host = entity_scraper.host_of(url)
        flight["by_host"][host] = flight["by_host"].get(host, 0) + 1
        flight["peak"][host] = max(flight["peak"].get(host, 0), flight["by_host"][host])
        await asyncio.sleep(0.01)
        flight["by_host"][host] -= 1
        return "text"

    monkeypatch.setattr(scraper, "_fetch_page_text", fake)

    async def run():
        urls = [f"https://www.cyberdaily.au/security/{i}-x" for i in range(8)]
        urls += [f"https://other.example/{i}" for i in range(3)]
        await asyncio.gather(*(scraper.get_page_text(u) for u in urls))

    asyncio.run(run())
    assert flight["peak"]["cyberdaily.au"] == entity_scraper.DEFAULT_HOST_CONCURRENCY == 2
    assert flight["peak"]["other.example"] == 2                         # its own slots, not cyberdaily's
    monkeypatch.setenv("SCRAPER_HOST_CONCURRENCY", "1")
    assert entity_scraper.host_concurrency() == 1
    monkeypatch.setenv("SCRAPER_HOST_CONCURRENCY", "nonsense")
    assert entity_scraper.host_concurrency() == entity_scraper.DEFAULT_HOST_CONCURRENCY


# --- routes fall through: a given-up story hands over to research, then a replacement search ---------

def _cyberdaily_incident(c, research_exhausted=True):
    _event(c, "ev-cd", "Theta Corp", [_member(
        "cdgone", source="CyberDaily", url="https://www.cyberdaily.au/security/12345-theta-hacked",
        p_status="quarantined" if research_exhausted else None, attempts=3 if research_exhausted else 0)])
    cr.ensure_schema(c)
    c.commit()


def test_a_story_gone_from_the_site_is_out_of_reach_and_the_next_routes_take_over(conn):
    _cyberdaily_incident(conn)
    conn.execute("INSERT INTO CyberDailyRefetches (raw_event_id, url, status, failures, fetched_at, error_type, "
                 "gave_up_at, last_error) VALUES ('cdgone', 'u', 'missing', 1, '2026-10-28T10:00:00', 'missing', "
                 "'2026-10-28T10:00:00', 'HTTP 404')")
    conn.commit()
    [ev] = er.assess_events(conn, ["ev-cd"], labels_path=None)
    member = ev.members[0]
    # One failure only - the old "3 failures" test called this still pending forever.
    assert member.page_route.state == er.ROUTE_EXHAUSTED and "404" in member.page_route.note
    assert member.research_route.state == er.ROUTE_EXHAUSTED
    assert ev.replacement_route.state == er.ROUTE_NEEDED and ev.recovery == er.ROUTE_NEEDED

    searched = []
    report = er.recover(conn, search=lambda q, m: searched.append(q) or FOUND, labels_path=None)
    assert len(searched) == 1 and "Theta Corp" in searched[0]
    # The replacement is a record of its own, with its own URL; the original page text is untouched.
    url, meta = conn.execute("SELECT source_url, source_metadata FROM RawEvents "
                             "WHERE source_metadata LIKE '%evidence_for%'").fetchone()
    assert url == "https://news.example/report" and json.loads(meta)["evidence_for"] == "ev-cd"
    assert conn.execute("SELECT raw_content FROM RawEvents WHERE raw_event_id = 'cdgone'").fetchone()[0] is None
    assert report.by_recovery == {er.ROUTE_PENDING: 1}


def test_a_story_given_up_after_repeated_failures_is_exhausted_at_the_refetchs_own_cap(conn):
    _cyberdaily_incident(conn, research_exhausted=False)
    conn.execute("INSERT INTO CyberDailyRefetches (raw_event_id, url, status, failures, fetched_at, error_type, "
                 "last_error) VALUES ('cdgone', 'u', 'failed', 3, '2026-10-28T10:00:00', 'network', 'timed out')")
    conn.commit()
    [ev] = er.assess_events(conn, ["ev-cd"], labels_path=None)
    # Three failures of five is still being retried (the old module called it exhausted).
    assert ev.members[0].page_route.state == er.ROUTE_PENDING
    conn.execute("UPDATE CyberDailyRefetches SET failures = 5, gave_up_at = '2026-10-28T11:00:00'")
    conn.commit()
    [ev] = er.assess_events(conn, ["ev-cd"], labels_path=None)
    assert ev.members[0].page_route.state == er.ROUTE_EXHAUSTED and "5 failure" in ev.members[0].page_route.note
    # Research is still waiting, so no paid replacement search yet.
    assert ev.members[0].research_route.state == er.ROUTE_PENDING and ev.replacement_route.state == er.ROUTE_DONE


# --- the drain over incidents ---------------------------------------------------------------------

class FakeSteps:
    """Steps that move the database as the real ones would, and record what was asked of them."""

    def __init__(self, conn, throttle=False):
        self.conn, self.throttle = conn, throttle
        self.refetched, self.rescraped, self.researched = [], 0, []
        self.search_calls = []

    def refetch(self, conn, ids):
        self.refetched.append(list(ids))
        if self.throttle:
            return cr.DrainReport(ended=cr.THROTTLED_STOP)
        for raw_id in ids:
            conn.execute("UPDATE RawEvents SET raw_content = ? WHERE raw_event_id = ?", (ARTICLE, raw_id))
        conn.commit()
        return cr.DrainReport(fetched=len(ids))

    def rescrape(self, conn):
        self.rescraped += 1
        for (raw_id,) in conn.execute("SELECT raw_event_id FROM PageRescrapes WHERE status = 'queued'").fetchall():
            conn.execute("UPDATE RawEvents SET raw_content = ? WHERE raw_event_id = ?", (ARTICLE, raw_id))
            conn.execute("UPDATE PageRescrapes SET status = 'stored' WHERE raw_event_id = ?", (raw_id,))
        conn.commit()
        return 1

    def research(self, conn, limit):
        self.researched.append(limit)
        conn.execute("UPDATE EnrichedEvents SET perplexity_validated = 1, "
                     "perplexity_enrichment_data = '{\"formal_entity_name\": \"X\"}' "
                     "WHERE enriched_event_id IN (SELECT enriched_event_id FROM EnrichedEvents "
                     "WHERE COALESCE(perplexity_validated, 0) = 0 LIMIT ?)", (limit,))
        conn.commit()
        return limit

    def search(self, query, model):
        self.search_calls.append(query)
        return FOUND

    def steps(self):
        return er.Steps(refetch=self.refetch, rescrape=self.rescrape, research=self.research, search=self.search)


@pytest.fixture
def incidents(conn):
    _event(conn, "ev-cd", "Theta Corp", [_member(
        "cd1", source="CyberDaily", url="https://www.cyberdaily.au/security/12345-theta-hacked")])
    _event(conn, "ev-web", "Iota Ltd", [_member("web1", source="WebberInsurance")], "2024-05-01")
    cr.ensure_schema(conn)
    return conn


def test_drain_runs_rounds_until_nothing_more_can_be_retrieved(incidents):
    fake = FakeSteps(incidents)
    report = er.drain(incidents, fake.steps(), labels_path=None)
    assert report.ended == er.DRAINED and report.complete
    assert fake.refetched and fake.refetched[0] == ["cd1"]
    assert fake.rescraped >= 1 and fake.researched
    assert report.counts[er.SUPPORTED] == 2 and report.counts[er.UNSUPPORTED] == 0
    assert all(s == "supported" for (s,) in incidents.execute("SELECT evidence_status FROM DeduplicatedEvents"))
    assert "round 1" in report.summary()


def test_drain_stops_when_the_site_throttles(incidents):
    fake = FakeSteps(incidents, throttle=True)
    report = er.drain(incidents, fake.steps(), labels_path=None)
    assert report.ended == er.THROTTLED and not report.complete and not fake.rescraped
    assert len(report.rounds) == 1
    assert "throttling" in report.detail


def test_drain_stops_when_the_api_budget_is_spent(incidents, monkeypatch):
    from cyber_data_collector.utils.api_budget import budget

    fake = FakeSteps(incidents)
    original = fake.research

    def research(conn, limit):
        budget.refused["perplexity"] += 1                                # a request was refused
        return original(conn, 0)

    fake.research = research
    try:
        report = er.drain(incidents, fake.steps(), labels_path=None)
    finally:
        budget.reset()
    assert report.ended == er.BUDGET and "perplexity" in report.detail


def test_drain_stalls_rather_than_looping_when_nothing_changes(incidents):
    fake = FakeSteps(incidents)
    fake.research = lambda conn, limit: 0                                # research never validates anything
    fake.rescrape = lambda conn: 0
    report = er.drain(incidents, fake.steps(), labels_path=None, max_rounds=10)
    assert report.ended in (er.STALLED, er.DRAINED) and len(report.rounds) < 10


def test_drain_reports_a_missing_search_key_and_still_marks_statuses(conn, monkeypatch):
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    _event(conn, "ev-dead", "Zeta Health", [_member("dead", url="", p_status="quarantined", attempts=3)])
    fake = FakeSteps(conn)
    steps = er.Steps(refetch=fake.refetch, rescrape=fake.rescrape, research=fake.research, search=None)
    report = er.drain(conn, steps, labels_path=None)
    assert report.ended == er.BLOCKED and "PERPLEXITY_API_KEY" in report.detail
    assert conn.execute("SELECT evidence_status FROM DeduplicatedEvents").fetchone()[0] == "unsupported"


# --- per-route progress and the quality report -----------------------------------------------------

def test_route_progress_counts_every_route(env):
    # Cyber Daily: 3 queued, 1 retrying (network), 1 given up, 1 fixed.
    ids = [_stored_story(env.path, _story_url(i), CUT_OFF, env.now.date().isoformat()) for i in range(6)]
    c = sqlite3.connect(env.path)
    cr.ensure_schema(c)
    now = datetime.now().isoformat()
    c.execute("INSERT INTO CyberDailyRefetches (raw_event_id, url, status, failures, fetched_at, error_type, "
              "next_attempt_at) VALUES (?, 'u', 'failed', 1, ?, 'network', ?)", (ids[3], now, now))
    c.execute("INSERT INTO CyberDailyRefetches (raw_event_id, url, status, failures, fetched_at, error_type, "
              "gave_up_at) VALUES (?, 'u', 'missing', 1, ?, 'missing', ?)", (ids[4], now, now))
    c.execute("UPDATE RawEvents SET raw_content = ? WHERE raw_event_id = ?", (ARTICLE, ids[5]))
    c.execute("INSERT INTO CyberDailyRefetches (raw_event_id, url, status, failures, fetched_at) "
              "VALUES (?, 'u', 'updated', 0, ?)", (ids[5], now))
    c.commit()
    progress = rp.measure(c, datetime.now())
    cd = progress[rp.CYBERDAILY_REFETCH]
    assert (cd.queued, cd.succeeded, cd.exhausted, cd.failing_by_type) == (3, 1, 1, {fe.NETWORK: 1})
    assert cd.attempted == 3 and cd.total == 6
    assert progress[rp.PAGE_RESCRAPE].total == 0
    c.close()


def test_route_progress_for_the_other_routes(conn):
    from cyber_data_collector.processing import page_rescrape as pr

    _event(conn, "ev-a", "A", [_member("a")])
    _event(conn, "ev-b", "B", [_member("b")], "2024-05-01")
    pr.ensure_schema(conn)
    for raw_id, status, error in (("a", "queued", None), ("b", "gave_up", "HTTP 404")):
        conn.execute("INSERT INTO PageRescrapes (raw_event_id, url, attempts, status, last_error, last_attempt_at) "
                     "VALUES (?, 'u', 3, ?, ?, '2026-10-01T00:00:00')", (raw_id, status, error))
    source_gate.ensure_replacement_schema(conn)
    conn.execute("INSERT INTO SourceReplacements (enriched_event_id, query, model, created_at, status, purpose, "
                 "attempts, last_error) VALUES ('e-a', 'q', 'm', '2026-10-02T00:00:00', 'pending', ?, 1, "
                 "'HTTP 429')", (er.PURPOSE,))
    conn.commit()
    out = rp.measure(conn, datetime(2026, 10, 29))
    pages = out[rp.PAGE_RESCRAPE]
    assert (pages.queued, pages.exhausted) == (1, 1) and pages.oldest_queued_days == 28.0
    assert out[rp.REPLACEMENT_SEARCH].failing_by_type == {fe.THROTTLED: 1}
    research = out[rp.RESEARCH]
    assert research.queued == 2 and research.succeeded == 0


def test_a_route_that_cannot_be_read_is_not_measured_not_empty(tmp_path):
    path = tmp_path / "bare.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE RawEvents (raw_event_id TEXT)")
    c.commit()
    found = qm.measure_recovery_routes(c, load_thresholds(), date(2026, 10, 29))
    gone = found["recovery.route.page_rescrape.queued"]
    assert gone.not_measured and "PageRescrapes" in gone.not_measured
    c.close()


def test_quality_metrics_report_each_route_and_the_trend_table(conn):
    from cyber_data_collector.processing import page_rescrape as pr

    _event(conn, "ev-a", "A", [_member("a")])
    pr.ensure_schema(conn)
    cr.ensure_schema(conn)
    source_gate.ensure_replacement_schema(conn)
    conn.execute("INSERT INTO PageRescrapes (raw_event_id, url, attempts, status, last_error, last_attempt_at) "
                 "VALUES ('a', 'u', 1, 'retry', 'HTTP 503', '2026-10-01T00:00:00')")
    conn.commit()
    found = qm.measure_recovery_routes(conn, load_thresholds(), date(2026, 10, 29))
    assert found["recovery.route.page_rescrape.failing"].value == 1
    assert found["recovery.route.page_rescrape.failing.throttled"].value == 1
    assert found["recovery.route.cyberdaily_refetch.oldest_queued_days"].value == 0.0
    report = run_quality_checks(conn, evaluation_dir=None)
    names = {m.name: m for m in report.metrics}
    assert names["recovery.route.page_rescrape.failing"].status == "info"
    assert names["recovery.route.page_rescrape.oldest_queued_days"].status == "pass"
    text = render_markdown(report)
    assert "Evidence recovery routes trend" in text and "1 failing" in text
