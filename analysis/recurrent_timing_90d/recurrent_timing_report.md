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
- elapsed-time bands: 0-90, 91-365, 366-730, 731-1460, >1460
- minimum event date: 2003-07-04
- maximum event date: 2026-10-27
- first-of-month event dates: 256
- verified incident-date precision: {'unverified': 654, 'day': 240, 'month': 234}
- tie policy (several incidents of one organisation on one date): collapse
- same-date ties: {'ties (organisation-dates with several incidents)': 5, 'incidents in ties': 10, 'organisations with a tie': 5, 'repeats a zero-gap policy would add': 5, 'ties: month_precision_first_of_month': 0, 'ties: first_of_month_not_month_verified': 3, 'ties: other_day': 2, 'repeats added by the zero_gap policy in this run': 0}
- incidents not ordered by their dates (different stored dates, overlapping verified intervals): {'consecutive pairs with overlapping verified intervals': 0, '... of which stored dates differ by under 90 days': 0}
- covariate coverage: {'size_estimate levels': {'HUGE': 296, 'LARGE': 326, 'MEDIUM': 299, 'SMALL': 109, 'UNKNOWN': 98}, 'size_confidence populated': 1128, 'employee_count populated': 991, 'turnover populated': 0, 'industry populated': 989, 'entity_kind populated': 1128, 'sector_proxy levels': {'education': 80, 'finance': 78, 'government': 201, 'health': 101, 'industrial': 25, 'media': 8, 'other': 399, 'retail': 45, 'technology': 140, 'telecom_transport': 51}, 'prior records populated': 510}
- unresolved victim attributions: {'policy': 'include', 'victim-event attributions unresolved': 55, 'events with an unresolved attribution': 55, 'organisations with an unresolved attribution': 54, 'victim-event attributions analysed': 1128, 'repeats': {'included': {'observed repeats': 182, 'entities with a repeat': 95, 'repeats by elapsed band': {'0-90': 25, '91-365': 57, '366-730': 33, '731-1460': 43, '>1460': 24}}, 'excluded': {'observed repeats': 158, 'entities with a repeat': 85, 'repeats by elapsed band': {'0-90': 20, '91-365': 48, '366-730': 28, '731-1460': 41, '>1460': 21}}, 'repeats involving an unresolved attribution': 29}}

## Same-Date Ties And Unknown Ordering

Several distinct incidents of one organisation can carry the same stored date; nothing orders them, and a date stored as the 1st of a month may only mean that month. The primary analysis (`collapse`) makes the tied incidents one time point - their ids and count are kept in `entity_date_ties.csv`, and no order or gap between them is invented - which can understate repeats. The `zero_gap` sensitivity counts each as its own incident one day after the previous, a repeat in the first band, which can overstate them (a tie may be one incident filed twice or a month placeholder). The truth lies between; both are adjusted for organisation size like the primary model.

| tie_policy   |   observed_repeats |   repeats_from_ties |   post_event_spells |   repeats_0-90 |   repeats_91-365 |   repeats_366-730 |   repeats_731-1460 |   repeats_>1460 |   adjusted_constant_hazard_p |   adjusted_0_90_91_180_banding_p |   delayed_peak_directional_p |
|:-------------|-------------------:|--------------------:|--------------------:|---------------:|-----------------:|------------------:|-------------------:|----------------:|-----------------------------:|---------------------------------:|-----------------------------:|
| collapse     |                182 |                   0 |                1123 |             25 |               57 |                33 |                 43 |              24 |                       0.3657 |                          0.2048  |                       0.238  |
| zero_gap     |                187 |                   5 |                1128 |             30 |               57 |                33 |                 43 |              24 |                       0.1321 |                          0.07478 |                       0.4781 |


## Primary Piecewise Exponential Test

The adjusted piecewise model does not reject a constant elapsed-time hazard at the 5% level (LRT p = 0.366). It rests on 182 observed repeats.

- unadjusted LRT p-value: 0.07907
- adjusted LRT p-value: 0.3657
- adjusted model controls for calendar period, prior event number category, and available organisation/exposure covariates.
- covariates used: org_size_score (SMALL=1, MEDIUM=2, LARGE=3, HUGE=4; UNKNOWN mean-imputed), org_size_unknown, sector_proxy (reference=government; pooled sparse levels=2), entity_kind_group (reference=organisation; pooled sparse levels=1), prior_records_band (reference=unknown)
- covariates skipped: {}

Unadjusted descriptive piecewise recurrence rates are per 100 entity-years:

| elapsed_band   |   events |   exposure_days |   rate_per_100_entity_years |   bootstrap_ci_low |   bootstrap_ci_high |
|:---------------|---------:|----------------:|----------------------------:|-------------------:|--------------------:|
| 0-90           |       25 |       96396.000 |                       9.473 |              5.819 |              13.920 |
| 91-365         |       57 |      240145.000 |                       8.669 |              5.697 |              11.667 |
| 366-730        |       33 |      236019.000 |                       5.107 |              3.118 |               7.232 |
| 731-1460       |       43 |      238653.000 |                       6.581 |              4.524 |               8.939 |
| >1460          |       24 |      131016.000 |                       6.691 |              4.214 |               9.805 |

## Organisation Size And Sector Effects

Rate ratios are from the same adjusted piecewise exponential model used for the elapsed-time test.
The organisation-size effect is per one ordinal step: SMALL to MEDIUM to LARGE to HUGE; UNKNOWN size is mean-imputed with a separate indicator.

Scaled size comparisons:

| comparison      |   rate_ratio |   ci_low |   ci_high | plain_language                      |
|:----------------|-------------:|---------:|----------:|:------------------------------------|
| SMALL -> MEDIUM |        1.947 |    1.514 |     2.505 | about 95% higher repeat-event rate  |
| SMALL -> LARGE  |        3.792 |    2.291 |     6.275 | about 279% higher repeat-event rate |
| SMALL -> HUGE   |        7.383 |    3.468 |    15.720 | about 638% higher repeat-event rate |
| MEDIUM -> HUGE  |        3.792 |    2.291 |     6.275 | about 279% higher repeat-event rate |
| LARGE -> HUGE   |        1.947 |    1.514 |     2.505 | about 95% higher repeat-event rate  |

Sector effects, relative to government entities after adjusting for size and the other model controls:

| sector              |   rate_ratio |   ci_low |   ci_high | plain_language                    |
|:--------------------|-------------:|---------:|----------:|:----------------------------------|
| education           |        2.768 |    1.163 |     6.585 | about 177% higher than government |
| finance             |        2.050 |    0.910 |     4.616 | about 105% higher than government |
| telecom_transport   |        1.908 |    0.896 |     4.063 | about 91% higher than government  |
| technology          |        1.870 |    0.859 |     4.071 | about 87% higher than government  |
| industrial          |        1.595 |    0.413 |     6.162 | about 59% higher than government  |
| health              |        1.314 |    0.494 |     3.499 | about 31% higher than government  |
| sparse_or_no_repeat |        0.660 |    0.175 |     2.485 | about 34% lower than government   |
| other               |        0.517 |    0.217 |     1.233 | about 48% lower than government   |

Full adjusted covariate table:

| effect                           |   rate_ratio |   ci_low |   ci_high |
|:---------------------------------|-------------:|---------:|----------:|
| Entity kind: government_body     |        1.971 |    0.956 |     4.064 |
| Entity kind: sparse_or_no_repeat |        0.000 |    0.000 |   inf     |
| Organisation size                |        1.947 |    1.514 |     2.505 |
| Prior records: 100k+             |        0.972 |    0.642 |     1.471 |
| Prior records: 10k-100k          |        1.122 |    0.740 |     1.701 |
| Prior records: 1k-10k            |        0.919 |    0.537 |     1.573 |
| Prior records: <1k               |        0.901 |    0.483 |     1.682 |
| Sector: education                |        2.768 |    1.163 |     6.585 |
| Sector: finance                  |        2.050 |    0.910 |     4.616 |
| Sector: health                   |        1.314 |    0.494 |     3.499 |
| Sector: industrial               |        1.595 |    0.413 |     6.162 |
| Sector: other                    |        0.517 |    0.217 |     1.233 |
| Sector: sparse_or_no_repeat      |        0.660 |    0.175 |     2.485 |
| Sector: technology               |        1.870 |    0.859 |     4.071 |
| Sector: telecom_transport        |        1.908 |    0.896 |     4.063 |
| Unknown size                     |        1.289 |    0.661 |     2.513 |

Targeted contrast for the proposed low-then-rising pattern (hypothesis: the later bands are higher):

- 91-365, 366-730 days vs 0-90 days (unadjusted): rate ratio 0.73 (95% CI 0.47-1.14), about 27% lower; two-sided p = 0.162. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.47 to 1.14, which includes 1. The estimate is in the opposite direction to the hypothesis (lower, not higher). Repeat events: 90 in 91-365, 366-730 days and 25 in 0-90 days.

## U-Shape Test

Definition used here: immediate risk is the first elapsed-time band; the response period combines the next two bands; the long-term period combines the remaining later bands.
The adjusted phase model controls for the same size, sector, calendar-period, prior-event-number, entity-kind, and records-affected terms as the main elapsed-time model.

Directional U-shape test (all of: response vs immediate lower; long-term vs response higher), intersection p = 0.410. Not statistically significant at the 5% level: the data do not reject the null of no U-shape. The point estimates have the hypothesised shape. The weakest part is long-term vs response (p = 0.410).

- response vs immediate: rate ratio 0.80 (95% CI 0.51-1.25), about 20% lower; one-sided p = 0.160 (alternative: lower). Not statistically significant at the 5% level: the data do not reject the null of no decrease (rate ratio at least 1); the 95% CI runs from 0.51 to 1.25, which includes 1. Repeat events: 89 in the response period and 25 in the immediate period.
- long-term vs response: rate ratio 1.04 (95% CI 0.75-1.44), about 4% higher; one-sided p = 0.410 (alternative: higher). Not statistically significant at the 5% level: the data do not reject the null of no increase (rate ratio at most 1); the 95% CI runs from 0.75 to 1.44, which includes 1. Repeat events: 66 in the long-term period and 89 in the response period.
- long-term vs immediate: rate ratio 0.83 (95% CI 0.52-1.33), about 17% lower; two-sided p = 0.436. Not statistically significant at the 5% level: the data do not reject equal rates (rate ratio 1); the 95% CI runs from 0.52 to 1.33, which includes 1. Repeat events: 66 in the long-term period and 25 in the immediate period.

- phase-model LRT p-value versus constant elapsed-time risk: 0.6219
- directional U-shape p-value: 0.4099

Unadjusted phase rates per 100 entity-years:

| phase            |   events |   exposure_days |   rate_per_100_entity_years |
|:-----------------|---------:|----------------:|----------------------------:|
| Immediate period |       25 |           96396 |                       9.473 |
| Response period  |       89 |          476164 |                       6.827 |
| Long-term period |       66 |          369669 |                       6.521 |

Adjusted scaled phase comparisons:

| comparison             |   rate_ratio |   ci_low |   ci_high | plain_language   |   p_two_sided | directional_alternative   | p_directional   |
|:-----------------------|-------------:|---------:|----------:|:-----------------|--------------:|:--------------------------|:----------------|
| response vs immediate  |        0.798 |    0.511 |     1.245 | about 20% lower  |         0.320 | less                      | 0.160           |
| long-term vs response  |        1.039 |    0.748 |     1.442 | about 4% higher  |         0.820 | greater                   | 0.410           |
| long-term vs immediate |        0.829 |    0.517 |     1.329 | about 17% lower  |         0.436 |                           |                 |

The directional U-shape p-value is an intersection test: it only becomes small if the model supports both a fall after the immediate period and a later rise from the response period.

## U-Shape Covariate Sensitivity

These variants compare covariate specifications. Variants dropping size are unadjusted for size and are diagnostics only; they are not evidence of a size-adjusted recurrence pattern.

Phase comparisons by covariate set:

| variant_label                 | comparison            |   rate_ratio |   ci_low |   ci_high |   ci_width | plain_language   |   p_directional |
|:------------------------------|:----------------------|-------------:|---------:|----------:|-----------:|:-----------------|----------------:|
| All covariates                | response vs immediate |        0.798 |    0.511 |     1.245 |      0.734 | about 20% lower  |           0.160 |
| All covariates                | long-term vs response |        1.039 |    0.748 |     1.442 |      0.694 | about 4% higher  |           0.410 |
| Drop organisation size        | response vs immediate |        0.808 |    0.518 |     1.261 |      0.743 | about 19% lower  |           0.174 |
| Drop organisation size        | long-term vs response |        1.078 |    0.777 |     1.494 |      0.717 | about 8% higher  |           0.327 |
| Drop industry/sector          | response vs immediate |        0.795 |    0.510 |     1.241 |      0.731 | about 20% lower  |           0.156 |
| Drop industry/sector          | long-term vs response |        1.058 |    0.762 |     1.468 |      0.706 | about 6% higher  |           0.369 |
| Drop size and industry/sector | response vs immediate |        0.810 |    0.519 |     1.264 |      0.744 | about 19% lower  |           0.177 |
| Drop size and industry/sector | long-term vs response |        1.141 |    0.824 |     1.579 |      0.754 | about 14% higher |           0.213 |

Phase-model test by covariate set:

| variant_label                 |   p_value |   u_shape_intersection_p_value |
|:------------------------------|----------:|-------------------------------:|
| All covariates                |    0.6219 |                         0.4099 |
| Drop organisation size        |    0.6410 |                         0.3268 |
| Drop industry/sector          |    0.6124 |                         0.3686 |
| Drop size and industry/sector |    0.5618 |                         0.2131 |

If dropping a covariate group materially narrowed an interval, its CI width would shrink in this table. For response vs immediate, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.51-1.25). For long-term vs response, no variant narrows the 95% CI by 5% or more on the log scale ('All covariates' model: 0.75-1.44).

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
