# Data Sources Documentation

This document provides a comprehensive description of each data source used in the Australian Cyber Events Discovery and Enrichment Pipeline. It is intended to support independent review of source suitability and to enable independent replication of the data collection methodology.

---

## Table of Contents

1. [Overview and Justification](#1-overview-and-justification)
2. [Perplexity AI Search](#2-perplexity-ai-search)
3. [OAIC Notifiable Data Breaches](#3-oaic-notifiable-data-breaches)
4. [Google Custom Search](#4-google-custom-search)
5. [Webber Insurance Data Breaches List](#5-webber-insurance-data-breaches-list)
6. [GDELT (Deprecated)](#6-gdelt-deprecated)
7. [Source Comparison Matrix](#7-source-comparison-matrix)
8. [Content Scraping and Extraction](#8-content-scraping-and-extraction)
9. [LLM-Based Filtering and Classification](#9-llm-based-filtering-and-classification)
10. [Confidence Scoring and Quality Assurance](#10-confidence-scoring-and-quality-assurance)

---

## 1. Overview and Justification

### Why Multiple Sources?

The pipeline collects Australian cybersecurity incidents from four active data sources. No single source provides comprehensive coverage of the Australian cybersecurity incident landscape:

| Challenge | Solution |
|-----------|----------|
| No centralised Australian cyber incident register exists | Aggregate from multiple complementary sources |
| OAIC only covers notifiable data breaches (Privacy Act 1988) | Supplement with news-based discovery for incidents outside OAIC scope |
| News coverage varies by outlet and incident severity | Use AI-powered search (Perplexity) alongside traditional search (Google) |
| Some incidents are only documented by specialist industry sources | Include Webber Insurance's curated breach list |

### Default Source Configuration

The pipeline uses four sources by default, configured in `pipeline.py`:

```python
DEFAULT_SOURCES = ["Perplexity", "OAIC", "GoogleSearch", "WebberInsurance"]
```

GDELT was originally included but has been removed from the default set due to cost and data quality concerns (see [Section 6](#6-gdelt-deprecated)).

### Pipeline Integration

Each data source implements the `DataSource` abstract base class (`cyber_data_collector/datasources/base.py`) and produces `CyberEvent` objects. The discovery pipeline (`cyber_data_collector/pipelines/discovery.py`) orchestrates collection across all enabled sources, iterating month-by-month over the requested date range.

---

## 2. Perplexity AI Search

**Implementation:** `cyber_data_collector/datasources/perplexity.py`

### Purpose and Justification

Perplexity AI is the primary discovery source. Unlike traditional search engines, Perplexity uses large language models with real-time web access to synthesize information across multiple sources, enabling it to:

- Discover incidents that may not appear in the first few pages of traditional search results
- Aggregate information from multiple news outlets into structured event descriptions
- Identify incidents by semantic meaning rather than exact keyword matching
- Surface incidents reported in specialist cybersecurity publications that traditional search may rank lower

### API Configuration

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| API endpoint | `https://api.perplexity.ai` | OpenAI-compatible API interface |
| Model | `sonar-pro` | Perplexity's search-augmented LLM with real-time web access |
| Temperature | `0.1` | Low temperature for consistent, factual responses |
| Max tokens | `4000` | Large enough to capture full JSON responses with multiple events |
| Rate limit | 50 requests/minute, 1 request/second | Prevents API throttling |

### Search Queries

The data source generates seven search queries per month, each targeting a different sector or incident type. The `{date_range}` placeholder is replaced with Perplexity's date filter syntax (`after:MM/DD/YYYY before:MM/DD/YYYY`):

| # | Query Template | Target |
|---|----------------|--------|
| 1 | `Australian cyber attack {date_range} data breach security incident` | General incidents |
| 2 | `Australia cybersecurity breach {date_range} ransomware malware` | Malware-specific |
| 3 | `Australian company cyber incident {date_range} hacking data leak` | Private sector |
| 4 | `Australia government cyber attack {date_range} infrastructure security` | Government/infrastructure |
| 5 | `Australian bank financial cyber breach {date_range} fraud security` | Financial sector |
| 6 | `Australia healthcare cyber attack {date_range} medical data breach` | Healthcare sector |
| 7 | `Australian university cyber incident {date_range} education security` | Education sector |

**Date range expansion:** The search window is expanded by 2 months before the requested start date to catch late-reported incidents. For example, a January 2026 search would query from November 2025.

### System Prompt (Event Extraction)

The following system prompt instructs Perplexity to extract structured event data:

```
You are a cybersecurity analyst. Extract detailed information about Australian cyber
security incidents from search results.

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

### JSON Recovery Strategy

Perplexity responses sometimes contain malformed JSON. The implementation uses a four-tier fallback:

1. **Strip markdown wrappers** — Removes ` ```json ... ``` ` code block formatting, then parses
2. **Repair truncated JSON** — Counts unclosed brackets/braces, finds last complete event boundary (`},`), and closes the structure
3. **Strict JSON retry** — Re-queries Perplexity with `temperature=0.0` and explicit instruction: *"Return ONLY raw JSON. Do NOT wrap it in markdown code blocks."*
4. **Partial extraction + completion** — Extracts fields using regex (`"title"\s*:\s*"..."`, etc.) from the malformed response and sends partial data back to Perplexity for completion

### Event Type Mapping

Perplexity responses are mapped to internal event types by keyword matching:

| Keyword in Response | Mapped Type |
|---------------------|-------------|
| `ransomware` | `RANSOMWARE` |
| `breach` or `data` | `DATA_BREACH` |
| `phish` | `PHISHING` |
| All other | `OTHER` |

### Confidence Scores (Discovery Phase)

Events from Perplexity discovery receive baseline confidence scores that are later refined during enrichment:

| Dimension | Score | Rationale |
|-----------|-------|-----------|
| Overall | 0.65 | AI-synthesised data requires verification |
| Source reliability | 0.6 | Perplexity aggregates from multiple sources but may hallucinate |
| Data completeness | 0.6 | Not all fields may be present |
| Temporal accuracy | 0.6 | Dates may be approximate |
| Geographic accuracy | 0.7 | Queries are Australia-specific, so geographic accuracy is higher |

### Retry and Circuit Breaker Configuration

| Parameter | Value |
|-----------|-------|
| Max retries | 3 (+ initial attempt = 4 total) |
| Base delay | 2.0 seconds |
| Max delay | 60 seconds |
| Backoff multiplier | 2.0 (exponential: 2s, 4s, 8s) |
| Jitter | +/-10% random variation |
| Circuit breaker threshold | 5 consecutive failures |
| Circuit breaker duration | 300 seconds (5 minutes) |

### Perplexity in Enrichment (Post-Discovery)

Perplexity is also used in two post-discovery roles:

#### Fact-Checking (`cyber_data_collector/enrichment/perplexity_fact_checker.py`)

After GPT-4o extracts event details, Perplexity performs four verification checks using `sonar-pro` at `temperature=0.1` and `max_tokens=1000`:

**Check 1 — Organisation Verification:**
```
Is "{org_name}" a real, specific organization (not a generic description)?

Search for information about this organization and answer with JSON:
{
  "is_real_organization": true or false,
  "organization_type": "company|government|nonprofit|university|other|generic",
  "country": "primary country of operation or null",
  "confidence": 0.0 to 1.0,
  "reasoning": "brief explanation with sources if possible",
  "sources": ["URL1", "URL2"] or []
}

CRITICAL RULES:
- is_real_organization: FALSE if it's a generic description like "Australian IT company"
  or "healthcare provider"
- is_real_organization: TRUE only if you can confirm it's a specific legal entity with
  web presence
- If you cannot find ANY information about this organization, confidence should be < 0.3
```

**Check 2 — Incident Occurrence Verification:**
```
Did "{org_name}" experience a cybersecurity incident (data breach, ransomware attack,
cyber attack, etc.) on or around {incident_date}?

Search for reliable news sources and answer with JSON:
{
  "incident_confirmed": true or false,
  "actual_date": "YYYY-MM-DD if different from provided date, or null",
  "incident_type": "type of incident or null",
  "sources": ["URL1", "URL2"],
  "confidence": 0.0 to 1.0,
  "reasoning": "brief explanation citing sources"
}

CRITICAL RULES:
- incident_confirmed: TRUE only if you find credible news sources confirming the incident
- If date is slightly different (within 30 days), still confirm but provide actual_date
- Prefer Australian news sources (ABC, SMH, AFR) and cybersecurity sources (iTnews, ZDNet)
- If you cannot find ANY sources, confidence should be < 0.3
```

**Check 3 — Attacker Attribution Verification:**
```
Has the threat actor "{attacker}" been credibly linked to a cyber attack on "{victim}"?
```
Returns confirmed/suspected/unverified attribution with confidence and sources.

**Check 4 — Records Count Verification:**
```
In the "{org_name}" cyber incident, were approximately {records_count:,} records/people
affected?
```
Returns count confirmation, actual count if different (>20% variance), and sources.

**Confidence calculation (weighted):**

| Check | Weight |
|-------|--------|
| Organisation exists | 0.4 |
| Incident occurred | 0.4 |
| Attacker attribution | 0.1 |
| Records affected | 0.1 |

Failed checks receive a 50% penalty: `weighted_sum += weight * (1.0 - confidence) * 0.5`

#### Detail Enrichment (`cyber_data_collector/processing/perplexity_enricher.py`)

For events with missing fields, targeted queries are sent to Perplexity (`sonar-pro`, `temperature=0.1`, `max_tokens=1000`):

| Missing Field | Query Template |
|---------------|----------------|
| Threat actor | *"Who was responsible for the {title} cyber attack on {entity_name} in {event_date}?"* |
| Vulnerability details | *"What security vulnerability or weakness allowed the {title} attack on {entity_name}?"* |
| Regulatory fines | *"Were any regulatory fines or penalties imposed after the {title} incident involving {entity_name}?"* |
| Severity | *"What was the severity and impact of the {title} cyber incident involving {entity_name}?"* |
| Records count | *"How many records or accounts were affected in the {title} incident involving {entity_name}?"* |

---

## 3. OAIC Notifiable Data Breaches

### Purpose and Justification

The Office of the Australian Information Commissioner (OAIC) is the statutory authority for the Notifiable Data Breaches (NDB) scheme under Part IIIC of the Privacy Act 1988. The NDB scheme requires organisations covered by the Act to notify the OAIC and affected individuals when a data breach is likely to result in serious harm.

OAIC is included because:
- It is the **only official, statutory source** of Australian data breach notifications
- Notifications are a **legal obligation**, making the data authoritative and comprehensive within its scope
- It provides **aggregate statistics** (total notifications, sector breakdowns, attack type distributions) that contextualise individual incidents
- It has the **highest credibility score** (1.0) of any source in the pipeline

**Scope limitation:** The NDB scheme only covers entities with annual turnover exceeding $3 million (with exceptions for health service providers, credit reporting bodies, and certain other organisations). It does not cover state/territory government agencies (except ACT) or incidents that do not meet the "serious harm" threshold.

The pipeline collects OAIC data through three complementary methods.

### 3.1 Media Centre Scraper

**Implementation:** `cyber_data_collector/datasources/oaic.py`

Scrapes OAIC media centre articles for regulatory actions, enforcement decisions, and investigations related to privacy and data breaches.

**Source URL:**
```
https://www.oaic.gov.au/news/media-centre?query=&sort=dmetapublishedDateISO&num_ranks=1000
```

**Article filtering criteria:**

| Inclusion Keywords | Exclusion Keywords |
|--------------------|--------------------|
| cyber, data breach, privacy, security, hack, attack | privacy policy, your privacy rights |
| civil penalty, enforcement, investigation, determination | privacy complaints, australian privacy principles |
| enforceable undertaking, compliance, breach, incident | privacy guidance, privacy legislation |

**Entity extraction from titles** uses regex patterns:
```
r'(?:action against|penalty.*against|investigation.*into|determination.*against)\s+([name])'
r'([Name])\s+(?:cyber incident|data breach|privacy breach|breach|hack)'
r'([Name])(?:\s+–|\s+privacy|\s+security|\s+data)'
```

**Confidence scores:**

| Dimension | Score | Rationale |
|-----------|-------|-----------|
| Overall | 0.9 | Government source with high authority |
| Source reliability | 0.95 | Statutory body; information is legally reported |
| Temporal accuracy | 0.85 | Publication date may differ from incident date |
| Credibility | 0.95 | Official government publication |

### 3.2 PDF Report Scraper

**Implementation:** `scripts/oaic/oaic_data_scraper.py`

Extracts aggregate statistics from OAIC's semi-annual Notifiable Data Breaches reports (published as PDF documents).

**Source URL:**
```
https://www.oaic.gov.au/privacy/notifiable-data-breaches/notifiable-data-breaches-publications
```

**Reporting cadence:** Semi-annual (H1: January-June, H2: July-December). Reports are typically published 2-3 months after the reporting period ends (H1 reports in August/September, H2 reports in February/March).

**Extraction methods (cascading):**

1. **HTML extraction** (primary) — Regex patterns applied to the report's HTML page to extract statistics
2. **PDF extraction** (enhanced) — Uses `pdfplumber` to extract tables from the downloadable PDF

**Statistics extracted:**

| Category | Fields |
|----------|--------|
| Totals | `total_notifications`, `cyber_incidents_total`, `cyber_incidents_percentage` |
| Source breakdown | `malicious_attacks`, `human_error`, `system_faults` |
| Attack types | `ransomware`, `phishing`, `hacking`, `brute_force`, `malware`, `compromised_credentials` |
| Sector data | Top sectors with notification counts |
| Impact | `individuals_affected_distribution`, `individuals_affected_median`, `individuals_affected_average` |

**PDF table extraction details:**

- Uses `pdfplumber.open()` to iterate over PDF pages
- Searches for tables with headers containing "individual", "affected", or "breach size"
- Extracts breach size distribution ranges (1-100, 101-1,000, etc.) with counts
- Extracts median and average individuals affected using regex: `median.*?(\d+(?:,\d+)*)\s*(?:individual|people|record|affected)`
- Extracts top sectors from tables with "sector", "industry", or "entity" columns

**Data quality corrections:**

The scraper applies manual corrections for known extraction errors where automated parsing produces incorrect values (e.g., extracting the year "2024" instead of the notification count "595"). These corrections are hardcoded for specific reporting periods:

```python
# Example: 2024 H2 corrections
stats['total_notifications'] = 595   # Corrects year-extraction error
stats['cyber_incidents_total'] = 247
```

**Validation rules** (values exceeding these thresholds are set to `None`):
- Phishing, hacking, brute force > 1,000 (likely individuals, not incidents)
- Compromised credentials > 10,000
- Ransomware, malware > 1,000 (with period-specific exceptions)
- Sector notifications > 500
- Malicious attacks > 1,000

### 3.3 Power BI Dashboard Scraper

**Implementation:** `scripts/oaic/OAIC_dashboard_scraper.py`

Scrapes the OAIC's interactive Power BI dashboard using Playwright browser automation combined with GPT-4o-mini Vision API for data extraction from charts and visualisations.

**Dashboard URL:**
```
https://www.oaic.gov.au/privacy/notifiable-data-breaches/notifiable-data-breach-statistics-dashboard
```

**Browser configuration:**

| Parameter | Value |
|-----------|-------|
| Browser | Chromium (Playwright) |
| Window size | 2560x1440 |
| User-Agent | Chrome 120 (Windows NT 10.0) |
| Mode | Headless (configurable) |

**Dashboard pages scraped:**

| Page | Content | Data Extracted |
|------|---------|----------------|
| 2 | Snapshot | Total notifications, source breakdown, cyber incident types, top sectors |
| 3 | Notifications received | Monthly breakdown by type |
| 4 | Individuals affected | Distribution by breach size range |
| 5 | Personal information types | Types of personal data affected |
| 6 | Source of breaches | Comparison with previous period |
| 7 | Time to identify | Time distribution (hours/days) |
| 8 | Time to notify | Time to notify OAIC |
| 9 | Top sectors | Top sectors by source type |

Pages 1 (Home), 10 (Data Notes), and 11 (Glossary) are not scraped.

**Vision API extraction process:**

1. Navigate to dashboard page using Playwright
2. Select semester from dropdown (e.g., "Jan-Jun 2025")
3. Click filter buttons where applicable (e.g., "All breaches", "Cyber incidents")
4. Capture screenshot of the Power BI iframe
5. Send screenshot to GPT-4o-mini Vision API with a page-specific extraction prompt
6. Parse JSON response

**Vision API configuration:**

| Parameter | Value |
|-----------|-------|
| Model | `gpt-4o-mini` |
| Temperature | 0.1 |
| Max tokens | 4,096 |
| Response format | JSON |

**Example Vision API prompt (Page 2 — Snapshot):**

The prompt instructs the model to extract total notifications (with percentage change from previous period), source donut chart values (malicious attacks, human error, system faults), cyber incident breakdown (phishing, compromised credentials, ransomware, hacking, brute-force, malware as percentages), top 5 sectors, and human error causes breakdown.

**Available semesters:**
```
Jan-Jun 2025, Jul-Dec 2024, Jan-Jun 2024, Jul-Dec 2023, Jan-Jun 2023,
Jul-Dec 2022, Jan-Jun 2022, Jul-Dec 2021, Jan-Jun 2021, Jul-Dec 2020, Jan-Jun 2020
```

**Data merging priority:**
- Periods before 2025: PDF-scraped data is authoritative (more detailed)
- 2025 onwards: Dashboard-scraped data is primary (PDF may not yet be published)

### 3.4 Data Consolidation

**Implementation:** `scripts/oaic/cleanup_oaic_data.py`

Merges outputs from the PDF scraper and dashboard scraper into a single clean dataset. Validation during merge includes:
- Sector notification counts must not exceed `total * 1.1`
- Attack type counts must not exceed `total * 1.1` (nullified if invalid)
- Sectors with notification counts > 500 are removed (likely individuals misread as incidents)

---

## 4. Google Custom Search

**Implementation:** `cyber_data_collector/datasources/google_search.py`

### Purpose and Justification

Google Custom Search provides broad web coverage as a complementary source to Perplexity. While Perplexity synthesises information using AI reasoning, Google Custom Search returns raw search results, ensuring the pipeline captures incidents that may be indexed by Google but not surfaced by Perplexity's model. It is particularly useful for:

- Catching incidents covered by smaller or regional news outlets
- Providing URL-level results that can be individually scraped for full content
- Acting as an independent cross-check against Perplexity's AI-synthesised results

### API Configuration

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| API endpoint | `https://www.googleapis.com/customsearch/v1` | Google Custom Search JSON API |
| Results per page | 10 | API maximum per request |
| Max pages per query | 5 | 50 results per query (5 pages x 10 results) |
| Total potential results | 200 per run | 4 queries x 50 results |
| Rate limit | 100 requests/minute, 10 requests/second | Free tier limit |
| Quota | 100 queries/day (free tier) | Resets at midnight Pacific Time |

**Required environment variables:**
- `GOOGLE_CUSTOMSEARCH_API_KEY` — API key
- `GOOGLE_CUSTOMSEARCH_CX_KEY` — Custom Search Engine ID (configured to search the web)

### Search Queries

Four hardcoded queries target Australian cybersecurity incidents:

| # | Query | Target |
|---|-------|--------|
| 1 | `australian cybersecurity ("data breach" OR "ransomware" OR "cyber attack")` | General incidents with exact phrase matching |
| 2 | `australia "data breach" notification privacy commissioner` | OAIC-related breach notifications |
| 3 | `australian company "cyber incident" OR "security breach"` | Private sector incidents |
| 4 | `australia government "cyber attack" OR "security incident"` | Government sector incidents |

**Date range filtering:**

Google's date restriction parameter is used:
```
date:r:YYYYMMDD:YYYYMMDD
```
For example, `date:r:20260101:20260301` restricts results to January-March 2026.

### Australian Relevance Detection

Since Google returns global results, Australian relevance is determined by heuristic checks:

```python
is_australian = (
    "australia" in title.lower()
    or "australia" in snippet.lower()
    or ".com.au" in link
    or ".net.au" in link
    or ".org.au" in link
    or ".gov.au" in link
    or ".edu.au" in link
)
```

### Confidence Scores

Google Search results receive low baseline confidence because only the title and snippet are available at collection time (no full article content):

| Dimension | Score | Rationale |
|-----------|-------|-----------|
| Overall | 0.5 | Raw search results without LLM analysis |
| Source reliability | 0.5 | Google is an intermediary; source quality varies |
| Data completeness | 0.4 | Only title + snippet available at collection |
| Temporal accuracy | 0.4 | Date not extracted from snippet |
| Geographic accuracy | 0.5 | Heuristic detection only |
| Credibility | 0.5 | Varies by source website |
| Relevance | 0.6 | Queries are cyber-focused but results may include tangential content |

### Limitations

- **No LLM processing at collection time** — Events are created with `CyberEventType.OTHER` and `EventSeverity.MEDIUM` as defaults. Proper classification occurs during enrichment.
- **Daily quota** — Free tier allows 100 queries/day. The pipeline detects HTTP 429 and 403 quota errors and stops gracefully.
- **Snippet-only content** — Full article scraping happens in the enrichment phase via Playwright or Perplexity fallback.

---

## 5. Webber Insurance Data Breaches List

**Implementation:** `cyber_data_collector/datasources/webber_insurance.py`

### Purpose and Justification

Webber Insurance maintains a curated, chronological list of Australian data breaches on their website. This source is valuable because:

- It is **manually curated** by an Australian cyber insurance specialist, providing editorial judgement on what constitutes a significant breach
- It focuses **exclusively on Australian incidents**, eliminating the need for geographic filtering
- It provides a **historical archive** going back several years, capturing incidents that may no longer appear in search engine results
- It offers a **different editorial perspective** from news media, sometimes including incidents from insurance industry sources

### Scraping Configuration

| Parameter | Value |
|-----------|-------|
| Base URL | `https://www.webberinsurance.com.au/data-breaches-list` |
| Scraping method | HTML parsing (requests + BeautifulSoup) |
| Rate limit | 30 requests/minute, 1 request/second (per endpoint) |
| No API key required | Free, public webpage |

### Two-Phase Scraping Process

**Phase 1 — List Page Scraping:**

1. Fetch the main page listing all breaches
2. Parse `<h3>` section headers to extract month-year groupings (e.g., "June 2025")
3. Walk sibling elements between headers to find `<a>` links
4. Filter links by inclusion/exclusion keywords:

| Criteria | Keywords |
|----------|----------|
| **Include** (link text must contain) | breach, cyber, hack, attack, ransomware, incident |
| **Exclude** (link text must not contain) | guide, ultimate, notification laws, essentials |
| **Minimum text length** | 20 characters |

5. Apply date range filtering using section header dates (skips sections outside the requested date range before fetching detail pages)

**Phase 2 — Detail Page Scraping:**

For each qualifying link:

1. Fetch the detail page
2. Extract title from `<h1>` tag
3. Extract content from `<article>`, `<div class="content">`, or full page (cascading selectors)
4. Extract entity name from title using regex: split on dash/en-dash (e.g., "Vertel - Data Breach" yields "Vertel")
5. Parse event date using three-tier fallback:
   - **Tier 1:** Section header date (most reliable)
   - **Tier 2:** Date patterns in page text (e.g., `19 Jun 2025`, `June 19, 2025`, `2025-06-19`)
   - **Tier 3:** Date patterns in URL (e.g., `20_06_2025`)

### Perplexity Fallback for Failed Scrapes

When detail page scraping fails (e.g., broken URL, 403 error, content changes), the scraper falls back to Perplexity AI:

```
Find information about the cybersecurity incident that was originally reported at
this URL: {failed_url}

This incident occurred around {month} {year}

I need you to:
1. Identify what specific cybersecurity/data breach story this URL was about
2. Find information about the SAME incident from reliable sources
3. Provide a summary of the incident including company name, what happened, and impact
4. Focus on factual details about the breach/incident

The original URL is inaccessible, so I need the actual story content.
```

API parameters: `sonar-pro`, `temperature=0.1`, `max_tokens=800`.

### Confidence Scores

| Dimension | Score | Rationale |
|-----------|-------|-----------|
| Overall | 0.85 | Curated source with editorial judgement |
| Source reliability | 0.8 | Insurance industry specialist, but not an official authority |
| Data completeness | 0.7 | Variable detail depth depending on the source article |
| Temporal accuracy | 0.9 | Section headers provide reliable month-year dating |
| Geographic accuracy | 1.0 | All events are Australian by definition of the source |
| Credibility | 0.8 | Curated but commercial source |
| Relevance | 1.0 | All items are cyber breaches by definition |

### Data Defaults

All Webber Insurance events are created with:
- `event_type`: `DATA_BREACH` (the source only lists data breaches)
- `severity`: `MEDIUM` (conservative default; refined during enrichment)
- `australian_relevance`: `True` (source is exclusively Australian)
- `entity_type`: `OTHER` (requires LLM enrichment for specific classification)

---

## 6. GDELT (Deprecated)

**Implementation:** `cyber_data_collector/datasources/gdelt.py`
**Specification:** `specifications/gdelt_data_source.md`

### What is GDELT?

The [GDELT Project](https://www.gdeltproject.org/) (Global Database of Events, Language, and Tone) monitors news media worldwide and translates human activities into a structured database. It uses the CAMEO (Conflict and Mediation Event Observations) taxonomy to categorise events and provides a public BigQuery dataset (`gdelt-bq.gdeltv2.events`) updated every 15 minutes.

### How It Was Used

The pipeline queried GDELT via Google BigQuery for Australian cybersecurity events:

```sql
SELECT GLOBALEVENTID, DATEADDED, Actor1Name, Actor2Name, Actor1CountryCode,
       Actor2CountryCode, EventBaseCode, EventCode, GoldsteinScale, NumSources,
       SourceURL, ActionGeo_CountryCode, ActionGeo_FullName
FROM `gdelt-bq.gdeltv2.events`
WHERE CAST(DATEADDED AS STRING) >= @start_date
  AND CAST(DATEADDED AS STRING) <= @end_date
  AND (ActionGeo_CountryCode = 'AS'
       OR Actor1CountryCode = 'AS'
       OR Actor2CountryCode = 'AS')
  AND [cyber keyword and CAMEO code filters]
  AND NumSources >= 2
LIMIT @max_records
```

**Geographic filters:**
- `ActionGeo_CountryCode = 'AS'` — events occurring in Australia
- `Actor1CountryCode = 'AS'` or `Actor2CountryCode = 'AS'` — Australian actors involved

**CAMEO event codes used:**

| Code | Meaning |
|------|---------|
| 172 | Coerce cybernetically |
| 210 | Use conventional military force |
| 141 | Confiscate/establish blockade |

**Cyber keyword filters (11 categories):**
`data breach`, `cyber breach`, `security breach`, `ransomware`, `malware`, `cyber attack`, `cyberattack`, `phishing`, `ddos`, `credential`, `hack`, `vulnerability`, `exploit`

**Quality filter:** `NumSources >= 2` (at least two news sources must report the event)

### Why GDELT Was Removed from Default Sources

GDELT was excluded from the default pipeline for three reasons:

#### 1. Prohibitive Cost

GDELT data is accessed via Google BigQuery, which charges based on the volume of data scanned per query. The GDELT events table is extremely large (billions of rows spanning decades). Even with date and geographic filters, each query scans substantial data volumes.

**Documented cost estimates: $500-$2,000+ per usage period** (from `README.md`).

By contrast:
- Perplexity: ~$0.005 per query
- OAIC: Free (public government data)
- Google Custom Search: Free (100 queries/day free tier)
- Webber Insurance: Free (public webpage scraping)

#### 2. Poor Fit for Cybersecurity-Specific Data

The CAMEO taxonomy was designed for geopolitical events (conflict, diplomacy, protests), not cybersecurity. Only three CAMEO codes are relevant to cyber incidents (172, 141, 210), and these are imprecise:

- **Code 172 ("Coerce cybernetically")** is the only explicitly cyber-related code, but many cyber incidents are not coercion
- **Code 210 ("Use conventional military force")** captures some state-sponsored attacks but also unrelated military events
- **Code 141 ("Confiscate/establish blockade")** is a poor proxy for data breaches

This results in a **high false-positive rate** requiring aggressive keyword filtering and two-stage validation (discovery-stage and content-stage filtering) to remove irrelevant results.

#### 3. Operational Complexity

Using GDELT requires:
- A Google Cloud project with billing enabled
- BigQuery API enabled and configured
- Service account or OAuth credentials managed and refreshed
- Additional authentication setup script (`scripts/setup/setup_bigquery_auth.py`)

This infrastructure overhead is disproportionate given the other sources provide better cyber-specific coverage at lower cost.

### Current Status

The GDELT data source implementation is retained in the codebase for users who wish to enable it for specific research purposes (e.g., correlating cyber events with geopolitical events). It can be activated by explicitly passing `--source GDELT` to the pipeline:

```bash
python run_full_pipeline.py --source Perplexity OAIC GoogleSearch WebberInsurance GDELT
```

---

## 7. Source Comparison Matrix

| Attribute | Perplexity AI | OAIC | Google Custom Search | Webber Insurance | GDELT |
|-----------|---------------|------|----------------------|------------------|-------|
| **Status** | Active (default) | Active (default) | Active (default) | Active (default) | Deprecated |
| **Cost per run** | ~$1-5 | Free | Free (100/day) | Free | $500-2,000+ |
| **API key required** | Yes | No | Yes | No | Yes (Google Cloud) |
| **Coverage scope** | Australian cyber incidents (AI-synthesised) | NDB scheme notifications only | Broad web search results | Australian data breaches (curated) | Global events with AU filter |
| **Update frequency** | Real-time | Semi-annual reports; periodic media releases | Real-time index | Periodic (manual curation) | Every 15 minutes |
| **Credibility score** | 0.6 | 0.95-1.0 | 0.5 | 0.8 | Variable (from NumSources) |
| **Overall confidence** | 0.65 | 0.9 | 0.5 | 0.85 | 0.8 |
| **LLM at collection** | Yes (structured extraction) | No | No | No | No |
| **Geographic precision** | High (query-targeted) | Perfect (statutory AU source) | Medium (heuristic) | Perfect (AU-only source) | Medium (country code filter) |
| **Incident specificity** | High (AI filters general news) | Medium (aggregate stats + media releases) | Low (raw search results) | High (curated breach list) | Low (geopolitical taxonomy) |
| **Historical depth** | Limited by AI model training | 2018-present (NDB scheme start) | Current index only | Multi-year archive | 1979-present |
| **Unique contribution** | AI-synthesised multi-source discovery | Official statutory data | Broad web coverage | Curated specialist perspective | Geopolitical correlation |

---

## 8. Content Scraping and Extraction

**Implementation:** `cyber_data_collector/enrichment/content_acquisition.py`

After events are discovered, their source URLs are scraped for full article content. The content acquisition module uses a cascading extraction strategy:

| Priority | Method | Best For |
|----------|--------|----------|
| 1 | Cached content | Previously scraped content in `RawEvents.raw_content` |
| 2 | PDF extraction | URLs ending in `.pdf` |
| 3 | newspaper3k | Standard news articles |
| 4 | trafilatura | Difficult or non-standard sites |
| 5 | BeautifulSoup | Last-resort HTML parsing |
| 6 | Playwright | JavaScript-heavy sites |

### Trusted Source Reliability Scores

Australian government and news sources receive elevated reliability scores:

| Domain | Score | Type |
|--------|-------|------|
| oaic.gov.au | 1.0 | Government (statutory authority) |
| cyber.gov.au | 1.0 | Government (ACSC) |
| acsc.gov.au | 1.0 | Government (ACSC) |
| abc.net.au | 1.0 | Public broadcaster |
| smh.com.au | 0.95 | Major newspaper |
| theage.com.au | 0.95 | Major newspaper |
| afr.com | 0.95 | Financial press |
| itnews.com.au | 0.9 | Technology press |
| krebsonsecurity.com | 0.95 | Specialist cybersecurity journalism |

---

## 9. LLM-Based Filtering and Classification

All discovered events pass through multiple filtering and classification stages before storage.

### 9.1 GPT-4o-mini Fast Extraction (Discovery Phase)

**Implementation:** `cyber_data_collector/utils/llm_extractor.py`
**Model:** `gpt-4o-mini` | **Temperature:** `0.0` | **Max retries:** 2

Performs initial extraction on scraped article text (truncated to 12,000 characters):

```
You are an expert cybersecurity analyst. Your task is to analyze the provided news
article text and extract key information with high accuracy.
```

**Extracted fields:**

| Field | Description |
|-------|-------------|
| `is_australian_event` | Whether the event involves Australia, an Australian company, or Australian citizens |
| `is_specific_event` | Whether the article describes a specific, concrete cyber incident that has already occurred (vs. general advice, trend reports, or opinion pieces) |
| `primary_entity` | Main organisation targeted |
| `affected_entities` | Other named organisations mentioned |
| `summary` | Brief neutral summary |
| `event_date` | Actual incident date (not publication date) in YYYY-MM-DD format |
| `records_affected` | Number of individuals/records affected (integer) |

### 9.2 GPT-4o-mini LLM Classifier (Event Validation)

**Implementation:** `cyber_data_collector/processing/llm_classifier.py`
**Model:** `gpt-4o-mini` | **Concurrency:** 10 threads | **Timeout:** 60s

Validates whether each event is a genuine, specific cybersecurity incident affecting Australian entities:

**Key classification rules:**

| Accept | Reject |
|--------|--------|
| Specific incidents affecting named organisations | Summaries with "Multiple", "Several", "Various" |
| Actual data breaches, ransomware attacks, phishing campaigns | Time-period reports ("January 2020", "Q1 2020") |
| Incidents with identifiable victim, attack type, and timeframe | OAIC regulatory reports and summaries |
| | Policy documents, frameworks, guidelines |
| | Educational content, training materials |
| | Trend analyses, market reports |
| | Non-cyber events (celebrations, sports, elections) |

**Bias:** The classifier is explicitly instructed to bias toward inclusion — *"When in doubt about whether something is a specific incident affecting a named organization, ACCEPT it rather than reject it."*

**Classification outputs:**
- `event_type` — Classified into `CyberEventType` enum
- `severity` — Critical/High/Medium/Low/Unknown
- `detailed_description`, `technical_details`
- `estimated_customers_affected`, `estimated_financial_impact`
- `regulatory_fine`, `regulatory_undertaking`
- `attribution` — Threat actor if known

### 9.3 GPT-4o Primary Enrichment (Detailed Extraction)

**Implementation:** `cyber_data_collector/enrichment/gpt4o_enricher.py`
**Model:** `gpt-4o` | **Temperature:** `0.1` | **Response format:** forced JSON

The most detailed extraction stage, using GPT-4o's superior reasoning for:

1. **Victim organisation extraction** — Formal legal name (not brand or abbreviation), with few-shot examples for tricky cases (aggregate blogs, parent vs. subsidiary)
2. **Attacker information** — Threat actor name, type (nation_state, ransomware_gang, hacktivist, cybercriminal, insider, unknown), malware name, attack vector
3. **Incident details** — Severity, dates (incident/discovery/disclosure), records affected with strict validation:
   - Records must represent distinct individuals/customers (not transactions, files, or bytes)
   - Minimum: 50, Maximum: 1 billion
   - Unit parsing for "million", "thousand", "billion"
4. **Australian relevance assessment** — Relevance score (0.0-1.0)
5. **Event specificity assessment** — Is this a concrete incident with identifiable victim, attack type, and timeframe?
6. **Multi-victim detection** — Distinguishes one attack with multiple victims from aggregate roundup articles

### 9.4 Random Forest ML Filter

**Implementation:** `cyber_data_collector/filtering/rf_event_filter.py`
**Model type:** scikit-learn RandomForestClassifier (pre-trained)

A binary ML classifier that provides fast, non-LLM filtering:
- **Input features:** Source type (encoded) + TF-IDF vectorisation of combined title, description, content, and URL
- **Output:** `is_cyber_relevant` (boolean) + `confidence_score` (0.0-1.0)
- **Risk levels:** Low (>=0.8), Medium (0.4-0.8), High (<0.4)

### 9.5 Confidence-Based Progressive Filter

**Implementation:** `cyber_data_collector/filtering/confidence_filter.py`

Rule-based multi-stage filtering with configurable thresholds:

| Stage | Threshold | Purpose |
|-------|-----------|---------|
| Discovery | 0.2 | Very permissive — capture everything potentially relevant |
| Content | 0.4 | Balanced — filter after full content is available |
| Final | 0.6 | High precision — only confident events pass |

**Scoring signals:**
- **High-confidence cyber terms** (+0.2 to +0.4): `cyber attack`, `data breach`, `ransomware`, `malware`, `phishing`, `ddos`, `hacking`, `security breach`, `vulnerability`, `exploit`, `botnet`, `trojan`, `spyware`, `backdoor`
- **Medium-confidence terms** (+0.1 to +0.3): `virus`, `worm`, `firewall`, `encryption`, `authentication`, `intrusion`, `social engineering`, `fraud`
- **Australian context** (+0.05 to +0.15): Australian place names, `.gov.au` domains, regulatory body names
- **Negative indicators** (-0.15 to -0.6): Sports, entertainment, natural disasters, medical/health, education, transportation keywords

---

## 10. Confidence Scoring and Quality Assurance

### Multi-Dimensional Confidence

Every event carries a `ConfidenceScore` with five dimensions:

| Dimension | Description | Range |
|-----------|-------------|-------|
| `overall` | Weighted aggregate | 0.0-1.0 |
| `source_reliability` | How trustworthy is the data source? | 0.0-1.0 |
| `data_completeness` | How many fields are populated? | 0.0-1.0 |
| `temporal_accuracy` | How precise is the event date? | 0.0-1.0 |
| `geographic_accuracy` | How confident is the Australian relevance? | 0.0-1.0 |

### Cross-Source Validation

Events discovered by multiple sources are merged during deduplication (`cyber_data_collector/processing/deduplication_v2.py`), which uses:

1. **Entity-based matching** with a 0.15 similarity threshold
2. **Rule 1:** Same entity + same date → merge
3. **Rule 2:** Same entity + similar titles → merge
4. **LLM arbiter** (GPT-4o-mini) for borderline cases:

```
Are these the same cybersecurity incident? Consider:
1. Are they about the same breach/attack?
2. Do they involve the same organization?
3. Are the dates consistent with the same incident?
4. Are the details describing the same event?
```

The merged event uses the earliest event date from any source and tracks `total_data_sources` (count of contributing sources).

### Quality Hierarchy

When merging, data from higher-confidence sources takes precedence:

1. **OAIC** (0.9-1.0) — Statutory authority, legally mandated reporting
2. **Webber Insurance** (0.85) — Curated specialist source
3. **Perplexity AI** (0.65) — AI-synthesised, requires verification
4. **Google Custom Search** (0.5) — Raw search results, minimal initial processing

---

*Last updated: March 2026*
*Pipeline version: See `requirements.txt` for dependency versions*
