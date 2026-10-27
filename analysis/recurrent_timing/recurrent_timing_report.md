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
- elapsed-time bands: 0-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2011-04-19
- maximum event date: 2026-10-02
- first-of-month event dates: 266
- same-day entity-event groups collapsed: 3
- covariate coverage: {'size_estimate levels': {'HUGE': 208, 'LARGE': 228, 'MEDIUM': 193, 'SMALL': 63, 'UNKNOWN': 65}, 'size_confidence populated': 757, 'employee_count populated': 671, 'turnover populated': 0, 'industry populated': 715, 'entity_kind populated': 757, 'sector_proxy levels': {'education': 53, 'finance': 61, 'government': 134, 'health': 71, 'industrial': 14, 'media': 7, 'other': 248, 'retail': 28, 'technology': 120, 'telecom_transport': 21}, 'prior records populated': 336}

## Primary Piecewise Exponential Test

The adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level.

- unadjusted LRT p-value: 0.1505
- adjusted LRT p-value: 0.1985
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=2), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown; pooled sparse levels=1)
- covariates skipped: {}

Piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-180          |       28 |      125842.000 |                       8.127 |              4.446 |              11.979 |
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
| SMALL -> MEDIUM |        1.779 |    1.291 |     2.452 | about 78% higher repeat-event rate  |
| SMALL -> LARGE  |        3.165 |    1.666 |     6.014 | about 217% higher repeat-event rate |
| SMALL -> HUGE   |        5.631 |    2.150 |    14.747 | about 463% higher repeat-event rate |
| MEDIUM -> HUGE  |        3.165 |    1.666 |     6.014 | about 217% higher repeat-event rate |
| LARGE -> HUGE   |        1.779 |    1.291 |     2.452 | about 78% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| education           |        2.356 |    1.143 |     4.853 | about 136% higher than government |
| industrial          |        1.908 |    0.522 |     6.978 | about 91% higher than government  |
| finance             |        1.707 |    0.794 |     3.670 | about 71% higher than government  |
| telecom_transport   |        1.455 |    0.539 |     3.927 | about 45% higher than government  |
| health              |        0.811 |    0.300 |     2.188 | about 19% lower than government   |
| technology          |        0.515 |    0.227 |     1.168 | about 49% lower than government   |
| other               |        0.141 |    0.046 |     0.434 | about 86% lower than government   |
| sparse_or_no_repeat |        0.000 |    0.000 |   inf     | about 100% lower than government  |

Full adjusted covariate table:

| effect                             |   rate_ratio |   ci_low |   ci_high |
|:-----------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body       |        1.894 |    0.944 |     3.798 |
| Entity kind: sparse_or_no_repeat   |        0.000 |    0.000 |   inf     |
| Organisation size                  |        1.779 |    1.291 |     2.452 |
| Prior records: 100k+               |        1.305 |    0.716 |     2.379 |
| Prior records: 10k-100k            |        1.591 |    0.952 |     2.659 |
| Prior records: 1k-10k              |        0.750 |    0.367 |     1.533 |
| Prior records: sparse_or_no_repeat |        0.326 |    0.078 |     1.372 |
| Sector: education                  |        2.356 |    1.143 |     4.853 |
| Sector: finance                    |        1.707 |    0.794 |     3.670 |
| Sector: health                     |        0.811 |    0.300 |     2.188 |
| Sector: industrial                 |        1.908 |    0.522 |     6.978 |
| Sector: other                      |        0.141 |    0.046 |     0.434 |
| Sector: sparse_or_no_repeat        |        0.000 |    0.000 |   inf     |
| Sector: technology                 |        0.515 |    0.227 |     1.168 |
| Sector: telecom_transport          |        1.455 |    0.539 |     3.927 |
| Unknown size                       |        1.581 |    0.748 |     3.343 |

Targeted contrast for the proposed low-then-rising pattern:

- 181-365, 366-730 days vs 0-180 days rate ratio: 0.544
- approximate 95% CI: 0.330 to 0.897

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.
The adjusted model supports the requested U-shaped pattern at the 5% directional level.

- phase-model LRT p-value versus constant elapsed-time risk: 0.08635
- directional U-shape p-value: 0.03531

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       28 |          125842 |                       8.127 |
| Response period  |       32 |          280895 |                       4.161 |
| Long-term period |       40 |          321509 |                       4.544 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        0.613 |    0.367 |     1.022 | about 39% lower  |         0.060 | less                      | 0.030           |
| long-term vs response  |        1.571 |    0.963 |     2.562 | about 57% higher |         0.071 | greater                   | 0.035           |
| long-term vs immediate |        0.962 |    0.569 |     1.627 | about 4% lower   |         0.886 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants test whether the wide U-shape confidence intervals are mainly caused by including organisation size or industry/sector controls.

Adjusted phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        0.613 |    0.367 |     1.022 |      0.655 | about 39% lower  |           0.030 |
| All covariates                | long-term vs response |        1.571 |    0.963 |     2.562 |      1.599 | about 57% higher |           0.035 |
| Drop organisation size        | response vs immediate |        0.609 |    0.365 |     1.015 |      0.650 | about 39% lower  |           0.029 |
| Drop organisation size        | long-term vs response |        1.637 |    1.006 |     2.663 |      1.657 | about 64% higher |           0.024 |
| Drop industry/sector          | response vs immediate |        0.593 |    0.356 |     0.987 |      0.632 | about 41% lower  |           0.022 |
| Drop industry/sector          | long-term vs response |        1.387 |    0.856 |     2.248 |      1.392 | about 39% higher |           0.092 |
| Drop size and industry/sector | response vs immediate |        0.598 |    0.359 |     0.995 |      0.636 | about 40% lower  |           0.024 |
| Drop size and industry/sector | long-term vs response |        1.480 |    0.916 |     2.392 |      1.475 | about 48% higher |           0.054 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.0864 |                         0.0353 |
| Drop organisation size        |    0.0653 |                         0.0286 |
| Drop industry/sector          |    0.1158 |                         0.0919 |
| Drop size and industry/sector |    0.0972 |                         0.0545 |

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
