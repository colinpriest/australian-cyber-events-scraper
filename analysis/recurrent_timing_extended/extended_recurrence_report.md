# Extended recurrence analyses

Observation cutoff: 2026-10-28. Seed: 20261027.

The analyses describe recorded incidents among organisations with at least one recorded victim event. They do not estimate the probability of an attack across all Australian organisations.

Organisation size is included in comparisons using the recorded size categories. The standard population is the organisations in this observed cohort, counted once each; UNKNOWN remains a separate category. Size is an imperfect proxy for visibility, attack exposure and target value, which are not directly measured. Current size estimates may differ from size at the incident date, and residual confounding remains possible.

| Recorded size | Organisations | Standard population weight |
|---|---:|---:|
| SMALL | 49 | 8.0% |
| MEDIUM | 166 | 27.0% |
| LARGE | 177 | 28.8% |
| HUGE | 134 | 21.8% |
| UNKNOWN | 89 | 14.5% |

Retained 677 victim-event attributions; excluded 0 after the incident-date cutoff and 0 first discovered after it. 0 retained attributions have unknown discovery dates.

Historical cutoffs filter known discovery dates, but the snapshot contains present-day incident links, fields, and classifications. It is not a complete historical database snapshot; retrospective and prospective interpretations must account for that limitation.

## Timing-band and date-precision sensitivity

Rates are per 100 entity-years. Four band schemes, size-specific rates, and a kernel-smoothed rate are directly standardized to the fixed baseline organisation-size distribution, including UNKNOWN; see `timing_band_size_adjusted.csv`, `timing_band_size_strata.csv`, and `timing_smooth_size_adjusted.csv`. The smooth-curve interval resamples organisations and retains fixed weights. An estimate is unavailable when a nonzero-weight size stratum lacks exposure; no unsupported group is silently dropped. Baseline size weights: {'SMALL': 0.07967479674796749, 'MEDIUM': 0.26991869918699185, 'LARGE': 0.28780487804878047, 'HUGE': 0.21788617886178863, 'UNKNOWN': 0.14471544715447154}. Recorded current size is an imperfect proxy, not a direct adjustment for visibility, attack surface or target value; size estimates may be stale or missing.

| Days | Repeat events | Exposure days | Unadjusted rate | Size-standardized rate | Unsupported sizes |
|---|---:|---:|---:|---:|---|
| 1-90 | 3 | 59588 | 1.84 | 1.91 | none |
| 91-180 | 6 | 55471 | 3.95 | 3.63 | none |
| 181-270 | 4 | 49957 | 2.92 | 2.92 | none |

Precision evidence among victim-event attributions: {'unknown': 484, 'ambiguous_first_of_month': 193}. An initial day of a month is ambiguous, not proof that the date was imputed. `timing_date_precision_size_adjusted.csv` compares explicit month/year intervals, two separately labelled ambiguous-first scenarios, and complete histories with explicitly day-precise metadata (0 entities, 0 with observed repeats). Insufficient explicit date precision for a reliable precisely dated subset. Imputation percentiles show sensitivity to date assumptions, not confidence intervals for incidence.

| Date scenario | 91–180-day size-standardized median rate | 2.5–97.5% of date draws | Valid draws |
|---|---:|---:|---:|
| stored dates | 3.63 | 3.63–3.63 | 1/1 |
| known intervals | 3.63 | 3.63–3.63 | 100/100 |
| ambiguous firsts as month | 3.75 | 3.07–4.98 | 100/100 |
| ambiguous firsts as month or year | 3.55 | 2.43–4.67 | 100/100 |
| explicit day only complete histories | — | —–— | 0/1 |

A separate prospective size-adjusted protocol freezes the cohort and recorded size at registration, and compares 91–180 days with the adjacent 1–90 and 181–270 days using a conditional test within size strata. The fixed end is 2028-10-27 and at least 20 comparison events in supported strata are required. Current size-adjusted validation status: **pending**. The test assumes independent Poisson counts within size strata; shared incidents, sparse strata, mutable records and missing earlier history limit interpretation. The original unadjusted registered protocol is retained separately in `timing_peak_validation.json`; its definition has not been changed. Preserve `timing_size_adjusted_registration.json` for future validation.

### Linkage and shared-incident sensitivity

The scenarios below are independent alternatives to the baseline. Rates are recorded repeat events per 100 entity-years at risk; rate ratios compare days 91–180 with days 1–90. Each rate is directly standardised to the same baseline distribution of organisation sizes, so differing size composition between scenarios and time bands is accounted for.

Baseline size weights: {'SMALL': 0.07967479674796749, 'MEDIUM': 0.26991869918699185, 'LARGE': 0.28780487804878047, 'HUGE': 0.21788617886178863, 'UNKNOWN': 0.14471544715447154}. UNKNOWN is a separate stratum. Organisation size is a current recorded category, not a complete measurement of exposure, target value or reporting visibility.

| Scenario | Evidence status | Decisions applied | Victim attributions | Incidents | 0–90 events / days | 91–180 events / days | Size-standardised ratio | Adjustment status |
|---|---|---:|---:|---:|---:|---:|---:|---|
| baseline | observed cohort | 0 | 677 | 584 | 3 / 59588 | 6 / 55471 | 1.90 | estimated |
| active_same_overrides | evaluated | 2 | 677 | 582 | 3 / 59603 | 6 / 55524 | 1.90 | estimated |
| model_same_probability_ge_0.9 | evaluated | 0 | 677 | 584 | 3 / 59588 | 6 / 55471 | 1.90 | estimated |
| model_victim_probability_lt_0.5_excluded | evaluated | 56 | 621 | 540 | 1 / 54864 | 3 / 51388 | 2.70 | estimated |

Stored scores cover 81 in-cohort incident pairs and 608 victim attributions. Active different-incident overrides vetoed 0 override merges and 1 model-candidate merges, including conflicts created transitively. 0 current incident(s) contain an active different-incident override; these cannot be split reliably without per-report victim and date attribution.
Model scores are uncalibrated stored judgments, not confirmed truth; thresholds 0.9 and 0.5 define sensitivity scenarios, not validation. A merged incident uses the earliest observed date and the union of attributed victims. Evidence can be stale. Missing judgment or override tables are reported as unavailable. These scenarios cannot identify missing judgments or unrecorded victims.

43 recorded incidents have multiple attributed victims, involving 131 distinct victims and 229 event-level victim pairs; 16 shared incidents have a supplier name recorded. Supplier names alone are not counted as victims. The observed network has 522 components, largest 21 organisations.
The size-standardised bootstrap compares resampling organisations with resampling connected components (500 requested replicates):

| Resampling unit | Metric | Estimate | 95% percentile interval | Valid replicates |
|---|---|---:|---:|---:|
| entity | rate_0_90 | 1.91 | 0.00–4.00 | 500 |
| entity | rate_91_180 | 3.63 | 1.19–6.50 | 500 |
| entity | rate_ratio | 1.90 | 0.50–7.98 | 483 |
| shared_incident_component | rate_0_90 | 1.91 | 0.00–4.28 | 500 |
| shared_incident_component | rate_91_180 | 3.63 | 0.72–6.24 | 500 |
| shared_incident_component | rate_ratio | 1.90 | 0.41–8.08 | 469 |

Components connect organisations through any recorded shared incident, not only incidents with a named supplier. Bootstrap intervals use fixed baseline weights and retain all sizes within each component; draws lacking exposure in any required stratum are omitted and valid counts are shown. Small cells, zero early events and missing shared incidents limit inference. Stratum-specific rates are in linkage_size_strata.csv; crude results remain separately available for comparison.

## Event order and attack-type transitions

Comparisons use the same reference distribution of baseline unique organisations by current recorded size (SMALL, MEDIUM, LARGE, HUGE, UNKNOWN), including unknown size as its own stratum. Contradictory size labels for an organisation are treated as UNKNOWN. Direct standardized rates are unavailable if any reference stratum lacks exposure; no missing stratum is extrapolated. Size helps account for differences associated with visibility, exposure and target value, but those factors are not directly measured here and size adjustment cannot remove all confounding. The reference weights are in `recurrence_size_reference_weights.csv`.

These first/later recorded-incident rates are standardized to that common size distribution. Spells end at the next recorded incident or the censor date; censored spells contribute exposure. Use the size-stratified CSV to inspect sparse groups; Poisson intervals do not account for dependence between incidents at one organisation.

| dimension | group | organisations | repeats | reference weight covered | standardized rate per 100 entity years | status |
| --- | --- | --- | --- | --- | --- | --- |
| Event order | First observed | 615 | 42 | 1.00 | 2.05 | available |
| Event order | Second or later observed | 42 | 15 | 1.00 | 17.58 | available |

Transition counts and within-size conditional shares appear in `attack_type_transitions_by_size.csv`. These are conditional on a subsequent observed incident. Unknown types remain Unknown; different or unknown types on the same day are labelled Multiple or ambiguous. Cells are sparse and are not causal estimates.

| size | prior attack type | next attack type | transitions | organisations | share of prior type within size |
| --- | --- | --- | --- | --- | --- |
| HUGE | Data Breach | Data Breach | 17 | 13 | 0.65 |
| HUGE | Data Breach | Denial of Service | 1 | 1 | 0.04 |
| HUGE | Data Breach | Other | 3 | 3 | 0.12 |
| HUGE | Data Breach | Ransomware | 1 | 1 | 0.04 |
| HUGE | Data Breach | State-Sponsored Attack | 1 | 1 | 0.04 |
| HUGE | Data Breach | Supply Chain Attack | 1 | 1 | 0.04 |
| HUGE | Data Breach | Unknown | 2 | 2 | 0.08 |
| HUGE | Denial of Service | Data Breach | 1 | 1 | 1.00 |
| HUGE | Multiple or ambiguous | Data Breach | 1 | 1 | 1.00 |
| HUGE | Other | Other | 1 | 1 | 1.00 |
| HUGE | Ransomware | Data Breach | 1 | 1 | 0.50 |
| HUGE | Ransomware | Ransomware | 1 | 1 | 0.50 |
| LARGE | Data Breach | Data Breach | 6 | 5 | 0.50 |
| LARGE | Data Breach | Denial of Service | 1 | 1 | 0.08 |
| LARGE | Data Breach | Other | 3 | 3 | 0.25 |
| LARGE | Data Breach | Ransomware | 1 | 1 | 0.08 |
| LARGE | Data Breach | Unknown | 1 | 1 | 0.08 |
| LARGE | Denial of Service | Data Breach | 1 | 1 | 1.00 |
| LARGE | Other | Data Breach | 1 | 1 | 0.50 |
| LARGE | Other | Other | 1 | 1 | 0.50 |

Showing 20 of 29 rows; the CSV contains all rows.

## Reporting and observation bias

Source history: available; 0 event IDs with all known discovery dates after the cutoff excluded; 0 source records lack discovery dates. Organisation sizes are current recorded estimates and may not describe their size at the incident date.

Source groups refer to the prior incident, are mutually exclusive, and show source availability—not the chance that an incident was reported. Calendar groups are the prior incident's cohort, not calendar-time-adjusted rate ratios. Missing source history is Unknown.

The following source and calendar rates are standardized to the same fixed size weights. Size-specific rates and exposure are in `recurrence_order_source_calendar_by_size.csv`.

| dimension | group | organisations | repeats | reference weight covered | standardized rate per 100 entity years | status |
| --- | --- | --- | --- | --- | --- | --- |
| Prior incident source | GoogleSearch | 75 | 2 | 1.00 | 0.77 | available |
| Prior incident source | Multiple sources | 243 | 35 | 1.00 | 4.07 | available |
| Prior incident source | OAIC | 5 | 0 | 0.51 |  | unavailable: missing size exposure |
| Prior incident source | Perplexity | 174 | 11 | 1.00 | 2.08 | available |
| Prior incident source | PerplexityResearch | 1 | 0 | 0.27 |  | unavailable: missing size exposure |
| Prior incident source | WebberInsurance | 150 | 9 | 1.00 | 1.48 | available |
| Prior incident calendar cohort | 2019–2021 | 128 | 25 | 1.00 | 2.92 | available |
| Prior incident calendar cohort | 2022–2023 | 135 | 12 | 1.00 | 1.81 | available |
| Prior incident calendar cohort | 2024 onward | 372 | 15 | 1.00 | 3.23 | available |
| Prior incident calendar cohort | Through 2018 | 16 | 5 | 0.86 |  | unavailable: missing size exposure |

The following simulations remove some already recorded follow-ups under stated hypothetical observation probabilities: 75% for all follow-ups, or 50% for small and medium organisations and 100% for other recorded sizes, including Unknown. First observed incidents are retained. They illustrate sensitivity and do not estimate or reconstruct unreported attacks.

The following table uses fixed size standardization for each simulation draw; the full size-specific results, support and valid draw counts are in `observation_bias_by_size.csv`. A missing rate means one or more reference size groups have no exposure.

| family | scenario | order group | size | replicates | valid rate draws | median repeats | median rate | p05 rate | p95 rate | reference weight covered |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| reporting | uniform 75% follow-up observation | All observed orders | STANDARDIZED | 100 | 100 | 43.00 | 1.98 | 1.76 | 2.21 | 1.00 |
| reporting | lower observation for small organisations | All observed orders | STANDARDIZED | 100 | 100 | 55.00 | 2.45 | 2.32 | 2.60 | 1.00 |

The prior-history scenarios hypothetically assign an unobserved earlier incident to a fraction of organisations; they relabel existing spells without fabricating dates or exposure. They are not corrections or estimates of the true unobserved history.

| family | scenario | order group | size | replicates | valid rate draws | median repeats | median rate | p05 rate | p95 rate | reference weight covered |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| history | 0% missing prior history | First | STANDARDIZED | 1 | 1 | 42.00 | 2.05 | 2.05 | 2.05 | 1.00 |
| history | 0% missing prior history | Later | STANDARDIZED | 1 | 1 | 15.00 | 17.58 | 17.58 | 17.58 | 1.00 |
| history | 25% missing prior history | First | STANDARDIZED | 100 | 100 | 32.00 | 2.07 | 1.71 | 2.35 | 1.00 |
| history | 25% missing prior history | Later | STANDARDIZED | 100 | 100 | 25.00 | 3.98 | 3.18 | 5.06 | 1.00 |
| history | 50% missing prior history | First | STANDARDIZED | 100 | 100 | 21.00 | 2.09 | 1.48 | 2.66 | 1.00 |
| history | 50% missing prior history | Later | STANDARDIZED | 100 | 100 | 36.00 | 3.08 | 2.59 | 3.69 | 1.00 |

The original aggregate CSVs are unadjusted descriptive counts, supplied for continuity; they should not be used as size-adjusted comparisons.

These are associations in a selected, publicly reported dataset, conditional on an observed first incident. They do not establish causal effects or population attack risk. Sparse groups and reporting changes can be important.

## Reproducibility

Database snapshot SHA-256: `ac5537e06f15e91b3a40f9a60341919fb05aac3df6dba41b00dcd9de23323c3f`.
Bootstrap replicates: 500; date draws: 100.

The detailed result tables are saved as CSV files alongside this report.
