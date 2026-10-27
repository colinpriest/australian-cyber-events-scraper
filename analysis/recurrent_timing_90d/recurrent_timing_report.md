# Repeat Cyber-Event Timing Analysis

## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.

## Data Quality Summary

- victim event attributions: 758
- unique deduplicated events: 673
- victim entities: 658
- entities with more than one event: 58
- post-event spells: 754
- observed repeat spells: 97
- right-censored spells: 657
- censor date: 2026-10-02
- elapsed-time bands: 0-90, 91-365, 366-730, 731-1460, >1460
- minimum event date: 2011-04-19
- maximum event date: 2026-10-02
- first-of-month event dates: 267
- same-day entity-event groups collapsed: 3
- covariate coverage: {'size_estimate levels': {'HUGE': 206, 'LARGE': 227, 'MEDIUM': 195, 'SMALL': 63, 'UNKNOWN': 67}, 'size_confidence populated': 756, 'employee_count populated': 669, 'turnover populated': 0, 'industry populated': 713, 'entity_kind populated': 758, 'sector_proxy levels': {'education': 53, 'finance': 60, 'government': 134, 'health': 71, 'industrial': 14, 'media': 7, 'other': 248, 'retail': 28, 'technology': 120, 'telecom_transport': 23}, 'prior records populated': 337}

## Primary Piecewise Exponential Test

The adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level.

- unadjusted LRT p-value: 0.6139
- adjusted LRT p-value: 0.259
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=3), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown; pooled sparse levels=1)
- covariates skipped: {}

Piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |        7 |       65919.000 |                       3.879 |              1.121 |               7.116 |
| 91-365         |       29 |      169532.000 |                       6.248 |              3.206 |               9.488 |
| 366-730        |       20 |      174226.000 |                       4.193 |              2.321 |               6.385 |
| 731-1460       |       25 |      197776.000 |                       4.617 |              3.053 |               6.423 |
| >1460          |       16 |      129412.000 |                       4.516 |              2.220 |               6.852 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        1.743 |    1.265 |     2.402 | about 74% higher repeat-event rate  |
| SMALL -> LARGE  |        3.038 |    1.601 |     5.768 | about 204% higher repeat-event rate |
| SMALL -> HUGE   |        5.296 |    2.025 |    13.851 | about 430% higher repeat-event rate |
| MEDIUM -> HUGE  |        3.038 |    1.601 |     5.768 | about 204% higher repeat-event rate |
| LARGE -> HUGE   |        1.743 |    1.265 |     2.402 | about 74% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| education           |        2.209 |    1.061 |     4.596 | about 121% higher than government |
| industrial          |        1.858 |    0.507 |     6.809 | about 86% higher than government  |
| finance             |        1.535 |    0.690 |     3.415 | about 54% higher than government  |
| telecom_transport   |        1.192 |    0.424 |     3.348 | about 19% higher than government  |
| health              |        1.003 |    0.389 |     2.581 | about 0% higher than government   |
| technology          |        0.588 |    0.260 |     1.330 | about 41% lower than government   |
| sparse_or_no_repeat |        0.062 |    0.014 |     0.275 | about 94% lower than government   |

Full adjusted covariate table:

| effect                             |   rate_ratio |   ci_low |                                                                                                                                                                                                                                                                                                                  ci_high |
|:-----------------------------------|-------------:|---------:|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------:|
| Entity kind: government_body       |        1.884 |    0.935 |                                                                                                                                                                                                                                                                                                                    3.797 |
| Entity kind: sparse_or_no_repeat   |        0.000 |    0.000 | 14119379179557549718616932894243134789164226503217681769748612084730830551874745298468141827235891434821820574112394227777971217766261823336791948268476032528572508888824663562127199634078724777587266754787219922376869387489324015047302415138169315226431312923759274824270101578630049382587731633418406985728.000 |
| Organisation size                  |        1.743 |    1.265 |                                                                                                                                                                                                                                                                                                                    2.402 |
| Prior records: 100k+               |        0.991 |    0.524 |                                                                                                                                                                                                                                                                                                                    1.871 |
| Prior records: 10k-100k            |        1.656 |    0.978 |                                                                                                                                                                                                                                                                                                                    2.804 |
| Prior records: 1k-10k              |        0.733 |    0.355 |                                                                                                                                                                                                                                                                                                                    1.513 |
| Prior records: sparse_or_no_repeat |        0.340 |    0.081 |                                                                                                                                                                                                                                                                                                                    1.433 |
| Sector: education                  |        2.209 |    1.061 |                                                                                                                                                                                                                                                                                                                    4.596 |
| Sector: finance                    |        1.535 |    0.690 |                                                                                                                                                                                                                                                                                                                    3.415 |
| Sector: health                     |        1.003 |    0.389 |                                                                                                                                                                                                                                                                                                                    2.581 |
| Sector: industrial                 |        1.858 |    0.507 |                                                                                                                                                                                                                                                                                                                    6.809 |
| Sector: sparse_or_no_repeat        |        0.062 |    0.014 |                                                                                                                                                                                                                                                                                                                    0.275 |
| Sector: technology                 |        0.588 |    0.260 |                                                                                                                                                                                                                                                                                                                    1.330 |
| Sector: telecom_transport          |        1.192 |    0.424 |                                                                                                                                                                                                                                                                                                                    3.348 |
| Unknown size                       |        1.609 |    0.755 |                                                                                                                                                                                                                                                                                                                    3.430 |

Targeted contrast for the proposed low-then-rising pattern:

- 91-365, 366-730 days vs 0-90 days rate ratio: 1.342
- approximate 95% CI: 0.608 to 2.963

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.
The point estimates do not form the requested U-shape.

- phase-model LRT p-value versus constant elapsed-time risk: 0.1003
- directional U-shape p-value: 0.8626

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |        7 |           65919 |                       3.879 |
| Response period  |       47 |          343758 |                       4.994 |
| Long-term period |       41 |          327188 |                       4.577 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language    |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:------------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        1.559 |    0.703 |     3.461 | about 56% higher  |         0.275 | less                      | 0.863           |
| long-term vs response  |        1.426 |    0.903 |     2.251 | about 43% higher  |         0.128 | greater                   | 0.064           |
| long-term vs immediate |        2.223 |    0.963 |     5.134 | about 122% higher |         0.061 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants test whether the wide U-shape confidence intervals are mainly caused by including organisation size or industry/sector controls.

Adjusted phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        1.559 |    0.703 |     3.461 |      2.758 | about 56% higher |           0.863 |
| All covariates                | long-term vs response |        1.426 |    0.903 |     2.251 |      1.347 | about 43% higher |           0.064 |
| Drop organisation size        | response vs immediate |        1.548 |    0.698 |     3.434 |      2.736 | about 55% higher |           0.859 |
| Drop organisation size        | long-term vs response |        1.459 |    0.927 |     2.294 |      1.367 | about 46% higher |           0.051 |
| Drop industry/sector          | response vs immediate |        1.549 |    0.698 |     3.436 |      2.737 | about 55% higher |           0.859 |
| Drop industry/sector          | long-term vs response |        1.352 |    0.858 |     2.132 |      1.274 | about 35% higher |           0.097 |
| Drop size and industry/sector | response vs immediate |        1.554 |    0.701 |     3.446 |      2.745 | about 55% higher |           0.861 |
| Drop size and industry/sector | long-term vs response |        1.421 |    0.905 |     2.231 |      1.325 | about 42% higher |           0.063 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.1003 |                         0.8626 |
| Drop organisation size        |    0.0837 |                         0.8586 |
| Drop industry/sector          |    0.1513 |                         0.8591 |
| Drop size and industry/sector |    0.0994 |                         0.8612 |

If dropping a covariate group materially narrowed the interval, the CI width would shrink in this table. In this run, the interval around the initial fall remains broad under all variants, which points more to sparse repeat-event information than to a single covariate group consuming all precision.

## Delayed 91-180 Day Peak Sensitivity

This sensitivity reruns the elapsed-time model with bands `0-90`, `91-180`, `181-365`, `366-730`, `731-1460`, and `>1460` to test whether the apparent early elevation is concentrated in days 91-180.
A formal delayed peak requires the 91-180 day period to be higher than both 0-90 days and the pooled post-180 period after adjustment.

- adjusted elapsed-time LRT p-value for the 90/180 split: 0.04075
- phase-model LRT p-value versus constant elapsed-time risk: 0.05036
- directional delayed-peak p-value: 0.02141

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |        7 |           65919 |                       3.879 |
| Delayed peak 91-180 days |       17 |           60446 |                      10.272 |
| Post-180 days            |       71 |          610500 |                       4.248 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language    |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:------------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        2.741 |    1.136 |     6.613 | about 174% higher |         0.025 | greater                   | 0.012           |
| 91-180 vs post-180 |        1.754 |    1.018 |     3.020 | about 75% higher  |         0.043 | greater                   | 0.021           |
| post-180 vs 0-90   |        1.563 |    0.710 |     3.439 | about 56% higher  |         0.267 |                           |                 |

Interpretation: the 91-180 day period is much higher than the first 90 days, and that contrast is statistically strong. The stricter test that it is higher than both sides is suggestive rather than definitive because the 91-180 versus post-180 comparison is weaker.

## Parametric Survival Model Comparison

Best AIC model: exponential. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| exponential       |         -963.739 |            1 | 1929.477 | 1934.102 | True        |
| weibull           |         -963.646 |            2 | 1931.291 | 1940.542 | True        |
| loglogistic       |         -963.914 |            2 | 1931.829 | 1941.080 | True        |
| lognormal         |         -964.248 |            2 | 1932.496 | 1941.747 | True        |
| generalized_gamma |         -963.737 |            3 | 1933.474 | 1947.350 | True        |

Exponential vs Weibull LRT p-value: 0.6664

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
