# Repeat Cyber-Event Timing Analysis

## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.
The primary model includes recorded organisation size. Size is a proxy for differences in visibility, attack exposure and target value, not a direct measure of those factors; residual confounding remains possible. The ordinal size term assumes equal log-rate steps between categories, UNKNOWN has a separate indicator, and current recorded size may differ from size at an incident's date. Unadjusted rates, plots, parametric models and size-dropped variants below are descriptive diagnostics and do not control for this confounding.

## Data Quality Summary

- victim event attributions: 1128
- unique deduplicated events: 910
- victim entities: 941
- entities with more than one event: 97
- post-event spells: 1123
- observed repeat spells: 182
- right-censored spells: 941
- censor date: 2026-10-30
- elapsed-time bands: 0-90, 91-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2003-07-04
- maximum event date: 2026-10-27
- first-of-month event dates: 256
- verified incident-date precision: {'unverified': 654, 'day': 240, 'month': 234}
- tie policy (several incidents of one organisation on one date): collapse
- same-date ties: {'ties (organisation-dates with several incidents)': 5, 'incidents in ties': 10, 'organisations with a tie': 5, 'repeats a zero-gap policy would add': 5, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 3, 'ties: other_day': 2, 'repeats added by the zero_gap policy in this run': 0}
- incidents not ordered by their dates (different stored dates, overlapping verified intervals): {'consecutive pairs with overlapping verified intervals': 0, '... of which stored dates differ by under 90 days': 0}
- covariate coverage: {'size_estimate levels': {'HUGE': 296, 'LARGE': 326, 'MEDIUM': 299, 'SMALL': 109, 'UNKNOWN': 98}, 'size_confidence populated': 1128, 'employee_count populated': 991, 'turnover populated': 0, 'industry populated': 989, 'entity_kind populated': 1128, 'sector_proxy levels': {'education': 80, 'finance': 78, 'government': 201, 'health': 101, 'industrial': 25, 'media': 8, 'other': 399, 'retail': 45, 'technology': 140, 'telecom_transport': 51}, 'prior records populated': 510}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 55, 'events with an unresolved attribution': 55, 'organisations with an unresolved attribution': 54, 'victim-event attributions analysed': 1128, 'repeats': {'included': {'observed repeats': 182, 'entities with a repeat': 95, 'repeats by elapsed band': {'0-90': 25, '91-180': 27, '181-365': 30, '366-730': 33, '731-1460': 43, '>1460': 24}}, 'excluded': {'observed repeats': 158, 'entities with a repeat': 85, 'repeats by elapsed band': {'0-90': 20, '91-180': 23, '181-365': 25, '366-730': 28, '731-1460': 41, '>1460': 21}}, 'repeats involving an unresolved attribution': 29}}

## Same-Date Ties And Unknown Ordering

Several distinct incidents of one organisation can carry the same stored date; nothing orders them, and a date stored as the 1st of a month may only mean that month. The primary analysis (`collapse`) makes the tied incidents one time point - their ids and count are kept in `entity_date_ties.csv`, and no order or gap between them is invented - which can understate repeats. The `zero_gap` sensitivity counts each as its own incident one day after the previous, a repeat in the first band, which can overstate them (a tie may be one incident filed twice or a month placeholder). The truth lies between; both are adjusted for organisation size like the primary model.

| tie_policy   |   observed_repeats |   repeats_from_ties |   post_event_spells |   repeats_0-90 |   repeats_91-180 |   repeats_181-365 |   repeats_366-730 |   repeats_731-1460 |   repeats_>1460 |   adjusted_constant_hazard_p |   adjusted_0_90_91_180_banding_p |   delayed_peak_directional_p |
|:-------------|-------------------:|--------------------:|--------------------:|---------------:|-----------------:|------------------:|------------------:|-------------------:|----------------:|-----------------------------:|---------------------------------:|-----------------------------:|
| collapse     |                182 |                   0 |                1123 |             25 |               27 |                30 |                33 |                 43 |              24 |                      0.2048  |                          0.2048  |                       0.238  |
| zero_gap     |                187 |                   5 |                1128 |             30 |               27 |                30 |                33 |                 43 |              24 |                      0.07478 |                          0.07478 |                       0.4781 |


## Primary Piecewise Exponential Test

The adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.205). It rests on 182 observed repeats.

- unadjusted LRT p-value: 0.04364
- adjusted LRT p-value: 0.2048
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=2), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |       25 |       96396.000 |                       9.473 |              5.819 |              13.920 |
| 91-180         |       27 |       86580.000 |                      11.390 |              6.669 |              16.019 |
| 181-365        |       30 |      153565.000 |                       7.135 |              4.214 |              10.393 |
| 366-730        |       33 |      236019.000 |                       5.107 |              3.118 |               7.232 |
| 731-1460       |       43 |      238653.000 |                       6.581 |              4.524 |               8.939 |
| >1460          |       24 |      131016.000 |                       6.691 |              4.214 |               9.805 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        1.949 |    1.515 |     2.507 | about 95% higher repeat-event rate  |
| SMALL -> LARGE  |        3.798 |    2.294 |     6.285 | about 280% higher repeat-event rate |
| SMALL -> HUGE   |        7.401 |    3.476 |    15.758 | about 640% higher repeat-event rate |
| MEDIUM -> HUGE  |        3.798 |    2.294 |     6.285 | about 280% higher repeat-event rate |
| LARGE -> HUGE   |        1.949 |    1.515 |     2.507 | about 95% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| education           |        2.740 |    1.153 |     6.510 | about 174% higher than government |
| finance             |        2.051 |    0.911 |     4.617 | about 105% higher than government |
| telecom_transport   |        1.915 |    0.899 |     4.079 | about 92% higher than government  |
| technology          |        1.862 |    0.856 |     4.052 | about 86% higher than government  |
| industrial          |        1.591 |    0.412 |     6.143 | about 59% higher than government  |
| health              |        1.309 |    0.492 |     3.483 | about 31% higher than government  |
| sparse_or_no_repeat |        0.660 |    0.175 |     2.487 | about 34% lower than government   |
| other               |        0.516 |    0.216 |     1.230 | about 48% lower than government   |

Full adjusted covariate table:

| effect                           |   rate_ratio |   ci_low |                                                                                                                                                                                                                                                                ci_high |
|:---------------------------------|-------------:|---------:|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------:|
| Entity kind: government_body     |        1.970 |    0.956 |                                                                                                                                                                                                                                                                  4.058 |
| Entity kind: sparse_or_no_repeat |        0.000 |    0.000 | 498557114518821088867899438348484374991224051884501744908540800717648856181214801776183223977048765926790582159890344061041281483044557347652090287754929228194811157590489959722364579460470989162873929555398077744923389693469077419560457285421874558816747520.000 |
| Organisation size                |        1.949 |    1.515 |                                                                                                                                                                                                                                                                  2.507 |
| Prior records: 100k+             |        0.967 |    0.639 |                                                                                                                                                                                                                                                                  1.464 |
| Prior records: 10k-100k          |        1.125 |    0.742 |                                                                                                                                                                                                                                                                  1.705 |
| Prior records: 1k-10k            |        0.917 |    0.536 |                                                                                                                                                                                                                                                                  1.570 |
| Prior records: <1k               |        0.898 |    0.481 |                                                                                                                                                                                                                                                                  1.675 |
| Sector: education                |        2.740 |    1.153 |                                                                                                                                                                                                                                                                  6.510 |
| Sector: finance                  |        2.051 |    0.911 |                                                                                                                                                                                                                                                                  4.617 |
| Sector: health                   |        1.309 |    0.492 |                                                                                                                                                                                                                                                                  3.483 |
| Sector: industrial               |        1.591 |    0.412 |                                                                                                                                                                                                                                                                  6.143 |
| Sector: other                    |        0.516 |    0.216 |                                                                                                                                                                                                                                                                  1.230 |
| Sector: sparse_or_no_repeat      |        0.660 |    0.175 |                                                                                                                                                                                                                                                                  2.487 |
| Sector: technology               |        1.862 |    0.856 |                                                                                                                                                                                                                                                                  4.052 |
| Sector: telecom_transport        |        1.915 |    0.899 |                                                                                                                                                                                                                                                                  4.079 |
| Unknown size                     |        1.291 |    0.662 |                                                                                                                                                                                                                                                                  2.518 |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 91-180, 181-365 days vs 0-90 days (unadjusted): rate ratio 0.92 (95% CI 0.57-1.46), about 8% lower; two-sided p = 0.712. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.57 to 1.46, which includes 1. The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 57 in 91-180, 181-365 days and 25 in 0-90 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.912. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates do not have the hypothesised shape: long-term vs response goes the other way.

- response vs immediate: rate ratio 0.94 (95% CI 0.58-1.50), about 6% lower; one-sided p = 0.392 (alternative: lower). Not statistically significant at the 5% level: the data do not reject the null of no decrease (rate ratio at least 1); the 95% CI runs from 0.58 to 1.50, which includes 1. Repeat events: 56 in the response period and 25 in the immediate period.
- long-term vs response: rate ratio 0.79 (95% CI 0.57-1.11), about 21% lower; one-sided p = 0.912 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.57 to 1.11, which includes 1. The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 99 in the long-term period and 56 in the response period.
- long-term vs immediate: rate ratio 0.74 (95% CI 0.48-1.16), about 26% lower; two-sided p = 0.193. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.48 to 1.16, which includes 1. Repeat events: 99 in the long-term period and 25 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.2616
- directional U-shape p-value: 0.9116

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       25 |           96396 |                       9.473 |
| Response period  |       56 |          240145 |                       8.517 |
| Long-term period |       99 |          605688 |                       5.970 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        0.936 |    0.584 |     1.500 | about 6% lower   |         0.783 | less                      | 0.392           |
| long-term vs response  |        0.794 |    0.569 |     1.109 | about 21% lower  |         0.177 | greater                   | 0.912           |
| long-term vs immediate |        0.743 |    0.476 |     1.162 | about 26% lower  |         0.193 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        0.936 |    0.584 |     1.500 |      0.917 | about 6% lower   |           0.392 |
| All covariates                | long-term vs response |        0.794 |    0.569 |     1.109 |      0.541 | about 21% lower  |           0.912 |
| Drop organisation size        | response vs immediate |        0.950 |    0.593 |     1.524 |      0.931 | about 5% lower   |           0.416 |
| Drop organisation size        | long-term vs response |        0.810 |    0.580 |     1.130 |      0.550 | about 19% lower  |           0.893 |
| Drop industry/sector          | response vs immediate |        0.934 |    0.582 |     1.497 |      0.914 | about 7% lower   |           0.388 |
| Drop industry/sector          | long-term vs response |        0.802 |    0.574 |     1.120 |      0.545 | about 20% lower  |           0.903 |
| Drop size and industry/sector | response vs immediate |        0.952 |    0.594 |     1.526 |      0.932 | about 5% lower   |           0.419 |
| Drop size and industry/sector | long-term vs response |        0.839 |    0.602 |     1.170 |      0.568 | about 16% lower  |           0.849 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.2616 |                         0.9116 |
| Drop organisation size        |    0.3331 |                         0.8927 |
| Drop industry/sector          |    0.2844 |                         0.9025 |
| Drop size and industry/sector |    0.4551 |                         0.8491 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.58-1.50). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.57-1.11).

## Delayed 91-180 Day Peak Test

Definition used here: the hypothesised peak is 91-180 days after the prior event. A formal peak requires 91-180 days to be higher than both 0-90 days and the pooled post-180 period.
The adjusted peak model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main model.

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.238. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates have the hypothesised shape. The weakest part is 91-180 vs 0-90 (p = 0.238).

- 91-180 vs 0-90: rate ratio 1.22 (95% CI 0.71-2.10), about 22% higher; one-sided p = 0.238 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.71 to 2.10, which includes 1. Repeat events: 27 in 91-180 days and 25 in 0-90 days.
- 91-180 vs post-180: rate ratio 1.63 (95% CI 1.07-2.47), about 63% higher; one-sided p = 0.011 (alternative: higher). Statistically significant at the 5% level: the data reject the null of no increase (rate ratio at most 1). Repeat events: 27 in 91-180 days and 128 in post-180 days.
- post-180 vs 0-90: rate ratio 0.75 (95% CI 0.49-1.16), about 25% lower; two-sided p = 0.191. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.49 to 1.16, which includes 1. Repeat events: 128 in post-180 days and 25 in 0-90 days.

- phase-model LRT p-value versus constant elapsed-time risk: 0.06144
- directional delayed-peak p-value: 0.238

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |       25 |           96396 |                       9.473 |
| Delayed peak 91-180 days |       27 |           86580 |                      11.390 |
| Post-180 days            |      128 |          759253 |                       6.158 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        1.219 |    0.707 |     2.100 | about 22% higher |         0.476 | greater                   | 0.238           |
| 91-180 vs post-180 |        1.628 |    1.071 |     2.475 | about 63% higher |         0.023 | greater                   | 0.011           |
| post-180 vs 0-90   |        0.749 |    0.485 |     1.156 | about 25% lower  |         0.191 |                           |                 |

## Unadjusted Parametric Survival Model Comparison (Descriptive)

Best AIC model: weibull. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| weibull           |        -1735.702 |            2 | 3475.404 | 3485.451 | True        |
| generalized_gamma |        -1734.952 |            3 | 3475.903 | 3490.975 | True        |
| loglogistic       |        -1737.189 |            2 | 3478.378 | 3488.426 | True        |
| exponential       |        -1738.463 |            1 | 3478.927 | 3483.951 | True        |
| lognormal         |        -1742.650 |            2 | 3489.301 | 3499.348 | True        |

Exponential vs Weibull LRT p-value: 0.01877

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
