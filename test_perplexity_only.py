#!/usr/bin/env python3
"""Simple Perplexity API test"""

import asyncio
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

from cyber_data_collector.datasources.perplexity import PerplexityDataSource
from cyber_data_collector.models.config import DataSourceConfig, DateRange
from cyber_data_collector.utils import RateLimiter

async def test_perplexity():
    # Load environment
    load_dotenv()

    # Check if API key is available
    api_key = os.getenv("PERPLEXITY_API_KEY")
    if not api_key:
        print("PERPLEXITY_API_KEY not found in environment")
        return False

    # Create config
    config = DataSourceConfig(
        name="perplexity",
        enabled=True,
        timeout=30,
        custom_config={}
    )

    # Create rate limiter
    rate_limiter = RateLimiter()

    # Create Perplexity data source
    env_config = {"PERPLEXITY_API_KEY": api_key}
    perplexity_source = PerplexityDataSource(config, rate_limiter, env_config)

    # Validate configuration
    if not perplexity_source.validate_config():
        print("Failed to validate Perplexity configuration")
        return False

    # Define date range (last 30 days)
    end_date = datetime.now()
    start_date = end_date - timedelta(days=30)
    date_range = DateRange(start_date=start_date, end_date=end_date)

    print(f"Testing Perplexity API for period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")

    # Test API call with a simple query
    try:
        events = await perplexity_source.collect_events(date_range)
        print(f"Successfully collected {len(events)} events from Perplexity API")

        # Print first few events for verification
        for i, event in enumerate(events[:2]):
            print(f"\nEvent {i+1}:")
            print(f"  Title: {event.title}")
            print(f"  Type: {event.event_type}")
            print(f"  Date: {event.event_date}")
            print(f"  Description: {event.description[:100]}..." if len(event.description) > 100 else f"  Description: {event.description}")

        return True

    except Exception as exc:
        print(f"Perplexity API test failed: {exc}")
        return False

if __name__ == "__main__":
    success = asyncio.run(test_perplexity())
    if success:
        print("\nPerplexity API test completed successfully!")
    else:
        print("\nPerplexity API test failed!")