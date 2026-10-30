"""Sampling, blind packets and cut-off calibration for the same-organisation coherence check."""
from __future__ import annotations

import csv
import json
import sqlite3

import pytest

pytest.importorskip("typesafe_sdk")

from cyber_data_collector.dedup import coherence_same_org as so  # noqa: E402
from cyber_data_collector.tests.test_coherence_same_org import FakeTypeSafe, _event, _models, conn  # noqa: E402,F401
from cyber_data_collector.utils.jev import JevJudge  # noqa: E402
from scripts import calibrate_coherence_same_org as cal  # noqa: E402


@pytest.fixture
def world(conn, tmp_path, monkeypatch):
    """The test database as a file (the script opens it read-only), its one candidate judged at 0.05."""
    so.judge_members(conn, [(_event(conn), "far_verified")], judge=JevJudge(conn, client=FakeTypeSafe(default=0.05)))
    db = tmp_path / "snapshot.db"
    target = sqlite3.connect(db)
    conn.backup(target)
    target.close()
    monkeypatch.setattr(so, "ROOT", tmp_path)
    monkeypatch.setattr(so, "LABELS_PATH", tmp_path / "labels.csv")
    monkeypatch.setattr(so, "CALIBRATION_PATH", tmp_path / "calibration.json")
    monkeypatch.setattr(cal, "SAMPLE_PATH", tmp_path / "sample.csv")
    return db, tmp_path


def _rows(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_candidates_are_stratified_on_their_stored_score_and_others_form_n(world):
    db, tmp = world
    assert cal.main(["--db", str(db), "sample", "--sample-other", "3"]) == 0
    rows = _rows(tmp / "sample.csv")
    by = {}
    for r in rows:
        by.setdefault(r["stratum"], []).append(r)
    assert [r["enriched_event_id"] for r in by["A"]] == ["far_verified"] and by["A"][0]["weight"] == "1.0"
    assert len(by["N"]) == 3 and "far_verified" not in {r["enriched_event_id"] for r in by["N"]}
    # other-organisation members belong to the stray check and are never sampled
    assert "other_org" not in {r["enriched_event_id"] for r in rows}


def test_a_candidate_with_no_stored_score_fails_the_sample(world, capsys):
    db, tmp = world
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM ModelJudgements")
    conn.execute("DELETE FROM FinalDecisions")
    conn.commit()
    conn.close()
    assert cal.main(["--db", str(db), "sample"]) == 1
    assert "no stored final score" in capsys.readouterr().out


def test_the_packet_is_blind(world):
    db, tmp = world
    cal.main(["--db", str(db), "sample", "--sample-other", "3"])
    out = tmp / "packet"
    assert cal.main(["--db", str(db), "packet", "--out", str(out)]) == 0
    text = (out / "coherence_same_org_packet_1.jsonl").read_text(encoding="utf-8")
    lines = [json.loads(line) for line in text.splitlines()]
    assert len(lines) == 4
    for item in lines:
        assert "flagged_because" not in item
        assert {"deduplicated_event_id", "enriched_event_id", "event", "record", "other_records"} <= set(item)
    for forbidden in ("stratum", "weight", "p_jev", "p_same", "p_escalated", "flagged_because"):
        assert forbidden not in text


def test_calibrate_chooses_a_cutoff_from_labels_and_records_the_identity(world):
    db, tmp = world
    cal.main(["--db", str(db), "sample", "--sample-other", "3"])
    (tmp / "labels.csv").write_text(
        "deduplicated_event_id,enriched_event_id,label,rationale,incident,labeller\n"
        "youx,far_verified,different,separate Feb 2026 theft,MongoDB theft 2026,t\n"
        + "".join(f"{r['deduplicated_event_id']},{r['enriched_event_id']},same,r,,t\n"
                  for r in _rows(tmp / "sample.csv") if r["stratum"] == "N"), encoding="utf-8")
    assert cal.main(["--db", str(db), "calibrate", "--min-splits", "1"]) == 0
    result = json.loads((tmp / "calibration.json").read_text(encoding="utf-8"))
    assert result["cutoff"] is not None and 0.05 < result["cutoff"] < 0.5
    assert result["labelled_used"] == 1 and result["labelled_different"] == 1
    assert result["evidence"] == so.evidence_fingerprint()
    assert result["candidate_selection_misses"]["labelled_different"] == 0
    assert so.active_calibration(tmp / "calibration.json")["cutoff"] == result["cutoff"]


# --- frames: a later labelling snapshot ------------------------------------------------------

def _second_snapshot(first, tmp, p_new=0.03):
    """A later snapshot of the same database: youX gains a new far-dated candidate and two ordinary records."""
    second = tmp / "snapshot2.db"
    src, dst = sqlite3.connect(first), sqlite3.connect(second)
    src.backup(dst)
    src.close()
    for eid, title, when, pe_date in (("far_new", "youX second extortion", "2026-05-02", "2026-05-02"),
                                      ("a4", "youX exposure follow-up", "2025-03-10", "2025-03-10"),
                                      ("a5", "youX notifies customers", "2025-03-10", "2025-03-10")):
        dst.execute("INSERT INTO EnrichedEvents VALUES (?,?,?,?,NULL,NULL,?)",
                    (eid, eid, title, when, json.dumps({"formal_entity_name": "youX", "earliest_event_date": pe_date})))
        dst.execute("INSERT INTO RawEvents VALUES (?,?,?,NULL,NULL,NULL,?)",
                    (eid, f"https://news.example.com/{eid}", f"Page about {title}", "2026-06-01"))
        dst.execute("INSERT INTO EventDeduplicationMap VALUES (?,?,?)", ("m" + eid, eid, "youx"))
    dst.commit()
    event = cal._events(dst)["youx"]
    assert "far_new" in event["candidates"] and "far_verified" in event["candidates"]
    so.judge_members(dst, [(event, "far_new")],
                     judge=JevJudge(dst, client=FakeTypeSafe(p_by_title={"second extortion": p_new}, default=0.9)))
    dst.close()
    return second


def test_rows_written_before_frames_belong_to_frame_one(tmp_path):
    path = tmp_path / "old.csv"
    path.write_text("deduplicated_event_id,enriched_event_id,stratum,weight\nyoux,a1,N,2.0\n", encoding="utf-8")
    assert [(r["stratum"], r["frame"]) for r in cal.read_sample(path)] == [("N", "1")]
    assert cal.is_candidate_stratum("A") and cal.is_candidate_stratum("2C") and not cal.is_candidate_stratum("2N")


def test_a_later_frame_holds_only_subjects_new_to_it_and_keeps_frame_one(world):
    db, tmp = world
    assert cal.main(["--db", str(db), "sample", "--sample-other", "3"]) == 0
    frame_one = cal.read_sample()
    second = _second_snapshot(db, tmp)
    assert cal.main(["--db", str(second), "sample", "--frame", "2", "--earlier-frame-db", f"1={db}",
                     "--census-below", "0.15", "--sample-other", "5"]) == 0
    rows = cal.read_sample()
    assert rows[:len(frame_one)] == frame_one                         # frame 1 untouched
    added = {r["enriched_event_id"]: r for r in rows[len(frame_one):]}
    # far_verified was a candidate in frame 1; a1-a3 and the far_* records were in its population as others.
    assert set(added) == {"far_new", "a4", "a5"}
    assert added["far_new"]["stratum"] == "2A" and float(added["far_new"]["weight"]) == 1.0
    assert added["a4"]["stratum"] == added["a5"]["stratum"] == "2N"
    assert {r["frame"] for r in added.values()} == {"2"}


def test_a_later_frame_needs_every_earlier_snapshot_and_is_drawn_once(world):
    db, tmp = world
    cal.main(["--db", str(db), "sample", "--sample-other", "3"])
    second = _second_snapshot(db, tmp)
    with pytest.raises(SystemExit, match="earlier-frame-db 1="):
        cal.main(["--db", str(second), "sample", "--frame", "2"])
    assert cal.main(["--db", str(second), "sample", "--frame", "2", "--earlier-frame-db", f"1={db}"]) == 0
    assert cal.main(["--db", str(second), "sample", "--frame", "2", "--earlier-frame-db", f"1={db}"]) == 2
    assert cal.main(["--db", str(db), "sample"]) == 2          # redrawing frame 1 would move frame 2's population


def test_each_frame_is_packeted_and_calibrated_on_its_own_snapshot(world):
    db, tmp = world
    cal.main(["--db", str(db), "sample", "--sample-other", "3"])
    second = _second_snapshot(db, tmp)
    cal.main(["--db", str(second), "sample", "--frame", "2", "--earlier-frame-db", f"1={db}", "--census-below", "0.15"])
    out = tmp / "packet2"
    # frame 2's record is not in frame 1's snapshot: a packet of every row on it is refused, not half-written
    assert cal.main(["--db", str(db), "packet", "--out", str(tmp / "all")]) == 2
    assert cal.main(["--db", str(second), "packet", "--frame", "2", "--out", str(out)]) == 0
    items = [json.loads(line) for line in
             (out / "coherence_same_org_packet_1.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sorted(i["enriched_event_id"] for i in items) == ["a4", "a5", "far_new"]
    (tmp / "labels.csv").write_text(
        "deduplicated_event_id,enriched_event_id,label,rationale,incident,labeller\n"
        "youx,far_verified,different,separate Feb 2026 theft,,t\n"
        "youx,far_new,different,separate May 2026 extortion,,t\n", encoding="utf-8")
    assert cal.main(["--db", str(db), "calibrate", "--min-splits", "1"]) == 2       # frame 2 needs its database
    assert cal.main(["--db", str(db), "calibrate", "--min-splits", "1", "--frame-db", f"2={second}"]) == 0
    result = json.loads((tmp / "calibration.json").read_text(encoding="utf-8"))
    assert result["labelled_used"] == 2 and result["labelled_used_by_frame"] == {"1": 1, "2": 1}
    assert result["frame_databases"] == {"1": db.name, "2": second.name}
    assert {r["stratum"] for r in result["labelled_rows"]["split"]} == {"A", "2A"}


def test_recalibration_refuses_a_sample_spanning_frames(world):
    from cyber_data_collector.dedup import recalibration as rc

    db, tmp = world
    cal.main(["--db", str(db), "sample", "--sample-other", "3"])
    second = _second_snapshot(db, tmp)
    cal.main(["--db", str(second), "sample", "--frame", "2", "--earlier-frame-db", f"1={db}", "--census-below", "0.15"])
    (tmp / "labels.csv").write_text(
        "deduplicated_event_id,enriched_event_id,label,rationale,incident,labeller\n"
        "youx,far_verified,different,r,,t\nyoux,far_new,different,r,,t\n", encoding="utf-8")
    with pytest.raises(rc.PrerequisiteMissing, match="frame"):
        cal.plan(sqlite3.connect(db), so)
