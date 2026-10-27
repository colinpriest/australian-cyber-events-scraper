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

- **Find events that affect more than one organisation.** Done as
  `find-supplier-victims` (docs/10-supplier-incidents.md). Open: victims held
  because they still have their own event near the incident need that pair
  resolved (merge or a `different` ruling); and detection by record text is
  loose, so most detected "supplier incidents" have no candidates.
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
- **Stray records inside events.** Labellers found events holding records
  about other incidents: WSU with WA Health and Instacart records, University
  of Tasmania with University of Sydney Canvas records, NT Health with an
  Updoc record. `check-coherence` exists but is not in the refresh and is not
  calibrated.
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
