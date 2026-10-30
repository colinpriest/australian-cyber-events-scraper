"""The check-duplicates command reports pairs a label settled, which carry no score."""

from __future__ import annotations

import argparse
import sqlite3

from cyber_data_collector.dedup import duplicate_judgement
from cyber_data_collector.utils import jev
from scripts import dedup_admin


def test_report_prints_label_settled_proposals(tmp_path, monkeypatch, capsys):
    db = tmp_path / "events.db"
    sqlite3.connect(db).close()
    result = {
        "calibrated": True, "cutoff": 0.81, "judged": 1, "pairs": 2, "candidates": {},
        "proposals": [
            {"pair_id": "aaaaaaaa|bbbbbbbb", "p_same": None, "label": "same", "gap_days": 0,
             "organisation": None, "route": "ai"},
            {"pair_id": "cccccccc|dddddddd", "p_same": 0.9, "gap_days": None, "organisation": "Medibank"},
        ],
        "merged": [],
    }
    monkeypatch.setattr(jev, "require", lambda: None)
    monkeypatch.setattr(duplicate_judgement, "check_duplicates", lambda conn, **kwargs: result)
    args = argparse.Namespace(db=str(db), apply=True, skip_ai_pairs=False, no_candidate_cap=False,
                              partners_per_event=8, long_gap_budget=5)
    assert dedup_admin.cmd_check_duplicates(args) == 0
    out = capsys.readouterr().out
    assert "label:same" in out
    assert "0.900  Medibank" in out
