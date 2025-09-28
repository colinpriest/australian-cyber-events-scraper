# Cyber Emerging Risk Detector - Data Preparation Specification

This specification provides detailed technical guidance for replicating the data scraping, API data sourcing, and data preparation pipeline implemented in the Cyber Emerging Risk Detector project.

## Overview

The system collects and processes cybersecurity news articles from Google Custom Search API, applies intelligent filtering and categorization using LLMs, and performs time-series analysis to identify emerging cyber risks. The pipeline is designed to handle rate limiting, caching, and data quality issues while providing reliable, structured data for risk analysis.

## Table of Contents

1. [Prerequisites and Setup](#prerequisites-and-setup)
2. [API Configuration](#api-configuration)
3. [Data Collection Architecture](#data-collection-architecture)
4. [Data Scraping Implementation](#data-scraping-implementation)
5. [Data Processing and Transformation](#data-processing-and-transformation)
6. [Caching Strategy](#caching-strategy)
7. [Data Models and Schema](#data-models-and-schema)
8. [Quality Assurance and Validation](#quality-assurance-and-validation)
9. [Performance Optimization](#performance-optimization)
10. [Error Handling and Recovery](#error-handling-and-recovery)
11. [Testing and Validation](#testing-and-validation)

## Prerequisites and Setup

### Required Dependencies

Install the following Python packages (`requirements.txt`):

```text
instructor
openai
python-dotenv
pydantic
requests
python-dateutil
beautifulsoup4
lxml
cloudscraper
playwright
playwright-stealth
```

### System Requirements

- Python 3.8+
- 4GB RAM minimum
- Internet connection for API calls
- Chromium browser for Playwright (automatically installed)

### Environment Configuration

Create a `.env` file with the following variables:

```env
GOOGLE_CUSTOMSEARCH_CX_KEY=your_google_cx_key
GOOGLE_CUSTOMSEARCH_API_KEY=your_google_api_key
OPENAI_API_KEY=your_openai_api_key
```

## API Configuration

### Google Custom Search API Setup

1. **Google Cloud Console Setup**:
   - Create or select a Google Cloud project
   - Enable the Custom Search API
   - Create an API key with Custom Search API access

2. **Custom Search Engine Configuration**:
   - Visit [Google Programmable Search Engine](https://programmablesearchengine.google.com/)
   - Create a new search engine
   - Configure to search the entire web or specific cybersecurity domains
   - Note the Search Engine ID (CX key)

3. **API Quotas and Limits**:
   - Free tier: 100 queries/day
   - Paid tier: Up to 10,000 queries/day
   - Rate limit: 10 queries/second

### OpenAI API Configuration

1. **API Access**:
   - Sign up at [OpenAI Platform](https://platform.openai.com/)
   - Generate API key with GPT-4 access
   - Set billing limits for cost control

2. **Model Selection**:
   - Primary: `gpt-4o-mini` for classification and analysis
   - Fallback: `gpt-3.5-turbo` for cost optimization

## Data Collection Architecture

### Core Components

1. **news_fetcher.py**: Main data collection module
2. **cache_manager.py**: Caching and cache management
3. **data_models.py**: Pydantic data models
4. **time_series_analyzer.py**: Historical data processing

### Data Flow Pipeline

```
Google Search API → Raw Articles → Date Parsing → Filtering → LLM Classification → Cache Storage → Analysis
```

## Data Scraping Implementation

### Search Query Construction

The system uses carefully crafted search queries to target cybersecurity content:

```python
def fetch_monthly_articles(google_cx: str, google_api_key: str, month_key: str, articles_needed: int = 100):
    # Calculate date range
    start_date_obj = datetime.strptime(f"{month_key}-01", "%Y-%m-%d")
    end_of_month = (start_date_obj + timedelta(days=31)).replace(day=1) - timedelta(days=1)
    end_date_str = end_of_month.strftime("%Y-%m-%d")
    start_date_str = start_date_obj.strftime("%Y-%m-%d")

    # Construct targeted search query
    search_query = f'cybersecurity ("data breach" OR "ransomware" OR "phishing" OR "vulnerability") after:{start_date_str} before:{end_date_str}'
    sort_by_date_range = f"date:r:{start_date_str.replace('-', '')}:{end_date_str.replace('-', '')}"
```

### Google Custom Search API Integration

```python
def run_google_search(google_cx: str, google_api_key: str, query: str, sort_by_date_range: str, pages: int = 10):
    """Generic function to run paginated Google Custom Search."""
    url = "https://www.googleapis.com/customsearch/v1"
    all_results = []

    for i in range(pages):
        start_index = 1 + (i * 10)
        params = {
            "key": google_api_key,
            "cx": google_cx,
            "q": query,
            "num": 10,
            "sort": sort_by_date_range,
            "start": start_index
        }

        try:
            response = requests.get(url, params=params, timeout=20)
            if response.status_code == 429:
                print("Rate limited by Google API. Waiting 60s...")
                time.sleep(60)
                response = requests.get(url, params=params, timeout=20)

            response.raise_for_status()
            data = response.json()
            results = data.get("items", [])

            if not results:
                break

            all_results.extend(results)
            time.sleep(1)  # Rate limiting protection

        except requests.RequestException as e:
            print(f"Error during Google search: {e}")
            break

    return all_results
```

### Date Extraction and Parsing

The system implements multiple fallback strategies for extracting publication dates:

```python
def parse_date(raw_article_pagemap: Dict[str, Any], fallback_month: str, browser: Optional[Any] = None, url: Optional[str] = None):
    """Parse publication date with multiple fallback strategies."""

    # Primary: Check structured data in pagemap
    if raw_article_pagemap:
        try:
            metatags = raw_article_pagemap.get('metatags', [{}])[0]
            cse_image = raw_article_pagemap.get('cse_image', [{}])[0]

            date_str = (
                metatags.get('og:article:published_time') or
                metatags.get('article:published_time') or
                metatags.get('og:published_time') or
                metatags.get('published_date') or
                metatags.get('date') or
                cse_image.get('date')
            )
            if date_str:
                return date_str
        except (KeyError, IndexError):
            pass

    # Fallback 1: Scrape the article URL
    if browser and url:
        scraped_date = scrape_article_for_date(browser, url)
        if scraped_date:
            return scraped_date

    # Fallback 2: Use first day of month
    try:
        year, month = map(int, fallback_month.split('-'))
        return datetime(year, month, 1).isoformat()
    except (ValueError, TypeError):
        return None
```

### Web Scraping with Playwright

For robust date extraction, the system uses Playwright with stealth measures:

```python
def scrape_article_for_date(browser, url: str) -> Optional[str]:
    """Scrape URL for publication date using stealthed Playwright."""
    page = None
    try:
        page = browser.new_page()
        stealth_sync(page)  # Apply stealth measures

        response = page.goto(url, timeout=20000, wait_until='domcontentloaded')
        if not response or not response.ok:
            return None

        content = page.content()
        soup = BeautifulSoup(content, 'lxml')

        # Comprehensive date selectors
        selectors = [
            {'tag': 'meta', 'attrs': {'property': 'article:published_time'}},
            {'tag': 'meta', 'attrs': {'name': 'pubdate'}},
            {'tag': 'meta', 'attrs': {'name': 'date'}},
            {'tag': 'time', 'attrs': {}},
        ]

        for selector in selectors:
            element = soup.find(selector['tag'], selector['attrs'])
            if element:
                date_str = element.get('content') or element.text
                if date_str:
                    return date_str
    except PlaywrightTimeoutError:
        print(f"Scraping timed out for {url}")
    except Exception as e:
        print(f"Scraping failed for {url}: {e}")
    finally:
        if page:
            page.close()
    return None
```

## Data Processing and Transformation

### Article Data Model

```python
class Article(BaseModel):
    title: str = Field(..., description="The headline of the news article.")
    description: str = Field(..., description="A brief summary or snippet of the article.")
    published_date: Optional[str] = Field(None, description="The ISO 8601 publication date.")
    source: str = Field(..., description="The domain name of the news source.")
    category: Optional[CyberEventCategory] = Field(None, description="LLM-classified category.")
    is_cyber_event: Optional[bool] = Field(None, description="Flag for specific cyber attack events.")
```

### LLM-Based Article Classification

The system uses OpenAI's API with structured output to classify articles:

```python
def categorize_articles_with_llm(client: instructor.Instructor, articles: List[Article]) -> List[Article]:
    """Classify articles using LLM with defined taxonomy."""

    articles_to_classify = [
        ArticleToClassify(id=i, text=f"Title: {article.title}\nDescription: {article.description}")
        for i, article in enumerate(articles)
    ]

    try:
        classified_results = client.chat.completions.create(
            model="gpt-4o-mini",
            response_model=List[ClassifiedArticle],
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an expert cybersecurity analyst. Classify news articles with two outputs:\n"
                        "1. `category`: Choose from the provided taxonomy.\n"
                        "2. `is_cyber_event`: True ONLY for specific, recent cyber attacks at named entities.\n"
                        f"Taxonomy: {', '.join([cat.value for cat in CyberEventCategory])}"
                    )
                },
                {
                    "role": "user",
                    "content": f"Classify these articles:\n{json.dumps([a.model_dump() for a in articles_to_classify], indent=2)}"
                }
            ],
            max_retries=2
        )

        # Map classifications back to original articles
        for result in classified_results:
            if result.id < len(articles):
                articles[result.id].category = result.category
                articles[result.id].is_cyber_event = result.is_cyber_event

        return articles
    except Exception as e:
        print(f"LLM categorization failed: {e}")
        # Default fallback values
        for article in articles:
            article.category = CyberEventCategory.OTHER
            article.is_cyber_event = False
        return articles
```

### Cyber Event Categories

The system uses a defined taxonomy for consistent classification:

```python
class CyberEventCategory(str, Enum):
    RANSOMWARE = "Ransomware"
    DATA_BREACH = "Data Breach"
    PHISHING = "Phishing"
    VULNERABILITY_EXPLOIT = "Vulnerability Exploit"
    STATE_SPONSORED_ATTACK = "State-Sponsored Attack"
    SUPPLY_CHAIN_ATTACK = "Supply Chain Attack"
    MALWARE = "Malware"
    INSIDER_THREAT = "Insider Threat"
    DENIAL_OF_SERVICE = "Denial of Service"
    OTHER = "Other"
```

### Data Filtering and Quality Control

The system applies multiple filtering stages:

1. **Date Filtering**: Only articles after July 1, 2024
2. **Relevance Filtering**: Only articles marked as `is_cyber_event=True`
3. **Top-N Selection**: Keep only the 50 most recent/relevant articles

```python
# Filter for specific cyber events only
event_articles = [article for article in articles if article.is_cyber_event]
print(f"Filtered down to {len(event_articles)} articles about specific cyber events.")

# Keep top 50 for analysis
articles = event_articles[:50]
```

### ASCII Conversion and Text Cleaning

To ensure data compatibility, all text is converted to ASCII:

```python
def convert_to_ascii(text: str) -> str:
    """Convert text to ASCII, removing or replacing Unicode characters."""
    if not text:
        return ""

    try:
        # Convert to ASCII, ignoring non-ASCII characters
        ascii_text = text.encode('ascii', errors='ignore').decode('ascii')

        # Clean up common Unicode punctuation
        replacements = {
            '"': '"', '"': '"', ''': "'", ''': "'",
            '–': "-", '—': "-", '…': "...",
            '®': "(R)", '©': "(C)", '™': "(TM)",
            '°': " degrees", '±': "+/-"
        }

        for unicode_char, ascii_replacement in replacements.items():
            ascii_text = ascii_text.replace(unicode_char, ascii_replacement)

        # Remove non-printable characters
        cleaned_text = ''.join(char for char in ascii_text
                              if char.isprintable() or char in '\n\r\t ')

        # Clean up multiple spaces
        cleaned_text = ' '.join(cleaned_text.split())

        return cleaned_text
    except Exception:
        return ''.join(char for char in text
                      if ord(char) < 128 and (char.isprintable() or char in '\n\r\t '))
```

## Caching Strategy

### Cache Architecture

The system implements a sophisticated caching mechanism to minimize API costs:

- **Cache Directory**: `news/cache/`
- **Cache Format**: JSON files named `{YYYY-MM}_articles.json`
- **Cache Validity**: 7 days for current month, 30 days for past months

### Cache Implementation

```python
def fetch_monthly_articles_with_cache(google_cx: str, google_api_key: str, start_date: str, end_date: str, month_key: str, max_results: int = 10, cache_dir: Path = None):
    """Fetch articles with intelligent caching."""

    # Check cache first
    cache_file = cache_dir / f"{month_key}_articles.json"
    if cache_file.exists():
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cached_data = json.load(f)

            # Check cache validity
            cache_date = datetime.fromisoformat(cached_data.get('cache_date', '2020-01-01'))
            current_date = datetime.now()

            # Determine cache validity period
            if month_key == current_date.strftime('%Y-%m'):
                cache_valid_days = 7  # Current month
            else:
                cache_valid_days = 30  # Past months

            if (current_date - cache_date).days < cache_valid_days:
                print(f"Using cached data for {month_key}")
                articles = [Article(**article_data) for article_data in cached_data.get('articles', [])]
                return articles[:max_results]
            else:
                print(f"Cache expired for {month_key}, fetching fresh data...")
        except Exception as e:
            print(f"Cache corrupted for {month_key}: {e}, fetching fresh data...")

    # Fetch fresh data
    articles = fetch_monthly_articles(google_cx, google_api_key, month_key, max_results)

    # Save to cache
    if cache_dir:
        cache_data = {
            'month': month_key,
            'cache_date': datetime.now().isoformat(),
            'articles': [article.model_dump() for article in articles],
            'count': len(articles)
        }

        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, indent=2)
            print(f"Cached {len(articles)} articles for {month_key}")
        except Exception as e:
            print(f"Failed to cache data for {month_key}: {e}")

    return articles
```

### Cache Management

```python
def get_cache_info(cache_dir: Path = None) -> Dict[str, Any]:
    """Get comprehensive cache information."""
    if cache_dir is None:
        cache_dir = Path("news/cache")

    cache_info = {
        'cache_dir': str(cache_dir),
        'exists': cache_dir.exists(),
        'files': [],
        'total_articles': 0,
        'oldest_cache': None,
        'newest_cache': None
    }

    if cache_dir.exists():
        cache_files = list(cache_dir.glob("*.json"))
        cache_info['file_count'] = len(cache_files)

        for cache_file in cache_files:
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                cache_date = datetime.fromisoformat(data.get('cache_date', '2020-01-01'))
                cache_info['files'].append({
                    'file': cache_file.name,
                    'month': data.get('month', 'unknown'),
                    'cache_date': data.get('cache_date'),
                    'article_count': data.get('count', 0),
                    'age_days': (datetime.now() - cache_date).days
                })
                cache_info['total_articles'] += data.get('count', 0)

                # Track oldest/newest cache
                if cache_info['oldest_cache'] is None or cache_date < datetime.fromisoformat(cache_info['oldest_cache']):
                    cache_info['oldest_cache'] = data.get('cache_date')
                if cache_info['newest_cache'] is None or cache_date > datetime.fromisoformat(cache_info['newest_cache']):
                    cache_info['newest_cache'] = data.get('cache_date')

            except Exception as e:
                cache_info['files'].append({
                    'file': cache_file.name,
                    'error': str(e)
                })

    return cache_info
```

## Data Models and Schema

### Time Series Analysis Models

```python
class EventCategory(BaseModel):
    """Category of cyber events with trend information."""
    category: str = Field(..., description="Event category name")
    count: int = Field(..., description="Number of events in category")
    trend: str = Field(..., description="Trend direction: 'increasing', 'decreasing', 'stable'")
    percentage_change: float = Field(..., description="Percentage change from previous month")

class MonthlyTrend(BaseModel):
    """Cyber event trends for a specific month."""
    month: str = Field(..., description="Month in YYYY-MM format")
    total_events: int = Field(..., description="Total number of cyber events")
    categories: List[EventCategory] = Field(..., description="Breakdown by category")
    top_threat: str = Field(..., description="Most prominent threat type")
    key_insight: str = Field(..., description="Key insight about the month's landscape")

class TimeSeriesAnalysis(BaseModel):
    """Complete 12-month time series analysis."""
    monthly_trends: List[MonthlyTrend] = Field(..., description="Monthly breakdown")
    overall_trend: str = Field(..., description="Overall trend direction")
    most_volatile_category: str = Field(..., description="Most variable category")
    emerging_patterns: List[str] = Field(..., description="Identified patterns")
    time_series_summary: str = Field(..., description="Comprehensive summary")
```

### Time Series Processing

```python
def get_historical_news_summary(articles: List[Article]) -> Dict[str, Any]:
    """Create summary of historical news data."""
    monthly_data = {}

    for article in articles:
        month_key = None

        if article.published_date:
            try:
                date_obj = dateutil_parse(article.published_date, ignoretz=True)
                month_key = date_obj.strftime('%Y-%m')
            except (ValueError, TypeError, AttributeError):
                continue

        if not month_key:
            continue

        if month_key not in monthly_data:
            monthly_data[month_key] = {
                'articles': [],
                'total_count': 0,
                'categories': {}
            }

        monthly_data[month_key]['articles'].append(article)
        monthly_data[month_key]['total_count'] += 1

        # Use pre-assigned category from LLM
        category = article.category.value if article.category else CyberEventCategory.OTHER.value
        if category not in monthly_data[month_key]['categories']:
            monthly_data[month_key]['categories'][category] = 0
        monthly_data[month_key]['categories'][category] += 1

    return monthly_data
```

## Quality Assurance and Validation

### Data Validation Pipeline

1. **Schema Validation**: All data validated against Pydantic models
2. **Date Validation**: Publication dates parsed and validated
3. **Content Quality**: ASCII conversion and text cleaning
4. **Duplication Detection**: URL-based deduplication
5. **Relevance Filtering**: LLM-based event classification

### Error Handling Strategies

```python
# Comprehensive error handling in main pipeline
try:
    articles = get_news(
        google_cx=google_cx,
        google_api_key=google_api_key,
        save_dir=news_dir,
        months_back=12,
        articles_per_month=100
    )

    if not articles:
        print("No articles found. Exiting.")
        return

    # Categorize articles
    articles = categorize_articles_with_llm(client, articles)

    # Filter and validate
    event_articles = [article for article in articles if article.is_cyber_event]
    articles = event_articles[:50]

except Exception as e:
    print(f"Error in data pipeline: {e}")
    return
```

## Performance Optimization

### Rate Limiting and API Management

- Google API: 1-second delays between requests
- OpenAI API: Built-in retry logic with exponential backoff
- Batch processing for LLM classification
- Connection pooling and timeout management

### Memory Management

- Process articles in batches
- Clear unused variables
- Use generators where appropriate
- Implement garbage collection hints

### Concurrent Processing

```python
# Browser instance management for concurrent scraping
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)

    for raw_article in raw_articles:
        # Process articles with shared browser instance
        pass

    browser.close()
```

## Error Handling and Recovery

### API Failure Recovery

```python
def run_google_search_with_retry(google_cx: str, google_api_key: str, query: str, sort_by_date_range: str, pages: int = 10):
    """Google search with comprehensive error handling."""
    try:
        response = requests.get(url, params=params, timeout=20)
        if response.status_code == 429:
            print("Rate limited by Google API. Waiting 60s...")
            time.sleep(60)
            response = requests.get(url, params=params, timeout=20)

        response.raise_for_status()
        return response.json()

    except requests.RequestException as e:
        print(f"Error during Google search: {e}")
        return None
```

### Cache Corruption Recovery

```python
def handle_cache_corruption(cache_file: Path, month_key: str):
    """Handle corrupted cache files."""
    try:
        # Attempt to load cache
        with open(cache_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data
    except Exception as e:
        print(f"Cache corrupted for {month_key}: {e}")
        # Delete corrupted cache and fetch fresh data
        if cache_file.exists():
            cache_file.unlink()
        return None
```

## Testing and Validation

### Unit Testing Framework

```python
def test_article_classification():
    """Test article classification functionality."""
    sample_articles = [
        Article(
            title="Major Data Breach at Company X",
            description="Hackers stole 1M records from Company X",
            published_date="2024-09-15T10:00:00",
            source="example.com"
        )
    ]

    # Test classification
    classified = categorize_articles_with_llm(mock_client, sample_articles)
    assert classified[0].category == CyberEventCategory.DATA_BREACH
    assert classified[0].is_cyber_event == True
```

### Integration Testing

```python
def test_end_to_end_pipeline():
    """Test complete data pipeline."""
    # Test with minimal data
    articles = get_news(
        google_cx="test_cx",
        google_api_key="test_api",
        save_dir=Path("test_output"),
        months_back=1,
        articles_per_month=5
    )

    assert len(articles) > 0
    assert all(isinstance(article, Article) for article in articles)
```

### Cache Testing

```python
def test_cache_functionality():
    """Test caching mechanisms."""
    cache_dir = Path("test_cache")
    cache_dir.mkdir(exist_ok=True)

    # Test cache creation
    articles = fetch_monthly_articles_with_cache(
        "test_cx", "test_api", "2024-09-01", "2024-09-30",
        "2024-09", 10, cache_dir
    )

    # Verify cache file exists
    cache_file = cache_dir / "2024-09_articles.json"
    assert cache_file.exists()

    # Test cache retrieval
    cached_articles = fetch_monthly_articles_with_cache(
        "test_cx", "test_api", "2024-09-01", "2024-09-30",
        "2024-09", 10, cache_dir
    )

    assert len(cached_articles) == len(articles)
```

## Deployment Considerations

### Environment Variables

Ensure proper environment variable management:
- Use `.env` files for development
- Use secure environment variable injection for production
- Never commit API keys to version control

### Monitoring and Logging

Implement comprehensive logging:

```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('cyber_detector.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)
```

### Resource Management

- Monitor API quota usage
- Implement disk space monitoring for cache
- Set memory limits for large datasets
- Configure appropriate timeouts for all network calls

This specification provides a complete blueprint for replicating the data preparation pipeline. The implementation handles real-world challenges including rate limiting, data quality issues, caching strategies, and robust error handling while maintaining high data quality and system reliability.