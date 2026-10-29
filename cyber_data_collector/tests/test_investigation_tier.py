"""Investigation tier: organisation timelines, reasoning-model escalation, victim checks.

All offline: searches, Jev and the reasoning model are fakes.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

pytest.importorskip("typesafe_sdk")

from cyber_data_collector.dedup import event_reality as er  # noqa: E402
from cyber_data_collector.dedup import org_timeline as ot  # noqa: E402
from cyber_data_collector.dedup import victim_judgement as vj  # noqa: E402
from cyber_data_collector.utils import escalation  # noqa: E402
from cyber_data_collector.utils.jev import JevJudge  # noqa: E402

TIMELINE = "1. 2024-08-14 | Student management system breach | Oct 2024 notice | https://x\n" \
           "2. 2025-06-19 | Third-party breach | Oct 2025 notice | https://y"



@pytest.fixture(autouse=True)
def _no_text_extraction(monkeypatch):
    """The victim candidates include organisations the text names (an extraction by the fast model).
    These tests are about something else: the fake finds none, and bogus keys make a forgotten fake fail."""
    from cyber_data_collector.dedup import event_organisations

    for var in ("OPENAI_API_KEY", "TYPESAFE_API_KEY", "PERPLEXITY_API_KEY"):
        monkeypatch.setenv(var, "bogus-not-a-real-key")
    monkeypatch.setattr(event_organisations, "default_extractor",
                        lambda: (lambda text: event_organisations.NamedOrganisations()))


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, title TEXT, event_date TEXT,
            victim_organization_name TEXT, status TEXT, updated_at TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title TEXT,
            perplexity_enrichment_data TEXT, status TEXT, updated_at TEXT);
        CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_url TEXT, raw_content TEXT, raw_description TEXT,
            discovered_at TEXT);
        INSERT INTO EntitiesV2 VALUES (1, 'Western Sydney University');
    """)
    for did, title in [("w1", "WSU data breach (2024)"), ("w2", "WSU data breach (August 2025)")]:
        c.execute("INSERT INTO DeduplicatedEvents VALUES (?, ?, '2025-01-01', 'Western Sydney University', 'Active', NULL)",
                  (did, title))
        c.execute("INSERT INTO DeduplicatedEventEntities VALUES (?, 1, 'victim')", (did,))
        c.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?)", (f"{did}-r", did))
        c.execute("INSERT INTO EnrichedEvents VALUES (?, ?, ?, ?, 'Active', NULL)",
                  (f"{did}-r", f"{did}-r", title, json.dumps({"formal_entity_name": "Western Sydney University"})))
        c.execute("INSERT INTO RawEvents VALUES (?, 'https://x', 'Privacy policy', NULL, '2026-05-03T10:00:00')",
                  (f"{did}-r",))
    return c


def _search(calls):
    def search(query, model):
        calls.append(query)
        return {"answer": TIMELINE, "citations": ["https://x", "https://y"]}
    return search


def test_timeline_is_researched_once_and_reaches_the_event_state(conn):
    calls = []
    assert ot.research_all(conn, search=_search(calls)) == 1
    assert ot.research_all(conn, search=_search(calls)) == 1
    assert len(calls) == 1 and "Western Sydney University" in calls[0]
    state = er.build_state(conn, "w1", corroborated=True, with_timeline=True)
    assert "2025-06-19" in state["organisation_timeline"]["incidents"]


class FakeReasoner:
    """Stands in for an instructor client: returns a fixed probability, counts calls."""

    def __init__(self, probability):
        self.probability = probability
        self.calls = 0
        self.chat = self
        self.completions = self

    def create_with_completion(self, response_model, messages, **kwargs):
        self.calls += 1
        assert kwargs.get("reasoning_effort") == escalation.REASONING_EFFORT and "temperature" not in kwargs
        judgement = response_model(rationale="Matches incident 2 in the timeline.", probability=self.probability)
        return judgement, SimpleNamespace(model="gpt-5.5-2026-04-23", usage=None)


def test_escalation_is_stored_with_its_rationale_and_reused(conn):
    reasoner = FakeReasoner(0.9)
    args = (conn, "deduplicated_event", "w1", "q_escalated", "v1", {"x": 1}, "Is it real?")
    assert escalation.escalate(*args, client=reasoner) == pytest.approx(0.9)
    assert escalation.escalate(*args, client=FakeReasoner(0.1)) == pytest.approx(0.9)   # stored answer wins
    assert reasoner.calls == 1
    probs = json.loads(conn.execute("SELECT probabilities_json FROM ModelJudgements").fetchone()[0])
    assert probs["rationale"].startswith("Matches incident 2")


def test_only_the_middle_band_is_escalated_and_its_answer_replaces_jevs(conn):
    reasoner = FakeReasoner(0.95)
    escalated = er.escalate_band(conn, {"w1": 0.4, "w2": 0.05}, client=reasoner)
    assert escalated == {"w1": pytest.approx(0.95)} and reasoner.calls == 1


class FakeJev:
    def __init__(self, p):
        self.p = p

    def system_one(self, state, questions, model=None):
        return SimpleNamespace(model="jev-1.14.0", usage=None,
                               answers={q: SimpleNamespace(type="noul", noul=self.p) for q in questions})


def test_victim_attribution_reports_and_changes_nothing(conn, tmp_path):
    ot.research_all(conn, search=_search([]))
    result = vj.check_victim_attribution(conn, judge=JevJudge(conn, client=FakeJev(0.05)),
                                         calibration_path=tmp_path / "no_calibration.json")
    assert result["judged"] == 2 and len(result["reported"]) == 2 and not result["calibrated"]
    assert {r[0] for r in conn.execute("SELECT victim_organization_name FROM DeduplicatedEvents")} == \
           {"Western Sydney University"}


def test_a_timeline_is_refreshed_when_new_evidence_arrives(conn):
    calls = []
    ot.research_all(conn, search=_search(calls))
    # Pretend it was researched in May, before the new record below arrived in June.
    conn.execute("UPDATE OrganisationTimelines SET created_at = '2026-05-04T00:00:00'")
    first = ot.stored(conn, 1)
    assert not ot.is_stale(conn, 1)

    # A new record about the organisation arrives after the timeline was researched.
    conn.execute("INSERT INTO RawEvents VALUES ('w3-r', 'https://z', 'WSU notice', NULL, '2026-06-01T00:00:00')")
    conn.execute("INSERT INTO EnrichedEvents VALUES ('w3-r', 'w3-r', 'WSU notice', '{}', 'Active', NULL)")
    conn.execute("INSERT INTO EventDeduplicationMap VALUES ('w3-r', 'w1')")
    assert ot.is_stale(conn, 1)

    def newer(query, model):
        calls.append(query)
        return {"answer": TIMELINE + "\n3. 2099-01-01 | New breach | notice | https://z", "citations": []}

    refreshed = ot.research(conn, 1, "Western Sydney University", search=newer)
    assert len(calls) == 2 and "2099-01-01" in refreshed["timeline"]
    assert conn.execute("SELECT COUNT(*) FROM OrganisationTimelines").fetchone()[0] == 2   # both versions kept
    assert refreshed["timeline"] != first["timeline"] and not ot.is_stale(conn, 1)
    ot.research_all(conn, search=newer)
    assert len(calls) == 2   # nothing new: no search
