# Repeat Cyber-Event Timing Analysis

## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.

## Data Quality Summary

- victim event attributions: 757
- unique deduplicated events: 672
- victim entities: 652
- entities with more than one event: 60
- post-event spells: 753
- observed repeat spells: 102
- right-censored spells: 651
- censor date: 2026-10-02
- elapsed-time bands: 0-90, 91-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2011-04-19
- maximum event date: 2026-10-02
- first-of-month event dates: 266
- same-day entity-event groups collapsed: 3
- covariate coverage: {'size_estimate levels': {'HUGE': 208, 'LARGE': 228, 'MEDIUM': 193, 'SMALL': 63, 'UNKNOWN': 65}, 'size_confidence populated': 757, 'employee_count populated': 671, 'turnover populated': 0, 'industry populated': 715, 'entity_kind populated': 757, 'sector_proxy levels': {'education': 53, 'finance': 61, 'government': 134, 'health': 71, 'industrial': 14, 'media': 7, 'other': 248, 'retail': 28, 'technology': 120, 'telecom_transport': 21}, 'prior records populated': 336}

## Primary Piecewise Exponential Test

The adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level.

- unadjusted LRT p-value: 0.1252
- adjusted LRT p-value: 0.1474
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=2), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown; pooled sparse levels=1)
- covariates skipped: {}

Piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |       11 |       65715.000 |                       6.114 |              2.305 |              10.413 |
| 91-180         |       17 |       60127.000 |                      10.327 |              4.490 |              16.304 |
| 181-365        |       12 |      108346.000 |                       4.045 |              1.361 |               7.567 |
| 366-730        |       22 |      172549.000 |                       4.657 |              2.593 |               6.886 |
| 731-1460       |       25 |      194682.000 |                       4.690 |              2.977 |               6.644 |
| >1460          |       15 |      126827.000 |                       4.320 |              2.092 |               6.976 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        1.782 |    1.292 |     2.457 | about 78% higher repeat-event rate  |
| SMALL -> LARGE  |        3.175 |    1.670 |     6.035 | about 217% higher repeat-event rate |
| SMALL -> HUGE   |        5.656 |    2.158 |    14.825 | about 466% higher repeat-event rate |
| MEDIUM -> HUGE  |        3.175 |    1.670 |     6.035 | about 217% higher repeat-event rate |
| LARGE -> HUGE   |        1.782 |    1.292 |     2.457 | about 78% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |                                                                                                                                                                                       ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------:|:----------------------------------|
| education           |        2.345 |    1.138 |                                                                                                                                                                                         4.833 | about 134% higher than government |
| industrial          |        1.916 |    0.524 |                                                                                                                                                                                         7.013 | about 92% higher than government  |
| finance             |        1.714 |    0.797 |                                                                                                                                                                                         3.685 | about 71% higher than government  |
| telecom_transport   |        1.445 |    0.536 |                                                                                                                                                                                         3.900 | about 45% higher than government  |
| health              |        0.813 |    0.301 |                                                                                                                                                                                         2.193 | about 19% lower than government   |
| technology          |        0.514 |    0.227 |                                                                                                                                                                                         1.168 | about 49% lower than government   |
| other               |        0.142 |    0.046 |                                                                                                                                                                                         0.434 | about 86% lower than government   |
| sparse_or_no_repeat |        0.000 |    0.000 | 26257827851676035665071179827183727015467509618905871617361479667107704263471424928356991701704327297407825447526487454606508936921402618118576503938338782610878239686080303759928328192.000 | about 100% lower than government  |

Full adjusted covariate table:

| effect                             |   rate_ratio |   ci_low |                                                                                                                                                                                       ci_high |
|:-----------------------------------|-------------:|---------:|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------:|
| Entity kind: government_body       |        1.889 |    0.942 |                                                                                                                                                                                         3.789 |
| Entity kind: sparse_or_no_repeat   |        0.000 |    0.000 |                                                                                                                                                                                       inf     |
| Organisation size                  |        1.782 |    1.292 |                                                                                                                                                                                         2.457 |
| Prior records: 100k+               |        1.306 |    0.716 |                                                                                                                                                                                         2.380 |
| Prior records: 10k-100k            |        1.592 |    0.953 |                                                                                                                                                                                         2.661 |
| Prior records: 1k-10k              |        0.748 |    0.366 |                                                                                                                                                                                         1.529 |
| Prior records: sparse_or_no_repeat |        0.326 |    0.078 |                                                                                                                                                                                         1.371 |
| Sector: education                  |        2.345 |    1.138 |                                                                                                                                                                                         4.833 |
| Sector: finance                    |        1.714 |    0.797 |                                                                                                                                                                                         3.685 |
| Sector: health                     |        0.813 |    0.301 |                                                                                                                                                                                         2.193 |
| Sector: industrial                 |        1.916 |    0.524 |                                                                                                                                                                                         7.013 |
| Sector: other                      |        0.142 |    0.046 |                                                                                                                                                                                         0.434 |
| Sector: sparse_or_no_repeat        |        0.000 |    0.000 | 26257827851676035665071179827183727015467509618905871617361479667107704263471424928356991701704327297407825447526487454606508936921402618118576503938338782610878239686080303759928328192.000 |
| Sector: technology                 |        0.514 |    0.227 |                                                                                                                                                                                         1.168 |
| Sector: telecom_transport          |        1.445 |    0.536 |                                                                                                                                                                                         3.900 |
| Unknown size                       |        1.584 |    0.749 |                                                                                                                                                                                         3.348 |

Targeted contrast for the proposed low-then-rising pattern:

- 91-180, 181-365 days vs 0-90 days rate ratio: 1.028
- approximate 95% CI: 0.514 to 2.059

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.
The point estimates do not form the requested U-shape.

- phase-model LRT p-value versus constant elapsed-time risk: 0.8984
- directional U-shape p-value: 0.57

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       11 |           65715 |                       6.114 |
| Response period  |       28 |          168473 |                       6.070 |
| Long-term period |       61 |          494058 |                       4.510 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        1.065 |    0.530 |     2.141 | about 6% higher  |         0.860 | less                      | 0.570           |
| long-term vs response  |        1.078 |    0.675 |     1.721 | about 8% higher  |         0.753 | greater                   | 0.377           |
| long-term vs immediate |        1.148 |    0.592 |     2.224 | about 15% higher |         0.683 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants test whether the wide U-shape confidence intervals are mainly caused by including organisation size or industry/sector controls.

Adjusted phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        1.065 |    0.530 |     2.141 |      1.611 | about 6% higher  |           0.570 |
| All covariates                | long-term vs response |        1.078 |    0.675 |     1.721 |      1.047 | about 8% higher  |           0.377 |
| Drop organisation size        | response vs immediate |        1.057 |    0.526 |     2.125 |      1.599 | about 6% higher  |           0.562 |
| Drop organisation size        | long-term vs response |        1.098 |    0.689 |     1.749 |      1.060 | about 10% higher |           0.347 |
| Drop industry/sector          | response vs immediate |        1.066 |    0.530 |     2.143 |      1.612 | about 7% higher  |           0.571 |
| Drop industry/sector          | long-term vs response |        0.965 |    0.607 |     1.535 |      0.928 | about 4% lower   |           0.560 |
| Drop size and industry/sector | response vs immediate |        1.069 |    0.532 |     2.149 |      1.617 | about 7% higher  |           0.575 |
| Drop size and industry/sector | long-term vs response |        1.005 |    0.633 |     1.595 |      0.962 | about 1% higher  |           0.491 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.8984 |                         0.5700 |
| Drop organisation size        |    0.8688 |                         0.5620 |
| Drop industry/sector          |    0.9806 |                         0.5712 |
| Drop size and industry/sector |    0.9761 |                         0.5745 |

If dropping a covariate group materially narrowed the interval, the CI width would shrink in this table. In this run, the interval around the initial fall remains broad under all variants, which points more to sparse repeat-event information than to a single covariate group consuming all precision.

## Delayed 91-180 Day Peak Test

Definition used here: the hypothesised peak is 91-180 days after the prior event. A formal peak requires 91-180 days to be higher than both 0-90 days and the pooled post-180 period.
The adjusted peak model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main model.
The point estimates have a delayed 91-180 day peak, but the full directional test is not statistically strong.

- phase-model LRT p-value versus constant elapsed-time risk: 0.155
- directional delayed-peak p-value: 0.07492

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |       11 |           65715 |                       6.114 |
| Delayed peak 91-180 days |       17 |           60127 |                      10.327 |
| Post-180 days            |       72 |          602404 |                       4.366 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        1.746 |    0.818 |     3.730 | about 75% higher |         0.150 | greater                   | 0.075           |
| 91-180 vs post-180 |        1.739 |    1.013 |     2.985 | about 74% higher |         0.045 | greater                   | 0.022           |
| post-180 vs 0-90   |        1.004 |    0.526 |     1.917 | about 0% higher  |         0.990 |                           |                 |

## Parametric Survival Model Comparison

Best AIC model: exponential. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| exponential       |        -1007.089 |            1 | 2016.178 | 2020.802 | True        |
| weibull           |        -1006.305 |            2 | 2016.610 | 2025.859 | True        |
| loglogistic       |        -1006.485 |            2 | 2016.970 | 2026.218 | True        |
| lognormal         |        -1006.691 |            2 | 2017.382 | 2026.630 | True        |
| generalized_gamma |        -1006.284 |            3 | 2018.568 | 2032.440 | True        |

Exponential vs Weibull LRT p-value: 0.2106

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
