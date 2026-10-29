"""Consistent, retained snapshots of the database: the input every published result names.

The dashboard, the quality gate, the risk matrices and the analyses used to
read the live database separately, so a result could not be tied to the data
it was computed from: two outputs of one run could disagree, and the analysis
kept only a hash of a temporary copy that was then deleted.

:func:`take` copies the database with SQLite's online backup API (one
consistent view, committed WAL data included), checks it, and names it
``snap-<time>-<first 12 hex of the sha-256 of the file>``. :class:`SnapshotStore`
keeps a compressed copy and a manifest under ``instance/snapshots/``:

``<id>.db.gz``    the exact bytes that were hashed (gzip, level 6: a 435 MB
                  database is about 100 MB and takes about 8 seconds)
``<id>.json``     the manifest: id, sha-256, size, when, from which database,
                  what it is for (``published`` / ``blocked`` / ``analysis`` /
                  ``failed``), the quality run and the outputs built from it

:meth:`SnapshotStore.restore` decompresses a snapshot and verifies it against
the manifest hash, so a result that names a snapshot id can be reproduced.

**Retention.** Disk is the constraint, so copies are kept by role: the last
``KEEP_PUBLISHED`` (5) published snapshots, the last ``KEEP_OTHER`` (2) of
each other kind (a blocked or failed publication is kept to diagnose it), and
any snapshot an analysis result names in ``analysis_inputs.json`` (*pinned*:
those results are committed, and are only reproducible while their input
exists). Older copies are deleted but their manifests stay, with status
``expired``, so an id in an old output still resolves to the hash that proves
which data it was. At 100 MB a copy, 5 + 2 + 2 + a few pinned analysis
snapshots is 0.8 to 1.2 GB.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

KEEP_PUBLISHED = 5
KEEP_OTHER = 2
COMPRESS_LEVEL = 6
CHUNK = 1024 * 1024

PUBLISHED = "published"
BLOCKED = "blocked"
FAILED = "failed"
ANALYSIS = "analysis"
PENDING = "pending"
EXPIRED = "expired"
LATEST_PUBLISHED = "latest-published"

ANALYSIS_INPUTS = "analysis_inputs.json"


class SnapshotError(RuntimeError):
    """A snapshot could not be taken, retained or restored."""


@dataclass
class Snapshot:
    """A database copy on disk, with the identity it was given."""

    snapshot_id: str
    sha256: str
    taken_at: str
    source: str
    path: Path
    size_bytes: int

    def to_dict(self) -> Dict[str, Any]:
        return {"snapshot_id": self.snapshot_id, "sha256": self.sha256, "taken_at": self.taken_at,
                "source_database": self.source, "size_bytes": self.size_bytes}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_id(taken_at: datetime, sha256: str) -> str:
    return f"snap-{taken_at:%Y%m%d%H%M%S}-{sha256[:12]}"


def take(source: Path, destination_dir: Path, now: Optional[datetime] = None) -> Snapshot:
    """Copy ``source`` with the SQLite backup API into ``destination_dir`` and name the copy.

    The source is opened read-only and never modified. The copy must pass
    ``PRAGMA quick_check`` or ``SnapshotError`` is raised (and the copy removed).
    """
    source = Path(source).resolve()
    if not source.is_file():
        raise SnapshotError(f"database does not exist: {source}")
    destination_dir = Path(destination_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now()
    temporary = destination_dir / f".snapshot-{os.getpid()}-{now:%H%M%S%f}.db"
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    try:
        src.execute("PRAGMA query_only = ON")
        dst = sqlite3.connect(temporary)
        try:
            src.backup(dst)
            verdict = dst.execute("PRAGMA quick_check").fetchone()[0]
            if verdict != "ok":
                raise SnapshotError(f"snapshot of {source} failed quick_check: {verdict}")
            # Single-file database: nothing left in a journal or WAL to be lost with the copy.
            dst.execute("PRAGMA journal_mode = DELETE")
        finally:
            dst.close()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        src.close()
    digest = file_sha256(temporary)
    sid = snapshot_id(now, digest)
    final = destination_dir / f"{sid}.db"
    os.replace(temporary, final)
    return Snapshot(sid, digest, now.isoformat(timespec="seconds"), str(source), final, final.stat().st_size)


class SnapshotStore:
    """Compressed, retained snapshots and their manifests in one directory."""

    def __init__(self, root: Path):
        self.root = Path(root)

    # --- manifests ---------------------------------------------------------------
    def manifest_path(self, sid: str) -> Path:
        return self.root / f"{sid}.json"

    def copy_path(self, sid: str) -> Path:
        return self.root / f"{sid}.db.gz"

    def manifest(self, sid: str) -> Dict[str, Any]:
        path = self.manifest_path(sid)
        if not path.is_file():
            raise SnapshotError(f"unknown snapshot {sid!r}: no manifest {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def manifests(self) -> List[Dict[str, Any]]:
        """Every manifest, newest first."""
        if not self.root.is_dir():
            return []
        out = []
        for path in self.root.glob("snap-*.json"):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except ValueError:
                logger.error("Unreadable snapshot manifest %s", path)
        return sorted(out, key=lambda m: (m.get("taken_at", ""), m.get("snapshot_id", "")), reverse=True)

    def update(self, sid: str, **fields: Any) -> Dict[str, Any]:
        """Merge ``fields`` into a manifest (atomic rewrite)."""
        manifest = {**self.manifest(sid), **fields}
        self._write(sid, manifest)
        return manifest

    def _write(self, sid: str, manifest: Dict[str, Any]) -> None:
        path = self.manifest_path(sid)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    # --- retain / restore ---------------------------------------------------------
    def retain(self, snap: Snapshot, status: str = PENDING, **extra: Any) -> Dict[str, Any]:
        """Store the compressed copy and the manifest. Idempotent for one snapshot id."""
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.copy_path(snap.snapshot_id)
        if not target.is_file():
            temporary = target.with_suffix(".gz.tmp")
            with open(snap.path, "rb") as src, gzip.open(temporary, "wb", compresslevel=COMPRESS_LEVEL) as dst:
                shutil.copyfileobj(src, dst, CHUNK)
            os.replace(temporary, target)
        manifest = {**snap.to_dict(), "file": target.name, "compressed_bytes": target.stat().st_size,
                    "status": status, "retained_at": datetime.now().isoformat(timespec="seconds"), **extra}
        if self.manifest_path(snap.snapshot_id).is_file():
            manifest = {**self.manifest(snap.snapshot_id), **{k: v for k, v in manifest.items()
                                                                if k not in ("retained_at",)}}
        self._write(snap.snapshot_id, manifest)
        return manifest

    def restore(self, sid: str, destination_dir: Path) -> Path:
        """Decompress snapshot ``sid`` into ``destination_dir`` and verify it against its manifest hash.

        ``latest-published`` names the newest published snapshot whose copy is still retained.
        """
        sid = self.resolve(sid)
        manifest = self.manifest(sid)
        copy = self.copy_path(sid)
        if not copy.is_file():
            raise SnapshotError(f"snapshot {sid} has expired: its copy was removed by the retention policy "
                                f"(manifest kept; sha-256 {manifest.get('sha256')})")
        destination_dir = Path(destination_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)
        target = destination_dir / f"{sid}.db"
        with gzip.open(copy, "rb") as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst, CHUNK)
        digest = file_sha256(target)
        if digest != manifest.get("sha256"):
            target.unlink(missing_ok=True)
            raise SnapshotError(f"snapshot {sid} is corrupt: restored sha-256 {digest} != manifest "
                                f"{manifest.get('sha256')}")
        return target

    def resolve(self, sid: str) -> str:
        if sid != LATEST_PUBLISHED:
            return sid
        for manifest in self.manifests():
            if manifest.get("status") == PUBLISHED and self.copy_path(manifest["snapshot_id"]).is_file():
                return manifest["snapshot_id"]
        raise SnapshotError(f"no published snapshot is retained in {self.root}")

    # --- retention -----------------------------------------------------------------
    def prune(self, pinned: Iterable[str] = (), keep_published: int = KEEP_PUBLISHED,
              keep_other: int = KEEP_OTHER) -> List[str]:
        """Delete copies the policy no longer keeps; returns the ids expired. Manifests are kept."""
        pinned = set(pinned)
        kept: Dict[str, int] = {}
        expired = []
        for manifest in self.manifests():                    # newest first
            sid = manifest["snapshot_id"]
            copy = self.copy_path(sid)
            if not copy.is_file():
                continue
            kind = manifest.get("status", PENDING)
            limit = keep_published if kind == PUBLISHED else keep_other
            kept[kind] = kept.get(kind, 0) + 1
            if sid in pinned or kept[kind] <= limit:
                continue
            copy.unlink()
            self.update(sid, status=EXPIRED, expired_status=kind, expired_at=datetime.now().isoformat(
                timespec="seconds"))
            expired.append(sid)
        return expired

    def disk_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.root.glob("snap-*.db.gz")) if self.root.is_dir() else 0


def pinned_by_analyses(analysis_dir: Path) -> List[str]:
    """Snapshot ids named by any ``analysis_inputs.json`` under ``analysis_dir``."""
    ids = []
    if Path(analysis_dir).is_dir():
        for path in Path(analysis_dir).rglob(ANALYSIS_INPUTS):
            try:
                sid = json.loads(path.read_text(encoding="utf-8")).get("snapshot_id")
            except ValueError:
                continue
            if sid:
                ids.append(sid)
    return ids


def store_for(db_path: Path) -> SnapshotStore:
    """The store beside a database: ``<database folder>/snapshots``."""
    return SnapshotStore(Path(db_path).resolve().parent / "snapshots")


class AnalysisInput:
    """The database snapshot an analysis runs on, retained so the analysis can be reproduced.

    ``AnalysisInput.open(db, out_dir, snapshot_id=None)``:

    * with ``snapshot_id`` (an id, or ``latest-published``) the retained snapshot is restored and used,
      so several analyses - the main run, its bandings, the extended analyses - share one input;
    * otherwise the live database is snapshotted and the snapshot retained (status ``analysis``).

    ``path`` is the database to read; ``record(out_dir, **extra)`` writes ``analysis_inputs.json``
    beside the outputs; ``close()`` removes the working copy (the retained one stays).
    """

    def __init__(self, snap: Snapshot, manifest: Dict[str, Any], workdir: tempfile.TemporaryDirectory,
                 store: SnapshotStore, reused: bool):
        self.snapshot = snap
        self.manifest = manifest
        self.path = snap.path
        self.store = store
        self.reused = reused
        self._workdir = workdir
        self.reconstruction = None          # HistoryReconstruction once reconstruct() has rewound the working copy

    @classmethod
    def open(cls, db_path: Path, snapshot_id: Optional[str] = None,
             store: Optional[SnapshotStore] = None) -> "AnalysisInput":
        store = store or store_for(db_path)
        workdir = tempfile.TemporaryDirectory(prefix="cyber-analysis-snapshot-")
        try:
            if snapshot_id:
                sid = store.resolve(snapshot_id)
                path = store.restore(sid, Path(workdir.name))
                manifest = store.manifest(sid)
                snap = Snapshot(sid, manifest["sha256"], manifest["taken_at"], manifest["source_database"], path,
                                path.stat().st_size)
                return cls(snap, manifest, workdir, store, reused=True)
            snap = take(Path(db_path), Path(workdir.name))
            manifest = store.retain(snap, status=ANALYSIS)
            return cls(snap, manifest, workdir, store, reused=False)
        except BaseException:
            workdir.cleanup()
            raise

    def reconstruct(self, as_known_at: Any) -> Any:
        """Rewind the working copy to what was known at ``as_known_at`` (``dedup/input_history.py``).

        Only the working copy changes; the retained snapshot is untouched, so the snapshot id plus this date
        reproduce the reconstruction. Returns the :class:`HistoryReconstruction` (also kept as
        ``self.reconstruction`` and written by :meth:`record`). Raises ``HistoryError`` when the snapshot holds
        no history to reconstruct from.
        """
        from cyber_data_collector.dedup import input_history

        if self.reconstruction is not None:
            raise SnapshotError("this analysis input has already been reconstructed to "
                                f"{self.reconstruction.as_known_at}")
        self.reconstruction = input_history.rewind_database(self.path, as_known_at)
        return self.reconstruction

    def record(self, out_dir: Path, **extra: Any) -> Path:
        """Write ``analysis_inputs.json`` beside an analysis's outputs and keep this snapshot pinned."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / ANALYSIS_INPUTS
        reproduce = f"--snapshot-id {self.snapshot.snapshot_id}"
        reconstruction = {}
        if self.reconstruction is not None:
            reproduce += f" --as-known-at {self.reconstruction.as_known_at}"
            reconstruction = {"as_known_at": self.reconstruction.as_known_at,
                              "history_reconstruction": self.reconstruction.to_dict()}
        payload = {"snapshot_id": self.snapshot.snapshot_id, "snapshot_sha256": self.snapshot.sha256,
                   "snapshot_taken_at": self.snapshot.taken_at, "source_database": self.snapshot.source,
                   "retained_copy": f"{self.store.root.name}/{self.manifest.get('file')}",
                   "reproduce": reproduce, "recorded_at": datetime.now().isoformat(timespec="seconds"),
                   **reconstruction, **extra}
        path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        return path

    def close(self) -> None:
        """Remove the working copy. Windows can hold a just-written file for a moment (a scanner, a
        handle not yet released), so removal is retried; a copy that still cannot be removed is
        logged and left in the temporary folder - the retained snapshot is unaffected."""
        for attempt in range(5):
            try:
                self._workdir.cleanup()
                return
            except OSError as exc:
                if attempt == 4:
                    logger.warning("Could not remove the working copy of snapshot %s: %s",
                                   self.snapshot.snapshot_id, exc)
                    return
                time.sleep(0.3)

    def __enter__(self) -> "AnalysisInput":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
