# Repeat Cyber-Event Timing Analysis

## Executive Summary

**Is the data adequate for a reliable recurrence model?** Adequate for a simple model: 171 repeats, 19 per parameter of the models used here; but the full adjusted model is over-parameterised (26 parameters, 6.6 repeats each, 0 not identified), so it is not used for these conclusions; 62% of incident dates are unverified, so gaps can be resolved only to about a month: splits finer than 30 days are not attempted.

**What are the best splits for recurrence time bands?** No split is supported beyond what the search finds by chance: the best of 593 bandings (0-365, 366-1095, >1095 days) beats a constant rate by 2.7 BIC, but searching constant-rate data does as well (p = 0.052).

**Is the rate lower 1-90 days after an event (short-term vigilance)?** No evidence of short-term vigilance: the adjusted rate 1-90 days after an event is not lower than at other times (one-sided p = 0.888; 26 repeats against 21.2 expected if the rate were no different).

**Is there a delayed peak in recurrence 91-180 days after an event?** No evidence of a delayed peak: the adjusted rate 91-180 days after an event is not higher than at other times (one-sided p = 0.121; 24 repeats). The narrower 91-120 day window holds only 7 repeats, too few to test on its own.

**What fits recurrence rates best?** Best fit by AIC, holding size, prior event number and a calendar trend: bands 0-365, 366-1095, >1095. Its bands were chosen by the search, and the search-adjusted test does not support them, so treat the rate as constant.

**Does organisation size predict recurrence rates?** Yes: the recurrence rate rises with organisation size. Rate relative to SMALL organisations: MEDIUM 1.3, LARGE 3.6, HUGE 7.4 (monotone model holding prior event number and a calendar trend, bootstrap p <= 0.002 (no simulated value as extreme in 500)). Size stands for visibility and exposure as much as size: this is prediction, not cause.


## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.
The primary model includes recorded organisation size. Size is a proxy for differences in visibility, attack exposure and target value, not a direct measure of those factors; residual confounding remains possible. The ordinal size term assumes equal log-rate steps between categories (compared with categorical size and a size-by-elapsed-time interaction under Organisation Size Specification), UNKNOWN has a separate indicator, an organisation stored under several names takes the size its variants support best (`organisation_size_reconciliation.csv`), and current recorded size may differ from size at an incident's date. Unadjusted rates, plots, parametric models and size-dropped variants below are descriptive diagnostics and do not control for this confounding.

## Data Quality Summary

- victim event attributions: 1132
- unique deduplicated events: 1011
- victim entities: 959
- entities with more than one event: 110
- post-event spells: 1130
- observed repeat spells: 171
- right-censored spells: 959
- observation window (events before its start are excluded): {'observation start': '2020-01-01', 'victim event attributions before it (excluded)': 96, 'events before it (excluded)': 84, 'organisations with events only before it (excluded)': 23}
- censor date: 2026-11-03
- elapsed-time bands: 0-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2020-01-01
- maximum event date: 2026-11-01
- first-of-month event dates: 226
- verified incident-date precision: {'unverified': 702, 'day': 331, 'month': 99}
- tie policy (several incidents of one organisation on one date): collapse
- same-date ties: {'ties (organisation-dates with several incidents)': 2, 'incidents in ties': 4, 'organisations with a tie': 2, 'repeats a policy that separates tied incidents adds': 2, 'ties with a shared verified interval longer than a day (spread by interval_spread)': 0, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 2, 'ties: other_day': 0, 'repeats between tied incidents in this run': 0}
- incidents not ordered by their dates (different stored dates, overlapping verified intervals): {'consecutive pairs with overlapping verified intervals': 2, '... of which stored dates differ by under 90 days': 2}
- covariate coverage: {'size_estimate levels': {'HUGE': 268, 'LARGE': 367, 'MEDIUM': 323, 'SMALL': 126, 'UNKNOWN': 48}, 'size_confidence populated': 1126, 'employee_count populated': 1048, 'turnover populated': 0, 'industry populated': 944, 'entity_kind populated': 1132, 'sector_proxy levels': {'education': 96, 'finance': 60, 'government': 219, 'health': 94, 'industrial': 34, 'media': 10, 'other': 424, 'retail': 50, 'technology': 103, 'telecom_transport': 42}, 'prior records populated': 458}
- organisation size across name variants: {'organisations stored under several name variants': 11, 'organisations whose size changed against the representative-only rule': 1, 'organisations whose variants give conflicting known size bands': 0, 'deciding rule': {'higher size_confidence': 8, 'known band over UNKNOWN': 2, 'representative variant': 1}}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 39, 'events with an unresolved attribution': 37, 'organisations with an unresolved attribution': 39, 'victim-event attributions analysed': 1132, 'repeats': {'included': {'observed repeats': 171, 'entities with a repeat': 109, 'repeats by elapsed band': {'0-180': 50, '181-365': 37, '366-730': 27, '731-1460': 40, '>1460': 17}}, 'excluded': {'observed repeats': 156, 'entities with a repeat': 101, 'repeats by elapsed band': {'0-180': 44, '181-365': 34, '366-730': 24, '731-1460': 37, '>1460': 17}}, 'repeats involving an unresolved attribution': 17}}

## Same-Date Ties And Unknown Ordering

A same-date tie is far more likely one incident that deduplication has not joined than two incidents on one day. Every such pair, and every pair whose possible periods overlap, is an exception listed with the reason it is still two events in `dashboard/close_pairs.html`; the merging steps are fixed when one is a missed duplicate. Until a tie is resolved, each tie policy makes one stated assumption about it (every tie, with its incident ids and the length of any verified interval its incidents share, is in `entity_date_ties.csv`):

- `collapse` (primary): the tied incidents are one occurrence: one time point, never a repeat between them (their ids and count are kept).
- `zero_gap`: the tied incidents are distinct and one day apart, in id order: each after the first is a repeat after 1 day.
- `interval_spread`: the tied incidents are distinct and evenly spread across the verified interval they share (about interval length / n days apart, the first at the interval's start); a tie with no shared verified interval longer than a day is placed as under zero_gap.

None of these bounds the truth: a tie that is one incident filed twice is represented only by `collapse`; two incidents really weeks apart within a verified month are represented exactly by none; and a tie whose dates are unverified has no interval to place its incidents in, so `interval_spread` places it as `zero_gap` does. Agreement across the policies shows only that a result does not hinge on these assumptions. Every policy is adjusted for organisation size like the primary model.

- Under `collapse`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.031). It rests on 171 observed repeats.
- Under `zero_gap`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.022). It rests on 173 observed repeats.
- Under `interval_spread`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.022). It rests on 173 observed repeats.

| tie_policy      |   observed_repeats |   repeats_from_ties |   post_event_spells |   repeats_0-180 |   repeats_181-365 |   repeats_366-730 |   repeats_731-1460 |   repeats_>1460 |   adjusted_constant_hazard_p |   adjusted_0_90_91_180_banding_p |   delayed_peak_directional_p |
|:----------------|-------------------:|--------------------:|--------------------:|----------------:|------------------:|------------------:|-------------------:|----------------:|-----------------------------:|---------------------------------:|-----------------------------:|
| collapse        |                171 |                   0 |                1130 |              50 |                37 |                27 |                 40 |              17 |                      0.03077 |                          0.05871 |                       0.4864 |
| zero_gap        |                173 |                   2 |                1132 |              52 |                37 |                27 |                 40 |              17 |                      0.02153 |                          0.04149 |                       0.5941 |
| interval_spread |                173 |                   2 |                1132 |              52 |                37 |                27 |                 40 |              17 |                      0.02153 |                          0.04149 |                       0.5941 |


## Organisation Size Specification

The primary model enters size as a numeric score (SMALL=1 to HUGE=4, UNKNOWN mean-imputed, plus an UNKNOWN indicator), which assumes equal log-rate steps between bands. It is compared here with size as a categorical factor (reference: the band with the most repeats; bands with fewer than 3 repeats pooled) and, where every delayed-peak phase has at least 10 repeats at two or more known size bands, with the score's interaction with the delayed-peak phases. The score stays primary; the headline tests are re-run under categorical size. Full table: `size_specification_comparison.csv`.

- Size specification (bands 0-180, 181-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.623 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 1161.0 (categorical) against 1157.9 (score), lower for the score model.
- With bands 0-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.031 with the size score and p = 0.033 with categorical size: the same verdict at the 5% level.
- Size specification (bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.624 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 1229.3 (categorical) against 1226.2 (score), lower for the score model.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.059 with the size score and p = 0.063 with categorical size: the same verdict at the 5% level.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the directional delayed-peak test gives p = 0.486 with the size score and p = 0.487 with categorical size: the same verdict at the 5% level.
- Size by elapsed-time phase (the size score's slope in 91-180 and post-180 days against 0-90): LRT p = 0.006 on 2 df; at the 5% level the data reject a size effect that is the same in every phase.

| banding       | size_spec     |   log_likelihood |   parameters |   aic |   delta_aic_vs_score | nested   |   likelihood_ratio |   df |    p_value |   elapsed_constant_hazard_p |   delayed_peak_directional_p |
|:--------------|:--------------|-----------------:|-------------:|------:|---------------------:|:---------|-------------------:|-----:|-----------:|----------------------------:|-----------------------------:|
| run           | score         |           -553   |           26 |  1158 |                0     |          |           nan      |  nan | nan        |                     0.03077 |                     nan      |
| run           | categorical   |           -552.5 |           28 |  1161 |                3.055 | True     |             0.9449 |    2 |   0.6235   |                     0.033   |                     nan      |
| 0-90 / 91-180 | score         |           -586.1 |           27 |  1226 |                0     |          |           nan      |  nan | nan        |                     0.05871 |                       0.4864 |
| 0-90 / 91-180 | categorical   |           -585.6 |           29 |  1229 |                3.055 | True     |             0.9448 |    2 |   0.6235   |                     0.06257 |                       0.4865 |
| 0-90 / 91-180 | score_x_phase |           -581.1 |           29 |  1220 |               -6.086 | True     |            10.09   |    2 |   0.006455 |                   nan       |                     nan      |


## Primary Piecewise Exponential Test

The adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.031). It rests on 171 observed repeats.

- unadjusted LRT p-value: 0.002757
- adjusted LRT p-value: 0.03077
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=1), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-180          |       50 |      182410.000 |                      10.012 |              6.919 |              13.434 |
| 181-365        |       37 |      150743.000 |                       8.965 |              5.847 |              12.439 |
| 366-730        |       27 |      231473.000 |                       4.260 |              2.797 |               6.053 |
| 731-1460       |       40 |      225343.000 |                       6.483 |              4.679 |               8.547 |
| >1460          |       17 |       75511.000 |                       8.223 |              4.496 |              12.792 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        2.089 |    1.649 |     2.646 | about 109% higher repeat-event rate |
| SMALL -> LARGE  |        4.363 |    2.719 |     7.001 | about 336% higher repeat-event rate |
| SMALL -> HUGE   |        9.114 |    4.484 |    18.525 | about 811% higher repeat-event rate |
| MEDIUM -> HUGE  |        4.363 |    2.719 |     7.001 | about 336% higher repeat-event rate |
| LARGE -> HUGE   |        2.089 |    1.649 |     2.646 | about 109% higher repeat-event rate |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| education           |        2.776 |    0.937 |     8.225 | about 178% higher than government |
| telecom_transport   |        2.182 |    0.676 |     7.043 | about 118% higher than government |
| health              |        1.670 |    0.513 |     5.438 | about 67% higher than government  |
| industrial          |        1.636 |    0.444 |     6.023 | about 64% higher than government  |
| finance             |        1.431 |    0.435 |     4.708 | about 43% higher than government  |
| sparse_or_no_repeat |        1.401 |    0.252 |     7.784 | about 40% higher than government  |
| technology          |        1.043 |    0.331 |     3.290 | about 4% higher than government   |
| retail              |        0.792 |    0.194 |     3.233 | about 21% lower than government   |
| other               |        0.779 |    0.266 |     2.281 | about 22% lower than government   |

Full adjusted covariate table:

| effect                           |   rate_ratio |   ci_low |   ci_high |
|:---------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body     |        1.688 |    0.587 |     4.853 |
| Entity kind: sparse_or_no_repeat |        0.635 |    0.081 |     4.952 |
| Organisation size                |        2.089 |    1.649 |     2.646 |
| Prior records: 100k+             |        1.234 |    0.800 |     1.903 |
| Prior records: 10k-100k          |        1.177 |    0.738 |     1.876 |
| Prior records: 1k-10k            |        0.613 |    0.330 |     1.141 |
| Prior records: <1k               |        1.480 |    0.873 |     2.510 |
| Sector: education                |        2.776 |    0.937 |     8.225 |
| Sector: finance                  |        1.431 |    0.435 |     4.708 |
| Sector: health                   |        1.670 |    0.513 |     5.438 |
| Sector: industrial               |        1.636 |    0.444 |     6.023 |
| Sector: other                    |        0.779 |    0.266 |     2.281 |
| Sector: retail                   |        0.792 |    0.194 |     3.233 |
| Sector: sparse_or_no_repeat      |        1.401 |    0.252 |     7.784 |
| Sector: technology               |        1.043 |    0.331 |     3.290 |
| Sector: telecom_transport        |        2.182 |    0.676 |     7.043 |
| Unknown size                     |        0.272 |    0.036 |     2.033 |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 181-365, 366-730 days vs 0-180 days (unadjusted): rate ratio 0.61 (95% CI 0.42-0.88), about 39% lower; two-sided p = 0.009. Statistically significant at the 5% level: the data reject equal rates (rate ratio 1). The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 64 in 181-365, 366-730 days and 50 in 0-180 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.172. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates have the hypothesised shape. The weakest part is long-term vs response (p = 0.172).

- response vs immediate: rate ratio 0.73 (95% CI 0.50-1.07), no direction can be read from it; one-sided p = 0.052 (alternative: lower). Not statistically significant at the 5% level (borderline): the data do not reject the null of no decrease (rate ratio at least 1); the 95% CI runs from 0.50 to 1.07, which includes 1. Repeat events: 64 in the response period and 49 in the immediate period.
- long-term vs response: rate ratio 1.20 (95% CI 0.82-1.74), no direction can be read from it; one-sided p = 0.172 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.82 to 1.74, which includes 1. Repeat events: 57 in the long-term period and 64 in the response period.
- long-term vs immediate: rate ratio 0.88 (95% CI 0.58-1.33), no direction can be read from it; two-sided p = 0.530. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.58 to 1.33, which includes 1. Repeat events: 57 in the long-term period and 49 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.2511
- directional U-shape p-value: 0.1719

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       49 |          182410 |                       9.812 |
| Response period  |       64 |          382216 |                       6.116 |
| Long-term period |       57 |          300854 |                       6.920 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        0.731 |    0.501 |     1.067 | about 27% lower  |         0.104 | less                      | 0.052           |
| long-term vs response  |        1.197 |    0.825 |     1.739 | about 20% higher |         0.344 | greater                   | 0.172           |
| long-term vs immediate |        0.875 |    0.577 |     1.327 | about 12% lower  |         0.530 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        0.731 |    0.501 |     1.067 |      0.566 | about 27% lower  |           0.052 |
| All covariates                | long-term vs response |        1.197 |    0.825 |     1.739 |      0.914 | about 20% higher |           0.172 |
| Drop organisation size        | response vs immediate |        0.745 |    0.511 |     1.086 |      0.576 | about 26% lower  |           0.063 |
| Drop organisation size        | long-term vs response |        1.331 |    0.919 |     1.927 |      1.007 | about 33% higher |           0.065 |
| Drop industry/sector          | response vs immediate |        0.699 |    0.480 |     1.019 |      0.539 | about 30% lower  |           0.031 |
| Drop industry/sector          | long-term vs response |        1.185 |    0.818 |     1.715 |      0.897 | about 18% higher |           0.185 |
| Drop size and industry/sector | response vs immediate |        0.718 |    0.493 |     1.045 |      0.552 | about 28% lower  |           0.042 |
| Drop size and industry/sector | long-term vs response |        1.323 |    0.916 |     1.909 |      0.993 | about 32% higher |           0.068 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.2511 |                         0.1719 |
| Drop organisation size        |    0.1841 |                         0.0650 |
| Drop industry/sector          |    0.1763 |                         0.1846 |
| Drop size and industry/sector |    0.1486 |                         0.0677 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.50-1.07). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.82-1.74).

## Delayed 91-180 Day Peak Sensitivity

This sensitivity reruns the elapsed-time model with bands `0-90`, `91-180`, `181-365`, `366-730`, `731-1460`, and `>1460` to test whether the apparent early elevation is concentrated in days 91-180.
A formal delayed peak requires the 91-180 day period to be higher than both 0-90 days and the pooled post-180 period after adjustment.

- adjusted elapsed-time LRT p-value for the 90/180 split: 0.05871
- phase-model LRT p-value versus constant elapsed-time risk: 0.3922
- directional delayed-peak p-value: 0.4864

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |       26 |           96301 |                       9.861 |
| Delayed peak 91-180 days |       23 |           86109 |                       9.756 |
| Post-180 days            |      121 |          683070 |                       6.470 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        1.010 |    0.576 |     1.770 | about 1% higher  |         0.973 | greater                   | 0.486           |
| 91-180 vs post-180 |        1.285 |    0.815 |     2.028 | about 29% higher |         0.280 | greater                   | 0.140           |
| post-180 vs 0-90   |        0.786 |    0.507 |     1.216 | about 21% lower  |         0.279 |                           |                 |

Interpretation:

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.486. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates have the hypothesised shape. The weakest part is 91-180 vs 0-90 (p = 0.486).

- 91-180 vs 0-90: rate ratio 1.01 (95% CI 0.58-1.77), no direction can be read from it; one-sided p = 0.486 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.58 to 1.77, which includes 1. Repeat events: 23 in 91-180 days and 26 in 0-90 days.
- 91-180 vs post-180: rate ratio 1.29 (95% CI 0.81-2.03), no direction can be read from it; one-sided p = 0.140 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.81 to 2.03, which includes 1. Repeat events: 23 in 91-180 days and 121 in post-180 days.
- post-180 vs 0-90: rate ratio 0.79 (95% CI 0.51-1.22), no direction can be read from it; two-sided p = 0.279. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.51 to 1.22, which includes 1. Repeat events: 121 in post-180 days and 26 in 0-90 days.

## Unadjusted Parametric Survival Model Comparison (Descriptive)

Best AIC model: weibull. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| weibull           |        -1627.086 |            2 | 3258.172 | 3268.232 | True        |
| loglogistic       |        -1627.990 |            2 | 3259.979 | 3270.039 | True        |
| exponential       |        -1629.523 |            1 | 3261.047 | 3266.077 | True        |
| generalized_gamma |        -1629.513 |            3 | 3265.027 | 3280.117 | True        |
| lognormal         |        -1631.052 |            2 | 3266.105 | 3276.164 | True        |

Exponential vs Weibull LRT p-value: 0.02726

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
