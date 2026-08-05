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
- elapsed-time bands: 0-90, 91-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2012-01-01
- maximum event date: 2026-07-24
- first-of-month event dates: 287
- same-day entity-event groups collapsed: 0
- covariate coverage: {'size_estimate levels': {'HUGE': 197, 'LARGE': 216, 'MEDIUM': 164, 'SMALL': 63, 'UNKNOWN': 56}, 'size_confidence populated': 696, 'employee_count populated': 620, 'turnover populated': 0, 'industry populated': 662, 'entity_kind populated': 696, 'sector_proxy levels': {'education': 47, 'finance': 61, 'government': 128, 'health': 65, 'industrial': 14, 'media': 7, 'other': 221, 'retail': 24, 'technology': 111, 'telecom_transport': 18}, 'prior records populated': 308}

## Primary Piecewise Exponential Test

The adjusted piecewise model rejects a constant elapsed-time hazard at the 5% level.

- unadjusted LRT p-value: 0.2356
- adjusted LRT p-value: 0.0321
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=4), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown; pooled sparse levels=1)
- covariates skipped: {}

Piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |        6 |       61071.000 |                       3.588 |              1.199 |               6.478 |
| 91-180         |       15 |       55572.000 |                       9.859 |              3.666 |              15.780 |
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
| SMALL -> MEDIUM |        1.813 |    1.257 |     2.615 | about 81% higher repeat-event rate  |
| SMALL -> LARGE  |        3.288 |    1.581 |     6.839 | about 229% higher repeat-event rate |
| SMALL -> HUGE   |        5.962 |    1.987 |    17.883 | about 496% higher repeat-event rate |
| MEDIUM -> HUGE  |        3.288 |    1.581 |     6.839 | about 229% higher repeat-event rate |
| LARGE -> HUGE   |        1.813 |    1.257 |     2.615 | about 81% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| education           |        3.560 |    1.549 |     8.180 | about 256% higher than government |
| finance             |        2.559 |    1.096 |     5.974 | about 156% higher than government |
| telecom_transport   |        2.100 |    0.713 |     6.187 | about 110% higher than government |
| health              |        0.736 |    0.220 |     2.463 | about 26% lower than government   |
| technology          |        0.640 |    0.259 |     1.580 | about 36% lower than government   |
| sparse_or_no_repeat |        0.084 |    0.018 |     0.388 | about 92% lower than government   |

Full adjusted covariate table:

| effect                             |   rate_ratio |   ci_low |   ci_high |
|:-----------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body       |        2.826 |    1.287 |     6.207 |
| Entity kind: sparse_or_no_repeat   |        0.337 |    0.042 |     2.687 |
| Organisation size                  |        1.813 |    1.257 |     2.615 |
| Prior records: 100k+               |        1.207 |    0.628 |     2.322 |
| Prior records: 10k-100k            |        1.316 |    0.731 |     2.371 |
| Prior records: 1k-10k              |        0.758 |    0.366 |     1.571 |
| Prior records: sparse_or_no_repeat |        0.165 |    0.022 |     1.232 |
| Sector: education                  |        3.560 |    1.549 |     8.180 |
| Sector: finance                    |        2.559 |    1.096 |     5.974 |
| Sector: health                     |        0.736 |    0.220 |     2.463 |
| Sector: sparse_or_no_repeat        |        0.084 |    0.018 |     0.388 |
| Sector: technology                 |        0.640 |    0.259 |     1.580 |
| Sector: telecom_transport          |        2.100 |    0.713 |     6.187 |
| Unknown size                       |        1.753 |    0.788 |     3.904 |

Targeted contrast for the proposed low-then-rising pattern:

- 91-180, 181-365 days vs 0-90 days rate ratio: 1.742
- approximate 95% CI: 0.719 to 4.219

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.
The point estimates do not form the requested U-shape.

- phase-model LRT p-value versus constant elapsed-time risk: 0.1314
- directional U-shape p-value: 0.9131

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |        6 |           61071 |                       3.588 |
| Response period  |       26 |          157782 |                       6.019 |
| Long-term period |       59 |          448638 |                       4.803 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language    |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:------------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        1.853 |    0.762 |     4.505 | about 85% higher  |         0.174 | less                      | 0.913           |
| long-term vs response  |        1.197 |    0.736 |     1.946 | about 20% higher  |         0.470 | greater                   | 0.235           |
| long-term vs immediate |        2.217 |    0.940 |     5.229 | about 122% higher |         0.069 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants test whether the wide U-shape confidence intervals are mainly caused by including organisation size or industry/sector controls.

Adjusted phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        1.853 |    0.762 |     4.505 |      3.743 | about 85% higher |           0.913 |
| All covariates                | long-term vs response |        1.197 |    0.736 |     1.946 |      1.210 | about 20% higher |           0.235 |
| Drop organisation size        | response vs immediate |        1.827 |    0.751 |     4.442 |      3.690 | about 83% higher |           0.908 |
| Drop organisation size        | long-term vs response |        1.214 |    0.749 |     1.968 |      1.219 | about 21% higher |           0.216 |
| Drop industry/sector          | response vs immediate |        1.854 |    0.762 |     4.507 |      3.745 | about 85% higher |           0.913 |
| Drop industry/sector          | long-term vs response |        1.130 |    0.695 |     1.837 |      1.142 | about 13% higher |           0.311 |
| Drop size and industry/sector | response vs immediate |        1.845 |    0.759 |     4.486 |      3.726 | about 85% higher |           0.912 |
| Drop size and industry/sector | long-term vs response |        1.168 |    0.720 |     1.894 |      1.173 | about 17% higher |           0.264 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.1314 |                         0.9131 |
| Drop organisation size        |    0.1273 |                         0.9082 |
| Drop industry/sector          |    0.1806 |                         0.9133 |
| Drop size and industry/sector |    0.1519 |                         0.9118 |

If dropping a covariate group materially narrowed the interval, the CI width would shrink in this table. In this run, the interval around the initial fall remains broad under all variants, which points more to sparse repeat-event information than to a single covariate group consuming all precision.

## Delayed 91-180 Day Peak Test

Definition used here: the hypothesised peak is 91-180 days after the prior event. A formal peak requires 91-180 days to be higher than both 0-90 days and the pooled post-180 period.
The adjusted peak model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main model.
The point estimates have a delayed 91-180 day peak, but the full directional test is not statistically strong.

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
