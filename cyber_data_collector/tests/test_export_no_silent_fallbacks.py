"""Exports never leak identifying text into anonymised columns, count every
failure, and attach source text only through exact relationships.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest

from scripts.export import export_events_excel as excel
from scripts.export.anonymisation import ANONYMISATION_FAILED, NOT_ANONYMISED, identifying_leak
from scripts.export.export_cyber_events import CyberEventsExporter

SCHEMA = """
CREATE TABLE DeduplicatedEvents (
    deduplicated_event_id TEXT PRIMARY KEY, master_enriched_event_id TEXT, title TEXT,
    description TEXT, summary TEXT, event_type TEXT, severity TEXT, event_date TEXT,
    records_affected INTEGER, victim_organization_industry TEXT, victim_organization_name TEXT,
    attacking_entity_name TEXT, attack_method TEXT, is_australian_event INTEGER DEFAULT 1,
    is_specific_event INTEGER DEFAULT 1, confidence_score REAL, australian_relevance_score REAL,
    total_data_sources INTEGER, contributing_raw_events INTEGER,
    contributing_enriched_events INTEGER, similarity_score REAL, deduplication_method TEXT,
    status TEXT DEFAULT 'Active', created_at TEXT, updated_at TEXT);
CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT,
    title TEXT, description TEXT, summary TEXT);
CREATE TABLE EventDeduplicationMap (deduplicated_event_id TEXT, enriched_event_id TEXT);
CREATE TABLE RawEvents (raw_event_id TEXT, raw_title TEXT, raw_description TEXT, raw_content TEXT);
CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT, entity_type TEXT,
    industry TEXT, turnover TEXT, employee_count INTEGER, is_australian INTEGER,
    headquarters_location TEXT);
CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER,
    relationship_type TEXT, confidence_score REAL);
CREATE TABLE DeduplicatedEventSources (deduplicated_event_id TEXT, source_url TEXT,
    source_type TEXT, credibility_score REAL, content_snippet TEXT, discovered_at TEXT);
"""

SECRET = "Acme Hospital Group"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "events.db"
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    c.execute(
        "INSERT INTO DeduplicatedEvents (deduplicated_event_id, title, description, summary, "
        "event_type, event_date, records_affected, victim_organization_industry, "
        "victim_organization_name, attacking_entity_name) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("D1", "Cyber attack on Acme Hospital Group", f"{SECRET} was hit by ransomware.",
         f"{SECRET} breach summary.", "Ransomware", "2024-05-01", 1000, "Healthcare",
         SECRET, "LockBit"))
    # An unrelated enriched record whose title shares a 30-character prefix
    # with D1's title, linked to no event.
    c.execute("INSERT INTO EnrichedEvents VALUES ('E9', 'R9', ?, ?, ?)",
              ("Cyber attack on Acme Hospital Group Mk II - unrelated",
               "UNRELATED ARTICLE TEXT", "UNRELATED SUMMARY"))
    c.execute("INSERT INTO RawEvents VALUES ('R9', 't', 'd', ?)", ("UNRELATED RAW " * 20,))
    c.execute("INSERT INTO EntitiesV2 (entity_id, entity_name) VALUES (1, ?)", (SECRET,))
    c.commit()
    c.close()
    return path


class FakeClient:
    """OpenAI stand-in: ``answers`` maps a prompt substring to a reply or exception."""

    def __init__(self, summary="Summary of the incident at " + SECRET, anonymised="An organisation was hit."):
        self.summary, self.anonymised = summary, anonymised
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        user = kwargs["messages"][-1]["content"]
        reply = self.anonymised if user.startswith("Fully anonymize") else self.summary
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])


EVENT = {'event_date': '2024-05-01', 'title': 'Cyber attack on Acme Hospital Group',
         'records_affected': 1000, 'industry': 'Healthcare', 'event_type': 'Ransomware',
         'victim_organization_name': SECRET, 'attacking_entity_name': 'LockBit'}
SOURCE = f"{SECRET} was hit by ransomware. " * 10


# ---- 19: anonymised column never carries the original text -------------------

def test_failed_anonymisation_writes_placeholder_not_original():
    row = excel.process_single_event(FakeClient(anonymised=RuntimeError("rate limited")),
                                     EVENT, SOURCE, 500, True)
    assert row['Anonymised Description'].startswith(ANONYMISATION_FAILED)
    assert SECRET not in row['Anonymised Description']
    assert row['_anonymisation_failed'] is True


def test_anonymised_answer_still_naming_the_victim_is_a_failure():
    row = excel.process_single_event(FakeClient(anonymised=f"{SECRET} suffered an attack."),
                                     EVENT, SOURCE, 500, True)
    assert row['Anonymised Description'].startswith(ANONYMISATION_FAILED)
    assert SECRET not in row['Anonymised Description']
    assert row['_anonymisation_failed']


def test_short_description_is_anonymised_not_passed_through():
    # Under 20 characters used to be returned unchanged.
    assert excel.anonymize_with_llm(FakeClient(anonymised="A firm was hacked."),
                                    "Acme hacked", identifying_names=["Acme"]) == "A firm was hacked."
    with pytest.raises(excel.AnonymisationError):
        excel.anonymize_with_llm(None, "Acme hacked")


def test_failed_summary_is_labelled_and_never_anonymised_with_source_text():
    row = excel.process_single_event(FakeClient(summary=RuntimeError("down")), EVENT, SOURCE, 500, True)
    assert row['Event Description'].startswith(excel.SUMMARY_FAILED_PREFIX)
    assert row['Anonymised Description'].startswith(ANONYMISATION_FAILED)
    assert row['_summary_failed'] and row['_anonymisation_failed']


def test_no_llm_marks_anonymised_column_not_anonymised():
    row = excel.process_single_event(None, EVENT, SOURCE, 500, False)
    assert row['Anonymised Description'].startswith(NOT_ANONYMISED)
    assert SECRET not in row['Anonymised Description']
    assert not row['_anonymisation_failed']


def test_worker_failure_row_has_placeholders_only():
    row = excel.failed_worker_row(EVENT, RuntimeError("boom"))
    assert SECRET not in row['Anonymised Description'] + row['Event Description']
    assert row['_anonymisation_failed'] and row['_summary_failed']


def test_llm_requested_without_key_fails_instead_of_falling_back(db, tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(excel.ExportConfigurationError):
        excel.export_events_to_excel(str(db), str(tmp_path / "out.xlsx"), all_dates=True)
    assert not (tmp_path / "out.xlsx").exists()


def test_missing_database_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        excel.export_events_to_excel(str(tmp_path / "nope.db"), str(tmp_path / "o.xlsx"),
                                     use_llm=False)


def test_excel_export_counts_failures_and_writes_placeholders(db, tmp_path):
    out = tmp_path / "out.xlsx"
    result = excel.export_events_to_excel(
        str(db), str(out), all_dates=True, max_workers=1,
        client=FakeClient(anonymised=RuntimeError("timeout")))
    assert result.anonymisation_failures == 1 and not result.ok
    df = pd.read_excel(out, sheet_name="Cyber Events")
    assert df.loc[0, 'Anonymised Description'].startswith(ANONYMISATION_FAILED)
    assert SECRET not in df.loc[0, 'Anonymised Description']
    summary = pd.read_excel(out, sheet_name="Export Summary")
    assert 1 in summary['Value'].tolist()


def test_identifying_leak_detects_names_and_title():
    assert identifying_leak("Acme Ltd lost data", ["Acme Ltd"]) is not None
    assert identifying_leak("a firm lost data", ["Acme Ltd"]) is None
    assert identifying_leak("x Cyber attack on Acme Hospital Group y", [],
                            "Cyber attack on Acme Hospital Group") is not None


# ---- 29: only exact source relationships --------------------------------------

def test_source_text_never_attaches_title_prefix_matches(db):
    with sqlite3.connect(db) as c:
        text, note = excel.get_event_source_text(c.cursor(), "D1")
    assert "UNRELATED" not in text
    assert note == excel.NO_SOURCE_NOTE
    assert SECRET in text  # the event's own description is still used


def test_source_text_uses_map_and_source_snippets(db):
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO EnrichedEvents VALUES ('E1', 'R1', 't', 'Linked description', 's')")
        c.execute("INSERT INTO EventDeduplicationMap VALUES ('D1', 'E1')")
        c.execute("INSERT INTO DeduplicatedEventSources (deduplicated_event_id, content_snippet) "
                  "VALUES ('D1', 'Linked snippet')")
        text, note = excel.get_event_source_text(c.cursor(), "D1")
    assert "Linked description" in text and "Linked snippet" in text
    assert note is None


# ---- 19: export_cyber_events --anonymize ---------------------------------------

def test_detailed_anonymised_export_drops_identifying_columns(db, tmp_path):
    out = tmp_path / "anon.csv"
    with CyberEventsExporter(str(db)) as exporter:
        assert exporter.export_deduplicated_events_with_details(
            str(out), "csv", anonymize=True, all_dates=True)
    df = pd.read_csv(out)
    for column in CyberEventsExporter.ANONYMISED_DROP_COLUMNS:
        assert column not in df.columns
    assert SECRET not in df.to_csv()
    assert "LockBit" not in df.to_csv()


def test_detailed_anonymised_export_counts_leaks_and_fails(db, tmp_path, monkeypatch):
    out = tmp_path / "anon.csv"
    monkeypatch.setattr(CyberEventsExporter, "_anonymize_description",
                        lambda self, description, **kw: description)  # a broken anonymiser
    with CyberEventsExporter(str(db)) as exporter:
        ok = exporter.export_deduplicated_events_with_details(
            str(out), "csv", anonymize=True, all_dates=True)
        assert not ok and exporter.last_anonymisation_failures == 2
    df = pd.read_csv(out)
    assert df.loc[0, 'description'].startswith(ANONYMISATION_FAILED)
    assert SECRET not in df.to_csv()


def test_entity_name_query_error_is_not_swallowed(db):
    with sqlite3.connect(db) as c:
        c.execute("DROP TABLE EntitiesV2")
    with CyberEventsExporter(str(db)) as exporter:
        with pytest.raises(sqlite3.OperationalError):
            exporter._get_all_entity_names()


def test_anonymize_without_detailed_is_rejected(db, monkeypatch, capsys):
    import scripts.export.export_cyber_events as mod
    monkeypatch.setattr("sys.argv", ["x", "--db-path", str(db), "--output", "o.csv", "--anonymize"])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 2
