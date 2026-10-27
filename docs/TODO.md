# Data-quality to-do list

Open work found while building the calibrated filters (2026-10-27). Each item
names the evidence that showed the problem. Done items move to the README or
`docs/` with the commit that closed them.

## Done (2026-10-27, see `docs/10-victim-attribution-and-timelines.md`)

- Timelines scoped to Australia for multinationals and refusals; aliases
  limited to spellings of the same name; supplier-breach instruction
  (`timeline-v2`). Failed refreshes are recorded and flagged to judgements.
- Victim attribution calibrated; `check-victim-attribution --apply`
  demotes / re-attributes from the evidence, else flags.
- Timeline reconciliation resolves each mismatch class through a calibrated
  step (victim attribution, event reality, calibrated re-dating) or reports it.

## To do

- **Timeline recall of supplier breaches is still uneven.** After v2, UNSW's
  timeline lists the 2020 ProctorU breach but UWA's does not, and Perplexity's
  answers vary run to run. A second, supplier-only query per organisation
  (or per known supplier) would be the next step.
- **Victim re-attribution flags what it cannot replace.** Wrong sole victims
  whose real victim is not named by the evidence are flagged
  (`VictimAttributionActions.action = 'flagged'`), not changed.

- **Find events that affect more than one organisation.** A supplier breach
  (ProctorU 2020: ten Australian universities; Canvas/Instructure; Frontier
  payroll) is one incident with several co-equal victims. The schema supports
  several `victim` roles per event, but nothing finds the other victims or
  splits a vendor breach recorded under one customer. Needed: detect
  supplier-breach events, research the affected customers, attach each as a
  victim, and make sure the recurrence analysis counts it once per victim.
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
