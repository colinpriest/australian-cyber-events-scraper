# Extended recurrence analyses

Observation cutoff: 2026-10-27. Seed: 20261027.

The analyses describe recorded incidents among organisations with at least one recorded victim event. They do not estimate the probability of an attack across all Australian organisations.

Organisation size is included in comparisons using the recorded size categories. The standard population is the organisations in this observed cohort, counted once each; UNKNOWN remains a separate category. Size is an imperfect proxy for visibility, attack exposure and target value, which are not directly measured. Current size estimates may differ from size at the incident date, and residual confounding remains possible.

| Recorded size | Organisations | Standard population weight |
|---|---:|---:|
| SMALL | 51 | 8.9% |
| MEDIUM | 166 | 28.9% |
| LARGE | 172 | 30.0% |
| HUGE | 136 | 23.7% |
| UNKNOWN | 49 | 8.5% |

Retained 635 victim-event attributions; excluded 0 after the incident-date cutoff and 0 first discovered after it. 0 retained attributions have unknown discovery dates.

Historical cutoffs filter known discovery dates, but the snapshot contains present-day incident links, fields, and classifications. It is not a complete historical database snapshot; retrospective and prospective interpretations must account for that limitation.

## Timing-band and date-precision sensitivity

Rates are per 100 entity-years. Four band schemes, size-specific rates, and a kernel-smoothed rate are directly standardized to the fixed baseline organisation-size distribution, including UNKNOWN; see `timing_band_size_adjusted.csv`, `timing_band_size_strata.csv`, and `timing_smooth_size_adjusted.csv`. The smooth-curve interval resamples organisations and retains fixed weights. An estimate is unavailable when a nonzero-weight size stratum lacks exposure; no unsupported group is silently dropped. Baseline size weights: {'SMALL': 0.08885017421602788, 'MEDIUM': 0.289198606271777, 'LARGE': 0.29965156794425085, 'HUGE': 0.23693379790940766, 'UNKNOWN': 0.08536585365853659}. Recorded current size is an imperfect proxy, not a direct adjustment for visibility, attack surface or target value; size estimates may be stale or missing.

| Days | Repeat events | Exposure days | Unadjusted rate | Size-standardized rate | Unsupported sizes |
|---|---:|---:|---:|---:|---|
| 1-90 | 4 | 56281 | 2.60 | 2.52 | none |
| 91-180 | 10 | 51777 | 7.05 | 6.34 | none |
| 181-270 | 4 | 46841 | 3.12 | 3.09 | none |

Precision evidence among victim-event attributions: {'unknown': 436, 'ambiguous_first_of_month': 199}. An initial day of a month is ambiguous, not proof that the date was imputed. `timing_date_precision_size_adjusted.csv` compares explicit month/year intervals, two separately labelled ambiguous-first scenarios, and complete histories with explicitly day-precise metadata (0 entities, 0 with observed repeats). Insufficient explicit date precision for a reliable precisely dated subset. Imputation percentiles show sensitivity to date assumptions, not confidence intervals for incidence.

| Date scenario | 91–180-day size-standardized median rate | 2.5–97.5% of date draws | Valid draws |
|---|---:|---:|---:|
| stored dates | 6.34 | 6.34–6.34 | 1/1 |
| known intervals | 6.34 | 6.34–6.34 | 100/100 |
| ambiguous firsts as month | 6.33 | 4.52–7.77 | 100/100 |
| ambiguous firsts as month or year | 6.37 | 4.53–8.12 | 100/100 |
| explicit day only complete histories | — | —–— | 0/1 |

A separate prospective size-adjusted protocol freezes the cohort and recorded size at registration, and compares 91–180 days with the adjacent 1–90 and 181–270 days using a conditional test within size strata. The fixed end is 2028-10-27 and at least 20 comparison events in supported strata are required. Current size-adjusted validation status: **pending**. The test assumes independent Poisson counts within size strata; shared incidents, sparse strata, mutable records and missing earlier history limit interpretation. The original unadjusted registered protocol is retained separately in `timing_peak_validation.json`; its definition has not been changed. Preserve `timing_size_adjusted_registration.json` for future validation.

### Linkage and shared-incident sensitivity

The scenarios below are independent alternatives to the baseline. Rates are recorded repeat events per 100 entity-years at risk; rate ratios compare days 91–180 with days 1–90. Each rate is directly standardised to the same baseline distribution of organisation sizes, so differing size composition between scenarios and time bands is accounted for.

Baseline size weights: {'SMALL': 0.08885017421602788, 'MEDIUM': 0.289198606271777, 'LARGE': 0.29965156794425085, 'HUGE': 0.23693379790940766, 'UNKNOWN': 0.08536585365853659}. UNKNOWN is a separate stratum. Organisation size is a current recorded category, not a complete measurement of exposure, target value or reporting visibility.

| Scenario | Evidence status | Decisions applied | Victim attributions | Incidents | 0–90 events / days | 91–180 events / days | Size-standardised ratio | Adjustment status |
|---|---|---:|---:|---:|---:|---:|---:|---|
| baseline | observed cohort | 0 | 635 | 558 | 4 / 56281 | 10 / 51777 | 2.51 | estimated |
| active_same_overrides | evaluated | 2 | 635 | 556 | 4 / 56296 | 10 / 51831 | 2.52 | estimated |
| model_same_probability_ge_0.9 | no applicable candidate decisions | 0 | 635 | 558 | 4 / 56281 | 10 / 51777 | 2.51 | estimated |
| model_victim_probability_lt_0.5_excluded | evaluated | 25 | 610 | 547 | 2 / 54062 | 5 / 49967 | 2.56 | estimated |

Stored scores cover 97 in-cohort incident pairs and 110 victim attributions. Active different-incident overrides vetoed 0 override merges and 0 model-candidate merges, including conflicts created transitively. 0 current incident(s) contain an active different-incident override; these cannot be split reliably without per-report victim and date attribution.
Model scores are uncalibrated stored judgments, not confirmed truth; thresholds 0.9 and 0.5 define sensitivity scenarios, not validation. A merged incident uses the earliest observed date and the union of attributed victims. Evidence can be stale. Missing judgment or override tables are reported as unavailable. These scenarios cannot identify missing judgments or unrecorded victims.

41 recorded incidents have multiple attributed victims, involving 109 distinct victims and 163 event-level victim pairs; 14 shared incidents have a supplier name recorded. Supplier names alone are not counted as victims. The observed network has 498 components, largest 20 organisations.
The size-standardised bootstrap compares resampling organisations with resampling connected components (200 requested replicates):

| Resampling unit | Metric | Estimate | 95% percentile interval | Valid replicates |
|---|---|---:|---:|---:|
| entity | rate_0_90 | 2.52 | 0.00–5.69 | 200 |
| entity | rate_91_180 | 6.34 | 1.99–11.15 | 200 |
| entity | rate_ratio | 2.51 | 0.94–12.17 | 191 |
| shared_incident_component | rate_0_90 | 2.52 | 0.00–5.73 | 200 |
| shared_incident_component | rate_91_180 | 6.34 | 0.77–11.39 | 200 |
| shared_incident_component | rate_ratio | 2.51 | 1.24–9.58 | 168 |

Components connect organisations through any recorded shared incident, not only incidents with a named supplier. Bootstrap intervals use fixed baseline weights and retain all sizes within each component; draws lacking exposure in any required stratum are omitted and valid counts are shown. Small cells, zero early events and missing shared incidents limit inference. Stratum-specific rates are in linkage_size_strata.csv; crude results remain separately available for comparison.

## Event order and attack-type transitions

Comparisons use the same reference distribution of baseline unique organisations by current recorded size (SMALL, MEDIUM, LARGE, HUGE, UNKNOWN), including unknown size as its own stratum. Contradictory size labels for an organisation are treated as UNKNOWN. Direct standardized rates are unavailable if any reference stratum lacks exposure; no missing stratum is extrapolated. Size helps account for differences associated with visibility, exposure and target value, but those factors are not directly measured here and size adjustment cannot remove all confounding. The reference weights are in `recurrence_size_reference_weights.csv`.

These first/later recorded-incident rates are standardized to that common size distribution. Spells end at the next recorded incident or the censor date; censored spells contribute exposure. Use the size-stratified CSV to inspect sparse groups; Poisson intervals do not account for dependence between incidents at one organisation.

| dimension | group | organisations | repeats | reference weight covered | standardized rate per 100 entity years | status |
| --- | --- | --- | --- | --- | --- | --- |
| Event order | First observed | 574 | 41 | 1.00 | 2.13 | available |
| Event order | Second or later observed | 41 | 20 | 1.00 | 15.53 | available |

Transition counts and within-size conditional shares appear in `attack_type_transitions_by_size.csv`. These are conditional on a subsequent observed incident. Unknown types remain Unknown; different or unknown types on the same day are labelled Multiple or ambiguous. Cells are sparse and are not causal estimates.

| size | prior attack type | next attack type | transitions | organisations | share of prior type within size |
| --- | --- | --- | --- | --- | --- |
| HUGE | Data Breach | Data Breach | 13 | 11 | 0.54 |
| HUGE | Data Breach | Denial of Service | 1 | 1 | 0.04 |
| HUGE | Data Breach | Financial Fraud | 1 | 1 | 0.04 |
| HUGE | Data Breach | Other | 1 | 1 | 0.04 |
| HUGE | Data Breach | Phishing | 2 | 2 | 0.08 |
| HUGE | Data Breach | Ransomware | 3 | 3 | 0.12 |
| HUGE | Data Breach | State-Sponsored Attack | 1 | 1 | 0.04 |
| HUGE | Data Breach | Supply Chain Attack | 1 | 1 | 0.04 |
| HUGE | Data Breach | Vulnerability Exploit | 1 | 1 | 0.04 |
| HUGE | Denial of Service | Data Breach | 1 | 1 | 1.00 |
| HUGE | Financial Fraud | Data Breach | 1 | 1 | 1.00 |
| HUGE | Other | Data Breach | 1 | 1 | 1.00 |
| HUGE | Ransomware | Data Breach | 3 | 3 | 0.75 |
| HUGE | Ransomware | Ransomware | 1 | 1 | 0.25 |
| HUGE | State-Sponsored Attack | Other | 1 | 1 | 1.00 |
| HUGE | Vulnerability Exploit | Data Breach | 1 | 1 | 1.00 |
| LARGE | Data Breach | Data Breach | 9 | 7 | 0.69 |
| LARGE | Data Breach | Denial of Service | 1 | 1 | 0.08 |
| LARGE | Data Breach | Other | 2 | 2 | 0.15 |
| LARGE | Data Breach | Ransomware | 1 | 1 | 0.08 |

Showing 20 of 31 rows; the CSV contains all rows.

## Reporting and observation bias

Source history: available; 0 event IDs with all known discovery dates after the cutoff excluded; 0 source records lack discovery dates. Organisation sizes are current recorded estimates and may not describe their size at the incident date.

Source groups refer to the prior incident, are mutually exclusive, and show source availability—not the chance that an incident was reported. Calendar groups are the prior incident's cohort, not calendar-time-adjusted rate ratios. Missing source history is Unknown.

The following source and calendar rates are standardized to the same fixed size weights. Size-specific rates and exposure are in `recurrence_order_source_calendar_by_size.csv`.

| dimension | group | organisations | repeats | reference weight covered | standardized rate per 100 entity years | status |
| --- | --- | --- | --- | --- | --- | --- |
| Prior incident source | GoogleSearch | 88 | 3 | 1.00 | 1.35 | available |
| Prior incident source | Multiple sources | 231 | 37 | 1.00 | 4.56 | available |
| Prior incident source | OAIC | 6 | 0 | 0.54 |  | unavailable: missing size exposure |
| Prior incident source | Perplexity | 141 | 10 | 1.00 | 2.09 | available |
| Prior incident source | WebberInsurance | 141 | 11 | 1.00 | 2.01 | available |
| Prior incident calendar cohort | 2019–2021 | 126 | 22 | 1.00 | 2.80 | available |
| Prior incident calendar cohort | 2022–2023 | 126 | 15 | 1.00 | 3.54 | available |
| Prior incident calendar cohort | 2024 onward | 341 | 17 | 1.00 | 4.29 | available |
| Prior incident calendar cohort | Through 2018 | 18 | 7 | 0.91 |  | unavailable: missing size exposure |

The following simulations remove some already recorded follow-ups under stated hypothetical observation probabilities: 75% for all follow-ups, or 50% for small and medium organisations and 100% for other recorded sizes, including Unknown. First observed incidents are retained. They illustrate sensitivity and do not estimate or reconstruct unreported attacks.

The following table uses fixed size standardization for each simulation draw; the full size-specific results, support and valid draw counts are in `observation_bias_by_size.csv`. A missing rate means one or more reference size groups have no exposure.

| family | scenario | order group | size | replicates | valid rate draws | median repeats | median rate | p05 rate | p95 rate | reference weight covered |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| reporting | uniform 75% follow-up observation | All observed orders | STANDARDIZED | 100 | 100 | 46.00 | 2.23 | 1.94 | 2.47 | 1.00 |
| reporting | lower observation for small organisations | All observed orders | STANDARDIZED | 100 | 100 | 59.00 | 2.78 | 2.64 | 2.94 | 1.00 |

The prior-history scenarios hypothetically assign an unobserved earlier incident to a fraction of organisations; they relabel existing spells without fabricating dates or exposure. They are not corrections or estimates of the true unobserved history.

| family | scenario | order group | size | replicates | valid rate draws | median repeats | median rate | p05 rate | p95 rate | reference weight covered |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| history | 0% missing prior history | First | STANDARDIZED | 1 | 1 | 41.00 | 2.13 | 2.13 | 2.13 | 1.00 |
| history | 0% missing prior history | Later | STANDARDIZED | 1 | 1 | 20.00 | 15.53 | 15.53 | 15.53 | 1.00 |
| history | 25% missing prior history | First | STANDARDIZED | 100 | 100 | 31.50 | 2.17 | 1.85 | 2.42 | 1.00 |
| history | 25% missing prior history | Later | STANDARDIZED | 100 | 100 | 29.50 | 4.83 | 4.21 | 5.92 | 1.00 |
| history | 50% missing prior history | First | STANDARDIZED | 100 | 100 | 21.00 | 2.08 | 1.56 | 2.83 | 1.00 |
| history | 50% missing prior history | Later | STANDARDIZED | 100 | 100 | 40.00 | 3.72 | 3.12 | 4.19 | 1.00 |

The original aggregate CSVs are unadjusted descriptive counts, supplied for continuity; they should not be used as size-adjusted comparisons.

These are associations in a selected, publicly reported dataset, conditional on an observed first incident. They do not establish causal effects or population attack risk. Sparse groups and reporting changes can be important.

## Reproducibility

Database snapshot SHA-256: `e057fb6d6fa3ba7cbac9c35fee530097d86a597342e3395924488e3f98edde55`.
Bootstrap replicates: 200; date draws: 100.

The detailed result tables are saved as CSV files alongside this report.
