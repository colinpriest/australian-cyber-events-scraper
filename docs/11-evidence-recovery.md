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
python scripts/dedup_admin.py recover-evidence --until-empty [--api-budget perplexity=800] [--max-minutes N]
```

The hygiene phase runs `recover-evidence --limit N` straight after
`label-evidence` (so `rescrape-short-pages` fetches what it queued in the same
run) and `recover-evidence --status-only` last. Idempotent: a second run queues
nothing new and writes only statuses that changed.

## 6. Draining the queues (`--until-empty`)

A normal run queues work for at most 100 incidents and the refresh fetches at
most 200 Cyber Daily stories, so the 2026-10-29 backlog (122 unsupported
incidents, 2,723 stories) took many refreshes. `recover-evidence --until-empty`
works through it in one command, round after round (`evidence_recovery.drain`):

1. **Queue** (`recover`): page re-fetches, labelled reports, and a replacement
   search for an incident whose own page and research are both out of reach.
2. **Fetch** what waits for these incidents: their Cyber Daily stories first
   (`cyberdaily_refetch.drain`, restricted to those stories, the incident's
   stories first), then the queued non-Cyber Daily pages
   (`rescrape-short-pages`' queue), then Perplexity research for their records
   (`perplexity_backfill_events.enrich_waiting`, records of unsupported
   incidents first).
3. **Mark** every incident's status, and go round again.

It stops, and says why, when nothing is left to retrieve (`nothing left to
retrieve`; what remains is exhausted or waits on another step such as
`check-duplicates`), a round changed nothing (`no progress`), the Cyber Daily
site throttled (`throttled`), a paid API's budget is spent (`API budget spent`;
`--api-budget perplexity=800` or `API_BUDGET_PERPLEXITY`), a key is missing
(`blocked`, exit 2) or `--max-rounds` (20) rounds passed. Exit 0 only when it
ran dry. Everything is stored as it happens, so any stop is resumed by running
the command again.

**Routes fall through, in this order, without human help.** A route is
`exhausted` exactly when its queue has stopped trying: a Cyber Daily story gone
from the site (HTTP 404/410, one failure) or given up at the re-fetch's own cap
of 5 failures (`CyberDailyRefetches.gave_up_at`); a page re-fetch that failed 3
times; research that used its 3 attempts or was quarantined. An incident whose
record's page is exhausted is researched next (records of unsupported incidents
go first in the enrichment queue); when page and research are both exhausted
the replacement search runs, and its best citation becomes a **new record with
its own URL**, joined to the incident only by the calibrated duplicate check.
Another page's text is never stored as the record's own. (Until 2026-10-29 a
404 counted as one failure of three and the story stayed `pending` forever,
holding the later routes back; the module also disagreed with the re-fetch
about the cap.)

**Progress is measured per route** (section 3, the quality report):
queued, attempted, succeeded, failing by failure type, exhausted and the age of
the oldest queued item, with a trend table (`processing/recovery_progress.py`,
[10-quality-gate.md](10-quality-gate.md)).

### Why 2,724 Cyber Daily stories had no text (diagnosed 2026-10-29)

The discovery scrape fetches every URL of a month at once; the 2026-10-28
backfill handed the scraper hundreds of Cyber Daily pages together and the
site answered **HTTP 429** (`ProcessingLog.result_data` of the later runs, where
the scrape error is kept: `{"scrape_error": "HTTP 429", "error_type":
"throttled"}`; the backfill's own errors were not kept, only "Insufficient
content retrieved", 2.5 to 3 minutes each while pages timed out or were
refused). Nothing is wrong with the stories or with the re-fetch: on a copy of
the database, 5 of 5 stories fetched one at a time came back whole, as did 10
more in a drain of two at a time (about 6 seconds a story, 0 throttled), and
the 20 earlier re-fetches in the live database all succeeded. What changed:

- `PlaywrightScraper.get_page_text` now allows at most 2 requests per host in
  flight (`SCRAPER_HOST_CONCURRENCY`), however many tasks are waiting;
- a throttled re-fetch (429/503) is a fact about the site, not the story: it is
  retried after 2 hours and does not count towards giving the story up, and
  three throttled answers in a row end a run;
- stored URLs may carry a newsletter's tracking query, including a `token`
  naming the subscriber (24 stored URLs do: 22 Webber, 2 Google). Re-fetches
  now use scheme, host and path only, and log and record nothing else.

```bash
python scripts/discovery_queue.py refetch-cyberdaily --until-empty            # ~2,700 stories: about 4.5 hours at 2 at a time
python scripts/discovery_queue.py refetch-cyberdaily --until-empty --max-minutes 120
```

On the 21:39 copy the first run queued 38 page re-fetches and 1 labelled
report, made no searches (no incident had exhausted its own routes), and
marked 661 incidents supported, 317 partial and 155 unsupported; 154
unsupported incidents were then waiting on a queued route and 1 (Funlab)
needed a replacement search.
