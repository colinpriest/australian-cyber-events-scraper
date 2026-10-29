"""Publish every output from one retained snapshot, together, only if the quality gate passes.

Before this, the pipeline built ``dashboard/index.html`` from the live database,
then ran the quality gate on the live database in a separate read, and wrote the
risk matrices earlier still, before any gate. Two reads of a moving database can
disagree, and a blocked run could still have replaced the matrices.

:func:`publish` does it in one pass:

1. take a consistent snapshot of the database (SQLite backup API) and retain a
   compressed copy of it (``snapshot.py``);
2. run the quality gate on the snapshot, record the run in the live database
   with the snapshot id, and stop - leaving every previous output untouched -
   if it blocks (the snapshot is kept, marked ``blocked``);
3. build every output from the snapshot into a staging folder:
   ``index.html``, ``ai_incidents.html``, ``entities.html``, ``timelines.html``,
   ``dedup.html``, ``recurrence.html`` and the ``risk_matrix/`` workbooks;
4. stamp each one with the snapshot id and write ``publication.json`` (hashes
   of every output, the snapshot, the quality run, the inputs outside the
   database, the snapshots the recurrence analyses used);
5. move them all into place with :func:`install`, which backs up what it
   replaces and puts it all back if any move fails, and rolls back a run that
   was interrupted half-way.

A builder that raises, a dashboard that fails validation, or a failed move
publishes nothing. There is no fallback to a partial set.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from cyber_data_collector.publication import snapshot as snapshots

logger = logging.getLogger(__name__)

MANIFEST = "publication.json"
JOURNAL = "journal.json"
LOCK = ".publish.lock"
#: A lock older than this is a crashed run's; it is taken over (and logged).
LOCK_STALE_HOURS = 6
MIN_DASHBOARD_BYTES = 50 * 1024


class PublicationError(RuntimeError):
    """Publication failed; the previous outputs are in place."""


class PublicationBlocked(PublicationError):
    """The quality gate blocked publication."""

    def __init__(self, reasons: Sequence[str], snapshot_id: str):
        self.reasons = list(reasons)
        self.snapshot_id = snapshot_id
        super().__init__("Quality gate blocked publication, last published version kept: "
                         + "; ".join(str(r) for r in self.reasons)[:2000])


@dataclass
class BuildContext:
    """What a builder needs: the snapshot to read and the staging folders to write."""

    snapshot: snapshots.Snapshot
    dashboard_dir: Path          # staged files that go to the dashboard folder
    risk_dir: Path               # staged files that go to the risk-matrix folder
    instance_dir: Path           # folder of the live database (findings files the dashboards read)
    analysis_dir: Path
    live_db: Path

    @property
    def db(self) -> str:
        return str(self.snapshot.path)


@dataclass
class PublicationResult:
    snapshot_id: str
    snapshot_sha256: str
    published: bool
    files: List[str] = field(default_factory=list)
    quality_run_id: Optional[str] = None
    gate: str = "passed"
    warnings: List[str] = field(default_factory=list)
    manifest: Optional[str] = None


# --- validation, stamping --------------------------------------------------------------

def validate_dashboard(path: Optional[str]) -> List[str]:
    """Reasons a built ``index.html`` must not be published; empty when it is fine.

    A real dashboard with all its chart data is at least ~50 KB; a smaller file
    means a query failed silently.
    """
    if not path or not Path(path).exists():
        return ["no dashboard file was produced"]
    problems = []
    size = Path(path).stat().st_size
    if size < MIN_DASHBOARD_BYTES:
        problems.append(f"{Path(path).name} is only {size:,} bytes (< 50 KB): a query probably returned no rows")
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    if "</html>" not in text.lower():
        problems.append(f"{Path(path).name} is truncated (no closing </html>)")
    return problems


_HEAD = re.compile(r"<head[^>]*>", re.IGNORECASE)
_BODY_END = re.compile(r"</body\s*>", re.IGNORECASE)


def stamp_html(path: Path, snap: snapshots.Snapshot, published_at: str) -> None:
    """Record the snapshot in an HTML output: two ``<meta>`` tags and a visible one-line footer."""
    text = path.read_text(encoding="utf-8")
    meta = (f'<meta name="data-snapshot" content="{escape(snap.snapshot_id)}">'
            f'<meta name="data-snapshot-sha256" content="{snap.sha256}">')
    text = _HEAD.sub(lambda m: m.group(0) + meta, text, count=1) if _HEAD.search(text) else meta + text
    footer = (f'<p class="data-snapshot" style="font:12px/1.4 system-ui,sans-serif;color:#6b6b66;margin:16px">'
              f'Data snapshot <code>{escape(snap.snapshot_id)}</code> (sha-256 {snap.sha256[:12]}), '
              f'published {escape(published_at)}.</p>')
    matches = list(_BODY_END.finditer(text))
    if matches:
        end = matches[-1].start()
        text = text[:end] + footer + text[end:]
    else:
        text += footer
    path.write_text(text, encoding="utf-8")


def stamp_workbook(path: Path, snap: snapshots.Snapshot, published_at: str) -> None:
    """Record the snapshot in an Excel workbook's document properties (subject, keywords, description)."""
    from openpyxl import load_workbook

    workbook = load_workbook(path)
    workbook.properties.subject = f"data snapshot {snap.snapshot_id}"
    workbook.properties.keywords = f"snapshot:{snap.snapshot_id}"
    workbook.properties.description = (f"Built from database snapshot {snap.snapshot_id} "
                                       f"(sha-256 {snap.sha256}), published {published_at}.")
    workbook.save(path)


def file_sha256(path: Path) -> str:
    return snapshots.file_sha256(path)


def external_inputs(live_db: Path, repo_root: Path) -> Dict[str, str]:
    """sha-256 of the inputs outside the database that shape the outputs (thresholds, labels, OAIC dataset, findings)."""
    candidates = [Path(__file__).resolve().parents[1] / "quality" / "thresholds.json",
                  live_db.parent / "oaic" / "oaic_statistics.json",
                  live_db.parent / "timeline_reconciliation.json"]
    candidates += sorted((repo_root / "evaluation").glob("*_labels.csv")) if (repo_root / "evaluation").is_dir() else []
    candidates += sorted((repo_root / "evaluation").glob("*_calibration.json")) \
        if (repo_root / "evaluation").is_dir() else []
    out = {}
    for path in candidates:
        if path.is_file():
            out[path.name if path.parent.name != "evaluation" else f"evaluation/{path.name}"] = file_sha256(path)
    return out


def code_version(repo_root: Path) -> Optional[str]:
    try:
        done = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo_root, capture_output=True,
                              text=True, timeout=10)
        return done.stdout.strip() or None if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


# --- atomic install --------------------------------------------------------------------

def install(pairs: Sequence[Tuple[Path, Path]], work_dir: Path) -> None:
    """Move each staged file to its target, all or nothing.

    Every file a target replaces is backed up first; if any move fails, the
    targets already replaced are restored (and new ones removed) before the
    error is raised. A journal in ``work_dir`` lets :func:`recover_interrupted`
    roll back a process that died half-way. Staged files must be on the same
    volume as their targets (the move is ``os.replace``); a cross-device move
    is an error, never a copy.
    """
    work_dir = Path(work_dir)
    backup = work_dir / "backup"
    backup.mkdir(parents=True, exist_ok=True)
    entries = []
    for index, (staged, target) in enumerate(pairs):
        entries.append({"target": str(target), "backup": str(backup / str(index)) if Path(target).exists() else None})
    journal = work_dir / JOURNAL
    journal.write_text(json.dumps({"entries": entries, "complete": False}), encoding="utf-8")
    for entry in entries:
        if entry["backup"]:
            shutil.copy2(entry["target"], entry["backup"])
    done = 0
    try:
        for staged, target in pairs:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, target)
            done += 1
    except BaseException as exc:
        _restore(entries[:done + 1])
        journal.unlink(missing_ok=True)
        raise PublicationError(f"could not move the outputs into place ({type(exc).__name__}: {exc}); "
                               "the previous outputs were restored") from exc
    journal.write_text(json.dumps({"entries": entries, "complete": True}), encoding="utf-8")


def _restore(entries: Sequence[Dict[str, Any]]) -> None:
    for entry in entries:
        target = Path(entry["target"])
        if entry["backup"] and Path(entry["backup"]).exists():
            os.replace(entry["backup"], target)
        elif not entry["backup"]:
            target.unlink(missing_ok=True)


def recover_interrupted(parent: Path) -> List[str]:
    """Roll back installs that never completed (a crash between moves) and remove stale staging folders.

    Call only while holding the publication lock (:func:`locked`). Returns the folders cleaned.
    """
    cleaned = []
    for staging in sorted(Path(parent).glob(".publish-*")):
        if not staging.is_dir():
            continue
        journal = staging / JOURNAL
        if journal.is_file():
            try:
                state = json.loads(journal.read_text(encoding="utf-8"))
            except ValueError:
                state = {"entries": [], "complete": False}
            if not state.get("complete"):
                logger.error("Rolling back an interrupted publication (%s)", staging.name)
                _restore(state.get("entries", []))
        shutil.rmtree(staging, ignore_errors=True)
        cleaned.append(staging.name)
    return cleaned


class locked:
    """One publication at a time: an exclusive lock file beside the output folder."""

    def __init__(self, parent: Path):
        self.path = Path(parent) / LOCK

    def __enter__(self) -> "locked":
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                age = datetime.now() - datetime.fromtimestamp(self.path.stat().st_mtime)
                if age < timedelta(hours=LOCK_STALE_HOURS):
                    raise PublicationError(f"another publication is running ({self.path}, started {age} ago); "
                                           "nothing was changed") from None
                logger.error("Taking over a stale publication lock (%s old)", age)
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w") as fh:
                fh.write(f"{os.getpid()} {datetime.now().isoformat()}\n")
            return self
        raise PublicationError(f"could not take the publication lock {self.path}")

    def __exit__(self, *exc: Any) -> None:
        self.path.unlink(missing_ok=True)


# --- builders ---------------------------------------------------------------------------

def build_dashboard(ctx: BuildContext) -> List[Path]:
    """``index.html`` and ``ai_incidents.html``."""
    from scripts.build_static_dashboard import build_dashboard_file

    built = build_dashboard_file(db_path=ctx.db, out_dir=str(ctx.dashboard_dir))
    problems = validate_dashboard(built)
    if problems:
        raise PublicationError("Dashboard failed validation, last published version kept: " + "; ".join(problems))
    return [ctx.dashboard_dir / "index.html", ctx.dashboard_dir / "ai_incidents.html"]


def build_entities(ctx: BuildContext) -> List[Path]:
    from scripts import build_entity_dashboard as module

    conn = module._connect(ctx.db)
    try:
        rows = module.load_rows(conn)
        stats = module.summarise(rows)
        note = module.population_note(conn, stats)
    finally:
        conn.close()
    path = ctx.dashboard_dir / "entities.html"
    path.write_text(module.build_html(rows, stats, note), encoding="utf-8")
    return [path]


def build_timelines(ctx: BuildContext) -> List[Path]:
    from scripts import build_timeline_dashboard as module

    conn = sqlite3.connect(ctx.db)
    try:
        orgs = module.load(conn, module.DEFAULT_TOP,
                           module.load_reconciliation(ctx.instance_dir / "timeline_reconciliation.json"))
    finally:
        conn.close()
    path = ctx.dashboard_dir / "timelines.html"
    path.write_text(module.build_html(orgs), encoding="utf-8")
    return [path]


def build_dedup(ctx: BuildContext) -> List[Path]:
    from scripts import build_dedup_dashboard as module

    conn = sqlite3.connect(ctx.db)
    try:
        data = module.collect(conn)
    finally:
        conn.close()
    path = ctx.dashboard_dir / "dedup.html"
    path.write_text(module.build_html(data), encoding="utf-8")
    return [path]


def build_recurrence(ctx: BuildContext) -> List[Path]:
    """``recurrence.html`` from the analysis folders, naming the snapshot each was computed on."""
    from scripts import build_recurrence_dashboard as module

    path = ctx.dashboard_dir / "recurrence.html"
    path.write_text(module.build_html(ctx.analysis_dir, published_snapshot_id=ctx.snapshot.snapshot_id),
                    encoding="utf-8")
    return [path]


def build_risk_matrices(ctx: BuildContext) -> List[Path]:
    """The ASD risk-matrix workbooks, from the snapshot's stored classifications (no model call is made)."""
    from scripts.asd_risk_classifier import ASDRiskClassifier

    # The classifier needs a key only to construct its OpenAI client; exporting a matrix sends nothing.
    classifier = ASDRiskClassifier(ctx.db, api_key=os.getenv("OPENAI_API_KEY") or "export-only-no-request-is-made")
    try:
        return [Path(p) for p in classifier.compile_risk_matrix(ctx.risk_dir)]
    finally:
        classifier.close()


DEFAULT_BUILDERS: Dict[str, Callable[[BuildContext], List[Path]]] = {
    "dashboard": build_dashboard,
    "entities": build_entities,
    "timelines": build_timelines,
    "dedup": build_dedup,
    "recurrence": build_recurrence,
    "risk_matrix": build_risk_matrices,
}


# --- orchestration -----------------------------------------------------------------------

def publish(db_path: Path, out_dir: Path, risk_dir: Path, *, analysis_dir: Path = Path("analysis"),
            skip_gate: bool = False, snapshot_id: Optional[str] = None,
            builders: Optional[Dict[str, Callable[[BuildContext], List[Path]]]] = None,
            store: Optional[snapshots.SnapshotStore] = None, repo_root: Optional[Path] = None,
            thresholds_path: Optional[Path] = None) -> PublicationResult:
    """Snapshot, gate, build, stamp and install every output (see the module docstring).

    ``snapshot_id`` rebuilds from a retained snapshot (an id, or ``latest-published``) instead of
    taking a new one: the outputs are then reproduced from exactly the data they were first built on.
    The gate runs on it all the same - the code and thresholds may have changed since.

    Raises:
        PublicationBlocked: the quality gate blocked; nothing was replaced.
        PublicationError: a builder, a validation or the install failed; nothing was replaced.
    """
    db_path, out_dir, risk_dir = Path(db_path), Path(out_dir), Path(risk_dir)
    if not db_path.is_file():
        raise PublicationError(f"database not found: {db_path}")
    repo_root = repo_root or Path.cwd()
    store = store or snapshots.store_for(db_path)
    instance_dir = db_path.resolve().parent
    builders = DEFAULT_BUILDERS if builders is None else builders
    parent = out_dir.resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    with locked(parent):
        recover_interrupted(parent)
        return _publish_locked(db_path, out_dir, risk_dir, parent, analysis_dir, skip_gate, snapshot_id, builders,
                               store, repo_root, thresholds_path, instance_dir)


def _publish_locked(db_path: Path, out_dir: Path, risk_dir: Path, parent: Path, analysis_dir: Path, skip_gate: bool,
                    snapshot_id: Optional[str], builders: Dict[str, Callable[[BuildContext], List[Path]]],
                    store: snapshots.SnapshotStore, repo_root: Path, thresholds_path: Optional[Path],
                    instance_dir: Path) -> PublicationResult:
    from cyber_data_collector.quality.gate import run_quality_checks, store_report, write_outputs
    from cyber_data_collector.quality.model import load_thresholds

    work = Path(tempfile.mkdtemp(prefix=".publish-", dir=parent))
    snap: Optional[snapshots.Snapshot] = None
    try:
        # 1. the snapshot, retained before anything reads it
        if snapshot_id:
            sid = store.resolve(snapshot_id)
            path = store.restore(sid, work)
            manifest = store.manifest(sid)
            snap = snapshots.Snapshot(sid, manifest["sha256"], manifest["taken_at"], manifest["source_database"],
                                      path, path.stat().st_size)
        else:
            snap = snapshots.take(db_path, work)
            store.retain(snap, status=snapshots.PENDING)
        logger.info("Publication snapshot %s (sha-256 %s)", snap.snapshot_id, snap.sha256[:12])

        # 2. the gate, on the snapshot; recorded in the live database
        quality_run_id, gate = None, "skipped (--skip-quality-gate)"
        if skip_gate:
            logger.warning("Quality gate SKIPPED (--skip-quality-gate): publishing unchecked")
        else:
            conn = sqlite3.connect(f"{snap.path.as_uri()}?mode=ro", uri=True)
            try:
                report = run_quality_checks(conn, load_thresholds(thresholds_path), snapshot=snap,
                                            db_path=str(db_path))
            finally:
                conn.close()
            write_outputs(report, instance_dir)
            writer = sqlite3.connect(db_path)
            try:
                store_report(writer, report)
            finally:
                writer.close()
            quality_run_id = report.run_id
            for reason in report.reasons:
                logger.error("Publication blocked: %s", reason)
            if not report.allowed:
                store.update(snap.snapshot_id, status=snapshots.BLOCKED, quality_run_id=report.run_id,
                             reasons=report.reasons)
                store.prune(snapshots.pinned_by_analyses(analysis_dir))
                raise PublicationBlocked(report.reasons, snap.snapshot_id)
            gate = "passed"

        # 3. build everything from the snapshot into staging
        ctx = BuildContext(snap, work / "dashboard", work / "risk_matrix", instance_dir, Path(analysis_dir),
                           db_path)
        ctx.dashboard_dir.mkdir()
        ctx.risk_dir.mkdir()
        produced: List[Path] = []
        for name, builder in builders.items():
            try:
                produced += [Path(p) for p in builder(ctx)]
            except PublicationError:
                raise
            except Exception as exc:
                raise PublicationError(f"{name} could not be built from snapshot {snap.snapshot_id}: "
                                       f"{type(exc).__name__}: {exc}") from exc
        missing = [str(p) for p in produced if not Path(p).is_file()]
        if missing:
            raise PublicationError(f"builders reported outputs that do not exist: {missing}")

        # 4. stamp and describe
        published_at = datetime.now().isoformat(timespec="seconds")
        for path in produced:
            if path.suffix == ".html":
                stamp_html(path, snap, published_at)
            elif path.suffix == ".xlsx":
                stamp_workbook(path, snap, published_at)
        outputs = []
        for path in produced:
            root = ctx.risk_dir if ctx.risk_dir in path.parents else ctx.dashboard_dir
            folder = risk_dir if root is ctx.risk_dir else out_dir
            outputs.append({"path": (folder / path.relative_to(root)).as_posix(), "sha256": file_sha256(path),
                            "bytes": path.stat().st_size})
        warnings = _analysis_warnings(ctx.analysis_dir, snap.snapshot_id)
        manifest_data = {
            "snapshot_id": snap.snapshot_id, "snapshot_sha256": snap.sha256, "snapshot_taken_at": snap.taken_at,
            "source_database": str(db_path.resolve()), "published_at": published_at,
            "quality_gate": gate, "quality_run_id": quality_run_id,
            "outputs": outputs, "external_inputs": external_inputs(db_path, repo_root),
            "analysis_snapshots": _analysis_snapshots(ctx.analysis_dir), "warnings": warnings,
            "code_version": code_version(repo_root),
            "reproduce": f"python scripts/publish_outputs.py --snapshot-id {snap.snapshot_id}",
        }
        manifest_path = ctx.dashboard_dir / MANIFEST
        manifest_path.write_text(json.dumps(manifest_data, indent=2) + "\n", encoding="utf-8")

        # 5. all into place, or nothing
        pairs = [(p, (risk_dir if ctx.risk_dir in p.parents else out_dir)
                  / p.relative_to(ctx.risk_dir if ctx.risk_dir in p.parents else ctx.dashboard_dir))
                 for p in produced]
        pairs.append((manifest_path, out_dir / MANIFEST))
        install(pairs, work)
        store.update(snap.snapshot_id, status=snapshots.PUBLISHED, published_at=published_at,
                     quality_run_id=quality_run_id, quality_gate=gate, outputs=[o["path"] for o in outputs])
        expired = store.prune(snapshots.pinned_by_analyses(analysis_dir))
        if expired:
            logger.info("Snapshot retention expired %d older copy(ies): %s", len(expired), ", ".join(expired))
        for warning in warnings:
            logger.warning("Publication: %s", warning)
        return PublicationResult(snap.snapshot_id, snap.sha256, True, [str(t) for _, t in pairs],
                                 quality_run_id, gate, warnings, str(out_dir / MANIFEST))
    except PublicationBlocked:
        raise
    except BaseException:
        try:
            if "snap" in locals() and store.manifest_path(snap.snapshot_id).is_file() and \
                    store.manifest(snap.snapshot_id).get("status") == snapshots.PENDING:
                store.update(snap.snapshot_id, status=snapshots.FAILED)
        except Exception:                                                  # noqa: BLE001 - bookkeeping only
            logger.exception("Could not mark the snapshot failed")
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _analysis_snapshots(analysis_dir: Path) -> Dict[str, Optional[str]]:
    found: Dict[str, Optional[str]] = {}
    if Path(analysis_dir).is_dir():
        for path in sorted(Path(analysis_dir).iterdir()):
            if path.is_dir() and (path.name.startswith("recurrent_timing")):
                inputs = path / snapshots.ANALYSIS_INPUTS
                try:
                    found[path.name] = json.loads(inputs.read_text(encoding="utf-8")).get("snapshot_id") \
                        if inputs.is_file() else None
                except ValueError:
                    found[path.name] = None
    return found


def _analysis_warnings(analysis_dir: Path, published_id: str) -> List[str]:
    """Analyses (shown on recurrence.html) computed on another snapshot than the one published."""
    out = []
    for folder, sid in _analysis_snapshots(analysis_dir).items():
        if sid is None:
            out.append(f"analysis/{folder} does not record the snapshot it was computed on")
        elif sid != published_id:
            out.append(f"analysis/{folder} was computed on snapshot {sid}, not the published {published_id}; "
                       "rerun it with --snapshot-id latest-published")
    return out
