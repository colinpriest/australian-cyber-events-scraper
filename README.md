# Australian Cyber Events Discovery and Enrichment Pipeline

A comprehensive system for discovering, scraping, filtering, and enriching Australian cyber security events from multiple data sources using machine learning and LLM-based analysis.

## 🎯 Overview

This pipeline automatically discovers cyber security incidents affecting Australian organizations by collecting data from multiple sources, scraping full content from URLs, and using both machine learning and LLM-based filtering to identify relevant events. The system creates a structured database of cyber events with detailed metadata, affected entities, and confidence scores.

## ✨ Key Features

- **Multi-Source Data Collection**: GDELT, Perplexity AI, Google Search, Webber Insurance, OAIC
- **Intelligent Content Scraping**: Playwright-based scraping with enhanced Perplexity AI fallback
- **Machine Learning Filtering**: Random Forest classifier with runtime statistics tracking
- **LLM-Based Analysis**: GPT-4o-mini for event classification and entity extraction
- **Concurrent Processing**: 10-thread concurrent LLM processing for performance
- **Smart Deduplication**: Cross-month deduplication with 30-day tolerance and 3-month scope
- **Enhanced Error Handling**: Robust error recovery and detailed failure reporting
- **Comprehensive Logging**: Detailed processing logs and statistics
- **Database Management**: SQLite-based storage with backup and migration tools

## 🏗️ Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Data Sources  │───▶│  Discovery &     │───▶│  Content        │
│                 │    │  Collection      │    │  Scraping       │
│ • GDELT         │    │                  │    │                 │
│ • Perplexity    │    │                  │    │                 │
│ • Google Search │    │                  │    │                 │
│ • Webber        │    │                  │    │                 │
│ • OAIC          │    │                  │    │                 │
└─────────────────┘    └──────────────────┘    └─────────────────┘
                                                         │
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Enriched      │◀───│  LLM Analysis    │◀───│  ML Filtering   │
│   Database      │    │  & Entity        │    │  (Random        │
│                 │    │  Extraction      │    │   Forest)       │
└─────────────────┘    └──────────────────┘    └─────────────────┘
```

## 📁 Project Structure

```
australian-cyber-events-scraper/
├── discover_enrich_events.py          # Main pipeline script
├── rf_event_filter.py                 # Random Forest filter implementation
├── train_rf_filter.py                 # ML model training script
├── cyber_data_collector/              # Core data collection framework
│   ├── datasources/                   # Data source implementations
│   │   ├── gdelt.py                   # GDELT BigQuery integration
│   │   ├── perplexity.py              # Perplexity AI integration
│   │   ├── google_search.py           # Google Search API
│   │   ├── webber_insurance.py        # Webber Insurance scraper
│   │   └── oaic.py                    # OAIC data source
│   ├── processing/                    # Event processing components
│   │   ├── llm_classifier.py          # LLM-based classification
│   │   ├── entity_extractor.py        # Entity extraction
│   │   └── deduplication.py           # Deduplication algorithms
│   ├── models/                        # Data models and schemas
│   └── utils/                         # Utility functions
├── machine_learning_filter/           # ML model files
│   ├── event_training_data.xlsx       # Training dataset
│   ├── random_forest_filter.pkl       # Trained model
│   ├── source_type_encoder.pkl        # Feature encoder
│   └── text_vectorizer.pkl            # Text vectorizer
├── instance/                          # Database files
│   └── cyber_events.db                # Main SQLite database
├── documentation/                     # Setup and configuration docs
├── specifications/                    # Technical specifications
└── requirements.txt                   # Python dependencies
```

## 🚀 Quick Start

### Prerequisites

- Python 3.8+
- Google Cloud account (for GDELT BigQuery)
- OpenAI API key (for LLM analysis)
- Perplexity AI API key (optional, for fallback scraping)

### Installation

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd australian-cyber-events-scraper
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Set up environment variables**
   ```bash
   cp .env.example .env
   # Edit .env with your API keys and configuration
   ```

4. **Configure BigQuery authentication**
   ```bash
   python setup_bigquery_auth.py
   ```

5. **Run the pipeline**
   ```bash
   python discover_enrich_events.py --discover --enrich
   ```

## 🔧 Configuration

### Environment Variables

Create a `.env` file with the following variables:

```env
# API Keys
OPENAI_API_KEY=your_openai_api_key
PERPLEXITY_API_KEY=your_perplexity_api_key
GOOGLE_SEARCH_API_KEY=your_google_search_api_key
GOOGLE_SEARCH_ENGINE_ID=your_search_engine_id

# Google Cloud
GOOGLE_CLOUD_PROJECT=your_project_id
GOOGLE_APPLICATION_CREDENTIALS=path/to/service-account.json

# Database
DATABASE_URL=sqlite:///instance/cyber_events.db

# Processing
MAX_THREADS=10
BATCH_SIZE=20
```

### Data Source Configuration

Each data source can be configured in the `cyber_data_collector/models/config.py` file:

```python
# Example configuration
gdelt_config = DataSourceConfig(
    name="GDELT",
    enabled=True,
    custom_config={
        "max_records": 1000,
        "date_range_days": 30
    }
)
```

## 📊 Usage

### Main Pipeline

The primary script `discover_enrich_events.py` provides several modes of operation:

```bash
# Full pipeline (discover, scrape, enrich)
python discover_enrich_events.py --discover --enrich

# Discovery only
python discover_enrich_events.py --discover --max-events 500

# Enrichment only (for existing raw events)
python discover_enrich_events.py --enrich

# Specific data sources
python discover_enrich_events.py --discover --source GDELT --source Perplexity

# Date range processing
python discover_enrich_events.py --discover --days 7
```

### Command Line Options

| Option | Description | Default |
|--------|-------------|---------|
| `--discover` | Run discovery phase | False |
| `--enrich` | Run enrichment phase | False |
| `--max-events N` | Limit events per source | 1000 |
| `--source SOURCE` | Specific data sources | All |
| `--days N` | Date range in days | 7 |
| `--db-path PATH` | Database file path | instance/cyber_events.db |

### Training ML Models

Train the Random Forest filter with labeled data:

```bash
python train_rf_filter.py
```

This will:
- Load training data from `machine_learning_filter/event_training_data.xlsx`
- Train a Random Forest classifier
- Save the model and preprocessing components
- Generate performance metrics

## 🗄️ Database Schema

The system uses SQLite with the following main tables:

### RawEvents
Stores discovered events before processing:
- `raw_event_id` (TEXT, PRIMARY KEY)
- `source_type` (VARCHAR)
- `title` (TEXT)
- `description` (TEXT)
- `raw_content` (TEXT)
- `source_url` (TEXT)
- `event_date` (DATE)
- `is_processed` (BOOLEAN)

### EnrichedEvents
Stores processed and classified events:
- `enriched_event_id` (TEXT, PRIMARY KEY)
- `raw_event_id` (TEXT, FOREIGN KEY)
- `title` (TEXT)
- `description` (TEXT)
- `summary` (TEXT)
- `event_type` (VARCHAR)
- `severity` (VARCHAR)
- `event_date` (DATE)
- `is_australian_event` (BOOLEAN)
- `is_specific_event` (BOOLEAN)
- `confidence_score` (REAL)

### ProcessingLog
Tracks processing attempts and results:
- `log_id` (TEXT, PRIMARY KEY)
- `raw_event_id` (TEXT)
- `processing_stage` (VARCHAR)
- `status` (VARCHAR)
- `result_data` (TEXT)
- `error_message` (TEXT)

## 🤖 Machine Learning Components

### Random Forest Filter

The system uses a trained Random Forest classifier to filter out non-cyber events:

**Features:**
- Source type (categorical)
- Combined text features (title + description + content + processed URL)
- TF-IDF vectorization with 1000 features
- Bigram support for better context

**Training Data:**
- 115 labeled events
- Binary classification (keep/filter)
- Cross-validation F1 score: 0.763

**Performance:**
- Test accuracy: 87.0%
- Handles class imbalance with balanced weights
- Word boundary matching to avoid false positives

**Runtime Statistics:**
- Tracks events processed, kept, and filtered per run
- Provides detailed filtering statistics
- Resets statistics for each pipeline run
- Clear reporting of current run performance

### LLM Classification

Uses GPT-4o-mini for detailed event analysis:

**Capabilities:**
- Event validation (cyber vs non-cyber)
- Australian relevance assessment
- Event type classification
- Severity assessment
- Entity extraction
- Technical detail extraction

**Concurrent Processing:**
- 10 concurrent threads
- Semaphore-based rate limiting
- Error handling and retry logic

## 📈 Performance Optimizations

### Concurrent Processing
- **LLM Classification**: 10 concurrent threads with semaphore limiting
- **Entity Extraction**: 10 concurrent threads with semaphore limiting
- **URL Scraping**: Async/await with Playwright and enhanced fallback
- **Database Operations**: Thread-safe with locks and transaction management

### Enhanced Scraping
- **Perplexity AI Fallback**: Automatic fallback for failed or short content
- **Content Length Validation**: Triggers fallback for content < 50 characters
- **Alternative URL Discovery**: Finds working URLs for broken/inaccessible links
- **Smart Retry Logic**: Handles timeouts, 403/404 errors, and content issues

### Smart Deduplication
- **Cross-Month Processing**: Only processes events from last 3 months
- **30-Day Tolerance**: Events within 30 days can be considered duplicates
- **Multi-Month Detection**: Automatically skips when all events are from same month
- **Deduplicated Events Focus**: Processes only deduplicated events, not all enriched events

### Caching and Rate Limiting
- **API Rate Limiting**: Prevents API quota exhaustion
- **Content Caching**: Avoids re-scraping same URLs
- **Database Indexing**: Optimized queries for large datasets

### Memory Management
- **Batch Processing**: Processes events in configurable batches
- **Streaming**: Handles large datasets without memory issues
- **Connection Pooling**: Efficient database connections

## 🔍 Monitoring and Logging

### Logging Levels
- **INFO**: Pipeline progress and statistics
- **DEBUG**: Detailed processing information
- **WARNING**: Non-critical issues
- **ERROR**: Processing failures

### Statistics Tracking
- Events discovered per source
- Scraping success rates with Perplexity fallback tracking
- Random Forest filtering statistics (runtime only)
- LLM processing metrics
- Cross-month deduplication statistics
- Database operation counts

### Progress Indicators
- Real-time progress bars for long operations
- Batch processing indicators
- Success/failure counts
- Performance metrics

## 🛠️ Development and Testing

### Running Tests
```bash
# Run all tests
pytest

# Run specific test modules
pytest cyber_data_collector/tests/

# Run with coverage
pytest --cov=cyber_data_collector
```

### Database Management
```bash
# Backup database
cp instance/cyber_events.db instance/cyber_events_backup_$(date +%Y%m%d_%H%M%S).db

# Reset database
python wipe_database.py

# Migrate schema
python database_migration_v2.py
```

### Debugging Tools
```bash
# Test specific data sources
python test_gdelt_only.py
python test_perplexity_only.py

# Debug LLM extraction
python debug_llm_extractor.py

# Test Random Forest filter
python debug_rf_filter.py
```

## 📚 Documentation

- **[BigQuery Setup](documentation/bigquery_setup.md)**: Google Cloud configuration
- **[Data Sources](specifications/all_cyber_data_sources.md)**: Detailed source specifications
- **[Storage Schema](specifications/data_storage_specifications.md)**: Database design
- **[GDELT Integration](specifications/gdelt_data_source.md)**: GDELT-specific documentation

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests for new functionality
5. Ensure all tests pass
6. Submit a pull request

### Code Style
- Follow PEP 8 guidelines
- Use type hints
- Add docstrings for functions and classes
- Include error handling
- Write comprehensive tests

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 🆘 Troubleshooting

### Common Issues

**BigQuery Authentication Errors**
```bash
# Re-authenticate
python setup_bigquery_auth.py
# or
gcloud auth application-default login
```

**LLM API Rate Limits**
- Check your OpenAI API usage
- Reduce concurrent threads if needed
- Implement exponential backoff

**Database Lock Errors**
- Ensure only one pipeline instance runs at a time
- Check for zombie processes
- Restart if necessary

**Memory Issues**
- Reduce batch size in configuration
- Process smaller date ranges
- Monitor system resources

**Scraping Failures**
- Check Perplexity API key configuration
- Verify network connectivity
- Review scraping logs for specific error patterns
- Ensure Playwright dependencies are installed

**Cross-Month Deduplication Issues**
- Verify events span multiple months (not just same day)
- Check event_date field accuracy
- Review deduplication tolerance settings (30 days)

### Getting Help

1. Check the logs in `discover_enrich_events.log`
2. Review the troubleshooting section in documentation
3. Check GitHub issues for similar problems
4. Create a new issue with detailed error information

## 🆕 Recent Improvements

### Enhanced Perplexity AI Integration
- **Content Length Fallback**: Automatically triggers Perplexity when content is too short (< 50 chars)
- **Alternative URL Discovery**: Finds working URLs for broken or inaccessible links
- **Improved Success Tracking**: Detailed reporting of Perplexity fallback attempts and success rates

### Smart Deduplication System
- **Cross-Month Intelligence**: Only processes events from last 3 months of actual event dates
- **30-Day Tolerance**: Extended from 7 days to 30 days for better duplicate detection
- **Automatic Skip Logic**: Skips cross-month deduplication when all events are from same month
- **Deduplicated Events Focus**: Processes only deduplicated events, not all enriched events

### Random Forest Filter Enhancements
- **Runtime Statistics**: Tracks filtering performance per pipeline run
- **Clear Reporting**: Shows current run statistics, not cumulative totals
- **Statistics Reset**: Clean slate for each pipeline execution

### Robust Error Handling
- **Enhanced Scraping**: Better handling of timeouts, HTTP errors, and content issues
- **Database Transaction Management**: Improved transaction handling and error recovery
- **Detailed Failure Reporting**: Comprehensive logging of scraping failures and fallback attempts

## 🔮 Future Enhancements

- **Additional Data Sources**: More cyber security feeds
- **Advanced ML Models**: Deep learning for better classification
- **Real-time Processing**: Stream processing capabilities
- **Web Interface**: Dashboard for monitoring and management
- **API Endpoints**: REST API for external integrations
- **Cloud Deployment**: Docker and Kubernetes support
- **Enhanced Fallback Systems**: Multiple fallback strategies for failed scrapes
- **Advanced Deduplication**: ML-based similarity detection
- **Performance Analytics**: Detailed performance metrics and optimization suggestions

---

**Built with ❤️ for Australian cyber security research and monitoring.**
