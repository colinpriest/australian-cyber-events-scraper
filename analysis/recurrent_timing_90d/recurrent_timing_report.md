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
- elapsed-time bands: 0-90, 91-365, 366-730, 731-1460, >1460
- minimum event date: 2011-04-19
- maximum event date: 2026-09-30
- first-of-month event dates: 229
- same-day entity-event groups collapsed: 0
- covariate coverage: {'size_estimate levels': {'HUGE': 185, 'LARGE': 207, 'MEDIUM': 186, 'SMALL': 60, 'UNKNOWN': 59}, 'size_confidence populated': 695, 'employee_count populated': 616, 'turnover populated': 0, 'industry populated': 655, 'entity_kind populated': 697, 'sector_proxy levels': {'education': 45, 'finance': 53, 'government': 122, 'health': 64, 'industrial': 13, 'media': 7, 'other': 230, 'retail': 28, 'technology': 117, 'telecom_transport': 18}, 'prior records populated': 328}

## Primary Piecewise Exponential Test

The adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level.

- unadjusted LRT p-value: 0.1466
- adjusted LRT p-value: 0.0801
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=4), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown; pooled sparse levels=1)
- covariates skipped: {}

Piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |        4 |       60995.000 |                       2.395 |              0.603 |               4.684 |
| 91-365         |       24 |      157555.000 |                       5.564 |              3.146 |               8.687 |
| 366-730        |       11 |      164987.000 |                       2.435 |              1.109 |               4.104 |
| 731-1460       |       22 |      192214.000 |                       4.180 |              2.661 |               5.952 |
| >1460          |       13 |      125575.000 |                       3.781 |              1.781 |               6.318 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        1.673 |    1.172 |     2.388 | about 67% higher repeat-event rate  |
| SMALL -> LARGE  |        2.799 |    1.374 |     5.704 | about 180% higher repeat-event rate |
| SMALL -> HUGE   |        4.683 |    1.610 |    13.621 | about 368% higher repeat-event rate |
| MEDIUM -> HUGE  |        2.799 |    1.374 |     5.704 | about 180% higher repeat-event rate |
| LARGE -> HUGE   |        1.673 |    1.172 |     2.388 | about 67% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                   |
|:--------------------|-------------:|---------:|----------:|:---------------------------------|
| finance             |        1.250 |    0.501 |     3.119 | about 25% higher than government |
| education           |        1.104 |    0.400 |     3.050 | about 10% higher than government |
| telecom_transport   |        0.909 |    0.250 |     3.304 | about 9% lower than government   |
| technology          |        0.907 |    0.413 |     1.995 | about 9% lower than government   |
| health              |        0.850 |    0.289 |     2.499 | about 15% lower than government  |
| sparse_or_no_repeat |        0.097 |    0.027 |     0.348 | about 90% lower than government  |

Full adjusted covariate table:

| effect                             |   rate_ratio |   ci_low |   ci_high |
|:-----------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body       |        1.711 |    0.792 |     3.693 |
| Entity kind: sparse_or_no_repeat   |        0.000 |    0.000 |   inf     |
| Organisation size                  |        1.673 |    1.172 |     2.388 |
| Prior records: 100k+               |        0.956 |    0.457 |     2.002 |
| Prior records: 10k-100k            |        1.746 |    0.934 |     3.265 |
| Prior records: 1k-10k              |        1.025 |    0.470 |     2.236 |
| Prior records: sparse_or_no_repeat |        0.496 |    0.115 |     2.129 |
| Sector: education                  |        1.104 |    0.400 |     3.050 |
| Sector: finance                    |        1.250 |    0.501 |     3.119 |
| Sector: health                     |        0.850 |    0.289 |     2.499 |
| Sector: sparse_or_no_repeat        |        0.097 |    0.027 |     0.348 |
| Sector: technology                 |        0.907 |    0.413 |     1.995 |
| Sector: telecom_transport          |        0.909 |    0.250 |     3.304 |
| Unknown size                       |        1.673 |    0.742 |     3.772 |

Targeted contrast for the proposed low-then-rising pattern:

- 91-365, 366-730 days vs 0-90 days rate ratio: 1.655
- approximate 95% CI: 0.588 to 4.656

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.
The point estimates do not form the requested U-shape.

- phase-model LRT p-value versus constant elapsed-time risk: 0.07863
- directional U-shape p-value: 0.8881

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |        4 |           60995 |                       2.395 |
| Response period  |       33 |          322542 |                       3.737 |
| Long-term period |       35 |          317789 |                       4.023 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language    |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:------------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        1.911 |    0.673 |     5.427 | about 91% higher  |         0.224 | less                      | 0.888           |
| long-term vs response  |        1.508 |    0.887 |     2.563 | about 51% higher  |         0.129 | greater                   | 0.064           |
| long-term vs immediate |        2.882 |    0.971 |     8.555 | about 188% higher |         0.057 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants test whether the wide U-shape confidence intervals are mainly caused by including organisation size or industry/sector controls.

Adjusted phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        1.911 |    0.673 |     5.427 |      4.754 | about 91% higher |           0.888 |
| All covariates                | long-term vs response |        1.508 |    0.887 |     2.563 |      1.675 | about 51% higher |           0.064 |
| Drop organisation size        | response vs immediate |        1.924 |    0.678 |     5.461 |      4.783 | about 92% higher |           0.891 |
| Drop organisation size        | long-term vs response |        1.616 |    0.953 |     2.739 |      1.786 | about 62% higher |           0.037 |
| Drop industry/sector          | response vs immediate |        1.913 |    0.673 |     5.436 |      4.763 | about 91% higher |           0.888 |
| Drop industry/sector          | long-term vs response |        1.467 |    0.862 |     2.496 |      1.634 | about 47% higher |           0.079 |
| Drop size and industry/sector | response vs immediate |        1.949 |    0.686 |     5.533 |      4.847 | about 95% higher |           0.895 |
| Drop size and industry/sector | long-term vs response |        1.643 |    0.970 |     2.782 |      1.811 | about 64% higher |           0.032 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.0786 |                         0.8881 |
| Drop organisation size        |    0.0445 |                         0.8907 |
| Drop industry/sector          |    0.0959 |                         0.8883 |
| Drop size and industry/sector |    0.0367 |                         0.8949 |

If dropping a covariate group materially narrowed the interval, the CI width would shrink in this table. In this run, the interval around the initial fall remains broad under all variants, which points more to sparse repeat-event information than to a single covariate group consuming all precision.

## Delayed 91-180 Day Peak Sensitivity

This sensitivity reruns the elapsed-time model with bands `0-90`, `91-180`, `181-365`, `366-730`, `731-1460`, and `>1460` to test whether the apparent early elevation is concentrated in days 91-180.
A formal delayed peak requires the 91-180 day period to be higher than both 0-90 days and the pooled post-180 period after adjustment.

- adjusted elapsed-time LRT p-value for the 90/180 split: 0.007186
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

Interpretation: the 91-180 day period is much higher than the first 90 days, and that contrast is statistically strong. The stricter test that it is higher than both sides is suggestive rather than definitive because the 91-180 versus post-180 comparison is weaker.

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
