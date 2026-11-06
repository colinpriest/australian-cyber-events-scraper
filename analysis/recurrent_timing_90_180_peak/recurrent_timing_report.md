# Repeat Cyber-Event Timing Analysis

## Executive Summary

**Is the data adequate for a reliable recurrence model?** Adequate for a simple model: 157 repeats, 20 per parameter of the models used here; but the full adjusted model is over-parameterised (27 parameters, 5.8 repeats each, 1 not identified), so it is not used for these conclusions; 52% of incident dates are unverified, so gaps can be resolved only to about a month: splits finer than 30 days are not attempted.

**What are the best splits for recurrence time bands?** Best bands: 0-270, >270 days, with rates 1.00, 0.55 times the first band's (strong evidence: 6.2 BIC better than a constant rate, holding size, prior event number and a calendar trend; search-adjusted p = 0.004). The long-term growth in recorded incidents does not explain it: the per-organisation rate changes by -0% a year (yearly effects from 2020, p = 0.74), and the bands hold under no calendar adjustment, a linear trend, monthly reporting intensity and yearly effects alike (p from 0.0002 to 0.0007).

**Is the rate lower 1-90 days after an event (short-term vigilance)?** No evidence of short-term vigilance: the adjusted rate 1-90 days after an event is not lower than at other times (one-sided p = 0.895; 24 repeats against 19.3 expected if the rate were no different).

**Is there a delayed peak in recurrence 91-180 days after an event?** Yes: the adjusted rate 91-180 days after an event is 1.71 times the rate at other times (95% CI 1.08-2.60; one-sided p = 0.011; 27 repeats). The narrower 91-120 day window holds only 8 repeats, too few to test on its own.

**What fits recurrence rates best?** Best fit by AIC, holding size, prior event number and a calendar trend: bands 0-270, >270. The constant rate is 10.4 AIC behind. The bands were chosen by the search, so that margin is optimistic.

**Does organisation size predict recurrence rates?** Yes: the recurrence rate rises with organisation size. Rate relative to SMALL organisations: MEDIUM 1.4, LARGE 3.6, HUGE 9.0 (monotone model holding prior event number and a calendar trend, bootstrap p <= 0.002 (no simulated value as extreme in 500)). Size stands for visibility and exposure as much as size: this is prediction, not cause.


## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.
The primary model includes recorded organisation size. Size is a proxy for differences in visibility, attack exposure and target value, not a direct measure of those factors; residual confounding remains possible. The ordinal size term assumes equal log-rate steps between categories (compared with categorical size and a size-by-elapsed-time interaction under Organisation Size Specification), UNKNOWN has a separate indicator, an organisation stored under several names takes the size its variants support best (`organisation_size_reconciliation.csv`), and current recorded size may differ from size at an incident's date. Unadjusted rates, plots, parametric models and size-dropped variants below are descriptive diagnostics and do not control for this confounding.

## Data Quality Summary

- victim event attributions: 1063
- unique deduplicated events: 1008
- victim entities: 906
- entities with more than one event: 105
- post-event spells: 1063
- observed repeat spells: 157
- right-censored spells: 906
- observation window (events before its start are excluded): {'observation start': '2020-01-01', 'victim event attributions before it (excluded)': 94, 'events before it (excluded)': 88, 'organisations with events only before it (excluded)': 26}
- censor date: 2026-11-03
- elapsed-time bands: 0-90, 91-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2020-01-01
- maximum event date: 2026-11-01
- first-of-month event dates: 245
- verified incident-date precision: {'unverified': 553, 'day': 390, 'month': 120}
- tie policy (several incidents of one organisation on one date): collapse
- same-date ties: {'ties (organisation-dates with several incidents)': 0, 'incidents in ties': 0, 'organisations with a tie': 0, 'repeats a policy that separates tied incidents adds': 0, 'ties with a shared verified interval longer than a day (spread by interval_spread)': 0, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 0, 'ties: other_day': 0, 'repeats between tied incidents in this run': 0}
- incidents not ordered by their dates (different stored dates, overlapping verified intervals): {'consecutive pairs with overlapping verified intervals': 2, '... of which stored dates differ by under 90 days': 2}
- covariate coverage: {'size_estimate levels': {'HUGE': 237, 'LARGE': 348, 'MEDIUM': 320, 'SMALL': 123, 'UNKNOWN': 35}, 'size_confidence populated': 1057, 'employee_count populated': 994, 'turnover populated': 0, 'industry populated': 925, 'entity_kind populated': 1063, 'sector_proxy levels': {'education': 87, 'finance': 55, 'government': 201, 'health': 89, 'industrial': 31, 'media': 10, 'other': 398, 'retail': 49, 'technology': 105, 'telecom_transport': 38}, 'prior records populated': 425}
- organisation size across name variants: {'organisations stored under several name variants': 6, 'organisations whose size changed against the representative-only rule': 1, 'organisations whose variants give conflicting known size bands': 0, 'deciding rule': {'higher size_confidence': 5, 'known band over UNKNOWN': 1}}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 39, 'events with an unresolved attribution': 38, 'organisations with an unresolved attribution': 38, 'victim-event attributions analysed': 1063, 'repeats': {'included': {'observed repeats': 157, 'entities with a repeat': 105, 'repeats by elapsed band': {'0-90': 24, '91-180': 27, '181-365': 36, '366-730': 25, '731-1460': 33, '>1460': 12}}, 'excluded': {'observed repeats': 142, 'entities with a repeat': 98, 'repeats by elapsed band': {'0-90': 21, '91-180': 23, '181-365': 33, '366-730': 23, '731-1460': 29, '>1460': 13}}, 'repeats involving an unresolved attribution': 18}}

## Same-Date Ties And Unknown Ordering

A same-date tie is far more likely one incident that deduplication has not joined than two incidents on one day. Every such pair, and every pair whose possible periods overlap, is an exception listed with the reason it is still two events in `dashboard/close_pairs.html`; the merging steps are fixed when one is a missed duplicate. Until a tie is resolved, each tie policy makes one stated assumption about it (every tie, with its incident ids and the length of any verified interval its incidents share, is in `entity_date_ties.csv`):

- `collapse` (primary): the tied incidents are one occurrence: one time point, never a repeat between them (their ids and count are kept).
- `zero_gap`: the tied incidents are distinct and one day apart, in id order: each after the first is a repeat after 1 day.
- `interval_spread`: the tied incidents are distinct and evenly spread across the verified interval they share (about interval length / n days apart, the first at the interval's start); a tie with no shared verified interval longer than a day is placed as under zero_gap.

None of these bounds the truth: a tie that is one incident filed twice is represented only by `collapse`; two incidents really weeks apart within a verified month are represented exactly by none; and a tie whose dates are unverified has no interval to place its incidents in, so `interval_spread` places it as `zero_gap` does. Agreement across the policies shows only that a result does not hinge on these assumptions. Every policy is adjusted for organisation size like the primary model.

- Under `collapse`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.033). It rests on 157 observed repeats.
- Under `zero_gap`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.033). It rests on 157 observed repeats.
- Under `interval_spread`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.033). It rests on 157 observed repeats.

| tie_policy      |   observed_repeats |   repeats_from_ties |   post_event_spells |   repeats_0-90 |   repeats_91-180 |   repeats_181-365 |   repeats_366-730 |   repeats_731-1460 |   repeats_>1460 |   adjusted_constant_hazard_p |   adjusted_0_90_91_180_banding_p |   delayed_peak_directional_p |
|:----------------|-------------------:|--------------------:|--------------------:|---------------:|-----------------:|------------------:|------------------:|-------------------:|----------------:|-----------------------------:|---------------------------------:|-----------------------------:|
| collapse        |                157 |                   0 |                1063 |             24 |               27 |                36 |                25 |                 33 |              12 |                      0.03337 |                          0.03337 |                       0.2311 |
| zero_gap        |                157 |                   0 |                1063 |             24 |               27 |                36 |                25 |                 33 |              12 |                      0.03337 |                          0.03337 |                       0.2311 |
| interval_spread |                157 |                   0 |                1063 |             24 |               27 |                36 |                25 |                 33 |              12 |                      0.03337 |                          0.03337 |                       0.2311 |


## Organisation Size Specification

The primary model enters size as a numeric score (SMALL=1 to HUGE=4, UNKNOWN mean-imputed, plus an UNKNOWN indicator), which assumes equal log-rate steps between bands. It is compared here with size as a categorical factor (reference: the band with the most repeats; bands with fewer than 3 repeats pooled) and, where every delayed-peak phase has at least 10 repeats at two or more known size bands, with the score's interaction with the delayed-peak phases. The score stays primary; the headline tests are re-run under categorical size. Full table: `size_specification_comparison.csv`.

- Size specification (bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.204 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 1222.3 (categorical) against 1221.5 (score), lower for the score model.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.033 with the size score and p = 0.037 with categorical size: the same verdict at the 5% level.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the directional delayed-peak test gives p = 0.231 with the size score and p = 0.230 with categorical size: the same verdict at the 5% level.
- Size by elapsed-time phase (the size score's slope in 91-180 and post-180 days against 0-90): LRT p = 0.034 on 2 df; at the 5% level the data reject a size effect that is the same in every phase.

| banding   | size_spec     |   log_likelihood |   parameters |   aic |   delta_aic_vs_score | nested   |   likelihood_ratio |   df |   p_value |   elapsed_constant_hazard_p |   delayed_peak_directional_p |
|:----------|:--------------|-----------------:|-------------:|------:|---------------------:|:---------|-------------------:|-----:|----------:|----------------------------:|-----------------------------:|
| run       | score         |           -583.8 |           27 |  1222 |               0      |          |            nan     |  nan |  nan      |                     0.03337 |                       0.2311 |
| run       | categorical   |           -582.2 |           29 |  1222 |               0.8189 | True     |              3.181 |    2 |    0.2038 |                     0.03658 |                       0.2301 |
| run       | score_x_phase |           -580.4 |           29 |  1219 |              -2.786  | True     |              6.786 |    2 |    0.0336 |                   nan       |                     nan      |


## Primary Piecewise Exponential Test

The adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.033). It rests on 157 observed repeats.

- unadjusted LRT p-value: 0.001094
- adjusted LRT p-value: 0.03337
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |       24 |       91048.000 |                       9.628 |              5.843 |              13.381 |
| 91-180         |       27 |       81478.000 |                      12.104 |              7.580 |              17.487 |
| 181-365        |       36 |      140306.000 |                       9.372 |              6.011 |              12.675 |
| 366-730        |       25 |      214701.000 |                       4.253 |              2.474 |               6.026 |
| 731-1460       |       33 |      207209.000 |                       5.817 |              3.811 |               7.887 |
| >1460          |       12 |       65418.000 |                       6.700 |              2.996 |              10.606 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                       |
|:----------------|-------------:|---------:|----------:|:-------------------------------------|
| SMALL -> MEDIUM |        2.337 |    1.825 |     2.994 | about 134% higher repeat-event rate  |
| SMALL -> LARGE  |        5.463 |    3.329 |     8.966 | about 446% higher repeat-event rate  |
| SMALL -> HUGE   |       12.770 |    6.074 |    26.846 | about 1177% higher repeat-event rate |
| MEDIUM -> HUGE  |        5.463 |    3.329 |     8.966 | about 446% higher repeat-event rate  |
| LARGE -> HUGE   |        2.337 |    1.825 |     2.994 | about 134% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector            |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:------------------|-------------:|---------:|----------:|:----------------------------------|
| education         |        2.355 |    1.081 |     5.131 | about 136% higher than government |
| telecom_transport |        1.615 |    0.659 |     3.953 | about 61% higher than government  |
| finance           |        1.520 |    0.621 |     3.724 | about 52% higher than government  |
| media             |        1.513 |    0.406 |     5.645 | about 51% higher than government  |
| health            |        1.299 |    0.535 |     3.155 | about 30% higher than government  |
| industrial        |        1.253 |    0.436 |     3.601 | about 25% higher than government  |
| technology        |        0.684 |    0.280 |     1.674 | about 32% lower than government   |
| other             |        0.526 |    0.240 |     1.155 | about 47% lower than government   |
| retail            |        0.421 |    0.113 |     1.568 | about 58% lower than government   |

Full adjusted covariate table:

| effect                           |   rate_ratio |   ci_low |                                                                                                                                                                                                                                                        ci_high |
|:---------------------------------|-------------:|---------:|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------:|
| Entity kind: government_body     |        1.144 |    0.549 |                                                                                                                                                                                                                                                          2.384 |
| Entity kind: sparse_or_no_repeat |        0.822 |    0.108 |                                                                                                                                                                                                                                                          6.229 |
| Organisation size                |        2.337 |    1.825 |                                                                                                                                                                                                                                                          2.994 |
| Prior records: 100k+             |        1.342 |    0.838 |                                                                                                                                                                                                                                                          2.150 |
| Prior records: 10k-100k          |        1.132 |    0.699 |                                                                                                                                                                                                                                                          1.834 |
| Prior records: 1k-10k            |        0.789 |    0.432 |                                                                                                                                                                                                                                                          1.439 |
| Prior records: <1k               |        1.299 |    0.754 |                                                                                                                                                                                                                                                          2.235 |
| Sector: education                |        2.355 |    1.081 |                                                                                                                                                                                                                                                          5.131 |
| Sector: finance                  |        1.520 |    0.621 |                                                                                                                                                                                                                                                          3.724 |
| Sector: health                   |        1.299 |    0.535 |                                                                                                                                                                                                                                                          3.155 |
| Sector: industrial               |        1.253 |    0.436 |                                                                                                                                                                                                                                                          3.601 |
| Sector: media                    |        1.513 |    0.406 |                                                                                                                                                                                                                                                          5.645 |
| Sector: other                    |        0.526 |    0.240 |                                                                                                                                                                                                                                                          1.155 |
| Sector: retail                   |        0.421 |    0.113 |                                                                                                                                                                                                                                                          1.568 |
| Sector: technology               |        0.684 |    0.280 |                                                                                                                                                                                                                                                          1.674 |
| Sector: telecom_transport        |        1.615 |    0.659 |                                                                                                                                                                                                                                                          3.953 |
| Unknown size                     |        0.000 |    0.000 | 3804965530522190170681562089920277903495031758915840160359693979793802977579279916999974251553175033874453985402526827046626999481451005483327267748391083997362067020552537821271086349440295675258215973689260804649833485321410627325401769744562913280.000 |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 91-180, 181-365 days vs 0-90 days (unadjusted): rate ratio 1.08 (95% CI 0.67-1.72), no direction can be read from it; two-sided p = 0.755. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.67 to 1.72, which includes 1. Repeat events: 63 in 91-180, 181-365 days and 24 in 0-90 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.999. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates do not have the hypothesised shape: response vs immediate, long-term vs response go the other way.

- response vs immediate: rate ratio 1.13 (95% CI 0.71-1.82), no direction can be read from it; one-sided p = 0.699 (alternative: lower). Not statistically significant at the 5% level: the data do not reject the null of no decrease (rate ratio at least 1); the 95% CI runs from 0.71 to 1.82, which includes 1. Repeat events: 62 in the response period and 24 in the immediate period.
- long-term vs response: rate ratio 0.57 (95% CI 0.40-0.82), no direction can be read from it; one-sided p = 0.999 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.40 to 0.82. The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 70 in the long-term period and 62 in the response period.
- long-term vs immediate: rate ratio 0.65 (95% CI 0.40-1.05), no direction can be read from it; two-sided p = 0.078. Not statistically significant at the 5% level (borderline): the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.40 to 1.05, which includes 1. Repeat events: 70 in the long-term period and 24 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.00802
- directional U-shape p-value: 0.9989

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       24 |           91048 |                       9.628 |
| Response period  |       62 |          221784 |                      10.211 |
| Long-term period |       70 |          487328 |                       5.246 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        1.134 |    0.707 |     1.819 | about 13% higher |         0.603 | less                      | 0.699           |
| long-term vs response  |        0.571 |    0.399 |     0.818 | about 43% lower  |         0.002 | greater                   | 0.999           |
| long-term vs immediate |        0.648 |    0.400 |     1.050 | about 35% lower  |         0.078 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        1.134 |    0.707 |     1.819 |      1.112 | about 13% higher |           0.699 |
| All covariates                | long-term vs response |        0.571 |    0.399 |     0.818 |      0.418 | about 43% lower  |           0.999 |
| Drop organisation size        | response vs immediate |        1.154 |    0.719 |     1.851 |      1.132 | about 15% higher |           0.724 |
| Drop organisation size        | long-term vs response |        0.615 |    0.431 |     0.878 |      0.446 | about 38% lower  |           0.996 |
| Drop industry/sector          | response vs immediate |        1.097 |    0.685 |     1.759 |      1.075 | about 10% higher |           0.650 |
| Drop industry/sector          | long-term vs response |        0.542 |    0.380 |     0.773 |      0.393 | about 46% lower  |           1.000 |
| Drop size and industry/sector | response vs immediate |        1.120 |    0.699 |     1.796 |      1.097 | about 12% higher |           0.682 |
| Drop size and industry/sector | long-term vs response |        0.600 |    0.422 |     0.854 |      0.432 | about 40% lower  |           0.998 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.0080 |                         0.9989 |
| Drop organisation size        |    0.0262 |                         0.9963 |
| Drop industry/sector          |    0.0023 |                         0.9996 |
| Drop size and industry/sector |    0.0151 |                         0.9977 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.71-1.82). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.40-0.82).

## Delayed 91-180 Day Peak Test

Definition used here: the hypothesised peak is 91-180 days after the prior event. A formal peak requires 91-180 days to be higher than both 0-90 days and the pooled post-180 period.
The adjusted peak model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main model.

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.231. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates have the hypothesised shape. The weakest part is 91-180 vs 0-90 (p = 0.231).

- 91-180 vs 0-90: rate ratio 1.23 (95% CI 0.71-2.15), no direction can be read from it; one-sided p = 0.231 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.71 to 2.15, which includes 1. Repeat events: 26 in 91-180 days and 24 in 0-90 days.
- 91-180 vs post-180: rate ratio 1.60 (95% CI 1.03-2.49), about 60% higher; one-sided p = 0.018 (alternative: higher). Statistically significant at the 5% level: the data reject the null of no increase (rate ratio at most 1). Repeat events: 26 in 91-180 days and 106 in post-180 days.
- post-180 vs 0-90: rate ratio 0.77 (95% CI 0.49-1.21), no direction can be read from it; two-sided p = 0.261. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.49 to 1.21, which includes 1. Repeat events: 106 in post-180 days and 24 in 0-90 days.

- phase-model LRT p-value versus constant elapsed-time risk: 0.1045
- directional delayed-peak p-value: 0.2311

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |       24 |           91048 |                       9.628 |
| Delayed peak 91-180 days |       26 |           81478 |                      11.655 |
| Post-180 days            |      106 |          627634 |                       6.169 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        1.231 |    0.707 |     2.145 | about 23% higher |         0.462 | greater                   | 0.231           |
| 91-180 vs post-180 |        1.601 |    1.031 |     2.485 | about 60% higher |         0.036 | greater                   | 0.018           |
| post-180 vs 0-90   |        0.769 |    0.487 |     1.215 | about 23% lower  |         0.261 |                           |                 |

## Unadjusted Parametric Survival Model Comparison (Descriptive)

Best AIC model: loglogistic. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| loglogistic       |        -1492.689 |            2 | 2989.379 | 2999.317 | True        |
| weibull           |        -1492.804 |            2 | 2989.608 | 2999.546 | True        |
| lognormal         |        -1492.906 |            2 | 2989.812 | 2999.750 | True        |
| generalized_gamma |        -1492.482 |            3 | 2990.964 | 3005.871 | True        |
| exponential       |        -1497.202 |            1 | 2996.405 | 3001.374 | True        |

Exponential vs Weibull LRT p-value: 0.003018

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
