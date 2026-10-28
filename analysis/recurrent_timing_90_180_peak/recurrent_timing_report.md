# Repeat Cyber-Event Timing Analysis

## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.
The primary model includes recorded organisation size. Size is a proxy for differences in visibility, attack exposure and target value, not a direct measure of those factors; residual confounding remains possible. The ordinal size term assumes equal log-rate steps between categories, UNKNOWN has a separate indicator, and current recorded size may differ from size at an incident's date. Unadjusted rates, plots, parametric models and size-dropped variants below are descriptive diagnostics and do not control for this confounding.

## Data Quality Summary

- victim event attributions: 1016
- unique deduplicated events: 895
- victim entities: 893
- entities with more than one event: 69
- post-event spells: 1016
- observed repeat spells: 123
- right-censored spells: 893
- censor date: 2026-10-29
- elapsed-time bands: 0-90, 91-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2003-07-04
- maximum event date: 2026-10-23
- first-of-month event dates: 253
- verified incident-date precision: {'unverified': 591, 'month': 259, 'day': 166}
- same-day entity-event groups collapsed: 0
- covariate coverage: {'size_estimate levels': {'HUGE': 262, 'LARGE': 294, 'MEDIUM': 280, 'SMALL': 100, 'UNKNOWN': 80}, 'size_confidence populated': 1013, 'employee_count populated': 902, 'turnover populated': 0, 'industry populated': 922, 'entity_kind populated': 1016, 'sector_proxy levels': {'education': 67, 'finance': 66, 'government': 157, 'health': 98, 'industrial': 25, 'media': 8, 'other': 365, 'retail': 49, 'technology': 143, 'telecom_transport': 38}, 'prior records populated': 429}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 43, 'events with an unresolved attribution': 43, 'organisations with an unresolved attribution': 42, 'victim-event attributions analysed': 1016, 'repeats': {'included': {'observed repeats': 123, 'entities with a repeat': 69, 'repeats by elapsed band': {'0-90': 13, '91-180': 13, '181-365': 24, '366-730': 24, '731-1460': 29, '>1460': 20}}, 'excluded': {'observed repeats': 106, 'entities with a repeat': 61, 'repeats by elapsed band': {'0-90': 11, '91-180': 11, '181-365': 18, '366-730': 21, '731-1460': 27, '>1460': 18}}, 'repeats involving an unresolved attribution': 22}}

## Primary Piecewise Exponential Test

The adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.718). It rests on 123 observed repeats.

- unadjusted LRT p-value: 0.6441
- adjusted LRT p-value: 0.7178
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=2), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |       13 |       87567.000 |                       5.422 |              2.135 |               8.684 |
| 91-180         |       13 |       79273.000 |                       5.990 |              2.775 |               9.303 |
| 181-365        |       24 |      140133.000 |                       6.255 |              3.159 |               9.697 |
| 366-730        |       24 |      216749.000 |                       4.044 |              2.417 |               5.757 |
| 731-1460       |       29 |      221861.000 |                       4.774 |              3.001 |               7.067 |
| >1460          |       20 |      120382.000 |                       6.068 |              3.248 |               8.603 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        1.578 |    1.188 |     2.095 | about 58% higher repeat-event rate  |
| SMALL -> LARGE  |        2.489 |    1.412 |     4.389 | about 149% higher repeat-event rate |
| SMALL -> HUGE   |        3.928 |    1.677 |     9.196 | about 293% higher repeat-event rate |
| MEDIUM -> HUGE  |        2.489 |    1.412 |     4.389 | about 149% higher repeat-event rate |
| LARGE -> HUGE   |        1.578 |    1.188 |     2.095 | about 58% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |                                                                                                                                                                                              ci_high | plain_language                    |
|:--------------------|-------------:|---------:|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------:|:----------------------------------|
| telecom_transport   |        4.695 |    1.346 |                                                                                                                                                                                               16.382 | about 369% higher than government |
| education           |        3.547 |    1.136 |                                                                                                                                                                                               11.072 | about 255% higher than government |
| technology          |        3.215 |    1.020 |                                                                                                                                                                                               10.135 | about 222% higher than government |
| finance             |        3.206 |    0.972 |                                                                                                                                                                                               10.580 | about 221% higher than government |
| health              |        2.938 |    0.814 |                                                                                                                                                                                               10.596 | about 194% higher than government |
| industrial          |        2.841 |    0.596 |                                                                                                                                                                                               13.539 | about 184% higher than government |
| other               |        0.742 |    0.205 |                                                                                                                                                                                                2.683 | about 26% lower than government   |
| sparse_or_no_repeat |        0.000 |    0.000 | 641210642840194584861137032905646445755444840515381609334509360553373255447654545599067653598747988740980344950422356709687940879105821042829729422755186036844259166263903938431977171162497024.000 | about 100% lower than government  |

Full adjusted covariate table:

| effect                           |   rate_ratio |   ci_low |                                                                                                                                                                                              ci_high |
|:---------------------------------|-------------:|---------:|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------:|
| Entity kind: government_body     |        4.383 |    1.462 |                                                                                                                                                                                               13.136 |
| Entity kind: sparse_or_no_repeat |        0.000 |    0.000 |                                                                                                                                                                                              inf     |
| Organisation size                |        1.578 |    1.188 |                                                                                                                                                                                                2.095 |
| Prior records: 100k+             |        0.950 |    0.557 |                                                                                                                                                                                                1.620 |
| Prior records: 10k-100k          |        1.451 |    0.882 |                                                                                                                                                                                                2.388 |
| Prior records: 1k-10k            |        1.190 |    0.630 |                                                                                                                                                                                                2.247 |
| Prior records: <1k               |        0.727 |    0.326 |                                                                                                                                                                                                1.620 |
| Sector: education                |        3.547 |    1.136 |                                                                                                                                                                                               11.072 |
| Sector: finance                  |        3.206 |    0.972 |                                                                                                                                                                                               10.580 |
| Sector: health                   |        2.938 |    0.814 |                                                                                                                                                                                               10.596 |
| Sector: industrial               |        2.841 |    0.596 |                                                                                                                                                                                               13.539 |
| Sector: other                    |        0.742 |    0.205 |                                                                                                                                                                                                2.683 |
| Sector: sparse_or_no_repeat      |        0.000 |    0.000 | 641210642840194584861137032905646445755444840515381609334509360553373255447654545599067653598747988740980344950422356709687940879105821042829729422755186036844259166263903938431977171162497024.000 |
| Sector: technology               |        3.215 |    1.020 |                                                                                                                                                                                               10.135 |
| Sector: telecom_transport        |        4.695 |    1.346 |                                                                                                                                                                                               16.382 |
| Unknown size                     |        0.942 |    0.389 |                                                                                                                                                                                                2.283 |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 91-180, 181-365 days vs 0-90 days (unadjusted): rate ratio 1.14 (95% CI 0.60-2.14), about 14% higher; two-sided p = 0.693. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.60 to 2.14, which includes 1. Repeat events: 37 in 91-180, 181-365 days and 13 in 0-90 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.676. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates do not have the hypothesised shape: response vs immediate, long-term vs response go the other way.

- response vs immediate: rate ratio 1.16 (95% CI 0.61-2.19), about 16% higher; one-sided p = 0.676 (alternative: lower). Not statistically significant at the 5% level: the data do not reject the null of no decrease (rate ratio at least 1); the 95% CI runs from 0.61 to 2.19, which includes 1. The estimate is in the opposite direction to the hypothesis (higher, not lower). Repeat events: 36 in the response period and 13 in the immediate period.
- long-term vs response: rate ratio 0.93 (95% CI 0.62-1.41), about 7% lower; one-sided p = 0.633 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.62 to 1.41, which includes 1. The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 72 in the long-term period and 36 in the response period.
- long-term vs immediate: rate ratio 1.08 (95% CI 0.59-1.97), about 8% higher; two-sided p = 0.805. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.59 to 1.97, which includes 1. Repeat events: 72 in the long-term period and 13 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.888
- directional U-shape p-value: 0.6756

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       13 |           87567 |                       5.422 |
| Response period  |       36 |          219406 |                       5.993 |
| Long-term period |       72 |          558992 |                       4.705 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        1.159 |    0.614 |     2.187 | about 16% higher |         0.649 | less                      | 0.676           |
| long-term vs response  |        0.931 |    0.616 |     1.407 | about 7% lower   |         0.734 | greater                   | 0.633           |
| long-term vs immediate |        1.079 |    0.591 |     1.970 | about 8% higher  |         0.805 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        1.159 |    0.614 |     2.187 |      1.572 | about 16% higher |           0.676 |
| All covariates                | long-term vs response |        0.931 |    0.616 |     1.407 |      0.791 | about 7% lower   |           0.633 |
| Drop organisation size        | response vs immediate |        1.166 |    0.618 |     2.199 |      1.581 | about 17% higher |           0.682 |
| Drop organisation size        | long-term vs response |        0.964 |    0.638 |     1.455 |      0.817 | about 4% lower   |           0.570 |
| Drop industry/sector          | response vs immediate |        1.157 |    0.613 |     2.182 |      1.569 | about 16% higher |           0.673 |
| Drop industry/sector          | long-term vs response |        0.915 |    0.607 |     1.379 |      0.772 | about 8% lower   |           0.664 |
| Drop size and industry/sector | response vs immediate |        1.164 |    0.617 |     2.196 |      1.579 | about 16% higher |           0.681 |
| Drop size and industry/sector | long-term vs response |        0.971 |    0.646 |     1.460 |      0.814 | about 3% lower   |           0.556 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.8880 |                         0.6756 |
| Drop organisation size        |    0.8910 |                         0.6822 |
| Drop industry/sector          |    0.8735 |                         0.6734 |
| Drop size and industry/sector |    0.8908 |                         0.6810 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.61-2.19). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.62-1.41).

## Delayed 91-180 Day Peak Test

Definition used here: the hypothesised peak is 91-180 days after the prior event. A formal peak requires 91-180 days to be higher than both 0-90 days and the pooled post-180 period.
The adjusted peak model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main model.

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.486. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates have the hypothesised shape. The weakest part is 91-180 vs post-180 (p = 0.486).

- 91-180 vs 0-90: rate ratio 1.12 (95% CI 0.52-2.41), about 12% higher; one-sided p = 0.388 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.52 to 2.41, which includes 1. Repeat events: 13 in 91-180 days and 13 in 0-90 days.
- 91-180 vs post-180: rate ratio 1.01 (95% CI 0.56-1.82), about 1% higher; one-sided p = 0.486 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.56 to 1.82, which includes 1. Repeat events: 13 in 91-180 days and 95 in post-180 days.
- post-180 vs 0-90: rate ratio 1.11 (95% CI 0.62-1.99), about 11% higher; two-sided p = 0.735. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.62 to 1.99, which includes 1. Repeat events: 95 in post-180 days and 13 in 0-90 days.

- phase-model LRT p-value versus constant elapsed-time risk: 0.9397
- directional delayed-peak p-value: 0.4864

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |       13 |           87567 |                       5.422 |
| Delayed peak 91-180 days |       13 |           79273 |                       5.990 |
| Post-180 days            |       95 |          699125 |                       4.963 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        1.118 |    0.518 |     2.412 | about 12% higher |         0.776 | greater                   | 0.388           |
| 91-180 vs post-180 |        1.010 |    0.562 |     1.816 | about 1% higher  |         0.973 | greater                   | 0.486           |
| post-180 vs 0-90   |        1.107 |    0.615 |     1.991 | about 11% higher |         0.735 |                           |                 |

## Unadjusted Parametric Survival Model Comparison (Descriptive)

Best AIC model: exponential. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| exponential       |        -1212.708 |            1 | 2427.416 | 2432.340 | True        |
| weibull           |        -1212.661 |            2 | 2429.322 | 2439.169 | True        |
| loglogistic       |        -1213.698 |            2 | 2431.397 | 2441.244 | True        |
| generalized_gamma |        -1212.707 |            3 | 2431.414 | 2446.185 | True        |
| lognormal         |        -1217.744 |            2 | 2439.488 | 2449.335 | True        |

Exponential vs Weibull LRT p-value: 0.7587

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
