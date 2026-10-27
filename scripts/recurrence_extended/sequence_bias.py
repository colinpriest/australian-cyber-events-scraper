"""Recurrent-event order, attack transitions and observation-bias sensitivity.

All rates describe recorded events after an organisation's first observed
event. They are not estimates of incident risk in the Australian population.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import chi2

YEAR_DAYS = 365.25
UNKNOWN = "Unknown"
AMBIGUOUS = "Multiple or ambiguous"
SIZE_LEVELS = ("SMALL", "MEDIUM", "LARGE", "HUGE", "UNKNOWN")


def _size(value: Any) -> str:
    label = str(value).strip().upper() if pd.notna(value) else "UNKNOWN"
    return label if label in SIZE_LEVELS else "UNKNOWN"


def _source_history(db_path: Path, censor_date: date) -> tuple[pd.DataFrame, str]:
    """Load source observations without creating or modifying a database.

    All source records are returned so callers can distinguish records first
    discovered after the as-of date from those without a discovery timestamp.
    """
    path = Path(db_path)
    if not path.exists():
        return pd.DataFrame(columns=["event_id", "source_type", "discovered_at"]), "database unavailable"
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA query_only = ON")
            rows = pd.read_sql_query(
                """SELECT DISTINCT m.deduplicated_event_id AS event_id,
                          r.source_type, r.discovered_at
                   FROM EventDeduplicationMap m JOIN RawEvents r ON r.raw_event_id = m.raw_event_id""",
                conn,
            )
    except (sqlite3.Error, pd.errors.DatabaseError) as exc:
        return pd.DataFrame(columns=["event_id", "source_type", "discovered_at"]), f"source history unavailable: {exc}"
    rows["source_type"] = rows["source_type"].fillna(UNKNOWN).astype(str).str.strip().replace("", UNKNOWN)
    # Discovery timestamps are stored as local ISO strings. Preserve their
    # recorded calendar date instead of shifting timezone-aware values to UTC.
    rows["discovery_day"] = pd.to_datetime(
        rows["discovered_at"].astype("string").str.slice(0, 10), errors="coerce"
    ).dt.date
    return rows, "available"


def _source_labels(rows: pd.DataFrame, censor_date: date) -> tuple[dict[str, str], set[str], int]:
    """Return as-of labels, definitely future-only event ids, and unknown-date count."""
    labels: dict[str, str] = {}
    future_only: set[str] = set()
    missing = 0
    if rows.empty:
        return labels, future_only, missing
    for event_id, group in rows.groupby("event_id", dropna=False):
        if pd.isna(event_id):
            continue
        observed = group[group["discovery_day"].notna() & (group["discovery_day"] <= censor_date)]
        undated = group[group["discovery_day"].isna()]
        if not len(observed) and not len(undated):
            future_only.add(str(event_id))
            continue
        missing += int(len(undated))
        # A source with no discovery timestamp cannot be credited as observed
        # at the cutoff. It is left Unknown rather than leaked into the past.
        sources = sorted(set(observed["source_type"]) - {UNKNOWN})
        labels[str(event_id)] = sources[0] if len(sources) == 1 else ("Multiple sources" if sources else UNKNOWN)
    return labels, future_only, missing


def _type_label(values: pd.Series) -> str:
    cleaned = set()
    for value in values:
        if pd.isna(value) or not str(value).strip() or str(value).strip().lower() in {"unknown", "none", "nan", "null"}:
            cleaned.add(UNKNOWN)
        else:
            cleaned.add(str(value).strip())
    return next(iter(cleaned)) if len(cleaned) == 1 else AMBIGUOUS


def _first_nonempty(values: pd.Series) -> str:
    for item in values:
        if pd.notna(item) and str(item).strip() and str(item).strip().lower() not in {"nan", "none", "null"}:
            return str(item).strip()
    return UNKNOWN


def prepare_events(events: pd.DataFrame, censor_date: date, labels: dict[str, str] | None = None,
                   future_only: set[str] | None = None) -> pd.DataFrame:
    """Collapse same-day events without inventing their order or attack type."""
    required = {"entity_id", "deduplicated_event_id", "event_date"}
    missing = required - set(events.columns)
    if missing:
        raise ValueError(f"Missing recurrence event columns: {', '.join(sorted(missing))}")
    df = events.copy()
    for col in ("event_type", "org_size", "size_estimate"):
        if col not in df:
            df[col] = None
    # Parse as dates, dropping invalid rows and events after the observation end.
    df["event_date"] = pd.to_datetime(df["event_date"], errors="coerce").dt.date
    df = df[df["event_date"].notna() & (df["event_date"] <= censor_date) & df["entity_id"].notna()].copy()
    df = df[~df["deduplicated_event_id"].astype(str).isin(future_only or set())]
    df = df.drop_duplicates(["entity_id", "deduplicated_event_id"])
    df["source_label"] = df["deduplicated_event_id"].astype(str).map(labels or {}).fillna(UNKNOWN)
    df["size_label"] = df["org_size"].map(_size)
    # A single fixed current size per organisation is required for comparisons.
    # Contradictory input labels become UNKNOWN rather than assigning different
    # incident histories to different size strata.
    sizes = df.groupby("entity_id")["size_label"].agg(lambda s: s.iloc[0] if s.nunique() == 1 else "UNKNOWN")
    df["size_label"] = df["entity_id"].map(sizes)
    if df.empty:
        return pd.DataFrame(columns=["entity_id", "event_date", "event_type", "event_ids", "source", "size"])
    grouped = df.groupby(["entity_id", "event_date"], sort=True, as_index=False).agg(
        event_type=("event_type", _type_label),
        event_ids=("deduplicated_event_id", lambda x: "|".join(sorted(set(map(str, x))))),
        source=("source_label", lambda x: _first_nonempty(x) if len(set(x)) == 1 else "Multiple sources"),
        size=("size_label", _first_nonempty),
    )
    return grouped.sort_values(["entity_id", "event_date", "event_ids"]).reset_index(drop=True)


def make_spells(days: pd.DataFrame, censor_date: date, order_shift: dict[Any, int] | None = None) -> pd.DataFrame:
    """Build one right-censored gap after each observed event day."""
    cols = ["entity_id", "event_date", "event_type", "event_ids", "source", "size"]
    if days.empty:
        return pd.DataFrame(columns=cols + ["prior_event_number", "end_date", "next_type", "event", "exposure_days"])
    frame = days.sort_values(["entity_id", "event_date", "event_ids"]).copy()
    frame["prior_event_number"] = frame.groupby("entity_id").cumcount() + 1
    if order_shift:
        frame["prior_event_number"] += frame["entity_id"].map(order_shift).fillna(0).astype(int)
    frame["end_date"] = frame.groupby("entity_id")["event_date"].shift(-1)
    frame["next_type"] = frame.groupby("entity_id")["event_type"].shift(-1)
    frame["event"] = frame["end_date"].notna().astype(int)
    frame["end_date"] = frame["end_date"].fillna(censor_date)
    frame["exposure_days"] = (pd.to_datetime(frame["end_date"]) - pd.to_datetime(frame["event_date"])).dt.days
    return frame[frame["exposure_days"] > 0].copy()


def _rate(count: int, days: float) -> tuple[float | None, float | None, float | None]:
    if days <= 0:
        return None, None, None
    scale = 100 * YEAR_DAYS / days
    lower = 0.0 if count == 0 else 0.5 * float(chi2.ppf(.025, 2 * count))
    upper = 0.5 * float(chi2.ppf(.975, 2 * (count + 1)))
    return count * scale, lower * scale, upper * scale


def order_rates(spells: pd.DataFrame) -> pd.DataFrame:
    """First observed event to second versus second and later observed gaps."""
    columns = ["event_order", "spells", "organisations", "repeats", "censored", "exposure_days",
               "rate_per_100_entity_years", "poisson_ci_low", "poisson_ci_high"]
    rows = []
    for label, mask in [("After first observed incident", spells["prior_event_number"] == 1),
                        ("After second or later observed incident", spells["prior_event_number"] >= 2)]:
        group = spells[mask]
        count, exposure = int(group["event"].sum()), float(group["exposure_days"].sum())
        rate, low, high = _rate(count, exposure)
        rows.append(dict(event_order=label, spells=len(group), organisations=int(group["entity_id"].nunique()),
                         repeats=count, censored=int(len(group) - count), exposure_days=exposure,
                         rate_per_100_entity_years=rate, poisson_ci_low=low, poisson_ci_high=high))
    return pd.DataFrame(rows, columns=columns)


def transition_table(spells: pd.DataFrame) -> pd.DataFrame:
    """Observed transitions only; same-day conflicting types are ambiguous."""
    cols = ["prior_attack_type", "next_attack_type", "transitions", "organisations", "share_of_prior_type"]
    observed = spells[spells["event"] == 1].copy()
    if observed.empty:
        return pd.DataFrame(columns=cols)
    result = observed.groupby(["event_type", "next_type"], dropna=False, as_index=False).agg(
        transitions=("event", "sum"), organisations=("entity_id", "nunique"))
    result.rename(columns={"event_type": "prior_attack_type", "next_type": "next_attack_type"}, inplace=True)
    result["share_of_prior_type"] = result["transitions"] / result.groupby("prior_attack_type")["transitions"].transform("sum")
    return result.sort_values(["prior_attack_type", "next_attack_type"]).reset_index(drop=True)[cols]


def observation_strata(spells: pd.DataFrame) -> pd.DataFrame:
    """Rates by source, calendar cohort and recorded organisation size."""
    columns = ["dimension", "group", "spells", "organisations", "repeats", "censored", "exposure_days",
               "rate_per_100_entity_years", "poisson_ci_low", "poisson_ci_high"]
    if spells.empty:
        return pd.DataFrame(columns=columns)
    work = spells.copy()
    years = pd.to_datetime(work["event_date"]).dt.year
    work["calendar_cohort"] = pd.cut(years, bins=[-np.inf, 2018, 2021, 2023, np.inf],
                                     labels=["Through 2018", "2019–2021", "2022–2023", "2024 onward"]).astype(str)
    out = []
    for col, label in [("source", "Source of prior incident"), ("calendar_cohort", "Calendar cohort of prior incident"),
                       ("size", "Recorded organisation size")]:
        for name, group in work.groupby(col, dropna=False):
            count, days = int(group["event"].sum()), float(group["exposure_days"].sum())
            rate, low, high = _rate(count, days)
            out.append(dict(dimension=label, group=str(name), spells=len(group),
                            organisations=int(group["entity_id"].nunique()), repeats=count,
                            censored=int(len(group) - count), exposure_days=days,
                            rate_per_100_entity_years=rate, poisson_ci_low=low, poisson_ci_high=high))
    return pd.DataFrame(out, columns=columns)


def _observed_rows(days: pd.DataFrame, rng: np.random.Generator, scenario: str) -> pd.DataFrame:
    frame = days.copy()
    first = frame.groupby("entity_id").cumcount() == 0
    if scenario == "uniform 75% follow-up observation":
        p = np.full(len(frame), .75)
    elif scenario == "lower observation for small organisations":
        p = np.where(frame["size"].astype(str).str.upper().isin(["SMALL", "MEDIUM"]), .5, 1.0)
    else:
        raise ValueError(f"Unknown observation scenario {scenario!r}")
    return frame[first | (rng.random(len(frame)) < p)].copy()


def reporting_sensitivity(days: pd.DataFrame, censor_date: date, seed: int, replicates: int = 100) -> pd.DataFrame:
    """Illustrative thinning of recorded follow-ups, not correction for missing attacks."""
    columns = ["scenario", "replicates", "median_recorded_repeats", "p05_recorded_repeats",
               "p95_recorded_repeats", "median_rate_per_100_entity_years", "p05_rate", "p95_rate"]
    if days.empty:
        return pd.DataFrame(columns=columns)
    if replicates < 1:
        raise ValueError("bias replicates must be at least 1")
    rng = np.random.default_rng(seed)
    out = []
    for scenario in ["uniform 75% follow-up observation", "lower observation for small organisations"]:
        rates, counts = [], []
        for _ in range(replicates):
            sample = _observed_rows(days, rng, scenario)
            spells = make_spells(sample, censor_date)
            n, exposure = int(spells["event"].sum()), float(spells["exposure_days"].sum())
            counts.append(n)
            if exposure > 0:
                rates.append(n * 100 * YEAR_DAYS / exposure)
        q = lambda values, quantile: float(np.quantile(values, quantile)) if values else None
        out.append(dict(scenario=scenario, replicates=replicates, median_recorded_repeats=q(counts, .5),
                        p05_recorded_repeats=q(counts, .05), p95_recorded_repeats=q(counts, .95),
                        median_rate_per_100_entity_years=q(rates, .5), p05_rate=q(rates, .05), p95_rate=q(rates, .95)))
    return pd.DataFrame(out, columns=columns)


def prior_history_sensitivity(days: pd.DataFrame, censor_date: date, seed: int, replicates: int = 100) -> pd.DataFrame:
    """Illustrate unknown earlier incidents by shifting the observed event order.

    The extra incident is hypothetical: no date or additional exposure is
    fabricated. Rates describe the existing observed spells under re-labelling.
    """
    columns = ["scenario", "replicates", "order_group", "median_repeats", "p05_repeats", "p95_repeats",
               "median_rate_per_100_entity_years", "p05_rate", "p95_rate"]
    if days.empty:
        return pd.DataFrame(columns=columns)
    if replicates < 1:
        raise ValueError("bias replicates must be at least 1")
    rng = np.random.default_rng(seed + 1)
    ids = days["entity_id"].drop_duplicates().tolist()
    output = []
    for probability in (0.0, .25, .5):
        observations = {"First": ([], []), "Later": ([], [])}
        for _ in range(replicates if probability else 1):
            shifts = dict(zip(ids, (rng.random(len(ids)) < probability).astype(int)))
            rates = order_rates(make_spells(days, censor_date, shifts))
            for index, name in [(0, "First"), (1, "Later")]:
                observations[name][0].append(int(rates.iloc[index]["repeats"]))
                rate = rates.iloc[index]["rate_per_100_entity_years"]
                if pd.notna(rate):
                    observations[name][1].append(float(rate))
        for name, (counts, rates) in observations.items():
            q = lambda v, p: float(np.quantile(v, p)) if v else None
            output.append(dict(scenario=f"{probability:.0%} of organisations had an unobserved earlier incident",
                               replicates=len(counts), order_group=name, median_repeats=q(counts, .5),
                               p05_repeats=q(counts, .05), p95_repeats=q(counts, .95),
                               median_rate_per_100_entity_years=q(rates, .5), p05_rate=q(rates, .05),
                               p95_rate=q(rates, .95)))
    return pd.DataFrame(output, columns=columns)


def _table(frame: pd.DataFrame, max_rows: int = 30) -> str:
    if frame.empty:
        return "No observations available."
    shown = frame.head(max_rows).copy()
    for col in shown.select_dtypes(include=["number"]).columns:
        shown[col] = shown[col].map(lambda x: "" if pd.isna(x) else f"{x:.2f}" if isinstance(x, float) else str(x))
    names = [str(c).replace("_", " ") for c in shown.columns]
    def clean(x: Any) -> str:
        return str(x).replace("|", "\\|").replace("\n", " ") if pd.notna(x) else ""
    lines = ["| " + " | ".join(names) + " |", "| " + " | ".join(["---"] * len(names)) + " |"]
    lines.extend("| " + " | ".join(clean(v) for v in row) + " |" for row in shown.itertuples(index=False, name=None))
    if len(frame) > max_rows:
        lines.append(f"\nShowing {max_rows} of {len(frame)} rows; the CSV contains all rows.")
    return "\n".join(lines)


def size_weights(days: pd.DataFrame) -> dict[str, float]:
    """Fixed distribution of unique organisations, including unknown size."""
    entities = days.drop_duplicates("entity_id")
    if entities.empty:
        return {}
    counts = entities["size"].map(_size).value_counts()
    return {s: float(counts.get(s, 0) / len(entities)) for s in SIZE_LEVELS if counts.get(s, 0)}


def standardize_rates(group: pd.DataFrame, weights: dict[str, float]) -> tuple[float | None, float]:
    """Direct standardization; never extrapolate a missing reference stratum."""
    stats = group.groupby("size", dropna=False).agg(events=("event", "sum"), days=("exposure_days", "sum"))
    available = {str(s): r for s, r in stats.iterrows() if r["days"] > 0}
    covered = sum(weight for size, weight in weights.items() if size in available)
    if not weights or covered < 1 - 1e-10:
        return None, float(covered)
    return float(sum(weight * 36525 * available[size]["events"] / available[size]["days"]
                     for size, weight in weights.items())), float(covered)


def size_comparisons(spells: pd.DataFrame, weights: dict[str, float]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Size-stratified and fixed-weight rates for order, source, and cohort."""
    columns = ["dimension", "group", "size", "organisations", "spells", "repeats", "exposure_days",
               "rate_per_100_entity_years", "poisson_ci_low", "poisson_ci_high"]
    standards = ["dimension", "group", "organisations", "repeats", "reference_weight_covered",
                 "standardized_rate_per_100_entity_years", "status"]
    if spells.empty:
        return pd.DataFrame(columns=columns), pd.DataFrame(columns=standards)
    work = spells.copy()
    work["order_group"] = np.where(work["prior_event_number"] == 1, "First observed", "Second or later observed")
    years = pd.to_datetime(work["event_date"]).dt.year
    work["calendar_cohort"] = pd.cut(years, bins=[-np.inf, 2018, 2021, 2023, np.inf],
                                     labels=["Through 2018", "2019–2021", "2022–2023", "2024 onward"]).astype(str)
    strata, adjusted = [], []
    for field, dimension in (("order_group", "Event order"), ("source", "Prior incident source"),
                             ("calendar_cohort", "Prior incident calendar cohort")):
        for name, group in work.groupby(field, dropna=False, sort=True):
            standard, covered = standardize_rates(group, weights)
            adjusted.append(dict(dimension=dimension, group=str(name), organisations=group.entity_id.nunique(),
                                 repeats=int(group.event.sum()), reference_weight_covered=covered,
                                 standardized_rate_per_100_entity_years=standard,
                                 status="available" if standard is not None else "unavailable: missing size exposure"))
            for size, stratum in group.groupby("size", dropna=False, sort=True):
                n, exposure = int(stratum.event.sum()), float(stratum.exposure_days.sum())
                rate, low, high = _rate(n, exposure)
                strata.append(dict(dimension=dimension, group=str(name), size=str(size),
                                   organisations=stratum.entity_id.nunique(), spells=len(stratum), repeats=n,
                                   exposure_days=exposure, rate_per_100_entity_years=rate,
                                   poisson_ci_low=low, poisson_ci_high=high))
    return pd.DataFrame(strata, columns=columns), pd.DataFrame(adjusted, columns=standards)


def transitions_by_size(spells: pd.DataFrame) -> pd.DataFrame:
    """Conditional transition counts by fixed organisation size, unknown included."""
    cols = ["size", "prior_attack_type", "next_attack_type", "transitions", "organisations", "share_of_prior_type_within_size"]
    observed = spells[spells["event"] == 1]
    if observed.empty:
        return pd.DataFrame(columns=cols)
    result = observed.groupby(["size", "event_type", "next_type"], dropna=False, as_index=False).agg(
        transitions=("event", "sum"), organisations=("entity_id", "nunique"))
    result.rename(columns={"event_type": "prior_attack_type", "next_type": "next_attack_type"}, inplace=True)
    result["share_of_prior_type_within_size"] = result.transitions / result.groupby(["size", "prior_attack_type"]).transitions.transform("sum")
    return result.sort_values(["size", "prior_attack_type", "next_attack_type"])[cols].reset_index(drop=True)


def size_bias_simulations(days: pd.DataFrame, censor_date: date, weights: dict[str, float],
                          seed: int, replicates: int) -> pd.DataFrame:
    """Size-stratified and standardized reporting/history simulations.

    Weights stay fixed across every draw and scenario; unsupported standardized
    estimates are missing rather than imputed. Percentiles summarize simulated
    scenarios, not statistical uncertainty or unobserved true incidents.
    """
    columns = ["family", "scenario", "order_group", "size", "replicates", "valid_rate_draws",
               "median_repeats", "median_rate", "p05_rate", "p95_rate", "reference_weight_covered"]
    if days.empty:
        return pd.DataFrame(columns=columns)
    if replicates < 1:
        raise ValueError("bias replicates must be at least 1")
    rng = np.random.default_rng(seed)
    ids = days.entity_id.drop_duplicates().tolist()
    records: dict[tuple[str, str, str, str], list[tuple[int, float | None, float]]] = {}
    scenarios = [("reporting", "uniform 75% follow-up observation", None),
                 ("reporting", "lower observation for small organisations", None),
                 ("history", "0% missing prior history", 0.0),
                 ("history", "25% missing prior history", .25),
                 ("history", "50% missing prior history", .5)]
    for family, scenario, probability in scenarios:
        for _ in range(1 if probability == 0.0 else replicates):
            if family == "reporting":
                sample = _observed_rows(days, rng, scenario)
                spells = make_spells(sample, censor_date)
                groups = [("All observed orders", spells)]
            else:
                shifts = dict(zip(ids, (rng.random(len(ids)) < probability).astype(int)))
                spells = make_spells(days, censor_date, shifts)
                groups = [("First", spells[spells.prior_event_number == 1]),
                          ("Later", spells[spells.prior_event_number >= 2])]
            for order, group in groups:
                for size in weights:
                    stratum = group[group["size"] == size]
                    n, exposure = int(stratum.event.sum()), float(stratum.exposure_days.sum())
                    rate = n * 36525 / exposure if exposure else None
                    records.setdefault((family, scenario, order, size), []).append((n, rate, 1.0 if exposure else 0.0))
                standard, covered = standardize_rates(group, weights)
                records.setdefault((family, scenario, order, "STANDARDIZED"), []).append((int(group.event.sum()), standard, covered))
    rows = []
    for (family, scenario, order, size), draws in records.items():
        rates = [r for _, r, _ in draws if r is not None and np.isfinite(r)]
        rows.append(dict(family=family, scenario=scenario, order_group=order, size=size,
                         replicates=len(draws), valid_rate_draws=len(rates),
                         median_repeats=float(np.median([n for n, _, _ in draws])),
                         median_rate=float(np.median(rates)) if rates else None,
                         p05_rate=float(np.quantile(rates, .05)) if rates else None,
                         p95_rate=float(np.quantile(rates, .95)) if rates else None,
                         reference_weight_covered=float(np.median([c for _, _, c in draws]))))
    return pd.DataFrame(rows, columns=columns)


def run(events: pd.DataFrame, db_path: Path, censor_date: date, out_dir: Path, seed: int, **kwargs: Any) -> dict[str, Any]:
    """Run requests 4, 5 and 7, producing report-ready Markdown and CSVs."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sources, source_status = _source_history(Path(db_path), censor_date)
    # Source metadata for unrelated events must not affect exclusion counts or
    # the reported volume of undated sources in this analysis population.
    event_ids = set(events["deduplicated_event_id"].dropna().astype(str)) if "deduplicated_event_id" in events else set()
    if not sources.empty:
        sources = sources[sources["event_id"].astype(str).isin(event_ids)].copy()
    labels, future_only, undated_sources = _source_labels(sources, censor_date)
    days = prepare_events(events, censor_date, labels, future_only)
    spells = make_spells(days, censor_date)
    reps = int(kwargs.get("bias_replicates", 100))
    weights = size_weights(days)
    size_strata, standardized = size_comparisons(spells, weights)
    tables = {
        "event_order_rates.csv": order_rates(spells),
        "attack_type_transitions.csv": transition_table(spells),
        "observation_bias_strata.csv": observation_strata(spells),
        "observation_bias_thinning.csv": reporting_sensitivity(days, censor_date, seed, reps),
        "prior_history_sensitivity.csv": prior_history_sensitivity(days, censor_date, seed, reps),
        "recurrence_order_source_calendar_by_size.csv": size_strata,
        "recurrence_order_source_calendar_standardized.csv": standardized,
        "attack_type_transitions_by_size.csv": transitions_by_size(spells),
        "observation_bias_by_size.csv": size_bias_simulations(days, censor_date, weights, seed, reps),
        "recurrence_size_reference_weights.csv": pd.DataFrame(
            [{"size": size, "unique_organisations": int(days.loc[days["size"] == size, "entity_id"].nunique()),
              "weight": weight} for size, weight in weights.items()],
            columns=["size", "unique_organisations", "weight"]),
    }
    for name, frame in tables.items():
        frame.to_csv(out / name, index=False)
    lines = [
        "## Event order and attack-type transitions", "",
        "Comparisons use the same reference distribution of baseline unique organisations by current recorded size "
        "(SMALL, MEDIUM, LARGE, HUGE, UNKNOWN), including unknown size as its own stratum. "
        "Contradictory size labels for an organisation are treated as UNKNOWN. Direct standardized rates "
        "are unavailable if any reference stratum lacks exposure; no missing stratum is extrapolated. "
        "Size helps account for differences associated with visibility, exposure and target value, but "
        "those factors are not directly measured here and size adjustment cannot remove all confounding. "
        "The reference weights are in `recurrence_size_reference_weights.csv`.", "",
        "These first/later recorded-incident rates are standardized to that common size distribution. "
        "Spells end at the next recorded incident or the censor date; censored spells contribute exposure. "
        "Use the size-stratified CSV to inspect sparse groups; Poisson intervals do not account for "
        "dependence between incidents at one organisation.", "",
        _table(standardized[standardized.dimension == "Event order"]), "",
        "Transition counts and within-size conditional shares appear in `attack_type_transitions_by_size.csv`. "
        "These are conditional on a subsequent observed incident. Unknown types remain Unknown; "
        "different or unknown types on the same day are labelled Multiple or ambiguous. "
        "Cells are sparse and are not causal estimates.", "",
        _table(tables["attack_type_transitions_by_size.csv"], max_rows=20), "",
        "## Reporting and observation bias", "",
        f"Source history: {source_status}; {len(future_only)} event IDs with all known discovery dates after "
        f"the cutoff excluded; {undated_sources} source records lack discovery dates. Organisation sizes "
        "are current recorded estimates and may not describe their size at the incident date.", "",
        "Source groups refer to the prior incident, are mutually exclusive, and show source availability—not "
        "the chance that an incident was reported. Calendar groups are the prior incident's cohort, not "
        "calendar-time-adjusted rate ratios. Missing source history is Unknown.", "",
        "The following source and calendar rates are standardized to the same fixed size weights. "
        "Size-specific rates and exposure are in `recurrence_order_source_calendar_by_size.csv`.", "",
        _table(standardized[standardized.dimension != "Event order"]), "",
        "The following simulations remove some already recorded follow-ups under stated hypothetical "
        "observation probabilities: 75% for all follow-ups, or 50% for small and medium organisations and "
        "100% for other recorded sizes, including Unknown. First observed incidents are retained. "
        "They illustrate sensitivity and do not estimate or reconstruct unreported attacks.", "",
        "The following table uses fixed size standardization for each simulation draw; "
        "the full size-specific results, support and valid draw counts are in `observation_bias_by_size.csv`. "
        "A missing rate means one or more reference size groups have no exposure.", "",
        _table(tables["observation_bias_by_size.csv"].query("family == 'reporting' and size == 'STANDARDIZED'")), "",
        "The prior-history scenarios hypothetically assign an unobserved earlier incident to a fraction of "
        "organisations; they relabel existing spells without fabricating dates or exposure. They are not corrections "
        "or estimates of the true unobserved history.", "",
        _table(tables["observation_bias_by_size.csv"].query("family == 'history' and size == 'STANDARDIZED'")), "",
        "The original aggregate CSVs are unadjusted descriptive counts, supplied for continuity; "
        "they should not be used as size-adjusted comparisons.", "",
        "These are associations in a selected, publicly reported dataset, conditional on an observed first incident. "
        "They do not establish causal effects or population attack risk. Sparse groups and reporting changes can be important.",
    ]
    return {"section": "\n".join(lines), "outputs": [str(out / name) for name in tables],
            "source_history_status": source_status, "future_only_events_excluded": len(future_only),
            "undated_source_records": undated_sources, "same_day_type_ambiguities": int((days["event_type"] == AMBIGUOUS).sum()),
            "bias_replicates": reps, "size_reference_weights": weights}
