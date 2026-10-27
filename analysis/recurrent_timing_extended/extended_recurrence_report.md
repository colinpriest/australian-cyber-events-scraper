# Extended recurrence analyses

Observation cutoff: 2026-10-27. Seed: 20261027.

The analyses describe recorded incidents among organisations with at least one recorded victim event. They do not estimate the probability of an attack across all Australian organisations.

Retained 635 victim-event attributions; excluded 0 after the incident-date cutoff and 0 first discovered after it. 0 retained attributions have unknown discovery dates.

Historical cutoffs filter known discovery dates, but the snapshot contains present-day incident links, fields, and classifications. It is not a complete historical database snapshot; retrospective and prospective interpretations must account for that limitation.

## Timing-band and date-precision sensitivity

Four fixed banding schemes and a kernel-smoothed rate with entity-bootstrap intervals are in `timing_band_sensitivity.csv` and `timing_smooth_hazard.csv`. These are unadjusted, descriptive rates for recorded incidents; the kernel interval reflects entity resampling.

| Days | Repeat events | Exposure days | Rate per 100 entity-years |
|---|---:|---:|---:|
| 1-90 | 4 | 56281 | 2.60 |
| 91-180 | 10 | 51777 | 7.05 |
| 181-270 | 4 | 46841 | 3.12 |

Precision evidence among victim-event attributions: {'unknown': 436, 'ambiguous_first_of_month': 199}. An initial day of a month is ambiguous, not proof that the date was imputed. `timing_date_precision_sensitivity.csv` compares explicit month/year intervals, two separately labelled ambiguous-first scenarios, and complete histories with explicitly day-precise metadata (0 entities, 0 with observed repeats). Insufficient explicit date precision for a reliable precisely dated subset. Imputation percentiles show sensitivity to date assumptions, not confidence intervals for incidence.

| Date scenario | 91–180-day median rate | 2.5–97.5% of date draws |
|---|---:|---:|
| stored dates | 7.05 | 7.05–7.05 |
| known intervals | 7.05 | 7.05–7.05 |
| ambiguous firsts as month | 7.06 | 4.94–8.47 |
| ambiguous firsts as month or year | 7.06 | 4.94–8.85 |
| explicit day only complete histories | — | —–— |

The pre-specified prospective comparison is 91–180 days versus the combined adjacent 1–90 and 181–270 day windows, with a fixed end of 2028-10-27 and at least 20 observed comparison events required before its conditional Poisson approximation. Current status: **pending**. The approximation assumes independent Poisson counts; mutable records may have changed since registration, and events between an older snapshot and registration may be missing. The frozen cohort is never recreated retrospectively; see `timing_peak_validation.json`.

### Linkage and shared-incident sensitivity

The scenarios below are independent alternatives to the baseline. Rates are recorded repeat events per 100 entity-years at risk; rate ratios compare days 91–180 with days 0–90.

| Scenario | Status | Decisions applied | Victim attributions | Incidents | 0–90 events / days | 91–180 events / days | Rate ratio |
|---|---|---:|---:|---:|---:|---:|---:|
| baseline | observed cohort | 0 | 635 | 558 | 4 / 56281 | 10 / 51777 | 2.72 |
| active_same_overrides | evaluated | 2 | 635 | 556 | 4 / 56296 | 10 / 51831 | 2.72 |
| model_same_probability_ge_0.9 | no applicable candidate decisions | 0 | 635 | 558 | 4 / 56281 | 10 / 51777 | 2.72 |
| model_victim_probability_lt_0.5_excluded | evaluated | 25 | 610 | 547 | 2 / 54062 | 5 / 49967 | 2.70 |

Stored scores cover 97 in-cohort incident pairs and 110 victim attributions. Active different-incident overrides vetoed 0 override merges and 0 model-candidate merges, including conflicts created transitively. 0 current incident(s) contain an active different-incident override; these cannot be split reliably without per-report victim and date attribution.
Model scores are uncalibrated stored judgments, not confirmed truth; thresholds 0.9 and 0.5 define sensitivity scenarios, not validation. A merged incident uses the earliest observed date and the union of attributed victims. Evidence can be stale. Missing judgment or override tables are reported as unavailable. These scenarios cannot identify missing judgments or unrecorded victims.

41 recorded incidents have multiple attributed victims, involving 109 distinct victims and 163 event-level victim pairs; 14 shared incidents have a supplier name recorded. Supplier names alone are not counted as victims. The observed network has 498 components, largest 20 organisations.
The bootstrap compares resampling organisations with resampling connected components (200 requested replicates):

| Resampling unit | Metric | Estimate | 95% percentile interval | Valid replicates |
|---|---|---:|---:|---:|
| entity | rate_0_90_per_100_years | 2.60 | 0.00–6.45 | 200 |
| entity | rate_91_180_per_100_years | 7.05 | 2.16–12.22 | 200 |
| entity | rate_ratio | 2.72 | 0.87–12.97 | 191 |
| shared_incident_component | rate_0_90_per_100_years | 2.60 | 0.00–7.13 | 200 |
| shared_incident_component | rate_91_180_per_100_years | 7.05 | 0.78–13.14 | 200 |
| shared_incident_component | rate_ratio | 2.72 | 1.24–9.51 | 168 |

Components connect organisations through any recorded shared incident, not only incidents with a named supplier. Percentile intervals reflect observed linkage only; few or dominant components and invalid ratio replicates limit inference. Missing shared incidents are not corrected.

## Event order and attack-type transitions

Rates compare observed incident histories. Spells end at the next recorded incident or the censor date; censored spells contribute exposure. Exact Poisson intervals are descriptive and do not account for dependence between repeated incidents at one organisation.

| event order | spells | organisations | repeats | censored | exposure days | rate per 100 entity years | poisson ci low | poisson ci high |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| After first observed incident | 574 | 574 | 41 | 533 | 617380.00 | 2.43 | 1.74 | 3.29 |
| After second or later observed incident | 61 | 41 | 20 | 41 | 42737.00 | 17.09 | 10.44 | 26.40 |

The next table is conditional on a subsequent observed incident. Unknown types remain Unknown; different or unknown types on the same day are labelled Multiple or ambiguous rather than ordered.

| prior attack type | next attack type | transitions | organisations | share of prior type |
| --- | --- | --- | --- | --- |
| Data Breach | Data Breach | 26 | 22 | 0.59 |
| Data Breach | Denial of Service | 3 | 3 | 0.07 |
| Data Breach | Financial Fraud | 1 | 1 | 0.02 |
| Data Breach | Infrastructure Attack | 1 | 1 | 0.02 |
| Data Breach | Other | 3 | 3 | 0.07 |
| Data Breach | Phishing | 2 | 2 | 0.05 |
| Data Breach | Ransomware | 4 | 4 | 0.09 |
| Data Breach | State-Sponsored Attack | 2 | 2 | 0.05 |
| Data Breach | Supply Chain Attack | 1 | 1 | 0.02 |
| Data Breach | Vulnerability Exploit | 1 | 1 | 0.02 |
| Denial of Service | Data Breach | 1 | 1 | 0.33 |
| Denial of Service | State-Sponsored Attack | 2 | 2 | 0.67 |
| Financial Fraud | Data Breach | 1 | 1 | 1.00 |
| Other | Data Breach | 2 | 2 | 0.67 |
| Other | Ransomware | 1 | 1 | 0.33 |
| Ransomware | Data Breach | 3 | 3 | 0.75 |
| Ransomware | Ransomware | 1 | 1 | 0.25 |
| State-Sponsored Attack | Data Breach | 4 | 3 | 0.80 |
| State-Sponsored Attack | Other | 1 | 1 | 0.20 |
| Vulnerability Exploit | Data Breach | 1 | 1 | 1.00 |

## Reporting and observation bias

Source history: available; 0 event IDs with all known discovery dates after the cutoff excluded; 0 source records lack discovery dates. Organisation sizes are current recorded estimates and may not describe their size at the incident date.

Source groups refer to the prior incident, are mutually exclusive, and show source availability—not the chance that an incident was reported. Calendar groups are the prior incident's cohort, not calendar-time-adjusted rate ratios. Missing source history is Unknown.

| dimension | group | spells | organisations | repeats | censored | exposure days | rate per 100 entity years | poisson ci low | poisson ci high |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Source of prior incident | GoogleSearch | 88 | 88 | 3 | 85 | 63054.00 | 1.74 | 0.36 | 5.08 |
| Source of prior incident | Multiple sources | 253 | 231 | 37 | 216 | 259922.00 | 5.20 | 3.66 | 7.17 |
| Source of prior incident | OAIC | 6 | 6 | 0 | 6 | 12860.00 | 0.00 | 0.00 | 10.48 |
| Source of prior incident | Perplexity | 145 | 141 | 10 | 135 | 124595.00 | 2.93 | 1.41 | 5.39 |
| Source of prior incident | WebberInsurance | 143 | 141 | 11 | 132 | 199686.00 | 2.01 | 1.00 | 3.60 |
| Calendar cohort of prior incident | 2019–2021 | 131 | 126 | 22 | 109 | 264965.00 | 3.03 | 1.90 | 4.59 |
| Calendar cohort of prior incident | 2022–2023 | 128 | 126 | 15 | 113 | 164278.00 | 3.34 | 1.87 | 5.50 |
| Calendar cohort of prior incident | 2024 onward | 358 | 341 | 17 | 341 | 171105.00 | 3.63 | 2.11 | 5.81 |
| Calendar cohort of prior incident | Through 2018 | 18 | 18 | 7 | 11 | 59769.00 | 4.28 | 1.72 | 8.81 |
| Recorded organisation size | HUGE | 169 | 136 | 33 | 136 | 223598.00 | 5.39 | 3.71 | 7.57 |
| Recorded organisation size | LARGE | 189 | 172 | 17 | 172 | 193144.00 | 3.21 | 1.87 | 5.15 |
| Recorded organisation size | MEDIUM | 170 | 166 | 4 | 166 | 130771.00 | 1.12 | 0.30 | 2.86 |
| Recorded organisation size | SMALL | 52 | 51 | 1 | 51 | 53914.00 | 0.68 | 0.02 | 3.77 |
| Recorded organisation size | UNKNOWN | 55 | 49 | 6 | 49 | 58690.00 | 3.73 | 1.37 | 8.13 |

The following simulations remove some already recorded follow-ups under stated hypothetical observation probabilities: 75% for all follow-ups, or 50% for small and medium organisations and 100% for other recorded sizes, including Unknown. First observed incidents are retained. They illustrate sensitivity and do not estimate or reconstruct unreported attacks.

| scenario | replicates | median recorded repeats | p05 recorded repeats | p95 recorded repeats | median rate per 100 entity years | p05 rate | p95 rate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| uniform 75% follow-up observation | 100 | 46.00 | 39.95 | 51.05 | 2.55 | 2.21 | 2.82 |
| lower observation for small organisations | 100 | 59.00 | 57.00 | 61.00 | 3.26 | 3.15 | 3.38 |

The prior-history scenarios hypothetically assign an unobserved earlier incident to a fraction of organisations; they relabel existing spells without fabricating dates or exposure. They are not corrections or estimates of the true unobserved history.

| scenario | replicates | order group | median repeats | p05 repeats | p95 repeats | median rate per 100 entity years | p05 rate | p95 rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0% of organisations had an unobserved earlier incident | 1 | First | 41.00 | 41.00 | 41.00 | 2.43 | 2.43 | 2.43 |
| 0% of organisations had an unobserved earlier incident | 1 | Later | 20.00 | 20.00 | 20.00 | 17.09 | 17.09 | 17.09 |
| 25% of organisations had an unobserved earlier incident | 100 | First | 31.00 | 25.95 | 35.00 | 2.42 | 2.02 | 2.74 |
| 25% of organisations had an unobserved earlier incident | 100 | Later | 30.00 | 26.00 | 35.05 | 5.74 | 4.79 | 6.92 |
| 50% of organisations had an unobserved earlier incident | 100 | First | 21.00 | 15.00 | 26.00 | 2.47 | 1.72 | 3.04 |
| 50% of organisations had an unobserved earlier incident | 100 | Later | 40.00 | 35.00 | 46.00 | 4.15 | 3.64 | 4.97 |

These are associations in a selected, publicly reported dataset, conditional on an observed first incident. They do not establish causal effects or population attack risk. Sparse groups and reporting changes can be important.

## Reproducibility

Database snapshot SHA-256: `e057fb6d6fa3ba7cbac9c35fee530097d86a597342e3395924488e3f98edde55`.
Bootstrap replicates: 200; date draws: 100.

The detailed result tables are saved as CSV files alongside this report.
