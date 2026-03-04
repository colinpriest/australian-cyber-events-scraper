# Discovery and Collection Stage

This document provides a detailed specification of the discovery and collection stage of the Australian Cyber Events pipeline. It covers the data sources, search criteria, scraping methods, filtering logic, LLM prompts, and data flow in sufficient detail for independent review and replication.

---

## Table of Contents

1. [Purpose and Scope](#1-purpose-and-scope)
2. [High-Level Data Flow](#2-high-level-data-flow)
3. [Data Sources](#3-data-sources)
   - 3.1 [Perplexity AI Search](#31-perplexity-ai-search)
   - 3.2 [OAIC Media Centre](#32-oaic-media-centre)
   - 3.3 [Google Custom Search](#33-google-custom-search)
   - 3.4 [Webber Insurance Data Breaches](#34-webber-insurance-data-breaches)
   - 3.5 [GDELT BigQuery](#35-gdelt-bigquery-optional)
4. [URL Scraping (Content Acquisition)](#4-url-scraping-content-acquisition)
5. [Multi-Stage Filtering](#5-multi-stage-filtering)
   - 5.1 [Random Forest ML Filter](#51-random-forest-ml-filter)
   - 5.2 [Confidence-Based Filter](#52-confidence-based-filter)
   - 5.3 [LLM Classification Filter](#53-llm-classification-filter)
6. [LLM Processing](#6-llm-processing)
   - 6.1 [Entity Extraction](#61-entity-extraction)
   - 6.2 [Event Classification and Enhancement](#62-event-classification-and-enhancement)
   - 6.3 [Content Analysis (Fast Pass)](#63-content-analysis-fast-pass)
7. [Month-by-Month Processing](#7-month-by-month-processing)
8. [Duplicate Detection](#8-duplicate-detection)
9. [Data Storage](#9-data-storage)
10. [Configuration and Thresholds](#10-configuration-and-thresholds)
11. [Error Handling and Resilience](#11-error-handling-and-resilience)
12. [Justification of Design Decisions](#12-justification-of-design-decisions)
13. [Replication Guide](#13-replication-guide)

---

## 1. Purpose and Scope

The discovery and collection stage is responsible for:

- **Discovering** cyber security incidents affecting Australian entities from multiple independent data sources.
- **Scraping** full article content from discovered URLs using browser automation.
- **Filtering** non-relevant content through machine learning and keyword-based scoring.
- **Extracting** structured metadata (entities, dates, severity, event types) using LLM analysis.
- **Storing** raw and enriched events in a SQLite database for downstream processing.

This stage corresponds to Phase 1 of the five-phase pipeline. It is invoked by `run_full_pipeline.py` and orchestrated by the `EventDiscoveryEnrichmentPipeline` class in `cyber_data_collector/pipelines/discovery.py`.

### What This Stage Does NOT Do

This stage does not perform Perplexity AI enrichment (Phase 2), global deduplication (Phase 3), ASD risk classification (Phase 4), or dashboard generation (Phase 5). Those are separate pipeline phases documented elsewhere.

---

## 2. High-Level Data Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                     For each month in range:                        │
│                                                                     │
│  ┌─────────┐ ┌──────┐ ┌────────┐ ┌────────┐ ┌───────┐             │
│  │Perplexity│ │ OAIC │ │ Google │ │ Webber │ │ GDELT │             │
│  │   AI     │ │      │ │ Search │ │  Ins.  │ │  (BQ) │             │
│  └────┬─────┘ └──┬───┘ └───┬────┘ └───┬────┘ └───┬───┘             │
│       │          │         │          │          │                  │
│       └──────────┴─────────┴──────────┴──────────┘                  │
│                          │                                          │
│                          ▼                                          │
│               ┌──────────────────┐                                  │
│               │ Store Raw Events │  (duplicate check by URL+title)  │
│               └────────┬─────────┘                                  │
│                        │                                            │
│                        ▼                                            │
│               ┌──────────────────┐                                  │
│               │ Playwright URL   │  (with Perplexity fallback)      │
│               │ Scraping         │                                  │
│               └────────┬─────────┘                                  │
│                        │                                            │
│                        ▼                                            │
│               ┌──────────────────┐                                  │
│               │ Random Forest    │  (ML content filter)             │
│               │ Filter           │                                  │
│               └────────┬─────────┘                                  │
│                        │                                            │
│                        ▼                                            │
│               ┌──────────────────┐                                  │
│               │ Entity Extraction│  (GPT-4o-mini)                   │
│               └────────┬─────────┘                                  │
│                        │                                            │
│                        ▼                                            │
│               ┌──────────────────┐                                  │
│               │ LLM Classifier   │  (GPT-4o-mini)                   │
│               │ (Accept/Reject)  │                                  │
│               └────────┬─────────┘                                  │
│                        │                                            │
│                        ▼                                            │
│               ┌──────────────────┐                                  │
│               │ Store Enriched   │  (confidence threshold ≥ 0.6)    │
│               │ Events           │                                  │
│               └──────────────────┘                                  │
│                                                                     │
│               Mark month as processed                               │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. Data Sources

The pipeline collects events from five independent data sources. Each source provides different coverage characteristics, and using multiple sources provides corroboration and reduces blind spots.

### Default Sources

By default, four sources are enabled: Perplexity, OAIC, GoogleSearch, WebberInsurance. GDELT is disabled by default due to BigQuery costs.

### 3.1 Perplexity AI Search

**Source file:** `cyber_data_collector/datasources/perplexity.py`

**What it is:** Perplexity AI is an AI-powered search engine that synthesizes information from multiple web sources in real time. The pipeline uses it as a primary discovery mechanism to find Australian cyber incidents.

**Why it is used:** Perplexity provides AI-curated search results that consolidate information across many sources, making it effective at discovering incidents that may be reported by niche or regional outlets. Its ability to synthesize across sources reduces the need to scrape dozens of individual news sites.

**API details:**
- **Endpoint:** `https://api.perplexity.ai` (OpenAI-compatible API)
- **Model:** `sonar-pro`
- **Temperature:** 0.1 (low randomness for factual consistency)
- **Max tokens:** 4,000
- **Cost:** Approximately $0.005 per query

**Search queries:** Seven queries are generated per month, each combining a topic focus with a date range filter. The date range is expanded by 2 months before the target month to catch late-reported events.

The exact query templates are:

```
1. "Australian cyber attack {date_range} data breach security incident"
2. "Australia cybersecurity breach {date_range} ransomware malware"
3. "Australian company cyber incident {date_range} hacking data leak"
4. "Australia government cyber attack {date_range} infrastructure security"
5. "Australian bank financial cyber breach {date_range} fraud security"
6. "Australia healthcare cyber attack {date_range} medical data breach"
7. "Australian university cyber incident {date_range} education security"
```

Where `{date_range}` is replaced with `after:MM/DD/YYYY before:MM/DD/YYYY` date filters.

**Justification for query design:** The seven queries are designed to cover the major sectors affected by Australian cyber incidents: general, government, financial, healthcare, and education. Each query uses overlapping but distinct keyword combinations to maximise recall. The expanded 3-month search window accounts for the documented pattern where incidents are often reported weeks or months after they occur.

**System prompt (verbatim):**

```
You are a cybersecurity analyst. Extract detailed information about Australian
cyber security incidents from search results.

Return your response as a JSON object with this exact structure:
{
    "events": [
        {
            "title": "Event title here",
            "description": "Event description here",
            "event_date": "YYYY-MM-DD format or null",
            "entity_name": "Affected entity name or null",
            "event_type": "Type of cyber event or null",
            "impact_description": "Description of impact or null",
            "source_urls": ["url1", "url2"]
        }
    ]
}

Only include real cyber security incidents related to Australia. If no relevant
incidents are found, return {"events": []}.
```

**JSON recovery system:** Because LLM responses can be truncated or malformed, the source implements a four-tier JSON recovery system:

1. **Tier 1:** Strip markdown code block wrappers (```` ```json ... ``` ````) and parse.
2. **Tier 2:** Fix truncated JSON by closing unclosed brackets/braces. Uses three strategies: find last complete event object boundary, find last `}` and close outer structure, or count open brackets and close them.
3. **Tier 3:** Retry the query with a strict JSON-only system prompt that explicitly prohibits markdown formatting. Uses temperature 0.0 for deterministic output.
4. **Tier 4:** Extract partial events using regex to find individual field values (`"title"`, `"description"`, etc.) from malformed JSON, then send them to Perplexity for completion and verification.

**Resilience features:**
- Exponential backoff retry: 3 retries maximum, 2-second base delay, 2x multiplier, 60-second maximum delay, with jitter to prevent thundering herd.
- Circuit breaker: After 5 consecutive failures, API calls are skipped for 5 minutes.
- Error categorisation: Authentication errors (401/403) are not retried. Rate limit errors (429) get extended 30-second delays. Server errors (5xx) and network errors are retried.

**Confidence scores assigned:**
- Overall: 0.65
- Source reliability: 0.6
- Data completeness: 0.6
- Temporal accuracy: 0.6
- Geographic accuracy: 0.7

---

### 3.2 OAIC Media Centre

**Source file:** `cyber_data_collector/datasources/oaic.py`

**What it is:** The Office of the Australian Information Commissioner (OAIC) is the Australian government body responsible for privacy regulation. Its media centre publishes regulatory actions, civil penalties, investigations, and enforcement actions related to data breaches and privacy incidents.

**Why it is used:** OAIC is the authoritative source for Australian privacy enforcement actions. These are high-confidence, verified events because they represent official regulatory outcomes. No other data source provides this coverage.

**Collection method:** Web scraping (no API available).

**Target URL:** `https://www.oaic.gov.au/news/media-centre?query=&sort=dmetapublishedDateISO&num_ranks=1000`

**Article link extraction:** The scraper uses two methods:

1. **Structured results extraction:** Searches HTML containers (`div`, `li`, `article`) for elements containing both a link and a date pattern (e.g., `14 March 2025`). Links are filtered by keyword and URL path.

2. **Keyword-based fallback:** Scans all `<a>` elements on the page, keeping those where the link text (minimum 20 characters) contains at least one of these keywords:

   ```
   cyber, data breach, privacy, security, hack, attack, civil penalty,
   enforcement, investigation, determination, enforceable undertaking,
   compliance, breach, incident
   ```

   And excludes links containing:

   ```
   privacy policy, your privacy rights, privacy complaints,
   australian privacy principles, privacy guidance, privacy legislation
   ```

   And the href must contain `/news/`, `/media-centre/`, or `s/redirect`.

**Date filtering:** Publication dates from the search results page are compared against the target month with a 2-month expansion window (same rationale as Perplexity).

**Duplicate skipping:** Before scraping, the source checks discovered URLs against a set of already-known URLs from the database (`get_known_source_urls('OAIC')`). Known URLs are skipped.

**Redirect handling:** OAIC uses redirect URLs (`/s/redirect?url=...`). The scraper extracts the actual destination URL from the query parameter.

**Article scraping:** Each article page is fetched with HTTP GET and parsed with BeautifulSoup. The scraper extracts:
- Title from `<h1>` tag
- Content from semantic selectors: `main`, `article`, `.content`, `.main-content`, `.article-content`, `.news-content`
- Publication date from meta tags (`DC.Date`, `article:published_time`) or regex patterns in the content

**Entity extraction from titles:** Regex patterns match phrases like "action against X", "penalty against X", "X data breach", "X privacy breach". Falls back to extracting the first capitalised multi-word phrase from the title.

**Event classification:**
- Event type: `DATA_BREACH` if title contains "data breach", "cyber incident", or "hack"; otherwise `OTHER`.
- Severity: `CRITICAL` if "civil penalty" appears in the title; otherwise `HIGH`.

**Confidence scores assigned:**
- Overall: 0.9
- Source reliability: 0.95 (government source)
- Data completeness: 0.8
- Temporal accuracy: 0.85
- Geographic accuracy: 1.0 (OAIC only regulates Australian entities)

---

### 3.3 Google Custom Search

**Source file:** `cyber_data_collector/datasources/google_search.py`

**What it is:** Google Custom Search API provides programmatic access to Google search results, configured with a custom search engine (CX) ID.

**Why it is used:** Google Search casts a wide net across the open web, catching incidents reported by smaller or regional publications that Perplexity may not surface. It serves as a breadth complement to the depth of Perplexity.

**API details:**
- **Endpoint:** `https://www.googleapis.com/customsearch/v1`
- **Free tier:** 100 queries per day (resets at midnight Pacific time)
- **Pagination:** Up to 5 pages (50 results) per query
- **Rate limiting:** 1-second delay between paginated requests

**Search queries (verbatim):**

```
1. australian cybersecurity ("data breach" OR "ransomware" OR "cyber attack")
2. australia "data breach" notification privacy commissioner
3. australian company "cyber incident" OR "security breach"
4. australia government "cyber attack" OR "security incident"
```

**Justification for query design:** The queries use Google's exact-match quoting and Boolean OR operators to maintain precision. Query 2 specifically targets OAIC-related coverage (privacy commissioner mentions), providing cross-validation with the OAIC source. Queries 3 and 4 target corporate and government incidents respectively.

**Date filtering:** A Google date sort filter is applied: `date:r:YYYYMMDD:YYYYMMDD`.

**Quota management:** Thread-safe class-level tracking of daily quota usage. If any request returns HTTP 429, 403 with quota/limit error messages, or a successful response containing an error object with quota-related messages, the daily quota flag is set and all remaining Google queries for that day are skipped. The flag resets automatically at midnight.

**Australian relevance detection:** Events are flagged as Australian if the title or snippet contains "australia" (case-insensitive), or the URL domain ends with `.com.au`, `.net.au`, `.org.au`, `.gov.au`, or `.edu.au`.

**Confidence scores assigned:**
- Overall: 0.5
- Source reliability: 0.5
- Data completeness: 0.4
- Temporal accuracy: 0.4
- Geographic accuracy: 0.5

The lower confidence scores reflect the fact that Google Search results are unverified and may include tangential or irrelevant content.

---

### 3.4 Webber Insurance Data Breaches

**Source file:** `cyber_data_collector/datasources/webber_insurance.py`

**What it is:** Webber Insurance maintains a curated list of Australian data breaches at `https://www.webberinsurance.com.au/data-breaches-list`, organised by entity name and date with links to detailed event pages.

**Why it is used:** This is a manually curated, Australian-specific list of data breaches maintained by a domain expert (an insurance provider specialising in cyber insurance). All events on this list are pre-verified as Australian data breaches, giving very high geographic accuracy and relevance.

**Collection method:** Two-stage web scraping.

**Stage 1 — List page extraction:** The main page is structured with `<h3>` section headers containing entity names and dates (e.g., "Vertel – June 2025"). The scraper:
1. Finds all `<h3>` headers.
2. Parses dates from headers using the regex pattern: `(January|February|...|December)\s+(\d{4})`. When only a month and year are provided, the 15th of that month is used as the default day.
3. For each header, traverses subsequent sibling elements until the next `<h3>`, collecting `<a>` links whose text (minimum 20 characters) contains keywords: `breach`, `cyber`, `hack`, `attack`, `ransomware`, `incident`, excluding `guide`, `ultimate`, `notification laws`, `essentials`.
4. Filters by section date: only sections whose date falls within the target month's date range are scraped further.

**Stage 2 — Detail page scraping:** Each event's detail page is fetched via HTTP GET and parsed:
- Title from `<h1>` tag
- Content from `<article>`, `div.content`, or full page body
- Entity name extracted from the title text before the first dash/em-dash character

**Date parsing:** Multiple strategies are attempted in order:
1. Regex patterns in page text: `19 Jun 2025`, `June 19, 2025`, `2025-06-19`, `19/06/2025`
2. URL patterns: `20_06_2025` in path, `campaign=20_06_2025` in query parameters
3. dateutil library parsing of extracted date strings
4. Fallback: section header date from Stage 1

**Perplexity fallback:** If a detail page returns an HTTP error (403, timeout, etc.), the scraper falls back to Perplexity AI to research the incident. The fallback prompt is:

```
System: You are a cybersecurity news specialist. When given an inaccessible URL
about a cybersecurity incident, you research and provide detailed information
about that incident.

IMPORTANT:
- Analyze the URL to understand what incident it was covering
- Use any date context provided to help identify the specific incident
- Provide factual information about the cybersecurity incident
- Include company name, type of breach, impact, and timeline if available
- Focus on verified information from reputable sources

Return a comprehensive summary of the incident in paragraph form.

User: Find information about the cybersecurity incident that was originally
reported at this URL: {failed_url}

This incident occurred around {month year}

I need you to:
1. Identify what specific cybersecurity/data breach story this URL was about
2. Find information about the SAME incident from reliable sources
3. Provide a summary of the incident including company name, what happened, and impact
4. Focus on factual details about the breach/incident

The original URL is inaccessible, so I need the actual story content.
```

**Confidence scores assigned:**
- Overall: 0.85
- Source reliability: 0.8
- Data completeness: 0.7
- Temporal accuracy: 0.9
- Geographic accuracy: 1.0 (Australian-only source)

All events are classified as `DATA_BREACH` with `MEDIUM` severity by default.

---

### 3.5 GDELT BigQuery (Optional)

**Source file:** `cyber_data_collector/datasources/gdelt.py`

**What it is:** The GDELT Project (Global Database of Events, Language, and Tone) is an open database that monitors global news media in real time, encoding events using the CAMEO (Conflict and Mediation Event Observations) coding system. Access is via Google BigQuery.

**Why it is used:** GDELT provides structured event data from a vast corpus of global media, enabling discovery of incidents that may not appear in keyword-based web searches. Its CAMEO coding provides event type classification at the data source level.

**Status:** Disabled by default. BigQuery costs can reach $500-$2,000+ per pipeline run depending on query volume.

**BigQuery SQL query (verbatim):**

```sql
SELECT
    GLOBALEVENTID, DATEADDED, Actor1Name, Actor2Name,
    Actor1CountryCode, Actor2CountryCode, EventBaseCode, EventCode,
    GoldsteinScale, NumSources, SourceURL,
    ActionGeo_CountryCode, ActionGeo_FullName,
    ActionGeo_Lat, ActionGeo_Long
FROM `gdelt-bq.gdeltv2.events`
WHERE CAST(DATEADDED AS STRING) >= @start_date
  AND CAST(DATEADDED AS STRING) <= @end_date
  AND (
    ActionGeo_CountryCode = 'AS' OR
    Actor1CountryCode = 'AS' OR
    Actor2CountryCode = 'AS'
  )
  AND (
    (LOWER(Actor1Name) LIKE '%data breach%' OR LOWER(Actor2Name) LIKE '%data breach%') OR
    (LOWER(Actor1Name) LIKE '%cyber breach%' OR LOWER(Actor2Name) LIKE '%cyber breach%') OR
    (LOWER(Actor1Name) LIKE '%security breach%' OR LOWER(Actor2Name) LIKE '%security breach%') OR
    (LOWER(Actor1Name) LIKE '%ransomware%' OR LOWER(Actor2Name) LIKE '%ransomware%') OR
    (LOWER(Actor1Name) LIKE '%malware%' OR LOWER(Actor2Name) LIKE '%malware%') OR
    (LOWER(Actor1Name) LIKE '%cyber attack%' OR LOWER(Actor2Name) LIKE '%cyber attack%') OR
    (LOWER(Actor1Name) LIKE '%cyberattack%' OR LOWER(Actor2Name) LIKE '%cyberattack%') OR
    (LOWER(Actor1Name) LIKE '%phishing%' OR LOWER(Actor2Name) LIKE '%phishing%') OR
    (LOWER(Actor1Name) LIKE '%ddos%' OR LOWER(Actor2Name) LIKE '%ddos%') OR
    (LOWER(Actor1Name) LIKE '%credential%' OR LOWER(Actor2Name) LIKE '%credential%') OR
    (LOWER(Actor1Name) LIKE '%hack%' OR LOWER(Actor2Name) LIKE '%hack%') OR
    (LOWER(Actor1Name) LIKE '%vulnerability%' OR LOWER(Actor2Name) LIKE '%vulnerability%') OR
    (LOWER(Actor1Name) LIKE '%exploit%' OR LOWER(Actor2Name) LIKE '%exploit%') OR
    EventCode LIKE '141%' OR
    EventCode LIKE '172%' OR
    EventCode LIKE '210%'
  )
  AND NOT (
    LOWER(Actor1Name) LIKE '%firework%' OR LOWER(Actor2Name) LIKE '%firework%' OR
    LOWER(Actor1Name) LIKE '%celebration%' OR LOWER(Actor2Name) LIKE '%celebration%' OR
    LOWER(Actor1Name) LIKE '%new year%' OR LOWER(Actor2Name) LIKE '%new year%' OR
    LOWER(Actor1Name) LIKE '%holiday%' OR LOWER(Actor2Name) LIKE '%holiday%' OR
    LOWER(Actor1Name) LIKE '%festival%' OR LOWER(Actor2Name) LIKE '%festival%' OR
    LOWER(Actor1Name) LIKE '%concert%' OR LOWER(Actor2Name) LIKE '%concert%' OR
    LOWER(Actor1Name) LIKE '%sports%' OR LOWER(Actor2Name) LIKE '%sports%' OR
    LOWER(Actor1Name) LIKE '%election%' OR LOWER(Actor2Name) LIKE '%election%'
  )
  AND NumSources >= 2
ORDER BY DATEADDED DESC
LIMIT @max_records
```

**Parameters:**
- `@start_date`: Start of target month in `YYYYMMDDHHmmss` format
- `@end_date`: End of target month in `YYYYMMDDHHmmss` format
- `@max_records`: Maximum rows to return (default: 1,000)

**Australian filtering:** Events must have at least one of: `ActionGeo_CountryCode = 'AS'`, `Actor1CountryCode = 'AS'`, or `Actor2CountryCode = 'AS'` (where 'AS' is the FIPS country code for Australia in GDELT).

**Keyword inclusion (14 patterns):** `data breach`, `cyber breach`, `security breach`, `ransomware`, `malware`, `cyber attack`, `cyberattack`, `phishing`, `ddos`, `credential`, `hack`, `vulnerability`, `exploit`.

**CAMEO code inclusion:** `141x` (data theft/information gathering), `172x` (state-level coercion/cyber operations), `210x` (critical infrastructure disruption).

**Exclusion keywords (8 patterns):** `firework`, `celebration`, `new year`, `holiday`, `festival`, `concert`, `sports`, `election`.

**Credibility threshold:** `NumSources >= 2` (requires at least 2 independent sources reporting the event).

**CAMEO-to-event-type mapping:**
- `172x` → `STATE_SPONSORED_ATTACK`
- `210x` → `INFRASTRUCTURE_ATTACK`
- `141x` → `DATA_BREACH`
- Other → `OTHER`

**Credibility calculation:** `min(NumSources × 0.2, 1.0)`, floored at 0.3.

---

## 4. URL Scraping (Content Acquisition)

**Source file:** `cyber_data_collector/utils/entity_scraper.py`

After raw events are stored, the pipeline scrapes the full content from each event's source URL using Playwright (headless Chromium). This step is essential because most data sources only provide titles and brief snippets; full article content is needed for accurate LLM classification.

### Browser Configuration

Playwright is launched with these Chromium flags to avoid detection:
```
--disable-blink-features=AutomationControlled
--disable-dev-shm-usage
--disable-infobars
--no-sandbox
--disable-setuid-sandbox
```

### Anti-Detection Measures

Each page request uses a randomised browser context with:

- **User agents:** Rotated from a pool of 11 current browser signatures (Chrome 130/131, Firefox 132/133, Safari 18.x, Edge 130/131).
- **Viewport:** Randomly selected from `{1920×1080, 1366×768, 1440×900}`.
- **Locale:** `en-AU` with timezone `Australia/Sydney`.
- **HTTP headers:** Full set of modern browser headers including `Sec-Fetch-*`, `DNT`, and `Cache-Control`.
- **Stealth JavaScript:** Injected on page load to override `navigator.webdriver`, set realistic `navigator.plugins` (Chrome PDF Plugin, Chrome PDF Viewer, Native Client), override `navigator.languages` to `['en-AU', 'en-US', 'en']`, and set realistic `hardwareConcurrency` (8), `deviceMemory` (8), and `platform` ('Win32').
- **Human-like scrolling:** After page load, the scraper scrolls incrementally in random 300-500px steps with 0.2-0.6s delays between steps.
- **Random delays:** 3-7 second wait after initial page load before content extraction.
- **Google referrer:** All navigation uses `referer: "https://www.google.com/"`.

### Content Extraction

Content is extracted using a prioritised list of CSS selectors:

```
article, main, .post-content, .entry-content, .content,
div[role='main'], #main-content, #content, .article-content,
.story-content, .article-body, .article__body, .story__body,
.news-article, .article-text, #article-body, .body-content,
.blog-post, .post-body, .page-content, #page-content,
.single-post-content, .wysiwyg-content, .rich-text,
.publication-content, .page-body, .gov-content, #main,
.rte-content, .field-body, .body-field,
[itemprop='articleBody'], [class*='article'], [class*='content']
```

Each selector is tried in order. A selector is accepted only if it yields more than 200 characters of text. If no selector matches, the full `<body>` text is used as a fallback.

### Australian News Site Handling

Special strategies are applied for known Australian news domains:
- `abc.net.au`, `news.com.au`, `theage.com.au`, `smh.com.au`, `theaustralian.com.au`, `theguardian.com/australia-news`, `thenewdaily.com.au`, `canberratimes.com.au`, `adelaidenow.com.au`, `heraldsun.com.au`, `couriermail.com.au`, `perthnow.com.au`, `ntnews.com.au`, `themercury.com.au`, `thewest.com.au`

For The New Daily (`thenewdaily.com.au`), additional headers are set including `Sec-GPC: 1`. For ABC News (`abc.net.au`), an extra 2-4 second delay is added before navigation.

### 403 Error Handling

On HTTP 403 responses, the scraper:
1. Waits 8-15 seconds.
2. Creates a new browser context with a different user agent, viewport (1366×768), and Australian Google referrer (`https://www.google.com.au/`).
3. Waits for `networkidle` instead of `domcontentloaded`.
4. If the retry also fails, falls back to Perplexity.

### PDF Support

URLs ending in `.pdf` are handled by a separate `PDFExtractor` module rather than Playwright.

### Perplexity AI Fallback

When direct scraping fails (timeouts, 403/404 errors, or sites known to block scrapers), the scraper uses Perplexity AI to find alternative coverage of the same story:

**Stubborn site domains (Perplexity tried first):**
```
nytimes.com, reuters.com, facebook.com, twitter.com, linkedin.com,
news.com.au, theaustralian.com.au, afr.com, wsj.com, ft.com, bloomberg.com
```

**Perplexity fallback system prompt:**

```
You are a news research specialist. When given a broken/inaccessible URL, you
find working alternative URLs that cover the exact same news story.

IMPORTANT:
- Analyze the provided URL carefully to understand what story it was about
- Use the date context to narrow down the timeframe
- Find the SAME story from different reputable news sources
- Return actual working URLs, not the broken one
- Focus on cybersecurity/data breach stories if indicated

Return your response as a simple list of alternative URLs, one per line. Only
include URLs that you're confident cover the same story.
```

The fallback tries up to 3 alternative URLs from Perplexity's response, preferring results from these domains:
```
abc.net.au, news.com.au, theage.com.au, smh.com.au, theguardian.com,
reuters.com, bbc.com, cnn.com, zdnet.com, techcrunch.com,
ars-technica.com, wired.com, cybersecuritydive.com,
securityweek.com, darkreading.com
```

### Parallel Processing

URL scraping runs concurrently using `asyncio.as_completed()` with a per-task timeout of 120 seconds. A tqdm progress bar tracks completion.

---

## 5. Multi-Stage Filtering

The pipeline applies multiple filtering stages to remove non-relevant content while minimising false negatives (discarding real cyber events).

### 5.1 Random Forest ML Filter

**Source file:** `cyber_data_collector/filtering/rf_event_filter.py`

**What it is:** A trained Random Forest classifier that predicts whether a raw event is a genuine cyber security incident based on text features.

**Why it is used:** ML-based filtering provides a data-driven alternative to keyword heuristics. The Random Forest model was trained on labelled examples of cyber events vs. non-cyber content from previous pipeline runs, giving it the ability to recognise patterns that keyword lists might miss.

**Model artifacts (in `machine_learning_filter/` directory):**
- `random_forest_filter.pkl` — Trained scikit-learn Random Forest model
- `source_type_encoder.pkl` — Label encoder for source type categorical feature
- `text_vectorizer.pkl` — TF-IDF vectorizer for text features

**When applied:** After URL scraping succeeds, before the event proceeds to LLM processing.

**Feature engineering:**
1. Source type is encoded as a numerical category via the label encoder.
2. Title, description, scraped content, and preprocessed URL are concatenated into a single text string.
3. URL preprocessing: remove protocol, replace hyphens/underscores with spaces, remove common TLD suffixes.
4. Text preprocessing: lowercase, collapse whitespace.
5. The combined text is vectorised using the trained TF-IDF vectorizer.
6. Source type encoding and TF-IDF features are horizontally stacked into the final feature vector.

**Decision rule:** The model's `predict()` output determines keep/filter. The model's `predict_proba()` provides a confidence score (probability of the positive class).

**Error behaviour:** On any prediction error, the event is conservatively kept (not filtered).

### 5.2 Confidence-Based Filter

**Source file:** `cyber_data_collector/filtering/confidence_filter.py`

**What it is:** A rule-based scoring system used by the GDELT data source for progressive filtering. It assigns confidence scores based on keyword matching and pattern detection rather than binary accept/reject decisions.

**Term lists (complete):**

**High-confidence cyber terms:**
```
cyber attack, cyberattack, cyber security, cybersecurity, cyber threat,
cyber incident, cyber breach, data breach, ransomware, malware, phishing,
ddos, dos attack, hacking, hacker, hack, security breach, data leak,
vulnerability, exploit, zero-day, botnet, trojan, spyware, adware,
rootkit, keylogger, backdoor
```

**Medium-confidence cyber terms:**
```
virus, worm, firewall, antivirus, encryption, authentication,
authorization, intrusion, penetration, social engineering, identity theft,
fraud, scam, credential, password, login, account, database,
network security, endpoint security, cloud security, iot security,
mobile security, web security
```

**Strong negative indicators (clear non-cyber):**
```
wedding, birthday, anniversary, graduation, ceremony, parade, festival,
celebration, party, holiday, christmas, new year, easter, thanksgiving,
football, cricket, tennis, rugby, basketball, olympics, world cup,
championship, tournament, match, game, player, team, coach, sport,
movie, film, music, concert, book, novel, art, gallery, museum,
theater, theatre, bushfire, wildfire, flood, drought, storm, cyclone,
hurricane, earthquake, tsunami
```

**Moderate negative indicators (usually non-cyber but can have cyber angles):**
```
covid, coronavirus, pandemic, epidemic, health, hospital, doctor, nurse,
patient, medical, education, school, university, college, student,
teacher, professor, academic, curriculum, transport, traffic, road,
highway, airport, flight, airline, train, railway, bus
```

**Australian context terms:**
```
australia, australian, sydney, melbourne, brisbane, perth, adelaide,
canberra, darwin, hobart, nsw, vic, qld, wa, sa, tas, nt, act,
commonwealth, federal, state government, council, ato, centrelink,
medicare, acsc, asd, asio
```

**Regex patterns for narrative, technical, and incident language:**

*Narrative pattern:*
```
(attacked|breached|compromised|hacked|infiltrated)
(stolen|leaked|exposed|accessed)\s+(data|information|records)
(security\s+(incident|breach|alert|warning))
(cyber\s+(attack|threat|incident))
(personal\s+information|customer\s+data|sensitive\s+data)
(unauthorized\s+(access|use|disclosure))
(malware|ransomware|virus)\s+(detected|found|discovered)
(systems?\s+(down|offline|compromised|affected))
```

*Technical pattern:*
```
(ip\s+address|network|server|database|firewall)
(vulnerability|exploit|patch|update|cve-\d+)
(encryption|decryption|certificate|ssl|tls)
(authentication|authorization|credential|password)
(endpoint|api|application|software|system)
(log|alert|detection|monitoring|forensic)
(backup|restore|recovery|business\s+continuity)
```

*Incident pattern:*
```
(incident\s+(response|team|management))
(investigation|forensic\s+analysis)
(containment|mitigation|remediation)
(affected\s+(customers|users|individuals))
(notification|disclosure|reporting)
(law\s+enforcement|authorities|police)
(privacy\s+(commissioner|office|authority))
(compliance|regulatory|audit|assessment)
```

**Scoring (discovery stage):**
- Base score: 0.5
- High-confidence terms: +0.2 per match (capped at +0.4)
- Medium-confidence terms: +0.1 per match (capped at +0.3)
- Context terms (only if high/medium matches exist): +0.05 per match (capped at +0.2)
- Australian terms: +0.05 per match (capped at +0.15)
- Strong negatives: −0.2 per match (capped at −0.6)
- Moderate negatives: −0.1 per match (capped at −0.3)
- Cyber-relevant URL keywords: +0.1
- **Threshold: 0.2** (very permissive)

**Scoring (content stage):**
- Base score: 0.5
- High-confidence terms: +0.15 per match (capped at +0.5)
- Medium-confidence terms: +0.08 per match (capped at +0.3)
- Cyber narrative pattern match: +0.2
- Technical indicators pattern match: +0.15
- Incident language pattern match: +0.15
- Australian terms: +0.08 per match (capped at +0.2)
- Strong negatives: −0.15 per match (capped at −0.5), penalty halved if high-confidence terms are also present
- Content length > 500 chars: +0.05
- Content length > 2000 chars: +0.05
- **Threshold: 0.4** (moderate)

**Final stage:** Uses LLM analysis directly. An event passes if and only if the LLM determined both `is_australian_event = True` AND `is_specific_event = True`. Score: 0.9 if accepted, 0.0 if rejected.

### 5.3 LLM Classification Filter

The LLM classifier (see Section 6.2) also acts as a filter. Events where the LLM determines `is_cybersecurity_event = False` or `is_australian_relevant = False` are rejected and excluded from further processing.

---

## 6. LLM Processing

Three distinct LLM interactions occur during discovery and collection. All use OpenAI's GPT-4o-mini model via the `instructor` library for structured output.

### 6.1 Entity Extraction

**Source file:** `cyber_data_collector/processing/entity_extractor.py`

**Model:** GPT-4o-mini via instructor (structured output)

**Concurrency:** 10 concurrent requests via asyncio semaphore. 60-second timeout per request.

**System prompt (verbatim):**

```
Extract all entities mentioned in this cyber security event. Identify
organizations, government agencies, companies, and individuals. Classify their
type and determine if they are Australian entities with confidence scores.
```

**User prompt:** The event's title, description, and content snippets (truncated to 2,000 characters) are passed as a JSON-serialised `EntityExtractionRequest`.

**Structured output schema:**

```python
class ExtractedEntity(BaseModel):
    name: str
    entity_type: EntityType  # Government, Financial, Healthcare, etc. (14 types)
    industry_sector: str | None
    location: str | None
    is_australian: bool
    confidence: float  # 0.0 to 1.0
```

**Behaviour on error/timeout:** Returns the original event unchanged (entities from the data source are preserved).

### 6.2 Event Classification and Enhancement

**Source file:** `cyber_data_collector/processing/llm_classifier.py`

**Model:** GPT-4o-mini via instructor (structured output)

**Concurrency:** 10 concurrent requests via asyncio semaphore. 60-second timeout per request.

**System prompt (verbatim):**

```
You are a cybersecurity incident analyst focused on identifying SPECIFIC
cybersecurity incidents affecting Australian organizations. ACCEPT events that
describe specific incidents affecting named organizations, even if details are
limited. Examples to ACCEPT: 'Toll Group ransomware attack', 'Perth Mint data
breach', 'ANU cyber attack', 'Canva security incident', 'Travelex ransomware'.
Examples to REJECT: 'Multiple Cyber Incidents Reported', 'OAIC Notifiable Data
Breaches Report', 'What is a cyber attack?', policy documents. REJECT obvious
summaries, regulatory reports, and policy documents, but ACCEPT specific
incidents. When in doubt about whether something is a specific incident affecting
a named organization, ACCEPT it rather than reject it. Bias toward inclusion of
potential incidents for further analysis.
```

**User prompt (verbatim structure):**

```
FIRST, determine if this is actually a cybersecurity INCIDENT and if it's
Australian-relevant.

Event Title: {title}
Event Description: {description}
Affected Entities: {comma-separated entity names}
Raw Data Snippets: {concatenated content snippets}

STEP 1 - VALIDATION (CRITICAL):
- `is_cybersecurity_event`: Is this genuinely about ONE SPECIFIC cybersecurity
  INCIDENT that actually happened to a named organization?
  - Return TRUE for: specific incidents affecting named organizations (e.g.,
    "Toll Group Ransomware Attack", "Perth Mint data breach", "ANU cyber attack")
  - Return TRUE for: actual data breaches, cyber attacks, malware infections,
    ransomware attacks, phishing campaigns that OCCURRED to ONE specific named
    company/organization
  - Return FALSE for:
    * General summaries with words: "Multiple", "Several", "Various", "incidents"
    * Time-period reports: "January 2020", "Q1 2020", "2020 breaches"
    * OAIC regulatory reports and summaries
    * Policy documents: "action plan", "framework", "guidance", "guidelines",
      "recommendation"
    * Educational content: "What is a cyber attack?", training materials
    * General trend analyses or market reports
    * Regulatory guidance, compliance documents, privacy principles
    * Training materials, educational content, best practices
    * Celebrations, fireworks, New Year events, holidays, festivals, concerts,
      sports, elections, entertainment

- `is_australian_relevant`: Does this SPECIFIC INCIDENT affect Australian
  organizations, systems, or citizens?
  - Return TRUE for incidents affecting Australian entities
  - Return FALSE for: generic global events, events in other countries without
    Australian impact
- `rejection_reason`: If either above is false, explain why this should be rejected

EXAMPLES TO ACCEPT:
- "Toll Group Ransomware Attack" ✓ (specific incident, named organization)
- "Perth Mint visitor data stolen" ✓ (specific breach, named organization)
- "Australian National University cyber attack" ✓ (specific incident)
- "Canva Security Incident" ✓ (specific incident, named organization)
- "Travelex website hit by ransomware" ✓ (specific incident)

EXAMPLES TO REJECT:
- "Multiple Cyber Incidents Reported in Australia (January 2020)" ✗ (summary)
- "OAIC Notifiable Data Breaches: January–June 2020" ✗ (regulatory report)
- "What is a cyber attack?" ✗ (educational content)
- "Australian Data Breach Action Plan" ✗ (policy document)

STEP 2 - CLASSIFICATION:
- `event_type`: Classify into appropriate category. If rejected, use "Other".
- `secondary_types`: List any other relevant event categories.
- `severity`: Assess severity. If rejected, use "Unknown".
- `detailed_description`: Provide detailed description.
- `technical_details`: Provide technical details.
- `estimated_customers_affected`: Extract number if mentioned.
- `estimated_financial_impact`: Extract amount if mentioned.
- `regulatory_fine`: Extract amount if mentioned.
- `regulatory_undertaking`: Describe if mentioned.
- `response_actions`: List response actions.
- `attribution`: Identify threat actor if mentioned.

CRITICAL REQUIREMENTS:
- ACCEPT specific incidents affecting named organizations, even if details are limited
- REJECT obvious summaries, reports, and policy documents
- Focus on the organization name and incident specificity
- When in doubt about whether something is a specific incident, ACCEPT it
- Always provide all fields even for rejected events
```

**Decision logic:** If the LLM returns `is_cybersecurity_event = False` or `is_australian_relevant = False`, the event is rejected (returns `None`) and excluded from further processing. Otherwise, the event's metadata is updated with the LLM's classifications.

**Structured output schema:**

```python
class EventEnhancement(BaseModel):
    is_cybersecurity_event: bool
    is_australian_relevant: bool
    rejection_reason: Optional[str]
    event_type: CyberEventType  # 13 categories
    secondary_types: List[CyberEventType]
    severity: EventSeverity  # Critical, High, Medium, Low, Unknown
    detailed_description: str
    technical_details: str
    estimated_customers_affected: Optional[int]
    estimated_financial_impact: Optional[float]
    regulatory_fine: Optional[float]
    regulatory_undertaking: Optional[str]
    response_actions: List[str]
    attribution: Optional[str]
    confidence_adjustments: Dict[str, float]
```

### 6.3 Content Analysis (Fast Pass)

**Source file:** `cyber_data_collector/utils/llm_extractor.py`

**Model:** GPT-4o-mini via instructor (structured output)

**Temperature:** 0.0 (deterministic)

**Max content:** 12,000 characters (truncated)

This is a lightweight extraction step that analyses scraped article content for key facts.

**System prompt (verbatim):**

```
You are an expert cybersecurity analyst. Your task is to analyze the provided
news article text and extract key information with high accuracy. Adhere strictly
to the response format.
```

**User prompt (verbatim structure):**

```
Please analyze the following article text and extract the required details.

Key Instructions:
1. `is_australian_event`: Set to `true` if the event involves Australia, an
   Australian company, or Australian citizens. Otherwise, `false`.
2. `is_specific_event`: This is the most important instruction. Set to `true` if
   the article describes a specific, concrete cyber incident that has already
   happened (e.g., a data breach at a named company, a ransomware attack on a
   specific date). Set to `false` if the article is about a potential future
   threat, a general security warning, a report on cyber trends, or an opinion
   piece about cybersecurity. Focus on whether a specific event is the main
   subject of the article.
3. `primary_entity`: Identify the main organization that was the target of the
   attack. If no single primary entity is clear, leave it as null.
4. `affected_entities`: List any other named organizations or groups mentioned as
   being affected.
5. `summary`: Provide a brief, neutral summary of the incident described.
6. `event_date`: Extract the actual date when the cyber incident occurred (NOT
   the publication date). Look for phrases like 'in June 2025', 'last month',
   'on June 5th', etc. Format as YYYY-MM-DD. If no specific date is found, set
   to null.
7. `records_affected`: Extract the specific number of people, customers, or data
   records affected. If a number is mentioned (e.g., '2 million customers',
   '50,000 records'), extract only the integer value. If no number is specified,
   set to null.

--- ARTICLE TEXT ---
{truncated article content}
```

**Structured output schema:**

```python
class ExtractedEventDetails(BaseModel):
    is_australian_event: bool
    is_specific_event: bool
    primary_entity: Optional[str]
    affected_entities: List[str]
    summary: str
    event_date: Optional[date]  # YYYY-MM-DD
    records_affected: Optional[int]
```

---

## 7. Month-by-Month Processing

The discovery stage processes events month by month rather than as a single bulk operation.

### Month Selection Logic

1. The target date range is determined by the `--days` parameter (default: full history from January 2020).
2. All months in the range are enumerated.
3. The database is queried for months not yet marked as processed (`get_unprocessed_months()`).
4. The most recent 3 calendar months (including the current month) are always reprocessed regardless of processing status, to catch late-reported events.
5. Months already processed and outside the 3-month window are skipped.

### Per-Month Processing Steps

For each month:

1. **Configure date range:** First day to last day of the month.
2. **Collect raw events:** All enabled data sources are queried concurrently using `asyncio.as_completed()`.
3. **Store raw events:** Each event is checked for duplicates (by source type + URL + title) and stored in the `RawEvents` table.
4. **Scrape URLs:** Playwright scrapes full content for all raw events that have a URL but no content yet.
5. **Apply Random Forest filter:** Scraped content is evaluated by the ML model; events classified as non-cyber are marked as filtered.
6. **Process events:** Entity extraction (GPT-4o-mini) and LLM classification (GPT-4o-mini) are run on all events.
7. **Store enriched events:** Processed events are stored in the `EnrichedEvents` table with entities linked via `EntitiesV2`.
8. **Confidence filter:** Events with confidence score below 0.6 are excluded.
9. **Mark month processed:** The month is recorded as processed with counts and processing notes.

### Failure handling

If any step fails for a month, the month is NOT marked as processed. Failed raw events that could not be enriched remain unprocessed for retry on the next pipeline run.

If more than 50% of events fail to store as enriched, the pipeline raises a `RuntimeError` and stops to prevent data quality issues.

---

## 8. Duplicate Detection

**At raw event insertion:** Before storing a new raw event, the pipeline calls `find_existing_raw_event(source_type, source_url, title)`. If a match is found (same source type AND same URL AND same title), the existing event ID is returned and the new event is skipped.

**At OAIC collection:** Before fetching an article page, the URL is checked against a pre-loaded set of all known OAIC URLs from the database. Known URLs are skipped entirely.

**At enriched event insertion:** Before creating an enriched event, the pipeline checks if an enriched event already exists for the given `raw_event_id` (preventing double-enrichment).

---

## 9. Data Storage

### Raw Events Table

Each discovered event is stored in `RawEvents` with:

| Field | Type | Description |
|-------|------|-------------|
| `raw_event_id` | TEXT (PK) | UUID, generated on insert |
| `source_type` | VARCHAR | Perplexity, OAIC, GoogleSearch, WebberInsurance, GDELT |
| `source_event_id` | VARCHAR | External ID (e.g., GDELT event ID) |
| `raw_title` | TEXT | Event title as discovered |
| `raw_description` | TEXT | Event description/snippet |
| `raw_content` | TEXT | Full scraped article content (NULL until scraped) |
| `event_date` | DATE | Event date (from source or publication date) |
| `source_url` | VARCHAR | Original source URL |
| `source_metadata` | TEXT (JSON) | Additional metadata including confidence scores |
| `discovered_at` | TIMESTAMP | When the event was discovered |
| `processed` | BOOLEAN | Whether enrichment has been attempted |
| `processed_at` | TIMESTAMP | When processing was completed |
| `error_message` | TEXT | Error details if processing failed |

### Enriched Events Table

Processed events are stored in `EnrichedEvents` with:

| Field | Type | Description |
|-------|------|-------------|
| `enriched_event_id` | TEXT (PK) | UUID |
| `raw_event_id` | TEXT (FK) | Link to raw event |
| `title` | TEXT | Cleaned/enhanced title |
| `description` | TEXT | LLM-enhanced description |
| `summary` | TEXT | LLM-generated summary |
| `event_type` | VARCHAR | Classified event type |
| `severity` | VARCHAR | Classified severity |
| `event_date` | DATE | Extracted/validated event date |
| `records_affected` | BIGINT | Number of affected records |
| `is_australian_event` | BOOLEAN | LLM-confirmed Australian relevance |
| `is_specific_event` | BOOLEAN | LLM-confirmed specific incident |
| `confidence_score` | REAL | Overall confidence (0-1) |
| `australian_relevance_score` | REAL | Australian relevance score (0-1) |
| `status` | VARCHAR | Active/Inactive |

### Entities Table

Extracted entities are stored in `EntitiesV2` and linked to enriched events:

| Field | Type | Description |
|-------|------|-------------|
| `entity_id` | INTEGER (PK) | Auto-increment |
| `entity_name` | VARCHAR | Organisation/person name |
| `entity_type` | VARCHAR | Government, Financial, Healthcare, etc. |
| `industry` | VARCHAR | Industry sector |
| `is_australian` | BOOLEAN | Whether entity is Australian |

### Month Processing Tracking

The pipeline tracks which months have been processed to enable incremental operation and avoid redundant collection.

---

## 10. Configuration and Thresholds

### Key Thresholds

| Parameter | Value | Purpose |
|-----------|-------|---------|
| Confidence threshold | 0.6 | Minimum event confidence for inclusion |
| RF filter threshold | 0.5 (model's decision boundary) | ML filter keep/reject boundary |
| Discovery filter threshold | 0.2 | Minimum score for GDELT discovery stage |
| Content filter threshold | 0.4 | Minimum score for GDELT content stage |
| Max concurrent LLM requests | 10 | Semaphore limit for entity extraction and classification |
| LLM timeout | 60 seconds | Per-request timeout for GPT-4o-mini calls |
| Scraping timeout | 120 seconds | Per-URL timeout for Playwright scraping |
| Page load timeout | 30 seconds | Playwright page load timeout |
| Perplexity circuit breaker | 5 consecutive failures | Threshold before 5-minute backoff |
| Perplexity retry limit | 3 attempts | Maximum retries per query |
| Perplexity retry base delay | 2 seconds | Initial backoff delay |
| Google daily quota | 100 queries | Free tier limit (auto-detected) |
| Late-reporting window | 3 months | Rolling reprocessing window |
| Search expansion window | 2 months | How far back to expand search queries |

### Environment Variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `OPENAI_API_KEY` | Yes | — | GPT-4o-mini for entity extraction, classification, content analysis |
| `PERPLEXITY_API_KEY` | Recommended | — | Perplexity AI discovery and fallback scraping |
| `GOOGLE_CUSTOMSEARCH_API_KEY` | Optional | — | Google Custom Search queries |
| `GOOGLE_CUSTOMSEARCH_CX_KEY` | Optional | — | Google Custom Search engine ID |
| `DATABASE_URL` | Optional | `sqlite:///instance/cyber_events.db` | Database location |
| `MAX_THREADS` | Optional | 4 | Concurrent data source threads |
| `BATCH_SIZE` | Optional | 20 | Events per processing batch |

---

## 11. Error Handling and Resilience

### Source-Level

- Each data source's `collect_events()` is wrapped in try/except. A source failure does not stop other sources.
- GDELT BigQuery authentication errors raise a `RuntimeError` with clear remediation instructions.
- Google Search quota exhaustion is detected and remaining queries for the day are skipped.

### Scraping-Level

- Per-URL timeout of 120 seconds prevents indefinite hangs.
- 403 errors trigger retry with different browser fingerprints.
- All scraping failures fall back to Perplexity AI content retrieval.
- Failed scrapes are logged with URLs and reasons for post-hoc analysis.

### LLM-Level

- Per-request timeout of 60 seconds.
- `max_retries=2` for instructor-based calls.
- On timeout or error, the original event is returned unchanged (not discarded).

### Pipeline-Level

- If >50% of enriched event stores fail in a month, the pipeline raises `RuntimeError` to prevent silent data quality degradation.
- Failed months are not marked as processed, enabling automatic retry on the next run.
- All processing is logged to `logs/discover_enrich_events.log`.

---

## 12. Justification of Design Decisions

### Why multiple data sources?

No single source provides comprehensive coverage of Australian cyber incidents. Perplexity AI provides AI-curated breadth across the web. OAIC provides authoritative regulatory actions. Google Search catches niche publications. Webber Insurance provides a curated, human-verified Australian-specific list. GDELT provides structured event data from global media monitoring. Using multiple sources enables cross-validation and reduces the risk of missing incidents.

### Why month-by-month processing?

Month-by-month processing enables incremental operation: the pipeline can be stopped and resumed without reprocessing already-completed months. It also bounds memory usage and provides natural checkpoints. The 3-month rolling reprocessing window handles late-reported events without requiring full rebuilds.

### Why GPT-4o-mini instead of GPT-4o?

GPT-4o-mini is used for the initial classification pass because it processes events at significantly lower cost and latency. With hundreds of events per pipeline run, using the full GPT-4o model would be prohibitively expensive for what is essentially a binary classification task (specific Australian cyber incident: yes/no). The more expensive Perplexity enrichment (Phase 2) subsequently validates and enhances the results using more capable models.

### Why a Random Forest filter?

The Random Forest model was trained on labelled data from previous pipeline runs, where human reviewers confirmed which events were genuine cyber incidents. This learned model captures complex feature interactions (e.g., a URL from a security blog + specific technical terms in the title) that keyword-based filters cannot express. On prediction failure, the filter conservatively keeps the event, ensuring no genuine incidents are lost.

### Why Perplexity AI as a scraping fallback?

Many important news sources aggressively block automated scraping (paywalls, bot detection, CAPTCHAs). Rather than losing coverage of events behind these barriers, the pipeline uses Perplexity's web search to find the same story from alternative sources. This is not a fabrication — Perplexity returns URLs to real published articles that are then independently scraped.

### Why bias toward inclusion?

The system prompt for the LLM classifier explicitly states "when in doubt, ACCEPT it rather than reject it." This reflects a deliberate design choice: in a discovery pipeline, false negatives (missing a real incident) are more costly than false positives (including a non-incident). False positives are caught by downstream deduplication and manual review stages. False negatives are permanently lost.

### Why 0.6 confidence threshold?

The 0.6 threshold balances signal quality with coverage. Events scoring below 0.6 typically lack sufficient corroborating evidence or have ambiguous relevance. Events above 0.6 have been validated by at least one source with reasonable confidence. Higher thresholds (0.7, 0.8) were tested but discarded too many legitimate events with limited initial metadata.

### Why a 3-month late-reporting window?

Analysis of historical data showed that cyber incidents are frequently reported 1-3 months after they occur. Companies may delay disclosure for investigation, regulatory compliance, or legal reasons. The 3-month reprocessing window ensures that the pipeline captures these late-reported events without requiring full historical rebuilds.

---

## 13. Replication Guide

To independently replicate the discovery and collection process:

### Prerequisites

1. Python 3.8+
2. OpenAI API key with GPT-4o-mini access
3. Perplexity AI API key (recommended but optional)
4. Google Custom Search API key + CX ID (optional)
5. Playwright with Chromium browser: `playwright install chromium`

### Installation

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
# Edit .env with your API keys
```

### Running Discovery Only

```bash
# Discover events for the last 90 days from all default sources
python run_full_pipeline.py --discover-only --days 90

# Discover from specific sources only
python run_full_pipeline.py --discover-only --days 30 --source Perplexity OAIC

# Limit events per source per month
python run_full_pipeline.py --discover-only --days 30 --max-events 100
```

### Verifying Results

```bash
# Check database contents
python pipeline.py status

# Export discovered events for review
python scripts/export/export_cyber_events.py --format csv --output review.csv
```

### Key Files for Replication

| File | Purpose |
|------|---------|
| `run_full_pipeline.py` | Entry point; `--discover-only` flag runs Phase 1 only |
| `cyber_data_collector/pipelines/discovery.py` | Main orchestration logic |
| `cyber_data_collector/datasources/perplexity.py` | Perplexity AI data source |
| `cyber_data_collector/datasources/oaic.py` | OAIC scraper |
| `cyber_data_collector/datasources/google_search.py` | Google Custom Search |
| `cyber_data_collector/datasources/webber_insurance.py` | Webber Insurance scraper |
| `cyber_data_collector/datasources/gdelt.py` | GDELT BigQuery (optional) |
| `cyber_data_collector/utils/entity_scraper.py` | Playwright scraping |
| `cyber_data_collector/utils/llm_extractor.py` | Content analysis (fast pass) |
| `cyber_data_collector/processing/entity_extractor.py` | Entity extraction (GPT-4o-mini) |
| `cyber_data_collector/processing/llm_classifier.py` | Event classification (GPT-4o-mini) |
| `cyber_data_collector/filtering/rf_event_filter.py` | Random Forest ML filter |
| `cyber_data_collector/filtering/confidence_filter.py` | Keyword confidence filter |
| `machine_learning_filter/` | Trained ML model artifacts |

### Validating Source Accuracy

To verify the pipeline is collecting genuine Australian cyber incidents:

1. Run discovery for a recent 30-day period.
2. Export the results to CSV.
3. Manually verify a random sample of 20-30 events by following the `source_url` links.
4. Check that events are: (a) about specific cyber incidents, (b) affecting Australian entities, (c) not duplicates, summaries, or policy documents.
5. Compare against known incidents from public OAIC reports or media coverage for the same period to estimate recall.

---

*Last updated: March 2026*
