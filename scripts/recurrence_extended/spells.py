"""The one place recurrence spells are built, and where tied incident dates are handled.

Every recurrence analysis - the piecewise-exponential models of
``scripts/analyze_recurrent_timing.py``, the timing-band and date-precision
sensitivity runs, and the order/transition runs - starts from the same thing:
each victim organisation's incidents in date order, and the gap from each one
to the next (or to the censoring date).  Three separate copies of that step
used to disagree about what to do when an organisation has several incidents
with the same stored date.

**What a tie is.**  Two or more *distinct incidents* (``deduplicated_event_id``)
of one organisation whose stored incident date is the same calendar day.  A date
stored as the 1st of a month may only mean "some day in that month"
(``processing/incident_dates.py``), so many ties are month-precision placeholders
rather than genuinely simultaneous incidents.  Nothing in the data orders the
incidents of a tie.

**Tie policies, and what each assumes** (``tie_policy``).  Each states one
assumption about the tied incidents, kept in :data:`TIE_POLICY_ASSUMPTIONS`
(the reports quote it):

``collapse`` (default - the primary analysis)
    The tied incidents are *one occurrence*: one time point for the
    organisation, so a tie can start or end one spell but is never a repeat of
    its own.  The incident ids and their count are kept on the point
    (``event_ids``, ``event_count``), and every tie is reported
    (:func:`tie_table`, :func:`tie_summary`) rather than hidden.

``zero_gap``
    The tied incidents are *distinct and one day apart*, ordered arbitrarily
    (by id): each follows the previous one after :data:`SAME_DATE_GAP_DAYS` day
    - the stored resolution, since an exact zero would give a spell no
    exposure.  A tie of ``n`` incidents is ``n - 1`` repeats in the first
    elapsed-time band, and counts toward the prior-event number of what
    follows.

``interval_spread``
    The tied incidents are *distinct and evenly spread across the verified
    interval they share*: the intersection of their verified
    ``event_date_earliest`` / ``event_date_latest`` intervals, cut at the
    censoring date, ``L`` days long and holding the stored date.  Incident ``k``
    of ``n`` (by id) is placed ``floor((k - 1) * L / n)`` days after the
    interval's start, so consecutive incidents are about ``L / n`` days apart
    and the first is at the start (the stored date, for a month placeholder).
    A tie with no shared verified interval longer than a day - verified days,
    or an unverified date, which has no interval - is placed as under
    ``zero_gap``.

``interval_draw`` (random, :data:`RANDOM_TIE_POLICIES`: needs an ``rng``)
    The tied incidents are *distinct, each on a day drawn uniformly and
    independently* within the shared verified interval; used over the extended
    analysis's repeated date draws.  Ties without such an interval are placed as
    under ``zero_gap``.

**None of these bounds the truth.**  A tie that is one incident filed twice is
represented only by ``collapse``; two incidents really 25 days apart within a
verified month are represented exactly by none (``interval_spread`` puts two
of them 15 days apart, ``zero_gap`` 1 day); and a tie whose dates are
unverified has no interval at all - its incidents may be months apart, or the
stored date wrong.  Results that agree across the policies do not depend on
*these* assumptions, which is all such agreement shows.  The underlying
incidents are never changed; only how the analysis counts them.

Spells run between *placed* dates (``placed_date``: the stored date, except for
tied incidents an interval policy places); nothing is placed after the
censoring date, and a repeat between two incidents on or before it is observed.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

#: Deterministic tie policies, usable wherever spells are built (``--ties``).
TIE_POLICIES = ("collapse", "zero_gap", "interval_spread")
#: Tie policies that place tied incidents at random: only where spells are rebuilt over repeated draws.
RANDOM_TIE_POLICIES = ("interval_draw",)
ALL_TIE_POLICIES = TIE_POLICIES + RANDOM_TIE_POLICIES
DEFAULT_TIE_POLICY = "collapse"
#: The one assumption each policy makes about the incidents of a tie, as the reports state it.
TIE_POLICY_ASSUMPTIONS = {
    "collapse": "the tied incidents are one occurrence: one time point, never a repeat between them "
                "(their ids and count are kept)",
    "zero_gap": "the tied incidents are distinct and one day apart, in id order: each after the first is a "
                "repeat after 1 day",
    "interval_spread": "the tied incidents are distinct and evenly spread across the verified interval they "
                       "share (about interval length / n days apart, the first at the interval's start); a tie "
                       "with no shared verified interval longer than a day is placed as under zero_gap",
    "interval_draw": "the tied incidents are distinct, each on a day drawn uniformly and independently within "
                     "the verified interval they share, over the date draws; a tie with no shared verified "
                     "interval longer than a day is placed as under zero_gap",
}
#: Gap, in days, between two incidents of one organisation placed on the same day (``zero_gap``, and an
#: interval policy whose interval has fewer days than incidents).
SAME_DATE_GAP_DAYS = 1
#: Precisions whose stored interval is verified (``processing/incident_dates.py``).
VERIFIED_PRECISIONS = ("day", "month", "year")

TIE_KINDS = ("month_precision_first_of_month", "first_of_month_not_month_verified", "other_day")


def check_policy(tie_policy: str) -> str:
    if tie_policy not in ALL_TIE_POLICIES:
        raise ValueError(f"tie_policy must be one of {ALL_TIE_POLICIES}, not {tie_policy!r}")
    return tie_policy


def id_list(values: Any) -> str:
    """Incident ids as one sorted, ``|``-joined string (how every output stores a set of incidents)."""
    return "|".join(sorted({str(v) for v in values}))


def _to_dates(values: pd.Series) -> pd.Series:
    return pd.to_datetime(values, errors="coerce").dt.date


def _verified_bounds(frame: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Each incident's verified date interval, ``None`` where its precision is unverified.

    ``date_earliest`` / ``date_latest`` (``timing_precision.annotate_precision``: verified only) are used
    when present; otherwise the stored ``event_date_earliest`` / ``event_date_latest``, only where
    ``date_precision`` is a verified precision.  An unverified date has no interval.
    """
    if {"date_earliest", "date_latest"} <= set(frame.columns):
        return _to_dates(frame["date_earliest"]), _to_dates(frame["date_latest"])
    if not {"event_date_earliest", "event_date_latest", "date_precision"} <= set(frame.columns):
        none = pd.Series([None] * len(frame), index=frame.index, dtype=object)
        return none, none
    verified = frame["date_precision"].isin(VERIFIED_PRECISIONS)
    return (_to_dates(frame["event_date_earliest"]).where(verified, None),
            _to_dates(frame["event_date_latest"]).where(verified, None))


def shared_intervals(frame: pd.DataFrame) -> dict[tuple[Any, date], tuple[date, date]]:
    """``(organisation, stored date) -> (start, end)`` for each tie whose incidents share a verified interval.

    The shared interval is the intersection of the tied incidents' verified intervals; a tie is listed only
    when every one of its incidents has a verified interval and the intersection holds the stored date.
    ``frame`` has one row per (organisation, incident) with dates already parsed.
    """
    start, end = _verified_bounds(frame)
    work = pd.DataFrame({"entity_id": frame["entity_id"], "event_date": frame["event_date"],
                         "start": start, "end": end})
    shared: dict[tuple[Any, date], tuple[date, date]] = {}
    for (entity, day), group in work.groupby(["entity_id", "event_date"], sort=False):
        if len(group) < 2 or group["start"].isna().any() or group["end"].isna().any():
            continue
        low, high = max(group["start"]), min(group["end"])
        if low <= day <= high:
            shared[(entity, day)] = (low, high)
    return shared


def _clean(events: pd.DataFrame) -> pd.DataFrame:
    frame = events.copy()
    frame["event_date"] = _to_dates(frame["event_date"])
    frame = frame[frame["event_date"].notna() & frame["entity_id"].notna()]
    return frame.drop_duplicates(["entity_id", "deduplicated_event_id"])


def occurrences(events: pd.DataFrame, tie_policy: str = DEFAULT_TIE_POLICY,
                aggregations: Mapping[str, tuple[str, Callable[[pd.Series], Any] | str]] | None = None) -> pd.DataFrame:
    """One row per organisation time point, in date order.

    ``events`` has one row per (organisation, incident): ``entity_id``,
    ``deduplicated_event_id`` and ``event_date`` (and, for the interval policies, the verified
    intervals read by :func:`_verified_bounds`).  Rows with an unknown date are dropped - an undated
    incident is unknown, never given a day.  ``aggregations`` are pandas named aggregations
    (``column=("source column", func)``) carried onto each point; under ``collapse`` they summarise the
    tied incidents, under the other policies each point is one incident.

    The result has ``entity_id``, ``event_date``, ``event_ids`` (the incident ids at the point),
    ``event_count`` (how many), ``tie_size`` (incidents of this organisation on this date), and, under a
    policy that separates ties, ``tie_position`` (1 .. ``tie_size``, by id) and ``tie_interval_start`` /
    ``tie_interval_end`` (the tie's shared verified interval, :func:`shared_intervals`; ``None`` if none).
    """
    check_policy(tie_policy)
    required = {"entity_id", "deduplicated_event_id", "event_date"}
    missing = required - set(events.columns)
    if missing:
        raise ValueError(f"Missing recurrence event columns: {', '.join(sorted(missing))}")
    frame = _clean(events)
    named = dict(aggregations or {})
    frame["_incident"] = frame["deduplicated_event_id"]
    keys = ["entity_id", "event_date"] if tie_policy == "collapse" else ["entity_id", "event_date", "_incident"]
    if frame.empty:
        columns = ["entity_id", "event_date", "event_ids", "event_count", "tie_size", *named]
        return pd.DataFrame(columns=columns)
    grouped = frame.groupby(keys, as_index=False, sort=True).agg(
        event_ids=("deduplicated_event_id", id_list),
        event_count=("deduplicated_event_id", "nunique"),
        **named,
    )
    if tie_policy != "collapse":
        grouped = grouped.drop(columns="_incident")
    sizes = frame.groupby(["entity_id", "event_date"])["deduplicated_event_id"].nunique()
    grouped["tie_size"] = [int(sizes.loc[(e, d)]) for e, d in zip(grouped["entity_id"], grouped["event_date"])]
    grouped = grouped.sort_values(["entity_id", "event_date", "event_ids"]).reset_index(drop=True)
    if tie_policy != "collapse":
        grouped["tie_position"] = grouped.groupby(["entity_id", "event_date"]).cumcount() + 1
        shared = shared_intervals(frame)
        bounds = [shared.get((e, d)) for e, d in zip(grouped["entity_id"], grouped["event_date"])]
        grouped["tie_interval_start"] = [b[0] if b else None for b in bounds]
        grouped["tie_interval_end"] = [b[1] if b else None for b in bounds]
    return grouped


def _placed_dates(points: pd.DataFrame, censor: date, tie_policy: str,
                  rng: np.random.Generator | None) -> list[date]:
    """Where each point sits in time: its stored date, except the tied incidents an interval policy places."""
    placed = list(points["event_date"])
    if tie_policy not in ("interval_spread", "interval_draw") or "tie_interval_start" not in points:
        return placed
    if tie_policy == "interval_draw" and rng is None:
        raise ValueError("interval_draw places tied incidents at random and needs an rng")
    for i, (start, end, position, size) in enumerate(zip(points["tie_interval_start"], points["tie_interval_end"],
                                                         points["tie_position"], points["tie_size"])):
        if start is None or end is None or pd.isna(start) or pd.isna(end):
            continue
        length = (min(end, censor) - start).days + 1        # never placed after the censoring date
        if length <= 1:
            continue
        if tie_policy == "interval_spread":
            offset = (int(position) - 1) * length // int(size)
        else:
            offset = int(rng.integers(0, length))
        placed[i] = start + timedelta(days=int(offset))
    return placed


def make_spells(points: pd.DataFrame, censor: date, tie_policy: str = DEFAULT_TIE_POLICY,
                carry_next: Sequence[str] = (), rng: np.random.Generator | None = None) -> pd.DataFrame:
    """One right-censored gap after each time point (from :func:`occurrences`).

    A point's spell runs from its placed date to the organisation's next point, or to ``censor`` if there
    is none on or before it - an incident after the censoring date is never the end of an observed repeat.
    A repeat between two points on or before ``censor`` is observed, even when both are on it (a tie on
    the censoring date under ``zero_gap``); a point with nothing after it on ``censor`` has no spell.
    Two points placed on the same day are :data:`SAME_DATE_GAP_DAYS` day apart (``tie_gap == 1``).
    ``rng`` is required by ``interval_draw`` only.

    Result: the point's own columns plus ``placed_date``, ``spell_end_date`` (the next point's placed
    date, or ``censor``), ``duration_days``, ``event`` (1 = a repeat ended it), ``prior_event_number``
    (1 + the points before it for the organisation), ``next_event_ids``, ``next_event_count``,
    ``tie_gap``, ``tie_repeat`` (1 = a repeat between two incidents of one tie) and ``next_<column>`` for
    each of ``carry_next``.
    """
    check_policy(tie_policy)
    points = points[points["event_date"] <= censor].copy()
    extra = [f"next_{c}" for c in carry_next]
    if points.empty:
        return pd.DataFrame(columns=[*points.columns, "placed_date", "spell_end_date", "duration_days", "event",
                                     "prior_event_number", "next_event_ids", "next_event_count", "tie_gap",
                                     "tie_repeat", *extra])
    points["placed_date"] = _placed_dates(points.reset_index(drop=True), censor, tie_policy, rng)
    points = points.sort_values(["entity_id", "placed_date", "event_date", "event_ids"],
                                kind="stable").reset_index(drop=True)
    by_entity = points.groupby("entity_id", sort=False)
    out = points.copy()
    out["prior_event_number"] = by_entity.cumcount() + 1
    next_date = by_entity["placed_date"].shift(-1)
    next_stored = by_entity["event_date"].shift(-1)
    out["event"] = next_date.notna().astype(int)
    out["spell_end_date"] = [n if pd.notna(n) else censor for n in next_date]
    out["next_event_ids"] = by_entity["event_ids"].shift(-1).fillna("")
    out["next_event_count"] = by_entity["event_count"].shift(-1).fillna(0).astype(int)
    for column in carry_next:
        out[f"next_{column}"] = by_entity[column].shift(-1)
    days = np.array([(end - start).days for start, end in zip(out["placed_date"], out["spell_end_date"])])
    out["tie_gap"] = ((days == 0) & (out["event"] == 1)).astype(int)
    out["tie_repeat"] = [int(bool(e) and n == d) for e, n, d in zip(out["event"], next_stored, out["event_date"])]
    if tie_policy == "collapse" and out["tie_gap"].any():
        raise AssertionError("collapse produced two points on one date")
    out["duration_days"] = np.where(out["tie_gap"] == 1, SAME_DATE_GAP_DAYS, days)
    # Every spell with exposure stays: a censored one starts before ``censor``, and an observed one
    # ends at a point on or before it (two tied incidents on the censoring date included).
    out = out[out["duration_days"] > 0]
    return out.reset_index(drop=True)


def _tie_kind(day: date, precisions: Sequence[Any]) -> str:
    if day.day == 1:
        if all(p == "month" for p in precisions):
            return "month_precision_first_of_month"
        return "first_of_month_not_month_verified"
    return "other_day"


def tie_table(events: pd.DataFrame) -> pd.DataFrame:
    """Every (organisation, date) with more than one incident, and what kind of tie it is.

    ``kind``: ``month_precision_first_of_month`` (the stored 1st is a month placeholder: every incident's
    verified precision is month, so their order and spacing are unknown by construction);
    ``first_of_month_not_month_verified`` (a 1st whose precision is not all verified as month - it may or
    may not be a placeholder); ``other_day`` (any other date).  ``shared_interval_days`` is the length of
    the verified interval the tied incidents share (:func:`shared_intervals`; empty when they share none),
    the interval ``interval_spread`` spreads them over.  Ties are by stored date; two incidents whose
    verified intervals overlap on different stored dates are not listed.
    """
    columns = ["entity_id", "entity_name", "event_date", "incident_count", "event_ids", "titles", "kind",
               "precisions", "shared_interval_days"]
    required = {"entity_id", "deduplicated_event_id", "event_date"}
    if not required <= set(events.columns):
        raise ValueError(f"Missing recurrence event columns: {', '.join(sorted(required - set(events.columns)))}")
    frame = _clean(events)
    shared = shared_intervals(frame)
    rows = []
    for (entity, day), group in frame.groupby(["entity_id", "event_date"], sort=True):
        if group["deduplicated_event_id"].nunique() < 2:
            continue
        if "date_precision_basis" in group:        # the extended analysis's reconciled label
            precisions = [b.replace("explicit_", "") if isinstance(b, str) and b.startswith("explicit_") else "unverified"
                          for b in group["date_precision_basis"]]
        else:
            precisions = [p if isinstance(p, str) and p else "unverified"
                          for p in (group["date_precision"] if "date_precision" in group else ["unverified"] * len(group))]
        titles = group["title"].dropna().astype(str).head(5).tolist() if "title" in group else []
        interval = shared.get((entity, day))
        rows.append({
            "entity_id": entity,
            "entity_name": str(group["entity_name"].iloc[0]) if "entity_name" in group else "",
            "event_date": day,
            "incident_count": int(group["deduplicated_event_id"].nunique()),
            "event_ids": id_list(group["deduplicated_event_id"]),
            "titles": " | ".join(titles),
            "kind": _tie_kind(day, precisions),
            "precisions": "|".join(sorted(set(precisions))),
            "shared_interval_days": (interval[1] - interval[0]).days + 1 if interval else None,
        })
    return pd.DataFrame(rows, columns=columns)


def tie_summary(ties: pd.DataFrame) -> dict[str, Any]:
    """Counts for the report: how many ties, how many incidents are in them, and of which kind."""
    spread = (pd.to_numeric(ties["shared_interval_days"], errors="coerce") > 1) if len(ties) and \
        "shared_interval_days" in ties else pd.Series(dtype=bool)
    summary: dict[str, Any] = {
        "ties (organisation-dates with several incidents)": int(len(ties)),
        "incidents in ties": int(ties["incident_count"].sum()) if len(ties) else 0,
        "organisations with a tie": int(ties["entity_id"].nunique()) if len(ties) else 0,
        "repeats a policy that separates tied incidents adds": int((ties["incident_count"] - 1).sum()) if len(ties) else 0,
        "ties with a shared verified interval longer than a day (spread by interval_spread)": int(spread.sum()),
    }
    for kind in TIE_KINDS:
        summary[f"ties: {kind}"] = int((ties["kind"] == kind).sum()) if len(ties) else 0
    return summary


def overlapping_interval_pairs(events: pd.DataFrame, earliest: str = "event_date_earliest",
                               latest: str = "event_date_latest", window_days: int = 90) -> dict[str, int]:
    """Consecutive incidents of one organisation whose verified date intervals overlap.

    A tie is two incidents on the same *stored* date; two incidents in the same verified month stored on
    different days are no tie, yet their dates do not establish which came first either. Counts the
    consecutive pairs (in stored-date order) that overlap, and how many of those are inside
    ``window_days`` by stored date - the short elapsed times the recurrence models are estimated from.
    Pairs with an unverified date have no interval and are not counted.
    """
    short_key = f"... of which stored dates differ by under {window_days} days"
    if earliest not in events or latest not in events:
        return {"consecutive pairs with overlapping verified intervals": 0, short_key: 0}
    frame = events.copy()
    frame["event_date"] = _to_dates(frame["event_date"])
    frame["_start"] = _to_dates(frame[earliest])
    frame["_end"] = _to_dates(frame[latest])
    frame = frame[frame["event_date"].notna() & frame["entity_id"].notna()]
    frame = frame.drop_duplicates(["entity_id", "deduplicated_event_id"])
    frame = frame.sort_values(["entity_id", "event_date", "deduplicated_event_id"])
    overlapping = short = 0
    for _, group in frame.groupby("entity_id", sort=False):
        rows = list(zip(group["event_date"], group["_start"], group["_end"]))
        for (d1, s1, e1), (d2, s2, e2) in zip(rows, rows[1:]):
            if pd.isna(s1) or pd.isna(e1) or pd.isna(s2) or pd.isna(e2) or d1 == d2:
                continue
            if s2 <= e1 and s1 <= e2:
                overlapping += 1
                short += int((d2 - d1).days < window_days)
    return {"consecutive pairs with overlapping verified intervals": overlapping, short_key: short}
