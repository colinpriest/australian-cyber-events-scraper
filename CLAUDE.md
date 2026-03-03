# Australian Cyber Events Discovery and Enrichment Pipeline

A comprehensive Python system for discovering, scraping, filtering, and enriching Australian cyber security events from multiple data sources using machine learning and LLM-based analysis. The pipeline combines Perplexity AI search, OAIC breach notifications, Google Custom Search, Webber Insurance data, and optional GDELT feeds to create a structured, deduplicated database of cyber incidents with detailed metadata, affected entities, and ASD risk classifications.

## Tech Stack

| Layer | Technology | Version | Purpose |
|-------|------------|---------|---------|
| Runtime | Python | 3.8+ | Core scripting and data processing |
| Framework | Pydantic | 2.x | Data validation and models |
| LLM Primary | OpenAI GPT-4o | Latest | Primary event extraction and enrichment |
| LLM Fallback | GPT-4o-mini | Latest | Fast initial filtering during discovery |
| AI Search | Perplexity AI | API v1 | Real-time cyber event discovery and fact-checking |
| Database | SQLite | 3.x | Event storage and persistence |
| Web Scraping | Playwright | 1.44+ | Browser-based content extraction |
| Data Processing | Pandas | 2.x | Data manipulation and analysis |
| ML Classification | scikit-learn | 1.3+ | Random Forest event filtering |
| PDF Processing | pdfplumber, tabula-py | Latest | OAIC PDF and Power BI dashboard extraction |
| Cloud APIs | Google BigQuery | 3.14+ | GDELT data source (optional, expensive) |
| CLI | Typer | 0.15+ | Command-line interface |

## Quick Start

### Prerequisites

- Python 3.8+
- API keys for:
  - OpenAI (OPENAI_API_KEY) - required for LLM enrichment
  - Perplexity AI (PERPLEXITY_API_KEY) - recommended for event discovery
  - Google Custom Search (optional) - for Google Search data source
  - Google Cloud BigQuery (optional, expensive) - only for GDELT source

### Installation

```bash
# Clone the repository
git clone https://github.com/colinpriest/australian-cyber-events-scraper.git
cd australian-cyber-events-scraper

# Install dependencies
pip install -r requirements.txt

# For OAIC Power BI dashboard scraping (optional):
playwright install chromium

# Create and configure environment variables
cp .env.example .env
# Edit .env with your API keys
```

### Development

```bash
# Run the unified pipeline (discovers, enriches, deduplicates, classifies, generates dashboard)
python run_full_pipeline.py

# Quick rolling refresh (last 90 days, recommended for monthly updates)
python pipeline.py refresh

# Check pipeline status (last ingest + latest event)
python pipeline.py status

# Run tests
pytest

# Generate dashboard only (if data already exists)
python run_full_pipeline.py --dashboard-only
```

### Available Commands

| Command | Description |
|---------|-------------|
| `python pipeline.py refresh` | Rolling 3-month refresh with recommended sources |
| `python pipeline.py status` | Report last ingest and latest event |
| `python run_full_pipeline.py` | Full pipeline with all 5 phases |
| `python run_full_pipeline.py --discover-only` | Discovery phase only (auto-enriches with Perplexity) |
| `python run_full_pipeline.py --dashboard-only` | Generate dashboard from existing data |
| `python asd_risk_classifier.py` | Classify events with ASD risk matrix |
| `python build_static_dashboard.py` | Generate interactive HTML dashboard |
| `pytest` | Run test suite |

## Project Structure

```
australian-cyber-events-scraper/
├── run_full_pipeline.py           # MAIN ENTRY POINT: unified pipeline (all phases)
├── pipeline.py                     # Simplified CLI wrapper (refresh/status/rebuild)
├── project_status.py               # Status reporter utility
├── asd_risk_classifier.py          # ASD risk classification standalone
├── build_static_dashboard.py       # Dashboard HTML generation
├── wipe_database.py                # Database reset utility
├── requirements.txt                # Python dependencies
│
├── cyber_data_collector/           # MAIN MODULE: core collection and processing framework
│   ├── __init__.py
│   ├── datasources/                # Data source implementations
│   │   ├── base.py                 # Base DataSource class
│   │   ├── perplexity.py           # Perplexity AI integration
│   │   ├── oaic.py                 # OAIC (Office of Australian Info Commissioner)
│   │   ├── google_search.py        # Google Custom Search API
│   │   ├── webber_insurance.py     # Webber Insurance scraper
│   │   └── gdelt.py                # GDELT BigQuery integration (expensive)
│   │
│   ├── enrichment/                 # High-quality enrichment pipeline (5-stage process)
│   │   ├── high_quality_enrichment_pipeline.py  # Main orchestrator
│   │   ├── content_acquisition.py  # Multi-tier content scraping (newspaper3k → trafilatura → BeautifulSoup)
│   │   ├── gpt4o_enricher.py       # Stage 2: GPT-4o primary extraction
│   │   ├── perplexity_fact_checker.py # Stage 3: Perplexity fact-checking
│   │   ├── enrichment_validator.py # Stage 4: Validation and confidence scoring
│   │   └── enrichment_audit_storage.py # Audit trail for all enrichment stages
│   │
│   ├── processing/                 # Event processing and enrichment components
│   │   ├── entity_extractor.py     # Entity (victim, threat actor) extraction
│   │   ├── llm_classifier.py       # Event type and severity classification
│   │   ├── deduplication.py        # V1 deduplication algorithm
│   │   ├── deduplication_v2.py     # V2 entity-based deduplication (current)
│   │   ├── perplexity_enrichment.py # Perplexity enrichment module
│   │   └── perplexity_enricher.py  # Alternative enricher implementation
│   │
│   ├── filtering/                  # Event quality filtering
│   │   ├── progressive_filter.py   # Multi-stage filtering logic
│   │   └── confidence_filter.py    # Confidence-based filtering
│   │
│   ├── storage/                    # Data persistence layer
│   │   ├── database.py             # SQLite database operations
│   │   ├── deduplication_storage.py # Deduplication tracking
│   │   └── cache.py                # Caching layer
│   │
│   ├── models/                     # Data models and schemas
│   │   ├── config.py               # Configuration models
│   │   ├── events.py               # CyberEvent, EventSeverity, CyberEventType enums
│   │   └── vulnerability_taxonomy.py # Attack type taxonomy
│   │
│   ├── pipelines/                  # High-level pipeline implementations
│   │   └── discovery.py            # Discovery and initial processing pipeline
│   │
│   ├── utils/                      # Utility and helper modules
│   │   ├── config_manager.py       # Environment and config management
│   │   ├── rate_limiter.py         # API rate limiting
│   │   ├── thread_manager.py       # Concurrent processing management
│   │   ├── validation.py           # Data validation utilities
│   │   ├── pdf_extractor.py        # PDF content extraction
│   │   └── logging_config.py       # Logging configuration
│   │
│   └── tests/                      # Test suite
│       ├── test_deduplication.py
│       ├── test_deduplication_v2.py
│       ├── test_enrichment_audit_storage_validation.py
│       └── test_validation_utils.py
│
├── machine_learning_filter/        # ML model artifacts
│   ├── random_forest_filter.pkl    # Trained Random Forest model
│   ├── source_type_encoder.pkl     # Feature encoder
│   ├── text_vectorizer.pkl         # TF-IDF vectorizer
│   └── event_training_data.xlsx    # Training dataset
│
├── instance/                       # Database and runtime files
│   └── cyber_events.db             # SQLite database
│
├── dashboard/                      # Generated dashboard
│   └── index.html                  # Static HTML dashboard
│
├── .claude/                        # Claude Code configuration
│   └── settings.local.json         # Local Claude settings
│
└── documentation/                  # Additional documentation
```

## Architecture Overview

The pipeline executes five distinct phases:

```
[Data Sources] ──▶ [Discovery & Scraping] ──▶ [Perplexity Enrichment]
                         │                            │
                         ▼                            ▼
                    [ML Filtering]          [Global Deduplication]
                         │                            │
                         └──────────┬─────────────────┘
                                    ▼
                         [ASD Risk Classification]
                                    │
                                    ▼
                         [Dashboard Generation]
```

### Phase 1: Discovery & Initial Processing
- Collects events from Perplexity, OAIC, Google Search, Webber Insurance, GDELT
- Scrapes full article content using Playwright + Perplexity fallback
- Performs fast initial GPT-4o-mini filtering (quality check)
- Stores raw events in database

### Phase 2: Perplexity AI Enrichment
- **Automatic**: Runs after discovery for all new events
- Extracts formal entity names, threat actors, attack methods
- Performs multi-source verification for confidence scoring
- Much higher quality than initial fast-pass

### Phase 3: Global Deduplication
- Entity-based matching with 0.15 similarity threshold
- Rule 1: Same entity + same date → merge
- Rule 2: Same entity + similar titles → merge
- Uses earliest event date for merged events

### Phase 4: ASD Risk Classification
- Assigns C1-C6 severity categories using Australian Signals Directorate framework
- Determines stakeholder impact levels
- Uses GPT-4o for intelligent classification
- Incremental: only processes unclassified events

### Phase 5: Dashboard Generation
- Generates static HTML dashboard with embedded data
- Includes ASD risk matrices for all years
- Creates interactive Chart.js visualizations

### Key Modules

| Module | Location | Purpose | Use When |
|--------|----------|---------|----------|
| UnifiedPipeline | run_full_pipeline.py | Complete 5-phase pipeline orchestrator | Running full pipeline or specific phases |
| DiscoveryPipeline | cyber_data_collector/pipelines/discovery.py | Data discovery and initial processing | Collecting new events |
| HighQualityEnrichmentPipeline | cyber_data_collector/enrichment/high_quality_enrichment_pipeline.py | 5-stage enrichment orchestrator | Enriching events with LLMs |
| DeduplicationV2 | cyber_data_collector/processing/deduplication_v2.py | Entity-based deduplication | Merging duplicate events |
| DatabaseManager | cyber_data_collector/storage/database.py | SQLite operations | Persisting/querying events |
| ConfigManager | cyber_data_collector/utils/config_manager.py | Environment config loading | Accessing API keys and settings |

## Development Guidelines

### Code Style

**File Naming:**
- Module files: `snake_case` (e.g., `config_manager.py`, `perplexity_enricher.py`)
- Test files: `test_*.py` or `*_test.py` (e.g., `test_deduplication.py`)
- Scripts: `snake_case` (e.g., `run_full_pipeline.py`, `asd_risk_classifier.py`)

**Code Naming (Python):**
- Classes/Functions: `PascalCase` for classes (e.g., `ConfigManager`, `DatabaseManager`)
- Functions: `snake_case` (e.g., `load_events()`, `validate_entity_name()`)
- Variables: `snake_case` (e.g., `event_count`, `is_valid`)
- Constants: `SCREAMING_SNAKE_CASE` (e.g., `MAX_THREADS`, `DEFAULT_BATCH_SIZE`)
- Private methods/fields: `_leading_underscore` (e.g., `_resolve_db_path()`, `_config`)
- Boolean variables: prefix with `is_`, `has_`, `should_` (e.g., `is_valid`, `has_content`)
- Enum values: `SCREAMING_SNAKE_CASE` (e.g., `EventSeverity.CRITICAL`)

**Import Order:**
1. Python standard library (os, sys, datetime, etc.)
2. Third-party packages (pydantic, requests, pandas, etc.)
3. Type hints (from typing import ...)
4. Local module imports (from cyber_data_collector import ...)
5. Relative imports (from . import ...)

Example:
```python
from __future__ import annotations

import logging
from typing import Dict, List, Optional
from pathlib import Path

import pandas as pd
from pydantic import BaseModel

from cyber_data_collector.models.events import CyberEvent
from cyber_data_collector.storage.database import DatabaseManager
```

**Type Hints:**
- Always use type hints for function parameters and return types
- Use `from __future__ import annotations` at top of file for forward references
- Use `Optional[T]` for nullable types
- Use `Union[T1, T2]` or `T1 | T2` for multiple types

**Docstrings:**
- Use triple-quoted docstrings for modules, classes, and public methods
- Format: one-line summary, blank line, detailed description if needed
- Include parameter descriptions and return type for complex functions

Example:
```python
def process_event(event: RawEvent, enrich: bool = True) -> EnrichedEvent:
    """Process a raw event through the enrichment pipeline.

    Args:
        event: The raw event to process
        enrich: Whether to run LLM enrichment (default: True)

    Returns:
        The enriched event with extracted metadata
    """
```

### Database Schema

The project uses SQLite with these main tables:

**RawEvents:** Initial discovered events
- `raw_event_id` (TEXT, PK)
- `source_type` (VARCHAR) - Perplexity, OAIC, GoogleSearch, WebberInsurance, GDELT
- `raw_title`, `raw_description`, `raw_content` (TEXT)
- `event_date` (DATE)
- `source_url` (VARCHAR)
- `discovered_at` (TIMESTAMP)

**EnrichedEvents:** LLM-processed events
- `enriched_event_id` (TEXT, PK)
- `raw_event_id` (TEXT, FK)
- `title`, `description`, `summary` (TEXT)
- `event_type` (VARCHAR) - see CyberEventType enum
- `severity` (VARCHAR) - Critical/High/Medium/Low
- `records_affected` (BIGINT)
- `confidence_score` (REAL, 0-1)
- `is_australian_event` (BOOLEAN)

**DeduplicatedEvents:** Final unique events after merging
- `deduplicated_event_id` (TEXT, PK)
- `master_enriched_event_id` (TEXT, FK)
- `title`, `description`, `summary` (TEXT)
- `event_type`, `severity`, `event_date` (various)
- `total_data_sources` (INTEGER) - count of merged sources
- `similarity_score` (REAL)

**ASDRiskClassifications:** Risk matrix assignments
- `classification_id` (TEXT, PK)
- `deduplicated_event_id` (TEXT, FK)
- `severity_category` (VARCHAR) - C1-C6
- `primary_stakeholder_category` (VARCHAR)
- `reasoning_json` (TEXT) - GPT-4o justification

**EntitiesV2:** Organizations and people mentioned
- `entity_id` (INTEGER, PK)
- `entity_name` (VARCHAR)
- `entity_type` (VARCHAR) - Government, Financial, Healthcare, etc.
- `industry` (VARCHAR)
- `is_australian` (BOOLEAN)

### Testing

- **Unit tests:** Located in `cyber_data_collector/tests/`, use pytest
- **Coverage target:** Aim for 70%+ coverage on core modules
- **Test naming:** `test_*.py` files with `def test_*()` functions

Run tests:
```bash
pytest                              # Run all tests
pytest cyber_data_collector/tests/  # Run specific test module
pytest --cov=cyber_data_collector   # With coverage report
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENAI_API_KEY` | Yes | OpenAI API key for GPT-4o and GPT-4o-mini |
| `PERPLEXITY_API_KEY` | Recommended | Perplexity AI API key for discovery and fact-checking |
| `GOOGLE_CUSTOMSEARCH_API_KEY` | Optional | Google Custom Search API key |
| `GOOGLE_CUSTOMSEARCH_CX_KEY` | Optional | Google Custom Search engine ID |
| `GOOGLE_CLOUD_PROJECT` | Optional (GDELT only) | Google Cloud project ID |
| `GOOGLE_APPLICATION_CREDENTIALS` | Optional (GDELT only) | Path to BigQuery service account JSON |
| `DATABASE_URL` | Optional | SQLite database URL (default: `sqlite:///instance/cyber_events.db`) |
| `MAX_THREADS` | Optional | Concurrent LLM processing threads (default: 10) |
| `BATCH_SIZE` | Optional | Event batch size (default: 20) |

## Additional Resources

- @README.md - Full project documentation with detailed usage guides
- @requirements.txt - All Python dependencies with version constraints
- `.env` - Local API keys and configuration (keep private, never commit)
- `documentation/` - Technical specifications and guides
- `specifications/` - Detailed data source and architecture documentation

## Deployment

### Local Development
```bash
python pipeline.py refresh
```

### Scheduled Monthly Updates
Create a cron job (Linux/Mac) or Task Scheduler (Windows):
```bash
0 2 1 * * cd /path/to/australian-cyber-events-scraper && python pipeline.py refresh
```

### Database Backups
The pipeline automatically creates timestamped backups before major operations:
```bash
ls instance/*.backup.*
```

## Key Design Decisions

1. **Unified Pipeline:** Single entry point (`run_full_pipeline.py`) ensures consistent processing
2. **Perplexity Integration:** Automatic enrichment after discovery provides AI fact-checking
3. **Entity-Based Deduplication:** Uses formal entity names instead of string similarity alone
4. **ASD Risk Framework:** Classifies events using Australian government risk matrix
5. **Static Dashboard:** Self-contained HTML (no server required) for easy sharing
6. **SQLite Database:** Lightweight, no external dependencies, portable across systems
7. **Concurrent Processing:** 10-thread LLM processing with semaphore rate limiting
8. **Type Safety:** Extensive use of Pydantic models and type hints

## Common Tasks

### Refresh Data (Monthly)
```bash
python pipeline.py refresh
```

### Check Status
```bash
python pipeline.py status
```

### Full Rebuild (Rare)
```bash
python pipeline.py rebuild --force
```

### Run Just Discovery
```bash
python run_full_pipeline.py --discover-only --days 30
```

### Skip Classification (Faster)
```bash
python run_full_pipeline.py --skip-classification
```

---

**Last Updated:** January 2025
**Python Version:** 3.8+
**Main Entry Points:** `run_full_pipeline.py`, `pipeline.py`


## Skill Usage Guide

When working on tasks involving these technologies, invoke the corresponding skill:

| Skill | Invoke When |
|-------|-------------|
| sqlite | Designs SQLite schema, migrations, and database query optimization |
| perplexity | Manages Perplexity AI API integration for discovery and fact-checking |
| pydantic | Handles data validation models and Pydantic schema definitions |
| python | Manages Python 3.8+ runtime, dependencies, and package management |
| openai | Configures GPT-4o and GPT-4o-mini API integration for enrichment |
| playwright | Configures browser automation for web scraping and content extraction |
| pandas | Handles data manipulation, aggregation, and CSV/Excel processing |
| scikit-learn | Trains and evaluates Random Forest ML models for event filtering |
| typer | Builds command-line interfaces with Typer argument parsing |
| openpyxl | Generates and manipulates Excel workbook files and sheets |
| google-cloud | Configures BigQuery integration for GDELT data source queries |
| pdfplumber | Extracts data and text content from PDF files |
| pytest | Writes and executes unit tests with pytest framework |
