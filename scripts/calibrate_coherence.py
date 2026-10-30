"""Draw the labelling sample for the stray-record (coherence) check, and calibrate its split cut-off.

Steps, each reproducible from committed files:

``sample``     Writes ``evaluation/coherence_sample.csv`` - (event, member)
               pairs drawn from active multi-member events, without looking at
               any score:
               * Stratum ``S`` - every *suspect* member (one naming another
                 organisation than the event's victim, vendor or majority): a
                 census, weight 1. Only suspects can be split by the model, so
                 any cut-off acts here and is calibrated here.
               * Stratum ``F`` - a seeded sample of non-suspect members dated
                 more than ``--far-days`` from their event; ``N`` - a seeded
                 sample of the other non-suspects. Weighted up to their
                 populations, they measure the strays the suspect gate keeps
                 (reported, never acted on).
``packet``     Writes a blind labelling packet per sampled pair (JSON lines):
               the event, all its records and the record under review, with
               no score and no stratum.
``judge``      Asks Jev (and the escalation model in its band) about every
               sampled pair. Answers are stored; re-running reuses them.
``calibrate``  Reads ``evaluation/coherence_labels.csv``
               (``deduplicated_event_id,enriched_event_id,label,rationale,
               evidence,labeller`` with label ``same``, ``different`` or
               ``unsure``) and the stored final scores, and writes
               ``evaluation/coherence_calibration.json``: the largest cut-off
               at which the stratum-S members it would split are at least
               ``--precision`` (0.95) labelled ``different``, the bootstrap
               out-of-sample precision of that whole choose-then-test
               procedure, and the identity (models + question versions) the
               pipeline checks before splitting.

Labels describe the database when they were made, so calibrate on that
snapshot (``--db``); its file name is recorded.

Usage (from the project root):
    python scripts/calibrate_coherence.py --db <snapshot> sample
    python scripts/calibrate_coherence.py --db <snapshot> packet --out <dir>
    python scripts/calibrate_coherence.py --db <snapshot> judge
    python scripts/calibrate_coherence.py --db <snapshot> calibrate --precision 0.95
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.dedup import deployment  # noqa: E402
from scripts.evaluate_calibrations import after_calibration  # noqa: E402
try:  # the project's .env, explicitly (cwd-independent)
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # pragma: no cover
    pass

from cyber_data_collector.dedup import coherence as co  # noqa: E402
from cyber_data_collector.dedup.entity_resolution import EntityResolver  # noqa: E402
from cyber_data_collector.utils.llm_models import (  # noqa: E402
    add_model_arguments, apply_model_arguments,
)
from scripts.calibrate_event_reality import choose_cutoff, wilson  # noqa: E402

SAMPLE_PATH = co.ROOT / "evaluation" / "coherence_sample.csv"


def _days_apart(a: Optional[str], b: Optional[str]) -> Optional[int]:
    try:
        return abs((date.fromisoformat(str(a)[:10]) - date.fromisoformat(str(b)[:10])).days)
    except (TypeError, ValueError):
        return None


def _events(conn: sqlite3.Connection) -> Dict[str, Dict]:
    """Every active multi-member event, with its suspects marked."""
    rows = co._multi_member_events(conn)
    members = {r[0]: co.load_event_members(conn, r[0]) for r in rows}
    resolver = co.fitted_resolver(conn, EntityResolver(conn), rows, members)
    out = {}
    for did, title, edate, victim, vendor in rows:
        suspects = co.suspect_members(resolver, members[did], victim, vendor)
        out[did] = {"id": did, "title": title, "date": edate, "victim": victim, "vendor": vendor,
                    "members": members[did], "suspect_ids": [m["id"] for m in suspects]}
    return out


def cmd_sample(args) -> int:
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    events = _events(conn)
    bands: Dict[str, List[tuple]] = {"S": [], "F": [], "N": []}
    for did, e in events.items():
        for m in e["members"]:
            if m["id"] in e["suspect_ids"]:
                bands["S"].append((did, m["id"]))
            else:
                gap = _days_apart(m["date"], e["date"])
                bands["F" if gap is not None and gap > args.far_days else "N"].append((did, m["id"]))
    rng = random.Random(args.seed)
    chosen = {"S": sorted(bands["S"]),
              "F": sorted(rng.sample(sorted(bands["F"]), min(args.sample_far, len(bands["F"])))),
              "N": sorted(rng.sample(sorted(bands["N"]), min(args.sample_near, len(bands["N"]))))}
    SAMPLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SAMPLE_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["deduplicated_event_id", "enriched_event_id", "stratum", "weight"])
        for stratum, pairs in chosen.items():
            weight = len(bands[stratum]) / len(pairs) if pairs else 0.0
            w.writerows([(d, m, stratum, round(weight, 4)) for d, m in pairs])
            print(f"Stratum {stratum}: {len(pairs)} of {len(bands[stratum])} members (weight {weight:.2f})")
    print(f"Seed {args.seed}; snapshot {Path(args.db).name}. Written {SAMPLE_PATH}")
    return 0


def _sample() -> List[Dict[str, str]]:
    with SAMPLE_PATH.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def cmd_packet(args) -> int:
    """Blind packets: what a labeller needs, and nothing about any score or stratum."""
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    events = _events(conn)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = sorted(_sample(), key=lambda r: (r["deduplicated_event_id"], r["enriched_event_id"]))
    chunks = [rows[i::args.parts] for i in range(args.parts)] if args.parts > 1 else [rows]
    for n, chunk in enumerate(chunks, 1):
        path = out_dir / f"coherence_packet_{n}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for r in chunk:
                event = events[r["deduplicated_event_id"]]
                state = co.build_member_state(conn, event, r["enriched_event_id"])
                state["other_records"] = [
                    {"enriched_event_id": m["id"], "title": m["title"], "date": m["date"],
                     "organisation": m["org"], "url": m["url"], "summary": (m["body"] or "")[:400]}
                    for m in event["members"] if m["id"] != r["enriched_event_id"]]
                fh.write(json.dumps({"deduplicated_event_id": r["deduplicated_event_id"],
                                     "enriched_event_id": r["enriched_event_id"], **state},
                                    ensure_ascii=False, default=str) + "\n")
        print(f"Written {len(chunk)} packet item(s) to {path}")
    return 0


def cmd_judge(args) -> int:
    conn = sqlite3.connect(args.db)
    events = _events(conn)
    items = [(events[r["deduplicated_event_id"]], r["enriched_event_id"]) for r in _sample()
             if r["deduplicated_event_id"] in events and (not args.stratum or r["stratum"] in args.stratum)]
    scores = co.judge_members(conn, items)
    print(f"Judged {len(scores)} sampled member(s); "
          f"{sum(1 for s in scores.values() if s['p_escalated'] is not None)} escalated")
    return 0


def bootstrap(rows: List[Dict], precision: float, min_splits: int, n: int = 1000,
              seed: int = 1) -> Dict[str, Optional[float]]:
    """Out-of-bag precision of the whole choose-then-test procedure (rows from one census stratum)."""
    rng = random.Random(seed)
    oob: List[float] = []
    for _ in range(n):
        boot = [rng.choice(rows) for _ in rows]
        chosen, _ = choose_cutoff(boot, precision, min_splits)
        if chosen is None:
            continue
        ids = {id(r) for r in boot}
        out = [r for r in rows if id(r) not in ids and r["p"] < chosen["cutoff"]]
        if out:
            oob.append(sum(r["not_incident"] for r in out) / len(out))
    oob.sort()
    if not oob:
        return {"replicates": 0, "median": None, "mean": None, "p05": None}
    return {"replicates": len(oob), "median": oob[len(oob) // 2], "mean": sum(oob) / len(oob),
            "p05": oob[int(0.05 * len(oob))]}


def cmd_calibrate(args) -> int:
    sample = {co.member_subject_id(r["deduplicated_event_id"], r["enriched_event_id"]): r for r in _sample()}
    labels = co.load_labels()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    events = _events(conn)
    probs = co.stored_final_probabilities(
        conn, [(events[r["deduplicated_event_id"]], r["enriched_event_id"]) for r in sample.values()
               if r["deduplicated_event_id"] in events and r["enriched_event_id"] in
               {m["id"] for m in events[r["deduplicated_event_id"]]["members"]}])

    rows, gate = [], []
    unsure = missing = 0
    for sid, s in sample.items():
        label = labels.get(sid)
        if label not in ("same", "different"):
            unsure += label == "unsure"
            missing += label is None
            continue
        row = {"id": sid, "stratum": s["stratum"], "weight": float(s["weight"]), "p": probs.get(sid),
               "not_incident": label == "different"}
        if s["stratum"] == "S":
            if row["p"] is None:
                missing += 1
                continue
            rows.append(row)
        else:
            gate.append(row)

    chosen, table = choose_cutoff(rows, args.precision, args.min_splits)
    boot = bootstrap(rows, args.precision, args.min_splits)

    # What the suspect gate keeps: labelled strays among non-suspects, weighted to their strata.
    w_all = sum(r["weight"] for r in gate)
    w_diff = sum(r["weight"] for r in gate if r["not_incident"])
    n_diff = sum(r["not_incident"] for r in gate)
    lo, hi = wilson(n_diff, len(gate))
    gate_summary = {"labelled": len(gate), "labelled_different": n_diff,
                    "weighted_share_different": (w_diff / w_all) if w_all else None,
                    "unweighted_share_ci95": [lo, hi],
                    "different_ids": sorted(r["id"] for r in gate if r["not_incident"])}

    result = {
        **co.calibration_identity(), "escalate_band": list(co.ESCALATE_BAND),
        "calibrated_on_database": Path(args.db).name,
        "cutoff": chosen["cutoff"] if chosen else None,
        "precision_target": args.precision, "min_splits": args.min_splits,
        "at_cutoff": chosen, "labelled_used": len(rows),
        "labelled_different": sum(r["not_incident"] for r in rows),
        "unsure_excluded": unsure, "missing": missing,
        "labels_file": co.LABELS_PATH.relative_to(co.ROOT).as_posix(),
        "sample_file": SAMPLE_PATH.relative_to(co.ROOT).as_posix(), "generated": datetime.now().isoformat(),
        "bootstrap_out_of_sample_precision": boot,
        "suspect_gate_misses": gate_summary,
        "curve": [{"cutoff": t["cutoff"], "split_labelled": t["rejected_labelled"],
                   "correct_labelled": t["correct_labelled"], "precision": t["weighted_precision"],
                   "recall": t["weighted_recall"], "precision_ci95": t["unweighted_precision_ci95"]}
                  for t in table],
    }
    result["labelled_rows"] = {"split": deployment.standard_rows(rows, "not_incident")}
    co.CALIBRATION_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    after_calibration("coherence")
    if chosen:
        print(f"Cut-off {chosen['cutoff']:.4f}: splits {chosen['rejected_labelled']} labelled suspects, "
              f"{chosen['correct_labelled']} truly strays; precision {chosen['weighted_precision']:.3f}, "
              f"recall {chosen['weighted_recall']:.3f}, 95% CI "
              f"{chosen['unweighted_precision_ci95'][0]:.2f}-{chosen['unweighted_precision_ci95'][1]:.2f}")
    else:
        print(f"No cut-off reaches precision {args.precision}: model-judged splits stay report-only.")
    print(f"Bootstrap out-of-sample precision: {boot}")
    print(f"Suspect gate: {n_diff} of {len(gate)} labelled non-suspects are strays "
          f"(weighted share {gate_summary['weighted_share_different']})")
    print(f"Used {len(rows)} labelled suspects ({result['labelled_different']} different; {unsure} unsure "
          f"excluded, {missing} missing). Written {co.CALIBRATION_PATH}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="instance/cyber_events.db")
    add_model_arguments(parser)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sample")
    p.add_argument("--far-days", type=int, default=180)
    p.add_argument("--sample-far", type=int, default=28)
    p.add_argument("--sample-near", type=int, default=28)
    p.add_argument("--seed", type=int, default=20261027)
    p.set_defaults(func=cmd_sample)
    p = sub.add_parser("packet")
    p.add_argument("--out", required=True)
    p.add_argument("--parts", type=int, default=1, help="Split the packet into this many files.")
    p.set_defaults(func=cmd_packet)
    p = sub.add_parser("judge")
    p.add_argument("--stratum", action="append", default=None,
                   help="Only these strata (S, F, N); repeatable. Calibration needs S.")
    p.set_defaults(func=cmd_judge)
    p = sub.add_parser("calibrate")
    p.add_argument("--precision", type=float, default=0.95)
    p.add_argument("--min-splits", type=int, default=5,
                   help="A cut-off must split at least this many labelled suspects to count.")
    p.set_defaults(func=cmd_calibrate)
    args = parser.parse_args(argv)
    apply_model_arguments(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
