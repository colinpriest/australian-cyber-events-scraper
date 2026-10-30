"""Sample, score, package for blind labelling, and calibrate ``fill-victims``.

Each step is reproducible from committed files:

``sample``     Writes ``evaluation/victim_fill_sample.csv``: active events with no usable victim, stratified on
               the stored *final* probability of the best candidate (the reasoning model's where it was
               escalated, else Jev's), so run ``fill-victims`` (report only) - or ``score`` - on the database
               first. Bands ``A`` < 0.5, ``B`` 0.5-0.8, ``C`` 0.8-0.95, ``D`` >= 0.95, each sampled up to its
               quota (a census when smaller), weighted back to its band. Events with no candidate at all cannot
               be filled and are not sampled.

``score``      Asks the choice (stored answers reused, else Jev / the extraction and reasoning models) for every
               event of a frame's population (below), so ``sample`` can stratify it. Exits 1 if any event could
               not be scored.

``packets``    Writes one JSON packet per sampled event (``--frame``: only that frame's) to ``--out`` (not
               committed): the event, every record's page text and research, the candidate organisations the
               evidence offers - and **no score** and no indication of the model's choice. Labellers work from the
               packet plus web search.

``calibrate``  Reads ``evaluation/victim_fill_labels.csv``
               (``deduplicated_event_id,label,correct_victim,rationale,labeller``; label ``named`` with
               ``correct_victim``, ``none_named`` or ``unsure``) and the stored choices, and writes
               ``evaluation/victim_fill_calibration.json``: the **cut-off** is the lowest probability of the
               chosen candidate at which, weighted by stratum, at least ``--precision`` of the events it would
               fill are labelled with that very victim (at least ``--min-fills`` of them). An event labelled
               ``none_named`` that would be filled counts as an error. ``unsure`` labels are excluded and
               counted. A bootstrap of the whole choose-then-test procedure is the out-of-sample estimate. The
               models, question versions and evidence fingerprint are recorded; the pipeline fills only while all
               match.

**Frames: more than one labelling snapshot.** Labels describe the database as it was when they were made, and
filling removes events from the population, so a later snapshot cannot replace an earlier one. A sample can
therefore hold several *frames*, each drawn from its own snapshot (column ``frame``; frame ``1`` is ``--db``):

* frame ``F``'s population is the active events with no usable victim in *its* snapshot that were in no earlier
  frame's population (victimless in that frame's snapshot), so the frames' populations are disjoint;
* frame ``F`` is sampled with the same bands and quotas; its strata are named ``F`` + band (``2D``) and weighted
  back to its own population, so every row's weight is the number of events of the union of the frames' populations
  it stands for, and the weighted precision is an estimate over that union;
* every row is scored and calibrated on its own frame's snapshot (``calibrate --frame-db F=PATH``); a frame with
  labelled rows and no database is an error, never skipped.

Usage (from the project root):
    python scripts/calibrate_victim_fill.py --db <snapshot> sample
    python scripts/calibrate_victim_fill.py --db <snapshot> packets --out <dir>
    python scripts/calibrate_victim_fill.py --db <snapshot> calibrate
    # a later frame, on a later snapshot (earlier frames' snapshots name their populations):
    python scripts/calibrate_victim_fill.py --db <snapshot 2> score --frame 2 --earlier-frame-db 1=<snapshot 1>
    python scripts/calibrate_victim_fill.py --db <snapshot 2> sample --frame 2 --earlier-frame-db 1=<snapshot 1>
    python scripts/calibrate_victim_fill.py --db <snapshot 2> packets --frame 2 --out <dir>
    python scripts/calibrate_victim_fill.py --db <snapshot 1> calibrate --frame-db 2=<snapshot 2>
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
from typing import Dict, List, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.dedup import deployment  # noqa: E402
from scripts.evaluate_calibrations import after_calibration  # noqa: E402
from cyber_data_collector.dedup import victim_fill as vf  # noqa: E402
from cyber_data_collector.utils.llm_models import add_model_arguments, apply_model_arguments  # noqa: E402
from scripts.calibrate_victim_attribution import replacement_bootstrap, replacement_threshold  # noqa: E402
from scripts.calibration_frames import (  # noqa: E402
    PRIMARY_FRAME, earlier_connections, frame_databases, parse_frame_dbs, row_frame, stratum_name)

SAMPLE_PATH = vf.ROOT / "evaluation" / "victim_fill_sample.csv"
BANDS = (("A", 0.0, 0.5), ("B", 0.5, 0.8), ("C", 0.8, 0.95), ("D", 0.95, 1.0001))
QUOTAS = {"A": 20, "B": 40, "C": 40, "D": 60}
SAMPLE_COLUMNS = ["deduplicated_event_id", "stratum", "weight", "frame"]


# ---------------------------------------------------------------- frames

def read_sample(path: Optional[Path] = None) -> List[Dict[str, str]]:
    """The sample rows; a row written before frames existed belongs to frame 1."""
    with Path(path or SAMPLE_PATH).open(encoding="utf-8", newline="") as fh:
        return [{**r, "frame": row_frame(r)} for r in csv.DictReader(fh)]


def write_sample(rows: Sequence[Mapping[str, object]], path: Optional[Path] = None) -> None:
    with Path(path or SAMPLE_PATH).open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(SAMPLE_COLUMNS)
        w.writerows([[r[c] for c in SAMPLE_COLUMNS] for r in rows])


def frame_population(conn: sqlite3.Connection, earlier: Mapping[str, sqlite3.Connection]) -> List[str]:
    """Active events with no usable victim in ``conn`` that were in no earlier frame's population."""
    seen = set()
    for other in earlier.values():
        seen.update(e["deduplicated_event_id"] for e in vf.victimless_events(other))
    return [e["deduplicated_event_id"] for e in vf.victimless_events(conn) if e["deduplicated_event_id"] not in seen]


def stratified_rows(ids: Sequence[str], probs: Mapping[str, float], rng: random.Random,
                    frame: str = PRIMARY_FRAME) -> List[Dict[str, object]]:
    """Rows for one frame: each band sampled up to its quota, weighted back to the band (of this frame)."""
    rows: List[Dict[str, object]] = []
    for name, lo, hi in BANDS:
        band = sorted(i for i in ids if i in probs and lo <= probs[i] < hi)
        chosen = sorted(rng.sample(band, min(QUOTAS[name], len(band))))
        weight = len(band) / len(chosen) if chosen else 0.0
        rows.extend({"deduplicated_event_id": i, "stratum": stratum_name(frame, name), "weight": round(weight, 4),
                     "frame": frame} for i in chosen)
        print(f"Frame {frame} stratum {stratum_name(frame, name)} ({lo}-{hi}): {len(chosen)} of {len(band)} events "
              f"(weight {weight:.2f})")
    return rows


def _earlier_connections(args, existing_frames: Sequence[str]) -> Dict[str, sqlite3.Connection]:
    """Read-only connections to every earlier frame's snapshot; every existing frame must be given."""
    return earlier_connections(args.frame, args.earlier_frame_db, existing_frames)


def _existing_frames(path: Optional[Path] = None) -> List[str]:
    path = Path(path or SAMPLE_PATH)
    return sorted({r["frame"] for r in read_sample(path)}) if path.exists() else []


# ---------------------------------------------------------------- commands

def cmd_sample(args) -> int:
    conn = sqlite3.connect(args.db)
    probs = vf.stored_best_probabilities(conn)
    rng = random.Random(args.seed)
    if args.frame == PRIMARY_FRAME:
        later = [f for f in _existing_frames() if f != PRIMARY_FRAME]
        if later:
            print(f"The sample holds frame(s) {', '.join(later)} drawn after frame 1; redrawing frame 1 would "
                  "change the population they exclude. Refusing.")
            return 2
        events = [e["deduplicated_event_id"] for e in vf.victimless_events(conn)]
        rows = stratified_rows(events, probs, rng)
        write_sample(rows)
    else:
        existing = _existing_frames()
        if not existing:
            print("No sample yet: draw frame 1 first.")
            return 2
        if args.frame in existing:
            print(f"Frame {args.frame} is already in {SAMPLE_PATH.name}; refusing to draw it again.")
            return 2
        events = frame_population(conn, _earlier_connections(args, existing))
        rows = stratified_rows(events, probs, rng, frame=args.frame)
        write_sample([*read_sample(), *rows])
    ids = [i for i in events if i in probs]
    print(f"Seed {args.seed}; {len(events) - len(ids)} of {len(events)} eligible event(s) had no stored choice "
          f"(no candidates, or not yet scored). Written {SAMPLE_PATH}")
    return 0


def cmd_score(args) -> int:
    """Score every event of the frame's population (stored answers reused)."""
    from cyber_data_collector.utils.jev import JevJudge

    conn = sqlite3.connect(args.db)
    existing = [f for f in _existing_frames() if f != args.frame]
    events = (frame_population(conn, _earlier_connections(args, existing)) if args.frame != PRIMARY_FRAME
              else [e["deduplicated_event_id"] for e in vf.victimless_events(conn)])
    judge = JevJudge(conn)
    errors, scored, empty, escalated = [], 0, 0, 0
    try:
        for n, did in enumerate(events, 1):
            try:
                choice = vf.choose_victim(conn, did, judge)
            except Exception as exc:      # noqa: BLE001 - every failure is listed and fails the command
                errors.append((did, f"{type(exc).__name__}: {exc}"))
                continue
            if not choice["candidates"]:
                empty += 1
            else:
                scored += 1
                escalated += int(choice["escalated"])
            if n % 10 == 0:
                print(f"  {n}/{len(events)}")
    finally:
        judge.close()
    print(f"Frame {args.frame}: {len(events)} event(s); {scored} scored ({escalated} escalated), {empty} with no "
          f"candidate, {len(errors)} failed; Jev requests made: {judge.calls}")
    for did, error in errors:
        print(f"  FAILED {did}: {error}")
    return 1 if errors else 0


def packet(conn: sqlite3.Connection, dedup_id: str) -> Dict:
    """Everything a labeller sees for one event - never a score."""
    from cyber_data_collector.dedup import event_reality, event_organisations
    from cyber_data_collector.dedup import victim_judgement as vj

    state = event_reality.build_state(conn, dedup_id, corroborated=True)
    extracted = event_organisations.named_organisations(conn, dedup_id, stored_only=True)
    state["candidate_organisations_from_evidence"] = [
        c["name"] for c in vj.replacement_candidates(conn, dedup_id, [], organisations=extracted, include_scalar=True)]
    return {"deduplicated_event_id": dedup_id, **state}


def cmd_packets(args) -> int:
    conn = sqlite3.connect(args.db)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = [r for r in read_sample() if args.frame is None or r["frame"] == args.frame]
    if not rows:
        print(f"No sampled rows for frame {args.frame}.")
        return 2
    random.Random(args.seed).shuffle(rows)   # labellers see no stratum order either
    index = []
    for n, r in enumerate(rows, 1):
        name = f"packet_{n:03d}.json"
        (out / name).write_text(json.dumps(packet(conn, r["deduplicated_event_id"]), indent=1, ensure_ascii=False,
                                           default=str), encoding="utf-8")
        index.append({"packet": name, "deduplicated_event_id": r["deduplicated_event_id"]})
    (out / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    print(f"Wrote {len(index)} packets (no scores) to {out}")
    return 0


def cmd_calibrate(args) -> int:
    from cyber_data_collector.utils.jev import JevJudge

    sample = {r["deduplicated_event_id"]: r for r in read_sample()}
    labels = vf.load_labels(required=True)
    usable = {did: s for did, s in sample.items() if (labels.get(did) or {}).get("label") in ("named", "none_named")}
    try:
        paths = frame_databases(args.db, parse_frame_dbs(args.frame_db), sorted({s["frame"] for s in usable.values()}))
    except ValueError as exc:
        print(f"Cannot calibrate: {exc}")
        return 2
    excluded = {"unsure": 0, "missing": 0}
    rows = []
    for frame, path in sorted(paths.items()):
        conn = sqlite3.connect(path)
        judge = JevJudge(conn)
        try:
            for did, s in sample.items():
                if s["frame"] != frame:
                    continue
                label = labels.get(did) or {}
                kind = label.get("label")
                if kind == "unsure":
                    excluded["unsure"] += 1
                    continue
                if kind not in ("named", "none_named"):
                    excluded["missing"] += 1
                    continue
                choice = vf.choose_victim(conn, did, judge)     # stored answers are reused
                wanted = (label.get("correct_victim") or "").strip()
                right = kind == "named" and vf.names_match(conn, choice["name"], wanted)
                rows.append({"id": did, "frame": frame, "stratum": s["stratum"], "weight": float(s["weight"]),
                             "choice": choice["name"], "prob": choice["probability"] or 0.0, "right": bool(right),
                             "label": kind, "labelled_victim": wanted or None,
                             "candidates": [c["name"] for c in choice["candidates"]]})
        finally:
            judge.close()
            conn.close()
    # Rows of frames with no usable label are counted too (their snapshot was not needed).
    excluded["missing"] += sum(1 for did, s in sample.items() if s["frame"] not in paths
                               and (labels.get(did) or {}).get("label") != "unsure")
    excluded["unsure"] += sum(1 for did, s in sample.items() if s["frame"] not in paths
                              and (labels.get(did) or {}).get("label") == "unsure")

    chosen, curve = replacement_threshold(rows, args.precision, args.min_fills)
    boot = replacement_bootstrap(rows, args.precision, args.min_fills)
    named = [r for r in rows if r["label"] == "named"]
    result = {
        **vf.calibration_identity(), "settled_at": vf.SETTLED_AT,
        "calibrated_on_database": Path(args.db).name,
        "frame_databases": {f: Path(p).name for f, p in sorted(paths.items())},
        "cutoff": chosen["threshold"] if chosen else None, "precision_target": args.precision,
        "min_fills": args.min_fills, "at_cutoff": chosen, "bootstrap_out_of_sample_precision": boot,
        "labelled_used": len(rows), "labelled_named": len(named), "excluded": excluded,
        "recall_labelled_named": (sum(1 for r in named if r["choice"] and r["prob"] >= chosen["threshold"]) / len(named)
                                  if chosen and named else None),
        "labels_file": str(vf.LABELS_PATH.relative_to(vf.ROOT)), "sample_file": str(SAMPLE_PATH.relative_to(vf.ROOT)),
        "generated": datetime.now().isoformat(), "curve": curve, "rows": rows,
    }
    result["labelled_rows"] = {"fill": deployment.standard_rows(rows, "right", p_key="prob", choice_key="choice")}
    vf.CALIBRATION_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    after_calibration("victim-fill")
    if chosen:
        print(f"Cut-off {chosen['threshold']:.4f}: fills {chosen['replaced_labelled']} labelled events, "
              f"{chosen['correct_labelled']} with the labelled victim; weighted precision "
              f"{chosen['weighted_precision']:.3f}")
    else:
        print(f"No cut-off reaches precision {args.precision}: fill-victims stays report-only.")
    print(f"Bootstrap out-of-sample precision: {boot}")
    print(f"Used {len(rows)} labelled events ({len(named)} named) from frame(s) {', '.join(sorted(paths))}; "
          f"excluded {excluded}. Written {vf.CALIBRATION_PATH}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="instance/cyber_events.db")
    add_model_arguments(parser)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sample")
    p.add_argument("--seed", type=int, default=20261029)
    p.add_argument("--frame", default=PRIMARY_FRAME, help="the frame to draw (1 = the whole sample, redrawn)")
    p.add_argument("--earlier-frame-db", action="append", metavar="FRAME=PATH",
                   help="snapshot of a frame already in the sample (every one is required for a later frame)")
    p.set_defaults(func=cmd_sample)
    p = sub.add_parser("score")
    p.add_argument("--frame", default=PRIMARY_FRAME)
    p.add_argument("--earlier-frame-db", action="append", metavar="FRAME=PATH")
    p.set_defaults(func=cmd_score)
    p = sub.add_parser("packets")
    p.add_argument("--out", required=True)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--frame", default=None, help="only this frame's rows (default: every row)")
    p.set_defaults(func=cmd_packets)
    p = sub.add_parser("calibrate")
    p.add_argument("--precision", type=float, default=0.95)
    p.add_argument("--min-fills", type=int, default=10)
    p.add_argument("--frame-db", action="append", metavar="FRAME=PATH",
                   help="snapshot of a frame other than 1 (frame 1 is --db); required for every labelled frame")
    p.set_defaults(func=cmd_calibrate)
    args = parser.parse_args(argv)
    apply_model_arguments(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
