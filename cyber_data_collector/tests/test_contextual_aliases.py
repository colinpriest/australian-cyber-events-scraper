"""Acronym aliases apply only where the record's text says which organisation is meant (models mocked)."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from cyber_data_collector.dedup import contextual_aliases as ca
from cyber_data_collector.dedup.entity_resolution import canonical_entity_name
from cyber_data_collector.storage.schema_init import initialize_database


@pytest.mark.parametrize("raw, canonical, expected", [
    ("AP", "Advance Press", True),
    ("AV", "Ambulance Victoria", True),
    ("UC", "University of Canberra", True),
    ("IPC NSW", "Information and Privacy Commissioner of NSW (IPC)", True),
    ("M&S", "Marks & Spencer (M&S)", True),
    ("National Australia Bank", "NAB", False),                           # a full name means one organisation
    ("ABC (Australian Broadcasting Corporation)", "ABC", False),
    ("NVIDIA", "Nvidia", False),                                         # a spelling, not an acronym
    ("JBS S.A.", "JBS", False),                                          # the name plus a qualifier
    ("US CISA", "CISA", False),
    ("Medicare", "Services Australia", False),
])
def test_which_variants_are_contextual(raw, canonical, expected):
    assert ca.is_contextual_variant(raw, canonical) is expected


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "instance" / "t.db"
    initialize_database(str(path))
    c = sqlite3.connect(path)
    c.executemany("INSERT INTO EntityAliases (raw_name, canonical_name, canonical_key, source) VALUES (?, ?, '', ?)",
                  [("AP", "Advance Press", "llm"), ("AV", "Ambulance Victoria", "llm"),
                   ("UWA", "University of Western Australia", "human"),
                   ("ASIC", "Australian Securities and Investments Commission", "llm"),   # in EXPLICIT_ALIASES
                   ("National Australia Bank", "NAB", "llm")])
    c.commit()
    yield c
    c.close()


def _record(c, rid, text, entity=None, role="victim", dedup=None):
    c.execute("INSERT INTO RawEvents (raw_event_id, source_type, raw_content) VALUES (?, 'GoogleSearch', ?)",
              (f"r{rid}", text))
    c.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, status, is_australian_event, "
              "is_specific_event) VALUES (?, ?, ?, 'Active', 1, 1)", (rid, f"r{rid}", text[:40]))
    if dedup:
        c.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, master_enriched_event_id, title, status, "
                  "victim_organization_name, is_australian_event, is_specific_event) "
                  "VALUES (?, ?, 't', 'Active', ?, 1, 1)", (dedup, rid, entity))
        c.execute("INSERT INTO EventDeduplicationMap (map_id, raw_event_id, enriched_event_id, deduplicated_event_id, "
                  "contribution_type, similarity_score) VALUES (?, ?, ?, ?, 'primary', 1.0)",
                  (f"m{rid}", f"r{rid}", rid, dedup))
    if entity:
        row = c.execute("SELECT entity_id FROM EntitiesV2 WHERE entity_name = ?", (entity,)).fetchone()
        entity_id = row[0] if row else c.execute("INSERT INTO EntitiesV2 (entity_name) VALUES (?)", (entity,)).lastrowid
        c.execute("INSERT INTO EnrichedEventEntities (enriched_event_id, entity_id, relationship_type) "
                  "VALUES (?, ?, ?)", (rid, entity_id, role))
    c.commit()


def _entity_of(c, rid):
    return [r[0] for r in c.execute("SELECT v.entity_name FROM EnrichedEventEntities l JOIN EntitiesV2 v "
                                    "ON v.entity_id = l.entity_id WHERE l.enriched_event_id = ?", (rid,))]


class FakeJudge:
    """Answers the mention question with a fixed distribution per record."""

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def ask(self, subject_type, subject_id, version, state, questions):
        self.asked.append(subject_id)
        rid = subject_id.split(":")[1]
        return {ca.QUESTION_ID: SimpleNamespace(probabilities=self.answers[rid])}


def test_model_confirmed_acronyms_move_to_the_contextual_table_and_reviewed_ones_stay_global(conn):
    moved = ca.migrate_contextual_aliases(conn)
    assert sorted(moved) == [("AP", "Advance Press"), ("AV", "Ambulance Victoria")]
    assert {r[0] for r in conn.execute("SELECT raw_name FROM EntityAliases")} == {
        "UWA", "ASIC", "National Australia Bank"}                                    # reviewed: global
    assert ca.migrate_contextual_aliases(conn) == []                                 # idempotent


def test_an_acronym_resolves_at_ingest_only_when_the_text_names_one_organisation(conn):
    ca.migrate_contextual_aliases(conn)
    ca.add_contextual_alias(conn, "AV", "Victorian Ambulance Union")
    assert canonical_entity_name(conn, "AV") == "AV"                                 # no context: as written
    assert canonical_entity_name(conn, "AV", context="Ambulance Victoria (AV) said...") == "Ambulance Victoria"
    both = "AV staff, represented by the Victorian Ambulance Union, told Ambulance Victoria..."
    assert canonical_entity_name(conn, "AV", context=both) == "AV"                   # two candidates: undecided
    assert canonical_entity_name(conn, "National Australia Bank") == "NAB"           # global aliases unchanged


def test_links_are_placed_by_their_own_records_text(conn):
    # The old entity-wide fold put both AP records under Advance Press.
    _record(conn, "e1", "Advance Press, a Melbourne printer (AP), was hit by ransomware.", "Advance Press")
    _record(conn, "e2", "AP reported that the hackers leaked US voter data.", "Advance Press", dedup="d2")
    _record(conn, "e3", "AV confirmed its rostering system was breached.", "AV")
    _record(conn, "e4", "Ambulance Victoria (AV) said patient data was not affected.", "AV")
    ca.migrate_contextual_aliases(conn)
    _record(conn, "e7", "AP Responds to the breach.", "Advance Press")      # unclear: must not move
    judge = FakeJudge({"e2": {"candidate_1": 0.02, ca.DIFFERENT: 0.97, ca.UNCLEAR: 0.01},
                       "e3": {"candidate_1": 0.95, ca.DIFFERENT: 0.02, ca.UNCLEAR: 0.03},
                       "e7": {"candidate_1": 0.05, ca.DIFFERENT: 0.03, ca.UNCLEAR: 0.92}})
    report = ca.resolve_links(conn, ca.MentionResolver(conn, judge=judge))
    assert _entity_of(conn, "e1") == ["Advance Press"]          # names the organisation: kept, no model asked
    assert _entity_of(conn, "e2") == ["AP"]                     # Associated Press: back to the name it used
    assert _entity_of(conn, "e3") == ["Ambulance Victoria"]     # Jev reads the context
    assert _entity_of(conn, "e4") == ["Ambulance Victoria"]     # named in the text
    assert _entity_of(conn, "e7") == ["Advance Press"]          # unsure: an unsure answer moves nothing
    assert sorted(s.split(":")[1] for s in judge.asked) == ["e2", "e3", "e7"]
    assert conn.execute("SELECT victim_organization_name FROM DeduplicatedEvents").fetchone()[0] == "AP"
    assert report.victims_renamed == 1
    methods = dict(conn.execute("SELECT subject_id, method FROM EntityMentionResolutions").fetchall())
    assert methods == {"e2": ca.METHOD_JEV, "e3": ca.METHOD_JEV, "e4": ca.METHOD_NAMED, "e7": ca.METHOD_UNRESOLVED}
    again = ca.resolve_links(conn, ca.MentionResolver(conn, judge=judge))   # same answers (Jev's are stored)
    assert not again.moved_to_organisation and not again.moved_to_variant


def test_the_reviewed_alias_step_does_not_relabel_model_aliases_as_reviewed(tmp_path):
    # It folds every trusted stored alias; it used to record each as "human", so a
    # model-confirmed acronym (AFP) became "reviewed" and escaped the contextual rule.
    from scripts import dedup_admin

    path = tmp_path / "instance" / "t.db"
    initialize_database(str(path))
    c = sqlite3.connect(path)
    c.executemany("INSERT INTO EntityAliases (raw_name, canonical_name, canonical_key, source) VALUES (?, ?, '', ?)",
                  [("AFP", "Australian Federal Police", "llm"),
                   ("Australian Federal Police (AFP)", "Australian Federal Police", "llm"),
                   ("ASIC", "Australian Securities and Investments Commission", "llm")])
    _record(c, "a1", "The Australian Federal Police (AFP) arrested two men.", "AFP")
    _record(c, "a2", "ASIC said the breach was contained.", "ASIC")
    c.execute("INSERT INTO EntitiesV2 (entity_name) VALUES ('Australian Federal Police')")
    c.execute("INSERT INTO EntitiesV2 (entity_name) VALUES ('Australian Securities and Investments Commission')")
    c.commit()
    c.close()
    rc = dedup_admin.cmd_canonicalise_entities(SimpleNamespace(db=str(path), dry_run=False, explicit_only=True,
                                                               limit=None))
    c = sqlite3.connect(path)
    assert rc == 0
    assert dict(c.execute("SELECT raw_name, source FROM EntityAliases").fetchall()) == {
        "Australian Federal Police (AFP)": "llm",                    # kept its source
        "ASIC": "human"}                                             # EXPLICIT_ALIASES: reviewed, global
    assert c.execute("SELECT raw_name FROM ContextualEntityAliases").fetchall() == [("AFP",)]
    assert _entity_of(c, "a1") == ["Australian Federal Police"]      # placed by its own text
    assert _entity_of(c, "a2") == ["Australian Securities and Investments Commission"]
    c.close()


def test_an_unsettled_answer_is_escalated_and_a_failed_escalation_changes_nothing(conn, monkeypatch):
    from cyber_data_collector.utils import escalation

    _record(conn, "e5", "AV said the incident was contained.", "AV")
    _record(conn, "e6", "AV said systems were restored.", "AV")
    ca.migrate_contextual_aliases(conn)
    judge = FakeJudge({"e5": {"candidate_1": 0.6, ca.UNCLEAR: 0.4}, "e6": {"candidate_1": 0.55, ca.UNCLEAR: 0.45}})
    answers = {"e5": {"candidate_1": 0.93, ca.DIFFERENT: 0.02, ca.UNCLEAR: 0.05}, "e6": None}
    monkeypatch.setattr(escalation, "escalate_choice",
                        lambda conn, st, subject, *a, **k: answers[subject.split(":")[1]])
    report = ca.resolve_links(conn, ca.MentionResolver(conn, judge=judge))
    assert _entity_of(conn, "e5") == ["Ambulance Victoria"]
    assert _entity_of(conn, "e6") == ["AV"] and report.failed == [("EnrichedEventEntities", "e6", "AV")]
