# Repeat Cyber-Event Timing Analysis

## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.
The primary model includes recorded organisation size. Size is a proxy for differences in visibility, attack exposure and target value, not a direct measure of those factors; residual confounding remains possible. The ordinal size term assumes equal log-rate steps between categories, UNKNOWN has a separate indicator, and current recorded size may differ from size at an incident's date. Unadjusted rates, plots, parametric models and size-dropped variants below are descriptive diagnostics and do not control for this confounding.

## Data Quality Summary

- victim event attributions: 677
- unique deduplicated events: 584
- victim entities: 615
- entities with more than one event: 45
- post-event spells: 672
- observed repeat spells: 57
- right-censored spells: 615
- censor date: 2026-10-28
- elapsed-time bands: 0-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2011-04-19
- maximum event date: 2026-09-30
- first-of-month event dates: 193
- same-day entity-event groups collapsed: 5
- covariate coverage: {'size_estimate levels': {'HUGE': 166, 'LARGE': 195, 'MEDIUM': 171, 'SMALL': 51, 'UNKNOWN': 94}, 'size_confidence populated': 633, 'employee_count populated': 566, 'turnover populated': 0, 'industry populated': 621, 'entity_kind populated': 677, 'sector_proxy levels': {'education': 44, 'finance': 53, 'government': 111, 'health': 58, 'industrial': 13, 'media': 4, 'other': 236, 'retail': 24, 'technology': 116, 'telecom_transport': 18}, 'prior records populated': 328}

## Primary Piecewise Exponential Test

The adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level.

- unadjusted LRT p-value: 0.3306
- adjusted LRT p-value: 0.1195
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=technology; pooled sparse levels=4), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown; pooled sparse levels=1)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-180          |        9 |      115059.000 |                       2.857 |              0.968 |               4.869 |
| 181-365        |        8 |       99112.000 |                       2.948 |              0.748 |               6.139 |
| 366-730        |        8 |      162084.000 |                       1.803 |              0.682 |               3.248 |
| 731-1460       |       17 |      190480.000 |                       3.260 |              1.755 |               5.043 |
| >1460          |       15 |      123954.000 |                       4.420 |              2.529 |               7.012 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        1.651 |    1.115 |     2.444 | about 65% higher repeat-event rate  |
| SMALL -> LARGE  |        2.726 |    1.244 |     5.972 | about 173% higher repeat-event rate |
| SMALL -> HUGE   |        4.500 |    1.388 |    14.595 | about 350% higher repeat-event rate |
| MEDIUM -> HUGE  |        2.726 |    1.244 |     5.972 | about 173% higher repeat-event rate |
| LARGE -> HUGE   |        1.651 |    1.115 |     2.444 | about 65% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                   |
|:--------------------|-------------:|---------:|----------:|:---------------------------------|
| telecom_transport   |        1.439 |    0.406 |     5.098 | about 44% higher than government |
| finance             |        1.018 |    0.416 |     2.488 | about 2% higher than government  |
| education           |        0.906 |    0.278 |     2.946 | about 9% lower than government   |
| sparse_or_no_repeat |        0.564 |    0.184 |     1.729 | about 44% lower than government  |
| government          |        0.259 |    0.070 |     0.964 | about 74% lower than government  |
| other               |        0.245 |    0.081 |     0.743 | about 76% lower than government  |

Full adjusted covariate table:

| effect                             |   rate_ratio |   ci_low |   ci_high |
|:-----------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body       |        4.569 |    1.420 |    14.702 |
| Entity kind: sparse_or_no_repeat   |        0.000 |    0.000 |   inf     |
| Organisation size                  |        1.651 |    1.115 |     2.444 |
| Prior records: 100k+               |        1.474 |    0.724 |     3.001 |
| Prior records: 10k-100k            |        2.466 |    1.180 |     5.151 |
| Prior records: 1k-10k              |        0.961 |    0.340 |     2.717 |
| Prior records: sparse_or_no_repeat |        0.397 |    0.052 |     3.033 |
| Sector: education                  |        0.906 |    0.278 |     2.946 |
| Sector: finance                    |        1.018 |    0.416 |     2.488 |
| Sector: government                 |        0.259 |    0.070 |     0.964 |
| Sector: other                      |        0.245 |    0.081 |     0.743 |
| Sector: sparse_or_no_repeat        |        0.564 |    0.184 |     1.729 |
| Sector: telecom_transport          |        1.439 |    0.406 |     5.098 |
| Unknown size                       |        0.772 |    0.266 |     2.239 |

Targeted contrast for the proposed low-then-rising pattern:

- 181-365, 366-730 days vs 0-180 days rate ratio: 0.783
- approximate 95% CI: 0.346 to 1.772

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.
The point estimates have the requested U-shape, but the full directional test is not statistically strong. The later rise is clearer than the initial fall.

- phase-model LRT p-value versus constant elapsed-time risk: 0.05454
- directional U-shape p-value: 0.3689

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |        9 |          115059 |                       2.857 |
| Response period  |       15 |          261196 |                       2.098 |
| Long-term period |       32 |          314434 |                       3.717 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language    |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:------------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        0.867 |    0.375 |     2.002 | about 13% lower   |         0.738 | less                      | 0.369           |
| long-term vs response  |        2.151 |    1.107 |     4.182 | about 115% higher |         0.024 | greater                   | 0.012           |
| long-term vs immediate |        1.865 |    0.816 |     4.260 | about 86% higher  |         0.139 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language    |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:------------------|----------------:|
| All covariates                | response vs immediate |        0.867 |    0.375 |     2.002 |      1.627 | about 13% lower   |           0.369 |
| All covariates                | long-term vs response |        2.151 |    1.107 |     4.182 |      3.076 | about 115% higher |           0.012 |
| Drop organisation size        | response vs immediate |        0.869 |    0.377 |     2.004 |      1.626 | about 13% lower   |           0.371 |
| Drop organisation size        | long-term vs response |        2.281 |    1.172 |     4.439 |      3.267 | about 128% higher |           0.008 |
| Drop industry/sector          | response vs immediate |        0.854 |    0.370 |     1.972 |      1.602 | about 15% lower   |           0.356 |
| Drop industry/sector          | long-term vs response |        2.280 |    1.173 |     4.431 |      3.258 | about 128% higher |           0.008 |
| Drop size and industry/sector | response vs immediate |        0.859 |    0.373 |     1.978 |      1.605 | about 14% lower   |           0.361 |
| Drop size and industry/sector | long-term vs response |        2.468 |    1.273 |     4.784 |      3.510 | about 147% higher |           0.004 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.0545 |                         0.3689 |
| Drop organisation size        |    0.0333 |                         0.3713 |
| Drop industry/sector          |    0.0343 |                         0.3560 |
| Drop size and industry/sector |    0.0153 |                         0.3608 |

If dropping a covariate group materially narrowed the interval, the CI width would shrink in this table. In this run, the interval around the initial fall remains broad under all variants, which points more to sparse repeat-event information than to a single covariate group consuming all precision.

## Delayed 91-180 Day Peak Sensitivity

This sensitivity reruns the elapsed-time model with bands `0-90`, `91-180`, `181-365`, `366-730`, `731-1460`, and `>1460` to test whether the apparent early elevation is concentrated in days 91-180.
A formal delayed peak requires the 91-180 day period to be higher than both 0-90 days and the pooled post-180 period after adjustment.

- adjusted elapsed-time LRT p-value for the 90/180 split: 0.1216
- phase-model LRT p-value versus constant elapsed-time risk: 0.4353
- directional delayed-peak p-value: 0.3937

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |        3 |           59588 |                       1.839 |
| Delayed peak 91-180 days |        6 |           55471 |                       3.951 |
| Post-180 days            |       47 |          575630 |                       2.982 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language    |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:------------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        2.206 |    0.551 |     8.831 | about 121% higher |         0.264 | greater                   | 0.132           |
| 91-180 vs post-180 |        1.127 |    0.471 |     2.696 | about 13% higher  |         0.787 | greater                   | 0.394           |
| post-180 vs 0-90   |        1.956 |    0.596 |     6.419 | about 96% higher  |         0.268 |                           |                 |

Interpretation: the 91-180 day period is much higher than the first 90 days, and that contrast is statistically strong. The stricter test that it is higher than both sides is suggestive rather than definitive because the 91-180 versus post-180 comparison is weaker.

## Unadjusted Parametric Survival Model Comparison (Descriptive)

Best AIC model: exponential. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| exponential       |         -592.936 |            1 | 1187.873 | 1192.383 | True        |
| weibull           |         -592.409 |            2 | 1188.818 | 1197.838 | True        |
| loglogistic       |         -592.784 |            2 | 1189.568 | 1198.589 | True        |
| generalized_gamma |         -592.158 |            3 | 1190.315 | 1203.846 | True        |
| lognormal         |         -596.733 |            2 | 1197.466 | 1206.486 | True        |

Exponential vs Weibull LRT p-value: 0.3043

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
