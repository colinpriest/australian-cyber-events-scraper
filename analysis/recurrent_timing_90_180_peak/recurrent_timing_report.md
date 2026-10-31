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
- elapsed-time bands: 0-90, 91-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2003-07-04
- maximum event date: 2026-10-27
- first-of-month event dates: 205
- verified incident-date precision: {'unverified': 559, 'day': 226, 'month': 190}
- tie policy (several incidents of one organisation on one date): collapse
- same-date ties: {'ties (organisation-dates with several incidents)': 0, 'incidents in ties': 0, 'organisations with a tie': 0, 'repeats a policy that separates tied incidents adds': 0, 'ties with a shared verified interval longer than a day (spread by interval_spread)': 0, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 0, 'ties: other_day': 0, 'repeats between tied incidents in this run': 0}
- incidents not ordered by their dates (different stored dates, overlapping verified intervals): {'consecutive pairs with overlapping verified intervals': 0, '... of which stored dates differ by under 90 days': 0}
- covariate coverage: {'size_estimate levels': {'HUGE': 233, 'LARGE': 287, 'MEDIUM': 274, 'SMALL': 103, 'UNKNOWN': 78}, 'size_confidence populated': 957, 'employee_count populated': 867, 'turnover populated': 0, 'industry populated': 868, 'entity_kind populated': 975, 'sector_proxy levels': {'education': 75, 'finance': 59, 'government': 167, 'health': 87, 'industrial': 25, 'media': 8, 'other': 344, 'retail': 43, 'technology': 126, 'telecom_transport': 41}, 'prior records populated': 411}
- organisation size across name variants: {'organisations stored under several name variants': 6, 'organisations whose size changed against the representative-only rule': 1, 'organisations whose variants give conflicting known size bands': 0, 'deciding rule': {'known band over UNKNOWN': 3, 'higher size_confidence': 2, 'representative variant': 1}}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 36, 'events with an unresolved attribution': 36, 'organisations with an unresolved attribution': 35, 'victim-event attributions analysed': 975, 'repeats': {'included': {'observed repeats': 122, 'entities with a repeat': 65, 'repeats by elapsed band': {'0-90': 12, '91-180': 16, '181-365': 29, '366-730': 18, '731-1460': 22, '>1460': 25}}, 'excluded': {'observed repeats': 108, 'entities with a repeat': 61, 'repeats by elapsed band': {'0-90': 9, '91-180': 14, '181-365': 24, '366-730': 14, '731-1460': 23, '>1460': 24}}, 'repeats involving an unresolved attribution': 18}}

## Same-Date Ties And Unknown Ordering

A same-date tie is far more likely one incident that deduplication has not joined than two incidents on one day. Every such pair, and every pair whose possible periods overlap, is an exception listed with the reason it is still two events in `dashboard/close_pairs.html`; the merging steps are fixed when one is a missed duplicate. Until a tie is resolved, each tie policy makes one stated assumption about it (every tie, with its incident ids and the length of any verified interval its incidents share, is in `entity_date_ties.csv`):

- `collapse` (primary): the tied incidents are one occurrence: one time point, never a repeat between them (their ids and count are kept).
- `zero_gap`: the tied incidents are distinct and one day apart, in id order: each after the first is a repeat after 1 day.
- `interval_spread`: the tied incidents are distinct and evenly spread across the verified interval they share (about interval length / n days apart, the first at the interval's start); a tie with no shared verified interval longer than a day is placed as under zero_gap.

None of these bounds the truth: a tie that is one incident filed twice is represented only by `collapse`; two incidents really weeks apart within a verified month are represented exactly by none; and a tie whose dates are unverified has no interval to place its incidents in, so `interval_spread` places it as `zero_gap` does. Agreement across the policies shows only that a result does not hinge on these assumptions. Every policy is adjusted for organisation size like the primary model.

- Under `collapse`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.031). It rests on 122 observed repeats.
- Under `zero_gap`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.031). It rests on 122 observed repeats.
- Under `interval_spread`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.031). It rests on 122 observed repeats.

| tie_policy      |   observed_repeats |   repeats_from_ties |   post_event_spells |   repeats_0-90 |   repeats_91-180 |   repeats_181-365 |   repeats_366-730 |   repeats_731-1460 |   repeats_>1460 |   adjusted_constant_hazard_p |   adjusted_0_90_91_180_banding_p |   delayed_peak_directional_p |
|:----------------|-------------------:|--------------------:|--------------------:|---------------:|-----------------:|------------------:|------------------:|-------------------:|----------------:|-----------------------------:|---------------------------------:|-----------------------------:|
| collapse        |                122 |                   0 |                 975 |             12 |               16 |                29 |                18 |                 22 |              25 |                      0.03108 |                          0.03108 |                       0.1488 |
| zero_gap        |                122 |                   0 |                 975 |             12 |               16 |                29 |                18 |                 22 |              25 |                      0.03108 |                          0.03108 |                       0.1488 |
| interval_spread |                122 |                   0 |                 975 |             12 |               16 |                29 |                18 |                 22 |              25 |                      0.03108 |                          0.03108 |                       0.1488 |


## Organisation Size Specification

The primary model enters size as a numeric score (SMALL=1 to HUGE=4, UNKNOWN mean-imputed, plus an UNKNOWN indicator), which assumes equal log-rate steps between bands. It is compared here with size as a categorical factor (reference: the band with the most repeats; bands with fewer than 3 repeats pooled) and, where every delayed-peak phase has at least 10 repeats at two or more known size bands, with the score's interaction with the delayed-peak phases. The score stays primary; the headline tests are re-run under categorical size. Full table: `size_specification_comparison.csv`.

- Size specification (bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.037 on 2 df; at the 5% level the data reject equal log-rate steps between size bands; AIC 901.0 (categorical) against 903.6 (score), lower for the categorical model.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.031 with the size score and p = 0.035 with categorical size: the same verdict at the 5% level.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the directional delayed-peak test gives p = 0.149 with the size score and p = 0.151 with categorical size: the same verdict at the 5% level.
- Size by elapsed-time phase (the size score's slope in 91-180 and post-180 days against 0-90): LRT p = 0.105 on 2 df; at the 5% level the data do not reject a size effect that is the same in every phase.

| banding   | size_spec     |   log_likelihood |   parameters |   aic |   delta_aic_vs_score | nested   |   likelihood_ratio |   df |   p_value |   elapsed_constant_hazard_p |   delayed_peak_directional_p |
|:----------|:--------------|-----------------:|-------------:|------:|---------------------:|:---------|-------------------:|-----:|----------:|----------------------------:|-----------------------------:|
| run       | score         |           -424.8 |           27 | 903.6 |               0      |          |            nan     |  nan | nan       |                     0.03108 |                       0.1488 |
| run       | categorical   |           -421.5 |           29 | 901   |              -2.598  | True     |              6.598 |    2 |   0.03693 |                     0.03461 |                       0.1509 |
| run       | score_x_phase |           -422.5 |           29 | 903.1 |              -0.5116 | True     |              4.512 |    2 |   0.1048  |                   nan       |                     nan      |


## Primary Piecewise Exponential Test

The adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.031). It rests on 122 observed repeats.

- unadjusted LRT p-value: 0.005051
- adjusted LRT p-value: 0.03108
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=2), entity_kind_group (reference=organisation), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |       12 |       84277.000 |                       5.201 |              2.234 |               8.604 |
| 91-180         |       16 |       75878.000 |                       7.702 |              3.420 |              12.399 |
| 181-365        |       29 |      133251.000 |                       7.949 |              4.921 |              11.260 |
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
| SMALL -> LARGE  |        6.264 |    3.123 |    12.565 | about 526% higher repeat-event rate  |
| SMALL -> HUGE   |       15.678 |    5.519 |    44.537 | about 1468% higher repeat-event rate |
| MEDIUM -> HUGE  |        6.264 |    3.123 |    12.565 | about 526% higher repeat-event rate  |
| LARGE -> HUGE   |        2.503 |    1.767 |     3.545 | about 150% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| education           |        7.151 |    1.619 |    31.588 | about 615% higher than government |
| industrial          |        4.943 |    0.832 |    29.376 | about 394% higher than government |
| telecom_transport   |        4.807 |    1.086 |    21.278 | about 381% higher than government |
| technology          |        4.480 |    1.135 |    17.679 | about 348% higher than government |
| finance             |        3.507 |    0.803 |    15.313 | about 251% higher than government |
| health              |        2.139 |    0.398 |    11.484 | about 114% higher than government |
| sparse_or_no_repeat |        0.744 |    0.068 |     8.126 | about 26% lower than government   |
| other               |        0.623 |    0.123 |     3.145 | about 38% lower than government   |

Full adjusted covariate table:

| effect                        |   rate_ratio |   ci_low |   ci_high |
|:------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body  |        4.239 |    1.098 |    16.360 |
| Entity kind: other_or_unknown |        1.487 |    0.297 |     7.450 |
| Organisation size             |        2.503 |    1.767 |     3.545 |
| Prior records: 100k+          |        1.248 |    0.752 |     2.073 |
| Prior records: 10k-100k       |        1.589 |    0.948 |     2.664 |
| Prior records: 1k-10k         |        1.164 |    0.607 |     2.233 |
| Prior records: <1k            |        1.180 |    0.568 |     2.452 |
| Sector: education             |        7.151 |    1.619 |    31.588 |
| Sector: finance               |        3.507 |    0.803 |    15.313 |
| Sector: health                |        2.139 |    0.398 |    11.484 |
| Sector: industrial            |        4.943 |    0.832 |    29.376 |
| Sector: other                 |        0.623 |    0.123 |     3.145 |
| Sector: sparse_or_no_repeat   |        0.744 |    0.068 |     8.126 |
| Sector: technology            |        4.480 |    1.135 |    17.679 |
| Sector: telecom_transport     |        4.807 |    1.086 |    21.278 |
| Unknown size                  |        1.053 |    0.291 |     3.810 |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 91-180, 181-365 days vs 0-90 days (unadjusted): rate ratio 1.51 (95% CI 0.80-2.86), about 51% higher; two-sided p = 0.204. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.80 to 2.86, which includes 1. Repeat events: 45 in 91-180, 181-365 days and 12 in 0-90 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.994. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates do not have the hypothesised shape: response vs immediate, long-term vs response go the other way.

- response vs immediate: rate ratio 1.55 (95% CI 0.82-2.93), about 55% higher; one-sided p = 0.910 (alternative: lower). Not statistically significant at the 5% level: the data do not reject the null of no decrease (rate ratio at least 1); the 95% CI runs from 0.82 to 2.93, which includes 1. The estimate is in the opposite direction to the hypothesis (higher, not lower). Repeat events: 44 in the response period and 12 in the immediate period.
- long-term vs response: rate ratio 0.60 (95% CI 0.40-0.89), about 40% lower; one-sided p = 0.994 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.40 to 0.89. The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 64 in the long-term period and 44 in the response period.
- long-term vs immediate: rate ratio 0.93 (95% CI 0.50-1.74), about 7% lower; two-sided p = 0.822. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.50 to 1.74, which includes 1. Repeat events: 64 in the long-term period and 12 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.04255
- directional U-shape p-value: 0.9942

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       12 |           84277 |                       5.201 |
| Response period  |       44 |          209129 |                       7.685 |
| Long-term period |       64 |          537542 |                       4.349 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        1.548 |    0.817 |     2.934 | about 55% higher |         0.180 | less                      | 0.910           |
| long-term vs response  |        0.601 |    0.405 |     0.893 | about 40% lower  |         0.012 | greater                   | 0.994           |
| long-term vs immediate |        0.931 |    0.497 |     1.743 | about 7% lower   |         0.822 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        1.548 |    0.817 |     2.934 |      2.117 | about 55% higher |           0.910 |
| All covariates                | long-term vs response |        0.601 |    0.405 |     0.893 |      0.488 | about 40% lower  |           0.994 |
| Drop organisation size        | response vs immediate |        1.585 |    0.836 |     3.003 |      2.167 | about 58% higher |           0.921 |
| Drop organisation size        | long-term vs response |        0.631 |    0.425 |     0.936 |      0.510 | about 37% lower  |           0.989 |
| Drop industry/sector          | response vs immediate |        1.526 |    0.806 |     2.891 |      2.085 | about 53% higher |           0.903 |
| Drop industry/sector          | long-term vs response |        0.580 |    0.391 |     0.859 |      0.468 | about 42% lower  |           0.997 |
| Drop size and industry/sector | response vs immediate |        1.576 |    0.832 |     2.985 |      2.153 | about 58% higher |           0.919 |
| Drop size and industry/sector | long-term vs response |        0.629 |    0.425 |     0.930 |      0.505 | about 37% lower  |           0.990 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.0426 |                         0.9942 |
| Drop organisation size        |    0.0656 |                         0.9890 |
| Drop industry/sector          |    0.0277 |                         0.9967 |
| Drop size and industry/sector |    0.0630 |                         0.9899 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.82-2.93). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.40-0.89).

## Delayed 91-180 Day Peak Test

Definition used here: the hypothesised peak is 91-180 days after the prior event. A formal peak requires 91-180 days to be higher than both 0-90 days and the pooled post-180 period.
The adjusted peak model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main model.

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.149. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates have the hypothesised shape. The weakest part is 91-180 vs 0-90 (p = 0.149).

- 91-180 vs 0-90: rate ratio 1.49 (95% CI 0.70-3.15), about 49% higher; one-sided p = 0.149 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.70 to 3.15, which includes 1. Repeat events: 16 in 91-180 days and 12 in 0-90 days.
- 91-180 vs post-180: rate ratio 1.37 (95% CI 0.80-2.35), about 37% higher; one-sided p = 0.125 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.80 to 2.35, which includes 1. Repeat events: 16 in 91-180 days and 92 in post-180 days.
- post-180 vs 0-90: rate ratio 1.09 (95% CI 0.59-2.00), about 9% higher; two-sided p = 0.792. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.59 to 2.00, which includes 1. Repeat events: 92 in post-180 days and 12 in 0-90 days.

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
