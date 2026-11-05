# Repeat Cyber-Event Timing Analysis

## Executive Summary

**Is the data adequate for a reliable recurrence model?** Adequate for a simple model: 157 repeats, 20 per parameter of the models used here; but the full adjusted model is over-parameterised (26 parameters, 6.0 repeats each, 1 not identified), so it is not used for these conclusions; 52% of incident dates are unverified, so gaps can be resolved only to about a month: splits finer than 30 days are not attempted.

**What are the best splits for recurrence time bands?** Best bands: 0-270, >270 days, with rates 1.00, 0.52 times the first band's (strong evidence: 8.1 BIC better than a constant rate, holding size, prior event number and a calendar trend; search-adjusted p = 0.010). The long-term growth in recorded incidents does not explain it: the per-organisation rate changes by -0% a year (yearly effects from 2020, p = 0.48), and the bands hold under no calendar adjustment, a linear trend, monthly reporting intensity and yearly effects alike (p from 0.0001 to 0.0003).

**Is the rate lower 1-90 days after an event (short-term vigilance)?** No evidence of short-term vigilance: the adjusted rate 1-90 days after an event is not lower than at other times (one-sided p = 0.978; 27 repeats against 19.0 expected if the rate were no different).

**Is there a delayed peak in recurrence 91-180 days after an event?** Yes: the adjusted rate 91-180 days after an event is 1.59 times the rate at other times (95% CI 0.99-2.44; one-sided p = 0.027; 25 repeats). The narrower 91-120 day window holds only 8 repeats, too few to test on its own.

**What fits recurrence rates best?** Best fit by AIC, holding size, prior event number and a calendar trend: bands 0-270, >270. The constant rate is 12.5 AIC behind. The bands were chosen by the search, so that margin is optimistic.

**Does organisation size predict recurrence rates?** Yes: the recurrence rate rises with organisation size. Rate relative to SMALL organisations: MEDIUM 1.4, LARGE 3.6, HUGE 8.8 (monotone model holding prior event number and a calendar trend, bootstrap p <= 0.002 (no simulated value as extreme in 500)). Size stands for visibility and exposure as much as size: this is prediction, not cause.


## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.
The primary model includes recorded organisation size. Size is a proxy for differences in visibility, attack exposure and target value, not a direct measure of those factors; residual confounding remains possible. The ordinal size term assumes equal log-rate steps between categories (compared with categorical size and a size-by-elapsed-time interaction under Organisation Size Specification), UNKNOWN has a separate indicator, an organisation stored under several names takes the size its variants support best (`organisation_size_reconciliation.csv`), and current recorded size may differ from size at an incident's date. Unadjusted rates, plots, parametric models and size-dropped variants below are descriptive diagnostics and do not control for this confounding.

## Data Quality Summary

- victim event attributions: 1067
- unique deduplicated events: 1011
- victim entities: 907
- entities with more than one event: 107
- post-event spells: 1064
- observed repeat spells: 157
- right-censored spells: 907
- observation window (events before its start are excluded): {'observation start': '2020-01-01', 'victim event attributions before it (excluded)': 94, 'events before it (excluded)': 88, 'organisations with events only before it (excluded)': 26}
- censor date: 2026-11-03
- elapsed-time bands: 0-90, 91-365, 366-730, 731-1460, >1460
- minimum event date: 2020-01-01
- maximum event date: 2026-11-01
- first-of-month event dates: 246
- verified incident-date precision: {'unverified': 553, 'day': 394, 'month': 120}
- tie policy (several incidents of one organisation on one date): collapse
- same-date ties: {'ties (organisation-dates with several incidents)': 3, 'incidents in ties': 6, 'organisations with a tie': 3, 'repeats a policy that separates tied incidents adds': 3, 'ties with a shared verified interval longer than a day (spread by interval_spread)': 0, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 2, 'ties: other_day': 1, 'repeats between tied incidents in this run': 0}
- incidents not ordered by their dates (different stored dates, overlapping verified intervals): {'consecutive pairs with overlapping verified intervals': 4, '... of which stored dates differ by under 90 days': 4}
- covariate coverage: {'size_estimate levels': {'HUGE': 240, 'LARGE': 348, 'MEDIUM': 321, 'SMALL': 123, 'UNKNOWN': 35}, 'size_confidence populated': 1061, 'employee_count populated': 998, 'turnover populated': 0, 'industry populated': 929, 'entity_kind populated': 1067, 'sector_proxy levels': {'education': 87, 'finance': 57, 'government': 202, 'health': 90, 'industrial': 31, 'media': 10, 'other': 398, 'retail': 49, 'technology': 105, 'telecom_transport': 38}, 'prior records populated': 427}
- organisation size across name variants: {'organisations stored under several name variants': 6, 'organisations whose size changed against the representative-only rule': 1, 'organisations whose variants give conflicting known size bands': 0, 'deciding rule': {'higher size_confidence': 5, 'known band over UNKNOWN': 1}}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 39, 'events with an unresolved attribution': 38, 'organisations with an unresolved attribution': 38, 'victim-event attributions analysed': 1067, 'repeats': {'included': {'observed repeats': 157, 'entities with a repeat': 105, 'repeats by elapsed band': {'0-90': 27, '91-365': 60, '366-730': 21, '731-1460': 36, '>1460': 13}}, 'excluded': {'observed repeats': 142, 'entities with a repeat': 98, 'repeats by elapsed band': {'0-90': 24, '91-365': 53, '366-730': 20, '731-1460': 31, '>1460': 14}}, 'repeats involving an unresolved attribution': 18}}

## Same-Date Ties And Unknown Ordering

A same-date tie is far more likely one incident that deduplication has not joined than two incidents on one day. Every such pair, and every pair whose possible periods overlap, is an exception listed with the reason it is still two events in `dashboard/close_pairs.html`; the merging steps are fixed when one is a missed duplicate. Until a tie is resolved, each tie policy makes one stated assumption about it (every tie, with its incident ids and the length of any verified interval its incidents share, is in `entity_date_ties.csv`):

- `collapse` (primary): the tied incidents are one occurrence: one time point, never a repeat between them (their ids and count are kept).
- `zero_gap`: the tied incidents are distinct and one day apart, in id order: each after the first is a repeat after 1 day.
- `interval_spread`: the tied incidents are distinct and evenly spread across the verified interval they share (about interval length / n days apart, the first at the interval's start); a tie with no shared verified interval longer than a day is placed as under zero_gap.

None of these bounds the truth: a tie that is one incident filed twice is represented only by `collapse`; two incidents really weeks apart within a verified month are represented exactly by none; and a tie whose dates are unverified has no interval to place its incidents in, so `interval_spread` places it as `zero_gap` does. Agreement across the policies shows only that a result does not hinge on these assumptions. Every policy is adjusted for organisation size like the primary model.

- Under `collapse`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.003). It rests on 157 observed repeats.
- Under `zero_gap`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.001). It rests on 160 observed repeats.
- Under `interval_spread`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.001). It rests on 160 observed repeats.

| tie_policy      |   observed_repeats |   repeats_from_ties |   post_event_spells |   repeats_0-90 |   repeats_91-365 |   repeats_366-730 |   repeats_731-1460 |   repeats_>1460 |   adjusted_constant_hazard_p |   adjusted_0_90_91_180_banding_p |   delayed_peak_directional_p |
|:----------------|-------------------:|--------------------:|--------------------:|---------------:|-----------------:|------------------:|-------------------:|----------------:|-----------------------------:|---------------------------------:|-----------------------------:|
| collapse        |                157 |                   0 |                1064 |             27 |               60 |                21 |                 36 |              13 |                     0.002937 |                         0.006389 |                       0.4869 |
| zero_gap        |                160 |                   3 |                1067 |             30 |               60 |                21 |                 36 |              13 |                     0.001027 |                         0.002348 |                       0.6419 |
| interval_spread |                160 |                   3 |                1067 |             30 |               60 |                21 |                 36 |              13 |                     0.001027 |                         0.002348 |                       0.6419 |


## Organisation Size Specification

The primary model enters size as a numeric score (SMALL=1 to HUGE=4, UNKNOWN mean-imputed, plus an UNKNOWN indicator), which assumes equal log-rate steps between bands. It is compared here with size as a categorical factor (reference: the band with the most repeats; bands with fewer than 3 repeats pooled) and, where every delayed-peak phase has at least 10 repeats at two or more known size bands, with the score's interaction with the delayed-peak phases. The score stays primary; the headline tests are re-run under categorical size. Full table: `size_specification_comparison.csv`.

- Size specification (bands 0-90, 91-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.247 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 1165.7 (categorical) against 1164.5 (score), lower for the score model.
- With bands 0-90, 91-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.003 with the size score and p = 0.003 with categorical size: the same verdict at the 5% level.
- Size specification (bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.247 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 1228.0 (categorical) against 1226.8 (score), lower for the score model.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.006 with the size score and p = 0.007 with categorical size: the same verdict at the 5% level.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the directional delayed-peak test gives p = 0.487 with the size score and p = 0.486 with categorical size: the same verdict at the 5% level.
- Size by elapsed-time phase (the size score's slope in 91-180 and post-180 days against 0-90): LRT p = 0.003 on 2 df; at the 5% level the data reject a size effect that is the same in every phase.

| banding       | size_spec     |   log_likelihood |   parameters |   aic |   delta_aic_vs_score | nested   |   likelihood_ratio |   df |    p_value |   elapsed_constant_hazard_p |   delayed_peak_directional_p |
|:--------------|:--------------|-----------------:|-------------:|------:|---------------------:|:---------|-------------------:|-----:|-----------:|----------------------------:|-----------------------------:|
| run           | score         |           -556.2 |           26 |  1164 |                0     |          |            nan     |  nan | nan        |                    0.002937 |                     nan      |
| run           | categorical   |           -554.8 |           28 |  1166 |                1.206 | True     |              2.794 |    2 |   0.2473   |                    0.003158 |                     nan      |
| 0-90 / 91-180 | score         |           -586.4 |           27 |  1227 |                0     |          |            nan     |  nan | nan        |                    0.006389 |                       0.4869 |
| 0-90 / 91-180 | categorical   |           -585   |           29 |  1228 |                1.205 | True     |              2.795 |    2 |   0.2472   |                    0.006838 |                       0.4856 |
| 0-90 / 91-180 | score_x_phase |           -580.7 |           29 |  1219 |               -7.444 | True     |             11.44  |    2 |   0.003273 |                  nan        |                     nan      |


## Primary Piecewise Exponential Test

The adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.003). It rests on 157 observed repeats.

- unadjusted LRT p-value: 9.189e-05
- adjusted LRT p-value: 0.002937
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |       27 |       90923.000 |                      10.846 |              7.200 |              15.662 |
| 91-365         |       60 |      221677.000 |                       9.886 |              6.909 |              13.043 |
| 366-730        |       21 |      216390.000 |                       3.545 |              2.057 |               5.280 |
| 731-1460       |       36 |      210357.000 |                       6.251 |              4.367 |               8.378 |
| >1460          |       13 |       67075.000 |                       7.079 |              3.227 |              11.079 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                       |
|:----------------|-------------:|---------:|----------:|:-------------------------------------|
| SMALL -> MEDIUM |        2.259 |    1.764 |     2.891 | about 126% higher repeat-event rate  |
| SMALL -> LARGE  |        5.101 |    3.113 |     8.359 | about 410% higher repeat-event rate  |
| SMALL -> HUGE   |       11.521 |    5.492 |    24.168 | about 1052% higher repeat-event rate |
| MEDIUM -> HUGE  |        5.101 |    3.113 |     8.359 | about 410% higher repeat-event rate  |
| LARGE -> HUGE   |        2.259 |    1.764 |     2.891 | about 126% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector            |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:------------------|-------------:|---------:|----------:|:----------------------------------|
| education         |        2.354 |    1.081 |     5.124 | about 135% higher than government |
| telecom_transport |        1.634 |    0.670 |     3.982 | about 63% higher than government  |
| media             |        1.497 |    0.402 |     5.579 | about 50% higher than government  |
| health            |        1.308 |    0.538 |     3.177 | about 31% higher than government  |
| industrial        |        1.193 |    0.415 |     3.430 | about 19% higher than government  |
| finance           |        1.180 |    0.481 |     2.892 | about 18% higher than government  |
| technology        |        0.695 |    0.284 |     1.700 | about 31% lower than government   |
| other             |        0.526 |    0.240 |     1.156 | about 47% lower than government   |
| retail            |        0.419 |    0.113 |     1.560 | about 58% lower than government   |

Full adjusted covariate table:

| effect                           |   rate_ratio |   ci_low |   ci_high |
|:---------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body     |        1.146 |    0.550 |     2.387 |
| Entity kind: sparse_or_no_repeat |        0.804 |    0.106 |     6.117 |
| Organisation size                |        2.259 |    1.764 |     2.891 |
| Prior records: 100k+             |        1.318 |    0.823 |     2.111 |
| Prior records: 10k-100k          |        1.126 |    0.695 |     1.824 |
| Prior records: 1k-10k            |        0.781 |    0.428 |     1.427 |
| Prior records: <1k               |        1.420 |    0.834 |     2.420 |
| Sector: education                |        2.354 |    1.081 |     5.124 |
| Sector: finance                  |        1.180 |    0.481 |     2.892 |
| Sector: health                   |        1.308 |    0.538 |     3.177 |
| Sector: industrial               |        1.193 |    0.415 |     3.430 |
| Sector: media                    |        1.497 |    0.402 |     5.579 |
| Sector: other                    |        0.526 |    0.240 |     1.156 |
| Sector: retail                   |        0.419 |    0.113 |     1.560 |
| Sector: technology               |        0.695 |    0.284 |     1.700 |
| Sector: telecom_transport        |        1.634 |    0.670 |     3.982 |
| Unknown size                     |        0.000 |    0.000 |   inf     |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 91-365, 366-730 days vs 0-90 days (unadjusted): rate ratio 0.62 (95% CI 0.40-0.96), about 38% lower; two-sided p = 0.033. Statistically significant at the 5% level: the data reject equal rates (rate ratio 1). The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 81 in 91-365, 366-730 days and 27 in 0-90 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.378. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates have the hypothesised shape. The weakest part is long-term vs response (p = 0.378).

- response vs immediate: rate ratio 0.71 (95% CI 0.46-1.10), no direction can be read from it; one-sided p = 0.063 (alternative: lower). Not statistically significant at the 5% level (borderline): the data do not reject the null of no decrease (rate ratio at least 1); the 95% CI runs from 0.46 to 1.10, which includes 1. Repeat events: 80 in the response period and 27 in the immediate period.
- long-term vs response: rate ratio 1.06 (95% CI 0.73-1.55), no direction can be read from it; one-sided p = 0.378 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.73 to 1.55, which includes 1. Repeat events: 49 in the long-term period and 80 in the response period.
- long-term vs immediate: rate ratio 0.75 (95% CI 0.45-1.24), no direction can be read from it; two-sided p = 0.266. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.45 to 1.24, which includes 1. Repeat events: 49 in the long-term period and 27 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.3297
- directional U-shape p-value: 0.3776

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       27 |           90923 |                      10.846 |
| Response period  |       80 |          438067 |                       6.670 |
| Long-term period |       49 |          277432 |                       6.451 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        0.708 |    0.455 |     1.101 | about 29% lower  |         0.125 | less                      | 0.063           |
| long-term vs response  |        1.062 |    0.729 |     1.545 | about 6% higher  |         0.755 | greater                   | 0.378           |
| long-term vs immediate |        0.752 |    0.455 |     1.242 | about 25% lower  |         0.266 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        0.708 |    0.455 |     1.101 |      0.646 | about 29% lower  |           0.063 |
| All covariates                | long-term vs response |        1.062 |    0.729 |     1.545 |      0.816 | about 6% higher  |           0.378 |
| Drop organisation size        | response vs immediate |        0.727 |    0.468 |     1.129 |      0.661 | about 27% lower  |           0.078 |
| Drop organisation size        | long-term vs response |        1.197 |    0.826 |     1.736 |      0.909 | about 20% higher |           0.171 |
| Drop industry/sector          | response vs immediate |        0.676 |    0.436 |     1.050 |      0.615 | about 32% lower  |           0.041 |
| Drop industry/sector          | long-term vs response |        0.999 |    0.689 |     1.451 |      0.762 | about 0% lower   |           0.501 |
| Drop size and industry/sector | response vs immediate |        0.698 |    0.450 |     1.082 |      0.632 | about 30% lower  |           0.054 |
| Drop size and industry/sector | long-term vs response |        1.163 |    0.805 |     1.681 |      0.877 | about 16% higher |           0.210 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.3297 |                         0.3776 |
| Drop organisation size        |    0.3132 |                         0.1707 |
| Drop industry/sector          |    0.2225 |                         0.5011 |
| Drop size and industry/sector |    0.2690 |                         0.2104 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.46-1.10). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.73-1.55).

## Delayed 91-180 Day Peak Sensitivity

This sensitivity reruns the elapsed-time model with bands `0-90`, `91-180`, `181-365`, `366-730`, `731-1460`, and `>1460` to test whether the apparent early elevation is concentrated in days 91-180.
A formal delayed peak requires the 91-180 day period to be higher than both 0-90 days and the pooled post-180 period after adjustment.

- adjusted elapsed-time LRT p-value for the 90/180 split: 0.006389
- phase-model LRT p-value versus constant elapsed-time risk: 0.07545
- directional delayed-peak p-value: 0.4869

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |       27 |           90923 |                      10.846 |
| Delayed peak 91-180 days |       24 |           81321 |                      10.780 |
| Post-180 days            |      105 |          634178 |                       6.047 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        1.009 |    0.582 |     1.750 | about 1% higher  |         0.974 | greater                   | 0.487           |
| 91-180 vs post-180 |        1.525 |    0.968 |     2.404 | about 53% higher |         0.069 | greater                   | 0.034           |
| post-180 vs 0-90   |        0.662 |    0.427 |     1.026 | about 34% lower  |         0.065 |                           |                 |

Interpretation:

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.487. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates have the hypothesised shape. The weakest part is 91-180 vs 0-90 (p = 0.487).

- 91-180 vs 0-90: rate ratio 1.01 (95% CI 0.58-1.75), no direction can be read from it; one-sided p = 0.487 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.58 to 1.75, which includes 1. Repeat events: 24 in 91-180 days and 27 in 0-90 days.
- 91-180 vs post-180: rate ratio 1.53 (95% CI 0.97-2.40), about 53% higher; one-sided p = 0.034 (alternative: higher). Statistically significant at the 5% level: the data reject the null of no increase (rate ratio at most 1). The two-sided 95% CI still includes 1, so the evidence is one-sided only. Repeat events: 24 in 91-180 days and 105 in post-180 days.
- post-180 vs 0-90: rate ratio 0.66 (95% CI 0.43-1.03), no direction can be read from it; two-sided p = 0.065. Not statistically significant at the 5% level (borderline): the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.43 to 1.03, which includes 1. Repeat events: 105 in post-180 days and 27 in 0-90 days.

## Unadjusted Parametric Survival Model Comparison (Descriptive)

Best AIC model: weibull. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| weibull           |        -1493.069 |            2 | 2990.138 | 3000.078 | True        |
| loglogistic       |        -1493.411 |            2 | 2990.823 | 3000.762 | True        |
| generalized_gamma |        -1493.061 |            3 | 2992.123 | 3007.032 | True        |
| lognormal         |        -1494.532 |            2 | 2993.065 | 3003.004 | True        |
| exponential       |        -1498.426 |            1 | 2998.853 | 3003.822 | True        |

Exponential vs Weibull LRT p-value: 0.001063

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
