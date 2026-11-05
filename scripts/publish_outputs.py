#!/usr/bin/env python3
"""Publish every output from one retained snapshot, or manage the retained snapshots.

    python scripts/publish_outputs.py                          # snapshot, gate, build, publish together
    python scripts/publish_outputs.py --snapshot-id <id>       # rebuild from a retained snapshot (reproduce)
    python scripts/publish_outputs.py --snapshot-id latest-published
    python scripts/publish_outputs.py --with-analyses          # one snapshot: rerun the analyses, publish once
    python scripts/publish_outputs.py --list                   # retained snapshots, newest first
    python scripts/publish_outputs.py --prune                  # apply the retention policy now

The same step ``run_full_pipeline.py`` runs as its dashboard phase
(``cyber_data_collector/publication/outputs.py``): the quality gate runs on the
snapshot, then ``dashboard/index.html``, ``ai_incidents.html``, ``entities.html``,
``timelines.html``, ``dedup.html``, ``recurrence.html`` and the ``risk_matrix/``
workbooks are built from it and moved into place together, only if the gate
passes. ``dashboard/publication.json`` records the snapshot, the quality run
and a hash of every output.

Before a new snapshot is taken, the stored per-event judgements the outputs show
(AI involvement and the AI incident descriptions; breach source and personal-
information types) are brought up to date
(``cyber_data_collector/publication/judgements.py``): only events whose evidence
changed are asked again. If that fails nothing is published (exit 2);
``--no-refresh-judgements`` publishes the judgements as stored, stale ones marked.

``--with-analyses`` is the step after a correction to the data: judgements are brought up to date, the three
recurrence runs (and the extended analyses) read one new snapshot, and everything is published from that snapshot,
once. Publishing first and analysing afterwards meant publishing twice.

Exit codes: 0 published; 1 blocked by the quality gate; 2 failed (nothing replaced).
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cyber_data_collector.publication import snapshot as snapshots  # noqa: E402
from cyber_data_collector.publication.judgements import JudgementRefreshFailed, refresh_judgements  # noqa: E402
from cyber_data_collector.publication.outputs import PublicationBlocked, PublicationError, publish  # noqa: E402
from cyber_data_collector.utils.config_manager import resolve_db_path  # noqa: E402


def _list(store: snapshots.SnapshotStore) -> int:
    rows = store.manifests()
    if not rows:
        print(f"No snapshots in {store.root}")
        return 0
    print(f"{'snapshot':<34}{'status':<11}{'taken':<21}{'size MB':>8}  copy")
    for m in rows:
        exists = store.copy_path(m["snapshot_id"]).is_file()
        size = (m.get("compressed_bytes") or 0) / 1e6 if exists else 0
        print(f"{m['snapshot_id']:<34}{m.get('status', ''):<11}{m.get('taken_at', ''):<21}{size:>8.0f}  "
              f"{'kept' if exists else 'expired'}")
    print(f"\n{store.disk_bytes() / 1e6:.0f} MB retained in {store.root}")
    return 0


def run_analyses(db_path: Path, analysis_dir: Path, bootstrap: int = 500, date_draws: int = 100) -> str:
    """Run every recurrence analysis on one new snapshot; the snapshot's id. Raises on any failure."""
    script = str(PROJECT_ROOT / "scripts" / "analyze_recurrent_timing.py")
    base = [sys.executable, script, "--db", str(db_path), "--bootstrap", str(bootstrap)]
    main_dir = analysis_dir / "recurrent_timing"
    subprocess.run([*base, "--out-dir", str(main_dir), "--extended", "--date-draws", str(date_draws),
                    "--extended-out-dir", str(analysis_dir / "recurrent_timing_extended")],
                   cwd=PROJECT_ROOT, check=True)
    snapshot_id = json.loads((main_dir / "analysis_inputs.json").read_text(encoding="utf-8"))["snapshot_id"]
    bandings = {"recurrent_timing_90_180_peak": "0,90,180,365,730,1460,inf",
                "recurrent_timing_90d": "0,90,365,730,1460,inf"}
    running = [subprocess.Popen([*base, "--snapshot-id", snapshot_id, "--elapsed-bounds", bounds,
                                 "--out-dir", str(analysis_dir / name)], cwd=PROJECT_ROOT)
               for name, bounds in bandings.items()]
    codes = [p.wait() for p in running]
    if any(codes):
        raise subprocess.CalledProcessError(max(codes), "analyze_recurrent_timing.py (a banding run)")
    return snapshot_id


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", default=None, help="database (default: DATABASE_URL or instance/cyber_events.db)")
    parser.add_argument("--out-dir", default="dashboard", help="where the dashboards go (default: dashboard)")
    parser.add_argument("--risk-matrix-dir", default="risk_matrix", help="where the risk matrices go")
    parser.add_argument("--analysis-dir", default="analysis", help="folder of the recurrence analyses")
    parser.add_argument("--snapshot-id", default=None,
                        help="build from this retained snapshot (an id, or 'latest-published') instead of a new one")
    parser.add_argument("--skip-quality-gate", action="store_true", help="publish without the gate (logged)")
    parser.add_argument("--no-refresh-judgements", action="store_true",
                        help="do not bring the stored AI-involvement and breach-source judgements up to date first "
                             "(stale ones are published marked stale)")
    parser.add_argument("--with-analyses", action="store_true",
                        help="rerun the recurrence analyses on one new snapshot, then publish from it, once")
    parser.add_argument("--list", action="store_true", help="list the retained snapshots and exit")
    parser.add_argument("--prune", action="store_true", help="apply the retention policy and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    db_path = Path(resolve_db_path(args.db_path))
    store = snapshots.store_for(db_path)
    if args.list:
        return _list(store)
    if args.prune:
        expired = store.prune(snapshots.pinned_by_analyses(Path(args.analysis_dir)))
        print(f"Expired {len(expired)} snapshot cop{'y' if len(expired) == 1 else 'ies'}; "
              f"{store.disk_bytes() / 1e6:.0f} MB retained")
        return 0
    if not args.snapshot_id:
        if args.no_refresh_judgements:
            logging.warning("Stored judgements NOT refreshed (--no-refresh-judgements): stale ones are published marked")
        else:
            try:
                refresh_judgements(db_path)
            except JudgementRefreshFailed as exc:
                print(f"FAILED, nothing replaced: {exc}", file=sys.stderr)
                return 2
    if args.with_analyses:
        if args.snapshot_id:
            parser.error("--with-analyses takes its own new snapshot; do not pass --snapshot-id")
        try:
            args.snapshot_id = run_analyses(db_path, Path(args.analysis_dir).resolve())
        except subprocess.CalledProcessError as exc:
            print(f"FAILED, nothing replaced: {exc}", file=sys.stderr)
            return 2
    try:
        result = publish(db_path, Path(args.out_dir), Path(args.risk_matrix_dir),
                         analysis_dir=Path(args.analysis_dir), skip_gate=args.skip_quality_gate,
                         snapshot_id=args.snapshot_id, store=store)
    except PublicationBlocked as exc:
        print(f"BLOCKED (snapshot {exc.snapshot_id} kept): {exc}", file=sys.stderr)
        return 1
    except (PublicationError, snapshots.SnapshotError) as exc:
        print(f"FAILED, nothing replaced: {exc}", file=sys.stderr)
        return 2
    print(f"Published {len(result.files)} file(s) from snapshot {result.snapshot_id} "
          f"(quality gate: {result.gate}; manifest {result.manifest})")
    for warning in result.warnings:
        print(f"  WARNING {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
