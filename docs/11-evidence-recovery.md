# 11. Evidence recovery and evidence status

`cyber_data_collector/processing/evidence_recovery.py`, run by
`python scripts/dedup_admin.py recover-evidence` in every refresh's hygiene
phase.

## 1. The problem

The 2026-10-28 18:06 quality report (`evidence.events_without_evidence_share`)
found 169 of 1,202 active incidents none of whose records held 300 characters
of stored page text or validated Perplexity research. That does not make them
false, but the pipeline cannot verify them: the calibrated filters judge
events from page text and research, and exports and charts showed their
fields as if they were established.

Diagnosis of a copy of the live database taken at 21:39 the same evening
(1,133 active incidents after that day's hygiene; 152 such incidents by the
quality report's definition, 155 by the stricter one below):

| What the records lack | Records |
|---|---|
| Cyber Daily stories ingested that day with **no page text at all** (classified from the feed's standfirst), research never attempted | 100 |
| the same, research retrying after "unsupported claims withheld" | 21 |
| Google / Perplexity / Webber records with no or short page text, never re-fetched | 38 |
| a stored block page, counted as page text by the older metric | 1 |

No record of those incidents had been re-fetched (`PageRescrapes`), searched
for a replacement (`SourceReplacements`) or quarantined; 133 had never had
Perplexity research attempted. One (Funlab) is labelled real and cites a report
that is already another event's record.

Of the 4,682 Cyber Daily records the backfill stored that day, 2,724 have no
page text; 325 of those are records of active incidents. The Cyber Daily
re-fetch (`processing/cyberdaily_refetch.py`) repairs short stories, but only
24 hours after they were stored.

## 2. Diagnosis

For each active record of each active incident (`assess_events`):

| Question | Answer |
|---|---|
| What does the stored page hold? | `own page` (>= 300 chars of the record's own URL), `another page` (`content_origin = perplexity_alternative`: the text of `content_source_url`), `block page`, `short`, `missing` |
| Has its own page been fetched again? | Cyber Daily stories: `CyberDailyRefetches` (out of reach after 3 failures); any other URL: `PageRescrapes` (`queued` / `retry` / `gave_up` after 3); no URL, or a social / profile / marketplace URL: out of reach |
| Where is its research? | `validated`, `not attempted`, `retrying` (attempt n of 3, with the last error), `quarantined`, `attempts used up`, `not a specific event` |
| Was a labelled report queued? | `label_evidence.queued_reports`; a cited report already stored and enriched as another event's record is out of reach here (merge by hand) |
| Was a replacement searched? | `SourceReplacements` rows with `purpose = 'evidence_recovery'` for the incident |

Each route is `done`, `needed` (nothing asked for yet), `pending` (asked for,
waiting for a later step or run) or `exhausted`.

## 3. Evidence status

| `evidence_status` | Meaning |
|---|---|
| `supported` | an active record holds its own page text and one holds validated research |
| `partial` | one of the two, or only another page's text (named in the reason) |
| `unsupported` | neither: every set field is listed in `evidence_uncertain_fields` and is uncertain until evidence arrives |

`evidence_status_reason` says what is missing, record by record, and where
retrieval stands. `evidence_status_at` moves only when the status changes.
Another page's text never makes an incident `supported`: it is evidence about
the incident from a different page, and is labelled as such.

Shown in:

- `export_cyber_events.py --detailed`: `evidence_status`,
  `evidence_status_reason`, `evidence_uncertain_fields` (the reason is dropped
  with `--anonymize`, because it names pages);
- `export_events_excel.py`: an `Evidence Status` column;
- `dashboard/index.html`: the population note counts supported / partial /
  unsupported / not assessed incidents in the window;
- `dashboard/entities.html`: an `evidence unsupported` / `partial` badge on
  each event, with the reason on hover;
- the quality report: `evidence.status.*` and `evidence.recovery.*`, with a
  trend table across runs ([10-quality-gate.md](10-quality-gate.md)).

An incident not yet assessed is shown as `not assessed`, never as supported.

## 4. Retrieval

`recover(limit=100)` gives at most `limit` unsupported incidents new work per
run, newest first (`--evidence-recovery-limit` on `run_full_pipeline.py`).
Waiting work is not counted. Every route is an existing queue:

1. **The record's own page.** Cyber Daily stories are left to the Cyber Daily
   re-fetch (discovery phase). Any other URL is queued with
   `page_rescrape.queue_pages` (a `PageRescrapes` row with status `queued`),
   and `rescrape-short-pages`, the next hygiene step, fetches it with
   Playwright and no Perplexity substitute, storing only real text. When it
   stores text for a record whose research is not validated, that research is
   queued again (attempts and quarantine reset): the page is new evidence.
2. **Research.** Unvalidated, unquarantined records are already in the
   Perplexity enrichment queue (`perplexity_backfill_events.py`, run by the
   enrichment phase); records of `unsupported` incidents now go first.
3. **A labelled report.** For an incident labelled real whose label cites a
   URL, `label_evidence.queue(include_events=...)` queues the cited report,
   which `label-evidence` attaches once processed.
4. **A replacement search**, only when the record's page, its research and any
   labelled report are all out of reach, and only when no other active event
   about the same organisation near the same date covers the incident (then
   `check-duplicates` decides whether they are one incident). The search
   (`source_gate.search_and_queue`, with the source gate's prompt) is stored in
   `SourceReplacements` and never repeated; a failed one is `pending` and
   retried next run with its stored query. Its best citation is queued as a
   **new record with its own URL** (`source_metadata.evidence_for` = the
   incident), scraped and enriched like any other, and joined to the incident
   only by the calibrated duplicate check - never by this module. The source
   gate's own retry and prune leave these rows alone.

No route passes one page off as another: re-fetches store only the record's
own page, and alternatives arrive as records of their own.

A needed search without `PERPLEXITY_API_KEY` is an error (exit 2); the other
routes and the statuses are still written.

## 5. Commands

```bash
python scripts/dedup_admin.py recover-evidence --dry-run       # what would be queued
python scripts/dedup_admin.py recover-evidence [--limit 100]   # queue + mark status
python scripts/dedup_admin.py recover-evidence --status-only   # re-mark only
```

The hygiene phase runs `recover-evidence --limit N` straight after
`label-evidence` (so `rescrape-short-pages` fetches what it queued in the same
run) and `recover-evidence --status-only` last. Idempotent: a second run queues
nothing new and writes only statuses that changed.

On the 21:39 copy the first run queued 38 page re-fetches and 1 labelled
report, made no searches (no incident had exhausted its own routes), and
marked 661 incidents supported, 317 partial and 155 unsupported; 154
unsupported incidents were then waiting on a queued route and 1 (Funlab)
needed a replacement search.
