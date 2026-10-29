"""Sample, package for blind labelling, and calibrate the same-organisation coherence check.

Steps, each reproducible from committed files (see ``cyber_data_collector/dedup/coherence_same_org.py``):

``sample``     Writes ``evaluation/coherence_same_org_sample.csv`` - (event, member)
               pairs from active multi-member events. Candidates (members naming the event's
               own organisation, but with a verified date far from the event's or a different
               incident in the organisation's timeline) are stratified on the *stored final
               score* (``check-same-organisation`` must have judged them first). Labellers are
               never shown the score; stratifying on it only decides who is labelled, and the
               weights undo it:
               * Stratum ``A`` - every candidate scoring below ``--census-below`` (0.5): a
                 census, weight 1, because any cut-off lies here.
               * Stratum ``B`` - a seeded sample of those scoring 0.5-0.8; ``C`` - 0.8 and above.
                 Weighted up to their bands; they measure recall.
               * Stratum ``N`` - a seeded sample of the other same-organisation members
                 (not candidates), weighted up to its population. It measures the incidents
                 the candidate selection keeps (reported, never acted on).
               Only candidates can be split by the model, so any cut-off acts on A-C.
``packet``     Writes a blind labelling packet per sampled pair (JSON lines): the event, its
               records and the record under review - no score, no stratum, no candidate reasons.
``judge``      Asks Jev (and the escalation model in its band) about every sampled candidate.
               Answers are stored; re-running reuses them.
``calibrate``  Reads ``evaluation/coherence_same_org_labels.csv``
               (``deduplicated_event_id,enriched_event_id,label,rationale,incident,labeller`` with
               label ``same`` - the record reports the event's incident - ``different`` - a separate
               incident at the same organisation - or ``unsure``) and the stored final scores, and
               writes ``evaluation/coherence_same_org_calibration.json``: the largest cut-off at which
               the stratum A-C members it would split are, weighted, at least ``--precision`` (0.95)
               labelled ``different``, with a stratified bootstrap of the whole choose-then-test
               procedure, and the
               identity (models, question versions, evidence fingerprint) the pipeline checks.

Labelling convention (fix it before any score is seen): a record is ``different`` only when its own
content shows a separate incident - another attack, system, attacker or method, or a clearly separate
breach in the organisation's history - and ``same`` when it is a first report, follow-up, notification,
regulator statement or class action about the event's incident, even if it carries a wrong date.

Labels describe the database when they were made, so calibrate on that snapshot (``--db``); its file
name is recorded, and ``dedup_admin.py recalibrate`` re-scores the labelled cases when the evidence
processing changes (register the snapshot in ``evaluation/calibration_snapshots.json``).

Usage (from the project root):
    python scripts/calibrate_coherence_same_org.py --db <snapshot> sample
    python scripts/calibrate_coherence_same_org.py --db <snapshot> packet --out <dir>
    python scripts/calibrate_coherence_same_org.py --db <snapshot> judge
    python scripts/calibrate_coherence_same_org.py --db <snapshot> calibrate --precision 0.95
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

try:  # the project's .env, explicitly (cwd-independent)
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # pragma: no cover
    pass

from cyber_data_collector.dedup import coherence as co  # noqa: E402
from cyber_data_collector.dedup import coherence_same_org as so  # noqa: E402
from cyber_data_collector.dedup.entity_resolution import EntityResolver  # noqa: E402
from cyber_data_collector.utils.llm_models import add_model_arguments, apply_model_arguments  # noqa: E402
from scripts.calibrate_event_reality import bootstrap, choose_cutoff, wilson  # noqa: E402

SAMPLE_PATH = so.ROOT / "evaluation" / "coherence_same_org_sample.csv"


def _events(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """Every active multi-member event with its candidate members."""
    resolver = EntityResolver(conn)
    all_ids = [e[0] for e in co._multi_member_events(conn)]
    return {e["id"]: e for e in so.find_candidate_events(conn, resolver, include=all_ids)}


CANDIDATE_STRATA = ("A", "B", "C")


def cmd_sample(args) -> int:
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    events = _events(conn)
    probs = so.stored_final_probabilities(conn)
    bands: Dict[str, List[tuple]] = {"A": [], "B": [], "C": [], "N": []}
    unjudged = 0
    for did, e in sorted(events.items()):
        for m in e["members"]:
            if m["id"] in e["suspect_ids"]:
                continue          # other-organisation members belong to the stray check
            if m["id"] not in e["candidates"]:
                bands["N"].append((did, m["id"]))
                continue
            p = probs.get(co.member_subject_id(did, m["id"]))
            if p is None:
                unjudged += 1
                continue
            bands["A" if p < args.census_below else "B" if p < 0.8 else "C"].append((did, m["id"]))
    rng = random.Random(args.seed)
    chosen = {"A": sorted(bands["A"]),
              "B": sorted(rng.sample(sorted(bands["B"]), min(args.sample_mid, len(bands["B"])))),
              "C": sorted(rng.sample(sorted(bands["C"]), min(args.sample_high, len(bands["C"])))),
              "N": sorted(rng.sample(sorted(bands["N"]), min(args.sample_other, len(bands["N"]))))}
    SAMPLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SAMPLE_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["deduplicated_event_id", "enriched_event_id", "stratum", "weight"])
        for stratum, pairs in chosen.items():
            weight = len(bands[stratum]) / len(pairs) if pairs else 0.0
            w.writerows([(d, m, stratum, round(weight, 4)) for d, m in pairs])
            print(f"Stratum {stratum}: {len(pairs)} of {len(bands[stratum])} members (weight {weight:.2f})")
    print(f"Seed {args.seed}; snapshot {Path(args.db).name}; {unjudged} candidate(s) had no stored final "
          f"score (run check-same-organisation on the snapshot first). Written {SAMPLE_PATH}")
    return 1 if unjudged else 0


def _sample() -> List[Dict[str, str]]:
    with SAMPLE_PATH.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def cmd_packet(args) -> int:
    """Blind packets: what a labeller needs, and nothing about any score, stratum or candidate reason."""
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    events = _events(conn)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = sorted(_sample(), key=lambda r: (r["deduplicated_event_id"], r["enriched_event_id"]))
    rng = random.Random(args.seed)
    rng.shuffle(rows)   # strata follow the selection; the order must not
    chunks = [rows[i::args.parts] for i in range(args.parts)] if args.parts > 1 else [rows]
    for n, chunk in enumerate(chunks, 1):
        path = out_dir / f"coherence_same_org_packet_{n}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for r in chunk:
                event = events[r["deduplicated_event_id"]]
                state = so.build_member_state(conn, event, r["enriched_event_id"])
                state.pop("flagged_because", None)
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
             if r["deduplicated_event_id"] in events and r["stratum"] in CANDIDATE_STRATA
             and r["enriched_event_id"] in events[r["deduplicated_event_id"]]["candidates"]]
    scores = so.judge_members(conn, items)
    print(f"Judged {len(scores)} sampled candidate(s); "
          f"{sum(1 for s in scores.values() if s['p_escalated'] is not None)} escalated")
    return 0


def cmd_calibrate(args) -> int:
    sample = {co.member_subject_id(r["deduplicated_event_id"], r["enriched_event_id"]): r for r in _sample()}
    labels = co.load_labels(so.LABELS_PATH)
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    probs = so.stored_final_probabilities(conn)

    rows, other = [], []
    unsure = missing = 0
    for sid, s in sample.items():
        label = labels.get(sid)
        if label not in ("same", "different"):
            unsure += label == "unsure"
            missing += label is None
            continue
        row = {"id": sid, "stratum": s["stratum"], "weight": float(s["weight"]), "p": probs.get(sid),
               "not_incident": label == "different"}
        if s["stratum"] in CANDIDATE_STRATA:
            if row["p"] is None:
                missing += 1
                continue
            rows.append(row)
        else:
            other.append(row)

    chosen, table = choose_cutoff(rows, args.precision, args.min_splits)
    boot = bootstrap(rows, args.precision, args.min_splits)
    w_all = sum(r["weight"] for r in other)
    w_diff = sum(r["weight"] for r in other if r["not_incident"])
    n_diff = sum(r["not_incident"] for r in other)
    lo, hi = wilson(n_diff, len(other))
    keeps = {"labelled": len(other), "labelled_different": n_diff,
             "weighted_share_different": (w_diff / w_all) if w_all else None,
             "unweighted_share_ci95": [lo, hi], "different_ids": sorted(r["id"] for r in other if r["not_incident"])}
    result = {
        **so.calibration_identity(), "escalate_band": list(so.ESCALATE_BAND),
        "calibrated_on_database": Path(args.db).name,
        "cutoff": chosen["cutoff"] if chosen else None,
        "precision_target": args.precision, "min_splits": args.min_splits,
        "at_cutoff": chosen, "labelled_used": len(rows),
        "labelled_different": sum(r["not_incident"] for r in rows),
        "unsure_excluded": unsure, "missing": missing,
        "labels_file": so.LABELS_PATH.relative_to(so.ROOT).as_posix(),
        "sample_file": SAMPLE_PATH.relative_to(so.ROOT).as_posix(), "generated": datetime.now().isoformat(),
        "bootstrap_out_of_sample_precision": boot, "candidate_selection_misses": keeps,
        "curve": [{"cutoff": t["cutoff"], "split_labelled": t["rejected_labelled"],
                   "correct_labelled": t["correct_labelled"], "precision": t["weighted_precision"],
                   "recall": t["weighted_recall"], "precision_ci95": t["unweighted_precision_ci95"]}
                  for t in table],
    }
    so.CALIBRATION_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if chosen:
        print(f"Cut-off {chosen['cutoff']:.4f}: splits {chosen['rejected_labelled']} labelled candidates, "
              f"{chosen['correct_labelled']} truly separate incidents; precision {chosen['weighted_precision']:.3f}, "
              f"recall {chosen['weighted_recall']:.3f}")
    else:
        print(f"No cut-off reaches precision {args.precision}: model-judged splits stay report-only.")
    print(f"Bootstrap out-of-sample precision: {boot}")
    print(f"Candidate selection: {n_diff} of {len(other)} labelled non-candidates are separate incidents "
          f"(weighted share {keeps['weighted_share_different']})")
    print(f"Used {len(rows)} labelled candidates ({result['labelled_different']} different; {unsure} unsure "
          f"excluded, {missing} missing). Written {so.CALIBRATION_PATH}")
    return 0


def plan(conn: sqlite3.Connection, module) -> "object":
    """The recalibration plan (``dedup/recalibration.py``): labelled stratum A-C candidates the snapshot holds."""
    from cyber_data_collector.dedup.recalibration import Plan, RecalibrationError, _require_agreement

    labels = co.load_labels(module.LABELS_PATH)
    sample = [r for r in _sample() if r["stratum"] in CANDIDATE_STRATA
              and labels.get(co.member_subject_id(r["deduplicated_event_id"], r["enriched_event_id"]))
              in ("same", "different")]
    events = _events(conn)
    items = [(events[r["deduplicated_event_id"]], r["enriched_event_id"]) for r in sample
             if r["deduplicated_event_id"] in events
             and r["enriched_event_id"] in events[r["deduplicated_event_id"]]["candidates"]]

    def run(conn, judge, client):
        scores = module.judge_members(conn, items, judge=judge, escalation_client=client)
        undecided = sorted(s for s, v in scores.items() if v.get("error"))
        if undecided:
            raise RecalibrationError(f"{len(undecided)} member(s) could not be escalated (first: {undecided[0]}); "
                                     "see the log. Nothing was calibrated.")
        _require_agreement("member", {s: v["p"] for s, v in scores.items()}, module.stored_final_probabilities(conn))

    return Plan(items, len(sample), run)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="instance/cyber_events.db")
    add_model_arguments(parser)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sample")
    p.add_argument("--census-below", type=float, default=0.5)
    p.add_argument("--sample-mid", type=int, default=25)
    p.add_argument("--sample-high", type=int, default=25)
    p.add_argument("--sample-other", type=int, default=40)
    p.add_argument("--seed", type=int, default=20261029)
    p.set_defaults(func=cmd_sample)
    p = sub.add_parser("packet")
    p.add_argument("--out", required=True)
    p.add_argument("--parts", type=int, default=1, help="Split the packet into this many files.")
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(func=cmd_packet)
    p = sub.add_parser("judge")
    p.set_defaults(func=cmd_judge)
    p = sub.add_parser("calibrate")
    p.add_argument("--precision", type=float, default=0.95)
    p.add_argument("--min-splits", type=int, default=5,
                   help="A cut-off must split at least this many labelled candidates to count.")
    p.set_defaults(func=cmd_calibrate)
    args = parser.parse_args(argv)
    apply_model_arguments(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
