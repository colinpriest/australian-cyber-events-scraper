# Repeat Cyber-Event Timing Analysis

## Scope

This analysis is conditional on an entity having at least one observed victim event. Single-event entities contribute right-censored post-event spells.
The primary model includes recorded organisation size. Size is a proxy for differences in visibility, attack exposure and target value, not a direct measure of those factors; residual confounding remains possible. The ordinal size term assumes equal log-rate steps between categories, UNKNOWN has a separate indicator, and current recorded size may differ from size at an incident's date. Unadjusted rates, plots, parametric models and size-dropped variants below are descriptive diagnostics and do not control for this confounding.

## Data Quality Summary

- victim event attributions: 1128
- unique deduplicated events: 910
- victim entities: 941
- entities with more than one event: 97
- post-event spells: 1123
- observed repeat spells: 182
- right-censored spells: 941
- censor date: 2026-10-30
- elapsed-time bands: 0-180, 181-365, 366-730, 731-1460, >1460
- minimum event date: 2003-07-04
- maximum event date: 2026-10-27
- first-of-month event dates: 256
- verified incident-date precision: {'unverified': 654, 'day': 240, 'month': 234}
- tie policy (several incidents of one organisation on one date): collapse
- same-date ties: {'ties (organisation-dates with several incidents)': 5, 'incidents in ties': 10, 'organisations with a tie': 5, 'repeats a zero-gap policy would add': 5, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 3, 'ties: other_day': 2, 'repeats added by the zero_gap policy in this run': 0}
- incidents not ordered by their dates (different stored dates, overlapping verified intervals): {'consecutive pairs with overlapping verified intervals': 0, '... of which stored dates differ by under 90 days': 0}
- covariate coverage: {'size_estimate levels': {'HUGE': 296, 'LARGE': 326, 'MEDIUM': 299, 'SMALL': 109, 'UNKNOWN': 98}, 'size_confidence populated': 1128, 'employee_count populated': 991, 'turnover populated': 0, 'industry populated': 989, 'entity_kind populated': 1128, 'sector_proxy levels': {'education': 80, 'finance': 78, 'government': 201, 'health': 101, 'industrial': 25, 'media': 8, 'other': 399, 'retail': 45, 'technology': 140, 'telecom_transport': 51}, 'prior records populated': 510}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 55, 'events with an unresolved attribution': 55, 'organisations with an unresolved attribution': 54, 'victim-event attributions analysed': 1128, 'repeats': {'included': {'observed repeats': 182, 'entities with a repeat': 95, 'repeats by elapsed band': {'0-180': 52, '181-365': 30, '366-730': 33, '731-1460': 43, '>1460': 24}}, 'excluded': {'observed repeats': 158, 'entities with a repeat': 85, 'repeats by elapsed band': {'0-180': 43, '181-365': 25, '366-730': 28, '731-1460': 41, '>1460': 21}}, 'repeats involving an unresolved attribution': 29}}

## Same-Date Ties And Unknown Ordering

Several distinct incidents of one organisation can carry the same stored date; nothing orders them, and a date stored as the 1st of a month may only mean that month. The primary analysis (`collapse`) makes the tied incidents one time point - their ids and count are kept in `entity_date_ties.csv`, and no order or gap between them is invented - which can understate repeats. The `zero_gap` sensitivity counts each as its own incident one day after the previous, a repeat in the first band, which can overstate them (a tie may be one incident filed twice or a month placeholder). The truth lies between; both are adjusted for organisation size like the primary model.

| tie_policy   |   observed_repeats |   repeats_from_ties |   post_event_spells |   repeats_0-180 |   repeats_181-365 |   repeats_366-730 |   repeats_731-1460 |   repeats_>1460 |   adjusted_constant_hazard_p |   adjusted_0_90_91_180_banding_p |   delayed_peak_directional_p |
|:-------------|-------------------:|--------------------:|--------------------:|----------------:|------------------:|------------------:|-------------------:|----------------:|-----------------------------:|---------------------------------:|-----------------------------:|
| collapse     |                182 |                   0 |                1123 |              52 |                30 |                33 |                 43 |              24 |                      0.1522  |                          0.2048  |                       0.238  |
| zero_gap     |                187 |                   5 |                1128 |              57 |                30 |                33 |                 43 |              24 |                      0.04021 |                          0.07478 |                       0.4781 |


## Primary Piecewise Exponential Test

The adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.152). It rests on 182 observed repeats.

- unadjusted LRT p-value: 0.02679
- adjusted LRT p-value: 0.1522
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=2), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-180          |       52 |      182976.000 |                      10.380 |              6.814 |              14.534 |
| 181-365        |       30 |      153565.000 |                       7.135 |              4.214 |              10.393 |
| 366-730        |       33 |      236019.000 |                       5.107 |              3.118 |               7.232 |
| 731-1460       |       43 |      238653.000 |                       6.581 |              4.524 |               8.939 |
| >1460          |       24 |      131016.000 |                       6.691 |              4.214 |               9.805 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        1.950 |    1.516 |     2.509 | about 95% higher repeat-event rate  |
| SMALL -> LARGE  |        3.804 |    2.298 |     6.296 | about 280% higher repeat-event rate |
| SMALL -> HUGE   |        7.418 |    3.483 |    15.798 | about 642% higher repeat-event rate |
| MEDIUM -> HUGE  |        3.804 |    2.298 |     6.296 | about 280% higher repeat-event rate |
| LARGE -> HUGE   |        1.950 |    1.516 |     2.509 | about 95% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| education           |        2.737 |    1.152 |     6.501 | about 174% higher than government |
| finance             |        2.051 |    0.911 |     4.615 | about 105% higher than government |
| telecom_transport   |        1.915 |    0.900 |     4.078 | about 92% higher than government  |
| technology          |        1.858 |    0.854 |     4.041 | about 86% higher than government  |
| industrial          |        1.587 |    0.411 |     6.124 | about 59% higher than government  |
| health              |        1.306 |    0.491 |     3.476 | about 31% higher than government  |
| sparse_or_no_repeat |        0.659 |    0.175 |     2.484 | about 34% lower than government   |
| other               |        0.515 |    0.216 |     1.227 | about 49% lower than government   |

Full adjusted covariate table:

| effect                           |   rate_ratio |   ci_low |   ci_high |
|:---------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body     |        1.967 |    0.955 |     4.050 |
| Entity kind: sparse_or_no_repeat |        0.000 |    0.000 |   inf     |
| Organisation size                |        1.950 |    1.516 |     2.509 |
| Prior records: 100k+             |        0.968 |    0.640 |     1.465 |
| Prior records: 10k-100k          |        1.125 |    0.742 |     1.706 |
| Prior records: 1k-10k            |        0.917 |    0.536 |     1.570 |
| Prior records: <1k               |        0.899 |    0.482 |     1.678 |
| Sector: education                |        2.737 |    1.152 |     6.501 |
| Sector: finance                  |        2.051 |    0.911 |     4.615 |
| Sector: health                   |        1.306 |    0.491 |     3.476 |
| Sector: industrial               |        1.587 |    0.411 |     6.124 |
| Sector: other                    |        0.515 |    0.216 |     1.227 |
| Sector: sparse_or_no_repeat      |        0.659 |    0.175 |     2.484 |
| Sector: technology               |        1.858 |    0.854 |     4.041 |
| Sector: telecom_transport        |        1.915 |    0.900 |     4.078 |
| Unknown size                     |        1.291 |    0.662 |     2.518 |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 181-365, 366-730 days vs 0-180 days (unadjusted): rate ratio 0.57 (95% CI 0.39-0.82), about 43% lower; two-sided p = 0.003. Statistically significant at the 5% level: the data reject equal rates (rate ratio 1). The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 63 in 181-365, 366-730 days and 52 in 0-180 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.166. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates have the hypothesised shape. The weakest part is long-term vs response (p = 0.166).

- response vs immediate: rate ratio 0.63 (95% CI 0.43-0.91), about 37% lower; one-sided p = 0.007 (alternative: lower). Statistically significant at the 5% level: the data reject the null of no decrease (rate ratio at least 1). Repeat events: 62 in the response period and 52 in the immediate period.
- long-term vs response: rate ratio 1.19 (95% CI 0.84-1.70), about 19% higher; one-sided p = 0.166 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.84 to 1.70, which includes 1. Repeat events: 66 in the long-term period and 62 in the response period.
- long-term vs immediate: rate ratio 0.75 (95% CI 0.51-1.09), about 25% lower; two-sided p = 0.128. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.51 to 1.09, which includes 1. Repeat events: 66 in the long-term period and 52 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.04959
- directional U-shape p-value: 0.1665

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       52 |          182976 |                      10.380 |
| Response period  |       62 |          389584 |                       5.813 |
| Long-term period |       66 |          369669 |                       6.521 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        0.626 |    0.432 |     0.907 | about 37% lower  |         0.013 | less                      | 0.007           |
| long-term vs response  |        1.191 |    0.836 |     1.699 | about 19% higher |         0.333 | greater                   | 0.166           |
| long-term vs immediate |        0.746 |    0.512 |     1.088 | about 25% lower  |         0.128 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        0.626 |    0.432 |     0.907 |      0.474 | about 37% lower  |           0.007 |
| All covariates                | long-term vs response |        1.191 |    0.836 |     1.699 |      0.863 | about 19% higher |           0.166 |
| Drop organisation size        | response vs immediate |        0.631 |    0.436 |     0.914 |      0.478 | about 37% lower  |           0.007 |
| Drop organisation size        | long-term vs response |        1.236 |    0.868 |     1.760 |      0.892 | about 24% higher |           0.120 |
| Drop industry/sector          | response vs immediate |        0.623 |    0.430 |     0.901 |      0.471 | about 38% lower  |           0.006 |
| Drop industry/sector          | long-term vs response |        1.215 |    0.852 |     1.732 |      0.880 | about 21% higher |           0.141 |
| Drop size and industry/sector | response vs immediate |        0.632 |    0.437 |     0.915 |      0.478 | about 37% lower  |           0.008 |
| Drop size and industry/sector | long-term vs response |        1.309 |    0.921 |     1.861 |      0.941 | about 31% higher |           0.067 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.0496 |                         0.1665 |
| Drop organisation size        |    0.0537 |                         0.1205 |
| Drop industry/sector          |    0.0458 |                         0.1414 |
| Drop size and industry/sector |    0.0469 |                         0.0668 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.43-0.91). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.84-1.70).

## Delayed 91-180 Day Peak Sensitivity

This sensitivity reruns the elapsed-time model with bands `0-90`, `91-180`, `181-365`, `366-730`, `731-1460`, and `>1460` to test whether the apparent early elevation is concentrated in days 91-180.
A formal delayed peak requires the 91-180 day period to be higher than both 0-90 days and the pooled post-180 period after adjustment.

- adjusted elapsed-time LRT p-value for the 90/180 split: 0.2048
- phase-model LRT p-value versus constant elapsed-time risk: 0.06144
- directional delayed-peak p-value: 0.238

Unadjusted phase rates per 100 entity-years:

| phase                    |   events |   exposure_days |   rate_per_100_entity_years |
|:-------------------------|---------:|----------------:|----------------------------:|
| Immediate 0-90 days      |       25 |           96396 |                       9.473 |
| Delayed peak 91-180 days |       27 |           86580 |                      11.390 |
| Post-180 days            |      128 |          759253 |                       6.158 |

Adjusted scaled peak comparisons:

| comparison         |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| 91-180 vs 0-90     |        1.219 |    0.707 |     2.100 | about 22% higher |         0.476 | greater                   | 0.238           |
| 91-180 vs post-180 |        1.628 |    1.071 |     2.475 | about 63% higher |         0.023 | greater                   | 0.011           |
| post-180 vs 0-90   |        0.749 |    0.485 |     1.156 | about 25% lower  |         0.191 |                           |                 |

Interpretation:

Directional delayed peak test (all of: 91-180 vs 0-90 higher; 91-180 vs post-180 higher), intersection p = 0.238. Not statistically significant at the 5% level: the data do not reject the null of no delayed peak. The point estimates have the hypothesised shape. The weakest part is 91-180 vs 0-90 (p = 0.238).

- 91-180 vs 0-90: rate ratio 1.22 (95% CI 0.71-2.10), about 22% higher; one-sided p = 0.238 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.71 to 2.10, which includes 1. Repeat events: 27 in 91-180 days and 25 in 0-90 days.
- 91-180 vs post-180: rate ratio 1.63 (95% CI 1.07-2.47), about 63% higher; one-sided p = 0.011 (alternative: higher). Statistically significant at the 5% level: the data reject the null of no increase (rate ratio at most 1). Repeat events: 27 in 91-180 days and 128 in post-180 days.
- post-180 vs 0-90: rate ratio 0.75 (95% CI 0.49-1.16), about 25% lower; two-sided p = 0.191. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.49 to 1.16, which includes 1. Repeat events: 128 in post-180 days and 25 in 0-90 days.

## Unadjusted Parametric Survival Model Comparison (Descriptive)

Best AIC model: weibull. Lower AIC/BIC means better censored likelihood fit after penalising parameters.

| model             |   log_likelihood |   parameters |      aic |      bic | converged   |
|:------------------|-----------------:|-------------:|---------:|---------:|:------------|
| weibull           |        -1735.702 |            2 | 3475.404 | 3485.451 | True        |
| generalized_gamma |        -1734.952 |            3 | 3475.903 | 3490.975 | True        |
| loglogistic       |        -1737.189 |            2 | 3478.378 | 3488.426 | True        |
| exponential       |        -1738.463 |            1 | 3478.927 | 3483.951 | True        |
| lognormal         |        -1742.650 |            2 | 3489.301 | 3499.348 | True        |

Exponential vs Weibull LRT p-value: 0.01877

## Plots

- piecewise hazard: `piecewise_hazard.png`
- survival curve: `survival_curve.png`
- parametric hazards: `parametric_hazards.png`

## Interpretation Guardrails

- A failure to reject memorylessness is not proof that attacks are memoryless; power is limited by the number of observed repeat spells.
- The analysis estimates recurrence timing among observed victim entities, not attack incidence for all Australian entities.
- Date imprecision can materially affect short-gap bands; rerun sensitivity checks excluding low-confidence or first-of-month dates if needed.
