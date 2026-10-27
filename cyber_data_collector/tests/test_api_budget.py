"""Per-run API request budgets are enforced before a request is sent."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import httpx
import pytest
import requests

from cyber_data_collector.utils import api_budget as ab


@pytest.fixture
def budget(monkeypatch):
    fresh = ab.ApiBudget()
    monkeypatch.setattr(ab, "budget", fresh)
    ab.install()
    return fresh


def _httpx_client(sent):
    def handler(request):
        sent.append(str(request.url))
        return httpx.Response(200, json={})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_requests_within_budget_go_out_and_the_next_is_refused_unsent(budget):
    budget.set_limits({"openai": 2})
    sent = []
    client = _httpx_client(sent)
    client.post("https://api.openai.com/v1/chat/completions")
    client.post("https://api.openai.com/v1/chat/completions")
    with pytest.raises(ab.BudgetExhausted):
        client.post("https://api.openai.com/v1/chat/completions")
    assert len(sent) == 2                        # the refused request never left
    assert budget.exhausted() == {"openai": 1}


def test_other_hosts_and_providers_are_not_counted(budget):
    budget.set_limits({"openai": 1})
    sent = []
    client = _httpx_client(sent)
    client.get("https://news.example/article")
    client.post("https://api.perplexity.ai/chat/completions")
    client.post("https://api.openai.com/v1/x")
    assert budget.used == {"openai": 1, "perplexity": 1, "typesafe": 0}


def test_requests_library_is_guarded_too(budget):
    budget.set_limits({"perplexity": 1})

    class Adapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            response = requests.Response()
            response.status_code = 200
            return response

        def close(self):
            pass

    session = requests.Session()
    session.mount("https://", Adapter())
    session.post("https://api.perplexity.ai/chat/completions")
    with pytest.raises(ab.BudgetExhausted):
        session.post("https://api.perplexity.ai/chat/completions")


def test_concurrent_requests_never_exceed_the_budget(budget):
    budget.set_limits({"typesafe": 25})
    allowed = []

    def worker():
        for _ in range(10):
            try:
                budget.reserve("typesafe")
                allowed.append(1)
            except ab.BudgetExhausted:
                pass

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(allowed) == 25 and budget.used["typesafe"] == 25


def test_limits_from_environment_and_command_line(monkeypatch, budget):
    monkeypatch.setenv("API_BUDGET_OPENAI", "100")
    monkeypatch.setenv("API_BUDGET_PERPLEXITY", "50")
    ab.configure(["perplexity=10"])
    assert ab.budget.limits["openai"] == 100 and ab.budget.limits["perplexity"] == 10


def test_a_refusal_is_recognised_through_wrapping():
    try:
        try:
            raise ab.BudgetExhausted("API budget exhausted for openai (3 requests this run)")
        except ab.BudgetExhausted as inner:
            raise RuntimeError("enrichment failed") from inner
    except RuntimeError as outer:
        assert ab.is_budget_refusal(outer)
    assert not ab.is_budget_refusal(ValueError("timeout"))


def test_a_refused_enrichment_is_not_counted_as_a_failed_attempt():
    from scripts.perplexity_backfill_events import PerplexityBackfillProcessor
    recorded = []
    processor = PerplexityBackfillProcessor.__new__(PerplexityBackfillProcessor)
    processor.dry_run = False
    processor.stats = {"quarantined": 0}
    processor.db = SimpleNamespace(record_enrichment_failure=lambda eid, reason: recorded.append(reason))
    processor._record_failure({"enriched_event_id": "e1", "title": "t"},
                              "request failed: API budget exhausted for perplexity (5 requests this run)")
    processor._record_failure({"enriched_event_id": "e1", "title": "t"}, "request failed: timeout")
    assert recorded == ["request failed: timeout"]


def test_an_exhausted_budget_makes_the_run_degraded(tmp_path, monkeypatch, budget):
    import run_full_pipeline as rfp
    pipeline = rfp.UnifiedPipeline(str(tmp_path / "db.sqlite"))
    assert pipeline.outcome([]) == "ok"
    budget.set_limits({"openai": 1})
    budget.reserve("openai")
    with pytest.raises(ab.BudgetExhausted):
        budget.reserve("openai")
    assert pipeline.outcome([]) == "degraded"
