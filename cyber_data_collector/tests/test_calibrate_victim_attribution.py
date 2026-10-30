"""Extending the victim-attribution sample with a census of a low-score region."""

from __future__ import annotations

import csv
import sqlite3
from types import SimpleNamespace

import pytest

from scripts import calibrate_victim_attribution as cal

ROWS = [
    {"pair_id": "a|1", "stratum": "A", "weight": "1.0"},     # old census
    {"pair_id": "b|1", "stratum": "B", "weight": "6.6"},     # old sample, now scores low
    {"pair_id": "b|2", "stratum": "B", "weight": "6.6"},     # old sample, still high
    {"pair_id": "c|1", "stratum": "C", "weight": "6.1778"},
]
PROBS = {"a|1": 0.9, "b|1": 0.05, "b|2": 0.8, "c|1": 0.95, "n|1": 0.1, "n|2": 0.49, "n|3": 0.5, "n|4": 0.99}
POPULATION = sorted(PROBS)


def test_census_adds_every_unsampled_pair_below_the_bound_with_weight_one():
    ext = cal.extend_sample(ROWS, PROBS, POPULATION, 0.5, "D")
    assert ext["region"] == ["b|1", "n|1", "n|2"]          # 0.5 itself is not below
    assert ext["added"] == ["n|1", "n|2"]
    rows = {r["pair_id"]: r for r in ext["rows"]}
    assert rows["n|1"]["stratum"] == "D" and float(rows["n|1"]["weight"]) == 1.0
    assert "n|3" not in rows and "n|4" not in rows


def test_a_sampled_pair_in_the_region_moves_in_with_weight_one_and_others_are_unchanged():
    ext = cal.extend_sample(ROWS, PROBS, POPULATION, 0.5, "D")
    rows = {r["pair_id"]: r for r in ext["rows"]}
    assert rows["b|1"] == {"pair_id": "b|1", "stratum": "D", "weight": 1.0}
    assert ext["moved"] == [{"pair_id": "b|1", "from_stratum": "B", "from_weight": "6.6"}]
    # Outside the region every row keeps its inclusion probability (stratum and weight as written).
    assert rows["b|2"] == ROWS[2] and rows["c|1"] == ROWS[3] and rows["a|1"] == ROWS[0]


def test_a_census_row_already_weight_one_keeps_its_stratum():
    probs = {**PROBS, "a|1": 0.01}
    ext = cal.extend_sample(ROWS, probs, POPULATION, 0.5, "D")
    rows = {r["pair_id"]: r for r in ext["rows"]}
    assert rows["a|1"] == ROWS[0] and "a|1" not in {m["pair_id"] for m in ext["moved"]}


def test_a_pair_without_a_current_score_stops_the_extension():
    probs = {k: v for k, v in PROBS.items() if k != "n|3"}
    with pytest.raises(cal.ExtensionError, match="no final score"):
        cal.extend_sample(ROWS, probs, POPULATION, 0.5, "D")


def test_an_existing_stratum_name_is_refused():
    with pytest.raises(cal.ExtensionError, match="already in the sample"):
        cal.extend_sample(ROWS, PROBS, POPULATION, 0.5, "B")


def test_the_command_writes_nothing_when_the_extension_fails(tmp_path, monkeypatch, capsys):
    sample = tmp_path / "sample.csv"
    with sample.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["pair_id", "stratum", "weight"])
        w.writeheader()
        w.writerows(ROWS)
    before = sample.read_text(encoding="utf-8")
    monkeypatch.setattr(cal, "SAMPLE_PATH", sample)
    monkeypatch.setattr(cal.vj, "pairs_to_check", lambda conn: [{"pair_id": p} for p in POPULATION])
    db = tmp_path / "x.db"
    sqlite3.connect(db).close()
    args = SimpleNamespace(db=str(db), extend_below=0.5, extend_stratum="D", frame_db=None)

    monkeypatch.setattr(cal.vj, "stored_final_probabilities", lambda conn, pairs: {"a|1": 0.9})
    assert cal.cmd_sample(args) == 2
    assert sample.read_text(encoding="utf-8") == before and "Nothing written" in capsys.readouterr().err

    monkeypatch.setattr(cal.vj, "stored_final_probabilities", lambda conn, pairs: dict(PROBS))
    assert cal.cmd_sample(args) == 0
    written = list(csv.DictReader(sample.open(encoding="utf-8")))
    assert [r["pair_id"] for r in written] == ["a|1", "b|1", "b|2", "c|1", "n|1", "n|2"]
    assert {r["pair_id"]: (r["stratum"], float(r["weight"])) for r in written}["b|1"] == ("D", 1.0)


def test_a_frame_limits_the_census_to_the_population_the_sample_was_drawn_on(tmp_path, monkeypatch, capsys):
    sample = tmp_path / "sample.csv"
    with sample.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["pair_id", "stratum", "weight"])
        w.writeheader()
        w.writerows(ROWS)
    monkeypatch.setattr(cal, "SAMPLE_PATH", sample)
    monkeypatch.setattr(cal.vj, "pairs_to_check", lambda conn: [{"pair_id": p} for p in POPULATION])
    # n|2 was not in the database the sample was drawn on (say, its event was rejected first); it needs no score.
    frame = [p for p in POPULATION if p != "n|2"]
    monkeypatch.setattr(cal, "frame_pairs", lambda path: frame)
    seen = {}

    def scores(conn, pairs):
        seen["asked"] = sorted(p["pair_id"] for p in pairs)
        return {k: v for k, v in PROBS.items() if k != "n|2"}

    monkeypatch.setattr(cal.vj, "stored_final_probabilities", scores)
    db = tmp_path / "x.db"
    sqlite3.connect(db).close()
    args = SimpleNamespace(db=str(db), extend_below=0.5, extend_stratum="D", frame_db="frame.db")
    assert cal.cmd_sample(args) == 0
    assert seen["asked"] == frame
    written = [r["pair_id"] for r in csv.DictReader(sample.open(encoding="utf-8"))]
    assert written == ["a|1", "b|1", "b|2", "c|1", "n|1"] and "1 outside it" in capsys.readouterr().out

    # A frame that does not hold the sampled pairs is not the sample's frame.
    monkeypatch.setattr(cal, "frame_pairs", lambda path: ["n|1"])
    before = sample.read_text(encoding="utf-8")
    args.extend_stratum = "E"
    assert cal.cmd_sample(args) == 2
    assert sample.read_text(encoding="utf-8") == before and "not in the frame database" in capsys.readouterr().err
