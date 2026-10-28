# Repeat Cyber-Event Timing Analysis

## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.
The primary model includes recorded organisation size. Size is a proxy for differences in visibility, attack exposure and target value, not a direct measure of those factors; residual confounding remains possible. The ordinal size term assumes equal log-rate steps between categories, UNKNOWN has a separate indicator, and current recorded size may differ from size at an incident's date. Unadjusted rates, plots, parametric models and size-dropped variants below are descriptive diagnostics and do not control for this confounding.

## Data Quality Summary

- victim event attributions: 677
- unique deduplicated events: 584
- victim entities: 615
- entities with more than one event: 45
- post-event spells: 672
- observed repeat spells: 57
- right-censored spells: 615
- censor date: 2026-10-28
- elapsed-time bands: 0-90, 91-365, 366-730, 731-1460, >1460
- minimum event date: 2011-04-19
- maximum event date: 2026-09-30
- first-of-month event dates: 193
- same-day entity-event groups collapsed: 5
- covariate coverage: {'size_estimate levels': {'HUGE': 166, 'LARGE': 195, 'MEDIUM': 171, 'SMALL': 51, 'UNKNOWN': 94}, 'size_confidence populated': 633, 'employee_count populated': 566, 'turnover populated': 0, 'industry populated': 621, 'entity_kind populated': 677, 'sector_proxy levels': {'education': 44, 'finance': 53, 'government': 111, 'health': 58, 'industrial': 13, 'media': 4, 'other': 236, 'retail': 24, 'technology': 116, 'telecom_transport': 18}, 'prior records populated': 328}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 18, 'events with an unresolved attribution': 18, 'organisations with an unresolved attribution': 18, 'victim-event attributions analysed': 677, 'repeats': {'included': {'observed repeats': 57, 'entities with a repeat': 42, 'repeats by elapsed band': {'0-90': 3, '91-365': 14, '366-730': 8, '731-1460': 17, '>1460': 15}}, 'excluded': {'observed repeats': 52, 'entities with a repeat': 38, 'repeats by elapsed band': {'0-90': 1, '91-365': 14, '366-730': 8, '731-1460': 16, '>1460': 13}}, 'repeats involving an unresolved attribution': 5}}

## Primary Piecewise Exponential Test

The adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.080, borderline). It rests on 57 observed repeats.

- unadjusted LRT p-value: 0.2346
- adjusted LRT p-value: 0.07986
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=technology; pooled sparse levels=4), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown; pooled sparse levels=1)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |        3 |       59588.000 |                       1.839 |              0.000 |               3.798 |
| 91-365         |       14 |      154583.000 |                       3.308 |              1.452 |               5.782 |
| 366-730        |        8 |      162084.000 |                       1.803 |              0.682 |               3.248 |
| 731-1460       |       17 |      190480.000 |                       3.260 |              1.755 |               5.043 |
| >1460          |       15 |      123954.000 |                       4.420 |              2.529 |               7.012 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        1.652 |    1.116 |     2.445 | about 65% higher repeat-event rate  |
| SMALL -> LARGE  |        2.728 |    1.245 |     5.979 | about 173% higher repeat-event rate |
| SMALL -> HUGE   |        4.506 |    1.389 |    14.618 | about 351% higher repeat-event rate |
| MEDIUM -> HUGE  |        2.728 |    1.245 |     5.979 | about 173% higher repeat-event rate |
| LARGE -> HUGE   |        1.652 |    1.116 |     2.445 | about 65% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                   |
|:--------------------|-------------:|---------:|----------:|:---------------------------------|
| telecom_transport   |        1.423 |    0.402 |     5.040 | about 42% higher than government |
| finance             |        1.014 |    0.414 |     2.482 | about 1% higher than government  |
| education           |        0.900 |    0.276 |     2.937 | about 10% lower than government  |
| sparse_or_no_repeat |        0.563 |    0.184 |     1.725 | about 44% lower than government  |
| government          |        0.255 |    0.068 |     0.951 | about 75% lower than government  |
| other               |        0.244 |    0.081 |     0.742 | about 76% lower than government  |

Full adjusted covariate table:

| effect                             |   rate_ratio |   ci_low |   ci_high |
|:-----------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body       |        4.616 |    1.428 |    14.928 |
| Entity kind: sparse_or_no_repeat   |        0.000 |    0.000 |   inf     |
| Organisation size                  |        1.652 |    1.116 |     2.445 |
| Prior records: 100k+               |        1.476 |    0.725 |     3.006 |
| Prior records: 10k-100k            |        2.473 |    1.183 |     5.170 |
| Prior records: 1k-10k              |        0.953 |    0.337 |     2.695 |
| Prior records: sparse_or_no_repeat |        0.397 |    0.052 |     3.037 |
| Sector: education                  |        0.900 |    0.276 |     2.937 |
| Sector: finance                    |        1.014 |    0.414 |     2.482 |
| Sector: government                 |        0.255 |    0.068 |     0.951 |
| Sector: other                      |        0.244 |    0.081 |     0.742 |
| Sector: sparse_or_no_repeat        |        0.563 |    0.184 |     1.725 |
| Sector: telecom_transport          |        1.423 |    0.402 |     5.040 |
| Unknown size                       |        0.775 |    0.267 |     2.245 |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 91-365, 366-730 days vs 0-90 days (unadjusted): rate ratio 1.38 (95% CI 0.41-4.61), about 38% higher; two-sided p = 0.601. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.41 to 4.61, which includes 1. Repeat events: 22 in 91-365, 366-730 days and 3 in 0-90 days. Small count (3 in 0-90 days; fewer than 10): the estimate is imprecise and a few events either way could change it materially.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.761. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates do not have the hypothesised shape: response vs immediate goes the other way.

- response vs immediate: rate ratio 1.55 (95% CI 0.46-5.24), about 55% higher; one-sided p = 0.761 (alternative: lower). Not statistically significant at the 5% level: the data do not reject the null of no decrease (rate ratio at least 1); the 95% CI runs from 0.46 to 5.24, which includes 1. The estimate is in the opposite direction to the hypothesis (higher, not lower). Repeat events: 21 in the response period and 3 in the immediate period. Small count (3 in the immediate period; fewer than 10): the estimate is imprecise and a few events either way could change it materially.
- long-term vs response: rate ratio 1.95 (95% CI 1.05-3.61), about 95% higher; one-sided p = 0.017 (alternative: higher). Statistically significant at the 5% level: the data reject the null of no increase (rate ratio at most 1). Repeat events: 32 in the long-term period and 21 in the response period.
- long-term vs immediate: rate ratio 3.02 (95% CI 0.87-10.50), about 202% higher; two-sided p = 0.082. Not statistically significant at the 5% level (borderline): the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.87 to 10.50, which includes 1. Repeat events: 32 in the long-term period and 3 in the immediate period. Small count (3 in the immediate period; fewer than 10): the estimate is imprecise and a few events either way could change it materially.

- phase-model LRT p-value versus constant elapsed-time risk: 0.04359
- directional U-shape p-value: 0.7611

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |        3 |           59588 |                       1.839 |
| Response period  |       21 |          316667 |                       2.422 |
| Long-term period |       32 |          314434 |                       3.717 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language    |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:------------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        1.554 |    0.460 |     5.244 | about 55% higher  |         0.478 | less                      | 0.761           |
| long-term vs response  |        1.945 |    1.049 |     3.605 | about 95% higher  |         0.035 | greater                   | 0.017           |
| long-term vs immediate |        3.022 |    0.870 |    10.500 | about 202% higher |         0.082 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language    |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:------------------|----------------:|
| All covariates                | response vs immediate |        1.554 |    0.460 |     5.244 |      4.784 | about 55% higher  |           0.761 |
| All covariates                | long-term vs response |        1.945 |    1.049 |     3.605 |      2.556 | about 95% higher  |           0.017 |
| Drop organisation size        | response vs immediate |        1.569 |    0.465 |     5.290 |      4.824 | about 57% higher  |           0.766 |
| Drop organisation size        | long-term vs response |        2.057 |    1.109 |     3.814 |      2.705 | about 106% higher |           0.011 |
| Drop industry/sector          | response vs immediate |        1.525 |    0.452 |     5.140 |      4.688 | about 52% higher  |           0.752 |
| Drop industry/sector          | long-term vs response |        2.058 |    1.111 |     3.815 |      2.704 | about 106% higher |           0.011 |
| Drop size and industry/sector | response vs immediate |        1.538 |    0.457 |     5.178 |      4.721 | about 54% higher  |           0.757 |
| Drop size and industry/sector | long-term vs response |        2.222 |    1.204 |     4.100 |      2.896 | about 122% higher |           0.005 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.0436 |                         0.7611 |
| Drop organisation size        |    0.0262 |                         0.7662 |
| Drop industry/sector          |    0.0284 |                         0.7518 |
| Drop size and industry/sector |    0.0125 |                         0.7565 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.46-5.24). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 1.05-3.61).

## Delayed 91-180 Day Peak Sensitivity

This sensitivity reruns the elapsed-time model with bands `0-90`, `91-180`, `181-365`, `366-730`, `731-1460`, and `>1460` to test whether the apparent early elevation is concentrated in days 91-180.
A formal delayed peak requires the 91-180 day period to be higher than both 0-90 days and the pooled post-180 period after adjustment.

- adjusted elapsed-time LRT p-value for the 90/180 split: 0.1216
- phase-model LRT p-value versus constant elapsed-time risk: 0.4353
- directional delayed-peak p-value: 0.3937

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |        3 |           59588 |                       1.839 |
| Delayed peak 91-180 days |        6 |           55471 |                       3.951 |
| Post-180 days            |       47 |          575630 |                       2.982 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language    |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:------------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        2.206 |    0.551 |     8.831 | about 121% higher |         0.264 | greater                   | 0.132           |
| 91-180 vs post-180 |        1.127 |    0.471 |     2.696 | about 13% higher  |         0.787 | greater                   | 0.394           |
| post-180 vs 0-90   |        1.956 |    0.596 |     6.419 | about 96% higher  |         0.268 |                           |                 |

Interpretation:

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.394. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates have the hypothesised shape. The weakest part is 91-180 vs post-180 (p = 0.394).

- 91-180 vs 0-90: rate ratio 2.21 (95% CI 0.55-8.83), about 121% higher; one-sided p = 0.132 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.55 to 8.83, which includes 1. Repeat events: 6 in 91-180 days and 3 in 0-90 days. Small count (6 in 91-180 days, 3 in 0-90 days; fewer than 10): the estimate is imprecise and a few events either way could change it materially.
- 91-180 vs post-180: rate ratio 1.13 (95% CI 0.47-2.70), about 13% higher; one-sided p = 0.394 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.47 to 2.70, which includes 1. Repeat events: 6 in 91-180 days and 47 in post-180 days. Small count (6 in 91-180 days; fewer than 10): the estimate is imprecise and a few events either way could change it materially.
- post-180 vs 0-90: rate ratio 1.96 (95% CI 0.60-6.42), about 96% higher; two-sided p = 0.268. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.60 to 6.42, which includes 1. Repeat events: 47 in post-180 days and 3 in 0-90 days. Small count (3 in 0-90 days; fewer than 10): the estimate is imprecise and a few events either way could change it materially.

## Unadjusted Parametric Survival Model Comparison (Descriptive)

Best AIC model: exponential. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| exponential       |         -592.936 |            1 | 1187.873 | 1192.383 | True        |
| weibull           |         -592.409 |            2 | 1188.818 | 1197.838 | True        |
| loglogistic       |         -592.784 |            2 | 1189.568 | 1198.589 | True        |
| generalized_gamma |         -592.158 |            3 | 1190.315 | 1203.846 | True        |
| lognormal         |         -596.733 |            2 | 1197.466 | 1206.486 | True        |

Exponential vs Weibull LRT p-value: 0.3043

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
