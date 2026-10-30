"""Labelling *frames*: one calibration sample drawn from more than one labelling snapshot.

Labels describe the database as it was when they were made, and acting on a step (filling, splitting) removes
items from the population it is calibrated on, so a later snapshot cannot replace an earlier one. A sample can
therefore hold several frames (column ``frame``; frame ``1`` is the script's ``--db``):

* frame ``F``'s population is the step's population in *its* snapshot minus every subject that was in an earlier
  frame's population (in that frame's snapshot), so the frames' populations are disjoint;
* frame ``F``'s strata are named ``F`` + band (``2A``) and weighted back to its own population, so every row's
  weight is the number of subjects of the union of the frames' populations it stands for;
* every row is scored and calibrated on its own frame's snapshot (``--frame-db F=PATH``); a frame with labelled
  rows and no database is an error, never skipped.

The step-specific parts (the population, the bands, the score) stay in each calibration script; this module holds
what is the same for all of them (``scripts/calibrate_victim_fill.py``, ``scripts/calibrate_coherence_same_org.py``).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence

PRIMARY_FRAME = "1"


def row_frame(row: Mapping[str, Optional[str]]) -> str:
    """A sample row's frame; a row written before frames existed belongs to frame 1."""
    return (row.get("frame") or PRIMARY_FRAME).strip()


def stratum_name(frame: str, band: str) -> str:
    """Frame 1 keeps its bands' bare names (the rows written before frames); a later frame prefixes its number."""
    return band if frame == PRIMARY_FRAME else f"{frame}{band}"


def parse_frame_dbs(values: Optional[Sequence[str]]) -> Dict[str, str]:
    """``["2=path", ...]`` -> ``{"2": "path"}``; a malformed or repeated entry raises."""
    out: Dict[str, str] = {}
    for value in values or ():
        frame, sep, path = value.partition("=")
        frame, path = frame.strip(), path.strip()
        if not sep or not frame or not path:
            raise ValueError(f"expected FRAME=PATH, got {value!r}")
        if frame in out:
            raise ValueError(f"frame {frame} given twice")
        out[frame] = path
    return out


def frame_databases(primary: str, frame_dbs: Mapping[str, str], needed: Sequence[str]) -> Dict[str, str]:
    """The database of every frame in ``needed``: frame 1 is ``primary``; any other must be given."""
    paths = {PRIMARY_FRAME: primary, **frame_dbs}
    missing = sorted(f for f in set(needed) if f not in paths)
    if missing:
        raise ValueError(f"labelled rows of frame(s) {', '.join(missing)} need their snapshot: pass "
                         + " ".join(f"--frame-db {f}=<path>" for f in missing))
    return {f: paths[f] for f in needed}


def earlier_connections(frame: str, given_values: Optional[Sequence[str]],
                        existing_frames: Sequence[str]) -> Dict[str, sqlite3.Connection]:
    """Read-only connections to every earlier frame's snapshot; every frame already in the sample must be given."""
    given = parse_frame_dbs(given_values)
    missing = sorted(set(existing_frames) - set(given))
    if missing:
        raise SystemExit(f"frame(s) {', '.join(missing)} are already in the sample: pass "
                         + " ".join(f"--earlier-frame-db {f}=<its snapshot>" for f in missing)
                         + f" so frame {frame}'s population excludes theirs")
    out = {}
    for name, path in given.items():
        if not Path(path).is_file():
            raise SystemExit(f"snapshot for frame {name} not found: {path}")
        out[name] = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    return out
