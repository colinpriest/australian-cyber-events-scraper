"""Measure the event-deduplication paths on the labelled and seeded cases, with no manual review.

Every case is two sets of records and what is known about them:

* ``labels``  - ``evaluation/duplicate_labels.csv`` (same / different), the two
  labelled events as they were when labelled (the labelling-time snapshot named
  in ``evaluation/calibration_snapshots.json``, else where their records are now);
* ``review``  - ``evaluation/review_acceptance_cases.csv`` ``same_incident_as``
  cases: events that must be one incident;
* ``seeded-split`` - an active event of two or more records about one
  organisation, its records divided in two by a seeded shuffle: the halves are
  one incident, so a path should join them;
* ``seeded-join``  - two active events of one organisation more than
  ``--join-gap-days`` apart: two incidents, so a path should not join them.

Each path answers every case and is scored against what is known:

=============  ===============================================================
``rule``       the deterministic merge (``DeduplicationEngine.merge_decision``)
``rule-before``  the same rule before incident-specific corroboration was
               required (a shared organisation and exact day alone), for the
               before/after comparison
``candidate``  whether candidate generation puts the pair in front of a judge
``judgement``  the calibrated Jev duplicate check, read from ``ModelJudgements``
               (cases with a stored judgement only)
``adjudicator``  the pair LLM adjudicator, read from ``AdjudicationCache``
=============  ===============================================================

Per path and source it reports the **missed** duplicates (known same, not
joined), the **incorrect merges** (known different, joined), the candidate
counts and the **paid calls**: how many answers came from storage (cached) and
how many would need a new request (new). Nothing here calls a paid API unless
``--paid-calls`` is given; without it a case a path cannot answer from storage
is counted as one new call and left undecided. Open the database read-only
(it always is, unless ``--paid-calls``) and run on a copy.

Two more sections:

* ``end-state`` - the labelled outcomes in the database as it stands: ``same``
  pairs now one event, ``different`` pairs wrongly one event;
* ``rule-impact`` - how many existing events were joined only by the
  same-day rule without corroboration (counts only; nothing is undone).

``--save`` writes the result as JSON; ``--compare`` prints the change from an
earlier saved run, which is how a change is judged before and after.

Usage (from the project root, on a database copy):
    python scripts/evaluate_dedup.py --db D:/tmp/copy.db
    python scripts/evaluate_dedup.py --db D:/tmp/copy.db --save before.json
    python scripts/evaluate_dedup.py --db D:/tmp/copy.db --compare before.json
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.dedup import excerpts, merge_corroboration  # noqa: E402
from cyber_data_collector.dedup.adjudication_cache import AdjudicationCache  # noqa: E402
from cyber_data_collector.dedup.adjudicator import Adjudicator, EventRecord  # noqa: E402
from cyber_data_collector.dedup.entity_resolution import EntityResolver  # noqa: E402
from cyber_data_collector.processing.deduplication_v2 import (  # noqa: E402
    CyberEvent,
    DeduplicationEngine,
)

ROOT = Path(__file__).resolve().parent.parent
LABELS_PATH = ROOT / "evaluation" / "duplicate_labels.csv"
REVIEW_PATH = ROOT / "evaluation" / "review_acceptance_cases.csv"
SNAPSHOTS_PATH = ROOT / "evaluation" / "calibration_snapshots.json"
CALIBRATION_PATH = ROOT / "evaluation" / "duplicate_calibration.json"

SOURCES = ("labels", "review", "seeded-split", "seeded-join")
PATHS = ("rule-before", "rule", "candidate", "judgement", "adjudicator")
#: Certainty at which a pair adjudication counts as a merge (``find-missed`` default).
ADJUDICATOR_MIN_CERTAINTY = 0.9
RESULT_VERSION = 1


class PaidCallNeeded(RuntimeError):
    """A path needs an answer that is not stored and paid calls were not allowed."""


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

@dataclass
class Side:
    """One side of a case: the active records it consists of."""

    key: str                       # a deduplicated event id, or a synthetic label
    members: List[str]             # enriched_event_ids
    event_date: Optional[date] = None
    victim: Optional[str] = None
    replaces: Optional[str] = None  # the current event this side stands for, if any


@dataclass
class Case:
    case_id: str
    source: str
    expect: str                    # 'same' | 'different'
    left: Side
    right: Side
    pair_id: Optional[str] = None  # the pair id a stored judgement is keyed on
    note: str = ""


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


class Data:
    """Everything the paths read, loaded once."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        conn.row_factory = sqlite3.Row
        self.records: Dict[str, Dict[str, Any]] = {}
        for row in conn.execute(
                """SELECT e.enriched_event_id AS id, e.title, e.summary, e.description, e.event_date,
                          e.event_type, e.records_affected, e.attack_method, e.attacking_entity_name AS actor,
                          e.perplexity_enrichment_data AS research, r.source_url AS url
                   FROM EnrichedEvents e LEFT JOIN RawEvents r ON r.raw_event_id = e.raw_event_id
                   WHERE COALESCE(e.status, 'Active') = 'Active'"""):
            record = dict(row)
            try:
                research = json.loads(record.pop("research") or "{}")
                research = research if isinstance(research, dict) else {}
            except (TypeError, ValueError):
                research = {}
            record["victim"] = research.get("formal_entity_name")
            record["method"] = research.get("attack_method") or record["attack_method"]
            record["actor"] = research.get("threat_actor") or record["actor"]
            record["date"] = _parse_date(record["event_date"])
            self.records[record["id"]] = record
        self.events: Dict[str, Dict[str, Any]] = {}
        for row in conn.execute(
                """SELECT deduplicated_event_id AS id, title, summary, description, event_date, records_affected,
                          victim_organization_name AS victim, vendor_organization_name AS vendor, status
                   FROM DeduplicatedEvents"""):
            self.events[row["id"]] = dict(row)
        self.members_of: Dict[str, List[str]] = defaultdict(list)
        for row in conn.execute("SELECT deduplicated_event_id AS d, enriched_event_id AS e "
                                "FROM EventDeduplicationMap ORDER BY enriched_event_id"):
            if row["e"] in self.records:
                self.members_of[row["d"]].append(row["e"])
        self.active_events = [d for d, e in self.events.items()
                             if (e["status"] or "Active") == "Active" and self.members_of.get(d)]

    # -- representations ------------------------------------------------

    def cyber_event(self, side: Side) -> CyberEvent:
        members = [self.records[m] for m in side.members if m in self.records]
        dates = [m["date"] for m in members if m["date"]]
        names = [m["victim"] for m in members if m["victim"]]
        victim = side.victim or (Counter(names).most_common(1)[0][0] if names else None)
        counts = sorted({int(m["records_affected"]) for m in members
                         if isinstance(m["records_affected"], (int, float)) and m["records_affected"] > 0})
        first = members[0] if members else {}
        return CyberEvent(
            event_id=side.key, title=first.get("title") or "", summary=first.get("summary"),
            event_date=side.event_date or (min(dates) if dates else None),
            event_type=first.get("event_type"), records_affected=max(counts) if counts else None,
            victim_organization_name=victim,
            urls=sorted({m["url"] for m in members if m["url"]}),
            attack_methods=sorted({m["method"] for m in members if m["method"]}),
            threat_actors=sorted({m["actor"] for m in members if m["actor"]}),
            record_counts=counts)

    def event_record(self, side: Side) -> EventRecord:
        """The candidate-generation / adjudication view of a side."""
        cyber = self.cyber_event(side)
        members = [self.records[m] for m in side.members if m in self.records]
        names = sorted({m["victim"] for m in members if m["victim"]} - {cyber.victim_organization_name})
        body = " ".join(x for m in members for x in (m["description"],) if x)[:1500]
        return EventRecord(
            enriched_event_id=side.key, title=cyber.title, summary=cyber.summary,
            description=body or None, entity_name=cyber.victim_organization_name, alt_entities=names,
            event_date=cyber.event_date.isoformat() if cyber.event_date else None,
            source_urls=cyber.urls, records_affected=cyber.records_affected)

    def corpus_events(self) -> List[CyberEvent]:
        return [self.cyber_event(Side(key=eid, members=[eid])) for eid in sorted(self.records)]

    def base_event_records(self) -> List[EventRecord]:
        return [self.event_record(Side(key=d, members=self.members_of[d],
                                       event_date=_parse_date(self.events[d]["event_date"]),
                                       victim=self.events[d]["victim"])) for d in sorted(self.active_events)]


def pair_id(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


def _snapshot_for(db_path: Optional[Path], override: Optional[Path]) -> Optional[Path]:
    if override:
        return Path(override)
    try:
        name = json.loads(SNAPSHOTS_PATH.read_text(encoding="utf-8"))["snapshots"]["duplicates"]
    except (OSError, ValueError, KeyError):
        return None
    for folder in (db_path.parent if db_path else None, ROOT / "instance"):
        if folder and (folder / name).exists():
            return folder / name
    return None


def _snapshot_side(snapshot: sqlite3.Connection, event_id: str, data: Data) -> Optional[Side]:
    row = snapshot.execute("SELECT event_date, victim_organization_name FROM DeduplicatedEvents "
                           "WHERE deduplicated_event_id = ?", (event_id,)).fetchone()
    members = [r[0] for r in snapshot.execute(
        "SELECT enriched_event_id FROM EventDeduplicationMap WHERE deduplicated_event_id = ? "
        "ORDER BY enriched_event_id", (event_id,)) if r[0] in data.records]
    if row is None or not members:
        return None
    return Side(key=event_id, members=members, event_date=_parse_date(row[0]), victim=row[1])


def _current_side(data: Data, resolver, event_id: str) -> Optional[Side]:
    now, status = resolver.resolve(event_id)
    if status != "Active" or not now or not data.members_of.get(now):
        return None
    event = data.events[now]
    return Side(key=now, members=list(data.members_of[now]), event_date=_parse_date(event["event_date"]),
                victim=event["victim"], replaces=now)


def labelled_cases(data: Data, snapshot_path: Optional[Path], labels_path: Path) -> Tuple[List[Case], Dict[str, int]]:
    from cyber_data_collector.quality.regression import EventResolver

    stats = Counter()
    resolver = EventResolver(data.conn)
    snapshot = None
    if snapshot_path and Path(snapshot_path).exists():
        snapshot = sqlite3.connect(f"file:{Path(snapshot_path).as_posix()}?mode=ro", uri=True)
    cases = []
    try:
        with Path(labels_path).open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        for row in rows:
            label = (row.get("label") or "").strip()
            if label not in ("same", "different"):
                stats[f"label {label or 'blank'}"] += 1
                continue
            a, b = (row["pair_id"].split("|") + [None, None])[:2]
            sides = []
            for event_id in (a, b):
                side = _snapshot_side(snapshot, event_id, data) if snapshot is not None else None
                if side is None:
                    side = _current_side(data, resolver, event_id)
                    stats["side from current state"] += 1
                else:
                    stats["side from labelling snapshot"] += 1
                sides.append(side)
            if None in sides:
                stats["skipped: a side has no active record"] += 1
                continue
            if set(sides[0].members) & set(sides[1].members):
                stats["skipped: sides share a record"] += 1
                continue
            cases.append(Case(f"labels:{row['pair_id']}", "labels", label, sides[0], sides[1], pair_id=row["pair_id"]))
    finally:
        if snapshot is not None:
            snapshot.close()
    return cases, dict(stats)


def review_cases(data: Data, review_path: Path) -> Tuple[List[Case], Dict[str, int]]:
    from cyber_data_collector.quality.regression import EventResolver

    stats = Counter()
    resolver = EventResolver(data.conn)
    cases = []
    with Path(review_path).open(encoding="utf-8", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if (r.get("expectation") or "").strip() == "same_incident_as"]
    for row in rows:
        a, b = row["deduplicated_event_id"].strip(), row["value"].strip()
        (na, sa), (nb, sb) = resolver.resolve(a), resolver.resolve(b)
        if sa != "Active" or sb != "Active":
            stats["skipped: an event is no longer active"] += 1
            continue
        if na == nb:
            stats["already one event"] += 1      # decided by the end-state section; there is nothing to separate
            continue
        left, right = _current_side(data, resolver, a), _current_side(data, resolver, b)
        if left is None or right is None:
            stats["skipped: a side has no active record"] += 1
            continue
        cases.append(Case(f"review:{na}|{nb}", "review", "same", left, right, pair_id=pair_id(na, nb),
                          note=row.get("note", "")))
    return cases, dict(stats)


def seeded_cases(data: Data, seed: int, n_split: int, n_join: int, join_gap_days: int) -> List[Case]:
    rng = random.Random(seed)
    cases: List[Case] = []
    engine = DeduplicationEngine()

    eligible = []
    for did in sorted(data.active_events):
        members = data.members_of[did]
        if len(members) < 2:
            continue
        named = [data.cyber_event(Side(m, [m])) for m in members]
        named = [e for e in named if e.victim_organization_name]
        # A known single incident: every record that names its victim names one organisation.
        if len(named) >= 2 and all(engine._same_entity(named[0], other) for other in named[1:]):
            eligible.append(did)
    rng.shuffle(eligible)
    for did in eligible[:n_split]:
        members = list(data.members_of[did])
        rng.shuffle(members)
        half = len(members) // 2
        event = data.events[did]
        left = Side(f"{did}#a", sorted(members[:half]), victim=event["victim"], replaces=did)
        right = Side(f"{did}#b", sorted(members[half:]), victim=event["victim"], replaces=did)
        cases.append(Case(f"split:{did}", "seeded-split", "same", left, right,
                          note=f"{len(members)} records divided {half}/{len(members) - half}"))

    from cyber_data_collector.dedup.recurrence_check import load_recurrence_events
    from cyber_data_collector.dedup.victim_counting import organisation_groups

    events = [e for e in load_recurrence_events(data.conn, include_undated=False)
              if e.dedup_id in data.active_events]
    groups = organisation_groups(data.conn, [e.entity_id for e in events])
    by_org: Dict[Any, list] = defaultdict(list)
    for e in events:
        by_org[groups.get(e.entity_id, e.entity_id)].append(e)
    pairs = []
    for org_events in by_org.values():
        for i, a in enumerate(org_events):
            for b in org_events[i + 1:]:
                if a.dedup_id != b.dedup_id and a.event_date and b.event_date \
                        and abs((a.event_date - b.event_date).days) >= join_gap_days:
                    pairs.append((a, b))
    pairs.sort(key=lambda p: pair_id(p[0].dedup_id, p[1].dedup_id))
    rng.shuffle(pairs)
    for a, b in pairs[:n_join]:
        sides = []
        for e in (a, b):
            event = data.events[e.dedup_id]
            sides.append(Side(e.dedup_id, list(data.members_of[e.dedup_id]),
                              event_date=_parse_date(event["event_date"]), victim=event["victim"],
                              replaces=e.dedup_id))
        cases.append(Case(f"join:{pair_id(a.dedup_id, b.dedup_id)}", "seeded-join", "different",
                          sides[0], sides[1], pair_id=pair_id(a.dedup_id, b.dedup_id),
                          note=f"{abs((a.event_date - b.event_date).days)} days apart"))
    return cases


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

@dataclass
class Outcome:
    decision: Optional[bool] = None     # True: joined / judged the same; False: kept apart; None: no answer
    candidate: Optional[bool] = None
    cached: int = 0
    new: int = 0
    detail: str = ""


class Context:
    def __init__(self, data: Data, allow_paid: bool, cache: AdjudicationCache,
                 snapshot_path: Optional[Path] = None):
        self.data = data
        self.snapshot_path = snapshot_path
        self.allow_paid = allow_paid
        self.cache = cache
        self._corpus: Optional[List[CyberEvent]] = None
        self._base_records: Optional[List[EventRecord]] = None
        self._base_pairs: Optional[Dict[frozenset, Set[str]]] = None
        self._judgements: Optional[Dict[str, float]] = None
        self._escalated: Optional[Set[str]] = None
        self.cutoff: Optional[float] = None
        self.resolver = EntityResolver(data.conn)
        self.engine = DeduplicationEngine()
        self.engine.learn_corpus(self.corpus)

    @property
    def corpus(self) -> List[CyberEvent]:
        if self._corpus is None:
            self._corpus = self.data.corpus_events()
        return self._corpus

    @property
    def base_records(self) -> List[EventRecord]:
        if self._base_records is None:
            self._base_records = self.data.base_event_records()
        return self._base_records

    def candidate_pairs(self, records: Sequence[EventRecord]) -> Dict[frozenset, Set[str]]:
        judge = Adjudicator(resolver=EntityResolver(self.data.conn))
        judge.candidate_pairs(records)
        return judge.pair_reasons

    @property
    def base_pairs(self) -> Dict[frozenset, Set[str]]:
        if self._base_pairs is None:
            self._base_pairs = self.candidate_pairs(self.base_records)
        return self._base_pairs

    def judgements(self) -> Dict[str, float]:
        if self._judgements is None:
            from cyber_data_collector.dedup import duplicate_judgement as dj

            self._judgements, self._escalated = {}, set()
            # The labelled pairs were judged on the labelling-time database, so its stored answers
            # are read too (the live ones win for a pair stored in both).
            sources = [self.data.conn]
            if self.snapshot_path and Path(self.snapshot_path).exists():
                sources.insert(0, sqlite3.connect(f"file:{Path(self.snapshot_path).as_posix()}?mode=ro", uri=True))
            for conn in sources:
                try:
                    # A labelling-time snapshot cannot be re-asked, so the stored history is read as it is (not evidence-checked).
                    self._judgements.update(dj.unvalidated_final_probabilities(conn))
                    lo, hi = dj.ESCALATE_BAND
                    escalated = {r[0] for r in conn.execute(
                        "SELECT DISTINCT subject_id FROM ModelJudgements WHERE question_id = ? AND question_version = ?",
                        (dj.ESCALATION_QUESTION_ID, dj.ESCALATION_VERSION))}
                    jev = {r[0]: float(r[1]) for r in conn.execute(
                        "SELECT subject_id, value FROM ModelJudgements WHERE question_id = ? AND question_version = ?",
                        (dj.QUESTION_ID, dj.QUESTION_VERSION))}
                    self._escalated |= {p for p, v in jev.items() if lo <= v < hi and p not in escalated}
                except sqlite3.Error:
                    pass
                finally:
                    if conn is not self.data.conn:
                        conn.close()
            try:
                self.cutoff = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8")).get("cutoff")
            except (OSError, ValueError):
                self.cutoff = None
        return self._judgements

    @property
    def unescalated(self) -> Set[str]:
        self.judgements()
        return self._escalated or set()


def legacy_same_day_rule(engine: DeduplicationEngine, a: CyberEvent, b: CyberEvent) -> bool:
    """The merge rule before corroboration was required: a shared exact day (or an identical specific title)."""
    if not a.event_date or a.event_date != b.event_date:
        return False
    victims = engine.victim_compatibility(a, b)
    if victims == "conflict":
        return False
    if a.title and b.title and a.title.lower().strip() == b.title.lower().strip():
        if victims == "same":
            return True
        if not a.victim_organization_name and not b.victim_organization_name \
                and engine._title_identifies_something(a.title):
            return True
    return a.event_date.day != 1 and victims == "same"


def path_rule_before(ctx: Context, case: Case) -> Outcome:
    return Outcome(decision=legacy_same_day_rule(ctx.engine, ctx.data.cyber_event(case.left),
                                                 ctx.data.cyber_event(case.right)))


def path_rule(ctx: Context, case: Case) -> Outcome:
    reason = ctx.engine.merge_decision(ctx.data.cyber_event(case.left), ctx.data.cyber_event(case.right))
    return Outcome(decision=bool(reason), detail=reason or "")


def path_candidate(ctx: Context, case: Case) -> Outcome:
    left, right = ctx.data.event_record(case.left), ctx.data.event_record(case.right)
    replaced = {s.replaces for s in (case.left, case.right) if s.replaces}
    if replaced and all(s.replaces for s in (case.left, case.right)) and \
            all(s.key == s.replaces for s in (case.left, case.right)):
        pairs = ctx.base_pairs                        # both sides are current events, unchanged
    else:
        stand_in = {s.key for s in (case.left, case.right)}
        records = [r for r in ctx.base_records if r.enriched_event_id not in replaced | stand_in]
        pairs = ctx.candidate_pairs(records + [left, right])
    key = frozenset((left.enriched_event_id, right.enriched_event_id))
    return Outcome(candidate=key in pairs, detail="; ".join(sorted(pairs.get(key, ()))[:3]))


def path_judgement(ctx: Context, case: Case) -> Outcome:
    if not case.pair_id:
        return Outcome(detail="no stored judgement for a synthetic pair")
    p = ctx.judgements().get(case.pair_id)
    if p is None:
        return Outcome(new=1, detail="no stored judgement")
    new = 1 if case.pair_id in ctx.unescalated else 0     # in the escalation band, escalation not stored
    cutoff = ctx.cutoff if ctx.cutoff is not None else 0.81
    return Outcome(decision=p >= cutoff, cached=1, new=new, detail=f"P(same)={p:.2f} cutoff {cutoff}")


class _OfflineAdjudicator(Adjudicator):
    """An adjudicator that answers from storage and reports what it could not."""

    def _get_client(self):
        raise PaidCallNeeded("not stored")


def path_adjudicator(ctx: Context, case: Case) -> Outcome:
    aj = (Adjudicator if ctx.allow_paid else _OfflineAdjudicator)(resolver=ctx.resolver, cache=ctx.cache)
    sides = (case.left, case.right)
    names = {s.key: [n for n in (ctx.data.cyber_event(s).victim_organization_name,) if n] for s in sides}
    found = excerpts.load_member_excerpts(ctx.data.conn, {s.key: s.members for s in sides}, names,
                                          budget=excerpts.PAIR_EVIDENCE_CHARS)
    left, right = (ctx.data.event_record(s) for s in sides)
    left.evidence, right.evidence = found.get(case.left.key), found.get(case.right.key)
    try:
        aj.embed_records([left, right])
    except PaidCallNeeded:
        pass
    try:
        verdict = aj.adjudicate(left, right)
    except PaidCallNeeded:
        return Outcome(new=1, detail="adjudication not stored")
    cached = aj.stats["llm_cached"]
    new = aj.stats["llm_calls"]
    decided = verdict.decided_by.value
    return Outcome(decision=bool(verdict.is_same_event and verdict.certainty >= ADJUDICATOR_MIN_CERTAINTY),
                   cached=cached, new=new, detail=f"{decided} {verdict.certainty:.2f}")


PATH_FUNCS: Dict[str, Callable[[Context, Case], Outcome]] = {
    "rule-before": path_rule_before, "rule": path_rule, "candidate": path_candidate,
    "judgement": path_judgement, "adjudicator": path_adjudicator,
}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score(cases: Sequence[Case], outcomes: Sequence[Outcome], path: str) -> Dict[str, Any]:
    """Counts for one path over some cases. 'Missed' = known same and answered 'not joined' / 'not surfaced';
    'incorrect' = known different and joined (for ``candidate``: surfaced, which only costs a judgement);
    'undecided' = the path had no stored answer, so it is neither found nor missed."""
    out: Dict[str, Any] = {"cases": len(cases), "same": 0, "different": 0, "found": 0, "missed": 0,
                           "undecided": 0, "incorrect_merges": 0, "correctly_apart": 0,
                           "cached_calls": 0, "new_calls": 0}
    for case, outcome in zip(cases, outcomes):
        out["cached_calls"] += outcome.cached
        out["new_calls"] += outcome.new
        out[case.expect] += 1
        verdict = outcome.candidate if path == "candidate" else outcome.decision
        if verdict is None:
            out["undecided"] += 1          # no stored answer: neither found nor missed, but it would cost a call
            continue
        if case.expect == "same":
            out["found" if verdict else "missed"] += 1
        else:
            out["incorrect_merges" if verdict else "correctly_apart"] += 1
    if path == "candidate":
        out["surfaced_different"] = out.pop("incorrect_merges")
        out["not_surfaced_different"] = out.pop("correctly_apart")
    return out


def end_state(data: Data, labels_path: Path, review_path: Path) -> Dict[str, Any]:
    from cyber_data_collector.quality.regression import EventResolver

    resolver = EventResolver(data.conn)
    out = {"labels_same_now_one_event": 0, "labels_same_still_separate": 0, "labels_different_wrongly_one_event": 0,
           "labels_different_apart": 0, "labels_unresolvable": 0, "review_now_one_event": 0,
           "review_still_separate": 0, "review_unresolvable": 0}
    with Path(labels_path).open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            label = (row.get("label") or "").strip()
            if label not in ("same", "different"):
                continue
            a, b = row["pair_id"].split("|")
            (na, sa), (nb, sb) = resolver.resolve(a), resolver.resolve(b)
            if sa != "Active" or sb != "Active":
                out["labels_unresolvable"] += 1
            elif label == "same":
                out["labels_same_now_one_event" if na == nb else "labels_same_still_separate"] += 1
            else:
                out["labels_different_wrongly_one_event" if na == nb else "labels_different_apart"] += 1
    with Path(review_path).open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if (row.get("expectation") or "").strip() != "same_incident_as":
                continue
            (na, sa), (nb, sb) = resolver.resolve(row["deduplicated_event_id"].strip()), \
                resolver.resolve(row["value"].strip())
            if sa != "Active" or sb != "Active":
                out["review_unresolvable"] += 1
            else:
                out["review_now_one_event" if na == nb else "review_still_separate"] += 1
    return out


def rule_impact(ctx: Context) -> Dict[str, Any]:
    """Which existing events were joined only by the uncorroborated same-day rule. Counts only."""
    data, engine = ctx.data, ctx.engine
    events = {eid: data.cyber_event(Side(eid, [eid])) for eid in data.records}

    def components(ids: Sequence[str], joined: Callable[[CyberEvent, CyberEvent], bool]) -> int:
        parent = {i: i for i in ids}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if joined(events[a], events[b]):
                    parent[find(a)] = find(b)
        return len({find(i) for i in ids})

    def before(a: CyberEvent, b: CyberEvent) -> bool:
        return legacy_same_day_rule(engine, a, b)

    def after(a: CyberEvent, b: CyberEvent) -> bool:
        return bool(engine.merge_decision(a, b))

    multi = {d: [m for m in data.members_of[d] if m in events] for d in data.active_events}
    multi = {d: ids for d, ids in multi.items() if len(ids) >= 2}
    joined_before = joined_after = lose = 0
    for ids in multi.values():
        c_before, c_after = components(ids, before), components(ids, after)
        if c_before == 1:
            joined_before += 1
            if c_after == 1:
                joined_after += 1
            else:
                lose += 1
    by_day: Dict[date, List[str]] = defaultdict(list)
    for eid, e in events.items():
        if e.event_date and e.event_date.day != 1 and e.victim_organization_name:
            by_day[e.event_date].append(eid)
    pair_counts = Counter()
    for ids in by_day.values():
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if not before(events[a], events[b]):
                    continue
                pair_counts["pairs merged before"] += 1
                if after(events[a], events[b]):
                    pair_counts["pairs still merged"] += 1
                else:
                    assessment = merge_corroboration.assess(events[a], events[b], ctx.engine.corpus)
                    pair_counts[f"pairs now left to the judgement ({assessment.verdict})"] += 1
    return {"multi_record_events": len(multi), "events_joined_by_the_old_rules_alone": joined_before,
            "of_which_still_joined": joined_after, "of_which_would_now_need_judgement": lose,
            **{k: v for k, v in sorted(pair_counts.items())}}


# ---------------------------------------------------------------------------
# Run, report, compare
# ---------------------------------------------------------------------------

def run(db_path: Path, args: argparse.Namespace) -> Dict[str, Any]:
    uri = f"file:{Path(db_path).as_posix()}?mode={'rw' if args.paid_calls else 'ro'}"
    conn = sqlite3.connect(uri, uri=True)
    try:
        data = Data(conn)
        snapshot = _snapshot_for(Path(db_path), args.snapshot)
        cases: List[Case] = []
        notes: Dict[str, Any] = {}
        labelled, notes["labels"] = labelled_cases(data, snapshot, Path(args.labels))
        reviewed, notes["review"] = review_cases(data, Path(args.review))
        cases += labelled + reviewed
        if args.seeded_splits or args.seeded_joins:
            cases += seeded_cases(data, args.seed, args.seeded_splits, args.seeded_joins, args.join_gap_days)
        notes["snapshot"] = str(snapshot) if snapshot else None
        cache = AdjudicationCache(conn, create=bool(args.paid_calls))
        ctx = Context(data, bool(args.paid_calls), cache, snapshot)

        result: Dict[str, Any] = {
            "version": RESULT_VERSION, "created": datetime.now().isoformat(timespec="seconds"),
            "db": str(db_path), "seed": args.seed, "paid_calls_allowed": bool(args.paid_calls),
            "cases": dict(Counter(f"{c.source}/{c.expect}" for c in cases)), "case_notes": notes,
            "end_state": end_state(data, Path(args.labels), Path(args.review)), "paths": {}}
        chosen = [p for p in PATHS if not args.paths or p in args.paths]
        for path in chosen:
            func = PATH_FUNCS[path]
            outcomes = [func(ctx, case) for case in cases]
            entry = {"all": score(cases, outcomes, path)}
            for source in SOURCES:
                subset = [(c, o) for c, o in zip(cases, outcomes) if c.source == source]
                if subset:
                    entry[source] = score([c for c, _ in subset], [o for _, o in subset], path)
            entry["examples"] = [
                {"case": c.case_id, "expect": c.expect, "decision": o.decision, "candidate": o.candidate,
                 "detail": o.detail} for c, o in zip(cases, outcomes)
                if (c.expect == "same" and not (o.candidate if path == "candidate" else o.decision))
                or (c.expect == "different" and (o.candidate if path == "candidate" else o.decision))][:10]
            result["paths"][path] = entry
        if path_enabled(args, "candidate"):
            result["candidate_counts"] = {"events": len(ctx.base_records), "pairs": len(ctx.base_pairs),
                                          "pairs_per_event": round(len(ctx.base_pairs) / max(1, len(ctx.base_records)), 2)}
        result["rule_impact"] = rule_impact(ctx) if not args.skip_rule_impact else None
        result["adjudication_cache"] = {"available": cache.available, **cache.stats}
        return result
    finally:
        conn.close()


def path_enabled(args: argparse.Namespace, path: str) -> bool:
    return not args.paths or path in args.paths


def render(result: Dict[str, Any]) -> str:
    lines = [f"database {result['db']}   seed {result['seed']}   paid calls "
             f"{'ALLOWED' if result['paid_calls_allowed'] else 'not allowed (stored answers only)'}",
             "cases: " + ", ".join(f"{k} {v}" for k, v in sorted(result["cases"].items())),
             f"case notes: {json.dumps(result['case_notes'])}", ""]
    es = result["end_state"]
    lines.append("end state (labelled outcomes as the database stands)")
    lines.append(f"  duplicate labels  same: {es['labels_same_now_one_event']} one event, "
                 f"{es['labels_same_still_separate']} still separate   different: "
                 f"{es['labels_different_wrongly_one_event']} wrongly one event, "
                 f"{es['labels_different_apart']} apart   unresolvable {es['labels_unresolvable']}")
    lines.append(f"  review cases      same_incident_as: {es['review_now_one_event']} one event, "
                 f"{es['review_still_separate']} still separate   unresolvable {es['review_unresolvable']}")
    for path, entry in result["paths"].items():
        lines += ["", f"path: {path}"]
        header = f"  {'source':<14}{'same':>5}{'diff':>6}{'found':>7}{'missed':>8}{'undec':>7}" \
                 f"{'wrong-merge':>13}{'apart':>7}{'cached':>8}{'new':>6}"
        lines.append(header)
        for source in ("all",) + SOURCES:
            s = entry.get(source)
            if not s:
                continue
            wrong = s.get("incorrect_merges", s.get("surfaced_different", 0))
            apart = s.get("correctly_apart", s.get("not_surfaced_different", 0))
            lines.append(f"  {source:<14}{s['same']:>5}{s['different']:>6}{s['found']:>7}{s['missed']:>8}"
                         f"{s['undecided']:>7}{wrong:>13}{apart:>7}{s['cached_calls']:>8}{s['new_calls']:>6}")
        if path == "candidate":
            lines.append("  (candidate: 'found' = surfaced; the 'wrong-merge' column counts known-different pairs "
                         "surfaced, which cost a judgement but merge nothing)")
        for ex in entry["examples"][:3]:
            lines.append(f"    e.g. {ex['case'][:70]} expect {ex['expect']}: {ex['detail'][:60]}")
    if "candidate_counts" in result:
        c = result["candidate_counts"]
        lines += ["", f"candidate generation: {c['pairs']} pairs over {c['events']} active events "
                      f"({c['pairs_per_event']} per event)"]
    if result.get("rule_impact"):
        lines += ["", "rule impact on existing events (counts only; nothing is undone)"]
        lines += [f"  {k}: {v}" for k, v in result["rule_impact"].items()]
    lines += ["", f"adjudication cache: {result['adjudication_cache']}"]
    return "\n".join(lines)


COMPARED = ("found", "missed", "undecided", "incorrect_merges", "surfaced_different", "cached_calls", "new_calls")


def compare(now: Dict[str, Any], before: Dict[str, Any]) -> str:
    lines = [f"change from {before.get('created')} to {now.get('created')}"]
    if before.get("seed") != now.get("seed") or before.get("cases") != now.get("cases"):
        lines.append("  WARNING: the case sets differ (seed or labels changed); deltas mix cases")
    for path, entry in now["paths"].items():
        old = before.get("paths", {}).get(path)
        if not old:
            lines.append(f"{path}: not in the earlier run")
            continue
        lines.append(f"{path}")
        for source in ("all",) + SOURCES:
            a, b = entry.get(source), old.get(source)
            if not a or not b:
                continue
            deltas = [f"{k} {b.get(k, 0)}->{a.get(k, 0)}" for k in COMPARED
                      if k in a and b.get(k, 0) != a.get(k, 0)]
            lines.append(f"  {source:<14}" + (", ".join(deltas) if deltas else "no change"))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db", required=True, help="database to evaluate (opened read-only; use a copy)")
    p.add_argument("--labels", default=str(LABELS_PATH))
    p.add_argument("--review", default=str(REVIEW_PATH))
    p.add_argument("--snapshot", default=None,
                   help="labelling-time database for the duplicate labels (default: evaluation/calibration_snapshots.json)")
    p.add_argument("--seed", type=int, default=20261029)
    p.add_argument("--seeded-splits", type=int, default=100)
    p.add_argument("--seeded-joins", type=int, default=100)
    p.add_argument("--join-gap-days", type=int, default=730)
    p.add_argument("--paths", nargs="*", choices=PATHS, help="only these paths")
    p.add_argument("--skip-rule-impact", action="store_true")
    p.add_argument("--paid-calls", action="store_true",
                   help="allow the adjudicator path to make paid requests for answers that are not stored "
                        "(opens the database read-write, stores the answers); off by default")
    p.add_argument("--save", help="write the result as JSON")
    p.add_argument("--compare", help="a result saved earlier: print the change from it")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    result = run(Path(args.db), args)
    print(render(result))
    if args.save:
        Path(args.save).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        print(f"\nsaved {args.save}")
    if args.compare:
        print()
        print(compare(result, json.loads(Path(args.compare).read_text(encoding="utf-8"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
