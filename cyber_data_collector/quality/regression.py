"""Regression checks: labelled outcomes the database must not contradict, and
fixed synthetic cases the deterministic functions must keep getting right.

Labels (``evaluation/*_labels.csv``) were made against the database as it was
on the labelling day; since then events have been merged, split and rejected.
Each label is therefore checked against where its item is *now*:

* a merged event is followed to the event that holds its records today
  (``EventDeduplicationMap``), so "real, merged into a surviving event" is
  fine and "real, merged into an event that was later rejected" is not;
* an item that no longer exists, or whose merge cannot be followed to exactly
  one event, cannot be checked and is counted as such - never as agreement.

What contradicts a label:

=====================  ==============================================================
event_reality real     the event (or the event it was merged into) is Rejected
event_reality          the event is still Active (merged into another event is not
not_incident           counted: the incident it joined may be real)
event_reality          the event is still Active (a real incident outside the dataset's
out_of_scope           scope, e.g. not Australian)
event_scope in_scope   the event (or the event it was merged into) is Rejected
event_scope            the event is still Active
out_of_scope
duplicates same       the two events are still two distinct Active events
duplicates different   both now resolve to one Active event
source_gate            the record is evidence of an Active event, or is unmapped
not_incident           but still Active (it would be deduplicated next run)
source_gate            the record has been deactivated (EnrichedEvents.status is not
reports_incident       Active)
=====================  ==============================================================

``unsure`` labels are not checked.
"""

from __future__ import annotations

import csv
import sqlite3
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from cyber_data_collector.quality.model import Measurement

ROOT = Path(__file__).resolve().parents[2]
EVALUATION_DIR = ROOT / "evaluation"
LABEL_FILES = {
    "event_reality": "event_reality_labels.csv",
    "event_scope": "event_scope_labels.csv",
    "duplicates": "duplicate_labels.csv",
    "source_gate": "source_gate_labels.csv",
}
EXAMPLES = 10


class EventResolver:
    """Where a deduplicated event's records live now."""

    def __init__(self, conn: sqlite3.Connection):
        self.status: Dict[str, str] = {}
        self.master: Dict[str, str] = {}
        for event_id, status, master in conn.execute(
                "SELECT deduplicated_event_id, status, master_enriched_event_id FROM DeduplicatedEvents"):
            self.status[event_id] = status
            self.master[event_id] = master
        self.events_of: Dict[str, Set[str]] = defaultdict(set)
        self.members_of: Dict[str, Set[str]] = defaultdict(set)
        for enriched_id, event_id in conn.execute(
                "SELECT enriched_event_id, deduplicated_event_id FROM EventDeduplicationMap"):
            self.events_of[enriched_id].add(event_id)
            self.members_of[event_id].add(enriched_id)

    def resolve(self, event_id: str) -> Tuple[Optional[str], str]:
        """``(event_id_now, status_now)``; status ``missing`` / ``untraceable`` / ``ambiguous`` when unknown."""
        if event_id not in self.status:
            return None, "missing"
        frontier, seen = {event_id}, set()
        terminal: Dict[str, str] = {}
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            status = self.status.get(current)
            if status != "Merged":
                terminal[current] = status or "Active"
                continue
            records = set(self.members_of.get(current, ())) | {self.master.get(current)}
            holders = {e for r in records if r for e in self.events_of.get(r, ())} - seen
            frontier |= holders
        if not terminal:
            return None, "untraceable"
        active = [e for e, s in terminal.items() if s == "Active"]
        if len(active) == 1:
            return active[0], "Active"
        if len(active) > 1:
            return None, "ambiguous"
        if len(terminal) == 1:
            return next(iter(terminal.items()))
        statuses = set(terminal.values())
        return (sorted(terminal)[0], statuses.pop()) if len(statuses) == 1 else (None, "ambiguous")


def read_labels(path: Path, key: str) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if rows and (key not in rows[0] or "label" not in rows[0]):
        raise ValueError(f"{path} lacks the {key!r} / 'label' columns")
    return rows


def _result(prefix: str, checked: int, contradictions: List[str], unchecked: Dict[str, int],
            total: int, note: str) -> Dict[str, Measurement]:
    n_unchecked = sum(unchecked.values())
    breakdown = ", ".join(f"{k} {v}" for k, v in sorted(unchecked.items()) if v) or "none"
    return {
        f"labels.{prefix}.contradictions": Measurement(
            value=len(contradictions), examples=contradictions[:EXAMPLES],
            detail=f"{len(contradictions)} of {checked} checkable labels contradicted ({note})"),
        f"labels.{prefix}.checked": Measurement(value=checked, detail=f"{checked} of {total} labels checked"),
        f"labels.{prefix}.unchecked": Measurement(
            value=n_unchecked, detail=f"{n_unchecked} of {total} labels could not be checked: {breakdown}"),
    }


def _check_event_labels(prefix: str, resolver: EventResolver, path: Path, kept: str,
                        rejected: Tuple[str, ...], note: str,
                        excused: Optional[Set[str]] = None) -> Dict[str, Measurement]:
    """Labels that an event must survive (``kept``, followed through merges) or must not (``rejected``, not Active).

    ``excused``: events whose rejection does not contradict ``kept`` (counted, not reported).
    """
    rows = read_labels(path, "deduplicated_event_id")
    contradictions, unchecked, checked = [], defaultdict(int), 0
    for row in rows:
        label, event_id = row["label"].strip(), row["deduplicated_event_id"].strip()
        if label != kept and label not in rejected:
            unchecked[f"label {label!r}"] += 1
            continue
        raw = resolver.status.get(event_id)
        if raw is None:
            unchecked["event no longer exists"] += 1
            continue
        if label in rejected:
            checked += 1
            if raw == "Active":
                contradictions.append(f"{event_id}: labelled {label}, still Active - {row.get('rationale', '')[:90]}")
            continue
        now, status = resolver.resolve(event_id)
        if status in ("missing", "untraceable", "ambiguous"):
            unchecked[f"merge {status}"] += 1
            continue
        checked += 1
        if status == "Rejected" and now in (excused or ()):
            unchecked["rejected for another reason"] += 1
            continue
        if status == "Rejected":
            via = "" if now == event_id else f" (merged into {now})"
            contradictions.append(f"{event_id}: labelled {kept}, Rejected{via} - {row.get('rationale', '')[:90]}")
    return _result(prefix, checked, contradictions, unchecked, len(rows), note)


def _rejected_out_of_scope(conn) -> Set[str]:
    """Events rejected by the calibrated scope check (its ledger batches): real incidents, but not Australian."""
    from cyber_data_collector.dedup.event_scope import BATCH_PREFIX

    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'DedupSnapshots'").fetchone():
        return set()
    return {r[0] for r in conn.execute(
        """SELECT DISTINCT s.deduplicated_event_id FROM DedupSnapshots s
           JOIN DeduplicatedEvents d ON d.deduplicated_event_id = s.deduplicated_event_id
           WHERE d.status = 'Rejected' AND s.batch_id LIKE ?""", (f"{BATCH_PREFIX}-%",))}


def check_event_reality(conn, resolver: EventResolver, path: Path) -> Dict[str, Measurement]:
    # ``real`` says the incident happened, not that it is Australian: a scope rejection does not contradict it.
    return _check_event_labels("event_reality", resolver, path, "real", ("not_incident", "out_of_scope"),
                               "real must be Active or merged into an Active event (unless rejected as out of "
                               "scope); not_incident must not be Active", excused=_rejected_out_of_scope(conn))


def check_event_scope(conn, resolver: EventResolver, path: Path) -> Dict[str, Measurement]:
    return _check_event_labels("event_scope", resolver, path, "in_scope", ("out_of_scope",),
                               "in_scope must be Active or merged into an Active event; out_of_scope must not be Active")


def check_duplicates(conn, resolver: EventResolver, path: Path) -> Dict[str, Measurement]:
    # A labelled event can be split as well as merged: where the labelling-time records are kept beside the
    # labels, each side is the Active event holding most of them (dedup.label_contradictions.SideResolver).
    members_path = Path(path).parent / "duplicate_label_members.csv"
    if members_path.exists():
        from cyber_data_collector.dedup.label_contradictions import SideResolver, labelled_members

        resolver = SideResolver(conn, labelled_members(members_path))
    rows = read_labels(path, "pair_id")
    contradictions, unchecked, checked = [], defaultdict(int), 0
    for row in rows:
        label = row["label"].strip()
        if label not in ("same", "different"):
            unchecked[f"label {label!r}"] += 1
            continue
        parts = row["pair_id"].strip().split("|")
        if len(parts) != 2:
            unchecked["malformed pair_id"] += 1
            continue
        (a_now, a_status), (b_now, b_status) = resolver.resolve(parts[0]), resolver.resolve(parts[1])
        if {a_status, b_status} & {"missing", "untraceable", "ambiguous"}:
            unchecked["event missing or merge not traceable"] += 1
            continue
        checked += 1
        both_active = a_status == "Active" and b_status == "Active"
        if label == "same" and both_active and a_now != b_now:
            contradictions.append(f"{row['pair_id']}: labelled same, still two Active events ({a_now}, {b_now})")
        elif label == "different" and both_active and a_now == b_now:
            contradictions.append(f"{row['pair_id']}: labelled different, now one Active event {a_now}")
    return _result("duplicates", checked, contradictions, unchecked, len(rows),
                   "same must not be two Active events; different must not be one")


def check_source_gate(conn, resolver: EventResolver, path: Path) -> Dict[str, Measurement]:
    rows = read_labels(path, "enriched_event_id")
    status_of = dict(conn.execute("SELECT enriched_event_id, status FROM EnrichedEvents"))
    contradictions, unchecked, checked = [], defaultdict(int), 0
    for row in rows:
        label, record = row["label"].strip(), row["enriched_event_id"].strip()
        if label not in ("not_incident", "reports_incident"):
            unchecked[f"label {label!r}"] += 1
            continue
        if record not in status_of:
            unchecked["record no longer exists"] += 1
            continue
        checked += 1
        active_events = sorted(e for e in resolver.events_of.get(record, ()) if resolver.status.get(e) == "Active")
        if label == "not_incident":
            if active_events:
                contradictions.append(f"{record}: labelled not_incident, evidence of Active event "
                                      f"{', '.join(active_events)} - {row.get('rationale', '')[:80]}")
            elif not resolver.events_of.get(record) and status_of[record] == "Active":
                contradictions.append(f"{record}: labelled not_incident, unmapped but still Active")
        elif status_of[record] != "Active":
            contradictions.append(f"{record}: labelled reports_incident, record is {status_of[record]}")
    return _result("source_gate", checked, contradictions, unchecked, len(rows),
                   "not_incident pages must not be evidence of an Active event; "
                   "reports_incident pages must not be deactivated")


ACCEPTANCE_FILE = "review_acceptance_cases.csv"


def measure_acceptance(conn: sqlite3.Connection, thresholds=None, today=None,
                       evaluation_dir: Path = EVALUATION_DIR) -> Dict[str, Measurement]:
    """Whether the data satisfies the review's acceptance cases (``evaluation/review_acceptance_cases.csv``).

    Cases are things the reviewer said must hold of the published data, each from a real fragment of the
    database. ``victim <name>``: the (active) event the id now resolves to has a ``victim`` role for that
    organisation, after alias resolution. ``same_incident_as <id>``: both ids resolve to the same Active
    event. ``out_of_scope``: the event the id resolves to is Rejected (an Active one is a failure - it is
    still published). A case whose event no longer exists or cannot be traced is *unchecked*, never a pass. Warnings only: they say how far the
    rules have come, they do not close the gate.
    """
    from cyber_data_collector.dedup.victim_fill import names_match

    path = Path(evaluation_dir) / ACCEPTANCE_FILE
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if rows and not {"deduplicated_event_id", "expectation", "value"} <= set(rows[0]):
        raise ValueError(f"{path} lacks the deduplicated_event_id / expectation / value columns")
    resolver = EventResolver(conn)
    victims: Dict[str, List[str]] = defaultdict(list)
    for event_id, name in conn.execute(
            """SELECT dee.deduplicated_event_id, v.entity_name FROM DeduplicatedEventEntities dee
               JOIN EntitiesV2 v ON v.entity_id = dee.entity_id WHERE dee.relationship_type = 'victim'"""):
        victims[event_id].append(name)
    failed: Dict[str, List[str]] = {"victim": [], "same_incident_as": [], "out_of_scope": []}
    checked = Counter()
    unchecked: Dict[str, int] = defaultdict(int)
    for row in rows:
        kind, event_id, value = row["expectation"].strip(), row["deduplicated_event_id"].strip(), row["value"].strip()
        if kind not in failed:
            unchecked[f"{kind or 'blank'} (not read here)"] += 1
            continue
        now, status = resolver.resolve(event_id)
        if kind == "out_of_scope" and status in ("Active", "Rejected"):
            checked[kind] += 1
            if status == "Active":
                failed[kind].append(f"{event_id[:8]}: still Active as {now[:8]} (expected out of scope)")
            continue
        if status != "Active":
            unchecked[f"event {status}"] += 1
            continue
        checked[kind] += 1
        if kind == "victim":
            if not any(names_match(conn, value, name) for name in victims.get(now, ())):
                failed[kind].append(f"{event_id[:8]}: wanted victim {value!r}, has {victims.get(now, []) or 'none'}")
        else:
            other, other_status = resolver.resolve(value)
            if other_status != "Active":
                unchecked[f"event {other_status}"] += 1
                checked[kind] -= 1
            elif other != now:
                failed[kind].append(f"{event_id[:8]}: not in the same event as {value[:8]} "
                                    f"({now[:8]} / {other[:8]})")
    n_unchecked = sum(unchecked.values())
    breakdown = ", ".join(f"{k} {v}" for k, v in sorted(unchecked.items())) or "none"
    out: Dict[str, Measurement] = {}
    for kind, name in (("victim", "victim"), ("same_incident_as", "same_incident"), ("out_of_scope", "scope")):
        out[f"acceptance.{name}.failures"] = Measurement(
            value=len(failed[kind]), examples=failed[kind][:EXAMPLES],
            detail=f"{len(failed[kind])} of {checked[kind]} checkable {kind} case(s) not satisfied")
    out["acceptance.checked"] = Measurement(value=sum(checked.values()), detail=f"{len(rows)} cases in {ACCEPTANCE_FILE}")
    out["acceptance.unchecked"] = Measurement(value=n_unchecked, detail=f"cases not checked: {breakdown}")
    out.update(measure_validation_exceptions(conn))
    return out


def measure_validation_exceptions(conn: sqlite3.Connection) -> Dict[str, Measurement]:
    """The owner's manual validation exceptions (``ValidationExceptions``) the data still fails.

    Read-only: results are not written back (the measured database may be a snapshot). A database with no
    exceptions table has none to fail.
    """
    from cyber_data_collector.dedup import validation_exceptions as ve

    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'ValidationExceptions'").fetchone():
        return {"acceptance.exceptions.failures": Measurement(value=0, detail="no ValidationExceptions table")}
    results = ve.check(conn, record=False)
    failed = [f"{r.exception_id[:8]} {r.kind}: {r.detail}" for r in results if r.result == "fail"]
    unchecked = sum(r.result == "unchecked" for r in results)
    return {"acceptance.exceptions.failures": Measurement(
        value=len(failed), examples=failed[:EXAMPLES],
        detail=f"{len(failed)} of {len(results)} owner-reported validation exception(s) still fail; "
               f"{unchecked} unchecked (their records are in no single Active event)")}


LABEL_CHECKS: Dict[str, Callable] = {
    "event_reality": check_event_reality,
    "event_scope": check_event_scope,
    "duplicates": check_duplicates,
    "source_gate": check_source_gate,
}


def measure_labels(conn: sqlite3.Connection, thresholds=None, today=None,
                   evaluation_dir: Path = EVALUATION_DIR) -> Dict[str, Measurement]:
    out: Dict[str, Measurement] = {}
    resolver = EventResolver(conn)
    total, failed = 0, []
    for name, check in LABEL_CHECKS.items():
        path = Path(evaluation_dir) / LABEL_FILES[name]
        try:
            result = check(conn, resolver, path)
        except Exception as exc:  # noqa: BLE001 - reported as not measured
            failed.append(f"{name}: {type(exc).__name__}: {exc}")
            out[f"labels.{name}.contradictions"] = Measurement.unavailable(
                f"cannot check {path}: {type(exc).__name__}: {exc}")
            continue
        out.update(result)
        total += int(result[f"labels.{name}.contradictions"].value)
    if failed:
        out["labels.contradictions_total"] = Measurement.unavailable(
            "some label sets could not be checked: " + "; ".join(failed))
    else:
        examples = [e for name in LABEL_CHECKS for e in out[f"labels.{name}.contradictions"].examples]
        out["labels.contradictions_total"] = Measurement(
            value=total, examples=examples[:EXAMPLES],
            detail="labelled outcomes the current database contradicts, across "
                   + ", ".join(f"{n} {int(out[f'labels.{n}.contradictions'].value)}" for n in LABEL_CHECKS))
    return out


# ---------------------------------------------------------------------------
# Synthetic cases
# ---------------------------------------------------------------------------

def _merge_cases() -> List[Tuple[str, Callable[[], object], object]]:
    from cyber_data_collector.processing.deduplication_v2 import CyberEvent, DeduplicationEngine

    def decide(a: dict, b: dict, different: Tuple[str, ...] = ()):
        engine = DeduplicationEngine()
        if different:
            engine.different_pairs = {frozenset(different)}
        return engine.merge_decision(CyberEvent(**a), CyberEvent(**b))

    day, first = date(2024, 5, 14), date(2024, 5, 1)
    acme = {"title": "Acme Health ransomware attack", "victim_organization_name": "Acme Health"}
    return [
        ("merge.identical_title_date_victim",
         lambda: decide({"event_id": "1", "event_date": day, **acme}, {"event_id": "2", "event_date": day, **acme}),
         "identical title, date and victim"),
        ("merge.same_victim_same_day_corroborated",
         lambda: decide({"event_id": "1", "event_date": day, "records_affected": 50_000, **acme},
                        {"event_id": "2", "event_date": day, "title": "Acme Health confirms patient data stolen",
                         "victim_organization_name": "Acme Health", "records_affected": 48_000}),
         "same organisation on the same day, corroborated (record count)"),
        ("merge.same_victim_same_day_alone_is_a_candidate",
         lambda: decide({"event_id": "1", "event_date": day, **acme},
                        {"event_id": "2", "event_date": day, "title": "Acme Health confirms patient data stolen",
                         "victim_organization_name": "Acme Health"}),
         None),
        ("merge.same_victim_same_day_conflicting_counts_is_a_candidate",
         lambda: decide({"event_id": "1", "event_date": day, "records_affected": 50_000,
                         "urls": ["https://news.example/a"], **acme},
                        {"event_id": "2", "event_date": day, "title": "Acme Health confirms patient data stolen",
                         "victim_organization_name": "Acme Health", "records_affected": 5_000_000,
                         "threat_actors": ["Qilin"]}),
         None),
        ("merge.first_of_month_is_not_a_day",
         lambda: decide({"event_id": "1", "event_date": first, **acme},
                        {"event_id": "2", "event_date": first, "title": "Acme Health confirms patient data stolen",
                         "victim_organization_name": "Acme Health"}),
         None),
        ("merge.different_victims_generic_title",
         lambda: decide({"event_id": "1", "event_date": day, "title": "Data breach",
                         "victim_organization_name": "University of Sydney"},
                        {"event_id": "2", "event_date": day, "title": "Data breach",
                         "victim_organization_name": "University of Melbourne"}),
         None),
        ("merge.generic_title_no_victims",
         lambda: decide({"event_id": "1", "event_date": day, "title": "Data breach"},
                        {"event_id": "2", "event_date": day, "title": "Data Breach"}),
         None),
        ("merge.cached_different_ruling_vetoes",
         lambda: decide({"event_id": "1", "event_date": day, **acme}, {"event_id": "2", "event_date": day, **acme},
                        different=("1", "2")),
         None),
        ("merge.different_dates",
         lambda: decide({"event_id": "1", "event_date": day, **acme},
                        {"event_id": "2", "event_date": date(2024, 6, 20), **acme}),
         None),
    ]


def _records_cases() -> List[Tuple[str, Callable[[], object], object]]:
    from cyber_data_collector.utils.validation import validate_records_affected as v

    return [
        ("records.global_total_on_local_victim_rejected",
         lambda: v(275_000_000, "University of Melbourne caught in Canvas breach", "University of Melbourne"), None),
        ("records.genuine_global_victim_kept",
         lambda: v(560_000_000, "Ticketmaster/Live Nation breach", "Ticketmaster"), 560_000_000),
        ("records.plausible_australian_figure_kept", lambda: v(9_700_000, "Optus data breach", "Optus"), 9_700_000),
        ("records.below_minimum_rejected", lambda: v(10, "Acme Health data breach", "Acme Health"), None),
        ("records.negative_rejected", lambda: v(-5, "Acme Health data breach", "Acme Health"), None),
    ]


def _facts_cases() -> List[Tuple[str, Callable[[], object], object]]:
    from cyber_data_collector.dedup.event_facts import compute_facts

    event = {"title": "University of Melbourne caught in Canvas breach",
             "victim_organization_name": "University of Melbourne", "master_enriched_event_id": "m",
             "event_type": None, "severity": "High", "attack_method": None, "attacking_entity_name": None}
    # (enriched_event_id, records_affected, event_type, severity, attack_method, attacking_entity_name)
    members = [("m", 100, "Ransomware", "High", None, None),
               ("x", 50_000, "Data Breach", "Medium", "Credential theft", "ShinyHunters"),
               ("y", 275_000_000, "Data Breach", "Critical", None, "ShinyHunters")]
    return [
        ("facts.largest_valid_count_wins",
         lambda: compute_facts(event, members, {}).get("records_affected"), 50_000),
        ("facts.blank_filled_with_most_common",
         lambda: compute_facts(event, members, {}).get("event_type"), "Data Breach"),
        ("facts.master_value_stands", lambda: "severity" in compute_facts(event, members, {}), False),
        ("facts.override_wins",
         lambda: compute_facts(event, members, {"records_affected": "1234"}).get("records_affected"), 1234),
    ]


def _date_cases() -> List[Tuple[str, Callable[[], object], object]]:
    from cyber_data_collector.dedup.date_provenance import precision, record_dates

    meta = '{"publication_date": "2024-03-20"}'
    return [
        ("dates.precision_day", lambda: precision("2024-03-15"), "day"),
        ("dates.precision_month_placeholder", lambda: precision("2024-03-01"),
         "month only, or the 1st (stored as the 1st)"),
        ("dates.precision_year_placeholder", lambda: precision("2024-01-01"), "year only (stored as 1 January)"),
        ("dates.precision_missing", lambda: precision(None), "unknown"),
        ("dates.basis_publication",
         lambda: record_dates("2024-03-20", None, meta, "2024-04-02T10:00:00", None)["basis"],
         "publication date (may lag the incident)"),
        ("dates.basis_found",
         lambda: record_dates("2024-04-02", None, meta, "2024-04-02T10:00:00", None)["basis"],
         "date the record was found (may lag the incident)"),
        ("dates.basis_researched",
         lambda: record_dates("2024-03-05", None, meta, "2024-04-02T10:00:00",
                              '{"earliest_event_date": "2024-03-05"}')["basis"],
         "Perplexity research"),
        ("dates.basis_stated",
         lambda: record_dates("2024-03-07", None, meta, "2024-04-02T10:00:00", None,
                              text="The attack was detected on 7 March 2024.")["basis"],
         "stated in the record"),
        # A date that matches no source date and is not in the text is unknown, not "stated".
        ("dates.basis_unverified_is_unknown",
         lambda: record_dates("2024-03-07", None, meta, "2024-04-02T10:00:00", None)["basis"],
         "unknown (not a source date and not found in the text)"),
        # A month mention never supports an exact day.
        ("dates.month_does_not_support_a_day",
         lambda: record_dates("2024-03-07", None, meta, None, None, text="A breach in March 2024.")["basis"],
         "unknown (not a source date and not found in the text)"),
        ("dates.bad_metadata_tolerated",
         lambda: record_dates("2024-03-07", None, "{not json", None, None,
                              text="On 7 March 2024 the systems were encrypted.")["basis"],
         "stated in the record"),
    ]


CASE_GROUPS = (_merge_cases, _records_cases, _facts_cases, _date_cases)


def measure_synthetic(conn=None, thresholds=None, today=None) -> Dict[str, Measurement]:
    """Run every synthetic case; each is its own metric so a regression names itself."""
    out: Dict[str, Measurement] = {}
    for group in CASE_GROUPS:
        try:
            cases = group()
        except Exception as exc:  # noqa: BLE001 - the module under test failed to import
            out[f"synthetic.{group.__name__.strip('_')}"] = Measurement.unavailable(
                f"cannot load cases: {type(exc).__name__}: {exc}")
            continue
        for name, run, expected in cases:
            try:
                actual = run()
            except Exception as exc:  # noqa: BLE001
                out[f"synthetic.{name}"] = Measurement.unavailable(f"raised {type(exc).__name__}: {exc}")
                continue
            ok = actual == expected
            out[f"synthetic.{name}"] = Measurement(
                ok=ok, detail=f"expected {expected!r}" + ("" if ok else f", got {actual!r}"))
    return out
