# Repeat Cyber-Event Timing Analysis

## Executive Summary

**Is the data adequate for a reliable recurrence model?** Adequate for a simple model: 140 repeats, 13 per parameter of the models used here; but the full adjusted model is over-parameterised (26 parameters, 5.4 repeats each, 11 not identified), so it is not used for these conclusions; 56% of incident dates are unverified, so gaps can be resolved only to about a month: splits finer than 30 days are not attempted.

**What are the best splits for recurrence time bands?** Best bands: 0-365, 366-1095, >1095 days, with 7.8, 3.5, 7.9 repeats per 100 organisation-years (positive evidence: 4.3 BIC better than a constant rate, holding size, prior event number and calendar period; search-adjusted p = 0.018).

**Is there a delayed peak in recurrence 90-120 days after an event?** Cannot be tested reliably: only 5 repeats fall 91-120 days after an event (at least 10 are needed).

**What fits recurrence rates best?** Best fit by AIC: piecewise 0-365, 366-1095, >1095. Unadjusted, on every spell, the constant rate is 14.4 AIC behind; the bands were chosen by the search, so that margin is optimistic.

**Does organisation size predict recurrence rates?** Yes: the recurrence rate rises with organisation size. Rate relative to SMALL organisations: MEDIUM 1.0, LARGE 5.9, HUGE 14.6 (monotone model holding prior event number and calendar period, bootstrap p <= 0.002 (no simulated value as extreme in 500)). Size stands for visibility and exposure as much as size: this is prediction, not cause.


## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.
The primary model includes recorded organisation size. Size is a proxy for differences in visibility, attack exposure and target value, not a direct measure of those factors; residual confounding remains possible. The ordinal size term assumes equal log-rate steps between categories (compared with categorical size and a size-by-elapsed-time interaction under Organisation Size Specification), UNKNOWN has a separate indicator, an organisation stored under several names takes the size its variants support best (`organisation_size_reconciliation.csv`), and current recorded size may differ from size at an incident's date. Unadjusted rates, plots, parametric models and size-dropped variants below are descriptive diagnostics and do not control for this confounding.

## Data Quality Summary

- victim event attributions: 1008
- unique deduplicated events: 901
- victim entities: 868
- entities with more than one event: 76
- post-event spells: 1008
- observed repeat spells: 140
- right-censored spells: 868
- censor date: 2026-11-01
- elapsed-time bands: 0-90, 91-365, 366-730, 731-1460, >1460
- minimum event date: 2003-07-04
- maximum event date: 2026-10-26
- first-of-month event dates: 212
- verified incident-date precision: {'unverified': 561, 'day': 264, 'month': 183}
- tie policy (several incidents of one organisation on one date): collapse
- same-date ties: {'ties (organisation-dates with several incidents)': 0, 'incidents in ties': 0, 'organisations with a tie': 0, 'repeats a policy that separates tied incidents adds': 0, 'ties with a shared verified interval longer than a day (spread by interval_spread)': 0, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 0, 'ties: other_day': 0, 'repeats between tied incidents in this run': 0}
- incidents not ordered by their dates (different stored dates, overlapping verified intervals): {'consecutive pairs with overlapping verified intervals': 1, '... of which stored dates differ by under 90 days': 1}
- covariate coverage: {'size_estimate levels': {'HUGE': 267, 'LARGE': 296, 'MEDIUM': 288, 'SMALL': 112, 'UNKNOWN': 45}, 'size_confidence populated': 1008, 'employee_count populated': 931, 'turnover populated': 0, 'industry populated': 883, 'entity_kind populated': 1008, 'sector_proxy levels': {'education': 87, 'finance': 63, 'government': 176, 'health': 90, 'industrial': 28, 'media': 7, 'other': 348, 'retail': 45, 'technology': 116, 'telecom_transport': 48}, 'prior records populated': 436}
- organisation size across name variants: {'organisations stored under several name variants': 6, 'organisations whose size changed against the representative-only rule': 1, 'organisations whose variants give conflicting known size bands': 1, 'deciding rule': {'higher size_confidence': 3, 'known band over UNKNOWN': 3}}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 40, 'events with an unresolved attribution': 38, 'organisations with an unresolved attribution': 40, 'victim-event attributions analysed': 1008, 'repeats': {'included': {'observed repeats': 140, 'entities with a repeat': 76, 'repeats by elapsed band': {'0-90': 17, '91-365': 47, '366-730': 23, '731-1460': 30, '>1460': 23}}, 'excluded': {'observed repeats': 126, 'entities with a repeat': 72, 'repeats by elapsed band': {'0-90': 15, '91-365': 41, '366-730': 17, '731-1460': 30, '>1460': 23}}, 'repeats involving an unresolved attribution': 18}}

## Same-Date Ties And Unknown Ordering

A same-date tie is far more likely one incident that deduplication has not joined than two incidents on one day. Every such pair, and every pair whose possible periods overlap, is an exception listed with the reason it is still two events in `dashboard/close_pairs.html`; the merging steps are fixed when one is a missed duplicate. Until a tie is resolved, each tie policy makes one stated assumption about it (every tie, with its incident ids and the length of any verified interval its incidents share, is in `entity_date_ties.csv`):

- `collapse` (primary): the tied incidents are one occurrence: one time point, never a repeat between them (their ids and count are kept).
- `zero_gap`: the tied incidents are distinct and one day apart, in id order: each after the first is a repeat after 1 day.
- `interval_spread`: the tied incidents are distinct and evenly spread across the verified interval they share (about interval length / n days apart, the first at the interval's start); a tie with no shared verified interval longer than a day is placed as under zero_gap.

None of these bounds the truth: a tie that is one incident filed twice is represented only by `collapse`; two incidents really weeks apart within a verified month are represented exactly by none; and a tie whose dates are unverified has no interval to place its incidents in, so `interval_spread` places it as `zero_gap` does. Agreement across the policies shows only that a result does not hinge on these assumptions. Every policy is adjusted for organisation size like the primary model.

- Under `collapse`, the adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.176). It rests on 140 observed repeats.
- Under `zero_gap`, the adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.176). It rests on 140 observed repeats.
- Under `interval_spread`, the adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.176). It rests on 140 observed repeats.

| tie_policy      |   observed_repeats |   repeats_from_ties |   post_event_spells |   repeats_0-90 |   repeats_91-365 |   repeats_366-730 |   repeats_731-1460 |   repeats_>1460 |   adjusted_constant_hazard_p |   adjusted_0_90_91_180_banding_p |   delayed_peak_directional_p |
|:----------------|-------------------:|--------------------:|--------------------:|---------------:|-----------------:|------------------:|-------------------:|----------------:|-----------------------------:|---------------------------------:|-----------------------------:|
| collapse        |                140 |                   0 |                1008 |             17 |               47 |                23 |                 30 |              23 |                       0.1763 |                           0.2728 |                       0.3102 |
| zero_gap        |                140 |                   0 |                1008 |             17 |               47 |                23 |                 30 |              23 |                       0.1763 |                           0.2728 |                       0.3102 |
| interval_spread |                140 |                   0 |                1008 |             17 |               47 |                23 |                 30 |              23 |                       0.1763 |                           0.2728 |                       0.3102 |


## Organisation Size Specification

The primary model enters size as a numeric score (SMALL=1 to HUGE=4, UNKNOWN mean-imputed, plus an UNKNOWN indicator), which assumes equal log-rate steps between bands. It is compared here with size as a categorical factor (reference: the band with the most repeats; bands with fewer than 3 repeats pooled) and, where every delayed-peak phase has at least 10 repeats at two or more known size bands, with the score's interaction with the delayed-peak phases. The score stays primary; the headline tests are re-run under categorical size. Full table: `size_specification_comparison.csv`.

- Size specification (bands 0-90, 91-365, 366-730, 731-1460, >1460): categorical size and the numeric score are not nested (size bands MEDIUM, UNKNOWN have fewer than 3 repeats each and are pooled into one level, so the score model is not nested in the categorical one), so only AIC compares them: AIC 894.1 (categorical) against 896.8 (score), lower for the categorical model.
- With bands 0-90, 91-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.176 with the size score and p = 0.172 with categorical size: the same verdict at the 5% level.
- Size specification (bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460): categorical size and the numeric score are not nested (size bands MEDIUM, UNKNOWN have fewer than 3 repeats each and are pooled into one level, so the score model is not nested in the categorical one), so only AIC compares them: AIC 951.2 (categorical) against 953.9 (score), lower for the categorical model.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.273 with the size score and p = 0.267 with categorical size: the same verdict at the 5% level.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the directional delayed-peak test gives p = 0.310 with the size score and p = 0.314 with categorical size: the same verdict at the 5% level.
- Size by elapsed-time phase (the size score's slope in 91-180 and post-180 days against 0-90): LRT p = 0.300 on 2 df; at the 5% level the data do not reject a size effect that is the same in every phase.

| banding       | size_spec     |   log_likelihood |   parameters |   aic |   delta_aic_vs_score | nested   |   likelihood_ratio |   df |   p_value |   elapsed_constant_hazard_p |   delayed_peak_directional_p |
|:--------------|:--------------|-----------------:|-------------:|------:|---------------------:|:---------|-------------------:|-----:|----------:|----------------------------:|-----------------------------:|
| run           | score         |           -422.4 |           26 | 896.8 |                0     |          |            nan     |  nan |  nan      |                      0.1763 |                     nan      |
| run           | categorical   |           -420.1 |           27 | 894.1 |               -2.684 | False    |            nan     |  nan |  nan      |                      0.1719 |                     nan      |
| 0-90 / 91-180 | score         |           -450   |           27 | 953.9 |                0     |          |            nan     |  nan |  nan      |                      0.2728 |                       0.3102 |
| 0-90 / 91-180 | categorical   |           -447.6 |           28 | 951.2 |               -2.683 | False    |            nan     |  nan |  nan      |                      0.2671 |                       0.3136 |
| 0-90 / 91-180 | score_x_phase |           -448.8 |           29 | 955.5 |                1.589 | True     |              2.411 |    2 |    0.2996 |                    nan      |                     nan      |


## Primary Piecewise Exponential Test

The adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.176). It rests on 140 observed repeats.

- unadjusted LRT p-value: 0.04345
- adjusted LRT p-value: 0.1763
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=2), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |       17 |       86703.000 |                       7.162 |              3.773 |              10.605 |
| 91-365         |       47 |      214365.000 |                       8.008 |              5.314 |              11.157 |
| 366-730        |       23 |      209352.000 |                       4.013 |              2.042 |               6.020 |
| 731-1460       |       30 |      216178.000 |                       5.069 |              3.293 |               7.041 |
| >1460          |       23 |      118803.000 |                       7.071 |              4.140 |              10.373 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                       |
|:----------------|-------------:|---------:|----------:|:-------------------------------------|
| SMALL -> MEDIUM |        2.752 |    1.981 |     3.823 | about 175% higher repeat-event rate  |
| SMALL -> LARGE  |        7.575 |    3.926 |    14.616 | about 657% higher repeat-event rate  |
| SMALL -> HUGE   |       20.848 |    7.779 |    55.878 | about 1985% higher repeat-event rate |
| MEDIUM -> HUGE  |        7.575 |    3.926 |    14.616 | about 657% higher repeat-event rate  |
| LARGE -> HUGE   |        2.752 |    1.981 |     3.823 | about 175% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |                                                                                                                        ci_high | plain_language                         |
|:--------------------|-------------:|---------:|-------------------------------------------------------------------------------------------------------------------------------:|:---------------------------------------|
| education           |   160118.232 |    0.000 | 25684151697408809377623695498676171603001682878881393006275510414326673974386693792128054238102828969445611015111469694976.000 | about 16011723% higher than government |
| telecom_transport   |   132447.791 |    0.000 | 21251951947062198801677184276851283660916615662740130358738922643089888334627835857927166938759580182208730328800130236416.000 | about 13244679% higher than government |
| industrial          |    86291.445 |    0.000 | 13870264707229948099784817482568263130428334200077289954051774524435876798417278396838477208925627440718605450064764600320.000 | about 8629045% higher than government  |
| finance             |    86159.640 |    0.000 | 13824191856420708395777276585829336880519457662347396950037349554037040886725901340487790033187304023678019091098572423168.000 | about 8615864% higher than government  |
| technology          |    68166.784 |    0.000 | 10936113227690361097276451147441212879261734076643820896070710575508017190531756573086759087098383944502381189799595737088.000 | about 6816578% higher than government  |
| health              |    48404.785 |    0.000 |  7773060424478380768032596914036025224505986817115225891180935760223715157539829584178184368612358742295213380576932265984.000 | about 4840378% higher than government  |
| other               |    22673.869 |    0.000 |  3638457544957281771662431066770917050058390361701529670382869265955601662085303678913392590029305241961580346492766388224.000 | about 2267287% higher than government  |
| sparse_or_no_repeat |    14166.413 |    0.000 |  2287929474499081921958668965914082697118650085489576546307144463717431573189666999299501587264697264766542011636551516160.000 | about 1416541% higher than government  |

Full adjusted covariate table:

| effect                           |   rate_ratio |   ci_low |                                                                                                                        ci_high |
|:---------------------------------|-------------:|---------:|-------------------------------------------------------------------------------------------------------------------------------:|
| Entity kind: government_body     |    87698.889 |    0.000 | 14065858612987092014359187934333756903916745347391417524784943455830076222965794824770543313891079133880250791547948236800.000 |
| Entity kind: sparse_or_no_repeat |        1.791 |    0.238 |                                                                                                                         13.482 |
| Organisation size                |        2.752 |    1.981 |                                                                                                                          3.823 |
| Prior records: 100k+             |        1.441 |    0.908 |                                                                                                                          2.289 |
| Prior records: 10k-100k          |        1.533 |    0.940 |                                                                                                                          2.498 |
| Prior records: 1k-10k            |        1.253 |    0.704 |                                                                                                                          2.233 |
| Prior records: <1k               |        1.007 |    0.490 |                                                                                                                          2.071 |
| Sector: education                |   160118.232 |    0.000 | 25684151697408809377623695498676171603001682878881393006275510414326673974386693792128054238102828969445611015111469694976.000 |
| Sector: finance                  |    86159.640 |    0.000 | 13824191856420708395777276585829336880519457662347396950037349554037040886725901340487790033187304023678019091098572423168.000 |
| Sector: health                   |    48404.785 |    0.000 |  7773060424478380768032596914036025224505986817115225891180935760223715157539829584178184368612358742295213380576932265984.000 |
| Sector: industrial               |    86291.445 |    0.000 | 13870264707229948099784817482568263130428334200077289954051774524435876798417278396838477208925627440718605450064764600320.000 |
| Sector: other                    |    22673.869 |    0.000 |  3638457544957281771662431066770917050058390361701529670382869265955601662085303678913392590029305241961580346492766388224.000 |
| Sector: sparse_or_no_repeat      |    14166.413 |    0.000 |  2287929474499081921958668965914082697118650085489576546307144463717431573189666999299501587264697264766542011636551516160.000 |
| Sector: technology               |    68166.784 |    0.000 | 10936113227690361097276451147441212879261734076643820896070710575508017190531756573086759087098383944502381189799595737088.000 |
| Sector: telecom_transport        |   132447.791 |    0.000 | 21251951947062198801677184276851283660916615662740130358738922643089888334627835857927166938759580182208730328800130236416.000 |
| Unknown size                     |        0.000 |    0.000 |                                                                                                                        inf     |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 91-365, 366-730 days vs 0-90 days (unadjusted): rate ratio 0.84 (95% CI 0.50-1.43), no direction can be read from it; two-sided p = 0.526. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.50 to 1.43, which includes 1. Repeat events: 70 in 91-365, 366-730 days and 17 in 0-90 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.705. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates do not have the hypothesised shape: long-term vs response goes the other way.

- response vs immediate: rate ratio 0.92 (95% CI 0.54-1.56), no direction can be read from it; one-sided p = 0.375 (alternative: lower). Not statistically significant at the 5% level: the data do not reject the null of no decrease (rate ratio at least 1); the 95% CI runs from 0.54 to 1.56, which includes 1. Repeat events: 69 in the response period and 17 in the immediate period.
- long-term vs response: rate ratio 0.90 (95% CI 0.62-1.31), no direction can be read from it; one-sided p = 0.705 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.62 to 1.31, which includes 1. Repeat events: 52 in the long-term period and 69 in the response period.
- long-term vs immediate: rate ratio 0.83 (95% CI 0.47-1.46), no direction can be read from it; two-sided p = 0.513. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.47 to 1.46, which includes 1. Repeat events: 52 in the long-term period and 17 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.773
- directional U-shape p-value: 0.7052

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       17 |           86703 |                       7.162 |
| Response period  |       69 |          423717 |                       5.948 |
| Long-term period |       52 |          334981 |                       5.670 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        0.917 |    0.538 |     1.564 | about 8% lower   |         0.751 | less                      | 0.375           |
| long-term vs response  |        0.902 |    0.621 |     1.311 | about 10% lower  |         0.590 | greater                   | 0.705           |
| long-term vs immediate |        0.828 |    0.470 |     1.458 | about 17% lower  |         0.513 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        0.917 |    0.538 |     1.564 |      1.027 | about 8% lower   |           0.375 |
| All covariates                | long-term vs response |        0.902 |    0.621 |     1.311 |      0.689 | about 10% lower  |           0.705 |
| Drop organisation size        | response vs immediate |        0.943 |    0.553 |     1.607 |      1.054 | about 6% lower   |           0.414 |
| Drop organisation size        | long-term vs response |        1.007 |    0.695 |     1.459 |      0.764 | about 1% higher  |           0.485 |
| Drop industry/sector          | response vs immediate |        0.867 |    0.509 |     1.477 |      0.967 | about 13% lower  |           0.300 |
| Drop industry/sector          | long-term vs response |        0.877 |    0.606 |     1.270 |      0.664 | about 12% lower  |           0.757 |
| Drop size and industry/sector | response vs immediate |        0.899 |    0.528 |     1.531 |      1.002 | about 10% lower  |           0.348 |
| Drop size and industry/sector | long-term vs response |        1.008 |    0.698 |     1.456 |      0.758 | about 1% higher  |           0.483 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.7730 |                         0.7052 |
| Drop organisation size        |    0.9767 |                         0.4848 |
| Drop industry/sector          |    0.6019 |                         0.7567 |
| Drop size and industry/sector |    0.9260 |                         0.4831 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.54-1.56). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.62-1.31).

## Delayed 91-180 Day Peak Sensitivity

This sensitivity reruns the elapsed-time model with bands `0-90`, `91-180`, `181-365`, `366-730`, `731-1460`, and `>1460` to test whether the apparent early elevation is concentrated in days 91-180.
A formal delayed peak requires the 91-180 day period to be higher than both 0-90 days and the pooled post-180 period after adjustment.

- adjusted elapsed-time LRT p-value for the 90/180 split: 0.2728
- phase-model LRT p-value versus constant elapsed-time risk: 0.3891
- directional delayed-peak p-value: 0.3102

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |       17 |           86703 |                       7.162 |
| Delayed peak 91-180 days |       18 |           77656 |                       8.466 |
| Post-180 days            |      103 |          681042 |                       5.524 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        1.182 |    0.609 |     2.295 | about 18% higher |         0.620 | greater                   | 0.310           |
| 91-180 vs post-180 |        1.417 |    0.852 |     2.357 | about 42% higher |         0.180 | greater                   | 0.090           |
| post-180 vs 0-90   |        0.835 |    0.495 |     1.408 | about 17% lower  |         0.498 |                           |                 |

Interpretation:

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.310. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates have the hypothesised shape. The weakest part is 91-180 vs 0-90 (p = 0.310).

- 91-180 vs 0-90: rate ratio 1.18 (95% CI 0.61-2.30), no direction can be read from it; one-sided p = 0.310 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.61 to 2.30, which includes 1. Repeat events: 18 in 91-180 days and 17 in 0-90 days.
- 91-180 vs post-180: rate ratio 1.42 (95% CI 0.85-2.36), no direction can be read from it; one-sided p = 0.090 (alternative: higher). Not statistically significant at the 5% level (borderline): the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.85 to 2.36, which includes 1. Repeat events: 18 in 91-180 days and 103 in post-180 days.
- post-180 vs 0-90: rate ratio 0.83 (95% CI 0.49-1.41), no direction can be read from it; two-sided p = 0.498. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.49 to 1.41, which includes 1. Repeat events: 103 in post-180 days and 17 in 0-90 days.

## Unadjusted Parametric Survival Model Comparison (Descriptive)

Best AIC model: exponential. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| exponential       |        -1358.829 |            1 | 2719.659 | 2724.574 | True        |
| weibull           |        -1358.133 |            2 | 2720.267 | 2730.098 | True        |
| generalized_gamma |        -1358.138 |            3 | 2722.276 | 2737.023 | True        |
| loglogistic       |        -1359.642 |            2 | 2723.283 | 2733.115 | True        |
| lognormal         |        -1365.271 |            2 | 2734.542 | 2744.374 | True        |

Exponential vs Weibull LRT p-value: 0.2381

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
