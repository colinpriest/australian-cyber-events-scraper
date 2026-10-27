#!/usr/bin/env python3
"""Refresh the published OAIC statistics: the one command a pipeline runs.

    python scripts/oaic/refresh_oaic_statistics.py

What it does:

1. Downloads every Notifiable Data Breaches workbook from data.gov.au (the only
   channel OAIC still updates: July-December 2025 onward) and parses each into
   a semester record.
2. Checks each record: the shared internal-consistency rules
   (``oaic_consistency.find_contradictions``: source split vs total, monthly
   counts vs the semester, sector / method / bucket shares within their whole,
   percentages within 0-100) plus data.gov.au's exact-sum checks
   (``oaic_datagov_scraper.verify_record``).
3. Promotes a record into the published dataset
   (``instance/oaic/oaic_statistics.json``) only if it passes. A failing
   record is rejected with its reasons and its period keeps the last valid
   record; the dataset file is replaced atomically and never by a failed run.
   The first run bootstraps the dataset from the legacy
   ``oaic_cyber_statistics_*.json`` files, screened (contradicting figures
   withheld, with reasons).
4. Records the attempt in ``instance/oaic/oaic_refresh_state.json``: when each
   period was last refreshed, and the last fully successful refresh - shown on
   the dashboard as "OAIC statistics last refreshed <date> (latest period <p>)".

Exit status: 0 only when discovery, every download and every record passed;
1 otherwise (the published dataset then still holds the last valid data).
Periods up to 2025 H1 come from the discontinued Power BI dashboard and PDF
reports (``OAIC_dashboard_scraper.py``, ``oaic_data_scraper.py``); both promote
into the same dataset through the same checks.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.oaic.oaic_dataset import (  # noqa: E402
    DEFAULT_DATASET_PATH,
    DEFAULT_STATE_PATH,
    OAICDatasetError,
    PromotionResult,
    load_dataset,
    load_state,
    promote_records,
    refresh_status_line,
)
from scripts.oaic.oaic_datagov_scraper import ingest_datagov, verify_record  # noqa: E402

logger = logging.getLogger(__name__)


def refresh(dataset_path: Path = DEFAULT_DATASET_PATH,
            state_path: Path = DEFAULT_STATE_PATH,
            cache_dir: Path = Path("instance/oaic_datagov"),
            legacy_dir: Optional[Path] = Path("."),
            ingest=ingest_datagov) -> PromotionResult:
    """Ingest data.gov.au and promote what passes. ``ingest`` is injectable for tests."""
    records, errors, skipped = ingest(cache_dir)
    return promote_records(records, "data.gov.au", dataset_path=dataset_path,
                           state_path=state_path, legacy_dir=legacy_dir,
                           errors=errors, skipped=skipped, extra_checks=verify_record)


def report(result: PromotionResult, dataset_path: Path, state_path: Path) -> None:
    if result.bootstrapped:
        print(f"Bootstrapped the published dataset from legacy files "
              f"({len(result.bootstrap_notes)} figure(s) withheld or flagged)")
    print(f"Promoted: {', '.join(result.accepted) or 'none'}")
    for period, problems in result.rejected.items():
        print(f"REJECTED {period} (last valid record kept): {'; '.join(problems)}")
    for error in result.errors:
        print(f"ERROR {error}")
    for item, reason in result.skipped.items():
        print(f"Skipped {item}: {reason}")
    dataset = load_dataset(dataset_path)
    records = dataset["records"] if dataset else []
    print(refresh_status_line(load_state(state_path), records, "published dataset"))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH,
                        help=f"Published dataset (default: {DEFAULT_DATASET_PATH})")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH,
                        help=f"Refresh state (default: {DEFAULT_STATE_PATH})")
    parser.add_argument("--cache-dir", type=Path, default=Path("instance/oaic_datagov"),
                        help="Where downloaded workbooks are stored")
    parser.add_argument("--legacy-dir", type=Path, default=Path("."),
                        help="Directory of legacy oaic_cyber_statistics_*.json files used to "
                             "bootstrap the dataset on the first run")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s - %(levelname)s - %(message)s")
    try:
        result = refresh(args.dataset, args.state, args.cache_dir, args.legacy_dir)
    except OAICDatasetError as exc:
        print(f"[FAILED] {exc} - nothing was changed")
        return 1
    report(result, args.dataset, args.state)
    if not result.ok:
        print("[FAILED] OAIC refresh did not fully succeed; the dashboard keeps showing the "
              "last valid data and says so.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
