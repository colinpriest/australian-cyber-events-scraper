"""A Google search that lost pages is incomplete, not complete with fewer results.

A 403 that is not a quota refusal, and an ``error`` object in a 200 body, used
to end the query quietly, so the source-month was recorded as searched in full.
The HTTP calls are fakes.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import pytest

from cyber_data_collector.datasources import google_search
from cyber_data_collector.datasources.google_search import GoogleSearchDataSource
from cyber_data_collector.models.config import DataSourceConfig, DateRange
from cyber_data_collector.processing.fetch_errors import OTHER, failure_class

MARCH_2025 = DateRange(start_date=datetime(2025, 3, 1), end_date=datetime(2025, 3, 31))


class Response:
    def __init__(self, status_code, body):
        self.status_code, self.body = status_code, body
        self.text = str(body)

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError("not reached in these tests")


def _source(monkeypatch, responses):
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(params["start"])
        return responses[len(calls) - 1]

    async def no_sleep(seconds):
        return None

    monkeypatch.setattr(google_search.requests, "get", fake_get)
    monkeypatch.setattr(google_search.asyncio, "sleep", no_sleep)
    source = GoogleSearchDataSource.__new__(GoogleSearchDataSource)
    source.config, source.logger = DataSourceConfig(enabled=True, timeout=5), logging.getLogger("t")
    source.api_key, source.cx_key = "k", "cx"
    return source, calls


def _page(n):
    return Response(200, {"items": [{"title": f"Result {n}", "link": f"https://news.example/{n}", "snippet": "s"}]})


@pytest.fixture(autouse=True)
def _quota_flag():
    GoogleSearchDataSource._daily_quota_exceeded = False
    yield
    GoogleSearchDataSource._daily_quota_exceeded = False


def test_a_403_that_is_not_a_quota_refusal_keeps_the_results_and_is_a_failure(monkeypatch):
    forbidden = Response(403, {"error": {"code": 403, "message": "The caller does not have permission",
                                         "errors": [{"reason": "forbidden"}]}})
    source, calls = _source(monkeypatch, [_page(1), forbidden])
    results = asyncio.run(source._execute_google_search("q", MARCH_2025))
    assert [r["title"] for r in results] == ["Result 1"]
    assert len(calls) == 2 and not GoogleSearchDataSource._daily_quota_exceeded
    assert len(source.collection_failures) == 1 and "HTTP 403" in source.collection_failures[0]
    assert source.collection_error_types == [OTHER] and failure_class(OTHER) == "unknown"


def test_an_error_object_in_a_200_body_is_a_failure_not_the_end_of_the_results(monkeypatch):
    errored = Response(200, {"error": {"code": 500, "message": "Backend Error"}})
    source, calls = _source(monkeypatch, [_page(1), errored])
    results = asyncio.run(source._execute_google_search("q", MARCH_2025))
    assert [r["title"] for r in results] == ["Result 1"]
    assert len(source.collection_failures) == 1 and "Backend Error" in source.collection_failures[0]
    assert source.collection_error_types == ["network"]           # HTTP 500: temporary


def test_a_quota_error_object_still_stops_for_the_day_and_an_empty_page_is_the_end(monkeypatch):
    source, _ = _source(monkeypatch, [Response(200, {"error": {"message": "Daily Limit Exceeded"}})])
    assert asyncio.run(source._execute_google_search("q", MARCH_2025)) is None
    assert GoogleSearchDataSource._daily_quota_exceeded

    GoogleSearchDataSource._daily_quota_exceeded = False
    source, calls = _source(monkeypatch, [_page(1), Response(200, {"searchInformation": {}})])
    assert len(asyncio.run(source._execute_google_search("q", MARCH_2025))) == 1
    assert source.collection_failures == [] and len(calls) == 2
