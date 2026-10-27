"""Draw the labelling sample for the event-reality filter, and calibrate its cut-off.

Two steps, each reproducible from committed files:

``sample``     Writes ``evaluation/event_reality_sample.csv``: a sample of
               active events stratified on Jev's stored *screen* P(real)
               (``event-reality-v1``, stored evidence only). Labellers are
               never shown the probability; stratifying on it only decides who
               is labelled, and the weights undo it.
               * Stratum ``A`` - every event below ``--census-below`` (0.5): a
                 census, weight 1, because any cut-off lies here and its
                 precision is what decides whether rejection is safe.
               * Stratum ``B`` - a seeded sample of the 0.5-0.8 band.
               * Stratum ``C`` - a seeded sample of 0.8 and above.
               B and C are weighted up to their bands and measure what a
               cut-off misses (recall).

``calibrate``  Reads the labels (``evaluation/event_reality_labels.csv``:
               ``deduplicated_event_id,label,rationale,labeller`` with label
               ``real``, ``not_incident`` or ``unsure``) and Jev's stored
               probabilities, and writes ``evaluation/event_reality_calibration.json``.
               The cut-off is the largest one at which the events it would
               reject are, weighted by stratum, at least ``--precision`` (0.95)
               labelled ``not_incident``. ``unsure`` labels are excluded and
               counted. The model and question version are recorded; the
               pipeline only rejects while both match.

Usage (from the project root):
    python scripts/calibrate_event_reality.py sample
    python scripts/calibrate_event_reality.py calibrate --precision 0.95
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.dedup import event_reality as er  # noqa: E402
from cyber_data_collector.utils.llm_models import (  # noqa: E402
    add_model_arguments, apply_model_arguments, typesafe_model,
)

SAMPLE_PATH = er.ROOT / "evaluation" / "event_reality_sample.csv"


def wilson(successes: float, n: float, z: float = 1.96) -> Tuple[Optional[float], Optional[float]]:
    if n <= 0:
        return None, None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def cmd_sample(args) -> int:
    conn = sqlite3.connect(args.db)
    active = er.active_events(conn)
    # Stratify on the screen (stored evidence only): it exists before any
    # corroboration, so the sample - and its weights - do not depend on the
    # question being calibrated.
    probs = _probabilities(conn, active, version=er.SCREEN_VERSION)
    unjudged = len(active) - len(probs)
    bands = {
        "A": sorted(d for d, p in probs.items() if p < args.census_below),
        "B": sorted(d for d, p in probs.items() if args.census_below <= p < 0.8),
        "C": sorted(d for d, p in probs.items() if p >= 0.8),
    }
    rng = random.Random(args.seed)
    chosen = {"A": bands["A"],
              "B": sorted(rng.sample(bands["B"], min(args.sample_mid, len(bands["B"])))),
              "C": sorted(rng.sample(bands["C"], min(args.sample_high, len(bands["C"]))))}
    SAMPLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SAMPLE_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["deduplicated_event_id", "stratum", "weight"])
        for stratum, ids in chosen.items():
            weight = len(bands[stratum]) / len(ids) if ids else 0.0
            w.writerows([(d, stratum, round(weight, 4)) for d in ids])
            print(f"Stratum {stratum}: {len(ids)} of {len(bands[stratum])} events (weight {weight:.2f})")
    print(f"Seed {args.seed}; {unjudged} active event(s) had no stored judgement. Written {SAMPLE_PATH}")
    return 0


def _probabilities(conn: sqlite3.Connection, ids: List[str],
                   version: str = er.QUESTION_VERSION) -> Dict[str, float]:
    """Latest stored Jev answer per event for a question version and the current model."""
    out: Dict[str, float] = {}
    for did in ids:
        row = conn.execute(
            """SELECT value FROM ModelJudgements
               WHERE subject_type = 'deduplicated_event' AND subject_id = ? AND question_id = ?
                 AND question_version = ? AND model_requested = ?
               ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (did, er.QUESTION_ID, version, typesafe_model())).fetchone()
        if row is not None:
            out[did] = float(row[0])
    return out


def choose_cutoff(rows: List[Dict], precision: float, min_rejections: int) -> Tuple[Optional[Dict], List[Dict]]:
    """The precision curve over candidate cut-offs, and the one chosen.

    ``rows``: ``{"p", "weight", "not_incident"}`` per labelled event. An event
    is rejected when ``p < cutoff``. The chosen cut-off is the largest whose
    rejections are, weighted, at least ``precision`` not-incidents and number
    at least ``min_rejections`` labelled events.
    """
    candidates = sorted({round(r["p"] + 1e-6, 6) for r in rows})   # just above each observed p
    w_all_not = sum(r["weight"] for r in rows if r["not_incident"])
    table = []
    for c in candidates:
        rejected = [r for r in rows if r["p"] < c]
        w_rej = sum(r["weight"] for r in rejected)
        w_right = sum(r["weight"] for r in rejected if r["not_incident"])
        n_right = sum(r["not_incident"] for r in rejected)
        lo, hi = wilson(n_right, len(rejected))
        table.append({"cutoff": c, "rejected_labelled": len(rejected), "correct_labelled": n_right,
                      "weighted_precision": w_right / w_rej if w_rej else None,
                      "weighted_recall": w_right / w_all_not if w_all_not else None,
                      "unweighted_precision_ci95": [lo, hi]})
    ok = [t for t in table if t["weighted_precision"] is not None
          and t["weighted_precision"] >= precision and t["rejected_labelled"] >= min_rejections]
    return (max(ok, key=lambda t: t["cutoff"]) if ok else None), table


def bootstrap(rows: List[Dict], precision: float, min_rejections: int, n: int = 1000,
              seed: int = 1) -> Dict[str, Optional[float]]:
    """Out-of-bag weighted precision of the whole choose-then-test procedure, resampling within strata."""
    rng = random.Random(seed)
    strata: Dict[str, List[Dict]] = {}
    for r in rows:
        strata.setdefault(r["stratum"], []).append(r)
    oob: List[float] = []
    for _ in range(n):
        boot = [rng.choice(rs) for rs in strata.values() for _ in rs]
        chosen, _ = choose_cutoff(boot, precision, min_rejections)
        if chosen is None:
            continue
        ids = {id(r) for r in boot}
        out = [r for r in rows if id(r) not in ids and r["p"] < chosen["cutoff"]]
        weight = sum(r["weight"] for r in out)
        if weight:
            oob.append(sum(r["weight"] for r in out if r["not_incident"]) / weight)
    oob.sort()
    if not oob:
        return {"replicates": 0, "median": None, "mean": None, "p05": None}
    return {"replicates": len(oob), "median": oob[len(oob) // 2], "mean": sum(oob) / len(oob),
            "p05": oob[int(0.05 * len(oob))]}


def cmd_calibrate(args) -> int:
    sample = {r["deduplicated_event_id"]: r for r in csv.DictReader(SAMPLE_PATH.open(encoding="utf-8"))}
    labels = {r["deduplicated_event_id"]: r for r in csv.DictReader(er.LABELS_PATH.open(encoding="utf-8"))}
    conn = sqlite3.connect(args.db)
    # The final score the pipeline acts on: the escalation's in the middle band, else Jev's.
    probs = er.stored_final_probabilities(conn, list(sample))

    rows = []
    unsure = missing = 0
    for did, s in sample.items():
        label = (labels.get(did) or {}).get("label")
        if label not in ("real", "not_incident"):
            unsure += label == "unsure"
            missing += label is None
            continue
        if did not in probs:
            missing += 1
            continue
        rows.append({"id": did, "stratum": s["stratum"], "weight": float(s["weight"]),
                     "p": probs[did], "not_incident": label == "not_incident"})

    chosen, table = choose_cutoff(rows, args.precision, args.min_rejections)
    boot = bootstrap(rows, args.precision, args.min_rejections)
    print(f"Bootstrap out-of-sample weighted precision: {boot}")
    result = {
        **er.calibration_identity(), "escalate_band": list(er.ESCALATE_BAND),
        "calibrated_on_database": Path(args.db).name,
        "cutoff": chosen["cutoff"] if chosen else None,
        "precision_target": args.precision, "min_rejections": args.min_rejections,
        "at_cutoff": chosen, "labelled_used": len(rows), "unsure_excluded": unsure,
        "missing": missing, "labels_file": str(er.LABELS_PATH.relative_to(er.ROOT)),
        "sample_file": str(SAMPLE_PATH.relative_to(er.ROOT)), "generated": datetime.now().isoformat(),
        "bootstrap_out_of_sample_precision": boot,
        "curve": table,
    }
    er.CALIBRATION_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if chosen:
        print(f"Cut-off {chosen['cutoff']:.4f}: rejects {chosen['rejected_labelled']} labelled events, "
              f"{chosen['correct_labelled']} correctly; weighted precision {chosen['weighted_precision']:.3f}, "
              f"weighted recall {chosen['weighted_recall']:.3f}, unweighted precision 95% CI "
              f"{chosen['unweighted_precision_ci95'][0]:.2f}-{chosen['unweighted_precision_ci95'][1]:.2f}")
    else:
        print(f"No cut-off reaches precision {args.precision}: rejection stays report-only.")
    print(f"Used {len(rows)} labelled events ({unsure} unsure excluded, {missing} missing). "
          f"Written {er.CALIBRATION_PATH}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="instance/cyber_events.db")
    add_model_arguments(parser)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sample")
    p.add_argument("--census-below", type=float, default=0.5)
    p.add_argument("--sample-mid", type=int, default=25)
    p.add_argument("--sample-high", type=int, default=25)
    p.add_argument("--seed", type=int, default=20261027)
    p.set_defaults(func=cmd_sample)
    p = sub.add_parser("calibrate")
    p.add_argument("--precision", type=float, default=0.95)
    p.add_argument("--min-rejections", type=int, default=10,
                   help="A cut-off must reject at least this many labelled events to count.")
    p.set_defaults(func=cmd_calibrate)
    args = parser.parse_args(argv)
    apply_model_arguments(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
