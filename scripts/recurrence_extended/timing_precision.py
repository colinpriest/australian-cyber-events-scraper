"""Timing-band, smooth-hazard and incident-date precision sensitivity analyses.

All quantities describe *recorded* events among observed victims, not attacks
in the Australian population.  A first-of-month date is ambiguous, not proof
of a missing day.  Explicit precision is accepted only from a matching record's
metadata or the event's stored, verified ``date_precision`` with its stored
interval (``event_date_earliest`` / ``event_date_latest``); everything else has
no interval and is unknown - never read as a single day by a scenario that
depends on precision.  Spells (and what happens to several incidents of one
organisation on one date) come from ``recurrence_extended.spells``, the same
code the main analysis uses.

The prospective protocol was defined on 27 October 2026.  A frozen cohort is
recorded on that date, even if the latest data snapshot is older; its cutoff
is recorded. A later first run cannot reconstruct a genuine prospective
cohort from a mutable database and is labelled unavailable, not confirmatory.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import math
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from scipy import stats
from scipy.special import ndtr

from scripts.recurrence_extended.spells import (
    DEFAULT_TIE_POLICY,
    RANDOM_TIE_POLICIES,
    TIE_POLICIES,
    TIE_POLICY_ASSUMPTIONS,
    make_spells as shared_make_spells,
    occurrences,
    overlapping_interval_pairs,
    tie_summary,
    tie_table,
)

REGISTERED_ON = date(2026, 10, 27)
VALIDATION_END = date(2028, 10, 27)
MIN_VALIDATION_EVENTS = 20
SCHEMES = {
    "pre_specified": (0, 90, 180, 270, 365, 730, math.inf),
    "60_day_boundaries": (0, 60, 120, 180, 240, 365, 730, math.inf),
    "shifted_boundary": (0, 120, 180, 240, 365, 730, math.inf),
    "wide_first_band": (0, 180, 365, 730, math.inf),
}
PEAK_BOUNDS = (0, 90, 180, 270)
SIZE_LEVELS = ("SMALL", "MEDIUM", "LARGE", "HUGE", "UNKNOWN")


def size_labels(events: pd.DataFrame) -> pd.Series:
    """Use the current recorded size; absent and unrecognized values stay unknown."""
    values = events["org_size"] if "org_size" in events else pd.Series("UNKNOWN", index=events.index)
    labels = values.fillna("UNKNOWN").astype(str).str.strip().str.upper()
    labels = labels.where(labels.isin(SIZE_LEVELS), "UNKNOWN")
    if "entity_id" in events:
        labels = labels.groupby(events["entity_id"]).transform(lambda x: x.iloc[0] if x.nunique() == 1 else "UNKNOWN").fillna("UNKNOWN")
    return labels


def standard_size_weights(events: pd.DataFrame) -> dict[str, float]:
    """One fixed size per baseline entity, including UNKNOWN."""
    entities = events[["entity_id"]].copy()
    entities["size"] = size_labels(events)
    entities = entities.dropna(subset=["entity_id"]).drop_duplicates("entity_id")
    counts = entities["size"].value_counts()
    return {size: float(counts.get(size, 0) / len(entities)) if len(entities) else 0.0
            for size in SIZE_LEVELS}


def _as_date(value: Any) -> Optional[date]:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.date()


def _normalise_precision(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip().lower().replace("_", " ")
    if text in {"day", "daily", "exact", "exact day", "day precision"}:
        return "day"
    if text in {"month", "monthly", "month only", "month precision"}:
        return "month"
    if text in {"year", "yearly", "year only", "year precision"}:
        return "year"
    return None


def _parse_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _read_evidence(db_path: Path, as_of: Optional[date] = None) -> tuple[dict[str, set[str]], dict[str, date]]:
    """Return matching explicit precision and first collection date per event.

    Optional historical schemas may not contain the needed evidence.  Missing
    tables or columns are represented as absent data and surfaced in outputs.
    """
    evidence: dict[str, set[str]] = {}
    seen: dict[str, date] = {}
    if not Path(db_path).is_file():
        return evidence, seen
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"EventDeduplicationMap", "EnrichedEvents", "RawEvents", "DeduplicatedEvents"} <= tables:
            return evidence, seen
        columns = {t: {r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')} for t in
                   ("EventDeduplicationMap", "EnrichedEvents", "RawEvents", "DeduplicatedEvents")}
        required = {
            "EventDeduplicationMap": {"deduplicated_event_id", "enriched_event_id"},
            "EnrichedEvents": {"enriched_event_id", "raw_event_id", "event_date"},
            "RawEvents": {"raw_event_id", "event_date", "discovered_at"},
            "DeduplicatedEvents": {"deduplicated_event_id", "event_date"},
        }
        if any(not required[t] <= columns[t] for t in required):
            return evidence, seen
        metadata = "r.source_metadata" if "source_metadata" in columns["RawEvents"] else "NULL"
        enrichment = "e.perplexity_enrichment_data" if "perplexity_enrichment_data" in columns["EnrichedEvents"] else "NULL"
        query = f"""
            SELECT m.deduplicated_event_id, d.event_date, e.event_date, r.event_date,
                   r.discovered_at, {metadata}, {enrichment}
            FROM EventDeduplicationMap m
            JOIN DeduplicatedEvents d ON d.deduplicated_event_id=m.deduplicated_event_id
            JOIN EnrichedEvents e ON e.enriched_event_id=m.enriched_event_id
            JOIN RawEvents r ON r.raw_event_id=e.raw_event_id
        """
        for event_id, event_date, enriched_date, raw_date, discovered, raw_json, enriched_json in conn.execute(query):
            key = str(event_id)
            found = _as_date(discovered)
            if found and (key not in seen or found < seen[key]):
                seen[key] = found
            if as_of and (not found or found > as_of):
                continue
            chosen = _as_date(event_date)
            for payload, underlying in ((_parse_object(raw_json), raw_date),
                                        (_parse_object(enriched_json), enriched_date)):
                # Precision on a different date or publication date does not
                # establish the incident date's precision.
                if chosen is None or _as_date(underlying) != chosen:
                    continue
                for field in ("incident_date_precision", "event_date_precision"):
                    explicit = _normalise_precision(payload.get(field))
                    if explicit:
                        evidence.setdefault(key, set()).add(explicit)
    return evidence, seen


#: Narrowest first: compatible sources (a month and a day inside it) reconcile to the narrower interval.
PRECISION_ORDER = ("day", "month", "year")


def _stored_interval(row: pd.Series) -> Optional[tuple[date, date]]:
    start, end = _as_date(row.get("event_date_earliest")), _as_date(row.get("event_date_latest"))
    return (start, end) if start is not None and end is not None else None


def annotate_precision(events: pd.DataFrame, evidence: dict[str, set[str]]) -> pd.DataFrame:
    """Label each event's date by what is *verified* about it, and attach its interval.

    ``explicit_day`` / ``explicit_month`` / ``explicit_year``: a verified precision (the stored
    ``date_precision``, or matching record metadata); the interval is the stored one
    (``event_date_earliest`` / ``event_date_latest``, from ``processing/incident_dates.py``), else the same
    rule applied to the date.  Sources that give different but compatible precisions reconcile to the
    narrower.  Unverified dates have *no interval*: ``ambiguous_first_of_month`` (a 1st may be a month
    placeholder) and ``unknown`` (any other stated date of unverified precision) - the date is unknown,
    not a day.  Adds ``date_precision_basis``, ``date_earliest`` and ``date_latest``.
    """
    from cyber_data_collector.processing.incident_dates import interval as canonical_interval

    out = events.copy()
    out["event_date"] = [_as_date(v) for v in out["event_date"]]
    labels, earliest, latest = [], [], []
    for _, row in out.iterrows():
        explicit = set(evidence.get(str(row["deduplicated_event_id"]), set()))
        provided = _normalise_precision(row.get("date_precision"))
        if provided:
            explicit.add(provided)
        start = end = None
        if explicit and row["event_date"]:
            precision = next(p for p in PRECISION_ORDER if p in explicit)
            labels.append("explicit_" + precision)
            stored = _stored_interval(row) if precision == provided else None
            if stored is None:
                low, high = canonical_interval(row["event_date"], precision)
                stored = (_as_date(low), _as_date(high))
            start, end = stored
        elif row["event_date"] and row["event_date"].day == 1:
            labels.append("ambiguous_first_of_month")
        else:
            labels.append("unknown")
        earliest.append(start)
        latest.append(end)
    out["date_precision_basis"] = labels
    out["date_earliest"] = earliest
    out["date_latest"] = latest
    return out


def make_spells(events: pd.DataFrame, censor: date, tie_policy: str = DEFAULT_TIE_POLICY,
                rng: Optional[np.random.Generator] = None) -> pd.DataFrame:
    """Build spells with right censoring; incidents of one organisation on one date follow ``tie_policy``.

    The shared builder (``recurrence_extended.spells``) is used, so these rates and the main analysis
    treat ties identically (``collapse``, the default, is one time point; see that module for the others;
    ``interval_draw`` needs ``rng``).  Excluding future rows before pairing is essential: a later event
    must not be treated as observed beyond the censor date.
    """
    clean = events.copy()
    clean["size"] = clean["_analysis_size"] if "_analysis_size" in clean else size_labels(clean)
    points = occurrences(clean, tie_policy, {"size": ("size", "first")})
    spells = shared_make_spells(points, censor, tie_policy, rng=rng)
    start = pd.to_datetime(spells["placed_date"])
    return pd.DataFrame({"entity_id": spells["entity_id"].astype(str), "size": spells["size"], "start": start,
                         "end": pd.to_datetime(spells["spell_end_date"]),
                         "duration_days": spells["duration_days"].astype(int),
                         "event": spells["event"].astype(int)}).reset_index(drop=True)


def size_adjusted_bands(spells: pd.DataFrame, bounds: tuple[float, ...],
                        weights: dict[str, float]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Directly standardize rates to a fixed entity-size distribution.

    Every positive-weight size requires exposure in a band. No implicit
    reweighting or pseudocounts are applied when a stratum is unsupported.
    """
    rows, strata = [], []
    for lo, hi in zip(bounds, bounds[1:]):
        label = f"{int(lo)+1}-{int(hi)}" if np.isfinite(hi) else f">{int(lo)}"
        value, unsupported = 0.0, []
        for size in SIZE_LEVELS:
            group = spells[spells["size"] == size]
            duration = group["duration_days"].to_numpy(dtype=float)
            exposure = float(np.maximum(0, np.minimum(duration, hi) - lo).sum())
            count = int(((duration > lo) & (duration <= hi) & group["event"].to_numpy(dtype=bool)).sum())
            rate = 36525 * count / exposure if exposure else None
            if weights.get(size, 0) and rate is None:
                unsupported.append(size)
            elif rate is not None:
                value += weights.get(size, 0) * rate
            strata.append({"band": label, "size": size, "weight": weights.get(size, 0),
                           "events": count, "exposure_days": exposure,
                           "rate_per_100_entity_years": rate})
        rows.append({"band": label, "size_adjusted_rate_per_100_entity_years": value if not unsupported and sum(weights.values()) else None,
                     "unsupported_size_strata": "|".join(unsupported)})
    return pd.DataFrame(rows), pd.DataFrame(strata)


def band_rates(spells: pd.DataFrame, bounds: tuple[float, ...]) -> pd.DataFrame:
    rows = []
    duration = spells["duration_days"].to_numpy(dtype=float)
    observed = spells["event"].to_numpy(dtype=bool)
    for lo, hi in zip(bounds, bounds[1:]):
        exposure = float(np.maximum(0, np.minimum(duration, hi) - lo).sum())
        count = int((observed & (duration > lo) & (duration <= hi)).sum())
        rate = 36525 * count / exposure if exposure else None
        low = 36525 * 0.5 * stats.chi2.ppf(0.025, 2 * count) / exposure if count and exposure else (0.0 if exposure else None)
        high = 36525 * 0.5 * stats.chi2.ppf(0.975, 2 * (count + 1)) / exposure if exposure else None
        rows.append({"band": f"{int(lo)+1}-{int(hi)}" if np.isfinite(hi) else f">{int(lo)}",
                     "lower_days": lo, "upper_days": hi if np.isfinite(hi) else None,
                     "events": count, "exposure_days": exposure, "rate_per_100_entity_years": rate,
                     "poisson_ci_low": low, "poisson_ci_high": high})
    return pd.DataFrame(rows)


def smooth_hazard(spells: pd.DataFrame, seed: int, bootstrap: int = 200,
                  bandwidth_days: float = 45.0) -> pd.DataFrame:
    """Kernel event density / kernel-smoothed risk exposure, entity bootstrap.

    The denominator integrates the kernel over each observable spell, including
    censored spells.  Resampling whole entities preserves within-entity repeats.
    """
    grid = np.arange(1.0, 731.0, 5.0)
    columns = ["day", "rate_per_100_entity_years", "bootstrap_ci_low", "bootstrap_ci_high", "bandwidth_days"]
    if spells.empty:
        return pd.DataFrame(columns=columns)
    duration = spells["duration_days"].to_numpy(dtype=float)[:, None]
    x = grid[None, :]
    h = float(bandwidth_days)
    failure = spells["event"].to_numpy(dtype=float)[:, None] * stats.norm.pdf((duration - x) / h) / h
    exposure = ndtr((duration - x) / h) - ndtr(-x / h)
    numerator, denominator = failure.sum(axis=0), exposure.sum(axis=0)
    point = np.divide(numerator * 36525, denominator, out=np.full(len(grid), np.nan), where=denominator > 1e-12)
    lower = np.full(len(grid), np.nan)
    upper = np.full(len(grid), np.nan)
    groups, codes = np.unique(spells["entity_id"].astype(str), return_inverse=True)
    if bootstrap > 1 and len(groups) > 1:
        f_by_group = np.zeros((len(groups), len(grid)))
        e_by_group = np.zeros_like(f_by_group)
        np.add.at(f_by_group, codes, failure)
        np.add.at(e_by_group, codes, exposure)
        weights = np.random.default_rng(seed).multinomial(len(groups), np.full(len(groups), 1 / len(groups)), size=bootstrap)
        den = weights @ e_by_group
        boot = np.divide((weights @ f_by_group) * 36525, den,
                         out=np.full((bootstrap, len(grid)), np.nan), where=den > 1e-12)
        available = np.any(np.isfinite(boot), axis=0)
        if available.any():
            lower[available], upper[available] = np.nanpercentile(boot[:, available], [2.5, 97.5], axis=0)
    return pd.DataFrame({"day": grid, "rate_per_100_entity_years": point,
                         "bootstrap_ci_low": lower, "bootstrap_ci_high": upper,
                         "bandwidth_days": h})


def size_adjusted_smooth(spells: pd.DataFrame, weights: dict[str, float], seed: int,
                         bootstrap: int = 200, bandwidth_days: float = 45.0) -> pd.DataFrame:
    """Standardize kernel rates; resample entities and retain fixed size weights."""
    grid = np.arange(1.0, 731.0, 5.0)
    if spells.empty:
        return pd.DataFrame(columns=["day", "size_adjusted_rate", "bootstrap_ci_low", "bootstrap_ci_high", "valid_replicates"])
    duration = spells["duration_days"].to_numpy(dtype=float)[:, None]
    h = float(bandwidth_days)
    failure = spells["event"].to_numpy(dtype=float)[:, None] * stats.norm.pdf((duration - grid[None, :]) / h) / h
    exposure = ndtr((duration - grid[None, :]) / h) - ndtr(-grid[None, :] / h)
    entity, codes = np.unique(spells["entity_id"].astype(str), return_inverse=True)
    f_entity = np.zeros((len(entity), len(grid)))
    e_entity = np.zeros_like(f_entity)
    np.add.at(f_entity, codes, failure)
    np.add.at(e_entity, codes, exposure)
    labels = spells.groupby("entity_id", sort=True)["size"].first().reindex(entity).fillna("UNKNOWN").to_numpy()
    rng = np.random.default_rng(seed)
    samples = rng.multinomial(len(entity), np.full(len(entity), 1 / len(entity)), size=bootstrap) if bootstrap else np.empty((0, len(entity)))
    sample_weights = np.vstack([np.ones(len(entity)), samples])
    results = np.zeros((len(sample_weights), len(grid)))
    supported = np.ones_like(results, dtype=bool)
    for size in SIZE_LEVELS:
        weight = weights.get(size, 0)
        if not weight:
            continue
        mask = labels == size
        denominator = sample_weights[:, mask] @ e_entity[mask]
        numerator = sample_weights[:, mask] @ f_entity[mask]
        supported &= denominator > 1e-12
        results += weight * np.divide(numerator * 36525, denominator,
                                      out=np.zeros_like(denominator), where=denominator > 1e-12)
    results[~supported] = np.nan
    lower = np.full(len(grid), np.nan)
    upper = np.full(len(grid), np.nan)
    available = np.any(np.isfinite(results[1:]), axis=0)
    if available.any():
        lower[available], upper[available] = np.nanpercentile(results[1:, available], [2.5, 97.5], axis=0)
    return pd.DataFrame({"day": grid, "size_adjusted_rate": results[0], "bootstrap_ci_low": lower,
                         "bootstrap_ci_high": upper, "valid_replicates": np.isfinite(results[1:]).sum(axis=0),
                         "requested_replicates": bootstrap, "bandwidth_days": h})


def impute_dates(events: pd.DataFrame, rng: np.random.Generator,
                 ambiguous_scenario: str = "keep", censor: Optional[date] = None) -> pd.DataFrame:
    """Draw once per incident, so all victims of that incident share a date.

    Verified month and year dates are drawn uniformly from their *stored interval*
    (``date_earliest`` / ``date_latest``), never from an interval rebuilt around the point date.  Only
    under the hypothetical scenarios (``month`` / ``month_or_year``) is an interval assumed for a 1st of
    the month whose precision is unverified; otherwise unverified dates stay as stored, and the
    scenarios that must not rely on them (:func:`complete_histories`) leave them out instead.
    """
    out = events.copy()
    kinds = {"explicit_month", "explicit_year"}
    if ambiguous_scenario != "keep":
        kinds.add("ambiguous_first_of_month")
    unique = out[out["date_precision_basis"].isin(kinds)].drop_duplicates("deduplicated_event_id").copy()
    if unique.empty:
        return out
    days = pd.to_datetime(unique["event_date"], errors="coerce")
    assumed = unique["date_precision_basis"].eq("ambiguous_first_of_month")
    yearly = assumed & (ambiguous_scenario == "month_or_year") & days.dt.month.eq(1)
    first_assumed = days.dt.to_period("M").dt.start_time.where(~yearly, days.dt.to_period("Y").dt.start_time)
    last_assumed = days.dt.to_period("M").dt.end_time.dt.normalize().where(~yearly, days.dt.to_period("Y").dt.end_time.dt.normalize())
    first = pd.to_datetime(unique["date_earliest"], errors="coerce").where(~assumed, first_assumed)
    last = pd.to_datetime(unique["date_latest"], errors="coerce").where(~assumed, last_assumed)
    if first.isna().any() or last.isna().any():
        raise ValueError("a verified month or year date has no stored interval to draw from")
    if censor:
        last = last.clip(upper=pd.Timestamp(censor))
    lengths = (last - first).dt.days.to_numpy() + 1
    draw = first + pd.to_timedelta(rng.integers(0, lengths), unit="D")
    mapping = dict(zip(unique["deduplicated_event_id"], draw.dt.date))
    replacement = out["deduplicated_event_id"].map(mapping)
    out["event_date"] = replacement.where(replacement.notna(), out["event_date"])
    return out


def complete_histories(events: pd.DataFrame, verified: str) -> pd.DataFrame:
    """Organisations every one of whose dates is verified; the rest are left out, never read as days.

    Dropping only an organisation's uncertain events would fabricate a gap between the ones left, so a
    history is kept whole or not at all. ``verified='day'`` needs exact verified days; ``'interval'``
    needs a verified precision (day, month or year) - and thus a stored interval - on every event.
    """
    accepted = {"explicit_day"} if verified == "day" else {"explicit_day", "explicit_month", "explicit_year"}
    mask = events.groupby("entity_id")["date_precision_basis"].transform(lambda s: s.isin(accepted).all()).astype(bool)
    return events[mask]


def _scenario_events(events: pd.DataFrame, scenario: str, rng: np.random.Generator, censor: date) -> pd.DataFrame:
    if scenario == "baseline":
        return events
    if scenario == "exact":
        return complete_histories(events, "day")
    if scenario == "verified":
        return impute_dates(complete_histories(events, "interval"), rng, "keep", censor)
    return impute_dates(events, rng, scenario, censor)


#: ``(label, scenario)``: how each date-uncertainty scenario treats the dates it cannot verify.
DATE_SCENARIOS = [("stored_dates", "baseline"), ("known_intervals", "keep"),
                  ("ambiguous_firsts_as_month", "month"),
                  ("ambiguous_firsts_as_month_or_year", "month_or_year"),
                  ("explicit_day_only_complete_histories", "exact"),
                  ("verified_intervals_complete_histories", "verified")]


def date_sensitivity(events: pd.DataFrame, censor: date, seed: int, draws: int = 100,
                     tie_policy: str = DEFAULT_TIE_POLICY) -> pd.DataFrame:
    """Band rates under each date-uncertainty scenario (``DATE_SCENARIOS``).

    ``stored_dates`` and ``known_intervals`` keep the dates they cannot verify as stored; the two
    ``ambiguous_firsts`` scenarios add a hypothetical month or year around unverified 1sts; the two
    ``complete_histories`` scenarios restrict to organisations whose dates are all verified (as exact
    days, or as a verified day / month / year drawn within its stored interval).
    """
    rows = []
    for label, scenario in DATE_SCENARIOS:
        rng = np.random.default_rng(seed)
        n = 1 if scenario in {"baseline", "exact"} else draws
        samples = []
        for _ in range(n):
            shifted = _scenario_events(events, scenario, rng, censor)
            rate = band_rates(make_spells(shifted, censor, tie_policy), PEAK_BOUNDS)
            samples.append(rate.set_index("band"))
        for band in samples[0].index:
            vals = np.array([float(s.loc[band, "rate_per_100_entity_years"]) if pd.notna(s.loc[band, "rate_per_100_entity_years"]) else np.nan for s in samples])
            valid = vals[np.isfinite(vals)]
            rows.append({"scenario": label, "band": band, "draws": n,
                         "median_rate_per_100_entity_years": float(np.median(valid)) if len(valid) else None,
                         "imputation_2_5_pct": float(np.percentile(valid, 2.5)) if len(valid) and scenario != "exact" else None,
                         "imputation_97_5_pct": float(np.percentile(valid, 97.5)) if len(valid) and scenario != "exact" else None})
    return pd.DataFrame(rows)


def size_adjusted_date_sensitivity(events: pd.DataFrame, censor: date, seed: int,
                                   weights: dict[str, float], draws: int = 100,
                                   tie_policy: str = DEFAULT_TIE_POLICY) -> pd.DataFrame:
    """Keep the baseline standard population fixed for every date draw."""
    rows = []
    for label, scenario in DATE_SCENARIOS:
        rng = np.random.default_rng(seed)
        n = 1 if scenario in {"baseline", "exact"} else draws
        samples = []
        for _ in range(n):
            shifted = _scenario_events(events, scenario, rng, censor)
            rate, _ = size_adjusted_bands(make_spells(shifted, censor, tie_policy), PEAK_BOUNDS, weights)
            samples.append(rate.set_index("band"))
        for band in samples[0].index:
            values = np.array([float(s.loc[band, "size_adjusted_rate_per_100_entity_years"]) if pd.notna(s.loc[band, "size_adjusted_rate_per_100_entity_years"]) else np.nan for s in samples])
            valid = values[np.isfinite(values)]
            missing = sorted({part for s in samples for part in str(s.loc[band, "unsupported_size_strata"]).split("|") if part})
            rows.append({"scenario": label, "band": band, "draws": n, "valid_draws": len(valid),
                         "median_size_adjusted_rate": float(np.median(valid)) if len(valid) else None,
                         "imputation_2_5_pct": float(np.percentile(valid, 2.5)) if len(valid) else None,
                         "imputation_97_5_pct": float(np.percentile(valid, 97.5)) if len(valid) else None,
                         "unsupported_size_strata": "|".join(missing)})
    return pd.DataFrame(rows)


def _tie_rates(spells: pd.DataFrame, weights: dict[str, float]) -> pd.DataFrame:
    frames = []
    for scheme, bounds in SCHEMES.items():
        unadjusted = band_rates(spells, bounds)[["band", "events", "exposure_days", "rate_per_100_entity_years"]]
        adjusted, _ = size_adjusted_bands(spells, bounds, weights)
        merged = unadjusted.merge(adjusted, on="band", how="left")
        merged.insert(0, "scheme", scheme)
        frames.append(merged)
    return pd.concat(frames, ignore_index=True)


def tie_policy_sensitivity(events: pd.DataFrame, censor: date, weights: dict[str, float],
                           seed: int = 0, draws: int = 100) -> pd.DataFrame:
    """Band rates under every tie policy: what is assumed about several incidents of one organisation on one date.

    For every policy and band scheme: repeats, exposure, the unadjusted rate and the size-standardized
    rate, with the policy's stated assumption (``spells.TIE_POLICY_ASSUMPTIONS``). The deterministic
    policies (``spells.TIE_POLICIES``; ``collapse`` is the primary treatment) are one row each;
    ``interval_draw`` places tied incidents at random within their shared verified interval ``draws``
    times and reports the median over the draws, with the 2.5-97.5% range of the size-standardized
    rate. No policy bounds the truth (see ``recurrence_extended.spells``).
    """
    numeric = ["events", "exposure_days", "rate_per_100_entity_years", "size_adjusted_rate_per_100_entity_years"]
    frames = []
    for policy in TIE_POLICIES:
        table = _tie_rates(make_spells(events, censor, policy), weights)
        table.insert(0, "draws", 1)
        table.insert(0, "tie_policy", policy)
        frames.append(table)
    for policy in RANDOM_TIE_POLICIES:
        rng = np.random.default_rng(seed)
        samples = [_tie_rates(make_spells(events, censor, policy, rng=rng), weights) for _ in range(max(1, draws))]
        stacked = pd.concat(samples, ignore_index=True)
        for column in numeric:
            stacked[column] = pd.to_numeric(stacked[column], errors="coerce")
        grouped = stacked.groupby(["scheme", "band"], sort=False)
        table = grouped[numeric].median().reset_index()
        table["unsupported_size_strata"] = grouped["unsupported_size_strata"].agg(
            lambda s: "|".join(sorted({part for v in s.dropna() for part in str(v).split("|") if part}))).values
        table["size_adjusted_2_5_pct"] = grouped["size_adjusted_rate_per_100_entity_years"].quantile(0.025).values
        table["size_adjusted_97_5_pct"] = grouped["size_adjusted_rate_per_100_entity_years"].quantile(0.975).values
        table.insert(0, "draws", len(samples))
        table.insert(0, "tie_policy", policy)
        frames.append(table)
    out = pd.concat(frames, ignore_index=True)
    out["assumption"] = out["tie_policy"].map(TIE_POLICY_ASSUMPTIONS)
    return out


def prospective_validation(events: pd.DataFrame, seen: dict[str, date], censor: date,
                           out_dir: Path) -> dict[str, Any]:
    """Freeze a dated cohort, then assess a fixed future follow-up once mature."""
    path = out_dir / "timing_peak_registration.json"
    if path.exists():
        registration = json.loads(path.read_text(encoding="utf-8"))
        if registration.get("registered_on") != REGISTERED_ON.isoformat() or registration.get("version") != 1:
            raise ValueError(f"Unexpected prospective protocol in {path}; choose a new output directory")
    elif date.today() < REGISTERED_ON:
        return {"status": "pending_registration", "reason": "The fixed registration date has not yet arrived.",
                "registered_on": REGISTERED_ON.isoformat(), "validation_end": VALIDATION_END.isoformat()}
    elif date.today() > REGISTERED_ON:
        return {"status": "unavailable", "reason": "No cohort was frozen by the registration date; a later database cannot prove a prospective cohort.",
                "registered_on": REGISTERED_ON.isoformat(), "validation_end": VALIDATION_END.isoformat()}
    else:
        snapshot_cutoff = min(censor, REGISTERED_ON)
        eligible = events[(events["event_date"] <= snapshot_cutoff) &
                          events["deduplicated_event_id"].astype(str).map(lambda e: seen.get(e, date.max) <= snapshot_cutoff)]
        cohort = []
        for entity, group in eligible.groupby("entity_id", sort=True):
            anchor = max(group["event_date"])
            cohort.append({"entity_id": str(entity), "anchor_date": anchor.isoformat(),
                           "anchor_ids": sorted(group.loc[group["event_date"] == anchor, "deduplicated_event_id"].astype(str).tolist())})
        if not cohort:
            return {"status": "pending_registration", "reason": "No records with verified discovery dates were available at registration.",
                    "registered_on": REGISTERED_ON.isoformat(), "validation_end": VALIDATION_END.isoformat()}
        payload = {"version": 1, "registered_on": REGISTERED_ON.isoformat(),
                   "created_at": datetime.now().isoformat(), "validation_end": VALIDATION_END.isoformat(),
                   "snapshot_cutoff": snapshot_cutoff.isoformat(),
                   "primary_contrast": "91-180 days vs combined 1-90 and 181-270 days; one-sided conditional Poisson approximation",
                   "minimum_events": MIN_VALIDATION_EVENTS, "cohort": cohort}
        payload["cohort_sha256"] = hashlib.sha256(json.dumps(cohort, sort_keys=True).encode("utf-8")).hexdigest()
        # Exclusive creation makes re-runs use the same frozen cohort.
        try:
            with path.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            registration = payload
        except FileExistsError:
            registration = json.loads(path.read_text(encoding="utf-8"))
    cohort = registration.get("cohort", [])
    if registration.get("cohort_sha256") != hashlib.sha256(json.dumps(cohort, sort_keys=True).encode("utf-8")).hexdigest():
        raise ValueError(f"Frozen prospective cohort was changed in {path}")
    followup = events.copy()
    followup["entity_key"] = followup["entity_id"].astype(str)
    followup["seen_on"] = followup["deduplicated_event_id"].astype(str).map(seen)
    followup = followup[followup["seen_on"].notna() & (followup["seen_on"] > REGISTERED_ON) &
                        (followup["seen_on"] <= censor) & (followup["event_date"] > REGISTERED_ON) &
                        (followup["event_date"] <= censor)]
    exposure = np.zeros(3)
    count = np.zeros(3, dtype=int)
    end_of_followup = min(censor, VALIDATION_END)
    for item in cohort:
        start = date.fromisoformat(item["anchor_date"])
        rows = followup[followup["entity_key"] == item["entity_id"]]
        next_date = min(rows["event_date"]) if not rows.empty else None
        end = min(next_date, end_of_followup) if next_date else end_of_followup
        entry_days = max(0, (REGISTERED_ON - start).days)
        exit_days = max(entry_days, (end - start).days)
        for i, (lo, hi) in enumerate(zip(PEAK_BOUNDS, PEAK_BOUNDS[1:])):
            exposure[i] += max(0, min(exit_days, hi) - max(entry_days, lo))
            if next_date and next_date <= end_of_followup and lo < (next_date - start).days <= hi:
                count[i] += 1
    total = int(count.sum())
    mature = censor >= VALIDATION_END and total >= MIN_VALIDATION_EVENTS and exposure[1] > 0 and (exposure[0] + exposure[2]) > 0
    p = float(stats.binomtest(int(count[1]), total, p=float(exposure[1] / exposure.sum()), alternative="greater").pvalue) if mature else None
    return {"status": "complete" if mature else "pending", "registered_on": REGISTERED_ON.isoformat(),
            "validation_end": VALIDATION_END.isoformat(), "frozen_entities": len(cohort),
            "snapshot_cutoff": registration.get("snapshot_cutoff"),
            "events_by_band": count.tolist(), "exposure_days_by_band": exposure.tolist(),
            "minimum_events": MIN_VALIDATION_EVENTS, "one_sided_p_value": p,
            "interpretation": "Conditional Poisson approximation for recorded incidents assumes independent Poisson counts. Current mutable records may have changed since registration; if the snapshot predates registration, unobserved intervening events may be missed."}


def prospective_size_validation(events: pd.DataFrame, seen: dict[str, date], censor: date,
                                out_dir: Path) -> dict[str, Any]:
    """Freeze size and cohort for a separate, prospectively stratified test.

    This never modifies the previously registered unadjusted protocol. The
    conditional test combines independent binomial counts across size strata;
    it is valid under a common no-peak null within each size stratum and the
    stated Poisson independence assumptions, rather than treating size as a
    measure of exposure, visibility, or value.
    """
    path = out_dir / "timing_size_adjusted_registration.json"
    if path.exists():
        registration = json.loads(path.read_text(encoding="utf-8"))
        if registration.get("version") != 1 or registration.get("registered_on") != REGISTERED_ON.isoformat():
            raise ValueError(f"Unexpected size-adjusted protocol in {path}")
    elif date.today() < REGISTERED_ON:
        return {"status": "pending_registration", "reason": "Registration date has not arrived."}
    elif date.today() > REGISTERED_ON:
        return {"status": "unavailable", "reason": "No size-adjusted cohort was frozen on the registration date."}
    else:
        cutoff = min(censor, REGISTERED_ON)
        eligible = events.copy()
        eligible = eligible[(eligible["event_date"] <= cutoff) &
            eligible["deduplicated_event_id"].astype(str).map(lambda e: seen.get(e, date.max) <= cutoff)]
        eligible["frozen_size"] = size_labels(eligible)
        cohort = []
        for entity, group in eligible.groupby("entity_id", sort=True):
            anchor = max(group["event_date"])
            cohort.append({"entity_id": str(entity), "anchor_date": anchor.isoformat(),
                           "anchor_ids": sorted(group.loc[group["event_date"] == anchor, "deduplicated_event_id"].astype(str).tolist()),
                           "size": str(group["frozen_size"].iloc[0])})
        if not cohort:
            return {"status": "pending_registration", "reason": "No eligible cohort with verified discovery dates."}
        registration = {"version": 1, "registered_on": REGISTERED_ON.isoformat(),
                        "created_at": datetime.now().isoformat(), "snapshot_cutoff": cutoff.isoformat(),
                        "validation_end": VALIDATION_END.isoformat(), "minimum_events": MIN_VALIDATION_EVENTS,
                        "primary_contrast": "91-180 days versus combined 1-90 and 181-270 days, conditional binomial test within frozen size strata",
                        "cohort": cohort}
        registration["cohort_sha256"] = hashlib.sha256(json.dumps(cohort, sort_keys=True).encode("utf-8")).hexdigest()
        try:
            with path.open("x", encoding="utf-8") as handle:
                json.dump(registration, handle, indent=2)
        except FileExistsError:
            registration = json.loads(path.read_text(encoding="utf-8"))
    cohort = registration.get("cohort", [])
    if registration.get("cohort_sha256") != hashlib.sha256(json.dumps(cohort, sort_keys=True).encode("utf-8")).hexdigest():
        raise ValueError(f"Frozen size-adjusted cohort was changed in {path}")
    followup = events.copy()
    followup["entity_key"] = followup["entity_id"].astype(str)
    followup["seen_on"] = followup["deduplicated_event_id"].astype(str).map(seen)
    followup = followup[followup["seen_on"].notna() & (followup["seen_on"] > REGISTERED_ON) &
                        (followup["seen_on"] <= censor) & (followup["event_date"] > REGISTERED_ON) &
                        (followup["event_date"] <= censor)]
    stop = min(censor, VALIDATION_END)
    groups = {size: {"size": size, "entities": 0, "events": [0, 0, 0], "exposure_days": [0.0, 0.0, 0.0]}
              for size in SIZE_LEVELS}
    for item in cohort:
        size = item.get("size", "UNKNOWN")
        if size not in groups:
            size = "UNKNOWN"
        group = groups[size]
        group["entities"] += 1
        start = date.fromisoformat(item["anchor_date"])
        dates = followup.loc[followup["entity_key"] == item["entity_id"], "event_date"]
        next_date = min(dates) if len(dates) else None
        end = min(next_date, stop) if next_date else stop
        entry = max(0, (REGISTERED_ON - start).days)
        exit_day = max(entry, (end - start).days)
        for i, (lo, hi) in enumerate(zip(PEAK_BOUNDS, PEAK_BOUNDS[1:])):
            group["exposure_days"][i] += max(0, min(exit_day, hi) - max(entry, lo))
            if next_date and next_date <= stop and lo < (next_date - start).days <= hi:
                group["events"][i] += 1
    distribution = np.array([1.0])
    peak_events = comparison_events = 0
    supported = []
    for size, group in groups.items():
        early, peak, late = group["exposure_days"]
        if peak <= 0 or early + late <= 0:
            continue
        n = sum(group["events"])
        if n:
            distribution = np.convolve(distribution, stats.binom.pmf(np.arange(n + 1), n, peak / (early + peak + late)))
            peak_events += group["events"][1]
            comparison_events += n
        supported.append(size)
    mature = censor >= VALIDATION_END and comparison_events >= MIN_VALIDATION_EVENTS
    p = float(min(1.0, distribution[peak_events:].sum())) if mature else None
    return {"status": "complete" if mature else "pending", "registered_on": REGISTERED_ON.isoformat(),
            "validation_end": VALIDATION_END.isoformat(), "snapshot_cutoff": registration.get("snapshot_cutoff"),
            "frozen_entities": len(cohort), "size_strata": list(groups.values()), "supported_strata": supported,
            "peak_events_supported": peak_events, "comparison_events_supported": comparison_events,
            "minimum_events": MIN_VALIDATION_EVENTS, "one_sided_p_value": p,
            "interpretation": "Size-stratified conditional Poisson test assumes independent events and correctly specified exposure within size strata. Current classifications are frozen at registration, not historical measurements; size does not directly measure visibility, attack surface or value. Sparse or unsupported strata provide no evidence for the test."}


def run(events: pd.DataFrame, db_path: Path, censor_date: date, out_dir: Path,
        seed: int, **kwargs: Any) -> dict[str, Any]:
    """Write timing and date-sensitivity tables; return a Markdown section."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    censor = _as_date(censor_date)
    if censor is None:
        raise ValueError("censor_date must be a valid date")
    if not {"entity_id", "deduplicated_event_id", "event_date"} <= set(events.columns):
        raise ValueError("events require entity_id, deduplicated_event_id, event_date")
    evidence, seen = _read_evidence(Path(db_path), censor)
    if "first_discovered_at" in events:
        for event_id, discovered in zip(events["deduplicated_event_id"], events["first_discovered_at"]):
            found = _as_date(discovered)
            key = str(event_id)
            if found and (key not in seen or found < seen[key]):
                seen[key] = found
    annotated = annotate_precision(events, evidence)
    annotated = annotated[annotated["event_date"].notna() & (annotated["event_date"] <= censor)].copy()
    weights = standard_size_weights(annotated)
    annotated["_analysis_size"] = size_labels(annotated)
    spells = make_spells(annotated, censor)
    tables, adjusted_tables, size_tables = [], [], []
    for name, bounds in SCHEMES.items():
        frame = band_rates(spells, bounds)
        frame.insert(0, "scheme", name)
        tables.append(frame)
        adjusted, strata = size_adjusted_bands(spells, bounds, weights)
        adjusted.insert(0, "scheme", name)
        strata.insert(0, "scheme", name)
        adjusted_tables.append(adjusted)
        size_tables.append(strata)
    bands = pd.concat(tables, ignore_index=True)
    bands.to_csv(out_dir / "timing_band_sensitivity.csv", index=False)
    adjusted_bands = pd.concat(adjusted_tables, ignore_index=True)
    adjusted_bands.to_csv(out_dir / "timing_band_size_adjusted.csv", index=False)
    pd.concat(size_tables, ignore_index=True).to_csv(out_dir / "timing_band_size_strata.csv", index=False)
    smooth = smooth_hazard(spells, seed, bootstrap=int(kwargs.get("smooth_bootstrap", kwargs.get("bootstrap", 200))))
    smooth.to_csv(out_dir / "timing_smooth_hazard.csv", index=False)
    smooth_adjusted = size_adjusted_smooth(spells, weights, seed, bootstrap=int(kwargs.get("smooth_bootstrap", kwargs.get("bootstrap", 200))))
    smooth_adjusted.to_csv(out_dir / "timing_smooth_size_adjusted.csv", index=False)
    draws = max(1, int(kwargs.get("date_imputations", kwargs.get("date_draws", 100))))
    date_rates = date_sensitivity(annotated, censor, seed, draws=draws)
    date_rates.to_csv(out_dir / "timing_date_precision_sensitivity.csv", index=False)
    adjusted_dates = size_adjusted_date_sensitivity(annotated, censor, seed, weights, draws=draws)
    adjusted_dates.to_csv(out_dir / "timing_date_precision_size_adjusted.csv", index=False)
    precision = annotated.groupby("date_precision_basis").size().rename("victim_event_attributions").reset_index()
    precision.to_csv(out_dir / "timing_date_precision_coverage.csv", index=False)
    ties = tie_table(annotated)
    ties.to_csv(out_dir / "timing_entity_date_ties.csv", index=False)
    tie_rates = tie_policy_sensitivity(annotated, censor, weights, seed=seed, draws=draws)
    tie_rates.to_csv(out_dir / "timing_tie_policy_sensitivity.csv", index=False)
    validation = prospective_validation(annotated, seen, censor, out_dir)
    (out_dir / "timing_peak_validation.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")
    size_validation = prospective_size_validation(annotated, seen, censor, out_dir)
    (out_dir / "timing_peak_size_adjusted_validation.json").write_text(json.dumps(size_validation, indent=2), encoding="utf-8")
    # A plot of the smooth estimate is useful alongside the machine-readable table.
    if not smooth.empty:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(9, 4.5))
        ax.plot(smooth_adjusted["day"].to_numpy(), smooth_adjusted["size_adjusted_rate"].to_numpy(), label="Size-standardized recorded-event rate")
        ax.fill_between(smooth_adjusted["day"].to_numpy(dtype=float), smooth_adjusted["bootstrap_ci_low"].to_numpy(dtype=float),
                        smooth_adjusted["bootstrap_ci_high"].to_numpy(dtype=float), alpha=0.2, label="Entity-bootstrap 95% interval")
        ax.axvspan(91, 180, color="orange", alpha=0.12, label="91–180 days")
        ax.set(xlabel="Days since previous recorded incident", ylabel="Recorded repeats per 100 entity-years")
        ax.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(out_dir / "timing_smooth_hazard.png", dpi=160)
        plt.close(fig)
    counts = {str(k): int(v) for k, v in annotated["date_precision_basis"].value_counts().items()}
    complete = annotated.groupby("entity_id")["date_precision_basis"].transform(lambda s: s.eq("explicit_day").all()).astype(bool)
    precise_entities = int(annotated.loc[complete, "entity_id"].nunique())
    precise_repeating = int((annotated.loc[complete].groupby("entity_id")["event_date"].nunique() > 1).sum())
    precision_note = ("Insufficient explicit date precision for a reliable precisely dated subset. "
                      if precise_repeating < 10 else "The precisely dated subset is selective. ")
    def fmt(value: Any) -> str:
        return "—" if pd.isna(value) else f"{float(value):.2f}"
    main_rows = bands[(bands["scheme"] == "pre_specified") & bands["band"].isin(["1-90", "91-180", "181-270"])].merge(
        adjusted_bands, on=["scheme", "band"], how="left")
    main_table = "| Days | Repeat events | Exposure days | Unadjusted rate | Size-standardized rate | Unsupported sizes |\n|---|---:|---:|---:|---:|---|\n" + "\n".join(
        f"| {row.band} | {row.events} | {row.exposure_days:.0f} | {fmt(row.rate_per_100_entity_years)} | {fmt(row.size_adjusted_rate_per_100_entity_years)} | {row.unsupported_size_strata or 'none'} |"
        for row in main_rows.itertuples())
    date_rows = adjusted_dates[adjusted_dates["band"] == "91-180"]
    date_table = "| Date scenario | 91–180-day size-standardized median rate | 2.5–97.5% of date draws | Valid draws |\n|---|---:|---:|---:|\n" + "\n".join(
        f"| {row.scenario.replace('_', ' ')} | {fmt(row.median_size_adjusted_rate)} | {fmt(row.imputation_2_5_pct)}–{fmt(row.imputation_97_5_pct)} | {row.valid_draws}/{row.draws} |"
        for row in date_rows.itertuples())
    tie_summary_counts = tie_summary(ties)
    tie_bands = tie_rates[(tie_rates["scheme"] == "pre_specified") & tie_rates["band"].isin(["1-90", "91-180", "181-270"])]
    tie_table_text = "| Tie policy | Days | Repeat events | Unadjusted rate | Size-standardized rate |\n|---|---|---:|---:|---:|\n" + "\n".join(
        f"| {row.tie_policy} | {row.band} | {float(row.events):g} | {fmt(row.rate_per_100_entity_years)} | "
        f"{fmt(row.size_adjusted_rate_per_100_entity_years)} |" for row in tie_bands.itertuples())
    assumptions = "\n".join(f"- `{policy}`: {TIE_POLICY_ASSUMPTIONS[policy]}."
                            for policy in (*TIE_POLICIES, *RANDOM_TIE_POLICIES))
    tie_text = (
        "### Same-date ties and unknown ordering\n\n"
        "A same-date tie is far more likely one incident deduplication has not joined than two incidents on "
        "one day; every such pair is listed, with the reason it is still two events, in "
        "`dashboard/close_pairs.html`. Until a tie is resolved, each tie policy states one assumption about it "
        "(`collapse`, the primary treatment, keeps every id in `timing_entity_date_ties.csv`):\n\n"
        f"{assumptions}\n\n"
        "None of these bounds the truth: a tie that is one incident filed twice is represented only by "
        "`collapse`, two incidents really weeks apart within a verified month exactly by none, and a tie "
        "whose dates are unverified has no interval to place them in. Agreement across the policies shows "
        f"only that a result does not hinge on these assumptions; `interval_draw` is the median of {draws} "
        "draws. "
        f"Ties found: {tie_summary_counts}. Incidents on different stored dates whose verified intervals "
        f"overlap are no tie, but their dates do not order them either: "
        f"{overlapping_interval_pairs(annotated, 'date_earliest', 'date_latest')}; the `known_intervals` "
        "scenario varies those dates within their intervals.\n\n"
        f"{tie_table_text}"
    )
    section = (
        "## Timing-band and date-precision sensitivity\n\n"
        "Rates are per 100 entity-years. Four band schemes, size-specific rates, and a kernel-smoothed rate "
        "are directly standardized to the fixed baseline organisation-size distribution, including UNKNOWN; "
        "see `timing_band_size_adjusted.csv`, `timing_band_size_strata.csv`, and `timing_smooth_size_adjusted.csv`. "
        "The smooth-curve interval resamples organisations and retains fixed weights. An estimate is unavailable "
        "when a nonzero-weight size stratum lacks exposure; no unsupported group is silently dropped. "
        f"Baseline size weights: {weights}. Recorded current size is an imperfect proxy, not a direct adjustment for "
        "visibility, attack surface or target value; size estimates may be stale or missing.\n\n"
        f"{main_table}\n\n"
        f"Precision evidence among victim-event attributions: {counts}. Verified month and year dates are "
        "drawn within their stored interval (`event_date_earliest` / `event_date_latest`); a date whose "
        "precision is unverified has no interval - it is unknown, not a day, and is kept as stored only in "
        "the scenarios that say so. An initial day of a month is ambiguous, not proof that the date was "
        "imputed. `timing_date_precision_size_adjusted.csv` compares verified month/year intervals, two "
        "separately labelled hypothetical ambiguous-first scenarios, complete histories with explicitly "
        f"day-precise metadata ({precise_entities} entities, {precise_repeating} with observed repeats), and "
        "complete histories in which every date has a verified interval (unverified organisations left out, "
        f"never read as days). {precision_note}Imputation percentiles show "
        "sensitivity to date assumptions, not confidence intervals for incidence.\n\n"
        f"{date_table}\n\n"
        f"{tie_text}\n\n"
        "A separate prospective size-adjusted protocol freezes the cohort and recorded size at registration, "
        "and compares 91–180 days with the adjacent 1–90 and 181–270 days using a conditional test within size strata. "
        "The fixed end is 2028-10-27 and at least 20 comparison events in supported strata are required. "
        f"Current size-adjusted validation status: **{size_validation['status']}**. "
        "The test assumes independent Poisson counts within size strata; shared incidents, sparse strata, "
        "mutable records and missing earlier history limit interpretation. The original unadjusted registered "
        "protocol is retained separately in `timing_peak_validation.json`; its definition has not been changed. "
        "Preserve `timing_size_adjusted_registration.json` for future validation.\n"
    )
    outputs = ["timing_band_sensitivity.csv", "timing_smooth_hazard.csv", "timing_band_size_adjusted.csv",
               "timing_band_size_strata.csv", "timing_smooth_size_adjusted.csv", "timing_date_precision_size_adjusted.csv",
               "timing_date_precision_sensitivity.csv", "timing_date_precision_coverage.csv", "timing_peak_validation.json",
               "timing_peak_size_adjusted_validation.json", "timing_entity_date_ties.csv",
               "timing_tie_policy_sensitivity.csv"]
    if not smooth.empty:
        outputs.append("timing_smooth_hazard.png")
    return {"section": section, "precision_counts": counts, "precise_entities": precise_entities,
            "ties": tie_summary_counts, "tie_policies": [*TIE_POLICIES, *RANDOM_TIE_POLICIES],
            "precise_repeating_entities": precise_repeating, "validation": validation, "size_validation": size_validation,
            "size_weights": weights, "size_reference_weights": weights,
            "band_schemes": list(SCHEMES), "date_imputations": draws,
            "outputs": outputs}
