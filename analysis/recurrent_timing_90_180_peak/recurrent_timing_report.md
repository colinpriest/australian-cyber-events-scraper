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
- elapsed-time bands: 0-90, 91-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2011-04-19
- maximum event date: 2026-10-02
- first-of-month event dates: 267
- same-day entity-event groups collapsed: 3
- covariate coverage: {'size_estimate levels': {'HUGE': 206, 'LARGE': 227, 'MEDIUM': 195, 'SMALL': 63, 'UNKNOWN': 67}, 'size_confidence populated': 756, 'employee_count populated': 669, 'turnover populated': 0, 'industry populated': 713, 'entity_kind populated': 758, 'sector_proxy levels': {'education': 53, 'finance': 60, 'government': 134, 'health': 71, 'industrial': 14, 'media': 7, 'other': 248, 'retail': 28, 'technology': 120, 'telecom_transport': 23}, 'prior records populated': 337}

## Primary Piecewise Exponential Test

The adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level.

- unadjusted LRT p-value: 0.1098
- adjusted LRT p-value: 0.04075
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=3), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown; pooled sparse levels=1)
- covariates skipped: {}

Piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |        7 |       65919.000 |                       3.879 |              1.121 |               7.116 |
| 91-180         |       17 |       60446.000 |                      10.272 |              4.320 |              16.872 |
| 181-365        |       12 |      109086.000 |                       4.018 |              1.388 |               6.712 |
| 366-730        |       20 |      174226.000 |                       4.193 |              2.321 |               6.385 |
| 731-1460       |       25 |      197776.000 |                       4.617 |              3.053 |               6.423 |
| >1460          |       16 |      129412.000 |                       4.516 |              2.220 |               6.852 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        1.745 |    1.267 |     2.403 | about 74% higher repeat-event rate  |
| SMALL -> LARGE  |        3.044 |    1.605 |     5.776 | about 204% higher repeat-event rate |
| SMALL -> HUGE   |        5.312 |    2.033 |    13.881 | about 431% higher repeat-event rate |
| MEDIUM -> HUGE  |        3.044 |    1.605 |     5.776 | about 204% higher repeat-event rate |
| LARGE -> HUGE   |        1.745 |    1.267 |     2.403 | about 74% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| education           |        2.189 |    1.052 |     4.556 | about 119% higher than government |
| industrial          |        1.855 |    0.506 |     6.793 | about 85% higher than government  |
| finance             |        1.535 |    0.690 |     3.415 | about 53% higher than government  |
| telecom_transport   |        1.221 |    0.435 |     3.421 | about 22% higher than government  |
| health              |        1.001 |    0.389 |     2.574 | about 0% higher than government   |
| technology          |        0.584 |    0.258 |     1.319 | about 42% lower than government   |
| sparse_or_no_repeat |        0.062 |    0.014 |     0.275 | about 94% lower than government   |

Full adjusted covariate table:

| effect                             |   rate_ratio |   ci_low |   ci_high |
|:-----------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body       |        1.876 |    0.931 |     3.778 |
| Entity kind: sparse_or_no_repeat   |        0.000 |    0.000 |   inf     |
| Organisation size                  |        1.745 |    1.267 |     2.403 |
| Prior records: 100k+               |        0.988 |    0.524 |     1.863 |
| Prior records: 10k-100k            |        1.657 |    0.979 |     2.805 |
| Prior records: 1k-10k              |        0.735 |    0.357 |     1.517 |
| Prior records: sparse_or_no_repeat |        0.340 |    0.081 |     1.432 |
| Sector: education                  |        2.189 |    1.052 |     4.556 |
| Sector: finance                    |        1.535 |    0.690 |     3.415 |
| Sector: health                     |        1.001 |    0.389 |     2.574 |
| Sector: industrial                 |        1.855 |    0.506 |     6.793 |
| Sector: sparse_or_no_repeat        |        0.062 |    0.014 |     0.275 |
| Sector: technology                 |        0.584 |    0.258 |     1.319 |
| Sector: telecom_transport          |        1.221 |    0.435 |     3.421 |
| Unknown size                       |        1.594 |    0.748 |     3.396 |

Targeted contrast for the proposed low-then-rising pattern:

- 91-180, 181-365 days vs 0-90 days rate ratio: 1.611
- approximate 95% CI: 0.706 to 3.677

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.
The point estimates do not form the requested U-shape.

- phase-model LRT p-value versus constant elapsed-time risk: 0.308
- directional U-shape p-value: 0.891

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |        7 |           65919 |                       3.879 |
| Response period  |       28 |          169532 |                       6.032 |
| Long-term period |       60 |          501414 |                       4.371 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        1.684 |    0.735 |     3.859 | about 68% higher |         0.218 | less                      | 0.891           |
| long-term vs response  |        1.064 |    0.663 |     1.707 | about 6% higher  |         0.798 | greater                   | 0.399           |
| long-term vs immediate |        1.791 |    0.803 |     3.997 | about 79% higher |         0.155 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants test whether the wide U-shape confidence intervals are mainly caused by including organisation size or industry/sector controls.

Adjusted phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        1.684 |    0.735 |     3.859 |      3.124 | about 68% higher |           0.891 |
| All covariates                | long-term vs response |        1.064 |    0.663 |     1.707 |      1.044 | about 6% higher  |           0.399 |
| Drop organisation size        | response vs immediate |        1.678 |    0.733 |     3.846 |      3.113 | about 68% higher |           0.890 |
| Drop organisation size        | long-term vs response |        1.071 |    0.669 |     1.715 |      1.046 | about 7% higher  |           0.387 |
| Drop industry/sector          | response vs immediate |        1.699 |    0.742 |     3.893 |      3.152 | about 70% higher |           0.895 |
| Drop industry/sector          | long-term vs response |        1.007 |    0.628 |     1.614 |      0.985 | about 1% higher  |           0.489 |
| Drop size and industry/sector | response vs immediate |        1.706 |    0.745 |     3.907 |      3.163 | about 71% higher |           0.897 |
| Drop size and industry/sector | long-term vs response |        1.039 |    0.650 |     1.661 |      1.011 | about 4% higher  |           0.437 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.3080 |                         0.8910 |
| Drop organisation size        |    0.3027 |                         0.8896 |
| Drop industry/sector          |    0.3515 |                         0.8950 |
| Drop size and industry/sector |    0.3135 |                         0.8966 |

If dropping a covariate group materially narrowed the interval, the CI width would shrink in this table. In this run, the interval around the initial fall remains broad under all variants, which points more to sparse repeat-event information than to a single covariate group consuming all precision.

## Delayed 91-180 Day Peak Test

Definition used here: the hypothesised peak is 91-180 days after the prior event. A formal peak requires 91-180 days to be higher than both 0-90 days and the pooled post-180 period.
The adjusted peak model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main model.
The adjusted model supports a delayed 91-180 day peak at the 5% directional level.

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
