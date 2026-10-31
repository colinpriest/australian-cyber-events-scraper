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
import re
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


RULING_COLUMNS = {"override_id", "left_enriched_event_id", "right_enriched_event_id", "verdict", "reason",
                  "created_by", "created_at", "active"}


def _has_rulings(conn: sqlite3.Connection) -> bool:
    """A rulings table that records who made each ruling and when (one that does not holds no split rulings)."""
    if not _has_table(conn, "DedupOverrides"):
        return False
    return RULING_COLUMNS <= {r[1] for r in conn.execute("PRAGMA table_info(DedupOverrides)")}


def orphaned(conn: sqlite3.Connection) -> List[Orphan]:
    """Active rule-made split rulings whose two records both left the still-Active event they were split from."""
    if not (_has_rulings(conn) and _has_table(conn, "DedupSnapshots")):
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


LABEL_SPLIT = re.compile(r"^split: \[label-contradiction\] labelled different incidents \(([^\s)]+) vs ([^\s)]+)\)")


def withdrawn_label_rulings(conn: sqlite3.Connection, labels_path=None) -> List[Orphan]:
    """Rulings cached by ``resolve-label-contradictions`` for a pair that is no longer labelled different.

    Such a split rests only on the label (its reason names the pair by the first 8 characters of each id);
    when the label is changed (an owner's decision), the ruling has nothing left behind it.
    """
    from cyber_data_collector.dedup import label_contradictions as lc

    if not _has_rulings(conn):
        return []
    different = lc.labelled_different_pairs(labels_path)
    found = []
    for override_id, left, right, reason in conn.execute(
            """SELECT override_id, left_enriched_event_id, right_enriched_event_id, COALESCE(reason, '')
               FROM DedupOverrides WHERE active = 1 AND verdict = 'different' AND created_by = 'rule'
                 AND reason LIKE 'split: [label-contradiction]%'"""):
        m = LABEL_SPLIT.match(reason)
        if m and not any({a[:len(m.group(1))], b[:len(m.group(2))]} == {m.group(1), m.group(2)}
                         or {a[:len(m.group(2))], b[:len(m.group(1))]} == {m.group(1), m.group(2)}
                         for a, b in different):
            found.append(Orphan(override_id, left, right, "", reason))
    return found


COHERENCE_SPLIT = re.compile(r"^split: \[coherence\] stray record\(s\) about (.+?): P\(same\) ")


def outgrown_coherence_rulings(conn: sqlite3.Connection) -> List[Orphan]:
    """Model-scored coherence rulings whose basis is gone.

    A coherence split moves a record out because the organisation it is about is not the event's (and the model
    scored it below the cut-off). When that organisation is now a victim of the Active events holding *both*
    records, the record is about the event's own organisation after all (Agrimac's Storm-leak listing was split
    out of an event that later recorded Agrimac as its victim), and the ruling must not veto a merge the
    calibrated duplicate check makes. Rulings from a label ("labelled a different incident") are kept.
    """
    from cyber_data_collector.dedup.victim_fill import names_match

    if not _has_rulings(conn):
        return []
    rows = conn.execute(
        """SELECT override_id, left_enriched_event_id, right_enriched_event_id, COALESCE(reason, '')
           FROM DedupOverrides WHERE active = 1 AND verdict = 'different' AND created_by = 'rule'
             AND reason LIKE 'split: [coherence]%P(same)%'""").fetchall()
    if not rows:
        return []
    event_of: Dict[str, str] = {}
    for record, event_id in conn.execute(
            """SELECT m.enriched_event_id, m.deduplicated_event_id FROM EventDeduplicationMap m
               JOIN DeduplicatedEvents d ON d.deduplicated_event_id = m.deduplicated_event_id
               WHERE d.status = 'Active'"""):
        event_of[record] = event_id
    victims: Dict[str, List[str]] = {}
    for event_id, name in conn.execute(
            """SELECT dee.deduplicated_event_id, v.entity_name FROM DeduplicatedEventEntities dee
               JOIN EntitiesV2 v ON v.entity_id = dee.entity_id WHERE dee.relationship_type = 'victim'"""):
        victims.setdefault(event_id, []).append(name)
    found = []
    for override_id, left, right, reason in rows:
        m = COHERENCE_SPLIT.match(reason)
        el, er = event_of.get(left), event_of.get(right)
        if not (m and el and er):
            continue
        orgs = [o.strip() for o in m.group(1).split(",") if o.strip()]
        if any(all(any(names_match(conn, org, v) for v in victims.get(e, ())) for e in (el, er)) for org in orgs):
            found.append(Orphan(override_id, left, right, "", reason))
    return found


def same_victim_supplier_rulings(conn: sqlite3.Connection) -> List[Orphan]:
    """Supplier-split rulings between two records whose Active events now record the same victim.

    The per-customer split (``supplier_links.convert``) rules the records it moves different from every record
    that stays: "these are another customer's records". When the two events end up recording one victim, the
    split separated nothing - Australian Clinical Labs and its subsidiary Medlab, Ingram Micro and "Ingram Micro
    Holding Corporation", Nissan Oceania and Nissan, one of SA Government's Frontier records out of SA's own
    event - and the ruling must not veto the merge the calibrated duplicate check makes (0.93-0.98 on those).
    Events of two different customers (each university's Canvas event) keep their rulings.
    """
    from cyber_data_collector.dedup.victim_fill import names_match

    if not _has_rulings(conn):
        return []
    rows = conn.execute(
        """SELECT override_id, left_enriched_event_id, right_enriched_event_id, COALESCE(reason, '')
           FROM DedupOverrides WHERE active = 1 AND verdict = 'different' AND created_by = 'rule'
             AND reason LIKE 'split: [supplier-split]%'""").fetchall()
    if not rows:
        return []
    event_of: Dict[str, str] = {}
    victim: Dict[str, str] = {}
    for record, event_id, name in conn.execute(
            """SELECT m.enriched_event_id, m.deduplicated_event_id, d.victim_organization_name
               FROM EventDeduplicationMap m JOIN DeduplicatedEvents d ON d.deduplicated_event_id = m.deduplicated_event_id
               WHERE d.status = 'Active'"""):
        event_of[record] = event_id
        if name:
            victim[event_id] = name
    found = []
    for override_id, left, right, reason in rows:
        el, er = event_of.get(left), event_of.get(right)
        if el and er and el != er and el in victim and er in victim and names_match(conn, victim[el], victim[er]):
            found.append(Orphan(override_id, left, right, "", reason))
    return found


def void_orphaned(conn: sqlite3.Connection, apply: bool = True) -> List[Orphan]:
    """Deactivate the rulings :func:`orphaned`, :func:`withdrawn_label_rulings`,
    :func:`outgrown_coherence_rulings` and :func:`same_victim_supplier_rulings` find; ``apply`` False only lists
    them."""
    withdrawn = withdrawn_label_rulings(conn)
    outgrown = outgrown_coherence_rulings(conn)
    same_victim = same_victim_supplier_rulings(conn)
    if apply and same_victim:
        conn.executemany(
            "UPDATE DedupOverrides SET active = 0, reason = reason || ' [void: both records'' events now record the "
            "same victim; the split separated no customers]' WHERE override_id = ?",
            [(o.override_id,) for o in same_victim])
        conn.commit()
    if apply and outgrown:
        conn.executemany(
            "UPDATE DedupOverrides SET active = 0, reason = reason || ' [void: the organisation it was about is now "
            "a victim of both records'' events]' WHERE override_id = ?", [(o.override_id,) for o in outgrown])
        conn.commit()
    if apply and withdrawn:
        conn.executemany(
            "UPDATE DedupOverrides SET active = 0, reason = reason || ' [void: the pair is no longer labelled "
            "different]' WHERE override_id = ?", [(o.override_id,) for o in withdrawn])
        conn.commit()
    found = orphaned(conn)
    if apply and found:
        conn.executemany(
            "UPDATE DedupOverrides SET active = 0, reason = reason || ' ' || ? WHERE override_id = ?",
            [(VOID_NOTE.format(event=o.event[:8]), o.override_id) for o in found])
        conn.commit()
        logger.info("Voided %d split ruling(s) between records that both left the event they were split from",
                    len(found))
    return withdrawn + outgrown + same_victim + found
