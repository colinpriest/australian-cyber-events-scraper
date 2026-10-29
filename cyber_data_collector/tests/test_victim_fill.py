"""Victim filling, the text-named candidates, the proper-name judgement and the role-classification fixes.

All offline: Jev, the extraction model and the reasoning model are fakes, and an autouse fixture sets bogus
keys so a forgotten fake fails instead of spending.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from types import SimpleNamespace

import pytest

pytest.importorskip("typesafe_sdk")

from cyber_data_collector.dedup import event_organisations as eo  # noqa: E402
from cyber_data_collector.dedup import name_judgement as nj  # noqa: E402
from cyber_data_collector.dedup import victim_fill as vf  # noqa: E402
from cyber_data_collector.dedup import victim_judgement as vj  # noqa: E402
from cyber_data_collector.dedup.role_maintenance import staleness_report, stale_event_ids  # noqa: E402
from cyber_data_collector.utils.jev import JevJudge  # noqa: E402
from cyber_data_collector.utils.llm_models import openai_fast_model, openai_model, typesafe_model  # noqa: E402


@pytest.fixture(autouse=True)
def _bogus_keys(monkeypatch):
    for var in ("OPENAI_API_KEY", "TYPESAFE_API_KEY", "PERPLEXITY_API_KEY"):
        monkeypatch.setenv(var, "bogus-not-a-real-key")
    monkeypatch.setenv("TYPESAFE_MODEL", typesafe_model())
    monkeypatch.setenv("OPENAI_MODEL", openai_model())
    monkeypatch.setenv("OPENAI_FAST_MODEL", openai_fast_model())
    nj.deactivate()
    yield
    nj.deactivate()


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT, entity_kind TEXT,
            entity_type TEXT, industry TEXT, is_australian INTEGER);
        CREATE TABLE EntityAliases (alias_id INTEGER PRIMARY KEY AUTOINCREMENT, raw_name TEXT UNIQUE,
            canonical_name TEXT, canonical_key TEXT, source TEXT DEFAULT 'rule', confidence REAL, created_at TEXT);
        CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_url TEXT, raw_content TEXT,
            raw_description TEXT, event_date TEXT, source_metadata TEXT, discovered_at TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title TEXT,
            event_date TEXT, summary TEXT, description TEXT, perplexity_enrichment_data TEXT,
            status TEXT, updated_at TEXT, attacking_entity_name TEXT);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, title TEXT, event_date TEXT,
            status TEXT, victim_organization_name TEXT, victim_organization_industry TEXT,
            vendor_organization_name TEXT, updated_at TEXT, summary TEXT, description TEXT, master_enriched_event_id TEXT,
            roles_member_signature TEXT, roles_classified_at TEXT, roles_prompt_version TEXT);
        CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
        CREATE TABLE EnrichedEventEntities (enriched_event_id TEXT, entity_id INTEGER,
            relationship_type TEXT, confidence_score REAL);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER,
            relationship_type TEXT, confidence_score REAL, source_count INTEGER);
        CREATE TABLE DedupSnapshots (snapshot_id TEXT PRIMARY KEY, batch_id TEXT, deduplicated_event_id TEXT,
            payload_json TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
    """)
    return c


def _entity(conn, name, kind="organisation"):
    row = conn.execute("SELECT entity_id FROM EntitiesV2 WHERE entity_name = ?", (name,)).fetchone()
    if row:
        return row[0]
    return conn.execute("INSERT INTO EntitiesV2 (entity_name, entity_kind, is_australian) VALUES (?, ?, 1)",
                        (name, kind)).lastrowid


def _event(conn, did, page, links=(), victims=(), title="OpenAI agent accessed a government website",
           attacker=None, org=None, description="", scalar=None):
    """links: (name, role[, kind]); victims: names recorded with the victim role."""
    conn.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, title, event_date, status, "
                 "victim_organization_name) VALUES (?, ?, '2026-09-24', 'Active', ?)", (did, title, scalar))
    eid = f"{did}-r"
    conn.execute("INSERT INTO RawEvents VALUES (?, 'https://news/x', ?, NULL, NULL, NULL, '2026-10-01T00:00:00')",
                 (eid, page))
    conn.execute("INSERT INTO EnrichedEvents VALUES (?, ?, ?, '2026-09-24', NULL, ?, ?, 'Active', NULL, ?)",
                 (eid, eid, title, description, json.dumps({"formal_entity_name": org}) if org else None, attacker))
    conn.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?)", (eid, did))
    for link in list(links) + [(v, "victim") for v in victims]:
        name, role = link[0], link[1]
        kind = link[2] if len(link) > 2 else "organisation"
        conn.execute("INSERT INTO EnrichedEventEntities VALUES (?, ?, ?, NULL)", (eid, _entity(conn, name, kind), role))
        conn.execute("INSERT INTO DeduplicatedEventEntities VALUES (?, ?, ?, NULL, 1)",
                     (did, _entity(conn, name, kind), role))


def _role(conn, did, name):
    row = conn.execute("""SELECT relationship_type FROM DeduplicatedEventEntities dee JOIN EntitiesV2 v
                          ON v.entity_id = dee.entity_id WHERE deduplicated_event_id = ? AND entity_name = ?""",
                       (did, name)).fetchone()
    return row[0] if row else None


class FakeJev:
    """The fill Choice picks the named option (probability ``pick_p``); the name Noul answers from a map."""

    def __init__(self, pick=None, pick_p=0.95, proper=None):
        self.pick, self.pick_p, self.proper, self.asked = pick, pick_p, proper or {}, []

    def system_one(self, state, questions, model=None):
        answers = {}
        for qid, q in questions.items():
            self.asked.append(qid)
            if qid == nj.QUESTION_ID:
                answers[qid] = SimpleNamespace(type="noul", noul=self.proper.get(state["name"], 0.9))
                continue
            key = next((k for k, v in q.criteria.items() if v == self.pick), "none")
            probs = {k: (self.pick_p if k == key else (1 - self.pick_p) / (len(q.criteria) - 1)) for k in q.criteria}
            answers[qid] = SimpleNamespace(type="choice", choice=key, confidence=0.9, probabilities=probs)
        return SimpleNamespace(model="jev-1.14.0", usage=None, answers=answers)


def _extractor(victims=(), actors=()):
    calls = []

    def extract(text):
        calls.append(text)
        return eo.NamedOrganisations(possible_victims=list(victims), actors=list(actors))

    extract.calls = calls
    return extract


def _calibration(tmp_path, cutoff=0.8):
    path = tmp_path / "cal.json"
    path.write_text(json.dumps({**vf.calibration_identity(), "cutoff": cutoff}), encoding="utf-8")
    return path


def _labels(tmp_path, rows=()):
    path = tmp_path / "labels.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["deduplicated_event_id", "label", "correct_victim", "rationale", "labeller"])
        w.writerows(rows or [("other", "none_named", "", "placeholder", "test")])
    return path


NSW_TEXT = ("OpenAI said its agent accessed the NSW National Parks and Wildlife Service's Fire History service "
            "last month. The agency has been told.")
MEDICARE_TEXT = "A government services website that contained Medicare data was accessed by an OpenAI agent."


# ------------------------------------------------------------------ the text-named candidates

def test_the_named_agency_in_the_text_becomes_a_candidate_and_is_filled(conn, tmp_path):
    _event(conn, "e1", NSW_TEXT, links=[("OpenAI", "bystander")])
    extractor = _extractor(["NSW National Parks and Wildlife Service", "Invented Agency"], ["OpenAI"])
    judge = JevJudge(conn, client=FakeJev(pick="NSW National Parks and Wildlife Service"))
    result = vf.fill_victims(conn, apply=True, judge=judge, extractor=extractor,
                             calibration_path=_calibration(tmp_path), labels_path=_labels(tmp_path))
    assert result["counts"] == {"filled": 1} and result["applied"], result["plans"]
    assert _role(conn, "e1", "NSW National Parks and Wildlife Service") == "victim"
    assert conn.execute("SELECT victim_organization_name FROM DeduplicatedEvents").fetchone()[0] == \
        "NSW National Parks and Wildlife Service"
    plan = result["plans"][0]
    assert "Invented Agency" not in plan["candidates"]        # not in the text: the model cannot add organisations
    assert "OpenAI" not in plan["candidates"]                 # the actor the text names is never a candidate
    audit = conn.execute("SELECT action, new_victim, roles_before_json FROM VictimFillActions").fetchone()
    assert tuple(audit[:2]) == ("filled", "NSW National Parks and Wildlife Service") and json.loads(audit[2])
    assert conn.execute("SELECT COUNT(*) FROM DedupSnapshots").fetchone()[0] == 1
    # Idempotent: the event has a victim now.
    again = vf.fill_victims(conn, apply=True, judge=judge, extractor=extractor,
                            calibration_path=_calibration(tmp_path), labels_path=_labels(tmp_path))
    assert again["eligible"] == 0


def test_an_extraction_is_stored_not_asked_twice_and_a_failure_is_an_error(conn, tmp_path):
    _event(conn, "e1", NSW_TEXT)
    extractor = _extractor(["NSW National Parks and Wildlife Service"])
    eo.named_organisations(conn, "e1", extractor=extractor)
    eo.named_organisations(conn, "e1", extractor=extractor)
    assert len(extractor.calls) == 1

    _event(conn, "e2", "A different page about a Sunshine Hospital breach")

    def broken(text):
        raise TimeoutError("openai down")

    with pytest.raises(eo.ExtractionFailed):
        eo.named_organisations(conn, "e2", extractor=broken)
    result = vf.fill_victims(conn, apply=False, judge=JevJudge(conn, client=FakeJev()), extractor=broken,
                             calibration_path=_calibration(tmp_path), labels_path=_labels(tmp_path), only_event="e2")
    assert result["errors"] and result["counts"] == {}      # left undecided, not turned into "names nobody"
    with pytest.raises(eo.ExtractionFailed):                # and a forgotten fake fails rather than spends
        eo.named_organisations(conn, "e2")


def test_without_a_calibration_only_labelled_events_are_filled(conn, tmp_path):
    _event(conn, "e1", NSW_TEXT)
    _event(conn, "e2", NSW_TEXT.replace("NSW National Parks", "Sunshine Health"))
    extractor = _extractor(["NSW National Parks and Wildlife Service"])
    judge = JevJudge(conn, client=FakeJev(pick="NSW National Parks and Wildlife Service"))
    labels = _labels(tmp_path, [("e1", "named", "NSW National Parks and Wildlife Service", "r", "t")])
    result = vf.fill_victims(conn, apply=True, judge=judge, extractor=extractor,
                             calibration_path=tmp_path / "absent.json", labels_path=labels)
    assert not result["calibrated"] and result["counts"] == {"filled": 1, "unfilled": 1}
    assert _role(conn, "e1", "NSW National Parks and Wildlife Service") == "victim"
    assert not conn.execute("SELECT 1 FROM DeduplicatedEventEntities WHERE deduplicated_event_id = 'e2' "
                            "AND relationship_type = 'victim'").fetchone()


def test_labels_are_ground_truth_and_the_cutoff_and_text_guards_hold(conn, tmp_path):
    _event(conn, "e1", NSW_TEXT)
    _event(conn, "e2", NSW_TEXT)
    extractor = _extractor(["NSW National Parks and Wildlife Service"])
    labels = _labels(tmp_path, [("e1", "none_named", "", "roundup", "t")])
    judge = JevJudge(conn, client=FakeJev(pick="NSW National Parks and Wildlife Service", pick_p=0.95))
    result = vf.fill_victims(conn, apply=True, judge=judge, extractor=extractor, only_event="e1",
                             calibration_path=_calibration(tmp_path), labels_path=labels)
    assert result["counts"] == {"unfilled": 1} and "none_named" in result["plans"][0]["reason"]
    low = JevJudge(conn, client=FakeJev(pick="NSW National Parks and Wildlife Service", pick_p=0.95))
    result = vf.fill_victims(conn, judge=low, extractor=extractor, only_event="e2",
                             calibration_path=_calibration(tmp_path, cutoff=0.99), labels_path=labels)
    assert result["counts"] == {"unfilled": 1} and "below the cut-off" in result["plans"][0]["reason"]
    with pytest.raises(RuntimeError, match="refusing"):
        vf.fill_victims(conn, apply=True, judge=low, extractor=extractor, calibration_path=_calibration(tmp_path),
                        labels_path=tmp_path / "absent.csv")


def test_a_calibration_for_other_evidence_is_report_only(tmp_path, caplog):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"question_version": "victim-fill-v0", "model": "x", "cutoff": 0.5}), encoding="utf-8")
    assert vf.active_calibration(path) is None and "REPORT ONLY" in caplog.text


# ------------------------------------------------------------------ population and candidates

def test_an_event_whose_entities_are_all_bystanders_is_eligible_and_its_bystander_links_are_candidates(conn):
    _event(conn, "e1", MEDICARE_TEXT, links=[("OpenAI", "bystander"), ("Australian Medicare", "bystander", "government_body"),
                                              ("Yahoo News Australia", "bystander")])
    conn.execute("INSERT INTO EntityAliases (raw_name, canonical_name, canonical_key, source) "
                 "VALUES ('Medicare', 'Services Australia', 'services australia', 'human')")
    _entity(conn, "Services Australia", "government_body")
    assert [e["deduplicated_event_id"] for e in vf.victimless_events(conn)] == ["e1"]
    names = [c["name"] for c in vj.replacement_candidates(conn, "e1", [])]
    assert "Australian Medicare" in names and "Yahoo News Australia" in names


def test_a_variant_resolves_to_the_organisation_through_the_stored_alias(conn):
    _event(conn, "e1", MEDICARE_TEXT, links=[("Medicare", "bystander", "government_body")])
    conn.execute("INSERT INTO EntityAliases (raw_name, canonical_name, canonical_key, source) "
                 "VALUES ('Medicare', 'Services Australia', 'services australia', 'human')")
    candidates = vj.replacement_candidates(conn, "e1", [])
    assert [c["name"] for c in candidates] == ["Services Australia"] and "Medicare" in candidates[0]["spellings"]


def test_the_attacker_is_not_a_candidate_by_role_field_or_text_but_a_description_is_only_noted(conn):
    _event(conn, "e1", "page", links=[("OpenAI", "threat_actor"), ("Hugging Face", "bystander")])
    assert [c["name"] for c in vj.replacement_candidates(conn, "e1", [])] == ["Hugging Face"]
    _event(conn, "e2", "page", links=[("Acme Corp", "bystander"), ("Hugging Face", "bystander")], attacker="Acme Corp")
    assert [c["name"] for c in vj.replacement_candidates(conn, "e2", [])] == ["Hugging Face"]
    # A description that merely contains the name is not decided here: no word list says it is a tool.
    _event(conn, "e3", "page", links=[("OpenAI", "victim")], attacker="Unknown / autonomous OpenAI AI agent")
    parties = vj.acting_parties(conn, "e3")
    assert vj.is_acting_party("OpenAI", parties) is None
    assert "autonomous OpenAI AI agent" in vj.attacker_field_note("OpenAI", parties)
    state = vj.build_state(conn, "e3", _entity(conn, "OpenAI"), "OpenAI")
    assert "note_on_recorded_victim" in state
    other = vj.build_state(conn, "e2", _entity(conn, "Hugging Face"), "Hugging Face")
    assert "note_on_recorded_victim" not in other             # every other pair's input is unchanged


def test_a_product_named_after_an_organisation_offers_the_organisation_not_a_fold(conn):
    _event(conn, "e1", MEDICARE_TEXT, links=[("Medicare statistics portal", "product", "product")])
    conn.execute("INSERT INTO EntityAliases (raw_name, canonical_name, canonical_key, source) "
                 "VALUES ('Medicare', 'Services Australia', 'services australia', 'human')")
    _entity(conn, "Services Australia", "government_body")
    candidates = vj.replacement_candidates(conn, "e1", [])
    assert [c["name"] for c in candidates] == ["Services Australia"] and "product" in candidates[0]["sources"]
    assert _entity(conn, "Medicare statistics portal") != _entity(conn, "Services Australia")


def test_an_unsettled_choice_is_escalated_and_a_failed_escalation_is_an_error(conn, tmp_path):
    _event(conn, "e1", NSW_TEXT)
    extractor = _extractor(["NSW National Parks and Wildlife Service"])
    judge = JevJudge(conn, client=FakeJev(pick="NSW National Parks and Wildlife Service", pick_p=0.6))

    class Broken:
        chat = completions = None

    broken = Broken()
    broken.chat = broken.completions = broken

    def create_with_completion(**kwargs):
        raise TimeoutError("openai down")

    broken.create_with_completion = create_with_completion
    result = vf.fill_victims(conn, judge=judge, extractor=extractor, escalation_client=broken,
                             calibration_path=_calibration(tmp_path), labels_path=_labels(tmp_path))
    assert result["errors"] and result["counts"] == {}


def test_a_replacement_for_a_wrong_victim_also_sees_the_text_named_agency(conn):
    _event(conn, "e1", NSW_TEXT, victims=["OpenAI"], links=[("OpenAI", "victim")])
    extractor = _extractor(["NSW National Parks and Wildlife Service"], ["OpenAI"])
    judge = JevJudge(conn, client=FakeJev(pick="NSW National Parks and Wildlife Service"))
    choice = vj.choose_replacement(conn, "e1", ["OpenAI"], judge, extractor=extractor)
    assert choice["name"] == "NSW National Parks and Wildlife Service"


# ------------------------------------------------------------------ the proper-name judgement

def test_names_are_judged_once_and_act_only_with_a_calibration_or_a_label(conn, tmp_path):
    _event(conn, "e1", "page", victims=["government agency in Australia", "Services Australia"])
    fake = FakeJev(proper={"government agency in Australia": 0.03, "Services Australia": 0.97})
    judge = JevJudge(conn, client=fake)
    scores = nj.judge_new_names(conn, judge=judge)
    assert scores == {"Services Australia": 0.97, "government agency in Australia": 0.03}
    assert nj.names_to_judge(conn) == []
    nj.judge_new_names(conn, judge=judge)
    assert fake.asked.count(nj.QUESTION_ID) == 2              # asked once per name
    # No calibration, no labels: nothing is a description.
    assert nj.activate(conn, tmp_path / "none.json", tmp_path / "none.csv") == 0
    assert not nj.is_descriptive_name("government agency in Australia")
    cal = tmp_path / "cal.json"
    cal.write_text(json.dumps({**nj.calibration_identity(), "cutoff": 0.5}), encoding="utf-8")
    assert nj.activate(conn, cal, tmp_path / "none.csv") == 1
    assert nj.is_descriptive_name("Government Agency in Australia") and not nj.is_descriptive_name("Services Australia")
    # Labels are ground truth: a name labelled named is never a description, one labelled descriptive always is.
    labels = tmp_path / "names.csv"
    labels.write_text("name,label,rationale,labeller\ngovernment agency in Australia,named,r,t\n"
                      "Services Australia,descriptive,r,t\n", encoding="utf-8")
    nj.activate(conn, cal, labels)
    assert not nj.is_descriptive_name("government agency in Australia") and nj.is_descriptive_name("Services Australia")


def test_a_description_is_not_a_victim_candidate_or_a_usable_victim(conn, tmp_path):
    _event(conn, "e1", "page", links=[("Australian health department", "bystander"), ("Hugging Face", "bystander")],
           victims=["Australian health department"])
    labels = tmp_path / "names.csv"
    labels.write_text("name,label,rationale,labeller\nAustralian health department,descriptive,r,t\n", encoding="utf-8")
    nj.activate(conn, tmp_path / "none.json", labels)
    assert [e["deduplicated_event_id"] for e in vf.victimless_events(conn)] == ["e1"]       # a description is no victim
    assert [c["name"] for c in vj.replacement_candidates(conn, "e1", [])] == ["Hugging Face"]
    from cyber_data_collector.dedup.entity_classification import EntityAssignment, EventEntityRoles
    roles = EventEntityRoles(assignments=[EntityAssignment(name="Australian health department",
                                                           kind="government_body", role="victim", confidence=0.9)])
    assert roles.victim() is None


# ------------------------------------------------------------------ role classification

def test_the_classifier_is_shown_the_members_descriptions_when_the_event_has_no_summary(conn):
    from cyber_data_collector.dedup.entity_classification import classifier_detail

    _event(conn, "e1", "page", description="Breach of a government services website containing Medicare data.")
    assert "Medicare data" in classifier_detail(conn, "e1", None)
    assert classifier_detail(conn, "e1", "The event's own summary") == "The event's own summary"


def test_an_incident_with_no_victim_is_counted_and_an_older_prompt_makes_it_stale(conn):
    _event(conn, "e1", "page", links=[("OpenAI", "bystander")])
    _event(conn, "e2", "page", victims=["Optus"])
    from cyber_data_collector.dedup.role_maintenance import mark_classified

    mark_classified(conn, "e1")
    mark_classified(conn, "e2")
    report = staleness_report(conn)
    assert report["no_victim"] == 1 and report["current"] == 2 and report["prompt_outdated"] == 0
    conn.execute("UPDATE DeduplicatedEvents SET roles_prompt_version = 'entity-roles-v1'")
    report = staleness_report(conn)
    assert report["prompt_outdated"] == 1 and report["current"] == 1       # only the event still without a victim
    assert stale_event_ids(conn) == ["e1"]
    mark_classified(conn, "e1")
    assert staleness_report(conn)["prompt_outdated"] == 0


# ------------------------------------------------------------------ the acceptance measurement

def test_the_acceptance_check_resolves_aliases_and_merged_events(conn, tmp_path):
    from cyber_data_collector.quality import regression

    _event(conn, "a", "page", victims=["Services Australia"])
    _event(conn, "b", "page", victims=["Australian Medicare"])
    conn.execute("INSERT INTO EntityAliases (raw_name, canonical_name, canonical_key, source) "
                 "VALUES ('Australian Medicare', 'Services Australia', 'services australia', 'llm')")
    path = tmp_path / regression.ACCEPTANCE_FILE
    path.write_text("deduplicated_event_id,expectation,value,note,source\n"
                    "a,victim,Services Australia,n,t\nb,victim,Services Australia,n,t\n"
                    "b,same_incident_as,a,n,t\nx,out_of_scope,,n,t\n", encoding="utf-8")
    found = regression.measure_acceptance(conn, evaluation_dir=tmp_path)
    assert found["acceptance.victim.failures"].value == 0           # the alias resolves
    assert found["acceptance.same_incident.failures"].value == 1    # two Active events
    assert found["acceptance.unchecked"].value == 1                 # out_of_scope is the scope check's
    conn.execute("UPDATE DeduplicatedEvents SET status = 'Merged' WHERE deduplicated_event_id = 'b'")
    conn.execute("UPDATE EventDeduplicationMap SET deduplicated_event_id = 'a' WHERE deduplicated_event_id = 'b'")
    conn.execute("UPDATE DeduplicatedEvents SET master_enriched_event_id = 'b-r' WHERE deduplicated_event_id = 'b'")
    assert regression.measure_acceptance(conn, evaluation_dir=tmp_path)["acceptance.same_incident.failures"].value == 0
