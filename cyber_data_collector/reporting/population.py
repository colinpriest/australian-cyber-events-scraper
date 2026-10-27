"""The reporting population: which incidents every dashboard and export counts.

Why this module exists
----------------------
``DeduplicatedEvents`` is not a list of incidents. It is the whole history of
deduplication: a row stays behind, with its original id, after it has been
merged into another event (``Merged``) or after it was found never to be an
incident at all (``Rejected``). On the live database that is 700 ``Active``
rows beside 793 ``Merged`` and 544 ``Rejected`` ones.

``ASDRiskClassifications`` hangs off ``deduplicated_event_id`` and is never
cleaned when a row stops being active. The risk matrix counted every
classification, so it reported 1,201 "incidents" when only 643 classified
incidents were real - it nearly doubled the picture. Each dashboard had its own
copy of the status filter, so each could (and did) get it wrong independently.

Population definition
---------------------
An *incident* is a ``DeduplicatedEvents`` row with ``status = 'Active'``.

The flags ``is_australian_event`` and ``is_specific_event`` are deliberately
**not** part of the filter. Every dashboard query already ignored them, and on
this table they carry no information: ``DeduplicatedStorage`` writes both as
constant ``True`` for every row it creates ("all events are Australian",
"deduplicated events are specific"); the filtering that they stood for happened
upstream, on ``EnrichedEvents``, before a row could reach deduplication. Adding
them would change nothing today and would silently shrink the population if a
future writer stored ``0``. ``scripts/check_data_integrity.py`` verifies the
assumption instead (``population-flags-consistent``), so a violation is
reported rather than quietly changing every chart.

Everything else is scoped *from* that population, never beside it:

* ASD classifications are INNER JOINed to an active incident, so classifications
  of merged / rejected events (and orphans) cannot be counted.
* Entity links are joined from the incident with ``relationship_type = 'victim'``,
  so they cannot attach an organisation to an event outside the population.

Reporting periods
-----------------
A :class:`ReportingPeriod` is a pair of **calendar days, both inclusive**.
Incident dates are day-granular strings (``YYYY-MM-DD``); the SQL it emits is
half-open (``date >= start AND date < end + 1 day``). The half-open form is the
safe one: ``BETWEEN '2025-01-01' AND '2025-06-30'`` silently drops any value
carrying a time of day on the last day (``'2025-06-30 14:00'`` sorts after
``'2025-06-30'``), and writing ``<= end`` has the same flaw. An incident with
no date belongs to no period: it can be counted as an incident, but it cannot be
charted by time, and the reconciliation reports it explicitly.

Nothing that reports on a period should write a date literal; use
:func:`dashboard_period`, :func:`calendar_year`, :func:`half_year` and
:func:`half_year_from_label` so every query and every label comes from one place.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Iterator, List, Optional, Sequence, Tuple, Union

logger = logging.getLogger(__name__)

# Only status that denotes a real incident. 'Merged' and 'Rejected' are history.
ACTIVE_STATUS = "Active"

# First day any dashboard reports on. Incidents before it exist in the database
# (32 Active rows dated 2011-2019, plus one undated) but are not charted: OAIC's comparable series
# starts in 2020 and the monthly charts have always started here. The ASD matrix
# used to ignore this and so disagreed with every other chart on the page.
DASHBOARD_REPORTING_START = date(2020, 1, 1)

# OAIC's per-semester breakdowns (source of breach, time to notify, personal
# information, individuals-affected buckets, monthly bars) exist from 2022 H1.
# Database-side comparisons against them start on the same day.
OAIC_DETAIL_FIRST_YEAR = 2022

DateLike = Union[date, datetime, str]


def _to_date(value: DateLike) -> date:
    """Coerce a date, datetime or ISO string (date part only) to a ``date``."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip()[:10])


@dataclass(frozen=True)
class ReportingPeriod:
    """An inclusive range of calendar days.

    Attributes:
        start: First day of the period (inclusive).
        end: Last day of the period (inclusive).
        label: Human-readable name, e.g. ``"2025 H1"``. Informational only.
    """

    start: date
    end: date
    label: str = ""

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"Period ends before it starts: {self.start} > {self.end}")

    @classmethod
    def from_iso(cls, start: DateLike, end: DateLike, label: str = "") -> "ReportingPeriod":
        """Build a period from two ISO date strings (or dates)."""
        return cls(_to_date(start), _to_date(end), label)

    @property
    def end_exclusive(self) -> date:
        """The day after ``end`` - the upper bound of the half-open SQL range."""
        return self.end + timedelta(days=1)

    @property
    def start_iso(self) -> str:
        return self.start.isoformat()

    @property
    def end_iso(self) -> str:
        return self.end.isoformat()

    def sql_bounds(self) -> Tuple[str, str]:
        """``(lower, upper)`` for ``col >= lower AND col < upper``."""
        return self.start.isoformat(), self.end_exclusive.isoformat()

    def where(self, column: str = "event_date") -> Tuple[str, List[str]]:
        """SQL fragment and parameters restricting ``column`` to this period.

        ``column`` is interpolated, so it must be a trusted identifier from the
        calling code (never user input); the dates are bound parameters.
        """
        lower, upper = self.sql_bounds()
        return f"{column} >= ? AND {column} < ?", [lower, upper]

    def contains(self, value: Optional[DateLike]) -> bool:
        """True when ``value`` falls on a day inside the period (None is outside)."""
        if value is None or value == "":
            return False
        try:
            day = _to_date(value)
        except ValueError:
            return False
        return self.start <= day <= self.end

    def clipped_to(self, end: DateLike) -> "ReportingPeriod":
        """The same period cut off at ``end`` (used to drop an incomplete month)."""
        new_end = min(self.end, _to_date(end))
        return ReportingPeriod(self.start, new_end, self.label)

    def intersect(self, other: "ReportingPeriod") -> Optional["ReportingPeriod"]:
        """The days both periods contain, or None when they do not overlap.

        A calendar-year view of a dashboard period (e.g. this year, which has not
        finished) is the year clipped to what the dashboard reports on.
        """
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        if end < start:
            return None
        return ReportingPeriod(start, end, self.label)

    def months(self) -> Iterator[Tuple[int, int]]:
        """Yield every ``(year, month)`` the period touches, in order."""
        year, month = self.start.year, self.start.month
        while (year, month) <= (self.end.year, self.end.month):
            yield year, month
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)

    def __str__(self) -> str:
        return f"{self.label or 'period'} [{self.start_iso} .. {self.end_iso}]"


def dashboard_period(today: Optional[DateLike] = None) -> ReportingPeriod:
    """The dashboard's whole reporting window: 2020-01-01 to ``today``."""
    end = _to_date(today) if today is not None else date.today()
    return ReportingPeriod(DASHBOARD_REPORTING_START, end, f"{DASHBOARD_REPORTING_START.year} to date")


def calendar_year(year: int) -> ReportingPeriod:
    """1 January to 31 December of ``year``."""
    return ReportingPeriod(date(year, 1, 1), date(year, 12, 31), str(year))


def half_year(year: int, half: str) -> ReportingPeriod:
    """The OAIC reporting half: ``'H1'`` (Jan-Jun) or ``'H2'`` (Jul-Dec)."""
    key = half.strip().upper()
    if key == "H1":
        return ReportingPeriod(date(year, 1, 1), date(year, 6, 30), f"{year} H1")
    if key == "H2":
        return ReportingPeriod(date(year, 7, 1), date(year, 12, 31), f"{year} H2")
    raise ValueError(f"half must be 'H1' or 'H2', got {half!r}")


def half_year_from_label(label: str) -> ReportingPeriod:
    """Parse an OAIC-style label such as ``"2025 H2"``."""
    parts = str(label).split()
    if len(parts) != 2:
        raise ValueError(f"Not a half-year label: {label!r}")
    return half_year(int(parts[0]), parts[1])


def half_year_containing(day: DateLike) -> ReportingPeriod:
    """The half-year that contains ``day``."""
    d = _to_date(day)
    return half_year(d.year, "H1" if d.month <= 6 else "H2")


def half_years_between(first_year: int, last: DateLike) -> List[ReportingPeriod]:
    """Every half-year from ``first_year`` H1 up to the one containing ``last``."""
    final = half_year_containing(last)
    out: List[ReportingPeriod] = []
    year, half = first_year, "H1"
    while True:
        period = half_year(year, half)
        out.append(period)
        if period.start >= final.start:
            return out
        year, half = (year, "H2") if half == "H1" else (year + 1, "H1")


def span_of(periods: Iterable[ReportingPeriod]) -> Optional[ReportingPeriod]:
    """The smallest period covering all of ``periods`` (None when there are none).

    Used to line a database figure up with an OAIC series: the database is
    queried over exactly the semesters the OAIC side was summed over, instead of
    over a hard-coded range that happens to be close.
    """
    items = list(periods)
    if not items:
        return None
    start = min(p.start for p in items)
    end = max(p.end for p in items)
    whole_years = (start.month, start.day) == (1, 1) and (end.month, end.day) == (12, 31)
    if whole_years:
        label = str(start.year) if start.year == end.year else f"{start.year}-{end.year}"
    else:
        first = min(items, key=lambda p: p.start).label or start.isoformat()
        last = max(items, key=lambda p: p.end).label or end.isoformat()
        label = f"{first} to {last}"
    return ReportingPeriod(start, end, label)


# ---------------------------------------------------------------------------
# Population SQL
# ---------------------------------------------------------------------------

def incident_filter(
    period: Optional[ReportingPeriod] = None,
    alias: str = "",
    date_column: str = "event_date",
) -> Tuple[str, List[str]]:
    """WHERE fragment (and params) selecting active incidents, optionally in a period.

    Args:
        period: Restrict to incidents dated inside it. ``None`` means every active
            incident, including undated ones.
        alias: Table alias for ``DeduplicatedEvents`` (``"de"`` gives ``de.status``).
        date_column: Date column name; defaults to ``event_date``.

    Returns:
        ``(sql, params)``; ``sql`` has no leading ``WHERE``.
    """
    prefix = f"{alias}." if alias else ""
    clauses = [f"{prefix}status = '{ACTIVE_STATUS}'"]
    params: List[str] = []
    if period is not None:
        sql, params = period.where(f"{prefix}{date_column}")
        clauses.append(sql)
    return " AND ".join(clauses), params


# ASD classifications reachable only through an active incident. INNER JOIN, not
# LEFT: a classification with no incident behind it (or one that has been merged
# or rejected) must contribute nothing. Use together with incident_filter("de").
ASD_POPULATION_FROM = (
    "ASDRiskClassifications arc "
    "JOIN DeduplicatedEvents de ON arc.deduplicated_event_id = de.deduplicated_event_id"
)

# The breached organisation of each incident, reached from the incident alias
# ``de`` so it can never attach to an event outside the population. Only the
# 'victim' role: without it the join returns any linked entity, including the
# customers affected ("Australians") or a regulator.
VICTIM_ENTITY_JOINS = (
    "LEFT JOIN DeduplicatedEventEntities dee "
    "ON de.deduplicated_event_id = dee.deduplicated_event_id "
    "AND dee.relationship_type = 'victim' "
    "LEFT JOIN EntitiesV2 e ON dee.entity_id = e.entity_id"
)


def count_incidents(conn: sqlite3.Connection, period: Optional[ReportingPeriod] = None) -> int:
    """Number of active incidents (dated inside ``period`` when one is given)."""
    where, params = incident_filter(period, "de")
    return int(conn.execute(
        f"SELECT COUNT(*) FROM DeduplicatedEvents de WHERE {where}", params
    ).fetchone()[0])


def count_classified_incidents(conn: sqlite3.Connection,
                               period: Optional[ReportingPeriod] = None) -> int:
    """Active incidents that carry an ASD classification."""
    where, params = incident_filter(period, "de")
    return int(conn.execute(
        f"SELECT COUNT(DISTINCT de.deduplicated_event_id) FROM {ASD_POPULATION_FROM} "
        f"WHERE {where}", params
    ).fetchone()[0])


def count_classifications(conn: sqlite3.Connection,
                          period: Optional[ReportingPeriod] = None) -> int:
    """ASD classification rows belonging to active incidents.

    Equals :func:`count_classified_incidents` while ``ASDRiskClassifications``
    keeps its UNIQUE constraint on ``deduplicated_event_id``; they are separate
    functions so a relaxed constraint shows up as a reconciliation failure.
    """
    where, params = incident_filter(period, "de")
    return int(conn.execute(
        f"SELECT COUNT(*) FROM {ASD_POPULATION_FROM} WHERE {where}", params
    ).fetchone()[0])


def population_breakdown(conn: sqlite3.Connection,
                         period: Optional[ReportingPeriod] = None) -> dict:
    """Where every ``DeduplicatedEvents`` row sits relative to the population.

    Returned so a report can say exactly what it excluded instead of leaving the
    reader to wonder why a total differs from the table size.
    """
    out = {
        "total_rows": int(conn.execute("SELECT COUNT(*) FROM DeduplicatedEvents").fetchone()[0]),
        "active": count_incidents(conn),
        "merged": int(conn.execute(
            "SELECT COUNT(*) FROM DeduplicatedEvents WHERE status = 'Merged'").fetchone()[0]),
        "rejected": int(conn.execute(
            "SELECT COUNT(*) FROM DeduplicatedEvents WHERE status = 'Rejected'").fetchone()[0]),
        "active_undated": int(conn.execute(
            "SELECT COUNT(*) FROM DeduplicatedEvents "
            f"WHERE status = '{ACTIVE_STATUS}' AND (event_date IS NULL OR event_date = '')"
        ).fetchone()[0]),
    }
    if period is not None:
        out["active_in_period"] = count_incidents(conn, period)
        out["active_dated_outside_period"] = (
            out["active"] - out["active_undated"] - out["active_in_period"])
    return out


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

class ReconciliationError(AssertionError):
    """A report's totals do not add up to the reporting population."""


@dataclass(frozen=True)
class ReconciliationCheck:
    """One claim that a figure on a report agrees with the population's figure.

    Attributes:
        name: What is being reconciled, e.g. ``"ASD matrix total"``.
        expected: What the population says the figure is (or its ceiling).
        actual: What the report shows.
        detail: Optional context shown only on failure.
        relation: ``"=="`` (the default) when the report must equal the
            population; ``"<="`` for a chart that shows a defined subset of it
            (e.g. only incidents with a known breach source).
    """

    name: str
    expected: int
    actual: int
    detail: str = ""
    relation: str = "=="

    def __post_init__(self) -> None:
        if self.relation not in ("==", "<="):
            raise ValueError(f"relation must be '==' or '<=', got {self.relation!r}")

    @property
    def is_ok(self) -> bool:
        if self.relation == "<=":
            return self.actual <= self.expected
        return self.expected == self.actual

    def __str__(self) -> str:
        status = "ok" if self.is_ok else "MISMATCH"
        bound = "at most " if self.relation == "<=" else ""
        text = (f"{status}: {self.name}: report shows {self.actual}, "
                f"population has {bound}{self.expected}")
        return f"{text} ({self.detail})" if self.detail else text


def reconcile(checks: Sequence[ReconciliationCheck], context: str = "report") -> List[ReconciliationCheck]:
    """Log every check and raise if any failed.

    A wrong total that is published is worse than no page: it is read, quoted and
    compared with other figures. So a failure is logged at ERROR for each
    mismatch and then raised, and the caller must not write its output.

    Raises:
        ReconciliationError: if any check's ``actual`` differs from ``expected``.
    """
    failed = [c for c in checks if not c.is_ok]
    for check in checks:
        if check.is_ok:
            logger.info("Reconciliation (%s) %s", context, check)
        else:
            logger.error("RECONCILIATION FAILED (%s) %s", context, check)
    if failed:
        lines = "; ".join(str(c) for c in failed)
        message = (f"{context}: {len(failed)} of {len(checks)} totals do not reconcile "
                   f"to the reporting population - {lines}")
        logger.error(message)
        raise ReconciliationError(message)
    return list(checks)


def sum_counts(values: Sequence[Any]) -> int:
    """Total a chart series, ignoring gaps (None) the way a chart does."""
    return int(sum(v for v in values if isinstance(v, (int, float))))
