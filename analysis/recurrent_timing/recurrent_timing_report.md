# Repeat Cyber-Event Timing Analysis

## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.
The primary model includes recorded organisation size. Size is a proxy for differences in visibility, attack exposure and target value, not a direct measure of those factors; residual confounding remains possible. The ordinal size term assumes equal log-rate steps between categories (compared with categorical size and a size-by-elapsed-time interaction under Organisation Size Specification), UNKNOWN has a separate indicator, an organisation stored under several names takes the size its variants support best (`organisation_size_reconciliation.csv`), and current recorded size may differ from size at an incident's date. Unadjusted rates, plots, parametric models and size-dropped variants below are descriptive diagnostics and do not control for this confounding.

## Data Quality Summary

- victim event attributions: 1131
- unique deduplicated events: 910
- victim entities: 959
- entities with more than one event: 86
- post-event spells: 1127
- observed repeat spells: 168
- right-censored spells: 959
- censor date: 2026-10-30
- elapsed-time bands: 0-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2003-07-04
- maximum event date: 2026-10-27
- first-of-month event dates: 257
- verified incident-date precision: {'unverified': 656, 'day': 241, 'month': 234}
- tie policy (several incidents of one organisation on one date): collapse
- same-date ties: {'ties (organisation-dates with several incidents)': 4, 'incidents in ties': 8, 'organisations with a tie': 4, 'repeats a policy that separates tied incidents adds': 4, 'ties with a shared verified interval longer than a day (spread by interval_spread)': 0, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 3, 'ties: other_day': 1, 'repeats between tied incidents in this run': 0}
- incidents not ordered by their dates (different stored dates, overlapping verified intervals): {'consecutive pairs with overlapping verified intervals': 0, '... of which stored dates differ by under 90 days': 0}
- covariate coverage: {'size_estimate levels': {'HUGE': 293, 'LARGE': 331, 'MEDIUM': 301, 'SMALL': 111, 'UNKNOWN': 95}, 'size_confidence populated': 1131, 'employee_count populated': 998, 'turnover populated': 0, 'industry populated': 991, 'entity_kind populated': 1131, 'sector_proxy levels': {'education': 80, 'finance': 80, 'government': 202, 'health': 99, 'industrial': 25, 'media': 8, 'other': 401, 'retail': 45, 'technology': 140, 'telecom_transport': 51}, 'prior records populated': 512}
- organisation size across name variants: {'organisations stored under several name variants': 7, 'organisations whose size changed against the representative-only rule': 1, 'organisations whose variants give conflicting known size bands': 1, 'deciding rule': {'higher size_confidence': 4, 'known band over UNKNOWN': 3}}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 55, 'events with an unresolved attribution': 55, 'organisations with an unresolved attribution': 54, 'victim-event attributions analysed': 1131, 'repeats': {'included': {'observed repeats': 168, 'entities with a repeat': 84, 'repeats by elapsed band': {'0-180': 47, '181-365': 30, '366-730': 30, '731-1460': 37, '>1460': 24}}, 'excluded': {'observed repeats': 147, 'entities with a repeat': 77, 'repeats by elapsed band': {'0-180': 40, '181-365': 25, '366-730': 25, '731-1460': 35, '>1460': 22}}, 'repeats involving an unresolved attribution': 26}}

## Same-Date Ties And Unknown Ordering

Several distinct incidents of one organisation can carry the same stored date; nothing orders them, and a date stored as the 1st of a month may only mean that month. Each tie policy makes one stated assumption about the tied incidents (every tie, with its incident ids and the length of any verified interval its incidents share, is in `entity_date_ties.csv`):

- `collapse` (primary): the tied incidents are one occurrence: one time point, never a repeat between them (their ids and count are kept).
- `zero_gap`: the tied incidents are distinct and one day apart, in id order: each after the first is a repeat after 1 day.
- `interval_spread`: the tied incidents are distinct and evenly spread across the verified interval they share (about interval length / n days apart, the first at the interval's start); a tie with no shared verified interval longer than a day is placed as under zero_gap.

None of these bounds the truth: a tie that is one incident filed twice is represented only by `collapse`; two incidents really weeks apart within a verified month are represented exactly by none; and a tie whose dates are unverified has no interval to place its incidents in, so `interval_spread` places it as `zero_gap` does. Agreement across the policies shows only that a result does not hinge on these assumptions. Every policy is adjusted for organisation size like the primary model.

- Under `collapse`, the adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.197). It rests on 168 observed repeats.
- Under `zero_gap`, the adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.076, borderline). It rests on 172 observed repeats.
- Under `interval_spread`, the adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.076, borderline). It rests on 172 observed repeats.

| tie_policy      |   observed_repeats |   repeats_from_ties |   post_event_spells |   repeats_0-180 |   repeats_181-365 |   repeats_366-730 |   repeats_731-1460 |   repeats_>1460 |   adjusted_constant_hazard_p |   adjusted_0_90_91_180_banding_p |   delayed_peak_directional_p |
|:----------------|-------------------:|--------------------:|--------------------:|----------------:|------------------:|------------------:|-------------------:|----------------:|-----------------------------:|---------------------------------:|-----------------------------:|
| collapse        |                168 |                   0 |                1127 |              47 |                30 |                30 |                 37 |              24 |                      0.1971  |                           0.2966 |                       0.3933 |
| zero_gap        |                172 |                   4 |                1131 |              51 |                30 |                30 |                 37 |              24 |                      0.07596 |                           0.1289 |                       0.6074 |
| interval_spread |                172 |                   4 |                1131 |              51 |                30 |                30 |                 37 |              24 |                      0.07596 |                           0.1289 |                       0.6074 |


## Organisation Size Specification

The primary model enters size as a numeric score (SMALL=1 to HUGE=4, UNKNOWN mean-imputed, plus an UNKNOWN indicator), which assumes equal log-rate steps between bands. It is compared here with size as a categorical factor (reference: the band with the most repeats; bands with fewer than 3 repeats pooled) and, where every delayed-peak phase has at least 10 repeats at two or more known size bands, with the score's interaction with the delayed-peak phases. The score stays primary; the headline tests are re-run under categorical size. Full table: `size_specification_comparison.csv`.

- Size specification (bands 0-180, 181-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.403 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 1170.0 (categorical) against 1167.8 (score), lower for the score model.
- With bands 0-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.197 with the size score and p = 0.197 with categorical size: the same verdict at the 5% level.
- Size specification (bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.403 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 1232.1 (categorical) against 1229.9 (score), lower for the score model.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.297 with the size score and p = 0.297 with categorical size: the same verdict at the 5% level.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the directional delayed-peak test gives p = 0.393 with the size score and p = 0.393 with categorical size: the same verdict at the 5% level.
- Size by elapsed-time phase (the size score's slope in 91-180 and post-180 days against 0-90): LRT p = 0.231 on 2 df; at the 5% level the data do not reject a size effect that is the same in every phase.

| banding       | size_spec     |   log_likelihood |   parameters |   aic |   delta_aic_vs_score | nested   |   likelihood_ratio |   df |   p_value |   elapsed_constant_hazard_p |   delayed_peak_directional_p |
|:--------------|:--------------|-----------------:|-------------:|------:|---------------------:|:---------|-------------------:|-----:|----------:|----------------------------:|-----------------------------:|
| run           | score         |           -557.9 |           26 |  1168 |                0     |          |            nan     |  nan |  nan      |                      0.1971 |                     nan      |
| run           | categorical   |           -557   |           28 |  1170 |                2.184 | True     |              1.816 |    2 |    0.4034 |                      0.1971 |                     nan      |
| 0-90 / 91-180 | score         |           -588   |           27 |  1230 |                0     |          |            nan     |  nan |  nan      |                      0.2966 |                       0.3933 |
| 0-90 / 91-180 | categorical   |           -587   |           29 |  1232 |                2.184 | True     |              1.816 |    2 |    0.4033 |                      0.2965 |                       0.3931 |
| 0-90 / 91-180 | score_x_phase |           -586.5 |           29 |  1231 |                1.068 | True     |              2.932 |    2 |    0.2309 |                    nan      |                     nan      |


## Primary Piecewise Exponential Test

The adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.197). It rests on 168 observed repeats.

- unadjusted LRT p-value: 0.03162
- adjusted LRT p-value: 0.1971
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=2), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-180          |       47 |      184035.000 |                       9.328 |              5.883 |              13.124 |
| 181-365        |       30 |      154843.000 |                       7.077 |              4.289 |               9.802 |
| 366-730        |       30 |      237798.000 |                       4.608 |              2.778 |               6.561 |
| 731-1460       |       37 |      242914.000 |                       5.563 |              3.600 |               7.597 |
| >1460          |       24 |      134679.000 |                       6.509 |              3.959 |               9.614 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        2.114 |    1.607 |     2.781 | about 111% higher repeat-event rate |
| SMALL -> LARGE  |        4.470 |    2.583 |     7.736 | about 347% higher repeat-event rate |
| SMALL -> HUGE   |        9.452 |    4.152 |    21.515 | about 845% higher repeat-event rate |
| MEDIUM -> HUGE  |        4.470 |    2.583 |     7.736 | about 347% higher repeat-event rate |
| LARGE -> HUGE   |        2.114 |    1.607 |     2.781 | about 111% higher repeat-event rate |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| education           |        3.756 |    1.559 |     9.050 | about 276% higher than government |
| finance             |        2.629 |    1.152 |     6.001 | about 163% higher than government |
| technology          |        2.549 |    1.161 |     5.596 | about 155% higher than government |
| telecom_transport   |        2.409 |    1.134 |     5.117 | about 141% higher than government |
| industrial          |        2.323 |    0.599 |     9.008 | about 132% higher than government |
| health              |        1.499 |    0.619 |     3.630 | about 50% higher than government  |
| sparse_or_no_repeat |        0.947 |    0.251 |     3.576 | about 5% lower than government    |
| other               |        0.502 |    0.193 |     1.307 | about 50% lower than government   |

Full adjusted covariate table:

| effect                           |   rate_ratio |   ci_low |   ci_high |
|:---------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body     |        2.520 |    1.230 |     5.164 |
| Entity kind: sparse_or_no_repeat |        0.000 |    0.000 |   inf     |
| Organisation size                |        2.114 |    1.607 |     2.781 |
| Prior records: 100k+             |        1.100 |    0.718 |     1.685 |
| Prior records: 10k-100k          |        1.201 |    0.777 |     1.856 |
| Prior records: 1k-10k            |        0.977 |    0.556 |     1.714 |
| Prior records: <1k               |        0.957 |    0.504 |     1.815 |
| Sector: education                |        3.756 |    1.559 |     9.050 |
| Sector: finance                  |        2.629 |    1.152 |     6.001 |
| Sector: health                   |        1.499 |    0.619 |     3.630 |
| Sector: industrial               |        2.323 |    0.599 |     9.008 |
| Sector: other                    |        0.502 |    0.193 |     1.307 |
| Sector: sparse_or_no_repeat      |        0.947 |    0.251 |     3.576 |
| Sector: technology               |        2.549 |    1.161 |     5.596 |
| Sector: telecom_transport        |        2.409 |    1.134 |     5.117 |
| Unknown size                     |        0.745 |    0.297 |     1.867 |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 181-365, 366-730 days vs 0-180 days (unadjusted): rate ratio 0.60 (95% CI 0.41-0.88), about 40% lower; two-sided p = 0.008. Statistically significant at the 5% level: the data reject equal rates (rate ratio 1). The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 60 in 181-365, 366-730 days and 47 in 0-180 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.273. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates have the hypothesised shape. The weakest part is long-term vs response (p = 0.273).

- response vs immediate: rate ratio 0.67 (95% CI 0.46-0.99), about 33% lower; one-sided p = 0.021 (alternative: lower). Statistically significant at the 5% level: the data reject the null of no decrease (rate ratio at least 1). Repeat events: 59 in the response period and 47 in the immediate period.
- long-term vs response: rate ratio 1.12 (95% CI 0.77-1.62), about 12% higher; one-sided p = 0.273 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.77 to 1.62, which includes 1. Repeat events: 60 in the long-term period and 59 in the response period.
- long-term vs immediate: rate ratio 0.75 (95% CI 0.51-1.12), about 25% lower; two-sided p = 0.159. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.51 to 1.12, which includes 1. Repeat events: 60 in the long-term period and 47 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.1297
- directional U-shape p-value: 0.2727

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       47 |          184035 |                       9.328 |
| Response period  |       59 |          392641 |                       5.488 |
| Long-term period |       60 |          377593 |                       5.804 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        0.671 |    0.457 |     0.986 | about 33% lower  |         0.042 | less                      | 0.021           |
| long-term vs response  |        1.120 |    0.775 |     1.620 | about 12% higher |         0.545 | greater                   | 0.273           |
| long-term vs immediate |        0.752 |    0.505 |     1.119 | about 25% lower  |         0.159 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        0.671 |    0.457 |     0.986 |      0.530 | about 33% lower  |           0.021 |
| All covariates                | long-term vs response |        1.120 |    0.775 |     1.620 |      0.845 | about 12% higher |           0.273 |
| Drop organisation size        | response vs immediate |        0.676 |    0.460 |     0.993 |      0.533 | about 32% lower  |           0.023 |
| Drop organisation size        | long-term vs response |        1.154 |    0.799 |     1.668 |      0.869 | about 15% higher |           0.222 |
| Drop industry/sector          | response vs immediate |        0.660 |    0.449 |     0.969 |      0.520 | about 34% lower  |           0.017 |
| Drop industry/sector          | long-term vs response |        1.144 |    0.791 |     1.653 |      0.862 | about 14% higher |           0.237 |
| Drop size and industry/sector | response vs immediate |        0.670 |    0.456 |     0.984 |      0.528 | about 33% lower  |           0.021 |
| Drop size and industry/sector | long-term vs response |        1.233 |    0.855 |     1.777 |      0.922 | about 23% higher |           0.131 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.1297 |                         0.2727 |
| Drop organisation size        |    0.1420 |                         0.2221 |
| Drop industry/sector          |    0.1101 |                         0.2373 |
| Drop size and industry/sector |    0.1231 |                         0.1308 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.46-0.99). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.77-1.62).

## Delayed 91-180 Day Peak Sensitivity

This sensitivity reruns the elapsed-time model with bands `0-90`, `91-180`, `181-365`, `366-730`, `731-1460`, and `>1460` to test whether the apparent early elevation is concentrated in days 91-180.
A formal delayed peak requires the 91-180 day period to be higher than both 0-90 days and the pooled post-180 period after adjustment.

- adjusted elapsed-time LRT p-value for the 90/180 split: 0.2966
- phase-model LRT p-value versus constant elapsed-time risk: 0.1501
- directional delayed-peak p-value: 0.3933

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |       24 |           96781 |                       9.058 |
| Delayed peak 91-180 days |       23 |           87254 |                       9.628 |
| Post-180 days            |      119 |          770234 |                       5.643 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        1.082 |    0.611 |     1.918 | about 8% higher  |         0.787 | greater                   | 0.393           |
| 91-180 vs post-180 |        1.475 |    0.939 |     2.315 | about 47% higher |         0.091 | greater                   | 0.046           |
| post-180 vs 0-90   |        0.734 |    0.470 |     1.145 | about 27% lower  |         0.173 |                           |                 |

Interpretation:

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.393. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates have the hypothesised shape. The weakest part is 91-180 vs 0-90 (p = 0.393).

- 91-180 vs 0-90: rate ratio 1.08 (95% CI 0.61-1.92), about 8% higher; one-sided p = 0.393 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.61 to 1.92, which includes 1. Repeat events: 23 in 91-180 days and 24 in 0-90 days.
- 91-180 vs post-180: rate ratio 1.47 (95% CI 0.94-2.31), about 47% higher; one-sided p = 0.046 (alternative: higher). Statistically significant at the 5% level: the data reject the null of no increase (rate ratio at most 1). The two-sided 95% CI still includes 1, so the evidence is one-sided only. Repeat events: 23 in 91-180 days and 119 in post-180 days.
- post-180 vs 0-90: rate ratio 0.73 (95% CI 0.47-1.14), about 27% lower; two-sided p = 0.173. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.47 to 1.14, which includes 1. Repeat events: 119 in post-180 days and 24 in 0-90 days.

## Unadjusted Parametric Survival Model Comparison (Descriptive)

Best AIC model: generalized_gamma. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| generalized_gamma |        -1616.468 |            3 | 3238.936 | 3254.018 | True        |
| weibull           |        -1617.471 |            2 | 3238.941 | 3248.996 | True        |
| loglogistic       |        -1618.936 |            2 | 3241.873 | 3251.928 | True        |
| exponential       |        -1620.316 |            1 | 3242.632 | 3247.659 | True        |
| lognormal         |        -1624.308 |            2 | 3252.616 | 3262.670 | True        |

Exponential vs Weibull LRT p-value: 0.01706

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
