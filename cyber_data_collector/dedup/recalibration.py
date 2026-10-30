"""Re-score the labelled cases on current evidence, and recalibrate each stale cut-off.

A calibrated cut-off applies only to the evidence processing it was measured on
(:mod:`calibration_guard`). When a reader changes - pages withheld, records
filtered, character limits moved - the stored calibration no longer applies and
its step falls back to reporting. This module is how a step gets its cut-off
back without anyone remembering the procedure:

1. **Find the labelling-time snapshot.** Labels describe the database as it was
   when they were made; since then events were merged, split and rejected, so
   most labelled items are no longer active in the live database and cannot be
   re-judged there. ``evaluation/calibration_snapshots.json`` names, per step, a
   backup beside the live database taken before those changes.
2. **Work on a copy.** The snapshot is copied to ``<instance>/recalibration/
   <step>.db`` and the live database's stored organisation timelines and
   corroboration searches are carried over (a search cannot be replayed, and
   re-searching would measure something the pipeline does not use). The copy
   is kept between runs: Jev and escalation answers are keyed on a hash of the
   evidence, so a repeat run - or a resumed one - buys only what is missing.
3. **Re-score every labelled case with the current readers** - the same
   functions the pipeline uses, so current evidence is exactly what a model is
   shown. A case that cannot be scored raises; nothing is calibrated on a
   silently smaller label set.
4. **Recompute the cut-off** with the step's own calibration script, which
   records the new evidence fingerprint beside the model and question version.
   If far fewer labelled cases were usable than in the calibration being
   replaced (``--min-coverage``), the old file is restored and the step fails.

Limits, stated because they bound what the new cut-off proves. The snapshot
holds the *records, research and pages as they were at labelling time*; the
readers and the questions are today's. A change the live data has had since
(re-enrichment, re-scraped pages, a corrected date) is not in the measurement,
so the fingerprint certifies the reader, not the data. The strata weights come
from the original sample file and are not redrawn (they describe the labelling
population). A step whose snapshot is gone, whose keys are missing, or whose
labels are not in the snapshot fails with exit code 2 and stays report-only.
"""

from __future__ import annotations

import csv
import importlib
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from cyber_data_collector.dedup import calibration_guard
from cyber_data_collector.dedup.event_reality import ROOT

logger = logging.getLogger(__name__)

SNAPSHOT_REGISTRY = ROOT / "evaluation" / "calibration_snapshots.json"
WORK_DIR_NAME = "recalibration"
DEFAULT_PRECISION = 0.95
DEFAULT_MIN_COVERAGE = 0.9


class RecalibrationError(RuntimeError):
    """A step could not be recalibrated; its old calibration is left (or restored) untouched."""


class PrerequisiteMissing(RecalibrationError):
    """A snapshot, key or label file is missing: nothing was spent."""


@dataclass(frozen=True)
class StepSpec:
    name: str
    module: str               # module with CALIBRATION_PATH, calibration_identity()
    escalates: bool           # needs the OpenAI key as well as Jev's
    automatic: bool = True    # False: recalibrate with its own calibration script
    carries: Tuple[str, ...] = ()   # stored research (tables) carried from the live database into the copy
    min_present: float = 0.0  # fewest labelled cases the snapshot may lack: present/labelled below this fails


SPECS: Dict[str, StepSpec] = {s.name: s for s in (
    StepSpec("source-gate", "cyber_data_collector.dedup.source_gate", escalates=False),
    StepSpec("event-reality", "cyber_data_collector.dedup.event_reality", escalates=True),
    StepSpec("event-scope", "cyber_data_collector.dedup.event_scope", escalates=True),
    StepSpec("coherence", "cyber_data_collector.dedup.coherence", escalates=True),
    StepSpec("coherence-same-organisation", "cyber_data_collector.dedup.coherence_same_org", escalates=True),
    StepSpec("duplicates", "cyber_data_collector.dedup.duplicate_judgement", escalates=True),
    # AI-involved pairs (any victim) have their own question and cut-off; labelled apart from the above.
    StepSpec("ai-duplicates", "cyber_data_collector.dedup.ai_duplicates", escalates=True),
    StepSpec("victim-attribution", "cyber_data_collector.dedup.victim_judgement", escalates=True),
    # Needs its labelling-time snapshot named in the registry (or --snapshot victim-fill=PATH) once labels exist.
    StepSpec("victim-fill", "cyber_data_collector.dedup.victim_fill", escalates=True),
    # Its scoring is the find-supplier-victims candidate generation, run on the snapshot with the live
    # database's stored per-incident searches. A victim candidate named only by a search made at labelling
    # time (2026-10-27, not stored) cannot be rebuilt, so some labelled victims are absent: 70 of 103.
    StepSpec("supplier-victims", "cyber_data_collector.dedup.supplier_victims", escalates=True,
             carries=("SupplierIncidentSearches",), min_present=0.65),
)}


def _module(spec: StepSpec):
    return importlib.import_module(spec.module)


# ----------------------------------------------------------------------
# State
# ----------------------------------------------------------------------

def calibration_state(spec: StepSpec, path: Optional[Path] = None) -> Dict[str, Any]:
    """``{"step", "state", "reason", ...}``; state is current / stale / missing / unreadable / report-only."""
    mod = _module(spec)
    path = Path(path or mod.CALIBRATION_PATH)
    out: Dict[str, Any] = {"step": spec.name, "path": str(path), "automatic": spec.automatic}
    try:
        cal = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {**out, "state": "missing", "reason": f"{path.name} does not exist"}
    except (OSError, ValueError) as exc:
        return {**out, "state": "unreadable", "reason": f"{path.name} unreadable: {exc}"}
    reason = calibration_guard.mismatch(cal, mod.calibration_identity())
    out.update(cutoff=cal.get("cutoff"), evidence=cal.get("evidence"), generated=cal.get("generated"))
    if reason:
        return {**out, "state": "stale", "reason": reason}
    if "cutoff" in cal and cal.get("cutoff") is None:
        return {**out, "state": "report-only", "reason": "current, but no cut-off reached the precision target"}
    return {**out, "state": "current", "reason": ""}


def states(names: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    return [calibration_state(SPECS[n]) for n in (names or SPECS)]


def needs_recalibration(state: Dict[str, Any]) -> bool:
    return state["state"] in ("stale", "missing", "unreadable")


# ----------------------------------------------------------------------
# Snapshots
# ----------------------------------------------------------------------

def registered_snapshot(step: str, live_db: Path, overrides: Optional[Dict[str, str]] = None,
                        registry: Path = SNAPSHOT_REGISTRY) -> Path:
    """The labelling-time database for ``step``: an explicit override, else the registry entry beside ``live_db``."""
    override = (overrides or {}).get(step)
    if override:
        path = Path(override)
    else:
        try:
            names = json.loads(Path(registry).read_text(encoding="utf-8"))["snapshots"]
        except (OSError, ValueError, KeyError) as exc:
            raise PrerequisiteMissing(f"snapshot registry {registry} unreadable: {exc}") from exc
        if step not in names:
            raise PrerequisiteMissing(f"{registry.name} names no labelling snapshot for {step}; pass "
                                      f"--snapshot {step}=<path>")
        path = Path(live_db).resolve().parent / names[step]
    if not path.is_file():
        raise PrerequisiteMissing(f"labelling snapshot for {step} not found: {path}. Without it the labelled "
                                  "cases cannot be found as they were labelled (merges, splits and rejections "
                                  "since have changed the live database); restore it or pass "
                                  f"--snapshot {step}=<a backup taken before the labels were applied>")
    return path


def work_database(step: str, live_db: Path, snapshot: Path, fresh: bool = False,
                  extra_tables: Tuple[str, ...] = ()) -> Path:
    """The recalibration copy for ``step``: made from the snapshot once, then kept so answers are reused."""
    from scripts import prepare_calibration_snapshot as prep

    work = Path(live_db).resolve().parent / WORK_DIR_NAME / f"{step}.db"
    work.parent.mkdir(parents=True, exist_ok=True)
    if fresh and work.exists():
        work.unlink()
    if work.exists():
        added = prep.carry_over(str(work), str(live_db), extra_tables)
        logger.info("Reusing recalibration copy %s (carried over %s)", work, added)
    else:
        added = prep.prepare(str(snapshot), str(work), str(live_db), extra_tables)
        logger.info("Made recalibration copy %s from %s (carried over %s)", work, snapshot.name, added)
    return work


# ----------------------------------------------------------------------
# Scoring the labelled cases on current evidence
# ----------------------------------------------------------------------

def _csv_column(path: Path, column: str) -> List[str]:
    with Path(path).open(encoding="utf-8") as fh:
        return [r[column] for r in csv.DictReader(fh)]


def _require_scored(kind: str, wanted: Sequence[str], got: Dict[str, Any]) -> None:
    missing = sorted(set(wanted) - set(got))
    if missing:
        raise RecalibrationError(f"{len(missing)} labelled {kind}(s) could not be scored on current evidence "
                                 f"(first: {missing[0]}); see the log. Nothing was calibrated.")


def _require_agreement(kind: str, returned: Dict[str, float], stored: Dict[str, float]) -> None:
    """The calibration scripts read the latest stored answers; they must be the ones just obtained."""
    for key, value in returned.items():
        if key not in stored or abs(stored[key] - value) > 1e-9:
            raise RecalibrationError(f"stored final score for {kind} {key} ({stored.get(key)}) is not the one "
                                     f"just obtained ({value}); refusing to calibrate on stale answers")


@dataclass
class Plan:
    """What a step would score: the labelled cases the snapshot holds, out of those labelled."""

    items: List[Any]
    labelled: int
    runner: Callable[..., None]

    @property
    def present(self) -> int:
        return len(self.items)


def _plan_event_reality(conn: sqlite3.Connection) -> Plan:
    from cyber_data_collector.dedup import event_reality as er
    from scripts import calibrate_event_reality as cal

    labels = {r["deduplicated_event_id"]: r["label"] for r in _rows(er.LABELS_PATH)}
    wanted = [d for d in _csv_column(cal.SAMPLE_PATH, "deduplicated_event_id") if labels.get(d) in ("real", "not_incident")]
    active = set(er.active_events(conn))
    items = [d for d in wanted if d in active]

    def run(conn, judge, client):
        states: Dict[str, Dict[str, Any]] = {}      # each case's evidence, built once for all three steps
        jev = er.judge_events(conn, items, judge=judge, version=er.QUESTION_VERSION, states=states)
        _require_scored("event", items, jev)
        failed: List[str] = []
        escalated = er.escalate_band(conn, jev, client=client, failed=failed, states=states)
        if failed:
            raise RecalibrationError(f"{len(failed)} event(s) in the escalation band could not be escalated "
                                     f"(first: {failed[0]}); see the log. Nothing was calibrated.")
        final = er.record_final(conn, jev, escalated, states=states)
        _require_agreement("event", final, er.stored_final_probabilities(conn, items))

    return Plan(items, len(wanted), run)


def _plan_event_scope(conn: sqlite3.Connection) -> Plan:
    from cyber_data_collector.dedup import event_reality as er
    from cyber_data_collector.dedup import event_scope as es
    from scripts import calibrate_event_scope as cal

    known = es.labels(es.LABELS_PATH, er.LABELS_PATH)
    cases = [c for c in cal.calibration_cases(conn, cal.SAMPLE_PATH, known)
             if c["label"] in ("in_scope", "out_of_scope")]
    active = set(er.active_events(conn))
    items = [c["id"] for c in cases if c["id"] in active]

    def run(conn, judge, client):
        states: Dict[str, Dict[str, Any]] = {}      # each case's evidence, built once for all three steps
        jev = es.judge_events(conn, items, judge=judge, states=states)
        _require_scored("event", items, jev)
        failed: List[str] = []
        escalated = es.escalate_band(conn, jev, client=client, failed=failed, states=states)
        if failed:
            raise RecalibrationError(f"{len(failed)} event(s) in the escalation band could not be escalated "
                                     f"(first: {failed[0]}); see the log. Nothing was calibrated.")
        final = es.record_final(conn, jev, escalated, states=states)
        _require_agreement("event", final, es.stored_final_probabilities(conn, items))

    return Plan(items, len(cases), run)


def _plan_duplicates(conn: sqlite3.Connection) -> Plan:
    from cyber_data_collector.dedup import duplicate_judgement as dj

    labels = dj.labelled_pairs()
    wanted = {pid for pid, label in labels.items() if label in ("same", "different")}
    items = [p for p in dj.candidate_pairs(conn) if p["pair_id"] in wanted]

    def run(conn, judge, client):
        # Each final score is stored against the evidence it was made on (FinalDecisions); the calibration
        # script reads exactly those, so it cannot pick up an answer given on other evidence.
        scored = dj.score_pairs(conn, items, dj.ORGANISATION, judge=judge, escalation_client=client)
        _require_scored("pair", [p["pair_id"] for p in items], scored.jev)
        if scored.failed:
            raise RecalibrationError(f"{len(scored.failed)} pair(s) in the escalation band could not be escalated "
                                     f"(first: {scored.failed[0]}); see the log. Nothing was calibrated.")
        _require_agreement("pair", scored.final, dj.stored_final_probabilities(conn, items))

    return Plan(items, len(wanted), run)


def _plan_ai_duplicates(conn: sqlite3.Connection) -> Plan:
    from cyber_data_collector.dedup import ai_duplicates as ad
    from cyber_data_collector.dedup import duplicate_judgement as dj

    labels = ad.labelled_pairs(required=True)
    wanted = {pid for pid, label in labels.items() if label in ("same", "different")}
    items = [p for p in ad.candidate_pairs(conn) if p["pair_id"] in wanted]

    def run(conn, judge, client):
        scored = dj.score_pairs(conn, items, ad.ROUTE, judge=judge, escalation_client=client)
        _require_scored("pair", [p["pair_id"] for p in items], scored.jev)
        if scored.failed:
            raise RecalibrationError(f"{len(scored.failed)} pair(s) in the escalation band could not be escalated "
                                     f"(first: {scored.failed[0]}); see the log. Nothing was calibrated.")
        _require_agreement("pair", scored.final, ad.stored_final_probabilities(conn, items))

    return Plan(items, len(wanted), run)


def _plan_coherence(conn: sqlite3.Connection) -> Plan:
    from cyber_data_collector.dedup import coherence as co
    from scripts import calibrate_coherence as cal

    labels = co.load_labels()
    sample = [r for r in cal._sample() if r["stratum"] == "S"
              and labels.get(co.member_subject_id(r["deduplicated_event_id"], r["enriched_event_id"])) in ("same", "different")]
    events = cal._events(conn)
    items = [(events[r["deduplicated_event_id"]], r["enriched_event_id"]) for r in sample
             if r["deduplicated_event_id"] in events
             and r["enriched_event_id"] in events[r["deduplicated_event_id"]]["suspect_ids"]]

    def run(conn, judge, client):
        scores = co.judge_members(conn, items, judge=judge, escalation_client=client)
        undecided = sorted(s for s, v in scores.items() if v.get("error"))
        if undecided:
            raise RecalibrationError(f"{len(undecided)} member(s) could not be escalated (first: {undecided[0]}); "
                                     "see the log. Nothing was calibrated.")
        _require_agreement("member", {s: v["p"] for s, v in scores.items()}, co.stored_final_probabilities(conn, items))

    return Plan(items, len(sample), run)


def _plan_source_gate(conn: sqlite3.Connection) -> Plan:
    from cyber_data_collector.dedup import source_gate as sg

    labels = sg.labels(required=True)
    wanted = [i for i in _csv_column(sg.SAMPLE_PATH, "enriched_event_id")
              if labels.get(i) in ("reports_incident", "not_incident")]
    records = {r["enriched_event_id"]: r for r in sg.google_records(conn)}
    items = [records[i] for i in wanted if i in records and sg.judgeable(records[i])]
    # Pages with too little text are not calibrated on, so they are not "labelled cases" to find.
    judgeable_labelled = len([i for i in wanted if i not in records or sg.judgeable(records[i])])

    def run(conn, judge, client):
        sg.judge_records(conn, items, judge=judge)        # raises on any failure

    return Plan(items, judgeable_labelled, run)


def _plan_victim_attribution(conn: sqlite3.Connection) -> Plan:
    from cyber_data_collector.dedup import victim_judgement as vj
    from scripts import calibrate_victim_attribution as cal

    labels = vj.load_labels(required=True)
    wanted = [pid for pid in _csv_column(cal.SAMPLE_PATH, "pair_id")
              if (labels.get(pid) or {}).get("label") in ("correct", "wrong")]
    present = {p["pair_id"]: p for p in vj.pairs_to_check(conn)}
    items = [present[pid] for pid in wanted if pid in present]

    def run(conn, judge, client):
        vj.judge_pairs(conn, items, judge=judge, escalation_client=client)    # raises on any failure
        # The replacement threshold's choices are made (and stored) by the calibration script.

    return Plan(items, len(wanted), run)


def _plan_victim_fill(conn: sqlite3.Connection) -> Plan:
    from cyber_data_collector.dedup import victim_fill as vf
    from scripts import calibrate_victim_fill as cal

    labels = vf.load_labels(required=True)
    wanted = [d for d in _csv_column(cal.SAMPLE_PATH, "deduplicated_event_id")
              if (labels.get(d) or {}).get("label") in ("named", "none_named")]
    present = {e["deduplicated_event_id"] for e in vf.victimless_events(conn)}
    items = [d for d in wanted if d in present]

    def run(conn, judge, client):
        for dedup_id in items:      # raises on any failure (extraction, escalation); stored answers are reused
            vf.choose_victim(conn, dedup_id, judge, escalation_client=client)

    return Plan(items, len(wanted), run)


def _plan_coherence_same_org(conn: sqlite3.Connection) -> Plan:
    from cyber_data_collector.dedup import coherence_same_org as so
    from scripts import calibrate_coherence_same_org as cal

    return cal.plan(conn, so)


def _plan_supplier_victims(conn: sqlite3.Connection) -> Plan:
    """The labelled victim candidates and event pairs that candidate generation still produces on the snapshot."""
    from cyber_data_collector.dedup import supplier_victims as sv
    from scripts import calibrate_supplier_victims as cal

    sample, labels = cal.read_sample(), sv.read_labels(sv.LABELS_PATH)
    wanted = {kind: {sid for (k, sid) in sample if k == kind and labels.get((k, sid)) not in (None, "unsure")}
              for kind in cal.KINDS}
    g = sv.gather(conn)
    by_id = g.by_id
    victims = [(c.subject_id, sv.build_victim_state(conn, by_id[c.dedup_id], c))
               for cs in g.candidates.values() for c in cs if c.subject_id in wanted[sv.KIND_VICTIM]]
    pairs = [(p.pair_id, sv.build_pair_state(conn, by_id[p.supplier_id], p))
             for p in g.pairs if p.pair_id in wanted[sv.KIND_PAIR]]

    def run(conn, judge, client):
        for kind, items in ((sv.KIND_VICTIM, victims), (sv.KIND_PAIR, pairs)):
            scores = sv.judge(conn, items, kind, judge, client)
            if scores.failed or scores.undecided:
                bad = scores.failed + scores.undecided
                raise RecalibrationError(
                    f"{len(bad)} labelled {kind} item(s) could not be judged or escalated (first: {bad[0]}); "
                    "see the log. Nothing was calibrated.")
            _require_scored(kind, [sid for sid, _ in items], scores.final)
            _require_agreement(kind, scores.final, sv.stored_final_probabilities(conn, kind, items))

    return Plan(victims + pairs, sum(len(w) for w in wanted.values()), run)


def _rows(path: Path) -> List[Dict[str, str]]:
    with Path(path).open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


PLANS: Dict[str, Callable[[sqlite3.Connection], Plan]] = {
    "source-gate": _plan_source_gate, "event-reality": _plan_event_reality, "event-scope": _plan_event_scope,
    "coherence": _plan_coherence,
    "coherence-same-organisation": _plan_coherence_same_org, "duplicates": _plan_duplicates,
    "victim-attribution": _plan_victim_attribution, "supplier-victims": _plan_supplier_victims,
    "victim-fill": _plan_victim_fill, "ai-duplicates": _plan_ai_duplicates,
}

#: argv for each step's calibration script (``--db`` and the model arguments are added by the caller).
CALIBRATE: Dict[str, Tuple[str, List[str]]] = {
    "source-gate": ("scripts.calibrate_source_gate", ["calibrate"]),
    "event-reality": ("scripts.calibrate_event_reality", ["calibrate"]),
    "event-scope": ("scripts.calibrate_event_scope", ["calibrate"]),
    "coherence": ("scripts.calibrate_coherence", ["calibrate"]),
    "coherence-same-organisation": ("scripts.calibrate_coherence_same_org", ["calibrate"]),
    "duplicates": ("scripts.calibrate_duplicates", []),
    "ai-duplicates": ("scripts.calibrate_ai_duplicates", ["calibrate"]),
    "victim-attribution": ("scripts.calibrate_victim_attribution", ["calibrate"]),
    "supplier-victims": ("scripts.calibrate_supplier_victims", ["calibrate"]),
    "victim-fill": ("scripts.calibrate_victim_fill", ["calibrate"]),
}


def check_keys(spec: StepSpec) -> None:
    """Fail before anything is spent when a key the step needs is absent (no fallback to another model)."""
    from cyber_data_collector.utils import jev

    try:
        jev.require()
    except Exception as exc:  # noqa: BLE001 - JevUnavailable: say what is missing
        raise PrerequisiteMissing(str(exc)) from exc
    if spec.escalates and not os.getenv("OPENAI_API_KEY"):
        raise PrerequisiteMissing("OPENAI_API_KEY is not set; the escalation model is required and no other "
                                  "model will be substituted")


def recalibrate_step(name: str, live_db: Path, snapshot: Optional[Path] = None,
                     judge_factory: Optional[Callable[[sqlite3.Connection], Any]] = None,
                     escalation_client: Any = None, precision: float = DEFAULT_PRECISION,
                     min_coverage: float = DEFAULT_MIN_COVERAGE, fresh: bool = False,
                     verify_keys: bool = True, dry_run: bool = False,
                     calibrate_args: Sequence[str] = ()) -> Dict[str, Any]:
    """Re-score ``name``'s labelled cases on current evidence and rewrite its calibration file.

    Returns a summary (``before`` / ``after`` cut-offs and fingerprints, cases scored). With ``dry_run``
    nothing is copied, judged or written: the labelled cases the snapshot holds are counted from it
    directly (read-only). Any failure leaves the previous calibration file exactly as it was.
    ``judge_factory(conn)`` builds the Jev judge on the recalibration copy (tests pass a fake client);
    ``calibrate_args`` go to the step's calibration script (for instance ``--min-rejections``).
    """
    spec = SPECS[name]
    mod = _module(spec)
    if not spec.automatic:
        raise RecalibrationError(f"{name} is not recalibrated here: run its own calibration script "
                                 "on a labelling-time snapshot")
    snapshot = snapshot or registered_snapshot(name, live_db)
    before = calibration_state(spec)
    if dry_run:
        if spec.carries:
            # Candidate generation needs the carried research, which the snapshot lacks: count on a throwaway copy.
            import tempfile

            from scripts import prepare_calibration_snapshot as prep

            with tempfile.TemporaryDirectory() as scratch:
                copy = Path(scratch) / "dry_run.db"
                prep.prepare(str(snapshot), str(copy), str(live_db), spec.carries)
                conn = sqlite3.connect(copy)
                try:
                    plan = PLANS[name](conn)
                finally:
                    conn.close()
        else:
            conn = sqlite3.connect(f"file:{Path(snapshot).as_posix()}?mode=ro", uri=True)
            try:
                plan = PLANS[name](conn)
            finally:
                conn.close()
        return {"step": name, "dry_run": True, "snapshot": str(snapshot), "before": before,
                "labelled": plan.labelled, "present_in_snapshot": plan.present,
                "would_become": mod.calibration_identity()["evidence"]}
    if verify_keys:
        check_keys(spec)
    work = work_database(name, live_db, Path(snapshot), fresh=fresh, extra_tables=spec.carries)
    cal_path = Path(mod.CALIBRATION_PATH)
    old_text = cal_path.read_text(encoding="utf-8") if cal_path.exists() else None
    old = json.loads(old_text) if old_text else {}
    # The calibration script also rewrites the held-out evaluation beside it (deployment rule): restored with it.
    from cyber_data_collector.dedup.deployment import heldout_path_for

    heldout_path = heldout_path_for(cal_path)
    old_heldout = heldout_path.read_text(encoding="utf-8") if heldout_path.exists() else None

    from cyber_data_collector.utils.jev import JevJudge

    conn = sqlite3.connect(work)
    judge = (judge_factory or JevJudge)(conn)
    try:
        plan = PLANS[name](conn)
        if not plan.items:
            raise RecalibrationError(f"{name}: none of the {plan.labelled} labelled cases is in {snapshot.name}")
        if plan.present < spec.min_present * plan.labelled:
            raise RecalibrationError(
                f"{name}: only {plan.present} of {plan.labelled} labelled case(s) are in {snapshot.name} "
                f"(< {spec.min_present:.0%}); nothing was spent. Old calibration kept.")
        logger.info("%s: scoring %d of %d labelled case(s) on current evidence", name, plan.present, plan.labelled)
        plan.runner(conn, judge, escalation_client)
    finally:
        if hasattr(judge, "close"):
            judge.close()
        conn.close()

    module_name, argv = CALIBRATE[name]
    script = importlib.import_module(module_name)
    try:
        code = script.main(["--db", str(work), *argv, "--precision", str(precision), *calibrate_args])
        if code not in (0, None):
            raise RecalibrationError(f"{module_name} exited {code}")
        new = json.loads(cal_path.read_text(encoding="utf-8"))
        used, was = new.get("labelled_used"), old.get("labelled_used")
        if isinstance(used, int) and isinstance(was, int) and used < min_coverage * was:
            raise RecalibrationError(
                f"{name}: only {used} labelled case(s) were usable against {was} in the calibration being "
                f"replaced (< {min_coverage:.0%}); the snapshot does not hold enough of them. Old calibration kept.")
        new["recalibration"] = {
            "reason": "evidence processing changed" if before["state"] == "stale" else before["state"],
            "labelling_snapshot": Path(snapshot).name, "work_copy": work.name,
            "previous": {"evidence": old.get("evidence"), "cutoff": old.get("cutoff"),
                         "generated": old.get("generated"), "labelled_used": was},
            "cases_scored": plan.present, "cases_labelled": plan.labelled,
        }
        cal_path.write_text(json.dumps(new, indent=2), encoding="utf-8")
    except BaseException:
        if old_text is not None:
            cal_path.write_text(old_text, encoding="utf-8")
        if old_heldout is not None:
            heldout_path.write_text(old_heldout, encoding="utf-8")
        elif heldout_path.exists():
            heldout_path.unlink()
        raise
    after = calibration_state(spec)
    logger.info("%s recalibrated: cut-off %s -> %s, evidence %s -> %s", name, old.get("cutoff"), new.get("cutoff"),
                old.get("evidence"), new.get("evidence"))
    return {"step": name, "dry_run": False, "snapshot": str(snapshot), "before": before, "after": after,
            "cases_scored": plan.present, "cases_labelled": plan.labelled}
