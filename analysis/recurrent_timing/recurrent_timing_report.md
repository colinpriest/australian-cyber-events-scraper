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
- elapsed-time bands: 0-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2012-01-01
- maximum event date: 2026-07-24
- first-of-month event dates: 287
- same-day entity-event groups collapsed: 0
- covariate coverage: {'size_estimate levels': {'HUGE': 197, 'LARGE': 216, 'MEDIUM': 164, 'SMALL': 63, 'UNKNOWN': 56}, 'size_confidence populated': 696, 'employee_count populated': 620, 'turnover populated': 0, 'industry populated': 662, 'entity_kind populated': 696, 'sector_proxy levels': {'education': 47, 'finance': 61, 'government': 128, 'health': 65, 'industrial': 14, 'media': 7, 'other': 221, 'retail': 24, 'technology': 111, 'telecom_transport': 18}, 'prior records populated': 308}

## Primary Piecewise Exponential Test

The adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level.

- unadjusted LRT p-value: 0.7499
- adjusted LRT p-value: 0.1527
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=4), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown; pooled sparse levels=1)
- covariates skipped: {}

Piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-180          |       21 |      116643.000 |                       6.576 |              2.981 |              10.217 |
| 181-365        |       12 |      102210.000 |                       4.288 |              1.805 |               7.160 |
| 366-730        |       20 |      160500.000 |                       4.551 |              2.447 |               6.730 |
| 731-1460       |       24 |      177731.000 |                       4.932 |              3.119 |               6.989 |
| >1460          |       15 |      110407.000 |                       4.962 |              2.572 |               8.271 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        1.805 |    1.253 |     2.601 | about 81% higher repeat-event rate  |
| SMALL -> LARGE  |        3.259 |    1.569 |     6.767 | about 226% higher repeat-event rate |
| SMALL -> HUGE   |        5.882 |    1.966 |    17.603 | about 488% higher repeat-event rate |
| MEDIUM -> HUGE  |        3.259 |    1.569 |     6.767 | about 226% higher repeat-event rate |
| LARGE -> HUGE   |        1.805 |    1.253 |     2.601 | about 81% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| education           |        3.571 |    1.556 |     8.197 | about 257% higher than government |
| finance             |        2.540 |    1.088 |     5.930 | about 154% higher than government |
| telecom_transport   |        2.112 |    0.716 |     6.229 | about 111% higher than government |
| health              |        0.738 |    0.221 |     2.466 | about 26% lower than government   |
| technology          |        0.639 |    0.259 |     1.579 | about 36% lower than government   |
| sparse_or_no_repeat |        0.084 |    0.018 |     0.389 | about 92% lower than government   |

Full adjusted covariate table:

| effect                             |   rate_ratio |   ci_low |   ci_high |
|:-----------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body       |        2.831 |    1.289 |     6.217 |
| Entity kind: sparse_or_no_repeat   |        0.340 |    0.043 |     2.705 |
| Organisation size                  |        1.805 |    1.253 |     2.601 |
| Prior records: 100k+               |        1.212 |    0.630 |     2.331 |
| Prior records: 10k-100k            |        1.327 |    0.737 |     2.388 |
| Prior records: 1k-10k              |        0.764 |    0.369 |     1.583 |
| Prior records: sparse_or_no_repeat |        0.166 |    0.022 |     1.240 |
| Sector: education                  |        3.571 |    1.556 |     8.197 |
| Sector: finance                    |        2.540 |    1.088 |     5.930 |
| Sector: health                     |        0.738 |    0.221 |     2.466 |
| Sector: sparse_or_no_repeat        |        0.084 |    0.018 |     0.389 |
| Sector: technology                 |        0.639 |    0.259 |     1.579 |
| Sector: telecom_transport          |        2.112 |    0.716 |     6.229 |
| Unknown size                       |        1.736 |    0.782 |     3.856 |

Targeted contrast for the proposed low-then-rising pattern:

- 181-365, 366-730 days vs 0-180 days rate ratio: 0.677
- approximate 95% CI: 0.390 to 1.173

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.
The point estimates have the requested U-shape, but the full directional test is not statistically strong. The later rise is clearer than the initial fall.

- phase-model LRT p-value versus constant elapsed-time risk: 0.114
- directional U-shape p-value: 0.2328

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       21 |          116643 |                       6.576 |
| Response period  |       31 |          262710 |                       4.310 |
| Long-term period |       39 |          288138 |                       4.944 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        0.812 |    0.464 |     1.421 | about 19% lower  |         0.466 | less                      | 0.233           |
| long-term vs response  |        1.691 |    1.029 |     2.779 | about 69% higher |         0.038 | greater                   | 0.019           |
| long-term vs immediate |        1.373 |    0.773 |     2.439 | about 37% higher |         0.279 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants test whether the wide U-shape confidence intervals are mainly caused by including organisation size or industry/sector controls.

Adjusted phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        0.812 |    0.464 |     1.421 |      0.957 | about 19% lower  |           0.233 |
| All covariates                | long-term vs response |        1.691 |    1.029 |     2.779 |      1.750 | about 69% higher |           0.019 |
| Drop organisation size        | response vs immediate |        0.805 |    0.461 |     1.408 |      0.948 | about 19% lower  |           0.224 |
| Drop organisation size        | long-term vs response |        1.758 |    1.073 |     2.880 |      1.807 | about 76% higher |           0.013 |
| Drop industry/sector          | response vs immediate |        0.794 |    0.454 |     1.387 |      0.934 | about 21% lower  |           0.209 |
| Drop industry/sector          | long-term vs response |        1.629 |    0.989 |     2.683 |      1.694 | about 63% higher |           0.028 |
| Drop size and industry/sector | response vs immediate |        0.797 |    0.456 |     1.393 |      0.936 | about 20% lower  |           0.213 |
| Drop size and industry/sector | long-term vs response |        1.722 |    1.050 |     2.826 |      1.777 | about 72% higher |           0.016 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.1140 |                         0.2328 |
| Drop organisation size        |    0.0793 |                         0.2240 |
| Drop industry/sector          |    0.1550 |                         0.2086 |
| Drop size and industry/sector |    0.0960 |                         0.2129 |

If dropping a covariate group materially narrowed the interval, the CI width would shrink in this table. In this run, the interval around the initial fall remains broad under all variants, which points more to sparse repeat-event information than to a single covariate group consuming all precision.

## Delayed 91-180 Day Peak Sensitivity

This sensitivity reruns the elapsed-time model with bands `0-90`, `91-180`, `181-365`, `366-730`, `731-1460`, and `>1460` to test whether the apparent early elevation is concentrated in days 91-180.
A formal delayed peak requires the 91-180 day period to be higher than both 0-90 days and the pooled post-180 period after adjustment.

- adjusted elapsed-time LRT p-value for the 90/180 split: 0.0321
- phase-model LRT p-value versus constant elapsed-time risk: 0.06507
- directional delayed-peak p-value: 0.07311

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |        6 |           61071 |                       3.588 |
| Delayed peak 91-180 days |       15 |           55572 |                       9.859 |
| Post-180 days            |       70 |          550848 |                       4.641 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language    |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:------------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        2.909 |    1.128 |     7.500 | about 191% higher |         0.027 | greater                   | 0.014           |
| 91-180 vs post-180 |        1.527 |    0.863 |     2.703 | about 53% higher  |         0.146 | greater                   | 0.073           |
| post-180 vs 0-90   |        1.905 |    0.819 |     4.433 | about 90% higher  |         0.135 |                           |                 |

Interpretation: the 91-180 day period is much higher than the first 90 days, and that contrast is statistically strong. The stricter test that it is higher than both sides is suggestive rather than definitive because the 91-180 versus post-180 comparison is weaker.

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
