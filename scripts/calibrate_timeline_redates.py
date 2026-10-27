"""Package timeline date conflicts for blind labelling, and calibrate the re-dating cut-off.

A date conflict is an event that Jev matched to an incident in its
organisation's researched timeline (``timeline-match-v1``) whose stored date
disagrees with the incident's by more than the incident date's precision
allows (:mod:`timeline_reconciliation`). Re-dating it to the incident's date
is right only when the match is right *and* the timeline's date is the
incident's date. Only conflicts where the incident date is more precise than
the stored one (``redate_eligible``) can be acted on, so only they are
calibrated.

``packets``    From ``timeline_reconciliation.json`` (written by
               ``dedup_admin.py reconcile-timelines`` beside the database),
               writes one JSON packet per date conflict to ``--out``: the
               event's records with their date provenance, the matched
               incident's text, date and source - and **no match
               probability**. Labellers decide from the packet plus web search.

``calibrate``  Reads ``evaluation/timeline_redate_labels.csv``
               (``deduplicated_event_id,label,correct_date,rationale,labeller``;
               label ``timeline_date_right``, ``stored_date_right``,
               ``different_incident``, ``neither`` or ``unsure``) and writes
               ``evaluation/timeline_redate_calibration.json``. The cut-off is
               the lowest match probability at which eligible conflicts are at
               least ``--precision`` labelled ``timeline_date_right`` (at least
               ``--min-actions`` of them); a census, so unweighted, with the
               bootstrap out-of-sample estimate of the whole procedure.

Usage (from the project root):
    python scripts/calibrate_timeline_redates.py packets --findings <dir>/timeline_reconciliation.json --out <dir>
    python scripts/calibrate_timeline_redates.py calibrate --findings <dir>/timeline_reconciliation.json
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.dedup import timeline_reconciliation as tr  # noqa: E402
from cyber_data_collector.utils.llm_models import add_model_arguments, apply_model_arguments  # noqa: E402
from scripts.calibrate_duplicates import bootstrap, merge_cutoff  # noqa: E402


def _conflicts(findings: Path) -> List[Dict]:
    data = json.loads(Path(findings).read_text(encoding="utf-8"))
    return [{**c, "organisation": o["organisation"]} for o in data["organisations"] for c in o["date_conflicts"]]


def cmd_packets(args) -> int:
    from cyber_data_collector.dedup.duplicate_judgement import _event_with_dates

    conn = sqlite3.connect(args.db)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    conflicts = _conflicts(args.findings)
    random.Random(args.seed).shuffle(conflicts)
    index = []
    for n, c in enumerate(conflicts, 1):
        name = f"date_packet_{n:03d}.json"
        packet = {"deduplicated_event_id": c["deduplicated_event_id"], "organisation": c["organisation"],
                  "stored_event_date": c["stored_date"], "event": _event_with_dates(conn, c["deduplicated_event_id"]),
                  "matched_timeline_incident": {"date": c["incident_date"], "what": c["incident_text"],
                                                "source": c["incident_source"]}}
        (out / name).write_text(json.dumps(packet, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        index.append({"packet": name, "deduplicated_event_id": c["deduplicated_event_id"],
                      "redate_eligible": c["redate_eligible"]})
    (out / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    print(f"Wrote {len(index)} date-conflict packets (no probabilities) to {out}")
    return 0


def cmd_calibrate(args) -> int:
    labels = tr.load_redate_labels(required=True)
    conflicts = {c["deduplicated_event_id"]: c for c in _conflicts(args.findings)}
    rows = []
    excluded = {"not_eligible": 0, "unsure": 0, "missing": 0}
    for did, c in conflicts.items():
        label = (labels.get(did) or {}).get("label")
        if not c["redate_eligible"]:
            excluded["not_eligible"] += 1
            continue
        if label is None:
            excluded["missing"] += 1
            continue
        if label == "unsure":
            excluded["unsure"] += 1
            continue
        rows.append({"id": did, "p": float(c["match_probability"] or 0.0), "same": label == "timeline_date_right"})
    chosen, curve = merge_cutoff(rows, args.precision, args.min_actions)
    boot = bootstrap(rows, args.precision, args.min_actions)
    result = {**tr.redate_calibration_identity(), "calibrated_on_database": Path(args.db).name,
              "findings_file": Path(args.findings).name,
              "cutoff": chosen["cutoff"] if chosen else None, "precision_target": args.precision,
              "min_actions": args.min_actions, "at_cutoff": chosen, "bootstrap_out_of_sample_precision": boot,
              "labelled_used": len(rows), "labelled_right": sum(r["same"] for r in rows), "excluded": excluded,
              "labels_file": str(tr.REDATE_LABELS_PATH.relative_to(tr.ROOT)),
              "generated": datetime.now().isoformat(), "curve": curve}
    tr.REDATE_CALIBRATION_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if chosen:
        print(f"Cut-off {chosen['cutoff']:.4f}: re-dates {chosen['merged_labelled']} labelled conflicts, "
              f"{chosen['correct_labelled']} right; precision {chosen['precision']:.3f}")
    else:
        print(f"No cut-off reaches precision {args.precision} with {args.min_actions}+ re-dates: "
              "re-dating stays report-only.")
    print(f"Bootstrap: {boot}. Used {len(rows)} eligible labelled conflicts "
          f"({result['labelled_right']} timeline date right); excluded {excluded}. Written {tr.REDATE_CALIBRATION_PATH}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="instance/cyber_events.db")
    add_model_arguments(parser)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("packets")
    p.add_argument("--findings", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--seed", type=int, default=11)
    p.set_defaults(func=cmd_packets)
    p = sub.add_parser("calibrate")
    p.add_argument("--findings", required=True)
    p.add_argument("--precision", type=float, default=0.95)
    p.add_argument("--min-actions", type=int, default=5)
    p.set_defaults(func=cmd_calibrate)
    args = parser.parse_args(argv)
    apply_model_arguments(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
