"""AI-involved events as duplicate candidates of each other: candidates whatever the victim, a question that knows
the base rate, a calibration of its own, merges through the shared path, candidate ranking and conflict-aware groups."""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest

pytest.importorskip("typesafe_sdk")

from cyber_data_collector.dedup import ai_duplicates as ad  # noqa: E402
from cyber_data_collector.dedup import candidate_ranking as ranking  # noqa: E402
from cyber_data_collector.dedup import duplicate_judgement as dj  # noqa: E402
from cyber_data_collector.dedup import label_contradictions as lc  # noqa: E402
from cyber_data_collector.dedup import recalibration as rc  # noqa: E402
from cyber_data_collector.dedup.ledger import DedupLedger  # noqa: E402
from cyber_data_collector.processing import ai_involvement  # noqa: E402
from cyber_data_collector.utils.jev import JevJudge  # noqa: E402
from scripts import calibrate_ai_duplicates as cal  # noqa: E402

NO_SEARCH = lambda query, model: {"answer": "NONE FOUND", "citations": []}   # noqa: E731
NO_ESCALATION = object()   # never called: the fake scores sit outside the escalation band


@pytest.fixture(autouse=True)
def bogus_keys(monkeypatch):
    for name in ("OPENAI_API_KEY", "TYPESAFE_API_KEY", "PERPLEXITY_API_KEY"):
        monkeypatch.setenv(name, "bogus-key-for-tests")
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")


class FakeTypeSafe:
    """P(same) by the pair's two titles (order-free); anything not listed scores ``default``."""

    def __init__(self, scores=None, default=0.05):
        self.scores = {frozenset(k): v for k, v in (scores or {}).items()}
        self.default = default
        self.questions = []          # (question id, pair of titles) actually sent

    def system_one(self, state, questions, model=None):
        titles = frozenset((state["event_a"]["event"]["title"], state["event_b"]["event"]["title"]))
        p = self.scores.get(titles, self.default)
        self.questions.extend((q, titles) for q in questions)
        return SimpleNamespace(model="jev-1.14.0", usage=None,
                               answers={q: SimpleNamespace(type="noul", noul=p) for q in questions})


# id, title, date, ai label, victim, vendor, precision, earliest, latest, summary
EVENTS = [
    ("m1", "Agent hacked health portal", "2026-06-18", "AI-attacker", "Agency X", None, "day", None, None, "agent portal"),
    ("m2", "Albanese criticises agent hack", "2026-09-24", "AI-attacker", None, None, None, None, None, "agent portal"),
    ("m3", "Rogue agent or human error", "2026-09-25", "AI-attacker", "Data USA", None, None, None, None, "agent portal"),
    ("g1", "Assistant hacks gym website", "2026-04-01", "AI-attacker", None, None, None, None, None, "gym booking"),
    ("g2", "Autonomous agent hacks gym", None, "AI-attacker", None, None, None, None, None, "gym booking"),
    ("old", "Facial recognition in stores", "2020-06-01", "AI-misuse", "Retailer", None, None, None, None, "faces"),
    ("plain", "Ransomware hits council", "2026-06-20", "No-AI", "Council", None, "day", None, None, "ransom"),
    ("plain2", "Council ransomware follow-up", "2026-06-25", "No-AI", "Council", None, "day", None, None, "ransom"),
]


def _build(path=":memory:", events=EVENTS):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, title TEXT, event_date TEXT,
            victim_organization_name TEXT, status TEXT, records_affected INTEGER, attack_method TEXT,
            event_type TEXT, vendor_organization_name TEXT, summary TEXT, description TEXT,
            master_enriched_event_id TEXT, ai_involvement TEXT, event_date_precision TEXT,
            event_date_earliest TEXT, event_date_latest TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title TEXT,
            event_date TEXT, summary TEXT, description TEXT, perplexity_enrichment_data TEXT, status TEXT);
        CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_url TEXT, raw_content TEXT,
            raw_description TEXT, event_date TEXT, source_metadata TEXT, discovered_at TEXT);
    """)
    ai_involvement.ensure_schema(c)
    victims = {}
    for did, title, day, label, victim, vendor, prec, lo, hi, summary in events:
        c.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, title, event_date, victim_organization_name,"
                  " status, vendor_organization_name, summary, master_enriched_event_id, ai_involvement,"
                  " event_date_precision, event_date_earliest, event_date_latest) VALUES (?,?,?,?,'Active',?,?,?,?,?,?,?)",
                  (did, title, day, victim, vendor, summary, f"{did}-r", label, prec, lo, hi))
        if victim:
            eid = victims.setdefault(victim, len(victims) + 1)
            c.execute("INSERT OR IGNORE INTO EntitiesV2 VALUES (?, ?)", (eid, victim))
            c.execute("INSERT INTO DeduplicatedEventEntities VALUES (?, ?, 'victim')", (did, eid))
        c.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?)", (f"{did}-r", did))
        c.execute("INSERT INTO EnrichedEvents VALUES (?, ?, ?, ?, NULL, NULL, '{}', 'Active')", (f"{did}-r", f"{did}-r", title, day))
        c.execute("INSERT INTO RawEvents VALUES (?, ?, 'Page text', NULL, ?, ?, '2026-10-01')",
                  (f"{did}-r", f"https://news.example/{did}", day, json.dumps({"publication_date": day})))
    c.commit()
    return c


@pytest.fixture
def conn():
    return _build()


@pytest.fixture
def merges(monkeypatch):
    done = []
    monkeypatch.setattr(DedupLedger, "merge_events",
                        lambda self, target, source, reason, actor="human", respect_overrides=True:
                        done.append((target, source, reason)))
    return done


def _write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload, encoding="utf-8")
    return path


def _ai_cal(tmp_path, cutoff, **extra):
    return _write(tmp_path, "ai_cal.json", {"cutoff": cutoff, **ad.calibration_identity(), **extra})


def _org_cal(tmp_path, cutoff, **extra):
    return _write(tmp_path, "org_cal.json", {"cutoff": cutoff, **dj.calibration_identity(), **extra})


def _labels(tmp_path, name, rows):
    body = "".join(f"{pid},{label},r,packet,t\n" for pid, label in rows)
    return _write(tmp_path, name, "pair_id,label,rationale,corroboration,labeller\n" + (body or "x|y,same,unrelated,packet,t\n"))


def _run(conn, tmp_path, scores, ai_cutoff=None, org_cutoff=0.9, ai_labels=(), org_labels=(), apply=True, default=0.05,
         ai_extra=None, **kwargs):
    ai_cal = _ai_cal(tmp_path, ai_cutoff, **(ai_extra or {})) if ai_cutoff is not None else tmp_path / "none.json"
    return dj.check_duplicates(
        conn, apply=apply, timeline_search=NO_SEARCH, escalation_client=NO_ESCALATION,
        judge=JevJudge(conn, client=FakeTypeSafe(scores, default)),
        calibration_path=_org_cal(tmp_path, org_cutoff), labels_path=_labels(tmp_path, "org_labels.csv", org_labels),
        ai_calibration_path=ai_cal, ai_labels_path=_labels(tmp_path, "ai_labels.csv", ai_labels), **kwargs)


# ---------------------------------------------------------------- candidates

def _titles(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT deduplicated_event_id, title FROM DeduplicatedEvents")}


def test_every_pair_of_ai_events_in_the_window_is_a_candidate_whatever_the_victim(conn):
    ids = {p["pair_id"] for p in ad.candidate_pairs(conn)}
    # m2 has no victim, m3 a wrong one, m1 a vague one: none shares an organisation with another.
    for pair in ("m1|m2", "m1|m3", "m2|m3", "g1|m2"):
        assert pair in ids
    # A non-AI event is never an AI candidate, even beside AI events with the same dates.
    assert not any("plain" in p for p in ids)
    # The 2020 event is beyond the window of everything dated; the undated gym event pairs with every other.
    assert not any(p.startswith("m1|old") or p == "old|m1" for p in ids)
    assert {p for p in ids if "g2" in p.split("|")} == {dj.pair_id("g2", o) for o in ("m1", "m2", "m3", "g1", "old")}


def test_the_gap_is_the_distance_between_date_intervals():
    def event(start, end):
        return ad.AIEvent("x", "t", "AI-attacker", start, end)

    d = date
    assert ad.interval_gap(event(d(2026, 8, 1), d(2026, 8, 31)), event(d(2026, 8, 10), d(2026, 8, 10))) == 0   # overlap
    assert ad.interval_gap(event(d(2026, 8, 1), d(2026, 8, 31)), event(d(2026, 9, 24), d(2026, 9, 24))) == 24
    assert ad.interval_gap(event(d(2026, 9, 24), d(2026, 9, 24)), event(d(2026, 8, 1), d(2026, 8, 31))) == 24
    assert ad.interval_gap(event(None, None), event(d(2026, 8, 1), d(2026, 8, 1))) is None


def test_a_month_precision_event_spans_its_month(conn):
    conn.execute("UPDATE DeduplicatedEvents SET event_date = '2026-08-03', event_date_precision = 'month', "
                 "event_date_earliest = '2026-08-01', event_date_latest = '2026-08-31' WHERE deduplicated_event_id = 'g1'")
    pairs = {p["pair_id"]: p for p in ad.candidate_pairs(conn)}
    assert pairs["g1|m2"]["gap_days"] == 24                     # 31 August to 24 September, not 3 August
    assert pairs["g1|m1"]["gap_days"] == 0 or pairs["g1|m1"]["gap_days"] > 0


def test_a_shared_organisation_is_carried_for_the_timeline(conn):
    conn.execute("UPDATE DeduplicatedEvents SET victim_organization_name = 'Agency X' WHERE deduplicated_event_id = 'm2'")
    conn.execute("INSERT INTO DeduplicatedEventEntities VALUES ('m2', 1, 'victim')")   # entity 1 = Agency X
    pair = next(p for p in ad.candidate_pairs(conn) if p["pair_id"] == "m1|m2")
    assert pair["shared_organisation"] and pair["organisation"] == "Agency X" and pair["entity_id"] == 1


def test_no_ai_column_means_no_candidates():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT, status TEXT)")
    assert ad.ai_events(c) == [] and ad.candidate_pairs(c) == []


# ---------------------------------------------------------------- evidence

def test_the_base_rate_is_counted_from_the_data_and_leaves_out_the_half_year_in_progress(conn):
    rate = ad.base_rate(conn, today=date(2026, 10, 29))
    ai, every = rate["ai_incidents_per_half_year"], rate["all_incidents_per_half_year"]
    assert list(ai) == ["2023 H2", "2024 H1", "2024 H2", "2025 H1", "2025 H2", "2026 H1"]
    assert ai["2026 H1"] == 2 and every["2026 H1"] == 4          # m1 and g1 are AI; plain, plain2 are not
    assert "2026 H2" not in ai                                   # m2, m3 are in the half-year in progress
    conn.execute("UPDATE DeduplicatedEvents SET event_date = '2026-05-01' WHERE deduplicated_event_id = 'g2'")
    assert ad.base_rate(conn, today=date(2026, 10, 29))["ai_incidents_per_half_year"]["2026 H1"] == 3


def test_the_question_is_shown_the_base_rate_the_category_and_the_description(conn):
    ai_involvement.ensure_schema(conn)
    conn.execute("INSERT INTO EventDescriptions VALUES ('m1', ?, 'v', 'h', 'model', 'An AI agent reached a portal.', 't')",
                 (ai_involvement.DESCRIPTION_PURPOSE,))
    state = ad.build_pair_state(conn, next(p for p in ad.candidate_pairs(conn) if p["pair_id"] == "m1|m2"),
                                today=date(2026, 10, 29))
    assert state["event_a"]["event"]["ai_involvement"] == "AI-attacker"
    assert state["event_a"]["event"]["ai_description"] == "An AI agent reached a portal."
    assert "ai_description" not in state["event_b"]["event"]
    assert state["ai_incident_base_rate"]["ai_incidents_per_half_year"]["2026 H1"] == 2
    assert state["organisation"] is None
    assert "rare in this" in ad.INSTRUCTIONS or "how rare AI incidents are" in ad.INSTRUCTIONS


def test_the_decision_code_never_reads_the_acceptance_cases():
    """The acceptance file is a measure only: the quality report reads it, no decision code does."""
    import pathlib
    root = pathlib.Path(ad.__file__).resolve().parents[1]
    offenders = [str(p.relative_to(root)) for p in root.rglob("*.py")
                 if not {"tests", "quality"} & set(p.parts) and "review_acceptance" in p.read_text(encoding="utf-8")]
    assert offenders == []


# ---------------------------------------------------------------- the route

def test_a_pair_is_asked_by_one_route_only(conn, merges, tmp_path):
    """Two AI events that also share an organisation belong to the AI route; the organisation route skips them."""
    conn.execute("UPDATE DeduplicatedEvents SET victim_organization_name = 'Agency X' WHERE deduplicated_event_id = 'm2'")
    conn.execute("INSERT INTO DeduplicatedEventEntities VALUES ('m2', 1, 'victim')")
    fake = FakeTypeSafe()
    result = dj.check_duplicates(
        conn, timeline_search=NO_SEARCH, escalation_client=NO_ESCALATION, judge=JevJudge(conn, client=fake),
        calibration_path=_org_cal(tmp_path, 0.9), labels_path=_labels(tmp_path, "o.csv", []),
        ai_calibration_path=tmp_path / "none.json", ai_labels_path=tmp_path / "none.csv", cap_candidates=False)
    pair = {frozenset(("Agent hacked health portal", "Albanese criticises agent hack"))}
    asked = [q for q, titles in fake.questions if titles in pair]
    assert asked == [ad.QUESTION_ID]                              # the AI question only, once
    assert all(p["pair_id"] != "m1|m2" for p in dj.candidate_pairs(conn, skip={"m1|m2"}))
    assert result["pairs"] == 1                                  # the organisation route kept only the council pair


def test_without_a_calibration_ai_pairs_are_reported_and_nothing_merges(conn, merges, tmp_path):
    result = _run(conn, tmp_path, {("Agent hacked health portal", "Albanese criticises agent hack"): 0.99})
    assert merges == [] and not result["ai"]["calibrated"]
    assert result["ai"]["scored"][0]["pair_id"] == "m1|m2" and result["ai"]["scored"][0]["p_same"] == 0.99
    assert result["ai"]["proposals"] == []


def test_the_ai_cutoff_is_its_own_and_lower_than_the_organisation_one(conn, merges, tmp_path):
    """0.88 merges two AI events at the AI cut-off (0.85) while the same score does not merge two
    ordinary events at the organisation cut-off (0.9)."""
    scores = {("Agent hacked health portal", "Albanese criticises agent hack"): 0.88,
              ("Ransomware hits council", "Council ransomware follow-up"): 0.88}
    result = _run(conn, tmp_path, scores, ai_cutoff=0.85, org_cutoff=0.9)
    assert len(merges) == 1 and {merges[0][0], merges[0][1]} == {"m1", "m2"}
    assert "AI cut-off" in merges[0][2]
    assert result["ai"]["cutoff"] == 0.85 and result["cutoff"] == 0.9


def test_a_chain_of_ai_pairs_merges_into_one_event(conn, merges, tmp_path):
    scores = {("Agent hacked health portal", "Albanese criticises agent hack"): 0.95,
              ("Albanese criticises agent hack", "Rogue agent or human error"): 0.93}
    _run(conn, tmp_path, scores, ai_cutoff=0.85)
    assert len(merges) == 2 and {x for m in merges for x in m[:2]} == {"m1", "m2", "m3"}
    assert len({m[0] for m in merges}) == 1                       # one target absorbs both


def test_a_labelled_different_pair_is_never_merged_and_a_labelled_same_pair_is(conn, merges, tmp_path):
    scores = {("Agent hacked health portal", "Albanese criticises agent hack"): 0.99,
              ("Assistant hacks gym website", "Autonomous agent hacks gym"): 0.01}
    _run(conn, tmp_path, scores, ai_cutoff=0.85, ai_labels=[("m1|m2", "different"), ("g1|g2", "same")])
    assert [{m[0], m[1]} for m in merges] == [{"g1", "g2"}]


def test_the_organisation_routes_labels_also_protect_ai_pairs(conn, merges, tmp_path):
    scores = {("Agent hacked health portal", "Albanese criticises agent hack"): 0.99}
    _run(conn, tmp_path, scores, ai_cutoff=0.85, org_labels=[("m1|m2", "different")])
    assert merges == []


def test_unreadable_or_missing_labels_stop_ai_merging_once_calibrated(conn, merges, tmp_path):
    with pytest.raises(RuntimeError, match="refusing to merge"):
        dj.check_duplicates(conn, apply=True, timeline_search=NO_SEARCH, escalation_client=NO_ESCALATION,
                            judge=JevJudge(conn, client=FakeTypeSafe()),
                            calibration_path=_org_cal(tmp_path, 0.9), labels_path=_labels(tmp_path, "o.csv", []),
                            ai_calibration_path=_ai_cal(tmp_path, 0.85), ai_labels_path=tmp_path / "missing.csv")
    assert merges == []
    # ...but an uncalibrated route does not need labels that do not exist yet.
    assert ad.labelled_pairs(tmp_path / "missing.csv") == {}


def test_a_calibration_for_other_evidence_or_a_other_window_does_not_apply(conn, merges, tmp_path, monkeypatch):
    path = _ai_cal(tmp_path, 0.85)
    assert ad.active_calibration(path) is not None
    monkeypatch.setattr(ad, "AI_WINDOW_DAYS", 90)
    assert ad.active_calibration(path) is None                   # the window is in the evidence fingerprint
    monkeypatch.undo()
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-other")
    assert ad.active_calibration(path) is None


def test_skipping_the_ai_route_leaves_check_duplicates_as_it_was(conn, merges, tmp_path):
    result = _run(conn, tmp_path, {}, include_ai=False)
    assert "ai" not in result and result["candidates"]["ai"] is None


# ---------------------------------------------------------------- conflicts

def test_a_chain_is_not_merged_across_a_labelled_difference(conn, merges, tmp_path):
    """A=B, B=C, but A and C are labelled different: A and B join (the stronger link), B-C is held."""
    scores = {("Agent hacked health portal", "Albanese criticises agent hack"): 0.97,
              ("Albanese criticises agent hack", "Rogue agent or human error"): 0.90}
    result = _run(conn, tmp_path, scores, ai_cutoff=0.85, ai_labels=[("m1|m3", "different")])
    assert [{m[0], m[1]} for m in merges] == [{"m1", "m2"}]
    [held] = result["held_conflicts"]
    assert held["pair_id"] == "m2|m3" and held["blocked_by"] == "m1|m3" and "labelled different" in held["reason"]


def test_a_chain_is_not_merged_across_a_ruling(conn, merges, tmp_path):
    conn.execute("CREATE TABLE DedupOverrides (left_enriched_event_id TEXT, right_enriched_event_id TEXT, "
                 "verdict TEXT, reason TEXT, active INTEGER)")
    conn.execute("INSERT INTO DedupOverrides VALUES ('m1-r', 'm3-r', 'different', 'split earlier', 1)")
    scores = {("Agent hacked health portal", "Albanese criticises agent hack"): 0.97,
              ("Albanese criticises agent hack", "Rogue agent or human error"): 0.90}
    result = _run(conn, tmp_path, scores, ai_cutoff=0.85)
    assert [{m[0], m[1]} for m in merges] == [{"m1", "m2"}]
    assert "ruling" in result["held_conflicts"][0]["reason"]


def test_a_calibrated_low_score_blocks_the_chain_but_an_uncalibrated_one_does_not(conn, merges, tmp_path):
    """m1 and m3 were judged apart (0.05). Only a calibrated 'different' bound makes that a veto."""
    scores = {("Agent hacked health portal", "Albanese criticises agent hack"): 0.97,
              ("Albanese criticises agent hack", "Rogue agent or human error"): 0.90}
    # Score the unlinked pair first so the judgement is stored (it is a candidate, just below the cut-off).
    result = _run(conn, tmp_path, scores, ai_cutoff=0.85, ai_extra={"different_cutoff": 0.10})
    assert [{m[0], m[1]} for m in merges] == [{"m1", "m2"}]
    assert "calibrated 'different' bound" in result["held_conflicts"][0]["reason"]
    merges.clear()
    _run(conn, tmp_path, scores, ai_cutoff=0.85)                  # no different_cutoff: the low score is no veto
    assert {x for m in merges for x in m[:2]} == {"m1", "m2", "m3"}


def test_an_ai_decision_is_stored_under_the_ai_route_and_a_stale_low_one_is_asked_again(conn):
    """The AI route's final scores are stored against the evidence like the organisation route's; one made on other
    evidence neither counts as the pair's score nor vetoes, but is asked again where it could block a merge."""
    pair = next(p for p in ad.candidate_pairs(conn) if p["pair_id"] == "m1|m3")
    dj.score_pairs(conn, [pair], ad.ROUTE, judge=JevJudge(conn, client=FakeTypeSafe(default=0.05)))
    assert tuple(conn.execute("SELECT route, final_score FROM PairDecisions").fetchone()) == ("ai", 0.05)
    assert ad.stored_final_probabilities(conn, [pair]) == {"m1|m3": 0.05}
    conn.execute("UPDATE DeduplicatedEvents SET event_date = '2026-09-26' WHERE deduplicated_event_id = 'm3'")
    new_pair = next(p for p in ad.candidate_pairs(conn) if p["pair_id"] == "m1|m3")
    assert ad.stored_final_probabilities(conn, [new_pair]) == {}                  # not this pair's score any more
    lookups = {"organisation": lambda a, b: None, "ai": ad.AIUniverse(conn).pair}
    conflicts, revisited = dj.calibrated_negatives(
        conn, None, {"different_cutoff": 0.10}, {"m1", "m3"}, lookups,
        judge=JevJudge(conn, client=FakeTypeSafe(default=0.95)))
    assert conflicts == {} and revisited == ["m1|m3"]
    assert ad.stored_final_probabilities(conn, [new_pair]) == {"m1|m3": 0.95}


def test_ai_candidates_by_retrieval_are_the_enumerated_ones_and_never_leave_the_window(conn):
    corpus = ranking.load_corpus(conn)
    universe = ad.AIUniverse(conn)
    enumerated, _ = universe.ranked(corpus)
    retrieved, stats = universe.ranked(corpus, exhaustive_max=0)             # force bounded retrieval
    assert stats["bounded_groups"] == 1
    assert {p["pair_id"] for p in retrieved} == {p["pair_id"] for p in enumerated}
    assert all(universe.owns(p["a"], p["b"]) for p in retrieved)             # "old" (2020) is outside the window
    ids = {p["pair_id"] for p in retrieved}
    assert not {"m1|old", "m2|old", "m3|old", "g1|old"} & ids                # dated 2026 events: years apart
    assert "g2|old" in ids                                                   # g2 is undated: a missing date is no evidence
    assert universe.pair("m1", "old") is None and universe.pair("m1", "m2")["pair_id"] == "m1|m2"


def test_partition_takes_the_strongest_links_first_and_a_labelled_same_link_before_any_score():
    edges = [("a", "b", 0.90), ("b", "c", 0.99), ("c", "d", 2.0)]
    groups, held = dj.partition_without_conflicts(edges, {dj.pair_id("a", "c"): "x"})
    # c-d (label) then b-c, then a-b would put a with c: held.
    assert groups == [["b", "c", "d"]] and [h["pair_id"] for h in held] == ["a|b"]
    assert held[0]["p_same"] == 0.90
    groups, held = dj.partition_without_conflicts(edges, {})
    assert groups == [["a", "b", "c", "d"]] and held == []


def test_conflicts_between_events_no_link_connects_are_never_reached():
    groups, held = dj.partition_without_conflicts([("a", "b", 0.9), ("c", "d", 0.9)], {dj.pair_id("a", "c"): "x"})
    assert groups == [["a", "b"], ["c", "d"]] and held == []


# ---------------------------------------------------------------- ranking

def _pair(a, b, gap=10):
    return {"pair_id": dj.pair_id(a, b), "a": a, "b": b, "gap_days": gap}


def _corpus(**features):
    return ranking.Corpus({k: ranking.EventFeatures(k, **v) for k, v in features.items()}, rare_tokens={"zephyr", "quokka"},
                          hub_urls={"hub.example/list"})


def test_ranking_prefers_a_shared_source_supplier_and_rare_words_over_a_plain_date_match():
    corpus = _corpus(a={"urls": {"x.example/story"}, "vendor": "acme"}, b={"urls": {"x.example/story"}, "vendor": "acme"},
                     c={"tokens": {"zephyr", "quokka"}}, d={"tokens": {"zephyr", "quokka"}}, e={}, f={})
    source, s_reasons, s_strong = ranking.score_pair(corpus, "a", "b", 30)
    words, w_reasons, w_strong = ranking.score_pair(corpus, "c", "d", 30)
    plain, _, p_strong = ranking.score_pair(corpus, "e", "f", 0)
    assert source > words > plain and s_strong and w_strong and not p_strong
    assert any("shared source" in r for r in s_reasons) and any("same supplier" in r for r in s_reasons)
    assert any("incident-specific" in r for r in w_reasons)


def test_a_hub_url_is_no_source_relationship():
    corpus = _corpus(a={"urls": {"hub.example/list"}}, b={"urls": {"hub.example/list"}})
    score, reasons, strong = ranking.score_pair(corpus, "a", "b", None)
    assert score == 0 and reasons == [] and not strong


def test_a_large_group_keeps_each_events_best_partners_only():
    ids = [f"e{i}" for i in range(12)]
    corpus = _corpus(**{i: {} for i in ids})
    pairs = [_pair(a, b, gap=abs(int(a[1:]) - int(b[1:]))) for k, a in enumerate(ids) for b in ids[k + 1:]]
    kept, stats = ranking.select(corpus, pairs, group_of=lambda p: 0, partners_per_event=3, long_gap_budget=0)
    assert stats["before"] == 66 and stats["after"] < 66
    per_event = {i: sum(1 for p in kept if i in (p["a"], p["b"])) for i in ids}
    assert min(per_event.values()) >= 3                          # every event keeps its best partners
    assert all(p["gap_days"] <= 3 for p in kept if p["rank_kept_as"] == "partner")


def test_long_gap_and_undated_pairs_get_a_small_reserved_budget_only_when_the_evidence_is_strong():
    corpus = _corpus(a={"urls": {"s.example/x"}}, b={"urls": {"s.example/x"}}, c={}, d={}, u={"tokens": {"zephyr", "quokka"}},
                     v={"tokens": {"zephyr", "quokka"}})
    pairs = [_pair("a", "b", gap=900), _pair("c", "d", gap=900), _pair("u", "v", gap="unknown - no date")]
    kept, stats = ranking.select(corpus, pairs, group_of=lambda p: 0, long_gap_budget=5)
    assert {p["pair_id"] for p in kept} == {"a|b", "u|v"}        # the strong long-gap pair and the strong undated pair
    assert {p["pair_id"]: p["rank_kept_as"] for p in kept} == {"a|b": "reserved", "u|v": "partner"}
    assert stats["long_gap_before"] == 2 and stats["long_gap_after"] == 1
    kept, _ = ranking.select(corpus, pairs, group_of=lambda p: 0, long_gap_budget=0)
    assert "a|b" not in {p["pair_id"] for p in kept}             # no budget, no long-gap pair
    kept, _ = ranking.select(corpus, pairs, group_of=lambda p: 0, cap=False)
    assert len(kept) == 3                                        # the cap can be switched off


def test_a_strong_undated_pair_that_loses_the_partner_contest_is_reserved():
    corpus = _corpus(u={"tokens": {"zephyr", "quokka"}, "records_affected": 5000, "method": "phishing"},
                     v={"tokens": {"zephyr", "quokka"}, "records_affected": 7000, "method": "ransomware"},
                     w={"records_affected": 5000, "method": "phishing"},
                     x={"records_affected": 7000, "method": "ransomware"})
    pairs = [_pair("u", "w", gap=0), _pair("v", "x", gap=0), _pair("u", "v", gap="unknown - no date")]
    kept, _ = ranking.select(corpus, pairs, group_of=lambda p: 0, partners_per_event=1, long_gap_budget=1)
    assert {p["pair_id"]: p["rank_kept_as"] for p in kept} == {"u|w": "partner", "v|x": "partner", "u|v": "reserved"}
    kept, _ = ranking.select(corpus, pairs, group_of=lambda p: 0, partners_per_event=1, long_gap_budget=0)
    assert "u|v" not in {p["pair_id"] for p in kept}


def test_the_ranking_is_deterministic():
    corpus = _corpus(**{i: {} for i in "abcdef"})
    pairs = [_pair(x, y, gap=5) for k, x in enumerate("abcdef") for y in "abcdef"[k + 1:]]
    first = ranking.select(corpus, pairs, group_of=lambda p: 0, partners_per_event=2)[0]
    second = ranking.select(corpus, list(reversed(pairs)), group_of=lambda p: 0, partners_per_event=2)[0]
    assert [p["pair_id"] for p in first] == [p["pair_id"] for p in second]


def test_check_duplicates_reports_candidate_counts_before_and_after(conn, merges, tmp_path):
    result = _run(conn, tmp_path, {}, partners_per_event=1, long_gap_budget=0)
    ai = result["candidates"]["ai"]
    assert ai["before"] == len(ad.candidate_pairs(conn)) and ai["after"] < ai["before"]
    assert result["candidates"]["organisation"]["before"] == 1
    assert ai["parameters"]["partners_per_event"] == 1


# ---------------------------------------------------------------- calibration script

def test_the_merge_cutoff_is_the_lowest_score_meeting_the_weighted_precision():
    rows = [{"p": p, "same": s, "weight": w, "stratum": st} for p, s, w, st in
            [(0.95, True, 5.0, "high"), (0.9, True, 5.0, "high"), (0.7, True, 1.0, "census"), (0.6, True, 1.0, "census"),
             (0.5, True, 1.0, "census"), (0.4, False, 1.0, "census"), (0.1, False, 3.0, "low")]]
    chosen, _, _ = cal.merge_cutoff(rows, precision=0.95, min_merges=3)
    assert chosen["cutoff"] == pytest.approx(0.5, abs=1e-5) and chosen["weighted_precision"] == 1.0
    assert chosen["merged_labelled"] == 5 and chosen["correct_labelled"] == 5


def test_sampling_takes_a_census_of_the_middle_band_and_weights_the_tails(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "SAMPLE_PATH", tmp_path / "sample.csv")
    judge = JevJudge(conn, client=FakeTypeSafe({("Agent hacked health portal", "Albanese criticises agent hack"): 0.85,
                                                 ("Albanese criticises agent hack", "Rogue agent or human error"): 0.95},
                                                default=0.05))
    pairs = ad.candidate_pairs(conn)
    scores, _, failed = ad.score_pairs(conn, pairs, judge=judge, escalation_client=_FakeEscalation())
    assert not failed and len(scores) == len(pairs)
    args = SimpleNamespace(db=":memory:", seed=1, sample_low=2, sample_high=1)
    monkeypatch.setattr(cal.sqlite3, "connect", lambda *_a, **_k: conn)
    assert cal.cmd_sample(args) == 0
    rows = {r["pair_id"]: r for r in csv.DictReader((tmp_path / "sample.csv").open(encoding="utf-8"))}
    census = [r for r in rows.values() if r["stratum"] == "census"]
    assert [r["pair_id"] for r in census] == ["m1|m2"] and census[0]["weight"] == "1.0"
    low = [r for r in rows.values() if r["stratum"] == "low"]
    assert len(low) == 2 and float(low[0]["weight"]) > 1


class _FakeEscalation:
    """Stands in for the reasoning model: nothing in these tests is meant to reach it."""

    def __getattr__(self, name):
        raise AssertionError("the escalation client was used")


def test_the_packet_is_blind_to_scores_categories_and_descriptions(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "SAMPLE_PATH", tmp_path / "sample.csv")
    (tmp_path / "sample.csv").write_text("pair_id,stratum,weight\nm1|m2,census,1.0\n", encoding="utf-8")
    conn.execute("INSERT INTO EventDescriptions VALUES ('m1', ?, 'v', 'h', 'model', 'AI SAID THIS', 't')",
                 (ai_involvement.DESCRIPTION_PURPOSE,))
    monkeypatch.setattr(cal.sqlite3, "connect", lambda *_a, **_k: conn)
    assert cal.cmd_packet(SimpleNamespace(db=":memory:", out=str(tmp_path / "packet"), chunks=1, seed=1)) == 0
    text = (tmp_path / "packet" / "ai_duplicate_packet_1.jsonl").read_text(encoding="utf-8")
    item = json.loads(text.splitlines()[0])
    assert item["pair_id"] == "m1|m2" and item["event_a"]["records"][0]["title"]
    for leaked in ("AI SAID THIS", "AI-attacker", "p_same", "stratum", "census", "ai_involvement", "weight"):
        assert leaked not in text


def test_labelled_rows_skip_unsure_and_missing_and_reject_unknown_labels():
    sample = {k: {"stratum": "census", "weight": "1.0"} for k in ("a|b", "c|d", "e|f", "g|h")}
    rows, unsure, missing = cal.labelled_rows(sample, {"a|b": "same", "c|d": "unsure", "e|f": "different"},
                                              {"a|b": 0.9, "c|d": 0.5, "e|f": 0.1, "g|h": 0.2})
    assert [r["same"] for r in rows] == [True, False] and (unsure, missing) == (1, 1)
    with pytest.raises(SystemExit):
        cal.labelled_rows({"a|b": {"stratum": "census", "weight": "1"}}, {"a|b": "maybe"}, {"a|b": 0.5})


# ---------------------------------------------------------------- registration

def test_the_step_is_registered_with_recalibration_and_labels_protect_merges(conn, tmp_path, monkeypatch):
    assert rc.SPECS["ai-duplicates"].escalates and "ai-duplicates" in rc.PLANS and "ai-duplicates" in rc.CALIBRATE
    registered = json.loads(rc.SNAPSHOT_REGISTRY.read_text(encoding="utf-8"))["snapshots"]
    assert "ai-duplicates" in registered
    labels = _labels(tmp_path, "ai_labels.csv", [("m1|m2", "different")])
    monkeypatch.setattr(ad, "LABELS_PATH", labels)
    assert ("m1", "m2") in lc.ai_labelled_different_pairs()


def test_the_recalibration_plan_finds_labelled_pairs_beyond_the_cap(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "LABELS_PATH", _labels(tmp_path, "ai_labels.csv", [("m1|m2", "same"), ("g1|g2", "different"),
                                                                              ("gone|pair", "same")]))
    plan = rc.PLANS["ai-duplicates"](conn)
    assert sorted(p["pair_id"] for p in plan.items) == ["g1|g2", "m1|m2"] and plan.labelled == 3
