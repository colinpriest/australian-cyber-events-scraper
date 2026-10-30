"""Sample, package for blind labelling, and calibrate the same-organisation coherence check.

Steps, each reproducible from committed files (see ``cyber_data_collector/dedup/coherence_same_org.py``):

``sample``     Writes ``evaluation/coherence_same_org_sample.csv`` - (event, member)
               pairs from active multi-member events. Candidates (members naming the event's
               own organisation, but with a verified date far from the event's or a different
               incident in the organisation's timeline) are stratified on the *stored final
               score* (``check-same-organisation`` or ``score`` must have judged them first). Labellers
               are never shown the score; stratifying on it only decides who is labelled, and the
               weights undo it:
               * Stratum ``A`` - every candidate scoring below ``--census-below`` (0.5): a
                 census, weight 1, because any cut-off lies here.
               * Stratum ``B`` - a seeded sample of those scoring up to ``--mid-below`` (0.8); ``C`` - at
                 or above it. Weighted up to their bands; they measure recall.
               * Stratum ``N`` - a seeded sample of the other same-organisation members
                 (not candidates), weighted up to its population. It measures the incidents
                 the candidate selection keeps (reported, never acted on).
               Only candidates can be split by the model, so any cut-off acts on A-C.
``score``      Asks Jev (and the escalation model in its band) about every candidate of a frame's
               population (below), so ``sample`` can stratify it. Answers are stored and reused.
``packet``     Writes a blind labelling packet per sampled pair (JSON lines): the event, its
               records and the record under review - no score, no stratum, no candidate reasons.
``judge``      Asks Jev (and the escalation model in its band) about every sampled candidate of a frame.
               Answers are stored; re-running reuses them.
``calibrate``  Reads ``evaluation/coherence_same_org_labels.csv``
               (``deduplicated_event_id,enriched_event_id,label,rationale,incident,labeller`` with
               label ``same`` - the record reports the event's incident - ``different`` - a separate
               incident at the same organisation - or ``unsure``) and the stored final scores, and
               writes ``evaluation/coherence_same_org_calibration.json``: the largest cut-off at which
               the candidate-stratum members it would split are, weighted, at least ``--precision`` (0.95)
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

**Frames: more than one labelling snapshot** (``scripts/calibration_frames.py``, as in
``scripts/calibrate_victim_fill.py``). Splitting removes the separate incidents the cut-off is measured on, and a
census of one snapshot's acting band can be used up, so the sample can hold later frames (column ``frame``;
frame ``1`` is ``--db``; rows written before frames are frame 1):

* frame ``F``'s candidates are the candidates of *its* snapshot whose subject (event, member) was a candidate in no
  earlier frame's snapshot, so the frames' candidate populations are disjoint; its other same-organisation members
  (stratum ``N``) are those that were in no earlier frame's population at all (candidate or not);
* ``score --frame F`` judges every candidate of that population so ``sample --frame F`` can stratify it; its strata
  are ``F`` + band (``2A``) with the band bounds and quotas given, weighted back to its own population;
* every row is judged and calibrated on its own frame's snapshot (``judge --frame F`` on that snapshot,
  ``calibrate --frame-db F=PATH``); a labelled frame without its database is an error, never skipped, and
  ``dedup_admin.py recalibrate`` refuses a multi-frame sample rather than drop a frame.

Usage (from the project root):
    python scripts/calibrate_coherence_same_org.py --db <snapshot> sample
    python scripts/calibrate_coherence_same_org.py --db <snapshot> packet --out <dir>
    python scripts/calibrate_coherence_same_org.py --db <snapshot> judge
    python scripts/calibrate_coherence_same_org.py --db <snapshot> calibrate --precision 0.95
    # a later frame, on a later snapshot (earlier frames' snapshots name their populations):
    python scripts/calibrate_coherence_same_org.py --db <snapshot 2> score --frame 2 --earlier-frame-db 1=<snapshot 1>
    python scripts/calibrate_coherence_same_org.py --db <snapshot 2> sample --frame 2 --earlier-frame-db 1=<snapshot 1>
    python scripts/calibrate_coherence_same_org.py --db <snapshot 2> packet --frame 2 --out <dir>
    python scripts/calibrate_coherence_same_org.py --db <snapshot 2> judge --frame 2
    python scripts/calibrate_coherence_same_org.py --db <snapshot 1> calibrate --frame-db 2=<snapshot 2>
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
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.dedup import deployment  # noqa: E402
from scripts.evaluate_calibrations import after_calibration  # noqa: E402
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
from scripts.calibration_frames import (  # noqa: E402
    PRIMARY_FRAME, earlier_connections, frame_databases, parse_frame_dbs, row_frame, stratum_name)

SAMPLE_PATH = so.ROOT / "evaluation" / "coherence_same_org_sample.csv"
SAMPLE_COLUMNS = ["deduplicated_event_id", "enriched_event_id", "stratum", "weight", "frame"]
CANDIDATE_BANDS = ("A", "B", "C")
OTHER_BAND = "N"

Pair = Tuple[str, str]   # (deduplicated event id, enriched member id)


def _events(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """Every active multi-member event with its candidate members."""
    resolver = EntityResolver(conn)
    all_ids = [e[0] for e in co._multi_member_events(conn)]
    return {e["id"]: e for e in so.find_candidate_events(conn, resolver, include=all_ids)}


def is_candidate_stratum(stratum: str) -> bool:
    """A, B, C (frame 1) or 2A, 2B, ... (a later frame): the strata the model can split."""
    return stratum.lstrip("0123456789") in CANDIDATE_BANDS


def population(events: Mapping[str, Dict[str, Any]]) -> Tuple[List[Pair], List[Pair]]:
    """(candidates, other same-organisation members) of one snapshot."""
    candidates: List[Pair] = []
    others: List[Pair] = []
    for did, e in sorted(events.items()):
        for m in e["members"]:
            if m["id"] in e["suspect_ids"]:
                continue          # other-organisation members belong to the stray check
            (candidates if m["id"] in e["candidates"] else others).append((did, m["id"]))
    return candidates, others


def _subjects(pairs: Sequence[Pair]) -> Set[str]:
    return {co.member_subject_id(d, m) for d, m in pairs}


def frame_population(events: Mapping[str, Dict[str, Any]], earlier: Sequence[Mapping[str, Dict[str, Any]]]
                     ) -> Tuple[List[Pair], List[Pair]]:
    """A frame's (candidates, others): its snapshot's, less what earlier frames' snapshots already held.

    A candidate is dropped when its subject was a candidate in an earlier frame (the candidate populations are
    disjoint); an other member when its subject was in an earlier frame's population at all.
    """
    seen_candidates: Set[str] = set()
    seen_any: Set[str] = set()
    for other in earlier:
        c, o = population(other)
        seen_candidates |= _subjects(c)
        seen_any |= _subjects(c) | _subjects(o)
    candidates, others = population(events)
    return ([p for p in candidates if co.member_subject_id(*p) not in seen_candidates],
            [p for p in others if co.member_subject_id(*p) not in seen_any])


def read_sample(path: Optional[Path] = None) -> List[Dict[str, str]]:
    """The sample rows; a row written before frames existed belongs to frame 1."""
    with Path(path or SAMPLE_PATH).open(encoding="utf-8", newline="") as fh:
        return [{**r, "frame": row_frame(r)} for r in csv.DictReader(fh)]


def write_sample(rows: Sequence[Mapping[str, object]], path: Optional[Path] = None) -> None:
    path = Path(path or SAMPLE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(SAMPLE_COLUMNS)
        w.writerows([[r[c] for c in SAMPLE_COLUMNS] for r in rows])


def _existing_frames() -> List[str]:
    return sorted({r["frame"] for r in read_sample()}) if SAMPLE_PATH.exists() else []


def _earlier_events(args, existing: Sequence[str]) -> List[Dict[str, Dict[str, Any]]]:
    """The candidate events of every earlier frame's snapshot (each one required)."""
    conns = earlier_connections(args.frame, args.earlier_frame_db, existing)
    try:
        return [_events(c) for _, c in sorted(conns.items())]
    finally:
        for c in conns.values():
            c.close()


def stratified_rows(candidates: Sequence[Pair], others: Sequence[Pair], probs: Mapping[str, float],
                    args, frame: str) -> Tuple[List[Dict[str, object]], int]:
    """One frame's rows: a census below ``--census-below``, seeded samples above it and of the other members."""
    bands: Dict[str, List[Pair]] = {"A": [], "B": [], "C": [], OTHER_BAND: list(others)}
    unjudged = 0
    for did, mid in candidates:
        p = probs.get(co.member_subject_id(did, mid))
        if p is None:
            unjudged += 1
            continue
        bands["A" if p < args.census_below else "B" if p < args.mid_below else "C"].append((did, mid))
    rng = random.Random(args.seed)
    quotas = {"A": None, "B": args.sample_mid, "C": args.sample_high, OTHER_BAND: args.sample_other}
    rows: List[Dict[str, object]] = []
    for band, quota in quotas.items():
        pool = sorted(bands[band])
        chosen = pool if quota is None else sorted(rng.sample(pool, min(quota, len(pool))))
        weight = len(pool) / len(chosen) if chosen else 0.0
        name = stratum_name(frame, band)
        rows.extend({"deduplicated_event_id": d, "enriched_event_id": m, "stratum": name,
                     "weight": round(weight, 4), "frame": frame} for d, m in chosen)
        print(f"Frame {frame} stratum {name}: {len(chosen)} of {len(pool)} members (weight {weight:.2f})")
    return rows, unjudged


def _frame_candidates(args, events: Mapping[str, Dict[str, Any]], existing: Sequence[str]
                      ) -> Tuple[List[Pair], List[Pair]]:
    if args.frame == PRIMARY_FRAME:
        return population(events)
    return frame_population(events, _earlier_events(args, existing))


def cmd_sample(args) -> int:
    existing = _existing_frames()
    if args.frame == PRIMARY_FRAME:
        later = [f for f in existing if f != PRIMARY_FRAME]
        if later:
            print(f"The sample holds frame(s) {', '.join(later)} drawn after frame 1; redrawing frame 1 would "
                  "change the population they exclude. Refusing.")
            return 2
    else:
        if not existing:
            print("No sample yet: draw frame 1 first.")
            return 2
        if args.frame in existing:
            print(f"Frame {args.frame} is already in {SAMPLE_PATH.name}; refusing to draw it again.")
            return 2
    conn = sqlite3.connect(f"file:{Path(args.db).as_posix()}?mode=ro", uri=True)
    events = _events(conn)
    candidates, others = _frame_candidates(args, events, existing)
    probs = so.stored_final_probabilities(conn, [(events[d], m) for d, m in candidates])
    rows, unjudged = stratified_rows(candidates, others, probs, args, args.frame)
    write_sample(rows if args.frame == PRIMARY_FRAME else [*read_sample(), *rows])
    print(f"Seed {args.seed}; snapshot {Path(args.db).name}; {unjudged} candidate(s) had no stored final "
          f"score (run `score --frame {args.frame}` or check-same-organisation on the snapshot first). "
          f"Written {SAMPLE_PATH}")
    return 1 if unjudged else 0


def cmd_score(args) -> int:
    """Judge every candidate of the frame's population (stored answers reused) so ``sample`` can stratify it."""
    existing = [f for f in _existing_frames() if f != args.frame]
    conn = sqlite3.connect(args.db)
    events = _events(conn)
    candidates, _ = _frame_candidates(args, events, existing)
    scores = so.judge_members(conn, [(events[d], m) for d, m in candidates])
    undecided = sorted(s for s, v in scores.items() if v.get("error"))
    print(f"Frame {args.frame}: {len(candidates)} candidate(s); {len(scores) - len(undecided)} scored "
          f"({sum(1 for v in scores.values() if v['p_escalated'] is not None)} escalated), "
          f"{len(undecided)} undecided")
    for sid in undecided:
        print(f"  UNDECIDED {sid}: {scores[sid]['error']}")
    return 1 if undecided else 0


def _sample(frame: Optional[str] = None) -> List[Dict[str, str]]:
    return [r for r in read_sample() if frame is None or r["frame"] == frame]


def packet_item(conn: sqlite3.Connection, event: Dict[str, Any], enriched_id: str) -> Dict[str, Any]:
    """One blind item: the event, its other records and the record under review - never a score or reason."""
    state = so.build_member_state(conn, event, enriched_id)
    state.pop("flagged_because", None)
    state["other_records"] = [
        {"enriched_event_id": m["id"], "title": m["title"], "date": m["date"],
         "organisation": m["org"], "url": m["url"], "summary": (m["body"] or "")[:400]}
        for m in event["members"] if m["id"] != enriched_id]
    return {"deduplicated_event_id": event["id"], "enriched_event_id": enriched_id, **state}


def cmd_packet(args) -> int:
    """Blind packets: what a labeller needs, and nothing about any score, stratum or candidate reason."""
    rows = sorted(_sample(args.frame), key=lambda r: (r["deduplicated_event_id"], r["enriched_event_id"]))
    if not rows:
        print(f"No sampled rows for frame {args.frame}.")
        return 2
    conn = sqlite3.connect(f"file:{Path(args.db).as_posix()}?mode=ro", uri=True)
    events = _events(conn)
    absent = [r for r in rows if r["deduplicated_event_id"] not in events or r["enriched_event_id"] not in
              {m["id"] for m in events[r["deduplicated_event_id"]]["members"]}]
    if absent:
        print(f"{len(absent)} sampled row(s) are not members of their event in {Path(args.db).name} (first: "
              f"{absent[0]['deduplicated_event_id']}|{absent[0]['enriched_event_id']}, frame(s) "
              f"{', '.join(sorted({r['frame'] for r in absent}))}): "
              "give each frame's packet its own snapshot (--frame). Nothing written.")
        return 2
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    rng.shuffle(rows)   # strata follow the selection; the order must not
    chunks = [rows[i::args.parts] for i in range(args.parts)] if args.parts > 1 else [rows]
    for n, chunk in enumerate(chunks, 1):
        path = out_dir / f"coherence_same_org_packet_{n}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for r in chunk:
                item = packet_item(conn, events[r["deduplicated_event_id"]], r["enriched_event_id"])
                fh.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        print(f"Written {len(chunk)} packet item(s) to {path}")
    return 0


def cmd_judge(args) -> int:
    conn = sqlite3.connect(args.db)
    events = _events(conn)
    rows = [r for r in _sample(args.frame) if is_candidate_stratum(r["stratum"])]
    items = [(events[r["deduplicated_event_id"]], r["enriched_event_id"]) for r in rows
             if r["deduplicated_event_id"] in events
             and r["enriched_event_id"] in events[r["deduplicated_event_id"]]["candidates"]]
    scores = so.judge_members(conn, items)
    undecided = sorted(s for s, v in scores.items() if v.get("error"))
    print(f"Frame {args.frame}: judged {len(scores)} of {len(rows)} sampled candidate(s) "
          f"({len(rows) - len(items)} not candidates in {Path(args.db).name}); "
          f"{sum(1 for s in scores.values() if s['p_escalated'] is not None)} escalated, {len(undecided)} undecided")
    return 1 if undecided else 0


def cmd_calibrate(args) -> int:
    sample = {co.member_subject_id(r["deduplicated_event_id"], r["enriched_event_id"]): r for r in read_sample()}
    labels = co.load_labels(so.LABELS_PATH)
    usable = {sid: s for sid, s in sample.items() if labels.get(sid) in ("same", "different")}
    try:
        paths = frame_databases(args.db, parse_frame_dbs(args.frame_db), sorted({s["frame"] for s in usable.values()}))
    except ValueError as exc:
        print(f"Cannot calibrate: {exc}")
        return 2

    rows: List[Dict[str, Any]] = []
    other: List[Dict[str, Any]] = []
    unsure = sum(1 for sid in sample if labels.get(sid) == "unsure")
    missing = sum(1 for sid in sample if labels.get(sid) is None)
    for frame, path in sorted(paths.items()):
        mine = {sid: s for sid, s in usable.items() if s["frame"] == frame}
        conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
        try:
            events = _events(conn)
            probs = so.stored_final_probabilities(
                conn, [(events[s["deduplicated_event_id"]], s["enriched_event_id"]) for s in mine.values()
                       if s["deduplicated_event_id"] in events and s["enriched_event_id"] in
                       {m["id"] for m in events[s["deduplicated_event_id"]]["members"]}])
        finally:
            conn.close()
        for sid, s in mine.items():
            row = {"id": sid, "frame": frame, "stratum": s["stratum"], "weight": float(s["weight"]),
                   "p": probs.get(sid), "not_incident": labels[sid] == "different"}
            if is_candidate_stratum(s["stratum"]):
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
        "frame_databases": {f: Path(p).name for f, p in sorted(paths.items())},
        "cutoff": chosen["cutoff"] if chosen else None,
        "precision_target": args.precision, "min_splits": args.min_splits,
        "at_cutoff": chosen, "labelled_used": len(rows),
        "labelled_different": sum(r["not_incident"] for r in rows),
        "labelled_used_by_frame": {f: sum(1 for r in rows if r["frame"] == f) for f in sorted(paths)},
        "unsure_excluded": unsure, "missing": missing,
        "labels_file": so.LABELS_PATH.relative_to(so.ROOT).as_posix(),
        "sample_file": SAMPLE_PATH.relative_to(so.ROOT).as_posix(), "generated": datetime.now().isoformat(),
        "bootstrap_out_of_sample_precision": boot, "candidate_selection_misses": keeps,
        "curve": [{"cutoff": t["cutoff"], "split_labelled": t["rejected_labelled"],
                   "correct_labelled": t["correct_labelled"], "precision": t["weighted_precision"],
                   "recall": t["weighted_recall"], "precision_ci95": t["unweighted_precision_ci95"]}
                  for t in table],
    }
    result["labelled_rows"] = {"split": deployment.standard_rows(rows, "not_incident")}
    so.CALIBRATION_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    after_calibration("coherence-same-organisation")
    if chosen:
        print(f"Cut-off {chosen['cutoff']:.4f}: splits {chosen['rejected_labelled']} labelled candidates, "
              f"{chosen['correct_labelled']} truly separate incidents; precision {chosen['weighted_precision']:.3f}, "
              f"recall {chosen['weighted_recall']:.3f}")
    else:
        print(f"No cut-off reaches precision {args.precision}: model-judged splits stay report-only.")
    print(f"Bootstrap out-of-sample precision: {boot}")
    print(f"Candidate selection: {n_diff} of {len(other)} labelled non-candidates are separate incidents "
          f"(weighted share {keeps['weighted_share_different']})")
    print(f"Used {len(rows)} labelled candidates ({result['labelled_different']} different) from frame(s) "
          f"{', '.join(sorted(paths))}; {unsure} unsure excluded, {missing} missing. Written {so.CALIBRATION_PATH}")
    return 0


def plan(conn: sqlite3.Connection, module) -> "object":
    """The recalibration plan (``dedup/recalibration.py``): labelled candidate-stratum members the snapshot holds."""
    from cyber_data_collector.dedup.recalibration import (
        Plan, PrerequisiteMissing, RecalibrationError, _require_agreement)

    labels = co.load_labels(module.LABELS_PATH)
    labelled = [r for r in read_sample()
                if labels.get(co.member_subject_id(r["deduplicated_event_id"], r["enriched_event_id"]))
                in ("same", "different")]
    later = sorted({r["frame"] for r in labelled} - {PRIMARY_FRAME})
    if later:
        # Each frame was labelled on its own snapshot; re-scoring frame 1 alone would silently drop the others.
        raise PrerequisiteMissing(
            f"coherence-same-organisation's labelled sample spans frame(s) {', '.join(later)} besides frame 1, "
            "each on its own snapshot; recalibrate it with scripts/calibrate_coherence_same_org.py (judge --frame F "
            "on each frame's snapshot copy, then calibrate --frame-db), not here")
    sample = [r for r in labelled if is_candidate_stratum(r["stratum"])]
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
        _require_agreement("member", {s: v["p"] for s, v in scores.items()}, module.stored_final_probabilities(conn, items))

    return Plan(items, len(sample), run)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="instance/cyber_events.db")
    add_model_arguments(parser)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sample")
    p.add_argument("--census-below", type=float, default=0.5,
                   help="every candidate scoring below this is labelled (stratum A)")
    p.add_argument("--mid-below", type=float, default=0.8, help="upper bound of stratum B (C is at or above it)")
    p.add_argument("--sample-mid", type=int, default=25)
    p.add_argument("--sample-high", type=int, default=25)
    p.add_argument("--sample-other", type=int, default=40)
    p.add_argument("--seed", type=int, default=20261029)
    p.add_argument("--frame", default=PRIMARY_FRAME, help="the frame to draw (1 = the whole sample, redrawn)")
    p.add_argument("--earlier-frame-db", action="append", metavar="FRAME=PATH",
                   help="snapshot of a frame already in the sample (every one is required for a later frame)")
    p.set_defaults(func=cmd_sample)
    p = sub.add_parser("score", help="judge every candidate of a frame's population (stored answers reused)")
    p.add_argument("--frame", default=PRIMARY_FRAME)
    p.add_argument("--earlier-frame-db", action="append", metavar="FRAME=PATH")
    p.set_defaults(func=cmd_score)
    p = sub.add_parser("packet")
    p.add_argument("--out", required=True)
    p.add_argument("--parts", type=int, default=1, help="Split the packet into this many files.")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--frame", default=None, help="only this frame's rows (default: every row, one snapshot)")
    p.set_defaults(func=cmd_packet)
    p = sub.add_parser("judge")
    p.add_argument("--frame", default=PRIMARY_FRAME, help="the frame whose sampled candidates --db holds")
    p.set_defaults(func=cmd_judge)
    p = sub.add_parser("calibrate")
    p.add_argument("--precision", type=float, default=0.95)
    p.add_argument("--min-splits", type=int, default=5,
                   help="A cut-off must split at least this many labelled candidates to count.")
    p.add_argument("--frame-db", action="append", metavar="FRAME=PATH",
                   help="snapshot of a frame other than 1 (frame 1 is --db); required for every labelled frame")
    p.set_defaults(func=cmd_calibrate)
    args = parser.parse_args(argv)
    apply_model_arguments(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
