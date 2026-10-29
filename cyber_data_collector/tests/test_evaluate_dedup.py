"""The deduplication measurement harness: cases from labels and seeds, scored per path, no paid calls.

Offline: an autouse fixture sets bogus keys so any path that reaches a paid
service fails instead of spending; the adjudicator's client is a fake.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from types import SimpleNamespace

import pytest

pytest.importorskip("typesafe_sdk")

from cyber_data_collector.dedup import adjudicator as adj  # noqa: E402
from cyber_data_collector.dedup import duplicate_judgement as dj  # noqa: E402
from cyber_data_collector.dedup.models import LLMPairAdjudication  # noqa: E402
from cyber_data_collector.utils import jev  # noqa: E402
from cyber_data_collector.utils.llm_models import typesafe_model  # noqa: E402
from scripts import evaluate_dedup as ev  # noqa: E402


@pytest.fixture(autouse=True)
def bogus_keys(monkeypatch):
    for name in ("OPENAI_API_KEY", "PERPLEXITY_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.setenv(name, "bogus-not-a-key")


SCHEMA = """
    CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title TEXT, summary TEXT,
        description TEXT, event_date TEXT, event_type TEXT, records_affected INTEGER, attack_method TEXT,
        attacking_entity_name TEXT, perplexity_enrichment_data TEXT, status TEXT);
    CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_url TEXT, raw_content TEXT);
    CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, master_enriched_event_id TEXT,
        title TEXT, summary TEXT, description TEXT, event_date TEXT, records_affected INTEGER,
        victim_organization_name TEXT, vendor_organization_name TEXT, status TEXT, attack_method TEXT,
        event_type TEXT);
    CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
    CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT);
    CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
"""


def add_event(conn, key, victim, day, count=None, url=None, entity_id=1, members=1, method=None, actor=None):
    """One dedup event ``key`` with ``members`` records, all dated ``day``."""
    conn.execute("INSERT INTO DeduplicatedEvents VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'Active', ?, 'Data Breach')",
                 (key, f"{key}-1", f"{victim} breach {key}", f"{victim} was breached ({key}).", None, day,
                  count, victim, method))
    conn.execute("INSERT OR IGNORE INTO EntitiesV2 VALUES (?, ?)", (entity_id, victim))
    conn.execute("INSERT INTO DeduplicatedEventEntities VALUES (?, ?, 'victim')", (key, entity_id))
    for n in range(1, members + 1):
        member = f"{key}-{n}"
        conn.execute("INSERT INTO EnrichedEvents VALUES (?, ?, ?, ?, ?, ?, 'Data Breach', ?, ?, ?, ?, 'Active')",
                     (member, f"raw-{member}", f"{victim} breach {key}", f"{victim} was breached ({member}).",
                      f"{victim} reported an attack on {day}.", day, count, method, actor,
                      json.dumps({"formal_entity_name": victim, "attack_method": method, "threat_actor": actor})))
        conn.execute("INSERT INTO RawEvents VALUES (?, ?, ?)",
                     (f"raw-{member}", url, f"{victim} reported a serious attack on {day} and notified customers."))
        conn.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?)", (member, key))


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "eval.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    jev.ensure_schema(conn)
    # Acme: two records of one incident, corroborated by their counts.
    add_event(conn, "A1", "Acme Health", "2024-05-14", count=50_000, entity_id=1)
    add_event(conn, "A2", "Acme Health", "2024-05-14", count=49_000, entity_id=1)
    # Beta: same organisation and exact day but nothing ties the records together (labelled different).
    add_event(conn, "B1", "Beta Bank", "2023-03-09", entity_id=2)
    add_event(conn, "B2", "Beta Bank", "2023-03-09", entity_id=2)
    # Gamma: one event of four records; Delta: two incidents years apart.
    add_event(conn, "G1", "Gamma Energy", "2022-08-02", count=1_000_000, entity_id=3, members=4)
    add_event(conn, "D1", "Delta Water", "2020-02-11", entity_id=4)
    add_event(conn, "D2", "Delta Water", "2024-06-30", entity_id=4)
    # A stored Jev judgement for the labelled-same pair.
    conn.execute("INSERT INTO ModelJudgements VALUES ('j1', 'event_pair', 'A1|A2', ?, ?, 'noul', 'yes', 0.93, NULL, "
                 "NULL, '{}', 'h', ?, ?, 0, 0, '2026-10-29')",
                 (dj.QUESTION_ID, dj.QUESTION_VERSION, typesafe_model(), typesafe_model()))
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def label_files(tmp_path):
    labels, review = tmp_path / "labels.csv", tmp_path / "review.csv"
    with labels.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["pair_id", "label", "rationale", "corroboration", "labeller"])
        w.writerow(["A1|A2", "same", "one breach", "packet", "t"])
        w.writerow(["B1|B2", "different", "two incidents", "packet", "t"])
        w.writerow(["D1|D2", "different", "years apart", "packet", "t"])
        w.writerow(["A1|MISSING", "same", "event gone", "packet", "t"])
        w.writerow(["A1|B1", "unsure", "?", "packet", "t"])
    with review.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["deduplicated_event_id", "expectation", "value", "note", "source"])
        w.writerow(["G1", "victim", "Gamma Energy", "n", "t"])
        w.writerow(["A1", "same_incident_as", "A2", "n", "t"])
    return labels, review


def args(db, label_files, **extra):
    labels, review = label_files
    base = dict(db=str(db), labels=str(labels), review=str(review), snapshot=str(db.parent / "none.db"),
                seed=7, seeded_splits=0, seeded_joins=0, join_gap_days=730, paths=None, skip_rule_impact=False,
                paid_calls=False, save=None, compare=None)
    base.update(extra)
    return SimpleNamespace(**base)


def test_cases_come_from_the_labels_and_skip_what_cannot_be_judged(db, label_files):
    result = ev.run(db, args(db, label_files))
    assert result["cases"] == {"labels/same": 1, "labels/different": 2, "review/same": 1}
    notes = result["case_notes"]["labels"]
    assert notes["skipped: a side has no active record"] == 1 and notes["label unsure"] == 1


def test_the_rule_is_scored_before_and_after_corroboration(db, label_files):
    result = ev.run(db, args(db, label_files, paths=["rule-before", "rule"]))
    before, after = result["paths"]["rule-before"]["labels"], result["paths"]["rule"]["labels"]
    # Beta's records share an organisation and a day only: the old rule merged them wrongly.
    assert before["incorrect_merges"] == 1 and after["incorrect_merges"] == 0
    # Acme's counts corroborate, so the labelled-same pair is still found.
    assert before["found"] == after["found"] == 1


def test_judgement_reads_the_stored_answer_at_the_calibrated_cutoff(db, label_files):
    result = ev.run(db, args(db, label_files, paths=["judgement"]))
    labels = result["paths"]["judgement"]["labels"]
    assert labels["found"] == 1 and labels["cached_calls"] >= 1
    assert labels["undecided"] == 2 and labels["new_calls"] == 2       # B and D pairs have no stored judgement


def test_the_adjudicator_path_counts_new_calls_and_makes_none(db, label_files, monkeypatch):
    def refuse(self):
        raise AssertionError("a paid client was requested")

    monkeypatch.setattr(adj.Adjudicator, "_get_client", refuse)
    result = ev.run(db, args(db, label_files, paths=["adjudicator"]))
    entry = result["paths"]["adjudicator"]
    assert entry["all"]["new_calls"] >= 3 and entry["all"]["found"] == 0
    assert result["adjudication_cache"]["available"] is False


def test_stored_adjudications_are_cached_calls_on_the_next_run(db, label_files, monkeypatch):
    answer = LLMPairAdjudication(is_same_event=True, certainty=0.95, reasoning="r", supporting_facts=[],
                                 distinguishing_facts=[])

    class Client:
        calls = 0
        # No embeddings, so no similarity shortcut: every pair goes to the model.
        embeddings = SimpleNamespace(create=lambda model, input: (_ for _ in ()).throw(RuntimeError("down")))

        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    Client.calls += 1
                    return answer

    monkeypatch.setattr(adj.Adjudicator, "_get_client", lambda self: Client)
    first = ev.run(db, args(db, label_files, paths=["adjudicator"], paid_calls=True))
    # Three distinct pairs; the review case repeats the labelled A1|A2 pair, so it is answered from storage.
    assert Client.calls == 3 and first["paths"]["adjudicator"]["all"]["cached_calls"] == 1
    calls = Client.calls

    monkeypatch.setattr(adj.Adjudicator, "_get_client",
                        lambda self: (_ for _ in ()).throw(AssertionError("a paid client was requested")))
    second = ev.run(db, args(db, label_files, paths=["adjudicator"]))
    entry = second["paths"]["adjudicator"]["all"]
    assert Client.calls == calls and entry["new_calls"] == 0 and entry["cached_calls"] == 4
    assert entry["found"] == 2 and entry["incorrect_merges"] == 2     # the fake model says "same" to everything


def test_the_run_opens_the_database_read_only_unless_paid_calls_are_allowed(db, label_files):
    ev.run(db, args(db, label_files, paths=["rule"]))
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'AdjudicationCache'").fetchone()[0] == 0


def test_end_state_reports_where_the_labelled_outcomes_stand(db, label_files):
    state = ev.run(db, args(db, label_files, paths=["rule"]))["end_state"]
    assert state["labels_same_still_separate"] == 1 and state["labels_different_apart"] == 2
    assert state["labels_unresolvable"] == 1 and state["review_still_separate"] == 1


def test_seeded_cases_split_known_incidents_and_join_known_distinct_ones(db, label_files):
    result = ev.run(db, args(db, label_files, paths=["rule", "candidate"], seeded_splits=5, seeded_joins=5))
    assert result["cases"]["seeded-split/same"] == 1          # only Gamma has two or more records
    assert result["cases"]["seeded-join/different"] == 1      # Delta's two incidents, 4+ years apart
    again = ev.run(db, args(db, label_files, paths=["rule", "candidate"], seeded_splits=5, seeded_joins=5))
    assert again["paths"]["rule"]["all"] == result["paths"]["rule"]["all"]      # seeded: repeatable


def test_candidate_path_reports_surfacing_and_counts(db, label_files):
    result = ev.run(db, args(db, label_files, paths=["candidate"]))
    assert result["paths"]["candidate"]["labels"]["found"] == 1       # Acme's pair shares an organisation
    assert result["candidate_counts"]["pairs"] >= 3


def test_rule_impact_counts_events_that_now_need_judgement(db, label_files):
    conn = sqlite3.connect(db)
    # Beta's second record is added to the first event by the old rule alone.
    conn.execute("UPDATE EventDeduplicationMap SET deduplicated_event_id = 'B1' WHERE deduplicated_event_id = 'B2'")
    conn.commit()
    conn.close()
    impact = ev.run(db, args(db, label_files, paths=["rule"]))["rule_impact"]
    assert impact["events_joined_by_the_old_rules_alone"] >= 1
    assert impact["of_which_would_now_need_judgement"] >= 1
    assert any("left to the judgement" in k for k in impact)


def test_compare_prints_the_change_from_a_saved_run(db, label_files, tmp_path):
    saved = ev.run(db, args(db, label_files, paths=["rule"]))
    earlier = json.loads(json.dumps(saved))
    earlier["paths"]["rule"]["all"]["found"] += 2
    text = ev.compare(saved, earlier)
    assert "found 4->2" in text
    assert "no change" in ev.compare(saved, saved)


def test_main_writes_the_result_and_never_needs_a_key(db, label_files, tmp_path, capsys):
    out = tmp_path / "result.json"
    labels, review = label_files
    code = ev.main(["--db", str(db), "--labels", str(labels), "--review", str(review),
                    "--snapshot", str(tmp_path / "none.db"), "--seeded-splits", "0", "--seeded-joins", "0",
                    "--paths", "rule", "rule-before", "--save", str(out)])
    assert code == 0 and json.loads(out.read_text())["paths"]["rule"]["all"]["cases"] == 4
    assert "path: rule" in capsys.readouterr().out
