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
- elapsed-time bands: 0-90, 91-365, 366-730, 731-1460, >1460
- minimum event date: 2003-07-04
- maximum event date: 2026-10-23
- first-of-month event dates: 253
- verified incident-date precision: {'unverified': 591, 'month': 259, 'day': 166}
- same-day entity-event groups collapsed: 0
- covariate coverage: {'size_estimate levels': {'HUGE': 262, 'LARGE': 294, 'MEDIUM': 280, 'SMALL': 100, 'UNKNOWN': 80}, 'size_confidence populated': 1013, 'employee_count populated': 902, 'turnover populated': 0, 'industry populated': 922, 'entity_kind populated': 1016, 'sector_proxy levels': {'education': 67, 'finance': 66, 'government': 157, 'health': 98, 'industrial': 25, 'media': 8, 'other': 365, 'retail': 49, 'technology': 143, 'telecom_transport': 38}, 'prior records populated': 429}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 43, 'events with an unresolved attribution': 43, 'organisations with an unresolved attribution': 42, 'victim-event attributions analysed': 1016, 'repeats': {'included': {'observed repeats': 123, 'entities with a repeat': 69, 'repeats by elapsed band': {'0-90': 13, '91-365': 37, '366-730': 24, '731-1460': 29, '>1460': 20}}, 'excluded': {'observed repeats': 106, 'entities with a repeat': 61, 'repeats by elapsed band': {'0-90': 11, '91-365': 29, '366-730': 21, '731-1460': 27, '>1460': 18}}, 'repeats involving an unresolved attribution': 22}}

## Primary Piecewise Exponential Test

The adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.582). It rests on 123 observed repeats.

- unadjusted LRT p-value: 0.5014
- adjusted LRT p-value: 0.5822
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=2), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |       13 |       87567.000 |                       5.422 |              2.135 |               8.684 |
| 91-365         |       37 |      219406.000 |                       6.159 |              3.819 |               8.845 |
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
| SMALL -> LARGE  |        2.490 |    1.412 |     4.391 | about 149% higher repeat-event rate |
| SMALL -> HUGE   |        3.929 |    1.678 |     9.200 | about 293% higher repeat-event rate |
| MEDIUM -> HUGE  |        2.490 |    1.412 |     4.391 | about 149% higher repeat-event rate |
| LARGE -> HUGE   |        1.578 |    1.188 |     2.095 | about 58% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| telecom_transport   |        4.699 |    1.347 |    16.394 | about 370% higher than government |
| education           |        3.546 |    1.137 |    11.065 | about 255% higher than government |
| technology          |        3.218 |    1.021 |    10.142 | about 222% higher than government |
| finance             |        3.209 |    0.973 |    10.586 | about 221% higher than government |
| health              |        2.939 |    0.815 |    10.602 | about 194% higher than government |
| industrial          |        2.844 |    0.597 |    13.554 | about 184% higher than government |
| other               |        0.743 |    0.205 |     2.686 | about 26% lower than government   |
| sparse_or_no_repeat |        0.000 |    0.000 |   inf     | about 100% lower than government  |

Full adjusted covariate table:

| effect                           |   rate_ratio |   ci_low |   ci_high |
|:---------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body     |        4.388 |    1.464 |    13.150 |
| Entity kind: sparse_or_no_repeat |        0.000 |    0.000 |   inf     |
| Organisation size                |        1.578 |    1.188 |     2.095 |
| Prior records: 100k+             |        0.949 |    0.557 |     1.616 |
| Prior records: 10k-100k          |        1.451 |    0.882 |     2.388 |
| Prior records: 1k-10k            |        1.189 |    0.630 |     2.246 |
| Prior records: <1k               |        0.727 |    0.326 |     1.620 |
| Sector: education                |        3.546 |    1.137 |    11.065 |
| Sector: finance                  |        3.209 |    0.973 |    10.586 |
| Sector: health                   |        2.939 |    0.815 |    10.602 |
| Sector: industrial               |        2.844 |    0.597 |    13.554 |
| Sector: other                    |        0.743 |    0.205 |     2.686 |
| Sector: sparse_or_no_repeat      |        0.000 |    0.000 |   inf     |
| Sector: technology               |        3.218 |    1.021 |    10.142 |
| Sector: telecom_transport        |        4.699 |    1.347 |    16.394 |
| Unknown size                     |        0.941 |    0.388 |     2.282 |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 91-365, 366-730 days vs 0-90 days (unadjusted): rate ratio 0.94 (95% CI 0.52-1.71), about 6% lower; two-sided p = 0.845. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.52 to 1.71, which includes 1. The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 61 in 91-365, 366-730 days and 13 in 0-90 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.549. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates do not have the hypothesised shape: response vs immediate goes the other way.

- response vs immediate: rate ratio 1.04 (95% CI 0.57-1.90), about 4% higher; one-sided p = 0.549 (alternative: lower). Not statistically significant at the 5% level: the data do not reject the null of no decrease (rate ratio at least 1); the 95% CI runs from 0.57 to 1.90, which includes 1. The estimate is in the opposite direction to the hypothesis (higher, not lower). Repeat events: 60 in the response period and 13 in the immediate period.
- long-term vs response: rate ratio 1.19 (95% CI 0.80-1.78), about 19% higher; one-sided p = 0.190 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.80 to 1.78, which includes 1. Repeat events: 48 in the long-term period and 60 in the response period.
- long-term vs immediate: rate ratio 1.24 (95% CI 0.66-2.33), about 24% higher; two-sided p = 0.503. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.66 to 2.33, which includes 1. Repeat events: 48 in the long-term period and 13 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.6416
- directional U-shape p-value: 0.5495

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       13 |           87567 |                       5.422 |
| Response period  |       60 |          436155 |                       5.025 |
| Long-term period |       48 |          342243 |                       5.123 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        1.039 |    0.569 |     1.896 | about 4% higher  |         0.901 | less                      | 0.549           |
| long-term vs response  |        1.194 |    0.803 |     1.776 | about 19% higher |         0.381 | greater                   | 0.190           |
| long-term vs immediate |        1.241 |    0.660 |     2.334 | about 24% higher |         0.503 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        1.039 |    0.569 |     1.896 |      1.327 | about 4% higher  |           0.549 |
| All covariates                | long-term vs response |        1.194 |    0.803 |     1.776 |      0.973 | about 19% higher |           0.190 |
| Drop organisation size        | response vs immediate |        1.046 |    0.573 |     1.907 |      1.334 | about 5% higher  |           0.558 |
| Drop organisation size        | long-term vs response |        1.263 |    0.850 |     1.876 |      1.026 | about 26% higher |           0.124 |
| Drop industry/sector          | response vs immediate |        1.023 |    0.561 |     1.867 |      1.306 | about 2% higher  |           0.530 |
| Drop industry/sector          | long-term vs response |        1.193 |    0.804 |     1.771 |      0.966 | about 19% higher |           0.190 |
| Drop size and industry/sector | response vs immediate |        1.032 |    0.566 |     1.882 |      1.316 | about 3% higher  |           0.541 |
| Drop size and industry/sector | long-term vs response |        1.311 |    0.887 |     1.937 |      1.050 | about 31% higher |           0.087 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.6416 |                         0.5495 |
| Drop organisation size        |    0.4665 |                         0.5578 |
| Drop industry/sector          |    0.6503 |                         0.5299 |
| Drop size and industry/sector |    0.3611 |                         0.5414 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.57-1.90). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.80-1.78).

## Delayed 91-180 Day Peak Sensitivity

This sensitivity reruns the elapsed-time model with bands `0-90`, `91-180`, `181-365`, `366-730`, `731-1460`, and `>1460` to test whether the apparent early elevation is concentrated in days 91-180.
A formal delayed peak requires the 91-180 day period to be higher than both 0-90 days and the pooled post-180 period after adjustment.

- adjusted elapsed-time LRT p-value for the 90/180 split: 0.7178
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

Interpretation:

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.486. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates have the hypothesised shape. The weakest part is 91-180 vs post-180 (p = 0.486).

- 91-180 vs 0-90: rate ratio 1.12 (95% CI 0.52-2.41), about 12% higher; one-sided p = 0.388 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.52 to 2.41, which includes 1. Repeat events: 13 in 91-180 days and 13 in 0-90 days.
- 91-180 vs post-180: rate ratio 1.01 (95% CI 0.56-1.82), about 1% higher; one-sided p = 0.486 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.56 to 1.82, which includes 1. Repeat events: 13 in 91-180 days and 95 in post-180 days.
- post-180 vs 0-90: rate ratio 1.11 (95% CI 0.62-1.99), about 11% higher; two-sided p = 0.735. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.62 to 1.99, which includes 1. Repeat events: 95 in post-180 days and 13 in 0-90 days.

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
