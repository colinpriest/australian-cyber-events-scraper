"""Draw the labelling sample for the Google source gate, and calibrate its cut-off.

``sample``     Writes ``evaluation/source_gate_sample.csv``: active Google-sourced
               records stratified on Jev's stored P(page reports an incident).
               Every record below ``--census-below`` (0.5) is labelled (any
               cut-off lies there); seeded samples of 0.5-0.8 and 0.8+ are
               weighted up to their bands.

``calibrate``  Reads ``evaluation/source_gate_labels.csv``
               (``enriched_event_id,label,rationale,labeller``; label
               ``reports_incident``, ``not_incident`` or ``unsure``) and writes
               ``evaluation/source_gate_calibration.json``: the largest cut-off
               at which the records it would remove are, weighted, at least
               ``--precision`` (0.95) labelled ``not_incident``, with a
               bootstrap out-of-sample estimate.

Usage (from the project root):
    python scripts/calibrate_source_gate.py sample
    python scripts/calibrate_source_gate.py calibrate --db <labelling-time snapshot>
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.dedup import deployment  # noqa: E402
from scripts.evaluate_calibrations import after_calibration  # noqa: E402
from cyber_data_collector.dedup import source_gate as sg  # noqa: E402
from cyber_data_collector.utils.llm_models import add_model_arguments, apply_model_arguments  # noqa: E402
from scripts.calibrate_event_reality import bootstrap, choose_cutoff  # noqa: E402


def cmd_sample(args) -> int:
    conn = sqlite3.connect(args.db)
    active = {r["enriched_event_id"] for r in sg.google_records(conn)}
    probs = {k: v for k, v in sg.stored_probabilities(conn).items() if k in active}
    bands = {"A": sorted(k for k, p in probs.items() if p < args.census_below),
             "B": sorted(k for k, p in probs.items() if args.census_below <= p < 0.8),
             "C": sorted(k for k, p in probs.items() if p >= 0.8)}
    rng = random.Random(args.seed)
    chosen = {"A": bands["A"] if len(bands["A"]) <= args.max_census else sorted(rng.sample(bands["A"], args.max_census)),
              "B": sorted(rng.sample(bands["B"], min(args.sample_mid, len(bands["B"])))),
              "C": sorted(rng.sample(bands["C"], min(args.sample_high, len(bands["C"]))))}
    with sg.SAMPLE_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["enriched_event_id", "stratum", "weight"])
        for stratum, ids in chosen.items():
            weight = len(bands[stratum]) / len(ids) if ids else 0.0
            w.writerows([(i, stratum, round(weight, 4)) for i in ids])
            print(f"Stratum {stratum}: {len(ids)} of {len(bands[stratum])} records (weight {weight:.2f})")
    print(f"{len(active) - len(probs)} active Google record(s) unjudged. Written {sg.SAMPLE_PATH}")
    return 0


def cmd_calibrate(args) -> int:
    sample = {r["enriched_event_id"]: r for r in csv.DictReader(sg.SAMPLE_PATH.open(encoding="utf-8"))}
    known = sg.labels()
    conn = sqlite3.connect(args.db)
    probs = sg.stored_probabilities(conn)
    # Calibrate on the records the gate can act on: pages with enough stored text.
    records = {r["enriched_event_id"]: r for r in sg.google_records(conn)}
    rows, unsure, missing, unjudgeable = [], 0, 0, 0
    for rid, s in sample.items():
        if rid in records and not sg.judgeable(records[rid]):
            unjudgeable += 1
            continue
        label = known.get(rid)
        if label not in ("reports_incident", "not_incident"):
            unsure += label == "unsure"
            missing += label is None
            continue
        if rid not in probs:
            missing += 1
            continue
        rows.append({"id": rid, "stratum": s["stratum"], "weight": float(s["weight"]), "p": probs[rid],
                     "not_incident": label == "not_incident"})
    chosen, table = choose_cutoff(rows, args.precision, args.min_rejections)
    boot = bootstrap(rows, args.precision, args.min_rejections)
    result = {**sg.calibration_identity(), "cutoff": chosen["cutoff"] if chosen else None,
              "precision_target": args.precision, "min_rejections": args.min_rejections, "at_cutoff": chosen,
              "bootstrap_out_of_sample_precision": boot, "labelled_used": len(rows), "unsure_excluded": unsure,
              "missing": missing, "unjudgeable_excluded": unjudgeable, "min_page_chars": sg.MIN_PAGE_CHARS,
              "calibrated_on_database": Path(args.db).name,
              "generated": datetime.now().isoformat(), "curve": table}
    result["labelled_rows"] = {"reject": deployment.standard_rows(rows, "not_incident")}
    sg.CALIBRATION_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    after_calibration("source-gate")
    if chosen:
        print(f"Cut-off {chosen['cutoff']:.4f}: removes {chosen['rejected_labelled']} labelled records, "
              f"{chosen['correct_labelled']} correctly; weighted precision {chosen['weighted_precision']:.3f}, "
              f"weighted recall {chosen['weighted_recall']:.3f}")
    else:
        print(f"No cut-off reaches precision {args.precision}: the gate stays report-only.")
    print(f"Bootstrap out-of-sample weighted precision: {boot}")
    print(f"Used {len(rows)} labelled records ({unsure} unsure, {missing} missing). Written {sg.CALIBRATION_PATH}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="instance/cyber_events.db")
    add_model_arguments(parser)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sample")
    p.add_argument("--census-below", type=float, default=0.5)
    p.add_argument("--max-census", type=int, default=120)
    p.add_argument("--sample-mid", type=int, default=25)
    p.add_argument("--sample-high", type=int, default=25)
    p.add_argument("--seed", type=int, default=20261027)
    p.set_defaults(func=cmd_sample)
    p = sub.add_parser("calibrate")
    p.add_argument("--precision", type=float, default=0.95)
    p.add_argument("--min-rejections", type=int, default=10)
    p.set_defaults(func=cmd_calibrate)
    args = parser.parse_args(argv)
    apply_model_arguments(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
