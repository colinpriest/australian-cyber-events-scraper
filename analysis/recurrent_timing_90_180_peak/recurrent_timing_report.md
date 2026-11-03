# Repeat Cyber-Event Timing Analysis

## Executive Summary

**Is the data adequate for a reliable recurrence model?** Adequate for a simple model: 237 repeats, 26 per parameter of the models used here; but the full adjusted model is over-parameterised (28 parameters, 8.5 repeats each, 0 not identified), so it is not used for these conclusions; 61% of incident dates are unverified, so gaps can be resolved only to about a month: splits finer than 30 days are not attempted.

**What are the best splits for recurrence time bands?** Best bands: 0-365, 366-1095, >1095 days, with rates 1.00, 0.52, 1.06 times the first band's (very strong evidence: 11.5 BIC better than a constant rate, holding size, prior event number and a calendar trend; search-adjusted p <= 0.002 (no simulated value as extreme in 500)). The long-term growth in recorded incidents does not explain it: the per-organisation rate changes by +1% a year (yearly effects from 2020, p = 0.52), and the bands hold under no calendar adjustment, a linear trend, monthly reporting intensity and yearly effects alike (p from 0.0000 to 0.0000).

**Is the rate lower 1-90 days after an event (short-term vigilance)?** No evidence of short-term vigilance: the adjusted rate 1-90 days after an event is not lower than at other times (one-sided p = 0.902; 30 repeats against 24.4 expected if the rate were no different).

**Is there a delayed peak in recurrence 91-180 days after an event?** No evidence of a delayed peak: the adjusted rate 91-180 days after an event is not higher than at other times (one-sided p = 0.159; 27 repeats). The narrower 91-120 day window holds only 7 repeats, too few to test on its own.

**What fits recurrence rates best?** Best fit by AIC, holding size, prior event number and a calendar trend: bands 0-365, 366-1095, >1095. The constant rate is 18.4 AIC behind. The bands were chosen by the search, so that margin is optimistic.

**Does organisation size predict recurrence rates?** Yes: the recurrence rate rises with organisation size. Rate relative to SMALL organisations: MEDIUM 1.1, LARGE 2.4, HUGE 4.2 (monotone model holding prior event number and a calendar trend, bootstrap p <= 0.002 (no simulated value as extreme in 500)). Size stands for visibility and exposure as much as size: this is prediction, not cause.


## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.
The primary model includes recorded organisation size. Size is a proxy for differences in visibility, attack exposure and target value, not a direct measure of those factors; residual confounding remains possible. The ordinal size term assumes equal log-rate steps between categories (compared with categorical size and a size-by-elapsed-time interaction under Organisation Size Specification), UNKNOWN has a separate indicator, an organisation stored under several names takes the size its variants support best (`organisation_size_reconciliation.csv`), and current recorded size may differ from size at an incident's date. Unadjusted rates, plots, parametric models and size-dropped variants below are descriptive diagnostics and do not control for this confounding.

## Data Quality Summary

- victim event attributions: 1180
- unique deduplicated events: 1064
- victim entities: 940
- entities with more than one event: 140
- post-event spells: 1177
- observed repeat spells: 237
- right-censored spells: 940
- censor date: 2026-11-03
- elapsed-time bands: 0-90, 91-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2003-07-04
- maximum event date: 2026-11-01
- first-of-month event dates: 246
- verified incident-date precision: {'unverified': 725, 'day': 343, 'month': 112}
- tie policy (several incidents of one organisation on one date): collapse
- same-date ties: {'ties (organisation-dates with several incidents)': 3, 'incidents in ties': 6, 'organisations with a tie': 3, 'repeats a policy that separates tied incidents adds': 3, 'ties with a shared verified interval longer than a day (spread by interval_spread)': 0, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 0, 'ties: other_day': 3, 'repeats between tied incidents in this run': 0}
- incidents not ordered by their dates (different stored dates, overlapping verified intervals): {'consecutive pairs with overlapping verified intervals': 2, '... of which stored dates differ by under 90 days': 2}
- covariate coverage: {'size_estimate levels': {'HUGE': 320, 'LARGE': 364, 'MEDIUM': 325, 'SMALL': 130, 'UNKNOWN': 41}, 'size_confidence populated': 1180, 'employee_count populated': 1101, 'turnover populated': 0, 'industry populated': 1027, 'entity_kind populated': 1180, 'sector_proxy levels': {'education': 102, 'finance': 77, 'government': 228, 'health': 100, 'industrial': 36, 'media': 10, 'other': 411, 'retail': 52, 'technology': 112, 'telecom_transport': 52}, 'prior records populated': 490}
- organisation size across name variants: {'organisations stored under several name variants': 15, 'organisations whose size changed against the representative-only rule': 2, 'organisations whose variants give conflicting known size bands': 3, 'deciding rule': {'higher size_confidence': 10, 'representative variant': 3, 'known band over UNKNOWN': 2}}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 47, 'events with an unresolved attribution': 45, 'organisations with an unresolved attribution': 46, 'victim-event attributions analysed': 1180, 'repeats': {'included': {'observed repeats': 237, 'entities with a repeat': 139, 'repeats by elapsed band': {'0-90': 30, '91-180': 27, '181-365': 45, '366-730': 38, '731-1460': 51, '>1460': 46}}, 'excluded': {'observed repeats': 214, 'entities with a repeat': 129, 'repeats by elapsed band': {'0-90': 25, '91-180': 24, '181-365': 42, '366-730': 31, '731-1460': 49, '>1460': 43}}, 'repeats involving an unresolved attribution': 27}}

## Same-Date Ties And Unknown Ordering

A same-date tie is far more likely one incident that deduplication has not joined than two incidents on one day. Every such pair, and every pair whose possible periods overlap, is an exception listed with the reason it is still two events in `dashboard/close_pairs.html`; the merging steps are fixed when one is a missed duplicate. Until a tie is resolved, each tie policy makes one stated assumption about it (every tie, with its incident ids and the length of any verified interval its incidents share, is in `entity_date_ties.csv`):

- `collapse` (primary): the tied incidents are one occurrence: one time point, never a repeat between them (their ids and count are kept).
- `zero_gap`: the tied incidents are distinct and one day apart, in id order: each after the first is a repeat after 1 day.
- `interval_spread`: the tied incidents are distinct and evenly spread across the verified interval they share (about interval length / n days apart, the first at the interval's start); a tie with no shared verified interval longer than a day is placed as under zero_gap.

None of these bounds the truth: a tie that is one incident filed twice is represented only by `collapse`; two incidents really weeks apart within a verified month are represented exactly by none; and a tie whose dates are unverified has no interval to place its incidents in, so `interval_spread` places it as `zero_gap` does. Agreement across the policies shows only that a result does not hinge on these assumptions. Every policy is adjusted for organisation size like the primary model.

- Under `collapse`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.016). It rests on 237 observed repeats.
- Under `zero_gap`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.010). It rests on 240 observed repeats.
- Under `interval_spread`, the adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.010). It rests on 240 observed repeats.

| tie_policy      |   observed_repeats |   repeats_from_ties |   post_event_spells |   repeats_0-90 |   repeats_91-180 |   repeats_181-365 |   repeats_366-730 |   repeats_731-1460 |   repeats_>1460 |   adjusted_constant_hazard_p |   adjusted_0_90_91_180_banding_p |   delayed_peak_directional_p |
|:----------------|-------------------:|--------------------:|--------------------:|---------------:|-----------------:|------------------:|------------------:|-------------------:|----------------:|-----------------------------:|---------------------------------:|-----------------------------:|
| collapse        |                237 |                   0 |                1177 |             30 |               27 |                45 |                38 |                 51 |              46 |                     0.01622  |                         0.01622  |                       0.5656 |
| zero_gap        |                240 |                   3 |                1180 |             33 |               27 |                45 |                38 |                 51 |              46 |                     0.009588 |                         0.009588 |                       0.6991 |
| interval_spread |                240 |                   3 |                1180 |             33 |               27 |                45 |                38 |                 51 |              46 |                     0.009588 |                         0.009588 |                       0.6991 |


## Organisation Size Specification

The primary model enters size as a numeric score (SMALL=1 to HUGE=4, UNKNOWN mean-imputed, plus an UNKNOWN indicator), which assumes equal log-rate steps between bands. It is compared here with size as a categorical factor (reference: the band with the most repeats; bands with fewer than 3 repeats pooled) and, where every delayed-peak phase has at least 10 repeats at two or more known size bands, with the score's interaction with the delayed-peak phases. The score stays primary; the headline tests are re-run under categorical size. Full table: `size_specification_comparison.csv`.

- Size specification (bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460): categorical size against the numeric size score, LRT p = 0.312 on 2 df; at the 5% level the data do not reject equal log-rate steps between size bands; AIC 1623.2 (categorical) against 1621.5 (score), lower for the score model.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.016 with the size score and p = 0.018 with categorical size: the same verdict at the 5% level.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the directional delayed-peak test gives p = 0.566 with the size score and p = 0.568 with categorical size: the same verdict at the 5% level.
- Size by elapsed-time phase (the size score's slope in 91-180 and post-180 days against 0-90): LRT p = 0.033 on 2 df; at the 5% level the data reject a size effect that is the same in every phase.

| banding   | size_spec     |   log_likelihood |   parameters |   aic |   delta_aic_vs_score | nested   |   likelihood_ratio |   df |   p_value |   elapsed_constant_hazard_p |   delayed_peak_directional_p |
|:----------|:--------------|-----------------:|-------------:|------:|---------------------:|:---------|-------------------:|-----:|----------:|----------------------------:|-----------------------------:|
| run       | score         |           -782.7 |           28 |  1621 |                0     |          |            nan     |  nan | nan       |                     0.01622 |                       0.5656 |
| run       | categorical   |           -781.6 |           30 |  1623 |                1.669 | True     |              2.331 |    2 |   0.3117  |                     0.01783 |                       0.5678 |
| run       | score_x_phase |           -779.3 |           30 |  1619 |               -2.85  | True     |              6.85  |    2 |   0.03255 |                   nan       |                     nan      |


## Primary Piecewise Exponential Test

The adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level (LRT p = 0.016). It rests on 237 observed repeats.

- unadjusted LRT p-value: 0.004994
- adjusted LRT p-value: 0.01622
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=1), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |       30 |      100575.000 |                      10.895 |              6.730 |              15.202 |
| 91-180         |       27 |       90335.000 |                      10.917 |              6.348 |              15.984 |
| 181-365        |       45 |      159511.000 |                      10.304 |              7.098 |              14.070 |
| 366-730        |       38 |      245534.000 |                       5.653 |              3.924 |               7.762 |
| 731-1460       |       51 |      256743.000 |                       7.255 |              5.274 |               9.725 |
| >1460          |       46 |      148924.000 |                      11.282 |              8.359 |              14.435 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        1.776 |    1.468 |     2.149 | about 78% higher repeat-event rate  |
| SMALL -> LARGE  |        3.155 |    2.155 |     4.620 | about 216% higher repeat-event rate |
| SMALL -> HUGE   |        5.605 |    3.164 |     9.930 | about 461% higher repeat-event rate |
| MEDIUM -> HUGE  |        3.155 |    2.155 |     4.620 | about 216% higher repeat-event rate |
| LARGE -> HUGE   |        1.776 |    1.468 |     2.149 | about 78% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| education           |        2.651 |    1.145 |     6.140 | about 165% higher than government |
| industrial          |        2.021 |    0.717 |     5.697 | about 102% higher than government |
| telecom_transport   |        1.751 |    0.709 |     4.323 | about 75% higher than government  |
| finance             |        1.556 |    0.652 |     3.715 | about 56% higher than government  |
| sparse_or_no_repeat |        1.226 |    0.252 |     5.961 | about 23% higher than government  |
| health              |        1.150 |    0.451 |     2.929 | about 15% higher than government  |
| technology          |        0.884 |    0.365 |     2.143 | about 12% lower than government   |
| retail              |        0.731 |    0.229 |     2.335 | about 27% lower than government   |
| other               |        0.598 |    0.257 |     1.392 | about 40% lower than government   |

Full adjusted covariate table:

| effect                           |   rate_ratio |   ci_low |   ci_high |
|:---------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body     |        1.671 |    0.747 |     3.738 |
| Entity kind: sparse_or_no_repeat |        0.604 |    0.080 |     4.554 |
| Organisation size                |        1.776 |    1.468 |     2.149 |
| Prior records: 100k+             |        1.207 |    0.825 |     1.765 |
| Prior records: 10k-100k          |        1.240 |    0.846 |     1.817 |
| Prior records: 1k-10k            |        0.802 |    0.496 |     1.298 |
| Prior records: <1k               |        0.972 |    0.598 |     1.581 |
| Sector: education                |        2.651 |    1.145 |     6.140 |
| Sector: finance                  |        1.556 |    0.652 |     3.715 |
| Sector: health                   |        1.150 |    0.451 |     2.929 |
| Sector: industrial               |        2.021 |    0.717 |     5.697 |
| Sector: other                    |        0.598 |    0.257 |     1.392 |
| Sector: retail                   |        0.731 |    0.229 |     2.335 |
| Sector: sparse_or_no_repeat      |        1.226 |    0.252 |     5.961 |
| Sector: technology               |        0.884 |    0.365 |     2.143 |
| Sector: telecom_transport        |        1.751 |    0.709 |     4.323 |
| Unknown size                     |        0.226 |    0.031 |     1.650 |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 91-180, 181-365 days vs 0-90 days (unadjusted): rate ratio 0.97 (95% CI 0.63-1.48), no direction can be read from it; two-sided p = 0.874. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.63 to 1.48, which includes 1. Repeat events: 72 in 91-180, 181-365 days and 30 in 0-90 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.966. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates do not have the hypothesised shape: long-term vs response goes the other way.

- response vs immediate: rate ratio 0.97 (95% CI 0.63-1.49), no direction can be read from it; one-sided p = 0.444 (alternative: lower). Not statistically significant at the 5% level: the data do not reject the null of no decrease (rate ratio at least 1); the 95% CI runs from 0.63 to 1.49, which includes 1. Repeat events: 71 in the response period and 30 in the immediate period.
- long-term vs response: rate ratio 0.76 (95% CI 0.57-1.02), no direction can be read from it; one-sided p = 0.966 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.57 to 1.02, which includes 1. Repeat events: 134 in the long-term period and 71 in the response period.
- long-term vs immediate: rate ratio 0.74 (95% CI 0.50-1.10), no direction can be read from it; two-sided p = 0.139. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.50 to 1.10, which includes 1. Repeat events: 134 in the long-term period and 30 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.1158
- directional U-shape p-value: 0.966

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       30 |          100575 |                      10.895 |
| Response period  |       71 |          249846 |                      10.379 |
| Long-term period |      134 |          651201 |                       7.516 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        0.970 |    0.633 |     1.487 | about 3% lower   |         0.888 | less                      | 0.444           |
| long-term vs response  |        0.762 |    0.569 |     1.020 | about 24% lower  |         0.068 | greater                   | 0.966           |
| long-term vs immediate |        0.739 |    0.495 |     1.103 | about 26% lower  |         0.139 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        0.970 |    0.633 |     1.487 |      0.854 | about 3% lower   |           0.444 |
| All covariates                | long-term vs response |        0.762 |    0.569 |     1.020 |      0.451 | about 24% lower  |           0.966 |
| Drop organisation size        | response vs immediate |        0.983 |    0.641 |     1.507 |      0.866 | about 2% lower   |           0.469 |
| Drop organisation size        | long-term vs response |        0.792 |    0.592 |     1.060 |      0.468 | about 21% lower  |           0.941 |
| Drop industry/sector          | response vs immediate |        0.958 |    0.625 |     1.468 |      0.843 | about 4% lower   |           0.422 |
| Drop industry/sector          | long-term vs response |        0.752 |    0.562 |     1.006 |      0.444 | about 25% lower  |           0.972 |
| Drop size and industry/sector | response vs immediate |        0.971 |    0.633 |     1.488 |      0.854 | about 3% lower   |           0.446 |
| Drop size and industry/sector | long-term vs response |        0.792 |    0.592 |     1.058 |      0.466 | about 21% lower  |           0.943 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.1158 |                         0.9660 |
| Drop organisation size        |    0.2113 |                         0.9414 |
| Drop industry/sector          |    0.0866 |                         0.9725 |
| Drop size and industry/sector |    0.1934 |                         0.9428 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.63-1.49). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.57-1.02).

## Delayed 91-180 Day Peak Test

Definition used here: the hypothesised peak is 91-180 days after the prior event. A formal peak requires 91-180 days to be higher than both 0-90 days and the pooled post-180 period.
The adjusted peak model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main model.

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.566. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates do not have the hypothesised shape: 91-180 vs 0-90 goes the other way.

- 91-180 vs 0-90: rate ratio 0.96 (95% CI 0.57-1.62), no direction can be read from it; one-sided p = 0.566 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.57 to 1.62, which includes 1. Repeat events: 26 in 91-180 days and 30 in 0-90 days.
- 91-180 vs post-180: rate ratio 1.21 (95% CI 0.80-1.83), no direction can be read from it; one-sided p = 0.185 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.80 to 1.83, which includes 1. Repeat events: 26 in 91-180 days and 179 in post-180 days.
- post-180 vs 0-90: rate ratio 0.79 (95% CI 0.54-1.17), no direction can be read from it; two-sided p = 0.240. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.54 to 1.17, which includes 1. Repeat events: 179 in post-180 days and 30 in 0-90 days.

- phase-model LRT p-value versus constant elapsed-time risk: 0.3973
- directional delayed-peak p-value: 0.5656

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |       30 |          100575 |                      10.895 |
| Delayed peak 91-180 days |       26 |           90335 |                      10.513 |
| Post-180 days            |      179 |          810712 |                       8.064 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        0.957 |    0.566 |     1.618 | about 4% lower   |         0.869 | greater                   | 0.566           |
| 91-180 vs post-180 |        1.209 |    0.799 |     1.829 | about 21% higher |         0.369 | greater                   | 0.185           |
| post-180 vs 0-90   |        0.791 |    0.536 |     1.169 | about 21% lower  |         0.240 |                           |                 |

## Unadjusted Parametric Survival Model Comparison (Descriptive)

Best AIC model: generalized_gamma. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| generalized_gamma |        -2209.271 |            3 | 4424.543 | 4439.755 | True        |
| exponential       |        -2215.730 |            1 | 4433.460 | 4438.530 | True        |
| weibull           |        -2215.325 |            2 | 4434.650 | 4444.792 | True        |
| loglogistic       |        -2221.328 |            2 | 4446.656 | 4456.797 | True        |
| lognormal         |        -2232.893 |            2 | 4469.785 | 4479.927 | True        |

Exponential vs Weibull LRT p-value: 0.3683

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
