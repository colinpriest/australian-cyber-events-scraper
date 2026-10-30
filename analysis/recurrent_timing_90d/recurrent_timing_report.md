# Repeat Cyber-Event Timing Analysis

## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.
The primary model includes recorded organisation size. Size is a proxy for differences in visibility, attack exposure and target value, not a direct measure of those factors; residual confounding remains possible. The ordinal size term assumes equal log-rate steps between categories (compared with categorical size and a size-by-elapsed-time interaction under Organisation Size Specification), UNKNOWN has a separate indicator, an organisation stored under several names takes the size its variants support best (`organisation_size_reconciliation.csv`), and current recorded size may differ from size at an incident's date. Unadjusted rates, plots, parametric models and size-dropped variants below are descriptive diagnostics and do not control for this confounding.

## Data Quality Summary

- victim event attributions: 1125
- unique deduplicated events: 905
- victim entities: 956
- entities with more than one event: 86
- post-event spells: 1121
- observed repeat spells: 165
- right-censored spells: 956
- censor date: 2026-10-30
- elapsed-time bands: 0-90, 91-365, 366-730, 731-1460, >1460
- minimum event date: 2003-07-04
- maximum event date: 2026-10-27
- first-of-month event dates: 254
- verified incident-date precision: {'unverified': 651, 'day': 240, 'month': 234}
- tie policy (several incidents of one organisation on one date): collapse
- same-date ties: {'ties (organisation-dates with several incidents)': 4, 'incidents in ties': 8, 'organisations with a tie': 4, 'repeats a policy that separates tied incidents adds': 4, 'ties with a shared verified interval longer than a day (spread by interval_spread)': 0, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 3, 'ties: other_day': 1, 'repeats between tied incidents in this run': 0}
- incidents not ordered by their dates (different stored dates, overlapping verified intervals): {'consecutive pairs with overlapping verified intervals': 0, '... of which stored dates differ by under 90 days': 0}
- covariate coverage: {'size_estimate levels': {'HUGE': 287, 'LARGE': 331, 'MEDIUM': 301, 'SMALL': 111, 'UNKNOWN': 95}, 'size_confidence populated': 1124, 'employee_count populated': 991, 'turnover populated': 0, 'industry populated': 985, 'entity_kind populated': 1125, 'sector_proxy levels': {'education': 80, 'finance': 80, 'government': 202, 'health': 98, 'industrial': 24, 'media': 8, 'other': 400, 'retail': 46, 'technology': 138, 'telecom_transport': 49}, 'prior records populated': 511}
- organisation size across name variants: {'organisations stored under several name variants': 3, 'organisations whose size changed against the representative-only rule': 1, 'organisations whose variants give conflicting known size bands': 0, 'deciding rule': {'known band over UNKNOWN': 2, 'higher size_confidence': 1}}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 51, 'events with an unresolved attribution': 51, 'organisations with an unresolved attribution': 50, 'victim-event attributions analysed': 1125, 'repeats': {'included': {'observed repeats': 165, 'entities with a repeat': 84, 'repeats by elapsed band': {'0-90': 24, '91-365': 53, '366-730': 30, '731-1460': 34, '>1460': 24}}, 'excluded': {'observed repeats': 146, 'entities with a repeat': 78, 'repeats by elapsed band': {'0-90': 20, '91-365': 45, '366-730': 24, '731-1460': 34, '>1460': 23}}, 'repeats involving an unresolved attribution': 24}}

## Same-Date Ties And Unknown Ordering

Several distinct incidents of one organisation can carry the same stored date; nothing orders them, and a date stored as the 1st of a month may only mean that month. Each tie policy makes one stated assumption about the tied incidents (every tie, with its incident ids and the length of any verified interval its incidents share, is in `entity_date_ties.csv`):

- `collapse` (primary): the tied incidents are one occurrence: one time point, never a repeat between them (their ids and count are kept).
- `zero_gap`: the tied incidents are distinct and one day apart, in id order: each after the first is a repeat after 1 day.
- `interval_spread`: the tied incidents are distinct and evenly spread across the verified interval they share (about interval length / n days apart, the first at the interval's start); a tie with no shared verified interval longer than a day is placed as under zero_gap.

None of these bounds the truth: a tie that is one incident filed twice is represented only by `collapse`; two incidents really weeks apart within a verified month are represented exactly by none; and a tie whose dates are unverified has no interval to place its incidents in, so `interval_spread` places it as `zero_gap` does. Agreement across the policies shows only that a result does not hinge on these assumptions. Every policy is adjusted for organisation size like the primary model.

- Under `collapse`, the adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.213). It rests on 165 observed repeats.
- Under `zero_gap`, the adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.074, borderline). It rests on 169 observed repeats.
- Under `interval_spread`, the adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.074, borderline). It rests on 169 observed repeats.

| tie_policy      |   observed_repeats |   repeats_from_ties |   post_event_spells |   repeats_0-90 |   repeats_91-365 |   repeats_366-730 |   repeats_731-1460 |   repeats_>1460 |   adjusted_constant_hazard_p |   adjusted_0_90_91_180_banding_p |   delayed_peak_directional_p |
|:----------------|-------------------:|--------------------:|--------------------:|---------------:|-----------------:|------------------:|-------------------:|----------------:|-----------------------------:|---------------------------------:|-----------------------------:|
| collapse        |                165 |                   0 |                1121 |             24 |               53 |                30 |                 34 |              24 |                      0.2134  |                           0.2716 |                       0.4559 |
| zero_gap        |                169 |                   4 |                1125 |             28 |               53 |                30 |                 34 |              24 |                      0.07418 |                           0.1051 |                       0.6671 |
| interval_spread |                169 |                   4 |                1125 |             28 |               53 |                30 |                 34 |              24 |                      0.07418 |                           0.1051 |                       0.6671 |


## Organisation Size Specification

The primary model enters size as a numeric score (SMALL=1 to HUGE=4, UNKNOWN mean-imputed, plus an UNKNOWN indicator), which assumes equal log-rate steps between bands. It is compared here with size as a categorical factor (reference: the band with the most repeats; bands with fewer than 3 repeats pooled) and, where every delayed-peak phase has at least 10 repeats at two or more known size bands, with the score's interaction with the delayed-peak phases. The score stays primary; the headline tests are re-run under categorical size. Full table: `size_specification_comparison.csv`.

- Size specification (bands 0-90, 91-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.388 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 1166.3 (categorical) against 1164.2 (score), lower for the score model.
- With bands 0-90, 91-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.213 with the size score and p = 0.211 with categorical size: the same verdict at the 5% level.
- Size specification (bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.387 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 1225.4 (categorical) against 1223.3 (score), lower for the score model.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.272 with the size score and p = 0.268 with categorical size: the same verdict at the 5% level.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the directional delayed-peak test gives p = 0.456 with the size score and p = 0.456 with categorical size: the same verdict at the 5% level.
- Size by elapsed-time phase (the size score's slope in 91-180 and post-180 days against 0-90): LRT p = 0.163 on 2 df; at the 5% level the data do not reject a size effect that is the same in every phase.

| banding       | size_spec     |   log_likelihood |   parameters |   aic |   delta_aic_vs_score | nested   |   likelihood_ratio |   df |   p_value |   elapsed_constant_hazard_p |   delayed_peak_directional_p |
|:--------------|:--------------|-----------------:|-------------:|------:|---------------------:|:---------|-------------------:|-----:|----------:|----------------------------:|-----------------------------:|
| run           | score         |           -556.1 |           26 |  1164 |               0      |          |            nan     |  nan |  nan      |                      0.2134 |                     nan      |
| run           | categorical   |           -555.1 |           28 |  1166 |               2.107  | True     |              1.893 |    2 |    0.3881 |                      0.2107 |                     nan      |
| 0-90 / 91-180 | score         |           -584.7 |           27 |  1223 |               0      |          |            nan     |  nan |  nan      |                      0.2716 |                       0.4559 |
| 0-90 / 91-180 | categorical   |           -583.7 |           29 |  1225 |               2.103  | True     |              1.897 |    2 |    0.3873 |                      0.2682 |                       0.456  |
| 0-90 / 91-180 | score_x_phase |           -582.8 |           29 |  1224 |               0.3708 | True     |              3.629 |    2 |    0.1629 |                    nan      |                     nan      |


## Primary Piecewise Exponential Test

The adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.213). It rests on 165 observed repeats.

- unadjusted LRT p-value: 0.03147
- adjusted LRT p-value: 0.2134
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=2), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |       24 |       96306.000 |                       9.102 |              5.170 |              13.665 |
| 91-365         |       53 |      240981.000 |                       8.033 |              5.411 |              10.985 |
| 366-730        |       30 |      237162.000 |                       4.620 |              2.806 |               6.630 |
| 731-1460       |       34 |      243197.000 |                       5.106 |              3.090 |               6.948 |
| >1460          |       24 |      137580.000 |                       6.372 |              4.075 |               8.731 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        2.142 |    1.623 |     2.826 | about 114% higher repeat-event rate |
| SMALL -> LARGE  |        4.587 |    2.634 |     7.987 | about 359% higher repeat-event rate |
| SMALL -> HUGE   |        9.824 |    4.276 |    22.572 | about 882% higher repeat-event rate |
| MEDIUM -> HUGE  |        4.587 |    2.634 |     7.987 | about 359% higher repeat-event rate |
| LARGE -> HUGE   |        2.142 |    1.623 |     2.826 | about 114% higher repeat-event rate |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| education           |        3.849 |    1.597 |     9.276 | about 285% higher than government |
| finance             |        2.591 |    1.121 |     5.985 | about 159% higher than government |
| technology          |        2.567 |    1.168 |     5.643 | about 157% higher than government |
| industrial          |        2.270 |    0.582 |     8.850 | about 127% higher than government |
| telecom_transport   |        2.237 |    1.036 |     4.830 | about 124% higher than government |
| health              |        1.499 |    0.621 |     3.616 | about 50% higher than government  |
| sparse_or_no_repeat |        0.948 |    0.251 |     3.582 | about 5% lower than government    |
| other               |        0.560 |    0.220 |     1.425 | about 44% lower than government   |

Full adjusted covariate table:

| effect                           |   rate_ratio |   ci_low |   ci_high |
|:---------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body     |        2.442 |    1.191 |     5.006 |
| Entity kind: sparse_or_no_repeat |        1.278 |    0.151 |    10.805 |
| Organisation size                |        2.142 |    1.623 |     2.826 |
| Prior records: 100k+             |        1.040 |    0.677 |     1.599 |
| Prior records: 10k-100k          |        1.208 |    0.777 |     1.877 |
| Prior records: 1k-10k            |        1.073 |    0.609 |     1.889 |
| Prior records: <1k               |        1.135 |    0.596 |     2.163 |
| Sector: education                |        3.849 |    1.597 |     9.276 |
| Sector: finance                  |        2.591 |    1.121 |     5.985 |
| Sector: health                   |        1.499 |    0.621 |     3.616 |
| Sector: industrial               |        2.270 |    0.582 |     8.850 |
| Sector: other                    |        0.560 |    0.220 |     1.425 |
| Sector: sparse_or_no_repeat      |        0.948 |    0.251 |     3.582 |
| Sector: technology               |        2.567 |    1.168 |     5.643 |
| Sector: telecom_transport        |        2.237 |    1.036 |     4.830 |
| Unknown size                     |        0.614 |    0.229 |     1.651 |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 91-365, 366-730 days vs 0-90 days (unadjusted): rate ratio 0.70 (95% CI 0.44-1.10), about 30% lower; two-sided p = 0.119. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.44 to 1.10, which includes 1. The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 83 in 91-365, 366-730 days and 24 in 0-90 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.648. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates do not have the hypothesised shape: long-term vs response goes the other way.

- response vs immediate: rate ratio 0.77 (95% CI 0.49-1.22), about 23% lower; one-sided p = 0.134 (alternative: lower). Not statistically significant at the 5% level: the data do not reject the null of no decrease (rate ratio at least 1); the 95% CI runs from 0.49 to 1.22, which includes 1. Repeat events: 82 in the response period and 24 in the immediate period.
- long-term vs response: rate ratio 0.93 (95% CI 0.66-1.33), about 7% lower; one-sided p = 0.648 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.66 to 1.33, which includes 1. The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 57 in the long-term period and 82 in the response period.
- long-term vs immediate: rate ratio 0.72 (95% CI 0.44-1.18), about 28% lower; two-sided p = 0.193. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.44 to 1.18, which includes 1. Repeat events: 57 in the long-term period and 24 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.4372
- directional U-shape p-value: 0.6485

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       24 |           96306 |                       9.102 |
| Response period  |       82 |          478143 |                       6.264 |
| Long-term period |       57 |          380777 |                       5.468 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        0.772 |    0.489 |     1.219 | about 23% lower  |         0.267 | less                      | 0.134           |
| long-term vs response  |        0.934 |    0.658 |     1.326 | about 7% lower   |         0.703 | greater                   | 0.648           |
| long-term vs immediate |        0.722 |    0.441 |     1.179 | about 28% lower  |         0.193 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        0.772 |    0.489 |     1.219 |      0.730 | about 23% lower  |           0.134 |
| All covariates                | long-term vs response |        0.934 |    0.658 |     1.326 |      0.667 | about 7% lower   |           0.648 |
| Drop organisation size        | response vs immediate |        0.784 |    0.497 |     1.237 |      0.741 | about 22% lower  |           0.148 |
| Drop organisation size        | long-term vs response |        0.977 |    0.690 |     1.386 |      0.696 | about 2% lower   |           0.551 |
| Drop industry/sector          | response vs immediate |        0.762 |    0.483 |     1.202 |      0.719 | about 24% lower  |           0.121 |
| Drop industry/sector          | long-term vs response |        0.954 |    0.673 |     1.352 |      0.679 | about 5% lower   |           0.605 |
| Drop size and industry/sector | response vs immediate |        0.778 |    0.493 |     1.227 |      0.734 | about 22% lower  |           0.140 |
| Drop size and industry/sector | long-term vs response |        1.041 |    0.737 |     1.471 |      0.734 | about 4% higher  |           0.409 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.4372 |                         0.6485 |
| Drop organisation size        |    0.5440 |                         0.5509 |
| Drop industry/sector          |    0.4389 |                         0.6046 |
| Drop size and industry/sector |    0.5728 |                         0.4093 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.49-1.22). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.66-1.33).

## Delayed 91-180 Day Peak Sensitivity

This sensitivity reruns the elapsed-time model with bands `0-90`, `91-180`, `181-365`, `366-730`, `731-1460`, and `>1460` to test whether the apparent early elevation is concentrated in days 91-180.
A formal delayed peak requires the 91-180 day period to be higher than both 0-90 days and the pooled post-180 period after adjustment.

- adjusted elapsed-time LRT p-value for the 90/180 split: 0.2716
- phase-model LRT p-value versus constant elapsed-time risk: 0.1464
- directional delayed-peak p-value: 0.4559

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |       24 |           96306 |                       9.102 |
| Delayed peak 91-180 days |       22 |           86886 |                       9.248 |
| Post-180 days            |      117 |          772034 |                       5.535 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        1.033 |    0.579 |     1.843 | about 3% higher  |         0.912 | greater                   | 0.456           |
| 91-180 vs post-180 |        1.453 |    0.917 |     2.302 | about 45% higher |         0.111 | greater                   | 0.056           |
| post-180 vs 0-90   |        0.711 |    0.455 |     1.110 | about 29% lower  |         0.134 |                           |                 |

Interpretation:

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.456. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates have the hypothesised shape. The weakest part is 91-180 vs 0-90 (p = 0.456).

- 91-180 vs 0-90: rate ratio 1.03 (95% CI 0.58-1.84), about 3% higher; one-sided p = 0.456 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.58 to 1.84, which includes 1. Repeat events: 22 in 91-180 days and 24 in 0-90 days.
- 91-180 vs post-180: rate ratio 1.45 (95% CI 0.92-2.30), about 45% higher; one-sided p = 0.056 (alternative: higher). Not statistically significant at the 5% level (borderline): the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.92 to 2.30, which includes 1. Repeat events: 22 in 91-180 days and 117 in post-180 days.
- post-180 vs 0-90: rate ratio 0.71 (95% CI 0.46-1.11), about 29% lower; two-sided p = 0.134. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.46 to 1.11, which includes 1. Repeat events: 117 in post-180 days and 24 in 0-90 days.

## Unadjusted Parametric Survival Model Comparison (Descriptive)

Best AIC model: weibull. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| weibull           |        -1591.179 |            2 | 3186.357 | 3196.401 | True        |
| generalized_gamma |        -1591.650 |            3 | 3189.300 | 3204.366 | True        |
| loglogistic       |        -1592.799 |            2 | 3189.598 | 3199.642 | True        |
| exponential       |        -1594.520 |            1 | 3191.040 | 3196.062 | True        |
| lognormal         |        -1598.608 |            2 | 3201.216 | 3211.260 | True        |

Exponential vs Weibull LRT p-value: 0.009735

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
