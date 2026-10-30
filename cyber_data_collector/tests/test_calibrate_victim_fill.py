"""Frames in the victim-fill sample: a later labelling snapshot added without disturbing the earlier one.

Offline: the population and the stored choices are faked from a small table in each snapshot file.
"""

from __future__ import annotations

import csv
import sqlite3

import pytest

from scripts import calibrate_victim_fill as cal


def _snapshot(path, events):
    """events: {event id: stored best probability or None}; every one has no usable victim."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE V (id TEXT, p REAL)")
    conn.executemany("INSERT INTO V VALUES (?, ?)", list(events.items()))
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def fake(monkeypatch, tmp_path):
    committed = cal.SAMPLE_PATH.read_bytes()
    monkeypatch.setattr(cal, "SAMPLE_PATH", tmp_path / "sample.csv")
    yield from _with_fakes(monkeypatch, tmp_path)
    assert cal.vf.ROOT.joinpath("evaluation", "victim_fill_sample.csv").read_bytes() == committed


def _with_fakes(monkeypatch, tmp_path):
    monkeypatch.setattr(cal.vf, "victimless_events",
                        lambda conn: [{"deduplicated_event_id": r[0]} for r in conn.execute("SELECT id FROM V")])
    monkeypatch.setattr(cal.vf, "stored_best_probabilities",
                        lambda conn: dict(conn.execute("SELECT id, p FROM V WHERE p IS NOT NULL")))
    yield tmp_path


def test_rows_written_before_frames_belong_to_frame_one(tmp_path):
    path = tmp_path / "old.csv"
    path.write_text("deduplicated_event_id,stratum,weight\na,A,3.85\nb,D,1.0\n", encoding="utf-8")
    assert [(r["deduplicated_event_id"], r["stratum"], r["frame"]) for r in cal.read_sample(path)] == [
        ("a", "A", "1"), ("b", "D", "1")]


def test_a_later_frame_excludes_the_earlier_population_and_keeps_frame_one(fake):
    first = _snapshot(fake / "s1.db", {"a": 0.2, "b": 0.99, "c": None})
    second = _snapshot(fake / "s2.db", {"a": 0.98, "b": 0.99, "d": 0.99, "e": 0.97, "f": 0.3, "c": 0.9})
    assert cal.main(["--db", first, "sample"]) == 0
    frame_one = cal.read_sample()
    assert cal.main(["--db", second, "sample", "--frame", "2", "--earlier-frame-db", f"1={first}"]) == 0
    rows = cal.read_sample()
    assert rows[:len(frame_one)] == frame_one                       # frame 1 untouched
    added = {r["deduplicated_event_id"]: r for r in rows[len(frame_one):]}
    # a, b and c were in frame 1's population (victimless there), whatever they score now.
    assert set(added) == {"d", "e", "f"}
    assert {r["frame"] for r in added.values()} == {"2"}
    assert added["d"]["stratum"] == added["e"]["stratum"] == "2D" and added["f"]["stratum"] == "2A"
    assert float(added["d"]["weight"]) == 1.0                      # a census of its own band


def test_a_later_frame_needs_every_earlier_snapshot_and_is_drawn_once(fake):
    first = _snapshot(fake / "s1.db", {"a": 0.2})
    second = _snapshot(fake / "s2.db", {"d": 0.99})
    assert cal.main(["--db", first, "sample"]) == 0
    with pytest.raises(SystemExit, match="earlier-frame-db 1="):
        cal.main(["--db", second, "sample", "--frame", "2"])
    assert cal.main(["--db", second, "sample", "--frame", "2", "--earlier-frame-db", f"1={first}"]) == 0
    assert cal.main(["--db", second, "sample", "--frame", "2", "--earlier-frame-db", f"1={first}"]) == 2
    # Redrawing frame 1 would change what frame 2 excludes.
    assert cal.main(["--db", first, "sample"]) == 2


def test_every_labelled_frame_needs_its_database():
    assert cal.frame_databases("s1.db", {"2": "s2.db"}, ["1", "2"]) == {"1": "s1.db", "2": "s2.db"}
    with pytest.raises(ValueError, match="--frame-db 2="):
        cal.frame_databases("s1.db", {}, ["1", "2"])
    with pytest.raises(ValueError):
        cal.parse_frame_dbs(["2"])
    with pytest.raises(ValueError):
        cal.parse_frame_dbs(["2=a", "2=b"])


def test_packets_of_one_frame_only(fake, monkeypatch):
    first = _snapshot(fake / "s1.db", {"a": 0.2})
    second = _snapshot(fake / "s2.db", {"d": 0.99, "e": 0.6})
    cal.main(["--db", first, "sample"])
    cal.main(["--db", second, "sample", "--frame", "2", "--earlier-frame-db", f"1={first}"])
    monkeypatch.setattr(cal, "packet", lambda conn, did: {"deduplicated_event_id": did})
    out = fake / "packets"
    assert cal.main(["--db", second, "packets", "--frame", "2", "--out", str(out)]) == 0
    import json
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    assert sorted(i["deduplicated_event_id"] for i in index) == ["d", "e"]


def test_recalibration_refuses_a_sample_spanning_frames(fake, monkeypatch):
    from cyber_data_collector.dedup import recalibration as rc

    path = cal.SAMPLE_PATH
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cal.SAMPLE_COLUMNS)
        w.writerows([["a", "A", 1.0, "1"], ["d", "2D", 1.0, "2"]])
    monkeypatch.setattr(cal.vf, "load_labels", lambda required=False: {
        "a": {"label": "none_named"}, "d": {"label": "named", "correct_victim": "X"}})
    with pytest.raises(rc.PrerequisiteMissing, match="frame"):
        rc._plan_victim_fill(sqlite3.connect(":memory:"))
