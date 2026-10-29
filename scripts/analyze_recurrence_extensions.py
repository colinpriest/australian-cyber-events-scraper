"""Run the extended analyses of recorded cyber incident recurrence.

The input database is copied using SQLite's online backup API; every analysis
reads the same consistent snapshot and the source database is never modified.
The snapshot is *retained* (``instance/snapshots/``, see
``cyber_data_collector/publication/snapshot.py``) and named in
``analysis_inputs.json`` beside the outputs, so the exact input of a result can
be restored: rerun with ``--snapshot-id <id>`` to reproduce it, or with
``--snapshot-id latest-published`` to analyse what the dashboard shows.

Results live in ONE folder, ``analysis/recurrent_timing_extended/`` (also where
``analyze_recurrent_timing.py --extended`` writes). It holds the frozen
prospective cohorts, ``timing_peak_registration.json`` and
``timing_size_adjusted_registration.json``, which cannot be recreated after the
registration date: keep them there across reruns.

    python scripts/analyze_recurrence_extensions.py --db instance/cyber_events.db
    python scripts/analyze_recurrence_extensions.py --db instance/cyber_events.db \
        --censor-date 2026-10-02 --bootstrap 500 --date-draws 200
    python scripts/analyze_recurrence_extensions.py --snapshot-id latest-published
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.analyze_recurrent_timing import (
    UNRESOLVED_ATTRIBUTION_POLICIES, apply_attribution_policy, last_ingest_date, load_victim_events, parse_date,
)
from cyber_data_collector.publication.snapshot import AnalysisInput
from scripts.recurrence_extended import linkage_supplier, sequence_bias, timing_precision

DEFAULT_DB = "instance/cyber_events.db"
DEFAULT_OUT = "analysis/recurrent_timing_extended"


def snapshot_database(source: Path, destination: Path) -> None:
    """Copy a consistent SQLite snapshot, including committed WAL data."""
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Database does not exist: {source}")
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    try:
        src.execute("PRAGMA query_only = ON")
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
            result = dst.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"Database snapshot failed quick_check: {result}")
        finally:
            dst.close()
    finally:
        src.close()


def add_first_discovery(events: pd.DataFrame, db_path: Path) -> pd.DataFrame:
    """Attach earliest discovery timestamp from each incident's source records."""
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        timestamps = pd.read_sql_query(
            """
            SELECT m.deduplicated_event_id, MIN(r.discovered_at) AS first_discovered_at
            FROM EventDeduplicationMap m
            LEFT JOIN RawEvents r ON r.raw_event_id = m.raw_event_id
            GROUP BY m.deduplicated_event_id
            """,
            conn,
        )
    finally:
        conn.close()
    return events.merge(timestamps, on="deduplicated_event_id", how="left", validate="many_to_one")


def observed_by(events: pd.DataFrame, censor_date: date) -> tuple[pd.DataFrame, dict[str, int]]:
    """Exclude incidents dated or first known after the analysis cutoff.

    Unknown discovery dates remain in the descriptive analysis but are counted;
    they cannot establish availability at an earlier registration date.
    """
    incident_dates = pd.to_datetime(events["event_date"], errors="coerce").dt.date
    discovery_dates = pd.to_datetime(
        events["first_discovered_at"].astype("string").str.slice(0, 10), errors="coerce"
    ).dt.date
    valid_incident = incident_dates.notna() & (incident_dates <= censor_date)
    discovered_late = discovery_dates.notna() & (discovery_dates > censor_date)
    keep = valid_incident & ~discovered_late
    retained = events.loc[keep].copy()
    retained["event_date"] = incident_dates.loc[keep]
    retained["discovery_date_known"] = discovery_dates.loc[keep].notna().to_numpy()
    info = {
        "input_attributions": int(len(events)),
        "excluded_after_incident_cutoff": int((~valid_incident).sum()),
        "excluded_first_discovered_after_cutoff": int((valid_incident & discovered_late).sum()),
        "retained_unknown_discovery_date": int((keep & discovery_dates.isna()).sum()),
        "retained_attributions": int(keep.sum()),
    }
    return retained, info


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (date, datetime, Path)):
        return str(value)
    return value


def size_reference(events: pd.DataFrame) -> pd.DataFrame:
    """Count organisations once; conflicting or absent size is UNKNOWN."""
    if "entity_id" not in events:
        return pd.DataFrame(columns=["organisation_size", "organisations", "weight"])
    values = events["org_size"] if "org_size" in events else pd.Series("UNKNOWN", index=events.index)
    labels = values.fillna("UNKNOWN").astype(str).str.strip().str.upper()
    labels = labels.where(labels.isin(["SMALL", "MEDIUM", "LARGE", "HUGE"]), "UNKNOWN")
    sizes = pd.DataFrame({"entity_id": events["entity_id"], "size": labels}).dropna(subset=["entity_id"])
    categories = sizes.groupby("entity_id")["size"].agg(lambda s: s.iloc[0] if s.nunique() == 1 else "UNKNOWN")
    counts = categories.value_counts().reindex(["SMALL", "MEDIUM", "LARGE", "HUGE", "UNKNOWN"], fill_value=0)
    total = int(counts.sum())
    return pd.DataFrame({"organisation_size": counts.index, "organisations": counts.values,
                         "weight": counts.values / total if total else np.zeros(len(counts))})


def run_analysis(db_path: Path, out_dir: Path, censor_date: date | None = None,
                 bootstrap: int = 200, date_draws: int = 100,
                 seed: int = 20261027, unresolved_attributions: str = "include",
                 analysis_input: AnalysisInput | None = None,
                 snapshot_id: str | None = None) -> dict[str, Any]:
    """Run every extended analysis on one snapshot.

    The snapshot is ``analysis_input`` (shared with the caller's other analyses), else the retained
    snapshot ``snapshot_id``, else a new snapshot of ``db_path``, which is retained too. Its id and hash
    are written to ``analysis_inputs.json`` and the summary.

    ``unresolved_attributions`` keeps (``include``) or drops (``exclude``)
    victim rows the calibrated attribution check left unresolved; the counts
    are in the summary's ``unresolved_attributions`` either way.
    """
    if bootstrap < 1 or date_draws < 1:
        raise ValueError("bootstrap and date draws must both be positive")
    out_dir.mkdir(parents=True, exist_ok=True)
    owned = analysis_input is None
    inputs = analysis_input or AnalysisInput.open(db_path, snapshot_id)
    try:
        snapshot = inputs.path
        snapshot_sha256 = inputs.snapshot.sha256
        end = censor_date or last_ingest_date(snapshot)
        raw_events = add_first_discovery(load_victim_events(snapshot), snapshot)
        events, coverage = observed_by(raw_events, end)
        events, unresolved = apply_attribution_policy(events, unresolved_attributions)
        if events.empty:
            raise RuntimeError("No victim events were available by the analysis cutoff")
        reference = size_reference(events)
        reference.to_csv(out_dir / "organisation_size_reference.csv", index=False)
        results = {}
        for name, module in (
            ("timing_and_date_precision", timing_precision),
            ("linkage_and_shared_incidents", linkage_supplier),
            ("event_order_transitions_and_observation_bias", sequence_bias),
        ):
            results[name] = module.run(
                events.copy(), snapshot, end, out_dir, seed,
                bootstrap=bootstrap, date_draws=date_draws, draws=date_draws,
            )
        inputs.record(out_dir, analysis="extended recurrence analyses", censor_date=end.isoformat(),
                      seed=seed, bootstrap_replicates=bootstrap, date_draws=date_draws,
                      unresolved_attributions=unresolved_attributions)
    finally:
        if owned:
            inputs.close()

    summary = {
        "source_database": str(db_path.resolve()),
        "snapshot_id": inputs.snapshot.snapshot_id,
        "snapshot_sha256": snapshot_sha256,
        "censor_date": end.isoformat(),
        "seed": seed,
        "bootstrap_replicates": bootstrap,
        "date_draws": date_draws,
        "observation_coverage": coverage,
        "unresolved_attributions": unresolved,
        "organisation_size_reference": reference.to_dict(orient="records"),
        "analyses": results,
    }
    safe_summary = _json_safe(summary)
    (out_dir / "extended_analysis_summary.json").write_text(
        json.dumps(safe_summary, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    lines = [
        "# Extended recurrence analyses", "",
        f"Observation cutoff: {end.isoformat()}. Seed: {seed}.", "",
        "The analyses describe recorded incidents among organisations with at least "
        "one recorded victim event. They do not estimate the probability of an "
        "attack across all Australian organisations.", "",
        "Organisation size is included in comparisons using the recorded size "
        "categories. The standard population is the organisations in this observed "
        "cohort, counted once each; UNKNOWN remains a separate category. Size is an "
        "imperfect proxy for visibility, attack exposure and target value, which are "
        "not directly measured. Current size estimates may differ from size at the "
        "incident date, and residual confounding remains possible.", "",
        "| Recorded size | Organisations | Standard population weight |",
        "|---|---:|---:|",
        *[f"| {row.organisation_size} | {row.organisations} | {row.weight:.1%} |"
          for row in reference.itertuples(index=False)], "",
        f"Retained {coverage['retained_attributions']} victim-event attributions; "
        f"excluded {coverage['excluded_after_incident_cutoff']} after the incident-date "
        f"cutoff and {coverage['excluded_first_discovered_after_cutoff']} first discovered "
        f"after it. {coverage['retained_unknown_discovery_date']} retained attributions "
        "have unknown discovery dates.", "",
        f"Victim attributions left unresolved by the calibrated attribution check: "
        f"{unresolved['victim-event attributions unresolved']} on "
        f"{unresolved['events with an unresolved attribution']} incident(s), "
        f"{'included' if unresolved['policy'] == 'include' else 'excluded'} here "
        f"(--unresolved-attributions).", "",
        "Historical cutoffs filter known discovery dates, but the snapshot contains "
        "present-day incident links, fields, and classifications. It is not a complete "
        "historical database snapshot; retrospective and prospective interpretations "
        "must account for that limitation.", "",
    ]
    for result in results.values():
        section = result.get("section") or result.get("report_section")
        if section:
            lines.extend([str(section).strip(), ""])
    lines.extend(["## Reproducibility", "", f"Database snapshot: `{inputs.snapshot.snapshot_id}` "
                  f"(retained under `instance/snapshots/`; rerun with `--snapshot-id {inputs.snapshot.snapshot_id}`).",
                  f"Database snapshot SHA-256: `{snapshot_sha256}`.",
                  f"Bootstrap replicates: {bootstrap}; date draws: {date_draws}.", "",
                  "The detailed result tables are saved as CSV files alongside this report.", ""])
    (out_dir / "extended_recurrence_report.md").write_text("\n".join(lines), encoding="utf-8")
    return safe_summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB, help="Input SQLite database; opened read-only")
    parser.add_argument("--snapshot-id", default=None,
                        help="Analyse this retained snapshot (an id, or 'latest-published') instead of "
                             "snapshotting --db; reproduces an earlier result")
    parser.add_argument("--out-dir", default=DEFAULT_OUT, help="Directory for tables and report")
    parser.add_argument("--censor-date", help="Observation cutoff YYYY-MM-DD; defaults to last ingest")
    parser.add_argument("--bootstrap", type=int, default=200, help="Bootstrap replicates")
    parser.add_argument("--date-draws", type=int, default=100, help="Date-uncertainty draws")
    parser.add_argument("--seed", type=int, default=20261027, help="Random seed")
    parser.add_argument("--unresolved-attributions", choices=UNRESOLVED_ATTRIBUTION_POLICIES, default="include",
                        help="Keep (default) or drop victim rows whose attribution is unresolved")
    args = parser.parse_args(argv)
    summary = run_analysis(Path(args.db), Path(args.out_dir),
                           parse_date(args.censor_date) if args.censor_date else None,
                           args.bootstrap, args.date_draws, args.seed, args.unresolved_attributions,
                           snapshot_id=args.snapshot_id)
    print(f"Wrote extended recurrence analyses to {Path(args.out_dir).resolve()}")
    print(f"Observation cutoff: {summary['censor_date']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
