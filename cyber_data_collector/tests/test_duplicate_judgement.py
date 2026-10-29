"""Jev duplicate judgement: date provenance in the evidence, calibrated merges,
and labelled pairs as ground truth."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

pytest.importorskip("typesafe_sdk")

from cyber_data_collector.dedup import date_provenance as dp  # noqa: E402
from cyber_data_collector.dedup import duplicate_judgement as dj  # noqa: E402
from cyber_data_collector.dedup.ledger import DedupLedger  # noqa: E402
from cyber_data_collector.utils.jev import JevJudge  # noqa: E402
from scripts.calibrate_duplicates import merge_cutoff  # noqa: E402


@pytest.mark.parametrize("incident, published, found, researched, text, basis", [
    ("2023-06-08", "2023-06-08", "2026-05-03", None, "", "publication date (may lag the incident)"),
    ("2026-05-03", None, "2026-05-03", None, "", "date the record was found (may lag the incident)"),
    ("2023-06-01", "2023-06-08", None, "2023-06-01", "", "Perplexity research"),
    ("2023-05-24", "2023-06-08", None, None, "The attack began on 24 May 2023.", "stated in the record"),
    # Reviewer finding 26: matching nothing is not "stated"; finding 24: a month is not a day.
    ("2023-05-24", "2023-06-08", None, None, "", "unknown (not a source date and not found in the text)"),
    ("2023-05-24", "2023-06-08", None, None, "The breach happened in May 2023.",
     "unknown (not a source date and not found in the text)"),
    ("2023-05-01", "2023-06-08", None, None, "The breach happened in May 2023.", "stated in the record"),
    (None, "2023-06-08", None, None, "", "no date"),
])
def test_date_basis(incident, published, found, researched, text, basis):
    meta = json.dumps({"publication_date": published}) if published else None
    pplx = json.dumps({"earliest_event_date": researched}) if researched else None
    assert dp.record_dates(incident, None, meta, found, pplx, text=text)["basis"] == basis


@pytest.mark.parametrize("date, precision", [
    ("2023-06-08", "day"),
    ("2023-06-01", "month only, or the 1st (stored as the 1st)"),
    ("2023-01-01", "year only (stored as 1 January)"),
    (None, "unknown"),
])
def test_date_precision(date, precision):
    assert dp.precision(date) == precision


def NO_SEARCH(query, model):
    return {"answer": "NONE FOUND", "citations": []}


NO_ESCALATION = object()   # never called: the fake Jev scores sit outside the escalation band


class FakeTypeSafe:
    def __init__(self, p_same):
        self.p_same = p_same
        self.states = []

    def system_one(self, state, questions, model=None):
        self.states.append(state)
        return SimpleNamespace(model="jev-1.14.0", usage=None,
                               answers={q: SimpleNamespace(type="noul", noul=self.p_same) for q in questions})


@pytest.fixture
def merges():
    return []


@pytest.fixture
def conn(monkeypatch, merges):
    monkeypatch.setattr(DedupLedger, "merge_events",
                        lambda self, target, source, reason, actor="human", respect_overrides=True:
                        merges.append((target, source)))
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY, entity_name TEXT);
        CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, title TEXT, event_date TEXT,
            victim_organization_name TEXT, status TEXT, records_affected INTEGER, attack_method TEXT,
            event_type TEXT, vendor_organization_name TEXT, summary TEXT, description TEXT,
            master_enriched_event_id TEXT);
        CREATE TABLE DeduplicatedEventEntities (deduplicated_event_id TEXT, entity_id INTEGER, relationship_type TEXT);
        CREATE TABLE EventDeduplicationMap (enriched_event_id TEXT, deduplicated_event_id TEXT);
        CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY, raw_event_id TEXT, title TEXT,
            event_date TEXT, summary TEXT, description TEXT, perplexity_enrichment_data TEXT);
        CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_url TEXT, raw_content TEXT,
            raw_description TEXT, event_date TEXT, source_metadata TEXT, discovered_at TEXT);
        INSERT INTO EntitiesV2 VALUES (1, 'ACT Government');
    """)
    for did, title, date in [("e1", "Business - ACT Government", "2023-05-24"),
                             ("e2", "Free three-year-old preschool - ACT Government", "2023-06-08")]:
        c.execute("INSERT INTO DeduplicatedEvents VALUES (?, ?, ?, 'ACT Government', 'Active', NULL, NULL, NULL, "
                  "NULL, NULL, NULL, ?)", (did, title, date, f"{did}-r"))
        c.execute("INSERT INTO DeduplicatedEventEntities VALUES (?, 1, 'victim')", (did,))
        c.execute("INSERT INTO EventDeduplicationMap VALUES (?, ?)", (f"{did}-r", did))
        c.execute("INSERT INTO EnrichedEvents VALUES (?, ?, ?, ?, NULL, NULL, '{}')", (f"{did}-r", f"{did}-r", title, date))
        c.execute("INSERT INTO RawEvents VALUES (?, 'https://www.act.gov.au/x', 'Page text', NULL, ?, ?, '2026-05-03')",
                  (f"{did}-r", date, json.dumps({"publication_date": date})))
    return c


def _cal(tmp_path, cutoff):
    path = tmp_path / "cal.json"
    path.write_text(json.dumps({"cutoff": cutoff, **dj.calibration_identity()}))
    return path


def _labels(tmp_path, label):
    path = tmp_path / "labels.csv"
    rows = f"e1|e2,{label},r,packet,t\n" if label else "x|y,same,an unrelated labelled pair,packet,t\n"
    path.write_text("pair_id,label,rationale,corroboration,labeller\n" + rows, encoding="utf-8")
    return path


def test_pairs_carry_gap_and_date_provenance(conn):
    pairs = dj.candidate_pairs(conn)
    assert [(p["pair_id"], p["gap_days"]) for p in pairs] == [("e1|e2", 15)]
    state = dj.build_pair_state(conn, pairs[0])
    record = state["event_a"]["records"][0]
    assert record["date"]["basis"] == "publication date (may lag the incident)"
    assert state["event_a"]["event"]["records_carrying_event_date"] == "1 of 1"


def test_nothing_merges_without_a_calibration(conn, merges, tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    result = dj.check_duplicates(conn, timeline_search=NO_SEARCH, escalation_client=NO_ESCALATION, apply=True, judge=JevJudge(conn, client=FakeTypeSafe(0.99)),
                                 calibration_path=tmp_path / "none.json", labels_path=_labels(tmp_path, None))
    assert result["judged"] == 1 and not result["calibrated"] and merges == []


def test_calibrated_cutoff_merges(conn, merges, tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    result = dj.check_duplicates(conn, timeline_search=NO_SEARCH, escalation_client=NO_ESCALATION, apply=True, judge=JevJudge(conn, client=FakeTypeSafe(0.9)),
                                 calibration_path=_cal(tmp_path, 0.8), labels_path=_labels(tmp_path, None))
    assert len(result["proposals"]) == 1 and len(merges) == 1 and set(merges[0]) == {"e1", "e2"}


def test_labelled_different_is_never_merged(conn, merges, tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    result = dj.check_duplicates(conn, timeline_search=NO_SEARCH, escalation_client=NO_ESCALATION, apply=True, judge=JevJudge(conn, client=FakeTypeSafe(0.99)),
                                 calibration_path=_cal(tmp_path, 0.5), labels_path=_labels(tmp_path, "different"))
    assert result["proposals"] == [] and merges == []


def test_labelled_same_is_merged_below_the_cutoff(conn, merges, tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    dj.check_duplicates(conn, timeline_search=NO_SEARCH, escalation_client=NO_ESCALATION, apply=True, judge=JevJudge(conn, client=FakeTypeSafe(0.1)),
                        calibration_path=_cal(tmp_path, 0.8), labels_path=_labels(tmp_path, "same"))
    assert len(merges) == 1


def test_merge_cutoff_is_the_lowest_meeting_precision():
    rows = [{"p": p, "same": s} for p, s in
            [(0.95, True), (0.9, True), (0.85, True), (0.8, True), (0.7, True), (0.6, False), (0.1, False)]]
    chosen, _ = merge_cutoff(rows, precision=0.95, min_merges=1)
    # Merging down to 0.7 is all correct; reaching 0.6 lets a different pair in.
    assert chosen["cutoff"] == pytest.approx(0.7, abs=1e-5)
    assert chosen["merged_labelled"] == 5 and chosen["precision"] == 1.0


def test_unreadable_labels_stop_merging(conn, merges, tmp_path, monkeypatch):
    """Reviewer finding 6: without labels, 'different' pairs are unprotected - so nothing merges."""
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    with pytest.raises(RuntimeError, match="refusing to merge"):
        dj.check_duplicates(conn, timeline_search=NO_SEARCH, escalation_client=NO_ESCALATION, apply=True,
                            judge=JevJudge(conn, client=FakeTypeSafe(0.99)),
                            calibration_path=_cal(tmp_path, 0.5), labels_path=tmp_path / "missing.csv")
    assert merges == []


def test_failed_escalation_leaves_the_pair_undecided(conn, merges, tmp_path, monkeypatch):
    """Reviewer finding 7: a mid-band pair whose escalation fails is not merged on Jev's score."""
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    import cyber_data_collector.utils.escalation as esc
    monkeypatch.setattr(esc, "escalate", lambda *a, **k: None)
    result = dj.check_duplicates(conn, timeline_search=NO_SEARCH, apply=True,
                                 judge=JevJudge(conn, client=FakeTypeSafe(0.6)),
                                 calibration_path=_cal(tmp_path, 0.5), labels_path=_labels(tmp_path, None))
    assert result["escalation_failed"] == ["e1|e2"] and merges == []


def test_event_date_basis_and_precision_are_stored(conn):
    """Priority 7: the event records how its date was determined and how precise it is."""
    changed = dp.refresh_event_date_provenance(conn)
    row = conn.execute("SELECT date_basis, date_precision FROM DeduplicatedEvents "
                       "WHERE deduplicated_event_id = 'e2'").fetchone()
    assert changed == 2
    assert tuple(row) == ("publication date (may lag the incident)", "day")
    assert dp.refresh_event_date_provenance(conn) == 0        # idempotent


def test_events_are_paired_when_their_victims_are_one_organisation_under_two_names(conn):
    """2026-10-29: "ALS Global" and "ALS", or "St Vincent's Health Australia" and "St Vincent's
    Health", were one organisation in the recurrence analysis but separate entity rows, so a
    leak-site claim and the disclosure days later were never compared and counted as a repeat."""
    conn.execute("INSERT INTO EntitiesV2 VALUES (2, 'ACT Government Pty Ltd')")
    conn.execute("UPDATE DeduplicatedEventEntities SET entity_id = 2 WHERE deduplicated_event_id = 'e2'")
    [pair] = dj.candidate_pairs(conn)
    assert (pair["a"], pair["b"], pair["gap_days"]) == ("e1", "e2", 15)


def test_undated_events_are_still_paired_by_victim(conn):
    """2026-10-28: undated events were left out of the candidates, so 35 undated reports of
    one incident were never compared. A missing date is no evidence of a separate incident."""
    conn.execute("UPDATE DeduplicatedEvents SET event_date = NULL WHERE deduplicated_event_id = 'e2'")
    [pair] = dj.candidate_pairs(conn)
    assert (pair["a"], pair["b"], pair["gap_days"]) == ("e1", "e2", dj.GAP_UNKNOWN)
    assert dj.build_pair_state(conn, pair)["gap_days"] == dj.GAP_UNKNOWN


def test_each_record_is_shown_with_its_own_date_provenance(conn):
    """2026-10-29: the duplicate check joined the evidence's records to the date provenance's by
    position, two lists ordered by separate queries over different sets (the provenance included
    inactive records), so 331 of 1,190 displayed records had another record's date and basis."""
    conn.execute("ALTER TABLE EnrichedEvents ADD COLUMN status TEXT")
    # An inactive record that sorts FIRST in a by-text-then-id list, and an active one that sorts last.
    for rid, title, day, text, status in [("a-inactive", "Inactive copy", "2020-01-01", "Long page text", "Inactive"),
                                          ("z-second", "Second source", "2023-07-01", None, "Active")]:
        conn.execute("INSERT INTO EnrichedEvents VALUES (?, ?, ?, ?, NULL, NULL, '{}', ?)",
                     (rid, rid, title, day, status))
        conn.execute("INSERT INTO RawEvents VALUES (?, 'https://x.example/a', ?, NULL, ?, '{}', '2026-05-03')",
                     (rid, text, day))
        conn.execute("INSERT INTO EventDeduplicationMap VALUES (?, 'e1')", (rid,))
    state = dj.build_pair_state(conn, dj.candidate_pairs(conn)[0])
    shown = {r["title"]: r for r in state["event_a"]["records"]}
    assert set(shown) == {"Business - ACT Government", "Second source"}      # the inactive record is not shown
    assert shown["Business - ACT Government"]["date"]["date"] == "2023-05-24"
    assert shown["Second source"]["date"]["date"] == "2023-07-01"
    assert all("enriched_event_id" not in r and "enriched_event_id" not in r["date"]
               for r in state["event_a"]["records"])                        # the join key is not shown to a model
    assert state["event_a"]["event"]["records_carrying_event_date"] == "1 of 2"   # active records only


def test_a_record_with_no_provenance_is_an_error_not_a_blank(conn, monkeypatch):
    from cyber_data_collector.dedup import date_provenance
    real = date_provenance.event_dates
    monkeypatch.setattr(date_provenance, "event_dates", lambda *a, **k: {**real(*a, **k), "records": []})
    with pytest.raises(ValueError, match="no date provenance"):
        dj.build_pair_state(conn, dj.candidate_pairs(conn)[0])
