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
               counted; ``out_of_scope`` (a real incident that is not
               Australian) is excluded too. The model and question version are recorded; the
               pipeline only rejects while both match.

Extending a sample (more labels where the cut-off acts, the same design):

``score``      Scores, on the current evidence readers, every member of a screen band that
               has no final decision on its current evidence (``--stratum B``): Jev, then the
               escalation in its band, then the stored final decision - the calls
               ``recalibrate`` makes. Paid; stored answers are reused. ``--dry-run`` counts.

``sample --extend STRATUM --split-below T``
               Two-phase stratification of one sampled stratum. Every member of its band
               must have a final score (``score`` first, else exit 2). The band is split at
               the final score ``T``: ``<STRATUM>-low`` (final < T) becomes a **census**
               (every member, weight 1) and ``<STRATUM>-high`` keeps the stratum's existing
               sampled members that score at or above ``T``, weighted up to that part
               (an existing simple random sample of the band, restricted to one part, is a
               simple random sample of that part). Other strata are unchanged. The band
               size is checked against the stored weights first, so an extension on
               another database is refused. New rows are marked in the ``added`` column.

``packet``     Writes one JSON labelling packet per sampled event that has no label yet
               (``--out``, not committed): the event, every active record's page text and
               Perplexity research, any stored corroboration search and the victim's
               researched timeline - **no score**, in a seeded random order with no stratum.

Usage (from the project root):
    python scripts/calibrate_event_reality.py sample
    python scripts/calibrate_event_reality.py --db <snapshot copy> score --stratum B
    python scripts/calibrate_event_reality.py --db <snapshot copy> sample --extend B --split-below 0.5
    python scripts/calibrate_event_reality.py --db <snapshot copy> packet --out <dir>
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

from cyber_data_collector.dedup import deployment  # noqa: E402
from scripts.evaluate_calibrations import after_calibration  # noqa: E402
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


def screen_bands(conn: sqlite3.Connection, census_below: float = 0.5) -> Tuple[Dict[str, List[str]], int]:
    """The sampling bands on Jev's stored screen P(real), and how many active events have no screen answer.

    Stratify on the screen (stored evidence only): it exists before any corroboration, so the
    sample - and its weights - do not depend on the question being calibrated.
    """
    active = er.active_events(conn)
    probs = _probabilities(conn, active, version=er.SCREEN_VERSION)
    bands = {
        "A": sorted(d for d, p in probs.items() if p < census_below),
        "B": sorted(d for d, p in probs.items() if census_below <= p < 0.8),
        "C": sorted(d for d, p in probs.items() if p >= 0.8),
    }
    return bands, len(active) - len(probs)


def cmd_sample(args) -> int:
    if args.extend:
        return cmd_extend(args)
    conn = sqlite3.connect(args.db)
    bands, unjudged = screen_bands(conn, args.census_below)
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


SAMPLE_COLUMNS = ["deduplicated_event_id", "stratum", "weight", "added"]


def read_sample(path: Optional[Path] = None) -> List[Dict[str, str]]:
    with Path(path or SAMPLE_PATH).open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def extend_stratum(rows: List[Dict[str, str]], stratum: str, band: List[str], final: Dict[str, float],
                   split_below: float, added: str) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    """Split one sampled stratum at a final score: a census below ``split_below``, the existing sample above.

    ``rows`` is the sample as read; ``band`` every event of the stratum's band; ``final`` a final score for
    every one of them. Returns the new rows (other strata untouched, in their order) and counts. Raises
    ``ValueError`` when the band does not match the stored weights, a member has no score, the stratum was
    split already, or the part at or above the split would have no sampled member to stand for it.
    """
    if any(r["stratum"].startswith(f"{stratum}-") for r in rows):
        raise ValueError(f"stratum {stratum} was split already")
    sampled = [r for r in rows if r["stratum"] == stratum]
    if not sampled:
        raise ValueError(f"the sample has no stratum {stratum}")
    implied = float(sampled[0]["weight"]) * len(sampled)
    if round(implied) != len(band):
        raise ValueError(f"stratum {stratum}: its weights imply a band of {implied:.1f} events but this database "
                         f"has {len(band)}; extend on the database the sample was drawn on")
    members = set(band)
    outside = [r["deduplicated_event_id"] for r in sampled if r["deduplicated_event_id"] not in members]
    if outside:
        raise ValueError(f"stratum {stratum}: {len(outside)} sampled event(s) are not in its band here "
                         f"(first: {outside[0]})")
    unscored = [d for d in band if d not in final]
    if unscored:
        raise ValueError(f"{len(unscored)} event(s) of band {stratum} have no final score on their current "
                         f"evidence (first: {unscored[0]}); run 'score --stratum {stratum}' first")
    low = sorted(d for d in band if final[d] < split_below)
    high = {d for d in band if final[d] >= split_below}
    kept_high = [r for r in sampled if r["deduplicated_event_id"] in high]
    if high and not kept_high:
        raise ValueError(f"no sampled event of {stratum} scores at or above {split_below}: the part above the "
                         "split would have nothing to stand for it")
    already = {r["deduplicated_event_id"]: r for r in sampled}
    out = [dict(r, added=r.get("added") or "") for r in rows if r["stratum"] != stratum]
    out += [{"deduplicated_event_id": d, "stratum": f"{stratum}-low", "weight": "1.0",
             "added": (already[d].get("added") or "") if d in already else added} for d in low]
    w_high = round(len(high) / len(kept_high), 4) if kept_high else 0.0
    out += [{"deduplicated_event_id": r["deduplicated_event_id"], "stratum": f"{stratum}-high",
             "weight": str(w_high), "added": r.get("added") or ""} for r in kept_high]
    counts = {"band": len(band), "low": len(low), "low_new": sum(d not in already for d in low),
              "high": len(high), "high_sampled": len(kept_high)}
    return out, counts


def cmd_extend(args) -> int:
    if args.split_below is None:
        print("--extend needs --split-below", file=sys.stderr)
        return 2
    conn = sqlite3.connect(args.db)
    bands, _ = screen_bands(conn, args.census_below)
    stratum = args.extend
    if stratum not in bands:
        print(f"No band {stratum} (bands: {', '.join(bands)})", file=sys.stderr)
        return 2
    final = er.stored_final_probabilities(conn, bands[stratum])
    added = f"extend {stratum} split<{args.split_below} on {Path(args.db).name}, {datetime.now().date()}"
    try:
        rows, counts = extend_stratum(read_sample(), stratum, bands[stratum], final, args.split_below, added)
    except ValueError as exc:
        print(f"Not extended: {exc}", file=sys.stderr)
        return 2
    with SAMPLE_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SAMPLE_COLUMNS)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in SAMPLE_COLUMNS} for r in rows)
    print(f"Stratum {stratum} ({counts['band']} events) split at final score {args.split_below}: "
          f"{stratum}-low census of {counts['low']} ({counts['low_new']} new), {stratum}-high "
          f"{counts['high_sampled']} sampled of {counts['high']}. Written {SAMPLE_PATH}")
    return 0


def score_events(conn: sqlite3.Connection, ids: List[str], judge=None,
                 client=None) -> Tuple[Dict[str, float], List[str]]:
    """Final scores on the current evidence: Jev, the escalation in its band, the stored decision.

    The same calls ``recalibrate`` makes, on one frozen evidence state per event. Returns the final
    scores and the events that could not be scored (Jev or the escalation failed): those have none.
    """
    states: Dict[str, Dict] = {}
    jev = er.judge_events(conn, ids, judge=judge, version=er.QUESTION_VERSION, states=states)
    failed = [d for d in ids if d not in jev]
    esc_failed: List[str] = []
    escalated = er.escalate_band(conn, jev, client=client, failed=esc_failed, states=states)
    final = er.record_final(conn, jev, escalated, failed=esc_failed, states=states)
    return final, failed + esc_failed


def cmd_score(args) -> int:
    conn = sqlite3.connect(args.db)
    bands, _ = screen_bands(conn, args.census_below)
    if args.stratum not in bands:
        print(f"No band {args.stratum} (bands: {', '.join(bands)})", file=sys.stderr)
        return 2
    band = bands[args.stratum]
    have = er.stored_final_probabilities(conn, band)
    todo = [d for d in band if d not in have]
    print(f"Band {args.stratum}: {len(band)} events, {len(have)} with a final score on current evidence, "
          f"{len(todo)} to score")
    if args.dry_run or not todo:
        return 0
    final, failed = score_events(conn, todo)
    print(f"Scored {len(final)} of {len(todo)}; {len(failed)} failed")
    if failed:
        print(f"Could not score {len(failed)} event(s) (first: {failed[0]}); see the log. Run again.",
              file=sys.stderr)
        return 1
    return 0


LABEL_QUESTION = ("Is this stored event a real, specific cyber incident? Answer real, not_incident, "
                  "out_of_scope (a real incident that did not affect Australia) or unsure, with a "
                  "one-sentence rationale and the corroborating source you relied on.")


def packet(conn: sqlite3.Connection, dedup_id: str) -> Dict:
    """Everything a labeller sees for one event - never a score."""
    row = conn.execute("SELECT description, summary FROM DeduplicatedEvents WHERE deduplicated_event_id = ?",
                       (dedup_id,)).fetchone()
    return {"deduplicated_event_id": dedup_id, "question": LABEL_QUESTION,
            "definition": er.INSTRUCTIONS + er.CORROBORATION_INSTRUCTIONS + er.TIMELINE_INSTRUCTIONS,
            "real_means": er.CRITERIA["true"], "not_incident_means": er.CRITERIA["false"],
            "event_description": row[0] if row else None, "event_summary": row[1] if row else None,
            **er.final_state(conn, dedup_id)}


def cmd_packet(args) -> int:
    conn = sqlite3.connect(args.db)
    labelled = {r["deduplicated_event_id"] for r in csv.DictReader(er.LABELS_PATH.open(encoding="utf-8"))}
    rows = [r for r in read_sample() if r["deduplicated_event_id"] not in labelled]
    active = set(er.active_events(conn))
    absent = [r["deduplicated_event_id"] for r in rows if r["deduplicated_event_id"] not in active]
    if absent:
        print(f"{len(absent)} unlabelled sampled event(s) are not active in {args.db} (first: {absent[0]}); "
              "make the packets on the database the sample was drawn on", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    random.Random(args.seed).shuffle(rows)   # labellers see no stratum order
    index = []
    for n, r in enumerate(rows, 1):
        name = f"packet_{n:03d}.json"
        (out / name).write_text(json.dumps(packet(conn, r["deduplicated_event_id"]), indent=1,
                                           ensure_ascii=False, default=str), encoding="utf-8")
        index.append({"packet": name, "deduplicated_event_id": r["deduplicated_event_id"]})
    (out / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    print(f"Wrote {len(index)} packets (no scores) to {out}; {len(labelled)} event(s) already labelled")
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
               ORDER BY rowid DESC LIMIT 1""",
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
    result["labelled_rows"] = {"reject": deployment.standard_rows(rows, "not_incident")}
    er.CALIBRATION_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    after_calibration("event-reality")
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
    p.add_argument("--extend", metavar="STRATUM",
                   help="Split this sampled stratum at --split-below: a census below, the existing sample above")
    p.add_argument("--split-below", type=float, default=None,
                   help="Final score at which --extend splits the stratum")
    p.set_defaults(func=cmd_sample)
    p = sub.add_parser("score", help="PAID: score a band's events that have no final decision on current evidence")
    p.add_argument("--stratum", required=True)
    p.add_argument("--census-below", type=float, default=0.5)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_score)
    p = sub.add_parser("packet", help="Blind labelling packets for sampled events that have no label yet")
    p.add_argument("--out", required=True)
    p.add_argument("--seed", type=int, default=20261030)
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
