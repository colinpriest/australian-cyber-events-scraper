"""Draw the labelling sample for the AI-involvement flag, and calibrate its cut-off.

Three steps, each reproducible from committed files:

``sample``     Writes ``evaluation/ai_involvement_sample.csv``, stratified on
               Jev's stored P(AI) = 1 - P(No-AI) for the current question
               version. Labellers never see the probabilities.
               * ``A`` - every event with P(AI) >= ``--census-above`` (0.02):
                 a census, weight 1; any cut-off lies here.
               * ``K`` - a seeded sample of the rest whose evidence mentions AI
                 (``AI_TERMS``): where a missed AI incident would hide.
               * ``C`` - a seeded sample of the rest.
               K and C are weighted up to their strata and measure recall.

``packet``     Writes one JSON file per sampled event into ``--out``: the
               evidence Jev was given (no probabilities), for blind labelling.

``calibrate``  Reads ``evaluation/ai_involvement_labels.csv``
               (``deduplicated_event_id,label,rationale,labeller``; label
               ``ai_attacker``, ``ai_vulnerability``, ``ai_misuse``, ``no_ai`` or
               ``unsure``)
               and writes ``evaluation/ai_involvement_calibration.json``. An
               event is flagged as AI when P(AI) >= cut-off; the cut-off is the
               smallest at which the flagged labelled events are, weighted by
               stratum, at least ``--precision`` (0.95) labelled AI. How often
               the flagged events' category (attacker / vulnerability / misuse) matches
               the label is reported beside it. The Jev model and question
               version are recorded; :mod:`ai_involvement` applies the cut-off
               only while both match.

Usage (from the project root):
    python scripts/calibrate_ai_involvement.py sample
    python scripts/calibrate_ai_involvement.py packet --out evaluation/ai_packets
    python scripts/calibrate_ai_involvement.py calibrate --precision 0.95
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.processing import ai_involvement as ai  # noqa: E402
from cyber_data_collector.processing.breach_source import event_state  # noqa: E402
from cyber_data_collector.utils.llm_models import add_model_arguments, apply_model_arguments  # noqa: E402
from scripts.calibrate_event_reality import wilson  # noqa: E402

SAMPLE_PATH = ai.ROOT / "evaluation" / "ai_involvement_sample.csv"
AI_TERMS = re.compile(r"\bAI\b|artificial intelligence|\bLLMs?\b|chatbot|deepfake|AI agent|generative|"
                      r"machine learning|prompt injection|ChatGPT|OpenAI|\bGPT\b|Copilot|Gemini|Claude", re.I)


def _evidence_mentions_ai(state: Dict) -> bool:
    return bool(AI_TERMS.search(json.dumps(state)))


def cmd_sample(args) -> int:
    conn = sqlite3.connect(args.db)
    probs = ai.stored_probabilities(conn)
    unjudged = conn.execute("SELECT COUNT(*) FROM DeduplicatedEvents WHERE status = 'Active'").fetchone()[0] - len(probs)
    rest = sorted(d for d, p in probs.items() if p["ai"] < args.census_above)
    mentions = [d for d in rest if _evidence_mentions_ai(event_state(conn, d))]
    bands = {"A": sorted(d for d, p in probs.items() if p["ai"] >= args.census_above),
             "K": mentions, "C": sorted(set(rest) - set(mentions))}
    rng = random.Random(args.seed)
    chosen = {"A": bands["A"],
              "K": sorted(rng.sample(bands["K"], min(args.sample_mentions, len(bands["K"])))),
              "C": sorted(rng.sample(bands["C"], min(args.sample_rest, len(bands["C"]))))}
    SAMPLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SAMPLE_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["deduplicated_event_id", "stratum", "weight"])
        for stratum, ids in chosen.items():
            weight = len(bands[stratum]) / len(ids) if ids else 0.0
            w.writerows([(d, stratum, round(weight, 4)) for d in ids])
            print(f"Stratum {stratum}: {len(ids)} of {len(bands[stratum])} events (weight {weight:.2f})")
    print(f"Seed {args.seed}; {unjudged} active event(s) had no stored {ai.QUESTION_VERSION} judgement. "
          f"Written {SAMPLE_PATH}")
    return 0


def cmd_packet(args) -> int:
    conn = sqlite3.connect(args.db)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ids = [r["deduplicated_event_id"] for r in csv.DictReader(SAMPLE_PATH.open(encoding="utf-8"))]
    for did in ids:
        state = event_state(conn, did)
        date = conn.execute("SELECT event_date FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                            (did,)).fetchone()
        (out / f"{did}.json").write_text(json.dumps({"deduplicated_event_id": did,
                                                     "event_date": date[0] if date else None, **state},
                                                    indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Written {len(ids)} packet(s) to {out}")
    return 0


def choose_cutoff(rows: List[Dict], precision: float, min_flags: int) -> Tuple[Optional[Dict], List[Dict]]:
    """The precision curve and the smallest cut-off whose flags are >= ``precision`` AI (weighted).

    ``rows``: ``{"p", "weight", "is_ai", "category_right"}`` per labelled event.
    """
    candidates = sorted({round(r["p"], 6) for r in rows})
    w_all_ai = sum(r["weight"] for r in rows if r["is_ai"])
    table = []
    for c in candidates:
        flagged = [r for r in rows if r["p"] >= c]
        w_flag = sum(r["weight"] for r in flagged)
        w_right = sum(r["weight"] for r in flagged if r["is_ai"])
        n_right = sum(r["is_ai"] for r in flagged)
        lo, hi = wilson(n_right, len(flagged))
        ai_flagged = [r for r in flagged if r["is_ai"]]
        table.append({"cutoff": c, "flagged_labelled": len(flagged), "correct_labelled": n_right,
                      "weighted_precision": w_right / w_flag if w_flag else None,
                      "weighted_recall": w_right / w_all_ai if w_all_ai else None,
                      "unweighted_precision_ci95": [lo, hi],
                      "category_agreement": (sum(r["category_right"] for r in ai_flagged) / len(ai_flagged)
                                             if ai_flagged else None)})
    ok = [t for t in table if t["weighted_precision"] is not None
          and t["weighted_precision"] >= precision and t["flagged_labelled"] >= min_flags]
    return (min(ok, key=lambda t: t["cutoff"]) if ok else None), table


def bootstrap(rows: List[Dict], precision: float, min_flags: int, n: int = 1000, seed: int = 1) -> Dict:
    """Out-of-bag weighted precision of the whole choose-then-test procedure, resampling within strata."""
    rng = random.Random(seed)
    strata: Dict[str, List[Dict]] = {}
    for r in rows:
        strata.setdefault(r["stratum"], []).append(r)
    oob: List[float] = []
    for _ in range(n):
        boot = [rng.choice(rs) for rs in strata.values() for _ in rs]
        chosen, _ = choose_cutoff(boot, precision, min_flags)
        if chosen is None:
            continue
        ids = {id(r) for r in boot}
        out = [r for r in rows if id(r) not in ids and r["p"] >= chosen["cutoff"]]
        weight = sum(r["weight"] for r in out)
        if weight:
            oob.append(sum(r["weight"] for r in out if r["is_ai"]) / weight)
    oob.sort()
    if not oob:
        return {"replicates": 0, "median": None, "mean": None, "p05": None}
    return {"replicates": len(oob), "median": oob[len(oob) // 2], "mean": sum(oob) / len(oob),
            "p05": oob[int(0.05 * len(oob))]}


def cmd_calibrate(args) -> int:
    sample = {r["deduplicated_event_id"]: r for r in csv.DictReader(SAMPLE_PATH.open(encoding="utf-8"))}
    labels = ai.labelled_events()
    conn = sqlite3.connect(args.db)
    probs = ai.stored_probabilities(conn)
    rows, unsure, missing = [], 0, 0
    for did, s in sample.items():
        label = labels.get(did)
        if label not in ai.LABELS:
            unsure += label == "unsure"
            missing += label is None
            continue
        if did not in probs:
            missing += 1
            continue
        p = probs[did]
        rows.append({"id": did, "stratum": s["stratum"], "weight": float(s["weight"]), "p": p["ai"],
                     "is_ai": label != "no_ai", "category_right": p["category"] == label})
    chosen, table = choose_cutoff(rows, args.precision, args.min_flags)
    boot = bootstrap(rows, args.precision, args.min_flags)
    result = {**ai.calibration_identity(), "calibrated_on_database": Path(args.db).name,
              "cutoff": chosen["cutoff"] if chosen else None, "precision_target": args.precision,
              "min_flags": args.min_flags, "at_cutoff": chosen, "labelled_used": len(rows),
              "unsure_excluded": unsure, "missing": missing,
              "labels_file": str(ai.LABELS_PATH.relative_to(ai.ROOT)),
              "sample_file": str(SAMPLE_PATH.relative_to(ai.ROOT)), "generated": datetime.now().isoformat(),
              "bootstrap_out_of_sample_precision": boot, "curve": table}
    ai.CALIBRATION_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Bootstrap out-of-sample weighted precision: {boot}")
    if chosen:
        print(f"Cut-off {chosen['cutoff']:.4f}: flags {chosen['flagged_labelled']} labelled events, "
              f"{chosen['correct_labelled']} correctly; weighted precision {chosen['weighted_precision']:.3f}, "
              f"weighted recall {chosen['weighted_recall']:.3f}; category agreement "
              f"{chosen['category_agreement']:.3f}")
    else:
        print(f"No cut-off reaches precision {args.precision}: AI labels stay uncalibrated (most probable option).")
    print(f"Used {len(rows)} labelled events ({unsure} unsure excluded, {missing} missing). "
          f"Written {ai.CALIBRATION_PATH}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="instance/cyber_events.db")
    add_model_arguments(parser)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sample")
    p.add_argument("--census-above", type=float, default=0.02)
    p.add_argument("--sample-mentions", type=int, default=40)
    p.add_argument("--sample-rest", type=int, default=30)
    p.add_argument("--seed", type=int, default=20261028)
    p.set_defaults(func=cmd_sample)
    p = sub.add_parser("packet")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_packet)
    p = sub.add_parser("calibrate")
    p.add_argument("--precision", type=float, default=0.95)
    p.add_argument("--min-flags", type=int, default=10,
                   help="A cut-off must flag at least this many labelled events to count.")
    p.set_defaults(func=cmd_calibrate)
    args = parser.parse_args(argv)
    apply_model_arguments(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
