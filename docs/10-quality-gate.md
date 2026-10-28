# Continuous quality checks and the publication gate

Status: implemented 2026-10-27 (`cyber_data_collector/quality/`,
`scripts/quality_report.py`).

Every run measures the data, stores the measurements, re-checks known-correct
outcomes, and decides whether the dashboard and exports may be published. The
point is that a regression is caught by a check on every run, not by a reader
noticing a strange number afterwards.

---

## 1. Running it

```bash
python scripts/quality_report.py                 # measure, record, write instance/quality_report.{json,md,html}
python scripts/quality_report.py --no-record     # measure only (database opened read-only)
python scripts/quality_report.py --db COPY.db --out-dir some/dir --quiet
```

Run from the project root: the OAIC integrity checks read
`oaic_cyber_statistics_*.json` from the working directory, and without them
those checks are *not measured* (which blocks).

Exit code 0 means publication is allowed; 1 means it is blocked (or the
database is missing, or the run could not be recorded).

From code - the call the dashboard phase makes before publishing:

```python
from cyber_data_collector.quality import publication_allowed

allowed, reasons = publication_allowed(conn)            # records the run on conn
allowed, reasons = publication_allowed(conn, record=False, report_dir=Path("instance"))
```

`publication_allowed` raises `QualityStoreError` if it was asked to record the
run and could not: an unrecorded publication is not allowed to happen quietly.

## 2. Rules: nothing passes by default

* **Measuring only reads.** Checks run on whatever connection they are given;
  the script uses a `mode=ro` connection and records the run through a second
  one. Helpers that would migrate the schema as a side effect
  (`event_facts.stale_events`, `ASDRiskClassifier.__init__`) are not called
  when their columns are missing.
* **A check that cannot run is `not_measured`, with the reason**, never 0 and
  never `pass`. A blocking `not_measured` metric closes the gate exactly like a
  failure. A missing column (an older schema), a missing labels file, an
  integrity check that skipped itself or raised, a measuring function that
  crashed (`<group>.error`) - all are reported this way.
* **A configured rule that no check produced is `not_measured`** ("configured
  but not produced"), so deleting or renaming a check cannot silently remove it
  from the gate.
* **A metric with no rule is `info`** - kept for trends, never shown as a pass.
* Statuses: `pass`; `fail` (blocking threshold breached); `warn` (non-blocking
  threshold breached); `not_measured`; `info`.

## 3. Metrics

Thresholds live in [`cyber_data_collector/quality/thresholds.json`](../cyber_data_collector/quality/thresholds.json)
(versioned, hashed into every run; each rule carries its reason). Rules match by
`fnmatch` pattern, first match wins.

### Blocking (any `fail` or `not_measured` blocks publication)

| Metric | Threshold | Why it blocks |
|---|---|---|
| `integrity.*` - every check in `scripts/check_data_integrity.py`, keyed by its function name, plus `integrity.oaic-files-readable` | must pass | The existing CI gate. Includes the reporting-population reconciliation (`integrity.reporting-population`, `integrity.dashboard-totals-reconcile`) and `no-future-dated-incidents`. A wrong published total is worse than no page. |
| `labels.contradictions_total` | <= 2 | Labelled outcomes the database contradicts (section 4). Two are tolerated for a disputed label awaiting review; more means a cleaning step regressed or was not run. |
| `synthetic.*` | must pass | Fixed cases for the deterministic functions published numbers depend on (section 5). |
| `dates.future_active` | 0 | A future-dated incident belongs to no reporting period and disappears from every chart. |
| `dates.malformed_active` | 0 | A date that is not `YYYY-MM-DD` falls outside every half-open period query. |
| `staleness.asd_stale_share` | <= 5% | The risk matrix is published from stored classifications. The pipeline classifies before the dashboard phase, so more than 5% stale (unclassified, or classified from inputs whose `input_fingerprint` has changed) means classification was skipped or failed. Not measurable until the classifier has added `input_fingerprint` - which blocks. |

### Non-blocking (reported as `warn` when breached)

| Group | Metric | Threshold | Meaning |
|---|---|---|---|
| coverage | `coverage.records.<source>.<YYYY-MM>` | info | New raw records per source by discovery month, last 12 months |
| | `coverage.events.<source>.<YYYY-MM>`, `coverage.events.all.<YYYY-MM>` | info | Active events with a record from the source, by event month |
| | `coverage.records_flagged_months`, `coverage.events_flagged_months` | 0 | Complete months (the current one is not judged) in which a monthly source (`Perplexity`, `GoogleSearch`, `WebberInsurance`) had zero volume, or under 0.5 x its trailing 6-month median (when that median is at least 5) |
| evidence | `evidence.events_without_evidence_share` | <= 2% | Active events none of whose records has >= 300 characters of page text or validated Perplexity research - unverifiable, and invisible to the calibrated filters |
| | `evidence.status.unsupported_share` | <= 2% | The same question as the evidence status asks it (`processing/evidence_recovery.py`): another page's text or a block page is not the record's own page, so this can be slightly higher. `evidence.status.supported` / `partial` / `unsupported` / `partial_share` are recorded for the trend |
| | `evidence.status.stored_stale` | 0 | Active events whose stored `evidence_status` (what exports and dashboards show) is missing or no longer matches the evidence |
| | `evidence.recovery.needed` / `pending` / `exhausted` | info | Unsupported events whose retrieval is not yet queued (per-run cap), queued and waiting, or out of reach on every route |
| backlog | `backlog.raw_unprocessed` | info | Raw records not yet processed (new, or failed and left for retry) |
| | `backlog.raw_unprocessed_stale` | 0 | ...of which found more than 30 days ago: a retry that keeps failing |
| | `backlog.raw_processing_errors` | 0 | `processing_error` values that are not filter rejections |
| | `backlog.enrichment_retrying` / `backlog.enrichment_quarantined` | info / 0 | `EnrichedEvents.perplexity_status`; not measured on schemas without retry tracking |
| duplicates | `duplicates.same_victim_pairs_short_gap` | <= 10 | Pairs of active events sharing a victim-role entity, dated <= 90 days apart - the band the recurrence analysis estimates its early hazard from, and where reporting-lag duplicates concentrate |
| | `duplicates.identical_title_pairs` | 0 | Active event pairs with the same title |
| | `duplicates.records_in_multiple_active_events` | 0 | A record counted as evidence by two active events |
| dates | `dates.undated_active`, `dates.before_earliest_active` | 0 | Undated, or before 1990 |
| | `dates.first_of_month_share` | <= 40% | Month/year-only placeholders (35% on 2026-10-27) |
| staleness | `staleness.roles_outdated` | 0 | Entity roles decided for a different membership (`role_maintenance.staleness_report`) |
| | `staleness.facts_stale` | 0 | Facts computed for a different membership (`event_facts.stale_events`) |
| | `staleness.asd_unclassified` | info | Measurable even without fingerprints |

## 4. Label regression

`evaluation/event_reality_labels.csv`, `duplicate_labels.csv` and
`source_gate_labels.csv` were labelled against the database of their day.
Each item is followed to where it is **now**: a merged event is traced through
`EventDeduplicationMap` to the event holding its records today.

| Label | Contradiction |
|---|---|
| event `real` | the event, or the event it was merged into, is Rejected |
| event `not_incident` | the event is still Active |
| pair `same` | still two distinct Active events |
| pair `different` | both now resolve to one Active event |
| page `not_incident` | the record is evidence of an Active event (or unmapped and still Active) |
| page `reports_incident` | the record has been deactivated |

`unsure` labels, items that no longer exist and merges that cannot be followed
to exactly one event are counted in `labels.<set>.unchecked`, never as
agreement.

## 5. Synthetic cases

`regression.measure_synthetic` runs a fixed set of inputs through:

* `DeduplicationEngine.merge_decision` - identical title/date/victim merges;
  same victim same day merges; the 1st of a month is not a day; different
  victims or no victims with a generic title do not merge; a cached `different`
  ruling vetoes; different dates do not merge;
* `validate_records_affected` - a global vendor total on an Australian victim
  is rejected (University of Melbourne / Canvas), a genuine global victim keeps
  its figure (Ticketmaster), plausible, too-small and negative counts;
* `event_facts.compute_facts` - the largest valid member count wins, blanks are
  filled with the most common value, the master's value stands, an override
  wins;
* `date_provenance` - precision of day / month / year placeholders and the
  basis (publication, found, researched, stated) of a record's date.

Each case is its own metric (`synthetic.merge.first_of_month_is_not_a_day`),
so a regression names itself.

## 6. History

```sql
-- Trend of one metric
SELECT r.created_at, m.value, m.status FROM QualityMetrics m
JOIN QualityRuns r USING (run_id) WHERE m.metric = 'dates.first_of_month_share'
ORDER BY r.created_at;

-- Every blocked run and why
SELECT created_at, reasons_json FROM QualityRuns WHERE publication_allowed = 0;
```

`QualityRuns(run_id, created_at, db_path, thresholds_version, thresholds_sha256,
publication_allowed, blocking_failures, failed, warned, not_measured,
reasons_json)` and `QualityMetrics(run_id, metric, metric_group, value,
threshold, status, blocking, detail, examples_json)`. The reports show each
metric beside its value in the previous recorded run, and an **Evidence status
trend** table: this run and the last 12 recorded runs of
`evidence.status.supported` / `partial` / `unsupported` / `unsupported_share`
and `evidence.recovery.pending` / `exhausted` (`gate.metric_history`).

## 7. First run (copy of the live database, 2026-10-27)

Publication **blocked**:

* `integrity.severity-no-truncated-or-qualified`, `integrity.severity-in-known-set`,
  `integrity.event-type-no-truncated-or-qualified` - 25 rows with
  `EventSeverity.MEDIUM` / `CyberEventType.*` values (13 of them Active).
* `labels.contradictions_total` = 16: 13 events labelled `not_incident` are still
  Active (the event-reality step protects labelled-real events from rejection
  but does not force labelled non-incidents out; their Jev scores sit above the
  0.28 cut-off), and 3 pages labelled `not_incident` are still evidence of
  Active events.
* `staleness.asd_stale_share` - not measured: the live
  `ASDRiskClassifications` has no `input_fingerprint` column yet (the
  classifier adds it on its next run, and will then redo every classification).

Warnings: 12 source-months with no new records (refreshes ran every second
month), 3 event-month drops, 3.4% of active events without evidence, 12
unprocessed records older than 30 days, 7 identical-title pairs, 2 undated
events, 138 events with outdated entity roles. Not measured (older schema):
enrichment retry/quarantine counts and stale facts. Passing: future and
malformed dates, short-gap same-victim pairs (4, against <= 10), duplicate
labels (0 of 155 contradicted), all synthetic cases, every other integrity check.
