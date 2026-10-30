"""Conclusion sentences for the repeat-event timing reports, generated from the numbers.

Every interpretive sentence in ``analyze_recurrent_timing.py``'s report and in
``build_recurrence_dashboard.py``'s page comes from here, so a conclusion
cannot drift from the table beside it. A sentence states the estimate (rate
ratio and 95% CI), which test was run (two-sided or one-sided, and in which
direction), its p-value, whether the estimate points the hypothesised way, and
how many events it rests on:

- p below ``ALPHA``: "statistically significant at the 5% level", and for a
  one-sided test whose two-sided CI still includes 1, that the evidence is
  one-sided only;
- otherwise: the data do not reject the null, with the CI; p in
  ``[ALPHA, BORDERLINE)`` is called borderline;
- an estimate on the other side of 1 from the hypothesis says so;
- fewer than ``MIN_EVENTS`` events in a compared period is flagged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

ALPHA = 0.05
BORDERLINE = 0.10
MIN_EVENTS = 10
# A covariate variant's CI counts as narrower only past this (log-scale width).
MATERIAL_NARROWING_PCT = 5.0

TESTS = ("two-sided", "greater", "less")

# Comparison -> (numerator period, denominator period), keyed as the phase
# columns of the phase-rate tables, with the wording used for each period.
DELAYED_PEAK_PERIODS: Dict[str, Tuple[str, str]] = {
    "91-180 vs 0-90": ("peak_91_180", "immediate_0_90"),
    "91-180 vs post-180": ("peak_91_180", "post_180"),
    "post-180 vs 0-90": ("post_180", "immediate_0_90"),
}
U_SHAPE_PERIODS: Dict[str, Tuple[str, str]] = {
    "response vs immediate": ("response", "immediate"),
    "long-term vs response": ("long_term", "response"),
    "long-term vs immediate": ("long_term", "immediate"),
}
PERIOD_LABELS: Dict[str, str] = {
    "immediate_0_90": "0-90 days",
    "peak_91_180": "91-180 days",
    "post_180": "post-180 days",
    "immediate": "the immediate period",
    "response": "the response period",
    "long_term": "the long-term period",
}


@dataclass(frozen=True)
class Contrast:
    """One rate-ratio comparison and the test run on it.

    ``test`` is the alternative the p-value was computed against.
    ``hypothesis`` is the direction the analysis expects ("greater", "less" or
    None); it equals ``test`` for a one-sided test, and may be set for a
    two-sided test so that an estimate in the wrong direction is still named.
    ``events`` maps each compared period's label to its repeat-event count.
    """

    comparison: str
    rate_ratio: float
    ci_low: float
    ci_high: float
    p_value: float
    test: str = "two-sided"
    hypothesis: Optional[str] = None
    events: Optional[Mapping[str, int]] = field(default=None)

    def __post_init__(self) -> None:
        if self.test not in TESTS:
            raise ValueError(f"test must be one of {TESTS}, not {self.test!r}")
        if self.hypothesis not in (None, "greater", "less"):
            raise ValueError(f"hypothesis must be 'greater', 'less' or None, not {self.hypothesis!r}")
        if self.test != "two-sided" and self.hypothesis not in (None, self.test):
            raise ValueError("a one-sided test's hypothesis is its own direction")

    @property
    def expected(self) -> Optional[str]:
        return self.hypothesis or (None if self.test == "two-sided" else self.test)

    @property
    def is_estimable(self) -> bool:
        return all(_finite(v) for v in (self.rate_ratio, self.ci_low, self.ci_high, self.p_value))

    @property
    def is_significant(self) -> bool:
        return self.is_estimable and self.p_value < ALPHA

    @property
    def is_opposite(self) -> bool:
        if not self.is_estimable or self.expected is None:
            return False
        return self.rate_ratio < 1 if self.expected == "greater" else self.rate_ratio > 1

    @property
    def ci_includes_one(self) -> bool:
        return self.ci_low <= 1 <= self.ci_high


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _p(value: float) -> str:
    return "< 0.001" if value < 0.001 else f"= {value:.3f}"


def _change(rr: float) -> str:
    if abs(rr - 1) < 0.005:
        return "about the same"
    return f"about {(rr - 1) * 100:.0f}% higher" if rr > 1 else f"about {(1 - rr) * 100:.0f}% lower"


def _null(test: str) -> str:
    return {
        "two-sided": "equal rates (rate ratio 1)",
        "greater": "the null of no increase (rate ratio at most 1)",
        "less": "the null of no decrease (rate ratio at least 1)",
    }[test]


def _word(direction: str) -> str:
    return "higher" if direction == "greater" else "lower"


def event_note(events: Optional[Mapping[str, int]], min_events: int = MIN_EVENTS) -> str:
    """How many events an estimate rests on, flagging small counts."""
    if not events:
        return "Event counts behind this estimate were not recorded."
    parts = [f"{int(n)} in {label}" for label, n in events.items()]
    text = "Repeat events: " + (", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0]) + "."
    small = [(label, int(n)) for label, n in events.items() if int(n) < min_events]
    if small:
        listed = ", ".join(f"{n} in {label}" for label, n in small)
        text += (
            f" Small count ({listed}; fewer than {min_events}): the estimate is imprecise"
            " and a few events either way could change it materially."
        )
    return text


def describe_contrast(contrast: Contrast, min_events: int = MIN_EVENTS) -> str:
    """One paragraph stating a contrast's estimate, test, verdict and support."""
    c = contrast
    if not c.is_estimable:
        return (
            f"{c.comparison}: not estimable from these data (the rate ratio, its interval or its"
            f" p-value is undefined). {event_note(c.events, min_events)}"
        )
    test_label = (
        f"two-sided p {_p(c.p_value)}" if c.test == "two-sided"
        else f"one-sided p {_p(c.p_value)} (alternative: {_word(c.test)})"
    )
    sentences = [
        f"{c.comparison}: rate ratio {c.rate_ratio:.2f} (95% CI {c.ci_low:.2f}-{c.ci_high:.2f}),"
        f" {_change(c.rate_ratio)}; {test_label}."
    ]
    if c.is_significant:
        verdict = f"Statistically significant at the 5% level: the data reject {_null(c.test)}."
        if c.ci_includes_one:
            verdict += " The two-sided 95% CI still includes 1, so the evidence is one-sided only."
    else:
        verdict = (
            f"Not statistically significant at the 5% level"
            f"{' (borderline)' if c.p_value < BORDERLINE else ''}:"
            f" the data do not reject {_null(c.test)}; the 95% CI runs from"
            f" {c.ci_low:.2f} to {c.ci_high:.2f}"
            f"{', which includes 1' if c.ci_includes_one else ''}."
        )
    sentences.append(verdict)
    if c.is_opposite:
        sentences.append(
            f"The estimate is in the opposite direction to the hypothesis"
            f" ({_word('less' if c.expected == 'greater' else 'greater')}, not {_word(c.expected)})."
        )
    sentences.append(event_note(c.events, min_events))
    return " ".join(sentences)


def describe_joint_test(
    name: str,
    hypothesis: str,
    p_value: float,
    components: Sequence[Contrast],
) -> str:
    """Verdict on an intersection test: every component must go the hypothesised way.

    ``hypothesis`` completes "the data support ..." (e.g. "a delayed 91-180 day
    peak"); ``components`` are the one-sided contrasts whose largest p-value is
    ``p_value``.
    """
    if not _finite(p_value) or not all(c.is_estimable for c in components):
        return f"The directional {name} test could not be computed from these data."
    listed = "; ".join(f"{c.comparison} {_word(c.expected or 'greater')}" for c in components)
    head = f"Directional {name} test (all of: {listed}), intersection p {_p(p_value)}."
    if p_value < ALPHA:
        verdict = f"The data support {hypothesis} at the 5% level (one-sided)."
    else:
        verdict = (
            f"Not statistically significant at the 5% level"
            f"{' (borderline)' if p_value < BORDERLINE else ''}:"
            f" the data do not reject the null of no {name}."
        )
    opposite = [c.comparison for c in components if c.is_opposite]
    if opposite:
        shape = "The point estimates do not have the hypothesised shape: " + ", ".join(opposite) + (
            " goes" if len(opposite) == 1 else " go"
        ) + " the other way."
    else:
        shape = "The point estimates have the hypothesised shape."
        weakest = max(components, key=lambda c: c.p_value)
        if p_value >= ALPHA:
            shape += f" The weakest part is {weakest.comparison} (p {_p(weakest.p_value)})."
    return " ".join([head, verdict, shape])


def describe_lrt(label: str, p_value: float, null: str, events: Optional[int] = None,
                 min_events: int = MIN_EVENTS) -> str:
    """Verdict on a likelihood-ratio test of ``null``."""
    if not _finite(p_value):
        return f"{label}: the likelihood-ratio test could not be computed from these data."
    if p_value < ALPHA:
        verdict = f"{label} rejects {null} at the 5% level (LRT p {_p(p_value)})."
    else:
        verdict = (
            f"{label} does not reject {null} at the 5% level"
            f" (LRT p {_p(p_value)}{', borderline' if p_value < BORDERLINE else ''})."
        )
    if events is None:
        return verdict
    verdict += f" It rests on {int(events)} observed repeats."
    if int(events) < min_events:
        verdict += f" Small count (fewer than {min_events}): the test has little power."
    return verdict


def contrast_from_record(
    row: Mapping[str, Any],
    periods: Mapping[str, Tuple[str, str]],
    events_by_period: Optional[Mapping[str, int]],
    hypothesis: Optional[str] = None,
) -> Contrast:
    """Build a Contrast from a ``contrast_from_fit`` row and the phase event counts.

    A row with a directional alternative is judged on its one-sided p-value;
    one without, on its two-sided p-value.
    """
    comparison = str(row["comparison"])
    alternative = str(row.get("directional_alternative") or "")
    if alternative:
        test, p_value = alternative, row.get("p_directional")
    else:
        test, p_value = "two-sided", row.get("p_two_sided")
    events = None
    if events_by_period is not None and comparison in periods:
        events = {
            PERIOD_LABELS.get(key, key): int(events_by_period[key])
            for key in periods[comparison]
            if key in events_by_period
        } or None
    return Contrast(
        comparison=comparison,
        rate_ratio=_float(row.get("rate_ratio")),
        ci_low=_float(row.get("ci_low")),
        ci_high=_float(row.get("ci_high")),
        p_value=_float(p_value),
        test=test,
        hypothesis=hypothesis if test == "two-sided" else None,
        events=events,
    )


def _float(value: Any) -> float:
    return float(value) if _finite(value) else math.nan


def describe_ci_width_sensitivity(rows: Iterable[Mapping[str, Any]], baseline_label: str) -> str:
    """How much dropping covariate groups changes each comparison's CI width.

    ``rows`` carry variant_label, comparison, ci_low and ci_high; widths are on
    the log scale, so a ratio's interval is compared multiplicatively.
    """
    by_comparison: Dict[str, List[Tuple[str, float, float, float]]] = {}
    for row in rows:
        low, high = _float(row.get("ci_low")), _float(row.get("ci_high"))
        if not (low > 0 and high > 0):
            continue
        width = math.log(high) - math.log(low)
        by_comparison.setdefault(str(row["comparison"]), []).append(
            (str(row["variant_label"]), width, low, high)
        )
    sentences = []
    for comparison, variants in by_comparison.items():
        base = [v for v in variants if v[0] == baseline_label]
        others = [v for v in variants if v[0] != baseline_label]
        if not base or not others:
            continue
        base_width = base[0][1]
        label, width, low, high = min(others, key=lambda v: v[1])
        narrowing = (base_width - width) / base_width * 100 if base_width > 0 else math.nan
        baseline = f"'{baseline_label}' model: {base[0][2]:.2f}-{base[0][3]:.2f}"
        if not _finite(narrowing) or narrowing < MATERIAL_NARROWING_PCT:
            sentences.append(
                f"For {comparison}, no variant narrows the 95% CI by {MATERIAL_NARROWING_PCT:.0f}% or more"
                f" on the log scale ({baseline})."
            )
        else:
            sentences.append(
                f"For {comparison}, the narrowest 95% CI is the '{label}' model's ({low:.2f}-{high:.2f}),"
                f" {narrowing:.0f}% narrower on the log scale ({baseline})."
            )
    return " ".join(sentences)


def describe_size_specification(rows: Iterable[Mapping[str, Any]]) -> List[str]:
    """Sentences on how organisation size is specified, from ``size_specification_comparison`` rows.

    For each banding: whether categorical size fits better than the numeric score (an LRT when the score
    is nested in it, otherwise AIC alone), whether each headline elapsed-time test reaches the same 5%
    verdict under both specifications, and what a size-by-phase interaction shows (or why it was not
    fitted).
    """
    table = [dict(r) for r in rows]
    sentences: List[str] = []
    for banding in dict.fromkeys(r["banding"] for r in table):
        by_spec = {r["size_spec"]: r for r in table if r["banding"] == banding}
        score, categorical = by_spec.get("score"), by_spec.get("categorical")
        if not score or not categorical:
            continue
        bands = score.get("elapsed_bands", banding)
        aic_s, aic_c = _float(score.get("aic")), _float(categorical.get("aic"))
        aic = (f"AIC {aic_c:.1f} (categorical) against {aic_s:.1f} (score), lower for the"
               f" {'categorical' if aic_c < aic_s else 'score'} model")
        p = _float(categorical.get("p_value"))
        if categorical.get("nested") and _finite(p):
            outcome = ("reject" if p < ALPHA else "do not reject")
            sentences.append(
                f"Size specification (bands {bands}): categorical size against the numeric size score, LRT p {_p(p)}"
                f" on {int(_float(categorical.get('df')))} df; at the 5% level the data {outcome} equal log-rate"
                f" steps between size bands; {aic}.")
        else:
            sentences.append(
                f"Size specification (bands {bands}): categorical size and the numeric score are not nested"
                f" ({categorical.get('note') or 'no likelihood-ratio test'}), so only AIC compares them: {aic}.")
        for label, key in (("the adjusted constant-hazard test", "elapsed_constant_hazard_p"),
                           ("the directional delayed-peak test", "delayed_peak_directional_p")):
            p_s, p_c = _float(score.get(key)), _float(categorical.get(key))
            if not (_finite(p_s) and _finite(p_c)):
                continue
            change = ("the same verdict at the 5% level" if (p_s < ALPHA) == (p_c < ALPHA)
                      else "the verdict at the 5% level changes")
            sentences.append(f"With bands {bands}, {label} gives p {_p(p_s)} with the size score and"
                             f" p {_p(p_c)} with categorical size: {change}.")
        interaction = by_spec.get("score_x_phase")
        if interaction:
            p_i = _float(interaction.get("p_value"))
            if _finite(p_i):
                outcome = ("reject" if p_i < ALPHA else "do not reject")
                sentences.append(f"Size by elapsed-time phase (the size score's slope in 91-180 and post-180 days"
                                 f" against 0-90): LRT p {_p(p_i)} on {int(_float(interaction.get('df')))} df; at the"
                                 f" 5% level the data {outcome} a size effect that is the same in every phase.")
            else:
                sentences.append(f"Size by elapsed-time phase was not fitted: {interaction.get('note')}.")
    return sentences
