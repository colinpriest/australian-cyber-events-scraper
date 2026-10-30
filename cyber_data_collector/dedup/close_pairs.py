"""Pairs of incidents of one organisation that lie close together in time, and why each is still two events.

Two distinct incidents at one organisation on the same day, or within the same
possible period, are rare; two reports of one incident that deduplication did
not join are common. The recurrence analysis met such pairs as "ties" and had
to assume something about them. This module lists them instead, so each is an
exception the pipeline reports, with the reason the merging steps left it apart.

A pair is *close* when both are Active events of one organisation (the identity
the recurrence analysis and the duplicate check use,
:class:`duplicate_judgement.PairUniverse`) and either

* they carry the same stored date, or
* their possible periods overlap: a verified interval (``event_date_earliest``
  .. ``event_date_latest``); an unverified date stored as the 1st of a month,
  which may mean only that month, the whole month; any other unverified date,
  that day.

For each pair it reports the duplicate check's decision on the pair's *current*
evidence (a stored decision whose evidence has changed is not used), the
calibrated cut-off, and what stops a merge: a duplicate label, a 'different'
ruling between their records (with who made it), a label on another pair that
the merge would contradict, a score under the cut-off, or no decision yet.
Nothing is changed.
"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class Side:
    event_id: str
    title: str
    event_date: Optional[date]
    period: Optional[Tuple[date, date]]
    basis: str
    records: int


@dataclass
class ClosePair:
    pair_id: str
    organisation: str
    relation: str                       # 'same stored date' / 'overlapping periods'
    a: Side
    b: Side
    score: Optional[float] = None       # duplicate check, current evidence
    cutoff: Optional[float] = None
    label: Optional[str] = None
    blockers: List[str] = field(default_factory=list)


def possible_period(event) -> Tuple[Optional[Tuple[date, date]], str]:
    """The days an event's incident may have happened on, and how that was decided."""
    if event.has_verified_interval:
        return (event.date_earliest, event.date_latest), f"verified {event.date_precision or 'interval'}"
    d = event.event_date
    if d is None:
        return None, "undated"
    if d.day == 1:
        return (d, date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])), \
            "stored as the 1st, precision unverified (may be the month)"
    return (d, d), "stored day, precision unverified"


def _overlap(a: Optional[Tuple[date, date]], b: Optional[Tuple[date, date]]) -> bool:
    return a is not None and b is not None and a[0] <= b[1] and b[0] <= a[1]


def find(conn: sqlite3.Connection, universe=None) -> List[ClosePair]:
    """Every close pair of Active events, ordered by organisation then date."""
    from cyber_data_collector.dedup import duplicate_judgement as dj
    from cyber_data_collector.dedup import label_contradictions as lc
    from cyber_data_collector.dedup.ledger import DedupLedger

    universe = universe or dj.PairUniverse(conn)
    records = dict(conn.execute(
        "SELECT deduplicated_event_id, COUNT(*) FROM EventDeduplicationMap GROUP BY deduplicated_event_id"))
    found: List[Tuple[Dict[str, Any], ClosePair]] = []
    for pair in universe.all_pairs():
        spans = universe.groups[pair["group"]]
        ea, eb = spans[pair["a"]].payload, spans[pair["b"]].payload
        (pa, ba), (pb, bb) = possible_period(ea), possible_period(eb)
        same_day = ea.event_date is not None and ea.event_date == eb.event_date
        if not (same_day or _overlap(pa, pb)):
            continue
        found.append((pair, ClosePair(
            pair["pair_id"], ea.entity_name, "same stored date" if same_day else "overlapping periods",
            Side(ea.dedup_id, ea.title, ea.event_date, pa, ba, records.get(ea.dedup_id, 0)),
            Side(eb.dedup_id, eb.title, eb.event_date, pb, bb, records.get(eb.dedup_id, 0)))))
    if not found:
        return []
    scores = dj.stored_final_probabilities(conn, [p for p, _ in found])
    cal = dj.active_calibration(dj.CALIBRATION_PATH)
    cutoff = cal["cutoff"] if cal else None
    labels = dj.labelled_pairs(dj.LABELS_PATH)
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        ledger = DedupLedger(conn)
        for pair, close in found:
            close.score, close.cutoff, close.label = scores.get(close.pair_id), cutoff, labels.get(close.pair_id)
            if close.label == "different":
                close.blockers.append("labelled different incidents (evaluation/duplicate_labels.csv)")
            ruling = ledger.override_conflict(close.a.event_id, close.b.event_id)
            if ruling:
                by = conn.execute(
                    """SELECT created_by FROM DedupOverrides WHERE active = 1 AND verdict = 'different'
                       AND reason = ? LIMIT 1""", (ruling.split(": ", 1)[-1],)).fetchone()
                close.blockers.append(f"'different' ruling between their records ({ruling})"
                                      + (f", made by {by[0]}" if by else ""))
            labelled = lc.conflict(conn, close.a.event_id, close.b.event_id)
            if labelled:
                close.blockers.append(f"merging would contradict a label on another pair ({labelled})")
            if close.label != "same" and not close.blockers:
                if close.score is None:
                    close.blockers.append("no duplicate decision on the pair's current evidence")
                elif cutoff is None:
                    close.blockers.append("no duplicate calibration for the current models and evidence")
                elif close.score < cutoff:
                    close.blockers.append(f"duplicate check scored {close.score:.2f}, under the cut-off {cutoff:.2f}")
                else:
                    close.blockers.append(f"duplicate check scored {close.score:.2f} at or above the cut-off "
                                          "but the pair was not merged (held as a conflicting link, or not yet "
                                          "applied)")
    finally:
        conn.row_factory = previous
    return [c for _, c in sorted(found, key=lambda x: (x[1].organisation.lower(), x[1].a.event_date or date.max))]
