"""Classifier and entity-extraction timeouts stop the request, and one loop decides what is retried.

``asyncio.wait_for(asyncio.to_thread(...))`` abandoned a timed-out call while its thread and HTTP request kept
running, and a retry then started a second request on top, while instructor (``max_retries=2``) and the OpenAI
SDK (2 by default) retried on their own. These tests run the real stack - instructor over an OpenAI client over
an ``httpx.MockTransport`` - so every HTTP request is counted and none leaves the machine.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

import httpx
import instructor
import openai
import pytest

from cyber_data_collector.models.events import (
    ConfidenceScore,
    CyberEvent,
    CyberEventType,
    EventSeverity,
    EventSource,
)
from cyber_data_collector.processing import fetch_errors
from cyber_data_collector.processing import retry_queue as rq
from cyber_data_collector.processing.entity_extractor import EntityExtractor
from cyber_data_collector.processing.llm_classifier import EventEnhancement, EventEnhancementRequest, LLMClassifier
from cyber_data_collector.utils import llm_retry

ACCEPTED = {"is_cybersecurity_event": True, "is_australian_relevant": True, "event_type": "Data Breach",
            "severity": "Medium", "detailed_description": "Acme Logistics lost customer records."}


def _completion(name: str, arguments: dict) -> dict:
    return {"id": "c1", "object": "chat.completion", "created": 0, "model": "gpt-5.4-mini",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "t1", "type": "function",
                                "function": {"name": name, "arguments": json.dumps(arguments)}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


class Server:
    """The OpenAI API as the client sees it: each request takes the next scripted answer (the last repeats).

    An answer is an ``httpx`` exception (raised as the transport would), an int (an HTTP error status) or a
    completion body."""

    def __init__(self, *answers):
        self.answers, self.requests = list(answers), 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        answer = self.answers[min(self.requests, len(self.answers) - 1)]
        self.requests += 1
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            return httpx.Response(answer, json={"error": {"message": f"status {answer}", "type": "x"}})
        return httpx.Response(200, json=answer)


def _client(server: Server):
    raw = llm_retry.openai_client("sk-test", http_client=httpx.Client(transport=httpx.MockTransport(server)))
    return instructor.from_openai(raw)


def _classifier(server: Server):
    classifier = LLMClassifier(None)
    classifier.client = _client(server)
    classifier.waits = []

    async def sleep(seconds):
        classifier.waits.append(seconds)
    classifier.sleep = sleep
    return classifier


def _request():
    return EventEnhancementRequest(title="Acme Logistics data breach", description="Acme lost records",
                                   entity_names=["Acme Logistics"], raw_data_sources=["snippet"])


def _event() -> CyberEvent:
    return CyberEvent(
        title="Acme Logistics data breach", description="Acme Logistics lost customer records",
        event_type=CyberEventType.DATA_BREACH, severity=EventSeverity.MEDIUM, australian_relevance=True,
        event_date=datetime(2025, 3, 14),
        confidence=ConfidenceScore(overall=0.7, source_reliability=0.7, data_completeness=0.5,
                                   temporal_accuracy=0.5, geographic_accuracy=0.5),
        data_sources=[EventSource(source_id="s1", source_type="OAIC", url="https://example.com/acme",
                                  credibility_score=0.8, relevance_score=0.8)])


TIMEOUT = httpx.ReadTimeout("read timed out")


def test_the_client_times_out_at_the_http_level_and_never_retries_itself():
    client = llm_retry.openai_client("sk-test")
    assert client.max_retries == 0
    assert isinstance(client.timeout, httpx.Timeout) and client.timeout.read == llm_retry.REQUEST_TIMEOUT.read


def test_a_timeout_then_an_answer_makes_two_requests_and_returns_the_answer():
    server = Server(TIMEOUT, _completion("EventEnhancement", ACCEPTED))
    classifier = _classifier(server)
    result = asyncio.run(classifier._invoke_llm(_request()))
    assert isinstance(result, EventEnhancement) and result.is_cybersecurity_event
    assert server.requests == 2 and classifier.waits == [llm_retry.backoff_seconds(1)]


def test_a_request_that_always_times_out_is_tried_a_bounded_number_of_times_and_fails_as_temporary():
    server = Server(TIMEOUT)
    classifier = _classifier(server)
    with pytest.raises(openai.APITimeoutError) as raised:
        asyncio.run(classifier._invoke_llm(_request()))
    # One request per attempt: neither the SDK nor instructor adds requests of its own.
    assert server.requests == llm_retry.MAX_ATTEMPTS
    assert classifier.waits == [llm_retry.backoff_seconds(n) for n in range(1, llm_retry.MAX_ATTEMPTS)]
    assert rq.classify_failure(raised.value) == rq.KIND_TEMPORARY
    assert fetch_errors.classify_error(raised.value) == fetch_errors.NETWORK


def test_a_classify_run_hands_back_a_timed_out_event_flagged_temporary():
    server = Server(TIMEOUT)
    [out] = asyncio.run(_classifier(server).classify_events([_event()]))
    assert out.classification_status == "failed" and out.classification_error_kind == rq.KIND_TEMPORARY
    assert server.requests == llm_retry.MAX_ATTEMPTS


@pytest.mark.parametrize("status, transient", [(429, True), (503, True), (500, True), (400, False), (401, False)])
def test_only_transient_statuses_are_retried(status, transient):
    server = Server(status, _completion("EventEnhancement", ACCEPTED))
    classifier = _classifier(server)
    if transient:
        assert asyncio.run(classifier._invoke_llm(_request())).is_cybersecurity_event
        assert server.requests == 2
    else:
        with pytest.raises(openai.APIStatusError):
            asyncio.run(classifier._invoke_llm(_request()))
        assert server.requests == 1 and classifier.waits == []


def test_an_answer_that_does_not_validate_is_re_asked_once_by_instructor():
    invalid = _completion("EventEnhancement", {"severity": "Medium"})        # required fields missing
    server = Server(invalid, _completion("EventEnhancement", ACCEPTED))
    assert asyncio.run(_classifier(server)._invoke_llm(_request())).is_cybersecurity_event
    assert server.requests == 2


def test_an_answer_that_never_validates_fails_as_a_validation_error_without_transient_retries():
    server = Server(_completion("EventEnhancement", {"severity": "Medium"}))
    classifier = _classifier(server)
    with pytest.raises(Exception) as raised:
        asyncio.run(classifier._invoke_llm(_request()))
    assert not llm_retry.is_transient(raised.value) and classifier.waits == []
    assert server.requests == llm_retry.VALIDATION_ATTEMPTS


def test_entity_extraction_timeouts_are_bounded_and_temporary():
    server = Server(TIMEOUT)
    classifier = _classifier(server)
    [out] = asyncio.run(EntityExtractor(classifier).extract_entities([_event()]))
    assert out.classification_status == "failed" and out.classification_error_kind == rq.KIND_TEMPORARY
    assert server.requests == llm_retry.MAX_ATTEMPTS and len(classifier.waits) == llm_retry.MAX_ATTEMPTS - 1


def test_entity_extraction_recovers_from_one_timeout():
    entities = {"entities": [{"name": "Acme Logistics", "entity_type": "Transportation", "industry_sector": None,
                              "location": "Sydney", "is_australian": True, "confidence": 0.9}]}
    server = Server(TIMEOUT, _completion("ExtractedEntities", entities))
    [out] = asyncio.run(EntityExtractor(_classifier(server)).extract_entities([_event()]))
    assert [e.name for e in out.affected_entities] == ["Acme Logistics"] and server.requests == 2


def test_the_backoff_is_bounded():
    assert llm_retry.backoff_seconds(1) == llm_retry.BACKOFF_BASE_SECONDS
    assert llm_retry.backoff_seconds(30) == llm_retry.BACKOFF_MAX_SECONDS


def test_a_final_failure_is_logged_once_with_the_attempt_count(caplog):
    server = Server(TIMEOUT)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(openai.APITimeoutError):
            asyncio.run(_classifier(server)._invoke_llm(_request()))
    assert any(f"after {llm_retry.MAX_ATTEMPTS} attempt(s)" in r.getMessage() for r in caplog.records)
