# Data-quality to-do list

Open work found while building the calibrated filters (2026-10-27). Each item
names the evidence that showed the problem. Done items move to the README or
`docs/` with the commit that closed them.

## Done tonight (2026-10-27; timelines and victims: see `docs/10-victim-attribution-and-timelines.md`)

- Publication gate unblocked (2026-10-28): `out_of_scope` event label
  (rejected, gate-checked, left out of calibration); `label-evidence` hygiene
  step queues and attaches the reports labels cite; an event with no active
  record no longer "covers" an incident in the source gate.

- Labels win both ways: an unchanged labelled non-incident is rejected
  whatever it scores (12 on the live database); only events that gained
  records since labelling are held for review.
- `ASDRiskClassifications` accepts "Insufficient information" (the base
  schema's CHECK refused every asd-v2 answer of that kind).
- Supplier incidents: every affected organisation found, judged (Jev +
  escalation, calibrated) and attached; same-incident fragments merged; each
  organisation counted once in the recurrence analysis and entity dashboard
  (`find-supplier-victims`, docs/10-supplier-incidents.md).
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

- **Two label collisions left after the 2026-10-28 fix (gate allows: 2 <=
  threshold 2).** NHS Dumfries and Galloway (`a8862fc9`) is relabelled
  `out_of_scope` and rejected. For Home Affairs / ZircoDATA (`84c30c63`) and
  the University of Sydney (`793cba68`), `label-evidence` queued the reports
  their labels cite (USyd's iTnews replacement had been pruned: its own
  evidence-less event counted as covering it - fixed in `source_gate._covered`).
  Check after the next refresh that both attached. Risk: the Cyber Daily
  article is behind a registration wall (stored in 2025 as the wall and site
  menu, then dropped), so the Google page gate may reject it again; then the
  event needs another source (e.g. an OAIC or Home Affairs statement).
- **More evidence-less labelled-real events** found by `label-evidence`
  (queued the same way): LUSH (`bac5a336`), the Lynx electrical-wholesaler
  claim (`f7818274`), Tonga's health system (`29aec421`) and Ward Transport &
  Logistics (`c310fe7d`). The last two look non-Australian: candidates for
  `out_of_scope`. Strike Bowling's owner (`1043b84f`): its cited Cyber Daily
  report is already a record in another event - merge the two
  (`dedup_admin.py merge`).
- **Cyber Daily's registration wall.** 427 stored records cite cyberdaily.au
  (Webber 264, Google 98, Perplexity 65; 173 of 657 active events hold one),
  but scraping returns "You have N free articles left" and the site menu.
  Decide between a feed-based source (titles and summaries) and registered
  access before relying on its page text.
- **Short-gap repeats still made by data errors.** Oz Hair & Beauty is the
  recorded victim of a superannuation credential-stuffing campaign (victim
  attribution scored it 0.02 and flagged it, with no replacement), a 3-day
  "repeat"; youX's "Vroom by YouX Data Breach" (`4f6211e9`) is the March 2025
  S3 exposure dated 2026-02-01 (labelled so in `duplicate_labels.csv`), a
  14-day "repeat" and a duplicate of `6f4facfe`. Flagged victims should leave
  the analysis or be demoted; calibrated re-dating is still report-only.
- **ASD "Insufficient information" share.** Under prompt `asd-v2`, 101 of 637
  incidents in the dashboard window (about one in six) could not be placed and
  are listed under the matrix; before, they defaulted to C6. Review a sample:
  is the evidence genuinely too thin, or is the prompt too strict?
- **Inactive records still mapped to active events.** Exports
  (`export_events_excel.py`) and `event_reality.build_state` read every mapped
  record; `event_facts` and fingerprints read active ones only. Make them agree.

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
- **Supplier incidents: held victims and loose detection.** Victims held
  because they still have their own event near the incident (QLD Department of
  Education, University of Sydney, NDIA) need that pair resolved (`merge` or a
  `different` ruling). Detection by record text is loose, so most detected
  "supplier incidents" have no candidates. The co-victim cut-off is high (0.96,
  recall 0.11): the errors above it are brands or units of a victim already
  recorded; a better question needs fresh labels. Attachments have no undo
  command (`SupplierVictimAttachments.previous_role` records the prior role).
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
- **Calibrate the contextual-alias bar (0.9).** Label a sample of acronym mentions (record text,
  candidates, the right organisation) and choose the cut-off on them, as for the other Jev steps.
- **39 events dated "publication date" with no matching report.** Their date basis says
  publication, but no member now carries that publication date (most are 1st-of-month dates from
  the old discovery fallback). Re-derive their dates from the records' stated dates or research.
- **Recalibrate event reality, duplicates and coherence on the new evidence.** Their cut-offs were
  measured on evidence that included pages belonging to other articles (39% of stored pages on
  2026-10-28) and the first 1,200 characters only; the evidence readers now withhold mismatched
  pages and send whole pages. The calibrations still apply (same model and question versions) but
  were not measured on this evidence.
- **Records point at the wrong article at ingest.** Webber list items are paired with another
  item's link (435 records), Perplexity discovery returns a URL for a different story (334), and
  Google results a page that does not name the organisation (191). Fix the pairing in
  `datasources/webber_insurance.py` and validate a Perplexity event's URL names its organisation
  before storing it.
- **AI involvement calibration needs re-measuring when the question changes.** The cut-off in
  `evaluation/ai_involvement_calibration.json` applies only to `ai-involvement-v2` on the recorded Jev
  model; a new question version falls back to the most probable option until it is re-labelled.
- **Name-structure entity rules over-propose.** `canonical_groups` proposes folds such as IBM /
  Insurance Business Magazine / Australian Cyber Security Magazine and The Frontier Trust / Frontier
  Software; Perplexity rejects them, but each costs a call. Tighten `distinctive_core` matching.
- **Publication-date browser pass not yet run on the live data** (268 pages refused plain HTTP on
  2026-10-28); the next refresh runs it.
- **Personal-information types are not calibrated.** The breach-source step
  stores Jev's per-type probabilities and reads them at 0.5; label a sample and
  calibrate before the dashboard's exposure rates are relied on.
- **Lockfile resolved in an empty environment.** `requirements.lock` was
  frozen from the working environment, and the reviewer found contradictory
  pins (`httpcore` / `h11`, `typing-extensions`, `idna`); regenerate with a
  resolver, install into an empty environment, and keep `pip check` in CI.

## Evidence recovery follow-ups (2026-10-28)

- **Cyber Daily backfill stored 2,724 of 4,682 stories with no page text**
  (325 are records of active incidents, classified from the feed's
  standfirst). Evidence recovery leaves them to the Cyber Daily re-fetch, which
  waits 24 hours; why the first scrape stored nothing is not yet known
  ([11-evidence-recovery.md](11-evidence-recovery.md)).
- Funlab (`1043b84f`): its label cites a report already stored as another
  event's record; `dedup_admin.py merge` them, or let `check-duplicates` decide.

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
