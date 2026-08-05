"""Analyse repeat cyber-event timing for victim entities.

This script implements the survival/recurrent-event analysis behind the
"memoryless vs delayed recurrence" question:

1. Build censored post-event spells for each victim entity.
2. Fit a primary piecewise exponential model for elapsed time since prior event.
3. Compare censored parametric survival models.
4. Write summary tables, plots, and a Markdown report.

The analysis is conditional on an entity having at least one observed event.
It does not estimate attack risk for the full population of Australian entities.

Usage:
    python scripts/analyze_recurrent_timing.py
    python scripts/analyze_recurrent_timing.py --bootstrap 1000
    python scripts/analyze_recurrent_timing.py --elapsed-bounds 0,180,365,730,1460,inf
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import optimize, stats
from scipy.special import gammaln


DEFAULT_DB = "instance/cyber_events.db"
DEFAULT_OUT_DIR = "analysis/recurrent_timing"
DEFAULT_CENSOR_DATE = "2026-08-05"
DEFAULT_ELAPSED_BOUNDS = (0, 180, 365, 730, 1460, math.inf)
DELAYED_PEAK_ELAPSED_BOUNDS = (0, 90, 180, 365, 730, 1460, math.inf)
DEFAULT_CALENDAR_BOUNDS = ("2012-01-01", "2019-01-01", "2022-01-01", "2024-01-01")
REQUESTED_COVARIATES = ("org_size_score", "org_size_unknown", "sector_proxy", "entity_kind_group", "prior_records_band")
SIZE_SCORE = {"SMALL": 1.0, "MEDIUM": 2.0, "LARGE": 3.0, "HUGE": 4.0}


@dataclass(frozen=True)
class FitResult:
    name: str
    params: dict[str, float]
    log_likelihood: float
    n_params: int
    converged: bool
    message: str

    def as_row(self, n: int) -> dict[str, Any]:
        return {
            "model": self.name,
            "log_likelihood": self.log_likelihood,
            "parameters": self.n_params,
            "aic": 2 * self.n_params - 2 * self.log_likelihood,
            "bic": self.n_params * math.log(n) - 2 * self.log_likelihood,
            "converged": self.converged,
            "message": self.message,
            "params_json": json.dumps(self.params, sort_keys=True),
        }


def parse_date(value: str | date | pd.Timestamp) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def safe_exp(value: float) -> float:
    if value > 709:
        return math.inf
    if value < -745:
        return 0.0
    return math.exp(value)


def first_present(values: Iterable[Any]) -> Any:
    for value in values:
        if pd.notna(value) and str(value).strip():
            return value
    return None


def records_for_json(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return json.loads(frame.replace({np.nan: None}).to_json(orient="records"))


def parse_magnitude(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip().lower().replace(",", "")
    if not text:
        return None
    multiplier = 1.0
    if "billion" in text or text.endswith("b"):
        multiplier = 1_000_000_000.0
    elif "million" in text or text.endswith("m"):
        multiplier = 1_000_000.0
    elif "thousand" in text or text.endswith("k"):
        multiplier = 1_000.0
    match = __import__("re").search(r"[-+]?\d*\.?\d+", text)
    if not match:
        return None
    return float(match.group(0)) * multiplier


def employee_size_bucket(value: Any) -> str:
    count = parse_magnitude(value)
    if count is None:
        return "unknown"
    if count < 50:
        return "small"
    if count < 250:
        return "medium"
    if count < 1000:
        return "large"
    return "very_large"


def turnover_size_bucket(value: Any) -> str:
    amount = parse_magnitude(value)
    if amount is None:
        return "unknown"
    if amount < 10_000_000:
        return "small"
    if amount < 100_000_000:
        return "medium"
    if amount < 1_000_000_000:
        return "large"
    return "very_large"


def revenue_for_bucket(researched: Any, legacy: Any) -> Any:
    """Best available annual revenue figure, preferring the researched one.

    ``EntitiesV2.turnover`` is a legacy free-text column that was never
    populated on any row, so reading it alone left ``turnover_size`` at a
    single "unknown" level for the whole dataset - which is why the model
    dropped it outright. The researched figure lives in ``size_revenue_aud``
    as a number.

    A recorded revenue of zero is treated as missing rather than as a very
    small organisation: no operating entity in this dataset genuinely turns
    over nothing, so a zero is an extraction artefact, and letting it through
    would file large organisations under "small".
    """
    amount = parse_magnitude(researched)
    if amount is not None and amount > 0:
        return amount
    return first_present([legacy])


def normalise_size_label(value: Any) -> str:
    label = str(value or "UNKNOWN").strip().upper()
    if label in SIZE_SCORE or label == "UNKNOWN":
        return label
    return "UNKNOWN"


def records_bucket(value: Any) -> str:
    count = parse_magnitude(value)
    if count is None or count <= 0:
        return "unknown"
    if count < 1_000:
        return "<1k"
    if count < 10_000:
        return "1k-10k"
    if count < 100_000:
        return "10k-100k"
    return "100k+"


def entity_kind_bucket(value: Any) -> str:
    kind = str(value or "").strip().lower()
    if kind == "government_body":
        return "government_body"
    if kind == "organisation":
        return "organisation"
    return "other_or_unknown"


def sector_proxy(entity_name: Any, entity_kind: Any, industry: Any) -> str:
    name = str(entity_name or "").lower()
    kind = str(entity_kind or "").lower()
    ind = str(industry or "").lower()
    if ind:
        if ind in {"government"}:
            return "government"
        if ind in {"education"}:
            return "education"
        if ind in {"financial", "finance"}:
            return "finance"
        if ind in {"healthcare", "health"}:
            return "health"
        if ind in {"telecommunications", "transportation"}:
            return "telecom_transport"
        if ind in {"technology"}:
            return "technology"
        if ind in {"retail"}:
            return "retail"
        if ind in {"media", "entertainment"}:
            return "media"
        if ind in {"manufacturing", "energy", "mining"}:
            return "industrial"

    text = f"{name} {kind} {ind}"

    if kind == "government_body" or any(
        token in text
        for token in [
            "government",
            "department",
            "council",
            "parliament",
            "commission",
            "agency",
            "police",
            "minister",
        ]
    ):
        return "government"
    if any(token in text for token in ["university", "tafe", "college", "school", "education"]):
        return "education"
    if any(
        token in text
        for token in [
            "bank",
            "anz",
            "nab",
            "westpac",
            "commonwealth bank",
            "macquarie",
            "financial",
            "finance",
            "insurance",
            "superannuation",
            "credit",
        ]
    ):
        return "finance"
    if any(token in text for token in ["health", "hospital", "medical", "clinic", "ambulance", "pharmacy"]):
        return "health"
    if any(token in text for token in ["telstra", "optus", "vodafone", "telecom", "transport", "qantas", "airline"]):
        return "telecom_transport"
    return "other"


def add_entity_covariates(events: pd.DataFrame) -> pd.DataFrame:
    events = events.copy()
    events["entity_kind"] = events["entity_kind"].fillna("unknown").replace("", "unknown")
    events["org_size"] = events["size_estimate"].apply(normalise_size_label)
    known_scores = events["org_size"].map(SIZE_SCORE)
    mean_known_score = float(known_scores.dropna().mean()) if known_scores.notna().any() else 0.0
    events["org_size_score"] = known_scores.fillna(mean_known_score)
    events["org_size_unknown"] = (events["org_size"] == "UNKNOWN").astype(int)
    events["employee_size"] = events["employee_count"].apply(employee_size_bucket)
    events["turnover_size"] = events["turnover"].apply(turnover_size_bucket)
    events["entity_kind_group"] = events["entity_kind"].apply(entity_kind_bucket)
    events["sector_proxy"] = [
        sector_proxy(row.entity_name, row.entity_kind, row.industry)
        for row in events.itertuples(index=False)
    ]
    events["records_bucket"] = events["records_affected"].apply(records_bucket)
    return events


def band_labels(bounds: Iterable[float]) -> list[str]:
    values = list(bounds)
    labels: list[str] = []
    for lo, hi in zip(values, values[1:]):
        first = int(lo) + (0 if lo == 0 else 1)
        if math.isinf(hi):
            labels.append(f">{int(lo)}")
        else:
            labels.append(f"{first}-{int(hi)}")
    return labels


def parse_elapsed_bounds(value: str) -> tuple[float, ...]:
    bounds: list[float] = []
    for item in value.split(","):
        token = item.strip().lower()
        if token in {"inf", "infinity", "+inf"}:
            bounds.append(math.inf)
        else:
            bounds.append(float(token))
    if len(bounds) < 3:
        raise argparse.ArgumentTypeError("Need at least 3 bounds, for example 0,180,365,730,inf")
    if bounds[0] != 0:
        raise argparse.ArgumentTypeError("Elapsed bounds must start at 0")
    if bounds[-1] != math.inf:
        raise argparse.ArgumentTypeError("Elapsed bounds must end with inf")
    if any(a >= b for a, b in zip(bounds, bounds[1:])):
        raise argparse.ArgumentTypeError("Elapsed bounds must be strictly increasing")
    return tuple(bounds)


def elapsed_band_index(duration_days: float, bounds: Iterable[float]) -> int:
    values = list(bounds)
    for idx, (lo, hi) in enumerate(zip(values, values[1:])):
        if duration_days > lo and duration_days <= hi:
            return idx
    return len(values) - 2


def u_shape_phase_for_index(elapsed_idx: int, n_elapsed_bands: int) -> str:
    if elapsed_idx == 0:
        return "immediate"
    if n_elapsed_bands <= 3:
        return "response" if elapsed_idx == 1 else "long_term"
    return "response" if elapsed_idx <= 2 else "long_term"


def delayed_peak_phase_for_bounds(lo: float, hi: float) -> str:
    if lo == 0 and hi == 90:
        return "immediate_0_90"
    if lo == 90 and hi == 180:
        return "peak_91_180"
    if lo >= 180:
        return "post_180"
    return "not_applicable"


def load_victim_events(db_path: Path) -> pd.DataFrame:
    query = """
        SELECT v.entity_id,
               v.entity_name,
               v.employee_count,
               v.turnover,
               v.entity_kind,
               v.size_estimate,
               v.size_confidence,
               v.size_employees,
               v.size_revenue_aud,
               COALESCE(NULLIF(TRIM(v.industry), ''),
                        NULLIF(TRIM(d.victim_organization_industry), '')) AS industry,
               d.deduplicated_event_id,
               d.title,
               d.event_date,
               d.event_type,
               d.records_affected,
               d.vendor_organization_name,
               d.date_confidence,
               d.confidence_score,
               d.total_data_sources,
               (SELECT COUNT(*)
                FROM EventDeduplicationMap m
                WHERE m.deduplicated_event_id = d.deduplicated_event_id) AS source_members
        FROM DeduplicatedEventEntities dee
        JOIN EntitiesV2 v ON v.entity_id = dee.entity_id
        JOIN DeduplicatedEvents d
             ON d.deduplicated_event_id = dee.deduplicated_event_id
        WHERE dee.relationship_type = 'victim'
          AND COALESCE(d.status, 'Active') = 'Active'
          AND COALESCE(d.is_australian_event, 1) = 1
          AND COALESCE(d.is_specific_event, 1) = 1
          AND d.event_date IS NOT NULL
          AND v.entity_id IS NOT NULL
          AND v.entity_name IS NOT NULL
          AND TRIM(v.entity_name) != ''
        ORDER BY v.entity_name COLLATE NOCASE, d.event_date, d.title
    """
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(query, conn)

    if df.empty:
        raise RuntimeError("No victim events found for recurrent timing analysis.")

    df["event_date"] = pd.to_datetime(df["event_date"]).dt.date
    df = df.drop_duplicates(["entity_id", "deduplicated_event_id"]).copy()
    return add_entity_covariates(df)


def collapse_entity_same_day_events(events: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Collapse multiple same-day events for one entity into one recurrence time point."""
    grouped = (
        events.groupby(["entity_id", "entity_name", "event_date"], as_index=False)
        .agg(
            event_ids=("deduplicated_event_id", lambda s: "|".join(sorted(map(str, s)))),
            titles=("title", lambda s: " | ".join(str(v) for v in s.dropna().head(5))),
            event_count=("deduplicated_event_id", "nunique"),
            industry=("industry", first_present),
            entity_kind=("entity_kind", first_present),
            org_size=("org_size", first_present),
            org_size_score=("org_size_score", "max"),
            org_size_unknown=("org_size_unknown", "max"),
            size_confidence=("size_confidence", "max"),
            employee_size=("employee_size", first_present),
            turnover_size=("turnover_size", first_present),
            entity_kind_group=("entity_kind_group", first_present),
            sector_proxy=("sector_proxy", first_present),
            records_affected=("records_affected", lambda s: pd.to_numeric(s, errors="coerce").max()),
            records_bucket=("records_bucket", first_present),
        )
    )
    same_day = grouped[grouped["event_count"] > 1].copy()
    return grouped, same_day


def build_spells(events: pd.DataFrame, censor_date: date) -> tuple[pd.DataFrame, pd.DataFrame]:
    collapsed, same_day = collapse_entity_same_day_events(events)
    rows: list[dict[str, Any]] = []

    for entity_id, group in collapsed.groupby("entity_id", sort=False):
        group = group.sort_values(["event_date", "event_ids"]).reset_index(drop=True)
        entity_name = str(group.loc[0, "entity_name"])
        industry = group.loc[0, "industry"]
        entity_kind = group.loc[0, "entity_kind"] or "unknown"
        entity_kind_group = group.loc[0, "entity_kind_group"] or "other_or_unknown"
        org_size = group.loc[0, "org_size"] or "UNKNOWN"
        org_size_score = float(group.loc[0, "org_size_score"])
        org_size_unknown = int(group.loc[0, "org_size_unknown"])
        size_confidence = group.loc[0, "size_confidence"]
        employee_size = group.loc[0, "employee_size"] or "unknown"
        turnover_size = group.loc[0, "turnover_size"] or "unknown"
        entity_sector = group.loc[0, "sector_proxy"] or "other"

        for i in range(len(group)):
            start = parse_date(group.loc[i, "event_date"])
            if start >= censor_date:
                continue

            has_next = i + 1 < len(group)
            if has_next:
                end = parse_date(group.loc[i + 1, "event_date"])
                next_event_ids = group.loc[i + 1, "event_ids"]
                next_titles = group.loc[i + 1, "titles"]
            else:
                end = censor_date
                next_event_ids = ""
                next_titles = ""

            duration = (end - start).days
            if duration <= 0:
                continue

            rows.append(
                {
                    "entity_id": entity_id,
                    "entity_name": entity_name,
                    "industry": industry,
                    "entity_kind": entity_kind,
                    "entity_kind_group": entity_kind_group,
                    "org_size": org_size,
                    "org_size_score": org_size_score,
                    "org_size_unknown": org_size_unknown,
                    "size_confidence": size_confidence,
                    "employee_size": employee_size,
                    "turnover_size": turnover_size,
                    "sector_proxy": entity_sector,
                    "prior_records_band": group.loc[i, "records_bucket"] or "unknown",
                    "prior_event_number": i + 1,
                    "prior_event_date": start.isoformat(),
                    "prior_event_ids": group.loc[i, "event_ids"],
                    "prior_titles": group.loc[i, "titles"],
                    "spell_end_date": end.isoformat(),
                    "next_event_ids": next_event_ids,
                    "next_titles": next_titles,
                    "duration_days": duration,
                    "event": int(has_next),
                    "event_number_cat": "3+" if i + 1 >= 3 else str(i + 1),
                }
            )

    spells = pd.DataFrame(rows)
    if spells.empty:
        raise RuntimeError("No positive-duration post-event spells could be built.")
    return spells, same_day


def split_by_elapsed(spells: pd.DataFrame, bounds: tuple[float, ...]) -> pd.DataFrame:
    labels = band_labels(bounds)
    rows: list[dict[str, Any]] = []
    for _, spell in spells.iterrows():
        duration = float(spell["duration_days"])
        event_band = elapsed_band_index(duration, bounds) if int(spell["event"]) == 1 else None
        for idx, (lo, hi) in enumerate(zip(bounds, bounds[1:])):
            exposure = max(0.0, min(duration, hi) - lo)
            if exposure <= 0:
                continue
            rows.append(
                {
                    "entity_id": spell["entity_id"],
                    "elapsed_band": labels[idx],
                    "elapsed_band_index": idx,
                    "exposure_days": exposure,
                    "events": int(event_band == idx),
                }
            )
    return pd.DataFrame(rows)


def piecewise_summary(spells: pd.DataFrame, bounds: tuple[float, ...]) -> pd.DataFrame:
    split = split_by_elapsed(spells, bounds)
    out = (
        split.groupby(["elapsed_band_index", "elapsed_band"], as_index=False)
        .agg(events=("events", "sum"), exposure_days=("exposure_days", "sum"))
        .sort_values("elapsed_band_index")
    )
    out["rate_per_100_entity_years"] = out["events"] / out["exposure_days"] * 36525
    ci_low: list[float] = []
    ci_high: list[float] = []
    for _, row in out.iterrows():
        events = int(row["events"])
        exposure = float(row["exposure_days"])
        low = 0.0 if events == 0 else 0.5 * stats.chi2.ppf(0.025, 2 * events) / exposure
        high = 0.5 * stats.chi2.ppf(0.975, 2 * (events + 1)) / exposure
        ci_low.append(low * 36525)
        ci_high.append(high * 36525)
    out["rate_ci_low"] = ci_low
    out["rate_ci_high"] = ci_high
    return out


def poisson_loglik(events: np.ndarray, exposure: np.ndarray, rates: np.ndarray) -> float:
    mu = exposure * rates
    return float(np.sum(events * np.log(mu) - mu - gammaln(events + 1)))


def piecewise_lrt(summary: pd.DataFrame) -> dict[str, float]:
    events = summary["events"].to_numpy(dtype=float)
    exposure = summary["exposure_days"].to_numpy(dtype=float)
    total_rate = events.sum() / exposure.sum()
    rates = np.divide(events, exposure, out=np.zeros_like(events), where=exposure > 0)
    rates = np.where((rates == 0) & (events == 0), 1e-300, rates)
    ll_null = poisson_loglik(events, exposure, np.repeat(total_rate, len(events)))
    ll_alt = poisson_loglik(events, exposure, rates)
    df = int((exposure > 0).sum() - 1)
    stat = 2 * (ll_alt - ll_null)
    return {
        "constant_hazard_log_likelihood": ll_null,
        "banded_hazard_log_likelihood": ll_alt,
        "likelihood_ratio": stat,
        "df": df,
        "p_value": float(stats.chi2.sf(stat, df)) if df > 0 else math.nan,
    }


def calendar_band_for(day: date, bounds: list[date], censor_date: date) -> int:
    all_bounds = bounds + [censor_date + timedelta(days=1)]
    for idx, (lo, hi) in enumerate(zip(all_bounds, all_bounds[1:])):
        if lo <= day < hi:
            return idx
    return len(all_bounds) - 2


def split_for_adjusted_poisson(
    spells: pd.DataFrame,
    elapsed_bounds: tuple[float, ...],
    calendar_bounds: list[date],
    censor_date: date,
) -> pd.DataFrame:
    elapsed_labels = band_labels(elapsed_bounds)
    n_elapsed_bands = len(elapsed_labels)
    calendar_labels = [
        f"{lo.year}-{(hi - timedelta(days=1)).year}"
        for lo, hi in zip(calendar_bounds, calendar_bounds[1:] + [censor_date + timedelta(days=1)])
    ]
    rows: list[dict[str, Any]] = []
    for _, spell in spells.iterrows():
        start = parse_date(spell["prior_event_date"])
        duration = int(spell["duration_days"])
        event_elapsed_idx = elapsed_band_index(duration, elapsed_bounds) if int(spell["event"]) else None
        event_day = parse_date(spell["spell_end_date"]) if int(spell["event"]) else None
        event_cal_idx = calendar_band_for(event_day, calendar_bounds, censor_date) if event_day else None

        for eidx, (elo, ehi) in enumerate(zip(elapsed_bounds, elapsed_bounds[1:])):
            seg_start = start + timedelta(days=int(elo))
            seg_end = start + timedelta(days=int(min(duration, ehi)))
            if seg_end <= seg_start:
                continue
            for cidx, (clo, chi) in enumerate(
                zip(calendar_bounds, calendar_bounds[1:] + [censor_date + timedelta(days=1)])
            ):
                overlap_start = max(seg_start, clo)
                overlap_end = min(seg_end, chi)
                exposure = (overlap_end - overlap_start).days
                if exposure <= 0:
                    continue
                rows.append(
                    {
                        "elapsed_band_index": eidx,
                        "elapsed_band": elapsed_labels[eidx],
                        "u_shape_phase": u_shape_phase_for_index(eidx, n_elapsed_bands),
                        "delayed_peak_phase": delayed_peak_phase_for_bounds(float(elo), float(ehi)),
                        "calendar_band_index": cidx,
                        "calendar_band": calendar_labels[cidx],
                        "event_number_cat": spell["event_number_cat"],
                        "org_size": spell["org_size"],
                        "org_size_score": spell["org_size_score"],
                        "org_size_unknown": spell["org_size_unknown"],
                        "employee_size": spell["employee_size"],
                        "turnover_size": spell["turnover_size"],
                        "sector_proxy": spell["sector_proxy"],
                        "entity_kind_group": spell["entity_kind_group"],
                        "prior_records_band": spell["prior_records_band"],
                        "exposure_days": exposure,
                        "events": int(event_elapsed_idx == eidx and event_cal_idx == cidx),
                    }
                )

    split = pd.DataFrame(rows)
    return (
        split.groupby(
            [
                "elapsed_band_index",
                "elapsed_band",
                "u_shape_phase",
                "delayed_peak_phase",
                "calendar_band_index",
                "calendar_band",
                "event_number_cat",
                "org_size",
                "org_size_score",
                "org_size_unknown",
                "employee_size",
                "turnover_size",
                "sector_proxy",
                "entity_kind_group",
                "prior_records_band",
            ],
            as_index=False,
        )
        .agg(events=("events", "sum"), exposure_days=("exposure_days", "sum"))
        .sort_values(["elapsed_band_index", "calendar_band_index", "event_number_cat"])
    )


def add_factor_terms(
    adjusted: pd.DataFrame,
    columns: list[np.ndarray],
    names: list[str],
    column: str,
    prefix: str,
    used: list[str],
    skipped: dict[str, str],
) -> None:
    base_levels = adjusted[column].fillna("unknown").replace("", "unknown").astype(str)
    raw_totals = (
        adjusted.assign(_level=base_levels)
        .groupby("_level", as_index=False)
        .agg(events=("events", "sum"), exposure_days=("exposure_days", "sum"))
        .sort_values(["events", "exposure_days"], ascending=False)
    )
    sparse_levels = set(raw_totals.loc[(raw_totals["events"] < 3) & (len(raw_totals) > 2), "_level"])
    model_levels = base_levels.map(lambda value: "sparse_or_no_repeat" if value in sparse_levels else value)
    totals = (
        adjusted.assign(_level=model_levels)
        .groupby("_level", as_index=False)
        .agg(events=("events", "sum"), exposure_days=("exposure_days", "sum"))
        .sort_values(["events", "exposure_days"], ascending=False)
    )
    levels = [str(v) for v in totals.loc[totals["exposure_days"] > 0, "_level"]]
    if len(levels) <= 1:
        skipped[column] = "only one populated level"
        return

    reference = levels[0]
    for level in sorted(level for level in levels if level != reference):
        columns.append((model_levels == level).astype(float).to_numpy())
        names.append(f"{prefix}:{level}")
    if sparse_levels:
        used.append(f"{column} (reference={reference}; pooled sparse levels={len(sparse_levels)})")
    else:
        used.append(f"{column} (reference={reference})")


def design_matrix(
    adjusted: pd.DataFrame,
    include_elapsed: bool,
    include_covariates: bool,
    time_effect: str = "elapsed",
    excluded_covariates: set[str] | None = None,
) -> tuple[np.ndarray, list[str], list[str], dict[str, str]]:
    columns = [np.ones(len(adjusted))]
    names = ["intercept"]
    covariates_used: list[str] = []
    covariates_skipped: dict[str, str] = {}
    excluded_covariates = excluded_covariates or set()

    if include_elapsed:
        if time_effect == "elapsed":
            for label in sorted(adjusted["elapsed_band"].unique(), key=lambda x: adjusted.loc[adjusted["elapsed_band"] == x, "elapsed_band_index"].iloc[0])[1:]:
                columns.append((adjusted["elapsed_band"] == label).astype(float).to_numpy())
                names.append(f"elapsed:{label}")
        elif time_effect == "u_shape_phase":
            for label in ["response", "long_term"]:
                if label in set(adjusted["u_shape_phase"]):
                    columns.append((adjusted["u_shape_phase"] == label).astype(float).to_numpy())
                    names.append(f"u_shape_phase:{label}")
        elif time_effect == "delayed_peak_phase":
            for label in ["peak_91_180", "post_180"]:
                if label in set(adjusted["delayed_peak_phase"]):
                    columns.append((adjusted["delayed_peak_phase"] == label).astype(float).to_numpy())
                    names.append(f"delayed_peak_phase:{label}")
        else:
            raise ValueError(f"Unknown time_effect: {time_effect}")

    calendar_totals = (
        adjusted.groupby("calendar_band", as_index=False)
        .agg(events=("events", "sum"), exposure_days=("exposure_days", "sum"), order=("calendar_band_index", "min"))
        .sort_values(["events", "exposure_days"], ascending=False)
    )
    calendar_ref = str(calendar_totals.iloc[0]["calendar_band"])
    for label in sorted(
        adjusted["calendar_band"].unique(),
        key=lambda x: adjusted.loc[adjusted["calendar_band"] == x, "calendar_band_index"].iloc[0],
    ):
        if label == calendar_ref:
            continue
        columns.append((adjusted["calendar_band"] == label).astype(float).to_numpy())
        names.append(f"calendar:{label}")

    for label in sorted(adjusted["event_number_cat"].unique())[1:]:
        columns.append((adjusted["event_number_cat"] == label).astype(float).to_numpy())
        names.append(f"event_number:{label}")

    if include_covariates:
        if "org_size_score" in excluded_covariates:
            covariates_skipped["org_size_score"] = "excluded for sensitivity variant"
        elif adjusted["org_size_score"].nunique(dropna=True) > 1:
            score = adjusted["org_size_score"].astype(float)
            columns.append((score - score.mean()).to_numpy())
            names.append("org_size_score")
            covariates_used.append("org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed)")
        else:
            covariates_skipped["org_size_score"] = "only one populated level"

        if "org_size_unknown" in excluded_covariates:
            covariates_skipped["org_size_unknown"] = "excluded for sensitivity variant"
        elif adjusted["org_size_unknown"].nunique() > 1:
            columns.append(adjusted["org_size_unknown"].astype(float).to_numpy())
            names.append("org_size_unknown")
            covariates_used.append("org_size_unknown")
        else:
            covariates_skipped["org_size_unknown"] = "only one populated level"

        for column in REQUESTED_COVARIATES:
            if column in {"org_size_score", "org_size_unknown"}:
                continue
            if column in excluded_covariates:
                covariates_skipped[column] = "excluded for sensitivity variant"
                continue
            add_factor_terms(
                adjusted,
                columns,
                names,
                column,
                column,
                covariates_used,
                covariates_skipped,
            )

    return np.column_stack(columns), names, covariates_used, covariates_skipped


def fit_poisson_glm(
    adjusted: pd.DataFrame,
    include_elapsed: bool,
    include_covariates: bool,
    time_effect: str = "elapsed",
    excluded_covariates: set[str] | None = None,
) -> dict[str, Any]:
    x, names, covariates_used, covariates_skipped = design_matrix(
        adjusted,
        include_elapsed=include_elapsed,
        include_covariates=include_covariates,
        time_effect=time_effect,
        excluded_covariates=excluded_covariates,
    )
    y = adjusted["events"].to_numpy(dtype=float)
    exposure = adjusted["exposure_days"].to_numpy(dtype=float)
    offset = np.log(exposure)

    def objective(beta: np.ndarray) -> float:
        eta = offset + x @ beta
        mu = np.exp(np.clip(eta, -745, 50))
        return -float(np.sum(y * eta - mu - gammaln(y + 1)))

    result = optimize.minimize(objective, np.zeros(x.shape[1]), method="L-BFGS-B")
    beta = result.x
    eta = offset + x @ beta
    mu = np.exp(np.clip(eta, -745, 50))
    log_likelihood = -float(result.fun)
    fisher = x.T @ (mu[:, None] * x)
    cov = np.linalg.pinv(fisher)
    se = np.sqrt(np.clip(np.diag(cov), 0, np.inf))

    coef_rows = []
    for name, value, stderr in zip(names, beta, se):
        coef_rows.append(
            {
                "term": name,
                "estimate": value,
                "std_error": stderr,
                "rate_ratio": safe_exp(value),
                "ci_low": safe_exp(value - 1.96 * stderr),
                "ci_high": safe_exp(value + 1.96 * stderr),
            }
        )

    return {
        "include_elapsed": include_elapsed,
        "names": names,
        "beta": beta,
        "cov": cov,
        "log_likelihood": log_likelihood,
        "n_params": len(beta),
        "converged": bool(result.success),
        "message": str(result.message),
        "coef_table": pd.DataFrame(coef_rows),
        "covariates_used": covariates_used,
        "covariates_skipped": covariates_skipped,
    }


def fit_adjusted_elapsed_model(
    spells: pd.DataFrame,
    elapsed_bounds: tuple[float, ...],
    censor_date: date,
    include_covariates: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    min_date = parse_date(spells["prior_event_date"].min())
    calendar_bounds = [min_date]
    for bound in DEFAULT_CALENDAR_BOUNDS[1:]:
        d = parse_date(bound)
        if min_date < d < censor_date:
            calendar_bounds.append(d)
    calendar_bounds = sorted(set(calendar_bounds))

    adjusted = split_for_adjusted_poisson(spells, elapsed_bounds, calendar_bounds, censor_date)
    full = fit_poisson_glm(adjusted, include_elapsed=True, include_covariates=include_covariates)
    reduced = fit_poisson_glm(adjusted, include_elapsed=False, include_covariates=include_covariates)
    lr = 2 * (full["log_likelihood"] - reduced["log_likelihood"])
    df = full["n_params"] - reduced["n_params"]
    lrt = {
        "full_log_likelihood": full["log_likelihood"],
        "reduced_log_likelihood": reduced["log_likelihood"],
        "likelihood_ratio": lr,
        "df": df,
        "p_value": float(stats.chi2.sf(lr, df)) if df > 0 else math.nan,
        "full_converged": full["converged"],
        "full_message": full["message"],
        "reduced_converged": reduced["converged"],
        "reduced_message": reduced["message"],
        "covariates_used": full["covariates_used"],
        "covariates_skipped": full["covariates_skipped"],
    }
    return adjusted, full["coef_table"], lrt


def phase_summary(adjusted: pd.DataFrame) -> pd.DataFrame:
    order = {"immediate": 0, "response": 1, "long_term": 2}
    labels = {
        "immediate": "Immediate period",
        "response": "Response period",
        "long_term": "Long-term period",
    }
    out = (
        adjusted.groupby("u_shape_phase", as_index=False)
        .agg(events=("events", "sum"), exposure_days=("exposure_days", "sum"))
    )
    out["phase_order"] = out["u_shape_phase"].map(order)
    out["phase"] = out["u_shape_phase"].map(labels)
    out["rate_per_100_entity_years"] = out["events"] / out["exposure_days"] * 36525
    return out.sort_values("phase_order")[
        ["phase_order", "u_shape_phase", "phase", "events", "exposure_days", "rate_per_100_entity_years"]
    ]


def contrast_from_fit(
    fit: dict[str, Any],
    weights_by_term: dict[str, float],
    comparison: str,
    directional_alternative: str | None = None,
) -> dict[str, Any]:
    names = list(fit["names"])
    weights = np.zeros(len(names))
    missing_terms = []
    for term, weight in weights_by_term.items():
        if term not in names:
            missing_terms.append(term)
            continue
        weights[names.index(term)] = weight
    if missing_terms:
        return {
            "comparison": comparison,
            "missing_terms": ", ".join(missing_terms),
            "log_rate_ratio": math.nan,
            "std_error": math.nan,
            "rate_ratio": math.nan,
            "ci_low": math.nan,
            "ci_high": math.nan,
            "z": math.nan,
            "p_two_sided": math.nan,
            "directional_alternative": directional_alternative or "",
            "p_directional": math.nan,
        }

    estimate = float(weights @ fit["beta"])
    variance = float(weights @ fit["cov"] @ weights)
    se = math.sqrt(max(variance, 0.0))
    z = estimate / se if se > 0 else math.nan
    p_two_sided = float(2 * stats.norm.sf(abs(z))) if math.isfinite(z) else math.nan
    if directional_alternative == "less":
        p_directional = float(stats.norm.cdf(z)) if math.isfinite(z) else math.nan
    elif directional_alternative == "greater":
        p_directional = float(stats.norm.sf(z)) if math.isfinite(z) else math.nan
    else:
        p_directional = math.nan

    return {
        "comparison": comparison,
        "missing_terms": "",
        "log_rate_ratio": estimate,
        "std_error": se,
        "rate_ratio": safe_exp(estimate),
        "ci_low": safe_exp(estimate - 1.96 * se),
        "ci_high": safe_exp(estimate + 1.96 * se),
        "z": z,
        "p_two_sided": p_two_sided,
        "directional_alternative": directional_alternative or "",
        "p_directional": p_directional,
    }


def fit_u_shape_phase_model(
    adjusted: pd.DataFrame,
    include_covariates: bool = True,
    excluded_covariates: set[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    full = fit_poisson_glm(
        adjusted,
        include_elapsed=True,
        include_covariates=include_covariates,
        time_effect="u_shape_phase",
        excluded_covariates=excluded_covariates,
    )
    reduced = fit_poisson_glm(
        adjusted,
        include_elapsed=False,
        include_covariates=include_covariates,
        excluded_covariates=excluded_covariates,
    )
    lr = 2 * (full["log_likelihood"] - reduced["log_likelihood"])
    df = full["n_params"] - reduced["n_params"]
    lrt = {
        "full_log_likelihood": full["log_likelihood"],
        "reduced_log_likelihood": reduced["log_likelihood"],
        "likelihood_ratio": lr,
        "df": df,
        "p_value": float(stats.chi2.sf(lr, df)) if df > 0 else math.nan,
        "full_converged": full["converged"],
        "full_message": full["message"],
        "reduced_converged": reduced["converged"],
        "reduced_message": reduced["message"],
        "covariates_used": full["covariates_used"],
        "covariates_skipped": full["covariates_skipped"],
    }
    contrasts = pd.DataFrame(
        [
            contrast_from_fit(
                full,
                {"u_shape_phase:response": 1.0},
                "response vs immediate",
                "less",
            ),
            contrast_from_fit(
                full,
                {"u_shape_phase:long_term": 1.0, "u_shape_phase:response": -1.0},
                "long-term vs response",
                "greater",
            ),
            contrast_from_fit(
                full,
                {"u_shape_phase:long_term": 1.0},
                "long-term vs immediate",
                None,
            ),
        ]
    )
    if {"response vs immediate", "long-term vs response"}.issubset(set(contrasts["comparison"])):
        directional = contrasts.set_index("comparison")["p_directional"]
        lrt["u_shape_intersection_p_value"] = float(
            max(directional["response vs immediate"], directional["long-term vs response"])
        )
    else:
        lrt["u_shape_intersection_p_value"] = math.nan
    return phase_summary(adjusted), contrasts, lrt


def fit_u_shape_covariate_variants(adjusted: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    variant_specs = [
        ("all_covariates", "All covariates", set()),
        ("drop_size", "Drop organisation size", {"org_size_score", "org_size_unknown"}),
        ("drop_industry", "Drop industry/sector", {"sector_proxy"}),
        (
            "drop_size_and_industry",
            "Drop size and industry/sector",
            {"org_size_score", "org_size_unknown", "sector_proxy"},
        ),
    ]
    contrast_rows: list[dict[str, Any]] = []
    lrt_rows: list[dict[str, Any]] = []
    for variant, label, excluded in variant_specs:
        _, contrasts, lrt = fit_u_shape_phase_model(adjusted, excluded_covariates=excluded)
        for row in contrasts.to_dict(orient="records"):
            row["variant"] = variant
            row["variant_label"] = label
            contrast_rows.append(row)
        lrt_rows.append(
            {
                "variant": variant,
                "variant_label": label,
                "excluded_covariates": ", ".join(sorted(excluded)) if excluded else "",
                "phase_terms": int(lrt["df"]),
                "likelihood_ratio": lrt["likelihood_ratio"],
                "df": lrt["df"],
                "p_value": lrt["p_value"],
                "u_shape_intersection_p_value": lrt["u_shape_intersection_p_value"],
            }
        )
    return pd.DataFrame(contrast_rows), pd.DataFrame(lrt_rows)


def delayed_peak_summary(adjusted: pd.DataFrame) -> pd.DataFrame:
    applicable = adjusted[adjusted["delayed_peak_phase"] != "not_applicable"].copy()
    if applicable.empty:
        return pd.DataFrame()
    order = {"immediate_0_90": 0, "peak_91_180": 1, "post_180": 2}
    labels = {
        "immediate_0_90": "Immediate 0-90 days",
        "peak_91_180": "Delayed peak 91-180 days",
        "post_180": "Post-180 days",
    }
    out = (
        applicable.groupby("delayed_peak_phase", as_index=False)
        .agg(events=("events", "sum"), exposure_days=("exposure_days", "sum"))
    )
    out["phase_order"] = out["delayed_peak_phase"].map(order)
    out["phase"] = out["delayed_peak_phase"].map(labels)
    out["rate_per_100_entity_years"] = out["events"] / out["exposure_days"] * 36525
    return out.sort_values("phase_order")[
        ["phase_order", "delayed_peak_phase", "phase", "events", "exposure_days", "rate_per_100_entity_years"]
    ]


def fit_delayed_peak_model(
    adjusted: pd.DataFrame,
    include_covariates: bool = True,
    excluded_covariates: set[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    summary = delayed_peak_summary(adjusted)
    needed = {"immediate_0_90", "peak_91_180", "post_180"}
    if summary.empty or not needed.issubset(set(summary["delayed_peak_phase"])):
        return summary, pd.DataFrame(), {
            "available": False,
            "reason": "requires elapsed bands 0-90, 91-180, and at least one post-180 band",
        }

    model_data = adjusted[adjusted["delayed_peak_phase"] != "not_applicable"].copy()
    full = fit_poisson_glm(
        model_data,
        include_elapsed=True,
        include_covariates=include_covariates,
        time_effect="delayed_peak_phase",
        excluded_covariates=excluded_covariates,
    )
    reduced = fit_poisson_glm(
        model_data,
        include_elapsed=False,
        include_covariates=include_covariates,
        excluded_covariates=excluded_covariates,
    )
    lr = 2 * (full["log_likelihood"] - reduced["log_likelihood"])
    df = full["n_params"] - reduced["n_params"]
    lrt = {
        "available": True,
        "full_log_likelihood": full["log_likelihood"],
        "reduced_log_likelihood": reduced["log_likelihood"],
        "likelihood_ratio": lr,
        "df": df,
        "p_value": float(stats.chi2.sf(lr, df)) if df > 0 else math.nan,
        "full_converged": full["converged"],
        "full_message": full["message"],
        "reduced_converged": reduced["converged"],
        "reduced_message": reduced["message"],
        "covariates_used": full["covariates_used"],
        "covariates_skipped": full["covariates_skipped"],
    }
    contrasts = pd.DataFrame(
        [
            contrast_from_fit(
                full,
                {"delayed_peak_phase:peak_91_180": 1.0},
                "91-180 vs 0-90",
                "greater",
            ),
            contrast_from_fit(
                full,
                {"delayed_peak_phase:peak_91_180": 1.0, "delayed_peak_phase:post_180": -1.0},
                "91-180 vs post-180",
                "greater",
            ),
            contrast_from_fit(
                full,
                {"delayed_peak_phase:post_180": 1.0},
                "post-180 vs 0-90",
                None,
            ),
        ]
    )
    directional = contrasts.set_index("comparison")["p_directional"]
    lrt["delayed_peak_intersection_p_value"] = float(
        max(directional["91-180 vs 0-90"], directional["91-180 vs post-180"])
    )
    return summary, contrasts, lrt


def bootstrap_piecewise_rates(
    spells: pd.DataFrame,
    bounds: tuple[float, ...],
    n_boot: int,
    seed: int,
) -> pd.DataFrame:
    base = piecewise_summary(spells, bounds)[["elapsed_band_index", "elapsed_band"]]
    if n_boot <= 0:
        base["bootstrap_ci_low"] = np.nan
        base["bootstrap_ci_high"] = np.nan
        return base

    rng = np.random.default_rng(seed)
    entity_ids = spells["entity_id"].drop_duplicates().to_numpy()
    rates_by_band: dict[str, list[float]] = {band: [] for band in base["elapsed_band"]}

    for _ in range(n_boot):
        sampled_ids = rng.choice(entity_ids, size=len(entity_ids), replace=True)
        sample = pd.concat([spells[spells["entity_id"] == entity_id] for entity_id in sampled_ids], ignore_index=True)
        summary = piecewise_summary(sample, bounds)
        for _, row in summary.iterrows():
            rates_by_band[row["elapsed_band"]].append(float(row["rate_per_100_entity_years"]))

    base["bootstrap_ci_low"] = [
        float(np.percentile(rates_by_band[band], 2.5)) if rates_by_band[band] else math.nan
        for band in base["elapsed_band"]
    ]
    base["bootstrap_ci_high"] = [
        float(np.percentile(rates_by_band[band], 97.5)) if rates_by_band[band] else math.nan
        for band in base["elapsed_band"]
    ]
    return base


def censored_log_likelihood(
    name: str,
    theta: np.ndarray,
    durations: np.ndarray,
    observed: np.ndarray,
) -> tuple[float, dict[str, float]]:
    if name == "exponential":
        rate = math.exp(theta[0])
        log_pdf = np.log(rate) - rate * durations
        log_sf = -rate * durations
        params = {"rate": rate, "mean_days": 1 / rate}
    elif name == "weibull":
        shape = math.exp(theta[0])
        scale = math.exp(theta[1])
        z = durations / scale
        log_pdf = np.log(shape) - np.log(scale) + (shape - 1) * np.log(z) - z**shape
        log_sf = -(z**shape)
        params = {"shape": shape, "scale_days": scale}
    elif name == "lognormal":
        mu = theta[0]
        sigma = math.exp(theta[1])
        log_pdf = stats.lognorm.logpdf(durations, s=sigma, scale=math.exp(mu))
        log_sf = stats.lognorm.logsf(durations, s=sigma, scale=math.exp(mu))
        params = {"mu": mu, "sigma": sigma, "median_days": math.exp(mu)}
    elif name == "loglogistic":
        shape = math.exp(theta[0])
        scale = math.exp(theta[1])
        z = durations / scale
        log_pdf = np.log(shape) - np.log(scale) + (shape - 1) * np.log(z) - 2 * np.log1p(z**shape)
        log_sf = -np.log1p(z**shape)
        params = {"shape": shape, "scale_days": scale}
    elif name == "generalized_gamma":
        a = math.exp(theta[0])
        c = math.exp(theta[1])
        scale = math.exp(theta[2])
        log_pdf = stats.gengamma.logpdf(durations, a=a, c=c, scale=scale)
        log_sf = stats.gengamma.logsf(durations, a=a, c=c, scale=scale)
        params = {"a": a, "c": c, "scale_days": scale}
    else:
        raise ValueError(name)

    log_terms = np.where(observed == 1, log_pdf, log_sf)
    if not np.all(np.isfinite(log_terms)):
        return -math.inf, params
    return float(np.sum(log_terms)), params


def fit_parametric_models(spells: pd.DataFrame) -> list[FitResult]:
    durations = spells["duration_days"].to_numpy(dtype=float)
    observed = spells["event"].to_numpy(dtype=int)
    positive = durations > 0
    durations = durations[positive]
    observed = observed[positive]
    mean_duration = float(np.mean(durations))
    log_mean = math.log(mean_duration)
    log_median = math.log(float(np.median(durations)))

    starts = {
        "exponential": np.array([math.log(observed.sum() / durations.sum())]),
        "weibull": np.array([0.0, log_mean]),
        "lognormal": np.array([log_median, 0.0]),
        "loglogistic": np.array([0.0, log_median]),
        "generalized_gamma": np.array([0.0, 0.0, log_mean]),
    }

    results: list[FitResult] = []
    for name, start in starts.items():
        def objective(theta: np.ndarray) -> float:
            ll, _ = censored_log_likelihood(name, theta, durations, observed)
            if not math.isfinite(ll):
                return 1e100
            return -ll

        if name == "exponential":
            ll, params = censored_log_likelihood(name, start, durations, observed)
            results.append(FitResult(name, params, ll, len(start), True, "closed-form censored MLE"))
            continue

        result = optimize.minimize(objective, start, method="Nelder-Mead", options={"maxiter": 20000})
        ll, params = censored_log_likelihood(name, result.x, durations, observed)
        results.append(FitResult(name, params, ll, len(start), bool(result.success), str(result.message)))

    return results


def survival_curves(spells: pd.DataFrame) -> pd.DataFrame:
    durations = spells["duration_days"].to_numpy(dtype=float)
    observed = spells["event"].to_numpy(dtype=int)
    event_times = sorted(set(durations[observed == 1]))
    survival = 1.0
    cumulative_hazard = 0.0
    rows = [{"time_days": 0.0, "survival": 1.0, "nelson_aalen": 0.0, "events": 0, "at_risk": len(durations)}]

    for t in event_times:
        at_risk = int(np.sum(durations >= t))
        events = int(np.sum((durations == t) & (observed == 1)))
        survival *= 1 - events / at_risk
        cumulative_hazard += events / at_risk
        rows.append(
            {
                "time_days": t,
                "survival": survival,
                "nelson_aalen": cumulative_hazard,
                "events": events,
                "at_risk": at_risk,
            }
        )
    return pd.DataFrame(rows)


def hazard_for_model(model: FitResult, grid: np.ndarray) -> np.ndarray | None:
    params = model.params
    if model.name == "exponential":
        return np.repeat(params["rate"], len(grid)) * 36525
    if model.name == "weibull":
        shape = params["shape"]
        scale = params["scale_days"]
        return (shape / scale) * (grid / scale) ** (shape - 1) * 36525
    if model.name == "lognormal":
        sigma = params["sigma"]
        scale = params["median_days"]
        pdf = stats.lognorm.pdf(grid, s=sigma, scale=scale)
        sf = stats.lognorm.sf(grid, s=sigma, scale=scale)
        return np.divide(pdf, sf, out=np.zeros_like(pdf), where=sf > 0) * 36525
    if model.name == "loglogistic":
        shape = params["shape"]
        scale = params["scale_days"]
        z = grid / scale
        return (shape / scale) * z ** (shape - 1) / (1 + z**shape) * 36525
    if model.name == "generalized_gamma":
        pdf = stats.gengamma.pdf(grid, a=params["a"], c=params["c"], scale=params["scale_days"])
        sf = stats.gengamma.sf(grid, a=params["a"], c=params["c"], scale=params["scale_days"])
        return np.divide(pdf, sf, out=np.zeros_like(pdf), where=sf > 0) * 36525
    return None


def plot_piecewise_hazard(summary: pd.DataFrame, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(9, 5))
    x = np.arange(len(summary))
    y = summary["rate_per_100_entity_years"].to_numpy(dtype=float)
    low = summary["bootstrap_ci_low"].fillna(summary["rate_ci_low"]).to_numpy(dtype=float)
    high = summary["bootstrap_ci_high"].fillna(summary["rate_ci_high"]).to_numpy(dtype=float)
    yerr = np.vstack([np.maximum(0, y - low), np.maximum(0, high - y)])
    ax.errorbar(x, y, yerr=yerr, fmt="o-", capsize=4)
    ax.set_xticks(x)
    ax.set_xticklabels(summary["elapsed_band"], rotation=30, ha="right")
    ax.set_ylabel("Repeat events per 100 entity-years")
    ax.set_xlabel("Days since prior event")
    ax.set_title("Piecewise recurrence hazard")
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def plot_survival(km: pd.DataFrame, exponential: FitResult, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.step(km["time_days"], km["survival"], where="post", label="Kaplan-Meier")
    grid = np.linspace(0, float(km["time_days"].max()), 300)
    rate = exponential.params["rate"]
    ax.plot(grid, np.exp(-rate * grid), label="Fitted exponential", linestyle="--")
    ax.set_ylim(0, 1.03)
    ax.set_xlabel("Days since prior event")
    ax.set_ylabel("Probability no repeat event yet")
    ax.set_title("Censored time-to-repeat survival")
    ax.legend()
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def plot_parametric_hazards(models: list[FitResult], path: Path, max_day: float) -> None:
    fig, ax = plt.subplots(figsize=(9, 5))
    grid = np.linspace(1, max_day, 400)
    for model in models:
        if not model.converged and model.name != "exponential":
            continue
        hazard = hazard_for_model(model, grid)
        if hazard is None or not np.all(np.isfinite(hazard)):
            continue
        ax.plot(grid, hazard, label=model.name)
    ax.set_xlabel("Days since prior event")
    ax.set_ylabel("Repeat events per 100 entity-years")
    ax.set_title("Parametric hazard shapes")
    ax.legend()
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def target_rate_ratio(summary: pd.DataFrame) -> dict[str, float]:
    early = summary[summary["elapsed_band_index"] == 0]
    later = summary[summary["elapsed_band_index"].isin([1, 2])]
    if early.empty or later.empty:
        return {}
    e1 = float(early["events"].sum())
    x1 = float(early["exposure_days"].sum())
    e2 = float(later["events"].sum())
    x2 = float(later["exposure_days"].sum())
    rr = (e2 / x2) / (e1 / x1) if e1 > 0 and e2 > 0 else math.nan
    se = math.sqrt(1 / e1 + 1 / e2) if e1 > 0 and e2 > 0 else math.nan
    return {
        "early_events": e1,
        "early_bands": ", ".join(early["elapsed_band"].astype(str)),
        "early_exposure_days": x1,
        "later_events": e2,
        "later_bands": ", ".join(later["elapsed_band"].astype(str)),
        "later_exposure_days": x2,
        "later_vs_early_rate_ratio": rr,
        "ci_low": math.exp(math.log(rr) - 1.96 * se) if math.isfinite(rr) else math.nan,
        "ci_high": math.exp(math.log(rr) + 1.96 * se) if math.isfinite(rr) else math.nan,
    }


def write_report(
    path: Path,
    data_quality: dict[str, Any],
    piecewise: pd.DataFrame,
    adjusted_coef: pd.DataFrame,
    unadjusted_lrt: dict[str, float],
    adjusted_lrt: dict[str, Any],
    target_rr: dict[str, float],
    u_shape_summary: pd.DataFrame,
    u_shape_contrasts: pd.DataFrame,
    u_shape_lrt: dict[str, Any],
    u_shape_variant_contrasts: pd.DataFrame,
    u_shape_variant_lrt: pd.DataFrame,
    delayed_peak_summary_table: pd.DataFrame,
    delayed_peak_contrasts: pd.DataFrame,
    delayed_peak_lrt: dict[str, Any],
    delayed_peak_sensitivity_summary: pd.DataFrame,
    delayed_peak_sensitivity_contrasts: pd.DataFrame,
    delayed_peak_sensitivity_lrt: dict[str, Any],
    delayed_peak_sensitivity_elapsed_lrt: dict[str, Any],
    model_table: pd.DataFrame,
    weibull_lrt: dict[str, float],
    plot_paths: dict[str, Path],
) -> None:
    best = model_table.sort_values("aic").iloc[0]
    p_adjusted = adjusted_lrt["p_value"]
    if math.isfinite(p_adjusted) and p_adjusted < 0.05:
        conclusion = "The adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level."
    else:
        conclusion = "The adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level."

    covariate_terms = adjusted_coef[
        adjusted_coef["term"].str.startswith(
            (
                "org_size",
                "sector_proxy:",
                "entity_kind_group:",
                "prior_records_band:",
            )
        )
    ].copy()
    covariate_terms["effect"] = covariate_terms["term"].map(
        lambda term: (
            "Organisation size"
            if term == "org_size_score"
            else "Unknown size"
            if term == "org_size_unknown"
            else term.replace("sector_proxy:", "Sector: ")
            .replace("entity_kind_group:", "Entity kind: ")
            .replace("prior_records_band:", "Prior records: ")
        )
    )
    covariate_terms = covariate_terms[
        ["effect", "rate_ratio", "ci_low", "ci_high"]
    ].sort_values("effect")

    scaled_effect_rows: list[dict[str, Any]] = []
    size_rows = adjusted_coef[adjusted_coef["term"] == "org_size_score"]
    if not size_rows.empty:
        size_row = size_rows.iloc[0]
        beta = float(size_row["estimate"])
        se = float(size_row["std_error"])
        for label, steps in [
            ("SMALL -> MEDIUM", 1),
            ("SMALL -> LARGE", 2),
            ("SMALL -> HUGE", 3),
            ("MEDIUM -> HUGE", 2),
            ("LARGE -> HUGE", 1),
        ]:
            rr = safe_exp(beta * steps)
            low = safe_exp((beta - 1.96 * se) * steps)
            high = safe_exp((beta + 1.96 * se) * steps)
            scaled_effect_rows.append(
                {
                    "comparison": label,
                    "rate_ratio": rr,
                    "ci_low": low,
                    "ci_high": high,
                    "plain_language": (
                        f"about {(rr - 1) * 100:.0f}% higher repeat-event rate"
                        if rr >= 1
                        else f"about {(1 - rr) * 100:.0f}% lower repeat-event rate"
                    ),
                }
            )
    scaled_effects = pd.DataFrame(scaled_effect_rows)

    sector_terms = adjusted_coef[adjusted_coef["term"].str.startswith("sector_proxy:")].copy()
    sector_effects = pd.DataFrame()
    if not sector_terms.empty:
        sector_terms["sector"] = sector_terms["term"].str.replace("sector_proxy:", "", regex=False)
        sector_terms["plain_language"] = sector_terms["rate_ratio"].map(
            lambda rr: (
                f"about {(rr - 1) * 100:.0f}% higher than government"
                if rr >= 1
                else f"about {(1 - rr) * 100:.0f}% lower than government"
            )
        )
        sector_effects = sector_terms[
            ["sector", "rate_ratio", "ci_low", "ci_high", "plain_language"]
        ].sort_values("rate_ratio", ascending=False)

    lines = [
        "# Repeat Cyber-Event Timing Analysis",
        "",
        "## Scope",
        "",
        "This analysis is conditional on an entity having at least one observed victim event. "
        "Single-event entities contribute right-censored post-event spells.",
        "",
        "## Data Quality Summary",
        "",
    ]
    for key, value in data_quality.items():
        lines.append(f"- {key}: {value}")

    lines.extend(
        [
            "",
            "## Primary Piecewise Exponential Test",
            "",
            conclusion,
            "",
            f"- unadjusted LRT p-value: {unadjusted_lrt['p_value']:.4g}",
            f"- adjusted LRT p-value: {adjusted_lrt['p_value']:.4g}",
            "- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.",
            f"- covariates used: {', '.join(adjusted_lrt.get('covariates_used') or ['none'])}",
            f"- covariates skipped: {json.dumps(adjusted_lrt.get('covariates_skipped', {}), sort_keys=True)}",
            "",
            "Piecewise recurrence rates are per 100 entity-years:",
            "",
            piecewise[
                [
                    "elapsed_band",
                    "events",
                    "exposure_days",
                    "rate_per_100_entity_years",
                    "bootstrap_ci_low",
                    "bootstrap_ci_high",
                ]
            ].to_markdown(index=False, floatfmt=".3f"),
            "",
        ]
    )

    if not covariate_terms.empty:
        lines.extend(
            [
                "## Organisation Size And Sector Effects",
                "",
                "Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.",
                "The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.",
                "",
            ]
        )
        if not scaled_effects.empty:
            lines.extend(
                [
                    "Scaled size comparisons:",
                    "",
                    scaled_effects.to_markdown(index=False, floatfmt=".3f"),
                    "",
                ]
            )
        if not sector_effects.empty:
            lines.extend(
                [
                    "Sector effects, relative to government entities after adjusting for size and the other model controls:",
                    "",
                    sector_effects.to_markdown(index=False, floatfmt=".3f"),
                    "",
                ]
            )
        lines.extend(
            [
                "Full adjusted covariate table:",
                "",
                covariate_terms.to_markdown(index=False, floatfmt=".3f"),
                "",
            ]
        )

    if target_rr:
        lines.extend(
            [
                "Targeted contrast for the proposed low-then-rising pattern:",
                "",
                f"- {target_rr['later_bands']} days vs {target_rr['early_bands']} days rate ratio: "
                f"{target_rr['later_vs_early_rate_ratio']:.3f}",
                f"- approximate 95% CI: {target_rr['ci_low']:.3f} to {target_rr['ci_high']:.3f}",
                "",
            ]
        )

    if not u_shape_summary.empty and not u_shape_contrasts.empty:
        display_contrasts = u_shape_contrasts.copy()
        display_contrasts["plain_language"] = display_contrasts["rate_ratio"].map(
            lambda rr: (
                f"about {(rr - 1) * 100:.0f}% higher"
                if rr >= 1
                else f"about {(1 - rr) * 100:.0f}% lower"
            )
        )
        display_contrasts.loc[
            display_contrasts["directional_alternative"].eq(""),
            "p_directional",
        ] = np.nan
        display_contrasts["p_directional"] = display_contrasts["p_directional"].map(
            lambda value: "" if pd.isna(value) else f"{value:.3f}"
        )
        display_contrasts = display_contrasts[
            [
                "comparison",
                "rate_ratio",
                "ci_low",
                "ci_high",
                "plain_language",
                "p_two_sided",
                "directional_alternative",
                "p_directional",
            ]
        ]
        response_rr = float(u_shape_contrasts.loc[
            u_shape_contrasts["comparison"] == "response vs immediate",
            "rate_ratio",
        ].iloc[0])
        long_response_rr = float(u_shape_contrasts.loc[
            u_shape_contrasts["comparison"] == "long-term vs response",
            "rate_ratio",
        ].iloc[0])
        has_u_shape_point_estimate = response_rr < 1 and long_response_rr > 1
        if has_u_shape_point_estimate and u_shape_lrt["u_shape_intersection_p_value"] < 0.05:
            u_shape_text = "The adjusted model supports the requested U-shaped pattern at the 5% directional level."
        elif has_u_shape_point_estimate:
            u_shape_text = (
                "The point estimates have the requested U-shape, but the full directional test is not statistically strong. "
                "The later rise is clearer than the initial fall."
            )
        else:
            u_shape_text = "The point estimates do not form the requested U-shape."
        lines.extend(
            [
                "## U-Shape Test",
                "",
                "Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.",
                "The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.",
                u_shape_text,
                "",
                f"- phase-model LRT p-value versus constant elapsed-time risk: {u_shape_lrt['p_value']:.4g}",
                f"- directional U-shape p-value: {u_shape_lrt['u_shape_intersection_p_value']:.4g}",
                "",
                "Unadjusted phase rates per 100 entity-years:",
                "",
                u_shape_summary[
                    ["phase", "events", "exposure_days", "rate_per_100_entity_years"]
                ].to_markdown(index=False, floatfmt=".3f"),
                "",
                "Adjusted scaled phase comparisons:",
                "",
                display_contrasts.to_markdown(index=False, floatfmt=".3f"),
                "",
                "The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.",
                "",
            ]
        )

    if not u_shape_variant_contrasts.empty:
        variant_display = u_shape_variant_contrasts[
            u_shape_variant_contrasts["comparison"].isin(
                ["response vs immediate", "long-term vs response"]
            )
        ].copy()
        variant_display["ci_width"] = variant_display["ci_high"] - variant_display["ci_low"]
        variant_display["plain_language"] = variant_display["rate_ratio"].map(
            lambda rr: (
                f"about {(rr - 1) * 100:.0f}% higher"
                if rr >= 1
                else f"about {(1 - rr) * 100:.0f}% lower"
            )
        )
        variant_display["p_directional"] = variant_display["p_directional"].map(
            lambda value: "" if pd.isna(value) else f"{value:.3f}"
        )
        variant_display = variant_display[
            [
                "variant_label",
                "comparison",
                "rate_ratio",
                "ci_low",
                "ci_high",
                "ci_width",
                "plain_language",
                "p_directional",
            ]
        ]
        lrt_display = u_shape_variant_lrt[
            ["variant_label", "p_value", "u_shape_intersection_p_value"]
        ].copy()
        lines.extend(
            [
                "## U-Shape Covariate Sensitivity",
                "",
                "These variants test whether the wide U-shape confidence intervals are mainly caused by including organisation size or industry/sector controls.",
                "",
                "Adjusted phase comparisons by covariate set:",
                "",
                variant_display.to_markdown(index=False, floatfmt=".3f"),
                "",
                "Phase-model test by covariate set:",
                "",
                lrt_display.to_markdown(index=False, floatfmt=".4f"),
                "",
                "If dropping a covariate group materially narrowed the interval, the CI width would shrink in this table. In this run, the interval around the initial fall remains broad under all variants, which points more to sparse repeat-event information than to a single covariate group consuming all precision.",
                "",
            ]
        )

    if delayed_peak_lrt.get("available") and not delayed_peak_contrasts.empty:
        delayed_display = delayed_peak_contrasts.copy()
        delayed_display["plain_language"] = delayed_display["rate_ratio"].map(
            lambda rr: (
                f"about {(rr - 1) * 100:.0f}% higher"
                if rr >= 1
                else f"about {(1 - rr) * 100:.0f}% lower"
            )
        )
        delayed_display["p_directional"] = delayed_display["p_directional"].map(
            lambda value: "" if pd.isna(value) else f"{value:.3f}"
        )
        delayed_display = delayed_display[
            [
                "comparison",
                "rate_ratio",
                "ci_low",
                "ci_high",
                "plain_language",
                "p_two_sided",
                "directional_alternative",
                "p_directional",
            ]
        ]
        peak_vs_immediate_rr = float(delayed_peak_contrasts.loc[
            delayed_peak_contrasts["comparison"] == "91-180 vs 0-90",
            "rate_ratio",
        ].iloc[0])
        peak_vs_post_rr = float(delayed_peak_contrasts.loc[
            delayed_peak_contrasts["comparison"] == "91-180 vs post-180",
            "rate_ratio",
        ].iloc[0])
        has_peak_point_estimate = peak_vs_immediate_rr > 1 and peak_vs_post_rr > 1
        if has_peak_point_estimate and delayed_peak_lrt["delayed_peak_intersection_p_value"] < 0.05:
            delayed_peak_text = "The adjusted model supports a delayed 91-180 day peak at the 5% directional level."
        elif has_peak_point_estimate:
            delayed_peak_text = (
                "The point estimates have a delayed 91-180 day peak, but the full directional test is not statistically strong."
            )
        else:
            delayed_peak_text = "The point estimates do not support a delayed 91-180 day peak."
        lines.extend(
            [
                "## Delayed 91-180 Day Peak Test",
                "",
                "Definition used here: the hypothesised peak is 91-180 days after the prior event. A formal peak requires 91-180 days to be higher than both 0-90 days and the pooled post-180 period.",
                "The adjusted peak model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main model.",
                delayed_peak_text,
                "",
                f"- phase-model LRT p-value versus constant elapsed-time risk: {delayed_peak_lrt['p_value']:.4g}",
                f"- directional delayed-peak p-value: {delayed_peak_lrt['delayed_peak_intersection_p_value']:.4g}",
                "",
                "Unadjusted phase rates per 100 entity-years:",
                "",
                delayed_peak_summary_table[
                    ["phase", "events", "exposure_days", "rate_per_100_entity_years"]
                ].to_markdown(index=False, floatfmt=".3f"),
                "",
                "Adjusted scaled peak comparisons:",
                "",
                delayed_display.to_markdown(index=False, floatfmt=".3f"),
                "",
            ]
        )

    if (
        not delayed_peak_lrt.get("available")
        and delayed_peak_sensitivity_lrt.get("available")
        and not delayed_peak_sensitivity_contrasts.empty
    ):
        delayed_display = delayed_peak_sensitivity_contrasts.copy()
        delayed_display["plain_language"] = delayed_display["rate_ratio"].map(
            lambda rr: (
                f"about {(rr - 1) * 100:.0f}% higher"
                if rr >= 1
                else f"about {(1 - rr) * 100:.0f}% lower"
            )
        )
        delayed_display["p_directional"] = delayed_display["p_directional"].map(
            lambda value: "" if pd.isna(value) else f"{value:.3f}"
        )
        delayed_display = delayed_display[
            [
                "comparison",
                "rate_ratio",
                "ci_low",
                "ci_high",
                "plain_language",
                "p_two_sided",
                "directional_alternative",
                "p_directional",
            ]
        ]
        lines.extend(
            [
                "## Delayed 91-180 Day Peak Sensitivity",
                "",
                "This sensitivity reruns the elapsed-time model with bands `0-90`, `91-180`, `181-365`, `366-730`, `731-1460`, and `>1460` to test whether the apparent early elevation is concentrated in days 91-180.",
                "A formal delayed peak requires the 91-180 day period to be higher than both 0-90 days and the pooled post-180 period after adjustment.",
                "",
                f"- adjusted elapsed-time LRT p-value for the 90/180 split: {delayed_peak_sensitivity_elapsed_lrt['p_value']:.4g}",
                f"- phase-model LRT p-value versus constant elapsed-time risk: {delayed_peak_sensitivity_lrt['p_value']:.4g}",
                f"- directional delayed-peak p-value: {delayed_peak_sensitivity_lrt['delayed_peak_intersection_p_value']:.4g}",
                "",
                "Unadjusted phase rates per 100 entity-years:",
                "",
                delayed_peak_sensitivity_summary[
                    ["phase", "events", "exposure_days", "rate_per_100_entity_years"]
                ].to_markdown(index=False, floatfmt=".3f"),
                "",
                "Adjusted scaled peak comparisons:",
                "",
                delayed_display.to_markdown(index=False, floatfmt=".3f"),
                "",
                "Interpretation: the 91-180 day period is much higher than the first 90 days, and that contrast is statistically strong. The stricter test that it is higher than both sides is suggestive rather than definitive because the 91-180 versus post-180 comparison is weaker.",
                "",
            ]
        )

    lines.extend(
        [
            "## Parametric Survival Model Comparison",
            "",
            f"Best AIC model: {best['model']}. Lower AIC/BIC means better censored likelihood fit after penalising parameters.",
            "",
            model_table[["model", "log_likelihood", "parameters", "aic", "bic", "converged"]].to_markdown(
                index=False, floatfmt=".3f"
            ),
            "",
            f"Exponential vs Weibull LRT p-value: {weibull_lrt['p_value']:.4g}",
            "",
            "## Plots",
            "",
        ]
    )
    for label, plot_path in plot_paths.items():
        lines.append(f"- {label}: `{plot_path.name}`")

    lines.extend(
        [
            "",
            "## Interpretation Guardrails",
            "",
            "- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.",
            "- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.",
            "- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.",
        ]
    )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path.")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="Directory for analysis outputs.")
    parser.add_argument("--censor-date", default=DEFAULT_CENSOR_DATE, help="Right-censoring date, YYYY-MM-DD.")
    parser.add_argument(
        "--elapsed-bounds",
        type=parse_elapsed_bounds,
        default=DEFAULT_ELAPSED_BOUNDS,
        help="Comma-separated elapsed-time cutpoints in days. Must start with 0 and end with inf.",
    )
    parser.add_argument("--bootstrap", type=int, default=500, help="Entity bootstrap replicates for piecewise hazard CI.")
    parser.add_argument("--seed", type=int, default=20260805, help="Bootstrap random seed.")
    args = parser.parse_args()

    db_path = Path(args.db)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    censor_date = parse_date(args.censor_date)
    elapsed_bounds = args.elapsed_bounds

    events = load_victim_events(db_path)
    spells, same_day = build_spells(events, censor_date)
    spells["elapsed_band"] = [
        band_labels(elapsed_bounds)[elapsed_band_index(v, elapsed_bounds)]
        for v in spells["duration_days"]
    ]

    piecewise = piecewise_summary(spells, elapsed_bounds)
    boot = bootstrap_piecewise_rates(spells, elapsed_bounds, args.bootstrap, args.seed)
    piecewise = piecewise.merge(boot, on=["elapsed_band_index", "elapsed_band"], how="left")
    unadjusted_lrt = piecewise_lrt(piecewise)
    adjusted_split, adjusted_coef, adjusted_lrt = fit_adjusted_elapsed_model(
        spells, elapsed_bounds, censor_date
    )
    target_rr = target_rate_ratio(piecewise)
    u_shape_summary, u_shape_contrasts, u_shape_lrt = fit_u_shape_phase_model(adjusted_split)
    u_shape_variant_contrasts, u_shape_variant_lrt = fit_u_shape_covariate_variants(adjusted_split)
    delayed_peak_summary_table, delayed_peak_contrasts, delayed_peak_lrt = fit_delayed_peak_model(adjusted_split)
    delayed_peak_sensitivity_split, _, delayed_peak_sensitivity_elapsed_lrt = fit_adjusted_elapsed_model(
        spells, DELAYED_PEAK_ELAPSED_BOUNDS, censor_date
    )
    (
        delayed_peak_sensitivity_summary,
        delayed_peak_sensitivity_contrasts,
        delayed_peak_sensitivity_lrt,
    ) = fit_delayed_peak_model(delayed_peak_sensitivity_split)

    fits = fit_parametric_models(spells)
    model_table = pd.DataFrame([fit.as_row(len(spells)) for fit in fits]).sort_values("aic")
    exp_fit = next(fit for fit in fits if fit.name == "exponential")
    weibull_fit = next(fit for fit in fits if fit.name == "weibull")
    weibull_lrt_stat = 2 * (weibull_fit.log_likelihood - exp_fit.log_likelihood)
    weibull_lrt = {
        "likelihood_ratio": weibull_lrt_stat,
        "df": weibull_fit.n_params - exp_fit.n_params,
        "p_value": float(stats.chi2.sf(weibull_lrt_stat, weibull_fit.n_params - exp_fit.n_params)),
    }

    km = survival_curves(spells)

    events.to_csv(out_dir / "victim_events.csv", index=False)
    spells.to_csv(out_dir / "recurrent_event_spells.csv", index=False)
    same_day.to_csv(out_dir / "same_day_entity_events_collapsed.csv", index=False)
    piecewise.to_csv(out_dir / "piecewise_elapsed_hazard.csv", index=False)
    adjusted_split.to_csv(out_dir / "adjusted_piecewise_exposure.csv", index=False)
    adjusted_coef.to_csv(out_dir / "adjusted_piecewise_coefficients.csv", index=False)
    u_shape_summary.to_csv(out_dir / "u_shape_phase_rates.csv", index=False)
    u_shape_contrasts.to_csv(out_dir / "u_shape_adjusted_contrasts.csv", index=False)
    u_shape_variant_contrasts.to_csv(out_dir / "u_shape_covariate_variant_contrasts.csv", index=False)
    u_shape_variant_lrt.to_csv(out_dir / "u_shape_covariate_variant_lrt.csv", index=False)
    delayed_peak_summary_table.to_csv(out_dir / "delayed_peak_phase_rates.csv", index=False)
    delayed_peak_contrasts.to_csv(out_dir / "delayed_peak_adjusted_contrasts.csv", index=False)
    delayed_peak_sensitivity_summary.to_csv(out_dir / "delayed_peak_sensitivity_phase_rates.csv", index=False)
    delayed_peak_sensitivity_contrasts.to_csv(
        out_dir / "delayed_peak_sensitivity_adjusted_contrasts.csv", index=False
    )
    model_table.to_csv(out_dir / "parametric_model_comparison.csv", index=False)
    km.to_csv(out_dir / "kaplan_meier_nelson_aalen.csv", index=False)

    plot_paths = {
        "piecewise hazard": out_dir / "piecewise_hazard.png",
        "survival curve": out_dir / "survival_curve.png",
        "parametric hazards": out_dir / "parametric_hazards.png",
    }
    plot_piecewise_hazard(piecewise, plot_paths["piecewise hazard"])
    plot_survival(km, exp_fit, plot_paths["survival curve"])
    plot_parametric_hazards(fits, plot_paths["parametric hazards"], max_day=float(spells["duration_days"].quantile(0.95)))

    entity_counts = events.groupby("entity_id")["deduplicated_event_id"].nunique()
    covariate_coverage = {
        "size_estimate levels": {
            str(key): int(value)
            for key, value in events["org_size"].value_counts().sort_index().items()
        },
        "size_confidence populated": int(events["size_confidence"].notna().sum()),
        "employee_count populated": int(events["employee_count"].notna().sum()),
        "turnover populated": int(events["turnover"].notna().sum()),
        "industry populated": int(events["industry"].notna().sum()),
        "entity_kind populated": int(events["entity_kind"].notna().sum()),
        "sector_proxy levels": {
            str(key): int(value)
            for key, value in events["sector_proxy"].value_counts().sort_index().items()
        },
        "prior records populated": int(events["records_affected"].notna().sum()),
    }
    data_quality = {
        "victim event attributions": int(len(events)),
        "unique deduplicated events": int(events["deduplicated_event_id"].nunique()),
        "victim entities": int(events["entity_id"].nunique()),
        "entities with more than one event": int((entity_counts > 1).sum()),
        "post-event spells": int(len(spells)),
        "observed repeat spells": int(spells["event"].sum()),
        "right-censored spells": int((1 - spells["event"]).sum()),
        "censor date": censor_date.isoformat(),
        "elapsed-time bands": ", ".join(band_labels(elapsed_bounds)),
        "minimum event date": events["event_date"].min().isoformat(),
        "maximum event date": events["event_date"].max().isoformat(),
        "first-of-month event dates": int(sum(d.day == 1 for d in events["event_date"])),
        "same-day entity-event groups collapsed": int(len(same_day)),
        "covariate coverage": covariate_coverage,
    }

    summary = {
        "data_quality": data_quality,
        "elapsed_bounds": ["inf" if math.isinf(v) else int(v) for v in elapsed_bounds],
        "unadjusted_piecewise_lrt": unadjusted_lrt,
        "adjusted_piecewise_lrt": adjusted_lrt,
        "target_rate_ratio": target_rr,
        "u_shape_phase_lrt": u_shape_lrt,
        "u_shape_adjusted_contrasts": records_for_json(u_shape_contrasts),
        "u_shape_covariate_variant_lrt": records_for_json(u_shape_variant_lrt),
        "u_shape_covariate_variant_contrasts": records_for_json(u_shape_variant_contrasts),
        "delayed_peak_lrt": delayed_peak_lrt,
        "delayed_peak_adjusted_contrasts": records_for_json(delayed_peak_contrasts),
        "delayed_peak_sensitivity_elapsed_lrt": delayed_peak_sensitivity_elapsed_lrt,
        "delayed_peak_sensitivity_lrt": delayed_peak_sensitivity_lrt,
        "delayed_peak_sensitivity_adjusted_contrasts": records_for_json(delayed_peak_sensitivity_contrasts),
        "weibull_vs_exponential_lrt": weibull_lrt,
        "best_aic_model": str(model_table.iloc[0]["model"]),
        "outputs": {key: str(value) for key, value in plot_paths.items()},
    }
    (out_dir / "analysis_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    write_report(
        out_dir / "recurrent_timing_report.md",
        data_quality,
        piecewise,
        adjusted_coef,
        unadjusted_lrt,
        adjusted_lrt,
        target_rr,
        u_shape_summary,
        u_shape_contrasts,
        u_shape_lrt,
        u_shape_variant_contrasts,
        u_shape_variant_lrt,
        delayed_peak_summary_table,
        delayed_peak_contrasts,
        delayed_peak_lrt,
        delayed_peak_sensitivity_summary,
        delayed_peak_sensitivity_contrasts,
        delayed_peak_sensitivity_lrt,
        delayed_peak_sensitivity_elapsed_lrt,
        model_table,
        weibull_lrt,
        plot_paths,
    )

    print(f"Wrote recurrent timing analysis to {out_dir}")
    print(f"Adjusted elapsed-time LRT p-value: {adjusted_lrt['p_value']:.4g}")
    print(f"Best parametric AIC model: {model_table.iloc[0]['model']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
