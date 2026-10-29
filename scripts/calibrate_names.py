"""Sample, package for blind labelling, and calibrate the proper-name judgement (``judge-names``).

``sample``     Writes ``evaluation/name_sample.csv``: names with a stored P(proper name), stratified on it.
               Bands ``A`` < 0.2, ``B`` 0.2-0.5, ``C`` 0.5-0.8, ``D`` >= 0.8, each sampled up to its quota
               (a census when smaller) and weighted back to its band. Run ``judge-names`` on the database first.

``packets``    Writes one JSON packet per sampled name to ``--out`` (not committed): the name and the sentence it
               came from - and **no score**. Labellers may search the web.

``calibrate``  Reads ``evaluation/name_labels.csv`` (``name,label,rationale,labeller``; label ``named`` - the
               name identifies one specific organisation - ``descriptive`` - it only describes a kind of
               organisation, a sector, a group or an unnamed one - or ``unsure``) and writes
               ``evaluation/name_calibration.json``: the **cut-off** is the largest P(proper name) below which,
               weighted by stratum, at least ``--precision`` of the names are labelled ``descriptive`` (at least
               ``--min-actions`` of them). ``unsure`` labels are excluded and counted. A stratified bootstrap of
               the choose-then-test procedure is the out-of-sample estimate. Without a calibration, only names
               labelled ``descriptive`` are treated as descriptions.

Usage (from the project root):
    python scripts/calibrate_names.py --db <database> sample
    python scripts/calibrate_names.py --db <database> packets --out <dir>
    python scripts/calibrate_names.py --db <database> calibrate
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
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.dedup import name_judgement as nj  # noqa: E402
from cyber_data_collector.utils.llm_models import add_model_arguments, apply_model_arguments  # noqa: E402
from scripts.calibrate_event_reality import bootstrap, choose_cutoff  # noqa: E402

SAMPLE_PATH = nj.ROOT / "evaluation" / "name_sample.csv"
BANDS = (("A", 0.0, 0.2), ("B", 0.2, 0.5), ("C", 0.5, 0.8), ("D", 0.8, 1.0001))
QUOTAS = {"A": 60, "B": 60, "C": 40, "D": 40}


def cmd_sample(args) -> int:
    conn = sqlite3.connect(args.db)
    probs = nj.stored_probabilities(conn)
    original = {n.lower(): n for n in nj.names_to_judge(conn)}
    spelled = {sid.lower(): sid for (sid,) in conn.execute(
        "SELECT DISTINCT subject_id FROM ModelJudgements WHERE question_id = ?", (nj.QUESTION_ID,))}
    names = sorted(probs)
    rng = random.Random(args.seed)
    with SAMPLE_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "stratum", "weight"])
        for name, lo, hi in BANDS:
            band = [n for n in names if lo <= probs[n] < hi]
            chosen = sorted(rng.sample(band, min(QUOTAS[name], len(band))))
            weight = len(band) / len(chosen) if chosen else 0.0
            w.writerows([(spelled.get(n, n), name, round(weight, 4)) for n in chosen])   # the name as it was written
            print(f"Stratum {name} ({lo}-{hi}): {len(chosen)} of {len(band)} names (weight {weight:.2f})")
    print(f"Seed {args.seed}; {len(original)} name(s) still have no verdict. Written {SAMPLE_PATH}")
    return 0


def cmd_packets(args) -> int:
    conn = sqlite3.connect(args.db)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = list(csv.DictReader(SAMPLE_PATH.open(encoding="utf-8")))
    random.Random(args.seed).shuffle(rows)
    index = []
    for n, r in enumerate(rows, 1):
        name = f"packet_{n:03d}.json"
        (out / name).write_text(json.dumps({"name": r["name"], "sentence": nj.sentence_for(conn, r["name"])},
                                           indent=1, ensure_ascii=False), encoding="utf-8")
        index.append({"packet": name, "name": r["name"]})
    (out / "index.json").write_text(json.dumps(index, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(index)} packets (no scores) to {out}")
    return 0


def cmd_calibrate(args) -> int:
    sample = {r["name"].lower(): r for r in csv.DictReader(SAMPLE_PATH.open(encoding="utf-8"))}
    labels = nj.load_labels()
    if not labels:
        raise SystemExit(f"{nj.LABELS_PATH} is missing, unreadable or empty; refusing to calibrate")
    probs = nj.stored_probabilities(sqlite3.connect(args.db))
    rows, excluded = [], {"unsure": 0, "missing": 0}
    for key, s in sample.items():
        label = (labels.get(key) or {}).get("label", "").strip()
        if label == "unsure":
            excluded["unsure"] += 1
            continue
        if label not in ("named", "descriptive") or key not in probs:
            excluded["missing"] += 1
            continue
        rows.append({"id": key, "stratum": s["stratum"], "weight": float(s["weight"]), "p": probs[key],
                     "not_incident": label == "descriptive"})
    chosen, curve = choose_cutoff(rows, args.precision, args.min_actions)
    boot = bootstrap(rows, args.precision, args.min_actions)
    result = {**nj.calibration_identity(), "calibrated_on_database": Path(args.db).name,
              "cutoff": chosen["cutoff"] if chosen else None, "precision_target": args.precision,
              "min_actions": args.min_actions, "at_cutoff": chosen, "bootstrap_out_of_sample_precision": boot,
              "labelled_used": len(rows), "excluded": excluded,
              "labels_file": str(nj.LABELS_PATH.relative_to(nj.ROOT)), "sample_file": str(SAMPLE_PATH.relative_to(nj.ROOT)),
              "generated": datetime.now().isoformat(), "curve": curve}
    nj.CALIBRATION_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if chosen:
        print(f"Cut-off {chosen['cutoff']:.4f}: treats {chosen['rejected_labelled']} labelled names as descriptions, "
              f"{chosen['correct_labelled']} truly; weighted precision {chosen['weighted_precision']:.3f}")
    else:
        print(f"No cut-off reaches precision {args.precision}: only names labelled descriptive are descriptions.")
    print(f"Bootstrap out-of-sample precision: {boot}")
    print(f"Used {len(rows)} labelled names; excluded {excluded}. Written {nj.CALIBRATION_PATH}")
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
    p.add_argument("--min-actions", type=int, default=10)
    p.set_defaults(func=cmd_calibrate)
    args = parser.parse_args(argv)
    apply_model_arguments(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
