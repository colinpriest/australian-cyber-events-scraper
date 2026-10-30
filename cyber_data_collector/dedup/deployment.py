"""The deployment rule: a calibrated step acts automatically only on enough labelled evidence.

A calibration that matches the current models, questions and evidence
(:mod:`calibration_guard`) licenses automatic actions - rejections, merges,
splits, fills, attachments. Its cut-off was chosen where the labelled precision
reaches the target (0.95), but a point estimate on 10 labelled actions and one
on 100 are not the same evidence: 10 of 10 correct is consistent with a true
precision of 0.74. Before a step acts, every automatic action its calibration
licenses must pass one rule, the same for every step:

* the exact one-sided **Clopper-Pearson lower bound** on the precision of the
  action, from the unweighted labelled counts at the cut-off, clears the
  policy's floor at the policy's confidence;
* where the calibration is **weighted**, the bound computed from Kish's
  **effective sample size** of the acted-on labelled items (with the weighted
  precision as the success rate) clears it too, so weighting cannot inflate n;
* the labelled actions (and their effective number) reach the policy's minimum;
* the **held-out evaluation** (``scripts/evaluate_calibrations.py``: the cut-off
  re-chosen on folds of the labels by the step's own selection function, the
  precision measured on the labels not used to choose it) exists for this very
  calibration and its lower bound clears the floor too (``require_heldout_evaluation``).

A step whose calibration fails is **report-only**, exactly as with a stale
calibration: :func:`gate` returns None for the step's primary action (and nulls
the cut-off of a failing secondary action), logging the numbers and what would
satisfy the rule. The thresholds live in one committed file,
``evaluation/deployment_policy.json``; a missing, unreadable or invalid policy
raises :class:`DeploymentPolicyError` - it never defaults to acting.

Recorded bootstrap out-of-sample figures are reported beside the decision but do
not gate (the policy file says why).
"""

from __future__ import annotations

import copy
import importlib
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "evaluation" / "deployment_policy.json"
POLICY_KEYS = ("confidence", "precision_floor", "min_labelled_actions", "require_heldout_evaluation",
               "heldout_folds")
HELDOUT_VERSION = 1


class DeploymentPolicyError(RuntimeError):
    """The deployment policy is missing, unreadable or invalid: nothing may act until it is fixed."""


# ----------------------------------------------------------------------
# Policy
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Policy:
    confidence: float
    precision_floor: float
    min_labelled_actions: int
    require_heldout_evaluation: bool
    heldout_folds: int
    version: str = ""
    path: str = ""

    def summary(self) -> str:
        return (f"one-sided {self.confidence:.0%} lower bound >= {self.precision_floor:.2f} on >= "
                f"{self.min_labelled_actions} labelled actions"
                + (", held-out evaluation required" if self.require_heldout_evaluation else ""))


def load_policy(path: Optional[Path] = None) -> Policy:
    """The committed deployment policy; raises :class:`DeploymentPolicyError` rather than assume one."""
    path = Path(path or POLICY_PATH)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DeploymentPolicyError(f"deployment policy {path} does not exist; no calibrated step may act "
                                    "without it (restore it from git)") from exc
    except (OSError, ValueError) as exc:
        raise DeploymentPolicyError(f"deployment policy {path} is unreadable: {exc}; no calibrated step may "
                                    "act until it is fixed") from exc
    if not isinstance(raw, dict):
        raise DeploymentPolicyError(f"deployment policy {path} is not a JSON object")
    missing = [k for k in POLICY_KEYS if k not in raw]
    if missing:
        raise DeploymentPolicyError(f"deployment policy {path} lacks {', '.join(missing)}")
    conf, floor, minimum = raw["confidence"], raw["precision_floor"], raw["min_labelled_actions"]
    heldout, folds = raw["require_heldout_evaluation"], raw["heldout_folds"]
    problems = []
    if not isinstance(conf, (int, float)) or isinstance(conf, bool) or not 0.5 <= conf < 1:
        problems.append(f"confidence {conf!r} is not a number in [0.5, 1)")
    if not isinstance(floor, (int, float)) or isinstance(floor, bool) or not 0 < floor < 1:
        problems.append(f"precision_floor {floor!r} is not a number in (0, 1)")
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
        problems.append(f"min_labelled_actions {minimum!r} is not a positive integer")
    if not isinstance(heldout, bool):
        problems.append(f"require_heldout_evaluation {heldout!r} is not true/false")
    if not isinstance(folds, int) or isinstance(folds, bool) or folds < 2:
        problems.append(f"heldout_folds {folds!r} is not an integer >= 2")
    if problems:
        raise DeploymentPolicyError(f"deployment policy {path} is invalid: {'; '.join(problems)}")
    return Policy(float(conf), float(floor), minimum, heldout, folds, str(raw.get("version", "")), str(path))


# ----------------------------------------------------------------------
# Statistics
# ----------------------------------------------------------------------

def clopper_pearson_lower(successes: float, n: float, confidence: float) -> float:
    """Exact one-sided lower confidence bound on a binomial proportion.

    The ``1 - confidence`` quantile of Beta(successes, n - successes + 1); 0 when
    there are no successes. Non-integer counts (an effective sample size) are
    accepted: the Beta form extends to them continuously.
    """
    if n <= 0:
        return 0.0
    if successes < 0 or successes > n + 1e-9:
        raise ValueError(f"successes {successes} outside [0, {n}]")
    successes = min(successes, n)
    if successes <= 0:
        return 0.0
    from scipy.stats import beta

    return float(beta.ppf(1.0 - confidence, successes, n - successes + 1.0))


def kish_effective_n(weights: Iterable[float]) -> float:
    """Kish's effective sample size ``(sum w)^2 / sum w^2`` (0 for no weights). Never above the count."""
    ws = [float(w) for w in weights]
    sq = sum(w * w for w in ws)
    return (sum(ws) ** 2) / sq if sq > 0 else 0.0


def more_needed(correct: float, acted: float, policy: Policy) -> Optional[int]:
    """Additional labelled actions, all correct, that would bring the bound and the count to the policy.

    None when no number up to 10,000 would (a precision far below the floor).
    """
    for k in range(0, 10001):
        if acted + k >= policy.min_labelled_actions and \
                clopper_pearson_lower(correct + k, acted + k, policy.confidence) >= policy.precision_floor:
            return k
    return None


# ----------------------------------------------------------------------
# Which actions each calibration licenses
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class ActionSpec:
    """One automatic action a calibration licenses, and where its numbers are in the calibration file."""

    name: str
    verb: str                          # "rejections", "merges", ... (for messages)
    cutoff_path: Tuple[str, ...]       # the cut-off's keys in the calibration
    at_cutoff_path: Tuple[str, ...]    # the labelled counts at that cut-off
    acted_key: str                     # the count of labelled items the action takes at the cut-off
    bootstrap_path: Tuple[str, ...] = ()
    direction: str = "below"           # "below": acts on p < cut-off; "at_or_above": on p >= cut-off
    gated: bool = True                 # False: a conservative use (a veto); reported, never gated


@dataclass(frozen=True)
class StepSpec:
    name: str
    module: str
    path_attr: str = "CALIBRATION_PATH"
    identity_attr: str = "calibration_identity"
    actions: Tuple[ActionSpec, ...] = ()
    primary: Optional[str] = None      # failing this makes the whole step report-only; None: actions independent

    def module_obj(self):
        return importlib.import_module(self.module)

    def calibration_path(self) -> Path:
        return Path(getattr(self.module_obj(), self.path_attr))

    def identity(self) -> Dict[str, Any]:
        return getattr(self.module_obj(), self.identity_attr)()


def _reject(name="reject", verb="rejections", **kw) -> ActionSpec:
    return ActionSpec(name, verb, ("cutoff",), ("at_cutoff",), "rejected_labelled",
                      ("bootstrap_out_of_sample_precision",), **kw)


def _merge(name="merge") -> ActionSpec:
    return ActionSpec(name, "merges", ("cutoff",), ("at_cutoff",), "merged_labelled",
                      ("bootstrap_out_of_sample_precision",), direction="at_or_above")


def _different_veto() -> ActionSpec:
    return ActionSpec("different-veto", "vetoes", ("different_cutoff",), ("different_at_cutoff",),
                      "rejected_labelled", ("different_bootstrap_out_of_sample_precision",), gated=False)


STEPS: Dict[str, StepSpec] = {s.name: s for s in (
    StepSpec("source-gate", "cyber_data_collector.dedup.source_gate", actions=(_reject(verb="page removals"),),
             primary="reject"),
    StepSpec("event-reality", "cyber_data_collector.dedup.event_reality", actions=(_reject(),), primary="reject"),
    StepSpec("event-scope", "cyber_data_collector.dedup.event_scope", actions=(_reject(),), primary="reject"),
    StepSpec("coherence", "cyber_data_collector.dedup.coherence", actions=(_reject("split", "splits"),),
             primary="split"),
    StepSpec("coherence-same-organisation", "cyber_data_collector.dedup.coherence_same_org",
             actions=(_reject("split", "splits"),), primary="split"),
    # The 'different' bound only holds links back (a veto can only prevent a merge): reported, not gated.
    StepSpec("duplicates", "cyber_data_collector.dedup.duplicate_judgement", actions=(_merge(), _different_veto()),
             primary="merge"),
    StepSpec("ai-duplicates", "cyber_data_collector.dedup.ai_duplicates", actions=(_merge(), _different_veto()),
             primary="merge"),
    StepSpec("victim-attribution", "cyber_data_collector.dedup.victim_judgement", actions=(
        _reject("act", "demotions / replacements"),
        ActionSpec("replace", "replacements", ("replacement_threshold",), ("replacement_at_threshold",),
                   "replaced_labelled", ("replacement_bootstrap_out_of_sample_precision",),
                   direction="at_or_above")), primary="act"),
    StepSpec("victim-fill", "cyber_data_collector.dedup.victim_fill", actions=(
        ActionSpec("fill", "fills", ("cutoff",), ("at_cutoff",), "replaced_labelled",
                   ("bootstrap_out_of_sample_precision",), direction="at_or_above"),), primary="fill"),
    # Victim attachments, same-incident merges and 'different' rulings (which attach a customer as a
    # co-victim) are independent: each is switched off on its own.
    StepSpec("supplier-victims", "cyber_data_collector.dedup.supplier_victims", actions=(
        ActionSpec("victim", "victim attachments", ("victim", "cutoff"), ("victim", "at_cutoff"),
                   "accepted_labelled", ("victim", "bootstrap_out_of_sample_precision"), direction="at_or_above"),
        ActionSpec("same-incident", "same-incident merges", ("same_incident", "cutoff"),
                   ("same_incident", "at_cutoff"), "accepted_labelled",
                   ("same_incident", "bootstrap_out_of_sample_precision"), direction="at_or_above"),
        ActionSpec("different", "'different' rulings (co-victim attachments)", ("same_incident", "different_cutoff"),
                   ("same_incident", "different_at_cutoff"), "rejected_labelled",
                   ("same_incident", "different_bootstrap_out_of_sample_precision"))), primary=None),
    StepSpec("ai-involvement", "cyber_data_collector.processing.ai_involvement", actions=(
        ActionSpec("flag", "AI labels", ("cutoff",), ("at_cutoff",), "flagged_labelled",
                   ("bootstrap_out_of_sample_precision",), direction="at_or_above"),), primary="flag"),
    StepSpec("proper-name", "cyber_data_collector.dedup.name_judgement",
             actions=(_reject("descriptive", "names ruled descriptive"),), primary="descriptive"),
    StepSpec("timeline-redates", "cyber_data_collector.dedup.timeline_reconciliation",
             path_attr="REDATE_CALIBRATION_PATH", identity_attr="redate_calibration_identity",
             actions=(ActionSpec("redate", "re-datings", ("cutoff",), ("at_cutoff",), "merged_labelled",
                                 ("bootstrap_out_of_sample_precision",), direction="at_or_above"),),
             primary="redate"),
)}


def _get(mapping: Any, path: Sequence[str]) -> Any:
    for key in path:
        if not isinstance(mapping, Mapping):
            return None
        mapping = mapping.get(key)
    return mapping


def _set(mapping: Dict[str, Any], path: Sequence[str], value: Any) -> None:
    for key in path[:-1]:
        mapping = mapping.setdefault(key, {})
    mapping[path[-1]] = value


# ----------------------------------------------------------------------
# Labelled rows (recorded by every calibration script)
# ----------------------------------------------------------------------

def standard_rows(rows: Iterable[Mapping[str, Any]], positive: str, id_key: str = "id", p_key: str = "p",
                  choice_key: Optional[str] = None, invert: bool = False) -> List[Dict[str, Any]]:
    """A calibration's labelled rows in one form: ``{id, stratum, weight, p, positive}``.

    ``positive`` names the row's key that says the *action* would be right for
    the item (``not_incident`` for a rejection, ``same`` for a merge, ``right``
    for a replacement; ``invert`` for a 'different' ruling read from ``same``).
    With ``choice_key``, a row without a choice has ``p`` None: the action never
    takes it. These rows let ``scripts/evaluate_calibrations.py`` evaluate the
    calibration on held-out labels with no database.
    """
    out = []
    for r in rows:
        p = r.get(p_key)
        if choice_key is not None and not r.get(choice_key):
            p = None
        is_positive = bool(r[positive])
        out.append({"id": str(r[id_key]), "stratum": str(r.get("stratum") or ""),
                    "weight": float(r.get("weight", 1.0)), "p": None if p is None else float(p),
                    "positive": (not is_positive) if invert else is_positive})
    return out


# ----------------------------------------------------------------------
# Held-out evaluations (written by scripts/evaluate_calibrations.py)
# ----------------------------------------------------------------------

def heldout_path_for(calibration_path: Path) -> Path:
    """``evaluation/x_calibration.json`` -> ``evaluation/x_heldout.json`` (beside the calibration it describes)."""
    p = Path(calibration_path)
    stem = p.name[:-len("_calibration.json")] if p.name.endswith("_calibration.json") else p.stem
    return p.with_name(f"{stem}_heldout.json")


def calibration_fingerprint(cal: Mapping[str, Any]) -> Dict[str, Any]:
    """What a held-out evaluation must match: the identity keys and when the calibration was generated."""
    return {k: cal.get(k) for k in ("question_version", "model", "evidence", "generated")}


def read_heldout(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    """``(evaluation, "")`` or ``(None, why there is none usable)``."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"no held-out evaluation ({Path(path).name} does not exist)"
    except (OSError, ValueError) as exc:
        return None, f"held-out evaluation {Path(path).name} unreadable: {exc}"
    if not isinstance(data, dict) or data.get("version") != HELDOUT_VERSION:
        return None, f"held-out evaluation {Path(path).name} has an unknown format"
    return data, ""


def matching_heldout(evaluation: Optional[Mapping[str, Any]], cal: Mapping[str, Any]) -> Tuple[bool, str]:
    if evaluation is None:
        return False, "no held-out evaluation"
    want = calibration_fingerprint(cal)
    have = evaluation.get("calibration") or {}
    diff = [k for k in want if have.get(k) != want[k]]
    if diff:
        return False, ("the held-out evaluation is of another calibration (" +
                       ", ".join(f"{k} {have.get(k)!r} vs {want[k]!r}" for k in diff) + ")")
    return True, ""


# ----------------------------------------------------------------------
# Assessment
# ----------------------------------------------------------------------

@dataclass
class ActionDecision:
    step: str
    action: str
    verb: str
    gated: bool
    cutoff: Optional[float]
    acted: Optional[int] = None
    correct: Optional[int] = None
    weighted_precision: Optional[float] = None
    effective_n: Optional[float] = None
    lower_bound: Optional[float] = None             # the bound that decides (the lowest of those computed)
    lower_bound_unweighted: Optional[float] = None
    lower_bound_effective: Optional[float] = None
    heldout: Optional[Dict[str, Any]] = None
    bootstrap: Optional[Dict[str, Any]] = None
    deploy: bool = False
    reasons: List[str] = field(default_factory=list)
    needed: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def describe(self) -> str:
        counts = (f"{self.correct} of {self.acted} labelled {self.verb} correct"
                  if self.acted is not None else "no labelled counts")
        lb = f"lower bound {self.lower_bound:.3f}" if self.lower_bound is not None else "no lower bound"
        if self.effective_n is not None and self.acted is not None and self.effective_n < self.acted - 1e-9:
            lb += f" (effective n {self.effective_n:.1f})"
        if self.heldout and self.heldout.get("lower_bound") is not None:
            lb += (f"; held-out {self.heldout.get('correct')}/{self.heldout.get('acted')}, "
                   f"bound {self.heldout['lower_bound']:.3f}")
        return f"{self.step} {self.action} at cut-off {self.cutoff}: {counts}, {lb}"


def _bound(correct: float, acted: float, policy: Policy) -> float:
    return clopper_pearson_lower(correct, acted, policy.confidence)


def assess_action(step: StepSpec, spec: ActionSpec, cal: Mapping[str, Any], policy: Policy,
                  heldout: Optional[Mapping[str, Any]], heldout_problem: str = "") -> ActionDecision:
    """Whether ``spec``'s automatic action may run on ``cal``, with the numbers and what would change it."""
    cutoff = _get(cal, spec.cutoff_path)
    d = ActionDecision(step.name, spec.name, spec.verb, spec.gated, cutoff)
    d.bootstrap = _get(cal, spec.bootstrap_path) if spec.bootstrap_path else None
    if cutoff is None:
        d.reasons.append("no cut-off reached the precision target (the action is off already)")
        return d
    at = _get(cal, spec.at_cutoff_path)
    if not isinstance(at, Mapping) or at.get(spec.acted_key) is None or at.get("correct_labelled") is None:
        d.reasons.append(f"the calibration records no labelled counts at its cut-off "
                         f"({'.'.join(spec.at_cutoff_path)}.{spec.acted_key}); recalibrate")
        return d
    acted, correct = int(at[spec.acted_key]), int(at["correct_labelled"])
    d.acted, d.correct = acted, correct
    d.weighted_precision = at.get("weighted_precision", at.get("precision"))
    d.lower_bound_unweighted = _bound(correct, acted, policy)
    bounds = [d.lower_bound_unweighted]
    needs = [more_needed(correct, acted, policy)]

    entry = (heldout or {}).get("actions", {}).get(spec.name) if heldout else None
    if entry is not None and not entry.get("reproduces"):
        heldout_problem = ("the held-out evaluation could not reproduce this action from stored scores ("
                           f"{entry.get('reproduction_note') or 'see the file'})")
        entry = None
    elif heldout is not None and entry is None:
        heldout_problem = "the held-out evaluation does not cover this action"
    # Weighted calibrations: the effective number of acted-on labelled items, from the calibration itself
    # (calibrations written since the rule) or from the matching held-out evaluation's full-label rows.
    weighted = "weighted_precision" in at
    n_eff = at.get("effective_n")
    if n_eff is None and entry:
        n_eff = (entry.get("full") or {}).get("effective_n")
    if n_eff is not None:
        d.effective_n = float(n_eff)
    if weighted:
        if n_eff is None:
            d.reasons.append("the calibration is weighted but its effective sample size is unknown (no "
                             "matching held-out evaluation: run scripts/evaluate_calibrations.py evaluate "
                             f"--step {step.name})")
        else:
            wp = float(d.weighted_precision) if d.weighted_precision is not None else correct / max(acted, 1)
            d.lower_bound_effective = _bound(wp * d.effective_n, d.effective_n, policy)
            bounds.append(d.lower_bound_effective)
            needs.append(more_needed(wp * d.effective_n, d.effective_n, policy))
            if d.effective_n < policy.min_labelled_actions:
                d.reasons.append(f"effective n {d.effective_n:.1f} < minimum {policy.min_labelled_actions}")
    d.lower_bound = min(bounds)
    if acted < policy.min_labelled_actions:
        d.reasons.append(f"{acted} labelled {spec.verb} < minimum {policy.min_labelled_actions}")
    if d.lower_bound < policy.precision_floor:
        d.reasons.append(f"lower bound {d.lower_bound:.3f} < floor {policy.precision_floor:.2f}")

    if entry and entry.get("heldout"):
        h = dict(entry["heldout"])
        d.heldout = h
        if h.get("acted") is not None:
            h_bounds = [_bound(h.get("correct", 0), h["acted"], policy)]
            h_needs = [more_needed(h.get("correct", 0), h["acted"], policy)]
            if h.get("effective_n") is not None and h.get("weighted_precision") is not None:
                h_bounds.append(_bound(h["weighted_precision"] * h["effective_n"], h["effective_n"], policy))
                h_needs.append(more_needed(h["weighted_precision"] * h["effective_n"], h["effective_n"], policy))
            h["lower_bound"] = min(h_bounds)
            if h["lower_bound"] < policy.precision_floor:
                d.reasons.append(f"held-out lower bound {h['lower_bound']:.3f} < floor {policy.precision_floor:.2f} "
                                 f"({h.get('correct')} of {h['acted']} held-out {spec.verb} correct)")
            if h["acted"] < policy.min_labelled_actions:
                d.reasons.append(f"{h['acted']} held-out labelled {spec.verb} < minimum "
                                 f"{policy.min_labelled_actions}")
            elif h.get("effective_n") is not None and h["effective_n"] < policy.min_labelled_actions:
                d.reasons.append(f"held-out effective n {h['effective_n']:.1f} < minimum "
                                 f"{policy.min_labelled_actions}")
            needs += h_needs
    elif policy.require_heldout_evaluation:
        d.reasons.append((heldout_problem or "no held-out evaluation of this action") +
                         f"; run 'python scripts/evaluate_calibrations.py evaluate --step {step.name}'")

    if any(n is None for n in needs):
        d.needed = "more labels would not suffice at this precision; the cut-off itself needs to change"
    else:
        k = max(needs)
        d.needed = (f"{k} more labelled {spec.verb}, all correct (each wrong one needs more)" if k else None)
    d.deploy = not d.reasons
    return d


def assess(step_name: str, cal: Mapping[str, Any], policy: Optional[Policy] = None,
           calibration_path: Optional[Path] = None) -> List[ActionDecision]:
    """Every automatic action of ``step_name``'s calibration, assessed against the policy."""
    step = STEPS[step_name]
    policy = policy or load_policy()
    heldout, problem = read_heldout(heldout_path_for(calibration_path or step.calibration_path()))
    if heldout is not None:
        ok, problem = matching_heldout(heldout, cal)
        if not ok:
            heldout = None
    return [assess_action(step, spec, cal, policy, heldout, problem) for spec in step.actions]


def report_only_message(d: ActionDecision, policy: Policy) -> str:
    """The log line a step writes when the rule switches an action off (one wording, so it is greppable)."""
    return (f"REPORT ONLY (deployment rule): {d.describe()} - {'; '.join(d.reasons)}."
            + (f" Would satisfy the rule: {d.needed}." if d.needed else "")
            + f" Policy: {policy.summary()} ({Path(policy.path).name}).")


def gate(step_name: str, cal: Optional[Dict[str, Any]], calibration_path: Optional[Path] = None,
         policy: Optional[Policy] = None, quiet: bool = False) -> Optional[Dict[str, Any]]:
    """``cal`` if its automatic actions pass the deployment rule; else what may still act.

    Called by every step's ``active_calibration`` after the identity check. A
    failing primary action returns None (the step is report-only, as for a
    stale calibration); a failing secondary action has its cut-off set to None
    in the returned copy (that action alone is off). A policy problem raises.
    ``quiet`` logs at INFO (for readers that load a calibration on every command
    and leave the WARNING to the command that acts).
    """
    if cal is None:
        return None
    step = STEPS[step_name]
    policy = policy or load_policy()
    decisions = assess(step_name, cal, policy, calibration_path)
    out = copy.deepcopy(dict(cal))
    failed = []
    for spec, d in zip(step.actions, decisions):
        if not spec.gated or d.cutoff is None or d.deploy:
            continue
        (logger.info if quiet else logger.warning)(report_only_message(d, policy))
        failed.append(spec.name)
        _set(out, spec.cutoff_path, None)
    if step.primary in failed:
        return None
    if failed:
        out["deployment_disabled"] = failed
    return out


# ----------------------------------------------------------------------
# Status (recalibrate --status, the quality report)
# ----------------------------------------------------------------------

def step_status(step_name: str, policy: Optional[Policy] = None) -> Dict[str, Any]:
    """The calibration's state and every action's deployment decision for one step (reads files only)."""
    from cyber_data_collector.dedup import calibration_guard

    step = STEPS[step_name]
    path = step.calibration_path()
    out: Dict[str, Any] = {"step": step_name, "path": str(path), "actions": []}
    try:
        cal = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {**out, "state": "missing", "reason": f"{path.name} does not exist", "deploys": False}
    except (OSError, ValueError) as exc:
        return {**out, "state": "unreadable", "reason": f"{path.name} unreadable: {exc}", "deploys": False}
    reason = calibration_guard.mismatch(cal, step.identity())
    decisions = assess(step_name, cal, policy or load_policy(), path)
    out["actions"] = [d.to_dict() for d in decisions]
    primary = next((d for d in decisions if d.action == step.primary), None)
    gated = [d for d in decisions if d.gated and d.cutoff is not None]
    if reason:
        state, why = "stale", reason
    elif not gated:
        state, why = "report-only", "no cut-off reached the precision target"
    elif primary is not None and not primary.deploy:
        state, why = "held", "; ".join(primary.reasons)
    elif all(d.deploy for d in gated):
        state, why = "deployed", ""
    elif any(d.deploy for d in gated):
        state, why = "partly", "; ".join(f"{d.action}: {', '.join(d.reasons)}" for d in gated if not d.deploy)
    else:
        state, why = "held", "; ".join(f"{d.action}: {', '.join(d.reasons)}" for d in gated)
    return {**out, "state": state, "reason": why, "deploys": state in ("deployed", "partly")}


def statuses(names: Optional[Sequence[str]] = None, policy: Optional[Policy] = None) -> List[Dict[str, Any]]:
    policy = policy or load_policy()
    return [step_status(n, policy) for n in (names or STEPS)]


def format_table(rows: Sequence[Mapping[str, Any]], policy: Policy) -> str:
    """A plain-text table of every step's actions (for ``recalibrate --status`` and the evaluation script)."""
    lines = [f"Deployment rule: {policy.summary()} ({Path(policy.path).name}, {policy.version})",
             f"{'step':28} {'action':15} {'cut-off':>8} {'labelled':>9} {'n_eff':>6} {'bound':>6} "
             f"{'held-out':>13} {'h.bound':>7}  decision"]
    for row in rows:
        if not row["actions"]:
            lines.append(f"{row['step']:28} {'-':15} {'-':>8} {'-':>9} {'-':>6} {'-':>6} {'-':>13} {'-':>7}  "
                         f"{row['state']}: {row['reason']}")
            continue
        for a in row["actions"]:
            cut = "-" if a["cutoff"] is None else f"{a['cutoff']:.3f}"
            lab = "-" if a["acted"] is None else f"{a['correct']}/{a['acted']}"
            ne = "-" if a["effective_n"] is None else f"{a['effective_n']:.1f}"
            lb = "-" if a["lower_bound"] is None else f"{a['lower_bound']:.3f}"
            h = a.get("heldout") or {}
            hl = "-" if h.get("acted") is None else f"{h.get('correct')}/{h['acted']}"
            hb = "-" if h.get("lower_bound") is None else f"{h['lower_bound']:.3f}"
            if not a["gated"]:
                verdict = "not gated (veto)"
            elif a["cutoff"] is None:
                verdict = "off (no cut-off)"
            elif row["state"] == "stale":
                verdict = "stale"
            else:
                verdict = "ACTS" if a["deploy"] else "REPORT ONLY"
            if a["gated"] and a["cutoff"] is not None and not a["deploy"] and row["state"] != "stale":
                verdict += " - " + "; ".join(a["reasons"])
                if a.get("needed"):
                    verdict += f"; would pass with {a['needed']}"
            lines.append(f"{row['step']:28} {a['action']:15} {cut:>8} {lab:>9} {ne:>6} {lb:>6} {hl:>13} {hb:>7}  "
                         f"{verdict}")
    return "\n".join(lines)
