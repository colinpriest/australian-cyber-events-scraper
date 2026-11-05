# Repeat Cyber-Event Timing Analysis

## Executive Summary

**Is the data adequate for a reliable recurrence model?** Adequate for a simple model: 157 repeats, 20 per parameter of the models used here; but the full adjusted model is over-parameterised (27 parameters, 5.8 repeats each, 1 not identified), so it is not used for these conclusions; 51% of incident dates are unverified, so gaps can be resolved only to about a month: splits finer than 30 days are not attempted.

**What are the best splits for recurrence time bands?** Best bands: 0-270, >270 days, with rates 1.00, 0.54 times the first band's (strong evidence: 6.8 BIC better than a constant rate, holding size, prior event number and a calendar trend; search-adjusted p = 0.010). The long-term growth in recorded incidents does not explain it: the per-organisation rate changes by +1% a year (yearly effects from 2020, p = 0.65), and the bands hold under no calendar adjustment, a linear trend, monthly reporting intensity and yearly effects alike (p from 0.0002 to 0.0005).

**Is the rate lower 1-90 days after an event (short-term vigilance)?** No evidence of short-term vigilance: the adjusted rate 1-90 days after an event is not lower than at other times (one-sided p = 0.981; 27 repeats against 18.7 expected if the rate were no different).

**Is there a delayed peak in recurrence 91-180 days after an event?** Yes: the adjusted rate 91-180 days after an event is 1.61 times the rate at other times (95% CI 1.00-2.48; one-sided p = 0.024; 25 repeats). The narrower 91-120 day window holds only 7 repeats, too few to test on its own.

**What fits recurrence rates best?** Best fit by AIC, holding size, prior event number and a calendar trend: bands 0-270, >270. The constant rate is 11.0 AIC behind. The bands were chosen by the search, so that margin is optimistic.

**Does organisation size predict recurrence rates?** Yes: the recurrence rate rises with organisation size. Rate relative to SMALL organisations: MEDIUM 1.4, LARGE 3.6, HUGE 8.5 (monotone model holding prior event number and a calendar trend, bootstrap p <= 0.002 (no simulated value as extreme in 500)). Size stands for visibility and exposure as much as size: this is prediction, not cause.


## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.
The primary model includes recorded organisation size. Size is a proxy for differences in visibility, attack exposure and target value, not a direct measure of those factors; residual confounding remains possible. The ordinal size term assumes equal log-rate steps between categories (compared with categorical size and a size-by-elapsed-time interaction under Organisation Size Specification), UNKNOWN has a separate indicator, an organisation stored under several names takes the size its variants support best (`organisation_size_reconciliation.csv`), and current recorded size may differ from size at an incident's date. Unadjusted rates, plots, parametric models and size-dropped variants below are descriptive diagnostics and do not control for this confounding.

## Data Quality Summary

- victim event attributions: 1086
- unique deduplicated events: 1017
- victim entities: 927
- entities with more than one event: 107
- post-event spells: 1084
- observed repeat spells: 157
- right-censored spells: 927
- observation window (events before its start are excluded): {'observation start': '2020-01-01', 'victim event attributions before it (excluded)': 97, 'events before it (excluded)': 88, 'organisations with events only before it (excluded)': 28}
- censor date: 2026-11-03
- elapsed-time bands: 0-90, 91-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2020-01-01
- maximum event date: 2026-11-01
- first-of-month event dates: 247
- verified incident-date precision: {'unverified': 557, 'day': 407, 'month': 122}
- tie policy (several incidents of one organisation on one date): collapse
- same-date ties: {'ties (organisation-dates with several incidents)': 2, 'incidents in ties': 4, 'organisations with a tie': 2, 'repeats a policy that separates tied incidents adds': 2, 'ties with a shared verified interval longer than a day (spread by interval_spread)': 0, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 2, 'ties: other_day': 0, 'repeats between tied incidents in this run': 0}
- incidents not ordered by their dates (different stored dates, overlapping verified intervals): {'consecutive pairs with overlapping verified intervals': 4, '... of which stored dates differ by under 90 days': 4}
- covariate coverage: {'size_estimate levels': {'HUGE': 244, 'LARGE': 353, 'MEDIUM': 324, 'SMALL': 124, 'UNKNOWN': 41}, 'size_confidence populated': 1080, 'employee_count populated': 1011, 'turnover populated': 0, 'industry populated': 944, 'entity_kind populated': 1086, 'sector_proxy levels': {'education': 86, 'finance': 59, 'government': 206, 'health': 94, 'industrial': 31, 'media': 11, 'other': 406, 'retail': 49, 'technology': 104, 'telecom_transport': 40}, 'prior records populated': 438}
- organisation size across name variants: {'organisations stored under several name variants': 8, 'organisations whose size changed against the representative-only rule': 1, 'organisations whose variants give conflicting known size bands': 0, 'deciding rule': {'higher size_confidence': 6, 'representative variant': 1, 'known band over UNKNOWN': 1}}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 36, 'events with an unresolved attribution': 35, 'organisations with an unresolved attribution': 35, 'victim-event attributions analysed': 1086, 'repeats': {'included': {'observed repeats': 157, 'entities with a repeat': 106, 'repeats by elapsed band': {'0-90': 27, '91-180': 25, '181-365': 34, '366-730': 21, '731-1460': 36, '>1460': 14}}, 'excluded': {'observed repeats': 143, 'entities with a repeat': 99, 'repeats by elapsed band': {'0-90': 24, '91-180': 21, '181-365': 33, '366-730': 19, '731-1460': 31, '>1460': 15}}, 'repeats involving an unresolved attribution': 16}}

## Same-Date Ties And Unknown Ordering

A same-date tie is far more likely one incident that deduplication has not joined than two incidents on one day. Every such pair, and every pair whose possible periods overlap, is an exception listed with the reason it is still two events in `dashboard/close_pairs.html`; the merging steps are fixed when one is a missed duplicate. Until a tie is resolved, each tie policy makes one stated assumption about it (every tie, with its incident ids and the length of any verified interval its incidents share, is in `entity_date_ties.csv`):

- `collapse` (primary): the tied incidents are one occurrence: one time point, never a repeat between them (their ids and count are kept).
- `zero_gap`: the tied incidents are distinct and one day apart, in id order: each after the first is a repeat after 1 day.
- `interval_spread`: the tied incidents are distinct and evenly spread across the verified interval they share (about interval length / n days apart, the first at the interval's start); a tie with no shared verified interval longer than a day is placed as under zero_gap.

None of these bounds the truth: a tie that is one incident filed twice is represented only by `collapse`; two incidents really weeks apart within a verified month are represented exactly by none; and a tie whose dates are unverified has no interval to place its incidents in, so `interval_spread` places it as `zero_gap` does. Agreement across the policies shows only that a result does not hinge on these assumptions. Every policy is adjusted for organisation size like the primary model.

- Under `collapse`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.005). It rests on 157 observed repeats.
- Under `zero_gap`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.003). It rests on 159 observed repeats.
- Under `interval_spread`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.003). It rests on 159 observed repeats.

| tie_policy      |   observed_repeats |   repeats_from_ties |   post_event_spells |   repeats_0-90 |   repeats_91-180 |   repeats_181-365 |   repeats_366-730 |   repeats_731-1460 |   repeats_>1460 |   adjusted_constant_hazard_p |   adjusted_0_90_91_180_banding_p |   delayed_peak_directional_p |
|:----------------|-------------------:|--------------------:|--------------------:|---------------:|-----------------:|------------------:|------------------:|-------------------:|----------------:|-----------------------------:|---------------------------------:|-----------------------------:|
| collapse        |                157 |                   0 |                1084 |             27 |               25 |                34 |                21 |                 36 |              14 |                     0.005289 |                         0.005289 |                       0.4878 |
| zero_gap        |                159 |                   2 |                1086 |             29 |               25 |                34 |                21 |                 36 |              14 |                     0.002747 |                         0.002747 |                       0.5939 |
| interval_spread |                159 |                   2 |                1086 |             29 |               25 |                34 |                21 |                 36 |              14 |                     0.002747 |                         0.002747 |                       0.5939 |


## Organisation Size Specification

The primary model enters size as a numeric score (SMALL=1 to HUGE=4, UNKNOWN mean-imputed, plus an UNKNOWN indicator), which assumes equal log-rate steps between bands. It is compared here with size as a categorical factor (reference: the band with the most repeats; bands with fewer than 3 repeats pooled) and, where every delayed-peak phase has at least 10 repeats at two or more known size bands, with the score's interaction with the delayed-peak phases. The score stays primary; the headline tests are re-run under categorical size. Full table: `size_specification_comparison.csv`.

- Size specification (bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.286 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 1229.5 (categorical) against 1228.0 (score), lower for the score model.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.005 with the size score and p = 0.006 with categorical size: the same verdict at the 5% level.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the directional delayed-peak test gives p = 0.488 with the size score and p = 0.487 with categorical size: the same verdict at the 5% level.
- Size by elapsed-time phase (the size score's slope in 91-180 and post-180 days against 0-90): LRT p = 0.005 on 2 df; at the 5% level the data reject a size effect that is the same in every phase.

| banding   | size_spec     |   log_likelihood |   parameters |   aic |   delta_aic_vs_score | nested   |   likelihood_ratio |   df |    p_value |   elapsed_constant_hazard_p |   delayed_peak_directional_p |
|:----------|:--------------|-----------------:|-------------:|------:|---------------------:|:---------|-------------------:|-----:|-----------:|----------------------------:|-----------------------------:|
| run       | score         |           -587   |           27 |  1228 |                0     |          |            nan     |  nan | nan        |                    0.005289 |                       0.4878 |
| run       | categorical   |           -585.8 |           29 |  1230 |                1.498 | True     |              2.502 |    2 |   0.2863   |                    0.005653 |                       0.4866 |
| run       | score_x_phase |           -581.7 |           29 |  1221 |               -6.581 | True     |             10.58  |    2 |   0.005039 |                  nan        |                     nan      |


## Primary Piecewise Exponential Test

The adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.005). It rests on 157 observed repeats.

- unadjusted LRT p-value: 0.0002075
- adjusted LRT p-value: 0.005289
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |       27 |       92451.000 |                      10.667 |              7.152 |              14.811 |
| 91-180         |       25 |       82661.000 |                      11.047 |              6.259 |              16.411 |
| 181-365        |       34 |      142846.000 |                       8.694 |              5.772 |              12.549 |
| 366-730        |       21 |      220699.000 |                       3.475 |              2.003 |               5.033 |
| 731-1460       |       36 |      215269.000 |                       6.108 |              4.341 |               8.188 |
| >1460          |       14 |       68601.000 |                       7.454 |              3.549 |              12.164 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                       |
|:----------------|-------------:|---------:|----------:|:-------------------------------------|
| SMALL -> MEDIUM |        2.237 |    1.750 |     2.861 | about 124% higher repeat-event rate  |
| SMALL -> LARGE  |        5.006 |    3.062 |     8.184 | about 401% higher repeat-event rate  |
| SMALL -> HUGE   |       11.201 |    5.359 |    23.412 | about 1020% higher repeat-event rate |
| MEDIUM -> HUGE  |        5.006 |    3.062 |     8.184 | about 401% higher repeat-event rate  |
| LARGE -> HUGE   |        2.237 |    1.750 |     2.861 | about 124% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector            |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:------------------|-------------:|---------:|----------:|:----------------------------------|
| education         |        2.340 |    1.074 |     5.097 | about 134% higher than government |
| telecom_transport |        1.610 |    0.660 |     3.925 | about 61% higher than government  |
| media             |        1.282 |    0.343 |     4.787 | about 28% higher than government  |
| health            |        1.248 |    0.513 |     3.035 | about 25% higher than government  |
| finance           |        1.163 |    0.475 |     2.847 | about 16% higher than government  |
| industrial        |        1.153 |    0.400 |     3.323 | about 15% higher than government  |
| technology        |        0.690 |    0.281 |     1.693 | about 31% lower than government   |
| other             |        0.534 |    0.244 |     1.170 | about 47% lower than government   |
| retail            |        0.415 |    0.112 |     1.546 | about 58% lower than government   |

Full adjusted covariate table:

| effect                           |   rate_ratio |   ci_low |                                                                                                                                                                                                                                                                                        ci_high |
|:---------------------------------|-------------:|---------:|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------:|
| Entity kind: government_body     |        1.084 |    0.520 |                                                                                                                                                                                                                                                                                          2.260 |
| Entity kind: sparse_or_no_repeat |        0.812 |    0.107 |                                                                                                                                                                                                                                                                                          6.181 |
| Organisation size                |        2.237 |    1.750 |                                                                                                                                                                                                                                                                                          2.861 |
| Prior records: 100k+             |        1.282 |    0.802 |                                                                                                                                                                                                                                                                                          2.049 |
| Prior records: 10k-100k          |        1.129 |    0.697 |                                                                                                                                                                                                                                                                                          1.829 |
| Prior records: 1k-10k            |        0.786 |    0.431 |                                                                                                                                                                                                                                                                                          1.436 |
| Prior records: <1k               |        1.495 |    0.876 |                                                                                                                                                                                                                                                                                          2.551 |
| Sector: education                |        2.340 |    1.074 |                                                                                                                                                                                                                                                                                          5.097 |
| Sector: finance                  |        1.163 |    0.475 |                                                                                                                                                                                                                                                                                          2.847 |
| Sector: health                   |        1.248 |    0.513 |                                                                                                                                                                                                                                                                                          3.035 |
| Sector: industrial               |        1.153 |    0.400 |                                                                                                                                                                                                                                                                                          3.323 |
| Sector: media                    |        1.282 |    0.343 |                                                                                                                                                                                                                                                                                          4.787 |
| Sector: other                    |        0.534 |    0.244 |                                                                                                                                                                                                                                                                                          1.170 |
| Sector: retail                   |        0.415 |    0.112 |                                                                                                                                                                                                                                                                                          1.546 |
| Sector: technology               |        0.690 |    0.281 |                                                                                                                                                                                                                                                                                          1.693 |
| Sector: telecom_transport        |        1.610 |    0.660 |                                                                                                                                                                                                                                                                                          3.925 |
| Unknown size                     |        0.000 |    0.000 | 112087320669266893904621066512201489765136105338798088957993689130315995448601656508543288767142457495835828179572333214835759227969797181579836671772118504814937431942099714334467222178707229645292539816618236752061036074819784077150421801471468775252454544266271910257784763973632.000 |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 91-180, 181-365 days vs 0-90 days (unadjusted): rate ratio 0.90 (95% CI 0.57-1.41), no direction can be read from it; two-sided p = 0.636. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.57 to 1.41, which includes 1. Repeat events: 59 in 91-180, 181-365 days and 27 in 0-90 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.998. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates do not have the hypothesised shape: long-term vs response goes the other way.

- response vs immediate: rate ratio 0.94 (95% CI 0.59-1.48), no direction can be read from it; one-sided p = 0.387 (alternative: lower). Not statistically significant at the 5% level: the data do not reject the null of no decrease (rate ratio at least 1); the 95% CI runs from 0.59 to 1.48, which includes 1. Repeat events: 58 in the response period and 27 in the immediate period.
- long-term vs response: rate ratio 0.59 (95% CI 0.41-0.85), no direction can be read from it; one-sided p = 0.998 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.41 to 0.85. The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 71 in the long-term period and 58 in the response period.
- long-term vs immediate: rate ratio 0.55 (95% CI 0.35-0.88), about 45% lower; two-sided p = 0.012. Statistically significant at the 5% level: the data reject equal rates (rate ratio 1). Repeat events: 71 in the long-term period and 27 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.006303
- directional U-shape p-value: 0.9976

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       27 |           92451 |                      10.667 |
| Response period  |       58 |          225507 |                       9.394 |
| Long-term period |       71 |          504569 |                       5.140 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        0.935 |    0.591 |     1.479 | about 6% lower   |         0.774 | less                      | 0.387           |
| long-term vs response  |        0.592 |    0.412 |     0.852 | about 41% lower  |         0.005 | greater                   | 0.998           |
| long-term vs immediate |        0.554 |    0.348 |     0.880 | about 45% lower  |         0.012 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        0.935 |    0.591 |     1.479 |      0.887 | about 6% lower   |           0.387 |
| All covariates                | long-term vs response |        0.592 |    0.412 |     0.852 |      0.440 | about 41% lower  |           0.998 |
| Drop organisation size        | response vs immediate |        0.956 |    0.605 |     1.512 |      0.907 | about 4% lower   |           0.424 |
| Drop organisation size        | long-term vs response |        0.648 |    0.452 |     0.928 |      0.476 | about 35% lower  |           0.991 |
| Drop industry/sector          | response vs immediate |        0.905 |    0.573 |     1.430 |      0.857 | about 9% lower   |           0.334 |
| Drop industry/sector          | long-term vs response |        0.571 |    0.398 |     0.818 |      0.420 | about 43% lower  |           0.999 |
| Drop size and industry/sector | response vs immediate |        0.927 |    0.587 |     1.464 |      0.878 | about 7% lower   |           0.373 |
| Drop size and industry/sector | long-term vs response |        0.635 |    0.445 |     0.908 |      0.463 | about 36% lower  |           0.994 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.0063 |                         0.9976 |
| Drop organisation size        |    0.0291 |                         0.9910 |
| Drop industry/sector          |    0.0021 |                         0.9989 |
| Drop size and industry/sector |    0.0162 |                         0.9936 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.59-1.48). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.41-0.85).

## Delayed 91-180 Day Peak Test

Definition used here: the hypothesised peak is 91-180 days after the prior event. A formal peak requires 91-180 days to be higher than both 0-90 days and the pooled post-180 period.
The adjusted peak model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main model.

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.488. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates have the hypothesised shape. The weakest part is 91-180 vs 0-90 (p = 0.488).

- 91-180 vs 0-90: rate ratio 1.01 (95% CI 0.58-1.75), no direction can be read from it; one-sided p = 0.488 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.58 to 1.75, which includes 1. Repeat events: 24 in 91-180 days and 27 in 0-90 days.
- 91-180 vs post-180: rate ratio 1.56 (95% CI 0.99-2.45), about 56% higher; one-sided p = 0.028 (alternative: higher). Statistically significant at the 5% level: the data reject the null of no increase (rate ratio at most 1). The two-sided 95% CI still includes 1, so the evidence is one-sided only. Repeat events: 24 in 91-180 days and 105 in post-180 days.
- post-180 vs 0-90: rate ratio 0.65 (95% CI 0.42-1.00), no direction can be read from it; two-sided p = 0.052. Not statistically significant at the 5% level (borderline): the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.42 to 1.00, which includes 1. Repeat events: 105 in post-180 days and 27 in 0-90 days.

- phase-model LRT p-value versus constant elapsed-time risk: 0.05734
- directional delayed-peak p-value: 0.4878

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |       27 |           92451 |                      10.667 |
| Delayed peak 91-180 days |       24 |           82661 |                      10.605 |
| Post-180 days            |      105 |          647415 |                       5.924 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        1.009 |    0.582 |     1.748 | about 1% higher  |         0.976 | greater                   | 0.488           |
| 91-180 vs post-180 |        1.558 |    0.989 |     2.454 | about 56% higher |         0.056 | greater                   | 0.028           |
| post-180 vs 0-90   |        0.647 |    0.418 |     1.003 | about 35% lower  |         0.052 |                           |                 |

## Unadjusted Parametric Survival Model Comparison (Descriptive)

Best AIC model: weibull. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| weibull           |        -1496.637 |            2 | 2997.274 | 3007.250 | True        |
| loglogistic       |        -1497.135 |            2 | 2998.270 | 3008.247 | True        |
| generalized_gamma |        -1496.753 |            3 | 2999.507 | 3014.472 | True        |
| lognormal         |        -1498.655 |            2 | 3001.310 | 3011.287 | True        |
| exponential       |        -1501.531 |            1 | 3005.062 | 3010.050 | True        |

Exponential vs Weibull LRT p-value: 0.001756

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
