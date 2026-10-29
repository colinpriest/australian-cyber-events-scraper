"""Labelled-different incidents that ended up in one event: refuse the merge, undo the ones that happened.

``evaluation/duplicate_labels.csv`` records, for every pair of events about one
organisation, whether they are the same incident or two. A label is ground
truth, but only the calibrated duplicate check consulted it: the GPT
recurrence check merged two labelled-*different* pairs on 2026-10-28 at
certainty 0.90 and 0.98 (Western Sydney University's August 2024 and January
2025 breaches; youX's March 2025 S3 exposure and February 2026 MongoDB theft),
and the quality gate flagged them as label contradictions - a warning nobody
acted on.

Two things close that:

* **Prevention.** :meth:`DedupLedger.merge_events` calls :func:`conflict` and
  refuses (``ValueError``, like a cached ``different`` ruling) to join two
  events when a labelled-different pair resolves to one on each side. Every
  merge path that respects rulings now respects the labels.
* **Repair.** :func:`resolve` finds labelled-different pairs that now resolve to
  one Active event (the quality gate's definition, via
  :class:`quality.regression.EventResolver`) and splits them apart through the
  ledger - snapshotted, reversible, with ``different`` rulings cached between
  the two sides so no later pass re-merges them. Which records belong to which
  side is read from the database the pair was labelled on (the labelling-time
  snapshot of ``evaluation/calibration_snapshots.json``): the members of each
  labelled event then, intersected with the event's active records now. The
  smaller side leaves (a tie: the second event of the pair id), together, as
  one new event.

Limits: the labelled events could themselves hold strays or misdated copies
(WSU's second event held a record dated August 2024), and those travel with
their side; the same-organisation coherence check and the duplicate check
re-home them afterwards. Records that arrived after labelling belong to neither
side and stay. A contradiction whose sides cannot be identified (a side with no
active records left, a record claimed by both) is reported, not guessed.
"""

from __future__ import annotations

import csv
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from cyber_data_collector.dedup.event_reality import ROOT

logger = logging.getLogger(__name__)

LABELS_PATH = ROOT / "evaluation" / "duplicate_labels.csv"


class LabelsUnreadable(RuntimeError):
    """The duplicate labels could not be read: an unreadable file must not read as 'no labels'."""


def labelled_different_pairs(path: Optional[Path] = None) -> List[Tuple[str, str]]:
    """The labelled-different pairs, as ordered ``(a, b)`` event ids."""
    path = Path(path or LABELS_PATH)
    try:
        with Path(path).open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        raise LabelsUnreadable(f"duplicate labels {path} unreadable: {exc}") from exc
    pairs = []
    for row in rows:
        if (row.get("label") or "").strip() != "different":
            continue
        parts = (row.get("pair_id") or "").strip().split("|")
        if len(parts) == 2 and all(parts):
            pairs.append((parts[0], parts[1]))
    return pairs


def ai_labelled_different_pairs() -> List[Tuple[str, str]]:
    """The labelled-different pairs of AI-involved events (``evaluation/ai_duplicate_labels.csv``).

    Not labelled yet (no file) reads as none. Prevention only: the ledger refuses a merge that joins such a
    pair, whichever route proposes it; repairing an existing contradiction needs the labelling snapshot of
    that file and is not done here.
    """
    from cyber_data_collector.dedup import ai_duplicates

    if not Path(ai_duplicates.LABELS_PATH).exists():
        return []
    return labelled_different_pairs(ai_duplicates.LABELS_PATH)


def _resolver(conn: sqlite3.Connection):
    from cyber_data_collector.quality.regression import EventResolver

    previous = conn.row_factory
    conn.row_factory = None
    try:
        return EventResolver(conn)
    finally:
        conn.row_factory = previous


def conflict(conn: sqlite3.Connection, target: str, source: str, path: Optional[Path] = None) -> Optional[str]:
    """Why joining ``source`` into ``target`` contradicts a label, or None.

    A labelled-different pair conflicts when one of its events resolves to
    ``target`` and the other to ``source`` (through merges already made).
    """
    explicit = path is not None
    path = Path(path or LABELS_PATH)
    pairs = [(a, b, path.name) for a, b in labelled_different_pairs(path)]
    if not explicit:
        from cyber_data_collector.dedup import ai_duplicates
        pairs += [(a, b, ai_duplicates.LABELS_PATH.name) for a, b in ai_labelled_different_pairs()]
    if not pairs:
        return None
    resolver = _resolver(conn)
    for a, b, name in pairs:
        (a_now, a_status), (b_now, b_status) = resolver.resolve(a), resolver.resolve(b)
        if a_status != "Active" or b_status != "Active":
            continue
        if {a_now, b_now} == {target, source} and a_now != b_now:
            return f"labelled different: {a[:8]} vs {b[:8]} ({name})"
    return None


def blocked_pairs(conn: sqlite3.Connection, path: Optional[Path] = None) -> Dict[frozenset, str]:
    """Pairs of Active events a label forbids joining: ``{frozenset({a, b}): reason}``.

    Every labelled-different pair (duplicate labels, and the AI-event labels when ``path`` is not given) whose two
    events resolve, through merges already made, to two different Active events. Computed once, for a pass that
    asks many "may these join" questions; :meth:`DedupLedger.merge_events` still checks each merge against the
    labels as they stand when it runs (:func:`conflict`).
    """
    explicit = path is not None
    path = Path(path or LABELS_PATH)
    pairs = [(a, b, path.name) for a, b in labelled_different_pairs(path)]
    if not explicit:
        from cyber_data_collector.dedup import ai_duplicates
        pairs += [(a, b, ai_duplicates.LABELS_PATH.name) for a, b in ai_labelled_different_pairs()]
    if not pairs:
        return {}
    resolver = _resolver(conn)
    blocked: Dict[frozenset, str] = {}
    for a, b, name in pairs:
        (a_now, a_status), (b_now, b_status) = resolver.resolve(a), resolver.resolve(b)
        if a_status == "Active" and b_status == "Active" and a_now != b_now:
            blocked.setdefault(frozenset((a_now, b_now)), f"labelled different: {a[:8]} vs {b[:8]} ({name})")
    return blocked


@dataclass
class Contradiction:
    pair_id: str
    a: str
    b: str
    event: str                      # the one Active event both now resolve to
    side_a: List[str] = field(default_factory=list)       # enriched ids of the event that came from a
    side_b: List[str] = field(default_factory=list)
    leaving: List[str] = field(default_factory=list)
    unresolved: Optional[str] = None
    new_event: Optional[str] = None


def find(conn: sqlite3.Connection, path: Optional[Path] = None) -> List[Contradiction]:
    """Labelled-different pairs that now resolve to one Active event."""
    resolver = _resolver(conn)
    found = []
    for a, b in labelled_different_pairs(path):
        (a_now, a_status), (b_now, b_status) = resolver.resolve(a), resolver.resolve(b)
        if a_status == "Active" and b_status == "Active" and a_now == b_now:
            found.append(Contradiction(f"{a}|{b}", a, b, a_now))
    return found


def _snapshot_members(snapshot: Path, event_ids: Sequence[str]) -> Dict[str, Set[str]]:
    conn = sqlite3.connect(f"file:{Path(snapshot).as_posix()}?mode=ro", uri=True)
    try:
        return {e: {r[0] for r in conn.execute(
            "SELECT enriched_event_id FROM EventDeduplicationMap WHERE deduplicated_event_id = ?", (e,))}
            for e in event_ids}
    finally:
        conn.close()


def _active_members(conn: sqlite3.Connection, dedup_id: str) -> Set[str]:
    from cyber_data_collector.processing.active_evidence import member_condition

    return {r[0] for r in conn.execute(
        f"""SELECT m.enriched_event_id FROM EventDeduplicationMap m
            JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
            WHERE m.deduplicated_event_id = ? {member_condition(conn)}""", (dedup_id,))}


def plan_split(conn: sqlite3.Connection, c: Contradiction, labelled_members: Dict[str, Set[str]]) -> Contradiction:
    """Fill ``side_a`` / ``side_b`` / ``leaving`` (or ``unresolved``) from the labelling-time membership."""
    live = _active_members(conn, c.event)
    c.side_a = sorted(live & labelled_members.get(c.a, set()))
    c.side_b = sorted(live & labelled_members.get(c.b, set()))
    if set(c.side_a) & set(c.side_b):
        c.unresolved = "a record belonged to both labelled events"
    elif not c.side_a or not c.side_b:
        c.unresolved = ("no active record of the labelled event "
                        f"{(c.a if not c.side_a else c.b)[:8]} remains in the merged event")
    else:
        c.leaving = c.side_b if len(c.side_b) <= len(c.side_a) else c.side_a
        if len(c.leaving) >= len(live):
            c.unresolved = "splitting would leave the event with no records"
            c.leaving = []
    return c


def resolve(conn: sqlite3.Connection, snapshot: Optional[Path], apply: bool = False, ledger=None,
            path: Optional[Path] = None) -> List[Contradiction]:
    """Split every labelled-different pair that is now one event; ``apply`` False only plans.

    ``snapshot`` is the database the pairs were labelled on; None makes every contradiction
    unresolved (it is still listed). Each split is a ledger split: snapshotted, reversible,
    'different' rulings cached between the departing records and those that stay.
    """
    from cyber_data_collector.dedup.ledger import DedupLedger

    contradictions = find(conn, path)
    if not contradictions:
        return []
    members = _snapshot_members(snapshot, sorted({e for c in contradictions for e in (c.a, c.b)})) \
        if snapshot else {}
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row      # the ledger reads columns by name
    try:
        ledger = ledger or DedupLedger(conn)
        for c in contradictions:
            # An earlier split in this run may have separated this pair already.
            if not find_still(conn, c):
                continue
            if not snapshot:
                c.unresolved = "no labelling-time snapshot to say which records belong to which labelled event"
                continue
            plan_split(conn, c, members)
            if c.unresolved or not apply:
                continue
            reason = (f"[label-contradiction] labelled different incidents ({c.a[:8]} vs {c.b[:8]}) but merged into "
                      f"one event; the {len(c.leaving)} record(s) of the "
                      f"{'second' if c.leaving == c.side_b else 'first'} labelled event leave together")
            c.new_event = ledger.split_members(c.event, c.leaving, reason=reason, actor="rule")
        if apply:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.row_factory = previous
    return contradictions


def find_still(conn: sqlite3.Connection, c: Contradiction) -> List[Contradiction]:
    """``[c]`` while its pair still resolves to one Active event, else ``[]``."""
    resolver = _resolver(conn)
    (a_now, a_status), (b_now, b_status) = resolver.resolve(c.a), resolver.resolve(c.b)
    if a_status == "Active" and b_status == "Active" and a_now == b_now:
        c.event = a_now
        return [c]
    return []
