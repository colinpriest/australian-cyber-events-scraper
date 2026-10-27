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
- elapsed-time bands: 0-90, 91-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2011-04-19
- maximum event date: 2026-09-30
- first-of-month event dates: 193
- same-day entity-event groups collapsed: 5
- covariate coverage: {'size_estimate levels': {'HUGE': 166, 'LARGE': 195, 'MEDIUM': 171, 'SMALL': 51, 'UNKNOWN': 94}, 'size_confidence populated': 633, 'employee_count populated': 566, 'turnover populated': 0, 'industry populated': 621, 'entity_kind populated': 677, 'sector_proxy levels': {'education': 44, 'finance': 53, 'government': 111, 'health': 58, 'industrial': 13, 'media': 4, 'other': 236, 'retail': 24, 'technology': 116, 'telecom_transport': 18}, 'prior records populated': 328}

## Primary Piecewise Exponential Test

The adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level.

- unadjusted LRT p-value: 0.3213
- adjusted LRT p-value: 0.1216
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=technology; pooled sparse levels=4), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown; pooled sparse levels=1)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |        3 |       59588.000 |                       1.839 |              0.000 |               3.798 |
| 91-180         |        6 |       55471.000 |                       3.951 |              1.312 |               7.191 |
| 181-365        |        8 |       99112.000 |                       2.948 |              0.748 |               6.139 |
| 366-730        |        8 |      162084.000 |                       1.803 |              0.682 |               3.248 |
| 731-1460       |       17 |      190480.000 |                       3.260 |              1.755 |               5.043 |
| >1460          |       15 |      123954.000 |                       4.420 |              2.529 |               7.012 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        1.650 |    1.115 |     2.442 | about 65% higher repeat-event rate  |
| SMALL -> LARGE  |        2.722 |    1.242 |     5.963 | about 172% higher repeat-event rate |
| SMALL -> HUGE   |        4.490 |    1.384 |    14.563 | about 349% higher repeat-event rate |
| MEDIUM -> HUGE  |        2.722 |    1.242 |     5.963 | about 172% higher repeat-event rate |
| LARGE -> HUGE   |        1.650 |    1.115 |     2.442 | about 65% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                   |
|:--------------------|-------------:|---------:|----------:|:---------------------------------|
| telecom_transport   |        1.432 |    0.404 |     5.076 | about 43% higher than government |
| finance             |        1.015 |    0.415 |     2.480 | about 1% higher than government  |
| education           |        0.894 |    0.274 |     2.911 | about 11% lower than government  |
| sparse_or_no_repeat |        0.563 |    0.184 |     1.727 | about 44% lower than government  |
| government          |        0.256 |    0.069 |     0.955 | about 74% lower than government  |
| other               |        0.245 |    0.081 |     0.743 | about 76% lower than government  |

Full adjusted covariate table:

| effect                             |   rate_ratio |   ci_low |   ci_high |
|:-----------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body       |        4.605 |    1.430 |    14.827 |
| Entity kind: sparse_or_no_repeat   |        0.000 |    0.000 |   inf     |
| Organisation size                  |        1.650 |    1.115 |     2.442 |
| Prior records: 100k+               |        1.468 |    0.721 |     2.989 |
| Prior records: 10k-100k            |        2.472 |    1.183 |     5.166 |
| Prior records: 1k-10k              |        0.956 |    0.338 |     2.702 |
| Prior records: sparse_or_no_repeat |        0.397 |    0.052 |     3.032 |
| Sector: education                  |        0.894 |    0.274 |     2.911 |
| Sector: finance                    |        1.015 |    0.415 |     2.480 |
| Sector: government                 |        0.256 |    0.069 |     0.955 |
| Sector: other                      |        0.245 |    0.081 |     0.743 |
| Sector: sparse_or_no_repeat        |        0.563 |    0.184 |     1.727 |
| Sector: telecom_transport          |        1.432 |    0.404 |     5.076 |
| Unknown size                       |        0.777 |    0.269 |     2.250 |

Targeted contrast for the proposed low-then-rising pattern:

- 91-180, 181-365 days vs 0-90 days rate ratio: 1.799
- approximate 95% CI: 0.517 to 6.260

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.
The point estimates do not form the requested U-shape.

- phase-model LRT p-value versus constant elapsed-time risk: 0.4099
- directional U-shape p-value: 0.823

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |        3 |           59588 |                       1.839 |
| Response period  |       13 |          154583 |                       3.072 |
| Long-term period |       40 |          476518 |                       3.066 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language    |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:------------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        1.812 |    0.515 |     6.372 | about 81% higher  |         0.354 | less                      | 0.823           |
| long-term vs response  |        1.158 |    0.596 |     2.248 | about 16% higher  |         0.666 | greater                   | 0.333           |
| long-term vs immediate |        2.098 |    0.630 |     6.989 | about 110% higher |         0.228 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        1.812 |    0.515 |     6.372 |      5.856 | about 81% higher |           0.823 |
| All covariates                | long-term vs response |        1.158 |    0.596 |     2.248 |      1.652 | about 16% higher |           0.333 |
| Drop organisation size        | response vs immediate |        1.818 |    0.517 |     6.389 |      5.872 | about 82% higher |           0.824 |
| Drop organisation size        | long-term vs response |        1.213 |    0.625 |     2.351 |      1.725 | about 21% higher |           0.284 |
| Drop industry/sector          | response vs immediate |        1.768 |    0.503 |     6.212 |      5.709 | about 77% higher |           0.813 |
| Drop industry/sector          | long-term vs response |        1.204 |    0.620 |     2.335 |      1.715 | about 20% higher |           0.292 |
| Drop size and industry/sector | response vs immediate |        1.778 |    0.506 |     6.242 |      5.736 | about 78% higher |           0.815 |
| Drop size and industry/sector | long-term vs response |        1.281 |    0.664 |     2.473 |      1.809 | about 28% higher |           0.230 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.4099 |                         0.8230 |
| Drop organisation size        |    0.3478 |                         0.8243 |
| Drop industry/sector          |    0.3853 |                         0.8130 |
| Drop size and industry/sector |    0.2947 |                         0.8155 |

If dropping a covariate group materially narrowed the interval, the CI width would shrink in this table. In this run, the interval around the initial fall remains broad under all variants, which points more to sparse repeat-event information than to a single covariate group consuming all precision.

## Delayed 91-180 Day Peak Test

Definition used here: the hypothesised peak is 91-180 days after the prior event. A formal peak requires 91-180 days to be higher than both 0-90 days and the pooled post-180 period.
The adjusted peak model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main model.
The point estimates have a delayed 91-180 day peak, but the full directional test is not statistically strong.

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
