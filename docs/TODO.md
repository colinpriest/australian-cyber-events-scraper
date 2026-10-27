# Data-quality to-do list

Open work found while building the calibrated filters (2026-10-27). Each item
names the evidence that showed the problem. Done items move to the README or
`docs/` with the commit that closed them.

## In progress (2026-10-27, night)

Being implemented on `claude/cyber-attack-recurrence-analysis-c22779`; each
moves to the README when merged and run on the live database.

- **Resolve event/timeline mismatches.** `reconcile-timelines` reports events
  matching no researched incident (29 timeline gaps on the live data),
  wrong-organisation and not-an-incident cases, and date conflicts. Each class
  is being routed to a calibrated action (victim re-attribution, event-reality
  rejection, re-research, re-dating with date provenance).
- **Calibrate victim attribution.** `check-victim-attribution` is report-only
  because nothing is labelled. Known wrong victims: UN Women Australia under
  UWA, a NSW driver-licence exposure under Optus, Aussie Fluid Power under the
  AFP, Ambulance Tasmania under Ambulance Victoria, TAFE SA under Melbourne TAFE.
- **Scope timelines for multinationals to Australia, and ask for supplier
  breaches.** Perplexity declined to list Microsoft's incidents and found none
  for the Australian Digital Health Agency; UWA's timeline omits the 2020
  ProctorU breach.
- **Events that affect more than one organisation.** Supplier breaches
  (ProctorU: ten universities; Canvas/Instructure; Frontier payroll) are one
  incident with several victims; nothing finds the other victims, and the
  recurrence analysis must count such an incident once per victim.
- **Citations must support accepted facts; affected counts need unit, basis
  and scope.** A syntactically valid URL used to count as support. Counts need
  to say what they count (people / accounts / records), whether confirmed, and
  whether Australian or global.
- **5,836 enriched records carry the constant 0.85 reliability** set by old
  code; to be recomputed from their stored research.
- **Exports, dashboards and OAIC** (reviewer fallback findings 18-21, 29,
  33-37): anonymisation failures leaking original text, risk-matrix exports
  overwriting good output, "insufficient information" defaulting to C6,
  unknown severity counted as 0, contradictory OAIC statistics published, the
  older OAIC scraper substituting dates and counts.

## To do

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
