"""The published OAIC statistics dataset: validated, promoted only on pass, dated.

Every OAIC ingester used to write a new ``oaic_cyber_statistics_<ts>.json``
straight into the project root, and the dashboard merged every such file field
by field, newest first. A bad extract therefore superseded good data the moment
it was written, a corrupt file was skipped with a warning, and nothing recorded
when each period was last refreshed.

Now there is one published dataset (``DEFAULT_DATASET_PATH``) that only ever
holds records that passed :mod:`scripts.oaic.oaic_consistency`:

* :func:`promote_records` takes freshly extracted records, checks each one, and
  replaces a period's record only if the new one passes. A failing record is
  rejected with its reasons and the period keeps its last valid record. The
  dataset file is written to a temporary file and moved into place, so it is
  never left half-written or replaced by a failed run.
* Each promoted record carries ``refresh`` metadata (when it was last
  successfully refreshed, from which source); the refresh state file
  (``DEFAULT_STATE_PATH``) records every attempt and the last fully successful
  one, which the dashboard shows as "OAIC statistics last refreshed <date>
  (latest period <period>)".
* The first promotion bootstraps the dataset from the legacy files, screened
  with :func:`screen_record` (contradicting figures withheld, with reasons).

The routine refresh command is ``python scripts/oaic/refresh_oaic_statistics.py``.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from scripts.oaic.oaic_consistency import (
    Contradiction,
    find_contradictions,
    period_label,
    screen_record,
)
from scripts.oaic.oaic_validators import sanitize_top_sectors

logger = logging.getLogger(__name__)

DEFAULT_DATASET_PATH = Path("instance/oaic/oaic_statistics.json")
DEFAULT_STATE_PATH = Path("instance/oaic/oaic_refresh_state.json")
DATASET_SCHEMA = "oaic-statistics-v1"
LEGACY_GLOB = "oaic_cyber_statistics_*.json"
MAX_ATTEMPTS_KEPT = 50

_LEGACY_TS_RE = re.compile(r"(\d{8})_(\d{6})")


class OAICDatasetError(RuntimeError):
    """An OAIC statistics file could not be read, parsed or written."""


# ---------------------------------------------------------------------------
# Atomic JSON I/O
# ---------------------------------------------------------------------------

def write_json_atomically(path: Path, payload: Any) -> None:
    """Write JSON to a temporary file beside ``path``, then move it into place."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                       encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OAICDatasetError(f"cannot read {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Legacy oaic_cyber_statistics_*.json files
# ---------------------------------------------------------------------------

def legacy_files_newest_first(directory: Path = Path(".")) -> List[str]:
    """Legacy OAIC stat files newest-scrape-first, by the stamp in the filename.

    Sorts on the YYYYMMDD_HHMMSS stamp rather than mtime: mtime is reset by any
    copy/backup/rewrite and can silently promote stale data for a period.
    """
    def _key(path: str) -> str:
        m = _LEGACY_TS_RE.search(os.path.basename(path))
        return m.group(1) + m.group(2) if m else "0"

    return sorted(glob.glob(str(Path(directory) / LEGACY_GLOB)), key=_key, reverse=True)


def read_record_list(path: str) -> List[Dict[str, Any]]:
    """A legacy file's records. Raises OAICDatasetError if unreadable or malformed."""
    data = _read_json(Path(path))
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise OAICDatasetError(f"{path}: not a list of period records")
    return data


def _sanitise(record: Dict[str, Any]) -> Dict[str, Any]:
    """Null top-sector entries the shared sanitiser rejects (as the dashboard did)."""
    total = record.get('total_notifications') or 0
    if record.get('top_sectors'):
        record['top_sectors'] = sanitize_top_sectors(
            record['top_sectors'], total if total > 0 else None) or None
    return record


def _has_any_notifications(rec: Dict[str, Any]) -> bool:
    return any(isinstance(s, dict) and isinstance(s.get('notifications'), (int, float))
               for s in rec.get('top_sectors') or [])


@dataclass
class ScreenNote:
    """A figure (or a whole period) withheld, and why."""
    period: str
    reason: str
    fields: List[str]
    origin: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"period": self.period, "reason": self.reason, "fields": self.fields,
                "origin": self.origin}


def _note(period: str, c: Contradiction, origin: str) -> ScreenNote:
    return ScreenNote(period, c.message,
                      ["whole period"] if c.whole_period else list(c.fields), origin)


def merge_legacy_files(directory: Path = Path(".")) -> Tuple[List[Dict[str, Any]], List[ScreenNote]]:
    """Merge every legacy file into one record per period, screened.

    Files are merged newest first; an older file only fills fields the newer
    ones left empty, and only when it reports the same ``total_notifications``.
    Filling from a record with a different total mixed two extractions (2022 H1
    took a 250 total from a PDF scrape and a 397-notification monthly series from
    the Power BI one), so a disagreement on the total is reported as a note
    instead. Each file's record is screened before it is merged, so a
    contradicting figure is withheld and an older file may supply a consistent
    one; a record that contradicts its own total contributes nothing. The merged
    record is screened again.

    Raises:
        OAICDatasetError: a file cannot be read - a corrupt file is never skipped.
    """
    notes: List[ScreenNote] = []
    merged: Dict[str, Dict[str, Any]] = {}
    other_totals: Dict[str, Dict[Any, str]] = {}
    for path in legacy_files_newest_first(directory):
        name = os.path.basename(path)
        for raw in read_record_list(path):
            if not raw.get('year') or not raw.get('period'):
                notes.append(ScreenNote(f"{raw.get('year')} {raw.get('period')}",
                                        "record has no year/period", ["whole period"], name))
                continue
            if raw.get('period') not in ('H1', 'H2'):
                continue  # quarterly reports (2018-2019) are not half-year periods
            record, issues = screen_record(_sanitise(dict(raw)))
            key = period_label(raw)
            notes.extend(_note(key, c, name) for c in issues)
            if record is None:
                continue
            if key not in merged:
                merged[key] = record
                continue
            existing = merged[key]
            if record.get('total_notifications') != existing.get('total_notifications'):
                other_totals.setdefault(key, {}).setdefault(record.get('total_notifications'), name)
                continue
            if record.get('top_sectors') and not _has_any_notifications(existing):
                existing['top_sectors'] = record['top_sectors']
            for k, v in record.items():
                if k == 'withheld':
                    continue
                if k not in existing or existing[k] is None:
                    existing[k] = v

    for key, totals in other_totals.items():
        shown = merged[key].get('total_notifications')
        listed = ", ".join(f"{t} ({f})" for t, f in totals.items())
        notes.append(ScreenNote(
            key, f"legacy files disagree on total_notifications: showing {shown} from the newest "
                 f"file; other files report {listed} (not merged)", [], "legacy files"))

    out: List[Dict[str, Any]] = []
    for key, record in merged.items():
        record.pop('withheld', None)
        screened, issues = screen_record(record)
        notes.extend(_note(key, c, "merged legacy files") for c in issues)
        if screened is not None:
            out.append(screened)
    out.sort(key=lambda r: (r.get('year', 0), 0 if r.get('period') == 'H1' else 1))
    return out, _dedupe(notes)


def _dedupe(notes: Iterable[ScreenNote]) -> List[ScreenNote]:
    seen, out = set(), []
    for n in notes:
        key = (n.period, n.reason, tuple(n.fields))
        if key not in seen:
            seen.add(key)
            out.append(n)
    return out


# ---------------------------------------------------------------------------
# The published dataset
# ---------------------------------------------------------------------------

def load_dataset(path: Path = DEFAULT_DATASET_PATH) -> Optional[Dict[str, Any]]:
    """The published dataset, or None if it has never been written.

    Raises:
        OAICDatasetError: the file exists but is unreadable or malformed.
    """
    path = Path(path)
    if not path.exists():
        return None
    data = _read_json(path)
    if (not isinstance(data, dict) or data.get("schema") != DATASET_SCHEMA
            or not isinstance(data.get("records"), list)):
        raise OAICDatasetError(f"{path}: not an {DATASET_SCHEMA} dataset")
    return data


def load_state(path: Path = DEFAULT_STATE_PATH) -> Dict[str, Any]:
    """The refresh state ({} if no refresh has ever run). Raises if unreadable."""
    path = Path(path)
    if not path.exists():
        return {}
    data = _read_json(path)
    if not isinstance(data, dict):
        raise OAICDatasetError(f"{path}: not a refresh-state object")
    return data


@dataclass
class PromotionResult:
    """What one promotion accepted, rejected and wrote."""
    source: str
    accepted: List[str] = field(default_factory=list)
    rejected: Dict[str, List[str]] = field(default_factory=dict)
    skipped: Dict[str, str] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    bootstrapped: bool = False
    bootstrap_notes: List[ScreenNote] = field(default_factory=list)
    dataset_written: bool = False

    @property
    def ok(self) -> bool:
        return not self.rejected and not self.errors


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def promote_records(
    new_records: List[Dict[str, Any]],
    source: str,
    *,
    dataset_path: Path = DEFAULT_DATASET_PATH,
    state_path: Path = DEFAULT_STATE_PATH,
    legacy_dir: Optional[Path] = Path("."),
    errors: Optional[List[str]] = None,
    skipped: Optional[Dict[str, str]] = None,
    extra_checks=None,
    now: Optional[str] = None,
) -> PromotionResult:
    """Promote freshly extracted records into the published dataset.

    Args:
        new_records: Records from this run (one per semester).
        source: Where they came from ("data.gov.au", "power-bi", "pdf").
        dataset_path / state_path: The published dataset and refresh state.
        legacy_dir: Directory of legacy files to bootstrap from when the dataset
            does not exist yet (None: start empty).
        errors: Run-level failures to record (download/parse errors); any error
            makes the attempt a failure.
        skipped: Inputs deliberately not ingested, with the reason (recorded).
        extra_checks: Optional ``record -> List[str]`` of source-specific
            problems (e.g. the data.gov.au exact-sum checks), each a rejection.
        now: Timestamp override (tests).

    A record is promoted only if it has no contradiction and no extra-check
    problem; otherwise its period keeps the last valid record. The dataset is
    written atomically, and only if something changed. The attempt is always
    recorded in the state file; ``last_success_at`` moves only on a clean run.
    """
    now = now or _now()
    result = PromotionResult(source=source, errors=list(errors or []),
                             skipped=dict(skipped or {}))

    dataset = load_dataset(dataset_path)  # raises if corrupt: never overwrite it
    if dataset is None:
        records: Dict[str, Dict[str, Any]] = {}
        if legacy_dir is not None:
            legacy, notes = merge_legacy_files(legacy_dir)
            for r in legacy:
                r["refresh"] = {"refreshed_at": r.get("scraped_at"), "source": "legacy files",
                                "promoted_at": now}
                records[period_label(r)] = r
            result.bootstrap_notes = notes
        dataset = {"schema": DATASET_SCHEMA, "records": []}
        result.bootstrapped = True
    else:
        records = {period_label(r): r for r in dataset["records"]}

    for record in new_records:
        key = period_label(record)
        problems = [c.message for c in find_contradictions(record)]
        if extra_checks is not None:
            problems += list(extra_checks(record))
        if problems:
            result.rejected[key] = problems
            logger.error("[%s] not promoted (keeping the last valid record%s): %s", key,
                         "" if key in records else "; period has none, so it is not published",
                         "; ".join(problems))
            continue
        promoted = dict(record)
        promoted.pop("withheld", None)
        promoted["refresh"] = {"refreshed_at": now, "source": source, "promoted_at": now}
        records[key] = promoted
        result.accepted.append(key)

    if result.accepted or result.bootstrapped:
        dataset["records"] = sorted(
            records.values(), key=lambda r: (r.get("year", 0), 0 if r.get("period") == "H1" else 1))
        dataset["written_at"] = now
        if result.bootstrapped:
            dataset["bootstrap"] = {
                "at": now, "from": str(legacy_dir) if legacy_dir is not None else None,
                "withheld": [n.as_dict() for n in result.bootstrap_notes]}
        write_json_atomically(dataset_path, dataset)
        result.dataset_written = True

    _record_attempt(state_path, result, now, records)
    return result


def _record_attempt(state_path: Path, result: PromotionResult, now: str,
                    records: Dict[str, Dict[str, Any]]) -> None:
    state = load_state(state_path)
    attempt = {
        "at": now,
        "source": result.source,
        "status": "ok" if result.ok else "failed",
        "accepted": result.accepted,
        "rejected": result.rejected,
        "skipped": result.skipped,
        "errors": result.errors,
        "bootstrapped": result.bootstrapped,
    }
    attempts = (state.get("attempts") or [])[-(MAX_ATTEMPTS_KEPT - 1):] + [attempt]
    state["attempts"] = attempts
    state["last_attempt"] = attempt
    if result.ok:
        state["last_success_at"] = now
        state["last_success_source"] = result.source
    periods = state.get("period_refreshed_at") or {}
    for key in result.accepted:
        periods[key] = {"refreshed_at": now, "source": result.source}
    state["period_refreshed_at"] = periods
    if records:
        latest = max(records.values(),
                     key=lambda r: (r.get("year", 0), 0 if r.get("period") == "H1" else 1))
        state["latest_period"] = period_label(latest)
    write_json_atomically(state_path, state)


# ---------------------------------------------------------------------------
# Reading for the dashboard
# ---------------------------------------------------------------------------

@dataclass
class OAICLoad:
    """What the dashboard loaded and what it must tell the reader."""
    records: List[Dict[str, Any]]
    notes: List[ScreenNote]
    origin: str                      # "published dataset" | "legacy files" | "none"
    status_line: str


def refresh_status_line(state: Dict[str, Any], records: List[Dict[str, Any]],
                        origin: str) -> str:
    """The one-line OAIC freshness statement shown on the dashboard."""
    latest = (max(records, key=lambda r: (r.get("year", 0), 0 if r.get("period") == "H1" else 1))
              if records else None)
    latest_txt = period_label(latest) if latest else "none"
    if origin == "none":
        return "OAIC statistics: none loaded."
    if origin == "legacy files":
        return (f"OAIC statistics not from a validated refresh (merged legacy scrape files; "
                f"latest period {latest_txt}). Run python scripts/oaic/refresh_oaic_statistics.py.")
    success = state.get("last_success_at")
    line = (f"OAIC statistics last refreshed {success[:10]} (latest period {latest_txt})."
            if success else
            f"OAIC statistics: no successful refresh recorded (latest period {latest_txt}).")
    last = state.get("last_attempt") or {}
    if last.get("status") == "failed" and last.get("at") != success:
        problems = list(last.get("errors") or []) + [
            f"{p}: {'; '.join(m)}" for p, m in (last.get("rejected") or {}).items()]
        line += (f" The refresh attempt on {str(last.get('at'))[:10]} FAILED"
                 f"{' (' + problems[0][:160] + ')' if problems else ''}; "
                 "showing the last valid data.")
    return line


def load_for_dashboard(dataset_path: Path = DEFAULT_DATASET_PATH,
                       state_path: Path = DEFAULT_STATE_PATH,
                       legacy_dir: Path = Path(".")) -> OAICLoad:
    """Records to chart, every withheld figure, and the freshness line.

    Reads the published dataset when it exists, otherwise merges the legacy
    files (and says so). Either way every record is screened once more, so a
    contradiction can never reach a chart. Unreadable files raise.
    """
    dataset = load_dataset(dataset_path)
    if dataset is not None:
        origin = "published dataset"
        raw = dataset["records"]
        notes = [ScreenNote(n.get("period", ""), n.get("reason", ""), n.get("fields", []),
                            n.get("origin", ""))
                 for n in (dataset.get("bootstrap") or {}).get("withheld", [])]
        records = []
        for r in raw:
            screened, issues = screen_record(r)
            notes.extend(_note(period_label(r), c, "published dataset") for c in issues)
            if screened is not None:
                records.append(screened)
        state = load_state(state_path)
    else:
        records, notes = merge_legacy_files(legacy_dir)
        origin = "legacy files" if records or legacy_files_newest_first(legacy_dir) else "none"
        state = {}
    notes = _dedupe(notes)
    return OAICLoad(records, notes, origin, refresh_status_line(state, records, origin))
