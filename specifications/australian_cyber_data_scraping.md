# Australian Cyber Data Scraping Specification

This document provides a comprehensive specification for replicating the data scraping, API data sourcing, and data preparation steps used in the Australian Data Breaches project.

## Project Overview

The project scrapes Australian data breach information from the Webber Insurance data breaches list, extracts structured information, and exports the data to Excel format. The implementation uses both Selenium-based dynamic scraping and HTTP request-based static scraping approaches.

## Data Source

**Primary Target URL:** `https://www.webberinsurance.com.au/data-breaches-list`

**Website Structure:**
- Static webpage containing annual data breach compilations
- Organized by year with H2 headers (e.g., "2024", "2023", etc.)
- Each year contains breach entries in `wpb_text_column` divs
- Entries follow format: `Entity Name – Month Year`
- External links to news articles and official breach notifications

## Technical Requirements

### Dependencies
```
requests>=2.31.0
beautifulsoup4>=4.12.0
pandas>=2.0.0
openpyxl>=3.1.0
selenium>=4.0.0
chromedriver-autoinstaller (for Selenium approach)
```

### Python Libraries Used
- `requests`: HTTP client for web requests
- `beautifulsoup4`: HTML parsing and content extraction
- `pandas`: Data manipulation and DataFrame operations
- `openpyxl`: Excel file writing
- `selenium`: Dynamic web scraping with browser automation
- `re`: Regular expression pattern matching
- `time`: Rate limiting and delays
- `random`: Randomized delays for human-like behavior
- `urllib.parse`: URL parsing and joining
- `unicodedata`: Text normalization and cleaning

## Implementation Approaches

### Approach 1: HTTP Requests + BeautifulSoup (Recommended)

**File:** `working_scraper.py`

#### Core Components

1. **HTTP Client Setup**
```python
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept-Encoding': 'identity'  # Avoid compression issues
}
```

2. **Main Page Parsing**
- Fetch the main data breaches list page
- Extract text content using `soup.get_text(separator='\n', strip=True)`
- Parse lines to identify breach entries
- Create mapping of H3 tags to DOM elements for URL extraction

3. **Breach Entry Detection**
- Pattern matching for lines ending with "Month Year"
- Validation of month names against predefined list
- Extraction of organization names using delimiter patterns (`–` or ` - `)

4. **URL Extraction**
- Find corresponding H3 elements for each breach entry
- Locate sibling `<ul>` tags containing external links
- Filter out self-referential webberinsurance.com.au links

5. **Content Scraping from External URLs**
- Rate-limited requests to external breach notification sources
- Multiple CSS selector attempts for content extraction
- Fallback to body text extraction
- Text cleaning and length limiting

#### Rate Limiting Strategy
```python
# Random delays between requests
time.sleep(random.uniform(1, 3))

# Additional delays between URL scraping
time.sleep(random.uniform(0.5, 1.5))
```

### Approach 2: Selenium Dynamic Scraping

**File:** `Australian-data-breaches-scraper.py`

#### Browser Automation Setup

1. **Chrome Options Configuration**
```python
chrome_options = Options()
chrome_options.add_argument("--headless")  # Optional headless mode
chrome_options.add_argument("--no-sandbox")
chrome_options.add_argument("--disable-dev-shm-usage")
chrome_options.add_argument("--disable-blink-features=AutomationControlled")
```

2. **Anti-Detection Measures**
- Custom user agent strings
- Stealth mode configurations
- Randomized scroll behavior
- Human-like interaction delays

3. **Dynamic Content Handling**
- Wait for page elements to load
- Scroll simulation for triggering dynamic content
- JavaScript execution for content extraction

#### Content Extraction Pipeline

1. **Page Navigation**
```python
def visit_page(self, url, wait_for_element=None, timeout=20):
    self.driver.get(url)
    WebDriverWait(self.driver, timeout).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )
```

2. **Text Content Processing**
- Extract both page source and rendered text
- Parse content using multiple CSS selectors
- Apply text cleaning and normalization

3. **Link Discovery and Processing**
- Find all anchor tags on the page
- Filter relevant links based on entity name matching
- Scrape detailed content from relevant URLs

## Data Processing Pipeline

### 1. Text Cleaning and Normalization

**Function:** `clean_text_for_excel()` (Selenium) / `clean_text()` (HTTP)

#### Unicode Normalization
```python
text = unicodedata.normalize('NFKD', text)
```

#### Character Replacements
```python
replacements = {
    '\ufffd': '',  # Unicode replacement character
    '"': '"', '"': '"',  # Smart quotes
    ''': "'", ''': "'",  # Smart apostrophes
    '–': '-', '—': '-',  # Em/en dashes
    '…': '...',  # Ellipsis
}
```

#### Content Filtering
- Remove control characters
- Filter characters outside Basic Multilingual Plane
- Remove unwanted patterns (Cookie Policy, Privacy Policy, etc.)

### 2. Entity Name Extraction

**Pattern Matching:**
```python
# Primary pattern: "Entity Name – Month Year"
date_match = re.search(r'(.+?)\s*–\s*(\w+)\s+(\d{4})', text)

# Alternative patterns for different formats
patterns = [
    r'(.+?)\s+(confirms|discloses|exposes|says|reports)\s+(.+)',
    r'(.+?)\s+(data breach|cyber breach|security incident)',
    r'(.+?)\s*[-–]\s*(.+)',
]
```

### 3. Date Extraction and Validation

**Month Validation:**
```python
months = ['january', 'february', 'march', 'april', 'may', 'june',
         'july', 'august', 'september', 'october', 'november', 'december',
         'jan', 'feb', 'mar', 'apr', 'may', 'jun',
         'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
```

### 4. URL Processing and Content Scraping

#### Content Selector Priority
```python
selectors = [
    'div[class*="article"]',
    'div.entry-content',
    'div.post-content',
    'div.article-content',
    'article',
    'main',
    '.article-body',
    '.post-body'
]
```

## Output Data Structure

### DataFrame Schema
```python
columns = [
    "Name of the event",     # Entity/organization name
    "Month",                 # Breach occurrence month
    "Year",                  # Breach occurrence year
    "Entity affected",       # Same as "Name of the event"
    "Text description",      # Breach description text
    "URLs",                  # List of source URLs
    "Scraped Event Story"    # Full text from external sources
]
```

### Export Formats

#### Primary: Excel (.xlsx)
```python
df.to_excel(output_filename, index=False)
```

#### Fallback: CSV (.csv)
```python
df.to_csv(csv_filename, index=False, encoding='utf-8')
```

## Error Handling and Robustness

### HTTP Request Error Handling

1. **Connection Errors**
- Retry logic with exponential backoff
- Multiple user agent rotation
- Session persistence for cookie handling

2. **Rate Limiting (HTTP 429)**
```python
if response.status_code == 429:
    backoff_time = (2 ** attempt) * 10 + random.uniform(0, 5)
    time.sleep(backoff_time)
```

3. **Content Validation**
- Check for blocking indicators (CAPTCHA, access denied)
- Validate HTML content type
- Minimum content length validation

### Selenium Error Handling

1. **Driver Initialization**
- Multiple driver creation methods
- Automatic chromedriver installation
- Process cleanup and conflict resolution

2. **Element Loading**
- Explicit waits for element presence
- Timeout handling for slow-loading pages
- Graceful degradation for missing elements

## Performance Optimization

### Rate Limiting Best Practices
```python
# Base delay between requests
time.sleep(random.uniform(1, 3))

# Longer delays every 10 requests
if request_count % 10 == 0:
    time.sleep(random.uniform(5, 10))
```

### Content Processing Optimization
- Limit scraped content to 3000 characters
- Skip duplicate URLs using set tracking
- Early termination for failed requests

## Security and Ethics

### Respectful Scraping Practices
1. **Rate Limiting:** Implement delays between requests
2. **User-Agent:** Use realistic browser user agents
3. **Robots.txt:** Respect website crawling policies
4. **Error Handling:** Graceful failure without retry storms

### Data Privacy
- No personal information extraction
- Focus on publicly disclosed breach notifications
- Proper attribution of data sources

## Deployment and Execution

### Environment Setup
```bash
pip install -r requirements.txt
```

### Execution Options

#### HTTP-Based Scraping (Recommended)
```python
python working_scraper.py
```

#### Selenium-Based Scraping
```python
python Australian-data-breaches-scraper.py
```

### Configuration Options
- Headless vs. visible browser mode
- Rate limiting parameters
- Output file naming
- Content length limits

## Monitoring and Logging

### Logging Configuration
```python
logging.basicConfig(
    filename='scraper_errors.log',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s'
)
```

### Progress Tracking
- Request counter with status updates
- Success/failure ratios
- Content extraction statistics
- URL processing metrics

## Testing and Validation

### Structure Analysis
**File:** `analyze_structure.py`
- Website structure inspection
- Element location validation
- Content pattern analysis

### Debug Tools
**File:** `debug_scraper.py`
- Enhanced error reporting
- Content extraction debugging
- Rate limiting validation

### Test Harness
**File:** `test_scraper.py`
- Simplified scraping logic testing
- Pattern matching validation
- Output format verification

## Known Limitations

1. **Website Structure Dependencies**
   - Relies on specific HTML structure of target website
   - May break if website redesign occurs
   - CSS selector dependencies

2. **Rate Limiting Constraints**
   - Conservative delays may slow execution
   - Risk of temporary blocking with aggressive scraping

3. **Content Extraction Challenges**
   - Dynamic content may require Selenium approach
   - Paywall or login-protected content inaccessible
   - Variable content structure across source websites

## Maintenance and Updates

### Regular Monitoring
- Verify target website accessibility
- Validate data extraction accuracy
- Monitor for structural changes

### Code Maintenance
- Update dependencies regularly
- Refresh user agent strings
- Adjust rate limiting parameters based on website behavior

This specification provides sufficient detail for a software engineer to replicate the Australian cyber data scraping system while maintaining ethical scraping practices and robust error handling.