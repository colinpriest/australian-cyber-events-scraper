"""Evaluate every calibrated cut-off on labels it was not chosen on, from stored scores only.

A calibration chooses its cut-off on the labelled cases and quotes the precision
on the same cases, which flatters it. This script measures each automatic
action of each calibrated step (``cyber_data_collector/dedup/deployment.py``
lists them) **out of sample**:

1. **Rows.** The labelled cases as the calibration script scored them
   (``{id, stratum, weight, p, positive}``), from the calibration file's
   ``labelled_rows`` (written by every calibration script since the deployment
   rule), its older ``rows`` / ``replacement_rows``, or - for a calibration
   written before that - **extracted** by re-running the step's own calibration
   script on a copy of the database it was calibrated on (``calibrated_on_database``,
   found under ``--instance`` or ``instance/recalibration``; ``--db STEP=PATH``
   overrides). Extraction is cache-only: the copy is made with the SQLite backup
   API, every API key is removed from the environment, every socket connection
   is refused, and the Jev and escalation clients refuse any request, so a case
   whose stored answer is missing fails the extraction instead of being bought.
   The extracted rows must **reproduce** the committed calibration (the same
   cut-off re-chosen, the same labelled counts at it); otherwise the evaluation
   says so and the deployment rule does not use it.
2. **Full labels.** The labelled items the committed cut-off acts on: counts,
   weighted precision and Kish's effective sample size.
3. **Held out.** The labels are split into ``heldout_folds`` folds (policy) by
   ``sha256(step|subject id) mod K`` - deterministic, independent of order and
   of the score. For each fold the cut-off is re-chosen on the other folds with
   **the step's own selection function** (``choose_cutoff``, ``merge_cutoff``,
   ``accept_cutoff``, ``replacement_threshold``, ``choose_different`` in
   ``scripts/calibrate_*.py``) and applied to the fold. Each label is held out
   once; the pooled held-out actions give a precision with an exact one-sided
   Clopper-Pearson lower bound (unweighted, and from the effective n where
   weighted). A fold whose training labels yield no cut-off contributes no
   actions and is counted.
4. **Seeded cases** (``--dedup-db``): ``scripts/evaluate_dedup.py``'s seeded
   splits and joins (and the labelled pairs) scored by the deterministic rule and
   the calibrated duplicate judgement from storage; recorded with the duplicates
   evaluation. A synthetic pair has no stored judgement, so the calibrated path
   leaves seeded cases undecided without paid calls; they are reported, not
   gated.

Output: ``evaluation/<step>_heldout.json`` beside each calibration, matched by the
deployment rule to the calibration's identity and ``generated`` time. Every
calibration script calls :func:`after_calibration` after writing, so a new
calibration gets its evaluation at once.

Usage (from the project root):
    python scripts/evaluate_calibrations.py evaluate                     # every step
    python scripts/evaluate_calibrations.py evaluate --step duplicates --dedup-db instance/cyber_events.db
    python scripts/evaluate_calibrations.py report [--json]              # deployment decisions, no evaluation
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import logging
import os
import socket
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.dedup import deployment  # noqa: E402

logger = logging.getLogger(__name__)

Rows = List[Dict[str, Any]]
Selector = Callable[[Rows, float, int], Optional[float]]

#: The calibration script of each step (its selection functions are the ones evaluated).
SCRIPTS = {
    "source-gate": "scripts.calibrate_source_gate", "event-reality": "scripts.calibrate_event_reality",
    "event-scope": "scripts.calibrate_event_scope", "coherence": "scripts.calibrate_coherence",
    "coherence-same-organisation": "scripts.calibrate_coherence_same_org",
    "duplicates": "scripts.calibrate_duplicates", "ai-duplicates": "scripts.calibrate_ai_duplicates",
    "victim-attribution": "scripts.calibrate_victim_attribution", "victim-fill": "scripts.calibrate_victim_fill",
    "supplier-victims": "scripts.calibrate_supplier_victims", "ai-involvement": "scripts.calibrate_ai_involvement",
    "proper-name": "scripts.calibrate_names", "timeline-redates": "scripts.calibrate_timeline_redates",
}
#: argv after ``--db`` for re-running a step's calibration (as recalibration.CALIBRATE).
CALIBRATE_ARGV = {"duplicates": []}
MIN_KEYS = ("min_rejections", "min_splits", "min_merges", "min_actions", "min_fills", "min_accepts", "min_flags")
MIN_ARG = {k: "--" + k.replace("_", "-") for k in MIN_KEYS + ("min_replacements",)}
ACTION_MIN_KEY = {("victim-attribution", "replace"): "min_replacements"}


# ----------------------------------------------------------------------
# Selection: each step's own function, fed the standard rows
# ----------------------------------------------------------------------

def _script(step: str):
    return importlib.import_module(SCRIPTS[step])


def _scored(rows: Rows) -> Rows:
    return [r for r in rows if r["p"] is not None]


def _below(step: str) -> Selector:
    """Reject below the cut-off (``choose_cutoff`` of calibrate_event_reality, as each script imports it)."""
    def select(rows: Rows, precision: float, minimum: int) -> Optional[float]:
        chosen, _ = _script(step).choose_cutoff(
            [{"p": r["p"], "weight": r["weight"], "not_incident": r["positive"], "stratum": r["stratum"]}
             for r in _scored(rows)], precision, minimum)
        return chosen["cutoff"] if chosen else None
    return select


def _duplicate_merge(step: str) -> Selector:
    def select(rows: Rows, precision: float, minimum: int) -> Optional[float]:
        chosen, _ = _script(step).merge_cutoff([{"p": r["p"], "same": r["positive"]} for r in _scored(rows)],
                                               precision, minimum)
        return chosen["cutoff"] if chosen else None
    return select


def _ai_merge(rows: Rows, precision: float, minimum: int) -> Optional[float]:
    at, _, _ = _script("ai-duplicates").merge_cutoff(
        [{"p": r["p"], "weight": r["weight"], "same": r["positive"], "stratum": r["stratum"]} for r in _scored(rows)],
        precision, minimum)
    return at["cutoff"] if at else None


def _supplier_accept(rows: Rows, precision: float, minimum: int) -> Optional[float]:
    at, _, _ = _script("supplier-victims").accept_cutoff(
        [{"p": r["p"], "weight": r["weight"], "positive": r["positive"], "stratum": r["stratum"]}
         for r in _scored(rows)], precision, minimum)
    return at["cutoff"] if at else None


def _different(weighted: bool) -> Selector:
    """``choose_different`` (calibrate_supplier_victims): rows are 'different is right'; it reads 'same'."""
    def select(rows: Rows, precision: float, minimum: int) -> Optional[float]:
        chosen, _ = _script("supplier-victims").choose_different(
            [{"p": r["p"], "weight": r["weight"] if weighted else 1.0, "positive": not r["positive"],
              "stratum": r["stratum"] if weighted else "census"} for r in _scored(rows)], precision, minimum)
        return chosen["cutoff"] if chosen else None
    return select


def _replacement(rows: Rows, precision: float, minimum: int) -> Optional[float]:
    chosen, _ = _script("victim-attribution").replacement_threshold(
        [{"choice": r["p"] is not None, "prob": r["p"] if r["p"] is not None else 0.0, "right": r["positive"],
          "weight": r["weight"]} for r in rows], precision, minimum)
    return chosen["threshold"] if chosen else None


def _ai_flag(rows: Rows, precision: float, minimum: int) -> Optional[float]:
    chosen, _ = _script("ai-involvement").choose_cutoff(
        [{"p": r["p"], "weight": r["weight"], "is_ai": r["positive"], "category_right": True,
          "stratum": r["stratum"]} for r in _scored(rows)], precision, minimum)
    return chosen["cutoff"] if chosen else None


SELECTORS: Dict[Tuple[str, str], Selector] = {
    ("source-gate", "reject"): _below("source-gate"),
    ("event-reality", "reject"): _below("event-reality"),
    ("event-scope", "reject"): _below("event-scope"),
    ("coherence", "split"): _below("coherence"),
    ("coherence-same-organisation", "split"): _below("coherence-same-organisation"),
    ("proper-name", "descriptive"): _below("proper-name"),
    ("victim-attribution", "act"): _below("victim-attribution"),
    ("victim-attribution", "replace"): _replacement,
    ("victim-fill", "fill"): _replacement,
    ("duplicates", "merge"): _duplicate_merge("duplicates"),
    ("duplicates", "different-veto"): _different(weighted=False),
    ("ai-duplicates", "merge"): _ai_merge,
    ("ai-duplicates", "different-veto"): _different(weighted=True),
    ("supplier-victims", "victim"): _supplier_accept,
    ("supplier-victims", "same-incident"): _supplier_accept,
    ("supplier-victims", "different"): _different(weighted=True),
    ("ai-involvement", "flag"): _ai_flag,
    ("timeline-redates", "redate"): _duplicate_merge("timeline-redates"),
}


def acts(spec: deployment.ActionSpec, row: Mapping[str, Any], cutoff: float) -> bool:
    p = row["p"]
    if p is None:
        return False
    return p < cutoff if spec.direction == "below" else p >= cutoff


def minimum_for(step: str, action: str, cal: Mapping[str, Any]) -> int:
    key = ACTION_MIN_KEY.get((step, action))
    if key:
        return int(cal[key])
    for k in MIN_KEYS:
        if cal.get(k) is not None:
            return int(cal[k])
    raise KeyError(f"{step}: the calibration records no minimum ({', '.join(MIN_KEYS)})")


def fold_of(step: str, subject_id: str, folds: int) -> int:
    """The held-out fold of a labelled subject: a hash of the step and id, so it never depends on order or score."""
    return int(hashlib.sha256(f"{step}|{subject_id}".encode("utf-8")).hexdigest(), 16) % folds


def summarise(rows: Rows, confidence: float) -> Dict[str, Any]:
    """Counts, weighted precision, Kish effective n and the exact lower bounds over acted-on rows."""
    acted = len(rows)
    correct = sum(1 for r in rows if r["positive"])
    w = sum(r["weight"] for r in rows)
    wp = (sum(r["weight"] for r in rows if r["positive"]) / w) if w else None
    n_eff = deployment.kish_effective_n(r["weight"] for r in rows)
    out = {"acted": acted, "correct": correct, "weighted_precision": wp, "effective_n": round(n_eff, 4),
           "lower_bound_unweighted": deployment.clopper_pearson_lower(correct, acted, confidence) if acted else None}
    if acted and wp is not None:
        out["lower_bound_effective"] = deployment.clopper_pearson_lower(wp * n_eff, n_eff, confidence)
    return out


def evaluate_action(step: str, spec: deployment.ActionSpec, rows: Rows, cal: Mapping[str, Any],
                    policy: deployment.Policy) -> Dict[str, Any]:
    """Full-label numbers at the committed cut-off, the reproduction check, and the held-out folds."""
    select = SELECTORS[(step, spec.name)]
    precision = float(cal.get("precision_target", 0.95))
    minimum = minimum_for(step, spec.name, cal)
    committed = deployment._get(cal, spec.cutoff_path)
    out: Dict[str, Any] = {"cutoff": committed, "precision_target": precision, "minimum": minimum,
                           "labelled_rows": len(rows)}
    if committed is None:
        out.update(reproduces=False, reproduction_note="the calibration has no cut-off for this action")
        return out
    reselected = select(rows, precision, minimum)
    full = summarise([r for r in rows if acts(spec, r, committed)], policy.confidence)
    at = deployment._get(cal, spec.at_cutoff_path) or {}
    want = (at.get(spec.acted_key), at.get("correct_labelled"))
    problems = []
    if reselected is None or abs(reselected - committed) > 1e-6:
        problems.append(f"the rows re-choose cut-off {reselected}, not {committed}")
    if (full["acted"], full["correct"]) != want:
        problems.append(f"the rows give {full['correct']}/{full['acted']} at the cut-off, the calibration "
                        f"{want[1]}/{want[0]}")
    out.update(full=full, reselected_cutoff=reselected, reproduces=not problems,
               reproduction_note="; ".join(problems))

    folds = policy.heldout_folds
    pooled: Rows = []
    per_fold, empty = [], 0
    for k in range(folds):
        train = [r for r in rows if fold_of(step, r["id"], folds) != k]
        test = [r for r in rows if fold_of(step, r["id"], folds) == k]
        cut = select(train, precision, minimum)
        taken = [r for r in test if cut is not None and acts(spec, r, cut)]
        empty += cut is None
        pooled += taken
        per_fold.append({"fold": k, "train": len(train), "test": len(test), "cutoff": cut, "acted": len(taken),
                         "correct": sum(1 for r in taken if r["positive"])})
    held = summarise(pooled, policy.confidence)
    held.update(folds=folds, folds_without_cutoff=empty, per_fold=per_fold,
                fold_key="sha256(step|subject id) mod folds")
    out["heldout"] = held
    return out


# ----------------------------------------------------------------------
# Rows
# ----------------------------------------------------------------------

def rows_in_calibration(step: str, cal: Mapping[str, Any]) -> Dict[str, Rows]:
    """The standard rows the calibration file itself carries (``labelled_rows``, or the older per-item lists)."""
    out = {k: list(v) for k, v in (cal.get("labelled_rows") or {}).items()}
    if step == "victim-fill" and "fill" not in out and cal.get("rows"):
        out["fill"] = deployment.standard_rows(cal["rows"], "right", p_key="prob", choice_key="choice")
    if step == "victim-attribution" and "replace" not in out and cal.get("replacement_rows"):
        out["replace"] = deployment.standard_rows(cal["replacement_rows"], "right", p_key="prob",
                                                  choice_key="choice")
    return out


class PaidCallRefused(RuntimeError):
    """An extraction needed an answer that is not stored; nothing is bought."""


class _RefusingClient:
    """Stands in for every API client during extraction: any request raises."""

    def __getattr__(self, name: str) -> "_RefusingClient":
        if name.startswith("__"):
            raise AttributeError(name)
        return self

    def __call__(self, *args, **kwargs):
        raise PaidCallRefused("a stored answer was missing; extraction makes no API calls")

    def close(self) -> None:
        return None


@contextmanager
def offline() -> Iterator[None]:
    """No API key in the environment and no socket connection while extracting."""
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.endswith("_API_KEY")}
    connect, create = socket.socket.connect, socket.create_connection

    def refuse(*args, **kwargs):
        raise PaidCallRefused("network refused during extraction (stored scores only)")

    socket.socket.connect = refuse          # type: ignore[assignment]
    socket.create_connection = refuse       # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket.connect, socket.create_connection = connect, create   # type: ignore[assignment]
        os.environ.update(saved)


def copy_database(source: Path, target: Path) -> Path:
    """A private copy through the SQLite backup API; the source is opened read-only and never written."""
    if not target.exists():
        src = sqlite3.connect(f"file:{Path(source).as_posix()}?mode=ro", uri=True)
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    return target


def find_database(step: str, cal: Mapping[str, Any], instance: Path, overrides: Mapping[str, str]) -> Path:
    if step in overrides:
        return Path(overrides[step])
    name = cal.get("calibrated_on_database")
    if not name:
        raise FileNotFoundError(f"{step}: the calibration names no calibrated_on_database; pass --db {step}=PATH")
    for candidate in (instance / "recalibration" / name, instance / name):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"{step}: {name} not found under {instance} or {instance / 'recalibration'}; "
                            f"pass --db {step}=PATH")


def extract_rows(step: str, cal: Mapping[str, Any], source: Path, workdir: Path) -> Dict[str, Rows]:
    """Re-run ``step``'s calibration script on a copy of ``source`` (cache-only) and return its labelled rows."""
    from cyber_data_collector.dedup import recalibration
    from cyber_data_collector.utils.jev import JevJudge

    spec = deployment.STEPS[step]
    copy = copy_database(source, workdir / f"{hashlib.sha256(str(source).encode()).hexdigest()[:12]}_{Path(source).name}")
    mod = spec.module_obj()
    out_path = workdir / f"{step}_calibration.json"
    with offline():
        if step in recalibration.PLANS:
            conn = sqlite3.connect(copy)
            judge = JevJudge(conn, client=_RefusingClient())
            try:
                plan = recalibration.PLANS[step](conn)
                plan.runner(conn, judge, _RefusingClient())
            finally:
                conn.close()
        argv = ["--db", str(copy), *CALIBRATE_ARGV.get(step, ["calibrate"]),
                "--precision", str(cal.get("precision_target", 0.95))]
        for key in MIN_KEYS + ("min_replacements",):
            if cal.get(key) is not None:
                argv += [MIN_ARG[key], str(cal[key])]
        saved = getattr(mod, spec.path_attr)
        setattr(mod, spec.path_attr, out_path)
        try:
            code = _script(step).main(argv)
        finally:
            setattr(mod, spec.path_attr, saved)
    if code not in (0, None):
        raise RuntimeError(f"{SCRIPTS[step]} exited {code}")
    regenerated = json.loads(out_path.read_text(encoding="utf-8"))
    return {k: list(v) for k, v in (regenerated.get("labelled_rows") or {}).items()}


# ----------------------------------------------------------------------
# Evaluation documents
# ----------------------------------------------------------------------

def evaluate_calibration(step: str, cal: Mapping[str, Any], rows: Mapping[str, Rows], sources: Mapping[str, str],
                         policy: deployment.Policy, problems: Optional[Mapping[str, str]] = None,
                         store_rows: Sequence[str] = ()) -> Dict[str, Any]:
    spec = deployment.STEPS[step]
    doc: Dict[str, Any] = {
        "version": deployment.HELDOUT_VERSION, "step": step, "generated": datetime.now().isoformat(),
        "calibration": deployment.calibration_fingerprint(cal), "policy_version": policy.version,
        "confidence": policy.confidence, "folds": policy.heldout_folds, "rows_source": dict(sources),
        "actions": {}, "rows": {a: rows[a] for a in store_rows if a in rows},
    }
    for action in spec.actions:
        if deployment._get(cal, action.cutoff_path) is None:
            continue
        if action.name not in rows:
            doc["actions"][action.name] = {
                "cutoff": deployment._get(cal, action.cutoff_path), "reproduces": False,
                "reproduction_note": (problems or {}).get(action.name) or "no labelled rows for this action"}
            continue
        doc["actions"][action.name] = evaluate_action(step, action, rows[action.name], cal, policy)
    doc["reproduces_calibration"] = bool(doc["actions"]) and all(a.get("reproduces") for a in doc["actions"].values())
    doc["reproduction_note"] = "; ".join(f"{k}: {a['reproduction_note']}" for k, a in doc["actions"].items()
                                         if a.get("reproduction_note"))
    return doc


def write_document(step: str, doc: Mapping[str, Any], calibration_path: Optional[Path] = None) -> Path:
    path = deployment.heldout_path_for(calibration_path or deployment.STEPS[step].calibration_path())
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


def evaluate_step(step: str, policy: deployment.Policy, instance: Optional[Path] = None,
                  overrides: Optional[Mapping[str, str]] = None, workdir: Optional[Path] = None,
                  write: bool = True) -> Optional[Dict[str, Any]]:
    """Evaluate one step's committed calibration; None when it has no calibration file."""
    spec = deployment.STEPS[step]
    path = spec.calibration_path()
    try:
        cal = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    rows = rows_in_calibration(step, cal)
    sources = {a: "calibration file" for a in rows}
    gated = [a.name for a in spec.actions if deployment._get(cal, a.cutoff_path) is not None]
    problems: Dict[str, str] = {}
    stored: List[str] = []
    if any(a not in rows for a in gated):
        if instance is None and not (overrides and step in overrides):
            problems = {a: "the calibration carries no labelled rows; rerun with --instance to extract them "
                           "from its database" for a in gated if a not in rows}
        else:
            try:
                source = find_database(step, cal, Path(instance or "instance"), overrides or {})
                extracted = extract_rows(step, cal, source, Path(workdir or tempfile.mkdtemp()))
            except Exception as exc:  # noqa: BLE001 - recorded: the rule then treats the step as unevaluated
                logger.error("%s: rows could not be extracted from stored scores: %s", step, exc)
                problems = {a: f"rows could not be extracted from stored scores ({type(exc).__name__}: {exc})"
                            for a in gated if a not in rows}
            else:
                for action, action_rows in extracted.items():
                    if action not in rows:
                        rows[action] = action_rows
                        sources[action] = f"extracted from {source.name} (stored scores, no API calls)"
                        stored.append(action)
    doc = evaluate_calibration(step, cal, rows, sources, policy, problems, store_rows=stored)
    if write:
        write_document(step, doc, path)
    return doc


def after_calibration(step: str) -> None:
    """Called by every calibration script after it writes: evaluate the new calibration and print the decision."""
    policy = deployment.load_policy()
    doc = evaluate_step(step, policy)
    if doc is None:
        return
    status = deployment.step_status(step, policy)
    print(deployment.format_table([status], policy))


# ----------------------------------------------------------------------
# Seeded cases (scripts/evaluate_dedup.py)
# ----------------------------------------------------------------------

def seeded_evaluation(db: Path, workdir: Path, splits: int = 100, joins: int = 100) -> Dict[str, Any]:
    """The rule and the calibrated judgement on evaluate_dedup's labelled and seeded cases (read-only, no calls)."""
    from scripts import evaluate_dedup as ed

    copy = copy_database(db, workdir / f"{hashlib.sha256(str(db).encode()).hexdigest()[:12]}_{Path(db).name}")
    args = ed.build_parser().parse_args(["--db", str(copy), "--paths", "rule", "judgement", "--skip-rule-impact",
                                         "--seeded-splits", str(splits), "--seeded-joins", str(joins)])
    with offline():
        result = ed.run(copy, args)
    # Joined although known (labels) or assumed (seeded joins) different: the cases to review.
    examples = {p: [x for x in e.get("examples", []) if x["expect"] == "different" and x["decision"]]
                for p, e in result["paths"].items()}
    return {"source": Path(db).name, "seed": result["seed"], "cases": result["cases"],
            "paths": {p: {k: v for k, v in e.items() if k != "examples"} for p, e in result["paths"].items()},
            "wrong_examples": examples,
            "note": ("Reported, not gated. A seeded split is synthetic (two halves of one event), so the calibrated "
                     "judgement has no stored answer for it and leaves it undecided without paid calls. A seeded "
                     "join is a real pair of one organisation's events more than --join-gap-days apart, judged from "
                     "storage, but its 'different' is an assumption, not a label: misdated copies of one incident "
                     "lie that far apart (6 of the 17 labelled-same organisation pairs are 707-4,603 days apart), "
                     "so a join merged at the cut-off is a case to review, not a measured error.")}


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def cmd_evaluate(args) -> int:
    policy = deployment.load_policy()
    names = args.step or list(deployment.STEPS)
    overrides = {}
    for item in args.db or []:
        step, sep, value = item.partition("=")
        if not sep or step not in deployment.STEPS:
            print(f"--db wants STEP=PATH with a known step, got {item!r}", file=sys.stderr)
            return 2
        overrides[step] = value
    exit_code = 0
    with tempfile.TemporaryDirectory(dir=args.workdir, ignore_cleanup_errors=True) as scratch:
        for step in names:
            doc = evaluate_step(step, policy, Path(args.instance), overrides, Path(scratch))
            if doc is None:
                print(f"{step}: no calibration file - nothing to evaluate")
                continue
            if step == "duplicates" and args.dedup_db:
                doc["seeded"] = seeded_evaluation(Path(args.dedup_db), Path(scratch))
                write_document(step, doc)
            if not doc["reproduces_calibration"]:
                exit_code = 1
            for name, a in doc["actions"].items():
                h = a.get("heldout") or {}
                full = a.get("full") or {}
                print(f"{step:28} {name:15} cut-off {a.get('cutoff')}: full {full.get('correct')}/"
                      f"{full.get('acted')} (n_eff {full.get('effective_n')}), held-out {h.get('correct')}/"
                      f"{h.get('acted')} over {h.get('folds')} folds ({h.get('folds_without_cutoff')} without a "
                      f"cut-off); reproduces: {a.get('reproduces')} {a.get('reproduction_note') or ''}")
    print()
    print(deployment.format_table(deployment.statuses(names, policy), policy))
    return exit_code


def cmd_report(args) -> int:
    policy = deployment.load_policy()
    rows = deployment.statuses(args.step or None, policy)
    if args.json:
        print(json.dumps({"policy": policy.__dict__, "steps": rows}, indent=2, default=str))
    else:
        print(deployment.format_table(rows, policy))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("evaluate", help="evaluate calibrations on held-out labels and write <step>_heldout.json")
    p.add_argument("--step", nargs="*", choices=list(deployment.STEPS))
    p.add_argument("--instance", default="instance", help="where calibrated_on_database is looked for")
    p.add_argument("--db", action="append", help="STEP=PATH: the database a step's rows are extracted from")
    p.add_argument("--dedup-db", help="also score evaluate_dedup's seeded cases on (a copy of) this database")
    p.add_argument("--workdir", default=None, help="where database copies are made (deleted afterwards)")
    p.set_defaults(func=cmd_evaluate)
    p = sub.add_parser("report", help="the deployment decision for every step (reads files only)")
    p.add_argument("--step", nargs="*", choices=list(deployment.STEPS))
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_report)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
