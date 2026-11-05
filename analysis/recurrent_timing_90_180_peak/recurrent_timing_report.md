# Repeat Cyber-Event Timing Analysis

## Executive Summary

**Is the data adequate for a reliable recurrence model?** Adequate for a simple model: 158 repeats, 20 per parameter of the models used here; but the full adjusted model is over-parameterised (27 parameters, 5.9 repeats each, 1 not identified), so it is not used for these conclusions; 52% of incident dates are unverified, so gaps can be resolved only to about a month: splits finer than 30 days are not attempted.

**What are the best splits for recurrence time bands?** Best bands: 0-270, >270 days, with rates 1.00, 0.52 times the first band's (strong evidence: 8.9 BIC better than a constant rate, holding size, prior event number and a calendar trend; search-adjusted p = 0.006). The long-term growth in recorded incidents does not explain it: the per-organisation rate changes by -0% a year (yearly effects from 2020, p = 0.48), and the bands hold under no calendar adjustment, a linear trend, monthly reporting intensity and yearly effects alike (p from 0.0000 to 0.0002).

**Is the rate lower 1-90 days after an event (short-term vigilance)?** No evidence of short-term vigilance: the adjusted rate 1-90 days after an event is not lower than at other times (one-sided p = 0.986; 28 repeats against 19.0 expected if the rate were no different).

**Is there a delayed peak in recurrence 91-180 days after an event?** Yes: the adjusted rate 91-180 days after an event is 1.58 times the rate at other times (95% CI 0.98-2.43; one-sided p = 0.029; 25 repeats). The narrower 91-120 day window holds only 8 repeats, too few to test on its own.

**What fits recurrence rates best?** Best fit by AIC, holding size, prior event number and a calendar trend: bands 0-270, >270. The constant rate is 13.3 AIC behind. The bands were chosen by the search, so that margin is optimistic.

**Does organisation size predict recurrence rates?** Yes: the recurrence rate rises with organisation size. Rate relative to SMALL organisations: MEDIUM 1.4, LARGE 3.6, HUGE 8.7 (monotone model holding prior event number and a calendar trend, bootstrap p <= 0.002 (no simulated value as extreme in 500)). Size stands for visibility and exposure as much as size: this is prediction, not cause.


## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.
The primary model includes recorded organisation size. Size is a proxy for differences in visibility, attack exposure and target value, not a direct measure of those factors; residual confounding remains possible. The ordinal size term assumes equal log-rate steps between categories (compared with categorical size and a size-by-elapsed-time interaction under Organisation Size Specification), UNKNOWN has a separate indicator, an organisation stored under several names takes the size its variants support best (`organisation_size_reconciliation.csv`), and current recorded size may differ from size at an incident's date. Unadjusted rates, plots, parametric models and size-dropped variants below are descriptive diagnostics and do not control for this confounding.

## Data Quality Summary

- victim event attributions: 1068
- unique deduplicated events: 1012
- victim entities: 907
- entities with more than one event: 107
- post-event spells: 1065
- observed repeat spells: 158
- right-censored spells: 907
- observation window (events before its start are excluded): {'observation start': '2020-01-01', 'victim event attributions before it (excluded)': 94, 'events before it (excluded)': 88, 'organisations with events only before it (excluded)': 26}
- censor date: 2026-11-03
- elapsed-time bands: 0-90, 91-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2020-01-01
- maximum event date: 2026-11-01
- first-of-month event dates: 246
- verified incident-date precision: {'unverified': 553, 'day': 394, 'month': 121}
- tie policy (several incidents of one organisation on one date): collapse
- same-date ties: {'ties (organisation-dates with several incidents)': 3, 'incidents in ties': 6, 'organisations with a tie': 3, 'repeats a policy that separates tied incidents adds': 3, 'ties with a shared verified interval longer than a day (spread by interval_spread)': 0, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 2, 'ties: other_day': 1, 'repeats between tied incidents in this run': 0}
- incidents not ordered by their dates (different stored dates, overlapping verified intervals): {'consecutive pairs with overlapping verified intervals': 4, '... of which stored dates differ by under 90 days': 4}
- covariate coverage: {'size_estimate levels': {'HUGE': 240, 'LARGE': 349, 'MEDIUM': 321, 'SMALL': 123, 'UNKNOWN': 35}, 'size_confidence populated': 1062, 'employee_count populated': 999, 'turnover populated': 0, 'industry populated': 930, 'entity_kind populated': 1068, 'sector_proxy levels': {'education': 87, 'finance': 57, 'government': 202, 'health': 90, 'industrial': 31, 'media': 10, 'other': 399, 'retail': 49, 'technology': 105, 'telecom_transport': 38}, 'prior records populated': 428}
- organisation size across name variants: {'organisations stored under several name variants': 6, 'organisations whose size changed against the representative-only rule': 1, 'organisations whose variants give conflicting known size bands': 0, 'deciding rule': {'higher size_confidence': 5, 'known band over UNKNOWN': 1}}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 39, 'events with an unresolved attribution': 38, 'organisations with an unresolved attribution': 38, 'victim-event attributions analysed': 1068, 'repeats': {'included': {'observed repeats': 158, 'entities with a repeat': 105, 'repeats by elapsed band': {'0-90': 28, '91-180': 25, '181-365': 35, '366-730': 21, '731-1460': 36, '>1460': 13}}, 'excluded': {'observed repeats': 143, 'entities with a repeat': 98, 'repeats by elapsed band': {'0-90': 25, '91-180': 21, '181-365': 32, '366-730': 20, '731-1460': 31, '>1460': 14}}, 'repeats involving an unresolved attribution': 18}}

## Same-Date Ties And Unknown Ordering

A same-date tie is far more likely one incident that deduplication has not joined than two incidents on one day. Every such pair, and every pair whose possible periods overlap, is an exception listed with the reason it is still two events in `dashboard/close_pairs.html`; the merging steps are fixed when one is a missed duplicate. Until a tie is resolved, each tie policy makes one stated assumption about it (every tie, with its incident ids and the length of any verified interval its incidents share, is in `entity_date_ties.csv`):

- `collapse` (primary): the tied incidents are one occurrence: one time point, never a repeat between them (their ids and count are kept).
- `zero_gap`: the tied incidents are distinct and one day apart, in id order: each after the first is a repeat after 1 day.
- `interval_spread`: the tied incidents are distinct and evenly spread across the verified interval they share (about interval length / n days apart, the first at the interval's start); a tie with no shared verified interval longer than a day is placed as under zero_gap.

None of these bounds the truth: a tie that is one incident filed twice is represented only by `collapse`; two incidents really weeks apart within a verified month are represented exactly by none; and a tie whose dates are unverified has no interval to place its incidents in, so `interval_spread` places it as `zero_gap` does. Agreement across the policies shows only that a result does not hinge on these assumptions. Every policy is adjusted for organisation size like the primary model.

- Under `collapse`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.005). It rests on 158 observed repeats.
- Under `zero_gap`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.002). It rests on 161 observed repeats.
- Under `interval_spread`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.002). It rests on 161 observed repeats.

| tie_policy      |   observed_repeats |   repeats_from_ties |   post_event_spells |   repeats_0-90 |   repeats_91-180 |   repeats_181-365 |   repeats_366-730 |   repeats_731-1460 |   repeats_>1460 |   adjusted_constant_hazard_p |   adjusted_0_90_91_180_banding_p |   delayed_peak_directional_p |
|:----------------|-------------------:|--------------------:|--------------------:|---------------:|-----------------:|------------------:|------------------:|-------------------:|----------------:|-----------------------------:|---------------------------------:|-----------------------------:|
| collapse        |                158 |                   0 |                1065 |             28 |               25 |                35 |                21 |                 36 |              13 |                     0.004566 |                         0.004566 |                       0.5399 |
| zero_gap        |                161 |                   3 |                1068 |             31 |               25 |                35 |                21 |                 36 |              13 |                     0.001538 |                         0.001538 |                       0.6876 |
| interval_spread |                161 |                   3 |                1068 |             31 |               25 |                35 |                21 |                 36 |              13 |                     0.001538 |                         0.001538 |                       0.6876 |


## Organisation Size Specification

The primary model enters size as a numeric score (SMALL=1 to HUGE=4, UNKNOWN mean-imputed, plus an UNKNOWN indicator), which assumes equal log-rate steps between bands. It is compared here with size as a categorical factor (reference: the band with the most repeats; bands with fewer than 3 repeats pooled) and, where every delayed-peak phase has at least 10 repeats at two or more known size bands, with the score's interaction with the delayed-peak phases. The score stays primary; the headline tests are re-run under categorical size. Full table: `size_specification_comparison.csv`.

- Size specification (bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.295 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 1240.3 (categorical) against 1238.7 (score), lower for the score model.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.005 with the size score and p = 0.005 with categorical size: the same verdict at the 5% level.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the directional delayed-peak test gives p = 0.540 with the size score and p = 0.539 with categorical size: the same verdict at the 5% level.
- Size by elapsed-time phase (the size score's slope in 91-180 and post-180 days against 0-90): LRT p = 0.003 on 2 df; at the 5% level the data reject a size effect that is the same in every phase.

| banding   | size_spec     |   log_likelihood |   parameters |   aic |   delta_aic_vs_score | nested   |   likelihood_ratio |   df |    p_value |   elapsed_constant_hazard_p |   delayed_peak_directional_p |
|:----------|:--------------|-----------------:|-------------:|------:|---------------------:|:---------|-------------------:|-----:|-----------:|----------------------------:|-----------------------------:|
| run       | score         |           -592.4 |           27 |  1239 |                0     |          |             nan    |  nan | nan        |                    0.004566 |                       0.5399 |
| run       | categorical   |           -591.2 |           29 |  1240 |                1.56  | True     |               2.44 |    2 |   0.2953   |                    0.004915 |                       0.5387 |
| run       | score_x_phase |           -586.5 |           29 |  1231 |               -7.744 | True     |              11.74 |    2 |   0.002818 |                  nan        |                     nan      |


## Primary Piecewise Exponential Test

The adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.005). It rests on 158 observed repeats.

- unadjusted LRT p-value: 0.0001276
- adjusted LRT p-value: 0.004566
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |       28 |       90937.000 |                      11.246 |              7.599 |              15.995 |
| 91-180         |       25 |       81321.000 |                      11.229 |              6.626 |              16.410 |
| 181-365        |       35 |      140356.000 |                       9.108 |              5.905 |              12.532 |
| 366-730        |       21 |      216390.000 |                       3.545 |              2.057 |               5.280 |
| 731-1460       |       36 |      210343.000 |                       6.251 |              4.368 |               8.378 |
| >1460          |       13 |       67075.000 |                       7.079 |              3.227 |              11.079 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                       |
|:----------------|-------------:|---------:|----------:|:-------------------------------------|
| SMALL -> MEDIUM |        2.248 |    1.758 |     2.873 | about 125% higher repeat-event rate  |
| SMALL -> LARGE  |        5.052 |    3.091 |     8.256 | about 405% higher repeat-event rate  |
| SMALL -> HUGE   |       11.354 |    5.433 |    23.724 | about 1035% higher repeat-event rate |
| MEDIUM -> HUGE  |        5.052 |    3.091 |     8.256 | about 405% higher repeat-event rate  |
| LARGE -> HUGE   |        2.248 |    1.758 |     2.873 | about 125% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector            |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:------------------|-------------:|---------:|----------:|:----------------------------------|
| education         |        2.302 |    1.057 |     5.012 | about 130% higher than government |
| telecom_transport |        1.610 |    0.661 |     3.920 | about 61% higher than government  |
| media             |        1.491 |    0.400 |     5.557 | about 49% higher than government  |
| health            |        1.299 |    0.535 |     3.155 | about 30% higher than government  |
| industrial        |        1.198 |    0.417 |     3.442 | about 20% higher than government  |
| finance           |        1.166 |    0.476 |     2.855 | about 17% higher than government  |
| technology        |        0.680 |    0.278 |     1.663 | about 32% lower than government   |
| other             |        0.545 |    0.249 |     1.190 | about 46% lower than government   |
| retail            |        0.414 |    0.111 |     1.538 | about 59% lower than government   |

Full adjusted covariate table:

| effect                           |   rate_ratio |   ci_low |                                                                                                                                                                                                                                            ci_high |
|:---------------------------------|-------------:|---------:|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------:|
| Entity kind: government_body     |        1.142 |    0.548 |                                                                                                                                                                                                                                              2.376 |
| Entity kind: sparse_or_no_repeat |        0.800 |    0.105 |                                                                                                                                                                                                                                              6.078 |
| Organisation size                |        2.248 |    1.758 |                                                                                                                                                                                                                                              2.873 |
| Prior records: 100k+             |        1.383 |    0.868 |                                                                                                                                                                                                                                              2.202 |
| Prior records: 10k-100k          |        1.139 |    0.703 |                                                                                                                                                                                                                                              1.844 |
| Prior records: 1k-10k            |        0.788 |    0.431 |                                                                                                                                                                                                                                              1.439 |
| Prior records: <1k               |        1.431 |    0.840 |                                                                                                                                                                                                                                              2.438 |
| Sector: education                |        2.302 |    1.057 |                                                                                                                                                                                                                                              5.012 |
| Sector: finance                  |        1.166 |    0.476 |                                                                                                                                                                                                                                              2.855 |
| Sector: health                   |        1.299 |    0.535 |                                                                                                                                                                                                                                              3.155 |
| Sector: industrial               |        1.198 |    0.417 |                                                                                                                                                                                                                                              3.442 |
| Sector: media                    |        1.491 |    0.400 |                                                                                                                                                                                                                                              5.557 |
| Sector: other                    |        0.545 |    0.249 |                                                                                                                                                                                                                                              1.190 |
| Sector: retail                   |        0.414 |    0.111 |                                                                                                                                                                                                                                              1.538 |
| Sector: technology               |        0.680 |    0.278 |                                                                                                                                                                                                                                              1.663 |
| Sector: telecom_transport        |        1.610 |    0.661 |                                                                                                                                                                                                                                              3.920 |
| Unknown size                     |        0.000 |    0.000 | 3774365797678415553913357975664459274908723047526672737610066430481533365672396654623119782097227533874822013921744967571167098109224425715583742280759510479014826552372652809663030208734315116948927688516621796291720275771570312211070976.000 |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 91-180, 181-365 days vs 0-90 days (unadjusted): rate ratio 0.88 (95% CI 0.56-1.38), no direction can be read from it; two-sided p = 0.573. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.56 to 1.38, which includes 1. Repeat events: 60 in 91-180, 181-365 days and 28 in 0-90 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.998. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates do not have the hypothesised shape: long-term vs response goes the other way.

- response vs immediate: rate ratio 0.92 (95% CI 0.59-1.44), no direction can be read from it; one-sided p = 0.357 (alternative: lower). Not statistically significant at the 5% level: the data do not reject the null of no decrease (rate ratio at least 1); the 95% CI runs from 0.59 to 1.44, which includes 1. Repeat events: 59 in the response period and 28 in the immediate period.
- long-term vs response: rate ratio 0.58 (95% CI 0.40-0.84), no direction can be read from it; one-sided p = 0.998 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.40 to 0.84. The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 70 in the long-term period and 59 in the response period.
- long-term vs immediate: rate ratio 0.53 (95% CI 0.34-0.84), about 47% lower; two-sided p = 0.007. Statistically significant at the 5% level: the data reject equal rates (rate ratio 1). Repeat events: 70 in the long-term period and 28 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.003862
- directional U-shape p-value: 0.9983

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       28 |           90937 |                      11.246 |
| Response period  |       59 |          221677 |                       9.721 |
| Long-term period |       70 |          493808 |                       5.178 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        0.919 |    0.585 |     1.443 | about 8% lower   |         0.714 | less                      | 0.357           |
| long-term vs response  |        0.581 |    0.404 |     0.836 | about 42% lower  |         0.003 | greater                   | 0.998           |
| long-term vs immediate |        0.534 |    0.338 |     0.845 | about 47% lower  |         0.007 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        0.919 |    0.585 |     1.443 |      0.858 | about 8% lower   |           0.357 |
| All covariates                | long-term vs response |        0.581 |    0.404 |     0.836 |      0.432 | about 42% lower  |           0.998 |
| Drop organisation size        | response vs immediate |        0.938 |    0.597 |     1.472 |      0.875 | about 6% lower   |           0.390 |
| Drop organisation size        | long-term vs response |        0.635 |    0.444 |     0.910 |      0.467 | about 36% lower  |           0.993 |
| Drop industry/sector          | response vs immediate |        0.890 |    0.567 |     1.397 |      0.829 | about 11% lower  |           0.306 |
| Drop industry/sector          | long-term vs response |        0.558 |    0.389 |     0.801 |      0.411 | about 44% lower  |           0.999 |
| Drop size and industry/sector | response vs immediate |        0.910 |    0.580 |     1.427 |      0.847 | about 9% lower   |           0.340 |
| Drop size and industry/sector | long-term vs response |        0.621 |    0.434 |     0.887 |      0.453 | about 38% lower  |           0.996 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.0039 |                         0.9983 |
| Drop organisation size        |    0.0187 |                         0.9933 |
| Drop industry/sector          |    0.0011 |                         0.9992 |
| Drop size and industry/sector |    0.0093 |                         0.9956 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.59-1.44). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.40-0.84).

## Delayed 91-180 Day Peak Test

Definition used here: the hypothesised peak is 91-180 days after the prior event. A formal peak requires 91-180 days to be higher than both 0-90 days and the pooled post-180 period.
The adjusted peak model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main model.

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.540. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates do not have the hypothesised shape: 91-180 vs 0-90 goes the other way.

- 91-180 vs 0-90: rate ratio 0.97 (95% CI 0.56-1.68), no direction can be read from it; one-sided p = 0.540 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.56 to 1.68, which includes 1. Repeat events: 24 in 91-180 days and 28 in 0-90 days.
- 91-180 vs post-180: rate ratio 1.53 (95% CI 0.97-2.42), about 53% higher; one-sided p = 0.032 (alternative: higher). Statistically significant at the 5% level: the data reject the null of no increase (rate ratio at most 1). The two-sided 95% CI still includes 1, so the evidence is one-sided only. Repeat events: 24 in 91-180 days and 105 in post-180 days.
- post-180 vs 0-90: rate ratio 0.63 (95% CI 0.41-0.98), about 37% lower; two-sided p = 0.038. Statistically significant at the 5% level: the data reject equal rates (rate ratio 1). Repeat events: 105 in post-180 days and 28 in 0-90 days.

- phase-model LRT p-value versus constant elapsed-time risk: 0.05215
- directional delayed-peak p-value: 0.5399

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |       28 |           90937 |                      11.246 |
| Delayed peak 91-180 days |       24 |           81321 |                      10.780 |
| Post-180 days            |      105 |          634164 |                       6.048 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        0.973 |    0.564 |     1.678 | about 3% lower   |         0.920 | greater                   | 0.540           |
| 91-180 vs post-180 |        1.535 |    0.974 |     2.418 | about 53% higher |         0.065 | greater                   | 0.032           |
| post-180 vs 0-90   |        0.634 |    0.411 |     0.976 | about 37% lower  |         0.038 |                           |                 |

## Unadjusted Parametric Survival Model Comparison (Descriptive)

Best AIC model: weibull. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| weibull           |        -1500.990 |            2 | 3005.979 | 3015.921 | True        |
| loglogistic       |        -1501.365 |            2 | 3006.730 | 3016.672 | True        |
| generalized_gamma |        -1501.002 |            3 | 3008.004 | 3022.916 | True        |
| lognormal         |        -1502.526 |            2 | 3009.053 | 3018.994 | True        |
| exponential       |        -1506.967 |            1 | 3015.935 | 3020.905 | True        |

Exponential vs Weibull LRT p-value: 0.0005449

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
