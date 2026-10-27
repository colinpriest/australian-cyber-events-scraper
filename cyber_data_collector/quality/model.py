"""Metrics, thresholds and the publication verdict.

A *measurement* is what a check found (a value, or a pass/fail verdict, or the
reason it could not be found). A *rule* from ``thresholds.json`` turns it into
a *metric* with a status. Nothing defaults to passing:

* a check that raised or could not be computed is ``not_measured``, with the
  reason, and a ``not_measured`` blocking metric closes the gate exactly as a
  failure does;
* a rule that no check produced a metric for is itself reported as
  ``not_measured`` ("configured but not produced"), so renaming or deleting a
  check cannot quietly remove it from the gate;
* a metric with no rule is ``info`` - recorded for trends, never "pass".
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PASS = "pass"
FAIL = "fail"          # threshold breached on a blocking metric
WARN = "warn"          # threshold breached on a non-blocking metric
NOT_MEASURED = "not_measured"
INFO = "info"          # recorded, no threshold

GATE_CLOSING = (FAIL, NOT_MEASURED)

DEFAULT_THRESHOLDS_PATH = Path(__file__).resolve().parent / "thresholds.json"


@dataclass
class Measurement:
    """What one check found.

    Exactly one of ``value`` / ``ok`` normally carries the result; set
    ``not_measured`` (the reason) when the check could not be computed.
    """

    value: Optional[float] = None
    ok: Optional[bool] = None
    detail: str = ""
    examples: List[str] = field(default_factory=list)
    not_measured: Optional[str] = None

    @classmethod
    def unavailable(cls, reason: str) -> "Measurement":
        return cls(not_measured=reason)


@dataclass
class Metric:
    """A measurement judged against its rule."""

    name: str
    group: str
    status: str
    blocking: bool
    value: Optional[float] = None
    threshold: str = ""
    detail: str = ""
    examples: List[str] = field(default_factory=list)
    why: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Rule:
    match: str
    blocking: bool = False
    max: Optional[float] = None
    min: Optional[float] = None
    require: Optional[str] = None      # "pass": the measurement's own verdict must be ok
    why: str = ""

    @property
    def threshold_text(self) -> str:
        parts = []
        if self.max is not None:
            parts.append(f"<= {self.max:g}")
        if self.min is not None:
            parts.append(f">= {self.min:g}")
        if self.require == "pass":
            parts.append("must pass")
        return " and ".join(parts)

    def matches(self, name: str) -> bool:
        return fnmatch.fnmatchcase(name, self.match)


@dataclass
class Thresholds:
    version: str
    settings: Dict[str, Any]
    rules: List[Rule]
    path: str = ""
    sha256: str = ""

    def rule_for(self, name: str) -> Optional[Rule]:
        return next((r for r in self.rules if r.matches(name)), None)

    def setting(self, section: str, key: str) -> Any:
        """A required setting; a missing one is a configuration error, not a default."""
        try:
            return self.settings[section][key]
        except KeyError as exc:
            raise KeyError(f"thresholds setting {section}.{key} is missing from {self.path}") from exc


def load_thresholds(path: Optional[Path] = None) -> Thresholds:
    """Read the committed thresholds file. Malformed rules raise; nothing is guessed."""
    path = Path(path) if path else DEFAULT_THRESHOLDS_PATH
    raw_bytes = path.read_bytes()
    data = json.loads(raw_bytes.decode("utf-8"))
    rules = []
    allowed = {"match", "blocking", "max", "min", "require", "why"}
    for item in data["rules"]:
        unknown = set(item) - allowed
        if unknown:
            raise ValueError(f"{path}: rule {item.get('match')!r} has unknown keys {sorted(unknown)}")
        if item.get("require") not in (None, "pass"):
            raise ValueError(f"{path}: rule {item['match']!r}: require must be 'pass'")
        rules.append(Rule(**item))
    return Thresholds(version=str(data["version"]), settings=data.get("settings", {}), rules=rules,
                      path=str(path), sha256=hashlib.sha256(raw_bytes).hexdigest())


def evaluate(name: str, group: str, measurement: Measurement, thresholds: Thresholds) -> Metric:
    """Judge one measurement against the first rule matching its name."""
    rule = thresholds.rule_for(name)
    blocking = bool(rule and rule.blocking)
    metric = Metric(name=name, group=group, status=INFO, blocking=blocking, value=measurement.value,
                    threshold=rule.threshold_text if rule else "", detail=measurement.detail,
                    examples=list(measurement.examples), why=rule.why if rule else "")
    breached = FAIL if blocking else WARN
    if measurement.not_measured:
        metric.status = NOT_MEASURED
        metric.detail = measurement.not_measured + (f" ({measurement.detail})" if measurement.detail else "")
        return metric
    if rule is None:
        if measurement.ok is not None:
            metric.status = PASS if measurement.ok else WARN
        return metric
    if rule.max is not None or rule.min is not None:
        if measurement.value is None:
            metric.status = NOT_MEASURED
            metric.detail = "check produced no value" + (f" ({measurement.detail})" if measurement.detail else "")
            return metric
        within = ((rule.max is None or measurement.value <= rule.max)
                  and (rule.min is None or measurement.value >= rule.min))
        if rule.require == "pass" and measurement.ok is not None:
            within = within and measurement.ok
        metric.status = PASS if within else breached
        return metric
    if rule.require == "pass":
        if measurement.ok is None:
            metric.status = NOT_MEASURED
            metric.detail = "check produced no verdict" + (f" ({measurement.detail})" if measurement.detail else "")
        else:
            metric.status = PASS if measurement.ok else breached
        return metric
    if measurement.ok is not None:
        metric.status = PASS if measurement.ok else breached
    return metric


def unproduced_rules(metrics: List[Metric], thresholds: Thresholds) -> List[Metric]:
    """A placeholder ``not_measured`` metric for every rule that matched nothing."""
    out = []
    for rule in thresholds.rules:
        if not any(rule.matches(m.name) for m in metrics):
            out.append(Metric(name=rule.match, group=rule.match.split(".", 1)[0], status=NOT_MEASURED,
                              blocking=rule.blocking, threshold=rule.threshold_text, why=rule.why,
                              detail="configured in thresholds but no check produced it "
                                     "(the check failed before reporting, or was renamed)"))
    return out


def gate(metrics: List[Metric]) -> Tuple[bool, List[str]]:
    """Publication is allowed only when no blocking metric failed or went unmeasured."""
    reasons = []
    for m in metrics:
        if m.blocking and m.status in GATE_CLOSING:
            value = "" if m.value is None else f" = {m.value:g}"
            limit = f" (threshold {m.threshold})" if m.threshold else ""
            reasons.append(f"{m.name}{value}: {m.status}{limit} - {m.detail}".rstrip(" -"))
    return (not reasons), reasons
