# Repeat Cyber-Event Timing Analysis

## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.

## Data Quality Summary

- victim event attributions: 697
- unique deduplicated events: 614
- victim entities: 623
- entities with more than one event: 50
- post-event spells: 697
- observed repeat spells: 74
- right-censored spells: 623
- censor date: 2026-10-02
- elapsed-time bands: 0-90, 91-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2011-04-19
- maximum event date: 2026-09-30
- first-of-month event dates: 229
- same-day entity-event groups collapsed: 0
- covariate coverage: {'size_estimate levels': {'HUGE': 185, 'LARGE': 207, 'MEDIUM': 186, 'SMALL': 60, 'UNKNOWN': 59}, 'size_confidence populated': 695, 'employee_count populated': 616, 'turnover populated': 0, 'industry populated': 655, 'entity_kind populated': 697, 'sector_proxy levels': {'education': 45, 'finance': 53, 'government': 122, 'health': 64, 'industrial': 13, 'media': 7, 'other': 230, 'retail': 28, 'technology': 117, 'telecom_transport': 18}, 'prior records populated': 328}

## Primary Piecewise Exponential Test

The adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level.

- unadjusted LRT p-value: 0.01557
- adjusted LRT p-value: 0.007186
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=4), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown; pooled sparse levels=1)
- covariates skipped: {}

Piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |        4 |       60995.000 |                       2.395 |              0.603 |               4.684 |
| 91-180         |       15 |       55949.000 |                       9.792 |              4.597 |              15.855 |
| 181-365        |        9 |      101606.000 |                       3.235 |              1.060 |               6.633 |
| 366-730        |       11 |      164987.000 |                       2.435 |              1.109 |               4.104 |
| 731-1460       |       22 |      192214.000 |                       4.180 |              2.661 |               5.952 |
| >1460          |       13 |      125575.000 |                       3.781 |              1.781 |               6.318 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        1.681 |    1.177 |     2.401 | about 68% higher repeat-event rate  |
| SMALL -> LARGE  |        2.827 |    1.386 |     5.764 | about 183% higher repeat-event rate |
| SMALL -> HUGE   |        4.753 |    1.632 |    13.838 | about 375% higher repeat-event rate |
| MEDIUM -> HUGE  |        2.827 |    1.386 |     5.764 | about 183% higher repeat-event rate |
| LARGE -> HUGE   |        1.681 |    1.177 |     2.401 | about 68% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                   |
|:--------------------|-------------:|---------:|----------:|:---------------------------------|
| finance             |        1.262 |    0.507 |     3.143 | about 26% higher than government |
| education           |        1.086 |    0.393 |     3.000 | about 9% higher than government  |
| telecom_transport   |        0.927 |    0.255 |     3.368 | about 7% lower than government   |
| technology          |        0.891 |    0.405 |     1.958 | about 11% lower than government  |
| health              |        0.844 |    0.287 |     2.481 | about 16% lower than government  |
| sparse_or_no_repeat |        0.097 |    0.027 |     0.346 | about 90% lower than government  |

Full adjusted covariate table:

| effect                             |   rate_ratio |   ci_low |   ci_high |
|:-----------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body       |        1.702 |    0.788 |     3.677 |
| Entity kind: sparse_or_no_repeat   |        0.000 |    0.000 |   inf     |
| Organisation size                  |        1.681 |    1.177 |     2.401 |
| Prior records: 100k+               |        0.960 |    0.459 |     2.007 |
| Prior records: 10k-100k            |        1.745 |    0.934 |     3.262 |
| Prior records: 1k-10k              |        1.017 |    0.467 |     2.218 |
| Prior records: sparse_or_no_repeat |        0.493 |    0.115 |     2.118 |
| Sector: education                  |        1.086 |    0.393 |     3.000 |
| Sector: finance                    |        1.262 |    0.507 |     3.143 |
| Sector: health                     |        0.844 |    0.287 |     2.481 |
| Sector: sparse_or_no_repeat        |        0.097 |    0.027 |     0.346 |
| Sector: technology                 |        0.891 |    0.405 |     1.958 |
| Sector: telecom_transport          |        0.927 |    0.255 |     3.368 |
| Unknown size                       |        1.645 |    0.728 |     3.716 |

Targeted contrast for the proposed low-then-rising pattern:

- 91-180, 181-365 days vs 0-90 days rate ratio: 2.323
- approximate 95% CI: 0.806 to 6.695

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.
The point estimates do not form the requested U-shape.

- phase-model LRT p-value versus constant elapsed-time risk: 0.2078
- directional U-shape p-value: 0.9462

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |        4 |           60995 |                       2.395 |
| Response period  |       23 |          157555 |                       5.332 |
| Long-term period |       45 |          482776 |                       3.405 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language    |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:------------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        2.394 |    0.827 |     6.931 | about 139% higher |         0.108 | less                      | 0.946           |
| long-term vs response  |        0.841 |    0.490 |     1.443 | about 16% lower   |         0.530 | greater                   | 0.735           |
| long-term vs immediate |        2.013 |    0.704 |     5.757 | about 101% higher |         0.192 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants test whether the wide U-shape confidence intervals are mainly caused by including organisation size or industry/sector controls.

Adjusted phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language    |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:------------------|----------------:|
| All covariates                | response vs immediate |        2.394 |    0.827 |     6.931 |      6.105 | about 139% higher |           0.946 |
| All covariates                | long-term vs response |        0.841 |    0.490 |     1.443 |      0.953 | about 16% lower   |           0.735 |
| Drop organisation size        | response vs immediate |        2.395 |    0.827 |     6.935 |      6.108 | about 140% higher |           0.946 |
| Drop organisation size        | long-term vs response |        0.886 |    0.518 |     1.517 |      0.999 | about 11% lower   |           0.670 |
| Drop industry/sector          | response vs immediate |        2.402 |    0.829 |     6.955 |      6.126 | about 140% higher |           0.947 |
| Drop industry/sector          | long-term vs response |        0.821 |    0.478 |     1.411 |      0.933 | about 18% lower   |           0.762 |
| Drop size and industry/sector | response vs immediate |        2.415 |    0.834 |     6.994 |      6.159 | about 142% higher |           0.948 |
| Drop size and industry/sector | long-term vs response |        0.899 |    0.525 |     1.538 |      1.013 | about 10% lower   |           0.651 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.2078 |                         0.9462 |
| Drop organisation size        |    0.2024 |                         0.9463 |
| Drop industry/sector          |    0.2049 |                         0.9468 |
| Drop size and industry/sector |    0.1927 |                         0.9480 |

If dropping a covariate group materially narrowed the interval, the CI width would shrink in this table. In this run, the interval around the initial fall remains broad under all variants, which points more to sparse repeat-event information than to a single covariate group consuming all precision.

## Delayed 91-180 Day Peak Test

Definition used here: the hypothesised peak is 91-180 days after the prior event. A formal peak requires 91-180 days to be higher than both 0-90 days and the pooled post-180 period.
The adjusted peak model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main model.
The adjusted model supports a delayed 91-180 day peak at the 5% directional level.

- phase-model LRT p-value versus constant elapsed-time risk: 0.008296
- directional delayed-peak p-value: 0.005233

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |        4 |           60995 |                       2.395 |
| Delayed peak 91-180 days |       15 |           55949 |                       9.792 |
| Post-180 days            |       53 |          584382 |                       3.313 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language    |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:------------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        4.226 |    1.402 |    12.737 | about 323% higher |         0.010 | greater                   | 0.005           |
| 91-180 vs post-180 |        2.357 |    1.298 |     4.281 | about 136% higher |         0.005 | greater                   | 0.002           |
| post-180 vs 0-90   |        1.793 |    0.638 |     5.039 | about 79% higher  |         0.268 |                           |                 |

## Parametric Survival Model Comparison

Best AIC model: exponential. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| exponential       |         -751.593 |            1 | 1505.186 | 1509.733 | True        |
| weibull           |         -751.567 |            2 | 1507.133 | 1516.227 | True        |
| loglogistic       |         -751.633 |            2 | 1507.266 | 1516.359 | True        |
| lognormal         |         -751.725 |            2 | 1507.450 | 1516.543 | True        |
| generalized_gamma |         -751.592 |            3 | 1509.184 | 1522.824 | True        |

Exponential vs Weibull LRT p-value: 0.8185

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
