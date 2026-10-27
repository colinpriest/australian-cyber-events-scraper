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
- elapsed-time bands: 0-90, 91-365, 366-730, 731-1460, >1460
- minimum event date: 2011-04-19
- maximum event date: 2026-10-02
- first-of-month event dates: 266
- same-day entity-event groups collapsed: 3
- covariate coverage: {'size_estimate levels': {'HUGE': 208, 'LARGE': 228, 'MEDIUM': 193, 'SMALL': 63, 'UNKNOWN': 65}, 'size_confidence populated': 757, 'employee_count populated': 671, 'turnover populated': 0, 'industry populated': 715, 'entity_kind populated': 757, 'sector_proxy levels': {'education': 53, 'finance': 61, 'government': 134, 'health': 71, 'industrial': 14, 'media': 7, 'other': 248, 'retail': 28, 'technology': 120, 'telecom_transport': 21}, 'prior records populated': 336}

## Primary Piecewise Exponential Test

The adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level.

- unadjusted LRT p-value: 0.6752
- adjusted LRT p-value: 0.7914
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=2), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown; pooled sparse levels=1)
- covariates skipped: {}

Piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |       11 |       65715.000 |                       6.114 |              2.305 |              10.413 |
| 91-365         |       29 |      168473.000 |                       6.287 |              3.068 |               9.601 |
| 366-730        |       22 |      172549.000 |                       4.657 |              2.593 |               6.886 |
| 731-1460       |       25 |      194682.000 |                       4.690 |              2.977 |               6.644 |
| >1460          |       15 |      126827.000 |                       4.320 |              2.092 |               6.976 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        1.781 |    1.291 |     2.456 | about 78% higher repeat-event rate  |
| SMALL -> LARGE  |        3.172 |    1.668 |     6.033 | about 217% higher repeat-event rate |
| SMALL -> HUGE   |        5.649 |    2.153 |    14.820 | about 465% higher repeat-event rate |
| MEDIUM -> HUGE  |        3.172 |    1.668 |     6.033 | about 217% higher repeat-event rate |
| LARGE -> HUGE   |        1.781 |    1.291 |     2.456 | about 78% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| education           |        2.358 |    1.144 |     4.860 | about 136% higher than government |
| industrial          |        1.920 |    0.524 |     7.031 | about 92% higher than government  |
| finance             |        1.716 |    0.798 |     3.687 | about 72% higher than government  |
| telecom_transport   |        1.409 |    0.521 |     3.810 | about 41% higher than government  |
| health              |        0.812 |    0.301 |     2.194 | about 19% lower than government   |
| technology          |        0.516 |    0.227 |     1.172 | about 48% lower than government   |
| other               |        0.141 |    0.046 |     0.434 | about 86% lower than government   |
| sparse_or_no_repeat |        0.000 |    0.000 |   inf     | about 100% lower than government  |

Full adjusted covariate table:

| effect                             |   rate_ratio |   ci_low |   ci_high |
|:-----------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body       |        1.897 |    0.946 |     3.806 |
| Entity kind: sparse_or_no_repeat   |        0.000 |    0.000 |   inf     |
| Organisation size                  |        1.781 |    1.291 |     2.456 |
| Prior records: 100k+               |        1.315 |    0.720 |     2.402 |
| Prior records: 10k-100k            |        1.590 |    0.951 |     2.658 |
| Prior records: 1k-10k              |        0.748 |    0.366 |     1.529 |
| Prior records: sparse_or_no_repeat |        0.327 |    0.078 |     1.373 |
| Sector: education                  |        2.358 |    1.144 |     4.860 |
| Sector: finance                    |        1.716 |    0.798 |     3.687 |
| Sector: health                     |        0.812 |    0.301 |     2.194 |
| Sector: industrial                 |        1.920 |    0.524 |     7.031 |
| Sector: other                      |        0.141 |    0.046 |     0.434 |
| Sector: sparse_or_no_repeat        |        0.000 |    0.000 |   inf     |
| Sector: technology                 |        0.516 |    0.227 |     1.172 |
| Sector: telecom_transport          |        1.409 |    0.521 |     3.810 |
| Unknown size                       |        1.598 |    0.755 |     3.381 |

Targeted contrast for the proposed low-then-rising pattern:

- 91-365, 366-730 days vs 0-90 days rate ratio: 0.893
- approximate 95% CI: 0.466 to 1.714

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.
The point estimates do not form the requested U-shape.

- phase-model LRT p-value versus constant elapsed-time risk: 0.4848
- directional U-shape p-value: 0.5249

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       11 |           65715 |                       6.114 |
| Response period  |       49 |          341022 |                       5.248 |
| Long-term period |       40 |          321509 |                       4.544 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        1.021 |    0.529 |     1.970 | about 2% higher  |         0.950 | less                      | 0.525           |
| long-term vs response  |        1.304 |    0.832 |     2.041 | about 30% higher |         0.247 | greater                   | 0.123           |
| long-term vs immediate |        1.331 |    0.661 |     2.682 | about 33% higher |         0.424 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants test whether the wide U-shape confidence intervals are mainly caused by including organisation size or industry/sector controls.

Adjusted phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        1.021 |    0.529 |     1.970 |      1.440 | about 2% higher  |           0.525 |
| All covariates                | long-term vs response |        1.304 |    0.832 |     2.041 |      1.209 | about 30% higher |           0.123 |
| Drop organisation size        | response vs immediate |        1.011 |    0.524 |     1.949 |      1.425 | about 1% higher  |           0.513 |
| Drop organisation size        | long-term vs response |        1.357 |    0.869 |     2.117 |      1.248 | about 36% higher |           0.090 |
| Drop industry/sector          | response vs immediate |        0.994 |    0.516 |     1.915 |      1.399 | about 1% lower   |           0.492 |
| Drop industry/sector          | long-term vs response |        1.142 |    0.735 |     1.774 |      1.039 | about 14% higher |           0.278 |
| Drop size and industry/sector | response vs immediate |        0.998 |    0.518 |     1.922 |      1.404 | about 0% lower   |           0.498 |
| Drop size and industry/sector | long-term vs response |        1.219 |    0.787 |     1.888 |      1.101 | about 22% higher |           0.187 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.4848 |                         0.5249 |
| Drop organisation size        |    0.3838 |                         0.5128 |
| Drop industry/sector          |    0.8345 |                         0.4924 |
| Drop size and industry/sector |    0.6592 |                         0.4975 |

If dropping a covariate group materially narrowed the interval, the CI width would shrink in this table. In this run, the interval around the initial fall remains broad under all variants, which points more to sparse repeat-event information than to a single covariate group consuming all precision.

## Delayed 91-180 Day Peak Sensitivity

This sensitivity reruns the elapsed-time model with bands `0-90`, `91-180`, `181-365`, `366-730`, `731-1460`, and `>1460` to test whether the apparent early elevation is concentrated in days 91-180.
A formal delayed peak requires the 91-180 day period to be higher than both 0-90 days and the pooled post-180 period after adjustment.

- adjusted elapsed-time LRT p-value for the 90/180 split: 0.1474
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

Interpretation: the 91-180 day period is much higher than the first 90 days, and that contrast is statistically strong. The stricter test that it is higher than both sides is suggestive rather than definitive because the 91-180 versus post-180 comparison is weaker.

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
