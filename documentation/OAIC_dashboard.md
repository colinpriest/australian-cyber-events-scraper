# OAIC Notifiable Data Breach Dashboard - Complete Documentation & Software Specifications

## A) Dashboard Structure Documentation

## Overview

The OAIC Notifiable Data Breach (NDB) statistics dashboard is a Microsoft Power BI embedded dashboard with 11 pages, accessible at: [https://www.oaic.gov.au/privacy/notifiable-data-breaches/notifiable-data-breach-statistics-dashboard](https://www.oaic.gov.au/privacy/notifiable-data-breaches/notifiable-data-breach-statistics-dashboard)

The dashboard displays data for Jan-Jun 2025 with historical comparisons, last refreshed 31-Aug-25.[oaic.gov](https://www.oaic.gov.au/privacy/notifiable-data-breaches/notifiable-data-breach-statistics-dashboard)

## Page-by-Page Documentation

## **Page 1: Home (Navigation Page)**

* **Content** : Title "Notifiable Data Breach (NDB) statistics dashboard Jan-Jun 2025" with 8 navigation buttons arranged in a 2x4 grid
* **Navigation Buttons** :

1. Snapshot
2. Notification received during the reporting period
3. Number of individuals affected by breaches
4. Kinds of personal information involved in breaches
5. How likely to identify breaches
6. How likely to notify the OAIC of a breach
7. Sources of breaches
8. Most sectors by count of breaches

* **Interactive Elements** :
* 8 clickable navigation buttons (all redirect to specific pages)
* Information icon (Bookmark)
* Home navigation icon
* Data export and disclaimer links
* **Controls** : Semester dropdown (top left) with options: Jan-Jun 2025, Jul-Dec 2024, Jan-Jun 2024, Jul-Dec 2023, Jan-Jun 2023, Jan-Jun 2022

## **Page 2: Snapshot**

* **Content** : Executive summary page with multiple visualizations
* **Main Visualizations** :

1. **Notifications received** : KPI card showing 532 (▼ -10% compared to Jul-Dec 2024)
2. **Notifications received by month** : Bar chart (Jan-Jun 2025) showing monthly breakdown
3. **Sources of data breaches** : Donut chart showing:
   * Malicious or criminal attack: 308 (59.46%)
   * Human error: 193 (37.26%)
   * System fault: 17 (3.28%)
4. **Data breaches affected** : Text stating "67% of data breaches affected 100 people or fewer"
5. **Cyber incident breakdown** : Horizontal bar chart showing:
   * Phishing (compromised credentials): 28%
   * Compromised or stolen credentials (method unknown): 21%
   * Ransomware: 21%
   * Hacking: 17%
   * Brute-force attack: 6%
   * Malware: 4%
6. **Top causes of human breach** : Icons with percentages:
   * PI sent to wrong recipient (email): 44%
   * Unauthorised disclosure: 22%
   * Failure to use BCC: 9%
7. **Top 5 sectors to notify breaches** : Table showing:
   * Australian Government
   * Education
   * Finance (incl. superannuation)
   * Health service providers
   * Legal, accounting & management services

* **Interactive Elements** :
* Semester dropdown filter
* Clickable month bars (Jan-Jun)
* Clickable donut chart segments (3 segments)
* Clickable cyber incident bars (6 bars)
* Sector row checkboxes (5 rows, each can be selected for filtering)
* **Controls** : Semester selection dropdown (Jan-Jun 2025 default)

## **Page 3: Notifications received during the reporting period**

* **Content** : Time series analysis of notifications
* **Main Visualizations** :

1. **Table header** : Shows "Notifications received - Human and breach semester" with columns for reporting periods
2. **Line chart** : Showing notifications by month (1/6/2025 - 30/6/2025)
3. **Stacked bar chart** : Monthly data showing breakdown by:
   * Compromise defense
   * Malicious attack
   * Release via external attack
   * System fault

* **Interactive Elements** :
* Semester dropdown
* Clickable month points on line chart
* Clickable stacked bar segments
* **Controls** : Semester filter

## **Page 4: Number of individuals affected by breaches**

* **Content** : Distribution of breach impact by individual count
* **Main Visualizations** :

1. **Horizontal bar chart** : "Number of individuals evidently affected by breaches" showing ranges:
   * 1: 311
   * 2-10: 108
   * 11-100: 83
   * 101-1,000: 28
   * 1,001-5,000: 10
   * 5,001-10,000: 5
   * 10,001-50,000: 5
   * 50,001-100,000: 1
   * 100,001-250,000: 1
   * 250,001-500,000: 1
   * 1,000,001-5,000,000: 1
   * Unknown: 70
2. **Summary table** : Shows number of individuals affected by date range and breach count

* **Interactive Elements** :
* Semester dropdown
* Clickable bar chart segments (12 segments)
* **Controls** : Semester filter

## **Page 5: Kinds of personal information involved in breaches**

* **Content** : Types of personal information compromised
* **Main Visualizations** :

1. **Horizontal bar chart** : "Kinds of personal information involved in breaches"
   * Contact information: 485
   * Identity information: 352
   * Financial details: 304
   * Health information: 169
   * Tax file Numbers: 116
   * Other sensitive information: 104
2. **Data breaches involving Digital ID and/or CDR data** :
   * Consumer Data Right data: 9
   * Digital ID information documents: 9

* **Interactive Elements** :
* Semester dropdown
* Clickable bar segments (6 segments)
* **Controls** : Semester filter

## **Page 6: Source of breaches**

* **Content** : Detailed breakdown of breach sources with filtering
* **Main Visualizations** :

1. **Stacked bar chart** : "Source of data breaches - all" comparing Jul-Dec 2024 vs Jan-Jun 2025:
   * Human error: 171 vs 193
   * Malicious or criminal attack: 410 vs 308
   * System fault: 12 vs 17

* **Interactive Elements** :
* Semester dropdown
* Radio button filters (5 options):
  * All breaches (default)
  * Malicious or criminal attacks
  * Cyber incidents
  * Human error
  * System faults
* Clickable bar segments (6 segments - 3 categories x 2 time periods)
* "Visual view" button (default)
* "Table view" button
* **Controls** : Semester filter + Radio button selection

## **Page 7: Time taken to identify breaches**

* **Content** : Analysis of breach detection time
* **Main Visualizations** :

1. **Grouped bar chart** : "Time taken to identify breaches" comparing Jul-Dec 2024 vs Jan-Jun 2025 across time ranges:
   * Unknown
   * < 1H hours
   * 1D-2H days
   * 2D-2H days
   * >
   > 30 days
   >

* **Interactive Elements** :
* Semester dropdown
* Three button filters at top:
  * "By data contained" (default)
  * "On breach type"
  * "By source of breach"
* Clickable bar segments (10 segments - 5 time ranges x 2 periods)
* **Controls** : Semester filter + Button selection

## **Page 8: Time taken to notify the OAIC of breaches**

* **Content** : Analysis of notification timeline
* **Main Visualizations** :

1. **Grouped bar chart** : "Time taken to notify the OAIC of breaches" comparing periods across:
   * Unknown
   * < 1H days
   * 1H-3H days
   * 3H-30 days
   * >
   > 30 days
   >

* **Interactive Elements** :
* Semester dropdown
* Three button filters at top:
  * "By data type" (default)
  * "On breach type"
  * "By source of breach"
* Clickable bar segments (10 segments)
* **Controls** : Semester filter + Button selection

## **Page 9: Top 5 sectors by source of breaches**

* **Content** : Sector analysis with breach source breakdown
* **Main Visualizations** :

1. **Stacked/grouped visualization** : Showing top 5 sectors with breach sources
2. **Legend** : Shows 5 sectors:
   * Australian Government
   * Education
   * Finance (incl. superannuation)
   * Health service providers
   * Legal, accounting & management services

* **Interactive Elements** :
* Semester dropdown
* Five button filters at top:
  * "All"
  * "Cyber incidents"
  * "Malicious or criminal attacks"
  * "Human Error"
  * "System fault"
* Clickable sector segments
* **Controls** : Semester filter + Button selection

## **Page 10: Data notes and disclaimer**

* **Content** : Text-based informational page
* **Main Content** :
* About this dashboard
* Data source information
* Statistical comparisons notes
* Methodological notes
* Feedback information
* **Interactive Elements** : None (static text page)
* **Controls** : Semester dropdown (visible but non-functional on this page)

## **Page 11: Glossary**

* **Content** : Definitions table
* **Main Content** : Table with two columns defining terms used throughout the dashboard including:
* Consumer Data Right data
* Compromised credentials
* Digital ID information
* Human error
* Health information
* Other sensitive information
* Notifications
* Serious data breach
* Tax File Number
* Ransomware
* And others
* **Interactive Elements** : Scrollable table
* **Controls** : Semester dropdown

## Global Interactive Elements (Available on all pages)

1. **Navigation Controls** :

* "Go back" button (bottom left - disabled on page 1)
* Page dropdown selector (shows current page name)
* "Previous" button
* "Next" button
* "Exit full screen" button
* Page indicator (e.g., "2 of 11")
* Previous Page / Next Page buttons

1. **View Controls** :

* "Zoom Out" button (-)
* "Zoom In" button (+)
* Zoom level indicator (e.g., "59%")
* "Fit to page" button

1. **Information/Help** :

* Bookmark icon (information about the page)
* Home navigation icon
* Follow link icon
* Data refreshed date indicator

1. **Sharing Options** :

* Share via Facebook
* Share via Twitter
* Share via LinkedIn
* General Share button
* "Close full-screen mode" button

1. **Data Export** :

* "Data export and disclaimer" link (appears on most visualization pages)
* "View table" / "Table view" / "Data view" buttons on chart pages

## B) Software Specifications for Python Script + LLM Data Extraction

## System Architecture

<pre class="not-prose w-full rounded font-mono text-sm font-extralight"><div class="codeWrapper text-light selection:text-super selection:bg-super/10 my-md relative flex flex-col rounded-lg font-mono text-sm font-normal bg-subtler"><div class="translate-y-xs -translate-x-xs bottom-xl mb-xl flex h-0 items-start justify-end sm:sticky sm:top-xs"><div class="overflow-hidden rounded-full border-subtlest ring-subtlest divide-subtlest bg-base"><div class="border-subtlest ring-subtlest divide-subtlest bg-subtler"><button data-testid="copy-code-button" aria-label="Copy code" type="button" class="focus-visible:bg-subtle hover:bg-subtle text-quiet  hover:text-foreground dark:hover:bg-subtle font-sans focus:outline-none outline-none outline-transparent transition duration-300 ease-out select-none items-center relative group/button font-semimedium justify-center text-center items-center rounded-full cursor-pointer active:scale-[0.97] active:duration-150 active:ease-outExpo origin-center whitespace-nowrap inline-flex text-sm h-8 aspect-square" data-state="closed"><div class="flex items-center min-w-0 gap-two justify-center"><div class="flex shrink-0 items-center justify-center size-4"><svg role="img" class="inline-flex fill-current" width="16" height="16"><use xlink:href="#pplx-icon-copy"></use></svg></div></div></button></div></div></div><div class="-mt-xl"><div><div data-testid="code-language-indicator" class="text-quiet bg-subtle py-xs px-sm inline-block rounded-br rounded-tl-lg text-xs font-thin">text</div></div><div><span><code><span><span>┌─────────────────────────────────────────────────────────────┐
</span></span><span>│                    Main Orchestrator Script                  │
</span><span>│                   (dashboard_scraper.py)                     │
</span><span>└──────────┬──────────────────────────────────┬────────────────┘
</span><span>           │                                  │
</span><span>    ┌──────▼──────┐                    ┌─────▼──────────┐
</span><span>    │  Browser    │                    │   LLM Client   │
</span><span>    │  Automation │                    │   (ChatGPT)    │
</span><span>    │  Module     │                    │   4o-mini      │
</span><span>    └──────┬──────┘                    └─────┬──────────┘
</span><span>           │                                  │
</span><span>    ┌──────▼──────────────────────────────────▼──────────┐
</span><span>    │           Data Storage & Management                 │
</span><span>    │         (SQLite/PostgreSQL + File System)           │
</span><span>    └─────────────────────────────────────────────────────┘
</span><span></span></code></span></div></div></div></pre>

## Technical Stack Requirements

**Core Dependencies:**

<pre class="not-prose w-full rounded font-mono text-sm font-extralight"><div class="codeWrapper text-light selection:text-super selection:bg-super/10 my-md relative flex flex-col rounded-lg font-mono text-sm font-normal bg-subtler"><div class="translate-y-xs -translate-x-xs bottom-xl mb-xl flex h-0 items-start justify-end sm:sticky sm:top-xs"><div class="overflow-hidden rounded-full border-subtlest ring-subtlest divide-subtlest bg-base"><div class="border-subtlest ring-subtlest divide-subtlest bg-subtler"><button data-testid="copy-code-button" aria-label="Copy code" type="button" class="focus-visible:bg-subtle hover:bg-subtle text-quiet  hover:text-foreground dark:hover:bg-subtle font-sans focus:outline-none outline-none outline-transparent transition duration-300 ease-out select-none items-center relative group/button font-semimedium justify-center text-center items-center rounded-full cursor-pointer active:scale-[0.97] active:duration-150 active:ease-outExpo origin-center whitespace-nowrap inline-flex text-sm h-8 aspect-square" data-state="closed"><div class="flex items-center min-w-0 gap-two justify-center"><div class="flex shrink-0 items-center justify-center size-4"><svg role="img" class="inline-flex fill-current" width="16" height="16"><use xlink:href="#pplx-icon-copy"></use></svg></div></div></button></div></div></div><div class="-mt-xl"><div><div data-testid="code-language-indicator" class="text-quiet bg-subtle py-xs px-sm inline-block rounded-br rounded-tl-lg text-xs font-thin">python</div></div><div><span><code><span><span class="token token"># Browser automation</span><span>
</span></span><span><span>selenium</span><span class="token token operator">==</span><span class="token token">4.15</span><span class="token token">.0</span><span></span><span class="token token"># or playwright==1.40.0 (recommended for better Power BI support)</span><span>
</span></span><span><span>webdriver</span><span class="token token operator">-</span><span>manager</span><span class="token token operator">==</span><span class="token token">4.0</span><span class="token token">.1</span><span>
</span></span><span>
</span><span><span></span><span class="token token"># Image handling</span><span>
</span></span><span><span>Pillow</span><span class="token token operator">==</span><span class="token token">10.1</span><span class="token token">.0</span><span>
</span></span><span><span>opencv</span><span class="token token operator">-</span><span>python</span><span class="token token operator">==</span><span class="token token">4.8</span><span class="token token">.1</span><span></span><span class="token token"># For advanced image preprocessing if needed</span><span>
</span></span><span>
</span><span><span></span><span class="token token"># LLM integration</span><span>
</span></span><span><span>openai</span><span class="token token operator">==</span><span class="token token">1.3</span><span class="token token">.0</span><span></span><span class="token token"># For ChatGPT API</span><span>
</span></span><span><span>tiktoken</span><span class="token token operator">==</span><span class="token token">0.5</span><span class="token token">.1</span><span></span><span class="token token"># For token counting</span><span>
</span></span><span>
</span><span><span></span><span class="token token"># Data handling</span><span>
</span></span><span><span>pandas</span><span class="token token operator">==</span><span class="token token">2.1</span><span class="token token">.3</span><span>
</span></span><span><span>openpyxl</span><span class="token token operator">==</span><span class="token token">3.1</span><span class="token token">.2</span><span></span><span class="token token"># For Excel export</span><span>
</span></span><span><span>sqlalchemy</span><span class="token token operator">==</span><span class="token token">2.0</span><span class="token token">.23</span><span></span><span class="token token"># For database operations</span><span>
</span></span><span>
</span><span><span></span><span class="token token"># Utilities</span><span>
</span></span><span><span>python</span><span class="token token operator">-</span><span>dotenv</span><span class="token token operator">==</span><span class="token token">1.0</span><span class="token token">.0</span><span></span><span class="token token"># For environment variables</span><span>
</span></span><span><span>pydantic</span><span class="token token operator">==</span><span class="token token">2.5</span><span class="token token">.0</span><span></span><span class="token token"># For data validation</span><span>
</span></span><span><span>tenacity</span><span class="token token operator">==</span><span class="token token">8.2</span><span class="token token">.3</span><span></span><span class="token token"># For retry logic</span><span>
</span></span><span><span>tqdm</span><span class="token token operator">==</span><span class="token token">4.66</span><span class="token token">.1</span><span></span><span class="token token"># For progress bars</span><span>
</span></span><span></span></code></span></div></div></div></pre>

## Module 1: Browser Automation Module

**File: `browser_controller.py`**

<pre class="not-prose w-full rounded font-mono text-sm font-extralight"><div class="codeWrapper text-light selection:text-super selection:bg-super/10 my-md relative flex flex-col rounded-lg font-mono text-sm font-normal bg-subtler"><div class="translate-y-xs -translate-x-xs bottom-xl mb-xl flex h-0 items-start justify-end sm:sticky sm:top-xs"><div class="overflow-hidden rounded-full border-subtlest ring-subtlest divide-subtlest bg-base"><div class="border-subtlest ring-subtlest divide-subtlest bg-subtler"><button data-testid="copy-code-button" aria-label="Copy code" type="button" class="focus-visible:bg-subtle hover:bg-subtle text-quiet  hover:text-foreground dark:hover:bg-subtle font-sans focus:outline-none outline-none outline-transparent transition duration-300 ease-out select-none items-center relative group/button font-semimedium justify-center text-center items-center rounded-full cursor-pointer active:scale-[0.97] active:duration-150 active:ease-outExpo origin-center whitespace-nowrap inline-flex text-sm h-8 aspect-square" data-state="closed"><div class="flex items-center min-w-0 gap-two justify-center"><div class="flex shrink-0 items-center justify-center size-4"><svg role="img" class="inline-flex fill-current" width="16" height="16"><use xlink:href="#pplx-icon-copy"></use></svg></div></div></button></div></div></div><div class="-mt-xl"><div><div data-testid="code-language-indicator" class="text-quiet bg-subtle py-xs px-sm inline-block rounded-br rounded-tl-lg text-xs font-thin">python</div></div><div><span><code><span><span class="token token triple-quoted-string">"""
</span></span><span class="token token triple-quoted-string">Browser automation module for navigating and capturing Power BI dashboard
</span><span><span class="token token triple-quoted-string">"""</span><span>
</span></span><span>
</span><span><span></span><span class="token token">from</span><span> playwright</span><span class="token token punctuation">.</span><span>sync_api </span><span class="token token">import</span><span> sync_playwright</span><span class="token token punctuation">,</span><span> Page
</span></span><span><span></span><span class="token token">from</span><span> typing </span><span class="token token">import</span><span> List</span><span class="token token punctuation">,</span><span> Dict</span><span class="token token punctuation">,</span><span> Optional</span><span class="token token punctuation">,</span><span> Tuple
</span></span><span><span></span><span class="token token">import</span><span> time
</span></span><span><span></span><span class="token token">import</span><span> logging
</span></span><span><span></span><span class="token token">from</span><span> pathlib </span><span class="token token">import</span><span> Path
</span></span><span><span></span><span class="token token">from</span><span> dataclasses </span><span class="token token">import</span><span> dataclass
</span></span><span>
</span><span><span></span><span class="token token decorator annotation punctuation">@dataclass</span><span>
</span></span><span><span></span><span class="token token">class</span><span></span><span class="token token">DashboardPage</span><span class="token token punctuation">:</span><span>
</span></span><span><span></span><span class="token token triple-quoted-string">"""Represents a single dashboard page"""</span><span>
</span></span><span><span>    page_number</span><span class="token token punctuation">:</span><span></span><span class="token token">int</span><span>
</span></span><span><span>    page_name</span><span class="token token punctuation">:</span><span></span><span class="token token">str</span><span>
</span></span><span><span>    has_semester_filter</span><span class="token token punctuation">:</span><span></span><span class="token token">bool</span><span>
</span></span><span><span>    filter_options</span><span class="token token punctuation">:</span><span> List</span><span class="token token punctuation">[</span><span class="token token">str</span><span class="token token punctuation">]</span><span>
</span></span><span><span>    interactive_elements</span><span class="token token punctuation">:</span><span> List</span><span class="token token punctuation">[</span><span class="token token">str</span><span class="token token punctuation">]</span><span>
</span></span><span>
</span><span><span></span><span class="token token">class</span><span></span><span class="token token">PowerBIDashboardController</span><span class="token token punctuation">:</span><span>
</span></span><span><span></span><span class="token token triple-quoted-string">"""
</span></span><span class="token token triple-quoted-string">    Controls browser interaction with OAIC Power BI dashboard
</span><span><span class="token token triple-quoted-string">    """</span><span>
</span></span><span>  
</span><span><span>    BASE_URL </span><span class="token token operator">=</span><span></span><span class="token token">"https://www.oaic.gov.au/privacy/notifiable-data-breaches/notifiable-data-breach-statistics-dashboard"</span><span>
</span></span><span>  
</span><span><span></span><span class="token token"># Dashboard configuration</span><span>
</span></span><span><span>    TOTAL_PAGES </span><span class="token token operator">=</span><span></span><span class="token token">11</span><span>
</span></span><span><span>    PAGE_DEFINITIONS </span><span class="token token operator">=</span><span></span><span class="token token punctuation">{</span><span>
</span></span><span><span></span><span class="token token">1</span><span class="token token punctuation">:</span><span> DashboardPage</span><span class="token token punctuation">(</span><span class="token token">1</span><span class="token token punctuation">,</span><span></span><span class="token token">"Home"</span><span class="token token punctuation">,</span><span></span><span class="token token boolean">True</span><span class="token token punctuation">,</span><span></span><span class="token token punctuation">[</span><span class="token token punctuation">]</span><span class="token token punctuation">,</span><span></span><span class="token token punctuation">[</span><span class="token token">"navigation_buttons"</span><span class="token token punctuation">]</span><span class="token token punctuation">)</span><span class="token token punctuation">,</span><span>
</span></span><span><span></span><span class="token token">2</span><span class="token token punctuation">:</span><span> DashboardPage</span><span class="token token punctuation">(</span><span class="token token">2</span><span class="token token punctuation">,</span><span></span><span class="token token">"Snapshot"</span><span class="token token punctuation">,</span><span></span><span class="token token boolean">True</span><span class="token token punctuation">,</span><span></span><span class="token token punctuation">[</span><span class="token token">"semester"</span><span class="token token punctuation">]</span><span class="token token punctuation">,</span><span></span><span class="token token punctuation">[</span><span class="token token">"donut_chart"</span><span class="token token punctuation">,</span><span></span><span class="token token">"bar_chart"</span><span class="token token punctuation">,</span><span></span><span class="token token">"sector_checkboxes"</span><span class="token token punctuation">]</span><span class="token token punctuation">)</span><span class="token token punctuation">,</span><span>
</span></span><span><span></span><span class="token token">3</span><span class="token token punctuation">:</span><span> DashboardPage</span><span class="token token punctuation">(</span><span class="token token">3</span><span class="token token punctuation">,</span><span></span><span class="token token">"Notifications_received"</span><span class="token token punctuation">,</span><span></span><span class="token token boolean">True</span><span class="token token punctuation">,</span><span></span><span class="token token punctuation">[</span><span class="token token">"semester"</span><span class="token token punctuation">]</span><span class="token token punctuation">,</span><span></span><span class="token token punctuation">[</span><span class="token token">"line_chart"</span><span class="token token punctuation">,</span><span></span><span class="token token">"stacked_bars"</span><span class="token token punctuation">]</span><span class="token token punctuation">)</span><span class="token token punctuation">,</span><span>
</span></span><span><span></span><span class="token token">4</span><span class="token token punctuation">:</span><span> DashboardPage</span><span class="token token punctuation">(</span><span class="token token">4</span><span class="token token punctuation">,</span><span></span><span class="token token">"Individuals_affected"</span><span class="token token punctuation">,</span><span></span><span class="token token boolean">True</span><span class="token token punctuation">,</span><span></span><span class="token token punctuation">[</span><span class="token token">"semester"</span><span class="token token punctuation">]</span><span class="token token punctuation">,</span><span></span><span class="token token punctuation">[</span><span class="token token">"horizontal_bars"</span><span class="token token punctuation">]</span><span class="token token punctuation">)</span><span class="token token punctuation">,</span><span>
</span></span><span><span></span><span class="token token">5</span><span class="token token punctuation">:</span><span> DashboardPage</span><span class="token token punctuation">(</span><span class="token token">5</span><span class="token token punctuation">,</span><span></span><span class="token token">"Personal_information_types"</span><span class="token token punctuation">,</span><span></span><span class="token token boolean">True</span><span class="token token punctuation">,</span><span></span><span class="token token punctuation">[</span><span class="token token">"semester"</span><span class="token token punctuation">]</span><span class="token token punctuation">,</span><span></span><span class="token token punctuation">[</span><span class="token token">"horizontal_bars"</span><span class="token token punctuation">]</span><span class="token token punctuation">)</span><span class="token token punctuation">,</span><span>
</span></span><span><span></span><span class="token token">6</span><span class="token token punctuation">:</span><span> DashboardPage</span><span class="token token punctuation">(</span><span class="token token">6</span><span class="token token punctuation">,</span><span></span><span class="token token">"Source_of_breaches"</span><span class="token token punctuation">,</span><span></span><span class="token token boolean">True</span><span class="token token punctuation">,</span><span></span><span class="token token punctuation">[</span><span class="token token">"semester"</span><span class="token token punctuation">,</span><span></span><span class="token token">"breach_type_radio"</span><span class="token token punctuation">]</span><span class="token token punctuation">,</span><span></span><span class="token token punctuation">[</span><span class="token token">"stacked_bars"</span><span class="token token punctuation">,</span><span></span><span class="token token">"view_toggle"</span><span class="token token punctuation">]</span><span class="token token punctuation">)</span><span class="token token punctuation">,</span><span>
</span></span><span><span></span><span class="token token">7</span><span class="token token punctuation">:</span><span> DashboardPage</span><span class="token token punctuation">(</span><span class="token token">7</span><span class="token token punctuation">,</span><span></span><span class="token token">"Time_to_identify"</span><span class="token token punctuation">,</span><span></span><span class="token token boolean">True</span><span class="token token punctuation">,</span><span></span><span class="token token punctuation">[</span><span class="token token">"semester"</span><span class="token token punctuation">,</span><span></span><span class="token token">"filter_buttons"</span><span class="token token punctuation">]</span><span class="token token punctuation">,</span><span></span><span class="token token punctuation">[</span><span class="token token">"grouped_bars"</span><span class="token token punctuation">]</span><span class="token token punctuation">)</span><span class="token token punctuation">,</span><span>
</span></span><span><span></span><span class="token token">8</span><span class="token token punctuation">:</span><span> DashboardPage</span><span class="token token punctuation">(</span><span class="token token">8</span><span class="token token punctuation">,</span><span></span><span class="token token">"Time_to_notify"</span><span class="token token punctuation">,</span><span></span><span class="token token boolean">True</span><span class="token token punctuation">,</span><span></span><span class="token token punctuation">[</span><span class="token token">"semester"</span><span class="token token punctuation">,</span><span></span><span class="token token">"filter_buttons"</span><span class="token token punctuation">]</span><span class="token token punctuation">,</span><span></span><span class="token token punctuation">[</span><span class="token token">"grouped_bars"</span><span class="token token punctuation">]</span><span class="token token punctuation">)</span><span class="token token punctuation">,</span><span>
</span></span><span><span></span><span class="token token">9</span><span class="token token punctuation">:</span><span> DashboardPage</span><span class="token token punctuation">(</span><span class="token token">9</span><span class="token token punctuation">,</span><span></span><span class="token token">"Top_sectors"</span><span class="token token punctuation">,</span><span></span><span class="token token boolean">True</span><span class="token token punctuation">,</span><span></span><span class="token token punctuation">[</span><span class="token token">"semester"</span><span class="token token punctuation">,</span><span></span><span class="token token">"source_buttons"</span><span class="token token punctuation">]</span><span class="token token punctuation">,</span><span></span><span class="token token punctuation">[</span><span class="token token">"sector_chart"</span><span class="token token punctuation">]</span><span class="token token punctuation">)</span><span class="token token punctuation">,</span><span>
</span></span><span><span></span><span class="token token">10</span><span class="token token punctuation">:</span><span> DashboardPage</span><span class="token token punctuation">(</span><span class="token token">10</span><span class="token token punctuation">,</span><span></span><span class="token token">"Data_notes"</span><span class="token token punctuation">,</span><span></span><span class="token token boolean">False</span><span class="token token punctuation">,</span><span></span><span class="token token punctuation">[</span><span class="token token punctuation">]</span><span class="token token punctuation">,</span><span></span><span class="token token punctuation">[</span><span class="token token punctuation">]</span><span class="token token punctuation">)</span><span class="token token punctuation">,</span><span>
</span></span><span><span></span><span class="token token">11</span><span class="token token punctuation">:</span><span> DashboardPage</span><span class="token token punctuation">(</span><span class="token token">11</span><span class="token token punctuation">,</span><span></span><span class="token token">"Glossary"</span><span class="token token punctuation">,</span><span></span><span class="token token boolean">False</span><span class="token token punctuation">,</span><span></span><span class="token token punctuation">[</span><span class="token token punctuation">]</span><span class="token token punctuation">,</span><span></span><span class="token token punctuation">[</span><span class="token token">"scrollable_table"</span><span class="token token punctuation">]</span><span class="token token punctuation">)</span><span class="token token punctuation">,</span><span>
</span></span><span><span></span><span class="token token punctuation">}</span><span>
</span></span><span>  
</span><span><span></span><span class="token token"># Selectors (CSS/XPath) for Power BI elements</span><span>
</span></span><span><span>    SELECTORS </span><span class="token token operator">=</span><span></span><span class="token token punctuation">{</span><span>
</span></span><span><span></span><span class="token token">"fullscreen_button"</span><span class="token token punctuation">:</span><span></span><span class="token token">"button[title='Enter full screen mode'], button[aria-label*='full screen']"</span><span class="token token punctuation">,</span><span>
</span></span><span><span></span><span class="token token">"zoom_in"</span><span class="token token punctuation">:</span><span></span><span class="token token">"button#zoomInButton"</span><span class="token token punctuation">,</span><span>
</span></span><span><span></span><span class="token token">"zoom_out"</span><span class="token token punctuation">:</span><span></span><span class="token token">"button#zoomOutButton"</span><span class="token token punctuation">,</span><span>
</span></span><span><span></span><span class="token token">"zoom_level"</span><span class="token token punctuation">:</span><span></span><span class="token token">"button#zoomValueButton"</span><span class="token token punctuation">,</span><span>
</span></span><span><span></span><span class="token token">"next_page"</span><span class="token token punctuation">:</span><span> "
</span></span><span></span></code></span></div></div></div></pre>

1. [https://www.oaic.gov.au/privacy/notifiable-data-breaches/notifiable-data-breach-statistics-dashboard](https://www.oaic.gov.au/privacy/notifiable-data-breaches/notifiable-data-breach-statistics-dashboard)
