"""Executive summary of the repeat-event timing analysis: five questions, answered from parsimonious models.

The adjusted piecewise model of ``analyze_recurrent_timing.py`` carries about 25 parameters (elapsed bands,
calendar period, prior event number, size score and an UNKNOWN indicator, sector, entity kind, records band)
for 120-200 repeats, and several of them are not identified (an UNKNOWN-size indicator with no repeats; sector
and entity kind both separating government). Its non-significant contrasts still come with a direction, and
a reader cannot tell a direction the data support from one the parameterisation produced. The questions on
the page are therefore answered here from models with few parameters, each fitted to the spells directly:

**Calendar time.** Recorded incidents grow steeply (11 victim events in 2019, over 200 a year from 2024), and
long gaps end, and first-year gaps start, in the dense recent years: elapsed time since an event and the long-term
growth are confounded unless calendar time is modelled finely. Four coarse calendar periods did not (most of the
growth is inside the last one). Every model below splits follow-up by calendar month (owner, 2026-11-02) and holds
a linear calendar trend in the per-organisation rate (:data:`PRIMARY_CALENDAR`, one parameter); the elapsed-time
result is re-tested under every :data:`CALENDAR_SPECS` adjustment - none, the linear trend, the log of monthly
reporting intensity (all recorded victim events in the 12 months centred on the month) and a separate effect for
every year from 2020 (earlier years pooled) - and the summary says whether it holds under all of them. Prior event
number (2 parameters) is held too: organisations attacked more often recur sooner, and without it a falling
population rate stands for differences between organisations.

1. **Is the data adequate?** Repeats, repeats per parameter (at least :data:`MIN_EVENTS_PER_PARAMETER`) of the
   models used here and of the full adjusted model, its terms that are not identified (standard error above
   :data:`NOT_IDENTIFIED_SE` on the log scale), the share of unverified incident dates, and whether a tie policy
   changes the verdict.
2. **The best elapsed-time splits.** Every banding of up to :data:`MAX_CUTS` cut points from
   :data:`CANDIDATE_CUTS` with at least :data:`MIN_REPEATS_PER_BAND` repeats in each band is scored by BIC
   (penalty log of the number of repeats; the conservative criterion, because the search itself overfits), with
   size, prior event number and reporting intensity held as an offset; a search-adjusted p-value comes from
   rerunning the whole search on repeats simulated under a constant rate. No supported cut: no split.
3a. **Short-term vigilance** (owner, 2026-11-02): an attack makes an organisation more vigilant for a while, so
   the adjusted rate 1-90 days after an event is lower than at other times. The same exact conditional test as the
   delayed peak, one-sided for "lower".
3. **A delayed peak after an event.** The adjusted rate 91-180 days after an event (the window of the earlier
   delayed-peak tests; the owner accepted it on 2026-11-02 - 91-120 alone holds too few repeats, and is reported as
   such) against all other elapsed time, by the exact
   conditional (binomial) test, one-sided for "higher"; a window with fewer than :data:`MIN_REPEATS_PER_BAND`
   repeats is not tested.
4. **The best fit for recurrence rates.** AIC of the adjusted Poisson models with elapsed time as nothing
   (constant rate), log elapsed time (Weibull-shaped), log elapsed time and its square (rise-and-fall, log-normal
   shaped) or the selected bands, each with size, prior event number and the calendar trend. The unadjusted parametric fits of the main analysis cannot take the reporting
   trend into account and are kept for reference only (``unadjusted_timing_models``).
5. **Does organisation size predict recurrence?** A Poisson model with the selected bands and size as three
   ordered steps SMALL < MEDIUM < LARGE < HUGE, all of one sign (a **monotone** effect: larger organisations are
   never less, or never more, at risk than smaller ones). It is fitted both ways and the better kept; its
   p-value against no size effect comes from a parametric bootstrap under the null, because the constrained
   likelihood ratio does not follow a chi-squared distribution. UNKNOWN-size spells are left out of the size
   and band models (they are counted) - an unknown size has no place in an order.

A result that is not significant is stated as "no evidence", never with a direction: the direction of an estimate
the data cannot distinguish from zero is not a finding.
"""

from __future__ import annotations

import itertools
import math
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy import optimize, stats
from scipy.special import gammaln

SIZE_ORDER = ("SMALL", "MEDIUM", "LARGE", "HUGE")
CANDIDATE_CUTS = (30, 60, 90, 120, 150, 180, 270, 365, 545, 730, 1095, 1460, 2190)
MAX_CUTS = 4
MIN_REPEATS_PER_BAND = 10
MIN_EVENTS_PER_PARAMETER = 10
MIN_REPEATS = 100
PEAK_WINDOW = (90, 120)
PEAK_WINDOWS = ((90, 180), (90, 120))
VIGILANCE_WINDOW = (0, 90)
CALENDAR_SPECS = ("linear_trend", "none", "log_intensity", "yearly_from_2020")
PRIMARY_CALENDAR = "linear_trend"
ALPHA = 0.05
NOT_IDENTIFIED_SE = 5.0
UNVERIFIED_DATE_SHARE_LIMIT = 0.5
DAYS_PER_100_YEARS = 36525.0


def _band_label(lo: float, hi: float) -> str:
    first = int(lo) + (0 if lo == 0 else 1)
    return f">{int(lo)}" if math.isinf(hi) else f"{first}-{int(hi)}"


def _p_text(p: float, replicates: Optional[int] = None) -> str:
    if replicates and p <= 1 / (replicates + 1) + 1e-12:
        return f"p <= {p:.3f} (no simulated value as extreme in {replicates})"
    return "p < 0.001" if p < 0.001 else f"p = {p:.3f}"


def interval_table(spells: pd.DataFrame, cuts: Sequence[float]) -> pd.DataFrame:
    """Repeats and exposure (days) per elapsed interval ``(lo, hi]``, organisation size and prior event number."""
    durations = spells["duration_days"].to_numpy(dtype=float)
    observed = spells["event"].to_numpy(dtype=int)
    size = spells["org_size"].fillna("UNKNOWN").astype(str).str.upper().to_numpy()
    number = (spells["event_number_cat"].astype(str).to_numpy() if "event_number_cat" in spells.columns
              else np.repeat("1", len(spells)))
    edges = [0.0, *[float(c) for c in cuts], math.inf]
    frames = []
    for index, (lo, hi) in enumerate(zip(edges, edges[1:])):
        frame = pd.DataFrame({
            "size": size,
            "event_number": number,
            "exposure_days": np.clip(np.minimum(durations, hi) - lo, 0, None),
            "events": ((durations > lo) & (durations <= hi) & (observed == 1)).astype(int),
        }).groupby(["size", "event_number"], as_index=False).sum()
        frame.insert(0, "interval", index)
        frame.insert(1, "lo", lo)
        frame.insert(2, "hi", hi)
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def _month(ordinal: int) -> int:
    day = date.fromordinal(int(ordinal))
    return day.year * 12 + day.month - 1


def _next_month_start(ordinal: int) -> int:
    day = date.fromordinal(int(ordinal))
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return date(year, month, 1).toordinal()


def reporting_intensity(spells: pd.DataFrame, window: int = 12) -> Dict[int, float]:
    """Recorded victim events (spell starts) in the ``window`` months centred on each month, by month index."""
    starts = pd.to_datetime(spells["prior_event_date"]).map(lambda d: d.year * 12 + d.month - 1)
    ends = pd.to_datetime(spells["prior_event_date"]) + pd.to_timedelta(spells["duration_days"], unit="D")
    first, last = int(starts.min()), int(ends.map(lambda d: d.year * 12 + d.month - 1).max())
    counts = starts.value_counts().reindex(range(first, last + 1), fill_value=0).astype(float)
    smoothed = counts.rolling(window, center=True, min_periods=1).sum()
    return {int(m): max(float(v), 0.5) for m, v in smoothed.items()}


def monthly_table(spells: pd.DataFrame, cuts: Sequence[float]) -> pd.DataFrame:
    """Repeats, exposure (days) and summed elapsed days per elapsed interval ``(lo, hi]``, calendar month,
    organisation size and prior event number, with ``log_intensity`` (:func:`reporting_intensity`) per month."""
    edges = [0.0, *[float(c) for c in cuts], math.inf]
    starts = pd.to_datetime(spells["prior_event_date"]).map(lambda d: d.toordinal()).to_numpy()
    durations = spells["duration_days"].to_numpy(dtype=float)
    observed = spells["event"].to_numpy(dtype=int)
    sizes = spells["org_size"].fillna("UNKNOWN").astype(str).str.upper().to_numpy()
    numbers = (spells["event_number_cat"].astype(str).to_numpy() if "event_number_cat" in spells.columns
               else np.repeat("1", len(spells)))
    cells: Dict[Tuple[int, int, str, str], List[float]] = {}
    for start, duration, event, size, number in zip(starts, durations, observed, sizes, numbers):
        for index, (lo, hi) in enumerate(zip(edges, edges[1:])):
            end = min(duration, hi)
            if end <= lo:
                break
            current, stop = start + int(lo), start + int(math.ceil(end))
            while current < stop:
                boundary = min(stop, _next_month_start(current))
                cell = cells.setdefault((index, _month(current), size, number), [0.0, 0.0, 0.0])
                length = boundary - current
                cell[1] += length
                cell[2] += length * ((current + boundary) / 2 - start)
                current = boundary
            if event and lo < duration <= hi:
                cell = cells.setdefault((index, _month(start + int(math.ceil(duration)) - 1), size, number),
                                        [0.0, 0.0, 0.0])
                cell[0] += 1
    out = pd.DataFrame([(i, m, s, n, *v) for (i, m, s, n), v in cells.items()],
                       columns=["interval", "month", "size", "event_number", "events", "exposure_days",
                                "elapsed_days_sum"])
    out = out[out["exposure_days"] > 0].copy()
    intensity = reporting_intensity(spells)
    out["log_intensity"] = np.log(out["month"].map(intensity).fillna(0.5).clip(lower=0.5))
    out["calendar_year"] = out["month"] / 12.0
    out["lo"] = out["interval"].map(lambda i: edges[int(i)])
    out["hi"] = out["interval"].map(lambda i: edges[int(i) + 1])
    return out.reset_index(drop=True)


def fit_poisson(y: np.ndarray, exposure: np.ndarray, x: np.ndarray,
                bounds: Optional[Sequence[Tuple[Optional[float], Optional[float]]]] = None) -> Dict[str, Any]:
    """Poisson log-linear rate model ``log mu = log exposure + x beta`` (column 0 the intercept), optionally bounded."""
    keep = exposure > 0
    y, exposure, x = y[keep].astype(float), exposure[keep].astype(float), x[keep]
    offset = np.log(exposure)

    def objective(beta: np.ndarray) -> Tuple[float, np.ndarray]:
        eta = offset + x @ beta
        mu = np.exp(np.clip(eta, -700, 50))
        return -float(y @ eta - mu.sum()), -(x.T @ (y - mu))

    start = np.zeros(x.shape[1])
    start[0] = math.log(max(y.sum(), 0.5) / exposure.sum())
    result = optimize.minimize(objective, start, jac=True, method="L-BFGS-B", bounds=bounds)
    beta = result.x
    mu = np.exp(np.clip(offset + x @ beta, -700, 50))
    return {"beta": beta, "log_likelihood": -float(result.fun) - float(gammaln(y + 1).sum()),
            "mu": mu, "converged": bool(result.success), "n_params": int(x.shape[1])}


def _known_size(table: pd.DataFrame) -> pd.DataFrame:
    return table[table["size"].isin(SIZE_ORDER)].copy()


def _size_steps(sizes: pd.Series) -> np.ndarray:
    rank = sizes.map({s: i for i, s in enumerate(SIZE_ORDER)}).to_numpy()
    return np.column_stack([(rank >= k).astype(float) for k in range(1, len(SIZE_ORDER))])


def _band_columns(table: pd.DataFrame, cuts: Sequence[float]) -> np.ndarray:
    """Indicator columns for every band of ``cuts`` but the first, from the fine intervals' ``lo``."""
    if not cuts:
        return np.zeros((len(table), 0))
    band = np.searchsorted(np.asarray(cuts, dtype=float), table["lo"].to_numpy(dtype=float), side="right")
    return np.column_stack([(band == b).astype(float) for b in range(1, len(cuts) + 1)])


def _number_columns(table: pd.DataFrame) -> np.ndarray:
    """Indicator columns for every prior-event-number category but the first (organisations attacked more often
    recur sooner: without them a falling population rate stands for differences between organisations)."""
    levels = sorted(table["event_number"].astype(str).unique())[1:]
    if not levels:
        return np.zeros((len(table), 0))
    return np.column_stack([(table["event_number"].astype(str) == level).astype(float) for level in levels])


def _calendar_columns(table: pd.DataFrame, spec: str = PRIMARY_CALENDAR) -> np.ndarray:
    """Calendar-time columns of the rate model (see :data:`CALENDAR_SPECS`); none when the table has no months."""
    if spec == "none" or "month" not in table.columns:
        return np.zeros((len(table), 0))
    if spec == "linear_trend":
        years = table["calendar_year"].to_numpy(dtype=float)
        return (years - years.mean())[:, None]
    if spec == "log_intensity":
        values = table["log_intensity"].to_numpy(dtype=float)
        return (values - values.mean())[:, None]
    if spec == "yearly_from_2020":
        year = (table["month"] // 12).to_numpy()
        levels = [y for y in range(2020, int(year.max())) if (year == y).any()]     # the latest year is the reference
        return np.column_stack([(year < 2020).astype(float), *[(year == y).astype(float) for y in levels]])
    raise ValueError(f"calendar spec must be one of {CALENDAR_SPECS}")


def monotone_size_fit(table: pd.DataFrame, cuts: Sequence[float] = (),
                      calendar: str = PRIMARY_CALENDAR) -> Dict[str, Any]:
    """Size as three same-signed steps, with prior event number and the bands of ``cuts``: both directions of the
    size effect fitted, the better one kept."""
    known = _known_size(table)
    y = known["events"].to_numpy(dtype=float)
    exposure = known["exposure_days"].to_numpy(dtype=float)
    numbers = _number_columns(known)
    calendar_columns = _calendar_columns(known, calendar)
    base = np.column_stack([np.ones(len(known)), _band_columns(known, cuts), numbers, calendar_columns])
    steps = _size_steps(known["size"])
    free = [(None, None)] * base.shape[1]
    null = fit_poisson(y, exposure, base)
    up = fit_poisson(y, exposure, np.column_stack([base, steps]), free + [(0, None)] * steps.shape[1])
    down = fit_poisson(y, exposure, np.column_stack([base, steps]), free + [(None, 0)] * steps.shape[1])
    unconstrained = fit_poisson(y, exposure, np.column_stack([base, steps]))
    best = up if up["log_likelihood"] >= down["log_likelihood"] else down
    n_bands = len(cuts)
    number_effects = best["beta"][1 + n_bands:1 + n_bands + numbers.shape[1]]
    band_effects = best["beta"][1:1 + n_bands]
    calendar_effect = best["beta"][1 + n_bands + numbers.shape[1]:base.shape[1]]
    # Each row's rate multiplier from everything but the intercept and the bands: what the band search and the
    # peak tests hold fixed.
    held = np.column_stack([numbers, calendar_columns, steps])
    row_multiplier = np.exp(held @ best["beta"][1 + n_bands:])
    levels = sorted(known["event_number"].astype(str).unique())
    increments = best["beta"][base.shape[1]:]
    cumulative = np.concatenate([[0.0], np.cumsum(increments)])
    free_increments = unconstrained["beta"][base.shape[1]:]
    direction = ("flat" if np.allclose(increments, 0, atol=1e-6)
                 else "increasing" if best is up else "decreasing")
    return {
        "direction": direction,
        "log_likelihood": best["log_likelihood"],
        "null_log_likelihood": null["log_likelihood"],
        "unconstrained_log_likelihood": unconstrained["log_likelihood"],
        "likelihood_ratio": max(0.0, 2 * (best["log_likelihood"] - null["log_likelihood"])),
        "rate_ratio_vs_small": {s: float(math.exp(v)) for s, v in zip(SIZE_ORDER, cumulative)},
        "unconstrained_rate_ratio_vs_small": {
            s: float(math.exp(v)) for s, v in zip(SIZE_ORDER, np.concatenate([[0.0], np.cumsum(free_increments)]))},
        "constraint_binds": bool(np.any(np.sign(free_increments) != np.sign(free_increments.sum()))
                                 and abs(unconstrained["log_likelihood"] - best["log_likelihood"]) > 1e-6),
        "n_params": int(base.shape[1] + steps.shape[1]),
        "event_number_rate_ratio": {level: float(math.exp(v)) for level, v in
                                    zip(levels, np.concatenate([[0.0], number_effects]))},
        "band_rate_ratio": [1.0, *[float(math.exp(v)) for v in band_effects]],
        "calendar": calendar,
        "calendar_coefficients": [float(v) for v in calendar_effect],
        "row_multiplier": row_multiplier,
        "null_mu": null["mu"],
        "_y_shape": (len(known),),
        "_design": (y, exposure, base, steps),
    }


def bootstrap_size_p(fit: Dict[str, Any], replicates: int, seed: int) -> float:
    """Parametric bootstrap p-value of the monotone size effect against no size effect."""
    y, exposure, base, steps = fit["_design"]
    keep = exposure > 0
    exposure, base, steps = exposure[keep], base[keep], steps[keep]
    rng = np.random.default_rng(seed)
    observed = fit["likelihood_ratio"]
    free = [(None, None)] * base.shape[1]
    full = np.column_stack([base, steps])
    exceed = 0
    for _ in range(replicates):
        simulated = rng.poisson(fit["null_mu"]).astype(float)
        null = fit_poisson(simulated, exposure, base)["log_likelihood"]
        up = fit_poisson(simulated, exposure, full, free + [(0, None)] * steps.shape[1])["log_likelihood"]
        down = fit_poisson(simulated, exposure, full, free + [(None, 0)] * steps.shape[1])["log_likelihood"]
        if 2 * (max(up, down) - null) >= observed - 1e-9:
            exceed += 1
    return (exceed + 1) / (replicates + 1)


def size_offsets(table: pd.DataFrame, fit: Dict[str, Any]) -> pd.Series:
    """Each known-size row's exposure times its fitted rate multiplier for size, prior event number and calendar
    time (held fixed in the band search and the delayed-peak tests). ``fit`` is a fit of the same ``table``."""
    known = _known_size(table)
    return known["exposure_days"] * fit["row_multiplier"]

def _bands_from_intervals(events: np.ndarray, exposure: np.ndarray, edges: Sequence[float],
                          cuts: Sequence[float]) -> Tuple[np.ndarray, np.ndarray]:
    band = np.searchsorted(np.asarray(cuts, dtype=float), np.asarray(edges[:-1], dtype=float), side="right")
    k = len(cuts) + 1
    return (np.bincount(band, weights=events, minlength=k), np.bincount(band, weights=exposure, minlength=k))


def _piecewise_log_likelihood(events: np.ndarray, exposure: np.ndarray) -> float:
    """Profile log-likelihood of a piecewise-constant rate (per day), as the spell-level censored likelihood."""
    rates = np.divide(events, exposure, out=np.zeros_like(events), where=exposure > 0)
    terms = np.where(events > 0, events * np.log(np.where(rates > 0, rates, 1.0)), 0.0)
    return float(terms.sum() - events.sum())


def band_search(table: pd.DataFrame, size_fit: Dict[str, Any], cuts: Sequence[float] = CANDIDATE_CUTS,
                max_cuts: int = MAX_CUTS, min_repeats: int = MIN_REPEATS_PER_BAND) -> pd.DataFrame:
    """Every admissible banding of ``cuts``, scored by BIC and AIC (size held as an offset), best first."""
    known = _known_size(table).assign(adjusted=size_offsets(table, size_fit))
    by_interval = known.groupby("interval").agg(events=("events", "sum"), adjusted=("adjusted", "sum"),
                                                lo=("lo", "first"), hi=("hi", "first")).sort_index()
    events = by_interval["events"].to_numpy(dtype=float)
    exposure = by_interval["adjusted"].to_numpy(dtype=float)
    edges = [*by_interval["lo"].tolist(), math.inf]
    n_repeats = float(events.sum())
    rows = []
    for n in range(0, max_cuts + 1):
        for combo in itertools.combinations(cuts, n):
            band_events, band_exposure = _bands_from_intervals(events, exposure, edges, combo)
            if band_events.min() < min_repeats:
                continue
            ll = _piecewise_log_likelihood(band_events, band_exposure)
            k = len(combo) + 1
            bounds = [0.0, *combo, math.inf]
            rows.append({"cuts": list(combo), "bands": ", ".join(_band_label(a, b) for a, b in zip(bounds, bounds[1:])),
                         "repeats_per_band": [int(v) for v in band_events], "n_params": k,
                         "log_likelihood": ll, "aic": -2 * ll + 2 * k, "bic": -2 * ll + k * math.log(n_repeats)})
    out = pd.DataFrame(rows).sort_values(["bic", "n_params"]).reset_index(drop=True)
    constant = out.loc[out["n_params"] == 1, "bic"]
    out["delta_bic_vs_constant"] = out["bic"] - (float(constant.iloc[0]) if len(constant) else math.nan)
    return out


def _admissible_combos(cuts: Sequence[float], max_cuts: int) -> List[Tuple[float, ...]]:
    return [combo for n in range(0, max_cuts + 1) for combo in itertools.combinations(cuts, n)]


def _best_delta_bic(events: np.ndarray, exposure: np.ndarray, edges: Sequence[float],
                    combos: Sequence[Tuple[float, ...]], min_repeats: int) -> float:
    """BIC of the best admissible banding minus that of the constant rate (negative: a banding beats it)."""
    n = float(events.sum())
    if n <= 0:
        return 0.0
    constant = -2 * _piecewise_log_likelihood(np.array([n]), np.array([exposure.sum()])) + math.log(n)
    best = constant
    for combo in combos:
        band_events, band_exposure = _bands_from_intervals(events, exposure, edges, combo)
        if band_events.min() < min_repeats:
            continue
        bic = -2 * _piecewise_log_likelihood(band_events, band_exposure) + (len(combo) + 1) * math.log(n)
        best = min(best, bic)
    return best - constant


def search_p_value(table: pd.DataFrame, size_fit: Dict[str, Any], observed_delta: float, replicates: int,
                   seed: int, cuts: Sequence[float] = CANDIDATE_CUTS, max_cuts: int = MAX_CUTS,
                   min_repeats: int = MIN_REPEATS_PER_BAND) -> float:
    """How often the whole band search, run on repeats simulated under a constant rate (size, prior event number
    and calendar period held fixed), finds a banding at least as much better than constant as the observed one."""
    known = _known_size(table).assign(adjusted=size_offsets(table, size_fit))
    rows_interval = known["interval"].to_numpy(dtype=int)
    adjusted = known["adjusted"].to_numpy(dtype=float)
    k = int(known["interval"].max()) + 1
    exposure = np.bincount(rows_interval, weights=adjusted, minlength=k)
    edges = [*known.groupby("interval")["lo"].first().reindex(range(k)).tolist(), math.inf]
    rate = known["events"].sum() / adjusted.sum()
    combos = _admissible_combos(cuts, max_cuts)
    rng = np.random.default_rng(seed)
    exceed = 0
    for _ in range(replicates):
        simulated = np.bincount(rows_interval, weights=rng.poisson(adjusted * rate), minlength=k)
        if _best_delta_bic(simulated, exposure, edges, combos, min_repeats) <= observed_delta + 1e-9:
            exceed += 1
    return (exceed + 1) / (replicates + 1)


def bic_evidence(delta: float) -> str:
    """Kass and Raftery's reading of a BIC difference in favour of a model."""
    gain = -delta
    return ("very strong" if gain > 10 else "strong" if gain > 6 else "positive" if gain > 2
            else "not worth more than a bare mention")


def peak_window_test(table: pd.DataFrame, size_fit: Dict[str, Any],
                     window: Tuple[float, float] = PEAK_WINDOW) -> Dict[str, Any]:
    """Adjusted rate in ``(lo, hi]`` days against all other elapsed time: exact conditional (binomial) test, both
    one-sided p-values (higher; lower) and the repeats expected in the window if its rate were no different."""
    lo, hi = window
    known = _known_size(table).assign(adjusted=size_offsets(table, size_fit))
    inside = (known["lo"] >= lo) & (known["hi"] <= hi)
    e_in, x_in = int(known.loc[inside, "events"].sum()), float(known.loc[inside, "adjusted"].sum())
    e_out, x_out = int(known.loc[~inside, "events"].sum()), float(known.loc[~inside, "adjusted"].sum())
    total = e_in + e_out
    share = x_in / (x_in + x_out)
    p_higher = float(stats.binomtest(e_in, total, share, alternative="greater").pvalue) if total else math.nan
    p_lower = float(stats.binomtest(e_in, total, share, alternative="less").pvalue) if total else math.nan
    ci = stats.binomtest(e_in, total, share).proportion_ci(confidence_level=0.95) if total else None

    def to_ratio(p: float) -> float:
        if p <= 0:
            return 0.0
        if p >= 1:
            return math.inf
        return (p / (1 - p)) * (x_out / x_in)

    return {"window": f"{int(lo) + 1}-{int(hi)}", "repeats_in_window": e_in, "repeats_elsewhere": e_out,
            "rate_ratio": (e_in / x_in) / (e_out / x_out) if e_out and x_in else math.nan,
            "ci_low": to_ratio(ci.low) if ci else math.nan, "ci_high": to_ratio(ci.high) if ci else math.nan,
            "p_one_sided_higher": p_higher, "p_one_sided_lower": p_lower,
            "expected_if_no_difference": total * share, "enough_repeats": e_in >= MIN_REPEATS_PER_BAND}


def timing_model_comparison(spells: pd.DataFrame, parametric: Sequence[Any], cuts: Sequence[float]) -> pd.DataFrame:
    """AIC of the constant rate, the selected banding and the parametric fits, on every spell, without size."""
    table = interval_table(spells, cuts).groupby("interval").agg(events=("events", "sum"),
                                                                 exposure=("exposure_days", "sum"))
    rows = [{"model": "constant rate (exponential)", "n_params": 1,
             "log_likelihood": _piecewise_log_likelihood(np.array([table["events"].sum()], dtype=float),
                                                         np.array([table["exposure"].sum()], dtype=float))}]
    if cuts:
        bounds = [0.0, *cuts, math.inf]
        rows.append({"model": "piecewise " + ", ".join(_band_label(a, b) for a, b in zip(bounds, bounds[1:])),
                     "n_params": len(cuts) + 1,
                     "log_likelihood": _piecewise_log_likelihood(table["events"].to_numpy(dtype=float),
                                                                 table["exposure"].to_numpy(dtype=float))})
    for fit in parametric:
        if fit.name != "exponential" and math.isfinite(fit.log_likelihood):
            rows.append({"model": fit.name.replace("_", " "), "n_params": fit.n_params,
                         "log_likelihood": fit.log_likelihood})
    out = pd.DataFrame(rows)
    out["aic"] = -2 * out["log_likelihood"] + 2 * out["n_params"]
    out = out.sort_values(["aic", "n_params"]).reset_index(drop=True)
    out["delta_aic"] = out["aic"] - out["aic"].iloc[0]
    return out


def adjusted_timing_models(table: pd.DataFrame, size_fit: Dict[str, Any], cuts: Sequence[float]) -> pd.DataFrame:
    """AIC of elapsed-time shapes, each fitted with size (monotone, the fitted direction), prior event number and
    the calendar trend: constant, log t (Weibull-shaped), log t + (log t)^2 (rise and fall), the selected bands."""
    known = _known_size(table)
    y = known["events"].to_numpy(dtype=float)
    exposure = known["exposure_days"].to_numpy(dtype=float)
    log_t = np.log(np.maximum(known["elapsed_days_sum"].to_numpy(dtype=float) / np.maximum(exposure, 1e-9), 1.0))
    log_t = log_t - log_t.mean()
    base = np.column_stack([np.ones(len(known)), _number_columns(known), _calendar_columns(known)])
    steps = _size_steps(known["size"])
    sign = (0, None) if size_fit["direction"] != "decreasing" else (None, 0)
    shapes = [("constant rate", np.zeros((len(known), 0))),
              ("log elapsed time (Weibull-shaped)", log_t[:, None]),
              ("log elapsed time and its square (rise and fall)", np.column_stack([log_t, log_t ** 2]))]
    if cuts:
        bounds = [0.0, *cuts, math.inf]
        shapes.append(("bands " + ", ".join(_band_label(a, b) for a, b in zip(bounds, bounds[1:])),
                       _band_columns(known, cuts)))
    rows = []
    for name, columns in shapes:
        x = np.column_stack([base, columns, steps])
        bounds_list = [(None, None)] * (base.shape[1] + columns.shape[1]) + [sign] * steps.shape[1]
        fit = fit_poisson(y, exposure, x, bounds_list)
        rows.append({"model": name, "n_params": int(x.shape[1]), "log_likelihood": fit["log_likelihood"]})
    out = pd.DataFrame(rows)
    out["aic"] = -2 * out["log_likelihood"] + 2 * out["n_params"]
    out = out.sort_values(["aic", "n_params"]).reset_index(drop=True)
    out["delta_aic"] = out["aic"] - out["aic"].iloc[0]
    return out


def calendar_robustness(table: pd.DataFrame, cuts: Sequence[float]) -> pd.DataFrame:
    """The selected bands against a constant rate, and the calendar terms against none, under every calendar
    adjustment: whether the elapsed-time result is the long-term growth in reporting in disguise."""
    rows = []
    no_calendar = monotone_size_fit(table, (), "none")["log_likelihood"]
    for spec in CALENDAR_SPECS:
        constant = monotone_size_fit(table, (), spec)
        banded = monotone_size_fit(table, cuts, spec) if cuts else constant
        lr = 2 * (banded["log_likelihood"] - constant["log_likelihood"])
        calendar_df = len(constant["calendar_coefficients"])
        calendar_lr = 2 * (constant["log_likelihood"] - no_calendar)
        rows.append({"calendar": spec, "calendar_params": calendar_df,
                     "calendar_lr": calendar_lr if calendar_df else None,
                     "calendar_p": float(stats.chi2.sf(calendar_lr, calendar_df)) if calendar_df else None,
                     "bands_lr": lr, "bands_df": len(cuts),
                     "bands_p": float(stats.chi2.sf(lr, len(cuts))) if cuts else None,
                     "band_rate_ratio": banded["band_rate_ratio"]})
    return pd.DataFrame(rows)


def adjusted_model_parameters(coefficients: pd.DataFrame, repeats: int) -> Dict[str, Any]:
    """Parameters of the full adjusted model, repeats per parameter, and terms that are not identified."""
    if coefficients is None or coefficients.empty:
        return {"n_params": 0, "events_per_parameter": math.nan, "not_identified": []}
    se = coefficients["std_error"].astype(float)
    bad = coefficients.loc[~np.isfinite(se) | (se > NOT_IDENTIFIED_SE), "term"].astype(str).tolist()
    return {"n_params": int(len(coefficients)), "events_per_parameter": repeats / len(coefficients),
            "not_identified": bad}


def _tie_verdicts(tie_sensitivity: Optional[pd.DataFrame]) -> Optional[str]:
    if tie_sensitivity is None or tie_sensitivity.empty:
        return None
    column = "adjusted_constant_hazard_p" if "adjusted_constant_hazard_p" in tie_sensitivity.columns else None
    if column is None or "repeats_from_ties" not in tie_sensitivity.columns:
        return None
    if int(tie_sensitivity["repeats_from_ties"].max()) == 0:
        return None
    verdicts = {row["tie_policy"]: bool(row[column] < ALPHA) for _, row in tie_sensitivity.iterrows()
                if pd.notna(row[column])}
    if len(set(verdicts.values())) > 1:
        return ("whether same-date incidents are one occurrence or separate ones changes the constant-rate verdict ("
                + ", ".join(f"{k}: {'rejected' if v else 'not rejected'}" for k, v in verdicts.items()) + ")")
    return None


def executive_summary(spells: pd.DataFrame, parametric: Sequence[Any], adjusted_coefficients: pd.DataFrame,
                      date_precision: Dict[str, int], tie_sensitivity: Optional[pd.DataFrame] = None,
                      replicates: int = 500, seed: int = 20260427,
                      fine: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    """The five questions with their answers (one sentence each) and the numbers behind them.

    ``fine`` overrides :func:`monthly_table` of ``spells`` at :data:`CANDIDATE_CUTS` (tests).
    """
    fine = monthly_table(spells, CANDIDATE_CUTS) if fine is None else fine
    repeats = int(spells["event"].sum())
    known = _known_size(fine)
    unknown_spells = int((~spells["org_size"].fillna("UNKNOWN").astype(str).str.upper().isin(SIZE_ORDER)).sum())
    unknown_repeats = repeats - int(known["events"].sum())

    size_only = monotone_size_fit(fine)
    search = band_search(fine, size_only)
    chosen = search.iloc[0]
    cuts = [float(c) for c in chosen["cuts"]]
    size_fit = monotone_size_fit(fine, cuts)
    size_p = bootstrap_size_p(size_fit, replicates, seed) if replicates > 0 else math.nan
    peaks = [peak_window_test(fine, size_fit, window) for window in PEAK_WINDOWS]
    vigilance = peak_window_test(fine, size_fit, VIGILANCE_WINDOW)
    timing = adjusted_timing_models(fine, size_fit, cuts)
    unadjusted_timing = timing_model_comparison(spells, parametric, cuts)
    full = adjusted_model_parameters(adjusted_coefficients, repeats)

    # 1. Adequacy
    params = size_fit["n_params"]
    epv = int(known["events"].sum()) / params
    total_dates = sum(date_precision.values()) or 1
    unverified = date_precision.get("unverified", 0) / total_dates
    problems, caveats = [], []
    if repeats < MIN_REPEATS:
        problems.append(f"only {repeats} repeats (at least {MIN_REPEATS} are needed for a model of a few parameters)")
    if epv < MIN_EVENTS_PER_PARAMETER:
        problems.append(f"{epv:.0f} repeats per parameter in the size model (at least {MIN_EVENTS_PER_PARAMETER})")
    tie_note = _tie_verdicts(tie_sensitivity)
    if tie_note:
        problems.append(tie_note)
    if full["n_params"] and (full["events_per_parameter"] < MIN_EVENTS_PER_PARAMETER or full["not_identified"]):
        caveats.append(f"the full adjusted model is over-parameterised ({full['n_params']} parameters, "
                       f"{full['events_per_parameter']:.1f} repeats each, {len(full['not_identified'])} not identified), "
                       "so it is not used for these conclusions")
    if unverified > UNVERIFIED_DATE_SHARE_LIMIT:
        caveats.append(f"{unverified:.0%} of incident dates are unverified, so gaps can be resolved only to about a "
                       "month: splits finer than 30 days are not attempted")
    if problems:
        adequacy = "Not adequate for a reliable recurrence model: " + "; ".join(problems) + "."
        adequate = False
    else:
        adequacy = (f"Adequate for a simple model: {repeats} repeats, {epv:.0f} per parameter of the models used here"
                    + ("; but " + "; ".join(caveats) if caveats else "") + ".")
        adequate = True

    # 2. Splits
    delta = float(chosen["delta_bic_vs_constant"])
    search_p = (search_p_value(fine, size_only, delta, replicates, seed + 1)
                if replicates > 0 and cuts else math.nan)
    band_rates = ", ".join(f"{v:.2f}" for v in size_fit["band_rate_ratio"])
    robustness = calendar_robustness(fine, cuts)
    trend = robustness.loc[robustness["calendar"] == "linear_trend"].iloc[0]
    yearly = robustness.loc[robustness["calendar"] == "yearly_from_2020"].iloc[0]
    trend_per_year = math.exp(size_fit["calendar_coefficients"][0]) - 1 if size_fit["calendar_coefficients"] else 0.0
    calendar_note = (
        f" The long-term growth in recorded incidents does not explain it: the per-organisation rate "
        + (f"changes by {trend_per_year:+.0%} a year (yearly effects from 2020, p = {yearly['calendar_p']:.2f})"
           if yearly["calendar_p"] is not None and yearly["calendar_p"] >= ALPHA else
           f"does change with calendar year (yearly effects from 2020, p = {yearly['calendar_p']:.3f})")
        + ", and the bands hold under no calendar adjustment, a linear trend, monthly reporting intensity and "
          "yearly effects alike (p from "
        + f"{robustness['bands_p'].min():.4f} to {robustness['bands_p'].max():.4f})."
        if cuts and robustness["bands_p"].max() < ALPHA else
        (" The bands do not hold under every calendar adjustment (p up to "
         f"{robustness['bands_p'].max():.3f}), so they may be the long-term growth in reporting in disguise."
         if cuts else ""))
    if len(cuts) == 0:
        splits = ("No split is supported: a single constant rate fits best (BIC), "
                  f"against every banding with at least {MIN_REPEATS_PER_BAND} repeats per band.")
    elif not (search_p < ALPHA):
        splits = (f"No split is supported beyond what the search finds by chance: the best of "
                  f"{len(search)} bandings ({chosen['bands']} days) beats a constant rate by {-delta:.1f} BIC, but "
                  f"searching constant-rate data does as well ({_p_text(search_p, replicates)}).")
    else:
        splits = (f"Best bands: {chosen['bands']} days, with rates {band_rates} times the first band's "
                  f"({bic_evidence(delta)} evidence: {-delta:.1f} BIC better than a constant rate, holding size, "
                  f"prior event number and a calendar trend; search-adjusted "
                  f"{_p_text(search_p, replicates)}).{calendar_note}")

    # 3a. Short-term vigilance
    if vigilance["repeats_in_window"] + vigilance["expected_if_no_difference"] < MIN_REPEATS_PER_BAND:
        vigilant = (f"Cannot be tested reliably: {vigilance['repeats_in_window']} repeats fall "
                    f"{vigilance['window']} days after an event and about "
                    f"{vigilance['expected_if_no_difference']:.0f} would be expected.")
    elif vigilance["p_one_sided_lower"] < ALPHA:
        vigilant = (f"Yes: the adjusted rate {vigilance['window']} days after an event is "
                    f"{vigilance['rate_ratio']:.2f} times the rate at other times (95% CI {vigilance['ci_low']:.2f}-"
                    f"{vigilance['ci_high']:.2f}; one-sided {_p_text(vigilance['p_one_sided_lower'])}; "
                    f"{vigilance['repeats_in_window']} repeats against {vigilance['expected_if_no_difference']:.1f} "
                    "expected), consistent with short-term vigilance.")
    else:
        vigilant = (f"No evidence of short-term vigilance: the adjusted rate {vigilance['window']} days after an event "
                    f"is not lower than at other times (one-sided {_p_text(vigilance['p_one_sided_lower'])}; "
                    f"{vigilance['repeats_in_window']} repeats against {vigilance['expected_if_no_difference']:.1f} "
                    "expected if the rate were no different).")

    # 3. Delayed peak
    main_peak, narrow = peaks[0], peaks[1]
    if main_peak["repeats_in_window"] < MIN_REPEATS_PER_BAND:
        delayed = (f"Cannot be tested reliably: only {main_peak['repeats_in_window']} repeats fall "
                   f"{main_peak['window']} days after an event (at least {MIN_REPEATS_PER_BAND} are needed).")
    elif main_peak["p_one_sided_higher"] < ALPHA:
        delayed = (f"Yes: the adjusted rate {main_peak['window']} days after an event is {main_peak['rate_ratio']:.2f} "
                   f"times the rate at other times (95% CI {main_peak['ci_low']:.2f}-{main_peak['ci_high']:.2f}; "
                   f"one-sided {_p_text(main_peak['p_one_sided_higher'])}; {main_peak['repeats_in_window']} repeats).")
    else:
        delayed = (f"No evidence of a delayed peak: the adjusted rate {main_peak['window']} days after an event is not "
                   f"higher than at other times (one-sided {_p_text(main_peak['p_one_sided_higher'])}; "
                   f"{main_peak['repeats_in_window']} repeats).")
    if narrow["repeats_in_window"] < MIN_REPEATS_PER_BAND:
        delayed += (f" The narrower {narrow['window']} day window holds only {narrow['repeats_in_window']} repeats, "
                    "too few to test on its own.")

    # 4. Best fit
    best = timing.iloc[0]
    close = timing.loc[(timing.index > 0) & (timing["delta_aic"] < 2), "model"].tolist()
    best_fit = (f"Best fit by AIC, holding size, prior event number and a calendar trend: {best['model']}"
                + (f" (within 2 AIC: {', '.join(close)}, so these fit about as well)" if close else "")
                + ".")
    constant_gap = float(timing.loc[timing["model"].str.startswith("constant"), "delta_aic"].iloc[0])
    if best["model"].startswith("constant") or (close and any(m.startswith("constant") for m in close)):
        best_fit += " The risk of a repeat does not change measurably with time since the last event."
    elif best["model"].startswith("bands") and not (search_p < ALPHA):
        best_fit += (" Its bands were chosen by the search, and the search-adjusted test does not support them, "
                     "so treat the rate as constant.")
    else:
        best_fit += f" The constant rate is {constant_gap:.1f} AIC behind."
        if best["model"].startswith("bands"):
            best_fit += " The bands were chosen by the search, so that margin is optimistic."

    # 5. Size
    rr = size_fit["rate_ratio_vs_small"]
    if size_fit["direction"] == "flat" or not (size_p < ALPHA):
        size = (f"No evidence that organisation size predicts recurrence (monotone size model, bootstrap "
                f"{_p_text(size_p, replicates)}).")
    else:
        steps = ", ".join(f"{name} {rr[name]:.1f}" for name in SIZE_ORDER[1:])
        size = (f"Yes: the recurrence rate {'rises' if size_fit['direction'] == 'increasing' else 'falls'} with "
                f"organisation size. Rate relative to SMALL organisations: {steps} (monotone model holding prior "
                f"event number and a calendar trend, bootstrap {_p_text(size_p, replicates)}). Size stands for "
                "visibility and exposure as much as size: this is prediction, not cause.")

    def clean(value: Any) -> Any:
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value

    return {
        "questions": [
            {"key": "adequacy", "question": "Is the data adequate for a reliable recurrence model?",
             "answer": adequacy, "adequate": adequate, "caveats": caveats},
            {"key": "splits", "question": "What are the best splits for recurrence time bands?", "answer": splits},
            {"key": "vigilance", "question": "Is the rate lower 1-90 days after an event (short-term vigilance)?",
             "answer": vigilant},
            {"key": "delayed_peak", "question": "Is there a delayed peak in recurrence 91-180 days after an event?",
             "answer": delayed},
            {"key": "best_fit", "question": "What fits recurrence rates best?", "answer": best_fit},
            {"key": "size", "question": "Does organisation size predict recurrence rates?", "answer": size},
        ],
        "repeats": repeats,
        "unknown_size": {"spells": unknown_spells, "repeats": unknown_repeats},
        "unverified_date_share": unverified,
        "full_adjusted_model": {k: clean(v) for k, v in full.items()},
        "band_search": {"criterion": "BIC (penalty log repeats), size as offset",
                        "min_repeats_per_band": MIN_REPEATS_PER_BAND, "max_cuts": MAX_CUTS,
                        "candidate_cuts": list(CANDIDATE_CUTS), "models_scored": int(len(search)),
                        "selected_cuts": [int(c) for c in cuts], "selected_bands": chosen["bands"],
                        "delta_bic_vs_constant": delta, "search_adjusted_p": clean(search_p),
                        "rates_per_100_organisation_years": band_rates,
                        "top": [{k: clean(v) for k, v in r.items()} for r in search.head(5).to_dict("records")]},
        "delayed_peak": [{k: clean(v) for k, v in peak.items()} for peak in peaks],
        "vigilance": {k: clean(v) for k, v in vigilance.items()},
        "timing_models": [{k: clean(v) for k, v in r.items()} for r in timing.to_dict("records")],
        "unadjusted_timing_models": [{k: clean(v) for k, v in r.items()} for r in unadjusted_timing.to_dict("records")],
        "calendar": {"primary": PRIMARY_CALENDAR, "coefficients": size_fit["calendar_coefficients"],
                     "robustness": [{k: clean(v) for k, v in r.items()} for r in robustness.to_dict("records")]},
        "size_model": {"direction": size_fit["direction"], "bootstrap_p": clean(size_p),
                       "bootstrap_replicates": replicates, "likelihood_ratio": size_fit["likelihood_ratio"],
                       "n_params": size_fit["n_params"], "events_per_parameter": epv,
                       "rate_ratio_vs_small": size_fit["rate_ratio_vs_small"],
                       "unconstrained_rate_ratio_vs_small": size_fit["unconstrained_rate_ratio_vs_small"],
                       "constraint_binds": size_fit["constraint_binds"], "bands": [int(c) for c in cuts]},
    }


def markdown_section(summary: Dict[str, Any]) -> List[str]:
    """The executive summary as Markdown lines for the report."""
    lines = ["## Executive Summary", ""]
    for q in summary["questions"]:
        lines += [f"**{q['question']}** {q['answer']}", ""]
    return lines
