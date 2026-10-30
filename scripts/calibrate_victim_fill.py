"""Sample, package for blind labelling, and calibrate ``fill-victims``.

Three steps, each reproducible from committed files:

``sample``     Writes ``evaluation/victim_fill_sample.csv``: active events with no usable victim, stratified on
               the stored *final* probability of the best candidate (the reasoning model's where it was
               escalated, else Jev's), so run ``fill-victims`` (report only) on the database first.
               Bands ``A`` < 0.5, ``B`` 0.5-0.8, ``C`` 0.8-0.95, ``D`` >= 0.95, each sampled up to its quota
               (a census when smaller), weighted back to its band. Events with no candidate at all cannot be
               filled and are not sampled.

``packets``    Writes one JSON packet per sampled event to ``--out`` (not committed): the event, every
               record's page text and research, the candidate organisations the evidence offers - and **no
               score**. Labellers work from the packet plus web search.

``calibrate``  Reads ``evaluation/victim_fill_labels.csv``
               (``deduplicated_event_id,label,correct_victim,rationale,labeller``; label ``named`` with
               ``correct_victim``, ``none_named`` or ``unsure``) and the stored choices, and writes
               ``evaluation/victim_fill_calibration.json``: the **cut-off** is the lowest probability of the
               chosen candidate at which, weighted by stratum, at least ``--precision`` of the events it would
               fill are labelled with that very victim (at least ``--min-fills`` of them). An event labelled
               ``none_named`` that would be filled counts as an error. ``unsure`` labels are excluded and
               counted. A stratified bootstrap of the whole choose-then-test procedure is the out-of-sample
               estimate. The models, question versions and evidence fingerprint are recorded; the pipeline
               fills only while all match.

Calibrate on the database as it was when labelled (filling changes the population); the snapshot name is
recorded.

Usage (from the project root):
    python scripts/calibrate_victim_fill.py --db <snapshot> sample
    python scripts/calibrate_victim_fill.py --db <snapshot> packets --out <dir>
    python scripts/calibrate_victim_fill.py --db <snapshot> calibrate
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.dedup import deployment  # noqa: E402
from scripts.evaluate_calibrations import after_calibration  # noqa: E402
from cyber_data_collector.dedup import victim_fill as vf  # noqa: E402
from cyber_data_collector.utils.llm_models import add_model_arguments, apply_model_arguments  # noqa: E402
from scripts.calibrate_victim_attribution import replacement_bootstrap, replacement_threshold  # noqa: E402

SAMPLE_PATH = vf.ROOT / "evaluation" / "victim_fill_sample.csv"
BANDS = (("A", 0.0, 0.5), ("B", 0.5, 0.8), ("C", 0.8, 0.95), ("D", 0.95, 1.0001))
QUOTAS = {"A": 20, "B": 40, "C": 40, "D": 60}


def cmd_sample(args) -> int:
    conn = sqlite3.connect(args.db)
    probs = vf.stored_best_probabilities(conn)
    events = [e["deduplicated_event_id"] for e in vf.victimless_events(conn)]
    ids = [i for i in events if i in probs]
    rng = random.Random(args.seed)
    with SAMPLE_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["deduplicated_event_id", "stratum", "weight"])
        for name, lo, hi in BANDS:
            band = sorted(i for i in ids if lo <= probs[i] < hi)
            chosen = sorted(rng.sample(band, min(QUOTAS[name], len(band))))
            weight = len(band) / len(chosen) if chosen else 0.0
            w.writerows([(i, name, round(weight, 4)) for i in chosen])
            print(f"Stratum {name} ({lo}-{hi}): {len(chosen)} of {len(band)} events (weight {weight:.2f})")
    print(f"Seed {args.seed}; {len(events) - len(ids)} of {len(events)} eligible event(s) had no stored choice "
          f"(no candidates, or not yet run). Written {SAMPLE_PATH}")
    return 0


def packet(conn: sqlite3.Connection, dedup_id: str) -> Dict:
    """Everything a labeller sees for one event - never a score."""
    from cyber_data_collector.dedup import event_reality, event_organisations
    from cyber_data_collector.dedup import victim_judgement as vj

    state = event_reality.build_state(conn, dedup_id, corroborated=True)
    extracted = event_organisations.named_organisations(conn, dedup_id, stored_only=True)
    state["candidate_organisations_from_evidence"] = [
        c["name"] for c in vj.replacement_candidates(conn, dedup_id, [], organisations=extracted, include_scalar=True)]
    return {"deduplicated_event_id": dedup_id, **state}


def cmd_packets(args) -> int:
    conn = sqlite3.connect(args.db)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = list(csv.DictReader(SAMPLE_PATH.open(encoding="utf-8")))
    random.Random(args.seed).shuffle(rows)   # labellers see no stratum order either
    index = []
    for n, r in enumerate(rows, 1):
        name = f"packet_{n:03d}.json"
        (out / name).write_text(json.dumps(packet(conn, r["deduplicated_event_id"]), indent=1, ensure_ascii=False,
                                           default=str), encoding="utf-8")
        index.append({"packet": name, "deduplicated_event_id": r["deduplicated_event_id"]})
    (out / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    print(f"Wrote {len(index)} packets (no scores) to {out}")
    return 0


def cmd_calibrate(args) -> int:
    from cyber_data_collector.utils.jev import JevJudge

    sample = {r["deduplicated_event_id"]: r for r in csv.DictReader(SAMPLE_PATH.open(encoding="utf-8"))}
    labels = vf.load_labels(required=True)
    conn = sqlite3.connect(args.db)
    excluded = {"unsure": 0, "missing": 0}
    rows = []
    judge = JevJudge(conn)
    try:
        for did, s in sample.items():
            label = labels.get(did) or {}
            kind = label.get("label")
            if kind == "unsure":
                excluded["unsure"] += 1
                continue
            if kind not in ("named", "none_named"):
                excluded["missing"] += 1
                continue
            choice = vf.choose_victim(conn, did, judge)     # stored answers are reused
            wanted = (label.get("correct_victim") or "").strip()
            right = kind == "named" and vf.names_match(conn, choice["name"], wanted)
            rows.append({"id": did, "stratum": s["stratum"], "weight": float(s["weight"]), "choice": choice["name"],
                         "prob": choice["probability"] or 0.0, "right": bool(right), "label": kind,
                         "labelled_victim": wanted or None, "candidates": [c["name"] for c in choice["candidates"]]})
    finally:
        judge.close()

    chosen, curve = replacement_threshold(rows, args.precision, args.min_fills)
    boot = replacement_bootstrap(rows, args.precision, args.min_fills)
    named = [r for r in rows if r["label"] == "named"]
    result = {
        **vf.calibration_identity(), "settled_at": vf.SETTLED_AT,
        "calibrated_on_database": Path(args.db).name,
        "cutoff": chosen["threshold"] if chosen else None, "precision_target": args.precision,
        "min_fills": args.min_fills, "at_cutoff": chosen, "bootstrap_out_of_sample_precision": boot,
        "labelled_used": len(rows), "labelled_named": len(named), "excluded": excluded,
        "recall_labelled_named": (sum(1 for r in named if r["choice"] and r["prob"] >= chosen["threshold"]) / len(named)
                                  if chosen and named else None),
        "labels_file": str(vf.LABELS_PATH.relative_to(vf.ROOT)), "sample_file": str(SAMPLE_PATH.relative_to(vf.ROOT)),
        "generated": datetime.now().isoformat(), "curve": curve, "rows": rows,
    }
    result["labelled_rows"] = {"fill": deployment.standard_rows(rows, "right", p_key="prob", choice_key="choice")}
    vf.CALIBRATION_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    after_calibration("victim-fill")
    if chosen:
        print(f"Cut-off {chosen['threshold']:.4f}: fills {chosen['replaced_labelled']} labelled events, "
              f"{chosen['correct_labelled']} with the labelled victim; weighted precision "
              f"{chosen['weighted_precision']:.3f}")
    else:
        print(f"No cut-off reaches precision {args.precision}: fill-victims stays report-only.")
    print(f"Bootstrap out-of-sample precision: {boot}")
    print(f"Used {len(rows)} labelled events ({len(named)} named); excluded {excluded}. Written {vf.CALIBRATION_PATH}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="instance/cyber_events.db")
    add_model_arguments(parser)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sample")
    p.add_argument("--seed", type=int, default=20261029)
    p.set_defaults(func=cmd_sample)
    p = sub.add_parser("packets")
    p.add_argument("--out", required=True)
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(func=cmd_packets)
    p = sub.add_parser("calibrate")
    p.add_argument("--precision", type=float, default=0.95)
    p.add_argument("--min-fills", type=int, default=10)
    p.set_defaults(func=cmd_calibrate)
    args = parser.parse_args(argv)
    apply_model_arguments(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
