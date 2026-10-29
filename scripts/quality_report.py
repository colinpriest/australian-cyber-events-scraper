"""Measure data quality, record the run, and decide whether outputs may be published.

Run from the project root (the OAIC integrity checks read
``oaic_cyber_statistics_*.json`` from the working directory):

    python scripts/quality_report.py                       # measure, record, write reports
    python scripts/quality_report.py --no-record           # measure only; the database is opened read-only
    python scripts/quality_report.py --db COPY.db --out-dir some/dir
    python scripts/quality_report.py --snapshot-id latest-published   # measure a retained snapshot

With --snapshot-id the measured database is a retained snapshot (restored to a temporary
file; see cyber_data_collector/publication/snapshot.py): the report names its id and the
run is recorded, with the id, in the live database. Without it the report says it measured
the live database directly; the pipeline's publication step always measures a snapshot.

Measurement always reads through a read-only connection. Unless ``--no-record``
is given, the run is then stored in ``QualityRuns`` / ``QualityMetrics`` through
a separate connection, so the history shows every run.

Writes ``quality_report.json``, ``quality_report.md`` and ``quality_report.html``
to ``--out-dir`` (default ``instance/``).

Exit codes: 0 publication allowed; 1 blocked (a blocking check failed or could
not be measured), or the database could not be opened, or the run could not be
recorded.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cyber_data_collector.quality.gate import (  # noqa: E402
    QualityStoreError, run_quality_checks, store_report, write_outputs,
)
from cyber_data_collector.quality.model import FAIL, NOT_MEASURED, WARN, load_thresholds  # noqa: E402
from cyber_data_collector.utils.config_manager import resolve_db_path  # noqa: E402

logger = logging.getLogger("quality_report")


def read_only(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=None, help="database (default: DATABASE_URL or instance/cyber_events.db)")
    parser.add_argument("--out-dir", default="instance", help="where to write the reports (default: instance)")
    parser.add_argument("--thresholds", default=None, help="thresholds file (default: the committed one)")
    parser.add_argument("--no-record", action="store_true", help="do not store the run in the database")
    parser.add_argument("--snapshot-id", default=None,
                        help="measure this retained snapshot (an id, or 'latest-published') instead of the live database")
    parser.add_argument("--quiet", action="store_true", help="print only the verdict and blocking reasons")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    db_path = resolve_db_path(args.db)
    if not Path(db_path).exists():
        print(f"BLOCKED: database not found: {db_path}")
        return 1
    thresholds = load_thresholds(Path(args.thresholds) if args.thresholds else None)
    import tempfile

    from cyber_data_collector.publication import snapshot as snapshots

    with tempfile.TemporaryDirectory(prefix="quality-snapshot-") as work:
        snap = None
        measured = db_path
        if args.snapshot_id:
            store = snapshots.store_for(Path(db_path))
            try:
                sid = store.resolve(args.snapshot_id)
                measured = str(store.restore(sid, Path(work)))
            except snapshots.SnapshotError as exc:
                print(f"BLOCKED: {exc}")
                return 1
            manifest = store.manifest(sid)
            snap = snapshots.Snapshot(sid, manifest["sha256"], manifest["taken_at"], manifest["source_database"],
                                      Path(measured), 0)
        conn = read_only(measured)
        try:
            report = run_quality_checks(conn, thresholds, snapshot=snap, db_path=db_path)
        finally:
            conn.close()

    paths = write_outputs(report, Path(args.out_dir))
    status = 0 if report.allowed else 1
    if not args.no_record:
        writer = sqlite3.connect(db_path)
        try:
            store_report(writer, report)
        except QualityStoreError as exc:
            print(f"ERROR: {exc}")
            status = 1
        finally:
            writer.close()

    print(f"Publication {'ALLOWED' if report.allowed else 'BLOCKED'} - run {report.run_id} "
          f"({report.count(FAIL)} failed, {report.count(NOT_MEASURED)} not measured, {report.count(WARN)} warnings)")
    for reason in report.reasons:
        print(f"  BLOCKING  {reason}")
    if not args.quiet:
        for m in report.metrics:
            if m.status in (FAIL, NOT_MEASURED, WARN) and not (m.blocking and m.status in (FAIL, NOT_MEASURED)):
                value = "" if m.value is None else f" = {m.value:g}"
                print(f"  {m.status.upper():<14}{m.name}{value} ({m.threshold or 'no threshold'}) {m.detail[:140]}")
    print("Reports: " + ", ".join(str(p) for p in paths.values())
          + ("" if args.no_record else f"; recorded in {db_path} (QualityRuns / QualityMetrics)"))
    return status


if __name__ == "__main__":
    sys.exit(main())
