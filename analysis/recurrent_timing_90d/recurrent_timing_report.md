# Repeat Cyber-Event Timing Analysis

## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.

## Data Quality Summary

- victim event attributions: 696
- unique deduplicated events: 633
- victim entities: 604
- entities with more than one event: 52
- post-event spells: 696
- observed repeat spells: 92
- right-censored spells: 604
- censor date: 2026-08-05
- elapsed-time bands: 0-90, 91-365, 366-730, 731-1460, >1460
- minimum event date: 2012-01-01
- maximum event date: 2026-07-24
- first-of-month event dates: 287
- same-day entity-event groups collapsed: 0
- covariate coverage: {'size_estimate levels': {'HUGE': 197, 'LARGE': 216, 'MEDIUM': 164, 'SMALL': 63, 'UNKNOWN': 56}, 'size_confidence populated': 696, 'employee_count populated': 620, 'turnover populated': 0, 'industry populated': 662, 'entity_kind populated': 696, 'sector_proxy levels': {'education': 47, 'finance': 61, 'government': 128, 'health': 65, 'industrial': 14, 'media': 7, 'other': 221, 'retail': 24, 'technology': 111, 'telecom_transport': 18}, 'prior records populated': 308}

## Primary Piecewise Exponential Test

The adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level.

- unadjusted LRT p-value: 0.7039
- adjusted LRT p-value: 0.1109
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=4), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown; pooled sparse levels=1)
- covariates skipped: {}

Piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |        6 |       61071.000 |                       3.588 |              1.199 |               6.478 |
| 91-365         |       27 |      157782.000 |                       6.250 |              3.147 |               9.168 |
| 366-730        |       20 |      160500.000 |                       4.551 |              2.447 |               6.730 |
| 731-1460       |       24 |      177731.000 |                       4.932 |              3.119 |               6.989 |
| >1460          |       15 |      110407.000 |                       4.962 |              2.572 |               8.271 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        1.809 |    1.255 |     2.610 | about 81% higher repeat-event rate  |
| SMALL -> LARGE  |        3.274 |    1.574 |     6.811 | about 227% higher repeat-event rate |
| SMALL -> HUGE   |        5.925 |    1.975 |    17.776 | about 492% higher repeat-event rate |
| MEDIUM -> HUGE  |        3.274 |    1.574 |     6.811 | about 227% higher repeat-event rate |
| LARGE -> HUGE   |        1.809 |    1.255 |     2.610 | about 81% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| education           |        3.575 |    1.556 |     8.210 | about 257% higher than government |
| finance             |        2.558 |    1.096 |     5.972 | about 156% higher than government |
| telecom_transport   |        2.063 |    0.699 |     6.088 | about 106% higher than government |
| health              |        0.735 |    0.220 |     2.459 | about 26% lower than government   |
| technology          |        0.640 |    0.259 |     1.583 | about 36% lower than government   |
| sparse_or_no_repeat |        0.084 |    0.018 |     0.388 | about 92% lower than government   |

Full adjusted covariate table:

| effect                             |   rate_ratio |   ci_low |   ci_high |
|:-----------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body       |        2.826 |    1.286 |     6.208 |
| Entity kind: sparse_or_no_repeat   |        0.333 |    0.042 |     2.652 |
| Organisation size                  |        1.809 |    1.255 |     2.610 |
| Prior records: 100k+               |        1.213 |    0.630 |     2.337 |
| Prior records: 10k-100k            |        1.315 |    0.730 |     2.370 |
| Prior records: 1k-10k              |        0.754 |    0.364 |     1.563 |
| Prior records: sparse_or_no_repeat |        0.164 |    0.022 |     1.229 |
| Sector: education                  |        3.575 |    1.556 |     8.210 |
| Sector: finance                    |        2.558 |    1.096 |     5.972 |
| Sector: health                     |        0.735 |    0.220 |     2.459 |
| Sector: sparse_or_no_repeat        |        0.084 |    0.018 |     0.388 |
| Sector: technology                 |        0.640 |    0.259 |     1.583 |
| Sector: telecom_transport          |        2.063 |    0.699 |     6.088 |
| Unknown size                       |        1.769 |    0.794 |     3.940 |

Targeted contrast for the proposed low-then-rising pattern:

- 91-365, 366-730 days vs 0-90 days rate ratio: 1.503
- approximate 95% CI: 0.643 to 3.516

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.
The point estimates do not form the requested U-shape.

- phase-model LRT p-value versus constant elapsed-time risk: 0.04876
- directional U-shape p-value: 0.9166

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |        6 |           61071 |                       3.588 |
| Response period  |       46 |          318282 |                       5.279 |
| Long-term period |       39 |          288138 |                       4.944 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language    |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:------------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        1.827 |    0.778 |     4.291 | about 83% higher  |         0.167 | less                      | 0.917           |
| long-term vs response  |        1.452 |    0.918 |     2.296 | about 45% higher  |         0.111 | greater                   | 0.056           |
| long-term vs immediate |        2.652 |    1.090 |     6.451 | about 165% higher |         0.032 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants test whether the wide U-shape confidence intervals are mainly caused by including organisation size or industry/sector controls.

Adjusted phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        1.827 |    0.778 |     4.291 |      3.514 | about 83% higher |           0.917 |
| All covariates                | long-term vs response |        1.452 |    0.918 |     2.296 |      1.378 | about 45% higher |           0.056 |
| Drop organisation size        | response vs immediate |        1.794 |    0.764 |     4.210 |      3.446 | about 79% higher |           0.910 |
| Drop organisation size        | long-term vs response |        1.508 |    0.957 |     2.377 |      1.421 | about 51% higher |           0.038 |
| Drop industry/sector          | response vs immediate |        1.794 |    0.764 |     4.212 |      3.448 | about 79% higher |           0.910 |
| Drop industry/sector          | long-term vs response |        1.395 |    0.879 |     2.214 |      1.334 | about 40% higher |           0.079 |
| Drop size and industry/sector | response vs immediate |        1.787 |    0.762 |     4.194 |      3.432 | about 79% higher |           0.909 |
| Drop size and industry/sector | long-term vs response |        1.475 |    0.934 |     2.330 |      1.396 | about 48% higher |           0.048 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.0488 |                         0.9166 |
| Drop organisation size        |    0.0371 |                         0.9102 |
| Drop industry/sector          |    0.0756 |                         0.9103 |
| Drop size and industry/sector |    0.0468 |                         0.9090 |

If dropping a covariate group materially narrowed the interval, the CI width would shrink in this table. In this run, the interval around the initial fall remains broad under all variants, which points more to sparse repeat-event information than to a single covariate group consuming all precision.

## Parametric Survival Model Comparison

Best AIC model: exponential. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| exponential       |         -909.833 |            1 | 1821.667 | 1826.212 | True        |
| loglogistic       |         -909.745 |            2 | 1823.490 | 1832.580 | True        |
| weibull           |         -909.826 |            2 | 1823.651 | 1832.742 | True        |
| lognormal         |         -909.878 |            2 | 1823.755 | 1832.846 | True        |
| generalized_gamma |         -909.833 |            3 | 1825.665 | 1839.302 | True        |

Exponential vs Weibull LRT p-value: 0.9009

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
