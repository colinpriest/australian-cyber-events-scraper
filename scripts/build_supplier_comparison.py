"""Build the supplier vs non-supplier comparison (``dashboard/supplier_comparison.html``).

Active events of the reporting window (2020 onward) are split in two groups and compared attribute by
attribute, with a significance test for each at the bottom of the page.

**Supplier event.** An Active event that is linked to a supplier incident - as a customer's event
(``SupplierIncidentLinks.customer_event_id``) or as the supplier's own event
(``SupplierIncidents.supplier_event_id``) - or that records a vendor (a ``vendor`` role, or
``DeduplicatedEvents.vendor_organization_name``) or a service provider (a ``service_provider`` role).
Every other Active event of the window is a non-supplier event.

**Unit.** The event, not the victim organisation-event pair. The tests assume independent observations,
and an event with several victims would enter once per victim with the same severity, attack type,
records count, ASD classification, AI category and date. A victim attribute (size band, entity type) is
taken for an event when all its victim organisations share it; an event whose victims differ, or that
records no victim, is left out of that attribute's test and counted.

**Tests.** Categorical attributes: Pearson's chi-squared test of homogeneity with Cramér's V. Where more
than 20% of expected counts are under 5 (or one is under 1) sparse categories are pooled by a fixed rule
(:func:`pool_sparse`); a 2 x 2 table that is still sparse is tested with Fisher's exact test, whose effect
size is the phi coefficient computed from the counts (no chi-squared appears in its row). Records
affected: a two-sided Mann-Whitney U test. Unknown and missing values are left out of each test and
counted per group. p-values are Holm-adjusted across the tests that ran. Events of one supplier incident
are not independent of each other, so every test is repeated with one event per supplier incident.

Every sentence of the page's conclusions is generated from the numbers; a result that is not significant
after adjustment is reported as "no evidence of a difference", with no direction. A significant one is
never a bare "difference": it carries the size of its effect ("negligible difference", "small difference",
"medium difference", "large difference"), because with this many events a difference too small to matter
is still statistically significant.

The page is self-contained (no scripts, no external files): charts are HTML and CSS, in light and dark
mode, readable at phone width.

Usage:
    python scripts/build_supplier_comparison.py                      # the latest published snapshot
    python scripts/build_supplier_comparison.py --snapshot-id <id>   # a retained snapshot
    python scripts/build_supplier_comparison.py --db PATH            # this database, opened read-only
"""

from __future__ import annotations

import argparse
import html
import logging
import math
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cyber_data_collector.reporting.population import (  # noqa: E402
    ReportingPeriod, dashboard_period, incident_filter,
)

logger = logging.getLogger(__name__)

DEFAULT_DB = "instance/cyber_events.db"
DEFAULT_OUT = "dashboard/supplier_comparison.html"

#: Significance level, applied to the Holm-adjusted p-value.
ALPHA = 0.05
#: The expected-count rule (Cochran): pool when more than this share of cells is under MIN_EXPECTED ...
MIN_EXPECTED = 5.0
MAX_SPARSE_SHARE = 0.20
#: ... or when any expected count is under this.
MIN_ANY_EXPECTED = 1.0
#: A category is named in a conclusion when its adjusted standardised residual is beyond this.
RESIDUAL_CUT = 1.96
#: The effect size reported beside Fisher's exact test: the phi coefficient of the 2 x 2 table.
PHI_NAME = "phi coefficient |φ|"
#: The words for the size of an effect, at Cohen's cut-offs: for Cramér's V (two groups, so V equals Cohen's w)
#: and for the absolute rank-biserial correlation of the Mann-Whitney test (Cohen's cut-offs for r).
EFFECT_WORDS = ((0.5, "large"), (0.3, "medium"), (0.1, "small"), (0.0, "negligible"))
POOLED_LABEL = "Sparse categories (pooled)"

SUPPLIER = "Supplier events"
OTHER = "Non-supplier events"

#: Why an event is a supplier event. An event can have several.
BASES: Tuple[Tuple[str, str], ...] = (
    ("customer_event", "linked to a supplier incident as a customer's event"),
    ("supplier_event", "the supplier's own event of a supplier incident"),
    ("vendor", "records a vendor"),
    ("service_provider", "records a service provider"),
)

SIZE_ORDER = ("SMALL", "MEDIUM", "LARGE", "HUGE")
SEVERITY_ORDER = ("Critical", "High", "Medium", "Low")
ASD_CODES = ("C1", "C2", "C3", "C4", "C5", "C6")
#: The words the classifier's own prompt gives each category (scripts/asd_risk_classifier.py,
#: docs/07-ASD-risk-classification.md), so a chart row is not a bare code.
ASD_WORDS = {"C1": "most severe", "C2": "very severe", "C3": "severe", "C4": "moderate", "C5": "low",
             "C6": "least severe"}
ASD_LABELS = {code: f"{code} ({ASD_WORDS[code]})" for code in ASD_CODES}
ASD_ORDER = tuple(ASD_LABELS[code] for code in ASD_CODES)
AI_ORDER = ("No-AI", "AI-attacker", "AI-vulnerability", "AI-misuse", "AI-uncertain")
#: Entity types that name no sector.
GENERIC_TYPES = {"organization", "organisation"}
INSUFFICIENT = "Insufficient information"


class ComparisonError(RuntimeError):
    """The comparison cannot be built from this database."""


@dataclass(frozen=True)
class Attribute:
    """One categorical attribute compared between the groups."""

    key: str
    label: str
    ordered: bool                      # categories have an order: sparse ones are pooled with a neighbour
    order: Optional[Tuple[str, ...]]   # the fixed category order (None: by size, largest first)
    source: str                        # where the value comes from
    noun: str = "events"               # what the percentages are of


ATTRIBUTES: Tuple[Attribute, ...] = (
    Attribute("size", "Victim size band", True, SIZE_ORDER,
              "EntitiesV2.size_estimate of the event's victim organisation, reconciled across the "
              "organisation's name variants as the recurrence analysis does (reconcile_variant_sizes)"),
    Attribute("industry", "Victim industry (entity type)", False, None,
              "EntitiesV2.entity_type of the event's victim organisation (its representative name variant)"),
    Attribute("severity", "Severity", True, SEVERITY_ORDER, "DeduplicatedEvents.severity"),
    Attribute("attack_type", "Attack type", False, None, "DeduplicatedEvents.event_type"),
    Attribute("asd_severity", "ASD severity category", True, ASD_ORDER,
              "ASDRiskClassifications.severity_category (the event's latest classification); the words beside "
              "each code are the classifier's own definitions, C1 the most severe and C6 the least"),
    Attribute("asd_impact", "ASD impact type", False, None,
              "ASDRiskClassifications.impact_type (the event's latest classification)"),
    Attribute("ai", "AI involvement", False, AI_ORDER, "DeduplicatedEvents.ai_involvement"),
    Attribute("half_year", "Half-year of the event", True, None,
              "DeduplicatedEvents.event_date, as a calendar half-year"),
)
RECORDS_KEY = "records"
RECORDS_LABEL = "Records affected"


# --- statistics ------------------------------------------------------------------------------------------

def expected_counts(supplier: Sequence[float], other: Sequence[float]) -> np.ndarray:
    """Expected counts of the 2 x k table under homogeneity (row total x column total / n)."""
    table = np.array([supplier, other], dtype=float)
    total = table.sum()
    if total <= 0:
        raise ValueError("an empty table has no expected counts")
    return np.outer(table.sum(axis=1), table.sum(axis=0)) / total


def sparse_share(expected: np.ndarray) -> float:
    """The share of cells whose expected count is under :data:`MIN_EXPECTED`."""
    return float((expected < MIN_EXPECTED).sum()) / expected.size


def is_sparse(expected: np.ndarray) -> bool:
    """The expected-count rule: over 20% of cells under 5, or any cell under 1."""
    return sparse_share(expected) > MAX_SPARSE_SHARE or bool((expected < MIN_ANY_EXPECTED).any())


def pool_sparse(labels: Sequence[str], supplier: Sequence[int], other: Sequence[int],
                ordered: bool) -> Tuple[List[List[str]], List[int], List[int]]:
    """Pool sparse categories until the expected-count rule holds or two categories remain.

    While the rule fails (:func:`is_sparse`) and more than two categories remain, the category with the
    smallest total is pooled: for an *ordered* attribute with the neighbour that has the smaller total
    (the lower one on a tie), so pooled categories stay adjacent; for an unordered one into a single
    pooled group, started by joining the two smallest categories. Ties in the total go to the category
    listed first.

    Returns ``(groups, supplier, other)``: each group is the list of original labels it holds.
    """
    groups = [[label] for label in labels]
    a, b = [int(x) for x in supplier], [int(x) for x in other]
    pooled: Optional[int] = None          # index of the pooled group of an unordered attribute
    while len(groups) > 2 and is_sparse(expected_counts(a, b)):
        totals = [x + y for x, y in zip(a, b)]
        candidates = [i for i in range(len(groups)) if i != pooled]
        smallest = min(candidates, key=lambda i: (totals[i], i))
        if ordered:
            neighbours = [i for i in (smallest - 1, smallest + 1) if 0 <= i < len(groups)]
            target = min(neighbours, key=lambda i: (totals[i], i))
            low, high = sorted((smallest, target))
            groups[low] = groups[low] + groups[high]
        elif pooled is None:
            rest = [i for i in candidates if i != smallest]
            target = min(rest, key=lambda i: (totals[i], i))
            low, high = sorted((smallest, target))
            groups[low] = groups[low] + groups[high]
            pooled = low
        else:
            low, high = sorted((smallest, pooled))
            groups[low] = groups[low] + groups[high]
            pooled = low
        a[low] += a[high]
        b[low] += b[high]
        del groups[high], a[high], b[high]
    return groups, a, b


def group_label(group: Sequence[str], ordered: bool) -> str:
    """The label of a (possibly pooled) category."""
    if len(group) == 1:
        return group[0]
    return f"{group[0]} to {group[-1]}" if ordered else POOLED_LABEL


def holm(pvalues: Sequence[float]) -> List[float]:
    """Holm's step-down adjusted p-values, in the order given."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, index in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvalues[index]))
        adjusted[index] = running
    return adjusted


def effect_word(v: float) -> str:
    return next(word for cut, word in EFFECT_WORDS if v >= cut)


@dataclass
class TestResult:
    """One attribute's test. ``ran`` is False, with ``reason``, when it could not be run."""

    key: str
    label: str
    kind: str                                   # "categorical" or "ranks"
    ran: bool
    reason: str = ""
    method: str = ""
    statistic_name: str = ""
    statistic: Optional[float] = None
    dof: Optional[int] = None
    p: Optional[float] = None
    holm_p: Optional[float] = None
    effect_name: str = ""
    effect: Optional[float] = None
    n_supplier: int = 0
    n_other: int = 0
    # categorical
    tested_labels: List[str] = field(default_factory=list)
    tested_supplier: List[int] = field(default_factory=list)
    tested_other: List[int] = field(default_factory=list)
    pooled: List[List[str]] = field(default_factory=list)     # the groups of more than one category
    sparse_before: Optional[float] = None
    sparse_after: Optional[float] = None
    min_expected: Optional[float] = None
    residuals: Dict[str, float] = field(default_factory=dict)  # supplier row, adjusted standardised
    exact: bool = False                          # Fisher's exact test: an exact p-value and no test statistic
    # ranks
    superiority: Optional[float] = None         # P(supplier > non-supplier) + half the ties; says which way

    @property
    def significant(self) -> bool:
        return bool(self.ran and self.holm_p is not None and self.holm_p < ALPHA)

    @property
    def size_word(self) -> str:
        """negligible / small / medium / large, from the effect size (only meaningful when the test ran)."""
        return effect_word(self.effect) if self.ran and self.effect is not None else ""


def categorical_test(key: str, label: str, labels: Sequence[str], supplier: Sequence[int],
                     other: Sequence[int], ordered: bool) -> TestResult:
    """Chi-squared test of homogeneity of two groups over ``labels``, pooled or exact where sparse.

    Categories empty in both groups are dropped. The test is not run (``ran`` False, with the reason)
    when a group has no known value or fewer than two categories are observed.
    """
    keep = [i for i in range(len(labels)) if supplier[i] + other[i] > 0]
    labels = [labels[i] for i in keep]
    a, b = [int(supplier[i]) for i in keep], [int(other[i]) for i in keep]
    result = TestResult(key, label, "categorical", False, n_supplier=sum(a), n_other=sum(b))
    if not sum(a) or not sum(b):
        result.reason = "a group has no event with a known value"
        return result
    if len(labels) < 2:
        result.reason = "fewer than two categories are observed"
        return result

    result.sparse_before = sparse_share(expected_counts(a, b))
    groups, a, b = pool_sparse(labels, a, b, ordered)
    expected = expected_counts(a, b)
    observed = np.array([a, b], dtype=float)
    n = observed.sum()
    chi2 = float(((observed - expected) ** 2 / expected).sum())
    result.ran = True
    result.pooled = [g for g in groups if len(g) > 1]
    result.tested_labels = [group_label(g, ordered) for g in groups]
    result.tested_supplier, result.tested_other = a, b
    result.sparse_after = sparse_share(expected)
    result.min_expected = float(expected.min())
    result.effect_name = "Cramér's V"
    result.effect = math.sqrt(chi2 / n)            # two groups: min(rows, columns) - 1 = 1
    row_share, col_share = observed.sum(axis=1) / n, observed.sum(axis=0) / n
    adjusted = (observed - expected) / np.sqrt(expected * np.outer(1 - row_share, 1 - col_share))
    result.residuals = {name: float(z) for name, z in zip(result.tested_labels, adjusted[0])}
    if len(groups) == 2 and is_sparse(expected):
        # An exact test has no test statistic and no degrees of freedom: the row reports neither, and its
        # effect size is the phi coefficient computed from the four counts, not Cramér's V defined through
        # Pearson's chi-squared (owner, 2026-11-11: the row named Fisher's test beside a chi-squared-based
        # number). |phi| equals sqrt(chi-squared / n) for a 2 x 2 table, so the size words are unchanged.
        result.method, result.exact = "Fisher's exact test (2 x 2)", True
        result.p = float(stats.fisher_exact(observed, alternative="two-sided")[1])
        result.effect_name, result.effect = PHI_NAME, phi_coefficient(observed)
    else:
        result.method = "Chi-squared test of homogeneity"
        result.statistic_name, result.statistic, result.dof = "chi-squared", chi2, len(groups) - 1
        result.p = float(stats.chi2.sf(chi2, result.dof))
    return result


def rank_test(key: str, label: str, supplier: Sequence[float], other: Sequence[float]) -> TestResult:
    """Two-sided Mann-Whitney U test; the effect size is the absolute rank-biserial correlation."""
    result = TestResult(key, label, "ranks", False, n_supplier=len(supplier), n_other=len(other))
    if not len(supplier) or not len(other):
        result.reason = "a group has no event with a known value"
        return result
    if len(set(supplier) | set(other)) < 2:
        result.reason = "every known value is the same"
        return result
    u, p = stats.mannwhitneyu(supplier, other, alternative="two-sided")
    result.ran = True
    result.method = "Mann-Whitney U test (two-sided)"
    result.statistic_name, result.statistic, result.p = "U", float(u), float(p)
    result.superiority = float(u) / (len(supplier) * len(other))
    # The size of the effect without its direction: 0 when neither group tends to be larger, 1 when one always is.
    result.effect_name, result.effect = "rank-biserial |r|", abs(2 * result.superiority - 1)
    return result


def adjust(results: Sequence[TestResult]) -> None:
    """Set ``holm_p`` on every test that ran (the family is those tests)."""
    ran = [r for r in results if r.ran]
    for r, adjusted in zip(ran, holm([r.p for r in ran])):
        r.holm_p = adjusted


def quartiles(values: Sequence[float]) -> Tuple[float, float, float]:
    q1, median, q3 = np.percentile(np.asarray(values, dtype=float), [25, 50, 75])
    return float(q1), float(median), float(q3)


# --- formatting ------------------------------------------------------------------------------------------

def fmt_p(p: Optional[float]) -> str:
    if p is None:
        return "-"
    return f"{p:.3f}" if p >= 0.001 else f"{p:.1e}"


def fmt_pct(part: float, whole: float) -> str:
    return f"{100.0 * part / whole:.1f}%" if whole else "-"


def fmt_count(value: float) -> str:
    return f"{int(round(value)):,}"


def fmt_short(value: float) -> str:
    """1,234,567 -> 1.2M (axis ticks)."""
    for cut, suffix in ((1e9, "bn"), (1e6, "M"), (1e3, "k")):
        if value >= cut:
            scaled = value / cut
            return f"{scaled:.0f}{suffix}" if scaled >= 10 or scaled == int(scaled) else f"{scaled:.1f}{suffix}"
    return f"{value:.0f}"


def phi_coefficient(table: "np.ndarray") -> float:
    """|phi| of a 2 x 2 table from its counts: |ad - bc| / sqrt of the product of the four margins."""
    (a, b), (c, d) = table
    margins = (a + b) * (c + d) * (a + c) * (b + d)
    return float(abs(a * d - b * c) / math.sqrt(margins)) if margins else 0.0


def fmt_statistic(r: TestResult) -> str:
    if not r.ran:
        return "-"
    if r.exact:
        return "none (exact test)"
    if r.statistic_name == "U":
        return f"U = {r.statistic:,.0f}"
    return f"chi-squared = {r.statistic:.2f}"


def fmt_effect(r: TestResult) -> str:
    if not r.ran or r.effect is None:
        return "-"
    return f"{r.effect_name} = {r.effect:.3f} ({r.size_word})"


def verdict(r: TestResult) -> str:
    """The conclusion label of a test.

    A significant difference always carries the size of its effect ("small difference"): statistical
    significance says a difference exists, not that it matters, so a bare "difference" would mislead.
    """
    if not r.ran:
        return "not tested: " + r.reason
    if not r.significant:
        return "no evidence of a difference"
    return f"{r.size_word} difference"


def verdict_phrase(r: TestResult) -> str:
    """The verdict inside a sentence: ``a small difference`` / ``no evidence of a difference``."""
    return f"a {verdict(r)}" if r.significant else verdict(r)


def plural(n: int, noun: str) -> str:
    """``3 events`` / ``1 event``."""
    return f"{n:,} {noun}" if n == 1 else f"{n:,} {noun}s"


def join_words(items: Sequence[str]) -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


# --- generated sentences ---------------------------------------------------------------------------------

def _evidence(r: TestResult) -> str:
    if r.exact:
        return f"{r.method}: exact p = {fmt_p(r.p)}, Holm-adjusted p = {fmt_p(r.holm_p)}"
    dof = f", {r.dof} df" if r.dof is not None else ""
    return f"{r.method}: {fmt_statistic(r)}{dof}, p = {fmt_p(r.p)}, Holm-adjusted p = {fmt_p(r.holm_p)}"


def conclusion(r: TestResult, family: int) -> str:
    """The one-sentence conclusion of a test, generated from its numbers.

    A result that is not significant after Holm adjustment says "no evidence of a difference" and gives
    no direction, whatever the raw p-value. A significant one opens with the size of the difference
    (:func:`verdict`), then says where it lies.
    """
    if not r.ran:
        return f"{r.label}: not tested ({r.reason})."
    if not r.significant:
        text = (f"{r.label}: no evidence of a difference between supplier and non-supplier events "
                f"({_evidence(r)}).")
        if r.p is not None and r.p < ALPHA:
            text += (f" The unadjusted p-value is under {ALPHA}, but it does not hold after adjustment for the "
                     f"{family} tests on this page.")
        return text
    if r.kind == "ranks":
        direction = "more" if r.superiority > 0.5 else "fewer"
        return (f"{r.label}: {verdict_phrase(r)} between supplier and non-supplier events ({_evidence(r)}; "
                f"{r.effect_name} = {r.effect:.3f}). Supplier events tend to have {direction} records affected: "
                f"a supplier event has the larger count in {100 * r.superiority:.0f}% of supplier / non-supplier "
                "pairs, ties counted half.")
    n_s, n_o = sum(r.tested_supplier), sum(r.tested_other)

    def describe(index: int) -> str:
        return (f"{r.tested_labels[index]} ({fmt_pct(r.tested_supplier[index], n_s)} of supplier events against "
                f"{fmt_pct(r.tested_other[index], n_o)})")

    ranked = sorted(range(len(r.tested_labels)), key=lambda i: -abs(r.residuals[r.tested_labels[i]]))
    more = [describe(i) for i in ranked if r.residuals[r.tested_labels[i]] > RESIDUAL_CUT]
    less = [describe(i) for i in ranked if r.residuals[r.tested_labels[i]] < -RESIDUAL_CUT]
    text = (f"{r.label}: {verdict_phrase(r)} between supplier and non-supplier events ({_evidence(r)}; "
            f"{r.effect_name} = {r.effect:.3f}).")
    if more:
        text += f" More common among supplier events: {join_words(more)}."
    if less:
        text += f" Less common among supplier events: {join_words(less)}."
    if not more and not less:
        text += (f" No single category stands out (no adjusted residual beyond {RESIDUAL_CUT}).")
    return text


def pooling_note(r: TestResult) -> str:
    """What the expected-count rule did to this test, generated from its numbers."""
    if not r.ran or r.kind != "categorical":
        return ""
    before = f"{100 * r.sparse_before:.0f}% of expected counts were under {MIN_EXPECTED:g}"
    if not r.pooled and not r.exact:
        return (f"Expected counts: {before} (smallest {r.min_expected:.1f}); no category was pooled.")
    parts = []
    if r.pooled:
        pooled = "; ".join(f"{join_words(g)}" for g in r.pooled)
        parts.append(f"Expected counts: {before}, so sparse categories were pooled for the test: {pooled}. "
                     f"After pooling {100 * r.sparse_after:.0f}% are under {MIN_EXPECTED:g} "
                     f"(smallest {r.min_expected:.1f}).")
    else:
        parts.append(f"Expected counts: {before} (smallest {r.min_expected:.1f}).")
    if r.exact:
        parts.append("The 2 x 2 table still fails the expected-count rule, so it is tested with Fisher's exact "
                     "test, which gives an exact p-value and has no test statistic.")
    return " ".join(parts)


# --- data ------------------------------------------------------------------------------------------------

def _tables(conn: sqlite3.Connection) -> Set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def supplier_bases(conn: sqlite3.Connection) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
    """Why each Active event is a supplier event, and the supplier incidents it belongs to.

    Returns ``(bases, incidents)`` keyed by event id: ``bases`` holds keys of :data:`BASES`;
    ``incidents`` the ids of the supplier incidents linking the event (as a customer's event or as the
    supplier's own). A link to a Merged event counts for the Active event that holds its records now
    (``supplier_links.resolve_event``); a link to a Rejected event counts for none.
    """
    missing = {"SupplierIncidents", "SupplierIncidentLinks", "DeduplicatedEventEntities"} - _tables(conn)
    if missing:
        raise ComparisonError("the database has no " + ", ".join(sorted(missing))
                              + ": supplier events cannot be identified")
    from cyber_data_collector.dedup.supplier_links import resolve_event

    bases: Dict[str, Set[str]] = {}
    incidents: Dict[str, Set[str]] = {}
    resolved: Dict[str, Optional[str]] = {}

    def now(event_id: str) -> Optional[str]:
        if event_id not in resolved:
            resolved[event_id] = resolve_event(conn, event_id)
        return resolved[event_id]

    def add(event_id: Optional[str], basis: str, incident: Optional[str] = None) -> None:
        if not event_id:
            return
        bases.setdefault(event_id, set()).add(basis)
        if incident:
            incidents.setdefault(event_id, set()).add(incident)

    for incident, event_id in conn.execute(
            "SELECT supplier_incident_id, customer_event_id FROM SupplierIncidentLinks "
            "WHERE customer_event_id IS NOT NULL ORDER BY supplier_incident_id, customer_event_id").fetchall():
        add(now(event_id), "customer_event", incident)
    for incident, event_id in conn.execute(
            "SELECT supplier_incident_id, supplier_event_id FROM SupplierIncidents "
            "WHERE supplier_event_id IS NOT NULL ORDER BY supplier_incident_id").fetchall():
        add(now(event_id), "supplier_event", incident)
    for (event_id,) in conn.execute(
            "SELECT deduplicated_event_id FROM DeduplicatedEvents WHERE status = 'Active' "
            "AND vendor_organization_name IS NOT NULL AND TRIM(vendor_organization_name) != ''"):
        add(event_id, "vendor")
    for event_id, role in conn.execute(
            "SELECT dee.deduplicated_event_id, dee.relationship_type FROM DeduplicatedEventEntities dee "
            "JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id "
            "WHERE d.status = 'Active' AND dee.relationship_type IN ('vendor', 'service_provider')"):
        add(event_id, role)
    return bases, incidents


def victim_organisations(conn: sqlite3.Connection) -> Dict[str, List[Dict[str, Any]]]:
    """Each Active event's victim organisations: ``{"entity_id", "name", "size", "entity_type"}``.

    Victims are the ``victim`` roles, counted once per organisation as the recurrence analysis counts
    them (``analyze_recurrent_timing.one_organisation_per_event``: name variants joined by
    ``victim_counting.organisation_groups``, two names of one organisation on one event counted once, the
    size band reconciled across the organisation's variants). Every Active event is read, whatever its
    date, so name variants are joined as they are there.
    """
    import pandas as pd

    from scripts.analyze_recurrent_timing import normalise_size_label, one_organisation_per_event

    where, params = incident_filter(None, "d")
    frame = pd.read_sql_query(
        f"""SELECT v.entity_id, v.entity_name, d.deduplicated_event_id
            FROM DeduplicatedEventEntities dee
            JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
            JOIN DeduplicatedEvents d ON d.deduplicated_event_id = dee.deduplicated_event_id
            WHERE dee.relationship_type = 'victim' AND {where}
              AND v.entity_name IS NOT NULL AND TRIM(v.entity_name) != ''
            ORDER BY v.entity_name COLLATE NOCASE, d.event_date, d.title""", conn, params=params)
    if frame.empty:
        return {}
    frame = one_organisation_per_event(conn, frame)
    ids = sorted({int(e) for e in frame["entity_id"]})
    types: Dict[int, Optional[str]] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for entity_id, entity_type in conn.execute(
                f"SELECT entity_id, entity_type FROM EntitiesV2 WHERE entity_id IN ({','.join('?' * len(chunk))})",
                chunk):
            types[int(entity_id)] = entity_type
    out: Dict[str, List[Dict[str, Any]]] = {}
    for row in frame.to_dict(orient="records"):
        entity_id = int(row["entity_id"])
        out.setdefault(str(row["deduplicated_event_id"]), []).append({
            "entity_id": entity_id, "name": row.get("entity_name"),
            "size": normalise_size_label(row.get("size_estimate")), "entity_type": types.get(entity_id)})
    return out


def _asd(conn: sqlite3.Connection) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    """Each Active event's latest ASD classification: (severity category, impact type)."""
    if "ASDRiskClassifications" not in _tables(conn):
        raise ComparisonError("the database has no ASDRiskClassifications table")
    order = "arc.updated_at, arc.rowid" if "updated_at" in _columns(conn, "ASDRiskClassifications") else "arc.rowid"
    out: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    for event_id, severity, impact in conn.execute(
            "SELECT arc.deduplicated_event_id, arc.severity_category, arc.impact_type "
            "FROM ASDRiskClassifications arc JOIN DeduplicatedEvents de "
            "ON arc.deduplicated_event_id = de.deduplicated_event_id "
            f"WHERE de.status = 'Active' ORDER BY {order}"):
        out[event_id] = (severity, impact)
    return out


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def half_year_label(day: str) -> str:
    return f"{day[:4]} H{1 if int(day[5:7]) <= 6 else 2}"


def size_value(victims: Sequence[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    """(band, None) when every victim organisation of the event has the same known band, else (None, why)."""
    if not victims:
        return None, "No victim organisation recorded"
    bands = {v["size"] for v in victims}
    if len(bands) > 1:
        return None, "Several victims of different size bands"
    band = next(iter(bands))
    return (band, None) if band in SIZE_ORDER else (None, "UNKNOWN size band")


def industry_value(victims: Sequence[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    """(entity type, None) when every victim organisation of the event has the same sector type."""
    if not victims:
        return None, "No victim organisation recorded"
    types = {_clean(v["entity_type"]) for v in victims}
    if len(types) > 1:
        return None, "Several victims of different entity types"
    entity_type = next(iter(types))
    if entity_type is None:
        return None, "Entity type not recorded"
    if entity_type.lower() in GENERIC_TYPES:
        return None, "Generic type (Organization), no sector"
    return entity_type, None


def _listed(value: Optional[str], allowed: Sequence[str], missing: str) -> Tuple[Optional[str], Optional[str]]:
    """(category, None) for a value in ``allowed`` (case-insensitive), else (None, why it is left out)."""
    if value is None or value.lower() == "unknown":
        return None, missing
    for name in allowed:
        if value.lower() == name.lower():
            return name, None
    return None, value if value == INSUFFICIENT else f"Unrecognised value ({value})"


def asd_category(value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """(``C3 (severe)``, None) for a stored ``C3``; (None, why) for anything that is not C1-C6."""
    code, why = _listed(value, ASD_CODES, "Not classified")
    return (ASD_LABELS[code], None) if code else (None, why)


def load_events(conn: sqlite3.Connection, period: ReportingPeriod) -> List[Dict[str, Any]]:
    """One row per Active event dated in ``period``, with its group and every compared attribute.

    Each attribute is ``row["values"][key] = (category, None)`` or ``(None, why it is left out)``.
    ``row["records"]`` is the records-affected count (None when not recorded or under 1, which is no
    count); ``row["bases"]`` why it is a supplier event (empty: a non-supplier event);
    ``row["incidents"]`` the supplier incidents linking it.
    """
    bases, incidents = supplier_bases(conn)
    victims = victim_organisations(conn)
    asd = _asd(conn)
    where, params = incident_filter(period, "d")
    out: List[Dict[str, Any]] = []
    for event_id, event_date, severity, event_type, records, ai in conn.execute(
            "SELECT d.deduplicated_event_id, d.event_date, d.severity, d.event_type, d.records_affected, "
            f"d.ai_involvement FROM DeduplicatedEvents d WHERE {where} ORDER BY d.event_date, "
            "d.deduplicated_event_id", params):
        day = str(event_date)[:10]
        asd_severity, asd_impact = asd.get(event_id, (None, None))
        asd_severity, asd_impact = _clean(asd_severity), _clean(asd_impact)
        try:
            count = float(records) if records is not None else None
        except (TypeError, ValueError):
            count = None
        values = {
            "size": size_value(victims.get(event_id, [])),
            "industry": industry_value(victims.get(event_id, [])),
            "severity": _listed(_clean(severity), SEVERITY_ORDER, "Unknown or not recorded"),
            "attack_type": (_clean(event_type), None) if _clean(event_type) else (None, "Not recorded"),
            "asd_severity": (asd_category(asd_severity) if event_id in asd else (None, "Not classified")),
            "asd_impact": ((None, "Not classified") if event_id not in asd or asd_impact is None
                           else (None, INSUFFICIENT) if asd_impact == INSUFFICIENT else (asd_impact, None)),
            "ai": _listed(_clean(ai), AI_ORDER, "Not judged"),
            "half_year": (half_year_label(day), None),
        }
        out.append({
            "event_id": event_id, "event_date": day, "values": values,
            "records": count if count is not None and count >= 1 else None,
            "bases": sorted(bases.get(event_id, ())), "incidents": sorted(incidents.get(event_id, ())),
            "victims": len(victims.get(event_id, [])),
        })
    return out


def is_supplier(event: Dict[str, Any]) -> bool:
    """The group definition: any basis in :data:`BASES` makes the event a supplier event."""
    return bool(event["bases"])


def one_event_per_incident(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The events with one kept per supplier incident (the sensitivity analysis).

    Events linked by a supplier incident, directly or through a chain of incidents sharing an event,
    form one cluster; the earliest-dated event of each cluster is kept (ties: the lowest event id).
    Events in no supplier incident are all kept.
    """
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for event in events:
        for incident in event["incidents"]:
            parent[find("e:" + event["event_id"])] = find("i:" + incident)
    first: Dict[str, Tuple[str, str]] = {}
    for event in events:
        if event["incidents"]:
            root = find("e:" + event["event_id"])
            key = (event["event_date"], event["event_id"])
            if root not in first or key < first[root]:
                first[root] = key
    kept = {event_id for _, event_id in first.values()}
    return [e for e in events if not e["incidents"] or e["event_id"] in kept]


# --- tabulation and analysis -----------------------------------------------------------------------------

@dataclass
class Tabulation:
    """An attribute's counts by group: the categories in the test, and what is left out of it."""

    attribute: Attribute
    labels: List[str]
    supplier: List[int]
    other: List[int]
    left_out: List[Tuple[str, int, int]]     # (why, supplier events, non-supplier events)
    group_sizes: Tuple[int, int]             # all events of each group


def tabulate(events: Sequence[Dict[str, Any]], attribute: Attribute) -> Tabulation:
    counts: Dict[str, List[int]] = {}
    missing: Dict[str, List[int]] = {}
    sizes = [0, 0]
    for event in events:
        column = 0 if is_supplier(event) else 1
        sizes[column] += 1
        value, why = event["values"][attribute.key]
        target = counts if value is not None else missing
        target.setdefault(value if value is not None else why, [0, 0])[column] += 1
    if attribute.order is not None:
        labels = [name for name in attribute.order if name in counts]
        labels += sorted(name for name in counts if name not in attribute.order)
    elif attribute.ordered:
        labels = sorted(counts)
    else:
        labels = sorted(counts, key=lambda name: (-sum(counts[name]), name))
    left_out = [(why, n[0], n[1]) for why, n in sorted(missing.items(), key=lambda kv: (-sum(kv[1]), kv[0]))]
    return Tabulation(attribute, labels, [counts[name][0] for name in labels],
                      [counts[name][1] for name in labels], left_out, (sizes[0], sizes[1]))


@dataclass
class RecordsSummary:
    group: str
    events: int
    known: int
    values: List[float]

    @property
    def missing(self) -> int:
        return self.events - self.known


def records_summaries(events: Sequence[Dict[str, Any]]) -> Tuple[RecordsSummary, RecordsSummary]:
    out = []
    for name, wanted in ((SUPPLIER, True), (OTHER, False)):
        group = [e for e in events if is_supplier(e) == wanted]
        values = sorted(e["records"] for e in group if e["records"] is not None)
        out.append(RecordsSummary(name, len(group), len(values), values))
    return out[0], out[1]


def run_tests(events: Sequence[Dict[str, Any]]) -> Tuple[List[Tabulation], List[TestResult]]:
    """Every attribute's tabulation and test, Holm-adjusted, in page order (records affected third)."""
    tabulations = [tabulate(events, attribute) for attribute in ATTRIBUTES]
    results = [categorical_test(t.attribute.key, t.attribute.label, t.labels, t.supplier, t.other,
                                t.attribute.ordered) for t in tabulations]
    supplier, other = records_summaries(events)
    results.insert(2, rank_test(RECORDS_KEY, RECORDS_LABEL, supplier.values, other.values))
    adjust(results)
    return tabulations, results


@dataclass
class Comparison:
    period: ReportingPeriod
    events: List[Dict[str, Any]]
    tabulations: List[Tabulation]
    results: List[TestResult]
    records: Tuple[RecordsSummary, RecordsSummary]
    sensitivity_events: List[Dict[str, Any]]
    sensitivity: List[TestResult]

    @property
    def supplier_events(self) -> List[Dict[str, Any]]:
        return [e for e in self.events if is_supplier(e)]

    @property
    def family(self) -> int:
        return sum(1 for r in self.results if r.ran)


def analyse(events: Sequence[Dict[str, Any]], period: ReportingPeriod) -> Comparison:
    events = list(events)
    tabulations, results = run_tests(events)
    reduced = one_event_per_incident(events)
    _, sensitivity = run_tests(reduced)
    return Comparison(period, events, tabulations, results, records_summaries(events), reduced, sensitivity)


def collect(conn: sqlite3.Connection, period: Optional[ReportingPeriod] = None) -> Comparison:
    """Load the events of ``period`` (default: the dashboards' window, 2020 to date) and run every test."""
    period = period or dashboard_period()
    return analyse(load_events(conn, period), period)


# --- generated summary -----------------------------------------------------------------------------------

def group_sentence(c: Comparison) -> str:
    n_s = len(c.supplier_events)
    total = len(c.events)
    return (f"Of {total:,} Active events dated {c.period.start_iso} to {c.period.end_iso}, {n_s:,} "
            f"({fmt_pct(n_s, total)}) are supplier events and {total - n_s:,} ({fmt_pct(total - n_s, total)}) "
            "are non-supplier events.")


def headline_sentences(c: Comparison) -> List[str]:
    """The summary's opening sentences: the groups, then which attributes differ and which do not."""
    out = [group_sentence(c)]
    ran = [r for r in c.results if r.ran]
    differ = [r for r in ran if r.significant]
    same = [r.label for r in ran if not r.significant]
    skipped = [r.label for r in c.results if not r.ran]
    if ran:
        if differ:
            sizes = []
            for _, word in EFFECT_WORDS:
                names = [r.label for r in differ if r.size_word == word]
                if names:
                    sizes.append(f"{word} for {join_words(names)}")
            out.append(f"After Holm adjustment across the {len(ran)} tests, {len(differ)} of "
                       f"{plural(len(ran), 'attribute')} {'shows' if len(differ) == 1 else 'show'} a statistically "
                       f"significant difference between the groups at the {ALPHA} level. Size of the difference: "
                       f"{'; '.join(sizes)}.")
        else:
            out.append(f"After Holm adjustment across the {len(ran)} tests, no attribute differs between the "
                       f"groups at the {ALPHA} level.")
        if same:
            out.append(f"No evidence of a difference: {join_words(same)}.")
    if skipped:
        out.append(f"Not tested: {join_words(skipped)}.")
    if len(c.sensitivity_events) != len(c.events):
        out.append(sensitivity_sentence(c) + " Events of one supplier incident share an attack, so a conclusion "
                   "that changes rests on events that are not independent.")
    return out


def cluster_sentences(c: Comparison) -> List[str]:
    """How far supplier events cluster in supplier incidents, and what the one-per-incident check shows."""
    linked = [e for e in c.supplier_events if e["incidents"]]
    dropped = len(c.events) - len(c.sensitivity_events)
    clusters = len(linked) - dropped
    out = [f"{len(linked):,} of the {len(c.supplier_events):,} supplier events are linked to a supplier incident, "
           f"in {plural(clusters, 'separate incident')} (incidents that share an event counted as one). Events "
           "of one "
           "incident share an attack, so they are not independent observations and the tests above can "
           "overstate the evidence."]
    if not dropped:
        out.append("No incident holds more than one event, so the one-event-per-incident check uses the same "
                   "events.")
        return out
    out.append(f"With one event kept per incident (the earliest-dated; {plural(dropped, 'event')} left out, "
               f"{len([e for e in c.sensitivity_events if is_supplier(e)]):,} supplier events remain) every test "
               "was run again.")
    out.append(sensitivity_sentence(c))
    return out


def sensitivity_sentence(c: Comparison) -> str:
    """Which conclusions change with one event per supplier incident, generated from both sets of tests."""
    by_key = {r.key: r for r in c.sensitivity}
    changed, unchanged = [], 0
    for r in c.results:
        again = by_key[r.key]
        if not (r.ran and again.ran):
            continue
        if verdict(r) == verdict(again):
            unchanged += 1
        else:
            changed.append(f"{r.label} ({sensitivity_change(r, again)})")
    compared = unchanged + len(changed)
    lead = "With one event kept per supplier incident"
    if not compared:
        return f"{lead}, no test could be compared."
    if changed:
        return (f"{lead}, {len(changed)} of {plural(compared, 'conclusion')} "
                f"{'changes' if len(changed) == 1 else 'change'}: {'; '.join(changed)}.")
    return (f"{lead}, {'the one conclusion is' if compared == 1 else f'all {compared} conclusions are'} "
            "unchanged.")


def sensitivity_change(r: TestResult, again: TestResult) -> str:
    """How a conclusion differs between all events (``r``) and one event per supplier incident (``again``)."""
    def numbers(x: TestResult) -> str:
        effect = f", {x.effect_name} = {x.effect:.3f}" if x.significant else ""
        return f"Holm-adjusted p = {fmt_p(x.holm_p)}{effect}"

    return (f"{verdict_phrase(r)} on all events, {numbers(r)}; {verdict_phrase(again)} with one event per "
            f"incident, {numbers(again)}")


# --- HTML ------------------------------------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Supplier vs non-supplier events</title>
<style>
  :root {{ color-scheme: light dark; --bg: #f6f7f9; --card: #fff; --ink: #16181d; --muted: #667;
           --line: #dfe3e8; --warn: #8a5312; --warnbg: #fff3e0; --ok: #2f6b3d; --okbg: #e8f4ea;
           --supplier: #eb6834; --other: #2a78d6; --soft: #eef0f3; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #15171b; --card: #1d2026; --ink: #e6e8ec; --muted: #9aa1ab; --line: #2e333b;
             --warn: #f0c27a; --warnbg: #3a2c16; --ok: #9fd3a9; --okbg: #1f3324;
             --supplier: #d95926; --other: #3987e5; --soft: #262a31; }} }}
  * {{ box-sizing: border-box; }}
  body {{ font: 15px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 24px 16px;
         background: var(--bg); color: var(--ink); overflow-wrap: break-word; }}
  main {{ max-width: 1000px; margin: 0 auto; }}
  h1 {{ font-size: 21px; margin: 0 0 4px; }}
  h2 {{ font-size: 17px; margin: 0 0 6px; }}
  h3 {{ font-size: 14px; margin: 14px 0 4px; }}
  p {{ margin: 6px 0; }}
  .sub {{ color: var(--muted); font-size: 13px; margin-bottom: 18px; overflow-wrap: anywhere; }}
  .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 8px; margin-bottom: 14px;
           padding: 14px 16px; }}
  .m {{ font-size: 12.5px; color: var(--muted); }}
  .find {{ font-size: 14px; border-radius: 6px; padding: 7px 10px; margin: 8px 0; }}
  .find.diff {{ background: var(--warnbg); color: var(--warn); }}
  .find.same {{ background: var(--soft); color: var(--ink); }}
  .find.check {{ background: none; border: 1px dashed var(--line); color: var(--muted); font-size: 13px; }}
  ul.sum {{ margin: 6px 0; padding-left: 20px; }}
  ul.sum li {{ margin: 4px 0; }}
  .legend {{ display: flex; flex-wrap: wrap; gap: 4px 18px; font-size: 13px; margin: 8px 0 10px; }}
  .sw {{ display: inline-block; width: 12px; height: 12px; border-radius: 3px; margin-right: 6px;
         vertical-align: -1px; }}
  .sw.s, .bar.s, .box.s {{ background: var(--supplier); }}
  .sw.o, .bar.o, .box.o {{ background: var(--other); }}
  .bars {{ display: grid; grid-template-columns: minmax(84px, 28%) 1fr; gap: 9px 12px; align-items: center; }}
  .cat {{ font-size: 13px; overflow-wrap: anywhere; }}
  .line {{ display: flex; align-items: center; gap: 6px; height: 13px; margin: 2px 0; }}
  .bar {{ height: 9px; border-radius: 0 4px 4px 0; flex: 0 0 auto; min-width: 1px;
          width: calc((100% - 56px) * var(--f)); }}
  .val {{ font-size: 11.5px; color: var(--muted); white-space: nowrap; font-variant-numeric: tabular-nums; }}
  .out .bar {{ opacity: .45; }}
  .outhead {{ grid-column: 1 / -1; font-size: 12px; color: var(--muted); border-top: 1px dashed var(--line);
              padding-top: 8px; margin-top: 2px; }}
  .scroll {{ overflow-x: auto; margin-top: 10px; }}
  table {{ border-collapse: collapse; font-size: 13px; width: 100%; }}
  th, td {{ text-align: left; padding: 4px 10px 4px 0; border-bottom: 1px solid var(--line);
            vertical-align: top; }}
  th {{ font-size: 11.5px; text-transform: uppercase; letter-spacing: .03em; color: var(--muted);
        font-weight: 600; white-space: nowrap; }}
  td.n, th.n {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  tr.outrow td {{ color: var(--muted); }}
  tr.sep td {{ font-size: 11.5px; text-transform: uppercase; letter-spacing: .03em; color: var(--muted);
               padding-top: 10px; }}
  .boxes {{ display: grid; grid-template-columns: minmax(84px, 22%) 1fr; gap: 10px 12px; align-items: center; }}
  .plot {{ position: relative; height: 34px; }}
  .grid {{ position: absolute; top: 0; bottom: 0; width: 1px; background: var(--line); }}
  .whisker {{ position: absolute; top: 16px; height: 2px; background: var(--muted); }}
  .cap {{ position: absolute; top: 10px; height: 14px; width: 2px; background: var(--muted); }}
  .box {{ position: absolute; top: 6px; height: 22px; border-radius: 3px; opacity: .85; }}
  .median {{ position: absolute; top: 4px; height: 26px; width: 3px; margin-left: -1px; background: var(--ink); }}
  .dot {{ position: absolute; top: 14px; width: 6px; height: 6px; margin-left: -3px; border-radius: 50%;
          border: 1px solid var(--muted); background: var(--card); }}
  .hist {{ display: flex; align-items: flex-end; height: 132px; border-bottom: 1px solid var(--muted);
           border-right: 1px solid var(--line); }}
  .hbin {{ flex: 1 1 0; min-width: 0; height: 100%; display: flex; align-items: flex-end; justify-content: center;
           gap: 2px; border-left: 1px solid var(--line); }}
  .hbar {{ position: relative; width: 36%; max-width: 30px; border-radius: 4px 4px 0 0; }}
  .hbar.s {{ background: var(--supplier); }}
  .hbar.o {{ background: var(--other); }}
  .hbar span {{ position: absolute; bottom: 100%; left: 50%; transform: translateX(-50%); font-size: 10.5px;
                color: var(--muted); white-space: nowrap; padding-bottom: 1px; }}
  .axis {{ position: relative; height: 20px; }}
  .tick {{ position: absolute; top: 2px; transform: translateX(-50%); font-size: 11px; color: var(--muted);
           white-space: nowrap; }}
  .tick.first {{ transform: none; }}
  .tick.last {{ transform: translateX(-100%); }}
  details {{ margin-top: 10px; }}
  summary {{ cursor: pointer; font-size: 13px; color: var(--muted); }}
  code {{ font-size: 12px; }}
  @media (max-width: 640px) {{
    body {{ padding: 16px 10px; }}
    .bars {{ grid-template-columns: 1fr; gap: 2px 0; }}
    .bars .cat {{ margin-top: 8px; font-weight: 600; }}
    .boxes {{ grid-template-columns: 1fr; gap: 2px 0; }}
    .tick.alt {{ display: none; }}
    .hbar span {{ display: none; }}
  }}
</style>
</head>
<body>
<main>
<h1>Supplier vs non-supplier events</h1>
<div class="sub">{intro}</div>
{body}
</main>
</body>
</html>
"""

_E = html.escape


def _legend(n_supplier: int, n_other: int, noun: str = "events with a known value") -> str:
    return (f'<div class="legend"><span><span class="sw s"></span>{_E(SUPPLIER)} ({n_supplier:,} {_E(noun)})</span>'
            f'<span><span class="sw o"></span>{_E(OTHER)} ({n_other:,} {_E(noun)})</span></div>')


def _bar_lines(n_s: int, n_o: int, whole_s: int, whole_o: int, scale: float, label: str) -> str:
    lines = []
    for css, group, n, whole in (("s", SUPPLIER, n_s, whole_s), ("o", OTHER, n_o, whole_o)):
        share = n / whole if whole else 0.0
        tip = f"{group}: {label} - {n:,} of {whole:,} ({fmt_pct(n, whole)})"
        lines.append(f'<div class="line" title="{_E(tip)}"><div class="bar {css}" '
                     f'style="--f:{(share / scale if scale else 0):.4f}"></div>'
                     f'<span class="val">{fmt_pct(n, whole)}</span></div>')
    return "".join(lines)


def bar_chart(t: Tabulation) -> str:
    """Side-by-side bars of each group's percentage distribution; what is left out of the test below them."""
    known_s, known_o = sum(t.supplier), sum(t.other)
    all_s, all_o = t.group_sizes
    shares = [n / known_s for n in t.supplier if known_s] + [n / known_o for n in t.other if known_o]
    shares += [n / all_s for _, n, _ in t.left_out if all_s] + [n / all_o for _, _, n in t.left_out if all_o]
    scale = max(shares, default=0.0)
    rows = []
    for label, n_s, n_o in zip(t.labels, t.supplier, t.other):
        rows.append(f'<div class="cat">{_E(label)}</div><div>'
                    f'{_bar_lines(n_s, n_o, known_s, known_o, scale, label)}</div>')
    if t.left_out:
        rows.append('<div class="outhead">Left out of the test (bars: share of all events in the group)</div>')
        for label, n_s, n_o in t.left_out:
            rows.append(f'<div class="cat out">{_E(label)}</div><div class="out">'
                        f'{_bar_lines(n_s, n_o, all_s, all_o, scale, label)}</div>')
    return _legend(known_s, known_o) + f'<div class="bars">{"".join(rows)}</div>'


def distribution_table(t: Tabulation, result: TestResult) -> str:
    known_s, known_o = sum(t.supplier), sum(t.other)
    all_s, all_o = t.group_sizes
    rows = []
    for label, n_s, n_o in zip(t.labels, t.supplier, t.other):
        rows.append(f"<tr><td>{_E(label)}</td><td class=n>{n_s:,}</td><td class=n>{fmt_pct(n_s, known_s)}</td>"
                    f"<td class=n>{n_o:,}</td><td class=n>{fmt_pct(n_o, known_o)}</td></tr>")
    rows.append(f"<tr><td><b>In the test</b></td><td class=n><b>{known_s:,}</b></td><td class=n>100.0%</td>"
                f"<td class=n><b>{known_o:,}</b></td><td class=n>100.0%</td></tr>" if known_s and known_o else
                f"<tr><td><b>In the test</b></td><td class=n><b>{known_s:,}</b></td><td class=n>-</td>"
                f"<td class=n><b>{known_o:,}</b></td><td class=n>-</td></tr>")
    if t.left_out:
        rows.append('<tr class="sep"><td colspan="5">Left out of the test (% of all events in the group)</td></tr>')
        for label, n_s, n_o in t.left_out:
            rows.append(f'<tr class="outrow"><td>{_E(label)}</td><td class=n>{n_s:,}</td>'
                        f"<td class=n>{fmt_pct(n_s, all_s)}</td><td class=n>{n_o:,}</td>"
                        f"<td class=n>{fmt_pct(n_o, all_o)}</td></tr>")
    head = ("<tr><th>Category</th><th class=n>Supplier</th><th class=n>%</th>"
            "<th class=n>Non-supplier</th><th class=n>%</th></tr>")
    return f'<div class="scroll"><table>{head}{"".join(rows)}</table></div>'


def _finding(r: TestResult, c: Comparison) -> str:
    css = "diff" if r.significant else "same"
    again = next(x for x in c.sensitivity if x.key == r.key)
    note = ""
    if r.ran and again.ran and verdict(r) != verdict(again):
        note = (f'<div class="find check">Check, one event per supplier incident: '
                f"{_E(sensitivity_change(r, again))}.</div>")
    return f'<div class="find {css}">{_E(conclusion(r, c.family))}</div>{note}'


def attribute_card(t: Tabulation, r: TestResult, c: Comparison) -> str:
    note = pooling_note(r)
    return (f'<div class="card" id="{_E(t.attribute.key)}"><h2>{_E(t.attribute.label)}</h2>'
            f'<div class="m">Source: {_E(t.attribute.source)}. Bars are percentages of the group\'s events '
            "with a known value.</div>"
            f"{_finding(r, c)}{bar_chart(t)}{distribution_table(t, r)}"
            + (f'<p class="m">{_E(note)}</p>' if note else "") + "</div>")


def _log_position(value: float, decades: int) -> float:
    return 100.0 * math.log10(max(value, 1.0)) / decades


def log_decades(supplier: RecordsSummary, other: RecordsSummary) -> int:
    """How many powers of ten the log axis spans: from 1 to the first power of ten at or above the largest count."""
    top = max([v for s in (supplier, other) for v in s.values], default=1.0)
    return max(1, math.ceil(math.log10(top))) if top > 1 else 1


def records_bins(values: Sequence[float], decades: int) -> List[int]:
    """Counts per power of ten: bin ``k`` holds counts from 10^k up to, not including, 10^(k+1).

    The last bin also takes a count equal to the top of the axis (10^decades), so every count is in a bin.
    """
    counts = [0] * decades
    for value in values:
        counts[sum(1 for k in range(1, decades) if value >= 10 ** k)] += 1
    return counts


def bin_label(k: int, decades: int, top: float) -> str:
    """``1,000 to 9,999`` (records are whole numbers); the last bin ends on the axis top when a count reaches it."""
    upper = 10 ** (k + 1) - (0 if k == decades - 1 and top >= 10 ** decades else 1)
    return f"{fmt_count(10 ** k)} to {fmt_count(upper)}"


def histogram_parts(supplier: RecordsSummary, other: RecordsSummary) -> Tuple[int, List[str], List[int], List[int]]:
    """(powers of ten on the axis, bin labels, supplier counts, non-supplier counts) of the records histogram."""
    decades = log_decades(supplier, other)
    top = max([v for s in (supplier, other) for v in s.values], default=1.0)
    labels = [bin_label(k, decades, top) for k in range(decades)]
    return decades, labels, records_bins(supplier.values, decades), records_bins(other.values, decades)


def records_histogram(supplier: RecordsSummary, other: RecordsSummary) -> str:
    """Side-by-side histogram of records affected, one bin per power of ten, as a share of each group.

    The bins are the intervals between the grid lines of the box plots' log axis, so the two line up. A bar's
    height is the share of the group's events with a count that fall in the bin (the bars of one group sum to
    100%); the count is in the tooltip and in :func:`histogram_table`.
    """
    decades, labels, a, b = histogram_parts(supplier, other)
    shares = [n / supplier.known for n in a if supplier.known] + [n / other.known for n in b if other.known]
    scale = max(shares, default=0.0)
    bins = []
    for label, n_s, n_o in zip(labels, a, b):
        bars = []
        for css, summary, n in (("s", supplier, n_s), ("o", other, n_o)):
            share = n / summary.known if summary.known else 0.0
            tip = f"{summary.group}: {label} records - {n:,} of {summary.known:,} ({fmt_pct(n, summary.known)})"
            value = f"<span>{100 * share:.0f}%</span>" if n else ""
            # The tallest bar takes 84% of the height, leaving room for its label.
            bars.append(f'<div class="hbar {css}" style="height:{(84.0 * share / scale if scale else 0):.2f}%" '
                        f'title="{_E(tip)}">{value}</div>')
        bins.append(f'<div class="hbin">{"".join(bars)}</div>')
    return f'<div class="hist">{"".join(bins)}</div>'


def histogram_table(supplier: RecordsSummary, other: RecordsSummary) -> str:
    """The histogram's numbers: events and share of the group's events with a count, per power of ten."""
    _, labels, a, b = histogram_parts(supplier, other)
    rows = [f"<tr><td>{_E(label)}</td><td class=n>{n_s:,}</td><td class=n>{fmt_pct(n_s, supplier.known)}</td>"
            f"<td class=n>{n_o:,}</td><td class=n>{fmt_pct(n_o, other.known)}</td></tr>"
            for label, n_s, n_o in zip(labels, a, b)]
    rows.append(f"<tr><td><b>With a count</b></td><td class=n><b>{supplier.known:,}</b></td>"
                f"<td class=n>{fmt_pct(supplier.known, supplier.known)}</td><td class=n><b>{other.known:,}</b></td>"
                f"<td class=n>{fmt_pct(other.known, other.known)}</td></tr>")
    head = ("<tr><th>Records affected</th><th class=n>Supplier</th><th class=n>%</th>"
            "<th class=n>Non-supplier</th><th class=n>%</th></tr>")
    return f'<div class="scroll"><table>{head}{"".join(rows)}</table></div>'


def box_plot(supplier: RecordsSummary, other: RecordsSummary) -> str:
    """Box plots of records affected on a log scale (Tukey whiskers computed on log10), with the histogram of
    the same counts under them on the same axis."""
    decades = log_decades(supplier, other)
    grid = "".join(f'<div class="grid" style="left:{100.0 * k / decades:.3f}%"></div>' for k in range(decades + 1))
    rows = []
    for css, summary in (("s", supplier), ("o", other)):
        rows.append(f'<div class="cat">{_E(summary.group)}<div class="m">{summary.known:,} with a count</div></div>')
        if not summary.values:
            rows.append(f'<div class="plot">{grid}</div>')
            continue
        logs = np.log10(np.asarray(summary.values, dtype=float))
        q1, median, q3 = np.percentile(logs, [25, 50, 75])
        reach = 1.5 * (q3 - q1)
        low = float(logs[logs >= q1 - reach].min())
        high = float(logs[logs <= q3 + reach].max())
        outliers = [float(x) for x in logs if x < low or x > high]

        def at(log_value: float) -> float:
            return 100.0 * log_value / decades

        tip = (f"{summary.group}: median {fmt_count(10 ** median)}, quartiles {fmt_count(10 ** q1)} to "
               f"{fmt_count(10 ** q3)}, whiskers {fmt_count(10 ** low)} to {fmt_count(10 ** high)}, "
               f"{len(outliers)} beyond the whiskers, {summary.known:,} events")
        dots = "".join(f'<span class="dot" style="left:{at(x):.3f}%" title="{_E(fmt_count(10 ** x))}"></span>'
                       for x in outliers)
        rows.append(
            f'<div class="plot" title="{_E(tip)}">{grid}'
            f'<div class="whisker" style="left:{at(low):.3f}%;width:{at(high) - at(low):.3f}%"></div>'
            f'<div class="cap" style="left:{at(low):.3f}%"></div><div class="cap" style="left:{at(high):.3f}%"></div>'
            f'<div class="box {css}" style="left:{at(q1):.3f}%;width:{max(at(q3) - at(q1), 0.3):.3f}%"></div>'
            f'<div class="median" style="left:{at(median):.3f}%"></div>{dots}</div>')
    ticks = []
    for k in range(decades + 1):
        classes = "tick" + (" first" if k == 0 else " last" if k == decades else "")
        if decades > 6 and (decades - k) % 2 == 1:      # hidden at phone width: every second tick, the last kept
            classes += " alt"
        ticks.append(f'<span class="{classes}" style="left:{100.0 * k / decades:.3f}%">{fmt_short(10 ** k)}</span>')
    rows.append('<div class="cat">Histogram<div class="m">share of the group\'s events with a count, per power '
                f'of ten</div></div>{records_histogram(supplier, other)}')
    rows.append(f'<div class="cat m">Records affected (log scale)</div><div class="axis">{"".join(ticks)}</div>')
    return f'<div class="boxes">{"".join(rows)}</div>'


def records_table(supplier: RecordsSummary, other: RecordsSummary) -> str:
    rows = []
    for summary in (supplier, other):
        if summary.values:
            q1, median, q3 = quartiles(summary.values)
            stats_cells = (f"<td class=n>{fmt_count(median)}</td><td class=n>{fmt_count(q1)} to {fmt_count(q3)}</td>"
                           f"<td class=n>{fmt_count(summary.values[0])} to {fmt_count(summary.values[-1])}</td>")
        else:
            stats_cells = "<td class=n>-</td><td class=n>-</td><td class=n>-</td>"
        rows.append(f"<tr><td>{_E(summary.group)}</td><td class=n>{summary.events:,}</td>"
                    f"<td class=n>{summary.known:,}</td><td class=n>{summary.missing:,} "
                    f"({fmt_pct(summary.missing, summary.events)})</td>{stats_cells}</tr>")
    head = ("<tr><th>Group</th><th class=n>Events</th><th class=n>With a count</th><th class=n>Missing</th>"
            "<th class=n>Median</th><th class=n>IQR</th><th class=n>Range</th></tr>")
    return f'<div class="scroll"><table>{head}{"".join(rows)}</table></div>'


def records_card(c: Comparison, r: TestResult) -> str:
    supplier, other = c.records
    return (f'<div class="card" id="{RECORDS_KEY}"><h2>{_E(RECORDS_LABEL)}</h2>'
            '<div class="m">Source: DeduplicatedEvents.records_affected. A count under 1 is no count. Each box '
            "spans the quartiles with a line at the median; whiskers reach the furthest count within 1.5 "
            "interquartile ranges of the box on the log scale; circles are counts beyond them. The histogram "
            "under the boxes, on the same axis, gives the share of each group's events with a count in each power "
            "of ten (the bars of one group sum to 100%; counts are in the tooltips and the second table).</div>"
            f"{_finding(r, c)}"
            + _legend(supplier.known, other.known, "events with a count")
            + box_plot(supplier, other) + records_table(supplier, other) +
            '<p class="m">Events with no count are left out of the test; the Missing column gives their number '
            "and share in each group.</p>" + histogram_table(supplier, other) +
            '<p class="m">The histogram and this table describe the distributions; the test is the Mann-Whitney '
            "U test above, on the counts themselves.</p></div>")


def groups_card(c: Comparison) -> str:
    supplier = c.supplier_events
    total = len(c.events)
    rows = []
    for key, text in BASES:
        n = sum(1 for e in supplier if key in e["bases"])
        only = sum(1 for e in supplier if e["bases"] == [key])
        rows.append(f"<tr><td>{_E(text)}</td><td class=n>{n:,}</td><td class=n>{only:,}</td></tr>")
    several = sum(1 for e in c.events if e["victims"] > 1)
    none = sum(1 for e in c.events if e["victims"] == 0)
    return (
        '<div class="card" id="groups"><h2>The two groups</h2>'
        "<p><b>Supplier event:</b> an Active event that is linked to a supplier incident, as a customer's event "
        "(<code>SupplierIncidentLinks</code>) or as the supplier's own event (<code>SupplierIncidents</code>), or "
        "that records a vendor (a <code>vendor</code> role or <code>vendor_organization_name</code>) or a service "
        "provider (a <code>service_provider</code> role). Every other Active event is a non-supplier event.</p>"
        f"<p>{_E(group_sentence(c))}</p>"
        '<div class="scroll"><table><tr><th>Why a supplier event</th><th class=n>Events</th>'
        f'<th class=n>For this reason only</th></tr>{"".join(rows)}'
        f"<tr><td><b>Supplier events</b> (an event can have several reasons)</td><td class=n><b>{len(supplier):,}"
        f"</b></td><td class=n></td></tr><tr><td><b>Non-supplier events</b></td><td class=n><b>"
        f"{total - len(supplier):,}</b></td><td class=n></td></tr></table></div>"
        "<p><b>Unit: the event</b>, not the victim organisation-event pair. The tests assume independent "
        "observations, and an event with several victims would enter once per victim with the same severity, "
        f"attack type, records count, ASD classification, AI category and date. {several:,} of the {total:,} "
        f"events record more than one victim organisation and {none:,} record none. A victim attribute (size "
        "band, entity type) is taken for an event when all its victim organisations share it; otherwise the "
        "event is left out of that attribute's test and counted under the chart.</p></div>")


def tests_table(results: Sequence[TestResult], family: int, with_conclusion: bool = True) -> str:
    rows = []
    for r in results:
        cells = (f"<td>{_E(r.label)}</td><td>{_E(r.method or 'not run')}</td><td class=n>{_E(fmt_statistic(r))}</td>"
                 f"<td class=n>{r.dof if r.dof is not None else '-'}</td><td class=n>{fmt_p(r.p)}</td>"
                 f"<td class=n>{fmt_p(r.holm_p)}</td><td class=n>{_E(fmt_effect(r))}</td>"
                 f"<td class=n>{r.n_supplier:,} / {r.n_other:,}</td>")
        if with_conclusion:
            cells += f"<td>{_E(verdict(r))}</td>"
        rows.append(f"<tr>{cells}</tr>")
    head = ("<tr><th>Attribute</th><th>Test</th><th class=n>Statistic</th><th class=n>df</th><th class=n>p</th>"
            "<th class=n>Holm p</th><th class=n>Effect size</th><th class=n>n supplier / non-supplier</th>"
            + ("<th>Conclusion</th>" if with_conclusion else "") + "</tr>")
    return f'<div class="scroll"><table>{head}{"".join(rows)}</table></div>'


def methods_text(c: Comparison) -> List[str]:
    return [
        f"Categorical attributes: Pearson's chi-squared test of homogeneity on the 2 x k table of counts (no "
        f"continuity correction), with Cramér's V = sqrt(chi-squared / n) as the effect size (under 0.1 "
        f"negligible, from 0.1 small, from 0.3 medium, from 0.5 large).",
        f"Expected counts: when more than {100 * MAX_SPARSE_SHARE:.0f}% of the expected counts are under "
        f"{MIN_EXPECTED:g}, or one is under {MIN_ANY_EXPECTED:g}, the category with the smallest total is pooled "
        "until the rule holds or two categories remain - with its smaller neighbour for ordered categories "
        f"(size band, severity, ASD category, half-year), into one \"{POOLED_LABEL}\" group otherwise. A 2 x 2 "
        "table that still fails the rule is tested with Fisher's exact test (two-sided), which gives an exact "
        "p-value and has no test statistic or degrees of freedom; its effect size is the phi coefficient "
        "|ad - bc| / sqrt((a+b)(c+d)(a+c)(b+d)) of the 2 x 2 counts, read on the same scale. The pooled categories "
        "are named under each chart; the charts and tables show every category unpooled.",
        "Unknown and missing values are left out of each test and reported per group under each chart.",
        f"Records affected: a two-sided Mann-Whitney U test on the events with a count. Its effect size is the "
        "absolute rank-biserial correlation |2P - 1|, where P is the probability that a supplier event has more "
        "records affected than a non-supplier event (ties counted half): 0 when neither group tends to be "
        "larger, 1 when one always is (the same words at the same cut-offs: 0.1, 0.3, 0.5). Which group is "
        "larger is said only for a significant result.",
        "A significant result is labelled with the size of its effect - negligible, small, medium or large "
        "difference - never as a bare \"difference\": with this many events a difference too small to matter "
        "can still be statistically significant.",
        f"Multiple tests: p-values are Holm-adjusted across the {c.family} tests that ran. A difference is "
        f"reported when the adjusted p-value is under {ALPHA}; otherwise the result is \"no evidence of a "
        "difference\" and no direction is given.",
        f"Categories named in a conclusion are those whose adjusted standardised residual is beyond "
        f"{RESIDUAL_CUT} in the tested table.",
        "An organisation with several events contributes each of them, and the events of one supplier incident "
        "share an attack; the one-event-per-incident check addresses the second, not the first.",
    ]


def tests_card(c: Comparison) -> str:
    sentences = "".join(f"<li>{_E(conclusion(r, c.family))}</li>" for r in c.results)
    cluster = "".join(f"<p>{_E(s)}</p>" for s in cluster_sentences(c))
    methods = "".join(f"<li>{_E(m)}</li>" for m in methods_text(c))
    return (
        '<div class="card" id="tests"><h2>Are the differences statistically significant?</h2>'
        f"{tests_table(c.results, c.family)}"
        f'<ul class="sum">{sentences}</ul>'
        "<h3>Check: one event per supplier incident</h3>"
        f"{cluster}{tests_table(c.sensitivity, c.family)}"
        f'<details><summary>Methods</summary><ul class="sum">{methods}</ul></details></div>')


def summary_card(c: Comparison) -> str:
    items = "".join(f"<li>{_E(s)}</li>" for s in headline_sentences(c))
    return (f'<div class="card" id="summary"><h2>Summary</h2><ul class="sum">{items}</ul>'
            '<p class="m">Each attribute has a chart and a table below; the tests and how they were done are at '
            'the <a href="#tests">bottom of the page</a>.</p></div>')


def build_html(c: Comparison, generated: str) -> str:
    """The page. Every count, percentage and sentence of the conclusions is generated from ``c``."""
    by_key = {r.key: r for r in c.results}
    cards = [summary_card(c), groups_card(c)]
    for index, t in enumerate(c.tabulations):
        cards.append(attribute_card(t, by_key[t.attribute.key], c))
        if index == 1:                      # after size band and industry, as in the brief's order
            cards.append(records_card(c, by_key[RECORDS_KEY]))
    cards.append(tests_card(c))
    intro = (f"Active events dated {c.period.start_iso} to {c.period.end_iso} that involve a supplier, compared "
             f"with those that do not. Generated {_E(generated)}.")
    return PAGE.format(intro=intro, body="\n".join(cards))


# --- command line ----------------------------------------------------------------------------------------

def connect_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def text_summary(c: Comparison) -> str:
    """The group sizes and every test, as plain text (printed by the command line)."""
    lines = [group_sentence(c)]
    for key, text in BASES:
        lines.append(f"  {sum(1 for e in c.supplier_events if key in e['bases'])} {text}")
    lines.append("  " + sensitivity_sentence(c))
    for title, results in (("All events", c.results), ("One event per supplier incident", c.sensitivity)):
        lines.append(f"\n{title}:")
        for r in results:
            if not r.ran:
                lines.append(f"  {r.label}: not tested ({r.reason})")
                continue
            dof = f", df {r.dof}" if r.dof is not None else ""
            lines.append(f"  {r.label}: {r.method}; {fmt_statistic(r)}{dof}; p = {r.p:.4g}; Holm p = "
                         f"{r.holm_p:.4g}; {fmt_effect(r)}; n = {r.n_supplier} / {r.n_other}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = ap.add_mutually_exclusive_group()
    source.add_argument("--db", type=Path, help="read this SQLite database (opened read-only) instead of a snapshot")
    source.add_argument("--snapshot-id", default=None,
                        help="a retained snapshot (an id, or 'latest-published', the default)")
    ap.add_argument("--db-path", type=Path, default=Path(DEFAULT_DB),
                    help="the database whose snapshot store (<its folder>/snapshots) to read")
    ap.add_argument("--out", type=Path, default=Path(DEFAULT_OUT))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from cyber_data_collector.publication import snapshot as snapshots

    try:
        with tempfile.TemporaryDirectory(prefix="supplier-comparison-") as work:
            if args.db:
                if not args.db.is_file():
                    raise ComparisonError(f"database not found: {args.db}")
                db, named = args.db, f"database {args.db}"
            else:
                store = snapshots.store_for(args.db_path)
                sid = store.resolve(args.snapshot_id or snapshots.LATEST_PUBLISHED)
                db, named = store.restore(sid, Path(work)), f"snapshot {sid}"
            conn = connect_read_only(db)
            try:
                comparison = collect(conn)
            finally:
                conn.close()
    except (ComparisonError, snapshots.SnapshotError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build_html(comparison, f"from {named}, {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC"),
                        encoding="utf-8")
    print(text_summary(comparison))
    print(f"\n-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
