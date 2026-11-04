# Repeat Cyber-Event Timing Analysis

## Executive Summary

**Is the data adequate for a reliable recurrence model?** Adequate for a simple model: 173 repeats, 19 per parameter of the models used here; but the full adjusted model is over-parameterised (26 parameters, 6.7 repeats each, 0 not identified), so it is not used for these conclusions; 63% of incident dates are unverified, so gaps can be resolved only to about a month: splits finer than 30 days are not attempted.

**What are the best splits for recurrence time bands?** Best bands: 0-365, 366-1095, >1095 days, with rates 1.00, 0.52, 0.97 times the first band's (positive evidence: 4.8 BIC better than a constant rate, holding size, prior event number and a calendar trend; search-adjusted p = 0.018). The long-term growth in recorded incidents does not explain it: the per-organisation rate changes by -4% a year (yearly effects from 2020, p = 0.87), and the bands hold under no calendar adjustment, a linear trend, monthly reporting intensity and yearly effects alike (p from 0.0004 to 0.0005).

**Is the rate lower 1-90 days after an event (short-term vigilance)?** No evidence of short-term vigilance: the adjusted rate 1-90 days after an event is not lower than at other times (one-sided p = 0.945; 28 repeats against 21.4 expected if the rate were no different).

**Is there a delayed peak in recurrence 91-180 days after an event?** No evidence of a delayed peak: the adjusted rate 91-180 days after an event is not higher than at other times (one-sided p = 0.089; 25 repeats). The narrower 91-120 day window holds only 7 repeats, too few to test on its own.

**What fits recurrence rates best?** Best fit by AIC, holding size, prior event number and a calendar trend: bands 0-365, 366-1095, >1095. The constant rate is 11.3 AIC behind. The bands were chosen by the search, so that margin is optimistic.

**Does organisation size predict recurrence rates?** Yes: the recurrence rate rises with organisation size. Rate relative to SMALL organisations: MEDIUM 1.4, LARGE 3.6, HUGE 7.4 (monotone model holding prior event number and a calendar trend, bootstrap p <= 0.002 (no simulated value as extreme in 500)). Size stands for visibility and exposure as much as size: this is prediction, not cause.


## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.
The primary model includes recorded organisation size. Size is a proxy for differences in visibility, attack exposure and target value, not a direct measure of those factors; residual confounding remains possible. The ordinal size term assumes equal log-rate steps between categories (compared with categorical size and a size-by-elapsed-time interaction under Organisation Size Specification), UNKNOWN has a separate indicator, an organisation stored under several names takes the size its variants support best (`organisation_size_reconciliation.csv`), and current recorded size may differ from size at an incident's date. Unadjusted rates, plots, parametric models and size-dropped variants below are descriptive diagnostics and do not control for this confounding.

## Data Quality Summary

- victim event attributions: 1135
- unique deduplicated events: 1009
- victim entities: 960
- entities with more than one event: 111
- post-event spells: 1133
- observed repeat spells: 173
- right-censored spells: 960
- observation window (events before its start are excluded): {'observation start': '2020-01-01', 'victim event attributions before it (excluded)': 95, 'events before it (excluded)': 84, 'organisations with events only before it (excluded)': 22}
- censor date: 2026-11-03
- elapsed-time bands: 0-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2020-01-01
- maximum event date: 2026-11-01
- first-of-month event dates: 227
- verified incident-date precision: {'unverified': 710, 'day': 329, 'month': 96}
- tie policy (several incidents of one organisation on one date): collapse
- same-date ties: {'ties (organisation-dates with several incidents)': 2, 'incidents in ties': 4, 'organisations with a tie': 2, 'repeats a policy that separates tied incidents adds': 2, 'ties with a shared verified interval longer than a day (spread by interval_spread)': 0, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 2, 'ties: other_day': 0, 'repeats between tied incidents in this run': 0}
- incidents not ordered by their dates (different stored dates, overlapping verified intervals): {'consecutive pairs with overlapping verified intervals': 2, '... of which stored dates differ by under 90 days': 2}
- covariate coverage: {'size_estimate levels': {'HUGE': 270, 'LARGE': 368, 'MEDIUM': 321, 'SMALL': 126, 'UNKNOWN': 50}, 'size_confidence populated': 1130, 'employee_count populated': 1050, 'turnover populated': 0, 'industry populated': 947, 'entity_kind populated': 1135, 'sector_proxy levels': {'education': 99, 'finance': 60, 'government': 220, 'health': 94, 'industrial': 35, 'media': 10, 'other': 418, 'retail': 50, 'technology': 106, 'telecom_transport': 43}, 'prior records populated': 468}
- organisation size across name variants: {'organisations stored under several name variants': 10, 'organisations whose size changed against the representative-only rule': 1, 'organisations whose variants give conflicting known size bands': 0, 'deciding rule': {'higher size_confidence': 7, 'known band over UNKNOWN': 2, 'representative variant': 1}}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 39, 'events with an unresolved attribution': 37, 'organisations with an unresolved attribution': 39, 'victim-event attributions analysed': 1135, 'repeats': {'included': {'observed repeats': 173, 'entities with a repeat': 110, 'repeats by elapsed band': {'0-180': 53, '181-365': 37, '366-730': 27, '731-1460': 39, '>1460': 17}}, 'excluded': {'observed repeats': 158, 'entities with a repeat': 102, 'repeats by elapsed band': {'0-180': 47, '181-365': 34, '366-730': 24, '731-1460': 36, '>1460': 17}}, 'repeats involving an unresolved attribution': 17}}

## Same-Date Ties And Unknown Ordering

A same-date tie is far more likely one incident that deduplication has not joined than two incidents on one day. Every such pair, and every pair whose possible periods overlap, is an exception listed with the reason it is still two events in `dashboard/close_pairs.html`; the merging steps are fixed when one is a missed duplicate. Until a tie is resolved, each tie policy makes one stated assumption about it (every tie, with its incident ids and the length of any verified interval its incidents share, is in `entity_date_ties.csv`):

- `collapse` (primary): the tied incidents are one occurrence: one time point, never a repeat between them (their ids and count are kept).
- `zero_gap`: the tied incidents are distinct and one day apart, in id order: each after the first is a repeat after 1 day.
- `interval_spread`: the tied incidents are distinct and evenly spread across the verified interval they share (about interval length / n days apart, the first at the interval's start); a tie with no shared verified interval longer than a day is placed as under zero_gap.

None of these bounds the truth: a tie that is one incident filed twice is represented only by `collapse`; two incidents really weeks apart within a verified month are represented exactly by none; and a tie whose dates are unverified has no interval to place its incidents in, so `interval_spread` places it as `zero_gap` does. Agreement across the policies shows only that a result does not hinge on these assumptions. Every policy is adjusted for organisation size like the primary model.

- Under `collapse`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.017). It rests on 173 observed repeats.
- Under `zero_gap`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.011). It rests on 175 observed repeats.
- Under `interval_spread`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.011). It rests on 175 observed repeats.

| tie_policy      |   observed_repeats |   repeats_from_ties |   post_event_spells |   repeats_0-180 |   repeats_181-365 |   repeats_366-730 |   repeats_731-1460 |   repeats_>1460 |   adjusted_constant_hazard_p |   adjusted_0_90_91_180_banding_p |   delayed_peak_directional_p |
|:----------------|-------------------:|--------------------:|--------------------:|----------------:|------------------:|------------------:|-------------------:|----------------:|-----------------------------:|---------------------------------:|-----------------------------:|
| collapse        |                173 |                   0 |                1133 |              53 |                37 |                27 |                 39 |              17 |                      0.01717 |                          0.03436 |                       0.5342 |
| zero_gap        |                175 |                   2 |                1135 |              55 |                37 |                27 |                 39 |              17 |                      0.01086 |                          0.02152 |                       0.6364 |
| interval_spread |                175 |                   2 |                1135 |              55 |                37 |                27 |                 39 |              17 |                      0.01086 |                          0.02152 |                       0.6364 |


## Organisation Size Specification

The primary model enters size as a numeric score (SMALL=1 to HUGE=4, UNKNOWN mean-imputed, plus an UNKNOWN indicator), which assumes equal log-rate steps between bands. It is compared here with size as a categorical factor (reference: the band with the most repeats; bands with fewer than 3 repeats pooled) and, where every delayed-peak phase has at least 10 repeats at two or more known size bands, with the score's interaction with the delayed-peak phases. The score stays primary; the headline tests are re-run under categorical size. Full table: `size_specification_comparison.csv`.

- Size specification (bands 0-180, 181-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.694 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 1186.0 (categorical) against 1182.7 (score), lower for the score model.
- With bands 0-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.017 with the size score and p = 0.018 with categorical size: the same verdict at the 5% level.
- Size specification (bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.694 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 1257.0 (categorical) against 1253.7 (score), lower for the score model.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.034 with the size score and p = 0.037 with categorical size: the same verdict at the 5% level.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the directional delayed-peak test gives p = 0.534 with the size score and p = 0.534 with categorical size: the same verdict at the 5% level.
- Size by elapsed-time phase (the size score's slope in 91-180 and post-180 days against 0-90): LRT p = 0.002 on 2 df; at the 5% level the data reject a size effect that is the same in every phase.

| banding       | size_spec     |   log_likelihood |   parameters |   aic |   delta_aic_vs_score | nested   |   likelihood_ratio |   df |    p_value |   elapsed_constant_hazard_p |   delayed_peak_directional_p |
|:--------------|:--------------|-----------------:|-------------:|------:|---------------------:|:---------|-------------------:|-----:|-----------:|----------------------------:|-----------------------------:|
| run           | score         |           -565.3 |           26 |  1183 |                0     |          |           nan      |  nan | nan        |                     0.01717 |                     nan      |
| run           | categorical   |           -565   |           28 |  1186 |                3.268 | True     |             0.7317 |    2 |   0.6936   |                     0.01848 |                     nan      |
| 0-90 / 91-180 | score         |           -599.9 |           27 |  1254 |                0     |          |           nan      |  nan | nan        |                     0.03436 |                       0.5342 |
| 0-90 / 91-180 | categorical   |           -599.5 |           29 |  1257 |                3.268 | True     |             0.7319 |    2 |   0.6936   |                     0.03676 |                       0.5343 |
| 0-90 / 91-180 | score_x_phase |           -593.6 |           29 |  1245 |               -8.517 | True     |            12.52   |    2 |   0.001914 |                   nan       |                     nan      |


## Primary Piecewise Exponential Test

The adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.017). It rests on 173 observed repeats.

- unadjusted LRT p-value: 0.0009753
- adjusted LRT p-value: 0.01717
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=1), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-180          |       53 |      182766.000 |                      10.592 |              7.465 |              13.862 |
| 181-365        |       37 |      150407.000 |                       8.985 |              5.708 |              12.642 |
| 366-730        |       27 |      231138.000 |                       4.267 |              2.691 |               5.916 |
| 731-1460       |       39 |      226263.000 |                       6.296 |              4.469 |               8.304 |
| >1460          |       17 |       75511.000 |                       8.223 |              4.232 |              12.623 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        2.049 |    1.625 |     2.584 | about 105% higher repeat-event rate |
| SMALL -> LARGE  |        4.198 |    2.640 |     6.675 | about 320% higher repeat-event rate |
| SMALL -> HUGE   |        8.600 |    4.290 |    17.244 | about 760% higher repeat-event rate |
| MEDIUM -> HUGE  |        4.198 |    2.640 |     6.675 | about 320% higher repeat-event rate |
| LARGE -> HUGE   |        2.049 |    1.625 |     2.584 | about 105% higher repeat-event rate |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| education           |        3.811 |    1.079 |    13.456 | about 281% higher than government |
| telecom_transport   |        2.979 |    0.784 |    11.321 | about 198% higher than government |
| health              |        2.330 |    0.607 |     8.953 | about 133% higher than government |
| industrial          |        2.260 |    0.528 |     9.673 | about 126% higher than government |
| finance             |        2.021 |    0.522 |     7.823 | about 102% higher than government |
| sparse_or_no_repeat |        1.995 |    0.319 |    12.489 | about 100% higher than government |
| technology          |        1.528 |    0.417 |     5.603 | about 53% higher than government  |
| other               |        1.164 |    0.333 |     4.072 | about 16% higher than government  |
| retail              |        1.099 |    0.234 |     5.158 | about 10% higher than government  |

Full adjusted covariate table:

| effect                           |   rate_ratio |   ci_low |   ci_high |
|:---------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body     |        2.420 |    0.702 |     8.341 |
| Entity kind: sparse_or_no_repeat |        0.656 |    0.080 |     5.396 |
| Organisation size                |        2.049 |    1.625 |     2.584 |
| Prior records: 100k+             |        1.325 |    0.866 |     2.029 |
| Prior records: 10k-100k          |        1.148 |    0.720 |     1.828 |
| Prior records: 1k-10k            |        0.609 |    0.328 |     1.130 |
| Prior records: <1k               |        1.489 |    0.879 |     2.521 |
| Sector: education                |        3.811 |    1.079 |    13.456 |
| Sector: finance                  |        2.021 |    0.522 |     7.823 |
| Sector: health                   |        2.330 |    0.607 |     8.953 |
| Sector: industrial               |        2.260 |    0.528 |     9.673 |
| Sector: other                    |        1.164 |    0.333 |     4.072 |
| Sector: retail                   |        1.099 |    0.234 |     5.158 |
| Sector: sparse_or_no_repeat      |        1.995 |    0.319 |    12.489 |
| Sector: technology               |        1.528 |    0.417 |     5.603 |
| Sector: telecom_transport        |        2.979 |    0.784 |    11.321 |
| Unknown size                     |        0.244 |    0.033 |     1.798 |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 181-365, 366-730 days vs 0-180 days (unadjusted): rate ratio 0.58 (95% CI 0.40-0.83), about 42% lower; two-sided p = 0.003. Statistically significant at the 5% level: the data reject equal rates (rate ratio 1). The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 64 in 181-365, 366-730 days and 53 in 0-180 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.217. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates have the hypothesised shape. The weakest part is long-term vs response (p = 0.217).

- response vs immediate: rate ratio 0.69 (95% CI 0.47-0.99), about 31% lower; one-sided p = 0.023 (alternative: lower). Statistically significant at the 5% level: the data reject the null of no decrease (rate ratio at least 1). Repeat events: 64 in the response period and 52 in the immediate period.
- long-term vs response: rate ratio 1.16 (95% CI 0.80-1.69), no direction can be read from it; one-sided p = 0.217 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.80 to 1.69, which includes 1. Repeat events: 56 in the long-term period and 64 in the response period.
- long-term vs immediate: rate ratio 0.80 (95% CI 0.53-1.20), no direction can be read from it; two-sided p = 0.276. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.53 to 1.20, which includes 1. Repeat events: 56 in the long-term period and 52 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.1431
- directional U-shape p-value: 0.2169

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       52 |          182766 |                      10.392 |
| Response period  |       64 |          381545 |                       6.127 |
| Long-term period |       56 |          301774 |                       6.778 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        0.686 |    0.473 |     0.995 | about 31% lower  |         0.047 | less                      | 0.023           |
| long-term vs response  |        1.161 |    0.799 |     1.687 | about 16% higher |         0.434 | greater                   | 0.217           |
| long-term vs immediate |        0.797 |    0.529 |     1.199 | about 20% lower  |         0.276 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        0.686 |    0.473 |     0.995 |      0.521 | about 31% lower  |           0.023 |
| All covariates                | long-term vs response |        1.161 |    0.799 |     1.687 |      0.888 | about 16% higher |           0.217 |
| Drop organisation size        | response vs immediate |        0.698 |    0.482 |     1.012 |      0.530 | about 30% lower  |           0.029 |
| Drop organisation size        | long-term vs response |        1.287 |    0.888 |     1.865 |      0.977 | about 29% higher |           0.091 |
| Drop industry/sector          | response vs immediate |        0.657 |    0.454 |     0.951 |      0.497 | about 34% lower  |           0.013 |
| Drop industry/sector          | long-term vs response |        1.149 |    0.793 |     1.665 |      0.872 | about 15% higher |           0.232 |
| Drop size and industry/sector | response vs immediate |        0.671 |    0.464 |     0.970 |      0.507 | about 33% lower  |           0.017 |
| Drop size and industry/sector | long-term vs response |        1.284 |    0.888 |     1.855 |      0.967 | about 28% higher |           0.092 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.1431 |                         0.2169 |
| Drop organisation size        |    0.1330 |                         0.0910 |
| Drop industry/sector          |    0.0880 |                         0.2320 |
| Drop size and industry/sector |    0.0923 |                         0.0920 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.47-0.99). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.80-1.69).

## Delayed 91-180 Day Peak Sensitivity

This sensitivity reruns the elapsed-time model with bands `0-90`, `91-180`, `181-365`, `366-730`, `731-1460`, and `>1460` to test whether the apparent early elevation is concentrated in days 91-180.
A formal delayed peak requires the 91-180 day period to be higher than both 0-90 days and the pooled post-180 period after adjustment.

- adjusted elapsed-time LRT p-value for the 90/180 split: 0.03436
- phase-model LRT p-value versus constant elapsed-time risk: 0.1935
- directional delayed-peak p-value: 0.5342

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |       28 |           96446 |                      10.604 |
| Delayed peak 91-180 days |       24 |           86320 |                      10.155 |
| Post-180 days            |      120 |          683319 |                       6.414 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        0.976 |    0.566 |     1.685 | about 2% lower   |         0.932 | greater                   | 0.534           |
| 91-180 vs post-180 |        1.360 |    0.869 |     2.127 | about 36% higher |         0.178 | greater                   | 0.089           |
| post-180 vs 0-90   |        0.718 |    0.470 |     1.097 | about 28% lower  |         0.126 |                           |                 |

Interpretation:

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.534. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates do not have the hypothesised shape: 91-180 vs 0-90 goes the other way.

- 91-180 vs 0-90: rate ratio 0.98 (95% CI 0.57-1.68), no direction can be read from it; one-sided p = 0.534 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.57 to 1.68, which includes 1. Repeat events: 24 in 91-180 days and 28 in 0-90 days.
- 91-180 vs post-180: rate ratio 1.36 (95% CI 0.87-2.13), no direction can be read from it; one-sided p = 0.089 (alternative: higher). Not statistically significant at the 5% level (borderline): the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.87 to 2.13, which includes 1. Repeat events: 24 in 91-180 days and 120 in post-180 days.
- post-180 vs 0-90: rate ratio 0.72 (95% CI 0.47-1.10), no direction can be read from it; two-sided p = 0.126. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.47 to 1.10, which includes 1. Repeat events: 120 in post-180 days and 28 in 0-90 days.

## Unadjusted Parametric Survival Model Comparison (Descriptive)

Best AIC model: weibull. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| weibull           |        -1643.031 |            2 | 3290.062 | 3300.127 | True        |
| loglogistic       |        -1643.937 |            2 | 3291.874 | 3301.939 | True        |
| generalized_gamma |        -1643.043 |            3 | 3292.086 | 3307.184 | True        |
| exponential       |        -1646.691 |            1 | 3295.383 | 3300.415 | True        |
| lognormal         |        -1646.784 |            2 | 3297.568 | 3307.633 | True        |

Exponential vs Weibull LRT p-value: 0.006816

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
