# Data-quality to-do list

Open work found while building the calibrated filters (2026-10-27). Each item
names the evidence that showed the problem. Done items move to the README or
`docs/` with the commit that closed them.

## In progress

- **Reconcile events against organisation timelines.** The timeline dashboard
  shows events that match no researched incident and researched incidents with
  no event. At least one side is wrong each time - e.g. University of Western
  Australia: the timeline lists 2 incidents, the database holds 6 events; the
  timeline missed the 2020 ProctorU and 2022 Callista breaches, while the
  database holds UN Women Australia's attack under UWA and dates the June 2026
  Callista breach as February 2026. Needs a script that matches each event to
  a timeline incident and resolves every mismatch (see `reconcile-timelines`).

## To do

- **Find events that affect more than one organisation.** A supplier breach
  (ProctorU 2020: ten Australian universities; Canvas/Instructure; Frontier
  payroll) is one incident with several co-equal victims. The schema supports
  several `victim` roles per event, but nothing finds the other victims or
  splits a vendor breach recorded under one customer. Needed: detect
  supplier-breach events, research the affected customers, attach each as a
  victim, and make sure the recurrence analysis counts it once per victim.
- **Calibrate victim attribution.** `check-victim-attribution` (Jev + timeline)
  is report-only because no victim attributions are labelled. Wrong victims
  still in the data: UN Women Australia under UWA, a NSW driver-licence
  exposure under Optus, Aussie Fluid Power under the AFP, Ambulance Tasmania
  under Ambulance Victoria, TAFE SA under Melbourne TAFE. Label a stratified
  sample, calibrate, then let it re-attribute.
- **Scope timelines for multinationals to Australia.** Perplexity declined to
  list Microsoft's incidents (too many, worldwide) and found none for the
  Australian Digital Health Agency. For global organisations ask for incidents
  affecting Australian customers, data or operations.
- **Timelines miss supplier breaches.** UWA's timeline omits the 2020 ProctorU
  breach although the prompt asks for supplier breaches. Check recall of
  supplier incidents and strengthen the prompt or add a second query.
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
- **Re-scrape pages with no usable text.** 113 Google records the source gate
  would have judged have under 300 characters of stored page text, so the gate
  leaves them alone. Re-scraping them would let the gate (and the labellers)
  judge them.
- **Replacement records need the refresh to become events.** The source gate
  queues proper reporting for real incidents whose only evidence was a junk
  page; until scraping, enrichment and deduplication run, those incidents are
  absent from the data.
- **Older "latest" lookups.** The ledger and dedup dashboard order histories by
  `created_at` alone; on Windows, timestamps can coincide within milliseconds.
  Fine for display, but check any that pick a single latest row.
