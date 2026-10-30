"""Draw the labelling sample for the event-scope filter, package it for blind labelling, and calibrate its cut-off.

Three steps, each reproducible from committed files. All three read Jev's and the escalation's
*stored* answers, so run ``dedup_admin.py check-scope`` (report only, no calibration needed) on the
database first; calibrate on the database as it was when labelled.

``sample``     Writes ``evaluation/event_scope_sample.csv``: active events stratified on the
               stored *final* P(in scope) (escalation in the middle band, else Jev). Labellers are
               never shown the probability; stratifying on it only decides who is labelled, and the
               weights undo it.
               * ``A`` - every event below ``--census-below`` (0.5): a census, weight 1, because
                 any cut-off lies here and its precision decides whether rejection is safe.
               * ``B`` - a seeded sample of 0.5-0.9; ``C`` - of 0.9 and above. B and C are weighted
                 up to their bands and measure what a cut-off misses (recall).

``packet``     Writes one JSON labelling packet per sampled event to ``--out`` (not committed):
               the event, every active record's page text and Perplexity research, and the victim's
               researched timeline - and **no score**. Events that already carry a label (scope
               labels, or event-reality ``real`` / ``out_of_scope``) are skipped unless
               ``--include-labelled``. Labellers work from the packet plus web search; the labels
               go in ``evaluation/event_scope_labels.csv``.

``calibrate``  Reads the labels (``deduplicated_event_id,label,rationale,labeller``; label
               ``in_scope``, ``out_of_scope`` or ``unsure``) and the stored scores, and writes
               ``evaluation/event_scope_calibration.json``. The cut-off is the largest at which the
               events it would reject are, weighted by stratum, at least ``--precision`` (0.95)
               labelled ``out_of_scope`` (and at least ``--min-rejections`` labelled events).
               Labels come from the scope file; where it is silent the event-reality label
               ``out_of_scope`` counts (``real`` means a real incident, not an Australian
               one, so it says nothing about scope), and such a labelled event that
               is not in the sample joins as stratum ``X`` with weight 1. ``unsure`` is excluded
               and counted. A stratified bootstrap of the choose-then-test procedure is the
               out-of-sample estimate. The models, question versions and evidence fingerprint
               are recorded; the pipeline rejects only while all match.

Usage (from the project root):
    python scripts/dedup_admin.py check-scope                      # scores every active event
    python scripts/calibrate_event_scope.py sample
    python scripts/calibrate_event_scope.py packet --out <dir>
    python scripts/calibrate_event_scope.py calibrate --precision 0.95
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
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.dedup import deployment  # noqa: E402
from scripts.evaluate_calibrations import after_calibration  # noqa: E402
from cyber_data_collector.dedup import event_reality as er  # noqa: E402
from cyber_data_collector.dedup import event_scope as es  # noqa: E402
from cyber_data_collector.utils.llm_models import add_model_arguments, apply_model_arguments  # noqa: E402
from scripts.calibrate_event_reality import bootstrap, choose_cutoff  # noqa: E402

SAMPLE_PATH = es.ROOT / "evaluation" / "event_scope_sample.csv"
EXTRA_STRATUM = "X"


def cmd_sample(args) -> int:
    conn = sqlite3.connect(args.db)
    active = er.active_events(conn)
    probs = es.stored_final_probabilities(conn, active)
    if not probs:
        print("No stored scope judgements: run 'python scripts/dedup_admin.py check-scope' first.",
              file=sys.stderr)
        return 2
    unjudged = len(active) - len(probs)
    bands = {
        "A": sorted(d for d, p in probs.items() if p < args.census_below),
        "B": sorted(d for d, p in probs.items() if args.census_below <= p < 0.9),
        "C": sorted(d for d, p in probs.items() if p >= 0.9),
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


def packet(conn: sqlite3.Connection, dedup_id: str) -> Dict[str, Any]:
    """Everything a labeller sees for one event - never a score."""
    row = conn.execute("SELECT description, summary, victim_organization_name, vendor_organization_name "
                       "FROM DeduplicatedEvents WHERE deduplicated_event_id = ?", (dedup_id,)).fetchone()
    state = es.build_state(conn, dedup_id)
    return {"deduplicated_event_id": dedup_id,
            "question": "Is this event within the scope of a database of cyber incidents that affected "
                        "Australia? Answer in_scope, out_of_scope or unsure.",
            "definition": es.INSTRUCTIONS, "event_description": row[0] if row else None,
            "event_summary": row[1] if row else None, "recorded_vendor": row[3] if row else None, **state}


def cmd_packet(args) -> int:
    conn = sqlite3.connect(args.db)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = list(csv.DictReader(SAMPLE_PATH.open(encoding="utf-8")))
    known = {} if args.include_labelled else es.labels(es.LABELS_PATH, er.LABELS_PATH, required=False)
    skipped = [r for r in rows if r["deduplicated_event_id"] in known]
    rows = [r for r in rows if r["deduplicated_event_id"] not in known]
    random.Random(args.seed).shuffle(rows)   # labellers see no stratum order either
    index = []
    for n, r in enumerate(rows, 1):
        name = f"packet_{n:03d}.json"
        (out / name).write_text(json.dumps(packet(conn, r["deduplicated_event_id"]), indent=1,
                                           ensure_ascii=False, default=str), encoding="utf-8")
        index.append({"packet": name, "deduplicated_event_id": r["deduplicated_event_id"]})
    (out / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    print(f"Wrote {len(index)} packets (no scores) to {out}; {len(skipped)} sampled event(s) already labelled")
    return 0


def calibration_cases(conn: sqlite3.Connection, sample_path: Path = SAMPLE_PATH,
                      known: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """The labelled events a calibration scores: sampled ones (their stratum and weight), then any
    other labelled event active in ``conn`` as stratum ``X`` (weight 1). ``label`` is None when unlabelled."""
    known = es.labels(es.LABELS_PATH, er.LABELS_PATH) if known is None else known
    with Path(sample_path).open(encoding="utf-8") as fh:
        sample = list(csv.DictReader(fh))
    cases = [{"id": r["deduplicated_event_id"], "stratum": r["stratum"], "weight": float(r["weight"]),
              "label": known.get(r["deduplicated_event_id"])} for r in sample]
    sampled = {c["id"] for c in cases}
    active = set(er.active_events(conn))
    cases += [{"id": d, "stratum": EXTRA_STRATUM, "weight": 1.0, "label": label}
              for d, label in sorted(known.items()) if d not in sampled and d in active]
    return cases


def cmd_calibrate(args) -> int:
    conn = sqlite3.connect(args.db)
    known = es.labels(es.LABELS_PATH, er.LABELS_PATH)
    cases = calibration_cases(conn, SAMPLE_PATH, known)
    # The final score the pipeline acts on: the escalation's in the middle band, else Jev's.
    probs = es.stored_final_probabilities(conn, [c["id"] for c in cases])

    rows = []
    unsure = missing = from_reality = 0
    for c in cases:
        if c["label"] not in ("in_scope", "out_of_scope"):
            unsure += c["label"] == "unsure"
            missing += c["label"] is None
            continue
        if c["id"] not in probs:
            missing += 1
            continue
        from_reality += c["stratum"] == EXTRA_STRATUM
        # "not_incident" is the key choose_cutoff reads: the class the cut-off rejects (here, out of scope).
        rows.append({"id": c["id"], "stratum": c["stratum"], "weight": c["weight"], "p": probs[c["id"]],
                     "not_incident": c["label"] == "out_of_scope"})

    chosen, table = choose_cutoff(rows, args.precision, args.min_rejections)
    boot = bootstrap(rows, args.precision, args.min_rejections)
    print(f"Bootstrap out-of-sample weighted precision: {boot}")
    result = {
        **es.calibration_identity(), "escalate_band": list(es.ESCALATE_BAND),
        "calibrated_on_database": Path(args.db).name,
        "cutoff": chosen["cutoff"] if chosen else None,
        "precision_target": args.precision, "min_rejections": args.min_rejections,
        "at_cutoff": chosen, "labelled_used": len(rows),
        "labelled_out_of_scope": sum(r["not_incident"] for r in rows),
        "extra_labelled_outside_sample": from_reality, "unsure_excluded": unsure, "missing": missing,
        "labels_file": str(es.LABELS_PATH.relative_to(es.ROOT)),
        "sample_file": str(SAMPLE_PATH.relative_to(es.ROOT)), "generated": datetime.now().isoformat(),
        "bootstrap_out_of_sample_precision": boot, "curve": table,
    }
    result["labelled_rows"] = {"reject": deployment.standard_rows(rows, "not_incident")}
    es.CALIBRATION_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    after_calibration("event-scope")
    if chosen:
        print(f"Cut-off {chosen['cutoff']:.4f}: rejects {chosen['rejected_labelled']} labelled events, "
              f"{chosen['correct_labelled']} correctly out of scope; weighted precision "
              f"{chosen['weighted_precision']:.3f}, weighted recall {chosen['weighted_recall']:.3f}, "
              f"unweighted precision 95% CI {chosen['unweighted_precision_ci95'][0]:.2f}-"
              f"{chosen['unweighted_precision_ci95'][1]:.2f}")
    else:
        print(f"No cut-off reaches precision {args.precision}: rejection stays report-only.")
    print(f"Used {len(rows)} labelled events ({result['labelled_out_of_scope']} out of scope, "
          f"{from_reality} outside the sample; {unsure} unsure excluded, {missing} unlabelled or unscored). "
          f"Written {es.CALIBRATION_PATH}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="instance/cyber_events.db")
    add_model_arguments(parser)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sample")
    p.add_argument("--census-below", type=float, default=0.5)
    p.add_argument("--sample-mid", type=int, default=40)
    p.add_argument("--sample-high", type=int, default=30)
    p.add_argument("--seed", type=int, default=20261029)
    p.set_defaults(func=cmd_sample)
    p = sub.add_parser("packet", aliases=["packets"])
    p.add_argument("--out", required=True)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--include-labelled", action="store_true",
                   help="Also packet sampled events that already carry a scope or event-reality label")
    p.set_defaults(func=cmd_packet)
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
