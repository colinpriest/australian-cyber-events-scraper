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

* **Labelled-same pairs still apart.** :func:`find_same` finds pairs labelled
  *same* whose two sides are two Active events, and :func:`resolve` merges the
  smaller event into the larger through the ledger - only when every active
  record of the event merged away belonged to a labelled event (otherwise the
  extra records' incident is not labelled and the pair is reported). Nothing
  else merged these: the duplicate check only asks about pairs its candidate
  generation proposes, and a labelled event with no victim link (a generic
  sector article labelled as covering one company's incident) is never one.
  A merge blocked only by the source gate's own rulings ("page does not report
  an incident", made when it split a junk page out of the event) takes that
  page out - the gate's verdict - and merges the event: the label says the
  event is the incident, the gate says the page is no evidence of it, and both
  hold. Any other 'different' ruling (a person's, a model's) blocks the merge
  and is reported.

**Where a labelled event is now.** An event's id is followed through merges by
its records, but a labelled event can be split as well as merged: Optus's
labelled "White Pages (2019)" event had seven records, four of which are now in
the September 2022 event and its lead record (about the 2015 White Pages
finding) alone. Following the lead record read that as "still two events".
The records each labelled event held when labelled are kept in
``evaluation/duplicate_label_members.csv`` (written from the labelling-time
snapshot by ``resolve-label-contradictions --record-members``), and a labelled
event resolves to the Active event that now holds a strict majority of those
of its records still active (:class:`SideResolver`); no majority is
*ambiguous* and not checked. An event the file does not list is followed as
before. The quality gate reads labels the same way.

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
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from cyber_data_collector.dedup.event_reality import ROOT

logger = logging.getLogger(__name__)

LABELS_PATH = ROOT / "evaluation" / "duplicate_labels.csv"
MEMBERS_PATH = ROOT / "evaluation" / "duplicate_label_members.csv"


class LabelsUnreadable(RuntimeError):
    """The duplicate labels could not be read: an unreadable file must not read as 'no labels'."""


def labelled_different_pairs(path: Optional[Path] = None) -> List[Tuple[str, str]]:
    """The labelled-different pairs, as ordered ``(a, b)`` event ids."""
    return labelled_pairs(path, "different")


def labelled_same_pairs(path: Optional[Path] = None) -> List[Tuple[str, str]]:
    """The labelled-same pairs, as ordered ``(a, b)`` event ids."""
    return labelled_pairs(path, "same")


def labelled_pairs(path: Optional[Path], label: str) -> List[Tuple[str, str]]:
    path = Path(path or LABELS_PATH)
    try:
        with Path(path).open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        raise LabelsUnreadable(f"duplicate labels {path} unreadable: {exc}") from exc
    pairs = []
    for row in rows:
        if (row.get("label") or "").strip() != label:
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


def labelled_members(path: Optional[Path] = None) -> Dict[str, Set[str]]:
    """``{labelled event id: its enriched ids when labelled}`` from the members file; none when it is absent."""
    path = Path(path or MEMBERS_PATH)
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        raise LabelsUnreadable(f"labelled-event members {path} unreadable: {exc}") from exc
    members: Dict[str, Set[str]] = {}
    for row in rows:
        event_id = (row.get("deduplicated_event_id") or "").strip()
        record = (row.get("enriched_event_id") or "").strip()
        if event_id and record:
            members.setdefault(event_id, set()).add(record)
    return members


def record_members(snapshot: Path, labels_path: Optional[Path] = None, out_path: Optional[Path] = None) -> int:
    """Write the labelling-time records of every labelled event to the members file; the rows written.

    Idempotent: the file is rewritten from the snapshot, sorted.
    """
    labels_path, out_path = Path(labels_path or LABELS_PATH), Path(out_path or MEMBERS_PATH)
    events = sorted({e for label in ("same", "different") for pair in labelled_pairs(labels_path, label)
                     for e in pair})
    members = _snapshot_members(snapshot, events)
    rows = sorted((e, r) for e, records in members.items() for r in records)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["deduplicated_event_id", "enriched_event_id"])
        writer.writerows(rows)
    return len(rows)


class SideResolver:
    """Where a labelled event is now: the Active event holding a strict majority of its labelling-time records.

    An event the members file does not list, or none of whose recorded records is still active, is followed
    through merges by :class:`quality.regression.EventResolver`.
    """

    def __init__(self, conn: sqlite3.Connection, members: Optional[Dict[str, Set[str]]] = None):
        from cyber_data_collector.processing.active_evidence import member_condition
        from cyber_data_collector.quality.regression import EventResolver

        previous = conn.row_factory
        conn.row_factory = None
        try:
            self.events = EventResolver(conn)
            self.members = labelled_members() if members is None else members
            self.active_event: Dict[str, Set[str]] = {}
            if self.members:
                for record, event_id in conn.execute(
                        f"""SELECT m.enriched_event_id, m.deduplicated_event_id FROM EventDeduplicationMap m
                            JOIN EnrichedEvents e ON e.enriched_event_id = m.enriched_event_id
                            JOIN DeduplicatedEvents d ON d.deduplicated_event_id = m.deduplicated_event_id
                            WHERE d.status = 'Active' {member_condition(conn)}"""):
                    self.active_event.setdefault(record, set()).add(event_id)
        finally:
            conn.row_factory = previous

    def resolve(self, event_id: str) -> Tuple[Optional[str], str]:
        recorded = self.members.get(event_id)
        if recorded:
            held = Counter(e for r in recorded for e in self.active_event.get(r, ()))
            if held:
                (top, count), = held.most_common(1)
                return (top, "Active") if count * 2 > sum(held.values()) else (None, "ambiguous")
        return self.events.resolve(event_id)


def _resolver(conn: sqlite3.Connection):
    return SideResolver(conn)


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
    kind: str = "different"         # 'same': a labelled-same pair still two events, ``event`` the target
    source: Optional[str] = None    # 'same': the event merged into ``event``


def find(conn: sqlite3.Connection, path: Optional[Path] = None) -> List[Contradiction]:
    """Labelled-different pairs that now resolve to one Active event."""
    resolver = _resolver(conn)
    found = []
    for a, b in labelled_different_pairs(path):
        (a_now, a_status), (b_now, b_status) = resolver.resolve(a), resolver.resolve(b)
        if a_status == "Active" and b_status == "Active" and a_now == b_now:
            found.append(Contradiction(f"{a}|{b}", a, b, a_now))
    return found


def find_same(conn: sqlite3.Connection, path: Optional[Path] = None) -> List[Contradiction]:
    """Labelled-same pairs whose sides are two Active events (target chosen by :func:`plan_merge`)."""
    resolver = _resolver(conn)
    found = []
    for a, b in labelled_same_pairs(path):
        (a_now, a_status), (b_now, b_status) = resolver.resolve(a), resolver.resolve(b)
        if a_status == "Active" and b_status == "Active" and a_now != b_now:
            found.append(Contradiction(f"{a}|{b}", a, b, a_now, kind="same", source=b_now))
    return found


def plan_merge(conn: sqlite3.Connection, c: Contradiction, labelled: Dict[str, Set[str]]) -> Contradiction:
    """Target the event with more active records; the one merged away must hold only labelled records."""
    live_a, live_b = _active_members(conn, c.event), _active_members(conn, c.source)
    if len(live_b) > len(live_a):
        c.event, c.source, live_a, live_b = c.source, c.event, live_b, live_a
    c.side_a, c.side_b = sorted(live_a), sorted(live_b)
    labelled_records = labelled.get(c.a, set()) | labelled.get(c.b, set())
    if not labelled_records:
        c.unresolved = "no record of which records the labelled events held (members file or snapshot)"
        return c
    extra = live_b - labelled_records
    if extra:
        c.unresolved = (f"event {c.source[:8]} also holds {len(extra)} record(s) that were in neither labelled "
                        "event; their incident is not labelled")
    return c


def _blocking_rulings(conn: sqlite3.Connection, target: str, source: str) -> List[Tuple[str, str, str, str]]:
    """Active 'different' rulings between a member of ``source`` and one of ``target``:
    ``(source record, target record, reason, created_by)``."""
    rows = conn.execute(
        """SELECT o.left_enriched_event_id, o.right_enriched_event_id, COALESCE(o.reason, ''),
                  COALESCE(o.created_by, '')
           FROM DedupOverrides o WHERE o.active = 1 AND o.verdict = 'different'""").fetchall()
    src = {r[0] for r in conn.execute(
        "SELECT enriched_event_id FROM EventDeduplicationMap WHERE deduplicated_event_id = ?", (source,))}
    tgt = {r[0] for r in conn.execute(
        "SELECT enriched_event_id FROM EventDeduplicationMap WHERE deduplicated_event_id = ?", (target,))}
    out = []
    for left, right, reason, by in rows:
        if left in src and right in tgt:
            out.append((left, right, reason, by))
        elif right in src and left in tgt:
            out.append((right, left, reason, by))
    return out


def _is_source_gate_ruling(reason: str, created_by: str) -> bool:
    from cyber_data_collector.dedup.source_gate import QUESTION_VERSION

    return created_by == "rule" and reason.startswith(f"split: {QUESTION_VERSION}:")


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
    """Split every labelled-different pair that is one event, merge every labelled-same pair that is two;
    ``apply`` False only plans.

    Which records each labelled event held is read from the members file, else from ``snapshot`` (the
    database the pairs were labelled on); with neither, the contradiction is listed as unresolved. Splits and
    merges go through the ledger (snapshotted, reversible); a split caches 'different' rulings, and a merge
    the ledger refuses (a 'different' ruling or label) is reported, never forced.
    """
    from cyber_data_collector.dedup.ledger import DedupLedger

    contradictions = find(conn, path)
    same = find_same(conn, path)
    if not contradictions and not same:
        return []
    ids = sorted({e for c in contradictions + same for e in (c.a, c.b)})
    recorded = labelled_members()
    members = _snapshot_members(snapshot, [e for e in ids if e not in recorded]) if snapshot else {}
    members.update({e: recorded[e] for e in ids if e in recorded})
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row      # the ledger reads columns by name
    try:
        ledger = ledger or DedupLedger(conn)
        for c in contradictions:
            # An earlier split in this run may have separated this pair already.
            if not find_still(conn, c):
                continue
            if not members.get(c.a) and not members.get(c.b):
                c.unresolved = "no labelling-time snapshot to say which records belong to which labelled event"
                continue
            plan_split(conn, c, members)
            if c.unresolved or not apply:
                continue
            reason = (f"[label-contradiction] labelled different incidents ({c.a[:8]} vs {c.b[:8]}) but merged into "
                      f"one event; the {len(c.leaving)} record(s) of the "
                      f"{'second' if c.leaving == c.side_b else 'first'} labelled event leave together")
            c.new_event = ledger.split_members(c.event, c.leaving, reason=reason, actor="rule")
        for c in same:
            # An earlier merge in this run may have joined this pair already.
            now = [x for x in find_same(conn, path) if x.pair_id == c.pair_id]
            if not now:
                continue
            c.event, c.source = now[0].event, now[0].source
            plan_merge(conn, c, members)
            if c.unresolved or not apply:
                continue
            reason = (f"[label-contradiction] labelled the same incident ({c.a[:8]} vs {c.b[:8]}) but stored as "
                      f"two events; the {len(c.side_b)} record(s) of {c.source[:8]} join {c.event[:8]}")
            rulings = _blocking_rulings(conn, c.event, c.source)
            other = [r for r in rulings if not _is_source_gate_ruling(r[2], r[3])]
            if other:
                left, right, why, _ = other[0]
                c.unresolved = (f"a 'different' ruling blocks the merge ({left[:8]} vs {right[:8]}: {why or 'no reason'})")
                continue
            labelled = conflict(conn, c.event, c.source)
            if labelled:
                c.unresolved = f"the merge would join a labelled-different pair ({labelled})"
                continue
            junk = sorted({r[0] for r in rulings})
            if junk:
                # The source gate ruled these pages no evidence of the incident; take them out as it would have.
                conn.executemany("UPDATE EnrichedEvents SET status = 'Inactive' WHERE enriched_event_id = ?",
                                 [(r,) for r in junk])
                c.leaving = junk
                reason += f"; {len(junk)} page(s) the source gate ruled no incident report taken out"
            ledger.merge_events(c.event, c.source, reason=reason, actor="rule", respect_overrides=not junk)
        if apply:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.row_factory = previous
    return contradictions + same


def find_still(conn: sqlite3.Connection, c: Contradiction) -> List[Contradiction]:
    """``[c]`` while its pair still resolves to one Active event, else ``[]``."""
    resolver = _resolver(conn)
    (a_now, a_status), (b_now, b_status) = resolver.resolve(c.a), resolver.resolve(c.b)
    if a_status == "Active" and b_status == "Active" and a_now == b_now:
        c.event = a_now
        return [c]
    return []
