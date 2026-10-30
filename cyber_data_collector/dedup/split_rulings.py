"""Split rulings between two records that both left the same event: void them.

A split records a ``different`` ruling between each departing record and every
record that stays (:meth:`DedupLedger.split_members`), so no later pass merges
the stray back. The ruling means "this record is not about that event's
incident" - not "these two records are about different incidents". When two
strays leave one event in separate runs, the first split rules the second
(which was still there) different from the first, and the second split cannot
undo it: the two strays are then barred from each other for good, whatever
they report.

That is how Optus's White Pages 2019 breach stayed two events on one date: two
White Pages records left the September 2022 event on 2026-10-27, minutes apart,
and the calibrated duplicate check's 0.98 "same incident" could not merge them
past the ruling the first split had cached between them.

:func:`void_orphaned` finds every active rule-made split ruling whose two
records were both in the event the split was made from (the latest ``revert-*``
snapshot of an event holding both, taken no later than the ruling) when that
event is still Active and holds neither of them any more: both were ruled not
about its incident, so nothing is known about the pair. The ruling is
deactivated (``active = 0``, the reason kept with a note saying why); a
person's ruling, a model-judged pair ruling, or a ruling whose source event is
gone or still holds one of the records is left alone. Voiding a ruling merges
nothing: the pair is then decided by the calibrated duplicate check like any
other. Idempotent; reversible by setting ``active = 1``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Set, Tuple

logger = logging.getLogger(__name__)

VOID_NOTE = "[void: both records left event {event} as strays; the ruling was only against what stayed]"


@dataclass(frozen=True)
class Orphan:
    override_id: str
    left: str
    right: str
    event: str          # the event the split was made from
    reason: str


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def orphaned(conn: sqlite3.Connection) -> List[Orphan]:
    """Active rule-made split rulings whose two records both left the still-Active event they were split from."""
    if not (_has_table(conn, "DedupOverrides") and _has_table(conn, "DedupSnapshots")):
        return []
    rulings = conn.execute(
        """SELECT override_id, left_enriched_event_id, right_enriched_event_id, COALESCE(reason, ''), created_at
           FROM DedupOverrides WHERE active = 1 AND verdict = 'different' AND created_by = 'rule'
             AND reason LIKE 'split:%'""").fetchall()
    if not rulings:
        return []
    wanted = {r for row in rulings for r in (row[1], row[2])}
    # Revert snapshots are taken just before a split: (event, time, members) per snapshot holding a wanted record.
    held: Dict[str, List[Tuple[str, str, Set[str]]]] = {}
    for event_id, created_at, payload in conn.execute(
            "SELECT deduplicated_event_id, created_at, payload_json FROM DedupSnapshots WHERE batch_id LIKE 'revert-%'"):
        try:
            members = {m["enriched_event_id"] for m in json.loads(payload).get("members", [])}
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        snap = (event_id, str(created_at), members)
        for record in members & wanted:
            held.setdefault(record, []).append(snap)
    status = dict(conn.execute("SELECT deduplicated_event_id, status FROM DeduplicatedEvents"))
    current: Dict[str, Set[str]] = {}
    for record, event_id in conn.execute("SELECT enriched_event_id, deduplicated_event_id FROM EventDeduplicationMap"):
        current.setdefault(event_id, set()).add(record)
    found = []
    for override_id, left, right, reason, created_at in rulings:
        both = [s for s in held.get(left, ()) if right in s[2] and s[1] <= str(created_at)]
        if not both:
            continue
        event_id = max(both, key=lambda s: s[1])[0]
        if status.get(event_id) != "Active":
            continue
        if {left, right} & current.get(event_id, set()):
            continue
        found.append(Orphan(override_id, left, right, event_id, reason))
    return found


def void_orphaned(conn: sqlite3.Connection, apply: bool = True) -> List[Orphan]:
    """Deactivate the rulings :func:`orphaned` finds; ``apply`` False only lists them."""
    found = orphaned(conn)
    if apply and found:
        conn.executemany(
            "UPDATE DedupOverrides SET active = 0, reason = reason || ' ' || ? WHERE override_id = ?",
            [(VOID_NOTE.format(event=o.event[:8]), o.override_id) for o in found])
        conn.commit()
        logger.info("Voided %d split ruling(s) between records that both left the event they were split from",
                    len(found))
    return found
