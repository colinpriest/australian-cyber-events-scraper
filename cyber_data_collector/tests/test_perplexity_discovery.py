"""Perplexity discovery: answers read per incident object, incomplete answers not taken as complete,
one record per cited article.

Every call to Perplexity is a fake client; nothing leaves the machine.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from types import SimpleNamespace
from typing import Callable, Optional, Tuple

from cyber_data_collector.datasources import perplexity as pp
from cyber_data_collector.datasources.perplexity import (
    PerplexityDataSource,
    PerplexityEvent,
    PerplexitySearchResults,
)
from cyber_data_collector.models.config import DataSourceConfig, DateRange
from cyber_data_collector.processing.fetch_errors import INCOMPLETE, PARSE
# The discovery fixture, shared with the retry-queue tests.
from cyber_data_collector.tests.test_discovery_retry_queue import env  # noqa: F401

MARCH_2025 = DateRange(start_date=datetime(2025, 3, 1), end_date=datetime(2025, 3, 31))


class NoWait:
    async def wait(self, service):
        return None


class FakeCompletions:
    """``chat.completions.create``: ``respond(messages) -> (content, finish_reason)``."""

    def __init__(self, respond: Callable[[list], Tuple[Optional[str], str]]):
        self.respond, self.calls = respond, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content, finish_reason = self.respond(kwargs["messages"])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20))


def _source(respond) -> Tuple[PerplexityDataSource, FakeCompletions]:
    source = PerplexityDataSource(DataSourceConfig(name="Perplexity"), NoWait(), {})
    completions = FakeCompletions(respond)
    source.openai_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return source, completions


def _window(messages) -> Tuple[datetime, datetime]:
    text = messages[-1]["content"]
    after, before = re.search(r"after:(\S+) before:(\S+)", text).groups()
    return datetime.strptime(after, "%m/%d/%Y"), datetime.strptime(before, "%m/%d/%Y")


def _answer(*events) -> str:
    return json.dumps({"events": list(events)})


def _incident(title, url, event_date=None, entity=None):
    return {"title": title, "description": f"{title} description", "event_date": event_date,
            "entity_name": entity, "event_type": "Data Breach", "impact_description": None,
            "source_urls": [url]}


# ---------------------------------------------------------------------------
# Item 1: fields are read from inside their own incident object
# ---------------------------------------------------------------------------

# The first object has no event_date or entity_name; the second has both. The old reader paired the
# i-th title with the i-th date, so the second incident's date and victim went to the first.
MALFORMED = '''```json
{"events": [
  {"title": "Acme Logistics ransomware", "description": "Acme says "systems encrypted"", "event_type": "Ransomware",
   "source_urls": ["https://news.example/acme"]},
  {"title": "Bravo Health breach", "description": "Bravo notified patients", "event_date": "2025-03-14",
   "entity_name": "Bravo Health", "source_urls": ["https://news.example/bravo"]},
  {"title": "Charlie Council outage", "description": "The council's systems were cut off by an att'''


def test_fields_are_never_paired_across_incidents_by_position():
    recovered = pp.recover_event_objects(MALFORMED.split("\n", 1)[1])
    by_title = {r["title"]: r for r in recovered}
    assert list(by_title) == ["Acme Logistics ransomware", "Bravo Health breach", "Charlie Council outage"]
    acme, bravo, charlie = by_title.values()
    # Acme's object is closed but malformed (an unescaped quote): its fields up to the bad value only.
    assert acme["recovery"] == pp.RECOVERY_OBJECT and "event_date" not in acme and "entity_name" not in acme
    assert bravo == {"title": "Bravo Health breach", "description": "Bravo notified patients",
                     "event_date": "2025-03-14", "entity_name": "Bravo Health",
                     "source_urls": ["https://news.example/bravo"], "recovery": pp.RECOVERY_OBJECT}
    # The cut-off final object keeps its complete title and drops the cut-off description.
    assert charlie["recovery"] == pp.RECOVERY_TRUNCATED_OBJECT and "description" not in charlie


def test_a_cut_off_value_is_never_kept():
    objects = pp.scan_event_objects('{"events": [{"title": "A {not a brace}", "records": 1200}, '
                                    '{"title": "B", "records": 12')
    assert [closed for _, closed in objects] == [True, False]
    assert pp.read_event_object(*objects[0]) == {"title": "A {not a brace}", "records": 1200}
    assert pp.read_event_object(*objects[1]) == {"title": "B"}                 # 12 may be 120000, cut
    assert pp.read_event_object('{"title": "C", "event_date": null, "source_urls": ["https://x.example/a", "ht',
                                False) == {"title": "C", "event_date": None}
    assert pp.scan_event_objects("no JSON here") == []


def test_both_recovery_tiers_can_record_token_use():
    # They used `tracker` without importing it; the NameError was swallowed and both always returned None.
    source, completions = _source(lambda m: (_answer(_incident("Acme breach", "https://news.example/a")), "stop"))
    retried = asyncio.run(source._retry_strict_json("q", MARCH_2025))
    assert retried is not None and not retried.incomplete and retried.results.events[0].title == "Acme breach"
    completed = asyncio.run(source._complete_partial_events([{"title": "Acme breach"}], MARCH_2025))
    assert completed is not None and completed.events[0].source_urls == ["https://news.example/a"]
    assert len(completions.calls) == 2


def test_a_malformed_answer_is_recovered_and_the_search_is_incomplete():
    answers = iter([(MALFORMED, "stop"), ("not json either", "stop"), ("nor this", "stop")])
    source, completions = _source(lambda m: next(answers))
    outcome = asyncio.run(source._search("q", MARCH_2025))
    assert [e.title for e in outcome.results.events] == [
        "Acme Logistics ransomware", "Bravo Health breach", "Charlie Council outage"]
    assert outcome.incomplete and not outcome.truncated and outcome.error_type == INCOMPLETE
    bravo = outcome.results.events[1]
    assert (bravo.event_date, bravo.entity_name, bravo.recovery) == ("2025-03-14", "Bravo Health", "object")
    assert outcome.results.events[0].event_date is None
    # Strict retry, then a completion call for the cut-off object that lost its citations.
    assert len(completions.calls) == 3


# ---------------------------------------------------------------------------
# Item 3b: incomplete is not empty
# ---------------------------------------------------------------------------

def test_a_clean_empty_answer_is_complete_and_an_empty_content_is_not():
    source, _ = _source(lambda m: ('{"events": []}', "stop"))
    outcome = asyncio.run(source._search("q", MARCH_2025))
    assert outcome.incomplete is None and outcome.results.events == []

    source, _ = _source(lambda m: ("", "stop"))
    outcome = asyncio.run(source._search("q", MARCH_2025))
    assert outcome.incomplete and "empty answer" in outcome.incomplete

    source, _ = _source(lambda m: ("not JSON", "stop"))
    outcome = asyncio.run(source._search("q", MARCH_2025))
    assert outcome.incomplete and outcome.error_type == PARSE and outcome.results.events == []


def test_a_repaired_truncated_answer_is_kept_but_not_complete():
    whole = _answer(_incident("Acme breach", "https://news.example/a"), _incident("Bravo breach",
                                                                                 "https://news.example/b"))
    cut = whole[:whole.index("Bravo") + 3]
    source, completions = _source(lambda m: (cut, "length"))
    outcome = asyncio.run(source._search("q", MARCH_2025))
    assert outcome.truncated and outcome.incomplete
    assert [(e.title, e.recovery) for e in outcome.results.events] == [("Acme breach", "repaired")]
    assert len(completions.calls) == 1                     # no strict retry of an answer cut at the limit


def test_a_truncated_window_is_searched_again_in_halves():
    def respond(messages):
        start, end = _window(messages)
        if (end - start).days > 60:                       # the whole 3-month window: cut off
            return _answer(_incident("Acme breach", "https://news.example/a"))[:-5], "length"
        return _answer(_incident(f"Incident from {start:%m-%d}", f"https://news.example/{start:%m%d}")), "stop"

    source, completions = _source(respond)
    source._base_queries = lambda: ["Australian cyber attack {date_range}"]
    events = asyncio.run(source.collect_events(MARCH_2025))
    assert sorted(e.title for e in events) == ["Incident from 01-01", "Incident from 02-14"]
    assert source.collection_failures == []                # the halves answered completely
    assert len(completions.calls) == 3
    queries = {e.source_provenance["query"] for e in events}
    assert queries == {"Australian cyber attack after:01/01/2025 before:02/14/2025",
                       "Australian cyber attack after:02/14/2025 before:03/31/2025"}


def test_a_window_still_truncated_at_the_smallest_split_is_kept_and_recorded_incomplete():
    def respond(messages):
        start, _ = _window(messages)
        whole = _answer(_incident(f"Incident from {start:%m-%d}", f"https://news.example/{start:%m%d}"),
                        _incident("Cut", "https://news.example/cut"))
        return whole[:whole.index('"Cut"')], "length"

    source, completions = _source(respond)
    source._base_queries = lambda: ["Australian cyber attack {date_range}"]
    events = asyncio.run(source.collect_events(MARCH_2025))
    leaves = len(source.collection_failures)
    assert leaves == len(events) and leaves >= 16           # 89 days -> 44 -> 22 -> 11 -> 5 (not split again)
    assert set(source.collection_error_types) == {INCOMPLETE}
    assert all("incomplete" in f for f in source.collection_failures)
    assert len(completions.calls) == 2 * leaves - 1


def test_a_half_that_fails_keeps_the_truncated_answer_and_is_noted():
    def respond(messages):
        start, end = _window(messages)
        if (end - start).days > 60:
            whole = _answer(_incident("Acme breach", "https://news.example/a"), _incident("Cut", "https://x.example/c"))
            return whole[:whole.index('"Cut"')], "length"
        if start.month == 2:
            raise ValueError("400 Bad Request")               # a client error is not retried
        return _answer(_incident("January incident", "https://news.example/jan")), "stop"

    source, _ = _source(respond)
    source._base_queries = lambda: ["Australian cyber attack {date_range}"]
    events = asyncio.run(source.collect_events(MARCH_2025))
    assert sorted(e.title for e in events) == ["Acme breach", "January incident"]
    assert len(source.collection_failures) == 1 and "half of a truncated window" in source.collection_failures[0]


# ---------------------------------------------------------------------------
# Item 2 / item 1: one record per cited article, with the answer's fields
# ---------------------------------------------------------------------------

def _item(**overrides):
    fields = dict(title="Quality Rugs & Furniture data breach", description="d", event_date="2025-03-14",
                  entity_name="Quality Rugs & Furniture", event_type="Data Breach", impact_description=None,
                  source_urls=["https://news.example/quality-rugs-breach", "https://cybertrackerau.com/",
                               "https://other.example/rugs?utm_source=x", "https://news.example/quality-rugs-breach"])
    fields.update(overrides)
    return PerplexityEvent(**fields)


def test_each_cited_article_becomes_its_own_record_with_shared_provenance():
    source, _ = _source(lambda m: (None, "stop"))
    events = source._convert_results_to_events(PerplexitySearchResults(events=[_item()]), query="the query")
    assert [e.data_sources[0].url for e in events] == ["https://news.example/quality-rugs-breach",
                                                       "https://other.example/rugs"]
    first, second = (e.source_provenance for e in events)
    assert first["answer_item_id"] == second["answer_item_id"] == pp.answer_item_id(_item())
    assert (first["url_index"], second["url_index"], first["url_count"]) == (0, 1, 2)
    assert first["sibling_urls"] == ["https://news.example/quality-rugs-breach", "https://other.example/rugs"]
    assert first["skipped_urls"] == ["https://cybertrackerau.com/"]
    assert first["query"] == "the query" and first["origin"] == pp.PROVENANCE_ORIGIN
    assert first["answer"]["entity_name"] == "Quality Rugs & Furniture"
    for event in events:
        assert event.event_date == datetime(2025, 3, 14)
        assert (event.event_date_precision, event.event_date_basis) == (None, pp.BASIS_PERPLEXITY_ANSWER)
        assert event.primary_entity.name == "Quality Rugs & Furniture"
        assert event.external_ids["perplexity_answer_item"] == first["answer_item_id"]


def test_an_incident_citing_no_article_is_one_record_without_a_url_and_a_partial_date_is_not_a_day():
    source, _ = _source(lambda m: (None, "stop"))
    [event] = source._convert_results_to_events(PerplexitySearchResults(events=[
        _item(source_urls=["https://site.example"], event_date="2025-03", entity_name=None)]))
    assert event.data_sources[0].url is None and event.data_sources[0].source_type == "Perplexity Result"
    assert event.source_provenance["url_index"] is None and event.source_provenance["url_count"] == 0
    assert event.event_date is None and event.event_date_basis is None
    assert event.source_provenance["answer"]["event_date"] == "2025-03"
    assert event.primary_entity is None
    future = source._convert_results_to_events(PerplexitySearchResults(events=[_item(event_date="2999-01-01")]))
    assert all(e.event_date is None for e in future)


def test_children_are_stored_as_separate_records_and_found_again_per_child(env):
    from cyber_data_collector.tests.test_discovery_retry_queue import FakeCollector, _count, _raw

    def respond(messages):
        return _answer({**_incident("Acme Logistics data breach", "https://news.example/acme",
                                    event_date="2025-03-14", entity="Acme Logistics"),
                        "source_urls": ["https://news.example/acme", "https://other.example/acme-breach",
                                        "https://homepage.example/"]}), "stop"

    def make_source():
        source, _ = _source(respond)
        source.validate_config = lambda: True
        source._base_queries = lambda: ["Australian cyber attack {date_range}"]
        return source

    FakeCollector.sources = {"perplexity": make_source()}
    asyncio.run(env.make().discover_events(source_types=["Perplexity"], date_range_days=1))
    rows = _raw(env.path)
    assert sorted(r["source_url"] for r in rows) == ["https://news.example/acme",
                                                     "https://other.example/acme-breach"]
    assert {r["source_type"] for r in rows} == {"Perplexity"}
    assert all(r["event_date"] == "2025-03-14" and r["event_date_precision"] is None
               and r["event_date_basis"] == pp.BASIS_PERPLEXITY_ANSWER for r in rows)
    provenance = [json.loads(r["source_metadata"])["provenance"] for r in rows]
    assert len({p["answer_item_id"] for p in provenance}) == 1
    assert all(p["skipped_urls"] == ["https://homepage.example/"] for p in provenance)
    assert _count(env.path, "SELECT COUNT(*) FROM DiscoveryRecordState") == 2
    found = _count(env.path, "SELECT SUM(events_found) FROM SourceMonthDiscovery WHERE source_type = 'Perplexity' "
                             "AND status = 'complete'")
    assert found and found % 2 == 0                         # two records per answer, each month searched

    # The same answer again: each child is found by its own (source, URL, title), nothing is stored twice.
    ids = {r["raw_event_id"] for r in rows}
    FakeCollector.sources = {"perplexity": make_source()}
    asyncio.run(env.make().discover_events(source_types=["Perplexity"], date_range_days=1))
    assert {r["raw_event_id"] for r in _raw(env.path)} == ids

