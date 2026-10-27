"""Timing-band, smooth-hazard and incident-date precision sensitivity analyses.

All quantities describe *recorded* events among observed victims, not attacks
in the Australian population.  A first-of-month date is ambiguous, not proof
of a missing day.  Explicit precision is accepted only from a matching record's
metadata (or an input ``date_precision`` column); everything else is unknown.

The prospective protocol was defined on 27 October 2026.  A frozen cohort is
recorded on that date, even if the latest data snapshot is older; its cutoff
is recorded. A later first run cannot reconstruct a genuine prospective
cohort from a mutable database and is labelled unavailable, not confirmatory.
"""

from __future__ import annotations

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
    with sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True) as conn:
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


def annotate_precision(events: pd.DataFrame, evidence: dict[str, set[str]]) -> pd.DataFrame:
    out = events.copy()
    out["event_date"] = [_as_date(v) for v in out["event_date"]]
    labels = []
    for _, row in out.iterrows():
        explicit = set(evidence.get(str(row["deduplicated_event_id"]), set()))
        provided = _normalise_precision(row.get("date_precision"))
        if provided:
            explicit.add(provided)
        if len(explicit) == 1:
            labels.append("explicit_" + next(iter(explicit)))
        elif len(explicit) > 1:
            labels.append("conflicting_metadata")
        elif row["event_date"] and row["event_date"].day == 1:
            labels.append("ambiguous_first_of_month")
        else:
            labels.append("unknown")
    out["date_precision_basis"] = labels
    return out


def make_spells(events: pd.DataFrame, censor: date) -> pd.DataFrame:
    """Build spells with right censoring and same-day event collapse.

    Excluding future rows before pairing is essential: a later event must not
    be treated as observed beyond the censor date.
    """
    clean = events.copy()
    clean["event_date"] = pd.to_datetime(clean["event_date"], errors="coerce")
    clean = clean[clean["event_date"].notna() & (clean["event_date"] <= pd.Timestamp(censor))]
    clean = clean.drop_duplicates(["entity_id", "event_date"]).sort_values(["entity_id", "event_date"])
    next_date = clean.groupby("entity_id", sort=False)["event_date"].shift(-1)
    end = next_date.fillna(pd.Timestamp(censor))
    result = pd.DataFrame({"entity_id": clean["entity_id"].astype(str), "start": clean["event_date"],
                           "end": end, "duration_days": (end - clean["event_date"]).dt.days,
                           "event": next_date.notna().astype(int)})
    return result[result["duration_days"] > 0].reset_index(drop=True)


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


def impute_dates(events: pd.DataFrame, rng: np.random.Generator,
                 ambiguous_scenario: str = "keep", censor: Optional[date] = None) -> pd.DataFrame:
    """Draw once per incident, so all victims of that incident share a date."""
    out = events.copy()
    kinds = {"explicit_month", "explicit_year"}
    if ambiguous_scenario != "keep":
        kinds.add("ambiguous_first_of_month")
    unique = out[out["date_precision_basis"].isin(kinds)].drop_duplicates("deduplicated_event_id").copy()
    if unique.empty:
        return out
    days = pd.to_datetime(unique["event_date"], errors="coerce")
    yearly = unique["date_precision_basis"].eq("explicit_year") | (
        unique["date_precision_basis"].eq("ambiguous_first_of_month") & (ambiguous_scenario == "month_or_year") & days.dt.month.eq(1))
    first = days.dt.to_period("M").dt.start_time.where(~yearly, days.dt.to_period("Y").dt.start_time)
    last = days.dt.to_period("M").dt.end_time.dt.normalize().where(~yearly, days.dt.to_period("Y").dt.end_time.dt.normalize())
    if censor:
        last = last.clip(upper=pd.Timestamp(censor))
    lengths = (last - first).dt.days.to_numpy() + 1
    draw = first + pd.to_timedelta(rng.integers(0, lengths), unit="D")
    mapping = dict(zip(unique["deduplicated_event_id"], draw.dt.date))
    replacement = out["deduplicated_event_id"].map(mapping)
    out["event_date"] = replacement.where(replacement.notna(), out["event_date"])
    return out


def date_sensitivity(events: pd.DataFrame, censor: date, seed: int, draws: int = 100) -> pd.DataFrame:
    scenarios = [("stored_dates", "baseline"), ("known_intervals", "keep"),
                 ("ambiguous_firsts_as_month", "month"),
                 ("ambiguous_firsts_as_month_or_year", "month_or_year")]
    rows = []
    for label, scenario in scenarios:
        rng = np.random.default_rng(seed)
        n = 1 if label == "stored_dates" else draws
        samples = []
        for _ in range(n):
            shifted = events if label == "stored_dates" else impute_dates(events, rng, scenario, censor)
            rate = band_rates(make_spells(shifted, censor), PEAK_BOUNDS)
            samples.append(rate.set_index("band"))
        for band in samples[0].index:
            vals = np.array([float(s.loc[band, "rate_per_100_entity_years"]) if pd.notna(s.loc[band, "rate_per_100_entity_years"]) else np.nan for s in samples])
            valid = vals[np.isfinite(vals)]
            rows.append({"scenario": label, "band": band, "draws": n,
                         "median_rate_per_100_entity_years": float(np.median(valid)) if len(valid) else None,
                         "imputation_2_5_pct": float(np.percentile(valid, 2.5)) if len(valid) else None,
                         "imputation_97_5_pct": float(np.percentile(valid, 97.5)) if len(valid) else None})
    # Restrict entire entity histories, not just individual dates: deleting an
    # uncertain intermediate event would fabricate a first-to-third gap.
    fully_precise = events.groupby("entity_id")["date_precision_basis"].transform(lambda s: s.eq("explicit_day").all()).astype(bool)
    exact_events = events[fully_precise]
    rate = band_rates(make_spells(exact_events, censor), PEAK_BOUNDS)
    for _, r in rate.iterrows():
        rows.append({"scenario": "explicit_day_only_complete_histories", "band": r["band"], "draws": 1,
                     "median_rate_per_100_entity_years": r["rate_per_100_entity_years"],
                     "imputation_2_5_pct": None, "imputation_97_5_pct": None})
    return pd.DataFrame(rows)


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
    spells = make_spells(annotated, censor)
    tables = []
    for name, bounds in SCHEMES.items():
        frame = band_rates(spells, bounds)
        frame.insert(0, "scheme", name)
        tables.append(frame)
    bands = pd.concat(tables, ignore_index=True)
    bands.to_csv(out_dir / "timing_band_sensitivity.csv", index=False)
    smooth = smooth_hazard(spells, seed, bootstrap=int(kwargs.get("smooth_bootstrap", kwargs.get("bootstrap", 200))))
    smooth.to_csv(out_dir / "timing_smooth_hazard.csv", index=False)
    draws = max(1, int(kwargs.get("date_imputations", kwargs.get("date_draws", 100))))
    date_rates = date_sensitivity(annotated, censor, seed, draws=draws)
    date_rates.to_csv(out_dir / "timing_date_precision_sensitivity.csv", index=False)
    precision = annotated.groupby("date_precision_basis").size().rename("victim_event_attributions").reset_index()
    precision.to_csv(out_dir / "timing_date_precision_coverage.csv", index=False)
    validation = prospective_validation(annotated, seen, censor, out_dir)
    (out_dir / "timing_peak_validation.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")
    # A plot of the smooth estimate is useful alongside the machine-readable table.
    if not smooth.empty:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(9, 4.5))
        ax.plot(smooth["day"].to_numpy(), smooth["rate_per_100_entity_years"].to_numpy(), label="Smoothed recorded-event rate")
        ax.fill_between(smooth["day"].to_numpy(dtype=float), smooth["bootstrap_ci_low"].to_numpy(dtype=float),
                        smooth["bootstrap_ci_high"].to_numpy(dtype=float), alpha=0.2, label="Entity-bootstrap 95% interval")
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
    main_rows = bands[(bands["scheme"] == "pre_specified") & bands["band"].isin(["1-90", "91-180", "181-270"])]
    main_table = "| Days | Repeat events | Exposure days | Rate per 100 entity-years |\n|---|---:|---:|---:|\n" + "\n".join(
        f"| {row.band} | {row.events} | {row.exposure_days:.0f} | {fmt(row.rate_per_100_entity_years)} |"
        for row in main_rows.itertuples())
    date_rows = date_rates[date_rates["band"] == "91-180"]
    date_table = "| Date scenario | 91–180-day median rate | 2.5–97.5% of date draws |\n|---|---:|---:|\n" + "\n".join(
        f"| {row.scenario.replace('_', ' ')} | {fmt(row.median_rate_per_100_entity_years)} | {fmt(row.imputation_2_5_pct)}–{fmt(row.imputation_97_5_pct)} |"
        for row in date_rows.itertuples())
    section = (
        "## Timing-band and date-precision sensitivity\n\n"
        "Four fixed banding schemes and a kernel-smoothed rate with entity-bootstrap intervals "
        "are in `timing_band_sensitivity.csv` and `timing_smooth_hazard.csv`. These are unadjusted, "
        "descriptive rates for recorded incidents; the kernel interval reflects entity resampling.\n\n"
        f"{main_table}\n\n"
        f"Precision evidence among victim-event attributions: {counts}. An initial day of a month is "
        "ambiguous, not proof that the date was imputed. `timing_date_precision_sensitivity.csv` "
        "compares explicit month/year intervals, two separately labelled ambiguous-first scenarios, "
        f"and complete histories with explicitly day-precise metadata ({precise_entities} entities, "
        f"{precise_repeating} with observed repeats). {precision_note}Imputation percentiles show "
        "sensitivity to date assumptions, not confidence intervals for incidence.\n\n"
        f"{date_table}\n\n"
        "The pre-specified prospective comparison is 91–180 days versus the combined adjacent "
        "1–90 and 181–270 day windows, with a fixed end of 2028-10-27 and at least 20 observed "
        f"comparison events required before its conditional Poisson approximation. Current status: **{validation['status']}**. "
        "The approximation assumes independent Poisson counts; mutable records may have changed since "
        "registration, and events between an older snapshot and registration may be missing. "
        "The frozen cohort is never recreated retrospectively; see `timing_peak_validation.json`.\n"
    )
    outputs = ["timing_band_sensitivity.csv", "timing_smooth_hazard.csv",
               "timing_date_precision_sensitivity.csv", "timing_date_precision_coverage.csv", "timing_peak_validation.json"]
    if not smooth.empty:
        outputs.append("timing_smooth_hazard.png")
    return {"section": section, "precision_counts": counts, "precise_entities": precise_entities,
            "precise_repeating_entities": precise_repeating, "validation": validation,
            "band_schemes": list(SCHEMES), "date_imputations": draws,
            "outputs": outputs}
