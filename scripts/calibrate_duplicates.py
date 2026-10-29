"""Calibrate the merge cut-off for Jev's same-incident judgement.

Every pair of active events sharing a victim is a candidate, and every
candidate is labelled (``evaluation/duplicate_labels.csv``:
``pair_id,label,rationale,corroboration,labeller`` with label ``same``,
``different`` or ``unsure``), so there is no sampling weight: this is a census.

The cut-off is the lowest P(same) at which the pairs it would merge are at
least ``--precision`` (0.95) labelled ``same``, with at least
``--min-merges`` labelled merges. Choosing the cut-off on the labels it is
scored on flatters it, so a stratified bootstrap of the whole
choose-then-test procedure is reported alongside as the out-of-sample
estimate. The result, with the Jev model and question version, goes to
``evaluation/duplicate_calibration.json``; the pipeline merges only while
both still match.

Calibrate on the database **as it was when the pairs were labelled**: after
merging, labelled ``same`` pairs no longer exist as pairs. The file name of
that snapshot is recorded in the output.

Usage (from the project root):
    python scripts/calibrate_duplicates.py --db <labelling-time snapshot> --precision 0.95
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

from cyber_data_collector.dedup import duplicate_judgement as dj  # noqa: E402
from cyber_data_collector.utils.llm_models import (  # noqa: E402
    add_model_arguments, apply_model_arguments,
)
from scripts.calibrate_event_reality import choose_cutoff  # noqa: E402
from scripts.calibrate_supplier_victims import different_bound  # noqa: E402


def merge_cutoff(rows: List[Dict], precision: float, min_merges: int):
    """The lowest P(same) whose merges meet ``precision``; and the curve.

    The reality filter's rule (reject below a cut-off, precision on the
    rejected side) applied to 1 - P(same), so the two calibrations cannot
    drift apart in method.
    """
    mirrored = [{"p": 1.0 - r["p"], "weight": 1.0, "not_incident": r["same"]} for r in rows]
    chosen, table = choose_cutoff(mirrored, precision, min_merges)
    curve = [{"cutoff": round(1.0 - t["cutoff"], 6), "merged_labelled": t["rejected_labelled"],
              "correct_labelled": t["correct_labelled"], "precision": t["weighted_precision"],
              "recall": t["weighted_recall"], "precision_ci95": t["unweighted_precision_ci95"]}
             for t in table]
    if chosen is None:
        return None, curve
    return next(c for c in curve if c["merged_labelled"] == chosen["rejected_labelled"]
                and c["correct_labelled"] == chosen["correct_labelled"]), curve


def bootstrap(rows: List[Dict], precision: float, min_merges: int, n: int = 1000,
              seed: int = 1) -> Dict[str, Optional[float]]:
    """Out-of-bag precision of the whole choose-then-test procedure."""
    rng = random.Random(seed)
    oob: List[float] = []
    for _ in range(n):
        boot = [rng.choice(rows) for _ in rows]
        chosen, _ = merge_cutoff(boot, precision, min_merges)
        if chosen is None:
            continue
        ids = {id(r) for r in boot}
        out = [r for r in rows if id(r) not in ids and r["p"] >= chosen["cutoff"]]
        if out:
            oob.append(sum(r["same"] for r in out) / len(out))
    oob.sort()
    if not oob:
        return {"replicates": 0, "median": None, "mean": None, "p05": None}
    return {"replicates": len(oob), "median": oob[len(oob) // 2],
            "mean": sum(oob) / len(oob), "p05": oob[int(0.05 * len(oob))]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="instance/cyber_events.db")
    parser.add_argument("--precision", type=float, default=0.95)
    parser.add_argument("--min-merges", type=int, default=5)
    add_model_arguments(parser)
    args = parser.parse_args(argv)
    apply_model_arguments(args)

    labels = dj.labelled_pairs()
    # The final score the pipeline acts on (the escalation's in the middle band, else Jev's), as decided on each
    # pair's current evidence: a pair whose evidence changed since it was scored has none (score it again first).
    conn = sqlite3.connect(args.db)
    probs = dj.stored_final_probabilities(conn, [p for p in dj.candidate_pairs(conn) if p["pair_id"] in labels])
    rows = [{"pair_id": pid, "p": probs[pid], "same": label == "same"}
            for pid, label in labels.items() if label in ("same", "different") and pid in probs]
    unsure = sum(1 for label in labels.values() if label == "unsure")
    missing = sum(1 for pid, label in labels.items() if label in ("same", "different") and pid not in probs)

    chosen, curve = merge_cutoff(rows, args.precision, args.min_merges)
    boot = bootstrap(rows, args.precision, args.min_merges)
    # The largest score below which the labelled pairs are, at least 95%, labelled different and none is
    # labelled same: a calibrated *negative*, so that a chain A=B, B=C is not merged when A and C score
    # below it (duplicate_judgement.partition_without_conflicts). Same procedure as the supplier pairs.
    bound = different_bound([{**r, "positive": r["same"], "weight": 1.0, "stratum": "census"} for r in rows],
                            args.precision, args.min_merges)
    result = {**dj.calibration_identity(), **bound, "escalate_band": list(dj.ESCALATE_BAND),
              "calibrated_on_database": Path(args.db).name,
              "cutoff": chosen["cutoff"] if chosen else None, "precision_target": args.precision,
              "min_merges": args.min_merges, "at_cutoff": chosen, "bootstrap_out_of_sample_precision": boot,
              "labelled_used": len(rows), "labelled_same": sum(r["same"] for r in rows),
              "unsure_excluded": unsure, "missing": missing, "generated": datetime.now().isoformat(),
              "curve": curve}
    dj.CALIBRATION_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if chosen:
        print(f"Cut-off {chosen['cutoff']:.4f}: merges {chosen['merged_labelled']} labelled pairs, "
              f"{chosen['correct_labelled']} truly the same; precision {chosen['precision']:.3f}, "
              f"recall {chosen['recall']:.3f}, 95% CI {chosen['precision_ci95'][0]:.2f}-"
              f"{chosen['precision_ci95'][1]:.2f}")
    else:
        print(f"No cut-off reaches precision {args.precision}: merging stays report-only.")
    print(f"Bootstrap out-of-sample precision: {boot}")
    print(f"'Different' bound: {bound['different_cutoff']} (a pair scoring below it is, in the labels, different)")
    print(f"Used {len(rows)} labelled pairs ({result['labelled_same']} same; {unsure} unsure excluded, "
          f"{missing} missing). Written {dj.CALIBRATION_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
