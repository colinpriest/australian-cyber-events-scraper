# Repeat Cyber-Event Timing Analysis

## Executive Summary

**Is the data adequate for a reliable recurrence model?** Adequate for a simple model: 157 repeats, 20 per parameter of the models used here; but the full adjusted model is over-parameterised (27 parameters, 5.8 repeats each, 1 not identified), so it is not used for these conclusions; 52% of incident dates are unverified, so gaps can be resolved only to about a month: splits finer than 30 days are not attempted.

**What are the best splits for recurrence time bands?** Best bands: 0-270, >270 days, with rates 1.00, 0.52 times the first band's (strong evidence: 8.1 BIC better than a constant rate, holding size, prior event number and a calendar trend; search-adjusted p = 0.006). The long-term growth in recorded incidents does not explain it: the per-organisation rate changes by -0% a year (yearly effects from 2020, p = 0.48), and the bands hold under no calendar adjustment, a linear trend, monthly reporting intensity and yearly effects alike (p from 0.0001 to 0.0003).

**Is the rate lower 1-90 days after an event (short-term vigilance)?** No evidence of short-term vigilance: the adjusted rate 1-90 days after an event is not lower than at other times (one-sided p = 0.978; 27 repeats against 18.9 expected if the rate were no different).

**Is there a delayed peak in recurrence 91-180 days after an event?** Yes: the adjusted rate 91-180 days after an event is 1.59 times the rate at other times (95% CI 0.99-2.44; one-sided p = 0.027; 25 repeats). The narrower 91-120 day window holds only 7 repeats, too few to test on its own.

**What fits recurrence rates best?** Best fit by AIC, holding size, prior event number and a calendar trend: bands 0-270, >270. The constant rate is 12.5 AIC behind. The bands were chosen by the search, so that margin is optimistic.

**Does organisation size predict recurrence rates?** Yes: the recurrence rate rises with organisation size. Rate relative to SMALL organisations: MEDIUM 1.4, LARGE 3.6, HUGE 8.8 (monotone model holding prior event number and a calendar trend, bootstrap p <= 0.002 (no simulated value as extreme in 500)). Size stands for visibility and exposure as much as size: this is prediction, not cause.


## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.
The primary model includes recorded organisation size. Size is a proxy for differences in visibility, attack exposure and target value, not a direct measure of those factors; residual confounding remains possible. The ordinal size term assumes equal log-rate steps between categories (compared with categorical size and a size-by-elapsed-time interaction under Organisation Size Specification), UNKNOWN has a separate indicator, an organisation stored under several names takes the size its variants support best (`organisation_size_reconciliation.csv`), and current recorded size may differ from size at an incident's date. Unadjusted rates, plots, parametric models and size-dropped variants below are descriptive diagnostics and do not control for this confounding.

## Data Quality Summary

- victim event attributions: 1069
- unique deduplicated events: 1013
- victim entities: 908
- entities with more than one event: 107
- post-event spells: 1065
- observed repeat spells: 157
- right-censored spells: 908
- observation window (events before its start are excluded): {'observation start': '2020-01-01', 'victim event attributions before it (excluded)': 94, 'events before it (excluded)': 88, 'organisations with events only before it (excluded)': 26}
- censor date: 2026-11-03
- elapsed-time bands: 0-90, 91-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2020-01-01
- maximum event date: 2026-11-01
- first-of-month event dates: 245
- verified incident-date precision: {'unverified': 553, 'day': 396, 'month': 120}
- tie policy (several incidents of one organisation on one date): collapse
- same-date ties: {'ties (organisation-dates with several incidents)': 4, 'incidents in ties': 8, 'organisations with a tie': 4, 'repeats a policy that separates tied incidents adds': 4, 'ties with a shared verified interval longer than a day (spread by interval_spread)': 0, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 2, 'ties: other_day': 2, 'repeats between tied incidents in this run': 0}
- incidents not ordered by their dates (different stored dates, overlapping verified intervals): {'consecutive pairs with overlapping verified intervals': 4, '... of which stored dates differ by under 90 days': 4}
- covariate coverage: {'size_estimate levels': {'HUGE': 240, 'LARGE': 350, 'MEDIUM': 321, 'SMALL': 123, 'UNKNOWN': 35}, 'size_confidence populated': 1063, 'employee_count populated': 1000, 'turnover populated': 0, 'industry populated': 931, 'entity_kind populated': 1069, 'sector_proxy levels': {'education': 87, 'finance': 58, 'government': 202, 'health': 90, 'industrial': 31, 'media': 10, 'other': 399, 'retail': 49, 'technology': 105, 'telecom_transport': 38}, 'prior records populated': 429}
- organisation size across name variants: {'organisations stored under several name variants': 6, 'organisations whose size changed against the representative-only rule': 1, 'organisations whose variants give conflicting known size bands': 0, 'deciding rule': {'higher size_confidence': 5, 'known band over UNKNOWN': 1}}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 39, 'events with an unresolved attribution': 38, 'organisations with an unresolved attribution': 38, 'victim-event attributions analysed': 1069, 'repeats': {'included': {'observed repeats': 157, 'entities with a repeat': 105, 'repeats by elapsed band': {'0-90': 27, '91-180': 25, '181-365': 35, '366-730': 21, '731-1460': 36, '>1460': 13}}, 'excluded': {'observed repeats': 142, 'entities with a repeat': 98, 'repeats by elapsed band': {'0-90': 24, '91-180': 21, '181-365': 32, '366-730': 20, '731-1460': 31, '>1460': 14}}, 'repeats involving an unresolved attribution': 18}}

## Same-Date Ties And Unknown Ordering

A same-date tie is far more likely one incident that deduplication has not joined than two incidents on one day. Every such pair, and every pair whose possible periods overlap, is an exception listed with the reason it is still two events in `dashboard/close_pairs.html`; the merging steps are fixed when one is a missed duplicate. Until a tie is resolved, each tie policy makes one stated assumption about it (every tie, with its incident ids and the length of any verified interval its incidents share, is in `entity_date_ties.csv`):

- `collapse` (primary): the tied incidents are one occurrence: one time point, never a repeat between them (their ids and count are kept).
- `zero_gap`: the tied incidents are distinct and one day apart, in id order: each after the first is a repeat after 1 day.
- `interval_spread`: the tied incidents are distinct and evenly spread across the verified interval they share (about interval length / n days apart, the first at the interval's start); a tie with no shared verified interval longer than a day is placed as under zero_gap.

None of these bounds the truth: a tie that is one incident filed twice is represented only by `collapse`; two incidents really weeks apart within a verified month are represented exactly by none; and a tie whose dates are unverified has no interval to place its incidents in, so `interval_spread` places it as `zero_gap` does. Agreement across the policies shows only that a result does not hinge on these assumptions. Every policy is adjusted for organisation size like the primary model.

- Under `collapse`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.006). It rests on 157 observed repeats.
- Under `zero_gap`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.002). It rests on 161 observed repeats.
- Under `interval_spread`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.002). It rests on 161 observed repeats.

| tie_policy      |   observed_repeats |   repeats_from_ties |   post_event_spells |   repeats_0-90 |   repeats_91-180 |   repeats_181-365 |   repeats_366-730 |   repeats_731-1460 |   repeats_>1460 |   adjusted_constant_hazard_p |   adjusted_0_90_91_180_banding_p |   delayed_peak_directional_p |
|:----------------|-------------------:|--------------------:|--------------------:|---------------:|-----------------:|------------------:|------------------:|-------------------:|----------------:|-----------------------------:|---------------------------------:|-----------------------------:|
| collapse        |                157 |                   0 |                1065 |             27 |               25 |                35 |                21 |                 36 |              13 |                     0.006414 |                         0.006414 |                       0.4873 |
| zero_gap        |                161 |                   4 |                1069 |             31 |               25 |                35 |                21 |                 36 |              13 |                     0.001588 |                         0.001588 |                       0.6871 |
| interval_spread |                161 |                   4 |                1069 |             31 |               25 |                35 |                21 |                 36 |              13 |                     0.001588 |                         0.001588 |                       0.6871 |


## Organisation Size Specification

The primary model enters size as a numeric score (SMALL=1 to HUGE=4, UNKNOWN mean-imputed, plus an UNKNOWN indicator), which assumes equal log-rate steps between bands. It is compared here with size as a categorical factor (reference: the band with the most repeats; bands with fewer than 3 repeats pooled) and, where every delayed-peak phase has at least 10 repeats at two or more known size bands, with the score's interaction with the delayed-peak phases. The score stays primary; the headline tests are re-run under categorical size. Full table: `size_specification_comparison.csv`.

- Size specification (bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.239 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 1227.6 (categorical) against 1226.5 (score), lower for the score model.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.006 with the size score and p = 0.007 with categorical size: the same verdict at the 5% level.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the directional delayed-peak test gives p = 0.487 with the size score and p = 0.486 with categorical size: the same verdict at the 5% level.
- Size by elapsed-time phase (the size score's slope in 91-180 and post-180 days against 0-90): LRT p = 0.003 on 2 df; at the 5% level the data reject a size effect that is the same in every phase.

| banding   | size_spec     |   log_likelihood |   parameters |   aic |   delta_aic_vs_score | nested   |   likelihood_ratio |   df |   p_value |   elapsed_constant_hazard_p |   delayed_peak_directional_p |
|:----------|:--------------|-----------------:|-------------:|------:|---------------------:|:---------|-------------------:|-----:|----------:|----------------------------:|-----------------------------:|
| run       | score         |           -586.2 |           27 |  1226 |                0     |          |            nan     |  nan | nan       |                    0.006414 |                       0.4873 |
| run       | categorical   |           -584.8 |           29 |  1228 |                1.137 | True     |              2.863 |    2 |   0.2389  |                    0.00687  |                       0.4858 |
| run       | score_x_phase |           -580.5 |           29 |  1219 |               -7.477 | True     |             11.48  |    2 |   0.00322 |                  nan        |                     nan      |


## Primary Piecewise Exponential Test

The adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.006). It rests on 157 observed repeats.

- unadjusted LRT p-value: 0.0001859
- adjusted LRT p-value: 0.006414
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |       27 |       91013.000 |                      10.836 |              6.904 |              15.165 |
| 91-180         |       25 |       81425.000 |                      11.214 |              6.334 |              16.381 |
| 181-365        |       35 |      140541.000 |                       9.096 |              5.726 |              12.959 |
| 366-730        |       21 |      216755.000 |                       3.539 |              1.989 |               5.160 |
| 731-1460       |       36 |      210677.000 |                       6.241 |              4.435 |               8.336 |
| >1460          |       13 |       67075.000 |                       7.079 |              3.357 |              11.759 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                       |
|:----------------|-------------:|---------:|----------:|:-------------------------------------|
| SMALL -> MEDIUM |        2.261 |    1.766 |     2.895 | about 126% higher repeat-event rate  |
| SMALL -> LARGE  |        5.112 |    3.118 |     8.381 | about 411% higher repeat-event rate  |
| SMALL -> HUGE   |       11.558 |    5.505 |    24.265 | about 1056% higher repeat-event rate |
| MEDIUM -> HUGE  |        5.112 |    3.118 |     8.381 | about 411% higher repeat-event rate  |
| LARGE -> HUGE   |        2.261 |    1.766 |     2.895 | about 126% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector            |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:------------------|-------------:|---------:|----------:|:----------------------------------|
| education         |        2.345 |    1.077 |     5.106 | about 135% higher than government |
| telecom_transport |        1.636 |    0.672 |     3.986 | about 64% higher than government  |
| media             |        1.492 |    0.400 |     5.565 | about 49% higher than government  |
| health            |        1.306 |    0.538 |     3.171 | about 31% higher than government  |
| industrial        |        1.190 |    0.414 |     3.419 | about 19% higher than government  |
| finance           |        1.155 |    0.471 |     2.830 | about 15% higher than government  |
| technology        |        0.696 |    0.285 |     1.702 | about 30% lower than government   |
| other             |        0.526 |    0.239 |     1.155 | about 47% lower than government   |
| retail            |        0.420 |    0.113 |     1.561 | about 58% lower than government   |

Full adjusted covariate table:

| effect                           |   rate_ratio |   ci_low |                                                                                                                                                                              ci_high |
|:---------------------------------|-------------:|---------:|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------:|
| Entity kind: government_body     |        1.143 |    0.549 |                                                                                                                                                                                2.381 |
| Entity kind: sparse_or_no_repeat |        0.809 |    0.107 |                                                                                                                                                                                6.133 |
| Organisation size                |        2.261 |    1.766 |                                                                                                                                                                                2.895 |
| Prior records: 100k+             |        1.307 |    0.816 |                                                                                                                                                                                2.094 |
| Prior records: 10k-100k          |        1.129 |    0.697 |                                                                                                                                                                                1.828 |
| Prior records: 1k-10k            |        0.781 |    0.427 |                                                                                                                                                                                1.425 |
| Prior records: <1k               |        1.422 |    0.834 |                                                                                                                                                                                2.422 |
| Sector: education                |        2.345 |    1.077 |                                                                                                                                                                                5.106 |
| Sector: finance                  |        1.155 |    0.471 |                                                                                                                                                                                2.830 |
| Sector: health                   |        1.306 |    0.538 |                                                                                                                                                                                3.171 |
| Sector: industrial               |        1.190 |    0.414 |                                                                                                                                                                                3.419 |
| Sector: media                    |        1.492 |    0.400 |                                                                                                                                                                                5.565 |
| Sector: other                    |        0.526 |    0.239 |                                                                                                                                                                                1.155 |
| Sector: retail                   |        0.420 |    0.113 |                                                                                                                                                                                1.561 |
| Sector: technology               |        0.696 |    0.285 |                                                                                                                                                                                1.702 |
| Sector: telecom_transport        |        1.636 |    0.672 |                                                                                                                                                                                3.986 |
| Unknown size                     |        0.000 |    0.000 | 26766377081100671601549937331810376655862121278158131102596461797859217677092425883859666260234528873412095515347454134698702957930487415479858403404271246223334412041850978304.000 |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 91-180, 181-365 days vs 0-90 days (unadjusted): rate ratio 0.91 (95% CI 0.58-1.44), no direction can be read from it; two-sided p = 0.688. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.58 to 1.44, which includes 1. Repeat events: 60 in 91-180, 181-365 days and 27 in 0-90 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.998. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates do not have the hypothesised shape: long-term vs response goes the other way.

- response vs immediate: rate ratio 0.95 (95% CI 0.60-1.51), no direction can be read from it; one-sided p = 0.421 (alternative: lower). Not statistically significant at the 5% level: the data do not reject the null of no decrease (rate ratio at least 1); the 95% CI runs from 0.60 to 1.51, which includes 1. Repeat events: 59 in the response period and 27 in the immediate period.
- long-term vs response: rate ratio 0.59 (95% CI 0.41-0.84), no direction can be read from it; one-sided p = 0.998 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.41 to 0.84. The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 70 in the long-term period and 59 in the response period.
- long-term vs immediate: rate ratio 0.56 (95% CI 0.35-0.89), about 44% lower; two-sided p = 0.014. Statistically significant at the 5% level: the data reject equal rates (rate ratio 1). Repeat events: 70 in the long-term period and 27 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.006001
- directional U-shape p-value: 0.998

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       27 |           91013 |                      10.836 |
| Response period  |       59 |          221966 |                       9.709 |
| Long-term period |       70 |          494507 |                       5.170 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        0.954 |    0.604 |     1.507 | about 5% lower   |         0.842 | less                      | 0.421           |
| long-term vs response  |        0.585 |    0.407 |     0.842 | about 41% lower  |         0.004 | greater                   | 0.998           |
| long-term vs immediate |        0.559 |    0.351 |     0.889 | about 44% lower  |         0.014 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        0.954 |    0.604 |     1.507 |      0.903 | about 5% lower   |           0.421 |
| All covariates                | long-term vs response |        0.585 |    0.407 |     0.842 |      0.436 | about 41% lower  |           0.998 |
| Drop organisation size        | response vs immediate |        0.974 |    0.617 |     1.538 |      0.921 | about 3% lower   |           0.455 |
| Drop organisation size        | long-term vs response |        0.640 |    0.447 |     0.917 |      0.471 | about 36% lower  |           0.992 |
| Drop industry/sector          | response vs immediate |        0.925 |    0.586 |     1.459 |      0.873 | about 8% lower   |           0.368 |
| Drop industry/sector          | long-term vs response |        0.561 |    0.391 |     0.805 |      0.414 | about 44% lower  |           0.999 |
| Drop size and industry/sector | response vs immediate |        0.945 |    0.599 |     1.492 |      0.892 | about 5% lower   |           0.405 |
| Drop size and industry/sector | long-term vs response |        0.625 |    0.438 |     0.894 |      0.457 | about 37% lower  |           0.995 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.0060 |                         0.9980 |
| Drop organisation size        |    0.0272 |                         0.9924 |
| Drop industry/sector          |    0.0019 |                         0.9991 |
| Drop size and industry/sector |    0.0145 |                         0.9950 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.60-1.51). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.41-0.84).

## Delayed 91-180 Day Peak Test

Definition used here: the hypothesised peak is 91-180 days after the prior event. A formal peak requires 91-180 days to be higher than both 0-90 days and the pooled post-180 period.
The adjusted peak model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main model.

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.487. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates have the hypothesised shape. The weakest part is 91-180 vs 0-90 (p = 0.487).

- 91-180 vs 0-90: rate ratio 1.01 (95% CI 0.58-1.75), no direction can be read from it; one-sided p = 0.487 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.58 to 1.75, which includes 1. Repeat events: 24 in 91-180 days and 27 in 0-90 days.
- 91-180 vs post-180: rate ratio 1.52 (95% CI 0.97-2.40), about 52% higher; one-sided p = 0.035 (alternative: higher). Statistically significant at the 5% level: the data reject the null of no increase (rate ratio at most 1). The two-sided 95% CI still includes 1, so the evidence is one-sided only. Repeat events: 24 in 91-180 days and 105 in post-180 days.
- post-180 vs 0-90: rate ratio 0.66 (95% CI 0.43-1.03), no direction can be read from it; two-sided p = 0.065. Not statistically significant at the 5% level (borderline): the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.43 to 1.03, which includes 1. Repeat events: 105 in post-180 days and 27 in 0-90 days.

- phase-model LRT p-value versus constant elapsed-time risk: 0.07619
- directional delayed-peak p-value: 0.4873

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |       27 |           91013 |                      10.836 |
| Delayed peak 91-180 days |       24 |           81425 |                      10.766 |
| Post-180 days            |      105 |          635048 |                       6.039 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        1.009 |    0.582 |     1.749 | about 1% higher  |         0.975 | greater                   | 0.487           |
| 91-180 vs post-180 |        1.524 |    0.967 |     2.402 | about 52% higher |         0.070 | greater                   | 0.035           |
| post-180 vs 0-90   |        0.662 |    0.427 |     1.026 | about 34% lower  |         0.065 |                           |                 |

## Unadjusted Parametric Survival Model Comparison (Descriptive)

Best AIC model: weibull. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| weibull           |        -1493.297 |            2 | 2990.593 | 3000.535 | True        |
| loglogistic       |        -1493.639 |            2 | 2991.278 | 3001.219 | True        |
| generalized_gamma |        -1493.290 |            3 | 2992.580 | 3007.492 | True        |
| lognormal         |        -1494.767 |            2 | 2993.533 | 3003.475 | True        |
| exponential       |        -1498.633 |            1 | 2999.267 | 3004.237 | True        |

Exponential vs Weibull LRT p-value: 0.001087

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
