# Repeat Cyber-Event Timing Analysis

## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.
The primary model includes recorded organisation size. Size is a proxy for differences in visibility, attack exposure and target value, not a direct measure of those factors; residual confounding remains possible. The ordinal size term assumes equal log-rate steps between categories (compared with categorical size and a size-by-elapsed-time interaction under Organisation Size Specification), UNKNOWN has a separate indicator, an organisation stored under several names takes the size its variants support best (`organisation_size_reconciliation.csv`), and current recorded size may differ from size at an incident's date. Unadjusted rates, plots, parametric models and size-dropped variants below are descriptive diagnostics and do not control for this confounding.

## Data Quality Summary

- victim event attributions: 1007
- unique deduplicated events: 902
- victim entities: 854
- entities with more than one event: 87
- post-event spells: 995
- observed repeat spells: 141
- right-censored spells: 854
- censor date: 2026-10-30
- elapsed-time bands: 0-90, 91-365, 366-730, 731-1460, >1460
- minimum event date: 2003-07-04
- maximum event date: 2026-10-27
- first-of-month event dates: 217
- verified incident-date precision: {'unverified': 605, 'day': 212, 'month': 190}
- tie policy (several incidents of one organisation on one date): collapse
- same-date ties: {'ties (organisation-dates with several incidents)': 12, 'incidents in ties': 24, 'organisations with a tie': 12, 'repeats a policy that separates tied incidents adds': 12, 'ties with a shared verified interval longer than a day (spread by interval_spread)': 0, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 2, 'ties: other_day': 10, 'repeats between tied incidents in this run': 0}
- incidents not ordered by their dates (different stored dates, overlapping verified intervals): {'consecutive pairs with overlapping verified intervals': 0, '... of which stored dates differ by under 90 days': 0}
- covariate coverage: {'size_estimate levels': {'HUGE': 249, 'LARGE': 301, 'MEDIUM': 282, 'SMALL': 103, 'UNKNOWN': 72}, 'size_confidence populated': 996, 'employee_count populated': 905, 'turnover populated': 0, 'industry populated': 887, 'entity_kind populated': 1007, 'sector_proxy levels': {'education': 76, 'finance': 61, 'government': 175, 'health': 90, 'industrial': 25, 'media': 8, 'other': 357, 'retail': 43, 'technology': 131, 'telecom_transport': 41}, 'prior records populated': 441}
- organisation size across name variants: {'organisations stored under several name variants': 8, 'organisations whose size changed against the representative-only rule': 1, 'organisations whose variants give conflicting known size bands': 1, 'deciding rule': {'higher size_confidence': 3, 'representative variant': 3, 'known band over UNKNOWN': 2}}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 36, 'events with an unresolved attribution': 36, 'organisations with an unresolved attribution': 35, 'victim-event attributions analysed': 1007, 'repeats': {'included': {'observed repeats': 141, 'entities with a repeat': 78, 'repeats by elapsed band': {'0-90': 26, '91-365': 43, '366-730': 22, '731-1460': 25, '>1460': 25}}, 'excluded': {'observed repeats': 126, 'entities with a repeat': 74, 'repeats by elapsed band': {'0-90': 23, '91-365': 35, '366-730': 17, '731-1460': 27, '>1460': 24}}, 'repeats involving an unresolved attribution': 19}}

## Same-Date Ties And Unknown Ordering

A same-date tie is far more likely one incident that deduplication has not joined than two incidents on one day. Every such pair, and every pair whose possible periods overlap, is an exception listed with the reason it is still two events in `dashboard/close_pairs.html`; the merging steps are fixed when one is a missed duplicate. Until a tie is resolved, each tie policy makes one stated assumption about it (every tie, with its incident ids and the length of any verified interval its incidents share, is in `entity_date_ties.csv`):

- `collapse` (primary): the tied incidents are one occurrence: one time point, never a repeat between them (their ids and count are kept).
- `zero_gap`: the tied incidents are distinct and one day apart, in id order: each after the first is a repeat after 1 day.
- `interval_spread`: the tied incidents are distinct and evenly spread across the verified interval they share (about interval length / n days apart, the first at the interval's start); a tie with no shared verified interval longer than a day is placed as under zero_gap.

None of these bounds the truth: a tie that is one incident filed twice is represented only by `collapse`; two incidents really weeks apart within a verified month are represented exactly by none; and a tie whose dates are unverified has no interval to place its incidents in, so `interval_spread` places it as `zero_gap` does. Agreement across the policies shows only that a result does not hinge on these assumptions. Every policy is adjusted for organisation size like the primary model.

- Under `collapse`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.009). It rests on 141 observed repeats.
- Under `zero_gap`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p < 0.001). It rests on 153 observed repeats.
- Under `interval_spread`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p < 0.001). It rests on 153 observed repeats.

| tie_policy      |   observed_repeats |   repeats_from_ties |   post_event_spells |   repeats_0-90 |   repeats_91-365 |   repeats_366-730 |   repeats_731-1460 |   repeats_>1460 |   adjusted_constant_hazard_p |   adjusted_0_90_91_180_banding_p |   delayed_peak_directional_p |
|:----------------|-------------------:|--------------------:|--------------------:|---------------:|-----------------:|------------------:|-------------------:|----------------:|-----------------------------:|---------------------------------:|-----------------------------:|
| collapse        |                141 |                   0 |                 995 |             26 |               43 |                22 |                 25 |              25 |                    0.009365  |                        0.01916   |                       0.9115 |
| zero_gap        |                153 |                  12 |                1007 |             38 |               43 |                22 |                 25 |              25 |                    3.519e-06 |                        1.032e-05 |                       0.9966 |
| interval_spread |                153 |                  12 |                1007 |             38 |               43 |                22 |                 25 |              25 |                    3.519e-06 |                        1.032e-05 |                       0.9966 |


## Organisation Size Specification

The primary model enters size as a numeric score (SMALL=1 to HUGE=4, UNKNOWN mean-imputed, plus an UNKNOWN indicator), which assumes equal log-rate steps between bands. It is compared here with size as a categorical factor (reference: the band with the most repeats; bands with fewer than 3 repeats pooled) and, where every delayed-peak phase has at least 10 repeats at two or more known size bands, with the score's interaction with the delayed-peak phases. The score stays primary; the headline tests are re-run under categorical size. Full table: `size_specification_comparison.csv`.

- Size specification (bands 0-90, 91-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.176 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 984.7 (categorical) against 984.2 (score), lower for the score model.
- With bands 0-90, 91-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.009 with the size score and p = 0.009 with categorical size: the same verdict at the 5% level.
- Size specification (bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.176 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 1039.4 (categorical) against 1038.9 (score), lower for the score model.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.019 with the size score and p = 0.019 with categorical size: the same verdict at the 5% level.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the directional delayed-peak test gives p = 0.912 with the size score and p = 0.912 with categorical size: the same verdict at the 5% level.
- Size by elapsed-time phase (the size score's slope in 91-180 and post-180 days against 0-90): LRT p = 0.059 on 2 df; at the 5% level the data do not reject a size effect that is the same in every phase.

| banding       | size_spec     |   log_likelihood |   parameters |    aic |   delta_aic_vs_score | nested   |   likelihood_ratio |   df |   p_value |   elapsed_constant_hazard_p |   delayed_peak_directional_p |
|:--------------|:--------------|-----------------:|-------------:|-------:|---------------------:|:---------|-------------------:|-----:|----------:|----------------------------:|-----------------------------:|
| run           | score         |           -465.1 |           27 |  984.2 |               0      |          |            nan     |  nan | nan       |                    0.009365 |                     nan      |
| run           | categorical   |           -463.4 |           29 |  984.7 |               0.5254 | True     |              3.475 |    2 |   0.176   |                    0.00908  |                     nan      |
| 0-90 / 91-180 | score         |           -491.5 |           28 | 1039   |               0      |          |            nan     |  nan | nan       |                    0.01916  |                       0.9115 |
| 0-90 / 91-180 | categorical   |           -489.7 |           30 | 1039   |               0.5256 | True     |              3.474 |    2 |   0.176   |                    0.01863  |                       0.9124 |
| 0-90 / 91-180 | score_x_phase |           -488.6 |           30 | 1037   |              -1.677  | True     |              5.677 |    2 |   0.05852 |                  nan        |                     nan      |


## Primary Piecewise Exponential Test

The adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.009). It rests on 141 observed repeats.

- unadjusted LRT p-value: 0.0007479
- adjusted LRT p-value: 0.009365
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=1), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |       26 |       85161.000 |                      11.151 |              6.596 |              16.000 |
| 91-365         |       43 |      211466.000 |                       7.427 |              4.956 |              10.003 |
| 366-730        |       22 |      206470.000 |                       3.892 |              2.034 |               6.115 |
| 731-1460       |       25 |      212489.000 |                       4.297 |              2.737 |               6.074 |
| >1460          |       25 |      120507.000 |                       7.577 |              4.461 |              10.758 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                       |
|:----------------|-------------:|---------:|----------:|:-------------------------------------|
| SMALL -> MEDIUM |        2.398 |    1.763 |     3.261 | about 140% higher repeat-event rate  |
| SMALL -> LARGE  |        5.748 |    3.108 |    10.632 | about 475% higher repeat-event rate  |
| SMALL -> HUGE   |       13.782 |    5.478 |    34.669 | about 1278% higher repeat-event rate |
| MEDIUM -> HUGE  |        5.748 |    3.108 |    10.632 | about 475% higher repeat-event rate  |
| LARGE -> HUGE   |        2.398 |    1.763 |     3.261 | about 140% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| education           |        6.392 |    1.830 |    22.322 | about 539% higher than government |
| industrial          |        3.529 |    0.701 |    17.760 | about 253% higher than government |
| telecom_transport   |        3.515 |    0.996 |    12.405 | about 252% higher than government |
| technology          |        3.368 |    1.121 |    10.122 | about 237% higher than government |
| finance             |        2.598 |    0.753 |     8.964 | about 160% higher than government |
| health              |        2.139 |    0.542 |     8.442 | about 114% higher than government |
| retail              |        1.522 |    0.307 |     7.540 | about 52% higher than government  |
| other               |        0.516 |    0.131 |     2.032 | about 48% lower than government   |
| sparse_or_no_repeat |        0.000 |    0.000 |   inf     | about 100% lower than government  |

Full adjusted covariate table:

| effect                           |   rate_ratio |   ci_low |   ci_high |
|:---------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body     |        3.560 |    1.187 |    10.676 |
| Entity kind: sparse_or_no_repeat |        1.160 |    0.113 |    11.898 |
| Organisation size                |        2.398 |    1.763 |     3.261 |
| Prior records: 100k+             |        1.692 |    1.087 |     2.634 |
| Prior records: 10k-100k          |        1.513 |    0.914 |     2.504 |
| Prior records: 1k-10k            |        1.244 |    0.666 |     2.324 |
| Prior records: <1k               |        1.175 |    0.584 |     2.365 |
| Sector: education                |        6.392 |    1.830 |    22.322 |
| Sector: finance                  |        2.598 |    0.753 |     8.964 |
| Sector: health                   |        2.139 |    0.542 |     8.442 |
| Sector: industrial               |        3.529 |    0.701 |    17.760 |
| Sector: other                    |        0.516 |    0.131 |     2.032 |
| Sector: retail                   |        1.522 |    0.307 |     7.540 |
| Sector: sparse_or_no_repeat      |        0.000 |    0.000 |   inf     |
| Sector: technology               |        3.368 |    1.121 |    10.122 |
| Sector: telecom_transport        |        3.515 |    0.996 |    12.405 |
| Unknown size                     |        0.722 |    0.183 |     2.851 |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 91-365, 366-730 days vs 0-90 days (unadjusted): rate ratio 0.51 (95% CI 0.32-0.80), about 49% lower; two-sided p = 0.004. Statistically significant at the 5% level: the data reject equal rates (rate ratio 1). The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 65 in 91-365, 366-730 days and 26 in 0-90 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.652. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates do not have the hypothesised shape: long-term vs response goes the other way.

- response vs immediate: rate ratio 0.56 (95% CI 0.35-0.88), about 44% lower; one-sided p = 0.006 (alternative: lower). Statistically significant at the 5% level: the data reject the null of no decrease (rate ratio at least 1). Repeat events: 64 in the response period and 26 in the immediate period.
- long-term vs response: rate ratio 0.93 (95% CI 0.63-1.36), about 7% lower; one-sided p = 0.652 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.63 to 1.36, which includes 1. The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 49 in the long-term period and 64 in the response period.
- long-term vs immediate: rate ratio 0.52 (95% CI 0.31-0.85), about 48% lower; two-sided p = 0.009. Statistically significant at the 5% level: the data reject equal rates (rate ratio 1). Repeat events: 49 in the long-term period and 26 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.03112
- directional U-shape p-value: 0.6521

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       26 |           85161 |                      11.151 |
| Response period  |       64 |          417936 |                       5.593 |
| Long-term period |       49 |          332996 |                       5.375 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        0.557 |    0.352 |     0.882 | about 44% lower  |         0.013 | less                      | 0.006           |
| long-term vs response  |        0.926 |    0.630 |     1.361 | about 7% lower   |         0.696 | greater                   | 0.652           |
| long-term vs immediate |        0.516 |    0.315 |     0.847 | about 48% lower  |         0.009 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        0.557 |    0.352 |     0.882 |      0.530 | about 44% lower  |           0.006 |
| All covariates                | long-term vs response |        0.926 |    0.630 |     1.361 |      0.731 | about 7% lower   |           0.652 |
| Drop organisation size        | response vs immediate |        0.568 |    0.359 |     0.899 |      0.540 | about 43% lower  |           0.008 |
| Drop organisation size        | long-term vs response |        1.015 |    0.692 |     1.490 |      0.798 | about 2% higher  |           0.469 |
| Drop industry/sector          | response vs immediate |        0.540 |    0.342 |     0.854 |      0.512 | about 46% lower  |           0.004 |
| Drop industry/sector          | long-term vs response |        0.912 |    0.622 |     1.336 |      0.714 | about 9% lower   |           0.682 |
| Drop size and industry/sector | response vs immediate |        0.557 |    0.353 |     0.881 |      0.528 | about 44% lower  |           0.006 |
| Drop size and industry/sector | long-term vs response |        1.038 |    0.711 |     1.517 |      0.806 | about 4% higher  |           0.423 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.0311 |                         0.6521 |
| Drop organisation size        |    0.0574 |                         0.4690 |
| Drop industry/sector          |    0.0196 |                         0.6820 |
| Drop size and industry/sector |    0.0498 |                         0.4229 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.35-0.88). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.63-1.36).

## Delayed 91-180 Day Peak Sensitivity

This sensitivity reruns the elapsed-time model with bands `0-90`, `91-180`, `181-365`, `366-730`, `731-1460`, and `>1460` to test whether the apparent early elevation is concentrated in days 91-180.
A formal delayed peak requires the 91-180 day period to be higher than both 0-90 days and the pooled post-180 period after adjustment.

- adjusted elapsed-time LRT p-value for the 90/180 split: 0.01916
- phase-model LRT p-value versus constant elapsed-time risk: 0.02594
- directional delayed-peak p-value: 0.9115

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |       26 |           85161 |                      11.151 |
| Delayed peak 91-180 days |       15 |           76574 |                       7.155 |
| Post-180 days            |       98 |          674358 |                       5.308 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        0.645 |    0.342 |     1.219 | about 35% lower  |         0.177 | greater                   | 0.912           |
| 91-180 vs post-180 |        1.229 |    0.709 |     2.130 | about 23% higher |         0.462 | greater                   | 0.231           |
| post-180 vs 0-90   |        0.525 |    0.337 |     0.817 | about 48% lower  |         0.004 |                           |                 |

Interpretation:

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.912. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates do not have the hypothesised shape: 91-180 vs 0-90 goes the other way.

- 91-180 vs 0-90: rate ratio 0.65 (95% CI 0.34-1.22), about 35% lower; one-sided p = 0.912 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.34 to 1.22, which includes 1. The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 15 in 91-180 days and 26 in 0-90 days.
- 91-180 vs post-180: rate ratio 1.23 (95% CI 0.71-2.13), about 23% higher; one-sided p = 0.231 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.71 to 2.13, which includes 1. Repeat events: 15 in 91-180 days and 98 in post-180 days.
- post-180 vs 0-90: rate ratio 0.52 (95% CI 0.34-0.82), about 48% lower; two-sided p = 0.004. Statistically significant at the 5% level: the data reject equal rates (rate ratio 1). Repeat events: 98 in post-180 days and 26 in 0-90 days.

## Unadjusted Parametric Survival Model Comparison (Descriptive)

Best AIC model: generalized_gamma. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| generalized_gamma |        -1358.563 |            3 | 2723.127 | 2737.835 | True        |
| weibull           |        -1360.602 |            2 | 2725.204 | 2735.009 | True        |
| loglogistic       |        -1362.667 |            2 | 2729.334 | 2739.140 | True        |
| exponential       |        -1365.971 |            1 | 2733.941 | 2738.844 | True        |
| lognormal         |        -1368.827 |            2 | 2741.653 | 2751.459 | True        |

Exponential vs Weibull LRT p-value: 0.00105

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
