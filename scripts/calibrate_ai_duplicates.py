"""Score, sample, label and calibrate the merge cut-off for AI-involved event pairs.

Every pair of active AI-involved events within the window is a duplicate
candidate whatever its victim (``dedup/ai_duplicates.py``). Its cut-off is
calibrated apart from the organisation route's (0.81) because the pairs, the
question (it knows how rare AI incidents are) and the error trade-off differ.
Four steps, each reproducible from committed files:

``score``      Judges every candidate pair with the AI-pair question (Jev, the
               middle band escalated to the reasoning model) and stores the
               answers in ``ModelJudgements``. **Paid.** The same judging
               ``check-duplicates`` does for these pairs, without researching
               organisation timelines or merging; run it on the database
               copy that will be the labelling snapshot.

``sample``     Writes ``evaluation/ai_duplicate_sample.csv`` from the stored
               final score (the escalation's inside the band, else Jev's):
               ``census`` - every pair scoring 0.30 to 0.90, the band in which
               any plausible cut-off lies (the organisation route's is 0.81) -
               plus seeded samples of ``low`` (below 0.30) and ``high``
               (0.90 and above), each weighted up to its stratum.

``packet``     Writes the labelling packet (JSON lines): both events' evidence
               and the gap, **without any score, stratum, AI category or
               description**, so labels are blind to the model being calibrated.

``calibrate``  Reads the labels (``evaluation/ai_duplicate_labels.csv``:
               ``pair_id,label,rationale,corroboration,labeller`` with label
               ``same``, ``different`` or ``unsure`` - the definitions of
               ``evaluation/duplicate_labels.csv``; see
               ``evaluation/ai_duplicate_labelling_guide.md``) and writes
               ``evaluation/ai_duplicate_calibration.json``. The cut-off is the
               lowest final score at which the pairs it would merge are,
               weighted by stratum, at least ``--precision`` (0.95) labelled
               ``same``, with at least ``--min-merges`` labelled merges; a
               stratified bootstrap of the whole choose-then-test procedure is
               the out-of-sample precision. Missing or unreadable labels fail.
               The models, question versions and evidence fingerprint are
               recorded: ``check-duplicates`` merges AI pairs only while all
               still match, and ``dedup_admin.py recalibrate --step
               ai-duplicates`` re-scores the labelled pairs on current evidence.

Labels describe the database as it was when scored: after merging, labelled
``same`` pairs no longer exist as pairs. Take the labelling snapshot (a copy of
the live database, registered in ``evaluation/calibration_snapshots.json`` as
``ai-duplicates``) after ``score`` and before any AI merge.

Usage (from the project root):
    python scripts/calibrate_ai_duplicates.py --db <snapshot> score
    python scripts/calibrate_ai_duplicates.py --db <snapshot> sample
    python scripts/calibrate_ai_duplicates.py --db <snapshot> packet --out <dir>
    python scripts/calibrate_ai_duplicates.py --db <snapshot> calibrate --precision 0.95
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.dedup import ai_duplicates as ad  # noqa: E402
from cyber_data_collector.dedup import duplicate_judgement as dj  # noqa: E402
from cyber_data_collector.utils.llm_models import (  # noqa: E402
    add_model_arguments, apply_model_arguments,
)
from scripts.calibrate_event_reality import bootstrap, choose_cutoff  # noqa: E402
from scripts.calibrate_supplier_victims import different_bound  # noqa: E402

#: Every pair scoring in [CENSUS_LOW, CENSUS_HIGH) is labelled: any plausible cut-off lies in it.
CENSUS_LOW, CENSUS_HIGH = 0.30, 0.90
STRATA = ("low", "census", "high")


def stratum(p: float) -> str:
    return "low" if p < CENSUS_LOW else ("census" if p < CENSUS_HIGH else "high")


def merge_cutoff(rows: List[Dict], precision: float, min_merges: int):
    """The lowest P(same) whose merges meet ``precision`` (weighted by stratum); and the curve.

    The reality filter's rule (reject below a cut-off, weighted precision on the
    rejected side) applied to 1 - P(same), as the organisation route does, so
    the calibrations cannot drift apart in method. Returns
    ``(chosen, curve, mirrored)``; ``mirrored`` is what :func:`bootstrap` takes.
    """
    mirrored = [{"p": 1.0 - r["p"], "weight": r["weight"], "not_incident": r["same"], "stratum": r["stratum"]}
                for r in rows]
    chosen, table = choose_cutoff(mirrored, precision, min_merges)
    curve = [{"cutoff": round(1.0 - t["cutoff"], 6), "merged_labelled": t["rejected_labelled"],
              "correct_labelled": t["correct_labelled"], "weighted_precision": t["weighted_precision"],
              "weighted_recall": t["weighted_recall"], "unweighted_precision_ci95": t["unweighted_precision_ci95"]}
             for t in table]
    if chosen is None:
        return None, curve, mirrored
    at = next(c for c in curve if c["merged_labelled"] == chosen["rejected_labelled"]
              and c["correct_labelled"] == chosen["correct_labelled"])
    return at, curve, mirrored


def read_sample(path: Optional[Path] = None) -> Dict[str, Dict[str, str]]:
    path = path or ad.SAMPLE_PATH
    try:
        with Path(path).open(encoding="utf-8", newline="") as fh:
            return {r["pair_id"]: r for r in csv.DictReader(fh)}
    except OSError as exc:
        raise SystemExit(f"Sample file {path} cannot be read ({exc}); run the sample step first")


def cmd_score(args) -> int:
    """Judge every candidate pair (paid) so that the sample can be stratified on stored scores."""
    from cyber_data_collector.utils import jev

    jev.require()   # fails loudly; never skip or switch model
    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set: the middle band cannot be escalated. Refusing to run.")
        return 2
    conn = sqlite3.connect(args.db)
    pairs, stats = ad.ranked_candidates(conn)        # the pairs check-duplicates will ask, not all of them
    print(f"Candidates: {stats['before']} in the window -> {stats['after']} after ranking "
          f"(partners per event {stats['parameters']['partners_per_event']})")
    if args.limit:
        pairs = pairs[:args.limit]
    scores, escalated, failed = ad.score_pairs(conn, pairs)
    print(f"{len(pairs)} candidate pair(s) in a {ad.AI_WINDOW_DAYS}-day window; scored {len(scores)}, "
          f"escalated {escalated}, {len(failed)} escalation(s) failed, "
          f"{len(pairs) - len(scores) - len(failed)} not judged (see the log)")
    return 1 if failed or len(scores) + len(failed) < len(pairs) else 0


def cmd_sample(args) -> int:
    conn = sqlite3.connect(args.db)
    rng = random.Random(args.seed)
    ranked = ad.ranked_candidates(conn)[0]
    current = {p["pair_id"] for p in ranked}
    final = ad.stored_final_probabilities(conn, ranked)     # decisions on the pairs' current evidence
    probs = {pid: p for pid, p in final.items() if pid in current}
    unscored = len(current - set(probs))
    if unscored:
        print(f"{unscored} candidate pair(s) have no final score on their current evidence (not judged, evidence changed, or escalation failed): "
              "run the score step first; they are left out")
    bands: Dict[str, List[str]] = {name: [] for name in STRATA}
    for pid, p in probs.items():
        bands[stratum(p)].append(pid)
    want = {"low": args.sample_low, "census": None, "high": args.sample_high}
    rows = []
    for name in STRATA:
        ids = sorted(bands[name])
        chosen = ids if want[name] is None else sorted(rng.sample(ids, min(want[name], len(ids))))
        weight = len(ids) / len(chosen) if chosen else 0.0
        rows += [(pid, name, round(weight, 4)) for pid in chosen]
        print(f"{name:7}: {len(chosen)} of {len(ids)} (weight {weight:.2f})")
    ad.SAMPLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ad.SAMPLE_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["pair_id", "stratum", "weight"])
        w.writerows(rows)
    print(f"Seed {args.seed}. Written {ad.SAMPLE_PATH}")
    return 0


def _compact_event(state: dict) -> dict:
    """The evidence a labeller needs, without anything derived from the judgement."""
    records = []
    for r in state.get("records", []):
        date_info = r.get("date") or {}
        records.append({"title": r.get("title"), "url": r.get("url"), "page_text": (r.get("page_text") or "")[:700],
                        "published": date_info.get("published"), "date_basis": date_info.get("basis"),
                        "perplexity_named": (r.get("perplexity") or {}).get("formal_entity_name"),
                        "perplexity_reasoning": ((r.get("perplexity") or {}).get("reasoning") or "")[:400]})
    event = {k: v for k, v in state.get("event", {}).items() if k in ("title", "date", "victim", "record_count",
                                                                       "date_precision")}
    out = {"event": event, "records": records}
    if state.get("corroboration"):
        out["corroboration_search"] = state["corroboration"].get("answer")
    return out


QUESTION = ("Do EVENT_A and EVENT_B describe the SAME real-world incident? 'same': one incident reported "
            "again - follow-up coverage, commentary or analysis of it, a government, regulator or company "
            "response, an explainer, a later disclosure of more detail - even if the reports name a missing, "
            "vague or different organisation as the victim. 'different': separate occasions, victims, "
            "systems or actors (including a second incident disclosed later that involves the same AI "
            "system or vendor), or a different matter altogether. 'unsure' if the evidence, after searching "
            "the web, does not settle it.")


def cmd_packet(args) -> int:
    sample = read_sample()
    conn = sqlite3.connect(args.db)
    pairs = {p["pair_id"]: p for p in ad.candidate_pairs(conn)}
    items, missing = [], []
    for pid in sorted(sample):
        pair = pairs.get(pid)
        if pair is None:
            missing.append(pid)
            continue
        items.append({"pair_id": pid, "question": QUESTION, "gap_days": pair["gap_days"],
                      "shared_organisation": pair["organisation"],
                      "event_a": _compact_event(dj._event_with_dates(conn, pair["a"])),
                      "event_b": _compact_event(dj._event_with_dates(conn, pair["b"]))})
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # Shuffle (seeded) so strata - which follow the score - are not visible in the order.
    random.Random(args.seed).shuffle(items)
    for n in range(args.chunks):
        chunk = items[n::args.chunks]
        path = out / f"ai_duplicate_packet_{n + 1}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for item in chunk:
                fh.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        print(f"Written {len(chunk)} item(s) to {path}")
    if missing:
        print(f"{len(missing)} sampled pair(s) are no longer candidates (data changed since sampling): "
              + ", ".join(missing[:10]))
    return 0


def labelled_rows(sample, labels, probs):
    rows, unsure, missing = [], 0, 0
    for pid, s in sample.items():
        label = labels.get(pid)
        if not label or pid not in probs:
            missing += 1
            continue
        if label == "unsure":
            unsure += 1
            continue
        if label not in ("same", "different"):
            raise SystemExit(f"Label {label!r} for {pid} is not same / different / unsure")
        rows.append({"id": pid, "stratum": s["stratum"], "weight": float(s["weight"]), "p": probs[pid],
                     "same": label == "same"})
    return rows, unsure, missing


def cmd_calibrate(args) -> int:
    sample = read_sample()
    try:
        labels = ad.labelled_pairs(ad.LABELS_PATH, required=True)
    except RuntimeError as exc:
        print(f"Cannot calibrate: {exc}")
        return 2
    conn = sqlite3.connect(args.db)
    probs = ad.stored_final_probabilities(conn, [p for p in ad.candidate_pairs(conn) if p["pair_id"] in sample])
    rows, unsure, missing = labelled_rows(sample, labels, probs)
    chosen, curve, mirrored = merge_cutoff(rows, args.precision, args.min_merges)
    boot = bootstrap(mirrored, args.precision, args.min_merges)
    # The calibrated negative (see calibrate_duplicates.py): used to hold a link that would join events
    # a low-scoring pair says are different incidents.
    bound = different_bound([{**r, "positive": r["same"]} for r in rows], args.precision, args.min_merges)
    result = {**ad.calibration_identity(), **bound, "escalate_band": list(ad.ESCALATE_BAND), "window_days": ad.AI_WINDOW_DAYS,
              "calibrated_on_database": Path(args.db).name,
              "labels_file": str(ad.LABELS_PATH.relative_to(ad.ROOT)), "labels_sha256": ad.labels_sha256(),
              "sample_file": str(ad.SAMPLE_PATH.relative_to(ad.ROOT)),
              "cutoff": chosen["cutoff"] if chosen else None, "precision_target": args.precision,
              "min_merges": args.min_merges, "at_cutoff": chosen, "bootstrap_out_of_sample_precision": boot,
              "labelled_used": len(rows), "labelled_same": sum(r["same"] for r in rows),
              "unsure_excluded": unsure, "missing": missing, "generated": datetime.now().isoformat(),
              "organisation_route_cutoff": _organisation_cutoff(), "curve": curve}
    ad.CALIBRATION_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if chosen:
        print(f"Cut-off {chosen['cutoff']:.4f}: merges {chosen['merged_labelled']} labelled pairs, "
              f"{chosen['correct_labelled']} truly the same; weighted precision {chosen['weighted_precision']:.3f}, "
              f"weighted recall {chosen['weighted_recall']:.3f}, 95% CI "
              f"{chosen['unweighted_precision_ci95'][0]:.2f}-{chosen['unweighted_precision_ci95'][1]:.2f} "
              f"(organisation route: {result['organisation_route_cutoff']})")
    else:
        print(f"No cut-off reaches precision {args.precision} with {args.min_merges}+ labelled merges: "
              "AI pairs stay report-only.")
    print(f"Bootstrap out-of-sample precision: {boot}")
    print(f"'Different' bound: {bound['different_cutoff']}")
    print(f"Used {len(rows)} labelled pairs ({result['labelled_same']} same; {unsure} unsure excluded, "
          f"{missing} missing). Written {ad.CALIBRATION_PATH}")
    return 0


def _organisation_cutoff() -> Optional[float]:
    try:
        return json.loads(dj.CALIBRATION_PATH.read_text(encoding="utf-8")).get("cutoff")
    except (OSError, ValueError):
        return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="instance/cyber_events.db")
    add_model_arguments(parser)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("score", help="Judge every candidate pair (paid) and store the answers")
    p.add_argument("--limit", type=int, default=0, help="Only the first N pairs (a trial)")
    p.set_defaults(func=cmd_score)
    p = sub.add_parser("sample")
    p.add_argument("--sample-high", type=int, default=40)
    p.add_argument("--sample-low", type=int, default=40)
    p.add_argument("--seed", type=int, default=20261029)
    p.set_defaults(func=cmd_sample)
    p = sub.add_parser("packet")
    p.add_argument("--out", required=True, help="Directory for the blind labelling packet")
    p.add_argument("--chunks", type=int, default=4)
    p.add_argument("--seed", type=int, default=20261029)
    p.set_defaults(func=cmd_packet)
    p = sub.add_parser("calibrate")
    p.add_argument("--precision", type=float, default=0.95)
    p.add_argument("--min-merges", type=int, default=5,
                   help="A cut-off must merge at least this many labelled pairs to count.")
    p.set_defaults(func=cmd_calibrate)
    args = parser.parse_args(argv)
    apply_model_arguments(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
