# Repeat Cyber-Event Timing Analysis

## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.
The primary model includes recorded organisation size. Size is a proxy for differences in visibility, attack exposure and target value, not a direct measure of those factors; residual confounding remains possible. The ordinal size term assumes equal log-rate steps between categories (compared with categorical size and a size-by-elapsed-time interaction under Organisation Size Specification), UNKNOWN has a separate indicator, an organisation stored under several names takes the size its variants support best (`organisation_size_reconciliation.csv`), and current recorded size may differ from size at an incident's date. Unadjusted rates, plots, parametric models and size-dropped variants below are descriptive diagnostics and do not control for this confounding.

## Data Quality Summary

- victim event attributions: 1131
- unique deduplicated events: 910
- victim entities: 961
- entities with more than one event: 86
- post-event spells: 1127
- observed repeat spells: 166
- right-censored spells: 961
- censor date: 2026-10-30
- elapsed-time bands: 0-90, 91-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2003-07-04
- maximum event date: 2026-10-27
- first-of-month event dates: 257
- verified incident-date precision: {'unverified': 656, 'day': 241, 'month': 234}
- tie policy (several incidents of one organisation on one date): collapse
- same-date ties: {'ties (organisation-dates with several incidents)': 4, 'incidents in ties': 8, 'organisations with a tie': 4, 'repeats a policy that separates tied incidents adds': 4, 'ties with a shared verified interval longer than a day (spread by interval_spread)': 0, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 3, 'ties: other_day': 1, 'repeats between tied incidents in this run': 0}
- incidents not ordered by their dates (different stored dates, overlapping verified intervals): {'consecutive pairs with overlapping verified intervals': 0, '... of which stored dates differ by under 90 days': 0}
- covariate coverage: {'size_estimate levels': {'HUGE': 291, 'LARGE': 332, 'MEDIUM': 301, 'SMALL': 111, 'UNKNOWN': 96}, 'size_confidence populated': 1131, 'employee_count populated': 997, 'turnover populated': 0, 'industry populated': 991, 'entity_kind populated': 1131, 'sector_proxy levels': {'education': 80, 'finance': 81, 'government': 201, 'health': 99, 'industrial': 25, 'media': 8, 'other': 401, 'retail': 45, 'technology': 140, 'telecom_transport': 51}, 'prior records populated': 512}
- organisation size across name variants: {'organisations stored under several name variants': 3, 'organisations whose size changed against the representative-only rule': 1, 'organisations whose variants give conflicting known size bands': 0, 'deciding rule': {'known band over UNKNOWN': 2, 'higher size_confidence': 1}}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 55, 'events with an unresolved attribution': 55, 'organisations with an unresolved attribution': 54, 'victim-event attributions analysed': 1131, 'repeats': {'included': {'observed repeats': 166, 'entities with a repeat': 84, 'repeats by elapsed band': {'0-90': 23, '91-180': 22, '181-365': 30, '366-730': 30, '731-1460': 37, '>1460': 24}}, 'excluded': {'observed repeats': 145, 'entities with a repeat': 77, 'repeats by elapsed band': {'0-90': 19, '91-180': 20, '181-365': 24, '366-730': 24, '731-1460': 36, '>1460': 22}}, 'repeats involving an unresolved attribution': 26}}

## Same-Date Ties And Unknown Ordering

Several distinct incidents of one organisation can carry the same stored date; nothing orders them, and a date stored as the 1st of a month may only mean that month. Each tie policy makes one stated assumption about the tied incidents (every tie, with its incident ids and the length of any verified interval its incidents share, is in `entity_date_ties.csv`):

- `collapse` (primary): the tied incidents are one occurrence: one time point, never a repeat between them (their ids and count are kept).
- `zero_gap`: the tied incidents are distinct and one day apart, in id order: each after the first is a repeat after 1 day.
- `interval_spread`: the tied incidents are distinct and evenly spread across the verified interval they share (about interval length / n days apart, the first at the interval's start); a tie with no shared verified interval longer than a day is placed as under zero_gap.

None of these bounds the truth: a tie that is one incident filed twice is represented only by `collapse`; two incidents really weeks apart within a verified month are represented exactly by none; and a tie whose dates are unverified has no interval to place its incidents in, so `interval_spread` places it as `zero_gap` does. Agreement across the policies shows only that a result does not hinge on these assumptions. Every policy is adjusted for organisation size like the primary model.

- Under `collapse`, the adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.407). It rests on 166 observed repeats.
- Under `zero_gap`, the adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.201). It rests on 170 observed repeats.
- Under `interval_spread`, the adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.201). It rests on 170 observed repeats.

| tie_policy      |   observed_repeats |   repeats_from_ties |   post_event_spells |   repeats_0-90 |   repeats_91-180 |   repeats_181-365 |   repeats_366-730 |   repeats_731-1460 |   repeats_>1460 |   adjusted_constant_hazard_p |   adjusted_0_90_91_180_banding_p |   delayed_peak_directional_p |
|:----------------|-------------------:|--------------------:|--------------------:|---------------:|-----------------:|------------------:|------------------:|-------------------:|----------------:|-----------------------------:|---------------------------------:|-----------------------------:|
| collapse        |                166 |                   0 |                1127 |             23 |               22 |                30 |                30 |                 37 |              24 |                       0.4065 |                           0.4065 |                       0.3996 |
| zero_gap        |                170 |                   4 |                1131 |             27 |               22 |                30 |                30 |                 37 |              24 |                       0.201  |                           0.201  |                       0.6177 |
| interval_spread |                170 |                   4 |                1131 |             27 |               22 |                30 |                30 |                 37 |              24 |                       0.201  |                           0.201  |                       0.6177 |


## Organisation Size Specification

The primary model enters size as a numeric score (SMALL=1 to HUGE=4, UNKNOWN mean-imputed, plus an UNKNOWN indicator), which assumes equal log-rate steps between bands. It is compared here with size as a categorical factor (reference: the band with the most repeats; bands with fewer than 3 repeats pooled) and, where every delayed-peak phase has at least 10 repeats at two or more known size bands, with the score's interaction with the delayed-peak phases. The score stays primary; the headline tests are re-run under categorical size. Full table: `size_specification_comparison.csv`.

- Size specification (bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.396 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 1220.6 (categorical) against 1218.5 (score), lower for the score model.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.407 with the size score and p = 0.409 with categorical size: the same verdict at the 5% level.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the directional delayed-peak test gives p = 0.400 with the size score and p = 0.399 with categorical size: the same verdict at the 5% level.
- Size by elapsed-time phase (the size score's slope in 91-180 and post-180 days against 0-90): LRT p = 0.185 on 2 df; at the 5% level the data do not reject a size effect that is the same in every phase.

| banding   | size_spec     |   log_likelihood |   parameters |   aic |   delta_aic_vs_score | nested   |   likelihood_ratio |   df |   p_value |   elapsed_constant_hazard_p |   delayed_peak_directional_p |
|:----------|:--------------|-----------------:|-------------:|------:|---------------------:|:---------|-------------------:|-----:|----------:|----------------------------:|-----------------------------:|
| run       | score         |           -582.2 |           27 |  1218 |               0      |          |            nan     |  nan |  nan      |                      0.4065 |                       0.3996 |
| run       | categorical   |           -581.3 |           29 |  1221 |               2.147  | True     |              1.853 |    2 |    0.3959 |                      0.4088 |                       0.3992 |
| run       | score_x_phase |           -580.5 |           29 |  1219 |               0.6277 | True     |              3.372 |    2 |    0.1852 |                    nan      |                     nan      |


## Primary Piecewise Exponential Test

The adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.407). It rests on 166 observed repeats.

- unadjusted LRT p-value: 0.09889
- adjusted LRT p-value: 0.4065
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=2), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |       23 |       96854.000 |                       8.674 |              4.980 |              13.117 |
| 91-180         |       22 |       87426.000 |                       9.191 |              5.107 |              13.570 |
| 181-365        |       30 |      155213.000 |                       7.060 |              4.371 |               9.575 |
| 366-730        |       30 |      238545.000 |                       4.593 |              2.609 |               6.628 |
| 731-1460       |       37 |      244017.000 |                       5.538 |              3.451 |               7.457 |
| >1460          |       24 |      134951.000 |                       6.496 |              4.117 |               9.313 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        2.108 |    1.601 |     2.777 | about 111% higher repeat-event rate |
| SMALL -> LARGE  |        4.444 |    2.562 |     7.709 | about 344% higher repeat-event rate |
| SMALL -> HUGE   |        9.369 |    4.101 |    21.404 | about 837% higher repeat-event rate |
| MEDIUM -> HUGE  |        4.444 |    2.562 |     7.709 | about 344% higher repeat-event rate |
| LARGE -> HUGE   |        2.108 |    1.601 |     2.777 | about 111% higher repeat-event rate |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| education           |        3.754 |    1.546 |     9.116 | about 275% higher than government |
| technology          |        2.596 |    1.174 |     5.742 | about 160% higher than government |
| finance             |        2.557 |    1.107 |     5.909 | about 156% higher than government |
| industrial          |        2.194 |    0.560 |     8.594 | about 119% higher than government |
| telecom_transport   |        2.127 |    0.981 |     4.612 | about 113% higher than government |
| health              |        1.492 |    0.613 |     3.632 | about 49% higher than government  |
| sparse_or_no_repeat |        0.940 |    0.248 |     3.566 | about 6% lower than government    |
| other               |        0.494 |    0.188 |     1.296 | about 51% lower than government   |

Full adjusted covariate table:

| effect                           |   rate_ratio |   ci_low |                                                                                                                                                                                                      ci_high |
|:---------------------------------|-------------:|---------:|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------:|
| Entity kind: government_body     |        2.446 |    1.184 |                                                                                                                                                                                                        5.055 |
| Entity kind: sparse_or_no_repeat |        0.000 |    0.000 | 16778560490589828158856999776158082198322332556234941856154580898909039539528795916114363657535642057103206275438794574001319613821823303070625656221795203448203839442438077274827473017209779057065984.000 |
| Organisation size                |        2.108 |    1.601 |                                                                                                                                                                                                        2.777 |
| Prior records: 100k+             |        1.038 |    0.675 |                                                                                                                                                                                                        1.595 |
| Prior records: 10k-100k          |        1.222 |    0.790 |                                                                                                                                                                                                        1.889 |
| Prior records: 1k-10k            |        0.992 |    0.565 |                                                                                                                                                                                                        1.743 |
| Prior records: <1k               |        1.146 |    0.601 |                                                                                                                                                                                                        2.186 |
| Sector: education                |        3.754 |    1.546 |                                                                                                                                                                                                        9.116 |
| Sector: finance                  |        2.557 |    1.107 |                                                                                                                                                                                                        5.909 |
| Sector: health                   |        1.492 |    0.613 |                                                                                                                                                                                                        3.632 |
| Sector: industrial               |        2.194 |    0.560 |                                                                                                                                                                                                        8.594 |
| Sector: other                    |        0.494 |    0.188 |                                                                                                                                                                                                        1.296 |
| Sector: sparse_or_no_repeat      |        0.940 |    0.248 |                                                                                                                                                                                                        3.566 |
| Sector: technology               |        2.596 |    1.174 |                                                                                                                                                                                                        5.742 |
| Sector: telecom_transport        |        2.127 |    0.981 |                                                                                                                                                                                                        4.612 |
| Unknown size                     |        0.728 |    0.290 |                                                                                                                                                                                                        1.824 |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 91-180, 181-365 days vs 0-90 days (unadjusted): rate ratio 0.90 (95% CI 0.55-1.47), about 10% lower; two-sided p = 0.682. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.55 to 1.47, which includes 1. The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 52 in 91-180, 181-365 days and 23 in 0-90 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.901. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates do not have the hypothesised shape: long-term vs response goes the other way.

- response vs immediate: rate ratio 0.93 (95% CI 0.57-1.53), about 7% lower; one-sided p = 0.390 (alternative: lower). Not statistically significant at the 5% level: the data do not reject the null of no decrease (rate ratio at least 1); the 95% CI runs from 0.57 to 1.53, which includes 1. Repeat events: 51 in the response period and 23 in the immediate period.
- long-term vs response: rate ratio 0.79 (95% CI 0.56-1.13), about 21% lower; one-sided p = 0.901 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.56 to 1.13, which includes 1. The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 90 in the long-term period and 51 in the response period.
- long-term vs immediate: rate ratio 0.74 (95% CI 0.46-1.18), about 26% lower; two-sided p = 0.206. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.46 to 1.18, which includes 1. Repeat events: 90 in the long-term period and 23 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.2896
- directional U-shape p-value: 0.9014

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       23 |           96854 |                       8.674 |
| Response period  |       51 |          242639 |                       7.677 |
| Long-term period |       90 |          617513 |                       5.323 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        0.932 |    0.569 |     1.526 | about 7% lower   |         0.780 | less                      | 0.390           |
| long-term vs response  |        0.794 |    0.559 |     1.128 | about 21% lower  |         0.197 | greater                   | 0.901           |
| long-term vs immediate |        0.740 |    0.464 |     1.180 | about 26% lower  |         0.206 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        0.932 |    0.569 |     1.526 |      0.957 | about 7% lower   |           0.390 |
| All covariates                | long-term vs response |        0.794 |    0.559 |     1.128 |      0.569 | about 21% lower  |           0.901 |
| Drop organisation size        | response vs immediate |        0.949 |    0.580 |     1.554 |      0.974 | about 5% lower   |           0.417 |
| Drop organisation size        | long-term vs response |        0.807 |    0.568 |     1.146 |      0.578 | about 19% lower  |           0.885 |
| Drop industry/sector          | response vs immediate |        0.922 |    0.563 |     1.509 |      0.946 | about 8% lower   |           0.373 |
| Drop industry/sector          | long-term vs response |        0.798 |    0.563 |     1.133 |      0.570 | about 20% lower  |           0.896 |
| Drop size and industry/sector | response vs immediate |        0.944 |    0.577 |     1.545 |      0.969 | about 6% lower   |           0.410 |
| Drop size and industry/sector | long-term vs response |        0.836 |    0.590 |     1.184 |      0.594 | about 16% lower  |           0.844 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.2896 |                         0.9014 |
| Drop organisation size        |    0.3571 |                         0.8850 |
| Drop industry/sector          |    0.2898 |                         0.8963 |
| Drop size and industry/sector |    0.4599 |                         0.8437 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.57-1.53). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.56-1.13).

## Delayed 91-180 Day Peak Test

Definition used here: the hypothesised peak is 91-180 days after the prior event. A formal peak requires 91-180 days to be higher than both 0-90 days and the pooled post-180 period.
The adjusted peak model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main model.

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.400. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates have the hypothesised shape. The weakest part is 91-180 vs 0-90 (p = 0.400).

- 91-180 vs 0-90: rate ratio 1.08 (95% CI 0.60-1.94), about 8% higher; one-sided p = 0.400 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.60 to 1.94, which includes 1. Repeat events: 22 in 91-180 days and 23 in 0-90 days.
- 91-180 vs post-180: rate ratio 1.41 (95% CI 0.89-2.23), about 41% higher; one-sided p = 0.071 (alternative: higher). Not statistically significant at the 5% level (borderline): the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.89 to 2.23, which includes 1. Repeat events: 22 in 91-180 days and 119 in post-180 days.
- post-180 vs 0-90: rate ratio 0.77 (95% CI 0.49-1.20), about 23% lower; two-sided p = 0.247. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.49 to 1.20, which includes 1. Repeat events: 119 in post-180 days and 23 in 0-90 days.

- phase-model LRT p-value versus constant elapsed-time risk: 0.2417
- directional delayed-peak p-value: 0.3996

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |       23 |           96854 |                       8.674 |
| Delayed peak 91-180 days |       22 |           87426 |                       9.191 |
| Post-180 days            |      119 |          772726 |                       5.625 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        1.079 |    0.601 |     1.936 | about 8% higher  |         0.799 | greater                   | 0.400           |
| 91-180 vs post-180 |        1.410 |    0.891 |     2.232 | about 41% higher |         0.143 | greater                   | 0.071           |
| post-180 vs 0-90   |        0.765 |    0.487 |     1.204 | about 23% lower  |         0.247 |                           |                 |

## Unadjusted Parametric Survival Model Comparison (Descriptive)

Best AIC model: weibull. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| weibull           |        -1601.229 |            2 | 3206.457 | 3216.512 | True        |
| exponential       |        -1603.490 |            1 | 3208.980 | 3214.007 | True        |
| loglogistic       |        -1602.661 |            2 | 3209.322 | 3219.377 | True        |
| generalized_gamma |        -1601.682 |            3 | 3209.364 | 3224.446 | True        |
| lognormal         |        -1608.220 |            2 | 3220.440 | 3230.494 | True        |

Exponential vs Weibull LRT p-value: 0.03345

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
