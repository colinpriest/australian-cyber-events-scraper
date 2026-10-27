"""Sample, label and calibrate the supplier-incident judgements (co-victims and same-incident merges).

Three steps, each reproducible from committed files:

``sample``     Writes ``evaluation/supplier_victim_sample.csv``: for each kind
               (``victim`` - is <org> affected by this supplier incident?;
               ``same_incident`` - is another event the same supplier incident?)
               a seeded sample of the current candidates stratified on Jev's
               stored score - which exists before any escalation, so the
               sample cannot depend on the final score being calibrated:
               ``low`` below 0.2, ``mid`` 0.2-0.8 (the escalation band),
               ``high`` 0.8 and above, each weighted up to its band.
               Calibration uses the *final* score (the escalation's inside the
               band, Jev's outside it); an undecided item (in band, no
               escalation) has none, is never acted on, and is counted as
               missing.

``packet``     Writes the labelling packet for the sample (JSON lines): the
               evidence each judgement saw, **without any score**, so labels
               are blind to the model being calibrated.

``calibrate``  Reads the labels (``evaluation/supplier_victim_labels.csv``:
               ``kind,subject_id,label,rationale,labeller``; victim labels
               ``affected``, ``not_affected``, ``already_recorded`` or
               ``unsure``; same_incident labels ``same``, ``different`` or
               ``unsure``) and writes ``evaluation/supplier_victim_calibration.json``.
               For each kind the cut-off is the lowest final score at which the
               items it would act on are, weighted by stratum, at least
               ``--precision`` (0.95) labelled positive, with at least
               ``--min-accepts`` labelled acceptances; a stratified bootstrap of
               the whole choose-then-test procedure gives the out-of-sample
               precision. Missing or unreadable labels fail loudly. The models
               and question versions are recorded; ``find-supplier-victims
               --apply`` refuses to act unless both still match.

Usage (from the project root):
    python scripts/calibrate_supplier_victims.py --db <db> sample
    python scripts/calibrate_supplier_victims.py --db <db> packet --out <dir>
    python scripts/calibrate_supplier_victims.py --db <db> calibrate --precision 0.95
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.dedup import supplier_victims as sv  # noqa: E402
from cyber_data_collector.utils.llm_models import (  # noqa: E402
    add_model_arguments, apply_model_arguments,
)
from scripts.calibrate_event_reality import bootstrap, choose_cutoff  # noqa: E402

SAMPLE_PATH = sv.ROOT / "evaluation" / "supplier_victim_sample.csv"
KINDS = (sv.KIND_VICTIM, sv.KIND_PAIR)


def stratum(p: float) -> str:
    lo, hi = sv.ESCALATE_BAND
    return "low" if p < lo else ("mid" if p < hi else "high")


def cmd_sample(args) -> int:
    conn = sqlite3.connect(args.db)
    rng = random.Random(args.seed)
    g = sv.gather(conn)
    current = {sv.KIND_VICTIM: {c.subject_id for cs in g.candidates.values() for c in cs},
               sv.KIND_PAIR: {p.pair_id for p in g.pairs}}
    rows: List[Tuple[str, str, str, float]] = []
    for kind in KINDS:
        # Stratified on Jev's own score, which exists before any escalation: the
        # sample (and its weights) cannot depend on the final score being calibrated.
        jev, _ = sv.stored_probabilities(conn, kind)
        probs = {sid: p for sid, p in jev.items() if sid in current[kind]}
        unjudged = len(current[kind] - set(probs))
        if unjudged:
            print(f"{kind}: {unjudged} current candidate(s) have no stored Jev score")
        bands: Dict[str, List[str]] = {"low": [], "mid": [], "high": []}
        for sid, p in probs.items():
            bands[stratum(p)].append(sid)
        want = {"mid": args.sample_mid, "high": args.sample_high, "low": args.sample_low}
        for name, ids in bands.items():
            ids = sorted(ids)
            chosen = sorted(rng.sample(ids, min(want[name], len(ids))))
            weight = len(ids) / len(chosen) if chosen else 0.0
            rows += [(kind, sid, name, round(weight, 4)) for sid in chosen]
            print(f"{kind:13} {name:5}: {len(chosen)} of {len(ids)} (weight {weight:.2f})")
    SAMPLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SAMPLE_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["kind", "subject_id", "stratum", "weight"])
        w.writerows(rows)
    print(f"Seed {args.seed}. Written {SAMPLE_PATH}")
    return 0


def read_sample(path: Path = SAMPLE_PATH) -> Dict[Tuple[str, str], Dict[str, str]]:
    try:
        with Path(path).open(encoding="utf-8", newline="") as fh:
            return {(r["kind"], r["subject_id"]): r for r in csv.DictReader(fh)}
    except OSError as exc:
        raise SystemExit(f"Sample file {path} cannot be read ({exc}); run the sample step first")


def _compact_event(state: dict) -> dict:
    """The evidence a labeller needs, without anything derived from the judgement."""
    event = dict(state.get("event", {}))
    records = [{"title": r.get("title"), "url": r.get("url"), "page_text": (r.get("page_text") or "")[:700],
                "perplexity_named": (r.get("perplexity") or {}).get("formal_entity_name"),
                "perplexity_reasoning": ((r.get("perplexity") or {}).get("reasoning") or "")[:400]}
               for r in state.get("records", [])]
    out = {"event": event, "records": records}
    if state.get("corroboration"):
        out["corroboration_search"] = state["corroboration"].get("answer")
    return out


def cmd_packet(args) -> int:
    sample = read_sample()
    conn = sqlite3.connect(args.db)
    g = sv.gather(conn)
    by_id = g.by_id
    flat = {c.subject_id: c for cs in g.candidates.values() for c in cs}
    pairs = {p.pair_id: p for p in g.pairs}

    items = []
    missing = []
    for (kind, sid) in sorted(sample):
        if kind == sv.KIND_VICTIM:
            cand = flat.get(sid)
            if cand is None:
                missing.append(sid)
                continue
            state = sv.build_victim_state(conn, by_id[cand.dedup_id], cand)
            inc = state["incident"]
            items.append({
                "kind": kind, "subject_id": sid,
                "question": "Was the CANDIDATE organisation's own data, systems or customers' data affected by "
                            "THIS incident (so it should be recorded as an additional victim)?",
                "candidate": state["candidate"],
                "incident": {**_compact_event({"event": {k: inc.get(k) for k in ("title", "date", "victim",
                                                                                 "record_count")},
                                               "records": inc["records"],
                                               "corroboration": state.get("corroboration")}),
                             "vendors": inc["vendors"], "recorded_victims": inc["recorded_victims"]},
                "supplier_search": (state.get("supplier_search") or {}).get("answer"),
            })
        else:
            pair = pairs.get(sid)
            if pair is None:
                missing.append(sid)
                continue
            state = sv.build_pair_state(conn, by_id[pair.supplier_id], pair)
            items.append({
                "kind": kind, "subject_id": sid,
                "question": "Is OTHER_EVENT a report of the same supplier/third-party incident as "
                            "SUPPLIER_INCIDENT (same compromise, possibly about another customer)?",
                "gap_days": state["gap_days"], "why_paired": state["why_paired"],
                "supplier_incident": {**_compact_event(state["supplier_incident"]),
                                      "vendors": state["supplier_incident"]["event"].get("vendors")},
                "other_event": _compact_event(state["other_event"]),
            })
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # Shuffle (seeded) so strata - which follow the score - are not visible in the order.
    rng = random.Random(args.seed)
    rng.shuffle(items)
    chunks = [items[i::args.chunks] for i in range(args.chunks)]
    for n, chunk in enumerate(chunks, 1):
        path = out / f"supplier_packet_{n}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for item in chunk:
                fh.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        print(f"Written {len(chunk)} item(s) to {path}")
    if missing:
        print(f"{len(missing)} sampled item(s) are no longer candidates (data changed since sampling): "
              + ", ".join(missing[:10]))
    return 0


def accept_cutoff(rows: List[Dict], precision: float, min_accepts: int):
    """The lowest final score whose acceptances meet ``precision`` (weighted); and the curve.

    The reality filter's rule (reject below a cut-off, weighted precision on
    the rejected side) applied to 1 - p, as calibrate_duplicates does, so the
    calibrations cannot drift apart in method.
    """
    mirrored = [{"p": 1.0 - r["p"], "weight": r["weight"], "not_incident": r["positive"],
                 "stratum": r["stratum"]} for r in rows]
    chosen, table = choose_cutoff(mirrored, precision, min_accepts)
    curve = [{"cutoff": round(1.0 - t["cutoff"], 6), "accepted_labelled": t["rejected_labelled"],
              "correct_labelled": t["correct_labelled"], "weighted_precision": t["weighted_precision"],
              "weighted_recall": t["weighted_recall"], "unweighted_precision_ci95": t["unweighted_precision_ci95"]}
             for t in table]
    if chosen is None:
        return None, curve, mirrored
    at = next(c for c in curve if c["accepted_labelled"] == chosen["rejected_labelled"]
              and c["correct_labelled"] == chosen["correct_labelled"])
    return at, curve, mirrored


def cmd_calibrate(args) -> int:
    sample = read_sample()
    try:
        labels = sv.read_labels(sv.LABELS_PATH)
    except sv.CalibrationError as exc:
        print(f"Cannot calibrate: {exc}")
        return 2
    conn = sqlite3.connect(args.db)
    result: Dict[str, object] = {**sv.calibration_identity(), "escalate_band": list(sv.ESCALATE_BAND),
                                 "calibrated_on_database": Path(args.db).name,
                                 "labels_file": str(sv.LABELS_PATH.relative_to(sv.ROOT)),
                                 "labels_sha256": hashlib.sha256(sv.LABELS_PATH.read_bytes()).hexdigest(),
                                 "sample_file": str(SAMPLE_PATH.relative_to(sv.ROOT)),
                                 "precision_target": args.precision, "min_accepts": args.min_accepts,
                                 "generated": datetime.now().isoformat()}
    for kind in KINDS:
        probs = sv.stored_final_probabilities(conn, kind)
        rows, unsure, missing, negatives = [], 0, 0, sv.NEGATIVE[kind]
        for (k, sid), s in sample.items():
            if k != kind:
                continue
            label = labels.get((kind, sid))
            if label is None or sid not in probs:
                missing += 1
                continue
            if label == "unsure":
                unsure += 1
                continue
            rows.append({"id": sid, "stratum": s["stratum"], "weight": float(s["weight"]), "p": probs[sid],
                         "positive": label == sv.POSITIVE[kind]})
            assert label == sv.POSITIVE[kind] or label in negatives
        chosen, curve, mirrored = accept_cutoff(rows, args.precision, args.min_accepts)
        boot = bootstrap(mirrored, args.precision, args.min_accepts)
        result[kind] = {"cutoff": chosen["cutoff"] if chosen else None, "at_cutoff": chosen,
                        "bootstrap_out_of_sample_precision": boot, "labelled_used": len(rows),
                        "labelled_positive": sum(r["positive"] for r in rows), "unsure_excluded": unsure,
                        "missing": missing, "curve": curve}
        if chosen:
            print(f"{kind}: cut-off {chosen['cutoff']:.4f} accepts {chosen['accepted_labelled']} labelled, "
                  f"{chosen['correct_labelled']} correct; weighted precision {chosen['weighted_precision']:.3f}, "
                  f"weighted recall {chosen['weighted_recall']:.3f}; bootstrap {boot}")
        else:
            print(f"{kind}: no cut-off reaches precision {args.precision} with {args.min_accepts}+ "
                  f"acceptances - this kind stays report-only. Bootstrap {boot}")
        print(f"  used {len(rows)} labelled ({result[kind]['labelled_positive']} positive), "
              f"{unsure} unsure, {missing} missing")
    sv.CALIBRATION_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Written {sv.CALIBRATION_PATH}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="instance/cyber_events.db")
    add_model_arguments(parser)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sample")
    p.add_argument("--sample-high", type=int, default=60)
    p.add_argument("--sample-mid", type=int, default=40)
    p.add_argument("--sample-low", type=int, default=15)
    p.add_argument("--seed", type=int, default=20261027)
    p.set_defaults(func=cmd_sample)
    p = sub.add_parser("packet")
    p.add_argument("--out", required=True, help="Directory for the blind labelling packet")
    p.add_argument("--chunks", type=int, default=4)
    p.add_argument("--seed", type=int, default=20261027)
    p.set_defaults(func=cmd_packet)
    p = sub.add_parser("calibrate")
    p.add_argument("--precision", type=float, default=0.95)
    p.add_argument("--min-accepts", type=int, default=5,
                   help="A cut-off must accept at least this many labelled items to count.")
    p.set_defaults(func=cmd_calibrate)
    args = parser.parse_args(argv)
    apply_model_arguments(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
