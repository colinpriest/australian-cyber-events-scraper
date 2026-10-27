# Data-quality to-do list

Open work found while building the calibrated filters (2026-10-27). Each item
names the evidence that showed the problem. Done items move to the README or
`docs/` with the commit that closed them.

## In progress (2026-10-27, night)

- **Events that affect more than one organisation.** Supplier breaches
  (ProctorU: ten universities; Canvas/Instructure; Frontier payroll) are one
  incident with several victims; being built (find, judge and attach each
  victim; count once per victim in the recurrence analysis).

## Done tonight (2026-10-27; timelines and victims: see `docs/10-victim-attribution-and-timelines.md`)

- Timelines scoped to Australia for multinationals and refusals; aliases
  limited to spellings of the same name; supplier-breach instruction
  (`timeline-v2`). Failed refreshes are recorded and flagged to judgements.
- Victim attribution calibrated; `check-victim-attribution --apply`
  demotes / re-attributes from the evidence, else flags.
- Timeline reconciliation resolves each mismatch class through a calibrated
  step (victim attribution, event reality, calibrated re-dating) or reports it.

- Citation support for enrichment facts; affected counts carry unit, basis
  and scope; `PERPLEXITY_MODEL` everywhere (docs/05).
- Stray records: `check-coherence` on Jev, calibrated, in the refresh
  (docs/09 section 8j).
- Exports, dashboards, OAIC and ASD-classifier fallbacks (reviewer findings
  18-21, 29, 33-37); validated OAIC refresh in the pipeline.

## To do

- **Apply the reliability recompute after labelling a sample.**
  `perplexity_backfill_events.py --recompute-reliability` is merged and
  tested but not applied to the live data: on a copy it withholds
  unsupported facts on 3,707 records and would revert 2,486 event dates to
  the raw (often publication) date, leaving 162 more records undated. A spot
  check found most withheld entities wrong, but the date reversion feeds
  reporting-lag artefacts back into the recurrence analysis. Label a sample
  of withheld dates and counts first.
- **Timeline recall of supplier breaches is still uneven.** After v2, UNSW's
  timeline lists the 2020 ProctorU breach but UWA's does not, and Perplexity's
  answers vary run to run. A second, supplier-only query per organisation
  (or per known supplier) would be the next step.
- **Victim re-attribution flags what it cannot replace.** Wrong sole victims
  whose real victim is not named by the evidence are flagged
  (`VictimAttributionActions.action = 'flagged'`), not changed.

- **Strays at the same organisation.** `check-coherence` is calibrated
  (docs/09 section 8j) but splits only *suspects* - records naming another
  organisation. Labels show the gate keeps real strays: 7 of 28 non-suspects
  dated over 180 days from their event are another incident at the same
  organisation (Optus's 2019 White Pages record in its 2022 breach, Okta's
  2023 support breach in its 2022 Sitel event, AFP's HWL Ebsworth exposure in
  its 2022 leak) - about 28 records when weighted - while 21 of the 28 are
  misdated copies the gate rightly protects. Needed: score far-dated
  same-organisation members too, label them, and calibrate a separate cut-off
  before letting them leave. The "Instacart record in WSU" a labeller
  mentioned is not in the database (no record names Instacart).
- **Check coherence question v2 on fresh labels.** v2 was written after v1
  failed on the same 92 labelled suspects (it restates the labelling
  convention about mismatched stored pages); the next labelling round should
  measure it on suspects it has not seen.
- **Mismatched stored pages.** Labellers found many records whose stored page
  is a different article from the record's own title and research (Scouts
  Victoria with a UTAS page, Suncorp Bank with an HTW page, Service NSW with
  an IN SPORT page). Find out where the scraper attaches the wrong page.
- **Replacement records need the refresh to become events.** The source gate
  queues proper reporting for real incidents whose only evidence was a junk
  page; until scraping, enrichment and deduplication run, those incidents are
  absent from the data. (Failed replacement searches are now retried.)
- **Older "latest" lookups.** The ledger and dedup dashboard order histories by
  `created_at` alone; on Windows, timestamps can coincide within milliseconds.
  Fine for display, but check any that pick a single latest row.
- **Page gate for non-Google sources.** The Jev page gate that replaced the
  Random Forest drops only Google pages: on labelled records its lower-bound
  precision for Perplexity is 0.36 (56 would-drop of 104), Webber 0.5, OAIC
  0.2 (`evaluation/page_gate_source_evaluation.json`). Those sources are
  report-only until a source-specific question or labels make it safe.
- **Perplexity used as page content.** Webber's detail-page fallback and the
  scraper fallback now run only when explicitly enabled and are marked as
  alternative-source text; `content_acquisition.py` still returns None at debug
  level when the scraper cannot be imported, and Webber's list page failing
  returns no events with exit 0.
- **Rediscovered records are re-classified each run** (rolling three-month
  window). Deliberate, so a record first seen as a snippet is re-judged once
  scraped, but it costs LLM calls; cache by evidence fingerprint.
- **Personal-information types are not calibrated.** The breach-source step
  stores Jev's per-type probabilities and reads them at 0.5; label a sample and
  calibrate before the dashboard's exposure rates are relied on.
- **Lockfile resolved in an empty environment.** `requirements.lock` was
  frozen from the working environment, and the reviewer found contradictory
  pins (`httpcore` / `h11`, `typing-extensions`, `idna`); regenerate with a
  resolver, install into an empty environment, and keep `pip check` in CI.

## Recently closed (2026-10-27)

- Random Forest filter retired; Jev page gate with a durable retry queue and
  per-source-month completion (`processing/retry_queue.py`, `filtering/page_gate.py`).
- Legacy enum spellings fixed at the write path and normalised
  (`dedup_admin.py normalise-enums`).
- `breach_source_category` filled by Jev for every active event
  (`scripts/enrich_pii_and_source_category.py`, now a pipeline step).
- Short pages re-scraped so the source gate can judge them
  (`dedup_admin.py rescrape-short-pages`, a hygiene step).
- `sonar-pro` taken from the shared model setting (in progress with the
  enrichment work).
- No silent fallbacks: missing models, keys, labels or calibrations fail loudly
  or are logged as report-only; reviewer findings 1-7, 9-15, 24-28, 31, 32, 38-40.
