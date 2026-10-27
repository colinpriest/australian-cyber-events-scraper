#!/usr/bin/env python3
"""Validate every OAIC statistics file; fail loudly on anything unusable.

    python scripts/oaic/cleanup_oaic_data.py [--legacy-dir .] [--dataset PATH]

Checks:

* every legacy ``oaic_cyber_statistics_*.json`` file can be read and is a list
  of period records - a corrupt file is a FAILURE, never skipped (it used to be
  possible to get a clean-looking result with a corrupt file left out);
* the published dataset (``instance/oaic/oaic_statistics.json``) and the
  refresh state can be read, and no published record contradicts itself;
* contradictions inside legacy files are listed - they are withheld whenever
  those files are read (dashboard, bootstrap), never published.

Exit status 1 on an unreadable file or a contradiction in the published
dataset; 0 otherwise. This replaces the old one-off consolidation, which merged
three hard-coded file names (long gone) and nulled bad values while reporting
success. Consolidation is now ``refresh_oaic_statistics.py``: it bootstraps the
published dataset from the legacy files and promotes only consistent records.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.oaic.oaic_consistency import find_contradictions, period_label  # noqa: E402
from scripts.oaic.oaic_dataset import (  # noqa: E402
    DEFAULT_DATASET_PATH,
    DEFAULT_STATE_PATH,
    OAICDatasetError,
    legacy_files_newest_first,
    load_dataset,
    load_state,
    read_record_list,
)


def validate(legacy_dir: Path = Path("."), dataset_path: Path = DEFAULT_DATASET_PATH,
             state_path: Path = DEFAULT_STATE_PATH) -> tuple:
    """``(failures, findings)``: failures make validation fail; findings are informational."""
    failures: List[str] = []
    findings: List[str] = []

    files = legacy_files_newest_first(legacy_dir)
    for path in files:
        try:
            records = read_record_list(path)
        except OAICDatasetError as exc:
            failures.append(f"unreadable legacy file: {exc}")
            continue
        for record in records:
            if record.get("period") not in ("H1", "H2"):
                continue
            for c in find_contradictions(record):
                findings.append(f"{Path(path).name} {period_label(record)}: {c.message} "
                                f"(withheld when read)")

    try:
        dataset = load_dataset(dataset_path)
    except OAICDatasetError as exc:
        failures.append(f"unreadable published dataset: {exc}")
        dataset = None
    if dataset is None and not failures:
        findings.append(f"no published dataset at {dataset_path}; run "
                        "python scripts/oaic/refresh_oaic_statistics.py")
    for record in (dataset or {}).get("records", []):
        for c in find_contradictions(record):
            failures.append(f"published {period_label(record)}: {c.message}")

    try:
        load_state(state_path)
    except OAICDatasetError as exc:
        failures.append(f"unreadable refresh state: {exc}")

    if not files and dataset is None:
        failures.append("no OAIC statistics found (no legacy files, no published dataset)")
    return failures, findings


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate OAIC statistics files")
    parser.add_argument("--legacy-dir", type=Path, default=Path("."))
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    args = parser.parse_args(argv)

    failures, findings = validate(args.legacy_dir, args.dataset, args.state)
    for f in findings:
        print(f"  note: {f}")
    for f in failures:
        print(f"  FAIL: {f}")
    if failures:
        print(f"OAIC validation FAILED ({len(failures)} problem(s)).")
        return 1
    print(f"OAIC validation passed ({len(findings)} note(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
