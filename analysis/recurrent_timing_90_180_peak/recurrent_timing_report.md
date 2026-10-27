# Repeat Cyber-Event Timing Analysis

## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.

## Data Quality Summary

- victim event attributions: 712
- unique deduplicated events: 629
- victim entities: 623
- entities with more than one event: 54
- post-event spells: 709
- observed repeat spells: 86
- right-censored spells: 623
- censor date: 2026-10-02
- elapsed-time bands: 0-90, 91-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2011-04-19
- maximum event date: 2026-09-30
- first-of-month event dates: 238
- same-day entity-event groups collapsed: 3
- covariate coverage: {'size_estimate levels': {'HUGE': 190, 'LARGE': 213, 'MEDIUM': 189, 'SMALL': 61, 'UNKNOWN': 59}, 'size_confidence populated': 710, 'employee_count populated': 631, 'turnover populated': 0, 'industry populated': 670, 'entity_kind populated': 712, 'sector_proxy levels': {'education': 50, 'finance': 53, 'government': 125, 'health': 66, 'industrial': 14, 'media': 7, 'other': 230, 'retail': 28, 'technology': 118, 'telecom_transport': 21}, 'prior records populated': 335}

## Primary Piecewise Exponential Test

The adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level.

- unadjusted LRT p-value: 0.05472
- adjusted LRT p-value: 0.02905
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=3), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown; pooled sparse levels=1)
- covariates skipped: {}

Piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |        7 |       61942.000 |                       4.128 |              1.773 |               7.325 |
| 91-180         |       16 |       56746.000 |                      10.299 |              5.207 |              16.241 |
| 181-365        |       10 |      102927.000 |                       3.549 |              1.081 |               7.091 |
| 366-730        |       15 |      166523.000 |                       3.290 |              1.565 |               5.231 |
| 731-1460       |       23 |      193457.000 |                       4.342 |              2.762 |               6.047 |
| >1460          |       15 |      129447.000 |                       4.232 |              2.296 |               6.847 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        1.686 |    1.210 |     2.349 | about 69% higher repeat-event rate  |
| SMALL -> LARGE  |        2.841 |    1.463 |     5.518 | about 184% higher repeat-event rate |
| SMALL -> HUGE   |        4.790 |    1.770 |    12.961 | about 379% higher repeat-event rate |
| MEDIUM -> HUGE  |        2.841 |    1.463 |     5.518 | about 184% higher repeat-event rate |
| LARGE -> HUGE   |        1.686 |    1.210 |     2.349 | about 69% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| education           |        2.513 |    1.169 |     5.405 | about 151% higher than government |
| industrial          |        1.986 |    0.534 |     7.388 | about 99% higher than government  |
| telecom_transport   |        1.560 |    0.538 |     4.526 | about 56% higher than government  |
| finance             |        1.452 |    0.591 |     3.568 | about 45% higher than government  |
| health              |        1.143 |    0.431 |     3.032 | about 14% higher than government  |
| technology          |        0.551 |    0.226 |     1.343 | about 45% lower than government   |
| sparse_or_no_repeat |        0.034 |    0.004 |     0.259 | about 97% lower than government   |

Full adjusted covariate table:

| effect                             |   rate_ratio |   ci_low |   ci_high |
|:-----------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body       |        1.908 |    0.898 |     4.050 |
| Entity kind: sparse_or_no_repeat   |        0.000 |    0.000 |   inf     |
| Organisation size                  |        1.686 |    1.210 |     2.349 |
| Prior records: 100k+               |        0.994 |    0.505 |     1.958 |
| Prior records: 10k-100k            |        1.586 |    0.896 |     2.805 |
| Prior records: 1k-10k              |        0.812 |    0.386 |     1.707 |
| Prior records: sparse_or_no_repeat |        0.346 |    0.081 |     1.473 |
| Sector: education                  |        2.513 |    1.169 |     5.405 |
| Sector: finance                    |        1.452 |    0.591 |     3.568 |
| Sector: health                     |        1.143 |    0.431 |     3.032 |
| Sector: industrial                 |        1.986 |    0.534 |     7.388 |
| Sector: sparse_or_no_repeat        |        0.034 |    0.004 |     0.259 |
| Sector: technology                 |        0.551 |    0.226 |     1.343 |
| Sector: telecom_transport          |        1.560 |    0.538 |     4.526 |
| Unknown size                       |        1.744 |    0.790 |     3.846 |

Targeted contrast for the proposed low-then-rising pattern:

- 91-180, 181-365 days vs 0-90 days rate ratio: 1.441
- approximate 95% CI: 0.625 to 3.320

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.
The point estimates do not form the requested U-shape.

- phase-model LRT p-value versus constant elapsed-time risk: 0.6738
- directional U-shape p-value: 0.805

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |        7 |           61942 |                       4.128 |
| Response period  |       25 |          159673 |                       5.719 |
| Long-term period |       52 |          489427 |                       3.881 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        1.445 |    0.624 |     3.344 | about 44% higher |         0.390 | less                      | 0.805           |
| long-term vs response  |        0.923 |    0.560 |     1.522 | about 8% lower   |         0.753 | greater                   | 0.624           |
| long-term vs immediate |        1.333 |    0.595 |     2.990 | about 33% higher |         0.485 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants test whether the wide U-shape confidence intervals are mainly caused by including organisation size or industry/sector controls.

Adjusted phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        1.445 |    0.624 |     3.344 |      2.720 | about 44% higher |           0.805 |
| All covariates                | long-term vs response |        0.923 |    0.560 |     1.522 |      0.962 | about 8% lower   |           0.624 |
| Drop organisation size        | response vs immediate |        1.442 |    0.623 |     3.335 |      2.712 | about 44% higher |           0.804 |
| Drop organisation size        | long-term vs response |        0.937 |    0.570 |     1.542 |      0.972 | about 6% lower   |           0.600 |
| Drop industry/sector          | response vs immediate |        1.461 |    0.631 |     3.381 |      2.750 | about 46% higher |           0.812 |
| Drop industry/sector          | long-term vs response |        0.882 |    0.535 |     1.454 |      0.919 | about 12% lower  |           0.689 |
| Drop size and industry/sector | response vs immediate |        1.472 |    0.636 |     3.404 |      2.768 | about 47% higher |           0.817 |
| Drop size and industry/sector | long-term vs response |        0.916 |    0.557 |     1.505 |      0.948 | about 8% lower   |           0.635 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.6738 |                         0.8050 |
| Drop organisation size        |    0.6722 |                         0.8036 |
| Drop industry/sector          |    0.6514 |                         0.8122 |
| Drop size and industry/sector |    0.6459 |                         0.8168 |

If dropping a covariate group materially narrowed the interval, the CI width would shrink in this table. In this run, the interval around the initial fall remains broad under all variants, which points more to sparse repeat-event information than to a single covariate group consuming all precision.

## Delayed 91-180 Day Peak Test

Definition used here: the hypothesised peak is 91-180 days after the prior event. A formal peak requires 91-180 days to be higher than both 0-90 days and the pooled post-180 period.
The adjusted peak model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main model.
The adjusted model supports a delayed 91-180 day peak at the 5% directional level.

- phase-model LRT p-value versus constant elapsed-time risk: 0.03532
- directional delayed-peak p-value: 0.02014

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |        7 |           61942 |                       4.128 |
| Delayed peak 91-180 days |       16 |           56746 |                      10.299 |
| Post-180 days            |       61 |          592354 |                       3.761 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language    |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:------------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        2.534 |    1.042 |     6.161 | about 153% higher |         0.040 | greater                   | 0.020           |
| 91-180 vs post-180 |        2.132 |    1.212 |     3.749 | about 113% higher |         0.009 | greater                   | 0.004           |
| post-180 vs 0-90   |        1.189 |    0.538 |     2.629 | about 19% higher  |         0.670 |                           |                 |

## Parametric Survival Model Comparison

Best AIC model: exponential. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| exponential       |         -861.732 |            1 | 1725.464 | 1730.028 | True        |
| weibull           |         -861.492 |            2 | 1726.984 | 1736.112 | True        |
| loglogistic       |         -861.821 |            2 | 1727.641 | 1736.769 | True        |
| lognormal         |         -862.383 |            2 | 1728.766 | 1737.893 | True        |
| generalized_gamma |         -861.493 |            3 | 1728.987 | 1742.678 | True        |

Exponential vs Weibull LRT p-value: 0.4886

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
