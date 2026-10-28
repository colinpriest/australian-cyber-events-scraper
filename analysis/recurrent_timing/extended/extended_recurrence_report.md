# Extended recurrence analyses

Observation cutoff: 2026-10-29. Seed: 20260805.

The analyses describe recorded incidents among organisations with at least one recorded victim event. They do not estimate the probability of an attack across all Australian organisations.

Organisation size is included in comparisons using the recorded size categories. The standard population is the organisations in this observed cohort, counted once each; UNKNOWN remains a separate category. Size is an imperfect proxy for visibility, attack exposure and target value, which are not directly measured. Current size estimates may differ from size at the incident date, and residual confounding remains possible.

| Recorded size | Organisations | Standard population weight |
|---|---:|---:|
| SMALL | 93 | 10.4% |
| MEDIUM | 275 | 30.8% |
| LARGE | 272 | 30.5% |
| HUGE | 179 | 20.0% |
| UNKNOWN | 74 | 8.3% |

Retained 1016 victim-event attributions; excluded 0 after the incident-date cutoff and 0 first discovered after it. 0 retained attributions have unknown discovery dates.

Victim attributions left unresolved by the calibrated attribution check: 43 on 43 incident(s), included here (--unresolved-attributions).

Historical cutoffs filter known discovery dates, but the snapshot contains present-day incident links, fields, and classifications. It is not a complete historical database snapshot; retrospective and prospective interpretations must account for that limitation.

## Timing-band and date-precision sensitivity

Rates are per 100 entity-years. Four band schemes, size-specific rates, and a kernel-smoothed rate are directly standardized to the fixed baseline organisation-size distribution, including UNKNOWN; see `timing_band_size_adjusted.csv`, `timing_band_size_strata.csv`, and `timing_smooth_size_adjusted.csv`. The smooth-curve interval resamples organisations and retains fixed weights. An estimate is unavailable when a nonzero-weight size stratum lacks exposure; no unsupported group is silently dropped. Baseline size weights: {'SMALL': 0.10414333706606943, 'MEDIUM': 0.3079507278835386, 'LARGE': 0.3045912653975364, 'HUGE': 0.20044792833146696, 'UNKNOWN': 0.08286674132138858}. Recorded current size is an imperfect proxy, not a direct adjustment for visibility, attack surface or target value; size estimates may be stale or missing.

| Days | Repeat events | Exposure days | Unadjusted rate | Size-standardized rate | Unsupported sizes |
|---|---:|---:|---:|---:|---|
| 1-90 | 13 | 87567 | 5.42 | 4.81 | none |
| 91-180 | 13 | 79273 | 5.99 | 5.29 | none |
| 181-270 | 13 | 70834 | 6.70 | 5.73 | none |

Precision evidence among victim-event attributions: {'unknown': 462, 'explicit_month': 259, 'explicit_day': 166, 'ambiguous_first_of_month': 129}. An initial day of a month is ambiguous, not proof that the date was imputed. `timing_date_precision_size_adjusted.csv` compares explicit month/year intervals, two separately labelled ambiguous-first scenarios, and complete histories with explicitly day-precise metadata (137 entities, 2 with observed repeats). Insufficient explicit date precision for a reliable precisely dated subset. Imputation percentiles show sensitivity to date assumptions, not confidence intervals for incidence.

| Date scenario | 91–180-day size-standardized median rate | 2.5–97.5% of date draws | Valid draws |
|---|---:|---:|---:|
| stored dates | 5.29 | 5.29–5.29 | 1/1 |
| known intervals | 4.94 | 4.41–5.29 | 100/100 |
| ambiguous firsts as month | 5.07 | 4.41–5.94 | 100/100 |
| ambiguous firsts as month or year | 4.42 | 3.41–5.35 | 100/100 |
| explicit day only complete histories | 0.00 | 0.00–0.00 | 1/1 |

A separate prospective size-adjusted protocol freezes the cohort and recorded size at registration, and compares 91–180 days with the adjacent 1–90 and 181–270 days using a conditional test within size strata. The fixed end is 2028-10-27 and at least 20 comparison events in supported strata are required. Current size-adjusted validation status: **unavailable**. The test assumes independent Poisson counts within size strata; shared incidents, sparse strata, mutable records and missing earlier history limit interpretation. The original unadjusted registered protocol is retained separately in `timing_peak_validation.json`; its definition has not been changed. Preserve `timing_size_adjusted_registration.json` for future validation.

### Linkage and shared-incident sensitivity

The scenarios below are independent alternatives to the baseline. Rates are recorded repeat events per 100 entity-years at risk; rate ratios compare days 91–180 with days 1–90. Each rate is directly standardised to the same baseline distribution of organisation sizes, so differing size composition between scenarios and time bands is accounted for.

Baseline size weights: {'SMALL': 0.10414333706606943, 'MEDIUM': 0.3079507278835386, 'LARGE': 0.3045912653975364, 'HUGE': 0.20044792833146696, 'UNKNOWN': 0.08286674132138858}. UNKNOWN is a separate stratum. Organisation size is a current recorded category, not a complete measurement of exposure, target value or reporting visibility.

| Scenario | Evidence status | Decisions applied | Victim attributions | Incidents | 0–90 events / days | 91–180 events / days | Size-standardised ratio | Adjustment status |
|---|---|---:|---:|---:|---:|---:|---:|---|
| baseline | observed cohort | 0 | 1016 | 895 | 13 / 87567 | 13 / 79273 | 1.10 | estimated |
| active_same_overrides | evaluated | 2 | 1016 | 893 | 13 / 87582 | 13 / 79325 | 1.10 | estimated |
| model_same_probability_ge_0.9 | evaluated | 0 | 1016 | 895 | 13 / 87567 | 13 / 79273 | 1.10 | estimated |
| model_victim_probability_lt_0.5_excluded | evaluated | 129 | 887 | 785 | 8 / 76921 | 9 / 70341 | 1.15 | estimated |

Stored scores cover 246 in-cohort incident pairs and 999 victim attributions. Active different-incident overrides vetoed 0 override merges and 1 model-candidate merges, including conflicts created transitively. 0 current incident(s) contain an active different-incident override; these cannot be split reliably without per-report victim and date attribution.
Model scores are uncalibrated stored judgments, not confirmed truth; thresholds 0.9 and 0.5 define sensitivity scenarios, not validation. A merged incident uses the earliest observed date and the union of attributed victims. Evidence can be stale. Missing judgment or override tables are reported as unavailable. These scenarios cannot identify missing judgments or unrecorded victims.

53 recorded incidents have multiple attributed victims, involving 162 distinct victims and 346 event-level victim pairs; 26 shared incidents have a supplier name recorded. Supplier names alone are not counted as victims. The observed network has 774 components, largest 22 organisations.
The size-standardised bootstrap compares resampling organisations with resampling connected components (500 requested replicates):

| Resampling unit | Metric | Estimate | 95% percentile interval | Valid replicates |
|---|---|---:|---:|---:|
| entity | rate_0_90 | 4.81 | 2.34–7.61 | 500 |
| entity | rate_91_180 | 5.29 | 2.56–8.72 | 500 |
| entity | rate_ratio | 1.10 | 0.50–2.68 | 500 |
| shared_incident_component | rate_0_90 | 4.81 | 2.14–7.33 | 500 |
| shared_incident_component | rate_91_180 | 5.29 | 1.95–8.14 | 500 |
| shared_incident_component | rate_ratio | 1.10 | 0.44–2.28 | 500 |

Components connect organisations through any recorded shared incident, not only incidents with a named supplier. Bootstrap intervals use fixed baseline weights and retain all sizes within each component; draws lacking exposure in any required stratum are omitted and valid counts are shown. Small cells, zero early events and missing shared incidents limit inference. Stratum-specific rates are in linkage_size_strata.csv; crude results remain separately available for comparison.

## Event order and attack-type transitions

Comparisons use the same reference distribution of baseline unique organisations by current recorded size (SMALL, MEDIUM, LARGE, HUGE, UNKNOWN), including unknown size as its own stratum. Contradictory size labels for an organisation are treated as UNKNOWN. Direct standardized rates are unavailable if any reference stratum lacks exposure; no missing stratum is extrapolated. Size helps account for differences associated with visibility, exposure and target value, but those factors are not directly measured here and size adjustment cannot remove all confounding. The reference weights are in `recurrence_size_reference_weights.csv`.

These first/later recorded-incident rates are standardized to that common size distribution. Spells end at the next recorded incident or the censor date; censored spells contribute exposure. Use the size-stratified CSV to inspect sparse groups; Poisson intervals do not account for dependence between incidents at one organisation.

| dimension | group | organisations | repeats | reference weight covered | standardized rate per 100 entity years | status |
| --- | --- | --- | --- | --- | --- | --- |
| Event order | First observed | 893 | 69 | 1.00 | 2.75 | available |
| Event order | Second or later observed | 69 | 54 | 1.00 | 19.61 | available |

Transition counts and within-size conditional shares appear in `attack_type_transitions_by_size.csv`. These are conditional on a subsequent observed incident. Unknown types remain Unknown; different or unknown types on the same day are labelled Multiple or ambiguous. Cells are sparse and are not causal estimates.

| size | prior attack type | next attack type | transitions | organisations | share of prior type within size |
| --- | --- | --- | --- | --- | --- |
| HUGE | Data Breach | Data Breach | 39 | 26 | 0.62 |
| HUGE | Data Breach | Denial of Service | 5 | 4 | 0.08 |
| HUGE | Data Breach | Financial Fraud | 1 | 1 | 0.02 |
| HUGE | Data Breach | Infrastructure Attack | 1 | 1 | 0.02 |
| HUGE | Data Breach | Insider Threat | 2 | 2 | 0.03 |
| HUGE | Data Breach | Other | 9 | 8 | 0.14 |
| HUGE | Data Breach | Phishing | 1 | 1 | 0.02 |
| HUGE | Data Breach | Ransomware | 1 | 1 | 0.02 |
| HUGE | Data Breach | State-Sponsored Attack | 1 | 1 | 0.02 |
| HUGE | Data Breach | Supply Chain Attack | 1 | 1 | 0.02 |
| HUGE | Data Breach | Vulnerability Exploit | 2 | 2 | 0.03 |
| HUGE | Denial of Service | Data Breach | 2 | 2 | 0.67 |
| HUGE | Denial of Service | Vulnerability Exploit | 1 | 1 | 0.33 |
| HUGE | Financial Fraud | Data Breach | 2 | 2 | 0.67 |
| HUGE | Financial Fraud | Identity Theft | 1 | 1 | 0.33 |
| HUGE | Insider Threat | Ransomware | 1 | 1 | 1.00 |
| HUGE | Other | Data Breach | 2 | 2 | 0.40 |
| HUGE | Other | Financial Fraud | 1 | 1 | 0.20 |
| HUGE | Other | Other | 1 | 1 | 0.20 |
| HUGE | Other | Supply Chain Attack | 1 | 1 | 0.20 |

Showing 20 of 50 rows; the CSV contains all rows.

## Reporting and observation bias

Source history: available; 0 event IDs with all known discovery dates after the cutoff excluded; 0 source records lack discovery dates. Organisation sizes are current recorded estimates and may not describe their size at the incident date.

Source groups refer to the prior incident, are mutually exclusive, and show source availability—not the chance that an incident was reported. Calendar groups are the prior incident's cohort, not calendar-time-adjusted rate ratios. Missing source history is Unknown.

The following source and calendar rates are standardized to the same fixed size weights. Size-specific rates and exposure are in `recurrence_order_source_calendar_by_size.csv`.

| dimension | group | organisations | repeats | reference weight covered | standardized rate per 100 entity years | status |
| --- | --- | --- | --- | --- | --- | --- |
| Prior incident source | CyberDaily | 251 | 16 | 1.00 | 3.91 | available |
| Prior incident source | GoogleSearch | 73 | 1 | 1.00 | 0.44 | available |
| Prior incident source | Multiple sources | 360 | 60 | 1.00 | 5.46 | available |
| Prior incident source | OAIC | 1 | 0 | 0.30 |  | unavailable: missing size exposure |
| Prior incident source | Perplexity | 141 | 32 | 1.00 | 5.30 | available |
| Prior incident source | WebberInsurance | 132 | 14 | 1.00 | 2.44 | available |
| Prior incident calendar cohort | 2019–2021 | 137 | 33 | 1.00 | 3.43 | available |
| Prior incident calendar cohort | 2022–2023 | 177 | 30 | 1.00 | 3.99 | available |
| Prior incident calendar cohort | 2024 onward | 617 | 42 | 1.00 | 5.56 | available |
| Prior incident calendar cohort | Through 2018 | 21 | 18 | 0.92 |  | unavailable: missing size exposure |

The following simulations remove some already recorded follow-ups under stated hypothetical observation probabilities: 75% for all follow-ups, or 50% for small and medium organisations and 100% for other recorded sizes, including Unknown. First observed incidents are retained. They illustrate sensitivity and do not estimate or reconstruct unreported attacks.

The following table uses fixed size standardization for each simulation draw; the full size-specific results, support and valid draw counts are in `observation_bias_by_size.csv`. A missing rate means one or more reference size groups have no exposure.

| family | scenario | order group | size | replicates | valid rate draws | median repeats | median rate | p05 rate | p95 rate | reference weight covered |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| reporting | uniform 75% follow-up observation | All observed orders | STANDARDIZED | 100 | 100 | 92.50 | 3.10 | 2.88 | 3.35 | 1.00 |
| reporting | lower observation for small organisations | All observed orders | STANDARDIZED | 100 | 100 | 117.00 | 3.80 | 3.64 | 3.95 | 1.00 |

The prior-history scenarios hypothetically assign an unobserved earlier incident to a fraction of organisations; they relabel existing spells without fabricating dates or exposure. They are not corrections or estimates of the true unobserved history.

| family | scenario | order group | size | replicates | valid rate draws | median repeats | median rate | p05 rate | p95 rate | reference weight covered |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| history | 0% missing prior history | First | STANDARDIZED | 1 | 1 | 69.00 | 2.75 | 2.75 | 2.75 | 1.00 |
| history | 0% missing prior history | Later | STANDARDIZED | 1 | 1 | 54.00 | 19.61 | 19.61 | 19.61 | 1.00 |
| history | 25% missing prior history | First | STANDARDIZED | 100 | 100 | 52.00 | 2.79 | 2.49 | 3.05 | 1.00 |
| history | 25% missing prior history | Later | STANDARDIZED | 100 | 100 | 71.00 | 6.73 | 5.90 | 7.41 | 1.00 |
| history | 50% missing prior history | First | STANDARDIZED | 100 | 100 | 34.50 | 2.81 | 2.25 | 3.22 | 1.00 |
| history | 50% missing prior history | Later | STANDARDIZED | 100 | 100 | 88.50 | 5.13 | 4.79 | 5.59 | 1.00 |

The original aggregate CSVs are unadjusted descriptive counts, supplied for continuity; they should not be used as size-adjusted comparisons.

These are associations in a selected, publicly reported dataset, conditional on an observed first incident. They do not establish causal effects or population attack risk. Sparse groups and reporting changes can be important.

## Reproducibility

Database snapshot SHA-256: `176fa241aee3e6ba357947ecf9cbd72f330a197a64be31088989477d4e40bf95`.
Bootstrap replicates: 500; date draws: 100.

The detailed result tables are saved as CSV files alongside this report.
