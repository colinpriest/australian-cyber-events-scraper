"""Sample, package for blind labelling, and calibrate the victim-attribution judgement.

Three steps, each reproducible from committed files:

``sample``     Writes ``evaluation/victim_attribution_sample.csv``: event-victim
               pairs stratified on the stored *final* P(recorded victim is
               right) (escalation in the band, else Jev).
               * ``A`` - every pair below ``--census-below`` (0.5): a census,
                 weight 1, because any cut-off lies here.
               * ``B`` - a seeded sample of 0.5-0.9; ``C`` - of 0.9 and above.
               B and C are weighted up to their bands and measure recall.

               ``sample --extend-below P --extend-stratum NAME`` instead **adds
               a census** to the existing sample file: every pair whose final
               score on ``--db``'s *current* evidence is below ``P`` and that
               is not yet sampled becomes a row of stratum ``NAME``, weight 1,
               and a sampled pair in that region whose weight was above 1
               moves into it with weight 1 (it is now included with
               certainty). Every other row keeps its stratum and weight: a
               row outside the region keeps its inclusion probability, so its
               weight (1/probability) still estimates its stratum's pairs
               outside the region without bias. Every pair must have a score on
               its current evidence (run ``dedup_admin.py check-victim-attribution``
               on the database first); otherwise nothing is written. With
               ``--frame-db`` the census is limited to the pairs of the
               database the sample was drawn on (its weights describe that
               population); pairs of ``--db`` outside it are left out and
               counted.

``packets``    Writes one JSON labelling packet per sampled pair to
               ``--out`` (not committed): the event, its records' page text and
               Perplexity research, the corroboration search, the recorded
               victim's timeline and the candidate organisations (the same
               candidates a replacement is chosen among, including those the
               event's text names) - and **no score**. Labellers work from the
               packet plus web search. ``--unlabelled-only`` writes packets only
               for sampled pairs with no label yet (an extension).

``calibrate``  Reads ``evaluation/victim_attribution_labels.csv``
               (``pair_id,deduplicated_event_id,recorded_victim,label,
               correct_victim,rationale,labeller``; label ``correct``,
               ``wrong``, ``unsure`` or ``not_incident``) and the stored
               scores, and writes ``evaluation/victim_attribution_calibration.json``:
               * the **cut-off** - the largest at which the pairs it would act
                 on are, weighted by stratum, at least ``--precision`` labelled
                 ``wrong`` (at least ``--min-actions`` of them);
               * the **replacement threshold** - the lowest probability of
                 Jev's choice among the evidence's candidates at which the
                 chosen organisation is the labelled correct victim for at
                 least ``--precision`` of labelled-wrong pairs (at least
                 ``--min-replacements``); none if unreachable, and then wrong
                 victims are flagged, never replaced.
               ``unsure`` and ``not_incident`` labels are excluded and counted.
               A stratified bootstrap of each choose-then-test procedure is the
               out-of-sample estimate. The models and question versions are
               recorded; the pipeline acts only while all match.

Calibrate on the database as it was when labelled (re-attribution changes the
pairs); the snapshot name is recorded.

Usage (from the project root):
    python scripts/calibrate_victim_attribution.py --db <snapshot> sample
    python scripts/calibrate_victim_attribution.py --db <snapshot> sample --extend-below 0.5 --extend-stratum D \n        --frame-db <database the sample was drawn on>
    python scripts/calibrate_victim_attribution.py --db <snapshot> packets --out <dir> [--unlabelled-only]
    python scripts/calibrate_victim_attribution.py --db <snapshot> calibrate
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.dedup import deployment  # noqa: E402
from scripts.evaluate_calibrations import after_calibration  # noqa: E402
from cyber_data_collector.dedup import victim_judgement as vj  # noqa: E402
from cyber_data_collector.utils.llm_models import add_model_arguments, apply_model_arguments  # noqa: E402
from scripts.calibrate_event_reality import bootstrap, choose_cutoff, wilson  # noqa: E402

SAMPLE_PATH = vj.ROOT / "evaluation" / "victim_attribution_sample.csv"


class ExtensionError(ValueError):
    """The sample cannot be extended as asked; nothing is written."""


def extend_sample(rows: List[Dict[str, str]], probs: Dict[str, float], population: List[str],
                  below: float, stratum: str) -> Dict:
    """The sample with a census of every pair of ``population`` scoring below ``below`` added as ``stratum``.

    ``rows`` are the sample file's rows (``pair_id``, ``stratum``, ``weight``); ``probs`` the final score of
    each pair on its current evidence. Returns ``{"rows", "added", "moved", "region"}``. A sampled pair in
    the region with weight above 1 moves into ``stratum`` with weight 1; every other row is unchanged.
    Raises :class:`ExtensionError` when a pair of ``population`` has no score (a census needs every one),
    when ``stratum`` is already used, or when a pair is sampled twice.
    """
    seen = [r["pair_id"] for r in rows]
    if len(set(seen)) != len(seen):
        raise ExtensionError("the sample file lists a pair more than once")
    if any(r["stratum"] == stratum for r in rows):
        raise ExtensionError(f"stratum {stratum!r} is already in the sample; choose a new --extend-stratum")
    unscored = [pid for pid in population if pid not in probs]
    if unscored:
        raise ExtensionError(f"{len(unscored)} pair(s) have no final score on their current evidence (e.g. "
                             f"{unscored[0]}); run dedup_admin.py check-victim-attribution on this database first")
    region = sorted(pid for pid in population if probs[pid] < below)
    in_region = set(region)
    out, moved = [], []
    for r in rows:
        if r["pair_id"] in in_region and float(r["weight"]) > 1.0:
            moved.append({"pair_id": r["pair_id"], "from_stratum": r["stratum"], "from_weight": r["weight"]})
            out.append({"pair_id": r["pair_id"], "stratum": stratum, "weight": 1.0})
        else:
            out.append(dict(r))
    added = [pid for pid in region if pid not in set(seen)]
    out += [{"pair_id": pid, "stratum": stratum, "weight": 1.0} for pid in added]
    return {"rows": out, "added": added, "moved": moved, "region": region}


def frame_pairs(path: str) -> List[str]:
    """The pairs of the database a sample was drawn on (read-only): the population its weights describe."""
    if not Path(path).is_file():
        raise ExtensionError(f"frame database {path} not found")
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        return [p["pair_id"] for p in vj.pairs_to_check(conn)]
    finally:
        conn.close()


def cmd_extend(args) -> int:
    rows = list(csv.DictReader(SAMPLE_PATH.open(encoding="utf-8")))
    conn = sqlite3.connect(args.db)
    pairs = vj.pairs_to_check(conn)
    population = [p["pair_id"] for p in pairs]
    outside = 0
    try:
        if args.frame_db:
            # The census covers the population the existing weights describe, not pairs the sample could never
            # have drawn (on a pre-rejection snapshot: events rejected before the sample was drawn).
            frame = set(frame_pairs(args.frame_db))
            missing = [r["pair_id"] for r in rows if r["pair_id"] not in frame]
            if missing:
                raise ExtensionError(f"{len(missing)} sampled pair(s) are not in the frame database (e.g. "
                                     f"{missing[0]}): it is not the database the sample was drawn on")
            outside = sum(pid not in frame for pid in population)
            population = [pid for pid in population if pid in frame]
        probs = vj.stored_final_probabilities(conn, [p for p in pairs if p["pair_id"] in set(population)])
        ext = extend_sample(rows, probs, population, args.extend_below, args.extend_stratum)
    except ExtensionError as exc:
        print(f"ERROR: {exc}. Nothing written.", file=sys.stderr)
        return 2
    if args.frame_db:
        print(f"Frame {Path(args.frame_db).name}: {len(population)} pair(s) of {Path(args.db).name} in it, "
              f"{outside} outside it left out of the census.")
    with SAMPLE_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["pair_id", "stratum", "weight"])
        w.writerows([(r["pair_id"], r["stratum"], r["weight"]) for r in ext["rows"]])
    print(f"Census of the {len(ext['region'])} pair(s) of {Path(args.db).name} scoring below {args.extend_below} "
          f"on current evidence, as stratum {args.extend_stratum} (weight 1): {len(ext['added'])} added, "
          f"{len(ext['moved'])} sampled pair(s) moved in from "
          f"{dict(sorted(Counter(m['from_stratum'] for m in ext['moved']).items()))}; "
          f"other rows unchanged. Written {SAMPLE_PATH}")
    return 0


def cmd_sample(args) -> int:
    if args.extend_below is not None:
        if not args.extend_stratum:
            print("ERROR: --extend-below needs --extend-stratum (a new stratum name)", file=sys.stderr)
            return 2
        return cmd_extend(args)
    conn = sqlite3.connect(args.db)
    pairs = vj.pairs_to_check(conn)
    probs = vj.stored_final_probabilities(conn, pairs)      # decisions on each pair's current evidence
    unjudged = sum(1 for p in pairs if p["pair_id"] not in probs)
    ids = [p["pair_id"] for p in pairs if p["pair_id"] in probs]
    bands = {"A": sorted(i for i in ids if probs[i] < args.census_below),
             "B": sorted(i for i in ids if args.census_below <= probs[i] < 0.9),
             "C": sorted(i for i in ids if probs[i] >= 0.9)}
    rng = random.Random(args.seed)
    chosen = {"A": bands["A"],
              "B": sorted(rng.sample(bands["B"], min(args.sample_mid, len(bands["B"])))),
              "C": sorted(rng.sample(bands["C"], min(args.sample_high, len(bands["C"]))))}
    with SAMPLE_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["pair_id", "stratum", "weight"])
        for stratum, sel in chosen.items():
            weight = len(bands[stratum]) / len(sel) if sel else 0.0
            w.writerows([(i, stratum, round(weight, 4)) for i in sel])
            print(f"Stratum {stratum}: {len(sel)} of {len(bands[stratum])} pairs (weight {weight:.2f})")
    print(f"Seed {args.seed}; {unjudged} pair(s) had no final score. Written {SAMPLE_PATH}")
    return 0


def packet(conn: sqlite3.Connection, pair_id: str) -> Dict:
    """Everything a labeller sees for one pair - never a score.

    The candidates are those a replacement is chosen among (:func:`vj.choose_replacement`), including the
    organisations the event's text names (one stored extraction per event, reused by ``calibrate``), so a
    labeller who copies a candidate string names exactly what the choice can return. A failed extraction
    raises: a packet is never written with fewer candidates than the choice would see.
    """
    from cyber_data_collector.dedup import event_organisations
    from cyber_data_collector.dedup.org_timeline import stored

    did, entity_id = pair_id.split("|")
    entity_id = int(entity_id)
    victim = conn.execute("SELECT entity_name FROM EntitiesV2 WHERE entity_id = ?", (entity_id,)).fetchone()[0]
    state = vj.build_state(conn, did, entity_id, victim)
    timeline = stored(conn, entity_id)
    if timeline:
        state["organisation_timeline"]["researched_at"] = timeline["researched_at"][:10]
    others = [n for _, n in vj._victims(conn, did) if n != victim]
    organisations = event_organisations.named_organisations(conn, did)
    conn.commit()   # the extraction is stored once
    state["candidate_organisations_from_evidence"] = [
        c["name"] for c in vj.replacement_candidates(conn, did, [victim] + others, organisations=organisations)]
    return {"pair_id": pair_id, "deduplicated_event_id": did, **state}


def cmd_packets(args) -> int:
    conn = sqlite3.connect(args.db)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = list(csv.DictReader(SAMPLE_PATH.open(encoding="utf-8")))
    if args.unlabelled_only:
        labelled = set(vj.load_labels())
        rows = [r for r in rows if r["pair_id"] not in labelled]
    rng = random.Random(args.seed)
    rng.shuffle(rows)   # labellers see no stratum order either
    index = []
    for n, r in enumerate(rows, 1):
        name = f"packet_{n:03d}.json"
        (out / name).write_text(json.dumps(packet(conn, r["pair_id"]), indent=1, ensure_ascii=False,
                                           default=str), encoding="utf-8")
        index.append({"packet": name, "pair_id": r["pair_id"]})
    (out / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    print(f"Wrote {len(index)} packets (no scores) to {out}")
    return 0


def replacement_threshold(rows: List[Dict], precision: float, minimum: int):
    """Lowest choice probability at which choices are the labelled victim for >= ``precision``."""
    chosen = [r for r in rows if r["choice"]]
    table = []
    for t in sorted({round(r["prob"], 6) for r in chosen}):
        acted = [r for r in chosen if r["prob"] >= t]
        w = sum(r["weight"] for r in acted)
        right = sum(r["weight"] for r in acted if r["right"])
        n_right = sum(r["right"] for r in acted)
        table.append({"threshold": t, "replaced_labelled": len(acted), "correct_labelled": n_right,
                      "weighted_precision": right / w if w else None,
                      "unweighted_precision_ci95": list(wilson(n_right, len(acted)))})
    ok = [t for t in table if t["weighted_precision"] is not None and t["weighted_precision"] >= precision
          and t["replaced_labelled"] >= minimum]
    return (min(ok, key=lambda t: t["threshold"]) if ok else None), table


def replacement_bootstrap(rows: List[Dict], precision: float, minimum: int, n: int = 1000, seed: int = 1):
    rng = random.Random(seed)
    oob: List[float] = []
    for _ in range(n):
        boot = [rng.choice(rows) for _ in rows]
        chosen, _ = replacement_threshold(boot, precision, minimum)
        if chosen is None:
            continue
        ids = {id(r) for r in boot}
        out = [r for r in rows if id(r) not in ids and r["choice"] and r["prob"] >= chosen["threshold"]]
        w = sum(r["weight"] for r in out)
        if w:
            oob.append(sum(r["weight"] for r in out if r["right"]) / w)
    oob.sort()
    if not oob:
        return {"replicates": 0, "median": None, "mean": None, "p05": None}
    return {"replicates": len(oob), "median": oob[len(oob) // 2], "mean": sum(oob) / len(oob),
            "p05": oob[int(0.05 * len(oob))]}


def cmd_calibrate(args) -> int:
    from cyber_data_collector.dedup.org_timeline import is_same_name
    from cyber_data_collector.utils.jev import JevJudge

    sample = {r["pair_id"]: r for r in csv.DictReader(SAMPLE_PATH.open(encoding="utf-8"))}
    labels = vj.load_labels(required=True)
    conn = sqlite3.connect(args.db)
    probs = vj.stored_final_probabilities(conn, [p for p in vj.pairs_to_check(conn) if p["pair_id"] in sample])

    rows, wrong_rows = [], []
    excluded = {"unsure": 0, "not_incident": 0, "missing": 0}
    for pid, s in sample.items():
        label = (labels.get(pid) or {}).get("label")
        if label in ("unsure", "not_incident"):
            excluded[label] += 1
            continue
        if label not in ("correct", "wrong") or pid not in probs:
            excluded["missing"] += 1
            continue
        row = {"id": pid, "stratum": s["stratum"], "weight": float(s["weight"]), "p": probs[pid],
               "not_incident": label == "wrong"}
        rows.append(row)
        if label == "wrong":
            wrong_rows.append((row, labels[pid]))

    chosen, curve = choose_cutoff(rows, args.precision, args.min_actions)
    boot = bootstrap(rows, args.precision, args.min_actions)

    # Replacement: Jev's choice for every labelled-wrong pair (stored answers reused).
    judge = JevJudge(conn)
    replacement_rows = []
    try:
        for row, label in wrong_rows:
            did = row["id"].split("|")[0]
            victims = [n for _, n in vj._victims(conn, did)]
            if len(victims) != 1:
                continue   # co-victim events are demoted, not replaced: not this threshold's population
            choice = vj.choose_replacement(conn, did, victims, judge)
            wanted = (label.get("correct_victim") or "").strip()
            name = choice["name"]
            right = bool(name and wanted and (name.lower() == wanted.lower() or is_same_name(name, wanted)))
            replacement_rows.append({"id": row["id"], "stratum": row["stratum"], "weight": row["weight"],
                                     "choice": name, "prob": choice["probability"] or 0.0, "right": right,
                                     "labelled_victim": wanted or None,
                                     "candidates": [c["name"] for c in choice["candidates"]]})
    finally:
        judge.close()
    r_chosen, r_curve = replacement_threshold(replacement_rows, args.precision, args.min_replacements)
    r_boot = replacement_bootstrap(replacement_rows, args.precision, args.min_replacements)

    result = {
        **vj.calibration_identity(), "escalate_band": list(vj.ESCALATE_BAND) if vj.ESCALATE_BAND else None,
        "calibrated_on_database": Path(args.db).name,
        "cutoff": chosen["cutoff"] if chosen else None, "precision_target": args.precision,
        "min_actions": args.min_actions, "at_cutoff": chosen, "bootstrap_out_of_sample_precision": boot,
        "replacement_threshold": r_chosen["threshold"] if r_chosen else None,
        "min_replacements": args.min_replacements, "replacement_at_threshold": r_chosen,
        "replacement_bootstrap_out_of_sample_precision": r_boot,
        "labelled_used": len(rows), "labelled_wrong": len(wrong_rows), "excluded": excluded,
        "labels_file": str(vj.LABELS_PATH.relative_to(vj.ROOT)),
        "sample_file": str(SAMPLE_PATH.relative_to(vj.ROOT)), "generated": datetime.now().isoformat(),
        "curve": curve, "replacement_curve": r_curve, "replacement_rows": replacement_rows,
    }
    result["labelled_rows"] = {"act": deployment.standard_rows(rows, "not_incident"),
                               "replace": deployment.standard_rows(replacement_rows, "right", p_key="prob",
                                                                   choice_key="choice")}
    vj.CALIBRATION_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    after_calibration("victim-attribution")
    if chosen:
        print(f"Cut-off {chosen['cutoff']:.4f}: acts on {chosen['rejected_labelled']} labelled pairs, "
              f"{chosen['correct_labelled']} truly wrong; weighted precision {chosen['weighted_precision']:.3f}, "
              f"weighted recall {chosen['weighted_recall']:.3f}, unweighted 95% CI "
              f"{chosen['unweighted_precision_ci95'][0]:.2f}-{chosen['unweighted_precision_ci95'][1]:.2f}")
    else:
        print(f"No cut-off reaches precision {args.precision}: victim attribution stays report-only.")
    print(f"Bootstrap out-of-sample precision: {boot}")
    if r_chosen:
        print(f"Replacement threshold {r_chosen['threshold']:.4f}: {r_chosen['correct_labelled']} of "
              f"{r_chosen['replaced_labelled']} labelled replacements right "
              f"(weighted precision {r_chosen['weighted_precision']:.3f})")
    else:
        print("No replacement threshold reaches the target: wrong sole victims are flagged, not replaced.")
    print(f"Replacement bootstrap: {r_boot}")
    print(f"Used {len(rows)} labelled pairs ({len(wrong_rows)} wrong); excluded {excluded}. "
          f"Written {vj.CALIBRATION_PATH}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="instance/cyber_events.db")
    add_model_arguments(parser)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sample")
    p.add_argument("--census-below", type=float, default=0.5)
    p.add_argument("--sample-mid", type=int, default=40)
    p.add_argument("--sample-high", type=int, default=40)
    p.add_argument("--seed", type=int, default=20261027)
    p.add_argument("--extend-below", type=float, default=None,
                   help="add a census of every unsampled pair scoring below this on current evidence")
    p.add_argument("--extend-stratum", default=None, help="the new stratum's name (with --extend-below)")
    p.add_argument("--frame-db", default=None,
                   help="with --extend-below: only pairs also in this database (the one the sample was drawn on)")
    p.set_defaults(func=cmd_sample)
    p = sub.add_parser("packets")
    p.add_argument("--out", required=True)
    p.add_argument("--unlabelled-only", action="store_true",
                   help="only sampled pairs that have no label yet")
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(func=cmd_packets)
    p = sub.add_parser("calibrate")
    p.add_argument("--precision", type=float, default=0.95)
    p.add_argument("--min-actions", type=int, default=10)
    p.add_argument("--min-replacements", type=int, default=5)
    p.set_defaults(func=cmd_calibrate)
    args = parser.parse_args(argv)
    apply_model_arguments(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
