"""Supplier incidents: detecting them, finding co-victims, judging with Jev, acting only when
calibrated, merging the same incident recorded under several customers - and counting each
victim organisation once."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("typesafe_sdk")

from cyber_data_collector.dedup import supplier_victims as sv  # noqa: E402
from cyber_data_collector.dedup.ledger import DedupLedger  # noqa: E402

SCHEMA = """
CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT, entity_kind TEXT,
    employee_count INTEGER, turnover TEXT, size_estimate TEXT, size_confidence REAL,
    size_employees INTEGER, size_revenue_aud REAL, industry TEXT);
CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, title TEXT, event_date TEXT,
    victim_organization_name TEXT, vendor_organization_name TEXT, status TEXT, summary TEXT,
    description TEXT, updated_at TEXT);
CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER,
    relationship_type TEXT, confidence_score REAL, source_count INTEGER);
CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title TEXT,
    description TEXT, perplexity_enrichment_data TEXT);
CREATE TABLE EnrichedEventEntities (enriched_event_id TEXT, entity_id INTEGER, relationship_type TEXT,
    confidence_score REAL);
CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_url TEXT, raw_content TEXT,
    raw_description TEXT);
CREATE TABLE DedupSnapshots (snapshot_id TEXT, batch_id TEXT, deduplicated_event_id TEXT,
    payload_json TEXT);
CREATE TABLE DedupOverrides (override_id TEXT PRIMARY KEY, pair_key TEXT UNIQUE, left_enriched_event_id TEXT,
    right_enriched_event_id TEXT, verdict TEXT, reason TEXT, created_by TEXT, created_at TEXT,
    active INTEGER DEFAULT 1);
"""

ENTITIES = [(1, "ProctorU", "organisation"), (2, "University of Sydney", "organisation"),
            (3, "University of Tasmania", "organisation"), (4, "Curtin University", "organisation"),
            (5, "Honi Soit", "organisation"), (6, "Australian universities", "organisation"),
            (7, "Canvas", "product"), (8, "Acme Bakery", "organisation"),
            (9, "Monash University", "organisation")]


def _event(c, did, title, date, victim=None, vendor=None, records=(), status="Active"):
    c.execute("INSERT INTO DeduplicatedEvents VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
              (did, title, date, victim, vendor, status))
    for rid, rtitle, url, formal, links in records:
        c.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?)", (rid, did))
        c.execute("INSERT INTO EnrichedEvents VALUES (?, ?, ?, NULL, ?)",
                  (rid, rid, rtitle, json.dumps({"formal_entity_name": formal}) if formal else None))
        c.execute("INSERT INTO RawEvents VALUES (?, ?, 'page text', NULL)", (rid, url))
        for eid, role in links:
            c.execute("INSERT INTO EnrichedEventEntities VALUES (?, ?, ?, NULL)", (rid, eid, role))


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    c.executemany("INSERT INTO EntitiesV2 (entity_id, entity_name, entity_kind) VALUES (?, ?, ?)", ENTITIES)
    # The supplier incident: vendor ProctorU, one recorded victim, a customer linked on a record,
    # a student paper (bystander) and a collective.
    _event(c, "e1", "ProctorU data breach affects Australian universities", "2020-08-07",
           vendor="ProctorU",
           records=[("r1", "ProctorU breach exposes students", "https://news.example/proctoru", "ProctorU LLC",
                     [(1, "vendor"), (2, "victim"), (3, "affected_customer"), (5, "bystander"),
                      (6, "affected_customer"), (7, "affected")])])
    c.executemany("INSERT INTO DeduplicatedEventEntities VALUES (?, ?, ?, NULL, 1)",
                  [("e1", 1, "vendor"), ("e1", 2, "victim")])
    # The same supplier incident recorded under another customer.
    _event(c, "e2", "Curtin University students caught up in ProctorU breach", "2020-08-20",
           victim="Curtin University", vendor="ProctorU",
           records=[("r2", "Curtin confirms ProctorU exposure", "https://news.example/curtin", "Curtin University",
                     [(4, "victim"), (1, "vendor")])])
    c.executemany("INSERT INTO DeduplicatedEventEntities VALUES (?, ?, ?, NULL, 1)",
                  [("e2", 4, "victim"), ("e2", 1, "vendor")])
    # An unrelated ordinary event.
    _event(c, "e3", "Acme Bakery ransomware attack", "2021-03-01", victim="Acme Bakery",
           records=[("r3", "Bakery hit by ransomware", "https://news.example/acme", "Acme Bakery", [(8, "victim")])])
    c.execute("INSERT INTO DeduplicatedEventEntities VALUES ('e3', 8, 'victim', NULL, 1)")
    c.commit()
    return c


class FakeTypeSafe:
    """Answers every question with a fixed probability per subject (default ``p``)."""

    def __init__(self, p=0.95, by_subject=None):
        self.p, self.by_subject, self.calls = p, by_subject or {}, 0

    def system_one(self, state, questions, model=None):
        self.calls += 1
        key = json.dumps(state, sort_keys=True, default=str)
        p = next((v for k, v in self.by_subject.items() if k in key), self.p)
        return SimpleNamespace(model="jev-1.14.0", usage=None,
                               answers={q: SimpleNamespace(type="noul", noul=p) for q in questions})


def _judge(conn, **kw):
    from cyber_data_collector.utils.jev import JevJudge
    return JevJudge(conn, client=FakeTypeSafe(**kw))


def _calibration(tmp_path, victim=0.9, same=0.9, identity=None, different=0.5):
    path = tmp_path / "cal.json"
    path.write_text(json.dumps({**(identity or sv.calibration_identity()),
                                "victim": {"cutoff": victim},
                                "same_incident": {"cutoff": same, "different_cutoff": different}}))
    return path


def _labels(tmp_path, rows=()):
    path = tmp_path / "labels.csv"
    path.write_text("kind,subject_id,label,rationale,labeller\n"
                    + "".join(f"{k},{s},{lab},reason,test\n" for k, s, lab in rows), encoding="utf-8")
    return path


@pytest.fixture
def merge_events(monkeypatch):
    """Stand-in for the ledger merge: move members, retire the source, honour 'different' rulings."""
    calls = []
    refused = set()

    def fake(self, target, source, reason, actor="human", respect_overrides=True):
        if (target, source) in refused or (source, target) in refused:
            raise ValueError("blocked by a 'different' override")
        self.conn.execute("UPDATE EventDeduplicationMap SET deduplicated_event_id = ? WHERE deduplicated_event_id = ?",
                          (target, source))
        self.conn.execute("UPDATE DeduplicatedEvents SET status = 'Merged' WHERE deduplicated_event_id = ?", (source,))
        calls.append((target, source))

    monkeypatch.setattr(DedupLedger, "merge_events", fake)
    return SimpleNamespace(calls=calls, refused=refused)


# ----------------------------------------------------------------------
# Detection and candidates
# ----------------------------------------------------------------------

@pytest.mark.parametrize("name, kind, ok", [
    ("University of Tasmania", "organisation", True),
    ("Australian universities", None, False),        # a collective
    ("Queensland education sector", None, False),    # a sector
    ("Canvas", "product", False),
    ("Anna Patty", "person", False),
    ("Monash University", None, True),                # a person's shape, but an organisation
    ("ShinyHunters", "threat_actor", False),
    ("OAIC", None, False),                            # a regulator
    ("unknown", None, False),
])
def test_candidate_organisations(name, kind, ok):
    assert sv.is_candidate_organisation(name, kind) is ok


def test_detects_supplier_incidents_only(conn):
    found = {i.dedup_id: i for i in sv.supplier_incidents(conn)}
    assert set(found) == {"e1", "e2"}
    assert found["e1"].vendors == ["ProctorU"]
    assert found["e1"].searchable


def test_victim_candidates_come_from_records_not_mentions(conn):
    inc = next(i for i in sv.supplier_incidents(conn) if i.dedup_id == "e1")
    resolver = sv.EntityResolver(conn)
    names = [c.name for c in sv.victim_candidates(conn, inc, resolver)]
    # The linked customer is a candidate; the recorded victim, the vendor (also as Perplexity's
    # "ProctorU LLC"), the bystander paper, the collective and the 'affected' product are not.
    assert names == ["University of Tasmania"]


def test_search_names_become_candidates(conn):
    inc = next(i for i in sv.supplier_incidents(conn) if i.dedup_id == "e1")
    search = {"answer": "AFFECTED: University of Sydney | students | https://x\n"
                        "AFFECTED: Monash University | exam records exposed | https://y\n"
                        "AFFECTED: ProctorU | the supplier | https://z"}
    cands = sv.victim_candidates(conn, inc, sv.EntityResolver(conn), search=search)
    monash = next(c for c in cands if c.name == "Monash University")
    assert monash.entity_id == 9 and monash.found_by[0].startswith("named by the supplier search")
    assert {c.name for c in cands} == {"University of Tasmania", "Monash University"}


def test_parse_search_names():
    text = "AFFECTED: The University of Sydney [1] | data | u\nAFFECTED: The University of Sydney | x | y\n"
    assert [n["name"] for n in sv.parse_search_names(text)] == ["The University of Sydney"]
    assert sv.parse_search_names("AFFECTED: NONE FOUND") == []


def test_research_is_stored_and_reused(conn, monkeypatch):
    inc = next(i for i in sv.supplier_incidents(conn) if i.dedup_id == "e1")
    calls = []

    def search(query, model):
        calls.append(query)
        return {"answer": "AFFECTED: Monash University | x | y", "citations": ["https://y"]}

    sv.research_incident(conn, inc, search=search)
    found = sv.research_incident(conn, inc, search=search)
    assert len(calls) == 1 and "ProctorU" in calls[0]
    assert found["citations"] == ["https://y"]


def test_research_without_a_key_fails_loudly(conn, monkeypatch):
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    inc = next(i for i in sv.supplier_incidents(conn) if i.dedup_id == "e1")
    with pytest.raises(sv.SearchUnavailable):
        sv.research_incident(conn, inc)


def test_pairs_include_the_same_supplier_under_another_customer(conn):
    incidents = sv.supplier_incidents(conn)
    resolver = sv.EntityResolver(conn)
    cands = {i.dedup_id: sv.victim_candidates(conn, i, resolver) for i in incidents}
    pairs = sv.pair_candidates(conn, incidents, cands, resolver)
    assert [p.pair_id for p in pairs] == ["e1|e2"]
    assert any("same supplier" in r for r in pairs[0].reasons)


# ----------------------------------------------------------------------
# Judgement: escalation, and never acting on a raw score
# ----------------------------------------------------------------------

def test_failed_escalation_leaves_the_item_undecided(conn, monkeypatch):
    import cyber_data_collector.utils.escalation as esc
    monkeypatch.setattr(esc, "escalate", lambda *a, **k: None)
    scores = sv.judge(conn, [("s1", {"x": 1})], sv.KIND_VICTIM, judge_client=_judge(conn, p=0.5))
    assert scores.undecided == ["s1"] and "s1" not in scores.final
    assert sv.stored_final_probabilities(conn, sv.KIND_VICTIM) == {}


def test_escalation_replaces_the_middle_band(conn, monkeypatch):
    import cyber_data_collector.utils.escalation as esc
    monkeypatch.setattr(esc, "escalate", lambda *a, **k: 0.97)
    scores = sv.judge(conn, [("s1", {"x": 1}), ("s2", {"x": 2})], sv.KIND_VICTIM,
                      judge_client=_judge(conn, p=0.5, by_subject={'"x": 2': 0.99}))
    assert scores.final == {"s1": 0.97, "s2": 0.99}
    assert scores.escalated == {"s1": 0.97}


# ----------------------------------------------------------------------
# Applying: refuse without labels and a matching calibration
# ----------------------------------------------------------------------

def test_apply_refuses_without_labels(conn, tmp_path):
    with pytest.raises(sv.CalibrationError, match="cannot be read"):
        sv.find_supplier_victims(conn, apply=True, judge_client=_judge(conn),
                                 labels_path=tmp_path / "missing.csv",
                                 calibration_path=_calibration(tmp_path))


def test_apply_refuses_malformed_labels(conn, tmp_path):
    with pytest.raises(sv.CalibrationError, match="malformed"):
        sv.find_supplier_victims(conn, apply=True, judge_client=_judge(conn),
                                 labels_path=_labels(tmp_path, [("victim", "e1|3", "maybe")]),
                                 calibration_path=_calibration(tmp_path))


def test_apply_refuses_a_calibration_for_another_model(conn, tmp_path):
    with pytest.raises(sv.CalibrationError, match="recalibrate"):
        sv.find_supplier_victims(conn, apply=True, judge_client=_judge(conn),
                                 labels_path=_labels(tmp_path, [("victim", "x|1", "unsure")]),
                                 calibration_path=_calibration(tmp_path, identity={"model": "old",
                                                                                   "question_version": "v0"}))
    assert conn.execute("SELECT COUNT(*) FROM DeduplicatedEventEntities WHERE relationship_type = 'victim'"
                        ).fetchone()[0] == 3


def test_report_only_changes_nothing(conn):
    result = sv.find_supplier_victims(conn, apply=False, judge_client=_judge(conn))
    assert result["applied"] is False and result["attached"] == [] and result["merged"] == []
    assert conn.execute("SELECT status FROM DeduplicatedEvents WHERE deduplicated_event_id = 'e2'"
                        ).fetchone()[0] == "Active"


# ----------------------------------------------------------------------
# Applying: one incident with several victims
# ----------------------------------------------------------------------

def _victims(conn, did):
    return sorted(r[0] for r in conn.execute(
        """SELECT v.entity_name FROM DeduplicatedEventEntities dee JOIN EntitiesV2 v USING (entity_id)
           WHERE dee.deduplicated_event_id = ? AND dee.relationship_type = 'victim'""", (did,)))


def test_apply_merges_the_same_incident_and_attaches_victims(conn, tmp_path, merge_events):
    result = sv.find_supplier_victims(conn, apply=True, judge_client=_judge(conn),
                                      labels_path=_labels(tmp_path, [("victim", "x|1", "unsure")]),
                                      calibration_path=_calibration(tmp_path))
    assert merge_events.calls == [("e1", "e2")]
    assert _victims(conn, "e1") == ["Curtin University", "University of Sydney", "University of Tasmania"]
    audit = conn.execute("SELECT entity_name, decided_by, merged_from FROM SupplierVictimAttachments "
                         "ORDER BY entity_name").fetchall()
    assert [tuple(a) for a in audit] == [("Curtin University", "merge", "e2"),
                                         ("University of Tasmania", "jev", None)]
    assert [a["victim"] for a in result["attached"]] == ["University of Tasmania"]
    # Idempotent: a second run finds nothing left to do.
    again = sv.find_supplier_victims(conn, apply=True, judge_client=_judge(conn),
                                     labels_path=_labels(tmp_path, [("victim", "x|1", "unsure")]),
                                     calibration_path=_calibration(tmp_path))
    assert again["attached"] == [] and [m for m in again["merged"] if "refused" not in m] == []


def test_labels_are_ground_truth(conn, tmp_path, merge_events):
    labels = _labels(tmp_path, [("victim", "e1|3", "not_affected"), ("same_incident", "e1|e2", "different")])
    result = sv.find_supplier_victims(conn, apply=True, judge_client=_judge(conn, p=0.99),
                                      labels_path=labels, calibration_path=_calibration(tmp_path))
    assert merge_events.calls == [] and result["attached"] == []
    assert _victims(conn, "e1") == ["University of Sydney"]
    # ... and a positive label acts even below the cut-off.
    labels = _labels(tmp_path, [("victim", "e1|3", "affected"), ("same_incident", "e1|e2", "different")])
    result = sv.find_supplier_victims(conn, apply=True, judge_client=_judge(conn, p=0.05),
                                      labels_path=labels, calibration_path=_calibration(tmp_path))
    assert [(a["victim"], a["decided_by"]) for a in result["attached"]] == [("University of Tasmania", "label")]


def test_no_cutoff_means_no_model_action(conn, tmp_path, merge_events):
    result = sv.find_supplier_victims(conn, apply=True, judge_client=_judge(conn, p=0.99),
                                      labels_path=_labels(tmp_path, [("victim", "x|1", "unsure")]),
                                      calibration_path=_calibration(tmp_path, victim=None, same=None))
    assert merge_events.calls == [] and result["attached"] == []


def test_a_refused_merge_holds_the_victim_back(conn, tmp_path, merge_events):
    """Curtin's own event is the same incident but a cached ruling forbids the merge: attaching
    Curtin to the supplier incident as well would count it twice."""
    merge_events.refused.add(("e1", "e2"))
    conn.execute("INSERT INTO EnrichedEventEntities VALUES ('r1', 4, 'affected_customer', NULL)")
    result = sv.find_supplier_victims(conn, apply=True, judge_client=_judge(conn),
                                      labels_path=_labels(tmp_path, [("victim", "x|1", "unsure")]),
                                      calibration_path=_calibration(tmp_path))
    assert [m.get("refused") is not None for m in result["merged"]] == [True]
    assert [(h["victim"], h["own_event"]) for h in result["held"]] == [("Curtin University", "e2")]
    assert "ruled the same incident (model) but not merged" in result["held"][0]["why"]
    assert "Curtin University" not in _victims(conn, "e1")


# ----------------------------------------------------------------------
# A victim with its own event near the incident: decided by the ruling on that pair
# ----------------------------------------------------------------------

def _curtin_is_a_candidate(conn):
    """Curtin is linked on the supplier incident's record, and has its own event e2 (pair e1|e2)."""
    conn.execute("INSERT INTO EnrichedEventEntities VALUES ('r1', 4, 'affected_customer', NULL)")


def _override(conn, verdict, left="r1", right="r2", active=1):
    conn.execute("INSERT INTO DedupOverrides VALUES (?, ?, ?, ?, ?, 'test', 'human', NULL, ?)",
                 (f"o-{verdict}-{active}", f"{left}|{right}|{verdict}", left, right, verdict, active))


def _apply_curtin(conn, tmp_path, p, pair_label=None, calibration=None, victim_label="affected"):
    """Curtin's victim candidacy is accepted by label, so only the pair ruling varies."""
    rows = [("victim", "e1|4", victim_label)]
    if pair_label:
        rows.append(("same_incident", "e1|e2", pair_label))
    return sv.find_supplier_victims(conn, apply=True, judge_client=_judge(conn, p=p),
                                    labels_path=_labels(tmp_path, rows),
                                    calibration_path=calibration or _calibration(tmp_path))


def test_own_event_ruled_the_same_by_override_is_merged_not_held(conn, tmp_path, merge_events):
    _curtin_is_a_candidate(conn)
    _override(conn, "same")
    result = _apply_curtin(conn, tmp_path, p=0.05)   # the model alone would say a different incident
    assert merge_events.calls == [("e1", "e2")]
    assert result["held"] == []
    assert "Curtin University" in _victims(conn, "e1")
    assert conn.execute("SELECT decided_by FROM SupplierVictimAttachments WHERE entity_name = 'Curtin University'"
                        ).fetchone()[0] == "merge"


def test_own_event_ruled_the_same_by_label_is_merged_not_held(conn, tmp_path, merge_events):
    _curtin_is_a_candidate(conn)
    result = _apply_curtin(conn, tmp_path, p=0.05, pair_label="same")
    assert merge_events.calls == [("e1", "e2")]
    assert result["held"] == [] and "Curtin University" in _victims(conn, "e1")


def test_own_event_ruled_the_same_by_the_model_is_merged_not_held(conn, tmp_path, merge_events):
    _curtin_is_a_candidate(conn)
    result = _apply_curtin(conn, tmp_path, p=0.95)
    assert merge_events.calls == [("e1", "e2")]
    assert result["held"] == [] and "Curtin University" in _victims(conn, "e1")


@pytest.mark.parametrize("how", ["override", "label", "model"])
def test_own_event_ruled_different_attaches_the_customer(conn, tmp_path, merge_events, how):
    _curtin_is_a_candidate(conn)
    if how == "override":
        _override(conn, "different")
    result = _apply_curtin(conn, tmp_path, p=0.05, pair_label="different" if how == "label" else None)
    assert merge_events.calls == [] and result["held"] == []
    assert "Curtin University" in _victims(conn, "e1")
    attached = next(a for a in result["attached"] if a["victim"] == "Curtin University")
    assert [(r["own_event"], r["verdict"], r["basis"]) for r in attached["own_events_ruled_different"]] == [
        ("e2", "different", how)]
    # Its own event stays, as a separate incident.
    assert conn.execute("SELECT status FROM DeduplicatedEvents WHERE deduplicated_event_id = 'e2'"
                        ).fetchone()[0] == "Active"


def test_a_different_override_beats_a_confident_model(conn, tmp_path, merge_events):
    _curtin_is_a_candidate(conn)
    _override(conn, "different")
    result = _apply_curtin(conn, tmp_path, p=0.99)
    assert result["held"] == [] and "Curtin University" in _victims(conn, "e1")


def test_conflicting_rulings_are_held_with_the_reason(conn, tmp_path, merge_events):
    _curtin_is_a_candidate(conn)
    _override(conn, "different")
    result = _apply_curtin(conn, tmp_path, p=0.05, pair_label="same")
    assert merge_events.calls == []
    assert [(h["victim"], h["own_event"]) for h in result["held"]] == [("Curtin University", "e2")]
    assert "conflicting rulings: label says same, override says different" in result["held"][0]["why"]
    assert "Curtin University" not in _victims(conn, "e1")


def test_no_cutoff_leaves_an_unruled_pair_held(conn, tmp_path, merge_events):
    _curtin_is_a_candidate(conn)
    result = _apply_curtin(conn, tmp_path, p=0.95, calibration=_calibration(tmp_path, same=None))
    assert merge_events.calls == []
    assert "no calibrated same-incident cut-off" in result["held"][0]["why"]


def test_an_inactive_override_is_no_ruling(conn, tmp_path, merge_events):
    _curtin_is_a_candidate(conn)
    _override(conn, "different", active=0)
    result = _apply_curtin(conn, tmp_path, p=0.95)
    assert merge_events.calls == [("e1", "e2")] and result["held"] == []


def test_a_pair_nobody_judged_is_held_and_says_why(conn):
    ruling = sv.rule_on_own_event(conn, "e1", "e2", {}, sv.Scores(), 0.9, known_pairs=[])
    assert ruling.verdict == sv.UNRESOLVED and "never a candidate" in ruling.basis
    failed = sv.Scores(failed=["e1|e2"])
    assert "Jev failed" in sv.rule_on_own_event(conn, "e1", "e2", {}, failed, 0.9, ["e1|e2"]).basis
    undecided = sv.Scores(undecided=["e1|e2"])
    assert "escalation band" in sv.rule_on_own_event(conn, "e1", "e2", {}, undecided, 0.9, ["e1|e2"]).basis


def test_ruling_order_labels_and_overrides_before_the_model(conn):
    scores = sv.Scores(final={"e1|e2": 0.5})
    assert sv.rule_on_own_event(conn, "e1", "e2", {}, scores, 0.9, different_cutoff=0.6).verdict == sv.RULED_DIFFERENT
    assert sv.rule_on_own_event(conn, "e1", "e2", {}, scores, 0.5).verdict == sv.RULED_SAME   # at the cut-off
    unsure = {("same_incident", "e1|e2"): "unsure"}
    assert sv.rule_on_own_event(conn, "e1", "e2", unsure, scores, 0.9, different_cutoff=0.6).basis == "model"
    assert sv.rule_on_own_event(conn, "e1", "e2", {("same_incident", "e1|e2"): "same"}, scores, 0.9
                                ).basis == "label"


def test_a_score_between_the_two_bounds_is_held_and_the_band_is_named(conn):
    """The merge cut-off measured only the precision of merging: 0.85 under a 0.87 cut-off is not 'different'."""
    for p, expected in ((0.95, sv.RULED_SAME), (0.87, sv.RULED_SAME), (0.85, sv.UNRESOLVED),
                        (0.78, sv.UNRESOLVED), (0.7799, sv.RULED_DIFFERENT), (0.1, sv.RULED_DIFFERENT)):
        ruling = sv.rule_on_own_event(conn, "e1", "e2", {}, sv.Scores(final={"e1|e2": p}), 0.87,
                                      different_cutoff=0.78)
        assert ruling.verdict == expected, p
    band = sv.rule_on_own_event(conn, "e1", "e2", {}, sv.Scores(final={"e1|e2": 0.85}), 0.87, different_cutoff=0.78)
    assert "between the calibrated 'different' bound (0.78) and the merge cut-off (0.87)" in band.basis


def test_without_a_different_bound_a_model_score_never_rules_different(conn):
    ruling = sv.rule_on_own_event(conn, "e1", "e2", {}, sv.Scores(final={"e1|e2": 0.05}), 0.87)
    assert ruling.verdict == sv.UNRESOLVED and "no calibrated bound for 'different'" in ruling.basis
    # A label still rules.
    assert sv.rule_on_own_event(conn, "e1", "e2", {("same_incident", "e1|e2"): "different"},
                                sv.Scores(final={"e1|e2": 0.05}), 0.87).verdict == sv.RULED_DIFFERENT


def test_apply_holds_a_pair_in_the_band_and_attaches_one_below_the_different_bound(conn, tmp_path, merge_events):
    _curtin_is_a_candidate(conn)
    cal = _calibration(tmp_path, same=0.95, different=0.82)
    # The pair scores 0.9: under the 0.95 merge cut-off, above the 0.82 'different' bound -> in the band.
    held = _apply_curtin(conn, tmp_path, p=0.9, calibration=cal)
    assert merge_events.calls == [] and "Curtin University" not in _victims(conn, "e1")
    assert [h["victim"] for h in held["held"]] == ["Curtin University"]
    assert "between the calibrated 'different' bound (0.82) and the merge cut-off (0.95)" in held["held"][0]["why"]
    assert held["cutoffs"]["different"] == 0.82
    # Below the bound the same pair (its stored 0.9 is reused) is a different incident: the customer is attached.
    attached = _apply_curtin(conn, tmp_path, p=0.9, calibration=_calibration(tmp_path, same=0.95, different=0.92))
    assert attached["held"] == [] and "Curtin University" in _victims(conn, "e1")
    # With no 'different' bound recorded (an older calibration) the same score is held.
    conn.execute("DELETE FROM DeduplicatedEventEntities WHERE deduplicated_event_id = 'e1' AND entity_id = 4")
    older = _apply_curtin(conn, tmp_path, p=0.9, calibration=_calibration(tmp_path, same=0.95, different=None))
    assert [h["victim"] for h in older["held"]] == ["Curtin University"]
    assert "no calibrated bound for 'different'" in older["held"][0]["why"]



# ----------------------------------------------------------------------
# Counting once per organisation
# ----------------------------------------------------------------------

from cyber_data_collector.dedup.victim_counting import (  # noqa: E402
    one_row_per_organisation, organisation_groups, same_organisation,
)
from cyber_data_collector.dedup.entity_resolution import EntityResolver  # noqa: E402


@pytest.mark.parametrize("left, right, same", [
    ("Medibank", "Medibank Private Limited", True),
    ("Meta", "Meta Platforms, Inc.", True),
    ("Australian National University", "The Australian National University", True),
    ("Football Australia", "Fremantle Football Club", False),
    ("Service NSW", "NSW Government", False),
    ("Sharp Motor Group", "Sharp Office Systems", False),
    ("Super SA", "Super VPN", False),
    ("University of Sydney", "University of Technology Sydney", False),
])
def test_strict_same_organisation(left, right, same):
    assert same_organisation(EntityResolver(), left, right) is same


def _counting_db(path: Path) -> None:
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT, employee_count INTEGER,
            turnover TEXT, entity_kind TEXT, size_estimate TEXT, size_confidence REAL, size_employees INTEGER,
            size_revenue_aud REAL, size_basis TEXT, industry TEXT);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, master_enriched_event_id TEXT,
            title TEXT, summary TEXT, description TEXT, event_date TEXT, event_type TEXT, records_affected INTEGER,
            vendor_organization_name TEXT, date_confidence REAL, confidence_score REAL, total_data_sources INTEGER,
            victim_organization_industry TEXT, is_australian_event INTEGER, is_specific_event INTEGER,
            status TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        CREATE TABLE EventDeduplicationMap (deduplicated_event_id TEXT, enriched_event_id TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, summary TEXT, description TEXT);
    """)
    c.executemany("INSERT INTO EntitiesV2 (entity_id, entity_name, entity_kind, size_estimate) VALUES (?,?,?,?)",
                  [(1, "University of Sydney", "organisation", "HUGE"),
                   (2, "University of Tasmania", "organisation", "LARGE"),
                   (3, "Medibank", "organisation", "HUGE"),
                   (4, "Medibank Private Limited", "organisation", None),
                   (5, "ProctorU", "organisation", None)])
    events = [("p1", "ProctorU breach", "2020-08-07", "ProctorU"),
              ("m1", "Medibank breach", "2022-10-13", None),
              ("m2", "Medibank follow-up attack", "2023-06-01", None)]
    for did, title, date, vendor in events:
        c.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, master_enriched_event_id, title, "
                  "event_date, vendor_organization_name, status) VALUES (?, ?, ?, ?, ?, 'Active')",
                  (did, did + "-m", title, date, vendor))
    c.executemany("INSERT INTO DeduplicatedEventEntities VALUES (?,?,?)", [
        ("p1", 1, "victim"), ("p1", 2, "victim"), ("p1", 5, "vendor"),   # two co-equal victims
        ("m1", 3, "victim"), ("m1", 4, "victim"),                        # one organisation, two names
        ("m2", 3, "victim"),                                             # and a later event
    ])
    c.commit()
    c.close()


def test_analysis_counts_each_victim_once_and_each_organisation_once(tmp_path):
    from scripts.analyze_recurrent_timing import load_victim_events

    db = tmp_path / "count.db"
    _counting_db(db)
    df = load_victim_events(db)
    pairs = sorted(zip(df["entity_name"], df["deduplicated_event_id"]))
    assert pairs == [("Medibank", "m1"), ("Medibank", "m2"),
                     ("University of Sydney", "p1"), ("University of Tasmania", "p1")]
    # The variant is the same organisation: one entity with a repeat, sized as Medibank.
    medibank = df[df["entity_name"] == "Medibank"]
    assert medibank["entity_id"].nunique() == 1 and set(medibank["org_size"]) == {"HUGE"}


def test_entity_dashboard_lists_each_victim_once(tmp_path):
    from scripts.build_entity_dashboard import load_rows, summarise

    db = tmp_path / "count.db"
    _counting_db(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    rows = load_rows(c)
    assert sorted((r["entity"], r["event_id"]) for r in rows) == [
        ("Medibank", "m1"), ("Medibank", "m2"), ("University of Sydney", "p1"), ("University of Tasmania", "p1")]
    s = summarise(rows)
    assert s["entities"] == 3 and s["events"] == 3 and s["multi_entities"] == 1


def test_one_row_per_organisation():
    groups = {3: 3, 4: 3}
    assert one_row_per_organisation([(3, "m1"), (4, "m1"), (4, "m2"), (1, "p1"), (2, "p1")], groups) == [
        (3, "m1"), (3, "m2"), (1, "p1"), (2, "p1")]


def test_groups_use_complete_linkage(tmp_path):
    c = sqlite3.connect(":memory:")
    c.executescript("CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT);"
                    "CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT, status TEXT);"
                    "CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, "
                    "relationship_type TEXT);")
    c.executemany("INSERT INTO EntitiesV2 VALUES (?, ?)",
                  [(1, "Federal Court"), (2, "Federal Group"), (3, "Australian Federal Police"),
                   (4, "Medibank"), (5, "Medibank Private Limited")])
    groups = organisation_groups(c, [1, 2, 3, 4, 5])
    assert len({groups[i] for i in (1, 2, 3)}) == 3
    assert groups[4] == groups[5]


# ----------------------------------------------------------------------
# One organisation, two names, one event
# ----------------------------------------------------------------------

from cyber_data_collector.dedup.victim_counting import same_within_event, within_event_duplicates  # noqa: E402


@pytest.mark.parametrize("left, right, same", [
    ("Australian Red Cross", "Australian Red Cross Society", True),
    ("Block (ASX:SQ2)", "Block, Inc. (Cash App Investing)", True),
    ("Australian Red Cross", "International Committee of the Red Cross", False),
    ("City of Ballarat", "City of Greater Dandenong", False),
    ("University of Sydney", "University of Technology Sydney", False),
])
def test_same_within_event(left, right, same):
    assert same_within_event(EntityResolver(), left, right) is same


@pytest.mark.parametrize("name, ok", [("Sydney radiologist", False), ("Swedish pharmaceutical chain", False),
                                      ("Bush Heritage Australia", True), ("Block (ASX:SQ2)", True)])
def test_descriptions_are_not_victims(name, ok):
    assert sv.is_candidate_organisation(name, "organisation") is ok


def test_another_name_for_a_victim_is_not_attached(conn, tmp_path, merge_events):
    conn.execute("INSERT INTO EntitiesV2 (entity_id, entity_name, entity_kind) VALUES "
                 "(20, 'Acme Research Society', 'organisation')")
    conn.execute("INSERT INTO EntitiesV2 (entity_id, entity_name, entity_kind) VALUES "
                 "(21, 'Acme Research', 'organisation')")
    conn.execute("INSERT INTO DeduplicatedEventEntities VALUES ('e1', 21, 'victim', NULL, 1)")
    conn.execute("INSERT INTO EnrichedEventEntities VALUES ('r1', 20, 'affected_customer', NULL)")
    result = sv.find_supplier_victims(conn, apply=True, judge_client=_judge(conn),
                                      labels_path=_labels(tmp_path, [("victim", "x|1", "unsure")]),
                                      calibration_path=_calibration(tmp_path))
    assert [(v["candidate"], v["victim"]) for v in result["variants"]] == [
        ("Acme Research Society", "Acme Research")]
    assert "Acme Research Society" not in _victims(conn, "e1")


def test_within_event_duplicates_keep_one_name():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, "
              "relationship_type TEXT)")
    c.executemany("INSERT INTO DeduplicatedEventEntities VALUES (?, ?, 'victim')",
                  [("z1", 1), ("z1", 2), ("z1", 3), ("z2", 1)])
    rows = [(1, "Australian Red Cross", "z1"), (2, "Australian Red Cross Society", "z1"),
            (3, "International Committee of the Red Cross", "z1"), (1, "Australian Red Cross", "z2")]
    assert within_event_duplicates(c, rows, EntityResolver()) == {(2, "z1")}


# ----------------------------------------------------------------------
# Recalibrating on a labelling-time snapshot
# ----------------------------------------------------------------------

@pytest.fixture
def recal(conn, tmp_path, monkeypatch):
    """A labelling-time snapshot (the fixture database), a live database holding one stored search, and the
    label, sample and calibration files for one labelled victim and one labelled pair."""
    from scripts import calibrate_supplier_victims as cal

    instance = tmp_path / "instance"
    instance.mkdir()
    snapshot = instance / "labelling.db"
    out = sqlite3.connect(snapshot)
    conn.backup(out)
    out.close()
    live = instance / "live.db"
    lc = sqlite3.connect(live)
    sv.ensure_schema(lc)
    from cyber_data_collector.dedup import corroboration, org_timeline
    org_timeline.ensure_schema(lc)
    corroboration.ensure_schema(lc)
    lc.execute("INSERT INTO SupplierIncidentSearches VALUES ('e1', 'h1', 'q', 'sonar', ?, '[]', '2026-10-28')",
               ("AFFECTED: Monash University | exam records | https://y",))
    lc.commit()
    lc.close()
    labels = tmp_path / "labels.csv"
    labels.write_text("kind,subject_id,label,rationale,labeller\n"
                      "victim,e1|3,affected,why,test\nsame_incident,e1|e2,same,why,test\n"
                      "victim,e1|name:gone away university,affected,why,test\n", encoding="utf-8")
    sample = {("victim", "e1|3"): {"stratum": "high", "weight": "1"},
              ("same_incident", "e1|e2"): {"stratum": "high", "weight": "1"},
              ("victim", "e1|name:gone away university"): {"stratum": "high", "weight": "1"}}
    calibration = tmp_path / "calibration.json"
    old = {**sv.calibration_identity(), "evidence": "supplier-victims:0000000000000000",
           "victim": {"cutoff": 0.96}, "same_incident": {"cutoff": 0.87}, "generated": "2026-10-27T20:00:00"}
    calibration.write_text(json.dumps(old), encoding="utf-8")
    monkeypatch.setattr(sv, "ROOT", tmp_path)
    monkeypatch.setattr(sv, "LABELS_PATH", labels)
    monkeypatch.setattr(sv, "CALIBRATION_PATH", calibration)
    monkeypatch.setattr(cal, "SAMPLE_PATH", tmp_path / "sample.csv")
    (tmp_path / "sample.csv").write_text("kind,subject_id,stratum,weight\n")
    monkeypatch.setattr(cal, "read_sample", lambda: sample)
    return SimpleNamespace(live=live, snapshot=snapshot, calibration=calibration, labels=labels, sample=sample,
                           tmp=tmp_path)


def _recalibrate(recal, fake, **kwargs):
    from cyber_data_collector.dedup import recalibration as rc
    from cyber_data_collector.utils.jev import JevJudge

    return rc.recalibrate_step("supplier-victims", recal.live, recal.snapshot,
                               judge_factory=lambda c: JevJudge(c, client=fake), escalation_client=object(),
                               verify_keys=False, calibrate_args=["--min-accepts", "1"], **kwargs)


def test_supplier_victims_recalibrate_on_the_snapshot_and_record_the_evidence(recal):
    from cyber_data_collector.dedup import recalibration as rc

    assert rc.calibration_state(rc.SPECS["supplier-victims"])["state"] == "stale"
    fake = FakeTypeSafe(p=0.95)
    # 3 labelled; the one named only by a search that was never stored is gone, 2 of 3 > 65%: scored.
    result = _recalibrate(recal, fake)
    assert (result["cases_scored"], result["cases_labelled"]) == (2, 3) and fake.calls == 2
    new = json.loads(recal.calibration.read_text(encoding="utf-8"))
    assert new["evidence"] == sv.calibration_identity()["evidence"]
    assert new["recalibration"]["previous"]["evidence"] == "supplier-victims:0000000000000000"
    assert new["recalibration"]["labelling_snapshot"] == "labelling.db" and new["recalibration"]["cases_scored"] == 2
    assert new["victim"]["labelled_used"] == 1 and new["same_incident"]["labelled_used"] == 1
    assert "different_cutoff" in new["same_incident"]
    assert sv.active_calibration(recal.calibration)["same_incident"]["cutoff"] == new["same_incident"]["cutoff"]
    assert result["after"]["state"] == "current"
    # The live database's stored search was carried into the working copy (and a repeat run buys nothing).
    work = recal.live.parent / "recalibration" / "supplier-victims.db"
    assert sqlite3.connect(work).execute("SELECT COUNT(*) FROM SupplierIncidentSearches").fetchone()[0] == 1
    again = _recalibrate(recal, fake)
    assert fake.calls == 2 and again["cases_scored"] == 2


def test_supplier_recalibration_refuses_a_snapshot_that_lacks_too_many_labelled_cases(recal):
    from cyber_data_collector.dedup import recalibration as rc

    before = recal.calibration.read_text(encoding="utf-8")
    recal.labels.write_text(recal.labels.read_text(encoding="utf-8")
                            + "victim,e1|name:other gone,affected,why,test\n", encoding="utf-8")
    recal.sample[("victim", "e1|name:other gone")] = {"stratum": "high", "weight": "1"}
    fake = FakeTypeSafe(p=0.95)
    with pytest.raises(rc.RecalibrationError, match="only 2 of 4 labelled case"):
        _recalibrate(recal, fake)
    assert fake.calls == 0 and recal.calibration.read_text(encoding="utf-8") == before


def test_supplier_recalibration_dry_run_counts_on_a_throwaway_copy(recal):
    from cyber_data_collector.dedup import recalibration as rc

    result = rc.recalibrate_step("supplier-victims", recal.live, recal.snapshot, dry_run=True)
    assert (result["present_in_snapshot"], result["labelled"]) == (2, 3)
    assert not (recal.live.parent / rc.WORK_DIR_NAME).exists()


# ----------------------------------------------------------------------
# Calibrating the bound for "different"
# ----------------------------------------------------------------------

def _pair_rows():
    """Labelled pairs: many low-scoring different ones (heavy weight), a few near the cut-off, same above."""
    rows = [{"id": f"low{i}", "stratum": "low", "weight": 20.0, "p": 0.02 * i, "positive": False}
            for i in range(10)]
    rows += [{"id": "d1", "stratum": "mid", "weight": 1.0, "p": 0.5, "positive": False},
             {"id": "d2", "stratum": "mid", "weight": 1.0, "p": 0.78, "positive": False},
             {"id": "s1", "stratum": "high", "weight": 1.0, "p": 0.8, "positive": True},
             {"id": "d3", "stratum": "high", "weight": 1.0, "p": 0.86, "positive": False},
             {"id": "s2", "stratum": "high", "weight": 1.0, "p": 0.93, "positive": True},
             {"id": "s3", "stratum": "high", "weight": 1.0, "p": 0.99, "positive": True}]
    return rows


def test_calibrate_computes_a_different_bound_below_every_labelled_same_pair():
    from scripts.calibrate_supplier_victims import choose_different, different_bound

    bound = different_bound(_pair_rows(), 0.95, 5, n=50)
    # Weighted precision alone would run past 0.8 (the heavy low-scoring stratum drowns the labelled-same pair at
    # 0.8); a pair labelled the same incident is never ruled different, so the bound stops at 0.78 (just above).
    assert bound["different_cutoff"] == pytest.approx(0.780001)
    at = bound["different_at_cutoff"]
    assert at["correct_labelled"] == at["rejected_labelled"] == 12 and at["weighted_precision"] == 1.0
    assert 0 < at["weighted_recall"] <= 1.0
    assert bound["different_bootstrap_out_of_sample_precision"]["replicates"] > 0
    chosen, _ = choose_different(_pair_rows(), 0.95, 5)
    assert not any(r["positive"] and r["p"] < chosen["cutoff"] for r in _pair_rows())


def test_calibrate_has_no_different_bound_without_enough_labelled_pairs():
    from scripts.calibrate_supplier_victims import different_bound

    rows = [{"id": "d", "stratum": "mid", "weight": 1.0, "p": 0.3, "positive": False},
            {"id": "s", "stratum": "high", "weight": 1.0, "p": 0.95, "positive": True}]
    assert different_bound(rows, 0.95, 5, n=10)["different_cutoff"] is None


def test_different_only_adds_the_one_field_and_refuses_changed_data(tmp_path, monkeypatch):
    from scripts import calibrate_supplier_victims as cal

    rows = _pair_rows()
    sample_map = {("same_incident", r["id"]): {"stratum": r["stratum"], "weight": str(r["weight"])} for r in rows}
    label_map = {("same_incident", r["id"]): "same" if r["positive"] else "different" for r in rows}
    probs = {r["id"]: r["p"] for r in rows}
    chosen, _, _ = cal.accept_cutoff(rows, 0.95, 5)     # the merge cut-off the stored judgements reproduce
    existing = {"model": "m", "question_version": "q", "precision_target": 0.95, "min_accepts": 5,
                "victim": {"cutoff": 0.9}, "same_incident": {"cutoff": chosen["cutoff"] if chosen else None, "marker": "kept"}}
    path = tmp_path / "cal.json"
    path.write_text(json.dumps(existing))
    monkeypatch.setattr(cal, "read_sample", lambda: sample_map)
    monkeypatch.setattr(cal.sv, "read_labels", lambda p=None: label_map)
    monkeypatch.setattr(cal.sv, "CALIBRATION_PATH", path)
    monkeypatch.setattr(cal.sv, "stored_final_probabilities", lambda conn, kind: probs)
    args = SimpleNamespace(db=str(tmp_path / "x.db"), precision=0.95, min_accepts=5, different_only=True)

    assert cal.cmd_calibrate(args) == 0
    written = json.loads(path.read_text())
    assert written["same_incident"]["different_cutoff"] == pytest.approx(0.780001)
    assert written["same_incident"]["marker"] == "kept" and written["victim"] == {"cutoff": 0.9}
    # Besides the field, only the rows it was chosen on are added (for the deployment rule's held-out evaluation).
    assert {r["id"] for r in written["labelled_rows"]["different"]} == {r["id"] for r in rows}
    assert {k: v for k, v in written.items() if k not in ("same_incident", "labelled_rows")} == \
        {k: v for k, v in existing.items() if k != "same_incident"}

    # The data no longer reproduces the calibration's merge cut-off: refuse, change nothing.
    existing["same_incident"]["cutoff"] = 0.123
    path.write_text(json.dumps(existing))
    assert cal.cmd_calibrate(args) == 2
    assert "different_cutoff" not in json.loads(path.read_text())["same_incident"]


def test_a_stored_score_is_read_only_on_the_evidence_it_was_judged_on(conn, monkeypatch):
    # Jev scores mid-band and the escalation decides; both saw one state, recorded with the final score.
    class Jev:
        def ask(self, subject_type, sid, version, state, questions):
            return {next(iter(questions)): SimpleNamespace(value=0.5)}

        def close(self):
            pass

    monkeypatch.setattr("cyber_data_collector.utils.escalation.escalate", lambda *a, **k: 0.9)
    state = {"incident": {"title": "ProctorU breach"}, "candidate": {"name": "UWA"}}
    scores = sv.judge(conn, [("s1", state)], sv.KIND_VICTIM, judge_client=Jev())
    assert scores.final == {"s1": 0.9}
    assert sv.stored_final_probabilities(conn, sv.KIND_VICTIM, [("s1", state)]) == {"s1": 0.9}
    changed = {**state, "candidate": {"name": "UWA", "kind": "organisation"}}
    assert sv.stored_final_probabilities(conn, sv.KIND_VICTIM, [("s1", changed)]) == {}


# ----------------------------------------------------------------------
# Extending a stratum of the sample (more blind labels for the deployment rule)
# ----------------------------------------------------------------------

def _sample_rows():
    return ([{"kind": "victim", "subject_id": f"old{i}", "stratum": "high", "weight": "2.5"} for i in range(4)]
            + [{"kind": "victim", "subject_id": "lowA", "stratum": "low", "weight": "8.0"},
               {"kind": "same_incident", "subject_id": "pair1", "stratum": "high", "weight": "1.0"}])


def test_extend_draws_only_unsampled_items_of_the_stratum_and_reweights_it_alone():
    import random

    from scripts.calibrate_supplier_victims import extend_stratum

    # Population of the high stratum at sampling: 4 x 2.5 = 10. Current Jev scores: 3 unsampled high items, one
    # already sampled (old0), one mid and one low item.
    scores = {"old0": 0.95, "n1": 0.9, "n2": 0.85, "n3": 0.99, "m1": 0.5, "l1": 0.1}
    rows, summary = extend_stratum(_sample_rows(), "victim", "high", scores, 2, random.Random(1), 2)
    new = [r for r in rows if r["round"] == "2"]
    assert len(new) == 2 and {r["subject_id"] for r in new} <= {"n1", "n2", "n3"}
    assert all(r["stratum"] == "high" and r["kind"] == "victim" for r in new)
    # 10 in the stratum, 6 sampled now: every high item (old and new) weighs 10/6; other strata are untouched.
    high = [r for r in rows if r["kind"] == "victim" and r["stratum"] == "high"]
    assert len(high) == 6 and {r["weight"] for r in high} == {str(round(10 / 6, 4))}
    assert next(r for r in rows if r["subject_id"] == "lowA")["weight"] == "8.0"
    assert next(r for r in rows if r["subject_id"] == "pair1")["weight"] == "1.0"
    assert all(r["round"] == "1" for r in rows if r not in new)
    assert summary["eligible"] == 3 and summary["population"] == 10
    again, _ = extend_stratum(_sample_rows(), "victim", "high", scores, 2, random.Random(1), 2)
    assert again == rows          # deterministic for a seed


def test_extend_grows_the_population_when_the_stratum_holds_more_than_it_did():
    import random

    from scripts.calibrate_supplier_victims import extend_stratum

    scores = {f"n{i}": 0.9 for i in range(10)}          # 4 sampled + 10 eligible > the 10 the weights represent
    rows, summary = extend_stratum(_sample_rows(), "victim", "high", scores, 3, random.Random(2), 2)
    assert summary["population"] == 14 and summary["added"] == 3
    assert {r["weight"] for r in rows if r["stratum"] == "high" and r["kind"] == "victim"} == {str(round(14 / 7, 4))}


def test_extend_refuses_a_stratum_the_sample_does_not_have():
    import random

    from scripts.calibrate_supplier_victims import extend_stratum

    with pytest.raises(ValueError, match="no same_incident stratum 'low'"):
        extend_stratum(_sample_rows(), "same_incident", "low", {"x": 0.1}, 1, random.Random(1), 2)


def test_extend_command_needs_scores_for_every_candidate_then_judges_and_writes_the_new_items(conn, tmp_path,
                                                                                            monkeypatch):
    from scripts import calibrate_supplier_victims as cal

    db = tmp_path / "work.db"
    out = sqlite3.connect(db)
    conn.backup(out)
    sv.ensure_schema(out)      # a stored search naming a second customer: two victim candidates
    out.execute("INSERT INTO SupplierIncidentSearches VALUES ('e1', 'h1', 'q', 'sonar', ?, '[]', '2026-10-28')",
                ("AFFECTED: Monash University | exam records | https://y",))
    out.commit()
    out.close()
    items = cal._current_items(sqlite3.connect(db), sv.KIND_VICTIM)
    assert len(items) == 2
    first = items[0][0]
    sample = tmp_path / "sample.csv"
    sample.write_text(f"kind,subject_id,stratum,weight\nvictim,{first},high,3.0\n", encoding="utf-8")
    monkeypatch.setattr(cal, "SAMPLE_PATH", sample)
    fake = FakeTypeSafe(p=0.95)
    monkeypatch.setattr("cyber_data_collector.utils.jev.JevJudge._get_client", lambda self: fake)
    args = SimpleNamespace(db=str(db), kind=sv.KIND_VICTIM, stratum="high", add=1, round=2, seed=7, judge=False,
                           dry_run=False)
    # Unscored candidates have no stratum: refuse rather than draw from a biased subset; nothing is asked.
    assert cal.cmd_extend(args) == 2 and fake.calls == 0
    args.judge = True
    assert cal.cmd_extend(args) == 0
    rows = list(cal.read_sample(sample).values())
    new = [r for r in rows if r["round"] == "2"]
    assert len(new) == 1 and new[0]["subject_id"] != first and new[0]["stratum"] == "high"
    # The stratum held 3 at sampling (3.0 x 1), 2 are sampled now: both weigh 1.5.
    assert {r["weight"] for r in rows} == {"1.5"}
    # The new item's final score is stored on its current evidence, for the calibration.
    state = dict(items)[new[0]["subject_id"]]
    assert sv.stored_final_probabilities(sqlite3.connect(db), sv.KIND_VICTIM,
                                         [(new[0]["subject_id"], state)]) == {new[0]["subject_id"]: 0.95}
    # The same round cannot be drawn twice into the stratum.
    assert cal.cmd_extend(args) == 2
    # The packet of the round holds the new item only, without any score.
    packet = tmp_path / "packet"
    assert cal.cmd_packet(SimpleNamespace(db=str(db), out=str(packet), chunks=1, seed=1, round=2)) == 0
    lines = (packet / "supplier_packet_1.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["subject_id"] for x in lines] == [new[0]["subject_id"]]
    assert "0.95" not in lines[0]
