# Repeat Cyber-Event Timing Analysis

## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.
The primary model includes recorded organisation size. Size is a proxy for differences in visibility, attack exposure and target value, not a direct measure of those factors; residual confounding remains possible. The ordinal size term assumes equal log-rate steps between categories (compared with categorical size and a size-by-elapsed-time interaction under Organisation Size Specification), UNKNOWN has a separate indicator, an organisation stored under several names takes the size its variants support best (`organisation_size_reconciliation.csv`), and current recorded size may differ from size at an incident's date. Unadjusted rates, plots, parametric models and size-dropped variants below are descriptive diagnostics and do not control for this confounding.

## Data Quality Summary

- victim event attributions: 975
- unique deduplicated events: 889
- victim entities: 853
- entities with more than one event: 65
- post-event spells: 975
- observed repeat spells: 122
- right-censored spells: 853
- censor date: 2026-10-30
- elapsed-time bands: 0-90, 91-365, 366-730, 731-1460, >1460
- minimum event date: 2003-07-04
- maximum event date: 2026-10-27
- first-of-month event dates: 205
- verified incident-date precision: {'unverified': 559, 'day': 226, 'month': 190}
- tie policy (several incidents of one organisation on one date): collapse
- same-date ties: {'ties (organisation-dates with several incidents)': 0, 'incidents in ties': 0, 'organisations with a tie': 0, 'repeats a policy that separates tied incidents adds': 0, 'ties with a shared verified interval longer than a day (spread by interval_spread)': 0, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 0, 'ties: other_day': 0, 'repeats between tied incidents in this run': 0}
- incidents not ordered by their dates (different stored dates, overlapping verified intervals): {'consecutive pairs with overlapping verified intervals': 0, '... of which stored dates differ by under 90 days': 0}
- covariate coverage: {'size_estimate levels': {'HUGE': 233, 'LARGE': 287, 'MEDIUM': 274, 'SMALL': 103, 'UNKNOWN': 78}, 'size_confidence populated': 957, 'employee_count populated': 867, 'turnover populated': 0, 'industry populated': 868, 'entity_kind populated': 975, 'sector_proxy levels': {'education': 75, 'finance': 59, 'government': 167, 'health': 87, 'industrial': 25, 'media': 8, 'other': 344, 'retail': 43, 'technology': 126, 'telecom_transport': 41}, 'prior records populated': 411}
- organisation size across name variants: {'organisations stored under several name variants': 6, 'organisations whose size changed against the representative-only rule': 1, 'organisations whose variants give conflicting known size bands': 0, 'deciding rule': {'known band over UNKNOWN': 3, 'higher size_confidence': 2, 'representative variant': 1}}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 36, 'events with an unresolved attribution': 36, 'organisations with an unresolved attribution': 35, 'victim-event attributions analysed': 975, 'repeats': {'included': {'observed repeats': 122, 'entities with a repeat': 65, 'repeats by elapsed band': {'0-90': 12, '91-365': 45, '366-730': 18, '731-1460': 22, '>1460': 25}}, 'excluded': {'observed repeats': 108, 'entities with a repeat': 61, 'repeats by elapsed band': {'0-90': 9, '91-365': 38, '366-730': 14, '731-1460': 23, '>1460': 24}}, 'repeats involving an unresolved attribution': 18}}

## Same-Date Ties And Unknown Ordering

A same-date tie is far more likely one incident that deduplication has not joined than two incidents on one day. Every such pair, and every pair whose possible periods overlap, is an exception listed with the reason it is still two events in `dashboard/close_pairs.html`; the merging steps are fixed when one is a missed duplicate. Until a tie is resolved, each tie policy makes one stated assumption about it (every tie, with its incident ids and the length of any verified interval its incidents share, is in `entity_date_ties.csv`):

- `collapse` (primary): the tied incidents are one occurrence: one time point, never a repeat between them (their ids and count are kept).
- `zero_gap`: the tied incidents are distinct and one day apart, in id order: each after the first is a repeat after 1 day.
- `interval_spread`: the tied incidents are distinct and evenly spread across the verified interval they share (about interval length / n days apart, the first at the interval's start); a tie with no shared verified interval longer than a day is placed as under zero_gap.

None of these bounds the truth: a tie that is one incident filed twice is represented only by `collapse`; two incidents really weeks apart within a verified month are represented exactly by none; and a tie whose dates are unverified has no interval to place its incidents in, so `interval_spread` places it as `zero_gap` does. Agreement across the policies shows only that a result does not hinge on these assumptions. Every policy is adjusted for organisation size like the primary model.

- Under `collapse`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.016). It rests on 122 observed repeats.
- Under `zero_gap`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.016). It rests on 122 observed repeats.
- Under `interval_spread`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.016). It rests on 122 observed repeats.

| tie_policy      |   observed_repeats |   repeats_from_ties |   post_event_spells |   repeats_0-90 |   repeats_91-365 |   repeats_366-730 |   repeats_731-1460 |   repeats_>1460 |   adjusted_constant_hazard_p |   adjusted_0_90_91_180_banding_p |   delayed_peak_directional_p |
|:----------------|-------------------:|--------------------:|--------------------:|---------------:|-----------------:|------------------:|-------------------:|----------------:|-----------------------------:|---------------------------------:|-----------------------------:|
| collapse        |                122 |                   0 |                 975 |             12 |               45 |                18 |                 22 |              25 |                      0.01564 |                          0.03108 |                       0.1488 |
| zero_gap        |                122 |                   0 |                 975 |             12 |               45 |                18 |                 22 |              25 |                      0.01564 |                          0.03108 |                       0.1488 |
| interval_spread |                122 |                   0 |                 975 |             12 |               45 |                18 |                 22 |              25 |                      0.01564 |                          0.03108 |                       0.1488 |


## Organisation Size Specification

The primary model enters size as a numeric score (SMALL=1 to HUGE=4, UNKNOWN mean-imputed, plus an UNKNOWN indicator), which assumes equal log-rate steps between bands. It is compared here with size as a categorical factor (reference: the band with the most repeats; bands with fewer than 3 repeats pooled) and, where every delayed-peak phase has at least 10 repeats at two or more known size bands, with the score's interaction with the delayed-peak phases. The score stays primary; the headline tests are re-run under categorical size. Full table: `size_specification_comparison.csv`.

- Size specification (bands 0-90, 91-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.037 on 2 df; at the 5% level the data reject equal log-rate steps between size bands; AIC 845.4 (categorical) against 848.0 (score), lower for the categorical model.
- With bands 0-90, 91-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.016 with the size score and p = 0.018 with categorical size: the same verdict at the 5% level.
- Size specification (bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.037 on 2 df; at the 5% level the data reject equal log-rate steps between size bands; AIC 901.0 (categorical) against 903.6 (score), lower for the categorical model.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.031 with the size score and p = 0.035 with categorical size: the same verdict at the 5% level.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the directional delayed-peak test gives p = 0.149 with the size score and p = 0.151 with categorical size: the same verdict at the 5% level.
- Size by elapsed-time phase (the size score's slope in 91-180 and post-180 days against 0-90): LRT p = 0.105 on 2 df; at the 5% level the data do not reject a size effect that is the same in every phase.

| banding       | size_spec     |   log_likelihood |   parameters |   aic |   delta_aic_vs_score | nested   |   likelihood_ratio |   df |   p_value |   elapsed_constant_hazard_p |   delayed_peak_directional_p |
|:--------------|:--------------|-----------------:|-------------:|------:|---------------------:|:---------|-------------------:|-----:|----------:|----------------------------:|-----------------------------:|
| run           | score         |           -398   |           26 | 848   |               0      |          |            nan     |  nan | nan       |                     0.01564 |                     nan      |
| run           | categorical   |           -394.7 |           28 | 845.4 |              -2.598  | True     |              6.598 |    2 |   0.03692 |                     0.01758 |                     nan      |
| 0-90 / 91-180 | score         |           -424.8 |           27 | 903.6 |               0      |          |            nan     |  nan | nan       |                     0.03108 |                       0.1488 |
| 0-90 / 91-180 | categorical   |           -421.5 |           29 | 901   |              -2.598  | True     |              6.598 |    2 |   0.03693 |                     0.03461 |                       0.1509 |
| 0-90 / 91-180 | score_x_phase |           -422.5 |           29 | 903.1 |              -0.5116 | True     |              4.512 |    2 |   0.1048  |                   nan       |                     nan      |


## Primary Piecewise Exponential Test

The adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.016). It rests on 122 observed repeats.

- unadjusted LRT p-value: 0.002196
- adjusted LRT p-value: 0.01564
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=2), entity_kind_group (reference=organisation), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |       12 |       84277.000 |                       5.201 |              2.234 |               8.604 |
| 91-365         |       45 |      209129.000 |                       7.859 |              5.362 |              10.697 |
| 366-730        |       18 |      203914.000 |                       3.224 |              1.626 |               4.888 |
| 731-1460       |       22 |      211051.000 |                       3.807 |              2.352 |               5.282 |
| >1460          |       25 |      122577.000 |                       7.449 |              4.514 |              10.644 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                       |
|:----------------|-------------:|---------:|----------:|:-------------------------------------|
| SMALL -> MEDIUM |        2.503 |    1.767 |     3.545 | about 150% higher repeat-event rate  |
| SMALL -> LARGE  |        6.265 |    3.123 |    12.565 | about 526% higher repeat-event rate  |
| SMALL -> HUGE   |       15.680 |    5.520 |    44.541 | about 1468% higher repeat-event rate |
| MEDIUM -> HUGE  |        6.265 |    3.123 |    12.565 | about 526% higher repeat-event rate  |
| LARGE -> HUGE   |        2.503 |    1.767 |     3.545 | about 150% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| education           |        7.133 |    1.614 |    31.515 | about 613% higher than government |
| industrial          |        4.940 |    0.831 |    29.360 | about 394% higher than government |
| telecom_transport   |        4.808 |    1.086 |    21.289 | about 381% higher than government |
| technology          |        4.475 |    1.133 |    17.665 | about 347% higher than government |
| finance             |        3.506 |    0.803 |    15.317 | about 251% higher than government |
| health              |        2.138 |    0.398 |    11.481 | about 114% higher than government |
| sparse_or_no_repeat |        0.743 |    0.068 |     8.125 | about 26% lower than government   |
| other               |        0.622 |    0.123 |     3.143 | about 38% lower than government   |

Full adjusted covariate table:

| effect                        |   rate_ratio |   ci_low |   ci_high |
|:------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body  |        4.237 |    1.097 |    16.362 |
| Entity kind: other_or_unknown |        1.484 |    0.296 |     7.433 |
| Organisation size             |        2.503 |    1.767 |     3.545 |
| Prior records: 100k+          |        1.248 |    0.752 |     2.073 |
| Prior records: 10k-100k       |        1.590 |    0.949 |     2.665 |
| Prior records: 1k-10k         |        1.164 |    0.607 |     2.232 |
| Prior records: <1k            |        1.180 |    0.568 |     2.451 |
| Sector: education             |        7.133 |    1.614 |    31.515 |
| Sector: finance               |        3.506 |    0.803 |    15.317 |
| Sector: health                |        2.138 |    0.398 |    11.481 |
| Sector: industrial            |        4.940 |    0.831 |    29.360 |
| Sector: other                 |        0.622 |    0.123 |     3.143 |
| Sector: sparse_or_no_repeat   |        0.743 |    0.068 |     8.125 |
| Sector: technology            |        4.475 |    1.133 |    17.665 |
| Sector: telecom_transport     |        4.808 |    1.086 |    21.289 |
| Unknown size                  |        1.054 |    0.291 |     3.811 |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 91-365, 366-730 days vs 0-90 days (unadjusted): rate ratio 1.07 (95% CI 0.58-1.99), about 7% higher; two-sided p = 0.827. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.58 to 1.99, which includes 1. Repeat events: 63 in 91-365, 366-730 days and 12 in 0-90 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.707. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates do not have the hypothesised shape: response vs immediate, long-term vs response go the other way.

- response vs immediate: rate ratio 1.19 (95% CI 0.64-2.21), about 19% higher; one-sided p = 0.707 (alternative: lower). Not statistically significant at the 5% level: the data do not reject the null of no decrease (rate ratio at least 1); the 95% CI runs from 0.64 to 2.21, which includes 1. The estimate is in the opposite direction to the hypothesis (higher, not lower). Repeat events: 62 in the response period and 12 in the immediate period.
- long-term vs response: rate ratio 0.90 (95% CI 0.60-1.33), about 10% lower; one-sided p = 0.706 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.60 to 1.33, which includes 1. The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 46 in the long-term period and 62 in the response period.
- long-term vs immediate: rate ratio 1.07 (95% CI 0.56-2.04), about 7% higher; two-sided p = 0.848. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.56 to 2.04, which includes 1. Repeat events: 46 in the long-term period and 12 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.7868
- directional U-shape p-value: 0.7073

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       12 |           84277 |                       5.201 |
| Response period  |       62 |          413043 |                       5.483 |
| Long-term period |       46 |          333628 |                       5.036 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        1.189 |    0.639 |     2.211 | about 19% higher |         0.585 | less                      | 0.707           |
| long-term vs response  |        0.897 |    0.604 |     1.331 | about 10% lower  |         0.589 | greater                   | 0.706           |
| long-term vs immediate |        1.066 |    0.556 |     2.045 | about 7% higher  |         0.848 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        1.189 |    0.639 |     2.211 |      1.572 | about 19% higher |           0.707 |
| All covariates                | long-term vs response |        0.897 |    0.604 |     1.331 |      0.727 | about 10% lower  |           0.706 |
| Drop organisation size        | response vs immediate |        1.210 |    0.651 |     2.251 |      1.600 | about 21% higher |           0.727 |
| Drop organisation size        | long-term vs response |        0.981 |    0.661 |     1.455 |      0.794 | about 2% lower   |           0.539 |
| Drop industry/sector          | response vs immediate |        1.152 |    0.620 |     2.142 |      1.522 | about 15% higher |           0.673 |
| Drop industry/sector          | long-term vs response |        0.873 |    0.589 |     1.293 |      0.703 | about 13% lower  |           0.751 |
| Drop size and industry/sector | response vs immediate |        1.187 |    0.639 |     2.206 |      1.567 | about 19% higher |           0.706 |
| Drop size and industry/sector | long-term vs response |        0.994 |    0.673 |     1.470 |      0.797 | about 1% lower   |           0.511 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.7868 |                         0.7073 |
| Drop organisation size        |    0.8260 |                         0.7266 |
| Drop industry/sector          |    0.7610 |                         0.7514 |
| Drop size and industry/sector |    0.8518 |                         0.7064 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.64-2.21). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.60-1.33).

## Delayed 91-180 Day Peak Sensitivity

This sensitivity reruns the elapsed-time model with bands `0-90`, `91-180`, `181-365`, `366-730`, `731-1460`, and `>1460` to test whether the apparent early elevation is concentrated in days 91-180.
A formal delayed peak requires the 91-180 day period to be higher than both 0-90 days and the pooled post-180 period after adjustment.

- adjusted elapsed-time LRT p-value for the 90/180 split: 0.03108
- phase-model LRT p-value versus constant elapsed-time risk: 0.4921
- directional delayed-peak p-value: 0.1488

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |       12 |           84277 |                       5.201 |
| Delayed peak 91-180 days |       16 |           75878 |                       7.702 |
| Post-180 days            |       92 |          670793 |                       5.009 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        1.489 |    0.704 |     3.148 | about 49% higher |         0.298 | greater                   | 0.149           |
| 91-180 vs post-180 |        1.371 |    0.801 |     2.349 | about 37% higher |         0.250 | greater                   | 0.125           |
| post-180 vs 0-90   |        1.085 |    0.590 |     1.997 | about 9% higher  |         0.792 |                           |                 |

Interpretation:

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.149. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates have the hypothesised shape. The weakest part is 91-180 vs 0-90 (p = 0.149).

- 91-180 vs 0-90: rate ratio 1.49 (95% CI 0.70-3.15), about 49% higher; one-sided p = 0.149 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.70 to 3.15, which includes 1. Repeat events: 16 in 91-180 days and 12 in 0-90 days.
- 91-180 vs post-180: rate ratio 1.37 (95% CI 0.80-2.35), about 37% higher; one-sided p = 0.125 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.80 to 2.35, which includes 1. Repeat events: 16 in 91-180 days and 92 in post-180 days.
- post-180 vs 0-90: rate ratio 1.09 (95% CI 0.59-2.00), about 9% higher; two-sided p = 0.792. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.59 to 2.00, which includes 1. Repeat events: 92 in post-180 days and 12 in 0-90 days.

## Unadjusted Parametric Survival Model Comparison (Descriptive)

Best AIC model: exponential. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| exponential       |        -1198.809 |            1 | 2399.618 | 2404.500 | True        |
| weibull           |        -1198.685 |            2 | 2401.370 | 2411.135 | True        |
| generalized_gamma |        -1198.807 |            3 | 2403.614 | 2418.261 | True        |
| loglogistic       |        -1199.940 |            2 | 2403.881 | 2413.646 | True        |
| lognormal         |        -1203.217 |            2 | 2410.434 | 2420.199 | True        |

Exponential vs Weibull LRT p-value: 0.6189

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
