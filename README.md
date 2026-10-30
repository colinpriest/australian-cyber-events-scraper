# Australian Cyber Events Discovery and Enrichment Pipeline

A Python pipeline for discovering, scraping, filtering, and enriching Australian cyber security events from multiple data sources using machine learning and LLM-based analysis.

![Australian Cyber Events Dashboard](cyber-events-scraper.png)

---

## Quick Start — 3 Commands

All scripts must be run from the **project root directory**.

```bash
# Check status (last ingest + latest event in database)
python pipeline.py status

# Rolling 90-day refresh — recommended for monthly updates
python pipeline.py refresh

# Full pipeline with advanced options
python run_full_pipeline.py [options]
```

That's it for day-to-day use. Everything else is optional.

---

## Installation

```bash
git clone https://github.com/colinpriest/australian-cyber-events-scraper.git
cd australian-cyber-events-scraper

pip install -r requirements.lock      # exact versions the tests pass with
# (requirements.txt holds the ranges; regenerate the lock with
#  python scripts/setup/lock_dependencies.py after changing it)

# For OAIC Power BI dashboard scraping (optional):
playwright install chromium

# Configure API keys
cp .env.example .env
# Edit .env with your keys

# Create the database (or upgrade an existing one) - idempotent
python pipeline.py init
```

`init` applies the numbered SQL migrations in
`cyber_data_collector/storage/migrations/` (recorded in `SchemaMigrations`
with a checksum) and then every module's own schema step. CI
(`.github/workflows/ci.yml`) installs from the lock, initialises an empty
database and runs the tests, including a clean-install smoke test that takes
mocked source responses through storage, deduplication and status.

---

## Environment Variables

Create a `.env` file:

```env
# Required
OPENAI_API_KEY=your_openai_api_key

# Recommended (for event discovery)
PERPLEXITY_API_KEY=your_perplexity_api_key

# Required for the data-quality steps (Jev judgements with probabilities). Without it those
# steps fail and the run ends degraded - no other model is substituted.
TYPESAFE_API_KEY=your_typesafe_api_key

# Optional model overrides - defaults are pinned, dated snapshots (see "Language models")
# OPENAI_MODEL=gpt-5.5-2026-04-23
# OPENAI_FAST_MODEL=gpt-5.4-mini-2026-03-17
# TYPESAFE_MODEL=jev-1.14.0

# Optional (for Google Search source)
GOOGLE_CUSTOMSEARCH_API_KEY=your_google_api_key
GOOGLE_CUSTOMSEARCH_CX_KEY=your_cx_key

# Optional (GDELT only — WARNING: expensive BigQuery fees)
# GOOGLE_CLOUD_PROJECT=your_project_id
# GOOGLE_APPLICATION_CREDENTIALS=path/to/service-account.json

# Database
DATABASE_URL=sqlite:///instance/cyber_events.db

# Processing
MAX_THREADS=10
BATCH_SIZE=20
```

---

## Language models and replicability

Every LLM call takes its model from
[`cyber_data_collector/utils/llm_models.py`](cyber_data_collector/utils/llm_models.py),
so three settings describe a run completely:

| Setting | Default (dated snapshot) | Used for |
|---|---|---|
| `OPENAI_MODEL` / `--openai-model` | `gpt-5.5-2026-04-23` | adjudication, partitions, coherence, dates, ASD classification, OAIC vision extraction |
| `OPENAI_FAST_MODEL` / `--openai-fast-model` | `gpt-5.4-mini-2026-03-17` | discovery filtering, entity extraction, summaries, page classification without Jev |
| `TYPESAFE_MODEL` / `--jev-model` | `jev-1.14.0` | bounded judgements that return probabilities (page classification today) |

The options are accepted by `run_full_pipeline.py`, `scripts/dedup_admin.py`,
`scripts/run_global_deduplication.py`, `scripts/asd_risk_classifier.py` and
the export scripts. Defaults are dated snapshots, never aliases (`gpt-5.5`,
`jev-latest`), because an alias can change under the code. GPT-5 calls are made
with `reasoning_effort="none"` and `temperature=0`, the most repeatable
sampling those models allow. GPT-4o / GPT-4o-mini were used until 2026-10;
GPT-4o-mini is withdrawn in January 2027.

**Jev answers are stored, not re-asked.** Jev returns probabilities (a Choice's
distribution, a Noul's probability, a Score's level distribution) and is not
fully deterministic - the same question can return 0.99 and then 0.98. Every
answer is written to `ModelJudgements` with the question exactly as sent, a
hash of the input, the version that answered and the full distribution, and a
re-run reuses the stored answer unless the question, the input or the model
changes. The database is therefore the record of each decision: a replication
with the published database reproduces the decisions; one from scratch
reproduces them up to Jev's sampling variation, which the stored
distributions let you measure. See
[`cyber_data_collector/utils/jev.py`](cyber_data_collector/utils/jev.py).

```sql
-- Every page verdict with its distribution
SELECT subject_id, answer, value, probabilities_json, model, created_at
FROM ModelJudgements WHERE question_id = 'page_kind';
```

---

## pipeline.py — Simplified CLI

```bash
python pipeline.py refresh          # Rolling 90-day refresh (recommended)
python pipeline.py status           # Show last ingest and latest event
python pipeline.py rebuild --force  # Wipe and fully rebuild (destructive)
python pipeline.py init             # Create or upgrade the database schema
```

Every command acts on one database, resolved once: `--db-path`, else
`DATABASE_URL`, else `instance/cyber_events.db`. `rebuild` takes a verified
backup of that database before wiping it, and the wipe, the backup and the
rebuild all use the same file (it used to wipe the default database whatever
`--db-path` said).

---

## run_full_pipeline.py — Advanced Control

The unified 5-phase pipeline: Discovery → Perplexity Enrichment → Deduplication → ASD Classification → Dashboard.

```bash
# Full pipeline (all phases)
python run_full_pipeline.py

# Discovery only (auto-enriches with Perplexity)
python run_full_pipeline.py --discover-only

# Re-enrich existing events
python run_full_pipeline.py --re-enrich [--re-enrich-limit N]

# ASD Risk Classification only
python run_full_pipeline.py --classify-only [--classify-limit N]

# Dashboard only (data already collected)
python run_full_pipeline.py --dashboard-only

# Specific sources, limits, date window
python run_full_pipeline.py --source Perplexity OAIC GoogleSearch WebberInsurance --days 30 --max-events 500

# Skip classification (faster)
python run_full_pipeline.py --skip-classification
```

### Options

| Option | Description | Default |
|--------|-------------|---------|
| `--discover-only` | Discovery phase only | False |
| `--dashboard-only` | Dashboard generation only | False |
| `--classify-only` | ASD classification only | False |
| `--re-enrich` | Re-enrich existing events | False |
| `--re-enrich-limit N` | Limit re-enrichment count | No limit |
| `--skip-classification` | Skip ASD classification | False |
| `--classify-limit N` | Limit classification count | No limit |
| `--force-dedup` | Force full deduplication rebuild (skip incremental mode) | False |
| `--skip-recurrence-check` | Skip re-checking short-gap repeat attacks | False |
| `--recurrence-window N` | Gap in days (between the events' date intervals) below which a repeat is re-checked | 90 |
| `--recurrence-unverified-dates stored\|exclude` | Events whose date precision is unverified have no interval: let their stored date stand in (reported as unverified), or leave them out of the recurrence check | stored |
| `--recurrence-min-certainty F` | Certainty required to merge a suspected re-report | 0.85 |
| `--skip-missed-merge-check` | Skip the multi-key search for duplicates incremental dedup missed | False |
| `--missed-merge-apply` | Merge missed-duplicate findings automatically (default: report only) | False |
| `--missed-merge-days N` | Only judge pairs touching events changed in the last N days | 120 |
| `--missed-merge-call-budget N` | Most model calls the missed-duplicate search may make per run; groups too large to show whole are covered by overlapping neighbourhoods (see [Matching across large incident groups](#matching-across-large-incident-groups)); stored answers are free, and a spent budget is reported and continues next run | 250 |
| `--missed-merge-min-certainty F` | Certainty required to merge a missed duplicate | 0.9 |
| `--skip-entity-sizing` | Skip estimating size bands for new entities | False |
| `--entity-size-limit N` | Cap entities sized per run | No limit |
| `--evidence-recovery-limit N` | Incidents with no usable evidence given new retrieval work per run (see [Incidents with no usable evidence](docs/11-evidence-recovery.md)) | 100 |
| `--cited-refresh-limit N` | Cited pages already read that are checked again per run, most depended-on first (see [Cited pages are checked again](#cited-pages-are-checked-again)); 0 = none | 25 |
| `--continue-on-error` | Continue if a phase fails | False |
| `--enrich-limit N` | Most records automatic Perplexity enrichment takes per run (0 = no limit); the rest waits for the next run | 500 |
| `--api-budget P=N ...` | Requests allowed this run per paid API (`openai`, `perplexity`, `typesafe`); also `API_BUDGET_<PROVIDER>` | Unlimited |
| `--source SOURCE [SOURCE ...]` | Data sources to use | All |
| `--max-events N` | Max events per source per month | 1000 |
| `--days N` | Lookback window in days (0 = full history) | 0 |
| `--out-dir DIR` | Dashboard output directory | dashboard |
| `--db-path PATH` | Database file path | instance/cyber_events.db |

---

## Pipeline Phases

```
[Data Sources] → [Discovery & Scraping] → [Perplexity Enrichment]
                                                     |
                         [ML Filtering]     [Global Deduplication]
                                 |                   |
                                 └────────┬───────────┘
                                          ↓
                               [ASD Risk Classification]
                                          ↓
                               [Dashboard Generation]
```

1. **Discovery** — Collects events from Perplexity, OAIC, Google Search, Webber Insurance, Cyber Daily, GDELT. Scrapes full content. Judges each page with the calibrated Jev page gate, then the fast-model classification (see [Discovery filter and retry queue](#discovery-filter-and-retry-queue)).
2. **Perplexity Enrichment** — Extracts formal entity names, threat actors, attack methods via multi-source verification.
3. **Global Deduplication** — Merges only what is certain: an identical specific title on the same date, or the same organisation (by identity) on the same exact day *with incident-specific corroboration*. Everything else is decided by the calibrated Jev duplicate check that follows. See [Global Deduplication](#global-deduplication).
4. **ASD Risk Classification** — Assigns C1–C6 severity categories using the Australian Signals Directorate risk matrix. Incremental (skips already-classified events).
5. **Dashboard Generation** — Static HTML dashboard with Chart.js visualizations and ASD risk matrices for all years.

---

## Data Sources

| Source | Cost | API Key |
|--------|------|---------|
| Perplexity AI | ~$0.005/query | `PERPLEXITY_API_KEY` |
| OAIC | Free | None |
| Google Custom Search | Free (100 queries/day) | `GOOGLE_CUSTOMSEARCH_API_KEY` + `CX_KEY` |
| Webber Insurance | Free (scraping) | None |
| Cyber Daily | Free (section RSS feed) | None |
| GDELT | **WARNING: $500–$2000+** | Google Cloud + BigQuery |

> Do not enable GDELT unless you have a BigQuery budget. Use `--source Perplexity OAIC GoogleSearch WebberInsurance CyberDaily` to exclude it.

**Perplexity: one record per cited article** (2026-10-30, `datasources/perplexity.py`). Each article URL
an answer item cites is its own raw record (same title, description and answer fields), scraped, gated,
classified, enriched and deduplicated on its own: a citation that is not about the incident is rejected
without taking its siblings down. An item used to become one record with its *first* URL, so the other
citations were lost and the first page stood for the whole answer. `source_metadata.provenance` records
the query, the answer item's id (shared by its siblings), the URL's index, the sibling URLs, the URLs that
are not article addresses (skipped, as before) and the answer's own fields
([data dictionary](docs/data-dictionary.md#rawevents-source_metadata)). The answer's `event_date` is stored
only as a full day, with basis `stated in the Perplexity discovery answer (unverified)` and no precision.

**Incomplete is not empty.** A malformed answer is read one incident object at a time (a field only from
inside its own object; of a cut-off final object only complete values) - the old reader paired the i-th
title with the i-th date across the whole text. An empty answer, one cut off at `max_tokens`, or one
readable only after repair or recovery keeps its incidents but is noted as an `incomplete` failure, so the
source-month is searched again; a cut-off answer is first searched again over the two halves of its date
window (down to 3-day windows, 5 splits). A clean `{"events": []}` is complete. Google: a 403 that is not a
quota refusal, or an `error` object in a 200 body, keeps the results read and is noted the same way (both
used to leave the month recorded complete). The strict-JSON retry and the completion call now work (they
raised a swallowed `NameError`). Details: [docs/01-data-sources.md](docs/01-data-sources.md#json-recovery-strategy).

**Cyber Daily** (`CyberDaily`, added 2026-10-28) reads the Security section's
public RSS feed. The feed gives title, standfirst, publication date and URL,
and goes back to 2018. The article page is scraped and judged like any other
source's page. Its value is ransomware leak-site claims against small
Australian organisations that no other source reports. Of 1,001 Security
articles in the 12 months to 2026-10-27, 68 were already in the database. A
seeded sample of the rest suggests about 85 further Australian incidents a
year; the database holds 146 active events for that period.

The site's free-reading meter does not get in the way. 351 of 360 stored pages
hold the full article (most after a "N free articles left" banner), and the
other 9 keep the lead. Page-gate and event-reality scores do not differ. The
text the site hides behind its paywall is not read. Nothing logs in either:
the login form requires a Cloudflare Turnstile check, which the pipeline does
not try to get past.

With CyberDaily among the sources, every run also does two more things:

- **Backfill.** It searches processed months back to 2020-01 that Cyber Daily
  has not searched yet. The first run therefore covers about 5,000 articles
  and takes hours; it is resumable per month, and later runs have nothing left
  to do.
- **Re-fetch.** It fetches stored Cyber Daily stories again: those published
  in the last 90 days, plus any cut off or too short. Changed text is stored
  and re-enriched; unchanged text writes nothing.

- **Self-healing.** Every failure keeps its type: `throttled` (429/503),
  `missing` (404/410), `network`, `parse`, `incomplete`, `other` (another 4xx)
  or `unknown` (`processing/fetch_errors.py`; stored in `SourceMonthDiscovery`,
  `CyberDailyRefetches` and `DiscoveryRecordState`), and each type belongs to
  one of three classes. A **permanent** failure (404/410) is given up at once
  and never retried or probed; only `release` reopens it. A **temporary** one is
  retried with back-off (30 minutes doubling to a day); an **unknown** one
  (`other`, `unknown`: nothing recognised it as temporary or permanent) with a
  longer one (6 hours doubling to a week). After 5 consecutive failures a month
  is *parked*, not abandoned: the source is probed (one parked month searched,
  after a week, then after 2 and 4 weeks while the probe keeps failing) and a
  successful probe reopens every parked month of that source and searches them
  in the same run. A parked month also reopens when the configuration it failed
  under changes - the source's API key (stored as a hash), its query version
  (`processing/source_config.py`) or the installed client library versions -
  which every backfill checks first. No one has to run `release` for a
  throttled or broken-for-a-while month.
- **Incomplete stories are re-queued.** A story whose stored text is cut off
  or under 300 characters is fetched again, at most 200 per run;
  `refetch-cyberdaily --until-empty` drains the whole backlog in one command
  (2 pages at a time, stops when the site throttles, resumable). The stories
  had no text because the 2026-10-28 backfill fetched hundreds of pages at once
  and the site answered HTTP 429; the scraper now allows 2 requests per host in
  flight, and a throttled answer is the site's, not the story's, so it never
  counts towards giving a story up. That
  includes records filtered out on the feed's standfirst alone; when the full
  text arrives they are reopened and classified again. The first run left
  2,724 such records (the discovery scrape got no text).
- **Monitored.** The quality report shows Cyber Daily articles seen in the
  feed, stored and judged per publication month. It warns on months short
  against their neighbours, on months not searched or retrying after a
  temporary failure, on months parked and sources whose probe is overdue, and
  on incomplete stories. It blocks publication only on a month that was given
  up for good (404/410).

```bash
python scripts/discovery_queue.py backfill --source CyberDaily --since 2020-01 [--force]
python scripts/discovery_queue.py refetch-cyberdaily [--days 90 | --all] [--repair-limit N] [--dry-run]
python scripts/discovery_queue.py refetch-cyberdaily --until-empty [--max-minutes N] [--concurrency N]   # drain the backlog
python scripts/discovery_queue.py status    # failed searches with type and next retry; permanent give-ups; parked months with
                                            # their next probe and what would reopen them; stories no longer fetched
python scripts/discovery_queue.py reopen [--dry-run]   # reopen parked months whose source configuration has changed
python scripts/discovery_queue.py release   # permanently given-up months and stories back in the queue
python run_full_pipeline.py --cyberdaily-backfill-since none --skip-cyberdaily-refetch   # opt out for a run
```

Details: [docs/01-data-sources.md section 5a](docs/01-data-sources.md#5a-cyber-daily).

---

## Project Structure

```
australian-cyber-events-scraper/
├── pipeline.py                    # Simplified CLI entry point (refresh/status/rebuild)
├── run_full_pipeline.py           # Advanced 5-phase pipeline entry point
├── requirements.txt
│
├── cyber_data_collector/          # Core package
│   ├── datasources/               # Perplexity, OAIC, Google Search, Webber, Cyber Daily, GDELT
│   ├── enrichment/                # High-quality 5-stage enrichment pipeline
│   ├── filtering/
│   │   ├── page_gate.py           # Jev discovery page gate (rf_event_filter.py: retired)
│   │   ├── progressive_filter.py
│   │   └── confidence_filter.py
│   ├── models/                    # CyberEvent, EventSeverity, CyberEventType
│   ├── pipelines/
│   │   └── discovery.py           # Discovery and initial processing pipeline
│   ├── processing/                # LLM classification, deduplication, enrichment
│   ├── publication/               # Retained database snapshots; all-or-nothing publication of every output
│   ├── storage/
│   │   ├── cyber_event_data_v2.py # Thread-safe SQLite database operations
│   │   ├── database.py
│   │   └── deduplication_storage.py
│   ├── utils/
│   │   ├── entity_scraper.py      # Playwright-based web scraping
│   │   ├── llm_extractor.py       # GPT-4o-mini event extraction
│   │   └── ...
│   └── tests/
│
├── scripts/                       # Utility scripts (run from project root)
│   ├── asd_risk_classifier.py     # Standalone ASD risk classification
│   ├── build_static_dashboard.py  # HTML dashboard generation
│   ├── repair_confidence_defaults.py # Clear provably-defaulted confidence values (--dry-run)
│   ├── analyze_recurrent_timing.py # Repeat-event survival/recurrent timing analysis
│   ├── build_recurrence_dashboard.py # All recurrence runs on one HTML page
│   ├── recurrence_conclusions.py  # Conclusion sentences generated from the estimates
│   ├── quality_report.py          # Quality metrics, history and the publication gate
│   ├── measure_neighbourhoods.py  # Neighbourhoods vs disjoint slices on a copy (no model)
│   ├── evaluate_dedup.py          # Measure the dedup paths on labels and seeded cases (no paid calls by default)
│   ├── publish_outputs.py         # Snapshot, gate and publish every output together; list/prune snapshots
│   ├── perplexity_backfill_events.py  # Backfill Perplexity enrichment
│   ├── project_status.py          # Pipeline status reporter
│   ├── run_global_deduplication.py    # Standalone deduplication runner
│   ├── wipe_database.py           # Database reset utility
│   ├── oaic/
│   │   ├── oaic_data_scraper.py       # OAIC PDF report scraper (<= 2024 H2)
│   │   ├── OAIC_dashboard_scraper.py  # OAIC Power BI dashboard scraper (<= 2025 H1)
│   │   ├── oaic_datagov_scraper.py    # OAIC data.gov.au XLSX ingester (2025 H2 onward)
│   │   ├── refresh_oaic_statistics.py # Routine refresh into the published dataset
│   │   ├── oaic_dataset.py            # Published dataset: promote-on-pass, refresh state
│   │   ├── oaic_consistency.py        # Shared internal-consistency rules
│   │   └── cleanup_oaic_data.py       # OAIC data validation (fails on corrupt files)
│   ├── export/
│   │   ├── export_events_excel.py     # Export to Excel with LLM summaries
│   │   └── export_cyber_events.py     # Full database export (CSV/Excel)
│   └── setup/
│       └── setup_bigquery_auth.py     # BigQuery authentication setup
│
├── docs/                          # Detailed pipeline stage documentation
├── analysis/                      # Generated statistical analysis reports
├── machine_learning_filter/       # Retired Random Forest model artifacts (kept for reproduction)
├── instance/                      # SQLite database (gitignored)
├── logs/                          # Log files (unified_pipeline.log, etc.)
├── dashboard/                     # Generated HTML dashboard (gitignored)
└── risk_matrix/                   # Generated ASD risk matrix Excel files
```

---

## Utility Scripts

All utility scripts are in `scripts/` and must be run from the project root.

### ASD Risk Classifier

```bash
python scripts/asd_risk_classifier.py                     # Classify all unclassified events
python scripts/asd_risk_classifier.py --limit 50          # Limit to 50 events
python scripts/asd_risk_classifier.py --force-reclassify  # Re-classify all events
python scripts/asd_risk_classifier.py --output-dir my_matrices
```

When the evidence cannot decide a severity, impact or stakeholder the answer
is `Insufficient information` (prompt `asd-v2`), never a default C6; such
classifications are listed under the matrix (dashboard) and on a "Not placed"
sheet (Excel), not put in a cell. `ASDRiskClassifier.process_events` sets
`failed_count` / `failed_event_ids` for events it could not classify (their
old classifications stay stale and counted by `stale_count()`); the script
exits 1 if any failed. Each risk-matrix workbook is written to a temporary
file and moved into place, so a failure raises `RiskMatrixExportError` and
leaves the previous workbook untouched; an empty matrix is written only for a
population with no classifications.

### Global Deduplication

```bash
python scripts/run_global_deduplication.py             # Incremental deduplication (new events only)
python scripts/run_global_deduplication.py --force      # Full rebuild (reprocess all events)
python scripts/run_global_deduplication.py --dry-run   # Preview without changes
python scripts/run_global_deduplication.py --verbose    # Enable verbose logging
```

**What merges here.** Only two deterministic rules, shared by the full
rebuild and the incremental matcher (`DeduplicationEngine.merge_decision`):

- an identical title on the same date, when the title names something (a title
  such as "Data breach" is the same words for any incident and is treated like
  a bare shared day);
- the same victim organisation on the same exact day **and incident-specific
  corroboration** (below). "Same" means identity - legal-suffix and spelling
  variants, known aliases, compound names sharing a whole member - never a
  shared word: the old fallback treated any two titles containing
  "university", "council", "hospital" or "health" as one organisation. An event
  with no recorded victim matches nothing. The 1st of a month does not count as
  an exact day, because it is how a month-only date is stored.

**A shared day is a candidate, not a verdict.** Dates are often publication
dates, so a roundup or the report of a second attack can carry the day of the
first. The same-day rule merges only when the two sides agree on something
specific to the incident (`dedup/merge_corroboration.py`, rules
`merge-corroboration-v1`), with no conflict:

| Agreement (any one) | Definition |
|---|---|
| shared article | both cite the same article URL; hub URLs (cited by more than 3 events) and bare domains do not count |
| record count | both state at least 100 records, within 25% of each other |
| attacker | both name one, and one name (without its parenthetical qualifiers) appears whole in the other; placeholders such as "Unknown ..." are not names |
| attack method | both describe it and share two words, one of them *distinctive in the corpus* (at most 2% of events use it, measured by `CorpusStats`, not listed); silent without a corpus |

A conflict - every pairing of record counts differs by more than a factor of
two, or two named attackers share no word - defers the pair (a shared article
overrides). Every fact is a set, so an existing event of many records agrees if
any member does; incremental matching loads each existing event's members' URLs,
methods, attackers and counts for that (`run_global_deduplication.py`,
`_load_member_facts`). The thresholds are structural, not fitted to labels: they
only ever remove automatic merges, and `scripts/evaluate_dedup.py` measures what
they change. A pair left unmerged is not lost: `check-duplicates` pairs every
two events of one organisation (dated or not), so it is judged with both events'
evidence, date provenance and the organisation's timeline, once, and the answer
is stored. The run logs how many pairs were deferred
(`deferred_same_day_pairs` in the run statistics).

Measured on the 2026-10-29 live copy: of 13,647 record pairs the old rule
merged, 12,323 are corroborated (90.3%), 1,156 had no corroborating fact
(8.5%) and 168 conflicted (1.2%); of the 407 existing events of two or more
records, 46 are connected by the deterministic rules alone and 10 of those would
now need the judgement (all 10 look like true duplicates with sparse metadata -
no count, a generic method - which the judgement should re-join). Past merges
are not undone.

The fuzzy rules (titles 15% alike, descriptions 20-35% alike) are gone; those
pairs stay separate and the calibrated `check-duplicates` step decides them
with both events' evidence, date provenance and the organisation's timeline.
Incremental matching also respects cached `different` rulings, and attaches
every record of a matched group (only the lead record used to be attached,
which left 34 live records in no event).

**Membership check.** A run fails if any active record belongs to no event or
to more than one; records removed by a merge or rejection are not counted.

**Records that change after placement.** `RecordFingerprints` remembers what
each placed record looked like. When a record is re-enriched (a corrected date,
victim research or record count), its events are refreshed with their ids
kept: title and date re-derived from all members, fields copied from the master
record updated unless corrected at event level since, and entity roles marked
outdated for the hygiene phase's `classify-entities --stale-only`. ASD
classifications store a fingerprint of their inputs (event fields, prompt
version, model) and are redone when it changes; Jev judgements already key on
an input hash. So re-enrichment improves the published results without a full
rebuild.

> **Note on the incremental-mode warning.** `run_full_pipeline.py` warns when
> dedup output is under 10% of input. In incremental mode `output_events`
> counts only *newly created* dedup rows — events that merge into existing rows
> are counted separately — so a low ratio is normal and not evidence of
> over-merging. Check `merged into existing` in the log before acting on it.

### Deduplication v3 — audit, review and correction

Entity-aware, LLM-adjudicated deduplication with a full audit trail, reversible
merges, and human overrides that survive rebuilds. Full design:
[docs/09-deduplication-v3.md](docs/09-deduplication-v3.md).

```bash
python scripts/dedup_admin.py migrate                       # idempotent schema
python scripts/dedup_admin.py backfill --dry-run            # repair lineage + provenance
python scripts/dedup_admin.py backfill

python scripts/dedup_admin.py find-missed --no-llm          # free candidate preview
python scripts/dedup_admin.py find-missed                   # adjudicate (embeddings + GPT-4o)
python scripts/dedup_admin.py apply-missed --min-certainty 0.9

python scripts/dedup_admin.py override <enr_a> <enr_b> different --reason "..."
python scripts/dedup_admin.py split <dedup_id> <enriched_id> --reason "..."
python scripts/dedup_admin.py learn                         # fold corrections back in
python scripts/dedup_admin.py ancestry <dedup_id>           # provenance tree as JSON
```

Every merge records who decided, how certain, by what method, the reasoning and
the evidence. Splits and merges run against stored state — **no re-scraping or
pipeline rerun** — and each mutation snapshots the previous state for restore.
Overrides are keyed on `enriched_event_id` pairs (stable) rather than
`deduplicated_event_id` (regenerated every rebuild), so corrections persist.

### Missed-duplicate detection: three candidate keys

Deduplication can only merge what candidate generation puts in front of it. A
review of May/June 2026 found 21 duplicates still stored as separate events,
and the original blocking - on the single `victim_organization_name` - would
have surfaced only **9 of them**. Three shapes defeated it:

| Shape | Example | Key that catches it |
|---|---|---|
| No usable victim name | "Australian sugar producer", "Queensland education sector" | every organisation linked to the event: entity links, vendor, and Perplexity's formal victim name on each source record |
| Same article, different framing | "OpenAI slows training" vs "OpenAI hacked Medicare portal" | a shared source URL, ignoring *hub* URLs (roundups, breach lists, section indexes cited by more than 3 events) |
| Supplier breach, a different victim per report | Canvas/Instructure -> USyd, QLD DoE, DECYP | a corpus-rare subject word shared within 60 days |

With all three, 20 of the 21 surface. Precision still comes from the
adjudicator: a shared article or subject sends the pair to GPT-4o (past the
name gate and embedding filter, which would wrongly reject it) rather than
merging on a rule, because one article can cover two incidents. The prompt
states the project convention that one supplier breach reported about several
customers is one event.

`run_full_pipeline.py` runs this after every incremental dedup, scoped to pairs
touching events changed in the last 120 days, and writes findings to
`instance/dedup_missed_merges.json` **for review - it does not merge** unless
`--missed-merge-apply` is passed (`--skip-missed-merge-check` to skip it).

Why report-only: on a labelled set from the May/June 2026 review, pairwise
adjudication at certainty >= 0.9 kept 28 of 30 true duplicates but still
accepted 11 of 22 known-wrong pairs, mostly roundup / newsletter / guidance
pages matched to the incidents they mention, and it confidently rejected
misdated fragments of one incident when shown them two at a time. Better
candidate generation fixed recall; precision needs (1) junk pages rejected
before deduplication and (2) whole-cluster adjudication over the candidate
graph, as `adjudicate-clusters` does, instead of pairwise judgement.

**Stage 2 (2026-10-02) - measured, still report-only.** Whole-group
adjudication (`adjudicate-candidates`, now the routine step) partitions groups
of strongly-linked candidates instead of judging pairs. On the labelled set:

| At certainty >= 0.9 | Wrong pairs accepted | Genuine kept | Known May/June fragments |
|---|---|---|---|
| Pairwise (`find-missed`) | 2/10 | 34/36 | 9/21 |
| Whole-group, uncapped | 1/10 | 20/38 | 14/21 |
| Whole-group, groups capped at 20 (current) | 3/10 | 31/38 | 11/21 |

None is safe to merge unattended - each accepts some genuinely separate
incidents (Mathspace/Tixel, MIFF/Hampr, two Sarcoma victims) - so the routine
step proposes and a human confirms with `dedup_admin.py merge`. Uncapped
groups chained 545 of 820 events together through shared articles and rare
words, hence the size cap.

Also in stage 2: suspect pages are classified (`classify-pages`, gpt-4o-mini,
verdicts stored in `PageClassifications`) and confident roundup / guidance /
profile / index verdicts count as non-incidents; merges and splits date an
event by member consensus instead of earliest-wins, and a 1st-of-month
placeholder gives way to a day the members agree on. Known open issue:
*contaminated* events that contain a stray record about another incident
(RACGP holding a Smoke Alarm Solutions record) pull the adjudicator toward
wrong matches; `recheck-splits` / `split` are the current remedy.

A full sweep is manual:

```bash
python scripts/dedup_admin.py adjudicate-candidates --dry-run --verbose   # whole groups (routine step)
python scripts/dedup_admin.py classify-pages && python scripts/dedup_admin.py reject-non-incidents
python scripts/dedup_admin.py find-missed                  # all pairs
python scripts/dedup_admin.py find-missed --recent-days 120
python scripts/dedup_admin.py apply-missed --min-certainty 0.9
python scripts/dedup_admin.py merge <target_dedup_id> <source_dedup_id> --reason "..."
```

### Reusing expensive work, and better evidence for the adjudicators

`find-missed`, `adjudicate-candidates`, `adjudicate-clusters`,
`reconcile-entities`, `review-legacy`, `resolve-band` and `recheck-splits`
share three improvements (`dedup/adjudication_cache.py`, `dedup/excerpts.py`):

- **Embeddings are stored.** `EmbeddingCache` is keyed by a hash of the exact
  text embedded, the embedding model and `EMBED_TEXT_VERSION`; vectors are
  float32, and a fresh run is rounded the same way, so a cached and a fresh
  run compare identical numbers. They were kept in memory only, so every
  `adjudicate-candidates` run embedded every active event again (1,090 on the
  2026-10-29 copy, whatever had changed).
- **Scope first.** With `--recent-days`, `adjudicate-candidates` works out which
  events are connected to a recently changed event through candidate pairs
  (`candidates.scope_ids`, free) and embeds only those: a group is kept only if
  it holds a recent event, and every member of such a group is linked to that
  event, so the groups are the same as when everything is embedded. (On a copy
  made just after a full pipeline run nearly every event is "recent", so the
  saving appears on a database whose old events have not been touched.)
- **Answers are stored against their evidence.** `AdjudicationCache` keys a pair
  or cluster answer by a hash of everything the model was sent (system prompt
  and rendered evidence), the model, `PAIR_PROMPT_VERSION` /
  `CLUSTER_PROMPT_VERSION` and `excerpts.EXCERPT_VERSION`. A pair whose
  evidence, prompt or model is unchanged is not asked again; swapping the two
  records is a different prompt, so the order-robustness checks still ask.
  Only valid answers are stored (a failed call or a partition that does not
  cover every record is retried), and a hit needs no API client at all. The
  commands print the cache and call statistics.

**Excerpts instead of an opening.** The adjudicators used to be shown the first
few hundred characters of a summary, which is a masthead or a standfirst. They
now see passages chosen deterministically from each member's summary,
description and stored page (the page only if it belongs to the record, the
rule in `processing/active_evidence.py`), within a character budget (900 per
event in a pair prompt, 450 in a cluster prompt): a passage scores for naming
the organisation (3), the attack - the words of the record's own research
method and attacker (2) - and then for stating a date (2) or a count (2); a
passage that names neither the organisation nor the attack scores nothing, so a
roundup page's dates and counts about other organisations do not crowd out the
incident; sources are served round-robin, best passage first; a passage that
repeats one already chosen is skipped; when nothing scores, the opening is used
as before. The pair prompt is `pair-adjudication-v2` and the cluster prompt
`cluster-partition-v2`; these (and `excerpts-v1`) change what the model sees, so
earlier answers are not reused. These adjudicators are not calibrated paths
(`find-missed` and the candidate clusters report or merge at a stated
`--min-certainty`), so no calibration is affected; the calibrated
`check-duplicates` evidence (`dedup/duplicate_judgement.py`) is unchanged.

### Measuring the deduplication paths

```bash
python scripts/evaluate_dedup.py --db <copy of the database> --save before.json
# ... change something ...
python scripts/evaluate_dedup.py --db <copy of the database> --compare before.json
```

`scripts/evaluate_dedup.py` scores the event-dedup paths with no manual review,
on the known cases: `evaluation/duplicate_labels.csv` (as the pairs stood in the
labelling-time snapshot named in `evaluation/calibration_snapshots.json`),
`evaluation/review_acceptance_cases.csv` (`same_incident_as`), and seeded cases -
synthetic **splits** of active events that are known single incidents (records of
one organisation, divided in two by a seeded shuffle) and synthetic **joins** of
known distinct ones (one organisation's events more than 730 days apart). The
paths are the deterministic rule (and `rule-before`, the same rule before
corroboration was required), candidate generation, the calibrated Jev duplicate
check (read from `ModelJudgements`) and the pair adjudicator (read from
`AdjudicationCache`). Per path and source it prints the duplicates found and
**missed**, the **incorrect merges**, the candidate counts and the **paid
calls, cached versus new**; it also reports the labelled outcomes in the
database as it stands (`end-state`) and how many existing events were joined
only by the uncorroborated same-day rule (`rule-impact`; counts, nothing is
undone). It never calls a paid API unless `--paid-calls` is given and opens the
database read-only otherwise; a case a path cannot answer from storage is
counted as one new call and left undecided. Run it on a copy.

### Cached rulings: splits stay split

Every split is recorded as a `different` ruling in `DedupOverrides` between
the departing record(s) and every record left behind, and `merge_events`
refuses to join two events when any record of one has a `different` ruling
against any record of the other. So a manual correction cannot be undone by a
later automated pass (`find-missed`, `adjudicate-candidates`,
`reconcile-entities`, `adjudicate-clusters`, `check-recurrences`, ...).
Rulings are keyed on `enriched_event_id`, which survives rebuilds.

```bash
# Move records out together as ONE event (rulings only against what stays)
python scripts/dedup_admin.py split <dedup_id> <enriched_id> [<enriched_id> ...] --reason "..."
# A human can still overrule a cached ruling deliberately
python scripts/dedup_admin.py merge <target> <source> --reason "..." --force
```

Move a multi-record incident out with one `split` naming all its records:
splitting them one at a time caches rulings *between* them, and the safeguard
then (correctly) refuses to regroup them.

### Calibrated data-quality filters

Every refresh cleans the data after deduplication and before the recurrence
check (`run_full_pipeline.py`, `--skip-data-hygiene` to opt out). Each step
that deletes, merges or re-attributes acts only at a cut-off **calibrated on
labelled examples from this database**, recorded with the models, question
versions **and evidence processing** it was measured on; change a model, a
question or what a judgement is shown and the step falls back to reporting
until it is recalibrated (see [Calibrations hold only for their evidence](#calibrations-hold-only-for-their-evidence)). Labels are ground truth: an item
labelled one way is never acted on the other way. A labelled non-incident is
rejected whatever it scores, unless it holds a record it did not hold when
labelled (checked against the labelling snapshot; then it is held for review).
A labelled junk page that is the only record of a labelled-real event is
removed and the event kept for its replacement evidence.

| Order | Step | What it decides | Model | Cut-off (2026-10-27) | Precision on labels | Recall | Out-of-sample* |
|---|---|---|---|---|---|---|---|
| 0 | `check-sources --apply` | Does a Google result's page itself report an incident? | Jev | remove below **0.58** | 0.953 (95/100) | 0.97 | 0.95 |
| 1 | `classify-pages` | What kind of page each suspect record is (informs, removes nothing) | Jev | - | - | - | - |
| 2 | `check-events --apply` | Is the event a real, specific incident? | Jev + escalation | reject below **0.28** | 0.954 (68/72) | 0.71 | 0.91 |
| 2a | `check-scope --apply` | Does the event affect Australia (an Australian victim, or Australians among the victims)? A foreign incident that Australia only reacts to is out of scope | Jev + escalation | reject below **0.72** (2026-10-29) | 0.969 (57/59) | 0.48 | 0.93 |
| 3 | `check-victims` | Do the records name a different victim? (deterministic rule) | none | - | 8/9 reviewed | - | - |
| 3b | `check-coherence --apply` (not yet wired into `run_full_pipeline.py`) | Is each suspect record about its event's incident? (splits strays out) | Jev + escalation | split below **0.15** | 1.00 (17/17) | 0.94 | 0.98 |
| 3c | `check-same-organisation --apply` | Is a member that may be a *separate incident at the same organisation* (verified far date, or another incident in the timeline) the event's incident? | Jev + escalation | none yet: report-only until labelled and calibrated | - | - | - |
| 4 | `check-duplicates --apply` | Are two events about one organisation the same incident? | Jev + escalation | merge at **0.81** | 1.00 (16/16) | 0.94 | 0.97 |
| 4b | `check-duplicates --apply` (AI pairs) | Are two AI-involved events, whatever their victim, the same incident? | Jev + escalation (AI-pair question with the base rate) | its own cut-off from `evaluation/ai_duplicate_calibration.json`; **report-only until labelled and calibrated** | - | - | - |
| 3b | `check-victim-attribution --apply` | Is the recorded victim really the affected organisation? Demote / replace from the evidence / flag | Jev + escalation | act below **0.15**; replace at choice **0.84** | 0.956 (43/45); replacements 23/25 | 0.38 | 0.94 |
| 5 | `reconcile-timelines --queue-gaps` | Do events agree with the organisation's researched timeline? Resolved by steps 2 and 3b; re-dating report only (2 eligible conflicts, too few to calibrate) | Jev | - | - | - | - |

\*Mean precision of the whole choose-then-test procedure on held-out labels
(stratified bootstrap), the honest estimate - choosing a cut-off on the labels
it is scored on flatters it.

```bash
python scripts/dedup_admin.py check-sources [--apply] [--unmapped-only]
python scripts/dedup_admin.py check-events [--apply]
python scripts/dedup_admin.py check-scope [--apply] [--event <dedup_id>]
python scripts/dedup_admin.py check-victims [--dry-run]
python scripts/dedup_admin.py check-coherence [--apply] [--event <dedup_id>]
python scripts/dedup_admin.py check-same-organisation [--apply] [--event <dedup_id>]
python scripts/dedup_admin.py check-duplicates [--apply]
python scripts/dedup_admin.py check-victim-attribution [--apply]
python scripts/dedup_admin.py resolve-label-contradictions [--dry-run]
python scripts/dedup_admin.py research-timelines
python scripts/dedup_admin.py reconcile-timelines [--queue-gaps] [--apply]
python scripts/dedup_admin.py recalibrate [--status | --if-stale | --dry-run] [--step STEP ...]
```

These commands fail (exit 2) rather than skip when Jev or Perplexity is
needed and unavailable, and `--apply` refuses when its labels file is
missing, unreadable or empty. A calibration measured on another model,
question version or evidence processing makes the step report-only, and the
log says which part differs.

#### Calibrations hold only for their evidence

A cut-off measured on labelled cases scored over one body of evidence says
nothing about another. On 2026-10-28 the evidence readers changed
(`processing/active_evidence.py`: active records only, pages of other articles
withheld) while the event-reality, duplicate and coherence cut-offs - activated
on the model and question version alone - kept rejecting and merging. Each
reader now carries an **evidence fingerprint** (`dedup/calibration_guard.py`):
a hash of an `EVIDENCE_VERSION` constant, bumped by hand when the *logic* of
what a model is shown changes, and of the parameters that shape it (character
limits, record caps, the escalation effort, the shared active-evidence rules,
the timeline and corroboration limits, date provenance). It is written into
every calibration file as `evidence` beside `model` and `question_version`;
activation needs all three to match, and a calibration with no `evidence`
(measured before fingerprinting) does not apply. A reader that changes no
parameter but changes what it shows must bump its `EVIDENCE_VERSION`.

`recalibrate` is the committed way back. For each calibration that no longer
matches it:

1. finds the **labelling-time snapshot** named for the step in
   `evaluation/calibration_snapshots.json` (a backup beside the live database,
   taken before the labelled items were merged, split or rejected; override
   with `--snapshot STEP=PATH`) - labels describe the database as it was when
   they were made, and most labelled items no longer exist as active events;
2. works on a kept copy under `instance/recalibration/` into which the live
   database's stored timelines and corroboration searches are carried (a live
   search cannot be replayed);
3. re-scores every labelled case with the **current readers** - the same
   functions the pipeline calls - spending Jev and escalation calls only for
   evidence whose input hash is new (answers are keyed on it, so a repeat run
   or a resumed one buys nothing twice), and fails if any case cannot be
   scored;
4. recomputes the cut-off with the step's own calibration script, which
   records the new fingerprint, and keeps the old file if far fewer labelled
   cases were usable (`--min-coverage`, 90%).

Limits: the snapshot holds the records, research and pages as they were at
labelling time, scored by today's readers and questions. The new cut-off
therefore certifies the *reader*, not the data it meets now (re-enrichment,
re-scraped pages and corrected dates since labelling are not in the
measurement); strata weights come from the original sample files and are not
redrawn; the supplier-victim calibration is reported stale but recalibrated
with `scripts/calibrate_supplier_victims.py`. The source-gate calibration was
**attested** rather than re-measured (its reader is unchanged since
measurement; `evidence_attestation` in its file) and is replaced by a measured
value at its next recalibration. The refresh runs `recalibrate --if-stale`
first (`--skip-recalibration` to opt out; with `--report-event-rejections` it
only lists); a step whose calibration cannot be recomputed stays report-only
and the run ends degraded.

**Flagged victims are reassessed, and unresolved ones are marked.** When
`check-victim-attribution` finds a recorded victim below the cut-off and
neither a co-victim nor a calibrated replacement can take its place, it
*flags* the pair and leaves it. Each action row records the hash of the input
it judged and the event's member signature, and every run judges every pair
again (Jev is asked only where the evidence, the timeline or the membership
changed). So a flagged pair ends each run **confirmed** (re-judged at or above
the cut-off, or labelled correct: a `confirmed` row clears the flag),
**replaced** (demoted or re-attributed), or **still flagged** with its reason.
A still-flagged pair is an *unresolved attribution*. The incident stays in
every count, and victim counting, the by-entity dashboard ("attribution
unresolved" badge and filter) and the recurrence analysis (counts and repeats
with them included and excluded, `--unresolved-attributions exclude`) mark it
separately. On the 2026-10-28 copy, 43 active pairs were flagged; 15 had
changed input and are re-judged on the next run. Details:
[docs/10-victim-attribution-and-timelines.md](docs/10-victim-attribution-and-timelines.md).

**Labels** (all blind to the model being calibrated, each with a written
reason; reviewed where flagged): `evaluation/source_gate_labels.csv` (170
pages), `evaluation/event_reality_labels.csv` (168 events),
`evaluation/duplicate_labels.csv` (157 pairs - every same-victim pair),
`evaluation/coherence_labels.csv` (148 event members - every suspect).
`evaluation/victim_attribution_labels.csv` (151 event-victim pairs; see
[docs/10-victim-attribution-and-timelines.md](docs/10-victim-attribution-and-timelines.md)).
Labellers were Claude Opus 5.5 subagents working from the stored evidence plus
web searches; conventions worth reviewing are noted in each file's rationales
(dark-web sale claims naming no victim = unsure; campaigns naming no victim,
physical-security incidents and non-cyber fraud = not an incident).
An event label `out_of_scope` marks a real incident outside the dataset (not
Australian): `check-events --apply` rejects it, the quality gate checks it
stays rejected, and calibration leaves it out.

**Event scope.** Discovery's `is_australian_event` is the fast model's guess and
dedup writes it as a constant `True`, so until `check-scope` nothing asked of an
active event whether it affects Australia (a BBC story of an AI agent reaching
US agencies' sites sat in the database as an "Australian" event).
`check-scope` asks Jev one Noul question per active event over the same
evidence event reality sees (every active record's page text and Perplexity
research, the victim's stored timeline; no corroboration search), and the
middle band (0.1-0.7) goes to the reasoning model, as event reality does. In
scope: the affected organisation is Australian (or the Australian arm of a
foreign one), or the reporting says people or organisations in Australia were
among those affected, whoever is recorded as the victim. Out of scope: the
reporting identifies who was harmed and none of them is in Australia; an
Australian regulator's or commentator's reaction, an Australian outlet, "could
affect Australia" speculation and an unaffected Australian office are not
victims. A global breach (Okta, Twitch, a worldwide outage) counts as
Australian only where a source places Australians among its victims - not
because its users "must" include some (convention confirmed by the project
owner, 2026-10-29). Thin evidence is never grounds to reject. The decision is the model's
over the evidence: there are no country, agency or outlet lists in the code.
Calibration: `scripts/calibrate_event_scope.py sample | packet --out DIR |
calibrate` on blind labels in `evaluation/event_scope_labels.csv`
(`deduplicated_event_id,label,rationale,labeller`; `in_scope`, `out_of_scope` or
`unsure`), folded with the event-reality label `out_of_scope` (`real` means a
real incident, not an Australian one, so it says nothing about scope; a
scope-file label overrides); the cut-off is the largest
at which at least 95% (weighted) of the events it would reject are labelled
out of scope. With `--apply` and a matching calibration, events below it and
events labelled `out_of_scope` are rejected through the ledger (batch
`reject-event-scope-*`, reversible) and events labelled in scope never are;
without one it reports and writes `instance/event_scope_findings.json`.
Needs `TYPESAFE_API_KEY` and `OPENAI_API_KEY` (exit 2 otherwise); an event Jev or
the escalation failed on is left undecided and the step exits 1.

**Labelled-real events with no evidence.** The source gate keeps a
labelled-real event whose only page was junk, but the page stays mapped to it
and contradicts the page labels. `dedup_admin.py label-evidence` (the first
hygiene step) queues the report the label cites (`corroboration` column) for the
next refresh - processing a stored copy again rather than storing it twice -
and once that report is a record, merges its event into the labelled one and
splits the inactive pages out and rejects them. A cited report that is
processed and still rejected is reported, not left waiting.

**Incidents with no usable evidence.** On 2026-10-28, 169 of 1,202 active
incidents had neither 300 characters of stored page text nor validated
Perplexity research (on the evening copy, 121 of 152 were Cyber Daily stories
ingested that day with no page text at all). `dedup_admin.py recover-evidence`
(`processing/evidence_recovery.py`, the second hygiene step) finds what each
incident's records lack and queues retrieval through the existing queues only,
at most `--evidence-recovery-limit` (100) incidents a run: the record's own page
re-fetched (the Cyber Daily re-fetch, or `rescrape-short-pages` for any other
source, which then queues the record's unvalidated research again), Perplexity
enrichment (unsupported incidents first), the report a label cites, and - only
when all of those are out of reach - one stored replacement search whose best
citation becomes a **new record with its own URL**, joined to the incident only
by the calibrated duplicate check. Another page's text is never passed off as
the record's own. Every incident gets `DeduplicatedEvents.evidence_status`:
`supported` (own page text and validated research), `partial` (one of them, or
only another page's text) or `unsupported` (neither; `evidence_uncertain_fields`
lists the fields nothing supports), with the reason. Exports, the main and
entity dashboards and the quality report (with a trend table) show it.
Details: [docs/11-evidence-recovery.md](docs/11-evidence-recovery.md).

```bash
python scripts/dedup_admin.py recover-evidence --dry-run     # what would be queued
python scripts/dedup_admin.py recover-evidence               # queue + mark status
python scripts/dedup_admin.py recover-evidence --status-only # re-mark only
python scripts/dedup_admin.py recover-evidence --until-empty [--api-budget perplexity=800]   # drain every route
```

`--until-empty` repeats queue -> fetch (Cyber Daily stories, queued pages) ->
research -> mark until nothing more can be retrieved, a paid API's budget is
spent (`--api-budget`), the site throttles or a round changes nothing; it
exits 0 only when it ran dry and is resumed by running it again. A route that is
given up (a story gone from the site, a page that failed three times, research
that used its attempts) hands the incident to the next route - the record's
research, then one replacement search whose best citation becomes a record with
its own URL - never another page's text. The quality report shows each route's
queued / attempted / succeeded / failing (by failure type) / exhausted counts
and the age of its oldest queued item, with a trend
([docs/11-evidence-recovery.md](docs/11-evidence-recovery.md)).

**Calibrating** (`scripts/calibrate_source_gate.py`,
`calibrate_event_reality.py`, `calibrate_duplicates.py`): samples are
stratified on the model's own stored score (a census of the low band, where
any cut-off lies, plus seeded samples above it, weighted back), labelled, and
the cut-off is the largest (or, for merges, smallest) at which at least 95% of
the labelled items it would act on are correct. Labels describe the database
as it was when labelled - after rejections and merges the labelled items no
longer exist as active events or pairs - so recalibration runs on the backup
taken at labelling time (`scripts/prepare_calibration_snapshot.py`, which
carries stored research over); the snapshot used is recorded.

**What each step fixed**:

- *Source gate.* Google matches results on a snippet, which picks up incident
  words from menus, footers and policies; enrichment then attached the
  organisation's real incident to the page. Service NSW's "Report identity
  theft" help page became a second Service NSW attack weeks after its real
  March 2020 breach; LUSH's privacy policy became LUSH's breach. Applied: 469
  of 1,074 Google records removed (65 events left with no evidence, 404 junk
  records split out of events that keep other evidence). Pages with under 300
  characters of stored text are not judged; `rescrape-short-pages` re-fetches
  them (Playwright, no Perplexity substitutes; real text only, given up after 3
  failed attempts, tracked in `PageRescrapes`) so the next `check-sources`
  can. Where a removed record pointed at
  a real incident no other event covers, a targeted search queues proper
  reporting for normal ingestion (80 queued; 54 became events' evidence on the
  next pass).
- *Event reality.* Jev sees every record's page text, Perplexity research, a
  corroboration search (for events its screen scores below 0.5) and the
  victim's researched incident timeline; scores in 0.1-0.7 are re-judged by
  GPT-5.5 with reasoning. A page saying what an event is not is no evidence the
  event did not happen - for real incidents the stored page is often a privacy
  policy - so judging events, not pages, is what made rejection safe: on stored
  evidence alone no cut-off reached 95% precision (0.78 at best).
- *Victims.* When no record names the event's victim and two thirds name one
  other organisation, that organisation becomes the victim (guarded against
  legal and parent names, suppliers, products and regulators). Fixed TPG's
  breach filed under Optus, Henning Harders under Toll, Sharp Office Systems
  under Sharp Motor Group. Canonicalisation no longer folds names sharing a
  brand word across lines of business.
- *Duplicates.* Every pair of events sharing a victim, with both events'
  evidence, the gap in days, **how each date was determined** (stated,
  researched, publication or discovery date; day, month or year precision -
  `dedup/date_provenance.py`) and the organisation's timeline; 0.2-0.8
  escalated. Merged ACT Government's help pages into its June 2023 Barracuda
  breach and Services Australia's notice into Spectrum Medical Imaging's
  breach; the older GPT recurrence check had proposed those at 0.55 and 0.78,
  under its unmeasured 0.85 bar.

### Investigation tier: organisation timelines and escalation

Judging an event or a pair in isolation cannot answer what decides it: how
many incidents did this organisation actually have, and when? The labellers
answered that by searching - Western Sydney University had four separate
incidents (2023, August 2024, January 2025, June 2025).

- **Timelines** (`dedup/org_timeline.py`): one Perplexity search per
  organisation with several events, for its full incident history - dated,
  with what happened, how it became public and a source. Stored in
  `OrganisationTimelines` (a live search cannot be replayed) and shown to Jev
  for reality, duplicate and victim judgements. **Kept current**: a timeline is
  re-researched whenever a record about the organisation arrives after it was
  researched (a timeline that predates an incident does not list it, and the
  judgements treat "matches nothing in the researched history" as evidence
  against an event). Every version is kept; the latest is used; a new version
  changes the judgements' input, so affected events and pairs are re-judged
  automatically.
- **Escalation** (`utils/escalation.py`): cases Jev scores in a fixed middle
  band go to the strong OpenAI model with reasoning on, over the same evidence;
  its probability replaces Jev's and is stored with its rationale.
- **Reconciliation** (`dedup/timeline_reconciliation.py`,
  `reconcile-timelines`): matches each event to its organisation's timeline
  and resolves what disagrees - wrong-organisation events through the
  calibrated victim attribution, not-an-incident events through the calibrated
  event-reality filter, date conflicts by calibrated re-dating (`--apply`),
  incidents with no event by `--queue-gaps`; timeline gaps stay a report. Its
  verdicts appear on the timeline dashboard.
- **Timeline question v2**: aliases must be spellings of the same name (a
  misfiled record once put "Aussie Fluid Power" into the AFP's question, and
  the timeline then vouched for the misattribution); multinationals and
  declined questions are asked about Australia only; supplier breaches get
  their own instruction. A failed refresh is recorded and raises, and
  judgements are told when a timeline may be incomplete. Details:
  [docs/10-victim-attribution-and-timelines.md](docs/10-victim-attribution-and-timelines.md).

Every Jev and escalation answer is stored in `ModelJudgements` with the
question as sent, a hash of the input, the version that answered and the full
distribution, and reused unless the question, the input or the model changes.

### Supplier incidents: every affected organisation, one incident

A supplier breach (ProctorU 2020 and ten universities; Frontier Software's
payroll; Canvas/Instructure; HWL Ebsworth's clients) is one incident with
several co-equal victims. `find-supplier-victims` detects supplier incidents
(a vendor, or text describing a third-party compromise), gathers candidate
victims from the records' links, Perplexity's research and - with
`--research` - one stored search per incident, and finds other events that are
the same incident under another customer. Jev judges each (middle band
escalated; a failed escalation leaves the item undecided). With `--apply` and a
calibration for the current models and questions it merges those events
through the ledger and attaches the accepted customers as `victim` roles, with
an audit row each. A victim that has its own separate event near the incident
is decided by whether that pair has been *ruled on*: ruled the **same** incident
(a pair label, a `same` override, or the calibrated same-incident judgement at
its cut-off) it is merged into the supplier incident through the ledger, which
carries the customer across; ruled a **different** incident (a label, a
`different` override, or the calibrated judgement below its own `different_cutoff`)
the customer is attached as a victim of the supplier incident as well, and the
findings record which ruling allowed it; only an **unresolved** pair is held,
and the report says why (no judgement of the pair, a failed or un-escalated
one, no cut-off, or a label and an override that disagree). A label or override
outranks the model; two that disagree are held, never picked between. The
merge cut-off measured only the precision of merging, so a score just under it
is not evidence of "different": the calibration records a separate
`different_cutoff` (the largest score below which at least 95% of labelled
pairs, weighted, are labelled different and none is labelled the same), and a
pair scoring between the two, or any model-only pair when a calibration has no
such bound, is held with the band named. On the 2026-10-29 recalibration it is
0.09 against a 0.89 merge cut-off (`calibrate_supplier_victims.py calibrate
--different-only` adds it to an existing calibration from the stored judgements,
no API calls).

```bash
python scripts/dedup_admin.py find-supplier-victims --research            # report
python scripts/dedup_admin.py find-supplier-victims --research --apply    # act (calibrated)
python scripts/calibrate_supplier_victims.py sample | packet --out DIR | calibrate
python scripts/dedup_admin.py recalibrate --step supplier-victims         # after an evidence change
```

Calibrated 2026-10-27 on 187 blind-labelled items
(`evaluation/supplier_victim_labels.csv`), and **recalibrated 2026-10-29 on
current evidence** (the evidence fingerprints of `dedup/calibration_guard.py`
made the first calibration inapplicable): same-incident merges at **0.89**
(27/27 labelled merges right, recall 0.75, bootstrap mean 0.95 / p05 0.79),
co-victims at **0.87** (25/26, weighted precision 0.96, recall 0.58, bootstrap
mean 0.94 / p05 0.76 - the first calibration reached only 0.96 with recall 0.11),
and "different" below **0.09**. The recalibration scored 70 of the 103 labelled
victim candidates and 69 of the 71 labelled pairs: the other 33 victims were
named by a Perplexity search made on 2026-10-27 that was never stored and cannot
be replayed. `recalibrate` re-scores them on `instance/calibration_supplier_victims.db`
(a backup taken before the labelled events changed) with the live database's
stored searches, and refuses below 65% coverage. `--apply` refuses (exit 2)
without readable labels and a matching calibration.

The analysis and the by-entity dashboard count a multi-victim incident once per
victim organisation and never twice for one organisation stored under two
names (`dedup/victim_counting.py`). Distinct affected organisations stay
distinct - no shared word ("university", "sydney") and no shared acronym joins
them - while two reports of the incident under different spellings of one
customer link. An acronym is a candidate, not proof: "AP" fits both Advance
Press and Associated Press, so it is the same organisation as either only
through a confirmed global alias or this event's own stored resolution
(`EntityMentionResolutions`); otherwise it stays unresolved and distinct. An
acronym that fits exactly one organisation and conflicts with no alias still
resolves without a model. On the 2026-10-29 copy no count changed (893
organisations, 69 with a repeat, 123 repeats). Detail:
[docs/10-supplier-incidents.md](docs/10-supplier-incidents.md).

### Stray records: event coherence

The opposite of a missed merge: an event holding records about a *different*
incident. "Sharp Motor Group" held 18 records of which 5 were about it - 9 were
unrelated Cyber Daily articles and 4 were about another company (Sharp Office
Systems) - and took its date from a stray Partnered Health record. Strays
distort dates and counts and pull every later duplicate check toward wrong
matches.

```bash
python scripts/dedup_admin.py check-coherence                    # report
python scripts/dedup_admin.py check-coherence --apply            # split out (logged, reversible)
python scripts/dedup_admin.py check-coherence --event <dedup_id>
```

A member is a *suspect* when the organisation Perplexity named for it shares no
distinctive name key with the event's victim, vendor or majority organisation.
Jev answers, for each suspect, *is this record about the same incident as the
event?* from the record's title, summary, Perplexity research and stored page,
the event's victim and other records, and the victim's researched timeline;
0.1-0.7 is escalated to GPT-5.5 with reasoning. With `--apply` a suspect below
the calibrated cut-off (**0.15**, 17/17 correct on the labelled census of
suspects, recall 0.94) is split out through the ledger - snapshotted,
reversible, cached as `different` rulings - and strays about one incident leave
together. Labels are ground truth: `same` never leaves, `different` always does
(all 25 labelled strays - WSU's WA Health records, UTas's University of Sydney
and Canvas records, NT Health's Updoc record ... - leave on the first run).
Guards kept from past wrong splits: same-organisation members never leave on a
model score *here* (misdated copies; see the next section for the separate,
calibrated check), an organisation named in the event's title stays
(youX / Vroom by YouX), a majority of strays naming one organisation means the
event is mislabelled, and the question states the supplier-customer rule.
Split-off records are re-homed by `check-duplicates`, which runs next. No
fallback: missing keys, a Jev failure or a missing/unreadable labels or
calibration file under `--apply` exit non-zero; a failed escalation leaves its
member undecided (not acted on) and exits non-zero. Findings:
`dedup_coherence_findings.json` beside the database. Details, including what the
suspect gate misses: [docs/09-deduplication-v3.md section 8j](docs/09-deduplication-v3.md).

### Separate incidents merged under one organisation

The stray check cannot see the opposite defect: two real incidents at one
organisation joined into one event, because every member names the same
organisation. The 2026-10-28 recurrence check did exactly that to two pairs
labelled *different* (Western Sydney University's August 2024 and January 2025
breaches; youX's March 2025 S3 exposure and February 2026 MongoDB theft).

```bash
python scripts/dedup_admin.py check-same-organisation              # report
python scripts/dedup_admin.py check-same-organisation --apply      # split (only once calibrated; labelled always)
# on the labelling snapshot: run check-same-organisation first (it stores each candidate's score), then
python scripts/calibrate_coherence_same_org.py --db <snapshot> sample | packet --out DIR | judge | calibrate
```

A member naming the event's own organisation becomes a *candidate* only on
evidence of a different incident, **never a date gap by itself**: its incident
date is verified (stated in the record or researched - not a publication or
discovery date) and lies more than 90 days from the event's date interval, or
it matches a different incident than the event does in the organisation's
researched timeline. Jev judges each candidate with a versioned variant of the
stray question (`member-coherence-sameorg-v1`) that states the flags are hints,
not evidence, and that follow-ups and misdated copies are the same incident;
0.1-0.7 is escalated to GPT-5.5. **Splitting needs its own calibrated cut-off**
on labelled same-organisation cases (`evaluation/coherence_same_org_labels.csv`,
`..._calibration.json`, with the evidence fingerprint); until those exist the
step only reports (`dedup_coherence_same_org_findings.json`) and splits members
labelled `different`. Guards: labelled `same` never leaves; the departing
records must be fewer than those that stay (else the event is held for review);
a split goes through the ledger (snapshotted, reversible, `different` rulings
cached) with records leaving together when within 90 days of each other. On the
2026-10-29 live copy it finds 196 candidates in 82 events, including both
contradicted pairs' records above. Labelling convention: `different` only when
the record's own content shows a separate incident, `same` for any first report,
follow-up, notification, regulator statement or class action about the event's
incident, even with a wrong date.

**Labelled-different pairs are never merged, and merged ones are undone.**
`DedupLedger.merge_events` now refuses (`ValueError`, like a cached ruling) to
join two events when a pair labelled `different` in
`evaluation/duplicate_labels.csv` resolves to one on each side, so the GPT
recurrence check, `consolidate`, supplier merges and the rest respect labels
(`merge --force` overrules). `resolve-label-contradictions` repairs the ones
that were merged before (the quality gate's `labels.duplicates.contradictions`):
it reads which records belonged to each labelled event from the labelling-time
snapshot (`duplicates` in `evaluation/calibration_snapshots.json`), and the
smaller side leaves together through the ledger, with `different` rulings
between the two sides. A side with no active record left, or no snapshot, is
listed as unresolved, not guessed. The refresh runs it after the merging steps.
On the live copy it splits 2 records out of the WSU event and 11 out of the
youX event; records misattributed to a labelled event travel with it and are
re-homed by `check-same-organisation` and `check-duplicates`.

### Incidents with no victim: `fill-victims`, proper names, learned aliases, entity profiles

On 2026-10-29, 186 of 1,090 active events had no `victim` role (and 16 only a placeholder). Victim attribution
judges *recorded* event-victim pairs, so an event with none was never looked at; and role classification was shown
a blank "detail" for 97% of events (`DeduplicatedEvents.summary` is empty on 1,063 of them), told to "prefer
bystander over guessing", and labelled every entity of the OpenAI-agent coverage a bystander.

```bash
python scripts/dedup_admin.py judge-names [--dry-run]       # is a stored name the name of one organisation?
python scripts/dedup_admin.py fill-victims [--apply] [--dry-run] [--event ID]
python scripts/dedup_admin.py entity-profile research | show <name> | set <name> <kind> <value> --reason "..."
python scripts/dedup_admin.py roles-status                  # now also: older role prompt, NO VICTIM
```

* **Role classification** (`entity_classification.ROLES_PROMPT_VERSION = entity-roles-v2`): the classifier is shown
  the members' descriptions when the event has no summary, is told an incident has a victim (a target named only by
  the service or programme it runs is the organisation that runs it) and that the developer of an agent that broke in
  is the actor. Events classified under an older prompt that still have no victim are classified again
  (`roles_prompt_version`). An incident left with no victim is **not a finished classification**: it is counted
  (`roles-status`, `staleness.roles_no_victim` in the quality report) and taken up by `fill-victims`.
* **`fill-victims`** (`dedup/victim_fill.py`) gives each such event the organisation its evidence names. Candidates:
  each record's Perplexity victim, every linked organisation whatever its role, a usable scalar victim, the
  organisations the event's **text** names (`dedup/event_organisations.py`: the fast model lists names, kept only
  if they occur verbatim in the text it read and are usable; stored by input hash; a failure is an error for that
  event, never an empty list) and the organisation a linked *product* is named after, folded through the alias
  tables and without the parties that did the attacking (`threat_actor` role, an attacker field naming the same
  organisation, or an actor the text names). Jev chooses (`fill_victim`); a leading option under 0.9 is re-judged by
  the reasoning model. Adoption is `victim_agreement.apply_victim` (ledger snapshot, `VictimFillActions`), only at
  the cut-off calibrated by `scripts/calibrate_victim_fill.py` on blind labels
  (`evaluation/victim_fill_labels.csv`: `deduplicated_event_id,label,correct_victim,rationale,labeller`; label
  `named` + the victim, `none_named` or `unsure`) and only when the records' text names it. Without a calibration
  only labelled events are filled. It is a separate step from attribution because its cut-off is a different
  question over a different population; `check-victim-attribution` uses the same candidates (`EVIDENCE_VERSION`
  `victim-evidence-v2`: its calibration needs `recalibrate`, a cheap re-run), and a recorded victim an attacker field
  names carries a note to the judgement.
* **Proper names** (`dedup/name_judgement.py`). Whether "government agency in Australia" or "Australian Government
  Department" is a description rather than a name is **not** decided by a word list or by capitalisation: Jev gives
  P(this identifies one specific organisation) for the name and the sentence it came from (`judge-names`, stored,
  asked once). Names below the cut-off calibrated by `scripts/calibrate_names.py` (labels
  `evaluation/name_labels.csv`: `name,label,rationale,labeller`; `named` / `descriptive` / `unsure`) are
  descriptions and can be neither a victim nor a candidate; with no calibration only names labelled `descriptive` are.
* **Alias learning** (`dedup/alias_learning.py`, in `canonicalise-entities`). Besides name structure, proposals come
  from evidence: a name that is a *known variant plus a qualifier* ("Australian Medicare" against the stored alias
  "Medicare", which no longer exists as an entity to compare with), and names two records of one event each give as
  the victim. Every proposal goes through the same Perplexity verification (`same` / `parent_subsidiary` / `related` /
  `different` / `unsure`); only `same` folds, stored in `EntityAliases` (`llm`) and asked once per pair. The decision
  cache now also records the evidence hash (`EntityAliasDecisions.evidence_hash`) and the model: a model's decision
  is asked again when the incidents or profiles it was shown change **and the pair matters now** (it is proposed for a
  fold, or it vetoes one); a decision made by another model is not reused, and a person's (`human`) is never asked
  again. See "Alias decisions are tied to their evidence" below.
  A product or system named after an organisation is never folded; it offers the organisation as a victim candidate.
* **Entity profiles** (`dedup/entity_profiles.py`, `EntityProfiles`): jurisdiction, sector, type, administering
  organisation, services run, data held, other names, website. `human` rows (`entity-profile set ... --reason`) are never
  replaced by `llm` rows (one Perplexity lookup per organisation, citations kept, asked again only when the
  organisation's evidence changes). Shown to Jev with the candidates of an acronym, of a victim choice and the two
  names of an alias question; used to rank which alias pairs to ask about first. Evidence, never a matcher.
* **Mentions** (`contextual_aliases.locally_named`): an acronym is placed by a full name in the *same sentence* ("Australian
  Federal Police (AFP)"), not by one elsewhere in the article ("Photograph: AFP"); anything else is for the cached
  judgement.
* **Acceptance cases** (`evaluation/review_acceptance_cases.csv`): the quality report measures
  `acceptance.victim.failures` and `acceptance.same_incident.failures` (a warning, never a gate).

### Cluster-level adjudication

Pairwise comparison cannot see a cluster. When one breach generates a long tail
of coverage (incident report, regulator statement, class action, "government
concludes response"), each article emphasises something different, so pair by
pair an LLM keeps answering "different incident" - consistently, so re-checking
pairs does not help. HWL Ebsworth fragmented into 19 separate events.

`adjudicate-clusters` shows the model every record in a cluster at once and
asks for a **partition**, which is internally consistent by construction and
costs one call per cluster instead of N(N-1)/2.

```bash
python scripts/dedup_admin.py adjudicate-clusters --dry-run
python scripts/dedup_admin.py adjudicate-clusters --entity "Ebsworth"
python scripts/dedup_admin.py adjudicate-clusters --min-certainty 0.8
```

Nothing is applied unless the partition covers every record exactly once;
invalid partitions and LLM failures degrade to "change nothing" (an answer that
was not obtained is never read as an answer that keeps records apart). See
[docs/09-deduplication-v3.md](docs/09-deduplication-v3.md).

### Matching across large incident groups

The prompt loses track of indices past about 20 records, so a group of more
than that cannot be shown whole. It used to be skipped (a bucket of more than
twice the limit) or cut into **disjoint slices**, and two reports of one
supplier incident on opposite sides of a cut were never judged together.
`adjudicate-candidates` (the routine step), `adjudicate-clusters` and
`reconcile-entities` now cover such a group with bounded, overlapping
**neighbourhoods** (`dedup/neighbourhoods.py`):

1. *Links.* Events are linked by what shows they are one incident, scored by the
   duplicate ranking (`candidate_ranking.score_pair`): a shared non-hub article,
   the same supplier (reports of one supplier breach name a different customer
   each time, so events recording the same supplier are linked even with no
   shared article or word), corpus-rare words they share, an equal record count
   or attack method, and how close their date intervals are.
2. *Neighbourhoods.* A linked group of at most `--max-cluster-size` (20) events
   is one neighbourhood, as before. A larger one is covered by neighbourhoods
   that each take an event, its `--neighbours` (8) strongest links, and the
   events most strongly linked to those until the prompt is full, chosen
   greedily so that every strong link is inside at least one of them. Nothing is
   skipped for size.
3. *Budget.* At most `--call-budget` (250) model calls are made a run; stored
   answers (`AdjudicationCache`) are free and do not count. Neighbourhoods are
   asked in order of the links they settle; what the budget cuts is counted and
   printed (`CALL BUDGET SPENT: n neighbourhood(s) were not asked`) and the next
   run continues with the cached answers free.
4. *Reconciliation.* Two events are joined only when some neighbourhood shows
   them together at the certainty bar, **no** neighbourhood showing both
   separates them, and no human `different` ruling or `duplicate_labels.csv`
   `different` label forbids it. Incidents are built strongest link first (most
   neighbourhoods agreeing, then highest certainty) and two partial incidents
   are unioned only if no pair across them is separated or forbidden, so a shared
   event goes to the consistent side or stays out. An incident's certainty is the
   lowest on the links that built it.
5. *Victims.* A merge keeps every customer victim recorded on the event merged
   away (`supplier_victims.carry_victims`, audited in
   `SupplierVictimAttachments`): not the supplier itself, not a description, not
   another name for a victim the incident already has.

Victims are kept: applying the 35 merges the supplier-keyed stand-in proposes
on a copy (1,090 -> 1,055 active events) left the active events with the same
**910** distinct victim organisations (37 customer victims carried across),
where the same merges without carrying left **878** - 32 customers dropped from
every victim count.

Nothing is calibrated here (a partition's certainty is the model's own number),
so the routine step stays report-only (`--dry-run` unless `--missed-merge-apply`)
and a human confirms with `dedup_admin.py merge`. `--grouping disjoint` restores
the earlier slicing for comparison. The plan and the reconciliation (group sizes,
neighbourhoods, links covered, contradicted and blocked links, refused unions)
are written to `instance/dedup_candidate_neighbourhoods.json`.

Measured on a copy of the 2026-10-29 database (1,090 active events; the 692
strong candidate links that need no embeddings: a shared non-hub article or a
corpus-rare shared subject word; no model - `scripts/measure_neighbourhoods.py`
partitions what it is shown by a known key, so only the planning and
reconciliation are measured):

| | Disjoint slices (old) | Neighbourhoods |
|---|---|---|
| Linked groups | 81 over 637 events; one of **420** events (66%), the next 7 | the same |
| Shown to the model | 127 slices of at most 20: 127 calls | 200 neighbourhoods (80 whole groups, 120 overlapping, largest 20): 200 calls, inside the 250 budget |
| Strong links shown together | 541 of 692 (**151 cut**) | **692 of 692** |
| Groups of events recording one supplier (10 with two or more linked events), fully recovered | 1 (0 of the 9 that cross a slice) | 3 (2 of the 9 that cross a slice) |
| The same, with the supplier clue as a link (19 groups) | 0 | **19** |
| Groups of events chained by shared articles (32) | 31 | 32 (the one that crosses a slice) |
| Incidents joining events of different keys | - | 0 |

Without the supplier clue 5 of the 10 supplier groups have members in different
linked groups (nothing links them but the supplier), and 2 more sit three or
more links apart inside the 420-event group, so no neighbourhood shows both;
linking the events that record the same supplier (`supplier_pairs`, on by
default, `--no-supplier-links` to drop it) adds 71 links and joins them. A
full cover costs about 1.6 times the calls of the old slicing; the budget
bounds it, not the group size.

```bash
python scripts/dedup_admin.py adjudicate-candidates --dry-run --verbose              # neighbourhoods (default)
python scripts/dedup_admin.py adjudicate-candidates --dry-run --grouping disjoint    # the earlier slicing
python scripts/dedup_admin.py adjudicate-candidates --call-budget 100 --neighbours 6
python scripts/measure_neighbourhoods.py --db <copy.db> --key vendor|source [--supplier-links]
```

### Repeat attacks inside 90 days, re-checked

An organisation attacked again a few weeks after the last time is the shape a
*reporting lag* produces, not usually a second attack. The follow-up article,
the regulator's notification and the class action all describe the first breach
but carry their own publication dates; where the extraction takes the
publication date as the incident date, one breach becomes two events dated
weeks apart.

Short gaps are where this is both most likely (reporting lag is measured in
weeks) and most damaging: the short-elapsed-time band is what
`analyze_recurrent_timing.py` estimates its recurrence hazard from, so a false
duplicate there does not add noise — it manufactures the signal.

```bash
python scripts/dedup_admin.py check-recurrences --dry-run --verbose
python scripts/dedup_admin.py check-recurrences                  # apply
python scripts/dedup_admin.py check-recurrences --window-days 120
python scripts/dedup_admin.py check-recurrences --entity "Optus"
```

Neither existing pass covers this: `consolidate` needs the *same date*, and
`adjudicate-clusters` only compares what its blocking put in one cluster. Each
organisation's events are chained while every consecutive gap is under the
window and the whole **run** is partitioned in one call, so the verdicts cannot
contradict each other the way N pairwise answers can.

Merging two genuinely separate incidents is the worse error, so the adjudicator
is shown the distinguishing evidence explicitly (attack method, record count,
source URLs, the gap in days), merges only at or above `--min-certainty`
(default 0.85), and is vetoed outright by an existing human `different`
override. On the live database this found 8 short-gap runs, merged 3 false
repeats (MIFF, Swinburne, Notre Dame) and left the genuine ones alone —
including Optus's September and December 2022 events, 86 days apart. Findings
are written to `instance/recurrence_findings.json`; the check runs
automatically after every deduplication (`--skip-recurrence-check` to opt out).

**Gaps are measured between date intervals, not point dates.** An event stored on
the 1st of a month may have happened on any day of it. Each event carries its
verified `event_date_earliest` / `event_date_latest`; the gap between two events
is the distance between their intervals and is **0 when they overlap** (two
events in one verified month, or a month and a day inside it), a run is chained
on that gap, and the prompt shows each date with its precision ("month
precision: any day from 2024-06-01 to 2024-06-30", "exact day", "precision
unverified") instead of presenting a month as a day. Findings report the
interval gap and how many of a run's dates are unverified. An event whose
precision is unverified has no interval: by default (`--unverified-dates stored`
/ `--recurrence-unverified-dates stored`) its stored date stands in as the one
date held for it and is reported as unverified, because most events are in that
state (752 of 1,090 active events on 2026-10-29) and excluding them would drop 10 of the 12
consecutive short-gap pairs; `exclude` leaves them out. An undated event never
takes part. `RecurrenceEvent.gap_to` is the same interval gap, so the duplicate
check's `gap_days` follows. Measured on the 2026-10-29 copy: of 211 dated
same-organisation pairs the gap differs (is smaller) for 43, none of them across
the 90-day window, so the candidate runs are unchanged at 90, 120 and 180 days
(11, 15 and 22 runs).

### Entity size: SMALL, MEDIUM, LARGE, HUGE, UNKNOWN

`EntitiesV2.employee_count` and `turnover` have existed since v1 and were NULL
on **every one of 3,133 rows**, so any analysis asking how big the victim was
ran with one "unknown" level for the whole dataset. An exact headcount is
unobtainable for most of these organisations; the band is not.

| Band | Definition |
|---|---|
| `SMALL` | fewer than 20 employees, or under A$10m revenue |
| `MEDIUM` | 20–199 employees, or A$10m–100m |
| `LARGE` | 200–4,999 employees, or A$100m–1b |
| `HUGE` | 5,000+ employees or over A$1b; also national/state departments, the major universities, ASX-100 |
| `UNKNOWN` | not identifiable, or not an organisation at all |

The three-way cut is the **ABS definition of business size** (small 0–19,
medium 20–199, large 200+); `LARGE` is split at 5,000 because that band
otherwise runs from a mid-sized firm to Telstra.

```bash
python scripts/dedup_admin.py size-entities --dry-run --verbose
python scripts/dedup_admin.py size-entities                  # backfill
python scripts/dedup_admin.py size-entities --entity "Optus" --refresh
```

Perplexity looks each organisation up online — website, annual report,
LinkedIn, ABN/ASIC, news — and the band is derived from what it finds, with the
citations kept in `size_sources` so an estimate can be traced back to its
evidence. Asking an LLM from memory instead would confidently size the many
small Australian businesses it has never heard of. Where the answer reports a
figure and a band that disagree ("about 45,000 staff … LARGE"), the figure
decides.

`UNKNOWN` is an answer, not a failure: a product, a person, a ransomware group
and a collective noun ("Australian hospitals") have no organisational size, and
those are settled by rule at no API cost — about 44% of the table. A row whose
research could not be *reached* is marked `unavailable` and retried next run, so
an outage never hardens into a settled UNKNOWN. A `human` method is never
overwritten. Incremental by default, and run automatically after each
deduplication (`--skip-entity-sizing` to opt out).

### Other deduplication repair commands

```bash
# Rule 1: same entity + same date is one incident (deterministic, no LLM)
python scripts/dedup_admin.py consolidate --dry-run
python scripts/dedup_admin.py consolidate --date-tolerance 3

# Give borderline (0.80-0.89) findings a final disposition
python scripts/dedup_admin.py resolve-band --dry-run

# Re-examine splits that separated records about the same organisation
python scripts/dedup_admin.py recheck-splits --dry-run
```

### Enrichment acceptance and evidence

Classification and Perplexity enrichment read the **persisted** record
(`RawEvents.raw_content`, written by scraping) through one path,
`processing/record_evidence.py`, not the snippet-bearing objects built at
discovery. A record with no usable scraped text (under 200 characters) is
passed on explicitly labelled as snippet-only, and its confidence is capped.
A record rejected by a filter (blocked domain, or the Jev page gate after
scraping) is marked processed with `Rejected at <stage>` and goes no
further, in this run or later ones.

Perplexity answers pass `processing/enrichment_gate.py` before anything is
written: ISO dates not after discovery, confidences in [0, 1], non-negative
integer counts, and facts that are attributable to a cited source or to the
record's own text. A failed or empty answer is not marked validated and gets no
reliability; it is retried on later runs and quarantined after 3 attempts. A
weaker answer never overwrites a stronger stored fact. See
[docs/05-event-enrichment.md](docs/05-event-enrichment.md).

```bash
python scripts/perplexity_backfill_events.py --report-quarantine
python scripts/perplexity_backfill_events.py --release-quarantine
python scripts/perplexity_backfill_events.py --requeue-empty   # old empty "validated" rows
```

#### One active enrichment per record, enforced by the database

`create_enriched_event` used to look for an active enrichment, release its
lock, and take it again to insert, so two workers (threads, or processes with
their own connection) could both find none and both insert: two events from one
article. Creation is now one statement (`INSERT ... WHERE NOT EXISTS`, under one
lock) that returns the existing enrichment's id when there is one, and the
database backs it with a trigger (`trg_enriched_one_active_per_record`,
`storage/enrichment_uniqueness.py`, installed by `pipeline.py init` and on every
connection) that aborts the insert of an *active* enrichment for a record that
already has one. It is a trigger on `INSERT`, not a partial unique index on
`EnrichedEvents(raw_event_id) WHERE status = 'Active'`, because the
record-identity repair (`processing/record_identity.py`) moves enrichments
between records with an `UPDATE` of `raw_event_id`: a record that holds an
enrichment known to be about another article keeps it (reported as
`left_with_two`) while the one that belongs to it is attached, and a chain of
moves passes through states a unique index forbids. The invariant the pipeline
owns is that ingest never creates a second one.

Records that already hold two (none on the 2026-10-29 copy: 0 of 3,627 active
enrichments) are never repaired silently: `init` logs them at ERROR, and
`python scripts/check_data_integrity.py` fails
(`active-enrichment-unique-per-record`) for any pair that no `RecordIdentityChecks`
finding explains.

#### Citations must support the facts

A cited URL is not support until its page has been read. The gate retrieves
every cited page (cached in `CitedDocuments`, failures too) and accepts a fact
only if the record's own text, or a cited page naming an organisation the
record names, states it: a date to the precision claimed and not merely as a
page's publication date, a count with its unit, basis (stated / estimate /
at least) and scope (Australian / global). Unsupported claims are withheld
from the typed columns and the JSON (kept in `EnrichmentFactSupport` with the
reason; accepted facts keep their source there), and a confident one keeps
the record in the retry queue. `--recompute-reliability` re-judges every
stored answer the same way, replacing the old constant 0.85 reliability;
idempotent. The Perplexity model is `PERPLEXITY_MODEL` (default `sonar-pro`).
Details: [docs/05-event-enrichment.md](docs/05-event-enrichment.md#citation-support).

```bash
python scripts/perplexity_backfill_events.py --recompute-reliability --dry-run   # report
python scripts/perplexity_backfill_events.py --recompute-reliability             # apply
```

#### Cited pages are checked again

A page read once used to be treated as permanently fresh, so a corrected
notice or an updated victim list at the same URL stayed invisible to every
fact and event that rested on it. `refresh-cited-documents` (the third step of
the refresh hygiene phase, after the short-page re-scrape; `--cited-refresh-limit`,
default 25 pages a run, 0 for none) checks pages again:

- **Which.** Only pages cited by a record of an *active* event. A page is due
  when it was last checked longer ago than its interval: 14 days for a page that
  changed before or was itself published in the last 60 days, 45 days for a quiet
  one, doubling with each consecutive check that found nothing (to 180 days).
  Due pages are ranked by what depends on them - a citing record has a withheld
  fact (a correction may now support it) +4, an unresolved victim attribution +3,
  partial or unsupported evidence +3, an incident in the last 180 days +2, the
  page changed before +2, how overdue, how many events cite it - and the first
  `--limit` are checked.
- **How.** A conditional request with the stored `ETag` / `Last-Modified`
  (`If-None-Match` / `If-Modified-Since`; a 304 changes nothing). A server that
  sends no validators, or ignores them, sends the page again and its content
  fingerprint (SHA-256 of the extracted text, whitespace-insensitive) is compared
  with the stored one.
- **Versions.** A changed page replaces the current row in `CitedDocuments`
  (`version` + 1, new text, fingerprint, validators, fetch time) and the old
  version moves to `CitedDocumentVersions` with its fingerprint and fetch time. A
  check that fails (network error, HTTP error, no usable text) records the
  failure and keeps the page that was read: an outage must not remove the
  support facts rest on.
- **What reruns.** The records that cite a changed page are judged again by the
  acceptance gate against the cached pages (no fetching, no model): fact support
  (`EnrichmentFactSupport`), typed columns and enrichment JSON are rewritten
  through the same path as `--recompute-reliability`. A record whose enrichment
  changed then changes its fingerprint (`RecordFingerprints`), which refreshes
  the events built on it (title, date, master-copied fields, facts; roles marked
  outdated), and Jev judgements keyed on a hash of their evidence are asked again
  only for those events. A page that changed in a way that alters no fact costs
  nothing downstream. A record citing a page that was never cached is deferred,
  not judged on a missing page.

Measured on a copy of the 2026-10-29 database: 1,484 cached pages, 1,225 read,
**1,171 cited by an active event's record** (54 read pages nothing live rests on
are never checked). All were fetched on 2026-10-28, so 0 are due at 13 days, 94
(those published in the last 60 days) at 14 days, and all 1,171 at 46 days; the
25-page budget cycles them in 47 runs, withheld-fact, flagged-attribution and
weak-evidence pages first (the first 25 at day 46: 25 withheld-fact, 25
weak-evidence, 16 flagged, 12 recent-incident). No page stores validators yet;
they are recorded on the first check.

```bash
python scripts/dedup_admin.py refresh-cited-documents --dry-run --verbose      # what is due, and why
python scripts/dedup_admin.py refresh-cited-documents --dry-run --as-of 2026-12-15
python scripts/dedup_admin.py refresh-cited-documents --limit 25              # check, store, rejudge
```

### Discovery filter and retry queue

The Random Forest filter is **retired** (it dropped real incidents about as
often as junk: precision 0.53 on organisation-separated labels). Discovery now
drops nothing before scraping; after scraping, Jev judges each new page with
the calibrated source-gate question and rejects pages below its cut-off
(0.58) **only for Google**, where it was calibrated. Other sources are scored
and kept: on the event labels the same cut-off would drop real incidents
(Perplexity lower-bound precision 0.36, `evaluation/page_gate_source_evaluation.json`).
Every run first drains a durable retry queue of stored-but-unfinished records
(temporary failures retried with back-off forever, *recognised* permanent ones
quarantined after 3, unrecognised ones retried with a long back-off and never
quarantined), and a month whose search lost results is searched again. No TypeSafe
or OpenAI key: discovery stops, it does not fall back. Details:
[docs/04-event-filtering.md](docs/04-event-filtering.md) section 3.0 and
[docs/02-discovery-collection.md](docs/02-discovery-collection.md) section 7.

```bash
python scripts/discovery_queue.py status    # incomplete source-months, quarantined records, parked months, probes
python scripts/discovery_queue.py reopen --dry-run   # parked months whose source key / query / client version changed
python scripts/discovery_queue.py release   # return quarantined records and permanently given-up months to the queue
python scripts/discovery_queue.py backfill --source CyberDaily --months 12   # a source added later, over processed months
```

**Three kinds of failure, none assumed permanent.** The classifier used to call
every exception it did not recognise *permanent*, so an unexpected library error
or a code bug quarantined good records after three tries, and a source-month that
failed five times for any reason was given up until a person ran `release`. Now:

| Class | Recognised as | Record | Source-month search |
|---|---|---|---|
| permanent | a validation failure of the record's content (`ValueError`, a pydantic `ValidationError`, malformed JSON), a store integrity/data error, a value SQLite cannot bind, a request the provider refuses as invalid (400/404/410/413/414/415/422); `missing` (404/410) for a search | quarantined after 3; `release` returns it | given up at once; only `release` reopens it; never probed |
| temporary | timeouts, 429/5xx, connection errors, a locked database, API-budget refusals; `throttled`, `network`, `parse`, `incomplete` | retried with back-off forever | back-off 30 min doubling to 1 day; parked after 5 |
| unknown | anything else: an exception nothing recognises, 401/403, a message that says nothing | retried with a 6 h to 7 day back-off, logged at ERROR each time, never quarantined | back-off 6 h doubling to 7 days; parked after 5 |

A **parked** month is probed rather than abandoned (`RetryQueue.probe_due` /
`record_probe`, state in `SourceProbes`) and reopens on a successful probe or
when the configuration stored with the give-up (`give_up_config`: key hashes,
query version, client versions) differs from the current one. `status` and the
quality report (`discovery.source_months_parked`, `discovery.parked_config_changed`,
`discovery.source_probes_overdue`, `discovery.records_unknown_failures`) show
them. A month parked before configurations were recorded is searched once more.

**A failed store keeps its cause.** Storing a record, or marking it processed,
used to log the exception and return `None`, and the caller recorded every
`None` as a permanent failure: three locked-database errors quarantined a good
record. The store calls now raise `StorageError` (`processing/retry_queue.py`)
with the stage and the error text. A locked or busy database, a disk I/O error
or a full disk is the store failing, not the record: it is *temporary*, retried
with back-off and never counted toward quarantine (the same applies to a failed
write of a Perplexity enrichment). An integrity or validation error is the
record's and still quarantines after 3, with the real error in the reason. A
raw event that cannot be stored leaves its source-month recorded as failed, so
the month is searched again. Every discovery run first releases records an
earlier version quarantined for a temporary storage failure when their stored
reason says so; one quarantined with the old generic "enriched event could not
be stored" is listed by `status` and returned by `release`.

**Classifier timeouts stop the request.** The fast-model classification and
entity extraction wrapped a synchronous call in `asyncio.wait_for(asyncio.to_thread(...))`:
on timeout the await was abandoned while the thread and its HTTP request kept
running (holding a slot, spending tokens), the retry started a second request on
top, and instructor's `max_retries=2` (which retries any exception) and the OpenAI
SDK's own 2 retries multiplied the attempts. Now (`utils/llm_retry.py`) the client
has an httpx timeout (60 s per phase, 10 s to connect) and `max_retries=0`, so a
hung request is cancelled by httpx and raises `APITimeoutError`; instructor
re-asks only when the answer does not validate (one re-ask); and one loop retries
timeouts, connection errors, 429 and 5xx - one request per attempt, at most 3,
with back-off of 2 s doubling to at most 30 s. Other errors (400, 401, validation)
propagate at once. A final timeout is raised as itself, so the retry queue classes
it as temporary and the fetch-error types as `network`.

### API request budgets

`--api-budget openai=3000 perplexity=1500 typesafe=20000` (or
`API_BUDGET_OPENAI` etc. in `.env`) caps the requests each paid API may
receive in one run. Every request to those hosts reserves a unit before it is
sent - the hook is on the HTTP libraries, so threads, SDK retries and fallback
searches all count - and once a budget is spent the request is refused without
being sent. Nothing is lost: each step stores what it finished, and a refused
enrichment is not counted as a failed attempt, so the next run continues where
this one stopped. The same holds for a store that fails for a temporary cause (a
locked database, a disk I/O error): see "Discovery filter and retry queue". A run that hit a budget ends **degraded** (exit code 2). The
end-of-run log lists requests per provider. Token costs are still reported by
the token tracker; they cannot be capped directly because the GPT-5 snapshots
have no listed price.

### Dashboard safety

Dashboards embed scraped titles, organisation names and descriptions. Data is
embedded with `utils/html_safe.script_json` (a title containing `</script>`
used to end the entity dashboard's data script), links are drawn only for
http(s) URLs (`javascript:` URLs are shown as text), and recurrence reports are
escaped before Markdown conversion. Tests use hostile text in every builder.

### Records-affected sanity repair

Global vendor breaches get their worldwide total attributed to the Australian
customer caught up in them - the University of Melbourne (~50,000 students) was
recorded as losing 275,000,000 records, the global Canvas/Instructure figure,
because validation inferred the organisation from the event *title* (which
named the vendor) rather than the victim field.

```bash
python scripts/fix_records_affected.py --dry-run
python scripts/fix_records_affected.py
```

Validation is now victim-aware in both directions: an Australian-only
organisation is capped at the national population, while a genuinely global
victim (Dell, Ticketmaster) keeps its real figure. Only over-statements are
corrected - small counts are often exactly right ("6 senators confirm being
targeted").

### Confidence is unknown, not 0.7

A missing confidence used to be filled in: `0.7` for an event, `0.8` for an
entity with no score, `0.5` for a record rebuilt without one, and `0.0` by
validation (which also turned a real 0.0 into the same value). Stored, they read
as measured model output. Now:

- `ConfidenceScore.overall` is `Optional`; a missing score is stored as **NULL**
  in `EnrichedEvents`, `EnrichedEventEntities`, `EntitiesV2` and
  `DeduplicatedEvents`. A real `0.0` stays `0.0`.
- Where a score exists, `EnrichedEvents.confidence_model` and
  `confidence_version` say where it came from: `source-prior-v1` (the fixed prior
  of the data source that found the record, no model) or `classifier-adjusted-v1`
  with the fast OpenAI model that adjusted it. A NULL score has NULL provenance.
- Readers handle unknown explicitly: the collector's threshold keeps an
  unscored event (an uncalibrated cut-off may not drop records) and counts it;
  collection statistics average only scored events and report how many were not;
  the classifier does not adjust or cap a score that is not there; deduplication
  copies a master's NULL instead of writing 0.5, ranks a scored record above an
  unscored one and invents no score from the number of sources; SQL that
  orders by confidence puts NULL last (`DESC`) or first (`ASC`), i.e. as the
  lowest. The review dashboard and exports show NULL as empty.
- The quality report counts `confidence.events_unscored` (trend),
  `confidence.defaults_proven` (warns; should be 0) and
  `confidence.defaults_ambiguous` (trend).

```bash
python scripts/repair_confidence_defaults.py --dry-run   # what is provably a default, what is only suspect
python scripts/repair_confidence_defaults.py             # set the proven defaults to NULL (logged in ConfidenceRepairs)
```

The repair clears a value only when the database proves the code path that
wrote the default: `EnrichedEvents.confidence_score = 0.7` with no numeric
`confidence` in `RawEvents.source_metadata` and a successful `llm_analysis` entry in
`ProcessingLog` (the single-record path's `source_metadata.get('confidence', 0.7)`);
the 0.8 / 0.6 constants of that path's entity links; and a deduplicated event
copying a cleared master. Everything else at 0.7 or 0.8 is *ambiguous* - a
source prior plus a classifier adjustment can equal it - and is reported and left
alone. Idempotent; every cleared value is kept in `ConfidenceRepairs`. On the
2026-10-29 copy: 298 values proven (54 events, 213 entity links, 31 deduplicated
events) and 6,553 ambiguous values left.

### Entity typing: victim, vendor, and what each entity is

Counting mentions cannot tell the breached company apart from the software it
was breached through, which is how the global Canvas incident came to be
attributed to "Canvas Learning Management System" - a product. Every entity
link carried `relationship_type='affected'`, so the column said nothing.

Two levels are now recorded, because they vary independently:

| | Scope | Stored on | Values |
|---|---|---|---|
| **Kind** | Invariant | `EntitiesV2.entity_kind` | organisation, government_body, product, person, threat_actor, other |
| **Role** | Per event | `EnrichedEventEntities.relationship_type` | victim, vendor, affected_customer, regulator, threat_actor, product, bystander |

Victim remains the primary output; the vendor is recorded alongside it in
`DeduplicatedEvents.vendor_organization_name`, because a supply-chain breach is
only intelligible when both are known.

```bash
python scripts/dedup_admin.py classify-entities --dry-run
python scripts/dedup_admin.py classify-entities --entity "Canvas" --show-roles
python scripts/dedup_admin.py classify-entities
```

A role-labelled victim outranks the frequency heuristic. A product can never be
returned as the victim, invented entities are discarded, and on LLM failure the
rules apply - which never invent a victim.

Roles depend on which records an event contains, so a merge or split
invalidates them. Each event stores a signature of the members it was
classified against, so any membership change - by any route - shows up
automatically; nothing has to remember to raise a flag.

```bash
python scripts/dedup_admin.py roles-status                     # what needs refreshing
python scripts/dedup_admin.py classify-entities --stale-only   # refresh only those
python scripts/dedup_admin.py roles-status --invalidate-all    # after changing the classifier
```

Run `classify-entities --stale-only` after any merge pass (`apply-missed`,
`consolidate`, `adjudicate-clusters`). The dashboard shows a "roles stale"
badge and filter. `run_full_pipeline.py` runs it three times: early in data
hygiene, again after the steps that merge and split (`check-coherence`,
`check-duplicates`, `find-supplier-victims`), and last after the recurrence
check, followed by `check-victims` and `check-victim-attribution`
(`run_role_refresh`). Until 2026-10-28 it ran only the first time, and the later
merges left 145 events with outdated roles. If any active event still has
outdated or missing roles at the end (a failed model call), the run records an
error and ends degraded.

### Deduplication Review Dashboard

```bash
python scripts/build_dedup_dashboard.py                # -> dashboard/dedup.html
```

Per event: full ancestry (every source record, its role and outbound link), the
decisions behind it with certainty and reasoning, and copy-paste commands to
correct it. Filter by *merged but unexplained*, *certainty below 0.85*, *no
lineage*, or *human-corrected*.

### By-entity event dashboard

```bash
python scripts/build_entity_dashboard.py             # -> dashboard/entities.html
```

Every event listed under the organisation it happened to, sorted by entity then
event date, with entity / event / date / description columns. Sorting this way
makes under-deduplication visible: one incident still stored as several events
appears as consecutive rows under the same organisation, which is how the
Latitude, Medibank and Dymocks fragments were found. Tick **only entities with
more than one event** to hide every single-event organisation and leave just
those candidates.

Attribution comes from the `victim` role, not `victim_organization_name`, so an
event with several co-equal victims (the ProctorU breach has ten Australian
universities) is listed under each of them.

Each organisation carries its ordinal size band as a badge, with a filter in
the toolbar and the supporting evidence on hover — "what happens to small
businesses" is a different question from "what happens to the ASX-100", and the
page could not previously tell them apart. See
[Entity size](#entity-size-small-medium-large-huge-unknown).

### Organisation timeline dashboard

```bash
python scripts/build_timeline_dashboard.py             # -> dashboard/timelines.html (top 20)
python scripts/build_timeline_dashboard.py --top 40
```

For the organisations with the most events, the researched incident timeline
(Perplexity, `OrganisationTimelines`) beside the events the database holds,
both in date order. An incident on the left with nothing on the right is a
coverage gap; an event on the right matching nothing on the left is suspect
(junk evidence, a wrong victim or a misdated duplicate). Each card shows when
the timeline was researched and whether newer evidence has made it stale;
timelines refresh automatically when it has. Where research returned no list
("NONE FOUND", or a refusal to enumerate a multinational's incidents), the
answer is shown as given.

### Entity canonicalisation, date repair and per-entity reconciliation

Three defects compounded into most of the missed merges, because deduplication
blocks candidate pairs on the entity and treats a date gap as evidence of
separate incidents. Anything blocking never produced, no adjudicator could
recover.

```bash
python scripts/dedup_admin.py canonicalise-entities --dry-run          # proposals; nothing asked
python scripts/dedup_admin.py canonicalise-entities                    # reviewed aliases + Perplexity-confirmed folds
python scripts/dedup_admin.py canonicalise-entities --explicit-only    # reviewed aliases only

python scripts/dedup_admin.py redate --dry-run
python scripts/dedup_admin.py redate                  # member consensus + text
python scripts/dedup_admin.py redate --research       # Perplexity + GPT-4o for the rest

python scripts/dedup_admin.py reconcile-entities --dry-run
python scripts/dedup_admin.py reconcile-entities
```

**Entity spellings.** "Australian Securities and Investment Commission" and
"Australian Securities and Investments Commission" were separate rows, so three
ASIC events with byte-identical titles, dates and descriptions were never even
compared. Names that only a human would know are equivalent ("Australia" for
the Commonwealth, "CommBank"/"Commonwealth Bank") are listed in
`EXPLICIT_ALIASES` and fold directly. Every other fold - proposed by name
structure (containment, acronyms, spelling) - is **confirmed by Perplexity**
before it is applied (`dedup/entity_alias_verification.py`): both names, an
incident each appeared in, and a verdict of same / parent-subsidiary / related /
different / unsure with citations. Only `same` folds. Structure alone is not
safe: on 2026-10-28 it would have folded Marks & Spencer, Media Super and
Moody's into Mackay Sugar ("MS"), IBM into Australian Cyber Security Magazine
and a list of the four major banks into Westpac (an acronym now joins names only
when they are already one organisation, and a list of organisations is never
folded). Every verdict is stored in `EntityAliasDecisions` with the evidence it
was made on; every fold is remembered in `EntityAliases` (source `human` or `llm`) so
a future ingest resolves the variant instead of recreating it. The step runs in
every refresh; a pair Perplexity could not decide stays unfolded and the step
exits 1. Medicare is folded into Services Australia, which administers it:
coverage of the 2026 OpenAI-agent incident filed it under both.

**Alias decisions are tied to their evidence.** A settled verdict used to be
reused for ever, even after the incidents or profile it was judged on had
changed. The question shows the incident each name appeared in and any stored
profile of either organisation; `EntityAliasDecisions.evidence_hash` is a hash of
that. A model's decision is now **asked again when that hash has changed and the
pair matters now**: it is proposed for folding (by name structure, alias
learning or the stored-alias check), or it is a stored `different` /
`related` / `parent_subsidiary` that bars a name from a proposed fold. A decision
no proposal touches is left alone however old. A person's decision
(`model = 'human'`) is never asked again and outranks every model's. A decision
with no recorded hash (made before it was kept: 1,080 of 1,345 on the
2026-10-29 copy) cannot be shown unchanged, so it is asked again the first time
it matters (about 230 pairs were proposed that way on that copy; later runs ask
only what changed). The decision it replaces is kept in
`EntityAliasDecisionHistory`; a failed re-ask leaves a veto in force and the pair
in `report.failed` (the fold is the risky direction).

**A group is checked against every recorded verdict, not just its head.** Names
were checked one by one against the group's chosen name, so A=B and A=C could
both be confirmed while B and C were recorded as different organisations, and
the three still folded. `verified_groups` now refuses to put two names in one
fold when a `different`, `parent_subsidiary` or `related` verdict is stored
between *any* two of them (the name with fewer recorded conflicts joins, the
other is left out and reported as not folded). It asks Perplexity only about a
pair that has no verdict and could change the fold: a name a recorded conflict
already bars is not asked about. `unsure` is not a conflict.

**Folds are lossless, recorded and reversible** (`dedup/entity_merge_ledger.py`).
A fold used to delete the dropped entity before inheriting its attributes
(so none were), drop a link both spellings held on one record or event without
a trace, and leave nothing to undo it with. Each fold is now one atomic step:
attributes the dropped row carries are inherited first (blanks only; the size
estimate as a unit); its links move onto the kept entity; where a record or
event already links both spellings the surviving link keeps the **stronger
role** (victim > vendor > affected customer > ... > bystander), the higher
confidence and the summed source count; and an `EntityMerges` row records the
original entity rows, every link and what became of it (`moved` / `collided`,
with both original rows), roles, record and event ids, renamed events, mentions,
the alias and the verdict the fold rested on. Measured against the 2026-05-03
backup, 345 aliased variants had disappeared since, and 143 records had named
both spellings of one organisation; those second links were dropped with nothing
kept (all carried the same role then).

```bash
python scripts/dedup_admin.py entity-merges [--status applied|reversed]
python scripts/dedup_admin.py unmerge-entity <merge_id> [--reason "..."] [--keep-alias] [--dry-run]
```

`unmerge-entity` restores the dropped entity with its original id (so rows
still keyed by that id point at it again), moves its links back, splits the
collided links, restores the inherited attributes unless they changed since and
renames the events back; it removes the alias that would fold the pair again and
records the pair as `different`. It refuses, changing nothing, when the dropped
name is in use again or the kept entity was itself folded away later (undo that
fold first). A pair in `EXPLICIT_ALIASES` is folded again on the next run; a
verdict cannot override a reviewed alias. It is **automatic** where a later
decision overturns a fold: `canonicalise-entities` first undoes every applied
fold decided by a model (`llm`, `structure`) whose pair now has a `different`,
`parent_subsidiary`, `related` or `unsure` verdict, and again after verifying
stored aliases; folds a person reviewed (`human`) are never reversed by a model.
Folds made before the ledger existed have no record and still need review by
hand.

**Acronym aliases hold only in context** (`dedup/contextual_aliases.py`). A
confirmed alias used to apply everywhere: the variant's entity was folded and
every link moved with it. That is right for "National Australia Bank" -> NAB,
wrong for an acronym: "AP" is Advance Press in one record and Associated Press
in another, "UC" the University of Canberra or of California, "ACS" the
Australian Computer Society or the American Chemical Society. A
model-confirmed alias whose variant is an acronym or initialism (every token
upper case, at most 8 letters) is now **contextual** - kept in
`ContextualEntityAliases`, which can list several organisations for one
acronym - and is applied one record (or event) at a time:

1. the record's own text names exactly one candidate in full (or by a
   non-acronym alias) - placed, no model;
2. otherwise Jev reads the text around the mention and chooses a candidate, "a
   different organisation" or "the text does not say"; a candidate (or
   "different") at **0.9** or above decides it;
3. below that the strong OpenAI model re-judges with reasoning; a failed
   escalation leaves the link where it is and the step exits 1;
4. anything else stays unresolved, and an unsure answer moves nothing: a link
   leaves an organisation only when its text resolves it elsewhere or a model
   is confident the acronym means a different organisation.

Reviewed aliases (`human`, and every pair in `EXPLICIT_ALIASES` such as ASIC
and CBA) stay global, as do variants that contain the organisation's own name
("JBS S.A.", "US CISA"). `canonicalise-entities` moves stored acronym aliases
into the contextual table, and re-checks the links: a link on the acronym's
entity moves to the organisation its text resolves to, and a link that an
earlier entity-wide fold put on an organisation, in a record that mentions only
the acronym, returns to the acronym's entity when a model is confident it means
a different organisation - the OpenAI-agent reports credit photos to "AFP"
(Agence France-Presse) and "AP" (Associated Press), and had been filed under the
Australian Federal Police and Advance Press. Event victim names follow their links. Decisions are kept in
`EntityMentionResolutions`; model answers in `ModelJudgements`. Ingest
(`_link_entities_to_enriched_event`, victim replacement) resolves global
aliases and applies a contextual one only when the record's text names the
organisation. The 0.9 bar is not calibrated on labelled mentions; it is set
high because an unresolved acronym (a separate entity) is the less harmful
error than the wrong organisation. On 2026-10-29, 65 stored aliases became
contextual and 90 links were re-checked.

**Dates.** Merging took the *earliest* member date, so one bad extraction
captured the event: MediSecure has 77 source records, 64 saying April 2024, and
was stored as **March 2019** - which then convinced the cluster adjudicator that
its fragments were separate incidents. The modal month now wins, using the
earliest date within it. Overruling a stored date needs real agreement and is
refused when the title asserts its own date, so "Optus White Pages breach
(2019)" is not dragged to 2022 by coverage of the larger 2022 Optus breach.
Blank dates are recovered from the description, then by search.

**Reconciliation.** `reconcile-entities` ignores blocking entirely: it groups
every active event by the `victim` **role** finally attributed to it and asks
one LLM call per organisation whether those events are one incident. This is
the follow-up pass - it catches what blocking missed, including the many events
with no scalar victim that the cluster pass skipped.

### Publication dates, first reports and reporting lag

A report's publication date is not the incident date, and the enrichment gate
never passes one off as the other. It is stored in its own right instead, so
reporting lag can be modelled (`processing/publication_dates.py`):

| Column | Meaning |
|---|---|
| `RawEvents.publication_date` | the day the source was published: the source's own date (feed, listing, Google's page metadata), else the page's metadata (JSON-LD `datePublished`, `article:published_time`, ...; never a *modified* date), else a full date in the URL path |
| `RawEvents.publication_date_source` | where that came from |
| `DeduplicatedEvents.first_reported_date` | the earliest publication date of the event's records |
| `DeduplicatedEvents.reporting_lag_days` | first report minus incident date, only where the incident date is not itself a publication or discovery date (filter on `date_precision` for day-level lags) |

An event with no incident date is dated by its first report, with
`date_basis = 'publication date (may lag the incident)'`; the duplicate checks
are shown that basis. Until 2026-10-28 no Google or Perplexity record stored a
publication date (Google records took the *search window's* first day), which
left 129 active events undated and outside every chart.

```bash
python scripts/dedup_admin.py publication-dates [--no-browser] [--no-fetch] [--dry-run]
```

It runs in every refresh (after the short-page re-scrape, and again without
fetching at the end of the hygiene steps), and after every deduplication. Pages
are fetched by plain HTTP; a page the site refuses to a plain client is fetched
again by a browser (Playwright); failures are retried up to 3 times
(`PublicationDateFetches`). First live run: 1,128 of 1,902 pages dated from
their metadata or URL, 506 state no date, 268 failed (the browser pass was not
run); 77 undated events dated by their first report; 674 events have a
measurable lag.

**A listing page's date is nobody's publication date.** A page cited by
records of more than 3 events (the duplicate checks' hub rule) is a breach
list, roundup or tracker; the date in its metadata or URL is the listing's,
not when any one incident was reported. Webber's continuously updated
data-breaches list carries `datePublished` 2018-07-09, and the first backfill
gave that date to 110 records - which made Napoleon Perdis's 2025-26 Retail
Express breach look like a merge of a 2018 incident with a 2025 one. Such dates
are removed (a date the source gave for the item itself is kept) and the
records are not fetched again (`PublicationDateFetches.status = 'listing
page'`): 344 removed on 2026-10-29. A first report is also never earlier than
the event's verified incident interval (61 such dates ignored), and an event
dated by a report is re-dated when its first report changes.

**Incident dates are intervals with verified precision**
(`processing/incident_dates.py`). A raw record holds only an incident date its
source states (`RawEvents.event_date_precision` / `event_date_basis`); the
publication date is never copied into it, and the search window's first day is
no longer stored as one. Webber list headings ("Vertel - June 2025") are a
month, stored as the 1st with precision `month` - they used to become the 15th
(all 832 Webber records), outranking any day the article stated. A day the
article states wins only when its wording ties it to the breach ("the breach
occurred on", "discovered", "detected", "the attack on", "between X and Y");
the first date in an article is usually its byline or publication date
(`incident_date_from_text`: "Published 1 June 2025. The breach occurred on 2 May
2025." gives 2 May at day precision, "Published 1 June 2025" alone leaves the
heading's month), and a tied date more than 6 months before the heading's month
or after the next month is background. The newsletter date in the link is the
publication date. `publication-dates` repairs stored Webber records
(idempotent; `--dry-run`): the 15th goes to the heading month, then
`rederive_webber_dates` gives a heading-month record the day its stored text ties
to the incident (20 of 765 on the 2026-10-29 copy; the rest keep their month) and
returns a day the old parser took from a byline to the heading month (it reads
the live list for that, only when such records exist; none on that copy). Each event's
`event_date_precision` is set only from verified evidence (a source-stated
precision, or a citation-checked Perplexity date), with `event_date_earliest` /
`event_date_latest` as the interval; NULL means unverified. Calendar intervals
are nested or disjoint, so sources that give a month and a day inside it are
**compatible**: the event takes the narrower (a Webber "June 2025" heading and a
researched 12 June is a day, no longer a "conflict"; `PrecisionReport.reconciled`
counts them). A more exact day elsewhere in the same month leaves the month
interval, the one that still holds the stored date. Verified evidence that is
**incompatible** - a different month, or an interval that excludes the event's
date - leaves the event `conflicting` with no precision. On the 2026-10-29 copy
(`refresh_event_precision`, 1,090 active events) the precision or interval of
**74** events changed: 13 reconciled to the day (9 that had been conflicts, 4 that
kept only the month), 1 more gained a month, and 60 that held a verified
precision *and* verified evidence for a different date went to `conflicting`
(40 month, 20 day). Verified events fell from 388 to 338 (166 day, 172 month);
201 are `conflicting` and 551 unverified. The recurrence check and the analyses
read the interval, never a point date rebuilt from the 1st (below).

**Undated events are still deduplicated.** The duplicate check pairs every two
events sharing a victim, dated or not (`gap_days` is then "unknown"); undated
events used to be left out, so 35 undated reports of the OpenAI agent's
Medicare incident were never compared.

### AI-involved events are duplicate candidates of each other

Coverage of one AI incident defeats pairing by victim: the OpenAI agent that
reached Services Australia's Medicare statistics portal was spread over about 15
active events whose victims were missing, vague ("Australian health") or wrong
("Australian Parliament", "Data USA", "OpenAI"), and the one pair that was
compared scored 0.80 against the 0.81 cut-off. AI incidents are rare (a few per
half-year), so two AI events a few months apart are far more likely one incident
than two. `check-duplicates` therefore also pairs **every two AI-involved events
within 365 days** (date intervals, undated events included), whatever their
victim, and asks a question variant that is shown the AI base rate (AI incidents
and all incidents per completed half-year, computed from the data) and each
event's AI category and description. The merge cut-off is calibrated separately
on blind labels of AI pairs - the principled form of "merge AI events more
aggressively": if the model is conservative on these pairs the calibrated cut-off
comes out below 0.81. Until it is calibrated (and while its evidence fingerprint
matches) AI pairs are **reported, not merged**.

```bash
python scripts/dedup_admin.py check-duplicates [--apply] [--skip-ai-pairs]   # both routes; AI pairs report-only until calibrated
python scripts/calibrate_ai_duplicates.py --db <snapshot> score              # PAID: judge every candidate AI pair
python scripts/calibrate_ai_duplicates.py --db <snapshot> sample             # census 0.30-0.90, weighted tails
python scripts/calibrate_ai_duplicates.py --db <snapshot> packet --out DIR   # blind labelling packet
python scripts/calibrate_ai_duplicates.py --db <snapshot> calibrate          # evaluation/ai_duplicate_calibration.json
python scripts/dedup_admin.py recalibrate --step ai-duplicates               # after the evidence changes
```

Labels go in `evaluation/ai_duplicate_labels.csv`
(`pair_id,label,rationale,corroboration,labeller`; `same` / `different` /
`unsure`; guide: `evaluation/ai_duplicate_labelling_guide.md`) and protect pairs
in both directions. The labelling snapshot is a copy of the live database taken
after `score` and before any AI merge (`ai-duplicates` in
`evaluation/calibration_snapshots.json`). A pair is asked by one route only.
Details: [docs/09-deduplication-v3.md section 8l](docs/09-deduplication-v3.md).

**Candidates are ranked, and capped only where a group is large.** Every pair
sharing a victim was asked (229 in a sample, 170 of them more than a year apart;
large groups grew with the square of their size). Candidates are ranked by source
relationships, supplier clues, incident-specific details and date range
(`dedup/candidate_ranking.py`); in a group of more than 60 events each event keeps
its best 8 partners and a small reserved budget (5 per group) keeps strong
long-gap or undated pairs, and the rest are not asked. **Up to 60 events every
pair of an organisation is a candidate**, still ranked (2026-10-29): the cap
bounds a large group's cost, but on the small organisation groups that exist
(the largest has 7 events) it only lost recall - it dropped 6 of the 17
labelled-`same` organisation pairs, misdated copies 707 to 4,603 days apart with no
strong evidence (Qantas, Optus, Swinburne, Telstra, UWA, Elina), which the route
used to judge. The AI route is one group by construction and keeps its cap (16
partners per AI event). The parameters are `--partners-per-event`,
`--long-gap-budget`, `--no-candidate-cap`; `bounded_candidates.UNCAPPED_GROUP_MAX`
is the threshold, and the findings file reports the counts before and after and
`uncapped_groups`.

**Candidates are retrieved, not enumerated.** Ranking needed every pair of a group
built first, so CPU and memory grew with the square of the largest group even
though paid calls stayed bounded. A group of up to 60 events is ranked by
enumeration (all its pairs, at most 1,770); a larger one (`dedup/bounded_candidates.py`) retrieves a
limited set per event through indexes - the events citing the same non-hub
source, naming the same vendor, sharing a corpus-rare word or the same
records-affected figure, the nearest by date interval (sorted lists and
`bisect`), and the smallest pair ids among undated partners - and keeps the same
best partners and the same reserved route for strong long-gap or undated pairs.
Pairs it does not keep are never built; the counts before the cap are computed
arithmetically. `scripts/measure_candidate_generation.py` measures it against
the enumerated ranking and the labelled pairs. On one synthetic group of 2,000
events the enumerated path built 1,999,000 pairs in 30 s and 1.24 GB at peak; the
retrieved path built 31,721 in 1.1 s and 19 MB, and kept the identical 9,235
pairs (20,000 events: 317,920 pairs built of 199,990,000, 12 s, 189 MB). On the
2026-10-29 database the largest organisation group is 7 events and the AI group
36, so nothing there is large enough to need retrieval yet: forced onto every
group it keeps exactly what the capped enumeration keeps. Recall on the
labelling-time snapshots, organisation route: the cap alone kept 11 of the 17
labelled-`same` pairs and 36 of 138 labelled-`different`; with no cap below 60
events all 17 and all 138 are candidates (49 -> 157 pairs on the snapshot; 75 ->
236 on the live copy, of which 76 of the 234 not owned by the AI route have an
answer on their current evidence already and about 158 need a new Jev call, with
an escalation for those in the middle band). AI route: 70 of 70 `same` and 43 of
43 `different`, by every path.

**A final decision is tied to the evidence that produced it (every judging step).**
The final score of a pair - and of an event in event reality and event scope, a
member in coherence and its same-organisation variant, an event-victim pair in
victim attribution - was rebuilt from the newest Jev answer and the newest
escalation, each picked on its own, with no check that they saw the same
evidence: an escalation made before an event's date was corrected could be
combined with a fresh Jev answer, and a stale low score could veto a merge
(`partition_without_conflicts` held links on stored scores below
`different_cutoff`). Now Jev and the escalation are shown one evidence state, and
the final decision is stored in `FinalDecisions` (`dedup/final_decisions.py`)
against the hash of that state, the Jev and escalation models and the question
versions, per `step` (`event-reality`, `event-scope`, `coherence`,
`coherence-same-organisation`, `victim-attribution`, `duplicates-organisation`,
`duplicates-ai`). A score is read only from a decision made on the subject's
**current** evidence: every `stored_final_probabilities` now takes the subjects
(events, `(event, member)` items, pairs) and returns nothing for one whose
evidence changed - the calibration scripts and `recalibrate` therefore score the
labelled cases first (the recalibration plans record as they score), and
`reconcile-timelines` says "no judgement on its current evidence" instead of
reporting an outdated score. The acting steps (`check-events`, `check-scope`,
`check-coherence`, `check-same-organisation`, `check-victim-attribution`,
`check-duplicates`) judge on the current evidence by construction; **labels
decide first** and a subject a label settles is no longer asked about:
`check-events --apply` skips events labelled real, `check-scope --apply` those
labelled in or out of scope, the coherence checks members labelled
same/different, `check-duplicates` pairs labelled same/different (each reports
`settled_by_label`). Victim attribution still judges labelled pairs: its flag
review needs the score of a pair labelled correct to confirm a flag. A stale low decision is asked again **only where it could
affect a merge now**: the pair is a candidate this run (scored on its current
state anyway), or it lies between events a proposed group joins and its old
score was below the calibrated `different_cutoff` - then it is re-judged
(`veto_rejudged` in the findings) and the new answer decides; a stale decision
outside the proposed events, or a stale high one, is left alone, and a
pair whose escalation fails vetoes nothing. Stored Jev and escalation answers are
still keyed on their own input hashes, so a run on unchanged evidence buys
nothing, and a first run after this change fills `FinalDecisions` from the stored
answers without a paid call (on the 2026-10-29 copy 20 of the 73 organisation
pairs; the other 53 and the 347 AI pairs have no stored answer on their current
evidence and need asking, exactly as before this change). Nothing in the
evidence fingerprints changed, so no calibration goes stale.

**The evidence is frozen while it is judged.** Event reality and event scope used
to build an event's evidence three times - for the Jev question, again for the
escalation and again for the `FinalDecisions` row - so a timeline refreshed or a
record re-enriched in between (by this run or another process) left Jev, the
escalation and the stored decision describing different evidence, and the
rejection acted on a decision nobody had made. Each event's evidence for the
final judgement is now built once (after `check-events`' corroboration and
timeline research; the screen keeps its own cheaper state) and that one object is
what Jev is asked on, what the escalation is given and what is hashed into
`FinalDecisions` (`states` in `judge_events` / `escalate_band` / `record_final`;
`final_decisions.frozen`). Immediately before rejecting, each event a score would
reject is built again and its hash compared (`final_decisions.changed_since`); an
event whose evidence changed since it was judged is **not rejected**, is listed in
`evidence_changed` in the findings and the command output, and is judged again
next run. Rejections a label alone decides are unaffected. Coherence (both
variants), victim attribution and the duplicate checks already built each
subject's state once.

**Contradictory links do not join a group.** If A=B and B=C but A and C are
labelled different, ruled different, or scored below the calibration's
`different_cutoff`, the weaker link is held (`held_conflicts` in the findings)
instead of merging all three.

**Date provenance is joined to records by id.** The duplicate check used to pair
two independently ordered lists by position, so a record could be shown another
record's date and basis (331 of 1,190 displayed records on a 407-event sample).
The duplicate evidence version was bumped, so the duplicate calibration is stale
until `dedup_admin.py recalibrate --step duplicates` has re-scored the labelled
pairs.

### Which records and pages may drive event decisions

`processing/active_evidence.py` is the one rule every evidence reader uses
(event reality, duplicates, breach source, AI involvement): **active member
records only** (inactive ones stay for audit), and a stored **page only when it
belongs to its record** - it names the record's organisation (whole name, or
its first distinctive word: "NAB", "Uber") or, with no name, the title's
distinctive words. A page that does not is withheld from the evidence and the
record says so (`page_withheld`); its title, snippet and research still count.
On 2026-10-28 **1,051 of 2,690** stored pages behind active events failed the
check - another article entirely (the "Mackay Sugar ransomware" record held a
page about the OpenAI agent; a "WA Department of Health" record an iTnews page
about Garmin): Webber 435, Perplexity 334, Google 191, OAIC 80, Cyber Daily 11.
The cause is below.

#### Records linked to the wrong article

Measured on a copy of the live database (2026-10-29: 1,179 of 3,402 active records
failed the page rule; Webber 457, Perplexity 409, Google 220, OAIC 80, Cyber Daily
13), the URL was usually *not* the problem. In about 840 of them the raw record
is whole - its title, URL and stored page are one article - but the
`EnrichedEvents` row attached to it (title, description, date, Perplexity
research, entities) describes another article, and in 827 of 840 that enriched
title is *exactly* the raw title of a different record. 1,088 of the 1,283
enrichments made in September 2025 (mostly 2025-09-28) are unrelated to the title
of their record; since 2026-03 the rate is 1-2%. Records and enrichments were
paired by position in an old run and the enrichments came back in another order;
records are paired by id now (`_store_enriched_event` carries `raw_event_id`), and
`create_enriched_event` records any enrichment that does not describe its record.
The rest are pages that are not the record's article: Perplexity citations that
are a site root or a breach list (cybertrackerau.com, borderlesscs.com.au; the
model writes the `source_urls` itself), Webber links whose page heading is a site
name ("Gizmodo", "News", "Lifehacker Disclaimer"), Google matches on a snippet.
Webber's own pairing of an entry with its link was sound; its filter dropped 203
of 1,282 entries (see [01-data-sources.md](docs/01-data-sources.md#5-webber-insurance-data-breaches-list)).

`processing/record_identity.py` (table `RecordIdentityChecks`, [data
dictionary](docs/data-dictionary.md)) checks every record and repairs what it can:

```bash
python scripts/dedup_admin.py check-record-identity --dry-run     # what would change (rolls back)
python scripts/dedup_admin.py check-record-identity [--limit 100] # repair + queue retrieval
python scripts/dedup_admin.py check-record-identity --reverse <enriched_event_id>   # undo one move
```

* An enrichment whose title is exactly one other stored record's title, which that
  record's page does not contradict, is **re-attached** to that record
  (`EnrichedEvents.raw_event_id` and `EventDeduplicationMap.raw_event_id`; the
  enrichment is untouched, so no event is rebuilt). Of several copies the earliest
  goes; a move that would put a record into one event twice, or that no record
  can take, is not made. Moves repeat until none is left. Free, unlimited.
* A record left with no enrichment whose page is its article is reopened
  (`is_processed = 0`) for the discovery retry queue.
* A page that is not the record's article is queued for the **re-fetch**
  (`PageRescrapes`; the record becomes a `rescrape-short-pages` candidate whatever
  the length of its text, and the new text is checked again) when its URL is an
  article, and for the **replacement search** (`source_gate.replace_evidence`: the
  citation becomes a new record with its own URL, never the record's own page) when
  the URL is a site root or a page cited by more than 3 records, the re-fetch
  still gives another article, or the enrichment could not be re-attached. A
  search is skipped when the event has another record whose page is its article or
  another event covers the incident. `--limit` caps each route per run;
  a missing `PERPLEXITY_API_KEY` for a needed search exits 2.
* **Tracking and identity parameters are stripped from every record URL**
  (`utils/url_clean.py`: `utm_*`, `token` and `*token`, click and mail ids, a
  newsletter's `eid`+`edate`, `campaign`, session ids). A newsletter link carries
  the subscriber with it - 24 stored `source_url` values (22 Webber, 2 Google) held a
  signed token naming a subscriber's email. New links are cleaned in the Webber source
  and in `add_raw_event` (so Google, Perplexity and every other source; the newsletter
  date in a Webber link is kept as the publication date), and `check-record-identity`
  cleans what is stored (URL columns, and URLs quoted in stored text) idempotently;
  its report gives counts only, never a value. On the 2026-10-29 copy: 299 stored
  values in 11 columns (110 `RawEvents.source_url`, 141 `DeduplicatedEventSources`).
* New records are checked as they arrive: after scraping, a page that is not the
  record's article is withheld from classification (the record is classified from
  its snippet) and its retrieval is queued; as an enrichment is stored, one that
  does not describe its record is recorded.

On the copy of 2026-10-29 (a full run with a stubbed search; the next three runs
find nothing left to move): 1,343 enrichments and 147 pages mismatched (Webber 553,
Perplexity 586, Google 276, OAIC 73); **923 enrichments re-attached** in the first
run; 482 records they left without an enrichment are reopened, 100 a run (five
runs); 95 page re-fetches queued; 17 replacement searches (the other mismatches sit
in events that have another record whose page is the article). What remains: 227
enrichments are copies inside the event that already holds their record, 66 have no
stored record, 96 an ambiguous one (several records share the title), 27 sit on a
record that has an enrichment of its own, and 122 records end holding two active
enrichments (one known not to describe it; reported, not removed). The hygiene phase
runs the command first of all.

### Dashboard

```bash
python scripts/build_static_dashboard.py               # Generate dashboard/index.html
python scripts/enrich_pii_and_source_category.py       # OAIC breach source + PII types (Jev)
```

The OAIC comparisons (source of breach; personal information types) need
`DeduplicatedEvents.breach_source_category` and `personal_info_types_json`.
`enrich_pii_and_source_category.py` asks Jev per active event (a Choice over
OAIC's three sources plus "cannot tell", stored as `Unknown`; a Noul per
information type, read at 0.5 and uncalibrated) over the members' evidence,
stores every answer in `ModelJudgements` and re-asks only events whose
evidence changed. It requires `TYPESAFE_API_KEY` and exits non-zero if any
event could not be judged.

#### AI involvement

```bash
python scripts/classify_ai_involvement.py [--dry-run] [--no-descriptions]   # Jev, every active event
python scripts/build_ai_incident_report.py                                   # dashboard/ai_incidents.html alone
```

Every active event carries `DeduplicatedEvents.ai_involvement`, one of:

| Value | Meaning |
|---|---|
| `AI-attacker` | an AI system was the attacker or the attacker's instrument (the OpenAI agent that reached Services Australia's Medicare statistics portal in 2026; deepfakes or AI-written phishing the reporting says were used) |
| `AI-vulnerability` | the victim's own AI was attacked, manipulated or leaked data through a flaw (prompt injection, a jailbroken chatbot, exposed model or training data, a compromised AI integration) |
| `AI-misuse` | no attack: the organisation's own use of AI was the breach (Kmart's in-store facial recognition, Clearview AI scraping faces into its tool, a contractor uploading applicants' data to ChatGPT). Added 2026-10-29; those three were labelled attacker or vulnerability before |
| `AI-uncertain` | AI involvement likely, but no category holds three quarters of P(AI); listed, not counted as any category |
| `No-AI` | no AI involvement reported (speculation, commentary or AI as the victim's business does not count) |

Jev answers one Choice per event over the members' evidence (the same
evidence as the breach-source question: each record's **whole page** up to
12,000 characters, 60,000 per event, any cut stated - until 2026-10-28 it was
the first 1,200 characters, mostly site menus, and 80% of pages mentioning AI
first did so after that point). All four probabilities are stored in
`ai_attacker_probability`, `ai_vulnerability_probability`,
`ai_misuse_probability` and `no_ai_probability` (the full answer in `ModelJudgements`, reused until the
evidence, question or model changes). The label is decided by a **calibrated
cut-off** on P(AI) = 1 - P(No-AI) when `evaluation/ai_involvement_calibration.json`
matches the current model and question (`scripts/calibrate_ai_involvement.py`
`sample` / `packet` / `calibrate`, from blind labels in
`evaluation/ai_involvement_labels.csv`); labelled events take their label.
The calibration records the evidence fingerprint (`ai_involvement.evidence_fingerprint`) as well as the model and question;
any of them changed and it does not apply until `calibrate` is rerun. Without a matching calibration the label is the most probable option, and
`ai_involvement_basis`, the report and the dashboard say so. Each AI incident also gets
a 2-4 sentence description from the fast OpenAI model, stored in
`EventDescriptions` and rewritten only when its evidence changes. The step runs
in every refresh after the breach-source step; it requires `TYPESAFE_API_KEY`
(exit 2 without it) and exits 1 if any event could not be judged or described.

**Stale labels and descriptions are marked, never shown as current.** Each
label stores a hash of what it was decided on (evidence, question, models,
cut-off, label; `ai_involvement_input_hash`), and a failed re-judgement keeps
the old label with the error (`ai_involvement_error`). The report and the
dashboard re-derive the hash from the current evidence and mark and count any
label that no longer matches, including No-AI labels the report does not list
(which may now involve AI). A description is shown as current only for the
evidence it was written from; an older one is shown marked "written from older
evidence". The report's headline counts only incidents with a settled category;
AI-uncertain incidents are listed and counted separately.

The main dashboard shows AI incidents per half-year (stacked: attacker /
vulnerability / misuse / unsettled, with the count not yet judged), reconciled against the
database, and links to `dashboard/ai_incidents.html`, written and published
with it: every AI incident in the dashboard window, oldest first, with date,
victim(s), category (and its probability) and the description.

Unknown, missing or unrecognised severity is never scored as 0: severity
averages (by industry, by attack type, per month) use known severities only and
each chart says how many events it excluded. Personal-information rates use only
incidents with a well-formed `personal_info_types_json` as the denominator;
malformed and unclassified incidents are excluded from numerator and
denominator alike and counted on the chart (OAIC rates are over the semester's
total notifications).

### Publication: one snapshot, one gate, all outputs together

```bash
python run_full_pipeline.py --dashboard-only          # or the last phase of a full run
python scripts/publish_outputs.py                     # the same step on its own
python scripts/publish_outputs.py --snapshot-id <id>  # rebuild from a retained snapshot (reproduce)
python scripts/publish_outputs.py --list              # retained snapshots
```

The dashboards used to be built from the live database, the quality gate to read it
separately, and the risk matrices to be written before any gate. Now the step takes **one
consistent snapshot** (SQLite backup API), keeps a compressed copy under
`instance/snapshots/`, runs the gate on it, builds `dashboard/index.html`,
`ai_incidents.html`, `entities.html`, `timelines.html`, `dedup.html`, `recurrence.html` and
the `risk_matrix/` workbooks from it into a staging folder, stamps each with the snapshot
id, writes `dashboard/publication.json` (hashes of every output and of the inputs outside
the database) and moves them all into place together. A blocked gate, a failing builder or
a failed move leaves every previous output untouched, and a crash half-way is rolled back
by the next run. The quality run records the snapshot id (`QualityRuns.snapshot_id`).

Retention: the last 5 published snapshots, the last 2 blocked / failed / analysis ones,
and any snapshot an analysis result names; about 100 MB each, 0.8 to 1.2 GB in all.
Expired copies keep their manifest (id and hash). Details:
[docs/08-dashboard-generation.md](docs/08-dashboard-generation.md).

### Database

```bash
python scripts/wipe_database.py                        # Reset entire database
```

### Data Integrity Checker

End-to-end sanity check that detects every failure mode encountered to date
(truncated severity strings, qualified `EventSeverity.X` enum names,
ASD-classification orphans, byte-identical OAIC H1/H2 records, top_sectors
notification counts outside a sensible range, etc.). Returns exit code 1 if
any check fails, so it doubles as a CI gate.

```bash
python scripts/check_data_integrity.py                 # Full report
python scripts/check_data_integrity.py --quiet         # Only show failures
python scripts/check_data_integrity.py --db PATH       # Override DB path
python scripts/dedup_admin.py normalise-enums [--dry-run]  # repair qualified enum names
```

`active-enrichment-unique-per-record` fails when a record holds more than one
active enrichment that no record-identity finding explains.

Qualified names (`EventSeverity.MEDIUM`) came from `str()` of a `(str, Enum)`
member on Python 3.11+; the enums now stringify to their values, storage
canonicalises on write, and `normalise-enums` rewrites stored rows (including
`RawEvents.source_metadata`, which re-enrichment copies from) idempotently.

### Quality report and publication gate

`scripts/quality_report.py` measures data quality on every run, stores each
metric in `QualityRuns` / `QualityMetrics` (so trends are one query away), and
decides whether outputs may be published: source coverage per month, events
with no evidence, retry backlogs, short-gap same-victim pairs and identical
titles, invalid and placeholder dates, stale ASD classifications / roles /
facts, every `check_data_integrity.py` check, contradictions of the labelled
outcomes in `evaluation/*_labels.csv`, and fixed synthetic cases for the
deterministic merge, records-affected, fact and date-provenance functions.
Thresholds are committed in `cyber_data_collector/quality/thresholds.json`;
blocking ones are integrity, label contradictions (> 2), synthetic cases,
future or malformed dates and stale ASD classifications (> 5%). A check that
cannot run is *not measured*, which blocks like a failure - never a pass.

```bash
python scripts/quality_report.py                 # exit 1 when blocked; writes instance/quality_report.{json,md,html}
python scripts/quality_report.py --snapshot-id latest-published   # measure a retained snapshot (the pipeline always measures a snapshot)
python scripts/quality_report.py --no-record     # read-only
```

From code: `from cyber_data_collector.quality import publication_allowed`;
`allowed, reasons = publication_allowed(conn)`. Details and the first run's
results: [docs/10-quality-gate.md](docs/10-quality-gate.md).

A check that cannot run or cannot read its input is a **FAIL**, never a pass:
an unreadable OAIC file fails every OAIC check, a database error during the
dashboard check fails it, and a missing `--db` fails without creating a file
(the database is opened read-only). Checks that genuinely do not apply (an
in-memory or incomplete database, no published OAIC dataset yet) are reported
as `SKIP` and counted separately. `oaic-published-dataset-consistent` fails on
any contradiction in the published OAIC dataset.

### Reporting population: one definition for every dashboard and export

`DeduplicatedEvents` is the whole history of deduplication, not a list of
incidents: merged and rejected rows stay behind with their ids. The ASD risk
matrix once LEFT JOINed every classification to it with no status filter and
reported 1,201 classifications when only 643 belonged to active incidents - it
nearly doubled the picture, and each dashboard kept its own copy of the filter.

The population is now defined once, in
[`cyber_data_collector/reporting/population.py`](cyber_data_collector/reporting/population.py):

| | Definition |
|---|---|
| **Incident** | a `DeduplicatedEvents` row with `status = 'Active'`. `is_australian_event` / `is_specific_event` are not filtered on: dedup writes both as constant `True`, and `check_data_integrity.py` verifies that rather than assuming it |
| **ASD classifications** | INNER JOINed to an active incident, so merged / rejected / orphan classifications cannot be counted |
| **Victim entities** | joined *from* the incident with `relationship_type = 'victim'` |
| **Period** | `ReportingPeriod(start, end)`, both ends inclusive calendar days; SQL is half-open (`>= start AND < end + 1 day`) so a timestamp on the last day is not lost. Undated incidents belong to no period |
| **Dashboard window** | `dashboard_period()` = 2020-01-01 to the build date. The ASD matrix's "All Years" is now "2020 to date" so it agrees with every other chart |
| **OAIC comparisons** | `half_year()`, `calendar_year()`, `half_year_from_label()`, `span_of()`: the database is queried over exactly the semesters the OAIC series was summed over, never a hard-coded range |

Nothing outside that module may write `status = 'Active'` or a date literal in a
reporting query (a test enforces it).

**Reconciliation.** Every dashboard build proves its chart totals against the
population before writing anything - monthly / half-yearly / type / industry /
severity totals equal the active incidents in the window, yearly counts sum to
the total, the three ASD matrices equal the active classified incidents (cells
plus any the ASD vocabulary could not place), and each OAIC-vs-database column
reconciles over its own window. A mismatch is logged at ERROR for each check and
the build raises `ReconciliationError` / exits 1; the previous `index.html` is left
untouched. `scripts/check_data_integrity.py` runs the same checks
(`dashboard-totals-reconcile`) plus `reporting-population-consistent`,
`population-flags-consistent` and `no-future-dated-incidents`.

**Exports** (`export_cyber_events.py`, `export_events_excel.py`) contain the same
population by default: active incidents in the dashboard window. `--all-dates`
adds pre-2020 and undated incidents; `export_cyber_events.py --include-history`
adds merged / rejected rows for audit and says so. The deduplication review
dashboard deliberately shows history, and labels it: it lists the active
incidents, each with its full merge ancestry.

### Repeat-event timing analysis

`scripts/analyze_recurrent_timing.py` tests whether repeat cyber-event timing
for victim entities is consistent with a memoryless process, or whether risk
changes with time since the prior event.

The analysis is conditional on an entity having at least one observed victim
event. Single-event entities contribute right-censored post-event spells. The
primary model is a piecewise exponential recurrent-event model over elapsed
time since the prior event. The adjusted model controls for calendar period,
prior event number, ordinal organisation size (`SMALL`, `MEDIUM`, `LARGE`,
`HUGE`, with `UNKNOWN` handled separately), sector proxy, entity kind and
records-affected band.

Organisation size is a proxy for differences in visibility, attack exposure
and the value of targets; those factors are not directly observed or fully
controlled by size. The main model assumes equal log-rate steps between size
categories. Unadjusted tables and plots are descriptive diagnostics; use the
size-adjusted results for comparisons of recurrence.

**Same-date ties and unknown ordering.** Spells are built in one place,
`scripts/recurrence_extended/spells.py`, used by the main analysis
(`build_spells`), the timing and date-precision runs, the order/transition runs
and the linkage runs, so they cannot disagree about a tie. A *tie* is two or more
distinct incidents of one organisation with the same stored date; nothing orders
them, and a month-precision date is stored as the 1st, so some ties are month
placeholders rather than simultaneous incidents. Each tie policy says what the
model does with unknown ordering:

| Policy | A tie is | Effect on the models | Bias |
|---|---|---|---|
| `collapse` (**primary**, `--ties collapse`) | one time point; every incident id and the count are kept (`prior_event_ids`, `prior_event_count`, `next_event_count`) | no order or gap is invented; the tie can start or end a spell but is never a repeat of its own; prior-event numbers count time points | can understate repeats |
| `zero_gap` (`--ties zero_gap`) | separate incidents, ordered arbitrarily by id, each one day after the previous (`tie_gap = 1`; an exact zero would give a spell no exposure) | each extra incident is a repeat in the first elapsed-time band; prior-event numbers count incidents | can overstate repeats (a tie may be one incident filed twice, or a month placeholder) |

Both are always run and compared: the main report has a "Same-Date Ties And
Unknown Ordering" section, `tie_policy_sensitivity.csv` (repeats in total and
per band, the adjusted constant-hazard test, the 0-90 / 91-180 banding test
and the directional delayed-peak test under each policy, all adjusted for
organisation size like the primary model), and `entity_date_ties.csv` lists every tie
with its incident ids and kind: `month_precision_first_of_month` (every incident
verified as month precision, so order and spacing are unknown by construction),
`first_of_month_not_month_verified`, or `other_day`. The data-quality summary
counts the ties by kind and the consecutive incidents on *different* stored dates whose
verified intervals overlap (no tie, yet not ordered by their dates either). The
extended analyses take `collapse` as primary and report both in
`timing_tie_policy_sensitivity.csv` (unadjusted and size-standardized rates for every
band scheme) and `timing_entity_date_ties.csv`. Under `zero_gap` a tied
incident's attack type is its own and a transition between tied incidents
depends on the arbitrary order (a bound, not a finding). The underlying
incidents are never changed. On the 2026-10-29 copy the same-organisation
same-day merge rule leaves **one** tie (two incidents of one hotel group, a 1st of
the month whose precision is unverified, none month-verified): `zero_gap` adds one
repeat (126 to 127), and moves the adjusted constant-hazard `p` from 0.635 to 0.632,
the 0-90 / 91-180 banding `p` from 0.696 to 0.727 and the directional delayed-peak
`p` from 0.299 to 0.308.

**Dates in the sensitivity analysis are intervals.** `timing_precision.py` takes
an event's verified `date_precision` with its stored `event_date_earliest` /
`event_date_latest`; a month or year is drawn uniformly within that stored
interval, not within one rebuilt from the 1st. A date whose precision is
unverified has no interval - it is unknown, not a day - and the
`verified_intervals_complete_histories` scenario (organisations all of whose dates
are verified, imputed within their intervals) and
`explicit_day_only_complete_histories` leave other organisations out instead of
reading their dates as days. The two `ambiguous_firsts_*` scenarios remain
hypothetical assumptions about unverified 1sts and are labelled so.

```bash
# Main report with broad elapsed-time bands
python scripts/analyze_recurrent_timing.py --bootstrap 500

# Formal delayed-peak sensitivity: 0-90, 91-180, then later bands
python scripts/analyze_recurrent_timing.py \
  --elapsed-bounds 0,90,180,365,730,1460,inf \
  --out-dir analysis/recurrent_timing_90_180_peak \
  --bootstrap 500

# Extended timing, linkage, transition and observation-bias analyses
python scripts/analyze_recurrence_extensions.py \
  --db instance/cyber_events.db --bootstrap 500 --date-draws 100

# Or run the existing report and the extended analyses together
python scripts/analyze_recurrent_timing.py --extended --bootstrap 500 --date-draws 100

# Sensitivity: without victim attributions left unresolved by the attribution check
python scripts/analyze_recurrent_timing.py --unresolved-attributions exclude \
  --out-dir analysis/recurrent_timing_resolved_only --bootstrap 500

# Sensitivity: every incident of a same-date tie a separate repeat (default: collapse)
python scripts/analyze_recurrent_timing.py --ties zero_gap \
  --out-dir analysis/recurrent_timing_zero_gap_ties --bootstrap 500
```

**Unresolved victim attributions.** Victim rows that the calibrated attribution
check flagged and could not resolve are tagged `attribution_unresolved` when the
data loads. Every run reports, under "unresolved victim attributions" in the
data-quality summary (and `analysis_summary.json`), how many rows, events and
organisations they are. It also reports the observed repeats per elapsed-time
band with them included and excluded, and how many repeats involve one. A
repeat involves one only when its own organisation's attribution on the event
before or after it is unresolved: the flag belongs to an (organisation,
incident) pair, so on a supplier incident shared by several customers,
flagging one does not mark the others' repeats (and `exclude` drops only the
flagged customer's row). They are kept by default (`--unresolved-attributions include`; `exclude` drops them
from the whole analysis, in both scripts). On the 2026-10-28 copy, 41
attributions were unresolved and 19 of 150 observed repeats involved one.

The extended analyses write `extended_recurrence_report.md`, a JSON summary,
and detailed CSVs to **one folder, `analysis/recurrent_timing_extended/`**, whether
run standalone or with `--extended` (`--extended-out-dir` overrides it). Until
2026-10-29 `--extended` wrote a second copy under `analysis/recurrent_timing/extended/`:
that folder held 29 October results without the frozen cohorts (its prospective
test read "unavailable: no cohort was frozen") while the standalone one held 28
October results with them. The second copy is removed and the frozen cohorts stay where
they are.

**Every analysis names its data.** A run reads one retained snapshot of the database
(`instance/snapshots/`, see
[the publication section of docs/08](docs/08-dashboard-generation.md)), shared with its
`--extended` analyses, and writes `analysis_inputs.json` beside its outputs (snapshot id,
sha-256, the retained file, censor date, seed, options). The snapshot is kept while that file
exists under `analysis/`, so any result can be reproduced from its exact input:

```bash
python scripts/analyze_recurrent_timing.py --extended --bootstrap 500 --date-draws 100   # prints the snapshot id
python scripts/analyze_recurrent_timing.py --snapshot-id <id> --elapsed-bounds 0,90,180,365,730,1460,inf \
  --out-dir analysis/recurrent_timing_90_180_peak --bootstrap 500
python scripts/analyze_recurrent_timing.py --snapshot-id <id> --elapsed-bounds 0,90,365,730,1460,inf \
  --out-dir analysis/recurrent_timing_90d --bootstrap 500
python scripts/analyze_recurrent_timing.py --snapshot-id latest-published ...   # exactly what the dashboard shows
```

The extended report compares timing bands and a smoothed rate, date-precision
scenarios, alternative recorded merge and victim decisions, first and later
repeats, attack-type transitions, shared-incident dependence, and reporting and
missing-history scenarios. A first-of-month date is not assumed to be imprecise;
the report labels hypothetical date assumptions separately. Its prospective
peak test requires a cohort frozen by the fixed registration date and sufficient
future observations; historical data alone cannot establish confirmation.
The extended analysis also reports size-specific and directly standardised
comparisons using a fixed distribution of organisations in the observed cohort,
with UNKNOWN treated as a separate size group. A standardised estimate is
unavailable if a required size group has no exposure; it is not replaced by a
pooled estimate or by silently changing the standard population.
Keep `timing_peak_registration.json` in the output directory across reruns;
it contains the frozen cohort needed for future validation and cannot be
recreated retrospectively after the registration date. Also preserve
`timing_size_adjusted_registration.json`, the separately frozen cohort and
size groups for the new size-adjusted prospective test.
Results describe recorded incidents. A historical cutoff on its own filters by discovery date but
reads today's links; `--as-known-at` reconstructs them instead (next section).

#### Inputs as known at a date (`--as-known-at`)

The analyses used to take a cutoff by discovery date but read today's incident links, event dates, victims
and size bands. `dedup/input_history.py` now records **when each of those became known**, and both analysis
scripts can reconstruct their inputs from it:

```bash
python scripts/dedup_admin.py input-history              # start/report recording; idempotent (also run by `pipeline.py init`)
python scripts/dedup_admin.py input-history --status     # report only
python scripts/analyze_recurrence_extensions.py --as-known-at 2026-06-30 --bootstrap 500
python scripts/analyze_recurrent_timing.py --as-known-at 2026-06-30 --extended --bootstrap 500
python scripts/analyze_recurrence_extensions.py --snapshot-id <id> --as-known-at 2026-10-27   # a follow-up at the registration date
```

| Decision | Watched table | History table |
|---|---|---|
| incident dates, status, fields | `DeduplicatedEvents` | `EventStateHistory` |
| event membership | `EventDeduplicationMap` | `EventMembershipHistory` |
| victim attribution (roles) | `DeduplicatedEventEntities` | `EventEntityLinkHistory` |
| organisation size band | `EntitiesV2` | `EntityStateHistory` |

* **One mechanism for every writer.** SQLite triggers (installed by `init`, every `dedup` schema step and
  every pipeline connection; refreshed if a tracked column appears) append a row - the full tracked state, with
  `known_at` in UTC from the database clock - whenever a tracked column of a watched row changes. A merge,
  split, role refresh, entity fold, redate or re-sizing is therefore recorded whichever code path made it, and
  code added later is covered with no hook. Untracked columns write nothing.
* **Existing ledgers are reused, not copied.** `VictimAttributionActions`, `VictimFillActions`,
  `EntityAliases`, `ContextualEntityAliases`, `EntityMentionResolutions`, `DedupOverrides` and `ModelJudgements`
  carry their own timestamps; the reconstruction drops rows dated after the cutoff. `EntityMerges` and
  `DedupSnapshots` hold the state *before* one change, not since when it held and not for every change, so they
  cannot date a state; their effects are in the history tables from when tracking began.
* **History before tracking cannot be invented.** The first time a table is tracked its current state is
  written as `baseline` rows (`backfilled = 1`) and `InputHistoryStatus.tracking_since` records when. A cutoff
  earlier than that is **reconstructed from current state**: each row takes the state recorded when tracking
  began (later changes are undone), and a row whose first record is a genuine insert did not exist before it.
  A ledger is trusted from its first row only; a cutoff before that leaves the table as it is. The report
  (`## Inputs as known at DATE`) and `analysis_inputs.json` (`history_reconstruction`) list, per input, whether
  it came from history or from the baseline, with counts of rows removed (not yet known), restored (since
  deleted) and changed, and which columns were tracked only later.
* **Dates are UTC.** `--as-known-at 2026-06-30` is the end of that UTC day; `"2026-06-30 08:30"` is exact.
  The observation cutoff defaults to it and may not be later. Other inputs (attack type, records-affected
  band, source text) are the present-day values unless listed in the report.
* **Reproducible.** The retained snapshot is the live database as it was; the reconstruction runs on a
  working copy, so `--snapshot-id <id> --as-known-at <date>` (recorded as `reproduce` in
  `analysis_inputs.json`) reproduces it exactly.
* **The registered protocol is unchanged.** `timing_peak_registration.json` and
  `timing_size_adjusted_registration.json` are read, never written. When `--as-known-at` equals a
  registration's `snapshot_cutoff`, the report also compares the frozen cohort with the cohort the reconstructed
  inputs give (organisations in both, with the same anchor date and events), so a follow-up can show how far the
  database has drifted from what was frozen. Tracking began after the 2026-10-27 registration, so that date is
  reconstructed from the baseline and says so.

Main outputs are written under `analysis/recurrent_timing/`:

| File | Purpose |
|---|---|
| `recurrent_timing_report.md` | Human-readable report and interpretation |
| `analysis_summary.json` | Machine-readable summary of model results |
| `piecewise_elapsed_hazard.csv` | Unadjusted elapsed-time rates by band |
| `adjusted_piecewise_coefficients.csv` | Adjusted model coefficients and rate ratios |
| `u_shape_adjusted_contrasts.csv` | U-shape phase contrasts |
| `u_shape_covariate_variant_contrasts.csv` | Sensitivity checks dropping size and/or sector |
| `delayed_peak_sensitivity_adjusted_contrasts.csv` | Formal 91-180 day delayed-peak contrasts |
| `entity_date_ties.csv` | Every organisation-date with several incidents: ids, titles, kind (replaces `same_day_entity_events_collapsed.csv`) |
| `tie_policy_sensitivity.csv` | Repeats and the headline adjusted tests with ties collapsed (primary) and as zero-gap repeats |
| `piecewise_hazard.png`, `survival_curve.png`, `parametric_hazards.png` | Diagnostic plots |

**`dashboard/recurrence.html`** is published with the other dashboards (see
[Publication](#publication-one-snapshot-one-gate-all-outputs-together)): one
self-contained page with the headline tests for every banding side by side, then
each run's full report with its plots inline. It reads the run folders on disk, so
after a data refresh rerun all three bandings on one snapshot (main,
`_90_180_peak`, and `_90d` with `--elapsed-bounds 0,90,365,730,1460,inf`); the
overview lists the censor date and the data snapshot of every run and warns when
runs used different snapshots or not the published one. The analysis scripts no
longer write the page themselves (`--dashboard-out PATH` still does, for a one-off
look; `python scripts/build_recurrence_dashboard.py` builds it alone).

The censoring date defaults to the last ingest (`MAX(RawEvents.discovered_at)`),
so a rerun after a refresh uses the new data without editing the script.

Current headline result (live database, censor date 2026-10-29: the Cyber
Daily backfill, events dated by their first report, listing-page dates removed,
acronym aliases applied in context, and duplicates paired by organisation; 893
victim organisations, 69 with more than one event, 123 observed repeats).

Every conclusion sentence in the reports and on `dashboard/recurrence.html` is
generated from the estimates by `scripts/recurrence_conclusions.py`: rate
ratio, 95% CI, which test (two-sided or one-sided) and its p-value, whether the
estimate points the hypothesised way, and the repeat counts behind it (fewer
than 10 in a compared period is flagged).

- **No elapsed-time pattern.** None of the three bandings rejects constant
  elapsed-time risk: adjusted `p = 0.59` (broad `0-180 / 181-365 / ...`),
  `0.72` (`0-90 / 91-180 / ...`) and `0.58` (`0-90 / 91-365 / ...`). The
  directional delayed-peak test gives `p = 0.94`.
- Rates per 100 entity-years: `0-90` 5.42 (13 repeats), `91-180` 5.99 (13),
  `181-365` 6.26 (24), `366-730` 4.04 (24), `731-1460` 4.77 (29), `>1460` 6.07
  (20). Adjusted, `91-180` is `1.12x` the first 90 days (`95% CI 0.52-2.41`)
  and `1.01x` the pooled post-180 period (`0.56-1.82`).
- How the data and the counting moved it:

  | Data state | Repeats | 0-90 | 91-180 | 0-90 / 91-180 split `p` | 91-180 vs post-180 |
  |---|---|---|---|---|---|
  | 2026-10-02 refresh, before cleaning | 102 | 11 | - | 0.147 | 1.74x (1.01-2.98) |
  | + victim corrections, 69 rejections, 15 merges (2026-10-27) | 74 | 4 | - | 0.007 | 2.36x (1.30-4.28) |
  | same data, each organisation counted once (`victim_counting.py`) | 61 | 4 | 10 | 0.026 | 1.79x (0.88-3.63) |
  | + 2026-10-28 live run | 57 | 3 | 6 | 0.122 | 1.13x (0.47-2.70) |
  | + Cyber Daily backfill and dating undated events (2026-10-29) | 128 | 17 | 13 | 0.662 | 1.02x |
  | + duplicates paired by organisation, 8 merges (2026-10-29) | 123 | 13 | 13 | 0.718 | 1.01x (0.56-1.82) |

- **Short-gap repeats were partly one incident counted twice.** The duplicate
  check paired events by victim entity row, the analysis by organisation, so a
  leak-site claim and the victim's disclosure days later under another spelling
  ("ALS Global" / "ALS", "St Vincent's Health Australia" / "St Vincent's
  Health", NSW RFS, Myelec) were never compared and counted as repeats. Pairing
  by organisation merged 8 (four of the 17 `0-90` repeats).
- **Still in the short bands and suspect**: Consonic (18 days, the same Akira
  claim reported twice; duplicate score 0.80, just under the 0.81 cut-off),
  Revolut (8 days; the second event is commentary), Australian Centre for
  Heart Health (65 days, one claim under two names) and two misdated 2016 CBA
  events. Without them `0-90` would hold 9 repeats; the conclusion does not
  change.
- The earlier delayed peak (2026-10-27) rested on non-incident and attribution
  artefacts; see the history above and `analysis/recurrent_timing/` for the
  generated conclusions.
- The prospective test registered on 2026-10-27 (574 frozen organisations,
  `timing_peak_registration.json`) is unaffected and pending until
  2028-10-27.
- The analysis tests timing patterns, not causality.

### Safety Vault (snapshot/restore around destructive operations)

`run_global_deduplication.py --force` automatically snapshots ASD
classifications and industry overrides before rebuilding `DeduplicatedEvents`,
then restores them via the stable `master_enriched_event_id` after the rebuild.
Snapshots are written to `instance/safety_vault/`. No manual action required;
a force-dedup is now safe to re-run without losing session-level corrections.

The vault module: [`cyber_data_collector/utils/safety_vault.py`](cyber_data_collector/utils/safety_vault.py).
Tests: [`cyber_data_collector/tests/test_safety_net.py`](cyber_data_collector/tests/test_safety_net.py).

### OAIC Data Collection

OAIC (Office of the Australian Information Commissioner) publishes semi-annual Notifiable Data Breaches reports. Update when new reports are released (H1: August/September, H2: February/March).

The OAIC dashboard scraper extracts these fields per semester (page 2-9):

| Field | Source page | Notes |
|---|---|---|
| `total_notifications` | 2 | Big KPI |
| `monthly_notifications` | 2 | One bar per month |
| `human_error_pct` / `malicious_attacks_pct` / `system_faults_pct` | 2 | Donut |
| `cyber_incidents` (phishing/ransomware/etc. %) | 2 | Bar chart |
| `top_sectors[].notifications` | 2 | Top-5 sectors with COUNTS |
| `small_breaches_100_or_fewer_pct` | 2 | KPI |
| `human_error_causes` | 2 | Top-3 causes |
| `individuals_affected_distribution` | 4 | 13 buckets, real OAIC labels |
| `large_scale_australians` | 4 | Right-side table |
| `personal_info_types` | 5 | Contact / Identity / Financial / Health / TFN / Other / CDR / Digital ID |
| `breach_sources` | 6 | Human / Malicious / System current+previous |
| `time_to_identify_pct` | 7 | 5 buckets `Unknown` / `<= 10 days` / `11-20 days` / `21-30 days` / `> 30 days` |
| `time_to_notify_pct` | 8 | Same 5 buckets |
| `sector_by_source` | 9 | 5×3 matrix (still WIP - filter-click bug) |

Every per-page extraction is verified against the dashboard's own
"Show results for ..." semester label so silent semester-selection failures
can't mislabel data.

```bash
# Scrape statistics from OAIC PDF reports (older mechanism, less detailed)
python scripts/oaic/oaic_data_scraper.py --start-year 2024 --end-year 2025 --output json

# Scrape OAIC Power BI dashboard (Playwright + GPT-4o Vision).
# Periods up to Jan-Jun 2025 only - the dashboard is discontinued after that.
# Needed when rebuilding/repairing older periods, not for routine refreshes.
python scripts/oaic/OAIC_dashboard_scraper.py --from-year 2022
python scripts/oaic/OAIC_dashboard_scraper.py --semester "Jan-Jun 2025"

# Ingest OAIC statistics published on data.gov.au (2025 H2 onward - see below)
python scripts/oaic/oaic_datagov_scraper.py --existing-data oaic_cyber_statistics_<latest>.json

# Validate every OAIC file (exit 1 on a corrupt file or a published contradiction)
python scripts/oaic/cleanup_oaic_data.py

# Routine refresh: data.gov.au -> checked -> published dataset (exit 1 on any failure)
python scripts/oaic/refresh_oaic_statistics.py
```

#### Published OAIC dataset: validated, promoted only on pass, dated

The dashboard reads one published dataset, `instance/oaic/oaic_statistics.json`
(`scripts/oaic/oaic_dataset.py`). Every ingester (data.gov.au, Power BI, PDF)
promotes into it through the same internal-consistency rules
(`scripts/oaic/oaic_consistency.py`): source split vs total, monthly counts vs
the semester, sector / method / individuals-affected parts within their whole,
cyber incidents within malicious attacks, percentages within 0-100. A record
that fails is rejected with its reasons and its period keeps the last valid
record; the file is replaced atomically and never by a failed run. Each attempt
and each period's last successful refresh are recorded in
`instance/oaic/oaic_refresh_state.json`, and the dashboard shows "OAIC
statistics last refreshed <date> (latest period <period>)", plus a warning when
the last attempt failed. The first refresh bootstraps the dataset from the legacy
`oaic_cyber_statistics_*.json` files.

Contradictions are never published: when files or the dataset are read, every
figure involved in a contradiction is withheld (the whole period if it
contradicts its own total) and listed on the dashboard. Legacy files are merged
only between records that report the same total (2022 H1 used to combine a
250-notification PDF total with a 397-notification Power BI monthly series).
Power BI per-method counts are no longer estimated (they were percentages of
cyber incidents multiplied by malicious attacks, ~1.6x too high); they are
stored as `None` with the published percentages in `cyber_incident_method_pct`,
and older Power BI method counts are withheld. A malformed semester is an
error, not "the current year's H1"; the Power BI semester list is derived and
stops at Jan-Jun 2025 (later periods come from data.gov.au).

#### Where each OAIC period comes from

OAIC has changed publication channel twice, and **each channel stopped being
updated rather than being backfilled**, so all three scrapers remain necessary:

| Periods | Channel | Script |
|---|---|---|
| up to 2024 H2 | PDF reports on the OAIC publications page | `oaic_data_scraper.py` |
| up to 2025 H1 | Power BI statistics dashboard | `OAIC_dashboard_scraper.py` |
| 2025 H2 onward | XLSX resource on data.gov.au | `oaic_datagov_scraper.py` |

The Power BI dashboard has been **discontinued** and ceased to be the source
for OAIC statistics after June 2025: its last period is Jan-Jun 2025, and it
will not receive July-December 2025 or later data. `OAIC_dashboard_scraper.py`
is still required to (re)build periods up to 2025 H1, but on a routine refresh
it can only re-scrape stale periods and will silently report "no new data".
For every period from July 2025 onward, use `oaic_datagov_scraper.py` against
the data.gov.au [Notifiable Data Breaches (NDB) scheme](https://data.gov.au/data/dataset/notifiable-data-breaches-ndb-scheme)
dataset. The ingester discovers resources through the CKAN API rather than
hard-coded URLs, so a future Jan-Jun 2026 release is picked up without a code
change. (Jul-Dec 2025 appeared there on 29 June 2026, roughly six months after
the period closed.)

Two caveats on data.gov.au periods:

- The per-method cyber-incident split (phishing / ransomware / hacking /
  brute force / malware / compromised credentials) is **not published** there
  and is stored as `null`, not zero. Those dashboard series have a genuine gap
  from 2025 H2.
- `cyber_incidents_total` is set to the malicious-or-criminal-attack count to
  match how every dashboard-scraped period from 2023 H1 onward populates it.
  OAIC's narrower "Cyber incident" sub-source is preserved separately as
  `cyber_incident_only` (253 for 2025 H2, against 405 malicious attacks).

### Export

```bash
# Excel export with LLM summaries, anonymisation and an Export Notes column
python scripts/export/export_events_excel.py
python scripts/export/export_events_excel.py --exclude-unknown-records --limit 100

# Full database export (all columns)
python scripts/export/export_cyber_events.py --format csv --output events.csv --detailed
python scripts/export/export_cyber_events.py --format excel --output events.xlsx --detailed --anonymize
```

An anonymised column never carries the original text. A failed anonymisation
(LLM error, empty answer, an answer still naming the victim or attacker or
repeating the title, a worker crash) writes `[anonymisation failed] (reason)`,
is counted, and makes the script exit 1; `--no-llm` writes `[not anonymised]`;
an LLM export without `OPENAI_API_KEY` fails instead of falling back to raw
text. `--anonymize` requires `--detailed` and drops the identifying columns
(title, victim, attacker, entity and source JSON, and the evidence-status
reason, which names pages). Both exports carry each incident's
`evidence_status` (`unsupported` = every field uncertain). Source text comes only from
exact relationships (`EventDeduplicationMap`, `DeduplicatedEventSources`) -
never title matching; an event with none says so in Export Notes.

### Setup

```bash
# BigQuery authentication (GDELT only — expensive)
python scripts/setup/setup_bigquery_auth.py
```

---

## Database Schema

### Core Tables

| Table | Description |
|-------|-------------|
| `RawEvents` | Discovered events before processing |
| `EnrichedEvents` | LLM-processed events with metadata |
| `DeduplicatedEvents` | Final unique events after merging |
| `ASDRiskClassifications` | ASD C1–C6 risk matrix assignments |
| `EntitiesV2` | Organizations and entities mentioned, with `entity_kind` and the ordinal `size_estimate` |
| `EnrichmentAuditTrail` | Full audit trail for all enrichment stages |

---

## Monthly Update Guide

### Standard Monthly Refresh (Recommended)

```bash
python pipeline.py refresh
```

### Comprehensive Monthly Update

```bash
python run_full_pipeline.py --source Perplexity OAIC GoogleSearch WebberInsurance --days 30 --max-events 500
```

### Full Rebuild (Rare)

```bash
python pipeline.py rebuild --force
```

### Scheduled Task (Windows Task Scheduler)

```
python d:\dev\australian-cyber-events-scraper\run_full_pipeline.py --source Perplexity OAIC GoogleSearch WebberInsurance --days 30 --max-events 500
```

### Cron Job (Linux/Mac, first Monday of each month)

```cron
0 2 1-7 * 1 cd /path/to/australian-cyber-events-scraper && python run_full_pipeline.py --source Perplexity OAIC GoogleSearch WebberInsurance --days 30 --max-events 500
```

---

## Development

```bash
pytest                              # Run all tests
pytest cyber_data_collector/tests/  # Run specific module
pytest --cov=cyber_data_collector   # With coverage report
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| LLM API rate limits | Reduce `MAX_THREADS` in `.env` |
| Database lock errors | Ensure only one pipeline instance runs at a time |
| Scraping failures | Check Playwright installation: `playwright install chromium` |
| BigQuery auth errors | Run `python scripts/setup/setup_bigquery_auth.py` |
| Memory issues | Reduce `BATCH_SIZE` in `.env` or process smaller date ranges |

Check `logs/unified_pipeline.log` for detailed processing information.

---

## Documentation

Detailed specifications for each pipeline stage are in the `docs/` directory:

| Document | Description |
|----------|-------------|
| [01-data-sources.md](docs/01-data-sources.md) | Data source descriptions, API configurations, and justifications |
| [02-discovery-collection.md](docs/02-discovery-collection.md) | Discovery pipeline, search queries, and LLM prompts |
| [03-content-scraping.md](docs/03-content-scraping.md) | Playwright scraping, content extraction, and fallback strategies |
| [04-event-filtering.md](docs/04-event-filtering.md) | Jev page gate (Random Forest retired), confidence scoring, and LLM classification |
| [05-event-enrichment.md](docs/05-event-enrichment.md) | 5-stage enrichment pipeline with Perplexity and GPT-4o |
| [06-deduplication.md](docs/06-deduplication.md) | Entity-based deduplication, similarity matching, and merge logic |
| [07-ASD-risk-classification.md](docs/07-ASD-risk-classification.md) | ASD risk matrix framework and GPT-4o classification |
| [08-dashboard-generation.md](docs/08-dashboard-generation.md) | Dashboard SQL queries, Chart.js charts, and OAIC comparison |
| [10-quality-gate.md](docs/10-quality-gate.md) | Continuous quality metrics, label regression, synthetic cases and the publication gate |
| [10-supplier-incidents.md](docs/10-supplier-incidents.md) | Supplier incidents: co-victims, same-incident merges, calibration, counting once per organisation |
| [11-evidence-recovery.md](docs/11-evidence-recovery.md) | Incidents with no usable evidence: diagnosis, retrieval through existing queues, `evidence_status` |
| [data-dictionary.md](docs/data-dictionary.md) | Complete database schema, table definitions, and field descriptions |

These documents contain sufficient detail for independent review and replication of each pipeline stage.

---

**Built for Australian cyber security research and monitoring.**
