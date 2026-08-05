"""Tests for the short-gap repeat-attack audit.

The pass exists to catch one incident stored twice because its coverage was
published weeks apart, without destroying the genuine case of an organisation
attacked twice in a quarter. Both directions are tested here.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from cyber_data_collector.dedup.cluster_adjudicator import (
    ClusterPartition,
    IncidentGroup,
)
from cyber_data_collector.dedup.recurrence_check import (
    DEFAULT_WINDOW_DAYS,
    RecurrenceAuditor,
    RecurrenceEvent,
    attach_source_urls,
    build_runs,
    findings_from_partition,
    load_recurrence_events,
    parse_event_date,
    run_span,
)


def event(dedup_id, day, entity_id=1, entity="Acme Ltd", title=None,
          members=1, **kwargs):
    return RecurrenceEvent(
        dedup_id=dedup_id, entity_id=entity_id, entity_name=entity,
        title=title or f"Event {dedup_id}",
        event_date=date.fromisoformat(day) if isinstance(day, str) else day,
        members=members, **kwargs)


# ----------------------------------------------------------------------
# Run construction: which repeats get re-checked
# ----------------------------------------------------------------------

def test_short_gap_repeat_forms_a_run():
    events = [event("a", "2023-04-01"), event("b", "2023-05-20")]  # 49 days
    runs = build_runs(events)
    assert len(runs) == 1
    assert [e.dedup_id for e in runs[0]] == ["a", "b"]


def test_gap_at_or_beyond_the_window_is_left_alone():
    events = [event("a", "2023-01-01"), event("b", "2023-04-01")]  # 90 days
    assert build_runs(events, window_days=90) == []


def test_boundary_is_exclusive():
    """< 90 days is checked; exactly 90 is not."""
    assert build_runs([event("a", "2023-01-01"), event("b", "2023-03-31")],
                      window_days=90)          # 89 days
    assert not build_runs([event("a", "2023-01-01"), event("b", "2023-04-01")],
                          window_days=90)      # 90 days


def test_same_day_events_are_included():
    """A gap of zero is the most likely duplicate of all."""
    runs = build_runs([event("a", "2023-04-01"), event("b", "2023-04-01")])
    assert len(runs) == 1


def test_different_entities_never_share_a_run():
    events = [event("a", "2023-04-01", entity_id=1, entity="Acme"),
              event("b", "2023-04-10", entity_id=2, entity="Beta")]
    assert build_runs(events) == []


def test_consecutive_gaps_chain_into_one_run():
    """Three records six weeks apart are judged together, not as three pairs.

    Pairwise verdicts can contradict each other (A~B, B~C, A!~C); a partition
    over the whole run cannot.
    """
    events = [event("a", "2023-04-01"), event("b", "2023-05-15"),
              event("c", "2023-06-28")]
    runs = build_runs(events)
    assert len(runs) == 1
    assert len(runs[0]) == 3


def test_a_long_gap_breaks_the_chain():
    events = [event("a", "2023-01-01"), event("b", "2023-02-01"),
              event("c", "2024-06-01"), event("d", "2024-06-20")]
    runs = build_runs(events)
    assert len(runs) == 2
    assert [[e.dedup_id for e in r] for r in runs] == [["a", "b"], ["c", "d"]]


def test_isolated_events_produce_no_run():
    assert build_runs([event("a", "2020-01-01")]) == []


def test_undated_events_are_skipped():
    undated = RecurrenceEvent(dedup_id="x", entity_id=1, entity_name="Acme",
                              title="No date", event_date=None)
    assert build_runs([undated, event("a", "2023-04-01")]) == []


def test_one_event_listed_twice_under_an_entity_is_not_a_repeat():
    """A duplicated victim link must not look like an attack on itself."""
    duplicated = [event("a", "2023-04-01"), event("a", "2023-04-01")]
    assert build_runs(duplicated) == []


def test_runs_are_ordered_by_date():
    events = [event("late", "2023-05-01"), event("early", "2023-04-01")]
    assert [e.dedup_id for e in build_runs(events)[0]] == ["early", "late"]


def test_run_span_measures_first_to_last():
    run = build_runs([event("a", "2023-04-01"), event("b", "2023-05-01"),
                      event("c", "2023-06-01")])[0]
    assert run_span(run) == 61


def test_default_window_is_ninety_days():
    assert DEFAULT_WINDOW_DAYS == 90


@pytest.mark.parametrize("value,expected", [
    ("2023-04-26", date(2023, 4, 26)),
    ("2023-04-26 00:00:00", date(2023, 4, 26)),
    (None, None),
    ("not a date", None),
    ("", None),
])
def test_parse_event_date(value, expected):
    assert parse_event_date(value) == expected


# ----------------------------------------------------------------------
# Turning a verdict into merge instructions
# ----------------------------------------------------------------------

def test_grouped_records_become_a_finding():
    run = [event("a", "2023-04-01", members=5),
           event("b", "2023-05-20", members=1)]
    partition = ClusterPartition(incidents=[IncidentGroup(
        label="Acme breach (April 2023)", member_indices=[1, 2],
        certainty=0.93, reasoning="Same record count and attack method.")])
    findings = findings_from_partition(run, partition)
    assert len(findings) == 1
    # The richer event is kept; folding it into the thin one would throw away
    # the better lineage.
    assert findings[0]["target"]["id"] == "a"
    assert [s["id"] for s in findings[0]["sources"]] == ["b"]
    assert findings[0]["max_gap_days"] == 49
    assert findings[0]["certainty"] == 0.93


def test_genuinely_separate_attacks_produce_no_finding():
    run = [event("a", "2023-04-01"), event("b", "2023-05-20")]
    partition = ClusterPartition(incidents=[
        IncidentGroup(label="Phishing", member_indices=[1], certainty=0.9,
                      reasoning="Credential phishing."),
        IncidentGroup(label="Ransomware", member_indices=[2], certainty=0.9,
                      reasoning="Separate ransomware intrusion after remediation."),
    ])
    assert findings_from_partition(run, partition) == []


def test_a_mixed_verdict_merges_only_the_duplicated_pair():
    run = [event("a", "2023-04-01", members=3), event("b", "2023-04-20"),
           event("c", "2023-06-01")]
    partition = ClusterPartition(incidents=[
        IncidentGroup(label="April breach", member_indices=[1, 2],
                      certainty=0.9, reasoning="Follow-up coverage."),
        IncidentGroup(label="June incident", member_indices=[3],
                      certainty=0.88, reasoning="Different vector."),
    ])
    findings = findings_from_partition(run, partition)
    assert len(findings) == 1
    assert findings[0]["target"]["id"] == "a"
    assert [s["id"] for s in findings[0]["sources"]] == ["b"]


def test_gap_is_measured_in_date_order_not_merge_order():
    """The target is chosen by richness, so gaps must be computed separately."""
    run = [event("a", "2023-04-01", members=1),
           event("b", "2023-05-20", members=9)]
    partition = ClusterPartition(incidents=[IncidentGroup(
        label="one", member_indices=[1, 2], certainty=0.9, reasoning="r")])
    finding = findings_from_partition(run, partition)[0]
    assert finding["target"]["id"] == "b"      # richer event kept
    assert finding["max_gap_days"] == 49       # still a positive real gap
    assert finding["span_days"] == 49


# ----------------------------------------------------------------------
# The auditor's failure behaviour
# ----------------------------------------------------------------------

class _Boom:
    class chat:
        class completions:
            @staticmethod
            def create(**kwargs):
                raise RuntimeError("API down")


def test_llm_failure_changes_nothing():
    auditor = RecurrenceAuditor(openai_client=_Boom())
    run = [event("a", "2023-04-01"), event("b", "2023-05-01")]
    partition = auditor.audit_run(run)
    assert findings_from_partition(run, partition) == []
    assert auditor.stats["llm_failures"] == 1


def test_an_invalid_partition_changes_nothing():
    """A partition that drops or double-assigns a record must not be applied."""
    class Dropping:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    return ClusterPartition(incidents=[IncidentGroup(
                        label="only the first", member_indices=[1],
                        certainty=0.99, reasoning="r")])

    auditor = RecurrenceAuditor(openai_client=Dropping())
    run = [event("a", "2023-04-01"), event("b", "2023-05-01")]
    assert findings_from_partition(run, auditor.audit_run(run)) == []
    assert auditor.stats["invalid_partitions"] == 1


def test_no_client_leaves_everything_unchanged(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    auditor = RecurrenceAuditor()
    run = [event("a", "2023-04-01"), event("b", "2023-05-01")]
    assert findings_from_partition(run, auditor.audit_run(run)) == []


def test_oversized_runs_are_chunked_rather_than_dropped():
    calls = []

    class Grouping:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    body = kwargs["messages"][1]["content"]
                    size = body.count("] title:")
                    calls.append(size)
                    return ClusterPartition(incidents=[IncidentGroup(
                        label="all one", member_indices=list(range(1, size + 1)),
                        certainty=0.9, reasoning="r")])

    run = [event(str(i), f"2023-04-{i:02d}") for i in range(1, 11)]
    auditor = RecurrenceAuditor(openai_client=Grouping(), max_run_size=4)
    partition = auditor.audit_run(run)
    assert auditor.stats["chunked"] == 1
    assert calls == [4, 4, 2]
    # Renumbering must still cover every record exactly once.
    assert partition.validate_covers(len(run)) == []


def test_the_prompt_states_the_gap_between_records():
    """The gap is the reason the run is being examined; the model must see it."""
    auditor = RecurrenceAuditor()
    body = auditor.render_run([event("a", "2023-04-01"),
                               event("b", "2023-05-20")])
    assert "49 days after record 1" in body
    assert "Acme Ltd" in body


def test_the_prompt_carries_the_distinguishing_evidence():
    auditor = RecurrenceAuditor()
    body = auditor.render_run([
        event("a", "2023-04-01", records_affected=9_700_000,
              attack_method="Ransomware", source_urls=["https://x.test/1"]),
        event("b", "2023-05-01", records_affected=9_700_000,
              attack_method="Ransomware")])
    assert "9,700,000" in body
    assert "Ransomware" in body
    assert "https://x.test/1" in body


# ----------------------------------------------------------------------
# Loading from the database
# ----------------------------------------------------------------------

@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE EntitiesV2 (
            entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_name VARCHAR(255));
        CREATE TABLE DeduplicatedEvents (
            deduplicated_event_id TEXT PRIMARY KEY,
            master_enriched_event_id TEXT, title TEXT, description TEXT,
            summary TEXT, event_date DATE, records_affected BIGINT,
            attack_method TEXT, event_type TEXT,
            vendor_organization_name TEXT, status VARCHAR(20));
        CREATE TABLE DeduplicatedEventEntities (
            deduplicated_event_id TEXT, entity_id INTEGER,
            relationship_type VARCHAR(50));
        CREATE TABLE DeduplicatedEventSources (
            deduplicated_event_id TEXT, source_url TEXT);
        CREATE TABLE EventDeduplicationMap (
            deduplicated_event_id TEXT, enriched_event_id TEXT);
        CREATE TABLE EnrichedEvents (
            enriched_event_id TEXT PRIMARY KEY, summary TEXT, description TEXT);
        """
    )
    connection.execute("INSERT INTO EntitiesV2 (entity_name) VALUES ('Acme Ltd')")
    yield connection
    connection.close()


def _insert_event(conn, dedup_id, day, role="victim", status="Active",
                  summary="Some detail."):
    conn.execute(
        "INSERT INTO DeduplicatedEvents (deduplicated_event_id, title, "
        "summary, event_date, status) VALUES (?, ?, ?, ?, ?)",
        (dedup_id, f"Title {dedup_id}", summary, day, status))
    conn.execute("INSERT INTO DeduplicatedEventEntities VALUES (?, 1, ?)",
                 (dedup_id, role))
    conn.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?)",
                 (dedup_id, f"enr-{dedup_id}"))


def test_load_uses_the_victim_role(conn):
    _insert_event(conn, "d1", "2023-04-01")
    _insert_event(conn, "d2", "2023-04-20", role="bystander")
    loaded = load_recurrence_events(conn)
    assert [e.dedup_id for e in loaded] == ["d1"]


def test_load_skips_inactive_events(conn):
    _insert_event(conn, "d1", "2023-04-01")
    _insert_event(conn, "d2", "2023-04-20", status="Merged")
    assert [e.dedup_id for e in load_recurrence_events(conn)] == ["d1"]


def test_load_rejects_raw_pdf_as_detail(conn):
    """A description that is still a raw PDF scrape is worse than none."""
    _insert_event(conn, "d1", "2023-04-01", summary="%PDF-1.4 garbage")
    assert load_recurrence_events(conn)[0].summary is None


def test_end_to_end_run_detection_from_the_database(conn):
    _insert_event(conn, "d1", "2023-04-01")
    _insert_event(conn, "d2", "2023-05-20")
    _insert_event(conn, "d3", "2024-11-01")
    runs = build_runs(load_recurrence_events(conn))
    assert len(runs) == 1
    assert [e.dedup_id for e in runs[0]] == ["d1", "d2"]


def test_source_urls_are_attached(conn):
    _insert_event(conn, "d1", "2023-04-01")
    conn.execute("INSERT INTO DeduplicatedEventSources VALUES "
                 "('d1', 'https://news.test/story')")
    events = load_recurrence_events(conn)
    attach_source_urls(conn, events)
    assert events[0].source_urls == ["https://news.test/story"]
