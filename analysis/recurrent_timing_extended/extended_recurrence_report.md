# Extended recurrence analyses

Observation cutoff: 2026-11-03. Seed: 20260805.

The analyses describe recorded incidents among organisations with at least one recorded victim event. They do not estimate the probability of an attack across all Australian organisations.

Organisation size is included in comparisons using the recorded size categories. The standard population is the organisations in this observed cohort, counted once each; UNKNOWN remains a separate category. Size is an imperfect proxy for visibility, attack exposure and target value, which are not directly measured. Current size estimates may differ from size at the incident date, and residual confounding remains possible.

| Recorded size | Organisations | Standard population weight |
|---|---:|---:|
| SMALL | 119 | 13.1% |
| MEDIUM | 306 | 33.8% |
| LARGE | 301 | 33.2% |
| HUGE | 145 | 16.0% |
| UNKNOWN | 35 | 3.9% |

Retained 1063 victim-event attributions; excluded 0 after the incident-date cutoff and 0 first discovered after it. 0 retained attributions have unknown discovery dates.

Victim attributions left unresolved by the calibrated attribution check: 39 on 38 incident(s), included here (--unresolved-attributions).

Historical cutoffs filter known discovery dates, but the snapshot contains present-day incident links, fields, and classifications. It is not a complete historical database snapshot; retrospective and prospective interpretations must account for that limitation. Rerun with `--as-known-at DATE` to reconstruct incident links, dates, victims and size bands from the recorded history of when each became known (`InputHistoryStatus` says since when that history exists).

## Timing-band and date-precision sensitivity

Rates are per 100 entity-years. Four band schemes, size-specific rates, and a kernel-smoothed rate are directly standardized to the fixed baseline organisation-size distribution, including UNKNOWN; see `timing_band_size_adjusted.csv`, `timing_band_size_strata.csv`, and `timing_smooth_size_adjusted.csv`. The smooth-curve interval resamples organisations and retains fixed weights. An estimate is unavailable when a nonzero-weight size stratum lacks exposure; no unsupported group is silently dropped. Baseline size weights: {'SMALL': 0.13134657836644592, 'MEDIUM': 0.33774834437086093, 'LARGE': 0.3322295805739514, 'HUGE': 0.1600441501103753, 'UNKNOWN': 0.03863134657836645}. Recorded current size is an imperfect proxy, not a direct adjustment for visibility, attack surface or target value; size estimates may be stale or missing.

| Days | Repeat events | Exposure days | Unadjusted rate | Size-standardized rate | Unsupported sizes |
|---|---:|---:|---:|---:|---|
| 1-90 | 24 | 91048 | 9.63 | 8.65 | none |
| 91-180 | 27 | 81478 | 12.10 | 10.68 | none |
| 181-270 | 22 | 71077 | 11.31 | 9.18 | none |

Precision evidence among victim-event attributions: {'unknown': 427, 'explicit_day': 390, 'ambiguous_first_of_month': 126, 'explicit_month': 120}. Verified month and year dates are drawn within their stored interval (`event_date_earliest` / `event_date_latest`); a date whose precision is unverified has no interval - it is unknown, not a day, and is kept as stored only in the scenarios that say so. An initial day of a month is ambiguous, not proof that the date was imputed. `timing_date_precision_size_adjusted.csv` compares verified month/year intervals, two separately labelled hypothetical ambiguous-first scenarios, complete histories with explicitly day-precise metadata (324 entities, 7 with observed repeats), and complete histories in which every date has a verified interval (unverified organisations left out, never read as days). Insufficient explicit date precision for a reliable precisely dated subset. Imputation percentiles show sensitivity to date assumptions, not confidence intervals for incidence.

| Date scenario | 91–180-day size-standardized median rate | 2.5–97.5% of date draws | Valid draws |
|---|---:|---:|---:|
| stored dates | 10.68 | 10.68–10.68 | 1/1 |
| known intervals | 10.95 | 9.93–12.04 | 100/100 |
| ambiguous firsts as month | 9.98 | 8.80–10.96 | 100/100 |
| ambiguous firsts as month or year | 10.06 | 8.79–11.09 | 100/100 |
| explicit day only complete histories | 2.40 | 2.40–2.40 | 1/1 |
| verified intervals complete histories | 4.46 | 4.43–4.48 | 100/100 |

### Same-date ties and unknown ordering

A same-date tie is far more likely one incident deduplication has not joined than two incidents on one day; every such pair is listed, with the reason it is still two events, in `dashboard/close_pairs.html`. Until a tie is resolved, each tie policy states one assumption about it (`collapse`, the primary treatment, keeps every id in `timing_entity_date_ties.csv`):

- `collapse`: the tied incidents are one occurrence: one time point, never a repeat between them (their ids and count are kept).
- `zero_gap`: the tied incidents are distinct and one day apart, in id order: each after the first is a repeat after 1 day.
- `interval_spread`: the tied incidents are distinct and evenly spread across the verified interval they share (about interval length / n days apart, the first at the interval's start); a tie with no shared verified interval longer than a day is placed as under zero_gap.
- `interval_draw`: the tied incidents are distinct, each on a day drawn uniformly and independently within the verified interval they share, over the date draws; a tie with no shared verified interval longer than a day is placed as under zero_gap.

None of these bounds the truth: a tie that is one incident filed twice is represented only by `collapse`, two incidents really weeks apart within a verified month exactly by none, and a tie whose dates are unverified has no interval to place them in. Agreement across the policies shows only that a result does not hinge on these assumptions; `interval_draw` is the median of 100 draws. Ties found: {'ties (organisation-dates with several incidents)': 0, 'incidents in ties': 0, 'organisations with a tie': 0, 'repeats a policy that separates tied incidents adds': 0, 'ties with a shared verified interval longer than a day (spread by interval_spread)': 0, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 0, 'ties: other_day': 0}. Incidents on different stored dates whose verified intervals overlap are no tie, but their dates do not order them either: {'consecutive pairs with overlapping verified intervals': 2, '... of which stored dates differ by under 90 days': 2}; the `known_intervals` scenario varies those dates within their intervals.

| Tie policy | Days | Repeat events | Unadjusted rate | Size-standardized rate |
|---|---|---:|---:|---:|
| collapse | 1-90 | 24 | 9.63 | 8.65 |
| collapse | 91-180 | 27 | 12.10 | 10.68 |
| collapse | 181-270 | 22 | 11.31 | 9.18 |
| zero_gap | 1-90 | 24 | 9.63 | 8.65 |
| zero_gap | 91-180 | 27 | 12.10 | 10.68 |
| zero_gap | 181-270 | 22 | 11.31 | 9.18 |
| interval_spread | 1-90 | 24 | 9.63 | 8.65 |
| interval_spread | 91-180 | 27 | 12.10 | 10.68 |
| interval_spread | 181-270 | 22 | 11.31 | 9.18 |
| interval_draw | 1-90 | 24 | 9.63 | 8.65 |
| interval_draw | 91-180 | 27 | 12.10 | 10.68 |
| interval_draw | 181-270 | 22 | 11.31 | 9.18 |

A separate prospective size-adjusted protocol freezes the cohort and recorded size at registration, and compares 91–180 days with the adjacent 1–90 and 181–270 days using a conditional test within size strata. The fixed end is 2028-10-27 and at least 20 comparison events in supported strata are required. Current size-adjusted validation status: **pending**. The test assumes independent Poisson counts within size strata; shared incidents, sparse strata, mutable records and missing earlier history limit interpretation. The original unadjusted registered protocol is retained separately in `timing_peak_validation.json`; its definition has not been changed. Preserve `timing_size_adjusted_registration.json` for future validation.

### Linkage and shared-incident sensitivity

The scenarios below are independent alternatives to the baseline. Rates are recorded repeat events per 100 entity-years at risk; rate ratios compare days 91–180 with days 1–90. Each rate is directly standardised to the same baseline distribution of organisation sizes, so differing size composition between scenarios and time bands is accounted for.

Baseline size weights: {'SMALL': 0.13134657836644592, 'MEDIUM': 0.33774834437086093, 'LARGE': 0.3322295805739514, 'HUGE': 0.1600441501103753, 'UNKNOWN': 0.03863134657836645}. UNKNOWN is a separate stratum. Organisation size is a current recorded category, not a complete measurement of exposure, target value or reporting visibility.

| Scenario | Evidence status | Decisions applied | Victim attributions | Incidents | 0–90 events / days | 91–180 events / days | Size-standardised ratio | Adjustment status |
|---|---|---:|---:|---:|---:|---:|---:|---|
| baseline | observed cohort | 0 | 1063 | 1008 | 24 / 91048 | 27 / 81478 | 1.23 | estimated |
| active_same_overrides | evaluated | 1 | 1063 | 1007 | 24 / 91048 | 27 / 81478 | 1.23 | estimated |
| model_same_probability_ge_0.9 | evaluated | 2 | 1062 | 1006 | 23 / 91035 | 27 / 81479 | 1.30 | estimated |
| model_victim_probability_lt_0.5_excluded | evaluated | 115 | 948 | 900 | 19 / 81877 | 17 / 74426 | 1.02 | estimated |

Stored scores cover 377 in-cohort incident pairs and 1057 victim attributions. Active different-incident overrides vetoed 0 override merges and 4 model-candidate merges, including conflicts created transitively. 1 current incident(s) contain an active different-incident override; these cannot be split reliably without per-report victim and date attribution.
Model scores are uncalibrated stored judgments, not confirmed truth; thresholds 0.9 and 0.5 define sensitivity scenarios, not validation. A merged incident uses the earliest observed date and the union of attributed victims. Evidence can be stale. Missing judgment or override tables are reported as unavailable. These scenarios cannot identify missing judgments or unrecorded victims.

25 recorded incidents have multiple attributed victims, involving 76 distinct victims and 112 event-level victim pairs; 6 shared incidents have a supplier name recorded. Supplier names alone are not counted as victims. The observed network has 851 components, largest 7 organisations.
The size-standardised bootstrap compares resampling organisations with resampling connected components (500 requested replicates):

| Resampling unit | Metric | Estimate | 95% percentile interval | Valid replicates |
|---|---|---:|---:|---:|
| entity | rate_0_90 | 8.65 | 5.20–12.13 | 500 |
| entity | rate_91_180 | 10.68 | 6.26–16.28 | 500 |
| entity | rate_ratio | 1.23 | 0.64–2.19 | 500 |
| shared_incident_component | rate_0_90 | 8.65 | 5.12–12.11 | 500 |
| shared_incident_component | rate_91_180 | 10.68 | 6.30–15.46 | 500 |
| shared_incident_component | rate_ratio | 1.23 | 0.71–2.13 | 500 |

Components connect organisations through any recorded shared incident, not only incidents with a named supplier. Bootstrap intervals use fixed baseline weights and retain all sizes within each component; draws lacking exposure in any required stratum are omitted and valid counts are shown. Small cells, zero early events and missing shared incidents limit inference. Stratum-specific rates are in linkage_size_strata.csv; crude results remain separately available for comparison.

## Event order and attack-type transitions

Comparisons use the same reference distribution of baseline unique organisations by current recorded size (SMALL, MEDIUM, LARGE, HUGE, UNKNOWN), including unknown size as its own stratum. Contradictory size labels for an organisation are treated as UNKNOWN. Direct standardized rates are unavailable if any reference stratum lacks exposure; no missing stratum is extrapolated. Size helps account for differences associated with visibility, exposure and target value, but those factors are not directly measured here and size adjustment cannot remove all confounding. The reference weights are in `recurrence_size_reference_weights.csv`.

These first/later recorded-incident rates are standardized to that common size distribution. Spells end at the next recorded incident or the censor date; censored spells contribute exposure. Use the size-stratified CSV to inspect sparse groups; Poisson intervals do not account for dependence between incidents at one organisation.

| dimension | group | organisations | repeats | reference weight covered | standardized rate per 100 entity years | status |
| --- | --- | --- | --- | --- | --- | --- |
| Event order | First observed | 906 | 105 | 1.00 | 4.96 | available |
| Event order | Second or later observed | 105 | 52 | 0.96 |  | unavailable: missing size exposure |

Transition counts and within-size conditional shares appear in `attack_type_transitions_by_size.csv`. These are conditional on a subsequent observed incident. Unknown types remain Unknown; different or unknown types on the same day are labelled Multiple or ambiguous. Cells are sparse and are not causal estimates.

| size | prior attack type | next attack type | transitions | organisations | share of prior type within size |
| --- | --- | --- | --- | --- | --- |
| HUGE | Data Breach | Data Breach | 32 | 23 | 0.58 |
| HUGE | Data Breach | Denial of Service | 2 | 2 | 0.04 |
| HUGE | Data Breach | Insider Threat | 1 | 1 | 0.02 |
| HUGE | Data Breach | Malware | 1 | 1 | 0.02 |
| HUGE | Data Breach | Other | 7 | 7 | 0.13 |
| HUGE | Data Breach | Phishing | 3 | 3 | 0.05 |
| HUGE | Data Breach | Ransomware | 4 | 4 | 0.07 |
| HUGE | Data Breach | State-Sponsored Attack | 2 | 1 | 0.04 |
| HUGE | Data Breach | Supply Chain Attack | 1 | 1 | 0.02 |
| HUGE | Data Breach | Vulnerability Exploit | 2 | 2 | 0.04 |
| HUGE | Denial of Service | Other | 1 | 1 | 1.00 |
| HUGE | Financial Fraud | Identity Theft | 1 | 1 | 0.50 |
| HUGE | Financial Fraud | Ransomware | 1 | 1 | 0.50 |
| HUGE | Infrastructure Attack | Data Breach | 2 | 2 | 1.00 |
| HUGE | Other | Data Breach | 3 | 3 | 0.25 |
| HUGE | Other | Financial Fraud | 1 | 1 | 0.08 |
| HUGE | Other | Insider Threat | 1 | 1 | 0.08 |
| HUGE | Other | Other | 4 | 4 | 0.33 |
| HUGE | Other | Phishing | 1 | 1 | 0.08 |
| HUGE | Other | State-Sponsored Attack | 1 | 1 | 0.08 |

Showing 20 of 57 rows; the CSV contains all rows.

## Reporting and observation bias

Source history: available; 0 event IDs with all known discovery dates after the cutoff excluded; 0 source records lack discovery dates. Organisation sizes are current recorded estimates and may not describe their size at the incident date.

Source groups refer to the prior incident, are mutually exclusive, and show source availability—not the chance that an incident was reported. Calendar groups are the prior incident's cohort, not calendar-time-adjusted rate ratios. Missing source history is Unknown.

The following source and calendar rates are standardized to the same fixed size weights. Size-specific rates and exposure are in `recurrence_order_source_calendar_by_size.csv`.

| dimension | group | organisations | repeats | reference weight covered | standardized rate per 100 entity years | status |
| --- | --- | --- | --- | --- | --- | --- |
| Prior incident source | CyberDaily | 162 | 18 | 1.00 | 6.01 | available |
| Prior incident source | GoogleSearch | 52 | 5 | 1.00 | 3.02 | available |
| Prior incident source | Multiple sources | 454 | 74 | 1.00 | 6.84 | available |
| Prior incident source | Perplexity | 244 | 52 | 1.00 | 9.16 | available |
| Prior incident source | WebberInsurance | 85 | 8 | 1.00 | 1.74 | available |
| Prior incident calendar cohort | 2019–2021 | 115 | 41 | 1.00 | 4.92 | available |
| Prior incident calendar cohort | 2022–2023 | 214 | 56 | 1.00 | 6.56 | available |
| Prior incident calendar cohort | 2024 onward | 644 | 60 | 1.00 | 7.41 | available |

The following simulations remove some already recorded follow-ups under stated hypothetical observation probabilities: 75% for all follow-ups, or 50% for small and medium organisations and 100% for other recorded sizes, including Unknown. First observed incidents are retained. They illustrate sensitivity and do not estimate or reconstruct unreported attacks.

The following table uses fixed size standardization for each simulation draw; the full size-specific results, support and valid draw counts are in `observation_bias_by_size.csv`. A missing rate means one or more reference size groups have no exposure.

| family | scenario | order group | size | replicates | valid rate draws | median repeats | median rate | p05 rate | p95 rate | reference weight covered |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| reporting | uniform 75% follow-up observation | All observed orders | STANDARDIZED | 100 | 100 | 118.00 | 4.47 | 4.13 | 4.82 | 1.00 |
| reporting | lower observation for small organisations | All observed orders | STANDARDIZED | 100 | 100 | 148.00 | 5.45 | 5.28 | 5.63 | 1.00 |

The prior-history scenarios hypothetically assign an unobserved earlier incident to a fraction of organisations; they relabel existing spells without fabricating dates or exposure. They are not corrections or estimates of the true unobserved history.

| family | scenario | order group | size | replicates | valid rate draws | median repeats | median rate | p05 rate | p95 rate | reference weight covered |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| history | 0% missing prior history | First | STANDARDIZED | 1 | 1 | 105.00 | 4.96 | 4.96 | 4.96 | 1.00 |
| history | 0% missing prior history | Later | STANDARDIZED | 1 | 0 | 52.00 |  |  |  | 0.96 |
| history | 25% missing prior history | First | STANDARDIZED | 100 | 100 | 79.00 | 4.96 | 4.48 | 5.44 | 1.00 |
| history | 25% missing prior history | Later | STANDARDIZED | 100 | 100 | 78.00 | 7.74 | 6.87 | 8.63 | 1.00 |
| history | 50% missing prior history | First | STANDARDIZED | 100 | 100 | 53.00 | 5.03 | 4.12 | 5.79 | 1.00 |
| history | 50% missing prior history | Later | STANDARDIZED | 100 | 100 | 104.00 | 6.70 | 6.13 | 7.42 | 1.00 |

The original aggregate CSVs are unadjusted descriptive counts, supplied for continuity; they should not be used as size-adjusted comparisons.

These are associations in a selected, publicly reported dataset, conditional on an observed first incident. They do not establish causal effects or population attack risk. Sparse groups and reporting changes can be important.

## Reproducibility

Database snapshot: `snap-20261106160634-1edbf49c696a` (retained under `instance/snapshots/`; rerun with `--snapshot-id snap-20261106160634-1edbf49c696a`).
Database snapshot SHA-256: `1edbf49c696a2e3d579bee878951a011ee865c4b91a31f9e8b572b5c8afa8676`.
Bootstrap replicates: 500; date draws: 100.

The detailed result tables are saved as CSV files alongside this report.
