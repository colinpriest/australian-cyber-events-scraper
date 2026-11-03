# Repeat Cyber-Event Timing Analysis

## Executive Summary

**Is the data adequate for a reliable recurrence model?** Adequate for a simple model: 171 repeats, 19 per parameter of the models used here; but the full adjusted model is over-parameterised (26 parameters, 6.6 repeats each, 0 not identified), so it is not used for these conclusions; 62% of incident dates are unverified, so gaps can be resolved only to about a month: splits finer than 30 days are not attempted.

**What are the best splits for recurrence time bands?** Best bands: 0-365, 366-1095, >1095 days, with rates 1.00, 0.45, 1.03 times the first band's (very strong evidence: 12.5 BIC better than a constant rate, holding size, prior event number and a calendar trend; search-adjusted p <= 0.002 (no simulated value as extreme in 500)). The long-term growth in recorded incidents does not explain it: the per-organisation rate changes by -5% a year (yearly effects from 2020, p = 0.84), and the bands hold under no calendar adjustment, a linear trend, monthly reporting intensity and yearly effects alike (p from 0.0000 to 0.0000).

**Is the rate lower 1-90 days after an event (short-term vigilance)?** No evidence of short-term vigilance: the adjusted rate 1-90 days after an event is not lower than at other times (one-sided p = 0.951; 28 repeats against 21.2 expected if the rate were no different).

**Is there a delayed peak in recurrence 91-180 days after an event?** No evidence of a delayed peak: the adjusted rate 91-180 days after an event is not higher than at other times (one-sided p = 0.127; 24 repeats). The narrower 91-120 day window holds only 7 repeats, too few to test on its own.

**What fits recurrence rates best?** Best fit by AIC, holding size, prior event number and a calendar trend: bands 0-365, 366-1095, >1095. The constant rate is 18.9 AIC behind. The bands were chosen by the search, so that margin is optimistic.

**Does organisation size predict recurrence rates?** Yes: the recurrence rate rises with organisation size. Rate relative to SMALL organisations: MEDIUM 1.1, LARGE 2.9, HUGE 6.1 (monotone model holding prior event number and a calendar trend, bootstrap p <= 0.002 (no simulated value as extreme in 500)). Size stands for visibility and exposure as much as size: this is prediction, not cause.


## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.
The primary model includes recorded organisation size. Size is a proxy for differences in visibility, attack exposure and target value, not a direct measure of those factors; residual confounding remains possible. The ordinal size term assumes equal log-rate steps between categories (compared with categorical size and a size-by-elapsed-time interaction under Organisation Size Specification), UNKNOWN has a separate indicator, an organisation stored under several names takes the size its variants support best (`organisation_size_reconciliation.csv`), and current recorded size may differ from size at an incident's date. Unadjusted rates, plots, parametric models and size-dropped variants below are descriptive diagnostics and do not control for this confounding.

## Data Quality Summary

- victim event attributions: 1097
- unique deduplicated events: 982
- victim entities: 923
- entities with more than one event: 111
- post-event spells: 1094
- observed repeat spells: 171
- right-censored spells: 923
- observation window (events before its start are excluded): {'observation start': '2020-01-01', 'victim event attributions before it (excluded)': 83, 'events before it (excluded)': 82, 'organisations with events only before it (excluded)': 19}
- censor date: 2026-11-03
- elapsed-time bands: 0-90, 91-365, 366-730, 731-1460, >1460
- minimum event date: 2020-01-01
- maximum event date: 2026-11-01
- first-of-month event dates: 214
- verified incident-date precision: {'unverified': 677, 'day': 321, 'month': 99}
- tie policy (several incidents of one organisation on one date): collapse
- same-date ties: {'ties (organisation-dates with several incidents)': 3, 'incidents in ties': 6, 'organisations with a tie': 3, 'repeats a policy that separates tied incidents adds': 3, 'ties with a shared verified interval longer than a day (spread by interval_spread)': 0, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 0, 'ties: other_day': 3, 'repeats between tied incidents in this run': 0}
- incidents not ordered by their dates (different stored dates, overlapping verified intervals): {'consecutive pairs with overlapping verified intervals': 2, '... of which stored dates differ by under 90 days': 2}
- covariate coverage: {'size_estimate levels': {'HUGE': 273, 'LARGE': 340, 'MEDIUM': 316, 'SMALL': 126, 'UNKNOWN': 42}, 'size_confidence populated': 1096, 'employee_count populated': 1018, 'turnover populated': 0, 'industry populated': 947, 'entity_kind populated': 1097, 'sector_proxy levels': {'education': 97, 'finance': 61, 'government': 198, 'health': 96, 'industrial': 34, 'media': 10, 'other': 404, 'retail': 51, 'technology': 102, 'telecom_transport': 44}, 'prior records populated': 451}
- organisation size across name variants: {'organisations stored under several name variants': 15, 'organisations whose size changed against the representative-only rule': 2, 'organisations whose variants give conflicting known size bands': 3, 'deciding rule': {'higher size_confidence': 10, 'representative variant': 3, 'known band over UNKNOWN': 2}}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 39, 'events with an unresolved attribution': 37, 'organisations with an unresolved attribution': 39, 'victim-event attributions analysed': 1097, 'repeats': {'included': {'observed repeats': 171, 'entities with a repeat': 110, 'repeats by elapsed band': {'0-90': 28, '91-365': 64, '366-730': 26, '731-1460': 36, '>1460': 17}}, 'excluded': {'observed repeats': 156, 'entities with a repeat': 103, 'repeats by elapsed band': {'0-90': 23, '91-365': 59, '366-730': 22, '731-1460': 35, '>1460': 17}}, 'repeats involving an unresolved attribution': 17}}

## Same-Date Ties And Unknown Ordering

A same-date tie is far more likely one incident that deduplication has not joined than two incidents on one day. Every such pair, and every pair whose possible periods overlap, is an exception listed with the reason it is still two events in `dashboard/close_pairs.html`; the merging steps are fixed when one is a missed duplicate. Until a tie is resolved, each tie policy makes one stated assumption about it (every tie, with its incident ids and the length of any verified interval its incidents share, is in `entity_date_ties.csv`):

- `collapse` (primary): the tied incidents are one occurrence: one time point, never a repeat between them (their ids and count are kept).
- `zero_gap`: the tied incidents are distinct and one day apart, in id order: each after the first is a repeat after 1 day.
- `interval_spread`: the tied incidents are distinct and evenly spread across the verified interval they share (about interval length / n days apart, the first at the interval's start); a tie with no shared verified interval longer than a day is placed as under zero_gap.

None of these bounds the truth: a tie that is one incident filed twice is represented only by `collapse`; two incidents really weeks apart within a verified month are represented exactly by none; and a tie whose dates are unverified has no interval to place its incidents in, so `interval_spread` places it as `zero_gap` does. Agreement across the policies shows only that a result does not hinge on these assumptions. Every policy is adjusted for organisation size like the primary model.

- Under `collapse`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.004). It rests on 171 observed repeats.
- Under `zero_gap`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.002). It rests on 174 observed repeats.
- Under `interval_spread`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.002). It rests on 174 observed repeats.

| tie_policy      |   observed_repeats |   repeats_from_ties |   post_event_spells |   repeats_0-90 |   repeats_91-365 |   repeats_366-730 |   repeats_731-1460 |   repeats_>1460 |   adjusted_constant_hazard_p |   adjusted_0_90_91_180_banding_p |   delayed_peak_directional_p |
|:----------------|-------------------:|--------------------:|--------------------:|---------------:|-----------------:|------------------:|-------------------:|----------------:|-----------------------------:|---------------------------------:|-----------------------------:|
| collapse        |                171 |                   0 |                1094 |             28 |               64 |                26 |                 36 |              17 |                     0.003691 |                         0.008051 |                       0.616  |
| zero_gap        |                174 |                   3 |                1097 |             31 |               64 |                26 |                 36 |              17 |                     0.00186  |                         0.004218 |                       0.7438 |
| interval_spread |                174 |                   3 |                1097 |             31 |               64 |                26 |                 36 |              17 |                     0.00186  |                         0.004218 |                       0.7438 |


## Organisation Size Specification

The primary model enters size as a numeric score (SMALL=1 to HUGE=4, UNKNOWN mean-imputed, plus an UNKNOWN indicator), which assumes equal log-rate steps between bands. It is compared here with size as a categorical factor (reference: the band with the most repeats; bands with fewer than 3 repeats pooled) and, where every delayed-peak phase has at least 10 repeats at two or more known size bands, with the score's interaction with the delayed-peak phases. The score stays primary; the headline tests are re-run under categorical size. Full table: `size_specification_comparison.csv`.

- Size specification (bands 0-90, 91-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.365 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 1164.9 (categorical) against 1162.9 (score), lower for the score model.
- With bands 0-90, 91-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.004 with the size score and p = 0.004 with categorical size: the same verdict at the 5% level.
- Size specification (bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.365 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 1236.2 (categorical) against 1234.2 (score), lower for the score model.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.008 with the size score and p = 0.009 with categorical size: the same verdict at the 5% level.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the directional delayed-peak test gives p = 0.616 with the size score and p = 0.618 with categorical size: the same verdict at the 5% level.
- Size by elapsed-time phase (the size score's slope in 91-180 and post-180 days against 0-90): LRT p = 0.005 on 2 df; at the 5% level the data reject a size effect that is the same in every phase.

| banding       | size_spec     |   log_likelihood |   parameters |   aic |   delta_aic_vs_score | nested   |   likelihood_ratio |   df |    p_value |   elapsed_constant_hazard_p |   delayed_peak_directional_p |
|:--------------|:--------------|-----------------:|-------------:|------:|---------------------:|:---------|-------------------:|-----:|-----------:|----------------------------:|-----------------------------:|
| run           | score         |           -555.5 |           26 |  1163 |                0     |          |            nan     |  nan | nan        |                    0.003691 |                     nan      |
| run           | categorical   |           -554.5 |           28 |  1165 |                1.983 | True     |              2.017 |    2 |   0.3648   |                    0.004055 |                     nan      |
| 0-90 / 91-180 | score         |           -590.1 |           27 |  1234 |                0     |          |            nan     |  nan | nan        |                    0.008051 |                       0.616  |
| 0-90 / 91-180 | categorical   |           -589.1 |           29 |  1236 |                1.983 | True     |              2.017 |    2 |   0.3647   |                    0.008792 |                       0.6181 |
| 0-90 / 91-180 | score_x_phase |           -584.8 |           29 |  1228 |               -6.68  | True     |             10.68  |    2 |   0.004795 |                  nan        |                     nan      |


## Primary Piecewise Exponential Test

The adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.004). It rests on 171 observed repeats.

- unadjusted LRT p-value: 0.0003393
- adjusted LRT p-value: 0.003691
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=1), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |       28 |       93174.000 |                      10.976 |              7.147 |              15.131 |
| 91-365         |       64 |      228627.000 |                      10.225 |              7.310 |              13.045 |
| 366-730        |       26 |      220813.000 |                       4.301 |              2.730 |               6.090 |
| 731-1460       |       36 |      216322.000 |                       6.078 |              4.200 |               8.398 |
| >1460          |       17 |       73561.000 |                       8.441 |              4.431 |              13.701 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        2.066 |    1.639 |     2.603 | about 107% higher repeat-event rate |
| SMALL -> LARGE  |        4.267 |    2.688 |     6.773 | about 327% higher repeat-event rate |
| SMALL -> HUGE   |        8.813 |    4.406 |    17.629 | about 781% higher repeat-event rate |
| MEDIUM -> HUGE  |        4.267 |    2.688 |     6.773 | about 327% higher repeat-event rate |
| LARGE -> HUGE   |        2.066 |    1.639 |     2.603 | about 107% higher repeat-event rate |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| education           |        2.770 |    0.936 |     8.196 | about 177% higher than government |
| telecom_transport   |        2.234 |    0.706 |     7.072 | about 123% higher than government |
| industrial          |        1.883 |    0.513 |     6.919 | about 88% higher than government  |
| health              |        1.610 |    0.501 |     5.177 | about 61% higher than government  |
| finance             |        1.590 |    0.489 |     5.171 | about 59% higher than government  |
| sparse_or_no_repeat |        1.438 |    0.259 |     7.999 | about 44% higher than government  |
| technology          |        0.980 |    0.310 |     3.101 | about 2% lower than government    |
| other               |        0.741 |    0.251 |     2.185 | about 26% lower than government   |
| retail              |        0.727 |    0.178 |     2.963 | about 27% lower than government   |

Full adjusted covariate table:

| effect                           |   rate_ratio |   ci_low |   ci_high |
|:---------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body     |        1.680 |    0.583 |     4.843 |
| Entity kind: sparse_or_no_repeat |        0.677 |    0.088 |     5.235 |
| Organisation size                |        2.066 |    1.639 |     2.603 |
| Prior records: 100k+             |        1.416 |    0.923 |     2.173 |
| Prior records: 10k-100k          |        1.395 |    0.885 |     2.199 |
| Prior records: 1k-10k            |        0.784 |    0.431 |     1.426 |
| Prior records: <1k               |        1.132 |    0.644 |     1.988 |
| Sector: education                |        2.770 |    0.936 |     8.196 |
| Sector: finance                  |        1.590 |    0.489 |     5.171 |
| Sector: health                   |        1.610 |    0.501 |     5.177 |
| Sector: industrial               |        1.883 |    0.513 |     6.919 |
| Sector: other                    |        0.741 |    0.251 |     2.185 |
| Sector: retail                   |        0.727 |    0.178 |     2.963 |
| Sector: sparse_or_no_repeat      |        1.438 |    0.259 |     7.999 |
| Sector: technology               |        0.980 |    0.310 |     3.101 |
| Sector: telecom_transport        |        2.234 |    0.706 |     7.072 |
| Unknown size                     |        0.249 |    0.034 |     1.830 |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 91-365, 366-730 days vs 0-90 days (unadjusted): rate ratio 0.67 (95% CI 0.44-1.02), no direction can be read from it; two-sided p = 0.061. Not statistically significant at the 5% level (borderline): the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.44 to 1.02, which includes 1. Repeat events: 90 in 91-365, 366-730 days and 28 in 0-90 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.519. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates do not have the hypothesised shape: long-term vs response goes the other way.

- response vs immediate: rate ratio 0.73 (95% CI 0.47-1.11), no direction can be read from it; one-sided p = 0.071 (alternative: lower). Not statistically significant at the 5% level (borderline): the data do not reject the null of no decrease (rate ratio at least 1); the 95% CI runs from 0.47 to 1.11, which includes 1. Repeat events: 89 in the response period and 28 in the immediate period.
- long-term vs response: rate ratio 0.99 (95% CI 0.69-1.42), no direction can be read from it; one-sided p = 0.519 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.69 to 1.42, which includes 1. Repeat events: 53 in the long-term period and 89 in the response period.
- long-term vs immediate: rate ratio 0.72 (95% CI 0.44-1.17), no direction can be read from it; two-sided p = 0.184. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.44 to 1.17, which includes 1. Repeat events: 53 in the long-term period and 28 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.3353
- directional U-shape p-value: 0.5194

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       28 |           93174 |                      10.976 |
| Response period  |       89 |          449440 |                       7.233 |
| Long-term period |       53 |          289883 |                       6.678 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        0.726 |    0.473 |     1.113 | about 27% lower  |         0.142 | less                      | 0.071           |
| long-term vs response  |        0.991 |    0.691 |     1.421 | about 1% lower   |         0.961 | greater                   | 0.519           |
| long-term vs immediate |        0.719 |    0.442 |     1.170 | about 28% lower  |         0.184 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        0.726 |    0.473 |     1.113 |      0.640 | about 27% lower  |           0.071 |
| All covariates                | long-term vs response |        0.991 |    0.691 |     1.421 |      0.730 | about 1% lower   |           0.519 |
| Drop organisation size        | response vs immediate |        0.740 |    0.483 |     1.135 |      0.652 | about 26% lower  |           0.084 |
| Drop organisation size        | long-term vs response |        1.087 |    0.761 |     1.552 |      0.791 | about 9% higher  |           0.324 |
| Drop industry/sector          | response vs immediate |        0.706 |    0.460 |     1.082 |      0.622 | about 29% lower  |           0.055 |
| Drop industry/sector          | long-term vs response |        0.992 |    0.694 |     1.418 |      0.724 | about 1% lower   |           0.517 |
| Drop size and industry/sector | response vs immediate |        0.721 |    0.470 |     1.105 |      0.634 | about 28% lower  |           0.066 |
| Drop size and industry/sector | long-term vs response |        1.080 |    0.758 |     1.538 |      0.779 | about 8% higher  |           0.335 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.3353 |                         0.5194 |
| Drop organisation size        |    0.4005 |                         0.3239 |
| Drop industry/sector          |    0.2767 |                         0.5175 |
| Drop size and industry/sector |    0.3431 |                         0.3348 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.47-1.11). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.69-1.42).

## Delayed 91-180 Day Peak Sensitivity

This sensitivity reruns the elapsed-time model with bands `0-90`, `91-180`, `181-365`, `366-730`, `731-1460`, and `>1460` to test whether the apparent early elevation is concentrated in days 91-180.
A formal delayed peak requires the 91-180 day period to be higher than both 0-90 days and the pooled post-180 period after adjustment.

- adjusted elapsed-time LRT p-value for the 90/180 split: 0.008051
- phase-model LRT p-value versus constant elapsed-time risk: 0.1593
- directional delayed-peak p-value: 0.616

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |       28 |           93174 |                      10.976 |
| Delayed peak 91-180 days |       23 |           83143 |                      10.104 |
| Post-180 days            |      119 |          656180 |                       6.624 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        0.920 |    0.530 |     1.598 | about 8% lower   |         0.768 | greater                   | 0.616           |
| 91-180 vs post-180 |        1.340 |    0.850 |     2.114 | about 34% higher |         0.208 | greater                   | 0.104           |
| post-180 vs 0-90   |        0.687 |    0.449 |     1.049 | about 31% lower  |         0.082 |                           |                 |

Interpretation:

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.616. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates do not have the hypothesised shape: 91-180 vs 0-90 goes the other way.

- 91-180 vs 0-90: rate ratio 0.92 (95% CI 0.53-1.60), no direction can be read from it; one-sided p = 0.616 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.53 to 1.60, which includes 1. Repeat events: 23 in 91-180 days and 28 in 0-90 days.
- 91-180 vs post-180: rate ratio 1.34 (95% CI 0.85-2.11), no direction can be read from it; one-sided p = 0.104 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.85 to 2.11, which includes 1. Repeat events: 23 in 91-180 days and 119 in post-180 days.
- post-180 vs 0-90: rate ratio 0.69 (95% CI 0.45-1.05), no direction can be read from it; two-sided p = 0.082. Not statistically significant at the 5% level (borderline): the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.45 to 1.05, which includes 1. Repeat events: 119 in post-180 days and 28 in 0-90 days.

## Unadjusted Parametric Survival Model Comparison (Descriptive)

Best AIC model: weibull. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| weibull           |        -1618.685 |            2 | 3241.370 | 3251.366 | True        |
| loglogistic       |        -1619.523 |            2 | 3243.045 | 3253.041 | True        |
| generalized_gamma |        -1618.858 |            3 | 3243.715 | 3258.708 | True        |
| exponential       |        -1622.879 |            1 | 3247.758 | 3252.756 | True        |
| lognormal         |        -1623.058 |            2 | 3250.116 | 3260.111 | True        |

Exponential vs Weibull LRT p-value: 0.003777

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
