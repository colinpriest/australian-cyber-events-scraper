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
