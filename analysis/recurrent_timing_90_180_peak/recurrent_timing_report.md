# Repeat Cyber-Event Timing Analysis

## Executive Summary

**Is the data adequate for a reliable recurrence model?** Adequate for a simple model: 157 repeats, 20 per parameter of the models used here; but the full adjusted model is over-parameterised (27 parameters, 5.8 repeats each, 1 not identified), so it is not used for these conclusions; 51% of incident dates are unverified, so gaps can be resolved only to about a month: splits finer than 30 days are not attempted.

**What are the best splits for recurrence time bands?** Best bands: 0-270, >270 days, with rates 1.00, 0.55 times the first band's (strong evidence: 6.5 BIC better than a constant rate, holding size, prior event number and a calendar trend; search-adjusted p = 0.008). The long-term growth in recorded incidents does not explain it: the per-organisation rate changes by +0% a year (yearly effects from 2020, p = 0.64), and the bands hold under no calendar adjustment, a linear trend, monthly reporting intensity and yearly effects alike (p from 0.0002 to 0.0005).

**Is the rate lower 1-90 days after an event (short-term vigilance)?** No evidence of short-term vigilance: the adjusted rate 1-90 days after an event is not lower than at other times (one-sided p = 0.979; 27 repeats against 18.8 expected if the rate were no different).

**Is there a delayed peak in recurrence 91-180 days after an event?** Yes: the adjusted rate 91-180 days after an event is 1.60 times the rate at other times (95% CI 1.00-2.46; one-sided p = 0.026; 25 repeats). The narrower 91-120 day window holds only 7 repeats, too few to test on its own.

**What fits recurrence rates best?** Best fit by AIC, holding size, prior event number and a calendar trend: bands 0-270, >270. The constant rate is 10.7 AIC behind. The bands were chosen by the search, so that margin is optimistic.

**Does organisation size predict recurrence rates?** Yes: the recurrence rate rises with organisation size. Rate relative to SMALL organisations: MEDIUM 1.4, LARGE 3.6, HUGE 8.2 (monotone model holding prior event number and a calendar trend, bootstrap p <= 0.002 (no simulated value as extreme in 500)). Size stands for visibility and exposure as much as size: this is prediction, not cause.


## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.
The primary model includes recorded organisation size. Size is a proxy for differences in visibility, attack exposure and target value, not a direct measure of those factors; residual confounding remains possible. The ordinal size term assumes equal log-rate steps between categories (compared with categorical size and a size-by-elapsed-time interaction under Organisation Size Specification), UNKNOWN has a separate indicator, an organisation stored under several names takes the size its variants support best (`organisation_size_reconciliation.csv`), and current recorded size may differ from size at an incident's date. Unadjusted rates, plots, parametric models and size-dropped variants below are descriptive diagnostics and do not control for this confounding.

## Data Quality Summary

- victim event attributions: 1107
- unique deduplicated events: 1017
- victim entities: 948
- entities with more than one event: 107
- post-event spells: 1105
- observed repeat spells: 157
- right-censored spells: 948
- observation window (events before its start are excluded): {'observation start': '2020-01-01', 'victim event attributions before it (excluded)': 97, 'events before it (excluded)': 88, 'organisations with events only before it (excluded)': 28}
- censor date: 2026-11-03
- elapsed-time bands: 0-90, 91-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2020-01-01
- maximum event date: 2026-11-01
- first-of-month event dates: 248
- verified incident-date precision: {'unverified': 569, 'day': 416, 'month': 122}
- tie policy (several incidents of one organisation on one date): collapse
- same-date ties: {'ties (organisation-dates with several incidents)': 2, 'incidents in ties': 4, 'organisations with a tie': 2, 'repeats a policy that separates tied incidents adds': 2, 'ties with a shared verified interval longer than a day (spread by interval_spread)': 0, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 2, 'ties: other_day': 0, 'repeats between tied incidents in this run': 0}
- incidents not ordered by their dates (different stored dates, overlapping verified intervals): {'consecutive pairs with overlapping verified intervals': 4, '... of which stored dates differ by under 90 days': 4}
- covariate coverage: {'size_estimate levels': {'HUGE': 253, 'LARGE': 358, 'MEDIUM': 326, 'SMALL': 126, 'UNKNOWN': 44}, 'size_confidence populated': 1101, 'employee_count populated': 1029, 'turnover populated': 0, 'industry populated': 964, 'entity_kind populated': 1107, 'sector_proxy levels': {'education': 92, 'finance': 59, 'government': 214, 'health': 95, 'industrial': 32, 'media': 11, 'other': 409, 'retail': 50, 'technology': 104, 'telecom_transport': 41}, 'prior records populated': 448}
- organisation size across name variants: {'organisations stored under several name variants': 8, 'organisations whose size changed against the representative-only rule': 1, 'organisations whose variants give conflicting known size bands': 0, 'deciding rule': {'higher size_confidence': 6, 'representative variant': 1, 'known band over UNKNOWN': 1}}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 37, 'events with an unresolved attribution': 35, 'organisations with an unresolved attribution': 36, 'victim-event attributions analysed': 1107, 'repeats': {'included': {'observed repeats': 157, 'entities with a repeat': 106, 'repeats by elapsed band': {'0-90': 27, '91-180': 25, '181-365': 34, '366-730': 21, '731-1460': 36, '>1460': 14}}, 'excluded': {'observed repeats': 143, 'entities with a repeat': 99, 'repeats by elapsed band': {'0-90': 24, '91-180': 21, '181-365': 33, '366-730': 19, '731-1460': 31, '>1460': 15}}, 'repeats involving an unresolved attribution': 16}}

## Same-Date Ties And Unknown Ordering

A same-date tie is far more likely one incident that deduplication has not joined than two incidents on one day. Every such pair, and every pair whose possible periods overlap, is an exception listed with the reason it is still two events in `dashboard/close_pairs.html`; the merging steps are fixed when one is a missed duplicate. Until a tie is resolved, each tie policy makes one stated assumption about it (every tie, with its incident ids and the length of any verified interval its incidents share, is in `entity_date_ties.csv`):

- `collapse` (primary): the tied incidents are one occurrence: one time point, never a repeat between them (their ids and count are kept).
- `zero_gap`: the tied incidents are distinct and one day apart, in id order: each after the first is a repeat after 1 day.
- `interval_spread`: the tied incidents are distinct and evenly spread across the verified interval they share (about interval length / n days apart, the first at the interval's start); a tie with no shared verified interval longer than a day is placed as under zero_gap.

None of these bounds the truth: a tie that is one incident filed twice is represented only by `collapse`; two incidents really weeks apart within a verified month are represented exactly by none; and a tie whose dates are unverified has no interval to place its incidents in, so `interval_spread` places it as `zero_gap` does. Agreement across the policies shows only that a result does not hinge on these assumptions. Every policy is adjusted for organisation size like the primary model.

- Under `collapse`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.006). It rests on 157 observed repeats.
- Under `zero_gap`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.003). It rests on 159 observed repeats.
- Under `interval_spread`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.003). It rests on 159 observed repeats.

| tie_policy      |   observed_repeats |   repeats_from_ties |   post_event_spells |   repeats_0-90 |   repeats_91-180 |   repeats_181-365 |   repeats_366-730 |   repeats_731-1460 |   repeats_>1460 |   adjusted_constant_hazard_p |   adjusted_0_90_91_180_banding_p |   delayed_peak_directional_p |
|:----------------|-------------------:|--------------------:|--------------------:|---------------:|-----------------:|------------------:|------------------:|-------------------:|----------------:|-----------------------------:|---------------------------------:|-----------------------------:|
| collapse        |                157 |                   0 |                1105 |             27 |               25 |                34 |                21 |                 36 |              14 |                     0.006394 |                         0.006394 |                       0.4882 |
| zero_gap        |                159 |                   2 |                1107 |             29 |               25 |                34 |                21 |                 36 |              14 |                     0.003381 |                         0.003381 |                       0.5944 |
| interval_spread |                159 |                   2 |                1107 |             29 |               25 |                34 |                21 |                 36 |              14 |                     0.003381 |                         0.003381 |                       0.5944 |


## Organisation Size Specification

The primary model enters size as a numeric score (SMALL=1 to HUGE=4, UNKNOWN mean-imputed, plus an UNKNOWN indicator), which assumes equal log-rate steps between bands. It is compared here with size as a categorical factor (reference: the band with the most repeats; bands with fewer than 3 repeats pooled) and, where every delayed-peak phase has at least 10 repeats at two or more known size bands, with the score's interaction with the delayed-peak phases. The score stays primary; the headline tests are re-run under categorical size. Full table: `size_specification_comparison.csv`.

- Size specification (bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.344 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 1227.1 (categorical) against 1225.2 (score), lower for the score model.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.006 with the size score and p = 0.007 with categorical size: the same verdict at the 5% level.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the directional delayed-peak test gives p = 0.488 with the size score and p = 0.487 with categorical size: the same verdict at the 5% level.
- Size by elapsed-time phase (the size score's slope in 91-180 and post-180 days against 0-90): LRT p = 0.005 on 2 df; at the 5% level the data reject a size effect that is the same in every phase.

| banding   | size_spec     |   log_likelihood |   parameters |   aic |   delta_aic_vs_score | nested   |   likelihood_ratio |   df |    p_value |   elapsed_constant_hazard_p |   delayed_peak_directional_p |
|:----------|:--------------|-----------------:|-------------:|------:|---------------------:|:---------|-------------------:|-----:|-----------:|----------------------------:|-----------------------------:|
| run       | score         |           -585.6 |           27 |  1225 |                0     |          |            nan     |  nan | nan        |                    0.006394 |                       0.4882 |
| run       | categorical   |           -584.5 |           29 |  1227 |                1.865 | True     |              2.135 |    2 |   0.3438   |                    0.006924 |                       0.4869 |
| run       | score_x_phase |           -580.4 |           29 |  1219 |               -6.44  | True     |             10.44  |    2 |   0.005408 |                  nan        |                     nan      |


## Primary Piecewise Exponential Test

The adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.006). It rests on 157 observed repeats.

- unadjusted LRT p-value: 0.000222
- adjusted LRT p-value: 0.006394
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |       27 |       94206.000 |                      10.468 |              6.700 |              14.584 |
| 91-180         |       25 |       84239.000 |                      10.840 |              5.880 |              16.287 |
| 181-365        |       34 |      145497.000 |                       8.535 |              5.488 |              12.127 |
| 366-730        |       21 |      224171.000 |                       3.422 |              1.983 |               4.952 |
| 731-1460       |       36 |      218852.000 |                       6.008 |              4.166 |               8.359 |
| >1460          |       14 |       69830.000 |                       7.323 |              3.958 |              11.616 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        2.181 |    1.709 |     2.783 | about 118% higher repeat-event rate |
| SMALL -> LARGE  |        4.756 |    2.920 |     7.748 | about 376% higher repeat-event rate |
| SMALL -> HUGE   |       10.372 |    4.989 |    21.566 | about 937% higher repeat-event rate |
| MEDIUM -> HUGE  |        4.756 |    2.920 |     7.748 | about 376% higher repeat-event rate |
| LARGE -> HUGE   |        2.181 |    1.709 |     2.783 | about 118% higher repeat-event rate |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector            |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:------------------|-------------:|---------:|----------:|:----------------------------------|
| education         |        2.218 |    1.021 |     4.821 | about 122% higher than government |
| telecom_transport |        1.480 |    0.607 |     3.608 | about 48% higher than government  |
| media             |        1.277 |    0.342 |     4.765 | about 28% higher than government  |
| health            |        1.256 |    0.516 |     3.054 | about 26% higher than government  |
| finance           |        1.165 |    0.476 |     2.849 | about 16% higher than government  |
| industrial        |        1.136 |    0.394 |     3.271 | about 14% higher than government  |
| technology        |        0.707 |    0.288 |     1.734 | about 29% lower than government   |
| other             |        0.534 |    0.244 |     1.168 | about 47% lower than government   |
| retail            |        0.381 |    0.102 |     1.419 | about 62% lower than government   |

Full adjusted covariate table:

| effect                           |   rate_ratio |   ci_low |                                                                                                                                                              ci_high |
|:---------------------------------|-------------:|---------:|---------------------------------------------------------------------------------------------------------------------------------------------------------------------:|
| Entity kind: government_body     |        1.044 |    0.501 |                                                                                                                                                                2.174 |
| Entity kind: sparse_or_no_repeat |        0.804 |    0.106 |                                                                                                                                                                6.107 |
| Organisation size                |        2.181 |    1.709 |                                                                                                                                                                2.783 |
| Prior records: 100k+             |        1.256 |    0.785 |                                                                                                                                                                2.012 |
| Prior records: 10k-100k          |        1.104 |    0.682 |                                                                                                                                                                1.789 |
| Prior records: 1k-10k            |        0.800 |    0.438 |                                                                                                                                                                1.461 |
| Prior records: <1k               |        1.533 |    0.898 |                                                                                                                                                                2.617 |
| Sector: education                |        2.218 |    1.021 |                                                                                                                                                                4.821 |
| Sector: finance                  |        1.165 |    0.476 |                                                                                                                                                                2.849 |
| Sector: health                   |        1.256 |    0.516 |                                                                                                                                                                3.054 |
| Sector: industrial               |        1.136 |    0.394 |                                                                                                                                                                3.271 |
| Sector: media                    |        1.277 |    0.342 |                                                                                                                                                                4.765 |
| Sector: other                    |        0.534 |    0.244 |                                                                                                                                                                1.168 |
| Sector: retail                   |        0.381 |    0.102 |                                                                                                                                                                1.419 |
| Sector: technology               |        0.707 |    0.288 |                                                                                                                                                                1.734 |
| Sector: telecom_transport        |        1.480 |    0.607 |                                                                                                                                                                3.608 |
| Unknown size                     |        0.000 |    0.000 | 2095760157217117078040586030911977479352521106794564829628491663400785799032410279986713412222153042986046593352714324255273735586475853757954380538906232750080.000 |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 91-180, 181-365 days vs 0-90 days (unadjusted): rate ratio 0.90 (95% CI 0.57-1.41), no direction can be read from it; two-sided p = 0.637. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.57 to 1.41, which includes 1. Repeat events: 59 in 91-180, 181-365 days and 27 in 0-90 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.997. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates do not have the hypothesised shape: long-term vs response goes the other way.

- response vs immediate: rate ratio 0.94 (95% CI 0.59-1.48), no direction can be read from it; one-sided p = 0.388 (alternative: lower). Not statistically significant at the 5% level: the data do not reject the null of no decrease (rate ratio at least 1); the 95% CI runs from 0.59 to 1.48, which includes 1. Repeat events: 58 in the response period and 27 in the immediate period.
- long-term vs response: rate ratio 0.60 (95% CI 0.42-0.86), no direction can be read from it; one-sided p = 0.997 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.42 to 0.86. The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 71 in the long-term period and 58 in the response period.
- long-term vs immediate: rate ratio 0.56 (95% CI 0.35-0.89), about 44% lower; two-sided p = 0.014. Statistically significant at the 5% level: the data reject equal rates (rate ratio 1). Repeat events: 71 in the long-term period and 27 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.007817
- directional U-shape p-value: 0.9971

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       27 |           94206 |                      10.468 |
| Response period  |       58 |          229736 |                       9.221 |
| Long-term period |       71 |          512853 |                       5.057 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        0.936 |    0.592 |     1.479 | about 6% lower   |         0.776 | less                      | 0.388           |
| long-term vs response  |        0.600 |    0.417 |     0.862 | about 40% lower  |         0.006 | greater                   | 0.997           |
| long-term vs immediate |        0.561 |    0.354 |     0.892 | about 44% lower  |         0.014 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        0.936 |    0.592 |     1.479 |      0.888 | about 6% lower   |           0.388 |
| All covariates                | long-term vs response |        0.600 |    0.417 |     0.862 |      0.445 | about 40% lower  |           0.997 |
| Drop organisation size        | response vs immediate |        0.957 |    0.605 |     1.512 |      0.907 | about 4% lower   |           0.425 |
| Drop organisation size        | long-term vs response |        0.652 |    0.455 |     0.934 |      0.479 | about 35% lower  |           0.990 |
| Drop industry/sector          | response vs immediate |        0.906 |    0.574 |     1.432 |      0.858 | about 9% lower   |           0.336 |
| Drop industry/sector          | long-term vs response |        0.575 |    0.401 |     0.824 |      0.423 | about 43% lower  |           0.999 |
| Drop size and industry/sector | response vs immediate |        0.928 |    0.588 |     1.466 |      0.878 | about 7% lower   |           0.374 |
| Drop size and industry/sector | long-term vs response |        0.638 |    0.447 |     0.911 |      0.465 | about 36% lower  |           0.993 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.0078 |                         0.9971 |
| Drop organisation size        |    0.0320 |                         0.9902 |
| Drop industry/sector          |    0.0024 |                         0.9987 |
| Drop size and industry/sector |    0.0173 |                         0.9933 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.59-1.48). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.42-0.86).

## Delayed 91-180 Day Peak Test

Definition used here: the hypothesised peak is 91-180 days after the prior event. A formal peak requires 91-180 days to be higher than both 0-90 days and the pooled post-180 period.
The adjusted peak model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main model.

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.488. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates have the hypothesised shape. The weakest part is 91-180 vs 0-90 (p = 0.488).

- 91-180 vs 0-90: rate ratio 1.01 (95% CI 0.58-1.75), no direction can be read from it; one-sided p = 0.488 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.58 to 1.75, which includes 1. Repeat events: 24 in 91-180 days and 27 in 0-90 days.
- 91-180 vs post-180: rate ratio 1.54 (95% CI 0.98-2.43), about 54% higher; one-sided p = 0.031 (alternative: higher). Statistically significant at the 5% level: the data reject the null of no increase (rate ratio at most 1). The two-sided 95% CI still includes 1, so the evidence is one-sided only. Repeat events: 24 in 91-180 days and 105 in post-180 days.
- post-180 vs 0-90: rate ratio 0.65 (95% CI 0.42-1.01), no direction can be read from it; two-sided p = 0.056. Not statistically significant at the 5% level (borderline): the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.42 to 1.01, which includes 1. Repeat events: 105 in post-180 days and 27 in 0-90 days.

- phase-model LRT p-value versus constant elapsed-time risk: 0.06398
- directional delayed-peak p-value: 0.4882

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |       27 |           94206 |                      10.468 |
| Delayed peak 91-180 days |       24 |           84239 |                      10.406 |
| Post-180 days            |      105 |          658350 |                       5.825 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        1.008 |    0.582 |     1.748 | about 1% higher  |         0.976 | greater                   | 0.488           |
| 91-180 vs post-180 |        1.543 |    0.980 |     2.430 | about 54% higher |         0.061 | greater                   | 0.031           |
| post-180 vs 0-90   |        0.653 |    0.422 |     1.012 | about 35% lower  |         0.056 |                           |                 |

## Unadjusted Parametric Survival Model Comparison (Descriptive)

Best AIC model: weibull. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| weibull           |        -1499.372 |            2 | 3002.743 | 3012.758 | True        |
| loglogistic       |        -1499.861 |            2 | 3003.722 | 3013.737 | True        |
| generalized_gamma |        -1499.304 |            3 | 3004.609 | 3019.632 | True        |
| lognormal         |        -1501.386 |            2 | 3006.771 | 3016.786 | True        |
| exponential       |        -1504.231 |            1 | 3010.462 | 3015.469 | True        |

Exponential vs Weibull LRT p-value: 0.001824

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
