#!/usr/bin/env python3
"""Does the Google-calibrated page-gate cut-off hold for the other discovery sources?

The source gate's cut-off (``evaluation/source_gate_calibration.json``) was
calibrated on 170 labelled Google pages. The discovery page gate enforces it
only for sources where its precision has been shown; this script measures that
from the existing event labels (``evaluation/event_reality_labels.csv``).

Method. Each labelled event's member records from the source are judged with
the gate's own question and state (stored in ``ModelJudgements``; a rerun
reuses them). A record the gate would drop is

* a correct drop when its event is labelled ``not_incident`` (nothing in the
  event is an incident report), and
* counted as a **wrong** drop when its event is labelled ``real`` - a lower
  bound: a real event can hold a junk page, which the gate would rightly drop.

``unsure`` events are excluded. Precision is therefore a lower bound. Its
uncertainty is a bootstrap over **organisations** (the event's victim), so
records of one organisation never sit on both sides of a resample.
A source is accepted when at least 10 records would be dropped and the
lower-bound precision is at least 0.95 - the calibration's own rule.

Writes ``evaluation/page_gate_source_evaluation.json`` (read by
``filtering/page_gate.py``, which honours it only for the same model, question
version and cut-off) and ``evaluation/page_gate_source_evaluation.csv``.

Usage (from the project root; judge on a copy to keep the live database
untouched, or on the live database to keep the judgements):
    python scripts/evaluate_page_gate_sources.py --db <copy of instance/cyber_events.db>
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.dedup import source_gate as sg  # noqa: E402
from cyber_data_collector.filtering import page_gate as pg  # noqa: E402
from cyber_data_collector.utils.config_manager import resolve_db_path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LABELS = ROOT / "evaluation" / "event_reality_labels.csv"
OUT_JSON = ROOT / "evaluation" / "page_gate_source_evaluation.json"
OUT_CSV = ROOT / "evaluation" / "page_gate_source_evaluation.csv"
PRECISION_TARGET = 0.95
MIN_DROPS = 10


def labelled_members(conn: sqlite3.Connection, labels_path: Path = LABELS) -> List[Dict[str, Any]]:
    """Every record of a labelled event, with the event's label and organisation."""
    with labels_path.open(encoding="utf-8") as fh:
        labels = {r["deduplicated_event_id"]: r["label"] for r in csv.DictReader(fh)}
    out = []
    for event_id, label in labels.items():
        if label == "unsure":
            continue
        rows = conn.execute(
            """SELECT r.raw_event_id, r.source_type, r.raw_title, r.raw_description, r.raw_content, r.source_url,
                      COALESCE(NULLIF(d.victim_organization_name, ''), d.deduplicated_event_id)
               FROM EventDeduplicationMap m
               JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
               JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
               JOIN DeduplicatedEvents d ON d.deduplicated_event_id = m.deduplicated_event_id
               WHERE m.deduplicated_event_id = ?""", (event_id,)).fetchall()
        for rid, source, title, desc, content, url, org in rows:
            out.append({"raw_event_id": rid, "source_type": source, "raw_title": title, "raw_description": desc,
                        "raw_content": content, "source_url": url, "organisation": str(org).strip().lower(),
                        "deduplicated_event_id": event_id, "event_label": label})
    return out


def precision(rows: List[Dict[str, Any]], cutoff: float) -> Dict[str, Any]:
    drops = [r for r in rows if r["probability"] is not None and r["probability"] < cutoff]
    correct = sum(r["event_label"] == "not_incident" for r in drops)
    return {"judged": sum(r["probability"] is not None for r in rows), "would_drop": len(drops),
            "correct_drops": correct, "precision_lower_bound": correct / len(drops) if drops else None}


def cluster_bootstrap(rows: List[Dict[str, Any]], cutoff: float, replicates: int = 1000, seed: int = 7):
    by_org = defaultdict(list)
    for r in rows:
        by_org[r["organisation"]].append(r)
    orgs = sorted(by_org)
    rng = random.Random(seed)
    values = []
    for _ in range(replicates):
        sample = [r for org in (rng.choice(orgs) for _ in orgs) for r in by_org[org]]
        p = precision(sample, cutoff)["precision_lower_bound"]
        if p is not None:
            values.append(p)
    values.sort()
    if not values:
        return None
    return {"replicates": len(values), "p05": values[int(0.05 * len(values))],
            "median": values[len(values) // 2], "p95": values[min(len(values) - 1, int(0.95 * len(values)))]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=None, help="database to judge on (judgements are stored in it)")
    parser.add_argument("--sources", nargs="*", default=None, help="limit to these source types")
    args = parser.parse_args(argv)

    cal = sg.active_calibration()
    if cal is None:
        print("No source-gate calibration for the current model and question; nothing to evaluate.")
        return 1
    cutoff = float(cal["cutoff"])
    conn = sqlite3.connect(resolve_db_path(args.db))
    try:
        members = [m for m in labelled_members(conn) if m["source_type"] != sg.SOURCE_TYPE
                   and (not args.sources or m["source_type"] in args.sources)]
        gate = pg.DiscoveryPageGate(conn, mode=pg.MODE_REPORT_ONLY)
        for i, m in enumerate(members, 1):
            decision = gate.decide(m)
            m["probability"] = decision.probability
            if i % 25 == 0:
                print(f"judged {i}/{len(members)}")
        gate.close()
    finally:
        conn.close()

    by_source = defaultdict(list)
    for m in members:
        by_source[m["source_type"]].append(m)
    sources = {}
    for source, rows in sorted(by_source.items()):
        result = precision(rows, cutoff)
        result["organisations"] = len({r["organisation"] for r in rows})
        result["organisation_bootstrap"] = cluster_bootstrap(rows, cutoff)
        p = result["precision_lower_bound"]
        result["accepted"] = bool(result["would_drop"] >= MIN_DROPS and p is not None and p >= PRECISION_TARGET)
        sources[source] = result
        print(f"{source}: {result}")

    OUT_JSON.write_text(json.dumps({
        **sg.calibration_identity(), "cutoff": cal["cutoff"], "precision_target": PRECISION_TARGET,
        "min_drops": MIN_DROPS, "labels": str(LABELS.relative_to(ROOT)),
        "method": ("event labels mapped to member records; drops from 'real' events counted wrong (lower bound); "
                   "unsure excluded; bootstrap over victim organisations"),
        "generated": datetime.now().isoformat(timespec="seconds"), "sources": sources}, indent=2), encoding="utf-8")
    with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["raw_event_id", "source_type", "deduplicated_event_id", "event_label", "organisation",
                         "probability", "would_drop"])
        for m in sorted(members, key=lambda r: (r["source_type"], r["raw_event_id"])):
            writer.writerow([m["raw_event_id"], m["source_type"], m["deduplicated_event_id"], m["event_label"],
                             m["organisation"], "" if m["probability"] is None else round(m["probability"], 4),
                             int(m["probability"] is not None and m["probability"] < cutoff)])
    print(f"Wrote {OUT_JSON} and {OUT_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
