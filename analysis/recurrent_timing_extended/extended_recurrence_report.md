# Extended recurrence analyses

Observation cutoff: 2026-10-30. Seed: 20260805.

The analyses describe recorded incidents among organisations with at least one recorded victim event. They do not estimate the probability of an attack across all Australian organisations.

Organisation size is included in comparisons using the recorded size categories. The standard population is the organisations in this observed cohort, counted once each; UNKNOWN remains a separate category. Size is an imperfect proxy for visibility, attack exposure and target value, which are not directly measured. Current size estimates may differ from size at the incident date, and residual confounding remains possible.

| Recorded size | Organisations | Standard population weight |
|---|---:|---:|
| SMALL | 100 | 11.7% |
| MEDIUM | 276 | 32.3% |
| LARGE | 261 | 30.6% |
| HUGE | 151 | 17.7% |
| UNKNOWN | 66 | 7.7% |

Retained 1007 victim-event attributions; excluded 0 after the incident-date cutoff and 0 first discovered after it. 0 retained attributions have unknown discovery dates.

Victim attributions left unresolved by the calibrated attribution check: 36 on 36 incident(s), included here (--unresolved-attributions).

Historical cutoffs filter known discovery dates, but the snapshot contains present-day incident links, fields, and classifications. It is not a complete historical database snapshot; retrospective and prospective interpretations must account for that limitation. Rerun with `--as-known-at DATE` to reconstruct incident links, dates, victims and size bands from the recorded history of when each became known (`InputHistoryStatus` says since when that history exists).

## Timing-band and date-precision sensitivity

Rates are per 100 entity-years. Four band schemes, size-specific rates, and a kernel-smoothed rate are directly standardized to the fixed baseline organisation-size distribution, including UNKNOWN; see `timing_band_size_adjusted.csv`, `timing_band_size_strata.csv`, and `timing_smooth_size_adjusted.csv`. The smooth-curve interval resamples organisations and retains fixed weights. An estimate is unavailable when a nonzero-weight size stratum lacks exposure; no unsupported group is silently dropped. Baseline size weights: {'SMALL': 0.117096018735363, 'MEDIUM': 0.3231850117096019, 'LARGE': 0.30562060889929743, 'HUGE': 0.17681498829039813, 'UNKNOWN': 0.07728337236533958}. Recorded current size is an imperfect proxy, not a direct adjustment for visibility, attack surface or target value; size estimates may be stale or missing.

| Days | Repeat events | Exposure days | Unadjusted rate | Size-standardized rate | Unsupported sizes |
|---|---:|---:|---:|---:|---|
| 1-90 | 26 | 85161 | 11.15 | 9.82 | none |
| 91-180 | 15 | 76574 | 7.15 | 6.33 | none |
| 181-270 | 14 | 68102 | 7.51 | 6.29 | none |

Precision evidence among victim-event attributions: {'unknown': 480, 'explicit_day': 212, 'explicit_month': 190, 'ambiguous_first_of_month': 125}. Verified month and year dates are drawn within their stored interval (`event_date_earliest` / `event_date_latest`); a date whose precision is unverified has no interval - it is unknown, not a day, and is kept as stored only in the scenarios that say so. An initial day of a month is ambiguous, not proof that the date was imputed. `timing_date_precision_size_adjusted.csv` compares verified month/year intervals, two separately labelled hypothetical ambiguous-first scenarios, complete histories with explicitly day-precise metadata (187 entities, 1 with observed repeats), and complete histories in which every date has a verified interval (unverified organisations left out, never read as days). Insufficient explicit date precision for a reliable precisely dated subset. Imputation percentiles show sensitivity to date assumptions, not confidence intervals for incidence.

| Date scenario | 91–180-day size-standardized median rate | 2.5–97.5% of date draws | Valid draws |
|---|---:|---:|---:|
| stored dates | 6.33 | 6.33–6.33 | 1/1 |
| known intervals | 6.17 | 5.66–6.67 | 100/100 |
| ambiguous firsts as month | 6.17 | 5.37–7.17 | 100/100 |
| ambiguous firsts as month or year | 6.48 | 5.32–7.33 | 100/100 |
| explicit day only complete histories | 0.00 | 0.00–0.00 | 1/1 |
| verified intervals complete histories | 0.00 | 0.00–0.00 | 100/100 |

### Same-date ties and unknown ordering

A same-date tie is far more likely one incident deduplication has not joined than two incidents on one day; every such pair is listed, with the reason it is still two events, in `dashboard/close_pairs.html`. Until a tie is resolved, each tie policy states one assumption about it (`collapse`, the primary treatment, keeps every id in `timing_entity_date_ties.csv`):

- `collapse`: the tied incidents are one occurrence: one time point, never a repeat between them (their ids and count are kept).
- `zero_gap`: the tied incidents are distinct and one day apart, in id order: each after the first is a repeat after 1 day.
- `interval_spread`: the tied incidents are distinct and evenly spread across the verified interval they share (about interval length / n days apart, the first at the interval's start); a tie with no shared verified interval longer than a day is placed as under zero_gap.
- `interval_draw`: the tied incidents are distinct, each on a day drawn uniformly and independently within the verified interval they share, over the date draws; a tie with no shared verified interval longer than a day is placed as under zero_gap.

None of these bounds the truth: a tie that is one incident filed twice is represented only by `collapse`, two incidents really weeks apart within a verified month exactly by none, and a tie whose dates are unverified has no interval to place them in. Agreement across the policies shows only that a result does not hinge on these assumptions; `interval_draw` is the median of 100 draws. Ties found: {'ties (organisation-dates with several incidents)': 12, 'incidents in ties': 24, 'organisations with a tie': 12, 'repeats a policy that separates tied incidents adds': 12, 'ties with a shared verified interval longer than a day (spread by interval_spread)': 0, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 2, 'ties: other_day': 10}. Incidents on different stored dates whose verified intervals overlap are no tie, but their dates do not order them either: {'consecutive pairs with overlapping verified intervals': 0, '... of which stored dates differ by under 90 days': 0}; the `known_intervals` scenario varies those dates within their intervals.

| Tie policy | Days | Repeat events | Unadjusted rate | Size-standardized rate |
|---|---|---:|---:|---:|
| collapse | 1-90 | 26 | 11.15 | 9.82 |
| collapse | 91-180 | 15 | 7.15 | 6.33 |
| collapse | 181-270 | 14 | 7.51 | 6.29 |
| zero_gap | 1-90 | 38 | 16.30 | 14.83 |
| zero_gap | 91-180 | 15 | 7.15 | 6.33 |
| zero_gap | 181-270 | 14 | 7.51 | 6.29 |
| interval_spread | 1-90 | 38 | 16.30 | 14.83 |
| interval_spread | 91-180 | 15 | 7.15 | 6.33 |
| interval_spread | 181-270 | 14 | 7.51 | 6.29 |
| interval_draw | 1-90 | 38 | 16.30 | 14.83 |
| interval_draw | 91-180 | 15 | 7.15 | 6.33 |
| interval_draw | 181-270 | 14 | 7.51 | 6.29 |

A separate prospective size-adjusted protocol freezes the cohort and recorded size at registration, and compares 91–180 days with the adjacent 1–90 and 181–270 days using a conditional test within size strata. The fixed end is 2028-10-27 and at least 20 comparison events in supported strata are required. Current size-adjusted validation status: **pending**. The test assumes independent Poisson counts within size strata; shared incidents, sparse strata, mutable records and missing earlier history limit interpretation. The original unadjusted registered protocol is retained separately in `timing_peak_validation.json`; its definition has not been changed. Preserve `timing_size_adjusted_registration.json` for future validation.

### Linkage and shared-incident sensitivity

The scenarios below are independent alternatives to the baseline. Rates are recorded repeat events per 100 entity-years at risk; rate ratios compare days 91–180 with days 1–90. Each rate is directly standardised to the same baseline distribution of organisation sizes, so differing size composition between scenarios and time bands is accounted for.

Baseline size weights: {'SMALL': 0.117096018735363, 'MEDIUM': 0.3231850117096019, 'LARGE': 0.30562060889929743, 'HUGE': 0.17681498829039813, 'UNKNOWN': 0.07728337236533958}. UNKNOWN is a separate stratum. Organisation size is a current recorded category, not a complete measurement of exposure, target value or reporting visibility.

| Scenario | Evidence status | Decisions applied | Victim attributions | Incidents | 0–90 events / days | 91–180 events / days | Size-standardised ratio | Adjustment status |
|---|---|---:|---:|---:|---:|---:|---:|---|
| baseline | observed cohort | 0 | 1007 | 902 | 26 / 85161 | 15 / 76574 | 0.64 | estimated |
| active_same_overrides | evaluated | 2 | 1007 | 900 | 26 / 85176 | 15 / 76625 | 0.64 | estimated |
| model_same_probability_ge_0.9 | evaluated | 0 | 1007 | 902 | 26 / 85161 | 15 / 76574 | 0.64 | estimated |
| model_victim_probability_lt_0.5_excluded | evaluated | 85 | 922 | 826 | 24 / 78319 | 12 / 70885 | 0.54 | estimated |

Stored scores cover 398 in-cohort incident pairs and 916 victim attributions. Active different-incident overrides vetoed 0 override merges and 14 model-candidate merges, including conflicts created transitively. 1 current incident(s) contain an active different-incident override; these cannot be split reliably without per-report victim and date attribution.
Model scores are uncalibrated stored judgments, not confirmed truth; thresholds 0.9 and 0.5 define sensitivity scenarios, not validation. A merged incident uses the earliest observed date and the union of attributed victims. Evidence can be stale. Missing judgment or override tables are reported as unavailable. These scenarios cannot identify missing judgments or unrecorded victims.

53 recorded incidents have multiple attributed victims, involving 134 distinct victims and 263 event-level victim pairs; 18 shared incidents have a supplier name recorded. Supplier names alone are not counted as victims. The observed network has 757 components, largest 29 organisations.
The size-standardised bootstrap compares resampling organisations with resampling connected components (500 requested replicates):

| Resampling unit | Metric | Estimate | 95% percentile interval | Valid replicates |
|---|---|---:|---:|---:|
| entity | rate_0_90 | 9.82 | 6.26–14.38 | 500 |
| entity | rate_91_180 | 6.33 | 3.03–9.99 | 500 |
| entity | rate_ratio | 0.64 | 0.35–1.07 | 500 |
| shared_incident_component | rate_0_90 | 9.82 | 4.80–14.93 | 500 |
| shared_incident_component | rate_91_180 | 6.33 | 3.14–9.74 | 500 |
| shared_incident_component | rate_ratio | 0.64 | 0.40–1.09 | 500 |

Components connect organisations through any recorded shared incident, not only incidents with a named supplier. Bootstrap intervals use fixed baseline weights and retain all sizes within each component; draws lacking exposure in any required stratum are omitted and valid counts are shown. Small cells, zero early events and missing shared incidents limit inference. Stratum-specific rates are in linkage_size_strata.csv; crude results remain separately available for comparison.

## Event order and attack-type transitions

Comparisons use the same reference distribution of baseline unique organisations by current recorded size (SMALL, MEDIUM, LARGE, HUGE, UNKNOWN), including unknown size as its own stratum. Contradictory size labels for an organisation are treated as UNKNOWN. Direct standardized rates are unavailable if any reference stratum lacks exposure; no missing stratum is extrapolated. Size helps account for differences associated with visibility, exposure and target value, but those factors are not directly measured here and size adjustment cannot remove all confounding. The reference weights are in `recurrence_size_reference_weights.csv`.

These first/later recorded-incident rates are standardized to that common size distribution. Spells end at the next recorded incident or the censor date; censored spells contribute exposure. Use the size-stratified CSV to inspect sparse groups; Poisson intervals do not account for dependence between incidents at one organisation.

| dimension | group | organisations | repeats | reference weight covered | standardized rate per 100 entity years | status |
| --- | --- | --- | --- | --- | --- | --- |
| Event order | First observed | 854 | 78 | 1.00 | 3.13 | available |
| Event order | Second or later observed | 78 | 63 | 1.00 | 20.67 | available |

Transition counts and within-size conditional shares appear in `attack_type_transitions_by_size.csv`. These are conditional on a subsequent observed incident. Unknown types remain Unknown; different or unknown types on the same day are labelled Multiple or ambiguous. Cells are sparse and are not causal estimates.

| size | prior attack type | next attack type | transitions | organisations | share of prior type within size |
| --- | --- | --- | --- | --- | --- |
| HUGE | Data Breach | Data Breach | 41 | 27 | 0.61 |
| HUGE | Data Breach | Denial of Service | 3 | 3 | 0.04 |
| HUGE | Data Breach | Insider Threat | 2 | 2 | 0.03 |
| HUGE | Data Breach | Malware | 1 | 1 | 0.01 |
| HUGE | Data Breach | Other | 8 | 7 | 0.12 |
| HUGE | Data Breach | Phishing | 2 | 2 | 0.03 |
| HUGE | Data Breach | Ransomware | 4 | 4 | 0.06 |
| HUGE | Data Breach | State-Sponsored Attack | 2 | 2 | 0.03 |
| HUGE | Data Breach | Supply Chain Attack | 1 | 1 | 0.01 |
| HUGE | Data Breach | Vulnerability Exploit | 3 | 3 | 0.04 |
| HUGE | Denial of Service | Data Breach | 2 | 2 | 0.50 |
| HUGE | Denial of Service | Denial of Service | 1 | 1 | 0.25 |
| HUGE | Denial of Service | Other | 1 | 1 | 0.25 |
| HUGE | Financial Fraud | Data Breach | 1 | 1 | 0.50 |
| HUGE | Financial Fraud | Identity Theft | 1 | 1 | 0.50 |
| HUGE | Insider Threat | Ransomware | 1 | 1 | 1.00 |
| HUGE | Other | Data Breach | 6 | 5 | 0.55 |
| HUGE | Other | Other | 2 | 2 | 0.18 |
| HUGE | Other | Phishing | 1 | 1 | 0.09 |
| HUGE | Other | State-Sponsored Attack | 1 | 1 | 0.09 |

Showing 20 of 49 rows; the CSV contains all rows.

## Reporting and observation bias

Source history: available; 0 event IDs with all known discovery dates after the cutoff excluded; 0 source records lack discovery dates. Organisation sizes are current recorded estimates and may not describe their size at the incident date.

Source groups refer to the prior incident, are mutually exclusive, and show source availability—not the chance that an incident was reported. Calendar groups are the prior incident's cohort, not calendar-time-adjusted rate ratios. Missing source history is Unknown.

The following source and calendar rates are standardized to the same fixed size weights. Size-specific rates and exposure are in `recurrence_order_source_calendar_by_size.csv`.

| dimension | group | organisations | repeats | reference weight covered | standardized rate per 100 entity years | status |
| --- | --- | --- | --- | --- | --- | --- |
| Prior incident source | CyberDaily | 221 | 18 | 1.00 | 5.34 | available |
| Prior incident source | GoogleSearch | 68 | 4 | 1.00 | 2.17 | available |
| Prior incident source | Multiple sources | 403 | 76 | 1.00 | 5.77 | available |
| Prior incident source | Perplexity | 101 | 27 | 1.00 | 6.14 | available |
| Prior incident source | WebberInsurance | 133 | 16 | 1.00 | 2.19 | available |
| Prior incident calendar cohort | 2019–2021 | 120 | 31 | 1.00 | 2.86 | available |
| Prior incident calendar cohort | 2022–2023 | 181 | 35 | 1.00 | 4.22 | available |
| Prior incident calendar cohort | 2024 onward | 589 | 53 | 1.00 | 7.50 | available |
| Prior incident calendar cohort | Through 2018 | 26 | 22 | 1.00 | 7.56 | available |

The following simulations remove some already recorded follow-ups under stated hypothetical observation probabilities: 75% for all follow-ups, or 50% for small and medium organisations and 100% for other recorded sizes, including Unknown. First observed incidents are retained. They illustrate sensitivity and do not estimate or reconstruct unreported attacks.

The following table uses fixed size standardization for each simulation draw; the full size-specific results, support and valid draw counts are in `observation_bias_by_size.csv`. A missing rate means one or more reference size groups have no exposure.

| family | scenario | order group | size | replicates | valid rate draws | median repeats | median rate | p05 rate | p95 rate | reference weight covered |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| reporting | uniform 75% follow-up observation | All observed orders | STANDARDIZED | 100 | 100 | 106.00 | 3.43 | 3.09 | 3.72 | 1.00 |
| reporting | lower observation for small organisations | All observed orders | STANDARDIZED | 100 | 100 | 138.00 | 4.42 | 4.30 | 4.54 | 1.00 |

The prior-history scenarios hypothetically assign an unobserved earlier incident to a fraction of organisations; they relabel existing spells without fabricating dates or exposure. They are not corrections or estimates of the true unobserved history.

| family | scenario | order group | size | replicates | valid rate draws | median repeats | median rate | p05 rate | p95 rate | reference weight covered |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| history | 0% missing prior history | First | STANDARDIZED | 1 | 1 | 78.00 | 3.13 | 3.13 | 3.13 | 1.00 |
| history | 0% missing prior history | Later | STANDARDIZED | 1 | 1 | 63.00 | 20.67 | 20.67 | 20.67 | 1.00 |
| history | 25% missing prior history | First | STANDARDIZED | 100 | 100 | 58.00 | 3.16 | 2.74 | 3.49 | 1.00 |
| history | 25% missing prior history | Later | STANDARDIZED | 100 | 100 | 83.00 | 7.25 | 6.51 | 8.10 | 1.00 |
| history | 50% missing prior history | First | STANDARDIZED | 100 | 100 | 39.00 | 3.12 | 2.60 | 3.79 | 1.00 |
| history | 50% missing prior history | Later | STANDARDIZED | 100 | 100 | 102.00 | 5.65 | 5.10 | 6.10 | 1.00 |

The original aggregate CSVs are unadjusted descriptive counts, supplied for continuity; they should not be used as size-adjusted comparisons.

These are associations in a selected, publicly reported dataset, conditional on an observed first incident. They do not establish causal effects or population attack risk. Sparse groups and reporting changes can be important.

## Reproducibility

Database snapshot: `snap-20261031122958-2aaa6e711163` (retained under `instance/snapshots/`; rerun with `--snapshot-id snap-20261031122958-2aaa6e711163`).
Database snapshot SHA-256: `2aaa6e711163fe2bc8654c46283ae20628d9e38206025e8adc020a7363f166e3`.
Bootstrap replicates: 500; date draws: 100.

The detailed result tables are saved as CSV files alongside this report.
