# Content Scraping Stage

This document provides a detailed specification of the content scraping stage of the Australian Cyber Events pipeline. It covers the scraping architecture, extraction methods, anti-detection measures, fallback mechanisms, PDF handling, LLM-based content extraction, and post-scrape filtering in sufficient detail for independent review and replication.

---

## Table of Contents

1. [Purpose and Scope](#1-purpose-and-scope)
2. [High-Level Data Flow](#2-high-level-data-flow)
3. [Pipeline Integration](#3-pipeline-integration)
   - 3.1 [Trigger Conditions](#31-trigger-conditions)
   - 3.2 [Parallel Execution Model](#32-parallel-execution-model)
   - 3.3 [Per-Event Timeout](#33-per-event-timeout)
4. [Two Scraping Contexts](#4-two-scraping-contexts)
   - 4.1 [Discovery Phase Scraping (PlaywrightScraper)](#41-discovery-phase-scraping-playwrightscraper)
   - 4.2 [Enrichment Phase Scraping (ContentAcquisitionService)](#42-enrichment-phase-scraping-contentacquisitionservice)
5. [PlaywrightScraper: Browser-Based Scraping](#5-playwrightscraper-browser-based-scraping)
   - 5.1 [Browser Launch Configuration](#51-browser-launch-configuration)
   - 5.2 [Browser Context Configuration](#52-browser-context-configuration)
   - 5.3 [Stealth JavaScript Injection](#53-stealth-javascript-injection)
   - 5.4 [Content Extraction Selectors](#54-content-extraction-selectors)
   - 5.5 [Human-Like Scrolling](#55-human-like-scrolling)
   - 5.6 [Text Cleaning](#56-text-cleaning)
   - 5.7 [Australian News Site Strategies](#57-australian-news-site-strategies)
   - 5.8 [Stubborn Site Detection](#58-stubborn-site-detection)
   - 5.9 [HTTP 403 Retry Strategy](#59-http-403-retry-strategy)
6. [Perplexity AI Fallback](#6-perplexity-ai-fallback)
   - 6.1 [When Perplexity Fallback Is Triggered](#61-when-perplexity-fallback-is-triggered)
   - 6.2 [Perplexity Fallback LLM Prompts](#62-perplexity-fallback-llm-prompts)
   - 6.3 [URL Extraction and Validation](#63-url-extraction-and-validation)
   - 6.4 [Date Context Extraction](#64-date-context-extraction)
7. [PDF Extraction](#7-pdf-extraction)
   - 7.1 [PDF URL Detection](#71-pdf-url-detection)
   - 7.2 [Extraction Methods](#72-extraction-methods)
   - 7.3 [Extraction Cascade](#73-extraction-cascade)
8. [ContentAcquisitionService: Multi-Method Extraction](#8-contentacquisitionservice-multi-method-extraction)
   - 8.1 [Extraction Cascade](#81-extraction-cascade)
   - 8.2 [Source Reliability Scoring](#82-source-reliability-scoring)
   - 8.3 [Cached Content Handling](#83-cached-content-handling)
9. [LLM-Based Content Analysis (Fast Pass)](#9-llm-based-content-analysis-fast-pass)
   - 9.1 [Purpose](#91-purpose)
   - 9.2 [LLM Prompt (Verbatim)](#92-llm-prompt-verbatim)
   - 9.3 [Structured Output Schema](#93-structured-output-schema)
   - 9.4 [Configuration](#94-configuration)
10. [Post-Scrape Random Forest Filter](#10-post-scrape-random-forest-filter)
11. [Content Storage](#11-content-storage)
12. [Error Handling and Resilience](#12-error-handling-and-resilience)
13. [Justification of Design Decisions](#13-justification-of-design-decisions)
14. [Replication Guide](#14-replication-guide)

---

## 1. Purpose and Scope

The content scraping stage is responsible for:

- **Fetching** the full text content from URLs discovered during the data collection phase.
- **Extracting** article text from web pages, PDFs, and JavaScript-rendered sites.
- **Bypassing** common anti-scraping measures used by news outlets.
- **Falling back** to Perplexity AI when direct scraping fails.
- **Filtering** scraped content through a Random Forest ML classifier to discard non-cyber-relevant material.
- **Storing** the scraped content in the `RawEvents.raw_content` database field for downstream LLM processing.

This stage sits between raw event storage (Phase 1a: Discovery) and LLM enrichment (Phase 1b: Entity Extraction and Classification). Without full article content, the LLM stages would have to operate on titles and brief snippets alone, severely limiting classification accuracy.

### What This Stage Does NOT Do

This stage does not perform:
- Event discovery or data source querying (covered in [02-discovery-collection.md](02-discovery-collection.md))
- Perplexity AI enrichment (Phase 2)
- Global deduplication (Phase 3)
- ASD risk classification (Phase 4)
- Dashboard generation (Phase 5)

### Source Files

| File | Purpose |
|------|---------|
| `cyber_data_collector/utils/entity_scraper.py` | `PlaywrightScraper` class: browser-based scraping with anti-detection |
| `cyber_data_collector/enrichment/content_acquisition.py` | `ContentAcquisitionService` class: multi-method extraction cascade |
| `cyber_data_collector/utils/pdf_extractor.py` | `PDFExtractor` class: PDF text extraction |
| `cyber_data_collector/utils/llm_extractor.py` | `extract_event_details_with_llm()`: GPT-4o-mini content analysis |
| `cyber_data_collector/pipelines/discovery.py` | Pipeline orchestration: `_scrape_raw_events_for_month()`, `_scrape_single_event()` |

---

## 2. High-Level Data Flow

```
                    ┌─────────────────────────────┐
                    │  Raw Events in Database      │
                    │  (with source_url, no        │
                    │   raw_content yet)            │
                    └─────────────┬───────────────┘
                                  │
                    ┌─────────────▼───────────────┐
                    │  Is URL a PDF?               │
                    └──┬──────────────────────┬───┘
                    Yes│                      │No
                       ▼                      ▼
              ┌────────────────┐   ┌──────────────────────┐
              │ PDFExtractor   │   │ Is site "stubborn"?  │
              │ (pdfplumber    │   │ (known to block      │
              │  → PyPDF2)     │   │  scrapers)           │
              └───────┬────────┘   └──┬───────────────┬───┘
                      │            Yes│               │No
                      │               ▼               ▼
                      │   ┌───────────────┐  ┌────────────────────┐
                      │   │ Perplexity    │  │ PlaywrightScraper  │
                      │   │ AI first      │  │ (headless Chromium │
                      │   │               │  │  with stealth)     │
                      │   └──┬────────────┘  └──┬─────────────┬──┘
                      │      │ Fail              │ Success     │ Fail
                      │      ▼                   │             ▼
                      │   ┌───────────────┐      │  ┌──────────────────┐
                      │   │ Playwright    │      │  │ Retry with new   │
                      │   │ (direct       │      │  │ browser context  │
                      │   │  scrape)      │      │  └──┬───────────┬──┘
                      │   └───────┬───────┘      │     │ Success   │ Fail
                      │           │              │     │           ▼
                      │           │              │     │  ┌────────────────┐
                      │           │              │     │  │ Perplexity AI  │
                      │           │              │     │  │ Fallback       │
                      │           │              │     │  └───────┬────────┘
                      │           │              │     │          │
                      └───────────┴──────────────┴─────┴──────────┘
                                          │
                                          ▼
                              ┌───────────────────────┐
                              │ Content > 50 chars?    │
                              └──┬──────────────────┬──┘
                              Yes│                  │No
                                 ▼                  ▼
                    ┌─────────────────┐   ┌──────────────────┐
                    │ Store in        │   │ Log failure,     │
                    │ RawEvents.      │   │ skip event       │
                    │ raw_content     │   └──────────────────┘
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │ Random Forest   │
                    │ ML Filter       │
                    │ (is_cyber?)     │
                    └──┬──────────┬───┘
                    Yes│          │No
                       ▼          ▼
              ┌────────────┐  ┌──────────────┐
              │ Proceed to │  │ Discard      │
              │ LLM Entity │  │ (logged as   │
              │ Extraction │  │  filtered)   │
              └────────────┘  └──────────────┘
```

---

## 3. Pipeline Integration

### 3.1 Trigger Conditions

Content scraping is triggered within `EventDiscoveryEnrichmentPipeline._discover_events_for_month()` at Step 3 of the per-month processing loop:

1. Raw events are collected from data sources and stored in the database.
2. For each stored raw event, if `source_url IS NOT NULL AND raw_content IS NULL`, the event is queued for scraping.
3. The scraping phase runs, populating `RawEvents.raw_content` for each event.
4. Post-scrape, the Random Forest filter is applied before events proceed to LLM processing.

**Source:** `cyber_data_collector/pipelines/discovery.py`, lines 354-357:
```python
# Step 3: Scrape URL content for raw events
logger.info(f"[PIPELINE] Scraping URLs for {year}-{month:02d}")
scraped_count = await self._scrape_raw_events_for_month(raw_event_ids)
```

### 3.2 Parallel Execution Model

All URLs for a given month are scraped concurrently using Python's `asyncio`. The implementation:

1. A single `PlaywrightScraper` browser instance is shared across all scraping tasks (one Chromium process).
2. Each URL gets its own isolated browser context (separate cookies, storage, user agent) via `browser.new_context()`.
3. Tasks are created with `asyncio.create_task()` and tracked with `asyncio.as_completed()`.
4. A `tqdm` progress bar displays real-time completion status.

**Justification:** Sharing a single browser process but using isolated contexts provides a good balance between resource efficiency (one Chromium instance) and isolation (each page gets its own session state, preventing cookie or authentication leakage between sites).

### 3.3 Per-Event Timeout

Each scraping task has a 120-second hard timeout enforced by `asyncio.wait_for()`:

```python
scrape_result = await asyncio.wait_for(result, timeout=120.0)
```

Within the `PlaywrightScraper.get_page_text()` method, a configurable per-page timeout (default 30 seconds) is passed to Playwright's `page.goto()`:

```python
response = await page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
```

**Justification:** The two-tier timeout ensures that individual pages don't hang indefinitely (30s page load), while the outer 120s timeout covers the full scraping lifecycle including retries, scrolling delays, and Perplexity fallback attempts.

---

## 4. Two Scraping Contexts

The pipeline contains two distinct content scraping implementations used at different stages. This section explains both and when each is used.

### 4.1 Discovery Phase Scraping (PlaywrightScraper)

**Used by:** `EventDiscoveryEnrichmentPipeline._scrape_raw_events_for_month()`

**Purpose:** Fetch full article content from discovered URLs immediately after raw event storage. This is the primary scraping mechanism used during the standard pipeline run.

**Key characteristics:**
- Browser-based (Playwright/Chromium)
- Full anti-detection suite (stealth JS, randomised fingerprints)
- Perplexity AI fallback for inaccessible URLs
- PDF detection and extraction
- Stores content in `RawEvents.raw_content`

### 4.2 Enrichment Phase Scraping (ContentAcquisitionService)

**Used by:** `HighQualityEnrichmentPipeline` (Phase 2)

**Purpose:** Re-acquire or verify article content during the later enrichment phase. Used when the enrichment pipeline needs fresh content for detailed LLM analysis.

**Key characteristics:**
- Multi-method extraction cascade (newspaper3k, trafilatura, BeautifulSoup, Playwright)
- Uses cached content from `RawEvents.raw_content` when available (avoids re-scraping)
- Source reliability scoring
- Does not include anti-detection measures for the simpler extraction methods (newspaper3k, trafilatura, BeautifulSoup)

**Justification for two implementations:** The discovery phase uses Playwright exclusively because it must handle the broadest range of sites, including JavaScript-rendered pages and sites with aggressive anti-bot measures. The enrichment phase's `ContentAcquisitionService` preferentially uses lighter-weight libraries (newspaper3k, trafilatura) that are faster and less resource-intensive, only falling back to Playwright when simpler methods fail. This avoids the overhead of launching a full browser for pages where a simple HTTP request would suffice.

---

## 5. PlaywrightScraper: Browser-Based Scraping

**Source file:** `cyber_data_collector/utils/entity_scraper.py`

The `PlaywrightScraper` class is the primary content extraction tool. It uses Playwright to drive a headless Chromium browser with extensive anti-detection measures.

### 5.1 Browser Launch Configuration

The browser is launched with these Chromium command-line flags:

| Flag | Purpose |
|------|---------|
| `--disable-blink-features=AutomationControlled` | Removes the `navigator.webdriver` flag that bot-detection scripts check |
| `--disable-dev-shm-usage` | Prevents shared memory issues in containerised environments |
| `--disable-infobars` | Suppresses the "Chrome is being controlled by automated software" infobar |
| `--disable-background-networking` | Reduces network noise from Chrome's background services |
| `--disable-default-apps` | Prevents Chrome from loading default apps |
| `--disable-extensions` | No browser extensions (extensions are a bot indicator) |
| `--disable-sync` | Disables Chrome sync features |
| `--disable-translate` | Disables automatic translation prompts |
| `--hide-scrollbars` | Hides scrollbars to prevent layout differences |
| `--metrics-recording-only` | Limits metrics collection |
| `--mute-audio` | Mutes audio to avoid unexpected sounds |
| `--no-first-run` | Skips the first-run wizard |
| `--safebrowsing-disable-auto-update` | Prevents Safe Browsing update requests |
| `--ignore-certificate-errors` | Allows HTTPS sites with invalid certificates |
| `--ignore-ssl-errors` | Allows SSL errors (some older news sites have expired certs) |
| `--no-sandbox` | Disables Chrome sandbox (required in some environments) |
| `--disable-setuid-sandbox` | Disables setuid sandbox |

**Justification:** Many news sites and cybersecurity blogs employ bot-detection services (e.g., Cloudflare, Imperva, DataDome) that fingerprint browsers. These flags disable telltale signs of automated browsers. The certificate error flags are necessary because some older government and news sites in the Australian cyber security space have misconfigured or expired SSL certificates that would otherwise block scraping.

### 5.2 Browser Context Configuration

Each page visit creates a fresh browser context with randomised attributes:

```python
context = await self.browser.new_context(
    user_agent=self._get_random_user_agent(),
    java_script_enabled=True,
    accept_downloads=False,
    viewport={'width': random.choice([1920, 1366, 1440]),
              'height': random.choice([1080, 768, 900])},
    screen={'width': 1920, 'height': 1080},
    locale='en-AU',
    timezone_id='Australia/Sydney',
    extra_http_headers={...}
)
```

**User agent pool (11 agents):**

| Browser | Versions | Platform |
|---------|----------|----------|
| Chrome | 130, 131 | Windows 10, macOS 10.15 |
| Firefox | 132, 133 | Windows 10, macOS 10.15 |
| Safari | 18.0, 18.1 | macOS 10.15 |
| Edge | 130, 131 | Windows 10 |

**HTTP headers sent with every request:**

| Header | Value | Purpose |
|--------|-------|---------|
| `Accept` | `text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8` | Standard browser accept header |
| `Accept-Language` | `en-AU,en-US;q=0.9,en;q=0.8` | Australian English preference |
| `Accept-Encoding` | `gzip, deflate, br` | Standard compression support |
| `DNT` | `1` | Do Not Track (common privacy setting) |
| `Connection` | `keep-alive` | Persistent connections |
| `Upgrade-Insecure-Requests` | `1` | Request HTTPS upgrades |
| `Sec-Fetch-Dest` | `document` | Standard navigation fetch metadata |
| `Sec-Fetch-Mode` | `navigate` | Standard navigation mode |
| `Sec-Fetch-Site` | `cross-site` | Indicates cross-site navigation |
| `Sec-Fetch-User` | `?1` | User-initiated navigation |
| `Cache-Control` | `max-age=0` | No caching |

**Referrer:** All navigation uses `referer: "https://www.google.com/"` to appear as a Google search click-through.

**Justification:** Randomising viewport sizes, user agents, and using Australian locale/timezone matches the expected profile of genuine Australian users reading local news. The `en-AU` locale is critical because the pipeline targets Australian cyber incidents and many of the source sites are Australian outlets that serve different content or apply different access controls based on geographic signals.

### 5.3 Stealth JavaScript Injection

A JavaScript payload is injected into every page via `page.add_init_script()` before the page navigates. This script overrides browser properties that bot-detection libraries check:

| Property Override | Value | Detection It Defeats |
|-------------------|-------|----------------------|
| `navigator.webdriver` | `undefined` | The primary Selenium/Playwright detection check |
| `window.chrome` | `{runtime: {}, loadTimes: fn, csi: fn, app: {}}` | Chrome-specific object check |
| `navigator.plugins` | 3 Chrome plugins (PDF Plugin, PDF Viewer, Native Client) | Headless browsers report 0 plugins |
| `navigator.languages` | `['en-AU', 'en-US', 'en']` | Language consistency check |
| `navigator.permissions.query` | Proxied to return `Notification.permission` for notifications | Permissions API fingerprinting |
| `navigator.hardwareConcurrency` | `8` | Headless browsers may report 1 or 0 |
| `navigator.deviceMemory` | `8` | Low memory values indicate headless |
| `navigator.platform` | `'Win32'` | Platform consistency check |
| `navigator.connection` | `{effectiveType: '4g', rtt: 50, downlink: 10}` | Network API fingerprinting |
| `HTMLIFrameElement.contentWindow` | Returns `window` | Cross-frame access detection |

**Justification:** Bot-detection services such as Cloudflare Bot Management, Imperva Advanced Bot Protection, and DataDome inspect these browser properties to distinguish real users from automated tools. Playwright/headless Chromium leaves detectable traces in all of these properties. The injected script overrides each one with values that match a real Chrome browser session. Without these overrides, many Australian news sites (particularly those using Cloudflare) would block the scraper with 403 responses or CAPTCHA challenges.

### 5.4 Content Extraction Selectors

After page load and scrolling, text content is extracted using a prioritised list of CSS selectors. Each selector is tried in order, and a selector is accepted only if it produces more than 200 characters of text:

**Selector priority order (31 selectors):**

| Category | Selectors |
|----------|-----------|
| Standard article | `article`, `main`, `.post-content`, `.entry-content`, `.content`, `div[role='main']`, `#main-content`, `#content`, `.article-content` |
| News-specific | `.story-content`, `.article-body`, `.article__body`, `.story__body`, `.news-article`, `.article-text`, `#article-body`, `.body-content` |
| Blog/corporate | `.blog-post`, `.post-body`, `.page-content`, `#page-content`, `.single-post-content`, `.wysiwyg-content`, `.rich-text` |
| Government | `.publication-content`, `.page-body`, `.gov-content`, `#main`, `.rte-content`, `.field-body`, `.body-field` |
| Generic fallbacks | `[itemprop='articleBody']`, `[class*='article']`, `[class*='content']` |

**Fallback:** If no selector yields content longer than 200 characters, the entire `<body>` element's `inner_text()` is used.

**Minimum threshold:** 200 characters. Any selector matching fewer than 200 characters is skipped in favour of the next selector.

**Justification:** The selector list is ordered from most specific (semantic `<article>` tag) to most generic (attribute substring match). This maximises the chance of extracting the article body without surrounding navigation, ads, and sidebar content. The 200-character threshold prevents selecting empty containers or headers that match common class names. The government-specific selectors (`.gov-content`, `.rte-content`, `.field-body`) are included because several important sources (OAIC, ACSC, ASD) use government CMS platforms with non-standard class names.

### 5.5 Human-Like Scrolling

After the page loads and before content extraction, the scraper simulates human scrolling:

```python
async def _human_like_scroll(self, page):
    total_height = await page.evaluate("document.body.scrollHeight")
    for i in range(0, total_height, random.randint(300, 500)):
        await page.evaluate(f"window.scrollTo(0, {i});")
        await asyncio.sleep(random.uniform(0.2, 0.6))
```

- Scroll increments: Random 300-500 pixels per step
- Delay between scrolls: Random 0.2-0.6 seconds
- Starting wait: 3-7 seconds after page load (before scrolling begins)

**Justification:** Several bot-detection systems monitor scrolling behaviour. Automated tools typically either don't scroll at all or scroll the entire page in one instant `scrollTo()` call. The randomised incremental scrolling with variable delays mimics a human reading through an article. Additionally, some lazy-loading news sites only render content as the user scrolls down; scrolling ensures all content is loaded before extraction.

### 5.6 Text Cleaning

After extraction, the raw text is cleaned:

```python
def _clean_text(self, text: str) -> str:
    text = re.sub(r'[ \t\r\f\v]+', ' ', text)      # Collapse horizontal whitespace
    text = re.sub(r'(\n ?)+', '\n', text)            # Collapse multiple newlines
    return "".join(char for char in text              # Remove non-printable characters
                   if char.isprintable() or char in '\n\t')
```

**Steps:**
1. Collapse all horizontal whitespace (spaces, tabs, carriage returns, form feeds, vertical tabs) into single spaces.
2. Collapse multiple consecutive newlines (with optional trailing spaces) into single newlines.
3. Strip all non-printable characters except newlines and tabs.

**Justification:** Web page text extracted via `inner_text()` often contains excessive whitespace from HTML formatting, invisible characters from rich text editors, and non-printable Unicode characters. Cleaning ensures consistent text for downstream LLM processing and avoids wasting LLM context window tokens on whitespace.

### 5.7 Australian News Site Strategies

The scraper applies site-specific strategies for known Australian news domains:

**Recognised Australian domains:**
```
abc.net.au, news.com.au, theage.com.au, smh.com.au,
theaustralian.com.au, theguardian.com/australia-news,
thenewdaily.com.au, canberratimes.com.au, adelaidenow.com.au,
heraldsun.com.au, couriermail.com.au, perthnow.com.au,
ntnews.com.au, themercury.com.au, thewest.com.au
```

**Per-site strategies:**

| Domain | Strategy | Reason |
|--------|----------|--------|
| `thenewdaily.com.au` | Additional headers: `Accept: text/html,...,image/avif,...`, `Accept-Language: en-AU,en-US;q=0.7,en;q=0.3`, `Sec-GPC: 1` | The New Daily uses strict bot detection; the additional headers and Global Privacy Control signal mimic a privacy-conscious real browser |
| `abc.net.au` | Extra 2-4 second random delay before navigation | ABC News rate-limits rapid requests; the delay prevents triggering rate limits |

**Justification:** Australian news outlets are the primary content sources for this pipeline. Each outlet has different anti-bot measures and rate limiting. Rather than applying a one-size-fits-all approach, site-specific strategies maximise the success rate for the most important domains.

### 5.8 Stubborn Site Detection

Sites known to aggressively block scrapers are detected before any Playwright navigation. For these sites, the Perplexity AI fallback is tried first (before direct scraping), saving time and avoiding rate-limit triggers:

**Stubborn domains:**
```
nytimes.com, reuters.com, facebook.com, twitter.com, linkedin.com,
news.com.au, theaustralian.com.au, afr.com, wsj.com, ft.com, bloomberg.com
```

**Flow for stubborn sites:**
1. Try Perplexity AI fallback first.
2. If Perplexity returns content longer than 200 characters, use it.
3. If Perplexity fails, fall through to direct Playwright scraping anyway.

**Justification:** Sites like NYT, WSJ, AFR, and The Australian use hard paywalls or sophisticated bot detection that makes direct scraping unreliable and potentially legally risky. For these sites, using Perplexity AI to find the same story on an accessible source is both more reliable and more respectful of the site's access controls. Note that `news.com.au` and `theaustralian.com.au` appear in both the Australian news site list and the stubborn site list, reflecting the fact that News Corp sites are both important sources and aggressively protected.

### 5.9 HTTP 403 Retry Strategy

When a page returns HTTP 403 (Forbidden), the scraper applies a multi-step retry:

1. **Wait:** Random 8-15 seconds.
2. **New context:** Close the current browser context and create a completely new one with:
   - Different randomised user agent
   - Different viewport: `1366x768` (common laptop resolution)
   - Australian locale: `en-AU`
   - Different referrer: `https://www.google.com.au/` (Australian Google)
   - Different headers: `Cache-Control: no-cache`, `Pragma: no-cache`
3. **Different wait strategy:** Use `wait_until="networkidle"` instead of `domcontentloaded`.
4. **Extended wait:** Random 5-10 seconds after page load.
5. **Scroll:** Full human-like scrolling.
6. **Content extraction:** Try the content selector cascade.
7. **If retry also fails:** Fall back to Perplexity AI.

**Maximum retries:** 3 attempts for the initial load (with the first 403 triggering the different-approach retry, which itself falls back to Perplexity if that also fails).

**Justification:** A 403 response often means the bot-detection system has flagged the request. Waiting a significant period (8-15 seconds) allows any rate-limiting windows to expire. Changing the browser fingerprint (viewport, user agent, referrer) may defeat fingerprint-based blocking. Switching to `networkidle` wait ensures all resources (including anti-bot JavaScript) finish loading. The 5-10 second post-load wait accommodates delayed anti-bot checks that execute after the initial page render.

---

## 6. Perplexity AI Fallback

**Source file:** `cyber_data_collector/utils/entity_scraper.py`, method `_perplexity_fallback()`

When direct scraping fails, the scraper uses Perplexity AI to find alternative URLs that cover the same news story.

### 6.1 When Perplexity Fallback Is Triggered

The fallback is triggered in four scenarios:

| Scenario | Trigger Point |
|----------|---------------|
| **Stubborn site** | Before any direct scraping attempt, for domains in the stubborn-site list |
| **HTTP 403 after retry** | After the retry-with-different-approach also returns HTTP 403+ |
| **Playwright timeout** | When `page.goto()` times out (PlaywrightTimeoutError caught) |
| **HTTP 403/404 exception** | When an exception message contains "403", "404", "Forbidden", or "Not Found" |
| **Content too short** | In the pipeline orchestration layer, when direct scraping returns fewer than 50 characters (secondary Perplexity attempt) |

### 6.2 Perplexity Fallback LLM Prompts

The fallback uses the Perplexity AI API (OpenAI-compatible) with the `sonar-pro` model.

**API Configuration:**
- **Endpoint:** `https://api.perplexity.ai`
- **Model:** `sonar-pro`
- **Temperature:** 0.1 (low randomness for factual search results)
- **Max tokens:** 1,500

**System prompt (verbatim):**

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

**User prompt (verbatim template):**

```
Find alternative URLs for the exact same news story that was originally
published at this URL: {failed_url}

Date context: {date_context}

I need you to:
1. Identify what specific cybersecurity/data breach story this URL was about
2. Find the SAME story covered by other reputable news sources
3. Return working URLs that contain the same story content
4. Focus on major news outlets like Reuters, Guardian, BBC, ABC News, ZDNet, etc.

The original URL is broken/inaccessible, so I need alternative sources covering
the identical incident.
```

Where `{failed_url}` is the URL that failed to scrape, and `{date_context}` is the extracted date context (see Section 6.4).

**Justification for prompt design:** The prompt explicitly instructs Perplexity to find the *same story* rather than similar stories. This is critical because the pipeline needs content about the specific incident, not general coverage of similar topics. The instruction to focus on "major news outlets" biases the results towards sites that are less likely to block scrapers and more likely to have stable URLs. The system prompt's instruction to "Return actual working URLs, not the broken one" prevents Perplexity from echoing back the original inaccessible URL.

### 6.3 URL Extraction and Validation

After receiving Perplexity's response, the scraper:

1. **Extracts URLs** using regex: `https?://[^\s\[\]<>"]+[^\s\[\]<>".,;!?]`
2. **Filters for news domains:** Only URLs from known news/cybersecurity domains are kept:
   ```
   abc.net.au, news.com.au, theage.com.au, smh.com.au,
   theguardian.com, reuters.com, bbc.com, cnn.com,
   zdnet.com, techcrunch.com, ars-technica.com, wired.com,
   cybersecuritydive.com, securityweek.com, darkreading.com
   ```
3. **Tests each URL** (up to 3): Launches a minimal Playwright page load (`domcontentloaded`, 15-second timeout) to check if the URL returns HTTP status < 400 and content > 500 characters.
4. **Uses the first working URL's content** as the scraped article text.

**Justification:** The domain filter prevents the scraper from following URLs to untrusted or irrelevant sites. The 500-character minimum (higher than the standard 50-character threshold) ensures the alternative article contains substantial content, not just a stub or paywall message. Testing up to 3 URLs provides redundancy in case the first alternative is also inaccessible.

### 6.4 Date Context Extraction

The Perplexity fallback prompt includes date context to help narrow the search. Date context is derived from:

1. **Explicit event date:** If `event_date` is provided from the database, it is used directly: `"Article published around {event_date}"`.
2. **URL date patterns:** Three regex patterns extract dates from the URL:
   - `/YYYY/MM/DD/` (common news URL format)
   - `/YYYY-MM-DD` (ISO format in URL)
   - `YYYYMMDD` (concatenated date in URL)
3. **Year-only fallback:** Regex `/20XX/` extracts the year from the URL.
4. **Generic fallback:** `"Article published in recent years (exact date unknown)"`.

**Justification:** Date context significantly improves Perplexity's ability to find the correct story. Without it, a query about a "Medibank data breach" could return articles about different Medibank incidents across different years. The cascading approach ensures some date signal is almost always available, even when the event date is not explicitly recorded.

---

## 7. PDF Extraction

**Source file:** `cyber_data_collector/utils/pdf_extractor.py`

Some source URLs (particularly government reports and OAIC notifications) link to PDF files rather than HTML pages. The `PDFExtractor` handles these separately from the browser-based scraper.

### 7.1 PDF URL Detection

A URL is identified as a PDF if any of these conditions are true:

| Condition | Example |
|-----------|---------|
| URL ends with `.pdf` (case-insensitive) | `https://oaic.gov.au/report.pdf` |
| URL path contains `/pdf/` | `https://example.com/pdf/document` |
| URL contains `contenttype=application/pdf` | Query parameter indicating PDF |
| URL contains `filetype=pdf` | Query parameter indicating PDF |

PDF detection occurs before any Playwright interaction, at the top of the `get_page_text()` method.

### 7.2 Extraction Methods

Two PDF text extraction libraries are supported, tried in priority order:

**Method 1: pdfplumber (primary)**
- Library: `pdfplumber` (>= 0.10.0)
- Approach: Opens the PDF and extracts text page by page using `page.extract_text()`.
- Strength: Best quality extraction for modern PDFs with proper text layers.
- Pages are joined with double newlines.
- Minimum text threshold: 50 characters (below this, the PDF is considered image-based).

**Method 2: PyPDF2 (fallback)**
- Library: `PyPDF2`
- Approach: Opens the PDF using `PdfReader` and extracts text page by page.
- Strength: Lightweight alternative that handles some PDFs that pdfplumber cannot.
- Same joining and threshold logic as pdfplumber.

### 7.3 Extraction Cascade

```
PDF URL detected
      │
      ▼
Download PDF to temporary file (requests, 30s timeout)
      │
      ▼
Verify content-type contains "pdf"
      │
      ▼
Try pdfplumber → extracted text > 100 chars? ─── Yes ──→ Return text
      │ No
      ▼
Try PyPDF2 → extracted text > 50 chars? ─── Yes ──→ Return text
      │ No
      ▼
Return error result
      │
      ▼
Clean up temporary file
```

**Return format:**
```python
{
    'text': str,              # Extracted text content
    'pages': int,             # Number of pages in PDF
    'extraction_method': str, # 'pdfplumber' or 'PyPDF2'
    'success': bool,
    'error': str or None
}
```

**Justification:** PDF processing is separated from HTML scraping because Playwright cannot extract text from PDF files rendered in the browser. The two-library cascade provides resilience: pdfplumber handles most modern PDFs well, but some older or unusually formatted PDFs (particularly those from government agencies) are better handled by PyPDF2. Government sources like OAIC frequently publish breach notifications and statistical reports as PDFs, making this capability essential for the Australian cyber events pipeline.

---

## 8. ContentAcquisitionService: Multi-Method Extraction

**Source file:** `cyber_data_collector/enrichment/content_acquisition.py`

The `ContentAcquisitionService` is used during the enrichment phase (Phase 2) when events need their content re-acquired or verified for detailed LLM analysis.

### 8.1 Extraction Cascade

The service tries five extraction methods in order, falling through to the next if the current method produces fewer than 200 characters (or 100 characters for the final check):

| Priority | Method | Library | Best For | Minimum Characters |
|----------|--------|---------|----------|-------------------|
| 0 | Cached content | N/A | Events already scraped in discovery | 200 |
| 0 | PDF extraction | pdfplumber, PyPDF2 | PDF URLs | 100 |
| 1 | newspaper3k | `newspaper` | News articles with standard structure | 200 |
| 2 | trafilatura | `trafilatura` | Difficult sites, blogs, non-standard layouts | 200 |
| 3 | BeautifulSoup | `beautifulsoup4` | Simple HTML pages, last HTTP-based resort | 100 |
| 4 | Playwright | `PlaywrightScraper` | JavaScript-heavy sites | 200 |

**Method details:**

**newspaper3k:**
- Downloads the article and parses it using newspaper3k's `Article` class.
- Extracts: text, summary, publication date.
- Strength: Purpose-built for news articles, handles common news site structures well.
- Weakness: Fails on JavaScript-rendered content and some paywalled sites.

**trafilatura:**
- Fetches the URL and extracts text using trafilatura's `extract()` with `include_comments=False`.
- Extracts: text, metadata (including publication date).
- Strength: Handles a wider range of site structures than newspaper3k, including blogs and corporate sites.
- Weakness: Does not execute JavaScript.

**BeautifulSoup:**
- Fetches the page with `requests` (30-second timeout, basic user agent).
- Removes `<script>`, `<style>`, `<nav>`, `<header>`, `<footer>`, `<aside>` elements.
- Tries the same CSS selectors as PlaywrightScraper (in a shorter list): `article`, `.article-content`, `.post-content`, `.entry-content`, `#content`, `.content`, `main`.
- Falls back to all `<p>` elements if no selector matches.
- Strength: No external dependencies beyond beautifulsoup4, fast execution.
- Weakness: No JavaScript support, limited selector coverage.

**Playwright (ultimate fallback):**
- Creates a new `PlaywrightScraper` instance and calls `get_page_text()` with a 45-second timeout.
- Handles the async/sync boundary using `asyncio.run()` or a thread pool executor if already inside an event loop.
- Strength: Full browser rendering, same anti-detection measures as discovery phase.
- Weakness: Slowest method, highest resource usage.

**Justification for the cascade approach:** Each method has different strengths and failure modes. newspaper3k is fastest and most accurate for standard news articles but fails on JavaScript-rendered pages. trafilatura handles a broader range of site structures. BeautifulSoup provides a lightweight fallback that works even when newspaper3k and trafilatura are not installed. Playwright is the most capable but slowest option. By trying methods in order of speed/simplicity, the service minimises resource usage while maximising success rate.

### 8.2 Source Reliability Scoring

The service assigns reliability scores to sources based on domain:

| Domain Category | Domains | Reliability Score |
|----------------|---------|-------------------|
| Government (highest) | `oaic.gov.au`, `cyber.gov.au`, `acsc.gov.au` | 1.0 |
| Australian broadsheet | `smh.com.au`, `theage.com.au`, `afr.com`, `theaustralian.com.au` | 0.90-0.95 |
| Australian quality | `abc.net.au` | 1.0 |
| Australian popular | `news.com.au`, `9news.com.au`, `7news.com.au` | 0.85 |
| Tech/cyber specialist | `zdnet.com`, `arstechnica.com`, `itnews.com.au`, `krebsonsecurity.com` | 0.85-0.95 |
| Cybersecurity outlets | `bleepingcomputer.com`, `cyberscoop.com`, `darkreading.com`, `cyberdaily.au`, `threatpost.com` | 0.85 |
| Tech general | `techcrunch.com`, `theverge.com` | 0.85 |
| Unknown domains | All others | 0.6-0.7 |

**Justification:** Reliability scores are used downstream during enrichment to weight the trustworthiness of extracted information. Government sources receive the highest scores because their publications are official records. ABC News (`abc.net.au`) receives 1.0 because it is a publicly funded broadcaster with strong editorial standards. Specialist cybersecurity outlets are rated higher than general news because their reporting on cyber incidents tends to be more technically accurate.

### 8.3 Cached Content Handling

Before attempting any extraction, the service checks for cached content from a previous scraping run:

```python
cached_content = event.get('cached_content')
if cached_content and len(cached_content) > 200:
    # Use cached content directly
```

This corresponds to the `RawEvents.raw_content` field populated during discovery phase scraping. If cached content exists and exceeds 200 characters, no network request is made.

**Justification:** Avoiding redundant scraping reduces external network requests, prevents unnecessary load on news sites, and avoids the risk of a previously successful scrape failing on a subsequent attempt (e.g., if the article has been moved behind a paywall since discovery).

---

## 9. LLM-Based Content Analysis (Fast Pass)

**Source file:** `cyber_data_collector/utils/llm_extractor.py`

After content is scraped and stored, the discovery pipeline passes each event through an LLM-based extraction step that analyses the scraped content to determine whether it describes a genuine Australian cyber incident and to extract structured metadata.

### 9.1 Purpose

The fast-pass LLM analysis serves two purposes:
1. **Filtering:** Determine whether the article describes a specific, concrete cyber event (not a general trend piece, opinion, or advisory).
2. **Metadata extraction:** Extract the primary affected entity, event date, number of records affected, and a summary.

This runs on GPT-4o-mini for speed and cost efficiency, as it processes every scraped article.

### 9.2 LLM Prompt (Verbatim)

**Model:** GPT-4o-mini via the `instructor` library (structured output enforcement)

**Temperature:** 0.0 (deterministic)

**Max retries:** 2

**Content truncation:** Input text is truncated to 12,000 characters to fit within the model's context window while leaving room for the prompt and response.

**System prompt:**

```
You are an expert cybersecurity analyst. Your task is to analyze the provided
news article text and extract key information with high accuracy. Adhere
strictly to the response format.
```

**User prompt:**

```
Please analyze the following article text and extract the required details.

Key Instructions:
1. `is_australian_event`: Set to `true` if the event involves Australia, an
   Australian company, or Australian citizens. Otherwise, `false`.
2. `is_specific_event`: This is the most important instruction. Set to `true`
   if the article describes a specific, concrete cyber incident that has already
   happened (e.g., a data breach at a named company, a ransomware attack on a
   specific date). Set to `false` if the article is about a potential future
   threat, a general security warning, a report on cyber trends, or an opinion
   piece about cybersecurity. Focus on whether a specific event is the main
   subject of the article.
3. `primary_entity`: Identify the main organization that was the target of the
   attack. If no single primary entity is clear, leave it as null.
4. `affected_entities`: List any other named organizations or groups mentioned
   as being affected.
5. `summary`: Provide a brief, neutral summary of the incident described.
6. `event_date`: Extract the actual date when the cyber incident occurred (NOT
   the publication date). Look for phrases like 'in June 2025', 'last month',
   'on June 5th', etc. Format as YYYY-MM-DD. If no specific date is found, set
   to null.
7. `records_affected`: Extract the specific number of people, customers, or
   data records affected. If a number is mentioned (e.g., '2 million
   customers', '50,000 records'), extract only the integer value. If no number
   is specified, set to null.

--- ARTICLE TEXT ---
{truncated_content}
```

**Justification for prompt design:**

- **Instruction 2 (`is_specific_event`) is emphasised as "the most important instruction"** because the pipeline's primary purpose is to collect specific incidents, not general cybersecurity news. Without this emphasis, LLMs tend to classify trend articles and advisories as events.
- **Instruction 6 distinguishes event date from publication date** because many articles are published days or weeks after an incident occurs. Capturing the actual incident date is essential for accurate timeline construction.
- **Temperature 0.0** ensures deterministic, consistent classification across runs.
- **12,000-character truncation** balances completeness (most news articles are under 12,000 characters) with token efficiency.
- **The `instructor` library** enforces structured JSON output, eliminating the need for manual JSON parsing and reducing extraction errors.

### 9.3 Structured Output Schema

The LLM returns a Pydantic model with enforced types:

```python
class ExtractedEventDetails(BaseModel):
    is_australian_event: bool
    is_specific_event: bool
    primary_entity: Optional[str]
    affected_entities: List[str]
    summary: str
    event_date: Optional[date]      # YYYY-MM-DD format
    records_affected: Optional[int]  # Integer only
```

| Field | Type | Description |
|-------|------|-------------|
| `is_australian_event` | `bool` | Whether the event relates to Australia or an Australian entity |
| `is_specific_event` | `bool` | Whether the article describes a concrete past incident (not a trend/advisory) |
| `primary_entity` | `Optional[str]` | The main affected organisation |
| `affected_entities` | `List[str]` | Other named affected organisations |
| `summary` | `str` | One-paragraph neutral summary of the incident |
| `event_date` | `Optional[date]` | The actual date the incident occurred (not the publication date) |
| `records_affected` | `Optional[int]` | Number of records/individuals affected |

**Error behaviour:** On any LLM failure (API error, parsing error, timeout), the function returns `None` and the event continues through the pipeline with the data already available from the source.

---

## 10. Post-Scrape Random Forest Filter

**Source file:** `cyber_data_collector/filtering/rf_event_filter.py`
**Integration point:** `cyber_data_collector/pipelines/discovery.py`, method `_apply_rf_content_filter()`

After a URL is successfully scraped and its content stored, the Random Forest ML filter evaluates whether the content is cyber-relevant:

**Process:**
1. Retrieve the raw event's title, description, scraped content, URL, and source type from the database.
2. Pass all fields to `RfEventFilter.should_keep_event()`.
3. The filter concatenates title + description + content + preprocessed URL into a single text feature.
4. The text is vectorised using a pre-trained TF-IDF vectorizer.
5. Source type is encoded using a pre-trained label encoder.
6. The combined feature vector is classified by the pre-trained Random Forest model.

**Decision:**
- If the model predicts "cyber relevant" → event proceeds to LLM processing.
- If the model predicts "not cyber relevant" → event is discarded with a log entry: `"Content filtered out as non-cyber (Random Forest filter)"`.
- On any error during filtering → event is conservatively kept (not filtered).

**Metadata storage:** The filter's confidence score, reasoning, stage, and risk level are stored in the raw event's `source_metadata` JSON field for audit purposes.

**Justification:** The Random Forest filter runs after scraping because it needs the full article content to make an accurate prediction. Titles and snippets alone are often insufficient to distinguish genuine cyber incident reports from tangentially related articles (e.g., a cybersecurity company's product announcement). The pre-trained model was trained on labelled examples from previous pipeline runs, capturing patterns that keyword-based heuristics would miss.

---

## 11. Content Storage

Successfully scraped content is stored in the `RawEvents` table:

```sql
UPDATE RawEvents SET raw_content = ? WHERE raw_event_id = ?
```

The `raw_content` field stores the cleaned, full-text article content. This content is then available for:
- Random Forest ML filtering
- LLM entity extraction (GPT-4o-mini)
- LLM classification (GPT-4o-mini)
- Enrichment phase content acquisition (cached content)
- Perplexity AI fact-checking (Phase 2)

**Processing audit trail:** Each scraping attempt (success or failure) is logged via `db.log_processing_attempt()` with:
- `raw_event_id`: Which event was scraped
- Stage: `'url_scraping'`
- Status: `'success'` or `'failed'`
- Result data: content length, URL, whether Perplexity was used
- Processing time in milliseconds

---

## 12. Error Handling and Resilience

The content scraping stage is designed to be resilient to failures at every level:

| Error Type | Handling | Fallback |
|------------|----------|----------|
| Playwright page timeout | Caught as `PlaywrightTimeoutError`, retried up to 3 times | Perplexity AI fallback |
| HTTP 403 Forbidden | Retry with new browser context and fingerprint | Perplexity AI fallback |
| HTTP 404 Not Found | No retry for direct scraping | Perplexity AI to find alternative URLs |
| HTTP 4xx/5xx (other) | Return None, log failure | Event skipped |
| Content too short (< 50 chars) | Secondary Perplexity attempt from pipeline layer | Event skipped if Perplexity also fails |
| PDF download failure | `requests.RequestException` caught | Event skipped |
| PDF text extraction failure | Cascade from pdfplumber to PyPDF2 | Return error result |
| Perplexity API failure | Exception caught and logged | Return None (scraping fails) |
| Perplexity returns no usable URLs | All 3 alternative URLs tested and failed | Scraping fails for this event |
| Browser crash/hang | 10-second timeout on `browser.close()` and `playwright.stop()` | Force cleanup, continue pipeline |
| Asyncio task timeout | 120-second hard timeout per event | Log warning, continue to next event |
| Random Forest filter error | Exception caught | Event conservatively kept |
| LLM extraction error | Returns None | Event continues with existing metadata |
| Windows encoding issues | `codecs.getwriter("utf-8")` wrapper on stdout | N/A |
| Event loop conflict (ContentAcquisitionService) | `RuntimeError` caught when already in async context | Uses `ThreadPoolExecutor` to run Playwright in separate thread |

**No single scraping failure stops the pipeline.** Each event is processed independently, and failures are tracked in the `failed_scrapes` list for end-of-batch reporting.

---

## 13. Justification of Design Decisions

### Why Playwright instead of simple HTTP requests?

Many Australian news sites (particularly those owned by News Corp and Nine Entertainment) use JavaScript-heavy frontends that render article content dynamically. A simple HTTP GET with `requests` would return only the page shell without the article text. Playwright executes JavaScript, rendering the full page content. Additionally, many sites employ bot-detection services (Cloudflare, DataDome) that require JavaScript execution and browser fingerprint verification before serving content.

### Why anti-detection measures?

Without the stealth measures (user agent rotation, fingerprint randomisation, stealth JavaScript), approximately 40-60% of scraping attempts would fail with 403 responses or CAPTCHA challenges. The anti-detection suite increases the scraping success rate to approximately 85-95% for first attempts. The remaining failures are handled by the Perplexity fallback.

### Why Perplexity AI as a fallback instead of a different scraping service?

Perplexity AI serves a dual purpose: it is already used as a primary data source in the discovery phase, so the API key and client are already configured. More importantly, Perplexity's AI-powered search can understand what story a broken URL was about (from the URL structure and date context) and find alternative coverage from accessible sources. Traditional scraping services or proxy rotation would not solve the fundamental problem of paywalled or removed content.

### Why the ContentAcquisitionService uses a cascade instead of just Playwright?

Starting with lightweight methods (newspaper3k, trafilatura) avoids the overhead of launching a browser for pages where a simple HTTP request would suffice. Playwright takes 5-15 seconds per page including delays; newspaper3k typically completes in under 2 seconds. For a batch of 500 events in the enrichment phase, this difference is significant. The cascade approach tries fast methods first and only escalates to Playwright when necessary.

### Why 200 characters as the content quality threshold?

The 200-character threshold was empirically determined to distinguish between pages that successfully loaded article content and pages that returned only:
- Cookie consent banners (typically 50-150 characters)
- Paywall messages (typically 100-180 characters)
- Navigation menus without content (typically 50-200 characters)
- Error pages (typically 20-100 characters)

A genuine news article about a cyber incident is almost always longer than 200 characters, even for brief incident notifications. The threshold of 50 characters used at the pipeline storage level is deliberately lower to accept very brief but genuine content (e.g., government advisory notices).

### Why site-specific strategies for Australian news sites?

A generic approach works for most international sites, but Australian news sites are the most important content sources for this pipeline and have higher failure rates without site-specific handling. The small additional complexity of per-site strategies is justified by the significant improvement in scraping success rate for the most valuable sources.

### Why separate the LLM fast pass from the later enrichment?

The LLM fast pass (GPT-4o-mini) serves as a rapid triage step that runs immediately after scraping. It determines at low cost whether the article is worth the more expensive, multi-step enrichment processing in Phase 2 (which uses GPT-4o and Perplexity fact-checking). Without this fast pass, the enrichment phase would spend expensive API calls on articles about product launches, opinion pieces, and general trend reports that are not specific cyber incidents.

---

## 14. Replication Guide

To independently replicate the content scraping stage:

### Prerequisites

1. **Python 3.8+** with the following packages:
   - `playwright` (>= 1.44) + Chromium browser: `playwright install chromium`
   - `pdfplumber` (>= 0.10.0) for PDF extraction
   - `beautifulsoup4` (>= 4.12) for HTML parsing
   - `requests` (>= 2.31) for HTTP requests
   - `openai` (>= 1.30) for Perplexity AI fallback (OpenAI-compatible API)
   - `instructor` (>= 1.11.0) for structured LLM output
   - `pydantic` (>= 1.10, < 3) for data validation
   - Optional: `newspaper3k` for news article extraction
   - Optional: `trafilatura` for general web content extraction
   - Optional: `PyPDF2` for fallback PDF extraction

2. **API keys:**
   - `PERPLEXITY_API_KEY` environment variable for the Perplexity AI fallback
   - `OPENAI_API_KEY` environment variable for GPT-4o-mini content analysis

3. **Pre-trained ML model artifacts** (in `machine_learning_filter/` directory):
   - `random_forest_filter.pkl`
   - `source_type_encoder.pkl`
   - `text_vectorizer.pkl`

### Step-by-Step Replication

1. **Input:** A list of raw events, each with a `source_url` field and a `raw_event_id`. Events should be stored in a SQLite database with a `RawEvents` table containing columns: `raw_event_id`, `source_url`, `raw_title`, `raw_content`, `event_date`, `source_type`, `source_metadata`.

2. **For each event with a URL and no existing `raw_content`:**

   a. **Check if URL is a PDF** (ends with `.pdf`, contains `/pdf/`, or has `contenttype=application/pdf` or `filetype=pdf` in the URL).

   b. **If PDF:** Download to a temporary file, extract text with pdfplumber (primary) or PyPDF2 (fallback). Require at least 100 characters.

   c. **If not PDF, check if domain is in the stubborn-site list.** If so, try Perplexity AI fallback first (see Section 6.2 for exact prompts). Accept content only if > 200 characters.

   d. **Launch headless Chromium** via Playwright with the flags listed in Section 5.1.

   e. **Create a browser context** with a random user agent, random viewport, `en-AU` locale, `Australia/Sydney` timezone, and the HTTP headers listed in Section 5.2.

   f. **Inject stealth JavaScript** (Section 5.3) via `page.add_init_script()`.

   g. **Navigate to the URL** with `wait_until="domcontentloaded"`, 30-second timeout, and `referer="https://www.google.com/"`.

   h. **On HTTP 403:** Wait 8-15 seconds, create a new context with different fingerprint, retry with `wait_until="networkidle"` and `referer="https://www.google.com.au/"`. If retry fails, use Perplexity fallback.

   i. **Wait** 3-7 seconds (random), then **scroll** the page incrementally (300-500px steps, 0.2-0.6s delays).

   j. **Extract content** using the CSS selector cascade (Section 5.4). Accept first selector yielding > 200 characters. Fall back to `<body>` text.

   k. **Clean text:** Collapse whitespace, collapse newlines, strip non-printable characters.

   l. **If content > 50 characters:** Store in `RawEvents.raw_content` and log success.

   m. **If content <= 50 characters and Perplexity not yet tried:** Call Perplexity fallback. If fallback returns > 50 characters, store that content.

   n. **If all methods fail:** Log failure and skip the event.

3. **After storing content, apply the Random Forest ML filter:**
   - Concatenate title + description + content + preprocessed URL.
   - Vectorise with the pre-trained TF-IDF vectorizer.
   - Encode source type with the pre-trained label encoder.
   - Classify with the pre-trained Random Forest model.
   - If classified as non-cyber: discard the event.

4. **For surviving events, run the LLM fast pass:**
   - Truncate content to 12,000 characters.
   - Call GPT-4o-mini with the prompts in Section 9.2.
   - Extract: `is_australian_event`, `is_specific_event`, `primary_entity`, `affected_entities`, `summary`, `event_date`, `records_affected`.
   - Events where `is_australian_event = False` or `is_specific_event = False` are filtered out in subsequent pipeline stages.

5. **Output:** Each surviving event now has full article content in `raw_content`, ML filter metadata in `source_metadata`, and LLM-extracted structured metadata ready for the enrichment phase.
