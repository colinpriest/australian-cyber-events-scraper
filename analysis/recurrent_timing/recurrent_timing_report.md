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
- elapsed-time bands: 0-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2003-07-04
- maximum event date: 2026-10-26
- first-of-month event dates: 212
- verified incident-date precision: {'unverified': 561, 'day': 264, 'month': 183}
- tie policy (several incidents of one organisation on one date): collapse
- same-date ties: {'ties (organisation-dates with several incidents)': 0, 'incidents in ties': 0, 'organisations with a tie': 0, 'repeats a policy that separates tied incidents adds': 0, 'ties with a shared verified interval longer than a day (spread by interval_spread)': 0, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 0, 'ties: other_day': 0, 'repeats between tied incidents in this run': 0}
- incidents not ordered by their dates (different stored dates, overlapping verified intervals): {'consecutive pairs with overlapping verified intervals': 1, '... of which stored dates differ by under 90 days': 1}
- covariate coverage: {'size_estimate levels': {'HUGE': 267, 'LARGE': 296, 'MEDIUM': 288, 'SMALL': 112, 'UNKNOWN': 45}, 'size_confidence populated': 1008, 'employee_count populated': 931, 'turnover populated': 0, 'industry populated': 883, 'entity_kind populated': 1008, 'sector_proxy levels': {'education': 87, 'finance': 63, 'government': 176, 'health': 90, 'industrial': 28, 'media': 7, 'other': 348, 'retail': 45, 'technology': 116, 'telecom_transport': 48}, 'prior records populated': 436}
- organisation size across name variants: {'organisations stored under several name variants': 6, 'organisations whose size changed against the representative-only rule': 1, 'organisations whose variants give conflicting known size bands': 1, 'deciding rule': {'higher size_confidence': 3, 'known band over UNKNOWN': 3}}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 40, 'events with an unresolved attribution': 38, 'organisations with an unresolved attribution': 40, 'victim-event attributions analysed': 1008, 'repeats': {'included': {'observed repeats': 140, 'entities with a repeat': 76, 'repeats by elapsed band': {'0-180': 35, '181-365': 29, '366-730': 23, '731-1460': 30, '>1460': 23}}, 'excluded': {'observed repeats': 126, 'entities with a repeat': 72, 'repeats by elapsed band': {'0-180': 31, '181-365': 25, '366-730': 17, '731-1460': 30, '>1460': 23}}, 'repeats involving an unresolved attribution': 18}}

## Same-Date Ties And Unknown Ordering

A same-date tie is far more likely one incident that deduplication has not joined than two incidents on one day. Every such pair, and every pair whose possible periods overlap, is an exception listed with the reason it is still two events in `dashboard/close_pairs.html`; the merging steps are fixed when one is a missed duplicate. Until a tie is resolved, each tie policy makes one stated assumption about it (every tie, with its incident ids and the length of any verified interval its incidents share, is in `entity_date_ties.csv`):

- `collapse` (primary): the tied incidents are one occurrence: one time point, never a repeat between them (their ids and count are kept).
- `zero_gap`: the tied incidents are distinct and one day apart, in id order: each after the first is a repeat after 1 day.
- `interval_spread`: the tied incidents are distinct and evenly spread across the verified interval they share (about interval length / n days apart, the first at the interval's start); a tie with no shared verified interval longer than a day is placed as under zero_gap.

None of these bounds the truth: a tie that is one incident filed twice is represented only by `collapse`; two incidents really weeks apart within a verified month are represented exactly by none; and a tie whose dates are unverified has no interval to place its incidents in, so `interval_spread` places it as `zero_gap` does. Agreement across the policies shows only that a result does not hinge on these assumptions. Every policy is adjusted for organisation size like the primary model.

- Under `collapse`, the adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.191). It rests on 140 observed repeats.
- Under `zero_gap`, the adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.191). It rests on 140 observed repeats.
- Under `interval_spread`, the adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.191). It rests on 140 observed repeats.

| tie_policy      |   observed_repeats |   repeats_from_ties |   post_event_spells |   repeats_0-180 |   repeats_181-365 |   repeats_366-730 |   repeats_731-1460 |   repeats_>1460 |   adjusted_constant_hazard_p |   adjusted_0_90_91_180_banding_p |   delayed_peak_directional_p |
|:----------------|-------------------:|--------------------:|--------------------:|----------------:|------------------:|------------------:|-------------------:|----------------:|-----------------------------:|---------------------------------:|-----------------------------:|
| collapse        |                140 |                   0 |                1008 |              35 |                29 |                23 |                 30 |              23 |                       0.1909 |                           0.2728 |                       0.3102 |
| zero_gap        |                140 |                   0 |                1008 |              35 |                29 |                23 |                 30 |              23 |                       0.1909 |                           0.2728 |                       0.3102 |
| interval_spread |                140 |                   0 |                1008 |              35 |                29 |                23 |                 30 |              23 |                       0.1909 |                           0.2728 |                       0.3102 |


## Organisation Size Specification

The primary model enters size as a numeric score (SMALL=1 to HUGE=4, UNKNOWN mean-imputed, plus an UNKNOWN indicator), which assumes equal log-rate steps between bands. It is compared here with size as a categorical factor (reference: the band with the most repeats; bands with fewer than 3 repeats pooled) and, where every delayed-peak phase has at least 10 repeats at two or more known size bands, with the score's interaction with the delayed-peak phases. The score stays primary; the headline tests are re-run under categorical size. Full table: `size_specification_comparison.csv`.

- Size specification (bands 0-180, 181-365, 366-730, 731-1460, >1460): categorical size and the numeric score are not nested (size bands MEDIUM, UNKNOWN have fewer than 3 repeats each and are pooled into one level, so the score model is not nested in the categorical one), so only AIC compares them: AIC 903.5 (categorical) against 906.2 (score), lower for the categorical model.
- With bands 0-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.191 with the size score and p = 0.186 with categorical size: the same verdict at the 5% level.
- Size specification (bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460): categorical size and the numeric score are not nested (size bands MEDIUM, UNKNOWN have fewer than 3 repeats each and are pooled into one level, so the score model is not nested in the categorical one), so only AIC compares them: AIC 951.2 (categorical) against 953.9 (score), lower for the categorical model.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the adjusted constant-hazard test gives p = 0.273 with the size score and p = 0.267 with categorical size: the same verdict at the 5% level.
- With bands 0-90, 91-180, 181-365, 366-730, 731-1460, >1460, the directional delayed-peak test gives p = 0.310 with the size score and p = 0.314 with categorical size: the same verdict at the 5% level.
- Size by elapsed-time phase (the size score's slope in 91-180 and post-180 days against 0-90): LRT p = 0.300 on 2 df; at the 5% level the data do not reject a size effect that is the same in every phase.

| banding       | size_spec     |   log_likelihood |   parameters |   aic |   delta_aic_vs_score | nested   |   likelihood_ratio |   df |   p_value |   elapsed_constant_hazard_p |   delayed_peak_directional_p |
|:--------------|:--------------|-----------------:|-------------:|------:|---------------------:|:---------|-------------------:|-----:|----------:|----------------------------:|-----------------------------:|
| run           | score         |           -427.1 |           26 | 906.2 |                0     |          |            nan     |  nan |  nan      |                      0.1909 |                     nan      |
| run           | categorical   |           -424.7 |           27 | 903.5 |               -2.693 | False    |            nan     |  nan |  nan      |                      0.1856 |                     nan      |
| 0-90 / 91-180 | score         |           -450   |           27 | 953.9 |                0     |          |            nan     |  nan |  nan      |                      0.2728 |                       0.3102 |
| 0-90 / 91-180 | categorical   |           -447.6 |           28 | 951.2 |               -2.683 | False    |            nan     |  nan |  nan      |                      0.2671 |                       0.3136 |
| 0-90 / 91-180 | score_x_phase |           -448.8 |           29 | 955.5 |                1.589 | True     |              2.411 |    2 |    0.2996 |                    nan      |                     nan      |


## Primary Piecewise Exponential Test

The adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.191). It rests on 140 observed repeats.

- unadjusted LRT p-value: 0.0464
- adjusted LRT p-value: 0.1909
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=2), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-180          |       35 |      164359.000 |                       7.778 |              4.480 |              11.214 |
| 181-365        |       29 |      136709.000 |                       7.748 |              4.601 |              11.889 |
| 366-730        |       23 |      209352.000 |                       4.013 |              2.042 |               6.020 |
| 731-1460       |       30 |      216178.000 |                       5.069 |              3.293 |               7.041 |
| >1460          |       23 |      118803.000 |                       7.071 |              4.140 |              10.373 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                       |
|:----------------|-------------:|---------:|----------:|:-------------------------------------|
| SMALL -> MEDIUM |        2.754 |    1.983 |     3.826 | about 175% higher repeat-event rate  |
| SMALL -> LARGE  |        7.585 |    3.931 |    14.636 | about 659% higher repeat-event rate  |
| SMALL -> HUGE   |       20.890 |    7.794 |    55.991 | about 1989% higher repeat-event rate |
| MEDIUM -> HUGE  |        7.585 |    3.931 |    14.636 | about 659% higher repeat-event rate  |
| LARGE -> HUGE   |        2.754 |    1.983 |     3.826 | about 175% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |                                                                                                                                             ci_high | plain_language                         |
|:--------------------|-------------:|---------:|----------------------------------------------------------------------------------------------------------------------------------------------------:|:---------------------------------------|
| education           |   223716.490 |    0.000 | 82640903269373724072929406966523160938512736006448380655721232054929530140096502916605637696833514459636777862383475462090550811041460729675776.000 | about 22371549% higher than government |
| telecom_transport   |   185910.416 |    0.000 | 68692585333807437521372019499834417665115158368641873164744953502071966166104195145135008987002286166228048278161899471245190334329632476626944.000 | about 18590942% higher than government |
| finance             |   120894.404 |    0.000 | 44668046932983292023163559157749801892507075395517671923533586546291483691479668757918341566414707592553350277196410026353223820761723178057728.000 | about 12089340% higher than government |
| industrial          |   120629.229 |    0.000 | 44637821052432533411551112974377529483889535354797486073224227521587002654397553357345200682952062976287865528621254172129592970155545562447872.000 | about 12062823% higher than government |
| technology          |    95453.692 |    0.000 | 35265097074373025315187733195075420851189540397914320966943393934944304626182851242021190088154738433988462666518818874282399103410444674531328.000 | about 9545269% higher than government  |
| health              |    67811.302 |    0.000 | 25072845631083986519126591166253603607229613908055163874956841772926550069966476155384863336495595630174245610224996216696766986836877653311488.000 | about 6781030% higher than government  |
| other               |    31749.870 |    0.000 | 11732215168122884623251932644585125094596396099660019071628541267238583199328990052330090437216956453050442814570505830218459274260871095779328.000 | about 3174887% higher than government  |
| sparse_or_no_repeat |    19847.403 |    0.000 |  7373925703905864220608448710434812838556560121133704774600940978915212457194081219790562522352506856127057955676105826024157243203871062360064.000 | about 1984640% higher than government  |

Full adjusted covariate table:

| effect                           |   rate_ratio |   ci_low |                                                                                                                                             ci_high |
|:---------------------------------|-------------:|---------:|----------------------------------------------------------------------------------------------------------------------------------------------------:|
| Entity kind: government_body     |   122785.418 |    0.000 | 45352311021478526888948031551272161217867682271115147274003648140065480639463556823078406515658578455468413876441769150680167731649624338857984.000 |
| Entity kind: sparse_or_no_repeat |        1.790 |    0.237 |                                                                                                                                              13.504 |
| Organisation size                |        2.754 |    1.983 |                                                                                                                                               3.826 |
| Prior records: 100k+             |        1.442 |    0.908 |                                                                                                                                               2.290 |
| Prior records: 10k-100k          |        1.534 |    0.941 |                                                                                                                                               2.501 |
| Prior records: 1k-10k            |        1.254 |    0.704 |                                                                                                                                               2.234 |
| Prior records: <1k               |        1.008 |    0.490 |                                                                                                                                               2.074 |
| Sector: education                |   223716.490 |    0.000 | 82640903269373724072929406966523160938512736006448380655721232054929530140096502916605637696833514459636777862383475462090550811041460729675776.000 |
| Sector: finance                  |   120894.404 |    0.000 | 44668046932983292023163559157749801892507075395517671923533586546291483691479668757918341566414707592553350277196410026353223820761723178057728.000 |
| Sector: health                   |    67811.302 |    0.000 | 25072845631083986519126591166253603607229613908055163874956841772926550069966476155384863336495595630174245610224996216696766986836877653311488.000 |
| Sector: industrial               |   120629.229 |    0.000 | 44637821052432533411551112974377529483889535354797486073224227521587002654397553357345200682952062976287865528621254172129592970155545562447872.000 |
| Sector: other                    |    31749.870 |    0.000 | 11732215168122884623251932644585125094596396099660019071628541267238583199328990052330090437216956453050442814570505830218459274260871095779328.000 |
| Sector: sparse_or_no_repeat      |    19847.403 |    0.000 |  7373925703905864220608448710434812838556560121133704774600940978915212457194081219790562522352506856127057955676105826024157243203871062360064.000 |
| Sector: technology               |    95453.692 |    0.000 | 35265097074373025315187733195075420851189540397914320966943393934944304626182851242021190088154738433988462666518818874282399103410444674531328.000 |
| Sector: telecom_transport        |   185910.416 |    0.000 | 68692585333807437521372019499834417665115158368641873164744953502071966166104195145135008987002286166228048278161899471245190334329632476626944.000 |
| Unknown size                     |        0.000 |    0.000 |                                                                                                                                             inf     |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 181-365, 366-730 days vs 0-180 days (unadjusted): rate ratio 0.71 (95% CI 0.46-1.08), no direction can be read from it; two-sided p = 0.111. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.46 to 1.08, which includes 1. Repeat events: 52 in 181-365, 366-730 days and 35 in 0-180 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.559. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates do not have the hypothesised shape: long-term vs response goes the other way.

- response vs immediate: rate ratio 0.78 (95% CI 0.50-1.20), no direction can be read from it; one-sided p = 0.129 (alternative: lower). Not statistically significant at the 5% level: the data do not reject the null of no decrease (rate ratio at least 1); the 95% CI runs from 0.50 to 1.20, which includes 1. Repeat events: 51 in the response period and 35 in the immediate period.
- long-term vs response: rate ratio 0.97 (95% CI 0.65-1.44), no direction can be read from it; one-sided p = 0.559 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.65 to 1.44, which includes 1. Repeat events: 52 in the long-term period and 51 in the response period.
- long-term vs immediate: rate ratio 0.76 (95% CI 0.48-1.19), no direction can be read from it; two-sided p = 0.223. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.48 to 1.19, which includes 1. Repeat events: 52 in the long-term period and 35 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.4352
- directional U-shape p-value: 0.5585

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       35 |          164359 |                       7.778 |
| Response period  |       51 |          346061 |                       5.383 |
| Long-term period |       52 |          334981 |                       5.670 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        0.778 |    0.504 |     1.203 | about 22% lower  |         0.259 | less                      | 0.129           |
| long-term vs response  |        0.971 |    0.653 |     1.443 | about 3% lower   |         0.883 | greater                   | 0.559           |
| long-term vs immediate |        0.756 |    0.482 |     1.185 | about 24% lower  |         0.223 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        0.778 |    0.504 |     1.203 |      0.699 | about 22% lower  |           0.129 |
| All covariates                | long-term vs response |        0.971 |    0.653 |     1.443 |      0.791 | about 3% lower   |           0.559 |
| Drop organisation size        | response vs immediate |        0.794 |    0.514 |     1.225 |      0.711 | about 21% lower  |           0.148 |
| Drop organisation size        | long-term vs response |        1.083 |    0.729 |     1.607 |      0.877 | about 8% higher  |           0.347 |
| Drop industry/sector          | response vs immediate |        0.728 |    0.472 |     1.122 |      0.649 | about 27% lower  |           0.075 |
| Drop industry/sector          | long-term vs response |        0.957 |    0.645 |     1.419 |      0.775 | about 4% lower   |           0.587 |
| Drop size and industry/sector | response vs immediate |        0.750 |    0.487 |     1.156 |      0.668 | about 25% lower  |           0.096 |
| Drop size and industry/sector | long-term vs response |        1.097 |    0.741 |     1.624 |      0.883 | about 10% higher |           0.323 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.4352 |                         0.5585 |
| Drop organisation size        |    0.5853 |                         0.3469 |
| Drop industry/sector          |    0.2497 |                         0.5872 |
| Drop size and industry/sector |    0.4344 |                         0.3226 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.50-1.20). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.65-1.44).

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
