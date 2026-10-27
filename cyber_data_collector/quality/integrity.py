"""Run ``scripts/check_data_integrity.py``'s checks as quality metrics.

The checks are reused, not copied: each ``CheckResult`` becomes
``integrity.<check name>``. Two kinds of "pass" there are not passes here:

* a check that **skipped** itself ("Skipped: database schema is incomplete",
  "In-memory database ...", "Size columns not present") measured nothing, so it
  is ``not_measured``;
* the OAIC statistics checks read ``oaic_cyber_statistics_*.json`` from the
  working directory and silently ignore files they cannot parse. With no files
  they "pass" having looked at nothing, so they are ``not_measured``; and every
  file is parsed here first, so an unreadable one is reported
  (``integrity.oaic-files-readable``) instead of skipped.

``integrity.reporting-population`` and
``integrity.dashboard-totals-reconcile`` are the reporting-population
reconciliation the dashboards depend on.
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
from typing import Dict

from cyber_data_collector.quality.model import Measurement

SKIP_MARKERS = ("Skipped", "In-memory database", "Size columns not present")


def _load_module():
    return importlib.import_module("scripts.check_data_integrity")


def measure_integrity(conn: sqlite3.Connection, thresholds=None, today=None) -> Dict[str, Measurement]:
    out: Dict[str, Measurement] = {}
    try:
        module = _load_module()
    except Exception as exc:  # noqa: BLE001 - reported, and blocking via the rules
        out["integrity.module"] = Measurement.unavailable(
            f"cannot import scripts.check_data_integrity: {type(exc).__name__}: {exc}")
        return out

    for fn in module.DB_CHECKS:
        out.update(_run(fn, conn))

    files = module._oaic_files_newest_first()
    unreadable = []
    for path in files:
        try:
            with open(path, encoding="utf-8") as fh:
                json.load(fh)
        except Exception as exc:  # noqa: BLE001
            unreadable.append(f"{os.path.basename(path)}: {type(exc).__name__}: {exc}")
    if not files:
        reason = (f"no oaic_cyber_statistics_*.json in the working directory {os.getcwd()} "
                  "(run from the project root)")
        out["integrity.oaic-files-readable"] = Measurement.unavailable(reason)
        for fn in module.JSON_CHECKS:
            out[f"integrity.{_name(fn)}"] = Measurement.unavailable(reason)
        return out
    out["integrity.oaic-files-readable"] = Measurement(
        ok=not unreadable, value=len(unreadable), examples=unreadable,
        detail=f"{len(files) - len(unreadable)} of {len(files)} OAIC statistics files parse as JSON "
               "(the OAIC checks skip any that do not)")
    for fn in module.JSON_CHECKS:
        out.update(_run(fn, None))
    return out


def _name(fn) -> str:
    """Metric name from the check *function*, so it is the same whether the check
    passed, failed, raised or could not run (a check names its own result only
    when it gets that far, and some name it differently from the function)."""
    name = fn.__name__
    return (name[len("check_"):] if name.startswith("check_") else name).replace("_", "-")


def _run(fn, arg) -> Dict[str, Measurement]:
    key = f"integrity.{_name(fn)}"
    try:
        result = fn(arg)
    except Exception as exc:  # noqa: BLE001 - a check that raised measured nothing
        return {key: Measurement.unavailable(f"check raised {type(exc).__name__}: {exc}")}
    details = [str(d) for d in (result.details or [])]
    summary = f"[{result.name}] {result.summary or ''}".rstrip()
    if result.ok and (result.summary or "").startswith(SKIP_MARKERS):
        return {key: Measurement.unavailable(f"check skipped itself: {summary}")}
    return {key: Measurement(ok=bool(result.ok), detail=summary, examples=details[:15])}
