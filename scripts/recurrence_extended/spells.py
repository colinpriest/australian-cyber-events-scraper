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

**Two policies, and what each says about unknown ordering** (``tie_policy``):

``collapse`` (default - the primary analysis)
    The incidents of a tie are one time point for the organisation.  No order,
    and no gap between them, is invented: a tie contributes one point to the
    organisation's history, so it can start or end one spell, never a repeat of
    its own.  The incident ids and their count are kept on the point
    (``event_ids``, ``event_count``), and every tie is reported
    (:func:`tie_table`, :func:`tie_summary`) rather than hidden.  This
    *understates* repeats when tied incidents really were separate attacks.

``zero_gap``
    Every incident is its own point; the incidents of a tie are ordered
    arbitrarily (by id) and each follows the previous one after a gap of
    :data:`SAME_DATE_GAP_DAYS` day - the stored resolution, since an exact zero
    would give a spell no exposure.  A tie of ``n`` incidents is ``n - 1``
    repeats in the first elapsed-time band, and counts toward the prior-event
    number of what follows.  This *overstates* repeats when a tie is one
    incident filed twice or a month-precision placeholder.  It exists as a
    sensitivity bound, never as the primary analysis.

The truth for any one tie lies between the two, so conclusions that survive both
are not artefacts of how ties were handled.  The underlying incidents are never
changed; only how the analysis counts them.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

TIE_POLICIES = ("collapse", "zero_gap")
DEFAULT_TIE_POLICY = "collapse"
#: Gap, in days, between two incidents of one organisation on the same stored date under ``zero_gap``.
SAME_DATE_GAP_DAYS = 1

TIE_KINDS = ("month_precision_first_of_month", "first_of_month_not_month_verified", "other_day")


def check_policy(tie_policy: str) -> str:
    if tie_policy not in TIE_POLICIES:
        raise ValueError(f"tie_policy must be one of {TIE_POLICIES}, not {tie_policy!r}")
    return tie_policy


def id_list(values: Any) -> str:
    """Incident ids as one sorted, ``|``-joined string (how every output stores a set of incidents)."""
    return "|".join(sorted({str(v) for v in values}))


def _to_dates(values: pd.Series) -> pd.Series:
    return pd.to_datetime(values, errors="coerce").dt.date


def occurrences(events: pd.DataFrame, tie_policy: str = DEFAULT_TIE_POLICY,
                aggregations: Mapping[str, tuple[str, Callable[[pd.Series], Any] | str]] | None = None) -> pd.DataFrame:
    """One row per organisation time point, in date order.

    ``events`` has one row per (organisation, incident): ``entity_id``,
    ``deduplicated_event_id`` and ``event_date``.  Rows with an unknown date are
    dropped - an undated incident is unknown, never given a day.  ``aggregations`` are pandas named
    aggregations (``column=("source column", func)``) carried onto each point; under ``collapse`` they
    summarise the tied incidents, under ``zero_gap`` each point is one incident.

    The result has ``entity_id``, ``event_date``, ``event_ids`` (the incident ids at the point),
    ``event_count`` (how many), ``tie_size`` (incidents of this organisation on this date), and, under
    ``zero_gap``, ``tie_position`` (1 .. ``tie_size``, by id).
    """
    check_policy(tie_policy)
    required = {"entity_id", "deduplicated_event_id", "event_date"}
    missing = required - set(events.columns)
    if missing:
        raise ValueError(f"Missing recurrence event columns: {', '.join(sorted(missing))}")
    frame = events.copy()
    frame["event_date"] = _to_dates(frame["event_date"])
    frame = frame[frame["event_date"].notna() & frame["entity_id"].notna()]
    frame = frame.drop_duplicates(["entity_id", "deduplicated_event_id"])
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
    if tie_policy == "zero_gap":
        grouped = grouped.drop(columns="_incident")
    sizes = frame.groupby(["entity_id", "event_date"])["deduplicated_event_id"].nunique()
    grouped["tie_size"] = [int(sizes.loc[(e, d)]) for e, d in zip(grouped["entity_id"], grouped["event_date"])]
    grouped = grouped.sort_values(["entity_id", "event_date", "event_ids"]).reset_index(drop=True)
    if tie_policy == "zero_gap":
        grouped["tie_position"] = grouped.groupby(["entity_id", "event_date"]).cumcount() + 1
    return grouped


def make_spells(points: pd.DataFrame, censor: date, tie_policy: str = DEFAULT_TIE_POLICY,
                carry_next: Sequence[str] = ()) -> pd.DataFrame:
    """One right-censored gap after each time point (from :func:`occurrences`).

    A point's spell runs to the organisation's next point, or to ``censor`` if
    there is none on or before it - an incident after the censoring date is
    never the end of an observed repeat.  A point on or after ``censor`` has no
    spell.  Under ``zero_gap`` a repeat on the same date as its predecessor is a
    spell of :data:`SAME_DATE_GAP_DAYS` day (``tie_gap == 1``).

    Result: the point's own columns plus ``spell_end_date``, ``duration_days``,
    ``event`` (1 = a repeat ended it), ``prior_event_number`` (1 + the points
    before it for the organisation), ``next_event_ids``, ``next_event_count``,
    ``tie_gap`` and ``next_<column>`` for each of ``carry_next``.
    """
    check_policy(tie_policy)
    points = points[points["event_date"] <= censor]
    points = points.sort_values(["entity_id", "event_date", "event_ids"], kind="stable").reset_index(drop=True)
    extra = [f"next_{c}" for c in carry_next]
    if points.empty:
        return pd.DataFrame(columns=[*points.columns, "spell_end_date", "duration_days", "event",
                                     "prior_event_number", "next_event_ids", "next_event_count", "tie_gap", *extra])
    by_entity = points.groupby("entity_id", sort=False)
    out = points.copy()
    out["prior_event_number"] = by_entity.cumcount() + 1
    next_date = by_entity["event_date"].shift(-1)
    out["event"] = next_date.notna().astype(int)
    out["spell_end_date"] = [n if pd.notna(n) else censor for n in next_date]
    out["next_event_ids"] = by_entity["event_ids"].shift(-1).fillna("")
    out["next_event_count"] = by_entity["event_count"].shift(-1).fillna(0).astype(int)
    for column in carry_next:
        out[f"next_{column}"] = by_entity[column].shift(-1)
    days = np.array([(end - start).days for start, end in zip(out["event_date"], out["spell_end_date"])])
    out["tie_gap"] = ((days == 0) & (out["event"] == 1)).astype(int)
    if tie_policy == "collapse" and out["tie_gap"].any():
        raise AssertionError("collapse produced two points on one date")
    out["duration_days"] = np.where(out["tie_gap"] == 1, SAME_DATE_GAP_DAYS, days)
    out = out[(out["event_date"] < censor) & (out["duration_days"] > 0)]
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
    may not be a placeholder); ``other_day`` (any other date).  Ties are by stored date; two incidents
    whose verified intervals overlap on different stored dates are not listed.
    """
    columns = ["entity_id", "entity_name", "event_date", "incident_count", "event_ids", "titles", "kind",
               "precisions"]
    required = {"entity_id", "deduplicated_event_id", "event_date"}
    if not required <= set(events.columns):
        raise ValueError(f"Missing recurrence event columns: {', '.join(sorted(required - set(events.columns)))}")
    frame = events.copy()
    frame["event_date"] = _to_dates(frame["event_date"])
    frame = frame[frame["event_date"].notna() & frame["entity_id"].notna()]
    frame = frame.drop_duplicates(["entity_id", "deduplicated_event_id"])
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
        rows.append({
            "entity_id": entity,
            "entity_name": str(group["entity_name"].iloc[0]) if "entity_name" in group else "",
            "event_date": day,
            "incident_count": int(group["deduplicated_event_id"].nunique()),
            "event_ids": id_list(group["deduplicated_event_id"]),
            "titles": " | ".join(titles),
            "kind": _tie_kind(day, precisions),
            "precisions": "|".join(sorted(set(precisions))),
        })
    return pd.DataFrame(rows, columns=columns)


def tie_summary(ties: pd.DataFrame) -> dict[str, Any]:
    """Counts for the report: how many ties, how many incidents are in them, and of which kind."""
    summary: dict[str, Any] = {
        "ties (organisation-dates with several incidents)": int(len(ties)),
        "incidents in ties": int(ties["incident_count"].sum()) if len(ties) else 0,
        "organisations with a tie": int(ties["entity_id"].nunique()) if len(ties) else 0,
        "repeats a zero-gap policy would add": int((ties["incident_count"] - 1).sum()) if len(ties) else 0,
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
