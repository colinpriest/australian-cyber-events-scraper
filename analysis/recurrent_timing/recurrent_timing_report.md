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
- elapsed-time bands: 0-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2003-07-04
- maximum event date: 2026-10-27
- first-of-month event dates: 257
- verified incident-date precision: {'unverified': 656, 'day': 241, 'month': 234}
- tie policy (several incidents of one organisation on one date): collapse
- same-date ties: {'ties (organisation-dates with several incidents)': 4, 'incidents in ties': 8, 'organisations with a tie': 4, 'repeats a policy that separates tied incidents adds': 4, 'ties with a shared verified interval longer than a day (spread by interval_spread)': 0, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 3, 'ties: other_day': 1, 'repeats between tied incidents in this run': 0}
- incidents not ordered by their dates (different stored dates, overlapping verified intervals): {'consecutive pairs with overlapping verified intervals': 0, '... of which stored dates differ by under 90 days': 0}
- covariate coverage: {'size_estimate levels': {'HUGE': 291, 'LARGE': 332, 'MEDIUM': 301, 'SMALL': 111, 'UNKNOWN': 96}, 'size_confidence populated': 1131, 'employee_count populated': 997, 'turnover populated': 0, 'industry populated': 991, 'entity_kind populated': 1131, 'sector_proxy levels': {'education': 80, 'finance': 81, 'government': 201, 'health': 99, 'industrial': 25, 'media': 8, 'other': 401, 'retail': 45, 'technology': 140, 'telecom_transport': 51}, 'prior records populated': 512}
- organisation size across name variants: {'organisations stored under several name variants': 3, 'organisations whose size changed against the representative-only rule': 1, 'organisations whose variants give conflicting known size bands': 0, 'deciding rule': {'known band over UNKNOWN': 2, 'higher size_confidence': 1}}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 55, 'events with an unresolved attribution': 55, 'organisations with an unresolved attribution': 54, 'victim-event attributions analysed': 1131, 'repeats': {'included': {'observed repeats': 166, 'entities with a repeat': 84, 'repeats by elapsed band': {'0-180': 45, '181-365': 30, '366-730': 30, '731-1460': 37, '>1460': 24}}, 'excluded': {'observed repeats': 145, 'entities with a repeat': 77, 'repeats by elapsed band': {'0-180': 39, '181-365': 24, '366-730': 24, '731-1460': 36, '>1460': 22}}, 'repeats involving an unresolved attribution': 26}}

## Same-Date Ties And Unknown Ordering

Several distinct incidents of one organisation can carry the same stored date; nothing orders them, and a date stored as the 1st of a month may only mean that month. Each tie policy makes one stated assumption about the tied incidents (every tie, with its incident ids and the length of any verified interval its incidents share, is in `entity_date_ties.csv`):

- `collapse` (primary): the tied incidents are one occurrence: one time point, never a repeat between them (their ids and count are kept).
- `zero_gap`: the tied incidents are distinct and one day apart, in id order: each after the first is a repeat after 1 day.
- `interval_spread`: the tied incidents are distinct and evenly spread across the verified interval they share (about interval length / n days apart, the first at the interval's start); a tie with no shared verified interval longer than a day is placed as under zero_gap.

None of these bounds the truth: a tie that is one incident filed twice is represented only by `collapse`; two incidents really weeks apart within a verified month are represented exactly by none; and a tie whose dates are unverified has no interval to place its incidents in, so `interval_spread` places it as `zero_gap` does. Agreement across the policies shows only that a result does not hinge on these assumptions. Every policy is adjusted for organisation size like the primary model.

- Under `collapse`, the adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.286). It rests on 166 observed repeats.
- Under `zero_gap`, the adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.126). It rests on 170 observed repeats.
- Under `interval_spread`, the adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.126). It rests on 170 observed repeats.

| tie_policy      |   observed_repeats |   repeats_from_ties |   post_event_spells |   repeats_0-180 |   repeats_181-365 |   repeats_366-730 |   repeats_731-1460 |   repeats_>1460 |   adjusted_constant_hazard_p |   adjusted_0_90_91_180_banding_p |   delayed_peak_directional_p |
|:----------------|-------------------:|--------------------:|--------------------:|----------------:|------------------:|------------------:|-------------------:|----------------:|-----------------------------:|---------------------------------:|-----------------------------:|
| collapse        |                166 |                   0 |                1127 |              45 |                30 |                30 |                 37 |              24 |                       0.286  |                           0.4065 |                       0.3996 |
| zero_gap        |                170 |                   4 |                1131 |              49 |                30 |                30 |                 37 |              24 |                       0.1265 |                           0.201  |                       0.6177 |
| interval_spread |                170 |                   4 |                1131 |              49 |                30 |                30 |                 37 |              24 |                       0.1265 |                           0.201  |                       0.6177 |


## Organisation Size Specification

The primary model enters size as a numeric score (SMALL=1 to HUGE=4, UNKNOWN mean-imputed, plus an UNKNOWN indicator), which assumes equal log-rate steps between bands. It is compared here with size as a categorical factor (reference: the band with the most repeats; bands with fewer than 3 repeats pooled) and, where every delayed-peak phase has at least 10 repeats at two or more known size bands, with the score's interaction with the delayed-peak phases. The score stays primary; the headline tests are re-run under categorical size. Full table: `size_specification_comparison.csv`.

- Size specification (bands 0-180, 181-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.396 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 1160.2 (categorical) against 1158.1 (score), lower for the score model.
- With bands 0-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.286 with the size score and p = 0.288 with categorical size: the same verdict at the 5% level.
- Size specification (bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.396 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 1220.6 (categorical) against 1218.5 (score), lower for the score model.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.407 with the size score and p = 0.409 with categorical size: the same verdict at the 5% level.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the directional delayed-peak test gives p = 0.400 with the size score and p = 0.399 with categorical size: the same verdict at the 5% level.
- Size by elapsed-time phase (the size score's slope in 91-180 and post-180 days against 0-90): LRT p = 0.185 on 2 df; at the 5% level the data do not reject a size effect that is the same in every phase.

| banding       | size_spec     |   log_likelihood |   parameters |   aic |   delta_aic_vs_score | nested   |   likelihood_ratio |   df |   p_value |   elapsed_constant_hazard_p |   delayed_peak_directional_p |
|:--------------|:--------------|-----------------:|-------------:|------:|---------------------:|:---------|-------------------:|-----:|----------:|----------------------------:|-----------------------------:|
| run           | score         |           -553.1 |           26 |  1158 |               0      |          |            nan     |  nan |  nan      |                      0.286  |                     nan      |
| run           | categorical   |           -552.1 |           28 |  1160 |               2.147  | True     |              1.853 |    2 |    0.396  |                      0.288  |                     nan      |
| 0-90 / 91-180 | score         |           -582.2 |           27 |  1218 |               0      |          |            nan     |  nan |  nan      |                      0.4065 |                       0.3996 |
| 0-90 / 91-180 | categorical   |           -581.3 |           29 |  1221 |               2.147  | True     |              1.853 |    2 |    0.3959 |                      0.4088 |                       0.3992 |
| 0-90 / 91-180 | score_x_phase |           -580.5 |           29 |  1219 |               0.6277 | True     |              3.372 |    2 |    0.1852 |                    nan      |                     nan      |


## Primary Piecewise Exponential Test

The adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.286). It rests on 166 observed repeats.

- unadjusted LRT p-value: 0.05563
- adjusted LRT p-value: 0.286
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=2), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-180          |       45 |      184280.000 |                       8.919 |              5.696 |              12.773 |
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
| SMALL -> LARGE  |        4.445 |    2.563 |     7.711 | about 345% higher repeat-event rate |
| SMALL -> HUGE   |        9.372 |    4.102 |    21.411 | about 837% higher repeat-event rate |
| MEDIUM -> HUGE  |        4.445 |    2.563 |     7.711 | about 345% higher repeat-event rate |
| LARGE -> HUGE   |        2.108 |    1.601 |     2.777 | about 111% higher repeat-event rate |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| education           |        3.753 |    1.546 |     9.115 | about 275% higher than government |
| technology          |        2.595 |    1.173 |     5.741 | about 160% higher than government |
| finance             |        2.557 |    1.107 |     5.908 | about 156% higher than government |
| industrial          |        2.192 |    0.559 |     8.587 | about 119% higher than government |
| telecom_transport   |        2.128 |    0.981 |     4.613 | about 113% higher than government |
| health              |        1.492 |    0.613 |     3.631 | about 49% higher than government  |
| sparse_or_no_repeat |        0.942 |    0.248 |     3.571 | about 6% lower than government    |
| other               |        0.495 |    0.189 |     1.297 | about 51% lower than government   |

Full adjusted covariate table:

| effect                           |   rate_ratio |   ci_low |                                                                                                                                                                                                                                                      ci_high |
|:---------------------------------|-------------:|---------:|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------:|
| Entity kind: government_body     |        2.446 |    1.184 |                                                                                                                                                                                                                                                        5.055 |
| Entity kind: sparse_or_no_repeat |        0.000 |    0.000 | 23372238798464413751472016019755688723723102033714894222158318481399240324164556134361083499743624085237220050371373287030975116383563987207017289669357958675754814078875975708001796140200099360575689945645239373866262804739435361594113195784536064.000 |
| Organisation size                |        2.108 |    1.601 |                                                                                                                                                                                                                                                        2.777 |
| Prior records: 100k+             |        1.038 |    0.675 |                                                                                                                                                                                                                                                        1.596 |
| Prior records: 10k-100k          |        1.222 |    0.791 |                                                                                                                                                                                                                                                        1.890 |
| Prior records: 1k-10k            |        0.993 |    0.565 |                                                                                                                                                                                                                                                        1.744 |
| Prior records: <1k               |        1.147 |    0.601 |                                                                                                                                                                                                                                                        2.187 |
| Sector: education                |        3.753 |    1.546 |                                                                                                                                                                                                                                                        9.115 |
| Sector: finance                  |        2.557 |    1.107 |                                                                                                                                                                                                                                                        5.908 |
| Sector: health                   |        1.492 |    0.613 |                                                                                                                                                                                                                                                        3.631 |
| Sector: industrial               |        2.192 |    0.559 |                                                                                                                                                                                                                                                        8.587 |
| Sector: other                    |        0.495 |    0.189 |                                                                                                                                                                                                                                                        1.297 |
| Sector: sparse_or_no_repeat      |        0.942 |    0.248 |                                                                                                                                                                                                                                                        3.571 |
| Sector: technology               |        2.595 |    1.173 |                                                                                                                                                                                                                                                        5.741 |
| Sector: telecom_transport        |        2.128 |    0.981 |                                                                                                                                                                                                                                                        4.613 |
| Unknown size                     |        0.728 |    0.290 |                                                                                                                                                                                                                                                        1.824 |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 181-365, 366-730 days vs 0-180 days (unadjusted): rate ratio 0.62 (95% CI 0.42-0.92), about 38% lower; two-sided p = 0.017. Statistically significant at the 5% level: the data reject equal rates (rate ratio 1). The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 60 in 181-365, 366-730 days and 45 in 0-180 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.278. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates have the hypothesised shape. The weakest part is long-term vs response (p = 0.278).

- response vs immediate: rate ratio 0.70 (95% CI 0.48-1.04), about 30% lower; one-sided p = 0.038 (alternative: lower). Statistically significant at the 5% level: the data reject the null of no decrease (rate ratio at least 1). The two-sided 95% CI still includes 1, so the evidence is one-sided only. Repeat events: 59 in the response period and 45 in the immediate period.
- long-term vs response: rate ratio 1.12 (95% CI 0.77-1.62), about 12% higher; one-sided p = 0.278 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.77 to 1.62, which includes 1. Repeat events: 60 in the long-term period and 59 in the response period.
- long-term vs immediate: rate ratio 0.78 (95% CI 0.52-1.17), about 22% lower; two-sided p = 0.236. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.52 to 1.17, which includes 1. Repeat events: 60 in the long-term period and 45 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.2102
- directional U-shape p-value: 0.2784

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       45 |          184280 |                       8.919 |
| Response period  |       59 |          393758 |                       5.473 |
| Long-term period |       60 |          378968 |                       5.783 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        0.702 |    0.475 |     1.036 | about 30% lower  |         0.075 | less                      | 0.038           |
| long-term vs response  |        1.117 |    0.772 |     1.616 | about 12% higher |         0.557 | greater                   | 0.278           |
| long-term vs immediate |        0.784 |    0.524 |     1.172 | about 22% lower  |         0.236 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        0.702 |    0.475 |     1.036 |      0.561 | about 30% lower  |           0.038 |
| All covariates                | long-term vs response |        1.117 |    0.772 |     1.616 |      0.844 | about 12% higher |           0.278 |
| Drop organisation size        | response vs immediate |        0.707 |    0.479 |     1.044 |      0.565 | about 29% lower  |           0.041 |
| Drop organisation size        | long-term vs response |        1.157 |    0.800 |     1.672 |      0.872 | about 16% higher |           0.220 |
| Drop industry/sector          | response vs immediate |        0.690 |    0.467 |     1.018 |      0.551 | about 31% lower  |           0.031 |
| Drop industry/sector          | long-term vs response |        1.139 |    0.788 |     1.646 |      0.858 | about 14% higher |           0.245 |
| Drop size and industry/sector | response vs immediate |        0.701 |    0.475 |     1.034 |      0.559 | about 30% lower  |           0.037 |
| Drop size and industry/sector | long-term vs response |        1.232 |    0.855 |     1.776 |      0.921 | about 23% higher |           0.132 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.2102 |                         0.2784 |
| Drop organisation size        |    0.2248 |                         0.2196 |
| Drop industry/sector          |    0.1809 |                         0.2450 |
| Drop size and industry/sector |    0.1883 |                         0.1318 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.48-1.04). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.77-1.62).

## Delayed 91-180 Day Peak Sensitivity

This sensitivity reruns the elapsed-time model with bands `0-90`, `91-180`, `181-365`, `366-730`, `731-1460`, and `>1460` to test whether the apparent early elevation is concentrated in days 91-180.
A formal delayed peak requires the 91-180 day period to be higher than both 0-90 days and the pooled post-180 period after adjustment.

- adjusted elapsed-time LRT p-value for the 90/180 split: 0.4065
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

Interpretation:

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.400. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates have the hypothesised shape. The weakest part is 91-180 vs 0-90 (p = 0.400).

- 91-180 vs 0-90: rate ratio 1.08 (95% CI 0.60-1.94), about 8% higher; one-sided p = 0.400 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.60 to 1.94, which includes 1. Repeat events: 22 in 91-180 days and 23 in 0-90 days.
- 91-180 vs post-180: rate ratio 1.41 (95% CI 0.89-2.23), about 41% higher; one-sided p = 0.071 (alternative: higher). Not statistically significant at the 5% level (borderline): the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.89 to 2.23, which includes 1. Repeat events: 22 in 91-180 days and 119 in post-180 days.
- post-180 vs 0-90: rate ratio 0.77 (95% CI 0.49-1.20), about 23% lower; two-sided p = 0.247. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.49 to 1.20, which includes 1. Repeat events: 119 in post-180 days and 23 in 0-90 days.

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
