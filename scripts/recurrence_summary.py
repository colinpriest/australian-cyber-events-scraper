"""Executive summary of the repeat-event timing analysis: five questions, answered from parsimonious models.

The adjusted piecewise model of ``analyze_recurrent_timing.py`` carries about 25 parameters (elapsed bands,
calendar period, prior event number, size score and an UNKNOWN indicator, sector, entity kind, records band)
for 120-200 repeats, and several of them are not identified (an UNKNOWN-size indicator with no repeats; sector
and entity kind both separating government). Its non-significant contrasts still come with a direction, and
a reader cannot tell a direction the data support from one the parameterisation produced. The questions on
the page are therefore answered here from models with few parameters, each fitted to the spells directly:

1. **Is the data adequate?** Repeats, repeats per parameter (at least :data:`MIN_EVENTS_PER_PARAMETER`) of the
   models used here and of the full adjusted model, its terms that are not identified (standard error above
   :data:`NOT_IDENTIFIED_SE` on the log scale), the share of unverified incident dates, and whether a tie policy
   changes the verdict.
2. **The best elapsed-time splits.** Every banding of up to :data:`MAX_CUTS` cut points from
   :data:`CANDIDATE_CUTS` with at least :data:`MIN_REPEATS_PER_BAND` repeats in each band is scored by BIC
   (penalty log of the number of repeats; the conservative criterion, because the search itself overfits), with
   organisation size held as an offset. No cut beating the constant rate means no split is supported.
3. **A delayed peak 91-120 days after an event.** The size-adjusted rate in that window against all other elapsed
   time, by the exact conditional (binomial) test, one-sided for "higher".
4. **The best fit for recurrence rates.** AIC of the constant rate (exponential), the selected banding and the
   parametric models (Weibull, log-normal, log-logistic, generalised gamma), all on every spell, without size.
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


def table_from_split(split: pd.DataFrame, cuts: Sequence[float]) -> pd.DataFrame:
    """The fine-interval table from ``analyze_recurrent_timing.adjusted_poisson_split`` run with elapsed bounds
    ``0, *cuts, inf``: repeats and exposure per interval, calendar period, size and prior event number."""
    edges = [0.0, *[float(c) for c in cuts], math.inf]
    out = (split.assign(size=split["org_size"].fillna("UNKNOWN").astype(str).str.upper(),
                        event_number=split["event_number_cat"].astype(str),
                        calendar=split["calendar_band"].astype(str))
           .groupby(["elapsed_band_index", "calendar", "size", "event_number"], as_index=False)
           .agg(events=("events", "sum"), exposure_days=("exposure_days", "sum"))
           .rename(columns={"elapsed_band_index": "interval"}))
    out["lo"] = out["interval"].map(lambda i: edges[int(i)])
    out["hi"] = out["interval"].map(lambda i: edges[int(i) + 1])
    return out


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


def _calendar_columns(table: pd.DataFrame) -> np.ndarray:
    """Indicator columns for every calendar period but the one with the most repeats. Reporting is far denser in
    recent years, and recent spells are short: without them the first year after an event looks riskier."""
    if "calendar" not in table.columns:
        return np.zeros((len(table), 0))
    totals = table.groupby("calendar")["events"].sum().sort_values(ascending=False)
    levels = sorted(totals.index[1:])
    if not levels:
        return np.zeros((len(table), 0))
    return np.column_stack([(table["calendar"] == level).astype(float) for level in levels])


def monotone_size_fit(table: pd.DataFrame, cuts: Sequence[float] = ()) -> Dict[str, Any]:
    """Size as three same-signed steps, with prior event number and the bands of ``cuts``: both directions of the
    size effect fitted, the better one kept."""
    known = _known_size(table)
    y = known["events"].to_numpy(dtype=float)
    exposure = known["exposure_days"].to_numpy(dtype=float)
    numbers = _number_columns(known)
    calendar = _calendar_columns(known)
    base = np.column_stack([np.ones(len(known)), _band_columns(known, cuts), numbers, calendar])
    steps = _size_steps(known["size"])
    free = [(None, None)] * base.shape[1]
    null = fit_poisson(y, exposure, base)
    up = fit_poisson(y, exposure, np.column_stack([base, steps]), free + [(0, None)] * steps.shape[1])
    down = fit_poisson(y, exposure, np.column_stack([base, steps]), free + [(None, 0)] * steps.shape[1])
    unconstrained = fit_poisson(y, exposure, np.column_stack([base, steps]))
    best = up if up["log_likelihood"] >= down["log_likelihood"] else down
    n_bands = len(cuts)
    number_effects = best["beta"][1 + n_bands:1 + n_bands + numbers.shape[1]]
    calendar_effects = best["beta"][1 + n_bands + numbers.shape[1]:base.shape[1]]
    levels = sorted(known["event_number"].astype(str).unique())
    if "calendar" in known.columns:
        totals = known.groupby("calendar")["events"].sum().sort_values(ascending=False)
        calendar_levels = [totals.index[0], *sorted(totals.index[1:])]
    else:
        calendar_levels = []
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
        "calendar_rate_ratio": {level: float(math.exp(v)) for level, v in
                                zip(calendar_levels, np.concatenate([[0.0], calendar_effects]))},
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
    """Each known-size row's exposure multiplied by its size's, prior event number's and calendar period's rate
    ratios (held fixed in the band search and the delayed-peak test)."""
    known = _known_size(table)
    out = (known["exposure_days"] * known["size"].map(fit["rate_ratio_vs_small"])
           * known["event_number"].astype(str).map(fit["event_number_rate_ratio"]))
    if "calendar" in known.columns and fit.get("calendar_rate_ratio"):
        out = out * known["calendar"].map(fit["calendar_rate_ratio"])
    return out


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
    """Size-adjusted rate in ``(lo, hi]`` days against all other elapsed time: exact conditional test."""
    lo, hi = window
    known = _known_size(table).assign(adjusted=size_offsets(table, size_fit))
    inside = (known["lo"] >= lo) & (known["hi"] <= hi)
    e_in, x_in = int(known.loc[inside, "events"].sum()), float(known.loc[inside, "adjusted"].sum())
    e_out, x_out = int(known.loc[~inside, "events"].sum()), float(known.loc[~inside, "adjusted"].sum())
    total = e_in + e_out
    share = x_in / (x_in + x_out)
    p_higher = float(stats.binomtest(e_in, total, share, alternative="greater").pvalue) if total else math.nan
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
            "p_one_sided_higher": p_higher, "enough_repeats": e_in >= MIN_REPEATS_PER_BAND}


def timing_band_rates(spells: pd.DataFrame, cuts: Sequence[float]) -> str:
    """Unadjusted repeats per 100 organisation-years in each band of ``cuts``, as text."""
    table = interval_table(spells, cuts).groupby("interval").agg(events=("events", "sum"),
                                                                 exposure=("exposure_days", "sum"))
    rates = table["events"] / table["exposure"] * DAYS_PER_100_YEARS
    return ", ".join(f"{r:.1f}" for r in rates)


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

    ``fine`` is :func:`table_from_split` of the adjusted split at ``0, *CANDIDATE_CUTS, inf`` (with calendar
    periods); without it the models are not adjusted for calendar period.
    """
    fine = interval_table(spells, CANDIDATE_CUTS) if fine is None else fine
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
    peak = peak_window_test(fine, size_fit)
    timing = timing_model_comparison(spells, parametric, cuts)
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
    band_rates = timing_band_rates(spells, cuts)
    if len(cuts) == 0:
        splits = ("No split is supported: a single constant rate fits best (BIC), "
                  f"against every banding with at least {MIN_REPEATS_PER_BAND} repeats per band.")
    elif not (search_p < ALPHA):
        splits = (f"No split is supported beyond what the search finds by chance: the best of "
                  f"{len(search)} bandings ({chosen['bands']} days) beats a constant rate by {-delta:.1f} BIC, but "
                  f"searching constant-rate data does as well ({_p_text(search_p, replicates)}).")
    else:
        splits = (f"Best bands: {chosen['bands']} days, with {band_rates} repeats per 100 organisation-years "
                  f"({bic_evidence(delta)} evidence: {-delta:.1f} BIC better than a constant rate, holding size, "
                  f"prior event number and calendar period; search-adjusted {_p_text(search_p, replicates)}).")

    # 3. Delayed peak
    if peak["repeats_in_window"] < MIN_REPEATS_PER_BAND:
        delayed = (f"Cannot be tested reliably: only {peak['repeats_in_window']} repeats fall {peak['window']} days "
                   f"after an event (at least {MIN_REPEATS_PER_BAND} are needed).")
    elif peak["p_one_sided_higher"] < ALPHA:
        delayed = (f"Yes: the size-adjusted rate {peak['window']} days after an event is {peak['rate_ratio']:.2f} times "
                   f"the rate at other times (95% CI {peak['ci_low']:.2f}-{peak['ci_high']:.2f}; one-sided "
                   f"{_p_text(peak['p_one_sided_higher'])}; {peak['repeats_in_window']} repeats).")
    else:
        delayed = (f"No evidence of a delayed peak: the rate {peak['window']} days after an event is not higher than at "
                   f"other times (one-sided {_p_text(peak['p_one_sided_higher'])}; {peak['repeats_in_window']} repeats).")

    # 4. Best fit
    best = timing.iloc[0]
    close = timing.loc[(timing.index > 0) & (timing["delta_aic"] < 2), "model"].tolist()
    best_fit = (f"Best fit by AIC: {best['model']}"
                + (f" (within 2 AIC: {', '.join(close)}, so these fit about as well)" if close else "")
                + ".")
    constant_gap = float(timing.loc[timing["model"].str.startswith("constant"), "delta_aic"].iloc[0])
    if best["model"].startswith("constant") or (close and any(m.startswith("constant") for m in close)):
        best_fit += " The risk of a repeat does not change measurably with time since the last event."
    elif best["model"].startswith("piecewise") and not (search_p < ALPHA):
        best_fit += (" Its bands were chosen by the search, and the search-adjusted test does not support them, "
                     "so treat the rate as constant.")
    else:
        best_fit += (f" Unadjusted, on every spell, the constant rate is {constant_gap:.1f} AIC behind; the bands "
                     "were chosen by the search, so that margin is optimistic.")

    # 5. Size
    rr = size_fit["rate_ratio_vs_small"]
    if size_fit["direction"] == "flat" or not (size_p < ALPHA):
        size = (f"No evidence that organisation size predicts recurrence (monotone size model, bootstrap "
                f"{_p_text(size_p, replicates)}).")
    else:
        steps = ", ".join(f"{name} {rr[name]:.1f}" for name in SIZE_ORDER[1:])
        size = (f"Yes: the recurrence rate {'rises' if size_fit['direction'] == 'increasing' else 'falls'} with "
                f"organisation size. Rate relative to SMALL organisations: {steps} (monotone model holding prior "
                f"event number and calendar period, bootstrap {_p_text(size_p, replicates)}). Size stands for "
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
            {"key": "delayed_peak", "question": "Is there a delayed peak in recurrence 90-120 days after an event?",
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
        "delayed_peak": {k: clean(v) for k, v in peak.items()},
        "timing_models": [{k: clean(v) for k, v in r.items()} for r in timing.to_dict("records")],
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
